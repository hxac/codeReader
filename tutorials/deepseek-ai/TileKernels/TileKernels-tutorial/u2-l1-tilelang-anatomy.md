# TileLang 算子解剖：jit + prim_func + 动态符号

## 1. 本讲目标

第 1 单元我们建立了对 TileKernels 的整体认知：它是一个用 **TileLang DSL** 编写的高性能 GPU 算子库，每个算子家族由「TileLang kernel + Python wrapper」组成。本讲是第 2 单元的地基，我们要把一个 TileLang 算子「开膛破肚」，看清它由哪些固定零件拼成。

学完本讲，你应该能够：

1. 说出 TileLang 算子的**两层结构**：被 `@tilelang.jit` 装饰的「kernel 构造函数」+ 内部用 `@T.prim_func` 定义的「内核函数」。
2. 区分**编译期参数**（静态、被烤进编译产物）与**运行时维度**（用 `T.dynamic` 声明的符号），并理解这样设计的原因。
3. 会用 `T.Kernel(..., threads=N) as (...)` 定义网格（grid）与每块线程数（threads），并理解 program id 的绑定。
4. 读懂 wrapper 函数如何把一个 PyTorch 张量「翻译」成一次 kernel 启动。

本讲只讲**骨架**，不深入存储层级（那是 u2-l2）和循环/规约原语（那是 u2-l3）。把骨架记住，后面所有算子都是在往这个骨架里填肉。

## 2. 前置知识

### 2.1 什么是「DSL + JIT」

TileLang 是一种 **DSL（Domain-Specific Language，领域特定语言）**。它不是一个独立语言，而是嵌在 Python 里的一组 API（通过 `from tilelang import language as T` 引入）。你用这套 API 写出的 Python 函数，描述的是「一个 GPU kernel 应该做什么」，而不是普通的 Python 计算。

**JIT（Just-In-Time，即时编译）** 的意思是：这个描述性函数**不会在你 import 时就被执行**，而是在你**第一次真正调用它、给出具体的 tile 大小等参数时**，才被编译成一段真正的 CUDA 代码。这和 PyTorch 的 eager（立即执行）模式完全不同。

### 2.2 为什么需要「编译期 vs 运行时」两个层次

GPU kernel 追求极限性能。像 TVM、Triton、TileLang 这类工具的核心思路是：**把一部分形状信息在编译期固定下来**（例如一个 tile 是 `128×128`），编译器就能据此展开循环、分配寄存器、生成向量化指令；而**把另一部分形状信息留到运行时**（例如总共有多少个 token、多少个 batch），这样同一段编译产物能复用到很多不同的实际输入上。

这个「哪些固定、哪些留运行时」的切分，正是本讲要讲清的 `@tilelang.jit` 构造参数与 `T.dynamic` 的分工。

### 2.3 CUDA 里的两个基本概念

- **grid（网格）**：一次 kernel 启动会启动很多个「块（block）」，这些块排成的阵列叫 grid。grid 的每个维度对应一个 `program id`，TileLang 里写作 `pid`。
- **threads（线程）**：每个块内部有若干线程并行工作，数量由 `threads=` 指定。

如果你完全没接触过 CUDA，可以暂时把「块」理解成「一组工人」，grid 是「工人的方阵」，`pid` 是「这个工人在方阵里的坐标」。

## 3. 本讲源码地图

本讲精读两个文件，它们是 TileKernels 里最典型、也最简洁的两个算子：

| 文件 | 作用 | 本讲用它说明什么 |
|------|------|------------------|
| [tile_kernels/transpose/batched_transpose_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py) | 批量矩阵转置 kernel 及 wrapper | 多维 `T.dynamic`、3D 网格、StridedTensor 形参 |
| [tile_kernels/moe/topk_gate_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py) | MoE top-k 门控 kernel 及 wrapper | 单个 `T.dynamic`、1D 网格、最简 wrapper |

辅助阅读（理解 wrapper 调用链，已在 u1-l3 讲过，这里只复用结论）：

- [tile_kernels/transpose/__init__.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/__init__.py)：从 `batched_transpose_kernel` 再导出 `transpose, batched_transpose`。
- [tile_kernels/moe/__init__.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/__init__.py)：从 `topk_gate_kernel` 再导出 `topk_gate`。
- [tile_kernels/utils.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/utils.py)：提供 `align`（向上取整对齐），`topk_gate` 用它把专家数对齐到 32。

## 4. 核心概念与源码讲解

### 4.1 两层结构：kernel 构造函数 + @T.prim_func

#### 4.1.1 概念说明

一个 TileLang 算子的源码，从外到内固定是「三层套娃」：

1. **最外层：`@tilelang.jit(...)` 装饰器**。它告诉 TileLang「被装饰的函数不是一个普通 Python 函数，而是一个 kernel 的构造器」，并且可以传入编译开关（`pass_configs`）。
2. **中间层：kernel 构造函数**（如 `get_topk_gate_kernel`、`get_batched_transpose_kernel`）。它的**入参是编译期参数**（tile 大小、dtype 等），函数体里先声明运行时符号，最后 `return` 一个内核函数。调用它 = 触发一次 JIT 编译，返回的是一个**可调用的、已编译的 kernel 对象**。
3. **最内层：`@T.prim_func` 内核函数**（如 `topk_gate_kernel`、`batched_transpose_kernel`）。它描述「每个块要做什么」，形参是若干 `T.Tensor`。这才是真正会被编译成 CUDA 的部分。

一句话区分：**构造函数的参数是「烤进编译产物的常量」；`@T.prim_func` 的参数是「启动时传入的张量」。**

#### 4.1.2 核心流程

以 `topk_gate` 为例，从源码到运行的整体流程：

```text
@tilelang.jit(...)                      # ① 装饰器：标记为 JIT 构造器
def get_topk_gate_kernel(num_experts, num_topk):   # ② 构造函数（编译期参数）
    num_tokens = T.dynamic('num_tokens')           #   声明运行时符号
    ...
    @T.prim_func                                   # ③ 内核函数（启动时张量）
    def topk_gate_kernel(scores: T.Tensor[...], topk_idx: T.Tensor[...]):
        with T.Kernel(...) as pid:                 #   网格 + 线程
            ...                                    #   每个块的计算
    return topk_gate_kernel                        #   返回编译后的 kernel 对象

# wrapper 里：
kernel = get_topk_gate_kernel(num_experts, num_topk)  # 触发编译，拿到 kernel 对象
kernel(scores, topk_idx)                              # 启动：把张量喂给内核函数
```

#### 4.1.3 源码精读

先看 `topk_gate` 最典型的三层骨架：

[ tile_kernels/moe/topk_gate_kernel.py:L10-L25 ](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L10-L25) —— 装饰器 + 构造函数 + 内核函数签名。

```python
@tilelang.jit(
    pass_configs={
        tilelang.PassConfigKey.TL_DISABLE_WARP_SPECIALIZED: True,
    },
)
def get_topk_gate_kernel(num_experts: int, num_topk: int):
    num_tokens = T.dynamic('num_tokens')
    ...
    @T.prim_func
    def topk_gate_kernel(
        scores: T.Tensor[(num_tokens, num_experts), T.float32],
        topk_idx: T.Tensor[(num_tokens, num_topk), T.int64],
    ):
```

要点：

- `num_experts`、`num_topk` 是构造函数的**普通 Python 入参**，它们是编译期常量，会直接出现在 `T.Tensor[(num_tokens, num_experts), ...]` 这样的形状里。
- `pass_configs` 里的 `TL_DISABLE_WARP_SPECIALIZED: True` 是一个编译开关，关掉了 TileLang 默认的 warp 特化优化（本讲不展开，u10-l2 会集中讲 `pass_configs`）。
- `@T.prim_func` 下的 `topk_gate_kernel` 的两个形参 `scores`、`topk_idx` 才是启动时由 wrapper 传入的真实张量。

转置算子是同一个套路，只是构造函数的编译期参数变成了「形状对 128 取模」和 dtype：

[ tile_kernels/transpose/batched_transpose_kernel.py:L17-L28 ](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L17-L28) —— 装饰器 + 构造函数签名 + 第一行 dynamic 声明。

```python
@tilelang.jit(
    pass_configs={
        tilelang.PassConfigKey.TL_DISABLE_WARP_SPECIALIZED: True,
    },
)
def get_batched_transpose_kernel(shape_x_mod_128: int, shape_y_mod_128: int, dtype: T.dtype):
    assert shape_x_mod_128 in (0, 64) and shape_y_mod_128 in (0, 64)
    # Runtime symbols
    num_batches = T.dynamic('num_batches')
```

注意 `dtype` 也是编译期参数——不同的数据类型（如 float16 vs bfloat16）会编译出**不同**的 kernel 产物。这正是 TileLang 「按 tile 与类型特化」的设计。

#### 4.1.4 代码实践

**实践目标**：用眼睛把「三层套娃」在两个文件里各走一遍，确认你能指认出哪一行是装饰器、构造函数、内核函数。

**操作步骤**：

1. 打开 `tile_kernels/moe/topk_gate_kernel.py`，定位 `@tilelang.jit`、`def get_topk_gate_kernel`、`@T.prim_func`、`def topk_gate_kernel`、`return topk_gate_kernel` 这五个标记，记下它们的行号。
2. 打开 `tile_kernels/transpose/batched_transpose_kernel.py`，重复一遍，记下五个标记的行号。
3. 对照下表，确认两个文件的「编译期参数」分别是什么。

**需要观察的现象**：两个文件的「形状」完全一致——都是「装饰器 → 构造函数(编译期参数) → prim_func → return」。这就是 TileKernels 里所有算子的统一骨架。

**预期结果**：

| 标记 | topk_gate | batched_transpose |
|------|-----------|-------------------|
| 编译期参数 | `num_experts, num_topk` | `shape_x_mod_128, shape_y_mod_128, dtype` |
| 内核函数名 | `topk_gate_kernel` | `batched_transpose_kernel` |

#### 4.1.5 小练习与答案

**练习 1**：如果把 `get_topk_gate_kernel(64, 6)` 和 `get_topk_gate_kernel(128, 8)` 各调用一次，TileLang 会编译出几个 kernel 产物？

**参考答案**：2 个。编译期参数不同，每次调用构造函数都会触发一次特化编译，得到各自独立的 kernel 对象。这也是为什么 wrapper 里只对「需要特化的量」传构造函数，而把 token 数等留给运行时。

**练习 2**：为什么 `dtype` 要作为构造函数参数（编译期），而不是运行时再决定？

**参考答案**：因为不同的 dtype（如 float16 / bfloat16 / float32）对应不同的寄存器分配、向量化宽度、指令选择，编译器需要在编译期就知道 dtype 才能生成最优的 CUDA 代码。运行时切 dtype 会丢失特化机会，性能下降。

---

### 4.2 T.dynamic：声明运行时维度（动态符号）

#### 4.2.1 概念说明

`T.dynamic('名字')` 会创建一个**运行时符号**（symbol）。它看起来像一个整数变量，但它的值在**编译期是未知的**，只有在 kernel 真正启动、收到具体张量时才被确定。

它的用途只有一个：**出现在 `T.Tensor` 的形状或步长里**，表示「这一维的大小启动时才知道」。这样一段编译产物就能服务于「任意 token 数」「任意 batch 数」的输入，而不必为每个具体数值重新编译。

要注意：`T.dynamic` 声明的是「符号」，不是「实际数值」。你不能对它做需要确切数值的 Python 判断（比如不能写 `if num_tokens > 0:` 来分支——那是 wrapper 的职责），但可以在形状表达式、`T.Kernel` 网格表达式里自由使用它。

#### 4.2.2 核心流程

```text
声明：num_tokens = T.dynamic('num_tokens')     # 创建一个运行时符号
使用：T.Tensor[(num_tokens, num_experts), ...] # 用在张量形状里
     with T.Kernel(num_tokens, ...) as pid:    # 用在网格维度里
启动：kernel(scores, topk_idx)                  # 真实 num_tokens 由张量形状提供
```

#### 4.2.3 源码精读

**topk_gate 只有一个动态维度** —— token 数：

[ tile_kernels/moe/topk_gate_kernel.py:L16 ](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L16) —— 声明 `num_tokens` 这个运行时符号。

```python
num_tokens = T.dynamic('num_tokens')
```

随后它同时出现在张量形状和网格维度里（下一节会看到 `T.Kernel(num_tokens, ...)`）。专家数 `num_experts` 没有用 `T.dynamic`，因为它是编译期参数。

**batched_transpose 有四个动态维度**，是更完整的例子：

[ tile_kernels/transpose/batched_transpose_kernel.py:L25-L28 ](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L25-L28) —— 声明四个运行时符号。

```python
num_batches = T.dynamic('num_batches')
shape_x = T.dynamic('shape_x')
shape_y = T.dynamic('shape_y')
stride_x = T.dynamic('stride_x')
```

其中 `stride_x` 尤其值得注意：它不是「某一维的大小」，而是输入张量在某一维上的**步长**（stride），用于描述非连续内存布局。它会出现在 `T.StridedTensor` 的步长元组里：

[ tile_kernels/transpose/batched_transpose_kernel.py:L39-L42 ](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L39-L42) —— 动态符号同时用于形状和步长。

```python
def batched_transpose_kernel(
    x: T.StridedTensor[(num_batches, shape_x, shape_y), (shape_x * stride_x, stride_x, 1), dtype],
    out: T.Tensor[(num_batches, shape_y, shape_x), dtype],
):
```

`T.StridedTensor[(形状), (步长), dtype]` 比 `T.Tensor` 多了一个步长元组，`stride_x` 这个动态符号让同一段编译产物能处理「行间隔不同」的各种输入（步长的深入含义见 u2-l2 与 u10-l2）。

#### 4.2.4 代码实践

**实践目标**：理解「动态符号可以出现在形状表达式里参与运算」。

**操作步骤**：

1. 在 `batched_transpose_kernel.py` 里找到 `out: T.Tensor[(num_batches, shape_y, shape_x), dtype]`。
2. 注意输出形状的三个维度全部是动态符号，且 `shape_y`、`shape_x` 与输入的顺序对调了（这就是「转置」在形状层面的体现）。
3. 再找到 `T.Kernel(shape_y // block_y, shape_x // block_x, num_batches, ...)`（下一节），观察动态符号可以和编译期常量一起做整除 `//`，组成网格表达式。

**需要观察的现象**：动态符号既能直接当维度，也能进入算术表达式（`shape_x * stride_x`、`shape_y // block_y`）。编译器把这些表达式当作「启动时再求值」的公式保留下来。

**预期结果**：你会确认，一段编译产物并不固定输入到底是 `256×512` 还是 `1024×64`，只要满足「能被 tile 整除」等约束，都能用同一个 kernel。约束本身用 `T.assume` 告知编译器（见 [L49-L51](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L49-L51)）。

#### 4.2.5 小练习与答案

**练习 1**：在 `topk_gate` 里，为什么 `num_experts` 不是 `T.dynamic`，而 `num_tokens` 是？

**参考答案**：因为 `num_experts`（专家总数）在模型配置里通常是固定的，把它作为编译期参数能让 kernel 针对「专家数对齐到 32 后的具体值」做特化优化（见 wrapper 里 `align(num_experts, num_threads)`）。而 `num_tokens`（token 数）每次前向都可能变化，留作运行时符号可以避免每换个 batch size 就重新编译。

**练习 2**：下面这段伪代码哪里不对？`num_tokens = T.dynamic('num_tokens'); if num_tokens > 0: kernel(...)`

**参考答案**：`T.dynamic` 创建的是编译期未知的符号，不能在 Python 层面对它做 `if num_tokens > 0` 这种需要确切数值的判断。「token 数为 0 时跳过」这类守卫应该在 **wrapper** 里用真实张量的 shape 来做，事实上 `topk_gate` 的 wrapper 正是这么做的（见 4.4.3）。

---

### 4.3 T.Kernel：网格与线程块

#### 4.3.1 概念说明

`with T.Kernel(...) as (...):` 这一行，把内核函数的计算**映射到 GPU 的并行结构**上。它做两件事：

1. **定义 grid（网格）**：圆括号里的若干个表达式就是 grid 的各个维度，每个维度对应一个 program id。grid 维度的总数启动时由动态符号确定。
2. **定义 threads（每块线程数）**：`threads=N` 指定每个块（block）里有 N 个线程。

`as (...)` 把这些 program id 绑定到变量名上，供 with 块内部使用——它就是「我这个块在网格里的坐标」。

可以把它理解为一条 for 循环的并行展开：grid 有多少个块，就相当于「并行地跑了那么多份 with 块里的代码」，每份代码通过自己的 `pid` 知道自己处理哪一份数据。

#### 4.3.2 核心流程

```text
一维网格：with T.Kernel(num_tokens, threads=32) as pid:        # 每个 token 一个块
三维网格：with T.Kernel(gy, gx, gb, threads=256) as (py, px, pb): # 三层并行
```

- grid 维度数 = `as` 后绑定变量的个数。
- 每个 grid 维度的表达式 = 「该方向上的总块数」，可以是动态符号、编译期常量或它们的算术组合。
- `threads=` 是关键字参数，单独指定。

#### 4.3.3 源码精读

**topk_gate 用一维网格**，每个 token 分配一个块（一个 warp，32 线程）：

[ tile_kernels/moe/topk_gate_kernel.py:L25 ](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L25) —— 一维网格，每块 32 线程。

```python
with T.Kernel(num_tokens, threads=num_threads) as pid:
```

这里 `num_tokens` 既是张量形状，也是 grid 维度——意味着「启动 `num_tokens` 个块，每个块处理一行（一个 token）」。`num_threads = 32` 在构造函数里定义，是一个 warp 的大小（warp 相关原语见 u2-l3 / u5-l2）。

**batched_transpose 用三维网格**，按「行块 × 列块 × batch」三方向并行：

[ tile_kernels/transpose/batched_transpose_kernel.py:L43 ](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L43) —— 三维网格，每块 256 线程。

```python
with T.Kernel(shape_y // block_y, shape_x // block_x, num_batches, threads=num_threads) as (pid_y, pid_x, pid_batch):
```

三个 grid 维度分别是：

- `shape_y // block_y`：输出在「行方向」上分了多少个 `block_y` 大小的块；
- `shape_x // block_x`：「列方向」上分了多少个 `block_x` 大小的块；
- `num_batches`：batch 维。

三者都是「动态符号 ÷ 编译期 tile 常量」的表达式。绑定到 `(pid_y, pid_x, pid_batch)` 后，with 块内部就用这三个 pid 计算自己负责的数据偏移（如 `pid_x * block_x + i`）。

对照看，`num_threads` 的取值也不同：转置用 256（一个大块处理一个 `block_y × block_x` 的 tile），topk 用 32（一个 warp 处理一行）。threads 的选择与 tile 形状、每个线程要做的工作量挂钩，属于调优范畴（u10-l1）。

#### 4.3.4 代码实践

**实践目标**：建立「grid 维度数 ↔ as 绑定变量数 ↔ 数据并行方向」的一一对应直觉。

**操作步骤**：

1. 在 topk_gate 里把 `with T.Kernel(num_tokens, threads=32) as pid:` 的 `num_tokens` 改成观察对象——它为什么等于「token 数」？因为每个块处理一行 `scores[pid, :]`（见 [L34](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L34) `scores[pid, i]`）。
2. 在 batched_transpose 里确认 `(pid_y, pid_x, pid_batch)` 三个 pid 各自负责哪个方向（行 / 列 / batch）。
3. 数一下：两个文件里 `with T.Kernel(...)` 的圆括号里有几项，`as (...)` 里就有几个变量，二者必须相等。

**需要观察的现象**：grid 维度个数和 `as` 绑定个数严格相等；多一个少一个都会让语义错乱。

**预期结果**：

| 算子 | grid 维度数 | as 绑定 | threads |
|------|------------|---------|---------|
| topk_gate | 1 (`num_tokens`) | `pid` | 32 |
| batched_transpose | 3 (`gy, gx, gb`) | `pid_y, pid_x, pid_batch` | 256 |

#### 4.3.5 小练习与答案

**练习 1**：如果要把 `topk_gate` 改成「8 个 token 一组、一组一个块」的粒度，`T.Kernel` 这一行大概会变成什么样？

**参考答案**：`with T.Kernel(num_tokens // 8, threads=...) as pid:`，然后在 with 块内部用一个循环处理组内的 8 个 token（例如 `for t in range(8): row = pid * 8 + t`）。grid 维度从「token 数」变成「组数」，每个块的工作量相应变大。这是「网格粒度 vs 每块工作量」的权衡。

**练习 2**：`batched_transpose` 的 `threads=256` 和 `topk_gate` 的 `threads=32`，为什么差这么多？

**参考答案**：threads 数要匹配「每个块要处理的 tile 大小」和「每个线程要干多少活」。转置一个 `block_y × block_x = 128×128` 的 tile 需要较多线程协作搬运数据，故用 256；topk 一行最多对齐到 32 个专家（一个 warp），32 线程刚好覆盖一行，再多反而浪费。

---

### 4.4 wrapper：从 Python 张量到 kernel 启动

#### 4.4.1 概念说明

构造函数和内核函数加起来，描述了「编译期/运行时怎么分、kernel 内部怎么算」。但**用户不会直接调用它们**——用户调用的是 **wrapper**（如 `topk_gate(scores, num_topk)`、`batched_transpose(x)`）。

wrapper 是一段普通 Python 代码，职责是把「PyTorch 世界」翻译成「TileLang 启动」。它固定做四件事：

1. **校验**：断言输入的维度、连续性、dtype 符合 kernel 的假设。
2. **分配输出**：用 `torch.empty` 按正确的形状/dtype/device 预留输出张量。
3. **触发编译**：调用构造函数，传入编译期参数，拿到 kernel 对象。
4. **启动**：调用 `kernel(输入..., 输出...)`，把张量按 `@T.prim_func` 形参顺序喂进去。

wrapper 还经常做一个小优化：当输入规模为 0 时直接返回空输出，**跳过 kernel 启动**（这是上一节练习 2 提到的「`num_tokens > 0` 守卫应该在 wrapper 里做」的真实例子）。

回顾 u1-l3：wrapper 才是用户真正调用入口，包入口 `__init__.py` 也是从 `*_kernel.py` 再导出 wrapper（而不是导出 kernel 对象）。

#### 4.4.2 核心流程

```text
def wrapper(张量, ...):           # 用户入口
    assert 校验
    out = torch.empty(...)         # ① 分配输出
    if 规模为 0: return out         # ② 跳过启动（可选）
    kernel = get_xxx_kernel(编译期参数)  # ③ 触发 JIT 编译
    kernel(输入, out)               # ④ 启动，张量按 prim_func 形参顺序传入
    return out
```

#### 4.4.3 源码精读

**topk_gate 的 wrapper 是最简形态**，四步齐全且最短：

[ tile_kernels/moe/topk_gate_kernel.py:L77-L90 ](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L77-L90) —— 断言 → 分配 → 跳过空 → 编译 → 启动。

```python
    assert scores.dim() == 2 and scores.is_contiguous() and scores.dtype == torch.float32
    num_tokens, num_experts = scores.shape
    assert num_topk <= num_experts, ...
    topk_idx = torch.empty((num_tokens, num_topk), dtype=torch.int64, device=scores.device)
    if num_tokens == 0:
        return topk_idx

    kernel = get_topk_gate_kernel(num_experts, num_topk)

    if int(os.getenv('TK_PRINT_KERNEL_SOURCE', 0)):
        print(kernel.get_kernel_source())

    kernel(scores, topk_idx)
    return topk_idx
```

注意几个对应关系：

- `get_topk_gate_kernel(num_experts, num_topk)`：只把**编译期参数**（专家数、top-k 数）喂给构造函数，`num_tokens` 不传——它由张量形状在启动时提供。
- `kernel(scores, topk_idx)`：实参与 `@T.prim_func` 的形参 `scores, topk_idx` **按位置对应**。
- `if num_tokens == 0: return`：正是 4.2 练习里说的「运行时守卫在 wrapper 做」。
- `TK_PRINT_KERNEL_SOURCE`（u1-l2 讲过）：打开后可打印 TileLang 生成的 CUDA 源码，方便观察「编译产物长什么样」。

**batched_transpose 的 wrapper 多了两处特化**：编译期参数是对形状取模，以及对非连续输入的步长校验。

[ tile_kernels/transpose/batched_transpose_kernel.py:L104-L119 ](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L104-L119) —— 转置 wrapper 的四步。

```python
    assert x.dim() == 3
    num_batches, shape_x, shape_y = x.shape

    assert shape_x % 64 == 0 and shape_y % 64 == 0 and x.stride(-2) % 4 == 0 and x.stride(-1) == 1

    # Get kernel implement
    kernel = get_batched_transpose_kernel(shape_x % 128, shape_y % 128, T.dtype(x.dtype))
    ...
    out = torch.empty((num_batches, shape_y, shape_x), dtype=x.dtype, device='cuda')
    if num_batches > 0 and shape_x > 0 and shape_y > 0:
        kernel(x, out)

    return out
```

关键对比：

- **编译期参数是 `shape_x % 128, shape_y % 128`**：转置只把「形状对 128 取模后的余数（只能是 0 或 64）」作为编译期信息，而非完整形状。这样 `64, 192, 320, ...` 这些「mod 128 == 64」的形状共用同一份编译产物——这就是「编译期 vs 运行时」切分在转置里的精妙体现。
- 输出形状 `(num_batches, shape_y, shape_x)` 与输入的 `shape_x, shape_y` 顺序对调，呼应「转置」。
- 同样有 `if num_batches > 0 and shape_x > 0 and shape_y > 0` 的运行时守卫。
- `transpose`（[L79-L91](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L79-L91)）只是 `batched_transpose` 的 2D 适配壳：`unsqueeze(0)` → `batched_transpose` → `squeeze(0)`。

#### 4.4.4 代码实践

**实践目标**：把「构造函数形参 ↔ wrapper 传入的编译期量」「prim_func 形参 ↔ kernel 启动时传入的张量」这两组对应关系对清楚。

**操作步骤**：

1. 在 `topk_gate` wrapper 里，分别圈出「传给构造函数的参数」和「传给 `kernel(...)` 的张量」。
2. 对比 `@T.prim_func def topk_gate_kernel(scores, topk_idx)` 的形参顺序，确认 `kernel(scores, topk_idx)` 是按位置一一对应的。
3. 重复一遍 batched_transpose 的对应关系。
4. 设置 `TK_PRINT_KERNEL_SOURCE=1` 跑一次 topk 的测试（参考 u1-l2 的运行方式），观察打印出的 CUDA 源码里 `num_experts` 是不是一个具体数字、而 `num_tokens` 是不是作为运行时参数出现。

**需要观察的现象**：编译期参数（如 `num_experts=72`）在生成的 CUDA 源码里已经变成常量；运行时符号（`num_tokens`）则体现为 kernel 启动参数或由 grid 维度推导。

**预期结果**：你能用一句话说清——「wrapper 把编译期参数喂给构造函数做特化，把张量按 prim_func 形参顺序喂给 kernel 做启动」。

**说明**：步骤 4 需要可运行的 GPU 环境。如果当前没有 GPU 或未安装依赖，标注「待本地验证」，只做源码层面的对照也可达成实践目标。

#### 4.4.5 小练习与答案

**练习 1**：`get_topk_gate_kernel(num_experts, num_topk)` 的结果要不要缓存？为什么 wrapper 里每次都重新调用？

**参考答案**：TileLang 的 `@tilelang.jit` 装饰器**内部自带缓存**——对同一组编译期参数，第二次调用构造函数会命中缓存、直接返回已编译的 kernel 对象，不会重复编译。所以 wrapper 里看似「每次都调用」，实际只有第一次真正编译，后续都是查缓存。这是 JIT 框架的常规设计。

**练习 2**：`batched_transpose` 为什么用 `shape_x % 128` 而不是完整的 `shape_x` 作为编译期参数？用一个具体例子说明。

**参考答案**：因为转置 kernel 的 tile 大小只取决于「形状能否被 64 或 128 整除的余数情况」（见 [L31-L32](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L31-L32)，`block_x = 128 if shape_x_mod_128 == 0 else 64`）。形状 `256`（mod 128 == 0）和 `384`（mod 128 == 0）会用完全相同的 tile 划分与编译产物，没必要为每个具体值各编译一份。把「mod 128」作为编译期键，把「具体大小」留给运行时，既保证特化又最大化复用。

---

## 5. 综合实践：编写一个 `rowmax` 算子（四步骨架全流程）

把本讲学的「装饰器 + 构造函数 + `T.dynamic` + `T.Kernel` + wrapper」五件套串起来，亲手写一个最小的新算子：**返回每行的最大值**（rowmax）。它和 `topk_gate` 是近亲——`topk_gate` 内部就调用了 `T.reduce_max` 来找行最大值，我们只是把这个动作单独拎出来做一个独立算子。

### 5.1 实践目标

仿照 `topk_gate_kernel` 的写法，完成 `get_rowmax_kernel` 构造函数 + `rowmax` wrapper，并能说清每一步对应本讲的哪个概念。

### 5.2 参考模板与思路

`topk_gate` 在 [L41-L51](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L41-L51) 用 `T.reduce_max(scores_fragment, amax_fragment)` 把一行 scores 的最大值规约到 `amax_fragment[0]`。我们的 rowmax 只需要：把每个 token 一行的最大值写到输出 `rowmax[pid]`。

### 5.3 操作步骤（示例代码）

下面是**示例代码**（不是项目原有代码），严格模仿 `topk_gate_kernel.py` 的骨架：

```python
# 示例代码：仿 topk_gate 的 rowmax 算子
import os
import tilelang
import torch
from tilelang import language as T
from tile_kernels.utils import align


@tilelang.jit(
    pass_configs={
        tilelang.PassConfigKey.TL_DISABLE_WARP_SPECIALIZED: True,
    },
)
def get_rowmax_kernel(num_experts: int):          # ① 构造函数，编译期参数只有 num_experts
    num_tokens = T.dynamic('num_tokens')          # ② 运行时符号：token 数
    num_threads = 32
    num_aligned_experts = align(num_experts, num_threads)

    @T.prim_func                                  # ③ 内核函数
    def rowmax_kernel(
        scores: T.Tensor[(num_tokens, num_experts), T.float32],
        rowmax: T.Tensor[(num_tokens,), T.float32],
    ):
        with T.Kernel(num_tokens, threads=num_threads) as pid:   # ④ 一维网格，每块一行
            scores_fragment = T.alloc_fragment((num_aligned_experts,), T.float32)
            amax_fragment = T.alloc_fragment((1,), T.float32)

            # 载入一行，超出 num_experts 的位置填 -inf（对齐用）
            for i in T.Parallel(num_aligned_experts):
                if i < num_experts:
                    scores_fragment[i] = scores[pid, i]
                else:
                    scores_fragment[i] = -T.infinity(T.float32)

            T.reduce_max(scores_fragment, amax_fragment)   # 规约出本行最大值
            rowmax[pid] = amax_fragment[0]                 # 写回输出

    return rowmax_kernel


def rowmax(scores: torch.Tensor) -> torch.Tensor:           # ⑤ wrapper：用户入口
    assert scores.dim() == 2 and scores.is_contiguous() and scores.dtype == torch.float32
    num_tokens, num_experts = scores.shape
    out = torch.empty((num_tokens,), dtype=torch.float32, device=scores.device)
    if num_tokens == 0:                                       # 运行时守卫
        return out

    kernel = get_rowmax_kernel(num_experts)                   # 触发/命中 JIT 编译
    if int(os.getenv('TK_PRINT_KERNEL_SOURCE', 0)):
        print(kernel.get_kernel_source())
    kernel(scores, out)                                       # 启动，按 prim_func 形参顺序
    return out
```

把上面的代码保存为一个本地文件（不要写进 `tile_kernels/` 源码目录）。

### 5.4 需要观察的现象

对照本讲的四件套，逐项确认：

| 本讲概念 | 在 rowmax 里的体现 |
|----------|--------------------|
| `@tilelang.jit` 装饰器 | `@tilelang.jit(pass_configs={...})` |
| 构造函数 + 编译期参数 | `get_rowmax_kernel(num_experts)` |
| `T.dynamic` 运行时符号 | `num_tokens = T.dynamic('num_tokens')` |
| `@T.prim_func` 内核 | `def rowmax_kernel(scores, rowmax)` |
| `T.Kernel` 网格+线程 | `with T.Kernel(num_tokens, threads=32) as pid` |
| wrapper 四步 | 校验 → `torch.empty` → `if num_tokens==0` → `kernel(scores, out)` |

### 5.5 验证（若有 GPU 环境）

用 PyTorch 参考对拍：

```python
# 示例代码：对拍验证
scores = torch.randn((1024, 72), dtype=torch.float32, device='cuda')
ref = scores.max(dim=1).values
got = rowmax(scores)
print((ref == got).all())   # 预期 True（reduce_max 与 torch.max 都是精确最大值）
```

若无可用的 GPU/依赖环境，标注「待本地验证」，仅做源码层面的骨架对照同样完成本实践。

### 5.6 思考延伸

- 把 `T.reduce_max` 换成 `T.reduce_sum`，wrapper 改叫 `rowsum`，就是一个求行和的算子——这说明掌握了骨架，换一个规约原语就能快速产出新算子（规约原语细节见 u2-l3）。
- 如果要让 rowmax 支持 bf16 输入，需要把 `dtype` 提升为构造函数参数（像 `batched_transpose` 那样），并相应改 `T.Tensor` 的 dtype——体会「dtype 为何是编译期参数」。

## 6. 本讲小结

- 一个 TileLang 算子是固定「三层套娃」：`@tilelang.jit` 装饰器 → kernel 构造函数（编译期参数）→ `@T.prim_func` 内核函数（启动时张量）。所有 TileKernels 算子都遵循这一骨架。
- **编译期参数**（如 `num_experts`、`shape_x_mod_128`、`dtype`）是烤进编译产物的常量，不同取值会各自特化编译；**运行时维度**用 `T.dynamic('名字')` 声明，可出现在张量形状、步长和网格表达式里，启动时由张量提供具体值。
- `T.dynamic` 创建的是符号，不能在 Python 层面对它做需要确切数值的判断（如 `if num_tokens > 0`）；这类守卫由 **wrapper** 用真实张量的 shape 来做。
- `with T.Kernel(各维度, threads=N) as (pids):` 定义网格与每块线程数；grid 维度数必须等于 `as` 绑定的变量数，每个 pid 代表本块在该方向的坐标。
- wrapper 是用户真正的调用入口，固定四步：校验 → 分配输出 → 触发/命中编译 → 启动；启动时张量按 `@T.prim_func` 形参顺序位置对应地传入。
- 转置用「形状 mod 128」做编译期键、token/batch 数做运行时符号，是「编译期 vs 运行时」切分追求「既特化又复用」的典范例子。

## 7. 下一步学习建议

本讲只讲了**骨架**，with 块内部的具体计算细节全部跳过了。接下来：

1. **u2-l2（GPU 存储层级与数据搬运）**：本讲里出现的 `T.alloc_fragment`、`T.alloc_shared`、`T.copy`、共享内存 padding（`block_x + block_k`）到底是什么——它们决定了数据如何在寄存器/共享内存/全局内存之间搬运。
2. **u2-l3（循环、并行与规约原语）**：本讲里出现的 `T.Parallel`、`T.unroll`、`T.reduce_max`、`T.alloc_reducer` 的语义与适用场景，理解 rowmax 实践里 `T.reduce_max` 到底做了什么。
3. 之后进入第 3 单元（Transpose 模块深入），把本讲的转置骨架结合存储层级和布局，完整精读一遍。

建议在进入 u2-l2 前，先把本讲的「四件套」默写一遍：能用一张图画出 wrapper → 构造函数 → prim_func → T.Kernel 的调用与数据流向，再往下学会很顺。
