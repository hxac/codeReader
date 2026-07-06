# 执行模型：quantum_platform 与 QPU

> 本讲是「执行模型、算法原语与算符」单元的第一讲。前面两个单元你已经学会写内核、施加门、测量和采样（见 u1-l4、u1-l5、u2-l3）。但每次调用 `cudaq::sample(kernel)` 时，那行代码背后到底发生了什么？内核是怎么从一段 C++/Python 函数，变成在一次「量子处理单元」上的实际执行的？本讲就来拆解这条链路。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `quantum_platform`、`QPU`、`ExecutionContext`、`ExecutionManager` 四个抽象各自的角色，以及它们之间的层次关系。
- 解释一次 `cudaq::sample(kernel)` 调用是如何被「创建上下文 → 分发到平台 → 转发到 QPU → 在模拟器/设备上执行 → 终结并回收结果」这一整条链路完成的。
- 理解「执行意图」（采样？观测？追踪？）是如何用 `ExecutionContext::name` 这个字符串来分派的。
- 会用 `CUDAQ_LOG_LEVEL` 环境变量打开运行时日志，从日志里亲眼看到这条分发链路被触发的过程。

本讲**只讲执行模型的骨架**，不深入具体模拟器算法（那是 u6 的事），也不深入 `observe` 的期望值计算细节（那是 u3-l3 的事）。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 「执行」在 CUDA-Q 里意味着什么

在前面的单元里，「执行一个内核」对你来说就是 `cudaq::sample(kernel)`。但从运行时的视角看，一次「执行」需要回答好几个问题：

- 这次执行**要干什么**？是采样得到比特串分布，还是计算某个哈密顿量的期望值，还是只是跟踪内核里用了哪些门？
- 这次执行**在哪干**？本地 CPU 模拟器、GPU 模拟器、还是远程真实 QPU？要跑在哪一个 QPU 上（一台机器可能有多个）？
- 这次执行**带什么参数**？跑多少 shots？要不要叠加噪声？是否异步？

CUDA-Q 把这些「问题的答案」打包成一个对象，叫 **`ExecutionContext`（执行上下文）**。它就像一张「工单」：算法层填好工单，运行时按工单把活派给具体的「工人」。

### 2.2 两层抽象：平台与 QPU

CUDA-Q 用两层抽象来描述「量子硬件」：

- **`quantum_platform`（量子平台）**：对应「一整套量子架构」。可以理解成「这台机器/这套服务」。一个平台持有一组 `QPU`。
- **`QPU`（量子处理单元）**：对应「一个能执行量子内核的处理单元」。它可以是真实的量子硬件，**也可以是一个本地模拟器**（默认情况就是模拟器）。

一个很常见的误解是「QPU = 真实量子硬件」。在 CUDA-Q 里不是。本地 CPU 上的 qpp 模拟器、GPU 上的 custatevec 模拟器，在抽象层面都是一个 `QPU`。一台多 GPU 机器可以暴露多个模拟器 QPU（这就是后面的 mqpu 平台，见 u6-l6）。所以 **`QPU` 是「一个执行内核的单元」的抽象，不区分真假硬件**。

### 2.3 链接期决定后端

还记得 u1-l1 / u1-l3 反复强调的一个结论吗？**源码里不写死具体模拟器，后端是在构建/链接期决定的**。本讲你会看到这个设计在运行时是怎么落地的：平台和 QPU 都是通过「注册宏 + 插件加载」在运行时拿到具体实现的，算法层只面向抽象基类编程。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [runtime/cudaq/platform.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform.h) | 轻量入口，提供 `cudaq::get_platform()` 等便捷函数，拿到当前平台单例。 |
| [runtime/cudaq/platform/quantum_platform.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.h) | `quantum_platform` 抽象基类与 `CUDAQ_REGISTER_PLATFORM` 注册宏。 |
| [runtime/cudaq/platform/quantum_platform.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.cpp) | 平台实现：把上下文转发给具体 QPU、插件加载、内核启动 thunk。 |
| [runtime/cudaq/platform/qpu.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/qpu.h) | `QPU` 抽象基类：执行队列、`launchKernel`、上下文钩子。 |
| [runtime/common/ExecutionContext.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.h) | `ExecutionContext` 类定义与线程级 context 工具函数。 |
| [runtime/common/ExecutionContext.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.cpp) | `thread_local` 的当前上下文存储与读写。 |
| [runtime/cudaq/qis/execution_manager.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.h) | `ExecutionManager` 基类与 `with_default_em` 执行骨架。 |
| [runtime/cudaq/qis/execution_manager.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.cpp) | 上下文的配置/终结实现（转发到 nvqir 模拟器，按 `name` 分派）。 |
| [runtime/cudaq/algorithms/sample.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h) | `cudaq::sample` 顶层入口与 `runSampling` shot 循环。 |
| [runtime/cudaq/algorithms/launch.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/launch.h) | `detail::launch`：把上下文交给 QPU / ExecutionManager 执行的分叉点。 |
| [runtime/logger/logger.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/logger/logger.cpp) | `CUDAQ_LOG_LEVEL` 日志级别的解析。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **quantum_platform 与 QPU 抽象**——硬件那一层长什么样。
2. **ExecutionContext：执行意图的载体**——「工单」上有哪些字段。
3. **一次内核调用的完整分发链路**——把前两者串成一条从 `cudaq::sample` 到模拟器的完整路径。

### 4.1 quantum_platform 与 QPU 抽象

#### 4.1.1 概念说明

`quantum_platform` 是 CUDA-Q 对「一整套量子架构」的抽象。它的核心职责有两块：

- **查询能力**：这台机器有几个 QPU、每个 QPU 几个比特、比特怎么连接、是不是模拟器、是不是远程。
- **转发执行**：把算法层送来的 `ExecutionContext`（执行工单）转发给具体的某个 QPU。

`QPU` 则是「一个执行单元」。它持有：

- 一个逻辑编号 `qpu_id`（在一组 QPU 里的下标）。
- 比特数 `numQubits` 和连接拓扑 `connectivity`。
- 一个**异步执行队列** `execution_queue`——这是 `sample_async` 能并发的物理基础。
- 一个可选的噪声模型 `noiseModel`。

关键关系：**平台拥有 QPU 列表**。`quantum_platform` 的保护成员 `platformQPUs` 是一个 `vector<unique_ptr<QPU>>`，由具体子类（如 `DefaultQuantumPlatform`、`MultiQPUPlatform`）在构造时填好。平台的方法大多只是「按 `qpu_id` 取出对应 QPU，再把调用委托下去」。

#### 4.1.2 核心流程

平台的「转发」流程可以写成下面这段伪代码：

```
configureExecutionContext(ctx):
    qid = ctx.qpuId                 // 工单上写着要在哪个 QPU 跑
    platformQPUs[qid].configureExecutionContext(ctx)   // 委托给该 QPU

beginExecution():
    qid = 当前线程的 QPU id
    platformQPUs[qid].beginExecution()

finalizeExecutionContext(ctx):
    qid = ctx.qpuId
    platformQPUs[qid].finalizeExecutionContext(ctx)    // 让 QPU 把结果写回 ctx
```

平台本身几乎不做「真正的量子计算」，它是一个**调度层/门面（facade）**。真正干活的是 QPU，而 QPU 又会把指令交给底层模拟器（见 4.3）。

至于「当前到底是哪个平台」，是运行时通过插件机制拿到的——这就是「链接期决定后端」在运行时的落点。

#### 4.1.3 源码精读

**平台类的核心结构**——注意 `platformQPUs` 这个容器，它是平台与 QPU 之间的唯一纽带：

[runtime/cudaq/platform/quantum_platform.h:62-L75](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.h#L62-L75) 定义了 `quantum_platform` 类，注释明确写道「This type is meant to be subclassed for concrete realizations of quantum platforms, which are intended to populate this platformQPUs member」——即子类负责往 `platformQPUs` 里塞 QPU。

[runtime/cudaq/platform/quantum_platform.h:133-L138](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.h#L133-L138) 是最常被查询的两个方法：`num_qpus()` 直接返回 `platformQPUs.size()`，`getQPU(qpu_id)` 校验 id 后返回对应 QPU 引用。

[runtime/cudaq/platform/quantum_platform.h:255-L255](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.h#L255) 就是那个关键的 `std::vector<std::unique_ptr<QPU>> platformQPUs;`。

**转发到 QPU 的实现**——`configure/finalize` 都只是按 `ctx.qpuId` 取 QPU 再委托：

[runtime/cudaq/platform/quantum_platform.cpp:206-L211](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.cpp#L206-L211) 中 `configureExecutionContext` 先校验 `qid`，再调用 `platformQPUs[qid]->configureExecutionContext(ctx)`。

[runtime/cudaq/platform/quantum_platform.cpp:228-L232](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.cpp#L228-L232) 中 `finalizeExecutionContext` 同样委托给 `platformQPUs[qid]`。

**插件加载——「链接期决定后端」的运行时落点**：

[runtime/cudaq/platform/quantum_platform.cpp:56-L71](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.cpp#L56-L71) `getQuantumPlatformInternal()`：先看是否有人显式设置过（`setQuantumPlatformInternal`），否则调用 `getUniquePluginInstance<quantum_platform>("getQuantumPlatform")` 从链接进来的平台库里加载符号。换一个链接库，就换一个平台——这正是「后端在链接期切换」的机制。

**注册宏**——平台子类用这个宏把自己暴露成工厂：

[runtime/cudaq/platform/quantum_platform.h:317-L329](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.h#L317-L329) `CUDAQ_REGISTER_PLATFORM(NAME, PRINTED_NAME)` 宏展开后会定义一个 `getQuantumPlatform()` 函数，内部是 `thread_local` 单例。

**QPU 抽象**——注意默认值透露的信息：

[runtime/cudaq/platform/qpu.h:46-L90](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/qpu.h#L46-L90) 定义 `QPU` 类。注意两个默认值：`numQubits = 30`（默认 30 比特），以及第 114 行 `virtual bool isSimulator() { return true; }`——**默认就是一个模拟器**，这印证了「QPU 不一定是真实硬件」。

[runtime/cudaq/platform/qpu.h:134-L143](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/qpu.h#L134-L143) 给出了 QPU 的上下文钩子：`enqueue`（入队异步任务）、`configureExecutionContext`/`finalizeExecutionContext`/`beginExecution`/`endExecution`，以及一组 `launchKernel` 重载。这些都是子类要去实现的「干活」接口。

#### 4.1.4 代码实践

**实践目标**：用运行时 API 查询「当前平台是谁」，建立对平台/QPU 抽象的直觉。

**操作步骤**（基于 [runtime/cudaq/platform.h:21-L43](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform.h#L21-L43) 提供的便捷函数）：

写一个最小的 `__qpu__` 内核（含一次测量），然后在 `main` 里查询平台信息：

```cpp
// 示例代码：probe_platform.cpp
#include <cudaq.h>
#include <iostream>

struct bell {
  __qpu__ void operator()() {
    cudaq::qvector q(2);
    h(q[0]);
    x<cudaq::ctrl>(q[0], q[1]);
    mz(q);
  }
};

int main() {
  auto &platform = cudaq::get_platform();
  std::cout << "platform name   : " << platform.name() << '\n';
  std::cout << "num qpus        : " << platform.num_qpus() << '\n';
  std::cout << "is simulator    : " << platform.is_simulator() << '\n';
  std::cout << "is remote       : " << platform.is_remote() << '\n';
  std::cout << "num qubits(qpu0): " << platform.get_num_qubits(0) << '\n';
  auto counts = cudaq::sample(bell{});
  counts.dump();
  return 0;
}
```

编译运行（默认落到 CPU 的 qpp 模拟器）：

```bash
nvq++ probe_platform.cpp -o probe_platform.x
./probe_platform.x
```

**需要观察的现象**：

- `platform name` 应反映默认目标（如 `nvidia` 或 `qpp-cpu`，**具体名称待本地验证**，取决于默认 target 配置）。
- `num qpus` 在单模拟器目标下应为 `1`。
- `is simulator` 应为 `1`（true），`is remote` 应为 `0`（false）。
- `num qubits(qpu0)` 是该 QPU 声明的最大比特数（注意它不一定等于你内核实际用到的比特数）。

**预期结果**：你看到一张「这台机器」的简明名片，证明 `get_platform()` 返回的就是链接期绑定的那个平台实例。如果无法本地构建，此为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `quantum_platform::num_qpus()` 不需要任何参数就能返回 QPU 数量？
**答案**：因为平台在构造时就把所有 QPU 塞进了成员 `platformQPUs`（一个 `vector`），`num_qpus()` 只是返回 `platformQPUs.size()`，见 [quantum_platform.h:133](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.h#L133)。

**练习 2**：如果把一个不存在的 `qpu_id` 传给 `getQPU`，会发生什么？
**答案**：`validateQpuId` 会抛 `std::invalid_argument`，提示 "Invalid QPU ID"，见 [quantum_platform.cpp:172-L180](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.cpp#L172-L180)。

**练习 3**：`QPU::isSimulator()` 默认返回什么？这传递了什么设计意图？
**答案**：默认返回 `true`（[qpu.h:114](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/qpu.h#L114)）。意图是：在 CUDA-Q 的抽象里，QPU 默认就是模拟器，真实硬件反而是需要子类去「改写」的特殊情况。

---

### 4.2 ExecutionContext：执行意图的载体

#### 4.2.1 概念说明

如果说平台和 QPU 描述了「在哪干」，那么 `ExecutionContext` 描述的就是「这次到底要干什么、怎么干」。它是一个普通的 C++ 类（不是模板），算法层在每次执行前构造一个实例，把执行意图写进去，运行时再读取这些字段来调整后端行为。

它最重要的字段是 **`name`**——一个字符串，用来标识这次执行的「种类」。常见的取值有：

| `name` | 含义 | 由谁创建 |
| --- | --- | --- |
| `"sample"` | 采样，得到比特串分布 | `cudaq::sample` |
| `"observe"` | 观测，计算哈密顿量期望值 | `cudaq::observe`（见 u3-l3） |
| `"tracer"` | 追踪，只记录内核用了哪些资源，不真正执行 | 库模式下的预追踪 |
| `"basic"` | 通用执行（如 `get_state`） | 其它算法 |
| `"dem"` | 生成探测器错误模型 | stim 相关流程 |

`name` 之所以重要，是因为**终结阶段会按 `name` 分派**——同一个 `finalize` 入口，根据 `name` 决定是回收一个 `sample_result` 还是回收一个 `observe_result`。

#### 4.2.2 核心流程

`ExecutionContext` 的生命周期与一次执行严格绑定：

```
1. 构造  : ExecutionContext ctx("sample", shots, qpu_id);
2. 填充  : ctx.kernelName = ...; ctx.noiseModel = ...; ctx.shots = ...;
3. 设置  : setExecutionContext(&ctx);     // 放到 thread_local 槽位
4. 执行  : 内核运行；门/测量经 ExecutionManager 落到模拟器，结果写进 ctx.result
5. 终结  : finalizeExecutionContext(ctx); // 按 ctx.name 分派，整理结果
6. 清理  : resetExecutionContext();       // 清空 thread_local 槽位
```

这里有一个关键设计：**当前上下文是 thread_local（线程级）的**。也就是说，每个线程同时只有一个「当前 ExecutionContext」。门操作函数（`h`、`x`、`mz` 等）在内核里被调用时，并不需要显式传 context——它们通过 `getExecutionContext()` 隐式拿到当前线程的 context。这也是 `sample_async` 能够多线程并发的根本前提（不同线程各有各的 context）。

#### 4.2.3 源码精读

**ExecutionContext 的关键字段**——逐组理解：

[runtime/common/ExecutionContext.h:56-L76](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.h#L56-L76) 是类定义与构造函数。注意构造函数把 `name`、`shots`、`qpuId` 作为入参；`name` 是 `const`，构造后就不可变——执行种类在一次执行中是固定的。

[runtime/common/ExecutionContext.h:78-L95](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.h#L78-L95) 一组核心字段：`shots`（采样次数）、`spin`（observe 时的哈密顿量，`optional`）、`result`（采样计数结果）、`expectationValue`（期望值，`optional`）、`hasConditionalsOnMeasureResults`（内核是否含测量条件分支，承接 u2-l5）、`noiseModel`（噪声模型指针）。

[runtime/common/ExecutionContext.h:120-L139](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.h#L120-L139) 另一组关键字段：`kernelName`、`batchIteration`/`totalIterations`（批执行用，承接 `sample_n`/`observe_n`）、`qpuId`（要在哪个 QPU 跑——这是平台转发的依据）。注意 `qpuId` 默认 0，单 QPU 目标下永远是 0。

**thread_local 存储**——「当前上下文」的物理实现：

[runtime/common/ExecutionContext.cpp:14-L21](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.cpp#L14-L21) 定义了 `thread_local ExecutionContext *currentExecutionContext = nullptr;`，并提供了 `getExecutionContext()` 读取它。这就是「隐式传递 context」的实现基础。

[runtime/common/ExecutionContext.cpp:43-L47](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.cpp#L43-L47) `setExecutionContext`/`resetExecutionContext` 写入和清空这个指针。

**几个便捷查询**——它们都依赖 thread_local 指针：

[runtime/common/ExecutionContext.cpp:23-L41](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.cpp#L23-L41) 提供 `isInTracerMode()`（看 `name=="tracer"`）、`isInBatchMode()`（看 `totalIterations!=0`）、`getCurrentQpuId()`（读 `ctx.qpuId`）。模拟器代码就是用这些函数来在线调整行为的。

#### 4.2.4 代码实践

**实践目标**：用「源码阅读」的方式，把一次 `cudaq::sample` 调用与 `ExecutionContext` 的字段填充对应起来。这是典型的源码阅读型实践，无需运行。

**操作步骤**：

1. 打开 [runtime/cudaq/algorithms/sample.h:146-L150](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L146-L150)，看 `runSampling` 如何构造上下文：`ExecutionContext ctx("sample", shots, qpu_id);`——这里就设定了 `name="sample"`、`shots`、`qpuId`。
2. 打开 [runtime/cudaq/algorithms/sample.h:78-L91](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L78-L91)，看 `samplePreamble` 如何继续填充：`ctx.kernelName`、`ctx.batchIteration`、`ctx.totalIterations`、`ctx.explicitMeasurements`、`ctx.noiseModel`。

**需要观察的现象**：你会看到，算法层在执行前就把几乎所有 `ExecutionContext` 字段都填好了，之后才把 `ctx` 交给平台。

**预期结果**：你能画出一张表，左列是 `ExecutionContext` 的字段名，右列是「谁、在哪一行给它赋值」。例如 `name` ← 构造函数（sample.h:146）；`noiseModel` ← `samplePreamble`（sample.h:91）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ExecutionContext::name` 被声明为 `const std::string`？
**答案**：因为执行种类（sample/observe/...）在一次执行中不应改变，且终结阶段要靠它做分派依据；`const` 在类型层面保证了这一点。见 [ExecutionContext.h:73](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.h#L73)。

**练习 2**：内核里的 `mz(q)` 调用并没有接收 `ExecutionContext` 参数，模拟器是怎么知道当前上下文的？
**答案**：通过 thread_local 的 `currentExecutionContext`。门/测量函数内部调用 `getExecutionContext()` 隐式获取，见 [ExecutionContext.cpp:16-L21](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.cpp#L16-L21)。

**练习 3**：`ExecutionContext::qpuId` 在单 QPU 平台下通常是多少？它的作用是什么？
**答案**：默认且始终为 0。它的作用是告诉平台「这次执行要转发给 `platformQPUs[qpuId]` 这个 QPU」，见 [quantum_platform.cpp:207-L210](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.cpp#L207-L210)。

---

### 4.3 一次内核调用的完整分发链路

#### 4.3.1 概念说明

前两节我们分别认识了「硬件抽象」（平台/QPU）和「执行工单」（ExecutionContext）。本节把它们串起来，回答本讲最核心的问题：

> 一行 `cudaq::sample(kernel)` 是怎么最终变成模拟器上的一次执行的？

整条链路有两条分支，取决于**执行模式**：

- **库模式（library mode）**：内核作为一个真正的 C++ 函数被调用，函数体里的门操作通过 `ExecutionManager` 一条条记录到底层 `CircuitSimulator`（即 nvqir 模拟器，见 u6-l1）。这是「边执行边记录」。
- **MLIR/QIR 模式**：内核先被编译（AOT 或 JIT）成 QIR 机器码，再由 QPU 的 `launchKernel` 执行编译产物。这是「先编译后执行」。

两种模式的分叉点在 `detail::launch`。本讲重点是骨架，所以两条路径都点到，但把更多笔墨放在「公共骨架」上。

#### 4.3.2 核心流程

下面是一次同步 `cudaq::sample`（默认 qpp、库模式或本地模拟器）的完整流程，用文字流程图表示：

```
cudaq::sample(kernel)                         [sample.h:280]
   │
   │  取平台、取内核名、默认 shots=1000
   ▼
detail::runSampling(...)                      [sample.h:130]
   │
   │  ① 构造 ExecutionContext ctx("sample", shots, qpu_id)
   │  ② samplePreamble 填充 ctx 与 sample_policy
   │  ③ 进入 shot 循环 while(counts.shots < 目标):
   ▼
detail::launch(policy, qpu_id, ctx, platform, kernel)   [launch.h:37]
   │
   ├── 若库模式 ───────────────────────────────────────┐
   │     with_policy_and_ctx(policy, ctx, kernel)      │ ExecutionContext.h:264
   │        setExecutionContext(&ctx)                   │ 把 ctx 放到 thread_local
   │        ExecutionManager::with_default_em(policy,   │ execution_manager.h:219
   │            [&]{ kernel(); })                       │
   │           em->configureExecutionContext(policy)    │ → nvqir 模拟器
   │           em->beginExecution()                     │
   │           kernel()   ← 门/测量经 ExecutionManager │
   │                      落到 CircuitSimulator         │
   │           em->finalizeExecutionContext(policy)     │ → sample_result
   │           em->endExecution()                       │
   │        resetExecutionContext(); 恢复外层 ctx       │
   │                                                    │
   │── 若 MLIR/QIR 模式 ────────────────────────────────┤
   │     qpu = platform.getQPU(qpu_id)                  │
   │     ctx.executeKernelApi = 回调(JIT编译→qpu.launchKernel)
   │     with_policy_and_ctx(policy, ctx, kernel)       │
   │        setExecutionContext(&ctx)                   │
   │        kernel()  ← 内核入口改写后调用 altLaunchKernel
   │                   等 thunk，触发 executeKernelApi  │
   │        finalize ...                                │
   ▼
回到 runSampling 的 shot 循环，累加 result 到 counts
   │
   ▼
返回 sample_result 给用户
```

注意几个关键设计：

1. **`with_default_em` / `with_execution_context` 是执行骨架**：它们用 RAII/`try_finally` 保证「配置→开始→执行→终结→结束」这个序列即使抛异常也能正确收尾。
2. **`ExecutionContext` 是隐式流动的**：一旦 `setExecutionContext(&ctx)`，后续整条调用链（门、测量、模拟器）都通过 `getExecutionContext()` 共享同一个 ctx。
3. **终结按 `name` 分派**：`finalizeExecutionContext` 读 `ctx.name`，决定把结果整理成 `sample_result` 还是 `observe_result` 写回 ctx。

#### 4.3.3 源码精读

**顶层入口**——`cudaq::sample`：

[runtime/cudaq/algorithms/sample.h:280-L294](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L280-L294) 取 `cudaq::get_platform()`、内核名，调用 `detail::runSampling` 并传入默认 `DEFAULT_NUM_SHOTS`（承接 u1-l4，默认 1000）。

**创建上下文 + shot 循环**——`runSampling`：

[runtime/cudaq/algorithms/sample.h:145-L160](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L145-L160) 这里能看到三件事：构造 `ExecutionContext ctx("sample", shots, qpu_id)`；调用 `samplePreamble` 填充；然后 `while (counts.get_total_shots() < shots)` 循环调用 `detail::launch`，把每次返回的 `result` 累加到 `counts`。这就是「采样次数 = 多次执行内核」的实现。

[runtime/cudaq/algorithms/sample.h:171-L180](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L171-L180) 还能看到「0 shot 警告」的来历（承接 u1-l4 / u2-l3）：如果内核没有测量，一次执行产生 0 个 shot，循环会跳出并打印 `WARNING: this kernel invocation produced 0 shots ...`。

**分叉点**——`detail::launch`：

[runtime/cudaq/algorithms/launch.h:43-L59](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/launch.h#L43-L59) 这是「库模式分支」：判断 `platform.is_library_mode()`，若是，则用 `detail::with_policy_and_ctx` 配合 `ExecutionManager::with_default_em` 执行内核，并打印 `Launching kernel in library mode with policy sample`。

[runtime/cudaq/algorithms/launch.h:61-L104](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/launch.h#L61-L104) 这是「MLIR/QIR 模式分支」：取 `platform.getQPU(qpu_id)`，把一个 lambda 挂到 `ctx.executeKernelApi`（这个 lambda 负责 JIT 编译模块并调用 `qpu.launchKernel`），最后仍用 `with_policy_and_ctx` 在 context 内执行。

**执行骨架**——`with_default_em`（库模式）：

[runtime/cudaq/qis/execution_manager.h:219-L232](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.h#L219-L232) 这就是库模式的标准执行骨架：`configureExecutionContext(policy)` → `beginExecution()` → 执行 `f()` → `finalizeExecutionContext(policy)` → `endExecution()`，用 `try_finally` 包裹保证收尾。

**配置/终结转发到模拟器**：

[runtime/cudaq/qis/execution_manager.cpp:46-L59](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.cpp#L46-L59) `ExecutionManager::configureExecutionContext` 直接委托给 `nvqir::getCircuitSimulatorInternal()->configureExecutionContext(...)`。这就是 `ExecutionManager` 与具体模拟器（nvqir，见 u6-l1）的衔接点。

**按 `name` 分派的终结**：

[runtime/cudaq/qis/execution_manager.cpp:61-L72](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.cpp#L61-L72) `finalizeExecutionContext` 用 `policies::withPolicy(ctx.name, ...)` 把字符串 `name` 转回类型化的 policy，再分三种回收路径：`sample_result` 写到 `ctx.result`；`observe_result` 同时写 `ctx.result`（原始数据）和 `ctx.expectationValue`（期望值）；void 结果什么都不做。**这一段是「执行意图→结果类型」的关键映射**。

**`with_policy_and_ctx` 的实现**——设置/恢复 thread_local context：

[runtime/common/ExecutionContext.h:264-L286](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.h#L264-L286) 保存外层 context、`setExecutionContext(&ctx)`、执行内核、清理时 `resetExecutionContext` 并恢复外层。注意它会**保存并恢复外层 context**，这意味着内核可以嵌套调用（比如预追踪阶段在 sample 内部又跑一次 tracer context，见 `samplePreamble` 的 tracer 逻辑）。

> 补充：库模式下 `getDefaultExecutionManager()` 的取值优先级见 [execution_manager.h:266-L271](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.h#L266-L271)：先看是否有人显式设置（`getExecutionManagerInternal`），否则用注册的默认（`getRegisteredExecutionManager`）。这又是一个「可替换」的扩展点。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用 `CUDAQ_LOG_LEVEL` 打开运行时日志，亲眼追踪一次 `cudaq::sample` 的 `ExecutionContext` 创建与平台分发过程。这正是本讲规格里要求的实践任务。

**操作步骤**：

1. 复用 4.1.4 里的 `bell` 内核（或任何含测量的内核），编译成可执行文件：

   ```bash
   nvq++ probe_platform.cpp -o probe_platform.x
   ```

2. 用日志级别运行（`trace` 最详细，会包含 `info` 级日志；若噪音过多可先用 `info`）：

   ```bash
   CUDAQ_LOG_LEVEL=trace ./probe_platform.x 2>&1 | tee run.log
   ```

3. 在日志里检索分发链路的关键痕迹（这些都是源码里 `CUDAQ_INFO` 打出来的字符串，是可靠的检索锚点）：

   ```bash
   grep -nE "Launching kernel|policy|library mode|Compiling|set.*platform" run.log
   ```

**需要观察的现象**（基于源码推理的预期，**实际输出待本地验证**）：

- 在 [launch.h:52](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/launch.h#L52) 或 [launch.h:95](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/launch.h#L95) 处的 `CUDAQ_INFO` 会产生形如 `Launching kernel in library mode with policy sample` 或 `Launching kernel in sync mode with policy sample` 的日志——**这一行直接告诉你走了哪条分支**。
- 在 [launch.h:73](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/launch.h#L73) / [launch.h:87](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/launch.h#L87) 处的 `No compiled module found. Compiling.` 或 `Launching kernel.` 在 MLIR/QIR 模式下出现，体现「JIT 编译 → launchKernel」的过程。
- 可能还会看到 [quantum_platform.cpp:50](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/quantum_platform.cpp#L50) 或 [execution_manager.cpp:26](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.cpp#L26) 处的 `external caller setting the ...` 日志，对应插件/外部设置的 platform 或 ExecutionManager。

**预期结果**：你能从日志里重建出 `sample → runSampling → launch → with_default_em/launchKernel → 模拟器` 这条调用顺序，并能判断本次运行走的是库模式还是 MLIR/QIR 模式。如果想把日志写进文件，可用 `CUDAQ_LOG_FILE=run.log`（见 [logger.cpp:61-L65](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/logger/logger.cpp#L61-L65)）。

> 关于 `CUDAQ_LOG_LEVEL` 的取值：它用的是 spdlog 的级别名（`trace/debug/info/warn/err/critical/off`），可逗号叠加，见 [logger.cpp:53-L59](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/logger/logger.cpp#L53-L59)。默认级别是 `warn`（[logger.cpp:51](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/logger/logger.cpp#L51)），所以平时看不到这些 info 日志。

#### 4.3.5 小练习与答案

**练习 1**：在 `detail::launch` 里，库模式和 MLIR/QIR 模式分别用什么执行内核？它们共用哪一步？
**答案**：库模式用 `ExecutionManager::with_default_em`（[launch.h:55](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/launch.h#L55)）；MLIR/QIR 模式用 `ctx.executeKernelApi` 回调触发 JIT 编译并调用 `qpu.launchKernel`（[launch.h:88](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/launch.h#L88)）。两者共用 `detail::with_policy_and_ctx` 来设置/恢复 `ExecutionContext`（[launch.h:99](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/launch.h#L99)）。

**练习 2**：为什么 `with_policy_and_ctx` 在执行前要保存「外层 context」并在结束后恢复它？
**答案**：因为 `ExecutionContext` 是 thread_local 的单一槽位。如果当前线程已经在一个外层执行里（例如 sample 内部还要做一次 tracer 预追踪，见 [sample.h:102-L106](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L102-L106)），内层执行结束后必须恢复外层 context，否则外层后续的门操作会拿不到正确的上下文。

**练习 3**：终结阶段是怎么知道该把结果整理成 `sample_result` 还是 `observe_result` 的？
**答案**：靠 `ctx.name`。`finalizeExecutionContext` 用 `policies::withPolicy(ctx.name, ...)` 把字符串映射到类型化 policy，再分派到不同的回收逻辑，见 [execution_manager.cpp:61-L72](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.cpp#L61-L72)。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「执行模型观测器」小任务。

**任务**：写一个含测量的内核（如 Bell 态或 GHZ 态），完成以下三件事：

1. **平台画像**：在执行前用 `cudaq::get_platform()` 打印 `name()`、`num_qpus()`、`is_simulator()`、`is_remote()`、`get_num_qubits(0)`（对应 4.1）。
2. **日志追踪**：用 `CUDAQ_LOG_LEVEL=info`（或 `trace`）运行，从日志里找出 `Launching kernel ... with policy sample` 这一行，判断本次走了库模式还是 MLIR/QIR 模式（对应 4.3）。
3. **链路对照**：在日志或源码里，把以下五个节点按发生顺序排列，并各写一句话说明它的作用：
   - `ExecutionContext ctx("sample", ...)` 构造
   - `setExecutionContext(&ctx)`
   - `kernel()` 执行
   - `finalizeExecutionContext(ctx)`
   - `resetExecutionContext()`

**验收标准**：

- 你能指出这五个节点分别对应源码的哪一行（提示：构造在 [sample.h:146](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L146)；设置/重置在 [ExecutionContext.h:271/L275](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ExecutionContext.h#L271) 的 `with_policy_and_ctx`；终结在 [execution_manager.cpp:61](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/execution_manager.cpp#L61)）。
- 你能用一句话解释：为什么修改 `ctx.shots` 会改变最终采样次数（提示：shot 循环在 [sample.h:157](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L157)）。采样概率满足 \(P(b)=N_b/S\)，其中 \(S\) 是 `shots` 总数、\(N_b\) 是比特串 \(b\) 被观测到的次数；增大 \(S\) 会让估计值 \(P(b)\) 更逼近真实概率分布。

如果本地无法构建 CUDA-Q，可以把「运行」替换为「源码阅读」：照着上面的行号在仓库里走一遍，把五个节点串成一条带行号的调用链写在笔记里。

---

## 6. 本讲小结

- CUDA-Q 用「`quantum_platform` 拥有一组 `QPU`」的两层抽象描述量子硬件；平台是门面/调度层，QPU 才是执行单元，且 QPU 默认就是模拟器，不一定是真实硬件。
- `ExecutionContext` 是「执行工单」，用 `name`（`"sample"`/`"observe"`/`"tracer"`/...）标识执行种类，用 `shots`/`noiseModel`/`spin`/`qpuId` 等字段承载执行意图；它是 thread_local 的，所以门/测量函数能隐式共享当前 context。
- 一次 `cudaq::sample` 的链路是：`sample` → `runSampling`（构造 ctx、shot 循环）→ `detail::launch`（库模式 vs MLIR/QIR 模式分叉）→ `with_policy_and_ctx`/`with_default_em`（配置→执行→终结骨架）→ 模拟器执行 → 按 `name` 分派终结、把结果写回 ctx。
- 平台/ExecutionManager 都通过注册宏 + 插件加载在运行时获取具体实现，这是「链接期决定后端」在运行时的具体落地。
- `CUDAQ_LOG_LEVEL`（spdlog 级别，默认 `warn`）可以打开运行时日志，`Launching kernel ... with policy ...` 等字符串是追踪分发链路的可靠锚点。

## 7. 下一步学习建议

- **u3-l2（`cudaq::sample` 与异步执行深入）**：本讲只点了 `runSampling` 的同步 shot 循环，异步版 `sample_async` 会用到 QPU 的 `execution_queue`（[qpu.h:52](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform/qpu.h#L52)）和 `enqueueAsyncTask`，下一讲深入。
- **u3-l3（`cudaq::observe` 与 spin_op）**：本讲已铺垫 `ExecutionContext` 的 `name="observe"`、`spin`、`expectationValue` 字段以及终结阶段的 observe 分支，下一讲讲期望值 \(\langle H\rangle\) 具体怎么算。
- **u6-l1（nvqir 与 CircuitSimulator API）**：本讲多次提到 `ExecutionManager` 把指令转发给 `nvqir::CircuitSimulator`，那是真正「施加门、维护状态向量」的地方，u6 单元会拆开讲。
- **u6-l4（平台与目标配置）**：本讲的 `quantum_platform` 是抽象基类，它的具体实现（`DefaultQPU`、YAML 目标配置）在 u6-l4。
- **u6-l6（多 QPU 与分布式）**：本讲多次提到「一个平台可含多个 QPU」「thread_local context 支持并发」，mqpu 平台正是把这两点用到极致，u6-l6 展开。
