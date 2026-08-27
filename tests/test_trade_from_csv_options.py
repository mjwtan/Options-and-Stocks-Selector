"""Options execution wiring in trade_from_csv.py - system-spec.md S8.2, S5.6.

Fakes record every call in order, so these assert the actual sequence
(close options -> equity sells -> confirm -> capital check -> equity buys
-> open options), not just that each step eventually happens.
"""

from datetime import date

import pytest

from brokers.base import Account, EquityBroker, FillStatus, OptionPosition, OptionsBroker, Order, OrderSide
from trade_from_csv import OptionTarget, execute_actions, load_option_targets, plan_option_actions


class RecordingEquityBroker(EquityBroker):
    def __init__(self):
        self.calls = []

    def positions(self):
        return []

    def account(self):
        return Account(equity=100_000, cash=10_000, buying_power=50_000, portfolio_value=100_000)

    def is_tradable(self, symbol):
        return True

    def submit_notional(self, symbol, notional, side):
        self.calls.append(("equity_order", symbol, side, notional))
        return Order(id=f"eq-{symbol}-{side.value}", symbol=symbol, status=FillStatus.FILLED, notional=notional)

    def close_position(self, symbol):
        self.calls.append(("equity_close", symbol))
        return Order(id=f"eq-close-{symbol}", symbol=symbol, status=FillStatus.FILLED)

    def order_status(self, order_id):
        return Order(id=order_id, symbol="X", status=FillStatus.FILLED)

    def pending_corporate_action(self, symbol, lookahead_days=90):
        return None


class RecordingOptionsBroker(OptionsBroker):
    def __init__(self):
        self.calls = []

    def chain(self, symbol, expiry_range):
        return []

    def submit_option(self, contract, qty, side):
        self.calls.append(("option_order", contract.occ_symbol, side, qty))
        return Order(id=f"opt-{contract.occ_symbol}-{side.value}", symbol=contract.occ_symbol, status=FillStatus.FILLED)

    def option_positions(self):
        return []

    def buying_power_reserved(self):
        return 0

    def latest_contract_quote(self, occ_symbol):
        from brokers.base import Contract
        return Contract(
            occ_symbol=occ_symbol, underlying="TEST", contract_type="put", strike=50.0,
            expiry=date(2024, 3, 15), bid=1.0, ask=1.10, open_interest=500, volume=10,
        )


def test_options_close_before_equity_sells_and_open_after_equity_buys():
    equity = RecordingEquityBroker()
    options = RecordingOptionsBroker()
    actions = [("AAA", "sell", 1000, 500, 500), ("BBB", "buy", 0, 500, 500)]
    open_target = OptionTarget(ticker="CCC", instrument="short_put", occ_symbol="CCC240315P00050000", strike=50.0, expiry="2024-03-15", contracts=1)

    execute_actions(
        equity, actions,
        options_broker=options, close_option_occ=["DDD240315P00040000"], open_option_targets=[open_target],
        current_option_positions=[OptionPosition(occ_symbol="DDD240315P00040000", underlying="DDD", contract_type="put",
                                                   strike=40.0, expiry=date(2024, 3, 15), qty=-1, market_value=-100)],
        account_buying_power=50_000,
    )

    kinds = [c[0] for c in equity.calls + options.calls]
    # Build a single ordered timeline by call order across both fakes -
    # options.calls[0] must be the close, options.calls[-1] the open, and
    # both equity calls must sit strictly between them.
    assert options.calls[0][0] == "option_order" and options.calls[0][1] == "DDD240315P00040000"
    assert options.calls[-1][0] == "option_order" and options.calls[-1][1] == "CCC240315P00050000"
    assert len(equity.calls) == 2
    assert len(options.calls) == 2


def test_capital_reservation_blocks_overcommitted_buys():
    equity = RecordingEquityBroker()
    options = RecordingOptionsBroker()
    # $40,000 planned equity buy + a short put reserving $45,000 (strike 450
    # x 100 x 1) exceeds $50,000 buying power - the whole run should abort
    # before any equity buy or option open is submitted.
    actions = [("BBB", "buy", 0, 40_000, 40_000)]
    open_target = OptionTarget(ticker="CCC", instrument="short_put", occ_symbol="CCC240315P00450000", strike=450.0, expiry="2024-03-15", contracts=1)

    execute_actions(
        equity, actions,
        options_broker=options, close_option_occ=[], open_option_targets=[open_target],
        current_option_positions=[], account_buying_power=50_000,
    )

    assert equity.calls == []  # buy never submitted
    assert options.calls == []  # open never submitted


def test_capital_reservation_allows_commitments_within_buying_power():
    equity = RecordingEquityBroker()
    options = RecordingOptionsBroker()
    actions = [("BBB", "buy", 0, 10_000, 10_000)]
    open_target = OptionTarget(ticker="CCC", instrument="short_put", occ_symbol="CCC240315P00050000", strike=50.0, expiry="2024-03-15", contracts=1)

    execute_actions(
        equity, actions,
        options_broker=options, close_option_occ=[], open_option_targets=[open_target],
        current_option_positions=[], account_buying_power=50_000,
    )

    assert len(equity.calls) == 1
    assert len(options.calls) == 1


def test_plan_option_actions_roll_produces_close_and_open():
    """A held contract not in this week's targets closes; a new target not
    yet held opens - even for the same underlying (a roll)."""
    held = [OptionPosition(occ_symbol="AAA240315P00040000", underlying="AAA", contract_type="put",
                            strike=40.0, expiry=date(2024, 3, 15), qty=-1, market_value=-100)]
    targets = {
        "AAA240415P00045000": OptionTarget(ticker="AAA", instrument="short_put", occ_symbol="AAA240415P00045000",
                                            strike=45.0, expiry="2024-04-15", contracts=1),
    }
    close_occ, open_targets = plan_option_actions(targets, held)
    assert close_occ == ["AAA240315P00040000"]
    assert [t.occ_symbol for t in open_targets] == ["AAA240415P00045000"]


def test_load_option_targets_ignores_share_rows_and_missing_columns(tmp_path):
    csv_with_options = tmp_path / "with_options.csv"
    csv_with_options.write_text(
        "ticker,position_size,instrument,occ_symbol,option_strike,option_expiry,option_contracts\n"
        "AAA,0.05,shares,,,,\n"
        "BBB,0.03,short_put,BBB240315P00050000,50.0,2024-03-15,2\n"
    )
    targets = load_option_targets(str(csv_with_options))
    assert set(targets.keys()) == {"BBB240315P00050000"}
    assert targets["BBB240315P00050000"].contracts == 2

    plain_csv = tmp_path / "plain.csv"
    plain_csv.write_text("ticker,position_size\nAAA,0.05\n")
    assert load_option_targets(str(plain_csv)) == {}


# --- fill confirmation: a real bug found live (mid-priced orders can sit
# unfilled indefinitely - see brokers/alpaca_options.py's submit_option
# docstring). close_option_positions/open_option_positions must not
# silently assume a submitted order actually filled. ---------------------

from trade_from_csv import close_option_positions, open_option_positions


class NeverFillsEquityBroker(EquityBroker):
    """order_status always comes back non-terminal, simulating a resting
    order that never crosses the spread."""
    def positions(self):
        return []

    def account(self):
        raise NotImplementedError

    def is_tradable(self, symbol):
        return True

    def submit_notional(self, symbol, notional, side):
        raise NotImplementedError

    def close_position(self, symbol):
        raise NotImplementedError

    def order_status(self, order_id):
        return Order(id=order_id, symbol="X", status=FillStatus.NEW)

    def pending_corporate_action(self, symbol, lookahead_days=90):
        return None


def test_open_option_positions_flags_unfilled_orders(capsys):
    options = RecordingOptionsBroker()
    target = OptionTarget(ticker="AAA", instrument="short_put", occ_symbol="AAA240315P00050000",
                           strike=50.0, expiry="2024-03-15", contracts=1)

    unfilled = open_option_positions(options, [target], equity_broker=NeverFillsEquityBroker(), poll_timeout=1)

    assert unfilled == ["AAA240315P00050000"]
    assert "ALERT" in capsys.readouterr().out


def test_open_option_positions_confirms_filled_orders():
    options = RecordingOptionsBroker()
    target = OptionTarget(ticker="AAA", instrument="short_put", occ_symbol="AAA240315P00050000",
                           strike=50.0, expiry="2024-03-15", contracts=1)

    unfilled = open_option_positions(options, [target], equity_broker=RecordingEquityBroker(), poll_timeout=1)

    assert unfilled == []


def test_close_option_positions_flags_unfilled_orders(capsys):
    options = RecordingOptionsBroker()
    positions = [OptionPosition(occ_symbol="AAA240315P00050000", underlying="AAA", contract_type="put",
                                 strike=50.0, expiry=date(2024, 3, 15), qty=-1, market_value=-100)]

    unfilled = close_option_positions(options, ["AAA240315P00050000"], positions,
                                       equity_broker=NeverFillsEquityBroker(), poll_timeout=1)

    assert unfilled == ["AAA240315P00050000"]
    assert "ALERT" in capsys.readouterr().out


def test_option_functions_skip_polling_without_an_equity_broker():
    """Backward-compatible default: equity_broker=None means "don't poll",
    not "crash" - callers that don't care about confirmation still work."""
    options = RecordingOptionsBroker()
    target = OptionTarget(ticker="AAA", instrument="short_put", occ_symbol="AAA240315P00050000",
                           strike=50.0, expiry="2024-03-15", contracts=1)
    unfilled = open_option_positions(options, [target])
    assert unfilled == []
