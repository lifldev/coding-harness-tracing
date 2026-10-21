---
name: manage-codex-tracing
description: Set up, configure or troubleshoot Codex CLI rollout tracing to Phoenix or Arize AX, including missing model responses, tools, token usage and native subagent lineage.
---

# Manage Codex tracing

Read `tracing/codex/README.md` in the installed repository for the current trace
contract and limits. This is the
[lifldev fork](https://github.com/lifldev/coding-harness-tracing) of
[Arize's exporter](https://github.com/Arize-ai/coding-harness-tracing).

## Installation and configuration

Use the repository installer, `./install.sh codex` (`install.bat codex` on
Windows), or the fork installer URL:

```bash
curl -sSL https://raw.githubusercontent.com/lifldev/coding-harness-tracing/main/install.sh | bash -s -- codex
```

The installer registers `arize-hook-codex-notify` in `$CODEX_HOME/config.toml`
(default `~/.codex/config.toml`). It uses `agent-turn-complete` and rollout JSONL,
not lifecycle hooks. Do not instruct users to approve `/hooks` for this exporter.
Preserve unrelated hook/notify configuration and use the installer to uninstall.
An explicit `CODEX_HOME` must exist.

Use `arize-config` for backend/project/content settings. Credentials belong in
`~/.arize/harness/config.json` or the documented env override file, never in chat
or committed files. Check existing configuration before asking for missing
information. Obtain authorization before any install/configuration change,
external send or model launch that is outside the user's requested scope.

## Expected reconstruction

- One CHAIN per turn, individual LLM response spans and named TOOL spans.
- Code-mode exec wrappers group real structured `CommandExecution` observations.
  Do not parse JavaScript source as evidence that a command ran.
- Response usage comes from `token_usage_record.usage`, deduplicated by
  `response_id`; older snapshots are a fallback. Cache/reasoning are details of
  input/output, not additional totals. Token counts belong only on LLM spans.
- Response timing is observed persistence timing, not provider request timing.
  Commands use observed lifecycle timing and nonzero exits fail.
- Native lineage requires child session metadata and parent-turn attribution.
  Exclude inherited history. Orca worker dispatch is not Codex native lineage.
- Missing completion/results are unknown and malformed records fail soft with
  diagnostics. Never turn an incomplete rollout into claimed success.

## Troubleshooting safely

1. Check the active Codex home and literal rollout session identity. Never read
   credentials or print raw private prompts/results into an issue or artifact.
2. Check `~/.arize/harness/logs/codex.log`, notify configuration, trace-enabled
   setting and backend/project resolution. Do not assume unset process env means
   configuration is absent.
3. Replay only an existing authorized rollout with backend sends mocked, or use
   `ARIZE_DRY_RUN=true`. Verify response IDs, per-response usage, command item IDs,
   lifecycle times, result statuses and parent attribution. Commit synthetic
   fixtures, never copied private rollout content.
4. Check content gates independently: `ARIZE_LOG_PROMPTS`,
   `ARIZE_LOG_TOOL_DETAILS`, `ARIZE_LOG_TOOL_CONTENT`. Tool results in following
   model inputs use the tool-content gate. Binary image/audio and encrypted
   reasoning are omitted; enabled attributes are bounded.
5. Duplicate notify delivery uses deterministic IDs and successful per-span hash
   receipts in `~/.arize/harness/state/codex/delivery/`. Failed sends remain
   retryable; do not delete receipts casually or claim exactly-once transport.
6. Report unsupported cases plainly: ephemeral/missing rollouts, forks without
   history boundaries, unavailable parents, unobserved/non-notifying background
   sessions, and missing provider request timestamps. Parser checks do not verify
   a live backend UI or deployment.
