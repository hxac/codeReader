# Puzzle 02 Vector Add：T.Parallel 与元素级运算

## 1. 本讲目标

学完本讲后，你应当能够：

- 用 `for i in T.Parallel(N)` 这种「并行循环」表达对一段 tile 的逐元素计算。
- 说清楚「元素级算术（`+`、`*`）」与上一讲的「TileOp（`T.copy`）」在写法上的根本区别：前者需要一个循环来遍历元素，后者一行搞定整段搬运。
- 用 `T.if_then_else(cond, true_value, false_value)` 在 kernel 里实现「每个元素根据自己的数据走不同分支」的条件逻辑（以 ReLU 为例）。
- 把「乘法 + ReLU」两个算子**融合**进同一个 kernel，并理解为什么要融合。
- 独立补全 `puzzles/02-vector-add.py` 中的 `tl_mul_relu_1d`，用 `test_puzzle` 验证与 PyTorch 结果一致。

## 2. 前置知识

本讲承接 [u1-l3](u1-l1-puzzle01-copy.md)（Puzzle 01 Copy）。在动手前，请确认你已经理解下面这些概念：

- **TileOp 与 `T.copy`**：上一讲我们用 `T.copy` 实现拷贝。`T.copy` 是一个 TileOp，框架会自动处理「并行 + 向量化」，你不需要写循环。
- **block / thread / 块索引**：`with T.Kernel(blocks, threads=N) as bx` 决定启动配置，`bx` 是每个 block 用来定位自己负责的数据区间的索引（对应 CUDA 的 `blockIdx`）。
- **`BLOCK_N` 是编译期超参**：在 `compile(...)` 时绑定，kernel 内部把它当成常量来切分数据。
- **`test_puzzle` 的约定**：它会根据 kernel 的参数表自动造随机输入，**把最后一个张量当成输出**跳过，再和 torch 参考结果用 `torch.allclose` 比对（默认 `atol=rtol=1e-2`）。

本讲要回答的核心问题是：**`T.copy` 能搬运数据，但如果我想在数据上做加减乘除、甚至加个 ReLU，该怎么写？**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [puzzles/02-vector-add.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py) | 题目文件。含**已完成**的 `tl_add_1d`（本讲的教学范例）和**待补全**的 `tl_mul_relu_1d`（本讲主任务）。 |
| [ans/02-vector-add.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py) | 参考答案。含 `tl_mul_relu_1d` 的完整实现，供你对照。 |
| [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | `test_puzzle` / `bench_puzzle` 验证与基准框架（上一讲已介绍，本讲直接使用）。 |

> 小提示：题目文件里块索引变量叫 `bx`，参考答案里同一个变量叫 `pid_n`，两者完全是同一个东西（都对应 `blockIdx`），只是起名不同。本讲以题目文件里的 `bx` 为主。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1** 用 `T.Parallel` 表达元素级加法（以已完成的 `tl_add_1d` 为范例）。
2. **4.2** `T.if_then_else` 条件表达式（ReLU 需要的新工具）。
3. **4.3** 把「乘法 + ReLU」融合进一个 kernel（本讲的主任务 `tl_mul_relu_1d`）。

### 4.1 T.Parallel 与元素级运算

#### 4.1.1 概念说明

上一讲我们用 `T.copy` 拷贝数据。`T.copy` 是一个 **TileOp**：它「整体」作用于一段 tile，框架自动把并行和向量化都包办了，你一行也不用写循环。

但是「加、减、乘、除」这类**算术运算**是**元素级（element-wise）**的——它们一次只作用于一个标量，本身并不知道要怎么并行。题目文件开头的注释把这件事说得很清楚：

> Tilelang provides basic arithmetic operations like add, sub, mul, div, etc. But these operations are element-wise (They are not TileOps like T.copy). So we need a **loop abstraction** to iterate over elements in the tensor.
> —— [puzzles/02-vector-add.py:16-19](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L16-L19)

所以我们需要一个「循环」来逐元素遍历，并在循环体里写出每个元素的算式。**`T.Parallel` 就是这个循环抽象**：你告诉 TileLang「这 N 次迭代彼此独立，请把它们并行分摊到 block 内的线程上」。

一句话区分两者：

| | `T.copy`（TileOp） | `T.Parallel` + 算术 |
| --- | --- | --- |
| 作用对象 | 整段 tile | 单个标量元素 |
| 是否要写循环 | 不需要 | 需要 `for i in T.Parallel(...)` |
| 并行谁来做 | 框架自动 | 框架自动（但你要声明「可并行」） |
| 典型用法 | 搬运数据 | 逐元素计算 |

#### 4.1.2 核心流程

以「`C[i] = A[i] + B[i]`」为例，一个 block 内的执行流程是：

1. 用块索引算出本 block 负责的数据起点：`base_idx = bx * BLOCK_N`。
2. 本 block 负责 `[base_idx, base_idx + BLOCK_N)` 这一段共 `BLOCK_N` 个元素。
3. `for i in T.Parallel(BLOCK_N)` 把这 `BLOCK_N` 次迭代**并行**分摊给 `threads=256` 个线程（每个线程处理多个元素）。
4. 循环体里写每个元素的算式：`C[base_idx + i] = A[base_idx + i] + B[base_idx + i]`。

关键点：`T.Parallel(BLOCK_N)` 表示「迭代次数是 `BLOCK_N`，且这些迭代相互独立、可并行」。它和 `T.Kernel` 里的 `threads=256` 是**解耦**的——你声明的是「迭代次数」和「可并行」，至于每个线程跑几次迭代，由框架根据 `threads` 自动分配。

#### 4.1.3 源码精读

先看 torch 参考，它就是逐元素相加：

```python
def ref_add_1d(A, B):
    ...
    return A + B
```

参考：[puzzles/02-vector-add.py:38-43](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L38-L43) —— torch 的 `A + B` 就是本讲要对齐的目标。

再看 TileLang 的 `tl_add_1d`（题目里已写好，是我们的教学范例）：

```python
@tilelang.jit
def tl_add_1d(A, B, BLOCK_N: int):
    N = T.const("N")
    A: T.Tensor((N,), T.float16)
    B: T.Tensor((N,), T.float16)
    C = T.empty((N,), T.float16)

    with T.Kernel(N // BLOCK_N, threads=256) as bx:
        base_idx = bx * BLOCK_N
        for i in T.Parallel(BLOCK_N):
            C[base_idx + i] = A[base_idx + i] + B[base_idx + i]

    return C
```

参考：[puzzles/02-vector-add.py:46-58](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L46-L58)。逐行解读：

- 第 53 行 `with T.Kernel(N // BLOCK_N, threads=256) as bx:` —— 启动 `N // BLOCK_N` 个 block，每 block 256 线程，块索引命名为 `bx`。
- 第 54 行 `base_idx = bx * BLOCK_N` —— 算出本 block 的数据起点（普通 Python 整数运算）。
- 第 55 行 `for i in T.Parallel(BLOCK_N):` —— **本讲的核心**：一个可并行的循环，`i` 遍历 `[0, BLOCK_N)`。
- 第 56 行 `C[base_idx + i] = A[base_idx + i] + B[base_idx + i]` —— 循环体里写元素级算术。注意下标是 `base_idx + i`，把「块索引定位」和「循环内偏移」组合成全局下标。

和 Puzzle 01 对比：01 用 `T.copy` 整段搬运；02 因为要做加法，**必须**写成「`T.Parallel` 循环 + 标量算式」。

#### 4.1.4 代码实践

**实践目标**：跑通已完成的 `tl_add_1d`，并验证 `T.Parallel` 的并行正确性与 `threads` 数量解耦。

**操作步骤**：

1. 在仓库根目录运行题目文件里专门测加法的入口（或直接跑整个文件）：

   ```bash
   python3 -c "from puzzles import __init__" 2>/dev/null; python3 puzzles/02-vector-add.py
   ```

   如果你只想跑加法这一段，可以在 Python 里单独调用：

   ```bash
   python3 -c "from puzzles['02-vector-add'] import *" 2>/dev/null || python3 -c "
   import importlib.util, sys
   spec = importlib.util.spec_from_file_location('p02', 'puzzles/02-vector-add.py')
   m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
   m.run_add_1d()
   "
   ```

   （上面的写法是为了避开 `puzzles/02-vector-add.py` 文件名里有连字符、不能直接 `import` 的问题。）

2. 做一个小实验：把 `tl_add_1d` 里的 `threads=256` 改成 `threads=128`（或 `512`），再次运行 `run_add_1d()`。

**需要观察的现象**：

- 终端打印 `✅ Results match: True`，说明 TileLang 结果与 torch 的 `A + B` 在 `atol=rtol=1e-2` 下一致。
- 改 `threads` 之后，结果**仍然**是 `True`。

**预期结果**：正确性不变。因为 `T.Parallel(BLOCK_N)` 只声明「迭代次数与可并行性」，`threads` 只决定「每个 block 配多少线程」，框架会自动把 `BLOCK_N` 次迭代分摊到这些线程上——无论 128 还是 256，每个元素都会被恰好处理一次。（至于哪种更快，属于性能问题，**待本地验证**。）

> 关于容差：`test_puzzle` 默认用 `torch.allclose(..., atol=1e-2, rtol=1e-2)`，见 [common/utils.py:66-87](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L66-L87) 与 [common/utils.py:71-72](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L71-L72) 的默认 `atol/rtol`。float16 精度有限，所以容差比常见的 `1e-5` 宽松。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能在 kernel 里直接写 `C = A + B`，而要写 `for i in T.Parallel(...)` 循环？

> **参考答案**：`A + B` 是元素级标量算术，它一次只算一个标量；而 `A`、`B`、`C` 是 global memory 上的张量指针。要遍历整段 tile 的所有元素，必须用一个循环。`T.Parallel` 正是声明「这些迭代相互独立、可以并行分摊给线程」的循环抽象。`T.copy` 之所以不用写循环，是因为它本身是 TileOp，框架内置了遍历与并行。

**练习 2**：`T.Parallel(BLOCK_N)` 里的 `BLOCK_N` 和 `T.Kernel(..., threads=256)` 里的 `256` 是什么关系？

> **参考答案**：二者解耦。`BLOCK_N` 是「每个 block 要处理的元素数 / 循环迭代次数」；`256` 是「每个 block 配置的线程数」。框架自动把 `BLOCK_N` 次迭代映射到这 256 个线程上（线程数 < 迭代数时每线程处理多个元素）。所以改 `threads` 不影响正确性。

### 4.2 T.if_then_else 条件表达式

#### 4.2.1 概念说明

ReLU 的定义是 \( \text{ReLU}(x) = \max(0, x) \)：当 \( x > 0 \) 时取 \( x \)，否则取 \( 0 \)。这是一个**条件**。

你可能会想直接写 Python 原生的 `if x > 0: ...`。**但这在 TileLang 里行不通**，原因是：

- TileLang 的循环体是被编译器**符号化处理**后生成 CUDA 代码的。
- 一个 Python `if` 会在**编译期**被求值（它是静态的），它无法表达「每个元素根据自己的数据值，在运行时走不同分支」。
- 你需要的是一个**运行时**的条件选择。

`T.if_then_else(cond, true_value, false_value)` 就是这个工具。它是一个**表达式**（返回一个值，而不是一条语句）：在生成的 CUDA 里，它会编译成类似三目运算符 / 条件移动（`select`）的指令，让每个线程根据自己的 `cond` 取不同的值。题目里的提示也明确指向它：

> HINT: We can use `T.if_then_else(cond, true_value, false_value)` to implement conditional logic.
> —— [puzzles/02-vector-add.py:72](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L72)

#### 4.2.2 核心流程

`T.if_then_else` 的签名与语义：

```
T.if_then_else(cond, true_value, false_value)
```

| 参数 | 含义 | 本讲例子 |
| --- | --- | --- |
| `cond` | 布尔比较表达式，运行时逐元素求值 | `x > 0` |
| `true_value` | `cond` 为真时取的值 | `x` |
| `false_value` | `cond` 为假时取的值 | `0` |

于是 \( \text{ReLU}(x) = \max(0, x) \) 可写成：

\[ \text{C}[i] = \text{T.if\_then\_else}(\, A[i]\cdot B[i] > 0,\; A[i]\cdot B[i],\; 0 \,) \]

要点：

- 它是**表达式**，可以直接放在赋值号右边。
- `cond` 是一个**比较**（如 `> 0`），结果是逐元素的布尔值。
- 它可以**嵌套**，用来表达更复杂的分段函数（见第 5 节综合实践的 clamp）。
- 当字面量需要参与计算时，为了 dtype 对齐，常用 `T.float16(1)`、`T.float16(0)` 这样的显式转型写法（项目文档在 backward-op 里就是这么写的，见 [docs/zh/4.backward-op/2.implementation-guide.md](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/zh/4.backward-op/2.implementation-guide.md) 中 `T.if_then_else(z > 0, T.float16(1), T.float16(0))`）。本讲里 `false_value` 直接写 `0` 是因为答案如此，能通过验证。

#### 4.2.3 源码精读

参考答案里 ReLU 部分的写法：

```python
for i in T.Parallel(BLOCK_N):
    C[base_idx + i] = T.if_then_else(
        A[base_idx + i] * B[base_idx + i] > 0,
        A[base_idx + i] * B[base_idx + i],
        0,
    )
```

参考：[ans/02-vector-add.py:109-113](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L109-L113)。注意：

- `cond` 是 `A[...] * B[...] > 0`：先做元素级乘法，再和 `0` 比较。
- `true_value` 与 `false_value` 都写成了 `T.if_then_else` 的参数，整个表达式直接赋给 `C[base_idx + i]`。
- 这一行既用了元素级乘法（4.1），又用了条件表达式（4.2），是两个模块的结合点。

#### 4.2.4 代码实践

**实践目标**：通过「改 `cond` 的阈值」直观感受 `T.if_then_else` 是逐元素生效的运行时条件。

**操作步骤**：

1. 先按 4.3 的主任务把 `tl_mul_relu_1d` 补全（得到 `> 0` 的 ReLU 版本）并验证通过。
2. 临时把 `cond` 从 `A[..] * B[..] > 0` 改成 `A[..] * B[..] > 0.5`（示例代码，仅用于观察），同时**手动**心算：哪些位置的乘积大于 0.5 才保留原值，其余归零。
3. 运行 `test_puzzle(tl_mul_relu_1d, ref_mul_relu_1d, {"N": N, "BLOCK_N": BLOCK_N})`。

**需要观察的现象**：因为你改了阈值，但 `ref_mul_relu_1d` 仍是标准 ReLU（阈值 0），所以此时 `test_puzzle` 应当报 `❌ Results match: False`，并在 `Diff` 里能看到哪些元素不一致——这正好说明 `T.if_then_else` 是按你写的 `cond` 逐元素判定的。

**预期结果**：改回 `> 0` 后重新 `✅ Results match: True`。（阈值 `0.5` 的版本仅用于观察，验证完请改回 `> 0`。）

#### 4.2.5 小练习与答案

**练习 1**：`T.if_then_else` 是「语句」还是「表达式」？为什么不能用它像 Python `if` 那样跨多行控制流？

> **参考答案**：它是**表达式**，返回一个值，通常直接放在赋值号右边或更大的表达式里。它表达的是「逐元素的条件取值」，编译成运行时的 `select` 类指令，而不是控制多个语句块是否执行的编译期分支。

**练习 2**：用嵌套的 `T.if_then_else` 写出 \( \text{clamp}(x, -1, 1) = \min(\max(x, -1), 1) \)。

> **参考答案**：先做内层「上截断」、再做外层「下截断」：
> ```python
> # 示例代码
> inner = T.if_then_else(x > 1, T.float16(1), x)   # max/min 上界：大于1则取1
> out   = T.if_then_else(inner < -1, T.float16(-1), inner)  # 下界：小于-1则取-1
> ```
> 注意字面量用 `T.float16(...)` 显式转型以对齐 dtype（待本地验证在你的 TileLang 版本下是否必须）。

### 4.3 融合乘法 + ReLU：把两个算子写进一个 kernel

#### 4.3.1 概念说明

有了 4.1 的 `T.Parallel` 和 4.2 的 `T.if_then_else`，我们就能实现本讲的主任务：**逐元素乘法 + ReLU**。

为什么不直接写两个 kernel（先 `C = A * B`，再 `D = relu(C)`）？因为这会带来：

1. **多一次 kernel 启动开销**（launch overhead）。
2. **多一次 global memory 往返**：中间结果 `C` 要写回显存，下一个 kernel 再读进来。

**算子融合（kernel fusion）**的思路是：把两个算子合并成一个 kernel，中间的乘积 `A[i]*B[i]` 直接留在寄存器里，马上喂给 ReLU，**不必落盘到 global memory**。这正是 `tl_mul_relu_1d` 要做的事：

\[ C[i] = \max(0,\; A[i] \cdot B[i]) \]

> 预告：在本讲的朴素实现里，「中间结果留在寄存器」是隐式发生的。下一讲 [u2-l2](u2-l2-memory-hierarchy-fragments.md) 会用 `T.alloc_fragment` 把这个寄存器缓存做得**显式**，并对比生成的 CUDA 代码。

#### 4.3.2 核心流程

`tl_mul_relu_1d` 的结构与 4.1 的 `tl_add_1d` **完全同构**，唯一的区别是把循环体里的「`A + B`」换成「`T.if_then_else(A*B > 0, A*B, 0)`」：

```
with T.Kernel(N // BLOCK_N, threads=256) as bx:
    base_idx = bx * BLOCK_N
    for i in T.Parallel(BLOCK_N):
        # 把「乘法」和「ReLU」融合在同一个循环体里
        C[base_idx + i] = T.if_then_else(A[..] * B[..] > 0, A[..] * B[..], 0)
```

执行流程：分块 → `T.Parallel` 逐元素 → 每个元素先乘、再用 `T.if_then_else` 做 ReLU → 写回 `C`。

#### 4.3.3 源码精读

先看题目里待补全的骨架（`# TODO` 处是空的）：

```python
@tilelang.jit
def tl_mul_relu_1d(A, B, BLOCK_N: int):
    N = T.const("N")
    A: T.Tensor((N,), T.float16)
    B: T.Tensor((N,), T.float16)
    C = T.empty((N,), T.float16)

    # TODO: Implement this function

    return C
```

参考：[puzzles/02-vector-add.py:101-110](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L101-L110)（[第 108 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L108) 是 `# TODO`）。再看参考答案的完整实现：

```python
@tilelang.jit
def tl_mul_relu_1d(A, B, BLOCK_N: int):
    N = T.const("N")
    A: T.Tensor((N,), T.float16)
    B: T.Tensor((N,), T.float16)
    C = T.empty((N,), T.float16)

    with T.Kernel(N // BLOCK_N, threads=256) as pid_n:
        base_idx = pid_n * BLOCK_N
        for i in T.Parallel(BLOCK_N):
            C[base_idx + i] = T.if_then_else(
                A[base_idx + i] * B[base_idx + i] > 0,
                A[base_idx + i] * B[base_idx + i],
                0,
            )

    return C
```

参考：[ans/02-vector-add.py:98-115](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L98-L115)（核心循环见 [第 106-113 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L106-L113)）。对照要点：

- 答案用的是 `pid_n`，题目里 `tl_add_1d` 用的是 `bx`——同一个块索引，名字不同而已。
- 循环体里 `A[base_idx + i] * B[base_idx + i]` 这个乘积出现了两次（一次在 `cond`，一次在 `true_value`）。在朴素实现里，编译器（如 NVCC 的公共子表达式消除 CSE）通常能复用这个中间值；这正是融合带来的好处——乘积留在寄存器里被 ReLU 直接复用。
- torch 参考 `ref_mul_relu_1d` 是 `(A * B).relu_()`，见 [ans/02-vector-add.py:90-95](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L90-L95)，即我们的对齐目标。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：补全 `puzzles/02-vector-add.py` 里的 `tl_mul_relu_1d`，实现 \( C[i] = \max(0, A[i]\cdot B[i]) \)，并用 `test_puzzle` 验证与 torch 一致。

**操作步骤**：

1. 打开 `puzzles/02-vector-add.py`，定位到 [`tl_mul_relu_1d`](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L101-L110) 的 `# TODO`（第 108 行）。
2. 参照 4.1 的 `tl_add_1d` 写出 `with T.Kernel(...)` 与 `for i in T.Parallel(BLOCK_N)`，把循环体写成 4.2 的 `T.if_then_else(...)`。建议照搬题目里 `tl_add_1d` 的命名（用 `bx`），保持文件风格一致。
3. 运行验证。可以用文件里自带的 `run_mul_relu_1d()`：

   ```bash
   python3 -c "
   import importlib.util
   spec = importlib.util.spec_from_file_location('p02', 'puzzles/02-vector-add.py')
   m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
   m.run_mul_relu_1d()
   "
   ```

   `run_mul_relu_1d()` 内部会调用 `test_puzzle(tl_mul_relu_1d, ref_mul_relu_1d, {"N": 1024*256, "BLOCK_N": 1024})`，见 [puzzles/02-vector-add.py:113-117](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L113-L117)。

**需要观察的现象**：终端打印 `✅ Results match: True`。

**预期结果**：与 torch 的 `(A * B).relu_()` 在 `atol=rtol=1e-2` 下一致。若出现 `❌`，`test_puzzle` 会打印 `Yours` / `Spec` / `Diff` / `Max diff`，按 diff 排查（常见错误：忘了 `base_idx + i` 偏移、`cond` 写反、或没写 `T.Parallel` 直接当成串行循环）。

#### 4.3.5 小练习与答案

**练习 1**：为什么「融合」后的 mul-relu 通常比「先 mul kernel、再 relu kernel」更快？

> **参考答案**：融合后 (1) 少了一次 kernel 启动开销；(2) 中间乘积 `A*B` 留在寄存器里直接喂给 ReLU，不必写回 global memory 再读回，省掉一次显存往返。访存往往是 GPU kernel 的瓶颈，省一次显存读写收益明显。

**练习 2**：题目 HINT 明确要求用 `T.if_then_else` 实现 ReLU。如果只看数学定义 \( \max(0, x) \)，你觉得还可以有哪些写法？为什么本讲仍坚持用 `T.if_then_else`？

> **参考答案**：从数学上 `max(0, x)` 也可由「取最大值」类内置函数实现（项目文档提到可「直接用 `max(0, x)`」风格），具体是否提供 `T.max` 等内置需查阅 `tilelang.language` 文档确认（**待确认**）。但本讲坚持用 `T.if_then_else`，是因为它是表达「逐元素运行时条件」最通用、可嵌套的写法，掌握它之后你能实现 clamp、Leaky ReLU、掩码等任意分段逻辑，而不只是 ReLU。

## 5. 综合实践

把本讲三个模块串起来，实现一个**融合乘法 + clamp** 的一维 kernel。

**任务**：在 `puzzles/02-vector-add.py` 里仿照 `tl_mul_relu_1d`，**新增**一个 kernel `tl_mul_clamp_1d`，计算

\[ C[i] = \text{clamp}(A[i]\cdot B[i],\; -1,\; 1) = \min(\max(A[i]\cdot B[i],\; -1),\; 1) \]

要求：

1. 用 `T.Parallel` 逐元素遍历（模块 4.1）。
2. 用**嵌套**的 `T.if_then_else` 实现 clamp（模块 4.2，参考 4.2.5 练习 2 的答案）。
3. 字面量用 `T.float16(1)` / `T.float16(-1)` 显式转型以对齐 dtype。
4. 写一个对应的 torch 参考，例如：

   ```python
   # 示例代码
   def ref_mul_clamp_1d(A, B):
       return (A * B).clamp(-1, 1)
   ```

5. 用 `test_puzzle(tl_mul_clamp_1d, ref_mul_clamp_1d, {"N": 1024*256, "BLOCK_N": 1024})` 验证。

**验收标准**：终端打印 `✅ Results match: True`。这个任务同时用到「元素级算术 + `T.Parallel` + 嵌套 `T.if_then_else` + 算子融合」四个要点，是把本讲内容串起来的最小综合练习。

> 提示：因为 `test_puzzle` 把参数表里**最后一个张量**当作输出，你的 kernel 签名写成 `(A, B, BLOCK_N)` 并 `return C` 即可，框架会自动为 `A`、`B` 造随机输入、把 `C` 当输出。字面量转型的具体行为**待本地验证**。

## 6. 本讲小结

- **`T.Parallel` 是元素级运算的循环抽象**：加减乘除是元素级标量算术（不是 TileOp），必须用 `for i in T.Parallel(N)` 遍历元素，框架再把迭代并行分摊给 block 内线程。
- **元素级算术 vs TileOp**：`T.copy` 整段搬运、框架包办并行；`T.Parallel` + 算术需要你写循环体，但并行仍由框架自动完成。
- **`T.if_then_else(cond, a, b)` 是运行时条件表达式**：用于「逐元素根据数据走不同分支」，弥补了 Python `if` 只能在编译期静态求值的不足。
- **算子融合**：把 mul 和 relu 写进同一个 `T.Parallel` 循环体，中间乘积留在寄存器，省一次显存往返和一次 kernel 启动。
- **块索引命名**：题目用 `bx`、答案用 `pid_n`，是同一个 `blockIdx`，不要被名字迷惑。
- **验证手段**：`test_puzzle` 用 `torch.allclose(atol=rtol=1e-2)` 比对 TileLang 与 torch 参考，float16 容差较宽松。

## 7. 下一步学习建议

下一讲 [u2-l2 GPU 内存层级与 T.alloc_fragment](u2-l2-memory-hierarchy-fragments.md) 会把本讲的朴素 mul-relu 升级为「显式用寄存器缓存」的 `tl_mul_relu_1d_mem`，你会学到：

- GPU 的三级内存（global / shared / registers）与用寄存器优化的动机（如 `ldg128`）。
- `T.alloc_fragment` 作为「block 内寄存器的统一抽象」。
- `pass_configs`（`TL_DISABLE_WARP_SPECIALIZED` / `TL_DISABLE_TMA_LOWER`）与 `compile().print_source_code()` 检视生成的 CUDA 代码。

建议提前阅读 [puzzles/02-vector-add.py:120-173](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L120-L173) 里关于内存层级的注释，带着「本讲的乘积是怎么隐式留在寄存器的」这个问题进入下一讲。再之后，[u2-l3](u2-l3-puzzle03-outer-vec-add.md) 会把 `T.Parallel` 扩展到二维 `T.Parallel(i, j)`。
