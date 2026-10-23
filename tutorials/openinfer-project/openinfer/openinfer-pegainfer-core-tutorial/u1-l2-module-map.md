# 目录结构与模块地图（u1-l2）

> 本讲承接 u1-l1《项目定位》：上一讲从外部看清了 pegainfer-core 在整个引擎里的位置（模型 crate 之下的共享运行时，向上不碰 HTTP、向下不编 CUDA 内核）。本讲走进 crate 内部，把它的物理目录结构逐个走读一遍，产出一张可以长期使用的「模块地图」。

## 1. 本讲目标

学完本讲，你应该能够：

1. 凭记忆画出 pegainfer-core 的完整目录树：crate 根（`Cargo.toml`、`src/`、`tests/`）、`src/` 下 14 个顶层 `.rs` 文件、`ops/`、`cuda_graph/`、`weight_loader/` 三个子目录，并标注每个文件的推断职责。
2. 解释 Rust「模块文件 + 同名目录」的组织惯例：`ops.rs` 与 `ops/` 目录如何拼成同一个模块 `ops`，`mod x;` 声明如何找到 `x.rs` 或 `x/`。
3. 掌握一套可复用的「职责推断」方法：从 `lib.rs` 的模块清单、文件头部 doc 注释、`pub use` 再出口语句三处证据推断文件职责；证据不足时写出「推断卡」并标注「待确认」。
4. 把 13 个模块分成「纯数据结构」（`kv_pool`、`page_pool`）与「机制」（`ops`、`cuda_graph`）两类，并说明这个区分为什么能帮你决定先读哪个文件。

## 2. 前置知识

### 2.1 承接 u1-l1 的分组

u1-l1 已经把 `lib.rs` 的 13 个模块按职责分成六组：门面（`tensor`、`ffi`）、算子封装与再出口（`ops`）、推理状态（`page_pool`、`kv_pool`）、执行机制（`cuda_graph`）、装配出口（`weight_loader`、`rope`、`decodable_vocab`、`eos`）、宿主服务（`cpu_topology`、`logging`、`tracing`）。本讲不重复论证这个分组，而是回答下一个自然的问题：**这些模块在磁盘上长什么样？每个文件凭什么这样切分？**

### 2.2 Rust 模块系统最小集

初学者只需要以下几条规则就能读懂本 crate 的文件布局：

- **crate 根**：Cargo 约定库 crate 的入口是 `src/lib.rs`，它是模块树的根。
- **`mod x;` 声明**：在某个模块文件里写 `mod x;`，编译器会在同级找 `x.rs` 或 `x/mod.rs`，把它挂成当前模块的子模块 `x`。
- **模块文件 + 同名目录**（Rust 2018 起的惯例）：`foo.rs` 与 `foo/` 目录可以并存——`foo.rs` 是模块 `foo` 的本体，`foo/bar.rs` 是它的子模块 `foo::bar`。不需要 `mod.rs`。
- **可见性**：`mod x;`（默认私有，仅 crate 内可见）与 `pub mod x;`（对外公开）。
- **`pub use`（再出口）**：把定义在别处的名字搬进当前模块的路径下，例如 `pub use pegainfer_kernels::tensor::*;` 让使用者以为类型就定义在本 crate。连续大量 `pub use` 的文件通常是在做「门面」。
- **`#[cfg(feature = "...")]`**：条件编译，与 Cargo feature 绑定；`#[cfg(test)]` 则只在 `cargo test` 时编译，常用于内嵌单元测试。

### 2.3 「纯数据结构」与「机制」是什么意思

- **纯数据结构模块**：主要定义类型、字段和少量纯函数（如页分配、索引计算），可以不碰 GPU 就在脑子里推演它的行为。本 crate 里的 `page_pool.rs`、`kv_pool.rs` 属于这类（`kv_pool` 也持有 GPU 缓冲，但它的核心是「布局与记账」）。
- **机制模块**：编排执行流程——发起 GPU 调用、捕获 CUDA Graph、从磁盘装载权重。理解它们要跟踪调用链与时序，读法完全不同。`ops.rs`、`cuda_graph.rs`、`weight_loader.rs` 属于这类。

先分清这两类，能帮你决定阅读策略：数据结构模块适合精读每一行；机制模块先抓主流程骨架再回填细节。

## 3. 本讲源码地图

本讲涉及的关键文件（相对 `pegainfer-core/`），行数来自当前 HEAD 的 `wc -l`：

| 文件 | 规模 | 在本讲中的角色 |
| --- | --- | --- |
| `src/lib.rs` | 15 行 | crate 根：13 个 `pub mod`，目录树的权威清单 |
| `src/ops.rs` | 115 行 | 「模块文件 + 同名目录」的第一样本：子模块声明 + 两层再出口 |
| `src/ops/`（5 个文件） | 共约 44 KB | `ops` 模块的子模块群 |
| `src/kv_pool.rs` | 563 行 | 「纯数据结构」样本：KV 页池布局与预约 |
| `src/page_pool.rs` | 194 行 | 「纯数据结构」样本：固定页分配器 + RAII 许可 |
| `src/cuda_graph.rs` + `src/cuda_graph/` | 234 行 + 2 文件 | 「机制」样本：`mod dump;` 子模块声明 |
| `src/weight_loader.rs` + `src/weight_loader/` | 1169 行 + 1 文件 | 「机制」样本：`mod staging;` 子模块声明 |
| `src/tensor.rs`、`src/ffi.rs` | 各 1 行 | 一行门面：纯粹转发 `pegainfer-kernels` |
| 其余顶层文件 | 108–329 行 | 职责卡练习对象（第 4.3 节） |
| `tests/weight_loader_gpu.rs` | — | 全 crate 唯一的集成测试文件 |
| `Cargo.toml` | — | 包定义（`name = "pegainfer-core"`，edition 2024） |

## 4. 核心概念与源码讲解

### 4.1 目录树巡览

#### 4.1.1 概念说明

一个 Rust 库 crate 的物理形态非常简单：`Cargo.toml` 描述包（名字、版本、依赖、feature），`src/` 放源码，`tests/` 放集成测试（每个 `.rs` 编译成独立二进制，只能访问 crate 的公开 API）。pegainfer-core 全部源码只有 **22 个文件、顶层 14 个文件合计 3517 行**——对一个推理引擎的共享运行时来说相当紧凑。本节先把这棵树完整画出来；后面两节再解释「为什么长这样」。

#### 4.1.2 核心流程

当前 HEAD 下的完整目录树（`find src -type f | sort` 的真实输出整理）：

```text
pegainfer-core/
├── Cargo.toml                     # 包定义
├── src/
│   ├── lib.rs                     #  15 行    crate 根：13 个 pub mod
│   ├── cpu_topology.rs            # 329 行    宿主服务：CPU 拓扑（推断）
│   ├── cuda_graph.rs              # 234 行    CUDA Graph 执行机制
│   ├── cuda_graph/
│   │   ├── dump.rs                #           图导出的模块本体
│   │   └── dump/
│   │       └── render.rs          #           图导出的渲染子模块
│   ├── decodable_vocab.rs         # 161 行    词表→可解码文本（推断）
│   ├── eos.rs                     # 162 行    EOS 判定（推断）
│   ├── ffi.rs                     #   1 行    门面：转发 kernels 的 FFI
│   ├── kv_pool.rs                 # 563 行    KV 页池布局与预约
│   ├── logging.rs                 # 233 行    日志初始化（推断）
│   ├── ops.rs                     # 115 行    GPU 算子封装与再出口
│   ├── ops/
│   │   ├── attention.rs           #           注意力包装
│   │   ├── call_spec.rs           #           调用规格
│   │   ├── call_trace.rs          #           调用跟踪（feature 门控）
│   │   ├── paged_plan.rs          #           分页计划
│   │   └── traced.rs              #           跟踪版算子（feature 门控）
│   ├── page_pool.rs               # 194 行    固定页分配器 + RAII 许可
│   ├── rope.rs                    # 232 行    旋转位置编码表（推断）
│   ├── tensor.rs                  #   1 行    门面：转发 kernels 的张量
│   ├── tracing.rs                 # 108 行    分布式追踪（推断）
│   ├── weight_loader.rs           # 1169 行   safetensors 权重装载
│   └── weight_loader/
│       └── staging.rs             #           权重staging子模块
└── tests/
    └── weight_loader_gpu.rs       # 唯一的集成测试（需 GPU）
```

先记住三个肉眼可见的观察，后面逐一解释：

1. **顶层文件普遍很小**：14 个文件里 10 个不到 250 行，最大的 `weight_loader.rs` 也只有 1169 行；没有万行巨石文件。
2. **只有三个模块拥有子目录**：`ops`、`cuda_graph`、`weight_loader`——恰好是职责最重、文件本体最大或最复杂的三个模块，子目录是「拆分」的痕迹。
3. **两个只有 1 行的文件**（`tensor.rs`、`ffi.rs`）：它们不是实现，是转发。

#### 4.1.3 源码精读

目录树的权威清单就是 crate 根。[src/lib.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs#L1) 是整个 crate 唯一的模块级 doc 注释，一句话给 crate 定位（「被 pegainfer 各模型 crate 使用的共享运行时 API」——u1-l1 已详细展开）：

```rust
//! Shared runtime API used by pegainfer model crates.
```

[src/lib.rs:L3-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs#L3-L15) 一次性声明了全部 13 个模块，**严格按字母序排列**，没有任何 `#[cfg]` 条件编译——也就是说无论开什么 feature，这 13 个模块都存在：

```rust
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

对照目录树会发现一个一一对应关系：13 个 `pub mod` 恰好对应 `src/` 下除 `lib.rs` 外的 13 个顶层 `.rs` 文件；`ops/`、`cuda_graph/`、`weight_loader/` 三个子目录**不在这份清单里新增条目**，因为它们只是同名模块的内部拆分（见 4.2）。

两个 1 行文件是目录树里最反直觉的部分，值得整行看完。[src/tensor.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tensor.rs#L1) 是文件的全部内容，一行 `pub use` 把 `pegainfer-kernels` 里定义的全部张量类型搬到 `pegainfer_core::tensor` 路径下：

```rust
pub use pegainfer_kernels::tensor::*;
```

[src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ffi.rs#L1) 同样只有一行，转发 kernels 的 FFI 绑定：

```rust
pub use pegainfer_kernels::ffi::*;
```

这就是 u1-l1 说的「门面」：类型真正定义在 `pegainfer-kernels`（张量与 CUDA FFI 的归属地），`pegainfer-core` 只提供稳定路径。模型 crate 写 `use pegainfer_core::tensor::Tensor` 时，不需要知道背后是哪个 crate。

#### 4.1.4 代码实践

**实践目标**：亲手重建上面这棵目录树，确认它与本讲一致（也确认你本地检出的代码版本与本讲相同）。

**操作步骤**：

1. 在 `pegainfer-core` 目录下执行：

   ```bash
   find src -type f | sort
   ls tests
   wc -l src/*.rs
   ```

2. 把三条命令的输出与 4.1.2 的目录树逐行对照。

**需要观察的现象**：`find` 应输出 22 个文件路径；`ls tests` 只有一个文件 `weight_loader_gpu.rs`；`wc -l` 中 `tensor.rs` 和 `ffi.rs` 各为 1，`weight_loader.rs` 是唯一超过 1000 行的文件。

**预期结果**：目录树完全一致。如果不一致（例如多出或少了文件），说明你本地的 HEAD 不是 `223206b7`，本讲的行号会随之漂移，需要先用 `git log --oneline -1` 确认版本。以上命令均为只读操作，已按当前 HEAD 验证；你的环境输出仍以「待本地验证」的态度核对一遍。

#### 4.1.5 小练习与答案

**练习 1**：`src/` 下有 14 个顶层 `.rs` 文件，但 `lib.rs` 只声明了 13 个模块。差的那个是谁？它是什么？

答案：`lib.rs` 自己。它是 crate 根，不需要（也不能）声明自己；其余 13 个文件与 13 个 `pub mod` 一一对应。

**练习 2**：`tests/` 目录和 `src/` 里内嵌的 `#[cfg(test)] mod tests` 都放测试，它们能测试的东西有什么差别？

答案：`tests/` 下的集成测试把 crate 当外部库用，只能访问公开 API（所以叫它 `weight_loader_gpu.rs`——它从外面调用权重装载流程，且需要真实 GPU 与权重文件）；内嵌单元测试与实现同文件，可以测私有函数和内部不变量，例如 [src/page_pool.rs:L132-L133](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L132-L133) 的 `#[cfg(test)] mod tests` 直接测页分配与归还。

**练习 3**：不看上文，说出三个拥有子目录的模块，并猜一个共同点。

答案：`ops`、`cuda_graph`、`weight_loader`。共同点：它们都是「机制」型模块、职责最重（`weight_loader.rs` 1169 行是全 crate 最大文件），子目录是对复杂度的拆分；而纯数据结构模块（`page_pool`、`kv_pool`）和宿主服务模块都保持单文件。

### 4.2 Rust 模块声明与文件布局

#### 4.2.1 概念说明

Rust 允许一个模块的代码横跨「一个文件 + 一个同名目录」：`foo.rs` 是模块 `foo` 的本体，`foo/` 里的 `bar.rs` 通过在 `foo.rs` 里写 `mod bar;` 成为子模块 `foo::bar`。编译器解析 `mod x;` 时，会在**当前模块文件的同级**找 `x.rs` 或 `x/mod.rs`。所以：

- `src/lib.rs` 里的 `pub mod ops;` 找到 `src/ops.rs`；
- `src/ops.rs` 里的 `mod attention;` 找到 `src/ops/attention.rs`（不是 `src/attention.rs`）。

这套惯例让 pegainfer-core 不需要任何 `mod.rs`：三个复杂模块各自持有「模块文件 + 同名目录」的形态——

| 模块文件 | 同名目录 | 子模块声明位置 |
| --- | --- | --- |
| `src/ops.rs` | `src/ops/`（5 个文件） | [src/ops.rs:L3-L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L3-L9) |
| `src/cuda_graph.rs` | `src/cuda_graph/`（`dump.rs` + `dump/render.rs`） | [src/cuda_graph.rs:L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cuda_graph.rs#L15) 的 `mod dump;` |
| `src/weight_loader.rs` | `src/weight_loader/`（`staging.rs`） | [src/weight_loader.rs:L30](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/weight_loader.rs#L30) 的 `mod staging;` |

注意 `cuda_graph/dump/` 里还有一层 `render.rs`：`dump.rs` 自己又是一个「模块文件 + 同名目录」，同一惯例可以递归嵌套。

#### 4.2.2 核心流程

[src/ops.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs) 只有 115 行，但结构上分成清晰的四段，是全 crate 最值得先读懂的文件之一：

```text
L1        //! 模块自述
L2
L3-L9     子模块声明（5 个，可见性与 feature 各不相同）
L10
L11-L16   第一层再出口：自家子模块 attention / paged_plan 的精选符号
L17-L95   第二层再出口：pegainfer_kernels::ops 的 69 个符号
            （其中 10 个被 #[cfg(not(feature = "kernel-call-trace"))] 守卫）
L96-L115  换装块：#[cfg(feature = "kernel-call-trace")] 下，同样的 10 个名字
            改从自家子模块 traced:: 再出口
```

用伪代码概括它的意图：

```text
默认构建：      ops::gemm_into        = kernels 里的原生包装
开 trace feature：ops::gemm_into        = traced::gemm_into（带调用跟踪的同名包装）
```

也就是说，`kernel-call-trace` 这个 feature 不新增路径，而是**在完全相同的路径上替换实现**——下游模型 crate 的 `use` 语句一个字都不用改。这是本 crate 里「feature 门控」的标准用法。

#### 4.2.3 源码精读

**第一段：五个子模块声明**（[src/ops.rs:L3-L9](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L3-L9)）。短短 7 行里出现了三种暴露策略，每一行都对应 `src/ops/` 里的一个文件：

```rust
mod attention;                                    // 私有：只经精选再出口暴露
pub mod call_spec;                                // 公开：外部可直接 use
#[cfg(feature = "kernel-call-trace")]
pub mod call_trace;                               // 公开但 feature 门控
mod paged_plan;                                   // 私有
#[cfg(feature = "kernel-call-trace")]
mod traced;                                       // 私有且 feature 门控
```

**第二段：自家符号的精选再出口**（[src/ops.rs:L11-L16](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L11-L16)）。`attention` 与 `paged_plan` 虽是私有模块，但它最有用的 6 个符号被搬到 `ops` 路径下——外界看到的是 `pegainfer_core::ops::prefill_attention_paged_into`，而不是更深的 `ops::attention::…`：

```rust
pub use attention::paged_attention_batch_decode_into;
pub use attention::paged_attention_batch_decode_split_kv_into;
pub use attention::prefill_attention_paged_into;
pub use paged_plan::PrefillPagedPlan;
pub use paged_plan::SplitKvCsr;
pub use paged_plan::build_split_kv_csr;
```

**第三段：kernels 的批量再出口**从 [src/ops.rs:L17](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L17) 的 `pub use pegainfer_kernels::ops::GEMM_LT_MAX_N;` 开始，按符号字母序一直排到 L95，共 69 条。其中 10 条被 `#[cfg(not(...))]` 守卫，例如 [src/ops.rs:L34-L35](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L34-L35)：默认构建时 `embedding_batch` 直接来自 kernels：

```rust
#[cfg(not(feature = "kernel-call-trace"))]
pub use pegainfer_kernels::ops::embedding_batch;
```

**第四段：换装块**（[src/ops.rs:L96-L115](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L96-L115)）：同一个 feature 打开时，这 10 个名字逐一改从 `traced::` 再出口，例如 [src/ops.rs:L97](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L97) 与 [src/ops.rs:L99](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L99)：

```rust
#[cfg(feature = "kernel-call-trace")]
pub use traced::embedding_batch;
#[cfg(feature = "kernel-call-trace")]
pub use traced::fused_add_rms_norm_batch_into;
```

`cfg(not(...))` 与 `cfg(...)` 互斥，所以任何 feature 组合下每个名字都恰好有一个定义——这就是「同路径换装」能编译通过的原因。

回到另外两个样本。[src/cuda_graph.rs:L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cuda_graph.rs#L15) 只有一行 `mod dump;`，把「CUDA Graph 导出/渲染」拆进 `cuda_graph/dump.rs` 与 `cuda_graph/dump/render.rs`，主文件专注捕获与执行。[src/weight_loader.rs:L30-L32](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/weight_loader.rs#L30-L32) 声明私有子模块 `staging` 并立刻取用其中的两个类型，说明 1169 行的主文件把「权重分片暂存」这类辅助职责也拆了出去：

```rust
mod staging;
use staging::ColShardPlan;
use staging::WeightStager;
```

#### 4.2.4 代码实践

**实践目标**：用只读命令亲手核对本节讲的所有结构事实，体验「结构可以 grep 出来」。

**操作步骤**：

1. 查看 ops.rs 的模块声明与守卫分布：

   ```bash
   grep -n '^mod \|^pub mod \|^#\[cfg' src/ops.rs
   ```

2. 数出两层再出口的规模：

   ```bash
   grep -c 'pub use pegainfer_kernels::ops::' src/ops.rs   # 预期 69
   grep -c 'pub use traced::' src/ops.rs                   # 预期 10
   ```

3. 验证「模块文件 + 同名目录」的三处对应：

   ```bash
   ls src/ops src/cuda_graph src/cuda_graph/dump src/weight_loader
   grep -n '^mod ' src/cuda_graph.rs src/weight_loader.rs
   ```

4. 打开两个单行门面：`cat src/tensor.rs src/ffi.rs`。

**需要观察的现象**：步骤 1 应输出 5 个子模块声明（L3-L9）与 10 个 `#[cfg(not(...))]` 守卫（L34、L43、L51、L53、L57、L59、L61、L68、L74、L88）；步骤 3 中 `mod dump;` 在 cuda_graph.rs 第 15 行、`mod staging;` 在 weight_loader.rs 第 30 行。

**预期结果**：所有数字与本节引用的行号一致。若你本地 HEAD 不同，行号会漂移，以步骤 1-3 的实际输出为准（本讲数字均按 HEAD `223206b7` 核对；你的运行结果仍属「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：`attention` 是私有模块，那外部怎么用它的功能？

答案：通过 [src/ops.rs:L11-L13](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L11-L13) 的精选再出口——`paged_attention_batch_decode_into` 等三个函数被 `pub use` 到 `ops` 路径。私有 + 再出口是一种「只暴露想暴露的表面」的策略。

**练习 2**：如果删除 `src/ops/attention.rs` 这个文件但保留 ops.rs 的声明，会发生什么？

答案：编译失败。`mod attention;`（[src/ops.rs:L3](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L3)）要求编译器能在同级找到 `ops/attention.rs`（或 `ops/attention/mod.rs`），找不到会报「file not found for module」类错误。文件布局是编译期契约。

**练习 3**：`pub use pegainfer_kernels::ops::gemm_into;`（cfg(not) 版）与 `pub use traced::gemm_into;`（cfg(feature) 版）同名，为什么不会冲突？

答案：两个 `#[cfg]` 条件互为否定，任何一次编译里恰好只有一个分支被编译，「同一路径、两种实现」的换装因此合法。这正是 `kernel-call-trace` 能零成本开关、且下游无需改 `use` 语句的原因。

### 4.3 职责推断方法：从文件到「职责卡」

#### 4.3.1 概念说明

读完目录树，自然要问：每个文件到底干什么？本节给出一套三层证据法，让你在**不逐行读完 3517 行**的情况下，为每个文件写出一行「职责卡」（文件 → 一句话职责 → 依据）：

1. **宏观证据**：`lib.rs` 的模块清单 + u1-l1 的六组分类——回答「它在 crate 里扮演什么角色」。
2. **自述证据**：文件头部的 `//!` 模块注释——作者自己写的一句话职责。本 crate 14 个顶层文件里有 8 个带 `//!`（lib、ops、weight_loader、rope、decodable_vocab、eos、logging、tracing）。
3. **微观证据**：`pub use` 再出口、`pub struct` / `pub fn` 签名、`use` 依赖——回答「它由什么构成、依赖谁」。

三层齐备的卡标「有据」；只有名字和规模支撑的卡标「推断」并注明「待确认」——这不是敷衍，而是诚实地记录知识边界，等后续单元读到时再升级。

#### 4.3.2 核心流程

职责卡的标准流程：

```text
看到文件 → 查 lib.rs 里它属于哪一组（宏观）
         → 读文件头 1-6 行找 //!（自述）
         → grep pub struct / pub fn / pub use（微观）
         → 写一句话职责 + 列证据 + 标置信度
```

判断「纯数据结构」还是「机制」有两个信号：

- **数据信号**：导入以 `std`、`parking_lot::Mutex` 为主；导出的是描述布局/记账的类型。比如 `page_pool` 的全部状态就是一个空闲页栈加容量，满足 \( \text{available\_pages} = \text{capacity\_pages} - \text{已租出页数} \)。
- **机制信号**：导入 `cudarc` 的 driver/CUDA Graph 类型、持有流（stream）、编排加载顺序；比如 `cuda_graph.rs` 开头就是一整排 `CUgraph`、`CUgraphExec`、流捕获模式枚举的导入。

信号要综合看：`page_pool` 有 `Mutex` 却仍是数据结构（锁只保护空闲表）；`kv_pool` 持有 GPU 缓冲，但核心职责是布局与预约记账。

#### 4.3.3 源码精读

**样本一：page_pool.rs——最干净的「有据卡」。** [src/page_pool.rs:L5](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L5) 的类型注释一句话说清职责：「带 RAII 许可的共享固定页分配器」：

```rust
/// Shared fixed-page allocator with RAII-owned permits.
```

微观证据与之完全吻合：[L7](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L7) `pub struct PagePool`、[L19](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L19) `pub struct OwnedPagePermit`、[L26](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L26) `pub struct PageId(usize)`。行为面也只有寥寥几个方法：[L45](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L45) `try_acquire_many`（一次租 N 页，不够返回 None）、[L91](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L91) `absorb`（合并同池许可）、[L101](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L101) `try_grow`（追加租页）。RAII 的落点是 [L120](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L120) 的 `impl Drop for OwnedPagePermit`：许可被丢弃时自动把页还回空闲表——「忘记释放」这类 bug 在类型层面被消灭。

**样本二：kv_pool.rs——类型注释代替模块注释。** 这个 563 行的文件没有 `//!`，但类型级注释足够定位职责。[L15-L17](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L15-L17)：

```rust
/// Page-first geometry: dimensions and derived strides for one page.
///
/// Pure value type — no GPU, no allocation. Shared by `KvPool`, `KvState`, `KvDesc`.
```

[L127-L137](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L127-L137) 的 `KvPool` 注释还画出了「页优先」的内存布局——每页内部按层连续存放 K/V：

```text
page_i: [L0_K | L0_V | L1_K | L1_V | ... | Ln_K | Ln_V]
```

微观证据：[L7-L8](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L7-L8) 从 kernels 再出口 `KvFormat`/`KvStorage`；[L9-L11](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L9-L11) 显式 `use crate::page_pool::{OwnedPagePermit, PageId, PagePool}`——**kv_pool 建立在 page_pool 之上**，页的租还全部交给后者；再加上 [L278](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L278) `try_reserve`、[L288](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L288) `KvReservation`、[L301-L304](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L301-L304) 每请求的 `KvState`，构成「池 → 预约 → 请求态」三层。

**样本三：带 `//!` 自述的文件群**，职责卡可以直接抄注释：

- [src/ops.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L1)：「Shared GPU operator wrappers and kernel-crate re-exports」。
- [src/weight_loader.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/weight_loader.rs#L1)：「Safetensors weight loading」。
- [src/rope.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/rope.rs#L1)：「RoPE cos/sin table generation」。
- [src/decodable_vocab.rs:L1-L6](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/decodable_vocab.rs#L1-L6)：前端分词器 schema 与「最宽可解码 token id」的合并计算。
- [src/eos.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/eos.rs#L1)：checkpoint 的 EOS 集合，所有模型线统一解析。
- [src/logging.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/logging.rs#L1)：统一日志配置。
- [src/tracing.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L1)：经 fastrace → OTLP 的分布式请求追踪（注释还说明：未设 OTLP 端点时探针几乎零开销）。

**样本四：无自述的推断卡。** [src/cuda_graph.rs:L1-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cuda_graph.rs#L1-L15) 没有 `//!`，但文件头 15 行给出强旁证：一整段 `cudarc::driver::sys` 的导入——`CUgraph`、`CUgraphExec`、图实例化标志、流捕获模式枚举，最后是 `mod dump;`。「直接编排 CUDA 图捕获与执行」的机制职责可以放心写，但卡片应标注「推断（强旁证）」。`cpu_topology.rs` 连旁证都要靠文件名，卡上就老老实实写「推断，待确认」。

**两类模块对照表**（本讲结论，后续单元逐一验证）：

| 类别 | 模块 | 读法建议 |
| --- | --- | --- |
| 纯数据结构 | `page_pool`、`kv_pool`（布局/记账部分） | 逐行精读，可纸面推演 |
| 机制 | `ops`（算子编排）、`cuda_graph`（图捕获执行）、`weight_loader`（磁盘→GPU 装载） | 先抓主流程骨架与调用链 |
| 门面 | `tensor`、`ffi` | 记住转发关系即可 |
| 宿主服务 | `cpu_topology`、`logging`、`tracing` | 用到时再读 |

#### 4.3.4 代码实践（本讲主实践：写职责卡）

**实践目标**：为 14 个顶层文件和 3 个子目录各写一张职责卡，亲身走一遍三层证据法。

**操作步骤**：

1. 先不看下文答案，准备一张三列表（对象 | 一句话职责 | 依据），对着 4.1.2 的目录树逐个填写。
2. 用一条命令找出「有自述」的文件，检查哪些卡可以升级为「有据」：

   ```bash
   grep -n '//!' src/*.rs
   ```

3. 对没有 `//!` 的文件，补微观证据：

   ```bash
   grep -n 'pub struct\|pub fn' src/page_pool.rs | head
   grep -n 'use crate::page_pool' src/kv_pool.rs
   head -15 src/cuda_graph.rs
   ```

4. 对照下面的参考答案修正卡片，把仍是「推断」的卡标记「待确认」。

**参考答案**（依据均为本讲已核对的源码位置）：

| 对象 | 职责卡 | 依据 | 置信度 |
| --- | --- | --- | --- |
| `lib.rs` | crate 根，声明全部 13 个模块 | [lib.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs#L1)、[L3-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs#L3-L15) | 有据 |
| `tensor.rs` | 张量类型门面：一行转发 kernels | [tensor.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tensor.rs#L1) | 有据 |
| `ffi.rs` | FFI 门面：一行转发 kernels | [ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ffi.rs#L1) | 有据 |
| `ops.rs` | GPU 算子统一入口：自家包装 + kernels 再出口 + trace 换装 | [ops.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L1)，见 4.2 节 | 有据 |
| `kv_pool.rs` | KV 页式布局、页池预约、每请求 KV 态（建立在 page_pool 上） | [L15-L17](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L15-L17)、[L127-L137](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L127-L137)、[L9-L11](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L9-L11) | 有据 |
| `page_pool.rs` | 固定页分配器 + RAII 页许可 | [L5](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L5)、[L7](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L7)、[L120](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/page_pool.rs#L120) | 有据 |
| `cuda_graph.rs` | CUDA Graph 捕获/执行机制（含图导出子模块） | 头部 CUgraph 系导入（[L1-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cuda_graph.rs#L1-L15)） | 推断（强旁证） |
| `weight_loader.rs` | safetensors 权重装载 | [L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/weight_loader.rs#L1)、[L30-L32](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/weight_loader.rs#L30-L32) | 有据 |
| `rope.rs` | RoPE cos/sin 表生成 | [rope.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/rope.rs#L1) | 有据 |
| `decodable_vocab.rs` | 前端词表 schema 合并与可解码宽度计算 | [L1-L6](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/decodable_vocab.rs#L1-L6) | 有据 |
| `eos.rs` | EOS 集合的统一解析 | [eos.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/eos.rs#L1) | 有据 |
| `cpu_topology.rs` | 宿主 CPU 拓扑/亲和性服务 | 无 `//!`，仅文件名与 329 行规模 | 推断，待确认 |
| `logging.rs` | 统一日志配置 | [logging.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/logging.rs#L1) | 有据 |
| `tracing.rs` | fastrace→OTLP 请求追踪 | [tracing.rs:L1](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tracing.rs#L1) | 有据 |
| `ops/` 子目录 | attention（注意力包装）、paged_plan（分页计划）、traced（跟踪换装）有 ops.rs 再出口佐证；call_spec、call_trace 仅名字与可见性佐证 | [ops.rs:L11-L16](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L11-L16)、[L96-L115](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops.rs#L96-L115) | 前三有据，后两推断 |
| `cuda_graph/` 子目录 | 图导出：dump.rs + dump/render.rs（私有子模块） | [cuda_graph.rs:L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cuda_graph.rs#L15) + 文件名 | 推断，待确认 |
| `weight_loader/` 子目录 | 权重分片暂存（ColShardPlan / WeightStager） | [weight_loader.rs:L30-L32](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/weight_loader.rs#L30-L32) | 推断（有旁证） |

**需要观察的现象**：步骤 2 的输出应显示 8 个文件带 `//!`（lib、ops、weight_loader、rope、decodable_vocab、eos、logging、tracing），而 page_pool、kv_pool、cuda_graph、cpu_topology、tensor、ffi 不在其中；步骤 3 中 kv_pool.rs 的三行 `use crate::page_pool::…` 出现在文件头 L9-L11。

**预期结果**：你的卡片与参考答案的「有据」行一致；「推断」行允许措辞不同，但都应带着各自的依据。命令输出请以本地为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`kv_pool.rs` 没有模块级 `//!` 注释，为什么它的职责卡仍然算「有据」？

答案：三层证据齐备——类型级 `///` 注释（[L15-L17](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L15-L17)、[L127-L137](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L127-L137) 含布局图）、显式依赖（L9-L11 引 page_pool 的三个类型）、清晰的类型族（KvLayout / KvPool / KvReservation / KvState）。`//!` 只是自述证据的一种形态。

**练习 2**：给出两个证据说明 `cuda_graph.rs` 是「机制」模块而非「纯数据结构」。

答案：其一，文件头导入 `cudarc::driver::sys` 的 `CUgraph` / `CUgraphExec` 与流捕获模式枚举（[L1-L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cuda_graph.rs#L1-L15)）——直接编排 CUDA 运行时对象；其二，[L15](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/cuda_graph.rs#L15) 还拆出了 `mod dump;` 子模块——机制模块复杂到需要拆分的典型信号。

**练习 3**：既然 `tensor.rs` 只是转发，为什么不让模型 crate 直接依赖 `pegainfer-kernels`？

答案（讨论型参考）：技术上可行，但会破坏 u1-l1 讲过的分层——`pegainfer-core` 是模型 crate 面对的稳定 API 面，门面把 kernels 的内部重组（改名、迁移、增删）隔离在 core 之下；`ops.rs` 的换装块同理，下游的 `use` 路径永远不变。两个 1 行文件换来整条依赖链的稳定，是很划算的写法。

## 5. 综合实践

**任务：盲读—验证闭环（纯源码阅读，无需 GPU）。**

1. **盲读**：把 4.1.2 目录树里的职责注释全部遮住，只看文件名与行数，10 分钟内为下列 5 个对象写出预测卡：`cpu_topology.rs`、`cuda_graph.rs`、`tracing.rs`、`ops/call_spec.rs`、`weight_loader/staging.rs`。
2. **验证**：逐条用只读命令检验你的预测——
   - `grep -n '//!' src/cpu_topology.rs src/tracing.rs`（有无自述、自述说了什么）
   - `grep -n 'CUgraph\|CUstream' src/cuda_graph.rs | head -5`（是否真编排 CUDA 图 API）
   - `grep -n 'use crate::page_pool' src/kv_pool.rs`（kv_pool 是否建立在 page_pool 之上）
   - `grep -n 'safetensors\|memmap2' Cargo.toml`（从依赖面反推 crate 职责边界：cudarc/half 面向 GPU，safetensors/memmap2 面向权重装载）
3. **观察与预期**：`tracing.rs` 的 `//!` 会明确写出 fastrace → OTLP 与「未设端点时近乎零开销」；`cuda_graph.rs` 会命中多处 `CUgraph`；`kv_pool.rs` 头部三行 `use crate::page_pool::…` 印证依赖；`Cargo.toml` 命中 `safetensors` 与 `memmap2`。`cpu_topology.rs` 大概率没有 `//!`，此时你的卡片保持「推断，待确认」就是正确做法。
4. **产出**：把最终卡片合并进你的 4.3.4 职责卡表，形成本 crate 的第一版「模块地图」，后续单元（u2 张量与算子、u3 状态与装配）逐张升级。运行输出均以本地为准（待本地验证）。

## 6. 本讲小结

- pegainfer-core 的物理结构极简：crate 根只有 `Cargo.toml`、`src/`（22 个文件）与 `tests/`（1 个集成测试 `weight_loader_gpu.rs`）；14 个顶层文件合计 3517 行，`lib.rs` 以 15 行声明 13 个按字母序排列的 `pub mod`，无任何条件编译。
- 「模块文件 + 同名目录」是本 crate 唯一的拆分手法，共三处：`ops.rs`+`ops/`（5 个子模块）、`cuda_graph.rs`+`cuda_graph/`（`mod dump;` 在 L15）、`weight_loader.rs`+`weight_loader/`（`mod staging;` 在 L30）；`cuda_graph/dump.rs`+`dump/` 展示了同一惯例的递归嵌套。
- `ops.rs` 的四段式结构（声明 / 自家再出口 L11-L16 / kernels 批量再出口 L17-L95 共 69 条 / traced 换装块 L96-L115 共 10 对）是「门面 + feature 同路径换装」的教科书样本。
- 职责卡三层证据法：lib.rs 清单（宏观）+ 文件头 `//!`（自述，14 个文件中 8 个有）+ 签名与 `pub use`（微观）；证据不足就写「推断，待确认」，诚实记录知识边界。
- 「纯数据结构」（page_pool、kv_pool）与「机制」（ops、cuda_graph、weight_loader）两类模块需要不同的阅读策略：前者逐行精读可纸面推演，后者先抓骨架与调用链。
- 两个 1 行文件（`tensor.rs`、`ffi.rs`）用一行 `pub use` 维持了「模型 crate 只依赖 core」的分层稳定。

## 7. 下一步学习建议

- **下一讲（u1-l3）**：把地图变成可运行的认知——构建与测试。重点对象：`Cargo.toml` 的依赖与 feature 声明（正好解释本讲 `ops.rs` 的 `kernel-call-trace` 从哪来）、`tests/weight_loader_gpu.rs` 这个唯一集成测试为何需要 GPU 与真实权重，以及 `cargo test --release -p pegainfer-core --lib` 与 `--test` 的差别。
- **预习方向（u2-l1）**：顺着 `tensor.rs` 的那行转发，去 `pegainfer-kernels` 里看 `tensor` 模块本体——`Tensor`、`Dtype`、`Storage`、`DeviceContext` 的真实定义就在门面背后。
- **遗留问题**（u1-l1 提出，本讲再次观察到）：`pegainfer-core::kv_pool` 与工作区里独立的 `pegainfer-kv-cache` crate 分工如何？带着职责卡到 u3（状态与装配单元）对照两个 API 面即可回答。
