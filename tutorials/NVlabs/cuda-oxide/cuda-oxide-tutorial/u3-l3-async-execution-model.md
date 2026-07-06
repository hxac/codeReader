# 异步执行模型

## 1. 本讲目标

本讲是「宿主运行时」单元的第三篇。前面 u2-l4 讲了如何用 `module.vecadd(&stream, ...)` **立即**把内核入队（raw 配置需 `unsafe`，签约内核走 `prepare_*` → `PreparedLaunch` 受检启动），u3-l1 讲了 `CudaContext`/`CudaStream`/`CudaEvent` 的安全封装。本讲把视角抬高一层：把「一个 GPU 操作」抽象成一个**惰性（lazy）对象**，它在被构造时不绑定任何流、不产生任何副作用，直到调度时刻才被绑定到某条 CUDA 流上真正执行。

学完本讲你应该能够：

- 理解 `DeviceOperation` trait 的惰性求值模型：描述计算、但**流无关（stream-agnostic）**。
- 说清 `DeviceFuture` 的**三态机** `Idle → Executing → Complete`，以及每次 `poll` 在各态下做什么。
- 解释 `cuLaunchHostFunc` 主机回调 + `AtomicWaker` 如何在不忙等（busy-wait）的前提下唤醒 future。
- 掌握 #318 后的 `*_async` 启动边界：raw 配置方法（`vecadd_async(LaunchConfig, ...)`）现在是 `unsafe fn`，底层走 `AsyncKernelLaunchBuilder::finalize_unchecked` 这道显式 unsafe 闸门；签约内核另有安全的「借用异步 / owned 异步」受检方法，消费同一个 `PreparedLaunch` 证明。

## 2. 前置知识

在进入源码前，先用大白话澄清几个本讲会用到的概念。

- **CUDA stream（流）**：GPU 上的一个「工作队列」。同一流内的任务按顺序执行（FIFO），不同流之间可以并行重叠。u3-l1 已经用 `CudaStream` 安全封装过它。
- **入队（enqueue）≠ 执行**：调用 `cuLaunchKernel` 只是把内核塞进流的队列，函数立刻返回，GPU 可能在之后才真正跑它。要拿结果，必须先同步该流。
- **Rust 的 `Future` / `async`/`await`**：一个 `Future` 是「还没算完的值」。它由执行器（executor）反复调用 `poll` 来驱动：`poll` 返回 `Poll::Ready(v)` 表示完成，返回 `Poll::Pending` 表示「还没好，请稍后再 poll 我」。
- **`Waker`**：`poll` 时执行器会塞进来一个 `Waker`。当 future 发现自己「还没好」时，应当把 `Waker` 存起来；将来某个事件触发时调用 `waker.wake()`，执行器就会被通知「可以再 poll 一次了」。**关键纪律**：不要在 `Pending` 里空转死等（busy-wait），而要用 `Waker` 被动唤醒。
- **`AtomicWaker`**：`futures` crate 提供的工具，让「另一个线程」（这里是 CUDA 的主机回调）能安全地唤醒一个 future。
- **惰性求值（lazy evaluation）**：先**描述**「要做什么」，等真正需要时再**执行**。本讲里，「描述」就是 `DeviceOperation`，「执行」就是把它绑定到流上入队。
- **raw `LaunchConfig` 与启动契约（承接 u2-l4）**：一个裸的 `LaunchConfig`（grid/block/shared 三元组）只是原始数据，**不携带**「这个内核需要几维索引、块多大、要多少动态共享内存」的信息。所以「喂 raw config 启动」天然 `unsafe`——调用方得自己证明这些。`#[launch_contract]` 则让作者把这些假设写进源码，宏据此生成 `prepare_*` 与 `PreparedLaunch` 受检安全 API。本讲会把同样的二分搬到**异步**路径上。

一句话概括本讲的设计哲学：**把 GPU 工作建模成一个可以先组合、后调度的惰性值，再用 Rust Future 把「GPU 完成」这件事桥接回异步执行器；并把 raw 启动的不安全性收束到一道显式 unsafe 闸门。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `crates/cuda-async/src/lib.rs` | crate 总览，给出 `DeviceOperation → schedule → DeviceFuture → .await` 的架构图。 |
| `crates/cuda-async/src/device_operation.rs` | 核心 trait `DeviceOperation`、`ExecutionContext`、`schedule`/`sync`/`sync_on`/`async_on` 与组合子。 |
| `crates/cuda-async/src/device_future.rs` | `DeviceFuture`：三态机、`StreamCallbackState`、`Future` 的 `poll` 实现、回调注册与取消回收。 |
| `crates/cuda-async/src/scheduling_policies.rs` | `SchedulingPolicy` trait 与 `StreamPoolRoundRobin`（轮询挑流）。 |
| `crates/cuda-async/src/launch.rs` | #318 拆成两型：安全惰性的 `AsyncKernelLaunchBuilder`（不实现 `DeviceOperation`/`IntoFuture`）与不可变可运行的 `AsyncKernelLaunch`；两者之间靠 `unsafe finalize_unchecked` 跨越。 |
| `crates/cuda-async/src/device_context.rs` | 线程级设备状态、`with_default_device_policy`、默认轮询池（4 条流）。 |
| `crates/cuda-async/src/reclaim.rs` | 取消后「在途结果」的延迟回收（limbo / park / sweep）。 |
| `crates/cuda-host/src/launch.rs` | `PreparedAsyncKernelLaunch`/`PreparedOwnedAsyncKernelLaunch`：把已受检的 `PreparedLaunch` 套在惰性 launch 外，提交前再 `validate_stream`。 |
| `crates/cuda-macros/src/lib.rs` | 过程宏生成 `vecadd_async`（raw → `unsafe fn`）与签约内核的 `prepare_*`/受检 `vecadd_async(&prepared)`。 |
| `crates/rustc-codegen-cuda/examples/async_vecadd/src/main.rs` | 完整可运行示例，串起 `load_async` → `unsafe { vecadd_async(...) }` → `.sync()`。 |

## 4. 核心概念与源码讲解

### 4.1 DeviceOperation trait：惰性、流无关的 GPU 操作

#### 4.1.1 概念说明

`DeviceOperation` 是 `cuda-async` crate 的核心抽象。它代表「一个 GPU 操作」——可能是一次内核启动、一次设备内存搬运、或者它们组合出来的数据流图。

它有两个关键性质：

1. **惰性**：构造一个 `DeviceOperation` **不做任何 GPU 工作**，也不分配 GPU 资源。它只是「一份描述」。
2. **流无关**：它**不携带**任何 `CudaStream`。具体用哪条流，被推迟到「调度时刻」才决定。

为什么要把流推迟？因为这样你就可以**先把多个操作拼成一张依赖图**（谁先谁后、谁和谁可以并行），而拼图阶段完全不需要接触 GPU；等整张图拼好、要执行时，再由**调度策略（SchedulingPolicy）**统一为每个操作挑流，从而实现独立工作之间的硬件级重叠。

#### 4.1.2 核心流程

一个 `DeviceOperation` 的生命周期可以画成：

```text
   构造（描述计算，无副作用）
        │
        │  组合子拼接：and_then / zip! / apply ...
        │  （依然流无关，依然没有 GPU 工作）
        ▼
   调度 schedule()：SchedulingPolicy 选一条流，包成 DeviceFuture
        │
        ▼
   执行 execute(&ExecutionContext)：在选定流上真正入队 GPU 工作
```

trait 提供了几条「拿到结果」的路径，区别只在「谁选流」和「是否阻塞」：

| 方法 | 谁选流 | 阻塞宿主线程? | 返回 Future? |
|------|--------|--------------|--------------|
| `schedule(policy)` | 传入的策略 | 否 | 是（`DeviceFuture`） |
| `.await`（经 `IntoFuture`） | 线程级默认策略 | 否 | 是 |
| `sync()` | 线程级默认策略 | 是 | 否 |
| `sync_on(stream)` | 调用方指定的流 | 是 | 否 |
| `async_on(stream)` | 调用方指定的流 | 否 | 否（直接入队，不等） |

#### 4.1.3 源码精读

trait 本身的定义在 [crates/cuda-async/src/device_operation.rs:133-162](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_operation.rs#L133-L162)：注意它的 super-trait 是 `IntoFuture`，且关联类型 `Output: Send + 'static`。这两个约束是后续三态机与延迟回收的前提。

`execute` 是每个 `DeviceOperation` 实现者必须提供的唯一「干活」方法，见 [crates/cuda-async/src/device_operation.rs:150-153](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_operation.rs#L150-L153)：

```rust
/// # Safety
///
/// GPU work may still be in flight when this returns. The caller must
/// synchronize the stream before reading device-side outputs.
unsafe fn execute(
    self,
    context: &ExecutionContext,
) -> Result<<Self as DeviceOperation>::Output, DeviceError>;
```

它的 `unsafe` 不是说实现者危险，而是**契约**：`execute` 返回时 GPU 工作可能仍在飞（in flight），调用者必须自己同步流才能读设备端输出。注意 `self` 被**消费**——一个操作只执行一次。

`ExecutionContext` 就是「执行上下文」：把一条流和它所属的 context/device 打包，见 [crates/cuda-async/src/device_operation.rs:47-84](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_operation.rs#L47-L84)。实现者只从这个 context 里取出流来入队，所以 context 是「操作」与「具体流」之间唯一的耦合点。

四条执行路径的实现都很薄，都在 trait 上给了默认实现。其中最常用的是 `sync()` 与 `sync_on()`，见 [crates/cuda-async/src/device_operation.rs:219-245](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_operation.rs#L219-L245)：

```rust
fn sync(self) -> Result<...> {
    with_default_device_policy(|policy| policy.sync(self))?
}

fn sync_on(self, stream: &Arc<CudaStream>) -> Result<...> {
    let ctx = ExecutionContext::new(Arc::clone(stream));
    let res = unsafe { self.execute(&ctx) };       // 入队
    finish_sync(res, stream.synchronize())          // 阻塞到流空闲
}
```

可以看到 `sync()` 把「选流」委托给线程级默认策略（`with_default_device_policy`，本讲 4.4 节会讲它怎么取到默认策略），而 `sync_on()` 直接由调用方指定流。两者最后都走到「`execute` 入队 + `synchronize` 阻塞」。

> 小知识：`schedule` 返回的 `DeviceFuture` 把「执行」推迟到第一次 `poll`，而 `sync`/`sync_on` 是**当场**就 `execute`。这是 `.await` 与 `.sync()` 在执行时机上的根本差别。

#### 4.1.4 代码实践

这是一道源码阅读型实践，目标是把四条路径彻底分清。

1. **实践目标**：对照 trait 默认实现，画出 `schedule` / `.await` / `sync` / `sync_on` / `async_on` 的调用关系。
2. **操作步骤**：
   - 打开 [device_operation.rs:219-245](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_operation.rs#L219-L245) 与 [device_operation.rs:229-235](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_operation.rs#L229-L235)。
   - 追问：`sync` 最终是否一定调用了 `execute`？`async_on` 是否调用了 `synchronize`？
3. **需要观察的现象**：在源码里确认 `sync` 走 `policy.sync(self)`，而 `sync_on` 自己 `execute` + `synchronize`。
4. **预期结果**：`sync` 与 `sync_on` 都会阻塞到流空闲；`async_on` 与 `schedule` 都不阻塞，区别是 `schedule` 包成 future（执行推迟到 poll），`async_on` 当场入队但不等。
5. 若不确定结论，标记「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`DeviceOperation::execute` 为什么标成 `unsafe fn`？

> **答案**：因为它返回时 GPU 工作可能仍在飞。调用者必须保证在读取设备端输出前先同步流，否则读到的是未完成的数据。`unsafe` 表达的是这条「先同步再读」的契约，而非实现本身不安全。

**练习 2**：`type Output: Send + 'static` 里的 `'static` 是为了什么？

> **答案**：一个被取消（drop）的、但 GPU 工作仍在飞的结果，会被放进 `reclaim` 的 limbo 里延迟回收，它可能比任何非 `'static` 的借用活得更久。`'static` 保证这样的延迟回收是安全的。

---

### 4.2 *_async 启动：从内核到 DeviceOperation（含 #318 的 builder/operation 拆分）

#### 4.2.1 概念说明

普通用户其实很少手写 `DeviceOperation` 实现。最常见的情况是：你写了一个 `#[kernel]`，并用 `#[cuda_module]` 标注了模块（见 u2-l1）。这时宏会**额外**为你生成一组 `*_async` 方法，把「一次内核启动」包装成一个现成的 `DeviceOperation`。

#318 给异步启动路径带来和同步侧（u2-l4）一致的安全收束。先看两个底层类型，它们把「攒参数」和「可运行」彻底分开：

- `AsyncKernelLaunchBuilder`：**安全的惰性 builder**。只攒「函数句柄 + 启动属性（cluster/cooperative）+ 类型擦除的参数指针」，**刻意不实现** `DeviceOperation` / `IntoFuture`——也就是说它根本没法被 `.sync()` / `.await`。
- `AsyncKernelLaunch`：**不可变的可运行操作**。它持有完整的 grid/block/shared 配置，实现 `DeviceOperation` + `IntoFuture`，可以被调度。

两者之间**只有一道桥**：`AsyncKernelLaunchBuilder::finalize_unchecked(cfg)`，一个显式的 `unsafe fn`。raw `LaunchConfig` 必须在这里「跨越安全边界」，因为裸 config 不携带内核的索引/块/共享内存语义。这正好对应宏文档里的 [crates/cuda-macros/src/lib.rs:335-342](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L335-L342)：「raw 同步、借用异步、owned 异步方法都是 `unsafe`；加 `#[launch_contract]` 才能得到安全的 prepared-launch 路径」。

于是宏为每个内核按「是否签约」生成两套异步入口：

| 内核类型 | 借用异步方法（参数借用设备缓冲） | owned 异步方法（缓冲 move 进操作） |
|---------|------------------------------|--------------------------------|
| 未签约（raw） | `unsafe fn vecadd_async(LaunchConfig, ...) -> Result<AsyncKernelLaunch>` | `unsafe fn vecadd_async_owned(LaunchConfig, ...)` |
| 签约（`#[launch_contract]`） | 安全 `fn vecadd_async(&PreparedLaunch, ...) -> PreparedAsyncKernelLaunch`，另有 `unsafe fn vecadd_async_unchecked` 逃生口 | 安全 `fn vecadd_async_owned(&PreparedLaunch, ...)`，另有 `*_unchecked` |

> 注意 `load_async`：与 u3-l2 讲的 `load` 一样，**签约模块**的 `load_async` / `load_async_named` 也是 `unsafe fn`（一次性绑定证明：你加载的确实是这个模块编译出的 artifact）。未签约模块的 `load_async` 仍是安全的。本讲的 `async_vecadd` 示例未签约，所以 `load_async` 安全，但 `vecadd_async` 因吃 raw config 而是 `unsafe`。

#### 4.2.2 核心流程

raw 路径（本讲 `async_vecadd` 示例走的路）：

```text
   unsafe { module.vecadd_async(cfg, &a, &b, &mut c) }?
        │  宏内部：new_async_kernel_launch_builder → push 参数
        │           → unsafe { builder.finalize_unchecked(cfg) }
        │  返回不可变 AsyncKernelLaunch（一个 DeviceOperation）
        │  —— 此刻没有任何 GPU 工作
        ▼
   .sync()?        // 或 .await
        │  策略挑流 → execute → cuLaunchKernel 入队 → 同步
        ▼
   结果
```

签约路径（推荐写法，把证明收进生成代码）：

```text
   let prepared = module.prepare_vecadd(LaunchConfig1D::new(grid, block, shmem))?;
        │  在活设备上一次性校验 block/共享内存/算力/cluster/cooperative
        │  → 产出品牌化见证 PreparedLaunch<__vecadd_CudaKernel>
        ▼
   module.vecadd_async(&prepared, &a, &b, &mut c)   // 安全！
        │  内部 unsafe finalize_unchecked(prepared.__raw_config())
        │  → 包成 PreparedAsyncKernelLaunch（也是 DeviceOperation）
        ▼
   .sync()? / .await?
        │  execute 先 prepared.validate_stream() 再委托内层 launch
```

`AsyncKernelLaunchBuilder` 内部像一个 builder：累积「函数句柄 + 启动属性 + 一堆类型擦除的参数指针」。参数被堆分配（`Box`）并由 `Drop` 在提交后回收，保证指针在 `cuLaunchKernel` 把值拷走之前一直有效。值得注意的是 #318 给 `KernelArgument` 加了 `Send` super-trait，所以非 `Send` 的捕获（如 `Rc`）进不了这个将被跨线程调度的操作。

#### 4.2.3 源码精读

先看两个类型的字段。**安全的 builder** 见 [crates/cuda-async/src/launch.rs:60-73](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/launch.rs#L60-L73)：注意它**没有** `cfg` 字段——launch 配置此刻还没进来。**不可变的可运行操作**见 [crates/cuda-async/src/launch.rs:90-104](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/launch.rs#L90-L104)：这里才有完整的 `cfg: LaunchConfig`，且构造它唯一的方式是消费一个 builder。

那道显式 unsafe 闸门是 [crates/cuda-async/src/launch.rs:236-281](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/launch.rs#L236-L281)：

```rust
/// Accepts an unverified raw launch configuration and seals this builder
/// into an immutable, schedulable operation.
///
/// # Safety
/// The caller must prove all facts that a typed prepared launch normally checks:
/// - `cfg` must match the kernel's indexing dimensionality, launch bounds,
///   block shape, and dynamic shared-memory requirements;
/// - ...
pub unsafe fn finalize_unchecked(self, cfg: LaunchConfig) -> AsyncKernelLaunch<'a> {
    AsyncKernelLaunch { func: self.func, args: self.args, cfg, /* ... */ }
}
```

它的文档同时挂了三个 `compile_fail` doctest 来固化契约：builder 不能被 `schedule`（不实现 `DeviceOperation`）、builder 不是 `Send`（参数存储留在组装线程）、`finalize_unchecked` 必须在 `unsafe` 块里调。换言之，「raw config → 可运行」这一步**无法**在安全 Rust 里悄悄发生。

`AsyncKernelLaunch` 实现 `DeviceOperation`，`execute` 只是调内部的 `launch(stream)`，见 [crates/cuda-async/src/launch.rs:484-490](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/launch.rs#L484-L490)。真正的入队在 `launch` 里按 `(cluster_dim, cooperative)` 四种组合分派到 `launch_kernel_on_stream` / `launch_kernel_ex_on_stream` 等 cuda-core 函数（见 [launch.rs:303-348](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/launch.rs#L303-L348)）。注意 `launch` 现在直接 `let cfg = self.cfg;`——因为 cfg 已是必填字段，不再有「未设置」的运行期错误。

为了能 `.await`，它还实现了 `IntoFuture`，见 [crates/cuda-async/src/launch.rs:493-502](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/launch.rs#L493-L502)：

```rust
fn into_future(self) -> Self::IntoFuture {
    match with_default_device_policy(|policy| policy.schedule(self)) {
        Ok(Ok(future)) => future,
        Ok(Err(e)) | Err(e) => DeviceFuture::failed(e),
    }
}
```

`.await` 会先用线程级默认策略 `schedule`（挑流 + 包成 `DeviceFuture`）；如果挑流阶段就失败（例如上下文未初始化），就返回一个**生而失败**的 `DeviceFuture`（4.3 节会讲它的 `Failed` 态）。

用户面的方法是过程宏生成的。对**未签约内核**，宏生成 `unsafe fn vecadd_async(LaunchConfig, ...) -> Result<AsyncKernelLaunch, DriverError>`，函数体正是「构造 builder → push 参数 → `Ok(unsafe { builder.finalize_unchecked(config) })`」，见 [crates/cuda-macros/src/lib.rs:2339-2394](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2339-L2394)。对**签约内核**，宏额外生成安全的 `fn vecadd_async(&PreparedLaunch, ...) -> PreparedAsyncKernelLaunch`，内部用 `finalize_unchecked(prepared.__raw_config())`（证明已由 `prepare_*` 完成），并另给一个 `unsafe fn vecadd_async_unchecked` 逃生口，见 [crates/cuda-macros/src/lib.rs:2396-2493](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2396-L2493)。宏文档里给出的 raw 用法范例（已被 `unsafe` 包住）正是本讲的实践目标，见 [crates/cuda-macros/src/lib.rs:322-332](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L322-L332)：

```ignore
let module = kernels::load_async(0)?;
// SAFETY: the raw launch is fully 1-D and matches vecadd's resources.
unsafe {
    module.vecadd_async(LaunchConfig::for_num_elems(n), &a, &b, &mut c)
}?.sync()?;
```

签约内核的安全 prepared 路径多一层包装 `PreparedAsyncKernelLaunch`，定义在 [crates/cuda-host/src/launch.rs:330-336](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/launch.rs#L330-L336)：它把一个已 finalizing 的 `AsyncKernelLaunch` 和品牌化见证 `PreparedLaunch<Contract>` 绑在一起。它的 `DeviceOperation::execute` 在委托内层 launch 之前，先做一次 `prepared.validate_stream(...)`——把受检启动的「流属于正确 context」校验也搬到异步路径，见 [crates/cuda-host/src/launch.rs:412-428](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/launch.rs#L412-L428)。它同样实现 `IntoFuture`，所以 `.await` 一样可用。

一个完整的端到端 raw 例子在 [async_vecadd/src/main.rs:92-104](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/async_vecadd/src/main.rs#L92-L104)：`unsafe { module.vecadd_async(...) }?` 拿到惰性操作，`.sync()?` 才真正挑流、入队、阻塞等待。

#### 4.2.4 代码实践

1. **实践目标**：确认 `vecadd_async` 的返回类型，并亲眼看到「构造时不启动」；体会 raw 路径为何必须 `unsafe`。
2. **操作步骤**：
   - 先 `cargo oxide build async_vecadd`（不需要 GPU，仅确认能编译）。
   - 打开示例 [async_vecadd/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/async_vecadd/src/main.rs)，在 `unsafe { module.vecadd_async(...) }?` 与 `.sync()?` 之间**临时插入一行** `println!("not launched yet");`，体会这两步是分开的。
   - 进阶：试着把 `unsafe` 块去掉，观察编译器报 `E0133`（调用 unsafe 函数需 unsafe 块），这正是 `finalize_unchecked` 闸门在生成方法上的体现。
3. **需要观察的现象**：编译通过；`vecadd_async` 返回的 `AsyncKernelLaunch` 是惰性的，必须 `.sync()`/`.await` 才有 GPU 工作；去掉 `unsafe` 编译失败。
4. **预期结果**：能编译；运行（需 GPU）时内核结果正确。
5. 运行结果（需要 GPU）：待本地验证。无 GPU 时用 `build` 即可完成本实践的阅读部分。

#### 4.2.5 小练习与答案

**练习 1**：为什么 #318 要把 `AsyncKernelLaunch` 拆成 `AsyncKernelLaunchBuilder` + `AsyncKernelLaunch` 两型，而不是保留原来一个带 `set_launch_config` 的可变类型？

> **答案**：为了让「raw 配置进入」成为**唯一且显式**的 unsafe 点。拆分后，builder 刻意不实现 `DeviceOperation`/`IntoFuture`（`compile_fail` doctest 保证），于是你无法在安全 Rust 里把一个还没提交几何证明的 builder 调度出去；而 `AsyncKernelLaunch` 一旦经 `finalize_unchecked` 构造就是不可变的，没有任何 setter 能再改它的 cfg/参数，安全假设不会被事后破坏。这道闸门把 u2-l4 的「raw config 不安全」纪律原样搬到了异步侧。

**练习 2**：`KernelArgument` 为什么在 #318 后加了 `Send` super-trait？

> **答案**：因为 finalizing 后的 `AsyncKernelLaunch` 是 `Send` 的，会被默认调度策略扔到另一条流（进而可能由别的宿主线程驱动回调）。如果允许 `Rc` 这类线程亲和的值混进类型擦除的参数存储，就会在被跨线程消费时触发 UB。`KernelArgument: Send`（以及 `Box<T: Send + 'static>` 的实现）在压栈这一刻就把非 `Send` 捕获挡在编译期。

**练习 3**：签约内核的 `module.vecadd_async(&prepared, ...)` 是安全方法，但它内部明明也调了 `unsafe finalize_unchecked`。这个 `unsafe` 凭什么是 sound 的？

> **答案**：因为「cfg 与内核语义匹配」这一事实已经由 `prepare_vecadd(...)` 在活设备上一次性证完，结果存进品牌化见证 `PreparedLaunch<Contract>`；此处只是用 `prepared.__raw_config()` 把同一份已证 config 喂给 `finalize_unchecked`，且 builder 没有暴露任何能在 finalizing 后改写 cfg/参数的接口。`PreparedAsyncKernelLaunch::execute` 还会再 `validate_stream` 校验流归属。所以全部 unsafe 都被封装在生成代码内部，对外是安全 API。

---

### 4.3 DeviceFuture 三态机

#### 4.3.1 概念说明

`DeviceFuture` 是把 `DeviceOperation` 桥接成 Rust `Future` 的类型。它实现 `std::future::Future`，所以可以被任意异步执行器 `.await`。

它内部是一个**三态机**（外加一个失败态）：

```text
   Idle ──poll()──> Executing ──回调触发──> Complete
          入队 + 注册回调         (返回结果)
```

- **Idle**：刚被 `schedule` 出来，还没执行过。第一次 `poll` 时：入队 GPU 工作 + 注册主机回调 → 转 `Executing`，返回 `Pending`。
- **Executing**：工作已入队，等回调。后续 `poll` 检查回调是否已置位：是 → 转 `Complete` 返回结果；否 → 重新注册 `Waker` 返回 `Pending`。
- **Complete**：结果已交出。再 `poll` 会 `panic!`。

还有一个 **Failed** 态：`schedule` 阶段就失败（例如挑不出流）时，future 生而失败，第一次 `poll` 立刻返回 `Err`。

#### 4.3.2 核心流程

`poll` 的状态转移可以写成伪代码：

```text
fn poll(state, waker):
    reclaim.sweep()                      // 顺带回收已完成的老结果
    match state:
      Failed  -> return Ready(Err)
      Idle    -> register waker
                  execute(op)           // ★ 此时 GPU 工作才真正入队
                  register_callback()   // 注册 cuLaunchHostFunc 回调
                  state = Executing
                  return Pending
      Executing -> if callback_complete?
                     state = Complete
                     return Ready(Ok(result))
                   else
                     register waker
                     return Pending
      Complete -> panic("poll after completion")
```

关键点：**GPU 工作是在 `Idle → Executing` 的第一次 poll 中被入队的**。从这一刻起，GPU 异步运行该工作；future 则通过回调被动等待，绝不忙等。

#### 4.3.3 源码精读

三态枚举定义在 [crates/cuda-async/src/device_future.rs:39-50](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L39-L50)。`DeviceFuture` 结构体本身持有：待执行的操作、执行上下文、结果槽、错误槽、当前态、以及与回调共享的状态，见 [crates/cuda-async/src/device_future.rs:99-113](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L99-L113)。

`Future` 的 `poll` 实现与一份状态表注释在一起，见 [crates/cuda-async/src/device_future.rs:285-355](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L285-L355)。其中 `Idle` 分支是「真正干活」的地方，见 [device_future.rs:319-337](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L319-L337)：

```rust
DeviceFutureState::Idle => {
    waker_state.waker.register(cx.waker());      // 先注册，防丢唤醒
    if let Err(e) = self.execute() { ... }        // 入队 GPU 工作
    if let Err(e) = unsafe { self.register_callback(...) } { ... } // 注册回调
    self.state = DeviceFutureState::Executing;
    Poll::Pending
}
```

注意两个细节：第一，`execute()` 真正在这里被调用——这就是「GPU 工作何时真正提交」的答案。第二，`waker.register(...)` 必须在 `execute` 之前，否则存在「回调在注册 waker 之前就触发」的竞态。`Executing` 分支则检查原子 `complete` 标志，见 [device_future.rs:338-350](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L338-L350)：完成就返回结果，没完成就再注册一次 waker 后 `Pending`。

crate 顶层文档把整个架构画成 `DeviceOperation ──schedule──> DeviceFuture ──.await──> Result<T>`，见 [crates/cuda-async/src/lib.rs:14-23](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/lib.rs#L14-L23)。

> 小知识：`DeviceFuture` 还实现了 `Unpin`（[device_future.rs:274](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L274)），因为它没有自引用指针，可以安全移动——这对很多执行器是必需的。无论是 raw `AsyncKernelLaunch` 还是签约 `PreparedAsyncKernelLaunch`，最终都被 `schedule` 包进同一个 `DeviceFuture`，三态机对二者完全一致。

#### 4.3.4 代码实践

1. **实践目标**：把三态机的每个分支在源码里逐一对应。
2. **操作步骤**：
   - 打开 [device_future.rs:295-354](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L295-L354) 的 `poll`。
   - 在笔记里画一张状态图，把 `Idle` 分支的两步动作（`execute` + `register_callback`）标在 `Idle→Executing` 的转移箭头上。
3. **需要观察的现象**：确认 `execute()` 只在 `Idle` 分支调用一次；`Executing` 分支只读原子标志、不重复入队。
4. **预期结果**：能解释「内核在第一次 poll 时入队，此后 poll 只是检查完成标志」。
5. 结论可在源码中直接验证，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Idle` 分支里要先 `waker.register(...)` 再 `execute()`，而不是反过来？

> **答案**：避免丢失唤醒。若先 `execute` 再注册 waker，可能存在一个窗口：GPU 极快完成、回调已经 `signal()` 想唤醒，但此时 waker 还没注册，`wake()` 落空，future 永远卡在 `Pending`。先注册后执行，再靠 `Executing` 分支末尾的二次检查兜底，就不会丢唤醒。

**练习 2**：`Complete` 态再被 `poll` 会发生什么？为什么这样设计？

> **答案**：会 `panic!("Poll called after completion.")`。因为 future 完成后其结果已被 `take()` 走，内部状态不再自洽；`panic` 是为了把「对已完成的 future 重复 poll」这种明显的执行器误用尽早暴露，而不是悄悄返回错误值。

---

### 4.4 回调唤醒：cuLaunchHostFunc + AtomicWaker（含取消与延迟回收）

#### 4.4.1 概念说明

`DeviceFuture` 要等 GPU 完成，但不能在 `poll` 里死等（那会占住执行器线程）。它的解法是 **CUDA 的主机回调 `cuLaunchHostFunc`**：

- 在 `Idle→Executing` 时，往同一条流里**再入队一个主机回调**。
- 当 GPU 执行到流里的这个位置时（即前面所有工作都完成了），驱动会在**某个主机线程**上调用这个回调。
- 回调只做两件事：把一个原子 `complete` 置位，并 `wake()` 之前注册的 `Waker`。
- 执行器被唤醒后重新 `poll`，发现 `complete` 为真，就返回结果。

`AtomicWaker`（来自 `futures` crate）让「CUDA 主机回调线程」能安全地唤醒「执行器线程」持有的 future，避免了用裸 `Mutex<Waker>` 的开销与死锁风险。

**取消（drop）怎么办？** 这是本节最精妙的设计：drop 一个还在飞的 future **不会取消 GPU 工作**——内核照常跑完。drop 也不能立刻释放结果持有的设备资源（例如显存），因为流序分配器可能已经把这块内存转手给别人，而内核还在写它。于是 cuda-async 把这样的「在途结果」连同「一个完成事件」**寄存（park）到 limbo**，等设备时间线越过该事件后再 `sweep` 回收。这样 drop 永不阻塞 GPU 进度。

#### 4.4.2 核心流程

唤醒路径：

```text
   poll(Idle): execute() 入队内核 ──> register_callback() 入队 cuLaunchHostFunc
        ...
   GPU 跑完内核，执行到回调点
        │
        ▼
   主机线程调用回调 → StreamCallbackState::signal()
        │  complete.store(true)
        │  waker.wake()
        ▼
   执行器被唤醒 → poll(Executing) 发现 complete -> 返回结果
```

取消路径：

```text
   drop(DeviceFuture)  (state == Executing，结果还在飞)
        │  record_event(stream)        // 记录完成事件
        │  reclaim::park(event, result)// 寄存到 limbo
        ▼
   后续任意 poll / drop → reclaim::sweep()
        │  event.query() 通过?  -> drop 结果   // 安全了
        │  还在飞?              -> 继续寄存
```

#### 4.4.3 源码精读

共享状态 `StreamCallbackState` 持有 `AtomicWaker` 和 `AtomicBool`，见 [crates/cuda-async/src/device_future.rs:56-63](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L56-L63)。回调触发时调用的 `signal()` 见 [device_future.rs:75-79](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L75-L79)：先置位再唤醒。

注册回调的方法 `register_callback` 见 [device_future.rs:139-150](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L139-L150)：它把一个 `move || waker_state.signal()` 闭包通过 `launch_host_function` 入队，底层就是 `cuLaunchHostFunc`（见 cuda-core 的 [stream.rs:171-178](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/stream.rs#L171-L178)）。

线程级默认策略的入口是 `with_default_device_policy`，见 [crates/cuda-async/src/device_context.rs:241-247](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_context.rs#L241-L247)，它取当前线程默认设备的策略。默认策略是 `StreamPoolRoundRobin`，池大小为 4，见 [device_context.rs:37](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_context.rs#L37)。它的 `schedule` 用原子计数器轮询挑流，见 [scheduling_policies.rs:174-198](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/scheduling_policies.rs#L174-L198)，挑流公式为：

\[
\text{idx} = \big(\text{counter}.\text{fetch\_add}(1)\big) \bmod N
\]

其中 \(N\) 是池大小（默认 4）。轮询让独立操作落到不同流上，从而被硬件重叠执行。

取消时的延迟回收在 `DeviceFuture::drop` 与 `reclaim_in_flight_result` 里，见 [device_future.rs:276-283](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L276-L283) 与 [device_future.rs:194-219](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L194-L219)：先 `reclaim::sweep()` 清理已完成的旧条目，再把自己的在途结果记一个事件并 `park`。limbo 的实现见 [reclaim.rs:94-140](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/reclaim.rs#L94-L140)：`sweep` 只做非阻塞的 `event.query()`，通过才 drop；查不到（还在飞或查询失败）一律继续寄存。`poll` 与 `drop` 每次都会顺手 `sweep` 一下，开销极低（无寄存时只一次原子读，见 [reclaim.rs:115-118](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/reclaim.rs#L115-L118)）。

> 小知识：回调里绝不能调用任何 CUDA API，所以 payload 的 `drop` 不能发生在回调里——`reclaim` 保证了 payload 只在普通主机线程的 `sweep`/`drain` 中被 drop。

#### 4.4.4 代码实践

1. **实践目标**：把「回调如何唤醒」与「取消如何回收」两条链在源码里走通。
2. **操作步骤**：
   - 从 [device_future.rs:139-150](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L139-L150) 的 `register_callback` 出发，追到 `launch_host_function`（cuda-core 的 `cuLaunchHostFunc`），再追回 `signal()`（[device_future.rs:75-79](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_future.rs#L75-L79)）。
   - 阅读 reclaim 的单测 [reclaim.rs:234-262](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/reclaim.rs#L234-L262)，看一个 `MockGate`：gate 未通过时 `sweep` 不 drop，gate 通过后才 drop。
3. **需要观察的现象**：确认 `signal()` 只做两个原子/唤醒操作；确认 `sweep` 永不阻塞。
4. **预期结果**：能用自己的话说出「回调 → 置位 + 唤醒 → 重 poll 取结果」与「drop 在途结果 → 记事件 → park → 后续 sweep 回收」。
5. 结论可在源码与单测中断言中直接验证，无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**：为什么 drop 一个在飞的 `DeviceFuture` 不能直接 `drop` 它持有的设备资源（如显存）？

> **答案**：因为内核可能还在写那块内存。流序分配器（`cuMemFreeAsync`）可能把刚释放的地址立刻分给下一次分配，而此时旧内核尚未完成写入，会造成设备端的 use-after-free。所以 cuda-oxide 选择先记一个完成事件、把结果寄存到 limbo，等设备时间线越过该事件、证明内核已完成后才回收。

**练习 2**：如果把默认轮询池大小 \(N\) 从 4 改成 1，对独立操作的重叠会有什么影响？

> **答案**：\(N=1\) 时所有操作都落到同一条流，同流严格 FIFO，独立操作之间无法硬件重叠，退化为串行。轮询的意义正是用多条流让独立工作并行。

---

## 5. 综合实践

把前面 u1-l4 / u2-l4 的同步 `vecadd` 改写成异步版本，参考 [async_vecadd/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/async_vecadd/src/main.rs)。

**任务**：在一个 `#[cuda_module]` 内写好 `vecadd` 内核，在 `main` 里用 `load_async` + `vecadd_async` + `.sync()` 完成计算，并写一段注释回答「内核究竟在哪个时刻真正在 GPU 上执行」。注意 raw 配置启动必须包在 `unsafe` 块里。

**参考骨架**（基于真实示例 [async_vecadd/src/main.rs:26-104](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/async_vecadd/src/main.rs#L26-L104)，标注为「示例代码」）：

```ignore
// 示例代码
use cuda_device::{DisjointSlice, kernel, thread};
use cuda_host::cuda_module;

#[cuda_module]
mod kernels {
    use super::*;
    #[kernel]
    pub fn vecadd(a: &[f32], b: &[f32], mut c: DisjointSlice<f32>) {
        let idx = thread::index_1d();
        let i = idx.get();
        if let Some(c_elem) = c.get_mut(idx) {
            *c_elem = a[i] + b[i];
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    cuda_async::device_context::init_device_contexts(0, 1)?;   // 初始化线程级异步上下文
    let module = kernels::load_async(0)?;                       // 加载内嵌 PTX（未签约模块，安全）

    // ... 分配 a_dev / b_dev / c_dev，把 a_host/b_host 拷到设备 ...

    // SAFETY: launch shape/resources match the kernel; buffers cover its accesses.
    unsafe {
        module.vecadd_async(                                    // ★ 仅构造 AsyncKernelLaunch，未启动
            cuda_core::LaunchConfig::for_num_elems(N as u32),
            &a_dev, &b_dev, &mut c_dev,
        )
    }?
    .sync()?;                                                    // ★★ 这里才挑流、入队、阻塞等待

    // ... 把 c 拷回 host 并校验 ...
    Ok(())
}
```

**进阶改写（签约 + 受检异步，推荐）**：给内核加 `#[launch_contract(domain = 1, block = (256, 1, 1))]`（参见 u2-l1/u2-l4），把启动改成安全路径：

```ignore
// 示例代码
// SAFETY: this program loads the embedded artifact generated from this exact
// contracted module, so its entry points and contract metadata match.
let module = unsafe { kernels::load_async(0)? };
let prepared = module.prepare_vecadd(cuda_core::LaunchConfig1D::new(4, 256, 0))?;
module
    .vecadd_async(&prepared, &a_dev, &b_dev, &mut c_dev)        // ★ 安全：消费已证 PreparedLaunch
    .sync()?;
```

**操作步骤**：

1. `cargo oxide build async_vecadd` 确认能编译（无需 GPU）。
2. 阅读源码，回答：
   - `vecadd_async` 返回了什么？此刻 GPU 上有工作吗？
   - `.sync()` 内部依次做了哪三件事（挑流 / `execute` 入队 / `synchronize`）？
   - 为什么 raw 版的 `vecadd_async` 必须包在 `unsafe` 里，而签约版的 `vecadd_async(&prepared, ...)` 不用？二者各自的「安全证明」分别由谁承担？
   - 如果把 `.sync()?` 换成 `.await?`，内核在哪一步入队？宿主线程在等待期间是否被阻塞？

**预期结论**：

- `vecadd_async` 返回 `AsyncKernelLaunch`（raw 路径）或 `PreparedAsyncKernelLaunch`（签约路径），二者都是 `DeviceOperation`，此刻**没有任何 GPU 工作**。
- raw 路径的 `unsafe` 落在生成方法内部那一句 `finalize_unchecked(cfg)`：调用方用 `SAFETY:` 注释证明「形状/资源/缓冲区与内核匹配」。签约路径则把这份证明前移到 `prepare_vecadd`（活设备一次性校验），所以 `vecadd_async(&prepared, ...)` 是安全方法。
- `.sync()` 依次：默认策略轮询挑流 → `execute()` 调 `cuLaunchKernel` 入队 → `synchronize()` 阻塞到流空闲。**内核真正在 GPU 上执行的时刻，是从入队之后到 `synchronize` 返回之前**。
- 换成 `.await` 后，入队发生在 `DeviceFuture` 第一次 `poll` 的 `Idle` 分支（`execute()`）；宿主线程**不被阻塞**，执行器在 `Pending` 期间可去 poll 别的 future，等 `cuLaunchHostFunc` 回调唤醒后再返回结果。

**运行结果**（需要 GPU）：待本地验证。无 GPU 时，本实践的「阅读 + 注释」部分用 `build` 即可完成。

## 6. 本讲小结

- `DeviceOperation` 是 cuda-async 的核心抽象：**惰性、流无关**，只描述「做什么」，构造时不碰 GPU。
- 四条执行路径区分清晰：`schedule`（包成 future）、`.await`（默认策略 + future）、`sync`/`sync_on`（阻塞到流空闲）、`async_on`（入队不等）。
- #318 把异步启动类型一分为二：安全的惰性 `AsyncKernelLaunchBuilder`（不实现 `DeviceOperation`/`IntoFuture`）与不可变可运行的 `AsyncKernelLaunch`，之间只有一道 `unsafe finalize_unchecked(cfg)` 显式闸门——raw `LaunchConfig` 必须在此跨越安全边界。
- 宏按「是否签约」生成两套 `*_async`：未签约内核的 `vecadd_async(LaunchConfig, ...)` 是 `unsafe fn`；签约内核另有安全的 `vecadd_async(&PreparedLaunch, ...)`（返回 `PreparedAsyncKernelLaunch`，提交前再 `validate_stream`）与 `*_async_unchecked` 逃生口。`KernelArgument` 因此加了 `Send` bound。
- `DeviceFuture` 用三态机 `Idle → Executing → Complete`（加 `Failed`）实现 `Future`；**GPU 工作在第一次 poll 的 `Idle` 分支入队**。raw 与签约两类操作最终都进同一个 `DeviceFuture`，三态机无差别。
- 完成检测靠 `cuLaunchHostFunc` 主机回调置位原子标志 + `AtomicWaker` 唤醒执行器，**绝不忙等**。
- drop 在飞 future 不会取消 GPU 工作，而是把结果连同完成事件寄存到 `reclaim` limbo，由后续 `sweep` 非阻塞回收——保证 drop 永不阻塞 GPU 进度。

## 7. 下一步学习建议

- 下一讲 **u3-l4 调度策略与组合子** 会展开本讲只点到为止的两件事：`SchedulingPolicy`（`StreamPoolRoundRobin` / `SingleStream`）如何挑流，以及 `and_then` / `zip!` 如何在不接触 GPU 的前提下拼出数据流依赖图，并配合 `reclaim` 处理取消。建议先回头精读 [scheduling_policies.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/scheduling_policies.rs) 与 [device_operation.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-async/src/device_operation.rs) 的组合子部分。
- 想看一个把 `and_then`/`zip` 用起来的真实流水线，直接读示例 `async_mlp/src/main.rs`，它是 u3-l4 的实践依据。
- 对 raw `LaunchConfig` 为何不安全、`prepare_*` → `PreparedLaunch` 受检链路如何工作，回看 u2-l4；本讲只是把同一套二分搬到了异步侧（`finalize_unchecked` ↔ `prepare_*`）。
- 对「内嵌 PTX 如何在 `load_async` 时被找回、签约模块 loader 为何 `unsafe`」有疑问，可回看 u3-l2 的 `.oxart` 制品与运行时发现机制。
- 对 Rust 异步底层（`Future`/`Waker`/`AtomicWaker`）还不熟，建议先动手写一个不依赖 CUDA 的最小 `Future`，再回来对照 `DeviceFuture` 的三态机，会顺畅很多。
