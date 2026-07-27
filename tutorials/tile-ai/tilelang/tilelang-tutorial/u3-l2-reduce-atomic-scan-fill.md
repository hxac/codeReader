# 归约、原子、scan 与填充

> 本讲对应单元 u3《核心计算与调度原语》，承接 u3-l1《T.gemm 与 tile op 体系》。
> 在 u3-l1 中我们看到「一行 `T.gemm` 会被编译器映射成张量核指令」。本讲把同样的思想推广到**非矩阵乘的计算原语**：归约（reduce）、原子（atomic）、扫描（scan）与填充（fill/clear）。它们同样是「先留语义占位、后按硬件展开」的 tile op。

## 1. 本讲目标

学完本讲后，你应当能够：

- 用 `T.reduce_max / T.reduce_sum` 在一个 tile 内做分块归约，并理解它如何 lowering 成 warp / block 级 `AllReduce`。
- 掌握 `T.atomic_add / T.atomic_max` 的两条代码路径（标量地址 vs. tile 区间），知道何时需要 `atomic_add`、何时不需要。
- 理解 `T.cumsum / T.cummax` 这类**前缀扫描（inclusive scan）**为什么必须经过 shared memory，以及 `reverse` 的含义。
- 会用 `T.fill / T.clear` 初始化片上缓冲，并理解「累加型操作前必须清零」这条铁律。
- 把上述原语串起来，亲手实现一个 block 内的**在线 softmax（online softmax）**，并用 `T.atomic_add` 把分块结果写回全局。

## 2. 前置知识

本讲假设你已经学完 u2（DSL 语言基础）与 u3-l1（T.gemm 与 tile op 体系）。需要记住的几件事：

- **tile op 的两段式展开**：DSL 层的 `T.gemm` / `T.reduce` 只生成一个 `tl.tileop.xxx` 的 `call_intrin` 占位节点，真正的硬件指令在 `lower_tile_op` 这个 Pass 里才展开（详见 u6-l2、u6-l3）。本讲的 `reduce / fill / cumsum / atomicadd` 都遵循同一套机制。
- **三种内存 scope**（u2-l2）：`global`（显存）、`shared` / `shared.dyn`（共享内存）、`local.fragment`（寄存器 fragment）、`local`（线程私有寄存器）。归约和扫描对 scope 极其敏感，因为它们需要在**多个线程之间交换数据**。
- **warp 与 block**：GPU 上 32 个线程组成一个 warp（同一 warp 内可用 shuffle 同步），一个 block 包含多个 warp（block 内用 `__syncthreads` 或 named barrier 同步）。归约的「跨线程」本质上就是在这两级同步。

如果你对下面两个直觉还陌生，先记住它们，本讲会反复用到：

1. **归约是把很多数变成一个数**（求和、求最大值）。GPU 上要把它拆成「每个线程先算自己的一份，再 warp 内合并，再跨 warp 合并」。
2. **原子操作是「多个线程同时写同一个地址」时的安全网**。它保证「读-改-写」这三步不会被别的线程打断，代价是可能排队串行化。

## 3. 本讲源码地图

本讲涉及的关键文件如下（Python 侧 DSL 表面 + C++ 侧 lowering + 示例与测试）：

| 文件 | 作用 |
| --- | --- |
| `tilelang/language/fill_op.py` | `T.fill` / `T.clear`：用常量填充缓冲，归约前置准备 |
| `tilelang/language/reduce_op.py` | `T.reduce_*`、`T.finalize_reducer`、`T.warp_reduce_*`：分块归约 DSL |
| `tilelang/language/atomic.py` | `T.atomic_add/max/min`、`atomic_addx2/x4`、`atomic_load/store`：原子操作 DSL |
| `tilelang/language/scan_op.py` | `T.cumsum` / `T.cummax`：前缀扫描 DSL |
| `tilelang/language/customize.py` | 把 `atomic.py` 里的原子函数再导出到语言表面 |
| `tilelang/language/common.py` | 汇总上述模块，决定哪些名字挂在 `T.*` 上 |
| `tilelang/utils/language.py` | `to_tile_region` / `is_shared` / `is_fragment` 等把缓冲转成 tile 区间的工具 |
| `src/op/reduce.cc` / `src/op/scan.cc` / `src/op/fill.cc` | C++ 侧 tile op 的注册与 `Lower` 入口 |
| `src/cuda/op/reduce.cc` | CUDA 后端把 reduce 展开成 `tl::AllReduce<...>::run` |
| `examples/online_softmax/online_softmax.py` | 真实的在线 softmax 示例，是本讲综合实践的蓝本 |
| `testing/python/language/test_tilelang_language_{reduce,atomic,scan}.py` | 各原语的正确性测试，实践任务依据 |

> **导出路径提示**：`T.reduce_*`、`T.cumsum`、`T.fill`/`T.clear` 由 [`tilelang/language/common.py`](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py#L67-L85) 直接导入；而 `T.atomic_*` 先由 [`customize.py`](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/customize.py#L9) 从 `atomic.py` 引入，再被 `common.py` 导出。所以 `T.atomic_add` 的本体在 `atomic.py`，但「挂到 T 上」多绕了一层 `customize`。

## 4. 核心概念与源码讲解

本讲按「先讲填充（归约的前置）、再讲归约、再讲原子、最后讲扫描」的顺序。每个模块都遵循 u3-l1 建立的 tile op 模型：**DSL 留占位 → C++ 按目标硬件展开**。

---

### 4.1 填充与清零（fill_op）

#### 4.1.1 概念说明

很多计算原语是**累加型**的：`T.gemm` 默认 `clear_accum=False` 会把结果累加到输出缓冲（u3-l1）；`T.reduce_sum` 在 `clear=False` 时也会累加；`T.cumsum` 必然把前缀累加进去。如果输出缓冲里残留着上一轮的垃圾数据，结果就全错了。

所以铁律是：**累加型操作之前，必须先把输出缓冲清成正确的初值**。求和初值是 0，求最大值初值是 \(-\infty\)，求最小值初值是 \(+\infty\)。

`T.fill(buf, value)` 把 `buf` 的每个元素写成 `value`；`T.clear(buf)` 是 `T.fill(buf, 0)` 的语法糖。它们和 `T.copy` 一样，是 tile 级的并行操作——不是用一个线程串行地写满整个缓冲，而是由 block 里所有线程并行填满。

#### 4.1.2 核心流程

`fill` 的执行流程非常直白：

1. 判断 `buffer` 是 `Buffer` / `BufferRegion` / `BufferLoad` 哪一种，据此推断要填充的 `extents`（每个维度填多少个元素）。
2. 用 `to_buffer_region(buffer, access_type="w", extents)` 把它编码成一个「可写 tile 区间」。
3. 发射 `tl.tileop.fill` 这个 tile op 占位节点，参数是 `(写区间, value)`。
4. 后续 `lower_tile_op` Pass 里，C++ 的 `Fill` 算子把它展开成「每个线程写自己负责的那一片」的循环。

`clear` 只是 `fill(buf, 0)` 的一层包装，但对 `Var`（带 let 绑定的变量）会先解引用出真正的 `BufferRegion` 再填充。

#### 4.1.3 源码精读

`fill` 的实现核心是构造 `tl.tileop.fill` 占位节点，关键是「把不同形态的 buffer 都归一化成带 extents 的写区间」：

[fill_op.py:10-37](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/fill_op.py#L10-L37) —— `fill`：先按 `Buffer`/`BufferRegion`/`BufferLoad` 三种情况算出 `extents`，最后 `call_intrin("handle", tl.tileop.fill, to_buffer_region(..., "w", extents), value)`。

`clear` 的实现：

[fill_op.py:40-63](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/fill_op.py#L40-L63) —— `clear`：对普通 buffer 直接 `fill(buffer, 0)`；对带 let 绑定的 `Var` 先解出 `BufferRegion`/`BufferLoad` 再清零。

C++ 侧的注册：`Fill` 算子注册为 `tl.tileop.fill`，其 `Lower` 同样委托给「按 target 解析的具体实现」，布局推理返回空（不做布局变换）：

[fill.cc:214-217](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/fill.cc#L214-L217) —— `TIR_REGISTER_TL_TILE_OP(Fill, fill)` 把 C++ `Fill` 算子挂到 `tl.tileop.fill` 这个名字上（这就是 Python `_KEY = "tl.tileop.fill"` 能找到它的原因）。

> **命名约定**：本仓库所有 tile op 都用宏 `TIR_REGISTER_TL_TILE_OP(Cpp类, op名)` 注册，产生的 op 全名是 `tl.tileop.<op名>`。所以 Python 侧 `_REDUCE_OP_KEY="tl.tileop.reduce"`、`_CUMSUM_OP_KEY="tl.tileop.cumsum"` 都能与 C++ 一一对应。

#### 4.1.4 代码实践

**实践目标**：体会「累加前不清零」会导致错误。

1. 阅读 [`examples/online_softmax/online_softmax.py:23`](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/online_softmax/online_softmax.py#L23)：`T.fill(lse, -T.infinity(accum_dtype))` 把 `lse`（log-sum-exp）初始化为 \(-\infty\)。
2. 把这一行注释掉，重新运行示例。
3. **需要观察的现象**：`torch.testing.assert_close` 会失败，因为 `lse` 残留了未初始化的垃圾值，后续在线合并公式算出的 max/sum 全部失真。
4. **预期结果**：恢复 `T.fill` 后断言通过。本步在 GPU 机器上运行，无 GPU 时记为「待本地验证」，但你可以静态推理：去掉初值后 `lse[i]` 的首读是未定义行为。

#### 4.1.5 小练习与答案

**练习 1**：`T.clear(buf)` 等价于 `T.fill(buf, ?)` 里的什么值？
**答**：等价于 `T.fill(buf, 0)`，见 `fill_op.py:63`。

**练习 2**：求一列数的最大值之前，输出缓冲应该 `T.fill` 成什么？为什么不是 0？
**答**：应填 \(-\infty\)（`-T.infinity(dtype)`）。若填 0，当所有输入都是负数时，归约结果会错误地变成 0 而不是真正的最大负数。

---

### 4.2 分块归约 T.reduce_*（reduce_op）

#### 4.2.1 概念说明

归约（reduction）是把一个 tile 沿某个维度「压缩」：`reduce_sum` 求和、`reduce_max` 求最大值、`reduce_min` 求最小值、`reduce_abssum`/`reduce_absmax` 取绝对值后再归约、`reduce_bitand/bitor/bitxor` 做按位归约。

在 GPU 上，「沿一个维度归约」意味着**原本分散在多个线程上的数据要汇总到少数线程**。例如一个 `[BLOCK_M, BLOCK_N]` 的 fragment，每个线程持有其中若干元素，沿 `dim=1` 归约成 `[BLOCK_M]`，就需要把同一行里不同线程持有的元素加起来。这分两步：

- **线程内归约**：每个线程先把自己持有的多个元素累加成一个标量。
- **跨线程归约**：再用 warp shuffle + shared memory / named barrier，把不同线程的标量合并成最终结果（tilelang 把这一步叫 `AllReduce`）。

`T.reduce_*` 就是把这两步打包成一行 DSL，并把「在线累加」「分批 AllReduce」「NaN 传播」等细节藏到编译器里。

#### 4.2.2 核心流程

`reduce(buffer, out, reduce_type, dim, clear, batch, nan_propagate)` 的处理分两种情况：

**情况 A：纯寄存器到寄存器（`local` → `local`）**——直接发射 `tl.tileop.reduce` 占位，不做任何搬运。

**情况 B：涉及 shared / fragment 的组合**——因为 C++ 的 `ReduceOp` **只支持 `local.fragment` scope**（见下方源码注释「Reduce for shared memory not implemented」），所以 Python 侧用一个 `@macro` 把 shared 先 `T.copy` 到临时 fragment，在 fragment 上归约，再 `T.copy` 回 shared。一共有 4 种 scope 组合（shared↔shared、shared→fragment、fragment→shared、fragment↔fragment），每种都按「需要时搬一份临时 fragment」处理。

归约算子本身（`ReduceOpNode::Lower`）做的事，其源码注释总结得很清楚：

> optional initialization → thread-local reduction（unrolled 内层循环）→ inter-thread reduction via backend AllReduce → optional accumulation / copy back

跨线程这一步，CUDA 后端会发射形如 `tl::AllReduce<reducer, reducing_threads, scale, offset, Barrier>::run` 的外部调用；SM90+ 用 `NamedBarrier`，更老的架构用 `SyncThreadsBarrier`；当参与归约的线程数 > 32（超出一个 warp）时，还会分配一块 workspace 协助跨 warp 合并。

**`clear` 参数的语义**很关键：

- `reduce_max/min` 的 `clear=True` 会先把输出初始化为单位元（max → \(-\infty\)，min → \(+\infty\)）。
- `reduce_sum` 的 `clear=True` **不直接在 out 上归约**，因为 warp 归约会让同一个值被累加多次（等于 warp 内线程数）。实现是：建临时 buffer → 拷贝 out → 归约 → 把临时 buffer 加回 out（见 `reduce_sum` 文档注释）。

**`batch` 参数**：默认 `batch=1`（标量路径，每次 AllReduce 出一个数）。设 `batch=N` 时，编译器发射 `run_batch`，一次 AllReduce 处理 N 个输出元素、共用一对 barrier，barrier 数量减少 N 倍——对 softmax 这种「每行要归约出一串数」的场景很有用。

**`nan_propagate`**：仅对 fp16/bf16 的 max/min/absmax 有意义。默认 `False` 用 `__hmax`（遇到 NaN 返回另一个操作数，即吞掉 NaN）；`True` 用 `__hmax_nan`（让 NaN 传播出去）。

#### 4.2.3 源码精读

先看 Python 侧 `reduce` 的总入口与「纯寄存器快速路径」：

[reduce_op.py:18-20](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/reduce_op.py#L18-L20) —— 归约 op 的键 `tl.tileop.reduce`，以及支持的 `ReduceKind` 枚举。

[reduce_op.py:50-65](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/reduce_op.py#L50-L65) —— 形状校验（输入 `[X, d, Y]`，输出必须是 `[X, Y]` 或 `[X, 1, Y]`）与 `batch`/`nan_propagate` 注解的构造。

[reduce_op.py:69-81](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/reduce_op.py#L69-L81) —— **快速路径**：当输入是 `local`、输出是 `local`/`local.var` 时，直接发射 `tl.tileop.reduce`，跳过 macro 展开（这样 `alloc_var` 仍保留为 buffer 而非退化成标量表达式）。

接着是处理 shared/fragment 组合的 `@macro`，以「shared→shared」分支为例：

[reduce_op.py:85-107](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/reduce_op.py#L85-L107) —— shared→shared：分配两个临时 fragment `red_frag_in/out`，把 `out` 拷进 `red_frag_out`（仅当 `clear=False`），把 `buffer` 拷进 `red_frag_in`，在 fragment 上归约，再把 `red_frag_out` 拷回 `out`。这正是「C++ 只支持 fragment，所以 shared 先搬到 fragment」的体现。

`reduce_sum` 关于 `clear` 的特别说明（解释了为什么不直接在 out 上归约）：

[reduce_op.py:209-232](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/reduce_op.py#L209-L232) —— `reduce_sum` 文档：`clear=True` 时走「临时 buffer + 拷贝 + 归约 + 加回」四步，避免 warp 归约重复累加。

两个进阶 API：

[reduce_op.py:319-347](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/reduce_op.py#L319-L347) —— `finalize_reducer`：配合 `T.alloc_reducer` 使用。当你用 `reducer[...] += ...` 手写部分归约后，调用 `finalize_reducer` 触发 `tl.tileop.finalize_reducer`，才会在线程间真正完成 AllReduce、让部分结果可见。

[reduce_op.py:350-363](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/reduce_op.py#L350-L363) —— `warp_reduce_sum`：在 warp 内用 shuffle 把一个寄存器标量归约，结果广播到 warp 内所有线程。还有同族的 `warp_reduce_max/min/bitand/bitor`。

再看 C++ 侧。`ReduceOpNode::Lower` 只是把活儿交给「按 target 解析出来的具体实现」：

[reduce.cc:188-192](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/reduce.cc#L188-L192) —— `ReduceOpNode::Lower` → `ResolveReduceImpl(target).lower(...)`。这套「注册 + 按 target 解析唯一实现」的套路和 u3-l1 的 `resolve_gemm_impl` 完全一致。

[reduce.cc:154-187](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/reduce.cc#L154-L187) —— `Lower` 的详细行为注释：仅支持 `local.fragment`（shared 会 abort），做「可选初始化 → 线程内 unrolled 归约 → 跨线程 AllReduce → 可选累加/拷回」，线程数 > 32 时分配 workspace。

[reduce.cc:234-237](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/reduce.cc#L234-L237) —— `TIR_REGISTER_TL_TILE_OP(ReduceOp, reduce)`：把 `ReduceOp` 注册成 `tl.tileop.reduce`。

CUDA 后端如何把归约变成 `AllReduce`：

[src/cuda/op/reduce.cc:49-61](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/reduce.cc#L49-L61) —— `MakeScalarAllReduce`：拼出 `tl::AllReduce<reducer, reducing_threads, scale, offset[, Barrier]>::run` 这个外部调用名；SM90+ 额外带上 `NamedBarrier`，否则用默认的 `SyncThreadsBarrier`。

[src/cuda/op/reduce.cc:72-81](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/reduce.cc#L72-L81) —— `RegisterCudaReduce`：把 CUDA 实现（`cuda::Reduce::Lower`）注册进 `ReduceImplRegistry`，匹配 `TargetIsCuda || TargetIsCuTeDSL`。`run_batch`（对应 `batch>1`）的拼接逻辑在同文件 `MakeBatchAllReduce`（[L32-47](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/reduce.cc#L32-L47)）。

#### 4.2.4 代码实践

**实践目标**：用 `T.reduce_sum` 在 fragment 上做分块求和，并与 torch 对照。

依据真实测试 [`testing/python/language/test_tilelang_language_reduce.py`](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language/test_tilelang_language_reduce.py)，其参数表里就有 fragment→fragment 的 sum 用例（[L68](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language/test_tilelang_language_reduce.py#L68)）。下面是最小可运行示例（**示例代码**，仿照该测试）：

```python
# 示例代码：fragment 上的 reduce_sum
import tilelang, tilelang.language as T, torch

@tilelang.jit
def rowsum(A, M=128, N=128):
    A: T.Tensor([M, N], T.float32)
    B = T.empty([M], T.float32)
    with T.Kernel(T.ceildiv(M, 128), threads=128) as (bm,):
        a = T.alloc_fragment((128, 128), T.float32)
        b = T.alloc_fragment((128,), T.float32)
        T.copy(A[bm * 128], a)
        T.reduce_sum(a, b, dim=1, clear=True)   # 沿 N 维归约
        T.copy(b, B[bm * 128])
    return B

A = torch.randn(128, 128, device="cuda")
B = rowsum(A)
torch.testing.assert_close(B, A.sum(dim=1), atol=1e-3, rtol=1e-3)
```

**操作步骤**：1) 在 GPU 机器上保存为 `rowsum.py` 运行；2) 把 `dim=1` 改成 `dim=0`，再与 `A.sum(dim=0)` 对照。

**需要观察的现象**：`dim` 决定沿哪一维压缩；输出形状由 [reduce_op.py:50-57](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/reduce_op.py#L50-L57) 的校验决定——`[M,N]` 沿 `dim=1` 归约应得 `[M]`。

**预期结果**：断言通过。无 GPU 时记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `reduce_sum(x, out, clear=True)` 不直接在 `out` 上归约，而要走「临时 buffer + 加回」？
**答**：因为 warp 归约会让同一个值被累加「warp 内线程数」次。先归约到临时 buffer、再把原 `out` 加回去，才能得到正确的累加语义（见 `reduce_op.py:220-227`）。

**练习 2**：`reduce_max(x, m, dim=1, clear=True)` 里 `clear=True` 把 `m` 初始化成什么？
**答**：\(-\infty\)（`-T.infinity(dtype)`），保证任何有限输入都能成为新的最大值。

**练习 3**：把 `batch=4` 传给 `reduce_sum` 会改变结果吗？改变的是什么？
**答**：不改变结果，只改变代码生成——编译器改用 `run_batch`，一次 AllReduce 处理 4 个输出元素、共用一对 barrier，barrier 总数减少约 4 倍（见 [reduce_op.py:36-39](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/reduce_op.py#L36-L39) 与 [src/cuda/op/reduce.cc:32-47](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/op/reduce.cc#L32-L47)）。

---

### 4.3 原子操作 T.atomic_*（atomic）

#### 4.3.1 概念说明

当**多个线程（或多个 block）要写同一个输出地址**时，普通的写操作会互相覆盖。例如：1000 个线程各自算出一份结果，都要累加到全局计数器 `counter[0]`。若直接 `counter[0] += v`，每个线程做的是「读 counter → 加 v → 写 counter」，三步之间会被别的线程插入，最终丢失大部分累加。

`T.atomic_add(dst, v)` 把这三步变成**不可分割的原子操作**：硬件保证一次「读-改-写」期间不会有别的线程插队。同理 `T.atomic_max/min` 是原子的取极值。代价是：原子操作可能让硬件排队串行化，比普通写慢——所以**只在真的有写冲突时才用**。

tilelang 的原子操作还有一个独特点：它同时支持**标量地址**和**整块 tile 区间**两种写法，背后走两条完全不同的 lowering 路径。

#### 4.3.2 核心流程

每个 `atomic_*` 函数都先用 `get_extent(dst)` / `get_extent(value)` 探测两边能不能推出「块大小」：

- **标量路径**（两边都推不出 extent）：发射 `tl.atomic_add_elem_op` 这类**元素级** intrinsic，参数是 `T.access_ptr(dst, "rw")` + 标量 `value`。支持 `return_prev=True`（返回旧值，用 `_ret_elem_op` 变体）和 `memory_order`。这就是「一个线程往一个地址加一个数」。
- **tile 区间路径**（至少一边能推出 extent）：发射 `tl.tileop.atomicadd`（注意前缀是 `tl.tileop.`，是个 tile op），参数是 `to_buffer_region` 编码的读/写区间。这是「整块张量原子地累加到另一块张量」。该路径**不支持** `return_prev`。

`memory_order`（内存序）遵循 C/C++ 命名，映射成数字 id：

\[ \text{relaxed}=0,\ \text{consume}=1,\ \text{acquire}=2,\ \text{release}=3,\ \text{acq\_rel}=4,\ \text{seq\_cst}=5 \]

`atomic_add` 还有个 `use_tma=True` 选项：在 SM90+ 上改用 TMA 的 `cp.reduce` 做原子加。

此外有几个特化变体：

- `atomic_addx2` / `atomic_addx4`：双宽 / 四宽原子加（如 fp16 成对、float4 一次加），吞吐更高。
- `atomic_load` / `atomic_store`：带内存序的原子读 / 写，常用于 producer-consumer 的 flag 同步。
- `atomic_or`：目前仅支持标量地址的按位或。

#### 4.3.3 源码精读

内存序映射表：

[atomic.py:12-22](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L12-L22) —— `_MEMORY_ORDER_ID_MAP` 与 load/store 各自允许的内存序集合。

`atomic_add` 的两条路径：

[atomic.py:253-267](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L253-L267) —— **标量路径**：两边都推不出 extent 时，用 `tl.atomic_add_elem_op`（或 `return_prev` 时的 `_ret_elem_op`），传 `access_ptr(dst, "rw")` + 标量值 + 可选内存序 id。

[atomic.py:269-297](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L269-L297) —— **tile 区间路径**：校验两个 Buffer 形状一致，把 src/dst 转成 `to_buffer_region`，发射 `tl.tileop.atomicadd`；`use_tma` 与 `memory_order` 进 annotations。

`atomic_max` 的标量路径（结构同 atomic_add，op 名不同）：

[atomic.py:81-93](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L81-L93) —— `atomic_max` 标量路径用 `tl.atomic_max_elem_op`（`return_prev` 时用 `tl.atomic_max_ret_elem_op`）。

双宽原子加：

[atomic.py:300-335](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L300-L335) —— `atomic_addx2`：用 `tl.atomic_addx2_elem_op`，对 fp16/bf16 成对、对地址做 `access_ptr(..., "rw")`。`atomic_addx4`（[L338-373](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L338-L373)）同理，用于 float4（SM90+）。

带内存序的原子读 / 写：

[atomic.py:376-419](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L376-L419) —— `atomic_load`：用 `tl.atomic_load_elem_op`，只允许 `relaxed/consume/acquire/seq_cst`。文档里给了 producer-consumer 自旋等待的典型用法。

[atomic.py:422-478](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L422-L478) —— `atomic_store`：用 `tl.atomic_store_elem_op`，只允许 `relaxed/release/seq_cst`，文档里有「先写数据、再 release 写 ready_flag」的发布模式。

#### 4.3.4 代码实践

**实践目标**：用 `T.atomic_add` 实现 split-K 风格的行求和——多 block 向同一输出地址累加，这正是必须用原子的场景。

依据真实测试 [`testing/python/language/test_tilelang_language_atomic.py`](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language/test_tilelang_language_atomic.py)，其 `atomic_add_program` 用 K 个 block 各取一片 A、原子加到同一个 B（[L11-23](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language/test_tilelang_language_atomic.py#L11-L23)）。下面是它的核心片段（**直接摘自该测试**）：

[testing/python/language/test_tilelang_language_atomic.py:11-23](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language/test_tilelang_language_atomic.py#L11-L23) —— `atomic_add_program`：grid 第三维 `K` 让 K 个 block 处理同一个 `[M,N]` 输出区，每个 block 取 `A[bz, ...]` 一片，在 `T.Parallel` 里 `T.atomic_add(B[...], A_shared[i,j])`。这里 `B[bx*block_M+i, by*block_N+j]` 是**标量地址**，所以走的是 `tl.atomic_add_elem_op` 标量路径。

**操作步骤**：1) 在 GPU 机器上运行该测试的 `run_atomic_add`（[L26-34](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language/test_tilelang_language_atomic.py#L26-L34)）；2) 把 `T.atomic_add` 换成普通赋值 `B[...] = A_shared[i,j]`，重跑。

**需要观察的现象**：用普通赋值时，K 个 block 互相覆盖，`B` 只剩最后一个 block 的值，`assert_close(B, A.sum(dim=0))` 失败；用 `atomic_add` 则各 block 正确累加。

**预期结果**：原子版断言通过。无 GPU 时记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`T.atomic_add(B[i,j], v)` 和 `T.atomic_add(B, V)`（B、V 都是同形状整块张量）分别走哪条 lowering 路径？
**答**：前者 `B[i,j]` 是标量地址（推不出 extent），走 `tl.atomic_add_elem_op` 标量路径；后者两边都是整块 Buffer，走 `tl.tileop.atomicadd` tile 区间路径（见 [atomic.py:253](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L253) 与 [atomic.py:297](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L297)）。

**练习 2**：为什么 tile 区间路径不支持 `return_prev`？
**答**：因为整块原子操作涉及多个元素，每个元素的「旧值」要返回需要向量返回类型与额外的运行时支持，目前仅在标量路径实现（见 [atomic.py:287-288](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L287-L288) 与 [atomic.py:111-112](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L111-L112) 的 `NotImplementedError`）。

**练习 3**：producer-consumer 里，为什么写数据后用 `atomic_store(flag, 1, memory_order="release")`、consumer 用 `atomic_load(flag, memory_order="acquire")`？
**答**：release 保证「flag=1 之前的所有写（数据）」对其它线程可见后再写 flag；acquire 保证「读到 flag=1 之后的所有读」看到的都是 release 前的写。两者配对构成了 happens-before 关系，避免 consumer 读到未初始化的数据（见 [atomic.py:448-451](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/atomic.py#L448-L451)）。

---

### 4.4 扫描 T.cumsum / T.cummax（scan_op）

#### 4.4.1 概念说明

归约把一列数压成**一个**数；扫描（scan / prefix sum）则把一列数变成**另一列同长**的数，每个位置是「到当前位置为止的归约结果」。**inclusive scan**（前缀和）定义：

\[ y_i = x_0 \oplus x_1 \oplus \cdots \oplus x_i \]

其中 \(\oplus\) 可以是加法（`cumsum`）、取最大值（`cummax`）。例如 `[1,2,3,4]` 的 `cumsum` 是 `[1,3,6,10]`，`cummax` 是 `[1,2,3,4]`。

扫描在递推计算里很常见：前缀和（prefix sum）、running max、注意力里的 causal mask 累积、动态规划等。`reverse=True` 表示从右往左扫（反向前缀）。

GPU 上高效做扫描，标准做法是 **Brent–Kung / Kogge–Stone** 这类基于 shared memory 的并行扫描算法：先把数据放进 shared memory，线程们分工合作、经过 \(\log\) 层交换把前缀算出来。**这就解释了为什么 scan 天然需要 shared memory**——归约只需把数据汇总到一个线程，而扫描要保留中间每一项，必须有个所有线程都能读写的「公告板」，那就是 shared memory。

#### 4.4.2 核心流程

`cumsum(src, dst=None, dim=0, reverse=False)` 的处理：

1. `_prepare_scan_args` 校验 `dim` 合法性、归一化负数 `dim`、若 `dst=None` 则就地写入 `src`、检查 src/dst 形状一致。
2. **若 `src` 是 fragment**：委托给 `cumsum_fragment` 这个 `@macro`。该 macro 先分配一块 shared memory，把 src 拷进去，在 shared 上做扫描，再拷回 dst——**因为 fragment（寄存器）无法在任意线程间共享，必须借道 shared memory**。
3. **若 `src` 不是 fragment（即 shared/global 等）**：直接发射 `tl.tileop.cumsum` 占位，参数是 `(src 读区间, dst 写区间, dim, reverse)`。C++ 侧的 scan 算子要求 shared buffer 是**线性布局**（linear layout），否则布局推理会报错。

`cummax` 完全对称，只是 op 键换成 `tl.tileop.cummax`、语义换成前缀最大值。

#### 4.4.3 源码精读

op 键定义：

[scan_op.py:10-11](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/scan_op.py#L10-L11) —— `_CUMSUM_OP_KEY = "tl.tileop.cumsum"` 与 `_CUMMAX_OP_KEY = "tl.tileop.cummax"`。

fragment 路径的核心 macro：

[scan_op.py:14-38](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/scan_op.py#L14-L38) —— `_scan_fragment`：`alloc_shared(src_shape, dtype, "shared.dyn")` → `copy(src, scan_smem)` → `call_intrin(tl.tileop.<op>, 读区间, 写区间, dim, reverse)`（在 shared 上就地扫描）→ `copy(scan_smem, dst)`。这段是「fragment 必须经 shared 中转」的直接证据。

参数校验与就地语义：

[scan_op.py:63-80](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/scan_op.py#L63-L80) —— `_prepare_scan_args`：`dim` 越界报错，负数归一化，`dst=None` 时就地写 `src`，并逐维检查 src/dst 形状一致。

`cumsum` 的分发：

[scan_op.py:133-146](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/scan_op.py#L133-L146) —— `cumsum`：fragment 则走 `cumsum_fragment`（无返回），否则直接发 `tl.tileop.cumsum` intrinsic。

C++ 侧：scan 算子同样「注册 + 按 target 解析」，并**强制 shared buffer 线性布局**：

[scan.cc:54-71](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/scan.cc#L54-L71) —— `InitScanOpNode`：从参数取出 src/dst 区间、`dim`、`reverse`，并校验 `dim < src.shape.size()`。

[scan.cc:73-107](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/scan.cc#L73-L107) —— `InferScanLayout`：对 shared buffer **要求线性布局**，若已有非线性格局则报错——这就是 scan 必须在连续 shared 上做的根本原因。

[scan.cc:152-155](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/scan.cc#L152-L155) 与 [scan.cc:174-177](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/scan.cc#L174-L177) —— 注册 `CumSumOp`/`CumMaxOp` 为 `tl.tileop.cumsum`/`cummax`，`Lower` 都委托给 `ResolveScanImpl(target).lower(...)`（[scan.cc:141-145](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/scan.cc#L141-L145)）。

#### 4.4.4 代码实践

**实践目标**：用 `T.cumsum` 在 shared memory 上做前缀和，与 torch 对照。

依据真实测试 [`testing/python/language/test_tilelang_language_scan.py`](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language/test_tilelang_language_scan.py)，其 shared 版核心片段如下（**直接摘自该测试**）：

[testing/python/language/test_tilelang_language_scan.py:14-28](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language/test_tilelang_language_scan.py#L14-L28) —— `cumsum_smem_test`：`alloc_shared` → `copy(A, A_shared)` → `T.cumsum(src=A_shared, dim=dim, reverse=reverse)`（就地）→ `copy(A_shared, B)`。

**操作步骤**：1) 在 GPU 机器上运行该文件的 `run_cumsum`（[L52-79](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language/test_tilelang_language_scan.py#L52-L79)），它会逐 tile 与 `A.cumsum(dim=dim)` 对照；2) 把 `dim=0` 改成 `dim=1`、`reverse=True`，观察反向前缀和。

**需要观察的现象**：`reverse=True` 时结果是「从右往左的前缀和」，等价于 `flip(cumsum(flip(A)))`（见测试的 `_torch_cummax` 思路 [L8-11](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language/test_tilelang_language_scan.py#L8-L11)）。

**预期结果**：断言通过。无 GPU 时记为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `T.cumsum` 在 fragment 上时，要先把数据拷到 shared memory？
**答**：并行扫描算法需要所有参与线程能读写中间每一项的前缀，fragment 是线程私有寄存器、无法跨线程共享，shared memory 才是 block 内的「公告板」（见 [scan_op.py:28-38](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/scan_op.py#L28-L38)）。

**练习 2**：`cumsum(src, dst=None)` 里 `dst=None` 是什么意思？
**答**：就地扫描，结果写回 `src` 自身（见 [scan_op.py:70-71](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/scan_op.py#L70-L71)）。

**练习 3**：`cumsum` 与 `reduce_sum` 最本质的区别是什么？
**答**：`reduce_sum` 把一维压成标量（改变形状，输出少一维）；`cumsum` 输出与输入同形状，每个位置是「前缀」而非「总和」。所以 reduce 是「多对一」，scan 是「一对一但带状态」。

---

## 5. 综合实践：分块在线 softmax + 原子写回

本任务把本讲四个原语串起来：用 `T.fill` 初始化、用 `T.reduce_max`/`T.reduce_sum` 做在线 softmax、用 `T.atomic_add` 把分块结果写回全局。它分两部分，Part A 是核心（reduce），Part B 演示原子写回（atomic）。

### Part A：block 内在线 softmax（reduce_max + reduce_sum）

「在线 softmax」的核心是不必一次性把整行读进显存，而是**逐 tile 合并**。设当前已合并了前若干 tile，维护两个状态：行最大值 \(m\) 与指数和 \(\ell\)。读入新 tile \(x^{(t)}\) 后，更新规则为：

\[ m^{(t)} = \max\bigl(m^{(t-1)},\ \mathrm{reduce\_max}(x^{(t)})\bigr) \]

\[ \ell^{(t)} = \ell^{(t-1)} \cdot e^{\,m^{(t-1)}-m^{(t)}} + \mathrm{reduce\_sum}\!\left(e^{\,x^{(t)}-m^{(t)}}\right) \]

遍历完所有 tile 后，该行 softmax 的分母就是 \(\ell^{(T)}\)，每个元素的输出为 \(e^{x_j - m^{(T)}}/\ell^{(T)}\)。这就是 FlashAttention 用的 online softmax。

下面的实现**直接改编自仓库示例** [`examples/online_softmax/online_softmax.py`](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/online_softmax/online_softmax.py)（该示例用 log2 域：`scale = 1/ln2`，`exp2`/`log2` 代替 `exp`/`log`）：

```python
# 示例代码：block 内在线 softmax（改编自 examples/online_softmax/online_softmax.py）
import torch, tilelang
import tilelang.language as T
from tilelang.profiler import do_bench

@tilelang.jit
def softmax_kernel(X, BLOCK_M=1, BLOCK_N=8192, dtype: T.dtype = T.float16):
    X: T.Tensor([M, N], dtype)
    Y = T.empty([M, N], dtype)
    accum_dtype = T.float32
    scale = 1.44269504  # log2(e) = 1/ln2

    with T.Kernel(T.ceildiv(M, BLOCK_M), threads=128) as (i_m,):
        x        = T.alloc_fragment([BLOCK_M, BLOCK_N], dtype)
        max_x    = T.alloc_fragment([BLOCK_M], dtype)
        exp_x    = T.alloc_fragment([BLOCK_M, BLOCK_N], accum_dtype)
        sum_exp  = T.alloc_fragment([BLOCK_M], accum_dtype)
        lse      = T.alloc_fragment([BLOCK_M], accum_dtype)
        T.fill(lse, -T.infinity(accum_dtype))          # ← fill：归约前置，初值 -inf

        for i_n in T.Pipelined(T.ceildiv(N, BLOCK_N)):
            T.copy(X[i_m * BLOCK_M, i_n * BLOCK_N], x)
            T.reduce_max(x, max_x, dim=1, clear=True)  # ← reduce_max：本 tile 的行最大
            for i, j in T.Parallel(BLOCK_M, BLOCK_N):
                exp_x[i, j] = T.exp2(x[i, j] * scale - max_x[i] * scale)
            T.reduce_sum(exp_x, sum_exp, dim=1, clear=True)  # ← reduce_sum：本 tile 的指数和
            for i in T.Parallel(BLOCK_M):
                # 在线合并：把旧 lse 与本 tile 的贡献融合进新 lse
                lse[i] = max_x[i] * scale + T.log2(T.exp2(lse[i] - max_x[i] * scale) + sum_exp[i])

        # 第二趟：用最终的 lse 归一化写出
        for i_n in T.Pipelined(T.ceildiv(N, BLOCK_N)):
            T.copy(X[i_m * BLOCK_M, i_n * BLOCK_N], x)
            for i, j in T.Parallel(BLOCK_M, BLOCK_N):
                y_val = T.alloc_local((), dtype) if False else x[i, j]  # 占位，见下方说明
            for i, j in T.Parallel(BLOCK_M, BLOCK_N):
                x[i, j] = T.exp2(x[i, j] * scale - lse[i])
            T.copy(x, Y[i_m * BLOCK_M, i_n * BLOCK_N])
    return Y
```

> 注：上面「第二趟」里那行 `y_val = ... if False else ...` 只是为了点出「也可以 `alloc_local` 一个临时寄存器」；示例原版直接复用 `x`，你可删掉它。原版第二趟见 [online_softmax.py:37-43](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/online_softmax/online_softmax.py#L37-L43)。

**正确性 & 性能对照**（摘自示例 [L48-61](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/online_softmax/online_softmax.py#L48-L61)）：

```python
X = torch.randn(8192, 8192, dtype=torch.float16, device="cuda")
Y = softmax_kernel(X)
torch.testing.assert_close(Y, X.softmax(dim=1), rtol=1e-2, atol=1e-2)   # 正确性
print("torch   :", do_bench(lambda: X.softmax(dim=1)))
print("tilelang:", do_bench(lambda: softmax_kernel(X)))                  # 性能
```

### Part B：用 T.atomic_add 把分块结果写回全局

Part A 里每行只由一个 block 负责，写回 `Y` 没有冲突、用 `T.copy` 即可。现在我们刻意制造冲突：**沿 N 维切成 K 份，由 K 个 block 各算一份部分和，原子累加到全局行和 `rowsum`**。这与 [`testing/python/language/test_tilelang_language_atomic.py`](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language/test_tilelang_language_atomic.py) 的 `atomic_add_program` 同构。

```python
# 示例代码：split-N 行求和，原子写回
@tilelang.jit
def rowsum_splitk(A, M=128, N=4096, BLOCK_M=128, BLOCK_N=128, dtype: T.dtype = T.float32):
    A: T.Tensor([M, N], dtype)
    B = T.empty([M], dtype)
    with T.Kernel(T.ceildiv(M, BLOCK_M), T.ceildiv(N, BLOCK_N), threads=128) as (bm, bn):
        a = T.alloc_fragment([BLOCK_M, BLOCK_N], dtype)
        s = T.alloc_fragment([BLOCK_M], dtype)
        T.copy(A[bm * BLOCK_M, bn * BLOCK_N], a)
        T.reduce_sum(a, s, dim=1, clear=True)                 # ← 分块归约
        for i in T.Parallel(BLOCK_M):
            T.atomic_add(B[bm * BLOCK_M + i], s[i])           # ← 原子写回：多 block 同地址
    return B

A = torch.randn(128, 4096, device="cuda")
B = torch.zeros(128, device="cuda")
rowsum_splitk(A, B)                                            # 注意 B 由 kernel 原子累加
torch.testing.assert_close(B, A.sum(dim=1), atol=1e-2, rtol=1e-2)
```

**操作步骤**：

1. 在 GPU 机器上先跑 Part A，确认 `assert_close(Y, X.softmax(dim=1))` 通过。
2. 再跑 Part B，确认 `assert_close(B, A.sum(dim=1))` 通过。
3. 把 Part B 的 `T.atomic_add(B[...], s[i])` 换成普通赋值 `B[...] = s[i]`，观察断言失败（多个 block 互相覆盖）。

**需要观察的现象**：

- Part A 的 `T.reduce_max`/`T.reduce_sum` 把 `[BLOCK_M, BLOCK_N]` 压成 `[BLOCK_M]`，对应在线公式里的 \(\mathrm{reduce\_max}(x^{(t)})\) 与 \(\mathrm{reduce\_sum}(e^{x^{(t)}-m^{(t)}})\)。
- Part B 中沿 N 切出的多个 `(bm, bn)` block 共享同一个输出地址 `B[bm*BLOCK_M+i]`，必须 `atomic_add`。
- 去掉 `atomic` 后，`B` 只保留「最后一个写到的 block」的结果，与 `A.sum(dim=1)` 不符。

**预期结果**：两部分的 `assert_close` 均通过；性能上 Part A 应与示例一样快于或接近 `torch.softmax`。无 GPU 时记为「待本地验证」，但去原子的失败现象可由「多 block 同地址写覆盖」静态推断。

> **何时该用 atomic_add**：当且仅当多个线程/block 可能写同一地址（split-K、scatter、直方图、梯度累加）。Part A 每个输出地址只有一个 block 写，用 `T.copy` 即可，加 atomic 反而损失性能——这是初学者最常踩的「过度原子化」坑。

## 6. 本讲小结

- **fill / clear 是归约的前置**：累加型操作（`gemm`、`reduce_sum`、`cumsum`）之前必须把输出清成正确初值（sum→0，max→\(-\infty\)，min→\(+\infty\)）。`fill` 发射 `tl.tileop.fill`。
- **reduce 是「先线程内、再跨线程 AllReduce」**：`reduce_*` 走 tile op `tl.tileop.reduce`，C++ 只支持 `local.fragment`，所以 shared 输入会先被 macro 搬到临时 fragment；CUDA 后端发射 `tl::AllReduce<...>::run`（SM90+ 用 NamedBarrier，`batch>1` 用 `run_batch` 省 barrier）。
- **atomic 有两条路径**：标量地址走 `tl.atomic_add_elem_op`（支持 `return_prev`、`memory_order`），整块 tile 走 `tl.tileop.atomicadd`（不支持 `return_prev`）。只在多线程写同一地址时才用。
- **scan 必须经 shared memory**：`cumsum`/`cummax` 是 inclusive scan，输出与输入同形状；fragment 输入会被 macro 中转到 shared，C++ 侧强制 shared 线性布局。`reverse` 反向扫描。
- **四类原语共享同一 tile op 模型**：DSL 留 `tl.tileop.*` 占位 → C++ 按 target 解析唯一实现 → `lower_tile_op` 展开成硬件指令，与 u3-l1 的 `T.gemm` 完全同构。
- **典型组合**：在线 softmax = `fill`（初值）+ `reduce_max`（行最大）+ `reduce_sum`（指数和）+ 在线合并公式；split-K 归约 = `reduce_sum`（分块和）+ `atomic_add`（原子写回）。

## 7. 下一步学习建议

- **u3-l3《软件流水线 Pipelined》**：本讲的在线 softmax 用了 `T.Pipelined` 来隐藏访存延迟，下一讲专门讲它的 producer/consumer 自动推断与 `num_stages`。
- **u6-l2《关键 lowering Pass 解读》**：想看 `reduce`/`scan`/`fill` 的占位节点是如何被 `lower_tile_op` 展开成 `AllReduce`、shared 扫描循环的，去读 C++ Pass。
- **u6-l3《设备代码生成与模板》**：`tl::AllReduce<...>` 的模板实现就在 `src/tl_templates/cuda/reduce.h`，配合本讲的 `src/cuda/op/reduce.cc` 一起读，能看懂「tile op → CUDA 源码」的最后一公里。
- **直接读示例**：[`examples/online_softmax/`](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/online_softmax/online_softmax.py) 与 [`examples/gemm_splitk/`](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm_splitk/)（split-K + atomicadd 的工业级用法）是本讲两个原语组合的最佳真实参考。
- **测试即文档**：[`testing/python/language/test_tilelang_language_{reduce,atomic,scan}.py`](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/language) 里枚举了 scope×dtype×batch×threads 的完整组合，改参数复跑是掌握这些原语最快的方式。
