"""Simulated portfolios: an append-only transaction ledger valued against a dataset version."""

from __future__ import annotations

import uuid
from typing import Any, Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select

from quantlab.analytics.performance import compute_performance, drawdown_series
from quantlab.analytics.risk import (
    StressScenario,
    apply_stress,
    concentration,
    monte_carlo,
    risk_decomposition,
    risk_summary,
)
from quantlab.analytics.summary import jsonable
from quantlab.data.adjust import total_returns
from quantlab.portfolio.ledger import LedgerError
from quantlab.portfolio.valuation import Entry, summary, time_weighted_returns, value_portfolio
from quantlab_api.errors import ApiError
from quantlab_api.models import Instrument, Portfolio, PortfolioTransaction, Role
from quantlab_api.routes.datasets import load_version
from quantlab_api.schemas import BoundedDate
from quantlab_api.security import DB, CurrentUser, audit, require_workspace
from quantlab_api.tasks import dataset_store

router = APIRouter(tags=["portfolios"])


class PortfolioIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    dataset_version_id: uuid.UUID
    benchmark: str | None = Field(None, max_length=32)
    initial_deposit: float = Field(gt=0, le=1e12)
    start_date: BoundedDate


class TransactionIn(BaseModel):
    trade_date: BoundedDate
    kind: Literal["deposit", "withdrawal", "buy", "sell"]
    symbol: str | None = Field(None, max_length=32)
    quantity: float = Field(0.0, ge=0, le=1e12)
    price: float | None = Field(None, gt=0, le=1e9)
    commission: float = Field(0.0, ge=0, le=1e9)
    amount: float = Field(0.0, ge=0, le=1e12)
    note: str = Field("", max_length=255)

    @model_validator(mode="after")
    def _shape(self) -> TransactionIn:
        if self.kind in ("buy", "sell"):
            if not self.symbol or self.quantity <= 0:
                raise ValueError("buys and sells need a symbol and a positive quantity")
        elif self.amount <= 0:
            raise ValueError("deposits and withdrawals need a positive amount")
        return self


def _entries(rows: list[PortfolioTransaction]) -> list[Entry]:
    return [
        Entry(
            seq=t.seq,
            date=t.trade_date,
            kind=t.kind,
            symbol=t.symbol,
            quantity=t.quantity,
            price=t.price or None,
            commission=t.commission,
            amount=t.amount,
        )
        for t in rows
    ]


def _load(db: DB, user: CurrentUser, pid: uuid.UUID, role: Role = Role.viewer) -> Portfolio:
    p = db.get(Portfolio, pid)
    if p is None:
        raise ApiError(404, "not_found", "Portfolio not found")
    require_workspace(db, user, p.workspace_id, role)
    return p


def _market(
    db: DB, user: CurrentUser, p: Portfolio, symbols: set[str]
) -> tuple[pd.DataFrame, pd.DataFrame, bool]:
    dv, ds = load_version(db, user, p.dataset_version_id)
    store = dataset_store()
    v = store.open(ds.slug, dv.version_hash)
    syms = sorted(symbols | ({p.benchmark} if p.benchmark else set()))
    bars = store.load_bars(v, symbols=syms or None)
    return bars, store.load_actions(v, symbols=syms or None), dv.is_synthetic


def _tx_out(t: PortfolioTransaction) -> dict[str, Any]:
    return {
        "seq": t.seq,
        "trade_date": t.trade_date,
        "kind": t.kind,
        "symbol": t.symbol,
        "quantity": t.quantity,
        "price": t.price,
        "commission": t.commission,
        "amount": t.amount,
        "note": t.note,
    }


@router.post("/workspaces/{workspace_id}/portfolios", status_code=201)
def create_portfolio(
    workspace_id: uuid.UUID, body: PortfolioIn, user: CurrentUser, db: DB, request: Request
) -> dict[str, Any]:
    require_workspace(db, user, workspace_id, Role.editor)
    dv, ds = load_version(db, user, body.dataset_version_id)
    if ds.workspace_id != workspace_id:
        raise ApiError(422, "validation_error", "dataset version belongs to another workspace")
    bench = body.benchmark.upper() if body.benchmark else None
    if bench and not db.scalar(
        select(Instrument.id).where(
            Instrument.dataset_version_id == dv.id, Instrument.symbol == bench
        )
    ):
        raise ApiError(422, "unknown_symbols", f"{bench} is not in this dataset version")
    p = Portfolio(
        workspace_id=workspace_id,
        name=body.name,
        dataset_version_id=dv.id,
        benchmark=bench,
        constraints={},
        created_by=user.id,
    )
    db.add(p)
    db.flush()
    t = PortfolioTransaction(
        portfolio_id=p.id,
        seq=1,
        trade_date=body.start_date,
        kind="deposit",
        amount=body.initial_deposit,
        note="initial deposit",
    )
    try:
        bars, actions, _ = _market(db, user, p, set())
        value_portfolio(_entries([t]), bars, actions)
    except LedgerError as exc:
        db.rollback()
        raise ApiError(422, "invalid_transaction", str(exc)) from exc
    db.add(t)
    audit(
        db,
        request,
        "portfolio.create",
        user=user,
        workspace_id=workspace_id,
        target_type="portfolio",
        target_id=p.id,
    )
    db.commit()
    return {"id": str(p.id), "name": p.name, "benchmark": p.benchmark}


@router.get("/workspaces/{workspace_id}/portfolios")
def list_portfolios(workspace_id: uuid.UUID, user: CurrentUser, db: DB) -> list[dict[str, Any]]:
    require_workspace(db, user, workspace_id)
    rows = db.execute(
        select(Portfolio, func.count(PortfolioTransaction.id))
        .outerjoin(PortfolioTransaction, PortfolioTransaction.portfolio_id == Portfolio.id)
        .where(Portfolio.workspace_id == workspace_id)
        .group_by(Portfolio.id)
        .order_by(Portfolio.created_at.desc())
    ).all()
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "benchmark": p.benchmark,
            "dataset_version_id": str(p.dataset_version_id),
            "transactions": n,
            "created_at": p.created_at,
        }
        for p, n in rows
    ]


@router.get("/portfolios/{portfolio_id}")
def get_portfolio(portfolio_id: uuid.UUID, user: CurrentUser, db: DB) -> dict[str, Any]:
    p = _load(db, user, portfolio_id)
    return {
        "id": str(p.id),
        "name": p.name,
        "benchmark": p.benchmark,
        "dataset_version_id": str(p.dataset_version_id),
        "transactions": [_tx_out(t) for t in p.transactions],
    }


@router.post("/portfolios/{portfolio_id}/transactions", status_code=201)
def add_transaction(
    portfolio_id: uuid.UUID, body: TransactionIn, user: CurrentUser, db: DB, request: Request
) -> dict[str, Any]:
    """Append a transaction. The whole ledger is replayed first; rejected entries are not
    stored, so the ledger can never reach an impossible state."""
    p = _load(db, user, portfolio_id, Role.editor)
    # Serialize appends per portfolio: the replay must see the latest ledger, and the
    # next sequence number must be unique.
    db.execute(select(Portfolio.id).where(Portfolio.id == p.id).with_for_update())
    db.refresh(p, ["transactions"])
    seq = (max((t.seq for t in p.transactions), default=0)) + 1
    t = PortfolioTransaction(
        portfolio_id=p.id,
        seq=seq,
        trade_date=body.trade_date,
        kind=body.kind,
        symbol=body.symbol.upper() if body.symbol else None,
        quantity=body.quantity,
        price=body.price or 0.0,
        commission=body.commission,
        amount=body.amount,
        note=body.note,
    )
    entries = _entries([*p.transactions, t])
    symbols = {e.symbol for e in entries if e.symbol}
    bars, actions, _ = _market(db, user, p, symbols)
    unknown = symbols - set(bars["symbol"].unique())
    if unknown:
        raise ApiError(422, "unknown_symbols", "Symbols not in dataset version", sorted(unknown))
    try:
        val = value_portfolio(entries, bars, actions)
    except LedgerError as exc:
        raise ApiError(422, "invalid_transaction", str(exc)) from exc
    if t.kind in ("buy", "sell") and not body.price:
        t.price = val.resolved_prices[seq]
    db.add(t)
    audit(
        db,
        request,
        "portfolio.transaction",
        user=user,
        workspace_id=p.workspace_id,
        target_type="portfolio",
        target_id=p.id,
        seq=seq,
        kind=t.kind,
    )
    db.commit()
    return _tx_out(t)


@router.get("/portfolios/{portfolio_id}/valuation")
def valuation(portfolio_id: uuid.UUID, user: CurrentUser, db: DB) -> dict[str, Any]:
    p = _load(db, user, portfolio_id)
    entries = _entries(list(p.transactions))
    symbols = {e.symbol for e in entries if e.symbol}
    bars, actions, synthetic = _market(db, user, p, symbols)
    try:
        val = value_portfolio(entries, bars, actions)
    except LedgerError as exc:
        raise ApiError(409, "invalid_ledger", str(exc)) from exc
    twr = time_weighted_returns(val.equity)
    index = 100 * (1 + twr).cumprod()
    index = pd.concat([pd.Series([100.0], index=[val.equity.index[0]]), index])
    bench_index = None
    if p.benchmark:
        b = bars[bars["symbol"] == p.benchmark]
        br = total_returns(b, actions)[p.benchmark].reindex(index.index).fillna(0.0)
        br.iloc[0] = 0.0
        bench_index = 100 * (1 + br).cumprod()
    perf = compute_performance(index, bench_index)
    risk: dict[str, Any] = {"summary": risk_summary(twr)}
    weights = (
        val.holdings.set_index("symbol")["weight"] if len(val.holdings) else pd.Series(dtype=float)
    )
    risk["concentration"] = concentration(weights)
    held = [s for s in weights.index]
    if held:
        rets = total_returns(bars[bars["symbol"].isin(held)], actions).iloc[-252:][held]
        try:
            rd = risk_decomposition(weights, rets)
            risk["contributions"] = [
                {"symbol": s, **{k: float(v) for k, v in r.items()}} for s, r in rd.iterrows()
            ]
        except Exception as exc:  # e.g. too little overlapping history
            risk["contributions_error"] = str(exc)
        if p.benchmark:
            bret = total_returns(bars[bars["symbol"] == p.benchmark], actions)[p.benchmark]
            br = bret.reindex(rets.index)
            betas = pd.Series(
                {
                    s: float(np.cov(rets[s].fillna(0), br.fillna(0))[0, 1] / max(br.var(), 1e-12))
                    for s in held
                }
            )
            risk["stress"] = [
                apply_stress(weights, StressScenario("Market falls 20%", {}, -0.20), betas),
                apply_stress(weights, StressScenario("Market falls 35%", {}, -0.35), betas),
            ]
    if len(twr) >= 60:
        mc = monte_carlo(twr, horizon=252, n_paths=1000, seed=0)
        mc.pop("fan", None)
        risk["monte_carlo"] = mc
    dd = drawdown_series(index)
    return jsonable(
        {
            "portfolio": {
                "id": str(p.id),
                "name": p.name,
                "benchmark": p.benchmark,
                "is_synthetic": synthetic,
            },
            "summary": summary(val),
            "series": {
                "dates": [d.strftime("%Y-%m-%d") for d in index.index],
                "index": index.round(6).tolist(),
                "benchmark": bench_index.round(6).tolist() if bench_index is not None else None,
                "drawdown": dd.round(6).tolist(),
                "equity": val.equity["equity"].round(2).tolist(),
            },
            "holdings": val.holdings.to_dict("records"),
            "performance": perf.to_dict(),
            "risk": risk,
        }
    )
