"""进程级共享状态：全局停止事件、状态写锁、软截止时钟、clone 锁。

跨模块共享的可变状态集中于此，以避免循环导入。main() 进入时清除停止事件并设置
软截止时钟；provider 的 429 退避在 deadline 到达后放弃重试。
"""
from __future__ import annotations

import threading
from pathlib import Path

# 任一仓库遇到额度耗尽、死区或软截止时设置该事件；其余仓库在各自讲义边界
# 检查该事件并有序退出。
GLOBAL_STOP = threading.Event()
# 使用 RLock 而非 Lock：save_state 可能被嵌套调用，RLock 允许同一线程重复加锁。
GLOBAL_STATE_LOCK = threading.RLock()

# 软截止时钟（仅赋值一次、只读），由 main() 写入，provider 的 429 退避循环读取。
RUN_START_MONO = 0.0
RUN_DEADLINE_MIN = 0.0

# 按 clone 目录加锁：同一父仓库的多个 folder 条目共享同一份 clone，并发的
# fetch/checkout 可能竞争 .git/index.lock，必须串行执行。
_CLONE_LOCKS: dict[str, threading.Lock] = {}
_CLONE_LOCKS_GUARD = threading.Lock()


def clone_lock(clone_dir: Path) -> threading.Lock:
    """获取（或创建）指定 clone 目录专属的锁。"""
    key = str(clone_dir)
    with _CLONE_LOCKS_GUARD:
        lk = _CLONE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _CLONE_LOCKS[key] = lk
        return lk
