# Work-context traces

## Why

Phoenix projects are named after the harness that produced the spans
(`claude-code`, `codex`, `harness-qa`). That split answers "which tool ran"
but not the questions the traces exist for: what a client ticket cost, how
much Autoloop work ran without a human, and how executors compare on the same
work. The default retention is 2 days, so no trend survives.

## What changes

- **Projects follow the data boundary.** `client` for sessions whose git
  remote belongs to the client organization, `personal` for everything else,
  `sandbox` for fork validation. Executors become attributes.
- **Every span carries work context:** client, repository, branch, ticket,
  pull request, run, executor, role, host and provider, as flat attributes and
  as OpenInference `metadata` for Phoenix filters.
- **The QA exporter** routes by the same rule and tags its spans `role=qa`
  instead of writing to the fixed `harness-qa` project.
- **Retention follows the boundary:** `client` 30 days, `personal` 14 days,
  `sandbox` 2 days. The Phoenix volume grows from 10Gi to 40Gi.
- **Hourly totals survive the raw data.** A sidecar reads Phoenix's database
  read-only and writes per-dimension hourly totals to a dedicated
  VictoriaMetrics instance kept for two years. Perses dashboards present them.

## Starting views

| Goal | View |
|---|---|
| Client delivery effort and pricing | Perses `agent-work/delivery`: cost, active hours and turns per client ticket and repository |
| Autoloop autonomy | Perses `agent-work/autonomy`: autonomous vs interactive active time, cost by role, tool error rate per run |
| Spend control | Perses `agent-work/overview`: daily cost by client, executor and model; largest sessions |
| Executor choice | Perses `agent-work/executors`: cost per turn, tool errors per call by executor and model |
| Drill-down | Phoenix `client` and `personal` projects: project dashboard as default view, documented filters |

## Out of scope

- Joining traces to PR merge or CI outcome. It needs a GitHub credential in
  the cluster, which is an owner decision.
- Tracing Oracle Codex workers. The QA launcher deliberately withholds Phoenix
  credentials from workers; installing hook exporters there needs its own
  design.
- Moving Phoenix to PostgreSQL or another host. Revisit when the database
  passes 25Gi or the UI slows.
