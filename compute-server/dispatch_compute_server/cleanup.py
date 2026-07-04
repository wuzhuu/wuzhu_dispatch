"""Work directory cleanup for task artifact directories.

Directory model::

    <work_dir>/
      tasks/
        <task_id>/
          work/           # shell cwd — execution happens here
          tmp/            # TMPDIR/TEMP/TMP for the task
          artifacts/      # result files the task wants to preserve
          logs/           # local task log cache (optional)
          meta.json       # task metadata for cleanup decisions
      cache/              # global cache (never auto-cleaned)
      quarantine/         # isolated suspicious files (optional)

Cleanup only scans ``<work_dir>/tasks/`` — everything else is excluded.
Hermes workspace directories are **never** touched.

Policy is driven by :class:`CleanupConfig` from ``node.yaml``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Set

logger = logging.getLogger(__name__)

# ── Safe task_id pattern ─────────────────────────────────────────
# UUIDs, simple slugs, dotted versions:  abc123, task.1, test_run-3
_SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_TRAVERSAL_NAMES = frozenset({".", "..", "..."})

# ── Subdirectory constants ───────────────────────────────────────
TASKS_DIR = "tasks"
WORK_DIR_SUB = "work"
TMP_DIR_SUB = "tmp"
ARTIFACT_DIR_SUB = "artifacts"
LOGS_DIR_SUB = "logs"
META_FILE = "meta.json"

# ── Orphan threshold — dirs without meta.json older than this are deleted ──
ORPHAN_MAX_AGE = 7 * 86400  # 7 days

# ── Path safety ──────────────────────────────────────────────────


def resolve_under(base: str | Path, child: str | Path) -> Path:
    """Resolve *child* relative to *base* and confirm it's underneath.

    Raises ``ValueError`` if *child* is not inside *base*.
    This prevents path traversal attacks (e.g. ``../../etc``).
    """
    base_resolved = Path(base).resolve()
    child_resolved = Path(child).resolve()
    child_resolved.relative_to(base_resolved)
    return child_resolved


def is_safe_child_path(base_dir: str | Path, path: str | Path) -> bool:
    """Check if *path* is a safe child of *base_dir*."""
    try:
        resolve_under(base_dir, path)
        return True
    except (ValueError, RuntimeError, OSError):
        return False


def is_valid_task_dir_name(name: str) -> bool:
    """Check if *name* is a safe task directory name.

    Only allows ``[A-Za-z0-9_.-]`` (max 128 chars).
    Blocks ``..``, ``.``, ``...``, empty, slash, null byte.
    """
    if not name or name in _TRAVERSAL_NAMES:
        return False
    return bool(_SAFE_TASK_ID_RE.match(name))


# ── Task directory helpers ───────────────────────────────────────


def task_root_dir(work_dir: str, task_id: str) -> str:
    """Return ``<work_dir>/tasks/<task_id>``."""
    return os.path.join(work_dir, TASKS_DIR, task_id)


def task_work_dir(work_dir: str, task_id: str) -> str:
    """Return ``<work_dir>/tasks/<task_id>/work``."""
    return os.path.join(work_dir, TASKS_DIR, task_id, WORK_DIR_SUB)


def task_tmp_dir(work_dir: str, task_id: str) -> str:
    """Return ``<work_dir>/tasks/<task_id>/tmp``."""
    return os.path.join(work_dir, TASKS_DIR, task_id, TMP_DIR_SUB)


def task_artifact_dir(work_dir: str, task_id: str) -> str:
    """Return ``<work_dir>/tasks/<task_id>/artifacts``."""
    return os.path.join(work_dir, TASKS_DIR, task_id, ARTIFACT_DIR_SUB)


def task_logs_dir(work_dir: str, task_id: str) -> str:
    """Return ``<work_dir>/tasks/<task_id>/logs``."""
    return os.path.join(work_dir, TASKS_DIR, task_id, LOGS_DIR_SUB)


def task_meta_path(work_dir: str, task_id: str) -> str:
    """Return ``<work_dir>/tasks/<task_id>/meta.json``."""
    return os.path.join(work_dir, TASKS_DIR, task_id, META_FILE)


# ── Meta.json I/O ────────────────────────────────────────────────


def write_task_meta(
    work_dir: str,
    task_id: str,
    status: str = "running",
    execution_mode: str = "shell",
    cleanup_after: float | None = None,
    started_at: str | None = None,
):
    """Write or update ``meta.json`` for a task.

    If the file already exists, it is updated (not overwritten) so
    that ``created_at`` is preserved.
    """
    path = Path(task_meta_path(work_dir, task_id))
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    now_iso = datetime.now(timezone.utc).isoformat()
    meta: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "created_at": existing.get("created_at", now_iso),
        "started_at": started_at or existing.get("started_at", now_iso),
        "finished_at": None,
        "execution_mode": execution_mode,
        "cleanup_after": cleanup_after,
        "task_root": task_root_dir(work_dir, task_id),
        "task_work_dir": task_work_dir(work_dir, task_id),
        "task_tmp_dir": task_tmp_dir(work_dir, task_id),
        "task_artifact_dir": task_artifact_dir(work_dir, task_id),
    }
    if status != "running":
        meta["finished_at"] = existing.get("finished_at", now_iso)
        if cleanup_after is not None:
            meta["cleanup_after"] = cleanup_after

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")


def read_task_meta(work_dir: str, task_id: str) -> dict[str, Any] | None:
    """Read ``meta.json`` for a task, returning ``None`` if missing/corrupt."""
    path = Path(task_meta_path(work_dir, task_id))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def update_task_meta_status(
    work_dir: str,
    task_id: str,
    status: str,
    cleanup_after: float | None = None,
):
    """Update the status (and optionally cleanup_after) in meta.json."""
    meta = read_task_meta(work_dir, task_id) or {
        "task_id": task_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "task_root": task_root_dir(work_dir, task_id),
    }
    meta["status"] = status
    meta["finished_at"] = datetime.now(timezone.utc).isoformat()
    if cleanup_after is not None:
        meta["cleanup_after"] = cleanup_after

    path = Path(task_meta_path(work_dir, task_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")


# ── Core cleanup operations ──────────────────────────────────────


def cleanup_task_dir(work_dir: str, task_id: str, reason: str = "cleanup") -> bool:
    """Remove ``<work_dir>/tasks/<task_id>/`` if it exists.

    Safety checks:
    - ``task_id`` must match :func:`is_valid_task_dir_name`
    - The resulting path must be under ``<work_dir>/tasks/``
    """
    if not is_valid_task_dir_name(task_id):
        logger.warning("Cleanup refused: invalid task_id %r", task_id)
        return False

    task_dir = task_root_dir(work_dir, task_id)

    if not is_safe_child_path(os.path.join(work_dir, TASKS_DIR), task_dir):
        logger.warning("Cleanup refused: path traversal %r", task_dir)
        return False

    if not os.path.isdir(task_dir):
        return False

    try:
        shutil.rmtree(task_dir)
        logger.info("Cleaned up task directory %s (reason=%s)", task_id, reason)
        return True
    except OSError as exc:
        logger.error("Failed to remove task directory %s: %s", task_id, exc)
        return False


# ── Scanning ─────────────────────────────────────────────────────


def _get_tasks_base(work_dir: str) -> Path:
    """Return the resolved ``<work_dir>/tasks/`` path."""
    return Path(os.path.join(work_dir, TASKS_DIR)).resolve()


def _scan_task_dirs(
    work_dir: str,
    allowed_workspaces: list[str] | None = None,
    exclude_running: set[str] | None = None,
) -> list[dict]:
    """Scan ``<work_dir>/tasks/`` for task subdirectories.

    Returns a list of dicts::

        {
            "path": Path,
            "name": str,          # task_id
            "mtime": float,
            "age_seconds": float,
            "meta": dict | None,  # parsed meta.json or None
            "has_meta": bool,
        }

    Only returns directories that match :func:`is_valid_task_dir_name`.
    Excludes ``allowed_workspaces`` Hermes dirs.
    """
    allowed_resolved: Set[Path] = set()
    for ws in allowed_workspaces or []:
        try:
            allowed_resolved.add(Path(ws).expanduser().resolve())
        except Exception:
            pass

    exclude_running = exclude_running or set()
    tasks_dir = _get_tasks_base(work_dir)

    if not tasks_dir.is_dir():
        return []

    entries: list[dict] = []
    for entry in tasks_dir.iterdir():
        if not entry.is_dir():
            continue

        name = entry.name
        if not is_valid_task_dir_name(name):
            continue
        if name in exclude_running:
            continue

        # Exclude Hermes workspaces that happen to be under tasks/
        try:
            resolved = entry.resolve()
        except OSError:
            continue
        if resolved in allowed_resolved:
            continue

        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue

        meta = read_task_meta(work_dir, name)

        entries.append({
            "path": entry,
            "name": name,
            "mtime": mtime,
            "age_seconds": time.time() - mtime,
            "meta": meta,
            "has_meta": meta is not None,
        })

    return entries


def _scan_legacy_dirs(
    work_dir: str,
    exclude_running: set[str] | None = None,
) -> list[dict]:
    """Scan ``<work_dir>/`` for legacy flat-layout task directories.

    Only used when ``legacy_cleanup`` is enabled.  Returns entries
    for directories directly under ``work_dir`` that match the
    task_id pattern.
    """
    exclude_running = exclude_running or set()
    base = Path(work_dir).resolve()
    if not base.is_dir():
        return []

    entries: list[dict] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if not is_valid_task_dir_name(name):
            continue
        if name in exclude_running:
            continue

        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue

        entries.append({
            "path": entry,
            "name": name,
            "mtime": mtime,
            "age_seconds": time.time() - mtime,
            "meta": None,
            "has_meta": False,
        })

    return entries


# ── Cleanup logic ────────────────────────────────────────────────


def should_delete_entry(
    entry: dict,
    cleanup_cfg: "CleanupConfig",  # type: ignore[name-defined]  # noqa: F821
    task_outcomes: dict[str, dict] | None = None,
) -> bool:
    """Decide whether a task directory entry should be deleted.

    Decision priority:
    1. If ``meta.json`` exists, read ``status`` and ``cleanup_after``.
    2. If not, check *task_outcomes* (in-memory tracker from main.py).
    3. If neither, treat as orphan (delete if older than 7 days).
    """
    name = entry["name"]
    age = entry["age_seconds"]
    meta = entry.get("meta")
    task_outcomes = task_outcomes or {}

    # ── Priority 1: meta.json
    if meta:
        status = meta.get("status", "unknown")
        cleanup_after = meta.get("cleanup_after")

        if cleanup_after is not None:
            # Explicit cleanup-after timestamp
            return time.time() >= cleanup_after

        if status == "success" and cleanup_cfg.cleanup_success:
            return age >= cleanup_cfg.keep_success_seconds
        elif status == "failed" and cleanup_cfg.cleanup_failed:
            return age >= cleanup_cfg.keep_failed_seconds
        elif status == "timeout" and cleanup_cfg.cleanup_timeout:
            return age >= cleanup_cfg.keep_timeout_seconds
        elif status == "cancelled" and cleanup_cfg.cleanup_cancelled:
            return age >= cleanup_cfg.keep_cancelled_seconds
        # Unknown status or not configured for cleanup — keep
        return False

    # ── Priority 2: in-memory outcomes (from main.py)
    outcome = task_outcomes.get(name)
    if outcome:
        status = outcome.get("status", "success")
        finish_time = outcome.get("finish_time", 0.0)
        dir_age = time.time() - finish_time

        if status == "success" and cleanup_cfg.cleanup_success:
            return dir_age >= cleanup_cfg.keep_success_seconds
        elif status == "failed" and cleanup_cfg.cleanup_failed:
            return dir_age >= cleanup_cfg.keep_failed_seconds
        elif status == "timeout" and cleanup_cfg.cleanup_timeout:
            return dir_age >= cleanup_cfg.keep_timeout_seconds
        elif status == "cancelled" and cleanup_cfg.cleanup_cancelled:
            return dir_age >= cleanup_cfg.keep_cancelled_seconds
        return False

    # ── Priority 3: orphan — no meta, no outcome
    # Use longest active retention as safe-guard
    max_retention = _max_active_retention(cleanup_cfg)
    if max_retention > 0:
        if age >= max_retention:
            return True
    # Even without active categories, delete very old orphans
    return age >= ORPHAN_MAX_AGE


def _max_active_retention(cleanup_cfg: "CleanupConfig") -> int:
    """Return the longest retention across all active categories."""
    ret = 0
    if cleanup_cfg.cleanup_success:
        ret = max(ret, cleanup_cfg.keep_success_seconds)
    if cleanup_cfg.cleanup_failed:
        ret = max(ret, cleanup_cfg.keep_failed_seconds)
    if cleanup_cfg.cleanup_timeout:
        ret = max(ret, cleanup_cfg.keep_timeout_seconds)
    if cleanup_cfg.cleanup_cancelled:
        ret = max(ret, cleanup_cfg.keep_cancelled_seconds)
    return ret


# ── Main cleanup entry point ─────────────────────────────────────


def cleanup_expired_task_dirs(
    work_dir: str,
    cleanup_cfg: "CleanupConfig",  # type: ignore[name-defined]  # noqa: F821
    allowed_workspaces: list[str] | None = None,
    task_outcomes: dict[str, dict] | None = None,
    running_task_ids: set[str] | None = None,
) -> tuple[int, int, dict]:
    """Scan ``<work_dir>/tasks/`` and remove expired task directories.

    Returns ``(removed_count, remaining_count, disk_status)`` where
    ``disk_status`` is a dict that can be embedded in heartbeat metrics::

        {
            "disk_pressure": bool,
            "work_dir_size_mb": float,
            "cleanup_warning": str | None,
        }

    Cleanup logic:
    1. For each task dir with a known outcome (meta.json or in-memory),
       apply the corresponding retention policy.
    2. For orphan dirs (no meta, no outcome), use the longest retention
       or delete if older than 7 days.
    3. Never delete currently running task dirs.
    4. Size-based eviction: if total size > max_work_dir_size_mb, delete
       oldest success/cancelled dirs first.
    5. Legacy mode: optionally scan flat ``<work_dir>/<task_id>`` dirs.
    """
    running_task_ids = running_task_ids or set()

    if not cleanup_cfg.enabled:
        logger.debug("Cleanup disabled — skipping")
        return (0, 0, {"disk_pressure": False, "work_dir_size_mb": 0.0, "cleanup_warning": None})

    allowed_workspaces = allowed_workspaces or []
    task_outcomes = task_outcomes or {}
    removed = 0

    # ── V8.2 layout: scan <work_dir>/tasks/ ──────────────────────
    entries = _scan_task_dirs(work_dir, allowed_workspaces, running_task_ids)
    entries.sort(key=lambda e: e["mtime"])  # oldest first

    for entry in entries:
        if should_delete_entry(entry, cleanup_cfg, task_outcomes):
            if cleanup_task_dir(work_dir, entry["name"], reason="expired"):
                removed += 1

    # ── Legacy layout (optional): scan <work_dir>/ directly ───────
    if cleanup_cfg.legacy_cleanup:
        legacy = _scan_legacy_dirs(work_dir, running_task_ids)
        legacy.sort(key=lambda e: e["mtime"])
        for entry in legacy:
            if should_delete_entry(entry, cleanup_cfg, task_outcomes):
                if cleanup_task_dir(work_dir, entry["name"], reason="legacy-expired"):
                    removed += 1

    # ── Size-based eviction ───────────────────────────────────────
    disk_status = _size_based_eviction(work_dir, cleanup_cfg, allowed_workspaces,
                                       running_task_ids, task_outcomes)
    removed += disk_status.get("evicted", 0)

    # ── Delete empty leftover directories ─────────────────────────
    if cleanup_cfg.delete_empty_dirs:
        _remove_empty_task_dirs(work_dir)

    remaining = len(_scan_task_dirs(work_dir, allowed_workspaces, running_task_ids))
    if cleanup_cfg.legacy_cleanup:
        remaining += len(_scan_legacy_dirs(work_dir, running_task_ids))

    # ── Build disk status ─────────────────────────────────────────
    current_mb = get_work_dir_size(work_dir)
    warning = None
    pressure = False
    if cleanup_cfg.max_work_dir_size_mb > 0 and current_mb > cleanup_cfg.max_work_dir_size_mb:
        pressure = True
        warning = f"work_dir exceeds max_work_dir_size_mb ({current_mb:.0f} > {cleanup_cfg.max_work_dir_size_mb})"

    return (removed, remaining, {
        "disk_pressure": pressure,
        "work_dir_size_mb": round(current_mb, 1),
        "cleanup_warning": warning,
    })


def _size_based_eviction(
    work_dir: str,
    cleanup_cfg: "CleanupConfig",
    allowed_workspaces: list[str] | None,
    running_task_ids: set[str],
    task_outcomes: dict[str, dict],
) -> dict:
    """Delete directories when work_dir exceeds max size.

    Priority order (highest first):
    1. Expired success/cancelled dirs (beyond their retention)
    2. Old success dirs (within retention, but oldest first)
    3. Expired failed/timeout dirs, if those cleanup flags are on
    4. Never delete running task dirs

    Returns ``{"evicted": int}``.
    """
    evicted = 0
    if cleanup_cfg.max_work_dir_size_mb <= 0:
        return {"evicted": 0}

    current_bytes = get_work_dir_size(work_dir) * 1024 * 1024
    max_bytes = cleanup_cfg.max_work_dir_size_mb * 1024 * 1024

    if current_bytes <= max_bytes:
        return {"evicted": 0}

    # Gather eligible dirs
    candidates = _scan_task_dirs(work_dir, allowed_workspaces, running_task_ids)

    # Score each candidate for eviction priority (lower = deleted first)
    def _priority(entry: dict) -> tuple:
        """Return a sort key: (score, mtime).

        Score 0 = expired success/cancelled (delete first)
        Score 1 = old success (within retention)
        Score 2 = expired failed/timeout (if enabled)
        Score 3 = anything else (delete last)
        """
        meta = entry.get("meta")
        status = meta.get("status") if meta else None
        outcome = task_outcomes.get(entry["name"], {})
        ostatus = outcome.get("status")
        effective_status = status or ostatus or "unknown"

        age = entry["age_seconds"]

        if effective_status == "success" and cleanup_cfg.cleanup_success:
            if age >= cleanup_cfg.keep_success_seconds:
                return (0, entry["mtime"])  # expired success
            return (1, entry["mtime"])  # old success (within retention)
        if effective_status == "cancelled" and cleanup_cfg.cleanup_cancelled:
            if age >= cleanup_cfg.keep_cancelled_seconds:
                return (0, entry["mtime"])
            return (1, entry["mtime"])
        if effective_status == "failed" and cleanup_cfg.cleanup_failed:
            if age >= cleanup_cfg.keep_failed_seconds:
                return (2, entry["mtime"])  # expired failed
            return (3, entry["mtime"])  # keep
        if effective_status == "timeout" and cleanup_cfg.cleanup_timeout:
            if age >= cleanup_cfg.keep_timeout_seconds:
                return (2, entry["mtime"])
            return (3, entry["mtime"])

        return (3, entry["mtime"])

    candidates.sort(key=_priority)

    for entry in candidates:
        if current_bytes <= max_bytes:
            break
        if entry["age_seconds"] < 60:
            continue  # don't delete very recent dirs
        if entry["name"] in running_task_ids:
            continue  # never delete running

        # Only delete statuses whose cleanup flag is enabled.
        # This ensures that when cleanup_failed=False, failed dirs are
        # NOT deleted even under disk pressure.
        meta = entry.get("meta")
        status = meta.get("status") if meta else None
        outcome = task_outcomes.get(entry["name"], {})
        ostatus = outcome.get("status")
        effective_status = status or ostatus

        if effective_status == "success" and not cleanup_cfg.cleanup_success:
            continue
        if effective_status == "failed" and not cleanup_cfg.cleanup_failed:
            continue
        if effective_status == "timeout" and not cleanup_cfg.cleanup_timeout:
            continue
        if effective_status == "cancelled" and not cleanup_cfg.cleanup_cancelled:
            continue
        if effective_status is None or effective_status == "unknown":
            continue  # skip unknown for size eviction

        # Compute size BEFORE deletion, then delete
        dir_bytes = _dir_size_bytes(entry["path"])
        if cleanup_task_dir(work_dir, entry["name"], reason="size-evict"):
            evicted += 1
            current_bytes -= dir_bytes
            if current_bytes < 0:
                current_bytes = 0

    return {"evicted": evicted}


def _remove_empty_task_dirs(work_dir: str):
    """Remove empty task root directories under ``<work_dir>/tasks/``."""
    tasks_dir = _get_tasks_base(work_dir)
    if not tasks_dir.is_dir():
        return
    for entry in tasks_dir.iterdir():
        if entry.is_dir() and is_valid_task_dir_name(entry.name):
            try:
                if not any(entry.iterdir()):
                    entry.rmdir()
                    logger.debug("Removed empty task dir %s", entry.name)
            except OSError:
                pass


# ── Size utilities ───────────────────────────────────────────────


def _dir_size_mb(path: Path) -> float:
    """Calculate the size of a directory in MB (approximate)."""
    return _dir_size_bytes(path) / (1024 * 1024)


def _dir_size_bytes(path: Path) -> int:
    """Calculate the size of a directory in bytes (approximate)."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total


def get_work_dir_size(work_dir: str) -> float:
    """Get total size of *work_dir* in MB (all contents)."""
    base = Path(work_dir).resolve()
    if not base.is_dir():
        return 0.0
    return _dir_size_mb(base)


def get_tasks_dir_size(work_dir: str) -> float:
    """Get total size of ``<work_dir>/tasks/`` in MB."""
    tasks_dir = _get_tasks_base(work_dir)
    if not tasks_dir.is_dir():
        return 0.0
    return _dir_size_mb(tasks_dir)


# ── Legacy helper — check if a path is an old-style flat task dir ─


def is_legacy_flat_task_dir(work_dir: str, dir_path: str | Path) -> bool:
    """Check if *dir_path* is an old-style flat task dir under *work_dir*.

    Old layout: ``<work_dir>/<task_id>/`` (flat, no ``tasks/`` subdir).
    """
    if not is_safe_child_path(work_dir, dir_path):
        return False
    name = Path(dir_path).name
    if not is_valid_task_dir_name(name):
        return False
    # If there's a tasks/ dir in work_dir, this is V8.2 layout already
    tasks_dir = _get_tasks_base(work_dir)
    if tasks_dir.is_dir():
        return False
    return True
