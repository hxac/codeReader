"""环境与路径配置：所有从环境变量读取的常量集中于此，在模块导入时确定。

selftest 会在导入前设置 RC_* 环境变量；个别路径常量（STATE_FILE、WORK_DIR 等）
还需在运行时被测试修改，因此调用方一律通过 `cfg.XXX` 在调用时取值，
不要 `from .config import XXX` 之后再引用。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # src/codereader/ → 控制仓库根目录

# Windows 控制台默认 GBK，日志里出现 ⚠/⛔ 这类符号会抛 UnicodeEncodeError 把管线打断。
# 统一改成「不可编码字符替换」而不是崩溃；UTF-8 终端（CI 全部如此）行为不变。
try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001 - 老解释器/被重定向的 stdout 忽略即可
    pass


def env_truthy(name: str, default: bool = False) -> bool:
    """将环境变量解析为布尔值：1/true/yes/on 视为真，其余视为假。

    未设置时返回 default。用于读取 RC_DEBUG、RC_MOCK 等开关量。
    """
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_path(name: str, fallback: Path) -> Path:
    return Path(os.environ.get(name) or fallback)


def log(msg: str) -> None:
    """统一以 [readcode] 前缀输出日志；flush=True 确保 CI 环境中实时可见。"""
    print(f"[readcode] {msg}", flush=True)


def phase_log(repo_name: str, phase: str, detail: str = "") -> None:
    """输出带仓库与阶段标签的日志。"""
    log(f"{repo_name} {phase}" + (f" {detail}" if detail else ""))


def annotation(kind: str, msg: str) -> None:
    """输出 GitHub Actions 注解（warning/error/notice）。

    只在 Actions 里输出：本地跑管线时终端不该被 `::warning::` 这类协议行污染。
    注解会出现在 run 摘要里，用来让「配置错误被跳过」这类不失败但空转的状态可见。
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{kind}::{msg}", flush=True)


# debug 模式：固定单仓库、只生成 1 篇讲义、忽略死区。默认将 state/work/tutorials
# 移入 .debug/ 沙箱；RC_DEBUG_PERSIST=1 时改用正式路径。
DEBUG = env_truthy("RC_DEBUG")
DEBUG_PERSIST = env_truthy("RC_DEBUG_PERSIST")
_DEBUG_SANDBOX = DEBUG and not DEBUG_PERSIST

# 关键路径均可被环境变量覆盖（selftest 借此将一切指向临时目录）。缺省位于控制仓库内。
REPOS_YML = _env_path("RC_REPOS_YML", ROOT / "repos.yml")
STATE_FILE = _env_path("RC_STATE_FILE",
                       (ROOT / ".debug" / "state.json") if _DEBUG_SANDBOX else (ROOT / "repos_state.json"))
WORK_DIR = _env_path("RC_WORK_DIR",
                     (ROOT / ".debug" / "work") if _DEBUG_SANDBOX else (ROOT / "work"))
TUTORIALS_DIR = _env_path("RC_TUTORIALS_DIR",
                          (ROOT / ".debug" / "tutorials") if _DEBUG_SANDBOX else (ROOT / "tutorials"))
PROMPTS_DIR = _env_path("RC_PROMPTS_DIR", ROOT / "prompts")
PLANNER_TEMPLATE = PROMPTS_DIR / "planner.prompt.md"
WORKER_TEMPLATE = PROMPTS_DIR / "worker.prompt.md"

# ---- 死区（UTC 小时）与软停余量 ----
DEAD_START = float(os.environ.get("RC_DEADZONE_UTC_START", "6"))
DEAD_END = float(os.environ.get("RC_DEADZONE_UTC_END", "10"))
SOFT_MARGIN_HOURS = float(os.environ.get("RC_DEADZONE_SOFT_MARGIN_MIN", "30")) / 60.0
# 校验：起止值均须在 [0,24) 内、margin 须小于 24 小时，否则软停窗口将覆盖全天，
# 导致任务永远被延后。
if not (0 <= DEAD_START < 24 and 0 <= DEAD_END < 24):
    raise SystemExit(f"RC_DEADZONE_UTC_START/END 必须在 [0,24)：got {DEAD_START}/{DEAD_END}")
if not (0 <= SOFT_MARGIN_HOURS < 24):
    raise SystemExit(f"RC_DEADZONE_SOFT_MARGIN_MIN 必须在 [0,1440)：got {SOFT_MARGIN_HOURS * 60}")

# ---- 并发 / 重试 / 退避 ----
CONCURRENCY = max(1, int(os.environ.get("RC_CONCURRENCY", "4")))
MAX_RETRIES = int(os.environ.get("RC_MAX_RETRIES", "5"))
# ---- 仓库级熔断（配置错误，不是限流）----
# 子目录不存在、上游仓库被删/改名这类错误重试不会自愈。同一条目连续 BLOCK_LIMIT 个
# 窗口失败即摘出队列，避免队头坏条目每轮吃光槽位、把后面健康的仓库饿死（2026-10
# 空转事故：4 个坏条目占满队列头，16 天零产出）。冷却 BLOCK_RETRY_HOURS 后自动放回
# 队列重试，上游补上目录即可自愈，无需人工清 state。
BLOCK_LIMIT = max(1, int(os.environ.get("RC_REPO_BLOCK_LIMIT", "3")))
BLOCK_RETRY_HOURS = float(os.environ.get("RC_REPO_BLOCK_RETRY_HOURS", "72"))
# 同一时间窗口内单篇 worker 失败后的当场重试次数；额度耗尽属全局停止条件，不计入。
INRUN_RETRIES = max(0, int(os.environ.get("RC_WORKER_INRUN_RETRIES", "1")))
BACKOFF_SEC = float(os.environ.get("RC_BACKOFF_SEC", "0"))
# 额度/限速退避：_invoke 中指数退避重试，次数用尽后向上抛出。base 1s、cap 60s。
QUOTA_RETRY_MAX = int(os.environ.get("RC_QUOTA_RETRIES", "10"))
QUOTA_RETRY_BASE = 1.0
QUOTA_RETRY_CAP = 60.0
MOCK = env_truthy("RC_MOCK")

# ---- 默认值 ----
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT = 3600
DEFAULT_WORKER_MAX_TURNS = "50"
DEFAULT_WORKER_BUDGET = "10.00"
DEFAULT_PLANNER_MAX_TURNS = "30"
DEFAULT_PLANNER_BUDGET = "3.00"
DEFAULT_SUMMARY_MAX_TURNS = "8"
DEFAULT_SUMMARY_BUDGET = "0.50"
