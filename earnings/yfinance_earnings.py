"""No-key earnings source, used as the automatic fallback when
FINNHUB_API_KEY isn't set (see provider.py). Unofficial (scrapes Yahoo
Finance via the yfinance package) - treat as "works out of the box" rather
than "authoritative"; upgrade to Finnhub for anything that matters more.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import yfinance as yf

from .base import EarningsCalendar


class YFinanceEarningsCalendar(EarningsCalendar):
    def _dates(self, ticker: str):
        t = yf.Ticker(ticker)
        df = t.get_earnings_dates(limit=16)
        if df is None or df.empty:
            return None
        return sorted(ts.date() for ts in df.index.tz_localize(None))

    def next_earnings_date(self, ticker: str) -> Optional[date]:
        try:
            dates = self._dates(ticker)
        except Exception:
            return None
        if not dates:
            return None
        today = datetime.now(timezone.utc).date()
        upcoming = [d for d in dates if d >= today]
        return min(upcoming) if upcoming else None

    def last_earnings_date(self, ticker: str) -> Optional[date]:
        try:
            dates = self._dates(ticker)
        except Exception:
            return None
        if not dates:
            return None
        today = datetime.now(timezone.utc).date()
        past = [d for d in dates if d < today]
        return max(past) if past else None
