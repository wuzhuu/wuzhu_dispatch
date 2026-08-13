from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.collectors.free_source_registry import list_free_sources


IMPORTS = {
    "akshare": "akshare",
    "eastmoney": "akshare",
    "sina": "akshare",
    "tencent": "akshare",
    "netease": "akshare",
    "baostock": "baostock",
    "yahoo_finance": "yfinance",
    "stooq": "requests",
    "rss_news": "feedparser",
}


def main() -> None:
    for source in list_free_sources():
        package = IMPORTS.get(source.name)
        installed = importlib.util.find_spec(package) is not None if package else True
        status = "ready" if source.enabled and installed else "disabled" if not source.enabled else "missing_dependency"
        print(f"{source.name}: {status} | {source.description}")


if __name__ == "__main__":
    main()
