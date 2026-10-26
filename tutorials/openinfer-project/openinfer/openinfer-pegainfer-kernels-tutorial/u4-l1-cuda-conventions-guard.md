# u4-l1 · CUDA 侧工程约定与异常防护

> 承接 u2-l3（Rust 侧的 FFI 错误契约）与 u3-l6（第一个 CUDA kernel 精读）：u2-l3 讲清了「-1 哨兵 + last_error 通道 + CUresult 统一映射」这条契约的 Rust 一端，u3-l6 带你读进了一个真实 kernel。本讲把视角翻到 C++ 一侧——**当你新写一个 CUDA 入口时，哪些样板是必经之路、为什么**。这一讲是 u4-l2「端到端新增算子」的直接前置。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出「入口样板四件套」——检查、guard、launch、错误返回——各自的职责与落点，并能对照一个真实入口判断它缺哪一件；
2. 解释 `map_cuda_error` 为什么把 runtime 错误码（`cudaError_t`）统一映射成 driver 错误码（`CUresult`）；
3. 解释 `PEGAINFER_CUBLAS_STATUS_BASE = 100000` 的基址偏移如何让一个 `i32` 同时承载四种来源的状态而不互相歧义；
4. 描述 `last_error` 的线程生命周期：进入 guard 即清空、异常时写入、Rust 读出后为何必须立即拷贝；
5. 拿到一个「半成品」入口，写出补齐清单并动手补齐后跑通既有测试。

## 2. 前置知识

- **extern "C" 边界**：Rust 与 C++ 之间的函数调用走 C ABI。C++ 异常依赖栈展开（unwinding），而 `extern "C"` 向编译器承诺「这条边界上没有异常」。Rust 帧缺少对应的展开元数据，一个漏网的 C++ 异常到达 Rust 栈帧时进程直接 `abort`，连错误信息都不会打印。仓库注释的原话是 "a foreign exception reaching Rust aborts the process with no message"。
- **FlashInfer 会抛异常**：本 crate 大量入口是 FlashInfer header-only 模板的薄包装，而 FlashInfer 的 host 侧 dispatch 在不支持的场景（比如未编译的 GQA 组大小）会 `throw`。这就是「每个入口都要 guard」的直接动因。
- **CUDA 的两套错误码**：runtime API 用 `cudaError_t`，driver API 用 `CUresult`。本 crate 的 Rust 侧（cudarc）统一按 `CUresult` 解码，所以 C++ 侧需要一座桥。
- **cuBLAS 的第三套状态**：cuBLAS 调用返回 `cublasStatus_t`，又是另一套枚举。
- **thread_local**：C++ 的线程局部存储。每个线程各有一份变量，线程间互不可见。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `csrc/shared/ffi_guard.cuh` | 本讲主角：guard 宏、`map_cuda_error`、`PEGAINFER_CUBLAS_STATUS_BASE`，全部约定集中在这 147 行里 |
| `csrc/shared/ffi_last_error.cu` | `last_error` 的 13 行实现：thread_local 存储 + 两个导出函数 |
| `csrc/shared/paged_attention.cu` | 四件套**完备**的标本：7 个入口全部走 guard |
| `csrc/shared/norm.cu` | **第一代**形态反例：void 返回、无 guard（u3-l6 精读过的 gated RMSNorm 就在这里） |
| `csrc/shared/dflash2_conv.cu` | **半成品**标本：有 launch 有错误映射、唯独缺 guard——本讲综合实践的改造对象 |
| `csrc/common.cuh` | 设备侧检查防线：`__trap()` 风格的 `csr_page_row_checked` / `resident_row_checked` |
| `csrc/shared/linear.cu` | cuBLAS 状态编码的参考实现 |
| `src/ffi/shared.rs` | Rust 侧 extern 声明面（声明即契约） |
| `src/ops.rs` | Rust 侧解码器：`ffi_exception_message` 与 `ffi_status_detail` |
| `tests/ffi_exception_catch.rs` | 端到端验证：故意触发 FlashInfer 异常，断言 -1 + 消息 |
| `src/ops/dflash2_conv.rs` / `tests/dflash2_conv.rs` | 综合实践配套：被改造入口的 wrapper 与既有测试 |

## 4. 核心概念与源码讲解

### 4.1 入口样板四件套：检查、guard、launch、错误返回

#### 4.1.1 概念说明

一个「合格」的 CUDA FFI 入口要在四个位置各尽其责：

1. **检查**——非法输入必须在**形成指针之前**被拦下。本 crate 的分工是：与张量形状/语义相关的检查前置到 Rust wrapper 的 `ensure!`（u3-l1 已建立），而依赖设备端数据（页表内容）的检查下沉到 kernel 里用 `__trap()` 兜底（见 4.1.3 的 `common.cuh`）。
2. **guard**——`PEGAINFER_FFI_GUARD_BEGIN/END` 包住入口体，把任何 C++ 异常转换成哨兵返回值 + `last_error` 消息，杜绝异常穿越 `extern "C"`。
3. **launch**——`kernel<<<grid, block, 0, stream>>>`，流参数由 wrapper 传入（`active_cu_stream`），入口自身绝不造流。
4. **错误返回**——launch 之后立刻 `cudaGetLastError()` 并经 `map_cuda_error` 归一成 `CUresult`；调 FlashInfer 模板的入口则直接把模板调用的返回码 `static_cast<int>` 后返回。

为什么说是「必经之路」而不是风格偏好？因为缺任何一件的后果都不是「报错不好看」，而是**进程级故障或静默错误**：缺 guard 是 abort，缺错误返回是 kernel 失败了还当成功继续跑（数据全错），缺检查是非法访存把整卡上下文打穿。

#### 4.1.2 核心流程

一个完备入口的形状（伪代码）：

```text
extern "C" 返回类型 op_xxx_cuda(参数…) {
  // ① 检查：形状/空指针在 Rust wrapper 的 ensure! 里（前置），
  //         依赖设备数据的在 kernel 内 __trap()（兜底）
  PEGAINFER_FFI_GUARD_BEGIN                 // ② 进入即清空 last_error，开 try 块
    …构造 FlashInfer 对象 / 计算 grid…
    kernel<<<grid, block, 0, stream>>>(…);  // ③ launch
    return map_cuda_error(cudaGetLastError()); // ④ 归一化错误码
  PEGAINFER_FFI_GUARD_END(-1)               // ②' catch → 写 last_error，返回哨兵
}
```

仓库现状的三个世代（读懂它们，就能对任意入口做「缺件诊断」）：

- **第一代（void、无 guard）**：launch 完就返回，错误信息全部丢失——`norm.cu` 的两个入口；
- **半成品（有 ④ 无 ②）**：错误码有了，但 host 侧异常仍会击穿进程——`dflash2_conv.cu`（本讲综合实践的对象）；
- **完备世代**：四件齐全——`paged_attention.cu` 全部 7 个入口。

#### 4.1.3 源码精读

**完备标本：`paged_kv_append_cuda`。** 这是 KV append 入口，从函数体第一行到最后一行展示了完整的四件套：

- [csrc/shared/paged_attention.cu:58-85](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_attention.cu#L58-L85) —— `int paged_kv_append_cuda(...)` 完整入口体：签名收一堆裸指针与维度后，`PEGAINFER_FFI_GUARD_BEGIN`（L74）开保护块，`make_paged_kv` 组装 FlashInfer 的页式 KV 包装对象，`AppendPagedKVCacheDecode` 的返回码 `static_cast<int>` 直接返回，`PEGAINFER_FFI_GUARD_END(-1)`（L85）收口。
- 同文件共有 7 个 guard 站点：L74、L116、L252、L311、L378、L447、L526，与 7 个 `GUARD_END(-1)` 一一配对（L85、L134、L260、L358、L425、L495、L560），可用 `grep -n "PEGAINFER_FFI_GUARD" csrc/shared/paged_attention.cu` 自行验证。
- [csrc/shared/paged_attention.cu:14-29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_attention.cu#L14-L29) —— `paged_attention_decode_cuda` 则是「薄委托」形态：本体只把参数转发给 `decode_launch<128, Variant>` 模板，guard 与错误处理在 [csrc/shared/paged_launch.cuh:137-137](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh#L137) 等 4 处（L137/L220/L346/L405）的 launch 模板里。**guard 包住的是「真正会 throw 的调用」，不必死守在 extern 入口那一层。**

**第一代反例：`norm.cu` 的两个 void 入口。**

- [csrc/shared/norm.cu:88-95](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/norm.cu#L88-L95) —— `rms_norm_gated_cuda`：纯 launch、void 返回、无 guard。它的 Rust 声明也是 void（[src/ffi/shared.rs:498-510](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L498-L510)）。自研 kernel 不调 FlashInfer、确实没有异常源，但 launch 配置错误也会被静默吞掉——这是历史形态，新入口不应再模仿。
- [csrc/shared/norm.cu:86-106](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/norm.cu#L86-L106) —— 整个 `extern "C"` 块：SiLU 变体在 L88，sigmoid 变体在 L97。

**半成品标本：`dflash2_grouped_conv_cuda`（本讲综合实践对象）。**

- [csrc/shared/dflash2_conv.cu:45-54](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/dflash2_conv.cu#L45-L54) —— 四件套里它有 ③④（L51-L52 launch，L53 `return map_cuda_error(cudaGetLastError())`），唯独没有 ②。L44 的注释明说形状检查前置到了 Rust wrapper（`dflash2_grouped_conv_into`）。
- [src/ops/dflash2_conv.rs:32-56](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/dflash2_conv.rs#L32-L56) —— wrapper 侧的 ①：五个 `ensure!` 分别校验尺寸换算、base 张量形状（`[2, taps, hidden]` 且 `side < 2`）、output 与 input 形状一致。
- [src/ops/dflash2_conv.rs:61-64](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/dflash2_conv.rs#L61-L64) 与 [src/ops/dflash2_conv.rs:65-80](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/dflash2_conv.rs#L65-L80) —— 取设备指针（守卫保活）、调用 FFI（L66 起）、`result.result()?`（L80）把 `CUresult` 转成 `anyhow::Error`。
- [src/ffi/shared.rs:8-20](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L8-L20) —— Rust 声明：`-> CUresult`。声明、C++ 定义、wrapper 解码三处的类型必须严格一致，这是 FFI 的第一纪律。

**设备侧检查防线：`common.cuh`。**

- [csrc/common.cuh:30-41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/common.cuh#L30-L41) —— `csr_page_row_checked`：在**形成任何页指针之前**校验 CSR 窗口（`begin/end` 的次序与边界），非法则 `__trap()`，注释原话是 "validated before any pointer is formed"。
- [csrc/common.cuh:45-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/common.cuh#L45-L50) —— `resident_row_checked`：同样的 trap 风格，防御 `page_size <= 0`、`origin < 0` 等非法元数据。

为什么设备侧用 `__trap()` 而不是返回错误码？kernel 无法跨越 ABI 返回复杂状态，trap 会以设备侧异常终止本次 launch，随后 `cudaGetLastError()`/同步点把它取回 host，再走 ④ 的归一化路径。这就是 ①（设备侧）与 ④ 的接力。

#### 4.1.4 代码实践

**实践 A：亲眼看一次「异常被 guard 拦下」。**

1. 实践目标：验证 -1 哨兵 + last_error 消息的端到端链路真实存在。
2. 操作步骤（需要 GPU；无 GPU 时该测试会显式跳过）：
   ```bash
   cargo test --release -p pegainfer-kernels --test ffi_exception_catch -- --nocapture
   ```
3. 需要观察的现象：测试向 decode 入口喂 GQA 组大小 7（`num_qo_heads=56, num_kv_heads=8`，见 [tests/ffi_exception_catch.rs:44-47](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/ffi_exception_catch.rs#L44-L47)），FlashInfer dispatch 抛异常，断言返回 -1（L79）且消息含 `group_size`（L81-L83）。
4. 预期结果：测试通过。文件头注释（[tests/ffi_exception_catch.rs:1-3](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/ffi_exception_catch.rs#L1-L3)）写明「guard 出现之前这一场景会 abort 整个进程」。

**实践 B：给仓库做一次「缺件普查」。**

1. 实践目标：拿到当前尚未走 guard 的入口清单，感受样板覆盖率。
2. 操作步骤：
   ```bash
   grep -rL "PEGAINFER_FFI_GUARD" --include=*.cu csrc | sort
   grep -rn "PEGAINFER_FFI_GUARD_END(" csrc/shared
   ```
3. 需要观察的现象：第一条列出未含 guard 宏的 `.cu` 文件（含 `norm.cu`、`dflash2_conv.cu`、全部 deepep shim、qwen35 的 conv1d/hd256 系列等）；第二条显示 shared 目录内在用的 `ret_on_throw` 全部是 `-1`。
4. 预期结果：约三十个文件缺 guard——多数是纯自研 kernel（无异常源），但其中调 FlashInfer 的（如 `flashinfer_norm.cu`）与 cuBLAS 系是真实的加固候选。具体数量随 HEAD 演进，普查结论待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`paged_kv_append_cuda` 若去掉 `PEGAINFER_FFI_GUARD_BEGIN/END`，会发生什么？
**参考答案**：guard 内的 `make_paged_kv` 与 `AppendPagedKVCacheDecode` 都是 C++ 代码，FlashInfer 的 host 侧 dispatch 一旦 `throw`，异常将穿越 `extern "C"` 边界让进程直接 abort——不是测试失败，而是整个进程崩溃且无消息。

**练习 2**：为什么 `norm.cu` 的入口可以「暂时」没有 guard 而线上没有炸？
**参考答案**：它只 launch 自研 kernel，不调用任何会 throw 的 C++ 代码（FlashInfer/cuBLAS/std 容器），异常源为零。代价是 launch 配置错误被静默吞掉（void 返回，无人读 sticky 错误位）——历史形态，新入口不应模仿。

**练习 3**：四件套中「检查」为什么拆成 Rust wrapper 的 `ensure!` 与 kernel 内的 `__trap()` 两层？合并到 C++ 入口里不行吗？
**参考答案**：形状/空指针检查需要张量语义（HiddenStates 的 hidden_dim、seq_len），只有 Rust 侧拿得到，且 `ensure!` 能给出带上下文的错误消息；依赖设备端数据（页表内容）的检查 host 侧无法先验，只能在 kernel 读到的瞬间校验。C++ 入口只拿到裸指针与 i32，两边的信息都不全，天然不承担检查职责。

### 4.2 map_cuda_error：把 runtime 错误统一到 driver 码

#### 4.2.1 概念说明

CUDA 有两套公开 API：runtime（`cudaMalloc`、`kernel<<<>>>`、`cudaGetLastError`，错误类型 `cudaError_t`）与 driver（`cuLaunchKernel` 等，错误类型 `CUresult`）。本 crate 的 Rust 侧依赖 cudarc，统一用 `CUresult` 解码，于是 C++ 侧需要一个纯函数把 `cudaError_t` 折叠到 `CUresult`——[csrc/shared/ffi_guard.cuh:25-131](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L25-L131) 的巨型 switch。统一的价值写在它的注释里：**同一个故障无论哪个 kernel 触发，在 Rust 侧读出来都是同一个码**，错误报告与匹配逻辑只需写一份。

#### 4.2.2 核心流程

```text
kernel<<<…>>>  →  cudaGetLastError()        // runtime 错误码（并清空 sticky 位）
                  │
                  ▼
              map_cuda_error(err)            // 巨型 switch：cudaError_t → CUresult
                  │
                  ▼
              return CUresult                // C++ 入口的统一返回类型
                  │
                  ▼  (Rust)
              result.result()?               // cudarc → anyhow::Error
```

三条设计规则（全部来自源码注释与结构）：

1. **同名同值**：有 driver 对应物的 runtime 错误直接映射过去（CUDA 12.8 与 13.1 头文件里这些配对共享同一数值）；
2. **runtime 独有错误** → `CUDA_ERROR_UNKNOWN`，唯二例外是被废弃的 invalid device pointer，归并到 `CUDA_ERROR_INVALID_VALUE`；
3. **比最老支持工具链（CUDA 12.3）新的错误码**必须用 `#if CUDART_VERSION >= …` 门控，否则老工具链上编译失败。

#### 4.2.3 源码精读

- [csrc/shared/ffi_guard.cuh:18-24](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L18-L24) —— 设计注释全文：说明映射动机（same fault reads the same in Rust）、同名配对、UNKNOWN 兜底与 CUDA 12.3 工具链底线。
- [csrc/shared/ffi_guard.cuh:25-31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L25-L31) —— 函数头与前几条映射：`cudaSuccess → CUDA_SUCCESS`、`cudaErrorInvalidValue → CUDA_ERROR_INVALID_VALUE`、`cudaErrorMemoryAllocation → CUDA_ERROR_OUT_OF_MEMORY`。
- [csrc/shared/ffi_guard.cuh:120-129](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L120-L129) —— 版本门控与兜底：`#if CUDART_VERSION >= 12060/12080` 分别放行两批新错误码，`default: return CUDA_ERROR_UNKNOWN` 收尾。
- [csrc/shared/dflash2_conv.cu:49-53](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/dflash2_conv.cu#L49-L53) —— 典型使用点：launch 后一行 `return map_cuda_error(cudaGetLastError());`。注意 `grid` 的计算公式 `min((count+255)/256, 65535)` 就是为了规避一维网格的维度上限，从源头减少 `cudaErrorInvalidConfiguration` 的可能。

#### 4.2.4 代码实践

1. 实践目标：验证「同名配对」规则，并理解 sticky 错误的读取时机。
2. 操作步骤（纸面推导为主，动手部分需 GPU）：
   - 纸面：在 [csrc/shared/ffi_guard.cuh:25-131](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L25-L131) 中找到 `cudaErrorIllegalAddress`、`cudaErrorLaunchTimeout`、`cudaErrorAssert` 三条，写下各自的 `CUresult` 目标。
   - 动手：给综合实践中已补 guard 的 `dflash2_grouped_conv_cuda` 喂一个极端 `rows`（让 grid 计算逼近或越过上限），观察 Rust 侧错误消息里的 CUresult 名字。
3. 需要观察的现象：`cudaGetLastError()` 一旦被调用就清空 sticky 位——如果入口在 launch 之后又做了会设置 sticky 位的操作，读到的是新错误而不是 launch 错误。
4. 预期结果：纸面三条分别是 `CUDA_ERROR_ILLEGAL_ADDRESS`、`CUDA_ERROR_LAUNCH_TIMEOUT`、`CUDA_ERROR_ASSERT`（可对照源码核对）；动手部分的具体错误码**待本地验证**（极端 rows 更可能先被 wrapper 的 `ensure!` 拦下）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接把 `cudaError_t` 的数值透传给 Rust？
**参考答案**：两套枚举的数值空间重叠但含义不同（同一个数字在两套里是不同故障），且 cudarc/Rust 侧只认 `CUresult`。透传会让「同一故障同一读数」的约定失效，Rust 侧还得维护一张对表。

**练习 2**：`map_cuda_error` 里的 `#if CUDART_VERSION >= 12080` 门控保护的是什么场景？
**参考答案**：构建机的 CUDA 工具链可能仍是 12.6/12.7（仓库声明最老支持 12.3）。`cudaErrorContained`、`cudaErrorTensorMemoryLeak` 在老头文件里不存在，不门控会直接编译失败——这是跨工具链构建的兼容性纪律，与 build.rs 的 SM 家族目标探测（u1-l3）同属一个主题。

### 4.3 cuBLAS 状态基址偏移：一个 i32 编码四种来源

#### 4.3.1 概念说明

FFI 返回值只有一个 `i32` 通道，但入口可能失败于三种不同的库：自研 kernel（runtime/driver 错误）、FlashInfer（异常或返回码）、cuBLAS（`cublasStatus_t`）。方案是把状态空间切成互不重叠的区间：

| 返回值区间 | 含义 | 谁产生 |
| --- | --- | --- |
| `0` | 成功（`CUDA_SUCCESS` 与 `CUBLAS_STATUS_SUCCESS` 都是 0，天然兼容） | 任意 |
| `-1` | C++ 异常哨兵，配 `last_error` 消息 | guard 宏 |
| 小正数（CUresult 枚举值） | CUDA 错误（经 `map_cuda_error` 归一） | 自研 / FlashInfer 路径 |
| `>= 100000` | `100000 + cublasStatus_t`，cuBLAS 失败 | cuBLAS 调用入口 |

基址 100000 的取值只要求远大于 CUresult 的全部枚举值（当前 999 量级），留足余量即可。

#### 4.3.2 核心流程

编码与解码严格对称：

```text
C++ 编码                                    Rust 解码
cublasStatus_t s ──► BASE + s           status >= BASE ─► "cublas_status={status - BASE}"
cudaError_t e    ──► map_cuda_error(e)  status 为其它   ─► "cuda_status={status}"
std::exception   ──► -1 (+ last_error)  result == -1    ─► 读 last_error 追加消息
```

关键约束是**区间互斥**：CUresult 是小正数、cuBLAS 垫高到 10 万起、异常是 -1、成功是 0——四段互不碰撞，因此 Rust 侧一次整数比较即可路由，无需任何标志位或额外调用。

#### 4.3.3 源码精读

- [csrc/shared/ffi_guard.cuh:13-16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L13-L16) —— 常量定义与契约注释：调用 cuBLAS 的入口失败时返回 `BASE + cublasStatus_t`，其余返回映射后的错误码，"Rust's `ops::ffi_status_detail` decodes the pair"——C++ 与 Rust 靠这条注释对齐。
- [csrc/shared/linear.cu:21-26](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/linear.cu#L21-L26) —— 编码侧参考实现 `cublas_status_to_error`：成功折叠回 `cudaSuccess`（0），失败垫高。
- [src/ops.rs:306-309](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L306-L309) —— Rust 侧镜像常量 `CUBLAS_STATUS_BASE: i32 = 100_000`，doc 注释直接指回 `csrc/shared/ffi_guard.cuh`。
- [src/ops.rs:311-318](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L311-L318) —— 解码器 `ffi_status_detail`：一次比较分流两种前缀。
- 同一模式在模型专属 cuBLAS 入口重复出现：[csrc/kimi_k2/kimi_mla_cublaslt.cu:41-41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/kimi_k2/kimi_mla_cublaslt.cu#L41)、[csrc/deepseek_v2_lite/dsv2_lite_moe.cu:269-269](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/deepseek_v2_lite/dsv2_lite_moe.cu#L269) 等——每处都是同一行 `return PEGAINFER_CUBLAS_STATUS_BASE + static_cast<int>(status);`。

#### 4.3.4 代码实践

1. 实践目标：吃透「四段互斥」编码，并能解释跨版本指纹的必要性。
2. 操作步骤：
   - 纸面：写出五个值各自落进的区间与解码文本：`0`、`-1`、`1`（CUDA_ERROR_INVALID_VALUE）、`100001`（BASE + CUBLAS_STATUS_NOT_INITIALIZED）、`100007`（BASE + CUBLAS_STATUS_INVALID_VALUE）。
   - 阅读：打开 `csrc/shared/lt_algo_store.cu`，或运行 `cargo test --release -p pegainfer-kernels --test lt_algo_store`（需 GPU），注意 algo 记录里携带的 cublasLt/CUDA 版本指纹。
3. 需要观察的现象：`1` 与 `100001` 解码出的字符串完全不同（`cuda_status=1` 与 `cublas_status=1`）——若没有垫高，这两个「1」无法区分。
4. 预期结果：纸面答案见 4.3.5 练习 1 的同款推导；`lt_algo_store` 的指纹设计与环境变量 `PEGAINFER_GEMM_LT_CACHE` 的「版本不匹配即 miss 重调优」语义互为因果（见仓库 CLAUDE.md 的说明）——这正是「状态码必须携带来源信息」的工程延伸。

#### 4.3.5 小练习与答案

**练习 1**：为什么成功值不需要任何处理？
**参考答案**：三套体系恰好共享 0：`CUDA_SUCCESS = 0`、`CUBLAS_STATUS_SUCCESS = 0`，guard 只在异常时返回 -1。0 是天然的公共成功值，编码与解码都零成本。

**练习 2**：如果未来 CUresult 枚举值涨到接近 100000，会出什么问题？
**参考答案**：区间不再互斥，解码器会把一个 CUDA 错误误读成 cuBLAS 错误。所以 BASE 要远大于 CUresult 枚举上界（当前 999 量级，BASE=100000 留了两个数量级余量）；真涨到临界时需同步改 C++ 常量与 Rust 镜像常量两处。

**练习 3**：`ffi_status_detail` 为什么不读 `last_error`？
**参考答案**：`last_error` 只在 -1 哨兵路径有定义（guard 写入）。CUresult/cuBLAS 路径的错误详情由状态码本身携带，此时 `last_error` 里只可能是空串或陈旧值——读取它反而违反 4.4 节的生命周期约定。

### 4.4 last_error 的线程生命周期

#### 4.4.1 概念说明

guard 宏捕获异常后，`e.what()` 字符串必须送到 Rust。跨 ABI 传字符串最简单的通道是一个 C++ 侧的线程局部缓冲区 + 一个 getter。它的生命周期约定有三条：

1. **进入即清空**：guard BEGIN 的第一件事就是把 `last_error` 置空串，保证 Rust 看到的 -1 永远对应「本次调用」的消息，而不是同线程更早某次调用的陈旧 `what()`；
2. **只在异常路径写入**：正常路径（包括 CUresult 错误路径）不碰它，因此「有消息」当且仅当「返回 -1」；
3. **线程私有**：`thread_local` 存储，多线程并发调用 FFI 时消息互不串扰；代价是 Rust 侧必须在**同一线程**上、且指针失效前立即拷贝。

#### 4.4.2 核心流程

一次异常的完整时序：

```text
[Rust 线程 T]
  unsafe { ffi::op_xxx_cuda(...) }
      │ 进入：BEGIN → set_last_error("")      ← 清空防线
      │ FlashInfer dispatch throw
      │ catch (std::exception& e) → set_last_error(e.what())
      │ return -1
      ▼
  result == -1
  ffi_exception_message(result)
      │ unsafe { ffi::pegainfer_kernels_last_error() } → *const c_char
      │ CStr::from_ptr(...).to_string_lossy()           ← 立即拷贝！
      ▼
  anyhow 错误消息里追加 ": {text}"
```

危险窗口：getter 返回的是 C++ `std::string` 内部缓冲的指针。若 Rust 拿到指针后、拷贝前，同线程又调了任何被 guard 的入口（BEGIN 会清空），指针指向的内容就变了。所以读取必须「拿到即拷」，一步都不能拖延。

#### 4.4.3 源码精读

- [csrc/shared/ffi_last_error.cu:5-7](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_last_error.cu#L5-L7) —— 存储本体：匿名命名空间里的 `thread_local std::string g_ffi_last_error`。
- [csrc/shared/ffi_last_error.cu:9-13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_last_error.cu#L9-L13) —— 写入与读取两个导出函数：setter 做空指针防御（写空串），getter 返回 `c_str()`。
- [csrc/shared/ffi_guard.cuh:132-137](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L132-L137) —— BEGIN 宏三行：清空 → 开 try 块；注释明确写出清空是为了防陈旧读取。
- [csrc/shared/ffi_guard.cuh:138-147](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L138-L147) —— END 宏：`ret_on_throw` 是宏参数（全仓在用的都是 -1），两个 catch 分支——`std::exception` 取 `e.what()`，其余一切写固定文案 "unknown C++ exception"。
- [src/ops.rs:290-304](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L290-L304) —— Rust 解码器 `ffi_exception_message`：只在 `result == -1` 时读（[L294](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L294) 取指针），空指针与空串都归一成空 `String`，非空则拼成 `": {text}"`。doc 注释（L286-L288）写明它对「绕过 wrapper 直接调 guarded 入口」的外部 crate 开放。
- [src/ffi/shared.rs:1026-1028](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L1026-L1028) —— getter 的 Rust 声明，独立的小 `unsafe extern "C"` 块。
- [tests/ffi_exception_catch.rs:79-83](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/ffi_exception_catch.rs#L79-L83) —— 端到端断言：`ensure!(result == -1, …)` 之后立刻 `CStr::from_ptr(ffi::pegainfer_kernels_last_error())` 拷贝并检查 `contains("group_size")`——「拿到即拷」的示范写法。

#### 4.4.4 代码实践

1. 实践目标：把 4.4.2 的时序图落到代码，确认每一步的线程归属。
2. 操作步骤（源码阅读型，无需 GPU）：
   - 依次打开上面 6 处源码，在时序图每一步旁标注：文件、行号、执行线程（全部在同一线程 T 上——体会 thread_local 的含义）；
   - 思考题落笔：如果 Rust 在拿到指针后先做了别的 FFI 调用再拷贝，会发生什么？
3. 需要观察的现象：`ffi_exception_message` 的三重防御——非 -1 不读、空指针返回空、空串归一——每层防的都是什么（陈旧读取、getter 失败、清空后的合法状态）。
4. 预期结果：一张完整标注的时序图；思考题答案：另一次 guarded 调用的 BEGIN 会清空缓冲，拷到的将是空串甚至悬垂内容（std::string 内部重分配）——所以「拿到即拷」是硬性纪律。

#### 4.4.5 小练习与答案

**练习 1**：BEGIN 里清空 `last_error` 的操作放在 `try` 块**外面**，有什么讲究？
**参考答案**：清空动作本身不可能抛异常（对短串赋值不触发分配），放外面保证「先清空、后进 try」的顺序不可动摇；若放进 try 内，语义会弱化成「异常前清空」。「进入即清空」是更强的保证，宏的实现选择了它。

**练习 2**：为什么 END 宏要区分 `std::exception` 与 `catch (...)` 两个分支？
**参考答案**：`e.what()` 只对 `std::exception` 派生类可用。`catch (...)` 捕获按非类类型抛出的一切异常，没有 what 可取，只能写死 "unknown C++ exception"——宁可损失细节也不让异常穿界。

**练习 3**：两个线程并发调用同一个 guarded 入口且都抛异常，消息会互相覆盖吗？
**参考答案**：不会。`g_ffi_last_error` 是 `thread_local`，各线程一份；每个线程的 -1 各自配对各自的消息。这也是 u2-l1 讲过的运行时线程模型下唯一安全的通道设计。

## 5. 综合实践

**任务：给 `dflash2_grouped_conv_cuda` 补齐 guard，并跑通它的既有测试。**

这是一个真实的「半成品」入口（4.1.3 已诊断：缺第 ② 件），补齐它要用到本讲全部四个模块的知识。

**第一步：写补齐清单。** 对照四件套逐项盘点（先写下来再动手）：

| 件 | 现状 | 证据 |
| --- | --- | --- |
| ① 检查 | 已前置到 wrapper（五个 `ensure!`） | [csrc/shared/dflash2_conv.cu:44-44](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/dflash2_conv.cu#L44)、[src/ops/dflash2_conv.rs:32-56](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/dflash2_conv.rs#L32-L56) |
| ② guard | **缺失** | 全文件不含 `PEGAINFER_FFI_GUARD` 宏 |
| ③ launch | 已有 | [csrc/shared/dflash2_conv.cu:51-52](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/dflash2_conv.cu#L51-L52) |
| ④ 错误返回 | 已有（map_cuda_error 路径） | [csrc/shared/dflash2_conv.cu:53-53](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/dflash2_conv.cu#L53) |

**第二步：动手补齐（示例代码——改在你的工作副本上，不要提交）。** `ffi_guard.cuh` 已在 [csrc/shared/dflash2_conv.cu:2-2](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/dflash2_conv.cu#L2-L2) 被 include（`map_cuda_error` 正来自它），所以只需包宏：

```cpp
// 示例代码：dflash2_grouped_conv_cuda 的入口体（对应 csrc/shared/dflash2_conv.cu:45-54）
extern "C" CUresult dflash2_grouped_conv_cuda(
    const __nv_bfloat16* input, const __nv_bfloat16* dynamic,
    const __nv_bfloat16* base, __nv_bfloat16* output, int rows, int hidden,
    int block, int group_size, int taps, int side, cudaStream_t stream) {
  PEGAINFER_FFI_GUARD_BEGIN                        // ② 补上：清空 last_error + try
  const int64_t count = static_cast<int64_t>(rows) * hidden;
  const int grid = static_cast<int>(std::min<int64_t>((count + 255) / 256, 65535));
  grouped_conv_kernel<<<grid, 256, 0, stream>>>(   // ③
      input, dynamic, base, output, rows, hidden, block, group_size, taps, side);
  return map_cuda_error(cudaGetLastError());       // ④
  PEGAINFER_FFI_GUARD_END(-1)                      // ②' 补上：异常 → 消息 + 哨兵
}
```

**哨兵选择的设计题**：该入口返回 `CUresult`，而 shared 目录在用的 `ret_on_throw` 全是 `-1`（int 入口）。`CUresult` 是无作用域枚举，`-1` 可以隐式转换；选 `-1` 与 `ffi_exception_message` 的 -1 判定及全仓惯例对齐。若你更倾向类型安全的 `CUDA_ERROR_UNKNOWN`，写下理由并与 `-1` 方案对比（提示：消息通道只在 -1 上被读取）。两种取舍的运行时表现**待本地验证**。

**第三步：验证。**

```bash
# 既有测试必须全绿（需 GPU；无 GPU 时 DeviceContext::new()? 会让该测试报错而非跳过）
cargo test --release -p pegainfer-kernels --test dflash2_conv -- --nocapture
# guard 通道不回归（无 GPU 时自动跳过）
cargo test --release -p pegainfer-kernels --test ffi_exception_catch -- --nocapture
```

**预期结果**：`dflash2_conv` 的对拍测试（[tests/dflash2_conv.rs:9-10](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/dflash2_conv.rs#L9-L10) 起，BF16 oracle 逐位对拍，L34-L37 还会用不同 batch 复用同一缓冲）通过；`ffi_exception_catch` 不受影响。改动验证完成后请 `git checkout -- csrc/shared/dflash2_conv.cu` 还原——本仓库不接受练习性改动入库；若作为真实加固提交，需按仓库规范附生产证据。

## 6. 本讲小结

- **四件套是结构性的，不是风格的**：检查（wrapper `ensure!` + 设备侧 `__trap()`）、guard（宏包异常）、launch（流参数由外部传入）、错误返回（`map_cuda_error` 归一）——缺一件的后果分别是非法访存、进程 abort、破坏流纪律、静默错误。
- **`map_cuda_error` 用巨型 switch 换「同一故障同一读数」**：runtime → driver 的归一让 Rust 侧只需一份解码逻辑；比 CUDA 12.3 新的错误码必须 `CUDART_VERSION` 门控。
- **一个 i32、四段互斥区间**：0 成功、-1 异常哨兵、小正数 CUresult、`100000+` cuBLAS——基址偏移让状态来源无歧义，`ffi_status_detail` 一次比较即可路由。
- **`last_error` 是 thread_local 的「进入即清空、异常才写入、读到即拷贝」通道**：它让 -1 哨兵携带人话消息，而「拿到指针立即拷贝」是唯一安全的读法。
- **guard 包住的是会 throw 的调用**：可以在 extern 入口层（`paged_attention.cu` 的 7 个入口），也可以在 launch 模板层（`paged_launch.cuh` 的 4 处）——位置跟着异常源走。
- **仓库里三个世代并存**：第一代 void 入口（`norm.cu`）、半成品（`dflash2_conv.cu`）、完备世代（`paged_attention.cu`）——读任何入口先做「缺件诊断」。

## 7. 下一步学习建议

- 下一讲 u4-l2「实战：端到端新增一个共享算子」会把本讲的四件套扩展成五处联动改动（kernel、FFI 声明、wrapper、测试、KERNELS.md 索引行），建议先完成本讲综合实践再进入。
- 想看 guard 宏如何被「批量」使用，通读 [csrc/shared/paged_attention.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_attention.cu)（7 个入口）与 [csrc/shared/paged_launch.cuh](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/paged_launch.cuh)（4 个 launch 模板）。
- 回顾 u2-l3 的 Rust 侧解码链（`ffi_exception_message` / `ffi_status_detail`），对照本讲 4.3/4.4 画出完整的编码-解码对称图。
- CUDA Graph 与流管理（u4-l3）会解释「为什么入口绝不自己造流」——本讲第 ③ 件的 stream 纪律在那一讲展开。
