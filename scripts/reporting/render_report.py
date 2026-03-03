#!/usr/bin/env python3
"""
Render brief/full markdown report from backtest json result.
"""
import json
import argparse
from pathlib import Path


def pct(x):
    if x is None:
        return '-'
    return f"{x*100:.2f}%"


def num(x, d=2):
    if x is None:
        return '-'
    return f"{x:.{d}f}"


def render_brief(data):
    isd = data.get('train', {})
    oos = data.get('oos_final_once', data.get('oos', {}))
    lines = []
    lines.append(f"## A) 一頁摘要")
    lines.append(f"- 策略名稱：{data.get('strategy','-')}")
    lines.append(f"- 結論：{data.get('conclusion','待填')}")
    lines.append("")
    lines.append("## B) 核心績效")
    lines.append(f"- IS 交易次數：{isd.get('trades','-')} | OOS 交易次數：{oos.get('trades','-')}")
    lines.append(f"- IS 勝率：{pct(isd.get('win_rate'))} | OOS 勝率：{pct(oos.get('win_rate'))}")
    lines.append(f"- IS 平均報酬率：{pct(isd.get('avg_ret'))} | OOS 平均報酬率：{pct(oos.get('avg_ret'))}")
    lines.append(f"- IS PF：{num(isd.get('pf'))} | OOS PF：{num(oos.get('pf'))}")
    lines.append(f"- IS 年化報酬：{pct(isd.get('ann_return'))} | OOS 年化報酬：{pct(oos.get('ann_return'))}")
    lines.append(f"- IS 夏普：{num(isd.get('ann_sharpe'))} | OOS 夏普：{num(oos.get('ann_sharpe'))}")
    lines.append(f"- IS 波動：{pct(isd.get('ann_vol'))} | OOS 波動：{pct(oos.get('ann_vol'))}")
    lines.append(f"- IS MDD：{pct(isd.get('mdd'))} | OOS MDD：{pct(oos.get('mdd'))}")
    return '\n'.join(lines)


def render_full(data):
    # keep concise but structured
    lines = ["# 詳細版回測報告", "", f"- 策略：{data.get('strategy','-')}", f"- 協議：{data.get('protocol','-')}", f"- 參數：{data.get('chosen_params',{})}", "", render_brief(data)]
    if 'validation_cost_stress' in data:
        lines.append("\n## 成本壓測")
        for k,v in data['validation_cost_stress'].items():
            lines.append(f"- cost={k}: ann={pct(v.get('ann_return'))}, sharpe={num(v.get('ann_sharpe'))}, mdd={pct(v.get('mdd'))}")
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True)
    ap.add_argument('--mode', choices=['brief','full'], default='brief')
    ap.add_argument('--output', required=True)
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text(encoding='utf-8'))
    text = render_brief(data) if args.mode == 'brief' else render_full(data)
    Path(args.output).write_text(text, encoding='utf-8')
    print(args.output)


if __name__ == '__main__':
    main()
