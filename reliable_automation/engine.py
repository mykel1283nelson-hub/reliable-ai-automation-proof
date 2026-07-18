"""Reliable, provider-agnostic automation state machine."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Protocol

from .clock import Clock
from .errors import (
    IdempotencyConflict,
    InvalidTransition,
    JobNotFound,
    MalformedProviderOutput,
    ProviderError,
    ProviderTimeout,
    TokenExpired,
    TransientProviderError,
    ValidationError,
)
from .models import FailureInfo, JobRecord, JobRequest, JobStatus, ProviderResult
from .observability import JsonlEventLogger
from .provider import AutomationProvider
from .storage import FileJobStore


class JitterSource(Protocol):
    def sample(self, job_id: str, retry_number: int) -> float:
        """Return a deterministic or random value in the inclusive range [0, 1]."""


class StableJitter:
    """Per-job deterministic jitter: spread load while keeping runs reproducible."""

    def sample(self, job_id: str, retry_number: int) -> float:
        material = f"{job_id}:{retry_number}".encode("utf-8")
        integer = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        return integer / ((1 << 64) - 1)


class MidpointJitter:
    """No-op jitter source useful in tests (factor is exactly 1.0)."""

    def sample(self, job_id: str, retry_number: int) -> float:
        del job_id, retry_number
        return 0.5


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 4.0
    jitter_ratio: float = 0.2
    max_token_refreshes: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValidationError("max_attempts must be at least 1")
        for name, value in (
            ("base_delay_seconds", self.base_delay_seconds),
            ("max_delay_seconds", self.max_delay_seconds),
            ("jitter_ratio", self.jitter_ratio),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"{name} must be numeric")
            if not math.isfinite(float(value)):
                raise ValidationError(f"{name} must be finite")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValidationError("retry delays must be non-negative and max >= base")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValidationError("jitter_ratio must be between 0 and 1")
        if (
            isinstance(self.max_token_refreshes, bool)
            or not isinstance(self.max_token_refreshes, int)
            or self.max_token_refreshes < 0
        ):
            raise ValidationError("max_token_refreshes cannot be negative")

    def delay(self, retry_number: int, jitter_value: float) -> float:
        if isinstance(retry_number, bool) or not isinstance(retry_number, int) or retry_number < 1:
            raise ValidationError("retry_number must be at least 1")
        if isinstance(jitter_value, bool) or not isinstance(jitter_value, (int, float)):
            raise ValidationError("jitter source must return a numeric value")
        if not math.isfinite(float(jitter_value)):
            raise ValidationError("jitter source returned a non-finite value")
        if not 0.0 <= jitter_value <= 1.0:
            raise ValidationError("jitter source returned a value outside [0, 1]")
        uncapped = self.base_delay_seconds * (2 ** (retry_number - 1))
        capped = min(self.max_delay_seconds, uncapped)
        factor = 1.0 + self.jitter_ratio * ((2.0 * jitter_value) - 1.0)
        return round(capped * factor, 6)


class ReliableAutomationEngine:
    """Orchestrate one provider behind a durable, observable state machine."""

    def __init__(
        self,
        *,
        store: FileJobStore,
        provider: AutomationProvider,
        clock: Clock,
        logger: JsonlEventLogger,
        retry_policy: RetryPolicy | None = None,
        jitter: JitterSource | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.clock = clock
        self.logger = logger
        self.retry_policy = retry_policy or RetryPolicy()
        self.jitter = jitter or StableJitter()

    def enqueue(self, request: JobRequest) -> JobRecord:
        """Persist a job before work begins; repeated identical requests are safe."""

        existing = self.store.load(request.job_id)
        if existing is not None:
            if existing.request_fingerprint != request.fingerprint:
                self.logger.emit(
                    "job.idempotency_conflict",
                    level="ERROR",
                    job_id=request.job_id,
                    status=existing.status.value,
                )
                raise IdempotencyConflict(
                    "idempotency key already belongs to a different validated request"
                )
            self.logger.emit(
                "job.idempotency_hit",
                job_id=existing.job_id,
                status=existing.status.value,
                attempt_count=existing.attempt_count,
            )
            return existing

        record = JobRecord.new(request, self.clock.now())
        self.store.save(record)
        self.logger.emit(
            "job.enqueued",
            job_id=record.job_id,
            status=record.status.value,
            requires_approval=request.requires_approval,
        )
        return record

    def submit(self, request: JobRequest) -> JobRecord:
        """Enqueue, then run until an approval gate or terminal state is reached."""

        record = self.enqueue(request)
        return self.run(record.job_id)

    def get(self, job_id: str) -> JobRecord:
        record = self.store.load(job_id)
        if record is None:
            raise JobNotFound(f"job not found: {job_id}")
        return record

    def run(self, job_id: str) -> JobRecord:
        """Run or recover a persisted job."""

        record = self.get(job_id)
        if record.status.terminal or record.status == JobStatus.AWAITING_APPROVAL:
            self.logger.emit(
                "job.run_noop",
                job_id=record.job_id,
                status=record.status.value,
                reason="terminal_or_waiting_for_human",
            )
            return record

        if record.status == JobStatus.RUNNING:
            if record.attempt_count >= self.retry_policy.max_attempts:
                return self._dead_letter(
                    record,
                    code="recovery_attempt_budget_exhausted",
                    message="process stopped during the final provider attempt; manual review required",
                    retryable=False,
                )
            record.status = JobStatus.PENDING
            record.add_history(
                self.clock.now(),
                "job.recovered",
                recovery="replay_with_same_provider_idempotency_key",
            )
            self.store.save(record)
            self.logger.emit(
                "job.recovered",
                level="WARNING",
                job_id=record.job_id,
                attempt_count=record.attempt_count,
            )

        while True:
            if record.status == JobStatus.RETRY_WAIT:
                wait = max(0.0, (record.next_retry_at or self.clock.now()) - self.clock.now())
                if wait:
                    self.logger.emit(
                        "job.retry_sleep",
                        job_id=record.job_id,
                        delay_seconds=round(wait, 6),
                        next_attempt=record.attempt_count + 1,
                    )
                    self.clock.sleep(wait)

            record.status = JobStatus.RUNNING
            record.next_retry_at = None
            record.failure = None
            record.attempt_count += 1
            record.add_history(
                self.clock.now(), "provider.attempt_started", attempt=record.attempt_count
            )
            self.store.save(record)
            self.logger.emit(
                "provider.attempt_started",
                job_id=record.job_id,
                attempt=record.attempt_count,
                timeout_seconds=float(record.request.timeout_seconds),
            )

            try:
                raw_result = self.provider.generate(
                    record.request, timeout_seconds=float(record.request.timeout_seconds)
                )
                result = ProviderResult.from_mapping(raw_result)
            except TokenExpired:
                if record.refresh_count >= self.retry_policy.max_token_refreshes:
                    return self._dead_letter(
                        record,
                        code="token_refresh_exhausted",
                        message="provider credential remained expired after the refresh budget",
                        retryable=False,
                    )
                self.logger.emit(
                    "provider.token_expired",
                    level="WARNING",
                    job_id=record.job_id,
                    refresh_attempt=record.refresh_count + 1,
                )
                try:
                    self.provider.refresh_token()
                except Exception as exc:
                    return self._dead_letter(
                        record,
                        code="token_refresh_failed",
                        message=f"credential refresh failed ({type(exc).__name__})",
                        retryable=False,
                    )
                record.refresh_count += 1
                record.status = JobStatus.PENDING
                record.add_history(
                    self.clock.now(),
                    "provider.token_refreshed",
                    refresh_count=record.refresh_count,
                )
                self.store.save(record)
                self.logger.emit(
                    "provider.token_refreshed",
                    job_id=record.job_id,
                    refresh_count=record.refresh_count,
                )
                continue
            except MalformedProviderOutput as exc:
                return self._dead_letter(
                    record,
                    code="malformed_provider_output",
                    message=str(exc),
                    retryable=False,
                )
            except ProviderTimeout:
                terminal = self._handle_retryable_failure(
                    record,
                    code="provider_timeout",
                    message=f"provider exceeded the {record.request.timeout_seconds:.2f}s deadline",
                )
                if terminal is not None:
                    return terminal
                continue
            except TransientProviderError:
                terminal = self._handle_retryable_failure(
                    record,
                    code="transient_provider_error",
                    message="provider reported a retryable transient failure",
                )
                if terminal is not None:
                    return terminal
                continue
            except ProviderError as exc:
                return self._dead_letter(
                    record,
                    code="provider_error",
                    message=f"provider reported a non-retryable failure ({type(exc).__name__})",
                    retryable=False,
                )
            except Exception as exc:
                return self._dead_letter(
                    record,
                    code="unexpected_provider_error",
                    message=f"provider raised an unexpected {type(exc).__name__}",
                    retryable=False,
                )

            record.result = result
            if record.request.requires_approval:
                record.status = JobStatus.AWAITING_APPROVAL
                event = "job.awaiting_approval"
            else:
                record.status = JobStatus.SUCCEEDED
                event = "job.succeeded"
            record.add_history(
                self.clock.now(),
                event,
                confidence=float(result.confidence),
            )
            self.store.save(record)
            self.logger.emit(
                event,
                job_id=record.job_id,
                status=record.status.value,
                attempt_count=record.attempt_count,
                confidence=float(result.confidence),
            )
            return record

    def _handle_retryable_failure(
        self, record: JobRecord, *, code: str, message: str
    ) -> JobRecord | None:
        if record.attempt_count >= self.retry_policy.max_attempts:
            return self._dead_letter(record, code=code, message=message, retryable=True)
        retry_number = record.attempt_count
        jitter_value = self.jitter.sample(record.job_id, retry_number)
        delay = self.retry_policy.delay(retry_number, jitter_value)
        record.status = JobStatus.RETRY_WAIT
        record.next_retry_at = self.clock.now() + delay
        record.failure = FailureInfo(code=code, message=message, retryable=True)
        record.add_history(
            self.clock.now(),
            "job.retry_scheduled",
            failure_code=code,
            delay_seconds=delay,
            next_attempt=record.attempt_count + 1,
        )
        self.store.save(record)
        self.logger.emit(
            "job.retry_scheduled",
            level="WARNING",
            job_id=record.job_id,
            failure_code=code,
            delay_seconds=delay,
            next_attempt=record.attempt_count + 1,
        )
        return None

    def _dead_letter(
        self,
        record: JobRecord,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> JobRecord:
        record.status = JobStatus.DEAD_LETTER
        record.next_retry_at = None
        record.failure = FailureInfo(code=code, message=message, retryable=retryable)
        record.add_history(
            self.clock.now(),
            "job.dead_lettered",
            failure_code=code,
            retryable=retryable,
        )
        self.store.save(record)
        self.logger.emit(
            "job.dead_lettered",
            level="ERROR",
            job_id=record.job_id,
            failure_code=code,
            retryable=retryable,
            attempt_count=record.attempt_count,
        )
        return record

    def approve(self, job_id: str, *, actor: str, note: str = "") -> JobRecord:
        record = self.get(job_id)
        if not isinstance(actor, str):
            raise ValidationError("approval actor must be a string")
        if not isinstance(note, str):
            raise ValidationError("approval note must be a string")
        actor = actor.strip()
        if not actor or len(actor) > 128:
            raise ValidationError("approval actor must be 1-128 characters")
        if len(note) > 500:
            raise ValidationError("approval note cannot exceed 500 characters")
        if record.status != JobStatus.AWAITING_APPROVAL or record.result is None:
            raise InvalidTransition(
                f"job must be awaiting_approval before approval; current={record.status.value}"
            )
        record.status = JobStatus.SUCCEEDED
        record.approved_by = actor
        record.add_history(
            self.clock.now(),
            "job.approved",
            actor=actor,
            note=note,
        )
        self.store.save(record)
        self.logger.emit(
            "job.approved",
            job_id=record.job_id,
            status=record.status.value,
            actor=actor,
        )
        return record

    def reject(self, job_id: str, *, actor: str, reason: str) -> JobRecord:
        record = self.get(job_id)
        if not isinstance(actor, str):
            raise ValidationError("rejection actor must be a string")
        if not isinstance(reason, str):
            raise ValidationError("rejection reason must be a string")
        actor = actor.strip()
        reason = reason.strip()
        if not actor or len(actor) > 128:
            raise ValidationError("rejection actor must be 1-128 characters")
        if not reason or len(reason) > 500:
            raise ValidationError("rejection reason must be 1-500 characters")
        if record.status != JobStatus.AWAITING_APPROVAL:
            raise InvalidTransition(
                f"job must be awaiting_approval before rejection; current={record.status.value}"
            )
        return self._dead_letter(
            record,
            code="human_rejected",
            message=f"rejected by {actor}: {reason}",
            retryable=False,
        )
