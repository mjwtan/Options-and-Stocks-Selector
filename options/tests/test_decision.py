"""Instrument decision engine - system-spec.md S5.0-S5.8.

Synthetic contracts are priced with the real Black-Scholes engine
(options.pricing.price_black_scholes) at a known vol/strike, so the IV
solve and delta computation inside decision.py recover a known, predictable
value - these are full round-trips through the real QuantLib pricing code,
not mocks of it.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from brokers.base import Contract, EquityBroker, OptionsBroker
from earnings.base import EarningsCalendar
from options.decision import (
    DecisionConfig,
    OptionsLayerError,
    compute_instrument_decisions,
    decide_instrument_for_name,
)
from options.pricing import price_black_scholes
from trading_calendar import TradingCalendar

VALUATION_DATE = date(2024, 1, 2)  # a real NYSE trading day
SPOT = 100.0
RATE = 0.04
DIV_YIELD = 0.0
SIGMA_REALISED = 0.25


class FakeEquityBroker(EquityBroker):
    def positions(self):
        return []

    def account(self):
        raise NotImplementedError

    def is_tradable(self, symbol):
        return True

    def submit_notional(self, symbol, notional, side):
        raise NotImplementedError

    def close_position(self, symbol):
        raise NotImplementedError

    def order_status(self, order_id):
        raise NotImplementedError

    def pending_corporate_action(self, symbol, lookahead_days=90):
        return None


class FakeEarningsCalendar(EarningsCalendar):
    def __init__(self, next_date=None, per_ticker=None):
        self._next_date = next_date
        self._per_ticker = per_ticker or {}

    def next_earnings_date(self, ticker):
        if ticker in self._per_ticker:
            return self._per_ticker[ticker]
        return self._next_date

    def last_earnings_date(self, ticker):
        return None


class FakeOptionsBroker(OptionsBroker):
    def __init__(self, contracts):
        self._contracts = contracts

    def chain(self, symbol, expiry_range):
        return self._contracts

    def submit_option(self, contract, qty, side):
        raise NotImplementedError

    def option_positions(self):
        return []

    def buying_power_reserved(self):
        return 0


class RaisingOptionsBroker(OptionsBroker):
    """Simulates a provider outage/auth failure - system-spec.md S15.5."""
    def chain(self, symbol, expiry_range):
        raise RuntimeError("simulated provider outage")

    def submit_option(self, contract, qty, side):
        raise NotImplementedError

    def option_positions(self):
        return []

    def buying_power_reserved(self):
        return 0


def _synthetic_contract(contract_type, strike, expiry, vol, oi=500):
    price = price_black_scholes(contract_type, SPOT, strike, expiry, VALUATION_DATE, RATE, DIV_YIELD, vol)
    cp = "C" if contract_type == "call" else "P"
    occ = f"TEST{expiry.strftime('%y%m%d')}{cp}{int(round(strike * 1000)):08d}"
    return Contract(
        occ_symbol=occ, underlying="TEST", contract_type=contract_type, strike=strike, expiry=expiry,
        bid=price, ask=price, open_interest=oi, volume=10,  # zero spread - always clears S3.5's spread filter
    )


def _put_chain_for_rich_iv(expiry, iv=0.40):
    """iv=0.40 vs sigma_realised=0.25 -> iv_ratio=1.60, well above the 1.25
    rich threshold, so this should drive a short_put decision. Includes a
    spread of strikes so the delta search has real candidates."""
    return [_synthetic_contract("put", strike, expiry, iv) for strike in range(70, 106, 2)]


def _call_chain_for_cheap_iv(expiry, iv=0.15):
    """iv=0.15 vs sigma_realised=0.25 -> iv_ratio=0.60, well below the 0.85
    cheap threshold, so this should drive a long_call decision."""
    return [_synthetic_contract("call", strike, expiry, iv) for strike in range(90, 126, 2)]


def _decide(ranking, contracts, expiry_days=45, earnings_calendar=None, **overrides):
    cfg = DecisionConfig(**overrides) if overrides else DecisionConfig()
    return decide_instrument_for_name(
        ticker="TEST",
        ranking=ranking,
        expected_horizon_days=expiry_days,
        weight=0.05,
        sigma_realised=SIGMA_REALISED,
        spot=SPOT,
        portfolio_value=1_000_000,
        valuation_date=VALUATION_DATE,
        options_broker=FakeOptionsBroker(contracts),
        earnings_calendar=earnings_calendar or FakeEarningsCalendar(),
        div_yield=DIV_YIELD,
        cfg=cfg,
    )


def test_rank_1_to_5_is_always_shares_regardless_of_signal():
    expiry = VALUATION_DATE + timedelta(days=45)
    result = _decide(ranking=3, contracts=_put_chain_for_rich_iv(expiry))
    assert result.instrument == "shares"
    assert "rank 3" in result.reason


def test_rank_16_to_20_never_gets_a_long_call():
    expiry = VALUATION_DATE + timedelta(days=45)
    result = _decide(ranking=18, contracts=_call_chain_for_cheap_iv(expiry))
    assert result.instrument == "shares"
    assert "not permitted for rank" in result.reason


def test_rank_16_to_20_can_get_a_short_put():
    expiry = VALUATION_DATE + timedelta(days=45)
    result = _decide(ranking=18, contracts=_put_chain_for_rich_iv(expiry))
    assert result.instrument == "short_put"
    assert result.contracts >= 1


def test_no_edge_iv_ratio_falls_back_to_shares():
    expiry = VALUATION_DATE + timedelta(days=45)
    # iv=0.27 vs sigma_realised=0.25 -> ratio=1.08, inside [0.85, 1.25].
    contracts = [_synthetic_contract("put", s, expiry, 0.27) for s in range(70, 106, 2)]
    result = _decide(ranking=10, contracts=contracts)
    assert result.instrument == "shares"
    assert "no edge" in result.reason


def test_expiry_outside_tolerance_falls_back_to_shares():
    # Only a 21 DTE expiry listed (the nearest edge of the S3.5 window, so
    # it still survives the chain filter) while expected_horizon_days=45
    # clips to a 45 DTE target - 24 days away, beyond the +/-14 tolerance.
    near_expiry = VALUATION_DATE + timedelta(days=21)
    result = _decide(ranking=10, contracts=_put_chain_for_rich_iv(near_expiry), expiry_days=45)
    assert result.instrument == "shares"
    assert "no listed expiry" in result.reason


def test_delta_outside_tolerance_falls_back_to_shares():
    expiry = VALUATION_DATE + timedelta(days=45)
    # Several deep OTM puts, all far from the -0.30 target delta but still
    # priced high enough to clear the bid>0 filter - enough of them to
    # clear S3.5's min-surviving-contracts count too, so the test actually
    # reaches delta selection rather than being skipped earlier.
    contracts = [_synthetic_contract("put", s, expiry, 0.40) for s in (55.0, 60.0, 65.0, 70.0)]
    result = _decide(ranking=10, contracts=contracts)
    assert result.instrument == "shares"
    assert "no strike within" in result.reason


def test_short_put_full_path_sizes_and_reserves_capital():
    expiry = VALUATION_DATE + timedelta(days=45)
    result = _decide(ranking=10, contracts=_put_chain_for_rich_iv(expiry))
    assert result.instrument == "short_put"
    assert result.delta == pytest.approx(-0.30, abs=0.10)
    assert result.contracts >= 1
    assert result.capital_reserved == pytest.approx(result.strike * 100 * result.contracts)


def test_long_call_full_path():
    expiry = VALUATION_DATE + timedelta(days=45)
    result = _decide(ranking=10, contracts=_call_chain_for_cheap_iv(expiry))
    assert result.instrument == "long_call"
    assert result.delta == pytest.approx(0.60, abs=0.10)
    assert result.contracts >= 1


def test_earnings_before_expiry_excludes_short_put_specifically():
    expiry = VALUATION_DATE + timedelta(days=45)
    earnings_before_expiry = VALUATION_DATE + timedelta(days=20)
    result = _decide(
        ranking=10, contracts=_put_chain_for_rich_iv(expiry),
        earnings_calendar=FakeEarningsCalendar(earnings_before_expiry),
    )
    assert result.instrument == "shares"
    assert "excluded from short puts" in result.reason


def test_options_disabled_is_always_shares():
    result = decide_instrument_for_name(
        ticker="TEST", ranking=10, expected_horizon_days=45, weight=0.05, sigma_realised=SIGMA_REALISED,
        spot=SPOT, portfolio_value=1_000_000, valuation_date=VALUATION_DATE, options_broker=None,
        earnings_calendar=FakeEarningsCalendar(), div_yield=0.0, cfg=DecisionConfig(),
    )
    assert result.instrument == "shares"
    assert "OPTIONS_ENABLED" in result.reason


# --- compute_instrument_decisions: skip gates + redistribution ------------

def _make_df(tickers, rankings):
    return pd.DataFrame({
        "ticker": tickers,
        "ranking": rankings,
        "regime": [1] * len(tickers),
        "risk_index": [50.0] * len(tickers),
        "volatility_index": [50.0] * len(tickers),
        "sentiment_index": [50.0] * len(tickers),
        "expected_horizon_days": [45] * len(tickers),
    })


def _redistribute_fn_factory(adv20, cfg=None):
    def _fn(w):
        renorm = w / w.sum()
        return renorm, [], {}
    return _fn


def test_liquidity_skip_gate_redistributes_weight():
    tickers = ["AAA", "BBB", "CCC"]
    df = _make_df(tickers, [1, 2, 3])
    w = pd.Series([0.5, 0.3, 0.2], index=tickers)
    sigma = pd.Series([0.20, 0.20, 0.20], index=tickers)
    adv20 = pd.Series([10_000_000, 1_000_000, 10_000_000], index=tickers)  # BBB fails MIN_ADV
    spot = pd.Series([SPOT] * 3, index=tickers)

    survivors, decisions, skips = compute_instrument_decisions(
        df=df, w_constrained=w, sigma=sigma, adv20=adv20, spot=spot, portfolio_value=1_000_000,
        valuation_date=VALUATION_DATE, equity_broker=FakeEquityBroker(),
        options_broker=None, earnings_calendar=FakeEarningsCalendar(), cal=TradingCalendar("NYSE"),
        dividend_yields={}, cfg=DecisionConfig(), redistribute_fn=_redistribute_fn_factory(adv20),
    )

    assert "BBB" in skips
    assert skips["BBB"].gate == "liquidity"
    assert "BBB" not in survivors.index
    assert survivors.sum() == pytest.approx(1.0)
    assert set(decisions.keys()) == {"AAA", "CCC"}


def test_volatility_ceiling_skip_gate():
    # 5 names, 1 skip (20%) - stays under MAX_SKIP_FRACTION so this
    # exercises the gate itself, not the abort threshold (see
    # test_max_skip_fraction_aborts_the_run for that).
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    df = _make_df(tickers, [1, 2, 3, 4, 5])
    w = pd.Series([0.2] * 5, index=tickers)
    sigma = pd.Series([0.20, 1.50, 0.20, 0.20, 0.20], index=tickers)  # BBB exceeds MAX_SIGMA
    adv20 = pd.Series([10_000_000] * 5, index=tickers)
    spot = pd.Series([SPOT] * 5, index=tickers)

    _survivors, _decisions, skips = compute_instrument_decisions(
        df=df, w_constrained=w, sigma=sigma, adv20=adv20, spot=spot, portfolio_value=1_000_000,
        valuation_date=VALUATION_DATE, equity_broker=FakeEquityBroker(),
        options_broker=None, earnings_calendar=FakeEarningsCalendar(), cal=TradingCalendar("NYSE"),
        dividend_yields={}, cfg=DecisionConfig(), redistribute_fn=_redistribute_fn_factory(adv20),
    )
    assert skips["BBB"].gate == "volatility_ceiling"


def test_earnings_window_skip_gate():
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    df = _make_df(tickers, [1, 2, 3, 4, 5])
    w = pd.Series([0.2] * 5, index=tickers)
    sigma = pd.Series([0.20] * 5, index=tickers)
    adv20 = pd.Series([10_000_000] * 5, index=tickers)
    spot = pd.Series([SPOT] * 5, index=tickers)
    cal = TradingCalendar("NYSE")
    near_earnings = cal.next_session(VALUATION_DATE)  # 1 session after valuation_date
    earnings_calendar = FakeEarningsCalendar(per_ticker={"AAA": near_earnings})  # only AAA is near earnings

    _survivors, _decisions, skips = compute_instrument_decisions(
        df=df, w_constrained=w, sigma=sigma, adv20=adv20, spot=spot, portfolio_value=1_000_000,
        valuation_date=VALUATION_DATE, equity_broker=FakeEquityBroker(),
        options_broker=None, earnings_calendar=earnings_calendar, cal=cal,
        dividend_yields={}, cfg=DecisionConfig(), redistribute_fn=_redistribute_fn_factory(adv20),
    )
    assert skips["AAA"].gate == "earnings_window"
    assert len(skips) == 1


def test_max_skip_fraction_aborts_the_run():
    tickers = [f"T{i}" for i in range(10)]
    df = _make_df(tickers, list(range(1, 11)))
    w = pd.Series([0.1] * 10, index=tickers)
    sigma = pd.Series([0.20] * 10, index=tickers)
    # 5/10 = 50% fail liquidity, above the default 40% MAX_SKIP_FRACTION.
    adv20 = pd.Series([1_000_000] * 5 + [10_000_000] * 5, index=tickers)
    spot = pd.Series([SPOT] * 10, index=tickers)

    with pytest.raises(OptionsLayerError, match="MAX_SKIP_FRACTION"):
        compute_instrument_decisions(
            df=df, w_constrained=w, sigma=sigma, adv20=adv20, spot=spot, portfolio_value=1_000_000,
            valuation_date=VALUATION_DATE, equity_broker=FakeEquityBroker(),
            options_broker=None, earnings_calendar=FakeEarningsCalendar(), cal=TradingCalendar("NYSE"),
            dividend_yields={}, cfg=DecisionConfig(), redistribute_fn=_redistribute_fn_factory(adv20),
        )


# --- system-spec.md S15.5: provider failures must not crash the run ------

def test_provider_exception_falls_back_to_shares_not_a_crash():
    result = decide_instrument_for_name(
        ticker="TEST", ranking=10, expected_horizon_days=45, weight=0.05, sigma_realised=SIGMA_REALISED,
        spot=SPOT, portfolio_value=1_000_000, valuation_date=VALUATION_DATE,
        options_broker=RaisingOptionsBroker(), earnings_calendar=FakeEarningsCalendar(),
        div_yield=0.0, cfg=DecisionConfig(),
    )
    assert result.instrument == "shares"
    assert "options decision failed unexpectedly" in result.reason
    assert "simulated provider outage" in result.reason


def test_all_names_failing_logs_a_provider_outage_warning(capsys):
    tickers = ["AAA", "BBB", "CCC"]
    df = _make_df(tickers, [6, 7, 8])  # all eligible for options (rank 6-15)
    w = pd.Series([0.4, 0.3, 0.3], index=tickers)
    sigma = pd.Series([0.20] * 3, index=tickers)
    adv20 = pd.Series([10_000_000] * 3, index=tickers)
    spot = pd.Series([SPOT] * 3, index=tickers)

    survivors, decisions, _skips = compute_instrument_decisions(
        df=df, w_constrained=w, sigma=sigma, adv20=adv20, spot=spot, portfolio_value=1_000_000,
        valuation_date=VALUATION_DATE, equity_broker=FakeEquityBroker(),
        options_broker=RaisingOptionsBroker(), earnings_calendar=FakeEarningsCalendar(), cal=TradingCalendar("NYSE"),
        dividend_yields={}, cfg=DecisionConfig(), redistribute_fn=_redistribute_fn_factory(adv20),
    )

    assert all(d.instrument == "shares" for d in decisions.values())  # equity-only continuation, not a crash
    assert survivors.sum() == pytest.approx(1.0)
    captured = capsys.readouterr()
    assert "options provider appears to be down" in captured.out


def test_one_name_failing_does_not_trigger_the_outage_warning(capsys):
    """A single bad ticker is an ordinary S5.7 fallback, not a provider
    outage - the loud warning should only fire when *every* eligible name
    fails the same way."""
    expiry = VALUATION_DATE + timedelta(days=45)
    tickers = ["AAA", "BBB"]
    df = _make_df(tickers, [6, 7])
    w = pd.Series([0.5, 0.5], index=tickers)
    sigma = pd.Series([SIGMA_REALISED] * 2, index=tickers)
    adv20 = pd.Series([10_000_000] * 2, index=tickers)
    spot = pd.Series([SPOT] * 2, index=tickers)

    class MixedBroker(OptionsBroker):
        def chain(self, symbol, expiry_range):
            if symbol == "AAA":
                raise RuntimeError("boom")
            return _put_chain_for_rich_iv(expiry)  # BBB gets a normal chain

        def submit_option(self, contract, qty, side):
            raise NotImplementedError

        def option_positions(self):
            return []

        def buying_power_reserved(self):
            return 0

    _survivors, decisions, _skips = compute_instrument_decisions(
        df=df, w_constrained=w, sigma=sigma, adv20=adv20, spot=spot, portfolio_value=1_000_000,
        valuation_date=VALUATION_DATE, equity_broker=FakeEquityBroker(),
        options_broker=MixedBroker(), earnings_calendar=FakeEarningsCalendar(), cal=TradingCalendar("NYSE"),
        dividend_yields={}, cfg=DecisionConfig(), redistribute_fn=_redistribute_fn_factory(adv20),
    )

    assert "options decision failed unexpectedly" in decisions["AAA"].reason
    assert decisions["BBB"].instrument == "short_put"
    captured = capsys.readouterr()
    assert "options provider appears to be down" not in captured.out
