"""Correctness checks for trading_calendar.py - quantlib-integration.md S7.
Run with: pytest tests/test_trading_calendar.py -v

Fixture dates are pinned to real, independently-checkable 2024 NYSE
holidays/weekends rather than derived from the calendar under test, so
these are not tautological:
  - Mon 2024-01-01 (New Year's Day) and Thu 2024-07-04 (Independence Day)
    are both NYSE holidays.
  - Wed 2024-01-17 is an ordinary mid-January trading day (no holiday
    nearby), used as a synthetic "dropped bar" gap.
"""

import datetime as dt

import pandas as pd
import pytest

from trading_calendar import TradingCalendar, to_ql, from_ql


@pytest.fixture(scope="module")
def cal():
    return TradingCalendar("NYSE")


def test_sessions_back_skips_holiday(cal):
    # Fri 2024-07-05 minus 1 session -> Wed 2024-07-03 (Thu 07-04 is a holiday).
    assert cal.sessions_back(dt.date(2024, 7, 5), 1) == dt.date(2024, 7, 3)
    # Tue 2024-01-02 minus 1 session -> Fri 2023-12-29 (Mon 01-01 is a holiday).
    assert cal.sessions_back(dt.date(2024, 1, 2), 1) == dt.date(2023, 12, 29)


def test_validate_bar_series_names_the_missing_date(cal):
    all_sessions = cal.sessions_between(dt.date(2024, 1, 2), dt.date(2024, 1, 31))
    with_gap = [d for d in all_sessions if d != dt.date(2024, 1, 17)]

    ok, msg = cal.validate_bar_series(with_gap)
    assert ok is False
    assert "2024, 1, 17" in msg or "2024-01-17" in msg

    ok_clean, _ = cal.validate_bar_series(all_sessions)
    assert ok_clean is True


def test_assert_fresh_raises_on_weekend_run_date(cal):
    with pytest.raises(RuntimeError):
        cal.assert_fresh(dt.date(2024, 1, 2), run_date=dt.date(2024, 1, 6))  # Saturday


def test_assert_fresh_raises_on_stale_bars(cal):
    # Run date Thu 2024-01-04 expects at least the previous session
    # (Wed 01-03) as the latest bar; a bar dated Tue 01-02 is behind that.
    with pytest.raises(RuntimeError):
        cal.assert_fresh(dt.date(2024, 1, 2), run_date=dt.date(2024, 1, 4))

    # No exception when the latest bar is exactly the previous session.
    cal.assert_fresh(dt.date(2024, 1, 3), run_date=dt.date(2024, 1, 4))

    # No exception when the latest bar is *newer* than the previous session
    # (e.g. today's bar has already arrived because this ran later in the
    # day, not strictly pre-market) - this was a real bug: the original
    # exact-match check rejected data that was actually fresher than
    # required, not staler.
    cal.assert_fresh(dt.date(2024, 1, 4), run_date=dt.date(2024, 1, 4))


def test_annualisation_factor_differs_from_naive_252_on_a_gap(cal):
    start, end = dt.date(2024, 1, 2), dt.date(2024, 12, 31)
    expected_sessions = cal.expected_sessions(start, end)
    assert expected_sessions == 251

    # No gap: observations match the calendar's expected session count
    # exactly, so the factor equals the naive constant.
    assert cal.annualisation_factor(start, end, n_obs=expected_sessions) == pytest.approx(252.0)

    # A gap (n_obs short of what the calendar expects for this window)
    # must produce a different, non-252 factor - this is the case a blanket
    # `* 252` cannot detect or correct for.
    gapped_factor = cal.annualisation_factor(start, end, n_obs=expected_sessions - 6)
    assert gapped_factor != pytest.approx(252.0)
    assert gapped_factor == pytest.approx((expected_sessions - 6) / (expected_sessions / 252), rel=1e-9)


@pytest.mark.parametrize(
    "value",
    [
        dt.date(2024, 3, 15),
        dt.datetime(2024, 3, 15, 9, 30),
        pd.Timestamp("2024-03-15"),
    ],
)
def test_round_trip_conversion(value):
    result = from_ql(to_ql(value))
    assert result == dt.date(2024, 3, 15)
