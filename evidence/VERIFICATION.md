# Verification Evidence

Generated locally on 2026-07-18 with Apple `/usr/bin/python3` 3.9.6 and Homebrew Python 3.14.6. No network requests, credentials, or customer data were used.

## Commands

```bash
python3 -m compileall -q reliable_automation tests
python3 -m unittest discover -s tests -v
python3 -m reliable_automation --state-dir evidence/sample-run demo all --reset
```

## Results

- Compilation: pass
- Tests: **33 passed, 0 failed on Python 3.9.6**
- Cross-version repeat: **33 passed, 0 failed on Python 3.14.6**
- Demo scenarios: **7 completed**
- Successful/approved scenarios: `success`, `retry`, `token-refresh`, `recovery`, `idempotency`
- Expected failure-isolation scenarios: `malformed`, `timeout` reached `dead_letter`
- Idempotency proof: repeated identical request, one provider call
- Retry proof: two transient failures, sleeps of 0.25 and 0.5 simulated seconds, then success
- Credential proof: one token refresh, then success
- Recovery proof: persisted `pending` state loaded by a new engine instance, then success
- State-integrity proof: impossible persisted states and ambiguous field types fail closed
- Mutation proof: validated request payloads and provider metadata are deeply immutable snapshots
- Reset-safety proof: unmanaged non-empty directories are refused and stale demo evidence requires explicit reset
- Redaction proof: nested tokens, passwords, private keys, and session cookies are removed from JSONL output

The full generated aggregate is [`sample-run/all-scenarios-summary.json`](sample-run/all-scenarios-summary.json). Each scenario directory includes:

- `events.jsonl`: redacted structured event stream;
- `summary.json`: compact verdict;
- `jobs/*.json`: final durable record and complete transition history.

`ManualClock` advances instantly, so retry behavior is fully deterministic and the suite does not wait on wall-clock sleeps.
