# cudaq::sample 与异步执行深入

> 本讲承接 u3-l1「执行模型：quantum_platform 与 QPU」。上一讲你已经看清一次 `cudaq::sample(kernel)` 是如何「创建 `ExecutionContext` 工单 → 经平台转发到 QPU → 在模拟器/设备上执行 → 终结回收结果」的整条骨架。本讲不再重复这条骨架，而是**zoom in 到 `sample` 自身**：它的参数与策略对象怎么组织、shot 循环怎么累积计数、`sample_async` 的「异步」到底异步在哪、以及如何把多组参数广播到多个 QPU 上并发采样。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `sample` 的三套 C++ 重载与 Python 签名各自接受什么参数，以及 `sample_options` / `sample_policy` 这两个「参数包」和「策略包」的关系。
- 解释 shot 循环 `while (counts.get_total_shots() < shots)` 的含义、计数是如何用 `operator+=` 累积的、以及本地模拟器为什么一次 `launch` 就能拿全 shots。
- 说清楚 `sample_async` 返回的 `async_sample_result` 是什么：它包了一层 `detail::future`，而 `future` 在本地是 `std::future<sample_result>`、在远程 QPU 上则是一组「服务器作业句柄」。
- 描述「每个 QPU 一条任务队列」的并发模型，以及 `sample(kernel, ArgumentSet{...})` 如何把 N 组参数切分到 `numQpus` 个 QPU 上并行执行。
- 动手用 `sample_async` 同时提交多个内核，回收结果并测耗时，与顺序 `sample` 对比。

本讲**不**深入具体模拟器算法（u6 的事），**不**深入 `observe` 的期望值计算（u3-l3 的事），**不**重复 u3-l1 已讲过的平台/QPU/`ExecutionContext` 基本概念。

## 2. 前置知识

进入源码前，先建立三个直觉。

### 2.1 「采样」是一个累加过程

所谓采样，就是把同一个内核重复执行 `shots` 次（每次叫一个 shot），把每次测量得到的比特串计数累加成一张「比特串 → 出现次数」的表。若某个比特串出现了 \(c\) 次，则它被观测到的概率估计为

\[
\hat{P}(\text{bitstring}) = \frac{c}{\text{shots}}
\]

这是蒙特卡洛估计，方差随 `shots` 增大而减小。CUDA-Q 的关键设计是：**这个累加过程不一定一次性完成**。运行时可能一次 `launch` 拿回全部 shots（本地模拟器常用），也可能分批拿回再累加。所以采样代码天然写成「while 还没攒够 shots 就再 launch 一次」的循环。

### 2.2 「异步」异步在哪

`cudaq::sample(kernel)` 是**同步**的：调用线程会一路阻塞，直到结果算完返回。`cudaq::sample_async(kernel)` 是**异步**的：它立刻返回一个「期票（future-like）」对象 `async_sample_result`，真正的采样在**别的执行线程**上跑；你之后调用期票的 `.get()` 时，如果那时采样还没完成，`.get()` 才会阻塞等结果。

异步的价值在于**重叠（overlap）**：你可以先连续提交多个 `sample_async`，让它们在后台排队/并行执行，期间主线程继续做别的事（比如再提交更多任务），最后统一回收结果。这在多 QPU 平台（mqpu）上能换来真实的并行加速，在单 QPU 平台上至少能 overlap 掉宿主侧的提交开销。

### 2.3 每个 QPU 一条队列

回顾 u3-l1：一个 `quantum_platform` 持有若干个 `QPU`。本讲要补的关键细节是：**每个 QPU 内部各有一条任务队列**。`platform.enqueueAsyncTask(qpu_id, task)` 就是把任务塞进**第 `qpu_id` 号 QPU** 的队列里。所以：

- 同一个 QPU 上的多个异步任务，按入队顺序**串行**执行（一条队列）。
- 不同 QPU 上的任务，**并行**执行（多条队列、多个工作线程）。

这就是「多 QPU 并发」的物理基础，也是后面「广播采样」能并行的原因。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `runtime/cudaq/algorithm.h` | 算法原语的总入口头文件，仅 `#include` 各子算法头；`sample` 的实现并不在此，而在 `sample.h`。 |
| `runtime/cudaq/algorithms/sample/options.h` | 定义 `DEFAULT_NUM_SHOTS` 与「参数包」`sample_options`（shots、noise、explicit_measurements）。 |
| `runtime/cudaq/algorithms/sample/policy.h` | 定义「策略包」`sample_policy`（名字、结果类型、选项、内核名）及其异步包装 `async_sample_policy`。 |
| `runtime/cudaq/algorithms/sample.h` | **本讲主战场**：`sample` / `sample_async` 的全部模板重载、`samplePreamble`、shot 循环 `runSampling`、异步版 `runSamplingAsync`、广播版 `sample(ArgumentSet)`。 |
| `runtime/cudaq/algorithms/launch.h` | `detail::launch`：把策略+上下文+内核送到 QPU 执行的通用分发器（库模式与 MLIR/QIR 模式在此分叉，u3-l1 已讲）。 |
| `runtime/cudaq/algorithms/broadcast.h` | `broadcastFunctionOverArguments`：把 N 组参数切分到 `numQpus` 个 QPU 并行执行的广播器。 |
| `runtime/common/Future.h` | `detail::future`（本地 `std::future` 或远程作业句柄）、`async_result<T>` 模板、`async_sample_result` 别名、`async_policy_wrapper`。 |
| `runtime/cudaq/platform/quantum_platform.cpp` | `enqueueAsyncTask` 的两个重载：把任务塞进对应 QPU 的队列并返回 `std::future`。 |
| `runtime/common/SampleResult.h` | `sample_result`：`operator+=` 累加计数、`get_total_shots` 查总 shots（u2-l3 已讲读取接口）。 |
| `python/cudaq/runtime/sample.py` | Python 端 `sample` / `sample_async` / `__broadcastSample` 与 `AsyncSampleResult` 包装类。 |
| `python/runtime/cudaq/algorithms/py_sample_async.cpp` | Python 绑定层 `sample_async_impl`：克隆 MLIR 模块、组装参数、调用 C++ `runSamplingAsync`。 |

## 4. 核心概念与源码讲解

本讲拆三个最小模块：**4.1 sample 参数与策略**、**4.2 sample_async 异步模型**、**4.3 并发采样**。

### 4.1 sample 参数与策略

#### 4.1.1 概念说明

要给 `sample` 传参，CUDA-Q 把「**用户可调的旋钮**」和「**运行时分派要用的标签**」分成两个东西：

- **参数包 `sample_options`**：纯粹描述「这次采样要多少 shots、要不要噪声、要不要显式测量顺序」。它跟「在哪执行、怎么分派」无关，只是用户意图。
- **策略包 `sample_policy`**：带一个静态名字 `"sample"`、一个结果类型 `sample_result`，外加运行时填进去的内核名、选项副本、噪声指针、重排序索引等。它是给执行管线看的「这次执行用 sample 语义来终结」的标签。

为什么要分两层？因为 CUDA-Q 把 sample / observe / run / get_state 都统一成「策略 + 通用 launch」的模式（见 u3-l1 的 `detail::launch`）。同一个 `launch` 函数能处理多种语义，靠的就是策略里的 `name` 字符串在终结阶段做分派。`sample_policy` 只是这套机制里「采样」那一格。

#### 4.1.2 核心流程

一次 `cudaq::sample(kernel, args...)` 的内部流程（C++ 端）：

1. 概念 `SampleCallValid` 在编译期校验：参数合法 **且** 内核返回类型为 `void`（采样不返回值，要返回值得用 `run`）。
2. 取平台 `get_platform()`、取内核名。
3. 调 `detail::runSampling(...)`，默认 `shots = DEFAULT_NUM_SHOTS = 1000`、`explicitMeasurements = false`。
4. `runSampling` 内：若该 QPU 是远程的，转走异步路径并立刻 `.get()` 阻塞回收（等于把远程也包成同步）。
5. 否则构造 `ExecutionContext("sample", shots, qpu_id)` 与 `sample_policy`，调 `samplePreamble` 填充它们（内核名、batch 信息、噪声、库模式下的中途测量探测）。
6. 进入 **shot 累加循环**：`while (counts.get_total_shots() < shots)` 反复 `detail::launch`，把每次结果 `counts += result` 累加；本地模拟器通常首轮就拿全 shots 并提前返回。
7. 返回累加好的 `sample_result`。

shot 累加的不变式：

\[
\text{get\_total\_shots}(\text{counts}) = \sum_{\text{bitstring}} c_{\text{bitstring}} \;\le\; \text{shots}
\]

循环结束时等号成立（除非内核无测量导致 0 shots，此时打印警告并跳出，避免死循环）。

#### 4.1.3 源码精读

先看参数包与默认 shots：[runtime/cudaq/algorithms/sample/options.h:14-27](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample/options.h#L14-L27) —— 第 14 行定义 `DEFAULT_NUM_SHOTS = 1000`；第 23–27 行的 `sample_options` 含 `shots`、`noise`、`explicit_measurements` 三字段，`shots` 默认 1000。

再看策略包：[runtime/cudaq/algorithms/sample/policy.h:25-52](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample/policy.h#L25-L52) —— `sample_policy` 第 27 行静态名字 `name[] = "sample"`、第 30 行结果类型 `result_type = sample_result`；第 52 行 `async_sample_policy = async_policy_wrapper<sample_policy>` 是它的异步包装（4.2 节细讲）。

编译期校验：[runtime/cudaq/algorithms/sample.h:46-49](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L46-L49) —— `SampleCallValid` 要求 `ValidArgumentsPassed` 且 `HasVoidReturnType`，从根上挡住「非 void 内核用 sample」。

三套 `sample` 重载（按「能传什么」递增）：

- 默认 shots：[runtime/cudaq/algorithms/sample.h:278-294](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L278-L294) —— `sample(kernel, args...)`，shots 写死 `DEFAULT_NUM_SHOTS`。
- 显式 shots：[runtime/cudaq/algorithms/sample.h:310-325](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L310-L325) —— `sample(shots, kernel, args...)`，注意 shots 是**第一个**位置参数。
- options 包：[runtime/cudaq/algorithms/sample.h:340-366](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L340-L366) —— `sample(options, kernel, args...)`，能带噪声与 explicit_measurements；它在采样前后成对调用 `platform.set_noise` / `reset_noise`。

shot 累加循环本体：[runtime/cudaq/algorithms/sample.h:130-183](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L130-L183)。关键几处：

- 第 136–143 行：远程 QPU 走 `runSamplingAsync(...).get()`，把异步包成同步。
- 第 152 行 `isQuantumDevice = platform.is_emulated(qpu_id)`、第 163 行 `if (isQuantumDevice) return result;`：本地模拟器（`is_emulated` 为真）一次 `launch` 就在模拟器内部跑完全部 shots，所以首轮后提前返回，省掉一次拷贝。
- 第 157 行 `while (counts.get_total_shots() < shots)`：未攒够就再 launch。
- 第 169 行 `counts += result`：用 `sample_result::operator+=` 把这批计数并进总量。
- 第 171–180 行：若一次 launch 后仍是 0 shots（内核无测量），打印警告并 `break`，防止无限循环；若开了 `explicit_measurements` 则直接抛错。

`samplePreamble` 的填充动作：[runtime/cudaq/algorithms/sample.h:60-119](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L60-L119) —— 第 66–72 行先拦截「带测量条件分支的内核」（这类内核已不让用 `sample`，要改 `run`，见 u2-l5）；第 78–91 行把内核名、batch、explicit、噪声分别写到 `ctx` 与 `policy` 上（噪声在两边各放一份，是历史遗留 TODO）；第 93–118 行是库模式下的 tracer 探测，用来发现中途测量条件分支。

Python 端签名对照：[python/cudaq/runtime/sample.py:110-194](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/sample.py#L110-L194)。第 111–115 行 `sample(kernel, *args, shots_count=1000, noise_model=None, explicit_measurements=False)`，参数语义与 C++ 一一对应（注意 Python 用关键字 `shots_count`，C++ 用位置 `shots`）。第 148–150 行先做同样的条件分支与 explicit 校验；第 171–191 行是 Python 版的 shot 循环，逻辑与 C++ `runSampling` 同构：第 176–181 行对硬件/首轮拿满的情况提前返回，第 182 行 `counts += result` 累加，第 183–191 行处理 0 shots。

#### 4.1.4 代码实践

**目标**：直观感受 `shots_count` 对采样分布的影响，并验证 shot 累加循环。

**操作步骤**（Python，默认 qpp 目标即可）：

```python
# 示例代码：观察 shots 对采样估计精度的影响
import cudaq

@cudaq.kernel
def bell():
    q = cudaq.qvector(2)
    h(q[0])
    cx(q[0], q[1])
    mz(q)

# 不同 shots 下估计 |00> 的概率（理论值 0.5）
for shots in (10, 100, 1000, 10000):
    res = cudaq.sample(bell, shots_count=shots)
    p00 = res.count("00") / shots         # = c / shots
    total = sum(res.values())             # Python 端累计计数应等于 shots
    print(f"shots={shots:>6}  P(00)={p00:.4f}  total={total}")
```

注：`SampleResult` 读取 `count("00")` 的接口在 u2-l3 已讲；Python 端没有 `get_total_shots`，用 `sum(res.values())` 等价累计计数。该代码片段为「示例代码」，非仓库原文件，请自行保存为 `.py` 用 `python3` 运行。

**需要观察的现象**：随 `shots` 增大，`P(00)` 越来越贴近 0.5，波动（方差）随之减小；`total_shots` 始终等于传入的 `shots_count`。

**预期结果**：低 shots（如 10）时 `P(00)` 可能明显偏离 0.5（如 0.3 或 0.7）；到 10000 时基本稳定在 0.49–0.51。若把内核里的 `mz(q)` 去掉再运行，应触发 0-shot 警告并提前退出（对应源码第 188–191 行）。

#### 4.1.5 小练习与答案

**练习 1**：C++ 里 `sample(shots, kernel, ...)` 和 `sample(kernel, shots, ...)` 哪个能编译？为什么？
**答案**：前者能编译。shots 是 `std::size_t` 位置参数，必须排在 kernel 前面（见 sample.h:312 重载签名）。后者会被当成 `sample(QuantumKernel&&, Args&&...)` 重载，把 `shots` 当成内核的第一个实参，触发 `SampleCallValid` 校验失败（参数类型不匹配）。

**练习 2**：把一个返回 `int` 的内核交给 `cudaq::sample` 会怎样？错误在什么阶段被挡住？
**答案**：编译期就被挡住。`SampleCallValid`（sample.h:47–49）要求 `HasVoidReturnType`，返回非 void 的内核不满足 concept，`sample` 模板替换失败，直接编译报错。Python 端对应 sample.py:148 的 `_detail_check_conditionals_on_measure` 运行时报「only supports kernels that return None」。

**练习 3**：为什么本地模拟器只 `launch` 一次就能拿全 1000 shots，而代码里还要写 while 循环？
**答案**：本地模拟器在单次执行里内部就把 1000 个 shot 全模拟完并一次性返回（`is_emulated` 为真，sample.h:163 提前 return）。while 循环是为「分批返回」的目标（例如某些远程/硬件场景一次提交只拿回部分 shots）保留的通用结构；对本地模拟器而言，循环体首轮即满足 `get_total_shots() >= shots` 并退出。

---

### 4.2 sample_async 异步模型

#### 4.2.1 概念说明

`sample_async` 与 `sample` 的**采样语义完全一样**（同样的 shot 循环、同样的 `sample_options`），区别只在「**何时拿结果**」：`sample` 同步返回 `sample_result`，`sample_async` 立刻返回一个**期票** `async_sample_result`，结果延后到 `.get()` 时兑现。

这个期票的本质是 `async_result<sample_result>`，它内部包了一个 `detail::future`。`future` 有两种形态：

- **本地形态**：包一个 `std::future<sample_result>`。采样任务被塞进某 QPU 的工作线程队列，跑完后通过 `std::promise` 把结果送回，`.get()` 阻塞等待。
- **远程形态**：包一组「服务器作业句柄」（job id、QPU 名、server 配置）。任务提交给远程 REST QPU，`.get()` 时凭句柄去服务器把结果拉回来——这种 future 甚至能序列化到文件、之后再用。

另外，`sample_async` 多了一个关键参数 **`qpu_id`**：因为异步意味着「我要把任务派到哪条队列」，必须显式指定目标 QPU（默认 0）。`sample` 不需要，因为它在调用线程同步执行，用当前线程绑定的 QPU 即可。

#### 4.2.2 核心流程

`cudaq::sample_async(qpu_id, kernel, args...)` 的内部流程（C++ 端）：

1. 同样过 `SampleCallValid` 编译期校验，取平台与内核名。
2. 调 `detail::runSamplingAsync(..., qpu_id, noise)`。
3. `runSamplingAsync` 内分两条路径：
   - **远程 QPU**（`platform.is_remote(qpu_id)`）：用 `async_sample_policy`（异步策略包装）走 `detail::launch`，直接返回一个绑了服务器句柄的 `async_sample_result`。
   - **本地 QPU**：把「跑一遍完整 `runSampling`」包成一个 `KernelExecutionTask`（一个返回 `sample_result` 的 `std::function`），调 `platform.enqueueAsyncTask(qpu_id, task)` 把它塞进第 `qpu_id` 号 QPU 的队列，拿到 `std::future<sample_result>`，包成 `async_sample_result` 返回。
4. 调用方拿到期票后继续干别的事；之后 `future.get()` 兑现结果。

任务在队列里执行的时序（本地路径）：

```
调用线程:  enqueue(task0)  enqueue(task1) ...  做别的事  f0.get()  f1.get()
                |               |                           ↑阻塞等    ↑已就绪
QPU#qpu_id 队列: [task0] → [task1] → ...   （工作线程串行消费）
```

同一个 QPU 的队列是**串行**消费的，所以同一个 `qpu_id` 上多个 `sample_async` 是排队执行，而非真并行。真并行要靠不同 `qpu_id`（见 4.3）。

#### 4.2.3 源码精读

异步包装与结果类型：[runtime/common/Future.h:204-212](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/Future.h#L204-L212) —— 第 205 行 `async_sample_result = async_result<sample_result>`；第 208–212 行 `async_policy_wrapper<InnerPolicy>` 把内层策略的结果类型从 `sample_result` 提升为 `async_result<sample_result>`，这就是 `async_sample_policy` 的来源。

`detail::future` 的双形态：[runtime/common/Future.h:36-101](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/Future.h#L36-L101) —— 第 55 行 `std::future<sample_result> inFuture`（本地形态）；第 43 行 `std::vector<Job> jobs` + 第 48 行 `qpuName` + 第 52 行 `serverConfig`（远程形态的作业句柄）；第 71–77 行构造函数接受 `std::future` 并置 `wrapsFutureSampling = true`。`.get()` 在两种形态下分别走「future 取值」或「凭句柄查服务器」。

`async_result::get`：[runtime/common/Future.h:143-182](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/Future.h#L143-L182) —— 第 144 行 `result.get()` 兑现内层 future；对 `sample_result` 特化（第 146–147 行）直接返回数据。

`runSamplingAsync` 本体（本节核心）：[runtime/cudaq/algorithms/sample.h:199-263](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L199-L263)。

- 第 204–209 行：校验 `qpu_id < platform.num_qpus()`，越界抛 `invalid_argument`。
- 第 211–230 行（仅 `#ifndef CUDAQ_LIBRARY_MODE`）：远程路径，远程 + 噪声直接报错（远程不支持噪声），构造 `async_sample_policy` 经 `detail::launch` 返回。
- 第 232–258 行：本地路径，把 `runSampling` 包成 `KernelExecutionTask`（第 232 行），闭包按值捕获噪声（`noise = std::move(noise)`，第 234 行注释强调「拷进异步任务以保证生命周期」），任务体内 `set_noise` → `runSampling` → `reset_noise`（即使异常也复位，第 246–251 行 try/catch）。
- 第 260–262 行：`platform.enqueueAsyncTask(qpu_id, task)` 入队拿 `std::future`，包成 `async_sample_result` 返回。

入队与 per-QPU 队列：[runtime/cudaq/platform/quantum_platform.cpp:153-170](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.cpp#L153-L170) —— 第 153–165 行的 `KernelExecutionTask` 重载：建 `promise<sample_result>`、取 `future`，把任务包成「跑 `t()` 并 `promise.set_value(counts)`」塞进 `platformQPUs[qpu_id]->enqueue(wrapped)`（第 163 行，**按 qpu_id 选队列**）；第 167–170 行是返回 `void` 的无返回值重载（广播器用到）。

`sample_async` 重载：[runtime/cudaq/algorithms/sample.h:382-496](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L382-L496)。第 384 行 `sample_async(qpu_id, kernel, ...)`；第 420 行带 shots；第 456 行带 options（含噪声，第 470 行注释说明噪声按值传入 `runSamplingAsync` 以避免悬垂指针）；第 493 行无 qpu_id 重载，内部默认 `qpu_id=0`（第 494 行）。

Python 端 `sample_async`：[python/cudaq/runtime/sample.py:197-267](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/sample.py#L197-L267)。第 247–251 行校验 `qpu_id < target.num_qpus()`；第 257 行 `prepare_call` 编译内核、第 263 行调 C++ `sample_async_impl`；第 267 行返回 Python 包装 `AsyncSampleResult`，它额外持有 MLIR 模块以防被提前 GC（见下）。

Python 包装与模块保活：[python/cudaq/runtime/sample.py:23-59](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/sample.py#L23-L59) —— `AsyncSampleResult` 第 35–40 行把传入的 `mod` 存进全局缓存 `cudaq_async_sample_module_cache`，第 47–55 行 `__del__` 在 `.get()` 已被调用后才清理。第 16–20 行注释解释：必须保活 `ModuleOp`，否则解释器可能在异步任务真正启动前就把模块垃圾回收掉，导致崩溃。

绑定层 `sample_async_impl`：[python/runtime/cudaq/algorithms/py_sample_async.cpp:24-67](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/runtime/cudaq/algorithms/py_sample_async.cpp#L24-L67) —— 第 45–49 行把 MLIR 模块克隆一份（`mod.clone()`，共享指针带 erase 删除器），保证异步任务持有独立副本；第 43 行释放 GIL；第 55–66 行调用同一套 C++ `runSamplingAsync`，闭包里调 `clean_launch_module` 执行内核（注释强调闭包内不得访问 Python 数据）。

#### 4.2.4 代码实践

**目标**：用 `sample_async` 同时提交多个内核，观察「先提交、后回收」的执行模式。

**操作步骤**（Python，默认 qpp 单 QPU 目标）：

```python
# 示例代码：连续提交多个 sample_async，再统一回收
import cudaq, time

@cudaq.kernel
def ghz(n: int):
    q = cudaq.qvector(n)
    h(q[0])
    for i in range(n - 1):
        cx(q[i], q[i + 1])
    mz(q)

# 1) 异步：先全部提交，再统一 get
t0 = time.perf_counter()
futures = [cudaq.sample_async(ghz, 6, shots_count=2000) for _ in range(4)]
results_async = [f.get() for f in futures]          # 在这里才阻塞
t_async = time.perf_counter() - t0

# 2) 同步：顺序 sample 四次
t0 = time.perf_counter()
results_sync = [cudaq.sample(ghz, 6, shots_count=2000) for _ in range(4)]
t_sync = time.perf_counter() - t0

print(f"async submit-then-get : {t_async:.3f}s")
print(f"sync sequential       : {t_sync:.3f}s")
for r in results_async:
    print(r.most_probable(), "total =", sum(r.values()))
```

> 注：Python 端 `SampleResult` 没有 `get_total_shots` 方法，用 `sum(r.values())` 等价累计计数。该片段为「示例代码」，非仓库原文件。

**需要观察的现象**：四个 `sample_async` 在 `f.get()` 之前都已入队；由于默认 qpp 只有一个 QPU，它们在单条队列上**串行**执行，所以 `t_async` 与 `t_sync` 通常**接近**（单 QPU 上没有真并行，差距主要来自宿主侧提交/回收的重叠）。

**预期结果（单 QPU qpp）**：`t_async` 与 `t_sync` 处在同一量级，可能略快或略慢；`results_async` 每个结果的 `most_probable()` 应为全 0 串 `000000` 与全 1 串 `111111`（GHZ 特征），二者计数之和接近 `shots_count`。**若要看到真正的并行加速，请切到多 QPU 目标**（见 4.3 与综合实践）。

**待本地验证**：具体耗时数值依赖机器与目标，无法预判绝对值，请以本地实测为准。

#### 4.2.5 小练习与答案

**练习 1**：`sample_async` 为什么必须传 `qpu_id`，而 `sample` 不用？
**答案**：`sample_async` 把任务入队到某条 QPU 队列，必须指明入哪条（`qpu_id`，默认 0）；任务在该队列上异步执行，与调用线程解耦。`sample` 在调用线程同步执行，用的是该线程当前绑定的 QPU，无需显式指定。

**练习 2**：本地路径下，`async_sample_result::get()` 内部最终等的是什么？
**答案**：等一个 `std::future<sample_result>`（`detail::future::inFuture`，Future.h:55）。该 future 由 `quantum_platform::enqueueAsyncTask` 用 `promise<sample_result>` 制造（quantum_platform.cpp:155–156），任务在 QPU 工作线程里跑完 `runSampling` 后 `promise.set_value(counts)`（第 160 行），`.get()` 即取到这个 `sample_result`。

**练习 3**：在远程 QPU 上调用 `sample_async` 时传了 `noise_model`，会发生什么？
**答案**：报错。`runSamplingAsync` 的远程分支在 `hasNoise` 时抛「Noise model is not supported on remote platforms」（sample.h:217–219）；Python 端 sample.py:253–255 同样拦截。远程硬件不支持用户自定义噪声模型。

---

### 4.3 并发采样

#### 4.3.1 概念说明

4.2 讲的 `sample_async` 解决「一个内核、异步拿结果」。但很多算法（如 VQE 在一组参数上扫描、或并行评估多个 ansatz）需要**把同一内核在一组不同参数上各跑一次**，并把 N 个结果一起拿回来。这种「参数扫描」如果顺序执行很慢。

CUDA-Q 提供两条并发路线：

1. **C++ 广播 `sample(kernel, ArgumentSet{...})`**：把 N 组参数自动切分到 `numQpus` 个 QPU 上**并行**执行，返回 `vector<sample_result>`。这是「真并发」——前提是平台有多个 QPU（如 mqpu）。
2. **手工 `sample_async`**：你显式对每组参数调 `sample_async(qpu_id=..., kernel, args)`，自己决定派到哪个 QPU、何时回收。

两者底层都依赖「每个 QPU 一条队列」。区别在于：广播器**自动**按 QPU 数切分工作并回收；手工方式把调度权交给你。

注意 Python 端的 `sample(kernel, [list_of_args])` 触发的 `__broadcastSample`（sample.py:62–82）是**顺序**循环、单 QPU 执行——它与 C++ 的 `broadcastFunctionOverArguments` 不是一回事，不要混淆。

#### 4.3.2 核心流程

C++ 广播 `sample(kernel, ArgumentSet)` 的执行流程：

1. 取平台与 `numQpus = platform.num_qpus()`。
2. 构造一个广播仿函数 `functor(qpuId, counter, N, args...) -> sample_result`：内部对单组参数调 `runSampling(..., qpuId, counter, N)`。
3. 调 `broadcastFunctionOverArguments(numQpus, platform, functor, params)`。
4. 广播器把 N 组参数按 `nExecsPerQpu = N/numQpus + (N%numQpus != 0)` 切成 `numQpus` 段，**每个 QPU 一段**。
5. 对每个 `qpuId`：建 `promise<vector<sample_result>>`，把「遍历本段参数、逐组调 functor、收集结果、`promise.set_value`」打包成任务，`platform.enqueueAsyncTask(qpuId, task)` 入队。
6. 因为不同 `qpuId` 入的是**不同队列**，这些段在多个工作线程上**并行**执行。
7. 主线程最后遍历所有 `future.get()` 收集全部结果，按 QPU 顺序拼成一个大 vector 返回。

切分示意（N=8 组参数，numQpus=2，nExecsPerQpu=4）：

```
ArgumentSet 参数序号:  0 1 2 3 | 4 5 6 7
                       QPU#0   |  QPU#1
        （两条队列并行消费各自一段）
```

#### 4.3.3 源码精读

广播 `sample` 重载：[runtime/cudaq/algorithms/sample.h:498-531](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L498-L531) —— 第 510–511 行取 `numQpus`；第 515–526 行构造仿函数，第 519–524 行对单组参数调 `runSampling`（注意把 `qpuId`、`counter`、`N` 传进去，以便 ExecutionContext 记录 batch 信息）；第 529–530 行交给 `broadcastFunctionOverArguments`。带 shots 的重载在 [sample.h:541-567](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L541-L567)，带 options（含噪声）的重载在 [sample.h:577-611](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L577-L611)（噪声在广播前后成对 set/reset）。

广播器本体：[runtime/cudaq/algorithms/broadcast.h:41-127](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/broadcast.h#L41-L127)。

- 第 49–50 行：`N = std::get<0>(params).size()`，`nExecsPerQpu = N/numQpus + (N%numQpus != 0)`（向上取整切分）。
- 第 53–57 行：校验各参数向量等长。
- 第 60 行：取线程相关随机种子 `seed`，传进各 QPU 任务以保证可复现。
- 第 62–117 行：**每个 QPU 起一个任务**。第 64–65 行建 `promise`/`future`；第 70–71 行算本段 `[lowerBound, upperBound)`；第 79–110 行遍历本段参数、逐组 `std::apply(apply, currentArgs)` 调 functor、收集 `results`；第 113 行 `promise.set_value(results)`。
- 第 116 行：`platform.enqueueAsyncTask(qpuId, functor)` —— **不同 qpuId 入不同队列，并行**。
- 第 120–124 行：主线程 `for (auto &f : futures) f.get()` 收集并拼接。

注意第 116 行用的是 `enqueueAsyncTask` 的 **`std::function<void()>` 重载**（无返回值），因为结果通过闭包里的 `promise` 自己回收，不需要外层 future。

Python 单 QPU 顺序广播（用于对比，避免混淆）：[python/cudaq/runtime/sample.py:62-82](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/sample.py#L62-L82) —— `__broadcastSample` 用 `for i, a in enumerate(argSet)` 逐组调 `launch_sample`，是**顺序**执行、不带 QPU 切分；第 73–75 行设置 `batchIteration`/`totalIterations` 以记录 batch 元信息。它在 sample.py:155–161 被 `__isBroadcast` 分支触发。

Python 多 QPU 真正并行的入口是 **`sample_async(qpu_id=..., kernel, args)`**（4.2 节）配合多 QPU 目标，或通过 MPI 多进程（见 docs/sphinx/examples/python/mpi/sample.py）。在 [docs/sphinx/examples/python/mpi/sample.py:60-74](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/mpi/sample.py#L60-L74) 中，每个 MPI rank 算出自己的 `qpu_id`，各自 `cudaq.set_target("tensornet", comm_handle=...)` 后独立 `cudaq.sample(ghz, num_qubits)`，构成跨进程/跨 QPU 的并行。

#### 4.3.4 代码实践

**目标**：在多 QPU（mqpu）目标上，对比「顺序 sample_async 提交到同一 QPU」与「分发到不同 QPU」的耗时差异，体会 per-QPU 队列的并发效果。

**操作步骤**（Python）：

```python
# 示例代码：mqpu 上把多个采样任务分发到不同 QPU 并行
import cudaq, time

@cudaq.kernel
def ghz(n: int):
    q = cudaq.qvector(n)
    h(q[0])
    for i in range(n - 1):
        cx(q[i], q[i + 1])
    mz(q)

cudaq.set_target("mqpu")          # 暴露多个模拟器 QPU
nqpus = cudaq.num_qpus()          # 等价于 get_target().num_qpus()
print("num_qpus =", nqpus)

tasks = 4                          # 想并行的任务数

# 方式 A：全派到 QPU0（单队列串行）
t0 = time.perf_counter()
fa = [cudaq.sample_async(ghz, 8, shots_count=2000, qpu_id=0) for _ in range(tasks)]
ra = [f.get() for f in fa]
tA = time.perf_counter() - t0

# 方式 B：轮询派到不同 QPU（多队列并行）
t0 = time.perf_counter()
fb = [cudaq.sample_async(ghz, 8, shots_count=2000, qpu_id=i % nqpus) for i in range(tasks)]
rb = [f.get() for f in fb]
tB = time.perf_counter() - t0

print(f"all on QPU0      : {tA:.3f}s")
print(f"round-robin QPUs : {tB:.3f}s")
```

> 该片段为「示例代码」。`mqpu` 目标需要本机构建支持多 QPU（通常依赖多 GPU 或多线程模拟器）；若 `cudaq.num_qpus()` 返回 1，则方式 A、B 等价，看不到并行收益。请以本地能否拿到 >1 个 QPU 为准。

**需要观察的现象**：当 `nqpus > 1` 且 `tasks` 能铺到多个 QPU 时，方式 B（round-robin）应明显快于方式 A（全挤 QPU0）；所有结果的 `most_probable()` 都应是 GHZ 的两个特征串。

**预期结果（多 QPU 可用时）**：`tB < tA`，加速比随可用 QPU 数趋近 `min(tasks, nqpus)`。`tB` 与单次 `sample_async` 的耗时接近（理想情况下 4 个任务在 4 条队列上同时跑完）。

**待本地验证**：是否具备 `mqpu` 目标、QPU 数量、绝对耗时均依赖本地环境，无法预判。

#### 4.3.5 小练习与答案

**练习 1**：C++ `broadcastFunctionOverArguments` 里，第 116 行 `enqueueAsyncTask(qpuId, functor)` 用的是「无返回值」重载而非 `KernelExecutionTask` 重载，为什么？
**答案**：因为结果通过闭包内部自己建的 `promise<vector<sample_result>>` 回收（broadcast.h:64–65、113），外层不需要再返回一个 future。用 `void()` 重载纯粹是为了「把任务塞进对应 QPU 队列」，结果通道由闭包负责。

**练习 2**：8 组参数、2 个 QPU 时，每个 QPU 分到几组？依据是哪一行？
**答案**：每个 QPU 分到 4 组。`nExecsPerQpu = N/numQpus + (N%numQpus != 0) = 8/2 + 0 = 4`（broadcast.h:50）。QPU0 跑参数 0–3，QPU1 跑参数 4–7（lowerBound/upperBound 见第 70–71 行）。

**练习 3**：为什么说 Python 的 `cudaq.sample(kernel, [arg0, arg1, ...])`（参数是列表）不是「多 QPU 并行」？
**答案**：它触发的是 `__isBroadcast` 分支，走 `__broadcastSample`（sample.py:62–82），那是**单 QPU 上的顺序 for 循环**，逐组调 `launch_sample`，没有 QPU 切分、没有并行。要多 QPU 并行，应改用 `sample_async(..., qpu_id=i)` 配合多 QPU 目标，或用 C++ 的 `sample(kernel, ArgumentSet{...})`。

---

## 5. 综合实践

把本讲三块知识串起来：写一个**参数扫描**小程序，对一组旋转角参数并行采样一个简单 ansatz，对比「顺序 sample」「单 QPU 异步」「多 QPU 轮询异步」三种方式的总耗时与结果一致性。

**任务要求**：

1. 定义一个参数化内核：对 1 个比特做 `ry(theta)` 后测量，返回采样分布。
2. 准备 8 个不同的 `theta`（如 `np.linspace(0, pi, 8)`）。
3. 实现三种执行方式：
   - **顺序同步**：`for theta: cudaq.sample(kernel, theta, shots_count=...)`。
   - **单 QPU 异步**：先 `for theta: sample_async(kernel, theta, qpu_id=0)` 全部入队，再统一 `get()`。
   - **多 QPU 轮询异步**：`sample_async(kernel, theta, qpu_id=i % num_qpus)`（若 `mqpu` 可用）。
4. 各方式都从结果里提取测量到 `1` 的概率 \( \hat{P}(1|\theta) \)，验证三种方式给出**一致**的概率曲线（在统计涨落范围内），应接近理论值 \( \sin^2(\theta) \)。
5. 打印三种方式的总耗时，分析在单 QPU 与多 QPU 环境下的差异。

**验证公式**：对 `ry(θ)|0⟩`，测量到 `1` 的概率

\[
P(1|\theta) = \sin^2(\theta/2) \cdot (\text{按 CUDA-Q 的 ry 约定，请以实测为准})
\]

> 不同库对 `ry(θ)` 的角度约定（半角 vs 全角）不同，请以你实测的曲线形状为准来判断公式形式，不要假定。

**检查点**：

- 三种方式的概率曲线是否在涨落内一致？（验证「异步/并发不改变采样统计」）
- 单 QPU 上，「单 QPU 异步」相比「顺序同步」是否有 overlap 收益？
- 多 QPU 可用时，「多 QPU 轮询」是否带来接近 `min(tasks, num_qpus)` 的加速？

若本地无 `mqpu`/多 GPU 环境，至少完成前两种方式的对比，并在报告中注明「多 QPU 部分待本地验证」。

## 6. 本讲小结

- `sample` 的可调旋钮集中在参数包 `sample_options`（shots/noise/explicit_measurements），运行时分派靠策略包 `sample_policy`（静态名字 `"sample"`、结果类型 `sample_result`）；C++ 有三套重载，Python 用关键字 `shots_count/noise_model/explicit_measurements`。
- 采样的本质是 shot 累加：`while (counts.get_total_shots() < shots)` 反复 `launch` 并 `counts += result`；本地模拟器一次 `launch` 即跑完全部 shots（`is_emulated` 为真时首轮提前返回），while 循环是为分批返回的目标保留的通用结构；内核无测量会触发 0-shot 警告并跳出。
- `sample_async` 与 `sample` 采样语义相同，差别在「立刻返回期票」：返回的 `async_sample_result` 内部是 `detail::future`，本地包 `std::future<sample_result>`、远程包服务器作业句柄；任务经 `platform.enqueueAsyncTask(qpu_id, task)` 入到**第 qpu_id 号 QPU 的队列**。
- 每个 QPU 一条队列：同一 QPU 上的异步任务串行，不同 QPU 上的任务并行——这是「单 QPU 异步没真并行、多 QPU 才有」的根因。
- C++ `sample(kernel, ArgumentSet{...})` 经 `broadcastFunctionOverArguments` 把 N 组参数按 `nExecsPerQpu` 切分到 `numQpus` 个 QPU 并行执行；Python 的列表参数广播 `__broadcastSample` 则是单 QPU 顺序循环，二者不要混淆。
- 异步任务里的噪声模型按值捕获（`noise = std::move(noise)`），并在任务体内外成对 `set_noise/reset_noise`，以防悬垂指针与全局状态污染；远程 QPU 不支持噪声模型。

## 7. 下一步学习建议

- **下一讲 u3-l3「cudaq::observe 与 spin_op 期望值」**：`observe` 复用本讲的 shot 累加与 `ExecutionContext("sample", ...)` 机制，只是把「单次采样分布」升级为「对哈密顿量每个 term 采样再加权求期望」。理解了本讲的 shot 循环，再看 `observe` 会很自然。
- **想深入多 QPU 并行的工程实现**：直接读 u6-l6「多 QPU（mqpu）与分布式执行」，看 `MultiQPUPlatform` 如何暴露多个模拟器 QPU、MPI 插件如何协同，以及 [docs/sphinx/examples/python/mpi/sample.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/mpi/sample.py) 的多 rank 切分。
- **想理解 `detail::launch` 在库模式与 MLIR/QIR 模式如何分叉**：回顾 u3-l1，并结合 [runtime/cudaq/algorithms/launch.h:37-104](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/launch.h#L37-L104) 的 `executeKernelApi` 回调设置。
- **想动手扩展**：u7-l3「实现一个新的模拟器后端」会用到本讲的 `CircuitSimulator` 注册机制；届时你会发现 `sample` 的 shot 循环对所有后端透明，新后端无需关心采样编排。
