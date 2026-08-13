from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
from loguru import logger

from src.collectors.market_provider import MarketDataProvider, ProviderRunInfo
from src.jobs.daily_update_job import run_daily_update
from src.jobs.quality_check import run_quality_checks
from src.storage.lake_store import LakeStore
from src.utils.calendar import is_trade_day
from src.utils.config import ensure_project_dirs, load_settings, resolve_db_path, resolve_log_root
from src.utils.logger import setup_logger


DATASET_NAME = "daily_price"


@dataclass
class WatchDecision:
    should_run: bool
    status: str
    message: str
    latest_remote_date: str | None = None
    latest_local_date: str | None = None


class DailyDataWatcher:
    def __init__(
        self,
        db_path: str | Path | None = None,
        provider: MarketDataProvider | None = None,
        update_func: Callable[[str | None, bool, str | None], None] = run_daily_update,
        store: LakeStore | None = None,
        settings: dict | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or load_settings()
        self.watcher_cfg = self.settings.get("watcher", {})
        self.db_path = str(db_path) if db_path is not None else None
        self.provider = provider or MarketDataProvider()
        self.source_name = self.provider.source_config_summary() if hasattr(self.provider, "source_config_summary") else "configured_market_provider"
        self.update_func = update_func
        self.store = store or LakeStore(db_path=resolve_db_path(self.settings, self.db_path))
        self._owns_store = store is None
        self.sleep_func = sleep_func
        self.report_rows: list[str] = []
        self._ensure_daily_view()

    def _ensure_daily_view(self) -> None:
        try:
            self.store.execute("SELECT 1 FROM v_daily_price LIMIT 1").fetchone()
        except Exception:
            self.store.refresh_views()

    def check_local_latest_date(self) -> str | None:
        row = self.store.execute("SELECT MAX(trade_date) FROM v_daily_price").fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def _target_count(self, target_date: str) -> int:
        row = self.store.execute("SELECT COUNT(*) FROM v_daily_price WHERE trade_date = ?", [target_date]).fetchone()
        return int(row[0]) if row else 0

    def _min_complete_daily_rows(self) -> int:
        return int(self.watcher_cfg.get("min_complete_daily_price_rows", 3000))

    def _local_has_target_date(self, target_date: str) -> bool:
        return self._target_count(target_date) > 0

    def probe_remote_latest_date(self, target_date: str) -> tuple[str | None, str, list[ProviderRunInfo]]:
        probe_symbols = self.watcher_cfg.get("probe_symbols", ["sh.000300", "sz.000001"])
        sleep_min = float(self.watcher_cfg.get("request_sleep_min", 0.8))
        sleep_max = float(self.watcher_cfg.get("request_sleep_max", 2.5))
        latest_remote: str | None = None
        messages: list[str] = []
        runs: list[ProviderRunInfo] = []

        for index, symbol in enumerate(probe_symbols):
            if index:
                self.sleep_func(random.uniform(sleep_min, sleep_max))
            try:
                df = self._probe_symbol(symbol, target_date)
                if self.provider.last_run:
                    runs.append(self.provider.last_run)
                if df.empty:
                    messages.append(f"{symbol}: empty")
                    continue
                remote_date = str(pd.to_datetime(df["trade_date"], errors="coerce").max().date())
                latest_remote = max(latest_remote, remote_date) if latest_remote else remote_date
                messages.append(f"{symbol}: latest={remote_date} source={self.provider.last_run.source if self.provider.last_run else ''}")
            except Exception as exc:
                if self.provider.last_run:
                    runs.append(self.provider.last_run)
                messages.append(f"{symbol}: probe failed {exc!r}")

        return latest_remote, "; ".join(messages), runs

    def should_run_update(self, target_date: str) -> WatchDecision:
        latest_local = self.check_local_latest_date()
        if not is_trade_day(target_date.replace("-", "")):
            return WatchDecision(False, "SKIPPED", f"{target_date} is not a trade day", latest_local_date=latest_local)

        local_count = self._target_count(target_date)
        min_complete_rows = self._min_complete_daily_rows()
        if local_count >= min_complete_rows:
            return WatchDecision(False, "SUCCESS", f"local already has complete daily_price rows for {target_date}: {local_count}/{min_complete_rows}", latest_local_date=target_date)

        partial_message = ""
        if local_count > 0:
            partial_message = f"local partial daily_price rows={local_count}/{min_complete_rows}; "

        if self._daily_attempts(target_date) >= int(self.watcher_cfg.get("max_daily_attempts", 3)):
            return WatchDecision(False, "PENDING", f"{partial_message}max daily watcher attempts reached", latest_local_date=latest_local)

        latest_remote, message, runs = self._probe_remote_with_backoff(target_date)
        source_status = self._source_status_from_runs(runs)
        if source_status == "FAIL" and self.watcher_cfg.get("stop_if_source_unstable", True):
            return WatchDecision(False, "FAIL", f"{partial_message}source unstable; {message}", latest_remote, latest_local)
        if latest_remote != target_date:
            return WatchDecision(False, "PENDING", f"{partial_message}remote not ready; {message}", latest_remote, latest_local)
        if source_status == "WARNING":
            return WatchDecision(True, "WARNING", f"{partial_message}remote ready with fallback warning; {message}", latest_remote, latest_local)
        return WatchDecision(True, "READY", f"{partial_message}remote ready; {message}", latest_remote, latest_local)

    def _probe_remote_with_backoff(self, target_date: str) -> tuple[str | None, str, list[ProviderRunInfo]]:
        backoffs = list(self.watcher_cfg.get("retry_backoff_seconds", [30, 120, 300]))
        max_attempts = max(1, int(self.watcher_cfg.get("max_daily_attempts", 3)))
        attempts = min(max_attempts, len(backoffs) + 1)
        messages: list[str] = []
        all_runs: list[ProviderRunInfo] = []
        latest_remote: str | None = None

        for attempt in range(attempts):
            latest_remote, message, runs = self.probe_remote_latest_date(target_date)
            messages.append(f"attempt={attempt + 1}: {message}")
            all_runs.extend(runs)
            if latest_remote == target_date or self._source_status_from_runs(runs) != "FAIL":
                break
            if attempt < attempts - 1:
                self.sleep_func(float(backoffs[min(attempt, len(backoffs) - 1)]))
        return latest_remote, " | ".join(messages), all_runs

    def run_once(self, target_date: str | None = None) -> WatchDecision:
        setup_logger("daily_watcher.log")
        target_date = target_date or datetime.now().strftime("%Y-%m-%d")
        self.report_rows = [
            f"- target_date: {target_date}",
            f"- checked_at: {datetime.now():%Y-%m-%d %H:%M:%S}",
        ]

        try:
            decision = self.should_run_update(target_date)
            self.report_rows.append(f"- decision: {decision.status} {decision.message}")
            if not decision.should_run:
                self._write_status(target_date, decision)
                self._write_report(target_date, decision, pd.DataFrame())
                return decision

            try:
                self.update_func(target_date, False, self.db_path)
                self.store.refresh_views()
            except Exception as exc:
                logger.exception("daily update failed")
                fail_decision = WatchDecision(False, "FAIL", repr(exc), decision.latest_remote_date, self.check_local_latest_date())
                self._write_status(target_date, fail_decision)
                self._write_report(target_date, fail_decision, pd.DataFrame())
                return fail_decision

            quality_df = run_quality_checks(self.store, target_date, sample_mode=False)
            latest_local = self.check_local_latest_date()
            local_count = self._target_count(target_date)
            min_complete_rows = self._min_complete_daily_rows()
            quality_status = "SUCCESS" if not quality_df.empty and not (quality_df["status"] == "FAIL").any() and local_count >= min_complete_rows else "WARNING"
            message = f"daily update finished; local_rows={local_count}/{min_complete_rows}"
            if decision.status == "WARNING" and quality_status == "SUCCESS":
                quality_status = "WARNING"
            final_decision = WatchDecision(False, quality_status, message, decision.latest_remote_date, latest_local)
            self._write_status(target_date, final_decision)
            self._write_report(target_date, final_decision, quality_df)
            return final_decision
        finally:
            if self._owns_store:
                self.store.close()

    def _probe_symbol(self, symbol: str, target_date: str) -> pd.DataFrame:
        if symbol.startswith("sh.000") or symbol.startswith("sz.399"):
            return self.provider.get_index_daily(symbol, target_date, target_date)
        return self.provider.get_daily_price(symbol, target_date, target_date)

    @staticmethod
    def _source_status_from_runs(runs: Iterable[ProviderRunInfo]) -> str:
        statuses = []
        for run in runs:
            if not run.success:
                statuses.append("FAIL")
            elif run.fallback_used:
                statuses.append("FAIL")
            elif run.fallback_error:
                statuses.append("WARNING")
            else:
                statuses.append("SUCCESS")
        if "FAIL" in statuses:
            return "FAIL"
        if "WARNING" in statuses:
            return "WARNING"
        return "SUCCESS" if statuses else "FAIL"

    def _daily_attempts(self, target_date: str) -> int:
        row = self.store.execute(
            """
            SELECT COUNT(*)
            FROM daily_update_log
            WHERE task_name = 'daily_watcher' AND target = ?
            """,
            [target_date],
        ).fetchone()
        return int(row[0]) if row else 0

    def _write_status(self, target_date: str, decision: WatchDecision) -> None:
        checked_at = pd.Timestamp.now()
        self.store.execute(
            "DELETE FROM data_source_status WHERE source = ? AND dataset_name = ?",
            [self.source_name, DATASET_NAME],
        )
        self.store.execute(
            """
            INSERT INTO data_source_status (
                source, dataset_name, latest_remote_date, latest_local_date, status, message, checked_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                self.source_name,
                DATASET_NAME,
                decision.latest_remote_date,
                decision.latest_local_date,
                decision.status,
                decision.message,
                checked_at,
            ],
        )
        self.store.execute(
            "INSERT INTO daily_update_log (log_time, task_name, target, status, message) VALUES (?, ?, ?, ?, ?)",
            [checked_at, "daily_watcher", target_date, decision.status, decision.message],
        )

    def _write_report(self, target_date: str, decision: WatchDecision, quality_df: pd.DataFrame) -> Path:
        log_root = resolve_log_root(self.settings)
        log_root.mkdir(parents=True, exist_ok=True)
        path = log_root / f"daily_watcher_{target_date.replace('-', '')}.md"
        lines = [
            "# Daily Watcher Report",
            "",
            *self.report_rows,
            f"- final_status: {decision.status}",
            f"- latest_remote_date: {decision.latest_remote_date or ''}",
            f"- latest_local_date: {decision.latest_local_date or ''}",
            f"- message: {decision.message}",
            "",
            "## Quality",
            "",
            quality_df.to_markdown(index=False) if not quality_df.empty else "No quality rows.",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def run_daily_watcher(target_date: str | None = None, db_path: str | None = None) -> WatchDecision:
    settings = load_settings()
    ensure_project_dirs(settings)
    watcher = DailyDataWatcher(db_path=db_path, settings=settings)
    return watcher.run_once(target_date)
