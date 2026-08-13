from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.recommendation.evaluator import finalize_results
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Finalize recommendation item and batch results.")
    add_db_path_arg(parser)
    parser.add_argument("--config", default="config/recommendation.example.yaml")
    parser.add_argument("--date", default=None, help="Accepted for CLI symmetry.")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--recompute-costs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    for line in finalize_results(args.db_path, args.config, args.batch_id, args.dry_run, args.recompute_costs, args.start, args.end):
        print(line)


if __name__ == "__main__":
    main()
