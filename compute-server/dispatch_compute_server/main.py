"""dispatch-compute-server: main entry point and task lifecycle loop."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
import traceback as tb_module
from pathlib import Path
from typing import Optional

from .client import ComputeClient
from .config import ComputeServerConfig
from .executor import execute_task
from .metrics import collect_metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] compute-server: %(message)s",
)
logger = logging.getLogger("compute-server")


class RunningTask:
    """Track a task that was pulled and is being executed."""

    def __init__(self, task: dict):
        self.task_id: str = task["task_id"]
        self.task: dict = task
        self.future: Optional[asyncio.Future] = None
        self.start_time: float = time.time()
        self.last_renew: float = 0.0
        self.lease_seconds: int = task.get("lease_seconds", 300)

    def is_done(self) -> bool:
        return self.future is not None and self.future.done()

    def result(self):
        if self.future is None:
            return None
        return self.future.result()


class ComputeServer:
    """Main compute server orchestrator."""

    def __init__(self, config: ComputeServerConfig):
        self.config = config
        self.client = ComputeClient(
            dispatcher_url=config.dispatcher_url,
            node_id=config.node_id,
            agent_token=config.agent_token,
            registration_token=config.registration_token,
        )
        self.running_tasks: dict[str, RunningTask] = {}
        self._shutdown = False

        os.makedirs(config.work_dir, exist_ok=True)
        os.makedirs(config.log_dir, exist_ok=True)

    # ── Lifecycle ─────────────────────────────────────────────────

    def run(self):
        asyncio.run(self._async_run())

    async def _async_run(self):
        # Registration: only attempt if registration_token is configured
        if self.config.registration_token:
            try:
                resp = self.client.register(self.config)
                logger.info("Registered node %s: %s",
                            self.config.node_id, resp.get("message"))
            except Exception:
                logger.exception("Auto-registration failed")
        else:
            logger.info(
                "No registration_token configured — skipping auto-register. "
                "Node %s must be pre-registered by an admin.",
                self.config.node_id,
            )

        last_heartbeat = 0.0
        last_pull = 0.0
        last_renew_check = 0.0

        while not self._shutdown:
            now = time.time()

            if now - last_heartbeat >= self.config.heartbeat_interval:
                await self._do_heartbeat()
                last_heartbeat = now

            if now - last_pull >= self.config.pull_interval:
                await self._do_pull()
                last_pull = now

            if now - last_renew_check >= 30:
                await self._do_renewals()
                last_renew_check = now

            await self._check_tasks()
            await asyncio.sleep(1)

        logger.info("Compute server shutting down.")

    # ── Heartbeat ─────────────────────────────────────────────────

    async def _do_heartbeat(self):
        try:
            metrics = collect_metrics()
            metrics["running_tasks"] = len(self.running_tasks)
            self.client.heartbeat(metrics)
            logger.debug("Heartbeat sent: CPU=%.1f%% Mem=%.1f%% Tasks=%d",
                         metrics["cpu_usage"], metrics["memory_usage"],
                         metrics["running_tasks"])
        except Exception:
            logger.warning("Heartbeat failed", exc_info=True)

    # ── Lease renewal ─────────────────────────────────────────────

    async def _do_renewals(self):
        now = time.time()
        for rt in list(self.running_tasks.values()):
            if rt.is_done():
                continue
            # Use the actual lease_seconds from the pull response
            renew_interval = max(rt.lease_seconds // 2, 30)
            if now - rt.last_renew >= renew_interval:
                try:
                    self.client.renew_task(rt.task_id, lease_seconds=rt.lease_seconds)
                    rt.last_renew = now
                    logger.debug("Renewed lease for task %s", rt.task_id)
                except Exception:
                    logger.warning("Failed to renew lease for task %s", rt.task_id)

    # ── Task pull ─────────────────────────────────────────────────

    async def _do_pull(self):
        profile = self.config.static_profile
        max_parallel = profile.get("limits", {}).get("max_parallel_tasks", 1)
        if len(self.running_tasks) >= max_parallel:
            return

        try:
            task = self.client.pull_task()
        except Exception:
            logger.debug("No task available (pull error)")
            return

        if task is None:
            return

        logger.info("Pulled task %s (type=%s, lease=%ds)",
                     task["task_id"], task["type"], task.get("lease_seconds", 300))

        try:
            mode = task.get("payload", {}).get("execution", {}).get("mode", "unknown")
            self.client.upload_log(task["task_id"], "INFO", f"Task pulled, mode={mode}")
        except Exception:
            pass

        rt = RunningTask(task)
        rt.future = asyncio.ensure_future(self._execute_and_report(rt))
        self.running_tasks[rt.task_id] = rt

    async def _execute_and_report(self, rt: RunningTask) -> dict:
        """Execute the task and report result."""
        task = rt.task
        task_id = task["task_id"]

        try:
            result = await execute_task(task, self.config.work_dir,
                                        self.config.log_dir, self.config)
        except Exception:
            result = {"success": False, "error": "Unexpected executor error",
                      "traceback": tb_module.format_exc()}

        if result["success"]:
            try:
                self.client.upload_log(task_id, "INFO", "Task completed successfully")
                self.client.finish_task(task_id, result.get("output", {}))
                logger.info("Task %s finished successfully", task_id)
            except Exception:
                logger.exception("Failed to upload finish for task %s", task_id)
        else:
            try:
                self.client.upload_log(task_id, "ERROR", result.get("error", ""))
                self.client.fail_task(task_id, result.get("error", ""),
                                      result.get("traceback", ""))
                logger.warning("Task %s failed: %s", task_id,
                               result.get("error", "")[:200])
            except Exception:
                logger.exception("Failed to upload failure for task %s", task_id)

        return result

    # ── Check running tasks ───────────────────────────────────────

    async def _check_tasks(self):
        done_ids = [tid for tid, rt in self.running_tasks.items() if rt.is_done()]
        for tid in done_ids:
            self.running_tasks.pop(tid, None)
            logger.debug("Removed completed task %s from tracking", tid)

        now = time.time()
        for tid, rt in list(self.running_tasks.items()):
            timeout = rt.task.get("timeout_seconds", 3600)
            if rt.future is None and now - rt.start_time > timeout:
                logger.warning("Task %s timed out after %ds (never started)", tid, timeout)
                try:
                    self.client.upload_log(tid, "ERROR",
                                           f"Task timed out after {timeout}s")
                    self.client.fail_task(tid, f"Timed out after {timeout}s",
                                          "Agent-side timeout")
                except Exception:
                    pass
                self.running_tasks.pop(tid, None)


# ── CLI entry point ────────────────────────────────────────────────


def main():
    import argparse
    parser = argparse.ArgumentParser(description="wuzhu-dispatch compute server")
    parser.add_argument("-c", "--config", default="/etc/wuzhu-dispatch/node.yaml",
                        help="Path to node.yaml config file")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    config = ComputeServerConfig.from_yaml(config_path)
    server = ComputeServer(config)
    server.run()


if __name__ == "__main__":
    main()
