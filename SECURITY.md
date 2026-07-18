# Security Notes

This repository contains no credentials, customer records, remote endpoints, or live provider integration. The checked-in examples use synthetic identifiers only.

## Safe integration rules

- Keep credentials in a secret manager or operating-system credential store, never in job payloads or repository files.
- Refresh credentials inside the provider adapter; do not return tokens to the orchestration engine.
- Propagate the job idempotency key to any provider operation capable of creating a side effect.
- Configure connect, read, and total timeouts in the provider's network client.
- Treat provider output as untrusted data until `ProviderResult` validation succeeds.
- Keep human approval enabled for consequential actions.
- Replace the single-worker file store with leased or compare-and-swap storage before concurrent deployment.

The JSONL logger recursively redacts fields whose names contain common credential markers. Redaction is defense in depth, not permission to put secrets into event data.

## Reporting a vulnerability

Please disclose vulnerabilities privately to the repository owner before publishing details. Include a minimal reproduction and avoid real credentials or customer data.
