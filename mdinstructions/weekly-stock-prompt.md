# Weekly Top-20 Selection Prompt

Paste the block below each week. Replace the bracketed sections before sending.

---

## THE PROMPT

**Role:** You are producing a weekly equity selection file. Accuracy of the numeric fields matters more than the quality of the prose. A fabricated number is worse than an admitted gap.

### Hard rules — read before anything else

1. **Every numeric value must come from a retrieved source, not from recall.** Use your data tools for each figure. If you cannot retrieve a figure, write `NA` in that cell and note it. Do not estimate, interpolate, or reason your way to a plausible number.
2. **State the as-of date and source for every metric class** (e.g. "D/E and current ratio: latest reported quarter, [source], as of YYYY-MM-DD").
3. If fewer than 20 names survive the screen with complete data, return fewer and say why. Do not pad the list.
4. Do not recommend position sizes, dollar amounts, or weights. Sizing is handled downstream. Ranking is conviction order only.

### Universe and screen

Apply these filters and **state the value each filter took this run**, so I can reproduce the screen in TradingView or Finviz:

- Market cap ≥ $2bn
- Average daily dollar volume (20d) ≥ $20m
- Price > 200-day SMA
- 6-month relative strength vs. S&P 500 in the top 50%
- ROE ≥ 12% (trailing twelve months)
- Debt-to-equity ≤ 2.0
- Positive free cash flow, trailing twelve months
- Exclude: companies that have reported earnings in the last 3 trading days or will report in the next 5

Report how many names passed each stage.

### Diversification constraints

- No more than 4 names from any one GICS sector
- Include at least 3 names outside mega-cap (< $50bn market cap)
- Flag any pair of picks with 1-year daily return correlation > 0.75, and state the correlation value

### Numeric field definitions — use exactly these, every week

**Ranking** (1–20): conviction order, 1 = highest. Based on strength and certainty of the fundamental case. No ties.

**Regime** (0/1): **market-wide, not per-stock.** 1 if the S&P 500 index closes above its own 200-day SMA, 0 if below. The same value appears on all 20 rows. State the index level and its 200-day SMA.

**Risk Index** (0–100): equal-weighted mean of three percentile ranks, computed *within this week's 20 names*:
- percentile rank of debt-to-equity (higher D/E → higher percentile)
- percentile rank of (1 / current ratio) (lower current ratio → higher percentile)
- percentile rank of 5-year monthly beta vs. S&P 500 (higher beta → higher percentile)

Percentile rank = (position when sorted ascending) / 20 × 100. Report the three raw inputs per stock alongside the composite.

**Volatility Index** (0–100): annualised standard deviation of daily log returns over the trailing 252 trading days, then percentile-ranked within this week's 20 names. Report the raw annualised figure as well as the percentile. If daily returns are unavailable for a name, use 5-year beta as the fallback input and mark that row `[beta-proxy]`.

**Sentiment Index** (0–100): weighted blend, stated to one decimal place —
- 60% × analyst consensus, mapped: Strong Buy = 100, Buy = 75, Hold = 50, Sell = 25, Strong Sell = 0. State the number of analysts covering.
- 40% × 3-month price return minus the 3-month return of the stock's GICS sector ETF, percentile-ranked within this week's 20 names.

Report both components separately as well as the blend.

**Expected Horizon Days** (5–365, integer): how many trading days you expect this thesis to take to play out — the time horizon implied by the specific catalyst(s) cited in "Why Included" (an upcoming earnings re-rating, a multi-quarter margin story, a refinancing event, a product cycle, etc.). State the catalyst driving the number for each stock. This drives option expiry selection downstream, so a vague "medium term" is not acceptable — give a number and say why.

### Prose fields

- **Why Included** — the specific metrics behind the pick: revenue and earnings growth rates, margin trend, moat source, net debt / EBITDA, free cash flow. Cite figures, not adjectives.
- **Valuation** — P/E and PEG vs. the stock's own 5-year median and vs. its sector median. State whether it screens cheap, fair, or expensive on that basis, and by roughly how much.
- **Bear Case** — the specific, named downside risk. Not "market conditions may change." What breaks the thesis, and what would be the observable early sign.

### Current holdings

My current positions: **[PASTE TICKERS AND WEIGHTS HERE — or write "none"]**

Use these only for context: note which current holdings dropped out of the top 20 this week and why, in a short paragraph after the table. Do not let holding a name bias its inclusion or its ranking. The screen decides; incumbency does not.

### Output format

Produce **two files**.

**top20.md** — a table with columns: Ticker, Sector, Why Included, Valuation, Bear Case. Below it: the screen-stage counts, the diversification and correlation summary, the dropped-holdings note, and the data sources with as-of dates.

**top20.csv** — one row per stock, columns exactly:

```
Ticker,Sector,Why Included,Valuation,Bear Case,Ranking,Regime,Risk Index,Volatility Index,Sentiment Index,Expected Horizon Days
```

CSV formatting requirements, non-negotiable:
- The three prose fields will contain commas. Quote every field with double quotes, and escape internal double quotes by doubling them (RFC 4180).
- No line breaks inside fields.
- Numeric fields: integers for Ranking, Regime, and Expected Horizon Days; one decimal place for the three indices.
- Header row exactly as above, no trailing spaces.

Below the CSV, include a short methodology note restating the exact formulas used for the four computed columns, with the raw inputs, so the numbers can be reproduced independently.

---

## Notes for you, not part of the prompt

**A conflict to resolve.** Your original definition made Regime per-stock ("price above its 200-day SMA"). The position-sizing spec treats Regime as a market-wide flag that must be identical on all 20 rows, and fails validation if it isn't. I have written the prompt for the market-wide version, since that is what the downstream code expects. If you want the per-stock version instead, the sizing spec's validation gate needs changing — but note that a per-stock regime flag is redundant here anyway, because "price > 200-day SMA" is already one of the screen filters, so every row would read 1.

**Normalisation was underspecified.** "Normalised against the S&P 500 average" has no single meaning — the prompt now pins all three indices to percentile ranks within the week's 20 names. This is self-normalising, so it stays comparable week to week even if the underlying scales drift. The trade-off: an index of 50 means "middle of this week's basket," not "middle of the market." Since your sizing only ever compares the 20 names against each other, that is the right choice.

**On equal weighting.** Your original prompt asked for equal 5% weights and no overweighting by conviction. That conflicts with your own sizing algorithm, which weights linearly by rank — rank 1 receives roughly twenty times rank 20 before volatility adjustment. I have resolved it by telling the model not to opine on sizing at all, which is cleaner: it ranks, you size.

**The honest caveat.** Even with tool access, an LLM assembling twenty stocks' worth of D/E, current ratio, beta, realised volatility and analyst consensus will occasionally return a figure that is stale, mismatched to the ticker, or silently wrong. The `NA`-rather-than-guess rule and the requirement to state sources are there to make errors visible rather than eliminate them. Spot-check a few figures against Finviz each week before trading on the file.
