"""Primary earnings source - Finnhub's free tier
(https://finnhub.io/docs/api/earnings-calendar). Needs FINNHUB_API_KEY in
.env; get one free at https://finnhub.io/register. provider.py falls back
to yfinance automatically when this key isn't set.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from .base import EarningsCalendar

_BASE_URL = "https://finnhub.io/api/v1/calendar/earnings"
_LOOKBACK_DAYS = 200
_LOOKAHEAD_DAYS = 200
_TIMEOUT_S = 10


class FinnhubEarningsCalendar(EarningsCalendar):
    def __init__(self, api_key: str):
        self._api_key = api_key

    def _dates(self, ticker: str):
        today = datetime.now(timezone.utc).date()
        params = {
            "from": (today - timedelta(days=_LOOKBACK_DAYS)).isoformat(),
            "to": (today + timedelta(days=_LOOKAHEAD_DAYS)).isoformat(),
            "symbol": ticker,
            "token": self._api_key,
        }
        resp = requests.get(_BASE_URL, params=params, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        rows = resp.json().get("earningsCalendar", [])
        return sorted(date.fromisoformat(r["date"]) for r in rows if r.get("date"))

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
