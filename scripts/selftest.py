#!/usr/bin/env python3
"""离线端到端自测。

用本地 git 夹具 + RC_MOCK 桩（不调 claude、不烧额度）跑完整管线，验证：
  1) full 首次生成 → 两篇讲义 done、phase=done、退出 0、planner 调 1 次
  2) 同 HEAD 续传 → planner 不再调（修复 #1）、跳过、退出 0
  3) 上游新提交 → incremental：planner 重跑、keep 讲义保留、退出 0
  4) 断点续传（手动置一篇 pending + phase=workers）→ 只补该篇、planner 不调、回到 done

跑法：uv run --no-project --with pyyaml --with jinja2 python scripts/selftest.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Windows 控制台默认 GBK，强制 UTF-8 避免 emoji/中文打印报错（CI 上是 UTF-8，无影响）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

SCRIPTS = Path(__file__).resolve().parent

# ---- 临时控制环境 + 本地 git 夹具 ----
TMP = Path(tempfile.mkdtemp(prefix="readcode_selftest_"))
CTRL = TMP / "ctrl"; CTRL.mkdir()
WORK = TMP / "work"; WORK.mkdir()
STATE = CTRL / "repos_state.json"
TUTORIALS = CTRL / "tutorials"
FIX = TMP / "fixture"; FIX.mkdir()
(FIX / "README.md").write_text("# fixture project\n", encoding="utf-8")


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {args} 失败：{r.stderr.strip()}")
    return r.stdout.strip()


git(FIX, "init", "-q")
git(FIX, "add", "-A")
git(FIX, "commit", "-qm", "init")
HEAD1 = git(FIX, "rev-parse", "HEAD")

(CTRL / "repos.yml").write_text(
    "repos:\n  - name: test/fixture\n"
    f"    url: file://{FIX.as_posix()}\n    project: Fixture\n    focus: test\n",
    encoding="utf-8")

# 必须在 import analyze 前设好（路径/MOCK 在导入期读取）
os.environ.update({
    "RC_REPOS_YML": str(CTRL / "repos.yml"),
    "RC_STATE_FILE": str(STATE),
    "RC_WORK_DIR": str(WORK),
    "RC_TUTORIALS_DIR": str(TUTORIALS),
    "RC_MOCK": "1",
    "RC_CONCURRENCY": "2",
    "RC_REPO_NAME": "test/fixture",
    "RC_MAX_RETRIES": "5",
    "RC_ALLOW_DEADZONE": "1",
})

sys.path.insert(0, str(SCRIPTS))
import analyze  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


TUTDIR = TUTORIALS / "test" / "fixture" / "Fixture-tutorial"


def state_entry() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8"))["repos"]["test/fixture"]


# ---- Run 1：full 首次 ----
print("\n=== Run 1: full（首次生成）===")
os.environ["RC_MODE"] = "full"; os.environ["RC_FORCE_FULL"] = "true"
analyze.MOCK_PLANNER_CALLS = 0
rc1 = analyze.main()
e = state_entry()
check(rc1 == 0, f"退出码 0（got {rc1}）")
check(e["phase"] == "done", "phase=done")
check(e["version"] == 1, "version=1")
check((TUTDIR / "manifest.json").exists(), "manifest.json 生成")
check((TUTDIR / "u1-l1.md").exists(), "u1-l1.md 生成")
check((TUTDIR / "u1-l2.md").exists(), "u1-l2.md 生成")
check(e["lectures"]["u1-l1"]["status"] == "done", "u1-l1 status=done")
check(e["lectures"]["u1-l2"]["status"] == "done", "u1-l2 status=done")
check(analyze.MOCK_PLANNER_CALLS == 1, f"planner 调用 1 次（got {analyze.MOCK_PLANNER_CALLS}）")

# ---- Run 2：同 HEAD 续传（修复 #1：不应再调 planner）----
print("\n=== Run 2: resume（同 HEAD，无 force）===")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
analyze.MOCK_PLANNER_CALLS = 0
rc2 = analyze.main()
check(rc2 == 0, f"退出码 0（got {rc2}）")
check(analyze.MOCK_PLANNER_CALLS == 0,
      f"续传不再调 planner（got {analyze.MOCK_PLANNER_CALLS}）—— 修复 #1")
check(state_entry()["phase"] == "done", "仍为 done")

# ---- Run 3：上游新提交 → incremental ----
print("\n=== Run 3: incremental（上游有新提交）===")
(FIX / "new.txt").write_text("change\n", encoding="utf-8")
git(FIX, "add", "-A"); git(FIX, "commit", "-qm", "change")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
analyze.MOCK_PLANNER_CALLS = 0
rc3 = analyze.main()
e = state_entry()
check(rc3 == 0, f"退出码 0（got {rc3}）")
check(analyze.MOCK_PLANNER_CALLS == 1,
      f"HEAD 变化→planner 重跑 1 次（got {analyze.MOCK_PLANNER_CALLS}）")
check(e["phase"] == "done", "incremental 后 phase=done")
check((TUTDIR / "u1-l1.md").exists(), "u1-l1.md（update）仍在")
check((TUTDIR / "u1-l2.md").exists(), "u1-l2.md（keep）保留")
check(e["lectures"]["u1-l2"]["status"] in ("done", "keep"), "u1-l2 为 keep/done")

# ---- Run 4：断点续传（手动置一篇 pending + phase=workers）----
print("\n=== Run 4: 断点续传（一篇 pending，同 HEAD，无 force）===")
st = json.loads(STATE.read_text(encoding="utf-8"))
st["repos"]["test/fixture"]["lectures"]["u1-l1"]["status"] = "pending"
st["repos"]["test/fixture"]["phase"] = "workers"
STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
analyze.MOCK_PLANNER_CALLS = 0
rc4 = analyze.main()
e = state_entry()
check(rc4 == 0, f"退出码 0（got {rc4}）")
check(analyze.MOCK_PLANNER_CALLS == 0,
      f"续传 worker 不调 planner（got {analyze.MOCK_PLANNER_CALLS}）")
check(e["lectures"]["u1-l1"]["status"] == "done", "pending 讲义被补完")
check(e["phase"] == "done", "phase 回到 done")

# ---- 验证 prev_head 持久化（修复 #2）----
print("\n=== 检查：prev_head 持久化（修复 #2）===")
e = state_entry()
check(e.get("prev_head") == HEAD1, f"prev_head 持久化为旧 HEAD（got {e.get('prev_head')})")

# ---- Run 5：单元回归（离线，不烧额度）----
print("\n=== Run 5: 单元回归（classify / signature CRLF / deadzone wrap / render 无 None）===")

# --- 5a. _classify_error 分类 ---
import claude_runner  # noqa: E402 (alread imported by analyze above)

cls = claude_runner._classify_error
check(cls("Error: max turns reached") is claude_runner.ClaudeRunnerError,
      "max turns reached → ClaudeRunnerError")
check(cls("Error: turns limit exceeded") is claude_runner.ClaudeRunnerError,
      "turns limit exceeded → ClaudeRunnerError")
check(cls("Error: max budget exceeded") is claude_runner.ClaudeRunnerError,
      "max budget exceeded → ClaudeRunnerError")
check(cls("Error: spending limit hit") is claude_runner.ClaudeRunnerError,
      "spend limit hit → ClaudeRunnerError")
check(cls("Error: rate_limit_exceeded (429)") is claude_runner.QuotaExhaustedError,
      "rate_limit 429 → QuotaExhaustedError")
check(cls("insufficient credit balance") is claude_runner.QuotaExhaustedError,
      "insufficient credit → QuotaExhaustedError")
check(cls("Error: cost limit exceeded") is claude_runner.QuotaExhaustedError,
      "cost limit exceeded (第三方端点) → QuotaExhausted（整体停，非单篇）")
check(cls("Error: credit balance too low") is claude_runner.QuotaExhaustedError,
      "credit balance → QuotaExhaustedError")
check(cls("Error: exceeded credit limit") is claude_runner.QuotaExhaustedError,
      "exceeded credit limit → QuotaExhaustedError")
check(cls("Random unexpected error text 12345") is None,
      "未匹配文本 → None（走 fallback ClaudeRunnerError）")

# --- 5b. signature CRLF normalize ---
import promptkit  # noqa: E402
tmp_prompts = TMP / "test_prompts"; tmp_prompts.mkdir(exist_ok=True)
(tmp_prompts / "a.md").write_bytes(b"hello\nworld\n")
(tmp_prompts / "b.md").write_bytes(b"hello\r\nworld\r\n")
sig_lf = promptkit.signature(tmp_prompts)
(tmp_prompts / "b.md").write_bytes(b"hello\nworld\n")
sig_crlf_normalized = promptkit.signature(tmp_prompts)
check(sig_lf == sig_crlf_normalized,
      f"signature CRLF normalised: LF={sig_lf} vs CRLF→LF={sig_crlf_normalized}")
shutil.rmtree(tmp_prompts, ignore_errors=True)

# --- 5c. deadzone wrap-around ---
from datetime import datetime as dt_mod, timezone as tz_mod  # noqa: E402
orig_s, orig_e = analyze.DEAD_START, analyze.DEAD_END
analyze.DEAD_START = 22; analyze.DEAD_END = 2
check(analyze.in_dead_zone(dt_mod(2026,1,1,23,0, tzinfo=tz_mod.utc)) is True,
      "跨午夜 23:00 → True")
check(analyze.in_dead_zone(dt_mod(2026,1,1,0,30, tzinfo=tz_mod.utc)) is True,
      "跨午夜 00:30 → True")
check(analyze.in_dead_zone(dt_mod(2026,1,1,5,0, tzinfo=tz_mod.utc)) is False,
      "跨午夜 05:00 → False")
check(analyze.in_dead_zone(dt_mod(2026,1,1,12,0, tzinfo=tz_mod.utc)) is False,
      "跨午夜 12:00 → False")
# no-wrap 路径
analyze.DEAD_START = 6; analyze.DEAD_END = 10
check(analyze.in_dead_zone(dt_mod(2026,1,1,8,0, tzinfo=tz_mod.utc)) is True,
      "正常 08:00 → True")
check(analyze.in_dead_zone(dt_mod(2026,1,1,5,0, tzinfo=tz_mod.utc)) is False,
      "正常 05:00 → False")
analyze.DEAD_START, analyze.DEAD_END = orig_s, orig_e

# --- 5d. compose_worker_prompt 对 None 兜底 ---
lec_none = {"id": "u1-l9", "title": "测试", "topic": "主题", "filename": "u1-l9.md"}
prompt = analyze.compose_worker_prompt(
    analyze.Repo(name="test/x", url="", branch=None, project="X", focus=""),
    lec_none, "new", "HEAD", None, "X-tutorial", "https://example.com/base/")
check("None" not in prompt and "自动判断" in prompt,
      "level=None 时渲染含兜底文案，无字面 None")
check("由 worker 根据主题和源码自行设计" in prompt,
      "practice_task=None 时渲染含兜底文案")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("SELFTEST PASSED ✅" if not failures
             else f"SELFTEST FAILED ❌ ({len(failures)}): " + "; ".join(failures)))
sys.exit(1 if failures else 0)
