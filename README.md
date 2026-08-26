# Top20stocks

Weekly-ranked-stock CSV -> position sizing -> paper-trading execution, per
the specs in `mdinstructions/`. See `position_sizing.py` and
`trade_from_csv.py` for the pipeline; `risk/` for the QuantLib Monte Carlo
CVaR engine.

## Setup

```
pip install -r requirements.txt
```

`.env`:

```
ALPACA_API_KEY=xxxx
ALPACA_SECRET_KEY=xxxx        # paper trading keys - https://app.alpaca.markets/paper/dashboard/overview

# Optional - earnings-date lookups for the options layer (system-spec.md
# S5.7/S5.8). Without this the system runs on a yfinance fallback (no key
# needed, but unofficial/lower-confidence). Free key:
# https://finnhub.io/register
FINNHUB_API_KEY=
```

## Usage

Equity-only (default):

```
python position_sizing.py weekly_input.csv --output target_positions.csv
python trade_from_csv.py target_positions.csv --dry-run
python trade_from_csv.py target_positions.csv
```

With the options layer (system-spec.md S5) - requires `expected_horizon_days`
in the input CSV, and an Alpaca paper account approved for options trading:

```
python position_sizing.py weekly_input.csv --output target_positions.csv --options-enabled
python trade_from_csv.py target_positions.csv --options-enabled --dry-run
python trade_from_csv.py target_positions.csv --options-enabled
```

`--options-enabled` on `position_sizing.py` computes and logs a per-name
shares/short-put/long-call/skip decision (writes `instrument`, `occ_symbol`,
`option_strike`, `option_expiry`, `option_contracts` columns to the output
CSV). The same flag on `trade_from_csv.py` reads those columns and submits
real (paper) option orders alongside the equity rebalance, per
system-spec.md S8.2's ordering. Omitting `--options-enabled` on either
script is equity-only and never imports the options code path at all.

## Daily monitoring and performance tracking

```
python daily_monitor.py --options-enabled     # S8.3/S9.2/S9.3 - alert-only, submits no orders
python track_performance.py                   # S14 - actual vs equal-weight vs SPY, per archived week
```

`daily_monitor.py` checks assignment risk, 21-DTE/expiry-week flags, option
delta drift, day-over-day regime moves, and equity stop-losses - it never
closes or opens a position on its own. `track_performance.py` reads every
week archived under `history/<date>/` (written automatically by
`position_sizing.py`) and recomputes, against today's live prices, how that
week's picks have actually done vs. equal-weighting the same names vs. SPY -
equity positions only, not options premium. Writes `history/performance_summary.csv`.

## Automation (system-spec.md S9)

```
.\scheduling\register_tasks.ps1 -OptionsEnabled
```

Registers two Windows Task Scheduler jobs: `OptionsSelector-DailyMonitor`
(weekdays 08:00 - runs `daily_monitor.py` + `track_performance.py`) and
`OptionsSelector-WeeklySizing` (Mondays 07:30 - runs `position_sizing.py`
only). Neither submits a trade automatically - the weekly job computes
`target_positions.csv` and archives it; you review it and run
`trade_from_csv.py` yourself. Refresh `top20.csv` (the weekly LLM prompt)
before the Monday run, or it fails closed on the staleness guard.

Inspect: `Get-ScheduledTask -TaskName "OptionsSelector-*"`.
Remove: `Get-ScheduledTask -TaskName "OptionsSelector-*" | Unregister-ScheduledTask -Confirm:$false`.

## Tests

```
pytest tests/ risk/tests/ options/tests/ -v
```

`benchmarks/bench_options_pricing.py` records the Black-Scholes-vs-binomial
comparison that decides the pricing engine for puts (system-spec.md S5.2) -
see `benchmarks/fixtures/options_pricing_comparison.json` for the result.