# Market Regime Filter — Specification

**Scope:** Computing `k_regime`, the scalar controlling total invested exposure based on broad market trend.

**Where it plugs in:** the final step of position sizing, after per-stock weights and portfolio volatility targeting are computed.

```
w_final = w_constrained * k_vol * k_regime
cash    = 1 - sum(w_final)
```

`k_regime` scales the whole book uniformly. It does not change relative weights between stocks.

**Why it matters:** this is the highest-impact parameter in the system. Whether the book is 30% or 100% invested during a drawdown moves returns more than any refinement to per-stock sizing.

---

## 1. Source of truth

**Compute regime from our own benchmark bars. Do not use the `regime` column in the incoming CSV as a trading input.**

Three reasons:

1. We already hold the benchmark bars for the covariance calculation — no extra data cost.
2. We control the as-of date and can guarantee it matches the bars used elsewhere in the run.
3. The upstream definition has already drifted once. An earlier version of the selection spec defined regime **per-stock** (each stock's price vs. its own 200-day SMA); the current one defines it **market-wide**. Depending on that field means our exposure silently changes if it drifts again.

Read the CSV's `regime` column, log it, compare it against our computed value, and alert on disagreement. Never act on it.

---

## 2. Inputs

| Input | Definition |
|---|---|
| `benchmark_closes` | ≥ 250 daily closes of the benchmark |
| `prev_k_regime` | Previous run's value, persisted (see §5) |
| `crossing_day_count` | Consecutive days on the new side of the MA, persisted |

**Benchmark:** S&P 500 index (`^GSPC`). Fall back to SPY if index data is unavailable, and log which was used — they diverge slightly because SPY is total-return-adjusted and the index is not.

---

## 3. Calculation

### 3.1 Distance from the moving average

```
sma200   = mean(benchmark_closes[-200:])          # simple, not exponential
distance = (benchmark_closes[-1] - sma200) / sma200
```

`distance` is a signed fraction: +0.04 means the index is 4% above its 200-day SMA.

### 3.2 Continuous scalar

A binary on/off switch liquidates and rebuys the entire book each time the index crosses its moving average. In a choppy market that is several full round trips a year carrying no information. Use a continuous function instead:

```
k_raw    = 0.5 + REGIME_SLOPE * distance          # REGIME_SLOPE = 5.0
k_target = clip(k_raw, REGIME_FLOOR, 1.0)         # REGIME_FLOOR = 0.30
```

Resulting behaviour:

| distance | k_target | invested |
|---|---|---|
| +10% | 1.00 | 100% |
| +6% | 0.80 | 80% |
| +4% | 0.70 | 70% |
| 0% (at the MA) | 0.50 | 50% |
| −2% | 0.40 | 40% |
| −4% and below | 0.30 | 30% (floor) |

**The floor is deliberate.** Never going fully to cash means a sharp V-shaped recovery does not leave the book entirely on the sidelines. That is the 200-day filter's worst-documented failure mode: the largest up-days cluster near market bottoms, when the signal still reads risk-off.

**The 1.0 cap is also deliberate.** A strongly trending market does not justify leverage.

---

## 4. Dampers

Both are required. Neither is optional.

### 4.1 Confirmation rule

`k_regime` may not cross the 0.5 line — in either direction — until the benchmark has closed on the new side of the SMA for `REGIME_CONFIRM_DAYS` (default 3) **consecutive** trading days.

Movement within the continuous range on the same side of 0.5 is unrestricted; only the crossing is gated.

```
if sign(distance) != sign(prev_distance):
    crossing_day_count = 1
else:
    crossing_day_count += 1

if crossing_would_span_0.5 and crossing_day_count < REGIME_CONFIRM_DAYS:
    k_target = clamp k_target to the current side of 0.5
```

Note this requires computing `distance` **daily**, not only on rebalance days, or the consecutive-day count is meaningless.

### 4.2 Rate limit

```
delta    = k_target - prev_k_regime
delta    = clip(delta, -REGIME_MAX_STEP, +REGIME_MAX_STEP)   # 0.15
k_regime = prev_k_regime + delta
```

A crash that would take the scalar from 1.0 to 0.3 in a single week instead moves 1.00 → 0.85 → 0.70 → 0.55 → 0.40 → 0.30 across five rebalances.

This trades some drawdown protection for a large reduction in whipsaw cost, and prevents one bad print from liquidating most of the book.

---

## 5. State

**`k_regime` is stateful, not a pure function of current price.** Both of these must persist between runs:

- `k_regime` (for the rate limiter)
- `crossing_day_count` and the previous day's `distance` sign (for the confirmation rule)

Store in a small JSON or SQLite file alongside the run logs.

**Cold start:** if no prior state exists, set `prev_k_regime = k_target` (skip the rate limiter on the first run only) and `crossing_day_count = REGIME_CONFIRM_DAYS` (treat the current side as already confirmed). Log clearly that a cold start occurred.

**Missed runs:** if the last persisted state is more than 10 days old, treat as a cold start rather than applying a single 0.15 step against stale state.

---

## 6. Interaction with the stock-level screen

The upstream selection screen already filters on price > 200-day SMA **per stock**. In a genuine market downtrend most candidates fail that filter independently, so the incoming list shrinks or arrives empty on its own.

Two consequences:

**A short or empty CSV during a downtrend is expected, not a fault.** It must still fail the validation gate rather than being read as "sell everything," but the alert should distinguish:

- Empty file + `distance < 0` → expected, informational
- Empty file + `distance > 0` → upstream fault, investigate

**Do not double-count.** The scalar is not there to duplicate the per-stock screen. It exists to cut *total* exposure when the market is weak, including for names that individually still pass the screen. Resist any suggestion to also penalise individual stocks for the market regime — that applies the same signal twice.

---

## 7. Logging

Log every day the calculation runs, whether or not anything changed:

- `benchmark_used` (index or SPY proxy)
- `close`, `sma200`, `distance`
- `k_raw`, `k_target` after clipping, `k_regime` after rate limiting
- `prev_k_regime` and the delta actually applied
- Whether the confirmation rule blocked a crossing, and the current `crossing_day_count`
- CSV-supplied `regime` value and whether it agreed with ours
- Rolling count of 0.5-line crossings in the trailing 12 months

**The crossing count is the diagnostic that matters.** More than four or five a year means the parameters are too twitchy — reduce `REGIME_SLOPE` or lengthen `REGIME_CONFIRM_DAYS`.

---

## 8. Parameters

| Name | Default | Notes |
|---|---|---|
| `REGIME_SLOPE` | 5.0 | Sensitivity to distance from the MA |
| `REGIME_FLOOR` | 0.30 | Minimum invested fraction |
| `REGIME_CONFIRM_DAYS` | 3 | Consecutive closes before crossing 0.5 |
| `REGIME_MAX_STEP` | 0.15 | Max change per rebalance |
| `REGIME_BENCHMARK` | `^GSPC` | Fall back to SPY, log which |
| `REGIME_SMA_WINDOW` | 200 | Trading days |

All config-driven, none hardcoded.

**CLI overrides for testing:**
- `--force-regime <float>` — pin `k_regime` to a fixed value, bypassing all logic
- `--regime-dry-run` — compute and log the scalar without applying it to weights

---

## 9. Tests

- `k_regime` is monotonic in `distance`.
- `k_regime` never exceeds 1.0 and never falls below `REGIME_FLOOR`, across randomised distance inputs.
- `k_regime` never changes by more than `REGIME_MAX_STEP` between consecutive runs.
- A single day's crossing of the MA does not move `k_regime` across 0.5; three consecutive days does.
- State persists correctly: a run loading prior state produces a different result from a cold start with identical price inputs.
- Stale state (> 10 days) triggers cold-start behaviour.
- Our computed regime is what reaches the sizing formula; the CSV's `regime` column is logged but never used in any weight calculation. Assert this by feeding a CSV whose `regime` contradicts the price data and confirming weights are unaffected.
- `--force-regime 1.0` produces weights identical to a run with the regime filter disabled.

---

## 10. Validation before this is trusted

**The defaults in §8 are reasoned, not fitted.** They come from the general literature on 200-day MA filters, not from testing on this strategy.

During paper trading, log what the regime scalar would have done and answer:

1. How many times did `k_regime` cross 0.5 over the period?
2. What did those round trips cost in spread and commission?
3. What would a fixed `k_regime = 1.0` — ignoring regime entirely — have returned over the same period?
4. What was the maximum drawdown with and without the filter?

**If the filter did not reduce drawdown by more than it cost in turnover, set `REGIME_FLOOR = 1.0` and disable it.**

That is a legitimate outcome, not a failure. The 200-day filter is well documented at reducing drawdown, but it underperforms in strong bull markets, whipsaws in range-bound ones, and its effect has weakened as it became widely known. Build it so it can be switched off cleanly, and treat switching it off as a live possibility rather than a fallback.
