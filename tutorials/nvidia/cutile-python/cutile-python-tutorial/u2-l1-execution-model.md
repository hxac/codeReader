# 执行模型：grid、block 与执行空间

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚一个 cuTile **kernel** 是由 **grid** 中的若干 **block** 并行执行的，并能用 `ct.cdiv` 自己算出 grid 的三维尺寸。
- 理解 **block** 是「执行单元」、**tile** 是「数据单元」，二者不能混淆；并理解为什么 cuTile **不暴露单个线程**，这种抽象在硬件上映射到 CTA / warp。
- 区分 **host code（宿主代码）**、**SIMT code（单指令多线程代码）**、**tile code（tile 代码）** 三种执行空间，并知道哪些构造能在哪种空间里用。

本讲承接 [u1-l1](u1-l1-project-overview.md) 建立的「Python 内核 → AST → HIR → Tile IR → 字节码 → cubin → cuLaunchKernel」全链路直觉。那一讲关注「编译流程」，本讲关注「**运行起来之后，代码在什么样的抽象机器上执行**」。

## 2. 前置知识

在进入源码之前，先建立三个直觉。

**直觉一：GPU 并行的粒度。** NVIDIA GPU 的并行执行层次大致是：一个 kernel 启动后会创建大量 **block**（CUDA 里也叫 CTA，Cooperative Thread Array），每个 block 内部又由若干 **warp**（32 个线程为一组）组成。传统 CUDA（SIMT 模型）让你直接写「每个线程做什么」，需要用 `threadIdx`、`blockIdx`、`__syncthreads()` 这些线程级原语。

**直觉二：cuTile 抬高了抽象层级。** cuTile 不让你碰单个线程，而是让你写「**一个 block 整体处理一块数据（tile）**」。标量运算在 block 内的某条线程上串行执行；而 tile 运算则由 block 内所有线程**集体并行**完成。编译器负责把这种「集体运算」映射到具体的 warp 划分、张量核、TMA（Tensor Memory Accelerator）等硬件资源上。

**直觉三：代码运行在哪里。** 一段 cuTile 程序里既有「在 CPU 上跑的 Python」（host code），也有「在 GPU 上跑的 tile 代码」（tile code）。有的工具函数（如 `ct.cdiv`）两边都能用。区分「这段代码在哪个执行空间运行」是理解 cuTile 行为的关键。

> 名词提示：cuTile 文档里大量出现 `|blocks|`、`|grid|`、`|tile code|` 这种竖线标记。它们是 Sphinx 文档的术语替换（见 [references.rst](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/references.rst)），最终都会渲染成带超链接的术语。下文我们直接用中文术语。

## 3. 本讲源码地图

本讲主要围绕「执行模型」的概念性文档，辅以少量源码把概念钉死在真实代码上。

| 文件 | 作用 |
| --- | --- |
| [docs/source/execution.rst](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst) | 执行模型的权威定义：Abstract Machine、Execution Spaces、Tile Parallelism 等都在这里。 |
| [docs/source/index.rst](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/index.rst) | 项目总览，含 `vector_add` 内核示例，展示了 grid/launch 的实际用法。 |
| [src/cuda/tile/_execution.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py) | `kernel`/`function`/`stub` 三个装饰器的实现，是「执行空间」概念在代码里的落点。 |
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py) | `bid`/`num_blocks`/`cdiv` 等内置操作的签名定义，是 grid/block 概念在用户 API 上的体现。 |
| [src/cuda/tile/_cext.pyi](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_cext.pyi) | C++ 扩展的 Python 类型存根，定义了 `launch` 的签名和 `Dim3` 类型。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**grid（网格）**、**block（线程块）**、**execution space（执行空间）**。

---

### 4.1 grid：网格，一次 kernel 启动的 block 总数

#### 4.1.1 概念说明

**grid** 是一次 kernel 启动时所创建的全部 block 的集合，组织成 1D、2D 或 3D 的网格。当你调用 `ct.launch(stream, grid, kernel, args)` 时，第二个参数 `grid` 就是这个网格的形状——它决定了「这个 kernel 要被多少个 block 并行执行」。

grid 的每个维度是一个正整数，三个维度相乘就是 block 的总数。例如 `grid = (4, 2, 1)` 表示一共有 \(4 \times 2 \times 1 = 8\) 个 block。

#### 4.1.2 核心流程

如何决定 grid 的尺寸？典型做法是「数据总量」除以「每个 block 处理的 tile 大小」，并**向上取整**：

\[
\text{grid}[0] = \left\lceil \frac{N}{T} \right\rceil
\]

其中 \(N\) 是待处理元素总数，\(T\) 是 tile 大小（如 `TILE_SIZE`）。向上取整保证了即使最后一组不足一个 tile，也会有 block 去处理它。cuTile 提供了 `ct.cdiv` 来做这个运算：

```
# host 端计算 grid 的伪代码
grid = (ct.cdiv(N, TILE_SIZE), 1, 1)
ct.launch(stream, grid, kernel, (a, b, result))
```

流程上，`launch` 会把 `grid` 透传给底层 CUDA Driver API（`cuLaunchKernel`），由驱动在 GPU 上分发对应数量的 block。

#### 4.1.3 源码精读

文档对 grid 的定义只有一句话，但它是整个执行模型的起点：

[docs/source/execution.rst:L13-L14](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L13-L14) —— 一个 tile kernel 由组织成 1D/2D/3D grid 的逻辑线程 block 来执行。

`launch` 的签名定义在 C++ 扩展的类型存根里，第二个参数 `grid` 的类型是 `Dim3`：

[src/cuda/tile/_cext.pyi:L10-L18](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_cext.pyi#L10-L18) —— `Dim3` 是长度为 1、2 或 3 的整数元组；`launch(stream, grid, kernel, kernel_args, /)` 接收它作为网格形状。

`cdiv` 的签名证实它既能用在 host，也能用在 tile code 里（ceil 除法）：

[src/cuda/tile/_stub.py:L3478-L3484](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L3478-L3484) —— `cdiv(x, y)` 计算 \(\lceil x / y \rceil\)，文档明确「Can be used on the host」。

一个真实例子来自项目总览的 `vector_add`：

[docs/source/index.rst:L36-L39](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/index.rst#L36-L39) —— host 端用 `grid = (ct.cdiv(a.shape[0], TILE_SIZE), 1, 1)` 算出网格，再用 `ct.launch(...)` 启动。

#### 4.1.4 代码实践

**实践目标**：手算一个 1D 问题的 grid，并对照源码确认类型。

**操作步骤**：

1. 阅读 [index.rst 的 vector_add 示例](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/index.rst#L36-L39)，找到 `grid = (ct.cdiv(a.shape[0], TILE_SIZE), 1, 1)`。
2. 假设输入长度 \(N = 200\)，`TILE_SIZE = 16`，用 \(\lceil 200/16 \rceil\) 手算 `grid[0]`。
3. 打开 [_cext.pyi 的 Dim3 定义](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_cext.pyi#L10)，确认 `grid` 必须是长度 1~3 的整数元组。

**需要观察的现象 / 预期结果**：

- `grid[0] = 13`，因为 \(200 = 12 \times 16 + 8\)，余下的 8 个元素需要第 13 个 block 处理（这个 block 处理的是一个不足 16 元素的尾部 tile）。
- 若你把 `grid` 误传成 `(200,)`（每个元素一个 block），程序仍可能跑通但极度低效；若传成 `(12,)`，则会漏处理最后 8 个元素，结果错误。

> 待本地验证：实际数值正确性需要在装有 CUDA 的环境运行，本实践侧重「读懂源码 + 手算」。

#### 4.1.5 小练习与答案

**练习 1**：二维矩阵 \(M = 64 \times 48\)，每个 block 处理 \((16, 16)\) 的 tile，grid 应该是多少？

**答案**：`grid = (cdiv(64,16), cdiv(48,16), 1) = (4, 3, 1)`，共 12 个 block。

**练习 2**：`cdiv` 为什么能同时在 host code 和 tile code 里使用？（提示：见 execution.rst 关于跨执行空间构造的说明。）

**答案**：文档在执行空间一节明确指出，有些构造横跨多个执行空间，`cdiv` 就是 host code 和 tile code 都能用的例子（见 [execution.rst:L54-L55](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L54-L55)）。

---

### 4.2 block：执行单元，集体并行的载体

#### 4.2.1 概念说明

**block** 是 cuTile 的执行单元。grid 里的每一个 block 都会**完整地执行一遍 kernel 函数体**。但与 SIMT CUDA 不同的是，cuTile 把 block 当作一个整体来编程：

- **标量运算**：在 block 内的某条线程上串行执行（例如 `block_id = ct.bid(0)` 这种取编号、做整数加减）。
- **数组（tile）运算**：由 block 内所有线程**集体并行**完成（例如两个 tile 相加）。

最重要的一点：cuTile **只表达 block 级并行，不暴露 block 内的单个线程**。你写不出、也不需要写 `threadIdx`。编译器会自动把每个集体 tile 运算映射到合适的 warp 划分与硬件单元。

另一个关键区分：**block 是执行单元，tile 是数据单元，二者绝不能混淆**。一个 block 在一次 kernel 执行中可能操作多个来自不同 array 的、形状各异的 tile。

#### 4.2.2 核心流程

一个 block 执行 kernel 的过程可概括为：

1. 通过 `ct.bid(axis)` 获取自己在 grid 三个轴上的编号（0, 1, 2）。
2. 用这个编号去 `ct.load` 出自己负责的那块数据（tile）。
3. 对 tile 做集体运算。
4. 用 `ct.store` 把结果 tile 写回 array。

关于同步，cuTile 有一个反直觉但很重要的规则：**block 内部不允许显式同步或通信，但不同 block 之间允许**。这正好和传统 CUDA 相反（CUDA 里 `__syncthreads()` 是 block 内同步）。原因在于：cuTile 的 tile 运算本身就是「集体」的，编译器在每个集体运算的边界隐式保证了同步；而 block 之间则通过 cluster（CGA）、原子操作等机制显式协作。

#### 4.2.3 源码精读

block 的定义与「不暴露线程」「不允许 block 内同步」全部写在 Abstract Machine 这一节：

[docs/source/execution.rst:L18-L27](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L18-L27) —— 每个 block 在 GPU 的一个子集上运行；标量串行、数组集体并行；tile 程序只表达 block 级并行，不暴露单个线程；block 内不允许显式同步，但 block 之间允许。

「block 是执行单元、tile 是数据单元」的强调：

[docs/source/execution.rst:L29-L31](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L29-L31) —— 不要把 block 和 tile 混淆；一个 block 可操作来自不同 array 的多个不同形状的 tile。

`bid` 是 block 概念在用户 API 上的直接体现，它的轴取值 0/1/2 正好对应 grid 的三个维度：

[src/cuda/tile/_stub.py:L1090-L1113](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L1090-L1113) —— `bid(axis)` 返回当前 block 在指定轴上的编号，axis ∈ {0,1,2}。

配套的 `num_blocks` 告诉你某条轴上一共有多少个 block（即 grid 的尺寸，可在 kernel 内查询）：

[src/cuda/tile/_stub.py:L1116-L1124](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L1116-L1124) —— `num_blocks(axis)` 返回该轴上的 block 总数。

`kernel` 装饰器的 docstring 把「kernel 由每个 block 执行」写进了定义里：

[src/cuda/tile/_execution.py:L61-L72](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L61-L72) —— 「tile kernel 是由 grid 中每个 block 执行的函数」，且其执行空间只能是 tile code，不能从 host 直接调用。

#### 4.2.4 代码实践

**实践目标**：理解 block 内的「集体并行」与「禁止显式同步」。

**操作步骤**：

1. 阅读 [execution.rst:L18-L27](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L18-L27)。
2. 回顾一个传统 CUDA 内核：它通常以 `int tid = threadIdx.x + blockIdx.x * blockDim.x;` 开头，并用 `__syncthreads()` 在 block 内插栅栏。
3. 在 cuTile 的 [vector_add 内核](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/index.rst#L27-L33) 里寻找：有没有 `threadIdx`？有没有 `__syncthreads()`？

**需要观察的现象 / 预期结果**：

- 你会发现 cuTile 内核里既没有 `threadIdx` 也没有 `__syncthreads()`。`a_tile + b_tile` 这一句在概念上是「整个 block 的所有线程一起把两个 tile 对应元素相加」，同步由编译器在集体运算边界隐式完成。
- 写下一句话：**cuTile 用「集体运算的边界」替代了显式的 `__syncthreads()`**。

> 待本地验证：本实践是源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：在 cuTile 内核里，`ct.bid(0)` 返回的是「线程编号」还是「block 编号」？

**答案**：block 编号。cuTile 不暴露线程编号，`bid` 取的是当前 block 在 grid 第 0 轴上的索引。

**练习 2**：为什么 cuTile 禁止 block 内显式同步，却允许 block 之间同步？

**答案**：block 内的 tile 运算是「集体」的，编译器在每次集体运算边界已隐式同步，用户插入栅栏既无意义又会限制编译器自由；而不同 block 之间默认没有同步关系，需要时必须显式提供（如 cluster/CGA、原子操作）。

---

### 4.3 execution space：执行空间

#### 4.3.1 概念说明

**执行空间（execution space）** 指的是「一段构造可以在哪些 target 上使用」。一个 target 是由硬件资源与编程模型定义的执行环境。cuTile 定义了三种执行空间：

| 执行空间 | 含义 | 典型代码 |
| --- | --- | --- |
| **host code** | 所有 CPU target | 你写的 `vector_add(a, b, result)` 包装函数、`ct.cdiv`、`ct.launch` |
| **SIMT code** | 所有 CUDA SIMT target（旧称 device code） | cuTile 内部生成、用户一般不直接写 |
| **tile code** | 所有 CUDA tile target | `@ct.kernel` 装饰的函数体内部 |

一个构造可能横跨多个空间。例如 `ct.cdiv` 既能用在 host code，也能用在 tile code。而 `@ct.kernel` 装饰的函数体只能运行在 tile code。

如果一个函数的装饰器**显式声明**了它的执行空间，就称它为 **annotated function（已标注函数）**。

#### 4.3.2 核心流程

cuTile 通过装饰器来标注执行空间：

```
@ct.kernel                  # 入口，只能在 tile code 运行
def my_kernel(a, b):
    ...                     # 这一段是 tile code

@ct.function(host=True)     # 同时可在 host 与 tile 调用
def helper(x):
    ...

def host_wrapper(...):      # 普通函数，host code
    ct.launch(...)          # launch 只能在 host code 调用
```

要点：

- `kernel` 装饰的函数**不能直接调用**，必须用 `launch` 启动（这从代码层面强制了「kernel 只属于 tile code」）。
- `function(host=False)`（默认）表示该函数只能在 tile code 内被调用；若从 host 调用，会被转发到调度模式（DispatchMode）去处理。
- `function(host=True)` 表示该函数也能在 host code 调用，此时它就是个普通 Python 函数。

#### 4.3.3 源码精读

执行空间的三分类定义在文档里：

[docs/source/execution.rst:L34-L58](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L34-L58) —— 定义 host / SIMT / tile 三种执行空间，指出 SIMT code 旧称 device code（为避免歧义弃用旧称），并说明跨空间构造（如 `cdiv`）与 annotated function 概念。

执行空间概念在代码里的落点是 `function` 装饰器，它用 `host` 和 `tile` 两个参数标注可调用空间：

[src/cuda/tile/_execution.py:L25-L43](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L25-L43) —— `function(func, *, host=False, tile=True)` 的 docstring：标注一个函数可在哪些执行空间被调用；无参数时表示 tile-only。

装饰器的实现体进一步揭示了「host 调用 tile 函数」的转发机制：

[src/cuda/tile/_execution.py:L44-L53](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L44-L53) —— 当 `host=True` 时直接返回原函数（CPU 上当普通函数用）；否则包一层，从 host 调用时走 `DispatchMode.get_current().call_tile_function_from_host(...)`。

`kernel` 类则用 `__call__` 强制「不能直接调用」，从代码层面保证 kernel 只属于 tile code：

[src/cuda/tile/_execution.py:L169-L170](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L169-L170) —— 直接调用 kernel 会抛出 `TypeError`，提示用 `cuda.tile.launch()`。

#### 4.3.4 代码实践

**实践目标**：通过阅读装饰器源码，预测不同 `host` 取值下的可调用性。

**操作步骤**：

1. 阅读 [_execution.py 的 function 装饰器](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L25-L58)。
2. 对下面三种写法，预测「能否在 host 端直接调用 `f(1)`」：
   - `@ct.function`（即 `host=False, tile=True`）
   - `@ct.function(host=True)`
   - `@ct.kernel`（假设 `k` 是 kernel 对象）
3. 对照 [execution.rst 的执行空间定义](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L34-L58) 验证。

**需要观察的现象 / 预期结果**：

- `@ct.function`（`host=False`）：在 host 直接调用会被转发到 `DispatchMode`（用于静态求值等场景），而不是当普通 Python 函数执行。
- `@ct.function(host=True)`：返回原函数，可在 CPU 上当普通函数直接调用。
- `@ct.kernel`：直接调用 `k(...)` 会抛 `TypeError`，必须用 `launch`。

> 待本地验证：实际抛错与转发行为需在运行时确认；本实践侧重阅读源码分支。

#### 4.3.5 小练习与答案

**练习 1**：把 `ct.cdiv`、`ct.launch`、`ct.bid`、`ct.load` 分别归类到 host code / tile code / 两者皆可。

**答案**：`ct.cdiv` 两者皆可；`ct.launch` 只在 host code；`ct.bid` 只在 tile code；`ct.load` 只在 tile code。

**练习 2**：为什么 cuTile 弃用了「device code」这个旧称？

**答案**：为了避免歧义——cuTile 里既有 SIMT 的 device 概念，又有 tile 概念，统称 device code 会混淆，因此把 SIMT target 明确叫 SIMT code（见 [execution.rst:L49-L52](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L49-L52)）。

---

## 5. 综合实践

**任务**：用一段文字解释「为什么 cuTile 不暴露单个线程」，并指出这种抽象在硬件上映射到什么（CTA / warp）。

**操作步骤**：

1. 重读 [execution.rst 的 Abstract Machine](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L18-L27)，注意「scalar runs serially on a single thread, while array operations run collectively in parallel across all threads of the block」与「no exposure to individual threads」。
2. 打开 [kernel 装饰器的编译选项](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_execution.py#L74-L88)，阅读 `num_ctas`、`occupancy`、`num_worker_warps` 三个参数的说明。
3. 基于以上源码，写一段 150 字左右的解释，覆盖三个要点：
   - **为什么不暴露线程**：tile 运算是集体运算，暴露单线程会破坏集体语义、限制编译器自由。
   - **block 在硬件上映射到什么**：一个 cuTile block 对应一个 CTA（Cooperative Thread Array）。
   - **集体运算如何落到 warp**：编译器把每个 tile 运算映射到 CTA 内的若干 warp，甚至使用 warp-specialized（生产/消费 warp）、TMA、张量核——这正是 `num_worker_warps`、`occupancy` 这些 hint 存在的原因。

**预期结果（参考答案要点）**：

> cuTile 不暴露单个线程，是因为它把编程粒度从「线程」抬高到「block（CTA）」：标量运算串行、tile 运算由整个 CTA 集体完成。如果暴露单线程，用户就不得不手写 warp 划分与同步，这既破坏集体语义，也剥夺了编译器把 tile 映射到最优 warp 配置、张量核、TMA 的自由。在硬件上，一个 cuTile block 就是一个 CTA，内含若干 warp；编译器借助 `num_worker_warps`、`occupancy`、`num_ctas` 这些编译 hint 来决定 warp 数量、每 SM 占用率，以及把多个 CTA 组成 CGA（cluster）。因此「不暴露线程」不是功能缺失，而是把硬件映射权交给编译器。

> 待本地验证：本任务是源码阅读与写作型，无需运行；如需观察编译器实际生成的 warp 配置，可在后续学完 [u8-l5](u8-l5-debugging-and-performance.md) 的 dump 工具后，用 `CUDA_TILE_DUMP_TILEIR` 查看。

## 6. 本讲小结

- cuTile kernel 由组织成 1D/2D/3D **grid** 的若干 **block** 并行执行；grid 的尺寸用 `ct.cdiv(N, TILE_SIZE)` 向上取整计算，类型是 `Dim3`（1~3 元整数组）。
- **block** 是执行单元，**tile** 是数据单元，二者不能混淆；标量运算串行、tile 运算集体并行；block 内禁止显式同步（集体运算边界已隐式同步），block 之间允许同步。
- cuTile **不暴露单个线程**，一个 block 在硬件上映射到一个 CTA，集体 tile 运算由编译器映射到内部的 warp / 张量核 / TMA。
- 三种**执行空间**：host code（CPU）、SIMT code（CUDA SIMT，旧称 device code）、tile code（CUDA tile）；`ct.cdiv` 跨 host 与 tile，`ct.launch` 仅 host，`ct.bid`/`ct.load` 仅 tile。
- `function` 装饰器用 `host`/`tile` 参数标注执行空间；`kernel` 用 `__call__` 抛错来强制「必须用 launch 启动」。

## 7. 下一步学习建议

本讲建立了「执行在什么上跑」的直觉。接下来应该建立「**数据长什么样**」的直觉，建议依次学习：

- [u2-l2 数据模型：全局数组 Array](u2-l2-data-model-array.md)：理解 array 的 strided 布局、如何通过 DLPack / CUDA Array Interface 把宿主张量传进内核。
- [u2-l3 数据模型：Tile、Scalar 与形状广播](u2-l3-data-model-tile.md)：理解 tile 的不可变性、每维为 2 的幂、以及广播规则——这是与本讲「集体并行」直接对应的数据载体。
- 之后再进入 [u3 用户 API 实战](u3-l1-load-store-pattern.md)，亲手写出第一个 load–compute–store 内核。

如果想提前看执行模型在文档里的完整原文，直接通读 [docs/source/execution.rst](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst) 即可。
