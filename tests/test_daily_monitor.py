"""Daily monitoring alert logic - system-spec.md S8.3, S9.2, S9.3.

Pure functions, no network - the live smoke test (`python daily_monitor.py
--options-enabled`) confirmed the orchestration wiring against a real
account; these confirm each alert actually fires (and doesn't) on the
conditions it's supposed to.
"""

from datetime import date, timedelta

from brokers.base import OptionPosition, Position
from daily_monitor import (
    ASSIGNMENT_DELTA_THRESHOLD,
    DELTA_DRIFT_THRESHOLD,
    REGIME_DAILY_ALERT_THRESHOLD,
    ROLL_DTE_THRESHOLD,
    STOP_LOSS_PCT,
    check_assignment_risk,
    check_delta_drift,
    check_dte,
    check_regime_move,
    check_stop_loss,
)
from trading_calendar import TradingCalendar

VALUATION_DATE = date(2024, 1, 2)
CAL = TradingCalendar("NYSE")


def _option_position(**overrides):
    defaults = dict(
        occ_symbol="TEST240315P00050000", underlying="TEST", contract_type="put",
        strike=50.0, expiry=VALUATION_DATE + timedelta(days=45), qty=-1, market_value=-100.0,
    )
    defaults.update(overrides)
    return OptionPosition(**defaults)


def test_dte_far_out_no_alert():
    pos = _option_position(expiry=VALUATION_DATE + timedelta(days=45))
    assert check_dte(pos, CAL, VALUATION_DATE) == []


def test_dte_at_threshold_alerts():
    # 21 calendar days out lands under 21 *trading* days (weekends in
    # between), so this is comfortably past the threshold either way.
    pos = _option_position(expiry=VALUATION_DATE + timedelta(days=ROLL_DTE_THRESHOLD))
    alerts = check_dte(pos, CAL, VALUATION_DATE)
    assert any(a.category == "dte_threshold" for a in alerts)


def test_dte_expiry_week_alerts_both():
    pos = _option_position(expiry=VALUATION_DATE + timedelta(days=3))
    alerts = check_dte(pos, CAL, VALUATION_DATE)
    categories = {a.category for a in alerts}
    assert "dte_threshold" in categories
    assert "expiry_week" in categories


def test_assignment_risk_ignores_long_positions():
    pos = _option_position(qty=1)  # long, not short
    assert check_assignment_risk(pos, current_delta=0.95, ex_div_date=None, valuation_date=VALUATION_DATE) == []


def test_assignment_risk_ignores_shallow_delta():
    pos = _option_position(qty=-1, contract_type="call")
    alerts = check_assignment_risk(pos, current_delta=0.5, ex_div_date=None, valuation_date=VALUATION_DATE)
    assert alerts == []


def test_assignment_risk_deep_itm_short_put_no_dividend_flag():
    """Only calls get the dividend-capture-specific flag - a deep ITM
    short put still gets the general deep-ITM note."""
    pos = _option_position(qty=-1, contract_type="put")
    alerts = check_assignment_risk(pos, current_delta=-0.95, ex_div_date=VALUATION_DATE + timedelta(days=5), valuation_date=VALUATION_DATE)
    categories = {a.category for a in alerts}
    assert "deep_itm_short" in categories
    assert "assignment_dividend_capture" not in categories


def test_assignment_risk_deep_itm_short_call_with_ex_div_before_expiry():
    pos = _option_position(qty=-1, contract_type="call", expiry=VALUATION_DATE + timedelta(days=10))
    alerts = check_assignment_risk(
        pos, current_delta=0.95, ex_div_date=VALUATION_DATE + timedelta(days=3), valuation_date=VALUATION_DATE,
    )
    categories = {a.category for a in alerts}
    assert "deep_itm_short" in categories
    assert "assignment_dividend_capture" in categories


def test_assignment_risk_deep_itm_short_call_ex_div_after_expiry_no_dividend_flag():
    pos = _option_position(qty=-1, contract_type="call", expiry=VALUATION_DATE + timedelta(days=10))
    alerts = check_assignment_risk(
        pos, current_delta=0.95, ex_div_date=VALUATION_DATE + timedelta(days=30), valuation_date=VALUATION_DATE,
    )
    categories = {a.category for a in alerts}
    assert "deep_itm_short" in categories
    assert "assignment_dividend_capture" not in categories


def test_delta_drift_within_band_no_alert():
    pos = _option_position(contract_type="put")
    assert check_delta_drift(pos, current_delta=-0.30 + (DELTA_DRIFT_THRESHOLD - 0.01)) == []


def test_delta_drift_beyond_band_alerts():
    pos = _option_position(contract_type="put")
    alerts = check_delta_drift(pos, current_delta=-0.80)
    assert len(alerts) == 1
    assert alerts[0].category == "delta_drift"


def test_delta_drift_none_delta_no_alert():
    pos = _option_position()
    assert check_delta_drift(pos, current_delta=None) == []


def test_stop_loss_above_threshold_no_alert():
    pos = Position(symbol="AAA", qty=10, market_value=1000, unrealized_plpc=-0.05)
    assert check_stop_loss(pos) == []


def test_stop_loss_below_threshold_alerts():
    pos = Position(symbol="AAA", qty=10, market_value=800, unrealized_plpc=STOP_LOSS_PCT - 0.01)
    alerts = check_stop_loss(pos)
    assert len(alerts) == 1
    assert alerts[0].category == "stop_loss"


def test_stop_loss_none_plpc_no_alert():
    pos = Position(symbol="AAA", qty=10, market_value=1000, unrealized_plpc=None)
    assert check_stop_loss(pos) == []


def test_regime_move_cold_start_no_alert():
    assert check_regime_move(prev_k_regime=None, new_k_regime=0.9) == []


def test_regime_move_small_move_no_alert():
    assert check_regime_move(prev_k_regime=0.90, new_k_regime=0.85) == []


def test_regime_move_large_move_alerts():
    alerts = check_regime_move(prev_k_regime=0.90, new_k_regime=0.90 - REGIME_DAILY_ALERT_THRESHOLD - 0.01)
    assert len(alerts) == 1
    assert alerts[0].category == "regime_move"
