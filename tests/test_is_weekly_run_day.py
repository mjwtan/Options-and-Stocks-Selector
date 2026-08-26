"""system-spec.md S15.2: "implement as 'first business day of the week',
not a hardcoded weekday" - so a holiday Monday correctly shifts the
weekly job to Tuesday instead of being silently skipped for the week.
"""

from datetime import date

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scheduling"))

from is_weekly_run_day import is_first_trading_day_of_week
from trading_calendar import TradingCalendar

CAL = TradingCalendar("NYSE")


def test_ordinary_monday_is_first_trading_day():
    # 2024-01-08 is an ordinary Monday, no holiday nearby.
    assert is_first_trading_day_of_week(CAL, date(2024, 1, 8)) is True


def test_ordinary_tuesday_is_not_first_trading_day():
    assert is_first_trading_day_of_week(CAL, date(2024, 1, 9)) is False


def test_holiday_monday_shifts_first_day_to_tuesday():
    # 2024-01-01 (New Year's Day, Monday) is a NYSE holiday; the week's
    # first trading day is Tuesday 2024-01-02.
    assert is_first_trading_day_of_week(CAL, date(2024, 1, 2)) is True
    assert is_first_trading_day_of_week(CAL, date(2024, 1, 3)) is False


def test_weekend_is_never_a_run_day():
    assert is_first_trading_day_of_week(CAL, date(2024, 1, 6)) is False  # Saturday
    assert is_first_trading_day_of_week(CAL, date(2024, 1, 7)) is False  # Sunday
