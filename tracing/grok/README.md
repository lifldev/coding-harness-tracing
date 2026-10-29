# Native Grok Build tracing

The native Grok adapter exports completed saved turns and tool observations to
the existing Phoenix/Arize backend. It does not call a model. Claude/Cursor hook
compatibility is not required and must not be enabled for this integration.

Install through `install.sh grok` (or `install.bat grok`). The native hook file is
`$GROK_HOME/hooks/arize-tracing.json`, defaulting to `~/.grok/hooks/`. It is additive
and exporter-owned; installation refuses to overwrite a foreign file.

For an existing detached deployment, stage and review registration first:

```sh
python -m tracing.grok.install --stage /tmp/grok-hooks.json \
  --launcher /path/to/deployed/.venv/bin/arize-harness
# Review the stage before installing it.
python -m tracing.grok.install --stage /tmp/grok-hooks.json \
  --install-to ~/.grok/hooks/arize-tracing.json
```

The backend needs a `harnesses.grok` entry in the existing tracing configuration.
The same `project_routes`, `default_project`, credential resolution and content
privacy gates apply. Grok is recorded as `agent.harness=grok`, not as a separate
cloud project. Native Stop, StopFailure, StopCancelled, SessionEnd and idle-prompt
Notification hooks request replay; none can block or resume the agent.

Replay an existing session without inference:

```sh
printf '%s\n' '{"hookEventName":"session_end","sessionId":"SESSION_ID"}' \
  | arize-harness hook grok
```

Records are read beneath `$GROK_HOME/sessions` with regular-file, path and size
checks. Session metadata must match the requested ID. Each completed prompt ID
gets deterministic trace/span IDs. Hash-only delivery receipts under
`~/.arize/harness/state/grok/delivery` suppress unchanged sends. Failed deliveries
stay replayable; dry runs never acknowledge delivery. A hook may run before the
terminal record is persisted, so a later completion/session-end event or explicit
replay reconciles it. There is no background retry scheduler.
Each snapshot includes the newest 64 closed turns; older delivered spans remain
in the backend, and delayed initial replay is bounded to those latest turns.

## Verifying native hook discovery

Grok Build 1.0.70's `grok inspect` can omit global native hook files, including
`~/.grok/hooks/arize-tracing.json`. An empty inspect list alone does not prove
the hooks failed to load. Initialize an ACP connection, create or load a session,
then query `_x.ai/hooks/list` with its `sessionId`. Check that the five
`global/arize-tracing` entries exist and have `disabled: false`.
This discovery check needs no `session/prompt` request or model call.

## Measurement boundaries

The CHAIN turn contains TOOL children keyed by native call IDs. Native tool
duration is preferred; ACP observation intervals are explicitly labeled when
native durations are unavailable. Incomplete turns are omitted until a native
completion record exists. Cancellation and failure are errors, never success.

Native per-model turn usage appears in zero-duration LLM accounting snapshots
marked `grok.usage.aggregation=turn_model`. They are aggregate accounting, not
individual inference calls. Cache reads are already part of input tokens, and
reasoning is already part of output tokens. Aggregates are not duplicated on the
root or tools. Per-response tokens, response cycles, service tiers and TTFT remain
unknown. No throughput or provider latency is inferred from these snapshots.

Session reasoning effort is labeled `grok.session.reasoning_effort`; it does not
establish the effort of every historical turn after resume. Prompts and assistant
text respect prompt logging. Tool bodies respect content logging; input also
requires details logging. Encrypted reasoning is never read or exported.
