from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import add_db_path_arg
from src.visualization.recommendation_charts import build_recommendation_charts


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate recommendation evaluation charts.")
    add_db_path_arg(parser)
    parser.add_argument("--date", default=None)
    parser.add_argument("--config", default="config/recommendation.example.yaml", help="Accepted for CLI symmetry.")
    parser.add_argument("--batch-id", default=None, help="Accepted for CLI symmetry.")
    parser.add_argument("--dry-run", action="store_true", help="Accepted for CLI symmetry; chart generation writes PNG/metadata.")
    parser.add_argument("--force", action="store_true", help="Accepted for CLI symmetry.")
    args = parser.parse_args()
    result = build_recommendation_charts(args.db_path, args.date)
    print(f"snapshots: {len(result['snapshots'])}")
    print(f"metrics: {len(result['metrics'])}")
    print(f"chart_root: {result['chart_root']}")


if __name__ == "__main__":
    main()
