"""Reconciliation against a second source (issue #12).

SYNTHETIC DATA ONLY: the primary is a generated market and every "second source" is a
copy of it, either perturbed on purpose or round-tripped through the CSV provider. The
discrepancies are planted, so each test knows exactly what must be found.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from quantlab.data.providers import CSVProvider
from quantlab.data.reconcile import CLASSES, Source, Tolerances, reconcile
from quantlab.data.synthetic import SyntheticSpec, generate_market


@pytest.fixture(scope="module")
def mkt():
    return generate_market(
        SyntheticSpec(
            n_assets=6, start="2021-01-04", end="2021-12-31", seed=4, split_probability=0.004
        )
    )


def _primary(m) -> Source:
    return Source("primary", m.bars, m.actions, m.symbol_history, is_synthetic=True)


def _secondary(m, bars=None, actions=None, history=None, name="secondary") -> Source:
    return Source(
        name,
        m.bars.copy() if bars is None else bars,
        m.actions.copy() if actions is None else actions,
        m.symbol_history if history is None else history,
        is_synthetic=True,
    )


def _only(report, cls: str, n: int) -> None:
    assert report.counts[cls] == n, report.counts
    others = {c: k for c, k in report.counts.items() if c != cls and k}
    assert not others, others


def test_identical_sources_reconcile_cleanly(mkt):
    r = reconcile(_primary(mkt), _secondary(mkt))
    assert r.status == "pass" and not any(r.counts.values())
    assert r.instruments["bars_compared"] == len(mkt.bars)
    assert r.synthetic_inputs is True
    json.dumps(r.to_dict())  # persistable as-is


def test_csv_round_trip_as_second_source(mkt, tmp_path):
    for sub in ("bars", "actions"):
        (tmp_path / sub).mkdir()
    for sym, g in mkt.bars.groupby("symbol"):
        g.drop(columns=["symbol", "available_at"]).to_csv(
            tmp_path / "bars" / f"{sym}.csv", index=False
        )
    for sym, g in mkt.actions.groupby("symbol"):
        g.drop(columns=["symbol"]).to_csv(tmp_path / "actions" / f"{sym}.csv", index=False)
    csv = CSVProvider(tmp_path, source="synthetic CSV copy", license="synthetic")
    s, e = mkt.bars["date"].min(), mkt.bars["date"].max()
    bars = pd.concat([csv.fetch_bars(x, s, e) for x in csv.symbols()])
    acts = pd.concat([csv.fetch_actions(x, s, e) for x in csv.symbols()])
    r = reconcile(_primary(mkt), _secondary(mkt, bars, acts, name="csv"))
    assert r.status == "pass", r.samples


def _pick(m, k: int = 1) -> str:
    return sorted(m.bars["symbol"].unique())[k]


def test_missing_bar(mkt):
    sym = _pick(mkt)
    rows = mkt.bars.index[mkt.bars["symbol"] == sym]
    r = reconcile(_primary(mkt), _secondary(mkt, bars=mkt.bars.drop(index=rows[100])))
    _only(r, "missing_bar", 1)
    assert r.samples["missing_bar"][0]["missing_in"] == "secondary"
    assert r.status == "fail"


@pytest.mark.parametrize(("factor", "flagged"), [(1.01, True), (1.0001, False)])
def test_price_mismatch_respects_tolerance(mkt, factor, flagged):
    b = mkt.bars.copy()
    i = b.index[b["symbol"] == _pick(mkt)][50]
    b.loc[i, "close"] *= factor
    r = reconcile(_primary(mkt), _secondary(mkt, bars=b))
    if flagged:
        _only(r, "price_mismatch", 1)
    else:
        assert r.status == "pass"
    loose = reconcile(_primary(mkt), _secondary(mkt, bars=b), tolerances=Tolerances(close_rel=0.02))
    assert loose.counts["price_mismatch"] == 0  # tolerances are configurable


def test_volume_mismatch_is_a_warning(mkt):
    b = mkt.bars.copy()
    i = b.index[b["symbol"] == _pick(mkt)][10]
    b.loc[i, "volume"] *= 1.5
    r = reconcile(_primary(mkt), _secondary(mkt, bars=b))
    _only(r, "volume_mismatch", 1)
    assert r.status == "warn"


def test_action_missing_and_action_mismatch(mkt):
    divs = mkt.actions.index[mkt.actions["kind"] == "dividend"]
    assert len(divs) >= 2
    dropped = reconcile(_primary(mkt), _secondary(mkt, actions=mkt.actions.drop(index=divs[0])))
    _only(dropped, "action_missing", 1)
    changed = mkt.actions.copy()
    changed.loc[divs[1], "value"] += 0.01
    r = reconcile(_primary(mkt), _secondary(mkt, actions=changed))
    _only(r, "action_mismatch", 1)
    s = r.samples["action_mismatch"][0]
    assert s["value_s"] - s["value_p"] == pytest.approx(0.01)


def test_split_ratio_mismatch(mkt):
    splits = mkt.actions.index[mkt.actions["kind"] == "split"]
    if not len(splits):
        pytest.skip("no split in this synthetic draw")
    a = mkt.actions.copy()
    a.loc[splits[0], "value"] *= 2
    r = reconcile(_primary(mkt), _secondary(mkt, actions=a))
    _only(r, "action_mismatch", 1)


def test_regular_plus_special_dividend_matches_their_total(mkt):
    divs = mkt.actions[mkt.actions["kind"] == "dividend"]
    row = divs.iloc[[0]]
    halves = pd.concat(
        [row.assign(value=row["value"] * 0.25), row.assign(value=row["value"] * 0.75)]
    )
    a = pd.concat([mkt.actions.drop(index=row.index), halves], ignore_index=True)
    r = reconcile(_primary(mkt), _secondary(mkt, actions=a))
    assert r.counts["action_missing"] == 0 and r.counts["action_mismatch"] == 0


def test_listing_mismatch_without_spurious_missing_bars(mkt):
    sym = _pick(mkt, 2)
    rows = mkt.bars.index[mkt.bars["symbol"] == sym]
    late = mkt.bars.drop(index=rows[:3])  # the second source starts 3 sessions later
    acts = mkt.actions[
        ~(
            (mkt.actions["symbol"] == sym)
            & (mkt.actions["ex_date"] <= mkt.bars.loc[rows[2], "date"])
        )
    ]
    r = reconcile(_primary(mkt), _secondary(mkt, bars=late, actions=acts))
    _only(r, "listing_mismatch", 1)
    assert r.status == "warn"


def test_identity_mismatches(mkt):
    # 1. An instrument the second source does not carry at all.
    sym = _pick(mkt, 3)
    gone = mkt.bars[mkt.bars["symbol"] != sym]
    r = reconcile(
        _primary(mkt), _secondary(mkt, bars=gone, actions=mkt.actions[mkt.actions["symbol"] != sym])
    )
    _only(r, "identity_mismatch", 1)
    assert r.samples["identity_mismatch"][0]["issue"] == "only in primary"

    # 2. Same prices, but the second source says the security changed ticker mid-year.
    sym = _pick(mkt, 4)
    h = mkt.symbol_history
    iid = h.loc[h["symbol"] == sym, "instrument_id"].iloc[0]
    switch = pd.Timestamp("2021-07-01")
    row = h[h["instrument_id"] == iid].iloc[0]
    renamed = pd.concat(
        [
            h[h["instrument_id"] != iid],
            pd.DataFrame(
                [
                    {**row, "valid_to": switch},
                    {**row, "symbol": f"{sym}X", "valid_from": switch},
                ]
            ),
        ],
        ignore_index=True,
    )
    b = mkt.bars.copy()
    b.loc[(b["symbol"] == sym) & (b["date"] >= switch), "symbol"] = f"{sym}X"
    a = mkt.actions.copy()
    a.loc[(a["symbol"] == sym) & (a["ex_date"] >= switch), "symbol"] = f"{sym}X"
    r = reconcile(_primary(mkt), _secondary(mkt, bars=b, actions=a, history=renamed))
    _only(r, "identity_mismatch", 1)
    s = r.samples["identity_mismatch"][0]
    assert (
        s["issue"] == "tickers differ" and s["ticker_s"] == f"{sym}X" and s["from"] >= "2021-07-01"
    )


def test_id_map_bridges_vendor_id_spaces(mkt):
    h = mkt.symbol_history
    vendor = h.assign(instrument_id="V" + h["instrument_id"])
    id_map = dict(zip(vendor["instrument_id"], h["instrument_id"], strict=True))
    unmapped = reconcile(_primary(mkt), _secondary(mkt, history=vendor))
    assert unmapped.counts["identity_mismatch"] == 2 * h["instrument_id"].nunique()
    mapped = reconcile(_primary(mkt), _secondary(mkt, history=vendor), id_map=id_map)
    assert mapped.status == "pass"


def test_every_class_has_a_configurable_severity(mkt):
    assert set(Tolerances().severity) == set(CLASSES)
    b = mkt.bars.copy()
    b.loc[b.index[5], "volume"] *= 3
    lenient = Tolerances(severity={**Tolerances().severity, "volume_mismatch": "fail"})
    assert reconcile(_primary(mkt), _secondary(mkt, bars=b), tolerances=lenient).status == "fail"
    assert np.isfinite(Tolerances().close_rel)


def test_report_is_attached_beside_the_immutable_version(mkt, tmp_path):
    from quantlab.data.store import DatasetStore

    store = DatasetStore(tmp_path)
    dv = store.write(
        "demo", mkt.bars, mkt.actions, mkt.provenance, symbol_history=mkt.symbol_history
    )
    before = sorted(p.name for p in dv.path.iterdir())
    report = reconcile(_primary(mkt), _secondary(mkt)).to_dict()
    store.write_report(dv, "reconciliation-csv", report)
    assert sorted(p.name for p in dv.path.iterdir()) == before  # version untouched
    assert store.verify(dv)
    assert store.load_reports(dv)["reconciliation-csv"]["status"] == "pass"
    assert [v.version for v in store.versions("demo")] == [dv.version]
    with pytest.raises(ValueError):
        store.write_report(dv, "../escape", report)
