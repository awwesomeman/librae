---
name: artifact-lifecycle-manager
description: Review and clean obsolete quant artifacts safely. Use when user asks to review old backtests/monitor scripts/reports and remove outdated, erroneous, duplicated, or no-longer-used files. Always apply two-phase cleanup: audit proposal first, deletion/archive after explicit approval.
---

# Artifact Lifecycle Manager

Use this skill to reduce clutter and technical debt without accidental loss.

Read `references/cleanup-flow.md` first when this skill triggers.

## Safety Rule (Mandatory)

Never hard-delete immediately.
Use two phases:
1. Audit and propose
2. Execute after user confirmation

Prefer archive/trash over irreversible delete.

## Phase 1 — Audit

For candidate files, classify each as:
- `keep`: still active or reference-critical
- `archive`: old but useful for traceability
- `delete`: duplicate/error output/no longer needed

Provide reason tags:
- duplicate
- superseded
- invalid-output
- stale-generated
- unused-script

## Phase 2 — Execution

After user approves:
- Move `archive` files to `archive/YYYYMMDD/`
- Remove `delete` files (prefer recoverable method when possible)
- Produce a cleanup changelog with before/after counts

## Required Report Format

- Summary counts: keep/archive/delete
- Item list with reason per file
- Risk notes (what was intentionally not removed)
- Follow-up actions (if references/scripts need updates)

## Integration Rule

After cleanup:
- Update TODO if follow-up work is created
- Keep report in markdown for review traceability
