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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

import claude_runner

ROOT = Path(__file__).resolve().parent.parent
# 关键路径可被环境变量覆盖（selftest 用），缺省指向控制仓库内。
REPOS_YML = Path(os.environ.get("RC_REPOS_YML") or (ROOT / "repos.yml"))
STATE_FILE = Path(os.environ.get("RC_STATE_FILE") or (ROOT / "state" / "repos_state.json"))
WORK_DIR = Path(os.environ.get("RC_WORK_DIR") or (ROOT / "work"))
TUTORIALS_DIR = Path(os.environ.get("RC_TUTORIALS_DIR") or (ROOT / "tutorials"))
PROMPTS_DIR = Path(os.environ.get("RC_PROMPTS_DIR") or (ROOT / "prompts"))
PLANNER_TEMPLATE = PROMPTS_DIR / "planner.prompt.md"
WORKER_TEMPLATE = PROMPTS_DIR / "worker.prompt.md"

# 时区（UTC 小时）
DEAD_START = float(os.environ.get("RC_DEADZONE_UTC_START", "6"))
DEAD_END = float(os.environ.get("RC_DEADZONE_UTC_END", "10"))
SOFT_MARGIN_HOURS = float(os.environ.get("RC_DEADZONE_SOFT_MARGIN_MIN", "30")) / 60.0
CONCURRENCY = max(1, int(os.environ.get("RC_CONCURRENCY", "2")))
MAX_RETRIES = int(os.environ.get("RC_MAX_RETRIES", "5"))
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
    h = _utc_hour(now or _now())
    return DEAD_START <= h < DEAD_END


def near_dead_zone(now: datetime | None = None) -> bool:
    h = _utc_hour(now or _now())
    return (DEAD_START - SOFT_MARGIN_HOURS) <= h < DEAD_START


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
            _git(["checkout", repo.branch], repo_dir)
            _git(["reset", "--hard", f"origin/{repo.branch}"], repo_dir)
        else:
            _git(["pull", "--ff-only"], repo_dir)
    else:
        if repo_dir.exists():
            shutil.rmtree(repo_dir, ignore_errors=True)
        log(f"  clone {repo.name}")
        if repo.branch:
            _git(["clone", "--branch", repo.branch, repo.url, str(repo_dir)], ROOT)
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
    if not clone_tutorial.exists() or not any(clone_tutorial.iterdir()):
        raise RuntimeError(f"未生成讲义目录（或为空）：{clone_tutorial}")
    control_tutorial.parent.mkdir(parents=True, exist_ok=True)
    if control_tutorial.exists():
        shutil.rmtree(control_tutorial)
    shutil.copytree(clone_tutorial, control_tutorial)


# --------------------------------------------------------------------------- #
# prompt 组装
# --------------------------------------------------------------------------- #
def compose_planner_prompt(repo: Repo, mode: str, head: str, prev_head: str | None,
                           existing_manifest: str | None, tutorial_dir: str,
                           permalink_base: str) -> str:
    lines = [
        "# 大纲规划任务", "",
        f"项目仓库: {repo.name}",
        f"项目名: {repo.project}",
        f"讲义目录: {tutorial_dir}/",
        f"模式: {mode}",
        f"当前 HEAD: {head}",
        f"代码永久链接 base: {permalink_base}",
        f"user_focus: {repo.focus or '（无）'}",
    ]
    if mode == "incremental" and prev_head:
        lines += [f"上次 HEAD（previous_head）: {prev_head}",
                  "", "## 现有大纲（manifest）", "```json", existing_manifest or "{}", "```"]
    lines += ["", "## 执行", "按 system prompt 中对应模式（full/incremental）产出 manifest JSON。"]
    return "\n".join(lines) + "\n"


def compose_worker_prompt(repo: Repo, lec: dict, action: str, head: str,
                          prev_head: str | None, tutorial_dir: str,
                          permalink_base: str) -> str:
    fn = lec.get("filename", f"{lec.get('id', 'lecture')}.md")
    lines = [
        "# 单篇讲义生成任务", "",
        f"项目仓库: {repo.name}",
        f"项目名: {repo.project}",
        f"讲义目录（Write/Edit 仅限）: {tutorial_dir}/",
        f"代码永久链接 base: {permalink_base}",
        f"当前 HEAD: {head}",
        f"动作: {action}",
    ]
    if action == "update" and prev_head:
        lines.append(f"上次 HEAD（previous_head）: {prev_head}")
    lines += [
        "", "## 本讲义规格（来自大纲）",
        f"- id: {lec.get('id')}",
        f"- 文件名: {fn}   ← 写到 {tutorial_dir}/{fn}",
        f"- 标题: {lec.get('title')}",
        f"- 主题: {lec.get('topic')}",
        f"- 应覆盖的最小模块: {', '.join(lec.get('minimal_modules') or []) or '（自行规划）'}",
        f"- 关键源码: {', '.join(lec.get('source_files') or []) or '（自行定位）'}",
        f"- 依赖讲义: {', '.join(lec.get('depends_on') or []) or '无'}",
        "", "## 任务",
        "按 worker 方法论生成这一篇讲义（最小模块 6 要素）。",
        "- new/rebuild：从零写该文件。",
        "- update：先 Read 现有文件，结合 git diff previous_head..current_head 就地更新。",
        f"只写 `{tutorial_dir}/{fn}` 这一个文件。完成后用一句话总结。",
    ]
    return "\n".join(lines) + "\n"


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
        if in_dead_zone() or near_dead_zone():
            stop.set()
            with lock:
                res["deferred"] = True
            return
        lid = lec.get("id")
        action = lec.get("action", "new")
        prompt = compose_worker_prompt(repo, lec, action, head, prev_head, tutorial_dir, permalink_base)
        try:
            r = call_worker(repo, repo_dir, lec, prompt, worker_cfg)
            with lock:
                prev = entry.get("lectures", {}).get(lid, {})
                entry.setdefault("lectures", {})[lid] = {
                    "status": "done", "action": action,
                    "retries": prev.get("retries", 0), "cost": r.cost_usd,
                }
                res["done"] += 1
        except claude_runner.QuotaExhaustedError as e:
            stop.set()
            with lock:
                prev = entry.get("lectures", {}).get(lid, {})
                entry.setdefault("lectures", {})[lid] = {
                    "status": "failed", "action": action,
                    "retries": prev.get("retries", 0), "error": f"quota: {str(e)[:200]}",
                }
                res["quota"] = True
        except Exception as e:  # noqa: BLE001
            with lock:
                prev = entry.get("lectures", {}).get(lid, {})
                entry.setdefault("lectures", {})[lid] = {
                    "status": "failed", "action": action,
                    "retries": prev.get("retries", 0) + 1,
                    "error": f"{type(e).__name__}: {str(e)[:200]}",
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

        # 已完成且 HEAD 未变且非强制 → 无事可做
        if entry.get("phase") == "done" and not head_changed and not force:
            log(f"  已完成且 HEAD 未变（{head[:8]}），跳过")
            entry["last_run_at"] = generated_at
            save_state(state)
            return "skipped"

        plan_mode = "full" if (force or not has_state) else "incremental"

        # 先把旧讲义/manifest 注入 clone（full 清空），再据其存在性判断是否需重新规划
        # —— 修复：注入必须先于 need_planner，否则续传会误判 manifest 缺失而重复规划
        if plan_mode == "full":
            clear_clone_tutorial(clone_tutorial)
        else:
            inject_prior(control_tutorial, clone_tutorial)

        need_planner = (force or not has_state or head_changed
                        or not (clone_tutorial / "manifest.json").exists()
                        or entry.get("phase") != "workers")

        if need_planner:
            if in_dead_zone() or near_dead_zone():
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
        worker_cfg = claude_runner.ClaudeConfig.from_env(WORKER_TEMPLATE, tutorial_dir)
        if todo:
            log(f"  生成 {len(todo)} 篇讲义（并发 {CONCURRENCY}）…")
            # worker 的 diff 基准用持久化的 prev_head（续传时仍指向旧 head），修复 #2
            res = run_workers(repo, repo_dir, todo, state, entry, head,
                              entry.get("prev_head"), tutorial_dir, permalink_base,
                              worker_cfg, generated_at)
            harvest(clone_tutorial, control_tutorial)
            entry["tutorial_path"] = control_tutorial.relative_to(ROOT).as_posix() \
                if str(control_tutorial).startswith(str(ROOT)) else str(control_tutorial)
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
        else:
            harvest(clone_tutorial, control_tutorial)
            entry["tutorial_path"] = control_tutorial.relative_to(ROOT).as_posix() \
                if str(control_tutorial).startswith(str(ROOT)) else str(control_tutorial)
            entry["last_head"] = head
            entry["last_run_at"] = generated_at

        remaining = [lid for lid, p in entry.get("lectures", {}).items()
                     if p.get("status") in ("pending", "failed")]
        if not remaining:
            if plan_mode == "full":
                entry["version"] = int(entry.get("version", 0)) + 1
            entry["phase"] = "done"
            entry["last_error"] = None
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

    if in_dead_zone() and not env_truthy("RC_ALLOW_DEADZONE"):
        log(f"处于死区 UTC {DEAD_START:g}-{DEAD_END:g}（北京 14-18），本次跳过")
        return 0

    repos = load_repos()
    if not repos:
        log("没有可处理的项目，退出")
        return 0

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
