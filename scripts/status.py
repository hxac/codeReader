#!/usr/bin/env python3
"""打印各项目讲义生成进度仪表盘（读 state/repos_state.json）。

跑法：python scripts/status.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("RC_STATE_FILE") or (ROOT / "state" / "repos_state.json"))

if not STATE.exists():
    print(f"无状态文件：{STATE}")
    sys.exit(0)

state = json.loads(STATE.read_text(encoding="utf-8"))
repos = state.get("repos", {})
if not repos:
    print("（state 为空，还没有任何项目跑过）")
    sys.exit(0)

print(f"状态文件: {STATE}\n")
for name, e in repos.items():
    phase = e.get("phase", "?")
    lec = e.get("lectures", {}) or {}
    counts = {"done": 0, "keep": 0, "pending": 0, "failed": 0, "abandoned": 0}
    cost = 0.0
    for v in lec.values():
        s = v.get("status", "?")
        counts[s] = counts.get(s, 0) + 1
        c = v.get("cost", 0)
        if isinstance(c, (int, float)):
            cost += c
    tot = len(lec)
    done = counts["done"] + counts["keep"]
    pct = (100.0 * done / tot) if tot else 0.0
    bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
    print(f"● {name}")
    print(f"  phase={phase}  v{e.get('version', 0)}  head={(e.get('manifest_head') or '?')[:10]}"
          f"  讲义 {done}/{tot} [{bar}] {pct:.0f}%  累计 ${cost:.2f}")
    detail = []
    for k in ("done", "keep", "pending", "failed", "abandoned"):
        if counts.get(k):
            detail.append(f"{k}={counts[k]}")
    print("  " + "  ".join(detail))
    if e.get("last_error"):
        print(f"  last_error: {e['last_error'][:140]}")
    for lid, v in lec.items():
        if v.get("status") in ("pending", "failed", "abandoned"):
            print(f"    · {lid}: {v.get('status')} retries={v.get('retries', 0)} "
                  f"{(v.get('error', '') or '')[:70]}")
    print()
