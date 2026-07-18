"""Provider adapter contract plus deterministic demo provider."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .errors import (
    ProviderTimeout,
    TokenExpired,
    TokenRefreshFailed,
    TransientProviderError,
)
from .models import JobRequest


class AutomationProvider(Protocol):
    """Boundary implemented by an actual model/API adapter.

    Implementations must propagate ``request.idempotency_key`` to any provider
    operation that can create side effects and must enforce ``timeout_seconds``.
    """

    def generate(self, request: JobRequest, *, timeout_seconds: float) -> Mapping[str, Any]:
        """Return a raw response mapping or raise a typed provider exception."""

    def refresh_token(self) -> None:
        """Refresh an expired credential without exposing it to the engine."""


class ScriptedProvider:
    """Secret-free provider used for deterministic tests and CLI demos."""

    def __init__(
        self,
        steps: Sequence[str | Mapping[str, Any]],
        *,
        refresh_fails: bool = False,
    ) -> None:
        self.steps = deque(steps)
        self.refresh_fails = refresh_fails
        self.call_count = 0
        self.refresh_count = 0
        self.seen_timeouts: list[float] = []
        self.seen_idempotency_keys: list[str] = []

    def generate(self, request: JobRequest, *, timeout_seconds: float) -> Mapping[str, Any]:
        self.call_count += 1
        self.seen_timeouts.append(timeout_seconds)
        self.seen_idempotency_keys.append(request.idempotency_key)
        if not self.steps:
            raise RuntimeError("scripted provider exhausted")
        step = self.steps.popleft()
        if isinstance(step, Mapping):
            return step
        if step == "success":
            return {
                "content": f"Completed: {request.objective}",
                "confidence": 0.96,
                "metadata": {"adapter": "scripted", "side_effects": False},
            }
        if step == "transient":
            raise TransientProviderError("simulated upstream 503")
        if step == "timeout":
            raise ProviderTimeout(f"simulated deadline exceeded after {timeout_seconds:.2f}s")
        if step == "token_expired":
            raise TokenExpired("simulated credential expiry")
        if step == "malformed":
            return {"unexpected": "shape"}
        raise RuntimeError(f"unknown scripted provider step: {step}")

    def refresh_token(self) -> None:
        self.refresh_count += 1
        if self.refresh_fails:
            raise TokenRefreshFailed("simulated credential refresh failure")
