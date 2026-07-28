# 计算原语：gemm 与 reduce

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `T.gemm` 的输入输出约束（A/B 通常在 shared、C 必须是 fragment 累加器）以及 `transpose_A`/`transpose_B`/`clear_accum` 等关键参数的含义。
- 理解 `GemmWarpPolicy`（Square/FullRow/FullCol）如何把一个 threadblock 内的 warp 切分到 M、N 两个维度，并知道它对性能的影响。
- 学会用 `T.reduce_max/min/sum` 等 `reduce_*` 系列原语对一个 tile 做规约，理解 `dim` 与 `clear` 的语义，以及不同显存 scope（shared/fragment）组合下的内部处理差异。
- 掌握 `T.fill` 与 `T.clear` 的初始化用途，了解 `alloc_reducer` + `finalize_reducer` 这条「跨线程规约」进阶链路。
- 能在 quickstart 的 matmul 基础上改造出一个用 `T.reduce_max` 做「逐行最大值」的 kernel 并校验。

## 2. 前置知识

本讲默认你已经掌握 [u2-l2 Tile 声明与显存层级](u2-l2-tile-alloc.md) 中的核心结论，尤其是：

- **fragment（寄存器张量）**：元素被打散分布在 warp 的线程上，专门对接 tensor core（mma/wgmma/tcgen05）。它是 `T.gemm` 累加器 C 的**硬性要求**——你不能把 shared/global 直接当作 gemm 的累加器。
- **shared memory**：threadblock 内可共享的快速显存，通常是 gemm 输入 A、B tile 的存放地。
- 分配原语 `alloc_shared`/`alloc_fragment`/`alloc_local` 都是对 `alloc_buffer` 的薄封装，区别只是 scope。

如果你还没读过 [u1-l3 quickstart 详解](u1-l3-quickstart.md)，建议先看一眼 `examples/quickstart.py`——它正是本讲所有改造的起点。本讲只聚焦「计算原语」，数据搬运（`T.copy`/`T.view`）和循环（`T.Pipelined`）分别在 [u2-l5](u2-l5-copy-view.md) 与 [u2-l4](u2-l4-loops-control-flow.md) 讲。

一个最关键的直觉：TileLang 的这些 `T.*` 计算原语，**本质上是把一段「tile 级意图」翻译成一个 TVM `tir.call_intrin`**。Python 端只负责声明「我要在这几个 tile 上做矩阵乘/规约/填充」，真正的指令生成（mma/wgmma/AllReduce 模板调用）发生在 C++ 的 lowering 阶段。理解这一点，你就抓住了本讲所有原语的共同骨架。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilelang/language/gemm_op.py` | `T.gemm`/`gemm_v1`/`gemm_v2` 的 Python 前端，拼装 `tir.call_intrin` |
| `tilelang/tileop/base.py` | `GemmWarpPolicy` 枚举与 warp 切分算法（Python 侧） |
| `tilelang/language/reduce_op.py` | `reduce_*` 系列、`cumsum`、`finalize_reducer`、`warp_reduce_*` |
| `tilelang/language/fill_op.py` | `T.fill` 与 `T.clear`（clear = fill 0） |
| `tilelang/language/allocate.py` | `alloc_reducer`：声明跨线程规约缓冲区（与 `finalize_reducer` 配套） |
| `src/op/gemm.cc` | GEMM 算子的 C++ lowering，emit 出 mma/wgmma/tcgen05 模板调用 |
| `src/op/reduce.cc` | Reduce 算子的 C++ lowering，emit 出 `tl::AllReduce<...>` 调用 |
| `src/op/fill.cc` | Fill 算子的 C++ lowering |
| `examples/quickstart.py` | 本讲实践的改造基底（matmul+relu） |

> 本讲的「源码精读」会同时引用 Python 前端（决定语义）与 C++ lowering（决定真正生成的指令）。前者是「你想表达什么」，后者是「它最终长什么样」。

---

## 4. 核心概念与源码讲解

### 4.1 T.gemm：矩阵乘原语

#### 4.1.1 概念说明

`T.gemm(A, B, C)` 表达的是一次 tile 级矩阵乘：

\[ C \mathrel{+}= A \times B \]

注意是 **`+=`**：gemm 默认把乘加结果**累加**到 C 上，而不是覆盖。这正是它要做「累加器」的原因——K 维循环里每一轮的 partial sum 都往同一个 C 上叠加，最终得到完整的矩阵乘。

三个关键约束：

1. **C 必须是 fragment**。因为累加发生在 tensor core 的寄存器上。A、B 通常是从 shared memory 喂进来的 tile。
2. **K 维必须匹配**。`A` 的最后一维（或倒数第二维，看是否转置）必须等于 `B` 对应的 K 维。
3. **C 的形状决定 M、N**。M、N 直接从 C 的 2D 形状读出，不单独传参。

#### 4.1.2 核心流程

`T.gemm` 在前端做的事非常薄，可以概括为：

```
T.gemm(A, B, C, transpose_A, transpose_B, policy, clear_accum, ...)
   │
   ├─ 把 A/B/C（可能是 let 绑定的变量）规整成 BufferRegion
   ├─ 从 A_region/B_region/C_region 抽取 shape / stride / offset
   ├─ 由 C_shape 推出 (M, N)；由 transpose 与 A/B shape 推出 K，并断言 K_A == K_B
   └─ tir.call_intrin(op_key, A_arg, B_arg, C_arg, trans_A, trans_B,
                       M, N, K, policy, clear_accum, stride_a, stride_b,
                       offset_a, offset_b, k_pack, wg_wait, mbar, C_coords)
```

其中 `op_key` 是字符串 `"tl.tileop.gemm"`（v1）或 `"tl.tileop.gemm_py"`（v2）。C++ 侧的 `GemmNode::Lower` 再把这些参数实例化成一条形如 `tl::gemm<...>(Aptr, Bptr, Cptr)` 的设备函数调用，目标架构（sm70/sm80/sm90/sm100…）决定它具体指向 mma 还是 wgmma 还是 tcgen05 模板。

#### 4.1.3 源码精读

`T.gemm` 的 Python 入口与参数文档（含 `clear_accum`、`k_pack`、`wg_wait`、`mbar` 的逐项说明）：

[tilelang/language/gemm_op.py:191-222](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py#L191-L222) —— `gemm` 默认按 `_env.use_gemm_v1()` 在 v1（`tl.tileop.gemm`）与 v2（`tl.tileop.gemm_py`，号称更快编译）之间选择。

真正干活的是共享实现 `_gemm_impl`。它先做参数合法化与维度推导：

[tilelang/language/gemm_op.py:57-96](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py#L57-L96) —— 断言 C 必须是 2D、A/B 至少 2D（多余的前导维必须为 1），并由 C 推 `(M,N)`、由 transpose 推 `K`，再断言 `K_A == K_B`：

```python
M, N = C_shape
K = A_shape[-2] if transpose_A else A_shape[-1]
K_B = B_shape[-1] if transpose_B else B_shape[-2]
assert prim_expr_equal(K, K_B), f"T.gemm K shape check failed: K_A = {K}, K_B = {K_B}"
```

最后拼装出唯一的 `tir.call_intrin`——这就是 gemm 在 IR 里的全部形态：

[tilelang/language/gemm_op.py:104-126](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/gemm_op.py#L104-L126) —— 把 18 个参数（含 region、转置标志、M/N/K、policy、clear_accum、stride/offset、k_pack、wg_wait、mbar、C 坐标）一次性塞进 intrin 调用。

C++ 侧，`GemmNode::Lower` 先确定使用哪条指令路径，再按 policy 切分 warp：

[src/op/gemm.cc:437-449](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L437-L449) —— 由 `getGemmInst` 选定 mma/wgmma/tcgen05，由 `policy_->computeWarpPartition(m_, n_, block_size, target, gemm_inst)` 得到 `(warp_m, warp_n)`。

`clear_accum` 最终被直接拼进模板实参列表（决定生成的模板是否在 mma 前清零）：

[src/op/gemm.cc:535-545](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L535-L545) —— `ss << ", " << bool(clear_accum_bool)` 把 `clear_accum` 作为模板参数 emit 进 `tl::gemm<M,N,K,warp_m,warp_n,transA,transB,clear_accum,...>`。

在 quickstart 里，`T.gemm` 就是这样在 `T.Pipelined` 的 K 维循环中被反复调用、逐块累加：

[examples/quickstart.py:36-38](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L36-L38) —— `T.gemm(A_shared, B_shared, C_local)`，A/B 来自 shared、C_local 是 fragment 累加器。

> **关于 `clear_accum` 与 `T.clear` 的关系**：quickstart 在循环**之前**显式调了 `T.clear(C_local)`（见 [examples/quickstart.py:25-26](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/quickstart.py#L25-L26)），而 `T.gemm` 用默认 `clear_accum=False`。这是正确的写法：第一次需要清零、之后每轮都必须是 `+=`（累加）。如果你把 `clear_accum=True` 用在循环内的 gemm 上，每轮都会清掉上一轮的累加结果，矩阵乘就错了。

#### 4.1.4 代码实践

**实践目标**：直观感受 `clear_accum` 的作用，确认 gemm 是「累加」而非「覆盖」。

**操作步骤**（源码阅读 + 局部参数修改型）：

1. 打开 `examples/quickstart.py`，确认它的结构是 `T.clear(C_local)` → `for ko in T.Pipelined(...)` 循环里多次 `T.gemm(...)`。
2. 做两个对照实验（每次只改一处，分别运行）：
   - **实验 A**：注释掉 `T.clear(C_local)` 这一行，其余不变，运行并看 `torch.testing.assert_close` 是否仍然通过。
   - **实验 B**：恢复 `T.clear`，但把循环内的 `T.gemm(A_shared, B_shared, C_local)` 改成 `T.gemm(A_shared, B_shared, C_local, clear_accum=True)`，运行。

**需要观察的现象**：
- 实验 A：因为累加器初始未清零，第一轮 gemm 把部分和加到了「脏」的寄存器初值上，结果会偏离参考值（具体偏多少取决于寄存器初值，可能通过也可能不通过，但语义上已不正确）。
- 实验 B：每一轮 gemm 都先把 C_local 清零再算，相当于只保留了**最后一轮** K 分块的乘积，结果与 `a @ b` 严重不符，校验必然失败。

**预期结果**：实验 A 风险（取决于初值，通常偏差可见）；实验 B 必然报错。两者共同验证了「gemm 默认累加、需要靠外部 `T.clear` 完成首次清零」这一语义。运行结果**待本地验证**（我未在此环境执行）。

#### 4.1.5 小练习与答案

**练习 1**：若 A 形状是 `(K, M)`、B 形状是 `(K, N)`，想算 `C = Aᵀ @ B`，`T.gemm` 该怎么写？

**答案**：`T.gemm(A, B, C, transpose_A=True)`。此时 `K = A_shape[-2]`（转置后取倒数第二维），与 B 的 `K_B = B_shape[-2]` 匹配；C 形状 `(M, N)`。

**练习 2**：为什么 `T.gemm` 的 C 不能传一个 shared memory 的 tile？

**答案**：因为累加发生在 tensor core 的寄存器（fragment）上。shared memory 不具备「元素按线程分散分布」的 fragment 布局，无法直接承载 mma/wgmma 的累加器。这正是 [u2-l2](u2-l2-tile-alloc.md) 强调 fragment 是 gemm 累加器硬性要求的原因。

---

### 4.2 GemmWarpPolicy：warp 切分策略

#### 4.2.1 概念说明

一个 threadblock 有多个 warp（例如 128 threads = 4 个 warp）。做 `(block_M, block_N)` 的 tile GEMM 时，这些 warp 可以沿 M 维、N 维或同时沿两维分工。`GemmWarpPolicy` 就是这个切分策略：

- `Square`（默认）：尽量在 M、N 上均衡分配，比例尽量贴合矩阵的长宽比。
- `FullRow`：所有 warp 都分到 M（行）维度，每个 warp 负责完整的几行。
- `FullCol`：所有 warp 都分到 N（列）维度，每个 warp 负责完整的几列。

它直接影响 L2 cache 局部性、shared memory bank conflict 和 tensor core 利用率，是调优 GEMM 性能时最常拨动的开关之一。

#### 4.2.2 核心流程

给定 `(M, N, num_warps)` 与策略，`compute_warp_partition` 返回 `(m_warp, n_warp)`，满足 `m_warp * n_warp == num_warps`：

- `FullRow`：`m_warp = num_warps, n_warp = 1`；若 M 不能被 `m_warp*16` 整除，则尽量把多余 warp 让给 N。
- `FullCol`：`n_warp = num_warps, m_warp = 1`；对称处理。
- `Square`：穷举所有满足约束的 `(m, n)` 组合，选一个让「每 warp 负载的长宽比」最接近 `M/N` 理想比的方案。

约束来自硬件：每个 warp 在 M 维至少要负责 16 个元素（mma 的 16 行），在 N 维至少 8 个元素。

#### 4.2.3 源码精读

枚举定义（`Square=0 / FullRow=1 / FullCol=2`）：

[tilelang/tileop/base.py:5-12](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/tileop/base.py#L5-L12)

`Square` 策略的核心搜索逻辑（穷举 + 最优均衡）：

[tilelang/tileop/base.py:114-152](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/tileop/base.py#L114-L152) —— 关键是这几行：

```python
max_m_warps = M // 16   # 每个 warp 至少 16 行
max_n_warps = N // 8    # 每个 warp 至少 8 列
...
for m in range(1, min(max_m_warps, num_warps) + 1):
    n = num_warps // m
    if m * n != num_warps: continue
    balance = abs((M/(m*16)) / (N/(n*8)) - ideal_ratio)
    ...  # 记录 balance 最小的 (m, n)
```

`FullRow` / `FullCol` 的「不整除就回退」处理在：

[tilelang/tileop/base.py:84-112](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/tileop/base.py#L84-L112)

还有一个反向工具 `from_warp_partition`，根据已有切分反推策略类别：

[tilelang/tileop/base.py:160-185](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/tileop/base.py#L160-L185)

> 注意：Python 侧的 `compute_warp_partition` 是「参考实现」；C++ lowering 里 `GemmWarpPolicyNode::computeWarpPartition`（[src/op/gemm.cc:144](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L144) 起）才是真正喂给 codegen 的版本，它还额外处理 tcgen05（忽略切分）等目标差异。两者算法思想一致。

#### 4.2.4 代码实践

**实践目标**：对比三种 policy 的性能差异，建立「policy 影响性能」的直觉。

**操作步骤**：

1. 复制 `examples/quickstart.py` 为 `gemm_policy.py`。
2. 把 `T.gemm(A_shared, B_shared, C_local)` 分别改成：

```python
import tilelang.language as T
from tilelang.tileop.base import GemmWarpPolicy

T.gemm(A_shared, B_shared, C_local, policy=GemmWarpPolicy.Square)      # 实验 1
# T.gemm(A_shared, B_shared, C_local, policy=GemmWarpPolicy.FullRow)   # 实验 2
# T.gemm(A_shared, B_shared, C_local, policy=GemmWarpPolicy.FullCol)   # 实验 3
```

3. 用 `matmul_relu_kernel.get_profiler().do_bench()` 记录每种 policy 的延迟。

**需要观察的现象**：三种 policy 输出都应通过正确性校验（policy 只改并行划分，不改数学结果），但延迟会有差异；通常在 M≈N 的方阵上 `Square` 表现稳健，而窄长矩阵上 `FullRow`/`FullCol` 可能更优。

**预期结果**：三组结果数值一致、延迟不同。具体谁更快**待本地验证**（依赖你的 GPU 架构与 tile 尺寸）。

#### 4.2.5 小练习与答案

**练习**：一个 block 有 8 个 warp，要算 `(128, 256)` 的 tile GEMM。按 `Square` 策略，`m_warp`、`n_warp` 大致会是多少？

**答案**：`max_m_warps = 128//16 = 8`，`max_n_warps = 256//8 = 32`，`ideal_ratio = 128/256 = 0.5`。在 `m*n=8` 且 `m≤8`、`n≤32` 的组合里，`m=2, n=4` 时 `m_per_warp=128/(2·16)=4`、`n_per_warp=256/(4·8)=8`，比值 `4/8=0.5` 与理想比完全吻合，是最优解。故 `m_warp=2, n_warp=4`。

---

### 4.3 reduce_* 系列：规约原语

#### 4.3.1 概念说明

规约就是把一个 tile 沿某一维「收缩」：`reduce_max` 取最大、`reduce_min` 取最小、`reduce_sum` 求和、`reduce_abssum`/`reduce_absmax` 取绝对值的和/最大，还有 `reduce_bitand/bitor/bitxor` 等位运算。所有这些都由同一个底层函数 `reduce(buffer, out, reduce_type, dim, clear)` 派生。

典型用途：attention 里的 `reduce_max`（求 softmax 分母前的最大值）、`reduce_sum`（求 softmax 分母）、layernorm 的方差等。

#### 4.3.2 核心流程

`reduce_*` 是一个 `@macro`，它的关键在于**按 src/dst 的 scope 组合分四种路径**处理：

| src scope | dst scope | 内部做法 |
| --- | --- | --- |
| shared | shared | 各开一个 fragment 中转：copy 到 frag_in → reduce → copy 出 frag_out |
| shared | fragment | copy 到 frag_in → reduce 到 out |
| fragment | shared | reduce 到 frag_out → copy 出 |
| fragment | fragment | 直接 reduce，无需中转 |

真正干活的始终是 intrin `"tl.tileop.reduce"`（4 个输入：src region、dst region、reduce_type、dim、clear——注意 Python 层 `reduce` 把 clear 也传进去，共 5 个序列化参数中 reduce_type/dim/clear 占 3 个）。

`dim` 支持负索引（Python 风格，`-1` 表示最后一维），由 `_legalize_dim` 归一化为正索引。`clear` 控制是否先把输出初始化为「幺元」：

| reduce_type | 幺元（init value） |
| --- | --- |
| sum / abssum | 0 |
| max | \(-\infty\)（浮点）/ INT_MIN / 0（无符号） |
| min | \(+\infty\)（浮点）/ INT_MAX / 全 1（无符号） |
| absmax | 0 |
| bitand | 全 1 |
| bitor / bitxor | 0 |

C++ lowering 里，规约分两步：**线程内局部规约**（展开的内层循环）+ **跨线程 AllReduce**（emit 出 `tl::AllReduce<Reducer, reducing_threads, scale, offset>::run`，Hopper/sm100/sm120 用 `run_hopper` 变体）。

#### 4.3.3 源码精读

公开 API 一览（`reduce_max/min/sum/abssum/absmax/bit*` 都只是给底层 `reduce` 套个 reduce_type）：

[tilelang/language/reduce_op.py:107-181](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/reduce_op.py#L107-L181) —— 例如 `reduce_max(buffer, out, dim=-1, clear=True)` 把 `dim` 归一化后调用 `reduce(buffer, out, "max", dim, clear)`。

底层 `reduce` 的四种 scope 路径：

[tilelang/language/reduce_op.py:42-104](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/reduce_op.py#L42-L104) —— `shared→shared` 路径里 `alloc_fragment` 中转的写法最值得读，它解释了「为什么 reduce 永远落到 fragment 上做」：tensor core 友好的线程分布 + 跨线程 AllReduce 都发生在 fragment scope。

输出形状校验（reduce 必须沿某一维收缩掉，允许 `[X,Y]` 或 `[X,1,Y]`）：

[tilelang/language/reduce_op.py:33-40](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/reduce_op.py#L33-L40)

C++ 侧，构造函数解析 5 个序列化参数并查表得 `ReduceType`：

[src/op/reduce.cc:31-43](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/reduce.cc#L31-L43)

幺元表（与上表对应，C++ 真正生成的初值）：

[src/op/reduce.cc:55-100](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/reduce.cc#L55-L100)

AllReduce 调用的 emit（决定走 `run` 还是 `run_hopper`）：

[src/op/reduce.cc:311-333](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/reduce.cc#L311-L333)

> **关于 `clear` 的一个微妙点**：对 sum/abssum/bitor/bitxor/bitand 这类「累加型」规约，当 `clear=False`（即想把结果叠加到 out 已有值上）时，lowering 会用一个临时 buffer 先算出本次规约结果，再把它累加回 dst（见 [src/op/reduce.cc:245-261](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/reduce.cc#L245-L261)）。原因是跨线程 AllReduce 会让每个线程都「看到」一份结果，直接写到 dst 会被重复累加多次。用临时 buffer 隔离能保证语义正确。日常用默认 `clear=True` 即可避开这个细节。

#### 4.3.4 代码实践

**实践目标**：用 `T.reduce_max` 对 gemm 累加器做「逐行最大值」并校验。这是本讲的主实践，完整可运行。

**操作步骤**：

1. 新建 `matmul_rowmax.py`，内容如下（示例代码，基于 quickstart 改造）：

```python
# 示例代码：基于 quickstart，把 matmul 结果在每个列分块内逐行取 max
import tilelang
import tilelang.language as T
import torch

@tilelang.jit
def matmul_rowmax(M, N, K, block_M, block_N, block_K,
                  in_dtype=T.float16, accum_dtype=T.float32, out_dtype=T.float16):
    N_blocks = N // block_N  # 假设 N 能被 block_N 整除

    @T.prim_func
    def main(
        A: T.Tensor((M, K), in_dtype),
        B: T.Tensor((K, N), in_dtype),
        C: T.Tensor((M, N_blocks), out_dtype),  # 每个列分块的逐行最大值
    ):
        # bx 遍历列分块，by 遍历行分块
        with T.Kernel(N_blocks, T.ceildiv(M, block_M), threads=128) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), in_dtype)
            B_shared = T.alloc_shared((block_K, block_N), in_dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            C_row   = T.alloc_fragment((block_M,), accum_dtype)   # 逐行最大值

            T.clear(C_local)                                      # 首次清零累加器
            for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
                T.copy(A[by * block_M, ko * block_K], A_shared)
                T.copy(B[ko * block_K, bx * block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)              # 累加

            # 沿最后一维（列，dim=1）做 reduce_max，得到每行的最大值
            T.reduce_max(C_local, C_row, dim=1, clear=True)

            T.copy(C_row, C[by * block_M, bx])                   # 写回该列分块

    return main


M = N = K = 1024
block_M = block_N = 128
block_K = 32
kernel = matmul_rowmax(M, N, K, block_M, block_N, block_K)

a = torch.randn(M, K, device="cuda", dtype=torch.float16)
b = torch.randn(K, N, device="cuda", dtype=torch.float16)
N_blocks = N // block_N
c = torch.empty(M, N_blocks, device="cuda", dtype=torch.float16)

kernel(a, b, c)

# 参考结果：先算完整 matmul，再在每个列分块内逐行取 max
full = a.float() @ b.float()                                   # (M, N)
ref = full.reshape(M, N_blocks, block_N).amax(dim=2).to(torch.float16)

torch.testing.assert_close(c, ref, rtol=1e-2, atol=1e-2)
print("reduce_max (per column-block, per row) matches PyTorch reference.")

profiler = kernel.get_profiler(tensor_supply_type=tilelang.TensorSupplyType.Normal)
print(f"Latency: {profiler.do_bench()} ms")
```

2. 运行 `python matmul_rowmax.py`。

**需要观察的现象**：
- kernel 成功编译并运行，`assert_close` 通过。
- 若把 `T.reduce_max(..., dim=1, ...)` 误写成 `dim=0`，你会得到「每列最大值」而非「每行最大值」，输出形状 `(block_N,)` 与 `C_row`（`(block_M,)`）不符，编译期就会报形状校验错误——这正好印证了 [reduce_op.py:33-40](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/reduce_op.py#L33-L40) 的输出形状约束。

**预期结果**：输出与 `ref` 数值一致（rtol/atol=1e-2 内）。具体延迟**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把上面的 `T.reduce_max` 换成「逐行之和」，参考值该怎么算？

**答案**：`T.reduce_sum(C_local, C_row, dim=1, clear=True)`；参考值改为 `ref = full.reshape(M, N_blocks, block_N).sum(dim=2).to(torch.float16)`。

**练习 2**：为什么 `reduce` 对 `shared → shared` 要绕一圈经过 fragment？

**答案**：跨线程规约（AllReduce）依赖「元素按线程分布」的 fragment 布局来定位哪个线程持有哪部分数据；shared memory 是 threadblock 线性共享的，没有线程分布信息。所以必须先把 shared 数据 copy 到 fragment 上、在 fragment 上做 AllReduce，再 copy 回 shared。

---

### 4.4 fill/clear 与 reducer：初始化与跨线程规约

#### 4.4.1 概念说明

- **`T.fill(buffer, value)`**：把一个 buffer（或它的某个 region/slice）整体填成 `value`。常用于给累加器、reducer 写初值。
- **`T.clear(buffer)`**：就是 `fill(buffer, 0)` 的语法糖，quickstart 里 `T.clear(C_local)` 即此。
- **`alloc_reducer` + `finalize_reducer`**：进阶的跨线程规约机制。当你想在 `T.Parallel` 循环里让多个线程向同一个缓冲区做 `+=`/`max=`，就需要一个「reducer」缓冲区；写之前必须 `T.fill` 正确的初值，写完之后必须 `T.finalize_reducer` 才能把各线程的部分结果合并成最终值。

#### 4.4.2 核心流程

`fill`/`clear` 的 Python 端同样是拼装 intrin：

```
fill(buffer, value)  →  tir.call_intrin("tl.tileop.fill", region, value)
clear(buffer)        →  fill(buffer, 0)
```

C++ `FillNode::Lower` 则按 dst 的 scope 生成不同的并行写入循环：fragment/local/shared/global 各自走一条「构造 SIMT 循环 → 按线程切分 → 向量化」的路径。

reducer 这条链路稍特殊：`alloc_reducer` 申明一个 fragment buffer 并在 block 属性里挂上 `reducer_info`（记录 op=sum/max/min 与 replication 策略）；用户在 `T.Parallel` 里对它做符合 op 的更新（sum 用 `+=`，max 用 `T.max(...)` 赋值）；最后 `T.finalize_reducer(reducer)` 触发把各副本合并。

#### 4.4.3 源码精读

`fill` 与 `clear` 的实现（含对 Buffer/BufferRegion/BufferLoad 与 let 绑定变量的兼容处理）：

[fill_op.py:9-36](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/fill_op.py#L9-L36) —— `fill` 把 region 与 value 打包成 `"tl.tileop.fill"` intrin；

[fill_op.py:39-62](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/fill_op.py#L39-L62) —— `clear` 先把 let 变量解包成真正的 region，再 `fill(..., 0)`。

C++ `FillNode::Lower` 按 scope 分支（fragment/shared/local/global 各自构造向量化线程循环）：

[src/op/fill.cc:158-197](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/fill.cc#L158-L197)

`alloc_reducer` 的申明（fragment + block 属性 `reducer_info`）与其使用契约：

[allocate.py:192-225](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L192-L225) —— 文档明确：「只有 `T.fill` 正确初值后才能开始规约；只有 `T.finalize_reducer` 后部分结果才可见」；sum 的初值必须是 0，max/min 分别用 `T.min_value`/`T.max_value`。

`finalize_reducer` 的前端（emit `"tl.tileop.finalize_reducer"` intrin）：

[reduce_op.py:367-384](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/reduce_op.py#L367-L384)

> 同一文件里还有一组 `warp_reduce_sum/max/min/...`（[reduce_op.py:387-465](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/reduce_op.py#L387-L465)），用于对一个**寄存器标量值**做 warp 内 shuffle 规约，返回值在 warp 内所有线程上一致。它和 tile 级 `reduce_*`（对一个 buffer 做）是两个层次，别混淆。

#### 4.4.4 代码实践

**实践目标**：理解 `alloc_reducer` + `finalize_reducer` 的使用契约（源码阅读型）。

**操作步骤**：

1. 阅读 [allocate.py:192-225](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/allocate.py#L192-L225) 的文档串，记下三条契约：
   - 申明时指定 `op="sum"/"max"/"min"` 与 `replication`；
   - 用 `T.fill` 写入正确的初值（sum=0；max 用 `T.min_value`，min 用 `T.max_value`）；
   - 规约写法必须与 op 一致（sum 用 `+=`，max/min 用 `T.max`/`T.min` 赋值）；
   - 结束时调用 `T.finalize_reducer(reducer)`。
2. 在仓库里搜索真实用例：

```bash
# 在项目根目录执行（只读检索）
# 搜索 alloc_reducer 的调用点
```

**需要观察的现象**：找到的示例会呈现典型的「alloc_reducer → fill 初值 → Parallel 循环里 += → finalize_reducer → 读取结果」五段式结构。

**预期结果**：能画出 reducer 的数据流：各线程副本 →（finalize 后）→ 合并到单一结果。具体示例位置**待确认**（用上面的搜索命令在仓库中定位）。

#### 4.4.5 小练习与答案

**练习 1**：`T.clear(buf)` 和 `T.fill(buf, 0)` 完全等价吗？

**答案**：语义等价——`clear` 内部就是 `fill(buf, 0)`（见 [fill_op.py:54/62](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/fill_op.py#L62)）。`clear` 还额外处理了「buf 是 let 绑定变量」的情形，先解包再 fill。

**练习 2**：用 `alloc_reducer(op="max")` 时，初值应该填什么？为什么？

**答案**：填 `T.min_value(dtype)`（该 dtype 的最小值）。因为 max 规约的「幺元」是 \(-\infty\)，只有当初值比任何真实数据都小，第一次 `T.max(初值, x)` 才会正确得到 `x`。这与 reduce.cc 里 `reduce_max` 的初值 \(-\infty\)（[src/op/reduce.cc:65-72](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/reduce.cc#L65-L72)）是同一道理。

---

## 5. 综合实践

把本讲四个模块串起来：**写一个「带逐行 softmax 分母」的 matmul kernel**——既用 gemm，又用 reduce_max/reduce_sum，还用到 fill/clear。

**任务**：计算 `C[i, bx] = (max_j P[i, bx, j])` 与 `S[i, bx] = (sum_j exp(P[i, bx, j] - max))`，其中 `P = A @ B` 在每个列分块 `(block_M, block_N)` 内。也就是对每个列分块，先 gemm 出 `C_local`，再做一次行级 max（数值稳定用），再做一次行级 exp+sum。这其实是 FlashAttention 里每个 block 的核心两步规约的简化版。

**建议步骤**：

1. 以本讲 4.3.4 的 `matmul_rowmax.py` 为模板，新增一个 fragment `C_sum: (block_M,)`。
2. `T.reduce_max(C_local, C_row, dim=1, clear=True)` 得到每行最大值。
3. 用 `T.Parallel(block_M, block_N)` 计算 `C_local[i,j] = T.exp(C_local[i,j] - C_row[i])`（`T.exp` 来自 math intrinsics）。
4. `T.reduce_sum(C_local, C_sum, dim=1, clear=True)` 得到每行 exp 之和。
5. 把 `C_row`（max）和 `C_sum`（sum）分别写回两个 global 输出。
6. 参考：`full = a@b`；`ref_max = full.reshape(M, Nb, bN).amax(2)`；`ref_sum = torch.exp(full.reshape(M,Nb,bN) - ref_max.unsqueeze(-1)).sum(2)`，逐项比对。

**验收标准**：max 与 sum 两个输出都在 `rtol/atol=1e-2` 内匹配参考；能说清每一步数据所在的 scope（global→shared→fragment→fragment）。这一步把 gemm 的累加语义、reduce_max/reduce_sum 的 dim 与 clear、以及 fill/clear 的初始化全部用到。

## 6. 本讲小结

- `T.gemm` 是「累加型」tile 矩阵乘：C（必须 fragment）`+=` A（shared）× B（shared），K 维必须匹配，M/N 由 C 形状决定；首次清零靠外部 `T.clear`，循环内用默认 `clear_accum=False`。
- `GemmWarpPolicy`（Square/FullRow/FullCol）决定 warp 在 M/N 两维的切分，直接影响性能；`Square` 用穷举选最均衡方案。
- `reduce_*` 系列底层是同一个 `reduce(buffer, out, reduce_type, dim, clear)` 宏，按 src/dst 的 scope 组合分四条路径，最终都在 fragment 上做「线程内展开 + 跨线程 AllReduce」。
- `clear` 控制是否先写入幺元（sum=0、max=\(-\infty\)、min=\(+\infty\) 等）；累加型规约在 `clear=False` 时会用临时 buffer 防止重复累加。
- `T.fill`/`T.clear` 做初始化；`alloc_reducer` + `finalize_reducer` 是更高级的跨线程规约通道，须配 fill 初值与符合 op 的更新写法。
- 所有 `T.*` 计算原语在前端都只是拼装 `tir.call_intrin`，真正的 mma/wgmma/AllReduce 指令在 C++ lowering（`src/op/*.cc`）里生成。

## 7. 下一步学习建议

- 想理解 `T.gemm` 的累加是如何藏在 `T.Pipelined` 软件流水里的，继续读 [u2-l4 循环与控制流](u2-l4-loops-control-flow.md)。
- 想搞清 A/B tile 是怎么从 global 搬到 shared 喂给 gemm 的，读 [u2-l5 数据搬运：copy 与 view](u2-l5-copy-view.md)。
- 想看 `reduce_*` 背后的 layout 推导与跨线程 AllReduce 细节，跳到进阶层 [u4-l1 Layout 推理机制](u4-l1-layout-inference.md)。
- 想追 `T.gemm` 从 Python intrin 到 C++ Op 的完整调用链，读 [u7-l1 C++ 算子实现机制](u7-l1-cpp-ops.md) 与 [u7-l2 CUDA 模板与 GEMM 内核族](u7-l2-cuda-gemm-templates.md)。
