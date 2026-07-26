# TFRT 新一代运行时

## 1. 本讲目标

本讲是「编译器与运行时」单元的第四讲，承接 [u3-l2 会话执行链路 Session 与 DirectSession](u3-l2-session-execution.md) 中讲透的 `DirectSession` 执行模型，以及 [u6-l1 Device 与 DeviceFactory](u6-l1-device-and-devicefactory.md) 中的设备抽象。

学完本讲，你应当能够：

- 说清 **TFRT（TensorFlow Runtime）** 这个「新一代运行时」到底想解决 `DirectSession` 的哪些历史包袱；
- 理解 TFRT 的分层模型：`HostContext` / `CoreRuntime` / `OpHandler` / `AsyncValue` / `BEF`，以及 TF 用 `tfrt_stub::Runtime` 把它们包起来的方式；
- 看懂 `TfrtSession` 如何用**静态注册的 `SessionFactory`** 把一整套 TFRT 运行时「塞进」既有的 `Session` 接口里，做到对用户代码透明；
- 理解「**内核回退（kernel fallback）**」机制——TFRT 自己负责调度，但每个算子的真正计算仍然委托给现有的 `OpKernel` 去跑，理解这一点就能解开「TFRT 为什么能渐进替换而不推倒重来」的谜题；
- 能对比 **BEF/MLRT 解释器**与 `DirectSession` 的 `Executor` 在执行模型上的根本差异。

本讲覆盖三个最小模块：`core.tfrt.runtime`、`core.tfrt.tfrt_session`、`core.runtime_fallback`，并以 `core.tfrt.graph_executor` 作为衔接前两者的中间层。

---

## 2. 前置知识

### 2.1 为什么要再造一个运行时

回顾 [u3-l2](u3-l2-session-execution.md)：传统的 `DirectSession` 在 `Run` 时会经历「剪枝 → 放置 → 优化 → 分区 → 由 `Executor` 按节点拓扑调度 → 经 `Rendezvous` 传递张量」这条链路。这套设计诞生于 TF 1.x 的图模式时代，有两个长期痛点：

1. **同步模型固化**：`Executor` 用「节点就绪检查 + 线程池调度」驱动整张图，每条数据依赖靠 `Rendezvous` 显式 `Send`/`Recv` 配对，跨设备开销与代码复杂度都很高。
2. **执行器与设备/算子强耦合**：算子执行、设备抽象、内存分配、线程调度交织在一起，难以单独演进或替换。

TFRT（独立项目 `tf_runtime`，作为 `@tf_runtime` 外部依赖引入）试图把运行时拆成**清晰的、可组合的、以异步数据流为核心**的几个抽象，让 CPU/GPU/TPU、同步/异步、TF 原生/XLA 等不同后端能在同一套框架下共存。它的口号是：**算子调度归 TFRT，真正计算归后端（对 TF 原生算子来说，就是「回退」到既有 `OpKernel`）**。

### 2.2 TFRT 的五个核心名词

本讲会反复出现下面五个 TFRT 原语，先建立直觉：

| 名词 | 直觉 | 类比 TF 既有概念 |
| --- | --- | --- |
| **HostContext** | 主机环境：分配器、工作队列、诊断器，是一切的容器 | 类似一个进程级的「资源根」 |
| **ConcurrentWorkQueue** | 异步任务队列，决定「就绪的算子」何时被哪个线程执行 | 类似 `DirectSession` 的 inter-op 线程池 |
| **AsyncValue** | 异步值（future/lazy value），算子之间的数据单位 | 类似 `Tensor`，但**未就绪时也能被引用和依赖** |
| **CoreRuntime** | 运行时中枢，持有 `HostContext` 与一组 `OpHandler` | 类似一个「超级 Session」 |
| **OpHandler** | 后端抽象：知道如何把一个 op 派发到具体后端执行 | 类似 `Device`，但更细粒度（参见 [u6-l1](u6-l1-device-and-devicefactory.md)） |

还有一个序列化格式 **BEF（Binary Executor Format）**：把 MLIR 编译出的图序列化成二进制，再由 BEF 执行器解释运行。它是「编译期产物」与「运行期执行器」之间的契约。本讲后半段还会出现 **MLRT**——TF 自己实现的、MLIR 字节码（bytecode）解释器，是与 BEF 执行器并列的第二种执行引擎。

> 提示：TFRT 是一个**独立仓库**（以 `@tf_runtime` 外部依赖引入，源码在 `third_party/`），本讲只读 TF 仓库里 `tensorflow/core/tfrt/` 与 `tensorflow/core/runtime_fallback/` 这些「把 TFRT 用起来」的胶水代码，不去翻 `@tf_runtime` 内部。

---

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| `tensorflow/core/tfrt/runtime/runtime.h` / `.cc` | 定义 `tfrt_stub::Runtime`，对 `tfrt::CoreRuntime` 的薄封装，是 TF 侧用 TFRT 的入口 |
| `tensorflow/core/tfrt/runtime/work_queue_interface.h` | 把 TF 的线程池适配成 TFRT 的 `ConcurrentWorkQueue` |
| `tensorflow/core/tfrt/tfrt_session/tfrt_session.h` / `.cc` | `TfrtSession` 与 `TfrtSessionFactory`：把 TFRT 接入既有 `Session` 接口 |
| `tensorflow/core/tfrt/graph_executor/graph_executor.h` / `.cc` | `GraphExecutor`：把 GraphDef 经 MLIR 编译成 BEF/字节码并运行 |
| `tensorflow/core/tfrt/fallback/fallback_state.h` / `.cc` | `FallbackState`：承载既有 TF 运行时状态（DeviceMgr、函数库等），供回退使用 |
| `tensorflow/core/runtime_fallback/runtime/runtime_fallback_op_handler.h` | `RuntimeFallbackOpHandler`：把 op 路由到 TF 内核的后端 |
| `tensorflow/core/runtime_fallback/kernel/kernel_fallback_execute.h` | `KernelFallbackExecute`：回退执行的对外入口 |
| `tensorflow/core/runtime_fallback/kernel/kernel_fallback_execute_compat.cc` | 回退的真正实现：实例化 `OpKernelContext` 跑既有 `OpKernel` |
| `tensorflow/core/runtime_fallback/tf_bef_executor_main.cc` | 一个命令行 demo，演示如何用 BEF 执行器加载并运行 BEF 文件 |

---

## 4. 核心概念与源码讲解

### 4.1 TFRT 运行时抽象 Runtime（core.tfrt.runtime）

#### 4.1.1 概念说明

`@tf_runtime` 暴露的最顶层对象是 `tfrt::CoreRuntime`，它内含一个 `HostContext`（管理分配器与工作队列）和一组 `OpHandler`。但 TF 代码不直接 `new CoreRuntime`，而是在 `tensorflow/core/tfrt/runtime/` 里包了一层 `tfrt_stub::Runtime`。

这一层封装做了三件事：

1. **隐藏构造细节**：用一个静态工厂 `Runtime::Create(...)` 把「分配器 + 工作队列 + 默认 host 设备名」凑齐再创建 `CoreRuntime`。
2. **适配 TF 线程池**：TF 有自己成熟的线程池实现，TFRT 想复用它而不是另起炉灶，于是用 `WorkQueueInterface` 做适配。
3. **提供进程级单例**：通过 `GetGlobalRuntime()` / `SetGlobalRuntime()` 让整个进程共享一个 `Runtime`（典型场景是 SavedModel 加载）。

源码注释里坦白说这是**临时封装**，将来会被官方的 `tensorflow::experimental::cc::Runtime` 取代——这恰好印证了 TFRT 是「仍在演进中的新一代设计」。

#### 4.1.2 核心流程

创建一个 `Runtime` 的过程可以画成：

```text
用户指定线程数
      │
      ▼
Runtime::Create(num_inter, num_intra)
      │  构造一个 ConcurrentWorkQueue（默认实现：多线程工作队列）
      ▼
Runtime::Create(unique_ptr<WorkQueueInterface>, diag_handler)
      │
      ▼
tfrt::CoreRuntime::Create(diag_handler, malloc_allocator, work_queue,
                          "/job:localhost/.../CPU:0")   ← 默认 host 设备名
      │
      ▼
持有 core_runtime_ 与 work_queue_，对外暴露 core_runtime() / work_queue()
```

这里有一个关键设计：`CoreRuntime` 在创建时需要一个**默认 host 设备名**（字符串）。这继承了 TF 的设备命名约定（见 [u6-l1](u6-l1-device-and-devicefactory.md) 中 `/job:/replica:/task:/device:CPU:0` 格式），但注意它在 TFRT 里只是一个标识字符串，真正的「设备」语义由 `OpHandler` 承载。

#### 4.1.3 源码精读

工厂方法 `Runtime::Create` 把线程数翻译成一个工作队列，再委托给重载版本：

[tensorflow/core/tfrt/runtime/runtime.cc:66-73](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/runtime/runtime.cc#L66-L73) — 把 `(num_inter_op_threads, num_intra_op_threads)` 翻成 TFRT 的多线程工作队列，再调用下面的核心 `Create`。

真正创建 `CoreRuntime` 的地方：

[tensorflow/core/tfrt/runtime/runtime.cc:51-64](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/runtime/runtime.cc#L51-L64) — 调用 `tfrt::CoreRuntime::Create(...)`，传入诊断处理器、`CreateMallocAllocator()` 分配器、移动进来的工作队列，以及默认 host 设备名 `kDefaultHostDeviceName`。

默认 host 设备名定义在文件顶部：

[tensorflow/core/tfrt/runtime/runtime.cc:35-36](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/runtime/runtime.cc#L35-L36) — `kDefaultHostDeviceName = "/job:localhost/replica:0/task:0/device:CPU:0"`。

`Runtime` 类本身的定义与「临时性」注释：

[tensorflow/core/tfrt/runtime/runtime.h:123-159](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/runtime/runtime.h#L123-L159) — 注释说明它「将被官方 `tensorflow::experimental::cc::Runtime` 取代」，并暴露 `core_runtime()`、`work_queue()` 两个 getter。

进程级单例的获取与设置：

[tensorflow/core/tfrt/runtime/runtime.h:241-249](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/runtime/runtime.h#L241-L249) — `GetGlobalRuntime()` 在未 `SetGlobalRuntime` 前返回 `nullptr`。

此外，`Runtime` 还提供一个**资源注入点** `AddCreateRuntimeResourceFn`：

[tensorflow/core/tfrt/runtime/runtime.h:162-184](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/runtime/runtime.h#L162-L184) — 允许在加载 SavedModel 时注入「按模型维度」的资源（如设备）创建回调。注释里说明这是临时方案，长期会被一个 Device 概念替代。

#### 4.1.4 代码实践

**实践目标**：从源码层面确认「TFRT 运行时 = `CoreRuntime` + 一个适配自 TF 线程池的工作队列」。

**操作步骤**：

1. 打开 `tensorflow/core/tfrt/runtime/runtime.cc`，定位 `Runtime::Create` 的两个重载。
2. 打开同目录 `tf_threadpool_concurrent_work_queue.h`，找到把 TF 的 `thread::ThreadPool` 包成 TFRT `ConcurrentWorkQueue` 的适配类。
3. 对照本节的流程图，确认：`Runtime` 持有 `core_runtime_` 与 `work_queue_` 两个成员。

**需要观察的现象**：`runtime.cc` 里 `Runtime` 的构造函数只接受一个 `CoreRuntime` 与一个裸指针 `WorkQueueInterface*`（见 [runtime.h:230-231](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/runtime/runtime.h#L230-L231)）。这说明 `CoreRuntime` 拿走了工作队列的**所有权**，而 `Runtime` 只保留一个裸指针用于后续访问——这是 TFRT「`HostContext` 管理工作队列生命周期」这一约定的体现。

**预期结果**：你能用一句话回答「`tfrt_stub::Runtime` 持有哪两样东西」——一个 `tfrt::CoreRuntime`（包含 `HostContext`）和一个指向工作队列的指针。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Runtime::Create` 要提供「传线程数」和「传工作队列」两个重载？

**参考答案**：前者是便捷入口，把「线程数 → TFRT 多线程工作队列」的默认策略固化下来，适合大多数场景；后者把工作队列的选择权完全交给调用方，使得 `TfrtSession` 可以传入自己定制的 `RunHandlerThreadWorkQueue`（一种按请求动态分配线程的高级队列，见 4.2 节）。这是典型的「便利构造 vs 完全控制」双入口设计。

**练习 2**：`GetGlobalRuntime()` 在未被初始化时返回什么？这暗示了怎样的使用约定？

**参考答案**：返回 `nullptr`。这暗示 `SetGlobalRuntime` 必须在使用前被显式调用一次（通常在进程初始化阶段），调用方需要自己处理「尚未设置」的情况，而不是依赖全局对象自动构造。

---

### 4.2 TfrtSession：把 TFRT 接入既有 Session 接口（core.tfrt.tfrt_session）

#### 4.2.1 概念说明

TFRT 想成为「新一代运行时」，但不能要求所有用户一夜之间改写代码。TF 的解决方案极其优雅：**让 TFRT 伪装成一个 `Session` 实现**。

回顾 [u3-l2](u3-l2-session-execution.md) 与 [u1-l5 版本信息与 C++ public 接口](u1-l5-version-and-public-api.md)：`Session` 是一个抽象基类，由 `SessionFactory` 按名字注册、由 `NewSession()` 经 `SessionFactory::GetFactory` 选用。`DirectSession` 就是注册名为 `"DIRECT_SESSION"` 的那个工厂。TFRT 只要：

1. 写一个 `TfrtSession : public tensorflow::Session`，实现 `Create/Run/Extend/Close` 等纯虚方法；
2. 写一个 `TfrtSessionFactory : public tensorflow::SessionFactory`，实现 `AcceptsOptions` 与 `NewSession`；
3. 用静态全局对象把它注册名为 `"tfrt_session"`。

这样一来，用户只要把 `SessionOptions.target` 设成 `"tfrt_session"`，或开启 `use_tfrt` 实验选项，拿到的 `Session*` 就是 TFRT 实现的，**上层 Python 代码完全无感**。

#### 4.2.2 核心流程

`TfrtSession` 的生命周期：

```text
NewSession(options)
   │  DeviceFactory::AddDevices  ← 复用既有设备发现
   │  TfrtSessionFactory::InitializeLocked
   │     └─ 若无外部 Runtime，则 Runtime::Create(RunHandlerWorkQueue) 造一个
   ▼
TfrtSession 构造，持有 runtime_ 指针
   │
session.Create(graph)
   │  FallbackState::CreateWithDeviceMgr(fdef_lib, device_manager)
   │     └─ 准备既有 TF 运行时状态（DeviceMgr、函数库、PFLR）
   │  GraphExecutor::Create(options, fallback_state, resource_context, graph, ...)
   │     └─ 把 graph 预处理（Placer 等），交给 GraphExecutor
   ▼
session.Run(inputs, outputs)
   │  构造本次请求的工作队列（intra/inter 线程池可按 Run 配置）
   │  graph_executor_->Run(...)   ← 进入 4.3 节
   ▼
outputs 填回
```

注意一个关键事实：`TfrtSession::Create` 会**创建一个 `FallbackState`**（见 4.4 节）。也就是说，即便走 TFRT，整张图的算子仍可能逐个回退到既有 TF 内核——这就是「回退」二字的由来。

#### 4.2.3 源码精读

工厂的 `AcceptsOptions` 决定了何时选用 `TfrtSession`：

[tensorflow/core/tfrt/tfrt_session/tfrt_session.cc:838-845](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/tfrt_session/tfrt_session.cc#L838-L845) — 当 `target == "tfrt_session"`，或 `target` 为空且开启了 `use_tfrt` 实验选项、或默认本地 Session 实现被全局设为 `kTfrtSession` 时受理。对照 [u3-l2](u3-l2-session-execution.md) 中 `DirectSession` 的 `AcceptsOptions`（target 空且非 TFRT 时受理），二者恰好互斥。

静态注册（与 `DirectSession` 完全同构的手法，见 u1-l5/u3-l2）：

[tensorflow/core/tfrt/tfrt_session/tfrt_session.cc:905-910](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/tfrt_session/tfrt_session.cc#L905-L910) — 借 C++ 静态全局对象在 `main` 前执行 `SessionFactory::Register("tfrt_session", session_factory)`。

`TfrtSession::CreateLocked` 是连接 TFRT 与既有 TF 的关键。它先造 `FallbackState`，再造 `GraphExecutor`：

[tensorflow/core/tfrt/tfrt_session/tfrt_session.cc:226-274](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/tfrt_session/tfrt_session.cc#L226-L274) — 用图的 `FunctionDefLibrary` 创建 `FallbackState`（L226-229），再调用 `GraphExecutor::Create(...)`（L270-274）。其中 L231-234 还注册了 MLRT 的内核注册表（`RegisterTfMlrtKernels`），为 4.3 节的字节码执行器铺路。

`TfrtSession::Run` 经过 `RunInternal` 把请求转交给 `GraphExecutor`：

[tensorflow/core/tfrt/tfrt_session/tfrt_session.cc:297-362](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/tfrt_session/tfrt_session.cc#L297-L362) — 重点看 L322-348：每次 `Run` 都按 `RunOptions` 决定本次请求的 inter-op 工作队列（可由调用方传入线程池，也可退化为单线程或默认池），然后把 `inputs/output_names` 交给 `graph_executor_->Run`（L351-353）。这与 `DirectSession` 每次 `Run` 重建/复用 `ExecutorsAndKeys` 的思路一致（见 u3-l2），但调度核心换成了 TFRT。

工厂初始化时若没有外部 `Runtime`，会自己造一个，并选用 `RunHandlerThreadWorkQueue` 这种「按请求动态分配线程」的高级队列：

[tensorflow/core/tfrt/tfrt_session/tfrt_session.cc:803-836](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/tfrt_session/tfrt_session.cc#L803-L836) — `InitializeLocked` 中 `Runtime::Create(CreateRunHandlerWorkQueue(...))`（L826-828）。`CreateRunHandlerWorkQueue` 在同文件 L591-627 定义，它区分 main / complementary 两类线程，是 TFRT 为「高并发推理」专门设计的。

#### 4.2.4 代码实践

**实践目标**：验证「`TfrtSession` 是通过 `SessionFactory` 注册机制透明替换 `DirectSession` 的」。

**操作步骤**：

1. 在 `tfrt_session.cc` 中搜索 `SessionFactory::Register`，确认注册名字为 `"tfrt_session"`。
2. 在 `tfrt_session.cc:838-845` 的 `AcceptsOptions` 里，列出会触发 TFRT 的三种条件。
3. 对照 [u3-l2 讲义](u3-l2-session-execution.md) 中 `DirectSession` 的 `AcceptsOptions`，写出二者如何「瓜分」`target` 为空的情况。

**需要观察的现象**：当 `target` 为空时，到底选 `DirectSession` 还是 `TfrtSession`，取决于 `use_tfrt()` 实验选项和 `GetDefaultLocalSessionImpl()` 的全局默认值——这是一个**运行期开关**，而不是编译期绑定。

**预期结果**：你能画出一张「`SessionOptions` → 工厂选择」的真值表：`target="tfrt_session"` → TFRT；`target=""` + `use_tfrt=true` → TFRT；否则 → DirectSession。这解释了为什么切换运行时**不需要重新编译用户代码**。

#### 4.2.5 小练习与答案

**练习 1**：`TfrtSession::Create` 为什么要构造 `FallbackState`？一个「新运行时」为何要带上旧运行时的状态？

**参考答案**：因为 TFRT 并没有为每一个 TF op 都重新写一份原生实现，绝大多数算子仍要回退到既有 `OpKernel` 执行（见 4.4 节）。而跑 `OpKernel` 需要 `DeviceMgr`、`ProcessFunctionLibraryRuntime`、函数库定义等既有 TF 状态——`FallbackState` 正是这些状态的容器。带着旧状态，TFRT 才能「调度用新的、计算用旧的」。

**练习 2**：`TfrtSessionFactory::AcceptsOptions` 与 `DirectSessionFactory::AcceptsOptions` 的判定必须保证不冲突。如果两者都受理同一个 `SessionOptions`，会发生什么？

**参考答案**：`SessionFactory::GetFactory` 在多工厂都受理时会按注册顺序或优先级裁决，可能导致行为不确定。因此两者被设计成对 `target` 为空的情况做「排他性」判定（`use_tfrt` 与默认本地实现开关），确保任意一份 `SessionOptions` 只被一个工厂受理。

---

### 4.3 GraphExecutor：编译 GraphDef 为 BEF/字节码并运行

> 本节严格说属于「把 `TfrtSession` 与 `Runtime` 衔接起来」的中间层（`core.tfrt.graph_executor`），是理解 4.4 节回退机制的前置。它解答一个核心问题：TFRT 拿到一张 GraphDef 之后，到底把它变成了什么、又用什么执行。

#### 4.3.1 概念说明

`GraphExecutor` 是 TFRT 的「图执行器」，职责是：给定一个 `GraphDef` 与一组输入输出，**按需编译并缓存**一个可执行的工件，然后用 TFRT 解释器跑它。它内部维护一张「`LoadedClientGraph` 缓存」，按输入输出名字的拼接键复用已编译产物——这与 `DirectSession` 用 `ExecutorsAndKeys` 缓存执行器的思路同源（见 u3-l2）。

关键的「双引擎」设计：编译产物有两种形态，对应两种执行器：

- **BEF 路径**：MLIR 模块经 `CompileMlirModuleToBef` 序列化成 `tfrt::BefBuffer`，加载成 `tfrt::BefFile`，由 TFRT 原生的 BEF 执行器解释运行。
- **MLRT 路径**：MLIR 模块 lower 成 TF 自己的字节码（`mlrt::LoadedExecutable`），由 MLRT 字节码解释器运行（`enable_mlrt` 选项控制）。

二者在加载期通过 `LoadedClientGraph::executable_context()->IsForMlrt()` 二选一。

#### 4.3.2 核心流程

加载并运行一张子图（`ClientGraph`）：

```text
GetOrCreateLoadedClientGraph(name, inputs, outputs)
   │  命中缓存？ ── 是 ──► 直接返回 LoadedClientGraph
   │  否
   ▼
ImportAndCompileClientGraph
   │  ImportClientGraphToMlirModule
   │     ├─ graph_execution_state_->CreateOptimizedGraph  ← 复用既有优化（Placer、Grappler）
   │     └─ tf2xla::v2::ConvertGraphToTfExecutor           ← 把 TF Graph 变成 tf_executor MLIR
   │  CompileMlirModuleToBef  或  lower 到 MLRT 字节码
   ▼
LoadClientGraph
   │  if (IsForMlrt())  InitBytecode()    ← 跑 _tfrt_fallback_init / _tfrt_resource_init
   │  else              InitBef()         ← 跑同样的两个初始化函数，但经 BEF 执行器
   ▼
GraphExecutionRunOnFunction
   │  CreateRequestInfo（构造本次请求的 ExecutionContext、工作队列等）
   │  if (loaded_executable)  RunMlrtFunction(...)        ← MLRT 路径
   │  else                    经 BEF 执行器运行 func      ← BEF 路径
   ▼
outputs
```

注意 `_tfrt_fallback_init` 与 `_tfrt_resource_init` 这两个特殊函数：它们由编译器生成，作用是在正式推理前**预先创建好所有需要回退的 op 与资源**（变量、资源句柄等），避免推理路径上的锁开销。这是 TFRT 为「热路径零开销」做的关键优化。

#### 4.3.3 源码精读

导入 GraphDef 到 MLIR 模块，复用了既有的图优化（Placer、Grappler），再转换成 `tf_executor` 方言：

[tensorflow/core/tfrt/graph_executor/graph_executor.cc:823-861](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/graph_executor/graph_executor.cc#L823-L861) — 重点看 L838-840 调用 `graph_execution_state_->CreateOptimizedGraph`（复用既有优化），以及 L853-857 调用 `tf2xla::v2::ConvertGraphToTfExecutor` 把优化后的图转成 MLIR。这与 [u6-l3 Grappler](u6-l3-grappler-optimizers.md) 的优化、[u7-l1 MLIR](u7-l1-mlir-tf-dialect.md) 的 dialect 概念直接衔接。

加载时按编译产物形态二选一地初始化：

[tensorflow/core/tfrt/graph_executor/graph_executor.cc:799-821](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/graph_executor/graph_executor.cc#L799-L821) — L810-813 是双引擎分叉点：`IsForMlrt()` 为真走 `InitBytecode`，否则走 `InitBef`。

BEF 路径的初始化（用 BEF 执行器跑两个特殊初始化函数）：

[tensorflow/core/tfrt/graph_executor/graph_executor.cc:863-891](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/graph_executor/graph_executor.cc#L863-L891) — L878-888 注释清楚说明：先跑 `_tfrt_fallback_init` 创建所有回退 op，再跑 `_tfrt_resource_init` 把资源写进运行时状态以实现「无锁高效检索」。

MLRT 路径的初始化（同样的两个函数，换执行器）：

[tensorflow/core/tfrt/graph_executor/graph_executor.cc:893-923](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/graph_executor/graph_executor.cc#L893-L923) — 与 `InitBef` 结构对称，只是把 `RunRuntimeInitializer` 换成 `RunMlrtFunction`。

请求执行时的双引擎分叉：

[tensorflow/core/tfrt/graph_executor/graph_executor.cc:394-405](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/graph_executor/graph_executor.cc#L394-L405) — `loaded_executable` 非空（MLRT 路径）则 `RunMlrtFunction`，否则走 BEF 路径。

`GraphExecutor` 类本身的公共接口与缓存：

[tensorflow/core/tfrt/graph_executor/graph_executor.h:146-336](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/graph_executor/graph_executor.h#L146-L336) — 关注 `Run`（L269）、`LoadedClientGraph`（L152，编译产物与运行状态的打包）、`ClientGraph`（L238，由 feed/fetch/target 圈定的子图描述）。

[tensorflow/core/tfrt/graph_executor/graph_executor.h:387-393](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/graph_executor/graph_executor.h#L387-L393) — `loaded_client_graphs_` 这个 `flat_hash_map` 就是「按输入输出名缓存已编译子图」的核心，键的拼接逻辑在 [graph_executor.cc:937-946](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/graph_executor/graph_executor.cc#L937-L946)。

#### 4.3.4 代码实践

**实践目标**：理解「同一张子图在 TFRT 下有两条编译/执行路径，但初始化逻辑是镜像对称的」。

**操作步骤**：

1. 打开 `graph_executor.cc`，把 `InitBef`（L863）与 `InitBytecode`（L893）并排对照阅读。
2. 列出二者都调用的两个魔法函数名（提示：在源码中以 `kFallbackInitFunction` / `kResourceInitFunction` 常量出现，分别对应 `_tfrt_fallback_init`、`_tfrt_resource_init`）。
3. 思考：为什么要把「创建回退 op」放在推理之前单独跑一次，而不是每次推理都创建？

**需要观察的现象**：两个初始化函数的内容几乎一模一样（构造 `RequestInfo` → 跑 `kFallbackInitFunction` → 跑 `kResourceInitFunction`），唯一区别是「用什么执行器跑这两个函数」。

**预期结果**：你能解释——把 op 创建提前到加载期一次性完成，是为了让**推理热路径**只剩纯粹的算子调度与计算，不再有创建/查找开销。这是 TFRT 相对 `DirectSession` 的一个重要性能设计。

#### 4.3.5 小练习与答案

**练习 1**：`GraphExecutor` 的 `loaded_client_graphs_` 缓存键是什么？为什么需要这个缓存？

**参考答案**：键是「排序后的输入名 + 输出名 + 目标名」拼接成的 `joined_name`（见 `GetOrCreateLoadedClientGraph`，L937-946），或显式的 `graph_name`（通常是 SavedModel 的 signature 名）。需要它是因为编译（MLIR 导入、Grappler、lower 到 BEF/字节码）开销很大，而同一组输入输出在反复推理时编译产物完全可复用。这与 `DirectSession` 缓存 `ExecutorsAndKeys` 是同一个动机。

**练习 2**：BEF 与 MLRT 是什么关系？为什么 TFRT 要维护两套执行器？

**参考答案**：BEF 是 TFRT 项目（`@tf_runtime`）自带的序列化格式与执行器，成熟但与 TFRT 项目绑定；MLRT 是 TF 自己实现的 MLIR 字节码解释器（`tensorflow/core/tfrt/mlrt/`），更贴近 TF 的需求、更易迭代，由 `enable_mlrt` 开关启用。二者是「新旧两种执行引擎」的过渡并存，最终目标是收敛到 MLRT。两者都复用同一套回退内核（4.4 节）。

---

### 4.4 内核回退 Kernel Fallback：用既有 OpKernel 干活（core.runtime_fallback）

#### 4.4.1 概念说明

这是理解整个 TFRT 的**钥匙**。TFRT 并没有、也不打算重写 TF 的全部算子。它的策略是：

- **调度（scheduling）**：由 TFRT 的 BEF/MLRT 解释器完成。解释器读字节码，按数据依赖（`AsyncValue` 的就绪关系）决定算子何时、在哪个线程执行，天然支持异步与流水线。
- **计算（compute）**：对没有 TFRT 原生实现的 op，**回退**到既有 TF `OpKernel`。具体做法是在 TFRT 的某个「OpHandler」（即 `RuntimeFallbackOpHandler`）里，把 TFRT 的张量转成 `tensorflow::Tensor`，构造一个 `OpKernelContext`，然后调用既有 `OpKernel::Compute`（回顾 [u4-l2 OpKernel 与 Compute 接口](u4-l2-opkernel-and-compute.md)）。

这样做的好处是「渐进迁移」：TFRT 可以先接管调度（拿到异步、流水线、低开销的好处），算子实现暂时复用既有内核，等某个 op 的原生实现成熟了再切换，无需推倒重来。

支撑这一切的是 `FallbackState`（4.2 节提到）：它持有既有 TF 运行时所需的 `DeviceMgr`、`DeviceSet`、`FunctionLibraryDefinition`、`ProcessFunctionLibraryRuntime`，因为跑 `OpKernel` 这些都缺一不可。

#### 4.4.2 核心流程

一次回退执行的链路：

```text
BEF/MLRT 解释器遇到一个需要回退的 op
   │
   ▼
命中 RuntimeFallbackOpHandler（一个 OpHandler 后端）
   │  把 AsyncValue<Tensor> 输入 → tensorflow::Tensor
   ▼
KernelFallbackExecuteCompatCoreRuntimeDispatch(op_name, device, args, results, ...)
   │  准备 OpKernelRunState（params：设备、op、属性）
   │  从 OpKernelRunnerCache 取得（或创建）对应的 OpKernel
   ▼
KernelFallbackExecuteCompatSyncInternal / ...AsyncInternal
   │  构造 OpKernelContext context(&params, num_outputs)
   │  kernel_runner.Run(&context)        ← 真正调用既有 OpKernel::Compute
   ▼
把 context 的 outputs 包装回 TFRT 的 AsyncValue<Tensor>
```

最关键的一行就是 `kernel_runner.Run(&context)`——它和 `DirectSession` 里 `Executor` 调 `device->Compute(op_kernel, &ctx)` 跑的是**同一份** `OpKernel` 代码。区别只在于「谁来调度这次调用」。

#### 4.4.3 源码精读

回退 OpHandler 的创建入口与职责说明：

[tensorflow/core/runtime_fallback/runtime/runtime_fallback_op_handler.h:16-34](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/runtime_fallback/runtime/runtime_fallback_op_handler.h#L16-L34) — 注释一句话点题：「responsible for running TFRT ops on Tensorflow」——即把 TFRT 的 op 跑到 TF 内核上。

回退执行的对内统一入口声明：

[tensorflow/core/runtime_fallback/kernel/kernel_fallback_execute.h:16-48](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/runtime_fallback/kernel/kernel_fallback_execute.h#L16-L48) — 注释「Provides a way to execute a TensorFlow kernel using TFRT kernel fallback」，`KernelFallbackExecute` 接收 TFRT 的 `ExecutionContext`、op 名、`AsyncValue*` 输入与属性，输出回 `AsyncValue`。

真正调用既有 `OpKernel` 的地方——同步版本：

[tensorflow/core/runtime_fallback/kernel/kernel_fallback_execute_compat.cc:231-255](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/runtime_fallback/kernel/kernel_fallback_execute_compat.cc#L231-L255) — L239 构造 `OpKernelContext context(&run_state->params, results.size())`，**L240 `kernel_runner.Run(&context)`** 就是真正触发既有 `OpKernel::Compute` 的那一行；随后 L249-252 把 `context.mutable_output(i)` 包装成 TFRT 的 `AsyncValue`。对照 [u4-l2 讲义](u4-l2-opkernel-and-compute.md) 中「`OpKernelContext` 是总线」的描述，这里完全复用了那套机制。

异步版本（异步 `OpKernel` 通过 `done_callback` 回填结果）：

[tensorflow/core/runtime_fallback/kernel/kernel_fallback_execute_compat.cc:159-224](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/runtime_fallback/kernel/kernel_fallback_execute_compat.cc#L159-L224) — L223 `kernel_runner.RunAsync(context_ptr, done_callback)`，`done_callback`（L196-221）在 op 完成后把输出搬进 `AsyncValue`。注意 L213 `tfrt::EnqueueWork` 把「搬运输出」扔回 TFRT 线程，体现 TFRT 与既有 TF 线程模型的协作。

`FallbackState` 持有的既有运行时状态：

[tensorflow/core/tfrt/fallback/fallback_state.h:38-100](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/tfrt/fallback/fallback_state.h#L38-L100) — 成员 `device_manager_`、`device_set_`、`func_lib_def_`、`pflr_`（L94-99）正是跑 `OpKernel` 所需的全部既有 TF 状态。注释（L38-39）说明它「contains the necessary runtime states used in current tensorflow」。

> 旁证：`tf_bef_executor_main.cc` 是仓库里自带的一个 BEF 执行器 demo，它用 `RunBefExecutor` 加载一个 BEF 文件并运行，并构造一个 fallback 执行上下文：

[tensorflow/core/runtime_fallback/tf_bef_executor_main.cc:59-72](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/core/runtime_fallback/tf_bef_executor_main.cc#L59-L72) — `RunBefExecutor` 的回调里，用 `resource_context` 取得一个 intra-op 线程池，再调 `CreateFallbackTestExecutionContext` 构造一个带回退能力的 `ExecutionContext`。这正是「BEF 执行器 + 回退内核」组合的最小可运行示例，也是本讲「综合实践」的切入点。

#### 4.4.4 代码实践

**实践目标**：亲眼确认「TFRT 的回退 = 复用既有 `OpKernel::Compute`」这一核心论断。

**操作步骤**：

1. 打开 `kernel_fallback_execute_compat.cc`，定位 `KernelFallbackExecuteCompatSyncInternal`（L231）。
2. 找到 `kernel_runner.Run(&context)`（L240），点进 `OpKernelRunner::Run`（在 `tensorflow/core/tfrt/fallback/op_kernel_runner.cc`），看它最终是否调用 `OpKernel` 的 `Compute`。
3. 对照 [u4-l2 讲义](u4-l2-opkernel-and-compute.md) 中 `device->Compute(op_kernel, &ctx)` 的传统调用方式，列出两者的相同点（同一个 `OpKernelContext`、同一个 `OpKernel` 子类）与不同点（调度方不同）。

**需要观察的现象**：`OpKernelContext` 在两条路径里**完全是同一个类型**，`mutable_output(i)`、`SetStatus`、`OP_REQUIRES` 这些 API 对算子作者来说完全不变。也就是说，一个用 `REGISTER_KERNEL_BUILDER` 写好的 CPU/GPU kernel，既能在 `DirectSession` 里跑，也能在 TFRT 回退里跑，**无需任何修改**。

**预期结果**：你能用一句话总结——「TFRT 把『谁来调度 op』这件事重写了，但『op 怎么算』完全复用既有内核」。这就是 TFRT 能渐进落地的根本原因。

**待本地验证**：若你本地能完整构建 TF，可尝试构建 `tf_bef_executor_main` 这个二进制目标，并用一个简单的 BEF 文件运行它，观察日志中 fallback 相关的初始化函数被执行的顺序。若无法构建，则以上为源码阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：既然每个算子最终都回退到既有 `OpKernel`，那 TFRT 的性能优势从何而来？

**参考答案**：优势不在「单个算子算得更快」，而在「调度层」：TFRT 以 `AsyncValue` 为单位做数据流驱动，能更细粒度地并行与流水线化算子间执行；线程池（如 `RunHandlerThreadWorkQueue`）针对高并发推理优化；加载期预创建回退 op 让热路径零开销；以及未来可逐步把热点 op 换成 TFRT 原生或 XLA 实现从而省去回退开销。简言之，先赢在调度，再逐步赢在计算。

**练习 2**：`FallbackState` 与 `TfrtSession` 的关系是什么？为什么不把它的内容直接放进 `Runtime`？

**参考答案**：`FallbackState` 是**按模型/按 Session** 的（持有该图的设备集、函数库、PFLR），而 `Runtime` 是**进程级单例**（一个 `CoreRuntime` 服务多个模型）。把按模型的状态放进进程级 `Runtime` 会造成模型间耦合与资源浪费，因此 `FallbackState` 由 `TfrtSession`（或 `GraphExecutor`）按实例持有，而 `Runtime` 只放跨模型共享的调度基础设施。这是「进程级 vs 模型级」的关注点分离。

---

## 5. 综合实践

**任务**：完成本讲规格中要求的核心实践——对照 `tfrt` 目录与 `runtime_fallback`，说明 BEF 执行器与传统 `DirectSession` 在执行模型上的主要不同，并指出 TFRT 试图解决什么问题。

**操作步骤**：

1. **重读 `DirectSession` 的执行链路**（复习 [u3-l2 讲义](u3-l2-session-execution.md)）：回忆「剪枝 → 放置 → 优化 → 分区 → `Executor` 按拓扑调度 → `Rendezvous` 传张量」这条链路，以及 `ExecutorsAndKeys` 的缓存机制。
2. **梳理 TFRT 的执行链路**（综合本讲 4.1–4.4）：`TfrtSession::Create` 造 `FallbackState` + `GraphExecutor`；首次 `Run` 触发 `GetOrCreateLoadedClientGraph`，把 GraphDef 经 MLIR 编译成 BEF/字节码；之后每次 `Run` 用 TFRT 解释器（BEF 或 MLRT）跑字节码，遇到需回退的 op 经 `RuntimeFallbackOpHandler` 调既有 `OpKernel`。
3. **填写对比表**（建议自己画在纸上或笔记里）：

   | 维度 | DirectSession（传统） | TFRT（BEF/MLRT 执行器） |
   | --- | --- | --- |
   | 调度核心 | 自研 `Executor`，按节点就绪状态 + 线程池 | TFRT 解释器，以 `AsyncValue` 就绪关系做数据流驱动 |
   | 数据传递 | `Rendezvous` 显式 `Send`/`Recv` 配对 | `AsyncValue` 引用传递，天然异步 |
   | 跨设备 | 显式插入 `_Send`/`_Recv` 节点 | `OpHandler` + 设备流抽象（stream） |
   | 算子计算 | `device->Compute(op_kernel, &ctx)` | 回退到**同一个** `OpKernel::Compute`（`kernel_runner.Run`） |
   | 线程池 | inter-op 固定线程池 | 可按请求分配（`RunHandlerThreadWorkQueue`） |
   | 编译产物 | 运行时 `Graph*` + 分区子图 | 预编译的 BEF/字节码，加载期预创建回退 op |
   | 接入方式 | `SessionFactory` 注册为 `"DIRECT_SESSION"` | `SessionFactory` 注册为 `"tfrt_session"`，同一 `Session` 接口 |

4. **回答「TFRT 试图解决什么」**：用一段话写清——TFRT 把运行时拆成可组合的、以异步数据流为核心的抽象（`HostContext`/`CoreRuntime`/`OpHandler`/`AsyncValue`/BEF），让调度、设备、计算解耦；它用「回退到既有 `OpKernel`」实现渐进迁移，用「加载期预创建 + 热路径零开销」与「按请求分配线程池」提升推理吞吐，最终目标是既能容纳 CPU/GPU/TPU 与 XLA 等多后端，又能逐步用原生实现替换回退。

**需要观察的现象**：你会清楚地看到，TFRT 与 `DirectSession` 共享了「`OpKernel` 怎么算」「`SessionFactory` 怎么注册」「Placer/Grappler 怎么优化」这一整层既有资产，**真正的差异只在调度核心与数据传递方式**。

**预期结果**：你能用三句话向同事讲清 TFRT——(1) 它是一个以异步数据流为核心的新运行时，通过 `SessionFactory` 透明替换 `DirectSession`；(2) 它复用既有 `OpKernel` 做计算（回退），自己只重写调度；(3) 它用 BEF/MLRT 两套解释器、`RunHandler` 线程池、加载期预创建等手段追求高并发推理的低开销。

**待本地验证**：完整构建 TFRT 相关目标（如 `tf_bef_executor_main`）需要较重的 Bazel 环境；若本地不具备，本实践以源码追踪与对比分析为准，不必强行运行。

---

## 6. 本讲小结

- **TFRT 是「新一代运行时」**，以 `HostContext` / `CoreRuntime` / `OpHandler` / `AsyncValue` / BEF 为核心抽象，目标是把调度、设备、计算解耦，支持异步与多后端；TF 侧用 `tfrt_stub::Runtime` 把 `CoreRuntime` 包起来，并提供进程级单例。
- **`TfrtSession` 用 `SessionFactory` 静态注册**（名 `"tfrt_session"`），伪装成一个 `Session` 实现，让上层代码无感切换；`AcceptsOptions` 与 `DirectSession` 排他性地瓜分 `target` 为空的情况。
- **`GraphExecutor` 是编译+运行的中间层**：把 GraphDef 经既有优化（Placer/Grappler）导入 MLIR，再编译成 BEF 或 MLRT 字节码，按输入输出名缓存 `LoadedClientGraph`，并在加载期跑 `_tfrt_fallback_init`/`_tfrt_resource_init` 预创建回退 op。
- **「内核回退」是理解 TFRT 的钥匙**：TFRT 自己只做调度，算子计算委托给既有 `OpKernel`——在 `kernel_fallback_execute_compat.cc` 里实例化 `OpKernelContext` 并调用 `kernel_runner.Run(&context)`，跑的是和 `DirectSession` 完全相同的内核代码。
- **`FallbackState` 承载既有运行时状态**（`DeviceMgr`/`DeviceSet`/函数库/PFLR），按模型实例持有，与进程级 `Runtime` 形成「模型级 vs 进程级」的关注点分离。
- **BEF/MLRT 解释器与 `DirectSession` 的 `Executor` 的根本差异**在于调度核心与数据传递：前者以 `AsyncValue` 就绪关系做数据流驱动，后者按节点就绪状态 + `Rendezvous` 显式配对。

---

## 7. 下一步学习建议

- **回到调度细节**：阅读 `tensorflow/core/tfrt/runtime/work_queue_interface.h` 与 `tf_threadpool_concurrent_work_queue.cc`，理解 TF 线程池如何被适配成 TFRT 的 `ConcurrentWorkQueue`，对比 `DirectSession` 的 inter-op 线程池模型。
- **深入回退热路径**：读 `tensorflow/core/tfrt/fallback/op_kernel_runner.cc` 与 `op_kernel_runner_cache.cc`，看清「加载期把 `OpKernel` 预先创建并缓存」的实现，理解热路径零开销的具体落地。
- **MLRT 字节码**：浏览 `tensorflow/core/tfrt/mlrt/`（`bytecode/`、`interpreter/`、`kernel/`），理解与 BEF 执行器并列的第二种执行引擎的字节码与解释器结构。
- **连接编译器**：本讲止步于「GraphDef → MLIR → BEF」的入口，建议接着读 [u7-l1 MLIR 与 TF dialect](u7-l1-mlir-tf-dialect.md) 与 [u7-l2 XLA / StableHLO](u7-l2-xla-stablehlo-tf2xla.md)，理解 `ConvertGraphToTfExecutor` 之后的 bridge pass 流水线如何把 `tf_executor` 方言 lower 到可执行形态。
- **设备与流**：结合 [u6-l1 Device 与 DeviceFactory](u6-l1-device-and-devicefactory.md)，对照 TFRT 的 `OpHandler` 与 `tensorflow/core/tfrt/runtime/stream.h`，体会 TFRT 如何用「流（stream）」抽象替代既有的 `_Send`/`_Recv` 跨设备机制。
