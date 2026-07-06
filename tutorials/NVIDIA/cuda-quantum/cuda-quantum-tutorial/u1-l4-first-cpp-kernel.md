# 第一个 C++ 量子内核

## 1. 本讲目标

本讲带你写出第一个真正能编译运行的 CUDA-Q C++ 量子内核。学完后你应当能够：

- 用 `__qpu__` 标注定义一个 CUDA-Q 量子内核（以「可调用结构体」为载体）。
- 用 `cudaq::qubit` / `cudaq::qvector` 分配量子比特，并调用 `h`、`x`、`ry`、`mz` 等内建门。
- 用 `cudaq::sample` 执行内核、读取采样计数分布，并理解「默认 1000 次 shots」的来源。

本讲只解决「最小可用闭环」一个问题：**把一段 C++ 写成量子内核，编译它，跑出测量结果**。后续单元（u2、u3）才会深入类型系统、修饰符与算法原语。

## 2. 前置知识

- **C++ 基础**：结构体（`struct`）、`operator()` 重载、函数模板。CUDA-Q 的内核本质上是一个带 `operator()` 的可调用类型（callable）。
- **量子计算直觉**：量子比特（qubit）、量子门（gate，如 Hadamard `H`、泡利 `X`、受控非门 `CNOT`）、测量（measurement）、采样（shots）。如果你只听过名词、没写过代码，没关系，本讲会用最小例子把它们串起来。
- **承接 u1-l3**：你已经知道 `nvq++` 是把含 `__qpu__` 的 `.cpp` 一键编译成可执行程序的驱动脚本，默认落到 CPU 的 `qpp` 后端。本讲会反复用到这条命令。

> 关键回顾：在 CUDA-Q 里，量子比特是「不可拷贝」的，后端在「链接期」决定（你在源码里不写 `qpp`）。这两点在本讲的源码里都会看到。

## 3. 本讲源码地图

本讲涉及的关键文件，按「从用户写到运行时」的顺序排列：

| 文件 | 作用 |
| --- | --- |
| [runtime/cudaq.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq.h) | 用户总头文件，`#include <cudaq.h>` 即可获得内核所需的全部基础声明，并默认引入 `sample`。 |
| [runtime/cudaq/qis/qubit_qis.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h) | 「默认量子指令集（QIS）」：定义 `__qpu__` 宏，以及 `h/x/y/z/ry/mz` 等门函数。本讲的核心。 |
| [runtime/cudaq/qis/qudit.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h) | 单个量子比特类型 `qudit<Levels>` 与 `qubit = qudit<2>` 的定义，含分配/释放与「不可拷贝」约束。 |
| [runtime/cudaq/qis/qvector.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qvector.h) | 动态大小的量子比特容器 `cudaq::qvector`（类比 `std::vector`）。 |
| [runtime/cudaq/algorithms/sample.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h) | `cudaq::sample` 算法原语的模板实现，把内核跑很多次并汇总计数。 |
| [runtime/cudaq/algorithms/sample/options.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample/options.h) | 定义默认采样次数 `DEFAULT_NUM_SHOTS = 1000`。 |
| [runtime/common/SampleResult.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.h) | `sample_result` 结果对象：`dump()`、迭代器、`to_map()` 等读取接口。 |
| [docs/sphinx/examples/cpp/basics/expectation_values.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/expectation_values.cpp) | 官方示例：参数化内核 `ansatz`，演示 `qvector`、`x`、`ry`、`x<cudaq::ctrl>`。 |
| [docs/sphinx/examples/cpp/basics/static_kernel.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/static_kernel.cpp) | 官方示例：编译期定长 `ghz` 内核，演示 `qarray`、`h`、`mz(q)`、`cudaq::sample` 与结果遍历。 |

## 4. 核心概念与源码讲解

### 4.1 `__qpu__` 与内核结构体

#### 4.1.1 概念说明

在 CUDA-Q 里，一段「会在 QPU（或模拟器）上执行的量子代码」必须被标注出来，编译器才会对它做特殊处理（映射成 Quake 方言、注册到运行时、改写入口）。这个标注就是 `__qpu__`。

`__qpu__` 不是一个语言关键字，而是一个 GCC 属性宏：

```cpp
#define __qpu__ __attribute__((annotate("quantum")))
```

它给函数贴上一个名为 `"quantum"` 的注解（annotation）。Clang 前端在编译时会保留这个注解，CUDA-Q 的 AST 桥（后续单元 u4-l4 讲）识别到它，就把这个函数当作「量子内核」来翻译，而不是普通的 C++ 函数。

一个内核通常以「可调用结构体 + 带 `__qpu__` 的 `operator()`」来书写：

```cpp
struct ghz {
  auto operator()() __qpu__ { /* 量子代码写在这里 */ }
};
```

为什么用结构体而不是普通函数？因为内核经常需要带模板参数（如比特数 `N`）或作为对象传给 `cudaq::sample`，结构体形式更灵活，也方便编译器为每个特化生成独立的内核符号。

#### 4.1.2 核心流程

一个内核从「写出来」到「跑出结果」要经过：

1. **书写**：把量子逻辑写在带 `__qpu__` 的 `operator()` 里。
2. **标注**：`__attribute__((annotate("quantum")))` 让 Clang 在 AST 里保留 `"quantum"` 标记。
3. **桥接**：CUDA-Q 的 nvq++ 流程识别该标记，把函数体翻译成 Quake MLIR（量子中间表示）。
4. **注册**：生成的内核被注册到运行时，宿主代码（`main`）通过内核名找到它。
5. **执行**：`cudaq::sample` 等算法原语把内核交给平台/后端执行。

本讲只关心第 1、5 步；第 3、4 步属于编译器内部细节（u4 单元）。

#### 4.1.3 源码精读

`__qpu__` 宏的定义只有一行，但它是整个 CUDA-Q 编程模型的「开关」：

```cpp
// 量子内核的标注宏（runtime/cudaq/qis/qubit_qis.h:28）
#define __qpu__ __attribute__((annotate("quantum")))
```

它通过 [runtime/cudaq/qis/qubit_qis.h:L28-L28](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L28-L28) 提供，而你之所以 `#include <cudaq.h>` 就能用它，是因为总头文件 [runtime/cudaq.h:L14-L14](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq.h#L14-L14) 间接包含了 `cudaq/qis/qubit_qis.h`。

官方示例 `expectation_values.cpp` 展示了带参数的内核写法：

```cpp
// docs/sphinx/examples/cpp/basics/expectation_values.cpp:12-19
struct ansatz {
  auto operator()(double theta) __qpu__ {
    cudaq::qvector q(2);
    x(q[0]);
    ry(theta, q[1]);
    x<cudaq::ctrl>(q[1], q[0]);
  }
};
```

这段代码定义了一个名为 `ansatz` 的内核结构体，它的 `operator()` 接收一个经典参数 `double theta`（旋转角度），返回 `auto`（实际为 `void`）。注意三个细节：

- `__qpu__` 写在参数列表之后、函数体之前，修饰整个 `operator()`。
- 内核体里可以混用量子操作（`x`、`ry`、受控 `x`）与经典值（`theta`）。
- 内核**没有 `return`**——`cudaq::sample` 要求内核返回 `void`（见 4.3）。

第二个示例 `static_kernel.cpp` 展示了用模板把内核「在编译期定长」的写法：

```cpp
// docs/sphinx/examples/cpp/basics/static_kernel.cpp:11-23
template <std::size_t N>
struct ghz {
  auto operator()() __qpu__ {
    cudaq::qarray<N> q;
    h(q[0]);
    for (int i = 0; i < N - 1; i++) {
      x<cudaq::ctrl>(q[i], q[i + 1]);
    }
    mz(q);
  }
};
```

这里 `ghz<10>` 在编译期就确定了比特数为 10，使用编译期定长容器 `cudaq::qarray<N>`（类比 `std::array`），并且内核体里出现了 `for` 循环——这会被翻译成 Quake/CC 的循环结构（u4 单元详述）。

#### 4.1.4 代码实践

**目标**：亲手写一个带 `__qpu__` 的最小内核结构体，确认它能被 `nvq++` 识别（即不报「找不到内核」之类的错）。

**操作步骤**：

1. 新建文件 `hello_kernel.cpp`，写入以下内容（示例代码）：

   ```cpp
   #include <cudaq.h>

   struct hello {
     auto operator()() __qpu__ {
       cudaq::qvector q(1);
       x(q[0]);      // 把 |0> 翻成 |1>
       mz(q[0]);     // 测量
     }
   };

   int main() {
     auto counts = cudaq::sample(hello{});
     counts.dump();
     return 0;
   }
   ```

2. 编译：`nvq++ hello_kernel.cpp -o hello.x`
3. 运行：`./hello.x`

**需要观察的现象**：程序应打印一段采样分布。由于只对单比特先 `x` 后测量，结果几乎全部落在 `1`。

**预期结果**：约 1000 次采样全部（统计涨落允许极少量例外）为 `1`。

> ⚠️ 待本地验证：本讲所有「预期输出」均基于 API 语义推断；实际比特串的字符顺序约定（哪一端是高位）请以你机器上的真实输出为准。

#### 4.1.5 小练习与答案

**练习 1**：把 `__qpu__` 删掉再编译，会发生什么？

**参考答案**：`hello{}` 退化为普通 C++ 可调用对象。在 MLIR 模式下，CUDA-Q 编译器不会把它当量子内核翻译/注册，`cudaq::sample` 在查找内核符号时会失败，通常报「kernel not found / not generated」类错误。可见 `__qpu__` 是内核被识别的唯一入口。

**练习 2**：为什么内核的 `operator()` 要返回 `void`？

**参考答案**：量子内核的「输出」是测量结果，由运行时收集，而不是通过 C++ 返回值返回。`cudaq::sample` 的合法性检查 `SampleCallValid`（见 4.3）显式要求返回类型为 `void`，否则编译期就报错。

---

### 4.2 `qubit` / `qvector` 分配与门

#### 4.2.1 概念说明

要在内核里做量子计算，首先得「分配量子比特」。CUDA-Q 提供两种最基础的分配方式：

- **`cudaq::qubit`**：单个量子比特，构造时即向运行时申请一个全局唯一编号（`id`）。
- **`cudaq::qvector`**：动态大小的量子比特数组，构造时一次性申请 N 个，语义类似 `std::vector`，可用 `q[i]` 索引。

两个关键设计（承接 u1-l1）：

1. **不可拷贝、不可移动**：量子比特是物理资源，不能像 `int` 那样复制。源码里把拷贝/移动构造与赋值都 `= delete` 了。
2. **RAII 分配/释放**：构造时分配，析构时归还给运行时复用。

至于「门」，CUDA-Q 用普通 C++ 自由函数表示：`h(q)`、`x(q)`、`ry(角度, q)`、`mz(q)`。它们并不是真的在 CPU 上执行矩阵乘法，而是把「这条量子指令」记录到执行管理器（ExecutionManager），最终交给后端去解释执行。

#### 4.2.2 核心流程

以 `cudaq::qvector q(2); x(q[0]);` 为例：

1. `qvector(2)` 构造 → 内部创建 2 个 `qudit<2>` → 每个 `qudit` 构造时调用 `getExecutionManager()->allocateQudit(...)` 拿到唯一 `id`。
2. `q[0]` 返回第 0 个比特的引用。
3. `x(q[0])` 把门名 `"x"` 与目标比特 `id` 提交给 `getExecutionManager()->apply(...)`。
4. 内核结束、`q` 析构 → 每个比特调用 `returnQudit` 归还。

也就是说，门函数的「体」是一段薄薄的记录代码，真正干活的是后端的模拟器（u6 单元）。

#### 4.2.3 源码精读

**单个量子比特**。`qubit` 只是 `qudit<2>` 的别名——`qudit` 是通用的 d 能级量子系统，d=2 即量子比特：

```cpp
// runtime/cudaq/qis/qudit.h:73-74
// A qubit is a qudit with 2 levels.
using qubit = qudit<2>;
```

`qudit` 在构造时分配编号、析构时归还，并且禁止拷贝/移动（[runtime/cudaq/qis/qudit.h:L32-L46](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qudit.h#L32-L46)）：

```cpp
// 构造即申请一个全局唯一 id
qudit() : idx(getExecutionManager()->allocateQudit(n_levels())) {}
// ...
// Qudits cannot be copied
qudit(const qudit &q) = delete;
// qudits cannot be moved
qudit(qudit &&) = delete;
// ...
// 析构即归还
~qudit() { getExecutionManager()->returnQudit({n_levels(), idx}); }
```

这段代码就是「量子比特不可拷贝」约束的物理来源——它在 C++ 类型系统层面直接 `delete` 了拷贝/移动构造。

**量子比特数组**。`qvector` 把一组 `qudit` 装进 `std::vector`，同样禁用拷贝/移动，并提供 `operator[]`、`size()`、`slice()` 等（[runtime/cudaq/qis/qvector.h:L31-L37](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qvector.h#L31-L37)）：

```cpp
// runtime/cudaq/qis/qvector.h:31-32
/// @brief Construct a `qvector` with `size` qudits in the |0> state.
qvector(std::size_t size) : qudits(size) {}
```

```cpp
// runtime/cudaq/qis/qvector.h:57-64 —— 同样不可拷贝/移动
qvector(qvector const &) = delete;
qvector(qvector &&) = delete;
qvector &operator=(const qvector &) = delete;
```

**门函数是怎么生成的**。`h`、`x`、`y`、`z`、`t`、`s` 这些无参单比特门，并不是手写的六个函数，而是由宏 `CUDAQ_QIS_ONE_TARGET_QUBIT_` 批量展开出来的（[runtime/cudaq/qis/qubit_qis.h:L138-L174](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L138-L174)）：

```cpp
// runtime/cudaq/qis/qubit_qis.h:169-174
CUDAQ_QIS_ONE_TARGET_QUBIT_(h)
CUDAQ_QIS_ONE_TARGET_QUBIT_(x)
CUDAQ_QIS_ONE_TARGET_QUBIT_(y)
CUDAQ_QIS_ONE_TARGET_QUBIT_(z)
CUDAQ_QIS_ONE_TARGET_QUBIT_(t)
CUDAQ_QIS_ONE_TARGET_QUBIT_(s)
```

每个宏展开后生成的 `x(...)` 最终落到 `oneQubitApply`，后者把门名和比特 `id` 提交给执行管理器（[runtime/cudaq/qis/qubit_qis.h:L63-L85](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L63-L85)）：

```cpp
// mod == base 时：对所有传入比特逐个施加该门（广播）
if constexpr (std::is_same_v<mod, base>) {
  for (auto &qubit : quditInfos)
    getExecutionManager()->apply(gateName, {}, {}, {qubit});
  return;
}
```

带角度的旋转门（`rx/ry/rz/r1`）由另一组宏 `CUDAQ_QIS_PARAM_ONE_TARGET_` 生成（[runtime/cudaq/qis/qubit_qis.h:L246-L249](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L246-L249)），所以 `ry(theta, q[1])` 中的 `theta` 会被打包进 `parameters` 一并提交。

**受控门**。`x<cudaq::ctrl>(q[1], q[0])` 表示「以 `q[1]` 为控制、`q[0]` 为目标」的受控非门。它由同一个 `x` 函数模板配合修饰符 `cudaq::ctrl` 实现：当 `mod == ctrl` 时，`oneQubitApply` 会把「前 N-1 个比特当控制、最后一个当目标」（[runtime/cudaq/qis/qubit_qis.h:L89-L101](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L89-L101)）。此外文件还提供了语义糖 `cnot`、`cx`、`cy`、`cz` 等（[runtime/cudaq/qis/qubit_qis.h:L339-L346](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L339-L346)）：

```cpp
// runtime/cudaq/qis/qubit_qis.h:339-340
inline void cnot(qubit &q, qubit &r) { x<cudaq::ctrl>(q, r); }
inline void cx(qubit &q, qubit &r) { x<cudaq::ctrl>(q, r); }
```

**测量**。`mz(qubit&)` 在 Z 基下测量单比特并返回 0/1（[runtime/cudaq/qis/qubit_qis.h:L437-L444](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L437-L444)）；而 `mz(qvector)` 会遍历整个寄存器逐个测量（[runtime/cudaq/qis/qubit_qis.h:L474-L482](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L474-L482)）。注意源码注释特别说明：在 MLIR 模式下，`mz` 是「仅 `__qpu__` 内可用」的入口，桥会拦截内核内的每次 `mz` 调用并直接生成 `quake.mz` 操作，函数体本身在构建出的内核里并不会执行。

#### 4.2.4 代码实践

**目标**：体会「不可拷贝」约束，并理解门函数只是「记录指令」。

**操作步骤**：

1. 把下面这段（示例代码）写进 `no_copy.cpp`：

   ```cpp
   #include <cudaq.h>
   struct bad {
     auto operator()() __qpu__ {
       cudaq::qubit a;
       cudaq::qubit b = a; // 故意拷贝一个量子比特
       x(b);
       mz(b);
     }
   };
   int main() { cudaq::sample(bad{}).dump(); }
   ```

2. 编译：`nvq++ no_copy.cpp -o no_copy.x`

**需要观察的现象**：编译失败。报错应指向 `qudit` 的拷贝构造是被 `delete` 的（`qudit(const qudit &q) = delete;`）。

**预期结果**：编译期错误，信息中包含「deleted」「copy constructor」等字样。这验证了「量子比特不可拷贝」是由类型系统在编译期强制的，而不是运行时检查。

**进阶观察**：把 `cudaq::qubit b = a;` 改成 `cudaq::qvector r(2); x(r[0]); x(r[1]); mz(r);`，编译即可通过。说明容器内的多个比特是「申请+索引」使用，而非拷贝。

#### 4.2.5 小练习与答案

**练习 1**：`cudaq::qvector q(3); q[2]` 中的 `2` 是什么含义？

**参考答案**：它是容器内的局部下标（从 0 开始），`operator[]` 返回该位置上的 `qudit` 引用（见 [qvector.h:L73-L73](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qvector.h#L73-L73)）。每个比特还有一个由执行管理器分配的**全局 `id`**（`qudit::id()`），门函数内部用的其实是这个全局 `id`，两者不要混淆。

**练习 2**：为什么 `h/x/ry` 这些函数能在内核里「记录」指令，却不需要你显式传入一个 `ExecutionManager`？

**参考答案**：它们内部调用了 `getExecutionManager()`，这是一个返回当前线程上下文中单例执行管理器的访问器。所以门 API 表面上是「无状态自由函数」，底下都汇流到同一个执行管理器，由它统一收集指令再交给后端。

---

### 4.3 `cudaq::sample` 执行与结果读取

#### 4.3.1 概念说明

写好的内核不会自己跑——你需要一个「算法原语」来驱动它。最常用的就是 `cudaq::sample`：它把内核重复执行很多次（每次称为一个 **shot**），把每次测得的比特串汇总成一个「计数字典」`{ 比特串 → 出现次数 }`。

最简形式：

```cpp
auto counts = cudaq::sample(kernel{});     // 默认 1000 shots
```

返回的 `counts` 是 `sample_result` 类型，可以：

- `counts.dump()`：直接打印计数分布。
- 用范围 `for` 遍历 `auto &[bits, count] : counts`，逐条拿到比特串与次数。
- `counts.to_map()`：拿到底层 `unordered_map<string, size_t>`。

为什么是「采样」而不是「给出确定答案」？因为量子测量的本质是概率性的：一个处于叠加态的比特，单次测量只给出一个随机结果；只有重复很多次，才能估计出概率分布。这正是「采样次数（shots）」存在的意义。

#### 4.3.2 核心流程

`cudaq::sample(kernel, args...)` 的内部流程（简化）：

1. 通过 `SampleCallValid` 概念在编译期校验：`kernel` 可调用、参数匹配、返回 `void`。
2. 取得当前平台 `cudaq::get_platform()` 与内核名。
3. 创建名为 `"sample"` 的 `ExecutionContext`，写入 `shots`。
4. 在 `runSampling` 里循环：反复 `launch` 内核，直到累计的 shot 数达到目标值。
5. 把每次的测量结果累加进一个 `sample_result`，返回给用户。

默认 shot 数由 `DEFAULT_NUM_SHOTS` 决定：

\[ \text{总采样次数} = \text{DEFAULT\_NUM\_SHOTS} = 1000 \]

若想改次数，用带 `shots` 的重载：`cudaq::sample(100, kernel{})`。

#### 4.3.3 源码精读

**默认 shots 的来源**——一个 `constexpr` 常量（[runtime/cudaq/algorithms/sample/options.h:L14-L14](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample/options.h#L14-L14)）：

```cpp
// runtime/cudaq/algorithms/sample/options.h:14
constexpr int DEFAULT_NUM_SHOTS = 1000;
```

**最常用的 `sample` 重载**——只传内核与参数，使用默认 shots（[runtime/cudaq/algorithms/sample.h:L278-L294](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L278-L294)）：

```cpp
template <typename QuantumKernel, typename... Args>
  requires SampleCallValid<QuantumKernel, Args...>
sample_result sample(QuantumKernel &&kernel, Args &&...args) {
  auto &platform = cudaq::get_platform();
  auto kernelName = cudaq::getKernelName(kernel);
  return detail::runSampling(
      [&]() mutable { kernel(std::forward<Args>(args)...); }, platform,
      kernelName, /*shots=*/DEFAULT_NUM_SHOTS,
      /*explicitMeasurements=*/false);
}
```

注意它把内核包成一个无参 lambda 再传给 `runSampling`——这是 CUDA-Q 把「带经典参数的内核」适配成「可重复执行的裸内核」的统一手法。`SampleCallValid` 概念（[runtime/cudaq/algorithms/sample.h:L46-L49](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L46-L49)）在这里把关：参数合法且返回 `void` 才允许编译通过。

**采样主循环**——`runSampling` 反复启动内核直到凑够 shots（[runtime/cudaq/algorithms/sample.h:L154-L182](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L154-L182)）：

```cpp
cudaq::sample_result counts;
while (counts.get_total_shots() < static_cast<std::size_t>(shots)) {
  auto result = detail::launch(policy, qpu_id, ctx, platform,
                               std::forward<KernelFunctor>(wrappedKernel));
  // ...累加 result 到 counts...
}
return counts;
```

这段揭示了「采样 = 多次执行 + 累加」的本质；硬件后端只需启动一次（`is_emulated` 分支早返回），而模拟器后端则在循环里一轮一轮凑够 shot 数。

**结果对象**。`sample_result` 提供 `dump()` 与迭代器（[runtime/common/SampleResult.h:L222-L222](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.h#L222-L222) 与 [L266-L270](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/common/SampleResult.h#L266-L270)）：

```cpp
void dump() const;
// ...
CountsDictionary::iterator begin();
CountsDictionary::iterator end();
```

官方示例 `static_kernel.cpp` 完整演示了「执行 + 打印 + 遍历」三步（[docs/sphinx/examples/cpp/basics/static_kernel.cpp:L25-L40](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/static_kernel.cpp#L25-L40)）：

```cpp
int main() {
  auto kernel = ghz<10>{};
  auto counts = cudaq::sample(kernel);

  if (!cudaq::mpi::is_initialized() || cudaq::mpi::rank() == 0) {
    counts.dump();
    for (auto &[bits, count] : counts) {
      printf("Observed: %s, %lu\n", bits.data(), count);
    }
  }
  return 0;
}
```

这里的 MPI 判断只是多进程运行时只在 0 号进程打印的常见写法，单机运行时它等价于直接打印。

#### 4.3.4 代码实践

**目标**：对比不同 shot 数下采样分布的统计涨落。

**操作步骤**：把 4.1.4 里的 `hello` 内核换成对 `|0>` 直接测量（不加 `x`），分别用默认 1000 次和显式 100 次采样：

```cpp
// 示例代码 shots_compare.cpp
#include <cudaq.h>
struct zero {
  auto operator()() __qpu__ {
    cudaq::qvector q(1);
    mz(q[0]);          // 直接测量 |0>
  }
};
int main() {
  auto c1 = cudaq::sample(zero{});
  auto c2 = cudaq::sample(100, zero{});   // 显式 100 shots
  printf("1000 shots:\n"); c1.dump();
  printf("100 shots:\n");  c2.dump();
  return 0;
}
```

编译运行：`nvq++ shots_compare.cpp -o sc.x && ./sc.x`

**需要观察的现象**：两次都几乎全是 `0`，但 `get_total_shots` 分别约为 1000 与 100。

**预期结果**：直接测量 `|0>` 几乎必然得到 `0`，所以分布高度集中；这用来确认 shot 数确实受你传入的参数控制（默认 1000，显式 100）。**待本地验证**：精确的 `dump` 输出格式（表头、字符串方向）请以实际为准。

#### 4.3.5 小练习与答案

**练习 1**：`cudaq::sample(kernel{})` 与 `cudaq::sample(100, kernel{})` 在源码层面走的是同一个底层函数吗？

**参考答案**：是。两者最终都调用 `detail::runSampling(...)`，区别只在外层重载传入的 `shots` 不同——前者传 `DEFAULT_NUM_SHOTS`（1000），后者传你给的 `100`（见 [sample.h:L312-L325](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L312-L325)）。

**练习 2**：如果一个内核里完全没有 `mz`，`cudaq::sample` 会得到什么？

**参考答案**：没有任何测量结果，`runSampling` 的 shot 循环里 `counts.get_total_shots()` 会停留在 0。源码对此有保护：会打印 `WARNING: this kernel invocation produced 0 shots worth of results...` 并跳出循环（[sample.h:L171-L180](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/algorithms/sample.h#L171-L180)），避免死循环。可见「内核必须含测量」是采样的硬性前提。

---

## 5. 综合实践

把本讲三个模块（`__qpu__` 内核、`qvector` 与门、`sample` 与结果读取）串起来，亲手实现一个 **Bell 态（最大纠缠态）内核** 并验证它的采样分布。

**背景**：Bell 态的经典制备流程是 `|00> --H--> (|0>+|1>)/√2 ⊗ |0> --CNOT--> (|00>+|11>)/√2`。理论上的测量分布是 `00` 与 `11` 各约 50%，绝不应出现 `01` 或 `10`。

**任务步骤**：

1. 新建 `bell.cpp`（示例代码），完整内容如下：

   ```cpp
   #include <cudaq.h>

   struct bell {
     auto operator()() __qpu__ {
       cudaq::qvector q(2);
       h(q[0]);                       // 制备叠加
       x<cudaq::ctrl>(q[0], q[1]);    // CNOT: q[0] 控制 q[1]
       mz(q);                         // 测量两个比特
     }
   };

   int main() {
     auto counts = cudaq::sample(bell{});   // 默认 1000 shots
     counts.dump();
     printf("--- 逐条遍历 ---\n");
     for (auto &[bits, count] : counts) {
       printf("bits=%s count=%lu\n", bits.data(), count);
     }
     return 0;
   }
   ```

2. 编译运行：

   ```bash
   nvq++ bell.cpp -o bell.x && ./bell.x
   ```

3. **观察与记录**：
   - `dump()` 输出的分布里，`00` 与 `11` 应各占约一半（~500/500，统计涨落允许几十的偏差）。
   - `01`、`10` 应近似为 0（理想模拟器下应为 0）。
   - 把这两点与你跑出的真实数字对照，写下一行结论。

4. **延伸思考（可选）**：把 `x<cudaq::ctrl>(q[0], q[1])` 换成等价的 `cnot(q[0], q[1])`，重新编译运行，确认结果一致——这印证了 4.2 里 `cnot` 只是 `x<cudaq::ctrl>` 的语义糖。

> ⚠️ 本任务假设你已在 u1-l3 里完成了 `build_cudaq.sh` 构建，`nvq++` 可用、默认 `qpp` 后端已链接。若尚未构建，请先回到 u1-l3。精确的比特串表示（高位在左还是在右）请以本地输出为准——它不影响「`00`/`11` 各半」的物理结论。

## 6. 本讲小结

- `__qpu__` 是一个 `__attribute__((annotate("quantum")))` 宏，贴在可调用类型的 `operator()` 上，是 CUDA-Q 识别「量子内核」的唯一开关。
- 量子比特以 `cudaq::qubit`（= `qudit<2>`）或 `cudaq::qvector` 分配；它们在类型系统层面 `delete` 了拷贝/移动，构造即向执行管理器申请全局 `id`，析构即归还。
- 门函数（`h/x/y/z/ry/mz` 等）由宏批量生成，函数体只是把「门名 + 比特 id + 参数」记录到单例 `ExecutionManager`，真正执行交给后端。
- 受控门用 `op<cudaq::ctrl>(control..., target)` 表达；`cnot/cx/cy/cz` 是其语义糖。
- `cudaq::sample(kernel)` 把内核重复执行 `DEFAULT_NUM_SHOTS = 1000` 次，返回 `sample_result`；可用 `dump()` 或范围 `for` 遍历 `{ 比特串 → 次数 }`。
- 内核必须返回 `void`、且必须含测量，否则采样会因 0 shot 而报警跳出。

## 7. 下一步学习建议

- **下一讲 u1-l5（第一个 Python 量子内核）**：用 `@cudaq.kernel` 在 Python 端重写本讲的 Bell 内核，体会两套前端在 API 上的对应关系——它们最终复用同一个 C++ 运行时。
- **横向延伸 u2（编程模型）**：本讲只用到了 `qvector` 和最基础的 `ctrl` 修饰符。要系统了解 `qudit/qarray/qspan/qview` 的类型层次、`neg`（负控）、`apply` 算子与中途测量，请进入单元 u2，尤其是 u2-l1（量子类型系统）与 u2-l2（门与修饰符）。
- **继续读源码**：想看清「门函数记录的指令如何被后端执行」，可顺着 `getExecutionManager()->apply(...)`（[qubit_qis.h:L63-L118](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq/qis/qubit_qis.h#L63-L118)）跳进 `ExecutionManager` 与 `CircuitSimulator`（u3-l1 与 u6-l1）。
- **编译器侧**：好奇 `__qpu__` 注解如何被翻译成 Quake MLIR，可在学完 u1 后跳读 u4-l1（MLIR 总览）与 u4-l4（AST Bridge）。
