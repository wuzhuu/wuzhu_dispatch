from __future__ import annotations

import pandas as pd


def fetch_industry_boards() -> pd.DataFrame:
    """Deprecated compatibility shim.

    Production industry board data is built locally from v_stock_industry_map
    and daily_price by scripts/build_industry_board_local.py.
    """
    df = pd.DataFrame()
    df.attrs["warning"] = "deprecated: use v_industry_board / industry_board_local"
    return df


def fetch_concept_boards() -> pd.DataFrame:
    import akshare as ak

    df = ak.stock_board_concept_name_em()
    if not df.empty:
        df = df.copy()
        df["board_type"] = "concept"
        df["source"] = "akshare.stock_board_concept_name_em"
        df["updated_at"] = pd.Timestamp.now()
    return df
