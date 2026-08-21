# QuantLib Integration — Changes to the Existing Project

**Scope:** replace ad-hoc date handling with QuantLib's exchange calendars. This is a correctness change, not a performance one — it fixes silent misalignment bugs in the volatility and moving-average windows.

**Add:** `calendar.py` (supplied). `pip install QuantLib`.

**Rule:** `ql.Date` must not appear outside `calendar.py`. Everything else uses `datetime.date` or `pd.Timestamp` and converts at the boundary.

---

## 1. Bar validation — `weekly_selection.py` and the sizing entry point

The current gap check is a heuristic. Replace it.

```python
from calendar import TradingCalendar

cal = TradingCalendar("NYSE")

for ticker, df in bars.items():
    ok, msg = cal.validate_bar_series(df.index)
    if not ok:
        raise DataError(f"{ticker}: {msg}")
```

This catches a missing session in the middle of a series, which currently passes silently and corrupts every window calculation downstream.

**Spec reference:** replaces the bar-gap clause in §2 of the position sizing spec.

---

## 2. Volatility annualisation — §3

Current:

```python
sigma = np.sqrt(var * 252)
```

Replace:

```python
factor = cal.annualisation_factor(window_start, window_end, n_obs=len(returns))
sigma = np.sqrt(var * factor)
```

A 252-session window rarely spans exactly one year. The blanket `× 252` is a small systematic bias in every volatility estimate, and therefore in every position size.

---

## 3. The 200-day SMA window — §6.1

Current implementation slices the last 200 rows. If any bar is missing, the window silently extends further back than 200 sessions and the SMA is wrong.

```python
window_start = cal.sessions_back(run_date, 200)
window = benchmark[benchmark.index >= window_start]

expected = cal.expected_sessions(window_start, run_date)
if len(window) != expected:
    raise DataError(f"benchmark window: {len(window)} bars, expected {expected}")

sma200 = window["close"].mean()
```

Same pattern applies to the 252-day volatility window and the 120/250-day covariance window.

---

## 4. Pre-market freshness guard — new

Add at the top of the daily run, before anything else:

```python
cal.assert_fresh(latest_bar_date=bars["SPY"].index[-1])
```

Raises if today is not a trading session, or if the most recent bar is not the previous session. For a pre-market job this is the guard against an upstream feed failure causing you to size on yesterday's stale data — currently there is nothing preventing that.

---

## 5. Regime confirmation counter — §4.1 of the regime spec

The confirmation rule counts *consecutive trading days*. A naive implementation using calendar days breaks across weekends and holidays.

```python
sessions = cal.sessions_between(state["last_checked"], run_date)
for session in sessions:
    # advance crossing_day_count one session at a time
```

---

## 6. Rebalance scheduling

If the run date lands on a holiday, schedule to the next session rather than skipping the week:

```python
if not cal.is_trading_day(run_date):
    run_date = cal.next_session(run_date)
```

---

## 7. Tests to add

- `sessions_back(d, 200)` returns a date exactly 200 sessions prior, verified against a known holiday-containing span.
- `validate_bar_series` fails when a mid-series session is dropped, and names the missing date.
- `assert_fresh` raises on a weekend run date and on a bar series one session stale.
- Annualisation over a window containing holidays differs from the naive `× 252` result — assert the difference is non-zero, confirming the correction is active.
- Round-trip conversion: `from_ql(to_ql(d)) == d` for datetime.date, datetime.datetime, and pd.Timestamp inputs.

---

## Notes

**Version check.** The QuantLib Python bindings occasionally rename market enums between versions. If `ql.UnitedStates(ql.UnitedStates.NYSE)` raises, check `dir(ql.UnitedStates)` — older builds require the argument, newer ones warn without it.

**Scope of the benefit.** This fixes correctness, not speed. The bugs it addresses are the quiet kind: a window that is 203 sessions long instead of 200, an annualisation factor off by 1%, a stale bar that no check catches. None of them throw an error today; all of them move position sizes.

**What QuantLib is not doing here.** Volatility estimation, covariance shrinkage, and portfolio optimisation stay with numpy, sklearn, and riskfolio-lib. QuantLib's volatility machinery is built around implied surfaces and option pricing, which is a different problem.
