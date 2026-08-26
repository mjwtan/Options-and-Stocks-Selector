"""system-spec.md S15.5 dead-man's-switch."""

import json
from datetime import datetime, timedelta, timezone

import heartbeat as hb


def test_record_and_check_fresh_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hb, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")

    hb.record_heartbeat("weekly_sizing")
    stale = hb.check_heartbeats(["weekly_sizing"], max_age_days=8)
    assert stale == {}


def test_never_recorded_job_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hb, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")

    stale = hb.check_heartbeats(["daily_monitor"], max_age_days=8)
    assert "daily_monitor" in stale
    assert "no heartbeat ever recorded" in stale["daily_monitor"]


def test_old_heartbeat_is_stale(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(hb, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hb, "HEARTBEAT_PATH", path)

    old = datetime.now(timezone.utc) - timedelta(days=10)
    path.write_text(json.dumps({"report": old.isoformat()}))

    stale = hb.check_heartbeats(["report"], max_age_days=8)
    assert "report" in stale
    assert "10 day" in stale["report"]


def test_recent_heartbeat_within_limit_is_not_stale(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    monkeypatch.setattr(hb, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hb, "HEARTBEAT_PATH", path)

    recent = datetime.now(timezone.utc) - timedelta(days=3)
    path.write_text(json.dumps({"report": recent.isoformat()}))

    stale = hb.check_heartbeats(["report"], max_age_days=8)
    assert stale == {}


def test_only_checks_requested_jobs(tmp_path, monkeypatch):
    """A job that isn't in job_names shouldn't generate a false alert -
    e.g. options-disabled setups never populate an options-specific job."""
    monkeypatch.setattr(hb, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hb, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")

    hb.record_heartbeat("weekly_sizing")
    stale = hb.check_heartbeats(["weekly_sizing"], max_age_days=8)
    assert stale == {}  # "daily_monitor" was never asked about, so it's not flagged


def test_corrupt_heartbeat_file_treated_as_empty(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    path.write_text("not valid json{{{")
    monkeypatch.setattr(hb, "STATE_DIR", tmp_path)
    monkeypatch.setattr(hb, "HEARTBEAT_PATH", path)

    stale = hb.check_heartbeats(["weekly_sizing"], max_age_days=8)
    assert "weekly_sizing" in stale  # falls back to "never recorded", doesn't crash
