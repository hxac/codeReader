"""provider 层：与 Agent CLI 交互的唯一入口。

- 错误分类：QuotaExhaustedError（额度/限流 → 保存进度并停止整批）与
  ClaudeRunnerError（普通失败 → 仅停止当前篇目）。
- 429 退避：指数退避重试，软截止到达后放弃。
- MOCK=1 时以桩替代真实调用（selftest 离线运行，不消耗额度）。
- 支持多种 Agent CLI（见 agents.py）：默认 claude（全参数）；gemini/codex
  best-effort；custom 由环境变量驱动。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import (
    config as cfg,
    prompts,
    runtime,
    scheduler,
)
from .agents import build_argv, extract, resolve_agent
from .state import lec_filename

# 只读工具集，planner 与 worker 共用；worker 额外获得仅限讲义目录的 Write/Edit。
READONLY_TOOLS = [
    "Read", "Grep", "Glob",
    "Bash(git log *)", "Bash(git diff *)", "Bash(git show *)",
    "Bash(git ls-files *)", "Bash(git rev-parse *)", "Bash(git remote *)",
]


def _worker_tools(tutorial_dir: str) -> list[str]:
    """worker 可用的工具：只读工具集，加上仅允许写入讲义目录的 Write 和 Edit。

    将写权限严格限定在讲义目录内，防止 worker 修改被分析项目的源码。
    """
    return READONLY_TOOLS + [f"Write({tutorial_dir}/**)", f"Edit({tutorial_dir}/**)"]


# ---- 错误分类 -------------------------------------------------------------- #
class QuotaExhaustedError(RuntimeError):
    """API 额度或限流已耗尽。调用方收到后应保存进度并停止整批任务。"""


class ClaudeRunnerError(RuntimeError):
    """普通失败：单篇预算超限、非零退出、JSON 解析失败等均归此类，仅影响当前篇目。"""


class ClaudeTimeoutError(ClaudeRunnerError):
    """子进程执行超时。将当前篇目判为失败并继续下一篇，但不停掉整轮。"""


# 单篇轮次超限的特征。命中时视为普通失败，仅停止当前篇目。注意该判断必须先于
# 额度判断执行，否则 "max turns exceeded" 中的 exceeded 会被额度正则误匹配，
# 导致整批停止。
_MAXTURNS_RE = re.compile(r"max(?:imum)?[\s_-]*turns?|turns?[\s_-]*(?:limit|exceed|reach)|reached[\s\w]{0,20}turn", re.IGNORECASE)
# 单篇预算超限的特征，命中时同样视为普通失败、仅停止当前篇目。
# 该正则故意不包含 cost 与 usage 两个裸词：第三方端点的额度报文常含
# "cost limit"/"usage limit"，若纳入将先被额度正则匹配而误判为整批停止。
# 此处仅识别明确属于单篇预算的表述。
_BUDGET_RE = re.compile(r"max.?budget|budget[\s_-]*(?:limit|exceed|reach)|spend(?:ing)?[\s_-]*(?:limit|exceed)|exceeded[\s\w]{0,15}(?:budget|spend)", re.IGNORECASE)
# 额度、限流、计费类错误的特征，命中即抛 QuotaExhaustedError、整批停止。
# 特意纳入 cost limit 与 usage limit：不少第三方端点以此表示账户额度耗尽。
_QUOTA_RE = re.compile(r"rate.?limit|429|overloaded|credit|quota|billing|insufficient|capacity|try again later|cost[\s_-]*limit|usage[\s_-]*limit|exceeded[\s\w]{0,15}(?:credit|quota|limit)", re.IGNORECASE)


def _classify_error(text: str) -> type | None:
    """根据报错文本判断失败类别；均未匹配时返回 None（调用方按普通失败处理）。

    匹配顺序有讲究：必须先匹配 max-turns 和 budget，再匹配 quota——"max turns
    exceeded" 同样包含 exceeded，若先匹配 quota 正则，单篇轮次超限会被误判为
    额度耗尽而停止整批。
    """
    if _MAXTURNS_RE.search(text) or _BUDGET_RE.search(text):
        return ClaudeRunnerError
    if _QUOTA_RE.search(text):
        return QuotaExhaustedError
    return None


# ---- 调用配置 -------------------------------------------------------------- #
@dataclass
class AgentConfig:
    model: str
    max_turns: str
    max_budget_usd: str
    timeout: int
    template_path: Path | None = None    # 系统提示词文件；None 表示不注入
    tutorial_dir: str = ""               # worker 写盘范围（用于工具白名单）
    transcript_dir: Path | None = None   # transcript jsonl 存放目录；None 表示不保存

    @classmethod
    def from_env(cls, template_path: Path, tutorial_dir: str,
                 max_turns: str | None = None, max_budget: str | None = None) -> AgentConfig:
        """从环境变量构造配置。max_turns/max_budget 可显式传入以覆盖默认值。"""
        return cls(
            model=os.environ.get("RC_MODEL", cfg.DEFAULT_MODEL),
            max_turns=max_turns or os.environ.get("RC_MAX_TURNS", cfg.DEFAULT_WORKER_MAX_TURNS),
            max_budget_usd=max_budget or os.environ.get("RC_MAX_BUDGET_USD", cfg.DEFAULT_WORKER_BUDGET),
            timeout=int(os.environ.get("RC_TIMEOUT", str(cfg.DEFAULT_TIMEOUT))),
            template_path=template_path,
            tutorial_dir=tutorial_dir,
        )


@dataclass
class RunnerResult:
    summary: str
    cost_usd: float
    session_id: str | None
    raw: dict
    structured: dict | None = None   # --json-schema 的结构化结果（planner 使用）


# ---- 子进程调用 ------------------------------------------------------------ #
def _harvest_transcripts(src_config_dir: Path, dst_dir: Path) -> None:
    """将一次 agent 调用产生的 transcript jsonl 增量拷贝到 dst_dir（同名文件跳过）。

    中断后 workflow 会将 transcript 单独提交到 transcripts 分支，便于排查。
    """
    projects_dir = src_config_dir / "projects"
    if not projects_dir.is_dir():
        return
    for jsonl in sorted(projects_dir.rglob("*.jsonl")):
        if not jsonl.is_file():
            continue
        # 保留 session 子目录的路径结构，避免不同会话的同名文件相互覆盖。
        rel = jsonl.relative_to(projects_dir)
        dst = dst_dir / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(jsonl, dst)


def _quota_backoff_should_bail(run_start: float, deadline_min: float,
                               global_stop_set: bool) -> bool:
    """判断 429 退避循环是否应放弃本次重试并让进程干净退出。

    条件为软截止已到或全局停止已触发；为 True 时调用方应立即抛出
    QuotaExhaustedError。deadline_min=0 表示无软截止，恒返回 False。
    """
    if deadline_min > 0 and scheduler.deadline_reached(run_start, deadline_min):
        return True
    return global_stop_set


def _invoke(cmd: list[str], repo_dir: Path, prompt_text: str, timeout: int,
            transcript_dir: Path | None = None) -> dict:
    """执行一次 agent 子进程调用，并解析 stdout 中的 JSON 返回。

    失败时不直接报错，而是按报错文本分类抛出对应异常。若传入 transcript_dir，
    则为本次调用创建独立的临时 CLAUDE_CONFIG_DIR，退出后将 jsonl 增量拷贝过去
    （仅 claude 等支持 transcript 的 agent 有效）。

    额度/限速类错误（QuotaExhaustedError）在本函数内指数退避重试（最多
    cfg.QUOTA_RETRY_MAX 次，base 1s，cap 60s），重试耗尽后向上抛出；
    其他错误立即抛出。
    """
    # Windows 上 npm 全局安装的 CLI 实为 .cmd 包装脚本，CreateProcess 无法按裸名
    # 直接找到。当裸名在 PATH 中找不到时补 .cmd 后缀；保持 shell=False，argv 引号
    # 由 list2cmdline 正确处理，带 JSON 的参数（如 --json-schema）不会被 cmd.exe
    # 拆分破坏。
    if sys.platform == "win32" and "." not in cmd[0] and shutil.which(cmd[0]) is None:
        cmd = [cmd[0] + ".cmd", *cmd[1:]]

    last_quota_err: QuotaExhaustedError | None = None
    for retry in range(cfg.QUOTA_RETRY_MAX):
        env = None
        tmpdir = None
        if transcript_dir is not None:
            tmpdir = Path(tempfile.mkdtemp(prefix="agent-config-"))
            env = {**os.environ, "CLAUDE_CONFIG_DIR": str(tmpdir)}
        try:
            proc = subprocess.run(
                cmd, cwd=str(repo_dir), input=prompt_text,
                capture_output=True, text=True, encoding="utf-8",
                timeout=timeout, env=env, check=False,
            )
        except FileNotFoundError as e:
            raise ClaudeRunnerError(
                f"`{cmd[0]}` 未在 PATH 中找到；确认已安装对应 CLI"
                f"（claude: npm install -g @anthropic-ai/claude-code）"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise ClaudeTimeoutError(f"agent 超时（{timeout}s）") from e
        finally:
            if tmpdir is not None and transcript_dir is not None:
                _harvest_transcripts(tmpdir, transcript_dir)
                shutil.rmtree(tmpdir, ignore_errors=True)

        if proc.returncode != 0:
            text = (proc.stderr or "") + "\n" + (proc.stdout or "")
            tail = text[-2000:]
            err_type = _classify_error(text)
            if err_type is QuotaExhaustedError:
                last_quota_err = QuotaExhaustedError(f"API 额度/限流耗尽：\n{tail}")
                if retry < cfg.QUOTA_RETRY_MAX - 1:
                    if _quota_backoff_should_bail(runtime.RUN_START_MONO, runtime.RUN_DEADLINE_MIN,
                                                  runtime.GLOBAL_STOP.is_set()):
                        cfg.log(f"  软截止已到，放弃退避重试（{str(last_quota_err)[:120]}）")
                        raise last_quota_err
                    delay = min(cfg.QUOTA_RETRY_BASE * (2 ** retry), cfg.QUOTA_RETRY_CAP)
                    cfg.log(f"  限速/额度退避 {delay:.0f}s（第 {retry + 1}/{cfg.QUOTA_RETRY_MAX} 次）"
                            f" — {str(last_quota_err)[:120]}")
                    time.sleep(delay)
                    continue
                raise last_quota_err
            if err_type is ClaudeRunnerError:
                raise ClaudeRunnerError(f"agent 非零退出（分类匹配）：\n{tail}")
            raise ClaudeRunnerError(f"agent 退出码 {proc.returncode}：\n{tail}")

        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ClaudeRunnerError(
                f"agent 输出不是 JSON：{e}\n--- stdout 末尾 ---\n{proc.stdout[-2000:]}"
            ) from e

    # 理论不可达（循环末尾必然 raise），保留以满足类型检查
    assert last_quota_err is not None
    raise last_quota_err


def run_agent(repo_dir: Path, prompt_text: str, agent_cfg: AgentConfig, *,
              tools: list[str], system_prompt_file: Path | None = None,
              schema: str | None = None,
              max_turns: str | None = None, max_budget: str | None = None) -> RunnerResult:
    """执行一次 agent 子进程调用。planner/worker/summarize 均经由本函数，差异仅在于参数。

    对于不支持 --append-system-prompt-file 的 agent，将系统提示词拼接进 stdin。
    max_turns/max_budget 缺省取 agent_cfg 中的 worker 上限；planner/summarize
    显式传入各自的值。
    """
    agent = resolve_agent()
    # 不支持 --append-system-prompt-file 的 agent：将系统提示词拼接进 stdin。
    if not agent.system_flags and system_prompt_file is not None:
        prompt_text = (f"# 系统提示词\n\n{system_prompt_file.read_text(encoding='utf-8')}\n\n---\n\n"
                       + prompt_text)
        system_prompt_file = None
    cmd = build_argv(agent, model=agent_cfg.model,
                     max_turns=max_turns or agent_cfg.max_turns,
                     max_budget=max_budget or agent_cfg.max_budget_usd,
                     tools=tools, system_prompt_file=system_prompt_file, schema=schema)
    payload = _invoke(cmd, repo_dir, prompt_text, agent_cfg.timeout,
                      transcript_dir=agent_cfg.transcript_dir if agent.transcripts else None)
    result, cost, session, structured = extract(agent, payload)
    return RunnerResult(summary=result, cost_usd=cost, session_id=session, raw=payload,
                        structured=structured if isinstance(structured, dict) else None)


# ---- MOCK 层：RC_MOCK=1 时以桩替代真实调用，selftest 依赖其离线运行 ------------------ #
MOCK_PLANNER_CALLS = 0    # selftest 使用：统计 mock planner 调用次数
MOCK_WORKER_CALLS = 0     # selftest 使用：统计 mock worker 调用次数（验证超时不当场重试）
MOCK_SUMMARIZE_CALLS = 0  # selftest 使用：统计 mock summarize 调用次数


def _mock_planner(repo, head: str, plan_mode: str) -> dict:
    """planner 的桩实现：不调用 agent，直接生成固定大纲，用于 selftest 验证流程。"""
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


def call_planner(repo, head: str, plan_mode: str, repo_dir: Path,
                 prompt: str, agent_cfg: AgentConfig, schema_json: str) -> dict:
    """让 agent 通读项目并输出大纲 manifest（dict）。claude 通过 --json-schema 固定输出结构。

    planner 的轮次与预算使用专用环境变量 RC_PLANNER_MAX_TURNS / RC_PLANNER_MAX_BUDGET_USD
    （默认 30 轮、3 美元），有意不使用 agent_cfg 中的 worker 上限——planner 为只读的
    一次性调用，其上限应远小于 worker（50 轮、10 美元），否则容易快速耗尽额度。
    """
    if cfg.MOCK:
        return _mock_planner(repo, head, plan_mode)
    r = run_agent(repo_dir, prompt, agent_cfg, tools=READONLY_TOOLS,
                  system_prompt_file=agent_cfg.template_path, schema=schema_json,
                  max_turns=os.environ.get("RC_PLANNER_MAX_TURNS", cfg.DEFAULT_PLANNER_MAX_TURNS),
                  max_budget=os.environ.get("RC_PLANNER_MAX_BUDGET_USD", cfg.DEFAULT_PLANNER_BUDGET))
    # --json-schema 的结构化结果位于 structured_output；不支持的 agent 可能将
    # manifest 放在 result 的 JSON 文本中。两种情况均需兼容。
    manifest = r.structured
    if manifest is None and r.summary:
        try:
            manifest = json.loads(r.summary)
        except json.JSONDecodeError as e:
            raise ClaudeRunnerError(f"planner result 非 JSON：{e}") from e
    if not isinstance(manifest, dict) or "units" not in manifest:
        raise ClaudeRunnerError(f"planner 未返回合法 manifest：{json.dumps(manifest)[:500]}")
    return manifest


def call_worker(repo, repo_dir: Path, lec: dict, prompt: str,
                agent_cfg: AgentConfig) -> RunnerResult:
    """生成或更新一篇讲义 md，文件由 agent 自行写入。返回总结文本与本次花费。"""
    if cfg.MOCK:
        global MOCK_WORKER_CALLS
        MOCK_WORKER_CALLS += 1
        _fail = (os.environ.get("RC_MOCK_TIMEOUT_ON") or "").strip()
        if _fail and lec.get("id") == _fail:
            raise ClaudeTimeoutError(f"mock timeout（{lec.get('id')}）")
        _quota = (os.environ.get("RC_MOCK_QUOTA_ON") or "").strip()
        if _quota and lec.get("id") == _quota:
            raise QuotaExhaustedError(f"mock quota（{lec.get('id')}）")
        d = repo_dir / agent_cfg.tutorial_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / lec_filename(lec)).write_text(
            f"# {lec.get('title', '')}\n\n> mock 讲义 `{lec.get('id')}`\n\n"
            f"主题: {lec.get('topic', '')}\n\n\\[E = mc^2\\]\n", encoding="utf-8")
        return RunnerResult(summary=f"mock wrote {lec_filename(lec)}", cost_usd=0.01,
                            session_id=None, raw={})
    return run_agent(repo_dir, prompt, agent_cfg, tools=_worker_tools(agent_cfg.tutorial_dir),
                     system_prompt_file=agent_cfg.template_path)


def call_summarize(repo, repo_dir: Path, lec: dict, tutorial_dir: str,
                   transcript_dir: Path | None = None) -> str:
    """读取一篇已生成的讲义 md，产出一段简短的中文摘要。仅使用只读工具，不带系统提示词。"""
    if cfg.MOCK:
        global MOCK_SUMMARIZE_CALLS
        MOCK_SUMMARIZE_CALLS += 1
        return f"（mock 摘要：{lec.get('id')} {lec.get('title')}）"
    summary_cfg = AgentConfig(
        model=os.environ.get("RC_MODEL", cfg.DEFAULT_MODEL),
        max_turns=os.environ.get("RC_SUMMARY_MAX_TURNS", cfg.DEFAULT_SUMMARY_MAX_TURNS),
        max_budget_usd=os.environ.get("RC_SUMMARY_MAX_BUDGET_USD", cfg.DEFAULT_SUMMARY_BUDGET),
        timeout=int(os.environ.get("RC_TIMEOUT", str(cfg.DEFAULT_TIMEOUT))),
        tutorial_dir=tutorial_dir,
        transcript_dir=transcript_dir,
    )
    r = run_agent(repo_dir, prompts.compose_summary_prompt(lec, tutorial_dir),
                  summary_cfg, tools=READONLY_TOOLS)
    return r.summary
