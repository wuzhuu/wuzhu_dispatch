"""Work directory cleanup for task artifact directories.

Provides safe, configurable cleanup of ``work_dir/<task_id>/`` directories
created by ShellExecutor.  Hermes workspace directories are **never** touched
— only top-level subdirectories of ``work_dir`` whose names match a safe
``task_id`` pattern are eligible for removal.

Policy is driven by :class:`CleanupConfig` from ``node.yaml``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)

# ── Safe task_id pattern — only allow safe characters ─────────────
# UUIDs, simple slugs, dotted versions:  abc123, task.1, test_run-3
_SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# Explicitly block path traversal: ".." , "." , "..."
_TRAVERSAL_NAMES = frozenset({".", "..", "..."})


def is_safe_child_path(base_dir: str | Path, path: str | Path) -> bool:
    """Check if *path* is a direct child of *base_dir* (safe path).

    Resolves both paths and verifies that *path* is inside *base_dir*.
    This prevents directory traversal attacks (e.g. ``../../etc``).

    Returns ``True`` only when the resolved *path* strictly starts with
    the resolved *base_dir* as an ancestor.
    """
    try:
        base = Path(base_dir).resolve()
        target = Path(path).resolve()
        target.relative_to(base)
        return True
    except (ValueError, RuntimeError, OSError):
        return False


def is_valid_task_dir_name(name: str) -> bool:
    """Check if *name* is a safe task directory name.

    Only allows ``[A-Za-z0-9_.-]`` — blocks path traversal components
    like ``..``, empty string, or slash.  Also blocks bare dot names
    (``.``, ``..``, ``...``) which are filesystem special entries.
    """
    if not name:
        return False
    if name in _TRAVERSAL_NAMES:
        return False
    return bool(_SAFE_TASK_ID_RE.match(name))


def get_task_status_dir(work_dir: str, task_id: str) -> str:
    """Return the path ``work_dir/<task_id>/``.

    Does **not** check disk existence — just returns the path string.
    """
    return os.path.join(work_dir, task_id)


def is_task_dir(work_dir: str, dir_path: str | Path) -> bool:
    """Check if *dir_path* is a plausible task subdirectory of *work_dir*.

    - Must be under *work_dir*
    - Name must match :func:`is_valid_task_dir_name`
    - Must be a directory
    """
    if not is_safe_child_path(work_dir, dir_path):
        return False
    name = Path(dir_path).name
    return is_valid_task_dir_name(name)


def cleanup_task_dir(work_dir: str, task_id: str, reason: str = "cleanup") -> bool:
    """Remove the task directory ``work_dir/<task_id>/`` if it exists.

    Returns ``True`` if the directory was removed, ``False`` if it did
    not exist or if safety checks failed.

    Safety checks:
    - ``task_id`` must match :func:`is_valid_task_dir_name`
    - The resulting path must be under *work_dir* (path traversal guard)
    - The directory must not be an allowed Hermes workspace
    """
    if not is_valid_task_dir_name(task_id):
        logger.warning("Cleanup refused: invalid task_id %r", task_id)
        return False

    task_dir = os.path.join(work_dir, task_id)

    if not is_safe_child_path(work_dir, task_dir):
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


def _get_eligible_task_dirs(
    work_dir: str,
    cleanup_cfg: "CleanupConfig",  # type: ignore[name-defined]  # noqa: F821
    allowed_workspaces: list[str] | None = None,
) -> list[dict]:
    """Scan *work_dir* for task subdirectories and determine eligibility.

    Returns a list of dicts::

        {
            "path": Path,
            "name": str,                    # task_id
            "mtime": float,                 # last modified time
            "age_seconds": float,
        }

    Only returns directories that are candidate for cleanup (not Hermes
    workspaces, not the work_dir itself, valid task names).
    """
    allowed_workspaces = allowed_workspaces or []
    allowed_resolved: Set[Path] = set()
    for ws in allowed_workspaces:
        try:
            allowed_resolved.add(Path(ws).expanduser().resolve())
        except Exception:
            pass

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

        # Exclude Hermes workspaces that happen to be under work_dir
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

        entries.append({
            "path": entry,
            "name": name,
            "mtime": mtime,
            "age_seconds": time.time() - mtime,
        })

    return entries


def cleanup_expired_task_dirs(
    work_dir: str,
    cleanup_cfg: "CleanupConfig",  # type: ignore[name-defined]  # noqa: F821
    allowed_workspaces: list[str] | None = None,
    task_outcomes: dict[str, dict] | None = None,
) -> tuple[int, int]:
    """Scan *work_dir* and remove expired task directories.

    Returns ``(removed_count, remaining_count)``.

    Cleanup logic:
    1. For each task dir, check if we have a recorded outcome (status +
       finish_time) from *task_outcomes*.  If available, apply the
       corresponding retention policy per status.
    2. Without an outcome record, use the **most conservative** (longest)
       retention across all enabled categories to avoid deleting dirs
       whose status is unknown.
    3. If total work_dir size exceeds ``max_work_dir_size_mb``, the
       oldest directories are evicted even if within their retention
       window.
    """
    if not cleanup_cfg.enabled:
        logger.debug("Cleanup disabled — skipping")
        return (0, 0)

    task_outcomes = task_outcomes or {}
    allowed_workspaces = allowed_workspaces or []
    entries = _get_eligible_task_dirs(work_dir, cleanup_cfg, allowed_workspaces)

    if not entries:
        return (0, 0)

    removed = 0

    # Sort by mtime ascending (oldest first)
    entries.sort(key=lambda e: e["mtime"])

    for entry in entries:
        name = entry["name"]
        age = entry["age_seconds"]

        if should_delete_task_dir(name, age, cleanup_cfg, task_outcomes):
            if cleanup_task_dir(work_dir, name, reason="expired"):
                removed += 1

    # Size-based eviction (only if enabled and after time-based removal)
    if cleanup_cfg.max_work_dir_size_mb > 0:
        current_size_mb = get_work_dir_size(work_dir)
        max_bytes = cleanup_cfg.max_work_dir_size_mb * 1024 * 1024

        if current_size_mb * 1024 * 1024 > max_bytes:
            # Re-scan to get still-present directories
            remaining = _get_eligible_task_dirs(work_dir, cleanup_cfg, allowed_workspaces)
            remaining.sort(key=lambda e: e["mtime"])  # oldest first

            for entry in remaining:
                if current_size_mb * 1024 * 1024 <= max_bytes:
                    break
                # Do not delete very recent (< 60s) dirs to avoid races
                if entry["age_seconds"] < 60:
                    continue
                if cleanup_task_dir(work_dir, entry["name"], reason="size-evict"):
                    removed += 1
                    dir_size_mb = _dir_size_mb(entry["path"])
                    current_size_mb -= dir_size_mb

    # Delete empty leftover directories if configured
    if cleanup_cfg.delete_empty_dirs and removed > 0:
        _remove_empty_subdirs(work_dir)

    remaining = len(_get_eligible_task_dirs(work_dir, cleanup_cfg, allowed_workspaces))
    return (removed, remaining)


def should_delete_task_dir(
    name: str,
    age_seconds: float,
    cleanup_cfg: "CleanupConfig",  # type: ignore[name-defined]  # noqa: F821
    task_outcomes: dict[str, dict] | None = None,
) -> bool:
    """Decide whether a task directory should be deleted.

    Uses recorded *task_outcomes* when available; otherwise falls back
    to **all** active cleanup categories (safe default).
    """
    task_outcomes = task_outcomes or {}
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
        # Status not eligible for cleanup — keep
        return False

    # No outcome record — use the most conservative (longest) retention
    # across enabled categories
    max_retention = 0
    if cleanup_cfg.cleanup_success:
        max_retention = max(max_retention, cleanup_cfg.keep_success_seconds)
    if cleanup_cfg.cleanup_failed:
        max_retention = max(max_retention, cleanup_cfg.keep_failed_seconds)
    if cleanup_cfg.cleanup_timeout:
        max_retention = max(max_retention, cleanup_cfg.keep_timeout_seconds)

    if max_retention > 0:
        return age_seconds >= max_retention

    # No cleanup categories active — never delete from time-based rules
    return False


def _dir_size_mb(path: Path) -> float:
    """Calculate the size of a directory in MB (approximate)."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total / (1024 * 1024)


def _remove_empty_subdirs(work_dir: str):
    """Remove empty subdirectories of *work_dir* (one level only)."""
    base = Path(work_dir).resolve()
    if not base.is_dir():
        return
    for entry in base.iterdir():
        if entry.is_dir() and is_valid_task_dir_name(entry.name):
            try:
                if not any(entry.iterdir()):  # empty
                    entry.rmdir()
                    logger.debug("Removed empty directory %s", entry.name)
            except OSError:
                pass


def get_work_dir_size(work_dir: str) -> float:
    """Get total size of *work_dir* in MB.

    This includes **all** contents (task dirs, stray files, etc).
    """
    base = Path(work_dir).resolve()
    if not base.is_dir():
        return 0.0
    return _dir_size_mb(base)
