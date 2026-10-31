"""Split and dividend adjustment against total returns computed by hand (golden)."""

import numpy as np
import pandas as pd

from quantlab.data.adjust import adjusted_close, total_returns
from quantlab.data.schema import normalize_actions
from tests.conftest import make_bars


def test_split_and_dividend_adjustment_golden():
    bars = make_bars({"A": [100.0, 102.0, 51.0, 50.0, 48.0]})
    d = bars["date"].unique()
    actions = normalize_actions(
        pd.DataFrame(
            [
                {"symbol": "A", "ex_date": d[2], "kind": "split", "value": 2.0},
                {"symbol": "A", "ex_date": d[4], "kind": "dividend", "value": 1.0},
            ]
        )
    )
    tr = total_returns(bars, actions)["A"].to_numpy()
    assert np.isnan(tr[0])
    np.testing.assert_allclose(tr[1:], [0.02, 2 * 51 / 102 - 1, 50 / 51 - 1, (48 + 1) / 50 - 1])
    adj = adjusted_close(bars, actions)["A"]
    np.testing.assert_allclose(adj.pct_change().to_numpy()[1:], tr[1:], rtol=1e-12)
    assert adj.iloc[-1] == 48.0  # last price is never adjusted
