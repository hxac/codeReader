# tensor.rs：动态 GPU 张量与 DeviceContext

> 讲义 id：u2-l1 ｜ 依赖：u1-l3（构建、依赖与测试）｜ 基准 HEAD：`223206b7`
>
> **证据边界说明**：`pegainfer-core/src/tensor.rs` 全文只有 1 行——它是一行转发门面，真正的 `Tensor`/`DeviceContext` 等类型定义在 `pegainfer-kernels` crate 的 `pegainfer_kernels::tensor` 模块里。本讲义的写作沙箱只能读取 `pegainfer-core` 目录，**无法读取 kernels 侧原文**，因此凡属 kernels 侧的定义细节（如 `Tensor` 的完整字段表、`Dtype` 变体清单、`Storage::Owned/Mapped`、`TensorError` 分类、`TENSOR_*` 常量）一律标注「待确认」，本讲以 core 内**可逐行验证的用法**为准来重建这些概念。所有永久链接均指向本仓库固定 commit。

## 1. 本讲目标

读完本讲，你应该能够：

1. 说出 `pegainfer-core/src/tensor.rs` 是一行门面（`pub use pegainfer_kernels::tensor::*;`），并解释「core 拥有 API 门面、kernels 拥有实现」这条分层边界。
2. 不看 kernels 源码，仅凭 core 内的 `use` 与调用点，枚举出 tensor 模块向模型 crate 暴露的主要类型（`DeviceContext`、`DeviceVec`、`DeviceMatrix`、`HiddenStates` 等）。
3. 解释 `DeviceContext` 的两个公开面：`ctx.ctx`（CUDA 上下文：pinned 分配、事件）与 `ctx.stream`（流上分配 `alloc_zeros`、D2H 拷贝 `clone_dtoh`），并追踪一条完整的 H2D→设备→D2H 往返链。
4. 解释设备侧所有权（`CudaSlice<T>` 的 RAII）与宿主侧 pinned 暂存（`alloc_pinned`）的区别，以及它们分别服务于 CUDA Graph 指针稳定和异步 DMA 上载的原因。
5. 解释 core 的算子「分发」其实是编译期 cfg 换出口（`kernel-call-trace` feature），并用 grep 亲自验证 `TENSOR_*` 常量不在 core 里。

## 2. 前置知识

- **host 与 device 内存**：CPU 侧叫 host 内存，GPU 显存叫 device 内存。数据从 host 到 device 的拷贝叫 H2D（host to device），反向叫 D2H。
- **stream（流）**：CUDA 的异步命令队列。分配、拷贝、kernel 启动都可以排进某条流，彼此按序执行。`DeviceContext` 持有一条默认流，`ctx.stream.alloc_zeros(n)` 就是在这条流上分配设备内存。
- **pinned（页锁定）内存**：普通 host 内存可以被操作系统换页，DMA 引擎无法安全地直接读它；pinned 内存被锁在物理页上，异步 DMA 拷贝（`memcpy_htod_async`）只对它有效。代价是分配昂贵，所以要复用（core 用两块 32 MiB 的 pinned 缓冲轮换）。
- **bf16（bfloat16）**：16 位浮点，1 位符号、8 位指数、7 位尾数——指数宽度与 f32 相同，动态范围大、精度低，是当前 LLM 权重与激活的主流格式。core 通过 `half` crate 使用 `bf16`（`Cargo.toml:14` 依赖，`kv_pool.rs:6` 引入）。
- **RAII**：Rust 的资源获取即初始化——`CudaSlice<T>` 被 drop 时显存自动归还，这正是 u1 讲过的页池 permit 同款纪律。
- **门面 / 再出口（re-export）**：u1-l1 已讲过——`pub use X::*;` 把别处定义的符号原样挂到本模块路径下，调用方只依赖门面路径，不依赖实现 crate。本讲的 `tensor.rs` 是全仓库最极端的例子：整个文件一行。
- **safetensors**：HuggingFace 的安全张量序列化格式。core 的 `Cargo.toml:26` 依赖它做权重读取（u3-l3 详讲）。注意它有自己的 `Dtype` 枚举（测试里出现的是 `safetensors::Dtype::BF16`），与 kernels tensor 模块的 dtype 体系是两回事。

## 3. 本讲源码地图

| 文件 | 行数 | 在本讲中的角色 |
| --- | --- | --- |
| `src/tensor.rs` | 1 | 一行门面：把 `pegainfer_kernels::tensor` 全部再出口 |
| `src/lib.rs` | 15 | crate 头注释与 13 个 `pub mod` 声明 |
| `Cargo.toml` | 39 | 依赖图证据：`cudarc`、`half`、`pegainfer-kernels`、`safetensors` |
| `src/kv_pool.rs` | 563 | tensor API 最大的设备侧消费者：`alloc_zeros`、`CudaSlice<bf16>`、stride 几何 |
| `src/rope.rs` | 232 | 最干净的 H2D 例子：`DeviceVec::from_host(ctx, &host)` |
| `src/weight_loader.rs` | 1169 | `StagedWeightLoader`：`take`/`take_vec` 返回 `DeviceMatrix`/`DeviceVec` |
| `src/weight_loader/staging.rs` | 517 | pinned 双缓冲与异步 H2D 上载、流一致性检查 |
| `src/ops.rs` | 115 | 算子门面：cfg 换出口的分发现场 |
| `src/ops/traced.rs` | 220 | trace 包装器：同签名转发 kernels 实现 |
| `src/ops/call_spec.rs` | 325 | kernels tensor 类型标签（`Bf16`/`F32`/`I32`/`Hidden`…）的集中引用点 |
| `src/cuda_graph.rs` | 234 | `active_cu_stream(ctx)` 与「图回放期间禁止分配」的纪律 |
| `tests/weight_loader_gpu.rs` | 77 | 全 core 唯一集成测试：BF16 round-trip 的现成范本 |

## 4. 核心概念与源码讲解

### 4.1 Tensor：一行门面与它的真身

#### 4.1.1 概念说明

`pegainfer-core` 的 tensor 模块不是「实现」，而是「署名」：core 对模型 crate 承诺一套张量 API，而把实现整体委托给 `pegainfer-kernels`。这样做的好处：

1. **单一事实源**：类型只定义一次（在 kernels），core 与所有模型 crate 看到的是同一份定义，不会出现两个 crate 各持一份 `Tensor`。
2. **编译边界清晰**：CUDA 相关的重型构建（nvcc、Triton AOT）全部收敛在 kernels 的 `build.rs`（见 CLAUDE.md「Build system」节），core 自身没有 `build.rs`（u1-l1 已确认）。
3. **模型 crate 只依赖 core**：模型 crate `use pegainfer_core::tensor::...` 即可，不必直接依赖 kernels（`src/ops/traced.rs:3-5` 是 core 内部直引 kernels 类型的少数例外，因为它要触碰类型标签）。

#### 4.1.2 核心流程

依赖链条（自上而下）：

```
模型 crate（qwen3 / qwen35 / kimi-k2 …）
    │  use pegainfer_core::tensor::*
    ▼
pegainfer-core/src/tensor.rs        ← 本讲主角，1 行
    │  pub use pegainfer_kernels::tensor::*
    ▼
pegainfer-kernels 的 tensor 模块     ← 类型真身（本沙箱不可读，细节待确认）
    │
    ▼
cudarc（Rust 的 CUDA Driver API 绑定：CudaSlice / CudaStream / CudaContext …）
```

core 内部各模块对 `crate::tensor::` 的引用是反向证据链：从「谁用了什么」可以反推出 tensor 模块的公开面。

#### 4.1.3 源码精读

**门面本体**——⟦src/tensor.rs:L1⟧(https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/tensor.rs#L1)：

```rust
pub use pegainfer_kernels::tensor::*;
```

整个文件就这一行：把 kernels tensor 模块的全部公开项以 `pegainfer_core::tensor::*` 的路径再出口。

**模块声明**——⟦src/lib.rs:L1-L15⟧(https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs#L1-L15)：头注释 `//! Shared runtime API used by pegainfer model crates.` 之后是 13 个 `pub mod`，其中 `pub mod tensor;`（⟦L13⟧(https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/lib.rs#L13)）把上面的门面挂进 crate 公开面。

**依赖图证据**——⟦Cargo.toml:L8-L29⟧(https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/Cargo.toml#L8-L29) 中与本讲相关的四行：`cudarc`（L11，CUDA 绑定）、`half`（L14，`bf16`）、`pegainfer-kernels`（L23，唯一路径依赖）、`safetensors`（L26，权重格式）。

**反向证据链**——core 内全部 `crate::tensor::` 引用（已用 grep 验证，共 19 处）：

| 出口 | 引用点（证据） |
| --- | --- |
| `DeviceContext` | `kv_pool.rs:13`、`rope.rs:6`、`weight_loader.rs:26`、`weight_loader/staging.rs:17`、`ops/attention.rs:7`、`ops/paged_plan.rs:7`、`ops/traced.rs:15`、`cuda_graph.rs:12`、`tests/weight_loader_gpu.rs:6` |
| `DeviceVec` | `rope.rs:7`、`weight_loader.rs:28`、`ops/attention.rs:8`、`ops/traced.rs:17` |
| `DeviceMatrix` | `weight_loader.rs:27`、`ops/traced.rs:16` |
| `HiddenStates` | `ops/attention.rs:9`、`ops/traced.rs:18` |
| `active_cu_stream`（函数） | `cuda_graph.rs:13` |
| `has_stream_override`（函数） | `weight_loader/staging.rs:324` |

一个值得注意的观察：**core 自身从未按名字引用 `Tensor` 类型**——core 的模块要么直接用 cudarc 的 `CudaSlice<T>`（如 kv_pool），要么用 `DeviceVec`/`DeviceMatrix`/`HiddenStates` 这些更专用的张量类型。完整的通用 `Tensor`（带 shape/strides/dtype/stream/tag 字段的那一个）是给模型 crate 前向计算用的，其字段表在 kernels 侧，**待确认**。

kernels 直引的类型标签还有一组（证据在 ⟦src/ops/call_spec.rs:L1-L20⟧(https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/ops/call_spec.rs#L1-L20)，import 清单在 L20 之后延续）：`AxisSpec`、`AxisTag`、`Batch`、`BatchPlusOne`、`Bf16`、`Contiguous1D`、`F32`、`HeadDim`、`Hidden`、`HiddenStatesLayout`、`I32`、`InDim`、`Inter2`、`Intermediate`、`KernelCall`、`Kv`、`KvDim`、`KvHead`、`Layer`…（见 4.5 节）。

#### 4.1.4 代码实践

1. **实践目标**：亲手重建上面的「tensor 出口清单」。
2. **操作步骤**：在 `pegainfer-core` 目录运行两条命令：
   ```bash
   grep -rn "crate::tensor::" src tests
   grep -rn "pegainfer_kernels::tensor::" src
   ```
3. **需要观察的现象**：第一条应得到 19 处引用、6 个不同出口；第二条应集中在 `src/ops/call_spec.rs`（类型标签）与 `src/ops/traced.rs:3-5`（`Hidden`/`InDim`/`OutDim`）。
4. **预期结果**：与 4.1.3 的表格逐行对上。若你想看渲染后的文档，可再跑 `cargo doc -p pegainfer-core --no-deps --open`（需要能完成 kernels 的 CUDA 构建；**待本地验证**）。
5. 两条 grep 是纯文本操作，可直接复跑。

#### 4.1.5 小练习与答案

1. **练习**：为什么 `kv_pool.rs` 写 `use crate::tensor::DeviceContext;` 而不是 `use pegainfer_kernels::tensor::DeviceContext;`？
   **答案**：走 core 自己的门面路径，模块间依赖只指向 crate 内部；这样即使未来 kernels 调整模块布局，只要门面还再出口同名符号，kv_pool 不用改。这也是「core API、kernels 实现」边界的纪律体现（对比 `traced.rs:3-5` 的直引是为了拿类型标签，属于有意为之的例外）。
2. **练习**：`pub use pegainfer_kernels::tensor::*;` 中的 `*` 会把什么带进来、什么带不进来？
   **答案**：带来 kernels tensor 模块的全部**公开**（`pub`）项——类型、函数、常量、trait；带不进来私有项和仅对该 crate 可见的项。`*` 再出口不会循环，也不会引入未使用的警告。
3. **练习**：core 里 `src/ffi.rs` 也是 1 行（u1-l1 已确认）。这两个一行文件说明了什么工程决策？
   **答案**：core 刻意不持有任何 CUDA FFI 声明与张量定义，把「和 CUDA/cudarc 的一切纠缠」下沉到 kernels 一个 crate，core 保持纯 Rust API 层，模型 crate 只面对 core。好处是构建配置（nvcc、Triton、FlashInfer）只出现在 kernels 的 `build.rs`。

### 4.2 DeviceContext：分配与拷贝的唯一入口

#### 4.2.1 概念说明

`DeviceContext` 是「拿到一块 GPU」的抽象：它持有 CUDA 上下文与默认流。从 core 的用法看，它有两个公开面（字段直接可访问）：

- `ctx.stream`——一条 `CudaStream`（cudarc 类型，内部 `Arc`，可 `clone`）。core 用它做三件事：流上分配设备内存（`alloc_zeros`）、D2H 拷贝（`clone_dtoh`）、以及被 `active_cu_stream(ctx)` 取出裸 `CUstream` 交给图启动。
- `ctx.ctx`——CUDA 上下文（cudarc 的 `CudaContext`）。core 用它分配 pinned host 内存（`alloc_pinned::<bf16>(len)`）和创建事件（`new_event`）。

另外 tensor 模块还提供一个线程局部流覆盖机制（`active_cu_stream` / `has_stream_override`），供模型 crate 在多流场景切换当前流；core 自己只是**检查**它（staging 拒绝在流覆盖期间做分块上载）。

#### 4.2.2 核心流程

一次典型「权重上载 + 设备分配 + 读回」的全景：

```
DeviceContext::new()
  ├─ ctx.ctx.alloc_pinned::<bf16>(N)     ← 宿主侧 pinned 暂存（staging.rs:148）
  ├─ ctx.ctx.new_event(BLOCKING_SYNC)    ← DMA 完成事件（staging.rs:150-153）
  ├─ memcpy_htod_async(pinned → device)  ← 分块异步 H2D（staging.rs:10 引入）
  ├─ ctx.stream.alloc_zeros(slots)       ← 流上分配设备内存（kv_pool.rs:221-224）
  ├─ DeviceVec::from_host(ctx, &host)    ← 一步 H2D（rope.rs:52-53）
  └─ ctx.stream.clone_dtoh(&slice)       ← D2H 读回（tests/weight_loader_gpu.rs:73）
```

流一致性规则（`ensure_uploadable`）：目的缓冲的分配流必须与 stager 的流 `Arc::ptr_eq` 相同，且不允许存在线程局部流覆盖——否则分块上载的流序无法保证。

#### 4.2.3 源码精读

**构造**——⟦tests/weight_loader_gpu.rs:L41-L42⟧(https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/tests/weight_loader_gpu.rs#L41-L42)：`let ctx = DeviceContext::new().unwrap();`，core 的测试与内嵌单测都这样起手（另见 `kv_pool.rs:453` 的 `DeviceContext::new().expect("GPU required for kv_pool tests")`）。

**流上分配（设备内存）**——⟦src/kv_pool.rs:L221-L224⟧(https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/kv_pool.rs#L221-L224)：KvPool 在构造时一次性分配整块 KV 缓冲，类型显式标注为 `CudaSlice<bf16>`，错误被包装成带上下文的 `anyhow` 消息 `"KvPool alloc failed: ｛e｝"`。

**一步 H2D**——⟦src/rope.rs:L45-L55⟧(https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/rope.rs#L45-L55)：`precompute_rope(ctx, spec)` 在宿主算好 cos/sin 表后，用 `DeviceVec::from_host(ctx, &cos_host)` 逐块上载，返回 `(DeviceVec, DeviceVec)`。这是 core 里最简洁的「host 向量 → 设备向量」范本。

**pinned 分配与事件（宿主侧）**——⟦src/weight_loader/staging.rs:L143-L162⟧(https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-core/src/weight_loader/staging.rs#L143-L162)：`WeightStager::new(ctx)` 造两块 pinned 缓冲（`bufs` 数组两元素）；`make()` 内部 `ctx.ctx.alloc_pinned::<bf16>(STAGE_BYTES / BF16_SIZE)`（L148）并 `ctx.ctx.new_event(Some(CUevent_flags::CU_EVENT_BLOCKING_SYNC))`（L150-153）；同时 `stream: ctx.stream.clone()`（L157）——再次证明 `stream` 字段可克隆（`Arc` 语义）。
