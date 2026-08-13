from __future__ import annotations

import sys
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.collectors.market_provider import ProviderRunInfo
from src.jobs.daily_update_job import _date_ranges
from src.jobs.daily_watcher import DailyDataWatcher
from src.storage.lake_store import LakeStore


TARGET_DATE = "2026-05-29"
NON_TRADE_DATE = "2026-05-30"


class FakeProvider:
    def __init__(self, remote_ready: bool = False):
        self.remote_ready = remote_ready
        self.last_run: ProviderRunInfo | None = None

    def source_config_summary(self) -> str:
        return "daily_price=baostock->eastmoney; daily_price_bse=tencent->eastmoney; index_daily=baostock->eastmoney; stock_basic=akshare->baostock"

    def get_index_daily(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        self.last_run = ProviderRunInfo(f"index_daily:{index_code}", "baostock.query_history_k_data_plus.index", False, success=True)
        if not self.remote_ready:
            return pd.DataFrame(columns=["index_code", "trade_date", "open", "high", "low", "close", "preclose", "volume", "amount", "pct_chg", "source", "updated_at"])
        return pd.DataFrame(
            [
                {
                    "index_code": index_code,
                    "trade_date": pd.to_datetime(end_date).date(),
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "preclose": 1.0,
                    "volume": 1.0,
                    "amount": 1.0,
                    "pct_chg": 0.0,
                    "source": "baostock.query_history_k_data_plus.index",
                    "updated_at": pd.Timestamp.now(),
                }
            ]
        )

    def get_daily_price(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        self.last_run = ProviderRunInfo(f"daily_price:{ts_code}", "baostock.query_history_k_data_plus", False, success=True)
        if not self.remote_ready:
            return pd.DataFrame(columns=_daily_columns())
        return pd.DataFrame([_daily_row(ts_code, end_date)])


def _daily_columns() -> list[str]:
    return ["ts_code", "trade_date", "open", "high", "low", "close", "preclose", "volume", "amount", "pct_chg", "turn", "tradestatus", "is_st", "adjust_type", "source", "updated_at"]


def _daily_row(ts_code: str, trade_date: str) -> dict[str, object]:
    return {
        "ts_code": ts_code,
        "trade_date": pd.to_datetime(trade_date).date(),
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "preclose": 1.0,
        "volume": 1.0,
        "amount": 1.0,
        "pct_chg": 0.0,
        "turn": 0.0,
        "tradestatus": "1",
        "is_st": "0",
        "adjust_type": "qfq",
        "source": "baostock.query_history_k_data_plus",
        "updated_at": pd.Timestamp.now(),
    }


def _settings() -> dict:
    return {
        "project": {"timezone": "Asia/Shanghai"},
        "database": {"duckdb_path": "stock_data_v2.duckdb"},
        "data": {"lake_root": "lake", "log_root": "logs", "report_root": "reports"},
        "watcher": {
            "enabled": True,
            "probe_symbols": ["sh.000300", "sz.000001"],
            "request_sleep_min": 0,
            "request_sleep_max": 0,
            "max_daily_attempts": 3,
            "min_complete_daily_price_rows": 2,
            "retry_backoff_seconds": [0],
            "stop_if_source_unstable": True,
        },
    }


def _watcher(tmp: Path, provider: FakeProvider, update_func=None) -> tuple[DailyDataWatcher, LakeStore]:
    os.environ["STOCK_DATA_ROOT"] = str(tmp)
    store = LakeStore(db_path=tmp / "stock.duckdb", lake_root=tmp / "lake")
    store.connect()
    watcher = DailyDataWatcher(
        db_path=tmp / "stock.duckdb",
        provider=provider,
        update_func=update_func or (lambda target_date, sample_mode, db_path: None),
        store=store,
        settings=_settings(),
        sleep_func=lambda seconds: None,
    )
    return watcher, store


def _status(store: LakeStore) -> str:
    row = store.execute("SELECT status FROM data_source_status WHERE dataset_name = 'daily_price'").fetchone()
    return str(row[0])


def test_non_trade_day_skipped() -> None:
    with TemporaryDirectory() as d:
        watcher, store = _watcher(Path(d), FakeProvider(remote_ready=True))
        decision = watcher.run_once(NON_TRADE_DATE)
        assert decision.status == "SKIPPED"
        assert _status(store) == "SKIPPED"


def test_partial_local_data_triggers_update() -> None:
    with TemporaryDirectory() as d:
        called = {"count": 0}

        def update(target_date, sample_mode, db_path):
            called["count"] += 1
            store.upsert_daily_price(pd.DataFrame([_daily_row("sh.600000", target_date)]))

        watcher, store = _watcher(Path(d), FakeProvider(remote_ready=True), update)
        store.upsert_daily_price(pd.DataFrame([_daily_row("sz.000001", TARGET_DATE)]))
        decision = watcher.run_once(TARGET_DATE)
        assert decision.status in {"SUCCESS", "WARNING"}
        assert called["count"] == 1


def test_complete_local_data_success_without_update() -> None:
    with TemporaryDirectory() as d:
        called = {"count": 0}
        watcher, store = _watcher(Path(d), FakeProvider(remote_ready=True), lambda target_date, sample_mode, db_path: called.__setitem__("count", called["count"] + 1))
        store.upsert_daily_price(
            pd.DataFrame(
                [
                    _daily_row("sz.000001", TARGET_DATE),
                    _daily_row("sh.600000", TARGET_DATE),
                ]
            )
        )
        decision = watcher.run_once(TARGET_DATE)
        assert decision.status == "SUCCESS"
        assert called["count"] == 0


def test_remote_pending_does_not_update() -> None:
    with TemporaryDirectory() as d:
        called = {"count": 0}
        watcher, store = _watcher(Path(d), FakeProvider(remote_ready=False), lambda target_date, sample_mode, db_path: called.__setitem__("count", called["count"] + 1))
        decision = watcher.run_once(TARGET_DATE)
        assert decision.status == "PENDING"
        assert called["count"] == 0
        assert _status(store) == "PENDING"


def test_remote_ready_triggers_update() -> None:
    with TemporaryDirectory() as d:
        called = {"count": 0}

        def update(target_date, sample_mode, db_path):
            called["count"] += 1
            store.upsert_daily_price(
                pd.DataFrame(
                    [
                        _daily_row("sz.000001", target_date),
                        _daily_row("sh.600000", target_date),
                    ]
                )
            )

        watcher, store = _watcher(Path(d), FakeProvider(remote_ready=True), update)
        decision = watcher.run_once(TARGET_DATE)
        assert called["count"] == 1
        assert decision.status in {"SUCCESS", "WARNING"}


def test_repeat_same_day_does_not_duplicate_or_update_again() -> None:
    with TemporaryDirectory() as d:
        called = {"count": 0}

        def update(target_date, sample_mode, db_path):
            called["count"] += 1
            store.upsert_daily_price(
                pd.DataFrame(
                    [
                        _daily_row("sz.000001", target_date),
                        _daily_row("sh.600000", target_date),
                    ]
                )
            )

        watcher, store = _watcher(Path(d), FakeProvider(remote_ready=True), update)
        watcher.run_once(TARGET_DATE)
        watcher2 = DailyDataWatcher(db_path=Path(d) / "stock.duckdb", provider=FakeProvider(remote_ready=True), update_func=update, store=store, settings=_settings(), sleep_func=lambda seconds: None)
        decision = watcher2.run_once(TARGET_DATE)
        rows = store.execute("SELECT COUNT(*) FROM v_daily_price WHERE trade_date = ?", [TARGET_DATE]).fetchone()[0]
        assert decision.status == "SUCCESS"
        assert called["count"] == 1
        assert rows == 2


def test_date_ranges_do_not_cross_weekends() -> None:
    ranges = _date_ranges(["2026-06-05", "2026-06-08", "2026-06-09"])
    assert ranges == [("2026-06-05", "2026-06-05"), ("2026-06-08", "2026-06-09")], ranges


def main() -> None:
    tests = [
        test_non_trade_day_skipped,
        test_partial_local_data_triggers_update,
        test_complete_local_data_success_without_update,
        test_remote_pending_does_not_update,
        test_remote_ready_triggers_update,
        test_repeat_same_day_does_not_duplicate_or_update_again,
        test_date_ranges_do_not_cross_weekends,
    ]
    for test in tests:
        test()
    print("DailyDataWatcher tests passed.")


if __name__ == "__main__":
    main()
