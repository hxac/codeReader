# 项目定位与整体架构

## 1. 本讲目标

本讲是整个学习手册的第一篇。读完本讲，你应该能够：

- 用一句话说清楚 **sgl-model-gateway**（以下简称「网关」）是什么、解决什么问题；
- 把网关放进 SGLang 生态里，说清楚它和「推理引擎 / Worker」之间的边界；
- 区分网关内部的**控制面（Control Plane）**与**数据面（Data Plane）**；
- 认识网关的**可靠性层**与**可观测性层**这两个横切关注点；
- 看懂 `Cargo.toml` 里的 workspace、二进制别名和外部 crate，并知道每个外部 crate 大致服务哪个子系统。

本讲**不要求你会 Rust**，也不要求你已经跑通过网关。我们只读三个文件：`README.md`、`Cargo.toml`、`src/lib.rs`，目的是建立一张「整体地图」，后续每一讲都是在这张地图上放大某一块。

## 2. 前置知识

在开始之前，先建立两个直觉。

### 2.1 什么是「推理引擎 / 推理服务」

大语言模型（LLM）本身只是一个权重文件。要让它能响应请求，需要一套运行时：加载权重到 GPU、把请求里的文本变成 token、做前向计算、再把 token 变回文本。这套运行时就是**推理引擎**。SGLang 项目最核心的产出就是一个高性能推理引擎，运行起来后通常监听一个 HTTP 或 gRPC 端口，我们把这个进程叫一个 **Worker**。

当只有一个 Worker 时，客户端可以直接连它。但当模型变大、并发变高时，单机扛不住，于是会出现**一组** Worker（几台甚至几十台机器）。这时就出现了一系列新问题：

- 请求发给哪一台？（负载均衡）
- 某一台挂了怎么办？（故障转移）
- 怎么知道哪一台当前更空闲？（负载感知）
- 新加一台机器、下线一台机器怎么不停机完成？（动态注册）

**网关**就是专门解决这些问题的那一层。它坐在客户端和一群 Worker 之间：客户端只跟网关说话，网关负责把请求路由到合适的 Worker，并在 Worker 出问题时自动重试或熔断。

### 2.2 什么是「控制面 / 数据面」

这两个词来自网络和云原生领域：

- **数据面（Data Plane）**：处理「真实业务流量」的路径，也就是每一次推理请求走过的地方。要求**快**、**低延迟**。
- **控制面（Control Plane）**：不直接处理业务流量，而是负责「管理数据面本身」——注册新 Worker、探活、统计负载、服务发现。要求**正确**、**最终一致**。

把它们分开，是因为这两类工作对延迟和一致性的要求完全不同。sgl-model-gateway 把这两者放在同一个进程里，但用不同的子系统实现，本讲会带你从源码里把它们区分出来。

> 名词小贴士：**Worker** 是真正干推理活的进程；**Gateway / Router** 是路由器（本项目里这两个词基本混用）；**SRT** 指 SGLang Runtime，即 SGLang 自己的推理运行时。

## 3. 本讲源码地图

本讲只涉及三个文件，它们正好回答了三个不同的问题：

| 文件 | 回答的问题 | 在本讲的角色 |
|------|------------|--------------|
| [README.md](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md) | 「这个项目是干什么的？」 | 项目自述与架构总览（人类语言） |
| [Cargo.toml](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml) | 「它怎么构建、依赖什么？」 | workspace、二进制、外部依赖边界 |
| [src/lib.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/lib.rs) | 「源码里有哪些顶层模块？」 | 库的模块导出地图（代码语言） |

> 贯穿全文的永久链接 base 是：
> `https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/`
> 对应 git HEAD `40b2119b23`。下文每一处关键代码都会附上带行号的永久链接。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先看 README 讲的定位，再看 Cargo.toml 的构建与依赖，再看 lib.rs 的模块地图，最后把三者综合成一张「四区域架构图」。

### 4.1 网关是什么：定位与在 SGLang 生态中的位置

#### 4.1.1 概念说明

README 第一段就用一句话给网关定了性：

> High-performance model routing **control and data plane** for large-scale LLM deployments.

翻译过来就是：**面向大规模 LLM 部署的高性能「模型路由」控制面与数据面**。注意三个关键词：

- **路由（routing）**：核心动作是「把请求送到正确的 Worker」；
- **大规模（large-scale）**：设计目标是成百上千个 Worker，不是单机玩具；
- **为 SGLang 运行时深度优化**：虽然它也能接 vLLM、TRT-LLM、OpenAI 等后端，但很多能力（比如 gRPC 流水线、PD 分离）是专门为 SGLang 设计的。

#### 4.1.2 核心流程

从生态位置看，一次请求的生命周期是：

```
客户端（OpenAI SDK / curl / 应用）
        │  POST /v1/chat/completions
        ▼
┌─────────────────────────────────┐
│      sgl-model-gateway          │   ← 本项目
│  (控制面管理 Worker + 数据面转发) │
└─────────────────────────────────┘
        │  按策略选一个 Worker
        ├──► Worker 1 (SGLang 引擎)
        ├──► Worker 2 (SGLang 引擎)
        └──► Worker N ...
```

关键点：客户端**只认网关**，不关心后面有几台 Worker；Worker 的增删对客户端透明。如果用 \(N\) 表示 Worker 数量，网关把「1 个入口」扇出（fan-out）成 \(N\) 个后端，并在它们之间做负载均衡与故障转移。

#### 4.1.3 源码精读

README 的「Overview」一节用六个要点概括了网关能力：

- [README.md:L5-L12](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L5-L12) — 这六条是整个项目的「功能宣言」，覆盖统一控制面、数据面、gRPC 流水线、多模型网关（IGW）、会话与历史存储、可靠性原语、可观测性。

紧接着的「Architecture at a Glance」把系统明确拆成两块：

- [README.md:L15-L19](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L15-L19) — **Control Plane**：Worker Manager（校验/发现/同步注册表）、Job Queue（串行化后台增删）、健康检查与负载监控、可选的 Kubernetes 服务发现。
- [README.md:L21-L27](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L21-L27) — **Data Plane**：HTTP 路由器（regular + PD）、gRPC 路由器与流水线、OpenAI 代理路由器、RouterManager（IGW 多模型协调）、可靠性层（限流/排队/重试/熔断）、负载均衡策略族。

> 注意 README 还提到「industry-first gRPC pipeline」（业界首个全 Rust gRPC 流水线）。这说明网关不只是「转发 HTTP」，它还能把 tokenize、reasoning 解析、tool 解析都放在 Rust 进程里做，从而省掉与 Python 的跨进程开销。这是本项目的核心差异化能力之一，第 7 单元会深入。

#### 4.1.4 代码实践

**实践目标**：通过 README 的 Quick Start，亲眼确认网关有几种「运行模式」，建立「同一个二进制能干不同的事」的直觉。

**操作步骤**：

1. 打开 [README.md:L122-L249](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L122-L249)（Quick Start 一节）。
2. 用文本编辑器或 `grep` 找出所有形如 `--xxx` 的「模式开关」标志，例如 `--pd-disaggregation`、`--enable-igw`、`--backend openai`、`grpc://...`、`--mcp-config-path`。
3. 把它们整理成一张「模式 → 触发标志 → 走哪类路由器」的表。

**需要观察的现象**：你会发现同一个二进制 `sgl-model-gateway`，靠不同的 CLI 标志就能切换成完全不同的数据面（HTTP 常规、HTTP PD 分离、gRPC、OpenAI 代理、多模型 IGW）。

**预期结果**：至少能列出 5 种模式，并指出每种模式由哪个标志触发。如果暂时分不清 IGW 和 PD 的区别也没关系，本讲第 4.4 节和后续单元会逐一展开。

> 命令行运行结果**待本地验证**：本实践是「阅读型实践」，不需要你真的启动网关；若想真的运行，请先看下一讲（u1-l2 构建）。

#### 4.1.5 小练习与答案

**练习 1**：README 说网关是「control and data plane」。请用一句话区分这两者。

> **参考答案**：控制面负责「管理 Worker 本身」（注册、探活、发现、负载统计），不直接处理用户的推理请求；数据面负责「转发用户的每一次推理请求」到合适的 Worker，要求低延迟。

**练习 2**：README 多次提到「OpenAI-compatible」。为什么网关要兼容 OpenAI 的接口格式？

> **参考答案**：因为 OpenAI 的 `/v1/chat/completions` 等接口已经成为 LLM 调用的事实标准，绝大多数 SDK、应用都按它来写。网关兼容这套接口，意味着客户端无需改代码就能从「直连 OpenAI」切换到「连内部网关 + 自建 Worker」，降低了迁移成本。

### 4.2 Cargo.toml：workspace、二进制别名与依赖边界

#### 4.2.1 概念说明

Rust 项目的「身份证」是 `Cargo.toml`。它告诉构建工具三件事：

1. 这个 crate 叫什么、版本是多少；
2. 它产出什么（库 / 可执行文件 / 两者）；
3. 它依赖哪些别的 crate。

对学习者来说，`Cargo.toml` 还有一个隐藏价值：**依赖列表就是一张「与外部世界的边界图」**。网关把很多能力做成了独立的 crate（`smg-*`、`reasoning-parser` 等），看懂这些依赖，就能知道「哪些是本项目自己写的、哪些是复用生态的」。

#### 4.2.2 核心流程

`Cargo.toml` 从上到下大致分成五段：

```
[workspace]      ← 工作空间：哪些子目录一起构建
[package]        ← 本 crate 元信息（名字、版本）
[features]       ← 可选编译特性开关
[lib] / [[bin]]  ← 产出物：一个库 + 三个二进制别名
[dependencies]   ← 依赖清单（最重要的一段）
[profile.*]      ← 不同场景的编译优化档位
```

#### 4.2.3 源码精读

**① workspace 与 package**

- [Cargo.toml:L1-L3](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L1-L3) — workspace 只把 `bindings/python` 纳入成员，`bindings/golang` 和 `examples` 被排除（它们有自己独立的构建方式）。
- [Cargo.toml:L5-L8](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L5-L8) — 包名 `sgl-model-gateway`，版本 `0.3.2`，Rust 2021 edition。

**② 一个库 + 三个二进制别名**

- [Cargo.toml:L20-L22](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L20-L22) — 库名设为 `smg`，类型是 `rlib`（普通 Rust 库），可以被其他 Rust 代码或 Python/Go 绑定引用。
- [Cargo.toml:L24-L34](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L24-L34) — 同一份 `src/main.rs` 同时产出三个二进制：`sgl-model-gateway`、`smg`、`amg`。**三个名字指向完全相同的程序**，只是方便不同习惯的人敲短命令。README 的版本检查小节也印证了这点（`smg --version` / `amg --version` 等价）。

**③ 外部依赖边界（重点）**

网关把可复用的能力拆成了多个独立 crate。下面这张表把关键依赖和它们服务的子系统对应起来：

| 外部 crate | 服务的子系统 | 在依赖列表里的位置 |
|------------|--------------|--------------------|
| `openai-protocol` | OpenAI 兼容的请求/响应类型（数据面共用） | [Cargo.toml:L83](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L83) |
| `reasoning-parser` | 分离 `<think>` 推理内容（gRPC / parse 端点） | [Cargo.toml:L82](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L82) |
| `tool-parser` | 提取 tool/function call（gRPC / parse 端点） | [Cargo.toml:L84](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L84) |
| `llm-tokenizer` | 本地 tokenize/detokenize（gRPC 流水线） | [Cargo.toml:L85](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L85) |
| `smg-auth` | API Key / JWT 鉴权（可靠性/安全层） | [Cargo.toml:L86](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L86) |
| `wfaas` | 工作流引擎（控制面 Worker 注册步骤） | [Cargo.toml:L87](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L87) |
| `data-connector` | 历史存储连接器（memory/none/oracle/postgres/redis） | [Cargo.toml:L88](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L88) |
| `smg-mcp` | MCP 客户端（工具调用循环） | [Cargo.toml:L89](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L89) |
| `smg-wasm` | WASM 中间件扩展 | [Cargo.toml:L90](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L90) |
| `smg-mesh` | Mesh 多实例状态同步（CRDT） | [Cargo.toml:L91](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L91) |
| `smg-grpc-client` | gRPC 客户端（gRPC 数据面） | [Cargo.toml:L114](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L114) |

此外还有一些「通用基础设施」依赖值得留意：`axum` + `tower`（Web 框架与中间件）、`reqwest`（HTTP 客户端，用于转发）、`tonic` + `prost`（gRPC）、`kube` + `k8s-openapi`（K8s 服务发现）、`tokio`（异步运行时）、`opentelemetry` + `metrics-exporter-prometheus`（可观测性）、`wasmtime`（WASM 运行时）、`crdts`（Mesh 的 CRDT 数据结构）。这些依赖本身就能告诉你网关用了什么技术栈。

**④ 编译档位（profile）**

- [Cargo.toml:L176-L187](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L176-L187) — `release` 档位用 `opt-level = "z"`（体积优先）+ `lto = "fat"` + `codegen-units = 1`，追求最小二进制；`ci` 档位继承 release 但用更轻的优化（`opt-level = 2`、`lto = "thin"`），在「运行够快」和「编译够快」之间取平衡。这说明网关对**二进制体积**和**CI 编译耗时**都有明确要求，是面向生产部署的项目。

#### 4.2.4 代码实践

**实践目标**：亲手从 `Cargo.toml` 里把「本项目自研的 smg-* / 业务 crate」和「通用第三方 crate」分开，建立依赖边界直觉。

**操作步骤**：

1. 打开本仓库的 [Cargo.toml:L36-L129](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L36-L129)（`[dependencies]` 一整段）。
2. 用编辑器搜索或如下命令，列出所有「业务相关」的 crate：
   ```bash
   # 在 sgl-model-gateway 目录下执行（只读检索）
   grep -nE '^(smg-|reasoning-parser|openai-protocol|tool-parser|llm-tokenizer|wfaas|data-connector)' Cargo.toml
   ```
3. 对每一个命中的 crate，参照上面那张表，写一句「它服务哪个子系统」。
4. 再挑出 3 个通用第三方 crate（如 `axum`、`tonic`、`kube`），思考：如果换掉它，会影响哪个子系统？

**需要观察的现象**：业务 crate 的版本大多是 `=1.0.0` 这种「锁定精确版本」的写法（前缀 `=`），说明它们和网关是**强耦合、协同发布**的（很可能是同一个组织维护的姊妹 crate）；而通用 crate 多用 `^` 范围版本。

**预期结果**：得到一张「crate → 子系统」对照表，至少覆盖 8 个业务 crate。

> 上述 grep 命令的输出**待本地验证**（取决于本地仓库是否干净），但表里列出的 crate 名都来自真实的 `Cargo.toml`，可以放心引用。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sgl-model-gateway`、`smg`、`amg` 三个二进制指向同一个 `src/main.rs`？这样设计有什么好处？

> **参考答案**：因为它们是**同一个程序的别名**，功能完全相同。好处是不同场景下大家习惯的命令名不一样（文档里混用 `smg` 和 `sgl-model-gateway`，Python 侧又常用 `amg`），提供多个名字可以减少混淆，同时只维护一份入口代码。

**练习 2**：`reasoning-parser` 和 `tool-parser` 是 gRPC 数据面最依赖的两个 crate。结合 README，猜一下它们为什么对 gRPC 模式特别重要？

> **参考答案**：gRPC 模式宣称「全 Rust 的 tokenize / reasoning / tool 解析」。也就是说，原本由 Python 做的「把模型输出里的 `<think>` 推理段分离出来」「把工具调用 JSON 提取出来」这些工作，被搬到了 Rust 进程里。`reasoning-parser` 和 `tool-parser` 就是承载这些解析逻辑的 crate，所以对 gRPC 模式至关重要。

### 4.3 lib.rs：库的模块导出地图

#### 4.3.1 概念说明

如果说 `Cargo.toml` 是「外部边界」，那么 `src/lib.rs` 就是「内部地图」。在 Rust 里，`lib.rs` 是一个库 crate 的根，它用 `pub mod xxx;` 声明「我对外暴露哪些子模块」。读一遍 `lib.rs`，就能拿到这个项目所有顶层模块的清单，相当于拿到了一张目录索引。

#### 4.3.2 核心流程

`lib.rs` 做两件事：

1. **声明自有模块**：用 `pub mod <名字>;` 把 `src/<名字>/` 或 `src/<名字>.rs` 暴露出来；
2. **重导出外部 crate**：用 `pub use <crate> as <别名>;` 把外部依赖换个短名字再暴露，方便项目内部统一引用。

#### 4.3.3 源码精读

整个 `lib.rs` 只有 16 行，却列出了全部顶层模块：

- [src/lib.rs:L1-L16](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/lib.rs#L1-L16) — 这是本讲最重要的一处引用。把它拆开看：

| `lib.rs` 里的声明 | 对应的源码目录 | 大致职责 |
|------------------|----------------|----------|
| `pub mod app_context;` | `src/app_context.rs` | 共享状态聚合（把各子系统攒到一起） |
| `pub mod config;` | `src/config/` | CLI 参数与配置类型、校验 |
| `pub mod core;` | `src/core/` | 核心抽象：Worker、注册表、作业队列、熔断、重试、限流 |
| `pub mod middleware;` | `src/middleware.rs` | axum 中间件链（认证、并发限流、WASM） |
| `pub mod observability;` | `src/observability/` | 指标、追踪、日志 |
| `pub mod policies;` | `src/policies/` | 负载均衡策略族 |
| `pub mod routers;` | `src/routers/` | 数据面：HTTP / gRPC / OpenAI / Mesh 路由器 |
| `pub mod server;` | `src/server.rs` | Axum 应用与启动编排 |
| `pub mod service_discovery;` | `src/service_discovery.rs` | K8s 服务发现 |
| `pub mod version;` | `src/version.rs` | 版本信息 |
| `pub mod wasm;` | `src/wasm/` | WASM 中间件管理 |

下半部分是四个「外部 crate 重导出」：

- `pub use smg_auth as auth;` — 鉴权能力直接以 `auth` 暴露
- `pub use openai_protocol as protocols;` — OpenAI 协议类型以 `protocols` 暴露
- `pub use reasoning_parser;` — 推理解析
- `pub use llm_tokenizer as tokenizer;` — tokenizer
- `pub use tool_parser;` — 工具解析

> 这个「自有模块 + 外部重导出」的混合写法，正是上一节 `Cargo.toml` 依赖边界的代码侧印证：外部 crate 被「吸收」进 `smg` 库的命名空间，对内就像自家模块一样使用。

附带一个小证据：`src/version.rs` 通过 `env!` 宏在编译期注入构建信息，这就是 `--version-verbose` 能打印 git commit、编译器版本的原因：

- [src/version.rs:L11-L20](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/version.rs#L11-L20) — 这些常量来自 `build.rs` 在编译时写进的环境变量（`SGL_MODEL_GATEWAY_*`）。

另外，`src/main.rs` 里有一个 `Backend` 枚举，说明网关不只服务 SGLang，还能对接多种后端：

- [src/main.rs:L56-L67](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L56-L67) — `Backend` 枚举包含 `sglang`、`vllm`、`trtllm`、`openai`、`anthropic` 五种取值。这印证了 README 说的「深度优化 SGLang，但也能接其他后端」。

#### 4.3.4 代码实践

**实践目标**：把 `lib.rs` 里的模块名，映射到本讲后面要画的「四区域架构」上，提前建立模块与职责的对应关系。

**操作步骤**：

1. 打开 [src/lib.rs:L1-L16](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/lib.rs#L1-L16)。
2. 画一张三列表格：`模块名 | 对应目录 | 猜它属于哪个区域（控制面/数据面/可靠性/可观测性/配置/扩展）`。
3. 对拿不准的模块（比如 `app_context`、`policies`），先写下你的猜测，下一节会给出答案。

**需要观察的现象**：有些模块归属很清晰（`observability` → 可观测性、`routers` → 数据面），有些模块则是「横切」的（比如 `core` 里既有控制面的 `job_queue`，又有可靠性的 `circuit_breaker`/`retry`/`token_bucket`）。

**预期结果**：得到一张初步映射表。重点是发现 **`core` 模块是跨区域的**——它既包含控制面的 Worker/注册表/作业队列，也包含可靠性的熔断/重试/限流。这个发现会在第 3、6 单元被细化。

#### 4.3.5 小练习与答案

**练习 1**：`lib.rs` 里 `pub use openai_protocol as protocols;` 这一行的作用是什么？为什么不直接在代码里写 `openai_protocol::chat::...`？

> **参考答案**：它把外部 crate `openai_protocol` 重命名为更短的 `protocols` 再暴露。好处有二：①项目内部统一用 `crate::protocols::...` 引用，名字更短更稳定；②如果将来换了底层实现，只需改这一行重导出，所有引用点不用动。

**练习 2**：`core` 模块同时包含 `job_queue`（作业队列）和 `circuit_breaker`（熔断器）。请判断它们分别属于哪个区域。

> **参考答案**：`job_queue` 属于**控制面**——它串行化 Worker 的增删等后台管理操作；`circuit_breaker` 属于**可靠性层**——它在数据面转发请求时判断某个 Worker 是否「暂时不可用」。两者都在 `core` 里，是因为它们都是「核心抽象」，但服务的是不同的横切关注点。

### 4.4 四区域架构：控制面 / 数据面 / 可靠性 / 可观测性

#### 4.4.1 概念说明

把前面三节综合起来，网关内部其实可以看成**四个区域加一条主链路**。这是本讲最想让你带走的一张图，也是整套学习手册的骨架：

- **控制面（Control Plane）**：管理 Worker 的生命周期。
- **数据面（Data Plane）**：转发每一次推理请求。
- **可靠性层（Reliability）**：让流量在故障下仍能流动（重试/熔断/限流/排队）。
- **可观测性层（Observability）**：把上面三者「正在做什么」变成指标、追踪和日志。

这四个区域不是物理隔离的进程，而是**同一个 Rust 进程里、不同子系统的逻辑分组**。

#### 4.4.2 核心流程

一次「带注册 + 推理」的完整流程，会依次穿过这四个区域：

```
                    ┌─────────────── 可观测性层（横切，贯穿全程）───────────────┐
                    │   metrics(40+) · OpenTelemetry trace · 结构化日志         │
                    └──────────────────────────────────────────────────────────┘
   控制面                                数据面                              可靠性层
┌──────────────┐                 ┌──────────────────┐               ┌──────────────────┐
│ POST /workers│  ① 异步作业入队  │ 客户端请求到达    │  ③ 选 worker  │ 重试 RetryExecutor│
│  → JobQueue  │ ──────────────► │   RouterManager  │ ────────────► │ 熔断 CircuitBreaker│
│ WorkerManager│                 │   (HTTP/gRPC/    │               │ 限流 TokenBucket   │
│ 注册表/探活   │  ② 后台注册完成  │    OpenAI/IGW)   │  ④ 转发worker │ 排队 concurrency   │
│ 服务发现      │ ◄────────────── │   策略选 worker   │ ◄─────────── │                    │
└──────────────┘                 └──────────────────┘               └──────────────────┘
                                          │ ⑤ 流式回传
                                          ▼
                                       客户端
```

读图要点：

1. **控制面与数据面解耦**：Worker 的注册（控制面）通过 JobQueue 异步完成，**不会阻塞**正在转发的推理请求（数据面）。这就是 README 反复强调「Job Queue serializes background operations」的意义。
2. **可靠性层嵌在数据面里**：选 worker 之前先过限流/排队，转发时套上重试与熔断。它不是独立的一跳，而是数据面的「安全包装」。
3. **可观测性层是横切的**：它不参与请求转发，但会监听所有区域的动作，产出指标和追踪。

#### 4.4.3 源码精读

回到 README 的「Architecture at a Glance」与「Reliability / Observability」两节，可以逐字对应到这四个区域：

- 控制面 → [README.md:L15-L19](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L15-L19)
- 数据面 → [README.md:L21-L27](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L21-L27)
- 可靠性层（限流/重试/熔断/排队） → [README.md:L728-L734](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L728-L734)
- 可观测性层（40+ 指标、OTLP 追踪、结构化日志） → [README.md:L743-L773](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L743-L773)

而 `src/lib.rs` 的模块清单（[src/lib.rs:L1-L16](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/lib.rs#L1-L16)）正好可以落在这四个区域上：

| 区域 | 主要模块（来自 lib.rs / core 子模块） | 对应学习单元 |
|------|--------------------------------------|--------------|
| 控制面 | `core`（worker / worker_registry / worker_manager / job_queue / steps）、`service_discovery` | 第 3 单元 |
| 数据面 | `routers`（http / grpc / openai / mesh）、`router_manager`、`policies` | 第 4、5、7、8 单元 |
| 可靠性层 | `core`（circuit_breaker / retry / token_bucket）、`middleware`、外部 `auth` | 第 6 单元 |
| 可观测性层 | `observability`（metrics / otel_trace / logging / inflight_tracker） | 第 9 单元 |
| 配置与装配 | `config`、`app_context`、`server` | 第 2 单元 |
| 扩展点 | `wasm`、外部 `smg-mesh` / `smg-mcp` | 第 9 单元 |

> 一句话总结这张图：**`config` 决定怎么建、`app_context` 把各区域攒到一起、`server` 负责启动并把数据面挂到 HTTP/gRPC 端口上，`core` 提供控制面 + 可靠性的共用底座，`routers` + `policies` 是数据面，`observability` 贯穿全程**。后面每一讲，本质上都是在放大这张图里的某一块。

#### 4.4.4 代码实践

**实践目标**：用一个具体的「指标名」串起四个区域，体会可观测性如何观测其它三个区域。

**操作步骤**：

1. 打开 [README.md:L751-L773](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L751-L773)（指标分类表）。
2. 找到这几个指标，判断它们各自观测的是哪个区域：
   - `smg_worker_*`（Worker 池大小、健康检查）→ 观测 ____ 面？
   - `smg_router_*`（按模型/端点的请求数、延迟）→ 观测 ____ 面？
   - `smg_worker_cb_*`（熔断器状态、转换次数）→ 观测 ____ 层？
   - `smg_worker_retries_*`（重试次数、退避时长）→ 观测 ____ 层？

**需要观察的现象**：你会发现同一套可观测性层，从四个不同角度（worker / router / 熔断 / 重试）去观测系统，正好覆盖了控制面、数据面和可靠性层。

**预期结果**：填出 `控制面 / 数据面 / 可靠性 / 可靠性`。这说明可观测性层确实是「横切」的，它本身不产生业务行为，而是把其它区域的行为变得可见。

> 运行 Prometheus 抓取 `/metrics` 的实际输出**待本地验证**；本实践只需要阅读 README 的指标表即可完成。

#### 4.4.5 小练习与答案

**练习 1**：为什么控制面的 Worker 注册要走 JobQueue 异步处理，而不是在收到 `POST /workers` 时直接同步注册完？

> **参考答案**：因为注册一个 Worker 要做很多慢操作（探测 `/server_info`、`/get_model_info`、加载 tokenizer、探活），如果同步处理，HTTP 请求会长时间挂起，客户端体验差、还可能超时。用 JobQueue 异步处理后，接口可以立即返回 `202 Accepted`，真正的注册在后台进行，状态通过 `/workers/{id}` 查询。

**练习 2**：假设某个 Worker 突然崩溃，请按「四区域」描述网关会如何应对。

> **参考答案**：① 可靠性层最先反应——熔断器在连续失败后打开，后续请求不再选这个 Worker，已发出的请求由重试执行器重试到别的 Worker；② 控制面随后跟上——健康检查探针标记该 Worker 为不健康，注册表里它的状态翻转；③ 数据面在选 worker 时会跳过不健康/熔断中的实例；④ 可观测性层全程记录 `smg_worker_cb_state`、`smg_worker_retries_*` 等指标，运维据此告警。

## 5. 综合实践

本讲的综合实践，就是把前面四个模块的产出**拼成一张完整的架构草图**。这是本讲规格里要求的核心实践任务。

**实践目标**：画出一张包含「控制面 / 数据面 / 可靠性 / 可观测性」四个区域的 sgl-model-gateway 架构图，并标注每类路由器依赖的外部 crate。

**操作步骤**：

1. **收集信息**（只读，不修改任何源码）：
   - 通读 [README.md:L1-L27](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L1-L27)，确定四个区域的职责描述。
   - 通读 [Cargo.toml:L36-L129](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/Cargo.toml#L36-L129)，圈出所有 `smg-*` 与业务 crate。
   - 通读 [src/lib.rs:L1-L16](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/lib.rs#L1-L16)，把模块名落到区域上。
2. **画图**（用纸笔、白板或任意画图工具均可，不必用代码）：
   - 画一个大矩形表示「sgl-model-gateway 进程」。
   - 在大矩形内部画四个子区域：左上控制面、右上数据面、左下可靠性、右下可观测性（可观测性画成贯穿大矩形的横条更准确）。
   - 在「数据面」区域里画出四类路由器：HTTP Router（regular + PD）、gRPC Router、OpenAI Router、RouterManager（IGW）。
   - 在「控制面」区域画出 WorkerManager、JobQueue、WorkerRegistry、Health Check、Service Discovery。
   - 在「可靠性」区域画出 RetryExecutor、CircuitBreaker、TokenBucket、Concurrency Queue。
3. **标注外部 crate**：在每类路由器旁边标注它最依赖的外部 crate，参考答案：
   - HTTP Router → `reqwest`（转发）、`openai-protocol`（协议类型）
   - gRPC Router → `tonic`/`prost`/`smg-grpc-client`（gRPC）、`llm-tokenizer`（本地 tokenize）、`reasoning-parser`/`tool-parser`（解析）
   - OpenAI Router → `reqwest`（代理）、`data-connector`（历史存储）、`smg-mcp`（MCP 工具循环）
   - 控制面 → `wfaas`（工作流引擎）
   - 可观测性 → `metrics-exporter-prometheus`、`opentelemetry-otlp`
4. **画外部依赖**：在大矩形左侧画「客户端」，右侧画一排 Worker 框，用箭头表示「客户端 → 网关 → Worker」的请求方向。

**需要观察的现象**：画完后，你应该能一眼看出——数据面是「宽而多」的（多种路由器），控制面是「深而有序」的（注册流水线），可靠性是「横切包裹」数据面的，可观测性是「覆盖一切」的。

**预期结果**：一张可复用的架构草图。后续每一讲学新模块时，都可以回到这张图，把对应的小方块涂亮，标记「已掌握」。

> 评判标准（自检）：
> - 四个区域是否都画到了？✓
> - 每类路由器是否都标注了至少一个外部 crate？✓
> - 是否体现了「控制面异步、数据面同步转发」的解耦？✓
>
> 如果三问都答「是」，本讲的目标就达成了。

## 6. 本讲小结

- sgl-model-gateway 是面向大规模 LLM 部署的**模型路由控制面 + 数据面**，深度优化 SGLang 运行时，也能接 vLLM/TRT-LLM/OpenAI 等后端。
- 它的内部可以分成四个区域：**控制面**（Worker 注册/探活/发现）、**数据面**（HTTP/gRPC/OpenAI/IGW 路由）、**可靠性层**（重试/熔断/限流/排队）、**可观测性层**（40+ 指标/OTLP 追踪/日志）。
- `Cargo.toml` 是「外部边界」：一个名为 `smg` 的库 + 三个二进制别名（`sgl-model-gateway`/`smg`/`amg`），依赖一批 `smg-*` 协同发布的姊妹 crate。
- `src/lib.rs` 是「内部地图」：11 个自有模块 + 5 个外部 crate 重导出，构成了网关的全部顶层结构。
- 控制面与数据面**解耦**：Worker 注册走 JobQueue 异步处理，不阻塞推理转发；可靠性层以「包装」方式嵌在数据面里；可观测性层横切所有区域。
- `core` 模块是跨区域的底座，既有控制面的 `job_queue`/`worker_registry`，也有可靠性的 `circuit_breaker`/`retry`/`token_bucket`。

## 7. 下一步学习建议

本讲只建立了「地图」，还没真正跑起来或读进任何一个模块的细节。建议按以下顺序继续：

1. **下一讲 u1-l2（构建、安装与发布）**：学会用 `cargo build --release` 和 `maturin` 把网关真正构建出来，理解 `release` / `ci` / `dev` 三个 profile 的取舍。
2. **u1-l3（快速启动与运行模式）**：亲手用 `--worker-urls` 启动一次常规 HTTP 路由，验证本讲画的架构图里「客户端 → 网关 → Worker」这一段。
3. **u1-l4（源码目录结构与模块地图）**：把本讲的「四区域」落到 `src/` 的具体子目录上，为进入第 2 单元（配置与启动链路）做准备。

如果想提前感受「差异化能力」，可以在学完 u1-l2 后，直接跳到 README 的 gRPC Routing 小节（[README.md:L211-L229](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/README.md#L211-L229)）扫一眼，建立对「全 Rust gRPC 流水线」的期待——那是第 7 单元的主题。
