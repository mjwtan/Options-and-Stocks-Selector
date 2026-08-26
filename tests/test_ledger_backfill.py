"""track_performance.py's backfill_ledger() - fills in forward returns once
enough time has actually passed, using the historical price *at that
target date*, not whatever the price happens to be when the backfill runs.
"""

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from brokers.base import MarketData, Quote
from track_performance import backfill_ledger, price_on_or_after


class FakeHistoricalMarketData(MarketData):
    """prices: {ticker: {date: close}}"""
    def __init__(self, prices):
        self._prices = prices

    def daily_bars(self, symbols, start, end):
        rows = []
        for s in symbols:
            for d, price in self._prices.get(s, {}).items():
                if start.date() <= d <= end.date():
                    rows.append({"symbol": s, "timestamp": pd.Timestamp(d, tz=timezone.utc), "close": price, "volume": 1000})
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).set_index(["symbol", "timestamp"])

    def latest_quote(self, symbol):
        raise NotImplementedError


def _write_week(history_dir, run_date: date, rows):
    day_dir = history_dir / run_date.isoformat()
    day_dir.mkdir(parents=True)
    pd.DataFrame(rows).to_csv(day_dir / "target_positions.csv", index=False)


def test_price_on_or_after_finds_next_available_date():
    series = pd.Series({date(2024, 1, 2): 100.0, date(2024, 1, 5): 105.0})
    # 2024-01-03 has no bar - should land on 01-05, not 01-02.
    assert price_on_or_after(series, date(2024, 1, 3)) == 105.0


def test_price_on_or_after_none_when_too_recent():
    series = pd.Series({date(2024, 1, 2): 100.0})
    assert price_on_or_after(series, date(2024, 1, 10)) is None


def test_backfill_ledger_fills_1w_return_once_a_week_has_passed(tmp_path):
    run_date = date(2026, 8, 1)
    today = run_date + timedelta(days=8)  # comfortably past the 1-week mark

    _write_week(tmp_path, run_date, [
        {"ticker": "AAA", "position_size": 0.6, "entry_price": 100.0},
        {"ticker": "BBB", "position_size": 0.4, "entry_price": 50.0},
    ])
    ledger = pd.DataFrame([{
        "run_date": run_date.isoformat(), "actual_return_1w": None, "equal_weight_return_1w": None,
    }])
    for col in ["portfolio_value", "cash_pct", "sigma_p_intended", "sigma_p_realised_20d", "k_vol", "k_risk",
                "k_regime", "turnover_pct", "est_cost_bps", "n_shares", "n_calls", "n_puts", "n_skipped",
                "options_fallback_count", "skip_count_by_gate", "iv_crosscheck_warnings", "regime_crossings_ytd",
                "mc_elapsed_ms", "mc_paths", "cvar_ann"]:
        ledger[col] = None
    ledger.to_csv(tmp_path / "validation_ledger.csv", index=False)

    target_date = run_date + timedelta(days=7)
    market_data = FakeHistoricalMarketData({
        "AAA": {target_date: 110.0},  # +10%
        "BBB": {target_date: 45.0},   # -10%
        "SPY": {target_date: 400.0},
    })

    backfill_ledger(market_data, tmp_path, today=today)

    result = pd.read_csv(tmp_path / "validation_ledger.csv")
    assert result.loc[0, "actual_return_1w"] == pytest.approx(0.6 * 0.10 + 0.4 * -0.10)
    assert result.loc[0, "equal_weight_return_1w"] == pytest.approx((0.10 + -0.10) / 2)


def test_backfill_ledger_does_not_touch_rows_too_recent(tmp_path):
    run_date = date(2026, 8, 1)
    today = run_date + timedelta(days=2)  # not yet a week

    _write_week(tmp_path, run_date, [{"ticker": "AAA", "position_size": 1.0, "entry_price": 100.0}])
    ledger = pd.DataFrame([{"run_date": run_date.isoformat(), "actual_return_1w": None, "equal_weight_return_1w": None}])
    for col in ["portfolio_value", "cash_pct"]:
        ledger[col] = None
    ledger.to_csv(tmp_path / "validation_ledger.csv", index=False)

    market_data = FakeHistoricalMarketData({"AAA": {run_date + timedelta(days=7): 999.0}})
    backfill_ledger(market_data, tmp_path, today=today)

    result = pd.read_csv(tmp_path / "validation_ledger.csv")
    assert pd.isna(result.loc[0, "actual_return_1w"])


def test_backfill_ledger_per_name_fills_1w_and_4w(tmp_path):
    run_date = date(2026, 8, 1)
    today = run_date + timedelta(days=29)

    _write_week(tmp_path, run_date, [{"ticker": "AAA", "position_size": 1.0, "entry_price": 100.0}])
    per_name = pd.DataFrame([{
        "run_date": run_date.isoformat(), "ticker": "AAA", "rank": 1, "weight": 1.0, "instrument": "shares",
        "forward_return_1w": None, "forward_return_4w": None,
    }])
    per_name.to_csv(tmp_path / "validation_ledger_per_name.csv", index=False)

    market_data = FakeHistoricalMarketData({
        "AAA": {
            run_date + timedelta(days=7): 105.0,
            run_date + timedelta(days=28): 120.0,
        },
    })
    backfill_ledger(market_data, tmp_path, today=today)

    result = pd.read_csv(tmp_path / "validation_ledger_per_name.csv")
    assert result.loc[0, "forward_return_1w"] == pytest.approx(0.05)
    assert result.loc[0, "forward_return_4w"] == pytest.approx(0.20)


def test_backfill_ledger_already_filled_rows_are_left_alone(tmp_path):
    run_date = date(2026, 8, 1)
    today = run_date + timedelta(days=8)

    _write_week(tmp_path, run_date, [{"ticker": "AAA", "position_size": 1.0, "entry_price": 100.0}])
    ledger = pd.DataFrame([{"run_date": run_date.isoformat(), "actual_return_1w": 0.5, "equal_weight_return_1w": 0.5}])
    ledger.to_csv(tmp_path / "validation_ledger.csv", index=False)

    market_data = FakeHistoricalMarketData({"AAA": {run_date + timedelta(days=7): 200.0}})  # would give +100% if recomputed
    backfill_ledger(market_data, tmp_path, today=today)

    result = pd.read_csv(tmp_path / "validation_ledger.csv")
    assert result.loc[0, "actual_return_1w"] == 0.5  # untouched, not overwritten with the +100% figure
