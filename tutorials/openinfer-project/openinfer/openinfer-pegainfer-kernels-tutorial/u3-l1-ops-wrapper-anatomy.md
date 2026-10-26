# u3-l1 ops.rs 分发与算子包装解剖

## 1. 本讲目标

学完本讲，你应该能够：

1. 看懂 [src/ops.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs) 的两层结构：上半部按 cargo feature 门控声明子模块，中部逐条 `pub use` 再导出，尾部沉淀错误解码工具。
2. 掌握算子包装（wrapper）的标准五步：**查形 → 取指针 → 转类型 → 传流并 `unsafe` 调用 → 收 `Result`**，并能识别本 crate 中三代 wrapper 形态的差异。
3. 说清 `HiddenStates` 的「token i 连续存于偏移 i×hidden_dim」布局约定为何让 wrapper 只需传两个 `i32` 维度，以及 `checked_extent`、`Columns` band 在其中扮演的角色。
4. 独立打开任意一个 `src/ops/*.rs` 模块，不需要逐行讲解就能读懂它。

本讲的标本是 `rms_norm_into`——它足够短，又恰好暴露了所有关键模式。

## 2. 前置知识

本讲建立在两篇前置讲义之上，先快速回顾：

**来自 u2-l1（DeviceContext 与 GpuTensor）**：

- `DeviceContext` 持有 CUDA 上下文、唯一计算流和设备号（[src/tensor.rs:350-354](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L350-L354)）。
- 非类型化的 `DeviceVec` 是「一段 bf16 + 逻辑长度」，`HiddenStates` 是「一段 bf16 + `hidden_dim` × `seq_len`」两个运行时维度字段。
- `active_cu_stream(ctx)` 返回「当前生效的流」：thread-local 覆盖优先，否则回退上下文默认流（[src/tensor.rs:75-80](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L75-L80)）。这是 Green Context SM 分区和并发 prefill 的挂载点。

**来自 u2-l3（FFI 声明与跨语言错误契约）**：

- `Half` 就是 `u16`（[src/ffi.rs:5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs#L5)），与 CUDA 的 `__nv_bfloat16`/half 在 ABI 上的角色相同：跨边界只传位模式，不传 Rust 类型。
- C++ 侧入口被 `PEGAINFER_FFI_GUARD_BEGIN/END` 宏包住，异常被拦截后写入 thread-local 的 `last_error` 并返回 `-1` 哨兵；CUDA/cuBLAS 错误统一折叠为 `CUresult` 数值（[csrc/shared/ffi_guard.cuh:133-144](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/ffi_guard.cuh#L133-L144)）。
- 关键推论：**C++ 异常不能穿越 `extern "C"` 边界**，所以错误只能以返回码 + side channel 的形式回来。

**本讲新增的基础概念**：

- **wrapper（包装函数）**：把「安全的 Rust 借用世界」翻译成「裸指针 + 裸函数的世界」的那一小段代码。它不做计算，只做翻译和检查。
- **`cudarc` 的 `DevicePtr`/`DevicePtrMut` trait**：调用 `slice.device_ptr(&stream)` 会返回 `(指针数值, 守卫)` 二元组。守卫必须绑定到一个变量上保活（哪怕叫 `_gx`），否则切片可能在 `unsafe` 调用期间被回收。本 crate 依赖 cudarc 0.19.3（工作区 [Cargo.toml:78](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/Cargo.toml#L78)）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/ops.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs) | 算子包装层的门面：门控声明子模块、逐条再导出、错误解码工具（`ffi_exception_message`/`ffi_status_detail`） |
| [src/ops/norm.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs) | norm 家族 wrapper，本讲标本 `rms_norm_into` 与三代形态的活样本 |
| [src/ffi.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi.rs) | FFI 门面：`Half` 定义 + 按模型线门控的子模块 re-export |
| [src/ffi/shared.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs) | 跨模型共享 kernel 的 `unsafe extern "C"` 声明（norm、elementwise、attention 等） |
| [src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs) | `DeviceVec`/`HiddenStates`/`Columns`/`checked_extent`/`active_cu_stream` 所在地 |
| [csrc/shared/flashinfer_norm.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/flashinfer_norm.cu) | C++ 侧 `extern "C"` 入口与 FlashInfer norm 模板的委托 |
| [csrc/shared/elementwise.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/elementwise.cu) | 综合实践的对照样本：`softcap` kernel 与入口 |
| ../pegainfer-qwen3/src/prefill.rs | 真实调用方：Qwen3 prefill 如何消费 `ops::rms_norm_batch_into` |

调用链回顾（u1-l2 已建立）：**模型 crate → `ops` wrapper → `ffi` 声明 → `csrc` 入口 → `__global__` kernel**。本讲解剖前两环。

## 4. 核心概念与源码讲解

### 4.1 ops.rs 的 cfg 门控导出

#### 4.1.1 概念说明

`src/ops.rs` 是整个包装层的「目录页 + 总开关」。它自己几乎不含算法，只做三件事：

1. **声明子模块**——上半部按 cargo feature 决定哪些 `mod` 存在；
2. **再导出符号**——中部把每个子模块的公开函数逐条（或整体 glob）`pub use` 出来，让模型 crate 写 `pegainfer_kernels::ops::rms_norm_into` 而不是更深的路径；
3. **沉淀公共工具**——尾部放所有 wrapper 共用的错误解码和宽度检查助手。

为什么要门控？回顾 u1-l1：每条模型线一个 feature，默认构建零 Python、只含常驻面。`ops.rs` 就是这条纪律在源码里的落点——不开 feature，对应模块**根本不参与编译**，而不是编译了但不导出。

#### 4.1.2 核心流程

`ops.rs` 的组织流程可以画成：

```text
cargo feature 集合
   │
   ├─ 常驻算子（无 cfg）：attention / elementwise / embedding / linear / lora / norm
   │     + dflash2 / dflash2_conv / sampling
   │
   ├─ 单 feature 门控：gemma4、glm52、k3、kimi-k2、qwen35、qwen4、
   │     deepseek-v2-lite、moe(deepep)
   │
   ├─ 组合门控：marlin_face = any(gemma4, kimi-k2)
   │
   └─ 每条 mod 声明配一组同条件 pub use
         （大模块用 glob `pub use gemma4::*;`，小模块逐条列出）
```

门控声明与再导出是**成对**的：`#[cfg] mod x;` 少了对应的 `#[cfg] pub use x::…;`，符号就出不去；反过来，`pub use` 存在而 `mod` 被门掉，编译直接报错。读这份文件时永远把两半对照着读。

#### 4.1.3 源码精读

**模块声明区**（第 3–49 行）：常驻模块裸声明，模型专属模块带 cfg。

- [src/ops.rs:3-13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L3-L13) —— `mod attention;` 无门控常驻；`deepep` 挂 `moe`、`deepseek_v2_lite` 系列挂 `deepseek-v2-lite`；`dflash2`/`dflash2_conv` 也是常驻（u1-l2 已讲：Qwen3 复用常驻面）。
- [src/ops.rs:42-44](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L42-L44) —— `marlin_face` 用 `any(feature = "gemma4", feature = "kimi-k2")`：量化 GEMM 基座被两条模型线共享，这是「feature 与模块是多对多」的最小例子。
- [src/ops.rs:45-48](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L45-L48) —— `norm` 常驻；`qwen35`、`qwen4` 各自门控。

**再导出区**（第 51–277 行）：

- [src/ops.rs:223-239](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L223-L239) —— norm 家族逐条列出：类型（`GatedNormActivation`）和全部公开函数（`rms_norm`、`rms_norm_into`、`rms_norm_batch_into`……）。逐条导出让「这个 crate 到底提供哪些 op」一眼可数，也和 KERNELS.md 的索引行一一对应。
- [src/ops.rs:240-243](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L240-L243) —— qwen4 的两个导出各自带 `#[cfg(feature = "qwen4")]`，与第 47–48 行的 mod 声明成对。
- 对比 [src/ops.rs:138-139](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L138-L139) —— `gemma4` 用整体 glob `pub use gemma4::*;`。模块大了逐条列不划算，就换成 glob；代价是符号清单要进 gemma4 模块里看。

**公共工具区**（第 279–318 行）——这是本模块「自产自销」的部分：

- [src/ops.rs:279-285](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L279-L285) —— `checked_i32`/`checked_u32`：把 `usize` 安全地转成 FFI 需要的 `i32`/`u32`，溢出时返回带上下文的错误而不是截断。子模块通过 `super::checked_i32` 调用（下文 4.2 会看到实例）。
- [src/ops.rs:287-304](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L287-L304) —— `ffi_exception_message(result)`：当 result 是 `-1` 哨兵时读 C++ 侧 thread-local `last_error`（对应声明在 [src/ffi/shared.rs:1027](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L1027)），拼成可读消息。
- [src/ops.rs:306-318](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L306-L318) —— `CUBLAS_STATUS_BASE = 100_000` 与 `ffi_status_detail`：把「垫高过的 cuBLAS 状态」和「CUDA 错误码」区分命名。

错误解码工具住在这里而不是 `ffi.rs`，正体现了分工：`ffi.rs` 只管声明（「有哪些符号」），`ops.rs` 管包装语义（「调用失败如何变成人话」）。

#### 4.1.4 代码实践

**实践目标**：验证门控的真实效果——不开启 feature 时，模型专属符号确实不存在。

**操作步骤**：

1. 在仓库根目录运行：
   ```bash
   cargo tree -p pegainfer-kernels -e features | head -40
   ```
   观察默认 feature 集合下激活了哪些 feature。
2. 在任一模型 crate 里找一个共享算子的调用点，例如 Qwen3 的 prefill：
   ```bash
   grep -rn "ops::rms_norm_batch_into" ../pegainfer-qwen3/src | head
   ```
3. 尝试写一个假调用（不必真的建文件，`cargo check` 想象即可）：在某个模型 crate 中调用 `pegainfer_kernels::ops::paged_attention_batch_decode_hd256_into`（qwen35 门控符号）但不开启 `qwen35` feature，观察编译错误。

**需要观察的现象**：步骤 2 找到 [../pegainfer-qwen3/src/prefill.rs:215-221](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/prefill.rs#L215-L221) 处 `ops::rms_norm_batch_into(&self.ctx, hidden, &layer.input_layernorm, self.config.rms_norm_eps, &mut bufs.normed)` —— 注意它**没有接 `?`**，因为这个 wrapper 返回 `()`（4.2 节解释为什么）。步骤 3 的错误信息应形如「cannot find function … in module」，证明门控发生在编译期。

**预期结果**：能列出默认构建下 `ops.rs` 实际编译的子模块清单；确认 qwen35/qwen4/gemma4 等符号不可见。（步骤 1、2 可在任何环境跑；若本机无 Rust 工具链，标注「待本地验证」。）

#### 4.1.5 小练习与答案

**练习 1**：`marlin_face` 为什么挂在 `any(gemma4, kimi-k2)` 而不是 `moe`？

**参考答案**：`moe` 底座对应的是 DeepEP 多机通信（`deepep` 模块），而 `marlin_face` 是量化 GEMM 的 Rust 包装面，被 gemma4 的 W4A16 与 kimi-k2 的 INT4 experts 两条线共用。门控条件跟着**实际消费者**走，而不是跟着「都跟量化沾边」的语义分类走——这是本 crate 的通行做法。

**练习 2**：为什么 `norm`、`elementwise`、`sampling` 不需要 cfg？

**参考答案**：它们是所有模型线都要用的共享算子（RMSNorm、逐元素、采样几乎是每个 LLM 的标配），且只依赖常驻的 CUDA/cuBLAS/FlashInfer 构建，没有额外的构建期需求（如 Python/Triton），因此放进默认面既不破坏「默认零 Python」也不增加体积负担的量级。

**练习 3**：`pub use elementwise::add_batch;`（[src/ops.rs:101](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L101)）与 `pub use gemma4::*;` 两种风格各自损失了什么信息？

**参考答案**：逐条导出在 `ops.rs` 内可数、可与 KERNELS.md 对表，但每加一个符号要改两处文件；glob 导出零维护，但符号清单要进子模块才能看到，且 glob 可能把不想公开的内部类型一并带出（gemma4 模块内部需自行控制 `pub` 粒度来兜底）。

### 4.2 wrapper 标准五步：以 rms_norm_into 为标本

#### 4.2.1 概念说明

wrapper 是包装层的最小工作单元。它站在两个世界的交界上：

- **Rust 侧**：`&DeviceVec`、`&mut DeviceVec` 这样的借用，维度是 `usize`，错误是 `anyhow::Result`。
- **FFI 侧**：`*const Half` 裸指针，维度是 `i32`，流是 `CUstream`，错误是返回码或干脆没有。

wrapper 的职责就是把左侧安全地降级成右侧，再把右侧的结果升级回左侧。本 crate 的 wrapper 收敛出一个**标准五步**模式：

| 步骤 | 做什么 | 典型代码 |
|------|--------|----------|
| ① 查形 | 校验长度/形状/参数合法性 | `assert_eq!(x.len, out.len)` 或 `ensure!(…)` |
| ② 取指针 | 向 cudarc 借设备指针，绑定守卫保活 | `let (x_ptr, _gx) = x.data.device_ptr(&ctx.stream);` |
| ③ 转类型 | 裸指针 cast 到 `ffi::Half`，`usize` 转 `i32` | `x_ptr as *const ffi::Half`、`x.len as i32` |
| ④ 传流调用 | 解析 active stream，`unsafe` 调 extern | `crate::tensor::active_cu_stream(ctx)` |
| ⑤ 收结果 | 把返回码翻译成 `Result` | `result.result()?` 或直接 `Ok(())` |

RMSNorm 的数学语义（供理解 kernel 在算什么，wrapper 只搬运参数）：

\[ \mathrm{out}_i = \frac{x_i}{\sqrt{\tfrac{1}{n}\sum_{j=1}^{n} x_j^2 + \varepsilon}} \cdot w_i \]

#### 4.2.2 核心流程

```text
rms_norm_into(ctx, x, weight, eps, out)
  │
  ① assert_eq!(x.len, out.len)            // 查形：panic 风格（旧代）
  ② x/w: device_ptr       out: device_ptr_mut
  │     守卫 _gx/_gw/_go 绑定保活
  ③ x_ptr as *const Half …  x.len as i32
  ④ unsafe { ffi::rms_norm_cuda(…, active_cu_stream(ctx)) }
  │     FFI 返回类型是 () —— 无处可查错误
  ⑤ Ok(())
```

值得注意的是，本 crate 的 wrapper 并非只有一种形态。按「错误能不能传回来」可分三代：

| 代际 | FFI 返回 | Rust 返回 | 查形风格 | 代表 |
|------|----------|-----------|----------|------|
| 第一代 | `()` | `()` 或恒 `Ok(())` 的 `Result<()>` | `assert_eq!` | `rms_norm_batch_into`（连 Result 都没有）、`rms_norm_into` |
| 第二代 | `CUresult` | `Result<()>` | `const OP` + `ensure!` + `checked_extent` + `checked_i32` | `rms_norm_batch_dual_into`、`softcap_bf16_in_place` |
| 分发代 | `()` | `()` | 断言 + `match` 挑 FFI 符号 | `rms_norm_gated_batch_into` |

代际差异的根源在 C++ 侧入口是否用 `map_cuda_error(cudaGetLastError())` 回传错误（u4-l1 会展开 CUDA 侧约定）。

#### 4.2.3 源码精读

**标本本体**：[src/ops/norm.rs:14-37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L14-L37) —— `rms_norm_into` 全文只有 23 行：

```rust
pub fn rms_norm_into(
    ctx: &DeviceContext,
    x: &DeviceVec,
    weight: &DeviceVec,
    eps: f32,
    out: &mut DeviceVec,
) -> Result<()> {
    assert_eq!(x.len, out.len);                              // ①
    let (x_ptr, _gx) = x.data.device_ptr(&ctx.stream);       // ② 读借用
    let (w_ptr, _gw) = weight.data.device_ptr(&ctx.stream);  // ②
    let (out_ptr, _go) = out.data.device_ptr_mut(&ctx.stream); // ② 写借用走 *_mut
    unsafe {
        ffi::rms_norm_cuda(                                  // ④
            x_ptr as *const ffi::Half,                       // ③
            w_ptr as *const ffi::Half,
            out_ptr as *mut ffi::Half,
            x.len as i32,                                    // ③ 隐患：直接 cast
            eps,
            crate::tensor::active_cu_stream(ctx),            // ④ 关键：不是 ctx.stream
        );
    }
    Ok(())                                                   // ⑤ 恒 Ok
}
```

五个细节，每个都有讲究：

1. **守卫绑定**：`_gx` 带下划线前缀但仍是具名绑定，活到函数结尾；如果写成 `let (_, _) = …` 或 `let _ = …`，守卫立即 drop，切片可能被回收，unsafe 调用变成悬垂指针。这是 cudarc 安全 API 与 unsafe 世界的接缝。
2. **读 vs 写**：只读参数走 `device_ptr`，输出走 `device_ptr_mut`——Rust 的借用规则因此穿透到指针层面。
3. **`x.len as i32`**：直接 cast 在 `len > i32::MAX`（约 21 亿 bf16 ≈ 4 GB）时静默截断。这就是 ops.rs 里 `checked_i32` 存在的原因——旧 wrapper 是历史形态，新 wrapper 一律改用受检转换。
4. **`active_cu_stream(ctx)` 而非 `ctx.stream.cu_stream()`**：每次 launch 都要尊重 thread-local 流覆盖，否则 Green Context SM 分区与并发 prefill 的流切换对这一发 kernel 失效。
5. **签名在两端对齐**：FFI 侧声明 [src/ffi/shared.rs:22-29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L22-L29) —— `rms_norm_cuda(x, weight, out, n, eps, stream)` 返回类型是空 `()`。对比同文件 [src/ffi/shared.rs:41-52](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L41-L52) 的 `rms_norm_batched_dual_cuda` 返回 `CUresult`——两个声明并排放着，就是两代契约的界碑。

**第二代对照**：[src/ops/norm.rs:174-234](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L174-L234) —— `rms_norm_batch_dual_into`。不必逐行读，注意它在五步上的升级：

- ① 查形从一行 `assert_eq!` 膨胀成 `const OP: &str = "rms_norm_batch_dual_into";` 加一组 `ensure!`（形状、背衬、`scale_a.is_finite()`），错误信息带操作名（[src/ops/norm.rs:184-207](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L184-L207)）；
- ③ 维度转换改用 `super::checked_i32(x.hidden_dim, "… hidden_dim")?`（[src/ops/norm.rs:211-212](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L211-L212)）；
- ⑤ 用 `result.result()?;` 把 `CUresult` 升级为 `anyhow::Error`（[src/ops/norm.rs:232-233](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L232-L233)）。

**C++ 侧对应**（只看返回值差异，kernel 细节留给 u3-l6）：

- [csrc/shared/flashinfer_norm.cu:548-558](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/flashinfer_norm.cu#L548-L558) —— `extern "C"` 块内的 `rms_norm_cuda` 把工作整体委托给 FlashInfer 的 `flashinfer::norm::RMSNorm` 模板，返回 `void`：launch 失败无人知晓。
- [csrc/shared/flashinfer_norm.cu:571-586](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/flashinfer_norm.cu#L571-L586) —— `rms_norm_batched_dual_cuda` 是自研 kernel 入口，末尾 `return map_cuda_error(cudaGetLastError());` 把 launch 错误折进 `CUresult` 送回 Rust。

**调用方视角**：[../pegainfer-qwen3/src/prefill.rs:214-221](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen3/src/prefill.rs#L214-L221) —— Qwen3 的 prefill 第一步就是 `ops::rms_norm_batch_into`。它返回 `()`，调用处没有 `?`；这是第一代形态向调用方暴露的直接后果：错误处理责任完全上移给了「输入一定正确」的工程纪律。

#### 4.2.4 代码实践

**实践目标**：把「五步」变成肌肉记忆——给两个 wrapper 做五步对照表。

**操作步骤**：

1. 打开 [src/ops/norm.rs:552-576](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L552-L576)（`rms_norm_batch_into`，第一代）和 [src/ops/norm.rs:174-234](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L174-L234)（`rms_norm_batch_dual_into`，第二代）。
2. 对每个函数逐行标注它属于五步中的哪一步，写出类似这样的表（示例答案）：

| 五步 | `rms_norm_batch_into` | `rms_norm_batch_dual_into` |
|------|----------------------|---------------------------|
| ① 查形 | `assert_eq!` ×3（L559-561） | `ensure!` ×7 + `checked_extent` ×3（L185-210） |
| ② 取指针 | 3 次借用（L562-564） | 5 次借用（L213-217） |
| ③ 转类型 | `hidden_dim as i32` 直接 cast | `super::checked_i32(...)?`（L211-212） |
| ④ 传流调用 | `active_cu_stream(ctx)`（L573） | 同（L229） |
| ⑤ 收结果 | 无（函数返回 `()`） | `result.result()?`（L232） |

3. 数一数：第一代在形状不符时是 panic（进程崩溃），第二代返回 `Err`（可恢复、带上下文）。思考哪种更适合长跑的推理服务。

**需要观察的现象**：两个函数体长度差约 3 倍，但「骨架」完全同构；差异全部集中在①查形的完备性和⑤错误回收上。

**预期结果**：得到一张五步对照表；能口头回答「第二代贵在哪、值不值」。纯纸面任务，无需 GPU，也不依赖本地运行。

#### 4.2.5 小练习与答案

**练习 1**：`rms_norm_into` 的 FFI 符号返回 `()`，那 launch 失败（比如 grid 维度非法）时会发生什么？

**参考答案**：这一发 kernel 的失败信息会丢失——`rms_norm_cuda` 是 `void`，不调 `cudaGetLastError()`，Rust 侧也无从得知。错误要么在后续某个会检查流状态的同步点（如 `stream_spin_wait` 或下一次拷贝）以「迟到的 CUDA error」形式炸出来，要么被下一发 launch 覆盖。这正是第二代入口一律返回 `CUresult` 的动机：错误要就地回收。

**练习 2**：找出本文件里一个**没有**使用 `active_cu_stream` 的例外，并说明它可能的影响。

**参考答案**：[src/ops/norm.rs:99-137](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L99-L137) 的 `layer_norm_into` 在调用处直接传 `ctx.stream.cu_stream()`（L131）。若该 op 在流覆盖激活期间被调用（例如 gemma4 的异步 prefill lane），它仍会发射到上下文默认流，破坏预期的并发隔离。读 wrapper 时「流参数用哪个」应当成为固定检查项。

**练习 3**：为什么 wrapper 普遍提供 `*_into`（写入调用者给的缓冲）和裸名（自己分配）两个版本，如 `rms_norm_into` 与 `rms_norm`（[src/ops/norm.rs:82-92](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L82-L92)）？

**参考答案**：`_into` 版零分配，服务于解码路径与 CUDA Graph 捕获——预分配缓冲换指针稳定（u4-l3 展开）；分配版只是 `_into` 之上的一层糖（先 `DeviceVec::zeros` 再委托），便于测试和一次性调用。两个名字的并存就是「引擎纪律（零分配）」与「易用性」的分层。

### 4.3 HiddenStates 布局约定

#### 4.3.1 概念说明

wrapper 传给 FFI 的维度参数只有 `hidden_dim` 和 `seq_len` 两个 `i32`，却足以让 kernel 正确定位每个元素——这依赖一个贯穿全 crate 的**布局约定**：

> **token i 的 `hidden_dim` 个 bf16 连续存放在偏移 `i × hidden_dim` 处**（见 [src/tensor.rs:609](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L609) 的 `from_host` 文档注释）。

两个直接推论（u2-l1 已从 cuBLAS 视角论证过，本讲从 wrapper 视角落实）：

1. **kernel 索引公式唯一**：第 `row` 行第 `col` 列 = `data[row * hidden_dim + col]`，没有第三个维度参数的事。cuBLAS 视角下同一段内存是 `[hidden_dim, seq_len]` 的列主序矩阵，GEMM 零转置直通。
2. **逻辑尺寸与背衬缓冲分离**：`HiddenStates` 的 `data` 可以比 `hidden_dim * seq_len` 大（预分配 arena 的常态），`checked_extent` 负责守住「逻辑 ≤ 背衬」这条线，指针稳定的前提就在这里。

#### 4.3.2 核心流程

wrapper 与布局约定的协作流程：

```text
模型 crate 持有预分配的 HiddenStates（背衬大、逻辑小）
   │
   wrapper ① checked_extent("x")?      → 校验 data.len() >= hidden_dim*seq_len
   │        ② checked_i32(hidden_dim)  → 安全降宽
   │        ③ device_ptr               → 取背衬首地址（不随逻辑尺寸变化）
   │        ④ FFI(hidden_dim, seq_len) → kernel 用 行×hidden_dim+列 寻址
   ▼
解码/Graph 回放：缓冲地址不变，只改 hidden_dim/seq_len 重放
```

再进一步的变体是 **band（列带）**：`Columns` 描述「某个融合投影结果张量的第 `[col, col+width)` 列带」。此时 kernel 寻址要用**父张量的行宽**（`gate.states.hidden_dim`）作 stride，而不是 band 自己的 width——因为相邻 token 的同名列带之间隔着父张量一整行。

#### 4.3.3 源码精读

- [src/tensor.rs:551-555](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L551-L555) —— `HiddenStates` 结构：一段 `CudaSlice<bf16>` 加两个运行时维度字段。没有 stride 字段，因为 stride 恒等于 `hidden_dim`——约定代替状态。
- [src/tensor.rs:580-594](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L580-L594) —— `checked_extent`：乘法防溢出 + 背衬下界校验，失败时返回带 `what` 上下文的错误。第二代 wrapper 每个张量参数都要过一遍它。
- [src/tensor.rs:759-790](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L759-L790) —— `Columns`：`(states, col, width)` 三元组描述列带；`assert_in_row` 在发射前确认带不越出行宽，注释明说「按行 stride 读 width 列否则会冲过最后一行」。
- **band 的 wrapper 落地**：[src/ops/norm.rs:650-689](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L650-L689) —— `rms_norm_gated_batch_into`。看三个关键点：
  - L662-663：`gate.into()` 归一化成 `Columns` 后立即 `gate.assert_in_row("rms_norm_gated gate")`；
  - L673：`let g_ptr = g_ptr + (gate.col * std::mem::size_of::<bf16>()) as u64;` —— 指针手工加上列带偏移，这是 wrapper 里少见的指针算术；
  - L687：传给 kernel 的 stride 是 `gate.states.hidden_dim`（父张量行宽），不是 band 宽度——kernel 内部按它跨行。
- 该函数同时是「分发代」样本：L676-705 用 `match activation` 在 `rms_norm_gated_cuda`（SiLU）与 `rms_norm_gated_sigmoid_cuda`（Sigmoid）两个 FFI 符号间选择——Qwen3.5 与 Qwen3.8-Flash-Next 共用同一 kernel 家族、只差激活函数（枚举定义见 [src/ops/norm.rs:631-642](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L631-L642) 的文档注释）。

#### 4.3.4 代码实践

**实践目标**：通过读真实测试，验证「布局约定 + wrapper」被什么门禁守护。

**操作步骤**：

1. 阅读 [src/ops/norm.rs:708-768](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/norm.rs#L708-L768)：`mod parity` 里的 `assert_bitwise`（L712-725）逐位比较两个设备张量（`to_bits()` 后 `assert_eq`），`the_dual_norm_matches_two_standalone_norms`（L747-768）断言「融合 dual norm ≡ 两次独立 norm + 一次 scale」。这些测试都带 `#[ignore = "requires a GPU"]`。
2. 注意 L987-995 有一个**不需要 GPU** 的测试 `assert_close_rejects_non_finite_values`——它守护的是对拍工具本身（NaN 不许蒙混过关）。在任何有 Rust 工具链的环境运行：
   ```bash
   cargo test --release -p pegainfer-kernels --lib assert_close_rejects_non_finite_values
   ```
3. 有 GPU 的环境下再跑被 ignore 的 parity 门禁（可选）：
   ```bash
   cargo test --release -p pegainfer-kernels --lib parity -- --ignored
   ```

**需要观察的现象**：步骤 2 应输出 1 个测试通过（它纯 host 侧，`#[should_panic]` 验证 NaN 会触发断言）。步骤 3 若无 GPU 会失败——这是刻意的环境门，不是 bug。

**预期结果**：理解「位级等价（bitwise）」门禁的存在理由：融合 kernel 的卖点是**逐位复现**被替换的独立调用链，容差对拍不足以锁住这个承诺。（步骤 2 待本地验证；无工具链则记录为纸面阅读任务。）

#### 4.3.5 小练习与答案

**练习 1**：`HiddenStates` 为什么不存 stride 字段？

**参考答案**：stride 恒等于 `hidden_dim`，存了就是冗余状态，冗余状态就有失同步的风险。用「token 连续、行宽即 stride」的约定替代字段，索引公式在全 crate 唯一，kernel 签名也因此少一个参数。代价是不支持padding 行——本 crate 宁可换缓冲也不用非连续布局。

**练习 2**：`rms_norm_gated_batch_into` 里 gate 为什么可以直接用「父张量行宽」当 stride？给出一个具体场景。

**参考答案**：因为 gate 常常是融合投影输出的一列带：QKV/gate_up 一次 GEMM 产出 `[seq_len, q+kv]` 或 `[seq_len, gate+up]` 的大张量，gate 只是其中 `[col, col+width)` 的带。相邻 token 的同一列带之间隔着父张量一整行（`gate.states.hidden_dim` 个 bf16），所以 kernel 必须按父行宽跨行寻址；wrapper 的 `g_ptr + col * 2` 只负责把起点挪到带上。

**练习 3**：`checked_extent` 只校验 `data.len() >= hidden_dim * seq_len`。若调用方把 `hidden_dim` 和 `seq_len` 互换传入（积相同），wrapper 层能发现吗？

**参考答案**：不能——乘积不变，`checked_extent` 通过；但 kernel 会按错误的行宽寻址，读到张量错位的值（不会越界，因为总元素数没超）。这正是查形步骤①还要 `ensure!(weight.len == x.hidden_dim)` 这类**交叉校验**的原因：让维度与已知长度的对象（权重）对表，单靠背衬大小兜不住语义错位。

## 5. 综合实践

**任务**：从 `ops.rs` 任选一个**未在本讲正文解剖过**的导出函数（elementwise 或 sampling 家族），画出「Rust wrapper → device_ptr → extern 声明 → csrc 入口 → `__global__` kernel」的完整调用图，并标注每一步的类型。

下面用 `softcap_bf16_in_place`（elementwise 家族、gemma4 的 final-logit softcap）给出一份**完整参考答案**，你要做的是换一个函数（建议 `suppress_logits_bf16_in_place`，或 sampling 的 `argmax` 家族）复刻同样的五站图。

### 参考答案：softcap_bf16_in_place 调用图

```text
① Rust wrapper  src/ops/elementwise.rs:1089-1112
   pub fn softcap_bf16_in_place(
       ctx: &DeviceContext, buf: &mut HiddenStates, cap: f32) -> Result<()>
   查形：cap 正有限（ensure!） + buf.checked_extent（逻辑≤背衬）
   取指针：buf.data.device_ptr_mut(&ctx.stream) → (u64, 守卫)
   转类型：buf_ptr as *mut ffi::Half（*mut u16）、n: i32（经 checked_i32）
        │
        ▼ unsafe 调用，传 active_cu_stream(ctx)
② extern 声明   src/ffi/shared.rs:219-224
   pub fn softcap_bf16_in_place_cuda(
       buf: *mut Half, cap: f32, n: i32, stream: CUstream) -> CUresult
        │  （同一符号名由 C++ 侧链接期对齐）
        ▼
③ csrc 入口     csrc/shared/elementwise.cu:898-904
   CUresult softcap_bf16_in_place_cuda(
       __nv_bfloat16 *buf, float cap, int n, cudaStream_t stream)
   { grid = n/256 向上取整; launch; return map_cuda_error(cudaGetLastError()); }
        │  <<<grid, 256, 0, stream>>>
        ▼
④ __global__ kernel  csrc/shared/elementwise.cu:346-362
   softcap_bf16_kernel(__nv_bfloat16 *__restrict__ buf, float cap, int n)
   grid-stride 循环：buf[idx] = bf16(cap * tanhf(f32(buf[idx]) / cap))
```

各站类型对照（你复刻时也要给这张表）：

| 站点 | 缓冲类型 | 维度类型 | 流/错误 |
|------|----------|----------|---------|
| wrapper | `&mut HiddenStates`（借用） | `usize`（`checked_i32` 转 `i32`） | `Result<()>` |
| extern | `*mut Half`（= `*mut u16`） | `i32` | `CUstream` / `CUresult` |
| csrc 入口 | `__nv_bfloat16 *` | `int` | `cudaStream_t` / `CUresult` |
| kernel | `__nv_bfloat16 *__restrict__` | `int` | 流由 launch 第 4 参携带 |

**操作步骤**：

1. 从 [src/ops.rs:99-133](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L99-L133)（elementwise 导出区）或 [src/ops.rs:262-277](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops.rs#L262-L277)（sampling 导出区）选定目标函数。
2. 用 `grep -n "函数名" src/ops/<家族>.rs src/ffi/shared.rs csrc/shared/*.cu` 定位四站。
3. 画出与参考答案同构的调用图和类型表；标注它属于哪一代 wrapper（看 FFI 返回 `()` 还是 `CUresult`、查形用 `assert` 还是 `ensure`）。

**需要观察的现象 / 预期结果**：四站齐全、符号名一致、流参数来源（`active_cu_stream` 还是 `ctx.stream`）明确、代际归属有依据。纯源码阅读任务，无需 GPU；grep 命令待本地验证。

## 6. 本讲小结

- `src/ops.rs` 是包装层门面：cfg 门控的 `mod` 声明与 `pub use` 再导出**成对出现**，尾部沉淀 `checked_i32` 与 FFI 错误解码工具；门控在编译期生效，不开 feature 符号就不存在。
- wrapper 标准五步：**查形 → 取指针（守卫保活）→ 转类型（cast 或受检转换）→ 传 `active_cu_stream` 并 unsafe 调用 → 收 `Result`**。
- 本 crate 存在三代 wrapper：第一代 `void` FFI 无错误回收（`rms_norm_batch_into` 连 `Result` 都不返回），第二代 `CUresult` + `ensure!` + `checked_extent` + `checked_i32`（`rms_norm_batch_dual_into`），分发代用 `match` 挑 FFI 符号（gated norm 的 SiLU/Sigmoid）。
- `HiddenStates` 布局约定「token i 在偏移 i×hidden_dim」让 kernel 只需两个 `i32` 维度；`checked_extent` 守住「逻辑 ≤ 背衬」以支撑预分配与指针稳定；`Columns` band 让融合投影的列带按父张量行宽寻址。
- 读任何 wrapper 的固定检查项：流参数用的是不是 `active_cu_stream`、维度转换是 cast 还是受检、launch 错误有没有被回收。
- 融合 kernel 的正确性门禁是**位级等价**（`assert_bitwise`），不只是容差对拍——这是「融合 ≡ 被替换的调用链」这一产品承诺的守护方式。

## 7. 下一步学习建议

- **u3-l2 线性层与 cuBLAS**：本讲的五步模式在 GEMM 家族上遇到新变量——cuBLASLt 算法选择、workspace 与 graph-safe 的取舍、`PEGAINFER_GEMM_LT_CACHE` 离线调优。`gemm_into` 与 `gemm_graphsafe_into` 的差异正是 wrapper 层「为 Graph 而分化」的下一层。
- **u3-l3 paged KV 布局元数据**：`HiddenStates` 之外另一套布局语言（`KvStorage` × `KvFormat` 正交轴），attention wrapper 全靠它描述页表。
- **u3-l6 norm 家族与第一个 CUDA kernel 精读**：本讲只看了 `flashinfer_norm.cu` 的入口形态，下一站下沉到 kernel 内部的 grid/block 映射。
- 顺手的巩固练习：把综合实践里你画的那张调用图与 [KERNELS.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md) 的对应行对表，确认 op_id → wrapper → FFI symbol → .cu 文件四段全部对得上。
