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
from quantlab.data.schema import normalize_actions, normalize_bars
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
    # Ticker-keyed, with no history of its own: resolved through the primary's history.
    r = reconcile(_primary(mkt), Source("csv", bars, acts, None, is_synthetic=True))
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


# ------------------------------------------------- lead review of #34: required fixes
def test_non_finite_values_are_flagged_not_passed(mkt):
    b = mkt.bars.copy()
    rows = b.index[b["symbol"] == _pick(mkt)]
    b.loc[rows[30], "close"] = np.nan
    b.loc[rows[31], "volume"] = np.inf
    r = reconcile(_primary(mkt), _secondary(mkt, bars=b))
    _only(r, "invalid_value", 2)
    assert r.status == "fail"


def _renamed_primary(mkt):
    """The primary says one security changed ticker on 2021-07-01 (FB -> META style)."""
    sym = _pick(mkt, 4)
    h = mkt.symbol_history
    iid = h.loc[h["symbol"] == sym, "instrument_id"].iloc[0]
    row = h[h["instrument_id"] == iid].iloc[0]
    switch = pd.Timestamp("2021-07-01")
    history = pd.concat(
        [
            h[h["instrument_id"] != iid],
            pd.DataFrame(
                [{**row, "valid_to": switch}, {**row, "symbol": f"{sym}X", "valid_from": switch}]
            ),
        ],
        ignore_index=True,
    )
    b, a = mkt.bars.copy(), mkt.actions.copy()
    b.loc[(b["symbol"] == sym) & (b["date"] >= switch), "symbol"] = f"{sym}X"
    a.loc[(a["symbol"] == sym) & (a["ex_date"] >= switch), "symbol"] = f"{sym}X"
    return Source("primary", b, a, history, is_synthetic=True), sym


def test_history_less_secondary_resolves_a_rename_through_the_primary(mkt):
    primary, _ = _renamed_primary(mkt)
    # Identical data, ticker-keyed, no history: previously an identity failure.
    r = reconcile(primary, Source("csv", primary.bars, primary.actions, None, is_synthetic=True))
    assert r.status == "pass", r.samples


def test_history_less_secondary_with_an_unknown_ticker(mkt):
    primary, sym = _renamed_primary(mkt)
    b = primary.bars.copy()
    # The secondary still uses the old ticker after the rename date.
    b.loc[b["symbol"] == f"{sym}X", "symbol"] = sym
    r = reconcile(primary, Source("csv", b, primary.actions, None, is_synthetic=True))
    assert r.counts["identity_mismatch"] >= 1
    issue = r.samples["identity_mismatch"][0]
    assert issue["ticker"] == sym and "unknown to primary" in issue["issue"]
    assert issue["from"] >= "2021-07-01"


@pytest.mark.parametrize(("shift", "expect"), [(1, "mismatch"), (3, "mismatch"), (6, "missing")])
def test_ex_date_tolerance_pairs_a_late_action(mkt, shift, expect):
    from quantlab.data.calendar import trading_days

    divs = mkt.actions.index[mkt.actions["kind"] == "dividend"]
    i = divs[len(divs) // 2]
    a = mkt.actions.copy()
    sessions = trading_days(a.loc[i, "ex_date"], a.loc[i, "ex_date"] + pd.Timedelta(days=20))
    a.loc[i, "ex_date"] = sessions[shift]  # recorded `shift` sessions late
    r = reconcile(_primary(mkt), _secondary(mkt, actions=a))
    if expect == "mismatch":
        _only(r, "action_mismatch", 1)
        assert r.samples["action_mismatch"][0]["issue"] == "ex_date differs"
    else:
        _only(r, "action_missing", 2)


def test_report_records_its_inputs_and_is_never_silently_overwritten(mkt, tmp_path):
    from quantlab.data.store import DatasetStore, content_hash

    store = DatasetStore(tmp_path)
    dv = store.write(
        "demo", mkt.bars, mkt.actions, mkt.provenance, symbol_history=mkt.symbol_history
    )
    p = Source("primary", mkt.bars, mkt.actions, mkt.symbol_history, True, version=dv.version)
    s = Source(
        "vendor-b",
        mkt.bars,
        mkt.actions,
        None,
        True,
        provider={"provider": "synthetic copy", "license": "synthetic"},
    )
    rep = reconcile(p, s).to_dict()
    assert rep["inputs"]["primary"]["version"] == dv.version
    assert rep["inputs"]["primary"]["version_is_computed"] is False
    assert rep["inputs"]["secondary"]["version"] == content_hash(
        normalize_bars(mkt.bars), normalize_actions(mkt.actions)
    )
    assert rep["inputs"]["secondary"]["provider"]["provider"] == "synthetic copy"
    assert rep["created_at"].endswith("+00:00")
    store.write_report(dv, "reconciliation-vendor-b", rep)
    with pytest.raises(FileExistsError):
        store.write_report(dv, "reconciliation-vendor-b", rep)
    store.write_report(dv, "reconciliation-vendor-b", {**rep, "status": "warn"}, replace=True)
    assert store.load_reports(dv)["reconciliation-vendor-b"]["status"] == "warn"
    with pytest.raises(TypeError):  # non-JSON payloads raise instead of being stringified
        store.write_report(dv, "bad", {"when": pd.Timestamp("2024-01-02")})
    leftovers = [
        x
        for x in (tmp_path / "demo" / "_reports" / dv.version).iterdir()
        if x.name.startswith(".tmp-")
    ]
    assert leftovers == []


def test_adjusted_secondary_is_classified_once_per_instrument(mkt):
    from quantlab.data.adjust import adjusted_close

    adj = adjusted_close(mkt.bars, mkt.actions).stack().rename("close").reset_index()
    b = mkt.bars.drop(columns="close").merge(adj, on=["date", "symbol"])
    r = reconcile(_primary(mkt), _secondary(mkt, bars=b))
    with_actions = set(mkt.actions["symbol"])
    flagged = {row["instrument_id"] for row in r.samples["adjusted_series"]}
    hist = mkt.symbol_history.set_index("symbol")["instrument_id"]
    assert flagged and flagged <= {hist[s] for s in with_actions}
    assert r.counts["price_mismatch"] == 0  # explained, not reported bar by bar


def test_price_floor_scales_with_price():
    from tests.conftest import make_bars

    def run(p_close: float, s_close: float) -> int:
        pb = make_bars({"A": [p_close] * 3})
        sb = make_bars({"A": [p_close, s_close, p_close]})
        none = normalize_actions(None)
        return reconcile(Source("p", pb, none), Source("s", sb, none)).counts["price_mismatch"]

    assert run(0.02, 0.024) == 1  # 20% on a penny stock is a mismatch
    assert run(0.02, 0.02004) == 0  # within half a sub-penny tick
    assert run(10.0, 10.004) == 0  # half-cent rounding
    assert run(10.0, 10.02) == 1


def test_many_to_one_id_map_is_rejected(mkt):
    h = mkt.symbol_history
    vendor = h.assign(instrument_id="V" + h["instrument_id"])
    ids = list(vendor["instrument_id"])
    bad = {ids[0]: h["instrument_id"].iloc[0], ids[1]: h["instrument_id"].iloc[0]}
    with pytest.raises(ValueError, match="one-to-one"):
        reconcile(_primary(mkt), _secondary(mkt, history=vendor), id_map=bad)


# ---------------------------------------------------------------- #54 follow-ups
def _scaled(mkt, sym: str, factor: float) -> pd.DataFrame:
    b = mkt.bars.copy()
    rows = b["symbol"] == sym
    b.loc[rows, "close"] *= factor
    return b


def test_a_constant_ratio_is_a_scale_mismatch_not_an_adjusted_series(mkt):
    sym = _pick(mkt, 1)
    for factor in (100.0, 1.0008):  # pence vs pounds; a steady 8 bp offset
        r = reconcile(_primary(mkt), _secondary(mkt, bars=_scaled(mkt, sym, factor)))
        assert r.counts["scale_mismatch"] == 1, (factor, r.counts)
        assert r.counts["adjusted_series"] == 0 and r.counts["price_mismatch"] == 0
        assert r.samples["scale_mismatch"][0]["ratios"] == [pytest.approx(factor, rel=1e-6)]


def test_a_bad_print_inside_an_adjusted_series_is_still_reported(mkt):
    from quantlab.data.adjust import adjusted_close

    adj = adjusted_close(mkt.bars, mkt.actions).stack().rename("close").reset_index()
    b = mkt.bars.drop(columns="close").merge(adj, on=["date", "symbol"])
    flagged = reconcile(_primary(mkt), _secondary(mkt, bars=b))
    iid = flagged.samples["adjusted_series"][0]["instrument_id"]
    sym = mkt.symbol_history.set_index("instrument_id").loc[iid, "symbol"]
    i = b.index[b["symbol"] == sym][40]
    b.loc[i, "close"] *= 1.003  # 30 bp: above the 5 bp tolerance, inside the old 1e-3 band
    r = reconcile(_primary(mkt), _secondary(mkt, bars=b))
    assert iid in {x["instrument_id"] for x in r.samples["adjusted_series"]}
    bad = [x for x in r.samples.get("price_mismatch", []) if x["instrument_id"] == iid]
    assert len(bad) == 1


def test_duplicate_rows_in_a_history_less_secondary_are_rejected(mkt):
    from quantlab.data.identity import IdentityError

    b = pd.concat([mkt.bars, mkt.bars.iloc[[10]]], ignore_index=True)
    with pytest.raises(IdentityError, match="duplicate bars"):
        reconcile(_primary(mkt), Source("csv", b, mkt.actions, None, is_synthetic=True))


def test_crossed_dividends_pair_by_value():
    from tests.conftest import make_bars

    bars = make_bars({"A": [100.0] * 10})
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    rows = lambda pairs: normalize_actions(  # noqa: E731
        pd.DataFrame(
            [{"symbol": "A", "ex_date": d[k], "kind": "dividend", "value": v} for k, v in pairs]
        )
    )
    p = Source("p", bars, rows([(3, 0.50), (4, 2.00)]))
    s = Source("s", bars, rows([(4, 0.50), (5, 2.00)]))  # both one session late
    r = reconcile(p, s)
    pairs = {(x["value_p"], x["value_s"]) for x in r.samples["action_mismatch"]}
    assert pairs == {(0.5, 0.5), (2.0, 2.0)}  # not (0.5, 2.0) and (2.0, 0.5)
    assert all(x["issue"] == "ex_date differs" for x in r.samples["action_mismatch"])


def test_infinite_values_are_shown_as_such(mkt):
    b = mkt.bars.copy()
    b.loc[b.index[5], "volume"] = np.inf
    r = reconcile(_primary(mkt), _secondary(mkt, bars=b))
    assert r.samples["invalid_value"][0]["volume_s"] == "inf"


def test_a_supplied_version_is_checked_against_the_data(mkt):
    p = Source("p", mkt.bars, mkt.actions, mkt.symbol_history, True, version="0" * 64)
    r = reconcile(p, _secondary(mkt))
    assert r.inputs["primary"]["version_matches_data"] is False
    assert r.inputs["secondary"]["version_matches_data"] is True


def test_id_map_targets_already_used_natively_are_rejected(mkt):
    h = mkt.symbol_history
    ids = list(h["instrument_id"])
    vendor = h.assign(instrument_id=["V" + ids[0], *ids[1:]])  # one vendor id, rest native
    with pytest.raises(ValueError, match="already used"):
        reconcile(_primary(mkt), _secondary(mkt, history=vendor), id_map={"V" + ids[0]: ids[1]})


def test_identity_sample_counts_are_integers(mkt):
    sym = _pick(mkt, 3)
    b = mkt.bars.copy()
    b.loc[b["symbol"] == sym, "symbol"] = "ZZZ"  # a ticker the primary does not know
    a = mkt.actions[mkt.actions["symbol"] != sym]
    r = reconcile(_primary(mkt), Source("csv", b, a, None, is_synthetic=True))
    unknown = [x for x in r.samples["identity_mismatch"] if x.get("ticker") == "ZZZ"]
    assert unknown and isinstance(unknown[0]["rows"], int)


def test_report_writes_cannot_race_into_an_overwrite(mkt, tmp_path, monkeypatch):
    from pathlib import Path

    from quantlab.data.store import DatasetStore

    store = DatasetStore(tmp_path)
    dv = store.write("demo", mkt.bars, mkt.actions, mkt.provenance)
    store.write_report(dv, "reconciliation-a", {"status": "pass"})
    # A second writer that passed the existence check just before the first one wrote.
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(FileExistsError):
        store.write_report(dv, "reconciliation-a", {"status": "fail"})
    monkeypatch.undo()
    assert store.load_reports(dv)["reconciliation-a"]["status"] == "pass"


def test_a_stored_version_with_a_master_matches_its_data(mkt, tmp_path):
    from quantlab.data.identity import normalize_master
    from quantlab.data.store import DatasetStore

    ids = sorted(mkt.symbol_history["instrument_id"].unique())
    master = normalize_master(
        pd.DataFrame(
            {
                "instrument_id": ids[:1],
                "delisting_date": ["2021-12-31"],
                "delisting_return": [-0.25],
            }
        )
    )
    store = DatasetStore(tmp_path)
    dv = store.write(
        "demo",
        mkt.bars,
        mkt.actions,
        mkt.provenance,
        symbol_history=mkt.symbol_history,
        master=master,
    )
    assert store.verify(dv)
    secondary = _secondary(mkt)
    good = reconcile(Source.from_version(store, dv, "stored", is_synthetic=True), secondary)
    assert good.inputs["primary"]["version_matches_data"] is True
    assert good.inputs["primary"]["version"] == dv.version

    def hand_built(m):
        return Source(
            "stored", mkt.bars, mkt.actions, mkt.symbol_history, True, version=dv.version, master=m
        )

    def matches(src):
        return reconcile(src, secondary).inputs["primary"]["version_matches_data"]

    assert matches(hand_built(master)) is True
    # Leaving the master out, or passing a different one, is different data.
    assert matches(hand_built(None)) is False
    assert matches(hand_built(master.assign(delisting_return=[-0.5]))) is False


# ------------------------------------------- lead review of #76: ratio levels
def _raw_series(n: int = 60, events=((20, "dividend", 1.0), (40, "dividend", 1.0))):
    """One raw instrument with a gentle drift and its actions (synthetic, hand-built)."""
    from tests.conftest import make_bars

    closes = [100.0 * (1.001**k) for k in range(n)]
    for k, kind, value in events:  # the raw close drops by the event at its ex-date
        for j in range(k, n):
            closes[j] = closes[j] / value if kind == "split" else closes[j] - value
    bars = make_bars({"A": closes})
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    acts = normalize_actions(
        pd.DataFrame(
            [{"symbol": "A", "ex_date": d[k], "kind": kind, "value": v} for k, kind, v in events]
        )
    )
    return bars, acts


def _adjusted_copy(bars, acts):
    from quantlab.data.adjust import adjusted_close

    adj = adjusted_close(bars, acts).stack().rename("close").reset_index()
    return bars.drop(columns="close").merge(adj, on=["date", "symbol"])


def _ratio_run(p_bars, p_acts, s_bars):
    return reconcile(Source("p", p_bars, p_acts), Source("s", s_bars, p_acts))


def test_bad_prints_between_two_dividends_are_not_an_adjusted_series():
    # Ten bad prints filling the segment between two dividends: levels [1.0, 0.97, 1.0].
    bars, acts = _raw_series(events=((20, "dividend", 1.0), (30, "dividend", 1.0)))
    bad = bars.copy()
    bad.loc[bad.index[20:30], "close"] *= 0.97
    r = _ratio_run(bars, acts, bad)
    assert r.counts["adjusted_series"] == 0 and r.counts["scale_mismatch"] == 0
    assert r.counts["price_mismatch"] == 10


def test_bad_prints_before_the_first_dividend_are_not_an_adjusted_series():
    # [0.97, 1.0] ends at 1 and is monotone, but a 3% step is not a 1% dividend.
    bars, acts = _raw_series()
    bad = bars.copy()
    bad.loc[bad.index[:20], "close"] *= 0.97
    r = _ratio_run(bars, acts, bad)
    assert r.counts["adjusted_series"] == 0 and r.counts["price_mismatch"] == 20


def test_a_one_session_segment_cannot_become_its_own_level():
    # Ex-dates on consecutive sessions leave a 1-bar segment; a bad print there stays a
    # price mismatch even though the rest of the series is genuinely adjusted.
    bars, acts = _raw_series(events=((20, "dividend", 1.0), (21, "dividend", 1.0)))
    adj = _adjusted_copy(bars, acts)
    adj.loc[adj.index[20], "close"] *= 1.05
    r = _ratio_run(bars, acts, adj)
    assert r.counts["adjusted_series"] == 1
    assert [x["date"] for x in r.samples["price_mismatch"]] == [str(bars["date"].iloc[20].date())]


@pytest.mark.parametrize(
    "events",
    [
        ((20, "dividend", 1.0), (40, "dividend", 1.0)),
        ((20, "split", 2.0), (40, "dividend", 0.5)),
        ((20, "split", 0.1), (40, "dividend", 1.0)),  # a reverse split steps the other way
    ],
)
@pytest.mark.parametrize("adjusted_side", ["secondary", "primary"])
def test_a_genuine_adjusted_series_is_still_explained(events, adjusted_side):
    bars, acts = _raw_series(events=events)
    adj = _adjusted_copy(bars, acts)
    p, s = (bars, adj) if adjusted_side == "secondary" else (adj, bars)
    r = _ratio_run(p, acts, s)
    assert r.counts["adjusted_series"] == 1, r.counts
    assert r.counts["price_mismatch"] == 0


def test_a_split_only_adjusted_series_is_explained():
    events = ((20, "split", 2.0), (40, "dividend", 1.0))
    bars, acts = _raw_series(events=events)
    adj = _adjusted_copy(bars, acts[acts["kind"] == "split"])
    r = _ratio_run(bars, acts, adj)
    assert r.counts["adjusted_series"] == 1 and r.counts["price_mismatch"] == 0


def test_steps_in_mixed_orientations_are_not_explained():
    # Adjusted for the first dividend on one side and the second on the other.
    bars, acts = _raw_series()
    first, second = acts.iloc[[0]], acts.iloc[[1]]
    p = _adjusted_copy(bars, second)
    s = _adjusted_copy(bars, first)
    r = _ratio_run(p, acts, s)
    assert r.counts["adjusted_series"] == 0 and r.counts["price_mismatch"] > 0


def test_reports_are_written_on_a_filesystem_without_hard_links(mkt, tmp_path, monkeypatch):
    import errno
    import os

    from quantlab.data.store import DatasetStore

    store = DatasetStore(tmp_path)
    dv = store.write("demo", mkt.bars, mkt.actions, mkt.provenance)

    def no_link(*a, **k):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_link)
    store.write_report(dv, "reconciliation-x", {"status": "pass"})
    assert store.load_reports(dv)["reconciliation-x"] == {"status": "pass"}
    with pytest.raises(FileExistsError):
        store.write_report(dv, "reconciliation-x", {"status": "fail"})
    assert store.load_reports(dv)["reconciliation-x"] == {"status": "pass"}
    leftovers = [x for x in dv.path.parent.parent.rglob(".tmp-*")]
    assert leftovers == []


def test_id_map_onto_an_id_used_only_by_actions_is_rejected():
    from tests.conftest import make_bars

    bars = make_bars({"A": [10.0] * 5})
    d = sorted(bars["date"].unique())
    hist = pd.DataFrame(
        {"instrument_id": ["P1"], "symbol": ["A"], "valid_from": [d[0]], "valid_to": [None]}
    )
    s_hist = pd.DataFrame(
        {
            "instrument_id": ["V1", "V2"],
            "symbol": ["A", "B"],
            "valid_from": [d[0], d[0]],
            "valid_to": [None, None],
        }
    )
    # V2 has an action but no bars; mapping V1 onto V2 would merge two instruments.
    s_acts = normalize_actions(
        pd.DataFrame([{"symbol": "B", "ex_date": d[2], "kind": "dividend", "value": 0.1}])
    )
    p = Source("p", bars, normalize_actions(None), hist)
    s = Source("s", bars, s_acts, s_hist)
    with pytest.raises(ValueError, match="already used"):
        reconcile(p, s, id_map={"V1": "V2"})


def test_pairing_prefers_the_agreeing_amount_over_the_same_date():
    from tests.conftest import make_bars

    bars = make_bars({"A": [100.0] * 10})
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))

    def rows(pairs):
        return normalize_actions(
            pd.DataFrame(
                [{"symbol": "A", "ex_date": d[k], "kind": "dividend", "value": v} for k, v in pairs]
            )
        )

    r = reconcile(
        Source("p", bars, rows([(5, 0.50)])), Source("s", bars, rows([(5, 0.52), (7, 0.50)]))
    )
    assert [x["issue"] for x in r.samples["action_mismatch"]] == ["ex_date differs"]
    assert r.counts["action_missing"] == 1
    assert r.samples["action_missing"][0]["value_s"] == 0.52


# ------------------------------------- adversarial review of the ratio-step rule
def test_a_zero_close_is_never_on_level():
    bars, acts = _raw_series()
    p = bars.copy()
    p.loc[p.index[[5, 25, 45]], "close"] = 0.0
    r = _ratio_run(p, acts, _adjusted_copy(bars, acts))
    assert r.counts["adjusted_series"] == 1
    assert r.counts["price_mismatch"] == 3  # was 0: inf <= inf put them on level


@pytest.mark.parametrize(
    ("true", "applied"),
    [(10.0, 9.0), (0.1, 1 / 9), (2.0, 2.0 / 1.03), (50.0, 50.0 / 1.2)],
)
def test_a_split_adjusted_by_the_wrong_factor_is_not_explained(true, applied):
    bars, acts = _raw_series(events=((20, "split", true),))
    wrong = acts.assign(value=applied)
    for p, s in ((bars, _adjusted_copy(bars, wrong)), (_adjusted_copy(bars, wrong), bars)):
        r = _ratio_run(p, acts, s)
        assert r.counts["adjusted_series"] == 0 and r.counts["price_mismatch"] == 20


def test_a_split_applied_the_wrong_way_is_not_explained():
    # The secondary multiplies pre-split prices by 2 instead of dividing: the ratio is
    # [2, 1], which a primary-adjusted series would also give, but the "raw" secondary
    # would then fall 75% on a 2:1 split.
    bars, acts = _raw_series(events=((20, "split", 2.0),))
    s = bars.copy()
    s.loc[s.index[:20], "close"] *= 2.0
    r = _ratio_run(bars, acts, s)
    assert r.counts["adjusted_series"] == 0 and r.counts["price_mismatch"] == 20


@pytest.mark.parametrize(
    "events",
    [
        ((20, "dividend", 1.0), (40, "split", 10.0)),  # the primary's adjusted c_prev is tiny
        ((20, "dividend", 60.0),),  # a 60% special dividend
        ((20, "dividend", 10.0), (20, "dividend", 9.0)),  # two on one day
        ((58, "dividend", 1.0),),  # an ex-date in the last, too-short segment
        ((20, "dividend", 1.0), (59, "dividend", 1.0)),
    ],
)
@pytest.mark.parametrize("adjusted_side", ["secondary", "primary"])
def test_genuine_adjusted_series_the_first_rule_missed(events, adjusted_side):
    bars, acts = _raw_series(events=events)
    adj = _adjusted_copy(bars, acts)
    p, s = (bars, adj) if adjusted_side == "secondary" else (adj, bars)
    r = _ratio_run(p, acts, s)
    assert r.counts["adjusted_series"] == 1, r.counts
    assert r.counts["scale_mismatch"] == 0
