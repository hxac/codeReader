# TFRT 新一代运行时

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 **TFRT（TensorFlow Runtime）** 想解决什么问题，它与传统 `DirectSession` 运行时在设计理念上的根本差异。
- 沿着「`Runtime` → `TfrtSession` → `GraphExecutor` → `runtime_fallback`」这条链路，读懂 TFRT 把一张 `GraphDef` 编译成可执行产物并跑出结果的全过程。
- 理解 **BEF / MLRT 字节码** 与 **fallback（回退）执行** 两个核心概念，明白为什么 TFRT 能在没有原生 kernel 时透明地复用老的 TF `OpKernel`。
- 对照源码，判断 TFRT 在当前仓库里是「默认关闭、按需开启」的，并知道开启它的几种入口。

本讲属「编译器与运行时」单元，是 u3-l2（`DirectSession` 执行链路）与 u6-l1（`Device`/`DeviceFactory`）的对照与延伸——前者讲「老运行时怎么跑图」，本讲讲「新一代运行时想怎么跑图」。

## 2. 前置知识

在进入源码前，先用三段通俗的话建立直觉。

**第一，什么是「运行时」。** 你在 Python 里写 `tf.constant`、`tf.matmul`，最终都会变成一张计算图（见 u3-l1）。图本身只是一份「说明书」，描述了谁连到谁；真正要把这张图跑起来、调度 op、分配内存、把结果送回 Python，需要一套执行引擎，这就是「运行时」。u3-l2 讲的 `DirectSession` 就是 TF 历史最久、默认使用的本地运行时。

**第二，为什么需要「新一代」。** `DirectSession` 是一个「大而全」的单体执行器：放置、剪枝、分区、优化、调度、kernel 执行都揉在一起，且以同步、命令式调度为主。这在工程上带来两个痛点：一是难以针对**异步执行**（尤其是 GPU/TPU 的流式并行）做深度优化；二是它与 TF 的具体 op 体系耦合过深，难以被裁剪复用到端侧、服务端等不同场景。TFRT 的目标就是把「运行时」重新拆成一组**可组合、异步优先、与具体 op 解耦**的组件，让同一套基础设施既能服务训练也能服务推理。

**第三，TFRT 的两个关键词。**

- **异步优先（async-first）**：TFRT 内部以 `AsyncValue`（一个尚不可用的值，将来会被填充）为基本数据单元，op 一旦输入就绪就立即异步调度，而不是像传统执行器那样按拓扑序一拍一拍同步推进。
- **BEF（Bytecode Executor Format）**：TFRT 不直接解释 `GraphDef`，而是先把图编译成一种紧凑的字节码（BEF），再由一个轻量字节码执行器解释。这把「图」从 protobuf 对象变成了可序列化、可预编译的产物，类似 JVM 与 `.class` 文件的关系。

> 名词澄清：本讲还会出现 **MLRT**（一种更新的、进程内字节码解释器）和 **fallback**（回退）。它们都是在 BEF 之后陆续加入的演进，后文会逐一精读。

## 3. 本讲源码地图

本讲涉及的关键目录与文件如下：

| 文件 / 目录 | 作用 |
| --- | --- |
| `tensorflow/core/tfrt/runtime/runtime.h` | TFRT 在 TF 侧的运行时抽象 `tfrt_stub::Runtime`，包装上游 `tfrt::CoreRuntime` 与工作队列。 |
| `tensorflow/core/tfrt/runtime/work_queue_interface.h` | `WorkQueueInterface`，把 TF 的线程池注入 TFRT 的抽象接口。 |
| `tensorflow/core/tfrt/tfrt_session/tfrt_session.h` / `.cc` | `TfrtSession` 与 `TfrtSessionFactory`：让 TFRT 伪装成一个普通 `tensorflow::Session`。 |
| `tensorflow/core/tfrt/graph_executor/graph_executor.h` | `GraphExecutor`：编译图（`GraphDef → MLIR → BEF/MLRT 字节码`）并执行的核心引擎。 |
| `tensorflow/core/runtime_fallback/tf_bef_executor_main.cc` | 一个 BEF 执行器驱动二进制，用来直接跑一份 BEF 文件，是观察 TFRT 执行的最小入口。 |
| `tensorflow/core/runtime_fallback/kernel/kernel_fallback_execute.h` | `KernelFallbackExecute`：当某 op 没有 TFRT 原生 kernel 时，回退到老 `OpKernel` 的桥梁。 |
| `tensorflow/core/runtime_fallback/runtime/static_registration.cc` | 用静态构造对象在启动期自动注册 fallback kernels（与 u4-l1 的 `REGISTER_OP` 同构）。 |
| `tensorflow/core/common_runtime/direct_session.cc` / `local_session_selection.cc` | 对照组：`DirectSession` 如何与 `TfrtSession` 在工厂里「抢」同一个本地 Session。 |

整体调用栈（自顶向下）：

```
用户 Python: session.run(...)
        │
   TfrtSession (实现 tensorflow::Session 接口)
        │  Create() / Run()
   GraphExecutor (编译 + 执行)
        │  GraphDef → MLIR → BEF/MLRT 字节码
   tfrt::CoreRuntime + WorkQueueInterface (由 tfrt_stub::Runtime 持有)
        │
   ┌────┴────────────────────┐
   │ 原生 TFRT kernel        │ 没有 native kernel？
   │ (异步 AsyncValue)        │
   └─────────────────────────┘
        │
   KernelFallbackExecute → 老的 tensorflow::OpKernel::Compute
```

## 4. 核心概念与源码讲解

### 4.1 TFRT 的设计动机与整体形态

#### 4.1.1 概念说明

TFRT 不是「替换 `DirectSession` 的又一个 Session」，而是一套**可重用的运行时基础设施**。它的设计目标可以归纳为三点：

1. **异步优先**：用 `AsyncValue` 串起整条数据流，让设备（GPU/TPU）的计算与主机调度最大程度重叠。
2. **可组合**：把「执行器」「工作队列」「设备」「kernel 注册表」拆成独立组件，按需拼装；同一套内核既能做训练也能做推理。
3. **与 op 解耦 + 渐进迁移**：TFRT 允许部分 op 用「原生 TFRT kernel」，部分 op 通过 **fallback** 透明地走老 `OpKernel`。这样不必等所有 op 都重写就能上线。

为了让用户**几乎无感知**地切换，TFRT 被包装成了与 `DirectSession` 同级的 `TfrtSession`——它们都实现 `tensorflow::Session` 接口（见 u1-l5、u3-l2）。也就是说，从 Python 侧看 `session.run()` 没有任何变化，变的只是工厂选了哪个实现。

#### 4.1.2 核心流程

TFRT 跑一张图的大致流程：

1. **建运行时**：进程启动时创建一个 `tfrt_stub::Runtime`（内含 `tfrt::CoreRuntime` 与工作队列）。
2. **建 Session**：`TfrtSessionFactory::NewSession` 造出一个 `TfrtSession`，持有上面的 Runtime。
3. **Create（建图）**：`TfrtSession::Create` 把 `GraphDef` 交给 `GraphExecutor::Create`。
4. **编译**：`GraphExecutor` 把 `GraphDef` 翻译成 MLIR（TF dialect），再做一系列 lowering，最后产出 **BEF 缓冲区** 或 **MLRT 字节码**。
5. **Run（执行）**：`GraphExecutor::Run` 用 BEF 执行器 / MLRT 解释器解释这段字节码；遇到无原生 kernel 的 op，经 fallback 调老 `OpKernel`。
6. **回收**：结果张量回填给 `TfrtSession::Run`，再按 Session 契约返回 Python。

#### 4.1.3 源码精读

最能体现「TFRT 是可独立运行的运行时」的，是仓库里自带的一个驱动二进制 `tf_bef_executor_main.cc`。它不经过任何 Session，直接读一份 BEF 文件并执行：

[tensorflow/core/runtime_fallback/tf_bef_executor_main.cc:33-73](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/runtime_fallback/tf_bef_executor_main.cc#L33-L73) —— `main` 把命令行参数（输入文件、共享库、函数名、工作队列类型）组装成 `RunBefConfig`，再调 `RunBefExecutor`。注意最后那个回调：它创建了一个 `HostContext` 资源 `TfThreadPool`，然后用 `CreateFallbackTestExecutionContext` 构造出执行上下文。这说明 **BEF 文件本身已经是一份可独立加载、可独立执行的产物**，与 Python、Session 无关——这正是「运行时」该有的样子。

关键片段（去掉参数解析后）：

```cpp
tfrt::RunBefConfig run_config;
run_config.input_filename = input_filename;
run_config.work_queue_type = absl::GetFlag(FLAGS_work_queue_type);
run_config.host_allocator_type = absl::GetFlag(FLAGS_host_allocator_type);

return RunBefExecutor(run_config, [](tfrt::HostContext* host, ...) {
  // 构造一个 fallback 执行上下文，模拟生产环境的 intra-op 线程池
  return tensorflow::tfd::CreateFallbackTestExecutionContext(host, ...);
});
```

这段代码把「字节码 + HostContext + 工作队列」三件套凑齐就能跑，是理解 TFRT 整体形态的最小样例。

#### 4.1.4 代码实践

**实践目标**：不运行任何东西，仅通过阅读确认「BEF 是独立可执行产物」这个判断。

**操作步骤**：

1. 打开 [tf_bef_executor_main.cc:36-55](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/runtime_fallback/tf_bef_executor_main.cc#L36-L55)。
2. 找到 `FLAGS_input_filename`、`FLAGS_shared_libs`、`FLAGS_functions` 三个 absl flag 的使用点。
3. 观察它们都被填进 `run_config`，最后交给 `RunBefExecutor`。

**需要观察的现象**：整个 `main` 里没有任何 `tensorflow::Session`、没有 `GraphDef`、没有 `NewSession`——输入就是一份 BEF 文件。

**预期结论**：TFRT 的执行层是「字节码 → 解释器」，与上层 Session 解耦；Session 只是给这套执行层套了一个符合老接口的外壳。

**运行结果**：本实践为源码阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`tf_bef_executor_main.cc` 里的 `work_queue_type` 和 `host_allocator_type` 两个参数说明了 TFRT 的什么设计取向？

> **答案**：说明执行器、工作队列（线程池策略）、主机内存分配器都是**可插拔**的组件，由调用方按需选择，而不是写死在运行时里——这是「可组合运行时」的体现。

**练习 2**：为什么 TFRT 选择把图编译成 BEF 字节码，而不是像 `DirectSession` 那样直接在内存的 `Graph` 对象上调度？

> **答案**：BEF 是一份预编译的、可序列化的产物，避免了每次执行都重新解释 `GraphDef`；同时它把「编译期」与「执行期」彻底分开，使执行器可以做得非常轻量、专注于异步调度。

---

### 4.2 `tfrt_stub::Runtime`：TFRT 在 TF 侧的运行时抽象（core.tfrt.runtime）

#### 4.2.1 概念说明

`Runtime` 是 TF 在 C++ 侧为 TFRT 定义的「运行时入口」抽象，位于 `tensorflow::tfrt_stub` 命名空间。它的注释直言不讳地写着「It is temporary」（它是临时的），最终会被官方的 `tensorflow::experimental::cc::Runtime` 取代。但当前它就是承载 TFRT 运行时能力的核心对象。

它对外只暴露两件最关键的事：

1. **创建运行时实例**（带可配置的线程数 / 工作队列）。
2. **创建张量**（TODO，注释里还标注着「待实现」）。

其内部真正干活的是上游 `tf_runtime` 项目（`@tf_runtime` 外部仓库）的 `tfrt::CoreRuntime`。

#### 4.2.2 核心流程

`Runtime` 的生命周期：

1. **构造**：`Runtime::Create(num_inter_op_threads, num_intra_op_threads)` 先造工作队列，再据此构造 `tfrt::CoreRuntime`。
2. **注入 per-model 资源**：通过 `AddCreateRuntimeResourceFn` 注册若干回调；加载一个 SavedModel 时，依次调用这些回调，把设备等「系统级资源」注入到该模型的 `ResourceContext` 里。
3. **per-request 工作队列**：每次推理请求可以经 `CreateRequestQueue` 拿到一个独立工作队列，实现请求间隔离。
4. **全局单例**：`GetGlobalRuntime` / `SetGlobalRuntime` 提供进程级单例，供 `TfrtSession` 取用。

#### 4.2.3 源码精读

`Runtime` 类的定义与设计意图，在头文件注释里写得很清楚：

[tensorflow/core/tfrt/runtime/runtime.h:123-160](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/runtime/runtime.h#L123-L160) —— 类注释说明它「只用于创建运行时实例与创建张量」，并明示它是临时方案。注意两个工厂方法：

```cpp
static std::unique_ptr<Runtime> Create(int num_inter_op_threads,
                                       int num_intra_op_threads = 0);
static std::unique_ptr<Runtime> Create(
    std::unique_ptr<WorkQueueInterface> work_queue,
    std::function<void(const tfrt::DecodedDiagnostic&)> diag_handler =
        LogOnError);
```

[tensorflow/core/tfrt/runtime/runtime.h:159-160](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/runtime/runtime.h#L159-L160) —— 暴露出的两个 getter：`core_runtime()` 取上游 `tfrt::CoreRuntime`，`work_queue()` 取工作队列。

全局单例与 per-model 资源注入：

[tensorflow/core/tfrt/runtime/runtime.h:172-196](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/runtime/runtime.h#L172-L196) —— `AddCreateRuntimeResourceFn` 把「创建设备等资源」的回调收进一个列表；`CreateRuntimeResources` 在加载模型时遍历调用它们，参数是一个 `ModelRuntimeContext`（封装了 `GraphExecutionOptions`、`ResourceContext`、设备管理器等模型级状态）。

[tensorflow/core/tfrt/runtime/runtime.h:244-249](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/runtime/runtime.h#L244-L249) —— 进程级单例 `GetGlobalRuntime` / `SetGlobalRuntime`。

工作队列的抽象层很重要——它把 TF 自己的线程池「翻译」给 TFRT：

[tensorflow/core/tfrt/runtime/work_queue_interface.h:41-54](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/runtime/work_queue_interface.h#L41-L54) —— `WorkQueueInterface` 继承自上游 `tfrt::ConcurrentWorkQueue`，但额外携带了 `intra_op_threadpool_`（intra-op 线程池）与一个 `id_`。头注释说得很明白：「这是 TF 与 TFRT core 之间的中间接口，让我们能加入 savedmodel/tensorflow 特有的方法（如创建 intra-op 线程池），而不必改动 TFRT 核心」。

#### 4.2.4 代码实践

**实践目标**：理解 `Runtime` 如何把 TF 的线程池注入 TFRT。

**操作步骤**：

1. 打开 `runtime.h`，找到两个 `Create` 重载（L139 与 L144）。
2. 打开 `work_queue_interface.h`，找到 `intra_op_threadpool_` 字段（L46）。
3. 在仓库里搜索 `CreateRunHandlerWorkQueue`（出现在 `tfrt_session.cc` 的 `InitializeLocked` 中），观察它是如何被传给 `Runtime::Create` 的。

**需要观察的现象**：`TfrtSession` 在初始化时，用一个基于 `RunHandler` 的线程池构造 `Runtime`，而这个线程池正是 TF 已有的 intra-op 调度器。

**预期结果**：你能画出一条「TF 线程池 → `WorkQueueInterface` → `tfrt::CoreRuntime`」的接线，说明 TFRT 复用了 TF 的线程调度基础设施，而非另起炉灶。

**运行结果**：源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`Runtime` 头注释说它是「temporary」，最终会被谁取代？这反映了 TFRT 演进的什么状态？

> **答案**：会被 `tensorflow::experimental::cc::Runtime` 取代。这反映 TFRT 仍在演进中，当前 `tfrt_stub::Runtime` 是过渡形态，最终接口尚未冻结。

**练习 2**：`AddCreateRuntimeResourceFn` 注册的回调为什么要求「thread-safe」？

> **答案**：因为 `CreateRuntimeResources` 可能在加载多个模型、处理多个请求时被并发调用，回调若非线程安全会引发数据竞争。

---

### 4.3 `TfrtSession`：把 TFRT 装进 Session 接口（core.tfrt.tfrt_session）

#### 4.3.1 概念说明

`TfrtSession` 是 TFRT 与既有 TensorFlow 体系之间的**适配层**。它继承自 `tensorflow::Session`（u1-l5 讲过这个抽象基类，契约是 `Create/Extend/Run/...`），所以从 Python 或 C++ 客户端看，它和一个 `DirectSession` 没有区别；但它的内部完全交给 `GraphExecutor` 与 TFRT 运行时。

它由 `TfrtSessionFactory` 生产。这个工厂同样用 u6-l1 见过的「静态全局对象自动注册」手法接入 `SessionFactory::Register("tfrt_session", ...)`。

#### 4.3.2 核心流程

**工厂裁决**：`NewSession(SessionOptions)` 时，`SessionFactory::GetFactory` 会依次问每个已注册工厂的 `AcceptsOptions`。`TfrtSessionFactory::AcceptsOptions` 在三种情况下受理：

1. `options.target == "tfrt_session"`（显式点名）。
2. `options.target` 为空 **且** `config.experimental().use_tfrt()` 为真。
3. `options.target` 为空 **且** 默认本地 Session 实现被设为 `kTfrtSession`。

而 `DirectSessionFactory::AcceptsOptions` 恰好是「target 为空 **且** `!use_tfrt()` **且** 默认实现是 `kDirectSession`」。二者构成互补：**默认走 DirectSession，TFRT 需显式开启**。

**Create 阶段**：`TfrtSession::Create` 做四件事——构造 `GraphExecutionOptions`、用原图的函数库造 `FallbackState`（回退所需的老 runtime 状态）、注册 MLRT kernels、最后 `GraphExecutor::Create`。

**Run 阶段**：把 fetches/feed_dict 翻成 `GraphExecutor::Run` 的输入，由后者完成编译缓存查找与执行。

#### 4.3.3 源码精读

工厂与配置选项：

[tensorflow/core/tfrt/tfrt_session/tfrt_session.h:67-75](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/tfrt_session/tfrt_session.h#L67-L75) —— `TfrtSessionFactory` 继承 `SessionFactory`，声明了 `AcceptsOptions` 与 `NewSession`。`TfrtSessionOptions`（L52）里的 `runtime`、`use_tpu`、`use_gpu`、`enable_mlrt` 等开关决定了走哪条执行路径。

`AcceptsOptions` 的实现是理解「何时启用 TFRT」的钥匙：

[tensorflow/core/tfrt/tfrt_session/tfrt_session.cc:838-845](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/tfrt_session/tfrt_session.cc#L838-L845)

```cpp
bool TfrtSessionFactory::AcceptsOptions(const SessionOptions& options) {
  if (options.target == "tfrt_session") return true;
  if (options.target.empty()) {
    return options.config.experimental().use_tfrt() ||
           GetDefaultLocalSessionImpl() == LocalSessionImpl::kTfrtSession;
  }
  return false;
}
```

对照 [tensorflow/core/common_runtime/direct_session.cc:209-213](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/common_runtime/direct_session.cc#L209-L213)，可以看到 `DirectSession` 的受理条件正好是 `target.empty() && !use_tfrt() && 默认==kDirectSession`。再配合 [tensorflow/core/common_runtime/local_session_selection.cc:20-21](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/common_runtime/local_session_selection.cc#L20-L21)（`default_local_session = kDirectSession`），可知**当前仓库默认仍是 DirectSession，TFRT 是 opt-in**。

`NewSession` 的关键步骤：

[tensorflow/core/tfrt/tfrt_session/tfrt_session.cc:847-880](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/tfrt_session/tfrt_session.cc#L847-L880) —— 注意 L862 调 `DeviceFactory::AddDevices` 收集设备（承接 u6-l1），L874 `new TfrtSession(...)` 把 runtime、设备管理器、各种开关传入。L866-868 还会跑 `InitializerRegistry`，说明 TFRT 也有自己的初始化钩子链。

`Create` 阶段如何把图交给 `GraphExecutor`：

[tensorflow/core/tfrt/tfrt_session/tfrt_session.cc:231-274](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/tfrt_session/tfrt_session.cc#L231-L274) —— 这里做了三件值得注意的事：

```cpp
auto kernel_registry = std::make_unique<mlrt::KernelRegistry>();
tensorflow::tf_mlrt::RegisterTfMlrtKernels(*kernel_registry);       // 注册原生 MLRT kernel
tensorflow::tf_mlrt::RegisterTfMlrtBatchKernels(*kernel_registry);
...
TF_ASSIGN_OR_RETURN(
    graph_executor_,
    tensorflow::tfrt_stub::GraphExecutor::Create(
        options, std::move(fallback_state), std::move(resource_context),
        std::move(graph), std::move(kernel_registry)));             // 交给 GraphExecutor
```

`Run` 阶段非常薄：

[tensorflow/core/tfrt/tfrt_session/tfrt_session.cc:351-353](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/tfrt_session/tfrt_session.cc#L351-L353) —— 直接转发给 `graph_executor_->Run(...)`。说明 `TfrtSession` 本身确实只是个「外壳」，重活都在 `GraphExecutor`。

工厂的静态自动注册：

[tensorflow/core/tfrt/tfrt_session/tfrt_session.cc:905-910](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/tfrt_session/tfrt_session.cc#L905-L910) —— 一个静态 bool 变量在 `main` 之前构造，`new TfrtSessionFactory()` 并调 `SessionFactory::Register("tfrt_session", ...)`，与 u6-l1 的 `REGISTER_LOCAL_DEVICE_FACTORY` 完全同构。

#### 4.3.4 代码实践

**实践目标**：通过修改一行配置，观察 Session 工厂的裁决结果（纯源码阅读 + 可选运行）。

**操作步骤**：

1. 对照 `tfrt_session.cc:838-845` 与 `direct_session.cc:209-213`，填写下表，判断每种 `SessionOptions` 下哪个工厂受理。

   | `target` | `use_tfrt()` | `GetDefaultLocalSessionImpl()` | 谁受理 |
   | --- | --- | --- | --- |
   | `""` | false | `kDirectSession` | ? |
   | `""` | true | `kDirectSession` | ? |
   | `"tfrt_session"` | false | `kDirectSession` | ? |
   | `""` | false | `kTfrtSession` | ? |

2. （可选运行，**待本地验证**）在已编译了 TFRT 的 TF 环境里运行以下示例代码，观察日志中是否出现 `"Registering TfrtSession"` 与 `"Start initializing TfrtSession"`：

   ```python
   # 示例代码：显式点名 tfrt_session
   import tensorflow as tf
   c = tf.constant([[1., 2.], [3., 4.]])
   # 注意：以下为示意，能否真正走 TFRT 取决于该 TF 是否编译并启用了 TFRT
   with tf.compat.v1.Session(target="tfrt_session") as sess:
       print(sess.run(c))
   ```

**需要观察的现象**：第 2 步若 TFRT 未编译，会报 `No session factory registered for the given session options` 之类的错误；这正是「TFRT 默认关闭」的实证。

**预期结果**：第 1 步答案依次是 DirectSession、TfrtSession、TfrtSession、TfrtSession。

**运行结果**：第 1 步可立即得出；第 2 步「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TfrtSession` 与 `DirectSession` 的 `AcceptsOptions` 在 `target.empty()` 这一支上必须是「互补」关系？

> **答案**：`NewSession` 时 `SessionFactory::GetFactory` 会遍历所有工厂，若两个工厂对同一组 `SessionOptions` 都返回 true，就会产生歧义。互补的设计保证任何本地 Session 选项都有且只有一个工厂受理。

**练习 2**：`TfrtSession::Create` 里为什么要单独构造一个 `FallbackState`？

> **答案**：因为 TFRT 并非所有 op 都有原生 kernel，需要保留一份「老 TF runtime 状态」（含函数库、设备管理器），以便在执行期对无原生 kernel 的 op 回退到老 `OpKernel`（见 4.5）。

---

### 4.4 `GraphExecutor`：从图到字节码的编译与执行（core.tfrt.graph_executor）

#### 4.4.1 概念说明

`GraphExecutor` 是 TFRT 的「施工队长」：它接收一张 `GraphDef`，负责把它编译成 TFRT 能执行的产物，并在每次 `Run` 时查找/执行已编译的子图。它是 `TfrtSession` 与底层 TFRT 执行器之间的中间层，也是最能体现「编译期 vs 执行期分离」的地方。

它管理两类核心产物：

- **BEF 缓冲区**（`tfrt::BefBuffer`）：经典的 TFRT 字节码格式，由 MLIR 模块经 `CompileMlirModuleToBef` 产出，用上游 `tfrt::BEFExecutor` 执行。
- **MLRT 字节码**（`mlrt::bc::Executable`）：一种更新的、进程内字节码解释器产物（见 `core/tfrt/mlrt/`），由 `ConvertTfMlirToBytecode` 产出，用 `mlrt::LoadedExecutable` 执行。

二者通过 `options.enable_mlrt` 这个开关二选一。

#### 4.4.2 核心流程

`GraphExecutor::Run` 的大致步骤：

1. **解析 ClientGraph**：把输入/输出张量名翻译成一个 `ClientGraph`（feed/fetch/target 节点集合）。
2. **查找/创建 LoadedClientGraph**：以「排序后的输入输出名拼接出的 joined_name」为键查缓存（`loaded_client_graphs_`），命中则复用。
3. **未命中则 ImportAndCompile**：`GraphDef → MLIR(TF dialect)` → lowering → BEF 或 MLRT 字节码。
4. **执行**：`GraphExecutionRunOnFunction` 或 `RunWithSyncInterpreter` 拿到字节码解释器跑一遍，产出输出张量。
5. （可选）**在线代价分析**：用 `CostRecorder` 记录 op 耗时，必要时触发重编译。

`LoadedClientGraph` 是「一个被请求过的子图 + 它的编译产物 + 运行期状态」的打包对象。

#### 4.4.3 源码精读

`GraphExecutor` 类与它的两个执行入口：

[tensorflow/core/tfrt/graph_executor/graph_executor.h:146-150](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/graph_executor/graph_executor.h#L146-L150) —— 类定义，`Run` 与 `RunWithSyncInterpreter` 是两个对外执行入口。

异步 `Run`：

[tensorflow/core/tfrt/graph_executor/graph_executor.h:269-274](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/graph_executor/graph_executor.h#L269-L274) —— 接收输入张量名-值对、输出/目标张量名，回填 `outputs`。

同步解释器入口：

[tensorflow/core/tfrt/graph_executor/graph_executor.h:302-308](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/graph_executor/graph_executor.h#L302-L308) —— `RunWithSyncInterpreter` 用 MLRT 解释器同步跑一张图，注释点明「run synchronously with the TFRT interpreter」。

编译产物的两条路径（私有方法）：

[tensorflow/core/tfrt/graph_executor/graph_executor.h:357-363](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/graph_executor/graph_executor.h#L357-L363)

```cpp
absl::StatusOr<tfrt::BefBuffer> CompileMlirModuleToBef(mlir::ModuleOp module) const;
absl::Status InitBef(LoadedClientGraph* loaded_client_graph,
                     tensorflow::tfrt_stub::WorkQueueInterface* work_queue);
absl::Status InitBytecode(LoadedClientGraph* loaded_graph);
```

`CompileMlirModuleToBef` + `InitBef` 是经典 BEF 路径；`InitBytecode` 是新的 MLRT 字节码路径。二者并存正是 TFRT 在执行器层面的演进现状。

`LoadedClientGraph` 的内部状态：

[tensorflow/core/tfrt/graph_executor/graph_executor.h:152-235](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/graph_executor/graph_executor.h#L152-L235) —— 它持有 `mlir_context_`、`executable_context_`（真正的编译产物）、`runner_table_`（fallback kernel 运行所需的 `OpKernelRunner` 缓存）、`resource_array_`、`sync_resource_state_` 等。`MaybeGetCostRecorder` / `UpdateCost`（L168-175）实现了「记录 op 代价 → 必要时重编译」的自适应优化。

缓存表与并发安全：

[tensorflow/core/tfrt/graph_executor/graph_executor.h:387-393](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/graph_executor/graph_executor.h#L387-L393) —— `loaded_client_graphs_` 是以 `joined_name` 为键的 `flat_hash_map`，用 `unique_ptr` 保值的指针稳定性，并由 `loaded_client_graphs_mu_` 保护。这与 u3-l2 里 `DirectSession` 的 `ExecutorsAndKeys` 缓存思路一致——都是「按 fetches/feed 组合缓存已编译子图」。

BEF vs MLRT 的开关（在 `.cc` 里）：

[tensorflow/core/tfrt/graph_executor/graph_executor.cc:532](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/graph_executor/graph_executor.cc#L532) —— `->Set(options.enable_mlrt ? "mlrt" : "bef")`，配合同文件 [graph_executor.cc:140](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/graph_executor/graph_executor.cc#L140) 的统计指标描述 `"executor modes (BEF vs MLRT interpreter)"`，可以确认存在两种执行器模式。

> 说明：`graph_executor.cc` 不在本讲 `source_files` 的头文件清单里，但它是理解 BEF/MLRT 选择逻辑的关键，这里作为补充引用。本讲引用的 `.cc` 行号基于当前 HEAD，若后续仓库改动请以实际为准。

#### 4.4.4 代码实践

**实践目标**：画出 `GraphExecutor` 的「编译产物二选一」与「缓存」两条主线。

**操作步骤**：

1. 打开 `graph_executor.h`，定位 `CompileMlirModuleToBef`（L357）、`InitBef`（L360）、`InitBytecode`（L363）三个私有方法。
2. 定位缓存表 `loaded_client_graphs_`（L391）与其保护锁 `loaded_client_graphs_mu_`（L387）。
3. 阅读 `LoadedClientGraph::MaybeGetCostRecorder` 的注释（L164-167），理解「在线代价分析 → 重编译」的触发条件。

**需要观察的现象**：`LoadedClientGraph` 同时持有「MLIR 模块」「executable_context」「cost_recorder」三套对象，说明一次 `Load` 之后仍可能因为代价数据更新而重编译。

**预期结果**：你能用一句话概括——`GraphExecutor` 把「按 joined_name 缓存的子图」编译成「BEF 或 MLRT 字节码」，执行时交给对应解释器，并可据运行时代价重编译。

**运行结果**：源码阅读型实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`GraphExecutor` 为什么用「排序后的输入输出名」作为缓存键，而不是直接用调用顺序？

> **答案**：因为同一次逻辑请求可能以不同顺序传入相同的输入输出集合，排序后归一化能保证「同样的子图只编译一次」，避免重复编译。这和 u3-l2 `DirectSession` 用 sorted key 缓存 `ExecutorsAndKeys` 的思路一致。

**练习 2**：`enable_mlrt` 为真时走 MLRT 字节码，为假时走 BEF。从 `LoadedClientGraph` 同时持有 `tf_mlir_with_op_keys` 与 `tfrt_mlir` 两个 MLIR 模块（L214-215）来看，重编译时为什么需要保留 MLIR 模块而不是只留字节码？

> **答案**：因为重编译需要从 MLIR 层面重新做 lowering（例如根据新代价数据改变聚类/分桶），而字节码是 lowering 的终点、不可逆；故必须保留上游 MLIR 模块作为重编译的起点。

---

### 4.5 `runtime_fallback`：让 TFRT 跑老 OpKernel 的回退机制（core.runtime_fallback）

#### 4.5.1 概念说明

TFRT 想用一套全新的异步 kernel 体系替代老 `OpKernel`，但 TF 仓库里有数百个 op，不可能一夜之间全重写。于是有了 **fallback（回退）机制**：当某个 op 在 TFRT 侧没有原生 kernel 时，运行时透明地退回到老的 `tensorflow::OpKernel::Compute`（u4-l2 讲过的那个 `Compute(OpKernelContext*)`）去执行。

这套机制住在 `tensorflow/core/runtime_fallback/` 目录，是 TFRT 能「渐进迁移」的关键——它让 TFRT 即使在 op 覆盖不全的情况下也能跑通任意 `GraphDef`。

目录里有两组实现：

- `runtime_fallback/kernel/`：把「执行一个老 OpKernel」封装成一个 TFRT kernel（`KernelFallbackExecute`）。
- `runtime_fallback/runtime/`：在 TFRT 运行时层面注册这些 fallback kernel 与张量转换函数。

#### 4.5.2 核心流程

fallback 的执行流程：

1. **建图/编译期**：`GraphExecutor` 在 lowering 时，遇到没有 TFRT 原生 kernel 的 op，就为它生成一个「调用 fallback」的 TFRT kernel（如 `tfrt_op_kernel`）。
2. **运行期**：BEF/MLRT 执行器跑到该 kernel 时，调用 `KernelFallbackExecute`。
3. **KernelFallbackExecute**：从 `ExecutionContext` 取出 `OpKernelRunner`（一个缓存了具体 `OpKernel` 对象 + `OpKernelContext` 所需状态的运行器），按老 TF 的方式调 `Compute`。
4. **张量适配**：fallback kernel 的输入输出在 TFRT 的 `AsyncValue` 与 TF 的 `tensorflow::Tensor` 之间转换（由 `runtime_fallback_tensor.h` 等负责）。
5. **结果回填**：把 `Compute` 写入的输出张量包回 `AsyncValue`，交给上游执行器继续异步传播。

#### 4.5.3 源码精读

fallback kernel 的核心入口：

[tensorflow/core/runtime_fallback/kernel/kernel_fallback_execute.h:43-47](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/runtime_fallback/kernel/kernel_fallback_execute.h#L43-L47) —— `KernelFallbackExecute` 接收 `ExecutionContext`、op 名、一组 `AsyncValue*` 输入、一组输出槽、属性 `OpAttrsRef`，异步执行一个老 OpKernel。头注释说得很直白：「Provides a way to execute a TensorFlow kernel using TFRT kernel fallback」。

注意它完全是异步签名（输入是 `AsyncValue*`，输出是 `RCReference<AsyncValue>`），这正是「把同步的老 kernel 塞进异步执行图」的接口形态：

```cpp
bool KernelFallbackExecute(
    const tfrt::ExecutionContext& exec_ctx, tfrt::string_view op_name,
    llvm::ArrayRef<tfrt::AsyncValue*> arguments,
    llvm::MutableArrayRef<tfrt::RCReference<tfrt::AsyncValue>> results,
    const tfrt::OpAttrsRef& attrs, KernelFallbackOutputType output_type);
```

`KernelFallbackOutputType` 枚举（L35-38）区分输出是普通 `tensorflow::Tensor` 还是 `KernelFallbackTensor`——后者是 fallback 体系内部用的张量包装，避免频繁格式转换。

fallback kernel 的自动注册：

[tensorflow/core/runtime_fallback/runtime/static_registration.cc:30-36](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/runtime_fallback/runtime/static_registration.cc#L30-L36)

```cpp
TFRT_STATIC_KERNEL_REGISTRATION(RegisterTfdDelegateKernels);

static bool runtime_fallback_conversion_fn_registration = []() {
  tfrt::AddStaticTensorConversionFn(
      RegisterTFRuntimeFallbackTensorToHostConversionFn);
  return true;
}();
```

这里用了两种「启动期自动注册」手法，都是 u4-l1、u6-l1 见过的老朋友：

- `TFRT_STATIC_KERNEL_REGISTRATION`：把一组 fallback kernel 注册进 TFRT 的 `KernelRegistry`（对应 TF 的 `REGISTER_KERNEL_BUILDER`）。
- 一个静态 bool lambda：在 `main` 之前注册「`RuntimeFallbackTensor` ↔ host tensor」的转换函数，让两种张量表示能互转。

`OpKernelRunner` 的缓存：

[tensorflow/core/tfrt/fallback/op_kernel_runner.h](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/tfrt/fallback/op_kernel_runner.h) —— `OpKernelRunner` 把「具体 `OpKernel` 对象 + NodeDef + 设备 + 函数库运行时」打包成一个可重复调用的运行器，`OpKernelRunnerCache`（`op_kernel_runner_cache.h`）按 (op, device, attrs) 缓存它，避免每次执行都重新 `CreateOpKernel`（承接 u4-l2）。

#### 4.5.4 代码实践

**实践目标**：理解 fallback 如何把「异步 TFRT 执行」与「同步老 OpKernel」粘合在一起。

**操作步骤**：

1. 打开 `kernel_fallback_execute.h`，确认 `KernelFallbackExecute` 的输入输出都是 `AsyncValue`。
2. 打开 `static_registration.cc`，确认它用静态构造对象注册了 fallback kernel 与张量转换函数。
3. 在 `runtime_fallback/kernel/` 下找到 `tfrt_op_kernel.cc`，阅读它如何把 `KernelFallbackExecute` 包成一个可被 BEF 调用的 TFRT kernel（即「一个 op 在图里长成一个 kernel 节点，执行时转交 fallback」）。

**需要观察的现象**：fallback 这条路对调用方完全透明——BEF/MLRT 执行器不关心一个 kernel 是「原生」还是「fallback」，它只管按字节码调度，具体实现由 kernel 注册表决定。

**预期结果**：你能解释「为什么 TFRT 不必等所有 op 重写完才能上线」——因为 fallback 让任意 op 都能跑，只是非原生 op 不享受 TFRT 的异步优化收益。

**运行结果**：源码阅读型实践，无需运行。

#### 4.5.5 小练习与答案

**练习 1**：`KernelFallbackExecute` 为什么用 `AsyncValue` 而不是直接传 `tensorflow::Tensor`？

> **答案**：因为 TFRT 整体是异步优先的，数据流以 `AsyncValue` 串联；fallback kernel 必须遵守同样的异步契约，才能与原生 TFRT kernel 混在一张执行图里被统一调度，否则会破坏异步流水线。

**练习 2**：`static_registration.cc` 同时注册了「kernel」和「张量转换函数」两类东西。为什么缺一不可？

> **答案**：只注册 kernel 能让 fallback 被调用，但 fallback 产出的 `KernelFallbackTensor` 无法被下游原生 TFRT kernel 识别；张量转换函数负责在两种张量表示之间转换，缺了它数据流会在类型边界断裂。

---

## 5. 综合实践

**任务**：用一张「时序对照表 + 调用链图」把本讲四个模块串起来，回答规格里给出的核心问题——**BEF 执行器与传统 `DirectSession` 在执行模型上的主要不同是什么，TFRT 试图解决什么问题？**

请按以下步骤完成：

1. **复现两套执行链**。在源码中分别定位：
   - 传统链路：`direct_session.cc` 的 `Run` → `GetOrCreateExecutors` → `Executor::Run`（参见 u3-l2）。
   - TFRT 链路：`tfrt_session.cc:351` → `graph_executor_->Run` → BEF/MLRT 执行器。

2. **填写对照表**（写在你的学习笔记里）：

   | 维度 | DirectSession | TfrtSession + GraphExecutor |
   | --- | --- | --- |
   | 执行单元 | 内存中的 `Graph*` + `ExecutorImpl`（节点拓扑序调度） | BEF / MLRT 字节码 + 字节码解释器 |
   | 调度模型 | 以同步拓扑序为主，跨设备用 `_Send`/`_Recv` | 异步优先，`AsyncValue` 数据流驱动 |
   | op 覆盖 | 全部用老 `OpKernel` | 原生 TFRT kernel + 不足处 fallback 到老 `OpKernel` |
   | 编译期与执行期 | 揉在一次 `Run` 里（放置/优化/分区都在首次 Run） | 显式分离：`GraphExecutor::Create` 期编译成字节码，`Run` 期只解释 |
   | 可组合性 | 单体执行器 | Runtime / WorkQueue / KernelRegistry 可插拔 |

3. **回答「TFRT 解决什么」**。结合 4.1 的三点设计目标（异步优先、可组合、与 op 解耦+渐进迁移），用 3-5 句话写明 TFRT 相对 `DirectSession` 的改进点，并指出当前它「默认关闭、opt-in」这一事实（引用 `direct_session.cc:209-213` 与 `local_session_selection.cc:20-21`）。

4. **（可选，待本地验证）**：若你有一个编译了 TFRT 的 TF，尝试用 4.3.4 的示例代码显式 `target="tfrt_session"` 跑一个常量加法，对比同样的图在默认 `DirectSession` 下的日志差异（重点看是否出现 `"Start initializing TfrtSession"`）。

**交付物**：一张对照表 + 一段结论 + （可选）一份运行日志摘录。

## 6. 本讲小结

- **TFRT 是新一代、异步优先、可组合的运行时基础设施**，目标是替代 `DirectSession` 那套单体、同步、与 op 强耦合的执行器。
- 它把图**编译成字节码**（经典 BEF 或更新的 MLRT 字节码）再解释执行，做到了「编译期与执行期分离」，`tf_bef_executor_main.cc` 证明 BEF 是可独立加载执行的产物。
- `tfrt_stub::Runtime`（`core/tfrt/runtime/`）是 TF 侧的运行时抽象，内部包装上游 `tfrt::CoreRuntime`，并通过 `WorkQueueInterface` 复用 TF 的线程池。
- `TfrtSession`（`core/tfrt/tfrt_session/`）让 TFRT 伪装成普通 `tensorflow::Session`，与 `DirectSession` 在 `AcceptsOptions` 上互补；当前仓库**默认仍是 DirectSession，TFRT 需经 `use_tfrt()` 或 `target="tfrt_session"` 显式开启**。
- `GraphExecutor`（`core/tfrt/graph_executor/`）是施工队长，负责 `GraphDef → MLIR → BEF/MLRT` 的编译与按 `joined_name` 缓存的子图执行，还支持据运行时代价重编译。
- `runtime_fallback`（`core/runtime_fallback/`）通过 `KernelFallbackExecute` 与静态注册的 fallback kernel，让 TFRT 在 op 覆盖不全时透明回退到老 `OpKernel::Compute`，实现渐进迁移。

## 7. 下一步学习建议

- **横向对比执行模型**：回到 u3-l2（`DirectSession`）与本讲，亲手画一张「同一张图在两套运行时里的执行时序图」，这是巩固理解的最佳方式。
- **深入 MLRT 字节码**：阅读 `tensorflow/core/tfrt/mlrt/bytecode/executable.h` 与 `function.h`，理解 MLRT 作为 BEF 后继的字节码布局（`kernel_names` / `attributes` / `functions` 三段式）。
- **跟进上游 TFRT**：本讲多次出现 `@tf_runtime` 外部仓库（`tfrt::CoreRuntime`、`tfrt::BEFExecutor`、`AsyncValue`）。可在 `third_party/` 下找到其引用配置，进一步阅读上游设计与 `DecodedDiagnostic`、`HostContext` 等概念。
- **回到编译侧**：TFRT 的执行依赖 MLIR lowering 产物，建议接着读 u7-l1（MLIR 与 TF dialect）与 u7-l2（XLA / StableHLO），把「图 → MLIR → 字节码/设备代码」整条编译流水线打通。
- **服务端落地**：若关注推理部署，可继续阅读 `tensorflow/core/tfrt/saved_model/`，看 TFRT 如何配合 SavedModel 做加载与 AOT 编译（`saved_model_aot_compile.h`），这与后续的 TFLite（u8）形成「服务端 TFRT / 端侧 TFLite」的部署双线。
