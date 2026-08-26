"""Heartbeat tracking - system-spec.md S15.5: "every successful run writes
a timestamp. A separate check... alerts if no successful run has occurred
in 8 days. Silent failure is the dominant operational risk in scheduled
systems."

Deliberately dependency-light (stdlib only) so the checker itself is
almost incapable of failing - a heartbeat system that can silently break
is worse than none. Each job records its own name, so a stalled daily job
doesn't get masked by a healthy weekly job or vice versa.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path(__file__).parent / "state"
HEARTBEAT_PATH = STATE_DIR / "heartbeat.json"


def record_heartbeat(job_name: str) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    data = {}
    if HEARTBEAT_PATH.exists():
        try:
            data = json.loads(HEARTBEAT_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    data[job_name] = datetime.now(timezone.utc).isoformat()
    HEARTBEAT_PATH.write_text(json.dumps(data, indent=2))


def check_heartbeats(job_names: list[str], max_age_days: int = 8) -> dict:
    """Returns {job_name: reason} for every job in job_names that is stale
    (no recorded heartbeat, or older than max_age_days). Jobs not passed in
    job_names are not checked - a job that was never scheduled shouldn't
    generate a false "stale" alert."""
    data = {}
    if HEARTBEAT_PATH.exists():
        try:
            data = json.loads(HEARTBEAT_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}

    now = datetime.now(timezone.utc)
    stale = {}
    for name in job_names:
        if name not in data:
            stale[name] = "no heartbeat ever recorded"
            continue
        try:
            last = datetime.fromisoformat(data[name])
        except ValueError:
            stale[name] = f"unparseable timestamp: {data[name]!r}"
            continue
        age = now - last
        if age > timedelta(days=max_age_days):
            stale[name] = f"last success {last.isoformat()} ({age.days} day(s) ago, > {max_age_days} day limit)"
    return stale
