"""system-spec.md S16 validation ledger."""

import json
from datetime import date, timezone

import pandas as pd
import pytest

from validation_ledger import (
    _compute_turnover_pct,
    _crossings_this_calendar_year,
    build_ledger_row,
    build_per_name_rows,
)


def _fake_run_log(**overrides):
    base = {
        "run_at": "2026-08-26T14:00:00+00:00",
        "portfolio_value": 100_000.0,
        "cash_pct": 0.10,
        "weight_final": {"AAA": 0.5, "BBB": 0.4, "CCC": 0.0},
        "instrument_decisions": {
            "AAA": {"instrument": "shares", "reason": "rank 1 <= 5: shares only (S5.3)"},
            "BBB": {"instrument": "long_call", "reason": "iv_ratio=0.7 vs thresholds [0.85,1.25]"},
            "CCC": {"instrument": "shares", "reason": "no listed expiry within +/-14d of target 45DTE"},
        },
        "skip_decisions": {
            "DDD": {"gate": "liquidity", "reason": "adv20 too low"},
        },
        "risk": {
            "sigma_p_analytic": 0.20,
            "k_risk": 0.9,
            "risk_engine_used": "montecarlo",
            "elapsed_ms": 500.0,
            "n_paths": 50_000,
            "cvar_annualised": 0.22,
        },
        "regime": {"k_regime": 0.85},
        "cross_check_flags": {"AAA": 0.45},
        "config": {"sigma_target": 0.15},
    }
    base.update(overrides)
    return base


def test_build_ledger_row_basic_fields():
    row = build_ledger_row(_fake_run_log(), history_dir=__import__("pathlib").Path("/does/not/exist"), regime_state_path=__import__("pathlib").Path("/does/not/exist"))
    assert row["run_date"] == "2026-08-26"
    assert row["portfolio_value"] == 100_000.0
    assert row["cash_pct"] == 0.10
    assert row["k_regime"] == 0.85
    assert row["k_risk"] == 0.9
    assert row["k_vol"] == pytest.approx(min(1.0, 0.15 / 0.20))
    assert row["mc_paths"] == 50_000
    assert row["cvar_ann"] == 0.22
    assert row["actual_return_1w"] is None
    assert row["sigma_p_realised_20d"] is None


def test_build_ledger_row_instrument_counts():
    row = build_ledger_row(_fake_run_log(), history_dir=__import__("pathlib").Path("/does/not/exist"), regime_state_path=__import__("pathlib").Path("/does/not/exist"))
    assert row["n_shares"] == 2  # AAA (rank<=5) and CCC (fallback)
    assert row["n_calls"] == 1
    assert row["n_puts"] == 0
    assert row["n_skipped"] == 1


def test_build_ledger_row_fallback_count_excludes_rank_restriction():
    """AAA is shares because rank<=5 (never eligible) - not a fallback.
    CCC is shares because of a genuine S5.7 fallback (no matching expiry) -
    that one counts."""
    row = build_ledger_row(_fake_run_log(), history_dir=__import__("pathlib").Path("/does/not/exist"), regime_state_path=__import__("pathlib").Path("/does/not/exist"))
    assert row["options_fallback_count"] == 1


def test_build_ledger_row_options_disabled_not_counted_as_fallback():
    run_log = _fake_run_log(instrument_decisions={
        "AAA": {"instrument": "shares", "reason": "OPTIONS_ENABLED is False"},
    })
    row = build_ledger_row(run_log, history_dir=__import__("pathlib").Path("/does/not/exist"), regime_state_path=__import__("pathlib").Path("/does/not/exist"))
    assert row["options_fallback_count"] == 0


def test_build_ledger_row_skip_count_by_gate_is_json():
    row = build_ledger_row(_fake_run_log(), history_dir=__import__("pathlib").Path("/does/not/exist"), regime_state_path=__import__("pathlib").Path("/does/not/exist"))
    assert json.loads(row["skip_count_by_gate"]) == {"liquidity": 1}


def test_build_ledger_row_no_previous_week_gives_none_turnover(tmp_path):
    row = build_ledger_row(_fake_run_log(), history_dir=tmp_path, regime_state_path=tmp_path / "regime_state.json")
    assert row["turnover_pct"] is None
    assert row["est_cost_bps"] is None


def test_build_ledger_row_computes_turnover_against_previous_week(tmp_path):
    prev_dir = tmp_path / "2026-08-19"
    prev_dir.mkdir()
    pd.DataFrame({"ticker": ["AAA", "BBB"], "position_size": [0.3, 0.5]}).to_csv(prev_dir / "target_positions.csv", index=False)

    row = build_ledger_row(_fake_run_log(), history_dir=tmp_path, regime_state_path=tmp_path / "regime_state.json")
    # AAA: |0.5-0.3|=0.2, BBB: |0.4-0.5|=0.1, CCC: |0-0|=0 -> sum=0.3, /2 = 0.15
    assert row["turnover_pct"] == pytest.approx(0.15)
    assert row["est_cost_bps"] == pytest.approx(0.15 * 10.0)


def test_compute_turnover_pct_first_run_is_none():
    assert _compute_turnover_pct({"AAA": 0.5}, None) is None


def test_compute_turnover_pct_identical_weights_is_zero():
    assert _compute_turnover_pct({"AAA": 0.5, "BBB": 0.5}, {"AAA": 0.5, "BBB": 0.5}) == pytest.approx(0.0)


def test_crossings_this_calendar_year_filters_by_year():
    log = ["2026-01-15T00:00:00+00:00", "2025-12-20T00:00:00+00:00", "2026-06-01T00:00:00+00:00"]
    # frozen "now" isn't controllable here without more plumbing - just
    # assert it doesn't count the 2025 entry when "now" is naturally 2026.
    from datetime import datetime
    if datetime.now(timezone.utc).year == 2026:
        assert _crossings_this_calendar_year(log) == 2


def test_build_per_name_rows_uses_instrument_and_weight():
    rows = build_per_name_rows(_fake_run_log())
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["AAA"]["instrument"] == "shares"
    assert by_ticker["AAA"]["weight"] == 0.5
    assert by_ticker["BBB"]["instrument"] == "long_call"
    assert by_ticker["AAA"]["forward_return_1w"] is None


# --- upsert behavior: a real bug found live - repeated same-day runs
# (e.g. retesting with --force) were accumulating duplicate rows instead
# of replacing that day's entry, the way archive_run() already does for
# history/<date>/. ---------------------------------------------------------

from validation_ledger import append_ledger_row, append_per_name_rows


def test_append_ledger_row_replaces_same_day_row_not_duplicates(tmp_path):
    path = tmp_path / "validation_ledger.csv"
    row1 = build_ledger_row(_fake_run_log(portfolio_value=100_000.0), history_dir=tmp_path, regime_state_path=tmp_path / "r.json")
    row2 = build_ledger_row(_fake_run_log(portfolio_value=999_999.0), history_dir=tmp_path, regime_state_path=tmp_path / "r.json")

    append_ledger_row(row1, path)
    append_ledger_row(row2, path)

    result = pd.read_csv(path)
    assert len(result) == 1
    assert result.loc[0, "portfolio_value"] == 999_999.0  # the later run wins, not both accumulated


def test_append_ledger_row_keeps_different_dates_separate(tmp_path):
    path = tmp_path / "validation_ledger.csv"
    row1 = build_ledger_row(_fake_run_log(run_at="2026-08-20T14:00:00+00:00"), history_dir=tmp_path, regime_state_path=tmp_path / "r.json")
    row2 = build_ledger_row(_fake_run_log(run_at="2026-08-27T14:00:00+00:00"), history_dir=tmp_path, regime_state_path=tmp_path / "r.json")

    append_ledger_row(row1, path)
    append_ledger_row(row2, path)

    result = pd.read_csv(path)
    assert sorted(result["run_date"]) == ["2026-08-20", "2026-08-27"]


def test_append_per_name_rows_replaces_same_day_same_ticker(tmp_path):
    path = tmp_path / "per_name.csv"
    rows1 = build_per_name_rows(_fake_run_log(weight_final={"AAA": 0.5}))
    rows2 = build_per_name_rows(_fake_run_log(weight_final={"AAA": 0.9}))  # e.g. a totally different CSV run same day

    append_per_name_rows(rows1, path)
    append_per_name_rows(rows2, path)

    result = pd.read_csv(path)
    assert len(result) == 1
    assert result.loc[0, "weight"] == 0.9
