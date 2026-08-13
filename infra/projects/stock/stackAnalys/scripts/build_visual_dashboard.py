from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import add_db_path_arg
from src.visualization.dashboard_summary import build_visual_dashboard


def main() -> None:
    parser = argparse.ArgumentParser(description="Build daily visual analysis dashboard from local lake data.")
    add_db_path_arg(parser)
    parser.add_argument("--date", default=None, help="Trade date YYYY-MM-DD; default latest analysis date.")
    args = parser.parse_args()
    result = build_visual_dashboard(args.db_path, target_date=args.date)
    print(f"visualization_snapshot rows: {len(result['snapshots'])}")
    print(f"visualization_metric rows: {len(result['metrics'])}")
    if result["warnings"]:
        print("warnings:")
        for warning in result["warnings"]:
            print(f"- {warning}")


if __name__ == "__main__":
    main()
