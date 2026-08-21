"""Options pricing correctness - system-spec.md S12.

- American call on a non-dividend payer equals its European (Black-Scholes)
  value - no early-exercise value when there's no dividend to capture.
- American put value >= European put value - early exercise is always
  worth at least as much for a put.
- IV round-trip: price at a known vol, solve implied, recover the input.
"""

from datetime import date

import pytest

from options.pricing import delta, implied_vol, price_binomial, price_black_scholes

VALUATION_DATE = date(2024, 1, 2)   # a real NYSE trading day (see test_trading_calendar.py)
EXPIRY = date(2024, 3, 15)          # ~72 calendar days out
SPOT = 100.0
STRIKE = 100.0
RATE = 0.04
VOL = 0.25


def test_american_call_equals_european_when_no_dividend():
    bs_price = price_black_scholes("call", SPOT, STRIKE, EXPIRY, VALUATION_DATE, RATE, div_yield=0.0, vol=VOL)
    binomial_european = price_binomial("call", SPOT, STRIKE, EXPIRY, VALUATION_DATE, RATE, 0.0, VOL, american=False)
    binomial_american = price_binomial("call", SPOT, STRIKE, EXPIRY, VALUATION_DATE, RATE, 0.0, VOL, american=True)

    assert binomial_european == pytest.approx(bs_price, rel=5e-3)
    assert binomial_american == pytest.approx(bs_price, rel=5e-3)


def test_american_put_at_least_european_put():
    european_put = price_black_scholes("put", SPOT, STRIKE, EXPIRY, VALUATION_DATE, RATE, div_yield=0.0, vol=VOL)
    american_put = price_binomial("put", SPOT, STRIKE, EXPIRY, VALUATION_DATE, RATE, 0.0, VOL, american=True)
    assert american_put >= european_put - 1e-6


def test_american_put_with_dividend_exceeds_no_dividend():
    """Dividend-paying underlyings increase early-exercise value for puts
    further (S5.2)."""
    no_div = price_binomial("put", SPOT, STRIKE, EXPIRY, VALUATION_DATE, RATE, 0.0, VOL, american=True)
    with_div = price_binomial("put", SPOT, STRIKE, EXPIRY, VALUATION_DATE, RATE, 0.03, VOL, american=True)
    assert with_div >= no_div - 1e-6


@pytest.mark.parametrize("option_type,vol", [("call", 0.20), ("call", 0.45), ("put", 0.30)])
def test_iv_round_trip(option_type, vol):
    price = price_black_scholes(option_type, SPOT, STRIKE, EXPIRY, VALUATION_DATE, RATE, div_yield=0.0, vol=vol)
    recovered = implied_vol(option_type, price, SPOT, STRIKE, EXPIRY, VALUATION_DATE, RATE, div_yield=0.0)
    assert recovered is not None
    assert recovered == pytest.approx(vol, abs=1e-3)


def test_implied_vol_returns_none_on_unsolvable_price():
    # A "market price" below intrinsic value for a deep ITM call has no
    # solution in any reasonable vol range - impliedVolatility must raise
    # and this must return None, never a fabricated value (S5.2).
    result = implied_vol("call", market_mid=0.01, spot=SPOT, strike=50.0, expiry=EXPIRY,
                          valuation_date=VALUATION_DATE, rate=RATE, div_yield=0.0)
    assert result is None


def test_delta_call_between_zero_and_one_put_between_minus_one_and_zero():
    call_delta = delta("call", SPOT, STRIKE, EXPIRY, VALUATION_DATE, RATE, 0.0, VOL)
    put_delta = delta("put", SPOT, STRIKE, EXPIRY, VALUATION_DATE, RATE, 0.0, VOL)
    assert 0.0 < call_delta < 1.0
    assert -1.0 < put_delta < 0.0
