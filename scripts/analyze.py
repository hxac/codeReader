#!/usr/bin/env python3
"""codeReader 的编排层：两阶段管线，外加配额感知、时区死区和断点续传。

每个项目分两步走：
  第一步 planner：让 claude 把整个项目读一遍，吐出一份大纲 manifest，是一棵「单元 → 讲义」
    的树，每篇讲义上还挂着它该怎么处理。
  第二步 workers：对着需要处理的讲义，低并发地并行调 claude，一篇一篇生成或更新 md。

续传靠 state 文件记住每一篇的状态。五种状态分别是 pending、done、failed、keep、abandoned。
  planner 不是每次都跑，只在强制、没状态、HEAD 变了、manifest 丢了、或者还没进 workers 阶段
    这些情况下才重跑，省额度。
  续传只挑 pending 和 failed、而且没超过 RC_MAX_RETRIES 的讲义来生成。
额度耗尽或者快到死区了，就存盘、退出 75，把活留到下一个时间窗口。

退出码的约定：0 是全部完成，75 是还没做完但进度已经存好、等下轮续传，1 是真出了意外。

RC_MOCK=1 的时候用本地桩替代 claude，selftest 就靠它离线跑、不烧额度。
几条关键路径都能用环境变量盖掉：RC_REPOS_YML、RC_STATE_FILE、RC_WORK_DIR、
RC_TUTORIALS_DIR、RC_PROMPTS_DIR。

时区按 UTC 算。死区是 06:00 到 10:00，便宜的窗口在 00:00 到 04:00 和 10:00 到 14:00。

这个文件早先是由 analyze.py、claude_runner.py、promptkit.py 三个合并来的，provider 层和
提示词渲染层各自有注释隔开。哪天想换掉 claude 换个模型后端，只动 provider 层那一段就行。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent


def env_truthy(name: str, default: bool = False) -> bool:
    """把环境变量当成布尔开关来读：1、true、yes、on 都算真，其余算假。

    没设过就用 default。专门用来读 RC_DEBUG、RC_MOCK 这种开关量的。
    """
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# debug 模式专门用来低成本验证提示词：固定只跑一个仓库、只生成一篇、并且忽略死区。
# 默认还会把 state、work、tutorials 都挪到 .debug 沙箱里，免得污染正式进度；要是设了
# RC_DEBUG_PERSIST=1，就改回沿用正式路径。任何 RC_* 路径变量只要显式设了，仍然以它为准。
DEBUG = env_truthy("RC_DEBUG")
DEBUG_PERSIST = env_truthy("RC_DEBUG_PERSIST")
_DEBUG_SANDBOX = DEBUG and not DEBUG_PERSIST

# 几条关键路径都能被环境变量盖掉，selftest 就是靠这个把一切指到临时目录的。缺省都落在控制仓里。
REPOS_YML = Path(os.environ.get("RC_REPOS_YML") or (ROOT / "repos.yml"))
STATE_FILE = Path(os.environ.get("RC_STATE_FILE") or ((ROOT / ".debug" / "state.json") if _DEBUG_SANDBOX else (ROOT / "repos_state.json")))
WORK_DIR = Path(os.environ.get("RC_WORK_DIR") or ((ROOT / ".debug" / "work") if _DEBUG_SANDBOX else (ROOT / "work")))
TUTORIALS_DIR = Path(os.environ.get("RC_TUTORIALS_DIR") or ((ROOT / ".debug" / "tutorials") if _DEBUG_SANDBOX else (ROOT / "tutorials")))
PROMPTS_DIR = Path(os.environ.get("RC_PROMPTS_DIR") or (ROOT / "prompts"))
PLANNER_TEMPLATE = PROMPTS_DIR / "planner.prompt.md"
WORKER_TEMPLATE = PROMPTS_DIR / "worker.prompt.md"

# 死区按 UTC 小时算，下面这几个都从环境变量读。
DEAD_START = float(os.environ.get("RC_DEADZONE_UTC_START", "6"))
DEAD_END = float(os.environ.get("RC_DEADZONE_UTC_END", "10"))
SOFT_MARGIN_HOURS = float(os.environ.get("RC_DEADZONE_SOFT_MARGIN_MIN", "30")) / 60.0
# 给这两个值把个关：起止都得落在 0 到 24 之间，margin 也得小于 24 小时。不然软停窗口会
# 盖满一整天，结果就是永远软停、永远不干活。
if not (0 <= DEAD_START < 24 and 0 <= DEAD_END < 24):
    raise SystemExit(f"RC_DEADZONE_UTC_START/END 必须在 [0,24)：got {DEAD_START}/{DEAD_END}")
if not (0 <= SOFT_MARGIN_HOURS < 24):
    raise SystemExit(f"RC_DEADZONE_SOFT_MARGIN_MIN 必须在 [0,1440)：got {SOFT_MARGIN_HOURS*60}")
CONCURRENCY = max(1, int(os.environ.get("RC_CONCURRENCY", "4")))
MAX_RETRIES = int(os.environ.get("RC_MAX_RETRIES", "5"))
# 同一个时间窗口里，单篇 worker 失败后还能当场重试几次。注意额度耗尽不算在这里面，那种
# 情况是立刻停掉整批。它跟 RC_BACKOFF_SEC 配着用：失败后按这个秒数退避一下再重试。只有
# 当场重试也耗光了，才给 retries 加一，而 MAX_RETRIES 限的是窗口数，不是当场重试次数。
INRUN_RETRIES = max(0, int(os.environ.get("RC_WORKER_INRUN_RETRIES", "1")))
BACKOFF_SEC = float(os.environ.get("RC_BACKOFF_SEC", "0"))
# 额度/限速退避：_invoke 里遇到 QuotaExhaustedError 先指数退避重试，耗光才往上抛。
# 10 次、base 1s、cap 60s，最坏情况 ~1023s（约 17min）。
QUOTA_RETRY_MAX = int(os.environ.get("RC_QUOTA_RETRIES", "10"))
QUOTA_RETRY_BASE = 1.0
QUOTA_RETRY_CAP = 60.0
MOCK = env_truthy("RC_MOCK")
MOCK_PLANNER_CALLS = 0   # selftest 用：统计 mock planner 被调次数
MOCK_WORKER_CALLS = 0    # selftest 用：统计 mock worker 被调次数，用来验超时不当场重试
MOCK_SUMMARIZE_CALLS = 0  # selftest 用：统计 mock summarize 被调次数

# 全局并发控制：一把给所有仓库共享的停牌信号，外加一把写状态的锁。
# main 入口先 clear 停牌信号，之后任何一个仓库一旦碰上额度耗尽、死区或软截止，就把它 set 上；
# 其余仓库会在各自下一篇讲义的边界上检查到，然后有序退出。
GLOBAL_STOP = threading.Event()
# 用 RLock 而不是 Lock，是因为 save_state 可能嵌套调用，RLock 允许同一线程重复加锁。
GLOBAL_STATE_LOCK = threading.RLock()

# 按 clone 目录加锁：子目录拆分后，同一个父仓库的多个 folder 条目共享一份 clone，
# 它们会在 RC_CONCURRENCY 下并行跑 sync_repo，得让 fetch/checkout/reset 串行，
# 免得两个线程同时踩同一份 .git 的 index.lock。
_CLONE_LOCKS: dict[str, threading.Lock] = {}
_CLONE_LOCKS_GUARD = threading.Lock()


def _clone_lock(clone_dir: Path) -> threading.Lock:
    """取（或建）某个 clone 目录专属的锁。"""
    key = str(clone_dir)
    with _CLONE_LOCKS_GUARD:
        lk = _CLONE_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _CLONE_LOCKS[key] = lk
        return lk

# 讲义的五种状态串，集中定义在一处，免得字面量散得到处都是、迟早对不上。
STATUS_DONE, STATUS_KEEP, STATUS_PENDING, STATUS_FAILED, STATUS_ABANDONED = (
    "done", "keep", "pending", "failed", "abandoned")
# 续传时真正需要去生成的只有这两种。done、keep、abandoned 都直接跳过。
RETRYABLE = (STATUS_PENDING, STATUS_FAILED)


def _deadline_reached(run_start: float, deadline_min: float) -> bool:
    """软截止时间到了没有。

    在每个仓库、每篇讲义的边界上都会查一下。设这个软截止是为了赶在 GitHub Actions 的
    硬超时之前从容收尾，免得进程被强杀、进度没存盘。
    """
    return deadline_min > 0 and (time.monotonic() - run_start) / 60.0 >= deadline_min


# planner 输出的大纲 manifest 就按这个 JSON Schema 来约束。--json-schema 把它喂给 claude，
# 强制结构化输出，省得后面解析时还要猜格式。下面这棵树就是「单元 → 讲义」，每篇讲义带着
# 自己的 topic、要读哪些源文件、依赖哪几篇、以及这次该怎么处理。
MANIFEST_SCHEMA = {
    "type": "object",
    "required": ["project", "head", "units"],
    "properties": {
        "project": {"type": "string"},
        "head": {"type": "string"},
        "rationale": {"type": "string"},
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "lectures"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "level": {"type": "string", "enum": ["beginner", "intermediate", "advanced"]},
                    "order": {"type": "integer"},
                    "lectures": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "title", "filename", "topic"],
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "order": {"type": "integer"},
                                "filename": {"type": "string"},
                                "topic": {"type": "string"},
                                "level": {"type": "string", "enum": ["beginner", "intermediate", "advanced"]},
                                "learning_goals": {"type": "array", "items": {"type": "string"}},
                                "practice_task": {"type": "string"},
                                "source_files": {"type": "array", "items": {"type": "string"}},
                                "minimal_modules": {"type": "array", "items": {"type": "string"}},
                                "depends_on": {"type": "array", "items": {"type": "string"}},
                                "action": {"type": "string", "enum": ["keep", "update", "new", "rebuild"]},
                            },
                        },
                    },
                },
            },
        },
    },
}


# 一个待分析项目的全部信息，都是从 repos.yml 里读出来的。
@dataclass
class Repo:
    name: str            # owner/repo 形式的全名（子目录目标也是父仓库的 owner/repo）
    url: str             # clone 地址，缺省由 name 拼出来
    branch: str | None   # 指定分支；None 表示用默认分支
    project: str         # 项目短名，用来命名讲义目录；子目录目标 = {base}-{folder}
    focus: str           # 用户想让讲义侧重讲什么
    subpath: str = ""    # ""=整仓；非空=self 子目录目标（如 "clang"），只处理 clone 内该子目录


def repo_key(repo: "Repo") -> str:
    """fleet/state 用的唯一身份：整仓就是 name；子目录目标是 name@subpath。

    子目录目标（subpath 非空）和普通仓库同一等级，但共享父仓库的 clone，所以身份得带个子目录后缀
    做区分，免得 state key、教程目录、日志撞车。
    """
    return f"{repo.name}@{repo.subpath}" if repo.subpath else repo.name


def log(msg: str) -> None:
    """统一加上 [readcode] 前缀打一行日志，flush=True 保证 CI 里实时可见。"""
    print(f"[readcode] {msg}", flush=True)


def phase_log(repo_name: str, phase: str, detail: str = "") -> None:
    """打一条带仓库和阶段标签的日志，格式是 [readcode] 后跟 owner/repo、阶段、可选详情。"""
    log(f"{repo_name} {phase}" + (f" {detail}" if detail else ""))


def _rel_to_root(p: Path) -> str:
    """把一个路径转成相对控制仓根目录的 posix 写法，存 tutorial_path 就靠它。

    要是它不在根目录下面、没法相对，就回退成绝对路径。这里特意走 resolve 加 relative_to，
    而不是拿字符串前缀去比，是为了躲开 Windows 上大小写和斜杠方向带来的误判。
    """
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def _lec_filename(lec: dict) -> str:
    return lec.get("filename") or f"{lec.get('id', 'lecture')}.md"


def _lec_status(entry: dict, lid: str) -> str | None:
    return entry.get("lectures", {}).get(lid, {}).get("status")


def _write_lecture_status(entry: dict, lid: str, *, status: str, action: str,
                          retries, cost: float | None = None,
                          error: str | None = None) -> None:
    """把一篇讲义的状态写进 entry。所有键每次都写全，cost 和 error 缺了就给 None。

    之所以坚持写全键，是因为盘上有三处地方都在写状态，要是各写各的、漏几个键，schema 就会
    慢慢漂移、对不齐。
    """
    entry.setdefault("lectures", {})[lid] = {
        "status": status, "action": action, "retries": retries,
        "cost": cost, "error": error,
    }


# ========================================================================== #
#  provider 层：跟 claude 命令行打交道的地方                                  #
#                                                                             #
#  这一段最早是独立文件 claude_runner.py，后来并了进来。哪天想换掉 claude、    #
#  改用别的模型后端，只动这一段就够，上面的编排逻辑一行都不用碰。             #
#                                                                             #
#  claude 退出时的错误被分成两类，区别在于「该不该停掉整批」：                 #
#    QuotaExhaustedError —— 额度、限流、计费这类。一命中，编排层立刻存盘、     #
#      整批停手，等下一个时间窗口再续。                                        #
#    ClaudeRunnerError    —— 普通失败，比如单篇预算花超了。只把这一篇记成     #
#      失败，接着生成下一篇，不影响别的。                                      #
#                                                                             #
#  鉴权用的三个环境变量 ANTHROPIC_API_KEY、ANTHROPIC_AUTH_TOKEN、              #
#  ANTHROPIC_BASE_URL 全都从父进程继承，既不出现在代码里，也不会进 argv。      #
# ========================================================================== #
# 下面这些是只读工具，planner 和 worker 都靠它们读源码，谁也不会借机写盘。
READONLY_TOOLS = [
    "Read", "Grep", "Glob",
    "Bash(git log *)", "Bash(git diff *)", "Bash(git show *)",
    "Bash(git ls-files *)", "Bash(git rev-parse *)", "Bash(git remote *)",
]

# 单篇轮次超限的特征。命中它就当普通失败，只停这一篇。注意它必须在额度判断之前先跑，
# 否则 "max turns exceeded" 里那个 exceeded 会被下面的额度正则误抓，结果整批都被停掉。
_MAXTURNS_RE = re.compile(r"max(?:imum)?[\s_-]*turns?|turns?[\s_-]*(?:limit|exceed|reach)|reached[\s\w]{0,20}turn", re.I)
# 单篇预算超限的特征，命中同样是普通失败、只停这一篇。
# 这条故意不含 cost 和 usage 这两个裸词。因为第三方端点的额度报文里常带着 "cost limit" 或
# "usage limit"，一旦写进来就会被额度正则抢先命中、判成整体停，那就过头了。这里只认那些
# 明确属于单篇预算的说法。
_BUDGET_RE = re.compile(r"max.?budget|budget[\s_-]*(?:limit|exceed|reach)|spend(?:ing)?[\s_-]*(?:limit|exceed)|exceeded[\s\w]{0,15}(?:budget|spend)", re.I)
# 额度、限流、计费类错误的特征，命中就抛 QuotaExhaustedError、整批停。
# 这里特意把 cost limit 和 usage limit 也算进来，因为不少第三方端点拿 "cost limit exceeded"
# 来表示账户额度见底了。
_QUOTA_RE = re.compile(r"rate.?limit|429|overloaded|credit|quota|billing|insufficient|capacity|try again later|cost[\s_-]*limit|usage[\s_-]*limit|exceeded[\s\w]{0,15}(?:credit|quota|limit)", re.I)

# 环境变量的默认值集中放这儿，免得字面量散得到处都是。
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT = 3600
DEFAULT_WORKER_MAX_TURNS = "50"
DEFAULT_WORKER_BUDGET = "10.00"
DEFAULT_PLANNER_MAX_TURNS = "30"
DEFAULT_PLANNER_BUDGET = "3.00"
DEFAULT_SUMMARY_MAX_TURNS = "8"
DEFAULT_SUMMARY_BUDGET = "0.50"


@dataclass
class ClaudeConfig:
    model: str
    max_turns: str
    max_budget_usd: str
    timeout: int
    template_path: Path
    tutorial_dir: str          # 讲义目录名，相对 cwd。它圈定了 worker 能写盘的范围
    transcript_dir: Optional[Path] = None  # transcript 的 jsonl 存这儿；None 表示不存

    @classmethod
    def from_env(cls, template_path: Path, tutorial_dir: str,
                 max_turns: str | None = None, max_budget: str | None = None) -> "ClaudeConfig":
        """从环境变量凑一份配置。max_turns 和 max_budget 可以显式传进来盖掉默认值。"""
        return cls(
            model=os.environ.get("RC_MODEL", DEFAULT_MODEL),
            max_turns=max_turns or os.environ.get("RC_MAX_TURNS", DEFAULT_WORKER_MAX_TURNS),
            max_budget_usd=max_budget or os.environ.get("RC_MAX_BUDGET_USD", DEFAULT_WORKER_BUDGET),
            timeout=int(os.environ.get("RC_TIMEOUT", str(DEFAULT_TIMEOUT))),
            template_path=template_path,
            tutorial_dir=tutorial_dir,
        )


@dataclass
class RunnerResult:
    summary: str
    cost_usd: float
    session_id: Optional[str]
    raw: dict


class QuotaExhaustedError(RuntimeError):
    """API 额度或限流见底了。调用方拿到它就该存盘、把整批停下来。"""


class ClaudeRunnerError(RuntimeError):
    """普通失败：单篇预算花超、非零退出、JSON 解析挂了，都归这类。只影响这一篇。"""


class ClaudeTimeoutError(ClaudeRunnerError):
    """子进程跑超时了。run_lectures_sequential 会据此跳过当场重试、把这篇判失败、跨到下一篇，
    但不会停掉整轮。把它做成 ClaudeRunnerError 的子类，是为了让 worker 循环之外那些超时，
    比如 planner 的，也能被 except Exception 兜住。"""


def _worker_tools(tutorial_dir: str) -> list[str]:
    """worker 能用的工具：只读那套，再加上只准写讲义目录的 Write 和 Edit。

    把写权限严格圈在讲义目录里，是为了不让 worker 顺手改了被分析项目的源码。
    """
    return READONLY_TOOLS + [f"Write({tutorial_dir}/**)", f"Edit({tutorial_dir}/**)"]


def _classify_error(text: str) -> type | None:
    """看 claude 的报错文本，判断它属于哪一类失败。一条都没匹配上就返回 None。

    这里有个顺序上的讲究：必须先查 max-turns 和 budget，再查 quota。原因是 "max turns
    exceeded" 这句话里也带着 exceeded，要是先跑 quota 那条正则，单篇轮次超限会被错认成
    额度耗尽，结果一整批都被停掉，那就过头了。
    """
    if _MAXTURNS_RE.search(text) or _BUDGET_RE.search(text):
        return ClaudeRunnerError
    if _QUOTA_RE.search(text):
        return QuotaExhaustedError
    return None


def _build_argv(*, model: str, max_turns: str, max_budget: str, tools: list[str],
                system_prompt_file: Path | None = None, schema: str | None = None) -> list[str]:
    """拼出一次 claude 调用的命令行参数。

    这里只拼模型、轮次、预算、工具这些参数，鉴权的密钥一个都不往上放——它走的是环境变量，
    在 _invoke 里继承下去。这么做既不让密钥出现在进程参数里被人 ps 看到，也不会混进日志。
    """
    cmd = ["claude", "-p", "--model", model, "--output-format", "json",
           "--max-turns", max_turns, "--max-budget-usd", max_budget,
           "--allowedTools", ",".join(tools)]
    if system_prompt_file is not None:
        cmd += ["--append-system-prompt-file", str(system_prompt_file),
                "--exclude-dynamic-system-prompt-sections"]
    if schema is not None:
        cmd += ["--json-schema", schema]
    return cmd


def _harvest_transcripts(src_config_dir: Path, dst_dir: Path) -> None:
    """把一次 claude 调用留下的 transcript jsonl 收集到 dst_dir。

    是增量拷贝，目标里已有的同名文件就跳过，不会覆盖。中断后这些 transcript 会被 workflow
    单独捞到一个 transcripts 分支上，方便事后排查。
    """
    projects_dir = src_config_dir / "projects"
    if not projects_dir.is_dir():
        return
    for jsonl in sorted(projects_dir.rglob("*.jsonl")):
        if not jsonl.is_file():
            continue
        # 保留 session 子目录的路径结构，免得不同会话的同名文件互相覆盖。
        rel = jsonl.relative_to(projects_dir)
        dst = dst_dir / rel
        if dst.exists():
            continue   # 增量：已有的不覆盖
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(jsonl, dst)


def _invoke(cmd: list[str], repo_dir: Path, prompt_text: str, timeout: int,
            transcript_dir: Path | None = None) -> dict:
    """真正跑一次 claude 子进程，把它 stdout 里那段 JSON 解析出来返回。

    失败时不直接报错，而是按报错文本分类，抛对应的异常。要是传了 transcript_dir，就为这次
    调用单独建一个临时的 CLAUDE_CONFIG_DIR，等 claude 退出后再把里面的 jsonl 增量拷过去。

    额度/限速类错误（QuotaExhaustedError）会在本函数内做指数退避重试（最多 QUOTA_RETRY_MAX
    次，base 1s，cap 60s），全部耗尽才往上抛；其他错误立刻抛，不重试。
    """
    # Windows 上有个坑：npm 全局装的 claude 其实是个 .cmd 垫片，CreateProcess 按裸名字
    # "claude" 去找是找不到的。所以这里显式换成 claude.cmd，并且保持 shell=False——这样
    # argv 的引号由 Python 的 list2cmdline 正确处理，像 --json-schema 那种带 JSON 的参数
    # 才不会被 cmd.exe 拆坏。
    if sys.platform == "win32" and cmd and cmd[0] == "claude":
        cmd = ["claude.cmd", *cmd[1:]]

    last_quota_err: QuotaExhaustedError | None = None
    for retry in range(QUOTA_RETRY_MAX):
        env = None
        tmpdir = None
        if transcript_dir is not None:
            tmpdir = Path(tempfile.mkdtemp(prefix="claude-config-"))
            env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmpdir)}
        try:
            proc = subprocess.run(
                cmd, cwd=str(repo_dir), input=prompt_text,
                capture_output=True, text=True, encoding="utf-8",
                timeout=timeout, env=env,
            )
        except FileNotFoundError as e:
            raise ClaudeRunnerError(
                "`claude` CLI 未在 PATH 中找到；确认已 npm install -g @anthropic-ai/claude-code"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise ClaudeTimeoutError(f"claude 超时（{timeout}s）") from e
        finally:
            if tmpdir is not None:
                _harvest_transcripts(tmpdir, transcript_dir)
                shutil.rmtree(tmpdir, ignore_errors=True)

        if proc.returncode != 0:
            text = (proc.stderr or "") + "\n" + (proc.stdout or "")
            tail = text[-2000:]
            err_type = _classify_error(text)
            if err_type is QuotaExhaustedError:
                last_quota_err = QuotaExhaustedError(f"API 额度/限流耗尽：\n{tail}")
                if retry < QUOTA_RETRY_MAX - 1:
                    delay = min(QUOTA_RETRY_BASE * (2 ** retry), QUOTA_RETRY_CAP)
                    log(f"  限速/额度退避 {delay:.0f}s（第 {retry + 1}/{QUOTA_RETRY_MAX} 次）"
                        f" — {str(last_quota_err)[:120]}")
                    time.sleep(delay)
                    continue
                raise last_quota_err
            if err_type is ClaudeRunnerError:
                raise ClaudeRunnerError(f"claude 非零退出（分类匹配）：\n{tail}")
            raise ClaudeRunnerError(f"claude 退出码 {proc.returncode}：\n{tail}")

        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ClaudeRunnerError(
                f"claude 输出不是 JSON：{e}\n--- stdout 末尾 ---\n{proc.stdout[-2000:]}"
            ) from e

    # 理论上走不到这里（循环最后会 raise），但 pyright 需要它
    assert last_quota_err is not None
    raise last_quota_err


def run_planner(repo_dir: Path, prompt_text: str, cfg: ClaudeConfig,
                schema_json: str) -> dict:
    """让 claude 读一遍项目，吐出大纲 manifest，是个 dict。靠 --json-schema 把结构钉死。

    planner 的轮次和预算用的是它自己那两个专用环境变量 RC_PLANNER_MAX_TURNS 和
    RC_PLANNER_MAX_BUDGET_USD，默认 30 轮、3 美元，刻意不用 cfg 里的 worker 上限。原因很
    简单：planner 只是个只读的一次性调用，上限应当远小于 worker 那种 50 轮、10 美元，不然
    一不小心就把额度烧没了。cfg 在这里只贡献 model、template_path 和 timeout，这几个是
    planner 和 worker 共用的。
    """
    cmd = _build_argv(
        model=cfg.model,
        max_turns=os.environ.get("RC_PLANNER_MAX_TURNS", DEFAULT_PLANNER_MAX_TURNS),
        max_budget=os.environ.get("RC_PLANNER_MAX_BUDGET_USD", DEFAULT_PLANNER_BUDGET),
        tools=READONLY_TOOLS, system_prompt_file=cfg.template_path, schema=schema_json)
    payload = _invoke(cmd, repo_dir, prompt_text, cfg.timeout, transcript_dir=cfg.transcript_dir)
    # --json-schema 的结构化结果落在 structured_output 里；老版本可能放在 result 那段 JSON 文本里。两种都接住。
    manifest = payload.get("structured_output")
    if manifest is None:
        result = (payload.get("result") or "").strip()
        try:
            manifest = json.loads(result) if result else None
        except json.JSONDecodeError as e:
            raise ClaudeRunnerError(f"planner result 非 JSON：{e}") from e
    if not isinstance(manifest, dict) or "units" not in manifest:
        raise ClaudeRunnerError(f"planner 未返回合法 manifest：{json.dumps(manifest)[:500]}")
    return manifest


def run_worker(repo_dir: Path, prompt_text: str, cfg: ClaudeConfig) -> RunnerResult:
    """生成或者更新一篇讲义 md，文件由 claude 自己动手写。返回它的总结文本和这次的花费。"""
    cmd = _build_argv(
        model=cfg.model, max_turns=cfg.max_turns, max_budget=cfg.max_budget_usd,
        tools=_worker_tools(cfg.tutorial_dir), system_prompt_file=cfg.template_path)
    payload = _invoke(cmd, repo_dir, prompt_text, cfg.timeout, transcript_dir=cfg.transcript_dir)
    return RunnerResult(
        summary=(payload.get("result") or "").strip(),
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        session_id=payload.get("session_id"), raw=payload)


def run_summarize(repo_dir: Path, prompt_text: str,
                  transcript_dir: Path | None = None) -> RunnerResult:
    """读一篇已经生成好的讲义 md，产出一段简短的中文摘要。只用只读工具，不带系统提示词文件。"""
    cmd = _build_argv(
        model=os.environ.get("RC_MODEL", DEFAULT_MODEL),
        max_turns=os.environ.get("RC_SUMMARY_MAX_TURNS", DEFAULT_SUMMARY_MAX_TURNS),
        max_budget=os.environ.get("RC_SUMMARY_MAX_BUDGET_USD", DEFAULT_SUMMARY_BUDGET),
        tools=READONLY_TOOLS)
    payload = _invoke(cmd, repo_dir, prompt_text, int(os.environ.get("RC_TIMEOUT", str(DEFAULT_TIMEOUT))),
                      transcript_dir=transcript_dir)
    return RunnerResult(
        summary=(payload.get("result") or "").strip(),
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        session_id=None, raw=payload)


# ========================================================================== #
#  提示词渲染 + 变更签名：早先的 promptkit.py                                       #
# ========================================================================== #
def _jinja_env(prompts_dir: Path) -> jinja2.Environment:
    """搭一个 Jinja2 环境，模板从 prompts 目录加载。

    用 StrictUndefined 是故意的：模板里用了哪个变量却没传，就直接报错，而不是悄悄渲染成空。
    autoescape 关掉，因为产物是喂给 claude 的纯文本提示词，不是 HTML，用不着转义。
    """
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(prompts_dir)),
        undefined=jinja2.StrictUndefined,
        # 控制 {% %} 标签产生的空行：块标签后换行去除、行首空白去除
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )


def render(prompts_dir: Path, name: str, **kwargs) -> str:
    """渲染 prompts 目录下的某个模板，比如 worker.task.md。哪个变量没传就直接抛错。"""
    return _jinja_env(prompts_dir).get_template(name).render(**kwargs)


def signature(prompts_dir: Path) -> str:
    """给 prompts 目录下所有 md 的内容算一个 sha256，取前 16 位十六进制当签名。

    系统提示词和任务提示词都算在内，随便哪个被编辑过，签名就会变。编排层就拿这个签名判断
    要不要触发全量重构。
    """
    h = hashlib.sha256()
    files = sorted(p.name for p in prompts_dir.glob("*.md"))
    for fn in files:
        h.update(fn.encode("utf-8"))
        h.update(b"\0")
        # 把换行统一成 LF。不然 Windows 在 autocrlf=true 下 checkout 出来是 CRLF，CI 那边
        # Linux 上却是 LF，签名就会对不上，白白误触发一次全量重构。
        content = (prompts_dir / fn).read_bytes().replace(b"\r\n", b"\n")
        h.update(content)
        h.update(b"\0")
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# MOCK 层：RC_MOCK=1 时拿这些桩顶替真正的 claude 调用，selftest 全靠它离线跑
# --------------------------------------------------------------------------- #
def _mock_planner(repo: Repo, head: str, plan_mode: str) -> dict:
    """planner 的桩：不调 claude，直接捏一份固定大纲出来，够 selftest 验流程就行。"""
    global MOCK_PLANNER_CALLS
    MOCK_PLANNER_CALLS += 1
    act = "new" if plan_mode == "full" else "update"
    return {
        "project": repo.project,
        "head": head,
        "rationale": "mock 大纲",
        "units": [{
            "id": "u1", "title": "单元1", "order": 1,
            "lectures": [
                {"id": "u1-l1", "title": "讲义1", "order": 1, "filename": "u1-l1.md",
                 "topic": "topic1", "source_files": ["src/a.rs"], "minimal_modules": ["m1"],
                 "depends_on": [], "action": act},
                {"id": "u1-l2", "title": "讲义2", "order": 2, "filename": "u1-l2.md",
                 "topic": "topic2", "source_files": ["src/b.rs"], "minimal_modules": ["m2"],
                 "depends_on": ["u1-l1"], "action": "keep" if plan_mode == "incremental" else "new"},
            ],
        }],
    }


def call_planner(repo: Repo, head: str, plan_mode: str, repo_dir: Path,
                 prompt: str, cfg: ClaudeConfig, schema_json: str) -> dict:
    if MOCK:
        return _mock_planner(repo, head, plan_mode)
    return run_planner(repo_dir, prompt, cfg, schema_json)


def call_worker(repo: Repo, repo_dir: Path, lec: dict, prompt: str,
                cfg: ClaudeConfig) -> RunnerResult:
    if MOCK:
        global MOCK_WORKER_CALLS
        MOCK_WORKER_CALLS += 1
        _fail = (os.environ.get("RC_MOCK_TIMEOUT_ON") or "").strip()
        if _fail and lec.get("id") == _fail:
            raise ClaudeTimeoutError(f"mock timeout（{lec.get('id')}）")
        fn = _lec_filename(lec)
        d = repo_dir / cfg.tutorial_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / fn).write_text(
            f"# {lec.get('title', '')}\n\n> mock 讲义 `{lec.get('id')}`\n\n"
            f"主题: {lec.get('topic', '')}\n\n\\[E = mc^2\\]\n", encoding="utf-8")
        return RunnerResult(summary=f"mock wrote {fn}", cost_usd=0.01, session_id=None, raw={})
    return run_worker(repo_dir, prompt, cfg)


# --------------------------------------------------------------------------- #
# 时区
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    """当前 UTC 时间。死区判断全程用 UTC，跟部署机器的本地时区无关。"""
    return datetime.now(timezone.utc)


def _utc_hour(now: datetime) -> float:
    """把一个时刻换算成带小数的 UTC 小时，比如 1 点 30 分就是 1.5。"""
    return now.hour + now.minute / 60.0 + now.second / 3600.0


def in_dead_zone(now: datetime | None = None) -> bool:
    """判断当前是不是落在死区里，时间按 UTC 算。死区可以跨午夜：START 大于 END 时按 wrap-around 处理。"""
    h = _utc_hour(now or _now())
    if DEAD_START < DEAD_END:
        return DEAD_START <= h < DEAD_END
    # 跨午夜：死区 = [START, 24) ∪ [0, END)
    return h >= DEAD_START or h < DEAD_END


def near_dead_zone(now: datetime | None = None) -> bool:
    """判断是不是快到死区了，margin 那段也得把跨午夜算对。"""
    h = _utc_hour(now or _now())
    start = DEAD_START - SOFT_MARGIN_HOURS   # 软停窗口的起点，可能小于 0，意味着跨午夜
    end = DEAD_START                          # 软停窗口的终点就是死区的起点
    if start >= 0 and start < end:
        return start <= h < end
    # 软停窗口跨午夜。比如 START 是 1、margin 是 2，那 start 就是 -1，对 24 取模得 23，
    # 软停窗口就成了 23 到 1 这一段。
    start %= 24
    return h >= start or h < end


def _deadzone_blocked() -> bool:
    """死区的硬守卫：正处在死区、或者快到了，并且既不是 DEBUG、也没显式放行，才算被挡住。"""
    return not (DEBUG or env_truthy("RC_ALLOW_DEADZONE")) and (in_dead_zone() or near_dead_zone())


# --------------------------------------------------------------------------- #
# repos.yml
# --------------------------------------------------------------------------- #
def load_repos() -> list[Repo]:
    """从 repos.yml 读出所有待分析的项目。

    name 是必填的，其余字段缺了就用合理默认。如果 name 以 https:// 开头，视为第三方仓库地址，
    自动推导 url 和 project；否则按 owner/repo 拼出 GitHub 地址，project 取 name 最后一段。
    最后如果设了 RC_REPO_NAME，就只留它指的那一个，方便 debug 单仓库。
    """
    if not REPOS_YML.exists():
        log(f"WARN 未找到 {REPOS_YML}")
        return []
    data = yaml.safe_load(REPOS_YML.read_text(encoding="utf-8")) or {}
    repos: list[Repo] = []
    for item in data.get("repos", []) or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        if name.startswith("https://"):
            # 第三方仓库：name 即 URL，自动推导 state key 和 project
            url = name
            name = name.removeprefix("https://")
            project = (item.get("project") or "").strip() or (
                url.rstrip("/").split("/")[-1].removesuffix(".git"))
        else:
            # url 和 project 缺省都由 name 推出来
            url = (item.get("url") or "").strip() or f"https://github.com/{name}.git"
            project = (item.get("project") or "").strip() or name.split("/")[-1]
        branch = (item.get("branch") or "").strip() or None
        focus = (item.get("focus") or "").strip()
        folders = item.get("folders") or []
        if folders:
            # 子目录拆分：每个 folder 展开成一个独立的头等 fleet 条目。
            # self → 整仓条目（subpath=""，project 取 base 名，不拼 "-self" 后缀）。
            # 其他值作为子目录路径（支持多级如 scipy/cluster），project 中 / 净化成 -。
            for f in folders:
                f = (str(f) or "").strip().strip("/")
                if not f:
                    continue
                if f == "self":
                    repos.append(Repo(name=name, url=url, branch=branch,
                                      project=project, focus=focus, subpath=""))
                else:
                    subproj = f.replace("/", "-")
                    repos.append(Repo(name=name, url=url, branch=branch,
                                      project=f"{project}-{subproj}", focus=focus, subpath=f))
        else:
            repos.append(Repo(name=name, url=url, branch=branch,
                              project=project, focus=focus, subpath=""))
    # RC_REPO_NAME 用来只处理某一个仓库，debug 单仓库模式就靠它。
    # 给父仓库名（owner/repo）→ 选中它的全部 folder；给完整的 name@subpath → 只选那一个。
    filt = (os.environ.get("RC_REPO_NAME") or "").strip()
    if filt:
        repos = [r for r in repos if repo_key(r) == filt or r.name == filt]
        if not repos:
            log(f"WARN RC_REPO_NAME={filt!r} 无匹配")
    return repos


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    """读出持久化的进度 state。文件损坏或不存在，就当作什么都没有，从头开始。"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"WARN {STATE_FILE} 损坏，重启")
    return {"schema_version": 1, "repos": {}}


def save_state(state: dict) -> None:
    """把 state 落盘。先写临时文件再原子替换，免得写到一半被打断、留下半个 JSON。

    整个写操作都在全局状态锁里，跟并发的 workers 抢同一份 state 时不会踩到彼此。
    """
    with GLOBAL_STATE_LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)


def get_entry(state: dict, name: str) -> dict:
    """取出某个仓库的 state 记录，没有就现场建一个空的。"""
    return state.setdefault("repos", {}).setdefault(name, {})


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
def _git(args: list[str], cwd: Path) -> str:
    """在 cwd 下跑一条 git 命令，返回 stdout。非零退出就直接抛错，把 stderr 截一段带上。"""
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


def _remote_branch_exists(repo_dir: Path, branch: str) -> bool:
    r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"origin/{branch}"],
                       cwd=str(repo_dir), capture_output=True, text=True)
    return r.returncode == 0


def sync_repo(repo: Repo) -> Path:
    """把被分析的项目弄到本地工作区，保证是最新的一份。

    已经 clone 过就 fetch 一下；第一次就 clone。指定了分支的话还要处理一个常见情况：上游把
    那个分支删了或者改名了，这时候 fallback 到默认分支，而不是直接报错挂掉。
    """
    safe = repo.name.replace("/", "-")
    repo_dir = WORK_DIR / safe
    # 同一父仓库的多个 folder 共享这份 clone：按目录加锁，串行 fetch/clone，避免并发踩 .git/index.lock。
    with _clone_lock(repo_dir):
        if repo_dir.exists() and (repo_dir / ".git").exists():
            phase_log(repo.name, "clone", "(fetch)")
            _git(["fetch", "--all", "--tags"], repo_dir)
            if repo.branch:
                # 先看看 origin 上这个分支还在不在。上游可能把它删了或改了名。
                if not _remote_branch_exists(repo_dir, repo.branch):
                    log(f"  WARN 分支 {repo.branch!r} 在 origin 不存在，"
                        f"fallback 到默认分支（repos.yml 的 branch 配置可能过期）")
                    # 取 origin/HEAD 指向的默认分支
                    try:
                        default = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo_dir)
                        default = default.replace("origin/", "")
                    except RuntimeError:
                        default = "main"
                        log(f"  WARN 无法确定默认分支，fallback 到 'main'")
                    _git(["checkout", default], repo_dir)
                    _git(["reset", "--hard", f"origin/{default}"], repo_dir)
                else:
                    _git(["checkout", repo.branch], repo_dir)
                    _git(["reset", "--hard", f"origin/{repo.branch}"], repo_dir)
            else:
                _git(["pull", "--ff-only"], repo_dir)
        else:
            if repo_dir.exists():
                shutil.rmtree(repo_dir, ignore_errors=True)
            phase_log(repo.name, "clone")
            if repo.branch:
                # 先 clone 默认分支，再检查目标分支是否存在
                _git(["clone", repo.url, str(repo_dir)], ROOT)
                if _remote_branch_exists(repo_dir, repo.branch):
                    _git(["checkout", repo.branch], repo_dir)
                else:
                    log(f"  WARN clone 后分支 {repo.branch!r} 在 origin 不存在，留在默认分支")
            else:
                _git(["clone", repo.url, str(repo_dir)], ROOT)
    return repo_dir


def head_of(repo_dir: Path) -> str:
    """返回工作区当前 HEAD 的完整 commit id。"""
    return _git(["rev-parse", "HEAD"], repo_dir)


# --------------------------------------------------------------------------- #
# 讲义注入 / 收割
# --------------------------------------------------------------------------- #
def inject_prior(control_tutorial: Path, clone_tutorial: Path) -> None:
    """把上一轮的旧讲义复制进 clone 的工作区，给 incremental 续上底子。没有旧讲义就什么都不做。"""
    if not control_tutorial.exists():
        return
    clone_tutorial.parent.mkdir(parents=True, exist_ok=True)
    if clone_tutorial.exists():
        shutil.rmtree(clone_tutorial)
    shutil.copytree(control_tutorial, clone_tutorial)
    log(f"  注入旧讲义 → clone")


def clear_clone_tutorial(clone_tutorial: Path) -> None:
    """把 clone 里的讲义目录整个删掉，full 模式重头生成就靠它先清场。"""
    if clone_tutorial.exists():
        shutil.rmtree(clone_tutorial)


def harvest(clone_tutorial: Path, control_tutorial: Path) -> None:
    """把 clone 里新生成的讲义收割进 control 目录，整个过程是原子替换，中途崩了也不会丢旧讲义。

    为什么搞这么复杂：Windows 上的 os.replace 和 os.rename 没法原子地替换一个非空目录。
    所以走三步：先把 clone 拷一份到同盘的 staging，再把旧目录改名成 .old、把 staging 改名
    成目标，最后删掉 .old。中间任何一步失败都能回滚，旧讲义保得住。
    """
    if not clone_tutorial.exists() or not any(clone_tutorial.iterdir()):
        raise RuntimeError(f"未生成讲义目录（或为空）：{clone_tutorial}")
    control_tutorial.parent.mkdir(parents=True, exist_ok=True)

    # staging 必须跟 control 同一个盘，跨盘 rename 会失败。
    staging = control_tutorial.parent / (control_tutorial.name + ".new")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(clone_tutorial, staging)

    if control_tutorial.exists():
        old = control_tutorial.parent / (control_tutorial.name + ".old")
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        control_tutorial.rename(old)             # 第一步：旧目录改名为 .old，原子
        try:
            staging.rename(control_tutorial)     # 第二步：staging 改名为目标，原子
        except OSError:
            old.rename(control_tutorial)         # 出问题就回滚，把旧的还回去
            raise
        try:
            shutil.rmtree(old, ignore_errors=True)  # 第三步：清理掉 .old
        except OSError:
            pass   # Windows 上偶尔文件被占用删不掉，下次 harvest 会先用 ignore_errors 清一遍
    else:
        staging.rename(control_tutorial)


def _normalize_filename(fn: str) -> str:
    """filename 只认裸文件名：任何目录前缀都丢掉，斜杠统一成正斜杠，并且保证以 .md 结尾。

    这是个防御：planner 偶尔会把「项目名-tutorial/」这种前缀也写进 filename，要是放任它，
    跟 tutorial_dir 一拼就会出现双层路径，类似 Mooncake-tutorial/Mooncake-tutorial/u1-l1.md。
    """
    fn = (fn or "").replace("\\", "/").strip().strip("/")
    fn = fn.split("/")[-1]            # basename，丢弃任何目录前缀
    if fn and not fn.lower().endswith(".md"):
        fn += ".md"
    return fn or "lecture.md"


def normalize_manifest_filenames(manifest: dict) -> None:
    """把 manifest 里所有讲义的 filename 就地规范成裸 basename，写盘前调一次就够。"""
    for u in manifest.get("units", []):
        for lec in u.get("lectures", []):
            if "filename" in lec:
                lec["filename"] = _normalize_filename(lec["filename"])


def _record_harvest(entry: dict, control_tutorial: Path, head: str, generated_at: str) -> None:
    """收割完成后，把讲义路径、这次对应的 HEAD 和时间记进 state，供续传和发布用。"""
    entry["tutorial_path"] = _rel_to_root(control_tutorial)
    entry["last_head"] = head
    entry["last_run_at"] = generated_at


# --------------------------------------------------------------------------- #
# prompt 组装
# --------------------------------------------------------------------------- #
def compose_planner_prompt(repo: Repo, mode: str, head: str, prev_head: str | None,
                           existing_manifest: str | None, tutorial_dir: str,
                           permalink_base: str) -> str:
    """真正的任务提示词是 prompts/planner.task.md 这个 Jinja2 模板，这里只负责把参数填进去。"""
    return render(PROMPTS_DIR, "planner.task.md",
        repo_name=repo.name, project=repo.project, tutorial_dir=tutorial_dir,
        mode=mode, head=head, permalink_base=permalink_base,
        user_focus=repo.focus or "（无）",
        prev_head=prev_head, existing_manifest=existing_manifest or "{}")


def compose_worker_prompt(repo: Repo, lec: dict, action: str, head: str,
                          prev_head: str | None, tutorial_dir: str,
                          permalink_base: str,
                          *, outline_prompt: str = "", outline_content: str = "",
                          prior_summaries: list[dict] | None = None) -> str:
    """真正的任务提示词是 prompts/worker.task.md 这个 Jinja2 模板，这里只负责填参数。

    outline_prompt、outline_content、prior_summaries 这几个是给讲义做链式前文用的，
    也就是让后一篇能看到前面的内容和摘要，目前可以留空。
    """
    return render(PROMPTS_DIR, "worker.task.md",
        repo_name=repo.name, project=repo.project, tutorial_dir=tutorial_dir,
        permalink_base=permalink_base, head=head, action=action, prev_head=prev_head,
        lec_id=lec.get("id") or "lecture",
        filename=_lec_filename(lec), title=lec.get("title") or "（无标题）", topic=lec.get("topic") or "（无主题）",
        level=lec.get("level") or "自动判断",
        learning_goals=lec.get("learning_goals") or [],
        practice_task=lec.get("practice_task") or "由 worker 根据主题和源码自行设计",
        minimal_modules=lec.get("minimal_modules") or [],
        source_files=lec.get("source_files") or [],
        depends_on=lec.get("depends_on") or [],
        outline_prompt=outline_prompt, outline_content=outline_content,
        prior_summaries=prior_summaries or [])


# --------------------------------------------------------------------------- #
# 摘要：每篇讲义配一份压缩版 sidecar，给后面的讲义当链式前文用
# --------------------------------------------------------------------------- #
def compose_summary_prompt(lec: dict, tutorial_dir: str) -> str:
    """渲染摘要任务提示词，模板是 prompts/summary.task.md，同样是 Jinja2。"""
    return render(PROMPTS_DIR, "summary.task.md",
        lec_id=lec.get("id") or "lecture",
        title=lec.get("title") or "（无标题）",
        filename=_lec_filename(lec), tutorial_dir=tutorial_dir)


def call_summarize(repo: Repo, repo_dir: Path, lec: dict,
                   tutorial_dir: str,
                   transcript_dir: Path | None = None) -> str:
    """调 claude 给一篇已经完成的讲义生成摘要文本。MOCK 模式下返回一个桩。"""
    if MOCK:
        global MOCK_SUMMARIZE_CALLS
        MOCK_SUMMARIZE_CALLS += 1
        return f"（mock 摘要：{lec.get('id')} {lec.get('title')}）"
    prompt = compose_summary_prompt(lec, tutorial_dir)
    r = run_summarize(repo_dir, prompt, transcript_dir=transcript_dir)
    return r.summary


def _collect_prior_summaries(ordered_lectures: list[dict], current_lec: dict,
                             clone_tutorial: Path, repo: Repo,
                             repo_dir: Path, tutorial_dir: str,
                             transcript_dir: Path | None = None) -> list[dict]:
    """把当前这篇之前所有讲义的摘要 sidecar 收集起来。哪篇缺了就当场补一次，并打个日志。"""
    summaries: list[dict] = []
    for lec in ordered_lectures:
        lid = lec.get("id")
        if lid == current_lec.get("id"):
            break
        if not lid:
            continue
        sidecar = clone_tutorial / f"{lid}.summary.md"
        if sidecar.exists():
            summary_text = sidecar.read_text(encoding="utf-8").strip()
        else:
            md_path = clone_tutorial / _lec_filename(lec)
            if md_path.exists():
                log(f"  按需补摘要（{lid}）")
                try:
                    summary_text = call_summarize(repo, repo_dir, lec, tutorial_dir,
                                                  transcript_dir=transcript_dir)
                    sidecar.write_text(summary_text, encoding="utf-8")
                except Exception as e:
                    log(f"  WARN 摘要失败（{lid}）：{e}；记空")
                    summary_text = f"（摘要生成失败）"
            else:
                summary_text = "（讲义文件缺失，无摘要）"
        summaries.append({"id": lid, "title": lec.get("title", ""),
                          "summary": summary_text})
    return summaries


# --------------------------------------------------------------------------- #
# workers：串行生成讲义，全程感知配额和时间，还支持链式前文
# --------------------------------------------------------------------------- #
def run_lectures_sequential(repo: Repo, repo_dir: Path,
                            ordered_lectures: list[dict], state: dict,
                            entry: dict, head: str, prev_head: str | None,
                            tutorial_dir: str, permalink_base: str,
                            worker_cfg: ClaudeConfig,
                            generated_at: str,
                            # 链式前文
                            outline_prompt: str = "",
                            outline_content: str = "",
                            ) -> dict:
    """按 manifest 的顺序，一篇接一篇串行地生成或更新讲义。

    每篇开跑前都查一遍全局停机、软截止和死区。done 和 keep 的讲义虽然跳过不生成，但仍然留
    在循环里走过，目的是顺手把它们的摘要 sidecar 收集起来，喂给后面讲义当链式前文。

    返回一个字典，里面有 done、failed、quota、deferred 四个计数或标志，含义跟早先的
    run_workers 一致。
    """
    res = {"done": 0, "failed": 0, "quota": False, "deferred": False}
    run_start = time.monotonic()
    deadline_min = float(os.environ.get("RC_RUN_DEADLINE_MIN", "0") or "0")
    total = len([l for l in ordered_lectures if _lec_status(entry, l["id"]) in RETRYABLE])

    n_done = 0
    for lec in ordered_lectures:
        # ---- 全局/时间守卫 ----
        if GLOBAL_STOP.is_set():
            res["deferred"] = res["deferred"] or (not res["quota"])
            return res
        if _deadline_reached(run_start, deadline_min):
            GLOBAL_STOP.set()
            res["deferred"] = True
            return res
        if _deadzone_blocked():
            GLOBAL_STOP.set()
            res["deferred"] = True
            return res

        lid = lec.get("id")
        st = entry.get("lectures", {}).get(lid, {})
        status = st.get("status")
        action = lec.get("action", "new")

        # done、keep、abandoned 都跳过生成，但循环照走，好在这收集前文摘要
        if status in (STATUS_DONE, STATUS_KEEP, STATUS_ABANDONED):
            continue

        # pending/failed → 生成
        clone_tutorial = repo_dir / tutorial_dir
        prior = _collect_prior_summaries(ordered_lectures, lec, clone_tutorial,
                                         repo, repo_dir, tutorial_dir,
                                         transcript_dir=worker_cfg.transcript_dir)
        prompt = compose_worker_prompt(repo, lec, action, head, prev_head,
                                       tutorial_dir, permalink_base,
                                       outline_prompt=outline_prompt,
                                       outline_content=outline_content,
                                       prior_summaries=prior)
        last_err: Exception | None = None
        for attempt in range(INRUN_RETRIES + 1):
            if GLOBAL_STOP.is_set():
                break
            try:
                r = call_worker(repo, repo_dir, lec, prompt, worker_cfg)
                # 校验 .md 写入并生成摘要 sidecar
                md_path = clone_tutorial / _lec_filename(lec)
                if md_path.exists():
                    try:
                        summary_text = call_summarize(repo, repo_dir, lec, tutorial_dir,
                                                        transcript_dir=worker_cfg.transcript_dir)
                        (clone_tutorial / f"{lid}.summary.md").write_text(
                            summary_text, encoding="utf-8")
                    except Exception as e:
                        log(f"  WARN 摘要失败（{lid}）：{e}")
                res["done"] += 1
                n_done += 1
                # 变更+存盘同处一个锁，防并发 json.dumps 期间的字典变更
                with GLOBAL_STATE_LOCK:
                    _write_lecture_status(entry, lid, status=STATUS_DONE, action=action,
                                          retries=st.get("retries", 0), cost=r.cost_usd)
                    save_state(state)
                phase_log(repo_key(repo), "讲义生成", f"({n_done}/{total}) {lid}")
                break
            except QuotaExhaustedError as e:
                # 退避 10 次已耗尽，暂停当前仓库；不设全局停牌、不连累其他仓库
                res["quota"] = True
                with GLOBAL_STATE_LOCK:
                    _write_lecture_status(entry, lid, status=STATUS_FAILED, action=action,
                                          retries=st.get("retries", 0),
                                          error=f"quota: {str(e)[:200]}")
                    save_state(state)
                return res
            except ClaudeTimeoutError as e:
                # 超时就不当场重试了，否则要白烧两个 RC_TIMEOUT。判这篇失败、跨到下一篇，本轮不停
                res["failed"] += 1
                with GLOBAL_STATE_LOCK:
                    _write_lecture_status(entry, lid, status=STATUS_FAILED, action=action,
                                          retries=st.get("retries", 0) + 1,
                                          error=f"timeout: {str(e)[:200]}")
                    save_state(state)
                break   # 跳过当场重试和 else，这篇已标失败，lecture 循环继续下一篇
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < INRUN_RETRIES and BACKOFF_SEC > 0:
                    time.sleep(BACKOFF_SEC)
        else:
            # 当场重试也耗光了，但不是因为超时
            res["failed"] += 1
            with GLOBAL_STATE_LOCK:
                _write_lecture_status(entry, lid, status=STATUS_FAILED, action=action,
                                      retries=st.get("retries", 0) + 1,
                                      error=f"{type(last_err).__name__}: {str(last_err)[:200]}")
                save_state(state)

    return res


# --------------------------------------------------------------------------- #
# 单项目处理
# --------------------------------------------------------------------------- #
def process_repo(repo: Repo, state: dict, generated_at: str) -> str:
    """处理单个项目从头到尾的全过程，返回 done、incomplete 或 skipped 三种之一。"""
    entry = get_entry(state, repo_key(repo))
    try:
        repo_dir = sync_repo(repo)
        head = head_of(repo_dir)
        # 子目录目标（self 标记）：claude 直接在该子目录里跑，把它当项目根，自然只看得到该子目录。
        # 共享父仓库 clone，所以 cwd、worker 写入、manifest、harvest 全部落到子目录下。
        work_cwd = repo_dir / repo.subpath if repo.subpath else repo_dir
        if repo.subpath and not work_cwd.exists():
            with GLOBAL_STATE_LOCK:
                entry["last_error"] = f"子目录不存在: {repo.subpath!r}（检查 repos.yml 的 folders）"
                entry["last_run_at"] = generated_at
                save_state(state)
            log(f"  WARN {repo_key(repo)} 子目录 {repo.subpath!r} 不存在，跳过")
            return "incomplete"
        tutorial_dir = f"{repo.project}-tutorial"
        owner, seg = repo.name.split("/", 1)
        clone_tutorial = work_cwd / tutorial_dir
        control_tutorial = TUTORIALS_DIR / owner / seg / tutorial_dir
        permalink_base = f"https://github.com/{repo.name}/blob/{head}/" + (f"{repo.subpath}/" if repo.subpath else "")

        # 手动逃生口 RC_FORCE_FULL 和 RC_MODE=full 只在非定时运行时才生效。定时运行会结构性地
        # 忽略它们，保证定时只会因为「提示词被改」或「第一次建」才走全量。手动 dispatch、本地
        # 跑、selftest 都不设 RC_EVENT_NAME，所以 scheduled 是 False，逃生口照常能用。
        scheduled = os.environ.get("RC_EVENT_NAME") == "schedule"
        manual_force = (not scheduled) and (env_truthy("RC_FORCE_FULL") or override_mode == "full")
        force = manual_force
        has_state = bool(entry.get("manifest_head"))
        old_head = entry.get("manifest_head")        # 本次规划前的 head，是 worker 算 diff 的基准
        head_changed = has_state and old_head != head

        # 提示词签名：只要 prompts 下任何一个 md 被编辑过，不论系统提示词还是任务提示词，
        # 这个仓库就得全量重构。debug 模式特意跳过这步，免得 debug 也触发一整轮全跑。sig 只在
        # 这里算一次，下面两处落盘都复用它。
        prompt_changed = False
        sig = None
        if not DEBUG:
            sig = signature(PROMPTS_DIR)
            prompt_changed = entry.get("prompt_hash") is not None and entry.get("prompt_hash") != sig
            if prompt_changed:
                log(f"  检测到提示词变更（{entry.get('prompt_hash')}→{sig}）→ 全量重构")
                force = True
            # 尽早把基线签名落下：任何一个被碰过的仓库，哪怕 workers 建到一半、从没完成，都记上
            # 当前签名，这样之后提示词一改就能被发现。不然那些缺基线的 workers 仓库会漏检，旧讲义
            # 就一直停在旧提示词上。prompt_changed 的时候 prompt_hash 不会是 None，所以不会在这里被
            # 误覆盖；真正的更新发生在下面清空之前那一步。
            if entry.get("prompt_hash") is None:
                with GLOBAL_STATE_LOCK:
                    entry["prompt_hash"] = sig

        # 已完成且 HEAD 未变且非强制 → 无事可做
        if entry.get("phase") == "done" and not head_changed and not force:
            log(f"  已完成且 HEAD 未变（{head[:8]}），跳过")
            with GLOBAL_STATE_LOCK:
                entry["last_run_at"] = generated_at
                save_state(state)
            return "skipped"

        plan_mode = "full" if (force or not has_state) else "incremental"

        # 先把旧讲义和 manifest 注入 clone，full 模式则先清空，然后再据此判断要不要重新规划。
        # 这里有个顺序坑：注入必须排在 need_planner 之前，否则续传时会误以为 manifest 缺失，
        # 结果重复规划一遍。
        if plan_mode == "full":
            if prompt_changed:
                # 先把新签名提交了：哪怕重构中途额度耗尽、退了 75，下次续传时签名也已经对上，
                # 不会又清空一遍，已经生成好的那部分讲义能接着往下走，不至于陷入每次清空重来的死循环。
                with GLOBAL_STATE_LOCK:
                    entry["prompt_hash"] = sig
                    save_state(state)
            clear_clone_tutorial(clone_tutorial)
        else:
            inject_prior(control_tutorial, clone_tutorial)

        need_planner = (force or not has_state or head_changed
                        or not (clone_tutorial / "manifest.json").exists()
                        or entry.get("phase") != "workers")

        if need_planner:
            if _deadzone_blocked():
                log(f"  临近/处于死区，暂缓规划 {repo_key(repo)}（保留进度，下次窗口续传）")
                with GLOBAL_STATE_LOCK:
                    entry["last_run_at"] = generated_at
                    save_state(state)
                return "incomplete"
            phase_log(repo_key(repo), "大纲生成")
            existing = None
            if plan_mode == "incremental" and (clone_tutorial / "manifest.json").exists():
                existing = (clone_tutorial / "manifest.json").read_text(encoding="utf-8")
            planner_cfg = ClaudeConfig.from_env(PLANNER_TEMPLATE, tutorial_dir)
            (clone_tutorial / ".transcripts").mkdir(parents=True, exist_ok=True)
            planner_cfg.transcript_dir = (clone_tutorial / ".transcripts").resolve()
            manifest = call_planner(
                repo, head, plan_mode, work_cwd,
                compose_planner_prompt(repo, plan_mode, head, old_head, existing, tutorial_dir, permalink_base),
                planner_cfg, json.dumps(MANIFEST_SCHEMA),
            )
            normalize_manifest_filenames(manifest)   # filename 规范为裸 basename，防双层路径
            clone_tutorial.mkdir(parents=True, exist_ok=True)
            (clone_tutorial / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            lectures = {}
            for u in manifest.get("units", []):
                for lec in u.get("lectures", []):
                    if (lid := lec.get("id")):
                        action = lec.get("action") or ("new" if plan_mode == "full" else "update")
                        lectures[lid] = {"status": STATUS_KEEP if action == "keep" else STATUS_PENDING,
                                         "action": action, "retries": 0}
            with GLOBAL_STATE_LOCK:
                entry["lectures"] = lectures
                entry["manifest_head"] = head
                entry["prev_head"] = old_head            # 持久化 diff 基准，供续传 worker 使用
                entry["phase"] = "workers"
                entry["mode"] = plan_mode
                save_state(state)
        else:
            log(f"  续传 workers（{repo_key(repo)}）")
            if not (clone_tutorial / "manifest.json").exists():
                with GLOBAL_STATE_LOCK:
                    entry["phase"] = None
                    save_state(state)
                return "incomplete"

        # 重试次数超过上限的讲义标成 abandoned。这是终态，不再为它烧额度，也不让它卡住整个完成
        with GLOBAL_STATE_LOCK:
            for lid, p in list(entry.get("lectures", {}).items()):
                if p.get("status") in (STATUS_PENDING, STATUS_FAILED) and p.get("retries", 0) >= MAX_RETRIES:
                    p["status"] = STATUS_ABANDONED
                    p["error"] = (p.get("error", "") + " | abandoned: max retries").strip(" |")
            save_state(state)

        # workers 只跑 pending 和 failed。abandoned、keep、done 都跳过生成，但仍留在 ordered 里当链式前文的来源
        manifest = json.loads((clone_tutorial / "manifest.json").read_text(encoding="utf-8"))
        lec_specs = {lec["id"]: lec for u in manifest.get("units", [])
                     for lec in u.get("lectures", []) if lec.get("id")}
        # 全部讲义按 manifest 顺序排好。done、keep、abandoned 在循环里会跳过生成，只用来收集前文摘要
        ordered = [lec_specs[lid] for lid in entry.get("lectures", {})
                   if lid in lec_specs]
        if DEBUG:
            # debug 模式只生成一篇：用 RC_DEBUG_LECTURE_ID 指定，没设就取首个单元的第一篇
            target = (os.environ.get("RC_DEBUG_LECTURE_ID") or "").strip()
            spec = lec_specs.get(target) if (target and target in lec_specs) \
                else (next(iter(lec_specs.values()), None) if lec_specs else None)
            if not spec:
                log(f"  DEBUG：未找到目标讲义（RC_DEBUG_LECTURE_ID={target!r}），跳过")
                with GLOBAL_STATE_LOCK:
                    entry["last_error"] = "debug: target lecture not found"
                    save_state(state)
                return "incomplete"
            ordered = [spec]
        worker_cfg = ClaudeConfig.from_env(WORKER_TEMPLATE, tutorial_dir)
        worker_cfg.transcript_dir = clone_tutorial / ".transcripts"
        outline_prompt = PLANNER_TEMPLATE.read_text(encoding="utf-8")
        outline_content = json.dumps(
            {"project": manifest.get("project"), "rationale": manifest.get("rationale", ""),
             "units": manifest.get("units", [])},
            ensure_ascii=False, indent=2)
        todo = [l for l in ordered if _lec_status(entry, l["id"]) in RETRYABLE]
        if todo:
            phase_log(repo_key(repo), "讲义生成", f"(0/{len(todo)})")
            # worker 算 diff 用的是持久化下来的 prev_head，续传时它仍指向旧的 head，这就是修复 #2
            res = run_lectures_sequential(repo, work_cwd, ordered, state, entry, head,
                                          entry.get("prev_head"), tutorial_dir, permalink_base,
                                          worker_cfg, generated_at,
                                          outline_prompt=outline_prompt,
                                          outline_content=outline_content)
            harvest(clone_tutorial, control_tutorial)
            with GLOBAL_STATE_LOCK:
                _record_harvest(entry, control_tutorial, head, generated_at)
            if res["quota"]:
                with GLOBAL_STATE_LOCK:
                    entry["phase"] = "workers"
                    entry["last_error"] = "quota exhausted; will resume next window"
                    save_state(state)
                log(f"  额度耗尽，已收割进度 → {entry['tutorial_path']}")
                return "incomplete"
            if res["deferred"]:
                with GLOBAL_STATE_LOCK:
                    entry["phase"] = "workers"
                    entry["last_error"] = "near dead zone; will resume next window"
                    save_state(state)
                log(f"  临近死区，暂停；剩余讲义下次续传 → {entry['tutorial_path']}")
                return "incomplete"
            if DEBUG:
                # debug 模式下，这一篇生成了就算本轮完成，返回 done 让 main 退出 0。
                # 但刻意不把 phase 写成 done：一旦在 RC_DEBUG_PERSIST=1 时污染了正式 state，
                # 下次正式运行会被那处「已完成就跳过」的判断短路，剩下的讲义就永远不生成了。
                # 所以 phase 保持 workers。
                with GLOBAL_STATE_LOCK:
                    entry["last_error"] = None
                    entry["last_run_at"] = generated_at
                    save_state(state)
                log(f"  DEBUG 完成：已生成 {ordered[0].get('id')} → {entry['tutorial_path']}")
                return "done"
        else:
            harvest(clone_tutorial, control_tutorial)
            with GLOBAL_STATE_LOCK:
                _record_harvest(entry, control_tutorial, head, generated_at)

        remaining = [lid for lid, p in entry.get("lectures", {}).items()
                     if p.get("status") in RETRYABLE]
        # abandoned 是永久失败、retries 已经到上限的讲义，不计进 remaining，不会卡住 done。
        # 但得把它们记下来，发布时据此告警；不然一个带着缺失讲义的「done」仓库会悄无声息地
        # 把残缺教程发到 main 上。
        abandoned = [lid for lid, p in entry.get("lectures", {}).items()
                     if p.get("status") == STATUS_ABANDONED]
        if not remaining:
            with GLOBAL_STATE_LOCK:
                entry["abandoned"] = abandoned   # 可空列表
                if plan_mode == "full":
                    entry["version"] = int(entry.get("version", 0)) + 1
                entry["phase"] = "done"
                entry["last_error"] = None
                if not DEBUG and sig is not None:
                    entry["prompt_hash"] = sig   # 首次构建/每次完成都记当前签名，供后续变更检测
                save_state(state)
            if abandoned:
                log(f"  ⚠ 全部完成，但有 {len(abandoned)} 篇讲义 abandoned（永久失败、已跳过，缺篇发布）: {abandoned[:5]}")
            log(f"  全部完成 → {entry['tutorial_path']}（v{entry.get('version', 0)}）")
            return "done"
        with GLOBAL_STATE_LOCK:
            entry["abandoned"] = abandoned
            entry["phase"] = "workers"
            entry["last_error"] = f"{len(remaining)} 篇未完成: {remaining[:5]}"
            save_state(state)
        log(f"  仍有 {len(remaining)} 篇未完成：{remaining[:5]}")
        return "incomplete"
    except QuotaExhaustedError as e:
        # 退避已耗尽，仅暂停当前仓库；不设全局停牌
        log(f"  额度耗尽（{repo_key(repo)}）：{str(e)[:200]}；保存进度")
        with GLOBAL_STATE_LOCK:
            entry["last_error"] = f"quota: {str(e)[:300]}"
            entry["last_run_at"] = generated_at
            save_state(state)
        return "incomplete"
    except Exception as e:  # noqa: BLE001
        log(f"  ERROR {repo_key(repo)}: {type(e).__name__}: {e}")
        with GLOBAL_STATE_LOCK:
            entry["last_error"] = f"{type(e).__name__}: {str(e)[:300]}"
            entry["last_run_at"] = generated_at
            save_state(state)
        return "incomplete"


# 模块级变量，在 main 里赋值，process_repo 读它
override_mode = "auto"


def _safe_process_repo(repo: Repo, state: dict, generated_at: str,
                       run_start: float, deadline_min: float) -> str:
    """process_repo 的一层包装：开跑前先看全局停牌和软截止，能早退就早退；额度耗尽仅暂停当前仓库。"""
    if GLOBAL_STOP.is_set():
        return "incomplete"
    if _deadline_reached(run_start, deadline_min):
        GLOBAL_STOP.set()
        return "incomplete"
    try:
        return process_repo(repo, state, generated_at)
    except QuotaExhaustedError:
        # 退避已耗尽，仅暂停当前仓库；不设全局停牌
        entry = get_entry(state, repo_key(repo))
        with GLOBAL_STATE_LOCK:
            entry["last_error"] = "quota: exhausted after retries"
            entry["last_run_at"] = generated_at
            save_state(state)
        return "incomplete"


def main() -> int:
    global override_mode
    GLOBAL_STOP.clear()          # 每次进 main 都复位一下，selftest 反复调用也安全
    generated_at = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    override_mode = (os.environ.get("RC_MODE") or "auto").strip().lower()
    if override_mode in ("clone", "major", "rebuild"):
        override_mode = "full"

    if in_dead_zone() and not env_truthy("RC_ALLOW_DEADZONE") and not DEBUG:
        log(f"处于死区 UTC {DEAD_START:g}-{DEAD_END:g}（北京 14-18），本次跳过")
        return 0

    if DEBUG:
        log("=== DEBUG 模式：固定单仓库 + 只生成 1 篇讲义 + 忽略死区"
            + ("（进度写入 .debug/ 沙箱）" if _DEBUG_SANDBOX else "（沿用正式进度）") + " ===")

    repos = load_repos()
    if not repos:
        log("没有可处理的项目，退出")
        return 0
    if DEBUG and len(repos) > 1:
        log(f"DEBUG：固定仓库 {repos[0].name}（忽略其余 {len(repos) - 1} 个）")
        repos = [repos[0]]

    state = load_state()
    any_incomplete = False
    had_error = False

    # 两遍调度，只在 auto 增量模式下启用：第一遍先把没完成的仓库跑完；只有第一遍全部 done
    # 了，才进第二遍，复查那些已完成的仓库有没有上游或提示词的更新。DEBUG、RC_FORCE_FULL
    # 或者 RC_MODE=full 这几种情况都只跑单遍。
    two_pass = not (DEBUG or env_truthy("RC_FORCE_FULL") or override_mode == "full")

    def is_done(name: str) -> bool:
        return get_entry(state, name).get("phase") == "done"

    phase1 = list(repos)
    phase2: list[Repo] = []
    if two_pass:
        phase1 = [r for r in repos if not is_done(repo_key(r))]
        phase2 = [r for r in repos if is_done(repo_key(r))]

    # 软截止，由 RC_RUN_DEADLINE_MIN 控制
    run_start = time.monotonic()
    deadline_min = float(os.environ.get("RC_RUN_DEADLINE_MIN", "0") or "0")

    def run_phase(batch: list[Repo], label: str) -> None:
        """动态拉取调度：成功一个仓库才从队列拉下一个，失败/额度暂停不补位。

        启动首批 min(CONCURRENCY, len(batch)) 个仓库，用 wait(FIRST_COMPLETED) 逐结果收
        集。成功（done/skipped）→ 从 pending 拉一个补位；失败/额度暂停（incomplete）→ 不补。
        死区/软截止触发 GLOBAL_STOP 时仍然 break 整批。自然耗尽（pending 空 + 在飞全部结束）
        时循环退出，剩余仓库留给下个窗口。
        """
        nonlocal any_incomplete, had_error
        if not batch:
            return
        if GLOBAL_STOP.is_set():
            any_incomplete = True
            return
        log(f"=== {label}（{len(batch)} 个仓库，并行上限 {CONCURRENCY}）===")
        pending = deque(batch)
        futs: dict = {}  # Future → Repo

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            # 启动首批
            for _ in range(min(CONCURRENCY, len(pending))):
                r = pending.popleft()
                futs[ex.submit(_safe_process_repo, r, state, generated_at,
                               run_start, deadline_min)] = r

            while futs:
                done, _ = wait(futs, return_when=FIRST_COMPLETED)
                for f in done:
                    repo = futs.pop(f)
                    try:
                        outcome = f.result()
                    except Exception as e:
                        # 未预期的错误，_safe_process_repo 安全网没兜住的
                        had_error = True
                        log(f"  ERROR [{repo_key(repo)}] uncaught: {e}")
                        # 未预期异常不拉新
                        continue

                    if outcome == "incomplete":
                        any_incomplete = True
                        # 失败/额度暂停 → 不拉新
                    else:
                        # 成功（done / skipped）→ 拉下一个
                        if pending:
                            next_r = pending.popleft()
                            futs[ex.submit(_safe_process_repo, next_r, state,
                                           generated_at, run_start, deadline_min)] = next_r

                    log(f"  [{repo_key(repo)}] → {outcome}")

                    if GLOBAL_STOP.is_set():
                        any_incomplete = True
                        break
                if GLOBAL_STOP.is_set():
                    # 仍在飞的 futures 会在 _safe_process_repo 入口早退
                    break

    run_phase(phase1, "Phase 1：未完成仓库")

    # Phase 2 的门槛：只有第一遍全部 done、没人碰过 GLOBAL_STOP、并且两遍条件仍然成立，才进。
    # 注意 not any_incomplete 其实已经蕴含了第一遍全部 done，因为 done 和 skipped 都要求 phase 是 done。
    if two_pass and phase2 and not any_incomplete and not GLOBAL_STOP.is_set():
        run_phase(phase2, "Phase 2：复查已完成仓库更新")

    with GLOBAL_STATE_LOCK:
        save_state(state)
    if had_error:
        return 1
    if any_incomplete:
        log("=== 本轮存在未完成项（进度已保存，下次窗口自动续传）===")
        return 75
    log("=== 全部完成 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
