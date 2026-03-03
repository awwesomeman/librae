---
name: workflow-consistency
description: Enforce consistent daily workflow outputs for quant operations. Use when user asks to backtest a strategy, request TODO status, run robustness tests, or start strategy monitoring. Apply fixed templates (brief/full/robust/todo), enforce naming rules, require best-practice coding checks (code review + unit tests), and standardize response structure (執行中/待執行/等待決策).
---

# Workflow Consistency

Use this skill to keep outputs and process stable across repeated quant tasks.

Read `references/trigger-map.md` first when this skill triggers.

## Required Output Routing

- 回測分析：預設 brief，使用者要求再 full。
- 穩健性測試：使用 robust 模板，必含 WF/穩定區/成本壓測。
- 待辦清單：固定 完成/進行中/待執行，且每項有簡單描述。
- 監控策略：固定回報 setup/trigger、state、log、去重機制。

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
- Best-practice checks completed (py_compile + unit test for core changes)
- TODO synchronized for new/changed commitments
- Response blocks use: 執行中 / 待執行 / 等待決策 (when status update is requested)
