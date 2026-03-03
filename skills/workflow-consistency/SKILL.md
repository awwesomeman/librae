---
name: workflow-consistency
description: Enforce consistent daily workflow outputs for quant operations. Use when user asks to backtest a strategy, request TODO status, run robustness tests, or start strategy monitoring. Apply fixed templates (brief/full/robust/todo), enforce naming rules, require best-practice coding checks (code review + unit tests), and standardize response structure (執行中/待執行/等待決策).
---

# Workflow Consistency

Use this skill to keep outputs and process stable across repeated quant tasks.

## Required Output Routing

- If user asks 回測分析: use `templates/backtest_report_brief.md` first, then full version if requested.
- If user asks 穩健性測試: include walk-forward + stability + cost stress summary.
- If user asks 待辦清單: use full TODO structure with item descriptions.
- If user asks 監控策略: report setup/trigger schedule, state file, log file, dedupe behavior.

## Required Guardrails Before Finalizing Code

1. Keep naming consistent (file/variable/function naming style).
2. Keep logic DRY and modular (no duplicate business logic).
3. Run syntax check (`py_compile`) for changed Python files.
4. Run unit tests for core logic touched.
5. Report using three blocks:
   - 執行中
   - 待執行
   - 等待決策

## Naming Standard

Use strategy naming:
`[StrategyName]_v[Major].[Minor]-[TF]-[Side]-[Asset]`

- Major: logic change
- Minor: meaningful parameter/risk adjustment
- Bugfix only: no version bump

## Claude CLI Model Routing (coding tasks)

When delegating coding tasks via Claude CLI, route by difficulty:
- Sonnet 4.6: default for routine coding, moderate refactor, standard script work.
- Opus 4.6: complex architecture changes, high-risk refactor, ambiguous/critical logic decisions.

Prefer Sonnet for speed; escalate to Opus when correctness/risk dominates.

## Completion Checklist

Before sending final response, confirm all:
- Correct template selected
- Required metrics present
- Best-practice checks completed
- TODO synchronized for new/changed commitments
