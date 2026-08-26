"""Exit 0 if today is the first NYSE trading session of its week, exit 1
otherwise - system-spec.md S15.2: "if Monday is a holiday, the weekly run
moves to Tuesday - implement as 'first business day of the week', not a
hardcoded weekday." run_weekly.ps1 checks this before invoking
position_sizing.py, so the weekly scheduled task can fire every weekday
and still only actually run once, on whichever day is genuinely first.

Usage:
    python scheduling/is_weekly_run_day.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from trading_calendar import TradingCalendar


def is_first_trading_day_of_week(cal: TradingCalendar, d) -> bool:
    if not cal.is_trading_day(d):
        return False
    prev = cal.previous_session(d)
    # Holiday-robust: if Monday is a holiday, previous_session(Tuesday) is
    # the prior Friday - a different (and earlier) ISO week, so Tuesday
    # correctly registers as first. A plain "is today Monday" check would
    # miss this entirely.
    return d.isocalendar()[:2] != prev.isocalendar()[:2]


if __name__ == "__main__":
    cal = TradingCalendar("NYSE")
    today = datetime.now(timezone.utc).date()
    if is_first_trading_day_of_week(cal, today):
        print(f"{today} is the first trading day of its week - proceeding")
        sys.exit(0)
    print(f"{today} is not the first trading day of its week - skipping (S15.2)")
    sys.exit(1)
