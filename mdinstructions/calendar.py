"""
Trading calendar and day-count utilities, backed by QuantLib.

All date arithmetic in the project routes through this module. QuantLib's
Date objects do not interoperate with datetime or pandas timestamps, so
conversion is confined here rather than spread across the codebase.

Install:  pip install QuantLib
"""

from __future__ import annotations

import datetime as _dt
from typing import Iterable

import QuantLib as ql
import pandas as pd


# --------------------------------------------------------------------------
# Conversion helpers — the only place ql.Date should appear outside this file
# --------------------------------------------------------------------------

def to_ql(d) -> ql.Date:
    """Accept datetime.date, datetime.datetime, or pd.Timestamp."""
    if isinstance(d, ql.Date):
        return d
    if isinstance(d, pd.Timestamp):
        d = d.date()
    elif isinstance(d, _dt.datetime):
        d = d.date()
    if not isinstance(d, _dt.date):
        raise TypeError(f"cannot convert {type(d)!r} to ql.Date")
    return ql.Date(d.day, d.month, d.year)


def from_ql(d: ql.Date) -> _dt.date:
    return _dt.date(d.year(), d.month(), d.dayOfMonth())


# --------------------------------------------------------------------------

class TradingCalendar:
    """Exchange calendar wrapper.

    market: "NYSE" (default) or "LSE".
    """

    _MARKETS = {
        "NYSE": lambda: ql.UnitedStates(ql.UnitedStates.NYSE),
        "LSE": lambda: ql.UnitedKingdom(ql.UnitedKingdom.Exchange),
    }

    def __init__(self, market: str = "NYSE"):
        if market not in self._MARKETS:
            raise ValueError(f"unknown market {market!r}")
        self.market = market
        self.cal = self._MARKETS[market]()
        self.dc = ql.Actual252()

    # -- basic queries -----------------------------------------------------

    def is_trading_day(self, d) -> bool:
        return self.cal.isBusinessDay(to_ql(d))

    def previous_session(self, d) -> _dt.date:
        """Last trading day strictly before d."""
        return from_ql(self.cal.advance(to_ql(d), ql.Period(-1, ql.Days)))

    def next_session(self, d) -> _dt.date:
        return from_ql(self.cal.advance(to_ql(d), ql.Period(1, ql.Days)))

    def sessions_back(self, d, n: int) -> _dt.date:
        """The date n trading sessions before d.

        Uses the exchange calendar, so holidays and half-days are handled
        correctly. Do not substitute a fixed number of calendar days.
        """
        if n < 0:
            raise ValueError("n must be non-negative")
        return from_ql(self.cal.advance(to_ql(d), ql.Period(-n, ql.Days)))

    def expected_sessions(self, start, end, include_end: bool = True) -> int:
        """Number of trading sessions in (start, end].

        Compare against the actual bar count to detect missing data.
        """
        return self.cal.businessDaysBetween(
            to_ql(start), to_ql(end), False, include_end
        )

    def sessions_between(self, start, end) -> list[_dt.date]:
        """Every trading date in [start, end]."""
        out, cur, last = [], to_ql(start), to_ql(end)
        while cur <= last:
            if self.cal.isBusinessDay(cur):
                out.append(from_ql(cur))
            cur = cur + 1
        return out

    # -- annualisation -----------------------------------------------------

    def year_fraction(self, start, end) -> float:
        """Act/252 year fraction. Use instead of hardcoding /252."""
        return self.dc.yearFraction(to_ql(start), to_ql(end))

    def annualisation_factor(self, start, end, n_obs: int) -> float:
        """Multiplier converting per-observation variance to annual variance.

        Prefer this over a blanket * 252 when the sample window contains
        holidays, which is always.
        """
        yf = self.year_fraction(start, end)
        if yf <= 0:
            raise ValueError("non-positive year fraction")
        return n_obs / yf

    # -- data integrity ----------------------------------------------------

    def validate_bar_series(
        self,
        dates: Iterable,
        tolerance: int = 0,
    ) -> tuple[bool, str]:
        """Check a bar series has no missing sessions.

        Returns (ok, message). tolerance allows N missing sessions before
        failing — keep at 0 for anything feeding a volatility estimate.
        """
        dates = sorted(from_ql(to_ql(d)) for d in dates)
        if len(dates) < 2:
            return False, "fewer than two bars"

        expected = self.expected_sessions(dates[0], dates[-1])
        actual = len(dates) - 1
        missing = expected - actual

        if missing > tolerance:
            got = set(dates)
            gaps = [
                d for d in self.sessions_between(dates[0], dates[-1])
                if d not in got
            ]
            return False, (
                f"{missing} missing session(s); "
                f"first gaps: {gaps[:5]}"
            )
        return True, "ok"

    def assert_fresh(self, latest_bar_date, run_date=None) -> None:
        """Abort if the most recent bar is not the previous session.

        For a pre-market run this is the guard against sizing on stale
        data after an upstream feed failure.
        """
        run_date = run_date or _dt.date.today()
        if not self.is_trading_day(run_date):
            raise RuntimeError(f"{run_date} is not a {self.market} session")

        expected = self.previous_session(run_date)
        actual = from_ql(to_ql(latest_bar_date))
        if actual != expected:
            raise RuntimeError(
                f"stale bars: latest is {actual}, expected {expected}"
            )
