# u2-l1 DeviceContext 与 GpuTensor：cudarc 之上的运行时

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `DeviceContext` 持有哪些 CUDA 资源，以及它初始化时按什么顺序做了哪五件事、每件事为什么必要。
2. 区分 `DeviceVec`、`DeviceMatrix`、`HiddenStates` 三种非类型化张量，掌握 `HiddenStates` 的内存布局与「cuBLAS 视角下的列主序」约定。
3. 理解 `StreamOverrideGuard` 的 RAII 语义：thread-local 覆盖如何让几百个只接收 `&DeviceContext` 的算子包装透明地切换到另一条流（Green Context SM 分区、并发 prefill 的底层前提）。
4. 读懂 `active_cu_stream` 的解析顺序（thread-local 覆盖优先，否则回退上下文默认流）与 `stream_spin_wait` 的「自旋 5ms + 阻塞兜底」策略。

本讲是 u2 单元（Rust 侧运行时基础）的第一讲：后面 u2-l2 的常量泛型类型层、u2-l3 的 FFI 错误契约、u2-l4 的前向 DSL，全部建立在 `tensor.rs` 提供的上下文与张量抽象之上。

## 2. 前置知识

### 2.1 CUDA context 与 stream

- **CUDA context（上下文）**：可以理解为「一块 GPU 上的工作环境」。要先创建 context，才能在这块 GPU 上分配显存、编译模块、启动 kernel。
- **CUDA stream（流）**：GPU 上的任务队列。同一个流内的操作按顺序执行，不同流之间可以并行。PegaInfer 的关键设计是**默认只用一条计算流**（后面会看到代码注释解释为什么）。
- **Green Context**：CUDA 的一种能力，可以把一块 GPU 的 SM（流式多处理器，GPU 的计算单元）划出一个子集，让某条流上的任务只能用这个子集。PegaInfer 用它做流量隔离——例如 gemma4 的 `PEGAINFER_ASYNC_PREFILL=green:NN` 让 prefill 挤在一个小 SM 分区上跑，不去抢 decode 的算力（详见 `docs/models/qwen3/green-ctx-sm-partition.md`）。

### 2.2 cudarc

`cudarc` 是社区维护的 Rust CUDA Driver API 绑定 crate。本文件里的 `CudaContext`、`CudaStream`、`CudaSlice<T>` 都来自它。`CudaSlice<T>` 是 cudarc 的设备内存句柄：泛型参数 `T` 标注元素类型，内部持有显存指针并在 Drop 时释放。`CUstream` 则是 CUDA 驱动的裸流句柄类型（本质是指针），FFI 边界上传的就是它。

### 2.3 bf16

`bf16`（bfloat16）是 1 位符号 + 8 位指数 + 7 位尾数的 16 位浮点格式：指数位宽与 f32 相同，所以数值范围一样，只是精度低。LLM 推理几乎全程用 bf16 存激活与权重。bf16 转 f32 是无损的（尾数补零），源码注释里专门强调这一点，因为它允许测试用 f32 做逐位相等的断言。

### 2.4 thread_local 与 RAII guard

- `thread_local!` 声明每个线程各有一份的变量。本文件用它存「当前线程的流覆盖」。
- **RAII guard**：一个结构体在构造时改变全局状态，在 `Drop`（离开作用域）时自动恢复。Rust 的互斥锁守卫就是这种模式。`StreamOverrideGuard` 把它用在 thread-local 状态上。

### 2.5 行主序与列主序

同样一段连续内存 `[a, b, c, d, e, f]`，按行主序读是 2×3 矩阵的第一行 `a b c`、第二行 `d e f`；按列主序读则是第一列 `a d`、第二列 `b e`。cuBLAS（NVIDIA 的矩阵库，Fortran 血统）原生按列主序解释内存。这个区别是本讲 `HiddenStates` 布局的关键。

## 3. 本讲源码地图

| 文件 | 行数（约） | 作用 |
|------|------|------|
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/lib.rs#L1-L12) | 12 | crate 根：声明 7 个常驻模块 + 1 个 feature 门控模块，开启 nightly 的 `generic_const_exprs` |
| [src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L1-L864) | 864 | 本讲主角：流覆盖机制、`stream_spin_wait`、`DeviceContext`、非类型化张量、类型化张量、kernel 调用 IR |
| [src/ffi/shared.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L465-L468) | — | `DeviceContext` 初始化时调用的 `cublas_init` / `cuda_set_device` 的 extern 声明 |
| [tests/common/mod.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/common/mod.rs#L11-L34) | — | 测试公共设施 `device_or_skip`：如何区分「无 GPU」与「坏环境」 |

`tensor.rs` 的消费者（帮助理解设计动机，本讲只做交叉引用）：

- [pegainfer-core/src/cuda_graph.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L89)：CUDA Graph 捕获/回放都取 `active_cu_stream(ctx)`。
- [pegainfer-qwen35/src/unified_forward.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen35/src/unified_forward.rs#L77)：并发 prefill 用 `activate_prefill` 占用流覆盖。
- [pegainfer-gemma4/src/engine.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/engine.rs#L2412)：green 通道切换用 `StreamOverrideGuard::activate`。
- [pegainfer-sample/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-sample/src/lib.rs#L255)：decode 循环里用 `stream_spin_wait` 等流排空。

## 4. 核心概念与源码讲解

`tensor.rs` 内部其实分四个区域，正好对应本讲四个最小模块：

```text
src/tensor.rs
├── L18–L105   流覆盖机制（thread-local、guard、active_cu_stream、stream_spin_wait）
├── L107–L145  memcpy_dtod_u32_from_i32（流纪律的一个反例样本）
├── L147–L346  张量元数据词汇表（NamedTag / TensorSpec / KernelCall，服务静态调度与追踪）
├── L348–L405  DeviceContext
└── L407–L791  张量类型层（DeviceVec / DeviceMatrix / HiddenStates / GpuTensor<DIM> / Columns）
```

其中 L147–L346 的元数据词汇表（`TensorSpec`、`KernelCall` 等）是「被擦除类型的 kernel 调用 IR」，主要服务静态调度表与未来的 trace 工具，本讲不展开，遇到时知道它是纯数据描述即可。

### 4.1 DeviceContext：整个引擎的 CUDA 资源入口

#### 4.1.1 概念说明

`DeviceContext` 是 PegaInfer 一切 GPU 工作的起点：模型 crate、ops 包装层、CUDA Graph 捕获，全都是从拿到一个 `DeviceContext` 开始。它只持三样东西——CUDA 上下文、一条计算流、设备序号——并被 `#[derive(Clone)]` 标注为可廉价克隆（内部都是 `Arc`，克隆只加引用计数）。

之所以要自己包一层而不是直接用 cudarc 的类型，是因为初始化顺序里藏着三条引擎级纪律：单流、关事件追踪、cuBLAS 句柄随上下文初始化。这些纪律只有包在一个入口里才能保证不被绕过。

#### 4.1.2 核心流程

`DeviceContext::new_with_device(n)` 的初始化五步：

```text
1. ffi::cuda_set_device(n)        → 先用 FFI 把当前线程绑到目标设备（失败直接报错）
2. CudaContext::new(n)            → 创建 cudarc 的 CUDA 上下文
3. ctx.disable_event_tracking()   → 关闭 cudarc 的多流事件追踪（为 CUDA Graph 铺路）
4. ctx.new_stream()               → 创建唯一一条计算流
5. ffi::cublas_init()             → 初始化 cuBLAS 句柄
```

第 3 步是理解本 crate 架构的钥匙：cudarc 默认会在跨流拷贝时插入 `stream.wait(event)` 同步；而 PegaInfer 全程单流、根本不存在跨流依赖，这些多余的等待不仅浪费，还会**破坏 CUDA Graph 捕获**（捕获模式下非法的同步调用会让捕获失败）。所以要在创建任何流之前关掉它。

#### 4.1.3 源码精读

结构体定义——只有三个字段，全部公开（[src/tensor.rs:L348-L354](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L348-L354)）：`ctx: Arc<CudaContext>`、`stream: Arc<CudaStream>`、`device_ordinal: usize`。

初始化主体（[src/tensor.rs:L361-L397](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L361-L397)）：

```rust
pub fn new_with_device(device_ordinal: usize) -> Result<Self> {
    unsafe {
        let err = ffi::cuda_set_device(device_ordinal as i32);
        if err != 0 {
            return Err(anyhow!("Failed to set CUDA device {}: cudaError={}",
                device_ordinal, err));
        }
    }
    let ctx = CudaContext::new(device_ordinal)
        .map_err(|e| anyhow!("Failed to create CUDA context: {}", e))?;

    // Disable multi-stream event tracking before creating streams.
    // We use a single compute stream, so no cross-stream synchronization is needed.
    // This avoids stream.wait(event) calls that break CUDA Graph capture.
    // SAFETY: We only use one stream for all GPU work.
    unsafe {
        ctx.disable_event_tracking();
    }

    let stream = ctx
        .new_stream()
        .map_err(|e| anyhow!("Failed to create CUDA stream: {}", e))?;

    // Initialize cuBLAS handle
    unsafe {
        ffi::cublas_init();
    }

    Ok(Self { ctx, stream, device_ordinal })
}
```

逐段说明：

- L362–L371：先调 [ffi::cuda_set_device](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L468)（声明于 `ffi/shared.rs`，转发到 CUDA 侧）。注意错误处理风格：返回非 0 整数错误码就包成 `anyhow::Error` 返回——这正是 u2-l3 要讲的 FFI 错误契约的雏形。
- L372–L373：`CudaContext::new` 创建上下文；这一步也会初始化驱动。
- L374–L381：`disable_event_tracking` 前的注释就是上一节说的三条理由，`SAFETY:` 注释给出了安全性论证（只用一条流做所有 GPU 工作）。
- L383–L385：创建那条唯一的计算流。
- L387–L390：[ffi::cublas_init](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L465) 初始化 cuBLAS 句柄（u3-l2 精读线性层时会再遇到它）。

同步入口（[src/tensor.rs:L399-L404](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L399-L404)）：`sync()` 直接阻塞等待 `self.stream` 排空。注意它同步的是**上下文自己的流**，而不是 `active_cu_stream`——如果当前处于流覆盖状态，`sync()` 等的未必是你刚提交工作的那条流（这正是 `stream_spin_wait` 存在的原因之一，见 4.4）。

#### 4.1.4 代码实践

**实践目标**：亲手创建一个 `DeviceContext`，观察初始化路径；并理解 crate 测试如何对待「没有 GPU 的环境」。

**操作步骤**：

1. 阅读上面 L361–L397，把五步写在笔记里，标注每步失败时的报错信息长什么样。
2. 阅读 [tests/common/mod.rs:L11-L34](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/common/mod.rs#L11-L34) 的 `device_or_skip`：它先尝试 `DeviceContext::new()`；失败后再查 `device::get_count()`——count 为 0 才允许跳过；**count 非 0 但建不出上下文则直接 panic**，注释解释了原因（坏驱动和早期 `__trap()` 毒化的上下文看起来一样，绝不能当成「没有设备」静默跳过）。
3. 在有 GPU 的环境运行既有单测：

```bash
cargo test --release -p pegainfer-kernels --lib test_device_matrix_from_safetensors_matches_from_host -- --nocapture
```

**需要观察的现象**：测试创建 `DeviceContext` 后分别用 `from_host` 与 `from_safetensors` 装载同一份数据，D2H 拷回后逐位比对 bf16 位模式。运行成本身能通过即证明上下文创建、流分配、H2D/D2H 拷贝整条链路健康。

**预期结果**：测试通过；`--nocapture` 下无异常输出。若无 GPU，此测试无法运行（构建需要 nvcc，可按 u1-l3 用 `PEGAINFER_CUDA_SM` 覆盖目标架构完成编译，但设备初始化仍需真实 GPU）——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `disable_event_tracking` 必须发生在 `ctx.new_stream()` 之前？

**答案**：源码注释（L375）写明「before creating streams」。cudarc 的事件追踪作用于它创建的流对象：先建流再关闭追踪，已建流上可能已经登记了事件依赖逻辑；在创建任何流之前关闭，才能保证这条（也是唯一一条）流上不会出现 `stream.wait(event)` 调用，从而不破坏后续的 CUDA Graph 捕获。

**练习 2**：`DeviceContext` 被 `derive(Clone)`，克隆它会发生什么？为什么这是安全的？

**答案**：`ctx` 与 `stream` 都是 `Arc`，克隆只是原子递增两个引用计数，`device_ordinal` 是 `Copy` 的整数——整个克隆是 O(1) 且不涉及 CUDA API。安全的原因是它不拥有任何需要独占管理的资源：真正的释放由 `Arc` 归零时的 `CudaContext`/`CudaStream` Drop 完成。

**练习 3**：多卡机器上想用 1 号卡，应该调哪个函数？

**答案**：`DeviceContext::new_with_device(1)`（L361）；`new()`（L356–L359）只是 `new_with_device(0)` 的快捷方式。

### 4.2 GpuTensor 家族：DeviceVec、DeviceMatrix 与 HiddenStates 的列主序约定

#### 4.2.1 概念说明

`tensor.rs` 的张量层分两代：

- **非类型化层**（本讲重点）：`DeviceVec`（1 维向量）、`DeviceMatrix`（2 维行主序矩阵）、`HiddenStates`(批量激活值）。维度是运行时字段，历史最久、所有模型 crate 都在用。
- **类型化层**（u2-l2 精读）：`GpuTensor<DIM>`、`GpuWeight<OUT, IN>`、`NormWeight<DIM>`、`GpuRawSlice<ELEMS>`，用常量泛型把维度编进类型。L639–L646 的注释说明两代**并存**（additive），模型 crate 逐个迁移。

三者的分工一句话：`DeviceVec` 存一维数据（norm 权重、词表行）；`DeviceMatrix` 存权重的自然形态 `[rows, cols]` 行主序；`HiddenStates` 存「一批 token 的隐藏状态」，是前向计算里流动最频繁的对象。

#### 4.2.2 核心流程

`HiddenStates` 的内存布局是本讲最重要的约定。设隐藏维度为 \( D \)、序列长为 \( S \)，连续内存按 token 排列，token \( i \) 中第 \( d \) 个元素的地址是：

\[ \text{offset}(d, i) = i \times D + d \]

同一段字节有两种等价读法：

```text
PegaInfer 视角（token 优先）：  [token0 的 D 个值 | token1 的 D 个值 | ... | tokenS-1 的 D 个值]
cuBLAS 视角（列主序矩阵）：    [hidden_dim × seq_len] 矩阵，token i 恰好是第 i 列
```

也就是说「token i 在偏移 i×D 处连续存放」与「这是 [D, S] 的列主序矩阵」是**同一块内存的两种描述**——cuBLAS 原生按列主序工作，PegaInfer 不做任何转置就把激活直接喂给它。这条约定贯穿所有算子（L548–L551 的文档注释原文）。

#### 4.2.3 源码精读

**DeviceVec**（[src/tensor.rs:L407-L411](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L407-L411)）：`data: CudaSlice<bf16>` 加一个 `len`。四个入口——[from_host](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L415-L424)（H2D 拷贝）、[from_safetensors](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L426-L440)（把 safetensors 的字节按小端 reinterpret 成 bf16，注释坦承大端机会出错）、[zeros](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L442-L452)（零填充分配）、[to_host](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L454-L462)（D2H 拷回并逐元素转 f32，注释点明「bf16→f32 无损，所以 f32 相等即逐位相等」）。

**DeviceMatrix**（[src/tensor.rs:L474-L479](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L474-L479)）：`rows × cols` 行主序 bf16。最有意思的入口是 [vstack](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L483-L509)：把多份同列数矩阵按行拼接，先断言列数一致、分配总行数的零缓冲，然后逐个 D2D 拷贝——权重分片加载时常用。

**HiddenStates**（[src/tensor.rs:L548-L637](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L548-L637)）。结构体只有三个字段（L551–L555）：`data: CudaSlice<bf16>`、`hidden_dim`、`seq_len`。几个关键成员：

- 文档注释（L548–L551）逐行陈述 4.2.2 的布局约定。
- [checked_extent](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L575-L594)：校验 `hidden_dim * seq_len` 不溢出、且**不超过**背衬分配大小。注释解释了为什么是 `>=` 而非 `==`：缓冲按最大尺寸一次性分配，每步只把 `seq_len` 改写成当前活跃值（`gpu_buffers!` 的 `set_batch_size`，u2-l2/u4-l3 展开）。字段是公开的，安全代码可以把逻辑尺寸写到超过背衬，所以每个 launch 包装都先调它拦截。这是「CUDA Graph 指针稳定」策略在 Rust 侧的第一道防线。
- [columns](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L558-L565) 返回 `Columns`（L756–L764）：一行里可能并排躺着多个投影（例如融合的 gate|up），`Columns` 表示「每行的 `[col, col+width)` 带」，读取时仍按整行步长寻址。[assert_in_row](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L776-L791) 保证带不越出最后一行的末尾——否则按行步长读 `width` 列会在最后一行跑过缓冲区末端。u3-l1 讲 wrapper 时会看到它的真实用法。
- [as_ref](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L567-L573) 产出非拥有的 `HiddenStatesRef`（L748–L754），把「拥有」与「借用」分开，是通往非类型化 ops 的桥。
- [zeros / from_host / to_host](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L596-L637)：与 `DeviceVec` 同构，多一层 `hidden_dim × seq_len` 的长度断言。

**类型化层预告**（[src/tensor.rs:L639-L746](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L639-L746)）：`GpuTensor<const DIM: usize>` 把隐藏维度编进类型、只留 `seq_len` 在运行时（L648–L678）；`GpuWeight<OUT, IN>`、`NormWeight<DIM>`、`GpuRawSlice<ELEMS>` 同理，各自的 `from_*` 构造器用 `ensure!` 在运行时核对形状。lib.rs 开头的 `#![feature(generic_const_exprs)]`（[src/lib.rs:L1-L2](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/lib.rs#L1-L2)）说明这个 crate 需要 nightly 工具链——就是为这层类型化张量服务的。

#### 4.2.4 代码实践

**实践目标**：用 `HiddenStates` 做一次 host→device→host 往返，亲手验证列主序约定。

**操作步骤**（示例代码，读者可在自己环境放入 `pegainfer-kernels/tests/` 下临时运行）：

```rust
// 示例代码：验证 HiddenStates 布局（token i 在偏移 i*hidden_dim 处）
use half::bf16;
use pegainfer_kernels::tensor::{DeviceContext, HiddenStates};

#[test]
fn hidden_states_layout_roundtrip() {
    let ctx = DeviceContext::new().expect("需要 GPU");
    let d = 3usize; // hidden_dim
    let s = 2usize; // seq_len
    // token0 = [1,2,3]，token1 = [4,5,6]
    let host: Vec<bf16> = [1.0f32, 2.0, 3.0, 4.0, 5.0, 6.0]
        .iter().map(|v| bf16::from_f32(*v)).collect();
    let hs = HiddenStates::from_host(&ctx, &host, d, s).unwrap();
    assert_eq!(hs.data.len(), d * s);

    let back = hs.to_host(&ctx).unwrap();
    // cuBLAS 列主序视角：back[d*i + j] 是第 i 列（token i）的第 j 个分量
    assert_eq!(back[0], 1.0); // token0 分量0
    assert_eq!(back[3], 4.0); // token1 分量0 → 偏移 1*3+0
}
```

运行：`cargo test --release -p pegainfer-kernels --test hidden_states_layout -- --nocapture`（文件名按自己创建的为准）。

**需要观察的现象**：两个断言通过，说明 token1 的首元素落在偏移 `1×hidden_dim` 处，即内存按 token 分块、等价于 [hidden_dim, seq_len] 列主序。

**预期结果**：断言通过；若把 `back[3]` 改成 `back[1]` 则必然失败（偏移 1 属于 token0）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`DeviceMatrix::from_host` 断言 `data.len() == rows * cols`，`HiddenStates::from_host` 断言 `data.len() == hidden_dim * seq_len`。两者字节布局有何不同？

**答案**：都是连续 bf16 数组，差别只在语义解释：`DeviceMatrix` 按行主序读（第 r 行从偏移 `r*cols` 开始）；`HiddenStates` 的 `seq_len` 轴等价于列主序矩阵的列（token i 即第 i 列，从偏移 `i*hidden_dim` 开始）。同一份数据塞进两个类型，元素坐标含义完全不同。

**练习 2**：`checked_extent` 为什么用 `data.len() < extent` 判错，而不是 `!=`？

**答案**：见 L577–L579 注释：CUDA Graph 场景下缓冲按最大尺寸预分配、指针保持稳定，每步只改写逻辑 `seq_len`；背衬可比逻辑尺寸大是**常态**，所以校验只拒绝「背衬装不下」，不要求恰好相等。真正要防的是字段公开后被写出的「逻辑尺寸超过背衬」。

**练习 3**：`HiddenStates::to_host` 注释说「f32 equality is bitwise」，依据是什么？

**答案**：bf16 的指数位宽与 f32 相同，bf16→f32 只在低位补零、不损失任何信息；因此两个 bf16 若位模式相同，转换后的 f32 也逐位相同，测试可以直接用 f32 的 `==` 做严格比对。

### 4.3 StreamOverrideGuard 与 active_cu_stream：thread-local 流覆盖

#### 4.3.1 概念说明

问题：ops 包装层有几百个函数，签名都只收 `&DeviceContext`（加上张量参数）。当 gemma4 想把一段 prefill 工作发到 green 分区的低优先流上、qwen35 想把并发 prefill 发到另一条流上时，难道要给每个包装函数加一个 `stream` 参数？

解法：**thread-local 覆盖**。当前线程用一个 `Cell<Option<CUstream>>` 记住「临时生效的流」；所有算子包装启动 kernel 时统一调 `active_cu_stream(ctx)` 拿流——覆盖存在就用覆盖，不存在就用上下文默认流。于是「切换流」变成一个作用域内的动作（RAII guard），几百个函数签名一行不改。

配套的还有一个 `PREFILL_STREAM_OVERRIDE: Cell<bool>`：qwen35 的 split-concurrent prefill（[unified_forward.rs:L77](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-qwen35/src/unified_forward.rs#L77) 用 `activate_prefill`）需要让引擎其余部分知道「当前占用覆盖的是 prefill 通道」，`has_prefill_stream_override()` 就是这个查询口。

#### 4.3.2 核心流程

```text
激活：  unsafe { StreamOverrideGuard::activate(s) }
          └→ STREAM_OVERRIDE.replace(Some(s))        // 记住旧值到 guard.previous
             PREFILL_STREAM_OVERRIDE.replace(false)  // 记住旧值到 guard.previous_prefill
使用：  任意 ops wrapper → active_cu_stream(ctx)
          └→ 覆盖有值？用它 : ctx.stream.cu_stream()
恢复：  guard 离开作用域触发 Drop
          └→ 两个 Cell 都 set 回 previous               // 支持嵌套：恢复的是"上一层"的值
```

注意恢复的是 **previous**（进入前的旧值）而不是 `None`——所以 guard 可以嵌套：内层 guard 退出后回到外层 guard 的流，最外层退出后才回到默认流。既有单测（[src/tensor.rs:L797-L814](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L797-L814)）用假指针 0x10/0x20 精确验证了这条嵌套恢复链，全程不碰 GPU。

#### 4.3.3 源码精读

[thread_local 声明](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L18-L23)（L18 的注释点明用途：Green Context SM partitioning）：

```rust
thread_local! {
    static STREAM_OVERRIDE: Cell<Option<CUstream>> = const { Cell::new(None) };
    static PREFILL_STREAM_OVERRIDE: Cell<bool> = const { Cell::new(false) };
}
```

[StreamOverrideGuard](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L25-L61)：

```rust
pub struct StreamOverrideGuard {
    previous: Option<CUstream>,
    previous_prefill: bool,
}

impl StreamOverrideGuard {
    /// # Safety
    /// The stream must be valid for the guard's lifetime and belong to the
    /// same CUDA device as the `DeviceContext`.
    pub unsafe fn activate(stream: CUstream) -> Self {
        Self::activate_inner(stream, false)
    }
    pub unsafe fn activate_prefill(stream: CUstream) -> Self { /* ... prefill=true */ }

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

要点：

- **unsafe 契约**（L32–L34 的 Safety 注释）：流必须在 guard 整个生命周期内保持有效，且与 `DeviceContext` 同属一块设备。Rust 侧存的是裸 `CUstream`，不拥有流——所以持有对应 `Arc<CudaStream>` 的对象（如 gemma4 的 lane）必须比 guard 活得久。
- **查询口**：[has_stream_override](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L63-L66) 与 [has_prefill_stream_override](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L68-L71)。后者被 core 侧用来拒绝「流覆盖期间做分块上载」之类的操作。
- **解析顺序**（[active_cu_stream](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L73-L80)）：

```rust
#[inline]
pub fn active_cu_stream(ctx: &DeviceContext) -> CUstream {
    STREAM_OVERRIDE
        .with(Cell::get)
        .unwrap_or_else(|| ctx.stream.cu_stream())
}
```

一行总结解析顺序：**thread-local 覆盖优先，无覆盖回退 `ctx.stream`**。它标了 `#[inline]`——每个 kernel 启动都要走它，必须是零成本查询。

- **纪律反例**：[memcpy_dtod_u32_from_i32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L107-L145) 在 L117–L120 显式 `ensure!(!has_stream_override(), "dtod i32->u32 copy runs on the base stream only")`，而它发起的 `cuMemcpyDtoDAsync`（L132–L139）传的是 `ctx.stream.cu_stream()` 而非 active 流——**base-stream-only** 是这个拷贝的硬约束，宁可拒绝也不悄悄落到错误的流上。这类「每个 op 对流的取用策略必须明说」的纪律，会在 u3-l1 的 wrapper 解剖中反复出现。
- **真实消费者**：CUDA Graph 的捕获与回放都取 `active_cu_stream`（[cuda_graph.rs:L89](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L89)）——在 green 分区流上捕获的图回放时也绑定那个分区，这正是 gemma4 流量隔离能成立的底层前提（u4-l3 展开）。

#### 4.3.4 代码实践

本模块的实践就是**本讲主实践**，放在第 5 节综合实践中完整给出（创建自建流 → activate → 断言切换 → drop → 断言恢复）。这里先做轻量版：跑既有的纯主机单测（不需要 GPU 触碰设备，但 crate 编译仍需 nvcc）：

```bash
# 无 GPU 但有 CUDA 工具链时：
PEGAINFER_CUDA_SM=120 cargo test --release -p pegainfer-kernels --lib \
    stream_override_guard_restores_on_drop -- --nocapture
```

**需要观察的现象**：该测试（[src/tensor.rs:L797-L814](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L797-L814)）用 0x10/0x20 两个假指针验证：外层 `activate_prefill` 置位 prefill 标记 → 内层 `activate` 清掉它 → 内层 drop 恢复外层流与 prefill 标记 → 外层 drop 后两者归零。

**预期结果**：测试通过，输出 `ok`。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `StreamOverrideGuard::activate` 是 `unsafe`，而 `Drop` 不是？

**答案**：`activate` 接收裸 `CUstream` 并把它存进 thread-local，编译器无法验证该流指针有效、未被销毁、且属于同一设备——违反任何一条都会让后续 kernel 启动变成未定义行为，所以契约只能写在 Safety 注释里由调用方保证。`Drop` 只是把「之前记录的旧值」写回 Cell，不新解引用任何指针，本身无新增危险。

**练习 2**：两个线程各自 `activate` 了不同的流，会互相干扰吗？gemma4 的多流 decode 依赖这个性质吗？

**答案**：不会——`thread_local!` 保证每个线程一份 Cell，覆盖互不可见。gemma4 在全 SM 流与 green 分区流上各自解码时正是依赖这一点：每条流的持有线程各自激活自己的覆盖，互不踩踏（配合每条流独立的 `CudaGraphState`）。

**练习 3**：如果不慎在 guard 还存活时就 drop 了背后的 `Arc<CudaStream>`，会发生什么？为什么 Rust 编译器拦不住？

**答案**：thread-local 里留着悬垂的 `CUstream` 指针，之后本线程任何 `active_cu_stream` 的使用者都会往已释放的流上提交工作，未定义行为。编译器拦不住是因为 guard 只存裸指针、不持有引用或 `Arc`，所有权链在类型系统里断开了——这正是 Safety 注释要求「stream must be valid for the guard's lifetime」的原因。

### 4.4 stream_spin_wait：5ms 自旋 + 阻塞兜底

#### 4.4.1 概念说明

decode 循环每个 step 结束都要等流排空才能读结果。两种等法各有代价：

- **阻塞同步**（`cuStreamSynchronize`）：线程入睡，唤醒有固定的微秒级延迟——对只跑几十微秒的 decode step 来说，等待开销占比过高。
- **纯自旋**（反复 `cuStreamQuery`）：延迟最低，但若 kernel 意外跑很久，CPU 会被白烧。

`stream_spin_wait` 的策略是折中：**先自旋最多 5 毫秒**（用 `spin_loop` 提示 CPU 优化自旋循环），超时才退化为阻塞同步。decode step 的常态是远短于 5ms，于是绝大多数等待走零开销路径；异常长的 kernel 也不会烧穿 CPU。

#### 4.4.2 核心流程

```text
cap = now + 5ms
loop:
  cuStreamQuery(active_cu_stream(ctx))
    ├── CUDA_SUCCESS          → 返回 Ok
    ├── CUDA_ERROR_NOT_READY  ├── now >= cap → cuStreamSynchronize（失败则报错）→ Ok
    │                         └── 否则       → spin_loop() 继续轮询
    └── 其他错误              → 返回 Err（真正的故障，不重试）
```

注意它等待的是 **active 流**——当前线程若处于流覆盖状态，等的就是覆盖流。这补上了 4.1 里 `ctx.sync()` 只等默认流的缺口。

#### 4.4.3 源码精读

[src/tensor.rs:L82-L105](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/tensor.rs#L82-L105)：

```rust
const STREAM_SPIN_WAIT_CAP: std::time::Duration = std::time::Duration::from_millis(5);

pub fn stream_spin_wait(ctx: &DeviceContext) -> anyhow::Result<()> {
    let stream = active_cu_stream(ctx);
    let cap = std::time::Instant::now() + STREAM_SPIN_WAIT_CAP;
    loop {
        match unsafe { cudarc::driver::sys::cuStreamQuery(stream) } {
            CUresult::CUDA_SUCCESS => return Ok(()),
            CUresult::CUDA_ERROR_NOT_READY => {
                if std::time::Instant::now() >= cap {
                    let result = unsafe { cuStreamSynchronize(stream) };
                    anyhow::ensure!(result == CUresult::CUDA_SUCCESS,
                        "stream sync after spin cap failed: {result:?}");
                    return Ok(());
                }
                std::hint::spin_loop();
            }
            err => return Err(anyhow::anyhow!("cuStreamQuery failed: {err:?}")),
        }
    }
}
```

（节选，`CUresult` 路径做了缩写。）三个细节：

- L86 先解析 active 流——覆盖机制与等待语义在这里汇合。
- L100 的 `std::hint::spin_loop()` 生成 `PAUSE` 类指令，降低自旋对其他超线程的干扰并省电。
- L102 把「非 NOT_READY 的错误」直接当失败上抛——例如上下文被 `__trap()` 毒化时不会陷入死循环，而是立刻报错。

真实消费者：[pegainfer-sample/src/lib.rs:L255](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-sample/src/lib.rs#L255) 的 decode 主循环在每个 step 后调用它。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：确认「等哪条流」的语义差异，把 4.1、4.3、4.4 串成一条线。

**操作步骤**：

1. 打开 `src/tensor.rs`，对照 L400–L403（`sync` 等 `self.stream`）与 L86（`stream_spin_wait` 等 `active_cu_stream`），写下两者的差别。
2. 思考题落笔：若某线程处于 `StreamOverrideGuard` 激活状态，调 `ctx.sync()` 后立刻 D2H 拷贝覆盖流上刚写的缓冲，会发生什么？
3. 阅读 [pegainfer-sample/src/lib.rs:L255](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-sample/src/lib.rs#L255) 前后约 20 行，确认它等待的流与它提交 kernel 的流一致。

**需要观察的现象**：纸面推导应当得出——`sync()` 等默认流时覆盖流上的 kernel 可能尚未完成，读到的可能是旧数据或半成品数据；正确做法是像 `stream_spin_wait` 一样按 active 流等待。

**预期结果**：能用自己的话写出结论：「等待必须与提交走同一条流解析路径（active_cu_stream），否则存在竞态」。无需运行代码。

#### 4.4.5 小练习与答案

**练习 1**：把 `STREAM_SPIN_WAIT_CAP` 调成 0 或 50ms，分别偏向哪种策略？各有什么代价？

**答案**：调成 0 相当于第一次 NOT_READY 就直接 `cuStreamSynchronize`——纯阻塞，每次等待都付唤醒延迟，CPU 最友好；调成 50ms 则几乎总是自旋——延迟最低，但遇到长 kernel（如大 prompt prefill 混进同一条流）会空烧 CPU 数十毫秒。5ms 是 decode step 典型时长的经验折中。

**练习 2**：`cuStreamQuery` 返回 `CUDA_ERROR_NOT_READY` 与返回其他错误，为什么分支处理完全不同？

**答案**：`NOT_READY` 不是故障，只是「还没跑完」这一正常状态，应当继续轮询或最终阻塞等待；其他返回值（如上下文损坏、非法句柄）是真实故障，重试没有意义，代码立即转成 `anyhow::Error` 上抛，避免在坏上下文上无限打转。

**练习 3**：为什么 `stream_spin_wait` 里两次调 `Instant::now()` 分别用于设 cap 和比较，而不预先计算循环次数？

**答案**：每次 `cuStreamQuery` 与 `spin_loop` 的耗时并不固定（还可能被操作系统调度打断），轮询次数推不出真实流逝时间；用 `Instant` 直接度量墙钟时间才能保证「至多自旋 5ms」的承诺不被调度噪声破坏。

## 5. 综合实践

**任务**：写一个集成测试，完整走一遍本讲四个模块：创建 `DeviceContext` → 分配张量 → 建自建流并用 `StreamOverrideGuard` 切过去 → 断言 `active_cu_stream` 返回新流 → guard drop 后断言恢复默认流。

**操作步骤**：

1. 在自己环境的 `pegainfer-kernels/tests/` 下新建 `stream_override_device.rs`（示例代码）：

```rust
// 示例代码：tests/stream_override_device.rs
use pegainfer_kernels::tensor::{
    active_cu_stream, has_stream_override, DeviceContext, DeviceVec, StreamOverrideGuard,
};

#[test]
fn stream_override_switches_and_restores_device_stream() {
    // 模块 1：创建上下文（五步初始化）
    let ctx = DeviceContext::new().expect("需要一块可用 GPU");

    // 模块 2：分配两个张量（验证流上分配路径）
    let _a = DeviceVec::zeros(&ctx, 4).expect("alloc a");
    let _b = DeviceVec::zeros(&ctx, 8).expect("alloc b");

    // 模块 3：建自建流并激活覆盖
    let alt = ctx.ctx.new_stream().expect("create alternate stream");
    let alt_handle = alt.cu_stream();

    assert!(!has_stream_override());
    assert_eq!(active_cu_stream(&ctx), ctx.stream.cu_stream());

    {
        // unsafe 契约：alt（Arc<CudaStream>）必须活得比 guard 久
        let _guard = unsafe { StreamOverrideGuard::activate(alt_handle) };
        assert!(has_stream_override());
        assert_eq!(active_cu_stream(&ctx), alt_handle);
    } // guard 在此 Drop

    // guard 结束后：恢复上下文默认流
    assert!(!has_stream_override());
    assert_eq!(active_cu_stream(&ctx), ctx.stream.cu_stream());
    drop(alt); // guard 已结束，现在才允许释放自建流
}
```

2. 运行（按仓库惯例始终 `--release`；本 crate 的集成测试默认会因「无 GPU」而跳过，可设 `PEGAINFER_REQUIRE_GPU=1` 强制不跳，参见 `tests/common/mod.rs` 的门禁语义）：

```bash
cargo test --release -p pegainfer-kernels --test stream_override_device -- --nocapture
```

3. 做一个变体实验：把 `assert!(!has_stream_override())`（drop 后那行）改为在 guard 作用域**内**执行，观察测试如何失败，体会 RAII 恢复的边界在哪一行。

**需要观察的现象**：

- guard 存活期间：`has_stream_override()` 为真，`active_cu_stream(&ctx)` 返回自建流句柄（与 `alt.cu_stream()` 相等）。
- guard drop 之后：两个断言翻转，`active_cu_stream(&ctx)` 与 `ctx.stream.cu_stream()` 相等。
- 变体实验中，作用域内的 `!has_stream_override()` 断言会 panic，测试失败——恢复只发生在离开作用域的那一行。

**预期结果**：原版测试通过（输出 `ok`）；变体实验按预期失败。若环境无 GPU，测试无法完成设备初始化——**待本地验证**。

## 6. 本讲小结

- `DeviceContext` 只持 `Arc<CudaContext>` + `Arc<CudaStream>` + 设备号，但初始化五步（set device → 建 context → 关事件追踪 → 建唯一流 → cuBLAS init）锁定两条引擎纪律：单流与 CUDA Graph 兼容。
- 张量层分两代并存：非类型化的 `DeviceVec`/`DeviceMatrix`/`HiddenStates`（维度是运行时字段）与常量泛型的 `GpuTensor<DIM>`/`GpuWeight<OUT,IN>`（维度在类型里，需 nightly 的 `generic_const_exprs`）。
- `HiddenStates` 的核心约定：token \( i \) 在偏移 \( i \times D \) 处连续存放，同一段字节即 cuBLAS 眼中 `[hidden_dim, seq_len]` 的列主序矩阵——零转置直通 GEMM。
- `StreamOverrideGuard` 用 thread-local `Cell` + RAII 实现「作用域内切流」，让只收 `&DeviceContext` 的几百个算子包装透明支持 green 分区与并发 prefill；恢复的是 previous，天然支持嵌套。
- `active_cu_stream` 的解析顺序是「thread-local 覆盖优先，回退 `ctx.stream`」；`memcpy_dtod_u32_from_i32` 展示了 base-stream-only 操作如何显式拒绝覆盖。
- `stream_spin_wait` = 自旋至多 5ms + 阻塞兜底，等待的是 active 流，兼顾 decode 循环的微秒级延迟与异常长 kernel 下的 CPU 友好。

## 7. 下一步学习建议

下一讲 **u2-l2「常量泛型类型层：typed_ops 与 gpu_buffers!」** 深入本讲只做了预告的类型化层：`GpuTensor<DIM>` 与 `GpuWeight<OUT, IN>` 如何把形状检查移到编译期、`gemm_into` 家族的行为差异、`gpu_buffers!` 宏如何生成「按最大尺寸预分配、每步只改逻辑 seq_len」的缓冲结构（正是 4.2 里 `checked_extent` 那条 `>=` 约定的来源）。

继续阅读建议：

- 对照 [src/typed_ops.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/typed_ops.rs) 与 [src/gpu_buffers.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/gpu_buffers.rs)，找 `HiddenStates` 字段被 `set_batch_size` 改写的位置。
- 若对流与 CUDA Graph 的关系感兴趣，可提前读 [pegainfer-core/src/cuda_graph.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L34)（L34 起的注释直接解释了 `active_cu_stream` 与图捕获的关系），对应 pegainfer-core 教程的 u4-l1。
- 流量隔离的产品面背景：`docs/models/qwen3/green-ctx-sm-partition.md` 与 `docs/models/qwen35/unified-prefill-overlap.md`。
