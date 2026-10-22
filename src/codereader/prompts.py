"""提示词：Jinja2 渲染、内容签名、三类任务提示词组装。"""
from __future__ import annotations

import hashlib

import jinja2

from . import config as cfg
from .state import lec_filename

# planner 输出的 manifest 按此 JSON Schema 约束。通过 --json-schema 将其提供给 agent，
# 强制结构化输出，避免解析时猜测格式。结构为「单元 → 讲义」树，每篇讲义包含
# topic、源文件、依赖及本次处理方式。
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


def _jinja_env(prompts_dir) -> jinja2.Environment:
    """构建 Jinja2 环境，模板从 prompts 目录加载。

    启用 StrictUndefined：模板引用未传入的变量时直接报错，而非静默渲染为空。
    关闭 autoescape，因为产物是提供给 agent 的纯文本提示词，而非 HTML。
    """
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(prompts_dir)),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
        autoescape=False,
    )


def render(prompts_dir, name: str, **kwargs) -> str:
    """渲染 prompts 目录下的指定模板（如 worker.task.md）。任一变量未传入时直接抛错。"""
    return _jinja_env(prompts_dir).get_template(name).render(**kwargs)


def signature(prompts_dir) -> str:
    """对 prompts 目录下所有 md 文件的内容计算 sha256，取前 16 位十六进制作为签名。

    系统提示词与任务提示词均计入，任一文件被修改签名即变化。编排层据此判断
    是否触发全量重构。
    """
    h = hashlib.sha256()
    for fn in sorted(p.name for p in prompts_dir.glob("*.md")):
        h.update(fn.encode("utf-8"))
        h.update(b"\0")
        # 换行统一为 LF：Windows autocrlf=true 检出为 CRLF 而 CI 为 LF，若不归一化，
        # 签名将不一致并误触发全量重构。
        h.update((prompts_dir / fn).read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def compose_planner_prompt(repo, mode: str, head: str, prev_head: str | None,
                           existing_manifest: str | None, tutorial_dir: str,
                           permalink_base: str) -> str:
    """任务提示词本体为 prompts/planner.task.md 这一 Jinja2 模板，本函数仅负责填充参数。"""
    return render(cfg.PROMPTS_DIR, "planner.task.md",
        repo_name=repo.name, project=repo.project, tutorial_dir=tutorial_dir,
        mode=mode, head=head, permalink_base=permalink_base,
        user_focus=repo.focus or "（无）",
        prev_head=prev_head, existing_manifest=existing_manifest or "{}")


def compose_worker_prompt(repo, lec: dict, action: str, head: str,
                          prev_head: str | None, tutorial_dir: str,
                          permalink_base: str,
                          *, outline_prompt: str = "", outline_content: str = "",
                          prior_summaries: list[dict] | None = None) -> str:
    """任务提示词本体为 prompts/worker.task.md 这一 Jinja2 模板，本函数仅负责填充参数。

    outline_prompt、outline_content、prior_summaries 用于讲义的链式前文，
    使后续讲义能够看到先前的内容与摘要。
    """
    return render(cfg.PROMPTS_DIR, "worker.task.md",
        repo_name=repo.name, project=repo.project, tutorial_dir=tutorial_dir,
        permalink_base=permalink_base, head=head, action=action, prev_head=prev_head,
        lec_id=lec.get("id") or "lecture",
        filename=lec_filename(lec), title=lec.get("title") or "（无标题）",
        topic=lec.get("topic") or "（无主题）",
        level=lec.get("level") or "自动判断",
        learning_goals=lec.get("learning_goals") or [],
        practice_task=lec.get("practice_task") or "由 worker 根据主题和源码自行设计",
        minimal_modules=lec.get("minimal_modules") or [],
        source_files=lec.get("source_files") or [],
        depends_on=lec.get("depends_on") or [],
        outline_prompt=outline_prompt, outline_content=outline_content,
        prior_summaries=prior_summaries or [])


def compose_summary_prompt(lec: dict, tutorial_dir: str) -> str:
    """渲染摘要任务提示词，模板为 prompts/summary.task.md，同样基于 Jinja2。"""
    return render(cfg.PROMPTS_DIR, "summary.task.md",
        lec_id=lec.get("id") or "lecture",
        title=lec.get("title") or "（无标题）",
        filename=lec_filename(lec), tutorial_dir=tutorial_dir)
