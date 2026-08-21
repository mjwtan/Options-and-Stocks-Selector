"""Earnings-date lookup - needed by system-spec.md S5.7 (exclude short puts
when earnings fall before expiry) and S5.8 (skip gate: earnings inside 2
trading days).

No data source was configured for this originally (.env only carries
Alpaca equity keys). Both dates return `None` when unknown - callers MUST
treat that as the conservative case (exclude / flag as "unknown, not
gated"), never as "no earnings, safe to proceed". See provider.py for the
Finnhub-primary/yfinance-fallback resolution.
"""

from __future__ import annotations

from datetime import date
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class EarningsCalendar(Protocol):
    def next_earnings_date(self, ticker: str) -> Optional[date]:
        """The next scheduled/estimated earnings date on or after today, or
        None if unknown/unavailable."""
        ...

    def last_earnings_date(self, ticker: str) -> Optional[date]:
        """The most recent reported earnings date before today, or None if
        unknown/unavailable."""
        ...
