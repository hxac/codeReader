"""git 操作：clone / fetch / checkout / HEAD。"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import config as cfg
from . import runtime


def _git(args: list[str], cwd: Path) -> str:
    """在 cwd 下执行一条 git 命令并返回 stdout；非零退出时直接抛错，stderr 截取一段附于错误信息。"""
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


def _remote_branch_exists(repo_dir: Path, branch: str) -> bool:
    r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"origin/{branch}"],
                       cwd=str(repo_dir), capture_output=True, text=True, check=False)
    return r.returncode == 0


def sync_repo(repo) -> Path:
    """将被分析项目同步到本地工作区，确保其为最新状态。

    已 clone 则执行 fetch；首次则执行 clone。指定分支在上游被删除或改名时，
    回退到默认分支，而非直接报错终止。
    """
    safe = repo.name.replace("/", "-")
    repo_dir = cfg.WORK_DIR / safe
    # 同一父仓库的多个 folder 共享该 clone：按目录加锁，串行执行 fetch/clone，
    # 避免并发竞争 .git/index.lock。
    with runtime.clone_lock(repo_dir):
        if repo_dir.exists() and (repo_dir / ".git").exists():
            cfg.phase_log(repo.name, "clone", "(fetch)")
            _git(["fetch", "--all", "--tags"], repo_dir)
            if repo.branch:
                if not _remote_branch_exists(repo_dir, repo.branch):
                    cfg.log(f"  WARN 分支 {repo.branch!r} 在 origin 不存在，"
                            f"回退到默认分支（repos.yml 的 branch 配置可能已过期）")
                    try:
                        default = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                                       repo_dir).removeprefix("origin/")
                    except RuntimeError:
                        default = "main"
                        cfg.log("  WARN 无法确定默认分支，回退到 'main'")
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
            cfg.phase_log(repo.name, "clone")
            # 先 clone 默认分支，再检查目标分支是否存在
            _git(["clone", repo.url, str(repo_dir)], cfg.ROOT)
            if repo.branch and _remote_branch_exists(repo_dir, repo.branch):
                _git(["checkout", repo.branch], repo_dir)
            elif repo.branch:
                cfg.log(f"  WARN clone 后分支 {repo.branch!r} 在 origin 不存在，保留在默认分支")
    return repo_dir


def head_of(repo_dir: Path) -> str:
    """返回工作区当前 HEAD 的完整 commit id。"""
    return _git(["rev-parse", "HEAD"], repo_dir)


# git 报错里哪些属于「重试也不会自愈」：仓库被删/改名/转私有、分支不存在、凭据失效。
# 这类错误必须与限流/网络抖动区别对待——后者欠位等下轮，前者要摘出队列，
# 否则坏条目会一直占着并发槽位（2026-10 队头阻塞事故的直接成因）。
_PERMANENT_RE = re.compile(
    r"repository\b.*\bnot found"          # fatal: repository 'https://…' not found
    r"|repository does not exist"
    r"|does not appear to be a git repository"
    r"|not a git repository"
    r"|not found in upstream"             # 远端分支被删/改名
    r"|could not read username"           # 私有仓/凭据失效
    r"|authentication failed"
    r"|permission denied"
    r"|invalid refspec",
    re.IGNORECASE,
)


def is_permanent_error(exc: BaseException) -> bool:
    """判断一条 git 异常是否属于配置错误（重试无益）而非瞬时故障。"""
    return bool(_PERMANENT_RE.search(str(exc)))
