"""Executors dispatcher — routes task payloads to the right executor."""

from __future__ import annotations

import logging
from typing import Any

from .executors.shell_executor import ShellExecutor
from .executors.hermes_executor import HermesExecutor
from .executors.docker_executor import DockerExecutor

logger = logging.getLogger(__name__)

_executor_cache: dict[str, Any] = {}


def get_executor(mode: str, config: Any = None):
    """Return an executor instance for the given mode string."""
    cache_key = mode
    if cache_key in _executor_cache:
        return _executor_cache[cache_key]

    if mode == "shell":
        cls = ShellExecutor()
    elif mode == "hermes":
        allowed = list(config.allowed_hermes_workspaces) if config else []
        hermes_bin = config.hermes_bin if config else "hermes"
        cls = HermesExecutor(allowed_workspaces=allowed, hermes_bin=hermes_bin)
    elif mode == "docker":
        cls = DockerExecutor()
    else:
        raise ValueError(f"Unknown execution mode: {mode!r}. Supported: shell, hermes, docker")

    _executor_cache[cache_key] = cls
    return cls


async def execute_task(task: dict[str, Any], work_dir: str, log_dir: str,
                       config: Any = None) -> dict[str, Any]:
    """Run a task and return its result dict.

    Returns ``{"success": True, "output": ...}`` or
    ``{"success": False, "error": ..., "traceback": ...}``.
    """
    payload = task.get("payload", {})
    execution = payload.get("execution", {})

    mode = execution.get("mode", payload.get("mode", "shell"))

    try:
        executor = get_executor(mode, config)
    except ValueError as exc:
        return {"success": False, "error": str(exc), "traceback": ""}

    logger.info("Executing task %s with mode=%s", task.get("task_id", "?"), mode)
    try:
        result = await executor.run(task, execution, work_dir, log_dir)
        return result
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        logger.error("Executor failed for task %s: %s", task.get("task_id", "?"), exc)
        return {"success": False, "error": str(exc), "traceback": tb}
