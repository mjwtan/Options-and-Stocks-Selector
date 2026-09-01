"""AlpacaEquityBroker.positions() must only return equities - a real bug
found live: get_all_positions() returns every asset class in one list, so
an open option position was leaking into the equity list. Left unfixed,
trade_from_csv.py's "held but not in this week's target -> close it"
logic could see an option's OCC symbol as an untargeted equity holding and
try to close_position() on it.
"""

from unittest.mock import MagicMock, patch

from alpaca.trading.enums import AssetClass

from brokers.alpaca_equity import AlpacaEquityBroker


def _position(symbol, asset_class, qty="1", market_value="100.0", unrealized_plpc=None):
    p = MagicMock()
    p.symbol = symbol
    p.asset_class = asset_class
    p.qty = qty
    p.market_value = market_value
    p.unrealized_plpc = unrealized_plpc
    return p


def test_positions_excludes_option_positions():
    with patch("brokers.alpaca_equity.TradingClient") as MockClient:
        broker = AlpacaEquityBroker("fake-key", "fake-secret", paper=True)
        broker._client.get_all_positions.return_value = [
            _position("AAPL", AssetClass.US_EQUITY),
            _position("SPY260918P00705000", AssetClass.US_OPTION),
            _position("MSFT", AssetClass.US_EQUITY),
        ]

        result = broker.positions()

    symbols = {p.symbol for p in result}
    assert symbols == {"AAPL", "MSFT"}
    assert "SPY260918P00705000" not in symbols


def test_positions_empty_when_only_options_held():
    with patch("brokers.alpaca_equity.TradingClient"):
        broker = AlpacaEquityBroker("fake-key", "fake-secret", paper=True)
        broker._client.get_all_positions.return_value = [
            _position("SPY260918P00705000", AssetClass.US_OPTION),
        ]
        result = broker.positions()

    assert result == []


def test_constructor_strips_whitespace_from_credentials():
    """Real bug found live: a GitHub Actions secret pasted with a trailing
    newline reached requests as a literal '\\n' in the API key header,
    which requests correctly rejects with InvalidHeader - a much more
    confusing failure than a plain auth error. TradingClient must never
    see the raw, unstripped value."""
    with patch("brokers.alpaca_equity.TradingClient") as MockClient:
        AlpacaEquityBroker("fake-key\n\n", "  fake-secret\n", paper=True)

    MockClient.assert_called_once_with("fake-key", "fake-secret", paper=True)
