"""Error hierarchy for wuzhu-dispatch."""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Machine-readable error codes for API responses."""

    # Auth
    INVALID_TOKEN = "invalid_token"
    EXPIRED_TOKEN = "expired_token"
    FORBIDDEN = "forbidden"
    INSUFFICIENT_ROLE = "insufficient_role"
    MISSING_AUTH = "missing_authentication"

    # Task
    TASK_NOT_FOUND = "task_not_found"
    TASK_NOT_ASSIGNED = "task_not_assigned"
    TASK_WRONG_STATUS = "task_wrong_status"
    INVALID_PAYLOAD = "invalid_payload"

    # Node
    NODE_NOT_FOUND = "node_not_found"
    NODE_DISABLED = "node_disabled"
    NODE_OFFLINE = "node_offline"

    # General
    RATE_LIMITED = "rate_limited"
    VALIDATION_ERROR = "validation_error"
    INTERNAL_ERROR = "internal_error"
    NOT_IMPLEMENTED = "not_implemented"

    # Resource
    DUPLICATE_RESOURCE = "duplicate_resource"
    RESOURCE_EXHAUSTED = "resource_exhausted"


class DispatchError(Exception):
    """Base exception with an error code and HTTP status code."""

    def __init__(
        self,
        code: ErrorCode,
        message: str = "",
        status_code: int = 400,
        detail: dict | None = None,
    ):
        self.code = code
        self.message = message or code.value
        self.status_code = status_code
        self.detail = detail or {}
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {
            "error": self.code.value,
            "message": self.message,
            "detail": self.detail,
        }

    def __repr__(self) -> str:
        return f"<DispatchError [{self.status_code}] {self.code.value}: {self.message}>"
