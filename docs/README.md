# Documentation index

The root [README](../README.md) is the project entry point; this directory
holds task-oriented guides and engineering history. Read the docs that
describe current behavior first — go into `decisions/` and `plans/` only when
you need the reasoning behind a decision or the implementation history.

## Start here

| Need | Doc |
|---|---|
| Install Librae or set up a local dev environment | [Getting started](getting-started.md) |
| Run a complete strategy | [Examples](../examples/README.md) |
| Understand execution semantics and system design | [Architecture](../architecture.md) |
| Compare strategies, benchmarks, and backtest results | [Performance analysis](guides/performance-analysis.md) |
| Analyze a signal's forward outcomes | [Signal outcome analysis](guides/signal-outcome-analysis.md) |
| Check whether a strategy is ready for the next execution stage | [Strategy readiness checklist](guides/strategy-readiness.md) |
| Wire up broker market data or third-party factors | [External market data and factors](guides/external-data.md) |
| Set up the DB, Grafana, a broker, or a deployment | [Optional infrastructure](guides/optional-infrastructure.md) |

For saving data or backtest results to a local format such as Parquet or
SQLite, see [Local artifacts](guides/local-artifacts.md).

## Document types

| Location | Purpose | Maintenance rule |
|---|---|---|
| [`../architecture.md`](../architecture.md) | Current system state and design contract | Updated alongside the code whenever behavior or structure changes |
| `guides/` | Task-oriented guides for users and operators | Commands must stay runnable; deep detail links out to a reference |
| `decisions/` | Architecture decision records (ADRs) | Preserve the view at the time; supersede rather than rewrite history |
| `plans/` | Implementation plans and working notes | Status is historical unless the doc says otherwise |
| `research/` | Research notes and technical investigations | State assumptions, data scope, and conclusions clearly |
| `spikes/` | Time-boxed experiments and framework evaluations | Promote conclusions with lasting impact into `decisions/` |
| `learnings/` | Bugs and operational experience | Record the symptom, root cause, fix, and prevention |

## When docs disagree, what wins?

1. Tests and the current code define actual behavior.
2. [`architecture.md`](../architecture.md) describes the intended system
   state.
3. `guides/` describe how to accomplish a task under that state.
4. `decisions/` explain why a choice was made at a point in time.
5. `plans/`, `research/`, and `spikes/` are historical input, not an API
   guarantee.

This ordering keeps the root README concise while still giving deeper
information a clear entry point.
