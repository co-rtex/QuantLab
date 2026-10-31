"""The exchange calendar against NYSE's published 2024 holidays (golden)."""

from datetime import date

from quantlab.data.calendar import us_equity_holidays


def test_nyse_2024_holidays_golden():
    expected = {
        date(2024, 1, 1),
        date(2024, 1, 15),
        date(2024, 2, 19),
        date(2024, 3, 29),
        date(2024, 5, 27),
        date(2024, 6, 19),
        date(2024, 7, 4),
        date(2024, 9, 2),
        date(2024, 11, 28),
        date(2024, 12, 25),
    }
    assert us_equity_holidays(2024) == expected
