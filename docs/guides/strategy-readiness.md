# Strategy readiness checklist

This checklist is the promotion gate for a Librae strategy. Passing repository
tests proves the engine contract, not that a strategy, broker account, or venue
is safe for production. Record evidence for every applicable item and keep the
result with the strategy release.

## Promotion path

| Stage | What it establishes | Exit evidence |
|---|---|---|
| Backtest | Causal research logic and accounting under declared assumptions | Reproducible config/data revision, out-of-sample results, cost and capacity stress |
| Shadow simulation (`mode=sim`) | Incremental data arrival, feature parity, scheduling, and durable analytics | Stable completed-bar decisions and no stale-data/deadline alerts |
| Broker paper (`mode=live`) | Real broker normalization, order lifecycle, reconciliation, and restart behavior | Adapter certification issue complete for the selected broker and account type |
| Live broker | Small-capital operational behavior under real liquidity and failure modes | Named operator, limits, alerts, kill/recovery procedure, and reviewed paper evidence |

Shadow simulation is Librae's bar-fill model; it is not a broker paper
environment. Paper trading uses `mode=live` against a broker sandbox or paper
account and therefore follows the broker-confirmed execution path.

## Evidence checklist

### Data and point-in-time correctness

- [ ] Every input timestamp is timezone-aware and represents a completed,
      observable bar at the strategy decision time.
- [ ] Symbol membership, delistings, corporate actions, futures rolls, and
      fundamentals are point-in-time; no current constituent list or revised
      value is projected backward.
- [ ] Feature windows are grouped by symbol, contain only information through
      bar T, and are not pre-shifted to imitate execution. Librae owns the
      simulated T to T+1 delay.
- [ ] Missing bars, duplicate timestamps, stale data, volume units, currency,
      multiplier, tick size, and trading calendar behavior are explicitly
      tested.
- [ ] The candidate universe is predeclared. A point-in-time eligibility mask
      selects within it; runtime discovery and subscription mutation are not
      assumed.

### Research validity

- [ ] Train, validation, and test periods respect time order. Any overlapping
      label horizon is purged or embargoed.
- [ ] Parameter searches and strategy comparisons account for multiple
      testing; the reported variant is not selected only from its best
      in-sample result.
- [ ] Results are decomposed by market regime and include stress cases for
      spreads, volatility, gaps, missing bars, and delayed execution.
- [ ] Caller-selected benchmark, comparison population, annualization factor,
      signed risk-free rate, sample standard deviation convention, and
      not-computable metrics are documented.

### Portfolio and account semantics

- [ ] Every symbol resolves to the run's single account and currency, with an
      explicit instrument type and multiplier. Separate accounts or currencies
      use separate runs and an external FX and transfer model.
- [ ] A `PortfolioTargets` decision contains one complete account-level target
      state. Omitted existing holdings intentionally target zero.
- [ ] Optimizer inputs, covariance model, objective, optimizer-specific
      constraints, and rebalance schedule live in strategy code and use
      point-in-time data. Engine risk limits remain a separate safety overlay.
- [ ] Related legs have explicit quantities and a hedge ratio owned by the
      strategy. Sequential or cross-venue execution is not treated as atomic.

### Execution, costs, and capacity

- [ ] Fill timing and order type are mode-correct. Bar-field prices are
      simulation instructions, not live execution prices.
- [ ] Commission, tax, spread/slippage, contract multiplier, short/funding
      costs, bar-volume caps, and lagged ADV capacity are configured for every
      instrument and stressed above their base estimates.
- [ ] Perpetual funding observations use payment timestamps and decimal rates;
      missing payments remain missing rather than being forward-filled.
- [ ] Limit, stop, liquidation, partial-fill, terminal-exit, and insufficient
      liquidity behavior have deterministic tests where the strategy uses
      them.
- [ ] Live price/quantity normalization and minimum notional are verified
      against the selected account and venue.

### Risk, reconciliation, and operations

- [ ] Account-level position, gross/net exposure, order-notional, drawdown, and
      limit-price deviation limits are explicit.
- [ ] A first live run starts from a verified flat account or a reviewed
      restored Librae state; configured cash is never combined blindly with
      unknown broker positions.
- [ ] Durable state, single-process lease, broker-order reconciliation,
      restart recovery, and placement-ambiguity handling are exercised.
- [ ] Stale-data, cycle-deadline, database, notification, and broker failures
      have alerts and an operator response.
- [ ] The kill switch, account halt/reset, and unresolved-order procedure are
      rehearsed.
- [ ] The chosen broker/account lifecycle is certified in the applicable
      tracking issue below before paper or live claims are made.

## Choose the decision type by mode

| Decision | Backtest / shadow sim | Broker-confirmed live |
|---|---|---|
| `OrderIntent` | `limit_price=None` uses the configured next-bar market fill; a numeric limit is valid for one eligible bar | `None` submits market; numeric `limit_price` submits limit |
| `PortfolioTargets` | Complete one-account target state, resolved with the configured next-bar fill | The engine sizes from the latest completed close and replans from confirmed fills |
| `MultiLegOrder` | Explicit quantities execute as one synchronous OHLCV approximation | Rejected before submission; use a venue-native combo or strategy-owned coordinator |

The runnable [minimum-variance example](../../examples/minimum_variance/)
keeps the risk model and optimizer in strategy code. The
[multi-leg spread example](../../examples/multi_leg_spread/) owns its hedge
ratio and quantities. Both bundled runners are backtest-only because their
data is synthetic; attempting `sim` or `live` reports that boundary before
strategy execution.

## Capability tracking and intentional non-goals

| Capability | Tracking |
|---|---|
| Binance sandbox order lifecycle | [Issue #31](https://github.com/awwesomeman/librae/issues/31) |
| Shioaji simulation order lifecycle | [Issue #32](https://github.com/awwesomeman/librae/issues/32) |
| IBKR paper order lifecycle | [Issue #33](https://github.com/awwesomeman/librae/issues/33) |
| Broker-native stop and OCO capability audit | [Issue #34](https://github.com/awwesomeman/librae/issues/34) |
| Timestamped perpetual funding cash flows | [Issue #35](https://github.com/awwesomeman/librae/issues/35) |
| Engine correctness and this readiness workflow | [Issue #37](https://github.com/awwesomeman/librae/issues/37) |

The following remain intentional boundaries rather than implied roadmap
promises:

- no hidden alpha model, covariance estimator, optimizer, or feature pipeline;
- no runtime symbol discovery, subscription mutation, or automatic warm-up for
  an undeclared universe;
- no guarantee of atomic multi-leg or cross-venue execution;
- no FX conversion, settlement, transfer, or cross-run netting ledger;
- no automatic corporate-action, borrow-locate, or revised-fundamental model.

If a strategy requires one of these, implement and validate it upstream or
open a focused issue before representing the workflow as supported.
