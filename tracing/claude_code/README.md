# Claude Code Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for the Claude Code CLI and the Claude Agent SDK. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

## Trace structure

Each user turn is represented as a `CHAIN` with a separate `LLM` span for every model response. Tool calls are correlated by Claude's `tool_use_id` and parented to the model response that requested them. Subagents are represented as `AGENT` spans below their invoking tools, with their own model and tool spans. Background agents retain the launching turn's trace and parent tool even when they finish after that turn; nested and concurrent agents use explicit agent IDs or adjacent transcript metadata's `toolUseId` to resolve their parents.

```text
Turn 1 (CHAIN)
├── LLM call 1 (LLM)
│   └── Task (TOOL)
│       └── Subagent: Explore (AGENT)
│           ├── LLM call 2 (LLM)
│           │   └── Grep (TOOL)
│           └── LLM call 3 (LLM)
└── LLM call 4 (LLM)
    └── Bash (TOOL)
```

All spans for a turn share one trace ID. Turns from the same Claude Code session share `session.id`. Token and cache usage is attached to the individual `LLM` call that reported it.

Queued user input can start another turn trace while Claude continues working.
Without an observed Stop or StopFailure, the previous root has unknown status;
`claude.closure.source` and `claude.closure.reason` identify this inferred
boundary. Its output contains only observed assistant text. A failed terminal
export retains the original outcome, end time and transcript boundary for retry,
so the next prompt cannot replace a real answer with a recovery message.

Transcript parsing and hook observations are fail-soft. Unknown or malformed records produce diagnostics where possible; missing parents, duplicate IDs, invalid timestamps, and interrupted exports do not prevent the remaining valid graph from being emitted. If the transcript is unavailable, the integration falls back to the legacy turn export instead of inventing model-call boundaries.

### Current limitations

- Subagent reconstruction requires a readable child transcript and an unambiguous invoking tool, identified by the structured launch result's `agentId`, an explicit parent tool ID, or the child transcript's adjacent `.meta.json` `toolUseId`. Unmatched descriptors are retained; parent relationships are never guessed from timing, prompt text, or launch order.
- A subagent that never emits `SubagentStop`, or disappears without a recorded failure/cancellation signal, cannot be reconstructed as a completed subtree. Explicit failed/cancelled/interrupted outcomes and transcript API errors are preserved; missing tool results remain incomplete. A successful stop hook alone does not reveal an unrecorded earlier interruption.
- Claude transcript timestamps record response arrival, not model request dispatch. Model starts estimated from the preceding conversation boundary include possible local processing; `llm.timing.start_source` marks this inference, and `llm.timing.end_source` identifies the observed response record. Subagent timestamps use start/stop hooks when available; a launch-tool fallback is marked by `subagent.timing.start_source`.
- Export snapshots are serialized per session. Retries reuse persisted span IDs, and repeated completion hooks are acknowledged once while session state exists. A process crash between remote acceptance and local acknowledgment is not an exactly-once delivery guarantee; exporter/backend retry semantics still apply. Pending state survives `SessionEnd`, but missing completion hooks are not retried by a background scheduler.
- High-fidelity model/tool parenting depends on a readable Claude transcript. The no-transcript fallback remains intentionally less detailed.
- Unknown future Claude transcript schemas are handled fail-soft, but may produce partial graphs until fixtures and parser support are updated.

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, and registers the hooks in `~/.claude/settings.json`.

Pass `--with-skills` to also symlink the `manage-claude-code-tracing` skill into the current directory's `.agents/skills/` so Claude can help you manage the configuration interactively.

### Claude Code marketplace

The marketplace flow registers the hooks but skips the interactive wizard, so backend credentials and content-logging preferences must be set directly in `~/.claude/settings.json` under `env`:

```json
{
  "env": {
    "ARIZE_PROJECT_NAME": "claude-code",
    "ARIZE_API_KEY": "<your-arize-api-key>",
    "ARIZE_SPACE_ID": "<your-arize-space-id>",
    "ARIZE_LOG_PROMPTS": "true",
    "ARIZE_LOG_TOOL_DETAILS": "true",
    "ARIZE_LOG_TOOL_CONTENT": "true"
  }
}
```

For Phoenix, swap the Arize keys for `PHOENIX_ENDPOINT` (and optional `PHOENIX_API_KEY`). Each `ARIZE_LOG_*` flag accepts `"true"` or `"false"` — set to `"false"` to opt out per category. Env values take precedence over `~/.arize/harness/config.json`.

Install:

```bash
claude plugin marketplace add lifldev/coding-harness-tracing
claude plugin install claude-code-tracing@coding-harness-tracing
```

Uninstall:

```bash
claude plugin uninstall claude-code-tracing@coding-harness-tracing
claude plugin marketplace remove lifldev/coding-harness-tracing
```

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/lifldev/coding-harness-tracing/main/install.sh | bash -s -- claude
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/lifldev/coding-harness-tracing/main/install.sh | bash -s -- uninstall claude
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/lifldev/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat claude
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/lifldev/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall claude
```

### Local setup

```bash
git clone https://github.com/lifldev/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh claude
```

Uninstall:

```bash
./install.sh uninstall claude
```

**Windows (PowerShell)**

Install:

```powershell
install.bat claude
```

Uninstall:

```powershell
install.bat uninstall claude
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `claude-code` |
| Project name | `claude-code` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Hook config file | `~/.claude/settings.json` |
| Hook events registered | `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `UserPromptExpansion`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `StopFailure`, `SubagentStart`, `SubagentStop`, `Notification`, `PermissionRequest`, `PermissionDenied`, `PreCompact`, `PostCompact` |
| State directory | `~/.arize/harness/state/claude-code/` |
| Log file | `~/.arize/harness/logs/claude-code.log` |

## Content and privacy controls

The capture flags apply to every span in the high-fidelity tree:

| Flag | Protected content |
|------|-------------------|
| `ARIZE_LOG_PROMPTS` | Turn and model inputs/outputs, plus subagent prompts |
| `ARIZE_LOG_TOOL_DETAILS` | Commands, file paths, queries, and URLs extracted from tool inputs |
| `ARIZE_LOG_TOOL_CONTENT` | Tool arguments/results/errors and subagent output |

When a category is disabled, its values are replaced with redaction markers throughout the serialized OTLP payload, including error status messages. Defaults are unchanged by the high-fidelity renderer.

`ARIZE_TRACE_DEBUG=true` is different: it writes raw hook payloads under the harness state directory for troubleshooting. Treat those files as sensitive, enable the option only temporarily, and remove the debug directory when finished.

## Verifying tracing

Run any Claude Code session as you normally would (e.g. `claude` or `claude -p "hello"`). The installed hooks fire on every `SessionStart`, `UserPromptSubmit`, `PreToolUse`, etc.

- Errors and `ARIZE_VERBOSE=true` activity land in `~/.arize/harness/logs/claude-code.log`. To see routine hook activity (`session_start fired`, `emitted LLM span`, etc.), add `"ARIZE_VERBOSE": "true"` under `env` in `~/.claude/settings.json` and re-run a session.
- Confirm spans appear in your configured project in Arize AX or Phoenix.
- Set `"ARIZE_TRACE_ENABLED": "false"` under `env` to temporarily disable tracing without uninstalling, or `"ARIZE_DRY_RUN": "true"` to build spans without sending them.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides.
