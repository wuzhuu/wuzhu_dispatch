from __future__ import annotations

import pandas as pd

from src.utils.config import load_api_keys


def is_akshare_enabled() -> bool:
    api_keys = load_api_keys()
    return bool(api_keys.get("akshare", {}).get("enabled", False))


def fetch_daily_price_fallback(trade_date: str) -> pd.DataFrame:
    """Expose an enabled AKShare fallback hook.

    AKShare normally does not require an API token. Exact historical all-market
    daily bars do not map one-to-one to the Tushare daily endpoint, so this
    first version avoids silently writing mismatched fallback data.
    """
    if not is_akshare_enabled():
        raise RuntimeError("AKShare fallback is disabled in stock/key/api_keys.yaml or STOCK_API_KEYS_PATH -> akshare.enabled.")
    raise NotImplementedError(f"AKShare fallback is enabled but not wired into daily_price yet: {trade_date}")
