"""check_staleness()'s age check - real bug found live: a filesystem mtime
is meaningless in CI, where every scheduled run does a fresh git checkout
that stamps every file's mtime to "now" regardless of the file's actual
content age. The staleness check never once fired in months of scheduled
runs because of this. Fixed to prefer git's own commit history, which
survives a fresh checkout intact, falling back to mtime only outside git.
"""

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from position_sizing import Config, _git_last_commit_date, check_staleness


def _run_git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


def _init_repo(tmp_path):
    _run_git(["init"], tmp_path)
    _run_git(["config", "user.email", "test@example.com"], tmp_path)
    _run_git(["config", "user.name", "Test"], tmp_path)


def _commit_file(tmp_path, filename, content, commit_date: datetime):
    path = tmp_path / filename
    path.write_text(content)
    _run_git(["add", filename], tmp_path)
    date_str = commit_date.isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": date_str, "GIT_COMMITTER_DATE": date_str}
    subprocess.run(
        ["git", "commit", "-m", "test commit"],
        cwd=tmp_path, check=True, capture_output=True, env=env,
    )
    return path


def test_git_last_commit_date_returns_commit_time_for_tracked_file(tmp_path):
    commit_date = datetime.now(timezone.utc) - timedelta(days=10)
    _init_repo(tmp_path)
    path = _commit_file(tmp_path, "top20.csv", "a,b\n1,2\n", commit_date)

    result = _git_last_commit_date(path)

    assert result is not None
    assert abs((result - commit_date).total_seconds()) < 2


def test_git_last_commit_date_none_for_untracked_file_in_a_repo(tmp_path):
    _init_repo(tmp_path)
    path = tmp_path / "top20.csv"
    path.write_text("a,b\n1,2\n")  # never `git add`/committed

    assert _git_last_commit_date(path) is None


def test_git_last_commit_date_none_outside_a_git_repo(tmp_path):
    path = tmp_path / "top20.csv"
    path.write_text("a,b\n1,2\n")  # tmp_path is not a git repo at all

    assert _git_last_commit_date(path) is None


def test_check_staleness_uses_git_commit_date_when_available(tmp_path):
    stale_date = datetime.now(timezone.utc) - timedelta(days=10)
    _init_repo(tmp_path)
    path = _commit_file(tmp_path, "top20.csv", "a,b\n1,2\n", stale_date)
    # Give the file a fresh mtime, simulating what a CI checkout does -
    # the git-history date should still be used, not this.
    fresh = datetime.now().timestamp()
    os.utime(path, (fresh, fresh))

    errors, _file_hash, mtime = check_staleness(path, Config())

    assert any("per git commit" in e for e in errors)
    assert abs((mtime - stale_date).total_seconds()) < 2


def test_check_staleness_falls_back_to_mtime_outside_git(tmp_path):
    path = tmp_path / "top20.csv"
    path.write_text("a,b\n1,2\n")
    stale = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    os.utime(path, (stale, stale))

    errors, _file_hash, _mtime = check_staleness(path, Config())

    assert any("per filesystem mtime" in e for e in errors)


def test_check_staleness_fresh_git_commit_no_age_error(tmp_path):
    _init_repo(tmp_path)
    path = _commit_file(tmp_path, "top20.csv", "a,b\n1,2\n", datetime.now(timezone.utc))

    errors, _file_hash, _mtime = check_staleness(path, Config())

    assert not any("day(s) ago" in e for e in errors)
