"""Hermes executor — runs hermes CLI inside an allowed workspace.

Security:
- workspace must be in the configured allowed_workspaces list.
- hermes_bin comes from agent config, NOT from task payload.
- Uses create_subprocess_exec to avoid shell injection.
- Sensitive env vars are cleared before execution.
- Task root is created for temporary artifacts, but workspace is never
  automatically cleaned up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

from ..cleanup import (
    is_valid_task_dir_name,
    task_root_dir,
    task_work_dir,
    task_tmp_dir,
    task_artifact_dir,
    write_task_meta,
    update_task_meta_status,
)

logger = logging.getLogger(__name__)

# Sensitive env vars stripped from subprocess environment
_STRIPPED_ENV_VARS = frozenset({
    "HERMES_AGENT_TOKEN",
    "DISPATCH_SERVER_SECRET",
    "REGISTRATION_TOKEN",
    "MYSQL_PASSWORD",
    "DATABASE_URL",
    "SESSION_SECRET",
    "CLIENT_API_TOKEN",
    "AGENT_TOKEN",
})


class HermesExecutor:
    """Execute a Hermes agent prompt in a given workspace directory.

    Parameters
    ----------
    allowed_workspaces :
        List of allowed workspace directory prefixes.
        Passed from agent config (allowed_hermes_workspaces).
    hermes_bin :
        Path to the hermes CLI binary (from agent config, NOT task payload).
    """

    def __init__(self, allowed_workspaces: list[str] | None = None,
                 hermes_bin: str = "hermes"):
        self.allowed_workspaces = allowed_workspaces or []
        self.hermes_bin = hermes_bin

    async def run(
        self,
        task: dict[str, Any],
        execution: dict[str, Any],
        work_dir: str,
        log_dir: str,
    ) -> dict[str, Any]:
        workspace = execution.get("workspace", "")
        prompt = execution.get("prompt", "")

        if not workspace:
            return {"success": False, "error": "Hermes mode requires 'workspace' in execution payload"}
        if not prompt:
            return {"success": False, "error": "Hermes mode requires 'prompt' in execution payload"}

        workspace = os.path.expanduser(workspace)
        workspace = os.path.abspath(workspace)

        # Security: verify workspace is in an allowed location
        if not self._is_allowed_workspace(workspace):
            return {
                "success": False,
                "error": (
                    f"Workspace {workspace!r} is not in the allowed list. "
                    f"Configure allowed_hermes_workspaces in node.yaml."
                ),
            }

        if not os.path.isdir(workspace):
            return {"success": False, "error": f"Hermes workspace directory does not exist: {workspace}"}

        task_id = task.get("task_id", "unknown")
        if not is_valid_task_dir_name(task_id):
            return {
                "success": False,
                "error": f"Invalid task_id {task_id!r}: rejected for safety",
            }

        # ── Create task root for temporary files / results ────────────
        t_root = task_root_dir(work_dir, task_id)
        t_tmp = task_tmp_dir(work_dir, task_id)
        t_artifacts = task_artifact_dir(work_dir, task_id)
        os.makedirs(t_tmp, exist_ok=True)
        os.makedirs(t_artifacts, exist_ok=True)

        write_task_meta(
            work_dir=work_dir,
            task_id=task_id,
            status="running",
            execution_mode="hermes",
        )

        # hermes_bin comes from config, NOT from task payload
        hermes_bin = self.hermes_bin
        timeout = task.get("timeout_seconds", 3600)

        logger.info("Hermes task %s: workspace=%s prompt=%r",
                     task["task_id"], workspace, prompt[:120])

        # ── Build sanitised environment with task-local dirs ──────────
        env = dict(os.environ)
        for var in _STRIPPED_ENV_VARS:
            env.pop(var, None)
        env["TMPDIR"] = t_tmp
        env["TEMP"] = t_tmp
        env["TMP"] = t_tmp
        env["WUZHU_TASK_ID"] = task_id
        env["WUZHU_TASK_ROOT"] = t_root
        env["WUZHU_TASK_TMP"] = t_tmp
        env["WUZHU_TASK_ARTIFACTS"] = t_artifacts

        # Use create_subprocess_exec to avoid shell injection
        try:
            proc = await asyncio.create_subprocess_exec(
                hermes_bin, "-q", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
                preexec_fn=os.setsid,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                update_task_meta_status(work_dir, task_id, "timeout")
                return {
                    "success": False,
                    "error": f"Hermes prompt timed out after {timeout}s",
                    "task_root": t_root,
                    "task_work_dir": workspace,
                }
        except FileNotFoundError:
            update_task_meta_status(work_dir, task_id, "failed")
            return {
                "success": False,
                "error": f"Hermes binary {hermes_bin!r} not found on PATH",
                "task_root": t_root,
            }
        except Exception as exc:
            update_task_meta_status(work_dir, task_id, "failed")
            return {
                "success": False,
                "error": f"Hermes execution error: {exc}",
                "task_root": t_root,
            }

        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        # Truncate
        MAX_OUTPUT = 100_000
        if len(stdout_str) > MAX_OUTPUT:
            stdout_str = stdout_str[:MAX_OUTPUT] + f"\n... [truncated {len(stdout_str)} chars]"

        output = {"stdout": stdout_str, "stderr": stderr_str, "exit_code": proc.returncode or 0}

        if proc.returncode == 0:
            update_task_meta_status(work_dir, task_id, "success")
            return {"success": True, "output": output, "task_root": t_root}
        else:
            update_task_meta_status(work_dir, task_id, "failed")
            return {
                "success": False,
                "error": f"Hermes failed (exit {proc.returncode}): {stderr_str[:2000]}",
                "traceback": stderr_str,
                "task_root": t_root,
            }

    def _is_allowed_workspace(self, workspace: str) -> bool:
        """Check if workspace is in the configured allowed list.

        Uses pathlib.Path.resolve() + relative_to() to prevent
        directory traversal attacks.  e.g. /home/hermes/project_evil
        cannot pass via allow rule for /home/hermes/project.
        """
        if not self.allowed_workspaces:
            return False
        from pathlib import Path
        try:
            ws_resolved = Path(workspace).resolve()
        except Exception:
            return False
        for prefix in self.allowed_workspaces:
            try:
                allowed = Path(prefix).expanduser().resolve()
                ws_resolved.relative_to(allowed)
                return True
            except (ValueError, RuntimeError):
                continue
        return False
