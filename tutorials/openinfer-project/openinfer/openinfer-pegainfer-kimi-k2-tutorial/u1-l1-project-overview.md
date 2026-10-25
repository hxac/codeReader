# PegaInfer 与 Kimi-K2 crate 的定位

> 学习手册 · 单元 1 · 讲义 1（u1-l1）
> 项目：`openinfer-pegainfer-kimi-k2` · 基准 HEAD：`09a1a38f8b4938b06ee4e4f63f64fd4feed59434`

## 1. 本讲目标

读完本讲，你应该能够：

1. 说出 PegaInfer 是什么（纯 Rust + CUDA 的 LLM 推理引擎），以及它「每个模型一个 crate、共享内核与运行时」的组织方式。
2. 在 workspace 的成员清单里定位 `pegainfer-kimi-k2`，并说出它承担的模型线：Kimi-K2.6（MLA 注意力 + MoE + Marlin INT4 量化，8 卡专家并行）。
3. 逐条解释 `pegainfer-kimi-k2` 对 `pegainfer-frontend` / `pegainfer-core` / `pegainfer-kernels` / `pegainfer-kv-cache` / `pegainfer-sample` / `pegainfer-bench` 的依赖各自负责什么，并能用 `rg` 在源码里指出引用证据。
4. 对照 CLAUDE.md 的架构图，把 Kimi-K2 放进「HTTP 请求 → 前端 → 引擎契约 → 模型调度器 → TokenEvent」的链路中。

本讲是整本手册的第一篇：不涉及任何公式推导和 kernel 细节，只解决「我在哪、邻居是谁」这个定位问题。

## 2. 前置知识

本讲不需要你已经读过任何 PegaInfer 代码，但以下概念最好有个模糊印象（没有也行，下面每条都配了一句话解释）：

- **LLM 推理引擎**：接收文本请求、返回生成 token 的服务。一次生成分两个阶段——*prefill*（一次性吃掉整个 prompt，产出第一个 token 和 KV 缓存）与 *decode*（之后每步只算一个新 token）。**KV 缓存**就是把注意力中间结果存起来避免重算的显存区域。
- **MLA（Multi-head Latent Attention）**：DeepSeek 系与 Kimi 使用的注意力变体，把 KV 压缩到低维「潜在」向量再缓存，省显存。本手册第 5 单元会精读它，现在知道缩写即可。
- **MoE（Mixture of Experts）**：把一层 MLP 换成很多个「专家」小 MLP，由一个路由器每次只激活其中少数几个。Kimi-K2 有 384 个路由专家、每 token 激活 8 个。
- **Marlin INT4**：把权重压成 4-bit 整数（INT4）以省显存、提速度；Marlin 是一类为 GPU 优化的 INT4 GEMM（矩阵乘）kernel 家族。
- **TP / DP / EP**：三种多卡并行方式——TP（tensor parallel，把一个矩阵切开分给多卡）、DP（data parallel，不同卡服务不同请求）、EP（expert parallel，把 MoE 的不同专家分给不同卡）。Kimi-K2 两种拓扑都要用：TP8/DP1 和 TP1/DP8（后者的 EP 都是 8）。
- **Cargo workspace 与 feature**：一个仓库里多个 crate 共享一份锁文件和构建配置；`feature` 是 Cargo 的编译开关，可以按需把某部分代码编进或排除出构建。

另外两个仓库层面的事实：

- 本仓库必须用 **`--release`** 构建，debug 构建对 GPU/CUDA 来说慢到会超时（CLAUDE.md 明确强调）。
- `pegainfer-kimi-k2` 的 `src/lib.rs` 顶部有 `#![feature(generic_const_exprs)]`，说明它用到 Rust 的实验特性，依赖仓库 `rust-toolchain.toml` 钉住的工具链。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| `CLAUDE.md`（仓库根） | 给编码代理的仓库指南：模型表、构建命令、架构图 | 定位 Kimi-K2 的架构特征与整仓分工 |
| `README.md`（仓库根） | 面向用户的项目介绍：快速开始、模型支持表、架构分层表 | 理解「共享基础设施、模型自持执行」的哲学 |
| `Cargo.toml`（仓库根） | 虚拟 workspace 清单：21 个成员 crate 与共享依赖版本 | 数清邻居、找到 `pegainfer-kimi-k2` 的位置 |
| `pegainfer-kimi-k2/Cargo.toml` | 本 crate 的依赖与 feature 定义 | 本讲核心证据：依赖清单 = 分工清单 |
| `pegainfer-kimi-k2/src/lib.rs` | crate 门面：模块声明、`start_engine` 入口、`KimiLaunchOptions` | 看 feature 门住哪些模块、入口长什么样 |

补充引用（用来给依赖表找「use 证据」）：`src/runner/scheduler.rs`、`src/typed_scratch.rs`、`src/runner/worker.rs`、`src/runner/worker/state.rs`。这些文件的内部机制分别属于后续讲义，本讲只引用它们头部的 `use` 行。

## 4. 核心概念与源码讲解

### 4.1 workspace 布局与模型 crate 分工

#### 4.1.1 概念说明

PegaInfer 的自我定位写在 CLAUDE.md 第一段：**纯 Rust + CUDA 的 LLM 推理引擎，没有 PyTorch、没有框架，提供 OpenAI 兼容的 `/v1/completions` API**。

它把代码组织成一个 Cargo workspace：**每个模型线一个独立 crate**（qwen3、qwen35、qwen4、kimi-k2、glm52……），而所有模型共用的东西——GPU 张量抽象、CUDA kernel、采样、KV 缓存分页原语、HTTP 前端——放在共享 crate 里。README 把这条设计原则浓缩成一句话：

> Share the infrastructure; let each model own its execution.（共享基础设施；让每个模型拥有自己的执行。）

为什么这样分？因为不同模型的差异极大（Kimi-K2 是 MLA + MoE + INT4，Qwen3 是普通全注意力 dense 模型），硬塞进一个「通用引擎」会让抽象四处漏水；而 CUDA 绑定、safetensors 读取、分页 KV 这些又几乎人人一样，没必要每个模型抄一遍。于是「模型拥有调度与执行，基础设施共享」就成了自然的切分线。

#### 4.1.2 核心流程

一次请求在 PegaInfer 里的路径（这是理解所有后续讲义的主干图）：

```
HTTP 请求 → vLLM 前端 → EngineHandle（引擎契约）→ 每模型自己的调度器/执行器 → TokenEvent
```

- 前端把 OpenAI 协议翻译成 `GenerateRequest`，通过 `EngineHandle` 交给某个模型 crate。
- 模型 crate 内部的调度器决定怎么拼 batch、何时 prefill、何时 decode，并在 GPU 上执行 kernel。
- 生成的 token 以 `TokenEvent` 流回前端。
- 所有模型 crate 底下踩着同一层：`pegainfer-core` 运行时 + `pegainfer-kernels`（CUDA/cuBLAS、Triton AOT、FlashInfer）。

#### 4.1.3 源码精读

先看 workspace 清单。根 `Cargo.toml` 是一个虚拟清单（没有 `[package]` 段），`members` 列出全部成员：

[Cargo.toml:L2-L26](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/Cargo.toml#L2-L26) —— 定义 workspace：`default-members = ["pegainfer-server"]` 表示裸 `cargo run`/`cargo build` 只构建服务器；`pegainfer-kimi-k2` 出现在第 16 行的成员列表里。

```toml
default-members = ["pegainfer-server"]
members = [
  "pegainfer-frontend",
  "pegainfer-sim",
  "pegainfer-server",
  "pegainfer-core",
  ...
  "pegainfer-kimi-k2",   # ← 我们的主角，在成员清单第 14 个条目
  ...
]
```

数一下这份清单，workspace 共有 21 个成员，其中 **8 个是模型 crate**：`pegainfer-qwen3`、`pegainfer-qwen35`、`pegainfer-qwen4`、`pegainfer-deepseek-v2-lite`、`pegainfer-gemma4`、`pegainfer-kimi-k2`、`pegainfer-glm52`、`pegainfer-k3`。其余成员是共享设施：`pegainfer-frontend`（协议与契约）、`pegainfer-core`（运行时原语）、`pegainfer-kernels`（kernel 与 FFI）、`pegainfer-sample`（批量采样）、`pegainfer-kv-cache` / `pegainfer-kv-offload` / `pegainfer-kv-store`（KV 三件套）、`pegainfer-bench`（基准计时）、`pegainfer-build` / `pegainfer-cupti` / `pegainfer-sim` / `pegainfer-server`，以及一个 Dynamo 衍生的 `kvbm/kvbm-logical`。

workspace 同时统一管理路径依赖，任何成员引用兄弟 crate 都走这里：

[Cargo.toml:L131](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/Cargo.toml#L131) —— 声明 `pegainfer-kimi-k2 = { path = "pegainfer-kimi-k2" }`，其他 crate（例如服务器）可以用名字直接依赖它。

分工的权威表述在 README 的架构分层表里：

[README.md:L173-L181](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/README.md#L173-L181) —— 逐行定义每条边界的职责：

| 边界 | 职责（README 原文翻译） |
| --- | --- |
| `pegainfer-server` | 检测模型、校验其 CLI、启动选中的引擎 |
| `pegainfer-frontend` | OpenAI 协议、分词、chat 模板、流式、指标，以及引擎契约 |
| **各模型 crate** | **权重、调度器、prefill/decode 执行、状态布局、并行策略** |
| `pegainfer-core` / `pegainfer-sample` | 共享 GPU 与权重加载原语；批量 token 选择 |
| `pegainfer-kernels` | 原生 kernel、FFI、GPU 库集成、feature 门控的 AOT 构建 |
| KV 基础设施 | `pegainfer-kv-store` 与既有 `pegainfer-kv-cache` / `pegainfer-kv-offload` |

注意中间那一行——**「各模型 crate」拥有权重、调度器、执行、状态和并行策略**。这就是本手册后面 6 个单元要在 `pegainfer-kimi-k2` 里逐层拆开的东西：单元 2 拆权重、单元 3 拆并行拓扑、单元 4 拆调度器、单元 5-6 拆前向计算与 MoE、单元 7 拆优化与验证。

[README.md:L31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/README.md#L31) —— 项目的开门见山句：「PegaInfer 通过 OpenAI 兼容 API 服务 LLM。每个模型拥有自己的调度器、状态和 kernel；服务与 KV 基础设施共享。没有 PyTorch 或 Python 运行时。」

[README.md:L68](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/README.md#L68) —— 「服务器入口是 `pegainfer-server`；模型 crate 包含模型实现与诊断工具。」这解释了为什么 `default-members` 只有 server：服务器按 feature 把模型 crate 链接进来。

#### 4.1.4 代码实践：用 cargo tree 看邻居

1. **实践目标**：不靠记忆，用 Cargo 自己列出 `pegainfer-kimi-k2` 的直接依赖，验证 4.2 节的分工表。
2. **操作步骤**：在仓库根目录执行：
   ```bash
   cargo tree -p pegainfer-kimi-k2 --depth 1
   ```
   `--depth 1` 只显示直接依赖，输出最干净。
3. **需要观察的现象**：输出第一层应出现 `pegainfer-bench`、`pegainfer-core`、`pegainfer-frontend`、`pegainfer-kernels`、`pegainfer-kv-cache`、`pegainfer-sample` 六个兄弟 crate，以及 `cudarc`、`safetensors`、`memmap2`、`tokio` 等第三方库——与 `pegainfer-kimi-k2/Cargo.toml` 的 `[dependencies]` 段一一对应。传依赖的具体层级取决于锁文件状态，以本地输出为准（待本地验证）。
4. **预期结果**：你得到的直接依赖集合，恰好等于 4.2.3 节表格的行集合；任何多出来的条目都说明你忘了 `--depth 1`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `default-members` 是 `pegainfer-server` 而不是整个 workspace？
**答案**：裸 `cargo run`/`cargo build` 只需要构建「服务器 + 默认模型」。各模型线在 feature 后面，按需编译（Kimi-K2 要 `--features kimi-k2`，还会触发额外的 CUDA 编译），把它从默认构建里摘出去能保住默认构建的编译时间。

**练习 2**：workspace 里共有多少个成员？其中模型 crate 有几个？分别是哪些？
**答案**：21 个成员；8 个模型 crate：`pegainfer-qwen3`、`pegainfer-qwen35`、`pegainfer-qwen4`、`pegainfer-deepseek-v2-lite`、`pegainfer-gemma4`、`pegainfer-kimi-k2`、`pegainfer-glm52`、`pegainfer-k3`（见 [Cargo.toml:L3-L26](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/Cargo.toml#L3-L26)）。

**练习 3**：README 说「各模型 crate」负责哪五件事？
**答案**：权重、调度器、prefill/decode 执行、状态布局、并行策略（[README.md:L177](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/README.md#L177)）。

### 4.2 kimi-k2 的依赖清单

#### 4.2.1 概念说明

在 Rust 里，一个 crate 的 `Cargo.toml` 依赖段就是它和世界的全部接口——**读依赖清单等于读分工合同**。`pegainfer-kimi-k2` 的清单很短（19 个直接依赖），但每一行都有明确角色。

更重要的是 feature（编译开关）。这个 crate 的 `default` feature 是**空的**：不开 feature 时它几乎是空壳，`start_engine` 直接报错退出；开 `kimi-k2` 才把 `runner`、`weights`、`typed_scratch`、`model_line` 这些模块和对应的 CUDA kernel 编进来。这样做的好处：别人在 workspace 里做无关构建时，不必为 Kimi-K2 付出 CUDA 编译成本。

#### 4.2.2 核心流程

feature 之间的依赖链（全部可在 `Cargo.toml` 里核对）：

```
kimi-k2          → pegainfer-kernels/kimi-k2     （把 MLA/MoE/Marlin CUDA 源编进 kernels crate）
kernel-call-trace → pegainfer-core/kernel-call-trace（给 kernel 调用加追踪）
kernel-report    → kimi-k2 + kernel-call-trace    （观测工具的超集，供两个报告二进制用）
default          → （空）
```

构建时的传导路径：`cargo run --release --features kimi-k2` 会在**本 crate**打开 `kimi-k2` 模块树的同时，向 `pegainfer-kernels` 传递同名 feature——kernel 的编译归 `pegainfer-kernels/build.rs` 管（用 nvcc 编 `csrc/*.cu`，`kimi-k2` 会额外加入 MLA/MoE/Marlin CUDA 源，见 CLAUDE.md 的 Build system 一节）。也就是说：**模型逻辑在模型 crate，kernel 编译在 kernels crate，feature 是两者之间的接线**。

#### 4.2.3 源码精读

依赖段全文只有 19 行，先看骨架：

[pegainfer-kimi-k2/Cargo.toml:L10-L29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L10-L29) —— 全部直接依赖；六个 `pegainfer-*` 兄弟 crate 在 L19-L24 连续排列，第三方库（`cudarc`、`safetensors`、`memmap2`、`tokio` 等）穿插其间。

六个兄弟依赖各自承担的职责（右列的「use 证据」都是当前 HEAD 下真实存在的引用行，4.2.4 实践会让你亲手找到它们）：

| 依赖 | 分工 | use 证据（pegainfer-kimi-k2/src 内） |
| --- | --- | --- |
| `pegainfer-frontend` | 引擎请求/事件契约与协议栈：`EngineHandle`、`EngineLoadOptions`、`GenerateRequest`、`TokenEvent`、`SamplingParams` | [lib.rs:L17-L20](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L17-L20)、[runner/scheduler.rs:L15-L19](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/scheduler.rs#L15-L19) |
| `pegainfer-kernels` | GPU 张量与 kernel FFI：`gpu_buffers` 宏、`ops` 里的模型常量与 Marlin workspace、`tensor::{DeviceContext, GpuTensor, HiddenStates}` | [typed_scratch.rs:L7-L17](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/typed_scratch.rs#L7-L17) |
| `pegainfer-kv-cache` | 共享分页 KV 原语：逻辑页池 `BlockPool`、每请求 KV 租约 `RequestKv` | [runner/scheduler.rs:L20-L21](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/scheduler.rs#L20-L21)、[runner/bringup.rs:L21](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L21) |
| `pegainfer-core` | 共享运行时原语：CUDA Graph 状态、CPU/NUMA 拓扑、EOS 解析、kernel 调用追踪 | [runner/worker.rs:L23](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L23)、[runner/affinity.rs:L6-L13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/affinity.rs#L6-L13) |
| `pegainfer-sample` | 批量 token 选择（采样）：`BatchSamplingScratch`、`gpu_sample_batch_into` | [typed_scratch.rs:L169](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/typed_scratch.rs#L169)、[runner/worker/state.rs:L375](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/state.rs#L375) |
| `pegainfer-bench` | 计时/统计框架，服务本 crate 的 kernel-report 观测工具（第 7 单元展开） | 声明于 [Cargo.toml:L19](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L19)，消费方在 `kernel-report` feature 之后 |

第三方依赖也各有来历：`cudarc` 是 Rust 的 CUDA 绑定（这里带 `nccl` feature，多卡集合通信要用）；`safetensors` + `memmap2` 支撑「mmap 权重分片再拷入显存」的加载路径（第 2 单元精读）；`clap` 给 CLI 用；`crossbeam-channel` 支撑每 rank 一个线程的命令模型（第 3 单元）；`half` 是 bf16 浮点类型。

接着是 feature 定义：

[pegainfer-kimi-k2/Cargo.toml:L31-L35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L31-L35) —— 定义四个 feature：`default = []`（空默认）、`kimi-k2`（向 kernels 传导）、`kernel-call-trace`（向 core 传导）、`kernel-report`（= `kimi-k2` + `kernel-call-trace`）。

```toml
[features]
default = []
kernel-call-trace = ["pegainfer-core/kernel-call-trace"]
kernel-report = ["kimi-k2", "kernel-call-trace"]
kimi-k2 = ["pegainfer-kernels/kimi-k2"]
```

测试与工具二进制也被 feature 管着：

[pegainfer-kimi-k2/Cargo.toml:L37-L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L37-L50) —— `vllm_golden_gate` 精度门禁测试需要 `kimi-k2` feature；`kimi_kernel_report` / `kimi_model_report` 两个二进制需要 `kernel-report`。不开 feature 时 `cargo build -p pegainfer-kimi-k2` 连它们都不会编。

feature 门在源码里的样子，看 crate 门面 `lib.rs`：

[pegainfer-kimi-k2/src/lib.rs:L1-L4](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L1-L4) —— crate 级文档注释自述：「Text-only Kimi-K2.6 model crate」，并说明当前阶段持有编译期校验过的算子 API 面与 text-only 配置探针，CUDA/运行时本体落在这些头之后（也就是 `runner/`、`weights/` 子树）。

[pegainfer-kimi-k2/src/lib.rs:L22-L34](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L22-L34) —— 模块声明与各自的 feature 门：`config` 无门（任何时候都可校验配置），`batch_decode_trace`、`model_line`、`runner`、`typed_scratch`、`weights` 都在 `kimi-k2` 之后，`kernel_report` 在 `kernel-report` 之后。

最直观的「空壳 vs 本体」对比是入口函数的一对实现：

[pegainfer-kimi-k2/src/lib.rs:L38-L47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L38-L47) —— 开 `kimi-k2` 时 `start_engine` 转发到 `runner::start_engine`（真正的引擎启动，第 3 单元精读）；**不开时是同名的桩函数，直接 `anyhow::bail!` 报「Kimi-K2 runtime is feature-gated; rebuild with --features kimi-k2」**。这就是「默认构建纯 Rust、模型按需进入」在 API 层的落点。

#### 4.2.4 代码实践：亲手找出三处 use 引用

1. **实践目标**：用文本搜索验证上表的三条「use 证据」，把抽象分工落到具体行号。
2. **操作步骤**：在仓库根目录运行：
   ```bash
   rg -n "use pegainfer_frontend::engine" pegainfer-kimi-k2/src | head -3
   rg -n "pegainfer_kv_cache::"          pegainfer-kimi-k2/src | head -5
   rg -n "^use pegainfer_kernels::"      pegainfer-kimi-k2/src | head -5
   ```
3. **需要观察的现象**：三条命令分别应命中 `src/lib.rs:17` 附近、`src/runner/scheduler.rs:20-21` 与 `src/runner/bringup.rs:21`、`src/typed_scratch.rs:7-17`（这些行在当前 HEAD 已核对；行号若因代码演进而漂移，以文件内匹配内容为准）。
4. **预期结果**：每个 crate 至少找到一处真实引用；对照 4.2.3 的表格，为每处引用写一行「这个 use 在借什么能力」。
5. 若 `rg` 不在环境中，可用 `grep -rn` 替代，效果相同。

#### 4.2.5 小练习与答案

**练习 1**：不开任何 feature 直接调用 `pegainfer_kimi_k2::start_engine` 会发生什么？
**答案**：返回 `Err`，错误信息为「Kimi-K2 runtime is feature-gated; rebuild with --features kimi-k2」（[lib.rs:L44-L47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L44-L47) 的桩实现）。

**练习 2**：`kimi-k2` feature 打开后，除本 crate 的模块外还改变了什么？
**答案**：它向 `pegainfer-kernels` 传递同名 feature（[Cargo.toml:L35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L35)），让 kernels 的构建把 Kimi 的 MLA/MoE/Marlin CUDA 源编进产物。

**练习 3**：`kernel-report` 和 `kimi-k2` 是什么关系？想跑 `kimi_model_report` 二进制该用什么命令？
**答案**：`kernel-report = ["kimi-k2", "kernel-call-trace"]`，是超集。命令形如 `cargo run --release -p pegainfer-kimi-k2 --features kernel-report --bin kimi_model_report`（该 bin 的 `required-features` 见 [Cargo.toml:L47-L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L47-L50)；实际运行需要 GPU 与权重，属第 7 单元内容）。

### 4.3 架构图中的位置

#### 4.3.1 概念说明

CLAUDE.md 用一张 ASCII 架构图给出全仓库的「公交路线图」，Kimi-K2 是其中一个站点。它在模型表里的登记信息是：**MLA + MoE + Marlin INT4，8-GPU EP（专家并行）**。三个词分别对应本手册的三个技术主题：

- **MLA** → 第 5 单元（潜在注意力：KV 压缩、q_nope 吸收、FlashInfer 解码）；
- **MoE** → 第 6 单元（noaux_tc 路由器、NCCL 与 DeepEP 两种后端）;
- **Marlin INT4** → 第 2 单元（权重从 checkpoint 的 4-bit 布局重排成 Marlin kernel 要的显存布局）。

还有一个只属于 Kimi-K2 的关键点：它支持**两种 8 卡拓扑**——TP8/DP1（8 卡张量并行，MoE 走 NCCL）与 TP1/DP8（每卡独立服务请求，MoE 走 DeepEP）。这解释了为什么本 crate 的运行时既有 `runner/moe_nccl.rs` 又有 `runner/moe_deepep.rs`。本讲只需要记住「双拓扑」这个词，细节在单元 3 和 6。

#### 4.3.2 核心流程

把 CLAUDE.md 的架构图重绘一遍，Kimi-K2 加粗标出（与原图同构，只保留本讲需要的细节）：

```
HTTP 请求 → vLLM 前端 → EngineHandle → 每模型调度器/执行器 → TokenEvent
                                          │
           ┌─────────┬─────────┬─────────┼─────────┬─────────┐
           │         │         │         │         │         │
         qwen3     qwen35   dsv2-lite 【kimi-k2】  glm52      …
       (全注意力) (线性+全)  (MoE+EP)  (MLA+MoE) (MLA+MoE+FP8)
           │         │         │         │         │         │
           └─────────┴─────────┴─────────┼─────────┴─────────┘
                                        │
                 pegainfer-core 运行时 + pegainfer-kernels
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
              CUDA / cuBLAS        Triton AOT         FlashInfer
                                              (采样、注意力、norm、MLA decode)
```

读图三步：

1. **上层**（前端 + 契约）：所有模型共用，Kimi-K2 只实现契约，不改协议。
2. **中层**（模型 crate）：Kimi-K2 与 qwen3 等并列，各自拥有调度与执行——这一层就是 `pegainfer-kimi-k2` 的全部领土。
3. **下层**（core + kernels → CUDA 生态）：模型 crate 通过依赖「踩」在这一层上（对应 4.2 的依赖表）。

#### 4.3.3 源码精读

模型登记表：

[CLAUDE.md:L11-L19](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md#L11-L19) —— 每个模型线一行：crate 名、feature 名、架构特征。Kimi-K2 行在 L17：`MLA + MoE + Marlin INT4, 8-GPU EP`，feature 是 `kimi-k2`（非默认，与 4.2 的 feature 分析互证）。

原始架构图：

[CLAUDE.md:L80-L98](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md#L80-L98) —— 上面那张重绘图的出处：请求链路（L81）、模型并列层（L86-L87）、共享的 core+kernels 层（L91）、底部的 CUDA/cuBLAS、Triton AOT、FlashInfer（L93-L97）。

配套的关键抽象说明：

[CLAUDE.md:L100-L107](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md#L100-L107) —— 逐条定义 `pegainfer-frontend`（契约 + `ModelLine` 调度 trait）、每模型 crate、`pegainfer-core::ops`、`pegainfer-kernels`（模型专属 kernel 在 feature 门后的子模块里，`kimi_k2` 是其一）、CUDA Graph、KV 状态归属。

进入 crate 自己的入口，看它如何把自己挂进这张图。`lib.rs` 的 `launch` 是服务器视角的启动函数：

[pegainfer-kimi-k2/src/lib.rs:L49-L60](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L49-L60) —— `KimiLaunchOptions` 的文档注释说得很清楚：二进制只传原始 CLI 值，`launch` 拥有 EP 拓扑策略——校验 TP/DP、推导 EP world 与设备序号——「服务器永远不硬编码 Kimi 的并行布局」。

[pegainfer-kimi-k2/src/lib.rs:L62-L90](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L62-L90) —— `launch` 的实现骨架：校验 tp/dp 为正 → `ParallelConfig::new(tp, dp)` → 组装 `EngineLoadOptions`，其中 `device_ordinals: (0..parallel.ep_world()).collect()` 决定占哪些 GPU，最后转发 `runner::start_engine`。摘关键几行：

```rust
let parallel = ParallelConfig::new(options.tp_size, options.dp_size);
info!("Kimi-K2 EP options: tp_size={}, dp_size={}, ep_world={}, ep_backend={:?}", ...);
runner::start_engine(model_path, &EngineLoadOptions {
    enable_cuda_graph: options.cuda_graph,
    device_ordinals: (0..parallel.ep_world()).collect(),
    parallel_config: Some(parallel),
    ep_backend: options.ep_backend,
    seed: 42,
})
```

注意这里出现的类型全部来自 `pegainfer_frontend`（`EngineLoadOptions`、`EpBackend`、`ParallelConfig`）——模型 crate 实现「自己的执行」，但表达「怎么并行」的类型是共享契约的一部分。这正是 4.1 分工哲学在类型层面的样子。

最后，README 的模型支持表从用户视角再确认一次：

[README.md:L152](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/README.md#L152) —— 「Kimi-K2 / K2.5 · MLA + MoE，Marlin INT4 · feature `kimi-k2` · 8-GPU expert parallelism」，并链接到模型路线图 `docs/models/kimi-k2/roadmap.md`。

顺带解释一个命名细节：crate 门面自述为「Kimi-K2.6」（lib.rs 文档注释），README 表格写「Kimi-K2 / K2.5」——这是同一模型线的不同代次共享一个 crate；具体几何差异由第 2 讲的配置探针处理，本讲不必纠结版本号。

#### 4.3.4 代码实践：把请求路径画成自己的图

1. **实践目标**：脱离讲义，凭 CLAUDE.md 原图和 `lib.rs` 入口，亲手画一张「Kimi-K2 视角」的请求路径图。
2. **操作步骤**：
   1. 打开 [CLAUDE.md:L80-L98](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md#L80-L98) 的原图；
   2. 打开 [pegainfer-kimi-k2/src/lib.rs:L38-L47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L38-L47) 和 [L62-L90](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L62-L90)；
   3. 在纸上（或任意画图工具）画出：请求从 HTTP 进入后，经过哪些共享层、在哪里进入 `pegainfer-kimi-k2`、`start_engine`→`launch`→`runner::start_engine` 这条入口链在哪里汇入。
3. **需要观察的现象**：画的过程中你会发现「前端 → EngineHandle → 模型」这三段全部来自 `pegainfer_frontend::engine`，而「launch 的拓扑决策」是 Kimi 自己的。
4. **预期结果**：一张与 4.3.2 重绘图同构、但标注了具体类型名（`EngineHandle`、`GenerateRequest`、`TokenEvent`、`EngineLoadOptions`、`ParallelConfig`）的路径图。这张图将是你阅读单元 3（启动流程）的底稿。

#### 4.3.5 小练习与答案

**练习 1**：CLAUDE.md 模型表里 Kimi-K2 行的三个架构标签是什么？分别对应手册哪个单元？
**答案**：MLA（单元 5）、MoE（单元 6）、Marlin INT4（单元 2）；外加 8-GPU EP（单元 3 的双拓扑）。出处 [CLAUDE.md:L17](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md#L17)。

**练习 2**：请求链路的终点事件类型叫什么？在哪个共享 crate 定义、在本 crate 哪个文件被使用？
**答案**：`TokenEvent`；定义于 `pegainfer-frontend`（契约的一部分），本 crate 在 [runner/scheduler.rs:L18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/scheduler.rs#L18) 等处使用。

**练习 3**：`launch` 函数为什么强调「服务器不硬编码 Kimi 的并行布局」？
**答案**：因为并行布局是模型 crate 的私有策略——由 `launch` 校验 tp/dp、经 `ParallelConfig` 推导 `ep_world` 与 `device_ordinals`（[lib.rs:L68-L88](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L68-L88)）；服务器只递 CLI 原始值，这样换模型线时无需改服务器代码。

## 5. 综合实践

把本讲三个模块串成一个任务——**写一份 20 行以内的《crate 分工对照笔记》**：

1. **在仓库根目录运行**：
   ```bash
   cargo tree -p pegainfer-kimi-k2 --depth 1
   ```
   抄下六个 `pegainfer-*` 直接依赖（对照 4.1.4）。
2. **为三个关键邻居找 use 证据**（对照 4.2.4）：
   ```bash
   rg -n "use pegainfer_frontend::engine" pegainfer-kimi-k2/src | head -3
   rg -n "pegainfer_kv_cache::"          pegainfer-kimi-k2/src | head -5
   rg -n "^use pegainfer_kernels::"      pegainfer-kimi-k2/src | head -5
   ```
3. **写笔记**，每行一个条目，格式自定，例如：
   - `pegainfer-frontend` → lib.rs:17 借 `EngineHandle`/`EngineLoadOptions`：引擎契约，模型 crate 对外的唯一接口形状。
   - `pegainfer-kv-cache` → scheduler.rs:20 借 `BlockPool`/`RequestKv`：共享分页 KV，模型只管调度不管页池实现。
   - `pegainfer-kernels` → typed_scratch.rs:7 借 `gpu_buffers`/`ops`/`tensor`：GPU 张量与 Kimi 专属 kernel 的 FFI 面。
   - （`pegainfer-core`、`pegainfer-sample`、`pegainfer-bench` 各一行，依据 4.2.3 表格补全。）
4. **验收标准**：笔记里每个行号都能在本地 `rg` 复现；六个兄弟 crate 各有一行且不重复。cargo tree 的传依赖输出与锁文件有关，如与预期不符，以 `[dependencies]` 段为准（[Cargo.toml:L10-L29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/Cargo.toml#L10-L29)）。

这份笔记建议保留：第 3 讲（模块地图）、第 7 讲（kernel-report 工具链）都会回看它。

## 6. 本讲小结

- PegaInfer 是纯 Rust + CUDA 的 LLM 推理引擎（无 PyTorch、无 Python 运行时），组织原则是「**共享基础设施，模型自持执行**」。
- workspace 共 21 个成员，其中 8 个模型 crate；`pegainfer-kimi-k2` 承担 Kimi-K2.6 模型线：MLA + MoE + Marlin INT4、8-GPU EP、双拓扑（TP8/DP1 走 NCCL，TP1/DP8 走 DeepEP）。
- 依赖清单即分工合同：`frontend`（契约）、`kernels`（GPU 张量与 kernel FFI）、`kv-cache`（分页 KV 原语）、`core`（CUDA Graph/CPU 拓扑等运行时）、`sample`（批量采样）、`bench`（计时统计）。
- feature 体系：`default = []` 空默认；`kimi-k2` 门住运行时本体并向 `pegainfer-kernels` 传导 CUDA 编译；`kernel-report` 是观测超集；不开 feature 时 `start_engine` 是一个会报错的桩。
- 架构分三层看：前端契约层（共享）→ 模型 crate 层（Kimi 的领土）→ core+kernels → CUDA 生态层；请求链路 `HTTP → EngineHandle → 模型调度器 → TokenEvent`。

## 7. 下一步学习建议

按本手册的依赖顺序，接下来三讲同属单元 1（入门）：

1. **u1-l2 模型几何与配置探针**：读 `src/config.rs` 的常量表与 `probe_config_json`——这是唯一不依赖 feature、现在就能读能测的模块，也是理解一切张量形状的钥匙。
2. **u1-l3 crate 模块地图**：把 `runner/`、`weights/`、`typed_scratch` 等子树逐个定位，弄清 `foo.rs + foo/` 的扁平模块风格。
3. **u1-l4 构建与运行**：真正跑一次 `cargo run --release --features kimi-k2 -- --model-path models/Kimi-K2`，理解 `ModelLine` 的 probe → CLI → launch 流程（需要 8 卡 GPU 环境）。

补充阅读（项目内文档，建议先扫 TL;DR）：

- [docs/models/kimi-k2/roadmap.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/kimi-k2/roadmap.md) —— Kimi-K2 模型线的路线图，README 模型表直接链接它。
- [CLAUDE.md:L109-L111](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md#L109-L111) —— 构建系统说明：`pegainfer-kernels/build.rs` 如何用 nvcc 编 CUDA、feature 如何驱动额外代码生成。
