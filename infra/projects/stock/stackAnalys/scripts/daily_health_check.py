from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import smtplib
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Callable

import duckdb
import yaml
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.collectors.market_provider import MarketDataProvider
from src.storage.lake_store import LakeStore
from src.utils.config import add_db_path_arg, ensure_project_dirs, load_settings, resolve_db_path, resolve_log_root
from src.utils.logger import setup_logger
from src.utils.proxy import disable_proxy_if_configured


INDEX_CODES = ["sh.000001", "sz.399001", "sz.399006", "sh.000300", "sh.000905", "sh.000852"]
ALERT_RECIPIENT = "1521045234@qq.com"


@dataclass
class CheckResult:
    name: str
    status: str
    error: str | None = None
    details: str = ""
    checked_at: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily data health checks and bounded repairs.")
    add_db_path_arg(parser)
    parser.add_argument("--date", default=None, help="Target trade date YYYY-MM-DD; default previous weekday.")
    parser.add_argument("--min-daily-price-rows", type=int, default=5000)
    parser.add_argument("--lock-max-age-hours", type=float, default=4.0)
    parser.add_argument("--sql-timeout", type=float, default=20.0)
    parser.add_argument("--stock-timeout", type=float, default=30.0)
    parser.add_argument("--index-timeout", type=float, default=20.0)
    parser.add_argument("--stock-basic-timeout", type=float, default=45.0)
    parser.add_argument("--checkpoint-timeout", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true", help="Report repairs without writing or killing processes.")
    return parser.parse_args()


def previous_weekday(today: datetime | None = None) -> str:
    current = (today or datetime.now()).date() - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current.strftime("%Y-%m-%d")


def configure_logging(settings: dict[str, Any]) -> None:
    setup_logger("daily_update.log")
    log_root = resolve_log_root(settings)
    log_root.mkdir(parents=True, exist_ok=True)
    logger.add(log_root / "daily_health_check.log", level="INFO", rotation="20 MB", retention="30 days", encoding="utf-8", enqueue=True)


def _alert_state_path(settings: dict[str, Any]) -> Path:
    return resolve_log_root(settings) / "daily_health_alert_state.json"


def load_alert_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(key): str(value) for key, value in (data or {}).items()}
    except Exception as exc:
        logger.warning("alert state ignored path={} error={}", path, exc)
        return {}


def save_alert_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp_path.replace(path)


def alert_signature(result: CheckResult) -> str:
    payload = "\n".join([result.name, result.status, result.error or "", result.details or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def dedupe_alert_results(results: list[CheckResult], state_path: Path) -> list[CheckResult]:
    state = load_alert_state(state_path)
    next_state = dict(state)
    alert_results: list[CheckResult] = []
    for result in results:
        if result.status == "OK":
            next_state.pop(result.name, None)
            continue
        signature = alert_signature(result)
        if state.get(result.name) == signature:
            logger.info("alert suppressed duplicate check={} status={}", result.name, result.status)
            continue
        next_state[result.name] = signature
        alert_results.append(result)
    save_alert_state(state_path, next_state)
    return alert_results


def send_alert_summary(results: list[CheckResult]) -> None:
    if not results:
        return
    cfg_path = Path("~/key/email_smtp.yaml").expanduser()
    if not cfg_path.exists():
        logger.error("alert skipped; smtp config missing: {}", cfg_path)
        return
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = (yaml.safe_load(f) or {}).get("smtp", {})
    if not cfg:
        logger.error("alert skipped; smtp section missing: {}", cfg_path)
        return
    fail_count = sum(1 for result in results if result.status == "FAIL")
    warn_count = sum(1 for result in results if result.status == "WARN")
    subject = f"【数据健康告警】FAIL {fail_count} / WARN {warn_count}"
    sections: list[str] = []
    for index, result in enumerate(results, start=1):
        sections.extend(
            [
                f"{index}. {result.name} [{result.status}]",
                f"检查时间：{result.checked_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "错误详情：",
                str(result.error or result.details or "无错误详情"),
                "建议修复动作：",
                result.details or "请查看 logs/daily_health_check.log 和 daily_update.log",
                "",
            ]
        )
    body = "\n".join(
        [
            f"检查时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            f"告警项数：{len(results)}",
            "",
            *sections,
        ]
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = cfg["username"]
    msg["To"] = ALERT_RECIPIENT
    msg["Subject"] = Header(subject, "utf-8")
    try:
        server = smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=10)
        server.starttls()
        server.login(cfg["username"], cfg["password"])
        server.sendmail(cfg["username"], [ALERT_RECIPIENT], msg.as_string())
        server.quit()
        logger.info("alert summary sent count={} recipient={}", len(results), ALERT_RECIPIENT)
    except Exception as exc:
        logger.exception("alert summary send failed error={}", exc)


def _run_callable(queue, func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    try:
        queue.put(("ok", func(*args, **kwargs)))
    except BaseException as exc:
        queue.put(("error", repr(exc)))


def run_with_timeout(func: Callable[..., Any], timeout_seconds: float, *args: Any, **kwargs: Any) -> Any:
    ctx = get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=_run_callable, args=(queue, func, args, kwargs))
    proc.start()
    proc.join(timeout_seconds)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        raise TimeoutError(f"{func.__name__} exceeded {timeout_seconds:.0f}s")
    if queue.empty():
        if proc.exitcode == 0:
            return None
        raise RuntimeError(f"{func.__name__} exited with code {proc.exitcode}")
    status, payload = queue.get()
    if status == "ok":
        return payload
    raise RuntimeError(str(payload))


def query_scalar(db_path: str, sql: str, params: list[object] | None = None) -> Any:
    store = LakeStore(db_path=db_path)
    try:
        store.refresh_views()
        row = store.execute(sql, params or []).fetchone()
        return row[0] if row else None
    finally:
        store.close()


def checkpoint_database(db_path: str) -> str:
    conn = duckdb.connect(db_path)
    try:
        conn.execute("CHECKPOINT")
        return "checkpoint completed"
    finally:
        conn.close()


def fetch_stock_basic_payload(db_path: str, target_date: str) -> dict[str, Any]:
    provider = MarketDataProvider()
    try:
        df = provider.get_stock_basic()
        rows = 0
        if not df.empty:
            store = LakeStore(db_path=db_path)
            try:
                rows = store.write_stock_basic_snapshot(df, snapshot_date=target_date)
            finally:
                store.close()
        return {"rows": int(rows), "source": provider.last_run.source if provider.last_run else ""}
    finally:
        provider.close_baostock_session()


def check_duckdb_locks(db_path: Path, max_age_hours: float, dry_run: bool) -> CheckResult:
    name = "残留 DuckDB 锁"
    lsof = subprocess.run(["bash", "-lc", f"command -v lsof >/dev/null && lsof -F pcft -- {str(db_path)!r} || true"], text=True, capture_output=True, timeout=15)
    if lsof.returncode != 0:
        return CheckResult(name, "WARN", lsof.stderr.strip() or "lsof failed", "skip lock repair")
    stale: list[tuple[int, str, float]] = []
    current_pid = os.getpid()
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in lsof.stdout.splitlines():
        if line.startswith("p"):
            if current:
                records.append(current)
            current = {"pid": line[1:]}
        elif line.startswith("c"):
            current["command"] = line[1:]
    if current:
        records.append(current)
    for record in records:
        pid_text = record.get("pid", "")
        command = record.get("command", "")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid is None or pid == current_pid or command in {"cron", "crond"}:
            continue
        try:
            age_seconds = time.time() - os.stat(f"/proc/{pid}").st_ctime
        except OSError:
            continue
        if age_seconds >= max_age_hours * 3600:
            stale.append((pid, command, age_seconds / 3600))
    if not stale:
        return CheckResult(name, "OK", details="no stale non-cron DuckDB lock holders")
    if dry_run:
        return CheckResult(name, "WARN", details=f"dry-run stale holders={stale}")
    killed: list[str] = []
    for pid, command, age_hours in stale:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(f"{pid}:{command}:{age_hours:.1f}h")
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            return CheckResult(name, "FAIL", repr(exc), f"failed to kill {pid}:{command}")
    return CheckResult(name, "OK", details=f"killed stale holders={killed}")


def check_wal_checkpoint(db_path: Path, timeout_seconds: float, dry_run: bool) -> CheckResult:
    name = "索引修复"
    wal_path = Path(f"{db_path}.wal")
    if not wal_path.exists():
        return CheckResult(name, "OK", details="wal not present")
    if dry_run:
        return CheckResult(name, "WARN", details=f"dry-run wal present: {wal_path}")
    try:
        message = run_with_timeout(checkpoint_database, timeout_seconds, str(db_path))
        return CheckResult(name, "OK", details=message)
    except Exception as exc:
        return CheckResult(name, "FAIL", repr(exc), "manual DuckDB checkpoint may be required")


def check_daily_price(db_path: Path, target_date: str, min_rows: int, sql_timeout: float, stock_timeout: float, dry_run: bool) -> CheckResult:
    name = "昨日管线是否完成"
    try:
        rows = int(
            run_with_timeout(
                query_scalar,
                sql_timeout,
                str(db_path),
                "SELECT COUNT(*) FROM v_daily_price WHERE trade_date = ?",
                [target_date],
            )
            or 0
        )
    except Exception as exc:
        return CheckResult(name, "FAIL", repr(exc), "inspect DuckDB availability and v_daily_price")
    if rows > min_rows:
        return CheckResult(name, "OK", details=f"{target_date} daily_price rows={rows}")
    # 数据缺失 => 仅报告，不做全量补跑（5000+ 只股票逐个 fetch 会超时）
    # 补跑由每日管线 19:00 daily_run.sh 或 backfill_missing_dates.py 负责
    return CheckResult(name, "WARN", None, f"{target_date} daily_price rows={rows}, threshold={min_rows}. 管线可能未完成，等待下次 daily_run.sh 补跑。")


def check_stock_basic(db_path: Path, target_date: str, sql_timeout: float, stock_basic_timeout: float, dry_run: bool) -> CheckResult:
    name = "数据完整性"
    try:
        latest = run_with_timeout(
            query_scalar,
            sql_timeout,
            str(db_path),
            "SELECT CAST(MAX(snapshot_date) AS VARCHAR) FROM v_stock_basic_latest",
            [],
        )
    except Exception as exc:
        return CheckResult(name, "FAIL", repr(exc), "inspect stock_basic view")
    if str(latest)[:10] == target_date:
        return CheckResult(name, "OK", details=f"stock_basic snapshot={latest}")
    if dry_run:
        return CheckResult(name, "WARN", details=f"dry-run latest_snapshot={latest}, expected={target_date}")
    try:
        payload = run_with_timeout(fetch_stock_basic_payload, stock_basic_timeout, str(db_path), target_date)
        rows = int((payload or {}).get("rows", 0))
        if rows <= 0:
            return CheckResult(name, "FAIL", "stock_basic returned zero rows", "retry stock_basic source")
        return CheckResult(name, "OK", details=f"updated stock_basic snapshot={target_date} rows={rows}")
    except Exception as exc:
        return CheckResult(name, "FAIL", repr(exc), "retry stock_basic source or switch fallback")


def check_index_daily(db_path: Path, target_date: str, index_timeout: float, dry_run: bool) -> CheckResult:
    name = "指数数据检查"
    store = LakeStore(db_path=db_path)
    try:
        store.refresh_views()
        rows = store.execute(
            "SELECT COUNT(DISTINCT index_code) FROM v_index_daily WHERE trade_date = ?",
            [target_date],
        ).fetchone()
        count = int(rows[0]) if rows else 0
    except Exception as exc:
        return CheckResult(name, "FAIL", repr(exc), "inspect v_index_daily view")
    finally:
        store.close()
    if count >= len(INDEX_CODES):
        return CheckResult(name, "OK", details=f"{target_date} index_codes={count}")
    # 数据缺失 => 仅报告，不做补跑（补跑由 daily_run.sh 负责）
    return CheckResult(name, "WARN", None, f"{target_date} index_codes={count}/{len(INDEX_CODES)}. 管线可能未完成，等待下次 daily_run.sh 补跑。")


def main() -> int:
    disable_proxy_if_configured()
    args = parse_args()
    settings = load_settings()
    ensure_project_dirs(settings)
    configure_logging(settings)
    db_path = resolve_db_path(settings, args.db_path)
    target_date = args.date or previous_weekday()
    logger.info("daily health check start target_date={} db_path={} dry_run={}", target_date, db_path, args.dry_run)

    checks = [
        lambda: check_duckdb_locks(db_path, args.lock_max_age_hours, args.dry_run),
        lambda: check_wal_checkpoint(db_path, args.checkpoint_timeout, args.dry_run),
        lambda: check_stock_basic(db_path, target_date, args.sql_timeout, args.stock_basic_timeout, args.dry_run),
        lambda: check_daily_price(db_path, target_date, args.min_daily_price_rows, args.sql_timeout, args.stock_timeout, args.dry_run),
        lambda: check_index_daily(db_path, target_date, args.index_timeout, args.dry_run),
    ]
    results: list[CheckResult] = []
    for check in checks:
        try:
            result = check()
        except Exception as exc:
            result = CheckResult("health check wrapper", "FAIL", repr(exc), "unexpected wrapper error")
        result.checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        results.append(result)
        logger.info("health check result name={} status={} details={} error={}", result.name, result.status, result.details, result.error)
    alert_results = dedupe_alert_results(results, _alert_state_path(settings))
    send_alert_summary(alert_results)
    failed = [item for item in results if item.status == "FAIL"]
    logger.info("daily health check finished failed={} total={}", len(failed), len(results))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
