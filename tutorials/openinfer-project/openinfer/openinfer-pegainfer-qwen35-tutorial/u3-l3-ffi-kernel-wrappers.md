# FFI 边界与内核封装：ops.rs 与 recurrent.rs 的 unsafe 调用

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出一条 FFI 符号（如 `gated_delta_rule_prefill_chunk_prepare_cuda`）从 `recurrent.rs` 的调用点回到内核声明处的**三级重导出链**，并解释每一级各自承担什么职责。
2. 解释 `device_ptr` / `device_ptr_mut` 返回的 `(指针, guard)` 二元组中，**guard 为什么必须存活到 `unsafe` 调用结束**，并区分本 crate 里两种 guard 绑定风格的差别。
3. 对照 prepare → cumsum → A → solve → recompute → state → output 七个内核，说清 GDR 分块预填充流水线每一步读什么、写什么、错在哪一步如何报告。
4. 区分本 crate 算子面的**三个来源**（pegainfer-core 通用算子、pegainfer-kernels 专属算子、本 crate 自己的封装），以及内核的**两种错误返回约定**（`i32` 错误码 vs `CUresult`）。

## 2. 前置知识

**FFI 是什么。** Rust 代码与 C/C++（这里是 CUDA C++）编译出的机器码互相调用，靠的是 FFI（Foreign Function Interface，外部函数接口）。C 侧的函数没有 Rust 的借用检查、没有生命周期，只有「裸指针 + 字节布局」。Rust 用 `unsafe extern "C" { ... }` 声明这些符号（`extern "C"` 指定 C 调用约定），调用它们必须写在 `unsafe` 块里——编译器在提醒你：从这里开始，「指针指向的内存有效、不越界、不别名冲突」全靠你自己保证。

**RAII 与 guard 复习。** Rust 惯用的资源管理方式是「对象的 Drop 时机就是资源归还时机」。一个 *guard*（守卫值）本身不携带数据，它的唯一作用是**占住一段生命周期**：guard 活着，某种「借出」关系就成立；guard 被销毁，借出关系就结束。本讲会反复看到 `let (ptr, _guard) = ...` 这种模式——第二个元素看似没用（变量名以下划线开头，从不读取），但它决定了指针的「借出凭据」活多久。

**承接前两讲。** u3-l1 讲了全注意力层如何调用 hd256 内核族，u3-l2 讲了 GDN 状态（f32 `[H,K,V]` 递推状态 + bf16 conv_state）如何外置于调度器。本讲放大两者共用的那层「胶水」：内核符号从哪里来（FFI 重导出）、裸指针怎么安全地拿到手（guard 模式）、以及线性注意力 prefill 的七个内核如何被编排成一条流水线（`recurrent.rs` 的 `gated_delta_rule_prefill_chunkwise_into`）。一句话：**前两讲看的是「内核算什么」，本讲看的是「内核怎么被 Rust 安全地叫起来」**。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|------|------|------|
| `src/ffi.rs` | 1 | 本 crate 的 FFI 别名：一行 `pub(crate) use pegainfer_core::ffi::*;` |
| `src/ops.rs` | 29 | 算子面汇总：三来源 re-export 的「目录页」 |
| `src/recurrent.rs` | 1360 | 本讲主角：conv1d prefill、GDR 单槽 decode（测试用）、GDR chunkwise 七段流水线 + 全部单测 |
| `src/prefill_buffers.rs` | 182 | `GdrChunkwiseScratch35`：流水线全部 scratch 缓冲的定义与尺寸公式 |
| `src/prefill.rs` | 598 | 调用点：`prefill_linear_attention` 里的 conv 与 chunkwise 两步 |
| `src/lib.rs` | 416 | `mod ffi` / `mod ops` 声明与 `runtime_ops` 公开面 |
| `../pegainfer-core/src/ffi.rs` | 1 | 第二级重导出：`pub use pegainfer_kernels::ffi::*;` |
| `../pegainfer-kernels/src/ffi.rs` | 51 | 第三级（符号真正声明处）：`Half` ABI 类型 + 按 feature 分文件的外部符号 |
| `../pegainfer-kernels/src/ffi/qwen35.rs` | 295 | qwen35 专属内核的 `unsafe extern "C"` 声明（nvcc 内核 + Triton AOT 内核两个块） |
| `../pegainfer-kernels/src/ops/qwen35.rs` | 650+ | kernels 侧的批量 decode 封装（对照本 crate 封装风格的参照物） |

## 4. 核心概念与源码讲解

### 4.1 FFI 重导出：符号的单一来源与三级链条

#### 4.1.1 概念说明

Qwen3.5 的 GPU 内核（CUDA C++ / Triton 生成的 cubin）最终都编进 `pegainfer-kernels` 这个 crate 的静态库；它们的外部符号声明集中在 `pegainfer-kernels/src/ffi/` 下、按模型线分文件。但模型 crate `pegainfer-qwen35` 调用内核时写的却是 `ffi::xxx_cuda`，而且 `use crate::ffi`——这个 `ffi` 不是 kernels 的路径，而是**本 crate 自己的一行模块**。为什么隔两层转发？

- **符号只有一个声明处**：`pegainfer-kernels/src/ffi.rs` 是全部外部符号的唯一所有者，`Half` 这个共享 ABI 类型也在这里定义。模型 crate 永远不直接写 `pegainfer_kernels::ffi::...` 调内核（ops 封装除外）。
- **core 提供统一门面**：`pegainfer-core/src/ffi.rs` 把 kernels 的整个 FFI 面 re-export 一遍，让依赖 core 的模型 crate 都从同一条路径拿符号。
- **模型 crate 再缩短一次**：`src/ffi.rs` 让本 crate 的代码写 `crate::ffi` 即可，模型代码不感知「符号住在哪个 crate」。

这和 u1-l3 说的「模块可见性即 API 边界」一脉相承：三级都是一行 `use ...::*`，但每一行都在固定「谁依赖谁」的方向。

#### 4.1.2 核心流程

一个符号从调用到声明的解析路径：

```text
recurrent.rs:117  ffi::gated_delta_rule_prefill_chunk_prepare_cuda(...)
   └─ crate::ffi            （src/ffi.rs:1      pub(crate) use pegainfer_core::ffi::*）
       └─ pegainfer_core::ffi（core/src/ffi.rs:1 pub use pegainfer_kernels::ffi::*）
           └─ pegainfer_kernels::ffi（kernels/src/ffi.rs:48-49  pub use qwen35::*）
               └─ mod qwen35  （kernels/src/ffi/qwen35.rs:198  unsafe extern "C" 声明）
```

同时，`src/ops.rs` 是**算子面**（Rust 封装函数）的目录页，它把三来源汇成一张表：

| 来源 | re-export 行 | 例子 | 特点 |
|------|--------------|------|------|
| pegainfer-core 通用算子 | `ops.rs:3-19` | `gemm`、`gemm_rows_into`、`rms_norm_gated_batch_into`、`add_batch` | 所有模型线共享 |
| pegainfer-kernels 专属算子 | `ops.rs:20-25` | `conv1d_decode_batch_into`、`gated_delta_rule_decode_batch_into`、hd256 注意力族 | 只有 qwen35 feature 下存在 |
| 本 crate 自己的封装 | `ops.rs:26-27` | `conv1d_prefill_batch_into`、`gated_delta_rule_prefill_chunkwise_into` | 在 `recurrent.rs` 里手写，本讲 4.3 的主角 |

#### 4.1.3 源码精读

本 crate 的 FFI 别名只有一行，但它是三级链的起点——`recurrent.rs` 顶部的 `use crate::ffi`（[src/recurrent.rs:9](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L9)）就落在这里：

[src/ffi.rs:1](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ffi.rs#L1)
```rust
pub(crate) use pegainfer_core::ffi::*;
```
这一行把 core 的 FFI 面以 `pub(crate)` 可见性引入本 crate——外部 crate 看不到它，符合「内核符号只给本 crate 的封装层用」的边界。

第二级在 core，同样是一行转发，把 kernels 的符号面变成 core 公共 API 的一部分：

[../pegainfer-core/src/ffi.rs:1](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/ffi.rs#L1)
```rust
pub use pegainfer_kernels::ffi::*;
```

第三级是符号真正的家。kernels 的 FFI 根文件定义了共享 ABI 类型 `Half`，并按 feature 把各模型线的声明文件挂进来、再整体 re-export——qwen35 的块被 `#[cfg(feature = "qwen35")]` 门控，与 u1-l2 讲的「feature 跨 crate 联动」一致：

[../pegainfer-kernels/src/ffi.rs:4](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ffi.rs#L4-L8)
```rust
// Half type (16-bit float) - same layout as CUDA half. Shared ABI type used by all submodules.
pub type Half = u16;
...
#[cfg(feature = "qwen35")]
mod qwen35;
```
注意 `Half = u16`：它只是一个「16 位位模式」的透明载体，不带任何浮点语义。Rust 侧的 `bf16` 张量取到裸指针后直接 `as *const ffi::Half` 强转（见 4.2），位模式一个比特都不变，内核端按 bf16 解释——`recurrent.rs` 的单测用 bf16 输入喂内核并逐位对拍（4.3 的测试），证明这条位级通道是对的。

`ops.rs` 则是三来源算子面的汇总目录。读它的 re-export 就能看出一个细节：大多数条目是 `pub(crate) use`，只有三个是 `pub use`——因为它们要经 `lib.rs` 的 `runtime_ops` 侧门供给本 crate 的 bench 使用（u1-l3 讲过这个「三层公开面」）：

[src/ops.rs:20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L20-L29)
```rust
pub(crate) use pegainfer_kernels::ops::conv1d_decode_batch_into;
pub(crate) use pegainfer_kernels::ops::gated_delta_rule_decode_batch_into;
...
pub(crate) use recurrent::conv1d_prefill_batch_into;
pub use recurrent::gated_delta_rule_prefill_chunkwise_into;
```
对应 [src/lib.rs:76](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L76-L80) 里 `runtime_ops` 的 `pub use crate::ops::gated_delta_rule_prefill_chunkwise_into;`。

#### 4.1.4 代码实践

1. **实践目标**：亲手走通一条符号解析链，验证「三级重导出」不是纸面描述。
2. **操作步骤**：
   - 从 [src/recurrent.rs:117](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L117) 的 `ffi::gated_delta_rule_prefill_chunk_prepare_cuda` 出发；
   - 依次打开 `src/ffi.rs` → `../pegainfer-core/src/ffi.rs` → `../pegainfer-kernels/src/ffi.rs`（第 24-25 行的 `mod qwen35` 与第 48-49 行的 `pub use qwen35::*`）→ `../pegainfer-kernels/src/ffi/qwen35.rs` 第 198 行的声明；
   - 再用编辑器的「查找所有引用」数一数 `gated_delta_rule_prefill_chunk_prepare_cuda` 在整个仓库出现几次、分别在哪几个文件（应当只在 kernels 的声明处与本 crate 的调用处出现）。
3. **需要观察的现象**：没有任何模型代码绕过 `crate::ffi` 直接写 `pegainfer_kernels::ffi::` 调内核；kernels 侧的声明带完整参数注释。
4. **预期结果**：得到一条五跳的解析链草图；`ffi/qwen35.rs` 里两个 `unsafe extern "C"` 块（第 8 行起是 nvcc 编译的 `.cu` 内核，第 197 行起是 Triton AOT 内核）边界清晰。
5. 本实践纯静态阅读，无需构建工具链。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Half` 定义在 `pegainfer-kernels/src/ffi.rs` 的根部，而不是某个模型子模块里？

**答案**：注释写明它是 "Shared ABI type used by all submodules"——所有模型线的内核声明都用 16 位裸位模式传半精度数据，定义在根部让各子模块共享同一类型别名，避免每个文件各写一个 `u16` 别名造成 ABI 类型漂移。

**练习 2**：`src/ops.rs` 里 `gated_delta_rule_prefill_chunkwise_into` 用 `pub use`，而 `conv1d_prefill_batch_into` 用 `pub(crate) use`，差别的原因是什么？

**答案**：前者要被 `lib.rs` 的 `runtime_ops` 模块再 re-export 一次，供本 crate 的 criterion bench（`benches/qwen35_ops.rs`）直接调用，所以必须对 crate 外可见；后者只在 crate 内部（`prefill.rs`）使用，`pub(crate)` 足够。

**练习 3**：如果把 `pegainfer-qwen35` 的 feature 关掉（默认构建），`ffi::gated_delta_rule_prefill_chunk_prepare_cuda` 这个路径还存在吗？

**答案**：不存在。整个 crate 被 `#![cfg(feature = "qwen35")]` 门控（[src/lib.rs:7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L7)），feature 关闭时编译为空 crate；同时 kernels 侧 `mod qwen35` 也是 feature 门控的，符号声明本身也不存在——这正是 u1-l2 讲的「模型代码与内核产物同进同退」。

### 4.2 指针 guard 生命周期：借出凭据必须活过 unsafe 发射

#### 4.2.1 概念说明

Rust 侧的张量是 `CudaSlice<T>` / `HiddenStates`（内含 `CudaSlice`），内核要的却是裸设备地址。cudarc（workspace 锁定 0.19.x）的 `DevicePtr` / `DevicePtrMut` trait 提供了桥：

```rust
let (qkv_ptr, _gqkv) = qkv.data.device_ptr(&ctx.stream);
```

返回值是**二元组**：

- `qkv_ptr: u64`——裸设备地址，一个普通的数字，内核拿它当指针用；
- `_gqkv`——**guard（借出凭据）**。`device_ptr` 之所以要传 `&ctx.stream`，就是要把这次「借出」登记到那条流上；guard 存活期间，cudarc 的内存安全记账知道这段内存正在被该流使用（例如判断释放是否需要与某条流排序）。guard 被销毁，借出登记随之结束。

关键规则：**裸指针只在凭据（以及底层所有者）存活期间有效**。guard 若在内核发射前就被销毁，指针就成了「无主地址」——Rust 编译器不会拦你，因为 `u64` 没有生命周期。因此封装层的写法是：所有 guard 都用 `let` 绑定到**函数作用域**，让它们活过 `unsafe` 块、直到函数返回。

还有一层更基础的保证来自借用检查本身：封装函数的签名是 `&HiddenStates`、`&mut CudaSlice<f32>`，所有者（调用方的张量）在整个函数调用期间必然存活，所以裸指针指向的分配不会中途消失。guard 是叠在借用检查之上的**第二层防线**（多流记账），不是唯一防线——理解这个分层，才能读懂本 crate 里的两种写法差异（见下面 4.2.3 末尾的对照）。

#### 4.2.2 核心流程

一次典型封装（`conv1d_prefill_batch_into`）的骨架：

```text
fn conv1d_prefill_batch_into(ctx, x_seq, conv_weight, conv_state, out_seq, kernel_size):
    1. 形状断言（assert_eq!）：channel 数、seq_len、权重长度、状态长度
    2. 逐张量取裸指针：let (ptr, _guard) = t.device_ptr(&ctx.stream)   // guard 绑定到函数作用域
    3. unsafe { ffi::conv1d_prefill_cuda(ptr..., ..., ctx.stream.cu_stream()) }  // 所有 guard 仍存活
    4. 函数返回 → guard 销毁 → 借出登记结束
```

注意第 3 步还传了 `ctx.stream.cu_stream()`——**内核必须发射在借出登记的同一条流上**，这是「流归属」约定：guard 登记在 `ctx.stream`，发射也在 `ctx.stream`，两者一致。

`device_ptr` 与 `device_ptr_mut` 的区别对应只读/可写：输入张量（`&HiddenStates`）用 `device_ptr`，会被内核写入的张量（`&mut ...`）用 `device_ptr_mut`。Rust 的 `&`/`&mut` 区分在 FFI 边界处降级为「约定」——C 侧没有别名检查，靠封装层保证不把同一块可写内存同时交给两个内核参数（例外见 4.3 的原地 cumsum，那是刻意的 in-place）。

#### 4.2.3 源码精读

`recurrent.rs` 顶部引入这两个 trait，是全文件所有封装的公共前提：

[src/recurrent.rs:2](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L2-L4)
```rust
use cudarc::driver::CudaSlice;
use cudarc::driver::DevicePtr;
use cudarc::driver::DevicePtrMut;
```

以 conv1d 的 prefill 封装为例——先断言形状，再把每个张量的指针**连同 guard 一起**绑定到函数作用域，最后在所有 guard 存活时发射内核：

[src/recurrent.rs:64](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L64-L86)
```rust
let num_channels = x_seq.hidden_dim;
assert_eq!(out_seq.hidden_dim, num_channels);
assert_eq!(out_seq.seq_len, x_seq.seq_len);
assert_eq!(conv_weight.len, num_channels * kernel_size);
assert_eq!(conv_state.len, num_channels * (kernel_size - 1));

let (x_ptr, _gx) = x_seq.data.device_ptr(&ctx.stream);
let (w_ptr, _gw) = conv_weight.data.device_ptr(&ctx.stream);
let (s_ptr, _gs) = conv_state.data.device_ptr_mut(&ctx.stream);
let (o_ptr, _go) = out_seq.data.device_ptr_mut(&ctx.stream);

unsafe {
    ffi::conv1d_prefill_cuda(
        x_ptr as *const ffi::Half,
        ...
        ctx.stream.cu_stream(),
    );
}
```
这段代码把「四个张量、四份 guard、一次发射」的完整模式摆在同一个屏幕里：`_gx/_gw/_gs/_go` 四个绑定都活到函数结尾，覆盖了 `unsafe` 发射窗口；断言在取指针**之前**完成，坏形状在进入 FFI 前就 panic，不会带着错误布局发射内核。

对照 kernels 侧的批量 decode 封装，能看到同一模式加上「band（列带）」扩展——beta/alpha 可能是融合投影里的一段列，指针要在基地址上加列偏移，而**槽间距（stride）用的是宿主张量的行宽**：

[../pegainfer-kernels/src/ops/qwen35.rs:568](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/qwen35.rs#L568-L576)
```rust
let (b_base, _gb) = b_proj.states.data.device_ptr(&ctx.stream);
let b_ptr = b_base + (b_proj.col * std::mem::size_of::<bf16>()) as u64;
...
let (o_ptr, _go) = output.data.device_ptr_mut(&ctx.stream);
```
band 的合法性由 `Columns::assert_in_row` 在取指针前检查（[../pegainfer-kernels/src/tensor.rs:776](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L776-L791)），否则「从 col 起按行跨距读 width 列」会越过最后一行的末尾。`recurrent.rs` 里的 `assert_band_rejected` 测试（[src/recurrent.rs:1041](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L1041-L1054)）专门验证「越界 band 在发射前就 panic」。

最后是**两种绑定风格的对照**——这是本小节最值得盯的细节：

| 风格 | 代码 | guard 生命周期 | 出现位置 |
|------|------|----------------|----------|
| 命名绑定 | `let (qkv_ptr, _gqkv) = ...` | 活到**函数作用域结束**，覆盖 unsafe 发射 | `recurrent.rs` 全部封装（如 [L105-L114](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L105-L114)） |
| 立即丢弃 | `let (qf_ptr, _) = ...` | `let` 语句结束即销毁，早于 unsafe 发射 | `prefill.rs` Step 1（[L339-L349](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L339-L349)） |

`prefill.rs` 的 `_` 写法在这里是安全的：所有宿主张量是**同一函数里的局部变量**，从取指针到发射在同一条流上的同一个代码块内完成，所有者的存活范围完全覆盖发射窗口。但它是比 `recurrent.rs` 更紧的写法——一旦有人在两个 `let` 之间插入「把宿主张量 move 走 / 换流」的代码，命名绑定的版本还能靠 borrow checker 与 guard 语义兜住一部分。**写新封装时照 `recurrent.rs` 的命名绑定抄，是本 crate 的防御性惯例**。

#### 4.2.4 代码实践

1. **实践目标**：用一个最小实验分辨「guard 存活」与「guard 立即销毁」两种写法，并确认 guard 并非唯一安全防线。
2. **操作步骤**：
   - 阅读 [src/recurrent.rs:28](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L28-L51)（`gated_delta_rule_decode_vec_into`，测试专用）：数一数它取了几个指针、绑了几个 guard，最后一个 `unsafe` 调用距 guard 绑定隔了多远；
   - 再读 [src/prefill.rs:339](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L339-L373)：Step 1 的全部 `let (ptr, _)` 与发射在同一个 `unsafe` 块内；
   - 在草稿上回答：若把 `prefill.rs:339` 的 `_` 改成 `_g0` 这样命名绑定，行为有任何可观察差异吗？（提示：没有——两者生成的机器码相同，差别只在借出登记的结束时机。）
3. **需要观察的现象**：两种风格的宿主张量都在发射窗口内存活；差别只在 guard（借出登记）的销毁时机。
4. **预期结果**：能口头说出「guard 立即销毁之所以无恙，是因为所有者覆盖发射窗口且单流」；能指出新代码应采用哪种风格。
5. 本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`device_ptr` 与 `device_ptr_mut` 各对应 Rust 的哪种借用？为什么封装层必须区分？

**答案**：分别对应 `&`（共享/只读）与 `&mut`（独占/可写）。FFI 边界没有别名检查，C 侧只看到 `*const`/`*mut` 裸指针；封装层用「输入张量取 `device_ptr`、输出/状态张量取 `device_ptr_mut`」的纪律，把 Rust 的借用区分降级成可审计的约定，避免把同一块可写内存同时当两个可写参数传给内核。

**练习 2**：为什么内核发射必须用 `ctx.stream.cu_stream()`，而不能随便拿一条别的流？

**答案**：guard 的借出登记在 `ctx.stream` 名下；发射、登记、以及上层（如 `unified_forward` 的 SharedSM 重叠）对流的所有假设必须指向同一条流，否则「谁在哪条流上用了这块内存」的记账与实际执行分家，u4-l4 会看到违反这一点的后果（cuBLAS 句柄共享风险）。

**练习 3**：guard 变量名都以 `_` 开头（`_gx`、`_gs`），这和练习里的 `let (p, _)` 中的 `_` 是一回事吗？

**答案**：不是。`_gx` 是**正常绑定**（下划线前缀只关闭「未使用」警告），值活到作用域结束；模式位置的 `_` 是**通配符**，不绑定，右值当场销毁。一字之差，生命周期完全不同。

### 4.3 GDR 分块流水线：七个内核的编排与契约

#### 4.3.1 概念说明

u3-l2 讲过 GDN 的每 token 递推：状态 \(S\) 做门控衰减 + delta 写入。decode 时一次一个 token，递推天然串行、一个内核就够（`gated_delta_rule_decode_batch_cuda`）。但 prefill 面对的是几千上万个 token 的整段提示词——逐 token 递推等于把 decode 跑 N 遍，太慢。

**chunkwise（分块）算法**把序列切成 `CHUNK_SIZE = 64` 的块，把「块内 T 个串行递推」分解成一组可并行的矩阵运算：块内两两交互矩阵 A 及其逆 \((I+A)^{-1}\)、每块一次的状态更新、以及每块的输出累积。代价是必须**显式编排多个内核**、并预先分配它们之间传递的全部中间张量——这就是 `GdrChunkwiseScratch35` 存在的原因（注释原话："naturally a multi-stage pipeline rather than one opaque kernel launch"，[src/prefill_buffers.rs:20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L20-L21)）。

这七个内核由 build 期的 Triton AOT 生成（u1-l2 讲过那条工具链），声明在 kernels FFI 文件里独立于 nvcc 内核的第二个 `unsafe extern "C"` 块，注释明确标注 "Triton AOT-generated at build time"：

[../pegainfer-kernels/src/ffi/qwen35.rs:196](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ffi/qwen35.rs#L196-L198)
```rust
// Chunk-wise GDR prefill kernels, Triton AOT-generated at build time.
unsafe extern "C" {
    pub fn gated_delta_rule_prefill_chunk_prepare_cuda(
```

两个贯穿性契约（来自 kernels 侧的常量注释）：头维被 AOT 特化**写死为 128**（`GDN_AOT_KEY_HEAD_DIM` / `GDN_AOT_VALUE_HEAD_DIM`，[../pegainfer-kernels/src/ops/qwen35.rs:499](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/qwen35.rs#L499-L509)，注释说明 prefill 的 Triton 特化键同样硬编码），所以 u2-l1 的加载期维度门（GDN 128/128）正是与这里对拍的另一侧。

#### 4.3.2 核心流程

七个阶段按序执行，`gated_delta_rule_prefill_chunkwise_into` 是唯一入口（[src/recurrent.rs:341](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L341)）：

| # | 封装函数（recurrent.rs） | 读 | 写 | 数学角色 |
|---|--------------------------|----|----|----------|
| 1 | `..._chunk_prepare_into` (L90) | 融合 qkv（post-conv）、b、a、dt_bias、a_log | `q_expanded`、`k_expanded`、`v_raw`、`g`、`beta` | GQA 展开 q/k 到 value-head 布局，融合算出门控 log 值 g 与 β |
| 2 | `..._chunk_cumsum_inplace` (L139) | `g`（原地） | `g_cumsum` | 累积和 \(G_i = \sum_{t \le i} g_t\)，块内 (i,j) 衰减即 \(\exp(G_i - G_j)\) |
| 3 | `..._chunk_a_into` (L159) | `k_expanded`、`g_cumsum`、`beta` | `a_tril` | 块内下三角注意力矩阵 A（k·k 相似度 × β × 衰减） |
| 4 | `..._chunk_solve_into` (L186) | `a_tril` | `a_inv`（bf16） | 求逆 \((I+A)^{-1}\)——delta 规则「先扣旧值再写入」的块内解耦 |
| 5 | `..._chunk_recompute_into` (L208) | `k_expanded`、`v_raw`、`beta`、`a_inv`、`g_cumsum` | `w`、`u` | 校正后的 key/value 侧张量 w、u |
| 6 | `..._chunk_state_stage_into` (L246) | `k_expanded`、`w`、`u`、`g_cumsum`、`state`（初值） | `state`（终值）、`chunk_state`、`v_new` | **唯一触碰持久状态的一步**：每块一次状态更新 + 每块状态快照 |
| 7 | `..._chunk_o_stage_into` (L292) | `q_expanded`、`k_expanded`、`v_new`、`chunk_state`、`g_cumsum` | `output` | 输出累积：q 读「块前状态 + 块内项」，scale \(= 1/\sqrt{d_k}\) |

示意（具体符号约定以内核为准）：

\[
S_{\text{终}} = \text{衰减}(g) \cdot S_{\text{初}} + \sum_{\text{块内 } t} (\text{delta 校正后的写入})
\]

\[
o_i = \frac{1}{\sqrt{d_k}} \, q_i \cdot \left( S_{c(i)}^{\text{块前}} + \text{块内}_{j \le i} \text{项} \right)
\]

伪代码（入口编排）：

```text
gated_delta_rule_prefill_chunkwise_into(ctx, qkv, b, a, dt_bias, a_log, state, scratch, output, Hk, Hv, dk, dv):
    入口断言：scratch 各缓冲的 seq_len == qkv.seq_len
              各 hidden_dim == Hv*dk 或 Hv*dv
              g/beta/a_tril/a_inv/chunk_state 长度公式全部对上
    prepare(...)   → cumsum(...)  → a(...)   → solve(...)
    → recompute(...) → state_stage(state 初→终)  → o_stage(output, scale=1/√dk)
    每步失败都在 map_err 里带上阶段名，向上传播 anyhow 错误
```

scratch 的尺寸全部由 `seq_len` 与头数推出（`GdrChunkwiseScratch35::from_dims`，[src/prefill_buffers.rs:69](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L69-L114)），块数公式 `num_chunks = seq_len.div_ceil(64)`（[L116-L118](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L116-L118)）。这也解释了 u4-l1 会讲的 `PREFILL_CHUNK_LEN`：scratch 随长度线性伸缩，所以预填充要分块，让每块的 scratch 不超过启动时按 `SCRATCH_ESTIMATE_SEQ = 20_000` 预留的量（[src/prefill.rs:12](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L12-L20)）。

另外注意**两种错误返回约定**并存：

- Triton AOT 内核（本流水线七个）返回 `CUresult`，封装里 `result.result()?` 转成 `Result` 向上传播（[src/recurrent.rs:135](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L135)）；
- nvcc 编译的 `.cu` 内核（如 `batch_prefill_paged_cuda_hd256`）返回 `i32` 错误码，调用点用 `anyhow::ensure!(result == 0, ...)` 加上 `pegainfer_kernels::ops::ffi_exception_message(result)` 把码翻译成文字（[src/prefill.rs:418](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L418-L422)）。

#### 4.3.3 源码精读

**入口断言块**——chunkwise 入口先对 scratch 的每个缓冲做形状对拍，这是「校验集中在唯一入口」的落地：七个 stage 封装自己几乎不做断言，契约由入口统一背书：

[src/recurrent.rs:356](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L356-L378)
```rust
assert_eq!(scratch.q_expanded.seq_len, qkv.seq_len);
...
assert_eq!(scratch.q_expanded.hidden_dim, num_value_heads * key_dim);
...
let expected_chunk_state_len =
    GdrChunkwiseScratch35::num_chunks(qkv.seq_len) * num_value_heads * val_dim * key_dim;
assert_eq!(scratch.chunk_state.len(), expected_chunk_state_len);
```
对照 `conv1d_prefill_batch_into`（4.2.3）自带四个断言、state/o 两个 stage 有局部断言（[L258-L262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L258-L262)、[L304-L308](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L304-L308)），可以看到断言放在「信息最全的那一层」：chunkwise 入口同时看得见 scratch 全部字段与几何参数，就由它统一校验。

**prepare 封装**——4.2 的 guard 模式在多输出内核上的样子：五个输入指针、五个输出指针，十个 guard 全部命名绑定到函数作用域，`unsafe` 结束后用 `result.result()?` 上报错误：

[src/recurrent.rs:105](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L105-L136)
```rust
let (qkv_ptr, _gqkv) = qkv.data.device_ptr(&ctx.stream);
...
let (q_out_ptr, _gqo) = q_out.data.device_ptr_mut(&ctx.stream);
...
let result = unsafe {
    ffi::gated_delta_rule_prefill_chunk_prepare_cuda(
        qkv_ptr as *const ffi::Half,
        ...
        qkv.hidden_dim as i32,
        qkv.seq_len as i32,
        ctx.stream.cu_stream(),
    )
};
result.result()?;
```

**原地 cumsum**——同一个指针同时以 `*const` 与 `*mut` 传入，这是刻意的 in-place（读 g、写 cumsum、同一块内存），是 4.2 说「不把同一块可写内存交两个参数」规则的**显式例外**，由内核契约声明：

[src/recurrent.rs:145](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L145-L153)
```rust
let (g_ptr, _gg) = g_cumsum.device_ptr_mut(&ctx.stream);
let result = unsafe {
    ffi::gated_delta_rule_prefill_chunk_cumsum_cuda(
        g_ptr as *const f32,
        g_ptr as *mut f32,
        ...
```

**state 阶段**——整条流水线唯一读写持久递推状态的一步：请求的 `state` 分配被同时传成 `initial_state`（const）与 `final_state`（mut），内核先读旧状态、算完写入同一地址；`chunk_state`（每块快照）与 `v_new` 是 scratch。kernels 侧声明的注释给出了精确形状（`[num_value_heads, 128, 128] fp32 in [H, K, V]` 等）：

[../pegainfer-kernels/src/ffi/qwen35.rs:255](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ffi/qwen35.rs#L255-L262)
```rust
// Chunk-wise GDR prefill stage 1 (Triton AOT): recurrent chunk-state update.
//   initial_state / final_state: [num_value_heads, 128, 128] fp32 in [H, K, V] (V contiguous)
pub fn gated_delta_rule_prefill_chunk_state_cuda(
```
对应封装侧 [src/recurrent.rs:268](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L268) 与 [L278](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L278-L281)——`s_ptr` 两次出现，一次 `as *const f32`、一次 `as *mut f32`。

**编排与错误上下文**——入口按序调用七步，每步 `.map_err` 都拼上阶段名（"GDR prefill prepare failed" 等），排障时一眼定位是哪一环失败；最后一步把 `1.0 / (key_dim as f32).sqrt()` 作为 scale 传给 o 阶段：

[src/recurrent.rs:380](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L380-L395)（prepare 调用，`.map_err(|e| anyhow::anyhow!("GDR prefill prepare failed: {e}"))`）
[src/recurrent.rs:444](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L444-L455)（o 阶段调用，`1.0 / (key_dim as f32).sqrt()`）

**调用点**——`prefill.rs` 的线性层前向里，conv 与 chunkwise 一先一后：conv 先消费 `qkv_batch`（写 `conv_state` 的末尾抽头），产出 post-conv 的 `qkv_conv_batch` 再喂 chunkwise；chunkwise 的 `state` 参数取自该请求该层的 `layer_state.state`（u3-l2 的外置状态）：

[src/prefill.rs:504](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L504-L531)
```rust
let layer_state = &mut recurrent.layers[*linear_idx];

let mut qkv_conv_batch = HiddenStates::zeros(&self.ctx, qkv_dim, seq_len)?;
ops::conv1d_prefill_batch_into(
    &self.ctx, &qkv_batch, &attn.conv1d_weight,
    &mut layer_state.conv_state, &mut qkv_conv_batch, c.linear_conv_kernel_dim,
);

let mut gdr_out_batch = HiddenStates::zeros(&self.ctx, z_dim, seq_len)?;
ops::gated_delta_rule_prefill_chunkwise_into(
    &self.ctx, &qkv_conv_batch, &b_batch, &a_batch,
    &attn.dt_bias, &attn.a_log, &mut layer_state.state,
    gdr_chunkwise_scratch, &mut gdr_out_batch,
    geom.local_linear_num_key_heads(), geom.local_linear_num_value_heads(),
    c.linear_key_head_dim, c.linear_value_head_dim,
)?;
```
经 `ops.rs` 的 re-export（[src/ops.rs:26-27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L26-L27)），这两个名字才在 prefill 里以 `ops::` 前缀出现——4.1 的三来源目录页在此兑现。

**等价性测试**——流水线正确性的裁决是「chunkwise 整段 == 逐 token decode」：同一段输入分别走 chunkwise 与 96 次 `gated_delta_rule_decode_vec_into`，输出与状态的差都要落在容差内：

[src/recurrent.rs:907](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L907-L979)（`gdn_chunkwise_prefill_matches_stepwise_decode_at_48_value_heads`：seq_len=96、48 value heads，断言 `max_out_diff < 0.05`、`max_state_diff < 0.05`，并要求全部输出有限）

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：为 `gated_delta_rule_prefill_chunk_prepare_into` 写一段 SAFETY 注释，把「生效前置条件 + guard 存活范围」成文，并找到它在 prefill 前向里的最终调用点。
2. **操作步骤**：
   - 读 [src/recurrent.rs:90](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L90-L137)，对照内核声明 [../pegainfer-kernels/src/ffi/qwen35.rs:198](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ffi/qwen35.rs#L198-L214)；
   - 在自己的笔记（或 `openinfer-pegainfer-qwen35-tutorial/` 下的练习文件）里写注释，至少覆盖：
     - **形状前置条件**：`qkv/b_proj/a_proj` 的 `seq_len` 一致；`qkv.hidden_dim = 2*Hk*dk + Hv*dv`（融合 [q|k|v] 布局）；`b_proj/a_proj.hidden_dim = Hv`；`dt_bias.len = Hv`；`a_log.len() = Hv`；五个输出缓冲已按入口断言块的公式分配（注意：本函数自身**没有**断言，这些条件由唯一调用者 [src/recurrent.rs:380](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L380-L395) 的入口断言背书）；
     - **流归属**：所有张量分配在 `ctx` 的设备上；发射在 `ctx.stream`，与 guard 借出登记同流；
     - **guard 存活范围**：`_gqkv` 等 10 个 guard 绑定于 [L105-L114](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L105-L114)，活到函数返回，覆盖 [L116-L134](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L116-L134) 的整个 `unsafe` 发射；
     - **错误约定**：内核返回 `CUresult`，`result.result()?` 失败即向上传播；
   - 再沿调用链反向走一遍：`prefill.rs:517 ops::gated_delta_rule_prefill_chunkwise_into` → `ops.rs:27` re-export → `recurrent.rs:341` 入口 → `recurrent.rs:380` 调 prepare → `recurrent.rs:117` 进 FFI。把这条链和你 4.1 画的符号链拼成一张全图。
3. **需要观察的现象**：prepare 封装体内无任何 `assert`；全部校验都在 chunkwise 入口的断言块；调用链每一跳都能点击跳转核对。
4. **预期结果**：一段 10 行左右的 SAFETY 注释 + 一张「prefill.rs → ops.rs → recurrent.rs → ffi 三级 → 内核」的全链路图。
5. 若想实际运行等价性测试验证理解（需要 GPU + u1-l2 的 Triton AOT 构建工具链）：`cargo test --release -p pegainfer-qwen35 --features qwen35 --lib recurrent -- --nocapture`。无 GPU 环境下本实践为纯源码阅读，**运行结果待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：七个阶段里只有 state 阶段触碰持久递推状态。如果把它挪到 solve 之前先执行，会发生什么？

**答案**：会算错。state 阶段消费的是 recompute 产出的 `w`、`u`（经 \((I+A)^{-1}\) 校正后的 key/value 侧张量），把状态更新提前等于用未校正的量更新状态，且 `chunk_state` 快照与 o 阶段读到的块前状态不再对应。顺序是算法的一部分：prepare → cumsum → A → solve → recompute → state → output。

**练习 2**：`GdrChunkwiseScratch35` 里哪个缓冲随 `seq_len` 平方增长？哪些线性？

**答案**：没有平方增长的。`chunk_state` 是 `num_chunks × Hv × dk × dv`，而 `num_chunks = seq_len.div_ceil(64)`，所以它也随 seq_len **线性**增长；`g_cumsum/beta` 线性、`a_tril/a_inv` 是 `seq_len × Hv × 64` 线性、六个 HiddenStates 全部线性（见 [src/prefill_buffers.rs:76](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L76-L99) 的分配公式）。整包 scratch 对 seq_len 是线性的——这正是 `PREFILL_CHUNK_LEN` 分块的动机。

**练习 3**：为什么 `chunkwise` 入口的错误用 `anyhow::Result` 逐阶段包装，而 conv1d prefill 封装连 `Result` 都不返回？

**答案**：conv1d 调用的 `conv1d_prefill_cuda` 返回 `()`（无错误码可报，见 [../pegainfer-kernels/src/ffi/qwen35.rs:172](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ffi/qwen35.rs#L172-L181)），失败只能表现为后续数据不对；而七个 Triton AOT 内核都返回 `CUresult`，有真实的错误码可查，封装把它转成 `Result` 并在入口按阶段附加上下文，让 prefill 调用点能用 `?` 传播。

## 5. 综合实践

**任务：为「一条 1000 token 的提示词走完一个线性注意力层的 prefill」画一张全算子序列图，并标注三来源与状态读写。**

1. 从 [src/prefill.rs:448](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L448-L531) 的 `prefill_linear_attention` 出发，列出这 1000 token 依次经过的全部算子：
   - 4 次 `ops::gemm_rows_into`（读融合权重的行段，产出 qkv/z/b/a 四个张量）——来源 core；
   - 1 次 `ops::conv1d_prefill_batch_into`——来源本 crate（`recurrent.rs`）；
   - 1 次 `ops::gated_delta_rule_prefill_chunkwise_into` 展开成七步——来源本 crate，内核为 kernels 的 Triton AOT；
   - 1 次 `ops::rms_norm_gated_batch_into`（z 门控 RMSNorm）——来源 core；
   - 1 次 `ops::gemm`（out_proj）——来源 core。
2. 对每一步标注：**读什么状态、写什么状态**（`conv_state` 末尾抽头、持久 `state` 初→终、scratch、还是纯临时张量），以及**错误如何报告**（无返回码 / `CUresult`）。
3. 标注 1000 token 下的 scratch 尺寸：套用 [src/prefill_buffers.rs:69](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L69-L99) 的公式，`num_chunks = 1000.div_ceil(64) = 16`，逐缓冲算出元素数。
4. 最后把 4.3.4 的 SAFETY 注释贴在图旁。预期产出：一张「算子 → 来源 crate → 读写状态 → 错误通道」四列表 + 一段 SAFETY 注释。整个过程不需要 GPU；若想验证第 2 步的状态交接结论，可对照 [src/recurrent.rs:907](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L907-L1037) 的等价性测试（**运行待本地验证**）。

## 6. 本讲小结

- **FFI 重导出三级链**：`crate::ffi`（[src/ffi.rs:1](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ffi.rs#L1)）→ `pegainfer_core::ffi` → `pegainfer_kernels::ffi`（符号唯一声明处，`Half = u16` 共享 ABI 类型）；`ops.rs` 则把 core 通用、kernels 专属、本 crate 封装三个来源汇成算子目录页。
- **guard 模式**：`device_ptr(&ctx.stream)` 返回 `(裸指针, 借出凭据)`；凭据要活过 `unsafe` 发射（recurrent.rs 的命名绑定是防御性惯例），真正的硬保证来自借用检查——所有者覆盖发射窗口、发射与登记同流。
- **GDR 分块流水线**：prepare → cumsum → A → solve → recompute → state → output 七个 Triton AOT 内核，scratch 预分配、随 seq_len 线性伸缩、块长 64；只有 state 阶段触碰持久递推状态（同一指针既作初值又作终值）。
- **两种错误约定**：Triton 内核返回 `CUresult`（`.result()?`），nvcc 内核返回 `i32` 码（`ensure!` + `ffi_exception_message`）。
- **校验分层**：chunkwise 入口统一断言全部 scratch 形状，七个 stage 封装自身几乎不断言；conv1d 封装则自带断言——断言放在「信息最全的那一层」。

## 7. 下一步学习建议

本讲讲完了「内核怎么被安全地叫起来」，下一讲 **u4-l1（Prefill 主链路：分块、位置校验与 last-hidden 提取）** 将把镜头拉远：`prefill_last_hidden` 如何把超长提示词按 `PREFILL_CHUNK_LEN` 切成多段、每段如何推进分页 KV 与 recurrent 状态（本讲的 conv/chunkwise 在每个 chunk 里各跑一次）、以及「先验证后变更」的错误处理策略。建议先自己重读 [src/prefill.rs:46](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L46-L59) 的 `checked_prefill_end_pos`，思考「为什么位置校验要发生在任何状态变更之前」。若想先看 decode 侧的批量指针表如何复用本讲的内核，可直接跳读 u3-l2 的 `LinearStatePointerTables` 一节再回到 u4-l2。
