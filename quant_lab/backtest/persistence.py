"""Backtest output persistence — JSON and CSV formats.

JSON: full output including metadata, metrics, trades.
CSV: equity curve (one row per ts) for Grafana/Streamlit time-series charts.

Both are keyed by run_id so multiple runs can coexist in the same directory.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from quant_lab.contracts import (
    REQUIRED_BACKTEST_TOP_LEVEL_KEYS,
    SCHEMA_VERSION,
    is_schema_compatible,
    require_keys,
)

from .schema import (
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
    TradeRecord,
)


def _dt_to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _iso_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _output_to_dict(output: BacktestOutput) -> dict:
    """Serialize BacktestOutput to a plain dict (JSON-safe)."""
    meta = asdict(output.run_metadata)
    # datetime → ISO string
    for k in ("start_ts", "end_ts", "run_ts"):
        if isinstance(meta.get(k), datetime):
            meta[k] = _dt_to_iso(meta[k])

    equity_curve = []
    for pt in output.equity_curve:
        row = asdict(pt)
        row["ts"] = _dt_to_iso(pt.ts)
        equity_curve.append(row)

    trades = []
    for tr in output.trades:
        row = asdict(tr)
        row["entry_ts"] = _dt_to_iso(tr.entry_ts)
        row["exit_ts"] = _dt_to_iso(tr.exit_ts)
        trades.append(row)

    return {
        "schema_version": output.run_metadata.schema_version or SCHEMA_VERSION,
        "run_metadata": meta,
        "equity_curve": equity_curve,
        "trades": trades,
        "metrics": asdict(output.metrics),
    }


def _dict_to_output(data: dict) -> BacktestOutput:
    """Deserialize a plain dict back to BacktestOutput."""
    require_keys(data, REQUIRED_BACKTEST_TOP_LEVEL_KEYS, "backtest_output")
    payload_version = str(data.get("schema_version", ""))
    if not is_schema_compatible(payload_version, SCHEMA_VERSION):
        raise ValueError(
            f"Incompatible schema_version: payload={payload_version}, expected_major={SCHEMA_VERSION.split('.')[0]}"
        )

    meta_raw = data["run_metadata"]
    for k in ("start_ts", "end_ts", "run_ts"):
        if isinstance(meta_raw.get(k), str):
            meta_raw[k] = _iso_to_dt(meta_raw[k])
    run_metadata = RunMetadata(**meta_raw)

    equity_curve = []
    for row in data.get("equity_curve", []):
        row = dict(row)
        row["ts"] = _iso_to_dt(row["ts"])
        equity_curve.append(EquityCurvePoint(**row))

    trades = []
    for row in data.get("trades", []):
        row = dict(row)
        row["entry_ts"] = _iso_to_dt(row["entry_ts"])
        row["exit_ts"] = _iso_to_dt(row["exit_ts"])
        trades.append(TradeRecord(**row))

    metrics = StrategyMetrics(**data["metrics"])

    return BacktestOutput(
        run_metadata=run_metadata,
        equity_curve=equity_curve,
        trades=trades,
        metrics=metrics,
    )


def save_backtest_output(
    output: BacktestOutput,
    directory: str | Path,
    *,
    save_equity_csv: bool = True,
) -> dict[str, Path]:
    """Persist a BacktestOutput to disk.

    Writes:
      <directory>/<run_id>.json   — full output
      <directory>/<run_id>_equity_curve.csv  — equity curve (if save_equity_csv=True)

    Returns a dict of {"json": path, "csv": path} with paths written.
    """
    output.validate()
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)

    run_id = output.run_metadata.run_id
    paths: dict[str, Path] = {}

    # JSON
    json_path = out_dir / f"{run_id}.json"
    json_path.write_text(
        json.dumps(_output_to_dict(output), indent=2, default=str),
        encoding="utf-8",
    )
    paths["json"] = json_path

    # CSV equity curve
    if save_equity_csv and output.equity_curve:
        csv_path = out_dir / f"{run_id}_equity_curve.csv"
        fieldnames = [
            "ts",
            "equity",
            "equity_unit",
            "ret_1d",
            "drawdown",
            "benchmark_equity",
            "benchmark_ret_1d",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for pt in output.equity_curve:
                row = asdict(pt)
                row["ts"] = _dt_to_iso(pt.ts)
                writer.writerow(row)
        paths["csv"] = csv_path

    return paths


def load_backtest_output(path: str | Path) -> BacktestOutput:
    """Load a BacktestOutput from a JSON file written by save_backtest_output."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return _dict_to_output(data)
