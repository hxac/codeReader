# 源码目录结构与模块地图

## 1. 本讲目标

前几讲我们已经知道 sgl-model-gateway（以下简称「网关」，库名 `smg`）是做什么的、怎么构建、怎么跑起来。本讲把镜头从「整体」拉到「代码内部」，带你把 `src/` 目录看懂：

- 学完本讲，你应该能闭着眼睛说出 `src/` 下每个顶层模块的职责。
- 你应该能区分三层结构：`core`（抽象底座）、`routers`（数据面）、`policies`（策略层），并说出它们之间的依赖方向。
- 你应该能解释 `config → core → policies → routers` 这条依赖链为什么是这样走的。
- 你应该能指出 `main.rs` 直接引用了哪些模块，哪些模块是「被间接使用」的。
- 你应该了解工程根目录下 `bindings`、`benches`、`e2e_test`、`examples` 这几个辅助目录分别是干什么用的。

本讲是「阅读源码」型讲义，重点是建立**模块地图**，不要求你运行任何命令。地图建好之后，后续每一讲都是在地图上的某个格子放大细看。

## 2. 前置知识

在开始之前，请确认你理解下面几个 Rust 的基础概念。如果你已经熟悉，可以跳过本节。

- **crate 与 module**：一个 Rust crate 是一次编译的最小单位；module 是 crate 内部的代码组织单元。网关这个 crate 的「根模块」就是 `src/lib.rs`。
- **`pub mod` 与 `pub use`**：
  - `pub mod foo;` 表示「把子模块 `foo` 挂到当前路径下，并且对外公开」。它通常对应一个文件 `foo.rs` 或目录 `foo/mod.rs`。
  - `pub use bar as baz;` 表示「把别处的某个东西重新导出，取个新名字」。在网关里它经常被用来把**外部 crate**（别的库）以一个短名字重新暴露。
- **依赖方向**：如果模块 A 里 `use` 了模块 B 的类型，我们就说「A 依赖 B」。一个健康的工程会把依赖组织成**有向无环图**，尽量「上层依赖下层，下层不反过来依赖上层」。本讲要画的依赖图，核心就是看 `config → core → policies → routers` 这条从下往上的链。
- **控制面 / 数据面**：这是前几讲建立的术语。简单说，**控制面**负责「管理有哪些 Worker、它们是否健康、什么时候增删」（后台、慢、异步）；**数据面**负责「把每一次推理请求转发给某个 Worker」（前台、快、同步）。如果你忘了，先回到 u1-l1 复习。

> 一句话回顾 u1-l1 的「四区域架构」：网关内部可以分成**控制面**、**数据面**、**可靠性层**、**可观测性层**四个区域。本讲的任务就是把这四个区域一一对应到 `src/` 下的具体目录。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 / 目录 | 作用 |
| --- | --- |
| `src/lib.rs` | crate 根模块。声明整个模块树，是理解工程结构的「总目录」。 |
| `src/core/mod.rs` | 控制面 + 可靠性 + 抽象底座的「核心」模块集合入口。 |
| `src/routers/mod.rs` | 数据面路由器集合入口，定义统一接口 `RouterTrait`。 |
| `src/policies/mod.rs` | 负载均衡策略层入口，定义 `LoadBalancingPolicy` trait。 |
| `src/config/mod.rs` | 配置类型入口，是最底层、几乎不依赖其他内部模块的一层。 |
| `src/app_context.rs` | 把各层组件「装配」成一个共享状态 `AppContext` 的地方。 |
| `src/server.rs` | 把 `AppContext` 接到 axum Web 框架上，定义路由表与启动流程。 |
| `bindings/`、`benches/`、`e2e_test/`、`examples/` | 工程根目录下的辅助目录：多语言绑定、性能基准、端到端测试、示例。 |

阅读建议：先看 `lib.rs` 建立总览，再看 `core`、`routers`、`policies` 三个 `mod.rs`，最后用 `app_context.rs` 和 `server.rs` 理解它们是怎么被串起来的。

## 4. 核心概念与源码讲解

### 4.1 lib.rs：整个 crate 的总入口

#### 4.1.1 概念说明

`src/lib.rs` 是这个 crate 的**根模块**（crate root）。Rust 编译器编译这个库时，第一件事就是读这个文件。它本身代码量极少（不到 20 行），但它是一张「**模块声明表**」——它告诉编译器：这个 crate 由哪些子模块组成，以及对外暴露哪些名字。

理解 `lib.rs` 的价值在于：**读这一个文件，就能拿到整个工程的骨架**。后续无论你要找哪个功能，都可以先在 `lib.rs` 里定位它属于哪个顶层模块，再进去细看。

#### 4.1.2 核心流程

`lib.rs` 里只有两类语句，理解了这两类，就读懂了整个文件：

1. **`pub mod xxx;`（自有模块声明）**：声明一个本 crate 内部的子模块。它会对应到 `src/xxx.rs` 文件或 `src/xxx/` 目录。这是「自家写的代码」。
2. **`pub use yyy as zzz;`（外部 crate 重导出）**：把**另一个外部 crate**（比如 `smg_auth`）的内容，以一个短名字（比如 `auth`）重新暴露出来。这样使用者只要 `use smg::auth::...`，而不必关心真正的 crate 叫 `smg_auth`。

> 为什么要把外部 crate 重导出？因为网关依赖好几个「姊妹 crate」（`smg_auth`、`openai_protocol`、`reasoning_parser`、`llm_tokenizer`、`tool_parser`），它们和网关一起协同发布。在 `lib.rs` 里给它们起短名字，相当于在 crate 边界上做了一次「统一命名」，让内部代码和外部用户都用同一套名字。

#### 4.1.3 源码精读

`lib.rs` 全文只有 16 行有效声明，我们逐块来看：

[src/lib.rs#L1-L16](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/lib.rs#L1-L16) — 整个 crate 的模块声明表，是本讲的「总目录」。

```rust
pub mod app_context;          // 1. 共享状态装配
pub use smg_auth as auth;     // 2. 外部 crate 重导出：认证
pub mod config;               // 3. 配置层（最底层）
pub mod core;                 // 4. 抽象底座 + 控制面 + 可靠性
pub mod middleware;           // 5. axum 中间件（限流/认证等）
pub mod observability;        // 6. 可观测性（指标/追踪/日志）
pub mod policies;             // 7. 负载均衡策略层
pub use openai_protocol as protocols;  // 8. 外部 crate：协议类型
pub use reasoning_parser;     // 9. 外部 crate：推理内容解析
pub mod routers;              // 10. 数据面路由器
pub mod server;               // 11. axum 应用与启动
pub mod service_discovery;    // 12. K8s 服务发现
pub use llm_tokenizer as tokenizer;    // 13. 外部 crate：分词器
pub use tool_parser;          // 14. 外部 crate：工具调用解析
pub mod version;              // 15. 版本信息
pub mod wasm;                 // 16. WASM 中间件扩展
```

把这 16 行分成两类来数：

- **11 个自有模块**（`pub mod`）：`app_context`、`config`、`core`、`middleware`、`observability`、`policies`、`routers`、`server`、`service_discovery`、`version`、`wasm`。这 11 个就是网关「自家写的」全部顶层模块。
- **5 个外部 crate 重导出**（`pub use`）：`smg_auth → auth`、`openai_protocol → protocols`、`reasoning_parser`、`llm_tokenizer → tokenizer`、`tool_parser`。

> 记忆口诀：**11 个自家模块 + 5 个外部 crate**。这和 u1-l1 给出的结论完全一致。以后只要有人问「网关由哪些模块组成」，背这 11 个名字即可。

#### 4.1.4 代码实践

这是一个源码阅读型实践，目标是亲手验证「11 + 5」这个数字。

1. **实践目标**：用工具自动统计 `lib.rs` 里的两类声明，确认你理解了「自有模块」和「外部重导出」的区别。
2. **操作步骤**：
   - 在仓库根目录执行：`grep -c "^pub mod " src/lib.rs`，统计自有模块数量。
   - 再执行：`grep -c "^pub use " src/lib.rs`，统计外部重导出数量。
   - 把两者的和与 16 对比。
3. **需要观察的现象**：两条命令分别应该输出 `11` 和 `5`，加起来正好等于 16。
4. **预期结果**：11 个 `pub mod` + 5 个 `pub use` = 16 行有效声明，与人工数数一致。
5. 如果你在不同版本上运行结果不同，说明工程结构发生了变化，请以实际 `lib.rs` 为准。

#### 4.1.5 小练习与答案

**练习 1**：如果我想新增一个叫 `feature_flags` 的自有模块，应该在 `lib.rs` 加一行什么？对应的文件应该放在哪里？

**答案**：在 `lib.rs` 加 `pub mod feature_flags;`，然后新建文件 `src/feature_flags.rs`（或目录 `src/feature_flags/mod.rs`）。

**练习 2**：`pub use openai_protocol as protocols;` 这一行，重导出之后，外部用户应该用 `smg::protocols::...` 还是 `smg::openai_protocol::...` 来访问？

**答案**：用 `smg::protocols::...`。`as protocols` 给了它新的对外名字，`openai_protocol` 这个原名在 `smg` 路径下并不直接可见。

---

### 4.2 core：跨区域的抽象底座

#### 4.2.1 概念说明

`src/core/` 是整个工程最关键的「**底座**」。它的名字叫 core（核心），是因为它定义了所有区域都要用的基础抽象：

- **控制面**要用的：`Worker`（一个推理进程的抽象）、`WorkerRegistry`（worker 注册表）、`JobQueue`（异步作业队列）、`steps`（多步工作流引擎）。
- **可靠性层**要用的：`CircuitBreaker`（熔断器）、`RetryExecutor`（重试执行器）、`TokenBucket`（令牌桶限流）。
- **数据面**也要用的：`Worker`、`HashRing`（一致性哈希环，给策略用）、`ModelCard`（模型元数据）。

换句话说，core 是「被所有人依赖、但自己尽量少依赖别人」的那一层。这就是分层架构里**底层**的典型特征。

#### 4.2.2 核心流程

`core/mod.rs` 做三件事，理解了这三件事就理解了 core 的组织方式：

1. **声明子模块**：用一连串 `pub mod` 列出 core 下所有子模块。
2. **重导出常用类型**：用 `pub use` 把每个子模块里最常用的类型「提升」到 `core::` 这一层，方便外部直接写 `smg::core::Worker`，而不用写 `smg::core::worker::Worker`。
3. **跨 crate 重导出**：比如 `pub use crate::protocols::UNKNOWN_MODEL_ID;`，把协议层的一个常量拿到 core 里来用。

core 下的子模块可以按「四区域」大致归类（这是本讲的思维框架，文件里并没有显式分组）：

| 区域 | core 下的子模块 |
| --- | --- |
| 控制面 | `worker`、`worker_builder`、`worker_registry`、`worker_manager`、`worker_service`、`job_queue`、`steps` |
| 可靠性层 | `circuit_breaker`、`retry`、`token_bucket` |
| 模型/数据 | `model_card`、`model_type`、`metrics_aggregator` |
| 基础设施 | `error` |

#### 4.2.3 源码精读

先看文件顶部的文档注释，它一句话讲清了 core 的定位：

[src/core/mod.rs#L1-L10](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/mod.rs#L1-L10) — 说明 core 包含 Worker、模型类型、错误类型、熔断器、令牌桶、工作流步骤等基础抽象。

接着是子模块声明，共 14 个 `pub mod`：

[src/core/mod.rs#L15-L28](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/mod.rs#L15-L28) — core 的 14 个子模块声明，对应目录下的 14 个文件 / 目录。

```rust
pub mod circuit_breaker;
pub mod error;
pub mod job_queue;
pub mod metrics_aggregator;
pub mod model_card;
pub mod model_type;
pub mod retry;
pub mod steps;          // 注意：steps 是一个目录，下面还有更细的子模块
pub mod token_bucket;
pub mod worker;
pub mod worker_builder;
pub mod worker_manager;
pub mod worker_registry;
pub mod worker_service;
```

最后是「常用类型提升」，把长路径缩短：

[src/core/mod.rs#L30-L43](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/mod.rs#L30-L43) — 把 `CircuitBreaker`、`Worker`、`WorkerRegistry`、`JobQueue` 等常用类型重导出到 `core::` 顶层。

```rust
pub use circuit_breaker::{CircuitBreaker, CircuitBreakerConfig, CircuitState};
pub use error::{WorkerError, WorkerResult};
pub use job_queue::{Job, JobQueue, JobQueueConfig};
// ...
pub use worker_registry::{HashRing, WorkerRegistry};
pub use worker_service::WorkerService;
```

> 这里有一个值得记住的设计模式：**子模块声明 + 顶层重导出**。前者负责「物理组织」（代码放哪个文件），后者负责「逻辑接口」（外部怎么方便地用）。两者分离，让内部文件结构可以自由调整，而对外接口保持稳定。

`steps` 子模块尤其重要，它是控制面的「工作流引擎」，下面还分了 `worker/`、`mcp_registration.rs`、`tokenizer_registration.rs` 等。它的入口注释这样描述自己：

[src/core/steps/mod.rs#L1-L10](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/steps/mod.rs#L1-L10) — steps 包含 Worker 管理、MCP 注册、WASM 注册、Tokenizer 注册等多步工作流。这一部分会在 u3-l5 详讲。

#### 4.2.4 代码实践

1. **实践目标**：把 core 的 14 个子模块归类到「四区域」框架里，验证 core 确实是一个跨区域的共享底座。
2. **操作步骤**：
   - 打开 `src/core/mod.rs`，把 L15-L28 的 14 个模块名抄下来。
   - 按本讲 4.2.2 的表格，把它们填进「控制面 / 可靠性层 / 模型数据 / 基础设施」四个格子里。
   - 用 `ls src/core/` 对比，确认文件名和模块名一一对应（注意 `steps` 是目录）。
3. **需要观察的现象**：14 个模块能被干净地归到四个区域里，没有哪个模块「无处安放」。
4. **预期结果**：你会发现 control-plane 相关的模块（worker 系列 + job_queue + steps）占了大多数，这正是 core 作为「控制面主链底座」的体现。
5. 如果某个模块你觉得归类困难（比如 `metrics_aggregator`），没关系——边界模块本来就是跨区域的，标注「待确认」即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CircuitBreaker`（熔断器）放在 `core` 里，而不是放在 `routers`（数据面）里？

**答案**：因为熔断器是「per-worker」的可靠性机制，控制面（worker 管理）和数据面（请求转发）都需要读写它的状态。把它放在最底层的 core，两个上层都能依赖它，避免循环依赖。

**练习 2**：`pub use worker_registry::{HashRing, WorkerRegistry};` 这行重导出，对使用者的好处是什么？

**答案**：使用者可以直接写 `smg::core::WorkerRegistry`，而不必写 `smg::core::worker_registry::WorkerRegistry`。缩短了路径，也让 core 对外暴露的「公共类型清单」一目了然。

---

### 4.3 routers：数据面路由器集合

#### 4.3.1 概念说明

如果说 `core` 是「被所有人依赖的底座」，那么 `routers` 就是「依赖最多人的顶层」。`routers` 是**数据面**——它负责接收客户端的每一次推理请求，选出合适的 Worker，把请求转发过去，再把响应（包括流式响应）回传给客户端。

routers 下按「**协议 / 功能**」分了很多子模块，比如：

- `http/`：HTTP 协议路由器（常规路由、PD 分离路由）。
- `grpc/`：gRPC 协议路由器（全 Rust 实现的流水线）。
- `openai/`：OpenAI 兼容代理（包括 `/v1/responses` 与 MCP 工具循环）。
- `conversations/`：会话管理。
- `mesh/`：多实例高可用状态同步。
- `tokenize/`、`parse/`：分词与解析端点。
- `factory.rs`、`router_manager.rs`：路由器的工厂与多模型管理器。

为了让这些形态各异的路由器能被统一对待，`routers/mod.rs` 定义了一个核心 trait：`RouterTrait`。

#### 4.3.2 核心流程

routers 的组织遵循一个清晰的模式：

1. **定义统一接口 `RouterTrait`**：所有路由器都实现这个 trait，提供 `route_chat`、`route_generate`、`route_embeddings` 等方法。
2. **用默认实现降低实现成本**：trait 里大部分方法都有默认实现（直接返回 `501 Not Implemented`），子类只需实现自己关心的方法。唯一**必须**实现的是 `route_chat`。
3. **用工厂按需创建**：`factory.rs` 根据连接模式（HTTP/gRPC）和路由模式（Regular/PD/OpenAI）创建对应的具体路由器。
4. **用 RouterManager 管理多个路由器**：在多模型网关（IGW）模式下，`router_manager.rs` 负责按 `model` 选择对应的路由器。

这个模式的好处是：上层代码（比如 `server.rs`）只跟 `RouterTrait` 打交道，完全不用关心底下到底是 HTTP 路由器还是 gRPC 路由器。

#### 4.3.3 源码精读

先看 routers 下的子模块声明，共 14 个：

[src/routers/mod.rs#L23-L40](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/routers/mod.rs#L23-L40) — routers 的 14 个子模块声明，外加对 `RouterFactory` 和 HTTP 路由器的重导出。

```rust
pub mod conversations;
pub mod error;
pub mod factory;          // 路由器工厂
pub mod grpc;             // gRPC 数据面
pub mod header_utils;
pub mod http;             // HTTP 数据面
pub mod mcp_utils;
pub mod mesh;             // 多实例高可用
pub mod openai;           // OpenAI 兼容代理
pub mod parse;
pub mod persistence_utils;
pub mod router_manager;   // 多模型路由管理器（IGW）
pub mod streaming_utils;  // SSE 流式工具
pub mod tokenize;
```

接着是统一接口 `RouterTrait` 的定义开头：

[src/routers/mod.rs#L46-L99](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/routers/mod.rs#L46-L99) — `RouterTrait` 定义。注意大多数方法有默认实现（返回 501），只有 `route_chat` 是必须实现的（没有默认实现、以分号结尾）。

```rust
#[async_trait]
pub trait RouterTrait: Send + Sync + Debug {
    fn as_any(&self) -> &dyn std::any::Any;

    // 带默认实现：未实现就返回 501
    async fn route_generate(&self, ...) -> Response {
        (StatusCode::NOT_IMPLEMENTED, "Generate endpoint not implemented").into_response()
    }

    // 唯一「必须实现」的方法 —— 注意它以分号结尾，没有函数体
    async fn route_chat(
        &self,
        headers: Option<&HeaderMap>,
        body: &ChatCompletionRequest,
        model_id: Option<&str>,
    ) -> Response;

    fn router_type(&self) -> &'static str;   // 返回路由器类型名
    fn is_pd_mode(&self) -> bool { ... }     // 是否 PD 模式
}
```

> 重点观察：`route_chat` 方法签名后面直接是 `;`（分号），没有 `{ ... }` 函数体——这表示它**没有默认实现**，任何实现 `RouterTrait` 的类型都必须提供它。而 `route_generate`、`route_embeddings` 等都有 `{ ... }` 默认体，子类可以「选择性实现」。这种「一个核心方法强制实现 + 其余方法默认 501」的设计，让新增一种路由器的成本很低。

#### 4.3.4 代码实践

1. **实践目标**：统计 `RouterTrait` 一共有多少个方法，区分哪些是「必须实现」、哪些是「可选实现」。
2. **操作步骤**：
   - 打开 `src/routers/mod.rs`，从 L46 的 `pub trait RouterTrait` 开始往下读。
   - 对每个方法，看它后面是 `;`（必须实现）还是 `{ ... }`（有默认实现、可选）。
   - 用 `grep -nE "async fn |fn " src/routers/mod.rs` 列出所有方法及行号辅助定位。
3. **需要观察的现象**：你会看到大量方法以 `(StatusCode::NOT_IMPLEMENTED, ...).into_response()` 结尾，只有少数方法（如 `route_chat`、`as_any`、`router_type`）没有默认体。
4. **预期结果**：能列出 1 个必须实现的业务方法（`route_chat`）加上几个必须实现的元方法（`as_any`、`router_type`），其余都是可选。这正是「最小实现负担」设计的体现。
5. 如果不同版本方法数量不同，以实际源码为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `route_chat` 被设计成「必须实现」，而 `route_embeddings` 是「可选」？

**答案**：因为聊天补全（chat completion）是几乎所有推理服务最核心、最常用的端点，任何路由器都必须支持它；而 embeddings、rerank、classify 等是可选能力，不是所有后端都支持，所以给默认 501，让不需要的路由器可以不实现。

**练习 2**：`routers/factory.rs` 和 `routers/router_manager.rs` 都不带斜杠（不是目录），它们和 `http/`、`grpc/` 这类目录模块是什么关系？

**答案**：`http/`、`grpc/`、`openai/` 是「具体路由器实现」（数据面的不同形态）；`factory.rs` 是「按配置创建哪个具体路由器」的工厂；`router_manager.rs` 是「在多模型场景下管理多个路由器」的管理者。后两者是「组织者」，前者是「被执行者」。

---

### 4.4 分层依赖与工程辅助目录

#### 4.4.1 概念说明

前面三节我们分别看了 `lib.rs`、`core`、`routers`。现在把它们串起来，看**依赖方向**，这是本讲最核心的一张图。

一个健康的 Rust 工程通常呈「**金字塔**」结构：底层是一些不依赖别人、被广泛依赖的基础模块；顶层是少数「组装一切」的入口。网关的分层可以简化为：

```
        ┌─────────────────────────────────┐
顶层 →  │  server.rs / app_context.rs     │  组装层：把所有组件接到 axum 上
        ├─────────────────────────────────┤
        │  routers (数据面)                │  依赖 core + policies + observability
        ├─────────────────────────────────┤
        │  policies (策略层)               │  依赖 core（Worker、HashRing）
        ├─────────────────────────────────┤
        │  core (抽象底座)                 │  依赖 config、protocols
        ├─────────────────────────────────┤
底层 →  │  config (配置类型)               │  几乎不依赖内部模块
        └─────────────────────────────────┘
```

依赖方向是**自上而下**：上层依赖下层，下层不反过来依赖上层。这就形成了本讲标题里强调的那条链：**config → core → policies → routers**（从下往上读，表示「下层先存在，上层依赖它」）。

> 注意：`observability`（可观测性）和 `middleware`（中间件）是「**横切关注点**」（cross-cutting concern）。它们几乎被所有层使用，但不属于上面这条纵向链，所以在图里单独理解即可。

除了 `src/` 主代码，工程根目录还有几个**辅助目录**，它们不参与主二进制的核心逻辑，但对开发、测试、发布必不可少：

| 目录 | 作用 |
| --- | --- |
| `bindings/` | 多语言绑定。`bindings/python/` 用 PyO3 + maturin 把网关打包成 Python wheel；`bindings/golang/` 提供 Go（cgo）绑定。 |
| `benches/` | 性能基准测试（criterion 风格），度量策略、路由、流式等热路径的性能。 |
| `e2e_test/` | 端到端测试（Python），按功能分了 `chat_completions/`、`embeddings/`、`responses/`、`k8s_integration/` 等子目录。 |
| `examples/` | 示例代码。目前主要是 `examples/wasm/`，演示如何编写 WASM 中间件 guest。 |

#### 4.4.2 核心流程

要把「依赖方向」和「模块引用」讲透，关键是看**谁在 `use` 谁**。我们用两个视角：

**视角一：分层链 `config → core → policies → routers` 的依据**

- `core` 里大量出现 `crate::protocols::...`（见 4.2.3 的 `UNKNOWN_MODEL_ID` 重导出），且 `core` 的类型用 `config` 里的配置来构造——所以 core 依赖 config / protocols。
- `policies/mod.rs` 开头就 `use crate::core::{HashRing, Worker};`——所以 policies 依赖 core。
- `routers` 里的路由器在转发请求时要调用策略选 Worker、要用熔断器/重试、要打指标——所以 routers 依赖 policies + core + observability。

**视角二：`main.rs` 直接引用了哪些模块**

这是本讲综合实践要回答的核心问题。`main.rs` 的开头有一个 `use smg::{ ... }` 块，它**只**引入了这些顶层模块：

[src/main.rs#L5-L22](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L5-L22) — main.rs 从 `smg` 直接引入的模块清单。

```rust
use smg::{
    auth::{ApiKeyEntry, ControlPlaneAuthConfig, JwtConfig, Role},
    config::{ /* 一堆配置类型 */ },
    core::ConnectionMode,
    observability::{ /* Prometheus / otel */ },
    server::{self, ServerConfig},
    service_discovery::ServiceDiscoveryConfig,
    version,
};
use smg_mesh::service::MeshServerConfig;   // 外部 crate
```

也就是说，`main.rs` **直接**引用的模块只有 7 个：`auth`、`config`、`core`（且只用了 `ConnectionMode` 一个类型）、`observability`、`server`、`service_discovery`、`version`，外加外部 crate `smg_mesh`。

那么 `policies`、`routers`、`wasm`、`app_context`、`middleware`、`tokenizer` 这些模块去哪了？答案是：**它们被 `server::startup` 间接使用**。`main.rs` 只负责解析 CLI、构建配置、然后调用 `server::startup(config)`，真正的「组装所有组件」发生在 `server.rs` 和 `app_context.rs` 里。

这就是为什么 `app_context.rs` 被称为「装配层」——它把每一层的组件捏合在一起：

[src/app_context.rs#L40-L62](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/app_context.rs#L40-L62) — `AppContext` 结构体，把 config、core（registry/job_queue/service）、policies（policy_registry）、routers（router_manager）、storage、tokenizer、wasm 等所有层的组件聚合到一个共享状态里。

```rust
pub struct AppContext {
    pub client: Client,
    pub router_config: RouterConfig,            // config 层
    pub worker_registry: Arc<WorkerRegistry>,    // core 层（控制面）
    pub policy_registry: Arc<PolicyRegistry>,    // policies 层
    pub router_manager: Option<Arc<RouterManager>>, // routers 层
    pub response_storage: Arc<dyn ResponseStorage>, // 外部 data_connector
    pub tokenizer_registry: Arc<TokenizerRegistry>, // 外部 tokenizer crate
    pub wasm_manager: Option<Arc<WasmModuleManager>>, // wasm 层
    // ... 还有 load_monitor、inflight_tracker 等
}
```

> 注意 `AppContext` 里有些字段是 `Arc<OnceLock<...>>`（如 `worker_job_queue`、`workflow_engines`、`mcp_manager`），表示它们是「**延迟初始化**」的——启动时先放一个空的锁，等后续步骤再填入真实对象。这个设计细节会在 u2-l4 详讲，这里先有个印象即可。

最后，`server.rs` 把 `AppContext` 包进 `AppState`，接到 axum 的路由表上：

[src/server.rs#L536-L536](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L536) 与 [src/server.rs#L696-L696](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L696) — `build_app`（组装 axum 应用与路由表）与 `startup`（完整启动编排）这两个函数，是 `main.rs` 之后真正的执行主角。

#### 4.4.3 源码精读

`config` 作为最底层，结构非常简单，几乎没有内部依赖：

[src/config/mod.rs#L1-L6](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/mod.rs#L1-L6) — config 层只声明 `builder` 和 `types` 两个子模块并整体重导出。

```rust
pub mod builder;
pub mod types;
pub use builder::*;
pub use types::*;
```

`policies` 的开头明确写出它对 core 的依赖：

[src/policies/mod.rs#L7-L13](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/policies/mod.rs#L7-L13) — policies 直接 `use crate::core::{HashRing, Worker};`，这是「策略层依赖 core」的直接证据。

```rust
use async_trait::async_trait;
use smg_mesh::OptionalMeshSyncManager;
use crate::core::{HashRing, Worker};   // ← 依赖 core
```

辅助目录的真实内容，可以用 `ls` 验证：

| 命令 | 结果 |
| --- | --- |
| `ls bindings/` | `golang/  python/` |
| `ls benches/` | `consistent_hash_bench.rs`、`manual_policy_benchmark.rs`、`request_processing.rs`、`router_registry_bench.rs`、`streaming_utils_bench.rs`、`tree_benchmark.rs`、`wasm_middleware_latency.rs` |
| `ls e2e_test/` | `chat_completions/`、`embeddings/`、`responses/`、`k8s_integration/`、`benchmarks/`、`fixtures/`、`infra/`、`conftest.py` 等 |
| `ls examples/` | `wasm/` |

> 这些目录的命名直接对应它们的功能：`benches/` 里每个文件都是对一个热路径（一致性哈希、manual 策略、请求处理、路由注册、流式工具、前缀树、wasm 延迟）的性能压测；`e2e_test/` 按功能域分目录，模拟真实的多 worker 拓扑做端到端验证。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证「分层依赖方向」和「main.rs 直接引用清单」这两个结论。
2. **操作步骤**：
   - 验证 policies 依赖 core：`grep -n "use crate::core" src/policies/mod.rs`，应能看到 `use crate::core::{HashRing, Worker};`。
   - 验证 main.rs 的直接引用：`grep -nE "use smg::" src/main.rs`，再展开看 `use smg::{ ... }` 块里出现的顶层名字。
   - 探索辅助目录：依次执行 `ls bindings/`、`ls benches/`、`ls e2e_test/`、`ls examples/`，记录每个目录的子项。
3. **需要观察的现象**：
   - policies 里确实出现了对 core 的 `use`，证明依赖方向是 policies → core。
   - main.rs 的 `use smg::{...}` 块里只出现 `auth / config / core / observability / server / service_discovery / version` 这 7 个顶层模块。
   - 4 个辅助目录的内容与上表一致。
4. **预期结果**：三条命令的输出都印证了本节给出的结论。特别地，你会确认 `policies`、`routers`、`wasm` 等模块名在 `main.rs` 里**并不直接出现**（仅在注释或 CLI 字段名里偶现，不是模块引用）。
5. 如果 `grep` 在注释里匹配到这些词（比如 `main.rs` 第 106 行注释里的 "policies"），请区分「注释/字段名」和「真正的模块引用」，以前者为准地排除掉。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `policies` 里的某个策略改成需要调用 `routers` 的某个函数，会出什么问题？

**答案**：会形成 `routers → policies → routers` 的循环依赖，Rust 编译器会拒绝。这正是为什么分层要严格「上层依赖下层」：策略层只能依赖更底层的 core，不能反过来碰数据面。如果确实需要交互，应该通过 trait 抽象或把公共部分下沉到 core。

**练习 2**：`bindings/python` 和 `benches` 都不是 `src/` 下的代码，为什么它们能用到网关的核心功能？

**答案**：它们以**库依赖**的形式引用 `smg` 这个 crate。`bindings/python` 是一个独立的子 crate（cdylib），通过 Cargo 的 `path` 依赖复用 `smg` 核心；`benches` 则通过 cargo benchmark 机制链接到本 crate。两者都只消费 `lib.rs` 暴露的公共 API，不参与主二进制的编译链路。

**练习 3**：为什么 `main.rs` 不直接 `use smg::routers::...`，而是把这些交给 `server::startup`？

**答案**：因为 `main.rs` 的职责应该尽可能薄——只做「解析参数 + 构建配置 + 启动」。具体的「创建哪些路由器、装配哪些组件」属于**启动编排逻辑**，交给 `server.rs` / `app_context.rs` 集中管理，可以让 `main.rs` 保持简单，也方便测试时单独构造 `AppContext`。

## 5. 综合实践

现在把本讲的所有知识串起来，完成下面这个**画图任务**（本讲的核心实践，对应规格里的实践任务）。

**任务**：在 `src/` 下绘制一张「模块依赖图」，标注 `config → core → policies → routers` 的依赖方向，并指出哪些模块会被 `main.rs` 直接引用。

**操作步骤**：

1. 在白纸或绘图工具上画出一个自下而上的金字塔（参考 4.4.1 的示意），包含四层：`config`、`core`、`policies`、`routers`。
2. 在 `core` 层右侧，画两个「横切」框：`observability`、`middleware`，用虚线连到 `routers` 和 `core`，表示它们被多层共享。
3. 在金字塔顶端画两个「组装」框：`app_context`、`server`，用箭头从它们指向下面各层，表示「组装层依赖所有层」。
4. 在图的最上方画一个 `main.rs` 框，从它只引出 **7 条**箭头，分别指向 `auth`、`config`、`core`、`observability`、`server`、`service_discovery`、`version`。
5. 在图的一侧单独列出 4 个辅助目录 `bindings`、`benches`、`e2e_test`、`examples`，标注它们「以库依赖形式复用 `smg`，不参与主二进制核心链路」。

**需要观察的现象 / 检查清单**：

- [ ] 图里能清晰看到 `config → core → policies → routers` 这条从下到上的依赖链，箭头方向是「上层指向下层」。
- [ ] `main.rs` 只有 7 条出边，没有直接连到 `policies`、`routers`、`wasm`。
- [ ] `observability` 和 `middleware` 被标注为「横切关注点」，不属于纵向链。
- [ ] `app_context` / `server` 位于顶端，依赖几乎所有层。

**预期结果**：你得到一张可以贴在墙上的「网关模块地图」。以后阅读任何一篇后续讲义，你都能先在这张图上定位它在哪个格子，再深入细节。这张图也是你检验「是否真懂了工程结构」的试金石——如果画不出来，说明还需要重读 4.1～4.4。

> 本实践为源码阅读型，无需运行网关。如果你愿意，可以用 `grep` 抽取每个模块文件顶部的 `use crate::...` 语句来**佐证**你画的箭头方向，让图有据可查。

## 6. 本讲小结

- `src/lib.rs` 是 crate 根模块，用 16 行声明搭起整个工程骨架：**11 个自有模块 + 5 个外部 crate 重导出**。
- `core` 是被所有区域依赖的**抽象底座**，包含 Worker、注册表、作业队列、熔断器、重试、令牌桶、工作流步骤等 14 个子模块。
- `routers` 是**数据面**，用统一接口 `RouterTrait` 把 HTTP/gRPC/OpenAI 等形态各异的路由器抽象在一起；`route_chat` 是唯一必须实现的方法，其余方法默认返回 501。
- 网关呈金字塔分层：**config → core → policies → routers**，依赖方向自上而下；`observability` 与 `middleware` 是横切关注点。
- `app_context.rs` 是**装配层**，把各层组件捏合成共享状态 `AppContext`；`server.rs` 再把它接到 axum。
- `main.rs` 只直接引用 7 个模块（auth/config/core/observability/server/service_discovery/version），其余模块通过 `server::startup` 间接使用。
- 工程根目录的 `bindings`、`benches`、`e2e_test`、`examples` 是辅助目录，分别负责多语言绑定、性能基准、端到端测试、示例代码。

## 7. 下一步学习建议

本讲建立的是「静态地图」。下一单元（第 2 单元「配置与启动链路」）会带你走「动态链路」——从 `main.rs` 一路追到 `server::startup`，看这张地图上的模块是如何被**依次激活**的。建议按以下顺序继续：

1. **u2-l1（CLI 参数与 RouterConfig 构建）**：先看 `main.rs` 如何把命令行参数变成 `RouterConfig`，这是启动链路的第一步。
2. **u2-l3（server::startup 启动编排）**：本讲提到了 `startup` 函数（[src/server.rs#L696](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L696)），u2-l3 会逐行拆解它的初始化顺序。
3. **u2-l4（AppContext 共享状态）**：本讲提到了 `AppContext` 的装配，u2-l4 会深入讲解它的 `OnceLock` 延迟初始化设计。

如果你更想先深入某一层，也可以直接跳读 `src/core/mod.rs` 里的某个子模块（比如 `worker.rs`、`job_queue.rs`）来预热第 3 单元「控制面」。但建议先完成第 2 单元，把「启动链路」这条主线走通，再横向展开各层细节。
