from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.recommendation.stability import evaluate_recommendation_stability
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate recommendation TopN stability and strategy comparison.")
    add_db_path_arg(parser)
    parser.add_argument("--config", default="config/recommendation.example.yaml")
    parser.add_argument("--date", default=None)
    parser.add_argument("--batch-id", default=None, help="Accepted for CLI symmetry.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Accepted for CLI symmetry.")
    args = parser.parse_args()
    for line in evaluate_recommendation_stability(args.db_path, args.config, args.date, args.dry_run):
        print(line)


if __name__ == "__main__":
    main()
