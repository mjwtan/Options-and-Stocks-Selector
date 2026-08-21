"""Black-Scholes vs. binomial comparison harness - system-spec.md S5.2's
required mitigation for the "American puts always carry early-exercise
value BS doesn't capture" limitation.

Sweeps 21-90 DTE x 0.20-0.30 delta x 0-4% dividend yield and records the max
observed pricing difference on puts (percent of premium). That number - not
assumption - decides the production behavior:
  - gap < ~2% of premium: Black-Scholes stays the production engine for puts too.
  - gap >= ~2%: switch strike selection and entry pricing to binomial for
    puts specifically (S5.2), leaving Black-Scholes everywhere else
    including the Monte Carlo grid.

Run:
    python benchmarks/bench_options_pricing.py

Writes benchmarks/fixtures/options_pricing_comparison.json.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from options.pricing import delta as bs_delta, price_binomial, price_black_scholes

VALUATION_DATE = date(2024, 1, 2)
SPOT = 100.0
VOL = 0.30
RATE = 0.04
DTE_GRID = [21, 35, 55, 75, 90]
TARGET_DELTAS = [0.20, 0.25, 0.30]
DIV_YIELDS = [0.0, 0.01, 0.02, 0.03, 0.04]
BINOMIAL_STEPS = 200


def _strike_for_put_delta(target_delta: float, expiry: date, div_yield: float) -> float:
    """Search strikes to find the one whose BS put delta is nearest
    -target_delta (mirrors S5.5's real strike-selection procedure, so the
    comparison is measured at the strikes the system will actually trade)."""
    best_strike, best_diff = None, None
    for strike in np.arange(60.0, 100.0, 0.5):
        d = bs_delta("put", SPOT, float(strike), expiry, VALUATION_DATE, RATE, div_yield, VOL)
        diff = abs(d - (-target_delta))
        if best_diff is None or diff < best_diff:
            best_diff, best_strike = diff, float(strike)
    return best_strike


def run():
    results = []
    for dte in DTE_GRID:
        expiry = VALUATION_DATE + timedelta(days=dte)
        for target_delta in TARGET_DELTAS:
            for div_yield in DIV_YIELDS:
                strike = _strike_for_put_delta(target_delta, expiry, div_yield)
                bs_price = price_black_scholes("put", SPOT, strike, expiry, VALUATION_DATE, RATE, div_yield, VOL)
                binom_price = price_binomial(
                    "put", SPOT, strike, expiry, VALUATION_DATE, RATE, div_yield, VOL,
                    steps=BINOMIAL_STEPS, american=True,
                )
                diff_pct = (binom_price - bs_price) / bs_price if bs_price > 0 else float("nan")
                results.append({
                    "dte": dte,
                    "target_delta": target_delta,
                    "div_yield": div_yield,
                    "strike": strike,
                    "bs_price": bs_price,
                    "binomial_american_price": binom_price,
                    "diff_pct_of_premium": diff_pct,
                })

    max_diff = max(r["diff_pct_of_premium"] for r in results)
    max_diff_row = max(results, key=lambda r: r["diff_pct_of_premium"])
    decision = "binomial" if max_diff >= 0.02 else "black_scholes"

    output = {
        "valuation_date": VALUATION_DATE.isoformat(),
        "spot": SPOT,
        "vol": VOL,
        "rate": RATE,
        "binomial_steps": BINOMIAL_STEPS,
        "grid": {"dte": DTE_GRID, "target_deltas": TARGET_DELTAS, "div_yields": DIV_YIELDS},
        "results": results,
        "max_diff_pct_of_premium": max_diff,
        "max_diff_at": max_diff_row,
        "decision_threshold": 0.02,
        "decision": decision,
    }

    out_path = Path(__file__).parent / "fixtures" / "options_pricing_comparison.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))

    print(f"Max BS-vs-binomial gap on puts: {max_diff:.2%} of premium "
          f"(at {max_diff_row['dte']}DTE, delta={max_diff_row['target_delta']}, div={max_diff_row['div_yield']:.0%})")
    print(f"Decision (>= 2% -> binomial for puts): {decision}")
    print(f"Wrote {out_path}")
    return output


if __name__ == "__main__":
    run()
