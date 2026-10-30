from datetime import date

from quantlab.data.calendar import is_trading_day, trading_days, us_equity_holidays


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


def test_session_counts_match_published_nyse_totals():
    assert len(trading_days("2021-01-01", "2021-12-31")) == 252
    assert len(trading_days("2022-01-01", "2022-12-31")) == 251
    assert len(trading_days("2023-01-01", "2023-12-31")) == 250
    assert len(trading_days("2024-01-01", "2024-12-31")) == 252


def test_weekend_observance_rules():
    # 2022-01-01 was a Saturday: NYSE did NOT close on Friday 2021-12-31.
    assert is_trading_day("2021-12-31")
    # 2021-07-04 Sunday -> observed Monday 2021-07-05.
    assert not is_trading_day("2021-07-05")
    # Juneteenth only from 2022.
    assert is_trading_day("2021-06-18")
    assert not is_trading_day("2023-06-19")


def test_empty_range():
    assert len(trading_days("2024-01-10", "2024-01-01")) == 0
