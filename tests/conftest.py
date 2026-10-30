from __future__ import annotations

import pandas as pd
import pytest

from quantlab.data.synthetic import SyntheticMarket, SyntheticSpec, generate_market


@pytest.fixture(scope="session")
def market() -> SyntheticMarket:
    return generate_market(
        SyntheticSpec(
            n_assets=30, start="2019-01-02", end="2022-12-30", seed=11, split_probability=0.002
        )
    )


def make_bars(
    prices: dict[str, list[float]],
    start: str = "2024-01-02",
    opens: dict[str, list[float]] | None = None,
) -> pd.DataFrame:
    """Tiny hand-written bar frames for golden tests (open == close unless given)."""
    from quantlab.data.calendar import trading_days
    from quantlab.data.schema import normalize_bars

    n = max(len(v) for v in prices.values())
    dates = trading_days(start, pd.Timestamp(start) + pd.Timedelta(days=3 * n + 10))[:n]
    rows = []
    for sym, closes in prices.items():
        op = (opens or {}).get(sym, closes)
        for d, c, o in zip(dates, closes, op, strict=False):
            if c is None:
                continue
            rows.append(
                {
                    "symbol": sym,
                    "date": d,
                    "open": o,
                    "high": max(o, c),
                    "low": min(o, c),
                    "close": c,
                    "volume": 1e6,
                }
            )
    return normalize_bars(pd.DataFrame(rows))
