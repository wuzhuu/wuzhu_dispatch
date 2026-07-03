"""Compute Server HTTP client — talks to the dispatcher's compute API.

Uses node_id + agent_token for all authenticated calls.
Registration uses a separate registration_token on first connect.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class ComputeClient:
    """HTTP client for the dispatcher's /api/v1/compute/* endpoints."""

    def __init__(
        self,
        dispatcher_url: str,
        node_id: str,
        agent_token: str,
        registration_token: str = "",
        timeout: int = 30,
    ):
        self.base_url = dispatcher_url.rstrip("/")
        self.node_id = node_id
        self.agent_token = agent_token
        self.registration_token = registration_token
        self.timeout = timeout

    # ── Auth headers ──────────────────────────────────────────────

    def _compute_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.agent_token}",
            "X-Node-Id": self.node_id,
            "Content-Type": "application/json",
        }

    def _registration_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.registration_token}",
            "Content-Type": "application/json",
        }

    # ── HTTP helpers ──────────────────────────────────────────────

    def _post(self, path: str, body: dict | None = None,
              headers: dict | None = None) -> requests.Response:
        url = f"{self.base_url}{path}"
        resp = requests.post(url, json=body or {},
                             headers=headers or self._compute_headers(),
                             timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def _get(self, path: str, headers: dict | None = None) -> requests.Response:
        url = f"{self.base_url}{path}"
        resp = requests.get(url, headers=headers or self._compute_headers(),
                            timeout=self.timeout)
        resp.raise_for_status()
        return resp

    # ── Node lifecycle ────────────────────────────────────────────

    def register(self, config: "ComputeServerConfig") -> dict:
        """POST /api/v1/compute/register using registration_token.

        Raises RuntimeError if no registration_token is configured.
        (synchronous — called via asyncio.to_thread in main loop)
        """
        if not self.registration_token:
            raise RuntimeError(
                "registration_token is not set. The compute-server cannot "
                "auto-register without a registration_token. Either:\n"
                "  1. Set registration_token in node.yaml for auto-register, or\n"
                "  2. Have an admin pre-register this node via "
                "POST /api/v1/admin/nodes/register"
            )
        body = {
            "node_id": config.node_id,
            "agent_token": config.agent_token,
            "name": config.name,
            "region": config.region,
            "provider": config.provider,
            "roles": config.roles,
            "tags": config.tags,
            "static_profile": config.static_profile,
        }
        resp = self._post("/api/v1/compute/register", body,
                          headers=self._registration_headers())
        return resp.json()

    def heartbeat(self, metrics: dict[str, Any]) -> dict:
        """POST /api/v1/compute/heartbeat."""
        resp = self._post("/api/v1/compute/heartbeat", metrics)
        return resp.json()

    # ── Task lifecycle ────────────────────────────────────────────

    def pull_task(self, wait_seconds: int = 0) -> Optional[dict]:
        """POST /api/v1/compute/tasks/pull — returns task or None.

        When *wait_seconds* > 0, the dispatcher holds the connection
        for up to that many seconds waiting for a suitable task.
        """
        try:
            path = "/api/v1/compute/tasks/pull"
            if wait_seconds > 0:
                path += f"?wait_seconds={wait_seconds}"
            resp = self._post(path, {})
            data = resp.json()
            if data is None:
                return None
            # Long-poll timeout response
            if isinstance(data, dict) and data.get("task") is None:
                return data
            return data
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    def upload_log(self, task_id: str, level: str, message: str):
        """POST /api/v1/compute/tasks/{id}/log."""
        body = {"level": level, "message": message}
        self._post(f"/api/v1/compute/tasks/{task_id}/log", body)

    def renew_task(self, task_id: str, lease_seconds: int = 300):
        """POST /api/v1/compute/tasks/{id}/renew."""
        body = {"lease_seconds": lease_seconds}
        self._post(f"/api/v1/compute/tasks/{task_id}/renew", body)

    def finish_task(self, task_id: str, result: dict):
        """POST /api/v1/compute/tasks/{id}/finish."""
        body = {"result": result}
        self._post(f"/api/v1/compute/tasks/{task_id}/finish", body)

    def fail_task(self, task_id: str, error: str, traceback: str = ""):
        """POST /api/v1/compute/tasks/{id}/fail."""
        body = {"error": error, "traceback": traceback}
        self._post(f"/api/v1/compute/tasks/{task_id}/fail", body)
