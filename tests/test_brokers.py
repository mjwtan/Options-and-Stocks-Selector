"""Broker/data adapter abstraction - system-spec.md S1.

Two things checked here:
1. Equity-only mode never imports brokers.alpaca_options - the whole point
   of S1.1's "no options dependency imported or credentialed" requirement.
2. position_sizing.py's validate()/bars_to_frames() work against fake,
   in-memory EquityBroker/MarketData implementations - previously these
   were only exercisable against a live Alpaca account.
"""

import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from brokers.base import Account, EquityBroker, FillStatus, MarketData, Order, OrderSide, Position, Quote
from brokers.providers import resolve_providers
from trading_calendar import TradingCalendar

import position_sizing as ps


class FakeEquityBroker(EquityBroker):
    def __init__(self, tradable=None, account=None):
        self._tradable = tradable or {}
        self._account = account or Account(equity=100_000, cash=10_000, buying_power=50_000, portfolio_value=100_000)
        self.submitted = []

    def positions(self):
        return []

    def account(self):
        return self._account

    def is_tradable(self, symbol):
        if symbol not in self._tradable:
            raise RuntimeError(f"unknown asset {symbol}")
        return self._tradable[symbol]

    def submit_notional(self, symbol, notional, side):
        self.submitted.append((symbol, notional, side))
        return Order(id="fake-1", symbol=symbol, status=FillStatus.FILLED, notional=notional)

    def close_position(self, symbol):
        return Order(id="fake-close", symbol=symbol, status=FillStatus.FILLED)

    def order_status(self, order_id):
        return Order(id=order_id, symbol="X", status=FillStatus.FILLED)


class FakeMarketData(MarketData):
    def __init__(self, bars_df):
        self._bars_df = bars_df

    def daily_bars(self, symbols, start, end):
        return self._bars_df

    def latest_quote(self, symbol):
        return Quote(symbol=symbol, bid=99.5, ask=100.5)


def _make_bars_df(symbols, dates):
    rows = []
    for s in symbols:
        for i, d in enumerate(dates):
            rows.append({"symbol": s, "timestamp": d, "close": 100.0 + i * 0.1, "volume": 1_000_000})
    df = pd.DataFrame(rows).set_index(["symbol", "timestamp"])
    return df


def test_equity_only_never_imports_options_module():
    sys.modules.pop("brokers.alpaca_options", None)
    resolve_providers("fake-key", "fake-secret", options_broker="none")
    assert "brokers.alpaca_options" not in sys.modules


def test_validate_reports_non_tradable_and_lookup_failure():
    df = pd.DataFrame({
        "ticker": ["AAA", "BBB"],
        "ranking": [1, 2],
        "regime": [1, 1],
        "risk_index": [10.0, 20.0],
        "volatility_index": [10.0, 20.0],
        "sentiment_index": [50.0, 50.0],
    })
    broker = FakeEquityBroker(tradable={"AAA": False})  # BBB: lookup fails (not in dict)
    cfg = ps.Config()
    errors = ps.validate(df, broker, cfg)
    assert any("AAA is not tradable" in e for e in errors)
    assert any("BBB failed asset lookup" in e for e in errors)


def test_bars_to_frames_uses_fake_market_data_and_detects_calendar_gap():
    cal = TradingCalendar("NYSE")
    dates = cal.sessions_between(
        datetime(2024, 1, 2).date(), datetime(2024, 1, 31).date()
    )
    # Drop one mid-series session for one symbol only - the union-index
    # heuristic this replaced would miss this if it were dropped for *all*
    # symbols simultaneously; here it's exercised via the per-ticker
    # cal.validate_bar_series path directly.
    gapped_dates = [d for d in dates if d != dates[10]]

    bars_df = _make_bars_df(["AAA"], gapped_dates)
    with pytest.raises(ps.ValidationError, match="missing session"):
        ps.bars_to_frames(bars_df, ["AAA"], lookback_days=len(gapped_dates), cal=cal)

    clean_df = _make_bars_df(["AAA"], dates)
    close, volume = ps.bars_to_frames(clean_df, ["AAA"], lookback_days=len(dates), cal=cal)
    assert list(close.columns) == ["AAA"]
    assert len(close) == len(dates)
