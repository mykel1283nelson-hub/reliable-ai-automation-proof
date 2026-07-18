from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from reliable_automation.clock import ManualClock
from reliable_automation.engine import MidpointJitter, ReliableAutomationEngine, RetryPolicy
from reliable_automation.errors import IdempotencyConflict, InvalidTransition, ValidationError
from reliable_automation.models import JobRequest, JobStatus
from reliable_automation.observability import JsonlEventLogger
from reliable_automation.provider import ScriptedProvider
from reliable_automation.storage import FileJobStore


class ReliableAutomationEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = ManualClock()
        self.stream = io.StringIO()

    def make_engine(
        self,
        steps: list[str | dict[str, object]],
        *,
        root: Path | None = None,
        refresh_fails: bool = False,
        policy: RetryPolicy | None = None,
    ) -> tuple[ReliableAutomationEngine, ScriptedProvider]:
        provider = ScriptedProvider(steps, refresh_fails=refresh_fails)
        engine = ReliableAutomationEngine(
            store=FileJobStore(root or self.root),
            provider=provider,
            clock=self.clock,
            logger=JsonlEventLogger(self.clock, stream=self.stream),
            retry_policy=policy
            or RetryPolicy(
                max_attempts=3,
                base_delay_seconds=0.25,
                max_delay_seconds=1.0,
                jitter_ratio=0.2,
                max_token_refreshes=1,
            ),
            jitter=MidpointJitter(),
        )
        return engine, provider

    @staticmethod
    def request(
        *,
        key: str = "order:123",
        payload: dict[str, object] | None = None,
        approval: bool = True,
    ) -> JobRequest:
        return JobRequest(
            idempotency_key=key,
            objective="prepare an account summary",
            payload=payload or {"account_id": "example"},
            requires_approval=approval,
            timeout_seconds=2.0,
        )

    def test_happy_path_stops_at_approval_gate_then_commits(self) -> None:
        engine, provider = self.make_engine(["success"])
        record = engine.submit(self.request())
        self.assertEqual(record.status, JobStatus.AWAITING_APPROVAL)
        self.assertIsNone(record.approved_by)
        self.assertEqual(engine.run(record.job_id).status, JobStatus.AWAITING_APPROVAL)
        approved = engine.approve(record.job_id, actor="reviewer")
        self.assertEqual(approved.status, JobStatus.SUCCEEDED)
        self.assertEqual(approved.approved_by, "reviewer")
        with self.assertRaises(InvalidTransition):
            engine.approve(record.job_id, actor="reviewer")
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(engine.get(record.job_id).status, JobStatus.SUCCEEDED)

    def test_idempotent_replay_never_calls_provider_twice(self) -> None:
        engine, provider = self.make_engine(["success"])
        request = self.request()
        record = engine.submit(request)
        engine.approve(record.job_id, actor="reviewer")
        replay = engine.submit(request)
        self.assertEqual(replay.status, JobStatus.SUCCEEDED)
        self.assertEqual(provider.call_count, 1)
        self.assertIn("job.idempotency_hit", self.stream.getvalue())

    def test_same_key_different_request_is_a_conflict(self) -> None:
        engine, _ = self.make_engine([])
        engine.enqueue(self.request(payload={"version": 1}))
        with self.assertRaises(IdempotencyConflict):
            engine.enqueue(self.request(payload={"version": 2}))

    def test_transient_failures_back_off_then_succeed(self) -> None:
        engine, provider = self.make_engine(["transient", "transient", "success"])
        record = engine.submit(self.request())
        self.assertEqual(record.status, JobStatus.AWAITING_APPROVAL)
        self.assertEqual(record.attempt_count, 3)
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(self.clock.sleeps, [0.25, 0.5])

    def test_expired_token_refreshes_and_replays(self) -> None:
        engine, provider = self.make_engine(["token_expired", "success"])
        record = engine.submit(self.request())
        self.assertEqual(record.status, JobStatus.AWAITING_APPROVAL)
        self.assertEqual(provider.refresh_count, 1)
        self.assertEqual(record.refresh_count, 1)
        self.assertEqual(record.attempt_count, 2)
        self.assertEqual(self.clock.sleeps, [])

    def test_failed_token_refresh_dead_letters(self) -> None:
        engine, provider = self.make_engine(["token_expired"], refresh_fails=True)
        record = engine.submit(self.request())
        self.assertEqual(record.status, JobStatus.DEAD_LETTER)
        self.assertEqual(record.failure.code, "token_refresh_failed")
        self.assertEqual(provider.refresh_count, 1)

    def test_malformed_output_is_not_retried(self) -> None:
        engine, provider = self.make_engine(["malformed"])
        record = engine.submit(self.request())
        self.assertEqual(record.status, JobStatus.DEAD_LETTER)
        self.assertEqual(record.failure.code, "malformed_provider_output")
        self.assertEqual(provider.call_count, 1)

    def test_timeouts_exhaust_budget_into_dead_letter(self) -> None:
        engine, provider = self.make_engine(["timeout", "timeout", "timeout"])
        record = engine.submit(self.request())
        self.assertEqual(record.status, JobStatus.DEAD_LETTER)
        self.assertEqual(record.failure.code, "provider_timeout")
        self.assertTrue(record.failure.retryable)
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(self.clock.sleeps, [0.25, 0.5])
        self.assertEqual(provider.seen_timeouts, [2.0, 2.0, 2.0])

    def test_pending_job_recovers_with_new_engine_instance(self) -> None:
        first_engine, _ = self.make_engine([])
        queued = first_engine.enqueue(self.request())
        second_engine, second_provider = self.make_engine(["success"])
        recovered = second_engine.run(queued.job_id)
        self.assertEqual(recovered.status, JobStatus.AWAITING_APPROVAL)
        self.assertEqual(second_provider.call_count, 1)

    def test_interrupted_running_job_replays_with_same_idempotency_key(self) -> None:
        first_engine, _ = self.make_engine([])
        record = first_engine.enqueue(self.request())
        record.status = JobStatus.RUNNING
        record.attempt_count = 1
        first_engine.store.save(record)
        second_engine, provider = self.make_engine(["success"])
        recovered = second_engine.run(record.job_id)
        self.assertEqual(recovered.status, JobStatus.AWAITING_APPROVAL)
        self.assertEqual(provider.seen_idempotency_keys, [record.request.idempotency_key])
        self.assertIn("job.recovered", [item["event"] for item in recovered.history])

    def test_no_approval_request_can_finish_automatically(self) -> None:
        engine, _ = self.make_engine(["success"])
        record = engine.submit(self.request(approval=False))
        self.assertEqual(record.status, JobStatus.SUCCEEDED)
        self.assertIsNone(record.approved_by)

    def test_human_rejection_is_a_named_dead_letter(self) -> None:
        engine, _ = self.make_engine(["success"])
        record = engine.submit(self.request())
        rejected = engine.reject(record.job_id, actor="reviewer", reason="facts need revision")
        self.assertEqual(rejected.status, JobStatus.DEAD_LETTER)
        self.assertEqual(rejected.failure.code, "human_rejected")

    def test_retry_policy_rejects_non_finite_or_ambiguous_values(self) -> None:
        with self.assertRaises(ValidationError):
            RetryPolicy(base_delay_seconds=float("nan"))
        with self.assertRaises(ValidationError):
            RetryPolicy(max_delay_seconds=float("inf"))
        with self.assertRaises(ValidationError):
            RetryPolicy(max_attempts=True)
        with self.assertRaises(ValidationError):
            RetryPolicy(max_token_refreshes=False)
        with self.assertRaises(ValidationError):
            RetryPolicy().delay(1, float("nan"))

    def test_human_decision_fields_fail_with_domain_validation(self) -> None:
        engine, _ = self.make_engine(["success"])
        record = engine.submit(self.request())
        with self.assertRaises(ValidationError):
            engine.approve(record.job_id, actor=None)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            engine.reject(record.job_id, actor="reviewer", reason=None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
