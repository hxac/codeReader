"""Agent 抽象层：各 CLI 的参数拼接与输出解析规则集中于此。

默认 claude（Claude Code CLI），所有参数均经过实测。gemini / codex 为尽力支持
（best-effort）：仅使用各自 CLI 最稳定的参数（print 模式、模型、JSON 输出），
turns/budget/tools/schema 等可选限制不传——缺失不影响运行，仅少一层约束；
系统提示词直接拼接进 stdin。RC_AGENT=custom（或任何未收录的名称）时完全由
环境变量驱动，可接入任意新 CLI。

密钥不进入 argv，全部通过环境变量继承（ANTHROPIC_* 或各 CLI 自身的鉴权变量）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from . import config as cfg


@dataclass(frozen=True)
class AgentSpec:
    key: str
    binary: str
    base_flags: tuple[str, ...] = ()            # 固定参数，如 claude 的 -p
    model_flags: tuple[str, ...] = ("--model",)
    json_flags: tuple[str, ...] = ()            # 请求 JSON 输出的参数
    turns_flags: tuple[str, ...] = ()           # 轮次上限参数；空表示 CLI 不支持
    budget_flags: tuple[str, ...] = ()          # 预算上限参数；空表示 CLI 不支持
    tools_flags: tuple[str, ...] = ()           # 工具白名单参数；空表示交由 CLI 自身策略
    system_flags: tuple[str, ...] = ()          # 系统提示词文件参数；空表示内联拼接进 stdin
    system_suffix: tuple[str, ...] = ()         # 系统提示词参数之后追加的尾部参数（claude 专用）
    schema_flags: tuple[str, ...] = ()          # JSON Schema 参数；空表示不做结构化约束
    result_keys: tuple[str, ...] = ("result", "text", "content")
    structured_key: str = "structured_output"
    cost_keys: tuple[str, ...] = ("total_cost_usd", "cost")
    session_key: str = "session_id"
    transcripts: bool = False                   # 是否支持 CLAUDE_CONFIG_DIR 式 transcript 隔离


CLAUDE = AgentSpec(
    key="claude", binary="claude",
    base_flags=("-p",),
    model_flags=("--model",),
    json_flags=("--output-format", "json"),
    turns_flags=("--max-turns",),
    budget_flags=("--max-budget-usd",),
    tools_flags=("--allowedTools",),
    system_flags=("--append-system-prompt-file",),
    system_suffix=("--exclude-dynamic-system-prompt-sections",),
    schema_flags=("--json-schema",),
    result_keys=("result",),
    structured_key="structured_output",
    cost_keys=("total_cost_usd",),
    session_key="session_id",
    transcripts=True,
)

GEMINI = AgentSpec(
    key="gemini", binary="gemini",
    base_flags=("-p",),
    model_flags=("--model",),
    json_flags=("--json",),
    result_keys=("text", "result"),
    cost_keys=("cost",),
)

CODEX = AgentSpec(
    key="codex", binary="codex",
    base_flags=("exec",),
    model_flags=("--model",),
    json_flags=("--json",),
    result_keys=("result", "text", "content"),
    cost_keys=("cost",),
)

CUSTOM = AgentSpec(key="custom", binary="")

AGENT_SPECS = {"claude": CLAUDE, "gemini": GEMINI, "codex": CODEX, "custom": CUSTOM}


def resolve_agent() -> AgentSpec:
    """根据 RC_AGENT 选择 spec；RC_AGENT_BINARY 可覆盖可执行文件名。未收录的名称回退为 custom。"""
    key = (os.environ.get("RC_AGENT") or "claude").strip().lower()
    spec = AGENT_SPECS.get(key)
    if spec is None:
        cfg.log(f"WARN 未知 RC_AGENT={key!r}，按 custom 处理（需配 RC_AGENT_BINARY）")
        spec = CUSTOM
    binary = (os.environ.get("RC_AGENT_BINARY") or "").strip() or spec.binary
    if not binary:
        raise SystemExit("RC_AGENT=custom 需要 RC_AGENT_BINARY 指定可执行文件（或用内置 agent 名）")
    return replace(spec, binary=binary)


def build_argv(spec: AgentSpec, *, model: str, max_turns: str | None = None,
               max_budget: str | None = None, tools: list[str] | None = None,
               system_prompt_file: Path | None = None,
               schema: str | None = None) -> list[str]:
    """拼接一次 agent 调用的命令行。鉴权密钥不进入 argv，通过环境变量继承。"""
    argv = [spec.binary, *spec.base_flags]

    def add(flags: tuple[str, ...], value: str | None) -> None:
        if value is not None:
            argv.extend([*flags, value])

    add(spec.model_flags, model)
    if spec.json_flags:
        argv.extend(spec.json_flags)
    add(spec.turns_flags, max_turns)
    add(spec.budget_flags, max_budget)
    if tools:
        add(spec.tools_flags, ",".join(tools))
    if system_prompt_file is not None and spec.system_flags:
        argv.extend([*spec.system_flags, str(system_prompt_file), *spec.system_suffix])
    if schema is not None and spec.schema_flags:
        argv.extend([*spec.schema_flags, schema])
    # RC_AGENT_EXTRA_ARGS：追加任意 CLI 参数（JSON 字符串数组；非 JSON 时按空白分隔）。
    extra = os.environ.get("RC_AGENT_EXTRA_ARGS")
    if extra:
        try:
            argv.extend(json.loads(extra))
        except json.JSONDecodeError:
            argv.extend(extra.split())
    return argv


def extract(spec: AgentSpec, payload: dict) -> tuple[str, float, str | None, object]:
    """从 JSON payload 中提取 (结果文本, 花费, session_id, 结构化输出)。"""
    result = next((str(payload[k]) for k in spec.result_keys if payload.get(k)), "").strip()
    cost = next((float(payload[k]) for k in spec.cost_keys
                 if isinstance(payload.get(k), (int, float))), 0.0)
    session = payload.get(spec.session_key)
    structured = payload.get(spec.structured_key)
    return result, cost, session, structured
