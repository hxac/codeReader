# ffi.rs：Rust 与 CUDA 的边界

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 Rust 调用 CUDA kernel 的**「声明 → 编译 → 链接 → 调用」四步闭环**，并为每一步指出具体文件与构建阶段。
2. 解释 `pegainfer-core/src/ffi.rs` 与 `tensor.rs` 为什么都只是一行 `pub use`——单一事实源与编译边界收敛这两个动机。
3. 理解 FFI 边界为什么收敛在 pegainfer-core 与 pegainfer-kernels 两个 crate，而不散落在各模型 crate。
4. 区分仓库里三条容易混淆的「FFI」：CUDA kernel 的 FFI（本讲主角）、宿主 libc 的 FFI（cpu_topology）、以及只是类型转换工具的 `std::ffi::CStr`。

本讲承接 u2-l1（tensor 门面），是通往 u2-l3（ops 算子封装）的中间一环：ops 封装的地基正是本讲的 ffi 声明。

## 2. 前置知识

- **FFI（Foreign Function Interface，外部函数接口）**：让 Rust 调用 C/C++ 编译产物中符号的机制。Rust 侧用 `extern "C"` 块声明「存在一个这样签名的 C 函数」，链接器负责把声明和实现缝在一起。
- **ABI（Application Binary Interface）**：双方在机器码层面「怎么传参数、怎么返回」的约定，不一致就是未定义行为。本仓库的约定：16 位浮点用 `Half = u16`（与 CUDA `half` 位布局相同）、流句柄用 `CUstream`、返回码用 `CUresult`——后两者来自 cudarc 对 CUDA 驱动 API 类型的再暴露。
- **`unsafe extern "C"`**：Rust 2024 版语法（core 是 edition 2024，见 u1-l3）。2024 版要求 `extern` 块本身标 `unsafe`，提醒你「声明的签名与实现是否一致」由你负责，编译器不检查。
- **静态链接 vs 运行时加载**：CUDA 代码可编成目标文件静态链进二进制（符号在构建期解析），也可编成 cubin/PTX 由驱动在运行时加载。本仓库 csrc 路线是前者（4.3 验证）；Triton/TileLang AOT 生成的内核走运行时路线（本讲不展开，见 CLAUDE.md 的构建系统说明）。
- **build.rs（Cargo 构建脚本）**：Cargo 在编译 crate 前先编译并运行的脚本，用 `println!("cargo:rustc-link-lib=...")` 这类指令反过来告诉链接器怎么链。
- **再出口门面（re-export facade）**：自己不定义任何东西，只 `pub use` 别处的符号，让外界经稳定路径访问。u2-l1 已在 tensor.rs 上建立这个概念。
- **一条重要澄清**：仓库里带「ffi」字样的东西有三种身份——kernels 的 CUDA 符号声明模块（本讲主角）、Rust 标准库 `std::ffi`（CStr/c_char 等类型工具）、宿主进程对 libc 的调用。4.1.4 的实践会帮你把三者彻底分开。

## 3. 本讲源码地图

| 文件 | 规模 | 作用 |
| --- | --- | --- |
| `pegainfer-core/src/ffi.rs` | 1 行 | 门面：一行再出口 kernels 的 ffi 模块（本讲主角） |
| `pegainfer-core/src/tensor.rs` | 1 行 | 孪生门面（u2-l1 已精读） |
| `pegainfer-core/src/lib.rs` | 15 行 | 挂载 13 个 `pub mod`，其中 ffi 在第 7 行、tensor 在第 13 行 |
| `pegainfer-core/src/ops.rs` | 大宗再出口清单 | 算子封装层——ffi 声明的间接消费出口（u2-l3 主角） |
| `pegainfer-core/src/cpu_topology.rs` | 329 行 | 宿主侧 FFI 对照组：libc + cudarc driver API |
| `pegainfer-kernels/src/ffi.rs` | 约 50 行 | FFI 真身聚合器：定义 `Half`，按 feature 挂载子模块并统一再出口 |
| `pegainfer-kernels/src/ffi/qwen4.rs` | 40+ 行 | 模型线声明样本：QSA indexer 与分组 RMSNorm 两个符号 |
| `pegainfer-kernels/src/ffi/shared.rs` | 逾千行 | 跨模型共享符号，含错误文本出口 |
| `pegainfer-kernels/csrc/qwen4/*.cu` | 2 个文件 | 声明对应的 CUDA C++ 实现 |
| `pegainfer-kernels/csrc/shared/ffi_last_error.cu` | — | `pegainfer_kernels_last_error` 的 extern "C" 实现 |
| `pegainfer-kernels/build.rs` | 约 2900 行 | 编译闭环：nvcc、SM 探测、静态归档、链接指令 |
| `pegainfer-kernels/tests/ffi_exception_catch.rs` | 80+ 行 | FFI 错误契约的活样本 |

本讲主角是 core 的 `ffi.rs`，但它的「真身」在 pegainfer-kernels——我们要跨过 crate 边界看两边如何对接。这正是 u1-l1 遗留问题「core 与 kernels 怎么分工」的最好样本。

## 4. 核心概念与源码讲解

### 4.1 一行再出口门面：ffi.rs 与 tensor.rs 的孪生结构

#### 4.1.1 概念说明

core 的 `ffi.rs` 全文只有一行 `pub use pegainfer_kernels::ffi::*;`，与 `tensor.rs` 构成一对孪生门面。动机有三个：

1. **单一事实源（single source of truth）**：CUDA 符号的 Rust 签名只维护在 kernels 一处。若每个模型 crate 自己写一份 `extern "C"` 声明，签名漂移（比如参数从 `i32` 改成 `i64`）时只改了一处、别处悄悄错位——而且**两份不一致的声明照样能链接成功**（链接器只认符号名），运行时才是未定义行为。声明收敛后，这类错误在结构上不可能发生。
2. **编译边界收敛**：与 CUDA/cudarc/nvcc 的纠缠全部下沉到 kernels 一个 crate（只有它的 build.rs 跑 nvcc，见 4.3）。core 与模型 crate 只需「能引用这些符号」，不需要理解它们怎么被编译出来。
3. **公开路径稳定**：模型 crate 只依赖 pegainfer-core（u2-l1 建立的分层），ffi 符号经 `pegainfer_core::ffi` 稳定路径可达。kernels 侧持续演进——例如 qwen4 的分组 RMSNorm（提交 8b5b84a9）、kimi-k2 的专家路由修复（提交 09a1a38f）都只发生在 kernels 与模型 crate 一侧——core 门面保持零改动。

#### 4.1.2 核心流程

一个符号从模型 crate 到 CUDA 实现的名字解析链（以 qwen4 的新符号为例）：

```text
模型 crate:    pegainfer_core::ffi::rms_norm_grouped_offset_cuda
                 ↓  pub use pegainfer_kernels::ffi::*;       （core/src/ffi.rs:1）
kernels:       pegainfer_kernels::ffi::rms_norm_grouped_offset_cuda
                 ↓  #[cfg(feature="qwen4")] pub use qwen4::*;（kernels/src/ffi.rs:46-47）
kernels 子模块: qwen4.rs 内的 unsafe extern "C" 声明           （ffi/qwen4.rs:36）
                 ↓  链接期符号解析（见 4.3）
CUDA 实现:     libkernels_cuda.a 中的同名符号                 （csrc/qwen4/qwen4_grouped_rmsnorm.cu 编译产物）
```

注意中间一跳：kernels 的聚合器把按模型线拆分的子模块**重新拍平**成单一命名空间，下游永远不需要知道符号住在哪个文件。

#### 4.1.3 源码精读

- [pegainfer-core/src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ffi.rs#L1)：门面本体——整文件仅此一行，把 kernels 的全部 FFI 符号再出口为 core 公开面。
- [pegainfer-core/src/tensor.rs:L1](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/tensor.rs#L1)：孪生门面，同款一行，指向 kernels 的 tensor 模块（u2-l1 已精读其公开面）。
- [pegainfer-core/src/lib.rs:L7](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L7)：`pub mod ffi;` 把门面挂进 core 的模块树（`pub mod tensor;` 在 [lib.rs:L13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L13)）。mod 声明与 pub use 缺一不可：前者把文件纳入编译，后者提供内容。
- 对照组：[pegainfer-core/src/ops.rs:L1-L8](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L1-L8)——ops 不是一行门面：它既有原生子模块（attention、paged_plan、call_spec，以及 kernel-call-trace feature 下的 call_trace/traced），又有对 kernels 的大宗再出口。「门面」与「封装」是两种角色，ffi/tensor 属于前者。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认两个门面的「一行」事实，并验证「core 内部零消费」这一反直觉结论。
2. **操作步骤**（在 pegainfer-core 目录，纯 CPU 无需 GPU）：

   ```bash
   cat src/ffi.rs src/tensor.rs        # 两个文件各打印一行
   grep -rn "crate::ffi" src/          # 在 core 内搜 ffi 的内部消费
   grep -rn "ffi" src/ --include=*.rs  # 放宽到任意 ffi 字样，人工分类
   ```

3. **需要观察的现象**：第一个命令输出两行 `pub use`；第二个命令 0 命中；第三个命令的命中可分三类——`src/ffi.rs` 与 `src/lib.rs` 本身、`src/cpu_topology.rs` 与 `src/cuda_graph/dump.rs` 的 `std::ffi`，以及 "suffice"（rope.rs:158）、"insufficient"（page_pool.rs:166）这类英文单词的误报。
4. **预期结果**：core 的 ffi 门面没有任何内部消费者——它服务于下游模型 crate。core 自身需要 GPU 能力时走 ops 与 tensor 两个模块（kernels 内部的 ops 包装层才有 `crate::ffi` 消费，见 4.2.3 末尾的 [kernels/src/ops.rs:L294](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L294)）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ffi.rs` 的一行通配再出口改成逐项列举（如只 `pub use pegainfer_kernels::ffi::shared::*;`），会失去什么？
**答案**：kernels 侧按 feature 新增子模块（比如将来新增一条模型线）时，core 门面不会自动跟随，每加一条线都要同步改 core——一行通配再出口正是「kernels 持续演进、core 门面稳定」的机制保障。

**练习 2**：`pub mod ffi;`（lib.rs）与 `pub use pegainfer_kernels::ffi::*;`（ffi.rs）各自缺了会发生什么？
**答案**：缺 mod 声明则 ffi.rs 根本不参与编译；缺 pub use 则模块存在但为空。两者合力才构成「稳定路径 → kernels 符号」的完整链路。

**练习 3**：core 内部 grep 不到 `crate::ffi::`，那 core 的 kv_pool、rope 这些模块怎么用上 GPU？
**答案**：经 `crate::tensor`（DeviceContext/DeviceVec 等，u2-l1 统计过 19 处引用）和 `crate::ops`（算子封装）。原始 ffi 符号是给 kernels 内部包装层和下游模型 crate 直接使用的低层出口。

### 4.2 kernels 侧 FFI 真身：按模型线拆分的声明树

#### 4.2.1 概念说明

门面背后，真正的 `unsafe extern "C"` 声明全部住在 `pegainfer-kernels/src/ffi/` 目录，由聚合器 `ffi.rs` 统一挂载。设计要点：

1. **按「拥有该内核的模型线」拆文件**：qwen35.rs、qwen4.rs、kimi.rs、glm52.rs（含 `glm52/` 子目录六个文件）、k3.rs、gemma4.rs、deepseek_v2_lite.rs，加上跨模型的 shared.rs、deepep.rs、lora.rs，共 18 个文件。谁家的内核谁声明——CLAUDE.md「model-specific kernels live in feature-gated submodules」说的就是这件事。
2. **cargo feature 门控挂载**：除 shared 与 lora 外，每个子模块都在 `#[cfg(feature = "...")]` 下声明与再出口。不开 feature，声明根本不进编译——链接也就不会去要对应 .cu 的符号。
3. **`Half = u16` 共享 ABI 类型**：声明层只承诺位布局（16 位 = CUDA half），不承诺浮点语义；类型化的 f16/bf16 处理留在包装层。跨语言边界的类型越「哑」越安全。
4. **错误契约**：kernel 入口统一返回 `CUresult`；失败细节经 `pegainfer_kernels_last_error()` 出口取回 C 字符串。声明 → 实现 → 测试三点都能在源码里按行号对上（见 4.2.3 末尾）。

#### 4.2.2 核心流程

一个 feature 如何变成可用的符号：

```text
cargo feature 开启（如 --features qwen4，或经模型 crate 传递）
  → ffi.rs:  #[cfg(feature = "qwen4")] mod qwen4;         （L26-L27）
  → ffi.rs:  #[cfg(feature = "qwen4")] pub use qwen4::*;   （L46-L47）
  → 下游可见: pegainfer_kernels::ffi::rms_norm_grouped_offset_cuda
```

对照：`mod lora;`（L23）与 `mod shared;`（L28）无 cfg，恒定编译——因为它们被多条模型线共享。整个目录的 18 个文件分三组：

| 分组 | 文件 | 门控 feature |
| --- | --- | --- |
| 跨模型共享 | `shared.rs`、`lora.rs` | 无（恒挂载） |
| 模型线专属 | `qwen35.rs`、`qwen4.rs`、`kimi.rs`、`glm52.rs` + `glm52/` 六文件、`k3.rs`、`gemma4.rs`、`deepseek_v2_lite.rs`、`deepep.rs` | qwen35 / qwen4 / kimi-k2 / glm52 / k3 / gemma4 / deepseek-v2-lite / moe |
| 生成内核专属 | `gemma4_tilelang.rs`、`k3_tilelang.rs` | gemma4、k3 |

（`*_tilelang.rs` 对应 TileLang AOT 生成内核的声明——生成物与手写 csrc 一样收敛到同一个 ffi 命名空间。）

#### 4.2.3 源码精读

- [pegainfer-kernels/src/ffi.rs:L1-L2](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L1-L2)：头注释言明两件事——FFI 面按拥有模型拆分；**公开路径不变**，符号都经下面的再出口解析。这句话就是「kernels 随便拆文件、下游路径不动」的契约。
- [pegainfer-kernels/src/ffi.rs:L5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L5)：`pub type Half = u16;`——所有子模块共享的 ABI 类型，注释明示与 CUDA half 同布局。
- [pegainfer-kernels/src/ffi.rs:L7-L28](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L7-L28)：12 个子模块的 cfg 门控声明（deepep、deepseek_v2_lite、gemma4、gemma4_tilelang、glm52、k3、k3_tilelang、kimi、lora、qwen35、qwen4、shared）。
- [pegainfer-kernels/src/ffi.rs:L29-L50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L29-L50)：与上面一一配对的 `pub use <mod>::*;`——把树状拆分拍平成单一命名空间。
- [pegainfer-kernels/src/ffi/qwen4.rs:L1-L2](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/qwen4.rs#L1-L2)：头注释直接点名实现文件 `csrc/qwen4/qwen4_qsa_indexer_score.cu`——声明与 .cu 的配对关系写在代码里。
- [pegainfer-kernels/src/ffi/qwen4.rs:L4-L7](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/qwen4.rs#L4-L7)：`use cudarc::driver::sys::{CUresult, CUstream};` 与 `use super::Half;`——FFI 签名里的类型只有三种来源：cudarc 的驱动类型、共享 Half、Rust 原生标量（i32/f32/裸指针）。
- [pegainfer-kernels/src/ffi/qwen4.rs:L9](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/qwen4.rs#L9)：`unsafe extern "C" {`——2024 版语法，整个声明块由人向编译器担保与实现一致。
- [pegainfer-kernels/src/ffi/qwen4.rs:L17-L32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/qwen4.rs#L17-L32)：`qwen4_qsa_indexer_block_scores_cuda` 全签名——指针传数据、i32 传维度、f32 传 eps、CUstream 传流、返回 CUresult。其 [L10-L16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/qwen4.rs#L10-L16) 的 doc 注释值得细读：几何（640 宽的 fused 行、cos/sin 表 stride 64）**编译进了内核**，调用方缓冲布局不匹配时包装层的大小检查看不出来——这是 FFI 声明层必须写重文档的典型样本。
- [pegainfer-kernels/src/ffi/qwen4.rs:L34-L36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/qwen4.rs#L34-L36)：`rms_norm_grouped_offset_cuda`——即提交 8b5b84a9「add Qwen4's grouped (1+w) RMSNorm」引入的符号；doc 注释指明契约见 kernels 的 `ops::rms_norm_grouped_offset_batch_into`（声明与包装的分工再次显现）。实现配对 `csrc/qwen4/qwen4_grouped_rmsnorm.cu`——目录内两个 .cu 与两个声明一一对应。
- **错误契约三点一线**：声明在 [pegainfer-kernels/src/ffi/shared.rs:L1027](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L1027)（`pub fn pegainfer_kernels_last_error() -> *const c_char`）；实现在 [pegainfer-kernels/csrc/shared/ffi_last_error.cu:L13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_last_error.cu#L13)（`extern "C" const char* ... { return g_ffi_last_error.c_str(); }`）；测试在 [pegainfer-kernels/tests/ffi_exception_catch.rs:L81](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/ffi_exception_catch.rs#L81)（`CStr::from_ptr` 取文本）。消费样本在 [pegainfer-kernels/src/ops.rs:L294](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L294)——**kernels 的 ops 包装层**在这里把失败翻译成带文本的 Rust 错误。

#### 4.2.4 代码实践

1. **实践目标**：用一条命令把「声明 → 实现 → 消费 → 测试」四点串起来，掌握符号追踪的标准动作。
2. **操作步骤**（在仓库任意位置，git 只读）：

   ```bash
   git grep -n pegainfer_kernels_last_error HEAD -- ':(top)pegainfer-kernels'
   git ls-files pegainfer-kernels/src/ffi pegainfer-kernels/csrc/qwen4
   ```

3. **需要观察的现象**：第一条恰好命中四行——`src/ffi/shared.rs:1027`（声明）、`csrc/shared/ffi_last_error.cu:13`（实现）、`src/ops.rs:294`（消费）、`tests/ffi_exception_catch.rs:81`（测试）；第二条列出 18 个声明文件与 2 个 qwen4 的 .cu。
4. **预期结果**：一个 FFI 符号的完整生命周期都能用 `git grep <符号名>` 定位——这就是贡献新内核时自查「声明、实现、包装、测试是否齐活」的清单。

#### 4.2.5 小练习与答案

**练习 1**：为什么声明层用 `Half = u16` 而不用 half crate 的 `f16`？
**答案**：FFI 只需要位布局一致；`u16` 是所有 C 工具链都无歧义的最小公共类型。引入带运算语义的类型反而让人误以为跨边界还有类型保障——语义化（norm、scale、bf16 转换）全部留在包装层做。

**练习 2**：`gemma4_tilelang.rs`、`k3_tilelang.rs` 这类文件名后缀暗示什么？
**答案**：TileLang AOT 生成内核的声明入口。手写 csrc 与生成内核两条进料口，最终都收敛到同一个 `pegainfer_kernels::ffi` 命名空间——下游无感。

**练习 3**：声明返回 `CUresult` 而非 `Result<T, E>`，谁负责翻译成 Rust 风格错误？
**答案**：kernels 的 ops 包装层（[kernels/src/ops.rs:L294](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L294) 可见它调 `crate::ffi::pegainfer_kernels_last_error()` 取错误文本）。core 与模型 crate 见到的已经是安全包装。

### 4.3 声明 → 编译 → 链接 → 调用：四步闭环

#### 4.3.1 概念说明

把 4.1、4.2 的碎片拼成完整闭环：**声明**发生在源码期（ffi/*.rs 的 `unsafe extern "C"`）；**编译**发生在 kernels 构建期（build.rs 调 nvcc 把 csrc/*.cu 编成目标文件并归档）；**链接**由 build.rs 下发的 cargo 指令完成（静态库链进最终二进制，符号在链接期解析）；**调用**发生在运行期，且总是先经过 kernels ops 的安全包装、再经 core 的 ops 再出口到达模型 crate。

为什么这条边界收敛在 core 与 kernels 两个 crate，而不散落在模型 crate？四个理由：

1. **防签名漂移**：extern 声明只有一份；两份不一致仍能链接成功的那种静默未定义行为，在结构上被排除（见 4.1.1）。
2. **工具链知识只活一处**：nvcc、SM 探测、gencode 兼容性探测全部封装在 build.rs；模型 crate 对 CUDA 工具链零感知。
3. **feature 门控只此一家**：模型 crate 通过 cargo feature 选择内核集合，而不是各自维护声明副本。
4. **公开路径稳定**：kernels 内部怎么拆文件都不影响 `pegainfer_core::ffi` 路径——头注释「Public paths are unchanged」就是这句承诺。

#### 4.3.2 核心流程

```text
① 声明（源码期）  kernels/src/ffi/*.rs
                   unsafe extern "C" { pub fn xxx_cuda(...) -> CUresult; }
② 编译（构建期）  kernels/build.rs 调 nvcc 编译 csrc/**/*.cu
                   - SM 目标自动探测（--list-gpu-arch），PEGAINFER_CUDA_SM 可覆盖
                   - gencode 兼容性用编译探针验证
                   - 产物归档为 out_dir 下的 libkernels_cuda.a
③ 链接（构建期）  build.rs 下发：
                   cargo:rustc-link-search=native={out_dir}
                   cargo:rustc-link-lib=static=kernels_cuda
                   + 动态链接 cudart / cublas / cublasLt / cuda / nvrtc / stdc++
                   → extern 声明与 .a 中的符号在最终二进制链接期对上
④ 调用（运行期）  kernels ops 安全包装（CUresult → Rust 错误）
                   → core ops 大宗再出口（core/src/ops.rs:17 起）
                   → 模型 crate 前向代码
```

#### 4.3.3 源码精读

- [pegainfer-kernels/build.rs:L354-L363](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L354-L363)：`nvcc_command`——所有 nvcc 调用的入口，支持 `PEGAINFER_NVCC_LAUNCHER` 包装；[L367-L368](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L367-L368) 用 `--list-gpu-arch` 探测 nvcc 支持的架构，[L402](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L402) 起做 SM 目标归一化（CLAUDE.md 的 `PEGAINFER_CUDA_SM` 覆盖点）。
- [pegainfer-kernels/build.rs:L485-L497](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L485-L497)：gencode 探针——build.rs 现场写一个 `extern "C" __global__ void pegainfer_nvcc_probe() {}` 让 nvcc 试编译，验证 compute/sm 组合可用。构建脚本自己也在做一次微型 FFI 闭环。
- [pegainfer-kernels/build.rs:L2882](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2882) 与 [L2891](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2891)：`rustc-link-search` 指向 out_dir、`rustc-link-lib=static=kernels_cuda` 把归档静态链入——第③步的核心两行。
- [pegainfer-kernels/build.rs:L2892-L2899](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2892-L2899)：cudart/cublas/cublasLt/cuda/nvrtc 动态链接——自家内核静态、厂商运行库动态的混合策略（[L2904](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2904) 补 stdc++）。
- [pegainfer-core/src/ops.rs:L17](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L17)：闭环第④步的 core 侧起点——`pub use pegainfer_kernels::ops::...` 大宗再出口（[L34-L35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L34-L35) 还能看到 kernel-call-trace feature 如何在不改调用方的情况下换出口，u2-l3 展开）。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到四步闭环里的第②步发生。
2. **操作步骤**（二选一）：
   - 有 CUDA 工具链：`PEGAINFER_BUILD_TIMING=1 cargo build --release -p pegainfer-core`，观察 kernels build.rs 打印的各阶段计时（nvcc 编译占大头）。
   - 无 GPU/工具链（本讲作者环境即如此）：源码阅读型实践——沿 `rms_norm_grouped_offset_cuda` 走一遍四步：声明（[ffi/qwen4.rs:L36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/qwen4.rs#L36)）→ 实现（`csrc/qwen4/qwen4_grouped_rmsnorm.cu`）→ 链接（[build.rs:L2891](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2891)）→ 调用（kernels ops 的 `rms_norm_grouped_offset_batch_into` 包装，qwen4.rs doc 注释指路）。
3. **需要观察的现象**：分支一能看到 nvcc 阶段耗时输出；分支二能把每一步的文件与行号记进笔记。
4. **预期结果**：分支一的具体输出**待本地验证**（需要本机 CUDA 工具链）；分支二可立即完成，产物是四行「步骤 → 文件:行号」清单。

#### 4.3.5 小练习与答案

**练习 1**：为什么自家内核用 `static=kernels_cuda` 而 cublas 用动态链接？
**答案**：自家内核随每次构建产出、不存在版本兼容问题，静态链进二进制部署最简单；cublas/cudart 是随驱动和工具链安装的系统库，动态链接才能跟随机器上的版本。

**练习 2**：如果 ffi/qwen4.rs 里某声明的参数顺序与 .cu 实现不一致，构建会失败吗？
**答案**：不会。链接器只按符号名解析，签名不匹配不报错；运行期按错误布局传参是未定义行为。这正是 4.1.1 强调单一事实源的根本原因。

**练习 3**：`PEGAINFER_CUDA_SM` 环境变量作用在闭环哪一步？
**答案**：第②步编译期——build.rs 的 SM 探测与归一化（[build.rs:L367 起](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L367)）。它与运行期无关，改它需要重新构建。

## 5. 综合实践

**任务：整理你自己的「FFI 边界清单」**——把本讲全部内容压进一张表，并养成「查不到消费点就标注待确认」的纪律。

**步骤**：

1. 在 pegainfer-core 目录逐行确认两个门面：`cat src/ffi.rs src/tensor.rs`。
2. 用 `grep -rn "crate::ffi" src/` 与 `grep -rn "crate::tensor" src/` 分别统计 core 内部消费点。
3. 填写下表（「已验证」行为本讲作者的现成答案，可直接核对）：

| core 侧入口 | kernels 侧真身 | core 内消费点 | 状态 |
| --- | --- | --- | --- |
| `pegainfer_core::ffi`（[src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ffi.rs#L1)） | `pegainfer_kernels::ffi` 聚合器 + `src/ffi/` 18 个文件 | **0 处**（grep 验证） | 已验证 |
| `pegainfer_core::tensor`（[src/tensor.rs:L1](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/tensor.rs#L1)） | `pegainfer_kernels::tensor` 模块 | 19 处 `crate::tensor::`（u2-l1 统计） | 已验证 |
| `pegainfer_core::ops` 的再出口（[src/ops.rs:L17](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L17) 起） | `pegainfer_kernels::ops` 包装层（内部经 `crate::ffi` 调符号） | 未逐一统计（u2-l3 的任务） | 模式已验证 |
| `std::ffi::CStr`（[src/cpu_topology.rs:L3-L4](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cpu_topology.rs#L3-L4)、[src/cuda_graph/dump.rs:L2](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph/dump.rs#L2)） | Rust 标准库 | cpu_topology、dump 自用 | 已验证 |
| 宿主 libc FFI（[src/cpu_topology.rs:L121](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cpu_topology.rs#L121)、[L141](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cpu_topology.rs#L141)） | 系统 libc | cpu_topology 自用 | 已验证 |
| `rms_norm_grouped_offset_cuda` 是否经 core ops 再出口 | [ffi/qwen4.rs:L36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/qwen4.rs#L36) 声明、kernels ops 包装 | 待查 | **待确认** |

4. 对「待确认」行做交叉验证（本讲义范围外的方法）：
   - 在仓库根运行 `grep -rn "rms_norm_grouped" pegainfer-core/src/ops.rs pegainfer-qwen3/src/`，看该符号是否出现在 core 的再出口清单或模型 crate 的直接 ffi 调用里；
   - 用 `git grep -n <符号名> HEAD -- ':(top)pegainfer-kernels' ':(top)pegainfer-qwen3'` 做全仓追踪；
   - 查模型 crate 对门面的引用：`grep -rn "pegainfer_core::ffi" pegainfer-qwen3/src/`——ffi 门面的真正消费面在模型 crate，本讲作者的沙箱未能覆盖，故留作交叉验证。
5. **预期结果**：一张每行都有证据（命令输出或永久链接）的边界清单，以及至少一个诚实标注「待确认」的条目。这张表就是你在 u2-l3 精读 ops 时的导航图。

## 6. 本讲小结

- `ffi.rs` 与 `tensor.rs` 是一对**一行再出口门面**：core 拥有稳定公开路径，kernels 拥有全部实现——单一事实源防签名漂移，编译边界把 CUDA 工具链知识隔离在 kernels。
- FFI 真身是 `pegainfer-kernels/src/ffi/` 的 18 个文件：按拥有内核的模型线拆分、cargo feature 门控挂载、`Half = u16` 共享 ABI、统一拍平进单一命名空间。
- 四步闭环：**声明**（`unsafe extern "C"`）→ **编译**（build.rs 调 nvcc，SM 自动探测 + gencode 探针）→ **链接**（`static=kernels_cuda` + 厂商库动态链接）→ **调用**（kernels ops 安全包装 → core ops 再出口 → 模型 crate）。
- 错误契约：kernel 入口返回 `CUresult`，文本细节经 `pegainfer_kernels_last_error()` 出口取回——声明、实现、测试、消费四点都能在源码里按行号对上。
- 反直觉事实：core 内部**零** `crate::ffi` 消费——ffi 门面专为下游模型 crate 服务；core 自身经 ops 与 tensor 使用 GPU 能力。
- 三条 FFI 边界辨析：CUDA kernel FFI（kernels::ffi）、宿主 libc FFI（cpu_topology 的 sched_getaffinity 等）、`std::ffi` 工具类型（CStr/c_char）——名字相似，身份完全不同。

## 7. 下一步学习建议

1. **下一讲 u2-l3（ops.rs 总览）**：本讲的 ffi 声明在 kernels ops 层被包装成安全函数、再被 core 大宗再出口——u2-l3 精读这条封装链，重点是 kernel-call-trace feature 的 cfg 换出口机制（[core/src/ops.rs:L34-L35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/ops.rs#L34-L35) 已埋下伏笔）。
2. **继续阅读源码**：`pegainfer-kernels/src/ffi/shared.rs` 的头部（跨模型符号清单）与 `pegainfer-kernels/tests/ffi_exception_catch.rs` 全文（80 余行看完一个 FFI 错误契约测试）。
3. **远景衔接**：u4-l2 会回到 cpu_topology.rs 的宿主侧 FFI（libc + cudarc driver API），与本讲的 CUDA FFI 形成「设备侧 / 宿主侧」两条边界的完整对照。
