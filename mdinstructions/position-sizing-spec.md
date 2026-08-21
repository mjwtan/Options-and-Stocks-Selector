# Position Sizing & Rebalancing Specification

**Purpose:** Convert a weekly CSV of 20 ranked stocks into target portfolio weights, then into broker orders.

**Cadence:** Weekly.

**Execution venue:** Alpaca (paper first, live later). Use `notional` orders so fractional sizing works.

---

## 1. Inputs

### 1.1 Weekly CSV (supplied by the selection system)

Exactly 20 rows, one per stock. Six columns:

| Column | Type | Definition |
|---|---|---|
| `ticker` | string | Must match broker symbology exactly |
| `ranking` | int 1–20 | Conviction order. 1 = highest conviction, 20 = lowest. Strict, no ties, no gaps |
| `regime` | int 0/1 | 1 = benchmark above its 200-day MA, 0 = below. Market-wide, so identical on every row |
| `risk_index` | float | Higher = riskier |
| `volatility_index` | float | Higher = more volatile |
| `sentiment_index` | float | Higher = more positive |

Notes on the schema as received:

- `regime` is a property of the market, not the stock, so the same value repeats across all 20 rows. Validate that it is in fact uniform; a file with mixed regime values is malformed.
- There is **no timestamp column**. Staleness must therefore be detected by other means — see §2.
- Only `ranking` and `regime` feed the default sizing path. The three indices are optional secondary modifiers, disabled by default (§4.1).

**Assumptions to confirm with the supplier before writing code:** the direction of each index (higher = worse for risk and volatility, higher = better for sentiment), and whether the scales are fixed or relative to that week's batch. The method below is scale-invariant, so drifting scales are tolerated — but an inverted direction would silently reverse the sizing with no error raised.

**Worth requesting as an addition:** a `generated_at` UTC timestamp column. It is the cleanest staleness guard and costs the supplier nothing.

### 1.2 Data we compute ourselves

Pull ~300 trading days of daily bars for all 20 tickers plus the benchmark, from Alpaca. Everything below derives from these.

| Quantity | Definition |
|---|---|
| `sigma_i` | Annualised volatility, EWMA, 21-day half-life |
| `Sigma` | 20×20 covariance matrix, Ledoit–Wolf shrinkage, 250-day window, annualised |
| `adv20_i` | 20-day average daily dollar volume |
| `price_i` | Latest close |

**Do not** use the supplier's `volatility_index` in place of `sigma_i`. It is an estimate; we can compute the real thing from bars we already hold. The supplied index is used only as a cross-check (see §7).

---

## 2. Validation gate

Refuse to trade and alert if any of the following:

- Row count ≠ 20 (warn and proceed if between 10 and 19; abort below 10)
- Duplicate tickers
- Any ticker fails an Alpaca asset lookup, or is flagged non-tradable
- Any `ranking` outside 1–20, or the set of rankings not forming a strict 1..N sequence
- `regime` not uniform across all rows, or not in {0, 1}
- Any of the three index columns missing, non-numeric, or null — even though they are unused by default, a null signals the upstream run degraded
- Missing bars for any ticker, or a gap in the bar series longer than the exchange calendar allows

**Staleness, without a timestamp column:** since the file carries no `generated_at`, guard against re-consuming last week's file by both of the following:

1. Hash the file contents and compare against the previous run's hash. An identical hash means the file did not update — abort.
2. Check the file's modification time on disk is within the last 3 days.

Neither alone is sufficient: a legitimately unchanged pick list would produce an identical hash, and a touched-but-not-regenerated file would pass the mtime check. Requiring both, and alerting rather than silently proceeding when either fails, is the safe default. Requesting a `generated_at` column from the supplier removes this problem entirely.

A malformed file must never reach order submission. Log the reason and exit non-zero.

---

## 3. Volatility calculation

EWMA with a 21-day half-life. This is preferred over a fixed 60-day window because it has no cliff — an outlier's influence decays smoothly instead of dropping out abruptly and jolting position sizes.

```
r_t   = ln(P_t / P_{t-1})
lam   = 0.5 ** (1/21)                    # ≈ 0.9674
var   = variance(r[0:60])                # seed
for t in remaining days:
    var = lam * var + (1 - lam) * r_t**2

sigma_i = sqrt(var * 252)
sigma_i = clip(sigma_i, 0.12, 0.80)
```

The clip is load-bearing. Without a floor, an unusually quiet stock receives an enormous inverse-volatility weight based on what is probably a temporary lull.

---

## 4. Raw weights

The ranking is treated as a high-conviction signal, so weighting is linear in rank rather than lightly tilted.

```
raw_i = (21 - ranking_i) / sigma_i
w_i   = raw_i / sum(raw)
```

Ranking 1 contributes 20 units, ranking 20 contributes 1 unit, before volatility adjustment.

### 4.1 Optional secondary modifiers — default OFF

The supplier's `risk_index` and `sentiment_index` may be layered on, but only after validation. Implement behind a config flag, disabled initially.

Normalise each to a within-week percentile across the 20 rows (this neutralises scale drift):

```
p_risk_i = rank_of(risk_index_i among the 20) / 20
p_sent_i = rank_of(sentiment_index_i among the 20) / 20

r_mult_i = 1 - 0.20 * p_risk_i           # 1.00 → 0.80
s_mult_i = 0.92 + 0.16 * p_sent_i        # 0.92 → 1.08

raw_i = (21 - ranking_i) / sigma_i * r_mult_i * s_mult_i
```

**Before enabling:** compute the correlation between `p_risk` and our own `sigma_i` percentile over several weeks of files. If it exceeds 0.8 the two are measuring the same thing, and applying both double-penalises volatile names. In that case leave `risk_index` off permanently.

Sentiment is the noisiest input and is likely already reflected in `rank`. Treat enabling it as an experiment, not a default.

---

## 5. Constraints

Apply in order, renormalise after each pass, iterate until stable or 5 passes:

```
w_i <= 0.12                              # position cap
w_i >= 0.015  else drop the name         # floor; below this, costs dominate
w_i * V <= 0.005 * adv20_i               # liquidity: exitable in one day
```

Where `V` = total portfolio value.

Names dropped by the floor are expected behaviour, not an error. Under linear rank weighting, the bottom of the list naturally falls below the floor — the portfolio will typically hold **12–15 names, not 20**. This is intentional: concentration is how conviction is expressed.

Log which constraint bound for each name each week.

---

## 6. Portfolio volatility targeting

This is the only step that accounts for correlation between holdings. It must not be skipped.

```
Sigma   = LedoitWolf().fit(returns_250d).covariance_ * 252
sigma_p = sqrt(w.T @ Sigma @ w)

k_vol    = min(1.0, SIGMA_TARGET / sigma_p)      # SIGMA_TARGET = 0.15
k_regime = see §6.1

w_final = w * k_vol * k_regime
cash    = 1 - sum(w_final)
```

`k_vol` is capped at 1.0 — never lever. If the 20 names are highly correlated, `sigma_p` rises, `k_vol` falls, and the book automatically holds cash instead of taking one concentrated bet dressed up as twenty positions.

Ledoit–Wolf shrinkage is required, not optional: with 20 assets and 250 observations, a raw sample covariance matrix is unstable.

### 6.1 Regime handling

The regime scalar controls total invested exposure when the broad market is weak. It is the highest-impact parameter in the system — the difference between 30% and 100% invested during a drawdown dominates any refinement to the per-stock weights — so it is specified in detail here.

#### 6.1.1 Compute regime ourselves

**Do not use the CSV's `regime` column as the trading input.** Compute it from our own benchmark bars, for three reasons: we already hold the data, we control the as-of date, and it removes a dependency on an upstream field whose definition has already drifted once (it was per-stock in an earlier version of the selection spec).

Read the CSV's `regime` column, log it, and compare against our own value. Alert on disagreement. Do not act on theirs.

```
spx_close  = latest close of the benchmark index
sma200     = simple mean of the last 200 daily closes of the benchmark
distance   = (spx_close - sma200) / sma200
```

Benchmark: S&P 500 index. Use SPY as the proxy if index data is unavailable, and log which was used — they diverge slightly due to dividends.

#### 6.1.2 Continuous scalar

A binary switch liquidates and rebuys the entire book each time the index crosses its moving average. In a choppy market that is several full round trips a year with no informational content. Use a continuous function of distance instead:

```
k_raw    = 0.5 + REGIME_SLOPE * distance          # REGIME_SLOPE = 5.0
k_regime = clip(k_raw, REGIME_FLOOR, 1.0)         # FLOOR = 0.30
```

Behaviour:

| distance | k_regime | invested |
|---|---|---|
| +10% | 1.00 | 100% |
| +4% | 0.70 | 70% |
| 0% (at the MA) | 0.50 | 50% |
| −4% | 0.30 | 30% (floor) |
| −15% | 0.30 | 30% (floor) |

The floor is deliberate. Never going fully to cash means a sharp V-shaped recovery does not leave the book entirely on the sidelines — historically the filter's worst failure mode, since the largest up-days cluster near bottoms while the signal still reads risk-off.

#### 6.1.3 Confirmation and rate limiting

Two dampers, both required:

**Confirmation.** Compute `distance` daily but require **3 consecutive daily closes** on the new side of the MA before allowing `k_regime` to cross the 0.5 line in either direction. Below the crossing threshold, movement within the continuous range is unrestricted.

**Rate limit.** `k_regime` may not change by more than `REGIME_MAX_STEP = 0.15` per rebalance. A crash that would take it from 1.0 to 0.3 in one week instead moves 1.0 → 0.85 → 0.70 → 0.55 → 0.40 → 0.30 over five weeks. This trades some drawdown protection for a large reduction in whipsaw cost, and prevents a single bad print from liquidating most of the book.

Persist the previous `k_regime` between runs. It is state, not a pure function of current price.

#### 6.1.4 Instrumentation

Log every run, whether or not the regime changed:

- `spx_close`, `sma200`, `distance`
- `k_raw`, `k_regime` after clipping, `k_regime` after rate limiting
- Previous run's `k_regime` and the delta applied
- Whether the confirmation rule blocked a crossing, and the day count
- CSV-supplied `regime` and whether it agreed with ours
- Cumulative count of 0.5-line crossings in the trailing 12 months

The crossing count is the diagnostic that matters. More than four or five a year means the parameters are too twitchy and `REGIME_SLOPE` should be reduced or the confirmation window lengthened.

#### 6.1.5 Interaction with the stock-level screen

The selection screen already filters on price > 200-day SMA per stock. In a genuine market downtrend most candidates fail that filter independently, so the list shrinks or arrives empty on its own. This means the market-level scalar is partly redundant with a filter already applied upstream.

Consequences for implementation:

- A short or empty CSV in a downtrend is **expected**, not an error. It must still fail the §2 validation gate rather than being silently read as "sell everything," but the alert should distinguish "empty file, regime weak" from "empty file, regime strong" — the latter indicates an upstream fault.
- Do not double-count. The scalar is not there to duplicate the stock screen; it exists to cut *total* exposure when the market is weak, including for names that individually still pass.

#### 6.1.6 Parameters

| Name | Default | Notes |
|---|---|---|
| `REGIME_SLOPE` | 5.0 | Sensitivity to distance from MA |
| `REGIME_FLOOR` | 0.30 | Minimum invested fraction |
| `REGIME_CONFIRM_DAYS` | 3 | Consecutive closes before crossing 0.5 |
| `REGIME_MAX_STEP` | 0.15 | Max change per rebalance |
| `REGIME_BENCHMARK` | `^GSPC` | Fall back to SPY, log which |

All config-driven. Expose `--regime-scalar <float>` as a CLI override for testing, and `--force-regime <float>` for backtests.

#### 6.1.7 Validation before trusting these numbers

The defaults above are reasoned, not fitted. During paper trading, log what the regime scalar would have done and answer three questions before committing real capital:

1. How many times did it cross 0.5 in the period?
2. What did the round trips cost in spread and commission?
3. What would a fixed `k_regime = 1.0` (i.e. ignoring regime entirely) have returned over the same period?

If the filter did not reduce drawdown by more than it cost in turnover, set `REGIME_FLOOR = 1.0` and disable it. That is a legitimate outcome and should be treated as a live possibility, not a failure.

---

## 7. Cross-check (log only, do not act on)

Compare our computed `sigma_i` percentile against the supplied `volatility_index` percentile. Log any name where the two disagree by more than 0.4 in percentile terms. Persistent disagreement means either the supplier's data source differs from ours or a pick is mis-specified. This is a diagnostic, not a trading input.

---

## 8. Order generation

```
target_value_i  = w_final_i * V
current_value_i = from broker positions
delta_i         = target_value_i - current_value_i
```

Execute a trade only if:

```
abs(delta_i) > max(25, 0.20 * target_value_i)      # currency units
```

**Exit rule with hysteresis:** do not sell a currently held name until it falls out of the **top 25** of the ranking, not the top 20. A name oscillating around rank 20 would otherwise generate a round trip every week. Positions absent from the file entirely are exited in full.

**Execution order — mandatory:**

1. Submit all sells first.
2. Poll until fills confirm (or timeout → abort remaining orders and alert).
3. Then submit buys.

Buying before sells settle causes buying-power rejections and can self-cross. Full exits use `close_position`, not a computed notional, to avoid fractional dust.

Skip any order below ~£20 notional.

---

## 9. Logging

Persist per run:

- Input CSV hash and file modification time
- Computed `sigma_i` per name
- Weights at each stage: raw → constrained → after `k_vol` → after `k_regime`
- `sigma_p`, `k_vol`, `k_regime`
- Which constraint bound for each name
- Names dropped by the floor
- Intended vs. actual fills
- Turnover as % of portfolio value
- Cash %

Without this, diagnosing a divergence between model and realised performance is impossible.

---

## 10. Parameters

| Name | Default | Notes |
|---|---|---|
| `SIGMA_TARGET` | 0.15 | Annualised portfolio vol target |
| `POSITION_CAP` | 0.12 | Max single weight |
| `POSITION_FLOOR` | 0.015 | Below this, drop the name |
| `NO_TRADE_BAND` | 0.20 | Fractional deviation before trading |
| `MIN_TRADE_ABS` | 25 | Absolute floor on trade size |
| `EXIT_RANK` | 25 | Hysteresis threshold |
| `REGIME_SLOPE` | 5.0 | See §6.1 |
| `REGIME_FLOOR` | 0.30 | Minimum invested fraction |
| `REGIME_CONFIRM_DAYS` | 3 | Consecutive closes before crossing 0.5 |
| `REGIME_MAX_STEP` | 0.15 | Max `k_regime` change per rebalance |
| `EWMA_HALFLIFE` | 21 | Trading days |
| `LIQUIDITY_FRAC` | 0.005 | Max position as fraction of ADV |
| `USE_RISK_INDEX` | False | See §4.1 |
| `USE_SENTIMENT_INDEX` | False | See §4.1 |

All must be config-driven, not hardcoded.

---

## 11. Worked example

Portfolio value £10,000, `regime = 1`. Five illustrative names from the twenty:

| rank | sigma | raw = (21−rank)/sigma |
|---|---|---|
| 1 | 0.25 | 80.0 |
| 5 | 0.40 | 40.0 |
| 10 | 0.30 | 36.7 |
| 15 | 0.35 | 17.1 |
| 20 | 0.45 | 2.2 |

Assume `sum(raw)` across all 20 names ≈ 400.

| rank | w before constraints | after constraints |
|---|---|---|
| 1 | 20.0% | capped to 12.0% |
| 5 | 10.0% | ~10.6% (absorbs redistributed excess) |
| 10 | 9.2% | ~9.7% |
| 15 | 4.3% | ~4.5% |
| 20 | 0.6% | dropped (below 1.5% floor) |

Then `sigma_p` comes out at 0.19 against a 0.15 target:

```
k_vol = 0.15 / 0.19 = 0.79
```

Final: rank 1 → 9.5% → £950. Rank 5 → 8.4% → £840. Rank 15 → 3.6% → £360. Cash ≈ 21%.

Note the shape: the top pick receives roughly 2.6× the fifteenth pick, driven by both rank and volatility, and the entire book scales down because the basket's correlation structure made it riskier than target.

---

## 12. Testing requirements

- **Determinism:** same CSV + same bar data → identical weights, byte-for-byte.
- **Constraint satisfaction:** property test asserting no output weight exceeds the cap, falls below the floor, or breaches the liquidity limit, across randomised inputs.
- **Weights sum correctly:** `sum(w_final) + cash == 1.0` within floating-point tolerance.
- **Regime scalar:** `k_regime` is monotonic in `distance`, never exceeds 1.0, never falls below `REGIME_FLOOR`, and never changes by more than `REGIME_MAX_STEP` between consecutive runs.
- **Regime confirmation:** a single day's crossing of the MA does not move `k_regime` across 0.5; three consecutive days does.
- **Regime state persistence:** `k_regime` is loaded from the previous run, not recomputed from scratch.
- **Regime source:** our computed regime is used for sizing; the CSV's `regime` column is logged and compared but never acted on.
- **Validation gate:** malformed CSVs (short, duplicated, repeated-hash, non-uniform regime, bad rankings, null index columns) all fail closed and submit zero orders.
- **Sell-before-buy ordering** is enforced; test that a buy is never submitted before sell fills confirm.
- **No-trade band:** a target within the band produces no order.
- **Hysteresis:** a held name at rank 22 is retained; at rank 26 it is exited.

---

## 13. Build order

1. Data layer: bar fetching, EWMA volatility, covariance, ADV.
2. Validation gate.
3. Weight calculation through §5 constraints.
4. Volatility targeting and regime scaling.
5. Order generation and execution against Alpaca **paper**.
6. Logging.
7. Only then consider enabling the §4.1 optional modifiers, and only with data supporting them.

Run against paper trading for a minimum of several months before any live capital. During that period, also log what an equal-weight portfolio of the same names would have returned — if this model does not beat equal-weight net of costs, the added complexity is not earning its place.
