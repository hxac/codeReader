# GPU 内存层级与 T.alloc_fragment

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 GPU 的**三级内存层级**（global memory / shared memory / registers），以及「用寄存器优化访存」的动机（如 `ldg128` 一次搬 128 位）。
- 掌握 **`T.alloc_fragment`**：它是 TileLang 对「一个 block 内所有线程的寄存器」的统一抽象，让你像操作普通 buffer 一样操作寄存器，而不必手写「哪个线程搬哪个元素」的映射。
- 把上一讲的朴素 mul-relu 改写成**显式用寄存器缓存**的 `tl_mul_relu_1d_mem`：用 `T.copy` 在 global memory 与 fragment 之间搬运，计算在 fragment 里完成。
- 认识 `@tilelang.jit` 的 **`pass_configs`** 开关（`TL_DISABLE_WARP_SPECIALIZED` / `TL_DISABLE_TMA_LOWER`），并学会用 `compile().print_source_code()` 检视 TileLang 生成的 CUDA 代码，做「朴素版 vs 优化版」的对照。

## 2. 前置知识

本讲承接 [u2-l1](u2-l1-puzzle02-vector-add.md)（Puzzle 02 Vector Add）。在动手前，请确认你已经理解：

- **`T.Parallel` 与元素级算术**：加减乘除是元素级标量运算，要用 `for i in T.Parallel(N)` 遍历，框架再把迭代并行分摊给 block 内线程。
- **`T.if_then_else`**：运行时逐元素条件表达式，用来实现 ReLU（`max(0, x)`）。
- **算子融合**：上一讲的 `tl_mul_relu_1d` 把「乘法 + ReLU」写进同一个 `T.Parallel` 循环体，中间乘积 `A*B` **隐式**留在寄存器里。
- **`@tilelang.jit` 与 `compile(...)`**：`@tilelang.jit` 装饰的函数是一个可编译对象，调用 `.compile(N=..., BLOCK_N=...)` 会绑定符号维度和分块超参，返回一个可执行的 `JITKernel`。

本讲要回答的核心问题是：**上一讲里那个「隐式留在寄存器」的乘积，能不能用一种显式的、可控的方式写出来？更进一步，我们能不能让一段 tile 的数据一次性搬进最快的存储里，而不是每个元素都去啃最慢的显存？**

> 一句话定位：上一讲关心「算得对、算得并行」；本讲开始关心「**搬得省、搬得快**」——也就是 GPU kernel 性能工程的第一课：**内存层级**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [puzzles/02-vector-add.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py) | 题目文件。含**已完成**的朴素版 `tl_mul_relu_1d`（对照基准）、关于内存层级的两段关键注释、带 `pass_configs` 的**待补全** `tl_mul_relu_1d_mem`（本讲主任务），以及 `run_mul_relu_1d_mem()`（含 `compile().print_source_code()` 与 `bench_puzzle` 对照）。 |
| [ans/02-vector-add.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py) | 参考答案。含 `tl_mul_relu_1d_mem` 的完整实现，供你对照。 |
| [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | `bench_puzzle` 的计时方法学（CUDA Event + warmup + repeats），本讲用它做朴素 vs 优化的性能对比。 |

> 命名提示：和上一讲一样，题目里块索引叫 `bx`、答案里叫 `pid_n`，是同一个 `blockIdx`。本讲引用答案时沿用其 `pid_n`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，恰好对应题目文件里那两段注释和一段带 `pass_configs` 的代码：

1. **4.1** GPU 三级内存层级（global / shared / registers）——为什么要用寄存器。
2. **4.2** `T.alloc_fragment` 寄存器片段——TileLang 怎么把「block 内所有线程的寄存器」抽象成一块可操作的 buffer。
3. **4.3** `pass_configs` 与生成的 CUDA 代码——怎么关掉高级 lower 让生成的代码更易读，并用 `print_source_code()` 对照朴素版与优化版。

### 4.1 GPU 三级内存层级

#### 4.1.1 概念说明

如果你写过 CUDA 或其它 GPU 编程框架，一定接触过 GPU 的内存层级。题目文件的开篇注释把这件事讲得很直接：

> Typically, there are three main levels of memory: **global memory (DRAM)**, **shared memory**, and **registers**. Registers are the fastest but also the smallest form of memory.
> —— [puzzles/02-vector-add.py:128-130](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L128-L130)

一张表记住三者的相对位置（数值为量级直觉，不同架构有差异，**待本地用 nvidia-smi / 设备文档核对**）：

| 层级 | 位置 | 速度 | 容量 | 可见范围 | 在 TileLang 里的对应 |
| --- | --- | --- | --- | --- | --- |
| **global memory**（显存 / DRAM） | 片外 | 最慢（~数百 cycle） | 最大（GB 级） | 所有线程、host 可见 | `T.Tensor` / `T.empty` 分配的张量 |
| **shared memory**（共享内存） | 片上（SM 内） | 中等（~几十 cycle） | 小（每 SM 几十 KB） | **同一 block 内**所有线程 | `T.alloc_shared`（下一阶段才会大量用到） |
| **registers**（寄存器） | 每个核心最贴近 ALU | 最快（~1 cycle） | 最小（每线程数百个） | **线程局部** | `T.alloc_fragment`（本讲主角） |

三个关键直觉：

1. **越快越小**：寄存器最快但容量最小；显存最大但最慢。性能工程的本质，就是尽量让数据在「快的存储」里被反复复用，少跑「慢的显存」。
2. **可见范围决定能否协作**：global 对所有线程可见但不能用来做 block 间同步（上一讲强调过：跨 block 不可同步）；shared 只在同一 block 内可见，**正是 block 内线程协作的桥梁**（本讲先不深入，留给后续矩阵乘讲义）；寄存器是线程局部的。
3. **上一讲的 `A`、`B`、`C` 全是 global 指针**：朴素版每次 `A[base_idx+i]`、`B[base_idx+i]` 都是从显存读，`C[base_idx+i]=...` 是往显存写。题目注释点明了问题所在：

   > Our previous implementation loads data directly from A and B and stores the result to C, where A, B, and C are all passed as **global memory pointers**. This is inefficient because it requires accessing global memory for every single element.
   > —— [puzzles/02-vector-add.py:131-134](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L131-L134)

#### 4.1.2 核心流程

朴素版每个元素的访存可以粗算成「读 A、读 B、写 C」三次 global 操作（项目实现指南亦称「每个元素 3 次全局内存访问」，见 [docs/zh/2.vector-add/2.implementation-guide.md:91-104](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/zh/2.vector-add/2.implementation-guide.md#L91-L104)）。优化的核心思路是**批量搬运**：一次性把一段 tile 的数据从 global 搬进寄存器，计算全在寄存器里完成，最后一次性搬回 global。题目注释给出了一个具体手段：

> The key idea is to copy multiple data elements between registers and global memory **in a single operation**. For example, CUDA often uses **`ldg128`** to load 128 bits of data from global memory into registers at once, which can theoretically **reduce the number of memory accesses by 4x**.
> —— [puzzles/02-vector-add.py:137-140](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L137-L140)

`ldg128` = "load global, 128 bit"：一条指令从显存搬 128 位 = 16 字节 = 8 个 float16 进寄存器。相对按 32 位字（4 字节）一条条搬，理论上是 4 倍少的访存指令。把这段直觉写成流程：

```
朴素版（每个元素都碰显存）：
  for i: 读 A[i](global) → 读 B[i](global) → 算 → 写 C[i](global)

优化版（批量进出寄存器）：
  批量搬: global ──T.copy──► fragment（寄存器）
  块内算: 全在 fragment 上做 mul + relu
  批量搬: fragment ──T.copy──► global
```

> 诚实的补充：题目注释也坦白，朴素版里「中间结果留在寄存器」「批量访存」这些事，**有时 NVCC 会自动帮你做**（公共子表达式消除 CSE、向量化、合并访存等），见 [puzzles/02-vector-add.py:142-145](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L142-L145)。所以本讲真正教会你的是：**把访存意图显式地写进 DSL**，从而不再依赖编译器「碰巧优化」，并且得到一段可读、可控、可对比生成代码的实现。这也是后面所有 puzzle（矩阵乘、卷积、量化）会反复用到的主线。

#### 4.1.3 源码精读

先回顾朴素版（上一讲已实现），重点是它**直接对 global 张量做元素级读写**：

```python
@tilelang.jit
def tl_mul_relu_1d(A, B, BLOCK_N: int):
    ...
    with T.Kernel(N // BLOCK_N, threads=256) as pid_n:
        base_idx = pid_n * BLOCK_N
        for i in T.Parallel(BLOCK_N):
            # A[..]、B[..]、C[..] 都是 global memory 上的读写
            C[base_idx + i] = T.if_then_else(
                A[base_idx + i] * B[base_idx + i] > 0,
                A[base_idx + i] * B[base_idx + i],
                0,
            )
    return C
```

参考：[ans/02-vector-add.py:98-115](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L98-L115)（循环体见 [第 106-113 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L106-L113)）。注意 `A[base_idx + i]` 在 `cond` 和 `true_value` 里出现了两次——乘积 `A*B` 是否被复用，取决于编译器是否做 CSE。这正是我们要用 fragment 显式接管的部分。

再看题目里**待补全**的内存优化版骨架，它比朴素版多了一个 `pass_configs`（4.3 详述）：

```python
@tilelang.jit(
    pass_configs={
        tilelang.PassConfigKey.TL_DISABLE_WARP_SPECIALIZED: True,
        tilelang.PassConfigKey.TL_DISABLE_TMA_LOWER: True,
    },
)
def tl_mul_relu_1d_mem(A, B, BLOCK_N: int):
    N = T.const("N")
    dtype = T.float16
    A: T.Tensor((N,), dtype)
    B: T.Tensor((N,), dtype)
    C = T.empty((N,), dtype)

    # TODO: Implement this function

    return C
```

参考：[puzzles/02-vector-add.py:158-173](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L158-L173)（`# TODO` 在 [第 171 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L171)）。骨架本身还没用到 fragment——那是 4.2 要补进去的内容。

#### 4.1.4 代码实践

**实践目标**：在写代码之前，先用「源码阅读 + 生成代码检视」建立对三级内存的直觉。

**操作步骤**：

1. 阅读题目里关于内存层级的两段注释：[puzzles/02-vector-add.py:120-146](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L120-L146) 与 [puzzles/02-vector-add.py:148-155](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L148-L155)。
2. 用 `print_source_code()` 看一眼**朴素版**生成的 CUDA（即使还没补优化版，朴素版 `tl_mul_relu_1d` 在上一讲已经可用）：

   ```bash
   python3 -c "
   import importlib.util
   spec = importlib.util.spec_from_file_location('p02', 'puzzles/02-vector-add.py')
   m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
   k = m.tl_mul_relu_1d.compile(N=1024*4096, BLOCK_N=1024)
   k.print_source_code()
   "
   ```

   （用 `importlib` 加载是因为 `puzzles/02-vector-add.py` 文件名含连字符，不能直接 `import`。）

**需要观察的现象**：终端会打印出一段（可能很长的）CUDA 源码。在其中寻找：

- 从 global memory 读 A、B 的 load 指令（例如带 `__half` / `half` 类型、或向量化后的 `ldg` / `float4` 之类）。
- 往 global memory 写 C 的 store 指令。
- 注意是否出现了 `ldg.128` / 向量化加载，或被编译器合并成了宽 load。

**预期结果**：朴素版生成的 CUDA 里，A、B、C 的访问都指向 global memory 指针（`__grid_constant__` 之类参数）。是否能观察到 `ldg128` 取决于编译器是否自动向量化——**待本地验证**。这正好引出本讲动机：与其赌编译器优化，不如用 fragment 把批量搬运写成显式语义（4.2）。

#### 4.1.5 小练习与答案

**练习 1**：按「速度从快到慢」「容量从小到大」分别排列 global memory、shared memory、registers。

> **参考答案**：速度从快到慢：registers > shared memory > global memory；容量从小到大：registers < shared memory < global memory。一句话：**越快越小**。

**练习 2**：题目说 `ldg128`「理论上能把访存次数减少 4x」。这个 4x 是相对什么而言的？

> **参考答案**：相对按 32 位（4 字节）一条指令搬一个字的方式。`ldg128` 一次搬 128 位 = 16 字节，是 32 位的 4 倍，所以同样数据量所需的 load 指令数降为 1/4。注意这里讨论的是 float32 字（4 字节）口径；如果按 float16（2 字节）算，128 位能装 8 个，那相对「一次搬一个 fp16」就是 8x。题目取的是 32 位口径的 4x。

### 4.2 T.alloc_fragment 寄存器片段

#### 4.2.1 概念说明

知道「要用寄存器」之后，下一个问题是：**怎么用？**

在原始 CUDA 里，寄存器是**线程局部**的——每个线程有自己的一组寄存器（每线程量级 ~255 个），你在 kernel 里声明一个局部变量 `float x;`，`x` 就住在线程自己的寄存器里。这意味着：如果你想用一个 block 的 256 个线程协作搬 `BLOCK_N=1024` 个元素进寄存器，你得**自己写映射逻辑**——比如「我是第 `tid` 号线程，我负责下标 `tid`、`tid+256`、`tid+512`、`tid+768` 这 4 个元素」。这种「线程↔元素」的手工映射繁琐、易错。

TileLang 的杀手锏就是 `T.alloc_fragment`。题目注释把它的定位说得很清楚：

> TileLang explicitly exposes these memory levels to users. You can use `T.alloc_fragment` to allocate a fragment of registers. ... A fragment is an abstraction of **registers in all threads in a block**. We can manipulate this fragment in a unified way **as we do to a T.Buffer**.
> —— [puzzles/02-vector-add.py:148-155](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L148-L155)

三个要点：

1. **`T.alloc_fragment(shape, dtype)`**：在 block 内分配一块「形状为 `shape`、类型为 `dtype`」的寄存器片段。它在物理上由本 block 所有线程的寄存器共同构成，但在逻辑上你把它当成**一整块 buffer**。
2. **统一操作**：你可以像操作 `T.Buffer` 一样 `A_local[i]` 取元素、用 `T.copy` 整段搬运、用 `T.Parallel` 遍历——**不必手写线程映射**。框架自动把「逻辑下标 `i`」分摊到「具体哪个线程的哪个寄存器」。
3. **与 `T.copy` 天然配合**：`T.copy(global_slice, fragment)` 把一段 global 数据搬进寄存器（框架会选用向量化、合并的 load）；`T.copy(fragment, global_slice)` 把寄存器搬回 global。这正是 4.1 里「批量进出寄存器」的实现手段。

> 与 shared memory 的区别（预告）：fragment 是**寄存器**（线程局部物理上、block 级逻辑上），用来放本 block 要算的临时数据；`T.alloc_shared` 是**共享内存**（block 内线程真正共享、可相互通信）。本讲只用 fragment；矩阵乘阶段会引入 shared memory 做线程协作。

#### 4.2.2 核心流程

把 4.1 的「批量进出寄存器」流程用 fragment 落地，一个 block 内的执行步骤是：

1. **定位 tile**：`base_idx = pid_n * BLOCK_N`，本 block 负责 `[base_idx, base_idx + BLOCK_N)`。
2. **分配片段**：`A_local`、`B_local`、`C_local = T.alloc_fragment((BLOCK_N,), dtype)`——三个寄存器片段，各装 `BLOCK_N` 个元素。
3. **批量搬入**：`T.copy(A[base_idx], A_local)`、`T.copy(B[base_idx], B_local)`——把 global 里的两段 tile 搬进寄存器。
4. **块内计算**：`for i in T.Parallel(BLOCK_N): C_local[i] = relu(A_local[i] * B_local[i])`——全程在寄存器上做，不碰显存。
5. **批量搬出**：`T.copy(C_local, C[base_idx])`——把结果一次性写回 global。

伪代码（对照 4.1 的流程图）：

```
with T.Kernel(blocks, threads) as pid_n:
    base = pid_n * BLOCK_N
    A_local = T.alloc_fragment((BLOCK_N,), dtype)   # 寄存器
    B_local = T.alloc_fragment((BLOCK_N,), dtype)
    C_local = T.alloc_fragment((BLOCK_N,), dtype)
    T.copy(A[base], A_local)                          # global → register（批量）
    T.copy(B[base], B_local)
    for i in T.Parallel(BLOCK_N):                     # 寄存器上算
        C_local[i] = relu(A_local[i] * B_local[i])
    T.copy(C_local, C[base])                          # register → global（批量）
```

> 切片语法小贴士：答案里写 `A[base_idx]`（单下标），实现指南里写 `A[base_idx : base_idx+BLOCK_N]`（显式切片），两者等价——对一维张量，`A[base_idx]` 表示「从 `base_idx` 起的一段」，长度由目标 fragment 的形状（`BLOCK_N`）推断。见 [docs/zh/2.vector-add/2.implementation-guide.md:59-70](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/zh/2.vector-add/2.implementation-guide.md#L59-L70)。

#### 4.2.3 源码精读

参考答案的完整 `tl_mul_relu_1d_mem`（本讲主任务的对照目标）：

```python
@tilelang.jit(
    pass_configs={
        tilelang.PassConfigKey.TL_DISABLE_WARP_SPECIALIZED: True,
        tilelang.PassConfigKey.TL_DISABLE_TMA_LOWER: True,
    },
)
def tl_mul_relu_1d_mem(A, B, BLOCK_N: int):
    N = T.const("N")
    dtype = T.float16
    A: T.Tensor((N,), dtype)
    B: T.Tensor((N,), dtype)
    C = T.empty((N,), dtype)

    with T.Kernel(N // BLOCK_N, threads=256) as pid_n:
        base_idx = pid_n * BLOCK_N
        A_local = T.alloc_fragment((BLOCK_N), dtype)
        B_local = T.alloc_fragment((BLOCK_N), dtype)
        C_local = T.alloc_fragment((BLOCK_N), dtype)

        T.copy(A[base_idx], A_local)
        T.copy(B[base_idx], B_local)

        for i in T.Parallel(BLOCK_N):
            C_local[i] = A_local[i] * B_local[i]
            C_local[i] = T.if_then_else(C_local[i] > 0, C_local[i], 0)

        T.copy(C_local, C[base_idx])

    return C
```

参考：[ans/02-vector-add.py:163-192](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L163-L192)。逐段对照 4.2.2 的五步：

- [第 179-181 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L179-L181) `T.alloc_fragment((BLOCK_N), dtype)` —— 分配三个寄存器片段。注意 `(BLOCK_N)` 是单元素元组的简写，等价于 `(BLOCK_N,)`。
- [第 183-184 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L183-L184) `T.copy(A[base_idx], A_local)` —— global → fragment，框架自动选用宽 load。
- [第 186-188 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L186-L188) `for i in T.Parallel(BLOCK_N)` —— 在 fragment 上做元素级 mul-relu。和上一讲朴素版的差别只有一点：这里的 `A_local[i]`、`C_local[i]` 是**寄存器**访问，不是显存访问。
- [第 190 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L190) `T.copy(C_local, C[base_idx])` —— fragment → global，批量写回。

与朴素版 [ans/02-vector-add.py:106-113](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L106-L113) 并排看：**计算逻辑一模一样**（都是 `relu(A*B)`），区别只在「数据放在哪里」——朴素版直接读写 global 指针，优化版把 tile 先搬进 fragment 再算。这正应了那句「内存层级是 TileLang 显式暴露给用户的」。

#### 4.2.4 代码实践（本讲主任务）

**实践目标**：参照答案，补全 `puzzles/02-vector-add.py` 里的 `tl_mul_relu_1d_mem`，用 `T.alloc_fragment` 缓存 A/B/C，用 `T.copy` 在 global 与 fragment 间搬运，并用 `test_puzzle` 验证与 torch 一致。

**操作步骤**：

1. 打开 `puzzles/02-vector-add.py`，定位到 [`tl_mul_relu_1d_mem`](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L158-L173) 的 `# TODO`（[第 171 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L171)）。
2. 按 4.2.3 的五步填入：`with T.Kernel(N // BLOCK_N, threads=256) as pid_n:` → `base_idx = pid_n * BLOCK_N` → 三个 `T.alloc_fragment` → 两条 `T.copy` 搬入 → `T.Parallel` 算 mul-relu → 一条 `T.copy` 搬出。建议直接用题目里 `tl_add_1d` 的块索引命名（`bx`）以保持文件风格一致，但答案用的是 `pid_n`，两者等价。
3. 用文件自带的 `run_mul_relu_1d_mem()` 验证（它内部会 `test_puzzle` + 两个 `bench_puzzle`，见 [puzzles/02-vector-add.py:176-203](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L176-L203)）：

   ```bash
   python3 -c "
   import importlib.util
   spec = importlib.util.spec_from_file_location('p02', 'puzzles/02-vector-add.py')
   m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
   m.run_mul_relu_1d_mem()
   "
   ```

**需要观察的现象**：

- `test_puzzle` 打印 `✅ Results match: True`（`run_mul_relu_1d_mem` 用 `N=1024*4096, BLOCK_N=1024`，见 [puzzles/02-vector-add.py:178-179](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L178-L179)）。
- `bench_puzzle` 会打印三行耗时：`Torch time`（torch 参考）、`TL Naive time`（朴素版）、`TL OPT time`（你的优化版）。

**预期结果**：正确性与 torch 的 `(A*B).relu_()` 在 `atol=rtol=1e-2` 下一致。性能方面，朴素版与优化版的相对快慢**待本地验证**——因为对这个简单的 1D mul-relu，NVCC 可能已经把朴素版优化得很好（题目注释 4.1.2 已说明）。如果你看到两者差不多、甚至优化版略快，都属正常；本练的核心是**写法**与**生成代码**，而不是「一定能跑赢朴素版」。

> 排错提示：若出现 `❌`，`test_puzzle` 会打印 `Yours` / `Spec` / `Diff` / `Max diff`（见 [common/utils.py:91-106](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L91-L106)）。常见错误：忘了 `T.copy` 搬入就直接读 `A_local`（读到未初始化寄存器）、把 `C_local` 写回时下标写成 `C[i]` 而非 `C[base_idx]` 切片、或 `alloc_fragment` 的形状写错。

#### 4.2.5 小练习与答案

**练习 1**：在原始 CUDA 里，为什么「把 1024 个元素搬进寄存器」需要程序员手写「线程↔元素」的映射，而 TileLang 不用？

> **参考答案**：因为 CUDA 的寄存器是**线程局部**的，每个线程只能直接访问自己的寄存器；要让一个 block 的 256 个线程合搬 1024 个元素，必须显式规定「第 `tid` 号线程搬哪几个下标」。TileLang 的 `T.alloc_fragment` 把「本 block 所有线程的寄存器」抽象成**一整块逻辑 buffer**，你只需声明形状与搬运（`T.copy`、`T.Parallel`），框架自动完成「逻辑下标 → 具体线程的寄存器」的映射。

**练习 2**：本讲的 `tl_mul_relu_1d_mem` 里，ReLU 用的是 `T.if_then_else(C_local[i] > 0, C_local[i], 0)`，而上一讲朴素版用的是 `T.if_then_else(A[..]*B[..] > 0, A[..]*B[..], 0)`。两者为什么能写出不一样的形式？这说明了 fragment 的什么好处？

> **参考答案**：朴素版里 `A*B` 没有显式的「存放处」，只能每次重新在表达式里写 `A[..]*B[..]`；而优化版先把乘积存进 `C_local[i]`（寄存器），之后的 ReLU 可以**直接读 `C_local[i]`** 而不必重算乘法。这说明 fragment 让「中间结果落点」变得**显式且可复用**——你不再依赖编译器的 CSE 来避免重复计算，而是在 DSL 层面就把数据流写清楚了。

### 4.3 pass_configs 与生成的 CUDA 代码

#### 4.3.1 概念说明

优化版骨架比朴素版多了一段 `@tilelang.jit(...)` 的参数：

```python
@tilelang.jit(
    pass_configs={
        tilelang.PassConfigKey.TL_DISABLE_WARP_SPECIALIZED: True,
        tilelang.PassConfigKey.TL_DISABLE_TMA_LOWER: True,
    },
)
```

参考：[puzzles/02-vector-add.py:158-163](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L158-L163)。`pass_configs` 是 TileLang 编译流水线（一系列 pass）的**开关字典**，键是 `tilelang.PassConfigKey` 枚举，值是 `True/False`。这里的两个键：

| 键 | 关掉的优化（直觉解释） | 对本讲的影响 |
| --- | --- | --- |
| `TL_DISABLE_WARP_SPECIALIZED` | **Warp specialization**（warp 专用化）：把一个 block 内的不同 warp 分成「生产者 / 消费者」等不同角色（常见于 Hopper 上的高性能 GEMM/Attention）。关掉它后，所有 warp 跑同一套逻辑，控制流更简单。 | 这个 1D mul-relu 根本不需要 warp 分工，关掉后生成的 CUDA 更接近「所有线程做同样的事」，便于阅读。 |
| `TL_DISABLE_TMA_LOWER` | **TMA**（Tensor Memory Accelerator，Hopper SM90 的硬件单元）相关的内存拷贝 lowering：用 TMA 描述符做异步批量搬运（主要面向 shared memory 的大 tile）。关掉它后，访存回退到更常规的 load/store。 | 本讲只用 fragment（寄存器）、没有 shared memory 大 tile，用不上 TMA；关掉后生成代码里不会出现 TMA 相关的复杂调用。 |

> 诚实的边界：上面两个键的「精确硬件语义」涉及 Hopper（SM90）特性，本讲只要求你掌握**它们在这里的作用：让生成代码更简单、更易读**。这是教学场景刻意做的简化——题目特意为本节加这两个开关，正是为了让 `print_source_code()` 输出的 CUDA 更接近手写直觉、便于和朴素版对照。键名是否在所有 TileLang 版本下都一致、值的默认行为，**待本地用 `tilelang.__version__` 核对**。

第二个工具是 `compile().print_source_code()`：编译后的 `JITKernel` 对象带一个 `print_source_code()` 方法，会把 TileLang **最终生成的 CUDA 源码**打印到终端。这是「看穿 DSL」的窗口——你写的是 Python 风格的 TileLang，但落地的是 CUDA，`print_source_code()` 让你能对照两者。

#### 4.3.2 核心流程

「编译 → 检视」的标准用法：

```
kernel = tl_func.compile(N=..., BLOCK_N=...)   # 绑定符号维度与超参，得到 JITKernel
kernel.print_source_code()                      # 打印生成的 CUDA 源码
```

题目文件 `run_mul_relu_1d_mem()` 就是这个套路，而且**朴素版和优化版各打一次**做对照：

```python
tl_mul_relu_kernel     = tl_mul_relu_1d.compile(N=N, BLOCK_N=BLOCK_N)
tl_mul_relu_kernel.print_source_code()                       # 朴素版 CUDA
tl_mul_relu_kernel_opt = tl_mul_relu_1d_mem.compile(N=N, BLOCK_N=BLOCK_N)
tl_mul_relu_kernel_opt.print_source_code()                   # 优化版 CUDA
```

参考：[puzzles/02-vector-add.py:181-187](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L181-L187)。配合 `bench_puzzle`（见 4.3.3）就构成「**正确性 + 生成代码 + 性能**」三位一体的诊断流程。

`bench_puzzle` 的计时方法学（[common/utils.py:109-156](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L109-L156)）值得记下，后续所有性能对比都用它：

- `warmups = 10`、`repeats = 100`（[第 118-119 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L118-L119)）：先空跑 10 次预热（避免首次编译 / 缓存效应），再计 100 次取平均。
- 用 `torch.cuda.Event(enable_timing=True)` 配 `torch.cuda.synchronize()` 计时（[第 146-154 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L146-L154)）：起止两个事件夹住 100 次循环，`elapsed_time` 除以 `repeats` 得单次耗时。`synchronize()` 保证 GPU 真正跑完再读时间。
- `bench_torch=True` 时会顺带测 torch 参考实现的耗时（[第 128-141 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L128-L141)）。

#### 4.3.3 源码精读

看 `run_mul_relu_1d_mem()` 完整流程（[puzzles/02-vector-add.py:176-203](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L176-L203)）：

```python
def run_mul_relu_1d_mem():
    print("\n=== Vector Multiplication with ReLU 1D (Memory Optimized) ===\n")
    N = 1024 * 4096
    BLOCK_N = 1024

    print("Naive TL Implementation: ")
    tl_mul_relu_kernel = tl_mul_relu_1d.compile(N=N, BLOCK_N=BLOCK_N)
    tl_mul_relu_kernel.print_source_code()

    print("Optimized Version")
    tl_mul_relu_kernel_opt = tl_mul_relu_1d_mem.compile(N=N, BLOCK_N=BLOCK_N)
    tl_mul_relu_kernel_opt.print_source_code()

    test_puzzle(tl_mul_relu_1d_mem, ref_mul_relu_1d, {"N": N, "BLOCK_N": BLOCK_N})
    bench_puzzle(tl_mul_relu_1d,     ref_mul_relu_1d, {"N": N, "BLOCK_N": BLOCK_N},
                 bench_name="TL Naive", bench_torch=True)
    bench_puzzle(tl_mul_relu_1d_mem, ref_mul_relu_1d, {"N": N, "BLOCK_N": BLOCK_N},
                 bench_name="TL OPT",   bench_torch=False)
```

注意几个细节：

- [第 182-187 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L182-L187) 两次 `.compile(...).print_source_code()`：`compile` 的入参 `N=..., BLOCK_N=...` 正是上一讲讲的「符号维度 `N` 与编译期超参 `BLOCK_N` 在此时绑定」。
- [第 189 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L189) 只对优化版做 `test_puzzle`（朴素版上一讲已验证）。
- [第 190-203 行](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/02-vector-add.py#L190-L203) 两个 `bench_puzzle`：第一个 `bench_torch=True` 会打 `Torch time` 和 `TL Naive time`；第二个 `bench_torch=False` 只打 `TL OPT time`。三行合起来就是「torch vs 朴素 vs 优化」的对照。

#### 4.3.4 代码实践

**实践目标**：用 `print_source_code()` 对照朴素版与优化版生成的 CUDA，亲手「看见」fragment 带来的访存差异。

**操作步骤**：

1. 先按 4.2.4 补全 `tl_mul_relu_1d_mem` 并确认 `test_puzzle` 通过。
2. 重新运行 `run_mul_relu_1d_mem()`（命令同 4.2.4），把终端里两段 CUDA 源码分别保存下来。
3. 在两段 CUDA 里**对比**这几处（建议用文本搜索）：
   - **load 部分**：朴素版是否对 A、B 各元素单独 load？优化版是否出现了更宽的 load（如装载多个 `__half`、或 `ldg.128`/`float4` 风格）？
   - **中间乘积**：朴素版里 `A*B` 是否被算了两遍（依赖 CSE）？优化版是否明显算一遍、存寄存器、ReLU 复用？
   - **store 部分**：写回 C 的指令形态。
4.（可选）把 `pass_configs` 临时整段删掉（示例代码，仅用于观察），重新 `compile().print_source_code()`，看生成的 CUDA 是否变得更复杂（可能多出 warp specialization / TMA 相关结构）。**验证完务必改回原样**，因为题目要求保留这两个开关。

**需要观察的现象**：

- 优化版 CUDA 里，A/B 的 load 和 C 的 store 倾向于「成批、向量化」；乘法结果落在局部变量（寄存器）里被 ReLU 复用。
- 删掉 `pass_configs` 后，生成代码可能变长、出现更复杂的结构（具体形态取决于硬件与 TileLang 版本）。

**预期结果**：你能用自己的话指出「优化版在访存上和朴素版的区别」即可。是否肉眼看到 `ldg.128` 字样、删 `pass_configs` 后具体多出什么，**待本地验证**——不同 GPU 架构（如 Ampere vs Hopper）与 TileLang 版本下生成代码差异较大。重要的是建立「写 DSL → 看 CUDA → 对照优化」这套工作流，它会在后续矩阵乘、卷积讲义里反复用到。

> 关于 `print_source_code()` 的前提：它需要 kernel 真正 `compile` 成功（也就要求 GPU 环境就绪，见 [u1-l1](u1-l1-project-overview.md) 里 `scripts/check_tilelang_env.py` 的自检）。无 GPU 环境时无法生成 CUDA，本实践需在装有 CUDA 的机器上完成。

#### 4.3.5 小练习与答案

**练习 1**：`bench_puzzle` 为什么要先 `warmups=10` 次预热，再 `repeats=100` 次计时？为什么不直接计 1 次？

> **参考答案**：第一次（甚至前几次）执行会触发 JIT 编译、缓存填充、GPU 频率提升等一次性开销，不能反映 kernel 稳态性能。先预热 10 次把这些一次性成本「摊掉」，再用 100 次取平均，能得到更稳定的单次耗时；直接计 1 次会把编译开销算进去，严重高估。此外 `torch.cuda.synchronize()` 保证起止事件之间 GPU 真的跑完（GPU 调用是异步的）。

**练习 2**：本讲的 `tl_mul_relu_1d_mem` 关掉了 warp specialization 和 TMA lowering。请粗略判断：这两个优化对「1D mul-relu」有没有用？为什么题目还是要显式关掉它们？

> **参考答案**：对这个简单的 1D 元素级 kernel 基本没用——warp specialization 主要服务于「生产者/消费者 warp 分工」的矩阵乘/Attention，TMA 主要服务于「shared memory 大 tile 的异步批量搬运」，两者都依赖较复杂的 tile 协作结构，本讲的 fragment-only 元素级 kernel 用不上。题目显式关掉它们，是为了让 `print_source_code()` 输出的 CUDA 更简单、更贴近手写直觉，便于教学对照；在真正需要它们的高性能 kernel（后续讲义）里才会打开。

## 5. 综合实践

把本讲三个模块串起来，完成一次完整的「**正确性 + 生成代码 + 性能**」诊断。

**任务**：在已经补全 `tl_mul_relu_1d_mem` 的基础上，做一次端到端的优化对比实验，并写一份简短的观察记录。

要求：

1. **正确性**（模块 4.2）：运行 `run_mul_relu_1d_mem()`，确认 `test_puzzle` 打印 `✅ Results match: True`。
2. **生成代码**（模块 4.3）：把朴素版与优化版的 `print_source_code()` 输出各存一份，在你的记录里用 2–3 句话写出两者在 **load / 中间乘积 / store** 上的差别。
3. **性能**（模块 4.3）：记录 `bench_puzzle` 打印的三行耗时——`Torch time`、`TL Naive time`、`TL OPT time`，算出优化版相对朴素版的加速比（或减速比）。
4. **内存层级**（模块 4.1）：在你的记录里画一张本 kernel 的数据流图，标注 `global memory` 与 `registers(fragment)` 两个层级，以及 `T.copy` 在两者之间的两次搬运方向。
5. **（选做）调参**：把 `BLOCK_N` 从 `1024` 改成 `512` 和 `4096`，重跑 `bench_puzzle`，观察 fragment 大小对耗时的影响（fragment 越大占寄存器越多，可能影响 occupancy）。记录哪个最快。

**验收标准**：

- `✅ Results match: True`。
- 生成代码记录里能指出至少一处朴素版与优化版的访存差异。
- 性能记录里有三行耗时与加速比；若优化版并未明显更快，请如实写明并尝试用 4.1.2 「NVCC 可能已自动优化朴素版」来解释。
- 数据流图标注清楚两级内存与两次 `T.copy` 方向。

> 这是一个「诊断型」综合实践：它不追求「让优化版跑赢朴素版」，而是训练你用 `test_puzzle` + `print_source_code` + `bench_puzzle` 三件套去**看清一个 kernel 到底在做什么**。这套工作流是后续所有 puzzle 性能调试的基础。

## 6. 本讲小结

- **GPU 三级内存**：global memory（显存，最大最慢，所有线程可见）> shared memory（block 内共享，中等）> registers（线程局部，最快最小）。性能工程的核心是让数据尽量待在快的存储里。
- **`T.alloc_fragment`**：TileLang 对「一个 block 内所有线程的寄存器」的统一抽象。你把它当成一整块 buffer 操作（`A_local[i]`、`T.copy`、`T.Parallel`），框架自动完成「逻辑下标 → 具体线程寄存器」的映射，免去手写 CUDA 的线程映射。
- **批量进出寄存器**：用 `T.copy(global_slice, fragment)` 批量搬入、在 fragment 上算、用 `T.copy(fragment, global_slice)` 批量搬出。相比每个元素都碰显存，能减少访存指令、把中间结果显式留在寄存器（如 `ldg128` 一次搬 128 位）。
- **朴素版与优化版计算同构**：两者都是 `relu(A*B)`，差别只在「数据放在 global 指针 vs fragment」。
- **`pass_configs`**：编译流水线的开关字典；本讲用 `TL_DISABLE_WARP_SPECIALIZED` / `TL_DISABLE_TMA_LOWER` 关掉两个本节用不上的高级 lower，让生成代码更易读。
- **`compile().print_source_code()` + `bench_puzzle`**：构成「正确性 + 生成代码 + 性能」三件套，是看清 kernel 行为的标准工作流。`bench_puzzle` 用 CUDA Event + warmup(10) + repeats(100) + synchronize 做公平计时。

## 7. 下一步学习建议

本讲建立了一条贯穿全册的主线——**TileLang 显式暴露内存层级**。接下来：

- [u2-l3 Puzzle 03 Outer Vector Add](u2-l3-puzzle03-outer-vec-add.md) 会把 `T.Parallel` 和 fragment 扩展到**二维**：`(BLOCK_N, BLOCK_M)` 分块、多维 `T.Parallel(i, j)`、二维 fragment 中间结果。建议带着「fragment 在二维下怎么分配、怎么用 `T.copy` 搬运」的问题进入。
- 往后到矩阵乘阶段（u4）会引入 **`T.alloc_shared`（共享内存）** 与 **`T.Pipelined`（软件流水线）**，那时你会同时用到 global → shared → fragment 的三级搬运。本讲的 fragment 与 `T.copy` 是那条链路的起点——可以先扫一眼环境自检脚本里的完整 GEMM：[scripts/check_tilelang_env.py:33-41](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L33-L41)，它一次性展示了 `alloc_shared` + `alloc_fragment` + `T.clear` + `T.copy` + `T.gemm` + `T.Pipelined` 的合奏，作为「未来地图」先睹为快。
- 性能工程爱好者可提前读 [common/utils.py:109-156](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L109-L156) 里 `bench_puzzle` 的计时实现，并思考：为什么单次计时不可靠、为什么要 `synchronize`。
