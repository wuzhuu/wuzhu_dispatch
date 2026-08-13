from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import requests
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.collectors.market_provider import MarketDataProvider


def test_fallback_circuit_breaker_skips_after_failures() -> None:
    provider = MarketDataProvider()
    provider.fallback_circuit_breaker_failures = 2
    provider.fallback_circuit_breaker_cooldown_seconds = 60
    calls = {"fallback": 0}

    def primary_empty() -> pd.DataFrame:
        return pd.DataFrame()

    def fallback_fails() -> pd.DataFrame:
        calls["fallback"] += 1
        raise requests.ConnectionError("eastmoney closed connection")

    for _ in range(2):
        try:
            provider._with_fallback(
                "daily_price:sz.000001",
                primary_empty,
                fallback_fails,
                primary_source="baostock",
                fallback_source="eastmoney",
            )
        except requests.ConnectionError:
            pass

    df = provider._with_fallback(
        "daily_price:sz.000001",
        primary_empty,
        fallback_fails,
        primary_source="baostock",
        fallback_source="eastmoney",
    )
    assert df.empty
    assert calls["fallback"] == 2, calls
    assert provider.last_run is not None
    assert "disabled by circuit breaker" in provider.last_run.fallback_error


def main() -> None:
    logger.remove()
    test_fallback_circuit_breaker_skips_after_failures()
    print("MarketDataProvider fallback tests passed.")


if __name__ == "__main__":
    main()
