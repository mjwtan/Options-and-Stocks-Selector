"""Picks Finnhub if FINNHUB_API_KEY is set, else falls back to the no-key
yfinance source, so the S5.7/S5.8 earnings checks work out of the box even
before a Finnhub key is obtained.
"""

from __future__ import annotations

import os

from .base import EarningsCalendar


def resolve_earnings_calendar() -> EarningsCalendar:
    api_key = os.environ.get("FINNHUB_API_KEY")
    if api_key:
        from .finnhub_earnings import FinnhubEarningsCalendar
        return FinnhubEarningsCalendar(api_key)

    from .yfinance_earnings import YFinanceEarningsCalendar
    return YFinanceEarningsCalendar()
