"""load_bars with symbols reads only the row groups that can hold them, one at a time (#225).

The bar is equivalence: every frame equals what the previous implementation (a filtered
``pd.read_parquet``, kept below as ``reference``) returns, dtypes, index and column order
included. A file whose groups lack usable symbol statistics takes that full read.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
import pytest

import quantlab.data.store as store_module
from quantlab.data.store import DatasetStore, DatasetVersion

SYMBOLS = [f"S{i:02d}" for i in range(12)]
SESSIONS = pd.bdate_range("2024-01-02", periods=9)


def reference(
    dv: DatasetVersion,
    symbols: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """load_bars before #225, verbatim."""
    filters: list[tuple[str, str, Any]] = []
    if symbols:
        filters.append(("symbol", "in", [s.upper() for s in symbols]))
    if start:
        filters.append(("date", ">=", pd.Timestamp(start)))
    if end:
        filters.append(("date", "<=", pd.Timestamp(end)))
    df = pd.read_parquet(dv.path / "bars.parquet", filters=filters or None)
    df["symbol"] = df["symbol"].astype(str)
    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


def bars(symbols: list[str], sessions: pd.DatetimeIndex) -> pd.DataFrame:
    rows = [
        {
            "symbol": s,
            "date": d,
            "open": 10.0 + i + j / 8,
            "high": 11.0 + i + j / 8,
            "low": 9.0 + i + j / 8,
            "close": 10.5 + i + j / 8,
            "volume": 1000.0 + 7 * j,
        }
        for i, s in enumerate(symbols)
        for j, d in enumerate(sessions)
    ]
    return pd.DataFrame(rows)


def write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows_per_group: int, frame: pd.DataFrame
) -> DatasetVersion:
    """A real version, written by DatasetStore.write with small row groups."""
    monkeypatch.setattr(store_module, "ROW_GROUP_ROWS", rows_per_group)
    actions = pd.DataFrame(columns=["symbol", "ex_date", "kind", "value"])
    return DatasetStore(tmp_path).write("probe", frame, actions, {"source": "test"})


@pytest.fixture
def dv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DatasetVersion:
    # 9 sessions per symbol in 7-row groups: every symbol spans a group boundary.
    version = write(tmp_path, monkeypatch, 7, bars(SYMBOLS, SESSIONS))
    assert pq.ParquetFile(version.path / "bars.parquet").metadata.num_row_groups == 16
    return version


D = [str(d.date()) for d in SESSIONS]
CASES: list[tuple[list[str], str | None, str | None]] = [
    (["S01", "S02", "S03"], None, None),  # adjacent
    (["S00", "S05", "S11"], None, None),  # spread, the first and last groups
    (["S04"], None, None),  # one symbol across a boundary
    (SYMBOLS, None, None),  # every symbol
    (["ZZZ"], None, None),  # absent, after the last group's max
    (["A00"], None, None),  # absent, before the first group's min
    (["S04X"], None, None),  # absent, between two symbols inside one group
    (["ZZZ", "S07"], None, None),  # absent and present
    (["s03", "s09"], None, None),  # lowercase
    (["S03", "S03", "s03"], None, None),  # duplicates
    (["S02", "S08"], D[2], D[5]),  # bounds on existing sessions: inclusive
    (["S02", "S08"], D[4], D[4]),  # start == end
    (["S02"], D[0], D[-1]),  # the data's own first and last sessions
    (["S02"], None, "2023-12-29"),  # end before the data
    (["S02"], "2024-02-01", None),  # start after the data
    (["S06"], "2024-01-06", "2024-01-09"),  # bounds on a weekend and a session
]


@pytest.mark.parametrize(("symbols", "start", "end"), CASES)
def test_the_frame_equals_the_previous_read(
    dv: DatasetVersion, symbols: list[str], start: str | None, end: str | None
) -> None:
    got = DatasetStore(dv.path.parents[1]).load_bars(dv, symbols, start, end)
    pd.testing.assert_frame_equal(got, reference(dv, symbols, start, end), check_dtype=True)
    assert list(got.columns) == list(reference(dv).columns)
    assert isinstance(got.index, pd.RangeIndex)


def test_an_empty_result_keeps_the_columns_and_dtypes(dv: DatasetVersion) -> None:
    got = DatasetStore(dv.path.parents[1]).load_bars(dv, ["ZZZ"])
    assert got.empty
    assert got.dtypes.to_dict() == reference(dv).dtypes.to_dict()
    assert str(got["date"].dtype) == "datetime64[ns]"
    assert str(got["available_at"].dtype) == "datetime64[ns, UTC]"


@pytest.mark.parametrize("rows_per_group", [1, 9, 10_000])
def test_any_group_size_gives_the_same_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows_per_group: int
) -> None:
    dv = write(tmp_path, monkeypatch, rows_per_group, bars(SYMBOLS, SESSIONS))
    for symbols, start, end in CASES:
        got = DatasetStore(tmp_path).load_bars(dv, symbols, start, end)
        pd.testing.assert_frame_equal(got, reference(dv, symbols, start, end))


def rewrite(dv: DatasetVersion, **options: Any) -> None:
    """Rewrite the version's bars.parquet in place with other Parquet writer options."""
    path = dv.path / "bars.parquet"
    table = pq.read_table(path)
    pq.write_table(table, path, row_group_size=7, **options)


@pytest.mark.parametrize(
    "options",
    [
        {"write_statistics": False},  # no statistics at all
        {"write_statistics": ["date", "close"]},  # none for symbol
    ],
    ids=["no-statistics", "no-symbol-statistics"],
)
def test_groups_without_symbol_statistics_take_the_full_read(
    dv: DatasetVersion, monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]
) -> None:
    rewrite(dv, **options)
    want = reference(dv, ["S03", "S09"], D[1], D[6])
    calls: list[Path] = []
    real = pd.read_parquet

    def spy(path: Path, **kwargs: Any) -> pd.DataFrame:
        calls.append(path)
        return real(path, **kwargs)

    monkeypatch.setattr(store_module.pd, "read_parquet", spy)
    got = DatasetStore(dv.path.parents[1]).load_bars(dv, ["S03", "S09"], D[1], D[6])
    pd.testing.assert_frame_equal(got, want)
    assert calls == [dv.path / "bars.parquet"]


def test_with_symbol_statistics_the_full_read_is_never_taken(
    dv: DatasetVersion, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> pd.DataFrame:
        raise AssertionError("the full filtered read was taken")

    monkeypatch.setattr(store_module.pd, "read_parquet", refuse)
    store = DatasetStore(dv.path.parents[1])
    for symbols, start, end in CASES:
        store.load_bars(dv, symbols, start, end)


def test_a_dictionary_encoded_symbol_column_gives_the_same_frame(dv: DatasetVersion) -> None:
    path = dv.path / "bars.parquet"
    frame = pd.read_parquet(path)
    frame["symbol"] = frame["symbol"].astype("category")
    frame.to_parquet(path, index=False, row_group_size=7)
    for symbols, start, end in CASES:
        got = DatasetStore(dv.path.parents[1]).load_bars(dv, symbols, start, end)
        pd.testing.assert_frame_equal(got, reference(dv, symbols, start, end))


def test_without_symbols_the_read_is_unchanged(dv: DatasetVersion) -> None:
    store = DatasetStore(dv.path.parents[1])
    for start, end in [(None, None), (D[2], None), (None, D[5]), (D[3], D[3])]:
        pd.testing.assert_frame_equal(
            store.load_bars(dv, None, start, end), reference(dv, None, start, end)
        )


PEAK = textwrap.dedent(
    """
    import json, sys
    import pyarrow as pa, pyarrow.parquet as pq
    from quantlab.data.store import DatasetStore
    root, slug, version, symbols = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
    store = DatasetStore(root)
    dv = store.open(slug, version)
    pool = pa.default_memory_pool()
    before = pool.max_memory()
    rows = len(store.load_bars(dv, symbols))
    peak = pool.max_memory() - before
    group = pq.ParquetFile(dv.path / "bars.parquet").read_row_group(0).nbytes
    print(json.dumps({"peak": peak, "group": group, "rows": rows}))
    """
)


def test_the_arrow_peak_is_bounded_by_a_few_row_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 40 groups of 10 symbols; one wanted symbol per group, so every group is read and
    # the result is a tenth of the file. A whole-file decode would be ~40 groups.
    symbols = [f"T{i:03d}" for i in range(400)]
    dv = write(
        tmp_path, monkeypatch, 5_000, bars(symbols, pd.bdate_range("2020-01-01", periods=500))
    )
    assert pq.ParquetFile(dv.path / "bars.parquet").metadata.num_row_groups == 40
    wanted = symbols[::10]
    out = subprocess.run(
        [sys.executable, "-c", PEAK, str(tmp_path), dv.slug, dv.version, *wanted],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    got = json.loads(out)
    assert got["rows"] == 40 * 500
    # Measured: ~8 groups' worth (the result is ~4), against ~41 for the previous read's
    # whole-file decode. Twenty is generous either way.
    assert got["peak"] <= 20 * got["group"], got
