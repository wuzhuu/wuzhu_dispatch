from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.jobs.quality_check import run_quality_checks
from src.storage.lake_store import LakeStore
from src.utils.config import add_db_path_arg, load_settings, resolve_db_path
from src.utils.proxy import disable_proxy_if_configured


def main() -> None:
    disable_proxy_if_configured()
    parser = argparse.ArgumentParser()
    add_db_path_arg(parser)
    parser.add_argument("--date", default=None, help="Target date YYYY-MM-DD; default latest daily_price date.")
    parser.add_argument("--sample", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    store = LakeStore(db_path=resolve_db_path(settings, args.db_path))
    try:
        store.refresh_views()
        target = args.date
        if target is None:
            row = store.execute("SELECT MAX(trade_date) FROM v_daily_price").fetchone()
            target = str(row[0]) if row and row[0] is not None else ""
        if not target:
            raise RuntimeError("No daily_price data found; cannot infer quality check date.")
        df = run_quality_checks(store, target, sample_mode=args.sample)
        print(df.to_string(index=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
