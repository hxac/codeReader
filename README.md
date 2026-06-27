# codeReader

GitHub Actions + Claude Code headless 模式，按 `repos.yml` 项目生成讲义

+ 目前接入 GLM
+ 达到限额能保存进度
+ 手动 / API 触发

## 快速开始

1. 进仓库的 Settings → Secrets and variables → Actions → New repository secret

| Secret | 说明 |
|---|---|
| `ANTHROPIC_API_KEY`，`ANTHROPIC_AUTH_TOKEN` | 二选一，填 API 密钥 |
| `ANTHROPIC_BASE_URL` | 兼容 Anthropic 的端点 |
| `RC_MODEL` | 所用模型 |

2. 生成讲义的项目

```yaml
repos:
  - name: your-org/your-repo
    project: YourProj  # 可选，不写就取 name 的最后一段
```

3. 本地触发。或者 Actions → tutorial → Run workflow 触发

```bash
gh workflow run tutorial -f mode=full
```

产物：`tutorials/<owner>/<repo>/`

## 环境变量

**密钥类（Secrets）** —— 填在仓库 Settings → Secrets。

| Secret | 必填 | 说明 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 是 |  填 API 密钥 |
| `ANTHROPIC_AUTH_TOKEN` | 是 | 填 API 密钥 |
| `ANTHROPIC_BASE_URL` | 是 | Anthropic 端点的地址 |
| `RC_MODEL` | 是 | 所用模型 |

**调参类（Variables）** —— 填在仓库 Settings → Variables。

| Variable | 默认 | 说明 |
|---|---|---|
| `RC_CONCURRENCY` | `3` | 同时处理几个仓库 |
| `RC_MAX_TURNS` / `RC_MAX_BUDGET_USD` | `50` / `10.00` | 单篇讲义的上限，轮数和预算 |
| `RC_PLANNER_MAX_TURNS` / `RC_PLANNER_MAX_BUDGET_USD` | `30` / `3.00` | 排大纲的上限|
| `RC_SUMMARY_MAX_TURNS` / `RC_SUMMARY_MAX_BUDGET_USD` | `8` / `0.50` | 摘要的上限 |
| `RC_TIMEOUT` | `3600` | 一次 claude 子进程的超时时间 |
| `RC_WORKER_INRUN_RETRIES` | `1` | 同一个窗口里，单篇重试次数 |
| `RC_DEADZONE_UTC_START` / `RC_DEADZONE_UTC_END` | `6` / `10` | 死区，UTC 小时 |
| `RC_DEADZONE_SOFT_MARGIN_MIN` | `30` | 提前死区软停分钟 |
| `RC_RUNNER_LABELS` | `["self-hosted"]` | 仅 `runner=self-hosted` 时生效；跑在自建 runner 上，支持 JSON 数组 |

**手动触发（workflow_dispatch inputs）**

手动触发只在这一次生效，不影响后续的其他流程

| Input | 默认 | 说明 |
|---|---|---|
| `mode` | `auto` | `full` 全推倒重来 / `incremental` 增量 / `auto` 自动判断 |
| `repo_name` | 空 | 只做一个 `owner/repo`，留空按 `repos.yml` |
| `force_full` | `false` | 强制全量重建 |
| `ignore_timing` | `false` | 把死区那道时间关卡绕过去，调试的时候才动它 |
| `mock` | `false` | 管线测试，不运行 Claude Code |
| `debug` | `false` | 写进 `.debug/` 沙箱 |
| `runner` | `ubuntu-latest` | 默认 GitHub 官方；选 `self-hosted` 走 `RC_RUNNER_LABELS`（默认 self-hosted） |

## 注意事项

- **`tutorial-cache` 分支依赖与 `main` 共享提交历史**（用于把 `main` 的脚手架改动合并进进度分支）。若因敏感数据 scrub 等原因**改写了 `main` 历史**，两者会失去共同祖先、`git merge` 被拒。workflow 检测到「unrelated histories」时会**自动从当前 `main` 重建 `tutorial-cache` 并保留 `repos_state.json` + `tutorials/`**（强推覆盖旧分支），下一次运行即自愈，无需手动处理。
