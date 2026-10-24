# ffi.rs：Rust 与 CUDA 的边界

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 Rust 调用 CUDA kernel 的「声明 → 编译 → 加载 → 调用」四步闭环，并说出每一步分别发生在哪个文件、哪个构建阶段。
2. 解释 `pegainfer-core/src/ffi.rs` 与 `tensor.rs` 为什么都只是一行 `pub use`（单一事实源、编译边界收敛）。
3. 理解 FFI 边界为什么收敛在 core 与 kernels 两个 crate：`unsafe extern "C"` 声明只写在 `pegainfer-kernels/src/ffi/` 一处，模型 crate 从不自己声明 CUDA 符号。
4. 说出这条边界的错误契约：`CUresult` 返回码 + `-1` 哨兵 + `pegainfer_kernels_last_error()` 文本。

本讲承接 u2-l1（tensor 门面），是通往 u2-l3（ops 算子封装）的中间一环：ops 封装的地基正是本讲的 ffi 声明。

## 2. 前置知识

- **FFI（Foreign Function Interface，外部函数接口）**：让 Rust 代码调用 C/C++ 编译产物中符号的机制。Rust 侧用 `extern "C"` 块声明「存在一个这样签名的 C 函数」，链接器负责把声明和实现缝在一起。
- **ABI（Application Binary Interface）**：两种语言在机器码层面「怎么传参数、怎么返回值」的约定。双方必须一致，否则就是未定义行为。本仓库的约定是：浮点半精度用 `Half = u16`（与 CUDA `half` 布局相同）、流句柄用 `CUstream`、返回码用 `CUresult`——全部来自 cudarc 对 CUDA 驱动 API 类型的再暴露。
- **`unsafe extern "C"`**：Rust 2024 版语法（core 是 edition 2024，见 u1-l3）。在 2024 版中 `extern` 块本身标记 `unsafe`，提醒你「声明的签名是否与实现一致」由你负责，编译器无法检查。
- **静态链接 vs 运行时加载**：CUDA 代码可以编译成 `.o` 目标文件静态链进程序（符号在构建期解析），也可以编译成 cubin/PTX 在运行时由驱动加载。本仓库的 csrc 路线是前者；Triton/TileLang AOT 生成的内核走 `triton_cubin` 模块的运行时路线（本讲不展开）。
- **build script（build.rs）**：Cargo 在编译 crate 前先编译并运行的脚本，用 `println!("cargo:rustc-link-lib=...")` 这类指令反过来告诉链接器怎么链。
- **再出口门面（re-export facade）**：一个模块自己不定义任何东西，只 `pub use` 别处的符号，让外界通过稳定路径访问。u2-l1 已在 tensor.rs 上见过。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `pegainfer-core/src/ffi.rs` | 1 | 门面：再出口 kernels 的 ffi 模块 |
| `pegainfer-core/src/tensor.rs` | 1 | 门面：再出口 kernels 的 tensor 模块（u2-l1 已精读） |
| `pegainfer-core/src/lib.rs` | 15 | 声明 `pub mod ffi;` 等公开面 |
| `pegainfer-kernels/src/ffi.rs` | 51 | FFI 真身聚合器：定义 `Half`，按 feature 挂载子模块并统一再出口 |
| `pegainfer-kernels/src/ffi/shared.rs` | >1000 | 跨模型共享符号：rms_norm 家族、cuBLAS/cudaSetDevice 生命周期、错误出口 |
| `pegainfer-kernels/src/ffi/qwen4.rs` | 46 | qwen4 专属符号声明（QSA indexer、分组 RMSNorm） |
| `pegainfer-kernels/build.rs` | ~2900 | 编译闭环：收集 csrc、跑 nvcc、`ar` 归档、下发链接指令 |
| `pegainfer-kernels/csrc/qwen4/*.cu` | — | 声明对应的 CUDA C++ 实现 |
| `pegainfer-kernels/tests/ffi_exception_catch.rs` | 86 | FFI 错误契约的活样本（C++ 异常 → -1 哨兵 + 消息） |

注意：本讲主角是 core 的 `ffi.rs`，但它的「真身」在 pegainfer-kernels。我们要跨过 crate 边界看两边的对接，这正是理解「core 拥有门面、kernels 拥有实现」分层（u1-l1 的遗留问题）的最好样本。

## 4. 核心概念与源码讲解

### 4.1 一行再出口门面：ffi.rs 与 tensor.rs 的孪生结构

#### 4.1.1 概念说明

core 的 `ffi.rs` 全文只有一行：

```rust
pub use pegainfer_kernels::ffi::*;
```

它与 `tensor.rs`（`pub use pegainfer_kernels::tensor::*;`）是一对孪生门面。动机有两个：

1. **单一事实源（single source of truth）**：CUDA 符号的 Rust 签名只维护在 kernels 一处。假如每个模型 crate 都自己写一份 `extern "C"` 声明，签名漂移（比如参数从 `i32` 改成 `i64`）时只改了一处、别处悄悄错位——这类错误编译器查不出来，运行时才是未定义行为。声明收敛在 kernels，改一处即全局生效。
2. **编译边界收敛**：与 CUDA/cudarc/nvcc 的纠缠全部下沉到 kernels 这一个 crate（它的 build.rs 才跑 nvcc，见 4.3）。core 与模型 crate 只需要「能引用这些符号」，不需要理解它们怎么被编译出来。

#### 4.1.2 核心流程

```text
模型 crate 代码
    │  use pegainfer_core::ffi;          ← 稳定路径
    ▼
pegainfer-core::ffi   （src/ffi.rs，一行 pub use）
    │  pub use pegainfer_kernels::ffi::*
    ▼
pegainfer_kernels::ffi （src/ffi.rs，真身聚合器）
    │  pub use shared::*;  pub use qwen4::*;  ...
    ▼
unsafe extern "C" 声明（符号在链接期解析到 libkernels_cuda.a）
```

glob 再出口链不改变符号本身——`pegainfer_core::ffi::cublas_init` 与 `pegainfer_kernels::ffi::cublas_init` 是同一个函数。门面改变的是**依赖关系**：模型 crate 只依赖 core 时也能拿到全部 FFI 符号。

#### 4.1.3 源码精读

core 的 ffi 门面（全文就这一行）：

- [pegainfer-core/src/ffi.rs:L1-L1](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/ffi.rs#L1-L1)——把 kernels 的 ffi 模块全部符号原样再出口，core 自己零定义、零改动（本轮 HEAD 区间 kernels 侧新增了 qwen4 符号，这行门面无需任何变化，这就是单一事实源的收益）。

tensor 门面与之完全同构：

- [pegainfer-core/src/tensor.rs:L1-L1](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/tensor.rs#L1-L1)——u2-l1 精读过的张量门面，对照可见两个门面的写法一字不差。

模块在 lib.rs 中的声明位置：

- [pegainfer-core/src/lib.rs:L7-L7](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/lib.rs#L7-L7)——`pub mod ffi;`（第 7 行）；
- [pegainfer-core/src/lib.rs:L13-L13](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/lib.rs#L13-L13)——`pub mod tensor;`（第 13 行）。两者在 13 个 `pub mod` 中相邻成组，正是 u1-l1 说的「tensor/ffi 门面组」。

一个容易踩的坑：core 源码里 grep `ffi` 还会命中另外几处——`use std::ffi::CStr`（[cuda_graph/dump.rs:L2](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/cuda_graph/dump.rs#L2-L2)）和 `use std::ffi::{CStr, c_char}`（[cpu_topology.rs:L3-L4](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/cpu_topology.rs#L3-L4)）。那是 **Rust 标准库的 `std::ffi` 模块**（C 字符串/原生字符工具），与本讲的 CUDA FFI 门面毫无关系，只是名字撞车。检索时务必用 `crate::ffi` 而不是裸的 `ffi`。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认两个门面「真的只有一行」，并验证 core 内部没有任何 `crate::ffi` 消费点。
2. **操作步骤**：
   - 在 `pegainfer-core/` 目录下执行 `cat src/ffi.rs src/tensor.rs`，确认两者各只输出一行 `pub use`。
   - 执行 `grep -rn "crate::ffi::" src/`，观察返回为空（唯一能命中的是 `pub use` 那行所在的 `src/ffi.rs` 自身）。
   - 执行 `grep -rn "std::ffi" src/`，对比命中的两处标准库引用，体会名字撞车。
3. **需要观察的现象**：`crate::ffi::` 在 core 内部零命中——这个门面不是给 core 自己用的，是给模型 crate 用的（4.3.3 给出真实消费点）。
4. **预期结果**：core 内零消费；`std::ffi` 命中 cuda_graph/dump.rs 与 cpu_topology.rs 两处。本讲写作时已按上述命令验证过，读者复跑应得到同样结果（若仓库已演进，以实际输出为准）。

#### 4.1.5 小练习与答案

**练习 1**：如果不经门面、让每个模型 crate 直接 `use pegainfer_kernels::ffi::*`，功能上可行吗？为什么仓库仍然保留 core 门面？

**答案**：功能上可行——事实上部分模型 crate（如 kimi-k2，见 4.3.3）确实直接用 `pegainfer_kernels::ffi`。保留 core 门面的意义在于：依赖 core 的模型 crate 不必额外依赖 kernels 就能拿到符号；FFI 访问路径统一为一个「官方入口」，将来若要给符号加包装、审计或按 feature 裁剪，改门面一处即可，消费方代码不动。

**练习 2**：`pub use X::*` 与 `pub use X` 有什么区别？ffi.rs 用的是哪种？

**答案**：`pub use X` 是把模块 `X` 这个名字本身再出口（调用方要写 `ffi::shared::...`）；`pub use X::*` 是把 `X` 内部的全部**公开项**平铺再出口（调用方直接写 `ffi::cublas_init`）。ffi.rs 用的是后者，因此门面路径与 kernels 内部路径符号级一致。

### 4.2 kernels 侧 FFI 真身：pegainfer_kernels::ffi

#### 4.2.1 概念说明

门面背后是真身：`pegainfer-kernels/src/ffi.rs` 是一个聚合器，它做三件事——

1. 定义跨子模块共享的 ABI 类型 `Half = u16`；
2. 按 cargo feature 声明（`mod`）各模型专属子模块——`qwen4`、`qwen35`、`kimi`、`glm52`、`k3`、`gemma4`、`deepseek_v2_lite`、`deepep` 各自挂在自己的 feature 下，`shared` 与 `lora` 无条件编译；
3. 把每个子模块的内容 glob 再出口，保持公开路径稳定：`pegainfer_kernels::ffi::<符号>`。

于是「加一个模型的内核」= 在 kernels 加一个 ffi 子模块 + 一个 feature 门控 + build.rs 里一个源文件过滤规则，core 与模型 crate 均无需感知编译细节。

#### 4.2.2 核心流程

feature 到符号的挂载流程：

```text
Cargo.toml（kernels）开启 feature，如 qwen4
    ▼
src/ffi.rs:  #[cfg(feature = "qwen4")] mod qwen4;        ← 子模块被编译
            #[cfg(feature = "qwen4")] pub use qwen4::*;  ← 符号进入公开面
    ▼
core 的 ffi 门面（无条件 pub use *）原样透出
    ▼
build.rs: is_qwen4_source(...) 过滤 csrc/qwen4/*.cu 参与编译（见 4.3）
```

注意不对称性：Rust 侧声明与 CUDA 侧编译由**同一个 feature** 同步开关——只开声明不编译会链接失败，只编译不开声明则符号无人引用。

#### 4.2.3 源码精读

真身聚合器的头注释与 ABI 类型：

- [pegainfer-kernels/src/ffi.rs:L1-L2](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi.rs#L1-L2)——头注释言明设计：「FFI surface 按 owning model 拆分，公开路径 `pegainfer_kernels::ffi::<symbol>` 经再出口保持不变」。
- [pegainfer-kernels/src/ffi.rs:L4-L5](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi.rs#L4-L5)——`pub type Half = u16;`：与 CUDA `half` 位布局相同的 16 位浮点，所有子模块共享的 ABI 类型。

feature 门控的模块声明与再出口：

- [pegainfer-kernels/src/ffi.rs:L7-L28](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi.rs#L7-L28)——`mod deepep;`（`moe` feature）到 `mod shared;`（无条件）共 12 个子模块声明，每个前面挂 `#[cfg(feature = ...)]`；其中 `qwen4` 的声明在 L26-L27。
- [pegainfer-kernels/src/ffi.rs:L29-L50](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi.rs#L29-L50)——与声明一一对应的 `pub use <mod>::*;` 再出口清单；`lora`（L45）与 `shared`（L50）无门控，其余随 feature。

qwen4 子模块：一份典型的 `unsafe extern "C"` 声明（本轮 HEAD 新增的样本）：

- [pegainfer-kernels/src/ffi/qwen4.rs:L9-L16](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi/qwen4.rs#L9-L16)——`unsafe extern "C"` 块开头，先 `use cudarc::driver::sys::{CUresult, CUstream}` 定下 ABI 的返回码与流句柄类型。
- [pegainfer-kernels/src/ffi/qwen4.rs:L17-L32](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi/qwen4.rs#L17-L32)——`qwen4_qsa_indexer_block_scores_cuda`：入参全是裸指针（`*const Half`/`*mut Half`）加维度整数，末尾固定 `stream: CUstream`，返回 `CUresult`。doc 注释特别警告「几何已编译进内核」（fused 行宽 640、cos/sin 表步长 64），布局不符的调用方是在读错行——这是 FFI 无编译期检查的典型代价。
- [pegainfer-kernels/src/ffi/qwen4.rs:L36-L45](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi/qwen4.rs#L36-L45)——`rms_norm_grouped_offset_cuda`：分组 `(1+weight)` RMSNorm，doc 指向 `ops::rms_norm_grouped_offset_batch_into` 查看契约（ops 包装层是 u2-l3 的主题）。

shared 子模块：跨模型共享的符号池（节选）：

- [pegainfer-kernels/src/ffi/shared.rs:L6-L7](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi/shared.rs#L6-L7)——注释点明「所有模型共享的内核（CUDA / cuBLAS / FlashInfer）」。
- [pegainfer-kernels/src/ffi/shared.rs:L22-L29](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi/shared.rs#L22-L29)——`rms_norm_cuda`：注意它**没有返回值**（`-> ()`），与返回 `CUresult` 的邻居并存——错误契约并不统一，调用方要逐个看签名。
- [pegainfer-kernels/src/ffi/shared.rs:L465-L468](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi/shared.rs#L465-L468)——cuBLAS/cudaSetDevice 生命周期四件套：`cublas_init`、`cublas_activate_device_handles`、`cublas_destroy`、`cuda_set_device`。这正是模型 crate 经 core 门面最常调的一组（4.3.3 的消费点全是它们）。
- [pegainfer-kernels/src/ffi/shared.rs:L775-L775](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi/shared.rs#L775-L775)——`paged_attention_decode_cuda`：FlashInfer 分页解码注意力的 C 出口（u2-l4 会从 ops 封装角度再遇到它）。
- [pegainfer-kernels/src/ffi/shared.rs:L1027-L1027](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi/shared.rs#L1027-L1027)——`pegainfer_kernels_last_error() -> *const c_char`：错误文本出口，C++ 侧把异常转成消息存起来，Rust 侧用 `CStr` 读取（见 4.3.3 的测试样本）。

声明与实现的一一对应（以 qwen4 为例）：

- [pegainfer-kernels/csrc/qwen4/qwen4_grouped_rmsnorm.cu:L117-L117](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/csrc/qwen4/qwen4_grouped_rmsnorm.cu#L117-L117)——`CUresult rms_norm_grouped_offset_cuda(const DType *x, ...)`：CUDA C++ 侧实现，与 qwen4.rs L36 的 Rust 声明同名同签名（`DType` 即 16 位半精度）。
- [pegainfer-kernels/csrc/qwen4/qwen4_qsa_indexer_score.cu:L295-L295](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/csrc/qwen4/qwen4_qsa_indexer_score.cu#L295-L295)——`CUresult qwen4_qsa_indexer_block_scores_cuda(...)`：与 qwen4.rs L17 的声明配对。

#### 4.2.4 代码实践

1. **实践目标**：独立验证「一个 Rust 声明 ↔ 一个 .cu 实现」的配对关系。
2. **操作步骤**：
   - 读 [pegainfer-kernels/src/ffi/qwen4.rs:L9-L46](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ffi/qwen4.rs#L9-L46)，抄下两个符号名。
   - 在 `pegainfer-kernels/` 下执行 `grep -rn "rms_norm_grouped_offset_cuda\|qwen4_qsa_indexer_block_scores_cuda" csrc/`。
   - 对每个命中行，比对其 C 参数列表与 Rust 声明的参数列表（类型、顺序、个数）。
3. **需要观察的现象**：每个 Rust 声明恰好命中一个 .cu 定义；参数个数一致；`Half` 对应 `DType`/`uint16_t`、`CUstream` 对应 `CUstream`、`CUresult` 对应 `CUresult`。
4. **预期结果**：两对配对成立（`qwen4_grouped_rmsnorm.cu:117` 与 `qwen4_qsa_indexer_score.cu:295`，本讲已验证）。若发现参数不一致，那就是 ABI bug 的现场。

#### 4.2.5 小练习与答案

**练习 1**：`shared.rs` 里 `rms_norm_cuda` 返回 `()`，`rms_norm_batched_dual_cuda` 返回 `CUresult`。这种不一致意味着什么？

**答案**：意味着错误报告契约按符号逐个约定，没有全局保证。无返回值的符号要么「不会失败」（如纯数学内核，非法输入也只是算错），要么错误被吞掉；返回 `CUresult` 的符号才可检查失败。这正是 u2-l3 的 ops 封装层存在的理由之一：把不统一的底层契约统一成 Rust 的 `anyhow::Result` 风格出口。

**练习 2**：为什么 `Half = u16` 定义在 kernels 的 ffi.rs 顶部、而不是每个子模块各定义各的？

**答案**：ABI 类型必须全仓库唯一。若各子模块分别定义，一旦有人给其中一个加了包装类型（比如 newtype），位布局可能悄然改变，跨子模块传参就错位。定义在聚合器顶部、子模块 `use super::Half`，是从语法上锁死「同一类型」。

**练习 3**：core 的门面是**无条件** `pub use pegainfer_kernels::ffi::*`。当 kernels 没开 `qwen4` feature 时，`pegainfer_core::ffi::rms_norm_grouped_offset_cuda` 这个路径存在吗？

**答案**：不存在。feature 门控发生在 kernels 的 `mod qwen4` 声明处，未开 feature 时子模块不编译、其符号不存在，glob 再出口自然透不出这个符号——用这个路径的代码会在编译期报「未找到」错误。也就是说 feature 裁剪沿再出口链向上传播，编译期就拦截。

### 4.3 声明 → 编译 → 加载 → 调用：四步闭环

#### 4.3.1 概念说明

CLAUDE.md 的既定事实：「pegainfer-kernels/build.rs 负责 CUDA/Triton 编译：用 nvcc 编译 csrc/*.cu，自动探测 GPU SM 目标」。把它和 4.2 的声明合起来，就是完整的四步闭环：

1. **声明**（编译前）：`src/ffi/*.rs` 用 `unsafe extern "C"` 告诉 rustc「这些符号存在、签名如此」。
2. **编译**（build.rs）：nvcc 把 `csrc/**/*.cu` 逐个编成 `.o` 目标文件（按 feature 过滤参与编译的源）。
3. **加载**（链接期）：`ar` 把 `.o` 归档成 `libkernels_cuda.a`，build.rs 下发 `cargo:rustc-link-lib=static=kernels_cuda` 等指令，链接器把 Rust 声明与归档里的实现**静态**缝死——程序启动即有符号，没有运行时 dlopen。
4. **调用**（运行时）：kernels 内部的 ops 包装、以及模型 crate 经 `pegainfer_core::ffi` 调用这些函数。

为什么边界收敛、不散落在模型 crate？因为四步里最危险的是第 1 步（签名无人检查）和第 2 步（编译环境复杂）。把它们全部圈进 kernels（声明）+ core（门面）两个 crate 后：模型 crate 写的是纯 Rust 调用，换 CUDA 版本、调 nvcc 参数、加 feature，都只动 kernels/build.rs；符号签名漂移只可能在一处发生并被立刻全局暴露。

#### 4.3.2 核心流程

```text
cargo build（任意依赖 kernels 的 crate）
 ① rustc 先编译并运行 pegainfer-kernels/build.rs
    ├─ 收集 csrc/**/*.cu，按 feature 过滤（qwen4/glm52/kimi/... 各有谓词）
    ├─ 对每个 .cu：nvcc -c -O3 [-gencode arch=compute_X,code=sm_X] ... -o out/<stem>_cuda.o
    ├─ ar 归档全部 .o → libkernels_cuda.a
    └─ 下发链接指令：
         cargo:rustc-link-search=native=<out_dir>
         cargo:rustc-link-lib=static=kernels_cuda
         cargo:rustc-link-lib={cudart, cublas, cublasLt, cuda, nvrtc, stdc++}
 ② rustc 编译 kernels 的 src/ffi/*.rs → 记录 extern 符号引用
 ③ 最终链接：ld 在 libkernels_cuda.a 与 CUDA 库中解析每个 extern 声明
 ④ 运行：模型 crate → pegainfer_core::ffi::<符号> → libkernels_cuda.a 里的机器码
```

错误契约（边界上的第四件事）：C++ 侧内核可能抛异常，但异常**绝不允许**穿越 FFI 边界（C++ 异常进入 Rust 栈是未定义行为）。约定是：失败时返回 `-1` 哨兵，同时把异常文本存进线程局部缓冲，Rust 侧用 `pegainfer_kernels_last_error()` 取回 C 字符串。

#### 4.3.3 源码精读

**第 2 步（编译）——build.rs 收集并过滤 csrc 源**：

- [pegainfer-kernels/build.rs:L2263-L2313](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2263-L2313)——递归收集 `csrc/` 下全部文件，再按 feature 逐一剔除：`is_qwen4_source` 的过滤在 L2296-L2297（未开 `qwen4` feature 则 `csrc/qwen4/*.cu` 不参与编译），与其余 8 个模型专属谓词并列。注意 Rust 侧声明（4.2）与 CUDA 侧编译用的是**同一批 feature 名**，两边天然同步。
- [pegainfer-kernels/build.rs:L773-L774](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L773-L774)——`is_qwen4_source` 谓词本体：按路径前缀 `qwen4/` 判定归属。

**第 2 步（编译）——逐文件调 nvcc**：

- [pegainfer-kernels/build.rs:L2343-L2357](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2343-L2357)——对每个 `.cu` 构造 nvcc 命令：`-c`（只编译不链接）、`-o out/<stem>_cuda.o`（对象名由 `cuda_object_name` 派生）、`-O3`、`-isystem <cuda_include>`、`-I csrc`；个别内核再追加专属 `-gencode` 架构参数（如 glm52 系列，L2358 起）。

**第 3 步（加载/链接）——归档与链接指令**：

- [pegainfer-kernels/build.rs:L2856-L2863](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2856-L2863)——`ar` 把全部 `_cuda.o` 打成 `libkernels_cuda.a`。
- [pegainfer-kernels/build.rs:L2882-L2905](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2882-L2905)——向 rustc 下发：链接搜索路径指向 out_dir、`static=kernels_cuda`、外加 `cudart`/`cublas`/`cublasLt`/`cuda`/`nvrtc`/`stdc++`。静态链接意味着 extern 声明在**构建期**解析完毕——若声明了却没编译对应 `.cu`，链接直接失败，错误在 build 阶段暴露而不是运行时。

**第 4 步（调用）——kernels 内部与模型 crate 两条消费路径**：

- kernels 内部的 ops 包装是第一消费者：[pegainfer-kernels/src/ops/qwen4.rs:L22-L22](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ops/qwen4.rs#L22-L22) `use crate::ffi;`（同类引用还散布在 ops/embedding.rs、ops/dflash2.rs、ops/gemma4*.rs 等十余处）；而 [pegainfer-kernels/src/ops.rs:L294-L298](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/src/ops.rs#L294-L298) 展示了包装层读错误文本的标准姿势：`crate::ffi::pegainfer_kernels_last_error()` → `CStr::from_ptr` → `to_string_lossy`。
- 模型 crate 经 core 门面消费（真实调用点，全部属于 4.2.3 的 cuBLAS 生命周期四件套）：
  - [pegainfer-qwen3/src/executor.rs:L521-L521](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/executor.rs#L521-L521) `pegainfer_core::ffi::cublas_destroy()`；[L528](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/executor.rs#L528-L528) `cuda_set_device`；[L543](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen3/src/executor.rs#L543-L543) `cublas_init()`。
  - [pegainfer-gemma4/src/engine.rs:L1187-L1199](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-gemma4/src/engine.rs#L1187-L1199) `cublas_init` → `cublas_activate_device_handles` → `cuda_set_device` 的启动序列。
  - [pegainfer-deepseek-v2-lite/src/device.rs:L5-L5](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/device.rs#L5-L5) `use pegainfer_core::ffi;`（NCCL 后端同款引用见 [nccl_backend.rs:L35](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-deepseek-v2-lite/src/nccl_backend.rs#L35-L35)）。
  - 门面的门面：[pegainfer-qwen35/src/ffi.rs:L1-L1](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-qwen35/src/ffi.rs#L1-L1) `pub(crate) use pegainfer_core::ffi::*;`——qwen35 把 core 门面再窄化一层给自己的 crate 内部用。
- 诚实的补充：也有模型 crate **直接**用 `pegainfer_kernels::ffi`（如 [pegainfer-kimi-k2/src/runner/worker.rs:L661-L697](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kimi-k2/src/runner/worker.rs#L661-L697) 的 kimi 专属 cublasLt 句柄族）。所以准确的表述是：**声明只存在于 kernels 一处（单一事实源）**；访问路径有 core 门面与 kernels 直连两种，但无论哪种，模型 crate 都不写自己的 `extern "C"` CUDA 声明。

**错误契约的活样本**：

- [pegainfer-kernels/tests/ffi_exception_catch.rs:L38-L85](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/tests/ffi_exception_catch.rs#L38-L85)——测试用 GQA group 7（56/8，不受支持的组合）驱动 `ffi::paged_attention_decode_cuda`，断言 FlashInfer 的 C++ 异常以 `-1` 哨兵浮出（[L79](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/tests/ffi_exception_catch.rs#L79-L79)）且消息含 `group_size`（[L81-L83](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/tests/ffi_exception_catch.rs#L81-L83)，经 `CStr::from_ptr(ffi::pegainfer_kernels_last_error())` 读取）。头注释 L1-L3 说明了动机：加防护之前这个异常会直接杀死整个进程。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到闭环里「编译 → 加载」两步的产物与指令。
2. **操作步骤**：
   - 读 [pegainfer-kernels/build.rs:L2343-L2357](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2343-L2357) 与 [L2882-L2905](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-kernels/build.rs#L2882-L2905)，记下 nvcc 参数模板与七条链接库。
   - 在有 CUDA 工具链的环境执行 `cargo build --release -p pegainfer-kernels`（CLAUDE.md 提醒：务必 `--release`，且构建慢是正常的——nvcc 逐文件编译）。
   - 构建后到 `target/release/build/pegainfer-kernels-*/out/` 目录查看：应能看到 `*_cuda.o` 对象文件与 `libkernels_cuda.a` 归档。
3. **需要观察的现象**：构建日志中的 `Compiling CUDA translation units with N nvcc job(s)` 警告行（来自 build.rs L2333，`N` 受 `PEGAINFER_NVCC_JOBS` 影响）；out 目录里的 `.o` 文件名与 `csrc/` 相对路径的对应关系（`cuda_object_name` 的产出，如 qwen4 分组的 `qwen4_qwen4_grouped_rmsnorm_cuda.o` 风格）。
4. **预期结果**：`ls target/release/build/pegainfer-kernels-*/out/*.a` 能列出 `libkernels_cuda.a`。**待本地验证**：本讲写作环境未执行完整 GPU 构建，`.o` 的确切文件名与数量取决于本机开启的 feature 集，请以实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：假设有人在 `ffi/shared.rs` 里声明了一个符号 `foo_cuda`，但对应的 `.cu` 文件忘了放进 `csrc/`。错误在什么时候、以什么形式暴露？

**答案**：在最终链接期。`unsafe extern "C"` 声明只产生一个未解析的符号引用；rustc 编译ffi.rs 时不报错；直到链接器在 `libkernels_cuda.a` 与各 CUDA 库里找不到 `foo_cuda`，报 `undefined reference`/`unresolved symbol`，构建失败。若符号走运行时加载路线（如 Triton cubin），错误会推迟到运行时——静态链接把这类错误尽量提前了。

**练习 2**：为什么 `pegainfer_kernels_last_error` 返回 `*const c_char`（C 字符串指针）而不是 Rust 的 `String`？

**答案**：FFI 边界上不能传 Rust 类型——`String` 的布局（指针、长度、容量三元组）是 Rust 私有 ABI，C++ 侧无法构造。C 字符串（NUL 结尾的 `char` 数组）是两种语言共同的最低公分母；Rust 侧再用 `CStr::from_ptr` 安全地借出切片、`to_string_lossy` 转成 Rust 字符串。所有权约定是「C++ 侧持有缓冲、Rust 只读」，因此无需释放。

**练习 3**：模型 crate 想新增一个专属 CUDA 内核，按本讲的闭环要动哪几个文件？哪些 crate 完全不用动？

**答案**：只动 pegainfer-kernels：① `csrc/<model>/*.cu` 写实现；② `src/ffi/<model>.rs` 加 `unsafe extern "C"` 声明（新模型则新建子模块并在 `src/ffi.rs` 挂 feature 门控）；③ `build.rs` 加（或复用）`is_<model>_source` 过滤谓词与专属 nvcc 参数；④ Cargo.toml 的 feature 声明。core 的门面是 glob 再出口，自动透出新符号；已依赖 core 的模型 crate 直接就能 `pegainfer_core::ffi::new_symbol` 调用——这就是边界收敛的 payoff。

## 5. 综合实践

**任务：亲手产出一张「FFI 边界清单」，量化本讲的三条结论。**

你已经在本讲各模块做过零散检索，现在把它们拼成一张表。表中每行一个符号，五列：

| 符号 | core 门面路径 | kernels 真身模块 | core 内消费点 | 实际消费方（工作区） |
| --- | --- | --- | --- | --- |
| `cublas_init` | `pegainfer_core::ffi::cublas_init` | `ffi/shared.rs` L465 | 无 | qwen3 executor.rs:543、gemma4 engine.rs:1610 |
| `cuda_set_device` | `pegainfer_core::ffi::cuda_set_device` | `ffi/shared.rs` L468 | 无 | qwen3 executor.rs:528、gemma4 engine.rs:1199 |
| `paged_attention_decode_cuda` | `pegainfer_core::ffi::paged_attention_decode_cuda` | `ffi/shared.rs` L775 | 无 | kernels 自身测试 ffi_exception_catch.rs:57 |
| `rms_norm_grouped_offset_cuda` | `pegainfer_core::ffi::rms_norm_grouped_offset_cuda` | `ffi/qwen4.rs` L36 | 无 | 待确认（交叉验证见步骤 4） |
| `pegainfer_kernels_last_error` | `pegainfer_core::ffi::pegainfer_kernels_last_error` | `ffi/shared.rs` L1027 | 无 | kernels src/ops.rs:294 |

操作步骤：

1. **逐行确认门面**：`cat pegainfer-core/src/ffi.rs pegainfer-core/src/tensor.rs`——各一行，这是表格第二列成立的前提。
2. **填「core 内消费点」列**：在 `pegainfer-core/` 下 `grep -rn "crate::ffi" src/`——预期全表此列为「无」（本讲已验证），这证明门面服务对象是模型 crate 而非 core 自身。注意区分 `std::ffi::CStr` 的两个假阳性。
3. **填「实际消费方」列**：在仓库根目录 `grep -rn "pegainfer_core::ffi" --include="*.rs" .` 与 `grep -rn "pegainfer_kernels::ffi" --include="*.rs" .`，把命中归到表格（本讲 4.3.3 已给出主要命中：qwen3、gemma4、deepseek-v2-lite、qwen35 经 core 门面；kimi-k2、glm52、k3 直连 kernels）。
4. **交叉验证「待确认」条目**：对 core 乃至整个工作区都查不到消费点的符号（如 `rms_norm_grouped_offset_cuda`，HEAD 上刚随 qwen4 feature 落地），到本讲义范围外的 `pegainfer-kernels/src/ops/qwen4.rs` 检索符号名——kernels 的 ops 包装层（`use crate::ffi` 的那十余个文件）是消费点最可能的藏身处；仍找不到就如实保留「待确认」，并检查该 feature 是否默认开启（默认关的 feature，其符号自然无调用方）。
5. **写结论**：用三句话总结——(a) 声明集中在哪；(b) 门面为谁服务；(c) 若删掉 core 的 ffi.rs，哪些 crate 的编译会先坏（提示：检索 `pegainfer_core::ffi` 的 use 行）。

预期产物：一张 5 行以上的表 + 三句结论。此任务不需要 GPU，纯检索即可完成；全部命令本讲写作时均已实际运行过。

## 6. 本讲小结

- core 的 `ffi.rs` 与 `tensor.rs` 是孪生的一行门面（`pub use pegainfer_kernels::<mod>::*`），动机是单一事实源与编译边界收敛：CUDA 声明只维护在 kernels 一处，改一处全局生效。
- 真身 `pegainfer-kernels/src/ffi.rs` 是聚合器：`Half = u16` ABI 类型 + 按 feature 门控的 12 个子模块（shared/lora 无条件，qwen4/qwen35/kimi/glm52/k3/gemma4/deepseek_v2_lite/deepep 各随 feature），统一再出口保持路径稳定。
- 四步闭环：ffi/*.rs 声明 → build.rs 用 nvcc 把（feature 过滤后的）csrc/**/*.cu 编成 `.o` → `ar` 归档 `libkernels_cuda.a` 并静态链接（`static=kernels_cuda` + cudart/cublas/cublasLt/cuda/nvrtc/stdc++）→ kernels ops 包装与模型 crate 调用；符号在链接期解析，声明/实现不配对在构建期就报错。
- 错误契约：C++ 异常绝不穿越边界，失败以 `CUresult` 的 `-1` 哨兵浮出、文本经 `pegainfer_kernels_last_error()`（C 字符串）取回，`ffi_exception_catch.rs` 是活样本。
- core 内部零 `crate::ffi` 消费——门面纯粹为模型 crate 服务（qwen3/gemma4/dsv2-lite/qwen35 经门面，kimi-k2/glm52/k3 直连 kernels）；无论哪条路径，模型 crate 都不写自己的 CUDA `extern` 声明。
- 检索陷阱：core 内的 `std::ffi::CStr`/`c_char`（cuda_graph/dump.rs、cpu_topology.rs）是标准库模块，与 CUDA FFI 门面只是名字撞车。

## 7. 下一步学习建议

- **下一讲 u2-l3（ops.rs：共享 GPU 算子封装总览）**：本讲的 ffi 声明是「裸指针 + CUresult」的原始出口，ops 封装把它们包成类型安全、带错误检查、接受 `DeviceVec`/`DeviceMatrix` 的 Rust 函数，并展示 `kernel-call-trace` feature 如何换出口。带上本讲的边界清单去读，会看到表里每个符号在 ops 层的对应包装。
- **提前翻一眼 u2-l4**：`paged_attention_decode_cuda`（shared.rs L775）在 `ops/attention.rs` 有三个封装入口，读完 u2-l3 再看它是很好的闭环复习。
- **顺带阅读**：`pegainfer-kernels/src/ops.rs` L294-L298 的错误文本读取模式，与 `tests/ffi_exception_catch.rs` 对照，体会「哨兵 + 消息」契约的两侧写法。
