# u9-l3 Profiler 与性能分析

## 1. 本讲目标

训练一个模型时，我们常常会问：「我的程序到底慢在哪里？是 GPU 没吃饱，还是数据喂不过来，还是某一个算子特别耗时？」这些问题光靠「加打印、看秒表」是答不准的——因为一次训练步骤里有成百上千个算子在 CPU、GPU、多线程之间并发执行，肉眼无法分辨。

TensorFlow Profiler（性能分析器）就是用来回答这类问题的工具。它的职责是：**在程序运行过程中采集每一个算子、每一次设备调用、每一段用户代码的耗时与内存信息，再把它们汇总成可以定位瓶颈的报告**。

学完本讲，你应当能够：

- 说清 TensorFlow Profiler 的**两条技术路线**（基于 `RunMetadata` 的老式 tfprof，与基于 `TraceMe`/`XPlane` 的新式采样式 profiler），并知道该用哪一条。
- 理解 `tf.profiler.experimental.Trace` 这个上下文管理器背后的 **TraceMe 低开销采集原语**是怎么工作的、为什么不开 profiler 时几乎零开销。
- 看懂一个 profiling 会话（`ProfilerSession`）如何把散落的 trace 事件收集进统一的 `XSpace`/`XPlane` 数据容器。
- 掌握 tfprof 的**核心指标**（`micros` / `accelerator_micros` / `cpu_micros` / `bytes` 等）与四种视图（scope / graph / code / op），并能据此判断瓶颈类型。
- 能够对一次训练步骤开启 trace，找出耗时最高的算子，并给出优化方向。

本讲为「扩展与二次开发」单元的第三讲，承接 u3-l4（`tf.function`），因为现代 profiler 的「step」概念正是由 `tf.profiler.experimental.Trace` 标定的。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（若不熟悉可先回顾对应讲义）：

- **op 与 kernel 的执行**（u4-l2、u3-l2）：一个 op 被调度到设备上、由 `OpKernel::Compute` 真正计算。profiler 量化的就是这一过程的时间与内存。
- **`tf.function` 与 step 的概念**（u3-l4）：TF2 默认 Eager 执行，一次「训练 step」是一个完整的「前向 → 反向 → 更新」过程。profiler 通常以 step 为单位采集。
- **CPU 与加速器（GPU/TPU）的并发**：GPU kernel 是异步发射（launch）的——CPU 把 kernel 丢给 GPU 后就继续往下走，GPU 在后台排队执行。这会导致一个反直觉现象：**profiler 报告的「各算子耗时之和」常常大于墙钟时间**，因为很多算子在并行跑。这一点是理解所有耗时指标的前提。
- **protobuf**：profiler 的配置与输出（`OptionsProto`、`XSpace` 等）都用 protobuf 消息表示。

几个术语先在此约定：

| 术语 | 含义 |
| --- | --- |
| trace（追踪） | 记录一段时间内发生的离散事件（某算子何时开始、何时结束）。 |
| TraceMe | TF 内部的「一段被追踪的代码区间」采集原语，是 trace 事件的最小来源。 |
| XPlane / XSpace | profiler 内部统一的 trace 数据容器（见 4.3）。 |
| step（步骤） | 一次完整的训练迭代，profiler 按 step 切分采集窗口。 |
| 视图（view） | 同一批 profiling 数据按不同维度（按 op 类型 / 按名字作用域 / 按数据流图 / 按 Python 代码）组织展示。 |

## 3. 本讲源码地图

本讲涉及的关键文件，按「Python 用户 API → C++ 采集原语 → 数据容器 → 分析层」自上而下排列：

| 文件 | 作用 |
| --- | --- |
| `tensorflow/python/profiler/trace.py` | `tf.profiler.experimental.Trace` 上下文管理器，用户手动打点的入口。 |
| `tensorflow/python/profiler/profiler_v2.py` | `tf.profiler.experimental.start/stop/Profile/ProfilerOptions`，新式 profiler 的开关 API。 |
| `tensorflow/core/profiler/lib/traceme.h` | TraceMe 原语的 TF 侧声明（一个 shim，转发到 tsl 实现）。 |
| `third_party/xla/third_party/tsl/tsl/profiler/lib/traceme.h` | TraceMe 的真正实现：构造记开始时间、析构记结束时间并上交记录器。 |
| `third_party/xla/third_party/tsl/tsl/profiler/lib/profiler_session.h` | `ProfilerSession`：一次采集会话，负责启动/停止并把数据收进 `XSpace`。 |
| `third_party/xla/third_party/tsl/tsl/profiler/protobuf/xplane.proto` | `XSpace`/`XPlane`/`XLine`/`XEvent` 数据模型，trace 事件的统一容器。 |
| `tensorflow/core/profiler/tfprof_options.h` | 老式 tfprof 的 `Options` 结构与所有可选指标/命令/输出的枚举。 |
| `tensorflow/core/profiler/profiler.cc` | tfprof 命令行工具（`tfprof> ` 交互式 shell）的入口。 |
| `tensorflow/python/profiler/model_analyzer.py` | `tf.compat.v1.profiler.profile`/`Profiler` 的 Python 包装，分发四种视图。 |
| `tensorflow/python/profiler/option_builder.py` | `ProfileOptionBuilder`：用链式调用方便地构造选项字典。 |

> 说明：`tensorflow/core/profiler/lib/traceme.h`、`profiler_session.h` 在本版本中已变成**薄薄的转发头**（shim），通过 `using ... = tsl::...` 把实现委托给共享库 tsl（TensorFlow Shared Library）。真正干活的代码在 `third_party/xla/third_party/tsl/tsl/profiler/` 下。这两个路径在本仓库里都是 git 跟踪的真实文件，下面的永久链接会分别指向 shim 与 tsl 实现。

## 4. 核心概念与源码讲解

### 4.1 性能分析的全景：两条技术路线

#### 4.1.1 概念说明

打开 `tensorflow/python/profiler/` 目录，你会发现 profiler 的 API 看起来「有两套」，这并非历史包袱的偶然，而是**两种本质不同的采集思路**：

1. **老式 tfprof（基于 `RunMetadata`）**：在图模式下，`Session.run` 时传入一个空的 `RunMetadata`，执行完毕后 TF 会把本次 run 的耗时、内存统计**作为副产品**填进去。事后用 `tf.profiler.profile(graph, run_meta, cmd=...)` 离线分析。它是「**执行时顺手统计**」。

2. **新式采样式 profiler（基于 `TraceMe` / `XPlane`）**：用一个**全局采集会话** `ProfilerSession`，在一段时间窗口内持续监听所有线程里埋的 `TraceMe` 打点，以及 GPU/TPU 硬件计数器（如 CUDA 的 CUPTI），结束时把全部原始事件收进 `XSpace`，再交给 TensorBoard 渲染。它是「**主动采样**」。

为什么要有第二套？因为第一套深度耦合于「`Session.run` + 图」的执行模型，而 TF2 默认 Eager、又引入了 `tf.function`、XLA、TFRT 等新执行路径，旧机制无法统一覆盖。新式 profiler 用一个**语言/执行后端无关的 trace 容器**（XPlane）把所有来源（CPU 打点、GPU kernel、TPU、Python 函数调用）都装进同一个时间轴，这就是你在 TensorBoard 「Trace Viewer」里看到的那张甘特图。

两条路线**共存**：老的 `tf.profiler.profile` 适合「我只要某个 op 的耗时/参数量/FLOPs，在脚本里直接拿到 protobuf」，新的 `tf.profiler.experimental.start` 适合「我想看一次完整训练 step 在 GPU 上的时间线」。

#### 4.1.2 核心流程

新式 profiler 的一次典型采集流程：

```text
用户脚本                         C++ 运行时                         离线/UI
─────────                       ──────────                         ────────
tf.profiler.experimental
   .start(logdir)  ──► 创建 ProfilerSession，启动各 TraceMeRecorder / 设备 tracer
                         （此后所有 Active() 的 TraceMe 开始往线程本地缓冲区写记录）

for step in steps:
  with tf.profiler.experimental.Trace("Train", step_num=step, _r=1):
      train_step()   ──► 内部每个 op/kernel 都包了 TraceMe，自动产生 trace 事件
                         （GPU kernel 的起止由 CUPTI 回调记录）

tf.profiler.experimental.stop() ──► ProfilerSession::CollectData(XSpace)
                                   把所有线程本地记录、设备事件合并进 XSpace
                                   ──► 序列化写入 logdir ──► TensorBoard 读取展示
```

关键设计：**采集与执行解耦**。业务代码里早已埋好 TraceMe（每个 op kernel 默认就有），平时 `TraceMeRecorder::Active()` 返回 false，这些打点几乎不耗时；一旦 `start()` 把 recorder 激活，打点才开始真正记录。

#### 4.1.3 源码精读

先看新式 profiler 的「开/关」API。`start` 内部创建一个进程内单例 `_profiler`（加锁保证同时只有一个会话）：

[profiler_v2.py:108-117](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/profiler_v2.py#L108-L117) —— 用 `_profiler_lock` 保证全局唯一会话；`_profiler = _pywrap_profiler.ProfilerSession()` 创建 C++ 会话对象，`_profiler.start(logdir, opts)` 真正启动采集。注意 `opts = dict(options._asdict())`：因为 pybind11 不支持 namedtuple，先转成普通 dict 再传给 C++。

[profiler_v2.py:129-153](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/profiler_v2.py#L129-L153) —— `stop(save=True)` 调 `_profiler.export_to_tb()` 把结果写进 TensorBoard 的 logdir，然后把全局 `_profiler` 置空，允许下一次采集。

`ProfilerOptions` 只暴露四个旋钮，分别控制 CPU / Python / 设备三路 trace 的详细程度，以及多机对齐的延迟：

[profiler_v2.py:56-77](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/profiler_v2.py#L56-L77) —— `host_tracer_level`（CPU 打点级别，1 关键/2 信息/3 冗长，默认 2）、`python_tracer_level`（是否用 sys.settrace 追踪 Python 调用，开销大，默认关）、`device_tracer_level`（GPU/TPU 硬件 trace，默认开）、`delay_ms`（多机同步起点）。

> 重要约束：注释里写明 **「Only one active profiler session is allowed」**。如果你已经用 Keras 的 TensorBoard 回调（它会自动采样），就得先设 `profile_batches=[]` 关掉它，否则会和手动 `start` 冲突。

#### 4.1.4 代码实践

**实践目标**：从源码里确认「同时只能有一个 profiler 会话」这条约束。

1. 打开 [profiler_v2.py:80-126](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/profiler_v2.py#L80-L126) 的 `start` 函数。
2. 找到 `if _profiler is not None: raise errors.AlreadyExistsError(...)` 这一行，记下它的行号。
3. 思考：如果在一个 `with tf.profiler.experimental.Profile(...)` 还没退出时，再调用一次 `start`，会抛什么异常？

**预期结果**：会抛 `AlreadyExistsError: Another profiler is running.`。这解释了为什么采样式 profiler 不能像 `time.time()` 那样随意嵌套使用。

#### 4.1.5 小练习与答案

**练习 1**：老式 tfprof 和新式 profiler 各自的数据「来源」是什么？

> **参考答案**：老式 tfprof 的数据来源是 `Session.run` 时填入的 `RunMetadata`（执行时的副产品）；新式 profiler 的来源是全局激活的 `TraceMe` 打点 + 设备硬件 trace（如 CUPTI），统一收进 `XSpace`。

**练习 2**：为什么 Keras TensorBoard 回调与手动 `tf.profiler.experimental.start` 不能同时用？

> **参考答案**：因为新式 profiler 进程级只能有一个活跃会话（`_profiler` 单例 + `_profiler_lock`）。回调自己会起会话，再手动 `start` 会触发 `AlreadyExistsError`。

---

### 4.2 `python.profiler.trace`：用户打点的 Trace 上下文管理器

#### 4.2.1 概念说明

profiler 默认会自动记录每个 op/kernel 的执行，但它**不知道你代码的语义边界**——它不知道「这一段是训练 step」「这一段是数据预处理」。`tf.profiler.experimental.Trace` 就是给用户的**手动打点工具**：它告诉 profiler「从进入这个 `with` 块到退出，算作一个名为 X 的事件」，并可以挂上 `step_num` 这样的元数据。

它对应的 `tf.*` 导出名是 `tf.profiler.experimental.Trace`。最经典的用法是把整个训练 step 包起来，这样 TensorBoard 就能按 step 切分时间线。

#### 4.2.2 核心流程

`Trace` 是一个上下文管理器，它本身不直接计时，而是**包一层 C++ 的 `TraceMe` 对象**：

```text
with Trace("Train", step_num=step, _r=1):
   │ __init__: 若 enabled()，则 new 一个 _pywrap_traceme.TraceMe(name, **kwargs)
   │           —— TraceMe 构造时即记录开始时间戳
   ▼
   ...用户代码（训练 step）...
   │
   └ __exit__: 调 self._traceme.Stop() —— 记录结束时间戳，上交给记录器
```

两个性能关键点：

1. **`enabled()` 是一个极低开销的 C++ 函数**，直接查「profiler 是否激活」。没开 profiler 时，`Trace` 的 `__init__` 几乎什么都不做（`self._traceme = None`），所以你可以在生产代码里**永久保留** `Trace` 打点而不必担心拖慢训练。
2. 计时起点在 `__init__`（创建 TraceMe 时）就开始，而不是 `__enter__`，目的是**省掉一次 Python→C++ 调用**（见源码注释）。

#### 4.2.3 源码精读

模块顶部把 C++ 的 `traceme_enabled` 直接赋给模块级 `enabled`，注释点明它是「低开销地直接调 C++ 查 profiler 是否开启」：

[trace.py:22-24](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/trace.py#L22-L24) —— `enabled = _pywrap_traceme.traceme_enabled`。这就是「关闭 profiler 时近乎零开销」的入口。

`Trace` 类的 `__init__` 用 `enabled()` 做开关，命中才创建 C++ `TraceMe`（创建即开始计时）：

[trace.py:77-81](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/trace.py#L77-L81) —— 未开启时 `self._traceme = None`；开启时 `self._traceme = _pywrap_traceme.TraceMe(name, **kwargs)`。

`__enter__` 故意什么都不做（注释解释：在这里再启动时钟会多一次跨语言调用），真正的停止发生在 `__exit__`：

[trace.py:83-85](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/trace.py#L83-L85) —— `__enter__` 直接 `return self`。

[trace.py:121-123](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/trace.py#L121-L123) —— `__exit__` 调 `self._traceme.Stop()`，把区间结束时间交给记录器。

`set_metadata` 解决「创建时还不知道某些信息」的问题（例如想记录「这次 JIT 是否命中缓存」，但该信息要等编译完才知道）：

[trace.py:87-119](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/trace.py#L87-L119) —— 通过 `tm.set_metadata(in_cache=...)` 在事件进行中追加键值元数据。

文件末尾还提供一个更快的装饰器替代品 `trace_wrapper`，等价于 `with Trace(name): func()`，但用 `@functools.wraps` 包装，适合给固定函数长期打点：

[trace.py:176-186](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/trace.py#L176-L186) —— `inner_wrapper` 同样先用 `enabled()` 短路：未开启 profiler 时直接调原函数，零开销。

#### 4.2.4 代码实践

**实践目标**：用 `Trace` 标定训练 step，并验证「未开 profiler 时打点不产生副作用」。

```python
# 示例代码：需本地安装 tensorflow 后运行
import tensorflow as tf

logdir = "./logs"

# （A）先观察：不 start profiler，直接用 Trace
with tf.profiler.experimental.Trace("Train", step_num=0, _r=1):
    x = tf.reduce_sum(tf.random.normal((1000, 1000)))
print("未开启 profiler 时，Trace 静默运行，x =", float(x))
# 预期：正常打印一个浮点数，没有任何 trace 输出（因为 enabled() 为 False）

# （B）再观察：开启 profiler 后，Trace 事件被采集
tf.profiler.experimental.start(logdir)
for step in range(3):
    with tf.profiler.experimental.Trace("Train", step_num=step, _r=1):
        # _r=1 表示这是「根」trace，profiler 用它来界定 step 边界
        y = tf.reduce_sum(tf.random.normal((1000, 1000)))
tf.profiler.experimental.stop()
print("profiling 数据已写入", logdir)
```

**操作步骤**：
1. 保存为 `trace_demo.py` 并运行（`python trace_demo.py`）。
2. 安装 TensorBoard（`pip install tensorboard`），运行 `tensorboard --logdir=./logs`，浏览器打开 `http://localhost:6006`，进入 **Profile** 标签页。
3. 在 **Tools** 下拉里选 **Trace Viewer**。

**需要观察的现象**：
- 情况 (A) 不产生任何日志文件，证明 `Trace` 在 profiler 关闭时是「哑」的。
- 情况 (B) 在 Trace Viewer 里能看到 3 个标着 `step_num=0/1/2` 的 `Train` 区间，区间内是 `random_normal`、`sum` 等 op 的事件条。

**预期结果**：每个 `Train` 区间对应一次循环迭代，区间内的 op 事件被「归类」到该 step 名下。`_r=1`（root）参数让 profiler 把它当作 step 的根节点。

> 待本地验证：Trace Viewer 的具体 UI 随 TensorBoard 版本变化；若 Profile 标签页为空，确认 `stop()` 已被调用且 `logdir` 路径正确。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Trace.__enter__` 里不启动时钟，而是要在 `__init__` 里就启动？

> **参考答案**：因为 `__init__` 在创建 C++ `TraceMe` 时就已经记录了开始时间戳，在 `__enter__` 再启动会多一次 Python→C++ 调用；把计时点放在构造时可以省掉这次跨语言开销，提升精度。

**练习 2**：`tm.set_metadata(**kwargs)` 解决了什么问题？举一个例子。

> **参考答案**：它解决「创建 trace 时还不知道某些信息」的问题。例如想测量 `call()` 的总耗时（含编译+执行），但「是否命中缓存」要等 `jit_compile()` 返回才知道，这时先用 `Trace("call")` 开区间，事后再 `tm.set_metadata(in_cache=...)` 补元数据。

---

### 4.3 `core.profiler` 采集层：TraceMe 原语与 ProfilerSession

#### 4.3.1 概念说明

上一节我们看到 Python 的 `Trace` 只是 C++ `TraceMe` 的一层壳。本节深入 `core.profiler`，看清 trace 数据**是怎么产生、怎么收集、存成什么结构**的。这是新式 profiler 的核心机制。

三个关键对象：

- **TraceMe**：一段被追踪的代码区间。构造时记开始时间，析构（或 `Stop()`）时记结束时间，并把 `(名字, 开始, 结束)` 三元组交给记录器。它是 trace 事件的**生产者**，散布在 TF 运行时的每个 op kernel、每个关键函数里。
- **TraceMeRecorder**：全局记录器。每个线程有一个本地缓冲区，TraceMe 把记录写进去；profiler 关闭时记录器不激活，写入被短路。
- **ProfilerSession**：一次采集会话。`start` 创建它，它激活记录器与各设备 tracer；`CollectData` 时把所有线程本地缓冲区和设备事件**合并**进一个 `XSpace`。

#### 4.3.2 核心流程

一条 trace 事件从产生到落盘的完整旅程：

```text
某线程内：
  TraceMe trace("MatMul", level=kInfo);   // 构造：若 recorder.Active(level)，
                                          //   记 start_time = now()
  ... 执行 MatMul kernel ...
  trace.Stop();  // （或离开作用域析构）
                 // 若 recorder.Active()，调
                 //   TraceMeRecorder::Record({name, start_time, end_time})
                 //   写入【本线程的本地缓冲区】

profiler stop 时：
  ProfilerSession::CollectData(XSpace* space)
    ├─ 遍历所有线程的本地缓冲区，取出全部记录
    ├─ 取出设备 tracer 的事件（GPU kernel 起止等）
    └─ 统一转换/合并，写入 XSpace 里的 XPlane → XLine → XEvent
```

**级别过滤**：TraceMe 带一个 `level`（1=kCritical 用户级、2=kInfo 昂贵 op、3=kVerbose 廉价 op）。默认只记录 `level <= 2` 的事件，避免被海量廉价 op 淹没。

#### 4.3.3 源码精读

先看 TF 侧的 shim 头，确认 `TraceMe` 就是从 tsl 转发来的：

[lib/traceme.h:25-28](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/profiler/lib/traceme.h#L25-L28) —— `using TraceMe ... = tsl::profiler::TraceMe;`，TF 的 `tensorflow::profiler::TraceMe` 只是 `tsl::profiler::TraceMe` 的别名。下面看真正的实现。

tsl 的 `TraceMe` 用一个枚举定义三个预置级别，注释说明它们的用途：

[tsl/traceme.h:50-54](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/third_party/xla/third_party/tsl/tsl/profiler/lib/traceme.h#L50-L54) —— `kCritical=1`（用户自定义打点）、`kInfo=2`（昂贵 op，UI 默认显示）、`kVerbose=3`（廉价 op，默认不显示）。注释第 93 行明说「默认只记录 level <= 2」。

构造函数是「零开销」设计的关键：用 `TF_PREDICT_FALSE` 提示分支预测器，平时（recorder 未激活）走快路径，连开始时间都不记：

[tsl/traceme.h:98-108](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/third_party/xla/third_party/tsl/tsl/profiler/lib/traceme.h#L98-L108) —— 只有 `TraceMeRecorder::Active(level) && CheckFilter(filter_mask)` 同时为真，才 `name_.Emplace(name)` 并 `start_time_ = GetCurrentTimeNanos()`；否则 `start_time_` 保持 `kUntracedActivity`（即 0）。

`Stop()`（析构函数也会调它）是「真正上交记录」的地方，三处时间戳一次性发出：

[tsl/traceme.h:185-204](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/third_party/xla/third_party/tsl/tsl/profiler/lib/traceme.h#L185-L204) —— 若 `start_time_ != kUntracedActivity`（即当初确实在追踪），且 `TraceMeRecorder::Active()`，则 `TraceMeRecorder::Record({std::move(name), start_time_, GetCurrentTimeNanos()})`，把 `(名字, 开始, 结束)` 三元组写入线程本地缓冲区。

`ProfilerSession` 是把这些散落记录「收口」的会话。它的 `DefaultOptions` 揭示了默认采集强度：

[tsl/profiler_session.h:46-56](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/third_party/xla/third_party/tsl/tsl/profiler/lib/profiler_session.h#L46-L56) —— 默认 `device_tracer_level=1`（开设备 trace）、`host_tracer_level=2`（CPU info 级）、`python_tracer_level=0`（关 Python 调用追踪）、`include_dataset_ops=true`。

收集动作的契约——把全部数据写进一个 `XSpace`：

[tsl/profiler_session.h:63-65](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/third_party/xla/third_party/tsl/tsl/profiler/lib/profiler_session.h#L63-L65) —— `CollectData(tensorflow::profiler::XSpace* space)`。注释说明 `ProfilerSession` 创建即开始采集，析构或调用 `CollectData` 即停止；多个实例可创建但至多一个真正在采集（`Status()` 只对那个实例返回 OK）——这与 Python 侧「单会话」约束呼应。

最后看 trace 数据的统一容器 `XSpace`。它是一个层层嵌套的 protobuf：`XSpace`（整个采集结果）包含若干 `XPlane`（一个来源/一台主机），每个 `XPlane` 含若干 `XLine`（一条平行时间轴），每条 `XLine` 含一串 `XEvent`（一个事件，带偏移时间与时长）：

[xplane.proto:24-32](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/third_party/xla/third_party/tsl/tsl/profiler/protobuf/xplane.proto#L24-L32) —— `XSpace { repeated XPlane planes; repeated string hostnames; }`，一台主机对应一个 plane。

[xplane.proto:60-89](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/third_party/xla/third_party/tsl/tsl/profiler/protobuf/xplane.proto#L60-L89) —— `XLine` 是一条时间轴，有 `timestamp_ns`（起点）和 `repeated XEvent events`。注释指出「同一 XLine 内的 XEvent 不应部分重叠，但可以嵌套」——这就是甘特图里事件能套娃的依据。

[xplane.proto:91-112](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/third_party/xla/third_party/tsl/tsl/profiler/protobuf/xplane.proto#L91-L112) —— `XEvent` 用 `offset_ps`（相对 XLine 起点的皮秒偏移）和 `duration_ps`（皮秒时长）定位，可挂 `repeated XStat stats`（命名数值，如性能计数器）。注意单位是**皮秒（picosecond）**，精度极高。

这一套 `XSpace → XPlane → XLine → XEvent` 模型，就是把「CPU 打点 + GPU kernel + TPU + Python 调用」统一到同一张时间轴的底座。TensorBoard 的各个分析面板（Overview / Memory / Trace Viewer）都是对 `XSpace` 的不同视图。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读，跟踪一个 trace 事件「从被记录到进入 XSpace」的路径，并理解级别过滤。

1. 在 [tsl/traceme.h:98-108](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/third_party/xla/third_party/tsl/tsl/profiler/lib/traceme.h#L98-L108) 找到构造函数里写入 `start_time_` 的那一行，确认它被 `TraceMeRecorder::Active(level)` 守卫。
2. 在 [tsl/traceme.h:185-204](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/third_party/xla/third_party/tsl/tsl/profiler/lib/traceme.h#L185-L204) 找到 `Stop()` 里 `TraceMeRecorder::Record({...})` 调用，看清三元组的字段顺序。
3. 回答：如果一个 `TraceMe` 用 `level=3`（kVerbose）创建，但默认只记录 `level<=2`，那么它的 `start_time_` 会是什么值？`Stop()` 时会发生什么？

**预期结果**：因为 `Active(level)` 在 `level=3` 时返回 false，构造函数里 `start_time_` 不会被赋值，保持 `kUntracedActivity`（即 0）；`Stop()` 里判断 `start_time_ != kUntracedActivity` 为假，**不会**调用 `Record`。这就是级别过滤的实现——廉价 op 默认根本不进入记录管道。

> 待本地验证：可尝试把 `host_tracer_level` 调到 3（`ProfilerOptions(host_tracer_level=3)`），观察 Trace Viewer 里是否多出大量原本被过滤的低级 op 事件。

#### 4.3.5 小练习与答案

**练习 1**：`TraceMe` 在 profiler 未开启时为什么几乎零开销？具体是哪两处判断让它「什么都不做」？

> **参考答案**：构造函数里 `TraceMeRecorder::Active(level)` 返回 false 时，既不分配名字字符串、也不记开始时间，`start_time_` 保持 `kUntracedActivity`；`Stop()` 里又因 `start_time_ == kUntracedActivity` 而跳过 `Record`。两处都用 `TF_PREDICT_FALSE` 引导分支预测，让「不追踪」成为 CPU 流水线的快路径。

**练习 2**：`XSpace`、`XPlane`、`XLine`、`XEvent` 四者的包含关系是什么？为什么要分这么多层？

> **参考答案**：`XSpace` 包含多个 `XPlane`（一个来源/一台主机一个），`XPlane` 包含多条平行的 `XLine`（一条时间轴，如「CPU 线程 0」「GPU stream 0」），`XLine` 包含一串 `XEvent`（单个事件）。分层是为了把不同来源、不同并发流（线程、GPU stream）的事件统一到一个嵌套模型里，便于在同一张时间轴上对齐展示。

---

### 4.4 `core.profiler` 分析层：tfprof 的视图、选项与核心指标

#### 4.4.1 概念说明

新式 profiler 把原始 trace 事件存进 `XSpace`，但用户常常只想要一个简单答案：「哪个 op 最耗时？模型有多少参数？占多少显存？」——这类**聚合统计**就是老式 tfprof 擅长的，而它的核心配置定义在 `tensorflow/core/profiler/tfprof_options.h`。

tfprof 的核心思想是：**同一份 profiling 数据，按不同维度组织成不同「视图」，再用一组「选项」过滤、排序、选择要显示的指标**。理解这套选项体系，就掌握了「读懂 profiler 报告」的钥匙。

#### 4.4.2 核心流程

tfprof 的数据处理四步走（见 `g3doc/options.md` 概述）：

```text
1) 在内存里构建视图对应的数据结构：
     scope 视图 → 树（按名字作用域）
     graph 视图 → 图（按数据流）
     code  视图 → 树（按 Python 调用栈）
     op    视图 → 列表（按 op 类型，如 MatMul）

2) 用 account_type_regexes 选出要计账的节点（按 op 类型/设备/自定义类型）

3) 用各类 name_regexes / min_xxx / max_depth 进一步过滤要「显示」的节点
     （注意：未被显示的节点其统计仍会被父节点聚合，除非设 account_displayed_op_only）

4) 按 output 选项输出：stdout / file / timeline(json) / pprof / none
```

四种视图与七条命令、五种输出都枚举在头文件里：

- 命令 `kCmds` = `{scope, graph, code, op, advise, set, help}`，前四个是四种视图，`advise` 是自动诊断，`set`/`help` 用于交互式 shell。
- 输出 `kOutput` = `{timeline, stdout, file, pprof, none}`。

#### 4.4.3 源码精读

`tfprof_options.h` 把所有合法选项、排序键、可显示字段、命令、输出类型都列成静态数组，既是代码也是「选项字典」：

[tfprof_options.h:33-55](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/profiler/tfprof_options.h#L33-L55) —— `kOptions[]` 列出全部命令行选项名（`-max_depth`、`-min_micros`、`-min_accelerator_micros`、`-order_by`、`-select`、`-output` 等）。

[tfprof_options.h:74-79](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/profiler/tfprof_options.h#L74-L79) —— `kCmds[]`（七条命令）与 `kOutput[]`（五种输出）。这就是上一节那两个枚举的来源。

`struct Options` 把这些选项聚合成一个结构体，它的字段就是 profiler 报告里能出现的全部「指标阈值」：

[tfprof_options.h:152-164](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/profiler/tfprof_options.h#L152-L164) —— 这一组字段就是 tfprof 的**核心指标**：`min_bytes`（内存分配字节）、`min_micros`（总执行时间）、`min_accelerator_micros`（加速器时间）、`min_cpu_micros`（CPU 时间）、`min_params`（参数量）、`min_float_ops`（浮点运算数）、`min_occurrence`（出现次数）、`order_by`（排序键）。

**三个时间指标的关系**（来自 `g3doc/profile_time.md`，务必牢记）：

| 指标 | 含义 |
| --- | --- |
| `accelerator_micros` | 算子在**加速器（GPU/TPU）**上真正计算的时间 |
| `cpu_micros` | 算子在 **CPU** 上花的时间（含等待加速器完成的等待） |
| `micros`（即 `exec_micros`） | `accelerator_micros + cpu_micros` 之和 |

> 关键直觉：因为 GPU 异步执行，`micros`（各算子耗时之和）常常**大于**一次 step 的墙钟时间。若一个算子的 `cpu_micros` 很大但 `accelerator_micros` 很小，往往说明 **CPU 在等待 GPU**，是潜在的优化点。

**四类内存指标**（来自 `g3doc/options.md`）：

| 指标 | 含义 |
| --- | --- |
| `bytes` | 该算子请求分配的内存（累计值，因引用计数，常大于峰值） |
| `peak_bytes` | 峰值（高水位）占用 |
| `residual_bytes` | `Compute()` 结束时仍未释放的内存 |
| `output_bytes` | 该算子输出的内存（未必由它分配，可能是原地转发的输入） |

Python 侧，`model_analyzer.profile` 是用户入口，它把 dict 选项组装成 `OptionsProto`，再按 `cmd` 分发到四种视图，返回对应的 protobuf：

[model_analyzer.py:329-333](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/model_analyzer.py#L329-L333) —— 文档串说明四种 `cmd`：`op`（按 op 类型）、`scope`（按名字作用域）、`graph`（按数据流图）、`code`（按 Python 调用栈）。

[model_analyzer.py:358-378](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/model_analyzer.py#L358-L378) —— `code`/`op` 视图返回 `MultiGraphNodeProto`（多对一聚合），`graph`/`scope` 视图返回 `GraphNodeProto`（一一对应图节点）。底层都调 C++ 的 `print_mdl.PrintModelAnalysis(...)`。

`ProfileOptionBuilder` 提供了几个现成选项集，让你不必手写字典：

[option_builder.py:140-187](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/option_builder.py#L140-L187) —— `time_and_memory()` 预设：按 `micros` 排序、`select=['micros','bytes']`、`account_displayed_op_only=True`。这是「找最耗时算子」最常用的起点。

[option_builder.py:110-137](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/python/profiler/option_builder.py#L110-L137) —— `float_operation()` 预设：按 `float_ops` 排序，用于评估模型计算量。注意 FLOPs 依赖 op 注册的统计（`RegisterStatistics`）和完整形状信息。

最后看一眼 tfprof 命令行工具的入口，理解「profile_path」是一份可离线反复分析的二进制：

[profiler.cc:207-255](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/core/profiler/profiler.cc#L207-L255) —— `Run` 函数读取 `profile_path`（或等价的 graph+run_meta+op_log 组合）构造成 `TFStats`，之后可在 `tfprof> ` 交互 shell 里反复用不同视图/选项查询，无需重新跑模型。

#### 4.4.4 代码实践

**实践目标**：用老式 `tf.profiler.profile` 的 `op` 视图，在一次 `tf.function` 执行后找出耗时最高的算子类型。

```python
# 示例代码：需本地安装 tensorflow 后运行（TF1 风格 API，TF2 中位于 compat.v1）
import tensorflow as tf

@tf.function
def matmul_chain(x):
    y = x
    for _ in range(5):
        y = tf.matmul(y, tf.random.normal((64, 64)))
    return tf.reduce_sum(y)

# 用 RunOptions FULL_TRACE 跑一次，拿到带统计的 RunMetadata
from tensorflow.python.eager import context

# 构造一次可被 profile 的运行（这里用 compat.v1 RunMetadata 思路）
# 注意：TF2 Eager 下需借助 tf.profiler.experimental 采集，
#       下面展示经典的「op 视图 + stdout」离线分析写法（适用于图模式 / SavedModel）。

import tensorflow.compat.v1 as tf1
tf1.disable_v2_behavior()

g = tf1.Graph()
with g.as_default():
    x = tf1.placeholder(tf1.float32, (64, 64), name="x")
    y = x
    for i in range(5):
        y = tf1.matmul(y, tf1.random_normal((64, 64)), name="mm_%d" % i)
    loss = tf1.reduce_sum(y, name="loss")
    opt = tf1.train.GradientDescentOptimizer(0.01).minimize(loss)

with tf1.Session(graph=g) as sess:
    sess.run(tf1.global_variables_initializer())
    run_meta = tf1.RunMetadata()
    sess.run(opt, feed_dict={x: np.random.randn(64, 64).astype("float32")},
             options=tf1.RunOptions(trace_level=tf1.RunOptions.FULL_TRACE),
             run_metadata=run_meta)

    # 关键：用 op 视图按耗时排序，找出最贵的算子类型
    from tensorflow.python.profiler import option_builder
    opts = option_builder.ProfileOptionBuilder.time_and_memory()
    opts['order_by'] = 'micros'
    opts['select'] = ['micros', 'occurrence', 'device']
    opts['min_micros'] = 0
    tf1.profiler.profile(g, run_meta=run_meta, cmd='op', options=opts)
```

**操作步骤**：
1. `import numpy as np` 后运行脚本（需 `pip install numpy`）。
2. 观察终端打印的 op 视图表格。

**需要观察的现象**：表格按 `micros` 降序排列，会看到 `MatMul`、`MatMulGrad`、`RandomStandardNormal` 等算子类型，每行带「执行时间 / 占比 / 出现次数 / 所在设备」。

**预期结果**：反向梯度的 `MatMul`（梯度对权重的 `Conv2DBackpropFilter`/`MatMulGrad`）通常是耗时大头；`RandomStandardNormal` 因为要生成随机数也会占可观时间。

**据此优化**（这正是 profiler 的价值）：
- 若 `RandomStandardNormal` 占比过高 → 把随机数据**预生成成常量**或用 `tf.data` 预取，避免每步都生成。
- 若大量小算子（如 `Add`、`Switch`）累加耗时很高 → 用 **XLA 编译**（`@tf.function(jit_compile=True)`）把连续算子**融合**成少数大 kernel（承接 u7-l3 自动聚类）。
- 若 GPU 上算子 `accelerator_micros` 小、`cpu_micros` 大 → CPU 成了瓶颈，考虑增大 batch size 让 GPU 吃饱，或开 `prefetch` 让数据准备与计算重叠（承接 u5-l2 tf.data）。

> 待本地验证：具体耗时数值取决于机器与 TF 版本；重点是**相对占比**与 **CPU/加速器时间之比**，而非绝对数字。

#### 4.4.5 小练习与答案

**练习 1**：`micros`、`accelerator_micros`、`cpu_micros` 三者什么关系？为什么三者之和可能大于 step 的墙钟时间？

> **参考答案**：`micros = accelerator_micros + cpu_micros`。因为 GPU 异步执行、多核并行，不同算子的时间区间会重叠，所以「各算子 `micros` 之和」常常大于一次 step 实际经过的墙钟时间。

**练习 2**：`scope` 视图和 `op` 视图返回的 protobuf 类型为什么不一样？

> **参考答案**：`scope`/`graph` 视图里每个节点**一一对应**一个图节点，返回 `GraphNodeProto`；`op`/`code` 视图里每个节点是**多个图节点的聚合**（按 op 类型或按 Python 代码行），返回 `MultiGraphNodeProto`。

**练习 3**：`bytes` 为什么通常大于模型的峰值显存？

> **参考答案**：张量内存是引用计数的，释放难以精确追踪；profiler 只统计**分配请求**。一次 step 里反复分配/释放同一块内存，累计 `bytes` 会远大于任一时刻的真实占用（峰值）。看真实占用应看 `peak_bytes` 或生成 timeline。

---

## 5. 综合实践

把本讲三块知识（手动打点、采集会话、指标分析）串起来，完成一次**端到端的训练步骤性能剖析**。

**任务**：对一个极小的训练循环，用新式 profiler 采集 3 个 step，标定 step 边界，然后在 TensorBoard 里定位耗时构成，并写出至少两条优化假设。

```python
# 示例代码：需本地安装 tensorflow、tensorboard
import tensorflow as tf

logdir = "./train_logs"
model = tf.keras.Sequential([tf.keras.layers.Dense(128, input_shape=(784,)) for _ in range(4)])
opt = tf.keras.optimizers.SGD()

@tf.function
def step(x, y):
    with tf.GradientTape() as tape:
        pred = model(x)
        loss = tf.reduce_mean(tf.keras.losses.MSE(y, pred))
    grads = tape.gradient(loss, model.trainable_variables)
    opt.apply_gradients(zip(grads, model.trainable_variables))
    return loss

tf.profiler.experimental.start(logdir)
for i in range(5):
    # 用 Trace 标定 step 边界；跳过前两个预热 step，只采后三个（_r=1 表示根 trace）
    with tf.profiler.experimental.Trace("Train", step_num=i, _r=1):
        x = tf.random.normal((256, 784))
        y = tf.random.normal((256, 128))
        step(x, y)
tf.profiler.experimental.stop()
print("写入", logdir, "。运行: tensorboard --logdir", logdir)
```

**完成后请回答**（对照 TensorBoard 的 Overview 与 Trace Viewer）：

1. **Trace 机制**：为什么 `with tf.profiler.experimental.Trace("Train", ...)` 是必须的？如果去掉它，profiler 还能采到数据吗，还能按 step 区分吗？（提示：4.2——它标定 step 根边界。）
2. **耗时定位**：在 Trace Viewer 里，前向 `Dense`、反向 `gradient`、`SGD` 更新这三段，哪段占比最大？对应的算子主要是哪些？（提示：4.4 的时间指标。）
3. **瓶颈类型判断**：观察 GPU（若有）上的 kernel 间隙（gap）与 CPU 线程的等待。是**算子本身慢**（kernel 持续时间长），还是**喂不饱 GPU**（kernel 之间有大段空白）？据此给出优化方向。

**预期结论示例**（待本地验证）：
- 若 Trace Viewer 显示 kernel 之间有明显空白 → GPU 饥饿，优先优化**数据输入**（`tf.data` 的 `prefetch`）或减小 Python 开销。
- 若 `GradientTape` 反向算子（如 `MatMul` 的 backprop）耗时远超前向 → 反向是瓶颈，可考虑混合精度、XLA 融合或减小模型。
- 若 CPU 线程长时间阻塞在 `apply_gradients` 附近 → 优化器更新成了串行瓶颈，可考虑更大 batch 摊薄更新开销。

## 6. 本讲小结

- TensorFlow Profiler 有**两条路线**：老式 tfprof 基于 `RunMetadata` 做 op 级聚合统计（scope/graph/code/op 四视图）；新式采样式 profiler 基于 `TraceMe` + `XPlane`，把 CPU 打点、GPU/TPU 硬件事件统一到同一时间轴，主要在 TensorBoard 展示。
- `tf.profiler.experimental.Trace` 只是 C++ `TraceMe` 的薄壳。它靠模块级 `enabled()`（一个极低开销的 C++ 查询）实现「profiler 关闭时近乎零开销」，因此生产代码可永久保留打点。
- C++ 采集层三件套：`TraceMe`（构造记开始、`Stop()` 记结束，上交 `(名字,开始,结束)` 三元组）、`TraceMeRecorder`（每线程本地缓冲，靠 `Active(level)` 做级别过滤）、`ProfilerSession`（会话，`CollectData` 把所有记录合并进 `XSpace`）。
- trace 数据统一存进 `XSpace → XPlane → XLine → XEvent` 的 protobuf 容器，事件用皮秒级 `offset_ps`/`duration_ps` 定位，可嵌套。这是所有来源事件对齐到同一甘特图的底座。
- tfprof 的核心指标有三个时间（`micros = accelerator_micros + cpu_micros`）和四类内存（`bytes`/`peak_bytes`/`residual_bytes`/`output_bytes`）；由于 GPU 异步并发，各算子 `micros` 之和常大于墙钟时间。
- 定位瓶颈的关键不是看绝对耗时，而是看**相对占比**与 **CPU/加速器时间之比**：CPU 等待大、加速器空转 → 喂数据/批大小问题；反向算子重 → 考虑 XLA 融合、混合精度；连续小算子多 → XLA 自动聚类。

## 7. 下一步学习建议

- **立即上手**：在你的真实训练脚本上套用「综合实践」的模板，采集一次 trace，对照 TensorBoard 的 **Overview Page**（性能概览）和 **Trace Viewer**（时间线）练读图。
- **深入采集层**：阅读 `third_party/xla/third_party/tsl/tsl/profiler/backends/cpu/traceme_recorder.h`（线程本地缓冲与 `Active()`/`Record()` 的实现），以及 `tensorflow/core/profiler/convert/` 目录（`XSpace` 如何被转换成各种分析视图与 TensorBoard 数据）。
- **深入分析层**：阅读 `tensorflow/core/profiler/internal/tfprof_stats.cc`、`tfprof_show.cc`，理解四种视图的内存数据结构是如何从 `RunMetadata` 构建的；阅读 `g3doc/` 下的 `advise.md`、`profile_memory.md`，了解 `advise` 自动诊断与内存剖析。
- **跨讲义联系**：把本讲的「XLA 融合可减少小算子开销」结论与 u7-l3（JIT 自动聚类）、u5-l2（tf.data prefetch 缓解 GPU 饥饿）对照，理解「测量—诊断—优化」的闭环。
- **GPU/TPU 专项**：若关心设备利用率，进一步研究 profiler 的「设备能力」（device caps，如 GPU SM 数、带宽）如何与 `accelerator_micros` 结合，算出**达峰比例**（roofline 分析）。
