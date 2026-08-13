from __future__ import annotations

from enum import Enum


class LLMErrorType(str, Enum):
    AUTH_ERROR = "AUTH_ERROR"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    INVALID_JSON = "INVALID_JSON"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class LLMError(Exception):
    def __init__(self, error_type: LLMErrorType, message: str, status_code: int | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.status_code = status_code
