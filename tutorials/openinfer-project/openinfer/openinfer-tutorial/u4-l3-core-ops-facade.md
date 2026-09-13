# u4-l3 core::ops 算子门面与注意力包装

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PegaInfer 共享 GPU 算子的三层结构——`kernels::ffi` 原始 extern 声明、`kernels::ops` 安全包装、`core::ops` 模型门面——以及每一层的职责边界。
2. 以 `prefill_attention_paged_into` 为标本，从模型 crate 的调用点一路追到 C ABI 的 extern 声明，写出每层的函数签名对照。
3. 掌握 attention 包装族的输入输出：`PrefillPagedPlan`（预填充计划）、`SplitKvCsr`（split-KV 解码的 CSR 分页计划）以及它们背后的 paged-KV 几何。
4. 会用 `KernelCall` / `TensorSpec` / `CallSpec` 这套「与显存无关的调用元数据」描述一次内核调用。

本讲是单元 4 的第三讲，承接 u4-l1 的张量与设备层（`DeviceContext`、`HiddenStates`、bf16 体系），向下打通到内核；也为 u5-l3（Qwen3 前向计算）和 u6-l4（CUDA Graph 解码路径）准备「算子从哪来、长什么样」的基础。

## 2. 前置知识

**三层结构是什么意思？** PegaInfer 的模型 crate（如 qwen3）从不直接碰 C 指针。一次 GPU 计算从上到下穿过三层：

- **门面层 `core::ops`**：模型 crate 直接 `use` 的入口。它的职责是「类型翻译」——把 core 侧的类型（如 `KvLayout`）换成 kernels 侧的类型，其余原样转发。
- **包装层 `kernels::ops`**：安全 Rust 与 unsafe 的边界。它做形状/几何校验、取出 `CudaSlice` 的裸指针、计算偏移与 stride、调用 FFI、检查返回值并把错误翻译成 `anyhow::Error`。
- **FFI 层 `kernels::ffi`**：`unsafe extern "C"` 块里的函数声明，与 `pegainfer-kernels/csrc/*.cu`（u4-l2 讲过由 build.rs 用 nvcc 预编译成 `libkernels_cuda.a`）一一对应，参数全是裸指针、`i32`/`i64` 和 `CUstream`。

**CSR（Compressed Sparse Row）格式**：一种用「值数组 + 行偏移数组」表达变长分组的方式。本讲中它描述「每个请求占用哪些 KV 块」：`o_indptr[i]..o_indptr[i+1]` 给出第 i 个请求在 `request_indices` / `kv_tile_indices` 里拥有的条目区间。变长请求列表由此变成定长数组 + 偏移，可以直接 memcpy 到 GPU。

**paged KV 与 GQA**：KV cache 按「页」存放，一页含 `page_size` 个 token 位置、所有层的 K 和 V（u7-l1 会深入块池；本讲只需要几何布局）。GQA（Grouped Query Attention）指多个 Q 头共享一组 KV 头，`num_q_heads / num_kv_heads` 是组大小。FlashInfer 只实例化了部分组大小：`SUPPORTED_GQA_GROUP_SIZES = &[1, 2, 3, 4, 8]`，其他比值在分发时抛错（见 [pegainfer-kernels/src/ops/attention.rs:14-16](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L14-L16)）。

**FlashInfer**：本算子族的注意力后端。预填充用 BatchPrefillWithPagedKVCache，解码用 BatchDecode（含 partition-KV/split-K 变体），底层内核在构建期编进静态库（见 u4-l2）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pegainfer-core/src/ops.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops.rs#L1-L18) | 门面层入口：声明 `attention`、`paged_plan`、`call_spec` 子模块，并大批量 re-export `kernels::ops` 的符号 |
| [pegainfer-core/src/ops/attention.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/attention.rs#L1-L50) | 5 个 attention 包装的门面侧：翻译 `KvLayout` 后转发给 kernels |
| [pegainfer-core/src/ops/paged_plan.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/paged_plan.rs#L1-L69) | `SplitKvCsr` + `build_split_kv_csr`（纯主机侧），以及 `PrefillPagedPlan` 的 newtype 包装 |
| [pegainfer-kernels/src/ops.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops.rs#L1-L25) | 包装层模块表：attention/elementwise/norm/linear/sampling 等共享面，加 feature 门控的模型子面 |
| [pegainfer-kernels/src/ops/attention.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L580-L736) | 本讲主标本：`PrefillPagedPlan` 与 prefill/decode/split-KV 包装的真实实现 |
| [pegainfer-kernels/src/ffi.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi.rs#L1-L32) | FFI 层组织：`Half` ABI 类型 + 按 feature 裁剪的子模块 re-export |
| [pegainfer-kernels/src/ffi/shared.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L490-L656) | `unsafe extern "C"` 块：paged attention 家族的 C ABI 声明 |
| [pegainfer-kernels/src/paged_kv.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/paged_kv.rs#L16-L66) | `PagedKvLayout` / `KvStorage`：内核视角的页几何纯值类型 |
| [pegainfer-kernels/src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L307-L346) | `KernelCall` / `TensorSpec` / `AxisSpec`：擦除后的调用元数据 |
| [pegainfer-core/src/ops/call_spec.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/call_spec.rs#L36-L104) | `PagedDecodeCallSpec` 等预制的 KernelCall 构造器 |
| [pegainfer-qwen3/src/batch_decode_buffers.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/batch_decode_buffers.rs#L410-L452) | 真实调用方：`sync_split_kv_meta` 如何构建并上传 CSR |

## 4. 核心概念与源码讲解

### 4.1 三层调用约定：从 `unsafe extern "C"` 到模型门面

#### 4.1.1 概念说明

模型 crate 需要 100 多个 GPU 算子（RMSNorm、GEMM、RoPE、注意力、采样……），如果每个模型都直接写 `unsafe` 裸指针调用，既不安全也无法复用。PegaInfer 的解法是把「调用一个内核」拆成三份职责：

1. **`kernels::ffi`**：只做声明。一个 `unsafe extern "C"` 块 + C ABI 类型（`Half = u16`），与 csrc 里的 CUDA/C++ 符号一一对应。它不校验任何东西——指针传错就是未定义行为。
2. **`kernels::ops`**：做安全封装。收 `&HiddenStates`、`&CudaSlice<bf16>` 这类带类型的句柄，校验形状与几何，取裸指针，算偏移，调 FFI，查返回值。**所有 `unsafe` 都应该关在这里。**
3. **`core::ops`**：做门面。core 是「模型侧运行时门面」（u1-l3），模型 crate 只依赖 core；门面把 core 私有的类型翻译成 kernels 类型后转发，并把整套 `kernels::ops` 符号 re-export 出去，让模型 crate 写 `ops::gemm_into` 而不是 `pegainfer_kernels::ops::gemm_into`。

#### 4.1.2 核心流程

一次从模型到硅片的调用：

```text
模型 crate（qwen3 prefill.rs）
  │  ops::prefill_attention_paged_into(&ctx, &mut q_batch, ..., layout: &KvLayout, plan, ...)
  ▼
core::ops::attention::prefill_attention_paged_into      ← 门面：layout.kernel_layout() 翻译，转发
  ▼
kernels::ops::attention::prefill_attention_paged_into   ← 包装：几何校验 + 取指针 + 组装参数
  ▼
ffi::qk_norm_rope_batched_decode_cuda(...)              ← unsafe extern "C"，void 返回
ffi::paged_kv_scatter_cuda(...) -> i32                  ← 返回 0/错误码/-1 哨兵
ffi::batch_prefill_paged_cuda_with_cta_tile_q(...) -> i32
  ▼
libkernels_cuda.a 里的 CUDA kernel（FlashInfer / 自研 csrc）
```

FFI 返回值有两类约定：

- **`void` 返回**（如 `qk_norm_rope_batched_decode_cuda`）：内核被当作不可失败，错误（若内核内部检查失败）会在之后的流同步处浮出。
- **`i32` 返回**：`0` 成功；`-1` 是 C++ 侧异常守卫的哨兵，真实错误文本存进线程局部槽，由 `ffi_exception_message` 取回拼接；其他非零是 CUDA 错误码。包装层每次调用后都要检查并 `anyhow::bail!`。

#### 4.1.3 源码精读

**FFI 层的组织**。[pegainfer-kernels/src/ffi.rs:1-32](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi.rs#L1-L32) 定义了跨子模块共享的 ABI 类型并按 feature 裁剪子模块——`Half` 就是 16 位浮点的位模式（`pub type Half = u16;`，与 CUDA half 同布局），共享内核在 `shared.rs`，模型专属内核在各自的 feature 门控文件里。

[pegainfer-kernels/src/ffi/shared.rs:7](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L7) 打开 `unsafe extern "C"` 块。paged attention 家族的声明集中在 [shared.rs:490-656](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L490-L656)，例如 scatter 内核：

```rust
// Scatter contiguous KV → paged layout (one layer, FlashInfer prefill append).
pub fn paged_kv_scatter_cuda(
    kv_data: *const Half, k_offset_elems: i64, v_offset_elems: i64,
    page_indices: *const i32, page_indptr: *const i32, last_page_len_d: *const i32,
    src_k: *const Half, src_v: *const Half,
    batch_indices: *const i32, positions: *const i32,
    nnz: i32, num_kv_heads: i32, head_dim: i32, page_size: i32,
    stride_page: i64, src_stride_n: i64, src_stride_h: i64,
    stream: CUstream,
) -> i32;
```

（引自 [pegainfer-kernels/src/ffi/shared.rs:549-568](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L549-L568)。）注意参数全是裸指针和整数——这一层没有任何 Rust 类型信息。

**包装层的组织**。[pegainfer-kernels/src/ops.rs:1-25](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops.rs#L1-L25) 按域拆模块：`attention`、`elementwise`、`embedding`、`linear`、`norm`、`sampling` 是跨模型共享面；`gemma4`、`glm52`、`k3`、`kimi_k2`、`deepseek_v2_lite` 等 feature 门控子面只在该模型编译时存在（呼应 [docs/subsystems/kernels/pegainfer-kernels-boundary.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/subsystems/kernels/pegainfer-kernels-boundary.md#L67-L73) 说的「共享第三方基底窄面 + 模型局部面」边界）。

包装层还有两个公用小工具：[ops.rs:201-207](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops.rs#L201-L207) 的 `checked_i32` / `checked_u32` 把 `usize` 安全收缩（放不下就报错，绝不截断）；[ops.rs:209-226](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops.rs#L209-L226) 的 `ffi_exception_message` 在返回值为 `-1` 哨兵时调 `ffi::pegainfer_kernels_last_error()`（声明在 [shared.rs:1417](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L1417)）取回 C++ 侧捕获的异常文本。

**门面层的组织**。[pegainfer-core/src/ops.rs:1-18](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops.rs#L1-L18) 声明 core 自己的三个子模块并导出 5 个 attention 包装；[ops.rs:19-109](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops.rs#L19-L109) 则把上百个 `kernels::ops` 符号原样 re-export。另有一组 `#[cfg(feature = "kernel-call-trace")]` 的条件导出（[ops.rs:110-129](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops.rs#L110-L129)）：开启该 feature 后，`gemm_into` 等高频算子换成 `traced` 模块里带调用记录的版本——同一批名字、两套实现，调用方代码零改动（u10-l3 剖析时展开）。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「一个符号在三层各出现一次」。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -rn "prefill_attention_paged_into" pegainfer-core/src pegainfer-kernels/src pegainfer-qwen3/src --include="*.rs"`；
   - 再执行 `grep -n "paged_kv_scatter_cuda" pegainfer-kernels/src/ffi/shared.rs pegainfer-kernels/src/ops/attention.rs`；
   - 把命中位置按「FFI 声明 → 包装 → 门面 → 调用方」排成一条链。
3. **需要观察的现象**：`prefill_attention_paged_into` 在 kernels 与 core 各有一个 `pub fn`（同名不同层），qwen3 里只有调用点没有定义；`paged_kv_scatter_cuda` 只出现在 ffi 声明与包装内部，模型 crate 完全看不到它。
4. **预期结果**：得到一条 4 站的符号链，且模型 crate 内 grep 不到任何 FFI 符号——这就是「unsafe 关在 kernels::ops」的直接证据。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `core::ops` 不直接让模型 crate `use pegainfer_kernels::ops::*`，而要坚持一层 re-export？
**答案**：依赖方向。模型 crate 只依赖 `pegainfer-core`（u1-l3 的分层铁律），core 作为门面隔离了 kernels 的路径变化；同时门面必须做类型翻译（如 `KvLayout` → `PagedKvLayout`，见 4.2），这是纯 re-export 做不到的。此外 `kernel-call-trace` feature 需要在不改调用方的前提下替换实现，也只有经过门面的条件导出才可能。

**练习 2**：FFI 函数返回 `i32`，其中 `-1` 是什么意思？
**答案**：`-1` 是 C++ 异常守卫的哨兵，表示 C++ 侧抛了异常并被捕获；真实错误消息存在线程局部槽里，用 `ffi::pegainfer_kernels_last_error()` 取回，包装层经 `ffi_exception_message(-1)` 拼进 `anyhow` 错误。`0` 是成功，其他非零通常是 CUDA 错误码。

### 4.2 `prefill_attention_paged_into`：一个包装的解剖

#### 4.2.1 概念说明

`prefill_attention_paged_into` 是预填充阶段每一层都要走一次的「注意力大算子」。它把四件事打包成一次调用：

1. **QK RMSNorm + RoPE**：对 Q、K 做归一化并按 token 的绝对位置旋转；
2. **KV 追加（scatter）**：把本步新算出的 K、V 写进 paged KV cache 的对应页槽；
3. **分页批量注意力**：FlashInfer BatchPrefillWithPagedKVCache，因果掩码；
4. 输出写进 `output: &mut HiddenStates`。

之所以打包，是因为 2、3 共享同一套页表元数据（`plan`），拆开就得把元数据暴露给模型层。注意 `into` 后缀是项目命名约定：结果写入调用者提供的输出缓冲，不新分配。

#### 4.2.2 核心流程

包装内部（[attention.rs:586-736](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L586-L736)）按顺序做：

1. `checked_paged_geometry` 校验布局自洽性并算出 `k_offset` / `v_offset`（该层 K、V 在每页内的元素偏移）与 `stride_page`；
2. 对每个张量调 `device_ptr(_mut)` 拿裸指针（cudarc 的守卫保证 borrow 期内不释放）；
3. `active_cu_stream(ctx)` 取当前流（u4-l1 的流覆盖机制，Green Context 换流就靠它）；
4. 依次发三个 FFI：
   - `qk_norm_rope_batched_decode_cuda`（void）——位置数组来自 plan 的 `positions_d`；
   - `paged_kv_scatter_cuda`（i32，检查返回值）；
   - `batch_prefill_paged_cuda_with_cta_tile_q`（i32，检查返回值）。

注意力缩放系数按公式 \( \text{sm\_scale} = 1 / \sqrt{d_h} \) 计算（[attention.rs:607](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L607)）。

#### 4.2.3 源码精读

**门面侧**（core）：[pegainfer-core/src/ops/attention.rs:12-50](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/attention.rs#L12-L50) —— 注意两处翻译：`layout: &KvLayout` 转成 `&layout.kernel_layout()`（kernels 只认 `PagedKvLayout`），`plan: &PrefillPagedPlan`（core newtype）靠 `Deref` 强制转换成 kernels 的同名类型：

```rust
pub fn prefill_attention_paged_into(
    ctx: &DeviceContext,
    q_batch: &mut HiddenStates, k_batch: &mut HiddenStates, v_batch: &HiddenStates,
    q_norm: &DeviceVec, k_norm: &DeviceVec, cos_cache: &DeviceVec, sin_cache: &DeviceVec,
    kv_buffer: &CudaSlice<bf16>,
    layout: &KvLayout,          // ← core 侧类型
    layer: usize,
    plan: &PrefillPagedPlan,    // ← core newtype
    output: &mut HiddenStates,
    num_q_heads: usize, num_kv_heads: usize, head_dim: usize, rms_eps: f32,
) -> Result<()> {
    pegainfer_kernels::ops::prefill_attention_paged_into(
        ctx, q_batch, k_batch, v_batch, q_norm, k_norm, cos_cache, sin_cache,
        kv_buffer,
        &layout.kernel_layout(), // ← 翻译点
        layer, plan, output,
        num_q_heads, num_kv_heads, head_dim, rms_eps,
    )
}
```

`KvLayout` 是 core 侧的纯值几何类型（[pegainfer-core/src/kv_pool.rs:14-30](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/kv_pool.rs#L14-L30)），`kernel_layout()` 逐字段复制成 kernels 的 `PagedKvLayout`（[kv_pool.rs:79-90](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/kv_pool.rs#L79-L90)）。`PagedKvLayout` 本体在 [pegainfer-kernels/src/paged_kv.rs:16-33](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/paged_kv.rs#L16-L33)：一页内先 K 后 V 排布，\( \text{layer\_stride} = 2 \times \text{kv\_block\_len} \)，\( \text{page\_stride} = \text{num\_layers} \times \text{layer\_stride} \)；`KvStorage` 枚举（[paged_kv.rs:1-14](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/paged_kv.rs#L1-L14)）标记 bf16 或 e4m3（fp8 KV，gemma4 用）。

**包装侧**（kernels）：签名见 [attention.rs:586-604](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L586-L604)，文档注释点明关键设计——token 位置一律取自 plan 的逐 token 数组，所以 `start_pos > 0` 的分块预填充和前缀缓存命中场景对任意 batch 都成立（[attention.rs:580-584](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L580-L584)；代码里还留有注释：这里曾有一个 batch_size==1 的标量 start_pos 快速路径，因为它会把前缀命中后缀从位置 0 旋转而删除，见 [attention.rs:648-652](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L648-L652)）。

几何校验 `checked_paged_geometry`（[attention.rs:2896-2984](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L2896-L2984)）逐项断言：布局字段非零、`kv_block_len == page_size * num_kv_heads * head_dim`、`layer_stride == 2 * kv_block_len`、`page_stride == num_layers * layer_stride`、`layer < num_layers`——因为布局字段是 public 的，任何自相矛盾的值都必须在进入 `unsafe` 前死掉。

三个 FFI 调用本体在 [attention.rs:653-732](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L653-L732)：RoPE（void）、scatter（查返回值并 `bail!`，附 `ffi_exception_message`）、batch prefill（同样查）。对应的 C ABI 声明分别是 [shared.rs:495-510](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L495-L510)、[shared.rs:549-568](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L549-L568)、[shared.rs:630-656](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L630-L656)。

**真实调用方**（模型层长什么样）：[pegainfer-qwen3/src/prefill.rs:282-316](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/prefill.rs#L282-L316) 的 `forward_layer_attn` 就是把层权重、KV 池、plan 和缓冲区原样递进去，一行完成「norm+RoPE+追加+注意力」。

#### 4.2.4 代码实践

1. **实践目标**：写出 `prefill_attention_paged_into` 的三层签名对照表（本讲核心实践）。
2. **操作步骤**：
   - 打开 [pegainfer-core/src/ops/attention.rs:12-50](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/attention.rs#L12-L50)、[pegainfer-kernels/src/ops/attention.rs:586-604](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L586-L604)、[pegainfer-kernels/src/ffi/shared.rs:630-656](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L630-L656)，逐参数抄签名；
   - 按下表填空（答案已给出，先自己填再核对）：

| 维度 | core 门面 | kernels 包装 | ffi（batch_prefill 主内核） |
| --- | --- | --- | --- |
| 上下文/流 | `ctx: &DeviceContext` | 同左 | `stream: CUstream`（裸句柄） |
| Q/K/V | `&mut HiddenStates` / `&HiddenStates` | 同左 | `*const Half` / `*mut Half` 裸指针 |
| KV 池 | `kv_buffer: &CudaSlice<bf16>` | 同左 | `*const Half` + `k/v_offset_elems: i64` |
| 布局 | `layout: &KvLayout` | `layout: &PagedKvLayout` | 拆散成 `page_size: i32`、`stride_page: i64` 等标量 |
| 计划 | `plan: &PrefillPagedPlan`（newtype） | `plan: &PrefillPagedPlan`（本体） | 拆成 7 个元数据裸指针（`page_indices`、`q_indptr`、`request_indices`…） |
| 形状参数 | `num_q_heads/num_kv_heads/head_dim: usize` | 同左 | 全部 `i32` |
| 缩放 | 无（内部算） | 内部 \( 1/\sqrt{d_h} \) | `sm_scale: f32` 显式传 |
| 错误 | `Result<()>` | `Result<()>` + `bail!` | `i32` 返回码 |

3. **需要观察的现象**：越往下走，类型信息越少——`usize` 变 `i32`、结构体变裸指针、`Result` 变返回码；`rms_eps` 只在最上两层出现（它在 RoPE 前的 norm 里用），`cta_tile_q` 只在最下两层出现（它是内核分块参数）。
4. **预期结果**：一张说明「每层丢掉了什么、又加了什么」的表。这就是三层各自职责的直观体现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `positions` 必须来自 plan 的逐 token 数组，而不能传一个标量 `start_pos`？
**答案**：分块预填充（chunked prefill）和前缀缓存命中时 `start_pos > 0` 且批内各请求起点不同；标量快路径曾把命中后缀从位置 0 旋转，产出错误 RoPE，因此被删除（[attention.rs:648-652](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L648-L652) 的注释记录了这段历史）。逐 token 数组是位置的唯一事实来源。

**练习 2**：`paged_attention_batch_decode_into` 与 prefill 版共用哪个 FFI？差异在哪？
**答案**：两者都先调 `ffi::paged_kv_scatter_cuda` 追加 KV（decode 版传显式 `request_indices_d` 与 `positions_d`，见 [attention.rs:1285-1316](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L1285-L1316)）；之后 prefill 走 `batch_prefill_paged_cuda_with_cta_tile_q`，decode 走 `paged_attention_decode_cuda`（[attention.rs:1318-1348](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L1318-L1348)）。core 门面对应 [pegainfer-core/src/ops/attention.rs:53-91](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/attention.rs#L53-L91)——decode 版元数据不是 plan 对象而是 7 个独立 `CudaSlice<i32>`（CSR 数组）。

**练习 3**：`into` 后缀什么意思？
**答案**：写入调用者提供的输出缓冲（`output: &mut HiddenStates`），函数不新分配显存——这是 CUDA Graph 指针稳定性的前提（u4-l5/u6-l4）。

### 4.3 `PrefillPagedPlan`：预填充计划与图稳定复用

#### 4.3.1 概念说明

`PrefillPagedPlan` 是「一次预填充调用的 GPU 元数据包」：页表（页索引 + CSR 偏移 + 末页长度）、每个 token 的位置、Q 的行偏移、tile 划分（`request_indices` / `qo_tile_indices` / `kv_tile_indices` / `kv_chunk_size`）和总行数。它**每次预填充构建一次、跨所有层共享**——36 层的模型每层都读同一份页表，这就是把它从 attention 调用里抽出来做成独立结构的原因。

它有三种构建方式，对应三种生命周期：

- **单请求** `new` / `new_with_cta_tile_q`：一条请求（如纯 prefill 阶段）；
- **多请求批量** `new_batch_with_cta_tile_q` / core 侧别名 `from_raw_batch_with_cta_tile_q`：混合批（unified step 里 prefill 行 + decode 行）；
- **预分配 + 原地刷新** `new_preallocated` + `update_batch_with_cta_tile_q`：为 CUDA Graph 准备——先按最坏情况分配，之后每步只 memcpy 覆盖内容，**指针永不动**。

#### 4.3.2 核心流程

构建单请求计划（[attention.rs:131-200](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L131-L200)）：

1. `checked_head_relation`：`num_q_heads` 必须是 `num_kv_heads` 的正整数倍（GQA 组大小为整数）；
2. 各维度过 `checked_i32` / `checked_u32` 收缩；
3. `clone_htod` 上传页索引、CSR 偏移、末页长度、位置数组（`(start_pos..kv_len)` 区间）；
4. 调两个**纯查询 FFI** 问 FlashInfer：`batch_prefill_paged_num_tiles_with_cta_tile_q` 算 tile 数，`batch_prefill_cta_tile_q_with_override` 算实际 CTA tile 尺寸（声明在 [shared.rs:578-600](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L578-L600)）——tile 数组必须与内核的调度划分精确一致，所以不自算、直接问内核侧；
5. 非 tile 数为正即报错（非法 override）。

预分配路径（[attention.rs:280-334](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L280-L334)）：`new_preallocated` 对全部 11 个设备缓冲 `alloc_zeros` 最坏尺寸，几何字段置零；`update_batch_with_cta_tile_q` 在主机上重算元数据后 `memcpy_htod` 原地覆盖——`memcpy` 只拷 `src.len()` 个元素、容忍更大的目标，所以最坏分配大于实际填充也没关系。

#### 4.3.3 源码精读

**kernels 本体**：结构定义 [attention.rs:26-47](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L26-L47)（11 个 `CudaSlice` + 7 个标量）；`new_preallocated` 的文档注释明确说出图稳定的动机（[attention.rs:280-283](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L280-L283)）："Buffer pointers stay fixed across updates so a CUDA Graph captured against them remains valid on replay."

**core 包装**：[pegainfer-core/src/ops/paged_plan.rs:71-95](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/paged_plan.rs#L71-L95) 的 `new` 是核心翻译点：从 `KvDesc`（core 的 KV 视图描述）抽 `page_indices()`（把页 ID 枚举转 `Vec<i32>`）和 `last_page_len()` 再调 kernels 构造器。批量入口 `from_raw_batch_with_cta_tile_q`（[paged_plan.rs:128-153](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/paged_plan.rs#L128-L153)）接收切片们直接转发。`Deref` 实现（[paged_plan.rs:240-246](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/paged_plan.rs#L240-L246)）让所有 getter（`page_indices_d()` 等，[paged_plan.rs:202-237](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/paged_plan.rs#L202-L237)）不必逐个重写。

**真实调用方**：[pegainfer-qwen3/src/unified_forward.rs:283](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/unified_forward.rs#L283) 每个混合步用 `from_raw_batch_with_cta_tile_q` 现建计划；[pegainfer-qwen3/src/verify_graph.rs:119](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/verify_graph.rs#L119) 的投机验证图则用 `new_preallocated`——两个调用点正好对应两种生命周期。

#### 4.3.4 代码实践

1. **实践目标**：理解「构建一次、跨层共享」与「预分配、指针不动」两种用法。
2. **操作步骤**：读 [pegainfer-qwen3/src/prefill.rs:277-316](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/prefill.rs#L277-L316)（`plan` 作为参数逐层传递）；再读 [pegainfer-qwen3/src/verify_graph.rs:119](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/verify_graph.rs#L119) 附近的 `new_preallocated` 用法。
3. **需要观察的现象**：prefill 主路径每步新建 plan（`clone_htod` 新分配）；verify 图路径只在捕获期分配一次，回放期靠 `update` 覆盖。
4. **预期结果**：能回答「如果预分配 plan 在 update 时换了指针会发生什么」——CUDA Graph 记录的是捕获期的老地址，回放读到的是已释放内存，输出静默错误。这正是 `update_batch_with_cta_tile_q` 文档强调 no allocation/no pointer change 的原因（[attention.rs:315-318](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L315-L318)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 tile 数要调 FFI 查询而不是在 Rust 里自算？
**答案**：tile 划分必须与 FlashInfer 内核的 CTA 调度划分逐 tile 对齐（`request_indices_d` 等数组按 tile 索引）；内核侧的分块规则（含 `cta_tile_q` override 逻辑）是实现细节，Rust 侧重算一旦偏离就会越界。两个查询函数（[shared.rs:578-600](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L578-L600)）让划分只有一个事实来源。

**练习 2**：core 的 `PrefillPagedPlan` 为什么用 `Deref` 而不是重新导出 kernels 类型？
**答案**：core 想给模型 crate 一个稳定名字并把 `KvDesc` 翻译挡在自己这层；`Deref` 让几十个 getter 免写。代价是 core 类型「是」kernels 类型这一耦合，但两层本来就是上下相邻的稳定契约，收益大于代价。

### 4.4 `SplitKvCsr` 与 `build_split_kv_csr`：split-KV 解码的 CSR 布局

#### 4.4.1 概念说明

非分区解码内核的网格是 `(batch, kv_heads)`。低 batch、长上下文时（比如 batch=1、上下文 32k）这个网格只有几个 CTA，GPU 数千个 SM 大量闲置。FlashInfer 的 partition-KV/split-K 方案把每个请求的 KV 纵向切成若干 chunk，每个 chunk 一个 CTA 算局部 attention，再用第二个 kernel 沿 `o_indptr` 归并——网格扩大为 `(chunk 槽, kv_heads)`。

描述这个切分的数据结构就是 `SplitKvCsr`：四个纯主机 `Vec`——`request_indices`（槽 → 请求号）、`kv_tile_indices`（槽 → chunk 号）、`block_valid_mask`（槽是否有效，padding 槽为 0）、`o_indptr`（每请求的槽区间偏移）。它由 `build_split_kv_csr` 纯函数构建，**不需要 GPU**。

#### 4.4.2 核心流程

`build_split_kv_csr(chunk_size, cap, kv_lens, padded_bs)`（[paged_plan.rs:20-69](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/paged_plan.rs#L20-L69)）：

1. 前置校验：`chunk_size > 0`、`cap > 0`、`kv_lens.len() <= padded_bs`；
2. 对每个请求算 chunk 数并压槽：

\[ \text{chunks}(r) = \max\!\left(1,\ \left\lceil \frac{kv\_len_r}{chunk\_size} \right\rceil\right) \]

   超过 `cap` 即报错（提示「context limit misconfigured」）；
3. 每个真实 chunk 压入 `(request_idx, chunk_idx, mask=1)`，并记 `o_indptr`；
4. padding 请求（`kv_lens.len()..padded_bs`）贡献零 chunk，只补 `o_indptr`；
5. 尾部补零槽（mask=0）直到 `padded_bs * cap`——**槽总数恒定**，这是 CUDA Graph 捕获的要求（buffer 尺寸不随批内容变化）。

#### 4.4.3 源码精读

结构定义与注释（[paged_plan.rs:9-16](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/paged_plan.rs#L9-L16)）：

```rust
/// Host-side CSR for the split-KV decode kernel: padded request/chunk indices,
/// the per-slot validity mask, and the per-request chunk offsets.
pub struct SplitKvCsr {
    pub request_indices: Vec<i32>,
    pub kv_tile_indices: Vec<i32>,
    pub block_valid_mask: Vec<u8>,
    pub o_indptr: Vec<i32>,
}
```

消费端（[pegainfer-qwen3/src/batch_decode_buffers.rs:410-452](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/batch_decode_buffers.rs#L410-L452)）：`sync_split_kv_meta` 收集各 `KvView` 的 `seq_len` 作为 `kv_lens`，调 `build_split_kv_csr` 后把四个数组逐个 `memcpy_htod` 进预分配的设备缓冲，并记下 `split_padded_slots = padded_bs * cap`。旁边的 `attention_path`（[batch_decode_buffers.rs:454-466](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/batch_decode_buffers.rs#L454-L466)）揭示路由策略：批小于等于阈值走 SplitKv、大于走 NonPartition；确定性策略（Pin/PerToken）则恒走 SplitKv——呼应 CLAUDE.md 里 `--batch-invariant` 的语义。

消费这些元数据的包装是 `paged_attention_batch_decode_split_kv_into`（kernels 侧 [attention.rs:1361-1386](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L1361-L1386)；core 门面 [pegainfer-core/src/ops/attention.rs:94-146](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/attention.rs#L94-L146)）。注意它的独有输入：`row_offset`（unified step 里 decode 行排在 prefill 行后面，Q/K/V/output 都只在 `[row_offset, row_offset+batch)` 行窗口上读写，四个字节偏移逐一过 `checked_row_offset` 校验，见 [attention.rs:1387-1410](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L1387-L1410)），以及两个 part 结果缓冲 `split_tmp_v: &mut CudaSlice<bf16>`、`split_tmp_s: &mut CudaSlice<f32>`（V 部分和与 logsumexp）。

#### 4.4.4 代码实践

1. **实践目标**：用单测吃透 CSR 布局（无 GPU 也可完成阅读部分）。
2. **操作步骤**：
   - 精读三个纯主机单测：[batch_decode_buffers.rs:525-549](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/batch_decode_buffers.rs#L525-L549)（`uniform_batch_csr`、`padding_requests_contribute_zero_chunks`、`errors_when_a_request_exceeds_the_cap`）；
   - 在有 CUDA 工具链的机器上运行（三个测试本身不需要 GPU）：

     ```bash
     cargo test --release -p pegainfer-qwen3 --lib uniform_batch_csr -- --nocapture
     cargo test --release -p pegainfer-qwen3 --lib padding_requests_contribute_zero_chunks -- --nocapture
     cargo test --release -p pegainfer-qwen3 --lib errors_when_a_request_exceeds_the_cap -- --nocapture
     ```

   - 手算一遍再核对：`build_split_kv_csr(64, 64, &[100, 100], 2)`——\( \lceil 100/64 \rceil = 2 \)，两请求各 2 chunk，`o_indptr = [0, 2, 4]`，前 4 槽 `(0,0),(0,1),(1,0),(1,1)` 全有效，总槽 `2*64=128`。
3. **需要观察的现象**：`padding_requests_contribute_zero_chunks` 里 `o_indptr = [0, 1, 3, 3]`——第三个槽位是 padding，起止偏移相等即「零 chunk」；`kv_lens=[1000], cap=4` 时 \( \lceil 1000/64 \rceil = 16 > 4 \)，函数直接报错。
4. **预期结果**：测试通过（待本地验证——本讲义写作环境未运行；无 GPU 环境只做手算与阅读）。注意同文件里的 `shared_prefix_views_are_counted_by_reference` 需要真实 GPU，上面的过滤器恰好把它排除。

#### 4.4.5 小练习与答案

**练习 1**：为什么 padding 槽必须补满到 `padded_bs * cap` 而不是按实际 chunk 数截断？
**答案**：两个原因。其一，split-KV 解码路径被捕获进 CUDA Graph（u6-l4），图回放要求参与内核的 buffer 尺寸与指针固定，槽总数恒定才能复用同一张图；其二，网格按 `padded_bs * cap` 起算，`block_valid_mask=0` 让 padding 槽的 CTA 空转，输出无效但不污染真实槽。

**练习 2**：`row_offset` 参数为什么只出现在 decode 系包装上？
**答案**：unified step 把 prefill 行和 decode 行拼在同一个 `HiddenStates` 里；decode 算子只该碰自己的行窗口，于是 Q/K/V/output 都要加行偏移。prefill 行从头开始，不需要偏移。签名差异本身就是「混合批」这一执行形态在算子层的投影。

**练习 3**：`o_indptr` 长度是多少？
**答案**：`padded_bs + 1`（[paged_plan.rs:37-55](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/paged_plan.rs#L37-L55)：开头压 0，每个请求（含 padding）各压一次）——标准 CSR 前缀和布局。

### 4.5 `KernelCall` / `TensorSpec`：与显存无关的调用元数据

#### 4.5.1 概念说明

前四节讲的是「真的执行」的调用。PegaInfer 还需要「描述」一次调用：基准日程（pegainfer-bench）、内核报告（`qwen3_kernel_report`）、调用 trace（`kernel-call-trace` feature）都不该为了记录形状而持有显存。`KernelCall` 就是擦除后的调用 IR——只存算子名、标签、输入/输出张量的形状元数据和字符串属性，可序列化（`Serialize`/`Deserialize`）、可比较（`Eq`/`Hash`）。

#### 4.5.2 核心流程

用 builder 风格三步拼一个调用描述：

```text
KernelCall::new(op, label)        ← 算子名 + 实例标签
  .input(name, TensorSpec)        ← 命名输入：dtype + layout + axes
  .output(name, TensorSpec)
  .attr(name, value)              ← 字符串属性（eps、row_offset…）
```

`TensorSpec` 由 dtype 标签（`Bf16`/`F32`/`I32`…）、layout 标签（`Contiguous1D`/`RowMajor2D`/`PagedKvPageFirst`…）和 `AxisSpec` 列表（命名轴 + 尺寸，如 `Hidden=2560, Batch=8`）组成。轴用 const 泛型标签（u4-l1 讲过的 `AxisTag` 家族：`Hidden`、`QDim`、`KvDim`、`Page`…）命名，擦除后变成字符串——编译期类型安全与运行期可序列化两头兼顾。

#### 4.5.3 源码精读

三个核心类型在 [pegainfer-kernels/src/tensor.rs:208-269](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L208-L269)（`AxisSpec`、`TensorSpec`，`compact()` 给出 `bf16[Hidden=2560, Batch=8] layout=row_major` 这样的可读形式）；[tensor.rs:307-346](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L307-L346) 是 `KernelCall` 与 builder——文档注释一句话点题："Erased logical kernel call IR shared by static schedules and future traces."

core 侧的预制构造器在 [pegainfer-core/src/ops/call_spec.rs:67-120](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/call_spec.rs#L67-L120)，例如：

```rust
pub fn rms_norm_batch_call<A: AxisTag>(label: impl Into<String>, dim: usize, batch: usize, eps: f32) -> KernelCall {
    KernelCall::new("rms_norm_batch", label)
        .input("x", hidden_batch::<A>(dim, batch))
        .input("weight", vector::<A, Bf16>(dim))
        .output("out", hidden_batch::<A>(dim, batch))
        .attr("eps", eps.to_string())
}
```

decode 注意力还有专门的 `PagedDecodePath` 枚举（`NonPartition` / `SplitKv { chunk_size, cap }`）和 `PagedDecodeCallSpec`（[call_spec.rs:36-65](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/call_spec.rs#L36-L65)）——4.4 讲的两条解码路径在这里变成可记录的元数据。

#### 4.5.4 代码实践

1. **实践目标**：会用 `KernelCall` 描述一个你已读过的算子。
2. **操作步骤**：对照 [call_spec.rs:79-104](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/call_spec.rs#L79-L104) 的两个例子，为 `paged_kv_scatter` 手写一份调用描述（示例代码，非项目原有）：

   ```rust
   // 示例代码：仿照 call_spec.rs 的 builder 风格描述 scatter 内核
   KernelCall::new("paged_kv_scatter", "qwen3/prefill/layer0")
       .input("kv_data", TensorSpec::new::<Bf16, PagedKvPageFirst>(vec![
           AxisSpec::new::<Page>(num_pages),
           AxisSpec::new::<Layer>(num_layers),
       ]))
       .input("src_k", hidden_batch::<KvDim>(kv_dim, total_tokens))
       .attr("page_size", page_size.to_string())
   ```

3. **需要观察的现象**：描述里没有任何 `CudaSlice` 或指针；同一份结构既能进基准日程也能进 trace。
4. **预期结果**：理解「执行调用」（前四节）与「描述调用」（本节）是两套平行 API，后者为 u10-l2（基准与内核报告）和 u10-l3（profiling）服务。

#### 4.5.5 小练习与答案

**练习 1**：`KernelCall` 为什么要派生 `Serialize`/`Deserialize`/`Eq`/`Hash`？
**答案**：它要能写进报告与快照（序列化）、被日程去重或做集合键（`Eq`/`Hash`）。它是数据，不是句柄。

**练习 2**：`PagedDecodePath::SplitKv` 为什么携带 `chunk_size` 和 `cap`？
**答案**：这两个值决定 CSR 的形状（槽总数 `padded_bs * cap`、chunk 划分），trace 或报告要重放/比对形状就必须记录它们；`NonPartition` 无切分参数，故是 unit 变体。

## 5. 综合实践

**任务：给一次 unified step 的注意力调用建立「全链路档案」。**

以 [pegainfer-qwen3/src/unified_forward.rs:283](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/unified_forward.rs#L283)（构建 plan）和 [unified_forward.rs:492](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/unified_forward.rs#L492)（调 prefill attention）为入口，完成三份交付物：

1. **签名对照表**：按 4.2.4 的表格式，补齐 decode 侧 `paged_attention_batch_decode_split_kv_into` 的三层对照（core [attention.rs:94-146](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/ops/attention.rs#L94-L146) → kernels [attention.rs:1361-1386](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/attention.rs#L1361-L1386) → FFI `paged_kv_scatter_cuda` + `paged_attention_decode_split_kv_cuda`）。重点标注 `row_offset`、四个 split 元数据数组和两个 tmp 缓冲在哪一层出现、哪一层消失。
2. **CSR 手算**：设 `chunk_size=128, cap=8, kv_lens=[300, 500, 0(padding)], padded_bs=3`，手写 `o_indptr`、前若干个 `(request_idx, chunk_idx, mask)` 槽和总槽数，再用 4.4.4 的测试命令风格验证（可写成一个临时 `#[test]`，或对照 `uniform_batch_csr` 的断言格式自查）。（手算参考：chunks = 3、4、0；`o_indptr = [0, 3, 7, 7]`；总槽 24。）
3. **元数据描述**：仿照 4.5.3，为这个 unified step 的 prefill attention 写一个 `KernelCall` 描述（算子名、两个以上命名输入、至少一个 attr），并注明它对应 4.2 里哪几个真实 FFI 调用。

完成后你应当拥有：一条从模型层到 C ABI 的完整调用链、一份能预测 GPU 元数据缓冲形状的 CSR 心算模型、一套描述调用的词汇——这正是阅读 u5-l3（Qwen3 前向）与 u6-l4（CUDA Graph 解码）所需的全部算子侧准备。

## 6. 本讲小结

- 共享 GPU 算子是三层结构：`kernels::ffi` 只声明 C ABI（裸指针 + `i32`/`i64` + `CUstream`），`kernels::ops` 做校验/取指针/查返回值的唯一 unsafe 边界，`core::ops` 做类型翻译（`KvLayout` → `PagedKvLayout`、`KvDesc` → 页索引数组）与符号门面。
- `prefill_attention_paged_into` 一次打包 QK norm+RoPE、paged KV 追加、FlashInfer 分页批量注意力三个 FFI；位置一律取 plan 的逐 token 数组，分块预填充与前缀命中因此天然正确。
- `PrefillPagedPlan` 是跨层共享的预填充元数据包，tile 划分由 FFI 查询内核侧决定；`new_preallocated` + `update` 提供指针不变的图稳定复用。
- `SplitKvCsr` / `build_split_kv_csr` 用 \( \lceil kv\_len/chunk\_size \rceil \) 切块并补齐到 `padded_bs * cap` 槽，服务低 batch 长上下文的 split-KV 解码；`row_offset` 让 decode 行安全嵌在 unified step 的混合缓冲里。
- `KernelCall` / `TensorSpec` / `CallSpec` 是与显存无关的擦除调用 IR，支撑基准、报告与 trace（u10 展开）。
- FFI 错误约定：`void` 内核视为不可失败；`i32` 内核 `0` 成功、`-1` 走 `pegainfer_kernels_last_error()` 取异常文本、其余为 CUDA 错误码。

## 7. 下一步学习建议

- **u4-l4（权重加载）**：继续单元 4，看 `weight_loader` 如何把 safetensors 灌进这些算子吃的 `GpuWeight`。
- **u4-l5（CUDA Graph 基础设施）**：本讲两处伏笔——`new_preallocated` 的指针稳定性、`SplitKvCsr` 的恒定槽数——都为图捕获服务，下一讲正式讲状态机。
- **u5-l3（Qwen3 前向计算）**：把本讲的算子按层串联成完整前向，验证你对「plan 构建一次、逐层消费」的理解。
- **u10-l2 / u10-l3**：回去看 `KernelCall` 如何变成基准日程与内核报告，`kernel-call-trace` feature 如何在门面层替换实现。
