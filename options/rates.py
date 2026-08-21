"""Risk-free rate for options pricing (S5.2: "current T-bill yield,
refreshed weekly"). No dedicated rates provider is configured, so this
reuses yfinance (already a dependency for the earnings fallback, no key
needed) to pull ^IRX, the 13-week T-bill discount yield, quoted in percent.
Falls back to a fixed constant if the fetch fails - logged, never silent.
"""

from __future__ import annotations

from typing import Optional

import yfinance as yf

DEFAULT_RATE = 0.04


def fetch_risk_free_rate() -> tuple[float, str]:
    """Returns (rate, source_note)."""
    try:
        hist = yf.Ticker("^IRX").history(period="5d")
        if hist.empty:
            raise ValueError("empty ^IRX history")
        rate = float(hist["Close"].iloc[-1]) / 100.0
        return rate, f"^IRX 13-week T-bill, {rate:.2%}"
    except Exception as e:
        return DEFAULT_RATE, f"^IRX fetch failed ({e}), using default {DEFAULT_RATE:.2%}"


def fetch_dividend_yield(ticker: str) -> float:
    """Trailing annual dividend yield as a decimal fraction (0.004 = 0.4%).
    Feeds S5.2's pricing process - the bench_options_pricing.py harness
    showed div_yield materially moves an American put's early-exercise
    value, so a per-name figure is worth the extra fetch over a flat 0.
    Defaults to 0.0 (non-payer) on any lookup failure - never fabricated."""
    try:
        info = yf.Ticker(ticker).info
        yld = info.get("trailingAnnualDividendYield")
        return float(yld) if yld else 0.0
    except Exception:
        return 0.0
