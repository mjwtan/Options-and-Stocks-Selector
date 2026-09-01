# Options Selector

A systematic equity & options portfolio pipeline: a weekly LLM-generated
volatility screen flows through QuantLib-based options pricing and a Monte
Carlo CVaR risk engine, executes on Alpaca (paper), and runs end to end on a
schedule with no manual step required to keep it running. Trade execution
itself always requires a human to review and approve — the one deliberate
exception.

**Full specification:** [system-spec.md](system-spec.md) — an honest,
maintained description of what's actually implemented, including what
diverges from the original design and why, and a stated list of known
limitations. Start there for real depth; this README is the quickstart.

## Pipeline

```
Weekly LLM volatility screen (automated, §17)
  → CSV validation & staleness guards (§2)
  → market data, realised vol, covariance (§3)
  → rank-weighted equity sizing + constraints (§4)
  → per-name instrument decision: shares / calls / short puts (QuantLib, §5)
  → Monte Carlo CVaR risk scalar (§6)
  → regime scalar (§7)
  → target_positions.csv
  → [human review — dashboard or CLI] → paper orders (§8)
```

Three layers, each owning its own concern: an LLM picks *what* to consider
(a judgment this system doesn't try to reproduce), QuantLib decides *how* to
express it (shares vs. options), and a Monte Carlo risk engine decides *how
much* in total.

## Setup

```
pip install -r requirements.txt
```

`.env`:

```
ALPACA_API_KEY=xxxx
ALPACA_SECRET_KEY=xxxx        # paper trading keys - https://app.alpaca.markets/paper/dashboard/overview

# Optional - earnings-date lookups for the options layer (system-spec.md S5.7/S5.8).
# Without this the system runs on a yfinance fallback (no key needed, lower confidence).
# Free key: https://finnhub.io/register
FINNHUB_API_KEY=
```

## Usage

Equity-only (default):

```
python position_sizing.py top20.csv --output target_positions.csv
python trade_from_csv.py target_positions.csv --dry-run
python trade_from_csv.py target_positions.csv
```

With the options layer (system-spec.md §5) — requires `expected_horizon_days`
in the input CSV, and an Alpaca paper account approved for options trading:

```
python position_sizing.py top20.csv --output target_positions.csv --options-enabled
python trade_from_csv.py target_positions.csv --options-enabled --dry-run
python trade_from_csv.py target_positions.csv --options-enabled
```

`--options-enabled` on `position_sizing.py` computes and logs a per-name
shares/short-put/long-call/skip decision. The same flag on `trade_from_csv.py`
reads those columns and submits real (paper) option orders alongside the
equity rebalance. Omitting `--options-enabled` on either script is
equity-only and never imports the options code path at all.

## Dashboard

```
streamlit run dashboard.py
```

Mostly read-only: live account & positions, the latest sizing decision,
performance vs. equal-weight vs. SPY, ledger trends, daily-monitor alerts,
and automation health (heartbeats + live GitHub Actions status). One
deliberate exception — a "Rebalance to target_positions.csv" section on the
Positions tab previews the trade plan and can submit real paper orders, but
only behind a mandatory review checkbox that resets after every use.

## Daily monitoring and performance tracking

```
python daily_monitor.py --options-enabled     # alert-only, submits no orders
python track_performance.py                   # actual vs equal-weight vs SPY, per archived week
```

`daily_monitor.py` checks assignment risk, 21-DTE/expiry-week flags, option
delta drift, day-over-day regime moves, and equity stop-losses — it never
closes or opens a position on its own. `track_performance.py` recomputes,
against real historical prices, how each archived week's picks have
actually performed vs. equal-weighting the same names vs. SPY.

## Validation ledger

Every weekly run appends a row to `history/validation_ledger.csv` (portfolio
value, cash%, risk/regime scalars, turnover, skip/fallback counts, MC
diagnostics) and one row per name to `history/validation_ledger_per_name.csv`
(rank, weight, instrument, `iv_ratio`, fallback reason). Forward-return
columns are backfilled automatically once enough time has actually passed.

The statistical decision rules this ledger exists to eventually support
(does rank predict forward returns, does this beat equal-weight net of
costs) aren't built yet — deliberately: they need 12–26 weeks of real data
before they'd mean anything. See system-spec.md §16.

## Automation

The entire pipeline runs unattended. Two things are scheduled:

**The weekly volatility screen** — a Claude Code Routine (not a repo file;
see [`mdinstructions/weekly-csv-generation-routine.md`](mdinstructions/weekly-csv-generation-routine.md)
for its exact instructions, kept in sync by hand since a Routine's config
lives in Claude's hosted UI). Runs weekdays 11:00 UTC, gated to the week's
real first trading day, produces `top20.csv`/`top20.md`.

**Everything downstream of that** — GitHub Actions (`.github/workflows/`):

| Workflow | Schedule | Does |
|---|---|---|
| `weekly.yml` | Mon–Fri 12:15 UTC, self-gated | Runs `position_sizing.py`. Compute-only — never trades. |
| `daily.yml` | Tue–Fri 12:45 UTC | Runs `daily_monitor.py`. |
| `report.yml` | Friday 21:30 UTC | Runs `track_performance.py`. |
| `heartbeat.yml` | Mon–Fri | Dead-man's-switch — alerts if any of the above hasn't run in 8 days. |

Setup:
1. Repo Settings → Secrets and variables → Actions → add `ALPACA_API_KEY`,
   `ALPACA_SECRET_KEY`, and optionally `FINNHUB_API_KEY`.
2. Repo Settings → Actions → General → Workflow permissions → "Read and
   write permissions" (needed for the commit-back steps).
3. Actions tab → pick a workflow → "Run workflow" to test manually.

A Windows Task Scheduler alternative exists (`scheduling/register_tasks.ps1`)
for local development — GitHub Actions is the one meant for anything that
matters, since a local scheduled task stops the moment the laptop is closed.

## Tests

```
pytest tests/ risk/tests/ options/tests/ -v
```

156 tests. `benchmarks/bench_options_pricing.py` records the
Black-Scholes-vs-binomial comparison that decided the pricing engine for
puts — see `benchmarks/fixtures/options_pricing_comparison.json`.

## Design history

`mdinstructions/` holds the original design specifications this system was
built from (`position-sizing-spec.md`, `quantlib-integration.md`,
`quantlib-risk-engine-spec.md`, `regime-spec.md`), kept for provenance.
`system-spec.md` at the repo root is the current, maintained specification —
where the build ended up differing from that original plan, `system-spec.md`
states it plainly, including a stated list of known limitations rather than
implying everything is finished.
