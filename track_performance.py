"""
Performance tracking - system-spec.md S14: "Log what equal-weight would
have returned over the same period - if this does not beat equal-weight
net of costs, the complexity is not earning its place."

Reads every archived week from history/<date>/ (written by
position_sizing.py's archive_run - see its docstring), and for each one
compares, using today's live prices against that week's entry_price:
  - actual return: this system's rank/vol-weighted positions
  - equal-weight return: the same held names, weighted 1/N instead
  - benchmark return: SPY over the same window

Recomputed from scratch every run, not appended - "today's price" changes
daily, so the table always reflects "as of today, here's how each past
week's picks have done since they were set," not a frozen log entry.

Equity only. Options positions (position_size == 0 once S5.0 routes a name
to an option - see zero_out_options_weight()) are excluded from the return
math here: tracking an option's P&L needs its own live premium, which
decays and moves on IV/theta/gamma, not just the underlying's price - a
different and harder problem than this script solves. daily_monitor.py's
delta-drift check is the closest thing to open-option tracking today.

Usage:
    python track_performance.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from data.alpaca_data import AlpacaMarketData

load_dotenv()

BASE_DIR = Path(__file__).parent
HISTORY_DIR = BASE_DIR / "history"
BENCHMARK = "SPY"


def find_archived_weeks(history_dir: Path):
    """Returns [(date, target_positions_csv_path), ...], oldest first.
    Finds the output CSV by elimination - archive_run names the input copy
    input_<origname>.csv and the run log <name>.json, so whatever .csv is
    left is the sizing output, whatever its original filename was."""
    weeks = []
    for day_dir in sorted(history_dir.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        output_csvs = [p for p in day_dir.glob("*.csv") if not p.name.startswith("input_")]
        if not output_csvs:
            continue
        weeks.append((day, output_csvs[0]))
    return weeks


def load_held_positions(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "entry_price" not in df.columns or "position_size" not in df.columns:
        return pd.DataFrame(columns=["ticker", "position_size", "entry_price", "ranking"])
    held = df[df["position_size"] > 0].copy()
    return held[["ticker", "position_size", "entry_price"] + (["ranking"] if "ranking" in held.columns else [])]


def fetch_current_prices(market_data: AlpacaMarketData, tickers: list[str]) -> dict:
    prices = {}
    for t in tickers:
        try:
            prices[t] = market_data.latest_quote(t).mid
        except Exception as e:
            print(f"  warning: could not fetch a current quote for {t}: {e}")
    return prices


def fetch_benchmark_series(market_data: AlpacaMarketData, start, end) -> pd.Series:
    # Pad both ends by a few days: a single-day request (start == end, the
    # common case with only one archived week so far) can come back empty
    # depending on the time of day this runs, and the padding costs nothing.
    end_dt = datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=2)
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=5)
    bars = market_data.daily_bars([BENCHMARK], start_dt, end_dt)
    if bars.empty:
        return pd.Series(dtype=float)
    close = bars["close"].unstack(level=0)[BENCHMARK]
    close.index = pd.to_datetime(close.index).date
    return close.sort_index()


def benchmark_return(benchmark_close: pd.Series, entry_date, today):
    if benchmark_close.empty:
        return None
    entry_candidates = benchmark_close[benchmark_close.index <= entry_date]
    today_candidates = benchmark_close[benchmark_close.index <= today]
    if entry_candidates.empty or today_candidates.empty:
        return None
    entry_price = entry_candidates.iloc[-1]
    current_price = today_candidates.iloc[-1]
    return (current_price / entry_price) - 1.0


def compute_week_performance(held: pd.DataFrame, current_prices: dict):
    """Returns (actual_return, equal_weight_return, n_held) or (None, None, 0)
    if no held names have a current price available."""
    rows = []
    for _, row in held.iterrows():
        ticker = row["ticker"]
        entry_price = row["entry_price"]
        current_price = current_prices.get(ticker)
        if current_price is None or pd.isna(entry_price) or entry_price <= 0:
            continue
        per_name_return = (current_price / entry_price) - 1.0
        rows.append((ticker, row["position_size"], per_name_return))

    if not rows:
        return None, None, 0

    total_weight = sum(w for _, w, _ in rows)
    actual_return = sum((w / total_weight) * r for _, w, r in rows)
    equal_weight_return = sum(r for _, _, r in rows) / len(rows)
    return actual_return, equal_weight_return, len(rows)


def run():
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: set ALPACA_API_KEY and ALPACA_SECRET_KEY (in .env or env vars).")
        sys.exit(1)

    weeks = find_archived_weeks(HISTORY_DIR)
    if not weeks:
        print(f"No archived weeks found in {HISTORY_DIR} yet - run position_sizing.py at least once first.")
        return

    market_data = AlpacaMarketData(api_key, secret_key)
    today = datetime.now(timezone.utc).date()

    all_held = {day: load_held_positions(csv_path) for day, csv_path in weeks}
    all_tickers = sorted({t for held in all_held.values() for t in held["ticker"]})
    current_prices = fetch_current_prices(market_data, all_tickers)

    earliest = min(all_held.keys())
    benchmark_close = fetch_benchmark_series(market_data, earliest, today)

    results = []
    for day, held in all_held.items():
        actual, equal_weight, n_held = compute_week_performance(held, current_prices)
        bench = benchmark_return(benchmark_close, day, today)
        results.append({
            "week": day.isoformat(),
            "days_since": (today - day).days,
            "n_held": n_held,
            "actual_return": actual,
            "equal_weight_return": equal_weight,
            "benchmark_return": bench,
            "vs_equal_weight": (actual - equal_weight) if actual is not None and equal_weight is not None else None,
            "vs_benchmark": (actual - bench) if actual is not None and bench is not None else None,
        })

    out = pd.DataFrame(results)
    out_path = HISTORY_DIR / "performance_summary.csv"
    out.to_csv(out_path, index=False)

    print(f"Performance as of {today} (equity positions only - see module docstring on options)\n")
    fmt_cols = ["actual_return", "equal_weight_return", "benchmark_return", "vs_equal_weight", "vs_benchmark"]
    display = out.copy()
    for c in fmt_cols:
        display[c] = display[c].map(lambda v: f"{v:+.2%}" if pd.notna(v) else "n/a")
    print(display.to_string(index=False))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    run()
