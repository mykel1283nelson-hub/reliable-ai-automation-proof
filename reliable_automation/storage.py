"""Crash-safe JSON persistence for automation job records."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .errors import ValidationError
from .models import JobRecord, canonical_json


class FileJobStore:
    """One atomically replaced JSON document per deterministic job identifier."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.jobs_dir = root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def record_path(self, job_id: str) -> Path:
        """Return the validated filesystem location for ``job_id``."""

        if not job_id.startswith("job_") or not job_id[4:].isalnum():
            raise ValidationError("invalid job identifier")
        return self.jobs_dir / f"{job_id}.json"

    def save(self, record: JobRecord) -> Path:
        destination = self.record_path(record.job_id)
        serialized = canonical_json(record.to_dict()) + "\n"
        with self._lock:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{record.job_id}.", suffix=".tmp", dir=self.jobs_dir
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, destination)
                directory_fd = os.open(self.jobs_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
        return destination

    def load(self, job_id: str) -> JobRecord | None:
        path = self.record_path(job_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"corrupt persisted job record: {path.name}") from exc
        if not isinstance(value, dict):
            raise ValidationError(f"persisted job record must be an object: {path.name}")
        return JobRecord.from_dict(value)

    def list_records(self) -> list[JobRecord]:
        records: list[JobRecord] = []
        for path in sorted(self.jobs_dir.glob("job_*.json")):
            job_id = path.stem
            record = self.load(job_id)
            if record is not None:
                records.append(record)
        return records
