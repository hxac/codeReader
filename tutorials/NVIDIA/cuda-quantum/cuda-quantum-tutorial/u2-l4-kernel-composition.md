# 内核组合与参数传递

## 1. 本讲目标

前几讲里我们写的内核都是「一整块」：所有门都直接写在一个 `__qpu__` 函数体里。真实的量子算法往往要复杂得多——我们希望把一段反复出现的线路封装成**子内核**，再在主内核里调用它；也希望把旋转角、比特数、参数列表等**经典数据**作为参数传给内核。本讲就回答两个问题：

1. **内核组合**：一个 `__qpu__` 内核怎样调用另一个 `__qpu__` 内核？把整块子内核受控（`cudaq::control`）、求逆（`cudaq::adjoint`）、套「计算-作用-逆计算」（`cudaq::compute_action`）时，背后的机制是什么？
2. **参数传递**：内核能接收哪些类型的参数（`int`、`double`、`std::vector`、复数向量、量子态……）？为什么说「内核参数」和普通 C++ 函数参数**并不完全一样**？

读完本讲，你应该能够：

- 写出「主内核循环调用参数化子内核」这种最常见的组合模式，并知道何时必须用 `qkernel` 包装。
- 看懂 CUDA-Q 在 **JIT 编译期**做的「参数合成（Argument Synthesis）」：每次用具体参数调用内核时，编译器会把形式参数**替换成常量代码**，生成一个「没有参数的特化版本」。
- 用 `cudaq-opt --argument-synthesis` 单独观察这个特化过程，把「内核签名」与「参数合成」这两件事在源码层面串起来。

本讲的实践任务是：写一个参数化子内核（旋转角），在主内核里循环调用它、每次传不同角度，采样后验证每个比特的边缘分布是否符合 \(\cos^2(\theta/2)\)。

## 2. 前置知识

- **u1-l4 / u2-l1**：`__qpu__` 是贴在可调用对象 `operator()` 上的注解宏，是编译器把内核翻译成 Quake MLIR 的开关；量子比特以 `qubit`/`qvector` 分配，类型系统**禁止拷贝拥有型量子值**，跨内核传递一组比特必须用**按引用**或**非拥有视图**。
- **u2-l2**：门由「名字 + 比特 id + 参数」薄封装，汇入 ExecutionManager；`cudaq::ctrl` 是修饰符、`cudaq::control/adjoint/compute_action` 是把**整块子内核**受控/求逆的组合算子。本讲在「组合算子」的基础上，把它们用于「内核调用内核」的场景。
- **u2-l3**：`cudaq::sample` 把内核重复执行 `DEFAULT_NUM_SHOTS=1000` 次，返回 `sample_result`；可以用 `get_marginal` / `probability` 按比特位读取边缘分布。本讲实践要靠它来验证参数化内核的输出。
- **u1-l1 / u4 概览**：CUDA-Q 是「编程模型 + 编译器」项目，源码不指定具体模拟器，后端在**链接期**切换；内核在 **JIT（即时编译）** 阶段还会被进一步特化——这是理解本讲「参数合成」的前提。
- **部分求值（partial evaluation）直觉**：普通函数 `f(x)` 调用时 `f(3)` 仍是个带参数的函数；而「合成/特化」会把 `3` **织进函数体**，得到一个不带参数的 `f₃()`，使得 `f₃()` 的运行结果恰好等于 `f(3)`。本讲的 ArgumentSynthesis 就是这个思想在 Quake MLIR 上的实现。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `docs/sphinx/examples/cpp/building_kernels.cpp` | 官方「构建内核」示例集。本讲的「内核调用内核」「参数化内核」「整块子内核受控/求逆」全部出自这里。 |
| `docs/sphinx/examples/python/building_kernels.py` | 上面那份示例的 Python 镜像版本，用来对照双前端的参数传递写法。 |
| `runtime/cudaq/qis/qkernel.h` | `qkernel` 包装类型的定义。它给出了一条**关键规则**：内核「被引用而非直接调用」时必须用 `qkernel` 包装。 |
| `runtime/cudaq/qis/qubit_qis.h` | `cudaq::control / adjoint / compute_action / compute_dag_action` 组合算子的模板实现（用「控制区/伴随区」包围一次子内核调用）。 |
| `cudaq/lib/Optimizer/Transforms/ArgumentSynthesis.cpp` | **参数合成 Pass** 的实现：把内核的形式参数替换成由实际参数生成的常量代码，并擦除被替换掉的形参。 |
| `runtime/internal/compiler/Compiler.cpp` | 运行时 JIT 编译器。展示了「参数合成 → canonicalize → LambdaLifting → AggressiveInlining → …」的完整流水线，以及「为何要在特化前先内联内核调用」。 |
| `cudaq/test/Transforms/arg_subst_func.qke` | 参数合成 Pass 的 FileCheck 回归测试，用最小例子展示「带参数的函数 → 常量织入 → 无参函数」的可视化效果。 |

## 4. 核心概念与源码讲解

### 4.1 内核嵌套调用：用内核搭内核

#### 4.1.1 概念说明

随着线路变大，把重复片段写成**子内核**再调用，是保持代码可读性的关键。CUDA-Q 允许在一个 `__qpu__` 内核里**直接调用**另一个 `__qpu__` 内核，就像普通函数调用一样。最典型的模式是「主内核里循环调用子内核，每次传一对相邻比特」：

```cpp
// building_kernels.cpp [BuildingKernelsWithKernels] 片段
__qpu__ void kernel_A(cudaq::qubit &q0, cudaq::qubit &q1) {
  x<cudaq::ctrl>(q0, q1);          // 一个 CNOT
}

__qpu__ void kernel_B() {
  cudaq::qvector reg(10);
  for (int i = 0; i < 5; i++) {
    kernel_A(reg[i], reg[i + 1]);  // 直接调用子内核
  }
}
```

注意两个细节：

1. 子内核 `kernel_A` 的两个参数是 `cudaq::qubit &`（**按引用**）。这呼应 u2-l1 的结论——拥有型量子值不可拷贝，把比特「借给」子内核只能用引用（或 `qview` 视图），不能按值传。
2. `kernel_B` 对 `kernel_A` 是**直接调用（direct call）**。这种写法在内核内部是允许的，编译器会把它翻译成 Quake 里的一个函数调用，再在优化阶段**内联**展开。

但有一条容易踩坑的规则：**当一个内核被「引用」而非「直接调用」时——例如存进变量、作为参数传给 `cudaq::observe` 当作 ansatz、或交给 `cudaq::control` 受控——在内核之外的宿主代码里，必须先用 `qkernel` 包装它。** 这条规则写在 `qkernel` 的类型注释里：

> A `qkernel` must be used to wrap CUDA-Q kernels (callables annotated with the `__qpu__` attribute) when those kernels are *referenced* other than by a direct call in code outside of quantum kernels proper.

直觉解释：直接调用时编译器看得到调用点，能自动处理；而「被引用」意味着内核要像一个**值**一样被传递、被运行时按名查找，需要一个统一的类型擦除容器（`qkernel`）来承载它的签名与入口指针。

#### 4.1.2 核心流程

内核调用内核，从源码到执行经过这些阶段：

1. **AST 翻译**：`kernel_B` 体内的 `kernel_A(reg[i], reg[i+1])` 被翻译成 Quake/CC 里的一条函数调用（`func.call`），被调函数 `kernel_A` 作为模块里的另一个 Quake 函数存在。
2. **LambdaLifting / AggressiveInlining**：把子内核调用**内联**到主内核里，使主内核变成一块「扁平」的量子线路。
3. **跨边界特化**：内联之后，`control / adjoint` 这类「按子内核整体作用」的特化才能跨过原本的函数调用边界生效。
4. **后端执行**：最终扁平化的线路交给后端（默认 qpp）解释执行。

关键点：内核调用本质上是**编译期的代码组织手段**，运行时并不真的存在「函数调用栈」——所有子内核都会被内联进入口内核。这也是为什么 `control / adjoint` 能作用于「整块子内核」：内联后那块线路就是一段连续的量子操作，给它套一个控制区/伴随区即可。

#### 4.1.3 源码精读

**「内核调用内核」的最小示例**——[building_kernels.cpp:L130-L141](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/building_kernels.cpp#L130-L141)：`kernel_B` 在 `for` 循环里直接调用 `kernel_A`，子内核形参为 `qubit &`。Python 镜像见 [building_kernels.py:L200-L213](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/building_kernels.py#L200-L213)。

**`qkernel` 的包装规则**——[qkernel.h:L98-L106](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qkernel.h#L98-L106)：注释明确「被引用而非直接调用」时必须用 `qkernel`。其 `operator()` 把调用转交给类型擦除后的 `QKernelHolder::dispatch`，见 [qkernel.h:L142-L147](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qkernel.h#L142-L147)。

**整块子内核受控/求逆**——`cudaq::control` 把控制比特 id 收集起来，调用 `startCtrlRegion` 开启「控制区」，再执行子内核，最后 `endCtrlRegion` 关闭，使子内核里的每条门都自动带上这些控制比特：

```cpp
// qubit_qis.h，control 一个内核到单个控制比特上
template <typename QuantumKernel, typename... Args>
  requires isCallableVoidKernel<QuantumKernel, Args...>
void control(QuantumKernel &&kernel, qubit &control, Args &&...args) {
  std::vector<std::size_t> ctrls{control.id()};
  getExecutionManager()->startCtrlRegion(ctrls);   // 开控制区
  kernel(std::forward<Args>(args)...);             // 跑子内核
  getExecutionManager()->endCtrlRegion(ctrls.size());// 关控制区
}
```

完整三处重载（单比特、寄存器、引用列表）见 [qubit_qis.h:L664-L703](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L664-L703)；`adjoint` 用「伴随区」包围子内核见 [qubit_qis.h:L705-L713](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L705-L713)；`compute_action`（\(CAC^\dagger\)）与 `compute_dag_action`（\(C^\dagger AC\)）见 [qubit_qis.h:L718-L737](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L718-L737)。

调用方示例（注意 `cudaq::control(x_kernel, ...)` 在内核 `kernel6` 内部、`x_kernel` 是自由函数，属直接引用，无需 `qkernel` 包装）见 [building_kernels.cpp:L87-L115](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/building_kernels.cpp#L87-L115)；`cudaq::adjoint` 调用见 [building_kernels.cpp:L117-L128](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/building_kernels.cpp#L117-L128)。

**「为何要在特化前先内联」**——JIT 流水线里这行注释点破了内核调用的本质：见 [Compiler.cpp:L270-L273](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/internal/compiler/Compiler.cpp#L270-L273)，注释说明 `apply` 特化不会跨函数调用边界做 `control/adjoint` 特化，所以必须先用 `addAggressiveInlining` 把子内核调用内联掉。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：亲眼看到「内核调用内核」在 Quake IR 里就是一个普通函数调用，并在内联后被展平。
2. **步骤**：
   - 把本讲 4.1.1 的 `kernel_A / kernel_B` 存成 `nest.cpp`，用 `nvq++ -c nest.cpp`（或对内核加一个 `main` 后用 `nvq++ nest.cpp`）让它在编译期生成 Quake MLIR。
   - 设置 `CUDAQ_LOG_LEVEL=trace`（详见 u8-l2）运行，或用 `cudaq-quake` 观察编译早期产物，确认 `kernel_B` 体内存在形如 `call @...kernel_A...` 的调用，`kernel_A` 是模块内一个独立的 `func.func`。
3. **观察**：早期 IR 里 `kernel_B` 内有对 `kernel_A` 的调用；经过 `aggressive-inlining` 后，调用消失，`kernel_B` 体内直接出现 5 条 CNOT。
4. **预期结果**：内联前后逻辑等价；最终采样结果与「把 `kernel_A` 的 CNOT 手写 5 遍」完全一致。
5. 如无法在本地拿到中间 IR，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：把 4.1.1 的 `kernel_A` 形参从 `cudaq::qubit &q0, cudaq::qubit &q1` 改成 `cudaq::qubit q0, cudaq::qubit q1`（按值），会发生什么？为什么？

> **答案**：编译失败。`qubit`（即 `qudit<2>`）删除了拷贝/移动构造（u2-l1），按值传递需要拷贝形参，违反「量子比特不可克隆」的类型约束。这就是为什么跨内核传比特必须用引用或视图。

**练习 2**：`cudaq::compute_action(C, A)` 展开后等价于哪三步？它和直接写 `C(); A(); C();` 有什么本质区别？

> **答案**：等价于 `C(); A(); adjoint(C);`，即 \(CAC^\dagger\)。本质区别在第三步是 `adjoint(C)`——求**伴随（逆）**，不是把 `C` 再跑一遍。当 `C` 含非自逆门（如 `t`、`ry(θ)`）时，`C` 与 `C⁻¹` 不同，写错会导致错误的线路。

### 4.2 参数类型与传递

#### 4.2.1 概念说明

CUDA-Q 内核的参数并不局限于量子比特。它还可以接收**经典数据**，这些数据会在内核里决定线路的形状或门的参数。常见的参数类型有：

| 参数类型 | C++ 写法 | Python 写法 | 典型用途 |
|---|---|---|---|
| 标量整数 | `int N` | `N: int` | 决定 `qvector` 的大小、循环次数 |
| 标量浮点 | `double theta` | `theta: float` | 旋转角 `rx/ry/rz` 的参数 |
| 浮点列表 | `std::vector<double>` | `list[float]` | 一组旋转角（变分参数） |
| 复数向量 | `const std::vector<complex> &` | `list[complex]` | 用幅值向量**初始化**量子态 |
| 量子态 | （Python）`cudaq.State` | `state: cudaq.State` | 把另一个内核的输出态作为输入 |
| 量子比特引用 | `cudaq::qubit &` | `qubit: cudaq.qubit` | 把宿主分配的比特借给子内核操作 |

这里要建立两个关键区分：

1. **按值 vs 按引用**：经典数据（`int`、`double`、`std::vector`）按值或 `const &` 传都行，与普通 C++ 一致；**量子比特只能按引用（或视图）传**，因为拥有型量子值不可拷贝。
2. **参数 vs 字面量捕获**：在 Python 端，写进内核**签名**的是「参数」（每次调用都可变），而直接闭包捕获的外层变量是「字面量」（被织进 IR，见 building_kernels.py 的 `CapturingComplexVector`）。C++ 端同理：全局变量 `int N = 2;` 与形参 `kernel(int N)` 是两回事——前者是捕获，后者是参数。

#### 4.2.2 核心流程

参数从宿主走到量子线路的链路：

1. **宿主收集实参**：调用 `cudaq::sample(kernel, arg1, arg2, ...)` 时，运行时把实参打包成**类型擦除**的参数块（`OpaqueArguments`），因为入口启动函数是统一签名的。
2. **JIT 触发**：运行时找到内核对应的 Quake MLIR，进入 JIT 编译（见 4.3）。
3. **参数合成**：把实参的值**织进**内核体（标量变成 `arith.constant`，向量变成 `cc.stdvec_init` + 一串 `cc.store`），并擦除对应形参。
4. **内联与特化**：子内核调用被内联，循环边界（若依赖参数）随之确定，最终得到一块完全扁平、可交给后端的线路。

数学上，参数合成是一种**部分求值**。设内核签名为 \(K(x_1:T_1,\dots,x_n:T_n)\)，给定一组具体实参 \(a_1,\dots,a_n\)，合成产生无参特化版：

\[
K_{a_1,\dots,a_n}\;=\;K\big[x_1\mapsto \text{code}(a_1),\;\dots,\;x_n\mapsto \text{code}(a_n)\big],\qquad K_{a_1,\dots,a_n}()\;=\;K(a_1,\dots,a_n).
\]

其中 \(\text{code}(a_i)\) 是「计算 \(a_i\) 的常量代码」。注意等号右边 \(K_{a_1,\dots,a_n}\) **没有参数**——参数已经被「烤进」函数体。

#### 4.2.3 源码精读

**用 `int` 参数决定 `qvector` 大小**——[building_kernels.cpp:L22-L26](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/building_kernels.cpp#L22-L26)：`kernel(int N)` 内 `cudaq::qvector r(N);`。注意文件上方还有一行全局 `int N = 2;`——它和形参 `N` 是两个独立的东西，演示了「捕获」与「参数」的对照。Python 版见 [building_kernels.py:L14-L23](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/building_kernels.py#L14-L23)。

**传浮点向量当变分参数**——[building_kernels.cpp:L143-L152](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/building_kernels.cpp#L143-L152)：`kernel9(std::vector<double> thetas)` 用 `thetas[0]`、`thetas[1]` 作为 `rx/ry` 的角度。Python 版见 [building_kernels.py:L216-L226](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/building_kernels.py#L216-L226)。

**传复数向量初始化量子态**——[building_kernels.cpp:L28-L31](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/building_kernels.cpp#L28-L31)：`kernel(const std::vector<complex> &vec)`。更推荐用「精度无关」的 `cudaq::complex`，见 [building_kernels.cpp:L44-L55](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/building_kernels.cpp#L44-L55)。

**把一个内核的输出态当参数传给另一个内核**（Python 独有的便捷写法）——[building_kernels.py:L75-L94](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/building_kernels.py#L75-L94)：先用 `cudaq.get_state(kernel_initial)` 取出态，再把 `state: cudaq.State` 作为形参，内核内 `q = cudaq.qvector(state)` 完成初始化。这演示了「参数」可以是更高阶的量子对象。

#### 4.2.4 代码实践（源码阅读 + 运行）

1. **目标**：直观看到「同一内核，不同参数，不同分布」。
2. **步骤**：把 [building_kernels.cpp:L143-L152](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/building_kernels.cpp#L143-L152) 的 `kernel9` 补一个 `main`，分别用 `thetas = {0.0, 0.0}` 和 `thetas = {M_PI, M_PI}` 各采样一次（注意 `kernel9` 没有 `mz`，需要补一句 `mz(qubits);` 才能采样，否则触发 0-shot 警告——这是 u2-l3 的结论）。
3. **观察**：`{0,0}` 时两比特都未被旋转，应几乎全 `00`；`{π,π}` 时 `rx(π)` 等价于 `x`，应几乎全 `11`。
4. **预期结果**：两次采样分布显著不同，证明参数确实改变了线路。精确计数「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`kernel9(std::vector<double> thetas)` 里访问 `thetas[0]` 和 `thetas[1]`，运行时会不会做越界检查？向量长度在何时被「固定」？

> **答案**：在默认（非 `fullySpecialize`）目标下，`thetas` 的内容在 JIT 参数合成阶段被织进 IR：向量长度变成一个 `arith.constant`，每个元素也变成常量。换言之，长度在**编译期（特化时）**就被固定为调用时传入的实参长度；越界在合成后通常表现为编译期可见的越界常量下标，往往在后续优化或代码生成阶段报错，而非运行时动态检查。

**练习 2**：Python 端 building_kernels.py 的 `CapturingComplexVector` 把 `c` 写在签名外（闭包捕获），而 `PassingComplexVector` 把 `vec` 写进签名（参数）。这两种写法在「换一组复数重新运行」时有什么区别？

> **答案**：参数版 `kernel(vec)` 可以多次调用、每次传不同向量，每次都会重新 JIT 特化；捕获版 `kernel()` 的 `c` 在内核定义时就被织进 IR，换数据必须改源码或重新定义内核。需要反复换数据的场景应使用**参数**而非捕获。

### 4.3 ArgumentSynthesis：编译期的参数织入

#### 4.3.1 概念说明

4.2 已经提到「参数合成」这个动作，本节正式讲它对应的 Pass：**ArgumentSynthesis（参数合成）**。它的职责一句话概括：**把内核的形式参数，替换成由实际参数生成的代码，再擦除被替换掉的形参。**

为什么要单独搞这么一个 Pass？因为 CUDA-Q 的内核入口启动函数是**统一、类型擦除**的（宿主和设备之间靠一个固定签名桥接），但用户写的内核签名千差万别（`int`、`double`、`std::vector<double>`、`qubit&`……）。运行时不可能为每种签名生成专门的启动代码，于是采用如下策略：

- 把「带任意签名 + 实参」的内核，**特化**成一个「无参数（或只剩无法织入的参数）」的内核；
- 这样入口启动函数只需调用一个无参内核，签名问题被消除。

这个过程在**每次用新参数调用内核时**发生（JIT），所以同一份内核源码会被特化出多个版本——这是 CUDA-Q「源码一份、按调用特化」设计的体现。

#### 4.3.2 核心流程

ArgumentSynthesis Pass 的输入是「函数名 : 替换说明」的列表。对每个目标函数：

1. **解析替换说明**：替换内容可以是一段**原始 MLIR 字符串**（命令行里以 `*` 前缀），也可以是一个**文件路径**（命令行用法）。它会被解析成一个临时 `ModuleOp`。
2. **逐形参替换**：临时模块里每个 `cc.argument_substitution` 操作声明「第 `pos` 个形参用这段代码替换」。Pass 把这段代码**缝合**进目标函数的入口块：在原入口块开头切开，分支到替换代码，替换代码末尾再分支回剩余部分。
3. **替换使用点**：若替换代码的最后一个结果与原形参类型一致，把原形参的所有使用**替换**成这个新结果。
4. **擦除形参**：被替换的形参从函数签名中擦除（默认 `changeSemantics` 为真时）。

完整流水线在 JIT 里这样编排（见 4.3.3 源码点）：参数合成 → `canonicalize` → `LambdaLifting` → `AggressiveInlining` → `apply-specialization` → 再内联 → …… → 必要时 `GenerateKernelExecution`。注意「先内联、后特化」的顺序：`control/adjoint` 特化不跨函数调用边界，所以必须先用 `AggressiveInlining` 把子内核调用展开。

#### 4.3.3 源码精读

**Pass 主体：遍历「函数:替换」列表**——[ArgumentSynthesis.cpp:L33-L59](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Optimizer/Transforms/ArgumentSynthesis.cpp#L33-L59)：解析每一项 `funcName:text`，找到对应 `FuncOp`，`text` 为空则跳过。

**解析替换模块（原始字符串 vs 文件）**——[ArgumentSynthesis.cpp:L63-L70](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Optimizer/Transforms/ArgumentSynthesis.cpp#L63-L70)：`text.front() == '*'` 表示「`*` 之后是原始 MLIR 字符串」，否则当作文件路径解析。

**缝合 + 替换使用点 + 擦除形参**——[ArgumentSynthesis.cpp:L108-L156](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Optimizer/Transforms/ArgumentSynthesis.cpp#L108-L156)：核心三步——把替换代码块 splice 进函数体、把原形参的使用替换为新结果（`replaceAllUsesWith`，第 146-149 行）、最后 `func.eraseArguments(replacedArgs)`（第 155-156 行）。注释提醒：擦除形参会改变调用约定、破坏所有调用点，故「不必要且强烈不建议」手动触发。

**构造 Pass 的辅助函数**——[ArgumentSynthesis.cpp:L165-L175](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Optimizer/Transforms/ArgumentSynthesis.cpp#L165-L175)：`createArgumentSynthesisPass` 把「函数名列表 + 替换字符串列表」拼成 `funcName:*text` 的形式，方便从 JIT 工具里加进流水线。

**JIT 里如何调用这个 Pass**——[Compiler.cpp:L224-L266](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/internal/compiler/Compiler.cpp#L224-L266)：`ArgumentConverter::gen` 由类型擦除实参生成替换模块（`argCon.getKernelSubstitutions()`），收集函数名与替换字符串，再 `pm.addPass(createArgumentSynthesisPass(...))`。随后紧跟 `LambdaLifting` 和「特化前必须内联」的 `AggressiveInlining`（[Compiler.cpp:L267-L276](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/internal/compiler/Compiler.cpp#L267-L276)）。

**最小可视化：带参函数被特化成无参函数**——[arg_subst_func.qke:L9-L26](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/test/Transforms/arg_subst_func.qke#L9-L26)：测试输入是 `func.func @foo(%arg0: i32, %arg1: f32)`，经 `--argument-synthesis=functions=foo:%S/arg_subst.txt --canonicalize` 后，FileCheck 期望输出变成 `func.func @foo()`，原 `%arg0/%arg1` 的使用被 `arith.constant 42` 和 `arith.constant 3.1` 替换。这正是「形参 → 常量代码 → 擦除」的完整闭环。

#### 4.3.4 代码实践（源码阅读 + IR 观察）

1. **目标**：用一个最小例子，亲手把「带参数的函数」特化成「无参数的函数」。
2. **步骤**：
   - 阅读 [arg_subst_func.qke:L9-L26](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/test/Transforms/arg_subst_func.qke#L9-L26) 与同目录的 `arg_subst.txt`（替换说明文件），理解输入与期望输出的关系。
   - 如果本地已构建 CUDA-Q 工具链，构造一个最小 Quake/MLIR 文件，写一个 `func.func @myk(%arg0: i32)`，准备一个替换文件把 `%arg0` 换成常量 `7`，运行：
     ```
     cudaq-opt --argument-synthesis=functions=myk:./subst.txt --canonicalize myk.mlir
     ```
3. **观察**：输出里 `@myk` 的签名是否从 `@myk(%arg0: i32)` 变成了 `@myk()`，函数体内是否出现了 `arith.constant 7 : i32`。
4. **预期结果**：形参被擦除、原使用点被常量替换——与 `.qke` 测试里 `@foo` 的变化完全一致。
5. 若本地无 `cudaq-opt`，则改为纯阅读 `.qke` 测试与 `ArgumentSynthesis.cpp` 的 `runOnOperation`，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：ArgumentSynthesis 完成后，函数签名发生了什么变化？为什么这会「破坏所有调用点」？

> **答案**：被替换的形参从签名中**擦除**（`eraseArguments`）。函数的形参数量变化意味着调用约定（calling convention）变化，原先「按 N 个参数调用」的 `call` 操作现在实参数量对不上，所以会破坏所有调用点。这也是为什么 Pass 注释强调「擦除形参……不必要且强烈不建议」手动触发——正常流程里调用点会被后续内联/特化一并处理。

**练习 2**：为什么 JIT 流水线里 `ArgumentSynthesis` 之后要紧跟 `LambdaLifting` 和 `AggressiveInlining`，而不是直接交给后端？

> **答案**：参数合成可能在函数体里引入 lambda/临时结构（`LambdaLifting` 把它们提升为顶层函数）；而内核调用内核产生的 `func.call` 不会自动跨边界做 `control/adjoint` 特化，必须用 `AggressiveInlining` 内联成扁平线路，特化才能生效。跳过这两步会留下无法被后端直接执行或无法正确特化的中间结构。

## 5. 综合实践

把本讲三个模块（内核嵌套调用、参数传递、参数合成）串成一个完整任务：

**任务**：实现一个参数化子内核 `rot(theta, q)`（对单比特做 `ry(theta)`），在主内核 `chain` 里循环调用它，给每个比特传入不同角度，采样后验证每个比特的边缘分布。

**示例代码（C++）**：

```cpp
// 示例代码：综合实践
#include <cudaq.h>
#include <cmath>

// 参数化子内核：单比特旋转
__qpu__ void rot(double theta, cudaq::qubit &q) {
  ry(theta, q);
}

// 主内核：循环调用子内核，每个比特一个角度
__qpu__ void chain(int n, std::vector<double> angles) {
  cudaq::qvector q(n);
  for (int i = 0; i < n; i++) {
    rot(angles[i], q[i]);   // 内核调用内核 + 按引用传比特
  }
  mz(q);
}

int main() {
  int n = 4;
  std::vector<double> angles = {0.0, M_PI / 4, M_PI / 2, 3.0 * M_PI / 4};
  auto counts = cudaq::sample(chain, n, angles);  // 触发参数合成
  counts.dump();
  // 逐比特读取边缘分布
  for (int i = 0; i < n; i++) {
    printf("bit %d  P(0)=%.3f  (期望 cos^2(theta/2)=%.3f)\n", i,
           counts.probability(/*待本地确认 get_marginal 写法*/ 0),
           std::cos(angles[i] / 2) * std::cos(angles[i] / 2));
  }
}
```

**关注点对照**：

- **4.1 内核嵌套**：`chain` 内 `rot(angles[i], q[i])` 是直接调用，`q` 按引用传入子内核。
- **4.2 参数传递**：`n`（`int`）决定 `qvector` 大小、循环边界；`angles`（`std::vector<double>`）逐元素作为旋转角。
- **4.3 参数合成**：`cudaq::sample(chain, n, angles)` 在 JIT 期把 `n` 与 `angles` 织进 `chain`，得到一个无参特化版；子内核 `rot` 被内联展开。

**预期现象**：各比特独立旋转，比特 `i` 测得 0 的概率应近似

\[
P_i(0)=\cos^2\!\left(\frac{\text{angles}[i]}{2}\right).
\]

代入得：比特 0 ≈ 1.000（θ=0，不转）、比特 1 ≈ 0.854（θ=π/4）、比特 2 = 0.500（θ=π/2）、比特 3 ≈ 0.146（θ=3π/4）。精确计数「待本地验证」。也可用 Python（`@cudaq.kernel` + `cudaq.sample`）实现等价版本，对照双前端结果是否一致（u1-l5 的核心结论）。

## 6. 本讲小结

- **内核可以直接调用内核**：在 `__qpu__` 内核内直接调用另一个 `__qpu__` 内核是允许的，子内核形参里的量子比特必须**按引用（或视图）**传递，因为拥有型量子值不可拷贝。
- **`qkernel` 包装规则**：当内核「被引用而非直接调用」（如存进变量、作为 `observe` 的 ansatz）时，在宿主代码里必须用 `qkernel` 包装；内核内部对自由函数的直接引用则无需包装。
- **整块子内核可受控/求逆**：`cudaq::control / adjoint / compute_action` 用「控制区/伴随区」包围一次子内核调用，使整块线路受控或求逆；前提是子内核调用已被 `AggressiveInlining` 内联成扁平线路。
- **参数类型丰富**：内核可接收 `int`、`double`、`std::vector<double>`、复数向量、量子态、`qubit&` 等；标量/向量按值或 `const&`，量子比特只能按引用。
- **ArgumentSynthesis 是 JIT 期的部分求值**：每次用具体参数调用内核时，编译器把形式参数替换成常量代码并擦除形参，生成无参特化版，使统一签名入口得以调用任意签名的内核。
- **流水线顺序很关键**：「参数合成 → canonicalize → LambdaLifting → AggressiveInlining → 特化」的顺序保证了内核调用被内联、`control/adjoint` 特化能跨边界生效。

## 7. 下一步学习建议

- **进入执行模型（u3-l1）**：本讲的 `cudaq::sample(chain, n, angles)` 触发了 JIT 特化与后端执行。下一讲会拆解「一次内核调用如何被分发到具体后端」，把 `quantum_platform / QPU / ExecutionContext` 这套抽象与本讲的「参数合成 → 内联 → 执行」衔接起来。
- **深入编译流水线（u4-l6）**：若你想彻底搞懂 `LambdaLifting / AggressiveInlining / ArgumentSynthesis / GenKernelExecution` 这些 Pass 的依赖与调度，可直接进入编译器单元的「优化 Pass 流水线」一讲，并用 `cudaq-opt` 单独调试每个 Pass。
- **读源码建议**：先精读 [ArgumentSynthesis.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Optimizer/Transforms/ArgumentSynthesis.cpp) 与 [arg_subst_func.qke](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/test/Transforms/arg_subst_func.qke)，再看 [Compiler.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/internal/compiler/Compiler.cpp) 把 Pass 串成流水线的那段，就能把「内核签名 → 参数合成 → 后端执行」整条链路在源码里走通。
