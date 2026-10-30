import dataclasses

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quantlab.portfolio.ledger import (
    InsufficientCash,
    InsufficientPosition,
    InvalidOrder,
    Ledger,
    LedgerError,
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


def _tampered(index: int, **changes: float) -> Ledger:
    """A ledger whose log has one transaction edited and whose live state matches the log,
    so only the economics of that transaction are wrong."""
    led = Ledger()
    led.deposit(D, 1000)
    led.trade(D, "A", 5, 100.0, commission=1.0)
    led.dividend(D, "A", 2.0)
    led.split(D, "A", 2.0)
    led.transactions[index] = dataclasses.replace(led.transactions[index], **changes)
    r = replay(led.transactions, verify=False)
    led.cash, led.positions = r.cash, dict(r.positions)
    return led


@pytest.mark.parametrize(
    ("index", "changes"),
    [
        (1, {"cash_delta": -400.0}),  # a buy of 5 @ 100 + 1 commission must cost 501
        (1, {"price": 90.0}),  # the fill price no longer explains the cash moved
        (1, {"commission": -1.0, "cash_delta": -499.0}),  # negative fee disguised as income
        (2, {"quantity": 7.0, "cash_delta": 14.0}),  # dividend on shares that were not held
        (3, {"quantity": 1.0}),  # the split did not double the 5 shares held
        (3, {"cash_delta": 5.0}),  # a split never moves cash
    ],
)
def test_invariants_detect_transactions_whose_cash_is_not_explained(index, changes):
    with pytest.raises(LedgerError):
        _tampered(index, **changes).check_invariants()


def test_invariants_reject_a_log_that_overdraws_cash_midway():
    led = Ledger()
    led.deposit(D, 100)
    led.allow_negative_cash = True
    led.trade(D, "A", 2, 100.0)  # cash is -100 at this point
    led.trade(D, "A", -2, 100.0)  # and back to 100 at the end
    led.allow_negative_cash = False
    with pytest.raises(LedgerError, match="negative cash"):
        led.check_invariants()
