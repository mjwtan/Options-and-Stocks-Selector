"""
Position sizing pipeline — implements position-sizing-spec.md sections 1-7.

Turns a weekly ranked-stock CSV into target portfolio weights and writes an
output CSV with a `position_size` column (fraction of portfolio equity,
e.g. 0.095 = 9.5%) that trade_from_csv.py consumes directly.

Weekly input CSV format - exactly 20 rows. Required columns (case/spacing
insensitive, e.g. "Risk Index" == "risk_index"): ticker, ranking, regime,
risk_index, volatility_index, sentiment_index. Extra descriptive columns
(Sector, Why Included, Valuation, Bear Case, ...) are tolerated and ignored.

`regime` in this feed is a MARKET-WIDE flag (1 = benchmark above its own
200-day SMA, 0 = below) and must be uniform across all 20 rows - a file with
mixed values is malformed (see validate()). It is logged and cross-checked
but never used as a trading input: k_regime is always computed from our own
benchmark bars (regime-spec.md), since this upstream field has drifted
definition once already. There is no timestamp column, so staleness is
detected via content hash + file modification time instead (S2).

Pipeline (spec section references in comments):
    1. Validate the CSV (S2) - fail closed, exit non-zero on any problem.
    2. Pull ~300 days of daily bars for all tickers, and separately for the
       regime benchmark (with SPY fallback), from Alpaca.
    3. Compute EWMA volatility per name (S3).
    4. Compute raw ranking/vol weights, optional risk/sentiment modifiers (S4).
    5. Apply position cap / floor / liquidity constraints (S5).
    6. Ledoit-Wolf portfolio covariance -> risk targeting (S6): analytic
       volatility targeting by default, or a QuantLib Monte Carlo CVaR
       engine behind --risk-engine montecarlo (quantlib-risk-engine-spec.md,
       see risk/engine.py). Then the continuous/confirmed/rate-limited
       regime scalar (regime-spec.md).
    7. Cross-check computed vol vs supplied volatility_index (S7, log only).
    8. Write output CSV + a full JSON run log (S9).

NOT implemented here: S8 order generation (that's trade_from_csv.py) and the
top-25 exit hysteresis, which the weekly CSV's fixed ranking-1..20 contract
cannot support (there is no rank-21..25 data to check a dropped name
against) - see conversation notes.

Regime scalar implementation note: regime-spec.md's confirmation-day counter
is specified as an incremental daily state machine ("requires computing
distance daily, not only on rebalance days"). This pipeline only runs
weekly, but it already pulls ~300 days of daily benchmark closes on every
run, so instead of trusting an incrementally-persisted day counter (which
would silently go stale across a missed run), crossing_day_count is
recomputed from scratch each run by replaying the fetched daily closes. This
is mathematically equivalent to the spec's incremental version when the
series has no gaps, and is robust to missed/skipped runs. The one piece of
state that genuinely cannot be derived from price history alone -
prev_k_regime, the last *applied* (post rate-limit) scalar - is still
persisted in state/regime_state.json, per spec section 5.

Setup: same ALPACA_API_KEY / ALPACA_SECRET_KEY in .env as trade_from_csv.py.

Usage:
    python position_sizing.py weekly_input.csv --output target_positions.csv
"""

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.covariance import LedoitWolf

from brokers import resolve_providers
from brokers.base import EquityBroker, MarketData

from risk.engine import compute_risk_scalar, RiskConfig
from trading_calendar import TradingCalendar

load_dotenv()

BASE_DIR = Path(__file__).parent
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
REGIME_STATE_PATH = STATE_DIR / "regime_state.json"
INPUT_HASH_STATE_PATH = STATE_DIR / "last_input_hash.json"

REQUIRED_COLUMNS = {"ticker", "ranking", "regime", "risk_index", "volatility_index", "sentiment_index"}


class ValidationError(Exception):
    pass


@dataclass
class Config:
    sigma_target: float = 0.15
    position_cap: float = 0.12
    position_floor: float = 0.015
    min_trade_abs: float = 25.0          # consumed downstream by trade_from_csv.py
    no_trade_band: float = 0.20          # consumed downstream by trade_from_csv.py
    exit_rank: int = 25                  # NOT enforced here - see module docstring
    ewma_halflife: int = 21
    vol_floor: float = 0.12
    vol_cap: float = 0.80
    liquidity_frac: float = 0.005
    use_risk_index: bool = False
    use_sentiment_index: bool = False
    lookback_days: int = 300
    cov_window: int = 250
    max_constraint_passes: int = 5
    max_file_age_days: int = 3

    # --- regime scalar (regime-spec.md) ---
    regime_benchmark: str = "^GSPC"      # falls back to SPY if unavailable, see fetch_benchmark_close()
    regime_sma_window: int = 200
    regime_slope: float = 5.0
    regime_floor: float = 0.30           # set to 1.0 to cleanly disable the filter (spec S10)
    regime_confirm_days: int = 3
    regime_max_step: float = 0.15
    regime_state_max_age_days: int = 10  # persisted state older than this triggers a cold start
    force_regime: Optional[float] = None # CLI override: pin k_regime, bypass all regime logic
    regime_dry_run: bool = False         # CLI override: compute + log k_regime but apply 1.0 to weights

    # --- risk engine (quantlib-risk-engine-spec.md) ---
    risk_engine: str = "analytic"        # "analytic" (existing k_vol) or "montecarlo" (CVaR-targeted k_risk)
    cvar_target: float = 0.25
    cvar_alpha: float = 0.95
    mc_n_paths: int = 50_000
    mc_horizon_days: int = 5
    mc_seed: int = 42
    mc_antithetic: bool = True
    mc_generator: str = "sobol"          # "sobol" or "mersenne"
    mc_drift: float = 0.0
    risk_dry_run: bool = False           # compute + log k_risk but apply 1.0 to weights

    # --- provider adapters (system-spec.md S1, S10) ---
    equity_broker: str = "alpaca"
    market_data: str = "alpaca"
    options_broker: str = "none"         # only resolved/imported when options_enabled is True
    options_enabled: bool = False        # S5 options layer

    # --- options instrument decision (system-spec.md S5) ---
    min_adv: float = 5_000_000           # S5.8 liquidity skip gate
    max_sigma: float = 1.00              # S5.8 volatility ceiling skip gate
    max_skip_fraction: float = 0.40      # S5.8 guard rail - abort if more names than this are skipped
    iv_rich_threshold: float = 1.25      # S5.1
    iv_cheap_threshold: float = 0.85     # S5.1
    target_put_delta: float = -0.30      # S5.5
    target_call_delta: float = 0.60      # S5.5
    options_min_dte: int = 21            # S5.4 / S3.5
    options_max_dte: int = 90            # S5.4 / S3.5
    expiry_tolerance_days: int = 14      # S5.4
    delta_tolerance: float = 0.10        # S5.5
    min_surviving_contracts: int = 4     # S3.5 - not a specific number in the spec, see options/chain.py
    earnings_skip_days: int = 2          # S5.8


# ---------------------------------------------------------------------------
# S2: load + validate
# ---------------------------------------------------------------------------

def load_weekly_csv(csv_path):
    df = pd.read_csv(csv_path)
    # Normalise "Risk Index" / "risk_index" / " risk_index " etc. to the same key.
    # Extra columns (Sector, Why Included, Valuation, Bear Case, ...) are tolerated and just ignored.
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValidationError(f"CSV missing required columns: {sorted(missing)}")
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["ranking"] = pd.to_numeric(df["ranking"], errors="coerce")
    for col in ("regime", "risk_index", "volatility_index", "sentiment_index"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # expected_horizon_days (system-spec.md S2.1) drives S5.4 option expiry
    # selection. It's a system-spec.md addition on top of the base
    # position-sizing-spec.md schema, so it's optional here - only actually
    # required when OPTIONS_ENABLED=True (checked in validate()), so an
    # equity-only CSV without this column still works unchanged.
    if "expected_horizon_days" in df.columns:
        df["expected_horizon_days"] = pd.to_numeric(df["expected_horizon_days"], errors="coerce")
    return df


def check_staleness(csv_path, cfg: Config):
    """Two independent guards, since there's no generated_at column (S2):
    the file's content must differ from the last processed run, AND its
    mtime must be recent. Either failing alone is not sufficient."""
    errors = []
    path = Path(csv_path)
    content = path.read_bytes()
    file_hash = hashlib.sha256(content).hexdigest()
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age = datetime.now(timezone.utc) - mtime

    if age > timedelta(days=cfg.max_file_age_days):
        errors.append(f"input file was last modified {age.days} day(s) ago (> {cfg.max_file_age_days} day staleness limit)")

    prev = {}
    if INPUT_HASH_STATE_PATH.exists():
        try:
            prev = json.loads(INPUT_HASH_STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            prev = {}
    if prev.get("hash") == file_hash:
        errors.append("input file content hash matches the last processed run - file was not regenerated")

    return errors, file_hash, mtime


def validate(df, equity_broker: EquityBroker, cfg: Config):
    errors = []

    n = len(df)
    if n < 10:
        errors.append(f"row count {n} < 10 - aborting")
    elif n > 20:
        errors.append(f"row count {n} > 20 - expected exactly 20")
    elif n != 20:
        print(f"  WARNING: row count is {n}, expected exactly 20 - proceeding since it's within 10-19")

    dupes = df["ticker"][df["ticker"].duplicated()].unique().tolist()
    if dupes:
        errors.append(f"duplicate tickers: {dupes}")

    if df["ranking"].isna().any():
        errors.append("some ranking values are missing/non-numeric")
    else:
        rankings = sorted(int(r) for r in df["ranking"].tolist())
        if rankings != list(range(1, n + 1)):
            errors.append(f"rankings are not a strict 1..{n} sequence with no ties/gaps: got {rankings}")
        if n > 0 and (max(rankings) > 20 or min(rankings) < 1):
            errors.append("ranking values outside the 1-20 range")

    # regime is a market-wide flag: the same value must repeat on every row.
    # A file with mixed regime values is malformed (position-sizing-spec S1.1).
    if df["regime"].isna().any():
        errors.append("regime column has missing/non-numeric values")
    elif not df["regime"].isin([0, 1]).all():
        bad = df.loc[~df["regime"].isin([0, 1]), "ticker"].tolist()
        errors.append(f"regime values must be 0 or 1: bad for {bad}")
    elif df["regime"].nunique() != 1:
        errors.append(f"regime is not uniform across rows (market-wide flag): got {sorted(df['regime'].unique().tolist())}")

    for col in ("risk_index", "volatility_index", "sentiment_index"):
        if df[col].isna().any():
            bad = df.loc[df[col].isna(), "ticker"].tolist()
            errors.append(f"{col} missing/non-numeric for: {bad}")

    # system-spec.md S2.1/S2.2: required only when the options layer is
    # actually enabled, so equity-only CSVs (which predate this column)
    # keep working unchanged.
    if cfg.options_enabled:
        if "expected_horizon_days" not in df.columns:
            errors.append("expected_horizon_days column is required when OPTIONS_ENABLED=True (drives S5.4 expiry selection)")
        elif df["expected_horizon_days"].isna().any():
            bad = df.loc[df["expected_horizon_days"].isna(), "ticker"].tolist()
            errors.append(f"expected_horizon_days missing/non-numeric for: {bad}")
        else:
            out_of_range = df.loc[~df["expected_horizon_days"].between(5, 365), "ticker"].tolist()
            if out_of_range:
                errors.append(f"expected_horizon_days outside 5-365 for: {out_of_range}")

    for ticker in df["ticker"]:
        try:
            if not equity_broker.is_tradable(ticker):
                errors.append(f"{ticker} is not tradable")
        except Exception as e:
            errors.append(f"{ticker} failed asset lookup: {e}")

    return errors


# ---------------------------------------------------------------------------
# Data layer: bars, EWMA vol, covariance, ADV
# ---------------------------------------------------------------------------

def fetch_daily_bars(market_data: MarketData, symbols, lookback_days):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(lookback_days * 1.6) + 15)
    bars = market_data.daily_bars(symbols, start, end)
    if bars.empty:
        raise ValidationError("market data provider returned no bar data for the requested symbols")
    return bars


def bars_to_frames(bars_df, symbols, lookback_days, cal: TradingCalendar):
    close = bars_df["close"].unstack(level=0).tail(lookback_days)
    volume = bars_df["volume"].unstack(level=0).tail(lookback_days)

    missing = [s for s in symbols if s not in close.columns]
    if missing:
        raise ValidationError(f"missing bars entirely for: {missing}")

    # Two checks, not one. isna().any() only catches a gap for a ticker whose
    # missing date still appears in the union index (i.e. some *other*
    # fetched ticker traded that day) - it silently misses a session that no
    # ticker in the batch has a bar for (quantlib-integration.md S1). The
    # cal.validate_bar_series check is against the real exchange calendar,
    # so it catches that case too, and names the missing date(s).
    problems = []
    for s in symbols:
        series = close[s]
        if series.isna().any():
            problems.append(f"{s}: {int(series.isna().sum())} missing bar(s) in the {lookback_days}-day window")
            continue
        ok, msg = cal.validate_bar_series(series.index)
        if not ok:
            problems.append(f"{s}: {msg}")

    if problems:
        raise ValidationError("bar gaps detected:\n  " + "\n  ".join(problems))

    return close[symbols], volume[symbols]


def compute_ewma_vol(close: pd.DataFrame, cfg: Config, cal: TradingCalendar, seed_window: int = 60):
    log_ret = np.log(close / close.shift(1)).dropna(how="all")
    lam = 0.5 ** (1 / cfg.ewma_halflife)
    # A 252-session window rarely spans exactly one year (holidays), so use
    # the exchange calendar's actual session count rather than a blanket
    # `* 252` (quantlib-integration.md S2). close is already gap-validated
    # per ticker by bars_to_frames, and every ticker shares the same date
    # index at this point, so one factor applies to all of them.
    ann_factor = cal.annualisation_factor(log_ret.index[0], log_ret.index[-1], n_obs=len(log_ret))
    sigmas = {}
    for ticker in close.columns:
        r = log_ret[ticker].dropna().values
        if len(r) < seed_window + 10:
            raise ValidationError(f"not enough return history for {ticker} to seed EWMA vol")
        var = np.var(r[:seed_window])
        for rt in r[seed_window:]:
            var = lam * var + (1 - lam) * rt ** 2
        sigma = float(np.sqrt(var * ann_factor))
        sigmas[ticker] = float(np.clip(sigma, cfg.vol_floor, cfg.vol_cap))
    return pd.Series(sigmas, name="sigma")


def compute_covariance(close: pd.DataFrame, cfg: Config, cal: TradingCalendar):
    log_ret = np.log(close / close.shift(1)).dropna()
    window_ret = log_ret.tail(cfg.cov_window)
    ann_factor = cal.annualisation_factor(window_ret.index[0], window_ret.index[-1], n_obs=len(window_ret))
    lw = LedoitWolf().fit(window_ret.values)
    Sigma = lw.covariance_ * ann_factor
    return pd.DataFrame(Sigma, index=close.columns, columns=close.columns)


def compute_adv20(close: pd.DataFrame, volume: pd.DataFrame, n: int = 20):
    dollar_vol = (close * volume).tail(n)
    return dollar_vol.mean()


# ---------------------------------------------------------------------------
# S4: raw weights
# ---------------------------------------------------------------------------

def compute_raw_weights(df: pd.DataFrame, sigma: pd.Series, cfg: Config):
    d = df.set_index("ticker")
    raw = (21 - d["ranking"]) / sigma[d.index]

    if cfg.use_risk_index or cfg.use_sentiment_index:
        n = len(d)
        p_risk = d["risk_index"].rank(method="average") / n
        p_sent = d["sentiment_index"].rank(method="average") / n
        if cfg.use_risk_index:
            raw = raw * (1 - 0.20 * p_risk)
        if cfg.use_sentiment_index:
            raw = raw * (0.92 + 0.16 * p_sent)

    w = raw / raw.sum()
    return w, raw


# ---------------------------------------------------------------------------
# S5: constraints (cap, floor, liquidity) - renormalise each pass
# ---------------------------------------------------------------------------

def apply_constraints(w: pd.Series, adv20: pd.Series, V: float, cfg: Config):
    active = w.copy()
    dropped = []
    bound_log = {t: [] for t in w.index}

    for _ in range(cfg.max_constraint_passes):
        prev = active.copy()

        over_cap = active[active > cfg.position_cap].index.tolist()
        for t in over_cap:
            if "position_cap" not in bound_log[t]:
                bound_log[t].append("position_cap")
        active = active.clip(upper=cfg.position_cap)
        active = active / active.sum()

        liq_cap = cfg.liquidity_frac * adv20[active.index] / V
        over_liq = active[active > liq_cap].index.tolist()
        for t in over_liq:
            if "liquidity" not in bound_log[t]:
                bound_log[t].append("liquidity")
        active = pd.Series(np.minimum(active.values, liq_cap.values), index=active.index)
        active = active / active.sum()

        below_floor = active[active < cfg.position_floor].index.tolist()
        if below_floor:
            for t in below_floor:
                if t not in dropped:
                    dropped.append(t)
            active = active.drop(below_floor)
            if active.empty:
                raise ValidationError("all names were dropped by the floor constraint - check inputs")
            active = active / active.sum()

        if active.index.equals(prev.index) and np.allclose(
            active.values, prev.reindex(active.index).values, atol=1e-9
        ):
            break

    bound_log = {t: v for t, v in bound_log.items() if v}
    return active, dropped, bound_log


# ---------------------------------------------------------------------------
# S6: vol targeting + regime scaling
# ---------------------------------------------------------------------------

def aligned_sigma(w: pd.Series, Sigma: pd.DataFrame):
    """Sigma reindexed to w's (post-constraint, possibly-dropped) ticker set,
    as a plain ndarray for risk.engine.compute_risk_scalar."""
    idx = w.index
    return Sigma.loc[idx, idx].values


def fetch_benchmark_close(market_data: MarketData, cfg: Config, cal: TradingCalendar):
    """Fetch daily closes for cfg.regime_benchmark (default ^GSPC). Alpaca's
    stock data feed generally does not carry raw index tickers, so on any
    failure this falls back to SPY - logging which was used, since SPY is
    total-return-adjusted and the index is not (regime-spec.md S2)."""
    candidates = [cfg.regime_benchmark]
    if cfg.regime_benchmark != "SPY":
        candidates.append("SPY")

    last_err = None
    for symbol in candidates:
        try:
            bars = fetch_daily_bars(market_data, [symbol], cfg.lookback_days)
            close, _ = bars_to_frames(bars, [symbol], cfg.lookback_days, cal)
            if symbol != cfg.regime_benchmark:
                print(f"  regime benchmark {cfg.regime_benchmark!r} unavailable, using fallback {symbol!r}")
            return close[symbol], symbol
        except Exception as e:
            last_err = e
            print(f"  regime benchmark {symbol!r} unavailable ({e})")

    raise ValidationError(f"could not fetch benchmark bars for any of {candidates}: {last_err}")


def compute_distance_series(benchmark_close: pd.Series, sma_window: int, cal: TradingCalendar):
    """distance_t = (close_t - sma200_t) / sma200_t for every day the SMA is defined.

    Slicing "the last sma_window rows" (a bare rolling(sma_window)) silently
    widens the window past sma_window calendar sessions if any bar is
    missing - the SMA would then be computed over more history than
    intended with no error raised (quantlib-integration.md S3). Validate the
    benchmark series against the exchange calendar first: no gaps anywhere
    in it, and the most recent sma_window-session window starts exactly
    where the calendar says it should.
    """
    # Index dates as plain python dates throughout - benchmark_close's index
    # may be tz-aware (Alpaca returns UTC-localized timestamps) while
    # trading_calendar deals only in naive dates; only the calendar date
    # component matters here, never the time-of-day or tz.
    index_dates = [ts.date() for ts in benchmark_close.index]

    ok, msg = cal.validate_bar_series(index_dates)
    if not ok:
        raise ValidationError(f"benchmark bar series: {msg}")

    last_date = index_dates[-1]
    expected_window_start = cal.sessions_back(last_date, sma_window - 1)
    if expected_window_start < index_dates[0]:
        raise ValidationError(
            f"not enough benchmark history for a {sma_window}-session SMA: "
            f"need sessions from {expected_window_start}, have from {index_dates[0]}"
        )
    actual_window_start = next(d for d in index_dates if d >= expected_window_start)
    if actual_window_start != expected_window_start:
        raise ValidationError(
            f"benchmark {sma_window}-session window starts {actual_window_start}, "
            f"expected {expected_window_start} - gap in benchmark bars"
        )

    sma = benchmark_close.rolling(sma_window).mean()
    distance = (benchmark_close - sma) / sma
    return distance.dropna()


def compute_crossing_day_count(distance: pd.Series, confirm_days: int):
    """Consecutive trailing days (ending today) with the same sign of distance
    as today, capped at confirm_days (that's the only threshold that matters).
    Computed by replaying the fetched daily series rather than incremental
    state - see module docstring for why."""
    signs = np.where(distance.values >= 0, 1, -1)
    count = 1
    last_sign = signs[-1]
    for i in range(len(signs) - 2, -1, -1):
        if signs[i] != last_sign:
            break
        count += 1
        if count >= confirm_days:
            break
    return count


def load_regime_state(state_path: Path):
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            if "k_regime" in state and "last_run_at" in state:
                return state
        except (json.JSONDecodeError, OSError):
            pass
    return None


def compute_regime(benchmark_close: pd.Series, benchmark_used: str, supplied_regime, cfg: Config, cal: TradingCalendar, state_path=REGIME_STATE_PATH):
    """Continuous/confirmed/rate-limited k_regime scalar - regime-spec.md S3-S5."""
    distance_series = compute_distance_series(benchmark_close, cfg.regime_sma_window, cal)
    if len(distance_series) < cfg.regime_confirm_days:
        raise ValidationError("not enough benchmark history to compute a confirmed regime scalar")

    distance = float(distance_series.iloc[-1])
    sma200 = float(benchmark_close.rolling(cfg.regime_sma_window).mean().iloc[-1])
    close = float(benchmark_close.iloc[-1])

    k_raw = 0.5 + cfg.regime_slope * distance
    k_target = float(np.clip(k_raw, cfg.regime_floor, 1.0))
    crossing_day_count = compute_crossing_day_count(distance_series, cfg.regime_confirm_days)

    prev_state = load_regime_state(state_path)
    cold_start = prev_state is None
    if not cold_start:
        last_run_at = datetime.fromisoformat(prev_state["last_run_at"])
        if datetime.now(timezone.utc) - last_run_at > timedelta(days=cfg.regime_state_max_age_days):
            cold_start = True
            print(f"  regime state is stale (last run {last_run_at.isoformat()}) - treating as cold start")

    confirmation_blocked = False
    crossed_side = False
    crossing_count_12m = (prev_state or {}).get("crossing_log", [])

    if cold_start:
        prev_k_regime = k_target
        k_after_confirm = k_target
        k_regime = k_target
        print("  regime: no valid prior state - cold start (rate limiter and confirmation gate skipped this run)")
    else:
        prev_k_regime = float(prev_state["k_regime"])
        side_target = 1 if distance >= 0 else 0
        side_prev = 1 if prev_k_regime >= 0.5 else 0
        crossing_would_span = side_target != side_prev

        if crossing_would_span and crossing_day_count < cfg.regime_confirm_days:
            confirmation_blocked = True
            k_after_confirm = max(k_target, 0.5) if side_prev == 1 else min(k_target, 0.5)
        else:
            k_after_confirm = k_target

        delta = float(np.clip(k_after_confirm - prev_k_regime, -cfg.regime_max_step, cfg.regime_max_step))
        k_regime = prev_k_regime + delta

        side_new = 1 if k_regime >= 0.5 else 0
        if side_new != side_prev:
            crossed_side = True

    override_applied = None
    if cfg.force_regime is not None:
        override_applied = "force_regime"
        k_regime = cfg.force_regime
    elif cfg.regime_dry_run:
        override_applied = "regime_dry_run"

    if supplied_regime is not None:
        supplied_side = int(supplied_regime)
        computed_side = 1 if distance >= 0 else 0
        if supplied_side != computed_side:
            print(
                f"  note: supplied regime={supplied_side} disagrees with our computed "
                f"distance-from-SMA side={computed_side}; using our computed value"
            )

    now = datetime.now(timezone.utc)
    if crossed_side:
        crossing_count_12m = crossing_count_12m + [now.isoformat()]
    cutoff = now - timedelta(days=365)
    crossing_count_12m = [t for t in crossing_count_12m if datetime.fromisoformat(t) > cutoff]

    result = {
        "benchmark_used": benchmark_used,
        "close": close,
        "sma200": sma200,
        "distance": distance,
        "k_raw": k_raw,
        "k_target": k_target,
        "k_after_confirm": k_after_confirm,
        "k_regime": k_regime,
        "prev_k_regime": prev_k_regime,
        "crossing_day_count": crossing_day_count,
        "confirmation_blocked": confirmation_blocked,
        "cold_start": cold_start,
        "crossed_side_this_run": crossed_side,
        "crossing_count_trailing_12m": len(crossing_count_12m),
        "override_applied": override_applied,
        "supplied_regime": None if supplied_regime is None else int(supplied_regime),
    }

    if not cfg.regime_dry_run:
        state_path.write_text(json.dumps({
            "k_regime": k_regime,
            "last_run_at": now.isoformat(),
            "benchmark_used": benchmark_used,
            "crossing_log": crossing_count_12m,
        }, indent=2))

    if result["crossing_count_trailing_12m"] > 5:
        print(
            f"  WARNING: {result['crossing_count_trailing_12m']} regime crossings in the trailing 12 months "
            f"(>5/yr) - REGIME_SLOPE or REGIME_CONFIRM_DAYS may need tuning (regime-spec.md S7)"
        )

    return result


# ---------------------------------------------------------------------------
# S7: cross-check (log only)
# ---------------------------------------------------------------------------

def cross_check(df: pd.DataFrame, sigma: pd.Series):
    sigma_pct = sigma.rank(pct=True)
    vol_idx_pct = df.set_index("ticker")["volatility_index"].rank(pct=True)
    diffs = (sigma_pct - vol_idx_pct.reindex(sigma_pct.index)).abs()
    flagged = diffs[diffs > 0.4]
    for t, d in flagged.items():
        print(f"  cross-check WARNING: {t} computed-vol percentile vs supplied volatility_index percentile differ by {d:.2f}")
    return flagged.to_dict()


# ---------------------------------------------------------------------------
# S5.0: reconcile equity weights with the instrument decision
# ---------------------------------------------------------------------------

def zero_out_options_weight(w_final: pd.Series, instrument_decisions: dict) -> pd.Series:
    """The four S5.0 outcomes (shares / short put / long call / skip) are
    mutually exclusive expressions of the same target weight, not
    additive. A name the decision engine routed to an option must not also
    carry an equity `position_size` - trade_from_csv.py's options path
    spends the (much smaller, delta-equivalent) option premium for that
    name, not this notional, so leaving the equity weight non-zero here
    means trade_from_csv.py would buy the shares AND open the option: the
    S5.6 capital-overcommit warning, just on the equity side instead of
    CSP capital. Returns a new Series; does not mutate the input."""
    w_final = w_final.copy()
    for ticker, dec in instrument_decisions.items():
        if dec.instrument != "shares" and ticker in w_final.index:
            w_final[ticker] = 0.0
    return w_final


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_pipeline(csv_path, output_path, cfg: Config, basis="equity", force=False):
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: set ALPACA_API_KEY and ALPACA_SECRET_KEY (in .env or env vars).")
        sys.exit(1)

    # system-spec.md S1: nothing below this line imports alpaca-py (or any
    # provider SDK) directly - it all goes through the adapters resolved
    # here. With cfg.options_enabled False, options_broker resolves to
    # "none" and brokers.alpaca_options is never imported at all.
    providers = resolve_providers(
        api_key,
        secret_key,
        equity_broker=cfg.equity_broker,
        market_data=cfg.market_data,
        options_broker=(cfg.options_broker if cfg.options_enabled else "none"),
        paper=True,
    )
    equity_broker = providers.equity
    market_data = providers.market_data

    cal = TradingCalendar("NYSE")
    today = datetime.now(timezone.utc).date()
    if not cal.is_trading_day(today):
        # quantlib-integration.md S6: reschedule to the next session rather
        # than skip the week. There is no scheduler/cron in this repo (the
        # script is invoked manually) so there is nothing to reschedule -
        # just make the non-trading-day invocation visible rather than
        # silently computing against a closed market.
        print(f"  WARNING: {today} is not a NYSE trading session - next session is {cal.next_session(today)}")

    staleness_errors, file_hash, mtime = check_staleness(csv_path, cfg)
    if staleness_errors and force:
        print("--force: bypassing staleness guard:")
        for e in staleness_errors:
            print(f"  - {e}")
        staleness_errors = []

    df = load_weekly_csv(csv_path)
    errors = staleness_errors + validate(df, equity_broker, cfg)
    if errors:
        print("VALIDATION FAILED - refusing to trade:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    tickers = df["ticker"].tolist()

    print(f"Fetching {cfg.lookback_days}+ days of daily bars for {len(tickers)} symbols...")
    bars = fetch_daily_bars(market_data, tickers, cfg.lookback_days)
    close, volume = bars_to_frames(bars, tickers, cfg.lookback_days, cal)

    print(f"Fetching regime benchmark ({cfg.regime_benchmark})...")
    benchmark_close, benchmark_used = fetch_benchmark_close(market_data, cfg, cal)

    # Pre-run freshness guard (quantlib-integration.md S4): abort rather than
    # size a portfolio against a feed that silently stalled a session or
    # more behind. Uses the benchmark series since it's fetched on every
    # run regardless of which names are in the CSV.
    try:
        cal.assert_fresh(latest_bar_date=benchmark_close.index[-1], run_date=today)
    except RuntimeError as e:
        if force:
            print(f"--force: bypassing freshness guard: {e}")
        else:
            print(f"VALIDATION FAILED - refusing to trade:\n  - {e}")
            sys.exit(1)

    sigma = compute_ewma_vol(close[tickers], cfg, cal)
    Sigma = compute_covariance(close[tickers], cfg, cal)
    adv20 = compute_adv20(close[tickers], volume[tickers])

    w_raw, raw = compute_raw_weights(df, sigma, cfg)

    account = equity_broker.account()
    V = float(getattr(account, basis))

    w_constrained, dropped, bound_log = apply_constraints(w_raw, adv20, V, cfg)

    # system-spec.md S5: per-name instrument decision (shares / short put /
    # long call / skip). Runs after S4.2 constraints and before the risk
    # engine, since a skip's redistributed weight must be what the risk
    # engine and everything downstream actually sees. Imports from
    # options.* only happen inside this branch, so equity-only mode
    # (options_enabled=False, the default) never imports the options
    # package or anything it depends on (system-spec.md S1.1).
    instrument_decisions = {}
    skip_decisions = {}
    if cfg.options_enabled:
        from options.decision import DecisionConfig, OptionsLayerError, compute_instrument_decisions
        from options.rates import fetch_dividend_yield
        from earnings.provider import resolve_earnings_calendar

        earnings_calendar = resolve_earnings_calendar()
        spot = close[tickers].iloc[-1]
        dividend_yields = {t: fetch_dividend_yield(t) for t in w_constrained.index}

        decision_cfg = DecisionConfig(
            min_adv=cfg.min_adv,
            max_sigma=cfg.max_sigma,
            max_skip_fraction=cfg.max_skip_fraction,
            iv_rich_threshold=cfg.iv_rich_threshold,
            iv_cheap_threshold=cfg.iv_cheap_threshold,
            target_put_delta=cfg.target_put_delta,
            target_call_delta=cfg.target_call_delta,
            min_dte=cfg.options_min_dte,
            max_dte=cfg.options_max_dte,
            expiry_tolerance_days=cfg.expiry_tolerance_days,
            delta_tolerance=cfg.delta_tolerance,
            min_surviving_contracts=cfg.min_surviving_contracts,
            earnings_skip_days=cfg.earnings_skip_days,
        )

        def _redistribute(w):
            return apply_constraints(w, adv20.reindex(w.index), V, cfg)

        try:
            w_constrained, instrument_decisions, skip_decisions = compute_instrument_decisions(
                df=df,
                w_constrained=w_constrained,
                sigma=sigma,
                adv20=adv20,
                spot=spot,
                portfolio_value=V,
                valuation_date=today,
                equity_broker=equity_broker,
                options_broker=providers.options,
                earnings_calendar=earnings_calendar,
                cal=cal,
                dividend_yields=dividend_yields,
                cfg=decision_cfg,
                redistribute_fn=_redistribute,
            )
        except OptionsLayerError as e:
            print(f"VALIDATION FAILED - refusing to trade:\n  - {e}")
            sys.exit(1)

        for ticker, skip in skip_decisions.items():
            print(f"  SKIP   {ticker:<6} gate={skip.gate:<18} {skip.reason}")
        for ticker, dec in instrument_decisions.items():
            extra = f" strike={dec.strike} expiry={dec.expiry} contracts={dec.contracts}" if dec.contracts else ""
            print(f"  {ticker:<6} -> {dec.instrument:<10} {dec.reason}{extra}")

    risk_cfg = RiskConfig(
        risk_engine=cfg.risk_engine,
        sigma_target=cfg.sigma_target,
        cvar_target=cfg.cvar_target,
        cvar_alpha=cfg.cvar_alpha,
        mc_n_paths=cfg.mc_n_paths,
        mc_horizon_days=cfg.mc_horizon_days,
        mc_seed=cfg.mc_seed,
        mc_antithetic=cfg.mc_antithetic,
        mc_generator=cfg.mc_generator,
        mc_drift=cfg.mc_drift,
        risk_dry_run=cfg.risk_dry_run,
    )
    risk_result = compute_risk_scalar(w_constrained, aligned_sigma(w_constrained, Sigma), risk_cfg)
    sigma_p = risk_result.sigma_p_analytic
    k_vol = risk_result.k_risk
    applied_k_vol = 1.0 if cfg.risk_dry_run else k_vol
    w_after_vol = w_constrained * applied_k_vol

    supplied_regime = df["regime"].iloc[0]
    regime_result = compute_regime(benchmark_close, benchmark_used, supplied_regime, cfg, cal)
    k_regime = regime_result["k_regime"]
    applied_k_regime = 1.0 if cfg.regime_dry_run else k_regime

    w_final = w_after_vol * applied_k_regime
    w_final = zero_out_options_weight(w_final, instrument_decisions)
    cash = 1 - w_final.sum()

    cross_check_flags = cross_check(df, sigma)

    out = df.set_index("ticker").copy()
    out["sigma"] = sigma.reindex(out.index)
    out["weight_raw"] = w_raw.reindex(out.index)
    out["weight_after_constraints"] = w_constrained.reindex(out.index)
    out["weight_after_vol_target"] = w_after_vol.reindex(out.index)
    out["position_size"] = w_final.reindex(out.index).fillna(0.0)
    out["dropped_by_floor"] = out.index.isin(dropped)
    out["bound_constraint"] = [",".join(bound_log.get(t, [])) for t in out.index]
    if cfg.options_enabled:
        # system-spec.md S11: per-name instrument chosen, iv_ratio, and
        # fallback/skip reason logged for every name every run.
        out["instrument"] = [
            skip_decisions[t].gate if t in skip_decisions
            else (instrument_decisions[t].instrument if t in instrument_decisions else "")
            for t in out.index
        ]
        out["instrument_reason"] = [
            skip_decisions[t].reason if t in skip_decisions
            else (instrument_decisions[t].reason if t in instrument_decisions else "")
            for t in out.index
        ]
        out["iv_ratio"] = [instrument_decisions[t].iv_ratio if t in instrument_decisions else None for t in out.index]
        out["option_expiry"] = [instrument_decisions[t].expiry if t in instrument_decisions else None for t in out.index]
        out["option_strike"] = [instrument_decisions[t].strike if t in instrument_decisions else None for t in out.index]
        out["option_delta"] = [instrument_decisions[t].delta if t in instrument_decisions else None for t in out.index]
        out["option_contracts"] = [instrument_decisions[t].contracts if t in instrument_decisions else None for t in out.index]
        out["option_premium"] = [instrument_decisions[t].premium if t in instrument_decisions else None for t in out.index]
        out["capital_reserved"] = [instrument_decisions[t].capital_reserved if t in instrument_decisions else None for t in out.index]
        out["occ_symbol"] = [instrument_decisions[t].occ_symbol if t in instrument_decisions else None for t in out.index]
    out = out.reset_index()
    out.to_csv(output_path, index=False)

    run_log = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "input_csv": str(csv_path),
        "input_csv_hash": file_hash,
        "input_csv_mtime": mtime.isoformat(),
        "basis": basis,
        "portfolio_value": V,
        "sigma": sigma.to_dict(),
        "weight_raw": w_raw.to_dict(),
        "weight_after_constraints": w_constrained.to_dict(),
        "weight_after_vol_target": w_after_vol.to_dict(),
        "weight_final": w_final.to_dict(),
        "sigma_p": sigma_p,
        "k_vol": k_vol,
        "applied_k_vol": applied_k_vol,
        "risk": asdict(risk_result),
        "regime": regime_result,
        "applied_k_regime": applied_k_regime,
        "dropped_by_floor": dropped,
        "bound_constraints": bound_log,
        "cash_pct": cash,
        "cross_check_flags": cross_check_flags,
        "options_enabled": cfg.options_enabled,
        "instrument_decisions": {t: asdict(d) for t, d in instrument_decisions.items()},
        "skip_decisions": {t: asdict(s) for t, s in skip_decisions.items()},
        "config": asdict(cfg),
    }
    log_path = LOG_DIR / f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    log_path.write_text(json.dumps(run_log, indent=2, default=str))

    # Only mark this file as "processed" once the run completed successfully,
    # so a mid-pipeline failure (e.g. bad bars) can be safely retried on the same file.
    INPUT_HASH_STATE_PATH.write_text(
        json.dumps({"hash": file_hash, "path": str(csv_path), "run_at": run_log["run_at"]}, indent=2)
    )

    regime_override_note = f" [override: {regime_result['override_applied']}]" if regime_result["override_applied"] else ""
    regime_dry_run_note = " (dry-run, applied=1.0)" if cfg.regime_dry_run else ""
    risk_dry_run_note = " (dry-run, applied=1.0)" if cfg.risk_dry_run else ""
    print()
    print(f"Wrote {output_path} ({len(out)} rows, {len(w_final)} held names)")
    print(f"Wrote run log {log_path}")
    if risk_result.risk_engine_used == "montecarlo":
        print(
            f"[{risk_result.risk_engine_used}] sigma_p analytic={sigma_p:.3f} sim={risk_result.sigma_p_simulated:.3f}"
            f" (diff={risk_result.sigma_p_diff_pct:+.1%})  cvar_ann={risk_result.cvar_annualised:.3f}"
            f"  k_vol={k_vol:.3f}{risk_dry_run_note}  ({risk_result.n_paths} paths, {risk_result.generator},"
            f" {risk_result.elapsed_ms:.0f}ms)"
        )
    else:
        print(f"[{risk_result.risk_engine_used}] sigma_p={sigma_p:.3f}  k_vol={k_vol:.3f}{risk_dry_run_note}")
    print(
        f"k_regime={k_regime:.2f}{regime_dry_run_note}{regime_override_note}"
        f"  distance={regime_result['distance']:+.2%} vs {regime_result['benchmark_used']} 200d SMA"
        f"  cash={cash:.1%}"
    )
    if regime_result["cold_start"]:
        print("  (regime: cold start this run)")
    if regime_result["confirmation_blocked"]:
        print(f"  (regime: crossing blocked, {regime_result['crossing_day_count']}/{cfg.regime_confirm_days} confirmation days)")
    if dropped:
        print(f"Dropped by floor constraint: {dropped}")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", help="Weekly ranked-stock CSV from the selection system")
    parser.add_argument("--output", default="target_positions.csv", help="Output CSV path (default: target_positions.csv)")
    parser.add_argument("--basis", default="equity", choices=["equity", "cash", "buying_power", "portfolio_value"])
    parser.add_argument("--use-risk-index", action="store_true", help="Enable S4.1 risk_index modifier (default off)")
    parser.add_argument("--use-sentiment-index", action="store_true", help="Enable S4.1 sentiment_index modifier (default off)")
    parser.add_argument("--sigma-target", type=float, default=0.15)
    parser.add_argument("--position-cap", type=float, default=0.12)
    parser.add_argument("--position-floor", type=float, default=0.015)
    parser.add_argument("--regime-benchmark", default="^GSPC", help="Falls back to SPY automatically if unavailable")
    parser.add_argument("--regime-sma-window", type=int, default=200)
    parser.add_argument("--regime-slope", type=float, default=5.0)
    parser.add_argument("--regime-floor", type=float, default=0.30, help="Set to 1.0 to cleanly disable the regime filter")
    parser.add_argument("--regime-confirm-days", type=int, default=3)
    parser.add_argument("--regime-max-step", type=float, default=0.15)
    parser.add_argument("--force-regime", type=float, default=None, help="Pin k_regime to a fixed value, bypassing all regime logic")
    parser.add_argument("--regime-dry-run", action="store_true", help="Compute and log k_regime without applying it to weights")
    parser.add_argument("--risk-engine", choices=["analytic", "montecarlo"], default="analytic",
                         help="analytic = existing volatility-targeted k_vol; montecarlo = CVaR-targeted k_risk via QuantLib "
                              "(default analytic until CVAR_TARGET is calibrated against live weights - spec S13 step 8)")
    parser.add_argument("--cvar-target", type=float, default=0.25, help="Annualised CVaR target (montecarlo engine only)")
    parser.add_argument("--cvar-alpha", type=float, default=0.95)
    parser.add_argument("--mc-paths", type=int, default=50_000, dest="mc_n_paths")
    parser.add_argument("--mc-horizon-days", type=int, default=5)
    parser.add_argument("--mc-seed", type=int, default=42)
    parser.add_argument("--mc-generator", choices=["sobol", "mersenne"], default="sobol")
    parser.add_argument("--mc-no-antithetic", action="store_false", dest="mc_antithetic")
    parser.add_argument("--mc-drift", type=float, default=0.0, help="Non-zero means CVaR is no longer a pure risk number")
    parser.add_argument("--risk-dry-run", action="store_true", help="Compute and log k_risk/k_vol without applying it to weights")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the staleness guard (unchanged file hash / old mtime) - for re-testing the same file",
    )
    parser.add_argument("--equity-broker", default="alpaca", choices=["alpaca"], help="system-spec.md S1/S10")
    parser.add_argument("--market-data", default="alpaca", choices=["alpaca"], help="system-spec.md S1/S10")
    parser.add_argument("--options-enabled", action="store_true", help="Enable the S5 options instrument-decision layer (default off, equity-only)")
    parser.add_argument("--options-broker", default="alpaca", choices=["alpaca"], help="Only resolved/imported when --options-enabled is set")
    parser.add_argument("--min-adv", type=float, default=5_000_000, help="S5.8 liquidity skip gate")
    parser.add_argument("--max-sigma", type=float, default=1.00, help="S5.8 volatility ceiling skip gate")
    parser.add_argument("--max-skip-fraction", type=float, default=0.40, help="S5.8 guard rail")
    parser.add_argument("--iv-rich-threshold", type=float, default=1.25, help="S5.1")
    parser.add_argument("--iv-cheap-threshold", type=float, default=0.85, help="S5.1")
    parser.add_argument("--target-put-delta", type=float, default=-0.30, help="S5.5")
    parser.add_argument("--target-call-delta", type=float, default=0.60, help="S5.5")
    parser.add_argument("--options-min-dte", type=int, default=21, help="S5.4/S3.5")
    parser.add_argument("--options-max-dte", type=int, default=90, help="S5.4/S3.5")
    args = parser.parse_args()

    cfg = Config(
        sigma_target=args.sigma_target,
        position_cap=args.position_cap,
        position_floor=args.position_floor,
        use_risk_index=args.use_risk_index,
        use_sentiment_index=args.use_sentiment_index,
        regime_benchmark=args.regime_benchmark,
        regime_sma_window=args.regime_sma_window,
        regime_slope=args.regime_slope,
        regime_floor=args.regime_floor,
        regime_confirm_days=args.regime_confirm_days,
        regime_max_step=args.regime_max_step,
        force_regime=args.force_regime,
        regime_dry_run=args.regime_dry_run,
        risk_engine=args.risk_engine,
        cvar_target=args.cvar_target,
        cvar_alpha=args.cvar_alpha,
        mc_n_paths=args.mc_n_paths,
        mc_horizon_days=args.mc_horizon_days,
        mc_seed=args.mc_seed,
        mc_antithetic=args.mc_antithetic,
        mc_generator=args.mc_generator,
        mc_drift=args.mc_drift,
        risk_dry_run=args.risk_dry_run,
        equity_broker=args.equity_broker,
        market_data=args.market_data,
        options_enabled=args.options_enabled,
        options_broker=args.options_broker,
        min_adv=args.min_adv,
        max_sigma=args.max_sigma,
        max_skip_fraction=args.max_skip_fraction,
        iv_rich_threshold=args.iv_rich_threshold,
        iv_cheap_threshold=args.iv_cheap_threshold,
        target_put_delta=args.target_put_delta,
        target_call_delta=args.target_call_delta,
        options_min_dte=args.options_min_dte,
        options_max_dte=args.options_max_dte,
    )
    run_pipeline(args.csv_path, args.output, cfg, basis=args.basis, force=args.force)


if __name__ == "__main__":
    main()
