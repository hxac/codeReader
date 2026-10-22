#!/usr/bin/env python3
"""离线端到端自测：修改代码后应先运行本测试，不调用 agent、不消耗额度。

做法：以本地 git 夹具仓库作为被分析项目，设置 RC_MOCK 使管线以桩替代真实的
agent 调用。整条管线数秒内即可运行完毕，用于反复验证最易回归的不变量：

  1. full 首次生成：两篇讲义均 done，phase 落至 done，退出 0，planner 仅调用一次。
  2. 同一 HEAD 续传：planner 不应再被调用，直接跳过，退出 0（修复 #1）。
  3. 上游新增提交：走 incremental，planner 重跑一次，标 keep 的讲义保留，退出 0。
  4. 断点续传：手动将某一篇改为 pending、phase 改回 workers，应只补这一篇、
     不调用 planner，运行后回到 done。

其后为补充场景：提示词变更触发全量重构、定时运行忽略手动全量开关、worker 超时
跨至下一篇，以及一批纯函数单测。

运行方式：uv run python -m tests.selftest
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime as dt_mod, timedelta as td_mod, timezone as tz_mod
from pathlib import Path

# Windows 控制台默认按 GBK 解码，打印 emoji/中文时会报错；切换到 UTF-8。
# CI 环境本就是 UTF-8，此步骤为空操作。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# 搭建一次性控制环境：所有路径均指向系统临时目录下的新文件夹，运行结束即删除，
# 绝不触碰仓库中的正式 state 与 tutorials。
#   ctrl      扮演控制仓库，存放 repos_state.json 与生成的 tutorials
#   work      analyze clone 夹具仓库时使用的工作区
#   fixture   本地 git 仓库，模拟被分析的「上游项目」
TMP = Path(tempfile.mkdtemp(prefix="readcode_selftest_"))
CTRL = TMP / "ctrl"; CTRL.mkdir()
WORK = TMP / "work"; WORK.mkdir()
STATE = CTRL / "repos_state.json"
TUTORIALS = CTRL / "tutorials"
FIX = TMP / "fixture"; FIX.mkdir()
# 夹具仓库中放置一个文件，使 git 有内容可提交。
(FIX / "README.md").write_text("# fixture project\n", encoding="utf-8")


def git(cwd: Path, *args: str) -> str:
    """在 cwd 下执行一条 git 命令并返回 stdout；失败时直接抛错。

    自测中 git 出错后继续无意义。同时将作者与提交者固定为占位值，避免提交
    使用本机的 git 全局配置。
    """
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env=env, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git {args} 失败：{r.stderr.strip()}")
    return r.stdout.strip()


# 夹具仓库初始化：提交一次并记录首个 HEAD。Run 3 依赖它判断「上游是否变化」。
git(FIX, "init", "-q")
git(FIX, "add", "-A")
git(FIX, "commit", "-qm", "init")
HEAD1 = git(FIX, "rev-parse", "HEAD")

# 为控制仓库写入 repos.yml，仅列出夹具项目。url 使用 file:// 指向本地夹具仓库，
# 使 analyze 的 clone 走本地文件，不联网。
(CTRL / "repos.yml").write_text(
    "repos:\n  - name: test/fixture\n"
    f"    url: file://{FIX.as_posix()}\n    project: Fixture\n    focus: test\n",
    encoding="utf-8")

# 关键：这些环境变量必须在 import codereader 之前设置。路径与 MOCK 标志在 config
# 模块导入时即被读取并固化为模块级常量，导入后再设置无效。
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

from codereader import (  # noqa: E402
    config as cfg,
    gitutil,
    pipeline,
    prompts,
    provider,
    repo as repo_mod,
    runtime,
    scheduler,
    state as state_mod,
)

# 收集所有失败的断言，最后统一报告。每条失败均记录但不立即退出，以便一次运行查看全貌。
failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    """一条断言：条件成立输出 OK，否则输出 FAIL 并记录。"""
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


# 夹具项目的讲义最终落于此目录，后续反复检查其中的文件。
TUTDIR = TUTORIALS / "test" / "fixture" / "Fixture-tutorial"


def state_entry() -> dict:
    """重新从磁盘读取 state，取夹具项目对应的记录。每次现读，获取最新写入。"""
    return json.loads(STATE.read_text(encoding="utf-8"))["repos"]["test/fixture"]


# ---- Run 1：full 首次生成 ----
# 第一次运行，强制 full。预期：两篇讲义均生成、状态均 done、phase 推进至 done、
# planner 整个 Run 仅调用一次、退出码 0，摘要 sidecar 同步生成。
print("\n=== Run 1: full（首次生成）===")
os.environ["RC_MODE"] = "full"; os.environ["RC_FORCE_FULL"] = "true"
provider.MOCK_PLANNER_CALLS = 0
rc1 = pipeline.main()
e = state_entry()
check(rc1 == 0, f"退出码 0（got {rc1}）")
check(e["phase"] == "done", "phase=done")
check(e["version"] == 1, "version=1")
check((TUTDIR / "manifest.json").exists(), "manifest.json 生成")
check((TUTDIR / "u1-l1.md").exists(), "u1-l1.md 生成")
check((TUTDIR / "u1-l2.md").exists(), "u1-l2.md 生成")
check(e["lectures"]["u1-l1"]["status"] == "done", "u1-l1 status=done")
check(e["lectures"]["u1-l2"]["status"] == "done", "u1-l2 status=done")
check(provider.MOCK_PLANNER_CALLS == 1, f"planner 调用 1 次（got {provider.MOCK_PLANNER_CALLS}）")
check((TUTDIR / "u1-l1.summary.md").exists(), "u1-l1.summary.md 生成")
check((TUTDIR / "u1-l2.summary.md").exists(), "u1-l2.summary.md 生成")

# ---- Run 2：同一 HEAD 续传，盯修复 #1 ----
# HEAD 未变、未强制，analyze 应直接判断「无任务」，planner 不应被调用。
# 早期缺陷：此处仍会重跑 planner、浪费一次调用，本用例将其钉死。
print("\n=== Run 2: resume（同 HEAD，无 force）===")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
provider.MOCK_PLANNER_CALLS = 0
rc2 = pipeline.main()
check(rc2 == 0, f"退出码 0（got {rc2}）")
check(provider.MOCK_PLANNER_CALLS == 0,
      f"续传不再调 planner（got {provider.MOCK_PLANNER_CALLS}）—— 修复 #1")
check(state_entry()["phase"] == "done", "仍为 done")

# ---- Run 3：上游新增提交，走 incremental ----
# 向夹具仓库添加新提交使 HEAD 变化。analyze 应据此重跑 planner，但标 keep 的
# 讲义须原样保留、不被清除，最后 phase 仍回到 done。
print("\n=== Run 3: incremental（上游有新提交）===")
(FIX / "new.txt").write_text("change\n", encoding="utf-8")
git(FIX, "add", "-A"); git(FIX, "commit", "-qm", "change")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
provider.MOCK_PLANNER_CALLS = 0
rc3 = pipeline.main()
e = state_entry()
check(rc3 == 0, f"退出码 0（got {rc3}）")
check(provider.MOCK_PLANNER_CALLS == 1,
      f"HEAD 变化→planner 重跑 1 次（got {provider.MOCK_PLANNER_CALLS}）")
check(e["phase"] == "done", "incremental 后 phase=done")
check((TUTDIR / "u1-l1.md").exists(), "u1-l1.md（update）仍在")
check((TUTDIR / "u1-l2.md").exists(), "u1-l2.md（keep）保留")
check(e["lectures"]["u1-l2"]["status"] in ("done", "keep"), "u1-l2 为 keep/done")

# ---- Run 4：断点续传，只补一篇 ----
# 模拟「上一轮未完成」：手动将 u1-l1 改为 pending、phase 改回 workers，其余不动。
# 预期行为：仅补 u1-l1 这一篇、完全不调用 planner，运行后 phase 回到 done。
print("\n=== Run 4: 断点续传（一篇 pending，同 HEAD，无 force）===")
st = json.loads(STATE.read_text(encoding="utf-8"))
st["repos"]["test/fixture"]["lectures"]["u1-l1"]["status"] = "pending"
st["repos"]["test/fixture"]["phase"] = "workers"
STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
provider.MOCK_PLANNER_CALLS = 0
rc4 = pipeline.main()
e = state_entry()
check(rc4 == 0, f"退出码 0（got {rc4}）")
check(provider.MOCK_PLANNER_CALLS == 0,
      f"续传 worker 不调 planner（got {provider.MOCK_PLANNER_CALLS}）")
check(e["lectures"]["u1-l1"]["status"] == "done", "pending 讲义被补完")
check(e["phase"] == "done", "phase 回到 done")
# 摘要复用：u1-l2 的 sidecar 在 Run 1 已生成，本次仅重新生成 u1-l1，
# u1-l2 的摘要应原样复用、不被重新生成。
check((TUTDIR / "u1-l2.summary.md").exists(),
      "u1-l2.summary.md 复用（未重新摘要）")

# ---- 检查 prev_head 是否持久化，盯修复 #2 ----
# worker 计算 diff 需要「上次的 HEAD」。该值必须落盘，否则续传时丢失基准。
# 此处验证其确实保存为 Run 3 之前的旧 HEAD。
print("\n=== 检查：prev_head 持久化（修复 #2）===")
e = state_entry()
check(e.get("prev_head") == HEAD1, f"prev_head 持久化为旧 HEAD（got {e.get('prev_head')})")

# ---- Run 4.5：提示词变更触发全量重构 ----
# 重要不变量：auto 与定时运行仅在「提示词被修改」或「首次构建」时执行全量。
# 此处不修改真实提示词文件，而是将 state 中的 prompt_hash 篡改为伪造值以通过
# 签名检查，使 analyze 误判提示词已变更。HEAD 不变、不强制，验证是否触发全量重构。
print("\n=== Run 4.5: 提示词变更 → 全量重构（auto，head 不变）===")
st = json.loads(STATE.read_text(encoding="utf-8"))
sig_now = st["repos"]["test/fixture"].get("prompt_hash")   # 当前真实签名，提示词实际未动
st["repos"]["test/fixture"]["prompt_hash"] = "deadbeefdeadbeef"   # 签名不匹配 = 模拟提示词被改
STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
os.environ.pop("RC_EVENT_NAME", None)   # 确保非定时运行
provider.MOCK_PLANNER_CALLS = 0
rc45 = pipeline.main()
e = state_entry()
check(rc45 == 0, f"退出码 0（got {rc45}）")
check(provider.MOCK_PLANNER_CALLS == 1,
      f"提示词变更→planner 重跑 1 次（got {provider.MOCK_PLANNER_CALLS}）")
check(e["version"] == 2, f"全量重构→version 递增到 2（got {e['version']}）")  # 仅 plan_mode==full 完成时 bump
check(e["prompt_hash"] == sig_now, "重构后 prompt_hash 更新为当前签名")
check(e["phase"] == "done", "phase=done")

# ---- Run 4.6：定时运行必须忽略手动全量开关 ----
# 另一条不变量：定时运行结构性忽略 RC_FORCE_FULL 与 RC_MODE=full，即使 workflow
# 误设也绝不执行全量。此处同时设置两者并标记为定时运行，提示词与 HEAD 均未变，
# 应直接 skip、不重跑 planner。
print("\n=== Run 4.6: 定时忽略手动全量开关（RC_FORCE_FULL 被忽略）===")
os.environ["RC_EVENT_NAME"] = "schedule"
os.environ["RC_FORCE_FULL"] = "true"; os.environ["RC_MODE"] = "full"
provider.MOCK_PLANNER_CALLS = 0
rc46 = pipeline.main()
e = state_entry()
check(rc46 == 0, f"退出码 0（got {rc46}）")
check(provider.MOCK_PLANNER_CALLS == 0,
      f"定时忽略 RC_FORCE_FULL/RC_MODE=full→不重跑 planner（got {provider.MOCK_PLANNER_CALLS}）")
check(e["version"] == 2, f"version 不变（未全量重构，got {e['version']}）")
check(e["phase"] == "done", "phase 仍 done")
os.environ.pop("RC_EVENT_NAME", None)   # 复位，避免污染后续 Run
os.environ["RC_FORCE_FULL"] = "false"; os.environ["RC_MODE"] = "auto"

# ---- Run 4.7：worker 超时须跨至下一篇，而非停掉整轮 ----
# 对照「额度耗尽即停整轮」：单篇超时仅将这一篇判为失败、retries 加一、跳至下一篇，
# 既不进行 in-run 重试（避免额外消耗一个 RC_TIMEOUT），也不停掉整轮。
# 通过 RC_MOCK_TIMEOUT_ON 使 u1-l1 本次必然超时。INRUN_RETRIES 默认 1，若误走
# in-run 重试，worker 调用次数将变为 2 而非 1，该断言即可捕获。
print("\n=== Run 4.7: 超时跨下一篇（RC_MOCK_TIMEOUT_ON=u1-l1）===")
st = json.loads(STATE.read_text(encoding="utf-8"))
st["repos"]["test/fixture"]["lectures"]["u1-l1"]["status"] = "pending"
st["repos"]["test/fixture"]["lectures"]["u1-l1"]["retries"] = 0
st["repos"]["test/fixture"]["phase"] = "workers"
STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
os.environ["RC_MOCK_TIMEOUT_ON"] = "u1-l1"
provider.MOCK_PLANNER_CALLS = 0; provider.MOCK_WORKER_CALLS = 0
rc47 = pipeline.main()
e = state_entry()
check(rc47 == 75, f"超时一篇未完成→退出 75（got {rc47}）")
check(e["lectures"]["u1-l1"]["status"] == "failed", "超时讲义标 failed")
check(e["lectures"]["u1-l1"]["retries"] == 1, "超时计 retries+=1（本次窗口失败）")
check(e["lectures"]["u1-l2"]["status"] == "done", "下一篇仍 done（跨过去、未受影响）")
check(provider.MOCK_PLANNER_CALLS == 0, "续传 worker 不调 planner")
check(provider.MOCK_WORKER_CALLS == 1,
      f"超时不 in-run 重试：worker 仅调 1 次（got {provider.MOCK_WORKER_CALLS}）")
check(e["phase"] == "workers", "phase 仍 workers（未停本轮、未 done）")
os.environ.pop("RC_MOCK_TIMEOUT_ON", None)

# ---- Run 4.8：子目录拆分（folders）----
# 验证 folders 字段将仓库展开为多个头等 fleet 条目：共享一份 clone，每个子目录
# 独立规划/生成，state key 带 @subpath 后缀，worker 在子目录下运行（CWD 落子目录），
# 收割至各自目录。向后兼容依赖 subpath=""——前述所有场景均使用无 folders 的夹具，
# 完全不受影响。
print("\n=== Run 4.8: folders 子目录拆分（共享 clone + 各自独立）===")
CTRL2 = TMP / "ctrl2"; CTRL2.mkdir()
WORK2 = TMP / "work2"; WORK2.mkdir()
STATE2 = CTRL2 / "repos_state.json"
TUTORIALS2 = CTRL2 / "tutorials"
# 第二个夹具仓库：含 a/、b/、deep/nested/ 三个子目录，模拟 monorepo 的子项目。
# 配置 folders: [a, b, self, deep/nested] 以覆盖 self 令牌与多级子目录。
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
# 以下路径为导入时固化的模块常量，此处直接改属性切换至独立环境（调用时读取全局）。
_prev_paths = (cfg.REPOS_YML, cfg.STATE_FILE, cfg.WORK_DIR, cfg.TUTORIALS_DIR)
cfg.REPOS_YML = CTRL2 / "repos.yml"
cfg.STATE_FILE = STATE2
cfg.WORK_DIR = WORK2
cfg.TUTORIALS_DIR = TUTORIALS2
os.environ["RC_REPO_NAME"] = ""        # 清除先前过滤，使全部 4 个条目均运行
os.environ["RC_MODE"] = "full"; os.environ["RC_FORCE_FULL"] = "true"
os.environ.pop("RC_EVENT_NAME", None)
provider.MOCK_PLANNER_CALLS = 0
rc48 = pipeline.main()
st2 = json.loads(STATE2.read_text(encoding="utf-8"))["repos"]
check(rc48 == 0, f"退出码 0（got {rc48}）")
check(set(st2.keys()) == {"test/multifix@a", "test/multifix@b",
                          "test/multifix", "test/multifix@deep/nested"},
      f"展开成 4 个条目（self key+@subpath key；got {sorted(st2.keys())}）")
for k in ("test/multifix@a", "test/multifix@b", "test/multifix",
          "test/multifix@deep/nested"):
    check(st2[k]["phase"] == "done", f"{k} phase=done")
check(st2["test/multifix@a"]["version"] == 1, "@a version=1")
check(provider.MOCK_PLANNER_CALLS == 4,
      f"每个子目录 + self 各调一次 planner（got {provider.MOCK_PLANNER_CALLS}）")
TUT_A = TUTORIALS2 / "test" / "multifix" / "MultiFix-a-tutorial"
TUT_B = TUTORIALS2 / "test" / "multifix" / "MultiFix-b-tutorial"
TUT_SELF = TUTORIALS2 / "test" / "multifix" / "MultiFix-tutorial"
TUT_DN = TUTORIALS2 / "test" / "multifix" / "MultiFix-deep-nested-tutorial"
for k, p in [("@a", TUT_A), ("@b", TUT_B), ("(self)", TUT_SELF),
             ("@deep/nested", TUT_DN)]:
    check((p / "manifest.json").exists(), f"{k} manifest.json 生成")
    check((p / "u1-l1.md").exists(), f"{k} u1-l1.md 生成")
# 共享 clone：work2 下仅有一份 test-multifix，而非每个 folder 各 clone 一份。
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
# RC_REPO_NAME 过滤：父名 → 全部 4 个；完整 @key → 单个。直接以 load_repos 验证，不运行 main。
cfg.REPOS_YML = CTRL2 / "repos.yml"
os.environ["RC_REPO_NAME"] = "test/multifix"
both = repo_mod.load_repos()
check(len(both) == 4, f"RC_REPO_NAME=父名 → 选中全部 4 个（got {len(both)}）")
os.environ["RC_REPO_NAME"] = "test/multifix@a"
one = repo_mod.load_repos()
check(len(one) == 1 and one[0].subpath == "a",
      f"RC_REPO_NAME=@a → 只选 a（got {[r.subpath for r in one]}）")
os.environ["RC_REPO_NAME"] = "test/multifix@deep/nested"
dn = repo_mod.load_repos()
check(len(dn) == 1 and dn[0].subpath == "deep/nested",
      f"RC_REPO_NAME=@deep/nested → 只选 deep/nested（got {[r.subpath for r in dn]}）")
# 复位模块常量与环境变量，避免污染后续 Run 5（纯单测不依赖它们，复位仅为干净）。
cfg.REPOS_YML, cfg.STATE_FILE, cfg.WORK_DIR, cfg.TUTORIALS_DIR = _prev_paths
os.environ["RC_REPO_NAME"] = "test/fixture"
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"

# ---- Run 4.9：worker 429/额度耗尽 → exit 75，不连累其他讲义 ----
# 验证 429 仅停止当前仓库、不设置全局停止。通过 RC_MOCK_QUOTA_ON 使 u1-l1 模拟 429 退出。
print("\n=== Run 4.9: 429 额度耗尽退 75（RC_MOCK_QUOTA_ON=u1-l1）===")
st = json.loads(STATE.read_text(encoding="utf-8"))
st["repos"]["test/fixture"]["lectures"]["u1-l1"]["status"] = "pending"
st["repos"]["test/fixture"]["lectures"]["u1-l1"]["retries"] = 0
st["repos"]["test/fixture"]["phase"] = "workers"
STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
os.environ["RC_MODE"] = "auto"; os.environ["RC_FORCE_FULL"] = "false"
os.environ["RC_MOCK_QUOTA_ON"] = "u1-l1"
provider.MOCK_PLANNER_CALLS = 0; provider.MOCK_WORKER_CALLS = 0
rc49 = pipeline.main()
e = state_entry()
check(rc49 == 75, f"429 退出→75（got {rc49}）—— 非 1、非 traceback")
check(e["lectures"]["u1-l1"]["status"] == "failed", "429 讲义标 failed")
check("quota" in (e["lectures"]["u1-l1"].get("error") or "").lower(),
      f"429 讲义 error 含 quota 描述（got {e['lectures']['u1-l1'].get('error')}）")
check(e["lectures"]["u1-l2"]["status"] == "done",
      "下一篇仍 done（未连累其他讲义/仓库）")
check(provider.MOCK_PLANNER_CALLS == 0, "续传不调 planner")
check(e["phase"] == "workers", "phase 仍 workers（未 done、待续传）")

# 防回归：main 必须将 run_start/deadline 同步至 runtime——provider 的 429 退避
# 依赖其感知软截止。若 main 遗漏赋值，模块全局恒为 0.0，软截止 bail 将静默失效。
# main 刚运行过（time.monotonic() 恒 > 0）；若接线断裂，模块全局仍为初值 0.0 → 断言失败。
check(runtime.RUN_START_MONO > 0,
      "main 把 run_start 同步到 runtime.RUN_START_MONO（429 退避软截止接线）")
os.environ.pop("RC_MOCK_QUOTA_ON", None)

# ---- Run 5：纯函数单测，全部离线，不消耗额度 ----
print("\n=== Run 5: 单元回归（classify / signature CRLF / deadzone wrap / render 无 None）===")

# --- 5a. _classify_error 分类：表驱动，匹配顺序是承重的 ---
# 关键点：max-turns 和 budget 必须先于 quota 匹配，否则 "max turns exceeded"
# 会被误判为额度耗尽。以下表格逐一对照各类报错文本与期望的异常类型。
CASES = [
    ("Error: max turns reached", provider.ClaudeRunnerError),
    ("Error: turns limit exceeded", provider.ClaudeRunnerError),
    ("Error: max budget exceeded", provider.ClaudeRunnerError),
    ("Error: spending limit hit", provider.ClaudeRunnerError),
    ("Error: rate_limit_exceeded (429)", provider.QuotaExhaustedError),
    ("insufficient credit balance", provider.QuotaExhaustedError),
    ("Error: cost limit exceeded", provider.QuotaExhaustedError),   # 第三方端点 → 整体停，非单篇
    ("Error: credit balance too low", provider.QuotaExhaustedError),
    ("Error: exceeded credit limit", provider.QuotaExhaustedError),
    ("Random unexpected error text 12345", None),                  # 未匹配 → 回退为 ClaudeRunnerError
]
for text, expected in CASES:
    got = provider._classify_error(text)
    check(got is expected,
          f"{text!r} → {expected.__name__ if expected else 'None'}"
          f"（got {got.__name__ if got else 'None'}）")

# --- 5b. signature 对 CRLF 归一化 ---
# Windows 上 autocrlf 可能使本地检出为 CRLF 而 CI 为 LF，签名将不一致并误触发全量。
# 构造两个内容相同但换行不同的文件，验证签名将其视为相同。
tmp_prompts = TMP / "test_prompts"; tmp_prompts.mkdir(exist_ok=True)
(tmp_prompts / "a.md").write_bytes(b"hello\nworld\n")
(tmp_prompts / "b.md").write_bytes(b"hello\r\nworld\r\n")
sig_lf = prompts.signature(tmp_prompts)
(tmp_prompts / "b.md").write_bytes(b"hello\nworld\n")
sig_crlf_normalized = prompts.signature(tmp_prompts)
check(sig_lf == sig_crlf_normalized,
      f"signature CRLF normalised: LF={sig_lf} vs CRLF→LF={sig_crlf_normalized}")
shutil.rmtree(tmp_prompts, ignore_errors=True)

# --- 5c. 死区跨午夜的判定 ---
# 当 START 大于 END 时死区跨午夜，须按 wrap-around 计算。此处临时将死区设为
# 22 至 2 点，验证若干临界时刻，再切回正常区间验证不跨午夜的路径。
orig_s, orig_e = cfg.DEAD_START, cfg.DEAD_END
cfg.DEAD_START = 22; cfg.DEAD_END = 2
check(scheduler.in_dead_zone(dt_mod(2026, 1, 1, 23, 0, tzinfo=tz_mod.utc)) is True,
      "跨午夜 23:00 → True")
check(scheduler.in_dead_zone(dt_mod(2026, 1, 1, 0, 30, tzinfo=tz_mod.utc)) is True,
      "跨午夜 00:30 → True")
check(scheduler.in_dead_zone(dt_mod(2026, 1, 1, 5, 0, tzinfo=tz_mod.utc)) is False,
      "跨午夜 05:00 → False")
check(scheduler.in_dead_zone(dt_mod(2026, 1, 1, 12, 0, tzinfo=tz_mod.utc)) is False,
      "跨午夜 12:00 → False")
# 不跨午夜的路径
cfg.DEAD_START = 6; cfg.DEAD_END = 10
check(scheduler.in_dead_zone(dt_mod(2026, 1, 1, 8, 0, tzinfo=tz_mod.utc)) is True,
      "正常 08:00 → True")
check(scheduler.in_dead_zone(dt_mod(2026, 1, 1, 5, 0, tzinfo=tz_mod.utc)) is False,
      "正常 05:00 → False")
cfg.DEAD_START, cfg.DEAD_END = orig_s, orig_e

# --- 5d. compose_worker_prompt 对缺失字段的兜底 ---
# manifest 中部分字段可能缺失，渲染时不得出现字面 None，须有兜底文案。
lec_none = {"id": "u1-l9", "title": "测试", "topic": "主题", "filename": "u1-l9.md"}
prompt = prompts.compose_worker_prompt(
    repo_mod.Repo(name="test/x", url="", branch=None, project="X", focus=""),
    lec_none, "new", "HEAD", None, "X-tutorial", "https://example.com/base/")
check("None" not in prompt and "自动判断" in prompt,
      "level=None 时渲染含兜底文案，无字面 None")
check("由 worker 根据主题和源码自行设计" in prompt,
      "practice_task=None 时渲染含兜底文案")

# --- 5e. _refill_count 惰性回填：429 欠位延后、正常完成补满至上限 ---
# 将任意 429/失败统一视为「本仓库本轮超时退出」：incomplete→不补（欠位）；正常完成
# →一次补满至 CONCURRENCY，单欠位即「补 2」，多欠位一次补完；全部 incomplete 则
# 永不补充、自然排空。表驱动钉死 (active, pending, concurrency, incomplete) → 期望补数。
REFILL_CASES = [
    # case1：429/失败 → 不补（即使队列仍很长）
    (2, 5, 3, True, 0),
    # 全部退出、active 已降至 0 → 仍不补（「全部退出则不补充」）
    (0, 5, 3, True, 0),
    # case2：正常完成、无欠位（active=cap-1）→ 补 1
    (2, 5, 3, False, 1),
    # case3：正常完成、此前有 1 欠位（active=cap-2）→ 补 2
    (1, 5, 3, False, 2),
    # 多欠位：active=cap-3（2 欠位）→ 一次补 3 补满
    (0, 5, 3, False, 3),
    # 队列不足：可补数量受 pending 限制
    (1, 1, 3, False, 1),
    (0, 2, 3, False, 2),
    # pending 为空 → 补 0
    (2, 0, 3, False, 0),
]
for active, pending, conc, inc, expected in REFILL_CASES:
    got = pipeline._refill_count(active, pending, conc, inc)
    check(got == expected,
          f"_refill_count(active={active},pending={pending},conc={conc},"
          f"incomplete={inc}) → {expected}（got {got}）")

# --- 5f. _quota_backoff_should_bail 软截止感知 ---
# 验证 429 退避在 deadline 到达 / GLOBAL_STOP 已设置时 bail。
# run_start 采用「相对 now」的快照构造：now=刚启动（差值≈0，未超），now-9999min=
# 很久以前启动（差值巨大，远超）。不可使用裸 0.0——time.monotonic() 零点未指定
# （Windows 为开机秒数），裸 0.0 会使「未超」用例在开机较久的机器上翻转成「已超」，
# 造成间歇性失败。
_now_mono = time.monotonic()
_aged = _now_mono - 9999 * 60
BAIL_CASES = [
    (_now_mono, 0.0, False, False),       # deadline_min=0 → 不 bail
    (_now_mono, 5.0, False, False),       # 未超 deadline（刚启动）→ 不 bail
    (_aged, 5.0, False, True),            # 远超 deadline → bail
    (_now_mono, 5.0, True, True),         # GLOBAL_STOP 已设置 → bail（即使 deadline 未到）
    (_aged, 5.0, True, True),             # 两条件均满足
    (_aged, 0.0, False, False),           # deadline_min=0 即使时间很早也不 bail
]
for run_start, dl_min, stop, expected in BAIL_CASES:
    got = provider._quota_backoff_should_bail(run_start, dl_min, stop)
    _age_min = (time.monotonic() - run_start) / 60
    check(got == expected,
          f"_quota_backoff_should_bail(run_age={_age_min:+.0f}min,deadline={dl_min},"
          f"stop={stop}) → {expected}（got {got}）")

# --- 5g. _safe_save 不向外抛异常 ---
# 令 save_state 模拟写入失败，验证 _safe_save 不外抛、仅记录 WARN。
_orig_save = state_mod.save_state
_caught_save = False


def _mock_failing_save(state):
    raise OSError("mock write failure")


state_mod.save_state = _mock_failing_save
try:
    state_mod._safe_save({}, "test_label")
except Exception:
    _caught_save = True
finally:
    state_mod.save_state = _orig_save
check(not _caught_save, "_safe_save 在 save_state 失败时不向外抛异常")

# --- 5h. repos.yml 非法条目过滤（回归：全角引号的 self、多余引号、重复条目）---
# 2026-10 空转事故里，`”self“`（全角引号）被当成子目录、`dinotty'` 多了个单引号，
# 都成了「永远失败」的合法条目塞在队列头。这里钉住：非法条目必须在装载阶段被拦下，
# 绝不能进队列。
_bad_yml = TMP / "bad_repos.yml"
_bad_yml.write_text(
    "repos:\n"
    "  - name: \"bad/name'\"\n"
    "  - name: typst/typst\n"
    "    folders:\n"
    "      - ”self“\n"
    "      - crates/typst\n"
    "  - name: aria2/aria2\n"
    "  - name: aria2/aria2\n",
    encoding="utf-8")
_prev_yml = cfg.REPOS_YML
os.environ.pop("RC_REPO_NAME", None)
cfg.REPOS_YML = _bad_yml
try:
    _loaded = repo_mod.load_repos()
finally:
    cfg.REPOS_YML = _prev_yml
    os.environ["RC_REPO_NAME"] = "test/fixture"
_keys = [repo_mod.repo_key(r) for r in _loaded]
check(all("'" not in k for k in _keys),
      f"非法仓库名（多余引号）被拒（got {_keys}）")
check("typst/typst@”self“" not in _keys,
      f"全角引号 self 被当成非法子目录拒收（got {_keys}）")
check("typst/typst@crates/typst" in _keys,
      f"同一 folders 下的合法子目录保留（got {_keys}）")
check(_keys.count("aria2/aria2") == 1,
      f"重复条目去重为一条（got {_keys.count('aria2/aria2')}）")

# --- 5i. gitutil.is_permanent_error：配置错误 vs 瞬时故障 ---
# 永久错误要摘出队列（blocked），网络抖动/超时必须继续按 incomplete 欠位重试。
PERM_CASES = [
    ("fatal: repository 'https://github.com/x/y.git/' not found", True),
    ("fatal: Remote branch release-9 not found in upstream 'origin'", True),
    ("fatal: could not read Username for 'https://github.com': terminal prompts disabled", True),
    ("fatal: unable to access 'https://github.com/x/y.git/': "
     "Could not resolve host: github.com", False),
    ("fatal: the remote end hung up unexpectedly", False),
    ("git clone 超时", False),
]
for _text, _expected in PERM_CASES:
    _got = gitutil.is_permanent_error(RuntimeError(_text))
    check(_got is _expected, f"is_permanent_error({_text[:42]!r}…) → {_expected}（got {_got}）")

# --- 5j. _run_phase：队头永久失败不阻塞后续（2026-10 空转事故回归）---
# 旧逻辑里任何非 done/skipped 的结果都不补位：队头只要有几个永久坏条目，
# 整轮就只跑这 4 个然后 5 秒退出，后面 78 个健康仓库永远轮不到。
_qrepos = [repo_mod.Repo(name=f"q/r{i}", url="", branch=None, project=f"r{i}", focus="")
           for i in range(8)]
_seen: list[str] = []


def _proc_blocked_head(r, state, generated_at, run_start, deadline_min, mode):
    _seen.append(r.name)
    return "blocked" if r.name in ("q/r0", "q/r1") else "done"


runtime.GLOBAL_STOP.clear()
_inc, _err = pipeline._run_phase(_qrepos, "test-phase", {}, "2026-01-01T00:00:00Z",
                                 time.monotonic(), 0.0, "auto", process=_proc_blocked_head)
check(len(_seen) == len(_qrepos),
      f"队头 2 个 blocked 不饿死队列：8 个仓库全部被调度（got {_seen}）")
check(_inc is True, "blocked 计入 any_incomplete（本轮仍 exit 75/不谎报完成）")
check(_err is False, "blocked 不置 had_error（不误报 exit 1）")

# 反过来钉住：纯 429/额度耗尽仍然「欠位不补」，否则会把新任务砸进未冷却的限流窗口。
_seen2: list[str] = []


def _proc_all_incomplete(r, state, generated_at, run_start, deadline_min, mode):
    _seen2.append(r.name)
    return "incomplete"


runtime.GLOBAL_STOP.clear()
pipeline._run_phase(_qrepos, "test-phase-429", {}, "2026-01-01T00:00:00Z",
                    time.monotonic(), 0.0, "auto", process=_proc_all_incomplete)
check(len(_seen2) == cfg.CONCURRENCY,
      f"全部 incomplete（429）→ 只跑满首批 {cfg.CONCURRENCY} 个，欠位不补（got {len(_seen2)}）")

# --- 5k. 仓库级熔断：连续配置错误 → 摘出队列，冷却到期自动放回 ---
# 用独立 state 文件，避免污染本次运行的正式测试 state。
_prev_state_file = cfg.STATE_FILE
cfg.STATE_FILE = TMP / "blocked_state.json"
try:
    _entry: dict = {}
    _out = ""
    for _ in range(cfg.BLOCK_LIMIT):
        _out = pipeline._mark_blocked(_entry, "t/x", RuntimeError("子目录不存在"),
                                      "2026-01-01T00:00:00Z", {"repos": {}})
    check(_out == "blocked", f"_mark_blocked 返回 blocked（got {_out!r}）")
    check(_entry.get("blocked") == cfg.BLOCK_LIMIT,
          f"连续失败计数 = {cfg.BLOCK_LIMIT}（got {_entry.get('blocked')}）")
    check(_entry.get("disabled") is True, "达阈值后置 disabled（摘出队列）")
    check(_entry.get("disabled_at") == "2026-01-01T00:00:00Z", "记录摘出时间戳，供冷却判定")
    _now_utc = dt_mod.now(tz_mod.utc)
    _fresh = dict(_entry, disabled_at=_now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
    check(pipeline._disabled_for_now(_fresh, _now_utc) is True, "刚熔断 → 冷却中，跳过")
    _stale = dict(_entry, disabled_at=(_now_utc - td_mod(hours=cfg.BLOCK_RETRY_HOURS + 1))
                  .strftime("%Y-%m-%dT%H:%M:%SZ"))
    check(pipeline._disabled_for_now(_stale, _now_utc) is False, "冷却到期 → 自动放回队列重试")
    check(pipeline._disabled_for_now({"disabled": False}, _now_utc) is False, "未熔断 → 不进冷却")
    check(pipeline._disabled_for_now({"disabled": True, "disabled_at": "garbage"}, _now_utc) is False,
          "时间戳损坏 → 当作已到期，重新试一次")
    pipeline._clear_blocked(_entry)
    check(not _entry.get("disabled") and "blocked" not in _entry,
          "_clear_blocked 清掉计数与熔断标记（自愈后不留痕）")
finally:
    cfg.STATE_FILE = _prev_state_file

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("SELFTEST PASSED ✅" if not failures
             else f"SELFTEST FAILED ❌ ({len(failures)}): " + "; ".join(failures)))
sys.exit(1 if failures else 0)
