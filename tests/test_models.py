from __future__ import annotations

import unittest

from reliable_automation.errors import MalformedProviderOutput, ValidationError
from reliable_automation.models import JobRecord, JobRequest, ProviderResult


class JobRequestTests(unittest.TestCase):
    def test_valid_request_has_deterministic_identity(self) -> None:
        first = JobRequest("order:alpha", "draft an email", {"tone": "brief"})
        second = JobRequest("order:alpha", "draft an email", {"tone": "brief"})
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_request_payload_is_a_deeply_immutable_validated_snapshot(self) -> None:
        source = {"nested": {"items": [1]}}
        request = JobRequest("snapshot:test", "preserve identity", source)
        fingerprint = request.fingerprint

        source["nested"]["items"].append(2)
        self.assertEqual(request.payload, {"nested": {"items": [1]}})
        self.assertEqual(request.fingerprint, fingerprint)
        with self.assertRaises(TypeError):
            request.payload["nested"]["items"].append(3)

        exported = request.to_dict()
        exported["payload"]["nested"]["items"].append(4)
        self.assertEqual(request.payload, {"nested": {"items": [1]}})
        self.assertEqual(request.fingerprint, fingerprint)

    def test_rejects_invalid_key_and_objective(self) -> None:
        with self.assertRaises(ValidationError):
            JobRequest("x", "valid", {})
        with self.assertRaises(ValidationError):
            JobRequest("valid:key", "  ", {})

    def test_rejects_non_json_and_non_finite_payloads(self) -> None:
        with self.assertRaises(ValidationError):
            JobRequest("valid:key", "valid", {"bad": object()})
        with self.assertRaises(ValidationError):
            JobRequest("valid:key", "valid", {"bad": float("nan")})

    def test_rejects_unsafe_timeout(self) -> None:
        with self.assertRaises(ValidationError):
            JobRequest("valid:key", "valid", {}, timeout_seconds=0.01)


class ProviderResultTests(unittest.TestCase):
    def test_parses_strict_provider_contract(self) -> None:
        result = ProviderResult.from_mapping(
            {"content": "ready", "confidence": 0.75, "metadata": {"safe": True}}
        )
        self.assertEqual(result.content, "ready")

    def test_provider_metadata_is_a_deeply_immutable_snapshot(self) -> None:
        metadata = {"trace": {"steps": ["validated"]}}
        result = ProviderResult("ready", 0.75, metadata)
        metadata["trace"]["steps"].append("mutated")
        self.assertEqual(result.metadata, {"trace": {"steps": ["validated"]}})
        with self.assertRaises(TypeError):
            result.metadata["trace"]["steps"].append("blocked")

    def test_rejects_missing_or_invalid_fields(self) -> None:
        with self.assertRaises(MalformedProviderOutput):
            ProviderResult.from_mapping({"content": "missing confidence"})
        with self.assertRaises(MalformedProviderOutput):
            ProviderResult.from_mapping({"content": "bad", "confidence": 2.0})

    def test_job_record_round_trip_validates_fingerprint(self) -> None:
        request = JobRequest("round:trip", "validate persistence", {"n": 1})
        record = JobRecord.new(request, 123.0)
        loaded = JobRecord.from_dict(record.to_dict())
        self.assertEqual(loaded.to_dict(), record.to_dict())

    def test_job_record_rejects_impossible_persisted_state(self) -> None:
        request = JobRequest("state:invalid", "validate persistence", {"n": 1})
        raw = JobRecord.new(request, 123.0).to_dict()
        raw["status"] = "succeeded"
        raw["attempt_count"] = -1

        with self.assertRaises(ValidationError):
            JobRecord.from_dict(raw)

    def test_job_record_requires_strict_persisted_field_types(self) -> None:
        request = JobRequest("state:types", "validate persistence", {"n": 1})
        raw = JobRecord.new(request, 123.0).to_dict()
        raw["attempt_count"] = "0"

        with self.assertRaises(ValidationError):
            JobRecord.from_dict(raw)


if __name__ == "__main__":
    unittest.main()
