"""Dispatch Runtime Skill — task submission, quick call, result polling.

This skill is the main entry point for external Agents / Skills to call
the distributed compute network.  It uses a LOW-privilege client_token
and only supports template tasks (no raw shell/hermes).

Security:
- Uses client_token only (never agent_token)
- Default only creates template tasks
- Specific node targeting requires explicit permission
- Exposes a structured result format, not raw API responses
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from .skill_config import SkillConfig

logger = logging.getLogger(__name__)


class SkillResult:
    """Structured result from a Runtime Skill operation."""

    def __init__(self, ok: bool, done: bool = False, task_id: str = "",
                 status: str = "", node_id: str = "", error: str = "",
                 result: dict | None = None, logs_tail: list | None = None,
                 retry_after_seconds: int | None = None,
                 latency_ms: int = 0):
        self.ok = ok
        self.done = done
        self.task_id = task_id
        self.status = status
        self.node_id = node_id
        self.error = error
        self.result = result or {}
        self.logs_tail = logs_tail or []
        self.retry_after_seconds = retry_after_seconds
        self.latency_ms = latency_ms

    @classmethod
    def from_quick_response(cls, data: dict) -> "SkillResult":
        """Parse a quick task API response."""
        return cls(
            ok=data.get("status") != "failed" if data.get("done") else True,
            done=data.get("done", False),
            task_id=data.get("task_id", ""),
            status=data.get("status", ""),
            result=data.get("result"),
            retry_after_seconds=data.get("retry_after_seconds"),
        )

    @classmethod
    def from_task_response(cls, data: dict) -> "SkillResult":
        """Parse a standard TaskResponse."""
        return cls(
            ok=data.get("status") == "success",
            done=data.get("status") in ("success", "failed", "timeout", "cancelled"),
            task_id=data.get("task_id", ""),
            status=data.get("status", ""),
            node_id=data.get("assigned_node_id", ""),
            error=data.get("result", {}).get("error", "") if data.get("status") == "failed" else "",
            result=data.get("result"),
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "ok": self.ok,
            "done": self.done,
            "task_id": self.task_id,
            "status": self.status,
        }
        if self.node_id:
            d["node_id"] = self.node_id
        if self.error:
            d["error"] = self.error
        if self.result is not None:
            d["result"] = self.result
        if self.logs_tail:
            d["logs_tail"] = self.logs_tail
        if self.retry_after_seconds is not None:
            d["retry_after_seconds"] = self.retry_after_seconds
        if self.latency_ms:
            d["latency_ms"] = self.latency_ms
        return d


class DispatchRuntimeSkill:
    """Main skill for submitting and tracking tasks."""

    def __init__(self, dispatcher_url: str, client_token: str,
                 default_wait: int = 10, max_wait: int = 30):
        self.base_url = dispatcher_url.rstrip("/")
        self.client_token = client_token
        self.default_wait = default_wait
        self.max_wait = max_wait

    @classmethod
    def from_config(cls, config: SkillConfig | None = None) -> "DispatchRuntimeSkill":
        """Create from a SkillConfig object (or auto-load)."""
        if config is None:
            config = SkillConfig.from_file()
        if not config.is_valid:
            raise ValueError(
                "No valid skill config found. "
                "Create ~/.config/wuzhu-dispatch/skill.yaml or pass config directly."
            )
        return cls(
            dispatcher_url=config.dispatcher_url,
            client_token=config.client_token,
            default_wait=config.default_wait_seconds,
            max_wait=config.max_wait_seconds,
        )

    @classmethod
    def from_tokens(cls, dispatcher_url: str, client_token: str) -> "DispatchRuntimeSkill":
        return cls(dispatcher_url, client_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.client_token}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: dict, timeout: int = 30) -> dict:
        resp = requests.post(
            f"{self.base_url}{path}",
            json=body, headers=self._headers(), timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, timeout: int = 30) -> dict:
        resp = requests.get(
            f"{self.base_url}{path}",
            headers=self._headers(), timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Quick task ────────────────────────────────────────────────

    def quick(self,
              template_id: str,
              params: dict | None = None,
              target: dict | None = None,
              profile: str | None = None,
              wait_seconds: int | None = None,
              priority: int = 50,
              timeout_seconds: int = 300,
              dry_run: bool = False) -> SkillResult:
        """Submit a template task and wait for its result.

        Args:
            template_id: Template name (e.g. "http_probe").
            params: Template parameters.
            target: Target specification (auto/tags/node).
            profile: Named profile from skill.yaml (merges with target).
            wait_seconds: Max seconds to wait for completion.
            priority: Task priority (0-100).
            timeout_seconds: Max execution timeout.
            dry_run: If True, return the payload without sending.

        Returns:
            SkillResult with done=true and result, or done=false with task_id.
        """
        params = params or {}
        target = target or {}
        wait = min(wait_seconds or self.default_wait, self.max_wait)

        body = {
            "template_id": template_id,
            "params": params,
            "target": target,
            "priority": priority,
            "timeout_seconds": timeout_seconds,
            "wait_seconds": wait,
        }

        if dry_run:
            return SkillResult(
                ok=True, done=True, status="dry_run",
                result=body,
            )

        try:
            start = time.time()
            data = self._post("/api/v1/client/tasks/quick", body, timeout=wait + 10)
            elapsed = int((time.time() - start) * 1000)
            result = SkillResult.from_quick_response(data)
            result.latency_ms = elapsed
            return result
        except requests.HTTPError as e:
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = str(e)
            return SkillResult(ok=False, done=True, status="error", error=detail)
        except requests.ConnectionError:
            return SkillResult(ok=False, done=True, status="error",
                               error="Cannot connect to dispatcher")
        except requests.Timeout:
            return SkillResult(ok=False, done=True, status="timeout",
                               error="Quick task timed out waiting for response")

    # ── Submit (async) ────────────────────────────────────────────

    def submit(self,
               template_id: str,
               params: dict | None = None,
               target: dict | None = None,
               profile: str | None = None,
               priority: int = 50,
               timeout_seconds: int = 300,
               dry_run: bool = False) -> SkillResult:
        """Submit a template task without waiting for completion.

        Returns immediately with the task_id. Use ``wait()`` to poll.
        """
        params = params or {}
        target = target or {}

        body = {
            "template_id": template_id,
            "params": params,
            "target": target,
            "priority": priority,
            "timeout_seconds": timeout_seconds,
        }

        if dry_run:
            return SkillResult(
                ok=True, done=True, status="dry_run",
                result=body,
            )

        try:
            data = self._post("/api/v1/client/tasks", body)
            return SkillResult(
                ok=True,
                task_id=data.get("task_id", ""),
                status=data.get("status", "pending"),
            )
        except requests.HTTPError as e:
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = str(e)
            return SkillResult(ok=False, status="error", error=detail)

    # ── Wait / poll ───────────────────────────────────────────────

    def wait(self, task_id: str, timeout: int = 60, poll_interval: float = 1.0) -> SkillResult:
        """Poll a task until it completes or times out."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                data = self._get(f"/api/v1/client/tasks/{task_id}")
                result = SkillResult.from_task_response(data)
                if result.done:
                    return result
            except requests.HTTPError:
                pass
            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, 5.0)

        return SkillResult(ok=False, done=False, task_id=task_id,
                           status="timeout", error="Wait timed out")

    # ── Status / logs / result ────────────────────────────────────

    def status(self, task_id: str) -> SkillResult:
        """Get the current status of a task."""
        try:
            data = self._get(f"/api/v1/client/tasks/{task_id}")
            return SkillResult.from_task_response(data)
        except requests.HTTPError as e:
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = str(e)
            return SkillResult(ok=False, status="error", error=detail)

    def logs(self, task_id: str) -> list[dict]:
        """Get log entries for a task."""
        try:
            resp = requests.get(
                f"{self.base_url}/api/v1/client/tasks/{task_id}/logs",
                headers=self._headers(), timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return []

    def cancel(self, task_id: str) -> SkillResult:
        """Cancel a pending/running task."""
        try:
            self._post(f"/api/v1/client/tasks/{task_id}/cancel", {})
            return SkillResult(ok=True, task_id=task_id, status="cancelled")
        except requests.HTTPError as e:
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = str(e)
            return SkillResult(ok=False, status="error", error=detail)
