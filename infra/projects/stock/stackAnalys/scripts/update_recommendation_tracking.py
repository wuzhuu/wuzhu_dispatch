from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.recommendation.tracker import update_tracking
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Update 30-trading-day recommendation tracking.")
    add_db_path_arg(parser)
    parser.add_argument("--config", default="config/recommendation.example.yaml")
    parser.add_argument("--date", default=None)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    for line in update_tracking(args.db_path, args.config, args.date, args.batch_id, args.dry_run, args.force):
        print(line)


if __name__ == "__main__":
    main()
