# 调度策略与组合子

## 1. 本讲目标

上一讲（u3-l3）我们建立了 `cuda-async` 的异步执行模型：`DeviceOperation` 是**惰性、流无关**的——构造时只描述「做什么」，把「在哪条流上跑」推迟到调度时刻；`DeviceFuture` 用三态机在第一次 poll 时才真正把工作入队。

本讲顺着这条线索回答三个仍悬而未决的问题：

1. **谁来选流、怎么选？** 调度策略（`SchedulingPolicy`）如何在调度时刻为一个操作挑出一条 CUDA 流，从而让独立的工作自动重叠？
2. **多个操作怎么拼成一张依赖图？** `and_then` 与 `zip!` 这些组合子如何在**完全不碰 GPU** 的前提下，把若干操作串成顺序链或打包成元组？
3. **图被提前取消（drop）了怎么办？** 已经入队的 GPU 工作无法回收，`reclaim` 机制如何在不阻塞、不破坏内存安全的前提下「延迟回收」被取消的结果？

学完后你应当能够：

- 说清 `StreamPoolRoundRobin` 与 `SingleStream` 两种策略的选流规则与适用场景；
- 用 `and_then` / `zip!` / `value` / `with_context` 组合出一张设备端数据流图，并能正确判断**哪些部分会并行、哪些部分会串行**；
- 解释 drop 一个在飞 future 时，结果为何被「停尸」到 limbo 而非立即释放，以及谁负责最终清扫；
- 读懂 `async_mlp` 示例，理解一条多核前向流水线（GEMM→MatVec→ReLU→D2H）是如何跨 4 条流并发跑起来的。

## 2. 前置知识

本讲假设你已掌握 u3-l3 的内容。为便于承接，这里用一句话回顾几个关键术语，不展开：

- **CUDA stream（流）**：GPU 上的工作队列。同一条流内严格 FIFO（先入队的先完成）；不同流之间无序，因而可以并行重叠。想让两段独立工作并行，就把它们放到不同流里。
- **`DeviceOperation`**：描述一段 GPU 工作的惰性 trait，构造时不绑定任何流、不产生副作用；只有在拿到 `ExecutionContext`（内含一条具体流）后调用 `execute` 才真正入队。
- **`DeviceFuture`**：把 `DeviceOperation` 绑定到一条流后的句柄，实现 Rust `Future`；第一次 poll 时执行操作并注册 `cuLaunchHostFunc` 回调，回调置位后唤醒执行器，绝不忙等。
- **惰性求值（lazy evaluation）**：先「搭图」，后「执行」。搭图阶段只是组合结构体，真正提交给 GPU 发生在未来某个 poll 时刻。

如果你还记得一句话——**「drop 一个在飞 future 不会取消 GPU 工作，内核一定跑完」**——那么本讲的 reclaim 模块正是为这句话善后。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [crates/cuda-async/src/scheduling_policies.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/scheduling_policies.rs) | 定义 `SchedulingPolicy` trait 与两种实现 `StreamPoolRoundRobin`、`SingleStream`，以及顶层枚举 `GlobalSchedulingPolicy`。本讲 4.1 的主角。 |
| [crates/cuda-async/src/device_operation.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_operation.rs) | `DeviceOperation` trait 与全部组合子：`and_then`、`AndThen`、`Zip`、`zip!`、`value`、`with_context`、`arc` 等。本讲 4.2 的主角。 |
| [crates/cuda-async/src/reclaim.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/reclaim.rs) | 延迟回收机制：`ReclaimGate`、`park`、`sweep`、`drain` 与全局 limbo。本讲 4.3 的主角。 |
| [crates/cuda-async/src/device_future.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_future.rs) | `DeviceFuture` 的状态机、poll 与 Drop。本讲用它说明 reclaim 是如何被「挂」进 future 生命周期的。 |
| [crates/cuda-async/src/device_context.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_context.rs) | 线程本地设备状态：默认策略（4 流轮询）的安装、`with_default_device_policy` 入口。 |
| [crates/cuda-async/src/launch.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/launch.rs) | `AsyncKernelLaunch` 与 `OwnedAsyncKernelLaunch`：把一次内核启动实现成 `DeviceOperation`，是 `async_mlp` 里每个阶段的载体。 |
| [crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs) | 端到端示例：4 条流并发跑 4 个 batch 的多层感知机前向。本讲 4.4 的主角。 |

## 4. 核心概念与源码讲解

### 4.1 SchedulingPolicy：把「用哪条流」推迟到调度时刻

#### 4.1.1 概念说明

回顾 u3-l3：`DeviceOperation` 故意「不带流」。那么当一个操作真的要跑时，谁来决定它落到哪条流？这正是**调度策略（scheduling policy）**的职责。

调度策略是一个把「操作的构造」与「流的选择」解耦的抽象：

- 你写代码时只关心**做什么**（`h2d(data)`、`module.gemm_async(...)`）；
- 策略在**调度时刻**才决定**在哪条流上做**，从而对用户**透明地**实现独立工作的硬件级重叠。

cuda-oxide 提供两种策略：

| 策略 | 行为 | 适用场景 |
|------|------|----------|
| `StreamPoolRoundRobin` | 在一个 N 条流的池子里轮询（默认 N=4） | 生产：让独立内核/搬运自动跨流重叠 |
| `SingleStream` | 把所有工作串到同一条流上 | 调试、或需要全局严格顺序时 |

#### 4.1.2 核心流程

`SchedulingPolicy` 是一个必须 `Sync` 的 trait（因为同一设备的所有操作共享它），只有三个方法：

```text
init(&mut self, ctx)        // 一次性：在 ctx 上创建流池
schedule(op)  -> DeviceFuture   // 为 op 选一条流，包成 future（仍惰性，未执行）
sync(op)      -> T              // 为 op 选一条流，执行 + 同步阻塞到流空闲，返回结果
```

`StreamPoolRoundRobin` 的选流逻辑极其简单——一个原子计数器轮询：

```text
idx = next_stream_idx.fetch_add(1) % num_streams
```

注意一个容易踩的点：**计数器在 `schedule` 与 `sync` 两条路径上都会自增**。也就是说，哪怕你只是 `.sync()` 一个操作，它也会「消耗」一个流槽位。因此连续两次 `.sync()` 会落到不同流上——它们之间因此没有自动的顺序保证（除非你显式用 event 建序）。

策略初始化后会被装进 `Arc<GlobalSchedulingPolicy>` 存到线程本地设备上下文里（见 [device_context.rs:164-175](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_context.rs#L164-L175) 的 `init_with_default_policy`，默认装的就是 4 流轮询）。所有 `IntoFuture` 实现都经 `with_default_device_policy` 取到这个 `Arc`，再调 `policy.schedule(self)`。换言之，你写的 `.await` 用的就是默认设备的默认策略。

#### 4.1.3 源码精读

trait 定义本身只有一个约束（`Sync`）和三个方法（[scheduling_policies.rs:96-110](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/scheduling_policies.rs#L96-L110)）：

```rust
pub trait SchedulingPolicy: Sync {
    fn init(&mut self, ctx: &Arc<CudaContext>) -> Result<(), DeviceError>;
    fn schedule<T: Send, O: DeviceOperation<Output = T>>(
        &self, op: O,
    ) -> Result<DeviceFuture<T, O>, DeviceError>;
    fn sync<T: Send, O: DeviceOperation<Output = T>>(&self, op: O) -> Result<T, DeviceError>;
}
```

轮询策略的 `schedule`（[scheduling_policies.rs:174-197](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/scheduling_policies.rs#L174-L197)）：原子取一个下标，取出对应流，构造一个**已绑定该流、但尚未执行**的 `DeviceFuture`：

```rust
let idx = self.next_stream_idx
    .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    % self.num_streams;
let pool = self.stream_pool.as_ref()
    .ok_or_else(|| device_error(self.device_id, "Stream pool not initialized."))?;
Ok(DeviceFuture {
    device_operation: Some(op),
    execution_context: Some(ExecutionContext::new(Arc::clone(&pool[idx]))),
    result: None, error: None,
    state: Default::default(), callback_state: None,
})
```

这段里有一处值得注意的工程细节：构造 `DeviceFuture` 时**逐字段写出**而非用 `..Default::default()`。源码注释解释了原因——`DeviceFuture` 实现了 `Drop`，对「可 drop 的值」做部分移动（functional-update 语法）会触发编译器错误 E0509。这是 Rust 所有权系统对「带析构的类型」的一道保护。

`sync`（[scheduling_policies.rs:160-170](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/scheduling_policies.rs#L160-L170)）同样取下标，但随后调用 `op.sync_on(&pool[idx])`——即「在该流上执行 + 同步阻塞」。所以 `.sync()` 是 `schedule + execute + synchronize` 的快捷方式。

`SingleStream`（[scheduling_policies.rs:221-251](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/scheduling_policies.rs#L221-L251)）只有一条流，所有操作都绑定到它，因此天然全局串行——适合在出问题时关掉并行以定位时序 bug。

> 关于枚举包装：顶层用 `GlobalSchedulingPolicy` 枚举（[scheduling_policies.rs:31-65](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/scheduling_policies.rs#L31-L65)）把策略包成单个具体类型存进设备上下文。注意它的 `Arc` 包装实现里，`init` 直接返回错误（[scheduling_policies.rs:69-74](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/scheduling_policies.rs#L69-L74)）——因为一旦进了 `Arc` 就无法再 `&mut`，所以「先 init 再 Arc」是不可逆的顺序。

#### 4.1.4 代码实践

**实践目标**：理解轮询选流，预测一串操作分别落到哪条流。

**操作步骤（源码阅读型）**：

1. 打开 [scheduling_policies.rs:174-197](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/scheduling_policies.rs#L174-L197)，确认 `fetch_add` 用的是 `Ordering::Relaxed`。思考：为什么这里用 `Relaxed` 就够了？（提示：流池本身是不可变的，计数器只用来选下标，不需要与其它内存建立 happens-before。）
2. 假设默认池大小 `num_streams = 4`、初始 `next_stream_idx = 0`。写下连续 6 次 `schedule` 调用各自取到的 `idx`。
3. 打开 [device_context.rs:37](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_context.rs#L37)，确认默认池大小常量 `DEFAULT_ROUND_ROBIN_STREAM_POOL_SIZE`。

**需要观察的现象 / 预期结果**：

- 第 1～6 次调用的 `idx` 序列应为 `0,1,2,3,0,1`（对 4 取模）。
- 因为 `fetch_add` 在 `schedule` 和 `sync` 上都自增，所以即便只调用 `.sync()`，也会推进计数器——两次 `.sync()` 不会落在同一条流上。

> 运行型验证（**待本地验证**，需 GPU 与工具链）：写一个最小 async 程序连续 `.await` 6 个独立 `value(())` 操作，在每次 `with_context` 里打印 `ctx.get_cuda_stream()` 的地址（或流序号），核对是否符合上面的轮询预测。注意打印需要在执行时刻拿流，因此要放在 `with_context` 闭包内，而非构造时。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SchedulingPolicy` 必须是 `Sync`？

**参考答案**：策略被装进 `Arc<GlobalSchedulingPolicy>` 存在线程本地设备上下文，同一设备上的所有操作（可能来自不同 tokio 任务、不同线程的 poll）都共享这同一个 `Arc`。要在多线程间共享 `&` 引用并并发调用 `schedule`/`sync`，类型必须 `Sync`。轮询实现用 `AtomicUsize` 选流，正是为了在无锁前提下满足这一要求。

**练习 2**：默认池大小是多少？在哪里设定？

**参考答案**：默认 4 条流，由 [device_context.rs:37](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_context.rs#L37) 的 `DEFAULT_ROUND_ROBIN_STREAM_POOL_SIZE` 设定，在 `init_with_default_policy`（[device_context.rs:164-175](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_context.rs#L164-L175)）里传给 `StreamPoolRoundRobin::new`。

---

### 4.2 and_then / zip!：不碰 GPU 的依赖图组合子

#### 4.2.1 概念说明

组合子（combinator）是函数式编程里的经典手段：用几个小积木拼出复杂结构。在 cuda-oxide 里，组合子把若干 `DeviceOperation` 拼成一张**数据流图**，而这张图在**构造阶段完全不碰 GPU**——它只是把操作和闭包塞进一个个结构体里，真正的入队要等到未来某次 poll 时，整张图在 `execute` 里被递归展开。

核心组合子有三个：

| 组合子 | 语义 | 关键性质 |
|--------|------|----------|
| `and_then(f)` | 顺序依赖：先跑 `self`，把结果喂给 `f` 得到下一个操作再跑 | 两段跑在**同一条流**上，靠流的 FIFO 自动保序 |
| `zip!(a, b[, c])` | 把 2/3 个操作打包成一个返回元组的操作 | **不是并行**！内部按序在同一流上执行，仅为「凑元组」 |
| `value(x)` | 把一个宿主值提升为「无副作用」的操作 | 用于在图里透传数据、重打包元组 |

这里有一个**最容易被误解**的点，必须先讲清楚：

> **`zip!` 不会让两个操作并行。** 它的 `execute` 是「先跑 a，再跑 b，返回 `(a, b)`」，两者用同一个 `ExecutionContext`（同一条流）。真正的跨流并行，来自**顶层**把多个独立操作各自调度到不同流（典型做法是 `tokio::spawn` 多个任务，每个任务各自 `into_future` 一次，轮询策略自然把它们分到不同流）。

换句话说：

- **图内**的顺序依赖用 `and_then`（同流、自动保序）；
- **图内**的「凑一组」用 `zip!`（同流、顺序执行）；
- **图间**的并行靠「多个顶层操作 + 轮询策略分流」。

#### 4.2.2 核心流程

`and_then` 构造 `AndThen { op, closure }`。执行时：

```text
execute(context):
    input  = op.execute(context)        # 第一段，用 context 的流
    next   = closure(input)             # 闭包产出下一个操作
    return next.execute(context)        # 第二段，仍用同一个 context（同一条流！）
```

关键就在最后一行：第二段复用**同一个** `context`，所以两段绑在同一条流上。CUDA 流的 FIFO 语义保证了「第一段全部完成后第二段才开始」——你**不需要**手动插 event 或同步。

`zip!` 展开成 `Zip { a, b }`，执行时：

```text
execute(context):
    a = a.execute(context)              # 先 a
    b = b.execute(context)              # 再 b，同一个流
    return (a, b)
```

3 元组的 `zip!(a,b,c)` 是用两层二元 `Zip` 嵌套再 `and_then` 重打包实现的（[device_operation.rs:621-635](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_operation.rs#L621-L635)），所以三个操作同样按序跑在同一流上。

#### 4.2.3 源码精读

`and_then` 方法本身只是打包（[device_operation.rs:166-178](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_operation.rs#L166-L178)）：

```rust
fn and_then<O: Send, DO, F>(self, f: F) -> AndThen<...>
where DO: DeviceOperation<Output = O>, F: FnOnce(Self::Output) -> DO {
    AndThen { op: self, closure: f }
}
```

真正干活的是 `AndThen::execute`（[device_operation.rs:339-345](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_operation.rs#L339-L345)）——注意两段共用同一个 `context`：

```rust
unsafe fn execute(self, context: &ExecutionContext) -> Result<O, DeviceError> {
    unsafe {
        let input = self.op.execute(context)?;
        let output_op = (self.closure)(input);
        output_op.execute(context)   // ← 同一个 context，同一条流
    }
}
```

`Zip::execute`（[device_operation.rs:571-577](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_operation.rs#L571-L577)）同理，a、b 共用 `context`：

```rust
unsafe fn execute(self, context: &ExecutionContext) -> Result<(T1, T2), DeviceError> {
    unsafe {
        let a = self.a.execute(context)?;
        let b = self.b.execute(context)?;
        Ok((a, b))
    }
}
```

`zip!` 宏（[device_operation.rs:644-655](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_operation.rs#L644-L655)）只是把参数组成元组再调 `Zippable::zip`：

```rust
macro_rules! zip {
    ($a:expr) => { $a };
    ($a:expr, $b:expr) => { ($a, $b).zip() };
    ($a:expr, $b:expr, $c:expr) => { ($a, $b, $c).zip() };
}
```

`value`（[device_operation.rs:439-462](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_operation.rs#L439-L462)）把任意 `Send` 值包成 `Value`，其 `execute` 直接返回该值、**不做任何 GPU 工作**。它在图里充当「胶水」：当你需要在 `and_then` 链里重组元组、或透传宿主数据（如 `module` 句柄）时，用 `value(...)` 把它们重新装回一个操作。

还有一个对 4.4 很关键的组合子 `with_context`（[device_operation.rs:691-699](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_operation.rs#L691-L699)）：它把一个「接收 `ExecutionContext`、返回操作」的闭包包成操作。为什么需要它？因为像「分配显存 + H2D 拷贝」这类操作在**构造时还不知道流**——`malloc_async`/`memcpy_*_async` 都需要流参数。`with_context` 让你在**执行时刻**（此时策略已选好流）才拿到流，从而正确地发起流序异步分配与拷贝。

#### 4.2.4 代码实践

**实践目标**：用 `and_then` 串联两个内核（scale→relu），用 `zip!` 并行两个独立启动，画出依赖图，亲眼看「图内串行、图间并行」的差别。

**操作步骤**：

1. 以 vecadd 或 async_vecadd 示例为模板，定义两个 `#[kernel]`：`scale(x: f32, mut data: DisjointSlice<f32>)`（逐元素乘标量）与 `relu(mut data: DisjointSlice<f32>)`（逐元素 `max(0, x)`，可照搬 `async_mlp` 里的 `relu`，见 [async_mlp/src/main.rs:117-124](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs#L117-L124)）。
2. 写一段「顺序链」：先分配并 H2D（参考 4.4 的 `h2d`/`with_context`），`.and_then` 启动 `scale`，再 `.and_then` 启动 `relu`，最后 `.and_then(d2h)`。把整条链 `.await`。
3. 写一段「打包」：`zip!(h2d(a), h2d(b))` 同时分配两块独立缓冲，`.await` 拿到 `(buf_a, buf_b)`。
4. 写一段「真并行」：构造两个**完全独立**的链 `chain_a` 与 `chain_b`，用 `let ha = tokio::spawn(chain_a.into_future()); let hb = tokio::spawn(chain_b.into_future());` 各自 spawn，再 `ha.await?; hb.await?;`。
5. 为每一段画依赖图（节点=操作，边=数据依赖），标注每条边落在哪条流上。

**需要观察的现象 / 预期结果**：

- 步骤 2 的链：整条链所有阶段落在**同一条流**上，按 FIFO 顺序执行——这就是 `and_then` 的「自动保序」。
- 步骤 3 的 `zip!`：两次 `h2d` 落在**同一条流**上顺序执行（不是并行！），只是结果凑成一个元组。
- 步骤 4 的两个 spawn：`into_future` 各自被轮询策略分到**不同流**（如流 0 与流 1），于是两条链**真正并行**重叠——这才是跨流并行的来源。

> 运行结果**待本地验证**（需 GPU）。若暂无 GPU，可降级为源码阅读型实践：对照本节的 `execute` 源码，逐行解释为何步骤 3 的两次 `h2d` 必然串行、而步骤 4 的两条链必然落到不同流。

#### 4.2.5 小练习与答案

**练习 1**：`zip!(op_a, op_b)` 里 `op_a` 和 `op_b` 谁先执行？它们会并行吗？

**参考答案**：`op_a` 先、`op_b` 后（见 [device_operation.rs:571-577](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_operation.rs#L571-L577)）。它们**不会**并行——两者共用同一个 `ExecutionContext`（同一条流），按序执行。`zip!` 只负责把结果凑成元组，并行性必须靠「多个顶层操作被策略分流」获得。

**练习 2**：为什么 `and_then` 的两段不需要手动插入 `sync_threads` 或 event 来保序？

**参考答案**：因为 `AndThen::execute` 把两段都绑定到**同一条流**（同一个 `context`），而 CUDA 流保证同流内严格 FIFO——第一段全部完成后第二段才会开始。流的顺序语义已经替你做了保序，无需额外同步原语。

**练习 3**：`value(x)` 在依赖图里有什么用？

**参考答案**：`value` 是「无副作用」操作，`execute` 直接返回包着的值。它常用于在 `and_then` 链里**重组元组**或**透传宿主数据**（如 `module` 句柄、`Arc` 权重）：闭包接收上一阶段结果后，用 `value((a, b, module))` 把多个值重新打包成一个操作传给下一阶段。`async_mlp` 里大量用到这个技巧。

---

### 4.3 reclaim：取消在飞 future 的延迟回收

#### 4.3.1 概念说明

这是本讲最「硬核」也最体现工程功底的模块。问题来自一个无法回避的物理事实：

> **GPU 工作一旦入队就无法取消——内核一定跑完，无论宿主做什么。**

那么，如果你 `drop` 了一个 future，而它的内核还在 GPU 上飞，怎么办？关键是搞清楚「drop 能决定什么、不能决定什么」：

- **不能决定**：让内核停下（它停不了）。
- **能决定**：宿主**何时释放**内核还在使用的资源（比如设备缓冲）。

直接 drop 那些资源有两个坑：

1. **use-after-free**：流序分配器（stream-ordered allocator）可能在你 drop 的瞬间就把这块显存分给下一次分配，而旧内核还在往里写。
2. **阻塞执行器**：在 `Drop` 里同步流（`synchronize`）会阻塞执行器线程，时长不可控，违背异步模型。

cuda-oxide 的解法是**延迟回收（deferred reclamation）**：drop 时在流上记录一个 CUDA event（排在已提交工作之后），把 `(event, 结果)` 一起**停尸**到一个全局「limbo」；后续的清扫用**非阻塞**的 event 查询判断 GPU 是否已越过该点，越过了才真正 drop 结果。

```text
drop(future)                    后来的 sweep（poll / drop / drain）
  ├─ 在流上 record event          ├─ event 已过？ → drop 结果
  └─ 把 (event, 结果) 停尸          └─ 仍在飞？   → 继续停尸
```

#### 4.3.2 核心流程

reclaim 模块围绕一个 trait 与三个函数组织：

```text
ReclaimGate            # 完成闸门：passed() 非阻塞查询、wait() 阻塞等待
park(gate, payload)    # 停尸：把 (闸门, 结果) 推入全局 LIMBO
sweep()                # 非阻塞清扫：drop 所有「闸门已过」的结果；绝不阻塞 GPU
drain()                # 阻塞排空：等所有闸门过后再 drop；用于确定性回收（测试/退出）
```

几条**不可破坏的契约**（源码注释反复强调）：

- 结果**绝不**在 CUDA host callback 里 drop（回调里不能调 CUDA API，而 drop 一个设备缓冲会入队一次 async free）；
- 一次 sweep **绝不**阻塞 GPU 进度；
- 进程退出时仍停尸的条目**直接泄漏**——驱动在 teardown 时回收显存，提早 drop 可能破坏仍在使用的内存（泄漏比破坏安全）；
- 当连阻塞等待都失败时（驱动无法证明 GPU 工作完成），结果被**故意泄漏**并往 stderr 打一条信息。

为什么 `DeviceOperation::Output` 要求 `'static`？答案就在这里：被取消的在飞结果会被停尸到 limbo，在 GPU 完成（可能很久）之后才 drop，此时任何非 `'static` 借用早已失效。所以 Output 必须 `'static`（[device_operation.rs:142](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_operation.rs#L142) 的注释正是这么写的）。

#### 4.3.3 源码精读

`ReclaimGate` trait（[reclaim.rs:48-55](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/reclaim.rs#L48-L55)）抽象「设备时间线是否越过某点」。生产实现是 `CudaEvent`（`passed` = `query`、`wait` = `synchronize`），测试用宿主 mock：

```rust
pub(crate) trait ReclaimGate: Send {
    fn passed(&self) -> Result<bool, DriverError>;   // 非阻塞
    fn wait(&self) -> Result<(), DriverError>;       // 阻塞
}
```

全局停尸房用一把 `Mutex<Vec<LimboEntry>>` + 一个无锁计数器做快速路径（[reclaim.rs:77-80](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/reclaim.rs#L77-L80)）：

```rust
static PENDING: AtomicUsize = AtomicUsize::new(0);
static LIMBO: Mutex<Vec<LimboEntry>> = Mutex::new(Vec::new());
```

`park`（[reclaim.rs:94-101](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/reclaim.rs#L94-L101)）：加锁、推入、更新计数（`Release` 序，让其它线程的 `Acquire` 读到一致值）。

`sweep`（[reclaim.rs:115-140](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/reclaim.rs#L115-L140)）是核心。它先做一次无锁快速判断（计数为 0 直接返回），否则加锁遍历，**对每个闸门做非阻塞查询**：

```rust
for entry in entries.drain(..) {
    match entry.gate.passed() {
        Ok(true) => done.push(entry),       // 已完成，待 drop
        Ok(false) | Err(_) => kept.push(entry), // 仍在飞或查询失败 → 继续停尸
    }
}
```

注意两点：① 查询失败（`Err`）也按「仍在飞」处理——宁可多停一会儿也不冒险提早释放；② 真正的 `drop(completed)` 发生在**锁外**（[reclaim.rs:136-139](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/reclaim.rs#L136-L139)），因为一个 payload 的 drop 自己可能又触发 park/sweep，避免持锁重入。

`drain`（[reclaim.rs:153-183](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/reclaim.rs#L153-L183)）是阻塞版：等所有闸门过后再 drop。连等待都失败时，`mem::forget(entry.payload)` 故意泄漏并打 stderr——「无法证明 GPU 完成，泄漏比释放安全」。

**与 DeviceFuture 的衔接**在两处：

1. `DeviceFuture::poll` 顶部先 `reclaim::sweep()`（[device_future.rs:298](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_future.rs#L298)）——机会式回收历史取消的结果，nothing parked 时只花一次原子读。
2. `DeviceFuture::drop` 先 `reclaim::sweep()` 再 `reclaim_in_flight_result()`（[device_future.rs:276-283](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_future.rs#L276-L283)）。

`reclaim_in_flight_result`（[device_future.rs:194-219](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_future.rs#L194-L219)）判断 `has_undelivered_submission()`（状态为 `Executing`/`Complete` 且 `result.is_some()`），若是则在流上 record event 并 park；若 event 都记录失败，退化为阻塞 `cleanup_executing_result_with`，再失败就「响亮地泄漏」。

#### 4.3.4 代码实践

**实践目标**：通过阅读测试理解 reclaim 的「停尸—清扫」时序，而不是猜。

**操作步骤（源码阅读型）**：

1. 打开 [reclaim.rs:234-262](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/reclaim.rs#L234-L262) 的测试 `sweep_keeps_payload_parked_until_gate_passes`。它用一个 `MockGate`（`passed` 由一个 `AtomicBool` 模拟）和一个会计数 drop 的 `CountDrop`。
2. 跟着测试走一遍：先 `park` 一个闸门未过的条目，`sweep()`，断言 drop 计数仍为 0；再把闸门置为「已过」，再 `sweep()`，断言 drop 计数变为 1。
3. 再读 [reclaim.rs:287-307](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/reclaim.rs#L287-L307) 的 `drain_leaks_payload_when_wait_fails`：当 `wait_fails = true`（闸门永远无法证明完成）时，断言 drop 计数始终为 0——即故意泄漏。

**需要观察的现象 / 预期结果**：

- 闸门未过 → 结果保持停尸，绝不被 drop；
- 闸门已过 → 结果被 drop；
- 闸门「无法证明完成」→ 结果被泄漏而非 drop（安全优先）。

> 这些是纯逻辑单元测试，**可在本机用 `cargo test -p cuda-async reclaim` 运行验证**（不依赖 GPU，因为用的是 `MockGate`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sweep` 里 payload 的 drop 要放在锁外？

**参考答案**：因为一个 payload（比如 `DeviceBox`/设备缓冲）的 drop 可能再次触发 `park` 或 `sweep`（例如它内部的清理又停尸了别的资源）。如果在持锁状态下 drop，就会重入同一把 `Mutex` 导致死锁或破坏不变量。把 `drop(completed)` 移到锁外（[reclaim.rs:136-139](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/reclaim.rs#L136-L139)）规避了重入。

**练习 2**：进程退出时仍有条目停尸，为什么选择泄漏而不是 drop？

**参考答案**：泄漏的显存会在进程 teardown 时由驱动统一回收，是安全的；而提早 drop 可能把「内核仍在写」的缓冲释放掉，造成内存破坏。在「泄漏」与「破坏」之间，泄漏是更安全的选择（见 [reclaim.rs:30-34](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/reclaim.rs#L30-L34) 的模块注释）。

---

### 4.4 async_mlp：把一切串成一条并发流水线

#### 4.4.1 概念说明

`async_mlp` 示例把前面三个模块全部用上，演示一条**多层感知机（MLP）前向流水线**：对每个 batch 执行 `GEMM → MatVec → ReLU → D2H`，4 个 batch 在 4 条流上并发。它展示的 async 模式（见文件头注释 [async_mlp/src/main.rs:14-23](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs#L14-L23)）包括 `with_context`、`and_then`、`zip!`、`.arc()`、`tokio::spawn`、`.await`、`value()`——正好对应本讲全部组合子。

#### 4.4.2 核心流程

整体结构分三层：

```text
① 权重分配（一次）：  zip!(h2d(w0).arc(), h2d(w1))  → (Arc<W0>, Arc<W1>)
② 每个 batch 的图（搭图，不执行）：
     zip!(h2d(input), zeros(hidden), zeros(output))   # 3 缓冲，同流顺序分配
       .and_then( 启动 GEMM )                          # hidden = input @ W0
       .and_then( 启动 MatVec )                        # output = hidden @ W1
       .and_then( 启动 ReLU )                          # output = max(0, output)
       .and_then( d2h )                                # 拷回宿主
③ 并发执行：  每个 batch 用 tokio::spawn(pipeline.into_future())，
              轮询策略把 4 个 batch 分到 4 条流。
```

三个反复出现的关键技巧：

1. **`with_context` 让分配/搬运拿到流**：`h2d`/`zeros`/`d2h` 都用 `device_operation::with_context(move |ctx| { let stream = ctx.get_cuda_stream(); ... })`（[async_mlp/src/main.rs:134-145](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs#L134-L145)）。因为 `malloc_async`/`memcpy_*_async` 是流序的，必须在执行时刻拿到流。
2. **`*_async_owned` 启动并「持有」资源**：每个阶段调用 `module.sgemm_naive_async_owned(...)` 之类，返回一个 `OwnedAsyncKernelLaunch<R>`，其 `Output = R`（设备缓冲等资源）。`execute` 先启动内核、再把资源原样返回（[launch.rs:390-398](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/launch.rs#L390-L398)），这样资源就能在阶段间流转、并保证内核运行期间不被释放。
3. **`value(...)` 在阶段边界重打包**：每个 `and_then` 闭包接收上一阶段结果，启动本阶段内核后，用 `value((hidden, output, w1, module))` 把「下一阶段需要的全部东西」重新装成一个操作传下去（[async_mlp/src/main.rs:261-302](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs#L261-L302)）。

#### 4.4.3 源码精读

权重分配用 `zip!` 打包 + `.arc()` 共享（[async_mlp/src/main.rs:207-208](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs#L207-L208)）：

```rust
let (w0, w1): (Arc<DeviceBox<[f32]>>, Arc<DeviceBox<[f32]>>) =
    zip!(h2d(w0_host).arc(), h2d(w1_host).arc()).await?;
```

`.arc()`（[device_operation.rs:198-203](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_operation.rs#L198-L203)）把输出包成 `Arc<T>`，于是 4 个 batch 共享同一份权重（只读），后续每个 batch 闭包里 `w0.clone()`/`w1.clone()` 只增加引用计数。

每个 batch 的流水线是一条 `and_then` 长链（[async_mlp/src/main.rs:259-302](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs#L259-L302)）。以 GEMM 阶段为例：

```rust
.and_then(move |(input, hidden, output): (DeviceBox<[f32]>, ...)| {
    let launch = module.sgemm_naive_async_owned(gemm_cfg, ..., input, w0, 0.0, hidden)?;
    launch.and_then(move |(_input, _w0, hidden)| value((hidden, output, w1, module)))
})
```

内核启动本身又被 `.and_then` 链接——`launch`（`OwnedAsyncKernelLaunch`）的输出是它持有的资源元组，再用 `value(...)` 重组成下一阶段所需形态。这正是 4.2 讲的「`and_then` 同流保序 + `value` 重打包」的实战。

并发的来源在主循环里（[async_mlp/src/main.rs:228-307](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs#L228-L307)）：

```rust
for batch_idx in 0..num_batches {
    // ... 构建 pipeline（此时未执行）...
    handles.push(tokio::spawn(pipeline.into_future()));
}
```

`pipeline.into_future()` 在循环内、spawn 之前就被求值——这正是 4.1 讲的「调度时刻」：轮询策略此刻为该 batch 选定一条流（batch i → 流 i%4）。随后 `tokio::spawn` 把 future 丢给 runtime 并发 poll；每个 future 第一次 poll 时，整条 `and_then` 链在其绑定的流上展开执行。于是：

- **batch 内**：GEMM→MatVec→ReLU→D2H 全在同一流，FIFO 自动保序；
- **batch 间**：4 个 batch 落在 4 条流，真正硬件并行重叠。

最后 `handle.await?` 收集结果并校验 ReLU（所有元素非负，[async_mlp/src/main.rs:314-328](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs#L314-L328)）。

#### 4.4.4 代码实践

**实践目标**：跑通 `async_mlp` 并定位「分流」与「保序」各自的证据。

**操作步骤**：

1. 阅读 [async_mlp/src/main.rs:237-245](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs#L237-L245) 的注释，确认「Nothing executes until tokio::spawn polls the future」。
2.（**待本地验证**，需 GPU）`cargo oxide run async_mlp`，观察 4 个 batch 的输出与 `[ReLU OK]` 标记。
3. 用 `cargo oxide pipeline async_mlp`（无需 GPU）确认它能编译通过，并浏览产出的中间 IR/PTX，体会「4 个 batch 共享同一份 PTX、仅运行时复用」。
4. 在 `h2d` 的 `with_context` 闭包里临时加一行 `eprintln!("stream {:p}", stream.cu_stream());`，重跑，核对 4 个 batch 是否打印出 4 个不同的流地址（验证分流）。

**需要观察的现象 / 预期结果**：

- 4 个 batch 各自打印一个 ReLU 后的非负向量，全部带 `[ReLU OK]`；
- 步骤 4 应看到 4 个**不同**的流地址（轮询分流）；而单个 batch 内多次 `with_context`（h2d→各阶段）应复用**同一个**流地址（同流保序）。

> 若无 GPU，步骤 2/4 标注为「待本地验证」；步骤 1/3 为可离线完成的源码阅读与编译验证。

#### 4.4.5 小练习与答案

**练习 1**：`async_mlp` 里权重 W0/W1 为什么用 `.arc()` + `Arc`，而不是每个 batch 各拷一份？

**参考答案**：权重对所有 batch 是只读共享的。`.arc()` 把分配结果包成 `Arc<DeviceBox<[f32]>>`，4 个 batch 各自 `clone()` 只增加引用计数，不会重复分配/拷贝大块显存。这既省显存又省带宽。

**练习 2**：如果把主循环里的 `tokio::spawn(pipeline.into_future())` 改成「先串行 `pipeline.await` 再下一个 batch」，并发会怎样变化？

**参考答案**：那样 4 个 batch 会**顺序**执行——虽然轮询策略仍会给每个 batch 分不同流，但由于你 `.await` 阻塞等当前 batch 完成才进入下一个，流之间无法重叠。这正是「图间并行靠 spawn 多任务」的体现：去掉 spawn，并行性就消失了。

---

## 5. 综合实践

把 4.2 的实践任务做成一个完整可运行示例，串起本讲全部知识点。

**任务**：写一个最小的 async 内核图，包含一段 `and_then` 顺序链、一段 `zip!` 打包、以及一段真正的跨流并行，并画出完整依赖图。

**建议骨架**（基于 vecadd/async_vecadd/async_mlp 改造）：

1. 定义 `#[kernel] fn scale(factor: f32, mut x: DisjointSlice<f32>)` 与 `#[kernel] fn relu(mut x: DisjointSlice<f32>)`（relu 可照搬 [async_mlp/src/main.rs:117-124](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/rustc-codegen-cuda/examples/async_mlp/src/main.rs#L117-L124)）。
2. 复用 `async_mlp` 里的 `h2d`/`zeros`/`d2h` 三个 `with_context` 辅助函数。
3. **顺序链**：`h2d(data).and_then(scale启动).and_then(relu启动).and_then(d2h).await`，验证结果为 `max(0, factor*data)`。整条链应在**同一条流**上。
4. **打包**：`zip!(h2d(a), h2d(b)).await` 得到 `(buf_a, buf_b)`——确认两次 h2d 顺序同流执行（不是并行）。
5. **真并行**：构造两条独立的 `chain_a`、`chain_b`（各自一条步骤 3 那样的链），分别 `tokio::spawn(...into_future())`，再 `join`。确认两条链落到**不同流**并行。

**交付物**：

- 一张依赖图：用方框表示操作（h2d / scale / relu / d2h），用箭头表示数据依赖；用不同颜色标注每条边所属的流（同流同色）。
- 一段说明：指明图中哪些边是「同流顺序」（`and_then`/`zip!` 带来的），哪些是「跨流并行」（多 spawn + 轮询策略带来的）。

**验收**：

- 顺序链结果正确；
- 依赖图能准确预测每段落在哪条流（可在 `with_context` 里打印流地址核对，**待本地验证**）；
- 能口头解释：为何「去掉 `tokio::spawn` 改成串行 await」会让并行消失。

## 6. 本讲小结

- **调度策略**把「用哪条流」从操作构造中解耦：`StreamPoolRoundRobin` 用一个原子计数器在 N 条流（默认 4）上轮询，`SingleStream` 把所有工作串到一条流；选流发生在 `schedule`/`sync` 时刻，且两种调用都会推进计数器。
- **组合子在构造期不碰 GPU**：`and_then` 打包成 `AndThen` 结构体，`zip!` 打包成 `Zip`；真正的入队发生在 `execute` 里，且**整条链复用同一个 `ExecutionContext`（同一条流）**，靠流 FIFO 自动保序。
- **`zip!` 不是并行**：它的 `execute` 是「先 a 后 b，同流顺序」，只负责把结果凑成元组；真正的跨流并行来自「多个顶层操作各自被策略分流」（典型如 `tokio::spawn` 多任务）。
- **`value` 与 `with_context`** 是两个关键胶水：前者把宿主值/重打包元组提升为无副作用操作，后者让「需要流才能构造」的操作（流序分配/搬运）推迟到执行时刻拿流。
- **reclaim 解决「取消在飞 future」的善后**：drop 时在流上记录 event 并把结果停尸到全局 limbo；后续 `sweep` 用非阻塞查询决定是否 drop，绝不阻塞 GPU、绝不在回调里 drop、进程退出时宁可泄漏也不破坏内存。`Output: 'static` 正是为这条延迟 drop 路径而设。
- **`async_mlp`** 把以上全部串起来：`zip!`+`.arc()` 分配共享权重，`and_then`+`value`+`*_async_owned` 构建 batch 内同流保序的多核链，`tokio::spawn(pipeline.into_future())` 让 4 个 batch 跨 4 条流并行。

## 7. 下一步学习建议

- **想看 `*_async` 启动方法如何被宏生成**：进入 U2 的 `#[cuda_module]` 宏（u2-l1），重点看它如何为每个 `#[kernel]` 生成 `xxx_async` / `xxx_async_owned` 方法，这些方法正是 `async_mlp` 里 `sgemm_naive_async_owned` 等的来源。
- **想深入 DeviceFuture 的状态机与回调唤醒**：重读 [device_future.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/device_future.rs) 的 `Future` 实现，对照 u3-l3 的三态机说明，弄清 `Idle→Executing→Complete` 各分支与 `cuLaunchHostFunc` 回调的交互。
- **想理解 `OwnedAsyncKernelLaunch` 的参数 marshalling**：阅读 [launch.rs](https://github.com/NVlabs/cuda-oxide/blob/2b713541cb572517b4932a50b4f4087ffb66203d/crates/cuda-async/src/launch.rs) 的 `KernelArgument`/`push_arg` 与 `AsyncKernelLaunch::launch`，它桥接了 u2-l4 讲的 `cuLaunchKernel` 参数编组。
- **想跑更多异步范式**：浏览 `examples/` 下其它 `async_*` 示例（如 `async_vecadd`），对比它们各自用了哪些组合子，巩固「图内顺序、图间并行」的判断力。
