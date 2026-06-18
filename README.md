# codeReader

用 **GitHub Actions（定时 + 手动）+ Claude Code CLI headless** 为一批项目**生成中文讲义/教程**：
**两阶段管线** = planner 出大纲（单元→讲义 manifest）→ workers 并行生成每篇讲义（最小模块 6 要素）。
内置**配额/时区感知**与**断点续传**：额度有限、有死区时段时也能稳步推进，进度不会丢。

## 目录结构

```
codeReader/  (控制仓库)
├─ .github/workflows/tutorial.yml   # 定时(四便宜窗口：00:01/02:01/10:01/12:01 UTC)+手动入口；tutorial-cache 分支累积进度
├─ prompts/
│  ├─ planner.prompt.md             # 阶段1：读项目 → 大纲 manifest(JSON)
│  └─ worker.prompt.md              # 阶段2：生成单篇讲义（最小模块6要素，LaTeX，中文，永久链接）
├─ scripts/
│  ├─ analyze.py                    # 编排：两阶段、并行 worker、per-lecture 进度、时区守卫、续传
│  └─ claude_runner.py              # `claude -p` 封装（planner/worker + 额度错误分类）
├─ repos.yml                        # 项目列表（当前：Mooncake、Megakernels）
├─ requirements.txt
├─ state/repos_state.json           # 持久状态 + 每篇讲义进度（运行时生成）
├─ tutorials/<owner>/<repo>/<Project>-tutorial/   # 产物：manifest.json + 每讲义一个 .md
└─ work/                            # clone 工作副本（gitignore）
```

## 两阶段管线

**阶段 1 · planner**（每项目 1 次 claude 调用，只读 + `--json-schema`）
读项目 → 产出大纲 `manifest.json`：数个单元，每单元数篇讲义，每讲义含
`id/title/filename/topic/source_files/minimal_modules/depends_on/action`。
- `full`：全新规划（所有讲义 `action=new`）。
- `incremental`：读旧 manifest + 旧讲义 + `git diff prev..head`，给每篇标 `keep/update/new/rebuild`。

**阶段 2 · workers**（每篇讲义 1 次 claude 调用，并行，低并发）
对 `action != keep` 的讲义并行生成/更新 `tutorials/.../<Project>-tutorial/<filename>.md`。
每篇最小模块 6 要素：概念概述 · 伪代码 · 数学（`\[ \]` / `\( \)`）· 代码实现（GitHub 永久链接到行）· 思考题 · 答案。

## 配额 / 时区（你的约束已内置）

GitHub Actions 跑在 **UTC**。换算：

| 北京时间 | UTC | 含义 |
|---|---|---|
| 14:00–18:00 | **06:00–10:00** | **死区（3×消耗）—— 绝不执行** |
| 08:00–12:00 | 00:00–04:00 | 便宜/高速窗口 |
| 18:00–22:00 | 10:00–14:00 | 便宜/高速窗口 |
| 05 / 10 / 15 / 20 | 21:00 / 02:00 / 07:00 / 12:00 | 额度恢复点 |

行为：
- **死区硬守卫**：`analyze.py` 启动时若处于 UTC 06:00–10:00（且未设 `ignore_timing`），直接跳过，不烧额度。
- **临近死区软停**：距死区 30 分钟内不再启动新 worker，已保存的进度等下次窗口续传（默认窗口余量 `RC_DEADZONE_SOFT_MARGIN_MIN`）。
- **额度耗尽即停**：`claude_runner` 识别额度/限流/计费错误 → 抛 `QuotaExhaustedError` → `analyze.py` 保存进度、退出码 75、不再启动后续 worker/项目。
- **定时自动续传**：workflow 在四个便宜窗口开头（`00:01` / `02:01` / `10:01` / `12:01` UTC = 北京 08:01 / 10:01 / 18:01 / 20:01）自动触发 `mode=auto`，捡起未完成的讲义继续。

退出码：`0`=本轮全部完成 · `75`=未完成（有进度）· `1`=未预期错误。

## 分支策略（进度不丢）

- **`tutorial-cache`**（自动建/维护）：每次运行都把 `state/` + `tutorials/`（含未完成的）提交到这里 —— 额度耗尽也不丢。
- **`main`**：仅当某次运行**退出 0（全部完成）**时，把 `tutorial-cache` 合并发布到 `main`。
- 下次运行从 `tutorial-cache` 续传（`analyze.py` 按 per-lecture 状态只跑 pending/failed 的讲义）。

> 看「进行中」的半成品讲义去 `tutorial-cache` 分支；看「成品」去 `main`。

## 快速开始

### 1. 配密钥（Settings → Secrets）

| Secret | 何时需要 |
|---|---|
| `ANTHROPIC_API_KEY` | 真实 Anthropic 端点 |
| `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` | 第三方兼容端点（一起设） |

可选 **Variables**：

| Variable | 默认 | 说明 |
|---|---|---|
| `RC_CONCURRENCY` | `2` | worker 并行度（额度紧就调 1） |
| `RC_MODEL` | `claude-sonnet-4-6` | 模型 |
| `RC_MAX_TURNS` / `RC_MAX_BUDGET_USD` | `50` / `10.00` | 单篇讲义上限 |
| `RC_PLANNER_MAX_TURNS` / `RC_PLANNER_MAX_BUDGET_USD` | `30` / `3.00` | 单次规划上限 |
| `RC_TIMEOUT` | `3600` | 单次 claude 子进程超时(秒) |

### 2. 编辑 `repos.yml`

```yaml
repos:
  - name: kvcache-ai/Mooncake
    focus: "分离式 KVCache 各子系统"
  - name: your-org/your-repo
    project: YourProj        # 可选；缺省= name 最后一段
```

### 3. 触发

- **手动**（建议首次用 `force_full` 跑一个项目试水）：
  ```bash
  gh workflow run tutorial -f mode=full -f repo_name=HazyResearch/Megakernels
  ```
- **自动续传**：已配置 `00:01` / `02:01` / `10:01` / `12:01` UTC 四个 cron，无需干预。或手动 `mode=auto` 续传。

## 状态文件 `state/repos_state.json`

```json
{
  "schema_version": 1,
  "repos": {
    "HazyResearch/Megakernels": {
      "phase": "workers",
      "mode": "full",
      "manifest_head": "abc123...",
      "version": 0,
      "lectures": {
        "u1-l1": {"status": "done", "action": "new", "cost": 0.42},
        "u1-l2": {"status": "pending", "action": "new", "retries": 0},
        "u1-l3": {"status": "failed", "action": "new", "retries": 1, "error": "..."}
      },
      "tutorial_path": "tutorials/HazyResearch/Megakernels/Megakernels-tutorial",
      "last_error": "quota exhausted; will resume next window"
    }
  }
}
```
`phase`: `planner` → `workers` → `done`。续传只跑 `pending`/`failed` 讲义；`keep` 与 `done` 跳过。

## 本地调试（act）

所有本地配置统一在 `.env`（secrets + vars + env 三合一）和 `.input`（workflow_dispatch inputs），
与 GitHub Actions 的 Settings → Secrets/Variables 透传方式一致。act 默认读取 `.env` 和 `.input`。

```bash
cp .env.example .env        # 初次：填密钥和调参
cp .input.example .input     # 初次：填本次要跑的 inputs
```

### 零成本冒烟（MOCK，不烧额度，需联网 clone 目标仓）

```bash
# .input 里设 mock=true，.env 可全留占位（MOCK 不需要密钥）
act workflow_dispatch -W
# → tutorials/<owner>/<repo>/<Project>-tutorial/{manifest.json,u1-l1.md,u1-l2.md}
# env.ACT 守卫跳过 git push / publish / 分支切换 / claude 安装（mock 下不需要）
```

### 有额度真跑（debug 单篇，最低成本验证提示词）

```bash
# .env 填真 ANTHROPIC_API_KEY（或 AUTH_TOKEN+BASE_URL）
# .input 设 debug=true repo_name=HazyResearch/Megakernels
act workflow_dispatch -W
# → 只生成 1 篇讲义（debug 自动 ignore_timing、固定单仓、默认写 .debug/ 沙箱不污染正式产物）
```

### 有额度真跑（全量单仓）

```bash
# .input 设 mode=full repo_name=HazyResearch/Megakernels ignore_timing=true
act workflow_dispatch -W
```

## 设计要点

- **agentic 自写文件**：worker 以项目为 cwd，**仅限 `<Project>-tutorial/` 的 Write/Edit**，源码只读。
- **执行/提示词分层**：换 provider 只改 `claude_runner.py`；调讲义风格只改 `prompts/*.prompt.md`。
- **额度安全**：低并发默认 + 额度错误整体停 + 死区不跑 + 临近软停 + 进度落盘 + 缓存分支。
- **端点无关**：透传 3 个 `ANTHROPIC_*` 变量。

## 故障排查

| 现象 | 处理 |
|---|---|
| 死区被跳过（日志「处于死区…跳过」） | 正常；等便宜窗口自动续传，或手动勾 `ignore_timing`（烧 3× 额度） |
| 讲义 `status=failed` 且 retries 增长 | 看该讲义 `error`；可能是单篇预算/轮次不足，调高 `RC_MAX_TURNS`/`RC_MAX_BUDGET_USD` 后 `mode=auto` 续传 |
| `last_error: quota...` | 额度耗尽，正常；下次窗口自动续传未完成讲义 |
| `main` 一直没更新 | 正在 `tutorial-cache` 推进中；某次退出 0 才会 publish 到 main |
| planner 返回非法 manifest | 检查 `RC_PLANNER_MAX_TURNS`；或对该项目 `force_full` 重规划 |
