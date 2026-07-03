"""Compute Server configuration — loaded from node.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ComputeServerConfig:
    """Typed wrapper around node.yaml."""

    def __init__(self, data: dict[str, Any]):
        self._raw = data

        dispatcher_url = data.get("dispatcher_url", "").rstrip("/")
        if not dispatcher_url:
            raise ValueError(
                "Missing required field 'dispatcher_url' in compute-server config. "
                "Set it to the dispatcher's base URL, e.g.:\n"
                "  dispatcher_url: \"https://dispatch.example.com\""
            )
        self.dispatcher_url: str = dispatcher_url
        self.node_id: str = data.get("node_id", "")
        self.registration_token: str = data.get("registration_token", "")
        self.agent_token: str = data.get("agent_token", "")
        self.name: str = data.get("name", "")
        self.region: str = data.get("region", "")
        self.provider: str = data.get("provider", "")
        self.roles: list[str] = data.get("roles", [])
        self.tags: list[str] = data.get("tags", [])
        self.static_profile: dict[str, Any] = data.get("static_profile", {})

        agent_cfg = data.get("agent", {})

        # ── Heartbeat ────────────────────────────────────────────────
        self.heartbeat_interval: int = agent_cfg.get("heartbeat_interval_seconds", 20)
        # Separated: lightweight (online proof) vs full (resource metrics)
        self.lightweight_heartbeat_interval: int = agent_cfg.get(
            "lightweight_heartbeat_interval_seconds", 30
        )
        self.idle_metrics_interval: int = agent_cfg.get(
            "idle_metrics_interval_seconds", 60
        )
        self.cold_idle_metrics_interval: int = agent_cfg.get(
            "cold_idle_metrics_interval_seconds", 120
        )

        # ── Pull intervals — adaptive ────────────────────────────────
        self.pull_interval: int = agent_cfg.get("pull_interval_seconds", 10)
        self.active_pull_interval: int = agent_cfg.get("active_pull_interval_seconds", 3)
        self.warm_idle_pull_interval: int = agent_cfg.get("warm_idle_pull_interval_seconds", 10)
        self.cold_idle_pull_interval: int = agent_cfg.get("cold_idle_pull_interval_seconds", 30)
        self.max_idle_pull_interval: int = agent_cfg.get("max_idle_pull_interval_seconds", 60)

        # ── Long polling ─────────────────────────────────────────────
        self.long_poll_wait_seconds: int = agent_cfg.get("long_poll_wait_seconds", 25)

        # ── Error backoff ────────────────────────────────────────────
        self.pull_error_backoff_max: int = agent_cfg.get("pull_error_backoff_max_seconds", 120)

        # ── Work directories ─────────────────────────────────────────
        self.work_dir: str = agent_cfg.get("work_dir", "/opt/wuzhu-dispatch/work")
        self.log_dir: str = agent_cfg.get("log_dir", "/opt/wuzhu-dispatch/logs")
        self.allowed_hermes_workspaces: list[str] = agent_cfg.get("allowed_hermes_workspaces", [])
        self.hermes_bin: str = agent_cfg.get("hermes_bin", "hermes")

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ComputeServerConfig":
        path = Path(path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls(data)
