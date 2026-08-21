"""Alpaca implementation of the MarketData protocol - system-spec.md S1.1.

Extracted from position_sizing.py's fetch_daily_bars(); behavior-preserving
(same IEX feed, same request shape). daily_bars() returns exactly the
DataFrame shape bars_to_frames() already expects (index=(symbol,timestamp),
columns include 'close'/'volume'), per the MarketData protocol's contract.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from brokers.base import MarketData, Quote


class AlpacaMarketData(MarketData):
    def __init__(self, api_key: str, secret_key: str):
        self._client = StockHistoricalDataClient(api_key, secret_key)

    def daily_bars(self, symbols: list[str], start: datetime, end: datetime) -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        return self._client.get_stock_bars(req).df

    def latest_quote(self, symbol: str) -> Quote:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
        q = self._client.get_stock_latest_quote(req)[symbol]
        return Quote(symbol=symbol, bid=float(q.bid_price), ask=float(q.ask_price))
