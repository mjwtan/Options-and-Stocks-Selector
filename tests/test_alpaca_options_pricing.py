"""submit_option()'s limit pricing - two real bugs found live, in order:
1. Pricing at the mid isn't marketable (a SELL at mid sits above the bid,
   a BUY sits below the ask, so neither crosses at all).
2. Pricing exactly *at* the touch (BUY at ask, SELL at bid) still isn't
   reliably marketable in Alpaca's paper options simulation - orders sat
   ACCEPTED for over a minute at the then-current touch price with the
   quote never moving. Only a price that genuinely crosses *through* the
   touch reliably fills.
See brokers/alpaca_options.py's submit_option docstring for the full
incident writeup.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from brokers.alpaca_options import AlpacaOptionsBroker
from brokers.base import Contract, OrderSide


def _contract(bid=1.00, ask=1.10):
    return Contract(
        occ_symbol="TEST240315P00050000", underlying="TEST", contract_type="put",
        strike=50.0, expiry=date(2024, 3, 15), bid=bid, ask=ask, open_interest=500, volume=10,
    )


@pytest.fixture
def broker():
    with patch("brokers.alpaca_options.TradingClient"), patch("brokers.alpaca_options.OptionHistoricalDataClient"):
        b = AlpacaOptionsBroker("fake-key", "fake-secret", paper=True)
        b._checked_approval = True  # skip the account-lookup call in _check_options_approval
        return b


def test_buy_prices_above_the_ask_not_at_mid_or_at_the_touch(broker):
    broker._trading.submit_order = MagicMock(return_value=MagicMock(id="x", symbol="TEST240315P00050000", status="new", qty="1", notional=None))
    broker.submit_option(_contract(bid=1.00, ask=1.10), qty=1, side=OrderSide.BUY)

    submitted_request = broker._trading.submit_order.call_args[0][0]
    # spread=0.10, cross=max(0.01, 10%*0.10)=0.01 -> 1.10+0.01=1.11
    assert submitted_request.limit_price == pytest.approx(1.11)
    assert submitted_request.limit_price > 1.10  # strictly beyond the ask, not at it
    assert submitted_request.limit_price != pytest.approx(1.05)  # not the mid either


def test_sell_prices_below_the_bid_not_at_mid_or_at_the_touch(broker):
    broker._trading.submit_order = MagicMock(return_value=MagicMock(id="x", symbol="TEST240315P00050000", status="new", qty="1", notional=None))
    broker.submit_option(_contract(bid=1.00, ask=1.10), qty=1, side=OrderSide.SELL)

    submitted_request = broker._trading.submit_order.call_args[0][0]
    assert submitted_request.limit_price == pytest.approx(0.99)
    assert submitted_request.limit_price < 1.00  # strictly beyond the bid, not at it
    assert submitted_request.limit_price != pytest.approx(1.05)


def test_buy_and_sell_prices_differ_for_a_wide_spread(broker):
    """Regression guard: if this ever collapses back to a single "mid" or
    "at the touch" price for both sides, that's the exact bug this test
    file exists to catch - see the module docstring."""
    broker._trading.submit_order = MagicMock(return_value=MagicMock(id="x", symbol="TEST240315P00050000", status="new", qty="1", notional=None))

    broker.submit_option(_contract(bid=1.00, ask=1.20), qty=1, side=OrderSide.BUY)
    buy_price = broker._trading.submit_order.call_args[0][0].limit_price

    broker.submit_option(_contract(bid=1.00, ask=1.20), qty=1, side=OrderSide.SELL)
    sell_price = broker._trading.submit_order.call_args[0][0].limit_price

    # spread=0.20, cross=max(0.01, 10%*0.20)=0.02
    assert buy_price == pytest.approx(1.22)
    assert sell_price == pytest.approx(0.98)
    assert buy_price != sell_price


def test_cross_amount_scales_with_spread_width():
    """A wider spread should cross by more than the 1-cent floor, so a
    cheap tight-spread contract and an expensive wide-spread one both get
    a genuinely marketable price rather than a fixed cent bump that's
    negligible on an expensive contract."""
    with patch("brokers.alpaca_options.TradingClient"), patch("brokers.alpaca_options.OptionHistoricalDataClient"):
        b = AlpacaOptionsBroker("fake-key", "fake-secret", paper=True)
        b._checked_approval = True
        b._trading.submit_order = MagicMock(return_value=MagicMock(id="x", symbol="X", status="new", qty="1", notional=None))

    b.submit_option(_contract(bid=10.00, ask=11.50), qty=1, side=OrderSide.BUY)  # spread=1.50 -> cross=0.15
    price = b._trading.submit_order.call_args[0][0].limit_price
    assert price == pytest.approx(11.65)


def test_never_prices_at_or_below_zero():
    with patch("brokers.alpaca_options.TradingClient"), patch("brokers.alpaca_options.OptionHistoricalDataClient"):
        b = AlpacaOptionsBroker("fake-key", "fake-secret", paper=True)
        b._checked_approval = True
        b._trading.submit_order = MagicMock(return_value=MagicMock(id="x", symbol="X", status="new", qty="1", notional=None))

    b.submit_option(_contract(bid=0.01, ask=0.02), qty=1, side=OrderSide.SELL)
    price = b._trading.submit_order.call_args[0][0].limit_price
    assert price > 0
