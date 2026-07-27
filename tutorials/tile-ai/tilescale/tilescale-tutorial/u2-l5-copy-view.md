# 数据搬运：T.copy 与 T.view

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `T.copy(src, dst)` 在 global / shared / fragment 之间搬运一个 tile，并说清楚搬运范围（extent）是怎么由源/目的 tile 自动推断出来的。
- 读懂 `T.copy` 在前端只是拼装一条 `tl.tileop.copy` intrin，真正的 TMA / cp.async / ldmatrix / 普通并行 load-store 指令是在 C++ lowering 阶段按目标硬件自动挑选的。
- 用 `T.view` / `T.reshape` 以**零拷贝**方式把同一块显存重新解释成新的形状或数据类型，并理解它的硬性约束（bit 总数守恒、只能构造连续 stride）。
- 用 `T.c2d_im2col` 理解卷积如何借助 Hopper 的 TMA im2col 把「取卷积窗口」融合进一次 global→shared 搬运，以及非 Hopper 架构为什么要退回到手写 gather 循环。

本讲承接 u2-l2（显存层级与 tile 声明）。你已经在那里学过 `alloc_shared` / `alloc_fragment`；本讲回答的问题是：**tile 在各级显存之间怎么搬、搬完之后怎么换一种方式看同一块数据**。

## 2. 前置知识

在进入源码之前，先用三个直觉把概念立起来。

**(1) GPU 显存是一栋分层的「仓库」。** 数据从最远最大但最慢的 global memory（HBM），搬到离计算单元最近但最小的 fragment / 寄存器，中间通常要在 shared memory（SMEM）中转。每一次搬运都有代价，所以我们要让搬运「足够粗」以摊销寻址开销（用 tile 而不是逐元素），又要「足够巧」以用上硬件最快的那条路径（TMA bulk、cp.async 异步、ldmatrix 矩阵加载等）。

**(2) TileLang 把搬运抽象成「按目的推断范围」的 copy。** 你只要说清楚源 tile 和目的 tile 长什么样，`T.copy` 就自动算出要搬多少、怎么并行、用哪条指令。这是它和「手写两重 for 循环逐元素赋值」的根本区别。

**(3) 「视图（view）」不搬数据，只换看法。** 同一段连续字节，既可以当成 `(M, K)` 的 float16 矩阵看，也可以重新解释成形状不同、甚至数据类型不同的 buffer，前提是**总 bit 数不变**。这和 NumPy 的 `ndarray.view()` / PyTorch 在同一 storage 上做 reshape 是同一个思想。

> 本讲涉及的关键 scope 字符串回顾（来自 u2-l2）：`global`、`shared` / `shared.dyn`（动态共享内存）、`local` / `local.fragment`（寄存器/fragment）。`T.copy` 选哪条指令，本质上就是看 src 和 dst 各自在哪一层 scope。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/copy_op.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py) | **本讲主角**。`T.copy` 与 `T.c2d_im2col` 的前端实现：推断 extent、构造 region、拼装 `tl.tileop.*` intrin。 |
| [tilelang/language/customize.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/customize.py) | `T.reshape` 与 `T.view` 的实现：零拷贝重解释，bit 总数守恒校验。 |
| [tilelang/utils/language.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/language.py) | `copy` 依赖的工具：`to_buffer_region`、`get_buffer_region_from_load`、`legalize_pairwise_extents`、`bits_product`。 |
| [tilelang/language/proxy.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py) | `T.Tensor` 代理对象，`view`/`reshape` 返回值就靠它构造；决定「连续 stride」如何生成。 |
| [src/op/copy.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc) | **C++ 后端**。`Copy` 与 `Conv2DIm2ColOp` 算子的实现：指令选择（`GetCopyInst`）、SIMT 并行循环生成、TMA 描述符构造、im2col lowering。 |
| [examples/convolution/example_convolution.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/convolution/example_convolution.py) | `T.c2d_im2col`（Hopper）与手写 gather（非 Hopper）的真实对照，以及 `T.copy` 切片语法的活样例。 |

先记住一条导出关系：`tilelang/language/__init__.py:52` 处 `from .copy_op import copy, c2d_im2col`，以及同文件 `:84-85` 处 `reshape, view` 来自 `.customize` —— 也就是说你写 `T.copy` / `T.c2d_im2col` 调的是 `copy_op.py`，写 `T.view` / `T.reshape` 调的是 `customize.py`。（仓库里还有一个 `tilelang/language/copy.py`，它用旧的 `tl.copy` op 名、且未被 `__init__` 导出，不是 `T.copy` 的实际实现，阅读时请勿混淆。）

## 4. 核心概念与源码讲解

### 4.1 T.copy：切片语法与并行化搬运

#### 4.1.1 概念说明

`T.copy(src, dst)` 是 TileLang 里**唯一的**通用 tile 级搬运原语。它接收源和目的两个「buffer-like」对象（`Buffer` / `BufferRegion` / `BufferLoad` 都行），自动推断要搬多少数据，然后交给后端挑选当前硬件上最优的指令去执行。

它最重要的设计取舍是「**按形状推断范围，而不是让你手写循环边界**」：

- 给它两个完整 `Buffer`，它就搬整个 buffer；
- 给它一个切片（如 `A[by*block_M, bx*block_N]`），它就从切片的 region 推断出要搬的 tile 形状；
- 给它两个标量 `BufferLoad`（如 `copy(a[i], b[i])`），它直接降级成一条 `b[i] = a[i]` 的 store。

#### 4.1.2 核心流程

前端 `copy()` 的执行过程可以概括为 6 步：

1. **形状校验**：若 src 和 dst 都是完整 `Buffer`，断言二者 shape 结构相等。
2. **推断 extent**：`Buffer → shape`，`BufferRegion → [r.extent for r in region]`，`BufferLoad → 由其 region 推断`，标量 `BufferLoad → None`。
3. **标量快捷路径**：若两边都是没有 region 的标量 `BufferLoad`，直接返回 `tir.BufferStore(dst.buffer, src, dst.indices)`，等价于一次赋值。
4. **广播对齐**：把缺失的 extent 当作全 1，再用 `legalize_pairwise_extents` 从尾部对齐，逐维做广播（相等则保留、一边为 1 则扩成另一边、动态不等则取 `max`）。
5. **打包成 region**：用 `to_buffer_region` 把 src/dst 各自编码成一条 `tl.region` 调用，标明读/写权限与每维 extent。
6. **发出 intrin**：拼装 `tir.call_intrin("handle", Op.get("tl.tileop.copy"), src_region, dst_region, annotations=ann)`。

extent 广播的直觉可以用一行公式概括（从右往左逐维对齐，\(x\) 取自 src、\(y\) 取自 dst）：

\[
(x, y) \mapsto
\begin{cases}
(x, y), & x = y \\
(y, y), & x = 1 \\
(x, x), & y = 1 \\
(\max(x,y),\ \max(x,y)), & \text{否则（动态形状兜底）}
\end{cases}
\]

到了 C++ 后端，`Copy` 算子再根据 src/dst 的 **scope 组合** 与硬件能力，用 `GetCopyInst` 选一条具体指令，优先级见下表（越靠上越优先）：

| 指令 | 触发条件（scope 方向） | 含义 |
| --- | --- | --- |
| BulkLoad1D / BulkStore1D | global↔shared 且**连续** | Hopper TMA，一维连续整块搬运 |
| BulkLoad / BulkStore | global↔shared，末维字节为 16 的倍数 | Hopper TMA，多维 box 搬运 |
| LDSM (ldmatrix) | shared→fragment | warp 级 8×8 矩阵加载，喂 tensor core |
| STSM (stmatrix) | fragment→shared | warp 级矩阵写回 |
| TMemLoad / TMemStore | shared.tmem↔fragment | Blackwell (sm100) tensor memory |
| Normal | 兜底 | 普通并行 load-store 循环 |

两条关键事实：**Normal 拷贝会被并行化铺到整个 threadblock 的线程上**（由 `MakeSIMTLoop` 生成 `kParallel` 循环，再由 `LowerParallelLoop` 映射到线程）；而 **TMA bulk 拷贝只由 thread 0 发起**（TMA 是单线程发起的批量搬运，描述符由一个线程构建）。

#### 4.1.3 源码精读

先看前端的 extent 推断与标量快捷路径：

[copy_op.py:L56-L81](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L56-L81) —— 先校验「两个完整 Buffer 必须同形」，再用 `get_extent` 按 `Buffer`/`BufferRegion`/`BufferLoad` 三种情况推断每维 extent；若两边都是标量 `BufferLoad`（如 `copy(a[i], b[i])`），直接降级成一条 `BufferStore`，等价于 `b[i] = a[i]`。

[copy_op.py:L83-L93](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L83-L93) —— 断言至少一边能推断出 extent；把缺失的一边补成全 1（为了支持广播）；调用 `legalize_pairwise_extents` 从尾部对齐、逐维广播；最后用 `to_buffer_region(..., access_type="r"/"w", extents=...)` 把 src/dst 各自打包成带 extent 的 `tl.region`。

[copy_op.py:L96-L107](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L96-L107) —— 把三个可调旋钮（`coalesced_width`、`disable_tma`、`eviction_policy`）合并进一个 `annotations` 字典（`eviction_policy` 被映射成整数 0/1/2），然后发出本讲最关键的那条 intrin：`tir.call_intrin("handle", Op.get("tl.tileop.copy"), src, dst, annotations=ann)`。注意：和旧实现不同，这些旋钮现在走 `annotations`，位置参数只剩 src/dst 两个 region。

广播对齐的具体规则在工具函数里：

[tilelang/utils/language.py:L406-L449](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/language.py#L406-L449) —— `legalize_pairwise_extents`：先有一个「两边非 1 维数量相等就原样返回」的早退规则（保留逐维迭代映射、不凭空多造轴）；否则从最后一维往前逐对处理，相等则保留、一边为 1 则广播、动态不等则两边都抬到 `tir.max(x, y)` 做安全兜底。

再看后端如何选指令、如何并行：

[src/op/copy.cc:L106-L120](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L106-L120) —— C++ `Copy` 构造：`args[0]`/`args[1]` 就是前端发来的 src/dst region，`NormalizeToBufferRegion` 解析出 `buffer` 与每维 `Range`，`annotations` 直接保留。

[src/op/copy.cc:L688-L719](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L688-L719) —— `GetCopyInst` 是指令选择的「裁判」，严格按 4.1.2 表格的优先级依次试探：先 1D TMA（要求连续且无越界 OOB）、再多维 TMA、再 LDSM/STSM、再 tmem，全都不满足才落到 `kNormal`。`disable_tma_lower`（来自 pass_config）或调用方传 `disable_tma=True` 都会跳过所有 TMA 路径。

[src/op/copy.cc:L510-L543](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L510-L543) —— `CheckBulkLoad` 给出用 TMA bulk load 的四个硬条件：架构支持 bulk copy、src 在 global 且 dst 在 shared/shared.dyn、src 末维 `extent × dtype.bytes()` 是 16 的倍数、src 与 dst dtype 相同。任一不满足就回退 Normal。

[src/op/copy.cc:L281-L325](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L281-L325) —— `MakeSIMTLoop`：Normal 拷贝的并行化核心。它选出所有 extent>1 的维，每维生成一个 `kDataPar` 的 `IterVar`，最终把循环体包成一棵 `ForKind::kParallel` 的嵌套循环——这就是「T.copy 自动并行化」的由来，后面 `LowerParallelLoop` 会把这棵并行循环映射到 threadblock 的线程上。

[src/op/copy.cc:L758-L797](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L758-L797) —— `LowerNormalCopy`：先做 layout 推理（`par_op->InferLayout` 三档：Common/Strict/Free），再用 `LowerParallelLoop` 把并行循环落到线程、分区、向量化与谓词。CPU 目标或涉及 local buffer 时改走 `VectorizeLoop` 直接向量化。

最后看 TMA 的「单线程发起」：

[src/op/copy.cc:L1452-L1458](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L1452-L1458) —— 多维 TMA 拷贝收尾时用 `IfThenElse(EQ(T.thread_var, T.thread_bounds->min), tma_copy)` 包住，即**只有每个 block 的 0 号线程**真正发起 TMA。这与 Normal 拷贝「全员并行」形成鲜明对比，是理解 T.copy 性能行为的关键。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「同一个 `T.copy`，在不同 scope 方向下被编译成完全不同的指令」。

**操作步骤**（示例代码，标注为「示例代码」）：

```python
# 示例代码：对照 global→shared 与 shared→global 两条方向
import torch, tilelang
import tilelang.language as T

@tilelang.jit(out_idx=[1])
def copy_demo(M, K, dtype=T.float16, block=128):
    @T.prim_func
    def main(A: T.Tensor((M, K), dtype),
             C: T.Tensor((M, K), dtype)):
        with T.Kernel(T.ceildiv(M, block), T.ceildiv(K, block), threads=128) as (bx, by):
            A_sh = T.alloc_shared((block, block), dtype)
            # 方向1: global -> shared（Hopper 上应被选为 TMA bulk load）
            T.copy(A[bx * block, by * block], A_sh)
            # 方向2: shared -> global（应被选为 TMA bulk store）
            T.copy(A_sh, C[bx * block, by * block])
    return main

kernel = copy_demo(512, 512)
print(kernel.get_kernel_source())   # 打印生成的 CUDA 源码
```

**需要观察的现象**：

1. 在 Hopper（sm90）上，生成的 CUDA 里 `global→shared` 那段应出现 `cuTensorMapEncode*` + `cp.async.bulk.tensor`（TMA）；`shared→global` 应出现对应的 `cp.async.bulk.tensor.store`。
2. 如果你在 `T.copy(..., disable_tma=True)` 或设了 `tl.disable_tma_lower` pass_config，源码里应**不再有 TMA**，而是普通的一重并行 load-store 循环。
3. 在没有 TMA 的架构（如 Ampere sm80）上，`global→shared` 会落到 `cp.async` 或普通 load；`shared→fragment` 会落到 `ldmatrix`。

**预期结果**：`get_kernel_source()` 输出的 CUDA 里能定位到上述指令；端到端 `kernel(a)` 与 `a` 数值一致。实际生成的具体指令随你的 GPU 架构而变——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把上面示例的 `T.copy(A[bx*block, by*block], A_sh)` 改成 `T.copy(A, A_sh)`（不切片），会发生什么？
**答案**：源 region 变成整张 `A`，extent 变成 `(M, K)`，而 `A_sh` 只有 `(block, block)`。`legalize_pairwise_extents` 无法把 `(512,512)` 广播成 `(128,128)`，形状校验/extent 断言会报错。结论：**`T.copy` 的 src 与 dst 的非 1 维 extent 必须能对上**，切片是表达「搬一个 tile」的正道。

**练习 2**：为什么 TMA bulk 拷贝只由 thread 0 发起，而 Normal 拷贝由所有线程并行执行？
**答案**：TMA 是「单线程构建描述符 + 硬件整块搬运」的机制，多线程重复发起既无意义又浪费；而 Normal 拷贝本质是「每个线程搬若干元素」，必须铺满整个 threadblock 才有足够带宽。

---

### 4.2 T.view / T.reshape：视图与 layout 重解释

#### 4.2.1 概念说明

`T.reshape` 和 `T.view` 都**不搬运数据**。它们返回一个新的 `T.Tensor`，这个新 tensor 与源 buffer 共享**同一段底层 storage**（`src.data`），只是换了形状，或同时换了形状与数据类型。

二者的区别很轻：

- `T.reshape(src, shape)`：只改形状，dtype 沿用源。
- `T.view(src, shape=None, dtype=None)`：shape 和 dtype 都可省略（省略则沿用源），可同时改形状**和**数据类型。

它们共同遵守一条铁律——**总 bit 数守恒**：

\[
\text{bits}_{\text{new}} = \left(\prod_i \text{shape}^{\text{new}}_i\right) \times \text{dtype}^{\text{new}}.\text{bits}
\quad = \quad
\left(\prod_i \text{shape}^{\text{src}}_i\right) \times \text{dtype}^{\text{src}}.\text{bits}
= \text{bits}_{\text{src}}
\]

比如 `(128, 128)` 的 float16（bit 总数 = 128×128×16）可以 view 成 `(128, 64)` 的 float32（128×64×32），bit 数相等，合法；但 view 成 `(100, 100)` 的 float16 就会被断言拒绝。

> ⚠️ **重要约束（务必记住）**：`T.view` / `T.reshape` 返回的 tensor 用的是**连续（row-major）stride**（由 `TensorProxy._construct_strides` 重新构造），它**不支持 stride 化的逻辑转置**。也就是说，你不能用 `T.view` 把 `(M, K)` 看成转置后的 `(K, M)`——那会改变数据排列，而 view 只是换「连续字节怎么切分」的看法。真正的转置要用 `T.copy` 配合交换索引，或用 `T.Parallel` 手写（见 4.2.4 与第 5 节）。

#### 4.2.2 核心流程

`reshape` 与 `view` 的实现极其简洁，只有三步：

1. 用 `bits_product(shape, dtype)` 计算新形状的总 bit 数，与源的 bit 数做 `prim_expr_equal` 比较，不等就断言失败。
2. 调 `T.Tensor(shape, dtype, src.data)`，构造一个**复用 `src.data` 这段 storage** 的新 buffer。
3. 返回这个新 buffer。

其中 `T.Tensor(...)` 走 `TensorProxy.__call__`，它会用 `_construct_strides(shape)` 按 row-major 重新算出 stride，再把 `data`（storage 指针）原样塞进去——所以新 tensor 与源共享同一块显存，没有任何拷贝。

#### 4.2.3 源码精读

[customize.py:L40-L53](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/customize.py#L40-L53) —— `reshape`：用 `bits_product` 校验「新 shape × dtype 的 bit 数」等于「源 shape × dtype 的 bit 数」，然后返回 `T.Tensor(shape, src.dtype, src.data)`。注意第三个参数是 `src.data`——这就是「零拷贝、共享 storage」的关键。

[customize.py:L56-L66](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/customize.py#L56-L66) —— `view`：相比 `reshape` 多了「shape/dtype 可省略（沿用源）」与「允许换 dtype」；同样以 bit 总数守恒做断言，同样返回 `T.Tensor(shape, dtype, src.data)`，同样共享 storage。

[tilelang/utils/language.py:L377-L385](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/language.py#L377-L385) —— `bits_product`：把 shape 各维连乘，再乘 `DataType(dtype).bits`，得到这段 buffer 的总 bit 数。这是 view/reshape 合法性的唯一判据。

[tilelang/language/proxy.py:L143-L154](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/proxy.py#L143-L154) —— `TensorProxy._construct_strides` 与 `__call__`：从 shape 倒着累乘得到 row-major stride，再带着 `data` 构造 tir.Buffer。这段解释了「为什么 view 只能给出连续 stride」——stride 是这里**重新算出来的**，跟源 buffer 的 stride 无关，也不可能表达转置。

一个真实用例（来自仓库示例，view 同时改了 shape 与 dtype，把 shared 里的累加器重解释成另一种排布）：

[examples/dsa_sparse_finetune/sparse_mla_bwd.py:L165-L166](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/dsa_sparse_finetune/sparse_mla_bwd.py#L165-L166) —— `acc_dkv_shared = T.view(KV_shared, shape=[BS // split_store, D], dtype=accum_dtype)`：对同一个 `KV_shared` 这块 shared memory，用新的形状和累加 dtype 重新取一个视图，后续就按这个新视图读写——典型的「同一块 SMEM，多种看法」。

#### 4.2.4 代码实践

**实践目标**：验证「view 共享 storage、零拷贝」，并亲手看清 view **不能**做转置。

**操作步骤**（示例代码）：

```python
# 示例代码：view 共享 storage 验证
import torch, tilelang
import tilelang.language as T

@tilelang.jit(out_idx=[1])
def view_demo(N, dtype=T.float32):
    @T.prim_func
    def main(A: T.Tensor((N,), dtype),
             C: T.Tensor((N,), dtype)):
        with T.Kernel(1, threads=128):
            S = T.alloc_shared((N,), dtype)        # 一段 shared
            T.copy(A, S)                            # 把 A 搬进 S
            # 把同一段 storage 重解释成 (N//2, 2) 的形状
            S2 = T.reshape(S, [N // 2, 2])          # 零拷贝，与 S 共享 data
            # 通过新视图把 S2[0,0] 读出来写回 C[0]
            for i in T.Parallel(N):
                C[i] = S[i]                         # 走原视图
            C[0] = S2[0, 0]                          # 走 reshape 后的视图，值应等于 S[0]
    return main

a = torch.arange(1.0, 513.0, device="cuda").to(torch.float32)
c = view_demo(512)(a)
print("S2[0,0] via view ==", c[0].item(), " expected ==", a[0].item())
```

**需要观察的现象**：

1. `c[0]` 应等于 `a[0]`（即 `1.0`），证明 `S2` 和 `S` 指向同一段 shared memory。
2. 若把 `T.reshape(S, [N//2, 2])` 改成 bit 数不等的形状（如 `[N//3, 3]`），编译期断言会失败。

**验证 view 不支持转置**：试着写 `T.view(S_of_shape_MK, shape=[K, M])` 期望得到转置——你会发现它**只是把连续字节按 `[K, M]` 重新切分**（等价于 reshape），读出来的数值并不是数学上的转置。结论：转置请改用 4.1 的 `T.copy` 配合交换索引，或第 5 节综合实践里的 `T.Parallel` 转置写法。

**预期结果**：`c[0] == a[0]` 成立；非法 reshape 报断言错。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一个 `(64, 64)` 的 float16 buffer，下面哪些 `T.view` 合法？(a) shape=`(128, 32)`, dtype=float16；(b) shape=`(64, 64)`, dtype=float32；(c) shape=`(64, 32)`, dtype=float32。
**答案**：源 bit 数 = 64×64×16 = 65536。(a) 128×32×16 = 65536 ✅；(b) 64×64×32 = 131072 ❌；(c) 64×32×32 = 65536 ✅。所以 (a)(c) 合法，(b) 非法。

**练习 2**：既然 view 共享 storage，那么在 kernel 里通过 view 写入一个元素，原 buffer 能看到吗？
**答案**：能。因为二者底层 `data`（storage 指针）相同，通过任一视图写入都会反映到另一视图。这也是 `sparse_mla_bwd.py` 里用 `T.view` 重解释累加器、随后继续在同一块 SMEM 上读写的前提。

---

### 4.3 T.c2d_im2col：卷积数据布局的特殊搬运

#### 4.3.1 概念说明

卷积（Conv2D）的访存模式很「散」：输出一个像素要用到卷积核覆盖的一小块输入，跨空间位置、跨通道反复取数。直接实现这套寻址既复杂又低效。**im2col**（image to column）是一种经典变换：把每个卷积窗口要用的输入像素**gather（聚拢）**成矩阵的一列，于是卷积就退化成一次普通 GEMM，可以直接用 `T.gemm`。

`T.c2d_im2col` 是 TileLang 提供的「卷积专用搬运」：它在 **Hopper 上借助 TMA 的 im2col 描述符**，把「取卷积窗口 + 搬到 shared」融合成**一次** global→shared 搬运，省掉手写 gather 循环。在非 Hopper 架构上，TMA im2col 不可用，必须退回到手写的 `T.Parallel` gather 循环。

#### 4.3.2 核心流程

前端 `c2d_im2col()` 把卷积参数原样打包，发出 `tl.tileop.c2d_im2col` intrin：

1. 把 `eviction_policy` 映射成整数 0/1/2。
2. 把输入 `img`、输出 `col` 各自打包成读/写 region。
3. 发出 `tir.call_intrin("handle", Op.get("tl.tileop.c2d_im2col"), img_region, col_region, nhw_step, c_step, kernel, stride, dilation, pad, eviction_policy)`。

后端 `Conv2DIm2ColOpNode::Lower` 在 Hopper 上构造一个 `TMAIm2ColDesc`（含全局形状、elem stride、lower/upper corner、smem box 等），发出 `tma_load_im2col`，同样由 thread 0 发起；并对源/目的 scope、维度做了严格断言（src 必须 4D global、dst 必须 2D shared、dtype 一致、目标必须是 Hopper）。

> 关键设计点：`nhw_step` / `c_step` 是「当前要取哪一批 pixel / 哪一段 channel」的步进索引，由 kernel 外层的 tile 循环变量喂入；`kernel/stride/dilation/pad` 是卷积本身的几何参数。c2d_im2col 把这些参数编码进 TMA 描述符，硬件按描述符自动完成 gather。

#### 4.3.3 源码精读

前端：

[copy_op.py:L110-L154](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/copy_op.py#L110-L154) —— `c2d_im2col`：参数为 `(img, col, nhw_step, c_step, kernel, stride, dilation, pad, eviction_policy)`；把 img/col 各自用 `to_buffer_region` 打包成读/写 region，发出 `tl.tileop.c2d_im2col` intrin，共 9 个参数。

后端构造与 lowering：

[src/op/copy.cc:L1564-L1580](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L1564-L1580) —— `Conv2DIm2ColOp` 构造：从 9 个 args 里解出 src/dst region、`nhw_step_`/`c_step_` 以及 `kernel_`/`stride_`/`dilation_`/`padding_`/`eviction_policy_`（后五个强制为整数）。

[src/op/copy.cc:L1589-L1634](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L1589-L1634) —— `Conv2DIm2ColOpNode::Lower` 开头：断言目标必须是 Hopper、src 在 global 且为 4D、dst 在 shared 且为 2D、dtype 一致；随后构造 `TMAIm2ColDesc`，填入全局形状/stride、`elem_stride = {1, stride, stride, 1}`、`lower/upper_corner = {-padding, -padding}`（这就是 padding 在 TMA 层面的表达）、smem box 取自 dst 形状，并按 shared layout 决定 swizzle 模式（32B/64B/128B）。

[src/op/copy.cc:L1661-L1711](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/copy.cc#L1661-L1711) —— 用 `nhw_step_`/`c_step_` 反推出这次要取的全局坐标 `global_coords`（c, w, h, n）与 `image_offset`（w, h），最终发出 `tma_load_im2col`，并由 `IfThenElse(EQ(T.thread_var, T.thread_bounds->min), ...)` 限定只由 thread 0 发起——和普通 TMA bulk 一样是「单线程发起」。

真实示例里 Hopper 与非 Hopper 两条分支的对照：

[examples/convolution/example_convolution.py:L52-L64](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/convolution/example_convolution.py#L52-L64) —— 在 K 维的 `T.Pipelined` 循环里：`is_hopper` 为真时调 `T.c2d_im2col(data, data_shared, by, k_iter, KH, S, D, P)`，一次把卷积窗口聚拢进 `data_shared`；否则走 `T.Parallel(block_M, block_K)` 的手写 gather（逐元素算 `access_h/access_w`、用 `T.if_then_else(in_bound, ..., 0)` 处理越界补零）。两种走法后面都接 `T.copy(kernel_flat[...], kernel_shared)` + `T.gemm(...)`，说明 im2col 的目的就是「把卷积变成 GEMM」。

同文件里还有 `T.copy` 切片语法的活样例：

[examples/convolution/example_convolution.py:L63](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/convolution/example_convolution.py#L63) —— `T.copy(kernel_flat[k_iter * block_K, bx * block_N], kernel_shared)`：源是一个二维切片 `BufferLoad`，`copy` 据此推断出要搬 `(block_K, block_N)` 这么大一块进 `kernel_shared`，正好对应 4.1 讲的「按切片推断 extent」。

而 [examples/convolution/example_convolution.py:L48-L49](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/convolution/example_convolution.py#L48-L49) 里 `kernel_flat = T.Tensor((KH*KW*C, F), dtype, kernel.data)` 则是 4.2 讲的「零拷贝重解释」的活用法：把 4D 卷积核 `(KH, KW, C, F)` 在**同一段 storage** 上看成 2D `(KH*KW*C, F)`，这样后续 GEMM 就能直接用。

#### 4.3.4 代码实践

**实践目标**：读懂两条分支，理解 c2d_im2col 把 gather 融进搬运的价值。

**操作步骤**：

1. 打开 `examples/convolution/example_convolution.py`，定位 `is_hopper = check_hopper()` 与 `T.Pipelined` 内的两条分支（`:L52-L64`）。
2. 在 Hopper GPU 上运行 `python examples/convolution/example_convolution.py`，应打印 `All checks passed.✅`。
3. 强制走非 Hopper 分支：把 `is_hopper = check_hopper()` 临时改成 `is_hopper = False`（**仅用于本地观察，勿提交**），再运行，确认手写 gather 分支同样能通过校验。
4. 用 `kernel.get_kernel_source()` 对比两条分支生成的 CUDA：Hopper 分支应含 `tma_load_im2col` 相关指令；手写分支应是一段带越界判断的 load 循环。

**需要观察的现象**：两条分支数学结果一致（都通过 `torch.testing.assert_close`），但 Hopper 分支生成的指令更短、更「整块」，手写分支则有明显的逐元素寻址与补零逻辑。

**预期结果**：两次都打印 `All checks passed.✅`；指令差异如上。具体性能数字**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `c2d_im2col` 的 `Lower` 开头要 `ICHECK(TargetIsHopper(T.target))`？
**答案**：因为它底层用的是 Hopper TMA 的 im2col 描述符（`tma_load_im2col` / `create_tma_im2col_descriptor`），这是 Hopper 才有的硬件能力；非 Hopper 架构没有这条指令，所以示例里用 `is_hopper` 判断后退回手写 gather。

**练习 2**：`T.c2d_im2col` 和 `T.copy` 在「取数据」这件事上最大的区别是什么？
**答案**：`T.copy` 是**规则矩形**的整块搬运（src/dst region 形状一致）；`T.c2d_im2col` 则在搬运的同时做**卷积窗口 gather**——它按卷积的 stride/dilation/pad 把分散的输入像素聚拢成 shared 里的一列，这是普通 `T.copy` 表达不了的访存模式，所以才需要一条专用原语。

---

## 5. 综合实践

把本讲三个模块串起来：**用 `T.copy` 分块加载、用 `T.view` 零拷贝重解释、用 `T.copy` + 交换索引实现真正的转置**，并和 PyTorch 对照验证。

**任务**：写一个 kernel，把 `(M, K)` 矩阵 `A` 的每个 `(block_M, block_K)` tile 加载到 shared，然后写出它的转置 tile 到输出 `C`（形状 `(K, M)`）；途中用 `T.reshape` 把 shared tile 重解释，验证它共享 storage。

```python
# 示例代码（综合实践）
import torch, tilelang
import tilelang.language as T

@tilelang.jit(out_idx=[1])
def transpose_kernel(M, K, block_M=128, block_K=64, dtype=T.float16):
    @T.prim_func
    def main(A: T.Tensor((M, K), dtype),
             C: T.Tensor((K, M), dtype)):           # 输出是转置后形状
        # grid: 沿 A 的 (M,K) 切 tile；每个 block 产出一个转置 tile
        with T.Kernel(T.ceildiv(M, block_M), T.ceildiv(K, block_K), threads=128) as (bm, bk):
            A_sh = T.alloc_shared((block_M, block_K), dtype)

            # 模块1: T.copy 切片加载 global -> shared
            T.copy(A[bm * block_M, bk * block_K], A_sh)

            # 模块2: T.reshape 零拷贝重解释（bit 总数守恒）
            #   把 (block_M, block_K) 看成 (block_M//2, 2, block_K)，共享同一 storage
            A_view = T.reshape(A_sh, [block_M // 2, 2, block_K])

            # 模块3: 真正的转置——shared -> shared 的转置写（T.Parallel 交换索引）
            AT_sh = T.alloc_shared((block_K, block_M), dtype)
            for i, j in T.Parallel(block_M, block_K):
                AT_sh[j, i] = A_sh[i, j]

            # 再用 T.copy 把转置后的 shared tile 写回 global（shared -> global）
            #   注意 C 的对应 tile 起点：(bk*block_K, bm*block_M)
            T.copy(AT_sh, C[bk * block_K, bm * block_M])
    return main

M, K = 512, 256
a = torch.randn(M, K, device="cuda", dtype=torch.float16)
c = transpose_kernel(M, K)(a)                         # 待本地验证
ref = a.t().contiguous()
torch.testing.assert_close(c, ref, rtol=1e-3, atol=1e-3)
print("transpose check passed ✅")

# 额外观察：看生成的指令
src = transpose_kernel(M, K).get_kernel_source()
print("TMA load found:", "cp.async.bulk" in src or "TensorMap" in src or "tma" in src.lower())
```

**完成检查清单**：

- [ ] 数值上 `c` 等于 `a.t()`（验证转置正确）。
- [ ] 能解释为什么转置必须用 `T.Parallel` 交换索引（或 `T.copy` 配合交换索引），而**不能**用 `T.view(A_sh, [block_K, block_M])`——因为后者只是把连续字节按新形状切分，不是数学转置。
- [ ] 能解释 `T.reshape` 那一行**没有搬运任何数据**，`A_view` 与 `A_sh` 共享同一段 shared memory。
- [ ] `get_kernel_source()` 里能定位到 global→shared 的加载指令、shared→global 的写回指令。

> 数值与具体生成指令随 GPU 架构而变，**待本地验证**。若手头无 GPU，至少完成「源码阅读型实践」：跟踪 `T.copy(A[...], A_sh)` 这一行，在 `copy_op.py`（前端 region 构造）→ `src/op/copy.cc:GetCopyInst`（指令选择）→ `Lower`（PTX 生成）之间画出调用链。

## 6. 本讲小结

- `T.copy(src, dst)` 是唯一的通用 tile 搬运原语：**按 src/dst 的形状或切片自动推断 extent**，标量对会降级成单条 store，形状不齐时按尾部对齐做广播。
- 它前端只发出 `tl.tileop.copy` intrin，**真正的指令在 C++ `GetCopyInst` 里按 scope 组合 + 硬件能力挑选**，优先级是 1D-TMA → 多维 TMA → LDSM/STSM → tmem → Normal。
- **Normal 拷贝全员并行**（`MakeSIMTLoop` 生成 `kParallel` 循环再映射到线程），**TMA 拷贝只由 thread 0 发起**；`disable_tma` / pass_config 可强制回退到 Normal。
- `T.view` / `T.reshape` 是**零拷贝**重解释：复用 `src.data`，只换形状（reshape）或形状+dtype（view），唯一约束是 **bit 总数守恒**；它们只构造**连续 stride**，**不支持转置视图**。
- `T.c2d_im2col` 是卷积专用搬运：在 Hopper 上把「取卷积窗口 + 搬到 shared」融合成一次 TMA im2col（`tma_load_im2col`，thread 0 发起），把卷积变成 GEMM；非 Hopper 退回手写 `T.Parallel` gather。

## 7. 下一步学习建议

- **向编译流水线深入**：本讲反复出现的「前端发 intrin → C++ lowering 选指令」模式，将在 u3-l3（LowerAndLegalize）里作为完整 pass 链展开，建议接着读 `LowerTileOp` 如何调用本讲的 `CopyNode::Lower`。
- **向 layout 推理深入**：4.1 里 `LowerNormalCopy` 调的 `InferLayout`、TMA 路径里决定 swizzle（32B/64B/128B）的逻辑，属于 u4-l1（Layout 推理机制）的内容，读 `src/op/copy.cc:InferLayout` 与 `tilelang/transform/layout_inference.cc` 会豁然开朗。
- **向异步与流水深入**：T.copy 在 `T.Pipelined` 里作为「生产者」与 `T.gemm`（消费者）重叠，是 u4-l2（软件流水线）的核心戏码，建议结合 `quickstart.py` 的 K 维循环体会「copy 隐藏在 gemm 背后」的设计。
