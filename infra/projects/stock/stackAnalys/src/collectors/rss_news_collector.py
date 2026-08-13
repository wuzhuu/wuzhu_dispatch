from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import feedparser
import pandas as pd
import requests

from src.storage.lake_store import LakeStore
from src.utils.config import load_settings, resolve_db_path, resolve_lake_root, resolve_report_root
from src.utils.proxy import disable_proxy_if_configured


RSS_NEWS_COLUMNS = [
    "id",
    "title",
    "summary",
    "source",
    "category",
    "url",
    "publish_time",
    "collected_time",
    "source_type",
    "language",
    "raw_id",
    "content_hash",
]


HEALTH_COLUMNS = [
    "source_id",
    "status",
    "consecutive_fail",
    "last_error",
    "last_http_code",
    "last_ok_at",
    "last_fail_at",
    "disabled_until",
    "schema_drift_count",
    "field_completeness",
    "response_snippet",
]


TERMINAL_STATUSES = {"source_dead", "need_codex_fix"}


@dataclass
class FetchResult:
    entries: list[dict[str, Any]]
    error: str | None
    status_code: int | None
    elapsed: float
    response_snippet: str | None = None


def _clean(text: object) -> str:
    value = "" if text is None else str(text)
    value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", value, flags=re.DOTALL)
    return re.sub(r"\s+", " ", value).strip()


def _content_hash(title: str, url: str, summary: str, published: str) -> str:
    return sha256(f"{title}|{url}|{summary}|{published}".encode("utf-8")).hexdigest()


def _make_article_id(title: str, url: str, published: str) -> str:
    return sha256(f"{title}|{url}|{published}".encode("utf-8")).hexdigest()[:16]


def _parse_time(value: object) -> pd.Timestamp | pd.NaT:
    return pd.to_datetime(value, errors="coerce", utc=True)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _feed_id(feed: dict[str, Any]) -> str:
    return str(feed.get("source_id") or feed.get("name") or "rss").strip()


def _enabled_feeds(settings: dict[str, Any]) -> list[dict[str, Any]]:
    cfg: dict[str, Any] = settings.get("free_sources", {}).get("rss_news", {})
    if not cfg.get("enabled", True):
        return []
    return [feed for feed in cfg.get("feeds", []) if feed.get("url")]


def _ensure_tables(store: LakeStore) -> None:
    store.execute(
        """
        CREATE TABLE IF NOT EXISTS rss_news (
            id              VARCHAR PRIMARY KEY,
            title           VARCHAR,
            summary         VARCHAR,
            source          VARCHAR,
            category        VARCHAR,
            url             VARCHAR,
            publish_time    TIMESTAMP WITH TIME ZONE,
            collected_time  TIMESTAMP WITH TIME ZONE,
            source_type     VARCHAR,
            language        VARCHAR,
            raw_id          VARCHAR,
            content_hash    VARCHAR
        )
        """
    )
    store.execute("CREATE INDEX IF NOT EXISTS idx_rss_news_url ON rss_news(url)")
    store.execute("CREATE INDEX IF NOT EXISTS idx_rss_news_hash ON rss_news(content_hash)")
    store.execute(
        """
        CREATE TABLE IF NOT EXISTS rss_news_source_health (
            source_id        VARCHAR PRIMARY KEY,
            status           VARCHAR,
            consecutive_fail INT DEFAULT 0,
            last_error       VARCHAR,
            last_http_code   INT,
            last_ok_at       TIMESTAMP,
            last_fail_at     TIMESTAMP,
            disabled_until   TIMESTAMP,
            schema_drift_count INT DEFAULT 0,
            field_completeness JSON,
            response_snippet VARCHAR
        )
        """
    )


def _load_health(store: LakeStore) -> dict[str, dict[str, Any]]:
    rows = store.execute(f"SELECT {', '.join(HEALTH_COLUMNS)} FROM rss_news_source_health").fetchall()
    return {str(row[0]): dict(zip(HEALTH_COLUMNS, row)) for row in rows}


def _upsert_health(store: LakeStore, record: dict[str, Any]) -> None:
    data = {column: record.get(column) for column in HEALTH_COLUMNS}
    if isinstance(data.get("field_completeness"), dict):
        data["field_completeness"] = json.dumps(data["field_completeness"], ensure_ascii=False)
    store.execute("DELETE FROM rss_news_source_health WHERE source_id = ?", [data["source_id"]])
    placeholders = ", ".join(["?"] * len(HEALTH_COLUMNS))
    columns = ", ".join(HEALTH_COLUMNS)
    store.execute(f"INSERT INTO rss_news_source_health ({columns}) VALUES ({placeholders})", [data[column] for column in HEALTH_COLUMNS])


def _skip_reason(health: dict[str, Any] | None, now: pd.Timestamp) -> str | None:
    if not health:
        return None
    status = str(health.get("status") or "healthy")
    if status in TERMINAL_STATUSES:
        return status
    disabled_until = _parse_time(health.get("disabled_until"))
    if pd.notna(disabled_until) and disabled_until > now:
        return f"disabled_until={disabled_until.isoformat()}"
    if status == "degraded":
        last_fail_at = _parse_time(health.get("last_fail_at"))
        if pd.notna(last_fail_at) and now - last_fail_at < pd.Timedelta(days=2):
            return "degraded_2d_cooldown"
    return None


def _fetch_source(feed: dict[str, Any], timeout: tuple[int, int], retries: int, min_interval: float) -> FetchResult:
    url = str(feed["url"])
    last_error: str | None = None
    last_status: int | None = None
    snippet: str | None = None
    started = time.monotonic()
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min_interval)
        try:
            req_started = time.monotonic()
            response = requests.get(url, timeout=timeout, headers={"User-Agent": "stackAnalys-rss/1.0"})
            elapsed = time.monotonic() - req_started
            last_status = response.status_code
            if response.status_code >= 400:
                snippet = response.text[:500]
                last_error = f"HTTP {response.status_code}"
                continue
            parsed = feedparser.parse(response.content)
            if getattr(parsed, "bozo", False) and not parsed.entries:
                last_error = str(getattr(parsed, "bozo_exception", "feed parse failed"))
                continue
            entries: list[dict[str, Any]] = []
            for entry in parsed.entries:
                published = entry.get("published", entry.get("updated", ""))
                entries.append(
                    {
                        "title": _clean(entry.get("title")),
                        "link": _clean(entry.get("link")),
                        "published": published,
                        "summary": _clean(entry.get("summary", entry.get("description", ""))),
                        "raw_id": _clean(entry.get("id", entry.get("guid", ""))),
                    }
                )
            time.sleep(min_interval)
            return FetchResult(entries=entries, error=None, status_code=response.status_code, elapsed=elapsed)
        except requests.Timeout as exc:
            last_error = f"Timeout: {exc}"
        except requests.ConnectionError as exc:
            last_error = f"ConnectionError: {exc}"
        except Exception as exc:
            last_error = repr(exc)
    return FetchResult(entries=[], error=last_error or "unknown error", status_code=last_status, elapsed=time.monotonic() - started, response_snippet=snippet)


def _existing_keys(store: LakeStore) -> tuple[set[str], set[str]]:
    rows = store.execute("SELECT url, content_hash FROM rss_news").fetchall()
    urls = {str(url) for url, _ in rows if url}
    hashes = {str(content_hash) for _, content_hash in rows if content_hash}
    return urls, hashes


def _completeness(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {"title": 0.0, "url": 0.0, "publish_time": 0.0}
    return {
        "title": sum(bool(row.get("title")) for row in rows) / len(rows),
        "url": sum(bool(row.get("url")) for row in rows) / len(rows),
        "publish_time": sum(pd.notna(row.get("publish_time")) for row in rows) / len(rows),
    }


def _status_after_failure(current: dict[str, Any] | None, result: FetchResult, now: pd.Timestamp) -> dict[str, Any]:
    previous_fails = int((current or {}).get("consecutive_fail") or 0)
    consecutive_fail = previous_fails + 1
    previous_status = str((current or {}).get("status") or "healthy")
    http_code = result.status_code
    status = previous_status if previous_status in TERMINAL_STATUSES else "healthy"
    if http_code == 404 and consecutive_fail >= 2:
        status = "source_dead"
    elif http_code in (403, 429):
        status = "degraded"
    elif consecutive_fail >= 3:
        status = "disabled_24h"
    elif status == "healthy":
        status = "temporary_failure"
    return {
        **(current or {}),
        "status": status,
        "consecutive_fail": consecutive_fail,
        "last_error": result.error,
        "last_http_code": http_code,
        "last_fail_at": now,
        "disabled_until": now + pd.Timedelta(hours=24) if status == "disabled_24h" else (current or {}).get("disabled_until"),
        "response_snippet": result.response_snippet,
    }


def _status_after_success(current: dict[str, Any] | None, rows: list[dict[str, Any]], now: pd.Timestamp) -> dict[str, Any]:
    completeness = _completeness(rows)
    drift = any(completeness[field] < 0.8 for field in ("title", "url", "publish_time"))
    drift_count = int((current or {}).get("schema_drift_count") or 0) + 1 if drift else 0
    status = "need_codex_fix" if drift_count >= 2 else "schema_drift" if drift else "healthy"
    return {
        **(current or {}),
        "status": status,
        "consecutive_fail": 0,
        "last_error": None,
        "last_http_code": 200,
        "last_ok_at": now,
        "disabled_until": None,
        "schema_drift_count": drift_count,
        "field_completeness": completeness,
        "response_snippet": None,
    }


def _write_dataframe(store: LakeStore, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    conn = store.connect()
    conn.register("tmp_rss_news", df[RSS_NEWS_COLUMNS])
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO rss_news
            SELECT * FROM tmp_rss_news
            """
        )
    finally:
        conn.unregister("tmp_rss_news")
    return len(df)


def _report_lines(summary: dict[str, Any], health_rows: list[dict[str, Any]]) -> tuple[str, str, str | None]:
    generated_at = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
    ingest_lines = [
        "# RSS News Ingest Report",
        "",
        f"- generated_at: {generated_at}",
        f"- fetched_sources: {summary['fetched_sources']}",
        f"- skipped_sources: {summary['skipped_sources']}",
        f"- fetched_entries: {summary['fetched_entries']}",
        f"- new_rows: {summary['new_rows']}",
        f"- failed_sources: {len(summary['failed'])}",
        "",
        "## Source Summary",
        "",
        "| source | status | entries | new_rows | error |",
        "|---|---:|---:|---:|---|",
    ]
    for item in summary["sources"]:
        ingest_lines.append(f"| {item['source_id']} | {item['status']} | {item['entries']} | {item['new_rows']} | {_clean(item.get('error'))} |")
    ingest_lines.extend(["", "## Field Completeness", "", "| source | title | url | publish_time |", "|---|---:|---:|---:|"])
    for item in summary["sources"]:
        comp = item.get("field_completeness") or {}
        ingest_lines.append(f"| {item['source_id']} | {comp.get('title', 0):.2%} | {comp.get('url', 0):.2%} | {comp.get('publish_time', 0):.2%} |")
    ingest_lines.extend(["", "## Recent Samples", ""])
    for sample in summary["samples"][:10]:
        ingest_lines.append(f"- [{sample['source']}] {sample['title']} ({sample['publish_time']})")

    health_lines = [
        "# RSS News Source Health",
        "",
        f"- generated_at: {generated_at}",
        "",
        "| source | status | fails | last_ok_at | last_fail_at | disabled_until | completeness |",
        "|---|---|---:|---|---|---|---|",
    ]
    fix_rows: list[dict[str, Any]] = []
    for row in health_rows:
        status = row.get("status") or ""
        if status in TERMINAL_STATUSES:
            fix_rows.append(row)
        comp = row.get("field_completeness")
        if isinstance(comp, str):
            try:
                comp = json.loads(comp)
            except json.JSONDecodeError:
                comp = {}
        comp_text = ", ".join(f"{key}={float(value):.0%}" for key, value in (comp or {}).items())
        health_lines.append(
            f"| {row.get('source_id')} | {status} | {int(row.get('consecutive_fail') or 0)} | "
            f"{row.get('last_ok_at') or ''} | {row.get('last_fail_at') or ''} | {row.get('disabled_until') or ''} | {comp_text} |"
        )

    fix_text = None
    if fix_rows:
        fix_lines = [
            "# RSS News Fix Prompt",
            "",
            f"- generated_at: {generated_at}",
            "",
            "| source | status | last_error | http_code | response_snippet |",
            "|---|---|---|---:|---|",
        ]
        for row in fix_rows:
            fix_lines.append(
                f"| {row.get('source_id')} | {row.get('status')} | {_clean(row.get('last_error'))} | "
                f"{row.get('last_http_code') or ''} | {_clean(row.get('response_snippet'))} |"
            )
        fix_text = "\n".join(fix_lines) + "\n"
    return "\n".join(ingest_lines) + "\n", "\n".join(health_lines) + "\n", fix_text


def run_ingest(
    backfill: bool = False,
    db_path: str | Path | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = settings or load_settings()
    disable_proxy_if_configured()
    cfg = settings.get("free_sources", {}).get("rss_news", {})
    store = LakeStore(
        db_path=resolve_db_path(settings, str(db_path) if db_path else None),
        lake_root=resolve_lake_root(settings),
    )
    _ensure_tables(store)
    now = _now()
    health = _load_health(store)
    existing_urls, existing_hashes = _existing_keys(store)
    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "fetched_sources": 0,
        "skipped_sources": 0,
        "fetched_entries": 0,
        "new_rows": 0,
        "failed": [],
        "sources": [],
        "samples": [],
    }
    feeds = _enabled_feeds(settings)
    timeout = tuple(cfg.get("timeout", [10, 20]))
    retries = int(cfg.get("retries", 1))
    min_interval = 1.0 / max(float(cfg.get("global_qps", 5)), 0.1)

    try:
        for feed in feeds:
            source_id = _feed_id(feed)
            current_health = health.get(source_id)
            skip = None if backfill else _skip_reason(current_health, now)
            if skip:
                summary["skipped_sources"] += 1
                summary["sources"].append({"source_id": source_id, "status": f"skipped:{skip}", "entries": 0, "new_rows": 0, "field_completeness": {}})
                continue

            result = _fetch_source(feed, timeout=timeout, retries=retries, min_interval=min_interval)
            if result.error:
                updated = _status_after_failure(current_health, result, now)
                updated["source_id"] = source_id
                _upsert_health(store, updated)
                summary["failed"].append({"source_id": source_id, "error": result.error, "status_code": result.status_code})
                summary["sources"].append({"source_id": source_id, "status": updated["status"], "entries": 0, "new_rows": 0, "error": result.error, "field_completeness": {}})
                continue

            summary["fetched_sources"] += 1
            source_rows: list[dict[str, Any]] = []
            fetched_rows: list[dict[str, Any]] = []
            for entry in result.entries:
                title = _clean(entry.get("title"))
                url = _clean(entry.get("link"))
                published_raw = entry.get("published", "")
                summary_text = _clean(entry.get("summary"))
                content_hash = _content_hash(title, url, summary_text, str(published_raw))
                row = {
                    "id": _make_article_id(title, url, str(published_raw)),
                    "title": title,
                    "summary": summary_text,
                    "source": source_id,
                    "category": feed.get("category", "rss"),
                    "url": url,
                    "publish_time": _parse_time(published_raw),
                    "collected_time": now,
                    "source_type": "rss",
                    "language": feed.get("language", "unknown"),
                    "raw_id": entry.get("raw_id") or None,
                    "content_hash": content_hash,
                }
                fetched_rows.append(row)
                if (url and url in existing_urls) or (not url and content_hash in existing_hashes):
                    continue
                if url:
                    existing_urls.add(url)
                existing_hashes.add(content_hash)
                rows.append(row)
                source_rows.append(row)
            summary["fetched_entries"] += len(result.entries)
            completeness = _completeness(fetched_rows)
            updated = _status_after_success(current_health, fetched_rows, now)
            updated["source_id"] = source_id
            _upsert_health(store, updated)
            summary["sources"].append({"source_id": source_id, "status": updated["status"], "entries": len(result.entries), "new_rows": len(source_rows), "field_completeness": completeness})

        df = pd.DataFrame(rows, columns=RSS_NEWS_COLUMNS)
        summary["new_rows"] = _write_dataframe(store, df)
        summary["samples"] = rows[:10]

        report_root = resolve_report_root(settings)
        report_root.mkdir(parents=True, exist_ok=True)
        health_rows = [dict(zip(HEALTH_COLUMNS, row)) for row in store.execute(f"SELECT {', '.join(HEALTH_COLUMNS)} FROM rss_news_source_health ORDER BY source_id").fetchall()]
        ingest_text, health_text, fix_text = _report_lines(summary, health_rows)
        (report_root / "rss_news_ingest_report.md").write_text(ingest_text, encoding="utf-8")
        (report_root / "rss_news_source_health.md").write_text(health_text, encoding="utf-8")
        fix_path = report_root / "rss_news_fix_prompt.md"
        if fix_text:
            fix_path.write_text(fix_text, encoding="utf-8")
        elif fix_path.exists():
            fix_path.unlink()
        return summary
    finally:
        store.close()


def fetch_rss_news() -> pd.DataFrame:
    summary = run_ingest()
    if not summary.get("samples"):
        return pd.DataFrame(columns=RSS_NEWS_COLUMNS)
    return pd.DataFrame(summary["samples"], columns=RSS_NEWS_COLUMNS)
