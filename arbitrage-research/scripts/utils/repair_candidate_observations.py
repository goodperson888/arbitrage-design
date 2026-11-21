#!/usr/bin/env python3
"""Preview/recover complete rows from interrupted candidate observation gzip."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.live.candidates.observation_repair import repair_observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "data/live_monitor/candidates/v1")
    parser.add_argument("--apply", action="store_true", help="Back up originals and publish verified recovered gzip")
    args = parser.parse_args()
    try:
        results = repair_observations(args.directory, apply=args.apply)
    except BlockingIOError:
        parser.exit(1, "候选账本正在使用，请先执行 ./arb-monitor stop paper 后再修复。\n")
    print(json.dumps({"apply": args.apply, "partitions": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
