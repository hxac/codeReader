#!/usr/bin/env python3
"""codeReader 执行层（两阶段）。

封装 `claude -p` headless CLI，提供两个入口：

- `run_planner(...)`：只读工具 + `--json-schema`，让 claude 读项目后输出**大纲 manifest（JSON dict）**。
- `run_worker(...)`：读源码 + **仅限 `<tutorial_dir>/` 的 Write/Edit**，让 claude 生成/更新**一篇讲义 .md**。

这是**唯一**知道 Claude 如何被调用的模块；想换 provider 只改本文件。

错误分类：
- `QuotaExhaustedError`：额度耗尽 / 限流 / 计费类错误（analyze.py 据此**保存进度并停止**，等下次窗口续传）。
- `ClaudeRunnerError`：普通失败（含单篇预算超限）——analyze.py 记为该讲义失败、继续下一篇。

鉴权（ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL）由父进程环境继承。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 读源码用的只读工具（planner 与 worker 共用）。
READONLY_TOOLS = [
    "Read", "Grep", "Glob",
    "Bash(git log *)", "Bash(git diff *)", "Bash(git show *)",
    "Bash(git ls-files *)", "Bash(git rev-parse *)", "Bash(git remote *)",
]

# 额度/限流/计费类错误特征（命中 → QuotaExhaustedError，整体停）。
_QUOTA_RE = re.compile(r"rate.?limit|429|overloaded|credit|quota|billing|insufficient|exceeded|capacity|try again later", re.I)
# 单篇预算超限特征（命中 → 普通失败，只停这一篇）。
_BUDGET_RE = re.compile(r"budget|max.?budget|cost|spend|usage limit", re.I)


@dataclass
class ClaudeConfig:
    model: str
    max_turns: str
    max_budget_usd: str
    timeout: int
    template_path: Path
    tutorial_dir: str          # 相对 cwd 的讲义目录名（worker 写权限范围）

    @classmethod
    def from_env(cls, template_path: Path, tutorial_dir: str,
                 max_turns: str | None = None, max_budget: str | None = None) -> "ClaudeConfig":
        return cls(
            model=os.environ.get("RC_MODEL", "claude-sonnet-4-6"),
            max_turns=max_turns or os.environ.get("RC_MAX_TURNS", "50"),
            max_budget_usd=max_budget or os.environ.get("RC_MAX_BUDGET_USD", "10.00"),
            timeout=int(os.environ.get("RC_TIMEOUT", "3600")),
            template_path=template_path,
            tutorial_dir=tutorial_dir,
        )


@dataclass
class WorkerResult:
    summary: str
    cost_usd: float
    session_id: Optional[str]
    raw: dict


class QuotaExhaustedError(RuntimeError):
    """API 额度/限流耗尽 —— 调用方应保存进度并停止整批。"""


class ClaudeRunnerError(RuntimeError):
    """普通失败（单篇预算超限、非零退出、JSON 解析失败等）。"""


def _worker_tools(tutorial_dir: str) -> list[str]:
    return READONLY_TOOLS + [f"Write({tutorial_dir}/**)", f"Edit({tutorial_dir}/**)"]


def _invoke(cmd: list[str], repo_dir: Path, prompt_text: str, timeout: int) -> dict:
    """跑一次 claude 子进程，返回解析后的 JSON payload。失败按特征分类抛错。"""
    try:
        proc = subprocess.run(
            cmd, cwd=str(repo_dir), input=prompt_text,
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise ClaudeRunnerError(
            "`claude` CLI 未在 PATH 中找到；确认已 npm install -g @anthropic-ai/claude-code"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise ClaudeRunnerError(f"claude 超时（{timeout}s）") from e

    if proc.returncode != 0:
        text = (proc.stderr or "") + "\n" + (proc.stdout or "")
        tail = text[-2000:]
        if _BUDGET_RE.search(text):                 # 单篇预算/用量上限 → 只停这一篇
            raise ClaudeRunnerError(f"单篇预算/用量超限：\n{tail}")
        if _QUOTA_RE.search(text):                  # 额度/限流/计费 → 整体停
            raise QuotaExhaustedError(f"API 额度/限流耗尽：\n{tail}")
        raise ClaudeRunnerError(f"claude 退出码 {proc.returncode}（可能命中 --max-turns）：\n{tail}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeRunnerError(
            f"claude 输出不是 JSON：{e}\n--- stdout 末尾 ---\n{proc.stdout[-2000:]}"
        ) from e


def run_planner(repo_dir: Path, prompt_text: str, cfg: ClaudeConfig,
                schema_json: str, max_turns: str | None = None,
                max_budget: str | None = None) -> dict:
    """读项目 → 输出大纲 manifest（dict）。用 --json-schema 强制结构。"""
    cmd = [
        "claude", "-p",
        "--model", cfg.model,
        "--output-format", "json",
        "--json-schema", schema_json,
        "--max-turns", max_turns or os.environ.get("RC_PLANNER_MAX_TURNS", "30"),
        "--max-budget-usd", max_budget or os.environ.get("RC_PLANNER_MAX_BUDGET_USD", "3.00"),
        "--allowedTools", ",".join(READONLY_TOOLS),
        "--append-system-prompt-file", str(cfg.template_path),
        "--exclude-dynamic-system-prompt-sections",
        "--no-session-persistence",
    ]
    payload = _invoke(cmd, repo_dir, prompt_text, cfg.timeout)
    # --json-schema 的结果落在 structured_output；老版本可能落在 result（JSON 文本）。两种都兜底。
    manifest = payload.get("structured_output")
    if manifest is None:
        result = (payload.get("result") or "").strip()
        manifest = json.loads(result) if result else None
    if not isinstance(manifest, dict) or "units" not in manifest:
        raise ClaudeRunnerError(f"planner 未返回合法 manifest：{json.dumps(manifest)[:500]}")
    return manifest


def run_worker(repo_dir: Path, prompt_text: str, cfg: ClaudeConfig) -> WorkerResult:
    """生成/更新一篇讲义 .md（claude 自己写文件）。返回总结文本与花费。"""
    cmd = [
        "claude", "-p",
        "--model", cfg.model,
        "--output-format", "json",
        "--max-turns", cfg.max_turns,
        "--max-budget-usd", cfg.max_budget_usd,
        "--allowedTools", ",".join(_worker_tools(cfg.tutorial_dir)),
        "--append-system-prompt-file", str(cfg.template_path),
        "--exclude-dynamic-system-prompt-sections",
        "--no-session-persistence",
    ]
    payload = _invoke(cmd, repo_dir, prompt_text, cfg.timeout)
    return WorkerResult(
        summary=(payload.get("result") or "").strip(),
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        session_id=payload.get("session_id"),
        raw=payload,
    )


if __name__ == "__main__":
    import sys
    print("本模块供 analyze.py 导入；手动冒烟请用 analyze.py 的单仓库流程。", file=sys.stderr)
