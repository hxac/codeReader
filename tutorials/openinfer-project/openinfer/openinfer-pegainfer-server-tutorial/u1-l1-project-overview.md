# 项目定位与架构地图：pegainfer-server 是什么

> 学习阶段：入门（beginner）· 依赖：无（这是本手册的第一篇）

## 1. 本讲目标

读完本讲，你应该能够：

1. 用两三句话说清 **PegaInfer** 的定位：一个纯 Rust + CUDA 的 LLM 推理引擎，对外提供 OpenAI 兼容的 `/v1` HTTP API，不依赖 PyTorch 或 Python 运行时。
2. 说清 **pegainfer-server** 这个 crate 在整个工作区里的职责：它只是一个约 300 行的「纯分发」二进制——检测模型、校验 CLI、启动所选引擎、把服务交给前端，**不含任何模型逻辑**。
3. 对照 README 的架构边界表，指出 server / frontend / 模型 crate / core+kernels / KV 基础设施各层分别负责什么，以及一条请求大致穿过哪些层。
4. 解释 Cargo 的 **feature 矩阵**如何决定「这个二进制里编译进了哪几条模型线」，并知道默认构建只带 `qwen3`。

本讲不要求你写过 Rust 大型工程，但需要你能在终端里执行 `cargo` 命令、能读懂基本的 Rust 语法。

## 2. 前置知识

用通俗语言把几个基础概念先铺平：

- **LLM 推理引擎（inference engine）**：加载模型权重到 GPU，接收文本请求，逐个生成 token 并返回的服务程序。生成分两个阶段：**prefill**（把输入 prompt 一次性算完）和 **decode**（逐 token 生成）。引擎的核心工作是调度这两类计算、管理 KV 缓存。
- **OpenAI 兼容 API**：指暴露 `/v1/completions`、`/v1/chat/completions` 这类路由，请求/响应格式与 OpenAI 官方 API 一致。这样任何 OpenAI 客户端 SDK 指向本服务即可使用，无需改造。
- **Cargo workspace 与 crate**：一个仓库（repo）里可以放多个 Rust 包（crate），由根目录的 `Cargo.toml` 统一管理依赖版本。本仓库就是一个 workspace，`pegainfer-server` 是其中之一。
- **Cargo feature**：编译期的「开关」。crate 可以把一部分代码（甚至一部分依赖）藏在 feature 后面，只有 `--features xxx` 构建时才编译进来。本项目的每条模型线就是一个 feature。
- **纯 Rust + CUDA**：模型的算子直接用 Rust 绑定 CUDA/cuBLAS/FlashInfer 等原生库实现，构建期不需要 Python（个别模型线的构建期 AOT kernel 生成除外）。
- **ModelLine（模型线）**：本项目术语，指一类模型（如 Qwen3、Gemma 4）对应的完整实现 crate，包含配置解析、权重加载、调度器与执行。server 二进制通过一个统一的 trait（`ModelLine`）与它们对话——这个 trait 是第二单元的主角，本讲先只要知道它的存在。

## 3. 本讲源码地图

本讲涉及的关键文件（路径相对 `pegainfer-server/` 目录，与文末永久链接一致）：

| 文件 | 作用 |
| --- | --- |
| [../README.md](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md) | 工作区根 README：项目定位、快速开始、受支持模型表、**架构边界表**（本讲的地图来源） |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml) | server crate 的清单：二进制名、**feature 矩阵**、可选依赖 |
| [src/main.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs) | 整个二进制的唯一源码文件：模块文档（自我定位）、`model_lines()` 注册表、`families()` 身份表、`main()` 启动流程 |
| [../Cargo.toml](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml) | 工作区根清单：`default-members = ["pegainfer-server"]`，解释了为什么在仓库根直接 `cargo run` 就能跑 server |

pegainfer-server 这个 crate 小得惊人：`src/` 下只有一个 `main.rs`（338 行，其中约 50 行是测试），外加一个集成测试 `tests/detect_gate.rs`。**它的全部实质内容都在它引用的 `pegainfer-frontend` 契约里**——这正是本手册以 server 为锚点、逐步深入 frontend 的原因。

## 4. 核心概念与源码讲解

### 4.1 PegaInfer 全景：定位与分层架构（README 架构章节）

#### 4.1.1 概念说明

先建立全局观。PegaInfer 是什么？README 开篇一句话给出定位：

> PegaInfer serves LLMs through an OpenAI-compatible API. Each model owns its scheduler, state, and kernels; serving and KV infrastructure are shared. No PyTorch or Python runtime.

翻译过来有三层信息：

1. **对外**：OpenAI 兼容 API，监听 8000 端口（`pegainfer --model-path models/Qwen3-4B` 即可起服务）。
2. **对内**：设计原则是「**共享基础设施；让每个模型拥有自己的执行**」（README 原文：Share the infrastructure; let each model own its execution）。每条模型线自带调度器、状态与 kernel；协议栈、分词、KV 存储这些跨模型的能力则共享。
3. **技术栈**：纯 Rust + CUDA，无 PyTorch、无 Python 运行时（注意：前端复用了 vLLM 的 **Rust** 前端 crate，这与「基于 vLLM/Python」是两回事）。

在这张地图里，pegainfer-server 处在**最外缘的装配位置**：它不实现协议、不实现模型，只负责「把正确的引擎装配起来交给前端」。

#### 4.1.2 核心流程

一条 HTTP 请求的生命周期（本讲只看分层，不看细节）：

```text
OpenAI 客户端（curl / openai SDK）
        │  HTTP POST /v1/completions 或 /v1/chat/completions
        ▼
pegainfer-frontend ── OpenAI 协议、分词(tokenization)、chat 模板、流式、指标
        │  引擎契约（engine contract：请求/事件的数据类型约定）
        ▼
模型 crate（如 pegainfer-qwen3）── 权重、调度器、prefill/decode 执行、并行策略
        │
        ├── pegainfer-core / pegainfer-sample ── 共享 GPU 原语、权重加载、批量采样
        ├── pegainfer-kernels ── 原生 kernel、FFI、GPU 库接入、feature 门控的 AOT 构建
        └── KV 基础设施 ── pegainfer-kv-store / kv-cache / kv-offload，PegaFlow 提供 host/SSD/RDMA 分层
```

而 server 在这条链路的**上游**，负责启动时刻的装配：

```text
CLI 参数（--model-path 等）
        ▼
pegainfer-server：读 config.json → 检测模型家族 → 校验 CLI → 启动引擎 → 选择 serve 分支
        ▼
pegainfer-frontend 的 serve 函数：起 HTTP 服务，把请求喂给引擎
```

#### 4.1.3 源码精读

**① 项目定位句** —— [../README.md:31](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L31)：这一行就是 PegaInfer 的「一句话自我介绍」，本节开头的引文即出自此处。

**② 受支持模型表** —— [../README.md:142-156](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L142-L156)：README 明确说「Only `qwen3` is enabled by default... At launch, `--model-path` selects a checkpoint and its `config.json` identifies the model family」——即启动时靠模型目录里的 `config.json` 识别家族（这正是 server 的检测门要做的事，详见 u1-l4）。表格列出 7 条模型线（qwen3、qwen35、gemma4、deepseek-v2-lite、kimi-k2、glm52、k3）各自的 feature 名与能力范围。**注意：这张表是 7 行，而代码里的 feature 有 8 个——多出来的 `qwen4` 是实验线（`model_type` 为 `qwen4_exp`），尚未列入 README 的受支持模型表**。这种「文档与代码的错位」在真实项目里很常见，也是本讲综合实践要你亲手核对的点。

**③ 架构边界表（本讲最重要的表）** —— [../README.md:158-181](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L158-L181)：README 用一张所有权分层图加一张边界表描述架构。表的内容整理如下：

| 边界 | 职责（README 原文的中文转述） |
| --- | --- |
| `pegainfer-server` | **检测模型、校验其 CLI、启动所选引擎**——仅此三件事 |
| `pegainfer-frontend` | OpenAI 协议、分词、chat 模板、流式、指标，以及**引擎契约**；复用 vLLM 的 Rust 前端 crate |
| 各模型 crate | 权重、调度器、prefill/decode 执行、状态布局、并行策略 |
| `pegainfer-core` / `pegainfer-sample` | 共享 GPU 与权重加载原语；批量 token 采样 |
| `pegainfer-kernels` | 原生 kernel、FFI、GPU 库接入、feature 门控的 AOT 构建 |
| KV 基础设施 | `pegainfer-kv-store` 与 `pegainfer-kv-cache` / `pegainfer-kv-offload`；PegaFlow 提供 host/SSD/RDMA 存储与传输 |

**④ 契约共存说明** —— [../README.md:182](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L182)：README 坦率说明「基于 step 的契约与 legacy `EngineHandle` 契约当前并存」。本讲只需记住这个事实；两代契约的完整地图是第三单元（u3-l1）的内容。

**⑤ API 示例** —— [../README.md:184-196](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L184-L196)：给出了 `/v1/completions` 与流式 `/v1/chat/completions` 的 curl 示例。服务跑起来后可以用这两条命令验证。

#### 4.1.4 代码实践

**实践目标**：把 README 的架构边界表「落地」成你机器上真实可见的目录。

**操作步骤**：

1. 在仓库根目录执行 `ls`，对照边界表逐层找目录：`pegainfer-server`、`pegainfer-frontend`、`pegainfer-core`、`pegainfer-kernels`、`pegainfer-kv-store`、以及各模型 crate（`pegainfer-qwen3`、`pegainfer-gemma4`……）。
2. 再 `ls pegainfer-server`，亲眼确认这个 crate 只有一个 `Cargo.toml`、`src/main.rs`、`tests/detect_gate.rs`。
3. 打开根目录 `Cargo.toml`，找到 `default-members = ["pegainfer-server"]`（[../Cargo.toml:2](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/Cargo.toml#L2)）。

**需要观察的现象**：README 表里的每个「边界」都对应一个真实存在的成员 crate 目录；pegainfer-server 是其中最薄的一个。

**预期结果**：你能在不翻代码的情况下说出「HTTP 层在哪、模型逻辑在哪、GPU 原语在哪」。同时解释一个疑问：为什么 README 说「The server entrypoint is `pegainfer-server`」，而快速开始里直接 `cargo run --release -- --model-path ...` 就行？——因为 workspace 的 `default-members` 只含 `pegainfer-server`，在仓库根裸跑 `cargo run` 时 cargo 知道该跑谁。

#### 4.1.5 小练习与答案

**练习 1**：PegaInfer 说自己「No PyTorch or Python runtime」，但前端又「uses vLLM's Rust frontend crates」，这两句话矛盾吗？

**答案**：不矛盾。vLLM 项目里有一批纯 Rust 写的前端 crate（协议处理等），PegaInfer 复用的是这批 **Rust** 代码；它没有引入 vLLM 的 Python 推理栈。构建出的二进制不携带 Python 解释器（`qwen35` 等线在**构建期**需要 Python + Triton 做 AOT kernel 生成，但那是编译工具链，不是运行时依赖）。

**练习 2**：一个 `/v1/chat/completions` 请求的 chat 模板渲染、流式输出分别由哪一层负责？

**答案**：都是 `pegainfer-frontend`（README 边界表：「OpenAI protocol, tokenization, chat templates, streaming, metrics, and engine contracts」）。模型 crate 只见 token，不见 HTTP。

**练习 3**：如果把「读取 `config.json` 判断模型家族」这段逻辑实现在某个模型 crate 里，为什么不合理？

**答案**：因为判断「这是哪条线」发生在选择模型 crate **之前**——必须先识别家族，才知道要加载哪个 crate 的实现。所以检测逻辑只能放在所有模型线之外、且能同时看到所有已编译线的地方，即 server 的注册表（`ModelLineRegistry`）。这也是下一模块 feature 矩阵存在的意义：被编译掉的线根本不参与检测。

### 4.2 feature 矩阵：Cargo.toml 如何决定二进制里有哪些模型线

#### 4.2.1 概念说明

一个关键问题：**为什么不是所有模型都编译进同一个二进制？**

因为模型线的成本差异巨大：`qwen3` 是纯 Rust + CUDA，构建无需 Python；而 `qwen35` 需要构建期跑 Triton AOT（要装 Python + Triton），`glm52`/`k3` 需要 TileLang 生成 kernel，`kimi-k2`/`glm52` 还要额外的 CUDA 源码。把所有线无条件编译，意味着「只想跑 Qwen3 的用户」也得配齐整套工具链。

Cargo 的 feature + optional dependency 机制正好解决这个问题：

- 在 `[dependencies]` 里把各模型 crate 声明为 `optional = true`——默认不编译。
- 在 `[features]` 里为每条线开一个同名开关，开关激活时用 `dep:pegainfer-xxx` 把可选依赖「点亮」。
- `default = ["qwen3"]` 让默认构建只带 Qwen3，其他线按需 `--features` 拉入。

#### 4.2.2 核心流程

feature 激活的传导链：

```text
cargo build --features gemma4
        │
        ▼  Cargo.toml [features]：gemma4 = ["dep:pegainfer-gemma4", "pegainfer-gemma4/gemma4"]
        │
        ├── dep:pegainfer-gemma4          → 可选依赖被点亮，模型 crate 参与编译
        └── pegainfer-gemma4/gemma4       → 把同名 feature 转发进模型 crate 内部，
                                            激活其内部门控的实现（kernel、构建脚本产物等）
        │
        ▼  src/main.rs 里 #[cfg(feature = "gemma4")]
        │
        ▼  model_lines() 的注册向量里多出 &pegainfer_gemma4::model_line::MODEL_LINE
```

注意两种写法的区别：`qwen3 = ["dep:pegainfer-qwen3"]` 只拉依赖；而 `gemma4`、`glm52`、`k3`、`kimi-k2`、`qwen35`、`deepseek-v2-lite` 都带第二项转发。原因是这些模型 crate 把自己的实现藏在 crate 内部的同名 feature 之后（比如构建期的 AOT kernel 生成按 feature 触发），server 必须转发激活；而 `pegainfer-qwen3` 的 `[features]` 里只有诊断类开关（`kernel-call-trace`、`kernel-report`），没有 `qwen3` feature，编译即全量，所以只拉依赖就够。

#### 4.2.3 源码精读

**① 二进制名与 crate 名** —— [Cargo.toml:1-11](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L1-L11)：crate 叫 `pegainfer-server`（version 0.1.1），但产出的**二进制**叫 `pegainfer`（`[[bin]] name = "pegainfer"`，`default-run = "pegainfer"`）。所以命令行里敲的是 `pegainfer --model-path ...`，而 `cargo` 操作时用 `-p pegainfer-server`。`--version` 输出的版本号 `0.1.1` 就来自这里的 `version` 字段。

**② 可选依赖表** —— [Cargo.toml:13-32](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L13-L32)：8 个模型 crate 全部 `optional = true`（pegainfer-deepseek-v2-lite、gemma4、glm52、k3、kimi-k2、qwen3、qwen35、qwen4）；非可选的核心依赖是 `pegainfer-frontend`（契约所在）、`pegainfer-core`（日志与 tracing）、`clap`（CLI）、`anyhow`（错误）、`tokio`/`tokio-util`（异步运行时）。另外在 `[target.'cfg(not(target_env = "msvc"))'.dependencies]` 里给非 Windows-MSVC 平台加了 `tikv-jemallocator`——配合 `main.rs` 里的全局分配器声明使用（见 4.3.3）。

**③ feature 矩阵本体** —— [Cargo.toml:34-48](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L34-L48)：

```toml
[features]
# Qwen3-4B is the default model line: pure Rust + CUDA, no Python at build time.
default = ["qwen3"]
qwen3 = ["dep:pegainfer-qwen3"]
deepseek-v2-lite = [
  "dep:pegainfer-deepseek-v2-lite",
  "pegainfer-deepseek-v2-lite/deepseek-v2-lite",
]
gemma4 = ["dep:pegainfer-gemma4", "pegainfer-gemma4/gemma4"]
...
qwen4 = ["dep:pegainfer-qwen4"]
```

注释点明设计意图：默认线是纯 Rust + CUDA、构建期无 Python 的 Qwen3。这就是「stock build」的含义，也是 CLAUDE.md 里那句话「only `qwen3` is a default feature, so the stock build is pure Rust + CUDA with no Python」的出处。

#### 4.2.4 代码实践

**实践目标**：亲手验证「feature 决定依赖图」。

**操作步骤**：

1. 在仓库根执行 `cargo tree -p pegainfer-server -e features --depth 1 | head -40`，记下默认（qwen3）构建激活的 feature 集。
2. 再执行 `cargo tree -p pegainfer-server -e features --depth 1 --features gemma4 2>/dev/null | head -40`，对比差异（若本机没装 CUDA 工具链导致解析失败，退化为纯阅读对比：默认集里没有 gemma4 行，加 `--features gemma4` 后应出现 `pegainfer-server feature "gemma4"` 及其点亮的 `pegainfer-gemma4`）。

**需要观察的现象**：`--features gemma4` 之后，依赖图里多出 `pegainfer-gemma4` 这个（原本 optional 因而不激活的）节点，以及它内部 feature `gemma4` 的激活记录。

**预期结果**：你能指着两张依赖图的差异，复述 4.2.2 的传导链。若本机无法完成 cargo 解析（无 CUDA 工具链），此步可标注「待本地验证」，改为阅读对比两份 `Cargo.toml` 完成推理。

#### 4.2.5 小练习与答案

**练习 1**：`cargo run --release -p pegainfer-server`（不带任何 `--features`）编译出的二进制里有几条模型线？

**答案**：一条，Qwen3。`default = ["qwen3"]`（[Cargo.toml:35-36](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L35-L36)），其余 7 个 feature 未激活，对应的 optional 依赖不编译，`model_lines()` 里那些 `#[cfg]` 分支全部不存在。

**练习 2**：想在同一个二进制里同时支持 Qwen3 和 Gemma 4，怎么构建？

**答案**：`cargo run --release --features gemma4 ...` 即可——feature 是**叠加**的，`default` 已经含 `qwen3`，再添 `gemma4` 后两条线同时注册进 `model_lines()`。（若想完全自选，可用 `--no-default-features --features "qwen3,gemma4"`。）

**练习 3**：为什么 `qwen35` feature 必须在「构建期」找到 Python + Triton，而这与「运行时无 Python」不冲突？

**答案**：`qwen35` 转发激活了 `pegainfer-qwen35/qwen35`，后者触发构建期用 Triton 做 AOT（提前）kernel 生成——Python 是**编译工具**，产物编进二进制；运行时进程里没有任何解释器。README 的 Feature builds 一节也写了这一步（`uv venv` + `uv pip install triton` + `PEGAINFER_TRITON_PYTHON`）。

### 4.3 main.rs：纯分发二进制的自我描述与 model_lines()

#### 4.3.1 概念说明

`src/main.rs` 开头的模块文档（module doc）是全文件的设计宣言：

```rust
//! The pegainfer server binary: pure dispatch over the compiled-in
//! [`ModelLine`]s. Every model-specific flag, rule, and option type lives in
//! its model crate; this file only wires detection, CLI validation, and the
//! serve path selection together.
```

译成中文：**pegainfer server 二进制是对「已编译进的各条 ModelLine」的纯分发。所有模型专属的 flag、规则和选项类型都住在各自的模型 crate 里；本文件只把检测、CLI 校验和服务路径选择接线到一起。**

「纯分发（pure dispatch）」的含义值得咀嚼：

- server 持有的只是 `&'static dyn ModelLine`（trait 对象的静态引用）——它不知道也不关心某条线的选项具体长什么样。
- 新增一条模型线，对 server 的改动只是：加一个 Cargo feature、在 `model_lines()` 里加一行（可选地在 `families()` 加一行身份表）。
- 所有的「智能」都在别处：检测规则在模型 crate 的 `probe`，CLI 规则在 `augment_cli`/`validate`，引擎启动在 `launch`。

对初学者补充一句：`&'static dyn ModelLine` 里的 `dyn` 表示「编译期不锁定具体类型、运行时按 vtable 分发」；`'static` 表示这些对象以静态量形式存在于各模型 crate 里（如 `pegainfer_qwen3::model_line::MODEL_LINE`），进程全生命周期有效。

#### 4.3.2 核心流程

`main()` 的启动主流程全景（本讲只建立骨架，逐段精读在 u1-l3）：

```text
main()
 ├── logging/tracing 初始化
 ├── ModelLineRegistry::new(model_lines())        ← 把已编译的线装进注册表
 ├── registry.build_command(...) + get_matches()  ← 合并共享 CLI 与各线独占 flag
 ├── 读 <model-path>/config.json
 ├── registry.detect(&config)                     ← 恰好一条线认领才算成功
 │      └─ NoMatch 时尝试 feature_gate_hint 提示「rebuild with --features xxx」
 ├── registry.validate_provided(...) + shared.validate(...) + line.validate(...)
 ├── line.serve_plan(&ctx)                        ← 引擎加载前就需要知道的服务形态
 ├── tokio::task::spawn_blocking(line.launch)     ← 引擎加载与 HTTP 前端并行
 └── 按 plan 选 serve 分支：
       ├── lora_modules 有值 → serve_model_with_lora_routes（要求 Stepped 引擎）
       ├── prefill_only      → serve_prefill_only_with_engine_count
       └── 默认              → serve_with_engine_count
```

本模块聚焦第一步的 `model_lines()`，以及为「被编译掉的线」服务的 `families()` 身份表。

#### 4.3.3 源码精读

**① 模块文档** —— [src/main.rs:1-4](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L1-L4)：「纯分发」的自我定位，4.3.1 已全文引用。

**② 全局分配器** —— [src/main.rs:20-22](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L20-L22)：非 msvc 平台把 jemalloc 声明为全局分配器（LLM 推理服务大量分配释放张量元数据与缓冲，jemalloc 的碎片控制优于系统分配器）。这三行是性能基建，与分发逻辑无关，但属于「为什么这个二进制快」的一部分。

**③ `model_lines()`：注册表的内容清单** —— [src/main.rs:24-43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L24-L43)：

```rust
fn model_lines() -> Vec<&'static dyn ModelLine> {
    vec![
        #[cfg(feature = "deepseek-v2-lite")]
        &pegainfer_deepseek_v2_lite::model_line::MODEL_LINE,
        #[cfg(feature = "gemma4")]
        &pegainfer_gemma4::model_line::MODEL_LINE,
        // ... glm52 / k3 / kimi-k2 ...
        #[cfg(feature = "qwen3")]
        &pegainfer_qwen3::model_line::MODEL_LINE,
        // ... qwen35 / qwen4 ...
    ]
}
```

每个元素都是「一个 `#[cfg]` 守卫 + 一条线的静态 MODEL_LINE」。**这个函数就是 4.2 的 feature 矩阵在代码里的直接映照**：feature 关 → 该行被 cfg 掉 → 该线不参与检测与注册。默认构建下这个 `vec!` 只剩 qwen3 一项。

**④ `families()`：必要的第二真相源** —— [src/main.rs:54-118](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L54-L118)：一张硬编码的身份表，每行 `Family { feature, model_types, text_model_types, compiled }`，其中 `compiled: cfg!(feature = ...)` 在编译期求值。为什么需要它？文档注释说得很直白（[src/main.rs:56-60](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L56-L60)）：**提示存在的意义恰恰是针对「线被编译掉」的情形——线都被编译掉了，它的 `probe` 根本无法运行，身份字符串不可能从该 crate 读出**，只能硬编码在 server 里。它配合 `feature_gate_hint`（[src/main.rs:120-144](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L120-L144)）在检测失败时给出「this looks like a glm52 model; rebuild pegainfer-server with --features glm52」这样的建议。注意文档同时声明该提示是 **identity-only**：它只看 `model_type` 字符串，不校验量化配置、绑定嵌入、YaRN 等变体细节，所以提示「rebuild with」不代表 checkpoint 一定可服务。文件尾部的测试（[src/main.rs:291-338](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L291-L338)）逐一守护这些性质，例如 `hint_names_the_feature_for_a_known_but_uncompiled_family` 断言 `{"model_type": "glm_moe_dsa"}` 在未编译 glm52 时产出含 `--features glm52` 的提示。

**⑤ `main()` 骨架** —— [src/main.rs:146-186](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L146-L186)：从注册表构建到 `serve_plan` 的前半段，正是 4.3.2 流程图的前七行。留意 [src/main.rs:168-175](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L168-L175)：`detect` 失败时若是 `NoMatch`，会尝试用 `feature_gate_hint` 把提示拼进错误信息再返回给用户——这就是「检测门 + feature 提示」的完整体验，u1-l4 会用真实二进制驱动它。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：把「feature 名 → 模型 crate → `families()` 行」三张表对齐，并核实默认构建只含 qwen3。

**操作步骤**：

1. **画映射表**。对照阅读 [Cargo.toml:34-48](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L34-L48) 与 [src/main.rs:67-118](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L67-L118)，亲手画出 8 行映射表：`feature 名 | 激活的模型 crate | families() 里的 model_types | 默认是否编译`。参考答案见本节末尾。
2. **跑 `--help`**。在有 CUDA 工具链与 Rust 环境的机器上（仓库根目录）执行：

   ```bash
   cargo run --release -- --help
   ```

   观察帮助文本：`about` 应为 "PegaInfer GPU inference server"（来自 [src/main.rs:152-156](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L152-L156) 的 `clap::Command::new("pegainfer").version(...).about(...)`），版本号应为 `0.1.1`（来自 Cargo.toml 的 `version`）。再扫一遍 flag 列表：共享 flag（如 `--model-path`、`--port`）来自 `pegainfer-frontend` 的 `SharedArgs`；LoRA 相关的 `--lora-modules`、`--max-loras`、`--max-lora-rank` 等 Qwen3 独占 flag 来自 `pegainfer-qwen3` 的 model line——**帮助文本里不应出现任何其他模型线的独占 flag**（比如 gemma4 的环境变量类选项），因为它们没被编译进来。
3. **用 `compiled` 字段核对**。回到 [src/main.rs:67-118](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L67-L118)：每个 `Family.compiled` 的值是 `cfg!(feature = "...")`。默认构建下 8 行中仅 `qwen3` 一行为 `true`，其余 7 行为 `false`——这正是你从 `--help` 观察到的「只有 qwen3 独占 flag」的代码级解释。也可顺手运行本 crate 的单元测试核对提示行为：

   ```bash
   cargo test --release -p pegainfer-server --bin pegainfer
   ```

   （若本机没有 CUDA 工具链导致无法编译，步骤 2/3 标注「待本地验证」，以步骤 1 的静态推导 + 步骤 2 的源码推导代替。）

**需要观察的现象**：帮助文本是「共享 flag + qwen3 独占 flag」的组合；版本与 about 文案能在 main.rs/Cargo.toml 里逐字找到出处。

**预期结果**：一张 8 行映射表 + 一句结论——「默认（stock）构建编译进的模型线只有 qwen3；deepseek-v2-lite / gemma4 / glm52 / k3 / kimi-k2 / qwen35 / qwen4 七条线被编译掉，它们的 `model_lines()` 行不存在、`families().compiled == false`、检测到对应 config 时只能靠 identity 表给出 rebuild 提示」。

**步骤 1 参考答案（映射表）**：

| feature | 激活的模型 crate | `families()` 的 model_types（主） | 默认编译？ |
| --- | --- | --- | --- |
| `qwen3` | pegainfer-qwen3 | `qwen3` | ✅（default） |
| `qwen35` | pegainfer-qwen35 | `qwen3_5` | ❌ |
| `qwen4` | pegainfer-qwen4 | `qwen4_exp` | ❌ |
| `gemma4` | pegainfer-gemma4 | `gemma4`、`gemma4_unified` | ❌ |
| `deepseek-v2-lite` | pegainfer-deepseek-v2-lite | `deepseek_v2` | ❌ |
| `kimi-k2` | pegainfer-kimi-k2 | `kimi_k25`、`kimi_k2` | ❌ |
| `glm52` | pegainfer-glm52 | `glm_moe_dsa` | ❌ |
| `k3` | pegainfer-k3 | `kimi_k3` | ❌ |

（`text_model_types` 列此处略去，gemma4/qwen35/qwen4/k3/kimi-k2 还有 text_config 身份，详见 `families()` 源码。）

#### 4.3.5 小练习与答案

**练习 1**：`model_lines()` 里如果没有 `#[cfg(feature = ...)]` 守卫会发生什么？

**答案**：那么每条线都会成为**非可选**依赖（否则编译不过），任何构建都得拉齐 8 个模型 crate 及其构建期工具链（Triton/TileLang/额外 CUDA 源码）——「默认构建纯 Rust 无 Python」的性质就被破坏了。cfg 守卫与 optional 依赖是一对搭档。

**练习 2**：`families()` 的文档说自己是「a second source of truth by necessity」。既然是第二真相源，项目如何防止它与模型 crate 里的真实身份漂移？

**答案**：用测试钉住。qwen4 行有专门的绑定测试 `qwen4_hint_identity_matches_the_line_crate`（[src/main.rs:324-337](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L324-L337)，仅在 `qwen4` feature 开启时编译），把 `families()` 里的 `model_types`/`text_model_types` 与 `pegainfer_qwen4::MODEL_TYPE`/`TEXT_MODEL_TYPE` 常量逐一比对——改了任何一边，另一边不改就测试失败。

**练习 3**：一个用户拿着 `model_type: "qwen3"` 的**量化变体** checkpoint（probe 会拒绝的那种）在默认构建下启动，`feature_gate_hint` 会沉默吗？这是 bug 吗？

**答案**：会沉默——qwen3 已编译（`compiled == true`），提示只在「未编译」时触发；接下来 `registry.detect` 里已编译的 qwen3 线的 `probe` 会运行并给出它自己的拒绝理由（量化配置不支持等）。反过来，如果拿一个被编译掉的线的量化变体（如 glm52 的量化版）来跑，提示**会**触发，尽管 rebuild 后 probe 仍会拒绝——文档明说提示是 identity-only，「it is not a promise that the checkpoint is servable」。所以两种行为都是设计内的。

## 5. 综合实践

**任务：制作一份《pegainfer-server 构建形态核对报告》，把本讲三个模块串起来。**

1. **文档面**：从 [../README.md:142-156](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/README.md#L142-L156) 的受支持模型表抄下 7 条线及其 feature 名。
2. **代码面**：从 [Cargo.toml:34-48](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/Cargo.toml#L34-L48) 与 [src/main.rs:67-118](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L67-L118) 补齐第 8 条线（qwen4）。
3. **三方核对**：回答并记录三个差异点——
   - README 表（7 行）与 Cargo features（8 个）差在哪一行？
   - 8 个 feature 里，哪几个除了 `dep:` 之外还向模型 crate 转发了同名 feature？为什么 qwen3/qwen4 不需要？
   - 默认构建下 `families()` 的 8 个 `compiled` 字段各是什么？
4. **（可选，需 GPU 机器）实测**：`cargo run --release -- --help` 后，从帮助文本里挑出 3 个 flag，分别归类为「共享 flag」或「qwen3 独占 flag」，并在源码里找到它们的定义位置线索（共享 flag 在 `pegainfer-frontend/src/model_line.rs` 的 `SharedArgs`，独占 flag 在 `pegainfer-qwen3/src/model_line.rs`）。

**预期产出**：一页报告，能回答「这个二进制是怎么按 feature 裁剪的、裁掉之后检测提示靠什么工作」。如果全程无 GPU 环境，第 4 步标注「待本地验证」，前三步纯静态阅读即可完成。

## 6. 本讲小结

- PegaInfer 是纯 Rust + CUDA 的 LLM 推理引擎，对外提供 OpenAI 兼容 `/v1` API；设计原则是「共享基础设施，每个模型拥有自己的执行」。
- 架构分层：server（装配）→ frontend（协议/分词/模板/流式/引擎契约）→ 模型 crate（调度与执行）→ core/kernels（GPU 原语与 kernel）→ KV 基础设施；请求沿这条链路自上而下流动。
- pegainfer-server 是约 300 行的**纯分发**二进制：模块文档自述「every model-specific flag, rule, and option type lives in its model crate」，它只做检测、CLI 校验与服务路径选择。
- Cargo feature 矩阵决定二进制内容：`default = ["qwen3"]` 保证 stock 构建纯 Rust 无 Python；其余 7 条线经 `--features` 拉入，部分 feature 还要向模型 crate 转发以触发其内部门控实现。
- `model_lines()`（cfg 守卫的注册向量）与 `families()`（含 `compiled` 字段的身份表）是 feature 矩阵在代码里的映照；后者是「必要的第二真相源」，专为被编译掉的线提供 rebuild 提示。
- 观察到一处文档/代码错位：README 受支持模型表列 7 条线，代码里有 8 个 feature（`qwen4` 为实验线）——读源码时要习惯用代码核对文档。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：构建与运行——为什么 GPU 构建必须 `--release`、jemallocator 全局分配器、用 `cargo tree` 与 `--version` 验证 feature 构建假设。
- **随后（u1-l3）**：逐段精读 `main()` 从 CLI 到 serve 的完整启动主流程（本讲 4.3.2 的流程图会在那一讲展开到每一行）。
- **延伸阅读源码**（为第二、三单元做准备）：`../pegainfer-frontend/src/model_line.rs`（`ModelLine` trait 与注册表定义）——可以先只看 `trait ModelLine` 的方法签名，感受「server 只依赖这一个接缝」。
