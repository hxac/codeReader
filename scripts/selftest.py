#!/usr/bin/env python3
"""离线端到端的自测，改完代码先跑它，不调 claude、不烧一分钱额度。

做法是用一份本地 git 夹具当被分析的项目，再打开 RC_MOCK 让 analyze 用桩替代真正的
claude 调用。这样整条管线几秒就能跑完，反复验证那些最容易回归的几条不变量：

  1. full 首次生成：两篇讲义都 done，phase 落到 done，退出 0，planner 只调一次。
  2. 同一个 HEAD 续传：planner 不该再被调，直接跳过，退出 0。这条专门盯修复 #1。
  3. 上游有了新提交：走 incremental，planner 重跑一遍，标 keep 的讲义要保留，退出 0。
  4. 断点续传：手动把某一篇打回 pending、phase 掰回 workers，应当只补这一篇、不碰
     planner，跑完回到 done。

后面还跟了几个补充场景：提示词变更触发全量重构、定时运行忽略手动全量开关、worker
超时跨下一篇，再加一批纯函数的单测。

跑法：uv run --no-project --with pyyaml --with jinja2 python scripts/selftest.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Windows 控制台默认按 GBK 解码，遇到 emoji 和中文会崩在打印上，这里强行切 UTF-8。
# CI 那边本来就是 UTF-8，这一步等于空操作。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# scripts 目录，用来 import 同目录下的 analyze。
SCRIPTS = Path(__file__).resolve().parent

# 搭一个一次性的控制环境：所有路径都指到系统临时目录下的一个新文件夹，跑完就删，
# 绝不碰仓库里的正式 state 和 tutorials。
#   ctrl      扮演控制仓，放 repos_state.json 和生成的 tutorials
#   work      analyze clone 夹具仓时用的工作区
#   fixture   一个本地 git 仓，假装成被分析的「上游项目」
TMP = Path(tempfile.mkdtemp(prefix="readcode_selftest_"))
CTRL = TMP / "ctrl"; CTRL.mkdir()
WORK = TMP / "work"; WORK.mkdir()
STATE = CTRL / "repos_state.json"
TUTORIALS = CTRL / "tutorials"
FIX = TMP / "fixture"; FIX.mkdir()
# 夹具仓里随便放个文件，好让 git 有东西可提交。
(FIX / "README.md").write_text("# fixture project\n", encoding="utf-8")


def git(cwd: Path, *args: str) -> str:
    """在 cwd 下跑一条 git 命令，返回 stdout。失败就直接抛错——自测里 git 出错没有继续的意义。

    顺手把作者和提交者名字都钉成占位值，免得这些提交用到本机的 git 全局配置。
    """
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {args} 失败：{r.stderr.strip()}")
    return r.stdout.strip()


# 夹具仓初始化：提交一次，记下这第一个 HEAD。Run 3 要靠它判断「上游是不是变了」。
git(FIX, "init", "-q")
git(FIX, "add", "-A")
git(FIX, "commit", "-qm", "init")
HEAD1 = git(FIX, "rev-parse", "HEAD")

# 给控制仓写一份 repos.yml，只列夹具这一个项目。url 用 file:// 指向本地夹具仓，
# 这样 analyze 的 clone 走本地文件，不联网。
(CTRL / "repos.yml").write_text(
    "repos:\n  - name: test/fixture\n"
    f"    url: file://{FIX.as_posix()}\n    project: Fixture\n    focus: test\n",
    encoding="utf-8")

# 重点：这些环境变量必须在 import analyze 之前就位。路径和 MOCK 标志是在 analyze 模块
# 导入的那一刻就读取、冻结成模块级常量的，导入之后再设就不管用了。
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

# 把 scripts 加进 import 路径，好直接 import 同目录的 analyze，不用装包。
sys.path.insert(0, str(SCRIPTS))
import analyze  # noqa: E402

# 收集所有失败的断言，最后统一报。哪条挂了就记哪条，不立刻退出，尽量一次跑完看全貌。
failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    """一条断言：条件成立打 OK，不成立打 FAIL 并记下来。"""
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


# 夹具项目的讲义最终落到这个目录，后面反复检查这里的文件。
TUTDIR = TUTORIALS / "test" / "fixture" / "Fixture-tutorial"


def state_entry() -> dict:
    """重新从磁盘读 state，取夹具项目那一条记录。每次都现读，拿到的就是最新写入。"""
    return json.loads(STATE.read_text(encoding="utf-8"))["repos"]["test/fixture"]


# ---- Run 1：full 首次生成 ----
# 第一次跑，强制 full。要看到的是：两篇讲义都生成、状态都 done、phase 推进到 done、
# planner 整个 Run 只被调一次、退出码 0，顺带摘要 sidecar 也跟着出来了。
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
check((TUTDIR / "u1-l1.summary.md").exists(), "u1-l1.summary.md 生成")
check((TUTDIR / "u1-l2.summary.md").exists(), "u1-l2.summary.md 生成")

# ---- Run 2：同一个 HEAD 续传，盯修复 #1 ----
# HEAD 没变、也没强制，按理 analyze 应当一眼看出「没活干」，planner 根本不该被调。
# 早先有个 bug 是这里仍会重跑 planner、白烧一次调用，这条就是把它钉死。
print("\n=== Run 2: resume（同 HEAD，无 force）===")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
analyze.MOCK_PLANNER_CALLS = 0
rc2 = analyze.main()
check(rc2 == 0, f"退出码 0（got {rc2}）")
check(analyze.MOCK_PLANNER_CALLS == 0,
      f"续传不再调 planner（got {analyze.MOCK_PLANNER_CALLS}）—— 修复 #1")
check(state_entry()["phase"] == "done", "仍为 done")

# ---- Run 3：上游有了新提交，走 incremental ----
# 给夹具仓加一个新提交让 HEAD 变化。analyze 应当据此重跑 planner，但标 keep 的讲义
# 要原样保留、不能被清掉，最后 phase 仍回到 done。
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

# ---- Run 4：断点续传，只补一篇 ----
# 模拟「上一轮没跑完」：手动把 u1-l1 打回 pending、phase 掰回 workers，其余不动。
# 正确行为是只补 u1-l1 这一篇、完全不碰 planner，跑完 phase 回到 done。
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
# 摘要这一段：u1-l2 的 sidecar 在 Run 1 就建好了，这次只重新生成了 u1-l1，
# 所以 u1-l2 那份应当原样复用、不会被重新摘要。
check((TUTDIR / "u1-l2.summary.md").exists(),
      "u1-l2.summary.md 复用（未重新摘要）")

# ---- 检查 prev_head 有没有持久化，盯修复 #2 ----
# worker 算 diff 得知道「上次的 HEAD」。这个值必须落盘，否则续传时就丢了基准。
# 这里验它确实存成了 Run 3 之前的那个旧 HEAD。
print("\n=== 检查：prev_head 持久化（修复 #2）===")
e = state_entry()
check(e.get("prev_head") == HEAD1, f"prev_head 持久化为旧 HEAD（got {e.get('prev_head')})")

# ---- Run 4.5：提示词变更触发全量重构 ----
# 一条重要不变量：auto 和定时运行，只有在「提示词被改过」或「第一次建」时才走全量。
# 这里不动真正的提示词文件，而是把 state 里的 prompt_hash 篡改成一个假值，骗过签名检查，
# 让 analyze 以为提示词变了。HEAD 保持不变、也不强制，看它会不会因此触发全量重构。
print("\n=== Run 4.5: 提示词变更 → 全量重构（auto，head 不变）===")
st = json.loads(STATE.read_text(encoding="utf-8"))
sig_now = st["repos"]["test/fixture"].get("prompt_hash")   # 当前真实签名，提示词其实没动
st["repos"]["test/fixture"]["prompt_hash"] = "deadbeefdeadbeef"   # 签名不匹配 = 假装提示词被改
STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
os.environ.pop("RC_EVENT_NAME", None)   # 确保不是定时运行
analyze.MOCK_PLANNER_CALLS = 0
rc45 = analyze.main()
e = state_entry()
check(rc45 == 0, f"退出码 0（got {rc45}）")
check(analyze.MOCK_PLANNER_CALLS == 1,
      f"提示词变更→planner 重跑 1 次（got {analyze.MOCK_PLANNER_CALLS}）")
check(e["version"] == 2, f"全量重构→version 递增到 2（got {e['version']}）")  # 只有 plan_mode==full 在 done 时才 bump
check(e["prompt_hash"] == sig_now, "重构后 prompt_hash 更新为当前签名")
check(e["phase"] == "done", "phase=done")

# ---- Run 4.6：定时运行必须忽略手动全量开关 ----
# 另一条不变量：定时运行结构性忽略 RC_FORCE_FULL 和 RC_MODE=full，就算 workflow 误设了也绝不全量。
# 这里故意把两者都设上、再标成定时，提示词和 HEAD 却都没变，应当直接 skip、不重跑 planner。
print("\n=== Run 4.6: 定时忽略手动全量开关（RC_FORCE_FULL 被忽略）===")
os.environ["RC_EVENT_NAME"] = "schedule"
os.environ["RC_FORCE_FULL"] = "true"; os.environ["RC_MODE"] = "full"
analyze.MOCK_PLANNER_CALLS = 0
rc46 = analyze.main()
e = state_entry()
check(rc46 == 0, f"退出码 0（got {rc46}）")
check(analyze.MOCK_PLANNER_CALLS == 0,
      f"定时忽略 RC_FORCE_FULL/RC_MODE=full→不重跑 planner（got {analyze.MOCK_PLANNER_CALLS}）")
check(e["version"] == 2, f"version 不变（未全量重构，got {e['version']}）")
check(e["phase"] == "done", "phase 仍 done")
os.environ.pop("RC_EVENT_NAME", None)   # 复位，别污染后面的 Run
os.environ["RC_FORCE_FULL"] = "false"; os.environ["RC_MODE"] = "auto"

# ---- Run 4.7：worker 超时要跨到下一篇，而不是停掉整轮 ----
# 这条对照「额度耗尽就停整轮」：单篇超时只算这一篇失败、retries 加一、跳到下一篇，
# 既不会 in-run 重试去白白多烧一个 RC_TIMEOUT，也不会把整轮停掉。
# 用 RC_MOCK_TIMEOUT_ON 让 u1-l1 这次必超时。INRUN_RETRIES 默认是 1，要是误走了 in-run 重试，
# worker 调用次数会变成 2 而不是 1，这条断言就能把它抓出来。
print("\n=== Run 4.7: 超时跨下一篇（RC_MOCK_TIMEOUT_ON=u1-l1）===")
st = json.loads(STATE.read_text(encoding="utf-8"))
st["repos"]["test/fixture"]["lectures"]["u1-l1"]["status"] = "pending"
st["repos"]["test/fixture"]["lectures"]["u1-l1"]["retries"] = 0
st["repos"]["test/fixture"]["phase"] = "workers"
STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
os.environ["RC_MOCK_TIMEOUT_ON"] = "u1-l1"
analyze.MOCK_PLANNER_CALLS = 0; analyze.MOCK_WORKER_CALLS = 0
rc47 = analyze.main()
e = state_entry()
check(rc47 == 75, f"超时一篇未完成→退出 75（got {rc47}）")
check(e["lectures"]["u1-l1"]["status"] == "failed", "超时讲义标 failed")
check(e["lectures"]["u1-l1"]["retries"] == 1, "超时计 retries+=1（本次窗口失败）")
check(e["lectures"]["u1-l2"]["status"] == "done", "下一篇仍 done（跨过去、未受影响）")
check(analyze.MOCK_PLANNER_CALLS == 0, "续传 worker 不调 planner")
check(analyze.MOCK_WORKER_CALLS == 1,
      f"超时不 in-run 重试：worker 仅调 1 次（got {analyze.MOCK_WORKER_CALLS}）")
check(e["phase"] == "workers", "phase 仍 workers（未停本轮、未 done）")
os.environ.pop("RC_MOCK_TIMEOUT_ON", None)

# ---- Run 4.8：子目录拆分（folders）----
# 验证 folders 字段把一个仓库展开成多个头等 fleet 条目：共享一份 clone，每个子目录独立
# 规划/生成，state key 带 @subpath 后缀，worker 写到子目录下（CWD 落子目录），收割到各自目录。
# 向后兼容靠 subpath="" 保住——前面所有场景都用无 folders 的夹具，完全不受影响。
print("\n=== Run 4.8: folders 子目录拆分（共享 clone + 各自独立）===")
CTRL2 = TMP / "ctrl2"; CTRL2.mkdir()
WORK2 = TMP / "work2"; WORK2.mkdir()
STATE2 = CTRL2 / "repos_state.json"
TUTORIALS2 = CTRL2 / "tutorials"
# 第二个夹具仓：含 a/、b/、deep/nested/ 三个子目录，模拟 monorepo 的子项目。
# 配 folders: [a, b, self, deep/nested] 覆盖 self 令牌与多级子目录。
FIX2 = TMP / "fixture2"; FIX2.mkdir()
(FIX2 / "a").mkdir(); (FIX2 / "a" / "x.md").write_text("# a\n", encoding="utf-8")
(FIX2 / "b").mkdir(); (FIX2 / "b" / "y.md").write_text("# b\n", encoding="utf-8")
(FIX2 / "deep").mkdir(); (FIX2 / "deep" / "nested").mkdir()
(FIX2 / "deep" / "nested" / "z.md").write_text("# deep\n", encoding="utf-8")
git(FIX2, "init", "-q"); git(FIX2, "add", "-A"); git(FIX2, "commit", "-qm", "init")
(CTRL2 / "repos.yml").write_text(
    "repos:\n  - name: test/multifix\n"
    f"    url: file://{FIX2.as_posix()}\n    project: MultiFix\n    focus: split\n"
    "    folders: [a, b, self, deep/nested]\n",
    encoding="utf-8")
# 这几个路径是 import 时冻结成模块常量的，这里直接改属性切到独立环境（它们都是调用时读全局）。
_prev_paths = (analyze.REPOS_YML, analyze.STATE_FILE, analyze.WORK_DIR, analyze.TUTORIALS_DIR)
analyze.REPOS_YML = CTRL2 / "repos.yml"
analyze.STATE_FILE = STATE2
analyze.WORK_DIR = WORK2
analyze.TUTORIALS_DIR = TUTORIALS2
os.environ["RC_REPO_NAME"] = ""        # 清掉之前的过滤，让全部 4 个条目都跑
os.environ["RC_MODE"] = "full"; os.environ["RC_FORCE_FULL"] = "true"
os.environ.pop("RC_EVENT_NAME", None)
analyze.MOCK_PLANNER_CALLS = 0
rc48 = analyze.main()
st2 = json.loads(STATE2.read_text(encoding="utf-8"))["repos"]
check(rc48 == 0, f"退出码 0（got {rc48}）")
check(set(st2.keys()) == {"test/multifix@a", "test/multifix@b",
                          "test/multifix", "test/multifix@deep/nested"},
      f"展开成 4 个条目（self key+@subpath key；got {sorted(st2.keys())}）")
for k in ("test/multifix@a", "test/multifix@b", "test/multifix",
          "test/multifix@deep/nested"):
    check(st2[k]["phase"] == "done", f"{k} phase=done")
check(st2["test/multifix@a"]["version"] == 1, "@a version=1")
check(analyze.MOCK_PLANNER_CALLS == 4,
      f"每个子目录 + self 各调一次 planner（got {analyze.MOCK_PLANNER_CALLS}）")
TUT_A = TUTORIALS2 / "test" / "multifix" / "MultiFix-a-tutorial"
TUT_B = TUTORIALS2 / "test" / "multifix" / "MultiFix-b-tutorial"
TUT_SELF = TUTORIALS2 / "test" / "multifix" / "MultiFix-tutorial"
TUT_DN = TUTORIALS2 / "test" / "multifix" / "MultiFix-deep-nested-tutorial"
for k, p in [("@a", TUT_A), ("@b", TUT_B), ("(self)", TUT_SELF),
             ("@deep/nested", TUT_DN)]:
    check((p / "manifest.json").exists(), f"{k} manifest.json 生成")
    check((p / "u1-l1.md").exists(), f"{k} u1-l1.md 生成")
# 共享 clone：work2 下只有一份 test-multifix，不是每个 folder 各 clone 一份。
check((WORK2 / "test-multifix").exists(), "共享 clone 存在")
check(len([p for p in WORK2.iterdir() if p.is_dir()]) == 1,
      "work2 仅一份 clone（folder 共享 clone，未各 clone）")
# CWD 验证：self 在 clone 根，a/b 在一级子目录，deep/nested 在多级子目录。
check((WORK2 / "test-multifix" / "MultiFix-tutorial" / "u1-l1.md").exists(),
      "(self) 讲义写到 clone 根（CWD=clone 根）")
check((WORK2 / "test-multifix" / "a" / "MultiFix-a-tutorial" / "u1-l1.md").exists(),
      "@a 讲义写到 clone 的 a/ 下")
check((WORK2 / "test-multifix" / "b" / "MultiFix-b-tutorial" / "u1-l1.md").exists(),
      "@b 讲义写到 clone 的 b/ 下")
check((WORK2 / "test-multifix" / "deep" / "nested" / "MultiFix-deep-nested-tutorial" / "u1-l1.md").exists(),
      "@deep/nested 讲义写到 clone 的 deep/nested/ 下（CWD 落多级子目录）")
# RC_REPO_NAME 过滤：父名 → 全部 4 个；完整 @key → 单个。直接用 load_repos 验，不跑 main。
analyze.REPOS_YML = CTRL2 / "repos.yml"
os.environ["RC_REPO_NAME"] = "test/multifix"
both = analyze.load_repos()
check(len(both) == 4, f"RC_REPO_NAME=父名 → 选中全部 4 个（got {len(both)}）")
os.environ["RC_REPO_NAME"] = "test/multifix@a"
one = analyze.load_repos()
check(len(one) == 1 and one[0].subpath == "a",
      f"RC_REPO_NAME=@a → 只选 a（got {[r.subpath for r in one]}）")
os.environ["RC_REPO_NAME"] = "test/multifix@deep/nested"
dn = analyze.load_repos()
check(len(dn) == 1 and dn[0].subpath == "deep/nested",
      f"RC_REPO_NAME=@deep/nested → 只选 deep/nested（got {[r.subpath for r in dn]}）")
# 复位模块常量与 env，别污染后面的 Run 5（纯单测，本不依赖它们，复位仅为干净）。
analyze.REPOS_YML, analyze.STATE_FILE, analyze.WORK_DIR, analyze.TUTORIALS_DIR = _prev_paths
os.environ["RC_REPO_NAME"] = "test/fixture"
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"

# ---- Run 5：纯函数单测，全是离线的，不烧额度 ----
print("\n=== Run 5: 单元回归（classify / signature CRLF / deadzone wrap / render 无 None）===")

# --- 5a. _classify_error 分类：表驱动，顺序是承重的 ---
# 关键点：max-turns 和 budget 必须先于 quota 判断，否则 "max turns exceeded" 会被误判成额度耗尽。
# 下面这张表把各类报错文本和期望的异常类型一一对照跑一遍。
CASES = [
    ("Error: max turns reached", analyze.ClaudeRunnerError),
    ("Error: turns limit exceeded", analyze.ClaudeRunnerError),
    ("Error: max budget exceeded", analyze.ClaudeRunnerError),
    ("Error: spending limit hit", analyze.ClaudeRunnerError),
    ("Error: rate_limit_exceeded (429)", analyze.QuotaExhaustedError),
    ("insufficient credit balance", analyze.QuotaExhaustedError),
    ("Error: cost limit exceeded", analyze.QuotaExhaustedError),   # 第三方端点 → 整体停，非单篇
    ("Error: credit balance too low", analyze.QuotaExhaustedError),
    ("Error: exceeded credit limit", analyze.QuotaExhaustedError),
    ("Random unexpected error text 12345", None),                  # 没匹配上 → 回退成 ClaudeRunnerError
]
for text, expected in CASES:
    got = analyze._classify_error(text)
    check(got is expected,
          f"{text!r} → {expected.__name__ if expected else 'None'}"
          f"（got {got.__name__ if got else 'None'}）")

# --- 5b. signature 对 CRLF 做归一 ---
# Windows 上 autocrlf 可能让本地 checkout 出 CRLF，而 CI 是 LF，签名就会对不上、误触发全量。
# 这里造两个内容相同但换行不同的文件，验签名把它们当成一样。
tmp_prompts = TMP / "test_prompts"; tmp_prompts.mkdir(exist_ok=True)
(tmp_prompts / "a.md").write_bytes(b"hello\nworld\n")
(tmp_prompts / "b.md").write_bytes(b"hello\r\nworld\r\n")
sig_lf = analyze.signature(tmp_prompts)
(tmp_prompts / "b.md").write_bytes(b"hello\nworld\n")
sig_crlf_normalized = analyze.signature(tmp_prompts)
check(sig_lf == sig_crlf_normalized,
      f"signature CRLF normalised: LF={sig_lf} vs CRLF→LF={sig_crlf_normalized}")
shutil.rmtree(tmp_prompts, ignore_errors=True)

# --- 5c. 死区跨午夜的判定 ---
# 当 START 大于 END，死区就是跨午夜的，得按 wrap-around 算。这里临时把死区设成 22 到 2 点，
# 验几个临界时刻，再切回正常区间验一遍不跨午夜的路径。
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
# 不跨午夜的路径
analyze.DEAD_START = 6; analyze.DEAD_END = 10
check(analyze.in_dead_zone(dt_mod(2026,1,1,8,0, tzinfo=tz_mod.utc)) is True,
      "正常 08:00 → True")
check(analyze.in_dead_zone(dt_mod(2026,1,1,5,0, tzinfo=tz_mod.utc)) is False,
      "正常 05:00 → False")
analyze.DEAD_START, analyze.DEAD_END = orig_s, orig_e

# --- 5d. compose_worker_prompt 对缺失字段兜底 ---
# manifest 里有些字段可能缺失，渲染时不能冒出字面 None，得有兜底文案。
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
