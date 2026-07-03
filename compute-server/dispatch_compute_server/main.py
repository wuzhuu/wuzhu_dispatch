"""dispatch-compute-server: main entry point and task lifecycle loop.

Adaptive pull intervals:
- active: tasks running → short interval (default 3 s)
- warm_idle: just finished a task → medium interval (default 10 s)
- cold_idle: long idle → long interval (default 30-60 s)
- error_backoff: network errors → exponential backoff (max 120 s)

Long polling: when idle, uses ``?wait_seconds=25`` so the dispatcher
holds the connection and returns immediately when a task appears.
"""
from __future__ import annotations

import asyncio
import logging
import os
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

    # ── Pull-state constants ──────────────────────────────────────
    STATE_ACTIVE = "active"          # tasks are running
    STATE_WARM_IDLE = "warm_idle"    # just finished a task
    STATE_COLD_IDLE = "cold_idle"    # long idle
    STATE_ERROR_BACKOFF = "error"    # network error backoff

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

        # For running synchronous requests without blocking the event loop
        self._loop = None  # set in _async_run

        # Adaptive pull state
        self._state: str = self.STATE_COLD_IDLE
        self._last_task_finish: float = 0.0
        self._error_backoff_current: float = 1.0
        self._consecutive_errors: int = 0
        self._last_full_metrics: float = 0.0

        os.makedirs(config.work_dir, exist_ok=True)
        os.makedirs(config.log_dir, exist_ok=True)

    # ── Lifecycle ─────────────────────────────────────────────────

    def run(self):
        asyncio.run(self._async_run())

    async def _async_run(self):
        self._loop = asyncio.get_running_loop()

        # Registration
        if self.config.registration_token:
            try:
                resp = await asyncio.to_thread(self.client.register, self.config)
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
        last_state_transition_log = 0.0

        while not self._shutdown:
            now = time.time()

            # ── Heartbeat — lightweight every interval, full metrics on schedule
            if now - last_heartbeat >= self._heartbeat_interval():
                await self._do_heartbeat(full_metrics=self._should_send_full_metrics())
                last_heartbeat = now

            # ── Task pull — adaptive interval + long polling
            if now - last_pull >= self._pull_interval():
                await self._do_pull()
                last_pull = now

            # ── Lease renewal
            if now - last_renew_check >= 30:
                await self._do_renewals()
                last_renew_check = now

            # ── Check running tasks
            await self._check_tasks()

            # ── Log state transitions occasionally
            if now - last_state_transition_log >= 60:
                if self._consecutive_errors > 0:
                    logger.info("State=%s running=%d errors=%d",
                                self._state, len(self.running_tasks), self._consecutive_errors)
                last_state_transition_log = now

            await asyncio.sleep(0.5)

        logger.info("Compute server shutting down.")

    # ── State helpers ─────────────────────────────────────────────

    def _heartbeat_interval(self) -> float:
        """How often to heartbeat — active nodes heartbeat more."""
        if self.running_tasks:
            return self.config.heartbeat_interval
        return self.config.lightweight_heartbeat_interval

    def _should_send_full_metrics(self) -> bool:
        """Send full CPU/mem/disk metrics on schedule, lightweight otherwise."""
        now = time.time()
        if self.running_tasks:
            interval = self.config.heartbeat_interval
        elif self._is_warm():
            interval = self.config.idle_metrics_interval
        else:
            interval = self.config.cold_idle_metrics_interval
        if now - self._last_full_metrics >= interval:
            self._last_full_metrics = now
            return True
        return False

    def _is_warm(self) -> bool:
        return (time.time() - self._last_task_finish) < 30

    def _pull_interval(self) -> float:
        """Adaptive pull interval based on current state."""
        if self.running_tasks:
            return self.config.active_pull_interval
        if self._state == self.STATE_ERROR_BACKOFF:
            return min(self._error_backoff_current, self.config.pull_error_backoff_max)
        if self._is_warm():
            return self.config.warm_idle_pull_interval
        return min(self.config.cold_idle_pull_interval, self.config.max_idle_pull_interval)

    # ── Heartbeat ─────────────────────────────────────────────────

    async def _do_heartbeat(self, full_metrics: bool = False):
        try:
            if full_metrics:
                metrics = await asyncio.to_thread(collect_metrics)
                metrics["running_tasks"] = len(self.running_tasks)
                await asyncio.to_thread(self.client.heartbeat, metrics)
                logger.debug("Heartbeat (full): CPU=%.1f%% Mem=%.1f%% Tasks=%d",
                             metrics["cpu_usage"], metrics["memory_usage"],
                             metrics["running_tasks"])
            else:
                await asyncio.to_thread(self.client.heartbeat, {
                    "cpu_usage": 0,
                    "memory_usage": 0,
                    "running_tasks": len(self.running_tasks),
                    "status_json": {"state": self._state, "lightweight": True},
                })
            self._consecutive_errors = 0
        except Exception:
            self._consecutive_errors += 1
            if self._consecutive_errors <= 3:
                logger.warning("Heartbeat failed (%d)", self._consecutive_errors)

    # ── Lease renewal ─────────────────────────────────────────────

    async def _do_renewals(self):
        now = time.time()
        for rt in list(self.running_tasks.values()):
            if rt.is_done():
                continue
            renew_interval = max(rt.lease_seconds // 2, 30)
            if now - rt.last_renew >= renew_interval:
                try:
                    self.client.renew_task(rt.task_id, lease_seconds=rt.lease_seconds)
                    rt.last_renew = now
                    logger.debug("Renewed lease for task %s", rt.task_id)
                except Exception:
                    logger.warning("Failed to renew lease for task %s", rt.task_id)

    # ── Task pull (with long polling) ─────────────────────────────

    async def _do_pull(self):
        profile = self.config.static_profile
        max_parallel = profile.get("limits", {}).get("max_parallel_tasks", 1)
        if len(self.running_tasks) >= max_parallel:
            # At capacity — switch to active state
            self._state = self.STATE_ACTIVE
            return

        wait = self.config.long_poll_wait_seconds if not self.running_tasks else 0

        try:
            task = await asyncio.to_thread(self.client.pull_task, wait_seconds=wait)
        except Exception as e:
            self._state = self.STATE_ERROR_BACKOFF
            self._error_backoff_current = min(
                self._error_backoff_current * 2,
                self.config.pull_error_backoff_max,
            )
            logger.debug("Pull failed (backoff=%.1fs): %s", self._error_backoff_current, e)
            return

        # Reset error backoff on success
        self._error_backoff_current = 1.0
        self._consecutive_errors = 0

        if task is None:
            self._state = self.STATE_COLD_IDLE
            return

        # Long-poll timeout — still no task
        if isinstance(task, dict) and task.get("retry_after_seconds"):
            self._state = self.STATE_COLD_IDLE
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
        self._state = self.STATE_ACTIVE

    async def _execute_and_report(self, rt: RunningTask) -> dict:
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

        self._last_task_finish = time.time()
        self._state = self.STATE_WARM_IDLE
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

        if not self.running_tasks and self._state == self.STATE_ACTIVE:
            self._state = self.STATE_WARM_IDLE


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
