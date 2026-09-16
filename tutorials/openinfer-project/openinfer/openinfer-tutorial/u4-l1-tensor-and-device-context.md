# u4-l1 张量与设备层：DeviceContext 与 bf16 张量体系

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `DeviceContext` 封装了哪些 CUDA 资源（context、stream、cuBLAS 句柄初始化），以及它为什么坚持「单计算流」设计、这个设计和 CUDA Graph 捕获有什么关系。
2. 掌握运行时张量三件套 `DeviceVec` / `DeviceMatrix` / `HiddenStates` 的 bf16 内存布局，尤其是 `HiddenStates`「预分配到最大、每步改写 `seq_len`」的语义。
3. 掌握 const 泛型类型层 `GpuTensor<DIM>` / `GpuWeight<OUT, IN>` / `NormWeight<DIM>`，理解「把形状检查从运行时搬到编译期」的思路。
4. 理解 `StreamOverrideGuard` 与 `active_cu_stream` 的线程局部流覆盖机制：为什么它能在不改动任何算子签名的前提下，把一段代码的全部 GPU 工作切到另一条流上（Green Context SM 分区的基础）。

本讲是单元 4（共享运行时）的第一讲。你在 u1-l3 已经知道 `pegainfer-kernels` 拥有 CUDA FFI 和 build.rs；本讲进入它的第一个源码模块 `tensor.rs`——所有模型 crate 每天都在用的地基。

## 2. 前置知识

### 2.1 CUDA context 与 stream

- **CUDA context（上下文）**：可以理解为「一块 GPU 上的运行时工作间」。要让 GPU 干活，先得为这块卡建立 context，它管理显存分配器、模块加载、句柄等所有运行时状态。
- **CUDA stream（流）**：GPU 上的任务队列。你向一条流里按顺序提交 kernel 和拷贝，GPU 按提交顺序执行；**不同流之间没有顺序保证**，因此跨流协作需要 event 同步——而同步调用会带来额外开销，并且在 CUDA Graph 捕获期间是危险操作。
- `cudarc` 是本仓库依赖的第三方 Rust crate（见 [pegainfer-kernels/Cargo.toml:9](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/Cargo.toml#L9)），封装了 CUDA Driver API；`CudaContext`、`CudaStream`、`CudaSlice<T>` 都来自它。`CudaSlice<T>` 是设备显存的所有权句柄，drop 时释放显存。

### 2.2 bf16 是什么

bf16（bfloat16）用 16 位表示一个浮点数：1 位符号 + 8 位指数 + 7 位尾数。对比 f32（1 + 8 + 23）：

\[ \text{bf16: } \underbrace{s}_{1}\ \underbrace{e}_{8}\ \underbrace{m}_{7} \qquad \text{f32: } \underbrace{s}_{1}\ \underbrace{e}_{8}\ \underbrace{m}_{23} \]

指数位宽度相同，所以 bf16 的动态范围与 f32 一致（约 \(3.4\times10^{38}\)），只是精度低得多（约 2~3 位十进制有效数字）。深度学习权值和激活对精度不敏感、对范围敏感，bf16 因此成为 LLM 推理的主流格式，显存占用是 f32 的一半。bf16 → f32 的转换是**无损**的（尾数补零即可），这一点后面 `to_host` 会用到。

### 2.3 Rust 语法要点

- **const 泛型**：`GpuTensor<2560>` 把维度写进类型参数，编译器就能替你检查形状。
- **`thread_local!` + `Cell`**：线程局部可变状态，无需锁。
- **`Arc<T>`**：原子引用计数的共享所有权，克隆廉价——`DeviceContext` 因此可以到处按引用传。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [pegainfer-kernels/src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs) | 本讲主角：DeviceContext、流覆盖、运行时张量、const 泛型张量、KernelCall 元数据 |
| [pegainfer-core/src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/tensor.rs) | 只有一行：整体 re-export kernels 的 tensor 模块（模型 crate 经 core 使用这些类型） |
| [pegainfer-kernels/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/lib.rs) | kernels 的模块表；第 2 行开启了 nightly 的 `generic_const_exprs` 特性 |
| [pegainfer-kernels/src/ffi/shared.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs) | extern 声明：`cuda_set_device`、`cublas_init` 等 C 符号 |
| [pegainfer-kernels/src/ops/norm.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/norm.rs) | 消费者示例：一个真实算子包装如何拿设备指针、如何用 `active_cu_stream` |
| [pegainfer-kernels/src/typed_ops.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/typed_ops.rs) | 消费者示例：基于 `GpuTensor`/`GpuWeight` 的编译期形状安全算子 |
| [pegainfer-qwen3/src/executor.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/executor.rs) | 流覆盖的真实调用方：SplitConcurrent 步骤把 prefill/decode 各切到独立流 |
| [pegainfer-core/src/cuda_graph.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/cuda_graph.rs) | CUDA Graph 状态机；文档注释解释了「图绑定捕获流」与流覆盖的关系 |

## 4. 核心概念与源码讲解

### 4.1 DeviceContext：CUDA 上下文与单流设计

#### 4.1.1 概念说明

`DeviceContext` 是整个引擎与一块 GPU 打交道的**唯一锚点**：它持有 CUDA context、一条计算流和设备序号，并在创建时初始化 cuBLAS。模型 crate 的执行器、权重加载器、CUDA Graph 状态机都拿它的引用（`&DeviceContext`）干活。

它最特别的设计决策是：**全程只用一条计算流**。这不是偷懒，而是一次明确的三方交易：

1. 单流内天然保序，不需要任何跨流同步；
2. 跨流同步（`stream.wait(event)`）会在 CUDA Graph 捕获时制造麻烦；
3. 于是干脆在 context 层面关掉 cudarc 的事件跟踪，从根上消除这类调用。

「真的需要两条流怎么办？」——答案不是改 context，而是 4.2 节的流覆盖机制。

#### 4.1.2 核心流程

`DeviceContext::new_with_device(ordinal)` 的构造流程：

```text
1. ffi::cuda_set_device(ordinal)     # 把本线程的当前设备设为 ordinal（C API，返回错误码）
2. CudaContext::new(ordinal)         # cudarc 建立/获取该卡 context
3. ctx.disable_event_tracking()      # 关闭多流事件跟踪（为 CUDA Graph 捕获铺路）
4. ctx.new_stream()                  # 创建那条唯一的计算流
5. ffi::cublas_init()                # 初始化 cuBLAS 句柄
→ 返回 { ctx: Arc<CudaContext>, stream: Arc<CudaStream>, device_ordinal }
```

同步有两条路径：

- `DeviceContext::sync()`：直接 `stream.synchronize()`，阻塞到流排空。
- `stream_spin_wait(ctx)`：先对活动流自旋轮询 `cuStreamQuery`，超过 5ms 上限才退化为阻塞 `cuStreamSynchronize`。短内核场景下自旋等待比直接睡眠等待更快拿到结果。

#### 4.1.3 源码精读

结构体本体——三个字段，`Arc` 让克隆近乎免费：

```rust
#[derive(Clone)]
pub struct DeviceContext {
    pub ctx: Arc<CudaContext>,
    pub stream: Arc<CudaStream>,
    pub device_ordinal: usize,
}
```

[pegainfer-kernels/src/tensor.rs:348-354](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L348-L354) —— DeviceContext 持有 Arc 包装的 context 与 stream；克隆只加引用计数，所以调用方可以随手 `ctx.clone()` 存走。

构造函数的关键段落：

```rust
// Disable multi-stream event tracking before creating streams.
// We use a single compute stream, so no cross-stream synchronization is needed.
// This avoids stream.wait(event) calls that break CUDA Graph capture.
unsafe {
    ctx.disable_event_tracking();
}
let stream = ctx.new_stream()...;
unsafe {
    ffi::cublas_init();
}
```

[pegainfer-kernels/src/tensor.rs:375-390](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L375-L390) —— 注释写明了动机：单计算流 ⇒ 无跨流同步 ⇒ 没有 `stream.wait(event)` ⇒ 不破坏 CUDA Graph 捕获。`cublas_init` 对应的 extern 声明在 [pegainfer-kernels/src/ffi/shared.rs:448-451](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ffi/shared.rs#L448-L451)，与 `cuda_set_device` 并列。

完整的五步构造在 [pegainfer-kernels/src/tensor.rs:361-397](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L361-L397)：先 `ffi::cuda_set_device`（失败即报 `cudaError=`），再建 context、关事件跟踪、开流、初始化 cuBLAS。`new()` 是 `new_with_device(0)` 的别名（[pegainfer-kernels/src/tensor.rs:356-359](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L356-L359)），多卡时由执行器传入 rank 对应的 ordinal。

自旋等待的实现：

```rust
const STREAM_SPIN_WAIT_CAP: std::time::Duration = std::time::Duration::from_millis(5);

pub fn stream_spin_wait(ctx: &DeviceContext) -> anyhow::Result<()> {
    let stream = active_cu_stream(ctx);
    let cap = std::time::Instant::now() + STREAM_SPIN_WAIT_CAP;
    loop {
        match unsafe { cuStreamQuery(stream) } {
            CUDA_SUCCESS => return Ok(()),
            CUDA_ERROR_NOT_READY => {
                if now >= cap { /* 阻塞 cuStreamSynchronize 并检查结果 */ }
                std::hint::spin_loop();
            }
            err => return Err(...),
        }
    }
}
```

[pegainfer-kernels/src/tensor.rs:82-105](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L82-L105) —— 轮询上限 5ms；注意它等待的是 `active_cu_stream(ctx)`（当前生效的流，可能是被覆盖的那条），而不是裸的 `ctx.stream`。普通同步 `sync()` 在 [pegainfer-kernels/src/tensor.rs:399-404](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L399-L404)。

#### 4.1.4 代码实践

**实践目标**：亲手创建 `DeviceContext`，观察单流身份——同一个 context 里所有工作都落在同一条流地址上。

**操作步骤**（示例代码，需要一个可用 CUDA 环境；本讲义写作环境无 GPU，**待本地验证**）：

```rust
// 示例代码：可作为 pegainfer-kernels 的一个测试加入 tensor.rs 的 tests 模块
#[test]
fn device_context_reports_single_stream() {
    let ctx = DeviceContext::new().expect("需要一块 GPU");
    println!("device_ordinal = {}", ctx.device_ordinal);
    println!("stream ptr     = {:p}", ctx.stream.cu_stream());

    let a = DeviceVec::zeros(&ctx, 4).expect("alloc");
    let b = DeviceVec::zeros(&ctx, 4).expect("alloc");
    // 两次分配、两次 H2D 都走 ctx.stream —— 打印的地址应始终一致
    let _ = a.to_host(&ctx);
    let _ = b.to_host(&ctx);
    println!("stream ptr after work = {:p}", ctx.stream.cu_stream());
}
```

**需要观察的现象**：两次打印的 stream 指针值完全相同；`device_ordinal` 为你实际的卡号。

**预期结果**：单流设计意味着不存在「换了一条流」的可能——这正是 4.2 节要用「覆盖」而不是「换 context」的原因。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `disable_event_tracking()` 必须在 `ctx.new_stream()` **之前**调用？

**答案**：源码注释（tensor.rs:375-376）明确写了 "Disable multi-stream event tracking **before** creating streams"。cudarc 的事件跟踪会影响其创建/管理流时的同步行为；先关跟踪再开流，才能保证后续所有流操作不会暗插 `stream.wait(event)` 调用，CUDA Graph 捕获才安全。顺序反了，跟踪可能已经对已创建的流生效。

**练习 2**：`stream_spin_wait` 为什么要设 5ms 的自旋上限，而不是一直自旋或直接阻塞同步？

**答案**：一直自旋会浪费 CPU（如果内核实际要跑几百毫秒）；直接阻塞同步又为短内核付出唤醒延迟。折中方案：先自旋最多 5ms（覆盖「马上就好」的常见情形），超时才退化为 `cuStreamSynchronize` 阻塞等待（tensor.rs:87-99）。

### 4.2 流覆盖机制：StreamOverrideGuard 与 active_cu_stream

#### 4.2.1 概念说明

Green Context SM 分区（详见 u9-l2）需要把 prefill 工作发到一条 SM 受限的流上、decode 留在原流，两条流并发。问题：全仓库几百个算子包装的签名都是 `fn op(ctx: &DeviceContext, ...)`，逐个加 stream 参数是一场大手术。

本仓库的解法优雅得多：**算子们根本不直接用 `ctx.stream`，而是调用 `active_cu_stream(ctx)`**——它先查线程局部覆盖槽，有值用覆盖值，没值才回落到 `ctx.stream`。于是：

- 「把一段代码的全部 GPU 工作切到流 X」= 在作用域开头放一个 `StreamOverrideGuard::activate(X)`；
- guard 被 drop 时自动恢复之前的值（支持嵌套，像栈一样）；
- 算子签名零改动。

线程局部（而非全局）意味着这个切换只影响当前线程——正好与「每个 rank 一个专用线程」的执行模型（u6-l3）匹配。

#### 4.2.2 核心流程

```text
线程局部状态:  STREAM_OVERRIDE: Cell<Option<CUstream>>
               PREFILL_STREAM_OVERRIDE: Cell<bool>

激活:  guard = unsafe { StreamOverrideGuard::activate(s) }
        ├─ STREAM_OVERRIDE ← Some(s)        （记住旧值）
        └─ 返回持有旧值的 guard
生效:  active_cu_stream(ctx) = STREAM_OVERRIDE.get().unwrap_or(ctx.stream)
恢复:  guard drop 时把旧值写回（含 panic 展开路径，Drop 语义保证）
```

对 guard 的安全性约定（源码 `# Safety` 注释）：**覆盖的流必须在 guard 生命周期内保持有效，且与 `DeviceContext` 同属一个设备**——guard 只存裸指针，不持有所有权。

#### 4.2.3 源码精读

线程局部状态的定义：

```rust
thread_local! {
    static STREAM_OVERRIDE: Cell<Option<CUstream>> = const { Cell::new(None) };
    static PREFILL_STREAM_OVERRIDE: Cell<bool> = const { Cell::new(false) };
}
```

[pegainfer-kernels/src/tensor.rs:18-23](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L18-L23) —— 两个线程局部槽：主覆盖槽存目标流指针；`PREFILL_STREAM_OVERRIDE` 是配套的布尔标记，标记「当前覆盖由 split-concurrent prefill 持有」，供 `has_prefill_stream_override()`（[tensor.rs:68-71](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L68-L71)）查询。

guard 的激活与恢复：

```rust
pub struct StreamOverrideGuard {
    previous: Option<CUstream>,
    previous_prefill: bool,
}

impl StreamOverrideGuard {
    pub unsafe fn activate(stream: CUstream) -> Self { Self::activate_inner(stream, false) }
    pub unsafe fn activate_prefill(stream: CUstream) -> Self { Self::activate_inner(stream, true) }
    fn activate_inner(stream: CUstream, prefill: bool) -> Self {
        let previous = STREAM_OVERRIDE.with(|c| c.replace(Some(stream)));
        let previous_prefill = PREFILL_STREAM_OVERRIDE.with(|c| c.replace(prefill));
        Self { previous, previous_prefill }
    }
}

impl Drop for StreamOverrideGuard {
    fn drop(&mut self) {
        STREAM_OVERRIDE.with(|c| c.set(self.previous));
        PREFILL_STREAM_OVERRIDE.with(|c| c.set(self.previous_prefill));
    }
}
```

[pegainfer-kernels/src/tensor.rs:25-61](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L25-L61) —— `replace` 返回旧值并存入 guard；`Drop` 把旧值写回。因为是「保存旧值、恢复旧值」而不是「清空」，多个 guard 可以正确嵌套。

核心查询函数：

```rust
#[inline]
pub fn active_cu_stream(ctx: &DeviceContext) -> CUstream {
    STREAM_OVERRIDE
        .with(Cell::get)
        .unwrap_or_else(|| ctx.stream.cu_stream())
}
```

[pegainfer-kernels/src/tensor.rs:73-80](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L73-L80) —— 三行代码撑起整个机制：覆盖优先，否则回落 `ctx.stream`。算子侧的标准用法见 [pegainfer-kernels/src/ops/norm.rs:14-36](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/ops/norm.rs#L14-L36)：`rms_norm_into` 取三个设备指针后，把 `crate::tensor::active_cu_stream(ctx)` 作为最后一个参数传给 FFI 内核——它自己不知道也不需要知道现在在哪条流上。

真实调用方（qwen3 执行器的 SplitConcurrent 步骤）：

```rust
use pegainfer_kernels::tensor::StreamOverrideGuard;
...
{
    let _prefill_override = unsafe { StreamOverrideGuard::activate(prefill_stream.0) };
    let (logits, _, _) = lane.execute_prefill(...)?;
    prefill_logits = logits;
}   // ← guard 在此 drop，覆盖结束
...
{
    let _decode_override = unsafe { StreamOverrideGuard::activate(decode_stream.0) };
    lane.execute_decode(...)?;
}
```

[pegainfer-qwen3/src/executor.rs:424-450](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/executor.rs#L424-L450) —— 两个花括号作用域各自持有一个 guard：`execute_prefill` 内部所有算子（全部经 `active_cu_stream`）落在 prefill 流，出了作用域恢复；随后 decode 同理切到 decode 流。一段代码、两次整体切换，算子层零感知。

两个值得注意的边界：

- **图绑定语义**：[pegainfer-core/src/cuda_graph.rs:30-41](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/cuda_graph.rs#L30-L41) 的文档注释指出：CUDA Graph 捕获与回放都发生在 `active_cu_stream` 上，而流捕获会把每个节点绑定到捕获流的执行上下文——在 Green Context decode 流上捕获的图，无论从哪条流启动都只在那个 SM 分区上回放。因此 `CudaGraphState` 与流一一绑定，多流 decode 必须每流一份状态（u4-l5 展开）。
- **明确拒绝覆盖的例外**：[pegainfer-kernels/src/tensor.rs:107-145](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L107-L145) 的 `memcpy_dtod_u32_from_i32`（argmax i32 → 嵌入 u32 的设备间拷贝）开头就 `ensure!(!has_stream_override(), "dtod i32->u32 copy runs on the base stream only")`——这是一个安全护栏样例：该拷贝被 CUDA Graph 捕获依赖基准流地址，覆盖期间执行会破坏这一假设，所以宁可报错也不静默走错流。

#### 4.2.4 代码实践

**实践目标**：验证 guard 的嵌套与恢复语义（纯逻辑，不发起任何 CUDA 调用）。

**操作步骤**：仓库自带测试 `stream_override_guard_restores_on_drop`（[pegainfer-kernels/src/tensor.rs:751-768](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L751-L768)），它用 `0x10`、`0x20` 这样的**假指针**测试状态机，全程不触碰 GPU。运行：

```bash
cargo test --release -p pegainfer-kernels --lib stream_override_guard -- --nocapture
```

注意：测试逻辑本身不需要 GPU，但**编译** pegainfer-kernels 需要 CUDA 工具链（build.rs 用 nvcc 编译 csrc，SM 探测失败会 panic，回顾 u1-l2；无 nvidia-smi 时可设 `PEGAINFER_CUDA_SM` 覆盖）。本讲义写作环境未运行此命令，**待本地验证**。

**需要观察的现象**：测试通过；对照源码看三层断言——外层 guard 激活后 `has_prefill_stream_override()` 为真、内层 guard（非 prefill 变体）激活期间该标记暂时为假、内层 drop 后主槽值回到 `Some(outer)` 而不是 `None`。

**预期结果**：恢复的是「进入前的旧值」，嵌套正确展开。这正是 qwen3 executor 里两个 guard 能安全连用的前提。

#### 4.2.5 小练习与答案

**练习 1**：如果 `activate` 用「写 None」实现 Drop（而不是写回 `previous`），哪个场景会出错？

**答案**：嵌套场景。qwen3 executor（executor.rs:424-450）若在 prefill guard 存活期间又进入一个 guard，内层 drop 时清成 `None` 会让外层剩余代码回落到 `ctx.stream`，prefill 后半段的算子悄悄跑到错误的流上，且难以复现。写回 `previous` 使任意嵌套都正确。

**练习 2**：为什么 `active_cu_stream` 是 `pub fn` 而不是把流藏在 guard 内部自动注入？

**答案**：因为发起 kernel 的是上百个 FFI 包装函数（如 norm.rs:32），每个都要在调用点显式取「当前生效的流」传给 C 函数。集中成一个查询函数，算子侧统一一行 `active_cu_stream(ctx)`，覆盖机制对算子完全透明；藏起来反而没法把流参数传给 extern C 函数。

### 4.3 bf16 运行时张量：DeviceVec / DeviceMatrix / HiddenStates

#### 4.3.1 概念说明

这一层是「真正占显存的张量」，全部以 bf16 为元素类型（`CudaSlice<bf16>`，`half` crate 提供 `bf16`）：

- `DeviceVec`：一维向量（RMSNorm 权重、单 token 激活等）；
- `DeviceMatrix`：行主序二维矩阵（`[rows, cols]`），权重加载的中转形态；
- `HiddenStates`：批量激活——一层的输出/下一层的输入，Transformer 前向的通货。

`HiddenStates` 的布局是本节重点：`hidden_dim * seq_len` 个连续 bf16，第 \(i\) 个 token 的起始偏移为

\[ \text{offset}(i) = i \times d \quad (d = \text{hidden\_dim}) \]

human 视角是「row-major 的 `[seq_len, d]`」；cuBLAS 视角是「column-major 的 `[d, seq_len]`」——同一段内存的两种读法，这样 GEMM 无需转置就能直接喂给列主序的 cuBLAS。

#### 4.3.2 核心流程

以权重加载到前向激活的生命周期为例：

```text
safetensors 字节流（磁盘, 小端 bf16）
   │  from_safetensors: 字节长度校验后按 bf16 重解释（零拷贝视图→H2D）
   ▼
DeviceMatrix [rows, cols]            ← 权重中转
   │  GpuWeight::from_device_matrix（4.4 节）
   ▼
前向计算: HiddenStates [d × seq_len]  ← 每层流动的激活
   │  to_host: D2H + sync，bf16→f32（无损）
   ▼
主机侧 Vec<f32>（测试断言、logits 检查）
```

`HiddenStates` 还有一个服务级语义：**背衬分配按最大值做，`seq_len` 每步改写**。解码缓冲区开机时按最大 batch 一次分配；某个实际批次只有 3 个请求时，只是把结构体的 `seq_len` 字段改成 3，显存不重分。这是 CUDA Graph 指针稳定性的前提之一（u4-l5 / u6-l4 承接）。

#### 4.3.3 源码精读

`DeviceVec` 的四个入口：

```rust
pub struct DeviceVec {
    pub data: CudaSlice<bf16>,
    pub len: usize,
}
```

[pegainfer-kernels/src/tensor.rs:407-411](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L407-L411) —— 数据 + 逻辑长度。`from_host`（[tensor.rs:413-424](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L413-L424)）走 `ctx.stream.clone_htod` 异步 H2D；`zeros`（[tensor.rs:442-452](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L442-L452)）分配并清零；`to_host`（[tensor.rs:454-462](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L454-L462)）D2H 后**显式 `ctx.sync()`** 再转换——因为拷贝是异步的，不同步就读到旧数据。

safetensors 字节直读：

```rust
pub fn from_safetensors(ctx: &DeviceContext, data: &[u8]) -> Result<Self> {
    if !data.len().is_multiple_of(2) { return Err(...); }
    let len = data.len() / 2;
    // NOTE: This assumes a little-endian host. Safetensors are little-endian.
    let slice = unsafe { std::slice::from_raw_parts(data.as_ptr().cast::<bf16>(), len) };
    Self::from_host(ctx, slice)
}
```

[pegainfer-kernels/src/tensor.rs:427-440](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L427-L440) —— safetensors 本就是小端 bf16 字节流，主机是小端时直接按 `bf16` 重解释切片，免去逐元素转换。`DeviceMatrix::from_safetensors`（[tensor.rs:525-546](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L525-L546)）同理，只是先校验 `rows * cols * 2` 字节。矩阵版本还有 `vstack`（[tensor.rs:481-509](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L481-L509)）：按行拼接多个同列数矩阵，D2D 逐段拷贝——chunked prefill 拼批时会用到这类操作。

`HiddenStates` 与它的关键校验：

```rust
pub struct HiddenStates {
    pub data: CudaSlice<bf16>,
    pub hidden_dim: usize,
    pub seq_len: usize,
}
```

[pegainfer-kernels/src/tensor.rs:548-555](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L548-L555) —— 布局注释写明「token i at offset i * hidden_dim；cuBLAS interprets as [hidden_dim, seq_len] column-major」。

```rust
pub(crate) fn checked_extent(&self, what: &str) -> Result<usize> {
    let extent = self.hidden_dim.checked_mul(self.seq_len)...;
    if self.data.len() < extent {
        return Err(anyhow!("{what} backing len {} < hidden_dim {} * seq_len {}", ...));
    }
    Ok(extent)
}
```

[pegainfer-kernels/src/tensor.rs:566-585](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L566-L585) —— 注意比较是 `>=` 而不是 `==`，源码注释解释了原因：缓冲区按最大值分配、每步改写 `seq_len` 到活跃尺寸；又因为字段是 `pub`，安全调用方也可能把逻辑尺寸改到超出背衬——launch 包装在触达内核前用这个函数拒绝越界。这是「预分配 + 改写逻辑长度」模式的自卫机制。

往返无损性：

```rust
/// Copy to host as f32. bf16 → f32 is lossless, so f32 equality is bitwise.
pub fn to_host(&self, ctx: &DeviceContext) -> Result<Vec<f32>> { ... }
```

[pegainfer-kernels/src/tensor.rs:619-627](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L619-L627) —— 因为 bf16 → f32 无损，测试里对 f32 结果做 `==` 断言等价于对 bf16 位级断言。HF golden 门禁（u10-l1）正是建立在这个性质上。

最后是借用桥梁：

```rust
pub struct HiddenStatesRef<'a> {
    pub data: &'a CudaSlice<bf16>,
    pub hidden_dim: usize,
    pub seq_len: usize,
}
```

[pegainfer-kernels/src/tensor.rs:739-745](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L739-L745) —— 非所有权的 `&HiddenStates` 形态（由 [tensor.rs:557-564](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L557-L564) 的 `as_ref()` 构造），让未类型化算子能借用持久解码 arena 里的数据而不夺走所有权。

#### 4.3.4 代码实践

**实践目标**：验证 bf16 H2D→D2H 往返的位级无损性，并亲手触发一次 `checked_extent` 的拒绝路径。

**操作步骤**（示例代码，需 GPU，**待本地验证**）：

```rust
// 示例代码：可作为测试加入 tensor.rs 的 tests 模块
#[test]
fn bf16_roundtrip_is_bitexact() {
    let ctx = DeviceContext::new().expect("需要一块 GPU");
    let host: Vec<bf16> = vec![bf16::from_f32(0.0), bf16::from_f32(1.0),
                               bf16::from_f32(-0.5), bf16::from_f32(1024.5)];
    let d = DeviceVec::from_host(&ctx, &host).expect("H2D");
    let back = d.to_host(&ctx).expect("D2H");
    for (i, (a, b)) in host.iter().zip(back.iter()).enumerate() {
        assert_eq!(a.to_f32(), b, "index {i} 不无损");
    }
    // checked_extent 是 pub(crate)，此处从外部观察不到——改为读它的语义：
    // 构造 zeros(d=4, seq=2) 后手动把 seq_len 改成 3（4*3=12 > 背衬 8），
    // 想象 launch 包装会得到什么错误。
    let mut hs = HiddenStates::zeros(&ctx, 4, 2).unwrap();
    hs.seq_len = 3;
    println!("logical extent = {} 但 backing = {}", 4 * 3, hs.data.len());
}
```

**需要观察的现象**：往返值逐一相等（包括 `-0.5` 这类精确值）；第二段打印显示逻辑 extent（12）大于背衬长度（8）。

**预期结果**：真实 launch 包装调用 `checked_extent` 时会以 `backing len 8 < hidden_dim 4 * seq_len 3` 报错——字段公开带来的越界风险正是由这道检查兜底。仓库既有测试 `test_device_matrix_from_safetensors_matches_from_host`（[tensor.rs:779-816](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L779-L816)）做了类似的位级对照，可以对照阅读。

#### 4.3.5 小练习与答案

**练习 1**：`to_host` 里如果不调用 `ctx.sync()` 会怎样？

**答案**：`clone_dtoh` 是异步提交的，返回的 `Vec` 可能在拷贝完成前就被读（tensor.rs:620-626 先 `clone_dtoh` 再 `ctx.sync()?` 然后才 `map` 收集——顺序保证了读到的是完成后的数据）。去掉 sync 是典型的数据竞争：断言时好时坏。

**练习 2**：为什么 row-major `[seq_len, d]` 和 column-major `[d, seq_len]` 是同一段内存？

**答案**：两种读法都把元素 \((i,j)\)（第 \(i\) 个 token 的第 \(j\) 维）放在偏移 \(i \times d + j\)。行主序按「行=token」理解，列主序按「列=token」理解；cuBLAS 是列主序 API，这一布局让 GEMM 免转置直读（tensor.rs:549-551 的注释原文即此意）。

### 4.4 const 泛型类型层：GpuTensor / GpuWeight / NormWeight

#### 4.4.1 概念说明

4.3 节的类型把形状存在**运行时字段**里（`rows`、`cols`、`seq_len`），形状错误只能在运行时 `assert`/`ensure` 报错。`tensor.rs` 的后半部分（源码注释称 "Typed tensor layer"）把形状搬进**类型**：

- `GpuTensor<const DIM: usize>` —— 激活：隐藏维编译期已知，批次 `seq_len` 留运行时（每步都在变）；
- `GpuWeight<const OUT: usize, const IN: usize>` —— 权重矩阵 `[OUT, IN]`，两维都编译期已知；
- `NormWeight<const DIM: usize>` —— RMSNorm 权重向量；
- `GpuRawSlice<const ELEMS: usize>` / `GpuRawSliceI32<const ELEMS: usize>` —— f32 / i32 原始缓冲，每批项的元素数编译期已知。

收益：`gemm(w, x, y)` 里 `w: &GpuWeight<OUT, IN>`、`x: &GpuTensor<IN>`、`y: &mut GpuTensor<OUT>`——输入输出维度对不上**直接编译不过**，运行时只剩 `seq_len` 一项需要检查。代价：需要 nightly 的 `generic_const_exprs` 特性（[pegainfer-kernels/src/lib.rs:1-2](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/lib.rs#L1-L2)）。

源码注释还声明了迁移策略：这一层是**增量**的，`HiddenStates`/`DeviceMatrix` 原样保留，模型 crate 一次迁一个（[tensor.rs:630-637](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L630-L637)）。

同文件还有一层**类型擦除的元数据**（`TensorSpec` / `TensorArg` / `KernelCall`，[tensor.rs:208-346](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L208-L346)）：用 `DTypeTag`/`LayoutTag`/`AxisTag` 标记类型词表（`named_tag!` 宏批量生成，如 `Bf16`、`RowMajor2D`、`Hidden`），拼出「某内核吃了哪些形状的张量」的字符串化描述，供基准日程与内核报告（u10-l2）使用。它描述内核调用，不承载显存——与上面四件套是两个世界。

#### 4.4.2 核心流程

一次类型化 GEMM 的形状检查流程：

```text
调用 gemm_into::<OUT, IN>(ctx, w: &GpuWeight<OUT,IN>, x: &GpuTensor<IN>, y: &mut GpuTensor<OUT>)
  ├─ w 与 x 的 IN 匹配？          ← 编译期（类型系统强制）
  ├─ y 的 OUT 匹配？              ← 编译期
  └─ y.seq_len == x.seq_len？     ← 运行时 ensure!（typed_ops.rs:27-32）
       └─ 取设备指针 → launch_gemm(W[M,N] x X[N,bs] → Y[M,bs])
```

`GpuWeight::from_device_matrix` 是类型层的入口门卫：从运行时形状的 `DeviceMatrix` 构造编译期形状的权重，形状不合即报错——这是「运行时世界」到「编译期世界」的一次性闸口（4.3 的加载管线在此处交接）。

#### 4.4.3 源码精读

两个核心类型：

```rust
pub struct GpuTensor<const DIM: usize> {
    pub data: CudaSlice<bf16>,
    pub seq_len: usize,
}

pub struct GpuWeight<const OUT: usize, const IN: usize> {
    pub(crate) data: CudaSlice<bf16>,
}
```

[pegainfer-kernels/src/tensor.rs:639-646](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L639-L646) 与 [tensor.rs:671-674](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L671-L674) —— `GpuTensor` 的内存布局与 `HiddenStates` 相同（`[DIM * seq_len]`，token i 在 `i * DIM`，cuBLAS 视作 `[DIM, seq_len]` 列主序）；`GpuWeight` 的数据字段是 `pub(crate)`——外部只能经门卫构造，不能绕过形状检查改内部。

构造门卫：

```rust
impl<const OUT: usize, const IN: usize> GpuWeight<OUT, IN> {
    pub fn from_device_matrix(m: DeviceMatrix) -> Result<Self> {
        anyhow::ensure!(
            m.rows == OUT && m.cols == IN,
            "GpuWeight<{}, {}>::from_device_matrix shape mismatch: got [{}, {}]", ...);
        Ok(Self { data: m.data })
    }
}
```

[pegainfer-kernels/src/tensor.rs:676-688](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L676-L688) —— 运行时→编译期世界的唯一通道，形状不符在此报错。同款门卫：`GpuTensor::from_device_matrix_rows`（[tensor.rs:657-668](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L657-L668)，校验 cols==DIM）、`NormWeight::from_device_vec`（[tensor.rs:695-705](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L695-L705)，校验 len==DIM）。`GpuRawSlice` / `GpuRawSliceI32` 见 [tensor.rs:707-737](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L707-L737)。

消费者：类型化 GEMM：

```rust
/// `Y = W @ X` — compile-time shape: `X:[IN,bs]`, `Y:[OUT,bs]`.
pub fn gemm_into<const OUT: usize, const IN: usize>(
    ctx: &DeviceContext,
    w: &GpuWeight<OUT, IN>,
    x: &GpuTensor<IN>,
    y: &mut GpuTensor<OUT>,
) -> Result<()> {
    anyhow::ensure!(y.seq_len == x.seq_len, "typed GEMM seq_len mismatch: ...", ...);
    let (w_ptr, _gw) = w.data.device_ptr(&ctx.stream);
    let (x_ptr, _gx) = x.data.device_ptr(&ctx.stream);
    let (y_ptr, _gy) = y.data.device_ptr_mut(&ctx.stream);
    launch_gemm(w_ptr as *const ffi::Half, x_ptr as *const ffi::Half,
                y_ptr as *mut ffi::Half, OUT, x.seq_len, IN, x.seq_len == 1, ctx)
}
```

[pegainfer-kernels/src/typed_ops.rs:20-46](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/typed_ops.rs#L20-L46) —— 签名本身就是形状证明：`w` 的 IN 与 `x` 的 DIM 由类型统一，`y` 的 OUT 由类型统一；函数体只剩 seq_len 一项运行时检查。紧随其后的 `gemm_graphsafe_into`（[typed_ops.rs:48-74](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/typed_ops.rs#L48-L74)）是图捕获专用变体，恒走免 workspace 的 cuBLAS 路径。模块头注释（[typed_ops.rs:1-4](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/typed_ops.rs#L1-L4)）概括了这一层的设计意图。

最后，别忘了模型 crate 看到的入口：[pegainfer-core/src/tensor.rs:1](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/tensor.rs#L1) 只有一行 `pub use pegainfer_kernels::tensor::*;`——本讲的全部类型经 `pegainfer-core` 这个门面 re-export 给上层（u1-l3 的分层铁律：kernels 拥有实现，core 做模型侧门面）。

#### 4.4.4 代码实践

**实践目标**：体会「形状错误变成编译错误」。

**操作步骤**（示例代码，**待本地验证**——只需编译，不需要 GPU 跑起来）：

```rust
// 示例代码：放在一个会参与编译的测试/示例里
use pegainfer_kernels::tensor::{DeviceContext, DeviceMatrix, GpuTensor, GpuWeight};

#[test]
fn shape_mismatch_is_a_compile_error() {
    let ctx = DeviceContext::new().unwrap();
    // GpuWeight<64, 32>: OUT=64, IN=32
    let w: GpuWeight<64, 32> = GpuWeight::from_device_matrix(
        DeviceMatrix::from_host(&ctx, &vec![half::bf16::from_f32(0.0); 64 * 32], 64, 32).unwrap(),
    ).unwrap();
    // 故意造一个 GpuTensor<16>（IN 不匹配 w 的 32）
    let mut x = GpuTensor::<16>::zeros(&ctx, 4).unwrap();
    let mut y = GpuTensor::<64>::zeros(&ctx, 4).unwrap();
    pegainfer_kernels::typed_ops::gemm_into(&ctx, &w, &x, &mut y).unwrap();
    //                                            ^^^ 预期编译错误：
    // expected `&GpuTensor<32>`, found `&GpuTensor<16>`
}
```

**需要观察的现象**：`cargo check` 在 `gemm_into` 调用行报类型不匹配；把 `GpuTensor::<16>` 改成 `GpuTensor::<32>` 后编译通过。

**预期结果**：同样的错误若发生在 4.3 节的运行时类型上，只能等跑到那一步才 `ensure!` 报错；类型层把它提前到了 `cargo check`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `GpuTensor<DIM>` 只把 DIM 放进类型、`seq_len` 留在运行时？

**答案**：`seq_len` 每个调度步都在变（本步 prefill 512 token、下步 decode 3 token）。若它也是 const 泛型，每个批次尺寸都要单态化出一份代码，且无法用「预分配最大 + 每步改写 seq_len」的缓冲策略（4.3 节），CUDA Graph 的批间复用（u6-l4）也无从谈起。而 DIM（模型的 hidden_dim）在权重装载后永不变，适合编译期固定。

**练习 2**：`KernelCall` 与 `GpuTensor` 都叫「张量相关类型」，本质区别是什么？

**答案**：`GpuTensor` 持有 `CudaSlice<bf16>`，是真实占显存的运行数据；`KernelCall`（[tensor.rs:307-346](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L307-L346)）只持有字符串化的 `TensorSpec` 描述（dtype/layout/axes 的名字和大小），是内核调用的**擦除 IR**，用于静态日程与报告（u10-l2 的 KernelCall/CallSpec 基准日程），可序列化（`Serialize`/`Deserialize`），不接触 GPU。

## 5. 综合实践

把本讲四个模块串成一个测试函数：**创建设备上下文 → 分配 bf16 张量 → 拷贝与同步 → 打印底层流地址 → 验证流覆盖切换**。

以下为完整示例代码（可加入 `pegainfer-kernels/src/tensor.rs` 的 `tests` 模块尾部，或放进独立的集成测试文件；标注**待本地验证**——写作环境无 GPU，代码按真实 API 书写但未运行）：

```rust
// 示例代码：综合实践——DeviceContext 全流程 + 流覆盖验证
use cudarc::driver::sys::CUstream;
use half::bf16;

#[test]
fn u4l1_device_context_and_stream_override_tour() {
    // ── 模块 1：DeviceContext ──────────────────────────────────────────
    let ctx = DeviceContext::new().expect("需要一块 GPU");
    println!("ordinal={} stream={:p}", ctx.device_ordinal, ctx.stream.cu_stream());
    let base_stream = ctx.stream.cu_stream();

    // ── 模块 3：bf16 张量：分配 → 拷贝 → 同步 ────────────────────────
    let host: Vec<bf16> = (0..8).map(|i| bf16::from_f32(i as f32 * 0.25)).collect();
    let d = DeviceVec::from_host(&ctx, &host).expect("H2D");
    let mut z = DeviceVec::zeros(&ctx, host.len()).expect("zeros");
    let host_back = d.to_host(&ctx).expect("D2H 含 sync");
    assert_eq!(host_back, (0..8).map(|i| i as f32 * 0.25).collect::<Vec<_>>());
    let _ = &mut z; // z 在真实场景里作为预分配输出缓冲

    // ── 模块 2：流覆盖：激活 → 生效 → 恢复 ────────────────────────────
    assert!(!has_stream_override(), "基线：无覆盖");
    assert_eq!(active_cu_stream(&ctx), base_stream, "基线：活动流 = ctx.stream");
    {
        // 假指针仅用于观察切换，不发起任何 CUDA 调用（与仓库自带测试同款做法）
        let fake = 0x1234_0000usize as CUstream;
        let _guard = unsafe { StreamOverrideGuard::activate(fake) };
        assert!(has_stream_override());
        assert_eq!(active_cu_stream(&ctx), fake, "覆盖期间：活动流 = 覆盖值");
        {
            let fake2 = 0x5678_0000usize as CUstream;
            let _inner = unsafe { StreamOverrideGuard::activate(fake2) };
            assert_eq!(active_cu_stream(&ctx), fake2, "嵌套：内层生效");
        }
        assert_eq!(active_cu_stream(&ctx), fake, "内层 drop：恢复到外层");
    }
    assert!(!has_stream_override(), "全部 drop：回到基线");
    assert_eq!(active_cu_stream(&ctx), base_stream);

    // ── 模块 4：类型层门卫（形状错误在此报运行时错误）────────────────
    let m = DeviceMatrix::from_host(&ctx, &host, 4, 2).expect("4x2");
    let w: Result<GpuWeight<4, 2>, _> = GpuWeight::from_device_matrix(m);
    assert!(w.is_ok(), "64/32 门卫：形状吻合应通过");
    ctx.sync().expect("final sync");
}
```

**观察清单**：

1. `stream={:p}` 打印的地址在整段测试中不变（单流设计）；
2. bf16 往返 8 个值全部位级相等；
3. 覆盖断言全部按注释顺序通过——特别是「内层 drop 恢复到外层」而不是 `None`；
4. `GpuWeight::<4, 2>` 与 4×2 矩阵吻合，`from_device_matrix` 返回 Ok。

**延伸**（对照真实调用方）：测试通过后，打开 [pegainfer-qwen3/src/executor.rs:424-450](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/executor.rs#L424-L450)，把你这个测试里的 fake 指针换成真实 green ctx 流的等价物，就是执行器 SplitConcurrent 步骤每天在做的事。

## 6. 本讲小结

- `DeviceContext`（[tensor.rs:348-405](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L348-L405)）以 `Arc` 持有 CUDA context + 单条计算流 + 设备号，构造时关事件跟踪、初始化 cuBLAS；**单流设计消除了跨流同步，从根上保护 CUDA Graph 捕获**。
- 流覆盖机制（[tensor.rs:18-80](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/tensor.rs#L18-L80)）用线程局部 `Cell<Option<CUstream>>` + RAII guard（保存旧值、Drop 恢复、可嵌套），让一段作用域内的全部算子经 `active_cu_stream` 整体切流，算子签名零改动——Green Context SM 分区（u9-l2）的地基。
- 运行时张量 `DeviceVec`/`DeviceMatrix`/`HiddenStates` 全 bf16；`HiddenStates` 是「预分配最大 + 每步改写 `seq_len`」的布局（token i 偏移 \(i \times d\)，cuBLAS 列主序直读），`checked_extent` 以 `>=` 校验背衬；bf16→f32 无损使主机侧可做位级断言。
- const 泛型层 `GpuTensor<DIM>`/`GpuWeight<OUT,IN>`/`NormWeight<DIM>` 经 `from_device_matrix` 等门卫从运行时世界一次性进入，形状错误由运行时 `ensure!` 升级为编译期类型错误（需 nightly `generic_const_exprs`）；`KernelCall`/`TensorSpec` 则是与显存无关的擦除描述，服务基准与报告。
- 这些类型经 [pegainfer-core/src/tensor.rs:1](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/tensor.rs#L1) re-export 给全部模型 crate——kernels 实现、core 门面的分层铁律在类型层同样成立。

## 7. 下一步学习建议

本讲打通了「GPU 资源与张量」这层地基，接下来按依赖顺序：

1. **u4-l2（kernels 构建系统）**：本讲的 `ffi::cuda_set_device`、`cublas_init` 这些 C 符号从哪来？build.rs 如何用 nvcc 把 `csrc/` 下的 `.cu` 文件（当前 69 个）编进静态库并自动探测 SM 目标。
2. **u4-l3（core::ops 算子门面）**：本讲只看了一个算子（`rms_norm_into`）；下一讲系统看 ffi → kernels::ops → core::ops 三层包装与注意力算子族。
3. **u4-l5（CUDA Graph 基础设施）**：本讲埋了两处伏笔——`disable_event_tracking` 与「图绑定捕获流」——都在 `CudaGraphState` 里兑现；「预分配 + 改写 seq_len」也在那里成为指针稳定性的关键。
4. 带着问题读：为什么 qwen3 执行器要两条流各配一个 guard，而不是一个大 guard？（提示：两条流的工序有先后依赖，中间需要 event 同步——见 [pegainfer-qwen3/src/executor.rs:452-459](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/executor.rs#L452-L459) 创建 event 的那一行。）
