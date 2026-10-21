# Codex CLI tracing

OpenInference traces for Codex CLI, exported to Phoenix or Arize AX. This is the
[lifldev fork](https://github.com/lifldev/coding-harness-tracing) of
[Arize's coding-harness-tracing](https://github.com/Arize-ai/coding-harness-tracing).
The bounded rollout reader incorporates prior art from upstream
[PR #114](https://github.com/Arize-ai/coding-harness-tracing/pull/114).

## Setup

From a local checkout:

```bash
./install.sh codex
```

Or install the fork:

```bash
curl -sSL https://raw.githubusercontent.com/lifldev/coding-harness-tracing/main/install.sh | bash -s -- codex
```

Windows:

```powershell
iwr -useb https://raw.githubusercontent.com/lifldev/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat codex
```

The installer prompts for the backend, project and content-logging preferences,
then registers `arize-hook-codex-notify` in Codex's `notify` configuration.
This exporter uses `agent-turn-complete` notifications and persisted rollout
JSONL; lifecycle hooks and `/hooks` trust are not required. Existing unrelated
hooks are preserved. `--with-skills` also links the management skill.

`CODEX_HOME` selects the Codex configuration and sessions directory; otherwise
`~/.codex` is used. An explicit home must already exist. Backend configuration is
stored in `~/.arize/harness/config.json`; the optional env override file is
`$CODEX_HOME/arize-env.sh` (default `~/.codex/arize-env.sh`).

Uninstall with `./install.sh uninstall codex` or `install.bat uninstall codex`.

## Trace contract

Each observed turn has a **CHAIN** root. Individual model responses are **LLM**
children; function, custom, web, discovery, local-shell and image-generation
calls are represented separately. Code-mode `exec` is a CHAIN container with
actual observed `CommandExecution` TOOL children named `exec_command`.
JavaScript source alone never establishes that an inner tool executed.

`token_usage_record.response_id` identifies exact response usage. Its `usage`
is counted once, ignoring duplicate records and legacy token-count broadcasts
for that turn. Older formats use distinct `token_count` snapshots to close
observed response cycles. Input tokens already include cached input; cache and
reasoning are details, never added again. Only LLM spans carry OpenInference
token counts; the turn root carries no duplicate token totals. Observed zero
counts are retained and unknown fields are omitted.

Command lifecycle timestamps, exit codes, outputs and reported durations are
preserved. Nonzero exits are errors. Response timestamps describe persisted
observations, not provider request latency. Tools without lifecycle timing use
persisted item times. Missing results/completion remain unknown; aborted turns
are errors. Malformed input and ambiguous associations produce diagnostics
instead of fabricated success or parents.

Native subagent relationships require both
`session_meta.source.subagent.thread_spawn.parent_thread_id` and authoritative
`turn_attribution.parent_turn_id`. Copied history is excluded using the child's
`subagent_history_start_ordinal`. Known spawned child IDs can be replayed at the
parent's notification; nested and background children can also notify later.
The exporter does not infer native lineage from agent names, prompts or Orca
worker dispatch messages.

IDs are deterministic. Successful per-span delivery hashes are stored under
`~/.arize/harness/state/codex/delivery/` behind a file lock, so repeated notify
callbacks skip unchanged spans and failed sends remain retryable. A later
completion updates the same span IDs. A transport timeout after backend
acceptance can still cause a retry; this is not an exactly-once network guarantee.
Dry runs do not write successful-delivery receipts.

## Privacy and limits

- `ARIZE_LOG_PROMPTS` gates prompts and assistant text.
- `ARIZE_LOG_TOOL_DETAILS` gates arguments, commands and tool-specific details.
- `ARIZE_LOG_TOOL_CONTENT` gates results, including results copied into subsequent
  model-input attributes.
- Structured binary image/audio blocks and built-in image results are omitted.
  Encrypted reasoning is never exported. Enabled content is capped at 64 KiB
  per attribute. Delivery receipts contain hashes, not transcript content.

Reads are restricted to regular files, at most 64 MiB, 1 MiB per line and
200,000 records. Child replay is bounded to 64 observed turns and lineage depth
8. Ephemeral runs or missing rollouts produce an unknown CHAIN from notify data,
with no invented model usage or request timing. Forks without a trustworthy
history boundary cannot be reconstructed safely. Sessions that never notify
and whose child IDs are not observed are unavailable; there is no background
watcher or guessed thread discovery. Legacy rollouts lack exact response IDs
and request timings, so their response-cycle boundaries remain inferred.

## Verification

Inspect `~/.arize/harness/logs/codex.log` and the configured Phoenix or Arize
project after an authorized Codex run. `ARIZE_VERBOSE=true` enables routine
activity logging. `ARIZE_DRY_RUN=true` prevents backend sends. See the main
[environment variables](../../README.md#environment-variables) reference for
configuration details. A local parser replay verifies reconstruction only;
backend rendering and deployment need separate runtime evidence.
