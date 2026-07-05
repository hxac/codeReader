# 测量与采样：mz/mx/my 与 SampleResult

## 1. 本讲目标

本讲在「量子门与修饰符」（u2-l2）之后，正式回答一个反复被推迟的问题：**量子比特测出来之后，结果到底长什么样、怎么读？** 读完本讲，你应该能够：

1. 用 `mz / mx / my` 三个测量原语分别在 **Z / X / Y 基**下测量量子比特，并理解 `mx / my` 是「先做基变换、再用 `mz` 测」的实现，掌握背后的数学。
2. 理解 **shots（采样次数）** 的含义：`cudaq::sample` 默认把内核重复执行 `DEFAULT_NUM_SHOTS = 1000` 次，把每次的测量结果汇总成一个「比特串 → 出现次数」的分布；并搞清楚为什么「内核里没有任何测量」会触发 0-shot 警告。
3. 看懂 **`sample_result`（Python 侧叫 `SampleResult`）** 的数据结构：它内部是一个「寄存器名 → `ExecutionResult`」的映射，默认寄存器名是 `__global__`；学会用 `to_map / count / probability / most_probable / expectation / get_marginal / dump` 以及范围 `for` 把分布读出来。

本讲的实践任务是：测两组量子比特，分别读取两组的**边缘分布（marginal）**并打印——这是后续 `observe`（u3-l3）里「按子寄存器算期望值」的预演。

## 2. 前置知识

- **u1-l4 / u2-l1** 引入的量子类型与执行模型：`mz(q)` 和门函数一样，并不「立刻」得到一个经典值，而是把「测量事件」记录到全局单例 **ExecutionManager**，由后端在一次 shot 的末尾给出结果。本讲关注的不是「测量指令如何下发」，而是「测量结果如何回收与读取」。
- **计算基（Z 基）测量**：对一个量子比特 \(q\)，测它在 \(Z\) 算符的本征态 \(|0\rangle,|1\rangle\) 上的投影，得到 0 或 1。概率为

  \[
  P(0)=|\langle 0|\psi\rangle|^2,\qquad P(1)=|\langle 1|\psi\rangle|^2.
  \]

  这是 `mz` 的语义。`mx / my` 则是先把 X / Y 本征基「转」到 Z 基，再测 Z。
- **Hadamard 把 X 基转到 Z 基**：因为 \(H|+\rangle=|0\rangle,\ H|-\rangle=|1\rangle\)，其中 \(|\pm\rangle=(|0\rangle\pm|1\rangle)/\sqrt2\)。所以「测 X」=「先 H 再测 Z」。
- **parity（奇偶校验）**：一个比特串里 `1` 的个数的奇偶性。本讲会看到，全 Z 算符的期望值 \(\langle Z\otimes\cdots\otimes Z\rangle\) 正是「偶校验比特串的概率和 − 奇校验比特串的概率和」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `runtime/cudaq/qis/qubit_qis.h` | 测量原语 `mz / mx / my` 的全部声明，以及 `reset`、`measure(spin_op)`、`to_integer` 等辅助函数。`mx/my` 的「基变换 + mz」实现就在这里。 |
| `runtime/common/SampleResult.h` | 测量结果的**类型声明**：`ExecutionResult`（单个寄存器）与 `sample_result`（一次内核执行的全部寄存器结果），以及读取接口（`to_map / count / probability / expectation / get_marginal / dump / register_names / ...`）。 |
| `runtime/common/SampleResult.cpp` | 上述接口的**实现**，重点是 `expectation`（怎么由计数算 \(\langle Z\cdots Z\rangle\)）、`get_marginal`（怎么抽子寄存器）、`dump`（打印格式）。 |
| `runtime/cudaq/algorithms/sample/options.h` | `DEFAULT_NUM_SHOTS = 1000` 与 `sample_options`（含 `shots`、`explicit_measurements`）。 |
| `runtime/cudaq/algorithms/sample.h` | `cudaq::sample` 的模板定义与 shot 循环，包括「0 shots → 警告并跳出」的保护逻辑。 |
| `docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp` | 量子隐形传态示例，演示 `mz` 返回值参与条件分支，以及 `cudaq::run`（按 shot 收集返回值，区别于 `cudaq::sample`）。 |
| `runtime/cudaq/qis/measure_handle.h` | MLIR 模式下 `mz/mx/my` 的返回类型 `measure_handle`（一个延迟辨别的「测量句柄」）。 |
| `python/cudaq/qis/qis.py` | Python 端 `mz/my/mx`，带 `register_name` 形参——这是**命名寄存器**的官方入口。 |

## 4. 核心概念与源码讲解

### 4.1 测量原语 mz / mx / my 与测量基

#### 4.1.1 概念说明

CUDA-Q 的默认测量原语有三个，分别对应三个相互正交的测量基：

| 原语 | 测量基 | 物理含义 | 实现 |
|---|---|---|---|
| `mz(q)` | Z 基（计算基） | 直接区分 \(|0\rangle / |1\rangle\) | 直接测 |
| `mx(q)` | X 基 | 区分 \(|+\rangle / |-\rangle\) | 先 `h(q)` 再 `mz` |
| `my(q)` | Y 基 | 区分 \(|+i\rangle / |-i\rangle\) | 先 `r1(-π/2, q)` 再 `h(q)` 再 `mz` |

关键直觉：**真正会「测」的只有 `mz`**。`mx / my` 是在测量前插一段「基变换（basis change）」线路，把 X/Y 本征态旋转到 Z 本征态，再交给 `mz`。这是量子计算里非常通用的套路——后端硬件通常只支持 Z 基测量，其它基靠「先转再测」实现。

数学上：

\[
\text{测 }X:\quad H\text{ 后测 }Z,\qquad H|+\rangle=|0\rangle,\ H|-\rangle=|1\rangle.
\]

\[
\text{测 }Y:\quad S^{\dagger}\text{ 后 }H\text{ 后测 }Z,\qquad S^{\dagger}=r1(-\pi/2)=\begin{pmatrix}1&0\\0&-i\end{pmatrix}.
\]

源码里 `my` 用的正是 `r1(-M_PI_2)`（即 \(S^{\dagger}\)）再 `h`，与上式逐字对应。

#### 4.1.2 核心流程

以 `mz(q)` 为例，调用链很短：

1. `mz(qubit &q)` 命中 `qubit_qis.h` 里的内联函数。
2. **库模式（`CUDAQ_LIBRARY_MODE`）**：直接调用 `getExecutionManager()->measure(QuditInfo{...})`，由 ExecutionManager 交给当前后端，返回一个 `measure_result`。
3. **MLIR 模式（`nvq++` 的默认模式）**：函数体**根本不会执行**——编译器的 AST 桥会拦截内核里对 `mz/mx/my` 的每一次调用，直接生成 `quake.mz / quake.mx / quake.my` MLIR 操作。为了让「在主机域误调用」尽早暴露，这一支会抛 `kQpuOnlyHostScopeError`。

`mx / my` 多两步：先施加基变换门（`h`，或 `r1(-π/2); h`），最后一步与 `mz` 完全相同。注意：因为它们内部调用了门函数 `h / r1`，这些门会和 u2-l2 讲过的门一样被记录到 ExecutionManager，在 MLIR 模式下同样被桥翻成 `quake.h / quake.r1`。

测量「一批」比特：`mz(QubitRange &q)` 模板对一条寄存器（`qvector / qarray / qview`）逐个 `mz`，返回 `std::vector<measure_result>`；还有可变参数重载 `mz(q0, q1, q2)` 把多个比特/寄存器拼成一个结果向量。

#### 4.1.3 源码精读

三个测量原语与「MLIR 模式抛错」的注释集中在一处：

[runtime/cudaq/qis/qubit_qis.h:L426-L467](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L426-L467) —— `mz(q)` 直接 `measure`；`mx(q)` 先 `h(q)`；`my(q)` 先 `r1(-M_PI_2, q)` 再 `h(q)`。`#ifdef CUDAQ_LIBRARY_MODE` 分支说明：默认的 MLIR 模式下这些函数体不运行，桥会直接发 `quake.{mz,mx,my}`；只有在库模式下才走 C++ 函数体。

主机域误用的报错串：

[runtime/cudaq/qis/qubit_qis.h:L431-L434](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L431-L434) —— `kQpuOnlyHostScopeError` 明确写出「只在 `__qpu__` 内核里可用」。如果你在 `main()` 里直接 `mz(q)`（MLIR 模式），运行时会抛这个错。

「测一条寄存器」与「测多组」的重载：

[runtime/cudaq/qis/qubit_qis.h:L473-L519](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L473-L519) —— `mz(QubitRange&)` 逐比特 `mz`；可变参数版 `mz(q, qs...)` 递归拼接。这就是为什么 `mz(qvector)` 一次能把整条寄存器全测了。

MLIR 模式下 `mz` 返回的不是 `bool`，而是一个**测量句柄** `measure_handle`：

[runtime/cudaq/qis/measure_handle.h:L43-L69](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/measure_handle.h#L43-L69) —— 它在 IR 里降低为 `!cc.measure_handle`（最终是一个 `i64`）；对它的 `if (b)` 这类「隐式 bool」使用，桥会发 `quake.discriminate` 把句柄「辨别」成经典 0/1。这解释了为什么 `auto b = mz(q); if (b) x(r);` 能用来做「测量结果驱动的条件分支」。

#### 4.1.4 代码实践

**实践目标**：直观对比 Z / X / Y 三种基测量同一个态 \(|0\rangle\) 的结果分布。

**操作步骤**：

1. 新建 `bases.cpp`，写入下面的内核（示例代码）：

   ```cpp
   #include <cudaq.h>

   // basis: 0=Z, 1=X, 2=Y；始终从 |0> 出发
   __qpu__ void measure_basis(int basis) {
     cudaq::qubit q;
     // |0> 不做任何门
     if (basis == 1) mx(q);   // X 基
     else if (basis == 2) my(q); // Y 基
     else mz(q);              // Z 基
   }

   int main() {
     for (int b = 0; b < 3; b++) {
       auto counts = cudaq::sample(2000, measure_basis, b);
       const char *nm = b == 0 ? "Z" : (b == 1 ? "X" : "Y");
       printf("%s-basis on |0>: ", nm); counts.dump();
     }
   }
   ```

2. 编译运行：`nvq++ bases.cpp -o bases.x && ./bases.x`。

**需要观察的现象**：

- Z 基测 \(|0\rangle\)：几乎全 `0`。
- X 基测 \(|0\rangle\)：\(|0\rangle=(|+\rangle+|-\rangle)/\sqrt2\)，测 X 得 `0`（\(|+\rangle\)）与 `1`（\(|-\rangle\)）各约 50%。
- Y 基测 \(|0\rangle\)：\(|0\rangle=(|+i\rangle+|-i\rangle)/\sqrt2\)，同样约 50/50。

**预期结果**：Z 基接近全 `0`；X、Y 基近似 50/50。这就验证了 `mx/my = 基变换 + mz`。若你尚未跑通 `nvq++`（见 u1-l3），上述为「待本地验证」的理论值，可先按 \(P(0)=|\langle\text{基的 0 本征态}|\psi\rangle|^2\) 手算。

#### 4.1.5 小练习与答案

**练习 1**：把内核里 `qubit q;` 之后加一句 `h(q);`（即从 \(|+\rangle\) 出发），再分别 `mz/mx`，会看到什么？

**参考答案**：`h(q)` 后态为 \(|+\rangle\)。`mx` 测 X 基得 \(|+\rangle\)→几乎全 `0`（因为 \(|+\rangle\) 正是 X 的 +1 本征态）；`mz` 测 Z 基，\(|+\rangle=(|0\rangle+|1\rangle)/\sqrt2\)→约 50/50。这正说明 `mx` 测的是 X 本征基。

**练习 2**：为什么 `my` 里是 `r1(-M_PI_2)` 而不是 `r1(+M_PI_2)`？

**参考答案**：需要的是 \(S^{\dagger}\)。\(r1(\lambda)=|0\rangle\langle0|+e^{i\lambda}|1\rangle\langle1|\)，所以 \(r1(-\pi/2)=\text{diag}(1,-i)=S^{\dagger}\)。而 \(r1(+\pi/2)=S\)。把 Y 本征基转到 Z 基用的是 \(S^{\dagger}\)（再 H），因此取负角。若用错符号，测得的 0/1 含义会反过来。

---

### 4.2 shots 与采样分布

#### 4.2.1 概念说明

一次量子内核执行「测一下」只得到一串 0/1。但量子算法要的是**概率分布**，所以我们把内核重复跑很多次——每次叫一个 **shot**——再把所有 shot 的测量结果汇总成「比特串 → 出现次数」的计数表。这个计数表就是采样分布。

CUDA-Q 的关键约定：

- `cudaq::sample(kernel)` 不传次数时，默认跑 `DEFAULT_NUM_SHOTS = 1000` 次。
- 想指定次数，把次数放在第一个参数：`cudaq::sample(shots, kernel, args...)`。
- 汇总后的计数表类型是 `cudaq::sample_result`（见 4.3）。
- **内核里必须至少有一个测量**。如果一个 shot 跑完没有任何测量结果，本次就贡献 0 个有效结果；若一直 0，shot 循环会打印警告并跳出，避免死循环。

**`sample` vs `run` 的区别**（初学者极易混淆）：

- `cudaq::sample(...)`：返回 `sample_result`，把每个 shot 的测量比特串**汇总成计数表**。适合「看分布」。
- `cudaq::run(shots, kernel, args...)`：返回 `std::vector<返回类型>`，**逐 shot** 收集内核的 `return` 值。当内核 `return mz(q[2]);`（返回某个测量结果）时，`run` 给你「每个 shot 的那个返回值」列表。本讲示例 `mid_circuit_measurement.cpp` 用的就是 `run`。

一句话：要分布用 `sample`，要「逐 shot 的返回值」用 `run`。

#### 4.2.2 核心流程

`cudaq::sample` 的 shot 循环大致是：

1. 取 `sample_options.shots`（默认 1000）作为总次数。
2. 对 `i in [0, shots)`：执行一次内核 → 后端产出本次 shot 的 `sample_result`（一个或多个寄存器的计数，每条计数通常是 1 次出现）→ 累加到总结果上。
3. 累加用 `operator+=`，把同比特串的次数相加。
4. 每轮检查 `get_total_shots() == 0`：若是（本 shot 没产出任何测量），且没开 `explicit_measurements`，则打印警告并 `break`。

`sample_options` 里还有个 `explicit_measurements`：为 `true` 时按用户书写测量的顺序来拼装 `__global__` 寄存器（用于精确控制比特串里每位对应哪个 qubit）；默认 `false`。本讲实践会建议用「显式取边缘」来回避字节序问题。

#### 4.2.3 源码精读

默认采样次数与采样选项：

[runtime/cudaq/algorithms/sample/options.h:L14-L27](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample/options.h#L14-L27) —— `DEFAULT_NUM_SHOTS = 1000`；`sample_options { shots; noise; explicit_measurements; }`。

`sample` 的最常用重载（不传次数 → 默认 1000）：

[runtime/cudaq/algorithms/sample.h:L278-L280](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L278-L280) —— `sample(QuantumKernel&&, Args&&...)`；它内部把 `shots` 透传给采样策略，落点在：

[runtime/cudaq/algorithms/sample.h:L84-L85](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L84-L85) —— `policy.options.shots = shots;` 与 `explicit_measurements` 的赋值。

「0 shots 警告并跳出」的保护（解释了 u1-l4 里「内核必须含测量」的现象）：

[runtime/cudaq/algorithms/sample.h:L171-L180](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L171-L180) —— 当 `get_total_shots()==0` 时，若开了 `explicit_measurements` 直接抛错；否则 `printf("WARNING: ... 0 shots ...")` 并 `break`，跳出 shot 循环。

`run`（逐 shot 收集返回值）的真实用例：

[docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp:L33-L47](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/mid_circuit_measurement.cpp#L33-L47) —— `auto results = cudaq::run(nShots, kernel{});` 得到 `std::vector<bool>`（内核 `return mz(q[2])` 的逐 shot 结果），再数其中 `1` 的个数。对比可见：`run` 关心「每个 shot 的返回值」，而 `sample` 关心「比特串计数」。

#### 4.2.4 代码实践

**实践目标**：感受 shots 数对采样分布「涨落」的影响。

**操作步骤**：

1. 写内核（示例代码），制备 \(|+\rangle\) 单比特态并测 Z：

   ```cpp
   #include <cudaq.h>
   __qpu__ void plus_state() {
     cudaq::qubit q;
     h(q);
     mz(q);
   }
   int main() {
     for (int shots : {10, 100, 10000}) {
       auto c = cudaq::sample(shots, plus_state);
       printf("shots=%5d  ", shots); c.dump();
       printf("  P(0)=%.3f  P(1)=%.3f\n",
              c.probability("0"), c.probability("1"));
     }
   }
   ```

2. 编译运行：`nvq++ shots.cpp -o shots.x && ./shots.x`。

**需要观察的现象**：shots=10 时 `P(0)` 可能明显偏离 0.5（涨落大）；shots=10000 时 `P(0)` 非常接近 0.5。

**预期结果**：理论概率 \(P(0)=P(1)=0.5\)；样本量越大，经验概率越接近理论值（大数定律）。若把内核里的 `mz(q)` 删掉再跑，会看到 `WARNING: ... 0 shots ...`，印证 4.2.3 的保护逻辑。无运行环境时为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`cudaq::sample(kernel)` 与 `cudaq::sample(1000, kernel)` 行为有差别吗？

**参考答案**：没有差别。前者不传次数时使用 `DEFAULT_NUM_SHOTS = 1000`（见 [options.h:L14](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample/options.h#L14)），后者显式传 1000，二者等价。

**练习 2**：为什么内核里完全没有 `mz/mx/my` 时，`sample` 不会卡死，而是直接返回？

**参考答案**：因为 shot 循环在每轮检查 `get_total_shots() == 0`，发现本轮没有任何测量结果时，会打印 `WARNING` 并 `break`（见 [sample.h:L171-L180](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L171-L180)），避免在「永远产出 0 结果」的情况下无限循环。

---

### 4.3 sample_result / SampleResult 读取接口、寄存器与边缘分布

#### 4.3.1 概念说明

`cudaq::sample` 返回的 `cudaq::sample_result`（Python 端暴露为 `SampleResult`，文档里也常称 `MeasureCounts`）是本讲的「主角」。它的结构是两层：

- **外层 `sample_result`**：一张「**寄存器名 → `ExecutionResult`**」的映射，外加一个 `totalShots` 计数。
- **内层 `ExecutionResult`**：单个寄存器的计数表 `counts`（`unordered_map<string, size_t>`，即「比特串 → 出现次数」），可能附带一个预先算好的 `expectationValue`，以及顺序保留的 `sequentialData`。

为什么要有「寄存器名」这一层？因为一次内核执行可能测了**多组**比特（比如隐形传态里先测「Alice 的两个比特」，再测「Bob 的比特」），用户可能想分别看每组的结果。所有「没有特别命名」的测量默认归到一个叫 `__global__` 的寄存器里。

> ⚠️ **关于「命名寄存器」的准确事实**：在**当前版本**的 C++ QIS 里，`mz/mx/my` **不接受**寄存器名参数（见 4.1.3 引用的签名），因此 C++ 内核里的测量默认全部进入 `__global__`。要得到「按名字分开的多个寄存器」，最直接的做法是用 **Python** 端的 `mz(q, register_name="alice")`。C++ 端若想按比特子集分别统计，标准做法是用 `sample_result::get_marginal(indices)` 从 `__global__` 里**抽取边缘分布**。本讲的实践会两条路都走一遍。

#### 4.3.2 核心流程

读取一次采样结果的典型步骤：

1. `counts.dump()`：一眼看整体分布（打印格式见下）。
2. `counts.register_names()`：列出所有寄存器名（C++ 单测量内核通常只有 `__global__`）。
3. 对某个寄存器取计数表：`counts.to_map(regName)` 得到 `unordered_map<string,size_t>`；或直接范围 `for (auto &[bits, n] : counts)`（默认遍历 `__global__`）。
4. 点查询：`count(bits)`、`probability(bits)`、`most_probable()`。
5. 期望值：`expectation(regName)` 给出该寄存器的 \(\langle Z\cdots Z\rangle\)。
6. 边缘分布：`get_marginal({i0, i1, ...})` 抽出指定比特位上的子分布。

`dump()` 的打印格式（来自源码，可信）：

- **只有一个寄存器**（最常见，即只有 `__global__`）：`{ 00:250 11:250 ... }`，按比特串字典序排序。
- **有多个寄存器**：嵌套形式 `{ regA : { 00:.. } regB : { 1:.. } }`。

`expectation` 的算法：对每个比特串，偶校验取 \(+p\)、奇校验取 \(-p\)，求和——即

\[
\langle Z\otimes\cdots\otimes Z\rangle
= \sum_{b:\,\text{even}} P(b) \;-\; \sum_{b:\,\text{odd}} P(b).
\]

`get_marginal` 的算法：对原计数表里每个比特串，挑出指定位置上的字符拼成新比特串，**保留原计数**（注意：是被抽中位置之外的比特被「求和掉」，所以新分布的总 shots 不变）。

#### 4.3.3 源码精读

默认寄存器名常量：

[runtime/common/SampleResult.h:L21-L21](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.h#L21-L21) —— `GlobalRegisterName = "__global__"`。所有读取接口的 `registerName` 形参都默认取它。

`ExecutionResult` 的字段（单寄存器的计数 + 可选期望值 + 寄存器名 + 顺序数据）：

[runtime/common/SampleResult.h:L28-L99](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.h#L28-L99) —— `counts`、`expectationValue`、`registerName`、`sequentialData`；`appendResult(bitString, count)` 用于累加。

`sample_result` 的读取接口（本模块的「API 目录」）：

[runtime/common/SampleResult.h:L188-L211](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.h#L188-L211) —— `expectation(regName)`、`probability(bitString, regName)`、`most_probable(regName)`、`count(bitString, regName)`。

[runtime/common/SampleResult.h:L234-L262](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.h#L234-L262) —— `to_map(regName)`、`get_marginal(indices, regName)`（含 `std::vector<size_t>&&` 的 rvalue 重载）、`reorder(index, regName)`。

范围 `for` 的迭代器与 `register_names / size / dump / clear / get_total_shots / has_even_parity`：

[runtime/common/SampleResult.h:L264-L294](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.h#L264-L294) —— `begin()/end()` 等迭代器默认绑定 `__global__`；这就是 `for (auto &[bits,count] : counts)` 能直接工作的原因。

`expectation` 的实现（偶校验 +p、奇校验 −p）：

[runtime/common/SampleResult.cpp:L388-L408](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.cpp#L388-L408) —— 若该寄存器有预算好的 `expectationValue` 直接返回；否则用 `has_even_parity` 与 `probability` 现算。`has_even_parity` 的定义见 [SampleResult.cpp:L508-L511](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.cpp#L508-L511)：数 `1` 的个数模 2。

`get_marginal` 的实现（按位挑字符、保留计数）：

[runtime/common/SampleResult.cpp:L425-L449](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.cpp#L425-L449) —— 关键行 `newBits[counter++] = bits[index];`：`index` 是**比特串里的位置**，不是 qubit 编号。越界会抛 `Invalid marginal index`。

`dump` 的打印格式（单寄存器 vs 多寄存器）：

[runtime/common/SampleResult.cpp:L469-L504](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.cpp#L469-L504) —— 单寄存器平铺 `{ bits:count ... }`；多寄存器嵌套 `{ name : { bits:count ... } ... }`；均按键排序。

Python 端「命名寄存器」的官方入口：

[python/cudaq/qis/qis.py:L185-L203](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/qis/qis.py#L185-L203) —— `mz(*args, register_name='')`、`my(...)`、`mx(...)` 都带 `register_name` 形参（这些是类型桩，真实逻辑在 AST 桥，见 u5-l2）。

#### 4.3.4 代码实践

**实践目标**：测两组比特（一组 2 个、一组 1 个），分别读出两组的边缘分布。先用 C++ 的 `get_marginal`（从 `__global__` 抽），再用 Python 的 `register_name`（直接命名）。

**操作步骤（C++，用 `get_marginal`）**：

```cpp
// 示例代码：two_groups.cpp
#include <cudaq.h>

// 制备 q0=|1>, q1 与 q2 做 Bell 对；全部测到 __global__，3 位比特串
__qpu__ void two_groups() {
  cudaq::qvector q(3);
  x(q[0]);                       // q0 -> |1>
  h(q[1]);                       // Bell pair (q1,q2)
  x<cudaq::ctrl>(q[1], q[2]);
  mz(q);
}

int main() {
  auto counts = cudaq::sample(2000, two_groups);
  counts.dump();
  printf("registers: ");
  for (auto &r : counts.register_names()) printf("%s ", r.c_str());
  printf("\n");

  // 第 0 位是 q0（应恒为 1）；第 {1,2} 位是 Bell 对（应 50/50 的 00/11）
  auto m0 = counts.get_marginal({0});
  auto m12 = counts.get_marginal({1, 2});
  printf("group q0      : "); m0.dump();
  printf("group q1,q2   : "); m12.dump();
}
```

编译运行：`nvq++ two_groups.cpp -o two_groups.x && ./two_groups.x`。

**操作步骤（Python，用 `register_name`）**：

```python
# 示例代码：two_groups.py
import cudaq

@cudaq.kernel
def two_groups():
        q = cudaq.qvector(3)
        x(q[0])
        h(q[1])
        x.ctrl(q[1], q[2])
        mz(q[0], register_name="solo")     # 单独命名
        mz(q[1], q[2], register_name="pair")  # 一组命名

counts = cudaq.sample(two_groups, shots_count=2000)
print(counts)                       # 多寄存器嵌套打印
for name in counts.register_names():
        print(name, counts.dump(name) if hasattr(counts, "dump") else counts.to_map(name))
```

运行：`python two_groups.py`（或在已装 cudaq 的环境里）。

**需要观察的现象**：

- C++ 版：`group q0` 应几乎只有 `1`；`group q1,q2` 应约 50/50 出现 `00` 与 `11`。
- Python 版：能看到两个独立寄存器 `solo` 与 `pair`，各自的分布与上面对应。

**预期结果**：`q0` 恒为 1（因为 `x(q[0])`）；Bell 对在计算基下只在 `00`、`11` 上有概率。边缘分布正是「把不关心的比特求和掉」后的结果。

> ⚠️ **关于比特串里「第 i 位」对应哪个 qubit**：`get_marginal({i})` 抽的是**比特串字符串里的第 `i` 个字符**（见 `newBits[counter++] = bits[index]`）。这个位置到 qubit 编号的映射取决于测量顺序与 `explicit_measurements` 设置，不同后端/版本可能不同。**强烈建议先 `dump()` 看实际比特串，再决定要取哪几位**；必要时打开 `sample_options.explicit_measurements = true`（Python `sample(..., explicit_measurements=True)`）按书写顺序锁定每位。具体端序以你本地 `dump()` 输出为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：对一个只在 \(|0\rangle,|1\rangle\) 上有概率的单比特内核，`counts.expectation()` 应该是多少？它与 `probability("0") - probability("1")` 有什么关系？

**参考答案**：单比特的 \(\langle Z\rangle = (+1)P(0) + (-1)P(1) = P(0) - P(1)\)。所以 `expectation()` 正好等于 `probability("0") - probability("1")`。这正是 [SampleResult.cpp:L388-L408](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.cpp#L388-L408) 的算法在单比特情形下的化简。

**练习 2**：为什么 `get_marginal({0,2})` 抽出的边缘分布，其 `total_shots` 与原分布相等，而不是变小？

**参考答案**：因为 `get_marginal` 只是把每个原始比特串「投影」到指定几位（把不关心的位求和掉），计数 `count` 原封不动地搬到新比特串上（见 [SampleResult.cpp:L445](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.cpp#L445) 的 `sr.appendResult(newBits, count)`）。所以总样本数不变，变的只是「被区分的比特组合数」。

---

## 5. 综合实践：测两组比特并分别读取边缘分布

**任务**：构造一个内核，让前两个比特 \(q_0,q_1\) 处于确定态 \(|11\rangle\)，后两个比特 \(q_2,q_3\) 处于 Bell 态 \((|00\rangle+|11\rangle)/\sqrt2\)。整体采样后，**分别**得到 \(\{q_0,q_1\}\) 与 \(\{q_2,q_3\}\) 这两组的边缘分布，并打印每组各自的全 Z 期望值。

**C++ 实现骨架**（示例代码）：

```cpp
#include <cudaq.h>

__qpu__ void mixed_groups() {
  cudaq::qvector q(4);
  x(q[0]); x(q[1]);              // q0,q1 -> |11>
  h(q[2]);                       // q2,q3 Bell
  x<cudaq::ctrl>(q[2], q[3]);
  mz(q);
}

int main() {
  auto counts = cudaq::sample(4000, mixed_groups);
  printf("full: "); counts.dump();

  // 先 dump 出原始比特串，确认每位对应哪个 qubit 后再选定 index
  auto gA = counts.get_marginal(/*填 {q0位,q1位} */);
  auto gB = counts.get_marginal(/*填 {q2位,q3位} */);
  printf("group A (q0,q1): "); gA.dump();
  printf("group B (q2,q3): "); gB.dump();
  printf("<ZZ>_A = %.3f   <ZZ>_B = %.3f\n",
         gA.expectation(), gB.expectation());
}
```

**验证步骤**：

1. 编译运行：`nvq++ groups.cpp -o groups.x && ./groups.x`。
2. 先看 `full` 的比特串，确定 `q0..q3` 各自落在比特串的第几位（必要时用 `explicit_measurements`）。
3. 把 `get_marginal` 的索引填对，再读 `gA / gB`。

**预期结果**：

- 组 A（\(|11\rangle\)）：边缘分布只在 `11` 上，\(\langle ZZ\rangle_A = +1\)。
- 组 B（Bell \((|00\rangle+|11\rangle)/\sqrt2\)）：边缘分布约 50% `00` + 50% `11`。两个比特串都是偶校验，所以 \(\langle ZZ\rangle_B = (+1)\cdot 0.5 + (+1)\cdot 0.5 = +1\)。

> 这里的 \(\langle ZZ\rangle\) 计算直接对应 [SampleResult.cpp:L388-L408](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.cpp#L388-L408)：偶校验贡献 \(+p\)、奇校验贡献 \(-p\)。`00`、`11` 都是偶校验，故期望值为正。

**延伸思考**：把组 B 换成 \((|01\rangle+|10\rangle)/\sqrt2\)（提示：在 Bell 对制备后对 `q2` 加一个 `x`），\(\langle ZZ\rangle_B\) 会变成多少？（答案：两个比特串 `01`、`10` 都是奇校验，期望值变为 \(-1\)。）这正是「按子寄存器算期望」的雏形——下一单元的 `cudaq::observe`（u3-l3）会把它系统化。

## 6. 本讲小结

- 测量原语只有 `mz`（Z 基）真正「测」；`mx`（X 基）= `h` + `mz`，`my`（Y 基）= `r1(-π/2)` + `h` + `mz`，对应数学上的基变换 \(H\) 与 \(S^{\dagger}H\)。
- 在 MLIR 模式（`nvq++` 默认）下，`mz/mx/my` 的 C++ 函数体不运行——AST 桥直接发 `quake.{mz,mx,my}`；返回的是测量句柄 `measure_handle`，对其做 `if (b)` 会发 `quake.discriminate`。
- `cudaq::sample(kernel)` 默认跑 `DEFAULT_NUM_SHOTS = 1000` 个 shot，把每个 shot 的测量比特串汇总成计数表 `sample_result`；内核里没有任何测量会触发 `WARNING: ... 0 shots ...` 并跳出。
- `sample` 返回**计数表**用于看分布；`run` 返回**逐 shot 的内核返回值**——隐形传态示例用的就是 `run`。
- `sample_result` 是「寄存器名 → `ExecutionResult`」的映射，默认寄存器名 `__global__`。读取靠 `dump / to_map / count / probability / most_probable / expectation / register_names`，以及范围 `for`。
- 当前版本 C++ 的 `mz` 不带寄存器名，命名寄存器走 Python 的 `mz(register_name=...)`；C++ 按子集分别统计用 `get_marginal(indices)`，它按**比特串位置**抽字符、保留计数。
- 全 Z 期望值 \(\langle Z\cdots Z\rangle = \sum_{\text{even}}P - \sum_{\text{odd}}P\)，由 `has_even_parity` 与 `probability` 现算（除非后端预算好了 `expectationValue`）。

## 7. 下一步学习建议

- **u2-l4（内核组合与参数传递）**：本讲的测量结果常被传给后续逻辑或作为返回值，下一讲系统讲内核嵌套、参数合成（`ArgumentSynthesis`），含「测量结果如何在内核间流转」。
- **u2-l5（线路中途测量与条件执行）**：想深入 `if (b) x(r)` 这类「测量驱动条件分支」的语义与各后端差异，下一讲专门讲。
- **u3-l1 / u3-l3（执行模型与 observe）**：本讲的 `expectation` / `get_marginal` 是 `cudaq::observe`（按 spin_op 术语算期望值）的基石；想看「测量结果如何被 ExecutionContext 承载并分发到后端」，进入第 3 单元。
- **想看 `quake.mz` 在编译器侧如何定义**：可跳到 **u4-l2（Quake 方言）**，对照本讲的运行时行为理解 IR 语义。
