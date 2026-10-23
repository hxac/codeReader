# 项目定位与架构全景：pegainfer-core 是什么

> 讲义编号 u1-l1 ｜ 难度 入门 ｜ 前置讲义 无
> 本讲所有永久链接均指向提交 `223206b7ddbc7441063741484d9aa610d5ed9bb0`（当前 main 的 HEAD）。

## 1. 本讲目标

学完本讲，你应当能够：

1. **说清位置**：pegainfer-core 在 PegaInfer（纯 Rust + CUDA 的 LLM 推理引擎，无 PyTorch）中处于哪一层、为谁服务、向下依赖谁。
2. **列全模块**：合上资料也能写出 `lib.rs` 暴露的全部 **13 个 `pub mod`**，并按职责粗分类。
3. **划清边界**：区分 pegainfer-core 与 pegainfer-kernels、pegainfer-frontend、pegainfer-kv-cache 各自的分工。特别是两条硬边界——**HTTP 协议不在 core（在 frontend）**、**CUDA 内核编译不在 core（在 kernels 的 build.rs）**——并能逐条给出源码证据。

本讲刻意不进入任何模块的内部实现，只建立"地图感"；每个模块的深读留给后续讲义。

## 2. 前置知识

### 2.1 LLM 推理引擎在做什么

把一个训练好的大模型（一堆权重文件）加载进 GPU 显存，然后对外提供"给一段文本、逐个 token 吐回答"的服务。推理分两个阶段：

- **prefill（预填充）**：一次性处理用户输入的全部 token，产出第一份 KV 缓存和第一个输出 token；
- **decode（解码）**：之后每步只算一个新 token，直到命中停止条件（EOS）。

**KV cache** 是注意力计算中间结果的缓存：有了它，每生成一个新 token 不必重算整段前文。管理 KV cache 的显存设施叫 **KV 池**，它通常按"页"来分配（像内存页一样把显存切成固定大小的块）。

### 2.2 Rust 的 crate 与 workspace

- **crate**：Rust 的独立编译单元，可以理解为一个"库（或可执行程序）项目"。每个 crate 有自己的 `Cargo.toml`（声明名字与依赖）和 `src/`（源码）；库 crate 的根文件是 `src/lib.rs`。
- **workspace**：多个 crate 共享一份顶层 `Cargo.toml`、统一管理依赖版本的仓库组织方式。PegaInfer 就是一个多 crate workspace：模型 crate、core、kernels、frontend……各是一个 crate。
- **`pub mod x;`**：在 `lib.rs` 里声明"存在一个公开模块 x"，对应 `src/x.rs` 文件或 `src/x/` 目录。
- **再出口（re-export）**：`pub use some_crate::thing;` 把别处的条目通过当前路径再次导出。调用方只需依赖你，不必直接依赖 some_crate。

### 2.3 CUDA、kernel 与算子库

- **CUDA**：NVIDIA GPU 的并行计算平台。**kernel** 是跑在 GPU 上、被成千上万线程同时执行的函数。
- **cuBLAS**：NVIDIA 官方矩阵运算库（GEMM 等）。
- **FlashInfer**：高性能注意力/采样等推理算子库。
- **CUDA Graph**：把一串 GPU 调用录制成一张"图"，之后整图一键重放，省掉逐步启动的 CPU 开销——decode 循环每步执行相同的算子序列，特别适合。
- **nvcc**：CUDA kernel 的编译器，用来编译 `.cu` 文件。

### 2.4 术语速查表

| 术语 | 一句话解释 |
|---|---|
| crate / workspace / `pub mod` / 再出口 | 见 2.2 |
| safetensors | Hugging Face 推广的张量序列化格式，模型权重一般存成 `.safetensors` 文件 |
| KV cache / KV 池 / 页 | 见 2.1 |
| RAII | 资源随值存在、值销毁时自动归还的编程纪律（Rust 的 `Drop` trait） |
| 门面（façade） | 自己不实现，只把别处的实现以统一路径转卖出去的模块 |
| prefill / decode / EOS | 见 2.1；EOS = end-of-sequence，停止 token |

## 3. 本讲源码地图

| 文件 | 体量 | 在本讲中的角色 |
|---|---|---|
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs) | 15 行 | **主角**：一句 crate 自述 + 13 个模块声明，仅此而已 |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml) | 39 行 | 依赖证据：core 依赖什么、不依赖什么 |
| [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md)（仓库根） | — | 仓库级架构说明：架构图、模型表、构建体系 |
| [src/ops.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs) | 115 行 | "封装 + 再出口"风格的样本（只看头部声明区） |
| [src/page_pool.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs) / [src/kv_pool.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs) | 194 / 563 行 | 文件头 doc 注释自述职责的样本 |
| [src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tensor.rs) / [src/ffi.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ffi.rs) | 各 1 行 | 两个"一行门面"，理解 core 定位的钥匙 |
| [src/tracing.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs) | — | L99：core 对 frontend 的唯一直接引用 |
| 其余模块文件头（cuda_graph / weight_loader / rope / decodable_vocab / eos / cpu_topology / logging） | — | 各取文件头几行，佐证 4.2 的 13 模块职责表 |

本讲的取证手段只有只读命令：`cat -n`、`grep -n`、`ls`、`wc -l`。文中凡标**待确认**的，都是本讲没有核验到底、明确留给后续讲义的点。

## 4. 核心概念与源码讲解

### 4.1 crate 定位：一句自述——"Shared runtime API"

#### 4.1.1 概念说明

PegaInfer 整体是一个**纯 Rust + CUDA 的 LLM 推理引擎**：不依赖 PyTorch、不依赖 Python 框架（默认 feature 下构建全程无 Python），对外提供 OpenAI 兼容的 `/v1/completions` API。它支持多条模型线（Qwen3、Qwen3.5、DeepSeek-V2-Lite、Gemma 4、Kimi-K2、GLM5.2、Kimi-K3），**每条模型线一个独立的模型 crate**，按 cargo feature 开关。

这马上带来一个工程问题：各条模型线都要"分配 GPU 张量、调用算子、按页管理 KV 缓存、从磁盘加载权重、生成 RoPE 表、判定 EOS"——这些能力与具体模型架构无关。把它们抽到一处共享，就是 **pegainfer-core**。它的自我定位写在 `lib.rs` 第一行：**"Shared runtime API used by pegainfer model crates"**（供 pegainfer 各模型 crate 使用的共享运行时 API）。

注意这一定位的两半：

- **Shared（共享）**：一个 core 服务所有模型 crate；
- **runtime API（运行时 API）**：它是"跑推理时要用的能力"，而不是模型本身——模型结构（多少层、什么注意力）归各模型 crate，HTTP 协议归 frontend，kernel 编译归 kernels。

#### 4.1.2 核心流程

一条推理请求在 workspace 里的分层旅程（上层依据 CLAUDE.md 架构图，下层依据本讲核验的源码证据）：

```
HTTP 请求（POST /v1/completions，OpenAI 兼容）
   │
   ▼
pegainfer-frontend —— 协议层 + 引擎契约（EngineHandle / GenerateRequest / TokenEvent）
   │
   ▼
模型 crate（pegainfer-qwen3 / -qwen35 / -kimi-k2 / ...，每线一个，feature 门控）
   │    各自拥有：config 解析、权重、prefill/decode 执行、scheduler
   │    use pegainfer_core::...
   ▼
pegainfer-core —— 共享运行时 API（本手册主角）
   │    tensor / ffi：kernels 的门面
   │    ops：算子封装 + kernels 算子再出口
   │    page_pool / kv_pool：显存页与 KV 池
   │    weight_loader / rope / decodable_vocab / eos：装配与出口配套
   │    cuda_graph：图捕获；cpu_topology / logging / tracing：宿主服务
   │    pub use pegainfer_kernels::...
   ▼
pegainfer-kernels —— CUDA/Triton/FlashInfer 的构建所有者（build.rs 调 nvcc）
   │
   ▼
CUDA / cuBLAS / FlashInfer（GPU 真正执行）
```

一句话总结 core 的位置：**模型 crate 之下、kernels 之上的中间层**。

#### 4.1.3 源码精读

**证据一：crate 自述（lib.rs 全文仅 15 行，值得整读）**

[src/lib.rs:L1-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs#L1-L15)：crate 级文档注释 `//!` 只有一句自述，随后是 13 个按字母序排列的 `pub mod`，再无别的代码。这个"声明即全部"的根文件本身就是 core 风格的缩影——它是组织层，不是实现层。

```rust
//! Shared runtime API used by pegainfer model crates.

pub mod cpu_topology;
pub mod cuda_graph;
pub mod decodable_vocab;
pub mod eos;
pub mod ffi;
pub mod kv_pool;
pub mod logging;
pub mod ops;
pub mod page_pool;
pub mod rope;
pub mod tensor;
pub mod tracing;
pub mod weight_loader;
```

**证据二：包元数据与依赖**（[Cargo.toml:L1-L5](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L1-L5)、[Cargo.toml:L7-L29](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L7-L29)）

```toml
[package]
edition = "2024"           # 最新 Rust 版次
license = "Apache-2.0"
name = "pegainfer-core"
version = "0.1.0"
```

依赖块共 22 条（L8-L29），其中**工作区内部 crate 只有两条**：

```toml
pegainfer-frontend = { workspace = true }   # L22
pegainfer-kernels  = { workspace = true }   # L23
```

其余是通用库：`cudarc`（CUDA 绑定，L11）、`safetensors`（L26）、`memmap2`（L18）、`parking_lot`（L24）、`half`（L14）、`logforth`（L17）、`fastrace` 与 `opentelemetry` 系（L12-L13、L19-L21）等。**"依赖什么"就是"负责什么"的硬证据**，4.3 节把这些依赖逐一对应回模块。

**证据三：两个"一行模块"**（[src/tensor.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tensor.rs#L1)、[src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ffi.rs#L1)）

```rust
// src/tensor.rs 的全部内容（wc -l = 1）：
pub use pegainfer_kernels::tensor::*;

// src/ffi.rs 的全部内容（wc -l = 1）：
pub use pegainfer_kernels::ffi::*;
```

这两个模块是 pegainfer-kernels 同名模块的**门面**：模型 crate 写 `pegainfer_core::tensor::DeviceContext`，真正的定义在 `pegainfer_kernels::tensor`。看懂这一行，就看懂 core 定位的一大半：**"共享运行时 API" = 一部分向下转发（tensor、ffi，以及 ops 里的长清单）+ 一部分自己实现（page_pool、kv_pool、cuda_graph、weight_loader 等）**。

**证据四：对 frontend 的依赖面极窄**（[src/tracing.rs:L99](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L99)）

```rust
pegainfer_frontend::tracing_state::set_enabled(true);
```

在整个 `src/` 里 grep `pegainfer_frontend` **只有这一处命中**（本讲已核验）。也就是说：core 依赖 frontend 不是为了做 HTTP，而只是让 core 侧的追踪开关与前端状态联动。依赖存在、职责不越界。

#### 4.1.4 代码实践

（源码阅读型实践；下列命令在本讲义撰写时实际执行过，输出已核对。不需要 GPU。）

1. **实践目标**：用两条 grep 验证"core 向下大量委托 kernels、向上几乎不碰协议层"。
2. **操作步骤**（在 `pegainfer-core/` 目录下执行）：

```bash
grep -rln pegainfer_kernels src    # 哪些文件引用 kernels
grep -rn  pegainfer_frontend src   # 哪些行引用 frontend
```

3. **需要观察的现象**：
   - 第一条命中 9 个文件：`tensor.rs`、`ffi.rs`、`ops.rs`、`ops/attention.rs`、`ops/call_spec.rs`、`ops/call_trace.rs`、`ops/paged_plan.rs`、`ops/traced.rs`、`kv_pool.rs`；
   - 第二条只命中一行：`src/tracing.rs:99`。
4. **预期结果/结论**：kernels 的引用覆盖"张量门面 + 算子封装 + KV 池"三大块——core 的重活向下委托；对 frontend 的引用只有一处——core 不承担协议层职责。
5. 两条命令是纯文本操作，任何 checkout 都能复现；若你的输出与上不符，说明代码已前进，请以你当时的 HEAD 为准重新锚定行号。

#### 4.1.5 小练习与答案

**练习 1**：除 lib.rs 第一行的自述外，本讲还给出了哪些源码证据支持"core 为模型 crate 服务"？

**参考答案**：① [Cargo.toml:L22-L23](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L22-L23) 显示 core 只依赖 frontend/kernels 两个工作区 crate，处在模型 crate 之下的公共层；② [src/tensor.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tensor.rs#L1) / [src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ffi.rs#L1) 的门面转发，让模型 crate 只 `use pegainfer_core::...` 一层就能拿到张量与 FFI 类型；③ CLAUDE.md 架构图中各模型 crate 全部汇聚到 "pegainfer-core runtime + pegainfer-kernels"。

**练习 2**：tensor.rs 只有一行，为什么还需要这个文件？

**参考答案**：它是门面模块——给模型 crate 一个稳定路径 `pegainfer_core::tensor::*`，避免每个模型 crate 直接依赖 kernels 的内部模块路径，版本也由 workspace 统一管理。删掉它，所有 `pegainfer_core::tensor::` 引用都得改成直连 kernels。

**练习 3**：core 对 frontend 的唯一直接引用在做什么？这说明了什么？

**参考答案**：[src/tracing.rs:L99](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L99) 调用 `pegainfer_frontend::tracing_state::set_enabled(true)`，把 core 侧追踪开关与前端开关联动。说明 core 不做 HTTP/协议，对 frontend 的耦合被刻意压缩到一条追踪开关线上。

### 4.2 lib.rs 模块清单：13 个 pub mod 与粗分类

#### 4.2.1 概念说明

lib.rs 的 13 个模块是 core 公开面的全部入口。分类前先说清方法论：**下表每条"职责"都取自各文件头部的 doc 注释（`//!` / `///`）或文件头的 use / re-export 语句**，行号全部经 `grep -n` 锚定；而"六大组"的分组本身是本讲的归纳观点，不是源码原文——欢迎你用自己的维度重新分组。

| 分组（讲义观点） | 模块 | 组内共性 |
|---|---|---|
| A 张量与 FFI 门面 | `tensor`、`ffi` | 各 1 行，整体转发 kernels |
| B GPU 算子封装 | `ops` | 自有注意力/分页计划封装 + 大量再出口 kernels 算子 |
| C 推理状态 | `page_pool`、`kv_pool` | 显存页分配与 KV 池，core 自建 |
| D 执行机制 | `cuda_graph` | decode 路径的图捕获/回放 |
| E 装配与出口 | `weight_loader`、`rope`、`decodable_vocab`、`eos` | 磁盘→显存、位置编码表、词表、停止条件 |
| F 宿主服务 | `cpu_topology`、`logging`、`tracing` | CPU 拓扑、日志、分布式追踪 |

#### 4.2.2 核心流程

13 个模块之间**已核验**的依赖方向（箭头 = "use 了对方"）：

```
tensor（门面→kernels）
   ↑↑
   │├── rope        （L6-L7  use crate::tensor::DeviceContext / DeviceVec）
   ├── cuda_graph  （L12-L13 use crate::tensor::DeviceContext / active_cu_stream）
   └── kv_pool     （L13    use crate::tensor::DeviceContext）

page_pool（L3 use parking_lot::Mutex）
   └──→ kv_pool（L10-L12 use page_pool 的 OwnedPagePermit / PageId / PagePool——页池是 KV 池的地基）

ops（L3-L9 声明五个子模块；L11-L16 出口自有封装；L17 起再出口 kernels::ops 长清单）

weight_loader / decodable_vocab / eos：读磁盘文件（checkpoint / tokenizer），相对独立
cpu_topology（L9-L10 use cudarc driver API）/ logging / tracing：宿主侧服务
```

先记住两点（细节留给 u2/u3）：

1. **page_pool 是 kv_pool 的地基**：kv_pool 文件头直接 use 了 page_pool 的三个类型；
2. **ops 是"混合体"**：既有 core 自己写的 attention / paged_plan 封装（子模块），也有从 `pegainfer_kernels::ops` 整清单再出口的算子。

#### 4.2.3 源码精读

13 个模块逐条表（职责尽量引文件头原文；行号均经核验）：

| `pub mod` | 职责（依据文件头） | 证据 |
|---|---|---|
| `tensor` | kernels 张量模块的门面（GPU 张量类型从这里出去） | [src/tensor.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tensor.rs#L1)（全文 1 行） |
| `ffi` | kernels FFI 绑定的门面 | [src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ffi.rs#L1)（全文 1 行） |
| `ops` | "Shared GPU operation wrappers and kernel-crate re-exports"（自述原文：共享 GPU 算子封装与 kernel crate 再出口） | [src/ops.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L1)；声明区 [L3-L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L3-L9)；再出口清单从 [L17](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L17) 起 |
| `page_pool` | "Shared fixed-page allocator with RAII-owned permits"（带 RAII 许可的共享固定页分配器） | [src/page_pool.rs:L5](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L5)；PagePool [L7](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L7)、OwnedPagePermit [L19](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L19)、PageId [L26](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L26) |
| `kv_pool` | KV 池：page-first 几何 + 池/预约/状态；复用 kernels 的 KvFormat / KvStorage | 几何自述 [src/kv_pool.rs:L15-L17](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L15-L17)；再出口 [L7-L8](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L7-L8)；KvLayout [L19](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L19)、KvPool [L139](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L139)、KvReservation [L288](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L288)、KvState [L304](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L304)、KvDesc [L427](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L427) |
| `cuda_graph` | CUDA 图捕获/回放封装，附 dump 子模块 | [src/cuda_graph.rs:L4-L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cuda_graph.rs#L4-L9)（cudarc 图类型）、mod dump [L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cuda_graph.rs#L15)、dump 出口 [L17-L18](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cuda_graph.rs#L17-L18) |
| `weight_loader` | "Safetensors weight loading"（权重加载，含 staging 子目录） | [src/weight_loader.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/weight_loader.rs#L1) |
| `rope` | "RoPE cos/sin table generation"（旋转位置编码 cos/sin 表生成） | [src/rope.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/rope.rs#L1) |
| `decodable_vocab` | "Frontend tokenizer schema and decodable-vocabulary width"（镜像前端 tokenizer 的合并规则，计算可解码词表宽度） | [src/decodable_vocab.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/decodable_vocab.rs#L1) |
| `eos` | "The checkpoint's EOS set, resolved the same way for every model line"（各模型线一致的 EOS 解析） | [src/eos.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/eos.rs#L1)；入口 `eos_token_ids` [L18](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/eos.rs#L18) |
| `cpu_topology` | CPU 拓扑类型（CpuId 等），借助 cudarc driver API | CpuId [src/cpu_topology.rs:L13](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cpu_topology.rs#L13)；cudarc [L9-L10](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cpu_topology.rs#L9-L10) |
| `logging` | "Unified logging configuration"（统一日志配置，基于 logforth） | [src/logging.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/logging.rs#L1) |
| `tracing` | 基于 fastrace 的分布式请求追踪（对接 OTLP；L99 联动 frontend 开关） | [src/tracing.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L1)、[L99](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L99) |

再补两个结构性事实（`ls` 核验）：

- 有同名子目录的模块共 3 个：`ops/`（attention.rs、call_spec.rs、call_trace.rs、paged_plan.rs、traced.rs）、`cuda_graph/`（dump.rs 与 dump/ 目录）、`weight_loader/`（staging.rs）。
- `tests/` 目录下只有一个集成测试文件 `weight_loader_gpu.rs`——core 的大量单元测试内嵌在各源文件的 `#[cfg(test)]` 模块里（这是 u1-l3 的内容）。

#### 4.2.4 代码实践

1. **实践目标**：用 rustdoc 把 core 的公开面可视化，核对 13 个模块，并观察 feature 对文档的影响。
2. **操作步骤**：

```bash
cd <仓库根>/pegainfer-core
cargo doc -p pegainfer-core --no-deps    # --no-deps：只文档化 core 自己
# 然后用浏览器打开 target/doc/pegainfer-core/index.html（或加 --open）
```

3. **需要观察的现象**：左侧 Modules 栏的条目数与名称；`tensor`、`ffi` 两个模块页面的内容规模；`ops` 页面的函数列表规模。
4. **预期结果**：
   - Modules 栏恰好 13 项，与 [src/lib.rs:L3-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs#L3-L15) 一一对应；
   - `tensor` / `ffi` 页面是"再出口集合"（类型实际来自 kernels，rustdoc 会标注 re-export）；
   - `ops` 页面函数很多——[src/ops.rs:L17](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L17) 起长清单的直观体现（本讲核验到 L70 仍在继续，清单终点未逐条数完，精确条数**待确认**）；
   - 默认 feature 下（[Cargo.toml:L35](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L35) `default = []`）看不到 `call_trace` / `traced` 相关条目——它们被 [src/ops.rs:L5-L6](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L5-L6) 与 [L8-L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L8-L9) 的 `#[cfg(feature = "kernel-call-trace")]` 门控（feature 定义在 [Cargo.toml:L36](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L36)）。
5. **说明**：本讲义撰写环境**没有运行** `cargo doc`（不产生构建产物）；第 4 点是依据源码 cfg 与 Cargo 标准行为给出的预期，页面具体呈现**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：13 个模块中哪三个有同名子目录？各含什么文件？

**参考答案**：`ops/`（attention.rs、call_spec.rs、call_trace.rs、paged_plan.rs、traced.rs）；`cuda_graph/`（dump.rs 及 dump/ 目录）；`weight_loader/`（staging.rs）。依据：`ls src/ops src/cuda_graph src/weight_loader`（本讲核验）。

**练习 2**：哪个类型自称"纯值类型、不碰 GPU、不做分配、被三个类型共享"？

**参考答案**：kv_pool 里的 `KvLayout`——"Pure value type — no GPU, no allocation. Shared by `KvPool`, `KvState`, `KvDesc`"（[src/kv_pool.rs:L17](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L17)；几何自述在 [L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L15)，struct 在 [L19](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L19)）。它是 u3-l2 的主角。

**练习 3**：打开 `kernel-call-trace` feature 会额外编译哪些子模块？从哪两处看出来？

**参考答案**：`ops` 的 `call_trace` 与 `traced` 两个子模块。看 [src/ops.rs:L5-L6](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L5-L6)、[L8-L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L8-L9) 的 `#[cfg(feature = "kernel-call-trace")]`，以及 [Cargo.toml:L36](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L36) 的 feature 定义。

### 4.3 工作区分层架构：core 的上下文与三条边界

#### 4.3.1 概念说明

把 core 放回 workspace，它的"邻居"有四类（分工描述依据 CLAUDE.md）：

| 邻居 crate | 职责 | 与 core 的关系 |
|---|---|---|
| pegainfer-frontend | HTTP/协议栈（vllm 模块）+ 引擎契约（EngineHandle、GenerateRequest、TokenEvent）+ ModelLine 分发 | core 依赖它，但只用追踪开关（见 4.1.3 证据四） |
| pegainfer-kernels | tensor/FFI/kernel 的**构建所有者**：build.rs 用 nvcc 编 csrc/*.cu、跑 Triton AOT、接 FlashInfer | core 大量再出口/调用它的产物（src 里 9 个文件引用，见 4.1.4） |
| pegainfer-kv-cache / pegainfer-kv-offload | CLAUDE.md："shared paged-KV primitives live in pegainfer-kv-cache; host/SSD/RDMA offload bridge in pegainfer-kv-offload" | **core 的 Cargo.toml 不依赖它**——core 自带 page_pool / kv_pool。两套 KV 设施的分工**待确认**（u3 处理） |
| 模型 crate（qwen3 / qwen35 / deepseek-v2-lite / gemma4 / kimi-k2 / glm52 / k3） | 各自的 config、权重、prefill/decode 执行、scheduler、测试 | core 的服务对象 |

#### 4.3.2 核心流程

core 的三条职责边界（每条都有源码证据，见 4.3.3）：

```
边界一（对上）：HTTP / OpenAI 协议 ──── 在 frontend，不在 core
边界二（对下）：nvcc / Triton 编译 ─── 在 kernels 的 build.rs，不在 core
边界三（横向）：core 自建 page_pool/kv_pool；pegainfer-kv-cache 是另一套共享 paged-KV 原语
```

为什么边界重要：读任何 core 源码时先问"这件事归 core 吗"——在 core 里找不到一行 HTTP 解析、找不到一个 `.cu` 文件，不是遗漏，是设计。

#### 4.3.3 源码精读

**边界一（HTTP 不在 core）**：CLAUDE.md 对 frontend 的描述是"serving frontend：engine request/event contract（EngineHandle、GenerateRequest、TokenEvent）+ 协议栈（vllm 模块）+ ModelLine dispatch……server binary does pure dispatch"（见 [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/CLAUDE.md)）。core 侧对应证据：全 `src/` 对 `pegainfer_frontend` 的引用仅 [src/tracing.rs:L99](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L99) 一处（4.1.4 的 grep 已核验）。

**边界二（内核编译不在 core）**：`ls` pegainfer-core 的 crate 根，只有 `Cargo.toml`、`src/`、`tests/`（外加本教程目录）——**没有 build.rs，也没有任何 `.cu` 文件**。而 CLAUDE.md 的 Build system 一节明确写着 "`pegainfer-kernels/build.rs` owns CUDA/Triton compilation"。编译发生在 kernels，core 只是消费编译产物（[src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ffi.rs#L1) 转发其 FFI 绑定）。

**边界三（KV 设施双轨，关系待确认）**：core 的依赖块 [Cargo.toml:L7-L29](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L7-L29) 里没有 `pegainfer-kv-cache`；core 用 [src/page_pool.rs:L7](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L7) 的 PagePool 与 [src/kv_pool.rs:L139](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L139) 的 KvPool 自建了 KV 管理，且 kv_pool 还从 kernels 再出口 KvFormat / KvStorage（[src/kv_pool.rs:L7-L8](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L7-L8)）。同时 CLAUDE.md 说共享 paged-KV 原语在 pegainfer-kv-cache crate。**两套设施各自服务谁、是否分层复用，本讲未核验，列为待确认**（u3-l1 / u3-l2 处理）。

**依赖反推表**——把 [Cargo.toml:L8-L29](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L8-L29) 的关键依赖对应回使用它的模块（"依赖什么 = 负责什么"）：

| 依赖（行号） | 用在哪 | 证据 |
|---|---|---|
| `cudarc`（L11） | CUDA 绑定 | [src/cuda_graph.rs:L4-L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cuda_graph.rs#L4-L9)、[src/cpu_topology.rs:L9-L10](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cpu_topology.rs#L9-L10)、[src/kv_pool.rs:L5](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L5) |
| `parking_lot`（L24） | 页池的互斥锁 | [src/page_pool.rs:L3](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L3) |
| `half`（L14） | bf16 类型 | [src/kv_pool.rs:L6](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L6)、[src/rope.rs:L4](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/rope.rs#L4) |
| `safetensors` + `memmap2`（L26 / L18） | 权重加载 | [src/weight_loader.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/weight_loader.rs#L1) 自述 "Safetensors weight loading"（memmap2 的具体调用点未逐一核验，**待确认**） |
| `logforth` + `chrono` + `colored`（L17 / L9 / L10） | 日志 | [src/logging.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/logging.rs#L1) 自述 "Unified logging configuration" |
| `fastrace` + `opentelemetry` 系（L12-L13、L19-L21） | 分布式追踪 | [src/tracing.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L1) |
| `pegainfer-kernels`（L23） | 张量 / FFI / 算子 / KV 格式 | 4.1.4 的 9 文件清单 |
| `pegainfer-frontend`（L22） | 追踪开关联动 | [src/tracing.rs:L99](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L99) |

（`anyhow` / `serde` / `tokio` / `rayon` / `libc` 等通用工具依赖散布各文件，本讲不逐一对应。）

#### 4.3.4 代码实践

（源码阅读型实践，不需要 GPU。）

1. **实践目标**：亲手画一张"带证据的分层图"，并完成一份自己的依赖反推表。
2. **操作步骤**：
   1. 重跑 4.1.4 的两条 grep；
   2. `ls` crate 根，确认没有 build.rs；
   3. 打开 [Cargo.toml:L8-L29](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L8-L29)，把 22 条依赖分成四类：工作区内部 / GPU 相关 / 格式与序列化 / 日志追踪与通用工具；
   4. 为"工作区内部"和"GPU 相关"的每条依赖，用 `grep -rn "<依赖名>" src` 找使用点（对照 4.3.3 的反推表）；
   5. 抄一遍 4.1.2 的分层图，在每条边上标注证据（文件:行号）。
3. **需要观察的现象**：每条依赖能否在 src 里找到使用点；哪些一时找不到。
4. **预期结果**：与 4.3.3 的反推表一致。找不到使用点的依赖（如 `memmap2`）就如实记入待确认清单——**找不到不等于没用，可能只是走了间接路径**。
5. 本讲对 `memmap2` 的具体调用点即标注**待确认**。

#### 4.3.5 小练习与答案

**练习 1**：若要新增一条模型线（某新架构），按 CLAUDE.md 的分工，哪些 crate 必须动？core 要动吗？

**参考答案**：新建一个模型 crate（每条线自有 config / 权重 / prefill/decode 执行 / scheduler / 测试，CLAUDE.md 模型表里每条线都是"crate + feature flag"的组合）；若需要新 kernel，动 pegainfer-kernels；若只是组合既有运行时能力，core 通常不用动。这正是 core 存在的意义：模型差异留在模型 crate。

**练习 2**：core 依赖 frontend（[Cargo.toml:L22](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L22)），而请求流却是 frontend → 模型 crate → core。两个方向矛盾吗？

**参考答案**：不矛盾。**编译期依赖边**（谁 use 谁）与**运行期数据流边**（请求怎么流）是两类不同的边。core 只在追踪开关上 use frontend（[src/tracing.rs:L99](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L99)）；请求则从 frontend 的协议层流进模型 crate 再进 core。这种"依赖方向与调用方向不一致"的窄耦合是常见手法（值得在 u4-l2 再审视）。

**练习 3**：为什么能断言"core 不编译任何 CUDA 代码"？给出两个证据。

**参考答案**：① crate 根没有 build.rs（`ls` 核验：只有 Cargo.toml / src / tests）；② CLAUDE.md Build system 一节明确 nvcc / Triton 编译由 `pegainfer-kernels/build.rs` 拥有，core 的 ffi 模块只是转发其绑定（[src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ffi.rs#L1)）。

## 5. 综合实践

**任务：写一页《pegainfer-core 职责边界备忘录》**（你自己的笔记，建议存为 `openinfer-pegainfer-core-tutorial/notes-u1-l1.md`），把本讲三块内容串成一个交付物：

1. **跑**：完成 4.2.4 的 `cargo doc`（需要 Rust 工具链）与 4.1.4 / 4.3.4 的 grep、ls（不需要 GPU）。
2. **写四节**：
   - **(a) 13 模块分类表**：先不看讲义，凭 cargo doc 页面自己分类，再对照 4.2.3 修正，记录差异点；
   - **(b) core 为谁服务**：引用 [src/lib.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs#L1) 的自述，并补一条你自己找到的证据；
   - **(c) core 明确不做什么**：至少三条（HTTP 协议、内核编译、模型自身逻辑……），每条配证据链接；
   - **(d) 待确认清单**：至少两项（例如：core 的 kv_pool 与 pegainfer-kv-cache crate 的分工；`cpu_topology` 的消费方在哪个 crate；`memmap2` 的具体调用点）。
3. **自检**：备忘录里每个论断都能回答"证据在哪个文件第几行"；回答不了的，降级为待确认。
4. **预期结果**：一页纸、四节、每个论断带证据或待确认标记——这也是后续所有讲义的学习方法预演。

（本综合实践的 grep / ls 部分在讲义撰写时已实际执行验证；`cargo doc` 部分**待本地验证**。）

## 6. 本讲小结

- PegaInfer 是纯 Rust + CUDA、无 PyTorch 的多模型推理引擎；**pegainfer-core 是被所有模型 crate 复用的共享运行时层**，自述见 [src/lib.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs#L1)。
- lib.rs 仅 15 行：1 行自述 + 13 个 `pub mod`（[L3-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs#L3-L15)），可按"门面 / 算子 / 状态 / 执行 / 装配出口 / 宿主服务"粗分六组。
- **core 的一部分公开面是转发**：`tensor` 与 `ffi` 各只有 1 行，是 kernels 的门面；`ops` 也是"自有封装 + kernels 算子再出口"的混合体（再出口清单从 [src/ops.rs:L17](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L17) 起）。
- **两条硬边界**：HTTP 协议在 frontend（core 对其引用仅 [src/tracing.rs:L99](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L99) 一处）；nvcc / Triton 编译在 kernels 的 build.rs（core 的 crate 根没有 build.rs）。
- core 自建推理状态设施：PagePool（[src/page_pool.rs:L7](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L7)）与 KvPool（[src/kv_pool.rs:L139](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L139)）；它与 pegainfer-kv-cache crate 的分工关系**待确认**。
- 学习方法：**每个论断配"文件:行号"证据，核验不了就标待确认**——本讲所有行号均经 `cat -n` / `grep -n` 锚定（教训：`head` 的输出会丢空行，不能用来数行号）。

## 7. 下一步学习建议

- **下一讲 u1-l2《目录结构与模块地图》**：把本讲的文件级地图细化成每个文件的"职责卡"，并检验本讲六大组分类的成色（哪些文件证实了推断、哪些推翻了）。
- **优先通读的小文件**：[src/ops.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs)（115 行，本讲只看了声明区）、[src/page_pool.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs)（194 行，RAII 页池，doc 注释友好）。
- **结合 CLAUDE.md 重读**：对照本讲 4.1.2 的分层图，把 CLAUDE.md "Key abstractions" 清单里的每个 crate 在图中定位；再读 Build system 一节，体会"kernels 拥有编译、core 拥有封装"的分工。
- u2 进入 tensor / ops 内部，u3 进入 kv_pool / page_pool / weight_loader 内部——届时回填本讲留下的待确认项（pegainfer-kv-cache 的分工、memmap2 调用点、cpu_topology 的消费方）。
