from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.recommendation.validation import validate_recommendation_system
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate recommendation outputs.")
    add_db_path_arg(parser)
    parser.add_argument("--config", default="config/recommendation.example.yaml", help="Accepted for CLI symmetry.")
    parser.add_argument("--date", default=None, help="Accepted for CLI symmetry.")
    parser.add_argument("--batch-id", default=None, help="Accepted for CLI symmetry.")
    parser.add_argument("--dry-run", action="store_true", help="Print validation rows without writing analysis_validation.")
    parser.add_argument("--force", action="store_true", help="Accepted for CLI symmetry.")
    args = parser.parse_args()
    rows = validate_recommendation_system(args.db_path, args.dry_run)
    for row in rows:
        print(f"{row['check_name']}: {row['status']} {row['message']}")


if __name__ == "__main__":
    main()
