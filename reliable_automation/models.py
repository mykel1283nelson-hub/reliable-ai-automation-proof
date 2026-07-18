"""Typed request, result, and persisted job-state models."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Union, cast

from .errors import MalformedProviderOutput, ValidationError

JSONScalar = Union[None, bool, int, float, str]
JSONValue = Union[JSONScalar, List["JSONValue"], Dict[str, "JSONValue"]]

_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


def _immutable(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    raise TypeError("validated JSON values are immutable")


class _FrozenDict(dict[str, JSONValue]):
    """JSON-compatible dictionary that rejects mutation after validation."""

    __setitem__ = _immutable  # type: ignore[assignment]
    __delitem__ = _immutable  # type: ignore[assignment]
    clear = _immutable  # type: ignore[assignment]
    pop = _immutable  # type: ignore[assignment]
    popitem = _immutable  # type: ignore[assignment]
    setdefault = _immutable  # type: ignore[assignment]
    update = _immutable  # type: ignore[assignment]
    __ior__ = _immutable  # type: ignore[assignment]


class _FrozenList(list[JSONValue]):
    """JSON-compatible list that rejects mutation after validation."""

    __setitem__ = _immutable  # type: ignore[assignment]
    __delitem__ = _immutable  # type: ignore[assignment]
    append = _immutable  # type: ignore[assignment]
    clear = _immutable  # type: ignore[assignment]
    extend = _immutable  # type: ignore[assignment]
    insert = _immutable  # type: ignore[assignment]
    pop = _immutable  # type: ignore[assignment]
    remove = _immutable  # type: ignore[assignment]
    reverse = _immutable  # type: ignore[assignment]
    sort = _immutable  # type: ignore[assignment]
    __iadd__ = _immutable  # type: ignore[assignment]
    __imul__ = _immutable  # type: ignore[assignment]


def _validate_json(value: Any, path: str = "$") -> None:
    """Reject values that cannot be represented safely in canonical JSON."""

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"{path} must not contain NaN or infinity")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"{path} contains a non-string object key")
            _validate_json(item, f"{path}.{key}")
        return
    raise ValidationError(f"{path} contains unsupported type {type(value).__name__}")


def canonical_json(value: Any) -> str:
    _validate_json(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _freeze_json(value: JSONValue) -> JSONValue:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: JSONValue) -> JSONValue:
    if isinstance(value, dict):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_thaw_json(item) for item in value]
    return value


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"

    @property
    def terminal(self) -> bool:
        return self in {JobStatus.SUCCEEDED, JobStatus.DEAD_LETTER}


@dataclass(frozen=True)
class JobRequest:
    """Validated input contract for one automation job."""

    idempotency_key: str
    objective: str
    payload: dict[str, JSONValue]
    requires_approval: bool = True
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not isinstance(self.idempotency_key, str) or not _IDEMPOTENCY_PATTERN.fullmatch(
            self.idempotency_key
        ):
            raise ValidationError(
                "idempotency_key must be 3-128 safe characters and start with a letter or digit"
            )
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValidationError("objective must be a non-empty string")
        if len(self.objective) > 2_000:
            raise ValidationError("objective cannot exceed 2,000 characters")
        if not isinstance(self.payload, dict):
            raise ValidationError("payload must be a JSON object")
        _validate_json(self.payload, "$.payload")
        object.__setattr__(self, "payload", _freeze_json(self.payload))
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise ValidationError("timeout_seconds must be numeric")
        if not 0.05 <= float(self.timeout_seconds) <= 300.0:
            raise ValidationError("timeout_seconds must be between 0.05 and 300")
        if not isinstance(self.requires_approval, bool):
            raise ValidationError("requires_approval must be a boolean")

    @property
    def job_id(self) -> str:
        digest = hashlib.sha256(self.idempotency_key.encode("utf-8")).hexdigest()
        return f"job_{digest[:24]}"

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()
        return digest

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "idempotency_key": self.idempotency_key,
            "objective": self.objective,
            "payload": _thaw_json(self.payload),
            "requires_approval": self.requires_approval,
            "timeout_seconds": float(self.timeout_seconds),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JobRequest":
        try:
            return cls(
                idempotency_key=value["idempotency_key"],
                objective=value["objective"],
                payload=value["payload"],
                requires_approval=value.get("requires_approval", True),
                timeout_seconds=value.get("timeout_seconds", 10.0),
            )
        except KeyError as exc:
            raise ValidationError(f"request missing required field: {exc.args[0]}") from exc


@dataclass(frozen=True)
class ProviderResult:
    """Strictly parsed provider output."""

    content: str
    confidence: float
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or not self.content.strip():
            raise MalformedProviderOutput("provider output content must be a non-empty string")
        if len(self.content) > 100_000:
            raise MalformedProviderOutput("provider output content exceeds 100,000 characters")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise MalformedProviderOutput("provider confidence must be numeric")
        if not math.isfinite(float(self.confidence)) or not 0.0 <= float(self.confidence) <= 1.0:
            raise MalformedProviderOutput("provider confidence must be between 0 and 1")
        if not isinstance(self.metadata, dict):
            raise MalformedProviderOutput("provider metadata must be a JSON object")
        try:
            _validate_json(self.metadata, "$.metadata")
        except ValidationError as exc:
            raise MalformedProviderOutput(str(exc)) from exc
        object.__setattr__(self, "metadata", _freeze_json(self.metadata))

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "content": self.content,
            "confidence": float(self.confidence),
            "metadata": _thaw_json(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "ProviderResult":
        if not isinstance(value, Mapping):
            raise MalformedProviderOutput("provider output must be an object")
        missing = {"content", "confidence"} - set(value)
        if missing:
            raise MalformedProviderOutput(
                "provider output missing required fields: " + ", ".join(sorted(missing))
            )
        try:
            return cls(
                content=value["content"],
                confidence=value["confidence"],
                metadata=value.get("metadata", {}),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, MalformedProviderOutput):
                raise
            raise MalformedProviderOutput(str(exc)) from exc


@dataclass(frozen=True)
class FailureInfo:
    code: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code or len(self.code) > 128:
            raise ValidationError("failure code must be a non-empty string up to 128 characters")
        if not isinstance(self.message, str) or not self.message or len(self.message) > 2_000:
            raise ValidationError("failure message must be a non-empty string up to 2,000 characters")
        if not isinstance(self.retryable, bool):
            raise ValidationError("failure retryable flag must be a boolean")

    def to_dict(self) -> dict[str, JSONValue]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FailureInfo":
        if not isinstance(value, Mapping):
            raise ValidationError("persisted failure must be an object")
        try:
            return cls(
                code=value["code"],
                message=value["message"],
                retryable=value["retryable"],
            )
        except KeyError as exc:
            raise ValidationError(f"failure missing required field: {exc.args[0]}") from exc


@dataclass
class JobRecord:
    """Durable state machine record."""

    job_id: str
    request_fingerprint: str
    request: JobRequest
    status: JobStatus
    attempt_count: int
    refresh_count: int
    created_at: float
    updated_at: float
    next_retry_at: float | None = None
    result: ProviderResult | None = None
    failure: FailureInfo | None = None
    approved_by: str | None = None
    history: list[dict[str, JSONValue]] = field(default_factory=list)

    @classmethod
    def new(cls, request: JobRequest, now: float) -> "JobRecord":
        return cls(
            job_id=request.job_id,
            request_fingerprint=request.fingerprint,
            request=request,
            status=JobStatus.PENDING,
            attempt_count=0,
            refresh_count=0,
            created_at=now,
            updated_at=now,
            history=[{"at": now, "event": "job.enqueued", "status": JobStatus.PENDING.value}],
        )

    def add_history(self, now: float, event: str, **fields: JSONValue) -> None:
        if not isinstance(event, str) or not event:
            raise ValidationError("history event must be a non-empty string")
        entry: dict[str, JSONValue] = {"at": now, "event": event, "status": self.status.value}
        entry.update(fields)
        _validate_json(entry, "$.history[]")
        self.history.append(cast(dict[str, JSONValue], _thaw_json(entry)))
        self.updated_at = now

    def validate(self) -> None:
        """Fail closed if an in-memory or persisted state violates invariants."""

        if not isinstance(self.job_id, str) or self.job_id != self.request.job_id:
            raise ValidationError("persisted job_id does not match idempotency key")
        if (
            not isinstance(self.request_fingerprint, str)
            or self.request_fingerprint != self.request.fingerprint
        ):
            raise ValidationError("persisted request fingerprint mismatch")
        if not isinstance(self.status, JobStatus):
            raise ValidationError("persisted status is invalid")
        for name, count in (
            ("attempt_count", self.attempt_count),
            ("refresh_count", self.refresh_count),
        ):
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValidationError(f"{name} must be a non-negative integer")
        for name, timestamp in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                raise ValidationError(f"{name} must be numeric")
            if not math.isfinite(float(timestamp)):
                raise ValidationError(f"{name} must be finite")
        if self.updated_at < self.created_at:
            raise ValidationError("updated_at cannot precede created_at")
        if self.next_retry_at is not None:
            if isinstance(self.next_retry_at, bool) or not isinstance(
                self.next_retry_at, (int, float)
            ):
                raise ValidationError("next_retry_at must be numeric or null")
            if not math.isfinite(float(self.next_retry_at)):
                raise ValidationError("next_retry_at must be finite")
        if self.status == JobStatus.RETRY_WAIT:
            if self.next_retry_at is None or self.failure is None:
                raise ValidationError("retry_wait requires next_retry_at and failure")
        elif self.next_retry_at is not None:
            raise ValidationError("next_retry_at is only valid in retry_wait")
        if self.status == JobStatus.DEAD_LETTER:
            if self.failure is None:
                raise ValidationError("dead_letter requires failure details")
        elif self.status != JobStatus.RETRY_WAIT and self.failure is not None:
            raise ValidationError("failure details are only valid for retry_wait or dead_letter")
        if self.status in {JobStatus.AWAITING_APPROVAL, JobStatus.SUCCEEDED}:
            if self.result is None:
                raise ValidationError(f"{self.status.value} requires a validated result")
        elif self.status != JobStatus.DEAD_LETTER and self.result is not None:
            raise ValidationError("result is not valid before approval or success")
        if self.approved_by is not None:
            if (
                not isinstance(self.approved_by, str)
                or not self.approved_by
                or len(self.approved_by) > 128
            ):
                raise ValidationError("approved_by must be null or a 1-128 character string")
            if self.status != JobStatus.SUCCEEDED:
                raise ValidationError("approved_by is only valid for succeeded jobs")
        if (
            self.status == JobStatus.SUCCEEDED
            and self.request.requires_approval
            and self.approved_by is None
        ):
            raise ValidationError("approval-required succeeded job must name its approver")
        if not isinstance(self.history, list) or not self.history:
            raise ValidationError("history must be a non-empty JSON array")
        _validate_json(self.history, "$.history")

    def to_dict(self) -> dict[str, JSONValue]:
        self.validate()
        return {
            "schema_version": 1,
            "job_id": self.job_id,
            "request_fingerprint": self.request_fingerprint,
            "request": self.request.to_dict(),
            "status": self.status.value,
            "attempt_count": self.attempt_count,
            "refresh_count": self.refresh_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_retry_at": self.next_retry_at,
            "result": self.result.to_dict() if self.result else None,
            "failure": self.failure.to_dict() if self.failure else None,
            "approved_by": self.approved_by,
            "history": _thaw_json(cast(JSONValue, self.history)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JobRecord":
        if not isinstance(value, Mapping):
            raise ValidationError("persisted job record must be an object")
        if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
            raise ValidationError("unsupported job record schema_version")
        try:
            attempt_count = value["attempt_count"]
            refresh_count = value["refresh_count"]
            created_at = value["created_at"]
            updated_at = value["updated_at"]
            next_retry_at = value.get("next_retry_at")
            if isinstance(attempt_count, bool) or not isinstance(attempt_count, int):
                raise ValidationError("attempt_count must be an integer")
            if isinstance(refresh_count, bool) or not isinstance(refresh_count, int):
                raise ValidationError("refresh_count must be an integer")
            for name, timestamp in (("created_at", created_at), ("updated_at", updated_at)):
                if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                    raise ValidationError(f"{name} must be numeric")
            if next_retry_at is not None and (
                isinstance(next_retry_at, bool) or not isinstance(next_retry_at, (int, float))
            ):
                raise ValidationError("next_retry_at must be numeric or null")
            result_value = value.get("result")
            failure_value = value.get("failure")
            history = value.get("history")
            if not isinstance(history, list):
                raise ValidationError("history must be an array")
            record = cls(
                job_id=value["job_id"],
                request_fingerprint=value["request_fingerprint"],
                request=JobRequest.from_dict(value["request"]),
                status=JobStatus(value["status"]),
                attempt_count=attempt_count,
                refresh_count=refresh_count,
                created_at=float(created_at),
                updated_at=float(updated_at),
                next_retry_at=float(next_retry_at) if next_retry_at is not None else None,
                result=(
                    ProviderResult.from_mapping(result_value) if result_value is not None else None
                ),
                failure=(FailureInfo.from_dict(failure_value) if failure_value is not None else None),
                approved_by=value.get("approved_by"),
                history=cast(
                    list[dict[str, JSONValue]],
                    _thaw_json(cast(JSONValue, history)),
                ),
            )
        except KeyError as exc:
            raise ValidationError(f"job record missing required field: {exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError(f"invalid persisted job record: {exc}") from exc
        record.validate()
        return record
