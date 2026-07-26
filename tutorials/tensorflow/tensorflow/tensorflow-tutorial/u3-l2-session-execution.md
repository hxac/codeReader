# 会话执行链路 Session 与 DirectSession

## 1. 本讲目标

本讲承接 [u3-l1（Graph 数据结构与 GraphDef）](u3-l1-graph-and-graphdef.md) 和 [u1-l5（C++ public 接口）](u1-l5-version-and-public-api.md)。在 u3-l1 中我们知道了「计算图在内存里是 `Graph`/`Node`/`Edge`，在磁盘上是扁平的 `GraphDef`」；在 u1-l5 中我们知道了「`Session` 是一个只暴露 `Create/Run/Close` 等纯虚方法的**抽象基类**，靠 `NewSession()` 经工厂选实现」。但这两讲都把一个问题悬置了：**一张建好的图，到底是怎么被一步步执行出结果来的？**

本讲就把这条链路彻底打通。读完本讲，你应当能够：

1. 说出 Python 侧 `session.run(fetches, feed_dict)` 从接收 Python 对象到跨越语言边界进入 C++，中间经过了哪几道「翻译」工序。
2. 画出 `DirectSession::Run` 的端到端时序：放置（placement）→ 剪枝（pruning）→ 优化（optimization）→ 分区（partition）→ 调度（schedule）→ 执行（execute）→ 回收（collect）。
3. 解释为什么 `Session::Create` 时**并不**放置图，而是推迟到第一次 `Run` 才按需放置。
4. 理解「同一组 feeds/fetches 复用已编译执行器」的缓存机制，以及跨设备子图为何要用 `ExecutorBarrier` + `Rendezvous` 协调。

> ⚠️ 关于执行模式：`tf.Session` 属于 TF1 的**图模式（graph mode）**，在 TF2 中默认是 Eager 模式。本讲讲的是图模式下「显式建图 + 显式 `Session.run`」的经典执行模型，它是理解后续 [u3-l3（Eager）](u3-l3-eager-execution.md)、[u7-l4（TFRT）](u7-l4-tfrt-runtime.md) 的对照基线。代码均可在 `tf.compat.v1` 下复现。

---

## 2. 前置知识

- **抽象类与工厂模式（u1-l5）**：`Session` 是抽象基类，`NewSession(options)` 不直接 `new` 具体类，而是问 `SessionFactory::GetFactory` 要一个能处理该 `options.target` 的工厂，由工厂造实例。这样调用方只依赖抽象接口，不与具体实现编译期耦合。
- **静态自动注册（u1-l5）**：C++ 里借助「全局静态对象的构造函数副作用」把工厂登记进一张全局表，`DirectSession` 就是这样在程序启动时自动注册为 `"DIRECT_SESSION"`。
- **Graph / Node / Edge / GraphDef（u3-l1）**：运行时图是带端口的有向对象图，序列化时是 `NodeDef` 列表、边折叠成 `input` 字符串。本讲要处理的是「把这张完整图，按 feeds/fetches 裁出子图、按设备切成多片」。
- **pywrap 桥（u1-l4）**：Python 通过 `pywrap_tensorflow` 加载承载 C++ 内核的 `.so`，Python 侧的 `tf_session` 模块就是这层桥。本讲里 Python 调 `tf_session.TF_SessionRun_wrapper(...)` 就是过这座桥。

几个本讲新出现的术语，先给直觉：

| 术语 | 直觉解释 |
|---|---|
| **Placement（放置）** | 决定图里每个 op 跑在哪个设备（CPU/GPU/…），结果是给 `Node` 打上 `assigned_device_name`。 |
| **Pruning（剪枝）** | 只保留「从 feeds 出发、能到达 fetches/targets」的必要 op，砍掉无关分支，减小执行子图。 |
| **Partition（分区）** | 按放置结果把子图按设备切成多张「设备子图」，跨设备的边自动插入 `_Send`/`_Recv` 节点。 |
| **Rendezvous（汇合点）** | 跨设备/跨执行器传递张量的「信箱」，发送方 `Send`、接收方 `Recv`，靠 key 配对。 |
| **Executor（执行器）** | 真正在单个设备上按拓扑序调度 op kernel 跑的引擎，一台设备一个执行器。 |

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `tensorflow/python/client/session.py` | Python 侧会话实现：`Session` / `BaseSession`。负责把 Python 的 `fetches`/`feed_dict` 翻译成扁平的张量列表，再调 pywrap 进入 C++。 |
| `tensorflow/core/public/session.h` | `Session` 抽象基类与 `NewSession()` 工厂入口（u1-l5 已精读，本讲只引用其契约）。 |
| `tensorflow/core/common_runtime/direct_session.cc` | 本讲主角：`DirectSession` 的全部实现——注册、`Create`、`Run`、`RunInternal`、`GetOrCreateExecutors`、`CreateGraphs`。 |
| `tensorflow/core/common_runtime/direct_session.h` | `DirectSession` 类声明与内部数据结构（`ExecutorsAndKeys`、`PerPartitionExecutorsAndLib`、`RunState`）。 |

两个最小模块：

- **`core.common_runtime.direct_session`**：图执行的主调度器（模块 4.2、4.3、4.4）。
- **`python.client.session`**：Python 客户端与跨语言桥（模块 4.1）。

---

## 4. 核心概念与源码讲解

### 4.1 Python 侧的会话接口：从 `run()` 到跨语言桥

#### 4.1.1 概念说明

用户在 Python 里写 `sess.run(y, feed_dict={x: 3.0})` 时，`fetches` 和 `feed_dict` 是高度灵活的——`fetches` 可以是单个 `Tensor`、嵌套 list/tuple/dict、`Operation` 甚至字符串名字；`feed_dict` 的 key 可以是 `Tensor`、`SparseTensor`、`IndexedSlices`。但 C++ 内核只认得**扁平的、类型确定的张量列表**。

因此 Python 侧 `BaseSession` 的核心职责是**翻译与规整**：把任意嵌套结构的 fetches/feed_dict 拆平（flatten）、把高级类型（SparseTensor 等）展开成普通 `Tensor`、把值转成 numpy 数组、做形状/类型校验，最后产出一个干净的 `(feed 列表, fetch 列表, target 列表)` 三元组交给 C++。这层「Python 做策略、C++ 做数据」的分工，在 u2-l4 已经见过。

#### 4.1.2 核心流程

`session.run()` 在 Python 侧的调用链：

```
Session.run(fetches, feed_dict, options, run_metadata)   # 868
  └─ 序列化 options/run_metadata 为 C buffer
  └─ _run(None, fetches, feed_dict, options_ptr, run_metadata_ptr)   # 1133
       ├─ 校验 session 未关闭、图非空
       ├─ 处理 feed_dict：nest.flatten_dict_items + _REGISTERED_EXPANSIONS 展开
       │    └─ 每个 feed 经 graph.as_graph_element 解析成 Tensor，值转 numpy
       ├─ _FetchHandler(fetches) 把 fetches 拆成 fetch_list + target_list
       └─ _do_run(handle, target_list, fetch_list, feed_dict_tensor, ...)   # 1359
            ├─ feeds/fetches/targets → C 句柄 (_as_tf_output / _c_op)
            ├─ _run_fn:
            │    ├─ _extend_graph()   # 把 Python 侧新增的 op 同步到运行时
            │    └─ _call_tf_sessionrun(...)   # 1481 → tf_session.TF_SessionRun_wrapper
            └─ _do_call(_run_fn, ...)   # 包一层异常翻译
```

注意 `_run_fn` 里两步的顺序：先 `_extend_graph()`，再 `_call_tf_sessionrun()`。这是因为 Python 侧的 `tf.Graph` 是**惰性**的——你不断往里加 op，运行时并不会立刻知道；每次 `run` 前要把「自上次以来新增的节点」提交（Extend）给 C++ 运行时，运行时才认得你这次要 fetch 的张量。

#### 4.1.3 源码精读

`Session.run` 的入口只做 buffer 包装，真正的活在 `_run`：

```python
# tensorflow/python/client/session.py:972-987
    options_ptr = tf_session.TF_NewBufferFromString(
        compat.as_bytes(options.SerializeToString())) if options else None
    run_metadata_ptr = tf_session.TF_NewBuffer() if run_metadata else None
    try:
      result = self._run(None, fetches, feed_dict, options_ptr, run_metadata_ptr)
      ...
    return result
```

`_run` 把 `feed_dict` 的值规整成 numpy，并用 `_REGISTERED_EXPANSIONS` 注册表把 SparseTensor/IndexedSlices 等展开为普通张量：

```python
# tensorflow/python/client/session.py:1201-1202
          feed_dict_tensor[subfeed_t.ref()] = np_val
          feed_map[compat.as_bytes(subfeed_t.name)] = (subfeed_t, subfeed_val)
```

`_do_run` 是跨语言前的最后一站：把 Python 的 `Tensor`/`Operation` 转成 C 侧的输出句柄，然后定义真正干活的 `_run_fn`：

```python
# tensorflow/python/client/session.py:1387-1391
    def _run_fn(feed_dict, fetch_list, target_list, options, run_metadata):
      # Ensure any changes to the graph are reflected in the runtime.
      self._extend_graph()
      return self._call_tf_sessionrun(options, feed_dict, fetch_list,
                                      target_list, run_metadata)
```

`_extend_graph` 调用 pywrap 的 `ExtendSession` 把新增节点提交给运行时：

```python
# tensorflow/python/client/session.py:1428-1430
  def _extend_graph(self):
    with self._graph._session_run_lock():  # pylint: disable=protected-access
      tf_session.ExtendSession(self._session)
```

最后 `_call_tf_sessionrun` 一行就是语言边界，`tf_session` 即 `pywrap_tf_session`，`TF_SessionRun_wrapper` 进入 C API、最终抵达 `DirectSession::Run`：

```python
# tensorflow/python/client/session.py:1481-1485
  def _call_tf_sessionrun(self, options, feed_dict, fetch_list, target_list,
                          run_metadata):
    return tf_session.TF_SessionRun_wrapper(self._session, options, feed_dict,
                                            fetch_list, target_list,
                                            run_metadata)
```

而 `BaseSession` 在构造时就用 `TF_NewSessionRef` 建好了那个 `self._session` 句柄——这一步才真正触发了 C++ 侧 `NewSession → DirectSessionFactory::NewSession`（见模块 4.2）：

```python
# tensorflow/python/client/session.py:716-724
    self._session = None
    opts = tf_session.TF_NewSessionOptions(target=self._target, config=config)
    try:
      with self._graph._c_graph.get() as c_graph:
        self._session = tf_session.TF_NewSessionRef(c_graph, opts)
    finally:
      tf_session.TF_DeleteSessionOptions(opts)
```

> 🔗 [tensorflow/python/client/session.py:716-724](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/client/session.py#L716-L724) 构造 C 会话句柄；[tensorflow/python/client/session.py:1387-1391](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/client/session.py#L1387-L1391) `run` 真正的执行函数；[tensorflow/python/client/session.py:1481-1485](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/client/session.py#L1481-L1485) 跨语言调用点。

#### 4.1.4 代码实践

**实践目标**：理解 Python 侧 `run` 在跨语言前对 `fetches`/`feed_dict` 做了哪些规整。

**操作步骤**（源码阅读型）：

1. 打开 `tensorflow/python/client/session.py`，定位 `_REGISTERED_EXPANSIONS`（约 116 行）。
2. 回答：当 `fetches` 是一个 `tf.sparse.SparseTensor` 时，`_FetchHandler` 会把它展开成哪几个底层张量？依据是哪个扩展规则？
3. 定位 `_do_run`（1359 行）与 `_call_tf_sessionrun`（1481 行），写出从 `session.run(...)` 到 `TF_SessionRun_wrapper` 之间经过的方法名序列。

**预期结果**：你能列出 `run → _run → _do_run → _do_call → _run_fn → _call_tf_sessionrun` 这条链，并指出 `_run_fn` 内部「先 `_extend_graph` 再 `_call_tf_sessionrun`」的顺序。

> 若本地已安装 TensorFlow，可选运行（待本地验证）：
>
> ```python
> # 示例代码（非项目源码）
> import tensorflow.compat.v1 as tf1
> tf1.disable_v2_behavior()
> x = tf1.placeholder(tf1.float32)
> y = x * 2 + 1
> with tf1.Session() as s:
>     print(s.run(y, feed_dict={x: 3.0}))   # 预期 7.0
> ```
>
> 观察现象：`y` 是单个 Tensor，`_FetchHandler` 不做展开；`x` 是 placeholder 被 feed。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_run_fn` 里要先调 `_extend_graph()`，而不是在 `Session` 构造时就把整张图一次性交给运行时？

**答案**：Python 侧 `tf.Graph` 支持在 `Session` 创建后继续往里加 op（典型场景：先建部分图、跑几步、再加几层）。运行时持有的图必须与 Python 图同步，所以每次 `run` 前都要把「自上次 Extend 之后新增的节点」补交上去；构造时一次性提交无法覆盖后续增量。

**练习 2**：`fetches` 为一个 dict `{"a": t1, "b": [t2, t3]}` 时，C++ 收到的是字典还是列表？

**答案**：C++ 收到的是**扁平的张量名列表**（`[t1, t2, t3]` 对应的输出端点）。嵌套结构由 Python 侧 `_FetchHandler` 记录，执行完后用它把扁平结果「重新组装（build_results）」回原来的 dict/list 形状返回给用户。

**练习 3**：`_REGISTERED_EXPANSIONS` 的最后一项 `(object, ...)` 起什么作用？

**答案**：它是兜底规则——任何不属于 `SparseTensor`/`IndexedSlices` 的类型，都按「单个 Tensor，不展开」处理（`fetch_fn` 返回 `([fetch], lambda v: v[0])`）。注册表按顺序匹配，兜底项放最后，保证自定义类型有机会被前面的规则截获。

---

### 4.2 DirectSession 的注册、构造与 Create

#### 4.2.1 概念说明

`DirectSession` 是 `Session` 抽象基类的**默认本地实现**：当 `SessionOptions.target` 为空（即不连远程 `grpc://` 目标）、且未启用 TFRT 时，`NewSession()` 就会造一个 `DirectSession`。它把「同一进程内的设备（CPU、本地 GPU）」拢在一起，直接在本进程内驱动计算，没有网络开销。

理解 DirectSession 要抓住三件事：

1. **它如何被选中**：靠工厂的 `AcceptsOptions` 谓词。
2. **它何时建设备**：在 `NewSession` 时就 `DeviceFactory::AddDevices` 把可用设备造出来（CPU/GPU/…）。
3. **`Create(graph)` 做了什么、没做什么**：只把 `GraphDef` 存进 `GraphExecutionState`，**不**放置、**不**分区。真正的「按需放置 + 分区」推迟到第一次 `Run`。这是 TF 的一个关键设计——同一张图，不同 feeds/fetches 会裁出不同子图，放置结果也不同，所以放置必须延迟到 Run 时按需做。

#### 4.2.2 核心流程

```
程序启动
  └─ DirectSessionRegistrar 全局对象构造
       └─ SessionFactory::Register("DIRECT_SESSION", new DirectSessionFactory)

NewSession(options)                         # 用户/python 调用
  └─ SessionFactory::GetFactory(options)    # 用 AcceptsOptions 选工厂
       └─ DirectSessionFactory::AcceptsOptions == true  (target 空 & 非 TFRT)
  └─ DirectSessionFactory::NewSession
       ├─ DeviceFactory::AddDevices(...)    # 造 CPU/GPU/... 设备
       └─ new DirectSession(options, new StaticDeviceMgr(devices), this)

session->Create(graph)                      # 提交图
  └─ DirectSession::Create(graph&&)
       └─ ExtendLocked(graph)
            └─ GraphExecutionState::MakeForBaseGraph(...)  # 存图，不放置
                 ├─ execution_state_  ← 持有完整图 + 函数库
                 └─ flib_def_         ← FunctionLibraryDefinition
```

#### 4.2.3 源码精读

静态注册——一个文件作用域的全局对象，构造函数里登记工厂（u1-l5 讲过的「静态自动注册」在这里落地）：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:296-302
class DirectSessionRegistrar {
 public:
  DirectSessionRegistrar() {
    SessionFactory::Register("DIRECT_SESSION", new DirectSessionFactory());
  }
};
static DirectSessionRegistrar registrar;
```

工厂用谓词决定自己是否受理这次 `NewSession`——`target` 必须为空（本地）、且未走 TFRT：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:209-213
  bool AcceptsOptions(const SessionOptions& options) override {
    return options.target.empty() &&
           !options.config.experimental().use_tfrt() &&
           GetDefaultLocalSessionImpl() == LocalSessionImpl::kDirectSession;
  }
```

`NewSession` 里关键的设备创建：先 `AddDevices` 把本机设备造全，再用 `StaticDeviceMgr` 包起来传给 `DirectSession`：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:239-244
    std::vector<std::unique_ptr<Device>> devices;
    TF_RETURN_IF_ERROR(DeviceFactory::AddDevices(
        options, "/job:localhost/replica:0/task:0", &devices));

    DirectSession* session = new DirectSession(
        options, new StaticDeviceMgr(std::move(devices)), this);
```

> 注意设备名前缀 `/job:localhost/replica:0/task:0`——DirectSession 是单进程本地会话，所有设备都挂在这个 localhost job 下。

`Create` 只做「存图」，把图交给 `GraphExecutionState`，并构建函数库。注意 `graph_created_` 标志位防止重复 Create：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:485-496
absl::Status DirectSession::Create(GraphDef&& graph) {
  TF_RETURN_IF_ERROR(init_error_);
  if (graph.node_size() > 0) {
    mutex_lock l(graph_state_lock_);
    if (graph_created_) {
      return absl::AlreadyExistsError(
          "A Graph has already been created for this session.");
    }
    return ExtendLocked(std::move(graph));
  }
  return absl::OkStatus();
}
```

`ExtendLocked` 里第一次提交时调 `MakeForBaseGraph` 建立 `execution_state_`——这里**没有**任何放置/分区代码：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:512-527
  if (!(flib_def_ && execution_state_)) {
    GraphExecutionStateOptions options;
    options.device_set = &device_set_;
    options.session_options = &options_;
    options.session_handle = session_handle_;
    TF_RETURN_IF_ERROR(GraphExecutionState::MakeForBaseGraph(
        std::move(graph), options, &execution_state_));
    flib_def_.reset(
        new FunctionLibraryDefinition(execution_state_->flib_def()));
    graph_created_ = true;
  }
```

> 🔗 [tensorflow/core/common_runtime/direct_session.cc:209-L213](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L209-L213) 工厂受理条件；[tensorflow/core/common_runtime/direct_session.cc:239-L244](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L239-L244) 建设备 + 构造 DirectSession；[tensorflow/core/common_runtime/direct_session.cc:485-L496](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L485-L496) Create 只存图；[tensorflow/core/common_runtime/direct_session.cc:512-L527](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L512-L527) 建立 execution_state，不放置。

#### 4.2.4 代码实践

**实践目标**：验证「Create 不放置、放置发生在 Run」这一设计。

**操作步骤**（源码阅读型）：

1. 在 `direct_session.cc` 中 `grep` 搜索 `assigned_device` 与 `Partition(`，确认它们只出现在 `CreateGraphs`（1681 行起，由 `Run` 间接调用）里，而**不**出现在 `Create`/`ExtendLocked`（485–537 行）里。
2. 阅读类成员 `execution_state_`（`direct_session.h:408`）与 `flib_def_`（`direct_session.h:414`）的注释，确认它们在 Create 后是「完整图 + 函数库」的持有者。
3. 解释：为什么不能在 Create 时就一次性放置好整张图？

**预期结果**：你能用一句话说明「同一张图，不同 fetches 会裁出不同子图，放置结果因此不同；且放置依赖设备集与配置，延迟到 Run 才能拿到完整上下文」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `SessionOptions.target` 设成 `"grpc://localhost:2222"`，`DirectSessionFactory::AcceptsOptions` 会返回什么？为什么？

**答案**：返回 `false`。因为 `options.target.empty()` 不成立。非空 target 表示要连远程 worker，会改由 `GrpcSessionFactory` 之类的远程会话工厂受理。DirectSession 只管本地同进程执行。

**练习 2**：`DirectSession` 的设备是何时、由谁创建的？

**答案**：在 `DirectSessionFactory::NewSession` 中，由 `DeviceFactory::AddDevices(options, "/job:localhost/replica:0/task:0", &devices)` 创建（direct_session.cc:240-241）。它在构造 DirectSession **之前**就把设备造好，再用 `StaticDeviceMgr` 持有传进去。所以「设备存在」早于「图提交」。

**练习 3**：同一个 `DirectSession` 第二次调用 `Create(graph)` 会发生什么？

**答案**：返回 `AlreadyExistsError("A Graph has already been created for this session.")`（direct_session.cc:489-492）。想换图必须先 `Close()` 再新建会话；若只是**追加**节点，应改用 `Extend()`。

---

### 4.3 GetOrCreateExecutors 与图分区：按需剪枝、放置、优化与分区

#### 4.3.1 概念说明

`DirectSession::Run` 一进来，第一件大事不是执行，而是**为本次 feeds/fetches 准备好「执行器（Executor）」**。执行器不是每次都重建的——同一种 `(inputs, outputs, targets)` 组合会被缓存复用，因为「裁子图 + 放置 + 分区 + 编译」很贵，而训练循环里同一组 fetch 会重复调用成千上万次。

这套机制由三个关键数据结构承载（见 `direct_session.h`）：

- **`ExecutorsAndKeys`**（direct_session.h:168）：对应**一组** feeds/fetches 的全部执行产物。它的 `items` 是一个 `vector<PerPartitionExecutorsAndLib>`——**每个设备分区一个执行器**。
- **`PerPartitionExecutorsAndLib`**（direct_session.h:153）：单个设备分区上的 `(graph, device, flib, executor)` 四元组。
- **`executors_`**（direct_session.h:380）：一张 `key → shared_ptr<ExecutorsAndKeys>` 的缓存表，key 是 feeds/fetches/targets 拼出来的字符串。

而把「完整图」变成「若干设备子图」的过程在 `CreateGraphs` 里完成，依次是：**剪枝 → 放置 → 优化 → 分区 → 设备改写**。

#### 4.3.2 核心流程

```
DirectSession::Run(...)                                  # 923
  ├─ GetOrCreateExecutors(inputs, outputs, targets, &)   # 950
  │    ├─ fast key 命中缓存？ → 直接返回                  # 1584-1601
  │    ├─ sorted key 命中缓存？ → 直接返回                # 1609-1634
  │    └─ 未命中 → CreateExecutors → CreateGraphs        # 1659
  │
  │   CreateGraphs(...)                                 # 1681
  │     ├─ execution_state->BuildGraph(subgraph_options) # 1711  剪枝+放置+优化
  │     ├─ Partition(popts, &client_graph->graph, ...)   # 1776  按设备切片
  │     ├─ 每个分区：ConvertGraphDefToGraph              # 1799-1810
  │     └─ 每个设备：d->MaybeRewriteGraph(graph)         # 1831
  │
  │    → 产出 unordered_map<device_name, Graph> 分区子图
  │    → 由 CreateExecutors 为每个分区造一个 Executor
  │
  ├─ FunctionCallFrame + SetArgs(feed)                   # 960-974
  ├─ RunInternal(...)                                    # 987  → 见 4.4
  └─ call_frame.ConsumeRetvals(&outputs)                 # 994  回收结果
```

缓存 key 的设计值得一看——它用 feeds/fetches/targets 拼字符串，并且做了**两次查找**：先按原始顺序（fast path），再按排序后（slow path），这样即使用户两次 run 传入的 fetches 顺序不同，也能命中同一份执行器：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:1584-1601
  // Fast lookup path, no sorting.
  const std::string key = strings::StrCat(
      absl::StrJoin(inputs, ","), "->", absl::StrJoin(outputs, ","), "/",
      absl::StrJoin(target_nodes, ","), "/", run_state_args->is_partial_run,
      "/", debug_tensor_watches_summary);
  ...
  {
    mutex_lock l(executor_lock_);  // could use reader lock
    auto it = executors_.find(key);
    if (it != executors_.end()) {
      *executors_and_keys = it->second.get();
      return absl::OkStatus();
    }
  }
```

`CreateGraphs` 的「分区」这一步：分区函数 `Partition` 按**每个节点的 `assigned_device_name`** 决定它属于哪个设备子图（`popts.node_to_loc`）：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:1759-1776
  // Partition the graph across devices.
  PartitionOptions popts;
  popts.node_to_loc = [](const Node* node) {
    return node->assigned_device_name();
  };
  ...
  std::unordered_map<std::string, GraphDef> partitions;
  TF_RETURN_IF_ERROR(Partition(popts, &client_graph->graph, &partitions));
```

> 分区时，跨越设备边界的数据边会被自动改写：发送端插入 `_Send`、接收端插入 `_Recv`，二者通过 Rendezvous 的 key 配对（u3-l1 讲过的控制边用 `^name`，这里是数据边跨设备的情形）。

分区后，每个设备子图还给设备一次「改写自己子图」的机会（比如 GPU 设备插入 `_Send`/`_Recv` 的设备端实现、XLA 聚类改写等）：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:1827-1834
    // Give the device an opportunity to rewrite its subgraph.
    Device* d;
    s = device_mgr_->LookupDevice(partition_name, &d);
    if (!s.ok()) break;
    s = d->MaybeRewriteGraph(graph);
```

> 🔗 [tensorflow/core/common_runtime/direct_session.cc:1568-L1679](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L1568-L1679) 执行器缓存查找/构建；[tensorflow/core/common_runtime/direct_session.cc:1681-L1714](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L1681-L1714) BuildGraph（剪枝+放置+优化）；[tensorflow/core/common_runtime/direct_session.cc:1759-L1776](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L1759-L1776) 按设备分区；[tensorflow/core/common_runtime/direct_session.cc:1827-L1834](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L1827-L1834) 设备级改写。结构体定义见 [tensorflow/core/common_runtime/direct_session.h:153-L186](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.h#L153-L186)。

#### 4.3.3 概念补充：为什么要剪枝

完整图里可能有大量与本次 fetch 无关的节点（比如只 fetch 推理输出时，训练专用的梯度子图就是噪声）。`BuildGraph` 先做**反向可达性剪枝**：从 fetches/targets 出发反向遍历，只保留能被 feeds「喂到」、且能到达 fetches 的节点。这样执行子图最小化，放置和调度都更轻。

设完整图节点集为 \(V\)、feed 集为 \(F\)、fetch 集为 \(Y\)，则剪枝后保留的节点集近似为：

\[
V_{\text{sub}} \approx \{v \in V \mid \exists\, f \in F,\ y \in Y,\ \text{path}(f \to v) \wedge \text{path}(v \to y)\}
\]

（严格实现还涉及控制依赖与死分支剔除，这里只给直觉。）

#### 4.3.4 代码实践

**实践目标**：追踪「一组 feeds/fetches → 一个 ExecutorsAndKeys（含若干设备分区执行器）」的构建与缓存。

**操作步骤**（源码阅读型）：

1. 在 `direct_session.cc` 中打开 `GetOrCreateExecutors`（1568 行）。画出「fast key → sorted key → CreateExecutors」的三级查找流程。
2. 打开 `CreateGraphs`（1681 行），依次标注：`BuildGraph`（1711）、`Partition`（1776）、`MaybeRewriteGraph`（1831）三处分别在做什么。
3. 回答：如果两次 `run` 传入的 `fetch_list` 顺序不同但内容相同，会命中同一份 `ExecutorsAndKeys` 吗？依据是哪几行？

**预期结果**：你能解释 fast key 用原始顺序、sorted key 用排序后顺序，所以内容相同、顺序不同的两次 run 第二次会命中 sorted key 分支（1609-1634）。

> 可选本地观察（待本地验证）：用一个含 CPU 上 op 的小图，`run` 两次不同顺序的 fetches，第二次的耗时应明显低于第一次（命中缓存，跳过了 BuildGraph/Partition）。

#### 4.3.5 小练习与答案

**练习 1**：`ExecutorsAndKeys::items` 为什么是一个 **vector**（多个），而不是单个执行器？

**答案**：因为一张图经 `Partition` 后会被切成**每个设备一份**子图，每个设备子图配一个独立的 `Executor`。`items` 的每个元素是一个 `PerPartitionExecutorsAndLib`（含一个设备、一张子图、一个执行器）。单设备图 `items.size()==1`，多设备图 `items.size()>1`。

**练习 2**：分区函数依据什么把节点归到某个设备子图？

**答案**：依据每个 `Node` 的 `assigned_device_name()`（direct_session.cc:1761-1763 的 `popts.node_to_loc`）。这个赋值在更早的 `BuildGraph`/放置阶段就已经完成，分区只是按它把节点分组。

**练习 3**：`MaybeRewriteGraph` 是给谁用的？请举一个后续讲义会涉及的场景。

**答案**：它是给**具体设备**改写自己那份子图的机会。典型场景是 GPU/XLA：设备可以在自己的子图里做算子融合、插入设备特定的 Send/Recv 实现、或做 XLA 聚类改写。这与 [u7-l3（JIT 自动聚类）](u7-l3-jit-autoclustering.md) 直接相关。

---

### 4.4 RunInternal：Executor 的同步/异步调度与结果回收

#### 4.4.1 概念说明

执行器准备好后，`Run` 把 feed 装进 `FunctionCallFrame`，分配一个全局唯一的 `step_id`，然后交给 `RunInternal`。`RunInternal` 是真正「按下执行按钮」的地方，它要解决一个核心问题：**多个设备的执行器如何并行启动、又如何让调用方等到全部完成？**

答案分两条路径：

- **同步路径**：只有单个执行器（单设备）且无超时时，直接在调用线程里 `executor->Run(args)` 阻塞跑完，没有线程切换开销。
- **异步路径**：多个执行器（多设备）时，给每个执行器发 `RunAsync`，用一个 `ExecutorBarrier`（计数为 N 的屏障）等所有执行器完成；屏障在最后一个执行器结束时回调通知。

跨执行器/跨设备的数据传递则统一走 `Rendezvous`：发送端 `Send(key, tensor)`、接收端 `Recv(key)` 配对，屏蔽了「张量在哪个设备上算出来」的细节。

#### 4.4.2 核心流程

```
RunInternal(step_id, run_options, call_frame, executors_and_keys, ...)   # 578
  ├─ 构造 RunState（本次 step 的状态容器）
  ├─ 选线程池/Runner：
  │    ├─ can_execute_synchronously (单执行器 & 无超时)?  → pool=nullptr
  │    └─ 否则 → pool 或 RunHandler
  ├─ 装填 Executor::Args（rendezvous、cancellation_manager、runner、call_frame…）
  │
  ├─ if 同步:                                              # 808
  │     rendezvous = PrivateIntraProcessRendezvous
  │     item.executor->Run(args)                          # 阻塞执行
  │
  └─ else 异步:                                            # 815
       rendezvous = RefCountedIntraProcessRendezvous
       barrier = new ExecutorBarrier(num_executors, rendezvous, done_callback)
       for item in items:
           item.executor->RunAsync(args, barrier->Get())  # 每个执行器异步跑
       WaitForNotification(&executors_done, ...)          # 等屏障通知
       run_status = run_state.status

  → 返回后，Run 用 call_frame.ConsumeRetvals(&outputs) 回收结果   # 994
```

#### 4.4.3 源码精读

`Run` 在拿到执行器后，构造 call frame、装 feed、分配 step_id、调用 `RunInternal`，最后用 `ConsumeRetvals` 取回输出：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:960-989
  FunctionCallFrame call_frame(executors_and_keys->input_types,
                               executors_and_keys->output_types);
  ...
  const absl::Status s = call_frame.SetArgs(feed_args);
  ...
  const int64_t step_id = step_id_counter_.fetch_add(1);
  ...
  TF_RETURN_IF_ERROR(RunInternal(step_id, run_options, &call_frame,
                                 executors_and_keys, run_metadata,
                                 threadpool_options));
```

`RunInternal` 里决定同步/异步的关键判断——单执行器且无超时才能同步：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:727-728
  const bool can_execute_synchronously =
      executors_and_keys->items.size() == 1 && call_timeout == 0;
```

同步路径，直接在调用线程跑，`run_all_kernels_inline` 为真：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:808-814
  if (can_execute_synchronously) {
    PrivateIntraProcessRendezvous rendezvous(device_mgr_.get());
    args.rendezvous = &rendezvous;

    const auto& item = executors_and_keys->items[0];
    set_threadpool_args_for_item(item, &args);
    run_status = item.executor->Run(args);
```

异步路径，`ExecutorBarrier` 计数为执行器个数，每个执行器结束时回调屏障，最后一个触发 `executors_done.Notify()`：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:816-838
  } else {
    core::RefCountPtr<RefCountedIntraProcessRendezvous> rendezvous(
        new RefCountedIntraProcessRendezvous(device_mgr_.get()));
    args.rendezvous = rendezvous.get();

    // `barrier` will delete itself after the final executor finishes.
    absl::Notification executors_done;
    ExecutorBarrier* barrier = new ExecutorBarrier(
        num_executors, rendezvous.get(),
        [&run_state, &executors_done](const absl::Status& ret) {
          {
            mutex_lock l(run_state.mu);
            run_state.status.Update(ret);
          }
          executors_done.Notify();
        });

    for (const auto& item : executors_and_keys->items) {
      set_threadpool_args_for_item(item, &args);
      item.executor->RunAsync(args, barrier->Get());
    }

    WaitForNotification(&executors_done, &run_state, &step_cancellation_manager,
                        call_timeout);
```

`Executor::Args` 是把本次 step 的所有「执行上下文」打包传给执行器的容器，关键字段如下：

```cpp
// tensorflow/core/common_runtime/direct_session.cc:730-745
  Executor::Args args;
  args.step_id = step_id;
  args.call_frame = call_frame;
  args.collective_executor = ...;
  args.session_config = &options_.config;
  args.session_state = &session_state_;
  args.session_handle = session_handle_;
  args.tensor_store = &run_state.tensor_store;
  args.step_container = &run_state.step_container;
  args.sync_on_finish = sync_on_finish_;
  args.user_intra_op_threadpool = threadpool_options.intra_op_threadpool;
  args.run_all_kernels_inline = pool == nullptr;
  args.start_time_usecs = start_time_usecs;
  args.deadline = deadline;
```

> 🔗 [tensorflow/core/common_runtime/direct_session.cc:923-L1032](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L923-L1032) `Run` 主流程；[tensorflow/core/common_runtime/direct_session.cc:578-L911](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L578-L911) `RunInternal` 全貌；[tensorflow/core/common_runtime/direct_session.cc:808-L838](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L808-L838) 同步 vs 异步两条路径。

#### 4.4.4 概念补充：ExecutorBarrier 与 Rendezvous 如何协作

`ExecutorBarrier(count, rendezvous, done)` 是个计数屏障：初始化计数为执行器个数 `count`，每个执行器结束时调 `barrier->Get()` 返回的回调，该回调做两件事——先把这次的状态 `Update` 进 `run_state.status`，再把计数减一；计数归零时，触发 `done`（即 `executors_done.Notify()`）并销毁 barrier 自身、释放 rendezvous。

跨设备的数据依赖则这样解耦：设备 A 的执行器算出张量后 `rendezvous->Send(key, tensor)` 继续往下跑，不阻塞等设备 B；设备 B 的执行器跑到需要该张量时 `rendezvous->Recv(key)`，若尚未到达则挂起，到达后被唤醒。这样多个设备的执行器能真正并行推进，而不是串行等待。

#### 4.4.5 代码实践

**实践目标**：理解同步与异步两条执行路径的触发条件，以及结果如何回到调用方。

**操作步骤**（源码阅读型）：

1. 在 `RunInternal`（578 行）定位 `can_execute_synchronously`（727 行），写出它的两个条件。
2. 顺着异步分支（815-838 行），回答：`ExecutorBarrier` 的计数初值是多少？谁负责 `Notify`？`WaitForNotification` 在等什么？
3. 回到 `Run`（923 行），阅读 `ConsumeRetvals`（994 行）附近，说明执行器产出的结果是如何按 `output_name_to_index` 重排成用户要的 `outputs` 顺序的。

**预期结果**：你能讲清「单设备无超时 → 同步 `executor->Run`；多设备或有超时 → 异步 `RunAsync` + 屏障 + `WaitForNotification`」，以及结果经 call frame 回流的过程。

> 若想观察超时对路径的影响（待本地验证）：构造一次会 `DeadlineExceededError` 的 run（如设置极小的 `timeout_in_ms` 配合慢 op），由 `WaitForNotification` 触发取消，观察 `step_cancellation_manager` 的作用。

#### 4.4.6 小练习与答案

**练习 1**：为什么「单执行器 + 无超时」时选择同步执行，而不是统一走异步？

**答案**：单设备子图没有跨设备协调需求，无超时也无需异步以便触发取消，因此直接在调用线程里 `executor->Run(args)` 阻塞执行（`run_all_kernels_inline=true`）能省掉线程池调度与屏障开销，延迟最低。多设备时则必须异步，否则无法让多个执行器并行推进。

**练习 2**：`step_id`（981 行 `step_id_counter_.fetch_add(1)`）在整个执行中起什么作用？

**答案**：`step_id` 是本次 `Run`（一个 step）的全局唯一标识。它用于 profiler 的 TraceMe 关联（589-607 行把 `step_id` 编进 `SessionRun` 事件）、Rendezvous 中跨设备张量的命名空间隔离（不同 step 的同名张量不会混淆）、以及 collective 通信的 step 上下文。简言之，它让「同一次 run 内的众多 op 和跨设备消息」能被正确归并。

**练习 3**：如果异步路径中某个执行器失败了，调用方如何得知？

**答案**：失败执行器的状态会经 `barrier` 的回调 `run_state.status.Update(ret)`（824-828 行）汇入 `run_state.status`；屏障归零后 `executors_done.Notify()`，`WaitForNotification` 返回，随后 `run_status = run_state.status`（840-842 行）取出该状态，最终由 `TF_RETURN_IF_ERROR(run_status)`（854 行）把错误返回给调用方。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「端到端时序梳理 + 分阶段标注」任务。

**任务背景**：下面是一段最小图模式代码（**示例代码**，非项目源码）：

```python
# 示例代码
import tensorflow.compat.v1 as tf1
tf1.disable_v2_behavior()
g = tf1.Graph()
with g.as_default():
    x = tf1.placeholder(tf1.float32, name="x")
    w = tf1.constant(2.0, name="w")
    y = tf1.multiply(x, w, name="y")     # y = x * 2
    z = tf1.add(y, 1.0, name="z")        # z = y + 1
with tf1.Session(graph=g) as s:
    print(s.run(z, feed_dict={x: 3.0}))  # 预期 7.0
```

**你要做的**：

1. **Python 侧翻译**（对应模块 4.1）：写出 `s.run(z, feed_dict={x: 3.0})` 从 Python 到 `TF_SessionRun_wrapper` 的方法调用链；指出 `fetches=z`、`feed_dict={x:3.0}` 分别被规整成什么样的扁平结构。
2. **会话构造与 Create**（对应模块 4.2）：说明 `tf1.Session(graph=g)` 时设备是何时创建的、`Create` 在「建图」与「放置」之间做了哪一步、没做哪一步。
3. **执行器构建**（对应模块 4.3）：本次 run 的 `(inputs=["x:0"], outputs=["z:0"], targets=[])` 会触发 `GetOrCreateExecutors` 走「缓存命中」还是「新建」分支？经过 `CreateGraphs` 的哪几个阶段？由于本例全是 CPU op，分区后 `items.size()` 应为多少？
4. **执行与回收**（对应模块 4.4）：本例应走同步路径还是异步路径？依据是什么？结果 `7.0` 经由哪个对象回到 Python？
5. **画一张时序图**：把上述四个阶段画成一条从左到右的时间线，标注：`run → _run → _do_run → TF_SessionRun_wrapper → DirectSession::Run → GetOrCreateExecutors(+CreateGraphs) → RunInternal → executor->Run → ConsumeRetvals → 返回`。

**验收标准**：

- 能准确说出 `Create` 不放置、放置发生在 `Run` 内的 `CreateGraphs`。
- 能说出本例 `items.size()==1`、走同步路径（单执行器且无超时）。
- 能指出跨设备时才会出现 `Partition` 多片 + `ExecutorBarrier` + 异步 `RunAsync`。

> 运行结果与具体行号请以本地实际版本为准；若本地未安装 TensorFlow，本任务可纯靠源码阅读完成（标注「待本地验证」）。

---

## 6. 本讲小结

- **Python 侧只做翻译**：`session.run` 把任意嵌套的 `fetches`/`feed_dict` 经 `_run → _do_run` 拆平、转 numpy、校验形状，最后由 `_call_tf_sessionrun`（`TF_SessionRun_wrapper`）跨语言进入 C++；每次 run 前先 `_extend_graph` 同步新增节点。
- **DirectSession 靠工厂与静态注册接入**：`DirectSessionRegistrar` 在启动时登记 `"DIRECT_SESSION"` 工厂；`AcceptsOptions` 在 `target` 为空且非 TFRT 时受理；`NewSession` 时由 `DeviceFactory::AddDevices` 建好设备。
- **Create 只存图、不放置**：`DirectSession::Create` 把 `GraphDef` 交给 `GraphExecutionState` 持有，建立 `execution_state_` 与 `flib_def_`，放置/分区一律推迟到 `Run`。
- **执行器按 feeds/fetches 缓存复用**：`GetOrCreateExecutors` 用 fast/sorted 两次 key 查缓存；未命中时 `CreateGraphs` 做「剪枝 → 放置 → 优化 → 分区 → 设备改写」，产出每个设备一个 `Executor`。
- **RunInternal 分同步/异步两条路径**：单执行器且无超时走同步 `executor->Run`；多执行器走异步 `RunAsync` + `ExecutorBarrier` + `WaitForNotification`，跨设备数据经 `Rendezvous` 配对传递。
- **结果经 call frame 回收**：执行器把输出写回 `FunctionCallFrame`，`Run` 用 `ConsumeRetvals` 取出并按 `output_name_to_index` 重排成用户要的顺序。

---

## 7. 下一步学习建议

- **下一讲 [u3-l3 Eager 执行模式](u3-l3-eager-execution.md)**：本讲的 `Session.run` 是「攒一批 op 一次性执行」的图模式；Eager 模式下每个 op 立即派发执行，`Context/execute` 取代 `Session` 成为默认入口。对照学习能让你看清「图 vs 立即」两种执行模型的本质差异。
- **延伸阅读源码**：
  - `tensorflow/core/common_runtime/executor.{h,cc}` 与 `tensorflow/core/common_runtime/executor_factory.cc`——本讲把执行器当成黑盒调了它的 `Run/RunAsync`，下一站可以打开看它**内部**如何按拓扑序调度 kernel。
  - `tensorflow/core/common_runtime/rendezvous_mgr.{h,cc}`——跨设备 `Send/Recv` 的具体实现。
  - `tensorflow/core/common_runtime/graph_execution_state.cc`——`BuildGraph` 里剪枝与放置的具体算法。
- **横向对照**：学完 [u7-l4 TFRT](u7-l4-tfrt-runtime.md) 后，回头看本讲的 `DirectSession` + `Executor`，你会更清楚 TFRT 想用 BEF 执行器替换掉的正是这条链路里的哪几环。
