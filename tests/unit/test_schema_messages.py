"""A date that does not parse is reported in one line that names the value (#239).

pandas follows the failing value with several lines of advice about its own `format`
argument, which reached users of the CSV import verbatim.
"""

from __future__ import annotations

import pandas as pd
import pytest

from quantlab.data.schema import SchemaError, normalize_actions, normalize_bars

BARS = {
    "symbol": ["ABC", "ABC"],
    "date": ["2024-01-02", "not-a-date"],
    "open": [1.0, 1.0],
    "high": [1.0, 1.0],
    "low": [1.0, 1.0],
    "close": [1.0, 1.0],
    "volume": [1.0, 1.0],
}


def test_a_bar_date_that_does_not_parse_is_one_line_naming_the_value():
    with pytest.raises(SchemaError) as exc:
        normalize_bars(pd.DataFrame(BARS))
    message = str(exc.value)
    assert message.startswith("bar date is not a date: ")
    assert '"not-a-date"' in message
    assert "\n" not in message and "You might want" not in message


def test_an_action_date_that_does_not_parse_is_one_line_naming_the_value():
    actions = pd.DataFrame(
        {
            "symbol": ["ABC", "ABC"],
            "ex_date": ["2024-01-02", "soon"],
            "kind": ["split", "split"],
            "value": [2.0, 2.0],
        }
    )
    with pytest.raises(SchemaError) as exc:
        normalize_actions(actions)
    message = str(exc.value)
    assert message.startswith("corporate action ex_date is not a date: ")
    assert '"soon"' in message
    assert "\n" not in message and "You might want" not in message


def test_a_date_pandas_rejects_without_advice_keeps_its_reason():
    bars = {**BARS, "date": ["2024-01-02", "2024-13-45"]}
    with pytest.raises(SchemaError, match=r"^bar date is not a date: .*2024-13-45"):
        normalize_bars(pd.DataFrame(bars))
