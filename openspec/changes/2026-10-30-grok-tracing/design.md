# Grok adapter contract

## Native source and trigger

Use Grok's own hooks under its documented global hook directory, not Claude or
Cursor compatibility. Completion is observed through Stop, StopFailure,
StopCancelled and SessionEnd. Session-end Stop notifications must not create
another completed turn. Export failures must not block or resume the agent.

Resolve a session beneath the native session root using validated session IDs
and metadata. Read bounded regular JSON/JSONL files and reject path escapes and
symlinks. Grok documents updates.jsonl as the authoritative conversation stream.
events.jsonl supplies observed turn/tool lifecycle timing; usage.json supplies
turn totals. Read existing privacy settings before including any text.

The recorded benchmark has native turn_started/turn_ended events, tool completion
events with call IDs and durations, ACP tool updates, and a turn_completed update
with prompt ID, outcome, elapsed time and token totals. Preserve native provenance
when these records differ or fields are absent. Do not read encrypted reasoning.

## Trace structure and measurements

One CHAIN root per native turn; TOOL children keyed by native tool-call ID. Carry
known model and reasoning effort, native outcome and work-context dimensions.
Per-call token totals are not present in the inspected benchmark records. Export
turn/model aggregate usage explicitly as aggregate accounting, without duplicating
tokens on individual response spans or pretending it is one inference call.

Native first-token observations, summed API duration, turn elapsed time and CLI
wall time have different boundaries. Preserve each only with its source and unit;
do not fabricate per-response latency, service tier, response cycles or throughput.
Unknown parent/subagent relationships remain unknown.

## Delivery and installation

Use the existing OTLP transport, credential resolution, route selection, privacy
controls and work-context enrichment. Give Grok its own harness key and state
directory. Use deterministic trace/span IDs and hash-only successful-delivery
receipts under a lock; failed sends remain retryable. Repeated lifecycle hooks and
explicit replay update the same IDs. Dry runs do not acknowledge cloud delivery.

Install an additive, exporter-owned native hook file and backend entry through
staging and diff. Do not enable vendor hook imports or weaken guards. Runtime
consumers point to the detached deployed revision, with existing state/config
preserved for rollback.

## Verification

Use small synthetic fixtures for distinct behaviors and a local fake transport for
delivery failures. Replay the existing completed benchmark to verify real Phoenix
ingestion without inference. Query cloud IDs and whitelisted attributes; do not
dump span content or credentials. Existing Claude/Codex sessions provide routing
regression evidence. Backend acceptance and hook discovery must both be checked.
