"""Shared task statuses and execution modes used across all three components."""

from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    """Task lifecycle states."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

    @classmethod
    def terminal(cls) -> set[str]:
        return {"success", "failed", "cancelled", "timeout"}

    @classmethod
    def schedulable(cls) -> set[str]:
        return {"pending", "retrying"}


class ExecutionMode(str, Enum):
    """How a task's payload should be executed on the compute server."""

    SHELL = "shell"
    HERMES = "hermes"
    DOCKER = "docker"


class TaskRequirements:
    """Fields that a task may specify for node matching."""

    __slots__ = ("required_tags", "avoid_tags", "runtime", "min_cpu_cores",
                 "min_memory_mb", "min_bandwidth_mbps")

    def __init__(self, data: dict) -> None:
        self.required_tags: list[str] = data.get("required_tags", [])
        self.avoid_tags: list[str] = data.get("avoid_tags", [])
        self.runtime: dict = data.get("runtime", {})
        self.min_cpu_cores: int = data.get("min_cpu_cores", 0)
        self.min_memory_mb: int = data.get("min_memory_mb", 0)
        self.min_bandwidth_mbps: int = data.get("min_bandwidth_mbps", 0)


class NodeRequirements(TaskRequirements):
    """Same shape — a task's requirements dict is parsed identically
    on both the dispatcher (for scheduling) and the compute server (for
    local capability enforcement)."""
    pass
