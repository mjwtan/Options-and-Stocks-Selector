"""fetch_and_validate_bars' retry - a real failure observed live: Alpaca's
feed occasionally hadn't posted a thin name's most recent bar yet at fetch
time, which self-resolved within seconds. See position_sizing.py's
docstring on the function for the full incident writeup.
"""

from datetime import datetime, timezone

import pandas as pd
import pytest

from brokers.base import MarketData
from position_sizing import ValidationError, fetch_and_validate_bars
from trading_calendar import TradingCalendar


def _bars_df(symbols, dates, missing=None):
    """missing: {symbol: set_of_date_indices_to_drop}"""
    missing = missing or {}
    rows = []
    for s in symbols:
        for i, d in enumerate(dates):
            if i in missing.get(s, set()):
                continue
            rows.append({"symbol": s, "timestamp": d, "close": 100.0 + i * 0.1, "volume": 1_000_000})
    return pd.DataFrame(rows).set_index(["symbol", "timestamp"])


class ScriptedMarketData(MarketData):
    """Returns a different canned response on each successive call, like a
    feed that's momentarily behind and then catches up."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    def daily_bars(self, symbols, start, end):
        response = self._responses[min(self.call_count, len(self._responses) - 1)]
        self.call_count += 1
        return response

    def latest_quote(self, symbol):
        raise NotImplementedError


@pytest.fixture
def cal():
    return TradingCalendar("NYSE")


@pytest.fixture
def dates(cal):
    return cal.sessions_between((datetime(2024, 1, 2)).date(), (datetime(2024, 1, 31)).date())


def test_retries_and_succeeds_when_gap_resolves(cal, dates):
    gapped = _bars_df(["AAA", "BBB"], dates, missing={"BBB": {len(dates) - 1}})
    clean = _bars_df(["AAA", "BBB"], dates)
    market_data = ScriptedMarketData([gapped, clean])

    close, _volume = fetch_and_validate_bars(market_data, ["AAA", "BBB"], len(dates), cal, max_retries=2, retry_delay_s=0)

    assert market_data.call_count == 2
    assert list(close.columns) == ["AAA", "BBB"]
    assert not close.isna().any().any()


def test_raises_after_exhausting_retries_on_a_persistent_gap(cal, dates):
    gapped = _bars_df(["AAA", "BBB"], dates, missing={"BBB": {5}})  # mid-series - a real gap, never resolves
    market_data = ScriptedMarketData([gapped])  # every call returns the same gap

    with pytest.raises(ValidationError, match="bar gaps detected"):
        fetch_and_validate_bars(market_data, ["AAA", "BBB"], len(dates), cal, max_retries=2, retry_delay_s=0)

    assert market_data.call_count == 3  # initial attempt + 2 retries, then gives up


def test_missing_ticker_entirely_is_not_retried(cal, dates):
    only_aaa = _bars_df(["AAA"], dates)  # BBB never appears at all
    market_data = ScriptedMarketData([only_aaa])

    with pytest.raises(ValidationError, match="missing bars entirely"):
        fetch_and_validate_bars(market_data, ["AAA", "BBB"], len(dates), cal, max_retries=2, retry_delay_s=0)

    assert market_data.call_count == 1  # no retry for this failure mode
