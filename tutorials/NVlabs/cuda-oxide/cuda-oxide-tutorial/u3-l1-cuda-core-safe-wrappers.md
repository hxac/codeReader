# cuda-core 安全封装（含 module 受检启动宿主）

## 1. 本讲目标

本讲聚焦 cuda-oxide 宿主运行时的「地基」：`cuda-core` crate。它把 CUDA Driver API 那一堆以 `CUcontext` / `CUstream` / `CUmodule` / `CUevent` 为中心的 C 接口，包成了带生命周期的 Rust 类型。学完本讲你应该能够：

- 说出 `CudaContext` 如何用 RAII + `Arc` 管理主上下文（primary context），并理解「线程绑定」为什么是所有驱动调用的隐式前置条件。
- 用 `CudaStream` 的 fork/join 模型搭建多流依赖图，并区分「同流 FIFO」「跨流可重叠」两种顺序保证。
- 用 `CudaModule` / `CudaFunction` 加载 PTX 并取出内核句柄，理解引用计数如何防止「模块被提前卸载」。
- 用 `CudaEvent` 做跨流同步与 GPU 端计时，并知道什么场景必须关掉 `CU_EVENT_DISABLE_TIMING`。
- **（本轮 #318 新增）** 说出 `CudaModule` / `CudaFunction` 暴露的「活设备能力查询」表面（函数属性、设备上限、占用率），以及它如何成为类型化启动契约 `prepare_* → PreparedLaunch` 的宿主侧验证基石。

本讲承接 [u2-l4 从宿主启动内核](u2-l4-launching-kernels.md)：那一讲讲了 `prepare_*` 如何产出 `PreparedLaunch` 受检证明、未签约 kernel 如何走 `unsafe` 原始启动；本讲往下挖一层，讲这些验证所依赖的 context/stream/module/event 以及「活设备能力查询」是怎么被安全地创建、共享、查询和销毁的。

## 2. 前置知识

在进入源码前，先用大白话对齐四个 CUDA 概念。

**Context（上下文）**。可以把 context 理解成「一块 GPU 在你的进程里的一个工作台」。同一个设备有一个**主上下文（primary context）**，进程内所有使用者共享它。CUDA Driver API 的绝大多数调用都要求「调用线程当前正绑定到某个 context」——这一点贯穿本讲，务必记住。

**Stream（流）**。stream 是一个**按入队顺序执行（FIFO）的工作队列**。你往一条 stream 上提交「启动内核」「拷贝内存」等操作，它们会按提交顺序依次跑。不同 stream 之间的操作**可能重叠**（并发执行），这正是多流并发的来源。

**Module（模块）**。module 是一段已编译的 GPU 代码（PTX 文本或 cubin 二进制）被加载进 context 后的句柄。内核函数（kernel）是 module 里的一个**入口符号**，要靠名字去取。一个 `CUfunction` 还自带一组**编译期属性**（最大线程数、静态/动态共享内存、是否要求 cluster 等），驱动允许你在运行时查询甚至改写其中一部分——这是本讲 #318 的关键。

**Event（事件）**。event 是插在 stream 队列里的一个「书签」。在 A 流上 record 一个 event，再让 B 流 wait 这个 event，就能建立「B 等 A」的跨流顺序。event 还能携带时间戳，用来测 GPU 端耗时。

**RAII**（Resource Acquisition Is Initialization）。Rust 的标准资源管理范式：资源的生命周期绑在某个值的生命周期上，值被 drop 时自动释放资源。本讲的四个类型全是 RAII 类型——它们在 `Drop` 里调用对应的 `cu*Destroy` / `cu*Release`。

**`Arc`**（原子引用计数指针）。`Arc<T>` 让多个所有者共享同一份 `T`，最后一个 `Arc` 被释放时 `T` 才被 drop。本讲里 context 几乎总是放在 `Arc<CudaContext>` 里，因为 stream/event/module 都要持有它以确保「context 比我活得久」。

**粘性错误（sticky error）**。`Drop` 里不能返回 `Result`，但销毁资源时驱动调用可能失败。cuda-core 的做法是把这种错误码存进 context 的一个原子变量，等后续某次 `bind_to_thread` 时再「补报」出来。

**类型化启动契约（typed launch contract，#318）**。这是 [u2-l1](u2-l1-kernel-and-cuda-module-macros.md)/[u2-l4](u2-l4-launching-kernels.md) 引入的机制：用 `#[launch_contract(...)]` 声明一个 kernel 的 domain/block/共享内存/算力假设，宏据此生成 `prepare_<name>()`，它在**活设备**上一次性校验这些假设，产出品牌化见证类型 `PreparedLaunch<Kernel>`，此后启动该 kernel 是安全的。本讲的贡献是讲清「活设备校验」到底查了哪些东西——它们全落在 `CudaContext` 与 `CudaFunction` 上。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `crates/cuda-core/src/` 下：

| 文件 | 核心类型 | 作用 |
|------|----------|------|
| [context.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs) | `CudaContext` | 保留设备主上下文，提供线程绑定、新建流/事件、粘性错误记录；#318 起新增「设备能力查询」（启动上限、cooperative/cluster 支持、SM 数） |
| [stream.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/stream.rs) | `CudaStream` | 非阻塞流，fork/join 依赖图、host 回调 |
| [module.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs) | `CudaModule` / `CudaFunction` | 加载 PTX/cubin，按名取内核句柄；#318 起为 `CudaFunction` 增加函数属性/占用率查询与动态共享内存 opt-in，作为 `PreparedLaunch` 的宿主侧验证基石 |
| [launch.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs) | `LaunchConfig` / `PreparedLaunch` / `KernelLaunchContract` | 类型化启动契约的核心（详见 u2-l4）；本讲只引用其 `__prepare`，说明它消费的查询都来自 context/module |
| [event.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/event.rs) | `CudaEvent` | 跨流同步、GPU 端计时 |
| [error.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/error.rs) | `DriverError` / `IntoResult` | 把裸 `CUresult` 转成 `Result<T, DriverError>` |
| [lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/lib.rs) | 类型重导出、`launch_kernel_on_stream` | 公共 API 表面、把函数绑定到流的启动辅助 |

贯穿全部四个文件的两条「暗线」是：每个类型都持有一份 `Arc<CudaContext>`（保证 context 活得最久），以及每个方法在调驱动前都先 `bind_to_thread()`（保证当前线程绑定了正确的 context）。理解这两条暗线，四个类型就只剩细节差异了。本轮 #318 又加了第三条暗线：`CudaContext` 与 `CudaFunction` 的「能力查询」方法，专门服务于类型化启动契约的活设备校验。

---

## 4. 核心概念与源码讲解

### 4.1 CudaContext：主上下文、线程绑定与设备能力查询

#### 4.1.1 概念说明

`CudaContext` 是 cuda-core 的根对象。它的职责有四件：

1. **保留（retain）设备的主上下文**。主上下文是进程级共享的，多个 `CudaContext` 指向同一设备时背后其实是同一个 `CUcontext`。保留会令其引用计数 +1，释放时 -1。
2. **线程绑定**。CUDA Driver API 的调用是「context 作用域 + 线程局部」的——调用前，当前宿主线程必须先绑定到正确的 context。`CudaContext` 把这件事封装起来，几乎所有方法内部都会先调 `bind_to_thread`，调用方无需手动管理 context 栈。
3. **记录粘性错误**。用一个原子变量存「drop 期间发生的错误」，延迟到下次能返回 `Result` 的地方补报。
4. **（#318 新增）查询设备能力**。新增一组方法把 `cuDeviceGetAttribute` 包成领域语义：启动上限（`launch_limits`）、cooperative/cluster 是否支持、可选共享内存上限、SM 数量。这些查询被类型化启动契约按需调用。

`CudaContext::new` 返回的是 `Arc<Self>`，因为下游的 stream/event/module 都要克隆一份 `Arc` 来保活 context。

#### 4.1.2 核心流程

创建一个 context 的流程：

```text
CudaContext::new(ordinal)
  ├─ cuInit(0)                          // 初始化驱动（幂等）
  ├─ cuDeviceGet(ordinal)  -> CUdevice  // 按序号拿到设备句柄
  ├─ cuDevicePrimaryCtxRetain -> CUcontext  // 保留主上下文（引用计数 +1）
  ├─ 装入 Arc<CudaContext>
  └─ bind_to_thread()                   // 把它绑到当前线程
```

销毁（`Drop`）的流程：

```text
Drop
  ├─ bind_to_thread()                   // 释放前必须先绑定（驱动要求）
  ├─ 把 cu_ctx 置空，避免重复释放
  └─ cuDevicePrimaryCtxRelease_v2       // 引用计数 -1；归零时真正销毁
```

`bind_to_thread` 自带一处优化：它先用 `cuCtxGetCurrent` 查「当前线程已经绑定的 context」，只有当当前绑定的不是自己时才调用 `cuCtxSetCurrent`，省掉一次无谓的驱动往返。用一个式子概括主上下文的引用计数语义：

\[
\text{live}(\text{primaryCtx}) = \#\{\text{retain 调用}\} - \#\{\text{release 调用}\},\quad \text{真正销毁当且仅当 } \text{live} = 0
\]

设备能力查询统一走私有 helper `device_attribute`，它把 `cuDeviceGetAttribute` 的返回值安全地收拢成 `u32`：[crates/cuda-core/src/context.rs:L92-L104](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs#L92-L104)。所有公开查询方法都先 `bind_to_thread()` 再委托给它。

#### 4.1.3 源码精读

`CudaContext` 的字段定义。注意前三个字段是裸驱动句柄与序号，后三个是原子状态：[crates/cuda-core/src/context.rs:L36-L51](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs#L36-L51)。这段定义说明：context 同时承担「持有设备/上下文句柄」与「跨线程记账（活跃流数、事件追踪开关、粘性错误码）」两件事。

构造函数 `new`：[crates/cuda-core/src/context.rs:L112-L137](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs#L112-L137)。关键三步是 `cuInit` → `cuDeviceGet` → `cuDevicePrimaryCtxRetain`，最后 `ctx.bind_to_thread()` 把刚建好的 context 绑到调用线程，返回 `Arc`。

线程绑定逻辑：[crates/cuda-core/src/context.rs:L168-L179](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs#L168-L179)。先 `check_err()` 补报粘性错误，再判断「当前 context 是否已是自己」，不是才 `cuCtxSetCurrent`。

`Drop` 实现：[crates/cuda-core/src/context.rs:L69-L79](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs#L69-L79)。先把 `cu_ctx` 用 `mem::replace` 置空再释放，这样即便释放出错也不会重复释放；释放的错误经 `record_err` 存入粘性状态，绝不 panic。

粘性错误的读写：[crates/cuda-core/src/context.rs:L351-L370](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs#L351-L370)。`check_err` 用 `swap(0)` 原子地「读后清」，`record_err` 只在 `Err` 时 `store` 错误码。

`default_stream` 与 `new_stream`：[crates/cuda-core/src/context.rs:L195-L229](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs#L195-L229)。`default_stream` 返回一个持有**空** `CUstream` 指针的对象（驱动把空指针解释为默认流，默认流会与所有阻塞流隐式同步）；`new_stream` 用 `CU_STREAM_NON_BLOCKING` 标志创建非阻塞流，并且在「第一条流」诞生时同步一次 context 以建立干净的计时基线。

**（#318 新增）设备能力查询方法群**。`launch_limits` 一次性查回维度/线程数/可移植共享内存上限，封装成 `DeviceLaunchLimits`：[crates/cuda-core/src/context.rs:L281-L312](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs#L281-L312)。注意它的文档注释明确说「cooperative/cluster 能力**刻意不**在这里查」——这两种较新的能力只在 kernel 契约要求对应启动模式时才按需查，避免无谓开销。这两个按需查询分别是 `supports_cooperative_launch` 与 `supports_cluster_launch`：[crates/cuda-core/src/context.rs:L325-L338](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs#L325-L338)。另外 `max_opt_in_shared_memory_per_block`（[L318-L322](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs#L318-L322)）只在静态+动态共享内存超过可移植上限时才查；`multiprocessor_count`（[L341-L345](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/context.rs#L341-L345)）给占用率计算用。这组「懒查询」是 `PreparedLaunch` 校验的数据源，下一节的 `CudaFunction` 查询与之配套。

#### 4.1.4 代码实践

**实践目标**：亲手创建 context，查询设备信息与启动上限，直观感受 RAII 与「能力查询」。

**操作步骤**（这是一个源码阅读 + 本地可选运行的混合实践）：

1. 阅读 `CudaContext::new` 与 `device_name`、`compute_capability`、`launch_limits` 的实现，确认它们都先 `bind_to_thread()`。
2. 如果你本机有 CUDA GPU，仿照下面的「示例代码」写一个小程序（需配合 cuda-oxide 工具链，见 [u1-l3](u1-l3-toolchain-and-cargo-oxide.md)）：

```rust
// 示例代码：非项目原有，仅演示 API 用法
use cuda_core::CudaContext;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let ctx = CudaContext::new(0)?;            // ordinal=0 即第一块 GPU
    println!("device = {}", ctx.device_name()?);
    let (major, minor) = ctx.compute_capability()?;
    println!("sm_{}{}", major, minor);

    // #318 新增：查启动上限，体会「能力查询」表面
    let limits = ctx.launch_limits()?;
    println!("max_threads_per_block = {}", limits.max_threads_per_block);
    println!("supports cluster launch = {}", ctx.supports_cluster_launch()?);
    Ok(())
    // ctx 在此 drop -> cuDevicePrimaryCtxRelease_v2
}
```

**需要观察的现象**：打印出设备型号、算力版本（如 H100 → `sm_90`）以及最大线程数等上限；程序正常退出没有资源泄漏告警。

**预期结果**：拿到设备名、`(major, minor)` 元组与 `DeviceLaunchLimits`；无需手动释放任何资源。若本机无 GPU，**待本地验证**，可改为纯阅读型：口述 `new` 到 `Drop` 的全部驱动调用序列，以及 `launch_limits` 查了哪几个 `CU_DEVICE_ATTRIBUTE_*`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CudaContext` 的可变状态（`num_streams`、`error_state` 等）都用原子类型，而不是 `Mutex`？
**答案**：这些字段被多个持有 `Arc<CudaContext>` 的线程并发读写（不同线程各自创建/销毁流）。原子操作足以表达「计数加减」「错误码读写」这种简单场景，且无锁、不会阻塞，比 `Mutex` 更轻。`Send + Sync` 的 unsafe impl 也正是建立在这些字段都是原子的前提上。

**练习 2**：`Drop` 里为何先 `bind_to_thread()` 再调 `cuDevicePrimaryCtxRelease_v2`？
**答案**：CUDA 驱动要求释放主上下文前，当前线程必须绑定到该 context；否则释放调用会失败。`bind_to_thread` 保证了这一前置条件。

**练习 3**：`launch_limits` 为什么**不**顺带查 cooperative/cluster 支持？
**答案**：这两种是较新的、且只有当 kernel 契约显式要求 cooperative/cluster 启动模式时才需要的属性。把它们拆成独立的 `supports_cooperative_launch` / `supports_cluster_launch` 按需查询，可以让普通的 1D/2D/3D 启动校验少跑两次驱动往返。这是「懒查询」设计。

---

### 4.2 CudaStream：fork/join 与多流依赖图

#### 4.2.1 概念说明

`CudaStream` 包裹一个 `CUstream`，并把生命周期挂到父 `CudaContext` 上（持有一份 `Arc<CudaContext>`）。一个**空** `cu_stream` 代表该 context 的默认流（stream 0）。

cuda-core 的流一律用 `CU_STREAM_NON_BLOCKING` 创建：它**不会**和默认流隐式同步，因此多条非阻塞流之间可以真正重叠执行。要在流之间建立顺序，需要显式用 fork/join 或 event。

- `fork()`：在当前流的基础上「分叉」出一条新流，新流会等待 `self` 已入队的全部工作完成后再开始——相当于在流依赖图上立了一个 fork 点。
- `join(other)`：让 `self` 等待 `other` 已入队的全部工作。是 fork 的逆操作（join 点）。
- `wait(event)` / `record_event()`：更底层的砖块，fork/join 就是它们的组合。

`launch_host_function` 是另一条重要能力：往流上入队一个宿主端闭包，驱动在该流前面所有工作完成后、在驱动内部线程上回调它。这是后面 cuda-async 把「GPU 完成」桥接到 Rust `Future` 的关键（见 [u3-l3](u3-l3-async-execution-model.md)）。

#### 4.2.2 核心流程

fork/join 的本质是「record 一个 event + 让另一条流 wait 这个 event」：

```text
fork(self)        =  新建非阻塞流 s2;  s2.join(self)
join(self, other) =  ev = other.record_event();  self.wait(ev)
wait(self, ev)    =  cuStreamWaitEvent(self.cu_stream, ev.cu_event)
```

直观的依赖图（节点是流，边表示「等待」）：

```text
        ┌──→ stream_b  (fork 出来，等待 stream_a 已入队工作)
stream_a ┤
        └──→ stream_c
stream_b.join(stream_c)   // stream_b 后续工作要等 stream_c 完成
```

同一条流上的操作严格 FIFO；跨流的顺序只能靠 event 显式建立。用数学语言说，设 \( \prec_s \) 为流 \(s\) 上的入队先后关系，则 fork/join 给出的是跨流的偏序约束 \( a \prec_{s_1} \text{ev} \prec_{s_2} b \)。

#### 4.2.3 源码精读

`CudaStream` 结构与所有权：[crates/cuda-core/src/stream.rs:L40-L45](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/stream.rs#L40-L45)。`ctx: Arc<CudaContext>` 字段是「context 比流活得久」的保证。

`Drop`：[crates/cuda-core/src/stream.rs:L61-L70](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/stream.rs#L61-L70)。默认流（空句柄）不销毁；非默认流才 `fetch_sub` 活跃计数并 `cuStreamDestroy_v2`，错误经 `record_err` 存入 context。

`fork`：[crates/cuda-core/src/stream.rs:L96-L114](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/stream.rs#L96-L114)。先用 `CU_STREAM_NON_BLOCKING` 建流，再对新流调 `stream.join(self)`，从而让新流「承接」`self` 的已入队工作。

`join` 的实现只有一行，揭示 fork/join 就是 event 的语法糖：[crates/cuda-core/src/stream.rs:L122-L124](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/stream.rs#L122-L124)。

`wait`：[crates/cuda-core/src/stream.rs:L145-L155](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/stream.rs#L145-L155)，对应 `cuStreamWaitEvent`。

`launch_host_function` 与 `callback_wrapper`：[crates/cuda-core/src/stream.rs:L171-L204](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/stream.rs#L171-L204)。闭包被 `Box::new` 后 `Box::into_raw` 成裸指针传给 `cuLaunchHostFunc`；驱动的回调线程通过 `extern "C"` 跳板 `callback_wrapper` 用 `Box::from_raw` 重建并调用闭包。注意它用 `catch_unwind` 兜住 panic——绝不能让 Rust 的 unwinding 跨越 C ABI 边界。

#### 4.2.4 代码实践

**实践目标**：用 fork/join 在两条流之间建立依赖，观察「跨流等待」的效果。

**操作步骤**：

1. 阅读上述 `fork`/`join`/`wait` 三段源码，确认 `join` 内部确实只是 `record_event` + `wait`。
2. 仿照下面「示例代码」画出依赖关系（无 GPU 也能做阅读型实践）：

```rust
// 示例代码：演示 fork/join 的调用形态（省略内核启动细节）
let ctx = CudaContext::new(0)?;
let sa = ctx.new_stream()?;      // 流 A
let sb = sa.fork()?;             // 流 B：等待 A 已入队的工作
// ... 在 A 上入队一些操作 ...
// ... 在 B 上入队一些操作 ...
sb.join(&sa)?;                   // B 后续工作再等一次 A
```

**需要观察的现象**：去掉 `sb.join(&sa)` 时，A、B 两流没有顺序保证；加上后，B 在 join 点之后的工作必在 A 已入队工作之后才执行。

**预期结果**：能用 fork/join 表达「分叉—并行—汇合」的依赖。运行结果**待本地验证**（需要可启动的内核，见第 5 节综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：`fork` 返回的新流为什么也要持有一份 `ctx.clone()` 的 `Arc`？
**答案**：为了确保父 context 在新流被 drop 之前不会被释放。流的销毁（`cuStreamDestroy_v2`）和入队操作都需要 context 存活；持有 `Arc` 是 RAII 式的保活。

**练习 2**：为什么 `launch_host_function` 的跳板要用 `catch_unwind` 包住闭包调用？
**答案**：回调由 CUDA 驱动在驱动内部线程上发起，跨越了 C ABI 边界。如果闭包 panic，Rust 默认会 unwind 栈，但跨 C 边界 unwind 是未定义行为；`catch_unwind` 把 panic 捕获并丢弃，保证安全。

**练习 3**：默认流（空 `cu_stream`）和 `new_stream` 创建的非阻塞流，在与默认流的同步行为上有何区别？
**答案**：默认流会与同 context 内所有阻塞流隐式同步；非阻塞流（`CU_STREAM_NON_BLOCKING`）则不会与默认流隐式同步，因而能与之并发，跨流顺序只能显式建立。

---

### 4.3 CudaModule、CudaFunction 与类型化启动的活设备校验宿主

#### 4.3.1 概念说明

`CudaModule` 包裹一个 `CUmodule`——也就是一段被加载进 context 的 GPU 代码（PTX 文本、cubin 二进制或 fatbin）。`CudaFunction` 包裹一个 `CUfunction`，即 module 里的一个内核入口符号，按名字取出。

二者都通过 `Arc` 挂在父对象上：

- `CudaModule` 持有 `Arc<CudaContext>`。
- `CudaFunction` 持有 `Arc<CudaModule>`——这一步至关重要：它保证「只要还有一个 `CudaFunction` 句柄，module 就不会被卸载」，从而杜绝 use-after-unload。

**（#318 新增）`CudaFunction` 还是类型化启动契约的「活设备校验宿主」**。一个 `CUfunction` 自带一组编译期属性（最大线程数、静态共享内存、当前动态共享内存上限、是否要求 cluster 维度等），驱动允许在运行时用 `cuFuncGetAttribute` 查询、用 `cuFuncSetAttribute` 改写其中一部分；此外还能用 `cuOccupancyMax*` 系列算占用率。cuda-core 把这些包成了 `CudaFunction` 上的安全方法（`max_threads_per_block`、`static_shared_memory_bytes`、`max_dynamic_shared_memory_bytes`、`required_cluster_dimensions`、`set_max_dynamic_shared_memory_bytes`、占用率三件套）。`PreparedLaunch::__prepare`（在 [launch.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs)，详见 u2-l4）正是消费这组查询，在活设备上一次性校验 kernel 契约，再产出受检证明。换句话说：`CudaModule` 负责「把 PTX 装进设备」，`CudaFunction` 负责「告诉你这个 kernel 在这台设备上能怎么跑」，二者合起来才是 `prepare_*` 的完整宿主侧基础。

> 提示：日常使用中你很少直接调 `load_module_from_ptx_src`。宏 `#[cuda_module]` 生成的 `kernels::load(&ctx)` 已经把「加载内嵌 PTX bundle + 逐个 `load_function`」打包好了（见 [u2-l1](u2-l1-kernel-and-cuda-module-macros.md)）。但底层调的就是本节这套 API。

#### 4.3.2 核心流程

加载与取句柄：

```text
ctx.load_module_from_ptx_src(ptx_str)
  ├─ bind_to_thread()
  ├─ CString::new(ptx_str)              // PTX 必须是 NUL 结尾的 C 字符串
  ├─ cuModuleLoadData(...)  -> CUmodule // 驱动 JIT 编译 PTX 到当前设备架构
  └─ 包成 Arc<CudaModule>

module.load_function("vecadd")
  ├─ ctx.bind_to_thread()
  ├─ CString::new(name)
  ├─ cuModuleGetFunction(...) -> CUfunction
  └─ 包成 CudaFunction { module: self.clone() }   // 持有 Arc<CudaModule>
```

类型化启动契约的活设备校验（宿主侧，#318）：

```text
module.prepare_<name>(config)           // 宏生成，转发到 ↓
  └─ PreparedLaunch::<__name_CudaKernel>::__prepare(function, config)
       ├─ validate_static(SPEC, raw)                // 纯算术：秩、block 形状
       ├─ limits          = ctx.launch_limits()?    // 设备上限（4.1 新增）
       ├─ max_threads     = function.max_threads_per_block()?
       ├─ static_shared   = function.static_shared_memory_bytes()?
       ├─ func_max_dyn    = function.max_dynamic_shared_memory_bytes()?
       ├─ validate_live_shape(SPEC, raw, limits, max_threads)
       ├─ 校验动态共享内存总量，必要时 function.set_max_dynamic_shared_memory_bytes(..)
       ├─ 按 SPEC 按需查 ctx.supports_cooperative_launch() / supports_cluster_launch()
       └─ 包成 PreparedLaunch { function, config }  // 品牌化见证，后续启动安全
```

`cuModuleLoadData` 会触发驱动对 PTX 的 JIT 编译，目标是当前绑定 context 的设备架构——这也是为什么加载前必须先 `bind_to_thread`。

#### 4.3.3 源码精读

`CudaModule` 结构：[crates/cuda-core/src/module.rs:L41-L47](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L41-L47)。`ctx: Arc<CudaContext>` 保活 context。

`Drop`：[crates/cuda-core/src/module.rs:L62-L68](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L62-L68)，先 `bind_to_thread` 再 `cuModuleUnload`。

从 PTX 字符串加载：[crates/cuda-core/src/module.rs:L80-L96](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L80-L96)。`CString::new(ptx_src).unwrap()` 会因内嵌 NUL 字节而 panic——文档明确标注了这条契约。同文件还有从内存镜像加载（`load_module_from_image`，[L104-L120](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L104-L120)）与从磁盘文件加载（`load_module_from_file`，[L130-L145](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L130-L145)）两个变体。

`CudaFunction` 结构：[crates/cuda-core/src/module.rs:L164-L171](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L164-L171)。`module: Arc<CudaModule>` 是防提前卸载的关键。

`load_function`：[crates/cuda-core/src/module.rs:L241-L258](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L241-L258)，对应 `cuModuleGetFunction`。

**（#318 新增）`CudaFunction` 的属性查询表面**。私有 helper `attribute` 包装 `cuFuncGetAttribute` 并安全收拢成 `u32`：[crates/cuda-core/src/module.rs:L446-L458](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L446-L458)。其上的三个读查询——`max_threads_per_block`、`static_shared_memory_bytes`、`max_dynamic_shared_memory_bytes`——是 `__prepare` 校验 block 形状与共享内存的直接数据源：[crates/cuda-core/src/module.rs:L466-L482](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L466-L482)。`required_cluster_dimensions` 把三个 required-cluster 属性读回 `(u32,u32,u32)`，并刻意把「部分为零」的非法响应当成错误而非静默归一：[crates/cuda-core/src/module.rs:L489-L508](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L489-L508)。

**（#318 新增）动态共享内存 opt-in 与占用率**。`set_max_dynamic_shared_memory_bytes`（`pub(crate)`，仅类型化启动准备调用，且最多一次）对应 `cuFuncSetAttribute`：[crates/cuda-core/src/module.rs:L514-L529](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L514-L529)。占用率三件套 `max_active_blocks_per_multiprocessor` / `max_potential_cluster_size` / `max_active_clusters`（[L533-L650](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L533-L650)）包装 `cuOccupancyMax*`，给高级用户做占用率调优。需要裸句柄互操作时还有 `unsafe fn cu_function()`：[crates/cuda-core/src/module.rs:L664-L666](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/module.rs#L664-L666)。

**串起 #318 的关键一跃**：`PreparedLaunch::__prepare` 的函数体把这组查询一次性跑完——`ctx.launch_limits()`、`function.max_threads_per_block()`、`function.static_shared_memory_bytes()`、`function.max_dynamic_shared_memory_bytes()` 四连查后做 `validate_live_shape`：[crates/cuda-core/src/launch.rs:L816-L829](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L816-L829)。它产出的品牌化见证类型定义在 [crates/cuda-core/src/launch.rs:L786-L790](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L786-L790)，其文档注释点明「preparation 跑完全部资源查询，复用时只比 stream 的 context 句柄」。契约本身的静态描述（秩、block、共享内存、算力）由 `KernelLaunchContract` 关联类型与 `SPEC` 承载（[launch.rs:L298](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L298)、`DeviceLaunchLimits` 在 [launch.rs:L308](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L308)）。完整启动链路（含三种入口的差异）见 [u2-l4](u2-l4-launching-kernels.md)。

错误转换的底层机制（`IntoResult` trait 与 `CUresult` 的实现）：[crates/cuda-core/src/error.rs:L113-L128](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/error.rs#L113-L128)。所有 `*.result()?` 调用都走这里——把 `CUDA_SUCCESS` 映射为 `Ok(())`，其余包装成 `DriverError`。这是整个 crate「裸 C 返回值 → Rust `Result`」的统一出口。

#### 4.3.4 代码实践

**实践目标**：跟踪 vecadd 示例里「加载模块 + 启动」的真实调用链，并对照一个签约 kernel 看 `prepare_*` 的活设备校验。

**操作步骤**：

1. 打开 [crates/rustc-codegen-cuda/examples/vecadd/src/main.rs:L75-L84](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/vecadd/src/main.rs#L75-L84)，定位 `kernels::load(&ctx)` 与 `unsafe { module.vecadd(...) }`。注意 #318 之后：vecadd 没有 `#[launch_contract]`，故其启动方法吃 raw `LaunchConfig`、被宏标成 `unsafe`，调用处必须包在 `unsafe` 块里并由调用方用 `SAFETY:` 注释自证形状/资源/缓冲区匹配（承接 [u1-l4](u1-l4-hello-gpu-vecadd.md) 与 [u2-l4](u2-l4-launching-kernels.md)）。
2. 对照 `CudaModule::load_function` 源码，理解宏生成的 `module.vecadd(...)` 内部最终会调到 `cuModuleGetFunction` 拿到 `vecadd` 的 `CUfunction`，再走 `launch_kernel_on_stream` 启动。
3. **（进阶）** 找一个带 `#[launch_contract(...)]` 的示例（例如 reduce 类内核），把 `module.<name>(...)` 换成 `module.prepare_<name>(LaunchConfig1D::new(...))?` 再受检启动；对照 `__prepare` 的四连查，说出它在本机上校验了哪些量。

**需要观察的现象**：宏帮你把「按 PTX 名（`vecadd`，剥掉前缀的原始名，见 [u2-l1](u2-l1-kernel-and-cuda-module-macros.md)）取函数 + 类型化编组参数 + 绑定流」全自动化了；raw 路径下你只需 `unsafe { module.vecadd(&stream, cfg, ...) }`，而签约路径下 `prepare_*` 会真的去查 `launch_limits` / `max_threads_per_block` 等活设备属性。

**预期结果**：能复述从 `kernels::load(&ctx)` 到 `cuLaunchKernel` 之间的每一跳（`load_embedded_module` → `load_function` → `launch_kernel_on_stream` → `cuLaunchKernel`）；并说出签约 kernel 多出来的 `__prepare` 一跳调用了哪些 `CudaContext` / `CudaFunction` 查询。

#### 4.3.5 小练习与答案

**练习 1**：`CudaFunction` 为什么要持有一份 `Arc<CudaModule>`，而 `CudaModule` 只持有 `Arc<CudaContext>`？
**答案**：内核句柄 `CUfunction` 的有效性依赖于它所在的 `CUmodule` 仍然加载；`CudaFunction` 持有 `Arc<CudaModule>` 确保「只要还有函数句柄，模块就不会被 `cuModuleUnload`」。`CudaModule` 持有 `Arc<CudaContext>` 则是同理保活 context。这是 RAII 链式保活。

**练习 2**：`load_module_from_ptx_src` 为什么用 `CString::new(ptx_src).unwrap()` 而不是返回一个错误？
**答案**：合法的 PTX 字符串不会含内嵌 NUL 字节；若出现 NUL，说明输入本身就非法，属于程序员违约，用 panic 立即暴露比静默截断更安全。文档已显式声明此契约。

**练习 3**：`set_max_dynamic_shared_memory_bytes` 为什么是 `pub(crate)` 而不是 `pub`？为什么文档说「最多调一次」？
**答案**：动态共享内存 opt-in 是一个**契约级**的不可变设置——它改的是该 `CUfunction` 的编译期属性上限，应只由类型化启动准备在确认「静态+动态共享内存超过可移植上限且未超过 opt-in 上限」后调一次。把它设为 `pub(crate)` 是为了不让用户绕过 `prepare_*` 自行乱改；「最多一次」是因为重复写可能把契约最大值改小，破坏已经 prepared 的启动假设（源码注释明确指出：写的是「契约最大值」而非本次启动的选定值，正是为了避免并发 prepare 互相压低上限）。

---

### 4.4 CudaEvent：跨流同步与 GPU 端计时

#### 4.4.1 概念说明

`CudaEvent` 包裹一个 `CUevent`，是 stream 之间最轻量的同步原语。两个核心用法：

1. **建立跨流顺序**：在 A 流 `record` 一个 event，让 B 流 `wait` 它。
2. **GPU 端计时**：用两个带时间戳的 event 夹住一段工作，`elapsed_ms` 得到它们之间的毫秒数。

一个容易踩的坑：默认创建的 event 带 `CU_EVENT_DISABLE_TIMING` 标志，**开销更低但不能计时**。要测时间，必须传 `Some(CU_EVENT_DEFAULT)`。本讲的综合实践就会用到这一点。

#### 4.4.2 核心流程

```text
ctx.new_event(None)                // 默认 CU_EVENT_DISABLE_TIMING
ctx.new_event(Some(CU_EVENT_DEFAULT))  // 可计时

event.record(stream)  -> cuEventRecord(event, stream)   // 把书签插到 stream 队列
stream2.wait(&event)  -> cuStreamWaitEvent(...)          // stream2 后续工作等该 event
event.query()         -> true/false（非阻塞，cuEventQuery）
event.synchronize()   -> cuEventSynchronize（阻塞到完成）
event.elapsed_ms(end) -> cuEventElapsedTime（两个可计时 event 之间）
```

计时平均化：若在 start/end 之间跑了 \(N\) 次同样的内核，单次平均耗时为

\[
\overline{t}_{\text{kernel}} = \frac{\text{elapsed\_ms}(\text{start},\text{end})}{N}
\]

#### 4.4.3 源码精读

`CudaEvent` 结构与 Drop：[crates/cuda-core/src/event.rs:L30-L57](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/event.rs#L30-L57)。Drop 先 `bind_to_thread` 再 `cuEventDestroy_v2`，错误入粘性状态。

`new_event` 与默认标志：[crates/cuda-core/src/event.rs:L65-L80](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/event.rs#L65-L80)。`None` 时回落到 `CU_EVENT_DISABLE_TIMING`。

`record` / `synchronize` / `query`：[crates/cuda-core/src/event.rs:L99-L124](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/event.rs#L99-L124)。注意 `query` 把 `CUDA_ERROR_NOT_READY` 映射成 `Ok(false)` 而非错误——这是「还没完成」的正常语义。

`elapsed_ms`：[crates/cuda-core/src/event.rs:L134-L143](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/event.rs#L134-L143)。它先把两个 event 都 `synchronize`（确保工作真跑完），再调 `cuEventElapsedTime`。

真实工程用法可参考 GEMM 示例：用 `record_event(Some(CU_EVENT_DEFAULT))` 夹住迭代循环，再 `elapsed_ms` 算吞吐：[crates/rustc-codegen-cuda/examples/gemm_sol/src/main.rs:L4453-L4476](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/rustc-codegen-cuda/examples/gemm_sol/src/main.rs#L4453-L4476)。

#### 4.4.4 代码实践

**实践目标**：体会「默认 event 不能计时」这一契约。

**操作步骤**：

1. 阅读 `new_event`，确认 `None` → `CU_EVENT_DISABLE_TIMING`。
2. 阅读 `elapsed_ms` 的文档注释：两个 event 都必须**不带** `CU_EVENT_DISABLE_TIMING`，否则驱动返回 `CUDA_ERROR_INVALID_HANDLE`。

**需要观察的现象**：如果误用默认 event 去调 `elapsed_ms`，会得到一个 `DriverError`，而非时间值。

**预期结果**：明确「要计时就必须 `Some(CU_EVENT_DEFAULT)`」。运行验证**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`query()` 为什么把 `CUDA_ERROR_NOT_READY` 当成 `Ok(false)` 而不是 `Err`？
**答案**：`NOT_READY` 在这里表达的是「事件尚未被记录完成」，是查询的正常结果之一，并非驱动故障。把它映射为 `Ok(false)` 让 `query()` 的返回值语义干净：「完成 / 未完成 / 真错误」三态。

**练习 2**：`elapsed_ms` 为什么要先对两个 event 都调 `synchronize`？
**答案**：`cuEventElapsedTime` 读取的是两个 event 的时间戳；只有当事件对应的工作真正完成、时间戳被驱动写回后，读数才有意义。先同步确保 GPU 端工作已结束。

---

## 5. 综合实践：双流计时对比 + 签约 kernel 受检启动

把本讲四个模块串起来，完成规格里要求的任务：**创建两个 stream，分别启动核函数，用 CudaEvent 测量并对比它们的执行时间；再为一个签约 kernel 调用 `module.prepare_*`，观察其对活设备的校验。**

我们以 vecadd 为内核载体（你已在 [u1-l4](u1-l4-hello-gpu-vecadd.md) 跑通过它）。下面是一份可直接放进某个示例 `main.rs` 的「示例代码」骨架（**非项目原有代码**，仅供练习参考）：

```rust
// 示例代码：双流 vecadd 计时（raw 启动需 unsafe，承接 #318）
use cuda_core::{CudaContext, DeviceBuffer, LaunchConfig};
use cuda_device::{DisjointSlice, cuda_module, kernel, thread};

#[cuda_module]
mod kernels {
    use super::*;
    #[kernel]
    pub fn vecadd(a: &[f32], b: &[f32], mut c: DisjointSlice<f32>) {
        let idx = thread::index_1d();
        if let Some(c_elem) = c.get_mut(idx) {
            *c_elem = a[idx.get()] + b[idx.get()];
        }
    }
}

fn timed_run(iters: u32) -> Result<f32, Box<dyn std::error::Error>> {
    let ctx = CudaContext::new(0)?;
    let stream = ctx.new_stream()?;                 // 非阻塞流（模块 4.2）

    const N: usize = 1 << 20;
    let a: Vec<f32> = (0..N).map(|i| i as f32).collect();
    let b: Vec<f32> = (0..N).map(|i| i as f32).collect();
    let a_dev = DeviceBuffer::from_host(&stream, &a)?;
    let b_dev = DeviceBuffer::from_host(&stream, &b)?;
    let mut c_dev = DeviceBuffer::<f32>::zeroed(&stream, N)?;

    let module = kernels::load(&ctx)?;              // 加载内嵌 PTX（模块 4.3）
    stream.synchronize()?;                          // 先同步，建立干净基线

    // 关键：必须用 CU_EVENT_DEFAULT 才能计时（模块 4.4）
    let start = stream.record_event(Some(cuda_core::sys::CUevent_flags_enum_CU_EVENT_DEFAULT))?;
    for _ in 0..iters {
        // #318：vecadd 未签约，启动方法吃 raw LaunchConfig 故为 unsafe，调用方自证形状
        unsafe {
            module.vecadd(&stream, LaunchConfig::for_num_elems(N as u32), &a_dev, &b_dev, &mut c_dev)?;
        }
    }
    let end = stream.record_event(Some(cuda_core::sys::CUevent_flags_enum_CU_EVENT_DEFAULT))?;
    let total_ms = start.elapsed_ms(&end)?;         // GPU 端总耗时
    Ok(total_ms / iters as f32)                     // 单次平均
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 复用同一个 context 里的两条独立流做对比
    let avg = timed_run(50)?;
    println!("avg per-launch = {:.3} ms", avg);
    Ok(())
}
```

**任务步骤**：

1. **建流**：用 `ctx.new_stream()` 各建两条非阻塞流 `sa`、`sb`（不要用 `default_stream()`，否则两条会隐式同步）。
2. **加载**：`kernels::load(&ctx)` 拿到带类型的 module。
3. **基线**：启动前先 `stream.synchronize()`，确保计时不受前面 `from_host` 的尾巴影响（对照 gemm_sol 的 warmup 写法）。
4. **计时**：对每条流，用 `record_event(Some(CU_EVENT_DEFAULT))` 夹住循环，`elapsed_ms` 求总耗时，再除以迭代数得单次平均。注意 raw 启动要包在 `unsafe` 块中。
5. **对比**：打印 `sa`、`sb` 两条流的单次平均耗时；预期二者接近（同设备同内核）。再额外测一次「把两条流的工作都放到同一条流里串行」的总耗时，体会非阻塞流**可能重叠**带来的吞吐差异。
6. **（#318 进阶）签约 kernel 受检启动**：把你练习用的某个带 `#[launch_contract(domain=1, block=(256,1,1))]` 的 kernel（如 reduce）改成 `module.prepare_<name>(LaunchConfig1D::new(...))?` 产出 `PreparedLaunch`，再用受检同名方法启动。对照 [launch.rs:L816-L829](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L816-L829) 的四连查，说出 `prepare_*` 在你的设备上实际校验了 `launch_limits`、`max_threads_per_block`、`static_shared_memory_bytes`、`max_dynamic_shared_memory_bytes` 中的哪几项。

**需要观察的现象**：
- 若误把 `Some(CU_EVENT_DEFAULT)` 写成 `None`，`elapsed_ms` 会返回 `DriverError(INVALID_HANDLE)`——这是模块 4.4 的契约在「报警」。
- 两条非阻塞流的总墙钟时间通常**小于**「单流串行总时间 × 2」，说明它们在 GPU 上有重叠。
- 签约 kernel 的 `prepare_*` 在「block 超过设备 `max_threads_per_block`」或「动态共享内存超限」时会返回 `LaunchContractError`，而 raw 启动路径不会——这正是受检启动把「调用方自证」升级为「运行期可证」的体现。

**预期结果**：得到两条流各自的平均单次耗时并完成对比；理解 event 的计时刻度是 GPU 端时间戳（不受宿主 CPU 抖动影响）；理解 `prepare_*` 的活设备校验建立在 `CudaContext`/`CudaFunction` 的能力查询之上。

> 说明：本实践需要真实的 CUDA GPU 与 cuda-oxide 工具链。若本机不具备，请降级为**源码阅读型实践**：画出「`new_stream` → `load` → `synchronize` → `record_event` × 2 → `elapsed_ms`」与「`prepare_<name>` → `__prepare`（四连查）→ 受检启动」两条调用链，并标注每一步对应哪个模块、哪个驱动 API。具体数值**待本地验证**。

## 6. 本讲小结

- `CudaContext` 用 RAII + `Arc` 管理主上下文，`new` 走 `cuInit`→`cuDeviceGet`→`cuDevicePrimaryCtxRetain`，`Drop` 走 `cuDevicePrimaryCtxRelease_v2`；它是所有对象的根。
- **线程绑定是隐式契约**：几乎所有方法内部都先 `bind_to_thread()`，调用方无需手动管 context 栈；`bind_to_thread` 还顺带补报粘性错误。
- `CudaStream` 提供非阻塞流与 fork/join 依赖图；`join` 本质是 `record_event` + `wait` 的语法糖；`launch_host_function` 是通往 Rust async 的桥。
- `CudaModule`/`CudaFunction` 通过 `Arc` 链式保活（function→module→context），杜绝「模块被提前卸载」；`load_function` 对应 `cuModuleGetFunction`。
- **（#318）`CudaModule`/`CudaFunction` 是类型化启动契约的活设备校验宿主**：`CudaContext` 新增 `launch_limits`/`supports_cooperative_launch`/`supports_cluster_launch`/`max_opt_in_shared_memory_per_block`/`multiprocessor_count`，`CudaFunction` 新增 `max_threads_per_block`/`static_shared_memory_bytes`/`max_dynamic_shared_memory_bytes`/`required_cluster_dimensions`/`set_max_dynamic_shared_memory_bytes` 与占用率三件套；它们正是 `PreparedLaunch::__prepare` 在活设备上做受检校验的数据源。
- `CudaEvent` 是跨流同步与 GPU 端计时的统一原语；**计时必须用 `CU_EVENT_DEFAULT`**，默认的 `DISABLE_TIMING` 更便宜但不能 `elapsed_ms`。
- 全部四个类型共享两条暗线：持有一份 `Arc<CudaContext>`、调用驱动前先 `bind_to_thread()`；错误统一经 `IntoResult::result()` 转成 `DriverError`。#318 加了第三条暗线：面向 `prepare_*` 的「能力查询」表面。

## 7. 下一步学习建议

- 想看「内嵌 PTX bundle 是怎么被发现并 `load_module` 的」，继续学 [u3-l2 模块加载与内嵌制品](u3-l2-module-loading-and-embedded-artifacts.md)，它会展开 `load_embedded_module` 与 `.oxart` 制品格式，并说明为何签约模块的 loader 现在是 `unsafe`。
- 想把本讲的 stream/event/host 回调升级成「惰性求值 + Rust Future」，继续学 [u3-l3 异步执行模型](u3-l3-async-execution-model.md)，看 `DeviceOperation` 如何用 `launch_host_function` 唤醒 future，以及 raw `*_async` 路径为何变 `unsafe`。
- 想深入「多条流如何被自动分配与组合」，继续学 [u3-l4 调度策略与组合子](u3-l4-scheduling-and-combinators.md)。
- 若你对本讲引用的 `launch_kernel_on_stream`（绑定流的启动辅助）感兴趣，可直接阅读 [crates/cuda-core/src/lib.rs:L200-L219](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/lib.rs#L200-L219)，它把 `bind_to_thread` 与裸 `cuLaunchKernel` 串了起来；想看 `PreparedLaunch::__prepare` 的完整校验链路则读 [crates/cuda-core/src/launch.rs:L786-L829](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L786-L829)（完整三种启动入口的差异见 [u2-l4](u2-l4-launching-kernels.md)）。
