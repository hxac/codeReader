"""进度状态（repos_state.json）：读写、原子落盘、单篇讲义状态。

五种讲义状态集中定义于此；续传只需重新生成 pending/failed 状态的讲义。
"""
from __future__ import annotations

import json
from typing import Any

from . import config as cfg
from . import runtime

STATUS_DONE, STATUS_KEEP, STATUS_PENDING, STATUS_FAILED, STATUS_ABANDONED = (
    "done", "keep", "pending", "failed", "abandoned")
RETRYABLE = (STATUS_PENDING, STATUS_FAILED)


def load_state() -> dict:
    """读取持久化的进度状态。文件损坏或不存在时视为无状态，从头开始。"""
    if cfg.STATE_FILE.exists():
        try:
            return json.loads(cfg.STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg.log(f"WARN {cfg.STATE_FILE} 损坏，重置状态")
    return {"schema_version": 1, "repos": {}}


def save_state(state: dict) -> None:
    """将 state 写入磁盘：先写临时文件再原子替换，避免写入中途被打断而留下不完整的 JSON。

    整个写操作位于全局状态锁内，防止与并发 worker 对同一 state 的写入竞争。
    """
    with runtime.GLOBAL_STATE_LOCK:
        cfg.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg.STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cfg.STATE_FILE)


def _safe_save(state: dict, label: str) -> None:
    """save_state 的兜底：写入失败仅记录 WARN、不向上抛出，防止可控错误升级为 exit 1。"""
    try:
        save_state(state)
    except Exception as e:  # noqa: BLE001
        cfg.log(f"  WARN save_state 失败（{label}）：{type(e).__name__}: {e}；进度可能未落盘")


def get_entry(state: dict, name: str) -> dict:
    """获取某仓库的 state 记录；不存在时创建一个空记录。"""
    return state.setdefault("repos", {}).setdefault(name, {})


def lec_filename(lec: dict) -> str:
    return lec.get("filename") or f"{lec.get('id', 'lecture')}.md"


def lec_status(entry: dict, lid: str) -> str | None:
    return entry.get("lectures", {}).get(lid, {}).get("status")


def write_lecture_status(entry: dict, lid: str | None, *, status: str, action: str,
                         retries: Any, cost: float | None = None,
                         error: str | None = None) -> None:
    """将一篇讲义的状态写入 entry。所有键每次均写全，cost/error 缺失时写 None。

    统一写全键是为了避免盘上多处写入时字段缺失导致 schema 漂移。
    """
    entry.setdefault("lectures", {})[lid] = {
        "status": status, "action": action, "retries": retries,
        "cost": cost, "error": error,
    }
