#!/usr/bin/env python3
"""codeReader 编排层（两阶段管线 + 配额/时区感知 + 断点续传）。

每个项目两阶段：
  阶段1 planner：claude 读项目 → 输出大纲 manifest(JSON)：单元→讲义，每讲义带 action。
  阶段2 workers：对需要处理的讲义，并行（低并发）调 claude 各生成/更新一篇 .md。

可续传：state 记录每篇讲义状态（pending/done/failed/keep/abandoned）。
  - planner 仅在 force/无状态/head 变化/manifest 缺失/非 workers 阶段时才重跑（省额度）。
  - 续传只跑 pending/failed 且未超 RC_MAX_RETRIES 的讲义。
额度耗尽或临近死区 → 保存进度、退出 75。
退出码：0=全部完成；75=未完成（待续传）；1=未预期错误。

RC_MOCK=1 时用本地桩替代 claude（供 selftest 离线验证，不烧额度）。
关键路径可经 RC_REPOS_YML / RC_STATE_FILE / RC_WORK_DIR / RC_TUTORIALS_DIR / RC_PROMPTS_DIR 覆盖。

时区（UTC）：死区 06:00–10:00；便宜窗口 00:00–04:00 / 10:00–14:00。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

import claude_runner
import promptkit

ROOT = Path(__file__).resolve().parent.parent

# debug 模式（RC_DEBUG）：固定单仓库 + 只生成 1 篇 + 忽略死区；默认把 state/work/tutorials
# 重定向到 .debug/ 沙箱，避免污染正式进度（RC_DEBUG_PERSIST=1 则沿用正式路径）。
# 任何 RC_* 路径变量显式设置时仍优先之。
DEBUG = os.environ.get("RC_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
DEBUG_PERSIST = os.environ.get("RC_DEBUG_PERSIST", "").strip().lower() in ("1", "true", "yes", "on")
_DEBUG_SANDBOX = DEBUG and not DEBUG_PERSIST

# 关键路径可被环境变量覆盖（selftest 用），缺省指向控制仓库内。
REPOS_YML = Path(os.environ.get("RC_REPOS_YML") or (ROOT / "repos.yml"))
STATE_FILE = Path(os.environ.get("RC_STATE_FILE") or ((ROOT / ".debug" / "state.json") if _DEBUG_SANDBOX else (ROOT / "state" / "repos_state.json")))
WORK_DIR = Path(os.environ.get("RC_WORK_DIR") or ((ROOT / ".debug" / "work") if _DEBUG_SANDBOX else (ROOT / "work")))
TUTORIALS_DIR = Path(os.environ.get("RC_TUTORIALS_DIR") or ((ROOT / ".debug" / "tutorials") if _DEBUG_SANDBOX else (ROOT / "tutorials")))
PROMPTS_DIR = Path(os.environ.get("RC_PROMPTS_DIR") or (ROOT / "prompts"))
PLANNER_TEMPLATE = PROMPTS_DIR / "planner.prompt.md"
WORKER_TEMPLATE = PROMPTS_DIR / "worker.prompt.md"

# 时区（UTC 小时）
DEAD_START = float(os.environ.get("RC_DEADZONE_UTC_START", "6"))
DEAD_END = float(os.environ.get("RC_DEADZONE_UTC_END", "10"))
SOFT_MARGIN_HOURS = float(os.environ.get("RC_DEADZONE_SOFT_MARGIN_MIN", "30")) / 60.0
# 校验：死区起止必须在 [0,24)，且 margin < 24h（否则软停窗口覆盖全天，永远软停）
if not (0 <= DEAD_START < 24 and 0 <= DEAD_END < 24):
    raise SystemExit(f"RC_DEADZONE_UTC_START/END 必须在 [0,24)：got {DEAD_START}/{DEAD_END}")
if not (0 <= SOFT_MARGIN_HOURS < 24):
    raise SystemExit(f"RC_DEADZONE_SOFT_MARGIN_MIN 必须在 [0,1440)：got {SOFT_MARGIN_HOURS*60}")
CONCURRENCY = max(1, int(os.environ.get("RC_CONCURRENCY", "2")))
MAX_RETRIES = int(os.environ.get("RC_MAX_RETRIES", "5"))
# 同一窗口内单篇 worker 的瞬时重试次数（额度耗尽不在此列，仍立即停整批）；
# 与 RC_BACKOFF_SEC 配合：失败后按该秒数退避再重试。耗尽后才计 retries+=1（→ MAX_RETRIES 为窗口数上限）。
INRUN_RETRIES = max(0, int(os.environ.get("RC_WORKER_INRUN_RETRIES", "1")))
BACKOFF_SEC = float(os.environ.get("RC_BACKOFF_SEC", "0"))
MOCK = os.environ.get("RC_MOCK", "").strip().lower() in ("1", "true", "yes", "on")
MOCK_PLANNER_CALLS = 0   # selftest 用：统计 mock planner 被调次数

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


@dataclass
class Repo:
    name: str
    url: str
    branch: str | None
    project: str
    focus: str


def log(msg: str) -> None:
    print(f"[readcode] {msg}", flush=True)


def env_truthy(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _rel_to_root(p: Path) -> str:
    """优先返回相对 ROOT 的 posix 路径（持久化 tutorial_path 用）；不可相对则回退绝对路径。

    用 resolve()+relative_to 取代字符串前缀比较，避免 Windows 下大小写/分隔符误判。
    """
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------- #
# MOCK 层（RC_MOCK=1 时替代 claude，供 selftest）
# --------------------------------------------------------------------------- #
def _mock_planner(repo: Repo, head: str, plan_mode: str) -> dict:
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
                 prompt: str, cfg: claude_runner.ClaudeConfig, schema_json: str) -> dict:
    if MOCK:
        return _mock_planner(repo, head, plan_mode)
    return claude_runner.run_planner(repo_dir, prompt, cfg, schema_json)


def call_worker(repo: Repo, repo_dir: Path, lec: dict, prompt: str,
                cfg: claude_runner.ClaudeConfig) -> claude_runner.WorkerResult:
    if MOCK:
        fn = lec.get("filename") or f"{lec.get('id', 'lecture')}.md"
        d = repo_dir / cfg.tutorial_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / fn).write_text(
            f"# {lec.get('title', '')}\n\n> mock 讲义 `{lec.get('id')}`\n\n"
            f"主题: {lec.get('topic', '')}\n\n\\[E = mc^2\\]\n", encoding="utf-8")
        return claude_runner.WorkerResult(summary=f"mock wrote {fn}", cost_usd=0.01,
                                          session_id=None, raw={})
    return claude_runner.run_worker(repo_dir, prompt, cfg)


# --------------------------------------------------------------------------- #
# 时区
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_hour(now: datetime) -> float:
    return now.hour + now.minute / 60.0 + now.second / 3600.0


def in_dead_zone(now: datetime | None = None) -> bool:
    """是否处于死区（UTC）。支持跨午夜（DEAD_START > DEAD_END 时按 wrap-around 判定）。"""
    h = _utc_hour(now or _now())
    if DEAD_START < DEAD_END:
        return DEAD_START <= h < DEAD_END
    # 跨午夜：死区 = [START, 24) ∪ [0, END)
    return h >= DEAD_START or h < DEAD_END


def near_dead_zone(now: datetime | None = None) -> bool:
    """是否临近死区（margin 也正确处理跨午夜）。"""
    h = _utc_hour(now or _now())
    start = DEAD_START - SOFT_MARGIN_HOURS   # 软停窗口起点（可 < 0，表示跨午夜）
    end = DEAD_START                          # 软停窗口终点 = 死区起点
    if start < end:
        return start <= h < end
    # 软停窗口跨午夜（如 START=1, margin=2 → start=-1 → %24=23 → [23, 1)）
    start %= 24
    return h >= start or h < end


# --------------------------------------------------------------------------- #
# repos.yml
# --------------------------------------------------------------------------- #
def load_repos() -> list[Repo]:
    if not REPOS_YML.exists():
        log(f"WARN 未找到 {REPOS_YML}")
        return []
    data = yaml.safe_load(REPOS_YML.read_text(encoding="utf-8")) or {}
    repos: list[Repo] = []
    for item in data.get("repos", []) or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        url = (item.get("url") or "").strip() or f"https://github.com/{name}.git"
        project = (item.get("project") or "").strip() or name.split("/")[-1]
        repos.append(Repo(name=name, url=url,
                          branch=(item.get("branch") or "").strip() or None,
                          project=project, focus=(item.get("focus") or "").strip()))
    filt = (os.environ.get("RC_REPO_NAME") or "").strip()
    if filt:
        repos = [r for r in repos if r.name == filt]
        if not repos:
            log(f"WARN RC_REPO_NAME={filt!r} 无匹配")
    return repos


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"WARN {STATE_FILE} 损坏，重启")
    return {"schema_version": 1, "repos": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def get_entry(state: dict, name: str) -> dict:
    return state.setdefault("repos", {}).setdefault(name, {})


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


def sync_repo(repo: Repo) -> Path:
    safe = repo.name.replace("/", "-")
    repo_dir = WORK_DIR / safe
    if repo_dir.exists() and (repo_dir / ".git").exists():
        log(f"  fetch {repo.name}")
        _git(["fetch", "--all", "--tags"], repo_dir)
        if repo.branch:
            # 校验 origin/<branch> 是否存在（上游可能删/改分支）；缺失则 fallback 到默认分支
            ref = f"origin/{repo.branch}"
            check = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", ref],
                cwd=str(repo_dir), capture_output=True, text=True)
            if check.returncode != 0:
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
                _git(["reset", "--hard", ref], repo_dir)
        else:
            _git(["pull", "--ff-only"], repo_dir)
    else:
        if repo_dir.exists():
            shutil.rmtree(repo_dir, ignore_errors=True)
        log(f"  clone {repo.name}")
        if repo.branch:
            # 先 clone 默认分支，再检查目标分支是否存在
            _git(["clone", repo.url, str(repo_dir)], ROOT)
            check = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"origin/{repo.branch}"],
                cwd=str(repo_dir), capture_output=True, text=True)
            if check.returncode == 0:
                _git(["checkout", repo.branch], repo_dir)
            else:
                log(f"  WARN clone 后分支 {repo.branch!r} 在 origin 不存在，留在默认分支")
        else:
            _git(["clone", repo.url, str(repo_dir)], ROOT)
    return repo_dir


def head_of(repo_dir: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo_dir)


# --------------------------------------------------------------------------- #
# 讲义注入 / 收割
# --------------------------------------------------------------------------- #
def inject_prior(control_tutorial: Path, clone_tutorial: Path) -> None:
    if not control_tutorial.exists():
        return
    clone_tutorial.parent.mkdir(parents=True, exist_ok=True)
    if clone_tutorial.exists():
        shutil.rmtree(clone_tutorial)
    shutil.copytree(control_tutorial, clone_tutorial)
    log(f"  注入旧讲义 → clone")


def clear_clone_tutorial(clone_tutorial: Path) -> None:
    if clone_tutorial.exists():
        shutil.rmtree(clone_tutorial)


def harvest(clone_tutorial: Path, control_tutorial: Path) -> None:
    """把 clone 里的讲义收割到 control 目录（原子替换，中途崩溃不丢旧讲义）。

    Windows 上 os.replace / os.rename 不能原子替换非空目录，故采用三步法：
    先 copytree 到同盘 staging，再 rename 旧目录→.old、rename staging→目标，
    最后 rmtree .old。任一步失败都可回滚，旧讲义不丢。
    """
    if not clone_tutorial.exists() or not any(clone_tutorial.iterdir()):
        raise RuntimeError(f"未生成讲义目录（或为空）：{clone_tutorial}")
    control_tutorial.parent.mkdir(parents=True, exist_ok=True)

    # 临时目录必须与 control_tutorial 同盘（跨盘 rename 会失败）
    staging = control_tutorial.parent / (control_tutorial.name + ".new")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(clone_tutorial, staging)

    if control_tutorial.exists():
        old = control_tutorial.parent / (control_tutorial.name + ".old")
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        control_tutorial.rename(old)             # 步骤1：旧 → .old（原子）
        try:
            staging.rename(control_tutorial)     # 步骤2：staging → 目标（原子）
        except OSError:
            old.rename(control_tutorial)         # 回滚
            raise
        try:
            shutil.rmtree(old, ignore_errors=True)  # 步骤3：清理 .old
        except OSError:
            pass   # Windows 上偶发文件被占用，下次 harvest 先 ignore_errors 清
    else:
        staging.rename(control_tutorial)


def _normalize_filename(fn: str) -> str:
    """filename 只允许是裸文件名：丢弃任何目录前缀、统一正斜杠、保 `.md`。

    防御 planner 偶发把 `<项目名>-tutorial/` 前缀写进 filename，导致与 tutorial_dir
    拼接后出现双层路径（如 `Mooncake-tutorial/Mooncake-tutorial/u1-l1.md`）。"""
    fn = (fn or "").replace("\\", "/").strip().strip("/")
    fn = fn.split("/")[-1]            # basename，丢弃任何目录前缀
    if fn and not fn.lower().endswith(".md"):
        fn += ".md"
    return fn or "lecture.md"


def normalize_manifest_filenames(manifest: dict) -> None:
    """就地规范 manifest 中所有讲义的 filename 为裸 basename（写盘前调用一次即可）。"""
    for u in manifest.get("units", []):
        for lec in u.get("lectures", []):
            if "filename" in lec:
                lec["filename"] = _normalize_filename(lec["filename"])


# --------------------------------------------------------------------------- #
# prompt 组装
# --------------------------------------------------------------------------- #
def compose_planner_prompt(repo: Repo, mode: str, head: str, prev_head: str | None,
                           existing_manifest: str | None, tutorial_dir: str,
                           permalink_base: str) -> str:
    """任务提示词由 prompts/planner.task.md（Jinja2）渲染；此处只负责注入参数。"""
    return promptkit.render(PROMPTS_DIR, "planner.task.md",
        repo_name=repo.name, project=repo.project, tutorial_dir=tutorial_dir,
        mode=mode, head=head, permalink_base=permalink_base,
        user_focus=repo.focus or "（无）",
        prev_head=prev_head, existing_manifest=existing_manifest or "{}")


def compose_worker_prompt(repo: Repo, lec: dict, action: str, head: str,
                          prev_head: str | None, tutorial_dir: str,
                          permalink_base: str) -> str:
    """任务提示词由 prompts/worker.task.md（Jinja2）渲染；此处只负责注入参数。"""
    fn = lec.get("filename") or f"{lec.get('id', 'lecture')}.md"
    return promptkit.render(PROMPTS_DIR, "worker.task.md",
        repo_name=repo.name, project=repo.project, tutorial_dir=tutorial_dir,
        permalink_base=permalink_base, head=head, action=action, prev_head=prev_head,
        lec_id=lec.get("id") or "lecture",
        filename=fn, title=lec.get("title") or "（无标题）", topic=lec.get("topic") or "（无主题）",
        level=lec.get("level") or "自动判断",
        learning_goals=lec.get("learning_goals") or [],
        practice_task=lec.get("practice_task") or "由 worker 根据主题和源码自行设计",
        minimal_modules=lec.get("minimal_modules") or [],
        source_files=lec.get("source_files") or [],
        depends_on=lec.get("depends_on") or [])


# --------------------------------------------------------------------------- #
# workers（并行 + 配额/时间感知）
# --------------------------------------------------------------------------- #
def run_workers(repo: Repo, repo_dir: Path, todo: list[dict], state: dict,
                entry: dict, head: str, prev_head: str | None,
                tutorial_dir: str, permalink_base: str,
                worker_cfg: claude_runner.ClaudeConfig, generated_at: str) -> dict:
    stop = threading.Event()
    res = {"done": 0, "failed": 0, "quota": False, "deferred": False}
    lock = threading.Lock()

    def do_one(lec: dict) -> None:
        if stop.is_set():
            return
        if not (DEBUG or env_truthy("RC_ALLOW_DEADZONE")) and (in_dead_zone() or near_dead_zone()):
            stop.set()
            with lock:
                res["deferred"] = True
            return
        lid = lec.get("id")
        action = lec.get("action", "new")
        prompt = compose_worker_prompt(repo, lec, action, head, prev_head, tutorial_dir, permalink_base)
        last_err: Exception | None = None
        for attempt in range(INRUN_RETRIES + 1):
            if stop.is_set():
                return
            try:
                r = call_worker(repo, repo_dir, lec, prompt, worker_cfg)
                with lock:
                    prev = entry.get("lectures", {}).get(lid, {})
                    entry.setdefault("lectures", {})[lid] = {
                        "status": "done", "action": action,
                        "retries": prev.get("retries", 0), "cost": r.cost_usd,
                    }
                    res["done"] += 1
                return
            except claude_runner.QuotaExhaustedError as e:
                stop.set()
                with lock:
                    prev = entry.get("lectures", {}).get(lid, {})
                    entry.setdefault("lectures", {})[lid] = {
                        "status": "failed", "action": action,
                        "retries": prev.get("retries", 0), "error": f"quota: {str(e)[:200]}",
                    }
                    res["quota"] = True
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < INRUN_RETRIES and BACKOFF_SEC > 0:
                    time.sleep(BACKOFF_SEC)
        with lock:
            prev = entry.get("lectures", {}).get(lid, {})
            entry.setdefault("lectures", {})[lid] = {
                "status": "failed", "action": action,
                "retries": prev.get("retries", 0) + 1,
                "error": f"{type(last_err).__name__}: {str(last_err)[:200]}",
            }
            res["failed"] += 1

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [ex.submit(do_one, lec) for lec in todo]
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                log(f"  worker future error: {e}")
            with lock:
                save_state(state)
    return res


# --------------------------------------------------------------------------- #
# 单项目处理
# --------------------------------------------------------------------------- #
def process_repo(repo: Repo, state: dict, generated_at: str) -> str:
    """返回 'done' | 'incomplete' | 'skipped'。"""
    entry = get_entry(state, repo.name)
    try:
        repo_dir = sync_repo(repo)
        head = head_of(repo_dir)
        tutorial_dir = f"{repo.project}-tutorial"
        owner, seg = repo.name.split("/", 1)
        clone_tutorial = repo_dir / tutorial_dir
        control_tutorial = TUTORIALS_DIR / owner / seg / tutorial_dir
        permalink_base = f"https://github.com/{repo.name}/blob/{head}/"

        force = env_truthy("RC_FORCE_FULL") or override_mode == "full"
        has_state = bool(entry.get("manifest_head"))
        old_head = entry.get("manifest_head")        # 本次规划前的 head（worker diff 基准）
        head_changed = has_state and old_head != head

        # 提示词签名：任何 prompts/*.md（系统或任务）被编辑 → 该仓库全量重构。
        # debug 模式跳过（避免 debug 也触发全跑）。sig 仅在此处计算，下方两处安全落盘复用。
        prompt_changed = False
        sig = None
        if not DEBUG:
            sig = promptkit.signature(PROMPTS_DIR)
            prompt_changed = entry.get("prompt_hash") is not None and entry.get("prompt_hash") != sig
            if prompt_changed:
                log(f"  检测到提示词变更（{entry.get('prompt_hash')}→{sig}）→ 全量重构")
                force = True

        # 已完成且 HEAD 未变且非强制 → 无事可做
        if entry.get("phase") == "done" and not head_changed and not force:
            # 兜底：历史/手造状态可能没记 prompt_hash → 记当前签名作基线，
            # 此后任何提示词变更都能被检测（否则 prompt_hash 缺失时变更会被漏检、旧讲义不重写）。
            # 想立即重建用 RC_FORCE_FULL，这里不烧额度。
            if sig is not None and entry.get("prompt_hash") is None:
                entry["prompt_hash"] = sig
            log(f"  已完成且 HEAD 未变（{head[:8]}），跳过")
            entry["last_run_at"] = generated_at
            save_state(state)
            return "skipped"

        plan_mode = "full" if (force or not has_state) else "incremental"

        # 先把旧讲义/manifest 注入 clone（full 清空），再据其存在性判断是否需重新规划
        # —— 修复：注入必须先于 need_planner，否则续传会误判 manifest 缺失而重复规划
        if plan_mode == "full":
            if prompt_changed:
                # 提交新签名：即使重构中途额度耗尽（exit 75），下次续传签名已匹配、
                # 不再重复清空，已生成的部分讲义得以保留继续（避免「每次清空重来」死循环）。
                entry["prompt_hash"] = sig
                save_state(state)
            clear_clone_tutorial(clone_tutorial)
        else:
            inject_prior(control_tutorial, clone_tutorial)

        need_planner = (force or not has_state or head_changed
                        or not (clone_tutorial / "manifest.json").exists()
                        or entry.get("phase") != "workers")

        if need_planner:
            if not (DEBUG or env_truthy("RC_ALLOW_DEADZONE")) and (in_dead_zone() or near_dead_zone()):
                log(f"  临近/处于死区，暂缓规划 {repo.name}（保留进度，下次窗口续传）")
                entry["last_run_at"] = generated_at
                save_state(state)
                return "incomplete"
            log(f"  规划大纲（mode={plan_mode}）…")
            existing = None
            if plan_mode == "incremental" and (clone_tutorial / "manifest.json").exists():
                existing = (clone_tutorial / "manifest.json").read_text(encoding="utf-8")
            planner_cfg = claude_runner.ClaudeConfig.from_env(PLANNER_TEMPLATE, tutorial_dir)
            manifest = call_planner(
                repo, head, plan_mode, repo_dir,
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
                    lid = lec.get("id")
                    if not lid:
                        continue
                    action = lec.get("action") or ("new" if plan_mode == "full" else "update")
                    lectures[lid] = {"status": "keep" if action == "keep" else "pending",
                                     "action": action, "retries": 0}
            entry["lectures"] = lectures
            entry["manifest_head"] = head
            entry["prev_head"] = old_head            # 持久化 diff 基准，供续传 worker 使用
            entry["phase"] = "workers"
            entry["mode"] = plan_mode
            save_state(state)
        else:
            log(f"  续传 workers（{repo.name}）")
            if not (clone_tutorial / "manifest.json").exists():
                entry["phase"] = None
                save_state(state)
                return "incomplete"

        # 超过重试上限的讲义标记为 abandoned（终态，不再烧额度，也不阻塞完成）
        for lid, p in list(entry.get("lectures", {}).items()):
            if p.get("status") in ("pending", "failed") and p.get("retries", 0) >= MAX_RETRIES:
                p["status"] = "abandoned"
                p["error"] = (p.get("error", "") + " | abandoned: max retries").strip(" |")
        save_state(state)

        # workers：只跑 pending/failed（abandoned/keep/done 跳过）
        manifest = json.loads((clone_tutorial / "manifest.json").read_text(encoding="utf-8"))
        lec_specs = {lec["id"]: lec for u in manifest.get("units", [])
                     for lec in u.get("lectures", []) if lec.get("id")}
        todo = [lec_specs[lid] for lid, p in entry.get("lectures", {}).items()
                if p.get("status") in ("pending", "failed") and lid in lec_specs]
        if DEBUG:
            # debug 模式：只生成 1 篇——RC_DEBUG_LECTURE_ID 指定，否则首个单元首篇
            target = (os.environ.get("RC_DEBUG_LECTURE_ID") or "").strip()
            spec = lec_specs.get(target) if (target and target in lec_specs) \
                else (next(iter(lec_specs.values()), None) if lec_specs else None)
            if not spec:
                log(f"  DEBUG：未找到目标讲义（RC_DEBUG_LECTURE_ID={target!r}），跳过")
                entry["last_error"] = "debug: target lecture not found"
                save_state(state)
                return "incomplete"
            todo = [spec]
        worker_cfg = claude_runner.ClaudeConfig.from_env(WORKER_TEMPLATE, tutorial_dir)
        if todo:
            log(f"  生成 {len(todo)} 篇讲义（并发 {CONCURRENCY}）…")
            # worker 的 diff 基准用持久化的 prev_head（续传时仍指向旧 head），修复 #2
            res = run_workers(repo, repo_dir, todo, state, entry, head,
                              entry.get("prev_head"), tutorial_dir, permalink_base,
                              worker_cfg, generated_at)
            harvest(clone_tutorial, control_tutorial)
            entry["tutorial_path"] = _rel_to_root(control_tutorial)
            entry["last_head"] = head
            entry["last_run_at"] = generated_at
            if res["quota"]:
                entry["phase"] = "workers"
                entry["last_error"] = "quota exhausted; will resume next window"
                save_state(state)
                log(f"  额度耗尽，已收割进度 → {entry['tutorial_path']}")
                return "incomplete"
            if res["deferred"]:
                entry["phase"] = "workers"
                entry["last_error"] = "near dead zone; will resume next window"
                save_state(state)
                log(f"  临近死区，暂停；剩余讲义下次续传 → {entry['tutorial_path']}")
                return "incomplete"
            if DEBUG:
                # debug：单篇已生成即视为本轮完成（return "done" 让 main 退出 0），
                # 但**不写 phase=done**——RC_DEBUG_PERSIST=1 时会污染正式 state，
                # 下次正式跑被 line 478 短路，剩余讲义永不生成。phase 保持 "workers"。
                entry["last_error"] = None
                entry["last_run_at"] = generated_at
                save_state(state)
                log(f"  DEBUG 完成：已生成 {todo[0].get('id')} → {entry['tutorial_path']}")
                return "done"
        else:
            harvest(clone_tutorial, control_tutorial)
            entry["tutorial_path"] = _rel_to_root(control_tutorial)
            entry["last_head"] = head
            entry["last_run_at"] = generated_at

        remaining = [lid for lid, p in entry.get("lectures", {}).items()
                     if p.get("status") in ("pending", "failed")]
        if not remaining:
            if plan_mode == "full":
                entry["version"] = int(entry.get("version", 0)) + 1
            entry["phase"] = "done"
            entry["last_error"] = None
            if not DEBUG and sig is not None:
                entry["prompt_hash"] = sig   # 首次构建/每次完成都记当前签名，供后续变更检测
            save_state(state)
            log(f"  全部完成 → {entry['tutorial_path']}（v{entry.get('version', 0)}）")
            return "done"
        entry["phase"] = "workers"
        entry["last_error"] = f"{len(remaining)} 篇未完成: {remaining[:5]}"
        save_state(state)
        log(f"  仍有 {len(remaining)} 篇未完成：{remaining[:5]}")
        return "incomplete"
    except claude_runner.QuotaExhaustedError as e:
        log(f"  额度耗尽（{repo.name}）：{str(e)[:200]}；保存进度")
        entry["last_error"] = f"quota: {str(e)[:300]}"
        entry["last_run_at"] = generated_at
        save_state(state)
        return "incomplete"
    except Exception as e:  # noqa: BLE001
        log(f"  ERROR {repo.name}: {type(e).__name__}: {e}")
        entry["last_error"] = f"{type(e).__name__}: {str(e)[:300]}"
        entry["last_run_at"] = generated_at
        save_state(state)
        return "incomplete"


# module-level，在 main 里设置（process_repo 读取）
override_mode = "auto"


def main() -> int:
    global override_mode
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
    for i, repo in enumerate(repos):
        log(f"=== [{i + 1}/{len(repos)}] {repo.name}（{repo.project}）===")
        outcome = process_repo(repo, state, generated_at)
        if outcome == "incomplete":
            any_incomplete = True
            if any("quota" in (get_entry(state, r.name).get("last_error") or "")
                   for r in repos):
                log("检测到额度耗尽，停止后续项目，等待下次窗口")
                break

    save_state(state)
    if any_incomplete:
        log("=== 本轮存在未完成项（进度已保存，下次窗口自动续传）===")
        return 75
    log("=== 全部完成 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
