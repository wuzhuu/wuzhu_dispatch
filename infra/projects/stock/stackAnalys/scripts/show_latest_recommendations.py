from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.recommendation.stores import RecommendationStore
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Show latest recommendation batch.")
    add_db_path_arg(parser)
    parser.add_argument("--config", default="config/recommendation.example.yaml", help="Accepted for CLI symmetry.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Accepted for CLI symmetry.")
    parser.add_argument("--force", action="store_true", help="Accepted for CLI symmetry.")
    args = parser.parse_args()
    rec = RecommendationStore(args.db_path)
    try:
        rec.refresh()
        where = ""
        params = []
        if args.batch_id:
            where = "WHERE batch_id=?"
            params.append(args.batch_id)
        elif args.date:
            where = "WHERE signal_date=?"
            params.append(args.date)
        batch = rec.query_optional(f"SELECT * FROM v_recommendation_batch {where} ORDER BY signal_date DESC, created_at DESC LIMIT 1", params)
        if batch.empty:
            print("status: NO_RECOMMENDATION_BATCH")
            return
        batch_id = batch["batch_id"].iloc[0]
        print(batch.to_string(index=False))
        items = rec.query_optional(
            "SELECT rank, ts_code, name, industry_name, final_score, risk_level, weight, tracking_status FROM v_recommendation_item WHERE batch_id=? ORDER BY rank",
            [batch_id],
        )
        if items.empty:
            print("items: 0")
        else:
            print(items.to_string(index=False))
    finally:
        rec.close()


if __name__ == "__main__":
    main()
