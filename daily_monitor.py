"""
Daily monitoring job - system-spec.md S8.3, S9.2, S9.3.

Alert-only, by explicit choice: this never submits an order on its own -
including the mechanical 21-DTE close the spec allows to be automatic. It
is the "what needs your attention" report for the days between weekly
rebalances, not a second trader. Run it once per trading day, pre-market;
on a non-trading day it logs that and exits cleanly (matches the same
guard position_sizing.py uses).

Checks performed:
  - S8.3 assignment risk: a short option at |delta| > ASSIGNMENT_DELTA_THRESHOLD
    is deep enough ITM to carry real early-assignment risk. The spec's
    specific example - a short CALL exercised early to capture a dividend -
    is flagged separately when an upcoming ex-dividend date falls before
    expiry. Note this system currently only ever opens short PUTS and long
    calls (system-spec.md S5.0) - never a short call - so the dividend-
    capture case can't actually fire today. Kept because the spec names it
    explicitly and it costs nothing to have ready for when covered calls
    (a natural future addition - see options/decision.py's module notes)
    start opening short calls for real.
  - S8.3 21-DTE: options at or under MIN_DTE need closing or rolling.
    Alert only - no auto-action, per this build's explicit scope.
  - S8.3 expiry week: options expiring within EXPIRY_WEEK_DAYS trading days.
  - S9.2 delta drift: current delta vs this system's own S5.5 target delta
    band (-0.30 puts / +0.60 calls) - a position that's drifted far from
    its entry target signals the underlying has moved a lot since entry.
    There's no persisted "delta at entry" to diff against directly, so
    this compares against the target band instead - a reasonable proxy,
    not an exact reproduction of "drift since open".
  - S9.2/S9.3 regime: recomputes distance/k_regime daily (regime-spec.md S4.1
    requires this for the 3-day confirmation counter to mean anything even
    though rebalancing itself is weekly), and alerts if the day-over-day
    k_regime move exceeds REGIME_DAILY_ALERT_THRESHOLD (S9.3's named
    trigger, 0.10).
  - S9.2 stop-loss: equity positions with unrealized P&L below
    STOP_LOSS_PCT (Alpaca's own unrealized_plpc, since cost basis). The
    spec names this check in S9.2's bullet list but never defines a
    threshold or reference point anywhere in system-spec.md or
    position-sizing-spec.md - -15% is this build's chosen default, not a
    spec-pinned value. Not applied to options: "stop-loss" for a short put
    or long call isn't a standard, spec-defined concept the way it is for
    a share, so it's left out rather than inventing a definition.

Usage:
    python daily_monitor.py                    # equity positions only
    python daily_monitor.py --options-enabled   # also check option positions
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from brokers.alpaca_equity import AlpacaEquityBroker
from brokers.base import EquityBroker, OptionPosition, OptionsBroker, Position
from data.alpaca_data import AlpacaMarketData
from trading_calendar import TradingCalendar

load_dotenv()

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

ROLL_DTE_THRESHOLD = 21
EXPIRY_WEEK_DAYS = 5
ASSIGNMENT_DELTA_THRESHOLD = 0.90
DELTA_DRIFT_THRESHOLD = 0.20     # this build's default - not spec-pinned
STOP_LOSS_PCT = -0.15            # this build's default - not spec-pinned (see module docstring)
REGIME_DAILY_ALERT_THRESHOLD = 0.10
TARGET_PUT_DELTA = -0.30
TARGET_CALL_DELTA = 0.60


@dataclass
class Alert:
    severity: str      # "info" or "warning"
    category: str
    symbol: str
    message: str


def check_dte(pos: OptionPosition, cal: TradingCalendar, valuation_date) -> list:
    dte = cal.expected_sessions(valuation_date, pos.expiry) if pos.expiry >= valuation_date else 0
    alerts = []
    if dte <= ROLL_DTE_THRESHOLD:
        alerts.append(Alert(
            "warning", "dte_threshold", pos.occ_symbol,
            f"{dte} trading day(s) to expiry ({pos.expiry}) - at or under the {ROLL_DTE_THRESHOLD}-DTE "
            f"threshold; close or roll (S8.3) - not done automatically, this is a flag only",
        ))
    elif dte <= EXPIRY_WEEK_DAYS + ROLL_DTE_THRESHOLD:
        pass  # not yet actionable, nothing to say
    if dte <= EXPIRY_WEEK_DAYS:
        alerts.append(Alert(
            "warning", "expiry_week", pos.occ_symbol,
            f"expires within {EXPIRY_WEEK_DAYS} trading days ({pos.expiry}, {dte}TD out)",
        ))
    return alerts


def check_assignment_risk(pos: OptionPosition, current_delta: Optional[float], ex_div_date, valuation_date) -> list:
    alerts = []
    if pos.qty >= 0 or current_delta is None:
        return alerts  # only short positions carry assignment risk
    if abs(current_delta) <= ASSIGNMENT_DELTA_THRESHOLD:
        return alerts

    alerts.append(Alert(
        "warning", "deep_itm_short", pos.occ_symbol,
        f"deep ITM short {pos.contract_type} (delta={current_delta:.2f}), assignment probability is high",
    ))
    if pos.contract_type == "call" and ex_div_date is not None and valuation_date <= ex_div_date < pos.expiry:
        alerts.append(Alert(
            "warning", "assignment_dividend_capture", pos.occ_symbol,
            f"short call, deep ITM, ex-dividend {ex_div_date} falls before expiry {pos.expiry} - "
            f"classic early-exercise-for-dividend risk (S8.3)",
        ))
    return alerts


def check_delta_drift(pos: OptionPosition, current_delta: Optional[float]) -> list:
    if current_delta is None:
        return []
    target = TARGET_PUT_DELTA if pos.contract_type == "put" else TARGET_CALL_DELTA
    drift = abs(current_delta - target)
    if drift <= DELTA_DRIFT_THRESHOLD:
        return []
    return [Alert(
        "info", "delta_drift", pos.occ_symbol,
        f"current delta {current_delta:.2f} vs this system's {target:+.2f} target band "
        f"(drift {drift:.2f}) - underlying has likely moved materially since entry",
    )]


def check_stop_loss(pos: Position) -> list:
    if pos.unrealized_plpc is None or pos.unrealized_plpc >= STOP_LOSS_PCT:
        return []
    return [Alert(
        "warning", "stop_loss", pos.symbol,
        f"unrealized P&L {pos.unrealized_plpc:+.1%} is below the {STOP_LOSS_PCT:.0%} stop-loss threshold",
    )]


def check_regime_move(prev_k_regime: Optional[float], new_k_regime: float) -> list:
    if prev_k_regime is None:
        return []
    move = abs(new_k_regime - prev_k_regime)
    if move <= REGIME_DAILY_ALERT_THRESHOLD:
        return []
    return [Alert(
        "warning", "regime_move", "MARKET",
        f"k_regime moved {new_k_regime - prev_k_regime:+.2f} in a day ({prev_k_regime:.2f} -> "
        f"{new_k_regime:.2f}) - exceeds the {REGIME_DAILY_ALERT_THRESHOLD} S9.3 event-driven trigger",
    )]


def compute_live_delta(options_broker: OptionsBroker, market_data, pos: OptionPosition, rate: float, div_yield: float, valuation_date):
    """Solves the position's current delta from a fresh quote, rather than
    trusting a stale value from whenever it was opened - same philosophy
    as options/decision.py's strike selection."""
    from options.pricing import delta as bs_delta, implied_vol

    try:
        contract = options_broker.latest_contract_quote(pos.occ_symbol)
        spot = market_data.latest_quote(pos.underlying).mid
        iv = implied_vol(pos.contract_type, contract.mid, spot, pos.strike, pos.expiry, valuation_date, rate, div_yield)
        if iv is None:
            return None
        return bs_delta(pos.contract_type, spot, pos.strike, pos.expiry, valuation_date, rate, div_yield, iv)
    except Exception:
        return None


def run_daily_monitor(equity_broker: EquityBroker, options_broker: Optional[OptionsBroker], market_data, cal: TradingCalendar, valuation_date):
    alerts: list[Alert] = []

    # S9.2/S9.3: regime, recomputed daily regardless of whether options are enabled.
    from position_sizing import Config, compute_regime, fetch_benchmark_close, load_regime_state, REGIME_STATE_PATH

    cfg = Config()
    prev_state = load_regime_state(REGIME_STATE_PATH)
    prev_k_regime = float(prev_state["k_regime"]) if prev_state else None

    regime_result = None
    try:
        benchmark_close, benchmark_used = fetch_benchmark_close(market_data, cfg, cal)
        regime_result = compute_regime(benchmark_close, benchmark_used, None, cfg, cal)
        alerts.extend(check_regime_move(prev_k_regime, regime_result["k_regime"]))
    except Exception as e:
        alerts.append(Alert("warning", "regime_check_failed", "MARKET", str(e)))

    # S9.2: stop-loss on equity positions.
    for pos in equity_broker.positions():
        alerts.extend(check_stop_loss(pos))

    # S8.3/S9.2: option-specific checks.
    option_alerts_by_symbol = {}
    if options_broker is not None:
        from options.rates import fetch_dividend_yield, fetch_next_ex_dividend_date, fetch_risk_free_rate

        rate, _rate_note = fetch_risk_free_rate()
        positions = options_broker.option_positions()
        for pos in positions:
            pos_alerts = check_dte(pos, cal, valuation_date)

            current_delta = compute_live_delta(
                options_broker, market_data, pos, rate, fetch_dividend_yield(pos.underlying), valuation_date,
            )
            ex_div = fetch_next_ex_dividend_date(pos.underlying)
            if ex_div is not None and ex_div < valuation_date:
                ex_div = None  # stale/past date from the data source - not an upcoming risk
            pos_alerts.extend(check_assignment_risk(pos, current_delta, ex_div, valuation_date))
            pos_alerts.extend(check_delta_drift(pos, current_delta))

            if pos_alerts:
                option_alerts_by_symbol[pos.occ_symbol] = pos_alerts
            alerts.extend(pos_alerts)

    return alerts, regime_result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--options-enabled", action="store_true", help="Also check option positions (S8.3)")
    args = parser.parse_args()

    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: set ALPACA_API_KEY and ALPACA_SECRET_KEY (in .env or env vars).")
        sys.exit(1)

    cal = TradingCalendar("NYSE")
    today = datetime.now(timezone.utc).date()
    if not cal.is_trading_day(today):
        print(f"{today} is not a NYSE trading session - nothing to monitor today.")
        return

    equity_broker = AlpacaEquityBroker(api_key, secret_key, paper=True)
    market_data = AlpacaMarketData(api_key, secret_key)
    options_broker = None
    if args.options_enabled:
        from brokers.alpaca_options import AlpacaOptionsBroker
        options_broker = AlpacaOptionsBroker(api_key, secret_key, paper=True)

    alerts, regime_result = run_daily_monitor(equity_broker, options_broker, market_data, cal, today)

    print(f"Daily monitor - {today}")
    if regime_result:
        print(f"  k_regime={regime_result['k_regime']:.2f}  distance={regime_result['distance']:+.2%} "
              f"vs {regime_result['benchmark_used']} 200d SMA")
    if not alerts:
        print("  No alerts.")
    else:
        for a in alerts:
            tag = "WARNING" if a.severity == "warning" else "info"
            print(f"  [{tag}] {a.category:<28} {a.symbol:<24} {a.message}")

    log_path = LOG_DIR / f"monitor_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    log_path.write_text(json.dumps({
        "run_at": datetime.now(timezone.utc).isoformat(),
        "run_date": today.isoformat(),
        "options_enabled": args.options_enabled,
        "regime": regime_result,
        "alerts": [asdict(a) for a in alerts],
    }, indent=2, default=str))
    print(f"Wrote {log_path}")

    if any(a.severity == "warning" for a in alerts):
        sys.exit(2)  # non-zero so a scheduled task shows as "needs attention" without being a hard failure


if __name__ == "__main__":
    main()
