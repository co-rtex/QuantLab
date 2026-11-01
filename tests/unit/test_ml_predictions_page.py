"""ML prediction pages are streamed, not read whole into the API (#231).

The bar is equivalence with the previous read (kept below as ``reference``): the same
records and total for every model, offset and limit, across batch and row-group bounds.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from quantlab_api.routes import ml
from quantlab_api.routes.ml import predictions_page

MODELS = ["logistic", "random_forest", "momentum_rule"]


def reference(path: Path, model: str, offset: int, limit: int) -> tuple[pd.DataFrame, int]:
    """The route before #231: the whole file, then the model, then the page."""
    p = pd.read_parquet(path)
    p = p[p["model"] == model]
    return p.iloc[offset : offset + limit].copy(), len(p)


def predictions(folds: int = 4, symbols: int = 9, dates: int = 7) -> pd.DataFrame:
    """As the pipeline writes them: per fold, every model's rows in turn."""
    rng = np.random.default_rng(3)
    parts = []
    for fold in range(1, folds + 1):
        day = pd.bdate_range("2021-01-04", periods=dates) + pd.offsets.BDay(dates * fold)
        test = pd.DataFrame(
            {
                "date": np.repeat(day, symbols),
                "symbol": [f"S{i:02d}" for i in range(symbols)] * dates,
                "instrument_id": [f"ID{i:02d}" for i in range(symbols)] * dates,
                "fwd_return": rng.normal(0, 0.02, symbols * dates),
                "label": rng.integers(0, 2, symbols * dates),
            }
        )
        for m in MODELS[: 2 + fold % 2]:  # a model missing from some folds
            parts.append(test.assign(model=m, fold=fold, prob=rng.random(len(test))))
    return pd.concat(parts, ignore_index=True)


@pytest.fixture(params=[None, 10], ids=["one-row-group", "row-groups-of-10"])
def path(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    out = tmp_path / "predictions.parquet"
    predictions().to_parquet(out, index=False, row_group_size=request.param)
    return out


@pytest.mark.parametrize("batch", [7, 63, 65_536])
def test_every_page_equals_the_previous_read(
    path: Path, batch: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ml, "PREDICTION_BATCH_ROWS", batch)
    for model in [*MODELS, "nope"]:
        want_total = reference(path, model, 0, 1)[1]
        for offset in sorted({0, 1, 6, 7, 62, 63, 64, want_total - 1, want_total, want_total + 5}):
            for limit in (1, 5, 63, 200, 5000):
                if offset < 0:
                    continue
                want, total = reference(path, model, offset, limit)
                got, got_total = predictions_page(path, model, offset, limit)
                assert got_total == total, (model, offset, limit)
                pd.testing.assert_frame_equal(
                    got.reset_index(drop=True), want.reset_index(drop=True), check_dtype=True
                )
                assert got.to_dict("records") == want.to_dict("records")


PEAK = textwrap.dedent(
    """
    import json, sys
    import pandas as pd
    import pyarrow as pa
    from quantlab_api.routes.ml import predictions_page
    path, variant = sys.argv[1], sys.argv[2]
    pool = pa.default_memory_pool()
    before = pool.max_memory()
    if variant == "previous":
        p = pd.read_parquet(path)
        p = p[p["model"] == "logistic"]
        rows = len(p.iloc[300_000 : 300_200])
    else:
        rows = len(predictions_page(path, "logistic", 300_000, 200)[0])
    print(json.dumps({"peak": pool.max_memory() - before, "rows": rows}))
    """
)


def peak(path: Path, variant: str) -> dict[str, int]:
    out = subprocess.run(
        [sys.executable, "-c", PEAK, str(path), variant], capture_output=True, text=True, check=True
    ).stdout
    return dict(json.loads(out))


def test_a_page_costs_a_fraction_of_the_previous_read(tmp_path: Path) -> None:
    # Pyarrow decodes a row group's column chunks whatever the batch size, so the stream
    # holds about one row group, of up to ~1M rows by default; the gain shows once a file
    # has several. Here 1.5M rows in 2 row groups (3.6M rows measured: 436 -> 75 MiB).
    path = tmp_path / "predictions.parquet"
    predictions(folds=200, symbols=250, dates=12).to_parquet(path, index=False)
    assert pq.ParquetFile(path).metadata.num_row_groups >= 2
    before, after = peak(path, "previous"), peak(path, "stream")
    assert before["rows"] == after["rows"] == 200
    assert after["peak"] <= before["peak"] / 2, (before, after)
