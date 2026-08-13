from __future__ import annotations

import pandas as pd


def fetch_cninfo_announcements(keyword: str = "", page_num: int = 1) -> pd.DataFrame:
    """Reserved public disclosure entrance for CNINFO/stock exchanges.

    CNINFO and exchange disclosure pages are public, but request parameters and
    throttling rules change often. Keep this entry explicit so callers do not
    accidentally scrape too aggressively.
    """
    raise NotImplementedError("CNINFO/exchange announcement collector is reserved; implement with strict rate limits before use.")
