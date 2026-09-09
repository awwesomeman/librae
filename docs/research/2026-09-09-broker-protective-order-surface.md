# Broker protective-order surface: what the client libraries expose

Date: 2026-09-09
Scope: issue [#34](https://github.com/awwesomeman/librae/issues/34), preparatory only

## This is not certification evidence

**No box in #34 is closed by this document.**

#34 states the rule this note obeys: *"A documented API order type is not
sufficient evidence that the selected sandbox/paper account, routing
destination, and client library expose the required lifecycle."* Everything
below was produced by importing the three installed SDKs and reading their
class, enum, and method surface. No network call, no login, no order.

A symbol being importable proves only that the *client library* has a name for
the construct. It says nothing about whether the paper/sandbox account is
entitled to it, whether the routing destination accepts it, whether the venue
holds it server-side after disconnect, or how it triggers. Those are exactly
the questions #34 asks, and none of them are answerable offline.

Use this as the "client-library version" column of DoD box 1, and as the input
to scoping the live audit — not as its output.

## Method

All three SDKs were introspected from this repo's `.venv` (Python 3.14):

| Broker | SDK | Version | Adapter |
|---|---|---|---|
| Binance (spot + USD-M) | `ccxt` | 4.5.66 | `librae/brokers/crypto_adapter.py` |
| IBKR | `ib_async` | 2.1.0 | `librae/brokers/ibkr_adapter.py` |
| Shioaji (TAIFEX/TWSE) | `shioaji` | 1.7.0 | `librae/brokers/shioaji_adapter.py` |

Versions are the installed wheels, taken from `dist-info`. Re-record them at
audit time — the live audit must state the version it actually exercised, and
these will have moved.

---

## Binance — ccxt 4.5.66

Read from `exchange.has` on the `binance` (spot) and `binanceusdm` (USD-M
perpetual) classes, plus the `create_order` docstring in `ccxt/binance.py`.

| Construct | `binance` | `binanceusdm` |
|---|---|---|
| `createStopOrder` / `createStopLimitOrder` | yes | yes |
| `createStopMarketOrder` | **no** | yes |
| `createTriggerOrder` | yes | yes |
| `createStopLossOrder` / `createTakeProfitOrder` | yes | yes |
| `createTrailingPercentOrder` | yes | yes |
| `createTrailingAmountOrder` | unset (`None`) | unset (`None`) |
| `createOrderWithTakeProfitAndStopLoss` | **no** | **no** |
| `createOrders` (batch) / `editOrder` | yes | yes |
| `fetchClosedOrders` / `fetchCanceledOrders` | `emulated` | `emulated` |

Unified `create_order` documents these protective `params`: `triggerPrice`,
`stopLossPrice`, `takeProfitPrice`, `trailingPercent`, `trailingTriggerPrice`,
`stopLossOrTakeProfit` (required for *spot* trailing), plus `reduceOnly`,
`positionSide`, `hedged`, and `clientOrderId`.

**Most important finding.** There is no unified single-call OCO on either
class: `createOrderWithTakeProfitAndStopLoss` is `False`, so calling the base
helper raises `NotSupported`. OCO exists only as raw implicit endpoints —
`privatePostOrderOco`, `privatePostOrderListOco`, `privatePostOrderListOto`,
`privatePostOrderListOtoco`, with `privateGetOrderList` /
`privateDeleteOrderList` for lookup and cancel. Those are *spot* REST paths.
They appear as attributes on `binanceusdm` too, because `binanceusdm`
inherits binance's whole API map — that is method-name inheritance, **not**
evidence that a USD-M account can call them. Anything built on Binance OCO
would bypass ccxt's unified layer and hand-roll one venue's raw endpoints.

**Server-held behaviour: unknown offline.** Binance's documented model is that
stop/take-profit orders rest on the venue, and that USD-M's `closePosition`
flag makes the venue cancel the protective order when the position closes.
The library exposes the flag, and ccxt's own source warns that its
`closePositions` is not synonymous with Binance's meaning, so the venue
semantics here come from Binance's documentation and are unverified in this
survey. Whether testnet honours the auto-cancel, and what `workingType` (mark
price vs. last price) the account defaults to, cannot be observed without a
session. `workingType` and `priceProtect` appear in `ccxt/binance.py` only
inside commented example payloads — zero non-comment occurrences of either —
so ccxt does not read, validate or map them; they reach the venue, if at all,
through the generic unhandled-params passthrough, unvalidated. They are not in
`exchange.has` either.

**Also unverifiable offline:** whether `editOrder` accepts an amendment to a
resting trigger order (Binance's USD-M Modify-Order endpoint is described for
limit orders in venue documentation not consulted here), and whether
`fetchOpenOrders` returns trigger/algo orders in the same page as ordinary
ones or needs the separate algo-order endpoints
`fapiPrivateGetOpenAlgoOrders` / `fapiPrivateGetAllAlgoOrders`.

## IBKR — ib_async 2.1.0

Read from `ib_async.order` and the dataclass fields of `Order`.

Order classes present: `MarketOrder`, `LimitOrder`, `StopOrder` (`STP`,
stop in `auxPrice`), `StopLimitOrder` (`STP LMT`), and the
condition types `PriceCondition`, `TimeCondition`, `MarginCondition`,
`ExecutionCondition`, `VolumeCondition`, `PercentChangeCondition`.

`Order` carries 139 fields. The ones that matter here: `auxPrice`,
`trailStopPrice`, `trailingPercent`, `ocaGroup`, `ocaType`, `transmit`,
`parentId`, `parentPermId`, `triggerMethod`, `outsideRth`, `goodAfterTime`,
`goodTillDate`, `autoCancelDate`, `autoCancelParent`, `conditions`,
`conditionsCancelOrder`, `adjustedOrderType`, `adjustedStopPrice`,
`adjustedTrailingAmount`, `whatIf`. Defaults are inert: `ocaGroup=''`,
`ocaType=0`, `parentId=0`, `triggerMethod=0`, `transmit=True`.

`IB.oneCancelsAll(orders, ocaGroup, ocaType)` stamps an explicit OCA group.
`IB.bracketOrder(action, quantity, limitPrice, takeProfitPrice, stopLossPrice)`
returns a `BracketOrder` named tuple of `(parent, takeProfit, stopLoss)`.
`IB.whatIfOrder` exists for pre-trade margin preview.

**Most important finding.** This is the only one of the three with a
first-class parent/child protective primitive, and reading
`IB.bracketOrder`'s source shows two constraints Librae must design around.
First, the parent is hardcoded to `LimitOrder` — there is no market-parent
bracket helper, so a market entry with attached protection means assembling
`parentId` / `transmit` by hand. Second, the helper calls
`self.client.getReqId()` for each leg, so bracket construction is coupled to a
connected client's request-id sequence and cannot be built or unit-tested
detached. Note also that `bracketOrder` sets `parentId` and the
`transmit=False, False, True` staging but leaves `ocaGroup` empty. All the
library shows is that the field is untouched; sibling cancellation is left to
whatever IB does server-side, unobserved here — precisely what #34 wants
observed rather than assumed.

**Server-held behaviour: unknown offline.** Whether a bracket survives client
disconnect depends on TWS/Gateway settings and account type, not on the
library. `ib_async` declares `triggerMethod` as a bare `int` defaulting to
`0`; that `0` means "default" is an IB API documentation fact, not something
the library states, and which price the default resolves to per instrument
class is a TWS-side rule. Both are unreadable without a session.

Useful for DoD box 4: `OrderStatus` exposes `orderId`, `permId`, `parentId`,
`clientId`, and `Order` also carries `orderRef` and `parentPermId` — a
stable, server-issued relationship graph exists on paper. Whether `permId` and
`parentPermId` are populated for bracket children at every lifecycle stage is
an observation, not a read.

## Shioaji — shioaji 1.7.0

Read from `shioaji.constant` and the `_core.pyi` stubs.

**Most important finding — there is nothing to audit.** The futures order
surface has no protective construct of any kind.

- `FuturesPriceType` exposes exactly `LMT`, `MKT`, `MKP`. No stop, no trigger,
  no touched-price type.
- `OrderType` exposes exactly `ROD`, `IOC`, `FOK`.
- `FuturesOrder.__init__` takes `action`, `price`, `quantity`, `price_type`,
  `order_type`, `octype`, `custom_field`, `account` — and nothing else. No
  stop price, no trigger price, no parent id, no OCA group.
- The `Shioaji` client has no method whose name contains stop, trigger,
  condition, or OCO. `place_comboorder` / `cancel_comboorder` /
  `update_combostatus` are futures *spread* combos — `ComboType` is
  PriceSpread, TimeSpread, Straddle, Strangle, ConversionReversal and
  WeeklyTimeSpread — none of which is a protective pair.
- `update_order(trade, price=None, qty=None, timeout=30000, cb=None)` amends
  price and quantity only.

A repo-wide grep of the installed package for `stop_price`, `StopOrder`,
`trigger`, and `TouchPrice` returns nothing in the order path.

So for Shioaji the live audit's job is narrow: confirm that this absence is
real at the *account* level too (i.e. Sinopac offers no stop facility through
another channel that the Python SDK omits), and then record it as unsupported.
There is no lifecycle to certify.

One note on where this surface comes from: shioaji 1.7.0 is a rewrite whose
core ships as a compiled `_core.abi3.so` with a `.pyi` stub, so the enums and
signatures above are read from that stub rather than from Python source. It is
the stock wheel from the registry — no local patching — and the issue numbers
in its comments are not Librae's (librae #186 and #194 are unrelated merged
PRs). Which tracker they do belong to cannot be read offline. Re-read the
stub at audit time, since a
rewrite's surface can move within a minor version.

Also relevant to DoD box 4: `ShioajiAdapter.broker_client_order_id` truncates
a SHA-256 to **six** base32 characters, because the adapter treats
`custom_field` as a six-character field (`shioaji_adapter.py`'s docstring,
itself uncited). The SDK declares only `Optional[str]` with no length
constraint, so six is a Librae-side assumption this survey could not confirm
against the venue. That field is the only durable Librae→broker link. Encoding a
parent/child protective relationship in a six-character namespace shared with
ordinary orders is a real design problem, not a formality.

---

## What Librae would have to add, in all three cases

The engine rejects protective prices before any adapter sees them, at three
distinct layers. All three need work; none of them is where the hard part is.

1. `OrderIntent` in `librae/core/strategy.py` already carries `stop_price` and
   `take_profit_price`, and its docstring states both are *simulation-only*.
   `__post_init__` validates them but does not reject them outright.
2. `librae/live/engine.py` is the actual live gate: after
   `normalize_order_intents`, any intent with a non-`None` `stop_price` or
   `take_profit_price` raises `ValueError("Live stop-loss/take-profit requires
   broker-native protective orders; completed-bar range checks are
   simulation-only")`.
3. `validate_order_signal` in `librae/brokers/base.py` constrains the canonical
   signal dict's `order_type` to `"market"` or `"limit"`. The canonical signal
   has no stop-price field at all, so even if (2) were lifted there is nothing
   to carry the price to an adapter.

Beyond widening those, every adapter's lifecycle surface is single-order:
`place_order`, `find_order`, `get_order`, `list_open_orders`, `cancel_order`
each take one id. There is no concept of a related order set, no sibling
cancellation, and no restart-time reconciliation of a parent/child pair.
`librae/brokers/capabilities.py` publishes a per-broker capability table today,
but only for time-in-force; a protective-order capability table would be the
natural place to encode "USD-M has server-held stops, Shioaji has none."

Backtest already models both prices in `librae/core/executor.py` (stop-market
fills at the worse of stop/open on a gap; take-profit fills as a limit). Any
broker-native implementation must state where it diverges from that model,
because the two will not agree on gap and trigger semantics.

## DoD boxes and why they stay open

| Box | Status after this note |
|---|---|
| 1. Record support by broker, product, environment, client-library version | **Partial.** Client-library version and library-exposed constructs recorded. Tested product/instrument class and environment require a session. |
| 2. Server-held after disconnect, trigger/price-source semantics | **Open.** Not observable offline for any broker. Trivially "none" for Shioaji, but even that needs account-level confirmation. |
| 3. Parent/child activation, transmit, partial fill, sibling cancel, amend, reject, terminal state | **Open.** IB's `transmit`/`parentId`/`ocaType` fields are readable; their behaviour is not. |
| 4. Stable identifiers for restart reconciliation | **Open**, with the constraints above noted: IB `permId`/`parentPermId`, Binance `clientOrderId` plus an OCO list id from raw endpoints only, Shioaji a six-character `custom_field`. |
| 5. Sandbox/paper vs. production-only assumptions | **Open.** Requires both, by definition. |
| 6. Document unsupported combinations in `architecture.md` | **Open.** Deliberately not started — writing venue behaviour into `architecture.md` on library introspection alone is what #34 forbids. |
| 7. Select pilot scope | **Open**, though the surface survey points at IBKR (only first-class bracket/OCA primitive) or Binance USD-M (only unified stop/trailing surface) and rules Shioaji out of a first pilot. |
| 8. Broker-specific implementation issues | **Open.** Blocked on 7. |
