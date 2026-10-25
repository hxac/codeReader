# pegainfer-kernels 是什么：定位与职责边界

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `pegainfer-kernels` 在 PegaInfer 引擎中拥有哪五类资产、明确排除哪类对象（依据 `KERNELS.md` 的 Scope 段）。
2. 对照 `Cargo.toml` 的 `[features]` 段，把每个 cargo feature 对应到一条模型线，并说出它带来的构建期需求（是否需要 Python / Triton / TileLang / NCCL）。
3. 解释「默认构建零 Python 依赖」这一设计取舍是如何通过 feature 门控实现的。
4. 画出 kernels crate、`pegainfer-core` 与各模型 crate 三者之间的依赖方向与职责分工。

本讲是整本学习手册的第一篇，不要求你写过 CUDA，也不要求你熟悉 PegaInfer——所有背景概念都会在第 2 节解释。

## 2. 前置知识

### 2.1 PegaInfer 是什么

PegaInfer 是一个**纯 Rust + CUDA 的大语言模型（LLM）推理引擎**：没有 PyTorch、没有 Python 推理框架，直接用 Rust 调用 CUDA / cuBLAS / FlashInfer 等 GPU 库，对外提供 OpenAI 兼容的 `/v1/completions` API。整个仓库是一个 cargo workspace，包含前端（`pegainfer-frontend`）、运行时核心（`pegainfer-core`）、每条模型线一个模型 crate（`pegainfer-qwen3`、`pegainfer-kimi-k2`……），以及本讲的主角——底层的 `pegainfer-kernels`。

### 2.2 术语速查

| 术语 | 通俗解释 |
| --- | --- |
| **crate** | Rust 的编译单元，近似「一个包/一个库」。本 crate 名叫 `pegainfer-kernels`。 |
| **cargo feature** | cargo 的编译开关。开启一个 feature 可以额外编译一批模块、启用一批依赖，比如 `--features gemma4` 才会编译 Gemma 4 的专属 kernel。 |
| **CUDA kernel** | 运行在 NVIDIA GPU 上的一段 C/C++ 函数（文件后缀 `.cu`），由 nvcc 编译。CPU 上的 Rust 代码通过驱动 API 把它发射（launch）到 GPU 执行。 |
| **FFI** | Foreign Function Interface，跨语言调用边界。这里特指 Rust 与 C/C++ 之间的 `extern "C"` 函数声明——Rust 侧声明符号，链接到 CUDA 侧编译出的实现。 |
| **AOT** | Ahead-Of-Time，提前编译。与 JIT（运行时即时编译）相对，Triton/TileLang kernel 在**构建期**就被编译成 cubin（GPU 可执行产物）。 |
| **wrapper（包装函数）** | 包在 FFI 调用外面的一层安全 Rust 函数：负责取设备指针、传 CUDA stream、把 C 错误码转成 `Result`。 |
| **paged-KV** | 把注意力缓存（K/V）按「页」分块管理的显存布局技术（vLLM 首创）。本 crate 拥有这套布局的**元数据**定义，但页池的运行时分配不在本 crate。 |
| **bf16 / fp8 (e4m3)** | 半精度 / 8 位浮点格式。推理引擎用低精度换吞吐。 |
| **MoE / EP** | Mixture-of-Experts（混合专家）路由架构；Expert Parallelism（专家并行）是把不同专家放到不同 GPU 上的多机并行方式。 |

### 2.3 一个需要提前知道的细节

[src/lib.rs:1-2](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/lib.rs#L1-L2) 打开了 `generic_const_exprs` 这个**不稳定特性**（feature gate），说明本 crate 需要用 Rust nightly 工具链编译。后续讲座（u2-l2）会看到它服务于常量泛型类型层。

## 3. 本讲源码地图

本讲涉及的关键文件（相对 `pegainfer-kernels/` 目录）：

| 文件 | 作用 | 本讲用途 |
| --- | --- | --- |
| `KERNELS.md` | 全 crate 的**算子索引表**（op 路由表），开头 Scope 段定义职责边界，结尾 Editing Rule 定义维护规则 | 4.1、4.3 的核心文本 |
| `Cargo.toml` | crate 清单：依赖、feature 表、example/test 的 required-features | 4.2 的核心文本 |
| `src/lib.rs` | crate 根，仅 12 行，声明全部顶层模块 | 4.1 的模块清单 |
| `src/ops.rs` | 算子模块的门控与再导出中枢（318 行） | 4.2、4.3 看 feature 如何切分模块 |

顺带会引用两个 crate 外的文件作对照：`pegainfer-server/Cargo.toml`（workspace 默认成员，展示 feature 传导链）和 `pegainfer-core` / `pegainfer-frontend` 中的运行时对象定义（证明 Scope 排除项确实在别处）。目录里的 `build.rs`（2931 行）、`csrc/`、`cubin/`、`tools/` 本讲只点到为止，u1-l3 / u1-l4 会专门拆解。

## 4. 核心概念与源码讲解

### 4.1 crate 定位与职责边界

#### 4.1.1 概念说明

一个推理引擎的「算子」最终都要落到 GPU 上的一段代码。这些 GPU 代码从哪来、Rust 怎么调它们、张量按什么约定在两边传递——这些跨语言的「地基」问题需要一个统一的家。`pegainfer-kernels` 就是这个家。

按 `KERNELS.md` Scope 段的官方表述，本 crate **拥有**五类资产：

1. **CUDA/Triton 构建产物**——由 `build.rs` 在构建期用 nvcc / Triton AOT 编译出的 `.o` / cubin；
2. **FFI 声明**——`src/ffi` 下对每个 CUDA 入口的 `extern "C"` 声明；
3. **kernel ABI 张量助手**——`src/tensor.rs` 等模块，约定张量在 Rust 与 CUDA 之间的内存布局约定；
4. **paged-KV 布局元数据**——`src/paged_kv.rs`，定义 K/V 缓存页内每一行的宽度与排列；
5. **Rust 算子包装层**——`src/ops`，对每个 kernel 的安全 Rust 包装函数。

同时 Scope 明确**排除**：`KvPool`、`PagePool`、`SamplingParams` 这类**运行时策略对象**不归本 crate 管。一句话记忆：**本 crate 管「怎么算」，不管「什么时候算、给谁算」**。

为什么要划这条边界？因为算子是**跨模型复用**的（RMSNorm 对 Qwen3 和 Kimi-K2 长得一样），而调度、页池分配、采样策略是**每个模型运行时自己的事**。把复用层和策略层分开，模型 crate 才能薄。

#### 4.1.2 核心流程

一次算子调用自上而下穿过四层：

```text
模型 crate（如 pegainfer-qwen3）
    │  调用
    ▼
ops wrapper（src/ops/norm.rs 等：取指针 → 传 CUDA stream → 包 Result）
    │  unsafe 调用
    ▼
FFI 声明（src/ffi/*.rs：extern "C" 符号）
    │  链接到
    ▼
CUDA 入口（csrc/**/*.cu：参数检查 → launch __global__ kernel）
```

而构建期流程是：`build.rs` 在 `cargo build` 时把 `csrc/*.cu` 交给 nvcc 编译（feature 决定编译哪些文件、是否追加 Triton/TileLang AOT），产物参与链接，运行时上述四层才能打通。本讲只需记住这两条链的形状，细节分别在 u3（调用链）和 u1-l3 / u1-l4（构建链）展开。

#### 4.1.3 源码精读

**（1）Scope 原文——本 crate 的「宪法」**

[KERNELS.md:1-5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L1-L5) 是整份索引表的头部。第 3 行 Scope 段原文：

> **Scope**: this crate owns CUDA/Triton build output, FFI declarations, kernel ABI tensor helpers, paged-KV layout metadata, and Rust operator wrappers. Runtime policy objects such as `KvPool`, `PagePool`, and `SamplingParams` stay outside this crate.

翻译：本 crate 拥有 CUDA/Triton 构建产物、FFI 声明、kernel ABI 张量助手、paged-KV 布局元数据和 Rust 算子包装层；`KvPool`、`PagePool`、`SamplingParams` 等运行时策略对象留在本 crate 之外。

第 5 行还规定了这份文件的用法——从 `op_id` 出发，跳到 Rust wrapper、FFI 符号和源文件。也就是说 `KERNELS.md` 是**导航表**而非代码，后面每个单元都会反复用它定位源码。

**（2）`src/lib.rs`——五类资产在模块清单上的落点**

[src/lib.rs:4-12](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/lib.rs#L4-L12) 声明了全部顶层模块：

```rust
pub mod ffi;            // FFI 声明（资产 2）
pub mod forward_pass;   // 前向声明 DSL（u2-l4 专题）
pub mod gpu_buffers;    // 随 batch 伸缩的缓冲宏（u2-l2）
pub mod ops;            // Rust 算子包装层（资产 5）
pub mod paged_kv;       // paged-KV 布局元数据（资产 4，u3-l3 专题）
pub mod tensor;         // kernel ABI 张量助手（资产 3，u2-l1 专题）
#[cfg(feature = "tvm-ffi-triton-cubin")]
pub mod triton_cubin;   // TVM FFI cubin 桥（feature 门控，u5-l3）
pub mod typed_ops;      // 常量泛型 GEMM 家族（u2-l2）
```

整个 crate 根只有 12 行：**它是一个「底座」crate，不做任何调度决策**。注意 `triton_cubin` 是唯一带 `#[cfg]` 门控的顶层模块——feature 门控从 crate 根就开始了。

**（3）`Cargo.toml` 依赖段——Rust 侧刻意保持「薄」**

[Cargo.toml:7-13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L7-L13) 列出全部运行时依赖：

```toml
[dependencies]
anyhow = { workspace = true }   # 错误类型
cudarc = { workspace = true }   # CUDA 驱动/运行时的安全 Rust 绑定
half = { workspace = true }     # bf16/f16 类型
log = { workspace = true }      # 日志
serde = { workspace = true }    # 序列化（布局元数据等）
tvm-ffi = { version = "0.1.0-alpha.0", optional = true }  # 仅 TVM 桥 feature 需要
```

只有 5 个必选依赖，且没有任何 Python / PyTorch 相关依赖——重活全部下沉到构建期编译的 CUDA 侧（`build.rs` 与 `build-dependencies` 中的 `cc`、`pegainfer-build`，见 [Cargo.toml:27-29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L27-L29)）。这是「纯 Rust + CUDA」哲学在依赖层面的直接体现。

**（4）排除项的证据——它们确实住在别的 crate**

Scope 说不归本 crate 管的三个对象，实际定义位置（本讲义撰写时用 Grep 验证过）：

- `PagePool`：[pegainfer-core/src/page_pool.rs:7](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L7)
- `KvPool`：[pegainfer-core/src/kv_pool.rs:139](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L139)
- `SamplingParams`：[pegainfer-frontend/src/sampler.rs:2](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-frontend/src/sampler.rs#L2)

两个在运行时核心 crate，一个在服务前端 crate——正对应「运行时策略」的归属。

#### 4.1.4 代码实践

**实践目标**：亲手验证 Scope 段的每一句话都能在源码里找到落点。

**操作步骤**：

1. 打开 `KERNELS.md`，抄下 Scope 段列出的 5 项「拥有」和 3 个「排除」。
2. 对照 [src/lib.rs:4-12](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/lib.rs#L4-L12)，为每个模块标注它对应五类资产中的哪一类（或标注「专题模块」）。
3. 在仓库根目录执行（只读搜索，不修改任何文件）：

   ```bash
   grep -rn "pub struct KvPool\|pub struct PagePool\|pub struct SamplingParams" --include="*.rs" \
     pegainfer-kernels/src/ pegainfer-core/src/ pegainfer-frontend/src/
   ```

**需要观察的现象**：`pegainfer-kernels/src/` 下零命中；命中出现在 `pegainfer-core/src/kv_pool.rs`、`pegainfer-core/src/page_pool.rs` 与 `pegainfer-frontend/src/sampler.rs`。

**预期结果**：Scope 的「排除清单」与代码事实一致。（本讲义撰写时已运行过该搜索并确认上述三个落点；请你复跑一遍加深记忆。）

#### 4.1.5 小练习与答案

**练习 1**：Scope 说本 crate 拥有「paged-KV 布局元数据」，但不拥有 `PagePool`。「布局元数据」和「页池」的区别是什么？

**参考答案**：布局元数据回答「**一页之内**每个 token 的 K/V 值怎么排」——多少列、什么 dtype、读写各用什么 stride（`src/paged_kv.rs` 的 `KvStorage` / `KvFormat`，u3-l3 精读）；页池回答「**运行时**有多少页、分给哪个请求、何时回收」——那是 `pegainfer-core` 的 `PagePool` / `KvPool` 的策略职责。前者是静态约定，后者是动态管理。

**练习 2**：为什么 `src/lib.rs` 里没有 `scheduler`、`config` 之类的模块？

**参考答案**：因为本 crate 的边界就是「算子地基」。配置加载、请求调度、KV 页池分配都是**运行时策略**，Scope 段明确把它们排除在外；这些逻辑住在 `pegainfer-core` 和各模型 crate 里。crate 根只有 12 行恰好是这条边界的直观证据。

---

### 4.2 feature 门控矩阵

#### 4.2.1 概念说明

PegaInfer 支持七条以上模型线（Qwen3、Qwen3.5/Qwen3.8、DeepSeek-V2-Lite、Gemma 4、Kimi-K2、GLM5.2、Kimi-K3），每条线的专属 kernel（Marlin INT4、TileLang 生成码、DeepEP 多机通信……）编译成本差异巨大，有的还需要构建期 Python。如果把所有模型线的 kernel 都编进默认构建，默认用户就要背上 Python/Triton/NCCL 这些与己无关的依赖。

解法是 cargo feature 门控：**每条模型线一个 feature，默认一个都不开**。于是「默认构建 = 纯 Rust + CUDA、零 Python」成为可能——这正是仓库宣称的核心卖点。

需要注意一个容易混淆的点：**workspace 的默认 feature 是 `qwen3`，但那是 `pegainfer-server`（服务二进制）的 feature 表，不是本 crate 的**。本 crate 自己的 `default = []`。

#### 4.2.2 核心流程

一个 feature 开关会在三个地方同时生效：

```text
cargo build --features <F>
    │
    ├─► build.rs：决定 nvcc 编译哪些 csrc/<line>/*.cu、
    │   是否运行 Triton/TileLang AOT 代码生成（u1-l3 / u1-l4 专题）
    │
    ├─► src/ops.rs：#[cfg(feature = "F")] 门控 mod 声明与 pub use，
    │   决定哪些 Rust wrapper 被编译进 crate
    │
    └─► Cargo.toml：[[example]] / [[test]] 的 required-features，
        决定哪些示例与测试目标存在
```

而 feature 沿依赖链**向上游传导**：`pegainfer-server` 的 `qwen3` feature 拉入 `pegainfer-qwen3` 模型 crate，后者再依赖 `pegainfer-kernels`（不带任何模型 feature，因为 Qwen3-4B 全用共享 kernel）。

#### 4.2.3 源码精读

**（1）feature 总表**

[Cargo.toml:31-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L31-L50)：

```toml
[features]
default = []
tvm-ffi-triton-cubin = ["dep:tvm-ffi", "qwen35"]
# Qwen3.5 Triton AOT kernels (GDR chunkwise prefill) — the only feature that
# needs Python + Triton at build time.
deepseek-v2-lite = []
# Gemma 4: NVFP4 dequantization, whose conversion intrinsics need CUDA >= 12.8,
# and the TileLang-generated hd512 global-attention prefill.
gemma4 = []
qwen35 = []
# Qwen4 line (Qwen3.8-Flash-Next): the QSA token-indexer block-scoring kernels
# and the grouped (1+weight) RMSNorm.
qwen4 = []
# Shared MoE/MLA third-party substrate: DeepEP, DeepGEMM, and FlashMLA.
glm52 = ["moe"]
# Kimi K3: TileLang-generated kernels, FP8xFP4 masked grouped GEMM, and the
# MegaMoE fused EP kernel (NVLink peer access, no DeepEP/NCCL substrate).
k3 = []
kimi-k2 = ["moe"]
moe = []
```

逐条解读（结合仓库根 CLAUDE.md 的环境变量说明）：

| feature | 对应模型线 / 用途 | 依赖 feature | 构建期额外需求 |
| --- | --- | --- | --- |
| `default`（空） | 只编译共享算子面 + 基础 nvcc 流水线 | — | 无 Python；需 CUDA 工具链 |
| `qwen35` | Qwen3.5 / Qwen3.8 混合注意力线 | — | **唯一需要构建期 Python + Triton 的 feature**（注释原文；环境变量 `PEGAINFER_TRITON_PYTHON` 指定解释器） |
| `tvm-ffi-triton-cubin` | TVM FFI cubin 桥（u5-l3 的示例） | `dep:tvm-ffi` + `qwen35` | 同 `qwen35`，另拉 `tvm-ffi` alpha 依赖 |
| `deepseek-v2-lite` | DeepSeek-V2-Lite（MoE + EP，2 GPU） | — | kernels 侧无额外构建需求 |
| `gemma4` | Gemma 4 | — | NVFP4 反量化 intrinsics 需 **CUDA ≥ 12.8**（注释原文）；hd512 全局注意力由 TileLang 生成（其构建期是否需要 Python：**待确认**，注释未说明） |
| `qwen4` | Qwen4 线（Qwen3.8-Flash-Next）：QSA indexer + grouped RMSNorm | — | 注释未列额外需求 |
| `glm52` | GLM5.2 | `moe` | DeepEP/DeepGEMM/FlashMLA 底座；`PEGAINFER_NCCL_ROOT`（NCCL ≥ 2.30.4）；sm_90a 上 TileLang sparse-MLA 用 `PEGAINFER_TILELANG_PYTHON` |
| `kimi-k2` | Kimi-K2 | `moe` | 同 `moe` 底座 |
| `k3` | Kimi-K3 | —（**明确不依赖** `moe`） | 需要 DeepGEMM 子模块（见下）；TileLang kernel 有 generate / pre-generated / stub 三档构建层级 |
| `moe` | 共享 MoE/MLA 第三方底座 | — | DeepEP shim → `PEGAINFER_NCCL_ROOT` |

两点值得圈注：

- `glm52` 和 `kimi-k2` 都拉 `moe`，因为它们共用 DeepEP/DeepGEMM/FlashMLA 这套多机 MoE 底座；而 [KERNELS.md:168-171](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L168-L171) 明说 k3 「does not depend on the `moe` substrate — there is no DeepEP/NCCL requirement yet」，它的 MegaMoE 走 NVLink peer access，绕开了集合通信库。
- `tvm-ffi-triton-cubin` 展示了 feature 组合语法：既启用可选依赖（`dep:tvm-ffi`），又联动另一个 feature（`qwen35`）。

**（2）`default = []` 与服务器的传导链**

本 crate 默认不开启任何模型 feature（[Cargo.toml:32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L32)）。而 workspace 的默认构建目标 `pegainfer-server` 声明：

> [pegainfer-server/Cargo.toml:34-42](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-server/Cargo.toml#L34-L42) —— `default = ["qwen3"]`，注释：Qwen3-4B is the default model line: pure Rust + CUDA, no Python at build time.

注意一个精妙事实：**本 crate 的 feature 表里根本没有 `qwen3`**。Qwen3-4B 用的全是共享算子——对照 [KERNELS.md:17-37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L17-L37) 的 Qwen3-4B 表段，所有 `qwen3_4b.*` 条目的 Source 列都指向 `csrc/shared/`。所以「Qwen3 作默认线」不需要 kernels 侧任何门控，这正是共享层与专属层划分带来的红利。

**（3）`src/ops.rs` 的门控现场**

[src/ops.rs:3-49](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L3-L49) 是模块声明区，feature 在这里切分编译面。几个代表性片段：

```rust
#[cfg(feature = "moe")]
mod deepep;                        // L4-5：moe 底座独享
...
mod elementwise;                   // L22：共享算子，无条件编译
#[cfg(feature = "gemma4")]
mod gemma4;                        // L24-25：gemma4 独享
...
#[cfg(any(feature = "gemma4", feature = "kimi-k2"))]
mod marlin_face;                   // L42-43：两个模型线共用的 Marlin 门面
mod norm;                          // L44：共享
#[cfg(feature = "qwen35")]
mod qwen35;                        // L45-46
#[cfg(feature = "qwen4")]
mod qwen4;                         // L47-48
```

三种门控形态都在：单 feature 门（`deepep`）、**或门**（`marlin_face` 被 gemma4 与 kimi-k2 复用，u7-l1 专题）、无门（共享模块）。再导出区同理：[src/ops.rs:51-62](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L51-L62) 的 attention 系列不带任何 `#[cfg]`（共享面），而 [src/ops.rs:63-64](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L63-L64) 的 `pub use deepep::DeepEp;` 只在 `moe` 下存在，[src/ops.rs:73-78](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L73-L78) 的 GLM5.2 专属 EP ABI 绑定则只在 `glm52` 下出现。

**（4）feature 还门控 example 与 test 目标**

[Cargo.toml:52-66](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L52-L66)：`triton_cubin_tvm_ffi` 示例要求 `tvm-ffi-triton-cubin`；两个 `hd512_*_bench` 示例要求 `gemma4`；`grouped_rmsnorm_equivalence` 测试要求 `qwen4`。不开对应 feature 时 `cargo run --example hd512_prefill_bench` 会直接报「required features」错误——这是初学者最常撞到的第一堵墙。

#### 4.2.4 代码实践

**实践目标**：用 cargo 自带工具验证 feature 与依赖的联动关系。

**操作步骤**：

1. 在仓库根目录运行：

   ```bash
   cargo tree -p pegainfer-kernels
   ```

   观察输出：Rust 侧依赖树只有 anyhow / cudarc / half / log / serde 及其传递闭包。

2. 再运行（`-e features` 让 feature 出现在树里）：

   ```bash
   cargo tree -p pegainfer-kernels -e features -f "{p} {f}" --features glm52
   ```

3. 换成 `--features kimi-k2` 与 `--features k3` 各跑一次，对比。

**需要观察的现象**：第 2 步中 `moe` 应作为被启用的 feature 出现（因为 `glm52 = ["moe"]`），`tvm-ffi` 不应出现；第 3 步 `kimi-k2` 同样点亮 `moe`，而 `k3` 不点亮任何其它 feature。

**预期结果**：feature 依赖边（`glm52 → moe`、`kimi-k2 → moe`、`k3 → 无`）与 Cargo.toml 声明一致；同时确认默认树里没有 `tvm-ffi`（它是 optional 依赖）。命令本身是标准 cargo 用法，具体输出格式随 cargo 版本略有差异——**待本地验证**（无 GPU 环境也能运行 `cargo tree`，它只解析依赖不编译）。

#### 4.2.5 小练习与答案

**练习 1**：同事说「我想跑 `cargo run --example hd512_prefill_bench` 但报错了」，最可能的原因和修法是什么？

**参考答案**：该示例声明了 `required-features = ["gemma4"]`（[Cargo.toml:56-58](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L56-L58)），需要改为 `cargo run --release --features gemma4 --example hd512_prefill_bench`。

**练习 2**：为什么 `qwen35` 被设计成「唯一需要构建期 Python + Triton 的 feature」，而不是像其它 feature 一样默认可用？

**参考答案**：Qwen3.5 的 chunk-wise GDR prefill kernel 用 Triton 编写，构建期要由 Python 解释器运行 AOT 生成（工具链见 `tools/triton/`，u5-l2 专题）。若并入默认构建，每个用户（包括只用 Qwen3-4B 的人）都得装 Python + Triton，破坏「纯 Rust + CUDA、零 Python」的默认承诺。feature 门控把这份成本精确摊到需要它的模型线上。

**练习 3**：`moe` feature 存在的意义是什么？为什么不把 DeepEP 等底座直接挂进每个模型 feature？

**参考答案**：`moe` 是**共享底座** feature：`glm52` 与 `kimi-k2` 都需要 DeepEP/DeepGEMM/FlashMLA，挂成公共 feature 避免重复声明，也让 `cargo tree -e features` 能清楚显示「哪些线站在同一底座上」；而 k3 不拉 `moe`（它的 MegaMoE 用 NVLink peer access 绕开了 DeepEP/NCCL），这个差异本身就是一条架构信息。

---

### 4.3 与 pegainfer-core 及模型 crate 的分工

#### 4.3.1 概念说明

整个引擎对 GPU 算力的使用分三层：

| 层 | crate | 职责 | 典型内容 |
| --- | --- | --- | --- |
| **算子层** | `pegainfer-kernels`（本 crate） | 可复用的「怎么算」 | kernel、FFI、张量约定、布局元数据 |
| **运行时层** | `pegainfer-core` | 共享的「怎么调度」 | `KvPool`、`PagePool`、共享 op 高层包装、CUDA Graph 约定 |
| **模型层** | `pegainfer-qwen3` 等每线一个 | 每个模型的 DAG 与权重 | 配置加载、权重布局、层间拼装、每线 scheduler |

`KERNELS.md` 结尾的 Editing Rule 把这条分层原则写成了硬性规则，是理解本 crate 边界的第二份关键文本。

#### 4.3.2 核心流程

依赖方向（箭头 = 「依赖」）：

```text
pegainfer-server ──► pegainfer-qwen3 / qwen35 / kimi-k2 / glm52 / k3 / gemma4 / dsv2-lite（模型层）
                          │
                          ├────────► pegainfer-core（运行时层）
                          │              │
                          ▼              ▼
                    pegainfer-kernels（算子层，本讲义主角）
```

模型 crate 同时依赖 core 与 kernels：模型 DAG 的一步通常是「core 给调度/池化，kernels 给具体算子」。此外 `pegainfer-kv-cache`、`pegainfer-sample`、`pegainfer-bench` 也直接依赖 kernels（本讲义撰写时用 Grep 统计，共 **11 个成员 crate** 的 Cargo.toml 引用了 `pegainfer-kernels`）。

#### 4.3.3 源码精读

**（1）Editing Rule——分层原则的成文法**

[KERNELS.md:238-246](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L238-L246)：

> When adding or replacing a kernel used by Qwen3-4B, Kimi-K2, GLM5.2, or K3, update this routing table.
>
> Do not add model-specific machine-readable manifests here. The kernels crate owns reusable operator implementations; model crates should own model DAG metadata. If a Qwen3-4B manifest becomes useful for tracing or simulation, put it beside the Qwen3-4B model crate and generate or validate it from code.

两条规则：① 新增/替换 kernel 必须同步更新路由表（u4-l2 会把它变成肌肉记忆）；② **本 crate 只放可复用算子实现，模型 DAG 元数据归模型 crate**——哪怕它看起来「放在 kernel 旁边更方便」。这是防止底座 crate 被模型细节污染的护栏。

**（2）依赖边的源码证据**

- 运行时层依赖算子层：[pegainfer-core/Cargo.toml:23](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/Cargo.toml#L23) —— `pegainfer-kernels = { workspace = true }`。
- 模型层同时依赖两者：[pegainfer-qwen3/Cargo.toml:19-22](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/Cargo.toml#L19-L22) —— `pegainfer-core` 与 `pegainfer-kernels` 并列。

**（3）「共享面」的样子：无条件再导出**

[src/ops.rs:51-62](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L51-L62) 中 attention 系列、[src/ops.rs:99-137](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L99-L137) 中 elementwise/embedding 系列都没有任何 `#[cfg]`——这就是任何 feature 组合下都存在的**共享算子面**，Qwen3-4B 整条推理链（见 [KERNELS.md:21-37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L21-L37) 的表）都由它支撑。对照表格末列可以看到，同一个 `rms_norm_batched_cuda` 既服务 Qwen3（`qwen3_4b.norm.rms_batch`）也服务 Kimi-K2（[KERNELS.md:59](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L59) `kimi_k2.norm.rms_batch`）——「可复用算子」四个字的具象化。

**（4）一条重要的阅读纪律：索引表会漂移，以代码为准**

[KERNELS.md:36-37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L36-L37) 的 wrapper 列写着 `pegainfer-core::ops::select_batch_tokens_into`，但本讲义撰写时用 Grep 全仓库搜索，**当前 HEAD 的源码里已经不存在这个函数**——它是文档滞后于代码的痕迹。结论：把 `KERNELS.md` 当导航入口（它的文件/符号/形状注释仍然极有价值），但每个关键符号都要用 `Grep` 落到真实源码再下结论。本手册后续所有讲义都遵守这条纪律。

#### 4.3.4 代码实践

**实践目标**：亲手复现「谁依赖 kernels」的依赖图，并体验一次「文档 vs 代码」核对。

**操作步骤**：

1. 在仓库根目录运行：

   ```bash
   grep -l "pegainfer-kernels" */Cargo.toml
   ```

2. 把输出列表与 4.3.2 的依赖图对照，逐个标注每个 crate 属于哪一层（模型层 / 运行时层 / 工具层）。
3. 再运行一次「找漂移」搜索：

   ```bash
   grep -rn "select_batch_tokens" --include="*.rs" .
   ```

**需要观察的现象**：第 1 步应列出 11 个成员 crate（`pegainfer-core`、`pegainfer-qwen3`、`pegainfer-qwen35`、`pegainfer-deepseek-v2-lite`、`pegainfer-gemma4`、`pegainfer-glm52`、`pegainfer-k3`、`pegainfer-kimi-k2`、`pegainfer-kv-cache`、`pegainfer-sample`、`pegainfer-bench`；glob `*/Cargo.toml` 不匹配仓库根，根 Cargo.toml 里另有 `[workspace.dependencies]` 的 path 声明，成员 crate 的 `{ workspace = true }` 都指向它）；第 3 步在 `.rs` 源码中零命中——该符号只残留在 `KERNELS.md` 的表格行里。

**预期结果**：依赖图与 Editing Rule 描述的分层一致；`select_batch_tokens_into` 确认为文档残留。两条均已在撰写本讲义时验证，请你复跑确认（grep 不修改任何文件）。

#### 4.3.5 小练习与答案

**练习 1**：如果 Qwen3-4B 需要一张「层间执行顺序」的机器可读清单（DAG manifest），应该放在哪个 crate？依据是什么？

**参考答案**：放在 `pegainfer-qwen3` 模型 crate 旁边。依据 [KERNELS.md:243-246](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L243-L246)：kernels crate 只拥有可复用算子实现，模型 DAG 元数据归模型 crate，且应从代码生成或校验。

**练习 2**：`pegainfer-core` 依赖 `pegainfer-kernels`，反过来行不行？为什么？

**参考答案**：不行。kernels 是最底层算子地基，不依赖任何 pegainfer 运行时 crate（它的依赖只有 anyhow/cudarc/half/log/serde，见 4.1.3）。若反过来，core 的运行时类型会渗入算子层，`KvPool` 这类 Scope 明确排除的对象就会被拖进来，破坏「可复用、跨模型」的定位，并极可能造成循环依赖。

**练习 3**：同一个 `rms_norm_batched_cuda` FFI 符号出现在 `qwen3_4b.norm.rms_batch` 和 `kimi_k2.norm.rms_batch` 两个 op_id 下（[KERNELS.md:24](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L24)、[KERNELS.md:59](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L59)）。表格里 Kimi 那行特别注明「This is not a fallback path」，这句话想强调什么？

**参考答案**：强调这不是「Qwen3 主路径 + Kimi 借用降级路径」的关系，而是**同一个生产级共享实现**被两条模型线平等使用——RMSNorm 的数学与模型无关，参数化 wrapper（任意 hidden 维度）让 7168（Kimi）和 2560（Qwen3-4B）走同一份代码。这正是「可复用算子层」设计目标的直接证据。

---

## 5. 综合实践

**任务**：制作一份《feature 门控与构建期需求速查笔记》，并用 cargo tree 验证。这份笔记将陪伴你读完整个手册，以后每次构建报「required features」错误时先查它。

**步骤**：

1. **读两段原文**：通读 [KERNELS.md:1-5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L1-L5)（Scope）与 [Cargo.toml:31-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L31-L50)（features）。
2. **填表**：按下面的模板整理（答案可对照 4.2.3 的表自查，但请先独立填写再核对）：

   | feature | 模型线 | 需要 Python？ | 需要 Triton？ | 需要 TileLang？ | 需要 NCCL/其它 | 相关环境变量 |
   | --- | --- | --- | --- | --- | --- | --- |
   | `qwen35` | … | … | … | … | … | … |
   | （逐行补全 9 个 feature） | | | | | | |

3. **验证依赖关系**：

   ```bash
   cargo tree -p pegainfer-kernels                     # 默认面：确认薄依赖、无 tvm-ffi
   cargo tree -p pegainfer-kernels -e features --features glm52    # 观察 moe 被点亮
   cargo tree -p pegainfer-kernels -e features --features tvm-ffi-triton-cubin  # 观察 qwen35+dep:tvm-ffi 联动
   ```

4. **验证传导链**：打开 [pegainfer-server/Cargo.toml:34-42](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-server/Cargo.toml#L34-L42)，确认 `default = ["qwen3"]`，再回到 kernels 的 Cargo.toml 确认**没有** `qwen3` feature，用一句话在笔记里解释为什么不矛盾（提示：Qwen3-4B 全走共享面，见 4.2.3 第（2）点）。

**观察点与预期结果**：

- 默认 `cargo tree` 输出的依赖树极小（≤ 5 个直接依赖）；
- `--features glm52` / `kimi-k2` 时 `moe` 出现在 feature 列表；`--features k3` 时不出现；
- `--features tvm-ffi-triton-cubin` 时 `qwen35` 与 `tvm-ffi` 同时出现；
- 三个 `cargo tree` 命令均**不需要 GPU**，任何装了 Rust（nightly 工具链）的环境都能跑。具体输出排版随 cargo 版本不同，**待本地验证**后把实际输出贴进你的笔记。

## 6. 本讲小结

- `pegainfer-kernels` 是引擎的**算子地基**：拥有 CUDA/Triton 构建产物、FFI 声明、kernel ABI 张量助手、paged-KV 布局元数据、Rust 算子包装层五类资产；`KvPool` / `PagePool` / `SamplingParams` 等运行时策略对象明确排除在外。
- crate 根只有 12 行、Rust 依赖只有 5 个：一切重活在构建期 CUDA 侧（`build.rs` + `csrc/`）。
- 每条模型线一个 cargo feature，`default = []`；`qwen35` 是唯一需要构建期 Python + Triton 的 feature，由此保住「默认构建纯 Rust + CUDA、零 Python」的承诺。
- `glm52` 与 `kimi-k2` 共用 `moe` 底座（DeepEP/DeepGEMM/FlashMLA），`k3` 特立独行地不依赖它（NVLink peer access 路线）。
- 分层铁律写在 KERNELS.md Editing Rule：kernels 管可复用算子，模型 DAG 元数据归模型 crate；11 个成员 crate 依赖本 crate。
- 阅读纪律：`KERNELS.md` 是导航表而非代码，遇到关键符号先用 Grep 落到源码再采信（本讲已示范一处真实的文档漂移）。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：目录结构导览——把 `src` 七个模块、`csrc` 按模型线的分组、`tools` 生成器、`cubin` 预编译资产、`examples` 与 `tests` 的布局走一遍，本讲的抽象定位会变成具体的文件坐标。
- **提前浏览**：用 `KERNELS.md` 的分节标题（Shared / Qwen3-4B / Kimi-K2 / GLM5.2 / K3 各段）扫一遍每条模型线的 kernel 面，感受「共享面 + 专属面」的体量对比；u1-l5 会正式教你怎么高效查这张表。
- **暂不建议**：现在就扎进 `build.rs`（2931 行）。等 u1-l3 / u1-l4 用两讲的篇幅拆解 nvcc 流水线与 AOT 代码生成后再读，会顺很多。
