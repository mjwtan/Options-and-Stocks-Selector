# Systematic Equity & Options Portfolio — System Specification

**Version:** 2.0 — as-built
**Execution venue:** Alpaca (paper)
**Cadence:** Weekly full run, daily monitoring, fully automated end to end
**Language:** Python 3.11+ (CI) / 3.13 (local dev)

This is the current, maintained specification — it describes what is actually
implemented, not just what was originally designed. Where the build departs
from the original plan, that's stated plainly, with the reason. The original
design documents this was built from live in `mdinstructions/` for history;
`weekly-csv-generation-routine.md` there is the one still actively read (the
weekly screen's automation instructions).

A one-line status for anyone skimming: **the entire pipeline — screen
generation, position sizing, execution planning, daily monitoring, and
performance tracking — runs on a schedule with no manual step required to
keep it running.** The one deliberate exception is submitting real trades:
that always requires a human to review and click "execute," by design (§8.4).

---

## 0. Architecture

Three layers. Each consumes the previous layer's output and does not reach back.

| Layer | Question | Owner |
|---|---|---|
| 1. Discovery | What to own | LLM, delivered as CSV |
| 2. Expression | How to own it — shares, calls, or short puts | QuantLib options pricing |
| 3. Risk | How much in total | Monte Carlo CVaR |

The separation matters. Layer 1 is a judgment we do not attempt to reproduce. Layers 2 and 3 are calculations we own end to end, computed from our own market data.

```
CSV (20 ranked names)                    ← automated weekly (§17)
  → validate                             ← position_sizing.py
  → fetch bars + option chains
  → compute realised vol, covariance
  → raw weights from rank / vol
  → constraints
  → per-name instrument choice (QuantLib IV vs realised)
  → Monte Carlo portfolio simulation → CVaR → k_risk
  → regime scalar → k_regime
  → final positions → target_positions.csv, validation ledger (§16)
  → [human review — dashboard or CLI] → orders (sells, confirm, then buys)
```

---

## 1. Providers and abstraction

### 1.1 Design requirement — swappable, independent adapters

Equity and options are handled by **separate adapters behind separate interfaces**. The system runs equity-only with `OPTIONS_ENABLED=False` and no options provider imported or credentialed at all.

```
brokers/
  base.py               # EquityBroker, OptionsBroker, MarketData protocols
  alpaca_equity.py
  alpaca_options.py
  providers.py           # resolve_providers(cfg) factory
data/
  alpaca_data.py
```

Three protocols, deliberately separate — `brokers/base.py`'s actual signatures, slightly extended from the original design where a real gap required it (`is_tradable`, `pending_corporate_action`, `latest_contract_quote` were added for reasons noted inline in that file):

```python
class MarketData(Protocol):
    def daily_bars(self, symbols, start, end) -> DataFrame: ...
    def latest_quote(self, symbol) -> Quote: ...

class EquityBroker(Protocol):
    def positions(self) -> list[Position]: ...
    def account(self) -> Account: ...
    def is_tradable(self, symbol) -> bool: ...
    def submit_notional(self, symbol, notional, side) -> Order: ...
    def close_position(self, symbol) -> Order: ...
    def order_status(self, order_id) -> Status: ...
    def pending_corporate_action(self, symbol, lookahead_days=90) -> str | None: ...

class OptionsBroker(Protocol):
    def chain(self, symbol, expiry_range) -> list[Contract]: ...
    def submit_option(self, contract, qty, side) -> Order: ...
    def option_positions(self) -> list[OptionPosition]: ...
    def buying_power_reserved(self) -> Decimal: ...
    def latest_contract_quote(self, occ_symbol) -> Contract: ...
```

Nothing in the sizing, risk, or regime layers imports a provider module directly — everything goes through an adapter instance, resolved by `brokers/providers.py`. **Only Alpaca is implemented today**, for all three roles. The IBKR/Tradier/Polygon rows below remain the honest cost/tradeoff comparison from the original design, kept because the abstraction means adding one is a new adapter file, not a rewrite — but none of them have been built.

### 1.2 Equity execution

| Provider | Cost | Notes |
|---|---|---|
| **Alpaca** | Free | Commission-free US equities, fractional via notional orders, free paper trading, clean REST. **Implemented, in use.** |
| **IBKR** | Paid | Required only for non-US listings. Not implemented. |
| **Tradier** | $10/mo + $0.35/contract | Not implemented. |

### 1.3 Options execution and chain data

| Provider | Cost | Assessment |
|---|---|---|
| **Alpaca Options** | Free | **Implemented, in use.** Greeks/IV are carried through as a cross-check only (§1.4.3) — the actual pricing decision is computed independently via QuantLib (§5.2), not trusted from the provider. |
| **Tradier / Polygon / ORATS** | Paid | Not implemented. Remains the likely upgrade path if the IV signal (§5.1) proves to be where the edge is — establishing whether an `iv_ratio` needs historical context to interpret needs IV history this system doesn't have today. |

### 1.4 Verification before building

Done ad hoc via live testing against the real paper account during the build, rather than as a formal pre-flight harness — every provider-facing bug in this system (option order pricing not crossing the spread, equity positions leaking option holdings, a trailing newline in a CI secret breaking every HTTP header) was found this way, not by a written verification script. Worth building §1.4 as literally specified if a second provider is ever added, so the same class of bug doesn't need rediscovering live twice.

---

## 2. Layer 1 — CSV ingestion

### 2.1 Schema

Delivered weekly, automatically (§17). One row per stock. The actual schema is wider than originally specified — it carries the qualitative fields (`sector`, `why_included`, `valuation`, `bear_case`) the LLM screen produces, plus the numeric fields `position_sizing.py` computes on top:

| Column | Type | Source | Definition |
|---|---|---|---|
| `ticker` | string | screen | Must match broker symbology exactly |
| `sector`, `why_included`, `valuation`, `bear_case` | string | screen | Qualitative rationale, logged and shown in the dashboard, not used numerically |
| `ranking` | int 1–20 | screen | Conviction order, 1 = highest. Strict, no ties or gaps |
| `regime` | int 0/1 | screen | Market-wide flag. Logged, **not used** — see §7.1 |
| `risk_index` | float | screen | Higher = riskier |
| `volatility_index` | float | screen | Higher = more volatile |
| `sentiment_index` | float | screen | Higher = more positive |
| `expected_horizon_days` | int | screen | Drives option expiry selection (§5.4) |
| `data_quality` | float 0–1 | screen | Fraction of that row's numeric cells retrieved live vs. estimated |
| `entry_price`, `sigma` | float | screen | Reference price and the screen's own vol estimate — logged for comparison against this system's own computed `sigma_i` (§3.2), not used directly |

`position_sizing.py` then appends its own computed columns (weights at each stage, instrument decision, strike/expiry/delta/contracts/premium) to the same file for output — see §11.

### 2.2 Validation gate

Refuse to trade and alert on any of:

- Row count outside 10–20 (abort below 10; warn and proceed 10–19; exactly 20 expected)
- Duplicate tickers
- Any ticker failing an Alpaca asset lookup or flagged non-tradable
- `ranking` not forming a strict 1..N sequence
- `regime` not uniform across rows, or not in {0, 1}
- Any index column non-numeric or null
- `expected_horizon_days` missing, or outside 5–365
- Missing bars for any ticker, or a gap longer than the exchange calendar allows

**Staleness — two independent guards, as originally specified, but the age check is implemented differently than planned.** The file carries no timestamp, so it's checked two ways:

1. **Content hash** against the previous successfully-processed run (`state/last_input_hash.json`). Catches "nothing was regenerated" regardless of any timestamp.
2. **Age**, compared against a 3-day limit. The original design read the file's filesystem mtime — this turned out to be a real, silent bug once the pipeline moved to GitHub Actions: every scheduled run does a fresh `git checkout`, which stamps every file's mtime to "now" regardless of its actual content age, so the mtime check could structurally never fire in CI. Fixed to read the file's last **git commit date** instead (`git log -1 --format=%cI -- <path>`), which survives a checkout intact, falling back to mtime only for local/non-git usage. `weekly.yml`'s checkout uses `fetch-depth: 0` so this has the history it needs to be correct.

A malformed file never reaches order submission. Log the reason, exit non-zero.

---

## 3. Layer 2a — Market data

All computed from our own sources. Nothing here comes from the CSV, other than each ticker.

### 3.1 Equity bars

~300 daily bars per ticker plus the benchmark, from Alpaca, one bulk call, with bounded retry (`fetch_and_validate_bars`) for the transient "bar not posted yet" case found live on thinly-traded names.

### 3.2 Realised volatility (EWMA, 21-day half-life)

```
lam = 0.5 ** (1/21)                     # ≈ 0.9674
var = variance(r[0:60])                 # seed
for t in remaining:
    var = lam * var + (1 - lam) * r_t**2

sigma_i = clip(sqrt(var * 252), 0.12, 0.80)
```

As specified. The floor prevents an unusually quiet stock from receiving an enormous inverse-vol weight from a temporary lull.

### 3.3 Covariance

```python
Sigma = LedoitWolf().fit(returns_250d).covariance_ * 252
```

As specified — QuantLib has no shrinkage estimator, so sklearn does this; QuantLib's `pseudoSqrt` is still used downstream in the Monte Carlo path generator (§6.2) for the correlated-normal construction.

### 3.4 Calendar

`trading_calendar.py` (renamed from the originally-supplied `calendar.py` — a repo-root `calendar.py` shadows the stdlib module). One real bug fixed from the supplied version: `ql.Actual252()` does not exist in the installed QuantLib binding — `ql.Business252(calendar)` is the correct day counter and is used instead.

```python
cal = ql.UnitedStates(ql.UnitedStates.NYSE)
cal.advance(today, ql.Period(-200, ql.Days))
cal.businessDaysBetween(d1, d2)
```

**Pre-run guard:** `assert_fresh()` aborts if today is not a trading day, or if the latest bar is older than the previous session — deliberately `>=` rather than an exact match, since a run any time after the open on a normal day legitimately has a fresher bar than "yesterday," and an exact-match check rejected perfectly good data.

### 3.5 Option chains

Per ticker: strikes, expiries, bid, ask, open interest, volume, via `options/chain.py`.

Quality filters — discard any contract failing these, skip the options layer for a name if fewer than 4 contracts survive:

- Bid > 0 and ask > 0
- Relative spread `(ask - bid) / mid` ≤ 0.15
- Open interest ≥ 100
- Expiry between 21 and 90 days out

In practice, the spread filter does most of the eliminating — a real chain often lists 300+ contracts across a DTE window, with only a handful of near-the-money strikes genuinely liquid. Seeing 1–2 survivors out of 300+ fetched is normal market structure, not a bug in the filter.

---

## 4. Layer 2b — Equity weights

### 4.1 Raw weights

```
raw_i = (21 - ranking_i) / sigma_i
w_i   = raw_i / sum(raw)
```

As specified. Rank 1 contributes 20 units, rank 20 contributes 1, before volatility adjustment.

### 4.2 Constraints

```
w_i <= 0.12                             # position cap
w_i >= 0.015  else drop the name        # floor
w_i * V <= 0.005 * adv20_i              # liquidity: exitable in one day
```

Applied in order, renormalised after each pass, iterated to stability. As specified — the portfolio typically holds 12–16 names, not 20, and that's intentional concentration, not a bug.

### 4.3 Optional index modifiers — not built

`risk_index` and `sentiment_index` tilts remain unbuilt, correctly deferred per the original design's own instruction to only enable them "with data supporting them" — that data (several weeks of correlation between the risk percentile and this system's own `sigma_i` percentile) doesn't exist yet. `USE_RISK_INDEX`/`USE_SENTIMENT_INDEX` exist as CLI flags, default off, with no behavior wired behind them yet.

---

## 5. Layer 2c — Instrument decision (QuantLib)

### 5.0 The decision

For each of the 20 names: buy shares, sell a cash-secured put, buy a call, or skip entirely (§5.8). Fully implemented in `options/decision.py`. Skip is a real decision, not a fallback — weight it frees is redistributed by renormalising §4.2. Every outcome and reason is logged for all 20 names, every run, including skips — visible in the dashboard's "Latest Decision" tab.

### 5.1 The signal

```
iv_ratio = iv_atm / sigma_realised
```

| `iv_ratio` | Interpretation | Action |
|---|---|---|
| > `IV_RICH_THRESHOLD` (1.25) | Options expensive | Sell cash-secured put |
| < `IV_CHEAP_THRESHOLD` (0.85) | Options cheap | Buy call |
| Between | No edge | Buy shares |

As specified. **Observed in practice across the first three real weeks of data:** every single `iv_ratio` computed landed between 0.88 and 1.24 — inside the neutral band every time, several close to but never crossing either threshold. Per this system's own stated philosophy, this is *not* being treated as evidence the thresholds are miscalibrated after three data points — `iv_ratio` and `reason` are now logged permanently per name per week (§16) specifically so this can be judged properly once 12+ weeks exist, rather than tuned prematurely.

### 5.2 Pricing engine — Black-Scholes

Black-Scholes is the default engine for everything, including short-put strike selection. The required comparison harness (`benchmarks/bench_options_pricing.py`) was run across the specified 21–90 DTE × 0.20–0.30 delta × 0–4% dividend yield grid: **maximum observed gap between Black-Scholes and a full binomial (American, CRR) price was 1.35% of premium** — under the ~2% threshold that would have required switching puts to binomial. Result recorded in `benchmarks/fixtures/options_pricing_comparison.json`. The binomial engine (`price_binomial`) is implemented and used only by that comparison harness — not in the live decision path.

Failures (`impliedVolatility` throwing on stale/crossed quotes) are caught, logged, and the contract excluded — never a substituted default.

**ATM definition:** nearest strike to spot; if more than 3% away, interpolate IV between the two bracketing strikes.

### 5.3 Ranking-based instrument restriction

| Rank | Permitted expressions |
|---|---|
| 1–5 | Shares only |
| 6–15 | Shares, long calls, or short puts per `iv_ratio` |
| 16–20 | Shares or short puts only, never long calls |

As specified, implemented exactly.

### 5.4 Expiry selection

```
target_dte = clip(expected_horizon_days, 21, 90)
```

Nearest listed expiry within ±14 days; else fall back to shares and log it. Never selects inside 21 DTE.

### 5.5 Strike selection

By delta (`TARGET_PUT_DELTA = -0.30`, `TARGET_CALL_DELTA = 0.60`), nearest listed strike within ±0.10; else fall back to shares.

### 5.6 Sizing the options position

Delta-equivalent notional matching, as specified. Cash-secured put capital (`strike × 100 × contracts`) is tracked as an in-run reservation ledger (`options/sizing.py`, `AlpacaOptionsBroker.buying_power_reserved()`) — this is a same-run overcommit guard, not a persistent broker-side reservation, since Alpaca has no such primitive; stated explicitly rather than implied as stronger than it is.

### 5.7 Fallback to shares

As specified — chain-quality failure, IV solve failure, no expiry/strike within tolerance, `contracts < 1`, earnings before expiry (short puts specifically), or `OPTIONS_ENABLED=False`. Every fallback logged with its reason; `options_fallback_count` is now a permanent per-week ledger column (§16).

### 5.8 Skip the name entirely

As specified — liquidity, weight floor, volatility ceiling, data quality, tradability, pending corporate action, earnings within 2 trading days. `MAX_SKIP_FRACTION` (0.40) aborts the run if breached. Corporate-action and earnings checks are best-effort against whatever the provider exposes; an undetermined result is logged as `unknown, not gated`, never silently treated as safe.

---

## 6. Layer 3 — Monte Carlo risk

### 6.1 Why simulation is required here

As specified: option payoffs are non-linear, so `sigma_p = sqrt(wᵀΣw)` is invalid once options are in the book. The analytic path remains implemented behind `--risk-engine analytic`, used as the automatic fallback on any Monte Carlo failure (`risk/engine.py` wraps the simulation in a try/except specifically for this) and as the continuous cross-check logged every run (§11).

### 6.2 Path generation

`ql.pseudoSqrt` with spectral salvaging, both Sobol and Mersenne Twister generators implemented (`--mc-generator`), antithetic variates on by default, `mu = 0` by default. As specified.

### 6.3 Revaluing options along paths — known limitation, not built

**This is the one substantive gap against the original design.** §6.3 called for repricing each option along every simulated path via a precomputed fair-value grid, so the simulated CVaR reflects real option convexity (a long call) and tail risk (a short put). As actually built, `risk/montecarlo.py` aggregates simulated **asset** returns linearly (`asset_cum @ weights`) — it never reprices an option at all. The simulation is correctly built and tested for what it computes (it passes its own required analytic-agreement gate, §12), but that gate is specifically an equity-only, linear check — it doesn't exercise the missing piece.

Why it wasn't built: not a technical blocker — a real but bounded feature (a price grid per option, evaluated at the horizon date with correctly-reduced time-to-expiry, interpolated across simulated terminal prices, then combined with the linear share P&L into one portfolio return). Time went to the options *decision* layer and to fixing real execution bugs found live instead. Practical impact has been low so far because, per §5.1's observation, the options layer has landed on shares in effectively every real week logged to date — but this is a real gap that will matter more once options positions are actually held regularly. Fixing it is a self-contained, independently-verifiable engineering task (construct a known option position, confirm the fixed engine reproduces the correct convex/tail-heavy P&L shape) — it does **not** require waiting for the validation ledger's real-market data, which answers a different question (whether the ranking signal itself works), not this one (whether the risk engine's math is complete).

### 6.4 Risk measures

CVaR (expected shortfall) preferred over VaR, as specified. `risk/measures.py` implements both.

### 6.5 Sizing from CVaR

```
k_risk  = min(1.0, CVAR_TARGET / cvar_ann)
w_final = w_constrained * k_risk * k_regime
```

**`RISK_ENGINE` defaults to `montecarlo`, not `analytic` as originally specified** — an explicit, deliberate choice made ahead of the calibration step §13's build order recommends, before any live weight history existed to calibrate against. `sigma_p_analytic`/`sigma_p_simulated` are logged every run specifically so the gap stays visible while real weights accumulate — use it to judge whether `CVAR_TARGET` needs retuning, or whether reverting to `--risk-engine analytic` would have been the better call. **Not yet calibrated** — this remains open until enough weeks of real ledger data exist (§16).

---

## 7. Regime scalar

### 7.1–7.4

Computed independently from the CSV's own `regime` column (logged and cross-checked, never acted on), continuous scalar with the specified slope/floor, both dampers (3-day confirmation, 0.15/week rate limit), state persisted in `state/regime_state.json` with cold-start handling — all implemented as specified.

One change: the benchmark defaults to **`SPY`, not `^GSPC`**. `^GSPC` is a raw index ticker Alpaca's data feed cannot serve at all — every run paid for a guaranteed-fail call and a confusing log line, discovered live once daily automation made the noise visible daily instead of just once a week. The total-return-vs-price-index distinction that originally motivated `^GSPC` doesn't affect this signal in practice: it's a distance from SPY's own trailing SMA, both terms drawn from the same series, so the dividend-drag effect that distinction is about cancels out.

---

## 8. Execution

### 8.1 Order generation

```
trade if abs(delta_i) > max(25, 0.20 * target_value_i)
```

As specified. **Exit hysteresis (hold until rank 25, not rank 20) is not implemented** — structurally can't be, today: the weekly CSV only ever carries ranks 1–20, so there's no rank 21–25 data to check a dropped name against. Would require the upstream screen to report a top-25 list. Names absent from the CSV entirely are exited in full via `close_position`.

### 8.2 Ordering — mandatory, implemented exactly

Close options → equity sells → poll for confirmed fills → equity buys → open new options, in `trade_from_csv.py`'s `execute_actions()`. Two real bugs found live and fixed here: option orders originally priced at the mid never crossed the spread and sat unfilled; pricing exactly at the touch still wasn't reliably marketable in Alpaca's paper simulation — fixed to cross through by `max($0.01, 10% of spread)`.

### 8.3 Options-specific handling

Assignment monitoring (delta > 0.90 with an upcoming ex-dividend date), 21-DTE rolling flag, expiry-week flag — all implemented in `daily_monitor.py`, alert-only by explicit design (see §9.2).

### 8.4 Trade execution is never automatic — by design

`weekly.yml` computes `target_positions.csv` and stops. Nothing in the scheduled automation calls `trade_from_csv.py`. Submitting real (paper) orders requires a human to either run `trade_from_csv.py` directly or use the dashboard's "Approve & Execute" flow (§18), which requires an explicit review checkbox that resets after every use. This is the one deliberate, permanent human-in-the-loop checkpoint in an otherwise fully automated pipeline.

---

## 9. Cadence — fully automated

### 9.1 Weekly

Two scheduled jobs, ~75 minutes apart, both self-gating to the week's genuine first NYSE trading day via `scheduling/is_weekly_run_day.py` (holiday-aware — a holiday Monday shifts the run to Tuesday automatically, with no manual action):

1. **Weekly screen generation** (§17) — a Claude Code Routine, 11:00 UTC, produces `top20.csv`/`top20.md`.
2. **Weekly Sizing** (`weekly.yml`, GitHub Actions) — 12:15 UTC, runs `position_sizing.py`, writes `target_positions.csv`, archives the week, appends to the validation ledger (§16).

### 9.2 Daily — monitoring only

`daily_monitor.py`, via `daily.yml`, Tue–Fri at 12:45 UTC (deliberately staggered 30 minutes from Weekly Sizing's slot after both were found firing at the identical minute on overlapping days). Alert-only, never trades — recomputes `distance`/`k_regime` daily (required for §7.3's confirmation counter), checks delta drift, assignment risk, 21-DTE/expiry-week, and stop-loss.

Exits non-zero when it finds a warning — a deliberate choice to reuse GitHub's built-in failed-run email as a free alert channel rather than building a separate notification system. The cost of that choice is that a real alert and a genuine crash look identical in the Actions UI; mitigated by emitting a `::warning::`/`::notice::` GitHub annotation per alert, so the actual alert text renders as a distinct banner in the run summary rather than requiring a click into the raw log.

### 9.3 Event-driven

Same triggers as specified (21 DTE, stop-loss, assignment risk, `k_regime` moving >0.10/day) — surfaced as alerts for human review, not auto-acted-on, consistent with §8.4's design.

### 9.4 Reliability

`heartbeat.py` — every successful run records a timestamp; `scheduling/check_heartbeat.py` (run daily via `heartbeat.yml`) alerts if any of `weekly_sizing`/`daily_monitor`/`report` hasn't recorded one within 8 days. Full detail in §15.

---

## 10. Configuration

Actual current defaults (CLI flag names differ slightly from the original spec's config-var names in places — noted where they do):

| Name | Default | Section |
|---|---|---|
| `--position-cap` | 0.12 | §4.2 |
| `--position-floor` | 0.015 | §4.2 |
| (liquidity fraction) | 0.005 | §4.2, not a CLI flag |
| `--sigma-target` | 0.15 | §6.5 (analytic engine only) |
| `--use-risk-index` / `--use-sentiment-index` | off | §4.3, not yet wired |
| `--options-enabled` | off | §5 |
| `--equity-broker` / `--market-data` / `--options-broker` | `alpaca` | §1.1 |
| `--min-adv` | 5,000,000 | §5.8 |
| `--max-sigma` | 1.00 | §5.8 |
| `--max-skip-fraction` | 0.40 | §5.8 |
| `--iv-rich-threshold` / `--iv-cheap-threshold` | 1.25 / 0.85 | §5.1 |
| `--target-put-delta` / `--target-call-delta` | -0.30 / 0.60 | §5.5 |
| `--options-min-dte` / `--options-max-dte` | 21 / 90 | §5.4, §3.5 |
| `--risk-engine` | **`montecarlo`** | §6.5 — differs from original spec's `analytic` default, see §6.5 |
| `--cvar-target` | 0.25 | §6.5, not yet calibrated |
| `--cvar-alpha` | 0.95 | §6.4 |
| `--mc-paths` | 50,000 | §6.2 |
| `--mc-horizon-days` | 5 | §6.2 |
| `--mc-seed` | 42 | §6.2 |
| `--mc-generator` | `sobol` | §6.2 |
| `--mc-drift` | 0.0 | §6.2 |
| `--regime-benchmark` | **`SPY`** | §7.2 — differs from original spec's `^GSPC`, see §7 |
| `--regime-slope` | 5.0 | §7.2 |
| `--regime-floor` | 0.30 | §7.2 |
| `--regime-confirm-days` | 3 | §7.3 |
| `--regime-max-step` | 0.15 | §7.3 |
| `--force-regime` | none | §7, bypasses regime logic entirely, pins `k_regime` |

Also present: `--regime-dry-run` / `--risk-dry-run` (compute and log without applying to weights), `--force` (bypasses the §2.2 staleness guard — the everyday equivalent of the spec's `--dry-run` intent, since `position_sizing.py` never submits orders itself). The real order-submitting `--dry-run` flag lives on `trade_from_csv.py`, the only script capable of spending money.

---

## 11. Logging

Per run, persisted to `logs/run_<timestamp>.json` — CSV hash, per-name weight at every stage, instrument decision + `iv_ratio` + reason, option strike/expiry/delta/contracts/premium/capital reserved, `cvar_ann`/`k_risk`/`var_95`, `sigma_p` from both simulation and the analytic formula plus their difference (with a printed warning if they diverge >5% — treated as a bug, not a finding), regime state and the CSV's own `regime` value cross-checked, which constraints bound, turnover %, cash %, MC diagnostics. All as specified, plus the permanent weekly ledger described in §16, which the original design didn't have.

---

## 12. Tests

156 tests, `pytest -q`. Coverage against the original checklist:

- **Determinism, analytic-agreement gate, Sobol/Mersenne agreement, convergence (standard error ~1/√n), normal-case CVaR closed-form** — all present, `risk/tests/test_correctness.py`.
- **Validation** — extensive malformed-CSV coverage.
- **Regime** — monotonicity, bounds, confirmation-day gating, state persistence, cold start.
- **Option pricing** — American-vs-European bracketing, IV round-trip.
- **Execution ordering, no-trade band, capital reservation** — covered.
- **Not covered:** true property-based/fuzz testing (the spec asked for randomized-input property tests; what exists is thorough example-based testing instead). Grid-interpolation tests don't apply, since §6.3's grid was never built.

---

## 13. Build order — actual deviations, stated plainly

The original build order (§13 in the design docs) recommended: paper-trade equity-only for several weeks before adding the options layer, and run options **log-only** for several weeks before enabling real submission, switching to `RISK_ENGINE=montecarlo` only after calibration. **Both of those gates were explicitly skipped, by deliberate choice, in favor of building faster** — options execution was wired to submit real (paper) orders from the start, and the Monte Carlo engine went live as the default before any calibration history existed. Both choices are logged inline in the relevant modules' own docstrings, not hidden. The tradeoff: less soak time before trusting the numbers, in exchange for having a fully working system sooner. The validation ledger (§16) exists specifically to make up that soak time retroactively, with real data, once enough weeks accumulate.

---

## 14. Known limitations

Consolidated from throughout this document, so nothing is buried:

1. **§6.3 — Monte Carlo doesn't reprice options along paths.** The most substantive gap. CVaR is currently computed as if the book were linear even in weeks options are held. See §6.3 for the full reasoning and why it hasn't mattered much yet.
2. **`CVAR_TARGET` (0.25) has never been calibrated** against real weight history, and the Monte Carlo engine is the live default anyway (§6.5, §13).
3. **§8.1 exit hysteresis not implemented** — the CSV format itself doesn't carry the data needed (ranks 21–25).
4. **§4.3 index modifiers (risk/sentiment tilts) not built** — correctly deferred pending correlation data that doesn't exist yet.
5. **Single-provider (Alpaca only)** for equity, options, and market data — the abstraction supports more, none are built.
6. **No property-based testing** — the suite is thorough but example-based.
7. **The screening signal itself is unvalidated.** Do rank 1–5 names actually outperform 16–20? Does this beat equal-weight net of costs? Unknown until the validation ledger (§16) has 12–26 weeks of real data — deliberately not analyzed prematurely.

None of these are hidden failures — each is a real, acknowledged tradeoff made to ship a working system, documented here and in the code so a future decision to close any of them starts from an accurate picture rather than rediscovery.

---

## 15. Automation & reliability

Everything below runs unattended, on a schedule, with no manual trigger required — this section didn't exist in the original design and was built entirely during hardening.

### 15.1 GitHub Actions workflows (`.github/workflows/`)

| Workflow | Schedule | Does |
|---|---|---|
| `weekly.yml` | Mon–Fri 12:15 UTC, self-gated | Runs `position_sizing.py`, commits `target_positions.csv`/`history/`/ledger back to the repo. Compute-only — never trades (§8.4). |
| `daily.yml` | Tue–Fri 12:45 UTC | Runs `daily_monitor.py`. |
| `report.yml` | Friday 21:30 UTC | Runs `track_performance.py`, updates `history/performance_summary.csv`, backfills forward returns into the ledger. |
| `heartbeat.yml` | Mon–Fri | Runs `scheduling/check_heartbeat.py`. |

Each requires repo secrets `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` (and optionally `FINNHUB_API_KEY`) and "Read and write permissions" enabled under Settings → Actions, so the commit-back steps can push.

### 15.2 Reliability fixes found only by running this for real

None of these were anticipated in the original design; all were found by actually operating the automation and are worth listing because each is a genuinely common class of CI bug, not specific to this project:

- **Trailing whitespace in a pasted secret.** A GitHub Actions secret with a trailing newline (a common copy-paste artifact) reaches `requests` as a literal newline in the auth header, which is correctly rejected — `requests.exceptions.InvalidHeader`, not a normal auth error, which is a much more confusing failure to debug. Fixed by `.strip()`-ing credentials at every Alpaca client constructor (`brokers/alpaca_equity.py`, `brokers/alpaca_options.py`, `data/alpaca_data.py`), so it's fixed regardless of whether the stored secret itself is ever cleaned up.
- **Commit-back steps skipped on failure, not run.** A custom `if:` condition on a workflow step (e.g. `if: steps.gate.outcome == 'success'`) does *not* imply `always()` — GitHub's default "skip remaining steps after a failure" behavior still applies underneath it. `weekly.yml`'s commit-back step was silently skipped, not failed, whenever the sizing step itself errored — confirmed directly via the Actions API's step-level conclusions, not guessed. Fixed by making it `if: always() && steps.gate.outcome == 'success'`.
- **`git add -f state/` failing on a directory that was never created.** If the underlying script exits before writing anything (e.g. the credential bug above, before it was fixed), the commit-back step's own `git add` fails with `fatal: pathspec 'state/' did not match any files` — a second failure caused by the first. Fixed with `mkdir -p` before every `git add -f` in all three commit-back steps.
- **Two jobs scheduled at the identical minute.** `weekly.yml` and `daily.yml` originally both fired at 12:15 UTC on overlapping weekdays — a self-inflicted collision risk for their commit-back steps racing to push to `main` at the same moment. Fixed by staggering `daily.yml` to 12:45 UTC.
- **Push races, generally.** Any commit-back step can still be rejected by an unrelated concurrent push (another workflow, or a manual push). All three retry once after a rebase: `git push || (git fetch origin main && git rebase origin/main && git push)`.
- **Filesystem mtime is meaningless after a fresh checkout** — covered in full in §2.2.
- **A shallow checkout can't see far enough back in history** to answer "when did this specific file last change" correctly if a later, unrelated commit is now `HEAD` — `weekly.yml`'s checkout uses `fetch-depth: 0` specifically so the git-history staleness check (§2.2) gives a correct answer.

### 15.3 Local alternative (`scheduling/`)

Windows Task Scheduler wrapper scripts (`register_tasks.ps1` + `run_*.ps1`) exist as a dev/local alternative to GitHub Actions — the spec's own recommendation is to treat GitHub Actions as authoritative for anything that matters, since a local scheduled task stops the moment the laptop is closed.

### 15.4 Visibility

The Streamlit dashboard's **Automation** tab (§18) shows heartbeat freshness per job and live GitHub Actions run status side by side, so scheduling health is checkable without leaving the dashboard.

---

## 16. Validation ledger & performance tracking

Not present in the original design. Built specifically to answer §14 item 7 — whether the screening signal works at all — without waiting to build the analysis until enough data exists to need it.

### 16.1 `validation_ledger.py`

Writes two permanent, append-only (upsert-on-same-day, so repeated same-day test runs don't duplicate) CSVs on every successful weekly run:

- **`history/validation_ledger.csv`** — one row per week: portfolio value, cash %, `k_vol`/`k_risk`/`k_regime`, turnover %, estimated cost bps, instrument-mix counts, skip counts by gate, MC diagnostics, `cvar_ann`, regime crossings YTD. `actual_return_1w`/`equal_weight_return_1w` start blank and are backfilled once a week has actually elapsed.
- **`history/validation_ledger_per_name.csv`** — one row per (week, ticker): rank, weight, instrument, **`iv_ratio` and the fallback `reason`** (added specifically to make "is the IV threshold ever actually crossed" queryable directly, instead of hand-parsing archived CSVs), `forward_return_1w`/`forward_return_4w` (backfilled).

### 16.2 `track_performance.py`

Computes actual vs. equal-weight vs. benchmark (SPY) return per archived week, writes `history/performance_summary.csv`, backfills the ledger's forward-return columns. Structurally incapable of submitting an order — it only ever imports a read-only `MarketData` adapter, never a broker.

### 16.3 Deliberately not built yet

The actual statistical decision rules — rank-decile regression, equal-weight beat-rate, `CVAR_TARGET`/threshold recalibration — do not exist as code. The spec's own stated philosophy is that these need 12–26 weeks of real data before they mean anything; writing analysis code against zero rows of data would be guesswork; that data is now being collected automatically. Come back to this once `validation_ledger.csv` has enough rows.

---

## 17. Automated weekly screen generation

Not present in the original design at all — Layer 1 (§0) was assumed to arrive from an external, human-run process. It's now itself automated.

A **Claude Code Routine** (`Artemis Discovery - Weekly Top20 CSV Generation`), scheduled weekdays at 11:00 UTC — ~75 minutes before `weekly.yml` consumes its output — reuses `scheduling/is_weekly_run_day.py`'s exact gating logic, reads current holdings from the most recent `history/<date>/target_positions.csv`, runs the same volatility screen as §2's CSV schema expects (via the Bigdata.com MCP connector plus WebSearch/WebFetch), and commits `top20.csv`/`top20.md` plus dated archive copies back to the repo.

Because a Routine's configuration lives in Claude's hosted UI, not a versioned file, **`mdinstructions/weekly-csv-generation-routine.md` is the source of truth for what it actually does** — if the routine's instructions change, that file must change in the same commit, or the two silently drift. `volatility-prompt.md` (repo root) is the same core screening prompt without the automation wrapper, for manual/ad hoc use.

Nothing yet validates the generated CSV's shape before committing it beyond the prompt's own explicit formatting rules — `position_sizing.py`'s own §2.2 gate is the actual backstop if a bad file ever gets through (content-hash and schema validation both fire before anything downstream trusts it).

---

## 18. Dashboard

Not present in the original design. `dashboard.py`, `streamlit run dashboard.py` — mostly read-only, with one deliberate exception.

**Read-only tabs:** Overview (account/regime), Positions, Latest Decision (reads the most recent run log), Performance, Ledger Trends, Alerts (daily monitor's recent alerts, severity-colored), Automation (heartbeat freshness + live GitHub Actions status).

**The one order-capable path:** the Positions tab's "Rebalance to target_positions.csv" section previews the plan `trade_from_csv.py` would execute, then requires an explicit review checkbox — which resets after every use — before an "Approve & Execute" button (disabled until checked) submits real paper orders via the same `execute_actions()` logic the CLI uses. Every execution is logged to `logs/dashboard_execution_*.json`. This is the human-in-the-loop checkpoint described in §8.4, made accessible without needing the CLI.
