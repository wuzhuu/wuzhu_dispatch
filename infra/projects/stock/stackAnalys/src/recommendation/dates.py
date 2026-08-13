from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd


def normalize_trade_date(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        raise ValueError(f"invalid trade date: {value!r}")
    return ts.normalize()


def trade_date(value: Any) -> dt.date:
    return normalize_trade_date(value).date()
