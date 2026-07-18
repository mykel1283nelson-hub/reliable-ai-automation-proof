"""Injectable clocks keep orchestration deterministic under test."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol


class Clock(Protocol):
    """Minimal time dependency used by the engine."""

    def now(self) -> float:
        """Return seconds since the Unix epoch."""

    def sleep(self, seconds: float) -> None:
        """Wait (or simulate waiting) for ``seconds``."""


class SystemClock:
    """Production clock backed by the operating system."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class ManualClock:
    """Deterministic clock used by tests and reproducible demos."""

    current: float = 1_700_000_000.0
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration cannot be negative")
        self.sleeps.append(seconds)
        self.current += seconds

    def advance(self, seconds: float) -> None:
        """Advance without recording a retry sleep."""

        if seconds < 0:
            raise ValueError("advance duration cannot be negative")
        self.current += seconds
