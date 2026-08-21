"""Regression test for a real bug found during live testing: a name routed
to an option by the S5 decision engine kept its full equity `position_size`
too, so trade_from_csv.py would buy the shares AND open the option -
double exposure, not the mutually-exclusive S5.0 outcomes the spec
describes. zero_out_options_weight() is the fix.
"""

import pandas as pd

from options.decision import InstrumentDecision
from position_sizing import zero_out_options_weight


def test_option_instrument_zeroes_equity_weight():
    w_final = pd.Series({"AAA": 0.05, "BBB": 0.08, "CCC": 0.03})
    decisions = {
        "AAA": InstrumentDecision(ticker="AAA", instrument="shares", reason="rank <= 5"),
        "BBB": InstrumentDecision(ticker="BBB", instrument="long_call", reason="cheap", contracts=2),
        "CCC": InstrumentDecision(ticker="CCC", instrument="short_put", reason="rich", contracts=1),
    }

    result = zero_out_options_weight(w_final, decisions)

    assert result["AAA"] == 0.05  # shares - untouched
    assert result["BBB"] == 0.0   # long_call - zeroed, expressed via the option instead
    assert result["CCC"] == 0.0   # short_put - zeroed, expressed via the option instead


def test_does_not_mutate_input():
    w_final = pd.Series({"AAA": 0.05})
    decisions = {"AAA": InstrumentDecision(ticker="AAA", instrument="long_call", reason="cheap")}
    zero_out_options_weight(w_final, decisions)
    assert w_final["AAA"] == 0.05  # original untouched


def test_missing_ticker_in_decisions_is_left_alone():
    """A dropped-by-floor name never reaches the decision engine at all -
    absent from instrument_decisions entirely, not present with instrument
    'shares'. Must not error, and there's no weight for it anyway."""
    w_final = pd.Series({"AAA": 0.05})
    result = zero_out_options_weight(w_final, {})
    assert result["AAA"] == 0.05
