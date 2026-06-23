#!/usr/bin/env python3
"""看一眼各项目讲义生成到哪了。

它做的事很简单：读 repos_state.json，按仓库逐个打印一个进度小条，顺带把
每一篇讲义的状态数一数、花费加一加。纯只读，不改任何东西，跑多少次都安全。

跑法：python scripts/status.py
"""
import json
import os
import sys
from pathlib import Path

# Windows 的控制台默认按 GBK 解码，遇到 emoji、中文、还有下面要打印的那些块字符
# 就会崩。这里把标准输出强行切到 UTF-8。CI 那边本来就是 UTF-8，这一步等于空操作。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# 控制仓根目录，也就是 scripts 的上一层。
ROOT = Path(__file__).resolve().parent.parent
# 状态文件默认就在仓库根的 repos_state.json；想看别的快照，可以用 RC_STATE_FILE 指过去。
STATE = Path(os.environ.get("RC_STATE_FILE") or (ROOT / "repos_state.json"))
# 讲义的五种状态，按这个顺序展示。done 和 keep 都算「已完成」，进度条里它们一起计入分子。
STATUS_ORDER = ("done", "keep", "pending", "failed", "abandoned")

# 没有状态文件，多半是从没跑过，安安静静退出就行，别抛栈吓人。
if not STATE.exists():
    print(f"无状态文件：{STATE}")
    sys.exit(0)
# 状态文件可能正被 analyze 写到一半，这会儿去读就会撞上半个 JSON。抓下来提醒一句就退，
# 不当致命错误——稍等再跑一次通常就好了。
try:
    state = json.loads(STATE.read_text(encoding="utf-8"))
except json.JSONDecodeError:
    print(f"WARN {STATE} 损坏或正在写入，无法读取")
    sys.exit(0)
repos = state.get("repos", {})
if not repos:
    print("（state 为空，还没有任何项目跑过）")
    sys.exit(0)

print(f"状态文件: {STATE}\n")
for name, e in repos.items():
    # 这个仓库下所有讲义的逐篇状态记录。key 是讲义 id，value 里有 status、cost 这些。
    lec = e.get("lectures", {}) or {}
    # 先把五种状态都初始化成 0，下面再按实际状态往上加。
    counts = dict.fromkeys(STATUS_ORDER, 0)
    cost = 0.0
    for v in lec.values():
        s = v.get("status", "?")
        counts[s] = counts.get(s, 0) + 1
        c = v.get("cost", 0)
        # cost 偶尔可能不是数字，稳妥起见判一下类型再加。
        if isinstance(c, (int, float)):
            cost += c
    tot = len(lec)
    # done 和 keep 都算完成，合起来做分子算百分比。
    done = counts["done"] + counts["keep"]
    pct = (100.0 * done / tot) if tot else 0.0
    # 进度条画 20 格，每 5% 点亮一格。
    filled = int(pct / 5)
    print(f"● {name}")
    print(f"  phase={e.get('phase', '?')}  v{e.get('version', 0)}  "
          f"head={(e.get('manifest_head') or '?')[:10]}  讲义 {done}/{tot} "
          f"[{'█' * filled}{'░' * (20 - filled)}] {pct:.0f}%  累计 ${cost:.2f}")
    # 把非零的状态计数列出来，一眼看清还有几篇没动。
    print("  " + "  ".join(f"{k}={counts[k]}" for k in STATUS_ORDER if counts.get(k)))
    # 上次跑要是没干净收尾，这里会留一笔错误，截前 140 字符看个大概。
    if e.get("last_error"):
        print(f"  last_error: {e['last_error'][:140]}")
    # 没完成、失败、被放弃的讲义单独点出来，带上重试次数和错误摘要，方便定位。
    for lid, v in lec.items():
        if v.get("status") in ("pending", "failed", "abandoned"):
            print(f"    · {lid}: {v.get('status')} retries={v.get('retries', 0)} "
                  f"{(v.get('error', '') or '')[:70]}")
    print()
