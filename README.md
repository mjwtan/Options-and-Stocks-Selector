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

## Validation ledger (system-spec.md S16)

Every weekly `position_sizing.py` run appends one row to
`history/validation_ledger.csv` (portfolio value, cash%, k_vol/k_risk/
k_regime, turnover, skip/fallback counts, MC diagnostics) and one row per
name to `history/validation_ledger_per_name.csv` (rank, weight,
instrument). Two columns per table - `actual_return_1w`/
`equal_weight_return_1w`, and `forward_return_1w`/`forward_return_4w` -
can't be known until that much time has actually passed; `track_performance.py`
backfills them automatically once it is, using the real historical price
at that date rather than whatever the price happens to be when it runs.

The S16.2 decision rules built on top of this ledger (does rank predict
forward returns, does sizing beat equal-weight, is the regime filter worth
its cost, etc.) aren't implemented yet - the spec is explicit those need
12-26 weeks of real rows before they mean anything, and there are zero
rows as of this build. Come back to that once the ledger has real history
in it.

## Automation

Two ways to run this on a schedule - system-spec.md S15.3 is explicit that
only one of them is meant for anything that matters:

**GitHub Actions** (`.github/workflows/`) - the recommended path while
paper trading (S15.3). Four workflows: `weekly.yml` (Mon-Fri cron,
self-gated to the week's actual first trading day), `daily.yml` (Tue-Fri),
`report.yml` (Friday after close), `heartbeat.yml` (S15.5's dead-man's-
switch). None of them run `trade_from_csv.py`. Each commits `state/`/
`history/` back to the repo so the next run (a fresh container each time)
has continuity - this also happens to keep the repo from going 60 days
without activity, which is when GitHub auto-disables a repo's scheduled
workflows.

Setup:
1. Repo Settings -> Secrets and variables -> Actions -> add `ALPACA_API_KEY`,
   `ALPACA_SECRET_KEY`, and optionally `FINNHUB_API_KEY`.
2. Repo Settings -> Actions -> General -> Workflow permissions -> "Read and
   write permissions" (needed for the commit-back step).
3. Actions tab -> pick a workflow -> "Run workflow" to test manually before
   trusting the schedule.

**Windows Task Scheduler** (`scheduling/register_tasks.ps1`) -
development/testing only (S15.3: "a missed run because the laptop was
closed is a silent failure"). Registers the same four jobs locally:

```
.\scheduling\register_tasks.ps1 -OptionsEnabled
```

Inspect: `Get-ScheduledTask -TaskName "OptionsSelector-*"`.
Remove: `Get-ScheduledTask -TaskName "OptionsSelector-*" | Unregister-ScheduledTask -Confirm:$false`.

Running both is possible but they'll maintain two independent copies of
`state/`/`history/` (local disk vs. the repo) that drift apart from each
other over time - harmless since neither submits a trade, but confusing if
you compare their logs. Pick one as authoritative once you've decided.

## Tests

```
pytest tests/ risk/tests/ options/tests/ -v
```

`benchmarks/bench_options_pricing.py` records the Black-Scholes-vs-binomial
comparison that decides the pricing engine for puts (system-spec.md S5.2) -
see `benchmarks/fixtures/options_pricing_comparison.json` for the result.