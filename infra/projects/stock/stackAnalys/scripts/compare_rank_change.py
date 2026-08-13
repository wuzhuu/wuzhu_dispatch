from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.analysis_lake import AnalysisLakeStore
from src.utils.config import add_db_path_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare latest rank changes and persist them to the data lake.")
    add_db_path_arg(parser)
    parser.add_argument("--top-n", type=int, default=50)
    args = parser.parse_args()
    store = AnalysisLakeStore(args.db_path)
    try:
        score = store.read_dataset("score_daily")
        if score.empty:
            raise RuntimeError("score_daily is empty. Run scripts/run_analysis_pipeline.py first.")
        score["trade_date"] = pd.to_datetime(score["trade_date"]).dt.date
        dates = sorted(score["trade_date"].dropna().unique())
        if len(dates) < 2:
            latest = dates[-1]
            out = score[score["trade_date"] == latest].copy()
            out["previous_rank"] = pd.NA
            out["rank_change"] = pd.NA
            out["is_new_top"] = True
            out["is_drop_top"] = False
        else:
            previous, latest = dates[-2], dates[-1]
            current = score[score["trade_date"] == latest].copy()
            prev = score[score["trade_date"] == previous][["ts_code", "rank"]].rename(columns={"rank": "previous_rank"})
            out = current.merge(prev, on="ts_code", how="left")
            out["rank_change"] = pd.to_numeric(out["previous_rank"], errors="coerce") - pd.to_numeric(out["rank"], errors="coerce")
            out["is_new_top"] = out["previous_rank"].isna() & (pd.to_numeric(out["rank"], errors="coerce") <= args.top_n)
            prev_top = set(score[(score["trade_date"] == previous) & (pd.to_numeric(score["rank"], errors="coerce") <= args.top_n)]["ts_code"])
            curr_top = set(out[pd.to_numeric(out["rank"], errors="coerce") <= args.top_n]["ts_code"])
            out["is_drop_top"] = out["ts_code"].isin(prev_top - curr_top)
        out["top_n"] = args.top_n
        out["created_at"] = pd.Timestamp.now()
        store.write_dataset("ranking_change_daily", out)
        print(out.sort_values("rank").head(args.top_n).to_string(index=False))
        print("saved: lake/ranking_change_daily")
        print("query: SELECT * FROM v_ranking_change_daily ORDER BY trade_date DESC, rank ASC LIMIT 50;")
        print(f"data_root: {store.data_root}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
