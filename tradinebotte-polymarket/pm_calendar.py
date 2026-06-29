"""
Polymarket plugin — trading-calendar filter (Plan D step 4b).

US-market trading-hours / holiday logic for the Polymarket BTC Up/Down strategy,
extracted from the universal entrypoint (live_bot.py) into the plugin. `config` is
duck-typed (the entrypoint's BotConfig) — this module imports nothing from the
entrypoint, so live_bot re-exports these for its existing callers. Shipped flat beside
live_bot.py.
"""

import functools
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional


@functools.lru_cache(maxsize=4)
def _us_holidays(year: int) -> frozenset:
    """Return the frozenset of US federal holiday *observed* dates for `year`.

    Covers the 10 NYSE-recognised holidays.  Saturday holidays shift to Friday;
    Sunday holidays shift to Monday.  Results are cached per year (lru_cache).
    """
    def _observed(d: date) -> date:
        if d.weekday() == 5: return d - timedelta(days=1)   # Sat → Fri
        if d.weekday() == 6: return d + timedelta(days=1)   # Sun → Mon
        return d

    def _nth_weekday(y: int, m: int, wd: int, n: int) -> date:
        first = date(y, m, 1)
        delta = (wd - first.weekday()) % 7
        return first.replace(day=1 + delta + (n - 1) * 7)

    def _last_monday(y: int, m: int) -> date:
        for day in range(31, 21, -1):
            try:
                d = date(y, m, day)
                if d.weekday() == 0:
                    return d
            except ValueError:
                continue
        raise ValueError  # pragma: no cover

    def _easter(y: int) -> date:
        a, b, c = y % 19, y // 100, y % 100
        d, e, f = b // 4, b % 4, (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = c // 4, c % 4
        ll = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * ll) // 451
        mo = (h + ll - 7 * m + 114) // 31
        dy = (h + ll - 7 * m + 114) % 31 + 1
        return date(y, mo, dy)

    mon, thu = 0, 3
    return frozenset([
        _observed(date(year, 1,  1)),               # New Year's Day
        _nth_weekday(year, 1, mon, 3),              # MLK Day (3rd Mon Jan)
        _nth_weekday(year, 2, mon, 3),              # Presidents' Day (3rd Mon Feb)
        _easter(year) - timedelta(days=2),          # Good Friday
        _last_monday(year, 5),                      # Memorial Day (last Mon May)
        _observed(date(year, 6, 19)),               # Juneteenth
        _observed(date(year, 7,  4)),               # Independence Day
        _nth_weekday(year, 9, mon, 1),              # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, thu, 4),             # Thanksgiving (4th Thu Nov)
        _observed(date(year, 12, 25)),              # Christmas Day
    ])


def _is_us_holiday(dt: datetime) -> bool:
    """Return True if `dt` (UTC) falls on a US federal holiday (observed date)."""
    return dt.date() in _us_holidays(dt.year)


def _in_weekend_session(ts_ms: Optional[int] = None) -> bool:
    """Return True if timestamp falls in the weekend session (Fri 20:00 → Mon 13:30 UTC)."""
    dt     = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms is not None \
             else datetime.now(timezone.utc)
    dow    = dt.weekday()
    hour   = dt.hour
    minute = dt.minute
    if dow in (5, 6):                                  # Sat / Sun: always weekend
        return True
    if dow == 4 and hour >= 20:                       # Fri from US weekly close
        return True
    if dow == 0 and (hour < 13 or (hour == 13 and minute < 30)):  # Mon before US open
        return True
    return False


def is_trading_hour(config: Any, ts_ms: Optional[int] = None) -> bool:
    """Return True if the timestamp (or now) falls in the configured trading window."""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms is not None \
         else datetime.now(timezone.utc)
    if config.us_holiday_filter and _is_us_holiday(dt):
        return False
    if not config.hour_filter_enabled:
        return True
    dow    = dt.weekday()
    hour   = dt.hour
    minute = dt.minute

    if dow >= 5:
        if not config.weekend_utc_ranges:
            return False
        return any(s <= hour < e for s, e in config.weekend_utc_ranges)

    if dow == 0 and config.us_weekly_open:
        if hour < 13 or (hour == 13 and minute < 30):
            return False
    if dow == 4 and config.us_weekly_close:
        if hour >= 20:
            return False

    if not config.weekday_utc_ranges:
        return True
    return any(s <= hour < e for s, e in config.weekday_utc_ranges)
