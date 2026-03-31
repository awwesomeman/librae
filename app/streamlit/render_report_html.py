#!/usr/bin/env python3
"""
Render a responsive combined HTML backtest report (brief + full) for multiple strategies.
Usage:
    python scripts/reporting/render_report_html.py \
        --inputs a.json b.json ... \
        --output report.html
"""
import json
import argparse
import html as html_mod
from pathlib import Path


# ---------------------------------------------------------------------------
# Formatting helpers (mirrored from render_report.py but kept self-contained)
# ---------------------------------------------------------------------------

def pct(x):
    if x is None:
        return '-'
    return f"{x * 100:.2f}%"


def num(x, d=2):
    if x is None:
        return '-'
    return f"{x:.{d}f}"


def esc(s):
    """HTML-escape a value (converts to str first)."""
    return html_mod.escape(str(s))


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 13px;
    background: #f4f6f9;
    color: #222;
    padding: 16px;
}
h1 { font-size: 1.3em; margin-bottom: 12px; color: #1a1a2e; }
h2 { font-size: 1.05em; margin: 12px 0 6px; color: #1a1a2e; border-bottom: 1px solid #ddd; padding-bottom: 3px; }
h3 { font-size: 0.95em; margin: 10px 0 4px; color: #333; }

/* ---- Summary table ---- */
.summary-wrapper { overflow-x: auto; margin-bottom: 24px; }
table.summary {
    border-collapse: collapse;
    min-width: 100%;
    background: #fff;
    border-radius: 6px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0,0,0,.08);
}
table.summary th, table.summary td {
    padding: 7px 12px;
    text-align: right;
    white-space: nowrap;
    border-bottom: 1px solid #eee;
}
table.summary th:first-child, table.summary td:first-child {
    text-align: left;
    font-weight: 600;
    color: #555;
    min-width: 140px;
}
table.summary thead th {
    background: #1a1a2e;
    color: #fff;
    font-weight: 600;
}
table.summary tbody tr:hover { background: #f0f4ff; }
table.summary tbody tr:last-child td { border-bottom: none; }

/* ---- Column card layout ---- */
.columns {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    align-items: flex-start;
}
.card {
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 1px 5px rgba(0,0,0,.09);
    padding: 14px 16px;
    flex: 1 1 260px;
    min-width: 260px;
}
.card h2 { font-size: 0.95em; }

/* ---- Metric tables inside cards ---- */
table.metrics {
    border-collapse: collapse;
    width: 100%;
    font-size: 0.88em;
    margin-top: 4px;
}
table.metrics th, table.metrics td {
    padding: 4px 8px;
    text-align: right;
    border-bottom: 1px solid #f0f0f0;
}
table.metrics th:first-child, table.metrics td:first-child {
    text-align: left;
    color: #555;
}
table.metrics thead th {
    background: #eef2ff;
    color: #333;
    font-weight: 600;
}
table.metrics tbody tr:last-child td { border-bottom: none; }

/* ---- Key-value pairs ---- */
dl.kv { margin: 4px 0; }
dl.kv dt { display: inline; font-weight: 600; color: #555; }
dl.kv dd { display: inline; margin-left: 4px; color: #222; }
dl.kv dd::after { content: '\\A'; white-space: pre; }

/* ---- Cost stress table ---- */
table.stress { font-size: 0.85em; }
table.stress thead th { background: #fff3e0; }

/* ---- Responsive ---- */
@media (max-width: 640px) {
    .columns { flex-direction: column; }
    .card { min-width: 0; }
}
"""

# ---------------------------------------------------------------------------
# HTML fragments
# ---------------------------------------------------------------------------

def _cost_text(data):
    """Return a display string for cost. Prefer round_trip_cost_text if present."""
    cs = data.get('cost_settings', {})
    if 'round_trip_cost_text' in cs:
        return esc(cs['round_trip_cost_text'])
    rt = cs.get('round_trip_cost')
    if rt is not None:
        return esc(str(rt))
    # fallback: reconstruct from fee/slippage/tax
    parts = []
    for k in ('fee', 'slippage', 'tax'):
        v = cs.get(k)
        if v is not None:
            parts.append(f"{k}={v}")
    return esc(', '.join(parts)) if parts else '-'


def _oos_block(data):
    return data.get('oos_final_once', data.get('oos', {}))


# ---- Summary table ----------------------------------------------------------

_SUMMARY_ROWS = [
    ('OOS Trades',      lambda o: esc(str(o.get('trades', '-')))),
    ('OOS Win Rate',    lambda o: esc(pct(o.get('win_rate')))),
    ('OOS Avg Ret',     lambda o: esc(pct(o.get('avg_ret')))),
    ('OOS PF',          lambda o: esc(num(o.get('pf')))),
    ('OOS Ann Return',  lambda o: esc(pct(o.get('ann_return')))),
    ('OOS Sharpe',      lambda o: esc(num(o.get('ann_sharpe')))),
    ('OOS Ann Vol',     lambda o: esc(pct(o.get('ann_vol')))),
    ('OOS MDD',         lambda o: esc(pct(o.get('mdd')))),
]


def render_summary_table(datasets):
    headers = ''.join(
        f'<th>{esc(d.get("strategy", f"Strategy {i+1}"))}</th>'
        for i, d in enumerate(datasets)
    )
    rows_html = []
    for label, fn in _SUMMARY_ROWS:
        cells = ''.join(
            f'<td>{fn(_oos_block(d))}</td>'
            for d in datasets
        )
        rows_html.append(f'<tr><td>{esc(label)}</td>{cells}</tr>')
    body = '\n'.join(rows_html)
    return (
        '<div class="summary-wrapper">\n'
        '<table class="summary">\n'
        f'<thead><tr><th>Metric</th>{headers}</tr></thead>\n'
        f'<tbody>\n{body}\n</tbody>\n'
        '</table>\n</div>'
    )


# ---- Per-strategy card -------------------------------------------------------

def _metric_table(periods_data, period_keys=('train', 'validation', 'oos_final_once')):
    """Build a compact metric table for given period blocks."""
    labels = {
        'train': 'Train',
        'validation': 'Val',
        'oos_final_once': 'OOS',
        'oos': 'OOS',
    }
    available = [(k, labels.get(k, k)) for k in period_keys if k in periods_data]
    if not available:
        return ''

    col_heads = ''.join(f'<th>{lbl}</th>' for _, lbl in available)

    metrics = [
        ('Trades',      lambda b: esc(str(b.get('trades', '-')))),
        ('Win Rate',    lambda b: esc(pct(b.get('win_rate')))),
        ('Avg Ret',     lambda b: esc(pct(b.get('avg_ret')))),
        ('PF',          lambda b: esc(num(b.get('pf')))),
        ('Ann Return',  lambda b: esc(pct(b.get('ann_return')))),
        ('Sharpe',      lambda b: esc(num(b.get('ann_sharpe')))),
        ('Ann Vol',     lambda b: esc(pct(b.get('ann_vol')))),
        ('MDD',         lambda b: esc(pct(b.get('mdd')))),
    ]
    rows = []
    for m_label, fn in metrics:
        cells = ''.join(f'<td>{fn(periods_data[k])}</td>' for k, _ in available)
        rows.append(f'<tr><td>{esc(m_label)}</td>{cells}</tr>')
    body = '\n'.join(rows)
    return (
        '<table class="metrics">\n'
        f'<thead><tr><th>Metric</th>{col_heads}</tr></thead>\n'
        f'<tbody>\n{body}\n</tbody>\n'
        '</table>'
    )


def _cost_stress_table(stress):
    """Build cost stress table from validation_cost_stress dict."""
    if not stress:
        return ''
    cost_keys = sorted(stress.keys(), key=lambda x: float(x))
    col_heads = ''.join(f'<th>cost={esc(k)}</th>' for k in cost_keys)

    metrics = [
        ('Ann Return',  lambda b: esc(pct(b.get('ann_return')))),
        ('Sharpe',      lambda b: esc(num(b.get('ann_sharpe')))),
        ('MDD',         lambda b: esc(pct(b.get('mdd')))),
    ]
    rows = []
    for m_label, fn in metrics:
        cells = ''.join(f'<td>{fn(stress[k])}</td>' for k in cost_keys)
        rows.append(f'<tr><td>{esc(m_label)}</td>{cells}</tr>')
    body = '\n'.join(rows)
    return (
        '<table class="metrics stress">\n'
        f'<thead><tr><th>Metric</th>{col_heads}</tr></thead>\n'
        f'<tbody>\n{body}\n</tbody>\n'
        '</table>'
    )


def _params_str(params):
    if not params:
        return '-'
    return esc(', '.join(f'{k}={v}' for k, v in params.items()))


def _period_range(p):
    if not p:
        return '-'
    return esc(f"{p.get('start', '?')} → {p.get('end', '?')}")


def render_strategy_card(data):
    strat = esc(data.get('strategy', '-'))
    symbol = esc(data.get('symbol', '-'))
    data_src = esc(data.get('data', '-'))
    protocol = esc(data.get('protocol', '-'))
    cost_display = _cost_text(data)
    params = _params_str(data.get('chosen_params'))

    sp = data.get('sample_periods', {})
    periods_html = ''.join(
        f'<dt>{esc(k.capitalize())}:</dt><dd>{_period_range(sp.get(k))}</dd>'
        for k in ('train', 'validation', 'oos')
        if k in sp
    )

    # collect available period blocks in data
    period_blocks = {}
    for k in ('train', 'validation', 'oos_final_once', 'oos'):
        if k in data and isinstance(data[k], dict):
            period_blocks[k] = data[k]

    period_order = [k for k in ('train', 'validation', 'oos_final_once', 'oos') if k in period_blocks]
    metrics_html = _metric_table(period_blocks, period_order)

    stress = data.get('validation_cost_stress', {})
    stress_html = ''
    if stress:
        stress_table = _cost_stress_table(stress)
        stress_html = f'<h3>Cost Stress (Validation)</h3>\n{stress_table}'

    return f"""<div class="card">
<h2>{strat}</h2>
<dl class="kv">
<dt>Symbol:</dt><dd>{symbol}</dd>
<dt>Data:</dt><dd>{data_src}</dd>
<dt>Cost:</dt><dd>{cost_display}</dd>
<dt>Params:</dt><dd>{params}</dd>
<dt>Protocol:</dt><dd>{protocol}</dd>
</dl>
<h3>Sample Periods</h3>
<dl class="kv">{periods_html}</dl>
<h3>Train / Validation / OOS Metrics</h3>
{metrics_html}
{stress_html}
</div>"""


# ---------------------------------------------------------------------------
# Full page assembly
# ---------------------------------------------------------------------------

def render_page(datasets):
    summary = render_summary_table(datasets)
    cards = '\n'.join(render_strategy_card(d) for d in datasets)
    date_str = esc(
        __import__('datetime').date.today().isoformat()
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backtest Report — {date_str}</title>
<style>
{_CSS}
</style>
</head>
<body>
<h1>Backtest Combined Report — {date_str}</h1>
<h2>OOS Summary Comparison</h2>
{summary}
<h2>Strategy Details</h2>
<div class="columns">
{cards}
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='Render combined HTML backtest report.')
    ap.add_argument('--inputs', nargs='+', required=True, metavar='JSON',
                    help='One or more backtest JSON result files.')
    ap.add_argument('--output', required=True, metavar='HTML',
                    help='Output HTML file path.')
    args = ap.parse_args()

    datasets = []
    for path in args.inputs:
        text = Path(path).read_text(encoding='utf-8')
        # Strip any leading non-JSON lines (e.g. log preamble)
        lines = text.splitlines()
        # Find first line starting with '{'
        for i, line in enumerate(lines):
            if line.strip().startswith('{'):
                text = '\n'.join(lines[i:])
                break
        datasets.append(json.loads(text))

    html_out = render_page(datasets)
    Path(args.output).write_text(html_out, encoding='utf-8')
    print(f"Wrote {args.output}  ({len(html_out):,} bytes)")


if __name__ == '__main__':
    main()
