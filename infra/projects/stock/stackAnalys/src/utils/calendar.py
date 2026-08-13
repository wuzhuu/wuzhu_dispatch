from __future__ import annotations

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

import pandas as pd


def _fetch_sina_calendar() -> pd.DataFrame | None:
    """Fetch Sina trading calendar (runs in a thread, not used directly)."""
    try:
        import akshare as ak

        return ak.tool_trade_date_hist_sina()
    except Exception:
        return None


def is_trade_day(date_str: str) -> bool:
    """Check if date_str (YYYYMMDD) is a Chinese trading day.

    Quick weekday check first (covers 99% of cases), then tries Sina calendar
    to catch holidays, with a 5-second network timeout.
    """
    dt = datetime.strptime(date_str, "%Y%m%d")
    if dt.weekday() >= 5:
        return False

    # Try Sina trade calendar with timeout (covers Chinese holidays on weekdays)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_fetch_sina_calendar)
            cal = future.result(timeout=5)
            if cal is not None and not cal.empty:
                dates = pd.to_datetime(cal.iloc[:, 0], errors="coerce").dt.strftime("%Y%m%d")
                return date_str in set(dates.dropna())
    except FuturesTimeout:
        pass  # Sina unreachable → assume weekday is a trade day
    except Exception:
        pass
    return True
