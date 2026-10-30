"""Rule-based US equity trading calendar (NYSE-style regular sessions).

This calendar reproduces the standard NYSE full-day holiday rules. It does not model
early closes or one-off closures (e.g. national days of mourning, 2012 Hurricane Sandy).
Those omissions are documented limitations; real-data ingestion should prefer the
provider's own session list when one is available.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import pandas as pd


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=256)
def us_equity_holidays(year: int) -> frozenset[date]:
    days: set[date] = set()
    new_year = date(year, 1, 1)
    # NYSE does not observe New Year's Day on the preceding Friday (Rule 7.2).
    if new_year.weekday() == 6:
        days.add(new_year + timedelta(days=1))
    elif new_year.weekday() != 5:
        days.add(new_year)
    days.add(_nth_weekday(year, 1, 0, 3))  # Martin Luther King Jr. Day
    days.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday
    days.add(_easter(year) - timedelta(days=2))  # Good Friday
    days.add(_last_weekday(year, 5, 0))  # Memorial Day
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    days.add(_observed(date(year, 7, 4)))
    days.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    days.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    days.add(_observed(date(year, 12, 25)))
    return frozenset(days)


def trading_days(start: date | str, end: date | str) -> pd.DatetimeIndex:
    """Regular trading sessions in [start, end], inclusive."""
    s, e = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if e < s:
        return pd.DatetimeIndex([])
    bdays = pd.bdate_range(s, e)
    holidays: set[date] = set()
    for y in range(s.year, e.year + 1):
        holidays |= us_equity_holidays(y)
    mask = [d.date() not in holidays for d in bdays]
    return pd.DatetimeIndex(bdays[mask])


def is_trading_day(d: date | str) -> bool:
    ts = pd.Timestamp(d)
    return ts.weekday() < 5 and ts.date() not in us_equity_holidays(ts.year)
