# Runtime Registry：运行时依赖容器

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `routerruntime.Registry` 是什么、它解决的是什么问题。
- 列出 Registry 托管的 6 类运行时依赖，以及每一类的读/写方。
- 解释 ExtProc（请求路径）与 API Server（管理路径）如何通过**同一个** Registry 实例共享**实时**依赖，而不是各自维护一份。
- 理解「窄接口缝（narrow seam）」设计：为什么 `LearningRuntime` 只暴露一个 `UpdateOutcome` 方法，而把笨重的实现留在 extproc 内部。
- 看懂配置热更新（reload）时，Registry 如何用「先校验再切换」的方式保证不发布半成品状态。

本讲是 u4-l1（启动序列）的承接：u4-l1 讲了 `main()` 的启动顺序，本讲聚焦其中那一个被反复传递的 `*routerruntime.Registry` 指针到底装了什么、谁在写它、谁在读它。

## 2. 前置知识

阅读本讲前，建议你已经建立以下概念（来自 u1、u3、u4-l1）：

- **两个进程入口**：Go 路由器进程里同时跑着两条服务——面向 Envoy 的 **ExtProc gRPC 服务**（处理真实推理请求）和面向运维/面板的 **API Server**（HTTP 管理面，端口 8080）。它们在同一个进程内，但职责不同。
- **依赖（dependency）**：指那些「构造昂贵、需要在请求间复用」的运行时对象，例如分类服务、向量库、记忆库、模型选择器。它们不能每个请求都新建一次。
- **线程安全**：Go 里用 `sync.RWMutex`（读写锁）保护被多个 goroutine 并发读写的字段——读用 `RLock()`，写用 `Lock()`。
- **控制面 / 数据面**：API Server 是控制面（改配置、查状态、灌学习结果），ExtProc 是数据面（每个请求都走它）。本讲的核心痛点就是：控制面要能**看到并操作**数据面正在用的那些活对象。
- **热重载（reload）**：配置变更后，路由器会用新配置重建一套依赖，再原子地替换旧的那套（见 u4-l1）。

如果你还不熟悉这些，可以先回去看 u4-l1 的启动序列图。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/semantic-router/pkg/routerruntime/registry.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/registry.go) | `Registry` 容器本体：6 类依赖字段 + 读写锁 + 一组成对的 Get/Set 方法 + 两个便捷方法。 |
| [src/semantic-router/pkg/routerruntime/learning_outcome.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/learning_outcome.go) | `LearningRuntime`/`OutcomeRuntime` 窄接口与 `RouterOutcome` 数据类型——Registry 里唯一一个「接口」型依赖。 |
| [src/semantic-router/pkg/routerruntime/vectorstore_runtime.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/vectorstore_runtime.go) | `VectorStoreRuntime`：被 Registry 托管的向量库运行时，聚合了 FileStore/Backend/Manager/Pipeline/Embedder 五件套。 |
| [src/semantic-router/pkg/routerruntime/registry_test.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/registry_test.go) | 三个单元测试，正好把 Registry 的「原子发布 / 拒绝半成品刷新 / 模型选择器发布」三条关键行为锁死。 |
| [src/semantic-router/pkg/extproc/server.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go) | ExtProc 这一侧：`publishRouterState` 把路由器对象**写进** Registry。 |
| [src/semantic-router/pkg/apiserver/runtime_dependencies.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/runtime_dependencies.go) | API Server 这一侧：一组 `current*()` 方法从 Registry **读**实时依赖。 |
| [src/semantic-router/cmd/runtime_bootstrap.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go) | 启动序列里 Registry 的构造点与注入点。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Registry 容器**——它长什么样、靠什么做到线程安全。
2. **共享依赖**——ExtProc 写、API Server 读的分工，以及配置回写的反向通路。
3. **学习运行时接口**——`LearningRuntime` 这个「窄接口缝」为何只露一个方法。

### 4.1 Registry 容器：线程安全的依赖仓库

#### 4.1.1 概念说明

设想一个没有 Registry 的世界：ExtProc 内部建好了分类服务、记忆库、向量库……而 API Server 想给面板展示「当前命中了哪个路由」，或者想让用户通过 HTTP 上传一个知识库文档。要做到这些，API Server 必须拿到 ExtProc 正在用的那**同一批**对象——否则它操作的只是一份没人用的副本。

最朴素的办法是给每个对象配一个全局单例（`GetGlobalXxx()`）。项目里确实保留了一些这种全局函数作为兜底，但全局单例有一堆老问题：谁都能写、初始化顺序难控、测试不好隔离。

`routerruntime.Registry` 是更克制的替代方案：**一个**显式的、可传递的容器，集中托管这一批依赖。源码注释把它定位得很精确——「startup、reload、extproc 与 API server 共同持有的、运行时拥有的窄依赖缝（narrow runtime-owned dependency seam）」。也就是说，它不是一个大杂烩仓库，而是一条被刻意收窄的共享通道。

#### 4.1.2 核心流程

Registry 的运行流程可以概括为「一处构造、多方读写、读写都加锁」：

```text
main() 启动
  ├── NewRegistry(initialCfg)            // 1. 只装一个 config，其余字段为 nil
  ├── 把 *Registry 同时传给 ExtProc Server 和 API Server
  │
  ├── [ExtProc 侧] 路由器构建完成后
  │     publishRouterState(...)
  │       ├── PublishRouterRuntime(cfg, classificationSvc, memoryStore)  // 原子写 3 项
  │       ├── SetModelSelector(selector)
  │       └── SetLearningRuntime(learning)
  │
  ├── [Bootstrap 侧] 若开启向量库
  │     SetVectorStoreRuntime(vectorStoreRuntime)
  │
  └── [API Server 侧] 每次处理管理请求时
        currentMemoryStore() / currentVectorStoreManager() / ...
          └── 读 Registry 里「此刻」的指针，自动看到 reload 后的新对象
```

线程安全靠一把 `sync.RWMutex`：所有读方法走 `RLock()`（可并发读），所有写方法走 `Lock()`（独占写）。此外每个方法开头都有 `if r == nil { return nil }` 的防御——即便调用方拿到一个未初始化的指针也不会 panic。

#### 4.1.3 源码精读

先看容器本体。Registry 是一个普通 struct，6 个字段对应 6 类依赖，外加一把读写锁：

这是 Registry 的结构定义，注释说明它是多方共享的窄依赖缝——见 [registry.go:13-23](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/registry.go#L13-L23)：

```go
// Registry is the narrow runtime-owned dependency seam shared by startup,
// reload, extproc, and the API server.
type Registry struct {
	mu                    sync.RWMutex
	config                *config.RouterConfig
	classificationService *services.ClassificationService
	memoryStore           memory.Store
	vectorStore           *VectorStoreRuntime
	modelSelector         *selection.Registry
	learningRuntime       LearningRuntime
}
```

6 个字段分别是：当前配置、分类服务、记忆库、向量库运行时、模型选择器注册表、学习运行时。注意 `learningRuntime` 的类型是**接口** `LearningRuntime`，其余 5 个都是**具体指针**——这个区别是模块 4.3 的重点。

构造函数非常朴素，只塞一个 config，其余留空（等 ExtProc 构建完路由器后再回填）：

```go
func NewRegistry(cfg *config.RouterConfig) *Registry {
	return &Registry{config: cfg}
}
```

读方法都遵循同一个模板：nil 防御 → `RLock` → 返回字段。以 `ClassificationService()` 为例——见 [registry.go:54-61](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/registry.go#L54-L61)：

```go
func (r *Registry) ClassificationService() *services.ClassificationService {
	if r == nil {
		return nil
	}
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.classificationService
}
```

对应的写方法 `SetClassificationService` 把 `RLock` 换成 `Lock`，见 [registry.go:63-70](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/registry.go#L63-L70)。其余 5 类依赖的 Get/Set 对完全同构。

> 命名小细节：config 这一类没有叫 `SetConfig`/`GetConfig`，而是 `UpdateConfig`/`CurrentConfig`——语义上 config 是「当前快照」而非「注入的组件」。但它本质上仍是一对读写方法。

真正值得单独看的是两个**多字段便捷方法**。第一个是 `PublishRouterRuntime`，它在**一次加锁**内同时写 config、classificationService、memoryStore 三项——见 [registry.go:144-159](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/registry.go#L144-L159)：

```go
func (r *Registry) PublishRouterRuntime(
	cfg *config.RouterConfig,
	classificationService *services.ClassificationService,
	memoryStore memory.Store,
) {
	...
	r.mu.Lock()
	if cfg != nil {
		r.config = cfg
	}
	r.classificationService = classificationService
	r.memoryStore = memoryStore
	r.mu.Unlock()
}
```

为什么要「一次加锁写三项」？因为这三个对象来自**同一次**路由器构建，必须作为一个一致的整体对外可见。如果分成三次 `Set*`，API Server 可能在两次写之间读到「新 config + 旧分类服务」的错配状态。这种「原子发布」是 Registry 最关键的不变式之一，也被单元测试明确锁定——见 [registry_test.go:12-28](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/registry_test.go#L12-L28) 的 `TestRegistryPublishRouterRuntime`：发布后 `CurrentConfig()` 必须立刻指向新 cfg。

第二个便捷方法是 `RefreshRuntimeConfig`，它体现「先校验、再切换」的安全刷新——见 [registry.go:161-175](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/registry.go#L161-L175)：

```go
func (r *Registry) RefreshRuntimeConfig(newCfg *config.RouterConfig) {
	if r == nil {
		return
	}
	if service := r.ClassificationService(); service != nil {
		if err := service.TryRefreshRuntimeConfig(newCfg); err != nil {
			logging.Errorf("Runtime config refresh rejected; retaining previous registry snapshot: %v", err)
			return                       // ← 关键：校验失败就整笔回滚，不动 config
		}
	}
	r.UpdateConfig(newCfg)
}
```

注意它**先**让分类服务尝试刷新（`TryRefreshRuntimeConfig`），**只有成功了**才更新 config。一旦分类服务拒绝（例如新配置引用了不存在的信号），Registry 会原样保留上一份快照，并记一条 error 日志。这避免了「config 已经切到新版、但分类服务还停在旧版」的撕裂。该行为由 [registry_test.go:30-47](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/registry_test.go#L30-L47) 的 `TestRegistryRejectsPartialClassifierRefresh` 锁定：刷新失败后 config 与分类服务都不得变动。

#### 4.1.4 代码实践

**实践目标**：亲手清点 Registry 的全部公开读写方法，建立「6 类依赖 × 读/写」的完整心智表。

**操作步骤**：

1. 打开 [registry.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/registry.go)。
2. 把所有以 `func (r *Registry)` 开头、导出（大写开头）的方法挑出来，分成「读方法」「写方法」「便捷方法」三组。
3. 画一张表，行为 6 类依赖（config / classificationService / memoryStore / vectorStore / modelSelector / learningRuntime），列分别为「读方法」「写方法」。

**需要观察的现象**：

- 6 类依赖里，**5 类**有成对的「单个字段 Get/Set」；唯独 `config` 用的是 `CurrentConfig`/`UpdateConfig` 命名。
- 两个便捷方法 `PublishRouterRuntime` 和 `RefreshRuntimeConfig` 不属于任何单一字段，而是跨字段操作。
- 每个方法的第一行都是 `if r == nil { return ... }`。

**预期结果**（参考答案，下表）：

| 依赖 | 读方法 | 写方法 |
| --- | --- | --- |
| config | `CurrentConfig` | `UpdateConfig` |
| classificationService | `ClassificationService` | `SetClassificationService` |
| memoryStore | `MemoryStore` | `SetMemoryStore` |
| vectorStore | `VectorStoreRuntime` | `SetVectorStoreRuntime` |
| modelSelector | `ModelSelector` | `SetModelSelector` |
| learningRuntime | `LearningRuntime` | `SetLearningRuntime` |

便捷方法：`PublishRouterRuntime(cfg, svc, store)`、`RefreshRuntimeConfig(cfg)`。

#### 4.1.5 小练习与答案

**练习 1**：为什么所有读方法都用 `RLock()` 而不是 `Lock()`？

**参考答案**：`RLock()` 是读锁，允许多个 goroutine 同时持有；`Lock()` 是写锁，独占。API Server 处理并发请求时会频繁读 Registry，如果每次读都上写锁，这些请求会串行化，成为瓶颈。读多写少的场景用读写锁能显著提升吞吐，而写（reload、配置发布）很少发生，独占写不影响整体性能。

**练习 2**：`PublishRouterRuntime` 为什么要先把三项字段在 `r.mu.Lock()` 之内一次性赋值，而不是调用三次 `SetClassificationService` / `SetMemoryStore` / `UpdateConfig`？

**参考答案**：因为这三项来自同一次路由器构建，是一个「一致的整体」。分三次写会在两次加锁之间留下窗口，并发读的 API Server 可能读到「新 config + 旧 service」的错配快照。一次加锁内原子赋值，保证对外要么全旧、要么全新。

---

### 4.2 共享依赖：ExtProc 与 API Server 的读写分工

#### 4.2.1 概念说明

Registry 的价值只有在「两个消费者」都接上之后才看得出来。这两个消费者有明确的分工：

- **ExtProc（数据面）= 生产者**：它在构建/重建路由器时，把活的对象**写进** Registry。
- **API Server（控制面）= 消费者**：它处理管理请求时，从 Registry **读**当前的对象。每次请求都现读一次，所以 reload 之后自动看到新对象。
- **还有一条反向通路**：当用户通过 API Server 部署新配置（ETag 那一套，见 u11-l1）时，API Server 会把新 config **写回** Registry，并触发分类服务刷新。

这种设计让 API Server 能操作 ExtProc「正在用的那批活对象」，而两个包之间只共享一个 `*Registry` 指针和 `routerruntime` 包里定义的窄类型——API Server **不需要** import extproc 的内部结构。

> 一个细节：当 Registry 为 nil（旧的、未接入 Registry 的启动路径）时，代码会退回到全局单例（`services.GetGlobalClassificationService()`、`memory.SetGlobalMemoryStore()` 等）。也就是说 Registry 是取代全局单例的现代路径，全局函数只是兜底。

#### 4.2.2 核心流程

```text
                ┌────────────── 写入(发布) ──────────────┐
                │                                         │
                ▼                                         │
        ┌───────────────┐   读取(current*)   ┌────────────┴──────┐
        │   Registry    │ ◄──────────────── │   API Server      │
        │  (6 类依赖)   │                   │  (控制面/管理)    │
        └───────────────┘                   └───────────────────┘
                ▲                                         │
                │ 写入(SetVectorStoreRuntime)             │ 写回(UpdateConfig +
                │                                         │   RefreshRuntimeConfig)
        ┌───────┴─────────┐                               │
        │  Bootstrap(main)│ ◄──────── 配置部署 ETag ──────┘
        └─────────────────┘
                ▲
                │ 写入(PublishRouterRuntime /
                │      SetModelSelector /
                │      SetLearningRuntime)
        ┌───────┴─────────┐
        │   ExtProc       │
        │  (数据面/请求)  │
        └─────────────────┘
```

三个写入方写不同字段；API Server 是主要读取方，同时也是 config 字段的回写方。

#### 4.2.3 源码精读

**ExtProc 这一侧：把路由器写进 Registry。** 在 [server.go:345-361](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L345-L361) 的 `publishRouterState` 里，ExtProc 把刚构建好的路由器的几样核心对象一次性发布：

```go
func publishRouterState(cfg *config.RouterConfig, router *OpenAIRouter, runtimeRegistry *routerruntime.Registry) {
	...
	if runtimeRegistry != nil {
		runtimeRegistry.PublishRouterRuntime(cfg, router.ClassificationService, router.MemoryStore)
		runtimeRegistry.SetModelSelector(router.ModelSelector)
		runtimeRegistry.SetLearningRuntime(router.routerLearningRuntimeState())
		return
	}
	// Registry 为 nil 的兜底：退回全局单例
	services.SetGlobalClassificationService(router.ClassificationService)
	memory.SetGlobalMemoryStore(router.MemoryStore)
}
```

这个函数在两个时机被调用：启动时（[server.go:92](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L92)）和 reload 时（[server.go:286](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L286)）。注意 reload 路径在重建路由器后还会先 `attachRuntimeRegistry` 把新路由器指针绑回 Registry（[server.go:268](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L268)、[server.go:338-343](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L338-L343)）。

`OpenAIRouter` 自身也持有 Registry 的反向引用，方便请求路径里按需取用——见 [router.go:64-66](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router.go#L64-L66)：

```go
// RuntimeRegistry exposes runtime-owned services without forcing request-time
// code to depend on ...
RuntimeRegistry *routerruntime.Registry
```

**Bootstrap 这一侧：写向量库。** 向量库依赖在启动期单独初始化（因为它有自己的后台 ingestion pipeline），完成后写进 Registry——见 [runtime_bootstrap.go:380-410](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go#L380-L410)，关键是结尾这句：

```go
	if runtimeRegistry != nil {
		runtimeRegistry.SetVectorStoreRuntime(vectorStoreRuntime)
	}
```

同一个 Registry 指针随后被传给 API Server 启动函数——见 [runtime_bootstrap.go:464-490](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go#L464-L490)，作为 `InitOptions.RuntimeRegistry` 字段传入。这就是「一个 Registry 实例被两边共享」的物理连接点。

**API Server 这一侧：现读实时依赖。** API Server 不缓存依赖，而是每个请求通过一组 `current*()` 方法现读。以记忆库为例——见 [runtime_dependencies.go:12-22](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/runtime_dependencies.go#L12-L22)：

```go
func (s *ClassificationAPIServer) currentMemoryStore() memory.Store {
	if s != nil && s.runtimeRegistry != nil {
		if store := s.runtimeRegistry.MemoryStore(); store != nil {
			return store
		}
	}
	...
	return s.memoryStore      // 兜底
}
```

向量库运行时、文件库、embedder、ingestion pipeline 也都通过 `currentVectorStoreRuntime()` 现读，再下钻取 `Manager`/`Embedder`/`Pipeline`/`FileStore`——见 [runtime_dependencies.go:24-29](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/runtime_dependencies.go#L24-L29) 及紧随其后的几个方法。配置本身同理，API Server 通过 `resolveAPIServerConfig` 读 `runtimeRegistry.CurrentConfig()`——见 [server.go:146-151](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/server.go#L146-L151)。

> 这种「现读」模式有一个直接好处：reload 完成后 ExtProc 调用 `publishRouterState` 写入新指针，API Server **不需要任何通知**，下一次 `current*()` 自动拿到新对象。

**反向通路：API Server 写回 config。** 当用户通过管理 API 部署新配置，API Server 的 `publishConfigMutation` 会把新 config 写回 Registry 并触发分类服务刷新——见 [runtime_config.go:68-89](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/runtime_config.go#L68-L89)：

```go
	if s.runtimeRegistry != nil {
		s.runtimeRegistry.UpdateConfig(newCfg)
		if s.classificationSvc != nil {
			s.classificationSvc.RefreshRuntimeConfig(newCfg)
		} else if svc := s.runtimeRegistry.ClassificationService(); svc != nil {
			svc.RefreshRuntimeConfig(newCfg)
		}
		return
	}
	config.Replace(newCfg)      // 兜底
```

注意这里 API Server 直接调 `UpdateConfig`（无校验直写），与模块 4.1 里 ExtProc reload 走的 `RefreshRuntimeConfig`（先校验再写）是两条不同路径——管理面部署的语义是「立即生效」，由各 handler 自行保证配置合法。

#### 4.2.4 代码实践

**实践目标**：理清「谁写哪个字段、谁读哪个字段」，画出 Registry 的依赖流向表。这正是本讲规格里要求的实践。

**操作步骤**：

1. 在 ExtProc 侧，打开 [server.go:345-361](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L345-L361)，记录 `publishRouterState` 调用了哪几个 Registry 写方法。
2. 在 Bootstrap 侧，打开 [runtime_bootstrap.go:405-407](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go#L405-L407)，记录向量库写方法。
3. 在 API Server 侧，打开 [runtime_dependencies.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/runtime_dependencies.go)，记录每个 `current*()` 方法读了哪个 Registry 读方法。
4. 在 API Server 的反向通路，打开 [runtime_config.go:79-87](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/runtime_config.go#L79-L87)，记录 config 回写路径。

**需要观察的现象**：

- ExtProc 写了 5 个字段：`config`、`classificationService`、`memoryStore`（三者经 `PublishRouterRuntime`）、`modelSelector`、`learningRuntime`。
- Bootstrap 写了第 6 个字段：`vectorStore`。
- API Server 几乎读全部字段，并且额外**写回** `config`。

**预期结果**（参考答案，下表）：

| Registry 字段 | 写入方（方法） | 读取方 |
| --- | --- | --- |
| config | ExtProc（`PublishRouterRuntime`）/ API Server（`UpdateConfig`） | API Server（`CurrentConfig`）、ExtProc（`CurrentConfig`） |
| classificationService | ExtProc（`PublishRouterRuntime`） | API Server（`ClassificationService`） |
| memoryStore | ExtProc（`PublishRouterRuntime`） | API Server（`MemoryStore`） |
| vectorStore | Bootstrap（`SetVectorStoreRuntime`） | API Server（`VectorStoreRuntime`） |
| modelSelector | ExtProc（`SetModelSelector`） | API Server（`ModelSelector`） |
| learningRuntime | ExtProc（`SetLearningRuntime`） | API Server（`LearningRuntime`） |

结论：ExtProc 与 Bootstrap 是生产者，API Server 是主要消费者兼 config 的回写者。

#### 4.2.5 小练习与答案

**练习 1**：reload 之后，API Server 需要「订阅通知」才能拿到新的分类服务吗？为什么？

**参考答案**：不需要。API Server 用 `current*()` 方法在每次请求时现读 Registry，而 ExtProc 在 reload 完成时通过 `publishRouterState` 把新指针写进 Registry。下一次读自然就是新对象，无需任何通知机制或事件订阅。

**练习 2**：[server.go:359-360](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L359-L360) 里 `runtimeRegistry == nil` 时会调用 `services.SetGlobalClassificationService`。这说明了什么？

**参考答案**：说明 Registry 是**取代**旧全局单例的现代共享路径。当没有 Registry（旧的或测试用的启动方式）时，代码退回到全局单例兜底，保证功能不缺失。这是一次渐进式重构的典型痕迹：新代码走 Registry，老入口仍由全局变量支撑。

---

### 4.3 学习运行时接口：窄接口缝

#### 4.3.1 概念说明

Registry 的 6 个字段里，有 5 个是**具体指针类型**（如 `*services.ClassificationService`、`*selection.Registry`），唯独 `learningRuntime` 是**接口类型** `LearningRuntime`。这不是随手写的，而是一个刻意的架构决策，叫「窄接口缝（narrow seam）」。

背景：Router Learning（路由学习）是 SR 的一项进阶能力——它收集每次路由的结果反馈（outcome），用来累积每个模型的质量经验（类似 Elo/RL 的思路，详见 u6-l2、u10）。这部分逻辑很重：要查重放记录、要按 `(recipe, decision, tier, model)` 维度累积计数器、要算 EWMA……这些实现都住在 extproc 包里（`routerLearningRuntime`）。

问题是：API Server 需要提供一个 HTTP 端点让外部（用户、agent、eval 系统）上报 outcome，可它**不应该**为了转发一个 outcome 就把 extproc 的一大堆内部结构依赖进来。

解决方案就是窄接口：在 `routerruntime` 包里定义一个**极小**的接口，只暴露 API Server 真正需要的那一个方法；extproc 实现这个接口，把实例塞进 Registry；API Server 只面向接口编程。注释把这点说得很直白——「API server 只需要转发类型化结果，而不依赖 extproc 内部」。

#### 4.3.2 核心流程

```text
extproc 包                                routerruntime 包                  apiserver 包
─────────────                             ─────────────────                 ─────────────
routerLearningRuntime                LearningRuntime interface
  (笨重实现：                         { OutcomeRuntime }
     累积经验/EWMA/查重放...)              ▲
        │                                 │  实现
        └────── 实现 ────────────────────┘
                                            │  通过 Registry 暴露
                                            ▼
                                       Registry.learningRuntime
                                            │  读取
                                            ▼
                                                          handleRouterOutcome
                                                            只调 runtime.UpdateOutcome(ctx, outcome)
                                                            完全不 import extproc
```

数据流方向：API Server 收到一条 HTTP outcome 请求 → 校验 + 鉴权 + 派生 provenance → 通过 `currentLearningRuntime()` 拿到接口 → 调 `UpdateOutcome` → extproc 内部累积经验。

#### 4.3.3 源码精读

先看这条窄缝的接口定义。`LearningRuntime` 只是把 `OutcomeRuntime` 嵌进来，本身没加任何方法——见 [registry.go:25-30](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/registry.go#L25-L30)：

```go
// LearningRuntime is the narrow API-server seam for Router Learning state.
// The implementation lives with the router runtime; the API server only needs
// to forward typed outcomes without depending on extproc internals.
type LearningRuntime interface {
	OutcomeRuntime
}
```

而 `OutcomeRuntime` 只有一个方法——见 [learning_outcome.go:60-62](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/learning_outcome.go#L60-L62)：

```go
type OutcomeRuntime interface {
	UpdateOutcome(context.Context, *RouterOutcome) RouterOutcomeResult
}
```

整条缝就这一个方法。`RouterOutcome` 是一个纯数据结构（也在 routerruntime 包里定义），携带可枚举的 Source/Target/Verdict——见 [learning_outcome.go:32-42](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/learning_outcome.go#L32-L42)：

```go
type RouterOutcome struct {
	ReplayID       string
	Source         RouterOutcomeSource
	Target         RouterOutcomeTarget
	TargetRef      string
	Verdict        RouterOutcomeVerdict
	Reason         string
	Score          float64
	Metadata       map[string]string
	IdempotencyKey string
}
```

`Verdict` 取值被收窄成四类（[learning_outcome.go:26-29](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/learning_outcome.go#L26-L29)）：`good_fit` / `underpowered` / `overprovisioned` / `failed`——分别表示「模型正合适」「模型能力不足」「模型过剩」「调用失败」。返回值 `RouterOutcomeResult` 带 `Code` 字段表达幂等与所有权结果（[learning_outcome.go:53-58](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/learning_outcome.go#L53-L58)），取值见 [learning_outcome.go:45-51](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/learning_outcome.go#L45-L51)：`duplicate`、`replay_not_found`、`ownership_mismatch`、`invalid_outcome`。

接下来看 API Server 怎么用这条缝。`handleRouterOutcome` 完成「鉴权 → 派生 provenance → 校验 → 调接口」后，**只调一个方法**——见 [route_router_outcomes.go:62-72](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/route_router_outcomes.go#L62-L72)：

```go
	runtime := s.currentLearningRuntime()
	if runtime == nil {
		s.writeErrorResponse(w, http.StatusServiceUnavailable, "NO_ROUTER_LEARNING_RUNTIME", ...)
		return
	}
	...
	result := runtime.UpdateOutcome(ctx, outcome)
```

`currentLearningRuntime()` 本身就是从 Registry 读——见 [runtime_dependencies.go:81-86](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/runtime_dependencies.go#L81-L86)。注意两个安全细节：第一，`outcome.Source`（provenance，来源归属）是从**认证凭据**派生的，绝不信请求体（[route_router_outcomes.go:58-60](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/route_router_outcomes.go#L58-L60)），防止上报者伪造身份；第二，`IdempotencyKey` 也由策略层统一派生，保证同一事件重复上报不会重复计数。

最后看实现侧有多重——`routerLearningRuntime` 这个 struct（住在 extproc 包）持有配置、重放记录器、经验 map、幂等键 map 等一堆状态——见 [router_learning_runtime.go:14-21](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_learning_runtime.go#L14-L21)：

```go
type routerLearningRuntime struct {
	mu              sync.Mutex
	config          *config.RouterConfig
	replayRecorder  *routerreplay.Recorder
	replayRecorders map[string]*routerreplay.Recorder
	experience      map[string]*routerLearningModelExperience
	idempotencyKeys map[string]time.Time
}
```

对比一下：接口只有 1 个方法、3 个数据类型；实现却拖着 6 个字段和一堆 EWMA 计数逻辑（`routerLearningModelExperience`）。窄缝的意义就在这里——**API Server 对这堆复杂性完全无感**，它只看到一个 `UpdateOutcome`。

> 顺带一提，`routerLearningRuntime` 的 `UpdateOutcome` 实现里会做幂等检查（用 `idempotencyKeys`）和重放记录回写（通过 `replayRecorder`），把 outcome 既累积进经验 map，也追加到对应的重放记录上。这些细节属于 u6-l2（选择算法）和 Router Learning 专题，本讲只点到「实现很重、接口很窄」这个架构要点。

#### 4.3.4 代码实践

**实践目标**：验证「窄缝」的实际效果——确认 API Server 调用 outcome 端点的整条路径上，确实没有 import extproc 包。

**操作步骤**：

1. 打开 [route_router_outcomes.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/route_router_outcomes.go)，看它的 import 列表，确认只 import 了 `routerruntime`，**没有** import `extproc`。
2. 在同一个文件里搜索 `extproc.`，确认零命中。
3. 打开 [learning_outcome.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/routerruntime/learning_outcome.go)，确认 `RouterOutcome`、`RouterOutcomeResult`、`OutcomeRuntime` 这三个类型都定义在 `routerruntime` 包，而不是 extproc 包。

**需要观察的现象**：

- API Server 的 outcome handler 只引用 `routerruntime.RouterOutcome`、`routerruntime.LearningRuntime` 等类型。
- 真正的笨重实现 `routerLearningRuntime` 在 extproc 包，API Server 看不到也调不到它的任何私有方法。

**预期结果**：确认依赖方向是单向的 `apiserver → routerruntime ← extproc`，`apiserver` 与 `extproc` 之间没有直接依赖。这就是窄缝要达到的解耦效果。

> 如果你想本地验证 import 关系：在仓库根目录执行 `go list -deps -f '{{.ImportPath}}' github.com/vllm-project/semantic-router/src/semantic-router/pkg/apiserver 2>/dev/null | grep extproc`（**待本地验证**，因为本环境未构建 Go 模块）。预期不输出任何 extproc 包路径。

#### 4.3.5 小练习与答案

**练习 1**：如果未来 Router Learning 需要新增一个「查询某模型当前经验快照」的能力给 API Server 用，从「窄缝」设计出发，应该怎么改？

**参考答案**：在 `routerruntime` 包里给 `OutcomeRuntime`（或新建一个独立接口）增加一个方法，例如 `ExperienceSnapshot(decision, tier, model) RouterLearningExperience`，并定义好返回的纯数据类型。extproc 的 `routerLearningRuntime` 实现新方法，API Server 面向新接口编程。关键是：新增的类型/方法都定义在 `routerruntime` 包这条「缝」上，仍然不让 API Server 直接依赖 extproc。

**练习 2**：为什么 `outcome.Source` 必须从认证凭据派生，而不是直接用请求体里的 `source` 字段？

**参考答案**：因为 `source` 是 provenance（结果归属），决定这条学习信号记在谁头上、可信度多高。如果信请求体，任何调用者都能伪造 `source: "eval"` 把自己的偏好冒充成评测系统的结论，污染学习数据。把 source 从认证后的身份派生，保证归属不可伪造。代码里 [route_router_outcomes.go:58-60](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/route_router_outcomes.go#L58-L60) 正是用 `outcome.Source = source` 覆盖掉请求体里的值。

---

## 5. 综合实践

**任务**：用一张「Registry 生命周期时序图」把本讲三个模块串起来，标出每一次读/写发生在哪个阶段、由谁发起。

请按以下步骤完成：

1. **画出启动阶段**（对应 u4-l1 的启动序列）：
   - `NewRegistry(initialCfg)` 在哪里被调用（提示：在 main 启动序列早期）。
   - `SetVectorStoreRuntime` 由 [runtime_bootstrap.go:405-407](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go#L405-L407) 写入。
   - ExtProc Server 创建后，`publishRouterState` 在 [server.go:92](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L92) 写入 config/classification/memory/modelSelector/learning。
   - API Server 启动时收到同一个 Registry 指针（[runtime_bootstrap.go:482](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go#L482)）。

2. **画出稳态运行阶段**：
   - API Server 每处理一个 `/v1/classify`、`/v1/kbs`、`/v1/router/outcomes` 请求时，分别会读哪些 Registry 字段（参考模块 4.2 的表）。
   - 在图上标注：这些读都是「现读」，不缓存。

3. **画出 reload 阶段**（承接 u4-l1）：
   - ExtProc 重建路由器后，[server.go:268](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L268) 的 `attachRuntimeRegistry` 与 [server.go:286](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/server.go#L286) 的 `publishRouterState` 重新写入。
   - 在图上标注：reload 后 API Server 无需通知，下次 `current*()` 自动拿到新对象。

4. **画出配置部署反向通路**：
   - 用户通过管理 API 部署新 config → [runtime_config.go:79-87](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/runtime_config.go#L79-L87) 的 `UpdateConfig` + `RefreshRuntimeConfig` 回写 Registry。

5. **最后回答一个开放问题**：如果 ExtProc 的 `publishRouterState` 在 reload 时失败了（没写成 Registry），API Server 会看到什么现象？用模块 4.1 的「现读 + 兜底」机制解释。

**预期成果**：一张包含「启动 / 稳态 / reload / 配置回写」四个阶段的时序图，每个箭头标注方法名与方向（读/写），并附上对开放问题的简短回答（要点：API Server 会继续读到旧的依赖快照，因为指针没换；若 Registry 为 nil 则退回全局单例）。

> 本实践为**源码阅读型实践**，不需要运行服务。重点是理清数据流向，而不是执行命令。

## 6. 本讲小结

- `routerruntime.Registry` 是一个**显式传递的依赖容器**，用一把 `sync.RWMutex` 线程安全地托管 6 类运行时依赖：config、classificationService、memoryStore、vectorStore、modelSelector、learningRuntime。
- 它取代了旧的全局单例：每个读方法都现读、每个写方法都加锁，并且所有方法都有 `if r == nil` 防御；Registry 为 nil 时代码退回全局单例兜底。
- ExtProc 与 Bootstrap 是**生产者**（写入），API Server 是**主要消费者**（现读）；二者共享同一个 Registry 指针，因此管理面操作的就是数据面正在用的活对象。
- `PublishRouterRuntime` 在一次加锁内原子发布 config+分类服务+记忆库；`RefreshRuntimeConfig` 坚持「先校验再切换」，分类服务拒绝就整笔回滚，绝不发布半成品状态。
- `LearningRuntime` 是一条**窄接口缝**：routerruntime 包只暴露 `UpdateOutcome` 一个方法和几个纯数据类型，笨重的 `routerLearningRuntime` 实现留在 extproc 包，使 API Server 完全不依赖 extproc 内部。
- 配置部署还有一条**反向通路**：API Server 通过 `UpdateConfig` + 分类服务 `RefreshRuntimeConfig` 把新 config 写回 Registry，让数据面也立即生效。

## 7. 下一步学习建议

- **u4-l3（ExtProc gRPC 服务与 Router）**：本讲把 Registry 当作黑盒容器，下一讲会打开 ExtProc 这个「生产者」，看 `OpenAIRouter` 状态机和 `Process(stream)` 如何被 Envoy 调用。
- **u5（请求处理主链路）**：本讲只讲依赖怎么共享，u5 会讲请求进来后如何**消费**这些依赖（分类、决策、选择）。
- **u6-l2（选择算法注册表）**：本讲提到的 `modelSelector` 字段类型是 `*selection.Registry`，u6-l2 会讲 Elo/Hybrid 等算法如何注册与选用。
- **u11-l1（API Server 管理 API）**：本讲的「消费者」API Server 在 u11-l1 会全面展开，包括配置部署与 ETag 同步、classify/eval/kbs 等端点。
- **进阶阅读**：想深入 Router Learning 的实现，直接读 [router_learning_runtime.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_learning_runtime.go) 里 `recordModelExperience` 与 `experienceSnapshot`，理解经验累积与 EWMA 更新。
