from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.recommendation.selector import generate_recommendations
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily stock recommendation watchlist.")
    add_db_path_arg(parser)
    parser.add_argument("--config", default="config/recommendation.example.yaml")
    parser.add_argument("--date", default=None)
    parser.add_argument("--batch-id", default=None, help="Accepted for CLI symmetry; generation keys by date/version.")
    parser.add_argument("--selector", default=None, help="raw_top_n, confirmed_top_n, or low_turnover_top_n.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    for line in generate_recommendations(args.db_path, args.config, args.date, args.dry_run, args.force, args.selector):
        print(line)


if __name__ == "__main__":
    main()
