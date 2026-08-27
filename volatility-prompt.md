Using Artemis Discovery, produce this week's top 20 stock selection and return **two files**: `top20.csv` and `top20.md`.

**Role:** you are producing a weekly equity selection file for a systematic options and equity portfolio. The downstream system computes its own volatility, correlations and position sizes — your job is selection and ranking, not sizing.

### Objective

Identify 20 US-listed equities with **elevated realised and implied volatility** where there is a directional case over the next 2–8 weeks. The portfolio expresses these via shares, long calls, or short puts depending on the options market, so names need to be liquid enough to have a tradable options chain.

This is a volatility-seeking mandate, not a quality or value screen. Stable large-cap compounders are not the target.

### Screen

Apply these filters and **state the value each took this run**, plus how many names passed each stage:

- Market cap ≥ $2bn
- Average daily dollar volume (20d) ≥ $20m
- **Listed options chain available**, with open interest ≥ 500 across the front two monthly expiries
- 60-day realised volatility ≥ 30% annualised
- 60-day realised volatility in the top 40% of the screened universe
- Average option bid-ask spread near the money ≤ 15% of mid
- Exclude: names reporting earnings within the next 5 trading days

Report the count surviving each stage.

### Diversification

- No more than 4 names from any one GICS sector
- At least 3 names outside mega-cap (< $50bn)
- Flag any pair with 1-year daily return correlation > 0.75, stating the value

### Ranking

Rank 1–20 by conviction in the **directional thesis**, strict order, no ties or gaps. Rank on the strength and specificity of the catalyst or trend, not on volatility magnitude — volatility is a screen criterion, not a ranking criterion.

### Numeric columns — use exactly these definitions every week

**Ranking** (1–20): as above.

**Regime** (0/1): market-wide. 1 if the S&P 500 closes above its own 200-day SMA, 0 if below. Identical on all 20 rows. State the index level and its 200-day SMA.

**Risk Index** (0–100): equal-weighted mean of three percentile ranks computed *within this week's 20 names*:
- debt-to-equity (higher → higher percentile)
- 1 / current ratio (lower current ratio → higher percentile)
- 5-year monthly beta vs. S&P 500 (higher → higher percentile)

Percentile = (ascending sort position / 20) × 100. Report the three raw inputs per stock.

**Volatility Index** (0–100): annualised standard deviation of daily log returns over the trailing 252 trading days, percentile-ranked within this week's 20 names. Report the raw annualised figure alongside the percentile.

**Sentiment Index** (0–100), to one decimal:
- 60% × analyst consensus, mapped Strong Buy = 100, Buy = 75, Hold = 50, Sell = 25, Strong Sell = 0. State analyst count.
- 40% × (3-month stock return − 3-month GICS sector ETF return), percentile-ranked within this week's 20 names.

Report both components separately as well as the blend.

**Expected Horizon Days** (int): how long you expect the thesis to take to play out, in calendar days. This drives option expiry selection downstream — a 30-day catalyst and a 90-day trend need different contracts. Range 14–120. Base it on the specific catalyst, not a default.

### Data handling — always return 20 rows

Return 20 rows. Never drop a stock for incomplete data, and never return an empty file.

Mark every numeric cell with its provenance:
- `[R]` retrieved from a named source this run
- `[E]` estimated or from prior knowledge — state the basis briefly
- `NA` genuinely unavailable

Add a **Data Quality** column: count of `[R]` cells / total numeric cells for that row, as a decimal 0–1.

If an index input is `NA`, compute the index from the remaining components and state which were omitted. Do not substitute a neutral placeholder.

Before screening, retrieve one test metric (AAPL debt-to-equity) and report what came back — this confirms the data layer is live before the rest of the run depends on it.

If a stage eliminates everything, state which metric caused it rather than returning an empty result.

### Current holdings

Empty at the moment

Use only for context. After the table, note in a short paragraph which current holdings dropped out this week and why. Do not let incumbency affect inclusion or ranking.

### Output

**top20.md** — table with Ticker, Sector, Why Included, Valuation, Bear Case, Volatility Profile. Below it: screen-stage counts, diversification and correlation summary, dropped-holdings note, and data sources with as-of dates.

Prose field requirements:
- **Why Included** — the specific directional case: catalyst, trend, or dislocation, with figures. What moves this stock in the next 2–8 weeks.
- **Valuation** — P/E and PEG vs. the stock's own 5-year median and its sector median. Cheap, fair or expensive, and roughly by how much.
- **Bear Case** — the specific downside risk and the observable early sign that the thesis is failing.
- **Volatility Profile** — realised 60d vol, what is driving it, and whether it is expected to persist or decay.

**top20.csv** — one row per stock, columns exactly:

```
Ticker,Sector,Why Included,Valuation,Bear Case,Ranking,Regime,Risk Index,Volatility Index,Sentiment Index,Expected Horizon Days,Data Quality
```

CSV formatting, non-negotiable:
- Prose fields contain commas. Quote every field with double quotes; escape internal quotes by doubling (RFC 4180)
- No line breaks inside fields
- Integers for Ranking, Regime, Expected Horizon Days; one decimal for the three indices and Data Quality
- Header row exactly as above, no trailing spaces
- **`top20.csv` contains ONLY the header row plus exactly 20 data rows. Nothing else — no blank line, no methodology note, no commentary, no trailing text of any kind, anywhere in that file.** This file is machine-parsed directly; a single stray line breaks the parser for the whole file.

Put the methodology note (restating the exact formulas used for the computed columns with their raw inputs, so the numbers can be reproduced independently) in **`top20.md` only**, in its own section after the table.

Do not recommend position sizes, weights, or dollar amounts. Sizing is handled downstream.

---
