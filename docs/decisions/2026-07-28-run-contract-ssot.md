# Run contract and metadata SSOT

Date: 2026-07-28
Status: accepted

## Decision

- `RunConfig.execution: ExecutionPolicy` owns simulated fill and liquidity
  assumptions.
- `RunConfig.risk: RiskPolicy` owns engine-level position, exposure, and
  drawdown limits.
- `RunConfig.params` contains strategy logic parameters only. Execution and
  risk keys in this mapping are rejected.
- Public configuration parameters are named `config`, not `cfg`.
- `RunMetadata.symbols` and `backtest_runs.symbols` represent the complete run
  universe. `RunConfig.symbol` remains only an in-memory single-asset
  convenience.
- Shared domain aliases (`PositionSide`, `OrderAction`,
  `PositionEventType`, `RunMode`) replace repeated literal definitions.
- `volume_impact_ticks` means extra slippage ticks at 100% single-bar volume
  participation. It replaces the ambiguous `impact_coef`.

## Breaking migration

| Removed | Replacement |
|---|---|
| `cfg=` | `config=` |
| `Backtest(..., market_config=...)` | pass `cost_model=` or `config=` |
| risk keys in `params` | `risk=RiskPolicy(...)` or YAML `strategy.risk` |
| `RiskLimits` / `validate_risk_params` | `RiskPolicy` validation at configuration construction |
| `RunMetadata.symbol` | `RunMetadata.symbols` |
| `backtest_runs.symbol` | `backtest_runs.symbols` JSON array |
| `CostModel.impact_coef` | `CostModel.volume_impact_ticks` |
| `make_fill` / `eval_equity` / `direction` | `simulate_fill` / `calc_equity` / `side_multiplier` |

No compatibility aliases are retained. The database initialization script
migrates an existing scalar `symbol` into a one-element `symbols` array before
dropping the old column.

## Rationale

The engine should have one typed source for each result-affecting concern and
one representation for the complete run universe. This keeps backtest, sim,
live, persistence, and dashboards on the same contract without adding an
order hierarchy, optimizer abstraction, or a second configuration framework.
