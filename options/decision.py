"""Per-name instrument decision - system-spec.md S5.0-S5.8.

Four outcomes per name: buy shares (default), sell a cash-secured put, buy
a call, or skip the name entirely. The skip outcome is a real decision, not
a fallback - weight freed by a skip is redistributed across survivors by
re-running the S4.2 constraint loop (apply_constraints, reused from
position_sizing.py via the redistribute_fn callback rather than duplicated
here, to avoid a circular import between the two modules).

Every name gets a logged outcome and reason every run, including skips and
shares-by-default (S5.0's explicit logging requirement) - callers should
persist the returned decisions dict as-is.

Three of S5.8's seven gates are enforced upstream, not here, and are noted
rather than re-implemented:
  - weight floor: apply_constraints() already drops sub-floor names before
    w_constrained reaches this module, so they're simply absent from it.
  - data quality: bars_to_frames() already aborts the whole run on a gap.
  - tradability: validate() already aborts the whole run on a lookup
    failure/non-tradable flag, before any of this runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

import pandas as pd

from brokers.base import EquityBroker, OptionsBroker
from earnings.base import EarningsCalendar
from trading_calendar import TradingCalendar

from .chain import fetch_filtered_chain
from .pricing import delta as bs_delta, implied_vol, select_atm_iv
from .sizing import capital_reserved_for_short_put, contracts_for_long_call, contracts_for_short_put


class OptionsLayerError(Exception):
    """S5.8's MAX_SKIP_FRACTION guard rail. Caller (position_sizing.py)
    treats this like a ValidationError: abort, alert, submit zero orders."""


@dataclass
class DecisionConfig:
    min_adv: float = 5_000_000
    max_sigma: float = 1.00
    max_skip_fraction: float = 0.40
    iv_rich_threshold: float = 1.25
    iv_cheap_threshold: float = 0.85
    target_put_delta: float = -0.30
    target_call_delta: float = 0.60
    min_dte: int = 21
    max_dte: int = 90
    expiry_tolerance_days: int = 14
    delta_tolerance: float = 0.10
    min_surviving_contracts: int = 4
    earnings_skip_days: int = 2
    rate: float = 0.04


@dataclass
class InstrumentDecision:
    ticker: str
    instrument: str          # "shares", "short_put", "long_call"
    reason: str
    iv_ratio: Optional[float] = None
    expiry: Optional[date] = None
    strike: Optional[float] = None
    delta: Optional[float] = None
    contracts: Optional[int] = None
    premium: Optional[float] = None
    capital_reserved: Optional[float] = None
    occ_symbol: Optional[str] = None


@dataclass
class SkipDecision:
    ticker: str
    gate: str
    reason: str
    value: Optional[str] = None


def _permitted_instruments(ranking: int) -> set:
    """S5.3."""
    if ranking <= 5:
        return {"shares"}
    if ranking <= 15:
        return {"shares", "short_put", "long_call"}
    return {"shares", "short_put"}


def compute_skip_gates(
    w_constrained: pd.Series,
    sigma: pd.Series,
    adv20: pd.Series,
    equity_broker: EquityBroker,
    earnings_calendar: EarningsCalendar,
    cal: TradingCalendar,
    valuation_date: date,
    cfg: DecisionConfig,
) -> dict:
    """The four S5.8 gates not already enforced upstream (see module
    docstring). Distinct from S5.7's per-instrument fallback - these mean
    "do not buy the name in any form"."""
    skips: dict = {}

    for ticker in w_constrained.index:
        adv = adv20.get(ticker)
        if adv is not None and adv < cfg.min_adv:
            skips[ticker] = SkipDecision(ticker, "liquidity", f"adv20 {adv:,.0f} < MIN_ADV {cfg.min_adv:,.0f}", str(adv))
            continue

        sig = sigma.get(ticker)
        if sig is not None and sig > cfg.max_sigma:
            skips[ticker] = SkipDecision(ticker, "volatility_ceiling", f"sigma {sig:.2f} > MAX_SIGMA {cfg.max_sigma:.2f}", str(sig))
            continue

        try:
            note = equity_broker.pending_corporate_action(ticker)
        except Exception:
            note = None  # best-effort (system-spec.md S5.8): unknown, not gated
        if note:
            skips[ticker] = SkipDecision(ticker, "corporate_action", note)
            continue

        next_earn = earnings_calendar.next_earnings_date(ticker)
        if next_earn is not None and next_earn >= valuation_date:
            sessions_ahead = cal.expected_sessions(valuation_date, next_earn)
            if sessions_ahead <= cfg.earnings_skip_days:
                skips[ticker] = SkipDecision(
                    ticker, "earnings_window",
                    f"earnings on {next_earn} is {sessions_ahead} trading day(s) away (<= {cfg.earnings_skip_days})",
                    str(next_earn),
                )
                continue

    return skips


def _select_expiry(expiries: list, expected_horizon_days: float, valuation_date: date, cfg: DecisionConfig):
    """S5.4."""
    if not expiries:
        return None
    target_dte = min(max(expected_horizon_days, cfg.min_dte), cfg.max_dte)
    best = min(expiries, key=lambda e: abs((e - valuation_date).days - target_dte))
    best_dte = (best - valuation_date).days
    if abs(best_dte - target_dte) > cfg.expiry_tolerance_days:
        return None
    return best


def _select_strike_by_delta(contracts_at_expiry, contract_type, target_delta, spot, valuation_date, rate, div_yield, tolerance):
    """S5.5. Solves each candidate's own IV from its own mid rather than
    using a single flat ATM vol across strikes, so delta reflects that
    strike's actual market price rather than an ATM-vol approximation."""
    candidates = [c for c in contracts_at_expiry if c.contract_type == contract_type]
    best = None
    best_diff = None
    for c in candidates:
        iv = implied_vol(contract_type, c.mid, spot, c.strike, c.expiry, valuation_date, rate, div_yield)
        if iv is None:
            continue
        d = bs_delta(contract_type, spot, c.strike, c.expiry, valuation_date, rate, div_yield, iv)
        diff = abs(d - target_delta)
        if best_diff is None or diff < best_diff:
            best_diff, best = diff, (c, d, iv)
    if best is None or best_diff > tolerance:
        return None
    return best  # (Contract, computed_delta, computed_iv)


def decide_instrument_for_name(
    ticker: str,
    ranking: int,
    expected_horizon_days: float,
    weight: float,
    sigma_realised: float,
    spot: float,
    portfolio_value: float,
    valuation_date: date,
    options_broker: Optional[OptionsBroker],
    earnings_calendar: EarningsCalendar,
    div_yield: float,
    cfg: DecisionConfig,
) -> InstrumentDecision:
    """Never lets an options-provider failure crash the run (system-spec.md
    S15.5: "chain fetch fails for one name -> fall back to shares... for
    all names -> alert, run equity-only, continue"). Any exception from the
    chain/pricing calls below - a network error, an unapproved account, a
    provider outage - becomes a shares fallback with the error as the
    reason, same as any other S5.7 fallback. compute_instrument_decisions()
    checks whether *every* name failed this way and raises a distinct,
    louder alert for that case - a provider-wide outage is a different
    problem than one bad ticker."""
    try:
        return _decide_instrument_for_name_impl(
            ticker, ranking, expected_horizon_days, weight, sigma_realised, spot,
            portfolio_value, valuation_date, options_broker, earnings_calendar, div_yield, cfg,
        )
    except Exception as e:
        return InstrumentDecision(ticker, "shares", f"options decision failed unexpectedly: {e!r}")


def _decide_instrument_for_name_impl(
    ticker: str,
    ranking: int,
    expected_horizon_days: float,
    weight: float,
    sigma_realised: float,
    spot: float,
    portfolio_value: float,
    valuation_date: date,
    options_broker: Optional[OptionsBroker],
    earnings_calendar: EarningsCalendar,
    div_yield: float,
    cfg: DecisionConfig,
) -> InstrumentDecision:
    if options_broker is None:
        return InstrumentDecision(ticker, "shares", "OPTIONS_ENABLED is False")

    permitted = _permitted_instruments(ranking)
    if permitted == {"shares"}:
        return InstrumentDecision(ticker, "shares", f"rank {ranking} <= 5: shares only (S5.3)")

    chain_result = fetch_filtered_chain(
        options_broker, ticker, valuation_date,
        min_dte=cfg.min_dte, max_dte=cfg.max_dte, min_surviving_contracts=cfg.min_surviving_contracts,
    )
    if chain_result.skip_reason:
        return InstrumentDecision(ticker, "shares", chain_result.skip_reason)

    expiry = _select_expiry(chain_result.expiries, expected_horizon_days, valuation_date, cfg)
    if expiry is None:
        target_dte = min(max(expected_horizon_days, cfg.min_dte), cfg.max_dte)
        return InstrumentDecision(
            ticker, "shares",
            f"no listed expiry within +/-{cfg.expiry_tolerance_days}d of target {target_dte:.0f}DTE",
        )

    contracts_at_expiry = chain_result.contracts_by_expiry[expiry]

    atm_result = select_atm_iv(contracts_at_expiry, spot, valuation_date, cfg.rate, div_yield)
    if atm_result.iv is None:
        return InstrumentDecision(ticker, "shares", f"ATM IV solve failed: {atm_result.note}")

    iv_ratio = atm_result.iv / sigma_realised
    if iv_ratio > cfg.iv_rich_threshold:
        raw_instrument = "short_put"
    elif iv_ratio < cfg.iv_cheap_threshold:
        raw_instrument = "long_call"
    else:
        raw_instrument = "shares"

    if raw_instrument not in permitted:
        return InstrumentDecision(
            ticker, "shares",
            f"iv_ratio={iv_ratio:.2f} signals {raw_instrument}, not permitted for rank {ranking} (S5.3)",
            iv_ratio=iv_ratio,
        )

    if raw_instrument == "shares":
        return InstrumentDecision(ticker, "shares", f"iv_ratio={iv_ratio:.2f} - no edge ({atm_result.note})", iv_ratio=iv_ratio)

    contract_type = "put" if raw_instrument == "short_put" else "call"
    target_delta = cfg.target_put_delta if raw_instrument == "short_put" else cfg.target_call_delta

    selected = _select_strike_by_delta(
        contracts_at_expiry, contract_type, target_delta, spot, valuation_date, cfg.rate, div_yield, cfg.delta_tolerance,
    )
    if selected is None:
        return InstrumentDecision(
            ticker, "shares",
            f"no strike within +/-{cfg.delta_tolerance} delta of target {target_delta} at {expiry}",
            iv_ratio=iv_ratio,
        )
    contract, computed_delta, computed_iv = selected

    # S5.7: earnings before expiry excludes short puts specifically - IV
    # inflates into the print and collapses after, which iv_ratio misreads
    # as richness.
    if raw_instrument == "short_put":
        next_earn = earnings_calendar.next_earnings_date(ticker)
        if next_earn is not None and valuation_date <= next_earn < expiry:
            return InstrumentDecision(
                ticker, "shares",
                f"earnings ({next_earn}) fall before expiry ({expiry}) - excluded from short puts (S5.7)",
                iv_ratio=iv_ratio,
            )

    target_notional = weight * portfolio_value
    if raw_instrument == "long_call":
        contracts = contracts_for_long_call(target_notional, computed_delta, spot)
    else:
        contracts = contracts_for_short_put(target_notional, contract.strike)

    if contracts < 1:
        return InstrumentDecision(
            ticker, "shares",
            f"{raw_instrument} sizing rounds to 0 contracts (target notional ${target_notional:,.0f})",
            iv_ratio=iv_ratio, expiry=expiry, strike=contract.strike, delta=computed_delta,
        )

    capital_reserved = capital_reserved_for_short_put(contract.strike, contracts) if raw_instrument == "short_put" else None
    premium = contract.mid * contracts * 100

    return InstrumentDecision(
        ticker=ticker,
        instrument=raw_instrument,
        reason=f"iv_ratio={iv_ratio:.2f} vs thresholds [{cfg.iv_cheap_threshold},{cfg.iv_rich_threshold}]; {atm_result.note}",
        iv_ratio=iv_ratio,
        expiry=expiry,
        strike=contract.strike,
        delta=computed_delta,
        contracts=contracts,
        premium=premium,
        capital_reserved=capital_reserved,
        occ_symbol=contract.occ_symbol,
    )


def compute_instrument_decisions(
    df: pd.DataFrame,
    w_constrained: pd.Series,
    sigma: pd.Series,
    adv20: pd.Series,
    spot: pd.Series,
    portfolio_value: float,
    valuation_date: date,
    equity_broker: EquityBroker,
    options_broker: Optional[OptionsBroker],
    earnings_calendar: EarningsCalendar,
    cal: TradingCalendar,
    dividend_yields: dict,
    cfg: DecisionConfig,
    redistribute_fn: Callable[[pd.Series], tuple],
):
    """Orchestrates S5.0-S5.8 end to end. redistribute_fn(w) must behave
    like position_sizing.apply_constraints(w, adv20, V, cfg) restricted to
    w's own index, returning (w_final, dropped, bound_log) - passed in by
    the caller rather than imported, to avoid a circular import between
    this module and position_sizing.py.

    Returns (survivors_weight, decisions, skips).
    """
    skips = compute_skip_gates(w_constrained, sigma, adv20, equity_broker, earnings_calendar, cal, valuation_date, cfg)

    skip_fraction = len(skips) / len(df) if len(df) else 0.0
    if skip_fraction > cfg.max_skip_fraction:
        raise OptionsLayerError(
            f"{len(skips)}/{len(df)} names ({skip_fraction:.0%}) failed S5.8 skip gates, "
            f"exceeding MAX_SKIP_FRACTION ({cfg.max_skip_fraction:.0%}). Aborting - this indicates "
            f"a data problem or broken upstream file, not genuinely unsuitable stocks."
        )

    survivors_w = w_constrained.drop(index=[t for t in skips if t in w_constrained.index])
    if skips and not survivors_w.empty:
        renormalised = survivors_w / survivors_w.sum()
        survivors_w, _dropped2, _bound_log2 = redistribute_fn(renormalised)

    decisions: dict = {}
    d = df.set_index("ticker")
    for ticker in survivors_w.index:
        decisions[ticker] = decide_instrument_for_name(
            ticker=ticker,
            ranking=int(d.loc[ticker, "ranking"]),
            expected_horizon_days=float(d.loc[ticker, "expected_horizon_days"]),
            weight=float(survivors_w[ticker]),
            sigma_realised=float(sigma[ticker]),
            spot=float(spot[ticker]),
            portfolio_value=portfolio_value,
            valuation_date=valuation_date,
            options_broker=options_broker,
            earnings_calendar=earnings_calendar,
            div_yield=dividend_yields.get(ticker, 0.0),
            cfg=cfg,
        )

    # system-spec.md S15.5: "chain fetch fails for all names -> alert, run
    # equity-only for the week, continue" - distinguished from ordinary
    # per-name S5.7 fallbacks (thin chain, no matching expiry, etc.) by
    # checking whether *every* name eligible for an options expression hit
    # the exception path in decide_instrument_for_name, which only happens
    # for genuine failures (network, auth, provider outage), never for a
    # normal business-logic fallback.
    if options_broker is not None:
        eligible = [t for t in decisions if _permitted_instruments(int(d.loc[t, "ranking"])) != {"shares"}]
        failed = [t for t in eligible if decisions[t].reason.startswith("options decision failed unexpectedly")]
        if eligible and len(failed) == len(eligible):
            print(
                f"  WARNING: options decision failed for all {len(eligible)} eligible name(s) - "
                f"the options provider appears to be down or misconfigured, not a per-name data issue. "
                f"Continuing equity-only for this run (S15.5). First failure: {decisions[failed[0]].reason}"
            )

    return survivors_w, decisions, skips
