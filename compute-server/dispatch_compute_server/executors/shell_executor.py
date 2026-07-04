"""Shell executor — runs shell commands in a restricted task work directory.

Directory layout for each task::

    <work_dir>/tasks/<task_id>/
      work/           # cwd for the subprocess
      tmp/            # TMPDIR/TEMP/TMP
      artifacts/      # result files the task wants to preserve
      logs/           # local task log cache (optional)
      meta.json       # task metadata

Security:
- The ``work_dir`` is always under ``<work_dir>/tasks/<task_id>/work/``.
- ``task_id`` is strictly validated before being used as a directory name.
- Subprocesses run in a dedicated process group for clean kill on timeout.
- Sensitive environment variables are stripped before execution.
- ``TMPDIR`` / ``TEMP`` / ``TMP`` point to the task's private ``tmp/`` dir.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..cleanup import (
    is_valid_task_dir_name,
    task_root_dir,
    task_work_dir,
    task_tmp_dir,
    task_artifact_dir,
    task_logs_dir,
    write_task_meta,
    update_task_meta_status,
)

logger = logging.getLogger(__name__)

# Sensitive env vars stripped from subprocess environment
_STRIPPED_ENV_VARS = frozenset({
    "DISPATCH_SERVER_SECRET",
    "REGISTRATION_TOKEN",
    "MYSQL_PASSWORD",
    "DATABASE_URL",
    "SESSION_SECRET",
    "CLIENT_API_TOKEN",
    "AGENT_TOKEN",
})


def _build_task_env(task_id: str, task_tmp: str) -> dict[str, str]:
    """Build a sanitised environment for a subprocess.

    Strips sensitive vars and adds task-local temp directories.
    """
    env = dict(os.environ)
    for var in _STRIPPED_ENV_VARS:
        env.pop(var, None)
    env["TMPDIR"] = task_tmp
    env["TEMP"] = task_tmp
    env["TMP"] = task_tmp
    env["WUZHU_TASK_ID"] = task_id
    return env


def _compute_disk_usage(path: str) -> int:
    """Compute disk usage of *path* in bytes (fast approximate)."""
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.lstat(os.path.join(dirpath, f)).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


class ShellExecutor:
    """Execute a shell command as a subprocess with process-group cleanup."""

    async def run(
        self,
        task: dict[str, Any],
        execution: dict[str, Any],
        work_dir: str,
        log_dir: str,
    ) -> dict[str, Any]:
        command = execution.get("command", "")
        if not command:
            return {"success": False, "error": "No command specified in execution payload"}

        task_id = task.get("task_id", "unknown")
        if not is_valid_task_dir_name(task_id):
            return {
                "success": False,
                "error": f"Invalid task_id {task_id!r}: rejected for safety",
            }

        mode = task.get("payload", {}).get("execution", {}).get("mode", "shell")
        timeout = task.get("timeout_seconds", 3600)

        # ── Create task directory tree ────────────────────────────────
        task_root = task_root_dir(work_dir, task_id)
        cwd = task_work_dir(work_dir, task_id)
        tmpd = task_tmp_dir(work_dir, task_id)
        artifact_dir = task_artifact_dir(work_dir, task_id)
        logs_dir = task_logs_dir(work_dir, task_id)

        for d in (cwd, tmpd, artifact_dir, logs_dir):
            os.makedirs(d, exist_ok=True)

        # ── Write meta.json ───────────────────────────────────────────
        started_at = datetime.now(timezone.utc).isoformat()
        write_task_meta(
            work_dir=work_dir,
            task_id=task_id,
            status="running",
            execution_mode=mode,
            started_at=started_at,
        )

        logger.info(
            "Shell task %s: running %r in %s (timeout=%ds)",
            task_id, command, cwd, timeout,
        )

        # ── Build sanitised environment ───────────────────────────────
        env = _build_task_env(task_id, tmpd)

        # ── Execute ──────────────────────────────────────────────────
        start_time = time.time()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                preexec_fn=os.setsid,  # Create new process group
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                # Kill the entire process group
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

                update_task_meta_status(work_dir, task_id, "timeout")
                elapsed = time.time() - start_time

                return {
                    "success": False,
                    "error": f"Command timed out after {timeout}s (process group killed)",
                    "traceback": "",
                    "elapsed_seconds": round(elapsed, 1),
                    "task_root": task_root,
                    "task_work_dir": cwd,
                    "task_artifact_dir": artifact_dir,
                    "disk_usage_bytes": _compute_disk_usage(task_root),
                }
        except Exception as exc:
            update_task_meta_status(work_dir, task_id, "failed")
            return {
                "success": False,
                "error": f"Failed to start subprocess: {exc}",
                "traceback": "",
                "elapsed_seconds": round(time.time() - start_time, 1),
                "task_root": task_root,
                "task_work_dir": cwd,
                "task_artifact_dir": artifact_dir,
                "disk_usage_bytes": _compute_disk_usage(task_root),
            }

        elapsed = time.time() - start_time
        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        # Truncate excessively large output
        MAX_OUTPUT = 100_000  # 100KB
        if len(stdout_str) > MAX_OUTPUT:
            stdout_str = stdout_str[:MAX_OUTPUT] + f"\n... [truncated {len(stdout_str)} chars]"
        if len(stderr_str) > MAX_OUTPUT:
            stderr_str = stderr_str[:MAX_OUTPUT] + f"\n... [truncated {len(stderr_str)} chars]"

        output = {"stdout": stdout_str, "stderr": stderr_str, "exit_code": proc.returncode or 0}

        disk_usage = _compute_disk_usage(task_root)

        if proc.returncode == 0:
            update_task_meta_status(work_dir, task_id, "success")
            return {
                "success": True,
                "output": output,
                "elapsed_seconds": round(elapsed, 1),
                "task_root": task_root,
                "task_work_dir": cwd,
                "task_artifact_dir": artifact_dir,
                "disk_usage_bytes": disk_usage,
            }
        else:
            update_task_meta_status(work_dir, task_id, "failed")
            return {
                "success": False,
                "error": f"Command failed (exit {proc.returncode}): {stderr_str[:2000]}",
                "traceback": stderr_str,
                "elapsed_seconds": round(elapsed, 1),
                "task_root": task_root,
                "task_work_dir": cwd,
                "task_artifact_dir": artifact_dir,
                "disk_usage_bytes": disk_usage,
            }
