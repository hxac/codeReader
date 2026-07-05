# cudaq::observe 与 spin_op 期望值

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `cudaq::spin_op` 的代数运算（`+`、`*`、标量）从零拼装一个泡利字符串形式的哈密顿量 \(H\)，并理解 `spin_op` / `spin_op_term` 的类型层次。
- 说清一次 `cudaq::observe(kernel, H, args...)` 调用内部的完整流程：把 \(H\) 规范化、逐项求期望、按系数加权求和、封装成 `observe_result`。
- 解释「测量基变换」为什么必要——把不可直接测量的 \(X\)、\(Y\) 项旋转回 \(Z\) 基再测量；以及「术语合成」如何把一组泡利项归并为一个 `spin_op`、又如何在结果里按项拆回来。
- 理解 `canHandleObserve` 这个能力位如何让后端在「逐项采样」与「整算符矩阵直算」两条路径之间分叉，以及对性能（门数量/电路次数）的影响。

本讲承接 u3-l1 的执行模型（`ExecutionContext`、`quantum_platform`、分发链路），把「采样」升级为「求期望值」，是后续 VQE/优化（u3-l4）与算符代数（u3-l5）的基础。

## 2. 前置知识

- **期望值**：对于一个量子态 \(|\psi\rangle$ 和一个可观测量（算符）\(H\)，期望值定义为 \(\langle H\rangle = \langle\psi|H|\psi\rangle$。它是一个实数（当 \(H$ 厄米时），是 VQE 等变分算法里反复计算的「能量」。
- **泡利矩阵**：四个 \(2\times2$ 矩阵 \(I, X, Y, Z$。任意多比特哈密顿量都可以写成「泡利字符串的加权和」：\(H = \sum_k c_k\, P_k$，其中 \(P_k = \sigma_{k_0}\otimes\sigma_{k_1}\otimes\cdots$，每个 \(\sigma\in\{I,X,Y,Z\}$。
- **为什么只测 \(Z$**：真实硬件（和默认模拟器）原生只支持在计算基（\(Z$ 基）下测量。要得到 \(\langle X\rangle$ 或 \(\langle Y\rangle$，需要先做一次「基变换」把 \(X$、\(Y$ 转成 \(Z$，再测 \(Z$。这正是本讲的核心机制之一。
- **shot 与采样**：u2-l3 已讲过 `cudaq::sample` 的 shot 概念。本讲的 `observe` 内部会复用采样机制来估计每一项的期望值。
- **执行上下文**：u3-l1 讲过的 `ExecutionContext` 在本讲里会以 `name == "observe"` 的形态出现，承装 spin 算符、shots、噪声模型等「执行意图」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/sphinx/examples/cpp/basics/expectation_values.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/expectation_values.cpp) | 最小可运行示例：定义 ansatz 内核、用代数式构造 `spin_op`、调用 `observe` 求能量。 |
| [runtime/cudaq/spin_op.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/spin_op.h) | `spin_handler`（单个泡利算子的句柄）与 `cudaq::spin::x/y/z` 工厂函数的声明。 |
| [runtime/cudaq/operators.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h) | 类型别名 `spin_op = sum_op<spin_handler>`、`spin_op_term = product_op<spin_handler>`，以及 `x/y/z/empty/canonicalize/distribute_terms` 等接口。 |
| [runtime/cudaq/algorithms/observe.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe.h) | `cudaq::observe` 全部重载、`observePreamble`、`runObservation`、多 QPU 的 `distributeComputations`。 |
| [runtime/cudaq/algorithms/observe/policy.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe/policy.h) | `observe_policy`（名字串 `"observe"`、结果类型 `observe_result`、能力位 `canHandleObserve`）。 |
| [runtime/common/ObserveResult.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ObserveResult.h) | `observe_result` 类：全局期望值、按项期望值、`operator double()` 等。 |
| [runtime/nvqir/CircuitSimulator.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h) | 后端侧的 `finalizeExecutionContext(observe_policy)` 逐项循环，以及 `measureSpinOp` 的基变换实现。 |
| [runtime/cudaq/operators/sum_op.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/sum_op.cpp) | `canonicalize`、`distribute_terms` 的实现，解释项如何被规范化与切分。 |

> 提示：`spin_op.h` 文件名里只有 `spin_handler` 的声明；`spin_op` 这个「求和算符」类型其实是模板 `sum_op<spin_handler>` 的别名，定义在 `operators.h` 里。读源码时把两者对照看才不会迷路。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

1. **spin_op 代数构造**：泡利算符句柄、求和/乘积/标量运算、规范化。
2. **observe 期望值流程**：从 `cudaq::observe` 入口到后端逐项求和的完整链路。
3. **测量基变换与术语合成**：基变换的数学与代码、项的归并与拆分、能力位分流。

---

### 4.1 spin_op 代数构造

#### 4.1.1 概念说明

化学/物理里的哈密顿量几乎总是「一堆泡利字符串的加权和」。CUDA-Q 用一个统一的算符类型体系来表达它：

- **句柄（handler）**：最底层的单元。`spin_handler` 描述「在第 `target` 号比特上的一个 \(I/X/Y/Z$」。
- **乘积项 `product_op<spin_handler>`**：若干个 `spin_handler` 的张量积，外加一个复系数。这正是「一个泡利字符串」。CUDA-Q 给它起了别名 **`spin_op_term`**。
- **求和算符 `sum_op<spin_handler>`**：一组 `product_op` 的线性组合。这就是「完整的哈密顿量」。别名 **`spin_op`**。

两个别名写在 [runtime/cudaq/operators.h:1896-1900](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L1896-L1900)：

```cpp
typedef sum_op<spin_handler> spin_op;
// This typedef defines spin_op_term as a product_op with a spin_handler,
typedef product_op<spin_handler> spin_op_term;
```

这套层次的好处是：**你写出来的代数式会被自动归约到正确的类型**。例如 `spin_op::x(0) * spin_op::x(1)`（两个乘积项相乘）结果是单个 `product_op`（一个 2 比特泡利串）；再和标量 `5.907` 相加，就提升成 `sum_op`（即 `spin_op`）。

> 名词解释：
> - **泡利字符串（Pauli string）**：形如 \(X_0 Y_1 Z_2$ 的张量积，作用在若干比特上，是哈密顿量里的一项。
> - **`degree` / `target`**：算符作用的比特编号，源码里这两个词混用，含义相同。
> - **`canonicalize`（规范化）**：把一个 `spin_op` 整理成「标准形」——合并相同项、消去项内冗余的 \(I$ 因子、按 degree 排序——便于后续按项测量与比较。

#### 4.1.2 核心流程

构造一个哈密顿量的典型流程是：

1. 用静态工厂 `spin_op::x(n)` / `y(n)` / `z(n)` 造出单比特泡利算符（每个返回一个 `product_op<spin_handler>`）。
2. 用 `*` 把若干单比特算符合成一个多比特泡利串（仍是 `product_op`）。
3. 用标量 `*` 给每项配系数，用 `+` / `-` 把所有项加起来，得到 `sum_op<spin_handler>` = `spin_op`。
4. （可选）调用 `canonicalize()` 规范化。

工厂函数声明在 [runtime/cudaq/operators.h:723-730](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L723-L730)（`HANDLER_SPECIFIC_TEMPLATE(spin_handler)` 保证它们只对自旋算符实例化），底层每个工厂最终创建一个 `spin_handler(pauli, target)`，其中泡利枚举定义在 [runtime/cudaq/spin_op.h:21](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/spin_op.h#L21)：

```cpp
enum class pauli { I, X, Y, Z };
```

`canonicalize` 与 `distribute_terms` 的接口在 [runtime/cudaq/operators.h:779-792](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L779-L792)。其中 `canonicalize` 的实现（[runtime/cudaq/operators/sum_op.cpp:1558-1602](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/sum_op.cpp#L1558-L1602)）做的事情是：收集所有 degree，对每个乘积项调用 `product_op::canonicalize(degrees)` 让每项都作用在统一的比特集合上、内部排序去重，再插回新的 `sum_op`。

#### 4.1.3 源码精读

最直观的构造写法来自官方示例 [docs/sphinx/examples/cpp/basics/expectation_values.cpp:24-27](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/expectation_values.cpp#L24-L27)：

```cpp
// Build up your spin op algebraically
cudaq::spin_op h =
    5.907 - 2.1433 * cudaq::spin_op::x(0) * cudaq::spin_op::x(1) -
    2.1433 * cudaq::spin_op::y(0) * cudaq::spin_op::y(1) +
    .21829 * cudaq::spin_op::z(0) - 6.125 * cudaq::spin_op::z(1);
```

这段代码用一行人类可读的代数式构造了：

\[
H = 5.907\,I - 2.1433\,X_0X_1 - 2.1433\,Y_0Y_1 + 0.21829\,Z_0 - 6.125\,Z_1
\]

它就是一个 2 比特的分子哈密顿量（N₂ 类似模型的简化形式），共 5 项。常量项 `5.907` 会被包成 `scalar_operator`，通过 `operator+(scalar, sum_op)` 等友元重载（声明于 [runtime/cudaq/operators.h:594-602](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators.h#L594-L602)）提升为 `spin_op`。

> 类型提示：`spin_op::x(0)` 返回 `product_op<spin_handler>`；`x(0)*x(1)` 仍是 `product_op`；`2.1433 * x(0)*x(1)` 是 `product_op`（带系数）；最后整行的 `+`/`-` 把所有 `product_op` 与常量汇成一个 `sum_op<spin_handler>`，正好能赋值给 `cudaq::spin_op h`。

#### 4.1.4 代码实践

**目标**：亲手用代数运算拼一个 `spin_op`，并观察规范化前后项数与字符串表示的变化。

**步骤**：

1. 复制 `expectation_values.cpp` 为 `spin_op_explore.cpp`，把 `main` 改成只构造 `h` 并打印：

```cpp
// 示例代码：仅用于探索 spin_op 的构造与规范化
#include <cudaq.h>
#include <cudaq/algorithm.h>

int main() {
  cudaq::spin_op h =
      5.907 - 2.1433 * cudaq::spin_op::x(0) * cudaq::spin_op::x(1) -
      2.1433 * cudaq::spin_op::y(0) * cudaq::spin_op::y(1) +
      .21829 * cudaq::spin_op::z(0) - 6.125 * cudaq::spin_op::z(1);

  printf("项数 = %zu\n", h.num_terms());
  h.dump();                       // 打印规范化前的字符串表示
  // h.canonicalize();            // 取消注释观察规范化后的差异
  // printf("规范化后项数 = %zu\n", h.num_terms());
  return 0;
}
```

2. 编译运行：`nvq++ spin_op_explore.cpp -o spin_op_explore.x && ./spin_op_explore.x`。

**需要观察的现象**：

- `num_terms()` 报告的项数（预期为 5）。
- `dump()` 打印的字符串里每一项的泡利串与系数。

**预期结果**：能正确打印 5 项及对应泡利串。规范化（`canonicalize`）对该式不会减少项数（因为没有可合并的相同泡利串），但会统一每项作用到的 degree 集合并排序——这正是后续逐项测量所依赖的标准形。**待本地验证** `dump()` 的确切字符串格式与 `num_terms()` 的返回值。

#### 4.1.5 小练习与答案

**练习 1**：把上面 `h` 里的 `+ .21829 * z(0)` 再额外加一份 `+ 0.5 * z(0)`，项数会变成 6 吗？

**参考答案**：不一定会。`z(0)` 与 `z(0)` 是同一个泡利串，`canonicalize`（或构造时的内部归并）会把它们合并成 `(0.21829 + 0.5) * z(0)`，因此规范化后仍是 5 项。这正体现了规范化「合并相同项」的作用。

**练习 2**：`cudaq::spin_op::x(0) * cudaq::spin_op::x(1)` 与 `cudaq::spin_op::x(1) * cudaq::spin_op::x(0)` 在规范化后相等吗？

**参考答案**：相等。不同比特上的泡利算符彼此可交换，规范化会按 degree 排序，两者化成同一个标准形、同一个 `get_term_id()`。

---

### 4.2 observe 期望值流程

#### 4.2.1 概念说明

`cudaq::observe(kernel, H, args...)` 的任务是计算 \(\langle\psi(\text{args})|H|\psi(\text{args})\rangle$，其中 \(|\psi(\text{args})\rangle$ 是 `kernel(args...)` 制备的态。它的关键认知是：**期望值不是一次测量得到的，而是「逐项求和」拼出来的**：

\[
\langle H\rangle = \sum_k c_k\,\langle\psi|P_k|\psi\rangle
\]

每一项 \(\langle\psi|P_k|\psi\rangle$ 需要单独估计（通过基变换 + 采样，或矩阵直算），乘以系数 \(c_k$，最后把所有项加起来。常量项（纯 \(I$ 项）贡献就是它的系数本身，无需任何测量。

这与 u3-l1 的执行模型完全对接：`observe` 仍走 `launch` → 平台 → 后端，只不过 `ExecutionContext` 的 `name` 这次是 `"observe"`，并多携带一个 `spin` 字段（规范化后的 \(H$）。

#### 4.2.2 核心流程

一次 `cudaq::observe(kernel, H, args...)` 的内部链路：

1. **入口重载**（[observe.h:197-207](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe.h#L197-L207)）：取平台、取内核名，把内核包成 lambda，转交 `detail::runObservation(..., shots=-1)`。
2. **`runObservation`**（[observe.h:94-115](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe.h#L94-L115)）：构造 `ExecutionContext("observe", shots, qpu_id)` 与 `observe_policy`，调 `observePreamble` 填充它们，再 `detail::launch(...)`。
3. **`observePreamble`**（[observe.h:56-84](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe.h#L56-L84)）：最关键的一步——`ctx.spin = cudaq::spin_op::canonicalize(H)`，即**把用户传入的 \(H$ 规范化后塞进执行上下文**；同时把 shots、噪声模型、内核名同步到 `policy` 与 `ctx`。
4. **`launch`**（u3-l1 讲过）：库模式下走 `ExecutionManager::with_default_em`，执行内核并落入后端。
5. **后端终结**（[CircuitSimulator.h:1118-1143](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L1118-L1143)）：遍历 `H` 的每一项，非 \(I$ 项调 `measureSpinOp(term)` 求该项期望，乘系数累加；\(I$ 项直接累加系数。最后封装成 `observe_result(sum, H, data)`。

伪代码：

```
observe(kernel, H, args):
    ctx  = ExecutionContext("observe", shots)
    ctx.spin = canonicalize(H)          # 关键：规范化
    launch(ctx, policy,  kernel(args))  # 跑内核，把门/测量灌进后端
    return 后端.finalize(policy)        # 逐项求期望并求和
```

#### 4.2.3 源码精读

入口重载 [runtime/cudaq/algorithms/observe.h:197-207](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe.h#L197-L207)——注意 `shots=-1` 表示「不指定 shot 数」，交由后端决定走矩阵直算还是采样：

```cpp
template <typename QuantumKernel, typename... Args>
  requires ObserveCallValid<QuantumKernel, Args...>
observe_result observe(QuantumKernel &&kernel, const spin_op &H,
                       Args &&...args) {
  auto &platform = cudaq::get_platform();
  auto kernelName = cudaq::getKernelName(kernel);
  return detail::runObservation(
      [&kernel, &args...]() mutable { kernel(std::forward<Args>(args)...); }, H,
      platform, /*shots=*/-1, kernelName);
}
```

`observePreamble` 里把 \(H$ 规范化写进上下文（[observe.h:63-64](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe.h#L63-L64)）：

```cpp
ctx.kernelName = kernelName;
ctx.spin = cudaq::spin_op::canonicalize(H);  # 规范化后的 H 进入上下文
```

后端终结的逐项循环（[CircuitSimulator.h:1130-1142](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L1130-L1142)）是整个流程的「心脏」：

```cpp
double sum = 0.0;
for (const auto &term : H) {
  if (term.is_identity())
    sum += term.evaluate_coefficient().real();      // 常量项：直接取系数
  else {
    auto [exp, data] = measureSpinOp(term);          // 非常量项：基变换+测Z
    results.emplace_back(data.to_map(), term.get_term_id(), exp);
    sum += term.evaluate_coefficient().real() * exp; // 加权累加
  }
}
```

`policy.spin`（即规范化后的 \(H$）由 `observe_policy` 承载，定义在 [runtime/cudaq/algorithms/observe/policy.h:22-50](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe/policy.h#L22-L50)：`name[] = "observe"`、`result_type = observe_result`，并带一个能力位 `canHandleObserve`（4.3 节展开）。

结果对象 `observe_result`（[runtime/common/ObserveResult.h:21-115](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ObserveResult.h#L21-L115)）持有全局期望值 `expVal`、原始 `spin_op` 与每个项的采样数据 `sample_result data`。它最重要的两个接口是：

- `operator double()`（[ObserveResult.h:60](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ObserveResult.h#L60)）：让 `double e = cudaq::observe(...)` 直接拿到能量。
- `expectation(spin_op_term)`（[ObserveResult.h:68-71](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ObserveResult.h#L68-L71)）：取某一**项**的期望值，靠 `term.get_term_id()` 在 `data` 里查表。

#### 4.2.4 代码实践

**目标**：跑通官方示例，观察 `observe` 返回的能量；并学会从 `observe_result` 取「某一项」的期望。

**步骤**：

1. 编译运行 [expectation_values.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/expectation_values.cpp)：

```bash
nvq++ docs/sphinx/examples/cpp/basics/expectation_values.cpp -o d2.x && ./d2.x
```

2. 把 `main` 里这行（[expectation_values.cpp:31](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/expectation_values.cpp#L31)）展开，取回 `observe_result` 并查询某项：

```cpp
// 示例代码：展开 observe 返回值，演示按项查询
cudaq::observe_result res = cudaq::observe(ansatz{}, h, .59);
double energy = res.expectation();                       // 全局 <H>
double xx     = res.expectation(cudaq::spin_op::x(0) *
                                cudaq::spin_op::x(1));   // 单项 <X0X1>
printf("Energy=%lf, <X0X1>=%lf\n", energy, xx);
```

**需要观察的现象**：

- `Energy is ...` 打印的能量值。
- `energy` 与 `xx` 的数值；注意 \(X_0X_1$ 项系数为 \(-2.1433$，验证 `energy` 是否包含该项贡献。

**预期结果**：程序正常打印能量（一个负数，量级在 \(-1$ 到 \(-2$ 之间，因为这是分子基态附近的能量）。**待本地验证**精确数值——它取决于 ansatz 的参数 `0.59` 与默认的 qpp 后端精度。在 `shots=-1`、qpp 后端下，因为 `canHandleObserve()` 为真（见 4.3），能量是矩阵精确直算而非采样估计。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `runObservation` 里要构造 `ExecutionContext("observe", shots, qpu_id)`，而不是复用 `"sample"` 上下文？

**参考答案**：上下文的 `name` 决定了「终结阶段如何处理结果」。`"sample"` 上下文在终结时直接汇总比特串计数；`"observe"` 上下文则触发后端的 `finalizeExecutionContext(observe_policy)`，进入「逐项基变换+求期望+加权求和」的路径（[CircuitSimulator.h:1118](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L1118)）。名字串是分派终结逻辑的钥匙。

**练习 2**：如果哈密顿量里有一项是纯常数 `3.0`（即 \(3.0\,I$），后端会对它做什么？

**参考答案**：在逐项循环里命中 `term.is_identity()` 分支，**不调用 `measureSpinOp`、不做任何基变换或采样**，直接把系数 `3.0` 加进 `sum`（[CircuitSimulator.h:1132-1133](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L1132-L1133)）。`observe_result::id_coefficient()`（[ObserveResult.h:104-109](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/ObserveResult.h#L104-L109)）可专门取出这个常数项系数。

---

### 4.3 测量基变换与术语合成

#### 4.3.1 概念说明

本模块回答两个紧密相关的问题：

**问题 A——测量基变换**：硬件只能测 \(Z$。可哈密顿量里满是 \(X$、\(Y$。怎么办？
答案是：测量前在目标比特上插入一个「基变换门」\(U$，使得 \(P = U^\dagger Z U$，于是 \(\langle P\rangle = \langle\psi|U^\dagger Z U|\psi\rangle = \langle\varphi|Z|\varphi\rangle$（其中 \(|\varphi\rangle=U|\psi\rangle$），即**变换后测 \(Z$ 就等价于测原来的 \(P$**。具体：

- 对 \(X$：因为 \(X = HZH$，所以 \(U = H$，**测 \(X$ 前加一个 \(H$ 门**。
- 对 \(Y$：利用旋转恒等式 \(Y = R_x^\dagger(\pi/2)\,Z\,R_x(\pi/2)$，所以 \(U = R_x(\pi/2)$，**测 \(Y$ 前加一个 \(R_x(\pi/2)$ 门**（代码里即 `rx(π/2)`）。

**问题 B——术语合成**：有时你不是给一个现成的 `spin_op`，而是给「一串独立的泡利项」（一个容器）。`observe` 提供了一个重载，把这些项**合成**为一个 `spin_op` 再统一测量；测完后又把结果**按项拆回**成向量。这种「项的归并与拆分」就是术语合成（term synthesis）。

> 注意：本讲不要求你已学过 Jordan–Wigner 等映射（那是 u3-l5 算符代数的事）。这里只关心「拿到一个泡利字符串形式的 \(H$ 之后怎么测」。

#### 4.3.2 核心流程

**基变换发生在后端的 `measureSpinOp(term)` 里**，对单个非常量项执行：

1. 遍历该项的每个泡利算子，收集需要测量的比特（跳过 \(I$），并为每个 \(X$ 比特准备一个 `h`、每个 \(Y$ 比特准备一个 `rx(π/2)`。
2. 正向施加全部基变换门，刷新门队列。
3. 对收集到的比特做 `sample`（在 \(Z$ 基下测），得到该项期望。
4. 反向施加基变换门（\(H$ 自逆、\(R_x(-\pi/2)$ 逆 \(R_x(\pi/2)$），把状态还原，保证后续项测量仍在原始态上进行。

**能力位 `canHandleObserve` 决定走哪条路**：

- 为假（默认/采样路径）：上面这条「逐项基变换 + 测 \(Z$」。
- 为真（矩阵直算路径，如 qpp/custatevec 在无 shots 时）：后端直接用 \(\langle\psi|H|\psi\rangle$ 一次性算出，**不需要任何基变换门、不需要逐项**，速度极快但只适合态向量类后端。

**术语合成**则发生在 `observe(kernel, termList, args...)` 重载里：把 `termList` 里每个 `spin_op_term` 规范化后 `+=` 进一个空 `spin_op`，统一 `runObservation`；测完再用 `result.expectation(term)` 按 `term_id` 把每项期望拆出来。

#### 4.3.3 源码精读

基变换的核心实现 [runtime/nvqir/CircuitSimulator.h:1493-1520](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L1493-L1520)：

```cpp
auto term = *op.begin();
for (const auto &p : term) {
  auto pauli = p.as_pauli();
  auto target = p.target();
  if (pauli != cudaq::pauli::I)
    qubitsToMeasure.push_back(target);

  if (pauli == cudaq::pauli::Y)
    basisChange.emplace_back([&, target](bool reverse) {
      rx(!reverse ? M_PI_2 : -M_PI_2, target);   // Y → 测前 rx(+π/2)，还原 rx(-π/2)
    });
  else if (pauli == cudaq::pauli::X)
    basisChange.emplace_back([&, target](bool) { h(target); });  // X → 测前 H
}
// Change basis, flush the queue
if (!basisChange.empty()) {
  for (auto &basis : basisChange) basis(false);   // 正向施加
  flushGateQueue();
}
```

测量之后的「还原」在 [CircuitSimulator.h:1532-1539](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L1532-L1539)：把 `basisChange` 反序，逐个以 `reverse=true` 调用，即 \(X$ 用 \(H$ 还原、\(Y$ 用 \(R_x(-\pi/2)$ 还原。这样模拟器在「同一份制备好的态」上依次测完所有项——这正是态向量模拟器的优势（可任意次复用量子态）。

能力位分流在终结函数最上方（[CircuitSimulator.h:1126-1129](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L1126-L1129)）：

```cpp
if (policy.canHandleObserve) {
  auto [exp, data] = measureSpinOp(H);   // 整个 H 一次性矩阵直算
  return cudaq::observe_result(exp, H, data);
}
// 否则进入逐项循环
```

而 `canHandleObserve` 由后端各自的 `canHandleObserve()` 决定。例如 qpp 后端 [runtime/nvqir/qpp/QppCircuitSimulator.cpp:309-319](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/qpp/QppCircuitSimulator.cpp#L309-L319)：

```cpp
bool canHandleObserve() override {
  auto executionContext = cudaq::getExecutionContext();
  if (executionContext &&
      executionContext->shots != static_cast<std::size_t>(-1)) {
    return false;                // 用户要求 shots → 必须走采样路径
  }
  return !shouldObserveFromSampling();
}
```

也就是说：**只要指定了 shots，就强制走「逐项基变换 + 采样」**（因为采样是带统计噪声的，不能再用矩阵精确值冒充）；不指定 shots 且后端有能力时，才走矩阵直算。这条规则对性能影响极大（见 4.3.4）。

术语合成的重载 [runtime/cudaq/algorithms/observe.h:212-244](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe.h#L212-L244)，合入与拆回两段一目了然：

```cpp
// Convert all spin_ops to a single summed spin_op   ← 合成
auto op = cudaq::spin_op::empty();
for (auto &o : termList)
  op += cudaq::spin_op_term::canonicalize(o);

auto result = detail::runObservation(/*kernel*/, op, platform, /*shots=*/-1, kernelName);

// Convert back to a vector of results               ← 拆回
std::vector<observe_result> results;
for (const auto &term : op)
  results.emplace_back(result.expectation(term), term, result.counts(term));
```

每个项靠 `term.get_term_id()` 在统一结果 `data` 里找到自己的那一份计数与期望（这正是 `observe_result::expectation(term)` 的查表逻辑）。

> 补充：多 QPU 场景下还有「项的切分」`distribute_terms(n)`（[sum_op.cpp:1604-1630](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/operators/sum_op.cpp#L1604-L1630)），把项均分到若干 QPU 上并行计算再 `all_reduce` 求和（`distributeComputations`，[observe.h:163-191](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe.h#L163-L191)）。这是术语合成的「分布式变体」。

#### 4.3.4 代码实践

> 说明：规格里提到的「`terms_reordering` 策略」在本版本（HEAD `61face2b9a`）的运行时里**没有以该名字暴露的开关**（`observe_options` 仅含 `shots`、`noise`、`num_trajectories`，见 [runtime/cudaq/algorithms/observe/options.h:25-29](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe/options.h#L25-L29)）。因此下面的实践改为考察**真实存在且真正决定门数量/电路次数的两个因素**：(a) 矩阵直算 vs 逐项采样两条路径；(b) 项数与每项的 \(X/Y$ 基变换代价。

**目标**：用同一个 ansatz + 同一个 \(H$，分别以「无 shots（矩阵直算）」与「带 shots（逐项采样）」两种方式 `observe`，对比它们的执行路径与可观测差异；并从 \(H$ 的项结构估算逐项路径下的基变换门开销。

**步骤**：

1. 用 [expectation_values.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/expectation_values.cpp) 的 ansatz 与 \(H$，写两段对比（示例代码）：

```cpp
// 示例代码：对比矩阵直算 vs 逐项采样两条 observe 路径
struct ansatz { auto operator()(double theta) __qpu__ {
    cudaq::qvector q(2); x(q[0]); ry(theta, q[1]); x<cudaq::ctrl>(q[1], q[0]);
}};

int main() {
  cudaq::spin_op h = 5.907 - 2.1433 * cudaq::spin_op::x(0) * cudaq::spin_op::x(1)
                     - 2.1433 * cudaq::spin_op::y(0) * cudaq::spin_op::y(1)
                     + .21829 * cudaq::spin_op::z(0) - 6.125 * cudaq::spin_op::z(1);

  // (a) 无 shots：qpp 后端 canHandleObserve()=true → 矩阵直算，无基变换门
  double e1 = cudaq::observe(ansatz{}, h, .59);
  printf("[matrix ] energy = %lf\n", e1);

  // (b) 带 shots：强制走逐项采样路径，每项需基变换门 + 测 Z
  double e2 = cudaq::observe(/*shots*/ 10000, ansatz{}, h, .59);
  printf("[sampling] energy = %lf\n", e2);
  return 0;
}
```

2. 编译运行：`nvq++ cmp.cpp -o cmp.x && ./cmp.x`。可选：加 `CUDAQ_LOG_LEVEL=info` 观察日志里基变换门与逐项测量的痕迹。

3. **手工估算门开销**：\(H$ 有 4 个非常量项（\(X_0X_1, Y_0Y_1, Z_0, Z_1$），逐项采样路径下：
   - \(X_0X_1$ 项：2 个 \(H$ 门（每比特一个 \(X$）；
   - \(Y_0Y_1$ 项：2 个 `rx` 门（每比特一个 \(Y$）；
   - \(Z_0, Z_1$ 项：无需基变换门（\(Z$ 已是目标基）。
   - 加上每次测前的 ansatz 本身（`x` + `ry` + `cnot`），以及每个非 \(I$ 项都要**重新跑一遍 ansatz**（因为采样会塌缩态）。

**需要观察的现象**：

- 两种路径打印的能量数值：矩阵直算的精确值 vs 采样的统计估计值（后者应在前者附近波动）。
- 项越多、含 \(X/Y$ 越多，逐项采样的门/电路总开销越大；而矩阵直算的开销几乎与项数无关（只算一次矩阵期望）。

**预期结果**：两个能量数值接近（采样路径随 shots 增大收敛到矩阵值）。这印证了「`canHandleObserve` 为真时省掉全部基变换门、把 \(N$ 次电路合并成 1 次矩阵运算」的性能收益。**待本地验证**两者的精确数值与运行耗时差异。

#### 4.3.5 小练习与答案

**练习 1**：对项 \(X_0 Y_1 Z_2$，`measureSpinOp` 会施加哪些基变换门？分别在测量前和测量后施加什么？

**参考答案**：测量前对 q0 施 `h`、对 q1 施 `rx(π/2)`，对 q2 不加门（\(Z$ 本就是测量基），然后测 q0、q1、q2。测量后按反序还原：对 q1 施 `rx(-π/2)`、对 q0 施 `h`（`h` 自逆）。对应代码 [CircuitSimulator.h:1506-1539](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/CircuitSimulator.h#L1506-L1539)。

**练习 2**：为什么指定了 shots 之后，qpp 后端就不能走矩阵直算（`canHandleObserve()` 返回 false）？

**参考答案**：shots 意味着用户要的是「带统计噪声的真实采样分布」（模拟真实硬件实验）。矩阵直算给的是无噪声精确期望，与 shots 语义矛盾。所以 [QppCircuitSimulator.cpp:313-316](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/qpp/QppCircuitSimulator.cpp#L313-L316) 一旦检测到有效 shots 就强制返回 false，回到逐项基变换 + 采样的真实路径。

**练习 3**：术语合成重载 `observe(kernel, termList, args...)` 内部为什么先合成一个 `spin_op`、测完又要拆回向量？

**参考答案**：合成是因为后端的 `runObservation` 只接受单个 `spin_op`，统一一次调用即可让平台/后端决定最优测量方式（矩阵直算或逐项），避免用户对每个 term 各调一次 `observe` 带来的重复内核执行与平台开销。拆回是因为调用方传进来的是「一组独立的项」，自然期望拿回「每项各自的期望与计数」，所以用 `result.expectation(term)` 按 `term_id` 拆出。合与拆的代码在 [observe.h:228-243](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/observe.h#L228-L243)。

## 5. 综合实践

把本讲三个模块串起来，做一个「迷你 VQE 能量评估器」。

**任务**：

1. 写一个参数化 ansatz 内核 \(U(\theta)$（可沿用 [expectation_values.cpp:12-19](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/expectation_values.cpp#L12-L19) 的 `ansatz`）。
2. 构造一个 4–6 项的 `spin_op` \(H$（至少含一个 \(I$ 常量项、一个 \(ZZ$ 类项、一个含 \(X$ 或 \(Y$ 的项），`dump()` 打印并 `canonicalize` 后再 `dump()` 一次，对比项表示的变化。
3. 用 `observe(ansatz{}, H, theta)` 在一组 \(\theta\in\{0.1, 0.3, 0.59, 0.9, 1.2\}$ 上分别求能量，画出（或打印出）能量随 \(\theta$ 的曲线，找到能量最低的 \(\theta$。
4. 对最优 \(\theta$，再用带 shots 的 `observe(10000, ansatz{}, H, theta)` 跑一次，对比矩阵直算与采样的能量差。
5. 用 `observe_result::expectation(term)` 取出含 \(X/Y$ 的那一项的单独期望，验证：全局能量 ≈ \(\sum_k c_k \langle P_k\rangle$。

**验收标准**：

- 步骤 2 能正确解释规范化前后字符串的差异（如 degree 排序、冗余 \(I$ 因子消除）。
- 步骤 3 的能量曲线在某个 \(\theta$ 处取得最小值。
- 步骤 4 的采样能量在矩阵能量附近（误差随 shots 增大而减小）。
- 步骤 5 的手算加权求和与 `observe` 返回的全局能量一致。

> 提示：这一步把「spin_op 代数构造 → observe 流程 → 基变换/采样路径」三者打通，正是下一讲 u3-l4（优化器与梯度）里 VQE 内循环的雏形——那里会把步骤 3 的「网格搜索」换成真正的梯度优化器。

## 6. 本讲小结

- `spin_op = sum_op<spin_handler>`、`spin_op_term = product_op<spin_handler>`；用 `spin_op::x/y/z(n)` 工厂 + `*`/`+`/标量即可像写数学式一样构造泡利字符串形式的哈密顿量。
- `cudaq::observe(kernel, H, args...)` 的核心是**逐项求和**：\(\langle H\rangle=\sum_k c_k\langle P_k\rangle$；\(I$ 项贡献系数本身，非常量项靠后端测量。`observePreamble` 会先把 \(H$ `canonicalize` 写进 `ExecutionContext("observe", ...)`。
- **测量基变换**解决「只能测 \(Z$」：测 \(X$ 前加 `h`，测 \(Y$ 前加 `rx(π/2)`，测完反序还原；实现集中在 `CircuitSimulator::measureSpinOp`。
- **术语合成**指把一组 `spin_op_term` 合并为单个 `spin_op` 统一测量、测完按 `term_id` 拆回；多 QPU 下还有 `distribute_terms` 把项切分到各 QPU 并行。
- 能力位 `canHandleObserve` 决定后端走**矩阵直算**（无 shots、态向量后端，无需基变换门）还是**逐项采样**（带 shots 或硬件路径，每项需基变换并重跑 ansatz）；这是 observe 性能的关键开关。
- `observe_result` 通过 `operator double()` 直接给能量，也可用 `expectation(term)`/`counts(term)` 取每项明细。

## 7. 下一步学习建议

- **u3-l4 优化器与梯度**：把本讲的 `observe` 当作目标函数，接上 `cudaq::optimizer` + 梯度策略（parameter-shift、central difference），实现完整的 VQE 内循环。重点关注梯度估计如何复用本讲的「逐项期望」机制。
- **u3-l5 算符代数**：本讲的 `spin_op` 只是 CUDA-Q 算符体系的一员；下一步了解 `fermion_op`、`boson_op`、`matrix_op`，以及如何把费米子哈密顿量（化学积分）经 Jordan–Wigner 等映射变成这里的 `spin_op` 再交给 `observe`。
- **u3-l2 sample 深入**：若你想搞清「逐项采样」路径里 shot 累加、异步与多 QPU 并行的细节，回看 u3-l2，本讲的 `distributeComputations` 正是建立在 u3-l2 的异步采样原语之上。
- **源码延伸阅读**：想看远程 QPU（如 IonQ/IQM）如何在没有矩阵直算能力时实现 observe，可读 `runtime/common/BaseRemoteRESTQPU.h` 里 `observe_policy` 相关的 `completeLaunchKernel` 与 `observeResultFromCounts`（u6-l5 会展开）。
