"""查看各项目讲义的生成进度。

读取 repos_state.json，按仓库打印进度条，并统计每篇讲义的状态与累计花费。
纯只读操作，不修改任何数据，可重复执行。

运行方式：uv run codereader-status
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import config as cfg

# 讲义的五种状态按此顺序展示。done 与 keep 均视为「已完成」，在进度条中一并计入分子。
STATUS_ORDER = ("done", "keep", "pending", "failed", "abandoned")


def main() -> int:
    # Windows 控制台默认按 GBK 解码，打印 emoji/中文/块字符时会报错；切换到 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    # 状态文件默认为仓库根目录下的 repos_state.json；可通过 RC_STATE_FILE 指定其他快照。
    state_file = Path(os.environ.get("RC_STATE_FILE") or (cfg.ROOT / "repos_state.json"))
    # 状态文件不存在，通常表示尚未运行过；直接退出。
    if not state_file.exists():
        print(f"无状态文件：{state_file}")
        return 0
    # 状态文件可能正在被分析进程写入，读取时会遇到不完整的 JSON。捕获该情况并提示后退出。
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"WARN {state_file} 损坏或正在写入，无法读取")
        return 0
    repos = state.get("repos", {})
    if not repos:
        print("（state 为空，还没有任何项目跑过）")
        return 0

    print(f"状态文件: {state_file}\n")
    for name, e in repos.items():
        lec = e.get("lectures", {}) or {}
        counts = dict.fromkeys(STATUS_ORDER, 0)
        cost = 0.0
        for v in lec.values():
            s = v.get("status", "?")
            counts[s] = counts.get(s, 0) + 1
            c = v.get("cost", 0)
            # cost 偶尔可能不是数字，稳妥起见先判断类型再累加。
            if isinstance(c, (int, float)):
                cost += c
        tot = len(lec)
        done = counts["done"] + counts["keep"]
        pct = (100.0 * done / tot) if tot else 0.0
        filled = int(pct / 5)
        print(f"● {name}" + ("  [BLOCKED]" if e.get("disabled") else ""))
        print(f"  phase={e.get('phase', '?')}  v{e.get('version', 0)}  "
              f"head={(e.get('manifest_head') or '?')[:10]}  讲义 {done}/{tot} "
              f"[{'█' * filled}{'░' * (20 - filled)}] {pct:.0f}%  累计 ${cost:.2f}")
        print("  " + "  ".join(f"{k}={counts[k]}" for k in STATUS_ORDER if counts.get(k)))
        if e.get("blocked"):
            # 配置错误（子目录不存在 / 仓库被删改名）：重试无益，会在阈值后摘出队列。
            print(f"  blocked x{e['blocked']}"
                  f"（{'已摘出队列，冷却后自动重试' if e.get('disabled') else '再失败将摘出队列'}）: "
                  f"{(e.get('blocked_reason') or '')[:110]}")
        if e.get("last_error"):
            print(f"  last_error: {e['last_error'][:140]}")
        for lid, v in lec.items():
            if v.get("status") in ("pending", "failed", "abandoned"):
                print(f"    · {lid}: {v.get('status')} retries={v.get('retries', 0)} "
                      f"{(v.get('error', '') or '')[:70]}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
