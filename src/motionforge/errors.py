"""Stable, user-safe failures for CLI and sidecar clients."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    INVALID_SCENE = "INVALID_SCENE"
    SIMULATION_FAILED = "SIMULATION_FAILED"
    EXPORT_FAILED = "EXPORT_FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    DISK_FULL = "DISK_FULL"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    NOT_FOUND = "NOT_FOUND"
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_REQUEST = "INVALID_REQUEST"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class MotionForgeError(Exception):
    """An expected engine failure whose code and message are safe to return."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: Any = None,
        retriable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.retriable = retriable

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retriable": self.retriable,
        }
        if self.details is not None:
            result["details"] = self.details
        return result


def validation_diagnostics(error: Exception) -> list[dict[str, str]]:
    """Turn Pydantic errors into concise, stable JSON-path diagnostics."""

    errors = getattr(error, "errors", None)
    if not callable(errors):
        return [{"path": "$", "message": str(error)}]
    diagnostics: list[dict[str, str]] = []
    for item in errors(include_url=False, include_input=False):
        path = "$"
        for part in item.get("loc", ()):
            path += f"[{part}]" if isinstance(part, int) else f".{part}"
        diagnostics.append({"path": path, "message": item.get("msg", "Invalid value")})
    return diagnostics
