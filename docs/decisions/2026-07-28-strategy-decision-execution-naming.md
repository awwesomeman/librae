# Strategy, decision, and execution naming

Date: 2026-07-28
Status: Accepted

## Context

The old API mixed three different concepts: strategy implementations,
pre-execution requests, and portfolio targets. `Action` also looked like a
broker order even though no order had been submitted, while execution and
volume settings were split between free-form strategy parameters and engine
defaults.

Mainstream engines keep these stages distinct. QuantConnect separates strategy
insights, portfolio targets, and execution; Backtrader exposes strategy methods
that create target orders; NautilusTrader strategies create actual broker
orders through an order factory.

## Decision

Librae uses this one-way contract:

```text
Strategy.on_bar(Context)
    -> StrategyDecision
        -> list[OrderIntent] | PortfolioTargets
            -> execute_order_intents / execute_portfolio_targets
```

- `Strategy` is the only strategy base class.
- `OrderIntent` is a pre-submission symbol-level instruction. Its discriminator
  is `action = "long" | "short" | "close"`.
- `PortfolioTargets` is one portfolio-level decision containing a target-weight
  mapping. It is data returned by a strategy, not a strategy subclass.
- An empty list is the only no-decision representation; the old `hold` action
  is removed.
- Run-wide fill-field and volume assumptions live in typed
  `RunConfig.execution: ExecutionPolicy`.
- `params` remains for strategy and portfolio-risk parameters. Legacy execution
  keys in `params` fail validation instead of being silently accepted.

This is an intentional breaking change with no compatibility aliases:

| Removed | Replacement |
|---|---|
| `BaseStrategy` | `Strategy` |
| `Action(type=...)` | `OrderIntent(action=...)` |
| `RebalanceTargets` | `PortfolioTargets` |
| `StrategyIntent` | `StrategyDecision` |
| `process_actions` | `execute_order_intents` |
| `process_rebalance_targets` | `execute_portfolio_targets` |
| `params["fill_price"]` | `execution.default_fill_price` |
| `params["max_volume_participation_pct"]` | `execution.max_volume_participation_rate` |

Persisted live runtime state is schema v4. Older checkpoints are rejected and
must not be guessed or partially migrated.

## Consequences

The name of each object now identifies its lifecycle stage, and backtest, sim,
and live consume the same resolved execution policy. Users must update strategy
imports, constructors, configuration, and restart state in one deployment.
Actual live fills remain broker-confirmed; execution policy only controls local
request sizing and deterministic backtest/sim fills.

## References

- [QuantConnect portfolio construction](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/portfolio-construction/key-concepts)
- [Backtrader target orders](https://www.backtrader.com/docu/order_target/order_target/)
- [NautilusTrader strategies](https://nautilustrader.io/docs/latest/concepts/strategies/)
