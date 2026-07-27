# TileLang Kernel 骨架与测试/基准框架

## 1. 本讲目标

上一讲（u1-l1）我们已经让环境跑通、看了一遍仓库全貌。本讲我们要把镜头拉近，**拆开一个 TileLang kernel 看它的内部结构**，并搞懂项目自带的「测试 + 计时」框架是怎么自动验证我们写的 kernel 的。

学完本讲，你应当能够：

1. 说出一个 TileLang kernel 函数由哪两部分组成（host 声明部分 / device 计算部分），并解释 `@tilelang.jit`、`T.const`、`T.Tensor`、`T.empty`、`return` 各自的作用。
2. 看懂 `T.Kernel(...)` 的启动配置：blocks 怎么算、`threads` 是什么、块索引变量（如 `bx`、`pid_n`）从哪里来。
3. 解释 `common/utils.py` 里的 `test_puzzle` 如何「只凭形状和类型」就自动造出输入张量、跑 torch 参考实现、再和你的 kernel 结果比对；以及 `bench_puzzle` 如何用 CUDA Event 公平计时。

本讲**只讲骨架与框架**，不深入具体算子（拷贝的并行细节留给 u1-l3，归约/矩阵乘留给后续单元）。

## 2. 前置知识

在开始之前，用最通俗的方式建立三个概念。已熟悉的读者可以跳过。

- **Python 装饰器（decorator）**：`@something` 写在函数上方，等价于「先把下面这个函数交给 `something` 处理一下，再用处理后的结果替换原函数」。本讲里 `@tilelang.jit` 就是把一个普通 Python 函数「翻译/编译」成 GPU kernel。
- **张量（tensor）**：可以理解成「带形状的多维数组」。例如形状 `(N,)` 是一维向量，`(M, K)` 是矩阵。每个张量还有一个**数据类型（dtype）**，比如 `float16`（半精度浮点）。
- **GPU 的三层并行直觉**（极简版，细节在 u2-l2 再展开）：
  ```
  Grid（网格）= 很多 Block 的集合
    ├── Block 0  ── Thread 0, Thread 1, ..., Thread (threads-1)
    ├── Block 1  ── Thread 0, Thread 1, ...
    └── ...
  ```
  一个 kernel 启动时会同时跑很多个 **block**，每个 block 内部又同时跑很多个 **thread**。每个 block 需要知道「我是第几个 block」，这就是**块索引（block index）**。

承接 u1-l1：TileLang 是写高性能 GPU kernel 的 DSL；项目把学习拆成 10 个带 TODO 的 puzzle，用 `puzzles/`（题目）与 `ans/`（答案）一一对照，`common/utils.py` 负责验证正确性与计时。

## 3. 本讲源码地图

本讲涉及三个文件，它们正好构成「**一个完整示例 + 一个最简 puzzle + 验证框架**」的三角：

| 文件 | 作用 | 本讲怎么看它 |
|------|------|------------|
| [scripts/check_tilelang_env.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py) | 环境自检脚本，里面手写了一个完整的 GEMM（矩阵乘）kernel | 用作「**结构最完整的骨架样板**」来讲解 |
| [puzzles/01-copy.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py) | 第一个 puzzle（拷贝），含一个已写好的串行版本 | 用作「**最简骨架**」对照 |
| [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | `test_puzzle` / `bench_puzzle` / `rand_torch_tensor` 等公共工具 | 讲解验证与计时框架 |

> 提示：本讲的「最小模块」有三个——① `@tilelang.jit` 与 kernel 声明骨架；② `T.Kernel` 启动配置与块索引；③ `test_puzzle` / `bench_puzzle` 框架。下面逐一展开。

## 4. 核心概念与源码讲解

### 4.1 `@tilelang.jit` 与 kernel 声明骨架

#### 4.1.1 概念说明

TileLang 采用一种叫 **EagerJIT（即时编译）** 的编程风格：你写一个普通 Python 函数，用 `@tilelang.jit` 装饰它，TileLang 会在你「调用」这个函数时，把函数体**追踪（trace）并编译**成一段 GPU 代码（最终接近手写 CUDA）。

被装饰的函数体内部，逻辑上分成**两段**：

1. **host / 声明部分（declaration）**：在「编译期」执行。用来声明运行时常量、每个输入张量的形状与类型、并分配输出张量。这一段描述的是「计算长什么样」，并不真正算数据。
2. **device / 计算部分（kernel body）**：写在 `with T.Kernel(...)` 里，是真正会被编译成 GPU 代码、在显卡上跑的部分。

> 为什么要把形状写成「声明」而不是直接用 Python 变量？因为像 `N`（向量长度）在**写函数时还不知道**，要等用户调用时通过参数字典传进来。`T.const` 就是用来声明这种「现在先占位、稍后绑定」的符号维度。

#### 4.1.2 核心流程

一个 kernel 骨架的通用模板如下（伪代码，对照真实代码看）：

```python
@tilelang.jit
def my_kernel(输入张量A, 输入张量B, 超参数1: int, 超参数2: int):
    # ===== ① host 声明部分 =====
    N, M = T.const("N, M")              # 声明运行时常量（符号维度）
    A: T.Tensor((N,), T.float16)       # 声明输入张量的形状与 dtype
    out = T.empty((M,), T.float16)     # 分配输出张量

    # ===== ② device 计算部分 =====
    with T.Kernel(块数, threads=线程数) as (块索引变量):
        ... # 真正在 GPU 上执行的 TileLang DSL 代码 ...

    return out                          # ③ 把输出张量返回给宿主
```

三个要点：

- 函数的 **Python 形参** = 输入张量（compact torch Tensor）+ 超参数（如 `block_M: int`）。超参数在编译期就确定，用来决定分块大小、启动配置等。
- `T.const` 声明的常量，在编译时由调用方传入的字典绑定（例如 `kernel.compile(N=1024)`）。
- **必须 `return` 输出张量**：这是 TileLang 约定，框架（后面 `test_puzzle`）也会依赖「最后一个声明的张量就是输出」这一约定。

#### 4.1.3 源码精读

**最简骨架：Puzzle 01 的串行拷贝。** 先看项目里最短的一个完整 kernel——[puzzles/01-copy.py:62-79](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L62-L79)：

```python
@tilelang.jit
def tl_copy_1d_serial(A):
    # host 声明部分
    N = T.const("N")                 # 声明符号维度 N（运行时由 {"N": N} 绑定）
    A: T.Tensor((N,), T.float16)     # 输入：长度 N 的 float16 向量
    B = T.empty((N,), T.float16)     # 分配同形状的输出张量 B

    # device 计算部分
    with T.Kernel(1, threads=1) as _:  # 只启动 1 个 block、每 block 1 个线程
        T.copy(A, B)                   # 把 A 整体拷贝到 B（TileOp，后续讲）

    return B                           # 返回输出
```

逐行对应到三段：声明（`T.const` / `T.Tensor` / `T.empty`）→ 计算（`with T.Kernel`）→ 返回（`return B`）。注意 [puzzles/01-copy.py:65-67](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L65-L67) 这三行就是完整的 host 声明。

**结构最完整的样板：环境自检脚本里的 GEMM。** 它展示了多常量、多输入、混合精度的写法。看 [scripts/check_tilelang_env.py:14-21](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L14-L21)：

```python
@tilelang.jit
def gemm(A, B, block_M: int = 128, block_N: int = 128, block_K: int = 32):
    M, N, K = T.const("M, N, K")          # 一次声明 3 个符号维度
    A: T.Tensor[[M, K], T.float16]        # 输入矩阵 A
    B: T.Tensor[[K, N], T.float16]        # 输入矩阵 B
    C = T.empty((M, N), T.float16)        # 分配输出矩阵 C
```

两个细节值得注意：

- [scripts/check_tilelang_env.py:22](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L22-L22) 用 `T.const("M, N, K")` 一次性声明了三个常量（逗号分隔），等价于分别写三次。
- 这里输入用 `T.Tensor[[M, K], T.float16]`（**双括号**），而 Puzzle 01 用 `T.Tensor((N,), T.float16)`（**小括号**）。两种写法都合法——TileLang 同时接受 list 和 tuple 来描述形状，挑一种保持一致即可。

最后看「返回输出」这一约定在 GEMM 里的体现：[scripts/check_tilelang_env.py:41-42](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L41-L42) 把算好的 `C_local`（寄存器里的中间结果，4.2 讲）拷回全局张量 `C`，然后 `return C`。

#### 4.1.4 代码实践

> **实践目标**：用肉眼把一个 kernel 拆成「声明 / 计算 / 返回」三段，并验证你对每个语句作用的理解。

1. 打开 [puzzles/01-copy.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py)，定位到 `tl_copy_1d_serial`（62 行起）。
2. 用笔或注释把代码分成三块：① host 声明；② device 计算；③ 返回输出。
3. 思考并回答：如果把 [puzzles/01-copy.py:67](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L67-L67) 的 `B = T.empty((N,), T.float16)` 改成 `T.float32`，会发生什么？
4. （可选，需运行）真正改一下并运行 `python3 puzzles/01-copy.py`，观察 `test_puzzle` 报告的结果。

**需要观察的现象 / 预期结果**：

- 第 3 步的预测：参考实现 `ref_copy_1d` 返回的是 `float16`（见 [puzzles/01-copy.py:37-40](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L37-L40) `A.clone()`，A 是 float16），而你的 kernel 输出变成了 `float32`。两者 dtype 不同，`test_puzzle` 内部的 `torch.allclose` 很可能报 `❌`，并打印 dtype/shape 差异。**待本地验证**（不同 tilelang 版本对 dtype 提升的处理可能略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：在 GEMM 样板里，为什么 `M, N, K` 要用 `T.const` 声明，而 `block_M / block_N / block_K` 直接写成普通 Python 形参（`block_M: int = 128`）？

> **参考答案**：`block_M` 等是**编译期就确定的超参数**（分块大小），直接作为函数默认参数传入即可；而 `M, N, K` 是**张量的维度**，在写函数时还不知道具体数值，要等调用方用 `{"M": ..., "N": ..., "K": ...}` 绑定，所以必须用 `T.const` 声明为符号维度。

**练习 2**：一个 kernel 函数里可以 `return` 多个张量吗？结合本讲的「最后一个声明的张量是输出」这一约定，你认为框架对此有什么假设？

> **参考答案**：TileLang 语言层面允许返回张量；但本项目的 `test_puzzle` 框架做了一个**约定**——它把「参数列表里最后一个张量」当作输出（见 4.3.3）。所以在本项目的 puzzle 里，约定是「声明一个 `T.empty` 输出并 `return` 它」。这也是为什么每个 puzzle 都以 `return B` / `return C` 收尾。

---

### 4.2 `T.Kernel` 启动配置与块索引

#### 4.2.1 概念说明

`with T.Kernel(...) as (...):` 这一行回答两个问题：**「启动多少个 block」** 和 **「每个 block 里有多少 thread」**。它对应 CUDA 里的「launch 配置」（grid 与 block 维度）。

- **位置参数（positional args）**：每一个位置参数代表一个 **block 维度**，它们的乘积就是总 block 数（grid 大小）。
- **`threads=`**：每个 block 内的线程数。
- **`as (变量...)`**：解包出来的就是**块索引变量**，个数必须和位置参数个数一致。比如 `T.Kernel(a, b) as (bx, by)` 表示二维 grid，`bx`、`by` 分别是两个维度上的 block 编号。

#### 4.2.2 核心流程

把一个长度为 `M` 的维度按 `block_M` 分块，需要多少个 block？答案是向上取整：

\[
\text{grid}_M = \left\lceil \frac{M}{\text{block\_M}} \right\rceil
\]

TileLang 提供了现成的 `T.ceildiv(a, b)` 来表达这个向上取整（ceiling division）。整数实现上等价于 `(a + b - 1) // b`：

\[
\text{ceildiv}(a, b) = \left\lceil \frac{a}{b} \right\rceil = \frac{a + b - 1}{b} \quad (\text{整数除法})
\]

启动配置的整体流程：

```
1. 计算每个维度需要的 block 数：grid_d = ceildiv(维度长度, block_大小)
2. T.Kernel(grid_d0, grid_d1, ..., threads=N) as (b0, b1, ...)
3. 每个 block 拿到自己的索引 (b0, b1, ...)，据此计算它负责的数据区间
4. block 内部，threads 协作完成这个区间上的计算
```

#### 4.2.3 源码精读

**二维 grid 的样板：GEMM。** 看 [scripts/check_tilelang_env.py:29-32](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L29-L32)：

```python
with T.Kernel(T.ceildiv(M, block_M), T.ceildiv(N, block_N), threads=128) as (bx, by):
    ...
```

含义：在 M 维上分 `ceildiv(M, block_M)` 个 block、在 N 维上分 `ceildiv(N, block_N)` 个 block，每个 block 含 128 个线程；解包出 `bx`（M 维 block 编号）、`by`（N 维 block 编号）。于是当前 block 负责的输出 tile 起点 = `(bx * block_M, by * block_N)`——这正是后面 [scripts/check_tilelang_env.py:38-41](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L38-L41) 里 `A[bx * block_M, ...]`、`C[bx * block_M, by * block_N]` 这些下标的来源。

> 这里出现了 `T.alloc_shared` / `T.alloc_fragment` / `T.copy` / `T.gemm` / `T.Pipelined` 等。**本讲不需要理解它们的算子语义**，只需注意到：`T.Kernel` 块里的代码会用到块索引 `bx`/`by` 来定位数据。这些原语的细节会在 u2-l2（内存层级）和 u4（矩阵乘）逐一讲透。

**一维 grid 的样板：串行拷贝（只启动 1 个 block）。** 看 [puzzles/01-copy.py:71](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L71-L71)：

```python
with T.Kernel(1, threads=1) as _:
    T.copy(A, B)
```

这里 grid 只有一个维度、值为 `1`，所以只有一个 block 编号，且我们用 `_` 忽略它（因为只有一个 block，索引恒为 0，用不上）。`threads=1` 表示这个 block 里只有 1 个线程——所以这是**串行**拷贝。多 block 并行版（用 `pid_n` 做索引）的细节留给 u1-l3。

#### 4.2.4 代码实践

> **实践目标**：亲手改 GEMM 的分块参数，直观感受「启动配置是可调的超参数」，并确认 kernel 仍能正确编译运行。

1. 打开 [scripts/check_tilelang_env.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py)，定位到 `gemm` 的形参默认值 [scripts/check_tilelang_env.py:18-20](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L18-L20)（`block_M=128, block_N=128, block_K=32`）。
2. 把它们改成另一组**合理的**值，例如 `block_M=64, block_N=64, block_K=64`。
3. 运行 `python3 scripts/check_tilelang_env.py`。
4. 观察最后一行 `Check GEMM result: ...` 是 `True` 还是 `False`。

**需要观察的现象 / 预期结果**：

- 只要你选的 block size 能整除对应维度（此处 M=2048、N=2048、K=4096，见 [scripts/check_tilelang_env.py:44-45](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L44-L45)），并且满足 Tensor Core 对形状的约束（通常 block_M/block_N 是 16 的倍数），kernel 仍应编译成功且 `Check GEMM result: True`。
- 编译耗时会随 block size 变化（重新编译时能感觉到）。这正是「分块大小是性能调参旋钮」的第一次直观体验——性能对比方法在 4.3 与 u5-l4 系统讲。
- 如果改成无法被整除或违反硬件约束的值，预期会编译失败或结果错误。**具体数值待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`T.Kernel(T.ceildiv(M, block_M), T.ceildiv(N, block_N), threads=128) as (bx, by)` 里，一共有多少个 block？`bx` 和 `by` 的取值范围分别是什么？

> **参考答案**：总 block 数 = `ceildiv(M, block_M) * ceildiv(N, block_N)`；`bx ∈ [0, ceildiv(M, block_M))`，`by ∈ [0, ceildiv(N, block_N))`。

**练习 2**：串行拷贝里写的是 `T.Kernel(1, threads=1) as _`。如果改成 `T.Kernel(1, threads=256)`（仍只有 1 个 block，但每个 block 256 个线程），单从启动配置看，总线程数变成多少？这会让拷贝变快吗？

> **参考答案**：总线程数 = block 数 × threads = 1 × 256 = 256。是否会变快取决于 `T.copy` 能否利用这 256 个线程并行搬运——这正是 u1-l3 要回答的问题（`T.copy` 会在 block 内自动并行化，所以预期会更快）。

---

### 4.3 `test_puzzle` / `bench_puzzle` 框架

#### 4.3.1 概念说明

写完 kernel 后，你怎么知道它写对了、跑得快不快？项目在 [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) 里提供了两个工具函数：

- **`test_puzzle`**：正确性验证。它编译你的 kernel，**自动**构造随机输入，同时跑一遍你的 kernel 和一个 torch 参考实现，再用 `torch.allclose` 比对结果。
- **`bench_puzzle`**：性能计时。用 CUDA Event 反复跑（warmup + repeats），输出平均耗时（毫秒），可选地和 torch 对比。

它们的设计哲学是：**你只管声明张量的形状和 dtype，框架自动帮你造数据、跑参考、比结果**——这样每个 puzzle 的验证代码都只有一行。

#### 4.3.2 核心流程

`test_puzzle` 的整体链路（伪代码）：

```
test_puzzle(my_kernel, torch_ref, {"N": 1024}):
    1. tl_kernel = my_kernel.compile(N=1024)        # JIT 编译，得到带 .params 的对象
    2. inputs = _torch_tensor_materialize(tl_kernel.params)
         # 遍历 params，跳过最后一个（输出），按 shape/dtype 造随机 torch 张量
    3. output_torch = torch_ref(*inputs)            # 跑 torch 参考实现
       output_tl   = tl_kernel(*inputs_copy)        # 跑你的 kernel
    4. match = torch.allclose(output_torch, output_tl, atol, rtol)
    5. 打印 ✅ / ❌，不一致时打印逐元素 diff
```

`bench_puzzle` 的计时链路（伪代码）：

```
bench_puzzle(my_kernel, torch_ref, {...}):
    warmups = 10; repeats = 100
    先跑 warmups 次（预热，避免首次编译/缓存开销计入）
    torch.cuda.synchronize()                        # 确保前面都跑完
    start.record()                                  # CUDA Event 起点
    跑 repeats 次
    end.record(); torch.cuda.synchronize()          # 终点 + 同步
    平均耗时 = (start 到 end 的 elapsed) / repeats
```

为什么必须 `torch.cuda.synchronize()`？因为 GPU 调用是**异步**的——宿主发出命令后会立刻返回，并不等 GPU 真的算完。如果不同步就开始/结束计时，测到的只是「发命令」的时间，不是「算」的时间。CUDA Event 配合同步，才能测到真实的 GPU 耗时。

#### 4.3.3 源码精读

**关键一：自动造输入。** 看 [common/utils.py:50-63](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L50-L63)：

```python
def _torch_tensor_materialize(params: list[KernelParam]):
    inputs_in_torch_tensors: list[torch.Tensor] = []
    for idx, tl_param in enumerate(params):
        if idx == len(params) - 1:      # 关键约定：跳过最后一个参数（输出）
            continue
        shape = tl_param.shape
        dtype = tl_param.dtype
        torch_dtype = _tvm_ffi_dtype_to_torch_dtype(dtype)
        inputs_in_torch_tensors.append(rand_torch_tensor(shape, torch_dtype, device="cuda"))
    return inputs_in_torch_tensors
```

注意三个事实：

1. [common/utils.py:54-55](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L54-L55) **跳过最后一个参数**——这把「最后一个声明的张量」当作输出。这正是 4.1 反复强调「`return` 输出张量」的原因：框架靠位置约定识别输出。
2. [common/utils.py:58-60](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L58-L60) 直接用 `KernelParam` 的 `.shape` 和 `.dtype` 造张量——**形状是编译后绑定的具体值**（比如 `N=1024` 编译后，`shape` 就是 `[1024]`）。
3. [common/utils.py:60](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L60-L60) 把 tvm 的 dtype 字符串（如 `"float16"`）转成 torch dtype，转换表见 [common/utils.py:19-31](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L19-L31)（支持 `float16` / `float32` / `uint8` / `int32` / `int64`）。

**关键二：编译并比对。** 看 [common/utils.py:76-89](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L76-L89)：

```python
tl_kernel: JITKernel = puzzle_tl.compile(**tl_hyper_params)   # 编译，绑定 N 等常量
inputs_in_torch_tensors = _torch_tensor_materialize(tl_kernel.params)
inputs_copy = [i.clone() for i in inputs_in_torch_tensors]    # 防止 kernel 改了输入影响参考
output_torch = puzzle_torch(*inputs_in_torch_tensors)         # torch 参考
output_tl = tl_kernel(*inputs_copy)                           # 你的 kernel
match = torch.allclose(output_torch, output_tl, atol=atol, rtol=rtol)
```

[common/utils.py:82](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L82-L82) 先 `clone` 一份输入——因为有些 kernel 会原地修改输入，这样能保证 torch 参考和你用的是相同的原始数据。

**关键三：CUDA Event 计时。** 看 [common/utils.py:118-119](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L118-L119) 的 `warmups = 10; repeats = 100`，以及 TileLang 计时段 [common/utils.py:146-155](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L146-L155)：

```python
tl_start = torch.cuda.Event(enable_timing=True)
tl_end   = torch.cuda.Event(enable_timing=True)
torch.cuda.synchronize()
tl_start.record()
for _ in range(repeats):
    tl_kernel(*inputs_in_torch_tensors)
tl_end.record()
torch.cuda.synchronize()
tl_time = tl_start.elapsed_time(tl_end) / repeats
```

`record()` + `synchronize()` + `elapsed_time()` 是标准的 GPU 计时三件套。`bench_torch=True` 时还会用同样方式给 torch 计时，方便横向对比（见 [common/utils.py:128-141](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L128-L141)）。

**怎么调用它们？** Puzzle 01 的串行版只写了一行验证：[puzzles/01-copy.py:82-85](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L82-L85)

```python
def run_copy_1d_serial():
    print("\n=== Copy 1D Serial ===\n")
    N = 1024
    test_puzzle(tl_copy_1d_serial, ref_copy_1d, {"N": N})
```

第三个参数 `{"N": N}` 就是绑定到 `T.const("N")` 的超参数字典——从 `T.const` 声明，到这里绑定，再到 `compile(**tl_hyper_params)`，闭环。

> 补充一个细节：[common/utils.py:9](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L9-L9) 调用了 `tilelang.disable_cache()`，目的是**关闭 JIT 缓存**，保证你在学习/改代码时每次都真重新编译（不会因为缓存而看不到改动效果）。环境自检脚本里也做了同样的事（[scripts/check_tilelang_env.py:10](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L10-L10)）。

#### 4.3.4 代码实践

> **实践目标**：把 `test_puzzle` 的「自动造输入」链路走一遍，亲眼看 `KernelParam` 是怎么驱动这一切的。

1. 打开 [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py)，从 `test_puzzle`（66 行）开始读。
2. 跟着调用链走：`compile` → `tl_kernel.params` → `_torch_tensor_materialize` → `rand_torch_tensor`。
3. 回答：对于 GEMM kernel（声明了 `A`、`B` 两个输入张量和一个 `C` 输出），`_torch_tensor_materialize` 会造出**几个**张量？分别是什么 shape/dtype？
4. 在 `test_puzzle` 里临时加一行 `print(tl_kernel.params)`（或把 [common/utils.py:77](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L77-L77) 那行被注释掉的 `get_kernel_source()` 取消注释），运行任意一个 puzzle，观察输出。

**需要观察的现象 / 预期结果**：

- 第 3 步：会造出 **2 个**输入张量（A、B），跳过最后的 C。A 的 shape 是编译时绑定的 `[M, K]`、dtype `float16`；B 是 `[K, N]`、dtype `float16`（具体数值取决于你编译时传的 M/N/K，环境脚本里是 2048×4096 与 4096×2048）。
- 第 4 步：你会看到 `params` 是一串 `KernelParam` 对象，能直观验证「最后一个被跳过」。
- 注意：**不要把改过的 `common/utils.py` 提交回去**——这是阅读型实践，读完恢复原样即可（本讲禁止修改源码，仅作本地观察）。

#### 4.3.5 小练习与答案

**练习 1**：`bench_puzzle` 里为什么要有 `warmups = 10` 这一步？如果去掉会怎样？

> **参考答案**：首次调用 kernel 时会发生 JIT 编译、内存分配、CUDA 上下文初始化等一次性开销，远大于稳态耗时。先跑若干次 warmup 把这些「冷启动」开销摊掉，再开始计时，才能测到接近真实的稳态性能。去掉的话，前几次的巨大开销会被计入平均值，结果偏大且不稳定。

**练习 2**：`_torch_tensor_materialize` 为什么**跳过最后一个参数**而不是跳过第一个？如果某个 puzzle 把输出声明在输入**之前**（先 `T.empty` 再声明输入），框架还能正常工作吗？

> **参考答案**：因为项目的统一约定是「先声明所有输入张量，最后用 `T.empty` 声明并 `return` 输出」，所以框架按「最后一个 = 输出」来识别。如果反过来把输出声明在最前面，框架会错误地把某个输入当作输出跳过、把真正的输出当成输入去喂给 torch 参考函数，导致验证失败。结论：**写 puzzle 时务必遵守「输出放最后」的约定**。

**练习 3**：`torch.allclose` 用了 `atol=1e-2, rtol=1e-2`（[common/utils.py:71-72](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L71-L72)）。为什么 GPU 上的浮点结果不能直接用 `==` 比较？

> **参考答案**：float16/bfloat16 等低精度浮点在不同实现（你的 kernel vs torch）下的累加顺序、舍入方式不同，会有微小差异（如 1e-3 量级）。`allclose` 用绝对容差 `atol` 和相对容差 `rtol` 来允许这种合法误差，只要「差异在容差内」就认为数值等价。直接 `==` 会因为最后一位的不同而误报失败。

## 5. 综合实践

把本讲三个模块串起来，做一次「**从声明到验证**」的端到端走查。

**任务**：以 Puzzle 01 的串行拷贝为对象，画一张（或用文字描述）完整的「调用链」流程图，把以下环节连起来，并在每个环节标注它对应本讲哪个概念、哪段源码：

1. 你调用 `run_copy_1d_serial()`（[puzzles/01-copy.py:82-85](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L82-L85)）。
2. `test_puzzle` 用 `{"N": 1024}` 调用 `compile`（4.1：`T.const("N")` 在此被绑定）。
3. `compile` 产出 `JITKernel`，其 `.params` 包含 A 和 B 两个 `KernelParam`（4.3）。
4. `_torch_tensor_materialize` 跳过最后的 B，按 `[1024]` / `float16` 造出输入 A（4.3）。
5. 同时跑 `ref_copy_1d(A)` 和 `tl_copy_1d_serial(A_copy)`——后者进入 `with T.Kernel(1, threads=1)`（4.2）执行 `T.copy(A, B)`，最后 `return B`（4.1）。
6. `torch.allclose` 比对两边输出，打印 ✅/❌。

**进阶（可选）**：把上面的 `N` 从 1024 改成一个**不能**被你预设的 `BLOCK_N` 整除的值（比如 1000），预测哪个环节会出问题，并说明串行版（`threads=1`，整体拷贝）和多 block 版（u1-l3 将要实现的 `N // BLOCK_N` 分块）哪个更脆弱、为什么。

> 预期：串行版对任意 N 都能工作（它只是 `for i in range(N): B[i]=A[i]`，不依赖整除）；而多 block 版用 `N // BLOCK_N` 个 block，若 N 不被 BLOCK_N 整除，会有「尾部元素没人处理」的问题——这正是 u1-l3 要解决的边界处理动机。

## 6. 本讲小结

- 一个 TileLang kernel = `@tilelang.jit` 装饰的函数，内部分 **host 声明部分**（`T.const` / `T.Tensor` / `T.empty`）与 **device 计算部分**（`with T.Kernel`），并以 `return` 输出张量收尾。
- `T.const` 用来声明「写函数时未知、调用时绑定」的符号维度（如 N、M、K），绑定发生在 `compile(**hyper_params)`；`block_M` 这类编译期超参数则直接写成 Python 形参。
- `T.Kernel(各维度 block 数, threads=N) as (块索引...)` 决定启动配置；位置参数个数 = grid 维数 = 块索引变量个数，`T.ceildiv` 用来算向上取整的 block 数。
- 块索引（`bx` / `by` / `pid_n`）是 block 内代码定位「自己负责哪段数据」的依据（如 `A[bx * block_M, ...]`）。
- `test_puzzle` 靠 `KernelParam` 的 `.shape` / `.dtype` **自动造输入**，并按「最后一个张量是输出」的约定跳过输出；`bench_puzzle` 用 CUDA Event + `synchronize` + warmup 做公平计时。
- 本项目所有 puzzle 共享这一套骨架与框架，所以每个 puzzle 的验证代码都极简——你只需专注于补全 `with T.Kernel` 里的 DSL 代码。

## 7. 下一步学习建议

本讲只看了「最简串行拷贝」这一个 kernel 的骨架，并刻意回避了 `T.copy` 的并行能力与 block 索引的实战用法。下一讲 **u1-l3（Puzzle 01 Copy：第一个 Kernel 与并行）** 将：

- 把同一个拷贝问题写成三种实现——**单线程串行 → 多线程 → 多 block 并行**，亲手用 `threads` 和块索引 `pid_n` 把并行度一步步拉满。
- 用 `bench_puzzle` 量化三种实现的耗时差异，建立「并行度 ↑ → 耗时 ↓」的第一次直觉。

建议阅读顺序：先重读 [puzzles/01-copy.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py) 中 `tl_copy_1d_multi_threads` 与 `tl_copy_1d_parallel` 的 TODO（[puzzles/01-copy.py:107](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L107-L107)、[puzzles/01-copy.py:154](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L154-L154)），再对照参考答案 [ans/01-copy.py:108-163](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py#L108-L163)。

如果对 GPU 内存层级（global / shared / registers）好奇，可以提前扫一眼环境脚本里的 `T.alloc_shared` / `T.alloc_fragment`（[scripts/check_tilelang_env.py:33-35](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L33-L35)），这部分会在 **u2-l2（GPU 内存层级与 T.alloc_fragment）** 系统讲解。
