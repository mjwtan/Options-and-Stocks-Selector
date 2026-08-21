# Systematic Equity & Options Portfolio — System Specification

**Version:** 1.0
**Execution venue:** Alpaca (paper first)
**Cadence:** Weekly full run, daily monitoring
**Language:** Python 3.11+

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
CSV (20 ranked names)
  → validate
  → fetch bars + option chains
  → compute realised vol, covariance
  → raw weights from rank / vol
  → constraints
  → per-name instrument choice (QuantLib IV vs realised)
  → Monte Carlo portfolio simulation → CVaR → k_risk
  → regime scalar → k_regime
  → final positions
  → orders (sells, confirm, then buys)
```

---

## 1. Providers and abstraction

### 1.1 Design requirement — swappable, independent adapters

Equity and options are handled by **separate adapters behind separate interfaces**. The system must run equity-only with `OPTIONS_ENABLED=False` and no options provider configured at all — not merely with the options code path skipped, but with no options dependency imported or credentialed.

```
brokers/
  base.py               # EquityBroker, OptionsBroker, MarketData protocols
  alpaca_equity.py
  alpaca_options.py
  ibkr_equity.py        # if needed
  tradier_options.py    # if needed
data/
  base.py
  alpaca_data.py
  polygon_data.py
  tradier_data.py
```

Three protocols, deliberately separate:

```python
class MarketData(Protocol):
    def daily_bars(self, symbols, start, end) -> DataFrame: ...
    def latest_quote(self, symbol) -> Quote: ...

class EquityBroker(Protocol):
    def positions(self) -> list[Position]: ...
    def account(self) -> Account: ...
    def submit_notional(self, symbol, notional, side) -> Order: ...
    def close_position(self, symbol) -> Order: ...
    def order_status(self, order_id) -> Status: ...

class OptionsBroker(Protocol):
    def chain(self, symbol, expiry_range) -> list[Contract]: ...
    def submit_option(self, contract, qty, side) -> Order: ...
    def option_positions(self) -> list[OptionPosition]: ...
    def buying_power_reserved(self) -> Decimal: ...
```

Nothing in the sizing, risk, or regime layers may import a provider module directly. They receive an adapter instance. This is what makes the equity-only mode genuinely independent and what allows a provider swap without touching strategy code.

**Equity broker, options broker, and market data may be three different vendors.** This is the common configuration in practice — see §1.4.

### 1.2 Equity execution

| Provider | Cost | Notes |
|---|---|---|
| **Alpaca** | Free | Commission-free US equities, fractional via notional orders, free paper trading, clean REST. Default choice |
| **IBKR** | Paid | Required only for non-US listings. UK residents get IBKR Pro (no Lite tier): ~£3/trade European, ~$0.35 min US. No fractional on most non-US venues. Heavier integration — TWS/Gateway process or session auth |
| **Tradier** | $10/mo + $0.35/contract | Brokerage Plus tier. Strongest when options are the focus — see §1.3 |

Fractional shares matter here. Rank-weighted sizing produces awkward target values; without fractional support every position rounds to whole shares and small positions distort. Alpaca and IBKR (US only) support it; most others do not.

### 1.3 Options execution and chain data

This is where paid providers earn their cost. Alpaca's options offering is materially thinner than its equities offering.

| Provider | Cost | Assessment |
|---|---|---|
| **Alpaca Options** | Free | Adequate for basic chains. Greeks and IV not consistently provided; historical options data limited. Viable to start, likely to constrain later |
| **Tradier** | $10/mo + $0.35/contract | Purpose-built for options. Full chains with bid/ask/greeks/IV, good documentation, sandbox environment. Best value for this use case |
| **Polygon.io Options** | $29–199/mo | Data only, no execution. Excellent chain quality, historical IV, tick data. Pair with a separate execution broker |
| **IBKR** | Paid | Comprehensive chains and global coverage. Heaviest integration burden |
| **ORATS** | $99+/mo | Specialist options analytics — clean IV surfaces, historical vol data. Overkill unless the IV signal becomes the core of the strategy |

**Recommended starting configuration:** Alpaca for equity execution and equity bars (free), Tradier for options chains and options execution ($10/mo). Total cost is trivial and each vendor is used where it is strongest.

**If the IV signal proves to be where the edge is,** upgrade options data to Polygon or ORATS for historical IV — which you need to establish whether an `iv_ratio` of 1.3 is genuinely rich for that name or normal for it. This is the single most likely paid upgrade to be worth making.

### 1.4 Verification before building

Test each provider against real calls before committing to it:

1. **Equity bars** — pull 300 daily bars for 20 tickers in one request. Confirm adjusted closes, and check the adjustment convention (split-only vs. split-and-dividend). A mismatch between your volatility source and covariance source is subtle and wrong.
2. **Option chains** — pull a full chain for a mid-cap name. Confirm bid, ask, open interest, and at least two expiries between 21 and 90 days, with multiple strikes surviving the §3.5 quality filters.
3. **Greeks and IV** — check whether the provider supplies them. If so, use them as a cross-check against QuantLib's own calculation; if the two diverge materially, investigate before trusting either.
4. **Paper/sandbox** — confirm a full order lifecycle works end to end in the sandbox for both equity and options.
5. **Approval level** — confirm the account permits cash-secured puts and long calls. These are typically the lowest options tiers, but confirm rather than assume.

**If step 2 fails on all available providers, the options layer is not viable.** Run equity-only. That is a legitimate outcome, and the adapter design means it costs nothing to fall back.

---

## 2. Layer 1 — CSV ingestion

### 2.1 Schema

Delivered weekly. One row per stock.

| Column | Type | Definition |
|---|---|---|
| `ticker` | string | Must match broker symbology exactly |
| `ranking` | int 1–20 | Conviction order, 1 = highest. Strict, no ties or gaps |
| `regime` | int 0/1 | Market-wide flag. Logged, **not used** — see §7.1 |
| `risk_index` | float | Higher = riskier |
| `volatility_index` | float | Higher = more volatile |
| `sentiment_index` | float | Higher = more positive |
| `expected_horizon_days` | int | How long the thesis is expected to take to play out |

`expected_horizon_days` is new and load-bearing: it drives option expiry selection (§5.4). Without it, expiry is chosen by convention rather than by the actual view.

### 2.2 Validation gate

Refuse to trade and alert on any of:

- Row count ≠ 20 (warn and proceed between 10 and 19; abort below 10)
- Duplicate tickers
- Any ticker failing an Alpaca asset lookup or flagged non-tradable
- `ranking` not forming a strict 1..N sequence
- `regime` not uniform across rows, or not in {0, 1}
- Any index column non-numeric or null
- `expected_horizon_days` missing, or outside 5–365
- Missing bars for any ticker, or a gap longer than the exchange calendar allows

**Staleness:** the file carries no timestamp. Guard with both a content hash compared against the previous run, and a file mtime within 3 days. Neither alone suffices — an unchanged pick list produces an identical hash legitimately, and a touched-but-stale file passes mtime. Alert rather than proceed when either fails.

A malformed file must never reach order submission. Log the reason, exit non-zero.

---

## 3. Layer 2a — Market data

All computed from our own sources. Nothing here comes from the CSV.

### 3.1 Equity bars

~300 daily bars per ticker plus the benchmark, from Alpaca. One bulk call.

```
r_t = ln(P_t / P_{t-1})
```

### 3.2 Realised volatility (EWMA, 21-day half-life)

```
lam = 0.5 ** (1/21)                     # ≈ 0.9674
var = variance(r[0:60])                 # seed
for t in remaining:
    var = lam * var + (1 - lam) * r_t**2

sigma_i = clip(sqrt(var * 252), 0.12, 0.80)
```

The floor is load-bearing — without it an unusually quiet stock receives an enormous inverse-vol weight from what is probably a temporary lull.

### 3.3 Covariance

```python
Sigma = LedoitWolf().fit(returns_250d).covariance_ * 252
```

Shrinkage is required, not optional: with 20 assets and 250 observations a raw sample covariance is unstable. QuantLib has no shrinkage estimator, so sklearn does this.

### 3.4 Calendar

Use QuantLib for all date arithmetic:

```python
cal = ql.UnitedStates(ql.UnitedStates.NYSE)
cal.advance(today, ql.Period(-200, ql.Days))      # exact 200-session lookback
cal.businessDaysBetween(d1, d2)                   # expected bar count
```

Confine QuantLib date usage to one module with `datetime` conversion helpers at the boundary. `ql.Date` does not interoperate with pandas timestamps.

**Pre-run guard:** abort if today is not a trading day, or if the previous session's bars have not arrived.

### 3.5 Option chains

Per ticker: strikes, expiries, bid, ask, open interest, volume.

Quality filters — discard any contract failing these, and skip the options layer for a name if too few contracts survive:

- Bid > 0 and ask > 0
- Relative spread `(ask - bid) / mid` ≤ 0.15
- Open interest ≥ 100
- Expiry between 21 and 90 days out

---

## 4. Layer 2b — Equity weights

### 4.1 Raw weights

The ranking is treated as a high-conviction signal, so weighting is linear in rank.

```
raw_i = (21 - ranking_i) / sigma_i
w_i   = raw_i / sum(raw)
```

Rank 1 contributes 20 units, rank 20 contributes 1, before volatility adjustment.

### 4.2 Constraints

Apply in order, renormalise after each pass, iterate until stable or 5 passes:

```
w_i <= 0.12                             # position cap
w_i >= 0.015  else drop the name        # floor; below this, costs dominate
w_i * V <= 0.005 * adv20_i              # liquidity: exitable in one day
```

Names dropped by the floor are expected. Under linear rank weighting the portfolio will typically hold **12–15 names, not 20**. This is intentional — concentration is how conviction is expressed.

Log which constraint bound for each name.

### 4.3 Optional index modifiers — default OFF

`risk_index` and `sentiment_index` may be layered on. Implement behind a config flag, disabled initially.

Normalise to within-week percentiles across the 20 rows, which neutralises scale drift:

```
r_mult_i = 1 - 0.20 * percentile(risk_index_i)      # 1.00 → 0.80
s_mult_i = 0.92 + 0.16 * percentile(sentiment_i)    # 0.92 → 1.08
```

Before enabling, check the correlation between the risk percentile and our own `sigma_i` percentile across several weeks. Above 0.8 they measure the same thing and applying both double-penalises volatile names — leave `risk_index` off permanently in that case.

Sentiment is the noisiest input and likely already inside `ranking`. Treat enabling it as an experiment.

---

## 5. Layer 2c — Instrument decision (QuantLib)

### 5.0 The decision

For each of the 20 names, the system chooses one of four outcomes:

| Outcome | When |
|---|---|
| **Buy shares** | Default. No options edge, or options unavailable |
| **Sell cash-secured put** | Options expensive relative to our volatility estimate |
| **Buy call** | Options cheap relative to our volatility estimate |
| **Skip entirely** | The name fails a hard gate — see §5.8 |

The skip outcome is not a fallback; it is a real decision. A name in the CSV is a suggestion, not an obligation. Weight freed by a skip is redistributed across the remaining names by renormalising §4.2.

Log the outcome and its reason for all 20 names every run, including the skips.

### 5.1 The signal

Compare **implied volatility from market quotes** against **our realised volatility estimate** (§3.2).

```
iv_ratio = iv_atm / sigma_realised
```

| `iv_ratio` | Interpretation | Action |
|---|---|---|
| > `IV_RICH_THRESHOLD` (1.25) | Options expensive | Sell cash-secured put to enter |
| < `IV_CHEAP_THRESHOLD` (0.85) | Options cheap | Buy call for leveraged exposure |
| Between | No edge | Buy shares |

This is not a claim of arbitrage. The ratio compares the market's forward-looking view against our backward-looking estimate, and the two legitimately differ — particularly around known events. It identifies where the options market prices risk differently from recent history, which is a weaker but real signal.

**Caveat worth knowing:** without historical IV for the name, an `iv_ratio` of 1.3 cannot be distinguished from that name's normal level. Some tickers persistently trade rich. This is the strongest argument for a paid data provider with IV history (§1.3) once the basic system works.

### 5.2 Pricing engine — Black-Scholes

**Use Black-Scholes as the default engine.** This is a deliberate simplification with a real justification and a real limitation, both stated here.

```python
payoff   = ql.PlainVanillaPayoff(ql.Option.Call, strike)
exercise = ql.EuropeanExercise(expiry_date)
option   = ql.VanillaOption(payoff, exercise)

process = ql.BlackScholesMertonProcess(
    ql.QuoteHandle(ql.SimpleQuote(spot)),
    dividend_ts, risk_free_ts, vol_ts
)
option.setPricingEngine(ql.AnalyticEuropeanEngine(process))

iv = option.impliedVolatility(market_mid, process,
                              accuracy=1e-4, maxEvaluations=100)
```

**Why it is acceptable:**

- For a non-dividend-paying underlying, an American call has no early-exercise value — Black-Scholes is exact, not an approximation.
- The pricing is closed-form rather than iterative, which is roughly two orders of magnitude faster than a binomial tree. This matters enormously in §6.3, where options are revalued across simulated paths.
- Greeks are analytic rather than computed by finite difference, so they are both faster and more numerically stable.

**Where it is wrong:**

- American **puts** always carry early-exercise value. Black-Scholes underprices them, and the error grows with moneyness, dividend yield, and time to expiry. Since short puts are one of the three expressions, this is a live limitation, not a hypothetical one.
- Dividend-paying underlyings introduce early-exercise value for calls too, concentrated around ex-dividend dates.

**Required mitigation.** Implement a binomial engine behind `PRICING_ENGINE=binomial` and run a comparison harness across the realistic parameter range — 21–90 DTE, 0.20–0.30 delta, dividend yields 0–4%. Record the maximum observed pricing difference.

- If the gap stays under roughly 2% of premium, Black-Scholes is fine as the production engine.
- If it exceeds that on short puts, use binomial for **strike selection and entry pricing** on puts specifically, and Black-Scholes everywhere else including the Monte Carlo grid.

This is a measurable question with a measurable answer. Do not settle it by argument — run the comparison, record the numbers, and put them in the repo.

**Inputs required:** spot, dividend yield, risk-free rate (current T-bill yield, refreshed weekly), market mid price.

**Handle failures explicitly.** `impliedVolatility` throws when no solution exists in range — common with stale or crossed quotes. Catch, log the contract, exclude it. Never substitute a default value.

**ATM definition:** the strike closest to spot among surviving contracts at the chosen expiry. If the nearest is more than 3% from spot, interpolate IV between the two bracketing strikes rather than using a distant one.

### 5.3 Ranking-based instrument restriction

Not every name should get an options expression. The ranking decides:

| Rank | Permitted expressions |
|---|---|
| 1–5 | **Shares only.** Highest conviction — do not cap upside or add complexity |
| 6–15 | Shares, long calls, or short puts per the `iv_ratio` signal |
| 16–20 | Shares or short puts only. Never long calls |

The rank 1–5 restriction is deliberate. If the ranking has edge, that edge most likely lives in the top names making large moves. Capping or complicating those positions works against the signal generating returns.

### 5.4 Expiry selection

Driven by `expected_horizon_days` from the CSV:

```
target_dte = clip(expected_horizon_days, 21, 90)
```

Choose the listed expiry closest to `target_dte`. If nothing lists within ±14 days of target, fall back to shares for that name and log it.

Never select an expiry inside 21 days. Gamma rises sharply near expiry and the position requires intraday management the system does not provide.

### 5.5 Strike selection

**By delta, not by percentage from spot.** A fixed percentage means different things on a volatile name than a stable one; delta normalises for that.

```
Short puts:  target delta ≈ -0.30   (roughly 30% assignment probability)
Long calls:  target delta ≈  0.60   (enough directional exposure to matter)
```

Compute delta via QuantLib Greeks, select the listed strike whose delta is nearest target. If no strike falls within ±0.10 of target, fall back to shares.

### 5.6 Sizing the options position

Options are not interchangeable with shares at equal notional. Convert on a **delta-equivalent** basis so the portfolio's directional exposure matches the equity weights computed in §4.

```
target_notional_i = w_i * V

# Long call
contracts = round(target_notional_i / (delta * spot * 100))

# Short put — capital committed is strike, not spot
contracts = round(target_notional_i / (strike * 100))
```

**Cash-secured puts tie up `strike × 100 × contracts` in cash.** This must be reserved and excluded from buying power for other positions. Failing to account for it is the most likely way this system overcommits capital.

Round down. If `contracts < 1`, fall back to shares.

### 5.7 Fallback to shares

Use shares instead of options whenever any of these hold. These are not failures — shares remain a perfectly good expression.

- Chain data missing or fails the §3.5 quality filters
- IV solve failed for the ATM contract
- No expiry within ±14 days of target
- No strike within ±0.10 delta of target
- `contracts` would round to 0
- **Earnings fall before expiry** — IV inflates into a print and collapses after, which the `iv_ratio` signal misreads as richness. Fetch earnings dates and exclude affected names from short puts specifically
- `OPTIONS_ENABLED=False`

Log every fallback with its reason. A persistently high fallback rate means the options layer is not earning its place and should be reconsidered.

### 5.8 Skip the name entirely

Distinct from §5.7. These are hard gates — do not buy the name in any form, whatever its ranking says.

| Gate | Threshold | Rationale |
|---|---|---|
| Liquidity | `adv20 < MIN_ADV` (default $5m) | Cannot exit without moving the price |
| Weight floor | `w_i < POSITION_FLOOR` after constraints | Costs dominate the position |
| Volatility ceiling | `sigma_i > MAX_SIGMA` (default 1.00) | Beyond the risk model's useful range |
| Data quality | Bars missing, stale, or gapped beyond calendar expectation | Every downstream number is unreliable |
| Tradability | Broker flags the asset non-tradable, halted, or hard-to-borrow | Cannot execute |
| Pending corporate action | Announced merger, acquisition, or delisting | Price no longer reflects fundamentals; the thesis is void |
| Earnings inside 2 trading days | — | Overnight gap risk the weekly cadence cannot manage |

**Redistribution.** Weight freed by skipped names is redistributed by renormalising across survivors, then re-running the §4.2 constraint loop. Do not simply hold the freed weight in cash — that silently reduces exposure without the risk layer accounting for it.

**Guard rail.** If more than `MAX_SKIP_FRACTION` (default 0.40, i.e. 8 of 20) are skipped, abort the run and alert. That many gate failures indicates a data problem or a broken upstream file, not twenty genuinely unsuitable stocks.

Log skips with their gate and the value that triggered it. Skip rate by gate is a useful diagnostic — a rising liquidity-skip rate, for instance, means the upstream screen is drifting toward smaller names than the system can handle.

---

## 6. Layer 3 — Monte Carlo risk

### 6.1 Why simulation is required here

With options in the book, `sigma_p = sqrt(wᵀΣw)` is no longer valid. Option payoffs are non-linear: a long call is convex, a short put has a fat left tail, and variance is the wrong summary statistic for either. Simulation is the correct tool, not an embellishment.

Keep the analytic path implemented behind `--risk-engine analytic` as a reference for validation and as a fallback for equity-only runs.

### 6.2 Path generation

```python
ql_cov = ql.Matrix(n, n)                    # populate from Sigma_daily
L = ql.pseudoSqrt(ql_cov, ql.SalvagingAlgorithm.Spectral)
```

`SalvagingAlgorithm.Spectral` repairs non-positive-semi-definite matrices by flooring negative eigenvalues. This will occur occasionally with real data; without salvaging the run crashes. Log whenever salvaging alters the matrix materially — it signals a data problem.

Use Sobol low-discrepancy sequences — faster convergence means fewer paths for equivalent accuracy, which matters directly for the performance target.

```python
rsg = ql.SobolRsg(n_assets * n_steps, MC_SEED)
```

Also implement Mersenne Twister behind a flag as a convergence cross-check. If the two disagree beyond Monte Carlo error, the implementation is wrong.

Multivariate GBM per step:

```
z   = L @ independent_normals
r_t = (mu - 0.5 * sigma**2) * dt + sigma * sqrt(dt) * z
```

**Set `mu = 0` by default.** Expected-return estimates are unreliable at this horizon and inject a directional view into what should be a risk measurement. Configurable, but document that non-zero drift means the CVaR figure is no longer a pure risk number.

### 6.3 Revaluing options along paths

The expensive part. For each path, at horizon, each option must be repriced at the simulated underlying level.

**Do not reprice with a full pricing engine per path per position.** Even with closed-form Black-Scholes, 50,000 paths × 15 positions is 750,000 evaluations per run.

Instead, precompute a **price grid** per option — fair value across a range of underlying prices (default 61 points spanning ±30% of spot) — then interpolate along paths. Build the grid once, interpolate 50,000 times.

Black-Scholes (§5.2) makes this substantially cheaper than a tree-based engine would: grid construction is 61 closed-form evaluations rather than 61 binomial trees. This is a large part of why the analytic engine is the default.

Grid resolution is a config parameter. Validate that interpolation error is immaterial relative to Monte Carlo standard error — cubic spline interpolation on 61 points is normally more than sufficient.

### 6.4 Risk measures

```python
losses  = -portfolio_returns
var_95  = np.percentile(losses, 95)
cvar_95 = losses[losses >= var_95].mean()

cvar_ann = cvar_95 * sqrt(252 / MC_HORIZON_DAYS)
```

CVaR (expected shortfall) is preferred over VaR: it is coherent as a risk measure (VaR fails subadditivity) and it responds to tail shape rather than a single quantile — which is the point when the book contains short puts.

### 6.5 Sizing from CVaR

```python
k_risk  = min(1.0, CVAR_TARGET / cvar_ann)
w_final = w_constrained * k_risk * k_regime
```

`CVAR_TARGET` default **0.25**. Cap at 1.0 — never lever.

**Calibrate before trusting it.** Run both engines over several months of historical weights and compare `k_vol` against `k_risk` on an equity-only book. If `k_risk` is systematically lower, the target is too tight and the portfolio will sit in cash. Tune so the two produce similar average exposure in normal conditions — then the difference appears only under stress, which is the intent.

---

## 7. Regime scalar

### 7.1 Compute it ourselves

**Do not use the CSV's `regime` column as a trading input.** We hold the benchmark bars, we control the as-of date, and the upstream definition has already drifted once (per-stock in one version, market-wide in another).

Read it, log it, compare, alert on disagreement. Never act on it.

### 7.2 Continuous scalar

```
sma200   = mean(benchmark_closes[-200:])
distance = (close - sma200) / sma200

k_raw    = 0.5 + REGIME_SLOPE * distance         # slope = 5.0
k_target = clip(k_raw, REGIME_FLOOR, 1.0)        # floor = 0.30
```

| distance | k_regime |
|---|---|
| +10% | 1.00 |
| +4% | 0.70 |
| 0% | 0.50 |
| −4% and below | 0.30 |

The floor is deliberate: never going fully to cash means a V-shaped recovery does not leave the book on the sidelines — the 200-day filter's worst-documented failure, since the largest up-days cluster near bottoms while the signal still reads risk-off.

### 7.3 Dampers — both required

**Confirmation.** `k_regime` may not cross 0.5 in either direction until the benchmark has closed on the new side for 3 consecutive trading days. This requires computing `distance` **daily**, not only on rebalance days.

**Rate limit.** `k_regime` may change by at most `REGIME_MAX_STEP` (0.15) per rebalance. A crash moves 1.00 → 0.85 → 0.70 → 0.55 → 0.40 → 0.30 over five weeks rather than in one step.

### 7.4 State

`k_regime`, `crossing_day_count`, and the previous `distance` sign persist between runs. Store as JSON or SQLite alongside run logs.

**Cold start:** set `prev_k_regime = k_target`, `crossing_day_count = REGIME_CONFIRM_DAYS`. Log clearly.

**Missed runs:** state older than 10 days is treated as a cold start rather than applying one 0.15 step against stale state.

### 7.5 Interaction with the screen

The upstream selection screen already filters on price > 200-day SMA per stock, so in a genuine downtrend most candidates fail independently and the list shrinks or arrives empty on its own.

A short or empty CSV in a downtrend is **expected**, not a fault. It must still fail the §2.2 validation gate rather than being read as "sell everything," but the alert should distinguish:

- Empty file + `distance < 0` → expected, informational
- Empty file + `distance > 0` → upstream fault, investigate

Do not double-count. The scalar cuts *total* exposure; it must not also penalise individual stocks for the market regime.

---

## 8. Execution

### 8.1 Order generation

```
target_value_i  = w_final_i * V
current_value_i = from broker positions
delta_i         = target_value_i - current_value_i

trade if abs(delta_i) > max(25, 0.20 * target_value_i)
```

**Exit hysteresis:** do not sell a held name until it falls out of the **top 25** of the ranking, not the top 20. A name oscillating around rank 20 would otherwise generate a round trip every week. Names absent from the file entirely are exited in full.

### 8.2 Ordering — mandatory

1. Close options positions requiring exit
2. Submit equity sells
3. **Poll until fills confirm** (timeout → abort remaining, alert)
4. Submit equity buys
5. Submit new options positions last

Options go last because cash-secured puts require confirmed buying power. Buying before sells settle causes rejections and can self-cross.

Full equity exits use `close_position`, not a computed notional, to avoid fractional dust. Use `notional` orders for equity to get fractional precision; options are integer contracts only.

Skip any equity order below ~£20 notional.

### 8.3 Options-specific handling

**Assignment monitoring.** Deep in-the-money short calls are exercised early to capture dividends. Check daily for short options with delta > 0.90 and an ex-dividend date before expiry; alert for manual review.

**Rolling.** At 21 DTE, either close or roll to the next expiry. Do not hold into the final three weeks — gamma risk rises and the position needs management the daily job cannot provide.

**Expiry week.** Flag all positions expiring within 5 trading days in the daily monitoring report.

---

## 9. Cadence

### 9.1 Weekly — full run

Pre-market Monday. Ingest CSV, recompute everything, rebalance equity, evaluate options.

### 9.2 Daily — monitoring only

Pre-market every trading day. **No trading except on triggers.**

- Recompute `distance` and update `crossing_day_count` (required for §7.3 confirmation)
- Check option deltas for material drift
- Check assignment risk (§8.3)
- Flag positions at 21 DTE or under
- Check stop-loss breaches

### 9.3 Event-driven

Act outside the weekly cycle only for: option at 21 DTE, stop-loss breach, assignment risk, or `k_regime` moving more than 0.10 in a day.

### 9.4 Why not daily rebalancing

The thesis horizon is weeks; the signal does not refresh meaningfully day to day. Daily rebalancing on volatile names costs roughly 5–6% annually versus ~1% weekly, before options widen it further — single-name option spreads often run 5–10% of premium.

**Measure this rather than assume it.** Track overlap between consecutive days' top-20 lists. If Monday and Tuesday share 18 of 20 names, daily trading generates trades from noise. If they share 12, the signal genuinely is fast-moving and the cadence is worth revisiting.

---

## 10. Configuration

| Name | Default | Section |
|---|---|---|
| `POSITION_CAP` | 0.12 | §4.2 |
| `POSITION_FLOOR` | 0.015 | §4.2 |
| `LIQUIDITY_FRAC` | 0.005 | §4.2 |
| `NO_TRADE_BAND` | 0.20 | §8.1 |
| `MIN_TRADE_ABS` | 25 | §8.1 |
| `EXIT_RANK` | 25 | §8.1 |
| `EWMA_HALFLIFE` | 21 | §3.2 |
| `COV_WINDOW_DAYS` | 250 | §3.3 |
| `USE_RISK_INDEX` | False | §4.3 |
| `USE_SENTIMENT_INDEX` | False | §4.3 |
| `OPTIONS_ENABLED` | False | §5 |
| `EQUITY_BROKER` | `alpaca` | §1.1 |
| `OPTIONS_BROKER` | `none` | §1.1 |
| `MARKET_DATA` | `alpaca` | §1.1 |
| `MIN_ADV` | 5_000_000 | §5.8 |
| `MAX_SIGMA` | 1.00 | §5.8 |
| `MAX_SKIP_FRACTION` | 0.40 | §5.8 |
| `PRICING_ENGINE` | `black_scholes` | §5.2 |
| `IV_RICH_THRESHOLD` | 1.25 | §5.1 |
| `IV_CHEAP_THRESHOLD` | 0.85 | §5.1 |
| `BINOMIAL_STEPS` | 200 | §5.2, comparison only |
| `TARGET_PUT_DELTA` | -0.30 | §5.5 |
| `TARGET_CALL_DELTA` | 0.60 | §5.5 |
| `MIN_DTE` | 21 | §5.4 |
| `MAX_DTE` | 90 | §5.4 |
| `RISK_ENGINE` | `analytic` | §6 |
| `CVAR_TARGET` | 0.25 | §6.5 |
| `CVAR_ALPHA` | 0.95 | §6.4 |
| `MC_N_PATHS` | 50000 | §6.2 |
| `MC_HORIZON_DAYS` | 5 | §6.2 |
| `MC_SEED` | 42 | §6.2 |
| `MC_DRIFT` | 0.0 | §6.2 |
| `MC_GRID_POINTS` | 61 | §6.3 |
| `REGIME_SLOPE` | 5.0 | §7.2 |
| `REGIME_FLOOR` | 0.30 | §7.2 |
| `REGIME_CONFIRM_DAYS` | 3 | §7.3 |
| `REGIME_MAX_STEP` | 0.15 | §7.3 |

All config-driven, none hardcoded.

**CLI:** `--dry-run` (compute and print, submit nothing), `--risk-engine`, `--mc-paths`, `--mc-seed`, `--force-regime`, `--no-options`.

`--dry-run` is the most important flag in the system. For anything that spends real money, being able to see what it would do before letting it do it is a safety feature, not a convenience.

---

## 11. Logging

Per run, persisted:

- CSV hash, file mtime, row count
- Per name: `sigma_i`, rank, weight at each stage (raw → constrained → after `k_risk` → after `k_regime`)
- Per name: instrument chosen, `iv_ratio`, and fallback reason if applicable
- Options: strike, expiry, delta, contracts, premium, capital reserved
- `cvar_ann`, `k_risk`, `var_95`
- `sigma_p` from simulation **and** from the analytic formula, plus the difference
- `distance`, `k_raw`, `k_target`, `k_regime`, previous `k_regime`, delta applied
- CSV `regime` value and whether it agreed with ours
- Which constraints bound
- Intended vs. actual fills
- Turnover as % of portfolio value; cash %
- MC: `n_paths`, seed, generator, `elapsed_ms`

The simulated-versus-analytic volatility comparison should be logged **every run**, not only in tests. It is a continuous correctness check costing nothing.

---

## 12. Tests

**Determinism.** Same CSV, same bars, same seed → byte-identical weights.

**Constraints.** Property test across randomised inputs: no weight exceeds the cap, falls below the floor, or breaches the liquidity limit. `sum(w_final) + cash == 1.0` within tolerance.

**Validation.** Malformed CSVs (short, duplicated, repeated hash, non-uniform regime, bad rankings, null indices, missing horizon) all fail closed and submit zero orders.

**Regime.** `k_regime` monotonic in `distance`; never above 1.0 or below the floor; never changes more than `REGIME_MAX_STEP` between runs. A single day's crossing does not move it across 0.5; three consecutive days does. State persists — a run loading prior state differs from a cold start with identical prices. Feed a CSV whose `regime` contradicts the price data and assert weights are unaffected.

**Monte Carlo — the gate.** With `mu = 0` on an equity-only book, simulated portfolio volatility must match `sqrt(wᵀΣw)` within MC error. **Nothing proceeds until this passes** — it validates the correlation structure end to end.

**MC convergence.** CVaR converges as paths increase (1k → 100k) with standard error scaling as `1/sqrt(n)`. Sobol and Mersenne Twister converge to the same value.

**Normal-case CVaR.** For a normal distribution CVaR has a closed form; the simulated value must match it.

**Option pricing.** QuantLib American prices bracket the European Black-Scholes value correctly (American call on a non-dividend payer equals European; American put exceeds European). IV round-trip: price at known vol, solve implied, recover the input within tolerance.

**Grid interpolation.** Interpolated option values match full repricing within a stated tolerance, materially smaller than MC standard error.

**Execution.** Sells never precede confirmed fills before buys. Options submit after equity. A target within the no-trade band produces no order. Hysteresis: a held name at rank 22 is retained; at rank 26 it is exited.

**Capital reservation.** Cash-secured put capital is excluded from buying power for other positions. Assert the account cannot overcommit.

---

## 13. Build order

1. CSV ingestion and validation gate. No trading logic.
2. Market data: bars, EWMA volatility, covariance, QuantLib calendar.
3. Equity weights through §4.2 constraints.
4. Regime scalar with state persistence.
5. Order generation against Alpaca **paper**, equity only, `OPTIONS_ENABLED=False`, `RISK_ENGINE=analytic`.
6. Logging.
7. **Run equity-only paper trading for several weeks before adding anything.**
8. Monte Carlo engine with Mersenne Twister. Validate against §12's analytic agreement gate.
9. Sobol, antithetic variates, grid interpolation. Re-run convergence tests.
10. Calibrate `CVAR_TARGET` per §6.5; switch `RISK_ENGINE=montecarlo`.
11. Options layer: chain fetching, IV solving, instrument selection. **Log-only initially** — compute the recommendation, log it, do not act.
12. Enable options execution on paper only after several weeks of log-only output looks sane.
13. Optional index modifiers (§4.3), only with data supporting them.

Steps 7, 11 and 12 are the ones most likely to be skipped under time pressure and the most costly to skip.

---

## 14. Known tensions

Worth stating explicitly rather than discovering later.

**Sizing sophistication exceeds signal validation.** The system applies several tilts on top of a ranking whose predictive value is unmeasured. Careful sizing changes the shape of losses, not their sign. Log what equal-weight would have returned over the same period — if this does not beat equal-weight net of costs, the complexity is not earning its place.

**Selecting for volatility while sizing by inverse volatility.** If the upstream screen targets volatile names, inverse-vol sizing then shrinks exactly those positions, leaving the book largest in the least volatile of the volatile picks. Decide deliberately whether that is intended.

**Covered calls cap winners.** Short puts and calls work well on a book that drifts sideways; they work badly when a few names run hard. If the ranking's edge lives in the top names making large moves, options on those names fight the signal. The §5.3 rank restriction mitigates this rather than solving it.

**Monte Carlo is heavier than a 20-stock book strictly requires.** It is justified here because options make the analytic formula invalid, not because the equity-only case demanded it. Keep the analytic engine working and comparable — if the two consistently produce similar scalars in live conditions, that is worth knowing and is an argument for the simpler path.

**Paper trade for months, not weeks,** before any live capital. Track: hit rate by rank decile (does rank 1–5 actually outperform 16–20?), realised versus intended portfolio volatility, turnover cost, options fallback rate, and regime crossing count. Each of those can independently indicate the system should be simplified rather than extended.
