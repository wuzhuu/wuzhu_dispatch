from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.recommendation.history import backfill_recommendation_history
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill historical recommendation batches and 30-day tracking.")
    add_db_path_arg(parser)
    parser.add_argument("--config", default="config/recommendation.example.yaml")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--selectors", default=None, help="Comma-separated selectors; defaults to config recommendation.selector_names.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    selectors = [x.strip() for x in args.selectors.split(",") if x.strip()] if args.selectors else None
    for line in backfill_recommendation_history(args.db_path, args.config, args.start, args.end, args.force, args.dry_run, selectors):
        print(line)


if __name__ == "__main__":
    main()
