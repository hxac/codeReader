# CodeReader
## 快速开始

1. 配置 Secret

> Settings → Secrets and variables → Actions → New repository secret

| Secret | 说明 |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic 兼容的端点（与 AUTH_TOKEN 二选一） |
| `ANTHROPIC_AUTH_TOKEN` | 第三方端点令牌（与 API_KEY 二选一，需配合 BASE_URL） |
| `ANTHROPIC_BASE_URL` | 第三方兼容端点的完整地址 |

2. 配置解读的项目

```yaml
repos:
  - name: your-org/your-repo
    # project: YourProj  # 可选，不写就取 name 的最后一段
    # folders 可选，拆分子目录独立生成：
    #   - "self" → 整仓讲义
    #   支持多级路径如 "scipy/cluster"
```

3. 本地或 CI 触发。工作流捕获退出码：
   - 0 = 全部完成
   - 75 = 有未完成项（进度已存盘，下次窗口续传）
   - 1 = 未预期错误（跳过发布）

```bash
gh workflow run tutorial -f mode=full
```

产物：`tutorials/<owner>/<repo>/<project>-tutorial/`

## 环境变量

**密钥（Secrets）** —— 填在仓库 Settings → Secrets。

> `API_KEY` 与 `AUTH_TOKEN` 至少选一。

| Secret | 必填 | 说明 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 否* | 真 Anthropic 端点密钥 |
| `ANTHROPIC_AUTH_TOKEN` | 否* | 第三方端点的令牌 |
| `ANTHROPIC_BASE_URL` | 否 | 第三方兼容端点的完整地址 |
| `RC_MODEL` | 否 | 所用模型 |

**参数（Variables）** —— 填在仓库 Settings → Variables。

| Variable | 默认 | 说明 |
|---|---|---|
| `RC_CONCURRENCY` | `4` | 同时处理几个仓库 |
| `RC_MAX_TURNS` / `RC_MAX_BUDGET_USD` | `50` / `10.00` | 单篇讲义的上限，轮数和预算 |
| `RC_PLANNER_MAX_TURNS` / `RC_PLANNER_MAX_BUDGET_USD` | `30` / `3.00` | 排大纲的上限|
| `RC_SUMMARY_MAX_TURNS` / `RC_SUMMARY_MAX_BUDGET_USD` | `8` / `0.50` | 摘要的上限 |
| `RC_TIMEOUT` | `3600` | 一次 claude 子进程的超时时间 |
| `RC_WORKER_INRUN_RETRIES` / `RC_BACKOFF_SEC` | `1` / `0` | 同一窗口内单篇重试次数与退避间隔 |
| `RC_MAX_RETRIES` | `5` | 跨窗口最大失败次数 |
| `RC_RUN_DEADLINE_MIN` | `0` (本地) / `270` (CI) | 软截止：到点 clean exit 75 保进度 |
| `RC_DEADZONE_UTC_START` / `RC_DEADZONE_UTC_END` | `6` / `10` | 死区（UTC 6‑10，3× 消耗，绝不跑）|
| `RC_DEADZONE_SOFT_MARGIN_MIN` | `30` | 提前死区软停分钟 |

**workflow_dispatch 输入**

每次手动触发只在本次生效，不影响其他运行。

| Input | 默认 | 说明 |
|---|---|---|
| `mode` | `auto` | `full` 全量重建 / `incremental` 增量 / `auto` 自动 |
| `repo_name` | 空 | 只处理一个 `owner/repo`，留空按 `repos.yml` |
| `force_full` | `false` | 强制全量重建 |
| `mock` | `false` | 管线测试桩，不调用 Claude Code |
| `debug` | `false` | 写进 `.debug/` 沙箱 |
| `runner` | `ubuntu-latest` | 默认 GitHub 官方；选 `self-hosted` 走变量 `RC_RUNNER_LABELS` |

## 注意事项

GitHub 上的定时触发存在峰谷调度，实际触发时间会不准，建议采用 `cron` 定时触发，可参考如下脚本：

```bash
export GITHUB_TOKEN=github_pat_xxxx
export GITHUB_REPO=hxac/codeReader
export GITHUB_WORKFLOW=tutorial.yml

curl -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"ref": "main"}' \
  "https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${GITHUB_WORKFLOW}/dispatches"
```

> 注意 `GITHUB_TOKEN` 只需开放 `workflows` 的读写权限即可。
