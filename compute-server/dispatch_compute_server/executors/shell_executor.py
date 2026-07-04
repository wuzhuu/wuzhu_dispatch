"""Shell executor — runs shell commands in a restricted work_dir.

Security:
- Work directory is always under config.work_dir/<task_id>.
- Subprocesses run in a dedicated process group so we can kill the entire tree.
- Timeout kills the whole process group.
"""

import asyncio
import logging
import os
import signal
from typing import Any

logger = logging.getLogger(__name__)


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

        # Restrict execution to a task-specific subdirectory of work_dir
        task_id = task.get("task_id", "unknown")
        safe_dir = os.path.join(work_dir, task_id)
        os.makedirs(safe_dir, exist_ok=True)

        timeout = task.get("timeout_seconds", 3600)

        logger.info("Shell task %s: running %r in %s (timeout=%ds)", task_id, command, safe_dir, timeout)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=safe_dir,
                preexec_fn=os.setsid,  # Create new process group
                env={
                    **os.environ,
                    # Strip sensitive env vars
                    "DISPATCH_SERVER_SECRET": "",
                    "MYSQL_PASSWORD": "",
                    "REGISTRATION_TOKEN": "",
                },
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
                    # If process group doesn't have the right permissions,
                    # kill the process directly
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                return {
                    "success": False,
                    "error": f"Command timed out after {timeout}s (process group killed)",
                    "traceback": "",
                }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to start subprocess: {exc}",
                "traceback": "",
            }

        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        # Truncate excessively large output
        MAX_OUTPUT = 100_000  # 100KB
        if len(stdout_str) > MAX_OUTPUT:
            stdout_str = stdout_str[:MAX_OUTPUT] + f"\n... [truncated {len(stdout_str)} chars]"
        if len(stderr_str) > MAX_OUTPUT:
            stderr_str = stderr_str[:MAX_OUTPUT] + f"\n... [truncated {len(stderr_str)} chars]"

        output = {"stdout": stdout_str, "stderr": stderr_str, "exit_code": proc.returncode or 0}

        if proc.returncode == 0:
            return {"success": True, "output": output, "task_work_dir": safe_dir}
        else:
            return {
                "success": False,
                "error": f"Command failed (exit {proc.returncode}): {stderr_str[:2000]}",
                "traceback": stderr_str,
                "task_work_dir": safe_dir,
            }
