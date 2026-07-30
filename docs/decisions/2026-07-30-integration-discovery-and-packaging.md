# Integration discovery and packaging

Date: 2026-07-30
Status: accepted

## Context

Librae has concrete reference integrations for CCXT, Shioaji, IBKR,
TimescaleDB, Telegram, and Grafana. The engine also exposes typed protocols in
`librae.integrations`, offline checks in `librae.testing`, explicit adapter
factories in the repository orchestration layer, and direct constructor
injection on `LiveTrader`.

These integrations exercise the current extension boundaries without requiring
installation-time or import-time discovery:

| Integration concern | Current composition boundary |
|---|---|
| Market data and order routing | explicit adapter object or named factory mapping |
| Durable runtime state | injected `LiveStateStore` |
| Analytics persistence | injected callbacks |
| Notifications | injected `Notifier` |
| Dashboards and deployment | separate processes consuming persisted facts |

No observed integration needs to become active merely because its distribution
is installed. The composition root already knows which broker, state store,
notifier, or UI it intends to use.

The distribution currently includes the generic top-level import packages
`brokers`, `db`, `notifications`, `orchestration`, and `app`. Those names can
collide with unrelated packages in the same Python environment and are not
suitable as a published long-term package contract.

The relevant packaging standards are PyPA's plugin-discovery guidance and
entry-points specification plus PEP 420's native namespace-package rules.
Python packaging supports automatic discovery through naming conventions,
namespace packages, or distribution metadata. Entry points advertise
components for discovery, and loading an entry point imports provider code.
PyPA also warns against turning an application's main package into a plugin
namespace because one provider can break the application namespace.

## Decision

### Discovery remains explicit

Librae does not scan module names, namespace packages, or entry-point groups.
Installing a third-party integration does not import it, register global state,
or change engine behavior.

Caller-owned orchestration imports the selected integration and passes a
factory or object explicitly. Import and construction failures therefore occur
only for the selected integration and remain outside the engine. A failure in
one installed but unused integration cannot prevent `import librae` or affect a
different run.

Automatic discovery may be reconsidered only after at least two independently
distributed integrations need configuration-only selection without edits to a
composition root. Any future design must enumerate metadata without loading all
providers, reject duplicate names deterministically, load only the selected
provider, and isolate its import or construction failure.

### Reference integrations move under the regular Librae package

Before the first public PyPI release or Librae 1.0, whichever comes first, the
same distribution will move its reference import packages in one breaking
change:

| Current import package | Target import package |
|---|---|
| `brokers` | `librae.brokers` |
| `db` | `librae.db` |
| `notifications` | `librae.notifications` |
| `orchestration` | `librae.orchestration` |
| `app` | `librae.app` |

`librae` remains a regular package owned by one distribution. It will not
become a PEP 420 namespace package, and external distributions must not install
modules into `librae.*`. A separately released integration uses its own import
package, preferably with a project-specific prefix such as
`librae_broker_example`, and is registered explicitly by caller-owned
orchestration.

The migration does not retain top-level compatibility aliases. Before 1.0 the
repository has one current contract, and retaining both import paths would
preserve the collision that the migration is intended to remove.

### Existing public contracts are sufficient

`librae.integrations` remains the static typing boundary, and `librae.testing`
continues to cover canonical bars, order-adapter capabilities, and cumulative
execution reports. The observed TimescaleDB and Telegram implementations do not
require another repository interface, plugin base class, lifecycle manager, or
global registry. Their concrete tests plus the exported `LiveStateStore` and
`Notifier` protocols are sufficient.

Additional conformance helpers are added only after an external integration
exposes a repeatable mismatch that cannot be expressed by the existing
protocols or fixtures.

## Migration requirements

The namespace migration is one atomic breaking change:

1. Move all five reference packages and rewrite internal, example, deployment,
   and documentation imports.
2. Restrict package discovery to `librae*`; keep optional SDK imports lazy.
3. Package non-Python reference assets from their new locations.
4. Do not provide deprecated modules, import redirects, or dual paths.
5. Verify a minimal installation can import and run the backtest kernel while
   broker, database, notification, and UI dependencies are unavailable.
6. Verify each optional extra and the offline integration fixtures against the
   new paths.

## Consequences

- The base engine remains dependency-light and deterministic.
- Integration selection stays visible at the composition root.
- Unused or broken optional integrations cannot affect core imports.
- A later namespace migration is unavoidable but bounded and deliberately
  occurs before a stable compatibility contract.
- Entry points and separately versioned reference distributions remain
  available future options, but only in response to observed release or
  discovery pressure.
