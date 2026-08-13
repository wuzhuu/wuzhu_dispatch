from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class IndustryProviderConfig:
    timeout: float = 12.0
    retries: int = 2
    request_interval: float = 0.15


def fetch_stock_industry_map(ts_codes: Iterable[str] | None = None, config: IndustryProviderConfig | None = None) -> pd.DataFrame:
    """Fetch stock-to-industry mapping from BaoStock.

    This compatibility provider intentionally avoids EastMoney industry endpoints.
    Production updates should prefer scripts/update_stock_industry_map.py so that
    results are versioned in stock_industry_map and exposed through
    v_stock_industry_map.
    """
    del config
    try:
        from scripts.update_stock_industry_map import fetch_baostock_industry_map
    except Exception as exc:
        return _empty(f"baostock provider import failed: {exc!r}")
    df = fetch_baostock_industry_map()
    if df.empty or not ts_codes:
        return df
    wanted = {_normalize_ts_code(code) for code in ts_codes if code}
    return df[df["ts_code"].isin(wanted)].reset_index(drop=True)


def _normalize_ts_code(value: object) -> str:
    text = str(value).strip().lower()
    if "." in text:
        prefix, code = text.split(".", 1)
        if prefix in {"sh", "sz", "bj"}:
            return f"{prefix}.{code.zfill(6)}"
    digits = "".join(ch for ch in text if ch.isdigit())[-6:]
    if digits.startswith(("6", "9")):
        return f"sh.{digits}"
    if digits.startswith(("8", "4")):
        return f"bj.{digits}"
    return f"sz.{digits}"


def _empty(message: str = "") -> pd.DataFrame:
    df = pd.DataFrame(columns=["ts_code", "name", "industry_name", "source", "fetched_at"])
    if message:
        df.attrs["error"] = message
    return df
