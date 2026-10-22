"""编排层：两阶段管线（planner → workers），含配额感知、时区死区与断点续传。

每个项目分两步：
  第一步 planner：让 agent 通读整个项目，输出大纲 manifest——一棵「单元 → 讲义」树，
    每篇讲义附带其处理方式。
  第二步 workers：对需要处理的讲义，以低并发并行调用 agent，逐篇生成或更新 md。

续传依赖 state 文件记录每篇讲义的状态：pending、done、failed、keep、abandoned。
  planner 仅在强制、无状态、HEAD 变化、manifest 缺失或尚未进入 workers 阶段时重跑。
  续传只生成 pending 与 failed、且未超过 RC_MAX_RETRIES 的讲义。
额度耗尽或临近死区时保存进度并以 75 退出，将剩余任务留至下一个时间窗口。

退出码约定：0 = 全部完成；75 = 未完成但进度已存盘、下轮续传；1 = 意外错误。
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

from . import config as cfg
from . import gitutil, prompts, runtime, scheduler, state as st, tutorial
from .provider import (AgentConfig, ClaudeTimeoutError, QuotaExhaustedError,
                       call_planner, call_summarize, call_worker)
from .repo import Repo, load_repos, repo_key
from .state import (RETRYABLE, STATUS_ABANDONED, STATUS_DONE, STATUS_FAILED,
                    STATUS_KEEP, STATUS_PENDING)


class RepoConfigError(RuntimeError):
    """仓库级配置错误：重试不会自愈（子目录不存在、仓库被删/改名、名称非法…）。

    与限流/额度这类瞬时错误（incomplete）分开：配置错误要摘出队列，不能占着槽位
    等下个窗口；否则队头一坏就再也轮不到后面的仓库。
    """


# --------------------------------------------------------------------------- #
# workers：串行生成讲义，全程感知配额与时间，支持链式前文
# --------------------------------------------------------------------------- #
def _collect_prior_summaries(ordered_lectures: list[dict], current_lec: dict,
                             clone_tutorial, repo, repo_dir, tutorial_dir,
                             transcript_dir=None) -> list[dict]:
    """收集当前讲义之前所有讲义的摘要 sidecar；缺失时即时补生成。"""
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
            md_path = clone_tutorial / st.lec_filename(lec)
            if md_path.exists():
                cfg.log(f"  按需补摘要（{lid}）")
                try:
                    summary_text = call_summarize(repo, repo_dir, lec, tutorial_dir,
                                                  transcript_dir=transcript_dir)
                    sidecar.write_text(summary_text, encoding="utf-8")
                except Exception as e:  # noqa: BLE001
                    cfg.log(f"  WARN 摘要失败（{lid}）：{e}；记录为空")
                    summary_text = "（摘要生成失败）"
            else:
                summary_text = "（讲义文件缺失，无摘要）"
        summaries.append({"id": lid, "title": lec.get("title", ""),
                          "summary": summary_text})
    return summaries


def run_lectures_sequential(repo: Repo, repo_dir, ordered_lectures: list[dict], state: dict,
                            entry: dict, head: str, prev_head: str | None,
                            tutorial_dir: str, permalink_base: str,
                            worker_cfg: AgentConfig, generated_at: str,
                            outline_prompt: str = "",
                            outline_content: str = "") -> dict:
    """按 manifest 顺序逐篇串行生成或更新讲义。

    每篇开始前检查全局停止、软截止与死区。done/keep/abandoned 跳过生成，但仍遍历，
    以收集其摘要 sidecar 作为后续讲义的链式前文。

    返回 {done, failed, quota, deferred}。
    """
    res = {"done": 0, "failed": 0, "quota": False, "deferred": False}
    run_start = time.monotonic()
    deadline_min = float(os.environ.get("RC_RUN_DEADLINE_MIN", "0") or "0")
    total = len([l for l in ordered_lectures if st.lec_status(entry, l["id"]) in RETRYABLE])

    n_done = 0
    for lec in ordered_lectures:
        # ---- 全局/时间守卫 ----
        if runtime.GLOBAL_STOP.is_set():
            res["deferred"] = res["deferred"] or (not res["quota"])
            return res
        if scheduler.deadline_reached(run_start, deadline_min):
            runtime.GLOBAL_STOP.set()
            res["deferred"] = True
            return res
        if scheduler.deadzone_blocked():
            runtime.GLOBAL_STOP.set()
            res["deferred"] = True
            return res

        lid = lec.get("id")
        st_rec = entry.get("lectures", {}).get(lid, {})
        status = st_rec.get("status")
        action = lec.get("action", "new")

        # done、keep、abandoned 跳过生成，但遍历继续，以收集前文摘要
        if status in (STATUS_DONE, STATUS_KEEP, STATUS_ABANDONED):
            continue

        # pending/failed → 生成
        clone_tutorial = repo_dir / tutorial_dir
        prior = _collect_prior_summaries(ordered_lectures, lec, clone_tutorial,
                                         repo, repo_dir, tutorial_dir,
                                         transcript_dir=worker_cfg.transcript_dir)
        prompt = prompts.compose_worker_prompt(repo, lec, action, head, prev_head,
                                               tutorial_dir, permalink_base,
                                               outline_prompt=outline_prompt,
                                               outline_content=outline_content,
                                               prior_summaries=prior)
        last_err: Exception | None = None
        for attempt in range(cfg.INRUN_RETRIES + 1):
            if runtime.GLOBAL_STOP.is_set():
                break
            try:
                r = call_worker(repo, repo_dir, lec, prompt, worker_cfg)
                # 校验 .md 是否写入，并生成摘要 sidecar
                md_path = clone_tutorial / st.lec_filename(lec)
                if md_path.exists():
                    try:
                        summary_text = call_summarize(repo, repo_dir, lec, tutorial_dir,
                                                      transcript_dir=worker_cfg.transcript_dir)
                        (clone_tutorial / f"{lid}.summary.md").write_text(
                            summary_text, encoding="utf-8")
                    except Exception as e:  # noqa: BLE001
                        cfg.log(f"  WARN 摘要失败（{lid}）：{e}")
                res["done"] += 1
                n_done += 1
                # 状态变更与存盘置于同一锁内，防止并发 json.dumps 期间字典被修改
                with runtime.GLOBAL_STATE_LOCK:
                    st.write_lecture_status(entry, lid, status=STATUS_DONE, action=action,
                                            retries=st_rec.get("retries", 0), cost=r.cost_usd)
                    st.save_state(state)
                cfg.phase_log(repo_key(repo), "讲义生成", f"({n_done}/{total}) {lid}")
                break
            except QuotaExhaustedError as e:
                # 退避已耗尽：暂停当前仓库，不设置全局停止、不连累其他仓库
                res["quota"] = True
                with runtime.GLOBAL_STATE_LOCK:
                    st.write_lecture_status(entry, lid, status=STATUS_FAILED, action=action,
                                            retries=st_rec.get("retries", 0),
                                            error=f"quota: {str(e)[:200]}")
                    st.save_state(state)
                return res
            except ClaudeTimeoutError as e:
                # 超时不进行当场重试（否则将额外消耗一个 RC_TIMEOUT）：
                # 将当前篇目判为失败并继续下一篇
                res["failed"] += 1
                with runtime.GLOBAL_STATE_LOCK:
                    st.write_lecture_status(entry, lid, status=STATUS_FAILED, action=action,
                                            retries=st_rec.get("retries", 0) + 1,
                                            error=f"timeout: {str(e)[:200]}")
                    st.save_state(state)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < cfg.INRUN_RETRIES and cfg.BACKOFF_SEC > 0:
                    time.sleep(cfg.BACKOFF_SEC)
        else:
            # 当场重试已耗尽，且失败原因不是超时
            res["failed"] += 1
            with runtime.GLOBAL_STATE_LOCK:
                st.write_lecture_status(entry, lid, status=STATUS_FAILED, action=action,
                                        retries=st_rec.get("retries", 0) + 1,
                                        error=f"{type(last_err).__name__}: {str(last_err)[:200]}")
                st.save_state(state)

    return res


# --------------------------------------------------------------------------- #
# 仓库级熔断：把「配置错误」与「配额/限流」分开处理
# --------------------------------------------------------------------------- #
def _clear_blocked(entry: dict) -> None:
    """仓库这回正常处理了：清掉配置错误计数与熔断标记（自愈后不留痕）。

    用 pop 而不是写 False：否则每个跑过的仓库都会多出一个 `disabled: false` 字段，
    让 state 在第一次跑完后产生一整批无意义 diff。
    """
    entry.pop("blocked", None)
    entry.pop("blocked_reason", None)
    entry.pop("disabled", None)
    entry.pop("disabled_at", None)


def _mark_blocked(entry: dict, key: str, err: BaseException, generated_at: str,
                  state: dict) -> str:
    """记录一次配置错误；连续 BLOCK_LIMIT 次后把该仓库摘出队列（熔断）。

    返回 "blocked"：调度器据此**照常补位**——永久失败不该占用并发槽位。
    """
    with runtime.GLOBAL_STATE_LOCK:
        count = int(entry.get("blocked", 0)) + 1
        entry["blocked"] = count
        entry["blocked_reason"] = f"{type(err).__name__}: {str(err)[:300]}"
        entry["last_error"] = f"blocked: {entry['blocked_reason']}"
        entry["last_run_at"] = generated_at
        tripped = count >= cfg.BLOCK_LIMIT
        if tripped:
            entry["disabled"] = True
            entry["disabled_at"] = generated_at
        st.save_state(state)
    if tripped:
        cfg.log(f"  [BLOCK] {key} 配置错误（连续 {count} 次）→ 摘出队列，"
                f"{cfg.BLOCK_RETRY_HOURS:g} 小时后自动重试；请修 repos.yml")
        cfg.log(f"     原因：{entry['blocked_reason']}")
    else:
        cfg.log(f"  [BLOCK] {key} 配置错误（连续 {count}/{cfg.BLOCK_LIMIT} 次后摘出队列）："
                f"{entry['blocked_reason']}")
    return "blocked"


def _disabled_for_now(entry: dict, now: datetime) -> bool:
    """熔断中的仓库是否仍在冷却期。冷却到期即放回队列重试，无需人工干预。"""
    if not entry.get("disabled"):
        return False
    raw = entry.get("disabled_at")
    if not raw:
        return False
    try:
        when = datetime.strptime(str(raw), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False          # 时间戳损坏 → 当作已到期，重新试一次
    return (now - when).total_seconds() < cfg.BLOCK_RETRY_HOURS * 3600


# --------------------------------------------------------------------------- #
# 单项目处理
# --------------------------------------------------------------------------- #
def process_repo(repo: Repo, state: dict, generated_at: str, mode: str) -> str:
    """完整处理单个项目，返回 done、incomplete、skipped 或 blocked 之一。

    incomplete = 限额/额度/死区这类「下个窗口再试」的暂停；blocked = 配置错误
    （子目录不存在、仓库被删/改名），重试无益，调度器会立即补位并在计数达阈值后熔断。
    """
    entry = st.get_entry(state, repo_key(repo))
    try:
        repo_dir = gitutil.sync_repo(repo)
        head = gitutil.head_of(repo_dir)
        # 子目录目标（self 标记）：agent 在该子目录内运行，将其视为项目根，
        # 因此只能看到该子目录。共享父仓库的 clone，cwd、worker 写入、manifest
        # 与 harvest 均落在子目录下。
        work_cwd = repo_dir / repo.subpath if repo.subpath else repo_dir
        if repo.subpath and not work_cwd.exists():
            # 配置错误而非瞬时失败：clone 成功但目录不在，重试多少次都一样。
            # 交给 _mark_blocked 计数 + 熔断，绝不能再按 incomplete 占着槽位。
            raise RepoConfigError(
                f"子目录不存在: {repo.subpath!r}（检查 repos.yml 的 folders）")
        tutorial_dir = f"{repo.project}-tutorial"
        owner, seg = repo.name.split("/", 1)
        clone_tutorial = work_cwd / tutorial_dir
        control_tutorial = cfg.TUTORIALS_DIR / owner / seg / tutorial_dir
        permalink_base = (f"https://github.com/{repo.name}/blob/{head}/"
                          + (f"{repo.subpath}/" if repo.subpath else ""))

        # 手动全量开关（RC_FORCE_FULL / mode=full）仅在非定时运行时生效。定时运行
        # 结构性忽略这些开关，确保定时触发只在「提示词变更」或「首次构建」时执行全量。
        scheduled = os.environ.get("RC_EVENT_NAME") == "schedule"
        force = (not scheduled) and (cfg.env_truthy("RC_FORCE_FULL") or mode == "full")
        has_state = bool(entry.get("manifest_head"))
        old_head = entry.get("manifest_head")        # 本次规划前的 head，作为 worker 计算 diff 的基准
        head_changed = has_state and old_head != head

        # 提示词签名：prompts 下任一 md 被修改，该仓库即需全量重构。debug 模式有意
        # 跳过此检查，避免 debug 触发整轮全量。sig 仅在此计算一次，后续两处落盘复用。
        prompt_changed = False
        sig = None
        if not cfg.DEBUG:
            sig = prompts.signature(cfg.PROMPTS_DIR)
            prompt_changed = entry.get("prompt_hash") is not None and entry.get("prompt_hash") != sig
            if prompt_changed:
                cfg.log(f"  检测到提示词变更（{entry.get('prompt_hash')}→{sig}）→ 全量重构")
                force = True
            # 尽早写入基线签名：任何被触碰过的仓库，即使 workers 尚未完成，也记录
            # 当前签名，以便提示词变更时被发现。
            if entry.get("prompt_hash") is None:
                with runtime.GLOBAL_STATE_LOCK:
                    entry["prompt_hash"] = sig

        # 已完成且 HEAD 未变且非强制 → 无需处理
        if entry.get("phase") == "done" and not head_changed and not force:
            cfg.log(f"  已完成且 HEAD 未变（{head[:8]}），跳过")
            with runtime.GLOBAL_STATE_LOCK:
                entry["last_run_at"] = generated_at
                _clear_blocked(entry)
                st.save_state(state)
            return "skipped"

        plan_mode = "full" if (force or not has_state) else "incremental"

        # 先注入旧讲义或清场，再判断是否需要重新规划——顺序约束：注入必须先于
        # need_planner 判断，否则续传时会误判 manifest 缺失而重复规划。
        if plan_mode == "full":
            if prompt_changed:
                # 先写入新签名：即使重构中途额度耗尽而退出 75，下次续传时签名已一致，
                # 不会再次清空，已生成部分可继续，避免每次清空重来的循环。
                with runtime.GLOBAL_STATE_LOCK:
                    entry["prompt_hash"] = sig
                    st.save_state(state)
            tutorial.clear_clone_tutorial(clone_tutorial)
        else:
            tutorial.inject_prior(control_tutorial, clone_tutorial)

        need_planner = (force or not has_state or head_changed
                        or not (clone_tutorial / "manifest.json").exists()
                        or entry.get("phase") != "workers")

        if need_planner:
            if scheduler.deadzone_blocked():
                cfg.log(f"  临近或处于死区，暂缓规划 {repo_key(repo)}（保留进度，下次窗口续传）")
                with runtime.GLOBAL_STATE_LOCK:
                    entry["last_run_at"] = generated_at
                    st.save_state(state)
                return "incomplete"
            cfg.phase_log(repo_key(repo), "大纲生成")
            existing = None
            if plan_mode == "incremental" and (clone_tutorial / "manifest.json").exists():
                existing = (clone_tutorial / "manifest.json").read_text(encoding="utf-8")
            planner_cfg = AgentConfig.from_env(cfg.PLANNER_TEMPLATE, tutorial_dir)
            (clone_tutorial / ".transcripts").mkdir(parents=True, exist_ok=True)
            planner_cfg.transcript_dir = (clone_tutorial / ".transcripts").resolve()
            manifest = call_planner(
                repo, head, plan_mode, work_cwd,
                prompts.compose_planner_prompt(repo, plan_mode, head, old_head, existing,
                                               tutorial_dir, permalink_base),
                planner_cfg, json.dumps(prompts.MANIFEST_SCHEMA))
            tutorial.normalize_manifest_filenames(manifest)   # filename 规范为裸 basename
            clone_tutorial.mkdir(parents=True, exist_ok=True)
            (clone_tutorial / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            lectures = {}
            for u in manifest.get("units", []):
                for lec in u.get("lectures", []):
                    if lid := lec.get("id"):
                        action = lec.get("action") or ("new" if plan_mode == "full" else "update")
                        lectures[lid] = {"status": STATUS_KEEP if action == "keep" else STATUS_PENDING,
                                         "action": action, "retries": 0}
            with runtime.GLOBAL_STATE_LOCK:
                entry["lectures"] = lectures
                entry["manifest_head"] = head
                entry["prev_head"] = old_head            # 持久化 diff 基准，供续传 worker 使用
                entry["phase"] = "workers"
                entry["mode"] = plan_mode
                st.save_state(state)
        else:
            cfg.log(f"  续传 workers（{repo_key(repo)}）")
            if not (clone_tutorial / "manifest.json").exists():
                with runtime.GLOBAL_STATE_LOCK:
                    entry["phase"] = None
                    st.save_state(state)
                return "incomplete"

        # 重试次数超过上限的讲义标记为 abandoned：终态，不再为其消耗额度，
        # 也不会阻塞整体完成
        with runtime.GLOBAL_STATE_LOCK:
            for lid, p in list(entry.get("lectures", {}).items()):
                if p.get("status") in (STATUS_PENDING, STATUS_FAILED) and p.get("retries", 0) >= cfg.MAX_RETRIES:
                    p["status"] = STATUS_ABANDONED
                    p["error"] = (p.get("error", "") + " | abandoned: max retries").strip(" |")
            st.save_state(state)

        manifest = json.loads((clone_tutorial / "manifest.json").read_text(encoding="utf-8"))
        lec_specs = {lec["id"]: lec for u in manifest.get("units", [])
                     for lec in u.get("lectures", []) if lec.get("id")}
        # 全部讲义按 manifest 顺序排列；done/keep/abandoned 在循环中跳过生成，仅收集前文摘要
        ordered = [lec_specs[lid] for lid in entry.get("lectures", {})
                   if lid in lec_specs]
        if cfg.DEBUG:
            # debug 模式仅生成一篇：由 RC_DEBUG_LECTURE_ID 指定，未设置时取首个单元的第一篇
            target = (os.environ.get("RC_DEBUG_LECTURE_ID") or "").strip()
            spec = lec_specs.get(target) if (target and target in lec_specs) \
                else (next(iter(lec_specs.values()), None) if lec_specs else None)
            if not spec:
                cfg.log(f"  DEBUG：未找到目标讲义（RC_DEBUG_LECTURE_ID={target!r}），跳过")
                with runtime.GLOBAL_STATE_LOCK:
                    entry["last_error"] = "debug: target lecture not found"
                    st.save_state(state)
                return "incomplete"
            ordered = [spec]
        worker_cfg = AgentConfig.from_env(cfg.WORKER_TEMPLATE, tutorial_dir)
        worker_cfg.transcript_dir = clone_tutorial / ".transcripts"
        outline_prompt = cfg.PLANNER_TEMPLATE.read_text(encoding="utf-8")
        outline_content = json.dumps(
            {"project": manifest.get("project"), "rationale": manifest.get("rationale", ""),
             "units": manifest.get("units", [])},
            ensure_ascii=False, indent=2)
        todo = [l for l in ordered if st.lec_status(entry, l["id"]) in RETRYABLE]
        if todo:
            cfg.phase_log(repo_key(repo), "讲义生成", f"(0/{len(todo)})")
            res = run_lectures_sequential(repo, work_cwd, ordered, state, entry, head,
                                          entry.get("prev_head"), tutorial_dir, permalink_base,
                                          worker_cfg, generated_at,
                                          outline_prompt=outline_prompt,
                                          outline_content=outline_content)
            tutorial.harvest(clone_tutorial, control_tutorial)
            with runtime.GLOBAL_STATE_LOCK:
                tutorial.record_harvest(entry, control_tutorial, head, generated_at)
            if res["quota"]:
                with runtime.GLOBAL_STATE_LOCK:
                    entry["phase"] = "workers"
                    entry["last_error"] = "quota exhausted; will resume next window"
                    st.save_state(state)
                cfg.log(f"  额度耗尽，已收割进度 → {entry['tutorial_path']}")
                return "incomplete"
            if res["deferred"]:
                with runtime.GLOBAL_STATE_LOCK:
                    entry["phase"] = "workers"
                    entry["last_error"] = "near dead zone; will resume next window"
                    st.save_state(state)
                cfg.log(f"  临近死区，暂停；剩余讲义下个窗口续传 → {entry['tutorial_path']}")
                return "incomplete"
            if cfg.DEBUG:
                # debug 模式生成一篇即视为本轮完成（退出 0），但有意不写 phase=done：
                # 当 RC_DEBUG_PERSIST=1 写入正式 state 时，后续正式运行不会被
                # 「已完成」判断短路。
                with runtime.GLOBAL_STATE_LOCK:
                    entry["last_error"] = None
                    entry["last_run_at"] = generated_at
                    st.save_state(state)
                cfg.log(f"  DEBUG 完成：已生成 {ordered[0].get('id')} → {entry['tutorial_path']}")
                return "done"
        else:
            tutorial.harvest(clone_tutorial, control_tutorial)
            with runtime.GLOBAL_STATE_LOCK:
                tutorial.record_harvest(entry, control_tutorial, head, generated_at)

        remaining = [lid for lid, p in entry.get("lectures", {}).items()
                     if p.get("status") in RETRYABLE]
        # abandoned 为永久失败，不计入 remaining，不会阻塞 done；但需记录以供
        # 发布告警，否则带缺失讲义的 done 仓库会静默地把残缺教程发布到 main。
        abandoned = [lid for lid, p in entry.get("lectures", {}).items()
                     if p.get("status") == STATUS_ABANDONED]
        if not remaining:
            with runtime.GLOBAL_STATE_LOCK:
                entry["abandoned"] = abandoned   # 可为空列表
                if plan_mode == "full":
                    entry["version"] = int(entry.get("version", 0)) + 1
                entry["phase"] = "done"
                entry["last_error"] = None
                _clear_blocked(entry)
                if not cfg.DEBUG and sig is not None:
                    entry["prompt_hash"] = sig   # 首次构建/每次完成均记录当前签名
                st.save_state(state)
            if abandoned:
                cfg.log(f"  ⚠ 全部完成，但有 {len(abandoned)} 篇 abandoned"
                        f"（永久失败、已跳过，缺篇发布）: {abandoned[:5]}")
            cfg.log(f"  全部完成 → {entry['tutorial_path']}（v{entry.get('version', 0)}）")
            return "done"
        with runtime.GLOBAL_STATE_LOCK:
            entry["abandoned"] = abandoned
            entry["phase"] = "workers"
            entry["last_error"] = f"{len(remaining)} 篇未完成: {remaining[:5]}"
            st.save_state(state)
        cfg.log(f"  仍有 {len(remaining)} 篇未完成：{remaining[:5]}")
        return "incomplete"
    except RepoConfigError as e:
        return _mark_blocked(entry, repo_key(repo), e, generated_at, state)
    except QuotaExhaustedError as e:
        cfg.log(f"  额度耗尽（{repo_key(repo)}）：{str(e)[:200]}；保存进度")
        with runtime.GLOBAL_STATE_LOCK:
            entry["last_error"] = f"quota: {str(e)[:300]}"
            entry["last_run_at"] = generated_at
            st.save_state(state)
        return "incomplete"
    except Exception as e:  # noqa: BLE001
        # 仓库被删/改名、分支不存在、凭据失效等 git 永久错误同样按配置错误处理，
        # 否则它们会伪装成「配额不足」，永远占着槽位。
        if gitutil.is_permanent_error(e):
            return _mark_blocked(entry, repo_key(repo), e, generated_at, state)
        cfg.log(f"  ERROR {repo_key(repo)}: {type(e).__name__}: {e}")
        with runtime.GLOBAL_STATE_LOCK:
            entry["last_error"] = f"{type(e).__name__}: {str(e)[:300]}"
            entry["last_run_at"] = generated_at
            st.save_state(state)
        return "incomplete"


def _safe_process_repo(repo: Repo, state: dict, generated_at: str,
                       run_start: float, deadline_min: float, mode: str) -> str:
    """process_repo 的包装：运行前先检查全局停止与软截止，满足条件则提前退出。

    兜底机制：任何从 process_repo 逃逸的 Exception（含 429 处理期间 save_state
    失败、误分类的 429 等）均被捕获为 incomplete → exit 75，不会置 had_error → exit 1。
    """
    if runtime.GLOBAL_STOP.is_set():
        return "incomplete"
    if scheduler.deadline_reached(run_start, deadline_min):
        runtime.GLOBAL_STOP.set()
        return "incomplete"
    try:
        return process_repo(repo, state, generated_at, mode)
    except RepoConfigError as e:
        # process_repo 内部已处理；这里是兜底（例如未来新增的早退路径抛出的配置错误）
        return _mark_blocked(st.get_entry(state, repo_key(repo)), repo_key(repo),
                             e, generated_at, state)
    except QuotaExhaustedError:
        # 退避已耗尽：仅暂停当前仓库，不设置全局停止
        entry = st.get_entry(state, repo_key(repo))
        with runtime.GLOBAL_STATE_LOCK:
            entry["last_error"] = "quota: exhausted after retries"
            entry["last_run_at"] = generated_at
            st._safe_save(state, f"quota@{repo_key(repo)}")
        return "incomplete"
    except Exception as e:  # noqa: BLE001
        # 其他未预期异常均由安全网捕获，不会置 had_error
        cfg.log(f"  WARN [{repo_key(repo)}] _safe_process_repo 捕获：{type(e).__name__}: {e}")
        entry = st.get_entry(state, repo_key(repo))
        with runtime.GLOBAL_STATE_LOCK:
            entry["last_error"] = f"safeguard: {type(e).__name__}: {str(e)[:300]}"
            st._safe_save(state, f"safeguard@{repo_key(repo)}")
        return "incomplete"


def _refill_count(active: int, pending: int, concurrency: int,
                  incomplete: bool) -> int:
    """跨仓库调度的惰性回填策略：本批有仓库交还槽位时，计算应补入的新仓库数。

    规则（将任意 429/额度耗尽统一视为「本仓库本轮超时退出」）：
      - incomplete=True（429/失败/额度暂停）→ 0。本槽位留空（欠位），不立即拉取新任务，
        避免将新任务投入尚未冷却的限流窗口。
      - 正常完成（done/skipped）→ 一次补满至 concurrency 上限。若此前存在欠位，
        本次顺带全部补齐（单欠位即「补 2」）；无欠位则退化为「补 1」。
      - 全部仓库均 429 退出、无正常完成 → 永不触发正常完成分支，自然排空至 exit 75。

    active 为去掉刚交还槽位后的在飞数；pending 为尚未启动的队列长度。
    """
    if incomplete:
        return 0
    return max(0, min(concurrency - active, pending))


def _run_phase(batch: list[Repo], label: str, state: dict, generated_at: str,
               run_start: float, deadline_min: float, mode: str,
               process=None) -> tuple[bool, bool]:
    """动态拉取调度：跑完一批仓库，返回 (any_incomplete, had_error)。

    启动首批 min(CONCURRENCY, len(batch)) 个仓库，用 wait(FIRST_COMPLETED) 逐结果收集：
      - done / skipped：正常完成 → 补满至 CONCURRENCY（顺带回填此前欠位）；
      - blocked：永久性配置错误 → 同样**立即补位**，它没有理由占用槽位；
      - incomplete：429/额度/超时等瞬时失败 → 欠位不补，避免把新任务砸进未冷却的
        限流窗口（全部 incomplete 则自然排空到 exit 75）。
    死区 / 软截止触发 GLOBAL_STOP 时退出整批。

    process 可注入（测试用），默认 _safe_process_repo。
    """
    process = process or _safe_process_repo
    if not batch:
        return False, False
    if runtime.GLOBAL_STOP.is_set():
        return True, False
    cfg.log(f"=== {label}（{len(batch)} 个仓库，并行上限 {cfg.CONCURRENCY}）===")
    pending = deque(batch)
    futs: dict = {}  # Future → Repo
    any_incomplete = False
    had_error = False

    with ThreadPoolExecutor(max_workers=cfg.CONCURRENCY) as ex:
        # 启动首批
        for _ in range(min(cfg.CONCURRENCY, len(pending))):
            r = pending.popleft()
            futs[ex.submit(process, r, state, generated_at,
                           run_start, deadline_min, mode)] = r

        while futs:
            done, _ = wait(futs, return_when=FIRST_COMPLETED)
            for f in done:
                repo = futs.pop(f)
                try:
                    outcome = f.result()
                except Exception as e:  # noqa: BLE001
                    # 未预期错误，未被 _safe_process_repo 安全网捕获
                    had_error = True
                    cfg.log(f"  ERROR [{repo_key(repo)}] uncaught: {e}")
                    continue   # 未预期异常不拉取新任务

                if outcome == "blocked":
                    any_incomplete = True
                    refill = True      # 配置错误立即补位，绝不阻塞队列
                elif outcome == "incomplete":
                    any_incomplete = True
                    refill = False     # 限流/额度 → 欠位，等下次正常完成时一并补齐
                else:
                    refill = True      # done / skipped
                if refill:
                    for _ in range(_refill_count(len(futs), len(pending),
                                                 cfg.CONCURRENCY, incomplete=False)):
                        next_r = pending.popleft()
                        futs[ex.submit(process, next_r, state,
                                       generated_at, run_start, deadline_min, mode)] = next_r

                cfg.log(f"  [{repo_key(repo)}] → {outcome}")

                if runtime.GLOBAL_STOP.is_set():
                    any_incomplete = True
                    break
            if runtime.GLOBAL_STOP.is_set():
                # 仍在执行的 futures 会在 _safe_process_repo 入口提前退出
                break
    return any_incomplete, had_error


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    """整个管线的入口。退出码：0=全部完成，75=未完成（进度已存盘），1=意外错误。"""
    runtime.GLOBAL_STOP.clear()          # 每次进入 main 均复位，selftest 重复调用也安全
    generated_at = scheduler._now().strftime("%Y-%m-%dT%H:%M:%SZ")
    mode = (os.environ.get("RC_MODE") or "auto").strip().lower()
    if mode in ("clone", "major", "rebuild"):
        mode = "full"

    if scheduler.in_dead_zone() and not cfg.env_truthy("RC_ALLOW_DEADZONE") and not cfg.DEBUG:
        cfg.log(f"处于死区 UTC {cfg.DEAD_START:g}-{cfg.DEAD_END:g}（北京 14-18），本次跳过")
        return 0

    if cfg.DEBUG:
        cfg.log("=== DEBUG 模式：固定单仓库 + 只生成 1 篇讲义 + 忽略死区"
                + ("（进度写入 .debug/ 沙箱）" if cfg._DEBUG_SANDBOX else "（沿用正式进度）") + " ===")

    repos = load_repos()
    if not repos:
        cfg.log("没有可处理的项目，退出")
        return 0
    if cfg.DEBUG and len(repos) > 1:
        cfg.log(f"DEBUG：固定仓库 {repos[0].name}（忽略其余 {len(repos) - 1} 个）")
        repos = [repos[0]]

    state = st.load_state()
    any_incomplete = False
    had_error = False

    # 两遍调度仅在 auto 增量模式下启用：第一遍先完成所有未完成仓库；仅当第一遍
    # 全部 done 后，才进入第二遍，复查已完成仓库是否有上游或提示词更新。
    # DEBUG、RC_FORCE_FULL 或 mode=full 仅运行单遍。
    two_pass = not (cfg.DEBUG or cfg.env_truthy("RC_FORCE_FULL") or mode == "full")

    def is_done(name: str) -> bool:
        return st.get_entry(state, name).get("phase") == "done"

    phase1 = list(repos)
    phase2: list[Repo] = []
    if two_pass:
        phase1 = [r for r in repos if not is_done(repo_key(r))]
        phase2 = [r for r in repos if is_done(repo_key(r))]

    # 熔断中的仓库（连续配置错误）本轮不再占用槽位；冷却到期会自动放回队列。
    # 手动全量（RC_FORCE_FULL / mode=full）视为显式重试意图，忽略冷却。
    if two_pass:
        cooling = [r for r in phase1
                   if _disabled_for_now(st.get_entry(state, repo_key(r)), scheduler._now())]
        if cooling:
            cooling_keys = {repo_key(r) for r in cooling}
            phase1 = [r for r in phase1 if repo_key(r) not in cooling_keys]
            cfg.log(f"熔断冷却中，本轮跳过 {len(cooling)} 个配置错误仓库"
                    f"（{cfg.BLOCK_RETRY_HOURS:g}h 后自动重试）：{[repo_key(r) for r in cooling][:5]}")

    # 软截止由 RC_RUN_DEADLINE_MIN 控制；同时同步到 runtime，供 provider 的
    # 429 退避感知。
    run_start = time.monotonic()
    deadline_min = float(os.environ.get("RC_RUN_DEADLINE_MIN", "0") or "0")
    runtime.RUN_START_MONO = run_start
    runtime.RUN_DEADLINE_MIN = deadline_min

    inc, err = _run_phase(phase1, "Phase 1：未完成仓库", state, generated_at,
                          run_start, deadline_min, mode)
    any_incomplete = any_incomplete or inc
    had_error = had_error or err

    # Phase 2 的门槛：仅当第一遍全部 done、GLOBAL_STOP 未被触发且两遍条件仍然
    # 成立时进入。not any_incomplete 已蕴含第一遍全部 done（done/skipped 均要求
    # phase 为 done）。
    if two_pass and phase2 and not any_incomplete and not runtime.GLOBAL_STOP.is_set():
        inc, err = _run_phase(phase2, "Phase 2：复查已完成仓库更新", state, generated_at,
                              run_start, deadline_min, mode)
        any_incomplete = any_incomplete or inc
        had_error = had_error or err

    # 熔断摘要：这些仓库不会让 run 变红，但必须让人看见——否则「绿着空转」会重演。
    fleet_keys = {repo_key(r) for r in repos}
    disabled = sorted(k for k, e in state.get("repos", {}).items()
                      if e.get("disabled") and k in fleet_keys)
    if disabled:
        any_incomplete = True
        cfg.log(f"[WARN] {len(disabled)} 个仓库因配置错误被摘出队列"
                f"（{cfg.BLOCK_RETRY_HOURS:g}h 后自动重试）：{disabled[:5]}")
        cfg.annotation("warning", f"{len(disabled)} 个仓库配置错误已跳过（修 repos.yml）: "
                                  f"{', '.join(disabled[:5])}")

    with runtime.GLOBAL_STATE_LOCK:
        st._safe_save(state, "final")
    if had_error:
        return 1
    if any_incomplete:
        cfg.log("=== 本轮存在未完成项（进度已保存，下次窗口自动续传）===")
        return 75
    cfg.log("=== 全部完成 ===")
    return 0
