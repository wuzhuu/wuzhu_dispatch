"""Client HTTP client — talks to the dispatcher's client API.

Uses client API token (NOT agent token) for authentication.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class DispatchClient:
    """HTTP client for the dispatcher's /api/v1/client/* endpoints."""

    def __init__(self, dispatcher_url: str, client_token: str, timeout: int = 30):
        self.base_url = dispatcher_url.rstrip("/")
        self.client_token = client_token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.client_token}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: dict) -> dict:
        resp = requests.post(
            f"{self.base_url}{path}",
            json=body, headers=self._headers(), timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str) -> dict:
        resp = requests.get(
            f"{self.base_url}{path}",
            headers=self._headers(), timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Task operations ───────────────────────────────────────────

    def create_task(self, task_data: dict) -> dict:
        return self._post("/api/v1/client/tasks", task_data)

    def list_tasks(self, status: str = "") -> list:
        path = "/api/v1/client/tasks"
        if status:
            path += f"?status={status}"
        resp = requests.get(
            f"{self.base_url}{path}",
            headers=self._headers(), timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_task(self, task_id: str) -> dict:
        return self._get(f"/api/v1/client/tasks/{task_id}")

    def cancel_task(self, task_id: str) -> dict:
        return self._post(f"/api/v1/client/tasks/{task_id}/cancel", {})

    def retry_task(self, task_id: str) -> dict:
        return self._post(f"/api/v1/client/tasks/{task_id}/retry", {})

    def get_task_logs(self, task_id: str) -> list:
        resp = requests.get(
            f"{self.base_url}/api/v1/client/tasks/{task_id}/logs",
            headers=self._headers(), timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Node operations (admin) ───────────────────────────────────

    def list_nodes(self) -> list:
        resp = requests.get(
            f"{self.base_url}/api/v1/admin/nodes",
            headers=self._headers(), timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()
