# Native Grok tracing

## Problem

Grok Build sessions are absent from Phoenix because this exporter has no Grok
integration or native Grok hook registration. Claude and Cursor hook imports are
disabled intentionally; their payloads are not a native Grok tracing contract.

## Scope

- Add a native Grok adapter to the existing exporter and its CLI/install surfaces.
- Reconstruct completed turns and tools from Grok's saved session records, with
  deterministic IDs and delivery receipts so replay and repeated hooks converge.
- Register passive native completion, failure, cancellation and session-end hooks.
  Preserve all existing Grok hooks, permissions, authentication and compatibility
  settings. Stage and diff host configuration before installation.
- Reuse existing backend credentials, privacy gates, work-context attributes and
  project routing. Keep Grok as an executor attribute rather than a new project.
- Verify the missing benchmark session reaches Phoenix using saved-record replay;
  validate the installed hook path with synthetic events, without model calls.
- Check existing Claude/Codex traces and recommend project consolidation without
  moving or deleting existing cloud data.

## Acceptance

1. Synthetic fixtures cover multi-turn reconstruction, tool errors, cancellation,
   duplicate hook delivery, failed-send retry, malformed input and privacy gates.
2. Native token semantics are preserved: cached input is a subset of total input;
   reasoning is a subset of output. Unknown per-response usage/timing stays unknown.
3. All focused and repository tests pass. Existing harness routes remain unchanged.
4. The saved Grok benchmark session is queryable in Phoenix with matching native
   session/model/tool/usage data; a second replay creates no additional spans.
5. Host configuration is installed from a reviewed commit through the established
   detached deployment worktree. Native hook discovery and actual cloud receipts
   establish installation; configuration text alone does not.

## Boundaries

No model calls, API-key inference, collector infrastructure changes, credential
grants, legacy-project deletion or historical migration. No private transcripts
or user-specific host paths in committed fixtures. Cloud verification uploads only
the requested benchmark and clearly marked synthetic validation data.

## Project recommendation

Keep `voxaero`, `personal` and `sandbox` as the active destinations. Existing
retention is respectively 30, 14 and 2 days. Separate executor-named projects are
redundant because executor identity is already an attribute. Leave their history
under current retention; deleting project entries requires a separate decision.
