# 控制流与循环原语

## 1. 本讲目标

本讲承接 u2-l2（内存层级与数据搬运）。上一讲我们学会了用 `T.copy` 在 global→shared→fragment 之间搬数据，但真实 kernel 里数据搬运几乎总是嵌在「循环 + 条件」里：沿 K 维反复搬 tile、对边界做判断、用并行循环做 elementwise。

学完本讲，你应当能够：

- 用 `T.serial` / `T.unroll` / `T.Parallel` 三种循环原语写出正确的 tile 级循环，并知道各自适用的场景。
- 区分「TIR 条件（运行时判断）」和「Python 常量条件（编译期折叠）」，会用 `if/elif/else`、三元表达式、`while`、`break/continue`。
- 用 `T.any_of` / `T.all_of` 表达多维边界判断。
- 理解 `LegalizeSafeMemoryAccess` 这个 Pass 如何自动为可能越界（OOB, Out-Of-Bounds）的 global 访问插入运行时守卫，从而在很多场景下省略显式的 `if`。

本讲覆盖的最小模块是 `tilelang.language.loop` 与 `tilelang.language.logical`，并顺带讲清条件控制流在 eager builder 里的实现，以及负责自动边界保护的编译 Pass。

## 2. 前置知识

阅读本讲前，请确认你已经了解：

- **TIR PrimFunc**：tilelang 的 kernel 在编译期会被搭建成一个 TVM TIR 的 `PrimFunc`（见 u2-l1）。你在函数体里写的 `for`、`if` 不是「Python 运行时执行」，而是「编译期被记录成 IR 节点」。
- **Builder 与 frame 栈**：tilelang 用一个 eager builder 逐句解释你的函数体，每遇到一个 `with`/`for`/`if` 就压入一个 frame，退出时弹出并把子语句挂到父节点上（见 u1-l3、u5-l1）。理解这一点，就能理解为什么「Python 常量条件」和「TIR 表达式条件」会被区别对待。
- **scope 与缓冲**：global / shared / fragment 的区别，以及 `T.copy`、`T.alloc_shared`、`T.alloc_fragment`、`T.alloc_var` 的用法（见 u2-l2）。
- **循环展开（unroll）** 与 **软件流水线（pipeline）** 的直觉：unroll 用空间换延迟，pipeline 用多级缓冲隐藏访存延迟。

一个关键直觉：在 tilelang 里写 `for i in T.serial(N):` 时，`N` 是一个编译期已知的 trip count，`i` 是一个 TIR 循环变量；而 `if i < N:` 中的 `i < N` 是一个 TIR 布尔表达式，它会被原样记录到 IR 里，最终翻译成设备端的 `if`。这与普通 Python 里「循环真的跑了一遍」截然不同。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/loop.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/loop.py) | 循环原语的 Python 入口：`serial` / `unroll` / `Parallel` / `Pipelined` / `Persistent` / `vectorized`，以及大写别名。 |
| [tilelang/language/logical.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/logical.py) | 逻辑归约 `any_of` / `all_of`，把缓冲/区域归约成一个布尔标量。 |
| [tilelang/language/eager/builder.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py) | eager builder：把 `if`/`while`/`break`/`continue`、三元、`and/or/not`、带步长循环翻译成 TIR 控制节点。 |
| [tilelang/language/common.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py) | 语言门面，把 loop / logical 的符号 re-export 成 `T.serial` / `T.Parallel` / `T.any_of` 等。 |
| [src/transform/legalize_safe_memory_access.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/legalize_safe_memory_access.cc) | C++ Pass `LegalizeSafeMemoryAccess`：分析每个 global 访问的索引，无法证明在界内时插入运行时守卫。 |
| [tilelang/transform/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/__init__.py) | Python 侧 Pass 包装，含 `LegalizeSafeMemoryAccess`、`IfStmtBinding`、`LoopUnswitching` 等。 |
| [tilelang/transform/pass_config.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py) | `PassConfigKey`，含关闭安全访问/越界警告的开关。 |
| [docs/programming_guides/control_flow.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/control_flow.md) | 官方控制流指南，本讲的权威依据。 |

---

## 4. 核心概念与源码讲解

### 4.1 循环原语：serial / unroll / Parallel

#### 4.1.1 概念说明

tilelang 提供三种最常用的 tile 级循环：

- **`T.serial(N)`**：普通的串行 for 循环，迭代变量从 0 到 N−1。它是最「老实」的循环——不并行、不展开，按顺序生成。常用于 K 维累加、归约、流水线的主循环。
- **`T.unroll(N)`**：请求编译器展开的循环，适合 trip count 较小、希望减少分支开销的场景（例如展开一个小常数的内层 K 循环）。
- **`T.Parallel(M, N, ...)`**：构建**嵌套的并行循环**，专门用于 elementwise 计算。它的语义是「这些迭代之间相互独立、可被映射到线程」，因此非常适合「把一个 tile 的每个元素都做同样的事」。`T.Parallel` 会在后续 `LayoutInference` Pass 里被映射到 CUDA 线程，是 tilelang 表达数据并行的主力。

> 小贴士：tilelang 用大写命名（`T.Serial` / `T.Unroll` / `T.Parallel`）来强调这是「tile 级循环」，小写（`T.serial` / `T.unroll`）是其别名，二者完全等价。

#### 4.1.2 核心流程

三者都返回一个 `ForFrame` 上下文管理器。当你在 builder 上下文里写 `for i in T.serial(N):` 时：

1. `T.serial(N)` 被调用，返回一个 `ForFrame`（步长为 1 时直接复用 TVM 的 `tir.serial`，否则构造带步长版本）。
2. Python 的 `for ... in <ForFrame>` 触发 builder 的 `ctx_for`，把 frame 压入 frame 栈，进入循环体作用域。
3. 函数体里的语句被记录为该循环的子语句。
4. 退出 `for` 时，frame 弹出，生成一个 TIR `For` 节点（`Serial`/`Unroll`/`Parallel` 对应不同的 `ForKind`）。

对比三种循环生成节点的差异：

| 原语 | TIR ForKind | 含义 | 典型用法 |
| --- | --- | --- | --- |
| `T.serial` | `SERIAL` | 顺序执行 | K 维累加、归约、流水线主循环 |
| `T.unroll` | `UNROLLED`（带 pragma） | 提示展开 | 小常数内层循环 |
| `T.Parallel` | `PARALLEL` | 数据并行、可映射线程 | elementwise 写回、tile 内并行 |

#### 4.1.3 源码精读

`serial` 的实现：当步长缺省或为 1 时直接复用 TVM 的 `tir.serial`；否则走带步长路径（见 4.3.3）。

[serial 的主体逻辑：tilelang/language/loop.py:L223-L232](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/loop.py#L223-L232) —— 这里先判断 `step` 是否为 1，决定走哪条路径。

`unroll` 的实现多了一步：它会在 annotation 里默认塞入 `pragma_unroll_explicit`，并把可选的 `unroll_factor` 翻译成 `pragma_unroll_factor`：

[unroll 的 annotation 处理：tilelang/language/loop.py:L280-L297](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/loop.py#L280-L297) —— 注意当传入 `unroll_factor` 时，要求 `pragma_unroll_explicit` 必须为 `False`（即「设置展开因子但不强制完全展开」），否则抛 `ValueError`。

`Parallel` 是三者里最特殊的：它接受多个 extent，把 `coalesced_width` / `loop_layout` / `prefer_async` 等高级 hint 收集进一个 `annotations` 字典，然后调用 C++ 侧的 `_ffi_api.Parallel`：

[Parallel 收集 annotation 并调用 FFI：tilelang/language/loop.py:L78-L87](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/loop.py#L78-L87) —— 多维 extent 会被 C++ 侧展开成嵌套的 parallel 循环，body 一次性接收所有下标（如 `for i, j in T.Parallel(M, N)`）。

大写别名只是简单转发，源码注释明确说明了命名意图：

[Serial/Unroll 别名与命名注释：tilelang/language/loop.py:L300-L326](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/loop.py#L300-L326) —— 「We use uppercase to emphasize that they are tile-level loops.」

这些符号通过 `common.py` re-export 到 `T.` 命名空间：

[common.py 导出循环原语：tilelang/language/common.py:L21-L31](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py#L21-L31)。

#### 4.1.4 代码实践

**实践目标**：直观感受三种循环生成的 TIR 差异。

操作步骤（源码阅读型 + 本地可选运行）：

1. 阅读官方指南的 Loops 一节，对比三种循环的写法：[docs/programming_guides/control_flow.md:L49-L82](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/control_flow.md#L49-L82)。
2. 用下面的「示例代码」写三个 elementwise 向量加法 kernel，分别用 `T.serial`、`T.unroll`、`T.Parallel`：

```python
# 示例代码：仅用于对比三种循环，非项目原有文件
import tilelang
import tilelang.language as T

N = 128

@tilelang.jit
def add_serial(A: T.Tensor((N,), "float32"), B: T.Tensor((N,), "float32"), C: T.Tensor((N,), "float32")):
    with T.Kernel(1, threads=128) as bx:
        for i in T.serial(N):
            C[i] = A[i] + B[i]

@tilelang.jit
def add_parallel(A: T.Tensor((N,), "float32"), B: T.Tensor((N,), "float32"), C: T.Tensor((N,), "float32")):
    with T.Kernel(1, threads=128) as bx:
        for i in T.Parallel(N):
            C[i] = A[i] + B[i]
```

3. 用 `add_serial.get_kernel_source()` 与 `add_parallel.get_kernel_source()` 查看生成的设备源码。

需要观察的现象：

- `T.serial` 版本通常生成「单个线程跑完整个循环」的串行代码。
- `T.Parallel` 版本会被映射到线程，生成「每个线程处理若干元素」的并行代码（具体线程映射由 `LayoutInference` 决定）。

预期结果：`T.Parallel` 版本的延迟应显著低于 `T.serial`。**待本地验证**（需要 GPU 环境；无 GPU 时可只做源码阅读对比）。

#### 4.1.5 小练习与答案

**练习 1**：把 `add_parallel` 改成二维 `for i, j in T.Parallel(M, N): C[i, j] = A[i, j] + B[i, j]`，`T.Parallel` 接收了几个 extent？生成的循环嵌套是几层？

**答案**：接收 2 个 extent（`M`、`N`），生成 2 层嵌套的 parallel 循环，body 里一次拿到 `i, j` 两个下标。

**练习 2**：`T.unroll(K_TILE)` 里，如果同时传了 `unroll_factor=8`，但没把 `explicit` 设为 `False`，会发生什么？

**答案**：会抛 `ValueError("pragma_unroll_explicit must be True when unroll_factor is not None")`（见 loop.py 的校验逻辑）。其语义是：指定展开因子时必须是「非完全展开」模式。

---

### 4.2 条件控制流：if / elif / else、三元与逻辑归约

#### 4.2.1 概念说明

tilelang 支持在 kernel 里写标准的 Python 条件：

- **`if` / `elif` / `else`**：条件应是一个 TIR 表达式（如 `i < N`），它会被记录成 TIR 的 `IfThenElse` 节点，最终翻译成设备端的分支。
- **三元表达式** `x if cond else y`：当 `cond` 是 TIR 表达式时，会翻译成 `tir.if_then_else(cond, x, y)`。
- **短路布尔** `a and b` / `a or c` / `not a`：当操作数是 TIR 表达式时，翻译成 `tir.And` / `tir.Or` / `tir.Not`。

这里最容易踩的坑是：**Python 常量条件** 和 **TIR 条件** 的区别。

- 若条件在编译期就能求值成 Python `bool`（例如 `if 128 < 256:` 或 `if BLOCK_M == 64:`），builder 会把它当成编译期常量，**只生成then 或 else 一条分支**（常量折叠）。
- 若条件含 TIR 变量（例如 `if i < N:`，`i` 是循环变量、`N` 是符号维度），它会被原样保留成运行时分支。

此外，对于多维边界判断，tilelang 提供 `T.any_of(...)` / `T.all_of(...)`，把一组条件或一个缓冲归约成单个布尔标量，可读性比手写嵌套 `and` 更好。

#### 4.2.2 核心流程

条件控制流的翻译集中在 eager builder 的几个方法里：

1. 遇到 `if cond:`，builder 调 `unwrap_cond(cond)` 把条件归一化：TIR 表达式原样保留；`IntImm`/Python 布尔折成 Python `bool`。
2. 若得到 `PrimExpr`，进入 `tir.If` frame，then/else 各开一个子 frame；若得到 Python `bool`，则直接在 Python 层选择执行哪个分支（不生成 IR 分支）。
3. 三元 `x if cond else y` 走 `ifexp`：TIR 条件时生成 `tir.if_then_else`。
4. `and/or/not` 走 `boolop`：TIR 操作数时生成 `tir.And/Or/Not`。

#### 4.2.3 源码精读

条件的归一化在 `unwrap_cond`：

[unwrap_cond：区分 PrimExpr 与 Python 常量：tilelang/language/eager/builder.py:L84-L102](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L84-L102) —— 注意末尾：当一个普通 Python 表达式被当作条件时，会打印 warning 并当作「常量表达式」处理。这就是「TIR 条件 vs Python 常量」分歧点。

`if/elif/else` 的实际进入逻辑：

[ctx_if / ctx_then / ctx_else：tilelang/language/eager/builder.py:L296-L319](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L296-L319) —— `ctx_if` 在条件为 `PrimExpr` 时开 `tir.If` frame，并把一个 `_has_if_frame` 哨兵传给 `ctx_then`；否则直接把 Python 布尔结果传下去，由 `ctx_then`/`ctx_else` 在 Python 层决定是否执行。这正好对应「TIR 条件生成分支、常量条件只走一支」。

三元与布尔运算：

[ifexp 与 boolop：tilelang/language/eager/builder.py:L619-L639](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L619-L639) —— `ifexp` 在 TIR 条件下生成 `tir.if_then_else`；`boolop` 在 TIR 操作数下生成 `tir.And/Or/Not`，否则回落到父类的 Python 求值。

多维边界的 `all_of` / `any_of`：

[any_of / all_of：tilelang/language/logical.py:L12-L48](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/logical.py#L12-L48) —— 它们接收一个 `Buffer` 或 `BufferRegion`，调用 `tl.any_of` / `tl.all_of` 这个 intrinsic，把缓冲元素归约成一个 `bool` 标量返回。文档里推荐的用法是把多个标量条件 `T.all_of(gi < M, gj < N)` 写在一起以提升可读性。

官方指南对条件与边界处理的总结：

[Conditionals 与边界处理说明：docs/programming_guides/control_flow.md:L16-L45](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/control_flow.md#L16-L45)。

#### 4.2.4 代码实践

**实践目标**：体会「常量条件被折叠、TIR 条件被保留」的差异。

操作步骤：

1. 阅读真实示例里的边界判断写法，例如 GQA varlen 中的 `if bx * block_M + i < q_current_seqlen:`，这是典型的「TIR 条件」用法：[examples/flash_attention/example_gqa_bwd_tma_reduce_varlen.py:L70](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/flash_attention/example_gqa_bwd_tma_reduce_varlen.py#L70)。
2. 用下面的「示例代码」做对比实验：

```python
# 示例代码
import tilelang
import tilelang.language as T

@tilelang.jit
def kernel_const_fold(A: T.Tensor((64,), "float32"), C: T.Tensor((64,), "float32")):
    with T.Kernel(1, threads=64) as bx:
        for i in T.serial(64):
            # BLOCK_M 没有定义；这里用常量 64 与符号无关，编译期可折叠
            if 64 < 128:          # Python 常量条件 -> 编译期折叠，只保留 then 分支
                C[i] = A[i] * 2.0
            else:
                C[i] = A[i]
```

3. 再写一个含 TIR 变量条件的版本 `if i < 32:`，用 `get_kernel_source()` 对比两者。

需要观察的现象：

- 常量条件版本生成的源码里**看不到分支**（else 分支被丢弃）。
- TIR 条件版本生成的源码里**能看到真实的 `if`**。

预期结果：常量折叠发生在 builder 阶段（`ctx_if` 走 Python `bool` 分支）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：下面两个条件，哪个会生成运行时分支？为什么？
(a) `if T.ceildiv(K, BK) > 0:`，其中 `K`、`BK` 都是编译期整数。
(b) `if ko < T.ceildiv(K, BK):`，其中 `ko` 是 `T.Pipelined` 的循环变量。

**答案**：(a) 不会——`T.ceildiv` 在编译期可求值为 Python 整数，整个条件是 Python 常量，被折叠。(b) 会——`ko` 是 TIR 循环变量，条件是 TIR 表达式，保留为运行时分支。

**练习 2**：用 `T.all_of` 改写 `if (gi < M) and (gj < N):`，说明它的好处。

**答案**：写成 `if T.all_of(gi < M, gj < N):`。好处是可读性更好、意图明确（「所有边界都要满足才执行」），并且 `all_of` 走统一的归约 intrinsic，便于编译器分析（见 logical.py）。

---

### 4.3 while、break/continue 与带步长循环

#### 4.3.1 概念说明

除了 `for`，tilelang 还支持：

- **`while`**：当条件是 TIR 表达式时可用。builder 会显式检测「常量为真」的死循环并报错。
- **`break` / `continue`**：可在 `T.serial` / `T.unroll` / `T.Parallel` / `while` 循环里使用，分别退出或跳过本次迭代。
- **带步长的 `T.serial(start, stop, step)` / `T.unroll(start, stop, step)`**：当 `step != 1` 时，tilelang 用 `ceildiv` 把「带步长区间」换算成一个「步长为 1」的等价循环，循环变量的真实值由 `start + v * step` 还原。

这三者都不是最常用的特性，但理解它们有助于读懂 attention 等复杂 kernel（如带动态起止的 while 型循环）。

#### 4.3.2 核心流程

- `while cond:` → builder 的 `ctx_while` 先求值条件；若折叠成常量「真」则报「无限循环」错误，折叠成常量「假」则警告并跳过循环体；否则开 `tir.While` frame。
- `break` / `continue` → `ctx_break` / `ctx_continue` 生成 `tir.break_loop()` / `tir.continue_loop()`，并压入一个哨兵 frame，用于检测「break/continue 之后的死代码」并警告。
- 带步长循环 → `T.serial(start, stop, step)` 把 `(start, stop, step)` 包成 `SerialForWithStep`；builder 的 `ctx_for` 用 `ceildiv` 算出真实 trip count，构造一个步长为 1 的 `tir.serial`，再把循环变量映射回 `start + v * step`。

带步长循环的等价变换（设 `step > 0`）：

\[
\text{trip} = \left\lceil \frac{\text{stop} - \text{start}}{\text{step}} \right\rceil, \qquad i_v = \text{start} + v \cdot \text{step}, \; v \in [0, \text{trip})
\]

#### 4.3.3 源码精读

带步长循环的数据结构与归一化：

[SerialForWithStep / UnrollForWithStep：tilelang/language/eager/builder.py:L134-L167](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L134-L167) —— `UnrollForWithStep` 只是 `SerialForWithStep` 的子类，二者都只承载 `(start, stop, step, annotations)`。

`ctx_for` 对带步长循环的处理：

[ctx_for：用 ceildiv 计算真实 trip count：tilelang/language/eager/builder.py:L345-L383](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L345-L383) —— 注意 `step == 0` 会抛错；非整数步长会打印 warning。最终 `yield it.start + v * it.step` 把「步长为 1 的内部循环变量 v」还原成用户期望的带步长值。

`while` 与无限循环检测：

[ctx_while：折叠常量条件并检测无限循环：tilelang/language/eager/builder.py:L397-L414](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L397-L414) —— 常量「真」直接 `raise RuntimeError("Infinite while loop detected...")`；常量「假」则 warning 后跳过。

`break` / `continue`：

[ctx_break / ctx_continue：生成 break/continue 并加哨兵：tilelang/language/eager/builder.py:L385-L395](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L385-L395)。

官方指南对 while / break / continue 的说明：

[While Loops 与 Break/Continue：docs/programming_guides/control_flow.md:L113-L130](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/control_flow.md#L113-L130)。

#### 4.3.4 代码实践

**实践目标**：用带步长循环把一个 tile 切成若干子块。

操作步骤：

1. 阅读 `T.serial` / `T.unroll` 的多参数形式：[docs/programming_guides/control_flow.md:L49-L68](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/control_flow.md#L49-L68)。
2. 写一个「示例代码」，用 `T.serial(0, N, 4)` 每 4 个元素一组处理：

```python
# 示例代码
import tilelang
import tilelang.language as T

N = 32

@tilelang.jit
def stride_kernel(A: T.Tensor((N,), "float32"), C: T.Tensor((N,), "float32")):
    with T.Kernel(1, threads=32) as bx:
        for v in T.serial(0, N, 4):   # v = 0, 4, 8, ..., 28
            C[v] = A[v]
```

3. 用 `get_kernel_source()` 查看生成的循环，确认其 trip count 是 `ceildiv(32/4)=8`，且循环变量被还原成 `0 + v*4`。

需要观察的现象：生成的设备代码里循环上界是 8，但每次访问的下标是 `i*4`。

预期结果：与手写 `for v in T.serial(8): idx = v*4; C[idx]=A[idx]` 等价。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`while True: ...` 在 tilelang 里会发生什么？

**答案**：`ctx_while` 会把 `True` 折叠成 Python 常量「真」，直接抛 `RuntimeError("Infinite while loop detected...")`。tilelang 不允许编译期就能确定的无限循环。

**练习 2**：`T.serial(0, 10, 3)` 的真实 trip count 是多少？循环变量依次取哪些值？

**答案**：`trip = ceildiv((10-0)/3) = ceildiv(10,3) = 4`，循环变量取 `0, 3, 6, 9`（`9 < 10` 成立，`12 >= 10` 停止）。

---

### 4.4 LegalizeSafeMemoryAccess：自动 OOB 边界守卫

#### 4.4.1 概念说明

写 tile kernel 时，边界处理是最繁琐的部分。比如用 `T.Parallel(M, N)` 处理一个 `M×N` 的张量，但 grid 按 tile 切分后，**最后一个 tile 很可能超出真实的 M 或 N**（即「尾块」/ residual tile）。如果直接写 `C[gi, gj] = ...`，当 `gi >= M` 或 `gj >= N` 时就是越界（OOB）访问。

tilelang 用一个编译 Pass `LegalizeSafeMemoryAccess` 来自动化这件事：

- 它扫描每个 **global 缓冲** 的 `BufferLoad` / `BufferStore`，对每个索引用 `arith::Analyzer` 做「能否证明在界内」的分析。
- 如果**能证明**在界内，什么都不做（守卫被省略）。
- 如果**无法证明**在界内，就**自动插入一个运行时守卫条件**，把这次访问包起来（等价于自动加了一个 `if index < shape:`）。
- 对 shared / local 缓冲，默认只打 warning（因为 tile 程序里开发者通常自己负责），不强制加守卫。

这意味着：**对于简单的尾块场景，你可以省略显式的 `if gi < M:`，让 Pass 自动补上守卫**。但如果你需要自定义的边界逻辑（例如 OOB 位置填 0 而非跳过），仍要显式写 `if`。

#### 4.4.2 核心流程

`LegalizeSafeMemoryAccess` 的判定流程（针对单个索引 `index` 与缓冲维度 `shape_dim`）：

1. 若 `index` 是常量 → 跳过（编译期已知安全）。
2. 尝试用 `CanProve(index < shape_dim)`（`kSymbolicBound` 强度）证明上界；若失败，再退化用 `const_int_bound` 做一次区间证明。
3. 仍无法证明 → 若该缓冲是 global，则 `PushCondition(index < shape_dim)`，把这个条件收集为运行时守卫。
4. 对下界 `index >= 0` 重复同样的证明与守卫收集。
5. 被收集的守卫条件最终会把对应的访问包成「条件执行」。

> 数学上，Pass 在尝试证明的不等式是：\(0 \le \text{index} < \text{shape\_dim}\)。只有两侧都无法证明时，才需要运行时守卫。

#### 4.4.3 源码精读

Pass 的 Python 包装：

[LegalizeSafeMemoryAccess：tilelang/transform/\_\_init\_\_.py:L168-L176](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/__init__.py#L168-L176) —— 它只是 `_ffi_api.LegalizeSafeMemoryAccess()` 的薄包装，真正的逻辑在 C++。

C++ 里对 global 访问的守卫收集（核心）：

[CheckBufferIndices：证明 index < shape_dim 并按需 PushCondition：src/transform/legalize_safe_memory_access.cc:L280-L316](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/legalize_safe_memory_access.cc#L280-L316) —— 关键点：只有 `IsGlobalBuffer(buffer)` 为真时才 `PushCondition`（即只对 global 自动加守卫）；shared/local 只 `LOG(WARNING)`。

判定缓冲是否 global：

[IsGlobalBuffer：src/transform/legalize_safe_memory_access.cc:L168-L177](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/legalize_safe_memory_access.cc#L168-L177) —— 通过 `buffer.scope() == "global"` 判断。

守卫条件的「可证即省略」逻辑：

[PushScalarCondition：能证明则不入守卫：src/transform/legalize_safe_memory_access.cc:L202-L211](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/legalize_safe_memory_access.cc#L202-L211) —— `CanProve(cond, kSymbolicBound)` 成功就 `return`，不加入运行时条件列表。

控制开关（可用 `PassConfigKey` 关闭）：

[TL_DISABLE_SAFE_MEMORY_ACCESS 与越界警告：tilelang/transform/pass_config.py:L95-L96](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L95-L96) —— `TL_DISABLE_SAFE_MEMORY_ACCESS` 关闭整个安全访问优化；

[TL_DISABLE_OUT_OF_BOUND_WARNING：tilelang/transform/pass_config.py:L277](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L277) —— 关闭 OOB warning。

官方指南对边界处理的总结：

[Residual Tile Handling 示例：docs/programming_guides/control_flow.md:L132-L143](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/control_flow.md#L132-L143) —— 注释明确：「With LegalizeSafeMemoryAccess, the explicit guard can be omitted when you don't need a custom edge path.」

#### 4.4.4 代码实践

**实践目标**：验证「省略 if」与「显式 if」两种写法得到一致结果。

操作步骤（源码阅读型为主，本地可选运行）：

1. 先阅读项目自带的 Pass 测试，理解官方如何断言「守卫被正确插入」：[testing/python/transform/test_tilelang_transform_legalize_safe_memory_access.py:L69-L85](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/transform/test_tilelang_transform_legalize_safe_memory_access.py#L69-L85)。该测试把 kernel 喂给 `tl.transform.LegalizeSafeMemoryAccess()`，再用 `assert_structural_equal` 对比「加守卫后的 IR」与「期望 IR」。

2. 写一个 row-wise softmax 的「尾块」片段（示例代码）。设 tile 宽度 `BN` 大于实际列数 `N`，使每个 tile 都存在 OOB 列：

```python
# 示例代码：row-wise softmax 片段，聚焦边界处理对比
import tilelang
import tilelang.language as T

M, N, BN = 32, 100, 128      # BN > N，每个 tile 都有 28 个 OOB 列
LOG2E = 1.44269504           # log2(e)，用于把 exp 拆成 exp2

# 版本 A：显式 if 守卫
@tilelang.jit
def softmax_explicit_if(A: T.Tensor((M, N), "float32"), B: T.Tensor((M, N), "float32")):
    with T.Kernel(M) as row:
        acc = T.alloc_fragment((BN,), "float32")
        row_max = T.alloc_var("float32", init=-1e30)
        row_sum = T.alloc_var("float32", init=0.0)
        # 1) 载入 + 显式边界判断
        for k in T.serial(BN):
            if k < N:                       # <-- 显式守卫
                acc[k] = A[row, k]
            else:
                acc[k] = -1e30
        # 2) 行最大值
        for k in T.serial(BN):
            row_max[...] = T.max(row_max[0], acc[k])
        # 3) exp2(x - max) 并求和（exp2 为示例；真实 softmax 用 exp）
        for k in T.serial(BN):
            acc[k] = T.exp2((acc[k] - row_max[0]) * LOG2E) if k < N else 0.0
            row_sum[...] = row_sum[0] + acc[k]
        # 4) 归一化写回（只写有效列）
        for k in T.Parallel(BN):
            if k < N:
                B[row, k] = acc[k] / row_sum[0]

# 版本 B：省略 if，依赖 LegalizeSafeMemoryAccess 对 A[row,k]/B[row,k] 自动加守卫
@tilelang.jit
def softmax_auto_guard(A: T.Tensor((M, N), "float32"), B: T.Tensor((M, N), "float32")):
    with T.Kernel(M) as row:
        acc = T.alloc_fragment((BN,), "float32")
        row_max = T.alloc_var("float32", init=-1e30)
        row_sum = T.alloc_var("float32", init=0.0)
        for k in T.serial(BN):
            acc[k] = A[row, k]              # <-- 无显式守卫，由 Pass 自动 guard
        for k in T.serial(BN):
            row_max[...] = T.max(row_max[0], acc[k])
        for k in T.serial(BN):
            acc[k] = T.exp2((acc[k] - row_max[0]) * LOG2E)
            row_sum[...] = row_sum[0] + acc[k]
        for k in T.Parallel(BN):
            B[row, k] = acc[k] / row_sum[0]  # <-- 无显式守卫
```

3. 用两个 kernel 的 `get_kernel_source()` 对比生成代码：
   - 版本 A 的源码里能看到你手写的 `if (k < N)`。
   - 版本 B 的源码里，编译器应为 `A[row, k]` 与 `B[row, k]` 自动插入等价的运行时守卫（可能以 `if_then_else` 或谓词形式出现）。

需要观察的现象：

- 两个 kernel 在 `N=100, BN=128` 下对同一输入应得到**一致**的合法输出（OOB 列不影响结果，因为版本 A 把 OOB 当 −∞/0，版本 B 由守卫跳过 OOB 访问）。
- 用 `pass_configs={PassConfigKey.TL_DISABLE_SAFE_MEMORY_ACCESS: True}` 关闭 Pass 后，版本 B 不再有自动守卫，OOB 访问可能产生错误结果或越界——从而反向证明 Pass 的作用。

预期结果：开启 Pass 时两版本结果一致；关闭 Pass 后版本 B 不再安全。**待本地验证**（需要 GPU；无 GPU 时，重点完成步骤 1 的测试源码阅读）。

> 注意：上面的 softmax 仅为演示控制流与边界处理，数值上做了简化（用 `exp2` + `LOG2E` 近似 `exp`，OOB 位置用 `-1e30` 充当 −∞）。生产级 softmax 请参考 `examples/flash_attention/` 下的 online softmax 实现，它们用 `T.reduce_max` / `T.reduce_sum`（见 u3-l2）做更高效的归约。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `LegalizeSafeMemoryAccess` 默认只对 global 缓冲自动加守卫，而对 shared/local 只打 warning？

**答案**：因为 tile 程序里 shared/local 缓冲的尺寸由开发者用 `T.alloc_shared` / `T.alloc_fragment` 显式声明，越界通常是开发者的逻辑错误而非「尾块」天然现象；强制加守卫会破坏开发者对片上缓冲的精确控制。而 global 缓冲的尺寸来自输入张量，「尾块越界」是 tile 切分的固有副作用，适合自动化（见 `IsGlobalBuffer` 与 `CheckBufferIndices` 的判断）。

**练习 2**：如果你想完全关掉这个 Pass 以观察「裸」的 IR，应该用哪个 `PassConfigKey`？

**答案**：`PassConfigKey.TL_DISABLE_SAFE_MEMORY_ACCESS`（值为 `"tl.disable_safe_memory_legalize"`）。若只是不想看 OOB warning，用 `TL_DISABLE_OUT_OF_BOUND_WARNING`。

---

## 5. 综合实践

把本讲的三块知识（循环原语、条件控制流、自动边界守卫）串起来，完成下面这个「带尾块处理的 2D elementwise scale-and-add」小任务：

**任务**：对一个 `M×N` 的张量做 `C = alpha * A + B`，其中 grid 按 `BM×BN` 的 tile 切分，`M` 和 `N` 都不是 `BM`/`BN` 的整数倍（存在尾块）。

要求：

1. 用 `with T.Kernel(T.ceildiv(M, BM), T.ceildiv(N, BN), threads=BM*BN) as (by, bx):` 声明 grid。
2. 在 `T.Parallel(BM, BN)` 内计算全局坐标 `gi = by*BM + i`、`gj = bx*BN + j`。
3. 写出**两个版本**：
   - 版本 A：用 `if T.all_of(gi < M, gj < N):` 显式守卫。
   - 版本 B：省略 `if`，依赖 `LegalizeSafeMemoryAccess`。
4. 用 `pass_configs={PassConfigKey.TL_DISABLE_SAFE_MEMORY_ACCESS: True}` 关闭 Pass，观察版本 B 是否仍正确，从而理解 Pass 的价值。
5. （可选）用 `kernel.get_profiler().do_bench()` 测两版本延迟，思考「自动守卫」与「显式守卫」在性能上是否有差别。

参考写法见官方指南的 residual tile 例子：[docs/programming_guides/control_flow.md:L132-L143](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/control_flow.md#L132-L143)。

**验收标准**：开启 Pass 时两版本结果与参考实现（如 `torch` 的 `alpha*A+B`）一致；关闭 Pass 后版本 B 在尾块处出错。

## 6. 本讲小结

- tilelang 的三种核心循环：`T.serial`（串行）、`T.unroll`（展开）、`T.Parallel`（数据并行，elementwise 主力）；大写 `T.Serial` / `T.Unroll` 是等价别名，强调「tile 级循环」。
- 条件控制流要区分两类：**TIR 条件**（含循环变量/符号维度，生成运行时分支）与 **Python 常量条件**（编译期折叠，只保留一支）。`unwrap_cond` 是这个分歧的实现点。
- 三元、`and/or/not` 在操作数为 TIR 表达式时分别翻译成 `tir.if_then_else` 与 `tir.And/Or/Not`；多维边界推荐 `T.any_of` / `T.all_of`。
- `while` 会检测常量「真」并报无限循环错；`break`/`continue` 由 builder 翻译成 `tir.break_loop`/`continue_loop` 并带死代码警告。
- 带步长循环 `T.serial(start, stop, step)` 通过 `ceildiv` 归一化成步长为 1 的循环，再用 `start + v*step` 还原下标。
- `LegalizeSafeMemoryAccess` 对无法证明在界内的 **global** 访问自动插入运行时守卫，可证明则省略；这让你在很多尾块场景省略显式 `if`。可用 `TL_DISABLE_SAFE_MEMORY_ACCESS` 关闭。

## 7. 下一步学习建议

- **下一讲 u3-l1（T.gemm 与 tile op 体系）**：本讲的 `T.serial` / `T.Pipelined` 是 GEMM 主循环的骨架，下一讲会把 `T.gemm` 这类「计算原语」放进去，理解 tile op 的注册与 lowering。
- **u3-l2（归约、原子、scan）**：本讲综合实践里手写的行最大值/求和，在下一讲会换成 `T.reduce_max` / `T.reduce_sum`，理解 warp/thread 归约的 lowering。
- **u3-l3（软件流水线）**：本讲只点到了 `T.Pipelined` 的存在，下一讲会深入 producer/consumer 自动推断与 `stage`/`order` 手动注解。
- **延伸阅读源码**：想深入条件如何被进一步优化，可读 `IfStmtBinding` / `MergeIfStmt` / `LoopUnswitching` 三个 Pass（[tilelang/transform/\_\_init\_\_.py:L124-L154](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/__init__.py#L124-L154)），它们分别处理 if 语句绑定、if 合并与循环外提。
