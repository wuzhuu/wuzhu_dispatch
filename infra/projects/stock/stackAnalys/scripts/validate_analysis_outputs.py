from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.analysis_lake import AnalysisLakeStore, DATASET_KEYS
from src.utils.config import add_db_path_arg


REQUIRED_DATASETS = [
    "factor_daily",
    "score_daily",
    "risk_flag_daily",
    "market_state_daily",
    "daily_analysis_report",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate persisted analysis outputs.")
    add_db_path_arg(parser)
    args = parser.parse_args()
    store = AnalysisLakeStore(args.db_path)
    rows = []
    try:
        store.refresh_views()
        trade_date = pd.Timestamp.now().date()
        for dataset in REQUIRED_DATASETS:
            df = store.read_dataset(dataset)
            status = "OK" if not df.empty else "FAIL"
            rows.append(_row(trade_date, f"{dataset}_exists", status, f"rows={len(df)}", len(df), 1))
            keys = DATASET_KEYS.get(dataset, [])
            if not df.empty and keys and all(key in df.columns for key in keys):
                dup_count = int(df.duplicated(keys).sum())
                rows.append(_row(trade_date, f"{dataset}_duplicate_keys", "OK" if dup_count == 0 else "FAIL", f"duplicates={dup_count}", dup_count, 0))
        store.write_validation(rows)
        for row in rows:
            print(f"{row['check_name']}: {row['status']} {row['message']}")
        print(f"saved: lake/analysis_validation")
        print(f"data_root: {store.data_root}")
    finally:
        store.close()


def _row(trade_date, check_name: str, status: str, message: str, value, threshold) -> dict:
    return {
        "trade_date": trade_date,
        "check_name": check_name,
        "status": status,
        "message": message,
        "value": value,
        "threshold": threshold,
        "created_at": pd.Timestamp.now(),
    }


if __name__ == "__main__":
    main()
