import pandas as pd
import pytest

from quantlab.data.schema import normalize_actions
from quantlab.portfolio.ledger import LedgerError
from quantlab.portfolio.valuation import Entry, time_weighted_returns, value_portfolio
from tests.conftest import make_bars


def test_valuation_through_split_and_dividend_conserves_value():
    bars = make_bars({"A": [100.0, 100.0, 49.0, 49.0]})
    d = list(bars["date"].dt.date.unique())
    acts = normalize_actions(
        pd.DataFrame(
            [
                {"symbol": "A", "ex_date": d[2], "kind": "dividend", "value": 2.0},
                {"symbol": "A", "ex_date": d[2], "kind": "split", "value": 2.0},
            ]
        )
    )
    v = value_portfolio(
        [Entry(1, d[0], "deposit", amount=1000), Entry(2, d[0], "buy", "A", 10)], bars, acts
    )
    eq = v.equity["equity"]
    # 10 sh @100 -> dividend 20 cash, split to 20 sh @49 -> 980 + 20 = 1000
    assert eq.iloc[2] == pytest.approx(1000.0)
    assert v.holdings.iloc[0]["quantity"] == 20


def test_time_weighted_return_ignores_deposits():
    bars = make_bars({"A": [10.0, 11.0, 11.0]})
    d = list(bars["date"].dt.date.unique())
    v = value_portfolio(
        [
            Entry(1, d[0], "deposit", amount=100),
            Entry(2, d[0], "buy", "A", 10),
            Entry(3, d[2], "deposit", amount=500),
        ],
        bars,
        normalize_actions(None),
    )
    r = time_weighted_returns(v.equity)
    assert r.iloc[0] == pytest.approx(0.10)
    assert r.iloc[1] == pytest.approx(0.0)  # the 500 deposit is not a return


def test_rejections_name_the_entry():
    bars = make_bars({"A": [10.0, 10.0]})
    d = list(bars["date"].dt.date.unique())
    with pytest.raises(LedgerError, match="transaction #2"):
        value_portfolio(
            [Entry(1, d[0], "deposit", amount=10), Entry(2, d[1], "buy", "A", 5)],
            bars,
            normalize_actions(None),
        )
