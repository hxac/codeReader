# 循环与控制流

## 1. 本讲目标

学完本讲，你应当能够：

- 区分 TileLang 的四种主要循环构造 `T.serial` / `T.unroll` / `T.Parallel` / `T.Pipelined`，并说出它们各自映射到的硬件执行模式。
- 理解 `T.Parallel` 的「elementwise 并行」语义，知道它和普通 `serial` 循环的本质差别。
- 用 `T.Pipelined` 写出带软件流水线的 kernel，理解 `num_stages` 如何控制 copy 与 compute 的重叠。
- 用 `if` / 三元表达式 / `while` / `break` / `continue` 处理边界与控制流，并理解 `LegalizeSafeMemoryAccess` pass 何时会替你插入越界守卫。

本讲承接 [u2-l2](u2-l2-kernel-launch-context.md) 的 `T.Kernel` 启动上下文：上一讲讲了「怎么启动一个线程块」，本讲讲「线程块内部的循环怎么写、怎么映射到硬件」。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**第一，TileLang 的循环不是 Python 的循环。** 你写在 `@T.prim_func` 里的 `for i in T.serial(N):` 看起来像 Python，但在编译期它会被重写成构造 TIR（TileLang 的中间表示）的程序。也就是说，循环的「种类」（serial/unroll/parallel/pipelined）是在告诉编译器「请把这个循环编译成什么样的机器码」，而不是运行时行为。这一点在 [u2-l1](u2-l1-prim-func-and-type-system.md) 已经建立：`@T.prim_func` 不执行你写的计算，而是生成构造 TIR 的程序。

**第二，不同的循环种类对应不同的硬件执行模式。** GPU 上有几种典型的执行方式：

| 循环种类 | 典型硬件映射 | 何时用 |
|---|---|---|
| `T.serial` | 一个线程块内、按序执行的 `for` 循环 | 需要循环间有依赖、或显式串行时 |
| `T.unroll` | 编译期展开成一串顺序指令 | 循环次数小、想消除分支与循环开销时 |
| `T.Parallel` | 多个线程并行执行循环体（elementwise） | 每次迭代互相独立、典型的逐元素运算 |
| `T.Pipelined` | 软件流水线，重叠 copy 与 compute | GEMM / Attention 等访存密集 kernel |

`T.Kernel` 决定的是「启动多少个线程块、每块多少线程」（上一讲），而本讲的循环决定的是「在某个线程块内部，这些线程如何协作完成一个 tile 的计算」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| `tilelang/language/loop.py` | 循环构造的 Python 前端入口，定义 `serial`/`unroll`/`Parallel`/`Pipelined` 等 |
| `tilelang/language/eager/builder.py` | eager 构建器，把带 `step` 的循环折算成正确的 TIR for 帧 |
| `docs/programming_guides/control_flow.md` | 控制流官方指南（条件、循环、break/continue、边界） |
| `docs/programming_guides/software_pipeline.md` | 软件流水线标注的官方指南 |
| `examples/gemm/example_gemm.py` | 本讲实践用的 GEMM 示例 |
| `src/transform/pipeline_planning.cc` | 把 `num_stages` 标注规划成 stage/order 的 C++ pass |
| `src/transform/inject_pipeline.cc` | 真正注入软件流水线（prologue / steady / epilogue）的 pass |
| `src/transform/legalize_safe_memory_access.cc` | 自动越界守卫 pass |

## 4. 核心概念与源码讲解

### 4.1 serial / unroll：串行与展开循环

#### 4.1.1 概念说明

`T.serial` 是最朴素的循环：迭代按顺序执行，下一次迭代依赖上一次迭代也没关系。它是 TileLang 里最「老实」的循环——你写什么样，它就下译成一个按序执行的 `for`。

`T.unroll` 则是请求编译器**把循环展开**。当循环次数（trip count）比较小、且循环体里有可累积的标量运算时，展开可以消除循环控制开销、暴露指令级并行，让寄存器分配和常量折叠更好发挥作用。典型场景是 GEMM 内部那个很短的 reduction 维循环：

```python
for k in T.unroll(K_TILE):
    acc += a[k] * b[k]
```

`serial` 和 `unroll` 都有「大写别名」`T.Serial` / `T.Unroll`。源码注释明确说明：大写形式只是为了**强调这是一个 tile 级别的循环**，行为与小写完全一致。

#### 4.1.2 核心流程

两者都支持三种调用形式：

```python
for i in T.serial(N):           # 0..N-1，步长 1
for i in T.serial(0, N):        # start=0, stop=N
for i in T.serial(0, N, 2):     # start=0, stop=N, step=2  → 0,2,4,...
```

当 `step` 缺省或为 1 时，直接复用 TVM 的标准 `tb_tir.serial` / `tb_tir.unroll` 帧；当 `step` 不是 1 时，前端不会把步长直接塞进 TIR（TIR 的 for 帧没有 step），而是先**折算出真实的迭代次数**，再用步长 1 的帧模拟。

设起点为 \(s\)、终点为 \(e\)、步长为 \(d > 0\)，则真实迭代次数为：

\[
\text{trips} = \left\lceil \frac{e - s}{d} \right\rceil
\]

循环变量在第 \(v\) 次迭代取值为 \(s + v \cdot d\)。这样就把「带步长的 for」转换成了「步长 1 的 for + 仿射下标」。

`unroll` 额外支持两个专家级参数：`explicit=True`（完全展开，而非给后端一个提示）和 `unroll_factor=`（指定展开因子）。源码里有一条约束：使用 `unroll_factor` 时必须**不能**同时 `explicit=True`，否则报错。

#### 4.1.3 源码精读

`serial` 的实现：先判断步长是否为 1，是则走 TVM 标准帧，否则用 `SerialForWithStep` 携带步长信息：

[tilelang/language/loop.py:194-232](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py#L194-L232) —— `serial` 函数：步长为 1 时调用 `tb_tir.serial`，否则构造 `SerialForWithStep`。这段代码定义了「步长如何被前端处理」。

`unroll` 的实现：默认会注入 `pragma_unroll_explicit` 标注，并在指定 `unroll_factor` 时做互斥检查：

[tilelang/language/loop.py:235-297](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py#L235-L297) —— `unroll` 函数：默认设置 `pragma_unroll_explicit`，并在 `unroll_factor` 与 `explicit` 冲突时抛 `ValueError`。

「步长折算」并不发生在 `loop.py`，而是发生在 eager 构建器里。`SerialForWithStep` / `UnrollForWithStep` 只是携带 `start/stop/step` 的数据类，真正用 `ceildiv` 折算迭代次数的是构建器的 `ctx_for`：

[tilelang/language/eager/builder.py:345-383](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/builder.py#L345-L383) —— `ctx_for`：对带步长的循环，校验 `step != 0`，用 `tirx.ceildiv` 算出 `real_stop`，再生成步长 1 的 for 帧，并让循环变量取 `start + v * step`。

文档侧的简明描述：

[docs/programming_guides/control_flow.md:49-72](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/control_flow.md#L49-L72) —— Serial 与 Unroll 的官方写法，展示了 `T.serial(N)`、`T.serial(0, N, 2)`、`T.unroll(K_TILE)` 三种常见形式。

#### 4.1.4 代码实践

**实践目标**：直观感受 `unroll` 对短循环的影响。

**操作步骤**：

1. 新建一个最小 kernel（示例代码，非项目原有文件）：

```python
import tilelang
import tilelang.language as T

@tilelang.jit
def dot_prod(A, B, K, BLOCK=4):
    M, N = T.const("M, N")
    A: T.Tensor((M, K), T.float16)
    B: T.Tensor((K, N), T.float16)
    C = T.empty((M, N), T.float16)
    with T.Kernel(T.ceildiv(M, 1), threads=128) as (bx):
        acc = T.alloc_fragment((1,), T.float32)
        T.clear(acc)
        for k in T.unroll(K):          # 试试改成 T.serial(K) 对比
            acc[0] += A[bx, k] * B[k, 0]
        C[bx, 0] = T.cast(acc[0], T.float16)
    return C
```

2. 分别用 `T.unroll(K)` 和 `T.serial(K)` 编译，调用 `kernel.get_kernel_source()` 看生成的设备代码差异。

**需要观察的现象**：`unroll` 版本的生成代码里 reduction 会被展开成 `K` 条顺序的乘加；`serial` 版本会保留一个真正的 `for` 循环。

**预期结果**：当 `K` 很小时，`unroll` 生成的代码更长但没有循环跳转；`K` 很大时展开会让代码体积爆炸，此时应改回 `serial`。具体延迟数值**待本地验证**（取决于设备和 `K` 的大小）。

#### 4.1.5 小练习与答案

**练习 1**：`T.Serial` 和 `T.serial` 有什么区别？

**参考答案**：完全没有行为区别。源码里 `Serial` 只是 `serial` 的别名，大写形式仅用于在代码里强调「这是一个 tile 级循环」，便于阅读。

**练习 2**：为什么 `unroll_factor=8` 时不能同时设 `explicit=True`？

**参考答案**：`explicit=True` 表示「完全展开」（展开全部迭代），而 `unroll_factor` 表示「只展开指定倍数」。两者语义互斥，源码在 [loop.py:288-292](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py#L288-L292) 处显式抛 `ValueError` 拒绝这种组合。

---

### 4.2 Parallel：elementwise 并行循环

#### 4.2.1 概念说明

`T.Parallel(ext0, ext1, ...)` 构造的是一个**嵌套的并行循环**，专门用来写逐元素（elementwise）运算。它的语义和 `serial` 截然不同：`serial` 的迭代是同一个线程顺序执行的，而 `Parallel` 的每一次迭代都由**不同的线程并行执行**。

最典型的例子就是逐元素加法：

```python
for i, j in T.Parallel(M, N):
    C[i, j] = A[i, j] + B[i, j]
```

这里 `M*N` 个输出元素的计算被分摊到线程块里的所有线程上，每个线程算其中一个（或几个）元素。循环变量 `i, j` 在同一个 `for` 头里一起解包——这对应一个二维的并行嵌套。

#### 4.2.2 核心流程

`Parallel` 在前端做的事情其实很轻：把可选项（`coalesced_width`、`loop_layout`、`prefer_async`、`annotations`）合并进一个标注字典，然后调用 C++ 侧的 `_ffi_api.Parallel`。真正「把循环变成多线程并行」的工作在后续的 lowering pass 里完成，前端只负责**打标注**。

几个关键的可选项：

- `coalesced_width`：控制内存合并（coalescing）宽度，影响向量化检查。
- `loop_layout`：接受一个 `T.Fragment`，标注整个并行嵌套的布局，只挂到**最外层**循环上，且要求 `InputDim == 嵌套层数`。
- `prefer_async=True`：即使在非流水循环里，也请求注入 cp.async 异步拷贝。

布局约束很重要：对于一个 k 层的 `Parallel` 嵌套，布局标注必须挂最外层、且 `InputDim == k`，否则编译报错。原理是「内层循环无法控制/标注它的外层循环，只有最外层能管理整个嵌套区域」。

#### 4.2.3 源码精读

[tilelang/language/loop.py:13-87](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py#L13-L87) —— `Parallel` 函数：把 `coalesced_width`/`loop_layout`/`prefer_async` 合并进 `merged_annotations`，最终调用 `_ffi_api.Parallel(extents, merged_annotations)`。注意它只构造标注，不真正分配线程。

文档对 elementwise 用法和布局约束的说明：

[docs/programming_guides/control_flow.md:74-88](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/control_flow.md#L74-L88) —— Parallel 的官方示例与可选 hint（`coalesced_width`、`loop_layout`）。

#### 4.2.4 代码实践

**实践目标**：用 `Parallel` 写一个 2D ReLU，并对照生成的源码理解「并行」。

**操作步骤**：

1. 编写如下 kernel（示例代码）：

```python
@tilelang.jit
def relu2d(A, M, N):
    A: T.Tensor((M, N), T.float32)
    C = T.empty((M, N), T.float32)
    with T.Kernel(T.ceildiv(N, 64), T.ceildiv(M, 64), threads=128) as (bx, by):
        for i, j in T.Parallel(64, 64):
            gi, gj = by * 64 + i, bx * 64 + j
            C[gi, gj] = T.max(A[gi, gj], 0.0)
    return C
```

2. 编译并打印 `get_kernel_source()`。

**需要观察的现象**：生成代码里不应该出现串行的双重 `for`，而是直接基于 `threadIdx` 计算 `i, j`，每个线程处理一个元素。

**预期结果**：每个线程算一个输出元素，128 个线程并行覆盖 64×64 的 tile（线程数大于元素数时部分线程空闲，这只是为了演示；实际更优配置会让 threads 与 tile 大小匹配）。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `loop_layout` 必须挂最外层、且 `InputDim` 要等于嵌套层数？

**参考答案**：内层循环无法控制它的外层循环，只有最外层循环能管理整个嵌套区域并把标注传给 lowering pass 去重写整片区域。`InputDim == k` 保证布局覆盖了全部 k 个并行维度，缺失或不匹配会在 `LayoutInference` pass 的 `ParallelLoopLayoutValidator` 里报错。

**练习 2**：把上一节 `serial` 的 reduction 循环（`acc += a[k]*b[k]`）改成 `Parallel` 会怎样？

**参考答案**：会出错或得到错误结果。`acc += ...` 是循环间有数据依赖的归约，必须串行（或用专门的归约原语）；`Parallel` 要求每次迭代互相独立，不能用于带累积依赖的 reduction。

---

### 4.3 Pipelined：软件流水线

#### 4.3.1 概念说明

`T.Pipelined` 是 GEMM / Attention 这类访存密集 kernel 的「脊梁」。它表达的是**软件流水线（software pipeline）**：把「搬数据」（Global→Shared 的 copy）和「算数据」（GEMM/MMA）这两件事在时间上重叠起来，从而隐藏访存延迟。

直觉是这样的：如果不做流水线，第 `k` 轮要先等 `A`、`B` 拷进 shared，然后才能算 gemm，算完才能开始第 `k+1` 轮的拷贝——copy 和 compute 是串行的，访存延迟完全暴露。软件流水线则让「第 `k` 轮的 compute」和「第 `k+1`（甚至 `k+2`）轮的 copy」同时进行，只要给 shared memory 多备几份缓冲（double/triple buffering），就可以一边算当前 tile、一边预取未来 tile。

`num_stages` 就是这个「缓冲份数」的核心旋钮。回看 [u1-l4](u1-l4-first-gemm-kernel.md) 和本讲的 GEMM 示例：

```python
for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
    T.copy(A[by * block_M, k * block_K], A_shared)   # copy A
    T.copy(B[k * block_K, bx * block_N], B_shared)   # copy B
    T.gemm(A_shared, B_shared, C_local)              # compute
```

`num_stages=3` 表示在「生产者（copy）」和「消费者（gemm）」之间最多保留 3 份缓冲。

#### 4.3.2 核心流程

TileLang 支持两种写流水线的方式：

**1. 编译器推断式（推荐）**：只给 `num_stages`，让编译器自动决定哪个语句是 copy、哪个是 compute、各自属于第几 stage。这是常规 GEMM-like kernel 的首选入口。注意：**`num_stages=0` 表示不启用流水线**。

**2. 手动标注式**：手动给出 `order` 和 `stage` 两个数组，精确控制每条「可调度语句」的发射顺序和流水阶段。此时**不要**再设 `num_stages`，流水深度由 `max(stage) + 1` 推断。

每条可调度语句有两个数：

- `stage`：逻辑流水阶段，数字越小越早执行。典型 copy/compute 流水里 copy 在 stage 0、gemm 在 stage 1。
- `order`：发射顺序，决定同阶段内或重排时的语句排列。

依赖规则是：若两个语句在同一 stage，生产者的 `order` 必须小于消费者；若跨 stage，生产者的 stage 必须 ≤ 消费者。

下译成机器码分三段：**prologue**（先把前几个 tile 的 copy 灌满流水线）、**steady state**（稳定状态下每轮 copy 与 compute 重叠）、**epilogue**（把最后几个 tile 的 compute 排干）。这个三段式由 C++ pass 完成。

一个重要规则：**可重放的标量 `Bind`（如 `base = k * BK`）不是可调度语句**，不需要在 `order`/`stage` 里占位，编译器会在每个使用点按需重放它。

#### 4.3.3 源码精读

Python 侧 `Pipelined` 的实现：处理 `stop` 缺省、把空列表默认值填好后，调用 C++ 侧 `_ffi_api.Pipelined`：

[tilelang/language/loop.py:112-191](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/loop.py#L112-L191) —— `Pipelined` 函数：注意 docstring 里写明 `num_stages=0` 时不启用流水、手动 `order`/`stage` 时深度按 `max(stage)+1` 推断。

GEMM 示例里的真实用法：

[examples/gemm/example_gemm.py:19-22](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L19-L22) —— `for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3)`，两条 `T.copy` 加一条 `T.gemm`，这是最经典的推断式流水线写法。

C++ 侧的下译分两个 pass。第一个是 `PipelinePlanning`，它从循环标注里读出 `num_stages`，规划出每条语句的 stage/order：

[src/transform/pipeline_planning.cc:1061-1126](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L1061-L1126) —— `VisitStmt_(ForNode)`：分别读取 `tl_pipeline_order`、`tl_pipeline_stage`、`num_stages` 三种标注；`num_stages` 缺失时直接 `return`（不流水），否则取出整数值进入规划逻辑。注意 ROCm 非 gfx950 目标会先剥掉 `num_stages` 再递归。

[src/transform/pipeline_planning.cc:1334-1352](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L1334-L1352) —— `PipelinePlanning()` pass 的注册，名为 `tl.PipelinePlanning`。

第二个是 `InjectSoftwarePipeline`，它把规划结果真正注入成 prologue/steady/epilogue 三段：

[src/transform/inject_pipeline.cc:4010-4033](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/inject_pipeline.cc#L4010-L4033) —— `InjectPipeline` 函数与 `tl.InjectSoftwarePipeline` pass 注册。

官方对手动 `stage`/`order` 标注的权威说明：

[docs/programming_guides/software_pipeline.md:44-86](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/software_pipeline.md#L44-L86) —— Stage 与 Order 的对齐规则与依赖约束，并强调「提供 stage/order 时不要再设 num_stages」。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：体会 `num_stages` 对 GEMM 行为与延迟的影响。

**操作步骤**：

1. 打开 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L19-L22)，找到第 19 行的 `num_stages=3`。
2. 分别把它改成 `num_stages=1` 和 `num_stages=4`，各运行一次 `python examples/gemm/example_gemm.py`。
3. 用 `kernel.get_profiler().do_bench(backend="cupti")` 记录三种取值的延迟。
4. 用 `kernel.get_kernel_source()` 对比 `num_stages=1`（等于不重叠）与 `num_stages=3` 生成的代码差异。

**需要观察的现象**：

- `num_stages=1` 时 copy 与 compute 不重叠，生成代码里 shared 缓冲只有 1 份，prologue/epilogue 退化。
- `num_stages=3` 时出现多份 shared 缓冲与 `cp.async` / `mbarrier` 同步，steady state 明显。
- `num_stages=4` 缓冲更多，寄存器/shared 压力更大，可能更快也可能因资源压力变慢。
- 三者的数值正确性都应当通过（`torch.testing.assert_close`）。

**预期结果**：延迟通常随 `num_stages` 增大先降后升，存在一个甜点值（对 1024×1024×1024、block 128×128×32 常在 2~4 之间）。**具体数值待本地验证**——请如实记录你的设备上 1/3/4 三个延迟。

#### 4.3.5 小练习与答案

**练习 1**：`num_stages=0` 和 `num_stages=1` 有什么区别？

**参考答案**：`num_stages=0` 表示**完全不启用**软件流水线，循环保持原样下译；`num_stages=1` 会经过流水规划但只有 1 份缓冲，copy 与 compute 实际无法重叠（相当于退化成串行），不过仍会走完整的 pass 流程。

**练习 2**：手动给出 `stage=[0, 0, 1]`、`order=[0, 1, 2]` 时，为什么不应再设 `num_stages`？

**参考答案**：手动标注时流水深度由 `max(stage) + 1` 推断（本例为 2）。如果同时设 `num_stages`，会与推断值冲突，产生歧义；源码与文档都建议二者只选其一（推断式用 `num_stages`，手动式用 `order`/`stage`）。

---

### 4.4 边界处理：条件分支与 OOB 守卫

#### 4.4.1 概念说明

真实 kernel 经常遇到「输出矩阵尺寸不是 tile 尺寸整数倍」的情况：比如 M=1000、tile=128，最后那块只有 1000−7×128=104 行是有效的，多出来的 24 行就越界了。处理这类边界靠两样东西：**条件分支**和**自动越界守卫**。

TileLang 支持标准的 Python 控制流：

- `if` / `elif` / `else`：条件应是 TIR 表达式（如 `i < N`）；纯 Python 布尔会被当作编译期常量折叠。
- 三元表达式：`x if cond else y`。
- `while`：条件为 TIR 表达式；编译器检测到「恒为真」的条件会报错（防止死循环）。
- `break` / `continue`：可在 `serial`/`unroll`/`Parallel`/`while` 循环中使用。
- 多维边界用 `T.all_of(...)` / `T.any_of(...)` 表达更清晰。

除了手写 `if`，TileLang 还有一个重要 pass：`LegalizeSafeMemoryAccess`。它会在某次访存**可能越界**时自动插入守卫（guard），并在能证明安全时把守卫消去。这意味着很多简单边界场景你**可以省掉手写 `if`**——但当你需要自定义边界逻辑或想让代码更清晰时，手写 `if` 仍然是好习惯。

#### 4.4.2 核心流程

条件分支的下译依赖 eager 构建器。`Builder.ctx_if` 会先 `unwrap_cond` 把条件转成 TIR 表达式或 Python bool：

- 若是 `PrimExpr`（运行时条件）→ 生成真正的 `tirx.If(cond)` 帧，下译成设备码里的条件分支。
- 若是编译期 bool → 直接在 Python 层折叠，只生成then 分支或 else 分支。

`while` 的处理类似：`ctx_while` 把条件转成 TIR；若条件被判定为「编译期恒真」会抛 `RuntimeError`，若「恒假」会警告并跳过循环体。

`LegalizeSafeMemoryAccess` 的边界检查策略对**不同内存作用域**区别对待：

- **global buffer**：检查每个访存下标，可能越界时插入边界判断。
- **shared / local buffer**：仍然可能越界，但假定开发者会自行处理，只在开启告警时给出 warning。

这一区分的依据是「我们在写 TileProgram，shared/local 的越界由开发者负责」。

#### 4.4.3 源码精读

控制流总览与条件分支写法：

[docs/programming_guides/control_flow.md:16-45](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/control_flow.md#L16-L45) —— Conditionals 官方说明，含 `if`/`else`、三元、`T.all_of`，以及「`LegalizeSafeMemoryAccess` 会自动插入/消去守卫」的边界处理备注。

`while` 与 `break`/`continue` 的说明：

[docs/programming_guides/control_flow.md:112-130](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/control_flow.md#L112-L130) —— While 与 Break/Continue：`while` 条件须为 TIR 表达式，编译器会拒绝恒真条件；`break`/`continue` 可在各循环中使用。

eager 构建器对 `while` 的恒真检测：

[tilelang/language/eager/builder.py:397-414](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/builder.py#L397-L414) —— `ctx_while`：条件折叠为编译期真值时抛 `RuntimeError("Infinite while loop detected ...")`，折叠为假值时仅警告并跳过。

`LegalizeSafeMemoryAccess` pass 中按作用域区别处理越界的逻辑：

[src/transform/legalize_safe_memory_access.cc:48-70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/legalize_safe_memory_access.cc#L48-L70) —— `SafeMemChecker::VisitExpr_(BufferLoadNode)`：global buffer 检查下标并加边界判断；shared/local 仅在开启告警时记录 warning，注释明确「我们在写 TileProgram，假定开发者能处理 shared/local 的越界」。

一个典型的边界处理模式（带可选守卫）：

[docs/programming_guides/control_flow.md:132-143](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/control_flow.md#L132-L143) —— Residual Tile Handling：用 `T.Parallel` + `T.all_of(gi < M, gj < N)` 处理 2D 残块，并说明很多场景下显式 `if` 可省略。

#### 4.4.4 代码实践

**实践目标**：写一个带「非整数倍边界」的 elementwise kernel，体验何时需要手写 `if`、何时可以省略。

**操作步骤**：

1. 编写如下 kernel（示例代码），让输出尺寸不整除 tile（如 M=1000, tile=128）：

```python
@tilelang.jit
def add_boundary(A, B, M, N, BM=128, BN=128):
    A: T.Tensor((M, N), T.float32)
    B: T.Tensor((M, N), T.float32)
    C = T.empty((M, N), T.float32)
    with T.Kernel(T.ceildiv(N, BN), T.ceildiv(M, BM), threads=128) as (bx, by):
        for i, j in T.Parallel(BM, BN):
            gi, gj = by * BM + i, bx * BN + j
            if T.all_of(gi < M, gj < N):     # 可注释掉这一行，看 LegalizeSafeMemoryAccess 是否兜底
                C[gi, gj] = A[gi, gj] + B[gi, gj]
    return C
```

2. 先**保留** `if T.all_of(...)`，用 `M=1000, N=1000` 编译运行，验证结果正确。
3. 再**注释掉** `if` 那行，重新编译运行，观察是否仍正确（依赖 pass 自动加守卫）。

**需要观察的现象**：保留 `if` 时，结果一定正确；注释掉 `if` 后，由于 `LegalizeSafeMemoryAccess` 会为 global 访问自动加守卫，通常仍正确，但生成代码里会出现 pass 插入的边界判断。

**预期结果**：两种写法数值上都应通过；区别在生成代码与是否依赖自动守卫。**是否在所有 target 上都自动兜底待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `while True:` 在 TileLang 里会直接报错？

**参考答案**：`ctx_while` 会把条件 `unwrap` 并尝试折叠，发现是编译期恒真时就抛 `RuntimeError("Infinite while loop detected")`，避免生成无法终止的设备码。见 [builder.py:401-406](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/eager/builder.py#L401-L406)。

**练习 2**：对 shared memory 的潜在越界，`LegalizeSafeMemoryAccess` 会怎么处理？

**参考答案**：和 global 不同，shared/local 的越界只在开启告警时给出 warning，不会自动插入守卫——pass 注释明确「我们在写 TileProgram，假定开发者能处理 shared/local 的越界」。所以 shared 的边界需要你自己用 `if` 保证。

---

## 5. 综合实践

把本讲四种循环串起来，改造 GEMM 示例 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py)：

1. **主任务（Pipelined）**：固定 `M=N=K=1024`、`block_M=block_N=128`、`block_K=32`，把第 19 行 `num_stages` 依次设为 `1, 2, 3, 4, 5`，用 `get_profiler().do_bench()` 记录延迟，画出延迟—`num_stages` 曲线，找出你设备上的甜点值，并解释为什么再增大反而变慢（提示：shared/寄存器压力、缓冲份数）。
2. **扩展任务（unroll + 边界）**：在 GEMM 的 `T.gemm` 之外，观察内部 K 维 reduction 用了哪种循环（参考 [u4-l2](u4-l2-tileop-and-gemm-dispatch.md) 的指令分派会深入这一点）；再把 `M=1000`（非 128 整数倍）跑一次，确认残块结果仍正确，并说明这是手写 `if` 还是 `LegalizeSafeMemoryAccess` 兜的底。
3. **对比任务（Parallel vs Pipelined）**：写一个纯 elementwise 的 `A+B` kernel 用 `T.Parallel`，与 GEMM 的 `T.Pipelined` 对比生成代码，用自己的话总结「Parallel 适合独立元素、Pipelined 适合 copy/compute 重叠」。

把上述三步的延迟表与生成代码片段整理成一份小报告。

## 6. 本讲小结

- `T.serial` / `T.unroll` 分别下译成串行循环和展开循环；带步长的循环在前端用 `ceildiv` 折算成「步长 1 + 仿射下标」，`unroll` 的 `explicit` 与 `unroll_factor` 互斥。
- `T.Parallel` 表达 elementwise 并行，每次迭代由不同线程执行；它在前端只打标注（`coalesced_width`/`loop_layout`/`prefer_async`），真正的并行化在后续 lowering pass 完成，`loop_layout` 必须挂最外层且 `InputDim` 等于嵌套层数。
- `T.Pipelined` 表达软件流水线，靠 `num_stages` 控制 copy/compute 重叠的缓冲份数（`0` 表示不流水）；也可用 `order`/`stage` 手动标注，深度按 `max(stage)+1` 推断。下译由 `tl.PipelinePlanning` 规划、`tl.InjectSoftwarePipeline` 注入成 prologue/steady/epilogue 三段。
- 条件分支支持 `if`/三元/`while`/`break`/`continue`，条件须为 TIR 表达式，恒真 `while` 会被拒绝。
- `LegalizeSafeMemoryAccess` pass 对 global 越界自动加守卫（可证明安全时消去），对 shared/local 仅告警——所以很多简单边界可省 `if`，但 shared 边界要自己管。
- 选循环的直觉：独立元素用 `Parallel`、短 reduction 用 `unroll`、有依赖的串行用 `serial`、访存密集的 copy+compute 用 `Pipelined`。

## 7. 下一步学习建议

- 下一讲 [u2-l4](u2-l4-memory-hierarchy.md) 会讲内存层级与显存分配（`alloc_shared`/`alloc_fragment`/`T.copy`），与本讲的 `Pipelined` 紧密相关——`num_stages` 的「缓冲份数」正是分配在 shared memory 里的。
- 想深入 `Pipelined` 的下译细节，可直接读 `src/transform/pipeline_planning.cc` 与 `src/transform/inject_pipeline.cc`，这部分会在 [u4-l4](u4-l4-software-pipeline.md) 系统讲解。
- 想理解 `Parallel` 的布局约束如何被校验和推断，可先读 [u4-l3](u4-l3-layout-inference.md) 的 layout 推断。
- 调试循环行为时，善用 `kernel.get_kernel_source()` 看生成代码，配合 [u9-l3](u9-l3-debug-tools.md) 的 `T.print` 与日志工具定位问题。
