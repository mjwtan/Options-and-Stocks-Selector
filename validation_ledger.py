"""system-spec.md S16 validation ledger.

"Record every metric weekly from the first run. Retrofitting is
impossible - the data is gone once the run has passed." Most of S16.1's
schema is already computed during the weekly sizing run (risk scalars,
regime state, skip/fallback counts, MC diagnostics) - this module's job is
just to pull that data out of run_log into two permanent, append-only
CSVs at the moment it's freshest, rather than trying to reconstruct it
later:

  history/validation_ledger.csv           - one row per weekly run
  history/validation_ledger_per_name.csv  - one row per (run, ticker)

Two columns genuinely can't be filled in at write time - actual_return_1w
and equal_weight_return_1w need 1 week to actually pass, and
forward_return_1w/4w on the per-name table need 1 and 4 weeks
respectively. Those start as blank and get backfilled later (see
backfill_forward_returns() below) once enough time has elapsed - reusing
track_performance.py's existing price-fetching logic rather than
duplicating it.

Deliberately NOT built here: the S16.2 decision rules themselves (rank-
decile regression, equal-weight beat-rate, etc.). The spec is explicit
those need 12-26 weeks of real data before they mean anything - writing
analysis code against zero rows of data is guesswork, not validation.
Come back to S16.2 once history/validation_ledger.csv actually has enough
rows.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

LEDGER_COLUMNS = [
    "run_date", "portfolio_value", "cash_pct",
    "equal_weight_return_1w", "actual_return_1w",
    "sigma_p_intended", "sigma_p_realised_20d",
    "k_vol", "k_risk", "k_regime",
    "turnover_pct", "est_cost_bps",
    "n_shares", "n_calls", "n_puts", "n_skipped",
    "options_fallback_count", "skip_count_by_gate",
    "iv_crosscheck_warnings",
    "regime_crossings_ytd",
    "mc_elapsed_ms", "mc_paths", "cvar_ann",
]

PER_NAME_COLUMNS = ["run_date", "ticker", "rank", "weight", "instrument", "forward_return_1w", "forward_return_4w"]

EST_COST_BPS_PER_UNIT_TURNOVER = 10.0  # a placeholder estimate (spec names the column "est_"), not a measured cost


def _previous_week_weights(history_dir: Path, before_date) -> Optional[dict]:
    """{ticker: position_size} from the most recent archived week strictly
    before before_date, or None if there isn't one yet (first run ever)."""
    candidates = []
    for day_dir in history_dir.iterdir() if history_dir.exists() else []:
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= before_date:
            continue
        output_csvs = [p for p in day_dir.glob("*.csv") if not p.name.startswith("input_")]
        if output_csvs:
            candidates.append((day, output_csvs[0]))
    if not candidates:
        return None
    _day, path = max(candidates, key=lambda c: c[0])

    import pandas as pd
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "ticker" not in df.columns or "position_size" not in df.columns:
        return None
    return dict(zip(df["ticker"], df["position_size"]))


def _compute_turnover_pct(current_weights: dict, previous_weights: Optional[dict]) -> Optional[float]:
    if previous_weights is None:
        return None  # first run ever - no prior week to diff against
    tickers = set(current_weights) | set(previous_weights)
    return sum(abs(current_weights.get(t, 0.0) - previous_weights.get(t, 0.0)) for t in tickers) / 2.0


def _crossings_this_calendar_year(crossing_log: list) -> int:
    year = datetime.now(timezone.utc).year
    count = 0
    for ts in crossing_log:
        try:
            if datetime.fromisoformat(ts).year == year:
                count += 1
        except ValueError:
            continue
    return count


def build_ledger_row(run_log: dict, history_dir: Path, regime_state_path: Path) -> dict:
    weight_final = run_log["weight_final"]
    instrument_decisions = run_log.get("instrument_decisions", {})
    skip_decisions = run_log.get("skip_decisions", {})
    risk = run_log["risk"]
    cfg = run_log["config"]
    today = datetime.fromisoformat(run_log["run_at"]).date()

    n_shares = sum(1 for d in instrument_decisions.values() if d["instrument"] == "shares")
    n_calls = sum(1 for d in instrument_decisions.values() if d["instrument"] == "long_call")
    n_puts = sum(1 for d in instrument_decisions.values() if d["instrument"] == "short_put")
    # A "fallback" is a name that was actually eligible for an options
    # expression (rank 6-20) but landed on shares anyway (S5.7) - distinct
    # from rank 1-5 (never eligible, S5.3) or OPTIONS_ENABLED=False (the
    # whole layer off). decide_instrument_for_name()'s reason strings for
    # those two non-fallback cases are fixed prefixes; everything else that
    # ends in "shares" is a genuine S5.7 fallback.
    options_fallback_count = sum(
        1 for d in instrument_decisions.values()
        if d["instrument"] == "shares"
        and not d["reason"].startswith("rank ")
        and d["reason"] != "OPTIONS_ENABLED is False"
    )

    skip_count_by_gate: dict = {}
    for s in skip_decisions.values():
        skip_count_by_gate[s["gate"]] = skip_count_by_gate.get(s["gate"], 0) + 1

    crossing_log = []
    if regime_state_path.exists():
        try:
            crossing_log = json.loads(regime_state_path.read_text()).get("crossing_log", [])
        except (json.JSONDecodeError, OSError):
            crossing_log = []

    previous_weights = _previous_week_weights(history_dir, today)
    turnover_pct = _compute_turnover_pct(weight_final, previous_weights)

    sigma_p_analytic = risk["sigma_p_analytic"]
    k_vol_derived = min(1.0, cfg["sigma_target"] / sigma_p_analytic) if sigma_p_analytic else None

    return {
        "run_date": today.isoformat(),
        "portfolio_value": run_log["portfolio_value"],
        "cash_pct": run_log["cash_pct"],
        "equal_weight_return_1w": None,   # backfilled once 1 week has passed
        "actual_return_1w": None,          # backfilled once 1 week has passed
        "sigma_p_intended": cfg["sigma_target"],
        "sigma_p_realised_20d": None,      # needs a rolling daily-NAV series this build doesn't track yet - see module docstring
        "k_vol": k_vol_derived,
        "k_risk": risk["k_risk"] if risk["risk_engine_used"] == "montecarlo" else None,
        "k_regime": run_log["regime"]["k_regime"],
        "turnover_pct": turnover_pct,
        "est_cost_bps": (turnover_pct * EST_COST_BPS_PER_UNIT_TURNOVER) if turnover_pct is not None else None,
        "n_shares": n_shares,
        "n_calls": n_calls,
        "n_puts": n_puts,
        "n_skipped": len(skip_decisions),
        "options_fallback_count": options_fallback_count,
        "skip_count_by_gate": json.dumps(skip_count_by_gate),
        "iv_crosscheck_warnings": len(run_log.get("cross_check_flags", {})),
        "regime_crossings_ytd": _crossings_this_calendar_year(crossing_log),
        "mc_elapsed_ms": risk["elapsed_ms"],
        "mc_paths": risk["n_paths"],
        "cvar_ann": risk["cvar_annualised"],
    }


def build_per_name_rows(run_log: dict) -> list:
    weight_final = run_log["weight_final"]
    instrument_decisions = run_log.get("instrument_decisions", {})
    today = datetime.fromisoformat(run_log["run_at"]).date().isoformat()

    rows = []
    for ticker, weight in weight_final.items():
        decision = instrument_decisions.get(ticker)
        rows.append({
            "run_date": today,
            "ticker": ticker,
            "rank": None,  # filled by the caller, which has df on hand
            "weight": weight,
            "instrument": decision["instrument"] if decision else ("skipped" if weight == 0 else "shares"),
            "forward_return_1w": None,
            "forward_return_4w": None,
        })
    return rows


def _append_rows(rows: list, path: Path, columns: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def append_ledger_row(row: dict, path: Path):
    _append_rows([row], path, LEDGER_COLUMNS)


def append_per_name_rows(rows: list, path: Path):
    _append_rows(rows, path, PER_NAME_COLUMNS)
