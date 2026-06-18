#!/usr/bin/env python3
"""提示词渲染与签名（任务提示词解耦用）。

把任务提示词（每次调用 claude 的 stdin 包装）从 analyze.py 的内联字符串里搬出来，
变成 `prompts/*.task.md` 的 Jinja2 模板；本模块负责渲染 + 计算「提示词签名」。

- `render(prompts_dir, name, **vars)`：按 Jinja2 渲染模板。`StrictUndefined` 让缺变量直接报错。
- `signature(prompts_dir)`：对 `prompts/*.md`（系统+任务，按名排序）的内容取 sha256（前 16 hex），
  仅依赖内容不依赖 mtime。analyze.py 据此判断「提示词是否变更 → 是否需要全量重构」。

本模块不读环境变量、不持有状态，便于测试。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import jinja2


def _env(prompts_dir: Path) -> jinja2.Environment:
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
    """渲染 prompts_dir 下的模板 name（如 'worker.task.md'）。缺变量直接抛错。"""
    return _env(prompts_dir).get_template(name).render(**kwargs)


def signature(prompts_dir: Path) -> str:
    """对 prompts_dir 下所有 *.md 的内容取 sha256（前 16 hex）。

    覆盖系统提示词（*.prompt.md）与任务提示词（*.task.md）；任一被编辑 → 签名变化。
    """
    h = hashlib.sha256()
    files = sorted(p.name for p in prompts_dir.glob("*.md"))
    for fn in files:
        h.update(fn.encode("utf-8"))
        h.update(b"\0")
        # normalize 换行为 LF，避免 Windows autocrlf=true 下本地 checkout
        # 产生 CRLF 而 CI(Linux) 是 LF，导致 signature 不一致 → 误触发全量重构。
        content = (prompts_dir / fn).read_bytes().replace(b"\r\n", b"\n")
        h.update(content)
        h.update(b"\0")
    return h.hexdigest()[:16]
