from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.analysis_lake import AnalysisLakeStore
from src.analysis.path_resolver import resolve_data_root
from src.storage.lake_store import LakeStore
from src.utils.config import add_db_path_arg, load_settings, resolve_db_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh DuckDB views to point at the current local data lake.")
    add_db_path_arg(parser)
    args = parser.parse_args()
    settings = load_settings()
    db_path = resolve_db_path(settings, args.db_path)
    data_root = resolve_data_root(db_path)
    lake_root = data_root / "lake"
    market_store = LakeStore(db_path=db_path, lake_root=lake_root)
    analysis_store = AnalysisLakeStore(db_path=db_path)
    try:
        market_store.rebuild_manifest()
        market_store.refresh_views()
        analysis_store.refresh_views()
        print(f"refreshed market and analysis lake views: {db_path}")
        print(f"market_lake_root: {market_store.lake_root}")
        print(f"analysis_lake_root: {analysis_store.lake_root}")
    finally:
        analysis_store.close()
        market_store.close()


if __name__ == "__main__":
    main()
