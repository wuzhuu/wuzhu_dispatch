from __future__ import annotations

import pandas as pd


def fetch_institution_holdings_akshare(symbol: str) -> pd.DataFrame:
    """Fetch public institution holding disclosures through AKShare when available."""
    import akshare as ak

    df = ak.stock_institute_hold(symbol=symbol)
    if not df.empty:
        df = df.copy()
        df["source"] = "akshare.stock_institute_hold"
        df["updated_at"] = pd.Timestamp.now()
    return df
