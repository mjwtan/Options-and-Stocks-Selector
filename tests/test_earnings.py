"""Earnings data source - system-spec.md S5.7/S5.8 depend on this. Hits the
real network (yfinance, and Finnhub if FINNHUB_API_KEY is set) since these
providers have no meaningful mock - the point is confirming the actual
integration works, per the module's own recommendation to spot-check.
"""

import os
from datetime import date, timedelta

import pytest

from earnings.provider import resolve_earnings_calendar
from earnings.yfinance_earnings import YFinanceEarningsCalendar

KNOWN_TICKERS = ["AAPL", "MSFT", "AMZN"]


@pytest.mark.parametrize("ticker", KNOWN_TICKERS)
def test_yfinance_fallback_returns_sane_dates(ticker):
    cal = YFinanceEarningsCalendar()
    last = cal.last_earnings_date(ticker)
    nxt = cal.next_earnings_date(ticker)

    assert last is not None, f"{ticker}: expected a recent reported earnings date"
    assert nxt is not None, f"{ticker}: expected an upcoming earnings date"
    assert last < date.today()
    assert nxt >= date.today()
    # A liquid large-cap reports roughly quarterly - sanity bound, not exact.
    assert date.today() - last < timedelta(days=200)


def test_unknown_ticker_returns_none_not_a_crash():
    cal = YFinanceEarningsCalendar()
    assert cal.next_earnings_date("ZZZZNOTREAL") is None
    assert cal.last_earnings_date("ZZZZNOTREAL") is None


def test_provider_falls_back_to_yfinance_without_a_key(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    cal = resolve_earnings_calendar()
    assert isinstance(cal, YFinanceEarningsCalendar)


@pytest.mark.skipif(not os.environ.get("FINNHUB_API_KEY"), reason="FINNHUB_API_KEY not set")
def test_finnhub_agrees_with_yfinance_when_key_is_present():
    from earnings.finnhub_earnings import FinnhubEarningsCalendar

    finnhub = FinnhubEarningsCalendar(os.environ["FINNHUB_API_KEY"])
    yf_cal = YFinanceEarningsCalendar()

    for ticker in KNOWN_TICKERS:
        fh_last = finnhub.last_earnings_date(ticker)
        yf_last = yf_cal.last_earnings_date(ticker)
        if fh_last and yf_last:
            assert abs((fh_last - yf_last).days) <= 1
