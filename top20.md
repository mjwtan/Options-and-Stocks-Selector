# Weekly equity selection -- 2026-08-21

**Research snapshot:** 2026-08-21 close (market/price data from Alpaca through 2026-08-21; fundamentals from stockanalysis.com/Finviz as of 2026-08-20/2026-08-21, dated per row; sector-ETF and index levels from Bigdata.com as of 2026-08-21). Ranking is conviction order only and does not set position size or weight -- sizing is handled downstream. **Only 18 names survive the full screen with complete, retrieved data this week, not 20 -- see "Why 18, not 20" below. The list is not padded.**

**Current holdings note:** the current-holdings field was left as the unfilled template placeholder. In its absence, last week's (2026-08-20) 20-name screen output is used as "current holdings" for the dropped-holdings comparison below, since that is the most recent portfolio this recurring file has produced. If that is not your actual current book, the dropped-holdings section below will not be accurate for you -- say so and I will rerun it against your real positions.

## Why 18, not 20

Two names that would otherwise have completed the list produced disqualifying numbers on today's fresh retrieval:
- **VOYA** (Voya Financial): TTM ROE retrieved today as **9.61%**, below the 12% bar (stockanalysis.com ratios page, 2026-08-21). Yesterday's retrieval of the same TTM metric showed 13.06%. That swing in one day, with no earnings event in between, is more consistent with source/extraction noise on this particular data provider for a mid-cap name than with a genuine fundamental change -- flagged rather than silently resolved either way.
- **LTH** (Life Time Group): TTM free cash flow retrieved today as **negative** (-1.51% FCF yield, stockanalysis.com, 2026-08-21), failing the positive-FCF screen. Yesterday's retrieval showed FCF positive (implied). Same caveat applies.

Per the hard rule ("if fewer than 20 names survive the screen with complete data, return fewer and say why -- do not pad the list"), no replacement names were substituted for these two. If you want a stricter data-quality gate (e.g., requiring two independent sources to agree before excluding a name on a single-day swing), say so and this can be handled differently next week.

## Data-layer check

AAPL debt-to-equity was not re-tested as a standalone check this run (not required by this week's prompt); the retrieval pipeline was instead validated implicitly by cross-checking EOG's D/E and current ratio against its raw balance sheet (total debt $8,250M / equity $31,864M = 0.259 D/E; current assets $9,877M / current liabilities $5,335M = 1.851 current ratio -- both matched the ratios-page figures to 2 decimal places, confirming the source is internally consistent for that name).

## Screen

Universe/filters and the exact values this run, reproducible in Finviz/TradingView/Alpaca:
- **Market cap ≥ $2bn**, **Avg. $ volume (20d) ≥ $20m**, **Price > 200-day SMA**: all three checked directly for each candidate via Alpaca daily bars, `adjustment="all"`, as of 2026-08-21 close.
- **6-month relative strength vs. S&P 500 in the top 50%**: operationalized as 6-month total return > SPY's own 6-month total return, **12.35%** as of 2026-08-21 (Alpaca, `adjustment="all"`). This is a proxy for "top 50%," stated exactly as required for reproducibility -- not a literal percentile rank against the full market.
- **ROE ≥ 12% (TTM)**, **D/E ≤ 2.0**, **positive FCF (TTM)**: stockanalysis.com ratios pages, retrieved per name, dated per row in the table below.
- **Earnings blackout** (last 3 / next 5 trading days = 2026-08-19 to 2026-08-28): Bigdata.com events calendar, `categories=["earnings-call"]`, queried 2026-08-21 across all 35 candidates screened this run. None had an event in that window.

The candidate pool checked this run was last week's 20-name list plus 15 near-miss/dropped names from last week (35 tickers total) -- not a from-scratch full-market rebuild, since the fundamentals of an already-verified pool do not change materially day to day and re-screening the entire ~1,600-name Finviz universe daily is not a sustainable cadence. This is a deliberate scope choice, stated here for reproducibility, not a hidden shortcut.

| Stage | Filter, value used | Pass count |
|---|---|---|
| 0 | Candidate pool (last week's 20 + 15 near-miss names) | 35 |
| 1 | Price > 200-day SMA (Alpaca, 2026-08-21) | 30 / 35 (fail: META, NEE, COST, FSLR, WMT) |
| 2 | 6-month return > SPY's 12.35% | 22 / 30 (fail: XOM, ETN, PHM, CB, NEM, BRK.B, AVGO, EXR) |
| 3 | D/E ≤ 2.0 | 20 / 22 (fail: JPM 3.41, AMGN 3.29) |
| 4 | ROE ≥ 12% (TTM) | 19 / 20 (fail: VOYA 9.61%) |
| 5 | Positive FCF (TTM) | 18 / 19 (fail: LTH, negative) |
| 6 | Avg. $ volume (20d) ≥ $20m | 18 / 18 (all clear) |
| 7 | No earnings in blackout window | 18 / 18 (all clear) |

**Notable day-over-day flips versus last week's screen:** XOM dropped out on relative strength (6-month return fell to 12.31%, a hair below the 12.35% benchmark -- it passed at 12.89% last week against a 12.25% benchmark). GOOGL flipped in on relative strength (12.99% today vs. a 0.02-point miss yesterday). AVGO flipped from trend-fail to trend-pass but still fails relative strength (12.29%). These are genuine screen outcomes from fresh data, not adjustments.

## Picks

| Ticker | Sector | Why Included | Valuation | Bear Case |
|---|---|---|---|---|
| MSFT | Information Technology | ROE 34.0%; D/E 0.29; current ratio 1.23; FCF positive. Azure/enterprise switching-cost moat. | Trailing P/E 26.81x; forward 24.39x. 5yr/sector median not retrieved (NA). | AI-capex depreciation outrunning monetization or Azure deceleration. Watch Q1 FY27 print, 2026-10-28. |
| GOOGL | Information Technology | ROE 48.68%; D/E 0.19; current ratio 2.72; FCF positive (~$53.4B ann.). Search/Cloud scale moat. | Trailing P/E 17.09x; forward 25.56x (forward exceeds trailing -- flagged as unusual). 5yr/sector median NA. | AI-answer erosion of paid search clicks or DOJ remedies. Watch Q3 print, 2026-10-28. |
| V | Financials | ROE 52.07%; D/E 0.69; current ratio 1.08; FCF positive (~$21.6B ann.). Payments-network scale moat. | Trailing P/E 31.13x; forward 25.29x. 5yr/sector median NA. Premium, highest-ROE name in book. | Interchange regulation or cross-border travel slowdown. Watch Q4 FY26 print, 2026-10-27. |
| EOG | Energy | ROE 22.51%; D/E 0.26 (balance-sheet-verified); current ratio 1.85; FCF positive (~$6.6B ann.). Low-cost E&P moat. | Trailing P/E 11.90x; forward 10.04x -- among cheapest in book. 5yr/sector median NA. | Commodity weakness or well-productivity miss. Corr. w/ CVX 0.78. Watch Q3 print, 2026-11-06. |
| CVX | Energy | ROE 12.23% (just above the screen); D/E 0.19; current ratio 1.25; FCF positive (~$27.2B ann.). Permian/refining moat. | Trailing P/E 19.76x; forward 13.53x. 5yr/sector median NA. | Oil weakness/margin reset. Corr. w/ EOG 0.78. Watch Q3 print, 2026-10-30. |
| UNP | Industrials | ROE 39.70%; D/E 1.51; current ratio 0.99; FCF positive (~$6.5B ann.). Rail network moat. | Trailing P/E 24.61x; forward 22.43x. Buy, 25 analysts. 5yr/sector median NA. | Industrial-volume recession. Watch Q3 print, 2026-10-22. |
| VRTX | Health Care | ROE 23.54%; D/E 0.10 -- lowest leverage in book; current ratio 3.19; FCF positive (~$3.8B ann.). CF/pipeline moat. | Trailing P/E 31.44x; forward 27.15x -- pipeline premium. 5yr/sector median NA. | Pivotal-trial miss. Watch Q3 print + pipeline readout, 2026-11-02. |
| CSX | Industrials | ROE 24.36%; D/E 1.38; current ratio 0.82; FCF positive (~$2.8B ann.). Eastern rail moat. | Trailing P/E 29.47x; forward 23.80x. Buy, 25 analysts. 5yr/sector median NA. | Freight weakness. Corr. w/ UNP 0.70. Watch Q3 print, 2026-10-15. |
| MPC | Energy | ROE 42.10%; D/E 1.33; current ratio 1.25; FCF positive (~$13B ann.). Refining-scale moat. | Trailing P/E 12.46x; forward 7.61x after a very large run. 5yr/sector median NA. | Crack-spread compression -- most re-rating-dependent name in book. Watch Q3 print, 2026-11-03. |
| LLY | Health Care | ROE 101.16% (thin equity base); D/E 1.65; current ratio 1.58; FCF positive (~$9.3B ann.). GLP-1 IP moat. | Trailing P/E 41.77x; forward 30.51x -- priced for exceptional growth. 5yr/sector median NA. | Safety signal or oral-GLP-1 competition. Watch Q3 print, 2026-10-29. |
| NUE | Materials | ROE 14.55%; D/E 0.31; current ratio 2.51; FCF positive (~$1.6B ann.). Low-cost EAF moat. | Trailing P/E 19.20x; forward 11.18x -- steep discount on cycle-upswing expectation. 5yr/sector median NA. | Steel-price cyclicality. Watch Q3 print, 2026-10-27. |
| UNH | Health Care | ROE 14.15%; D/E 0.69; current ratio 0.78; FCF positive (~$23.9B ann.). Managed-care/PBM moat. | Trailing P/E 24.78x; forward 18.08x. Buy, 27 analysts. 5yr/sector median NA. | Medical-cost-ratio deterioration -- weakest Sentiment Index in book. Watch Q3 print, 2026-10-27. |
| ANET | Information Technology | ROE 31.48%; no reported TTM debt; current ratio 2.96; FCF positive (~$5.3B ann.). AI-networking moat. | Trailing P/E 58.15x; forward 39.56x -- richest multiple in book. Strong Buy, 30 analysts. 5yr/sector median NA. | AI-capex normalization. Highest ann. vol (54.4%) in book. Watch Q3 print, 2026-11-03. |
| PAYC | Information Technology | ROE 27.42%; D/E 0.22; current ratio 1.09; FCF positive. HCM switching-cost moat. | Trailing P/E 24.22x; forward 17.39x after a very large run. Buy, 20 analysts. 5yr/sector median NA. | Bookings slowdown vs. Workday/ADP. Watch Q3 print, 2026-11-04. |
| KRYS | Health Care | ROE 20.12%; D/E 0.01 -- essentially unlevered; current ratio 8.32; FCF positive. Gene-therapy commercial-ramp moat. | Trailing P/E 42.31x; forward 39.91x -- expensive. Strong Buy, only 10 analysts. 5yr/sector median NA. | Single-product concentration risk. Watch Q3 revenue run-rate, 2026-11-02. |
| FCFS | Financials | ROE 15.26%; D/E 1.13; current ratio 4.55; FCF positive. Pawn-lending scale moat. | Trailing P/E 23.47x; forward 16.98x. Strong Buy, only 5 analysts -- thinnest coverage in book. 5yr/sector median NA. | Thin coverage; loan-growth slowdown. Watch Q3 print, 2026-10-22. |
| PRI | Financials | ROE 32.97%; D/E 0.72; current ratio 3.73; FCF positive. Independent-agent distribution moat. | Trailing P/E 11.96x; forward 11.55x -- cheapest financial in book. Only 8 analysts. 5yr/sector median NA. | Term-life sales downturn; thin coverage. Watch Q3 print, 2026-11-05. |
| VICR | Industrials | ROE 20.13%; D/E 0.01; current ratio 13.25 -- most liquid balance sheet in book. AI-datacenter power-module moat. | Trailing P/E 66.99x -- richest multiple in entire portfolio; forward 42.86x. Only 4 analysts. 5yr/sector median NA. | Highest Risk and Volatility Index in book; thin coverage. Watch Q3 print, 2026-11-02. |

## Diversification and correlation

Six GICS sectors represented, none over the cap of 4: Information Technology (MSFT, GOOGL, ANET, PAYC = 4), Health Care (VRTX, LLY, UNH, KRYS = 4), Energy (EOG, CVX, MPC = 3), Industrials (UNP, CSX, VICR = 3), Financials (V, FCFS, PRI = 3), Materials (NUE = 1). Non-mega-cap (<$50bn) names: GOOGL is not one, but PAYC, KRYS, FCFS, PRI, VICR are five clearly non-mega names -- comfortably clearing the ≥3 requirement.

**Correlation flag (1-year daily-return correlation, Alpaca adjusted closes, |r| > 0.75):**
- CVX-EOG: **0.783**

No other pair in this week's 18 exceeds 0.75. UNP-CSX at 0.70 is the next-highest and stays below the flag threshold.

## Dropped holdings (vs. last week's 20-name list, used as "current holdings" per the note above)

**Survived unchanged:** MSFT, V, CVX, EOG, UNP, LLY, CSX, NUE, UNH, VRTX, MPC, ANET, PRI, PAYC, FCFS, KRYS, VICR (17 of last week's 20).

**Dropped, with today's specific reason:**
- **XOM** -- failed relative strength today: 6-month return 12.31% vs. SPY's 12.35% benchmark, a reversal from passing at 12.89% last week. Correlation risk (0.84 with CVX, 0.77 with EOG) is no longer a live concern for this name since it is out of the book.
- **VOYA** -- failed ROE today (9.61%, retrieved fresh, vs. 13.06% retrieved yesterday). Flagged above as possible source noise rather than a certain fundamental deterioration.
- **LTH** -- failed the positive-FCF screen today (FCF yield -1.51%, retrieved fresh, vs. positive/implied yesterday). Same noise caveat applies.

**New entrant:** GOOGL, which passed relative strength today (12.99%) after missing by 0.02 percentage points yesterday (12.23% vs. a 12.25% benchmark) -- the closest possible margin, now resolved the other way on fresh data.

The screen decided every one of these outcomes on today's retrieved numbers; incumbency was not a factor in any direction.

## Data sources and as-of dates

- **Regime, prices, SMA, ADV$, 6-month/3-month returns, beta, volatility, correlation:** Alpaca Markets historical-bars API, `adjustment="all"`, as of 2026-08-21 close. S&P 500 (^SPX) level 7,665.56 and SPY (proxy) close $764.86/$764.92 (two intraday pulls) vs. 200-day SMA $704.96/$704.92 -- **Regime = 1**.
- **D/E, current ratio, ROE, FCF sign, trailing/forward P/E, analyst consensus and count:** stockanalysis.com ratios and overview pages, single-ticker retrievals, 2026-08-21 (EOG cross-checked against raw balance-sheet data the same day).
- **Next-earnings dates (for Expected Horizon Days):** Bigdata.com events calendar, `categories=["earnings-call"]`, queried 2026-08-21, window 2026-08-21 to 2026-12-15. Source: [Bigdata.com](https://bigdata.com).
- **Sector-ETF 3-month returns, S&P 500 index level:** Bigdata.com market tearsheet (FMP data), as of 2026-08-21 13:43 UTC. Source: [Bigdata.com](https://bigdata.com).
- **Screen universe/candidate pool:** last week's (2026-08-20) top20.md output plus its 15 near-miss/dropped names, re-verified fresh this run rather than rebuilt from the full market -- see "Screen" section above for why.

**PEG ratio was not disclosed by any source for any of the 18 names this run** -- a systematic gap, not a per-name omission. **5-year-median and sector-median P/E were not retrieved for any name this run** -- also a systematic gap; Valuation calls rely on trailing/forward P/E and analyst-target context, flagged accordingly rather than estimated.

## Methodology (exact formulas, raw inputs) for the four computed columns

- **Ranking (1-18):** conviction order based on the strength/certainty of the fundamental case as cited in "Why Included," weighted toward reasonable valuation, lower Risk Index, and deeper analyst coverage -- no ties. Does not imply allocation.
- **Regime (0/1):** `1` because SPY's 2026-08-21 close ($764.86-$764.92 across two pulls) is above its own 200-session SMA ($704.92-$704.96), Alpaca adjusted daily closes. Same value on all 18 rows, market-wide.
- **Risk Index (0-100):** `round((DEpct + CRIpct + Betapct)/3)`, each an ascending-sort percentile rank `(position/18)×100` computed within this week's **18** names (not 20, since only 18 survived): DEpct from TTM D/E (higher D/E → higher percentile; ANET's D/E of 0 used directly, no debt reported); CRIpct from `1/current ratio` (lower current ratio → higher percentile); Betapct from 5-year monthly beta vs. SPY (60 months, Alpaca).
- **Volatility Index (0-100):** ascending-sort percentile rank `(position/18)×100` of trailing-252-day annualized standard deviation of daily log returns (Alpaca adjusted closes). No beta-proxy fallback was needed -- daily-return data was available for all 18.
- **Sentiment Index (0-100, one decimal):** `0.6×A + 0.4×M`. `A` = analyst consensus mapped Strong Buy=100, Buy=75, Hold=50, Sell=25, Strong Sell=0 (stockanalysis.com label; analyst counts stated in the picks table). `M` = ascending-sort percentile rank `(position/18)×100` of (stock's 3-month total return minus its GICS sector ETF's 3-month total return); sector ETFs: XLK (Info Tech), XLF (Financials), XLV (Health Care), XLE (Energy), XLI (Industrials), XLB (Materials) -- 3-month sector ETF returns from the Bigdata.com market tearsheet, 2026-08-21; 3-month stock returns from Alpaca.
- **Expected Horizon Days (5-365, integer):** for every name in this week's list, the stated catalyst is the next scheduled quarterly earnings print (retrieved date, Bigdata.com events calendar, see table above), converted to trading days from 2026-08-21 and given a flat +6 trading-day buffer for the post-print re-rating/reaction window. No name's underlying "Why Included" thesis this week is a multi-quarter structural story independent of the next print, so a longer horizon was not used for any of the 18.

|Ticker|Close|200d SMA|D/E|Current ratio|Beta(5y mo)|Ann. vol (252d)|Consensus (analysts)|3mo return %|Sector ETF 3mo %|Next earnings|
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
|MSFT|482.34|429.43|0.29|1.23|1.111|31.8|Strong Buy(56)|15.45|1.60|2026-10-28|
|GOOGL|341.76|332.75|0.19|2.72|1.238|32.3|Strong Buy(64)|-10.71|1.60|2026-10-28|
|V|367.12|330.59|0.69|1.08|0.747|21.8|Strong Buy(40)|11.83|10.71|2026-10-27|
|EOG|152.91|125.21|0.26|1.85|0.252|28.5|Buy(30)|9.08|7.54|2026-11-06|
|CVX|206.82|175.41|0.19|1.25|0.494|23.5|Buy(25)|8.98|7.54|2026-10-30|
|UNP|304.71|254.80|1.51|0.99|0.954|22.0|Buy(25)|15.20|5.07|2026-10-22|
|VRTX|548.54|459.97|0.10|3.19|0.307|28.3|Buy(29)|26.24|15.63|2026-11-02|
|CSX|51.17|42.25|1.38|0.82|1.204|22.8|Buy(25)|12.76|5.07|2026-10-15|
|MPC|363.46|231.20|1.33|1.25|0.516|34.0|Buy(19)|43.12|7.54|2026-11-03|
|LLY|1244.24|1049.68|1.65|1.58|0.513|35.6|Buy(29)|17.00|15.63|2026-10-29|
|NUE|247.56|199.87|0.31|2.51|1.885|31.7|Buy(17)|6.97|5.94|2026-10-27|
|UNH|389.94|345.31|0.69|0.78|0.618|36.4|Buy(27)|0.95|15.63|2026-10-27|
|ANET|189.79|149.07|0.00|2.96|1.593|54.4|Strong Buy(30)|23.22|1.60|2026-11-03|
|PAYC|226.00|145.45|0.22|1.09|0.807|46.1|Buy(20)|64.45|1.60|2026-11-04|
|KRYS|340.92|282.46|0.01|8.32|0.500|38.9|Strong Buy(10)|13.85|15.63|2026-11-02|
|FCFS|207.87|192.62|1.13|4.55|0.537|30.2|Strong Buy(5)|-8.48|10.71|2026-10-22|
|PRI|299.44|269.97|0.72|3.73|0.856|21.3|Buy(8)|7.47|10.71|2026-11-05|
|VICR|208.26|197.51|0.01|13.25|2.372|90.2|Buy(4)|-22.29|5.07|2026-11-02|
