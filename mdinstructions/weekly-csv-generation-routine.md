# Weekly Top-20 CSV Generation — Claude Code Routine

This is the automation that produces `top20.csv`/`top20.md` each week — the one
input this whole system doesn't compute itself (system-spec.md §0, Layer 1:
"a judgment we do not attempt to reproduce"). It runs as a **Claude Code
Routine** (a scheduled cloud agent), not a GitHub Actions workflow, because it
needs live research tool access (Bigdata.com for fundamentals/ratios/analyst
data, WebSearch/WebFetch for the rest) that a plain CI runner doesn't have.

Because a Routine's configuration lives in Claude's hosted UI rather than a
file in this repo, there is nothing to run `git diff` against if it changes —
this file is the only versioned record of what it actually does. If you
change the routine's instructions, update this file to match, in the same
commit, or the two will drift silently.

- **Named:** `Artemis Discovery - Weekly Top20 CSV Generation`
- **Schedule:** weekdays, 11:00 UTC (07:00 America/New_York) — a deliberate
  ~75 minute buffer before `weekly.yml`'s 12:15 UTC Weekly Sizing run, so a
  fresh CSV has time to land before it's consumed.
- **Runs with:** this repo (`mjwtan/Options-and-Stocks-Selector`), the
  Bigdata.com connector, and a Claude Code Remote connector (for the git
  commit/push step).
- **Model:** Sonnet 5.

Same holiday-aware gating as `weekly.yml`, reusing the identical script
rather than reimplementing the logic in two places — see `scheduling/is_weekly_run_day.py`.

---

## Why Artemis Discovery, not a single LLM pass

Step 2 is invoked through **Artemis Discovery** rather than as a single,
one-shot LLM completion — a deliberate methodology choice, not just a tool
name. Artemis's Discover process treats a goal as something to be
*experimented on*, not answered once:

1. **The goal is decomposed into Experiments** — distinct hypotheses about
   how to best satisfy it, rather than one fixed approach.
2. **Each Experiment produces multiple independent candidate Versions**, run
   in parallel rather than sequentially refined from a single attempt — so a
   weak first answer doesn't anchor everything downstream of it.
3. **Every version is validated against real, automated checks** before it's
   trusted — here, that's the same structural and content-provenance
   discipline `volatility-prompt.md` demands (exact CSV schema, the
   `[R]`/`[E]`/`NA` provenance marking, the 20-row requirement) — a version
   that violates that discipline doesn't survive.
4. **One or more independent reviewer models score each candidate** before
   further budget is spent on it — a second (or third) opinion on the
   reasoning, not just the first model grading its own homework.
5. **Only the best-validated version is carried forward** — here, that
   becomes this week's actual `top20.csv`/`top20.md`.

**Why this matters for a stock screen specifically, more than it would for
most tasks:** the output of this step isn't advisory — `position_sizing.py`
sizes real (paper) capital against whatever ranking comes out of it, with no
human review of the *research* itself before that happens (only of the
resulting trades, per §8.4). A single LLM pass has no mechanism to catch its
own weak reasoning, a thin catalyst dressed up as a strong one, or a
subtly-wrong number it was confident about — whatever it produces first is
what you get. Running several independent candidate analyses and having
separate reviewer models score them before one is trusted is a genuine,
structural check against exactly that failure mode, not just a more
expensive way to ask the same question.

---

## The routine's instructions, verbatim

You are running as a scheduled cloud agent in the mjwtan/Options-and-Stocks-Selector repo. This fires every weekday at 11:00 UTC (07:00 America/New_York), but it must only actually do work on the week's genuine first NYSE trading day (Monday normally, Tuesday if Monday is an NYSE holiday) - identical holiday logic to the existing weekly sizing job.

### Step 0 - gate

Run: `python scheduling/is_weekly_run_day.py`

If it exits non-zero, today is NOT the first trading day of the week. Stop immediately - do not do any of the research below, do not write or commit any files. Just end with a one-line note that today was skipped.

If it exits zero, note today's date (UTC, format YYYY-MM-DD) - you'll need it for file naming in Step 3 - and proceed to Step 1.

### Step 1 - determine current holdings

List the subdirectories of `history/` (they are named YYYY-MM-DD). Pick the lexicographically greatest one (= most recent date). If `history/<that-date>/target_positions.csv` exists and is non-empty, read its 'ticker' column - those tickers are your 'current holdings' for Step 2's context. If `history/` has no subdirectories, or that file is missing/empty, treat current holdings as empty.

### Step 2 - run the Artemis Discovery weekly top-20 volatility screen

Produce this week's top 20 stock selection and return two files: `top20.csv` and `top20.md`, following the exact specification below (this is the same recurring weekly prompt each run - do not deviate from the format).

---

Using Artemis Discovery, produce this week's top 20 stock selection and return two files: top20.csv and top20.md.

**Role:** you are producing a weekly equity selection file for a systematic options and equity portfolio. The downstream system computes its own volatility, correlations and position sizes - your job is selection and ranking, not sizing.

**Objective**

Identify 20 US-listed equities with elevated realised and implied volatility where there is a directional case over the next 2-8 weeks. The portfolio expresses these via shares, long calls, or short puts depending on the options market, so names need to be liquid enough to have a tradable options chain. This is a volatility-seeking mandate, not a quality or value screen. Stable large-cap compounders are not the target.

**Screen**

Apply these filters and state the value each took this run, plus how many names passed each stage:
- Market cap >= $2bn
- Average daily dollar volume (20d) >= $20m
- Listed options chain available, with open interest >= 500 across the front two monthly expiries
- 60-day realised volatility >= 30% annualised
- 60-day realised volatility in the top 40% of the screened universe
- Average option bid-ask spread near the money <= 15% of mid
- Exclude: names reporting earnings within the next 5 trading days

Report the count surviving each stage.

**Diversification**
- No more than 4 names from any one GICS sector
- At least 3 names outside mega-cap (< $50bn)
- Flag any pair with 1-year daily return correlation > 0.75, stating the value

**Ranking**

Rank 1-20 by conviction in the directional thesis, strict order, no ties or gaps. Rank on the strength and specificity of the catalyst or trend, not on volatility magnitude - volatility is a screen criterion, not a ranking criterion.

**Numeric columns - use exactly these definitions every week**

Ranking (1-20): as above.

Regime (0/1): market-wide. 1 if the S&P 500 closes above its own 200-day SMA, 0 if below. Identical on all 20 rows. State the index level and its 200-day SMA.

Risk Index (0-100): equal-weighted mean of three percentile ranks computed within this week's 20 names:
- debt-to-equity (higher -> higher percentile)
- 1 / current ratio (lower current ratio -> higher percentile)
- 5-year monthly beta vs. S&P 500 (higher -> higher percentile)
Percentile = (ascending sort position / 20) x 100. Report the three raw inputs per stock.

Volatility Index (0-100): annualised standard deviation of daily log returns over the trailing 252 trading days, percentile-ranked within this week's 20 names. Report the raw annualised figure alongside the percentile.

Sentiment Index (0-100), to one decimal:
- 60% x analyst consensus, mapped Strong Buy = 100, Buy = 75, Hold = 50, Sell = 25, Strong Sell = 0. State analyst count.
- 40% x (3-month stock return - 3-month GICS sector ETF return), percentile-ranked within this week's 20 names.
Report both components separately as well as the blend.

Expected Horizon Days (int): how long you expect the thesis to take to play out, in calendar days. This drives option expiry selection downstream - a 30-day catalyst and a 90-day trend need different contracts. Range 14-120. Base it on the specific catalyst, not a default.

**Data handling - always return 20 rows**

Return 20 rows. Never drop a stock for incomplete data, and never return an empty file.

Mark every numeric cell with its provenance:
- [R] retrieved from a named source this run
- [E] estimated or from prior knowledge - state the basis briefly
- NA genuinely unavailable

Add a Data Quality column: count of [R] cells / total numeric cells for that row, as a decimal 0-1.

If an index input is NA, compute the index from the remaining components and state which were omitted. Do not substitute a neutral placeholder.

Before screening, retrieve one test metric (AAPL debt-to-equity) and report what came back - this confirms the data layer is live before the rest of the run depends on it.

If a stage eliminates everything, state which metric caused it rather than returning an empty result.

**Current holdings**

Use whatever you found in Step 1 above (empty, or the tickers from `history/<latest-date>/target_positions.csv`). Use these only for context: note which current holdings dropped out of the top 20 this week and why, in a short paragraph after the table. Do not let holding a name bias its inclusion or its ranking. The screen decides; incumbency does not.

**Output**

top20.md - table with Ticker, Sector, Why Included, Valuation, Bear Case, Volatility Profile. Below it: screen-stage counts, diversification and correlation summary, dropped-holdings note, and data sources with as-of dates.

Prose field requirements:
- Why Included - the specific directional case: catalyst, trend, or dislocation, with figures. What moves this stock in the next 2-8 weeks.
- Valuation - P/E and PEG vs. the stock's own 5-year median and its sector median. Cheap, fair or expensive, and roughly by how much.
- Bear Case - the specific downside risk and the observable early sign that the thesis is failing.
- Volatility Profile - realised 60d vol, what is driving it, and whether it is expected to persist or decay.

top20.csv - one row per stock, columns exactly:
`Ticker,Sector,Why Included,Valuation,Bear Case,Ranking,Regime,Risk Index,Volatility Index,Sentiment Index,Expected Horizon Days,Data Quality`

CSV formatting, non-negotiable:
- Prose fields contain commas. Quote every field with double quotes; escape internal quotes by doubling (RFC 4180)
- No line breaks inside fields
- Integers for Ranking, Regime, Expected Horizon Days; one decimal for the three indices and Data Quality
- Header row exactly as above, no trailing spaces
- top20.csv contains ONLY the header row plus exactly 20 data rows. Nothing else - no blank line, no methodology note, no commentary, no trailing text of any kind, anywhere in that file. This file is machine-parsed directly; a single stray line breaks the parser for the whole file.

Put the methodology note (restating the exact formulas used for the computed columns with their raw inputs, so the numbers can be reproduced independently) in top20.md only, in its own section after the table.

Do not recommend position sizes, weights, or dollar amounts. Sizing is handled downstream.

---

Use the Bigdata.com MCP connector for company fundamentals, ratios, analyst ratings, earnings calendars, and market/sector data (find_securities then company_tearsheet; market_tearsheet for the S&P 500 level, 200-day SMA context, and sector ETF returns). Use WebSearch/WebFetch to fill gaps (earnings-date confirmation, catalyst news, PDUFA/short-interest calendars) and for anything Bigdata.com doesn't cover. Follow the same honest data-provenance discipline as prior runs: mark [R]/[E]/NA accurately, and do not fabricate a precise realized-volatility figure if no daily-return series was retrievable - estimate it plainly from range/beta/momentum and mark it [E].

### Step 3 - write files and commit

Write FOUR files (the sizing job at 08:15 ET hardcodes the undated names, so both must exist):
- top20.csv and top20.md - the canonical 'latest' copies, overwritten each run (this is what position_sizing.py consumes downstream)
- top20_<today's UTC date, YYYY-MM-DD from Step 0>.csv and the matching .md - a dated archive copy with identical content, so each week's file is preserved under its own name

Then:

```
git config user.name "claude-automation[bot]"
git config user.email "claude-automation[bot]@users.noreply.github.com"
git add top20.csv top20.md top20_*.csv top20_*.md
git commit -m "Automated weekly Artemis Discovery top20 screen ($(date -u +%Y-%m-%d))"
git push || (git fetch origin main && git rebase origin/main && git push)
```

Report a short summary at the end: whether today was a run day or a skip day, and (if it ran) the 20 tickers selected, the current-holdings source used (history date or 'none found'), and any data-quality caveats worth flagging before the 08:15 ET sizing job consumes this file.

---

## Relationship to `volatility-prompt.md`

`volatility-prompt.md` (repo root) is the same Step 2 specification, without
the Steps 0/1/3 automation wrapper — that's the version to paste into a plain
LLM chat if you ever want to run the screen manually instead of waiting for
the schedule. Keep the two in sync: if Step 2's methodology changes here, it
needs to change there too, and vice versa.

An earlier, superseded version of this prompt (predating the CSV-format fix
described in system-spec.md §2.2, and not matching the current
`position_sizing.py` schema) used to live at
`mdinstructions/weekly-stock-prompt.md` — removed rather than kept for
history, since it wasn't cited by section number anywhere and risked being
used as a template by mistake. `volatility-prompt.md` is the only version
to use.
