"""
Prints current Alpaca positions formatted for the weekly prompt's "Current
holdings" section (see volatility-prompt.md) - paste the output straight in.

Equity positions are shown as ticker + % of account equity. Option
positions are shown separately (weight isn't a meaningful comparison for
a leveraged options position the way it is for a share) since the prompt
only needs enough context to judge "did a current holding drop out and
why" - it doesn't size anything.

Usage:
    python current_positions.py
"""

import os
import sys

from dotenv import load_dotenv

from brokers.alpaca_equity import AlpacaEquityBroker

load_dotenv()


def main():
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: set ALPACA_API_KEY and ALPACA_SECRET_KEY (in .env or env vars).")
        sys.exit(1)

    equity_broker = AlpacaEquityBroker(api_key, secret_key, paper=True)
    account = equity_broker.account()
    positions = equity_broker.positions()

    if not positions:
        print("none")
        return

    weighted = sorted(
        ((p.symbol, p.market_value / account.equity * 100) for p in positions),
        key=lambda x: -x[1],
    )

    print("Paste this into the prompt's Current holdings section:\n")
    print(", ".join(f"{symbol} {weight:.1f}%" for symbol, weight in weighted))

    print(f"\n({len(positions)} position(s), {sum(w for _, w in weighted):.1f}% of ${account.equity:,.0f} equity)")

    # Option positions, if any (OPTIONS_ENABLED runs) - shown for context,
    # not folded into the equity weight list above.
    try:
        from brokers.alpaca_options import AlpacaOptionsBroker
        options_broker = AlpacaOptionsBroker(api_key, secret_key, paper=True)
        option_positions = options_broker.option_positions()
        if option_positions:
            print("\nOption positions (for your own context, not part of the paste above):")
            for p in option_positions:
                side = "short" if p.qty < 0 else "long"
                print(f"  {side} {p.underlying} {p.contract_type} ${p.strike:g} exp {p.expiry} x{abs(p.qty)}")
    except Exception:
        pass  # equity-only setups shouldn't error out just because options aren't configured


if __name__ == "__main__":
    main()
