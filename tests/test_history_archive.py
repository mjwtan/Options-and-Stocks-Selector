"""Weekly history archive - not part of any mdinstructions spec, added so
there's a dated, browsable record of each week's input CSV / output CSV /
run log to build later analytics (hit rate by rank, equal-weight
comparison, etc.) on top of.
"""

import json
from datetime import date

from position_sizing import archive_run


def test_archive_run_creates_dated_folder_with_all_three_files(tmp_path):
    csv_path = tmp_path / "top20.csv"
    csv_path.write_text("ticker,ranking\nAAA,1\n")
    output_path = tmp_path / "target_positions.csv"
    output_path.write_text("ticker,position_size\nAAA,0.05\n")
    log_path = tmp_path / "run_20260826T120000Z.json"
    log_path.write_text(json.dumps({"hello": "world"}))

    history_root = tmp_path / "history"
    day_dir = archive_run(csv_path, output_path, log_path, history_root, run_date=date(2026, 8, 26))

    assert day_dir == history_root / "2026-08-26"
    assert (day_dir / "input_top20.csv").read_text() == csv_path.read_text()
    assert (day_dir / "target_positions.csv").read_text() == output_path.read_text()
    assert json.loads((day_dir / "run_20260826T120000Z.json").read_text()) == {"hello": "world"}


def test_archive_run_same_day_overwrites_not_duplicates(tmp_path):
    csv_path = tmp_path / "top20.csv"
    output_path = tmp_path / "target_positions.csv"
    log_path = tmp_path / "run_1.json"
    history_root = tmp_path / "history"

    csv_path.write_text("v1")
    output_path.write_text("v1")
    log_path.write_text("{}")
    archive_run(csv_path, output_path, log_path, history_root, run_date=date(2026, 8, 26))

    csv_path.write_text("v2")
    output_path.write_text("v2")
    day_dir = archive_run(csv_path, output_path, log_path, history_root, run_date=date(2026, 8, 26))

    assert (day_dir / "input_top20.csv").read_text() == "v2"
    assert (day_dir / "target_positions.csv").read_text() == "v2"
    assert len(list(history_root.iterdir())) == 1  # one dated folder, not two


def test_archive_run_different_days_get_separate_folders(tmp_path):
    csv_path = tmp_path / "top20.csv"
    output_path = tmp_path / "target_positions.csv"
    log_path = tmp_path / "run_1.json"
    history_root = tmp_path / "history"
    csv_path.write_text("x")
    output_path.write_text("x")
    log_path.write_text("{}")

    archive_run(csv_path, output_path, log_path, history_root, run_date=date(2026, 8, 24))
    archive_run(csv_path, output_path, log_path, history_root, run_date=date(2026, 8, 26))

    assert sorted(p.name for p in history_root.iterdir()) == ["2026-08-24", "2026-08-26"]
