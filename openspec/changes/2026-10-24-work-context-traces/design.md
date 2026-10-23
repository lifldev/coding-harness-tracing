# Design

This contract is shared by three repositories: this fork (Claude Code and Codex
exporters), `lifldev/agent-harness` (QA exporter) and `lifldev/infra`
(Phoenix, rollup, dashboards). Names below are the interface; change them in
all three together.

## Project routing

Resolved once per session from the session working directory:

1. `PHOENIX_PROJECT_NAME` / `ARIZE_PROJECT_NAME` still win (fork validation
   sets `sandbox`).
2. Otherwise the `origin` remote of the git repository containing the working
   directory is parsed to `owner/name`. The first `project_routes` entry whose
   `owner` matches case-insensitively selects the project.
3. Otherwise `default_project`, then the harness `project_name`.

Configuration in `~/.arize/harness/config.json`, top level so every harness
shares it:

```json
"project_routes": [{"owner": "ExampleClient", "project": "client"}],
"default_project": "personal"
```

Git lookups use `git -C <cwd>` with a short timeout and are cached per
session. Failure falls through to the default and never drops a span.

## Span attributes

Set on every span, omitted when unknown. String values only.

| Attribute | Source |
|---|---|
| `work.client` | the routed project (`client`, `personal`, `sandbox`) |
| `work.repo` | `owner/name` from the origin remote |
| `work.branch` | current branch |
| `work.ticket` | `HARNESS_TICKET`, else `AUTOLOOP_TICKET_ID`, else the first `[A-Za-z]{2,10}-\d+` in the branch, upper-cased |
| `work.pr` | `HARNESS_PR` |
| `work.run_id` | `HARNESS_RUN_ID`, else `runId` from `HARNESS_TRACE_CONTEXT` |
| `agent.harness` | `claude-code`, `codex`, `harness-qa` |
| `agent.role` | `HARNESS_AGENT_ROLE` if one of `coordinator`, `worker`, `reviewer`, `qa`, `interactive`; else `worker` when `AUTOLOOP_TICKET_ID` or `HARNESS_TRACE_CONTEXT` is set; else `interactive` |
| `agent.host` | short hostname |
| `gen_ai.agent.name` | same as `agent.harness` |
| `gen_ai.provider.name` | `anthropic` or `openai` |

The same keys, minus the `gen_ai.*` pair, are written as a JSON object in the
OpenInference `metadata` attribute, so Phoenix filters read
`metadata['work.ticket'] == 'AB-894'`. Existing `metadata` keys are merged,
not replaced.

## Retention and storage

Phoenix trace retention policies: `client` 30 days, `personal` 14 days,
`sandbox` 2 days, Default 2 days (retires the old per-harness projects). At
the measured ~285 MiB/day this stays under 10GiB; the volume grows to 40Gi for
headroom and backups. Policies are set through the Phoenix REST API and
recorded in the infra tenant README.

## Hourly totals

A sidecar in the Phoenix pod opens `/data/phoenix.db` read-only and, every
15 minutes, recomputes the last 6 closed hours. It writes samples stamped at
the hour start to `vmsingle-agentwork` (2-year retention,
`-dedup.minScrapeInterval=1h`, so a rerun replaces rather than duplicates).
Reading SQLite avoids a new API credential and uses Phoenix's own computed
span costs; the pinned Phoenix image makes the schema a reviewed dependency,
and the sidecar logs and skips on schema mismatch.

Metrics, one sample per hour per label set. Labels: `project`, `client`,
`repo`, `ticket`, `harness`, `role`, `host`, plus `model` on LLM metrics.

| Metric | Meaning |
|---|---|
| `agentwork_cost_usd` | Phoenix span cost |
| `agentwork_tokens` | `kind` = `prompt`, `completion`, `cache_read`, `cache_write` |
| `agentwork_llm_calls` | LLM spans |
| `agentwork_tool_calls` / `agentwork_tool_errors` | TOOL spans / those with error status |
| `agentwork_turns` | root spans |
| `agentwork_active_seconds` | summed root span duration |
| `agentwork_sessions` | distinct `session.id` with a root span starting in the hour |

Dashboards use `sum_over_time(...[$__range])` grouped by label.
