from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from reliable_automation.cli import SCENARIOS, main
from reliable_automation.clock import ManualClock
from reliable_automation.errors import ValidationError
from reliable_automation.models import JobRecord, JobRequest
from reliable_automation.observability import JsonlEventLogger
from reliable_automation.storage import FileJobStore


class StorageAndObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_store_round_trip_is_atomic_and_leaves_no_temp_file(self) -> None:
        store = FileJobStore(self.root)
        request = JobRequest("store:test", "persist", {"safe": True})
        record = JobRecord.new(request, 1.0)
        path = store.save(record)
        self.assertTrue(path.exists())
        self.assertEqual(store.load(record.job_id).to_dict(), record.to_dict())
        self.assertEqual(list((self.root / "jobs").glob("*.tmp")), [])

    def test_corrupt_record_fails_closed(self) -> None:
        store = FileJobStore(self.root)
        request = JobRequest("store:corrupt", "persist", {})
        path = store.record_path(request.job_id)
        path.write_text("not-json", encoding="utf-8")
        with self.assertRaises(ValidationError):
            store.load(request.job_id)

    def test_structured_logger_redacts_nested_secrets(self) -> None:
        stream = io.StringIO()
        logger = JsonlEventLogger(ManualClock(), stream=stream)
        logger.emit(
            "safe.event",
            access_token="do-not-print",
            token_refreshes=2,
            payload={
                "password": "hidden",
                "private_key": "also-hidden",
                "session_cookie": "cookie-hidden",
                "public": "visible",
            },
        )
        record = json.loads(stream.getvalue())
        self.assertEqual(record["access_token"], "[REDACTED]")
        self.assertEqual(record["payload"]["password"], "[REDACTED]")
        self.assertEqual(record["payload"]["private_key"], "[REDACTED]")
        self.assertEqual(record["payload"]["session_cookie"], "[REDACTED]")
        self.assertEqual(record["payload"]["public"], "visible")
        self.assertEqual(record["token_refreshes"], 2)
        self.assertNotIn("do-not-print", stream.getvalue())
        self.assertNotIn("also-hidden", stream.getvalue())
        self.assertNotIn("cookie-hidden", stream.getvalue())

    def test_demo_reset_refuses_nonempty_unmanaged_directory(self) -> None:
        state_dir = self.root / "unmanaged"
        state_dir.mkdir()
        sentinel = state_dir / "keep-me.txt"
        sentinel.write_text("important", encoding="utf-8")
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = main(
                ["--state-dir", str(state_dir), "demo", "success", "--reset"]
            )

        self.assertEqual(exit_code, 2)
        self.assertTrue(sentinel.exists())
        self.assertIn("unmanaged directory", stderr.getvalue())

    def test_demo_reset_replaces_only_previously_managed_state(self) -> None:
        state_dir = self.root / "managed"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(["--state-dir", str(state_dir), "demo", "success"]),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "demo",
                        "success",
                        "--reset",
                    ]
                ),
                0,
            )

        summary = json.loads((state_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["provider_calls"], 1)
        events = (state_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum('"event":"demo.summary"' in line for line in events), 1)
        marker = json.loads(
            (state_dir / ".reliable-automation-demo-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marker["format"], "reliable-ai-automation-demo-state")

    def test_demo_repeat_requires_reset_instead_of_rewriting_call_counts(self) -> None:
        state_dir = self.root / "repeat"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(["--state-dir", str(state_dir), "demo", "success"]),
                0,
            )
        before = (state_dir / "summary.json").read_text(encoding="utf-8")
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = main(["--state-dir", str(state_dir), "demo", "success"])

        self.assertEqual(exit_code, 2)
        self.assertIn("rerun with --reset", stderr.getvalue())
        self.assertEqual((state_dir / "summary.json").read_text(encoding="utf-8"), before)

    def test_cli_all_scenarios_produces_durable_summaries(self) -> None:
        state_dir = self.root / "cli-demo"
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = main(["--state-dir", str(state_dir), "demo", "all"])
        self.assertEqual(exit_code, 0)
        summary = json.loads((state_dir / "all-scenarios-summary.json").read_text())
        self.assertEqual([item["scenario"] for item in summary], list(SCENARIOS))
        by_name = {item["scenario"]: item for item in summary}
        self.assertEqual(by_name["timeout"]["final_status"], "dead_letter")
        self.assertEqual(by_name["malformed"]["final_status"], "dead_letter")
        self.assertEqual(by_name["retry"]["retry_sleeps"], [0.25, 0.5])
        self.assertEqual(by_name["token-refresh"]["token_refreshes"], 1)
        self.assertEqual(by_name["idempotency"]["provider_calls"], 1)

    def test_cli_commands_resume_across_separate_invocations(self) -> None:
        state_dir = self.root / "manual-cli"
        request = JobRequest(
            "cli:separate:v1",
            "prepare a safe summary",
            {"account_id": "example"},
            timeout_seconds=1.0,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "enqueue",
                        "--idempotency-key",
                        request.idempotency_key,
                        "--objective",
                        request.objective,
                        "--payload",
                        '{"account_id":"example"}',
                        "--timeout",
                        "1.0",
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(["--state-dir", str(state_dir), "run", request.job_id]),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "--state-dir",
                        str(state_dir),
                        "approve",
                        request.job_id,
                        "--actor",
                        "integration-reviewer",
                    ]
                ),
                0,
            )
        record = FileJobStore(state_dir).load(request.job_id)
        self.assertEqual(record.status.value, "succeeded")
        self.assertEqual(record.approved_by, "integration-reviewer")


if __name__ == "__main__":
    unittest.main()
