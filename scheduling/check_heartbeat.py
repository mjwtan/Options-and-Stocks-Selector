"""system-spec.md S15.5 dead-man's-switch: alerts if any of the three
scheduled jobs hasn't recorded a successful run in --max-age-days. This is
the one script most responsible for catching "the laptop was closed for a
week and nothing happened" - see heartbeat.py's own docstring on why it's
kept dependency-light.

Usage:
    python scheduling/check_heartbeat.py
    python scheduling/check_heartbeat.py --max-age-days 3
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from heartbeat import check_heartbeats

JOBS = ["weekly_sizing", "daily_monitor", "report"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-age-days", type=int, default=8)
    args = parser.parse_args()

    stale = check_heartbeats(JOBS, max_age_days=args.max_age_days)
    if not stale:
        print(f"All {len(JOBS)} jobs have a heartbeat within the last {args.max_age_days} day(s). OK.")
        return

    print(f"STALE JOBS DETECTED (no successful run within {args.max_age_days} day(s)):")
    for name, reason in stale.items():
        print(f"  {name}: {reason}")
    sys.exit(1)


if __name__ == "__main__":
    main()
