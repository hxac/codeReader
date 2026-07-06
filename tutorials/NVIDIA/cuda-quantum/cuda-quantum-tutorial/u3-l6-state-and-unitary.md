# get_state、酉矩阵与状态获取

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `cudaq::sample`（采样）、`cudaq::observe`（期望值）、`cudaq::get_state`（态提取）与 `cudaq::get_unitary`（酉矩阵提取）四者在**执行语义**上的根本差异。
- 掌握 `get_state` / `get_state_async` 的用法，以及返回类型 `cudaq::state` 提供的读取接口（`amplitude`、`overlap`、`get_tensor` 等）。
- 理解 `get_unitary` 走的是「**门序列 trace + 矩阵乘法**」而非「真实模拟」这条特殊路径，并能阅读 `unitary_from_trace` 的重建逻辑。
- 知道状态获取对**后端能力**的硬性要求：只能在模拟器上用，且对张量网络/MPS 这类「非数组型」状态，线性下标读取会受限。

本讲是单元 3「执行模型与算法原语」的收尾，承接 u3-l1 的 `ExecutionContext` 分发骨架，把「执行意图」从「采样/观测」扩展到「直接拿出完整量子态与酉矩阵」。

## 2. 前置知识

本讲默认你已掌握 u3-l1 的两个核心结论，这里只做最简回顾：

- **ExecutionContext 是「执行工单」**：一次内核调用会被包成一个 `ExecutionContext`，其 `name` 字段（如 `"sample"`、`"observe"`）告诉运行时这次执行要产出什么。门与测量函数通过 `thread_local` 的上下文槽位隐式共享同一个工单。
- **平台 `with_execution_context(ctx, kernel)` 是统一入口**：它把上下文挂到当前线程，执行内核，再把上下文摘下来；后端在执行过程中根据 `ctx.name` 决定是「累计 shot 计数」还是「保留态向量」。

如果你还不熟悉「shots」「采样分布」「期望值」这些词，请先读 u3-l1、u3-l2、u3-l3。本讲只回答一个新问题：**当我不想拿计数、也不想拿标量，而是想直接拿内核执行后的完整量子态（或整条线路的酉矩阵）时，CUDA-Q 怎么做？**

一个关键直觉：真实 QPU 是「黑盒采样器」，它只能给你比特串计数，物理上不可能把态向量打印出来；只有**模拟器**才在内存里显式持有态向量或密度矩阵。所以「提取态」本质是一种模拟器特权——这一点会反复出现在源码里。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [runtime/cudaq/algorithms/get_state.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/get_state.h) | `get_state` / `get_state_async` 模板实现，核心是 `detail::extractState`：建 `"extract-state"` 上下文 → 跑一次内核 → 接管 `simulationState`。 |
| [runtime/cudaq/qis/state.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/state.h) | `cudaq::state` 类：对后端 `SimulationState` 的值语义包装，提供 `amplitude` / `overlap` / `get_tensor` / `dump` 等读取接口。 |
| [runtime/cudaq/algorithms/unitary.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/unitary.h) | `cudaq::contrib::get_unitary_cmat`：用 trace 重建酉矩阵，含 `unitary_from_trace`、`apply_gate_in_place`、`make_controlled_unitary`。 |
| [runtime/cudaq/algorithms/draw.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/draw.h) | `contrib::traceFromKernel`：建 `"tracer"` 上下文、跑内核收集门序列，是 `get_unitary` 与 `draw` 共用的前置步骤。 |
| [runtime/common/SimulationState.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SimulationState.h) | `SimulationState` 抽象基类与 `Tensor` 结构，定义后端如何向宿主暴露态数据；`isArrayLike()` 决定能否线性下标读取。 |
| [python/cudaq/runtime/state.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/state.py) / [unitary.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/unitary.py) | Python 前端 `cudaq.get_state` / `cudaq.get_state_async` / `cudaq.get_unitary`，编译内核后转发到 C++ 运行时。 |

## 4. 核心概念与源码讲解

### 4.1 状态获取与采样差异

#### 4.1.1 概念说明

到目前为止，我们用 `sample` 拿到的是「比特串 → 出现次数」的**统计直方图**，用 `observe` 拿到的是哈密顿量期望值这个**标量**。两者都隐含了一个动作：**重复执行很多次（shots）然后聚合**。

但很多时候你想要的不是统计，而是**完整的量子信息**：

- 我想知道内核制备出的纯态振幅 \(\alpha_{i}\) 具体是多少（例如验证一个 ansatz 是否正确制备了 \((|00\rangle+|11\rangle)/\sqrt{2}\)）。
- 我想算两个内核制备态之间的保真度 \(|\langle\psi|\phi\rangle|\)。
- 我想拿到整条线路的酉矩阵 \(U\)，去做后续的经典线性代数。

这些都需要「**把内核跑一次，然后把后端内存里的态原样交出来**」，而不是「跑很多次再统计」。这就是 `get_state` 与 `sample`/`observe` 的根本分野。

数学上，对于一个作用在 \(n\) 比特上的内核 \(U\)，从全 \(|0\rangle\) 出发：

\[
|\psi\rangle = U|0\rangle^{\otimes n} = \sum_{i=0}^{2^n-1} \alpha_i |i\rangle
\]

- `sample`：给 \(p_i = |\alpha_i|^2\) 的**经验估计**（受 shot 噪声影响）。
- `get_state`：直接给复数振幅向量 \((\alpha_0,\dots,\alpha_{2^n-1})\)（精确，无 shot 噪声）。
- `get_unitary`：直接给 \(2^n\times 2^n\) 的酉矩阵 \(U\) 本身（注意 \(|\psi\rangle\) 只是 \(U\) 的第一列）。

一句话总结：**采样是「测量后的统计」，状态获取是「测量前的完整量子信息」**。这决定了前者可在真实 QPU 上做、后者只能在模拟器上做。

#### 4.1.2 核心流程

四种执行原语在 u3-l1 的分发骨架上分叉如下：

| 原语 | ExecutionContext 名 | 执行次数 | 后端做什么 | 产出 |
|---|---|---|---|---|
| `sample` | `"sample"` | \(N\) shots | 逐 shot 测量、累计计数 | 比特串直方图 |
| `observe` | `"observe"` | 逐项 \(\times\) shots | 逐泡利项测期望、加权求和 | 标量能量 |
| `get_state` | `"extract-state"` | **1 次** | 跑完内核、**保留** `simulationState` | 态向量/密度矩阵 |
| `get_unitary` | `"tracer"` | **1 次**（仅记录门） | 把每个门追加进 `kernelTrace`、**不必真模拟** | 酉矩阵 |

`get_state` 的关键动作只有三步：

1. 校验当前平台是模拟器（否则直接抛错）。
2. 创建名为 `"extract-state"` 的上下文，用 `with_execution_context` 跑一次内核——后端看到这个名字，就不会去做 shot 聚合，而是把最终的 `SimulationState` 留在上下文里。
3. 把上下文里的 `simulationState`（一个 `unique_ptr`）**转移所有权**给返回值 `state` 对象。

#### 4.1.3 源码精读

整个流程的最简实现就是 `detail::extractState`，全部逻辑不到 20 行：

[runtime/cudaq/algorithms/get_state.h:36-56](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/get_state.h#L36-L56) —— 这是本讲最重要的一段代码。逐行解读：

- 第 42-43 行：`if (!platform.is_simulator()) throw ...`——状态提取是模拟器特权，物理 QPU 直接拒绝。
- 第 46 行：`ExecutionContext context("extract-state");`——用工单名字告诉后端「这次只要态、不要计数」。
- 第 48 行：`platform.with_execution_context(context, kernel)`——挂上下文 → 执行内核 → 摘上下文（u3-l1 讲过的统一入口）。
- 第 55 行：`return state(context.simulationState.release());`——`release()` 把 `SimulationState` 的所有权从上下文「移交」给新建的 `state`，避免拷贝整个态向量。

对比一下 `sample`（u3-l2）：`sample` 会 `while (counts.get_total_shots() < shots)` 反复 `launch`，并按 `name=="sample"` 把每 shot 结果累加进计数表；而 `extractState` 只跑**一次**，并且后端在 `"extract-state"` 名下不走测量/计数路径，而是把算完的态留在 `simulationState` 里。**同一个分发骨架，靠 `ctx.name` 长出完全不同的行为**——这正是 u3-l1「执行工单」抽象的威力。

#### 4.1.4 代码实践

**实践目标**：直观感受「采样给分布、取态给振幅」的差异。

**操作步骤**（Python，最容易跑）：

```python
# 示例代码
import cudaq, numpy as np

@cudaq.kernel
def bell():
    q = cudaq.qvector(2)
    h(q[0])
    x.ctrl(q[0], q[1])   # CNOT

# 1) 采样：拿分布
counts = cudaq.sample(bell, shots_count=1000)
print("采样分布:", counts)

# 2) 取态：拿振幅
s = cudaq.get_state(bell)
print("态向量:", np.array(s.dump()))
```

**需要观察的现象**：

- 采样结果只会出现 `00` 和 `11` 两种比特串，各约 500 次（受 shot 噪声影响，未必精确对半）。
- 态向量是 4 个复数，索引 0 和 3 的幅值约为 \(1/\sqrt{2}\approx 0.707\)，索引 1、2 为 0。

**预期结果**：数学上 Bell 态为

\[
|\Phi^+\rangle = \frac{|00\rangle+|11\rangle}{\sqrt{2}}
\]

对应振幅向量 \([0.707,\ 0,\ 0,\ 0.707]\)。采样看不到相位、也看不到中间振幅，只能看到 \(|\alpha_i|^2\)；`get_state` 把完整复数振幅都给你。这正是二者信息量的差距。

> 关于输出格式：`state.dump()` 在 Python 端返回可被 `np.array(...)` 转成 ndarray 的结构，C++ 端 `dump()` 则直接打印到标准输出。具体的打印排版（是否带括号、是否换行）请以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：把上面内核的 `h(q[0])` 换成 `h(q[0]); h(q[0]);`（连续两个 H），`get_state` 会输出什么？采样分布又是什么？

**答案**：两个 H 抵消（\(H^2=I\)），随后 CNOT 作用在 \(|00\rangle\) 上仍是 \(|00\rangle\)。态向量为 \([1,0,0,0]\)，采样 100% 为 `00`。

**练习 2**：用一句数学式说明为什么真实 QPU 无法实现 `get_state`。

**答案**：真实 QPU 只能通过测量得到概率 \(p_i=|\alpha_i|^2\)，测量会塌缩态且丢失相位；提取完整复数振幅 \(\alpha_i\) 需要对态进行层析（指数次测量）或直接读取内存中的态向量，后者只有模拟器能做到。

---

### 4.2 get_state 的用法与 state 类型

#### 4.2.1 概念说明

`get_state` 的返回类型 `cudaq::state` 是一个**值语义的薄包装**：它内部用一个 `std::shared_ptr<SimulationState>` 持有真正的后端态数据（态向量、密度矩阵，或张量网络/MPS 的一组张量）。值语义意味着你可以自由拷贝、返回、存进容器，而不必担心深拷贝整个态向量——多次拷贝共享同一份后端数据。

`SimulationState`（在 [runtime/common/SimulationState.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SimulationState.h) 中定义）是后端需要实现的抽象基类。它的核心设计是「**用一组 Tensor 描述底层态**」，每个 `Tensor` 只记 `{数据指针, 各维 extents, 浮点精度}`，数据可以留在 GPU 显存或 CPU 内存——目的是尽量**避免不必要的数据搬运**。不同后端对应的态形态：

- **态向量后端**（如 qpp、custatevec）：1 个张量，rank=1，\(2^n\) 个元素。
- **密度矩阵后端**（如 dem）：1 个张量，rank=2，\(2^n\times 2^n\) 个元素。
- **张量网络 / MPS 后端**（如 cutensornet）：**多个**张量，且通常**不是**连续数组。

这就引出了 `state` 的两种读取风格：对「数组型」态，可以用 `operator[]` 线性下标取元素；对 MPS 这类非数组型，线性下标会抛错，必须用 `amplitude(basisState)` 按计算基取值。

#### 4.2.2 核心流程

`get_state` 的调用与读取流程：

```
get_state(kernel, args...)
   └─ extractState(lambda)
        ├─ platform.is_simulator()?            // 校验
        ├─ ExecutionContext ctx("extract-state")
        ├─ platform.with_execution_context(ctx, lambda)  // 跑一次
        └─ return state(ctx.simulationState.release())    // 移交所有权

state s = get_state(...)
   ├─ s.dump()                // 打印
   ├─ s[i]                    // 线性下标取振幅（数组型）
   ├─ s.amplitude({b0,b1,..}) // 按计算基取振幅（通用）
   ├─ s.overlap(other)        // |<this|other>|
   ├─ s.get_tensor(idx)       // 取底层 Tensor
   ├─ s.get_num_qubits()
   └─ s.is_on_gpu() / s.to_host(...)
```

异步版 `get_state_async` 走 u3-l2 讲过的「每 QPU 一条队列」异步框架：它返回 `std::future<state>`（C++）或 `AsyncStateResult`（Python），任务经 `platform.enqueueAsyncTask(qpu_id, ...)` 入队。

#### 4.2.3 源码精读

**入口模板与模式分叉**。`get_state` 用预处理宏区分三种编译模式：

[runtime/cudaq/algorithms/get_state.h:96-119](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/get_state.h#L96-L119) —— 设备模式（`CUDAQ_QUANTUM_DEVICE`）下，`get_state` 不真跑模拟器，而是构造一个延迟求值的 `QPUState`（保存内核名 + Quake 代码 + 实参），交由后端在未来按需计算；只有宿主模拟模式（最常用的 `else` 分支，第 116-117 行）才调 `detail::extractState` 真正立刻取态。这说明同一个 API 在「真机/设备」与「模拟」下行为不同——但都对用户呈现成 `state`。

**异步版**：

[runtime/cudaq/algorithms/get_state.h:59-90](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/get_state.h#L59-L90) —— `runGetStateAsync` 与同步版几乎一样，差别是：`context.asyncExec = true`、`context.qpuId = qpu_id`，用 `std::promise<state>` 把结果投递给 `std::future`，再用 `platform.enqueueAsyncTask(qpu_id, wrapped)` 入队（与 u3-l2 的 `sample_async` 共用同一套「每 QPU 一队列」机制）。

**`state` 类的读取接口**：

[runtime/cudaq/qis/state.h:107-157](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/state.h#L107-L157) —— 关键接口逐一点名：

- `operator[](idx)`（第 107 行）：按线性下标取**向量**元素。
- `operator()(idx, jdx)`（第 110 行）：按二维下标取**矩阵**元素（密度矩阵用）。
- `operator()(init_list, tensorIdx)`（第 113-114 行）：通用多维提取。
- `get_tensor / get_tensors / get_num_tensors`（第 119-125 行）：拿底层 `Tensor`（数据指针 + extents + 精度）。
- `get_num_qubits`（第 128 行）、`get_precision`（第 131 行）、`is_on_gpu`（第 134 行）、`to_host`（第 137-144 行，仅 GPU 态需要、否则抛错）。
- `overlap(other)`（第 154 行）：计算纯态的 \(|\langle\psi|\phi\rangle|\)。
- `amplitude(basisState)` / `amplitudes(...)`（第 157-162 行）：按计算基（如 `{0,1}` 表示 \(|01\rangle\)）取振幅——这是对**任意**后端（含 MPS）都安全的取值方式。

注意第 26 行 `std::shared_ptr<SimulationState> internal;`——这就是值语义的来源：拷贝 `state` 只增加引用计数，不复制态数据。

**后端如何描述态**：

[runtime/common/SimulationState.h:110-125](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SimulationState.h#L110-L125) —— `Tensor` 结构：`data` 指针 + `extents`（各维度大小）+ `fp_precision`，并提供 `get_rank`、`get_num_elements`、`element_size`。一个 `SimulationState` 由若干这样的 `Tensor` 组成。同文件第 239 行 `isArrayLike()` 默认 true，但张量网络/MPS 后端会覆盖为 false，此时 `operator()` 线性下标会走第 213-220 行抛错路径，提示改用 `getAmplitude`。

#### 4.2.4 代码实践

**实践目标**：用一个带参数的内核，体验 `state` 的几种读取方式，并验证态向量振幅与采样概率的关系。

**操作步骤**（C++）：

```cpp
// 示例代码
#include <cstdio>
#include <cudaq.h>

__qpu__ void ghz(int n) {
    cudaq::qvector q(n);
    h(q[0]);
    for (int i = 0; i < n - 1; ++i)
        x<cudaq::ctrl>(q[i], q[i + 1]);
}

int main() {
    cudaq::state s = cudaq::get_state(ghz, 3);   // 3-qubit GHZ
    s.dump();                                     // 打印全部振幅

    // 按计算基取振幅：|000> 与 |111>
    printf("|000> = (%.4f, %.4f)\n",
           std::real(s.amplitude({0,0,0})), std::imag(s.amplitude({0,0,0})));
    printf("|111> = (%.4f, %.4f)\n",
           std::real(s.amplitude({1,1,1})), std::imag(s.amplitude({1,1,1})));

    // 线性下标取振幅（数组型后端可用）
    printf("s[0] = (%.4f, %.4f)\n", std::real(s[0]), std::imag(s[0]));

    printf("num_qubits = %zu\n", s.get_num_qubits());
    return 0;
}
```

编译运行（默认 qpp 后端）：

```bash
nvq++ ghz.cpp -o ghz.x && ./ghz.x
```

**需要观察的现象**：

- `dump()` 会打印 \(2^3=8\) 个复数振幅。
- `amplitude({0,0,0})` 与 `amplitude({1,1,1})` 应为 \(1/\sqrt{2}\)，其余计算基（如 `{0,1,0}`）为 0。
- `s[0]` 与 `amplitude({0,0,0})` 数值相同（线性下标 0 对应 \(|000\rangle\)）。

**预期结果**：3 比特 GHZ 态为

\[
|\text{GHZ}\rangle = \frac{|000\rangle+|111\rangle}{\sqrt{2}}
\]

故振幅在 \(|000\rangle\)、\(|111\rangle\) 处为 \(1/\sqrt{2}\)，其余为 0。把它对采样：`cudaq::sample(ghz, 3)` 只会给出约各半的 `000` 与 `111`，看不到其它信息——再次印证 4.1 的结论。

> 若你把目标切到张量网络/MPS 后端（见 u6-l2），`s[0]` 这类线性下标可能抛「不支持线性索引」错误，那时改用 `amplitude(...)` 即可。具体报错文案以本地为准。

#### 4.2.5 小练习与答案

**练习 1**：写一段代码，用 `overlap` 计算两个内核制备态的保真度，验证 Bell 态 \((|00\rangle+|11\rangle)/\sqrt{2}\) 与自己重叠为 1。

**答案**：

```cpp
// 示例代码
cudaq::state a = cudaq::get_state(bell_kernel);
cudaq::state b = cudaq::get_state(bell_kernel);
std::printf("|<a|b>| = %.6f\n", std::abs(a.overlap(b))); // 1.000000
```

**练习 2**：为什么 `state` 用 `shared_ptr` 而不是直接持有 `vector<complex>`？

**答案**：态数据可能很大（\(2^n\) 随比特数指数增长）、可能驻留 GPU 显存、可能是 MPS 的多张量结构，无法用单一连续 `vector` 表达。`shared_ptr<SimulationState>` 让 `state` 保持值语义（可拷贝、可返回）的同时，由后端子类决定数据实际如何存放，并避免深拷贝整个态。

---

### 4.3 get_unitary 与酉矩阵重建（trace 机制）

#### 4.3.1 概念说明

`get_unitary` 想拿的是整条线路的酉矩阵 \(U\)。注意它和 `get_state` 有个微妙差别：

\[
|\psi\rangle = U|0\rangle^{\otimes n}
\]

`get_state` 给的是 \(|\psi\rangle\)（一个长度 \(2^n\) 的列向量），而 `get_unitary` 给的是 \(U\) 本身（一个 \(2^n\times 2^n\) 的矩阵）——\(U\) 的第一列才等于 \(|\psi\rangle\)。

更关键的是实现路径不同。`get_state` 必须「真跑模拟器」才能得到最终态；而 `get_unitary` 走了一条更轻的路径：**它根本不需要把态演化算出来，只需要按顺序记录内核施加了哪些门，然后把每个门的矩阵按作用顺序连乘起来**。

这套「按顺序记录门」的机制就是 **Trace**（执行路径轨迹）。它同时也是 `cudaq::draw`（画电路图）的数据来源——所以你会看到 `get_unitary` 和 `draw` 共用同一个 `traceFromKernel` 前置步骤。

> 一个重要限制（来自 [runtime/common/Trace.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/Trace.h) 第 30-34 行注释）：Trace 只能记录执行管理器「看得见」的一条**执行路径**。如果内核里有经典控制流（`if`/`for` 随测量结果分支），Trace 代表的是这一次具体执行的路径——所以带中途测量条件分支的内核，其「酉矩阵」未必良定义，`get_unitary` 主要面向**纯酉、无测量**的线路。

#### 4.3.2 核心流程

`get_unitary` 的两步：

```
get_unitary_cmat(kernel, args...)
   ├─ trace = traceFromKernel(kernel, args...)   // 第 1 步：记录门序列
   │     ├─ ExecutionContext ctx("tracer")
   │     └─ 跑内核；执行管理器把每个门 append 进 ctx.kernelTrace
   └─ U = unitary_from_trace(trace)              // 第 2 步：矩阵重建
         ├─ U = I (2^n × 2^n)
         └─ for inst in trace:
              gate = nvqir::getGateByName(inst.name, inst.params)  // 查门矩阵
              if 有控制位: gate = make_controlled_unitary(gate, #ctrl)
              apply_gate_in_place(U, gate, n, inst_qubits)         // U ← U · gate
```

矩阵重建的数学含义：把每个门 \(G_k\) 提升到全 \(n\) 比特空间（受控位还要做「除全 1 块外填单位」的张量扩充），然后按施加顺序左乘到累计矩阵 \(U\) 上：

\[
U = G_m G_{m-1}\cdots G_2 G_1
\]

（\(G_1\) 是内核里第一个施加的门；作用在态上时最先施加的门在最右侧。）

#### 4.3.3 源码精读

**第 1 步：记录门序列**（与 `draw` 共用）：

[runtime/cudaq/algorithms/draw.h:31-52](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/draw.h#L31-L52) —— `contrib::traceFromKernel`：注意第 37-41 行，非模拟器平台它**不抛错、只警告并返回空 Trace**（与 `get_state` 的「直接抛」不同，因为 `draw` 想在更多场景下优雅降级）；第 45 行建 `"tracer"` 上下文；第 51 行返回 `context.kernelTrace`。`"tracer"` 名下，执行管理器不会真去演化态，而是把每个门 `(name, params, controls, targets)` 追加进 `Trace`。

`Trace` 的结构见 [runtime/common/Trace.h:38-88](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/Trace.h#L38-L88)：核心是 `Instruction{name, params, controls, targets}` 列表 + `numQudits`，支持 Gate/Noise/Measurement 三种指令类型。

**第 2 步：矩阵重建**：

[runtime/cudaq/algorithms/unitary.h:115-142](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/unitary.h#L115-L142) —— `unitary_from_trace` 逐条遍历 Trace：

- 第 121-122 行：用 `nvqir::getGateNameFromString` + `nvqir::getGateByName<double>` 从 nvqir 门库查出该门的小矩阵（如 H 是 \(2\times2\)，CNOT 的目标是 \(2\times2\)）。**这隐含一个要求：每个门都得有已知矩阵定义**，自定义门若无矩阵则在此失败（见 u7-l4）。
- 第 127-128 行：若带控制位，调 `make_controlled_unitary` 把它扩成受控版本。
- 第 139 行：`apply_gate_in_place(U, gate, num_qubits, inst_qubits)` 把这个小门矩阵「散布」到全 \(n\) 比特矩阵 \(U\) 的正确行列位置上。

**受控门如何构造**：

[runtime/cudaq/algorithms/unitary.h:24-38](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/unitary.h#L24-L38) —— `make_controlled_unitary` 的算法很直观：从 \(2^{\#\text{ctrl}}\times g\) 维单位阵开始，把右下角「所有控制位均为 1」的子块替换为原门矩阵。这正是受控门的数学定义 \(I\oplus G\)。

**入口函数**：

[runtime/cudaq/algorithms/unitary.h:149-153](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/unitary.h#L149-L153) —— `get_unitary_cmat` 把两步串起来，返回 `complex_matrix`。注意它位于 `namespace cudaq::contrib`（实验性命名空间），且 C++ 端入口名带 `_cmat` 后缀以强调返回的是 `complex_matrix` 类型；Python 端则暴露为更简洁的 `cudaq.get_unitary`，返回 numpy 数组：

[python/cudaq/runtime/unitary.py:15-44](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/unitary.py#L15-L44) —— 编译内核后转发到 C++ 运行时的 `get_unitary_impl`。

#### 4.3.4 代码实践

**实践目标**：对 Bell 态内核，分别用 `get_state` 拿态向量、用 `get_unitary` 拿酉矩阵，验证「\(U\) 的第一列就是 \(|\psi\rangle\)」。

**操作步骤**（Python，`cudaq.get_unitary` 直接返回 numpy 数组，便于对照）：

```python
# 示例代码
import cudaq, numpy as np

@cudaq.kernel
def bell():
    q = cudaq.qvector(2)
    h(q[0])
    x.ctrl(q[0], q[1])

U = cudaq.get_unitary(bell)
print("U shape:", U.shape)
print("U:\n", np.round(U, 4))

# 对比：get_state 给的 |ψ> 应等于 U 的第 0 列
psi = np.array(cudaq.get_state(bell).dump())
print("|ψ>       :", np.round(psi, 4))
print("U[:,0]    :", np.round(U[:,0], 4))
```

**需要观察的现象**：

- `U` 是 \(4\times4\) 复矩阵。
- `U[:,0]`（第 0 列，对应输入 \(|00\rangle\)）应与 `get_state` 返回的 `psi` 完全一致。
- `U` 应是酉矩阵：`np.allclose(U.conj().T @ U, np.eye(4))` 为真。

**预期结果**：

\[
U_{\text{Bell}} = |0\rangle\langle 0|\otimes I \;+\; |1\rangle\langle 1|\otimes X
= \begin{pmatrix} 1&0&0&0\\0&1&0&0\\0&0&0&1\\0&0&1&0 \end{pmatrix}\cdot(H\otimes I)
\]

其第 0 列为 \([1/\sqrt2,\,0,\,0,\,1/\sqrt2]\)，与 Bell 态振幅一致。若 `np.round` 看到极小的虚部（如 `1e-17j`），那是浮点误差。

> C++ 用户可调 `cudaq::contrib::get_unitary_cmat(bell)` 拿到 `complex_matrix`，再用其 `operator(i,j)` 读取元素。

#### 4.3.5 小练习与答案

**练习 1**：对一个只含 `h(q[0])` 的单比特内核，`get_unitary` 返回的 \(2\times2\) 矩阵是什么？它的第 0 列等于什么？

**答案**：返回 Hadamard 矩阵

\[
H=\frac{1}{\sqrt2}\begin{pmatrix}1&1\\1&-1\end{pmatrix}
\]

第 0 列为 \([1/\sqrt2,\,1/\sqrt2]\)，即 \(H|0\rangle=|+\rangle\)，与 `get_state` 一致。

**练习 2**：为什么 `get_unitary` 走 trace + 矩阵乘，而不是像 `get_state` 那样「真跑模拟器再反推矩阵」？

**答案**：从态向量反推整条线路的酉矩阵需要对**每个计算基输入**都演化一次（共 \(2^n\) 次），代价极高；而门序列一旦确定，按顺序连乘小门矩阵即可在「一次遍历」内重建 \(U\)，复杂度远低。trace 路径因此是更高效、也更通用的做法（同一份 trace 还能画电路图）。

---

### 4.4 后端能力要求

#### 4.4.1 概念说明

把 4.1～4.3 串起来，会发现状态获取对后端有三层能力要求：

1. **必须是模拟器**。`get_state` 在物理 QPU 上直接抛 `"Cannot use get_state on a physical QPU."`；`get_unitary`/`draw` 在非模拟器上只警告并返回空。这是「真实硬件是黑盒采样器」的硬约束。
2. **`get_state` 依赖后端能产出 `SimulationState`**。态向量/密度矩阵后端都没问题；但**张量网络/MPS 后端的态不是连续数组**，这会影响「怎么读」——`operator[]` 线性下标会抛错，须改用 `amplitude(...)`。
3. **`get_unitary` 依赖每个门都有矩阵定义**。它靠 `nvqir::getGateByName` 查表；遇到没有矩阵的自定义门或噪声信道，重建会失败。

换句话说：「能不能取」取决于是不是模拟器；「取出来是什么形态」取决于具体模拟器；「酉矩阵能不能重建」取决于门是否可表为矩阵。这三层对应 u6 单元要讲的「后端能力位」思想——同一套 API，在不同后端上行为不同。

还有一个常被忽略的点：`get_state` 跑在**密度矩阵后端**（如带噪声的 dem）上时，返回的是**密度矩阵**而非态向量——它的 `Tensor` 是 rank=2、\(2^n\times 2^n\)。这是状态获取与噪声模拟（u6-l3）的交汇点。

#### 4.4.2 核心流程

判断与降级流程：

```
get_state(kernel)
 └─ platform.is_simulator() ?  ──No──▶ throw "Cannot use get_state on a physical QPU."
      │Yes
      └─ 跑内核，取 simulationState
           └─ 后端是数组型(isArrayLike)?
                 ├─ Yes: state[i] / state(i,j) 可用
                 └─ No (MPS/张量网络): state[i] 抛错 → 改用 state.amplitude(...)

get_unitary_cmat(kernel)
 └─ traceFromKernel: is_simulator()? ──No──▶ warn + 空 Trace
      │Yes
      └─ 对每个门 nvqir::getGateByName 查矩阵
           └─ 门无矩阵定义? ──▶ 重建失败
```

#### 4.4.3 源码精读

**「只在模拟器上」的硬校验**：

[runtime/cudaq/algorithms/get_state.h:42-43](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/get_state.h#L42-L43) —— `if (!platform.is_simulator()) throw std::runtime_error("Cannot use get_state on a physical QPU.");`。`is_simulator()` 的声明见 [runtime/cudaq/platform.h:41-43](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/platform.h#L41-L43)，它转发到平台内部的 `is_simulator()`（u3-l1 讲过的平台抽象）。

**对比：`traceFromKernel` 的软降级**：

[runtime/cudaq/algorithms/draw.h:37-41](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/draw.h#L37-L41) —— 同样是非模拟器，这里只 `std::cerr` 警告并返回空 `Trace()`，因为 `draw`/`get_unitary` 想在更多场景下不致命。

**「数组型 vs 非数组型」的读取分流**：

[runtime/common/SimulationState.h:212-220](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SimulationState.h#L212-L220) —— `operator()` 在 `!isArrayLike()` 时抛 `"Element extraction by linear indexing not supported ... Please use getAmplitude."`。第 239 行 `isArrayLike()` 默认 true，张量网络/MPS 后端覆盖为 false。这就是「同一份 `state` API，在不同后端上部分接口可用」的根源。

**「门必须有矩阵」的隐式要求**：

[runtime/cudaq/algorithms/unitary.h:121-124](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/unitary.h#L121-L124) —— `nvqir::getGateNameFromString` + `getGateByName<double>` 查表取矩阵；若门未注册矩阵定义（如某些自定义门），此处会失败。这与 u7-l4「自定义门需提供矩阵定义」相呼应。

#### 4.4.4 代码实践

**实践目标**：体会「同一内核，不同后端，状态形态不同」。

**操作步骤**（Python）：

```bash
# 1) 默认 qpp（态向量后端）
python -c "import cudaq; print(cudaq.get_state.__doc__[:80])"
# 跑你的 bell 内核，记录 get_state 的 dump 形态

# 2) 切到密度矩阵后端 dem（带噪声时可对比）
nvq++ ... --target densitencpu ...   # 具体 target 名以本地 nvq++ --help 为准
```

更直接的源码阅读型实践：

1. 打开 [runtime/common/SimulationState.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SimulationState.h)，找到 `isArrayLike()`（第 239 行）与 `isDeviceData()`（第 232 行）两个能力位。
2. 用 Grep 在 `runtime/nvqir/` 下搜索 `isArrayLike`，确认哪些后端（如 cutensornet MPS）覆盖了它。

**需要观察的现象**：

- 态向量后端：`state` 的单个 `Tensor` 是 rank=1，元素数 \(2^n\)。
- 密度矩阵后端：单个 `Tensor` 是 rank=2。
- 张量网络/MPS 后端：`get_num_tensors()` > 1，且 `state[0]` 抛错。

**预期结果**：你应当能用一句话说清「为什么 `get_state` 在 MPS 后端上 `dump()` 的输出形态与 qpp 不同」——因为底层 `SimulationState` 子类用多个张量表达态，而非单个连续数组。

> 各 target 名（如 `nvidia`, `qpp-cpu`, `tensornet` 等）与是否可用取决于本地构建是否链接了对应后端（u1-l3 讲过的「链接期决定后端」）。具体可用 target 列表请以本地 `nvq++ --help` 或 `cudaq --list-targets` 为准。

#### 4.4.5 小练习与答案

**练习 1**：在带噪声的 dem（密度矩阵）后端上对一个单比特 X 门内核调用 `get_state`，返回的 `state` 是向量还是矩阵？维度是多少？

**答案**：是 \(2\times2\) 的密度矩阵（rank=2 的单个 `Tensor`）。因为密度矩阵后端用 \(\rho\) 而非 \(|\psi\rangle\) 表达态。

**练习 2**：如果一个内核里用了 `cudaq::amplitude_damping` 这类噪声信道，`get_unitary` 还能成功吗？为什么？

**答案**：通常不能。噪声信道不是酉操作、没有单一矩阵表示，`unitary_from_trace` 在 `getGateByName` 查表时会失败。整条线路含噪声后不再是酉的，`get_unitary` 的「酉矩阵」前提就不成立；此时应改用 `get_state`（密度矩阵后端）拿 \(\rho\)。

---

## 5. 综合实践

把本讲三个最小模块串成一个端到端的小任务。

**任务**：编写一个 2 比特内核，先用 `h` + `cx` 制备 Bell 态 \((|00\rangle+|11\rangle)/\sqrt{2}\)，然后**同一次运行内**完成下列四件事并互相对照：

1. 用 `cudaq::sample` 采样 1000 次，打印 `00`/`11` 的计数。
2. 用 `cudaq::get_state` 取态向量，打印 4 个振幅，并计算 \(\sum_i |\alpha_i|^2\)（应严格为 1）。
3. 用 `cudaq::get_unitary`（C++ 用 `cudaq::contrib::get_unitary_cmat`，Python 用 `cudaq.get_unitary`）取 \(4\times4\) 酉矩阵，验证它是酉的（\(U^\dagger U = I\)），并验证其第 0 列等于第 2 步的态向量。
4. 把第 2 步的态向量按 \(p_i=|\alpha_i|^2\) 转成概率分布，与第 1 步的采样分布对比，体会「采样是 \(|\alpha_i|^2\) 的噪声估计」。

**参考框架（Python）**：

```python
# 示例代码
import cudaq, numpy as np

@cudaq.kernel
def bell():
    q = cudaq.qvector(2)
    h(q[0])
    x.ctrl(q[0], q[1])

# 1) 采样
counts = cudaq.sample(bell, shots_count=1000)
print("counts:", counts)

# 2) 态向量
s = cudaq.get_state(bell)
psi = np.array(s.dump())
print("psi:", np.round(psi,4), " norm² =", round(np.sum(np.abs(psi)**2), 6))

# 3) 酉矩阵
U = np.array(cudaq.get_unitary(bell))
print("unitary?", np.allclose(U.conj().T @ U, np.eye(4)))
print("U[:,0] == psi?", np.allclose(U[:,0], psi))

# 4) 概率对照
prob = np.abs(psi)**2
print("theoretical p:", np.round(prob,4))
```

**验收标准**：

- 第 2 步 \(\sum|\alpha_i|^2 = 1\)。
- 第 3 步两个布尔皆为 `True`。
- 第 4 步理论概率在 `00`、`11` 处各约 0.5，其余为 0，与采样计数比例吻合（容许 shot 噪声）。

做完后，你应该能用一句话向别人解释 `sample` / `get_state` / `get_unitary` 三者各自给出了量子线路的哪一层信息。

## 6. 本讲小结

- `get_state`、`get_unitary` 与 `sample`/`observe` 共用 u3-l1 的 `ExecutionContext` 分发骨架，仅靠上下文名（`"extract-state"` / `"tracer"` / `"sample"` / `"observe"`）长出不同行为。
- `get_state` 只跑**一次**内核、把后端 `SimulationState` 的所有权移交给值语义的 `state` 对象；它给出**完整复数振幅**，而 `sample` 只给 \(|\alpha_i|^2\) 的统计估计。
- `get_unitary` 走更轻的 **trace + 矩阵连乘** 路径（`traceFromKernel` → `unitary_from_trace`），不必真模拟；它与 `draw` 共用 trace 机制。
- 状态获取是**模拟器特权**：`get_state` 在物理 QPU 上直接抛错，`get_unitary`/`draw` 仅警告返回空。
- `state` 的读取接口对后端形态敏感：态向量/密度矩阵是数组型（可线性下标），张量网络/MPS 是多张量、非数组型（须用 `amplitude(...)`）。
- `get_unitary` 要求每个门都有矩阵定义；含噪声信道的非酉线路不适用，应改用 `get_state` 取密度矩阵。

## 7. 下一步学习建议

- **向后端深处去**：本讲反复出现的「`is_simulator`」「`isArrayLike`」「门矩阵查表」都是后端能力位的体现。建议进入 u6-l1（`CircuitSimulator` API）与 u6-l2（CPU/GPU 模拟器对比），看 `SimulationState` 的具体子类如何在不同后端实现 `getTensor` / `getAmplitude`。
- **向噪声去**：想理解「为什么密度矩阵后端的 `get_state` 返回矩阵」，请读 u6-l3（噪声与密度矩阵）。
- **向动力学去**：`get_state` 返回的 `state` 是 `cudaq::evolve`（u7-l1）做含时演化时跟踪态的核心数据结构，本讲是它的前置。
- **源码延伸阅读**：动手改一下 [unitary.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/unitary.h) 的 `unitary_from_trace`，加一行打印每个 trace 指令的 `name`，你会看到 `get_unitary` 与 `cudaq::draw` 看到的是完全相同的门序列——这会帮你彻底打通「trace 是电路的多用途中间表示」这条线索。
