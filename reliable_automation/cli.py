"""Command-line entry point and reproducible demonstration scenarios."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence, cast

from .clock import ManualClock, SystemClock
from .engine import MidpointJitter, ReliableAutomationEngine, RetryPolicy
from .errors import AutomationError, ValidationError
from .models import JSONValue, JobRequest, JobStatus
from .observability import JsonlEventLogger, stdout_logger
from .provider import ScriptedProvider
from .storage import FileJobStore

SCENARIOS = ("success", "retry", "token-refresh", "malformed", "timeout", "recovery", "idempotency")
_DEMO_MARKER_NAME = ".reliable-automation-demo-state.json"
_DEMO_MARKER = {"format": "reliable-ai-automation-demo-state", "version": 1}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _read_demo_marker(path: Path) -> bool:
    marker = path / _DEMO_MARKER_NAME
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return value == _DEMO_MARKER


def _prepare_demo_state(path: Path) -> None:
    """Create or validate a directory dedicated to generated demo state."""

    expanded = path.expanduser()
    resolved = expanded.resolve()
    if expanded.is_symlink():
        raise ValidationError(f"refusing symlink demo state path: {expanded}")
    if resolved.exists() and not resolved.is_dir():
        raise ValidationError(f"demo state path is not a directory: {resolved}")
    if resolved.exists():
        entries = list(resolved.iterdir())
        if entries and not _read_demo_marker(resolved):
            raise ValidationError(
                f"refusing non-empty unmanaged demo state directory: {resolved}"
            )
    resolved.mkdir(parents=True, exist_ok=True)
    _atomic_json(resolved / _DEMO_MARKER_NAME, _DEMO_MARKER)


def _safe_reset(path: Path) -> None:
    """Remove only a directory carrying this CLI's exact ownership marker."""

    expanded = path.expanduser()
    resolved = expanded.resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if resolved in forbidden or len(resolved.parts) < 4:
        raise ValidationError(f"refusing unsafe demo reset path: {resolved}")
    if expanded.is_symlink():
        raise ValidationError(f"refusing symlink demo reset path: {expanded}")
    if resolved.exists():
        if not resolved.is_dir():
            raise ValidationError(f"demo reset path is not a directory: {resolved}")
        if any(resolved.iterdir()) and not _read_demo_marker(resolved):
            raise ValidationError(
                f"refusing to reset non-empty unmanaged directory: {resolved}"
            )
        shutil.rmtree(resolved)


def _demo_has_output(path: Path, scenario: str) -> bool:
    """Return whether a selected demo would reuse stale generated evidence."""

    if scenario == "all":
        if (path / "all-scenarios-summary.json").exists():
            return True
        targets = [path / name for name in SCENARIOS]
    else:
        targets = [path]
    generated_names = ("events.jsonl", "summary.json", "jobs")
    return any(any((target / name).exists() for name in generated_names) for target in targets)


def _engine(
    state_dir: Path,
    provider: ScriptedProvider,
    *,
    clock: ManualClock | SystemClock,
    echo: bool = True,
) -> ReliableAutomationEngine:
    logger = stdout_logger(clock, state_dir / "events.jsonl") if echo else JsonlEventLogger(clock)
    return ReliableAutomationEngine(
        store=FileJobStore(state_dir),
        provider=provider,
        clock=clock,
        logger=logger,
        retry_policy=RetryPolicy(
            max_attempts=3,
            base_delay_seconds=0.25,
            max_delay_seconds=1.0,
            jitter_ratio=0.2,
            max_token_refreshes=1,
        ),
        jitter=MidpointJitter(),
    )


def _scenario_steps(name: str) -> list[str]:
    return {
        "success": ["success"],
        "retry": ["transient", "transient", "success"],
        "token-refresh": ["token_expired", "success"],
        "malformed": ["malformed"],
        "timeout": ["timeout", "timeout", "timeout"],
        "recovery": ["success"],
        "idempotency": ["success"],
    }[name]


def run_demo(name: str, state_dir: Path) -> dict[str, object]:
    clock = ManualClock()
    provider = ScriptedProvider(_scenario_steps(name))
    engine = _engine(state_dir, provider, clock=clock)
    request = JobRequest(
        idempotency_key=f"demo:{name}:v1",
        objective=f"produce a safe automation artifact for the {name} scenario",
        payload={"account_id": "example-001", "dry_run": True},
        requires_approval=True,
        timeout_seconds=1.5,
    )

    if name == "recovery":
        record = engine.enqueue(request)
        engine.logger.emit(
            "demo.process_stopped",
            level="WARNING",
            job_id=record.job_id,
            persisted_status=record.status.value,
        )
        # A new store/engine instance proves that in-memory state is not required.
        engine = _engine(state_dir, provider, clock=clock)
        engine.logger.emit("demo.process_restarted", job_id=record.job_id)
        record = engine.run(record.job_id)
    else:
        record = engine.submit(request)

    gate_observed = record.status == JobStatus.AWAITING_APPROVAL
    if gate_observed:
        engine.logger.emit(
            "demo.human_review",
            job_id=record.job_id,
            decision="approve",
            output_committed=False,
        )
        record = engine.approve(
            record.job_id,
            actor="demo-reviewer",
            note="deterministic sample approved",
        )

    if name == "idempotency":
        replay = engine.submit(request)
        if replay.job_id != record.job_id:
            raise AssertionError("idempotent replay produced a different job")

    summary: dict[str, Any] = {
        "scenario": name,
        "job_id": record.job_id,
        "final_status": record.status.value,
        "failure_code": record.failure.code if record.failure else None,
        "provider_calls": provider.call_count,
        "token_refreshes": provider.refresh_count,
        "retry_sleeps": clock.sleeps,
        "approval_gate_observed": gate_observed,
        "persisted_record": str(engine.store.record_path(record.job_id)),
    }
    _atomic_json(state_dir / "summary.json", summary)
    engine.logger.emit("demo.summary", **summary)
    return summary


def _parse_payload(raw: str) -> dict[str, JSONValue]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"payload is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValidationError("payload JSON must be an object")
    return cast(dict[str, JSONValue], value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m reliable_automation",
        description="Durable AI automation proof: validation, retries, approvals, and recovery.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("runtime"),
        help="durable job/log directory (default: ./runtime)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run deterministic proof scenarios")
    demo.add_argument("scenario", choices=(*SCENARIOS, "all"))
    demo.add_argument("--reset", action="store_true", help="remove only the selected demo state first")

    enqueue = subparsers.add_parser("enqueue", help="validate and persist a request without running it")
    enqueue.add_argument("--idempotency-key", required=True)
    enqueue.add_argument("--objective", required=True)
    enqueue.add_argument("--payload", default="{}", help="JSON object")
    enqueue.add_argument("--timeout", type=float, default=10.0)
    enqueue.add_argument("--no-approval", action="store_true")

    run = subparsers.add_parser("run", help="run or recover a persisted job with the demo adapter")
    run.add_argument("job_id")
    run.add_argument(
        "--provider-steps",
        default="success",
        help="comma-separated demo steps: success,transient,timeout,token_expired,malformed",
    )

    approve = subparsers.add_parser("approve", help="approve an output at the human gate")
    approve.add_argument("job_id")
    approve.add_argument("--actor", required=True)
    approve.add_argument("--note", default="")

    reject = subparsers.add_parser("reject", help="reject an output into the dead-letter state")
    reject.add_argument("job_id")
    reject.add_argument("--actor", required=True)
    reject.add_argument("--reason", required=True)

    show = subparsers.add_parser("show", help="print a persisted job record")
    show.add_argument("job_id")
    subparsers.add_parser("list", help="print a compact list of persisted jobs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    state_dir: Path = args.state_dir.expanduser()
    try:
        if args.command == "demo":
            if args.reset:
                _safe_reset(state_dir)
            _prepare_demo_state(state_dir)
            if not args.reset and _demo_has_output(state_dir.resolve(), args.scenario):
                raise ValidationError(
                    "demo state already contains generated output; rerun with --reset"
                )
            if args.scenario == "all":
                summaries = [run_demo(name, state_dir / name) for name in SCENARIOS]
                _atomic_json(state_dir / "all-scenarios-summary.json", summaries)
            else:
                run_demo(args.scenario, state_dir)
            return 0

        clock = SystemClock()
        provider_steps = (
            [item.strip() for item in args.provider_steps.split(",") if item.strip()]
            if args.command == "run"
            else []
        )
        provider = ScriptedProvider(provider_steps)
        engine = _engine(state_dir, provider, clock=clock)

        if args.command == "enqueue":
            request = JobRequest(
                idempotency_key=args.idempotency_key,
                objective=args.objective,
                payload=_parse_payload(args.payload),
                requires_approval=not args.no_approval,
                timeout_seconds=args.timeout,
            )
            record = engine.enqueue(request)
        elif args.command == "run":
            record = engine.run(args.job_id)
        elif args.command == "approve":
            record = engine.approve(args.job_id, actor=args.actor, note=args.note)
        elif args.command == "reject":
            record = engine.reject(args.job_id, actor=args.actor, reason=args.reason)
        elif args.command == "show":
            record = engine.get(args.job_id)
            print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
            return 0
        elif args.command == "list":
            rows = [
                {
                    "job_id": item.job_id,
                    "status": item.status.value,
                    "attempt_count": item.attempt_count,
                    "failure_code": item.failure.code if item.failure else None,
                }
                for item in engine.store.list_records()
            ]
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        else:  # pragma: no cover - argparse guarantees this branch is unreachable.
            parser.error(f"unknown command: {args.command}")
            return 2

        engine.logger.emit(
            "cli.result",
            job_id=record.job_id,
            status=record.status.value,
            attempt_count=record.attempt_count,
            failure_code=record.failure.code if record.failure else None,
        )
        return 0 if record.status != JobStatus.DEAD_LETTER else 1
    except AutomationError as exc:
        print(
            json.dumps(
                {"level": "ERROR", "event": "cli.error", "error_type": type(exc).__name__, "message": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
