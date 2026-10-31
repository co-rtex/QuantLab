from quantlab.data.calendar import is_trading_day, trading_days


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
