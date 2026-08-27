"""
Rebalance an Alpaca paper trading account to match target position sizes
from a CSV — normally the target_positions.csv written by
position_sizing.py, but a plain manual CSV also works. Designed to be
re-run weekly with a new CSV: it buys positions under target, trims
positions over target, and closes positions no longer in the CSV at all.

CSV format (header required, column names are case-insensitive):
    ticker,position_size
    AAPL,0.095
    MSFT,0.0725
    ...

`position_size` is a fraction of account equity (0.095 = 9.5%). A plain
"percent" column (8 = 8%) is also accepted for manually-authored CSVs.

If the CSV also carries `instrument`/`occ_symbol`/`option_strike`/
`option_expiry`/`option_contracts` columns - written by position_sizing.py
when run with --options-enabled - short-put/long-call rows also get
executed as real (paper) option orders, per system-spec.md S5/S8. A plain
equity-only CSV without these columns works exactly as before; this is a
pure addition, never required.

Order generation follows position-sizing-spec.md S8 and, for the options
extension, system-spec.md S8.2's five-step mandatory ordering:
    1. Close option positions no longer wanted (dropped, or rolled to a
       different strike/expiry/instrument) - BEFORE equity sells, so
       capital they free up is available sooner.
    2. Equity sells/closes.
    3. Poll until sell/close fills confirm (timeout -> abort remaining, alert).
    4. Equity buys - gated by a capital-reservation check (S5.6): planned
       equity buys plus new cash-secured-put capital (this run's new short
       puts, plus whatever's already reserved by prior-week short puts
       still open) must not exceed buying power, or the run aborts before
       submitting anything further.
    5. New option positions - submitted last, after buying power is
       confirmed by step 4's check.

    - Trade only if abs(target - current) > max(MIN_TRADE_ABS, NO_TRADE_BAND * target)
    - Positions absent from the CSV are exited in full via close_position
      (NOTE: the spec's rank-25 exit hysteresis is NOT implemented — the
      weekly CSV only ever carries ranks 1-20, so there is no rank-21..25
      data to check a dropped name against)
    - Orders below MIN_TRADE_ABS are skipped.

Setup:
    1. Get paper trading API keys from https://app.alpaca.markets/paper/dashboard/overview
    2. Put them in .env:
         ALPACA_API_KEY=xxxx
         ALPACA_SECRET_KEY=xxxx
    3. Run:
         python trade_from_csv.py target_positions.csv --dry-run
         python trade_from_csv.py target_positions.csv
"""

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

from brokers.alpaca_equity import AlpacaEquityBroker
from brokers.base import EquityBroker, OptionsBroker, OrderSide

load_dotenv()

TICKER_ALIASES = {"ticker", "symbol", "stock"}
SIZE_ALIASES = {"position_size", "weight_final", "weight"}          # fraction, e.g. 0.095
PERCENT_ALIASES = {"percent", "percentage", "allocation", "pct"}     # percent, e.g. 9.5

NO_TRADE_BAND = 0.20      # fractional deviation from target before trading (spec S10)
MIN_TRADE_ABS = 25.0      # absolute $ floor on trade size (spec S10)
FILL_POLL_TIMEOUT = 60    # seconds to wait for a sell/close to confirm before aborting buys
FILL_POLL_INTERVAL = 2


def load_targets(csv_path):
    """Returns dict of {ticker: percent}, deduping tickers by summing their percents.
    Accepts either a `position_size` fraction column or a `percent` column."""
    targets = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV appears to be empty (no header row).")

        norm = {name: name.strip().lower() for name in reader.fieldnames}
        ticker_col = next((n for n, low in norm.items() if low in TICKER_ALIASES), None)
        size_col = next((n for n, low in norm.items() if low in SIZE_ALIASES), None)
        percent_col = next((n for n, low in norm.items() if low in PERCENT_ALIASES), None)

        if ticker_col is None or (size_col is None and percent_col is None):
            raise ValueError(
                f"Could not find ticker + position_size/percent columns in header {reader.fieldnames}. "
                f"Expected one of {TICKER_ALIASES} and one of {SIZE_ALIASES | PERCENT_ALIASES}."
            )
        is_fraction = size_col is not None
        value_col = size_col or percent_col

        for i, row in enumerate(reader, start=2):
            raw_ticker = (row.get(ticker_col) or "").strip().upper()
            raw_value = (row.get(value_col) or "").strip()
            if not raw_ticker:
                continue
            try:
                value = float(raw_value)
            except ValueError:
                raise ValueError(f"Row {i}: could not parse value '{raw_value}' for {raw_ticker}")
            percent = value * 100 if is_fraction else value
            if percent <= 0:
                continue
            if raw_ticker in targets:
                print(f"  note: {raw_ticker} appears more than once, summing")
                targets[raw_ticker] += percent
            else:
                targets[raw_ticker] = percent
    return targets


@dataclass
class OptionTarget:
    ticker: str
    instrument: str          # "short_put" or "long_call"
    occ_symbol: str
    strike: Optional[float]
    expiry: Optional[str]
    contracts: int


def load_option_targets(csv_path) -> dict:
    """Returns {occ_symbol: OptionTarget} for rows with a short_put/
    long_call instrument and a usable occ_symbol/contracts. Returns {} if
    the CSV lacks these columns entirely - a plain ticker+position_size CSV
    (or an options-disabled position_sizing.py run) is unaffected."""
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return {}
        norm = {name: name.strip().lower() for name in reader.fieldnames}
        ticker_col = next((n for n, low in norm.items() if low in TICKER_ALIASES), None)
        instrument_col = next((n for n, low in norm.items() if low == "instrument"), None)
        occ_col = next((n for n, low in norm.items() if low == "occ_symbol"), None)
        contracts_col = next((n for n, low in norm.items() if low == "option_contracts"), None)
        strike_col = next((n for n, low in norm.items() if low == "option_strike"), None)
        expiry_col = next((n for n, low in norm.items() if low == "option_expiry"), None)

        if not all([ticker_col, instrument_col, occ_col, contracts_col]):
            return {}

        targets = {}
        for row in reader:
            instrument = (row.get(instrument_col) or "").strip()
            if instrument not in ("short_put", "long_call"):
                continue
            ticker = (row.get(ticker_col) or "").strip().upper()
            occ_symbol = (row.get(occ_col) or "").strip()
            contracts_raw = (row.get(contracts_col) or "").strip()
            if not ticker or not occ_symbol or not contracts_raw:
                continue
            try:
                contracts = int(float(contracts_raw))
            except ValueError:
                continue
            if contracts < 1:
                continue
            strike = None
            if strike_col and (row.get(strike_col) or "").strip():
                try:
                    strike = float(row[strike_col])
                except ValueError:
                    strike = None
            expiry = (row.get(expiry_col) or "").strip() if expiry_col else None
            targets[occ_symbol] = OptionTarget(
                ticker=ticker, instrument=instrument, occ_symbol=occ_symbol,
                strike=strike, expiry=expiry or None, contracts=contracts,
            )
    return targets


def plan_option_actions(option_targets: dict, current_option_positions: list):
    """Returns (close_occ_symbols, open_targets).
    close: currently-held contracts no longer in this week's targets - the
      underlying dropped out, or the position was rolled to a different
      strike/expiry (a new occ_symbol replaces the old one).
    open: this week's targets not already held, matched by exact contract
      (occ_symbol) rather than just underlying, so a roll produces both a
      close and an open rather than being missed as "already have a put on
      this name"."""
    held_occ = {p.occ_symbol for p in current_option_positions}
    wanted_occ = set(option_targets.keys())
    close_occ = sorted(held_occ - wanted_occ)
    open_targets = [option_targets[occ] for occ in sorted(wanted_occ - held_occ)]
    return close_occ, open_targets


def close_option_positions(options_broker: OptionsBroker, occ_symbols, current_option_positions,
                            equity_broker: EquityBroker = None, poll_timeout: int = FILL_POLL_TIMEOUT):
    """Step 1 of S8.2 - closes option positions no longer wanted, via an
    offsetting order (buy-to-close a short, sell-to-close a long).

    Polls for fill confirmation when equity_broker is given (order_status
    is asset-class-agnostic on Alpaca, so the same lookup that confirms
    equity fills works for option order IDs too). This isn't decoration:
    live testing found option orders can sit unfilled indefinitely - a
    resting limit at the mid doesn't cross the spread (fixed in
    brokers/alpaca_options.py), and a genuinely illiquid contract may not
    fill at any price if there's no real trade flow to match against. A
    stuck close order left silent would mean a position you think is gone
    is actually still open. Returns the list of occ_symbols that didn't
    confirm filled, for the caller to alert on.
    """
    positions_by_occ = {p.occ_symbol: p for p in current_option_positions}
    unfilled = []
    for occ in occ_symbols:
        pos = positions_by_occ.get(occ)
        if pos is None:
            continue
        side = OrderSide.BUY if pos.qty < 0 else OrderSide.SELL
        try:
            contract = options_broker.latest_contract_quote(occ)
            o = options_broker.submit_option(contract, abs(pos.qty), side)
            print(f"  {occ:<24} CLOSE  {abs(pos.qty)} contract(s) submitted, order id {o.id}")
            if equity_broker is not None:
                result = wait_for_fill(equity_broker, o.id, timeout=poll_timeout)
                if result is None or not result.status.is_ok:
                    status = result.status.value if result else "unknown"
                    print(f"  {occ:<24} CLOSE  ALERT: not confirmed filled within {poll_timeout}s "
                          f"(status: {status}) - position may still be open, check manually")
                    unfilled.append(occ)
                else:
                    print(f"  {occ:<24} CLOSE  confirmed filled")
        except Exception as e:
            print(f"  {occ:<24} CLOSE  FAILED: {e}")
            unfilled.append(occ)
    return unfilled


def open_option_positions(options_broker: OptionsBroker, targets, equity_broker: EquityBroker = None,
                           poll_timeout: int = FILL_POLL_TIMEOUT):
    """Step 5 of S8.2 - submitted last, after equity buys, since cash-
    secured puts need confirmed buying power (S8.2). See
    close_option_positions' docstring on why fill confirmation matters
    here. Returns the list of occ_symbols that didn't confirm filled."""
    unfilled = []
    for t in targets:
        side = OrderSide.SELL if t.instrument == "short_put" else OrderSide.BUY
        try:
            contract = options_broker.latest_contract_quote(t.occ_symbol)
            o = options_broker.submit_option(contract, t.contracts, side)
            print(f"  {t.occ_symbol:<24} OPEN   {t.instrument} x{t.contracts} submitted, order id {o.id}")
            if equity_broker is not None:
                result = wait_for_fill(equity_broker, o.id, timeout=poll_timeout)
                if result is None or not result.status.is_ok:
                    status = result.status.value if result else "unknown"
                    print(f"  {t.occ_symbol:<24} OPEN   ALERT: not confirmed filled within {poll_timeout}s "
                          f"(status: {status}) - check manually, it may still fill later or need cancelling")
                    unfilled.append(t.occ_symbol)
                else:
                    print(f"  {t.occ_symbol:<24} OPEN   confirmed filled")
        except Exception as e:
            print(f"  {t.occ_symbol:<24} OPEN   FAILED: {e}")
            unfilled.append(t.occ_symbol)
    return unfilled


def get_basis_value(account, basis):
    return float(getattr(account, basis))


def get_current_values(equity_broker: EquityBroker):
    """Returns dict of {symbol: current market value (float)} for open positions."""
    return {p.symbol: float(p.market_value) for p in equity_broker.positions()}


def plan_actions(targets, current_values, basis_value, no_trade_band=NO_TRADE_BAND, min_trade_abs=MIN_TRADE_ABS):
    """
    Compares target % allocations against current $ position values and
    returns a list of planned actions: (symbol, kind, current_value, target_value, amount)
    kind is one of "buy", "sell", "close".
    """
    actions = []
    all_symbols = set(targets) | set(current_values)

    for symbol in sorted(all_symbols):
        percent = targets.get(symbol)
        current_value = current_values.get(symbol, 0.0)

        if percent is None:
            # Held but no longer in the target list -> close entirely (spec S8).
            if current_value > 0:
                actions.append((symbol, "close", current_value, 0.0, current_value))
            continue

        target_value = basis_value * percent / 100
        diff = target_value - current_value

        no_trade_threshold = max(min_trade_abs, no_trade_band * target_value)
        if abs(diff) <= no_trade_threshold:
            continue  # within the no-trade band

        if diff > 0:
            actions.append((symbol, "buy", current_value, target_value, round(diff, 2)))
        else:
            actions.append((symbol, "sell", current_value, target_value, round(-diff, 2)))

    return actions


def print_plan(actions, basis_value):
    print(f"{'SYMBOL':<8} {'ACTION':<6}{'CURRENT':>12}{'TARGET':>12}{'ORDER $':>12}")
    for symbol, kind, current_value, target_value, amount in actions:
        print(f"{symbol:<8} {kind.upper():<6}{current_value:>12,.2f}{target_value:>12,.2f}{amount:>12,.2f}")
    if not actions:
        print("(nothing to do — positions already match targets)")


def wait_for_fill(equity_broker: EquityBroker, order_id, timeout=FILL_POLL_TIMEOUT, poll_interval=FILL_POLL_INTERVAL):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = equity_broker.order_status(order_id)
        if last.status.is_terminal:
            return last
        time.sleep(poll_interval)
    return last  # may be non-terminal (timeout)


def execute_actions(
    equity_broker: EquityBroker,
    actions,
    min_trade_abs=MIN_TRADE_ABS,
    poll_timeout=FILL_POLL_TIMEOUT,
    options_broker: Optional[OptionsBroker] = None,
    close_option_occ=None,
    open_option_targets=None,
    current_option_positions=None,
    account_buying_power: Optional[float] = None,
    existing_reserved_capital: float = 0.0,
):
    """The five-step S8.2 mandatory order: close options -> equity sells ->
    confirm -> equity buys (capital-reservation gated) -> open options."""
    close_option_occ = close_option_occ or []
    open_option_targets = open_option_targets or []
    current_option_positions = current_option_positions or []

    sells = [a for a in actions if a[1] in ("close", "sell")]
    buys = [a for a in actions if a[1] == "buy"]

    # Step 1: close option positions no longer wanted - before equity sells,
    # per S8.2.
    if options_broker is not None and close_option_occ:
        print("Closing option positions no longer in target:")
        close_option_positions(options_broker, close_option_occ, current_option_positions,
                                equity_broker=equity_broker, poll_timeout=poll_timeout)
        print()

    # Step 2: equity sells/closes.
    sell_order_ids = []
    for symbol, kind, current_value, target_value, amount in sells:
        try:
            if kind == "close":
                # Full exits use close_position, not a computed notional, to avoid fractional dust.
                o = equity_broker.close_position(symbol)
                print(f"  {symbol:<6} CLOSE  full position (${amount:,.2f}) submitted, order id {o.id}")
                sell_order_ids.append(o.id)
            else:
                if amount < min_trade_abs:
                    print(f"  {symbol:<6} SKIP   sell ${amount:,.2f} below ${min_trade_abs:.0f} minimum")
                    continue
                o = equity_broker.submit_notional(symbol, amount, OrderSide.SELL)
                print(f"  {symbol:<6} SELL   ${amount:>10,.2f}  order id {o.id} [{o.status.value}]")
                sell_order_ids.append(o.id)
        except Exception as e:
            print(f"  {symbol:<6} {kind.upper():<6} ${amount:>10,.2f}  FAILED: {e}")

    # Step 3: poll for confirmation before anything spends capital.
    if sell_order_ids:
        print("\nWaiting for sell/close orders to confirm before submitting buys...")
        unfilled = []
        for oid in sell_order_ids:
            o = wait_for_fill(equity_broker, oid, timeout=poll_timeout)
            status = o.status if o is not None else "unknown"
            if o is None or not o.status.is_ok:
                unfilled.append((oid, status))
        if unfilled:
            print(f"ALERT: {len(unfilled)} sell/close order(s) did not confirm filled: {unfilled}")
            print("Aborting remaining buy/open orders — re-run once the account settles.")
            return

    # Capital reservation check (S5.6) - planned equity buys + new short-put
    # capital + whatever's already reserved by prior-week short puts must
    # not exceed buying power. This is an in-run guard, not a persistent
    # broker-side reservation (see brokers/alpaca_options.py's
    # buying_power_reserved docstring) - it only protects this run's own
    # order sequence from overcommitting.
    planned_buy_total = sum(amount for _s, _k, _cv, _tv, amount in buys if amount >= min_trade_abs)
    new_short_put_targets = [t for t in open_option_targets if t.instrument == "short_put"]
    if account_buying_power is not None and (new_short_put_targets or existing_reserved_capital):
        from options.sizing import capital_reserved_for_short_put

        unresolved = [t for t in new_short_put_targets if t.strike is None]
        new_reserved = sum(
            capital_reserved_for_short_put(t.strike, t.contracts)
            for t in new_short_put_targets if t.strike is not None
        )
        if unresolved:
            print(f"  note: {len(unresolved)} new short put(s) missing a strike in the CSV - "
                  f"not included in the capital-reservation check below")

        total_committed = planned_buy_total + new_reserved + existing_reserved_capital
        print(
            f"Capital check: buying power ${account_buying_power:,.2f} vs planned equity buys "
            f"${planned_buy_total:,.2f} + new short-put capital ${new_reserved:,.2f} + "
            f"already-reserved ${existing_reserved_capital:,.2f} = ${total_committed:,.2f}"
        )
        if total_committed > account_buying_power:
            print(
                f"ALERT: planned commitments (${total_committed:,.2f}) exceed buying power "
                f"(${account_buying_power:,.2f}). Aborting remaining buy/open orders — "
                f"re-run after positions settle or targets shrink."
            )
            return

    # Step 4: equity buys.
    for symbol, kind, current_value, target_value, amount in buys:
        if amount < min_trade_abs:
            print(f"  {symbol:<6} SKIP   buy ${amount:,.2f} below ${min_trade_abs:.0f} minimum")
            continue
        try:
            o = equity_broker.submit_notional(symbol, amount, OrderSide.BUY)
            print(f"  {symbol:<6} BUY    ${amount:>10,.2f}  order id {o.id} [{o.status.value}]")
        except Exception as e:
            print(f"  {symbol:<6} BUY    ${amount:>10,.2f}  FAILED: {e}")

    # Step 5: new option positions - last, after equity buys, since cash-
    # secured puts need confirmed buying power (S8.2).
    if options_broker is not None and open_option_targets:
        print("\nOpening new option positions:")
        open_option_positions(options_broker, open_option_targets, equity_broker=equity_broker, poll_timeout=poll_timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", help="Path to CSV file with ticker + position_size (or percent) columns")
    parser.add_argument(
        "--basis",
        choices=["equity", "cash", "buying_power", "portfolio_value"],
        default="equity",
        help="Account value used as the 100%% base for sizing (default: equity)",
    )
    parser.add_argument("--no-trade-band", type=float, default=NO_TRADE_BAND, help="Fractional deviation before trading (default: 0.20)")
    parser.add_argument("--min-trade-abs", type=float, default=MIN_TRADE_ABS, help="Absolute $ floor on trade size (default: 25)")
    parser.add_argument("--poll-timeout", type=int, default=FILL_POLL_TIMEOUT, help="Seconds to wait for sell/close fills before aborting buys")
    parser.add_argument("--dry-run", action="store_true", help="Compute and print planned trades without submitting them")
    parser.add_argument(
        "--options-enabled", action="store_true",
        help="Also execute short-put/long-call rows from a position_sizing.py --options-enabled CSV "
             "(instrument/occ_symbol/option_* columns). No effect on a plain equity CSV.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: set ALPACA_API_KEY and ALPACA_SECRET_KEY (in .env or env vars) — paper trading keys.")
        print("Get them at https://app.alpaca.markets/paper/dashboard/overview")
        sys.exit(1)

    try:
        targets = load_targets(args.csv_path)
    except (ValueError, OSError) as e:
        print(f"ERROR reading CSV: {e}")
        sys.exit(1)

    option_targets = load_option_targets(args.csv_path) if args.options_enabled else {}

    # Only bail here if there's truly nothing to do - a CSV can legitimately
    # carry option targets with no (or all-zero) equity rows.
    if not targets and not option_targets:
        print("No valid target positions found in CSV.")
        sys.exit(1)

    total_percent = sum(targets.values())
    if targets:
        print(f"Loaded {len(targets)} target positions totaling {total_percent:.2f}% of {args.basis}.")
        if total_percent > 100:
            print(f"WARNING: total percent ({total_percent:.2f}%) exceeds 100% — buying power may not cover all orders.")

    equity_broker = AlpacaEquityBroker(api_key, secret_key, paper=True)

    account = equity_broker.account()
    basis_value = get_basis_value(account, args.basis)
    current_values = get_current_values(equity_broker)

    print(f"Account {args.basis}: ${basis_value:,.2f}")
    print(f"Account buying power: ${float(account.buying_power):,.2f}")
    print(f"Currently holding {len(current_values)} position(s).")
    print()

    actions = plan_actions(targets, current_values, basis_value, args.no_trade_band, args.min_trade_abs)
    print_plan(actions, basis_value)
    print()

    # Options extension - only touches anything if --options-enabled AND the
    # CSV actually carries the instrument/occ_symbol columns (a plain
    # equity CSV has option_targets == {} here and every options_* variable
    # below stays empty, so the rest of the run is unaffected).
    options_broker = None
    close_option_occ = []
    open_option_targets = []
    current_option_positions = []
    existing_reserved_capital = 0.0

    if option_targets:
        from brokers.alpaca_options import AlpacaOptionsBroker

        options_broker = AlpacaOptionsBroker(api_key, secret_key, paper=True)
        current_option_positions = options_broker.option_positions()
        close_option_occ, open_option_targets = plan_option_actions(option_targets, current_option_positions)
        existing_reserved_capital = float(options_broker.buying_power_reserved())

        print(f"Loaded {len(option_targets)} option target(s) from CSV.")
        print(f"Currently holding {len(current_option_positions)} option position(s), "
              f"${existing_reserved_capital:,.2f} already reserved for short puts.")
        print(f"Option plan: close {len(close_option_occ)}, open {len(open_option_targets)}.")
        for occ in close_option_occ:
            print(f"  {occ:<24} CLOSE")
        for t in open_option_targets:
            print(f"  {t.occ_symbol:<24} OPEN   {t.instrument} x{t.contracts}")
        print()

    if args.dry_run:
        print(f"Dry run: {len(actions)} equity action(s) and "
              f"{len(close_option_occ) + len(open_option_targets)} option action(s) planned, nothing submitted.")
        return

    if not actions and not close_option_occ and not open_option_targets:
        print("Nothing to do.")
        return

    execute_actions(
        equity_broker, actions, min_trade_abs=args.min_trade_abs, poll_timeout=args.poll_timeout,
        options_broker=options_broker, close_option_occ=close_option_occ, open_option_targets=open_option_targets,
        current_option_positions=current_option_positions,
        account_buying_power=float(account.buying_power) if options_broker is not None else None,
        existing_reserved_capital=existing_reserved_capital,
    )
    print()
    print(f"Done: {len(actions)} equity action(s), "
          f"{len(close_option_occ) + len(open_option_targets)} option action(s) processed.")


if __name__ == "__main__":
    main()
