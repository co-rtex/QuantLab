"""The exact pro-rata factor (#195): feasible, maximal, and bounded in its search.

``buy_scale`` returns the largest s <= 1 whose scaled buys fit the cash, commission
included. Random cases check both properties to the last float step, including the
one-segment case of a minimum commission with no rate (#209 review N2: its closed form
overshot the cash by up to two units in the last place before the guard applied there).
"""

from __future__ import annotations

import math

import numpy as np

from quantlab.backtest.engine import buy_scale


def _cost(s: float, values: list[float], bps: float, cmin: float) -> float:
    return sum(s * v + max(cmin, s * v * bps) for v in values)


def _cases(n_cases: int, seed: int, rate: bool):
    rng = np.random.default_rng(seed)
    for _ in range(n_cases):
        values = [float(v) for v in 10 ** rng.uniform(1, 9, int(rng.integers(1, 9)))]
        bps = float(rng.choice([1e-4, 1e-3, 2.5e-3])) if rate else 0.0
        cmin = float(rng.choice([0.0, 1.0, 5.0])) if rate else float(rng.choice([1.0, 5.0]))
        full = _cost(1.0, values, bps, cmin)
        cash = float(full * rng.uniform(0.01, 0.999))
        yield values, bps, cmin, cash


def _check(values, bps, cmin, cash) -> None:
    s = buy_scale(values, bps, cmin, cash)
    if cmin > 0 and len(values) * cmin >= cash:
        assert s is None
        return
    assert s is not None and 0 < s <= 1
    assert _cost(s, values, bps, cmin) <= cash, (values, bps, cmin, cash, s)


def test_the_factor_fits_the_cash_with_a_rate():
    for case in _cases(3000, 195, rate=True):
        _check(*case)


def test_the_factor_fits_the_cash_with_only_a_minimum_commission():
    # The one-segment branch: bps = 0, cmin > 0.
    for case in _cases(3000, 209, rate=False):
        _check(*case)


def test_the_factor_is_maximal_to_a_few_units_in_the_last_place():
    # Largest feasible: a few float steps above the answer, the buys no longer fit.
    for values, bps, cmin, cash in _cases(500, 7, rate=True):
        s = buy_scale(values, bps, cmin, cash)
        if s is None or s == 1.0:
            continue
        up = s
        for _ in range(4):
            up = math.nextafter(up, 2.0)
        assert _cost(up, values, bps, cmin) > cash or up >= 1.0, (values, bps, cmin, cash, s)


def test_none_when_the_minimum_commissions_take_all_the_cash():
    assert buy_scale([100.0, 200.0], 0.001, 5.0, 10.0) is None
    assert buy_scale([100.0, 200.0], 0.001, 5.0, 10.000001) is not None
