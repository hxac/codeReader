"""时间与死区：UTC 时间计算、死区判定、软截止。全部按 UTC 计算，与部署机器的本地时区无关。"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from . import config as cfg


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_hour(now: datetime) -> float:
    """将时刻换算为带小数的 UTC 小时，例如 1 点 30 分为 1.5。"""
    return now.hour + now.minute / 60.0 + now.second / 3600.0


def in_dead_zone(now: datetime | None = None) -> bool:
    """判断当前是否处于死区。死区可跨午夜：START 大于 END 时按环绕（wrap-around）处理。"""
    h = _utc_hour(now or _now())
    if cfg.DEAD_START < cfg.DEAD_END:
        return cfg.DEAD_START <= h < cfg.DEAD_END
    return h >= cfg.DEAD_START or h < cfg.DEAD_END   # 跨午夜：[START,24) ∪ [0,END)


def near_dead_zone(now: datetime | None = None) -> bool:
    """判断当前是否进入死区前的软停窗口；跨午夜场景同样正确处理。"""
    h = _utc_hour(now or _now())
    start = cfg.DEAD_START - cfg.SOFT_MARGIN_HOURS   # 软停窗口起点，可能 <0（跨午夜）
    end = cfg.DEAD_START                             # 软停窗口终点即死区起点
    if start >= 0 and start < end:
        return start <= h < end
    start %= 24   # 例：START=1、margin=2 → start=-1，取模得 23，窗口为 23 至 1
    return h >= start or h < end


def deadzone_blocked() -> bool:
    """死区的硬性守卫：当前处于死区或即将进入死区，且既非 DEBUG 模式也未显式放行。"""
    return not (cfg.DEBUG or cfg.env_truthy("RC_ALLOW_DEADZONE")) and (in_dead_zone() or near_dead_zone())


def deadline_reached(run_start: float, deadline_min: float) -> bool:
    """判断软截止时间是否已到；deadline_min=0 表示禁用，恒返回 False。

    在每个仓库、每篇讲义的边界均会检查。设置软截止是为了在 GitHub Actions 硬超时
    之前有序收尾，避免进程被强制终止而进度未落盘。
    """
    return deadline_min > 0 and (time.monotonic() - run_start) / 60.0 >= deadline_min
