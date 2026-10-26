# u2-l3 FFI 声明与跨语言错误契约

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `src/ffi` 模块的组织方式：按模型线分文件、feature 门控、统一 re-export，以及共享 ABI 类型 `Half` 的含义。
2. 说清楚**为什么 C++ 异常不能穿越 `extern "C"` 边界**，以及 `PEGAINFER_FFI_GUARD_BEGIN/END` 宏如何把异常拦截在 C++ 一侧。
3. 理解错误码的统一设计：`map_cuda_error` 把 `cudaError_t` 折叠成 `CUresult`，`PEGAINFER_CUBLAS_STATUS_BASE` 把 cuBLAS 状态垫高到不冲突的整数区间。
4. 独立追踪一次 kernel 失败的完整路径：C++ 入口写 `thread_local` 的 `last_error` → 返回 `-1` 哨兵 → Rust wrapper 读出消息 → 转成 `anyhow::Error`。

本讲是 u2-l1（DeviceContext 与张量）的直接后续：u2-l1 讲「GPU 资源长什么样」，本讲讲「Rust 与 CUDA 之间的那条边界上，调用约定和错误是怎么传递的」。

## 2. 前置知识

- **FFI（Foreign Function Interface，外部函数接口）**：让 Rust 调用 C/C++ 编译出来的函数（反之亦然）的机制。Rust 侧用 `unsafe extern "C" { ... }` 块声明「这些符号在别处定义，签名如下」。
- **ABI（Application Binary Interface，应用二进制接口）**：调用方与被调方对「参数怎么传、返回值放哪、寄存器谁负责保存」的机器级约定。`extern "C"` 指定使用平台 C ABI——它是语言中立的公共分母。
- **C++ 异常与栈展开（unwinding）**：C++ 抛异常时，运行时会沿调用栈向上「展开」，逐帧调用析构函数，直到遇到匹配的 `catch`。这个过程依赖编译器为每个可能传播异常的函数生成的元数据（landing pad、unwind 表）。
- **为什么异常不能穿越 `extern "C"`**：C ABI 没有定义跨语言栈展开的语义。Rust 的栈帧没有 C++ 的 unwind 表；一个 C++ 异常穿过 `extern "C"` 边界进入 Rust 帧，行为未定义，实践中直接 `std::terminate` → 整个进程 `abort`，连错误消息都没有。本项目 `ffi_guard.cuh` 的头注释原话就是："a foreign exception reaching Rust aborts the process with no message"。
- **`__half` / `__nv_bfloat16`**：两种 16 位浮点格式。CUDA 侧的 kernel 按其中一种解释这 16 个 bit；Rust 侧只搬运、不解释。
- **`thread_local`**：C++ 的线程局部存储。每个线程各有一份独立副本，互不干扰——`last_error` 通道正是用它实现的。
- **`CUresult` 与 `cudaError_t`**：CUDA 有两套 API——driver API（返回 `CUresult` 枚举）和 runtime API（返回 `cudaError_t` 枚举）。两套码表大部分条目数值相同但不保证一致，所以需要显式映射。
- **cudarc**：Rust 的 CUDA 绑定库，u2-l1 已介绍。本 crate 的 FFI 声明直接复用它的 `CUresult` / `CUstream` 类型。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/ffi.rs` | FFI 门面：定义共享类型 `Half`，按 feature 声明并 re-export 各子模块 |
| `src/ffi/shared.rs` | 全模型共享 kernel 的 `unsafe extern "C"` 声明（本讲的主要标本），含 `pegainfer_kernels_last_error` |
| `csrc/shared/ffi_guard.cuh` | C++ 侧防御工事：异常捕获宏、`map_cuda_error`、`PEGAINFER_CUBLAS_STATUS_BASE` |
| `csrc/shared/ffi_last_error.cu` | `thread_local` 错误消息存储与 `extern "C"` 读取入口 |
| `csrc/shared/paged_launch.cuh` | FlashInfer 注意力启动模板，展示 guard 宏的真实用法（`decode_launch`） |
| `csrc/shared/paged_attention.cu` | `paged_attention_decode_cuda` 等薄 `extern "C"` 包装 |
| `csrc/shared/flashinfer_norm.cu` | 对比样本：void 返回型与 `CUresult` 返回型入口 |
| `csrc/shared/linear.cu` | 对比样本：cuBLAS 状态映射 `cublas_status_to_error` |
| `src/ops.rs` | Rust 侧解码器：`ffi_exception_message`、`ffi_status_detail`、`CUBLAS_STATUS_BASE` |
| `src/ops/attention.rs` | wrapper 层消费错误码的实例 |
| `tests/ffi_exception_catch.rs` | 端到端验证：FlashInfer 抛异常 → `-1` 哨兵 + 消息 |

## 4. 核心概念与源码讲解

### 4.1 extern "C" 声明与 Half：Rust 侧的 FFI 模块组织

#### 4.1.1 概念说明

Rust 不能直接调用 C++（名字修饰、类布局、异常都是障碍），所以所有跨语言调用都走 C ABI：C++ 侧用 `extern "C"` 导出函数，Rust 侧用 `unsafe extern "C"` 声明同名符号。本 crate 有约两百个这样的符号，全部集中声明在 `src/ffi/` 目录，**按「拥有该 kernel 的模型线」分文件**，再由 `src/ffi.rs` 统一 re-export，让外部路径稳定为 `pegainfer_kernels::ffi::<symbol>`。

`Half` 是这条边界上的共享词汇：Rust 没有内置 16 位浮点 ABI 类型，于是用一个 `u16` 别名表示「16 bit，布局与 CUDA `half` 相同」。注意它只是**不透明容器**——Rust 从不解释这些 bit 的含义；同一份 `Half` 内存，在注意力 kernel 里被读作 `__nv_bfloat16`（见 4.1.3），在别处可能被读作 `__half`。类型正确性由 C++ 侧的模板实例化保证。

#### 4.1.2 核心流程

一个符号从声明到可调用的路径：

1. `build.rs` 把 `csrc/**/*.cu` 编成 `libkernels_cuda.a` 并链入本 crate（u1-l3）。
2. C++ 侧 `extern "C"` 函数（例如 `rms_norm_cuda`）导出未修饰的 C 符号。
3. Rust 侧 `unsafe extern "C"` 声明同名函数，参数类型逐一对齐：指针 ↔ 裸指针、`int32_t` ↔ `i32`、`cudaStream_t` ↔ `CUstream`。
4. `src/ffi.rs` 把声明所在子模块 re-export 到统一路径。
5. ops wrapper 层取张量的 `device_ptr`，配上 `active_cu_stream(ctx)`（u2-l1），发起 `unsafe` 调用。

声明的**返回类型就是错误契约的第一半**，本 crate 有三种形态：

| 返回类型 | 语义 | 典型符号 |
|----------|------|----------|
| `()`（void） | 发后不管，调用处无法感知失败 | `rms_norm_cuda` |
| `CUresult`（u32） | driver 错误码；0 = 成功 | `rms_norm_batched_dual_cuda` |
| `i32` | 整数状态：0 = 成功，`-1` = guard 捕获了异常，其余 = `cudaError_t` 或 `100000 + cublasStatus_t` | `gemm_cuda`、`paged_attention_decode_cuda` |

#### 4.1.3 源码精读

先看门面。`Half` 定义在文件最顶部，注释点明它是所有子模块共享的 ABI 类型：

- [src/ffi.rs:4-5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L4-L5) —— `pub type Half = u16;`：与 CUDA `half` 同布局的 16 位浮点别名，供所有子模块共用。

- [src/ffi.rs:7-28](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L7-L28) —— 模块声明区：每条模型线一个 `#[cfg(feature = ...)] mod X;`（`deepep`、`gemma4`、`glm52`、`k3`、`kimi`、`qwen35`、`qwen4`……），而 `lora` 与 `shared` 无 cfg，**常驻编译**。这正对应 u1-l1 讲过的 feature 门控矩阵。

- [src/ffi.rs:29-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L29-L50) —— 与声明一一对应的 `pub use X::*;` re-export。文件头两行注释说明了设计意图：公共路径不变，`pegainfer_kernels::ffi::<symbol>` 永远可解析。

再看共享声明文件里三种返回形态的真实样本：

- [src/ffi/shared.rs:6-7](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L6-L7) —— 文件级注释「Shared kernels used across all models (CUDA / cuBLAS / FlashInfer)」加上 `unsafe extern "C" {` 块的开括号。

- [src/ffi/shared.rs:8-20](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L8-L20) —— `dflash2_grouped_conv_cuda`：`CUresult` 返回形态。每个参数都是裸指针 + 整数维度 + 末尾一个 `CUstream`，这是整个 crate 的声明惯用法。

- [src/ffi/shared.rs:22-29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L22-L29) —— `rms_norm_cuda`：void 返回形态，签名末尾直接以 `);` 结束（无返回类型）。它内部委托 FlashInfer 的 `RMSNorm`，后者不抛异常，调用方也不查错。

- [src/ffi/shared.rs:307-315](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L307-L315) —— `gemm_cuda`：`i32` 状态返回形态，走 cuBLAS，失败时返回 `100000 + cublasStatus_t`（见 4.3）。

「Half 只是不透明容器」的证据在 CUDA 侧：注意 `paged_launch.cuh` 把 DType 定为 `__nv_bfloat16`，而 Rust 声明里同一个缓冲传的是 `*const Half`——bit 布局相同（都是 16 位），语义解释权完全在 kernel：

- [csrc/shared/paged_launch.cuh:18-19](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L18-L19) —— `using DType = __nv_bfloat16; using IdType = int32_t;`：Rust `Half`/`i32` 在 C++ 侧的真身。

#### 4.1.4 代码实践

**实践目标**：亲手验证「声明与实现靠符号名对齐」这一机制，并体会三种返回形态的分布。

**操作步骤**：

1. 在仓库根目录执行 `grep -c "pub fn" pegainfer-kernels/src/ffi/shared.rs`，数出共享声明的大致规模。
2. 执行 `grep -n "pub fn" pegainfer-kernels/src/ffi/shared.rs | grep -v "CUresult;$" | head -30`，观察哪些声明不是 `CUresult` 返回型。
3. 随机挑一个符号（如 `add_cuda`），在 `csrc/` 下用 `grep -rn "add_cuda" pegainfer-kernels/csrc/shared/` 找到它的 `extern "C"` 定义，逐参数比对 Rust 声明与 C++ 签名的类型对应（`*const Half` ↔ `const DType*`、`i32` ↔ `int32_t`、`CUstream` ↔ `cudaStream_t`/`void* stream`）。
4. 做一张三列小表：符号名 | Rust 返回类型 | C++ 返回类型，填 5 行样本。

**需要观察的现象**：Rust 与 C++ 的签名在参数个数、顺序、宽度上完全一致；返回类型一致或按「void ↔ ()、CUresult ↔ CUresult、int ↔ i32」对应。

**预期结果**：能对上号的符号至少 5 个；若发现任何不一致，那不是 bug 就是本讲义写错了——去提交历史里查证。

**待本地验证**：以上命令输出取决于你的 checkout，具体数字以本地为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Half` 定义在 `src/ffi.rs`（门面）而不是 `src/ffi/shared.rs`（共享声明）里？

**答案**：`Half` 是**所有**子模块（qwen35、kimi、glm52……）共用的 ABI 词汇；放在门面里，子模块用 `use super::Half;` 一行即可引用（[src/ffi/shared.rs:4](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L4)），避免跨模块循环或重复定义。这是「共享类型上移」的常见组织手法。

**练习 2**：`rms_norm_cuda` 返回 `()`，如果它内部的 kernel 启动失败了，调用方如何知道？

**答案**：无法通过返回值知道。失败会遗留在 CUDA 的 sticky 错误状态里，直到下一次显式查询（如 `cudaGetLastError`）或某个后续 kernel 失败才暴露。这是「发后不管」形态的代价；crate 中需要感知失败的入口都改用了 `CUresult` / `i32` 返回形态（如 `rms_norm_batched_dual_cuda`，见 4.3.3）。

**练习 3**：Rust 声明里的 `stream: CUstream` 在 C++ 侧有时是 `cudaStream_t`、有时是 `void*`（如 `paged_attention_decode_cuda`），为什么不炸？

**答案**：`cudaStream_t` 本身就是 `struct CUstream_st*` 的别名，`void*` 只是再抹掉一层类型；三者是同一指针值的不同书写。ABI 层面传的就是一个指针宽度整数，布局无差。

### 4.2 FFI 异常捕获：guard 宏如何把异常拦在 C++ 一侧

#### 4.2.1 概念说明

本 crate 的 C++ 侧大量使用 FlashInfer——一个 header-only 的 C++ 模板库，它的 host 侧分发代码（如按 GQA 组大小选择 kernel 变体）**会抛 C++ 异常**。而 kernel 入口又被 `extern "C"` 导出、被 Rust 直接调用。

问题在于：C++ 异常的传播依赖栈展开，而 `extern "C"` 边界另一侧的 Rust 帧没有 C++ unwind 元数据。异常一旦穿过去，进程直接 abort，没有消息、没有堆栈、没有恢复机会——对一个长跑的推理服务来说是最坏的失败方式。

解法是一个两行宏 `PEGAINFER_FFI_GUARD_BEGIN/END`：把入口的函数体包进 `try/catch`，捕获 `std::exception` 时把 `e.what()` 写进 thread-local 的 `last_error`，然后返回调用方约定的哨兵值（通常是 `-1`）；连 `catch (...)` 兜底都准备好了，非 `std::exception` 的异常也不会泄漏。

#### 4.2.2 核心流程

guard 宏展开后的控制流（伪代码）：

```text
入口函数(参数...) {
    pegainfer_ffi_set_last_error("");      // ① 进入即清空，防止读到上一调用的陈旧消息
    try {
        ... kernel 启动 / FlashInfer 分发 ...
        return <状态码>;                    // ② 正常路径：cudaError_t / CUresult / 0
    }
    catch (const std::exception& e) {
        pegainfer_ffi_set_last_error(e.what());   // ③ 消息进 thread_local 通道
        return -1;                               // ④ 哨兵告诉 Rust「去读 last_error」
    }
    catch (...) {
        pegainfer_ffi_set_last_error("unknown C++ exception");
        return -1;
    }
}
```

关键设计点有两个：

- **进入即清空**（步骤 ①）：如果没有这一步，一次成功调用返回 `-1` 之外的正数错误码时，Rust 若仍去读 `last_error`，会读到**同线程更早一次调用**留下的陈旧 `what()`，把旧账算到新头上。
- **哨兵与通道分离**：返回值只回答「有没有异常」，消息本体走旁路通道。这样 `i32` 返回型可以继续用其余取值表达 CUDA/cuBLAS 错误码，互不冲突。

#### 4.2.3 源码精读

- [csrc/shared/ffi_guard.cuh:1-3](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L1-L3) —— 头文件第一句注释就写明了动机："Catch C++ exceptions (FlashInfer throws from host-side dispatch) before they cross the extern "C" boundary — a foreign exception reaching Rust aborts the process with no message."（在异常穿越 extern "C" 边界前抓住它——FlashInfer 的 host 侧分发会抛异常；漏到 Rust 侧的异类异常会让进程无消息 abort。）

- [csrc/shared/ffi_guard.cuh:133-137](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L133-L137) —— `PEGAINFER_FFI_GUARD_BEGIN` 的定义：先 `pegainfer_ffi_set_last_error("")` 清空消息，再开 `try {`。注意 L133-134 的注释解释清空的必要性（"so a -1 seen by Rust never reads a stale what()"）。

- [csrc/shared/ffi_guard.cuh:138-147](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L138-L147) —— `PEGAINFER_FFI_GUARD_END(ret_on_throw)`：两个 catch 分支分别写 `e.what()` 与 `"unknown C++ exception"`，然后 `return ret_on_throw;`——哨兵值由每个入口自己指定（guard 不绑定 `-1`，但共享入口的惯例是 `-1`）。

真实用法在 FlashInfer 注意力启动模板里。`decode_launch` 是被所有 paged decode 入口复用的函数模板：

- [csrc/shared/paged_launch.cuh:65-94](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L65-L94) —— `decode_launch` 的签名（大量文档注释逐参数说明形状）与 L94 的 `PEGAINFER_FFI_GUARD_BEGIN`：函数体从 guard 开始。

- [csrc/shared/paged_launch.cuh:126-137](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L126-L137) —— 正常路径把 FlashInfer `BatchDecodeWithPagedKVCacheDispatched` 的返回值（一个 `cudaError_t`）`static_cast<int>` 后原样返回；随后 `PEGAINFER_FFI_GUARD_END(-1)` 收尾——**这个入口的异常哨兵就是 -1**。当 GQA 组大小不被 FlashInfer 支持时，分发代码抛异常，被这里接住。

- [csrc/shared/paged_attention.cu:14-29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_attention.cu#L14-L29) —— 最外层的薄 `extern "C"` 包装 `paged_attention_decode_cuda`：只做参数转发并固定 `HEAD_DIM=128` 模板参数，guard 在内层的 `decode_launch` 里。这是「薄导出层 + 厚模板层」的典型分层。

guard 的覆盖范围可以用一条 grep 感受：`csrc/` 下有 40+ 处 `PEGAINFER_FFI_GUARD_BEGIN`（glm52、k3、kimi、gemma4、qwen35、shared 各线都有）。另有一类入口在 guard **之前**还做参数预检，例如：

- [csrc/glm52/glm52_fp8_gemm.cu:242-252](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_fp8_gemm.cu#L242-L252) —— 入口先判空指针、非正维度、未对齐的 m/k，直接 `return CUDA_ERROR_INVALID_VALUE;`，然后才进入 guard。宿主侧可判的非法输入走 fail-fast，不需要动用异常通道。

#### 4.2.4 代码实践

**实践目标**：运行本 crate 自带的异常穿越端到端测试，亲眼看一次「FlashInfer 抛异常 → Rust 收到 -1 + 消息」。

**操作步骤**：

1. 确保有 CUDA GPU 的机器（无 GPU 时该测试会优雅跳过，见下）。
2. 在仓库根目录运行：

   ```bash
   cargo test --release -p pegainfer-kernels --test ffi_exception_catch -- --nocapture
   ```

3. 阅读测试文件 [tests/ffi_exception_catch.rs:1-3](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/ffi_exception_catch.rs#L1-L3) 的模块注释：它故意用 GQA 组 7（`num_qo_heads=56, num_kv_heads=8`）驱动 decode——一个 FlashInfer 不支持的组大小（其分发只实例化 1/2/3/4/8）。
4. 对照 [tests/ffi_exception_catch.rs:13-17](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/ffi_exception_catch.rs#L13-L17)：测试自己声明了 `cudaMalloc/cudaFree/cudaSetDevice` 的 extern，不依赖 DeviceContext——独立小测试的常用姿势。
5. 重点看 [tests/ffi_exception_catch.rs:40-42](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/ffi_exception_catch.rs#L40-L42)（`cudaSetDevice(0) != 0` 即无 GPU 时打印 skipping 并返回 Ok）与 [tests/ffi_exception_catch.rs:79-84](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/ffi_exception_catch.rs#L79-L84)：断言 `result == -1`，再用 `CStr::from_ptr(ffi::pegainfer_kernels_last_error())` 读消息并断言包含 `"group_size"`。

**需要观察的现象**：测试通过；`--nocapture` 下若被跳过会打印 `skipping: no CUDA device`。

**预期结果**：有 GPU 时 `test unsupported_gqa_group_returns_sentinel_with_message ... ok`。测试注释还记录了历史："Before the guard this aborted the whole process"——guard 引入前，这个用例的表现是整个测试进程 abort。

**待本地验证**：本讲义撰写环境未运行该命令（需要 GPU），请在本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `PEGAINFER_FFI_GUARD_BEGIN` 里的 `pegainfer_ffi_set_last_error("")` 删掉，会出什么问题？构造一个具体场景。

**答案**：同线程上，调用 A（异常，写了 `what()`）之后调用 B（不抛异常但返回非零错误码，且 B 的入口也用 guard）。若 guard 不清空，B 失败后 Rust 侧若调用 `ffi_exception_message` 之外的代码去读 `last_error`，或者 B 的某个后续入口异常返回 `-1` 却没有新的 `what()` 写入，就会把 A 的旧消息当成 B 的。清空操作把「通道里有消息」与「本次调用抛了异常」重新对齐。

**练习 2**：为什么 `PEGAINFER_FFI_GUARD_END` 要带一个参数 `ret_on_throw`，而不是写死 `-1`？

**答案**：哨兵值属于**每个入口与其 Rust 调用方之间的契约**。`CUresult` 返回型的入口（如 `glm52_fp8_groupwise_gemm_sm100_cuda`）异常时返回的应是 `CUresult` 类型的值而非 `-1` 被截断后的 4294967295；让宏接受参数，两种返回型都能用同一套宏。共享的 `i32` 家族按惯例传 `-1`。

**练习 3**：guard 能捕获 kernel **执行中**的错误（如显存越界导致的 `cudaErrorIllegalAddress`）吗？

**答案**：不能。CUDA kernel 启动是异步的，host 侧 `catch` 只能拦住「启动参数非法、FlashInfer host 分发抛异常」这类**同步**失败。kernel 在设备上跑飞产生的错误是异步的，要靠后续的 `cudaGetLastError` / 同步点查询（见 4.3 的 `map_cuda_error(cudaGetLastError())` 用法）。guard 与错误码映射覆盖的是两类不同的失败面。

### 4.3 CUresult 与 cuBLAS 状态映射：统一错误码面

#### 4.3.1 概念说明

异常之外，CUDA 世界还有两大类同步错误码：

- runtime API 的 `cudaError_t`（kernel 启动结果、`cudaGetLastError`）；
- cuBLAS 的 `cublasStatus_t`（GEMM 等库调用结果）。

而本 crate 的 FFI 返回类型想统一用 cudarc 的 `CUresult`（driver 码）。于是需要两个映射：

1. **`map_cuda_error`**：`cudaError_t → CUresult`。CUDA 官方保证两套枚举在多数条目上数值成对相同，但 Rust 侧只有一份 `CUresult` 解码表，统一到一套码表意味着「同一个故障，无论哪个 kernel 撞上，在 Rust 里读出来都一样」——这正是 ffi_guard.cuh 注释强调的性质。
2. **`PEGAINFER_CUBLAS_STATUS_BASE = 100000`**：cuBLAS 状态**不映射**（枚举太贫瘠，1=NOT_INITIALIZED、3=ALLOC_FAILED 之类，信息量低），而是直接垫高 10 万：`100000 + cublasStatus_t`。Rust 侧看到 `≥ 100000` 就知道是 cuBLAS，减去基数还原原始状态。

#### 4.3.2 核心流程

一个调用 cuBLAS 的入口的状态合成规则：

```text
cublas_status_to_error(status):
    status == CUBLAS_STATUS_SUCCESS  →  0（即 cudaSuccess）
    否则                              →  100000 + status

Rust 侧解码：
    status == 0          → 成功
    status >= 100000     → "cublas_status={status - 100000}"
    其他                 → "cuda_status={status}"
```

数值区间的划分保证三者互不碰撞：`0` 成功；`(0, 100000)` 是 CUDA 错误码；`[100000, ∞)` 是 cuBLAS。而 `-1` 被保留为异常哨兵（负数区间，与上面三个区间天然隔离）。

`map_cuda_error` 的映射策略（读注释得出）：

- 每个有 driver 对应物的 runtime 错误映射到对应 `CUresult`（CUDA 12.8 与 13.1 头文件里这些配对数值相同）；
- 少数 runtime 独有、无 driver 码的错误落到 `CUDA_ERROR_UNKNOWN`；
- 个别过时条目（deprecated invalid device pointer）映射到 `CUDA_ERROR_INVALID_VALUE`；
- 比 CUDA 12.3（本 crate 支持的最老工具链）更新的枚举条目用 `#if CUDART_VERSION >= ...` 门控，老工具链下编译不过的问题被规避；
- 兜底 `default: CUDA_ERROR_UNKNOWN`。

#### 4.3.3 源码精读

- [csrc/shared/ffi_guard.cuh:13-16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L13-L16) —— `constexpr int PEGAINFER_CUBLAS_STATUS_BASE = 100000;` 及其契约注释："an entry point that calls cuBLAS returns it plus the cublasStatus_t on a cuBLAS failure, and the cudaError_t otherwise; Rust's `ops::ffi_status_detail` decodes the pair."（cuBLAS 入口失败时返回 基数+cublasStatus_t，否则返回 cudaError_t；Rust 的 ffi_status_detail 负责解码这一对。）

- [csrc/shared/ffi_guard.cuh:18-25](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L18-L25) —— `map_cuda_error` 的设计注释与函数签名。注释逐条说明：所有转换都过这里、同一故障在 Rust 侧读数一致、12.8/13.1 头文件中配对数值相同、runtime-only 错误映射 `CUDA_ERROR_UNKNOWN`、受门控的条目晚于 CUDA 12.3。

- [csrc/shared/ffi_guard.cuh:26-31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L26-L31) —— switch 开头几个代表性映射：`cudaSuccess → CUDA_SUCCESS`、两个 invalid value 类错误合并到 `CUDA_ERROR_INVALID_VALUE`（含 deprecated 的 invalid device pointer）、OOM 映射 `CUDA_ERROR_OUT_OF_MEMORY`。

- [csrc/shared/ffi_guard.cuh:120-130](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L120-L130) —— 版本门控与兜底：`CUDART_VERSION >= 12060`（12.6）才映射 `cudaErrorFunctionNotLoaded` 等三条，`>= 12080`（12.8）才映射 `cudaErrorContained` / `cudaErrorTensorMemoryLeak`，其余一律 `CUDA_ERROR_UNKNOWN`。这是「最老支持 CUDA 12.3」约束下的条件编译。

cuBLAS 侧的真实转换函数：

- [csrc/shared/linear.cu:21-26](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L21-L26) —— `cublas_status_to_error`：成功折叠为 0，失败返回 `PEGAINFER_CUBLAS_STATUS_BASE + status`。kimi-k2 与 dsv2 的 GEMM 入口各自有一份同样的三行函数（如 [csrc/kimi_k2/kimi_o_proj.cu:18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/kimi_k2/kimi_o_proj.cu#L18)）。

`map_cuda_error` 的典型调用点是「launch 后立刻查一次错误」：

- [csrc/shared/flashinfer_norm.cu:571-586](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/flashinfer_norm.cu#L571-L586) —— `rms_norm_batched_dual_cuda`：`CUresult` 返回型入口。启动 `DualRMSNormKernel<<<...>>>` 后 `return map_cuda_error(cudaGetLastError());`——把**同步可见**的启动错误（grid/block 非法、非法参数）折成 driver 码。同文件 [csrc/shared/flashinfer_norm.cu:553-558](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/flashinfer_norm.cu#L553-L558) 的 `rms_norm_cuda` 则是 void 返回型的对照。

一个值得注意的细节——负数哨兵可能被入口自定义：

- [csrc/shared/linear.cu:69-77](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L69-L77) —— cublasLt 家族定义了自己的负哨兵：`GEMM_LT_UNTUNED = -1`（该形状从未调优过）、`GEMM_LT_PIN_UNSUPPORTED = -2`、`GEMM_LT_PIN_NONDET = -3`。它们与异常哨兵 `-1` 同值但语义不同（该入口不抛异常，见注释"callers fall back to the cublasGemmEx paths"）。**读 wrapper 时先读入口的契约注释，不要默认 -1 一定是异常**。

#### 4.3.4 代码实践

**实践目标**：验证 `map_cuda_error` 的「同值配对」性质，并追踪一个 cuBLAS 错误码在 Rust 侧的呈现。

**操作步骤**：

1. 纸面练习：对照 [csrc/shared/ffi_guard.cuh:30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L30) 与 [csrc/shared/ffi_guard.cuh:75](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L75) 两条映射，写出「显存越界」与「kernel 跑飞」两种故障最终在 Rust 侧的整数取值（提示：分别是 `CUDA_ERROR_OUT_OF_MEMORY` 与 `CUDA_ERROR_ILLEGAL_ADDRESS`）。
2. 打开 [src/ops/linear.rs:386-392](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/linear.rs#L386-L392)，看 `s => bail!("cublasLt pin GEMM failed: {}, ...", ffi_status_detail(s), ...)`：cuBLAS 失败如何被格式化进错误消息。
3. 运行 `grep -rn "cublas_status_to_error" pegainfer-kernels/csrc/ | head`，统计有多少个模型线复制了这个三行转换。

**需要观察的现象**：`cublas_status_to_error` 在 shared、kimi_k2、deepseek_v2_lite 等多处出现；`ffi_status_detail` 的输出形如 `cublas_status=7`（CUBLAS_STATUS_INVALID_VALUE）或 `cuda_status=1`。

**预期结果**：能写出两条映射的 Rust 侧取值；找到至少 3 处 `cublas_status_to_error` 定义。

**待本地验证**：具体取值请对照本地 cudarc 版本的 `CUresult` 枚举定义（`CU_ERROR_OUT_OF_MEMORY = 2`、`CU_ERROR_ILLEGAL_ADDRESS = 700` 等，以你本地的 `cudarc::driver::sys` 为准）。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 cuBLAS 状态垫高 100000，而不是也给它写一张映射表？

**答案**：`cudaError_t` 有约 120 个语义丰富的条目、与 `CUresult` 一一成对，映射有信息增益；`cublasStatus_t` 只有约 10 个粗粒度条目（SUCCESS/NOT_INITIALIZED/ALLOC_FAILED/INVALID_VALUE/...），映射成 CUresult 反而丢上下文。垫高方案保留原始码值（Rust 侧 `status - 100000` 可直接对照 cuBLAS 头文件查义），实现只要一行加法。

**练习 2**：`map_cuda_error` 的注释说"the same fault reads the same in Rust whichever kernel hit it"。如果各入口各自内联写 `static_cast<CUresult>(err)`，会破坏什么？

**答案**：`cudaError_t` 与 `CUresult` 的数值在**大部分**条目上相同，但并不保证全部相同（runtime-only 错误、版本新增条目）。各处自行 cast 会让同一故障在不同入口呈现不同整数，Rust 侧的日志聚合与错误分类就失去一致性。集中一个 switch 同时也是加版本门控、处理特殊条目的唯一场所。

**练习 3**：`rms_norm_batched_dual_cuda` 在 kernel 启动**成功**但设备上稍后跑飞时，返回什么？

**答案**：返回 `CUDA_SUCCESS`（0）。`cudaGetLastError` 在启动后立即查询，只能看到同步错误；设备端异步故障留待后续同步点暴露。这解释了 4.2.5 练习 3 的分工：guard 管同步异常，错误码管同步状态，异步故障靠流同步后的一次查询。

### 4.4 last_error 读取：从错误码到 anyhow::Error 的完整链路

#### 4.4.1 概念说明

`-1` 哨兵只说「出异常了」，消息本体存在一条**thread-local 旁路通道**里：C++ 侧一个 `thread_local std::string`，由 `extern "C"` 函数 `pegainfer_kernels_last_error()` 暴露给 Rust。

选 thread-local 而非全局的理由是并发安全：TP/EP 多线程各自调 FFI 入口，线程各自的消息互不覆盖，也不需要锁——这条通道**没有锁**，靠「写读都发生在同一次调用的同一线程上」保证一致。

Rust 侧有两个公共解码器（都在 `src/ops.rs`）：

- `ffi_exception_message(result)`：result 为 `-1` 时读通道，格式化为 `": {消息}"`（供 `bail!` 拼接）；否则返回空串。
- `ffi_status_detail(status)`：按 4.3 的区间规则把整数状态渲染成 `cublas_status=N` 或 `cuda_status=N`。

wrapper 层的标准消费姿势是：`result != 0` 就 `anyhow::bail!("符号名 failed ... {result}{}", ffi_exception_message(result))`——错误码与异常消息拼进同一条错误，沿 `anyhow::Error` 一路上抛。

还有一个必须立刻拷贝的坑：`pegainfer_kernels_last_error()` 返回的是 C++ `std::string::c_str()` 的指针，**只在同线程下一次写入前有效**。Rust 侧必须立即 `CStr::from_ptr(...).to_string_lossy()` 拷成自有字符串，不能存指针。

#### 4.4.2 核心流程

一次 FFI 失败的完整旅程：

```text
[C++ 入口]                          [Rust]
guard 进入: 清空 thread_local
   │
   ├─ 异常 ── catch ── set(e.what()) ── return -1 ──→ result != 0
   │                                                  │ ffi_exception_message(-1)
   │                                                  │   → CStr::from_ptr(last_error) → 拷贝
   │                                                  │   → ": ...group_size..."
   │                                                  ▼
   │                                       anyhow::bail!("... failed with error -1: ...")
   ├─ cudaError_t ── map_cuda_error ── CUresult ──→ result != 0 → bail!("{result}")
   └─ cublasStatus_t ── 100000+s ────────────────→ ffi_status_detail → "cublas_status=N"
                                                        │
                                                        ▼
                                              anyhow::Error 沿调用栈上抛
```

#### 4.4.3 源码精读

通道的存储与导出（整个文件只有 13 行）：

- [csrc/shared/ffi_last_error.cu:5-11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_last_error.cu#L5-L11) —— 匿名命名空间里的 `thread_local std::string g_ffi_last_error;`，以及写入函数 `pegainfer_ffi_set_last_error`（nullptr 归一为空串）。写入方只有 guard 宏。

- [csrc/shared/ffi_last_error.cu:13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_last_error.cu#L13) —— 唯一的读取出口：`extern "C" const char* pegainfer_kernels_last_error()`，直接返回 `c_str()`。注意它返回的是**内部缓冲的指针**，生命周期规则如 4.4.1 所述。

Rust 侧声明（注意它在 shared.rs 里单独占一个 extern 块，因为它是基建而非算子）：

- [src/ffi/shared.rs:1026-1028](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L1026-L1028) —— `pub fn pegainfer_kernels_last_error() -> *const std::os::raw::c_char;`。经 `pub use shared::*`（[src/ffi.rs:50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L50)）后，测试可直接写 `ffi::pegainfer_kernels_last_error()`。

Rust 侧的两个解码器：

- [src/ops.rs:287-304](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L287-L304) —— `ffi_exception_message`。doc 注释写明契约："empty unless `result` is the -1 sentinel set by the C++ guard"，并说明它是 `pub` 的原因——绕过 wrapper 直接调 FFI 的外部 crate 也需要它。实现里三重防御：非 `-1` 返回空串；指针为 null 返回空串；消息为空返回空串；否则 `format!(": {text}")`（前导冒号专为拼进 `bail!` 消息设计）。

- [src/ops.rs:306-318](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L306-L318) —— Rust 侧镜像的 `CUBLAS_STATUS_BASE: i32 = 100_000`（doc 注释指回 C++ 的定义）与 `ffi_status_detail`：`>= 100000` 渲染 `cublas_status=N`，否则 `cuda_status=N`。**两份常数必须人工保持同步**——这是跨语言共享常数的固有税。

wrapper 层的标准消费姿势：

- [src/ops/attention.rs:711-716](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/attention.rs#L711-L716) —— `paged_kv_scatter_cuda` 失败时：`anyhow::bail!("paged_kv_scatter_cuda failed for layer {layer} with error {result}{}", crate::ops::ffi_exception_message(result))`。整数码与异常消息同框出现，这就是「从 CUDA 错误码到 anyhow::Error」的最后一米。全 crate 有 30+ 处同款调用（gemma4、qwen35、sampling、kimi 等模块）。

#### 4.4.4 代码实践

**实践目标**：完成规格中要求的最小闭环——不借助现成测试，自己驱动一个 FFI 入口出错，把 `last_error` 读出来并转成 `anyhow::Error`。

**操作步骤**：

1. 在你的工作副本里新建 `pegainfer-kernels/tests/ffi_error_probe.rs`（读者练习文件，非本仓库源码）。
2. 以 [tests/ffi_exception_catch.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/ffi_exception_catch.rs) 为骨架：抄 L13-17 的三个 extern（`cudaMalloc/cudaFree/cudaSetDevice`）与 L19-36 的 `DeviceBuf` RAII。
3. 调 `pegainfer_kernels::ffi::paged_attention_decode_cuda`，参数照抄测试的 GQA 组 7 配置（56/8/128/16）。
4. 关键差异——**不要**直接断言消息，而是走公共解码器构造 anyhow 错误：

   ```rust
   // 示例代码（读者实践，非仓库源码）
   let result = unsafe { ffi::paged_attention_decode_cuda(/* 组 7 配置，参数表见 ffi_exception_catch.rs */) };
   let detail = pegainfer_kernels::ops::ffi_exception_message(result);
   let err = anyhow::anyhow!("decode failed with error {result}{detail}");
   assert!(result == -1, "{err}");
   assert!(err.to_string().contains("group_size"), "{err}");
   ```

5. 运行：`cargo test --release -p pegainfer-kernels --test ffi_error_probe -- --nocapture`。
6. 回答书面问题：为什么这个闭环里「未捕获的 C++ 异常」会直接 abort 进程？（提示：想象把 guard 删掉——异常离开 `decode_launch` 的 try 块后，下一帧就是 Rust 的 `unsafe` 调用点，Rust 帧没有 C++ unwind 表，语言运行时唯一确定的出路是 terminate。）

**需要观察的现象**：`err.to_string()` 形如 `decode failed with error -1: ...group_size...`——整数哨兵与通道消息拼合成功；测试断言通过。

**预期结果**：闭环成立；把 `ffi_exception_message(result)` 换成手动 `CStr::from_ptr(ffi::pegainfer_kernels_last_error())` 也能得到同样文本（解码器内部就是这两行）。

**待本地验证**：需要 GPU 环境；无 GPU 时记得仿照原测试在开头 `cudaSetDevice(0) != 0` 即返回。

#### 4.4.5 小练习与答案

**练习 1**：两个线程同时各调一个会抛异常的 FFI 入口，`last_error` 会不会串台？

**答案**：不会。`g_ffi_last_error` 是 `thread_local`，每线程一份；各自的 guard 写各自的副本，各自的 Rust 读取也在同一线程上完成。通道因此无锁且线程安全。

**练习 2**：Rust 侧 `let p = ffi::pegainfer_kernels_last_error(); /* 先做点别的 */ let s = CStr::from_ptr(p);` 这样写有什么隐患？

**答案**：`p` 指向 thread-local `std::string` 的内部缓冲。「先做点别的」期间若同线程上任何 guarded FFI 入口被调用，guard 进入时的清空会**重写这个 string**，`p` 可能悬垂（`c_str()` 也不保证重分配后旧指针有效）。正确做法是拿到指针立刻转 `CStr` 并拷贝——`ffi_exception_message`（[src/ops.rs:294-298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L294-L298)）正是两行连写。

**练习 3**：`CUBLAS_STATUS_BASE` 同时存在于 C++（`constexpr int`）与 Rust（`pub const i32`），为什么不用 bindgen 之类的东西生成一份？

**答案**：可以，但成本收益不匹配——这个 crate 的 FFI 面是手工维护的（`src/ffi/*.rs` 全是手写声明，签名带文档注释、按模型线分组），为一个常数引入生成管线会打破「构建期零额外工具」的默认哲学（u1-l1）。手工双份的代价是**改一处必须改另一处**，两边注释互相指认（[csrc/shared/ffi_guard.cuh:13-16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L13-L16) 与 [src/ops.rs:306-309](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L306-L309) 各自注明对方位置）正是防漂移的手段。

## 5. 综合实践

把本讲四块内容串成一条链：**写一页「FFI 错误路径审计报告」**。

任务设定：你正在 review 一个假想的新 kernel 入口 `foo_cuda`（`i32` 返回型，内部既调 cuBLAS 又调 FlashInfer）。请产出：

1. **调用图**（纸面）：`foo_cuda` 的 Rust wrapper → `unsafe extern "C"` 声明（写明应放在 `src/ffi/` 哪个文件、归哪个 feature 门控）→ C++ 入口。标注每个环节的类型对应（`*const Half`/`i32`/`CUstream`）。
2. **错误契约表**：仿照 4.1.2 的三形态表，列出 `foo_cuda` 的全部返回取值及语义（0 / 负哨兵 / `(0,100000)` / `[100000,∞)`），并指明每种取值发生时 `last_error` 通道的状态（有/无/陈旧风险）。
3. **验证用例**：参照 `tests/ffi_exception_catch.rs` 的骨架，为 `foo_cuda` 写两个测试用例的伪代码——一个驱动 FlashInfer 异常路径（断言 `-1` + 消息），一个驱动参数预检路径（断言非零错误码且消息通道为空）。
4. **实机验证**：完成 4.4.4 的最小闭环练习并运行 4.2.4 的既有测试，两份输出贴进报告。

判定标准：报告能回答「一个 `-1` 从哪里来、到哪里去、路上谁清空了通道、Rust 何时拷贝字符串」，就说明本讲的目标达成了。

## 6. 本讲小结

- **FFI 组织**：`src/ffi.rs` 是门面——`Half`（= `u16`，不透明 16 位容器）定义于此，按模型线 `#[cfg]` 分模块声明并以 `pub use ...::*` 统一 re-export；C++ 侧靠未修饰 C 符号名与 Rust 声明对齐。
- **三种返回形态即三种错误契约**：`()` 发后不管；`CUresult` 是 driver 错误码；`i32` 承载 `0` / `-1` 异常哨兵 / CUDA 码 / `100000+cuBLAS 码` 的区间划分。
- **异常不能穿越 `extern "C"`**：C++ 异常依赖栈展开元数据，Rust 帧没有；漏网异常直接 abort 进程。`PEGAINFER_FFI_GUARD_BEGIN/END` 在 C++ 一侧 try/catch 接住，进入时清空 thread-local 消息防止陈旧读取。
- **统一错误码面**：`map_cuda_error` 把 `cudaError_t` 折成 `CUresult`（含版本门控与 `CUDA_ERROR_UNKNOWN` 兜底），`PEGAINFER_CUBLAS_STATUS_BASE = 100000` 垫高 cuBLAS 状态；两边常数在 C++/Rust 各存一份、注释互指。
- **last_error 通道**：`thread_local std::string` + `extern "C"` 读取出口，无锁、每线程独立；Rust 侧 `ffi_exception_message`（`-1` 时读消息，立即拷贝）与 `ffi_status_detail`（区间渲染）两个公共解码器，wrapper 用 `anyhow::bail!("... {result}{}", ffi_exception_message(result))` 收口。
- **既证事实**：`tests/ffi_exception_catch.rs` 用 GQA 组 7 驱动 FlashInfer 抛异常，断言 `-1` 哨兵与含 `group_size` 的消息——测试注释记录了 guard 引入前整个进程会 abort。

## 7. 下一步学习建议

- **下一讲 u2-l4（typed_pipeline! 前向声明 DSL）**：本讲的 `ffi_exception_message`/`ffi_status_detail` 正是 DSL 内部错误处理所复用的原语，学完后可以回看 `typed_ops` 里 30+ 处 `ffi_status_detail` 调用的语义。
- **向下游走（u3-l1）**：拿本讲的「wrapper 标准五步」视角去解剖 `ops.rs` 的任意导出函数，验证错误契约在 wrapper 层的落法。
- **延伸阅读源码**：`csrc/shared/paged_launch.cuh` 的另外三个 guard 入口（prefill/split-KV 变体）与 `csrc/k3/` 下 guard 密集的入口群——同一个防御工事在不同模型线的复制形态。
- **背景阅读**：Rust RFC 与 nomicon 中关于 `extern "C"` 与 unwind 的章节（搜 "extern C unwind abort"），理解 `extern "C"` 与 `extern "C-unwind"` 的区别，能加深对本讲核心动机的认识。
