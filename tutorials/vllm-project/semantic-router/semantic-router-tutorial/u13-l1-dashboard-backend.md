# 面板后端（Go）

## 1. 本讲目标

本讲拆解 Semantic Router 管理面板的 **Go 后端**（`dashboard/backend/`）——也就是 React 前端背后那台真正读写配置、触发部署、探活容器的 HTTP 服务。

学完后你应当能够：

- 说出后端三类核心 handler（config / deploy / status）各自处理哪些 HTTP 端点、各自完成什么职责；
- 读懂「改写 config.yaml → 同步到运行时」这条链路在 Go 代码里是怎么串起来的，并理解它和路由器进程内的 apiserver（见 u11-l1）如何分工协作；
- 区分 handler 层（HTTP 传输）与辅助层（文件改写、运行时下发、状态采集）的边界，理解仓库刻意保持「传输薄」的设计约定。

本讲只讲后端的配置/部署/状态三条主线 handler，前端的 React 实现留待 u13-l2，可视化与 DSL 编辑器留待 u13-l3。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **apiserver 是路由器进程内的控制面**（u11-l1）：它是 `src/semantic-router/pkg/apiserver`，和 ExtProc 数据面同处一个 Go 进程，经 `routerruntime.Registry` 共享运行时依赖，用 `source/runtime/active` 三态模型加 ETag 做配置部署与乐观并发。
- **本讲讲的「面板后端」是另一个进程**：`dashboard/backend/` 是独立编译的 Go 服务，是浏览器前端与底层基础设施之间的编排层。它既不是 apiserver，也不是 ExtProc，但它会复用路由器的 Go 配置类型，也会去读路由器暴露的健康/启动状态接口。
- **config.yaml 是单一真相源**（u3-l1、u3-l3）：路由器靠 fsnotify / 配置订阅热重载（u4-l3）读取磁盘上的 `config.yaml`；谁能安全地改写并下发它，谁就掌握了运行时行为。
- **本地编排由 vllm-sr CLI 负责**（u1-l4、u12-l1）：容器的命名、Envoy/router/dashboard 三件套的拓扑都由 Python CLI（`src/vllm-sr`）决定，后端的运行时下发会回调这套 Python 工具。

关键术语：

- **handler**：一个返回 `http.HandlerFunc` 的工厂函数，本后端几乎所有 handler 都形如 `XxxHandler(configPath string, ...) http.HandlerFunc`。
- **canonical config**：规范化后的 v0.3 运行时配置，对应 Go 类型 `routerconfig.CanonicalConfig`（来自 `src/semantic-router/pkg/config`），是后端与路由器共用的契约类型。
- **config projection（配置投影）**：每次部署/改写都会生成一条带版本号、来源、YAML 哈希、校验摘要的持久化记录，用于展示部署历史与检测漂移。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| `dashboard/backend/main.go` | 后端入口：加载配置、调用 `router.Setup`、启动 HTTP 服务。 |
| `dashboard/backend/router/router.go` | `Setup` 组装多路复用器（mux），打开投影存储，统一注册路由。 |
| `dashboard/backend/router/core_routes.go` | 核心路由注册表，把 URL 绑到 handler。 |
| `dashboard/backend/handlers/config.go` | **config handler**：读取/整体替换配置、读写全局默认。 |
| `dashboard/backend/handlers/deploy.go` | **deploy handler**：DSL 部署、合并、预览、版本回滚。 |
| `dashboard/backend/handlers/status.go` | **status handler** 的类型与入口。 |
| `dashboard/backend/handlers/status_modes.go` | 状态分派：容器内 / 宿主机两种探测模式。 |
| `dashboard/backend/handlers/status_collectors.go` | 各部署形态的状态采集器（Docker、直连、仅面板）。 |
| `dashboard/backend/handlers/status_runtime.go` | 路由器运行时状态合成（startup-status API / 本地文件）。 |
| `dashboard/backend/handlers/runtime_config_apply.go` | 配置写盘后的运行时下发与失败回滚。 |
| `dashboard/backend/handlers/runtime_config_sync.go` | 调用 Python CLI 同步运行时配置。 |
| `dashboard/backend/handlers/config_backups.go` | 配置备份的创建、列举、清理。 |
| `dashboard/backend/handlers/canonical_transport.go` | YAML↔JSON 的编解码与节点级合并工具。 |

## 4. 核心概念与源码讲解

### 4.1 后端整体架构与路由注册

#### 4.1.1 概念说明

面板后端是一个**标准库 `net/http` 单进程服务**：入口 `main.go` 加载 `config.Config`，调用 `router.Setup(cfg)` 得到一个 `Server`（内含 `http.Handler` 与关闭钩子），然后 `http.ListenAndServe` 监听端口（默认 8700，与 u1-l4 一致）。

设计上它遵循一条贯穿全后端的总约定——**HTTP 传输层要薄**。`handlers/AGENTS.md` 明确写道：handler 文件只该拥有「方法守卫、请求解码、响应编码、委派」这四件事，配置持久化、备份/版本清单、运行时下发、状态采集都要挪到相邻的辅助模块，而不是在 handler 里越写越胖。这条约定解释了为什么 `config.go`、`deploy.go`、`status.go` 都是「编排员」而非「实干家」。

#### 4.1.2 核心流程

启动到可服务的流程可以画成：

```text
main()
  └─ config.LoadConfig()               # 读端口、配置路径、RouterAPIURL、ReadonlyMode 等
  └─ router.Setup(cfg)
       ├─ setupAuthRoutes(...)          # 鉴权与会话
       ├─ workflowstore.Open(...)       # 工作流持久化存储
       ├─ configprojection.Open(...)    # 配置投影存储（部署历史）
       ├─ handlers.SetConfigProjectionStore(cp)   # 把投影存储注入 handler 包
       ├─ registerCoreRoutes(mux, cfg)  # config / status / topology / security / tools / health
       ├─ registerEvaluationRoutes(...) / SetupMCP(...) / registerMLPipelineRoutes(...)
       ├─ registerProxyRoutes(...)      # 代理 router / envoy / 可观测性
       └─ mux.Handle("/", StaticFileServer(...))   # 前端静态文件必须最后注册
       └─ return Server{Handler: wrapWithAuth(mux, authSvc), Close: ...}
  └─ http.ListenAndServe(":"+cfg.Port, srv.Handler)
```

`registerCoreRoutes` 把核心端点拆成若干小组注册，是本讲三类 handler 的总入口。

#### 4.1.3 源码精读

后端入口加载配置并交由 `Setup` 组装服务：

[dashboard/backend/main.go:11-21](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/main.go#L11-L21) —— `main()` 先 `config.LoadConfig()`，再 `router.Setup(cfg)`，配置加载失败即 `log.Fatalf`。

`Setup` 是整个后端的路由装配中心，它同时打开两个持久化存储并把投影存储注入 handler 包，最后才注册静态前端：

[dashboard/backend/router/router.go:20-66](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/router/router.go#L20-L66) —— 注意第 56 行 `mux.Handle("/", handlers.StaticFileServer(...))` 必须放最后，否则它会吞掉所有 `/api/...` 请求；而投影存储打开失败只打 warning 不致命，对应终端里「degraded」的降级语义。

核心端点分组注册，本讲的 config 与 status 就在 `registerCoreRoutes` 里：

[dashboard/backend/router/core_routes.go:18-25](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/router/core_routes.go#L18-L25) —— `registerCoreRoutes` 顺序注册健康/setup、配置、工具、状态、拓扑、安全策略六组路由。

config 路由把每个 URL 绑到一个 handler 工厂：

[dashboard/backend/router/core_routes.go:65-90](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/router/core_routes.go#L65-L90) —— 例如 `/api/router/config/all` → `ConfigHandler`、`/api/router/config/update` → `UpdateConfigHandler`、`/api/router/config/deploy` → `DeployHandler`。

status 路由只有两条：

[dashboard/backend/router/core_routes.go:123-129](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/router/core_routes.go#L123-L129) —— `/api/status` 与 `/api/logs`。

#### 4.1.4 代码实践

1. **实践目标**：在不开浏览器的情况下，摸清后端到底暴露了哪些端点。
2. **操作步骤**：
   - 通读 [core_routes.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/router/core_routes.go)，统计所有 `mux.HandleFunc` 的第一参数（URL）。
   - 按前缀归类：`/healthz`、`/api/setup/*`、`/api/router/config/*`、`/api/status`、`/api/logs`、`/api/topology/*`、`/api/security/*`、`/api/tools/*`。
   - 注意若干「legacy alias」：`/api/router/config/defaults` 与 `/api/router/config/global` 指向同一个 handler。
3. **需要观察的现象**：你会发现配置相关端点最密集，且写入类端点都把 `cfg.ReadonlyMode` 一路透传给 handler。
4. **预期结果**：能画出一张「URL → handler 工厂 → 所属文件」的对照表。

> 说明：本实践为源码阅读型实践，无需运行服务即可完成；若想验证，可在本地用 `make` 起栈后 `curl http://localhost:8700/api/status` 对照（待本地验证）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么静态前端 `mux.Handle("/", ...)` 必须最后注册？
  - **答案**：`http.ServeMux` 的最长前缀匹配会让 `/` 兜底匹配一切；若先注册 `/`，所有 `/api/...` 请求都会被静态文件处理器接管而永远到不了 API handler。
- **练习 2**：`configprojection.Open` 失败时后端还能启动吗？
  - **答案**：能。它只 `log.Printf` 一条 warning，把 `cp` 留为 `nil`；之后投影类 API 会返回 503，但核心 config/deploy/status 不受影响（见 `config_projection_deps.go` 的 nil 防御）。

### 4.2 Config handler：配置读写与运行时同步

#### 4.2.1 概念说明

config handler 解决的是「在面板里查看和整份编辑路由配置」。它围绕磁盘上的 `config.yaml` 提供读、整体替换、全局默认读写四种能力。最重要的一条语义是：**编辑器永远发送整份 canonical 文档**，所以 `UpdateConfigHandler` 不做任何「深合并」，而是用校验过的整份负载直接覆盖磁盘文件。

「整体替换」不代表「写完就完事」。它还必须把改动**同步下发给运行中的 Router 和 Envoy**，并且只有在运行时也接收成功后才向客户端返回成功——否则就会出现「面板显示保存成功、但线上还在跑旧配置」的假成功。这条「写盘 + 下发 + 必要时回滚」是 config 与 deploy 共享的骨架。

#### 4.2.2 核心流程

`UpdateConfigHandler`（整体替换）的主流程：

```text
POST /api/router/config/update
 ├─ 方法守卫（仅 POST/PUT）
 ├─ readonly 闸门：ReadonlyMode=true → 403 readonly_mode
 ├─ decodeYAMLTaggedBody[CanonicalConfig](r.Body)        # 把请求体解码成强类型配置
 ├─ validateCanonicalEndpointRefs(...)                   # 校验 entrypoint 引用
 ├─ os.ReadFile(configPath) → existingData               # 读旧配置，供回滚
 ├─ marshalYAMLBytes(configData) → yamlData
 ├─ routerconfig.ParseYAMLBytes(yamlData)                # 用路由器自己的解析器再校验一遍
 ├─ validateEndpointAddress(...) （每个 vLLM endpoint）
 ├─ writeConfigAtomically(configPath, yamlData)          # tmp + rename 原子写
 ├─ applyWrittenConfig(configPath, configDir, existingData, restoreOnFailure=true)
 │     └─ 失败则用 existingData 把磁盘与运行时一起回滚
 ├─ refreshConfigProjection(SourceManual)                # 异步记录部署投影
 └─ 返回 {"status":"success"}
```

读取侧 `ConfigHandler` 反而极简：读文件 → `writeYAMLTaggedJSON`（YAML 转 JSON 输出）。

#### 4.2.3 源码精读

读取配置为 JSON：

[dashboard/backend/handlers/config.go:15-34](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/config.go#L15-L34) —— `ConfigHandler` 设 `no-cache` 头，调 `readCanonicalConfigFile` 读盘，再用 `writeYAMLTaggedJSON` 输出。注意它本身不持有配置缓存，每次请求现读，与「config.yaml 是唯一真相源」一致。

整体替换的核心编排（注意 readonly 闸门与「先存旧、再写新、再下发」的顺序）：

[dashboard/backend/handlers/config.go:84-134](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/config.go#L84-L134) —— 第 89 行先校验 entrypoint 引用，第 95 行读旧配置 `existingData` 留作回滚依据，第 107 行用路由器的 `ParseYAMLBytes` 复校验，第 126 行原子写，第 131 行 `applyWrittenConfig(..., true)` 下发并允许失败回滚。

校验 vLLM endpoint 地址（解析阶段不做，故显式补一道）：

[dashboard/backend/handlers/config.go:113-124](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/config.go#L113-L124) —— 拒绝带 `http://` 前缀或路径或端口的非法地址，把错误前移到保存前。

「写盘 + 下发 + 回滚」的共享骨架定义在 `applyWrittenConfig`，它是 config 与 deploy 的共同终点：

[dashboard/backend/handlers/runtime_config_apply.go:43-55](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/runtime_config_apply.go#L43-L55) —— 下发失败且 `restoreOnFailure=true` 时，调 `restorePreviousRuntimeConfig` 把磁盘与运行时一起恢复成 `previousData`，并用 `runtimeConfigApplyError` 同时携带「下发错误」与「回滚错误」。

下发到底做了什么——`propagateConfigToRuntime` 把配置搬到运行时实际读取的位置，并重生成 Envoy 配置、重启 Envoy：

[dashboard/backend/handlers/runtime_config_apply.go:92-110](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/runtime_config_apply.go#L92-L110) —— 先 `syncRuntimeConfigForCurrentRuntime` 把 canonical 配置翻译成运行时配置，再按「是否在容器内、是否 split 拓扑、Envoy 容器是否在跑」决定本地重载还是在托管容器内重载。

「运行时配置同步」实际是回调 Python CLI：

[dashboard/backend/handlers/runtime_config_sync.go:151-167](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/runtime_config_sync.go#L151-L167) —— 拼出一段内联 Python，调用 `cli.commands.runtime_support.sync_runtime_config`（u12-l1 提到的 vllm-sr CLI 的运行时支持模块），把平台/算法覆盖项考虑进去后产出有效运行时配置。

> 与 apiserver 的协作：后端**不**经 apiserver 的 ETag 部署 API 下发配置。它直接改写 `config.yaml`，再通过 Python CLI 重生成 Envoy 配置并重启 Envoy；路由器进程则像 u4-l3 描述的那样，靠 fsnotify / 配置订阅热重载同一份 `config.yaml`。两端共享的契约是：**同一个 `config.yaml` 文件 + 同一套 `routerconfig` Go 类型**（后端用它做 `ParseYAMLBytes` 校验）。后端与 apiserver 的真正交互点在 status 侧——后端会去读路由器进程的 `/startup-status`、`/health`、`/info/models` 接口（见 4.4）。

#### 4.2.4 代码实践

1. **实践目标**：复现「改写 → 下发 → 失败回滚」的完整语义。
2. **操作步骤**：
   - 阅读 [config.go:64-147](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/config.go#L64-L147)，标注出 `existingData` 在哪一行被读、又在哪一行作为回滚源被使用。
   - 跟进 [runtime_config_apply.go:43-90](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/runtime_config_apply.go#L43-L90)，画出 `applyWrittenConfig → propagateConfigToRuntime → (失败) → restorePreviousRuntimeConfig` 的调用关系。
3. **需要观察的现象**：当 `propagateConfigToRuntime` 失败、`previousData` 非空、`restoreOnFailure=true` 时，磁盘上的 `config.yaml` 会被恢复成旧内容并再次下发；如果连回滚也失败，错误信息会同时包含两段。
4. **预期结果**：能用一段话解释「为什么面板保存配置很少出现磁盘与运行时不一致」。核心原因就是下发与回滚是成对、且基于同一份 `existingData` 的。运行时下发行为依赖本地容器拓扑，**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：`UpdateConfigHandler` 为什么不做深合并？
  - **答案**：注释（config.go:59-62）说明「编辑器总是发送整份 canonical 文档」，整份覆盖语义最清晰，也避免与遗留的旧磁盘布局产生歧义合并。
- **练习 2**：readonly 模式下，config 写入会走到哪一步被拦下？
  - **答案**：方法守卫通过后，第 72-82 行的 readonly 闸门直接返回 403 与 `readonly_mode` 错误体，请求体不会被解码、磁盘不会被触碰。

### 4.3 Deploy handler：DSL 部署、合并与版本回滚

#### 4.3.1 概念说明

deploy handler 解决的是「把 DSL 编译出来的路由片段部署成线上配置」。它与 config handler 的根本区别是：**输入不是整份配置，而是一个路由片段**（routing + 可选 entrypoints/recipes + 可选 global 片段），需要先和磁盘上已有的「基线配置」合并，再落盘。

为此 deploy 引入了三个机制：

1. **两种合并模式** `DeployModeMerge`（默认，向后兼容的部分合并）与 `DeployModeReplace`（原子替换 routing/entrypoints/recipes 表面）。
2. **并发互斥** `deployMu`：同一时刻只允许一个部署操作，用 `TryLock` 非阻塞抢占，抢不到立即 409。
3. **版本化备份**：每次部署先把当前配置备份成 `config.<时间戳>.yaml`，最多保留 10 份，支持按版本回滚。

#### 4.3.2 核心流程

`DeployHandler` 的核心是 `deployDirectWrite`：

```text
POST /api/router/config/deploy  (DeployRequest{YAML, DSL, BaseYAML, Mode})
 ├─ readonly 闸门 → 403
 ├─ deployMu.TryLock() → 抢不到 → 409 deploy_in_progress
 ├─ 校验 YAML 语法（routingFragmentDocument）
 ├─ 读 existingData
 ├─ mergeDeployPayload(existingData, req)            # 按 merge/replace 合并片段
 ├─ routerconfig.ParseYAMLBytes(merged)              # 校验合并结果
 ├─ createConfigBackup(configDir, existingData)      # 版本 = 时间戳
 ├─ archiveDeployDSL(configDir, req.DSL)             # 归档 DSL 源码供审计
 ├─ writeConfigAtomically(configPath, merged)
 ├─ applyWrittenConfig(..., restoreOnFailure=true)   # 下发，失败回滚
 ├─ cleanupBackups(backupDir)                        # 只留最近 10 份
 ├─ refreshConfigProjection(SourceDSL)               # 记录投影：来源=dsl
 └─ 返回 DeployResponse{Status, Version, Message}
```

回滚 `RollbackHandler` 走 `rollbackDirectWrite`：读指定版本备份 → 把当前配置也快照成一份新备份 → 原子写回备份 → 下发 → 投影来源记为 `SourceRollback`。

#### 4.3.3 源码精读

请求/响应类型与并发、备份常量：

[dashboard/backend/handlers/deploy.go:18-53](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/deploy.go#L18-L53) —— `deployMu`（包级 `sync.Mutex`）、`maxBackups = 10`、`DeployRequest`（含 `Mode`）、`DeployResponse`（返回 `Version`）。

部署主流程 `deployDirectWrite`，注意它的七步顺序与「同源回滚」：

[dashboard/backend/handlers/deploy.go:218-305](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/deploy.go#L218-L305) —— 第 220 行 `TryLock` 非阻塞抢锁；第 248 行 `mergeDeployPayload` 合并片段；第 259 行复校验；第 270 行建备份、第 273 行归档 DSL；第 284 行下发（失败回滚同 4.2）；第 292 行 `refreshConfigProjection(SourceDSL)`。

合并语义的核心是节点级操作 `mergeDSLOwnedNodes`，它只动 DSL 拥有的配置表面，从而让传输「向前兼容」（新增一种信号或投影不需要新加一条字段级合并分支）：

[dashboard/backend/handlers/deploy.go:359-393](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/deploy.go#L359-L393) —— `Replace` 模式整体替换 routing、entrypoints/recipes（未提供则删除）；`Merge` 模式对 routing 做递归映射合并，对 entrypoints/recipes 这类「带顺序与身份的列表」作为整体单元替换（省略则不动）。

备份创建用秒级时间戳作版本号，并存到固定的 `.vllm-sr/config-backups/` 目录：

[dashboard/backend/handlers/config_backups.go:15-38](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/config_backups.go#L15-L38) —— `version = time.Now().Format("20060102-150405")`，文件名 `config.<version>.yaml`；旧数据为空时只返回版本号不落备份文件。

清理只保留最近 `maxBackups` 份：

[dashboard/backend/handlers/config_backups.go:138-169](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/config_backups.go#L138-L169) —— 按文件名升序（即时间升序）排序后删除最旧的若干份。

预览 handler 提供部署前的 diff，它把当前与预览两侧都规范化（canonicalize）以消除「仅顺序不同」造成的噪声 diff：

[dashboard/backend/handlers/deploy.go:72-126](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/deploy.go#L72-L126) —— 返回 `{current, preview}` 两段 YAML，前端做并排 diff。

#### 4.3.4 代码实践

1. **实践目标**：理解 merge 与 replace 两种部署模式对同一份基线配置的差异。
2. **操作步骤**：
   - 准备一份含 `routing` + `providers` 的基线 `config.yaml`（可用 `config/config.yaml`）。
   - 构造一个只含 `routing` 的 DSL 编译片段。
   - 阅读 [mergeDeployPayload](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/deploy.go#L307-L354) 与 [mergeDSLOwnedNodes](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/deploy.go#L359-L393)。
   - 手工推演：merge 模式下基线的 `providers`、`global` 是否保留？replace 模式下若片段未提供 `recipes`，基线的 `recipes` 会被删掉吗？
3. **需要观察的现象**：
   - merge 模式：`providers` 等非 routing 段原样保留；routing 被递归合并。
   - replace 模式：routing 被整体替换；未提供 `recipes` 时基线 recipes 被删除（`deleteMappingValueNode`）。
4. **预期结果**：能给出两种模式下「基线 providers / routing / recipes」三者的去留表。

> 说明：可对照 `dashboard/backend/handlers/deploy_test.go` 与 `config_projection_deploy_test.go` 里的断言确认你的推演（这两个测试文件覆盖了 merge/replace/rollback/版本列表）。

#### 4.3.5 小练习与答案

- **练习 1**：两个部署请求几乎同时到达，第二个会怎样？
  - **答案**：`deployMu.TryLock()` 非阻塞，第二个请求抢不到锁，立即返回 409 `deploy_in_progress`，不会阻塞排队。
- **练习 2**：投影记录里的 `source` 字段对 deploy / 手动改写 / 回滚分别取什么值？
  - **答案**：deploy → `SourceDSL`（`"dsl"`），手动整体改写 → `SourceManual`（`"manual"`），回滚 → `SourceRollback`（`"rollback"`），定义见 `configprojection/types.go`。

### 4.4 Status handler：容器探活与状态合成

#### 4.4.1 概念说明

status handler 解决的是「面板首页要展示整套服务健不健康」。它面临的难点是后端可能跑在**多种环境**里：可能自己在容器内、可能直连一个本机进程、可能只看到自己（router 没起来）。因此它不是简单 ping 一下，而是一套**按部署形态分派的状态合成器**，最终拼出一个统一的 `SystemStatus` 结构。

`SystemStatus` 里有整体判定、部署类型、各服务明细、路由器运行时进度（下载模型阶段）、模型清单等，是前端概览页的数据来源。

#### 4.4.2 核心流程

```text
GET /api/status
 └─ detectSystemStatus(routerAPIURL, configDir)
      ├─ runtimePath = configDir/.vllm-sr/router-runtime.json
      ├─ if isRunningInContainer(): collectInContainerStatus        # 容器内：托管 Docker
      └─ else collectHostStatus:
            ├─ collectSplitManagedHostStatus  (split 托管拓扑优先)
            ├─ collectDirectStatus            (直连一个真在跑的 router)
            └─ collectDashboardOnlyHostStatus (兜底：只有面板)
```

每个采集器都会探一组服务（Router / Envoy / Dashboard），合成 `Services` 列表与 `Overall`（healthy / degraded / stopped / not_running）。路由器运行时状态 `RouterRuntime` 优先来自路由器进程的 `/startup-status` API，回退到本地 `router-runtime.json` 文件。

#### 4.4.3 源码精读

类型定义与 handler 入口：

[dashboard/backend/handlers/status.go:10-59](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/status.go#L10-L59) —— `SystemStatus` 结构含 `Overall`/`DeploymentType`/`Services`/`RouterRuntime`/`Models`/`Endpoints`/`Version`；`RouterRuntimeStatus` 直接复用 `pkg/startupstatus.EmbeddingProviderStatus`，体现「后端复用路由器 Go 包」的契约。

环境分派——先判是否在容器内：

[dashboard/backend/handlers/status_modes.go:5-21](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/status_modes.go#L5-L21) —— `detectSystemStatus` 决定走容器内还是宿主机分支；`baseSystemStatus` 给出 `not_running` 的安全默认底座。

宿主机分支的优先级链：

[dashboard/backend/handlers/status_collectors.go:7-16](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/status_collectors.go#L7-L16) —— 依次尝试 split 托管、直连、仅面板兜底。

托管 Docker 形态的采集——逐个探 Router/Envoy/Dashboard：

[dashboard/backend/handlers/status_collectors.go:33-55](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/status_collectors.go#L33-L55) —— `collectManagedDockerStatus` 取 router 容器日志尾部、解析各服务健康、合成 `RouterRuntime` 与 `Models`，再 `setManagedDockerOverall` 综合判定。

路由器运行时合成——优先 API，回退文件：

[dashboard/backend/handlers/status_runtime.go:15-37](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/status_runtime.go#L15-L37) —— `resolveRouterRuntimeStatus` 先试 `routerAPIURL + /startup-status`（这是 apiserver 的启动状态端点，见 u4-l1），拿不到再 `loadRouterRuntimeState` 读本地文件；若文件说 ready 但 `/ready` 不通，则降级回 `starting`。

底层探活原语都在 `status_probes.go`：

[dashboard/backend/handlers/status_probes.go:11-37](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/status_probes.go#L11-L37) —— `getDockerContainerStatus` 用 `docker inspect` 取状态、找不到返回 `"not found"`；`isRunningInContainer` 靠 `/.dockerenv` 与 `/proc/1/cgroup` 判定。

HTTP 健康探活：

[dashboard/backend/handlers/status_probes.go:46-80](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/status_probes.go#L46-L80) —— `checkHTTPHealth` 2 秒超时 GET；`checkEnvoyHealth` 返回三态（运行但上游未就绪 / 就绪 / 未运行）。

#### 4.4.4 代码实践

1. **实践目标**：理解同一个 `/api/status` 在三种环境下返回的内容差异。
2. **操作步骤**：
   - 阅读 [status_collectors.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/status_collectors.go)，列出 `collectManagedDockerStatus` / `collectDirectStatus` / `collectDashboardOnlyHostStatus` 各自的 `DeploymentType` 与 `Services` 个数。
   - 跟进 [status_runtime.go:39-62](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/status_runtime.go#L39-L62)，确认 `/startup-status` 返回 200 或 503 都会被接受（503 表示还在启动）。
3. **需要观察的现象**：
   - 直连模式（`collectDirectStatus`）只有当 router `/health` 通了才会被采纳，否则继续向下兜底。
   - 兜底模式会如实报告「Router API URL is not configured」或「Router health check failed」。
4. **预期结果**：能说清「为什么面板首页在 router 还没起来时不会报错崩溃，而是显示 degraded / starting」。容器与直连的实际返回值依赖本地运行环境，**待本地验证**。

#### 4.4.5 小练习与答案

- **练习 1**：`/startup-status` 返回 503 时，状态合成会失败吗？
  - **答案**：不会。[status_runtime.go:47-49](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/handlers/status_runtime.go#L47-L49) 明确接受 200 与 503，503 正是路由器还在启动的合法状态，会被解析成 `RouterRuntimeStatus.Phase` 展示。
- **练习 2**：为什么状态采集要分「容器内 / 宿主机」两大模式？
  - **答案**：探活手段不同。容器内时后端与 router/envoy 同处一套 Docker 拓扑，用 `docker inspect`/`docker logs` 探；宿主机时可能直连一个进程或只能看到自己，需用 HTTP 探活并逐级兜底。`isRunningInContainer()` 就是这条分水岭。

## 5. 综合实践

把本讲三条主线串起来，完成一个「配置改动从面板到运行时」的全链路追踪任务：

1. **梳理端点表**：通读 [core_routes.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/dashboard/backend/router/core_routes.go)，产出一张表，列出 config / deploy / status 三类 handler 各自对应的所有 URL、HTTP 方法、是否受 readonly 限制、调用了哪个核心辅助函数（如 `applyWrittenConfig`、`detectSystemStatus`、`mergeDeployPayload`）。
2. **追踪配置下发链路**：从 `UpdateConfigHandler`（或 `DeployHandler`）出发，画出 `writeConfigAtomically → applyWrittenConfig → propagateConfigToRuntime → syncRuntimeConfigForCurrentRuntime → (Python) sync_runtime_config / generate_envoy_config_from_user_config` 这条链路，标注每一步在哪个文件、失败时如何回滚。
3. **对比 apiserver**：写一段话说明——面板后端改配置走「改文件 + Python CLI 重生成 Envoy + Envoy 重启」，而 apiserver（u11-l1）走「ETag 乐观并发 + source/runtime/active 三态」；两者共享的是同一份 `config.yaml` 与同一套 `routerconfig` Go 类型。面板后端通过读取路由器的 `/startup-status`、`/health`、`/info/models` 来感知 apiserver 所在进程的状态。
4. **验证（可选）**：本地起栈后，分别 `curl /api/status`、`curl /api/router/config/all`、用一份合法 config 调 `/api/router/config/update`（注意 readonly），观察返回结构是否与你画的图一致（待本地验证）。

完成本任务后，你应当能独立判断「加一个新的配置类端点」需要改哪几处、以及为什么它不该把业务逻辑塞进 handler 本身。

## 6. 本讲小结

- 面板后端是一个独立编译的 `net/http` 服务，`main.go → router.Setup → registerCoreRoutes` 是其装配主干，静态前端必须最后注册。
- **config handler** 负责 `config.yaml` 的整份读取与整体替换，核心约定是「编辑器发整份文档」，写盘后必须经 `applyWrittenConfig` 下发到运行时、失败用同一份旧数据回滚。
- **deploy handler** 处理 DSL 编译出的路由片段，靠 `merge/replace` 两种模式做节点级合并，用 `deployMu` 保证部署串行、用秒级时间戳做版本化备份（保留 10 份），并支持按版本回滚。
- **status handler** 是一套按部署形态分派的状态合成器（容器内托管 / 宿主机直连 / 仅面板兜底），逐个探 Router/Envoy/Dashboard，路由器运行时优先取 `/startup-status` API、回退本地文件。
- 三类 handler 共享「写盘 + 下发 + 回滚」骨架与「配置投影」记录机制（来源分 `manual/dsl/rollback`），并共同遵循「HTTP 传输薄、业务塞进相邻辅助模块」的 `AGENTS.md` 约定。
- 面板后端与 apiserver 是两个进程：面板改的是文件、靠 Python CLI 下发 Envoy；路由器靠热重载读同一份文件；二者通过 `/startup-status` 等只读接口协作。

## 7. 下一步学习建议

- 阅读 `dashboard/backend/handlers/AGENTS.md`，体会「handler 只做传输、实干交给辅助模块」这条约定如何约束后续改动——这是给本后端加功能时的第一守则。
- 继续学习 **u13-l2 面板前端（React）**：看 `App.tsx`、`DashboardPage.tsx`、`ConfigPage.tsx` 如何消费本讲这些 `/api/...` 端点。
- 学习 **u13-l3 可视化配置与 DSL 编辑器**：看 `BuilderPage` 与 `DslEditorPage` 如何调用 deploy/preview 端点，把本讲的合并/回滚机制与前端的双向编辑闭环。
- 想深入运行时下发，可回头看 **u4-l3（ExtProc 的配置热重载）** 与 **u12-l1（vllm-sr CLI 的容器编排）**，理解面板后端回调的那段 Python（`sync_runtime_config`、`generate_envoy_config_from_user_config`）在整套编排里的位置。
