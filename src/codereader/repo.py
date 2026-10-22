"""repos.yml：待分析项目清单的读取与展开（folders 子目录拆分）。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import yaml

from . import config as cfg

# name / 子目录段允许的字符。GitHub 的 owner/repo 只允许这些，目录同理（放宽到 @+ 以兼容
# 少量真实目录名）。这个白名单能把「全角引号、多余的单引号、空格」这类手误挡在队列之外——
# 它们会被当成合法条目塞进队列，然后每轮都 clone 一遍再失败（2026-10 空转事故的 typst 条目
# 就是 `”self“` 这种全角引号）。
_SAFE_SEG = re.compile(r"^[A-Za-z0-9._@+-]+$")


@dataclass
class Repo:
    name: str            # owner/repo 形式的全名（子目录目标亦为父仓库的 owner/repo）
    url: str             # clone 地址，缺省由 name 推导
    branch: str | None   # 指定分支；None 表示使用默认分支
    project: str         # 项目短名，用于命名讲义目录；子目录目标 = {base}-{folder}
    focus: str           # 用户期望讲义侧重的内容
    subpath: str = ""    # "" 表示整仓；非空表示子目录目标（如 "clang"），只处理 clone 内该子目录


def repo_key(repo: Repo) -> str:
    """fleet/state 的唯一标识：整仓为 name，子目录目标为 name@subpath。

    子目录目标与普通仓库同级，但共享父仓库的 clone，因此标识需带子目录后缀，
    以避免 state key、教程目录和日志冲突。
    """
    return f"{repo.name}@{repo.subpath}" if repo.subpath else repo.name


def _safe_segments(text: str, *, min_segs: int) -> bool:
    """按段校验名称是否只含安全字符；用于挡掉手误（全角引号、多余引号、空格等）。"""
    segs = text.split("/")
    return len(segs) >= min_segs and all(_SAFE_SEG.match(s) for s in segs)


def load_repos() -> list[Repo]:
    """从 repos.yml 读取所有待分析的项目，支持 folders 子目录拆分与 RC_REPO_NAME 过滤。

    name 以 https:// 开头视为第三方仓库地址，自动推导 url 与 project；
    否则按 owner/repo 拼接 GitHub 地址，project 取 name 的最后一段。

    非法条目（含全角引号、多余引号、空段等）直接跳过并告警，而不是塞进队列：
    这类条目每轮都会 clone 一遍再失败，且会占住并发槽位。
    """
    if not cfg.REPOS_YML.exists():
        cfg.log(f"WARN 未找到 {cfg.REPOS_YML}")
        return []
    data = yaml.safe_load(cfg.REPOS_YML.read_text(encoding="utf-8")) or {}
    repos: list[Repo] = []
    for item in data.get("repos", []) or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        if name.startswith("https://"):
            url = name
            name = name.removeprefix("https://").strip("/")
            if not _safe_segments(name, min_segs=2):
                cfg.log(f"WARN repos.yml 条目 {url!r} 不是合法的 https://host/owner/repo 形式，已跳过")
                continue
            project = (item.get("project") or "").strip() or (
                url.rstrip("/").split("/")[-1].removesuffix(".git"))
        else:
            if not _safe_segments(name, min_segs=2) or len(name.split("/")) != 2:
                cfg.log(f"WARN repos.yml 条目 {name!r} 不是合法的 owner/repo"
                        f"（含非法字符或多级路径？），已跳过")
                continue
            url = (item.get("url") or "").strip() or f"https://github.com/{name}.git"
            project = (item.get("project") or "").strip() or name.split("/")[-1]
        branch = (item.get("branch") or "").strip() or None
        focus = (item.get("focus") or "").strip()
        folders = item.get("folders") or []
        if folders:
            # 子目录拆分：每个 folder 展开为独立条目。"self" 表示整仓（subpath=""，
            # project 取 base 名）；其他值作为子目录路径，project 中的 / 替换为 -。
            for f in folders:
                f = (str(f) or "").strip().strip("/")
                if not f:
                    continue
                if f != "self" and not _safe_segments(f, min_segs=1):
                    cfg.log(f"WARN repos.yml 条目 {name} 的子目录 {f!r} 含非法字符，已跳过"
                            f"（是不是把 self 写成了全角引号？）")
                    continue
                if f == "self":
                    repos.append(Repo(name=name, url=url, branch=branch,
                                      project=project, focus=focus, subpath=""))
                else:
                    repos.append(Repo(name=name, url=url, branch=branch,
                                      project=f"{project}-{f.replace('/', '-')}",
                                      focus=focus, subpath=f))
        else:
            repos.append(Repo(name=name, url=url, branch=branch,
                              project=project, focus=focus, subpath=""))
    # 去重：同一 repo_key 只保留一份。重复条目会在并发里双跑同一 clone / 产物目录，
    # 早年实际发生过（同一仓库在清单里出现两次）。
    seen: set[str] = set()
    deduped: list[Repo] = []
    for r in repos:
        key = repo_key(r)
        if key in seen:
            cfg.log(f"WARN repos.yml 重复条目 {key}，只保留一份（重复会触发并发双跑）")
            continue
        seen.add(key)
        deduped.append(r)
    repos = deduped
    # RC_REPO_NAME：传父仓库名时选中其全部 folder；传完整 name@subpath 时只选中那一个。
    filt = (os.environ.get("RC_REPO_NAME") or "").strip()
    if filt:
        repos = [r for r in repos if repo_key(r) == filt or r.name == filt]
        if not repos:
            cfg.log(f"WARN RC_REPO_NAME={filt!r} 无匹配")
    return repos
