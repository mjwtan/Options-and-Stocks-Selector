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

Not yet built: system-spec.md S8.3's daily monitoring job (assignment-risk
checks, 21-DTE rolling, expiry-week flags) - the options layer here covers
the weekly decide-and-execute path only.

## Tests

```
pytest tests/ risk/tests/ options/tests/ -v
```

`benchmarks/bench_options_pricing.py` records the Black-Scholes-vs-binomial
comparison that decides the pricing engine for puts (system-spec.md S5.2) -
see `benchmarks/fixtures/options_pricing_comparison.json` for the result.