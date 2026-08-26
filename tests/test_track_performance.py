"""Performance tracking - system-spec.md S14's equal-weight comparison.
Pure-function tests; the live run (`python track_performance.py`) already
confirmed the network/orchestration wiring against a real account.
"""

from datetime import date

import pandas as pd
import pytest

from track_performance import (
    benchmark_return,
    compute_week_performance,
    find_archived_weeks,
    load_held_positions,
)


def _held(rows):
    return pd.DataFrame(rows, columns=["ticker", "position_size", "entry_price"])


def test_compute_week_performance_weights_by_position_size():
    held = _held([
        ("AAA", 0.6, 100.0),  # +10%
        ("BBB", 0.4, 50.0),   # -10%
    ])
    prices = {"AAA": 110.0, "BBB": 45.0}
    actual, equal_weight, n = compute_week_performance(held, prices)

    assert n == 2
    assert actual == pytest.approx(0.6 * 0.10 + 0.4 * -0.10)
    assert equal_weight == pytest.approx((0.10 + -0.10) / 2)


def test_compute_week_performance_renormalises_weights_excluding_cash():
    # position_size sums to 0.5 (rest is cash) - the equity-sleeve return
    # should be computed on the held names alone, not diluted by cash.
    held = _held([("AAA", 0.5, 100.0)])
    prices = {"AAA": 120.0}
    actual, equal_weight, n = compute_week_performance(held, prices)
    assert actual == pytest.approx(0.20)
    assert equal_weight == pytest.approx(0.20)


def test_compute_week_performance_skips_names_with_no_current_price():
    held = _held([("AAA", 0.5, 100.0), ("BBB", 0.5, 100.0)])
    actual, equal_weight, n = compute_week_performance(held, {"AAA": 110.0})  # BBB has no quote
    assert n == 1
    assert actual == pytest.approx(0.10)


def test_compute_week_performance_no_prices_returns_none():
    held = _held([("AAA", 0.5, 100.0)])
    actual, equal_weight, n = compute_week_performance(held, {})
    assert actual is None
    assert equal_weight is None
    assert n == 0


def test_compute_week_performance_ignores_bad_entry_price():
    held = _held([("AAA", 0.5, 0.0), ("BBB", 0.5, 100.0)])  # AAA entry_price is 0/invalid
    actual, _equal_weight, n = compute_week_performance(held, {"AAA": 50.0, "BBB": 110.0})
    assert n == 1  # only BBB counted


def test_benchmark_return_uses_last_available_price_on_or_before_each_date():
    series = pd.Series(
        {date(2026, 8, 20): 100.0, date(2026, 8, 24): 105.0, date(2026, 8, 26): 110.0},
    )
    r = benchmark_return(series, entry_date=date(2026, 8, 21), today=date(2026, 8, 26))
    # entry_date 08-21 has no exact bar - falls back to the 08-20 close (100.0)
    assert r == pytest.approx((110.0 / 100.0) - 1.0)


def test_benchmark_return_empty_series_returns_none():
    assert benchmark_return(pd.Series(dtype=float), date(2026, 8, 1), date(2026, 8, 26)) is None


def test_find_archived_weeks_picks_output_csv_by_elimination(tmp_path):
    day_dir = tmp_path / "2026-08-19"
    day_dir.mkdir()
    (day_dir / "input_top20.csv").write_text("x")
    (day_dir / "target_positions.csv").write_text("ticker,position_size,entry_price\nAAA,0.5,100.0\n")
    (day_dir / "run_20260819T120000Z.json").write_text("{}")

    weeks = find_archived_weeks(tmp_path)
    assert len(weeks) == 1
    day, output_path = weeks[0]
    assert day == date(2026, 8, 19)
    assert output_path.name == "target_positions.csv"


def test_find_archived_weeks_ignores_non_date_directories(tmp_path):
    (tmp_path / "not_a_date").mkdir()
    (tmp_path / "not_a_date" / "target_positions.csv").write_text("ticker,position_size,entry_price\n")
    assert find_archived_weeks(tmp_path) == []


def test_load_held_positions_filters_zero_and_option_only_rows(tmp_path):
    csv_path = tmp_path / "target_positions.csv"
    csv_path.write_text(
        "ticker,ranking,position_size,entry_price\n"
        "AAA,1,0.05,100.0\n"
        "BBB,2,0.0,50.0\n"  # zeroed - routed to an option, per zero_out_options_weight
    )
    held = load_held_positions(csv_path)
    assert list(held["ticker"]) == ["AAA"]
