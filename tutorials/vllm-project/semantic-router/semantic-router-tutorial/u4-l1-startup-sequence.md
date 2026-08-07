# 启动主流程：main.go 启动序列

## 1. 本讲目标

本讲带你走进 vLLM Semantic Router（下文简称 SR）Go 路由器的**进程启动入口**。学完本讲，你应该能够：

- 说出 `main()` 中各初始化步骤的**先后顺序与依赖关系**，并能解释“为什么是这个顺序”。
- 理解为什么 **API Server 要提前启动**，以及它如何通过 `/startup-status` 端点把“模型正在下载”“路由已就绪”这些状态暴露给 dashboard / 编排器。
- 看懂**模型下载的进度上报机制**与“下载失败即 fatal”的严格语义，知道哪些环节失败会让进程直接退出。
- 掌握“选项解析 → 配置加载 → API 提前启动 → 模型下载 → 依赖初始化 → 热身 → 标记就绪 → 起 ExtProc”这条主链，为后续讲义（u4-l2 Runtime Registry、u4-l3 ExtProc Server）打好骨架。

> 本讲只讲“启动序列”，不深入 ExtProc 内部状态机、Registry 内部结构、决策引擎——这些是后续讲义的主题。

## 2. 前置知识

- **进程启动序列（startup sequence）**：一个常驻服务进程从 `main()` 开始，到“真正开始对外服务”之间，要经过一连串初始化（读配置、连依赖、起子服务）。这些步骤有严格的先后依赖，本讲讲的就是这条依赖链。
- **ExtProc（External Processor）**：Envoy 提供的一种 gRPC 扩展机制。Envoy 把进入的 HTTP 请求 headers/body 通过 gRPC 流转发给 SR，由 SR 决定“改写、拒绝、放行”。SR 进程的核心就是起一个 ExtProc gRPC 服务。相关概念见 u1-l1。
- **API Server / 控制面（control plane）**：SR 除了一条处理推理流量的 ExtProc 数据面（默认 50051），还有一条管理面 HTTP API（默认 8080）。`/health`、`/ready`、`/startup-status` 都挂在管理面上。详见 u1-l4 端口约定。
- **`config.Replace` 与热重载**：u3-l3 讲过 SR 配置子系统有全局缓存与 `config.Replace` 热替换入口。启动序列会把首次加载的配置 `Replace` 进全局缓存，之后 K8s/热重载才能覆盖它。
- **Go 标准库 `flag` 包**：SR 用标准库 `flag` 解析命令行参数（不是 cobra/spf13）。`flag.Parse()` 后所有参数就绪。
- **`os.Exit` / fatal 语义**：本讲多次出现 “fatal”——指记录一条 fatal 日志后调用 `os.Exit(1)`，进程立刻退出，常用于不可恢复的启动错误（配置缺失、模型下载失败）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/semantic-router/cmd/main.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go) | 进程入口 `main()`，启动序列的“总调度”，以及模型下载、后端调优、启动摘要等核心逻辑。 |
| [src/semantic-router/cmd/runtime_bootstrap.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go) | `main()` 调用的各类启动辅助函数：选项解析、配置加载、startup writer、依赖初始化、ExtProc 构造/启动、就绪标记。 |
| [src/semantic-router/pkg/modeldownload/ensure.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/modeldownload/ensure.go) | 模型下载的真正实现：检查缺失模型、调用 huggingface-cli 下载。 |
| [src/semantic-router/pkg/startupstatus/status.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/startupstatus/status.go) | 启动状态 `State` 结构与文件/Redis 两种持久化 writer。 |
| [src/semantic-router/pkg/apiserver/route_startup_status.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_startup_status.go) | `/startup-status` HTTP handler，把持久化的状态读出来返回给调用方。 |
| [src/semantic-router/pkg/apiserver/routes_catalog.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/routes_catalog.go) | 管理面路由目录，可看到 `/startup-status` 的注册与权限策略。 |
| [src/semantic-router/pkg/extproc/server.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/server.go) | ExtProc gRPC 服务的构造（`NewServer`）与启动（`Start`）。 |

## 4. 核心概念与源码讲解

`main()` 是整条启动序列的骨架。先用一张伪代码总览把 13 个步骤的顺序钉死，再逐段精读：

```text
main():
 1. logo.PrintVLLMLogo()
 2. opts := parseRuntimeOptions()          // 解析 flag
 3. initializeRuntimeLogger()              // 日志
 4. applyBackendRuntimeTuningDefaults()    // candle 后端调优
 5. cfg := loadRuntimeConfigOrFatal(...)   // 加载配置（失败 fatal）
 6. config.Replace(cfg)                    // 写入全局缓存
 7. registry := routerruntime.NewRegistry  // 运行时依赖容器
 8. startupWriter := newStartupWriter(...) // 启动状态 writer
 9. startAPIServerIfEnabled(...)           // ★ API 提前启动（goroutine）
10. ensureModelsDownloadedOrFatal(...)     // ★ 模型下载（失败 fatal，带进度）
11. exitIfDownloadOnly(...)                // --download-only 直接退出
12. tracing / metrics / signal / metrics-server
13. embedding := initializeRuntimeDependencies(...)  // 嵌入等依赖（失败 fatal）
14. server := newExtProcServerOrFatal(...) // 构造 ExtProc 服务
15. warmupRouterRuntime(...)               // 热身（工具库/知识库）
16. markRouterReady(...)                   // ★ 标记 startup-status = ready
17. logStartupSummary(...)                 // startup_complete 日志
18. startKubernetesControllerIfNeeded(...) // K8s CRD 控制器（可选）
19. startExtProcServerOrFatal(...)         // ★ 阻塞起 ExtProc gRPC
```

这正是 [src/semantic-router/cmd/main.go:18-52](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L18-L52) 的逐行映射。下面按最小模块拆开讲。

---

### 4.1 选项解析与后端运行时调优

#### 4.1.1 概念说明

启动序列的第一件事不是读配置，而是**解析命令行选项**。SR 用 Go 标准库 `flag` 把“监听端口、配置路径、是否启用 API、是否只下载模型”等运行期旋钮暴露出来。这些选项决定了后续每一步的走向——例如 `--download-only` 会让进程在模型下载完成后直接退出，`--enable-api=false` 则跳过管理面。

紧跟在日志初始化之后，还有一个容易被忽略的步骤：**后端运行时调优**。当用户用 `EMBEDDING_BACKEND_OVERRIDE=candle` 选择 Rust candle 本地推理后端时，SR 会主动设置一批线程数环境变量，避免嵌入推理与宿主线程库（OpenMP/MKL/OpenBLAS/Rayon）抢核。

#### 4.1.2 核心流程

1. `parseRuntimeOptions()` 注册所有 flag 并 `flag.Parse()`，返回一个值类型 `runtimeOptions`。
2. `initializeRuntimeLogger()` 从环境变量初始化结构化日志。
3. `applyBackendRuntimeTuningDefaults()` 仅当 `EMBEDDING_BACKEND_OVERRIDE=candle` 时，对未显式设置的线程类环境变量填默认值。

> 设计要点：`boolFlagOverride` 对“是否远程暴露管理 API”做特殊处理——只有命令行**显式**传了该 flag 才覆盖，否则返回 `nil`，让配置文件的默认值得以保留。

#### 4.1.3 源码精读

选项解析与默认值见 [src/semantic-router/cmd/runtime_bootstrap.go:42-75](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L42-L75)——注意几个默认值：ExtProc gRPC 端口 `50051`、管理面端口 `8080`、指标端口 `9190`、配置默认路径 `config/config.yaml`。这些端口与 u1-l4 的约定一致（router 探 `8080/health`）。

后端调优见 [src/semantic-router/cmd/main.go:59-94](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L59-L94)：

```go
func applyBackendRuntimeTuningDefaults() {
	backend := strings.TrimSpace(strings.ToLower(os.Getenv("EMBEDDING_BACKEND_OVERRIDE")))
	if backend != "candle" {
		return
	}
	defaults := map[string]string{
		"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
		"OPENBLAS_NUM_THREADS": "1", "RAYON_NUM_THREADS": "1",
		"TOKENIZERS_PARALLELISM": "false",
	}
	// 仅对“未显式设置”的环境变量填默认值，尊重用户已有设置
	...
}
```

这段代码在 candle 分支下把多套线程库都限到单线程，避免在容器内与 Go runtime 抢 CPU。它只覆盖“用户没设”的变量——`os.LookupEnv` 区分“未设置”与“设为空”。

#### 4.1.4 代码实践

1. **实践目标**：摸清 SR 二进制的全部命令行旋钮。
2. **操作步骤**：阅读 [src/semantic-router/cmd/runtime_bootstrap.go:42-75](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L42-L75)，把每个 `flag.Xxx` 的名字、默认值、含义整理成表。若本地可构建（需 cgo），执行 `go run ./src/semantic-router/cmd --help` 观察输出。
3. **需要观察的现象**：`--help` 会列出 `--config/--port/--api-port/--api-bind/--management-auth-mode/--management-remote-exposure/--metrics-port/--enable-api/--secure/--cert-path/--kubeconfig/--namespace/--download-only` 全部 flag。
4. **预期结果**：表格与 `--help` 输出一一对应；`--download-only` 默认 `false`，`--enable-api` 默认 `true`。
5. 待本地验证：`go run` 是否能在你的环境通过 cgo 构建。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `boolFlagOverride` 要用 `fs.Visit` 判断“是否显式传入”，而不是直接用解析后的布尔值？
  - **答案**：因为布尔 flag 默认值是 `false`，若用户没传，解析结果也是 `false`，无法区分“没传”和“显式传了 false”。用 `Visit`（只遍历显式设置的 flag）才能判断；未传时返回 `nil`，把决定权交还给配置文件默认值。
- **练习 2**：`applyBackendRuntimeTuningDefaults` 在什么条件下**完全不做任何事**？
  - **答案**：当 `EMBEDDING_BACKEND_OVERRIDE` 不等于 `candle`（或为空）时直接 return；即使是 candle，若用户已显式设置了全部线程变量，`applied` 为空也会提前 return。

---

### 4.2 配置加载、热替换与运行时 Registry

#### 4.2.1 概念说明

选项就绪后，启动序列的下一步是**把磁盘上的 config.yaml 变成内存里的 `RouterConfig`**。这一步复用了 u3-l3 讲过的 `config.Parse`（先 map 后类型的分层解析 + 规范化 + 语义校验）。加载完成后立即 `config.Replace(cfg)`，把配置写入全局缓存——这是后续 ExtProc、API Server、K8s 控制器共享配置的**唯一真相源**。

紧接着创建 `routerruntime.Registry`。它是 ExtProc 数据面与 API 管理面之间**共享运行时依赖的线程安全容器**（嵌入 provider、向量库、记忆等都会注入其中）。本讲只把它当作“一个被提前建好的依赖桶”，内部结构留给 u4-l2。

#### 4.2.2 核心流程

1. `loadRuntimeConfigOrFatal`：先 `os.Stat` 检查文件存在性（不存在直接 fatal），再 `config.Parse` 解析；解析失败同样 fatal。
2. `config.Replace(cfg)`：写入全局缓存，使整进程的配置可被各子系统读取。
3. `routerruntime.NewRegistry(cfg)`：用配置构造依赖容器，后续 ExtProc/API 都拿它注入依赖。
4. `newStartupWriter`：构造启动状态 writer（先写一条 `phase=starting` 的初始状态）。

> 关键约束：配置加载失败属于不可恢复错误，走 fatal；而向量库、tracing 等可选依赖失败通常是 warn，不阻断启动。这种“严格核心 + 宽容可选”的分级是贯穿整个启动序列的设计哲学。

#### 4.2.3 源码精读

配置加载见 [src/semantic-router/cmd/runtime_bootstrap.go:99-119](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L99-L119)：

```go
func loadRuntimeConfigOrFatal(configPath string) *config.RouterConfig {
	if _, err := os.Stat(configPath); os.IsNotExist(err) {
		logging.ComponentFatalEvent("router", "runtime_config_missing", ...) // fatal
	}
	cfg, err := config.Parse(configPath)
	if err != nil {
		logging.ComponentFatalEvent("router", "runtime_config_load_failed", ...) // fatal
	}
	...
	return cfg
}
```

`main()` 中把配置写进全局缓存并创建 Registry，见 [src/semantic-router/cmd/main.go:24-28](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L24-L28)：

```go
cfg := loadRuntimeConfigOrFatal(opts.configPath)
config.Replace(cfg)
runtimeRegistry := routerruntime.NewRegistry(cfg)
startupWriter := newStartupWriter(cfg, opts.configPath)
```

startup writer 的后端选择见 [src/semantic-router/cmd/runtime_bootstrap.go:131-157](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L131-L157)：若 `startup_status.store_backend=redis` 且配了 Redis，就用 `RedisWriter`（多副本共享）；否则回退 `FileWriter`，并打一条 warn 说明“文件后端在多副本/容器化部署下不可被 dashboard 看到”。

#### 4.2.4 代码实践

1. **实践目标**：验证“配置缺失即 fatal”的行为，并理解 startup writer 的两种后端。
2. **操作步骤**：在 [src/semantic-router/cmd/runtime_bootstrap.go:99-119](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L99-L119) 中确认两处 fatal 事件名（`runtime_config_missing`、`runtime_config_load_failed`）。再到配置里查找 `startup_status` 段，确认它默认走 file 还是 redis。
3. **需要观察的现象**：把配置路径指向一个不存在的文件启动，进程应在打印 `runtime_config_missing` 后退出码非 0。
4. **预期结果**：进程不会带着空配置继续往下走，避免后续 ExtProc 用到 nil 配置崩溃。
5. 待本地验证：实际退出码与日志文本。

#### 4.2.5 小练习与答案

- **练习 1**：为什么要在 `config.Replace(cfg)` 之后才创建 Registry？
  - **答案**：Registry 的构造依赖配置（`NewRegistry(cfg)`），而 `Replace` 把配置固化为全局真相源。先固化配置、再用配置构造依赖容器，保证 Registry 拿到的是经过规范化/校验的最终配置；后续 ExtProc/API 从全局缓存读到的也是同一份。
- **练习 2**：startup writer 回退到文件后端时，为什么代码要专门打 warn？
  - **答案**：文件后端的状态只存在于本地磁盘，**不能跨副本共享、容器化部署下 dashboard 也读不到**。warn 是提示运维“生产环境应配置 `startup_status.store_backend: redis`”，避免静默地用了不可见的状态后端。

---

### 4.3 API Server 提前启动与 startup-status 状态机

#### 4.3.1 概念说明

这是本讲最重要的设计决策之一：**API Server 在模型下载和依赖初始化之前就启动**。原因很现实——模型下载可能耗时几十秒到几分钟，如果管理面也要等到全部就绪才起来，K8s 的 liveness/readiness 探针、dashboard、编排器在这段时间内完全看不到进程在干什么，只能判断“死了还是活着”，无法判断“正在下载哪个模型、还差几个”。

于是 SR 把 `/startup-status` 做成了一个**细粒度的启动状态机**：进程即使在“下载模型”阶段，也能返回 503 + `{phase: "downloading_models", downloading_model: "...", ready_models: 2, total_models: 5}`，让外部知道“进程健康、只是在忙”。等到 `markRouterReady` 把状态写成 `ready: true`，端点才返回 200。

#### 4.3.2 核心流程

启动状态机贯穿整条序列，`Phase` 字段取值与转移如下：

```text
starting                       (newStartupWriter 初始写入)
   │
   ▼
checking_models  ──下载中──▶  downloading_models     (ensureModelsDownloaded reporter)
   │                                                 │
   │ ◀──无模型/已就绪── completed/skipped ───────────┘
   ▼
initializing_models            (initializeRuntimeDependencies)
   │
   ▼
ready (Ready=true)             (markRouterReady)   ──▶ /startup-status 返回 200
   │
   ▼ （任意 fatal）
error (Ready=false)            (failStartup)
```

`Phase` 字段定义见 [src/semantic-router/pkg/startupstatus/status.go:14-24](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/startupstatus/status.go#L14-L24) 的 `State` 结构（含 `Ready/Phase/Message/DownloadingModel/ReadyModels/TotalModels/EmbeddingProvider` 等字段）。

1. `startAPIServerIfEnabled` 在 **goroutine** 里调用 `apiserver.InitWithOptions`，非阻塞地起管理面。
2. 管理面注册了 `/startup-status` 路由，handler 读取 writer 持久化的最新状态。
3. `Ready=false` 时 handler 返回 **503**，`Ready=true` 时返回 **200**——天然适配 K8s readiness 探针。

#### 4.3.3 源码精读

API 提前启动见 [src/semantic-router/cmd/runtime_bootstrap.go:464-490](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L464-L490)：

```go
func startAPIServerIfEnabled(opts runtimeOptions, runtimeRegistry *routerruntime.Registry) {
	if !opts.enableAPI { return }
	go func() {   // 非阻塞：不拖住主序列
		...
		if err := apiserver.InitWithOptions(apiserver.InitOptions{
			ConfigPath: opts.configPath, Port: opts.apiPort, BindAddress: opts.apiBind,
			RemoteExposure: opts.managementRemoteExpose, AuthMode: opts.managementAuthMode,
			RuntimeRegistry: runtimeRegistry,
		}); err != nil { ... }
	}()
}
```

它在 `main()` 中的调用位置——**早于**模型下载，见 [src/semantic-router/cmd/main.go:30-34](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L30-L34)：

```go
// Start the API server early so /startup-status is available during
// model downloads and initialization.
startAPIServerIfEnabled(opts, runtimeRegistry)
ensureModelsDownloadedOrFatal(cfg, startupWriter)
```

`/startup-status` 路由注册见 [src/semantic-router/pkg/apiserver/routes_catalog.go:17-21](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/routes_catalog.go#L17-L21)（权限 `PermReadyRead`、敏感度 `SensitivityOperational`）。

handler 的 503/200 语义见 [src/semantic-router/pkg/apiserver/route_startup_status.go:15-37](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_startup_status.go#L15-L37)：

```go
func (s *ClassificationAPIServer) handleStartupStatus(w http.ResponseWriter, _ *http.Request) {
	state := s.loadStartupState()
	if state == nil { w.WriteHeader(http.StatusServiceUnavailable); ...; return }
	...
	status := http.StatusOK
	if !state.Ready { status = http.StatusServiceUnavailable }   // 未就绪→503
	w.WriteHeader(status)
	_, _ = w.Write(payload)
}
```

`loadStartupState` 会按 redis/file 后端读取最新状态——它读的正是 `ensureModelsDownloaded` / `markRouterReady` 写进去的那份 `State`。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到 `/startup-status` 在“未就绪”时返回 503 与阶段信息。
2. **操作步骤**：阅读 [src/semantic-router/pkg/apiserver/route_startup_status.go:15-37](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/apiserver/route_startup_status.go#L15-L37) 与 [src/semantic-router/pkg/startupstatus/status.go:14-24](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/startupstatus/status.go#L14-L24)。若本地能起服务，在模型尚未下载完成时 `curl -i localhost:8080/startup-status`。
3. **需要观察的现象**：HTTP 状态码为 503，JSON 含 `"phase":"downloading_models"`、`"ready_models"`、`"total_models"`；进程完全就绪后变为 200、`"phase":"ready"`。
4. **预期结果**：探针/编排器可据 503 判断“未就绪但不等于崩溃”，避免误重启。
5. 待本地验证：本地起服与下载耗时。

#### 4.3.5 小练习与答案

- **练习 1**：`startAPIServerIfEnabled` 为什么要放在 goroutine 里、且放在模型下载之前？
  - **答案**：放 goroutine 是为了**不阻塞主序列**（下载、初始化要继续往下走）；放在下载之前是为了让 `/startup-status` **在漫长的下载期间就可用**，外部能持续看到下载进度而非“黑洞”。
- **练习 2**：`/startup-status` 返回 503 时，进程其实可能是健康的。这种设计如何兼顾“可用性”与“就绪性”？
  - **答案**：503 表示“**尚未就绪**”而非“进程死亡”。liveness 探针应探 `/health`（进程级存活），readiness 探针探 `/ready` 或 `/startup-status`（业务级就绪）。这样下载期间 liveness 不重启进程、readiness 暂时不导流量，两全其美。

---

### 4.4 模型下载、进度上报与 fatal 语义

#### 4.4.1 概念说明

SR 在本地推理（非 API-only）模式下需要一批模型：嵌入模型、分类器、PII/越狱检测等。这些模型通常从 HuggingFace 拉取。启动序列用一个 `EnsureModelsForConfigWithProgress` 函数统一处理：先算出配置需要哪些模型，再检查哪些本地缺失，缺失的才调用 `huggingface-cli` 下载，并把进度通过回调实时写进 startup-status。

这里有一条严格的**fatal 语义**：模型下载失败 = 进程不能正常服务 = 直接 fatal 退出。与之配套的是 `--download-only`：仅下载、校验后 `os.Exit(0)`，常用于 CI/镜像构建阶段提前把模型烤进镜像，运行时不再下载。

#### 4.4.2 核心流程

`EnsureModelsForConfigWithProgress` 的决策树（[src/semantic-router/pkg/modeldownload/ensure.go:17-86](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/modeldownload/ensure.go#L17-L86)）：

```text
BuildModelSpecs(cfg)                    // 算出所需模型清单
  ├── 清单为空？ ──是──▶ 报 phase=skipped（api_only 模式），return nil
  └── GetMissingModels(specs)
        ├── 全部已存在？ ──是──▶ 报 phase=completed，return nil（无需 huggingface-cli）
        └── 存在缺失？
              ├── CheckHuggingFaceCLI()        // 仅当真有缺失才要求 CLI
              └── EnsureModelsWithProgress()    // 下载，逐个回调进度
```

进度回调（reporter）把 `modeldownload.ProgressState` 翻译成 `startupstatus.State`，见 [src/semantic-router/cmd/main.go:96-131](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L96-L131)：`phase=downloading`→`downloading_models`，`completed/skipped`→`initializing_models`。

下载进度上报的“剩余模型数”可用一个简单比例衡量就绪程度（设总模型数 \(N\)、已就绪 \(r\)，则下载完成度 \(c = r/N\)，\(c \in [0,1]\)）：

\[
c = \frac{r}{N}
\]

当 \(c=1\) 时全部就绪；reporter 在每个模型下载完更新 \(r\)，dashboard 据此渲染进度条。

#### 4.4.3 源码精读

`main()` 中的下载与 fatal 包装见 [src/semantic-router/cmd/main.go:34-35](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L34-L35)：

```go
ensureModelsDownloadedOrFatal(cfg, startupWriter)
exitIfDownloadOnly(opts.downloadOnly)
```

`ensureModelsDownloadedOrFatal` 把“下载返回 error”转成 fatal，见 [src/semantic-router/cmd/runtime_bootstrap.go:182-186](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L182-L186)；它内部调用 `failStartup`（写 `phase=error` 后 fatal），见 [src/semantic-router/cmd/runtime_bootstrap.go:170-180](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L170-L180)。

`--download-only` 的提前退出见 [src/semantic-router/cmd/runtime_bootstrap.go:188-197](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L188-L197)：`os.Exit(0)`。

> 一个容易踩坑的点：`EnsureModelsForConfigWithProgress` 只有在“确有缺失模型”时才要求 `huggingface-cli` 存在（见 [src/semantic-router/pkg/modeldownload/ensure.go:65-67](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/modeldownload/ensure.go#L65-L67)）。这样“模型已预挂载进容器”的环境即使没装 CLI 也能启动。

#### 4.4.4 代码实践

1. **实践目标**：理解“下载失败即 fatal”与“download-only 提前退出”两条路径。
2. **操作步骤**：在 [src/semantic-router/pkg/modeldownload/ensure.go:17-86](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/modeldownload/ensure.go#L17-L86) 中标出三处 return：`specs==0`（skip）、`missing==0`（completed）、下载成功；再追踪 [src/semantic-router/cmd/runtime_bootstrap.go:182-197](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L182-L197) 看 error 如何被转成 fatal。
3. **需要观察的现象**：构造一个“模型清单非空但全部已存在”的场景，进程应跳过 CLI 检查、报 completed、继续启动；若故意制造下载失败，进程应在写 `phase=error` 后退出。
4. **预期结果**：`--download-only` 模式下，下载成功后进程打印 `download_only_complete` 并以 0 退出，不会起 ExtProc。
5. 待本地验证：本地是否有 `huggingface-cli`、网络是否能拉模型。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `CheckHuggingFaceCLI` 要放在 `GetMissingModels` 之后，而不是函数开头？
  - **答案**：如果所有模型都已本地就绪（`missing==0`），根本不需要下载，也就不需要 CLI。延后检查让“模型已预挂载”的环境（如镜像里烤好模型）即使没装 `huggingface-cli` 也能正常启动，降低部署门槛。
- **练习 2**：`--download-only` 在 CI 里有什么用？
  - **答案**：在镜像构建/CI 阶段用 `--download-only` 把所有模型提前下载好并固化进镜像或缓存卷，正式运行时 `GetMissingModels` 返回空、秒级跳过下载，既加快启动、又避免运行时依赖外网拉模型。

---

### 4.5 运行时依赖初始化、热身与 ExtProc 起服

#### 4.5.1 概念说明

模型下载完，启动序列进入“依赖初始化”阶段：用 `modelruntime.PrepareRouterRuntime` 初始化嵌入 provider、加载分类器、模态分类等。这一步失败同样 fatal（没有嵌入，路由的信号/投影/缓存全部无法工作）。

之后是**热身（warmup）**：在正式接流量前，预加载工具库（tools database）和知识库（knowledge bases），避免第一个请求承受冷启动延迟。热身是“尽力而为”的——嵌入未就绪时跳过并记录原因，不 fatal。

最后两步是整条序列的收口：`markRouterReady` 把 startup-status 写成 `ready=true`（这是 `/startup-status` 从 503 翻成 200 的关键时刻），打印 `startup_complete` 摘要日志，然后 `startExtProcServerOrFatal` **阻塞**地起 gRPC 服务——从此进程进入“服务态”，`main()` 不再返回。

#### 4.5.2 核心流程

```text
initializeRuntimeDependencies
  ├── PrepareRouterRuntime(ctx, cfg, opts)   // 嵌入/分类器/模态分类（失败 fatal）
  └── initializeVectorStoreIfEnabled          // 向量库（按需，注入 Registry）
        ▼
newExtProcServerOrFatal                        // extproc.NewServer 构造（失败 fatal）
        ▼
warmupRouterRuntime                            // 预载 tools/knowledge（best-effort，可跳过）
        ▼
markRouterReady                                // ★ startup-status = ready
        ▼
logStartupSummary                              // startup_complete 事件（端口/决策/缓存等）
        ▼
startKubernetesControllerIfNeeded              // ConfigSource==kubernetes 时起 CRD 控制器
        ▼
startExtProcServerOrFatal                      // ★ server.Start() 阻塞，进入服务态
```

`extproc.NewServer` 在构造期就建好 `OpenAIRouter` 并把它发布到 Registry（见 [src/semantic-router/pkg/extproc/server.go:80-103](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/server.go#L80-L103)），这样 API 管理面也能通过 Registry 拿到 router 实例做分类/评估。`Start()` 则真正 `net.Listen` 并 serve gRPC（[src/semantic-router/pkg/extproc/server.go:111](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/server.go#L111)）。

#### 4.5.3 源码精读

依赖初始化见 [src/semantic-router/cmd/runtime_bootstrap.go:300-330](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L300-L330)：

```go
embeddingState, err := modelruntime.PrepareRouterRuntime(context.Background(), cfg, modelruntime.PrepareRouterRuntimeOptions{
	Component: "router", MaxParallelism: modelruntime.DefaultParallelism(5),
	OnEvent: logRuntimeLifecycleEvent, InitModalityClassifierFunc: extproc.InitModalityClassifier,
})
if err != nil { failStartup(writer, "Failed to initialize runtime dependencies: %v", err) }
...
initializeVectorStoreIfEnabled(cfg, shutdownHooks, runtimeRegistry)
return embeddingState
```

ExtProc 构造、热身、就绪、起服见 [src/semantic-router/cmd/main.go:44-52](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L44-L52)：

```go
embeddingRuntime := initializeRuntimeDependencies(cfg, startupWriter, &shutdownHooks, runtimeRegistry)
server := newExtProcServerOrFatal(opts, startupWriter, runtimeRegistry)
warmupRouterRuntime(server, embeddingRuntime)
markRouterReady(startupWriter, startupEmbeddingProviderStatus(embeddingRuntime))
logStartupSummary(cfg, opts, embeddingRuntime.AnyReady)
startKubernetesControllerIfNeeded(cfg, opts.kubeconfig, opts.namespace)
startExtProcServerOrFatal(server, startupWriter)
```

热身任务清单见 [src/semantic-router/cmd/runtime_bootstrap.go:439-462](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L439-L462)（`tools_database` 依赖 `ToolsReady`、`knowledge_bases` 依赖 `AnyReady`，未就绪则带 `SkipReason` 跳过）。就绪标记见 [src/semantic-router/cmd/runtime_bootstrap.go:492-499](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/runtime_bootstrap.go#L492-L499)。`startup_complete` 摘要见 [src/semantic-router/cmd/main.go:180-199](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L180-L199)，含 `extproc_port/api_port/metrics_port/decisions/embedding_ready/sem_cache_enabled` 等字段。

#### 4.5.4 代码实践

1. **实践目标**：定位“进程正式就绪”与“进入服务态”这两个关键转折点。
2. **操作步骤**：在 [src/semantic-router/cmd/main.go:44-52](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L44-L52) 标注：`markRouterReady`（让 `/startup-status` 变 200）与 `startExtProcServerOrFatal`（阻塞 serve gRPC）。再到 [src/semantic-router/pkg/extproc/server.go:80-103](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/server.go#L80-L103) 确认 `NewServer` 在构造期已把 router 发布进 Registry。
3. **需要观察的现象**：进程日志依次出现 `runtime_lifecycle_task_skipped`（若热身跳过）、`startup_complete`，随后进程持续运行不退出（serve 阻塞）。
4. **预期结果**：`startup_complete` 后，`curl localhost:8080/startup-status` 返回 200 且 `phase=ready`；Envoy 此时即可把流量导给 50051。
5. 待本地验证：本地起服后的实际日志顺序。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `markRouterReady` 在 `startExtProcServerOrFatal` **之前**？
  - **答案**：`markRouterReady` 把 startup-status 写成 ready，让探针/编排器认为“可以导流量了”。如果先起 ExtProc 再标记就绪，会出现“gRPC 已在监听但 readiness 仍 503”的窗口，编排器可能误判；反之先标记就绪、再阻塞起服，gap 极小，且 ExtProc 构造（`NewServer`）已完成、router 已就位。
- **练习 2**：热身（warmup）失败会 fatal 吗？为什么？
  - **答案**：不会。热身是 best-effort：嵌入未就绪时按 `SkipReason` 跳过（`tools_database`/`knowledge_bases`），失败的 best-effort 任务只记 warn。这样“工具库/知识库加载失败”不会阻断核心路由服务，首请求最多承受一点冷启动延迟。

---

## 5. 综合实践

**任务**：把 `main()` 的 13 个启动步骤画成一张时序图，并标注三类关键节点。

1. **画时序图**：以 [src/semantic-router/cmd/main.go:18-52](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L18-L52) 为准，从 `logo.PrintVLLMLogo` 到 `startExtProcServerOrFatal` 画一条纵向时间轴，标出每一步调用的函数名与所在文件。
2. **标注“/startup-status 何时可用”**：标出 `startAPIServerIfEnabled`（端点开始可访问，初始返回 503 `phase=starting`）与 `markRouterReady`（翻成 200 `phase=ready`）两个节点。
3. **标注“哪一步会 fatal”**：用红色标出所有 fatal 点——配置缺失/解析失败（`loadRuntimeConfigOrFatal`）、模型下载失败（`ensureModelsDownloadedOrFatal`）、依赖初始化失败（`initializeRuntimeDependencies`）、ExtProc 构造/启动失败（`newExtProcServerOrFatal` / `startExtProcServerOrFatal`）。
4. **标注“哪一步可能跳过/提前退出”**：`exitIfDownloadOnly`（`--download-only`）、`initializeVectorStoreIfEnabled`（未启用向量库时跳过）、`startKubernetesControllerIfNeeded`（仅 `ConfigSource==kubernetes`）、warmup 的 best-effort 跳过。
5. **自检问题**：假设模型下载到一半失败，时序图上会发生什么？—— 进程会在 `ensureModelsDownloadedOrFatal` → `failStartup` 处写 `phase=error` 并退出；此时 `/startup-status` 早已可用（API 已提前启动），外部能读到 `phase=error` 的失败原因，而不是单纯的“连接拒绝”。

> 完成后，把这张时序图与第 4 节各模块的源码精读对照，确保每个箭头都能对应到一行真实代码。这是后续阅读 u4-l2（Registry）与 u4-l3（ExtProc Server）的导航图。

## 6. 本讲小结

- `main()` 是一条**严格有序**的启动序列：选项 → 配置 → Registry → startup writer → API 提前起 → 模型下载 → 可观测性 → 依赖初始化 → ExtProc 构造 → 热身 → 标记就绪 → 摘要 → K8s → 起 ExtProc。
- **配置加载是 fatal 级**：文件缺失或解析失败直接退出，绝不让进程带空配置继续。
- **API Server 提前启动**：在模型下载前就起管理面，使 `/startup-status` 在漫长的下载/初始化期间持续可用，返回 503 + 细粒度阶段信息。
- **startup-status 是一个状态机**：`starting → checking_models → downloading_models → initializing_models → ready`（或 `error`），由 writer 持久化、由 handler 以 503/200 暴露。
- **模型下载有 fatal 语义与 download-only 逃生口**：下载失败即退出；`--download-only` 供 CI 提前烤模型。
- **严格核心 + 宽容可选**：配置/模型/嵌入/ExtProc 失败 fatal，tracing/向量库/warmup 失败仅 warn，分级清晰。

## 7. 下一步学习建议

- **u4-l2 Runtime Registry**：本讲把 `routerruntime.NewRegistry` 当作“依赖桶”，下一讲深入它如何被 ExtProc 与 API Server 共享、`Set*/Get*` 注入了哪些依赖。
- **u4-l3 ExtProc Server**：本讲只到 `extproc.NewServer` / `Start`，下一讲拆解 `OpenAIRouter` 状态机与 Envoy 的 headers→body→response gRPC 交互模型。
- **u11-l4 可观测性**：本讲出现的 `initializeTracing` / `startMetricsServerIfEnabled` / `startup_complete` 事件，将在可观测性讲义里系统讲解。
- **u12-l3 Operator 与 CRD**：本讲末尾的 `startKubernetesControllerIfNeeded` 是 K8s 模式下的配置热更新入口，配合 u3-l3 的 `config.Replace` 理解热重载闭环。
