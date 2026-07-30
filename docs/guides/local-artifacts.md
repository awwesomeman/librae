# Local artifacts

`no_db=True` means Librae performs no default persistence. It does not imply an
automatic memory, Parquet, or SQLite backend.

When a local artifact is useful, Librae can normalize market data or a
`BacktestOutput` into a versioned manifest and logical pandas tables. The
caller still owns the storage format, path, overwrite policy, partitioning,
transactions, and retention.

```python
from librae import build_backtest_artifact

artifact = build_backtest_artifact(output, config_hash=config.config_hash)
```

## Parquet

Install `librae[parquet]` (or another pandas-compatible Parquet engine), then
choose the directory and file policy in application code:

```python
import json
from pathlib import Path

target = Path("artifacts") / output.run_metadata.run_id
target.mkdir(parents=True, exist_ok=False)

for name, table in artifact.tables.items():
    table.to_parquet(target / f"{name}.parquet", index=False)

(target / "manifest.json").write_text(
    json.dumps(artifact.manifest, indent=2),
    encoding="utf-8",
)
```

## SQLite

The same tables can use pandas' standard SQL writer; Librae does not introduce
a second SQLite schema:

```python
import json
import sqlite3
from pathlib import Path

target = Path("artifacts") / f"{output.run_metadata.run_id}.sqlite"
target.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(target) as connection:
    for name, table in artifact.tables.items():
        table.to_sql(name, connection, if_exists="fail", index=False)
    connection.execute(
        "CREATE TABLE artifact_manifests "
        "(run_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO artifact_manifests VALUES (?, ?)",
        (
            output.run_metadata.run_id,
            json.dumps(artifact.manifest),
        ),
    )
```

For enriched price/factor input, call `build_market_data_artifact()` with
explicit `symbol`, `timeframe`, `data_source`, and `instrument_type`. It
preserves extra feature columns and normalizes timestamps to UTC.

These helpers are research/export boundaries. They do not replace durable live
state, active-order persistence, broker reconciliation, leases, or the
reference TimescaleDB analytics integration.
