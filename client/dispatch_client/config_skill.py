"""Dispatch Config Skill — configuration validation, diagnosis, node setup.

This skill manages and validates wuzhu-dispatch configuration.
It does NOT execute tasks — it's for setup, diagnostics, and registration.

Security boundaries:
- Config Skill uses a LOW-privilege client_token by default.
- It can validate configs without making API calls.
- Registration requires explicit high-permission token or DISPATCH_SERVER_SECRET.
- It never creates tasks, never accesses MySQL, never holds agent_token as primary.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import requests
import yaml

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Validation results
# ═══════════════════════════════════════════════════════════════════


class ConfigCheck:
    """A single check result."""

    def __init__(self, name: str, ok: bool, message: str = ""):
        self.name = name
        self.ok = ok
        self.message = message

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "message": self.message}


class ConfigReport:
    """Full diagnostic report."""

    def __init__(self):
        self.checks: list[ConfigCheck] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0 and all(c.ok for c in self.checks)

    @property
    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "All checks passed."
        parts = []
        if self.ok:
            parts.append("All checks passed")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return "; ".join(parts)

    def add_check(self, name: str, ok: bool, message: str = ""):
        self.checks.append(ConfigCheck(name, ok, message))

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    def add_error(self, msg: str):
        self.errors.append(msg)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
            "warnings": self.warnings,
            "errors": self.errors,
        }


# ═══════════════════════════════════════════════════════════════════
# Config validation helpers
# ═══════════════════════════════════════════════════════════════════


_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _is_valid_node_id(node_id: str) -> bool:
    return bool(_NODE_ID_RE.match(node_id))


def validate_node_yaml(path: str | Path) -> ConfigReport:
    """Validate a node.yaml file. Returns a ConfigReport."""
    report = ConfigReport()
    path = Path(path).expanduser().resolve()

    if not path.exists():
        report.add_error(f"File not found: {path}")
        return report

    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        report.add_error(f"Cannot parse YAML: {e}")
        return report

    # dispatcher_url
    dispatcher_url = cfg.get("dispatcher_url", "")
    report.add_check("dispatcher_url", bool(dispatcher_url),
                     "Present" if dispatcher_url else "Missing")

    # node_id
    node_id = cfg.get("node_id", "")
    node_ok = bool(node_id) and _is_valid_node_id(node_id)
    report.add_check("node_id", node_ok,
                     f"Valid: {node_id}" if node_ok else f"Invalid: {node_id!r}")

    # agent_token
    agent_token = cfg.get("agent_token", "")
    token_ok = bool(agent_token) and agent_token != "CHANGE_ME" and agent_token != "CHANGE_ME_AGENT_TOKEN"
    report.add_check("agent_token", token_ok,
                     "Present" if token_ok else "Missing or still default (CHANGE_ME)")

    # registration_token should NOT be permanently stored
    reg_token = cfg.get("registration_token", "")
    if reg_token:
        report.add_warning("registration_token is present in config — should only be used for initial registration")

    # work_dir writable check (READ-ONLY — never create dirs)
    work_dir = cfg.get("agent", {}).get("work_dir", "")
    if work_dir:
        test_path = Path(work_dir).expanduser().resolve()
        if test_path.exists():
            if os.access(str(test_path), os.W_OK):
                report.add_check("work_dir_writable", True, str(work_dir))
            else:
                report.add_check("work_dir_writable", False, f"Not writable: {work_dir}")
        else:
            report.add_check("work_dir_writable", False,
                             f"Does not exist (create with --create-missing-dirs): {work_dir}")

    # log_dir writable check (READ-ONLY)
    log_dir = cfg.get("agent", {}).get("log_dir", "")
    if log_dir:
        test_path = Path(log_dir).expanduser().resolve()
        if test_path.exists():
            if os.access(str(test_path), os.W_OK):
                report.add_check("log_dir_writable", True)
            else:
                report.add_check("log_dir_writable", False)
        else:
            report.add_check("log_dir_writable", False,
                             f"Does not exist: {log_dir}")

    # cleanup config
    cleanup = cfg.get("cleanup", {})
    if cleanup:
        report.add_check("cleanup_enabled", cleanup.get("enabled", True),
                         f"enabled={cleanup.get('enabled', True)}")
        max_size = cleanup.get("max_work_dir_size_mb", 0)
        if max_size and max_size < 100:
            report.add_warning(f"max_work_dir_size_mb={max_size} is very low")

    # static_profile
    profile = cfg.get("static_profile", {})
    if profile:
        runtime = profile.get("runtime", {})
        has_runtime = bool(runtime)
        report.add_check("static_profile.runtime", has_runtime,
                         "Present" if has_runtime else "Missing")

        memory_mb = profile.get("memory_mb", 0)
        if memory_mb and memory_mb < 512:
            # Check if docker/hermes/browser is enabled on low-memory node
            if runtime.get("docker"):
                report.add_warning("Node has <512MB memory but docker is enabled")
            if runtime.get("hermes"):
                report.add_warning("Node has <512MB memory but hermes is enabled")

        max_parallel = profile.get("limits", {}).get("max_parallel_tasks", 1)
        if max_parallel < 1:
            report.add_warning(f"max_parallel_tasks={max_parallel} is too low (minimum 1)")

    # hermes workspaces security
    agent_cfg = cfg.get("agent", {})
    hermes_workspaces = agent_cfg.get("allowed_hermes_workspaces", [])
    for ws in hermes_workspaces:
        ws_path = Path(ws).expanduser().resolve()
        if not ws_path.exists():
            report.add_warning(f"Hermes workspace does not exist: {ws}")
        # Warn if workspace is under work_dir (could be cleaned up)
        work_dir_resolved = Path(work_dir).expanduser().resolve() if work_dir else None
        if work_dir_resolved and work_dir_resolved in ws_path.parents:
            report.add_warning(f"Hermes workspace {ws} is under work_dir — cleanup may affect it")

    return report


def validate_client_yaml(path: str | Path) -> ConfigReport:
    """Validate a client.yaml file."""
    report = ConfigReport()
    path = Path(path).expanduser().resolve()

    if not path.exists():
        report.add_error(f"File not found: {path}")
        return report

    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        report.add_error(f"Cannot parse YAML: {e}")
        return report

    # dispatcher_url
    dispatcher_url = cfg.get("dispatcher_url", "")
    report.add_check("dispatcher_url", bool(dispatcher_url),
                     "Present" if dispatcher_url else "Missing")

    # client_token
    client_token = cfg.get("client_token", "")
    report.add_check("client_token", bool(client_token),
                     "Present" if client_token else "Missing")

    # Must NOT contain node_id
    node_id = cfg.get("node_id", "")
    if node_id:
        report.add_warning("client.yaml should NOT contain node_id (that's for node.yaml)")

    # Must NOT contain registration_token
    if cfg.get("registration_token"):
        report.add_warning("client.yaml should NOT contain registration_token")

    # Must NOT look like an agent_token
    if client_token and any(
        pattern in client_token.lower()
        for pattern in ["agent_token", "node_token", "change_me"]
    ):
        report.add_warning("client_token looks like it might be an agent token (contains 'agent' or 'node')")

    return report


def check_dispatcher_connectivity(url: str, timeout: int = 10) -> ConfigReport:
    """Check if a dispatcher is reachable and responding."""
    report = ConfigReport()
    base = url.rstrip("/")

    try:
        r = requests.get(f"{base}/health", timeout=timeout)
        if r.status_code == 200:
            report.add_check("health_endpoint", True, f"HTTP {r.status_code}")
        else:
            report.add_check("health_endpoint", False, f"HTTP {r.status_code}")
    except requests.ConnectionError:
        report.add_check("health_endpoint", False, "Connection refused")
    except requests.Timeout:
        report.add_check("health_endpoint", False, "Timed out")
    except Exception as e:
        report.add_check("health_endpoint", False, str(e))

    return report


# ═══════════════════════════════════════════════════════════════════
# Node config generation
# ═══════════════════════════════════════════════════════════════════


_NODE_PROFILES: dict[str, dict] = {
    "small-probe": {
        "description": "256MB/512M lightweight probe node",
        "config": {
            "roles": ["compute_server"],
            "tags": ["probe", "low_memory", "lightweight"],
            "static_profile": {
                "memory_mb": 256,
                "runtime": {"shell": True, "python": False, "docker": False, "hermes": False},
                "limits": {"max_parallel_tasks": 1, "allow_heavy_download": False, "allow_heavy_compute": False},
            },
            "agent": {
                "lightweight_heartbeat_interval_seconds": 30,
                "idle_metrics_interval_seconds": 300,
                "cold_idle_metrics_interval_seconds": 600,
                "long_poll_wait_seconds": 25,
            },
            "cleanup": {
                "enabled": True,
                "keep_success_seconds": 1800,
                "keep_failed_seconds": 86400,
                "max_work_dir_size_mb": 512,
            },
        },
    },
    "bandwidth-node": {
        "description": "High-bandwidth download/transfer node",
        "config": {
            "roles": ["compute_server"],
            "tags": ["high_bandwidth", "downloader", "public_ipv4"],
            "static_profile": {
                "runtime": {"shell": True, "python": True, "docker": False, "hermes": False},
                "limits": {"max_parallel_tasks": 2, "allow_heavy_download": True, "allow_heavy_compute": False},
            },
            "cleanup": {
                "enabled": True,
                "max_work_dir_size_mb": 4096,
            },
        },
    },
    "hermes-worker": {
        "description": "Hermes AI agent worker node",
        "config": {
            "roles": ["compute_server"],
            "tags": ["hermes_worker"],
            "static_profile": {
                "runtime": {"shell": True, "python": True, "hermes": True},
                "limits": {"max_parallel_tasks": 1},
            },
            "agent": {
                "allowed_hermes_workspaces": ["/home/example/workspace"],
                "hermes_bin": "hermes",
            },
            "cleanup": {"enabled": True},
        },
    },
    "general": {
        "description": "General-purpose compute node",
        "config": {
            "roles": ["compute_server"],
            "tags": ["general"],
            "static_profile": {
                "runtime": {"shell": True, "python": True, "docker": True, "hermes": True},
                "limits": {"max_parallel_tasks": 3, "allow_heavy_download": True, "allow_heavy_compute": True},
            },
            "cleanup": {"enabled": True, "max_work_dir_size_mb": 2048},
        },
    },
}


def list_node_profiles() -> dict:
    """Return available node profiles (without ids)."""
    return _NODE_PROFILES


def generate_node_yaml(
    profile: str,
    node_id: str,
    agent_token: str = "CHANGE_ME",
    dispatcher_url: str = "https://dispatch.example.com",
    name: str = "",
    region: str = "",
    provider: str = "",
) -> str:
    """Generate a complete node.yaml for the given profile.

    Args:
        profile: One of the keys from _NODE_PROFILES.
        node_id: Unique node identifier.
        agent_token: Node authentication token.
        dispatcher_url: Dispatcher base URL.
        name: Human-readable node name.
        region: Region code (e.g. HK, US).
        provider: Provider name.

    Returns:
        YAML string.

    Raises:
        ValueError: If profile is unknown.
    """
    if profile not in _NODE_PROFILES:
        raise ValueError(f"Unknown profile: {profile!r}. Available: {list(_NODE_PROFILES.keys())}")

    cfg = _NODE_PROFILES[profile]
    config = {
        "dispatcher_url": dispatcher_url,
        "node_id": node_id,
        "agent_token": agent_token,
        "name": name or f"{node_id} ({cfg['description']})",
        "region": region,
        "provider": provider,
        **cfg["config"],
    }

    return yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True)


def generate_client_yaml(
    dispatcher_url: str = "https://dispatch.example.com",
    client_token: str = "your-client-api-token",
) -> str:
    """Generate a simple client.yaml."""
    return yaml.dump({
        "dispatcher_url": dispatcher_url,
        "client_token": client_token,
    }, default_flow_style=False, sort_keys=False)


# ═══════════════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════════════


def register_node_via_api(
    dispatcher_url: str,
    admin_token: str,
    node_config: dict,
    dry_run: bool = False,
) -> dict:
    """Register a compute node via the admin API.

    Requires DISPATCH_SERVER_SECRET or admin client_token.

    Args:
        dispatcher_url: Dispatcher base URL.
        admin_token: Bearer token with admin/owner privileges.
        node_config: Node configuration dict (from node.yaml).
        dry_run: If True, only print the request without sending.

    Returns:
        API response dict or dry-run preview.
    """
    base = dispatcher_url.rstrip("/")

    payload = {
        "node_id": node_config.get("node_id", ""),
        "agent_token": node_config.get("agent_token", ""),
        "name": node_config.get("name", ""),
        "region": node_config.get("region", ""),
        "provider": node_config.get("provider", ""),
        "roles": node_config.get("roles", []),
        "tags": node_config.get("tags", []),
        "static_profile": node_config.get("static_profile", {}),
    }

    if dry_run:
        return {
            "dry_run": True,
            "method": "POST",
            "url": f"{base}/api/v1/admin/nodes/register",
            "payload": payload,
        }

    resp = requests.post(
        f"{base}/api/v1/admin/nodes/register",
        json=payload,
        headers={
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
