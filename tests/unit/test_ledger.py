import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quantlab.portfolio.ledger import (
    InsufficientCash,
    InsufficientPosition,
    InvalidOrder,
    Ledger,
    replay,
)

D = pd.Timestamp("2024-01-02")


def test_basic_accounting_and_fees_reduce_wealth():
    led = Ledger()
    led.deposit(D, 10_000)
    led.trade(D, "A", 10, 100.0, commission=5.0)
    assert led.cash == pytest.approx(10_000 - 1000 - 5)
    assert led.equity({"A": 100.0}) == pytest.approx(9_995)  # fee is a pure wealth loss
    led.trade(D, "A", -4, 110.0, commission=1.0)
    assert led.quantity("A") == 6
    assert led.equity({"A": 110.0}) == pytest.approx(10_000 - 6 + 10 * 10)
    led.check_invariants()


def test_dividend_and_split_preserve_value():
    led = Ledger()
    led.deposit(D, 1_000)
    led.trade(D, "A", 10, 50.0)
    before = led.equity({"A": 50.0})
    led.dividend(D, "A", 2.0)  # ex-dividend: price drops by 2
    assert led.equity({"A": 48.0}) == pytest.approx(before)
    led.split(D, "A", 2.0)  # 2-for-1: price halves
    assert led.quantity("A") == 20
    assert led.equity({"A": 24.0}) == pytest.approx(before)
    led.check_invariants()


def test_rejections():
    led = Ledger()
    led.deposit(D, 100)
    with pytest.raises(InsufficientCash):
        led.trade(D, "A", 2, 60.0)
    with pytest.raises(InsufficientPosition):
        led.trade(D, "A", -1, 60.0)
    with pytest.raises(InvalidOrder):
        led.trade(D, "A", 1, 0.0)
    with pytest.raises(InvalidOrder):
        led.trade(D, "A", 0, 10.0)
    with pytest.raises(InvalidOrder):
        led.trade(D, "A", 1, float("nan"))
    assert led.cash == 100 and not led.positions  # failed orders leave no trace
    assert len(led.transactions) == 1


op = st.one_of(
    st.tuples(
        st.just("trade"),
        st.sampled_from(["A", "B"]),
        st.floats(-50, 50, allow_nan=False).filter(lambda x: abs(x) > 1e-3),
        st.floats(1, 500),
        st.floats(0, 10),
    ),
    st.tuples(st.just("div"), st.sampled_from(["A", "B"]), st.floats(0.01, 5)),
    st.tuples(st.just("split"), st.sampled_from(["A", "B"]), st.sampled_from([0.5, 2.0, 3.0])),
)


@settings(max_examples=200, deadline=None)
@given(st.floats(100, 1e6), st.lists(op, max_size=40))
def test_property_replay_and_no_negative_balances(initial, ops):
    led = Ledger()
    led.deposit(D, initial)
    for o in ops:
        try:
            if o[0] == "trade":
                led.trade(D, o[1], o[2], o[3], commission=o[4])
            elif o[0] == "div":
                led.dividend(D, o[1], o[2])
            else:
                led.split(D, o[1], o[2])
        except (InsufficientCash, InsufficientPosition):
            pass
    assert led.cash >= -1e-6
    assert all(q >= 0 for q in led.positions.values())
    led.check_invariants()
    r = replay(led.transactions)
    assert r.cash == pytest.approx(led.cash)
    assert sum(t.cash_delta for t in led.transactions) == pytest.approx(led.cash)
