from datetime import date, timedelta

from brokers.base import Contract, OptionsBroker
from options.chain import fetch_filtered_chain

VALUATION_DATE = date(2024, 1, 2)


class FakeOptionsBroker(OptionsBroker):
    def __init__(self, contracts):
        self._contracts = contracts

    def chain(self, symbol, expiry_range):
        return self._contracts

    def submit_option(self, contract, qty, side):
        raise NotImplementedError

    def option_positions(self):
        return []

    def buying_power_reserved(self):
        return 0


def _contract(**overrides):
    defaults = dict(
        occ_symbol="TEST240301C00100000",
        underlying="TEST",
        contract_type="call",
        strike=100.0,
        expiry=VALUATION_DATE + timedelta(days=45),
        bid=1.0,
        ask=1.10,
        open_interest=500,
        volume=10,
    )
    defaults.update(overrides)
    return Contract(**defaults)


def test_survivor_passes_all_filters():
    result = fetch_filtered_chain(FakeOptionsBroker([_contract()] * 5), "TEST", VALUATION_DATE, min_surviving_contracts=1)
    assert result.total_surviving == 5
    assert result.skip_reason is None


def test_wide_spread_is_discarded():
    wide = _contract(bid=1.0, ask=1.40)  # (1.40-1.00)/1.20 = 33% > 15%
    result = fetch_filtered_chain(FakeOptionsBroker([wide]), "TEST", VALUATION_DATE, min_surviving_contracts=1)
    assert result.total_surviving == 0
    assert result.skip_reason is not None


def test_zero_bid_or_ask_is_discarded():
    result = fetch_filtered_chain(FakeOptionsBroker([_contract(bid=0.0)]), "TEST", VALUATION_DATE, min_surviving_contracts=1)
    assert result.total_surviving == 0


def test_low_open_interest_is_discarded():
    result = fetch_filtered_chain(FakeOptionsBroker([_contract(open_interest=50)]), "TEST", VALUATION_DATE, min_surviving_contracts=1)
    assert result.total_surviving == 0


def test_expiry_outside_dte_window_is_discarded():
    too_soon = _contract(expiry=VALUATION_DATE + timedelta(days=10))
    too_far = _contract(expiry=VALUATION_DATE + timedelta(days=120))
    result = fetch_filtered_chain(FakeOptionsBroker([too_soon, too_far]), "TEST", VALUATION_DATE, min_surviving_contracts=1)
    assert result.total_surviving == 0


def test_skip_reason_set_when_too_few_survive():
    result = fetch_filtered_chain(FakeOptionsBroker([_contract()]), "TEST", VALUATION_DATE, min_surviving_contracts=4)
    assert result.total_surviving == 1
    assert "need >= 4" in result.skip_reason


def test_contracts_grouped_by_expiry():
    e1 = VALUATION_DATE + timedelta(days=30)
    e2 = VALUATION_DATE + timedelta(days=60)
    result = fetch_filtered_chain(
        FakeOptionsBroker([_contract(expiry=e1), _contract(expiry=e1), _contract(expiry=e2)]),
        "TEST", VALUATION_DATE, min_surviving_contracts=1,
    )
    assert result.expiries == [e1, e2]
    assert len(result.contracts_by_expiry[e1]) == 2
    assert len(result.contracts_by_expiry[e2]) == 1
