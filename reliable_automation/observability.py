"""Structured JSON-lines logging with conservative field redaction."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from .clock import Clock

_NON_SECRET_TOKEN_FIELDS = {
    "token_count",
    "token_expired",
    "token_refreshes",
    "token_status",
}

_SENSITIVE_EXACT_FIELDS = {
    "auth",
    "cookie",
    "credential",
    "credentials",
    "mfa_code",
    "otp",
    "passphrase",
    "private_key",
    "recovery_code",
    "session",
    "session_cookie",
    "signing_key",
}


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized in _NON_SECRET_TOKEN_FIELDS:
        return False
    if normalized in _SENSITIVE_EXACT_FIELDS:
        return True
    if "password" in normalized or "secret" in normalized:
        return True
    if "cookie" in normalized or "credential" in normalized:
        return True
    if "api_key" in normalized or "apikey" in normalized:
        return True
    if normalized.endswith(("_private_key", "_signing_key", "_encryption_key")):
        return True
    if normalized.endswith(("_mfa_code", "_otp", "_passphrase", "_recovery_code")):
        return True
    if normalized == "authorization" or normalized.endswith("_authorization"):
        return True
    return normalized == "token" or normalized.endswith("_token") or normalized.startswith("token_")


def _redact(value: Any, key: str = "") -> Any:
    if _sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(child_key): _redact(child, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return repr(value)


class JsonlEventLogger:
    """Append-only structured logger suitable for terminals and evidence files."""

    def __init__(
        self,
        clock: Clock,
        *,
        path: Path | None = None,
        stream: TextIO | None = None,
    ) -> None:
        self.clock = clock
        self.path = path
        self.stream = stream
        self._lock = threading.RLock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, *, level: str = "INFO", **fields: Any) -> dict[str, Any]:
        record = {
            "ts_epoch": round(self.clock.now(), 6),
            "level": level,
            "event": event,
            **_redact(fields),
        }
        line = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        with self._lock:
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            if self.stream is not None:
                self.stream.write(line + "\n")
                self.stream.flush()
        return record


class NullEventLogger(JsonlEventLogger):
    """Drop-in logger for callers that do not need output."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock)


def stdout_logger(clock: Clock, path: Path | None = None) -> JsonlEventLogger:
    return JsonlEventLogger(clock, path=path, stream=sys.stdout)
