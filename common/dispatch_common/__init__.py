"""dispatch_common — shared types, schemas, and error models for wuzhu-dispatch.

This package is consumed by all three components:
- dispatcher (FastAPI server)
- compute-server (worker daemon)
- client (CLI / SDK)

It ensures type consistency across the wire.
"""

from .auth_types import ClientAuthScope, ComputeNodeAuth, TokenType
from .errors import DispatchError, ErrorCode
from .schemas import (
    ClientTaskCreateRequest,
    ClientTaskResponse,
    ComputeHeartbeatRequest,
    ComputeNodeProfile,
    ComputeRegisterRequest,
    ComputeTaskPullResponse,
    NodeStaticProfile,
    TaskPayload,
)
from .task_types import (
    ExecutionMode,
    NodeRequirements,
    TaskRequirements,
    TaskStatus,
)

__all__ = [
    # auth
    "ClientAuthScope",
    "ComputeNodeAuth",
    "TokenType",
    # errors
    "DispatchError",
    "ErrorCode",
    # schemas
    "ClientTaskCreateRequest",
    "ClientTaskResponse",
    "ComputeHeartbeatRequest",
    "ComputeNodeProfile",
    "ComputeRegisterRequest",
    "ComputeTaskPullResponse",
    "NodeStaticProfile",
    "TaskPayload",
    # task types
    "ExecutionMode",
    "NodeRequirements",
    "TaskRequirements",
    "TaskStatus",
]
