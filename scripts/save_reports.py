#!/usr/bin/env python3
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil


def copy_if(src: str | None, dst: Path):
    if not src:
        return
    p = Path(src)
    if p.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strategy', required=True)
    ap.add_argument('--brief')
    ap.add_argument('--full')
    ap.add_argument('--robust')
    ap.add_argument('--base', default='/home/jasonpan_subscribe/.openclaw/workspace/reports')
    args = ap.parse_args()

    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')
    strategy_dir = Path(args.base) / args.strategy
    history = strategy_dir / 'history'

    copy_if(args.brief, strategy_dir / 'latest_brief.md')
    copy_if(args.full, strategy_dir / 'latest_full.md')
    copy_if(args.robust, strategy_dir / 'latest_robust.md')

    copy_if(args.brief, history / f'{ts}_brief.md')
    copy_if(args.full, history / f'{ts}_full.md')
    copy_if(args.robust, history / f'{ts}_robust.md')

    print(str(strategy_dir))


if __name__ == '__main__':
    main()
