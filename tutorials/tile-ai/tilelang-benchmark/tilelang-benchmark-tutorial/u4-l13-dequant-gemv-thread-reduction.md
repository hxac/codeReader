# 反量化 GEMV 内核：线程级外积规约

## 1. 本讲目标

本讲解剖 TileLang 的 **反量化 GEMV（dequantize_gemv）内核**。读完本讲后，你应当能够：

1. 说清楚 **GEMV（M=1）为什么不用 TensorCore，而改用「线程级外积-规约」**，并能从算术强度（arithmetic intensity）角度解释。
2. 掌握 TileLang 的四个线程级原语：`T.alloc_local`（寄存器缓冲）、`T.thread_binding`（线程绑定）、`T.serial` / `T.vectorized`（串行 / 向量化循环）、`T.tvm_thread_allreduce`（跨线程归约）。
3. 能推导 `micro_size_k`、`micro_size_k_compressed`、`block_K` 这三个关键尺寸是怎么由「128 位访存事务」和「比特打包」算出来的。
4. 能在内核里标出 `(reduce_thread, n_partition)` 这两个线程维度分别负责哪段循环，并说清 `tvm_thread_allreduce` 的输入输出。
5. 理解这套「外积-规约」调度与 u3-l9 块级 GEMM（`T.gemm` + TensorCore）的本质区别。

本讲聚焦的源码文件只有一份：

- `hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py`

它是 W4A16（权重 4-bit、激活 16-bit）反量化 GEMV 的 TileLang 实现，结构上与同一目录下的 `benchmark_tilelang_matmul_fp16xfp4.py`（块级 TensorCore GEMM，u3-l9 风格）形成鲜明对照，后者会作为对比对象出现。

## 2. 前置知识

本讲建立在 u3-l9（块级 GEMM 五要素）与 u4-l12（多精度 GEMM）之上。这里用通俗语言补齐几个新概念。

**GEMM vs GEMV（算术强度）。** 矩阵乘 \(C = A \times B\)，当 \(M\)（A 的行数）很大时是 GEMM，每搬运一个元素能反复参与多次乘加，**算力受限（compute-bound）**，适合用 TensorCore 的矩阵乘加指令（MMA）一次算一整块。当 \(M=1\) 时退化为「矩阵-向量乘」（GEMV），每个输出元素 \(C[0, j]\) 只是 \(A\) 的一行和 \(B\) 的一列做点积，每个权重元素只参与一次乘加，**带宽受限（bandwidth-bound）**。此时用 TensorCore 会严重浪费——例如 `m16n8k16` 的 MMA 需要 A 提供 16 行，M=1 只填得满 1 行，利用率仅 1/16。

**TensorCore 的形状约束。** TileLang 的 `T.gemm` 在 NVIDIA 上最终会落到 wmma/MMA 指令，它们要求输出瓦片至少是 \(16\times8\)（fp16）这一级别。GEMV 的输出只有 1 行，硬塞进 MMA 既浪费算力也省不下带宽。

**CUDA 的线程层次。** grid → block → warp（32 线程）→ thread。本讲内核把一个 block 切成两个维度：`threadIdx.x` 上的一组线程合作算一个输出元素，`threadIdx.y` 上的若干组并行算相邻的多个输出元素。

**Warp shuffle / allreduce。** 同一 warp 内的 32 个线程可以通过 warp shuffle 指令直接交换寄存器值，做一次「全归约（allreduce）」只需少数几条指令、不必经过共享内存。这正是 GEMV 跨线程求和的高效手段。

**W4A16 比特打包。** 权重用 4-bit 存储、两个 4-bit 元素打包进 1 个 int8 字节（`num_elems_per_byte = 8/4 = 2`）；激活用 fp16。计算时先把权重「反量化」回 16-bit 再做点积。打包细节属于 u4-l14 的内容，本讲只需要知道「B 在显存里是压缩的，加载到寄存器后要解包」即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [benchmark_tilelang_matmul_fp16xint4.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py) | **本讲主角**。`dequantize_gemv` 内核 + 调优驱动 `main()`。线程级外积-规约。 |
| [benchmark_tilelang_matmul.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh) | 驱动脚本。可见所有 shape 的 \(M=1\)（GEMV 特征）。 |
| benchmark_tilelang_matmul_fp16xfp4.py（同目录） | 对照：块级 TensorCore GEMM（`alloc_shared/fragment` + `T.gemm` + `T.Pipelined`），与本文线程级写法相反。 |

> 一句话区分：**`fp16xint4.py` 是线程级 GEMV（本讲），`fp16xfp4.py` 是块级 GEMM（u3-l9 风格）**。两者同处 `dequantize_matmul` 目录，是 TileLang「同一算子、两种调度」的最佳对照。

## 4. 核心概念与源码讲解

### 4.1 GEMV 为何不用 TensorCore：外积-规约思想

#### 4.1.1 概念说明

u3-l9 的块级 GEMM 把输出切成 \(block_M\times block_N\) 的瓦片，每个 block 用 `T.gemm` 调 TensorCore 一次算一整块。这套打法的前提是 **输出是「二维瓦片」**，能填满 MMA 的 \(m\times n\) 形状。

GEMV（\(M=1\)）的输出只有一行，瓦片退化成 \(1\times block_N\)。此时：

- **算力角度**：MMA 的 \(m\) 维（如 16）严重浪费，TensorCore 利用率极低。
- **带宽角度**：GEMV 的瓶颈是把巨量的权重 \(B\) 从显存搬进来，而不是算得不够快。把功夫花在「最大化每次访存事务的吞吐」上更划算。

于是 TileLang 采用了另一套调度——**「外积-规约」（outer reduction）**：

- 把每个输出元素 \(C[0, j]\) 所需的 **K 维点积分摊给一组线程**（本讲里是 `reduce_thread` 个线程），每个线程只算 K 的一段，得到一个「部分和」。
- 再用一次 **跨线程归约（allreduce）** 把所有部分和加起来，得到完整点积。
- 与此同时，另一组线程维度 `n_partition` 横向并行，同时算相邻的多个输出列。

这正是代码注释里点名的来源——`bitblas.gpu.gemv.GEMV.sch_outer_reduction_with_config`（见 [benchmark_tilelang_matmul_fp16xint4.py:29-32](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L29-L32)），即 BitBLAS 的 GEMV 外积-规约调度。

#### 4.1.2 核心流程

对单个输出元素 \(C[by, j]\)（其中 \(j = bx\cdot\text{n\_partition} + ni\)），其数学定义为：

\[
C[by, j] = \sum_{t=0}^{K-1} A[by, t]\cdot \tilde{B}[j, t]
\]

其中 \(\tilde{B}\) 是把 4-bit 压缩权重 \(B\) 解包后的 fp16 权重。把长度 \(K\) 的求和按「每线程 `micro_size_k` 个、每 ko 步 `block_K` 个」切成若干段，分给 `reduce_thread` 个线程：

\[
p_{kr} = \sum_{ko}\sum_{v=0}^{\text{micro\_size\_k}-1} A[by,\; ko\cdot\text{block\_K}+kr\cdot\text{micro\_size\_k}+v]\cdot \tilde{B}[j,\; ko\cdot\text{block\_K}+kr\cdot\text{micro\_size\_k}+v]
\]

单个线程 \(kr\) 在寄存器里累加得到部分和 \(p_{kr}\)，最后：

\[
C[by, j] = \sum_{kr=0}^{\text{reduce\_thread}-1} p_{kr}
\]

这一步求和由 `tvm_thread_allreduce` 完成。整体流程伪代码：

```
grid: (ceildiv(N, n_partition), M)
block 线程: (reduce_thread, n_partition)   # threadIdx.x = kr, threadIdx.y = ni

每个 block:
    每个 (kr, ni) 线程:
        accum_res = 0
        for ko in range(ceildiv(K, block_K)):        # 串行遍历 K 的块
            A_local[..]  = 向量化加载 A 的一段        # kr 负责的那 micro_size_k 个
            Bq_local[..] = 向量化加载 B 的压缩段
            Bd_local[..] = 解包 Bq_local              # 反量化
            accum_res += sum(A_local * Bd_local)      # 标量乘加
        # 此时 accum_res 是本线程的部分和 p_kr
        reduced = allreduce(sum, accum_res)            # 跨 reduce_thread 个线程求和
        if kr == 0:
            C[by, j] = reduced                         # 只让一个线程回写，避免竞争
```

#### 4.1.3 源码精读

内核入口与线程块拓扑，注意 `threads=(reduce_thread, n_partition)` 这一行——它把线程块定义成二维，是「外积-规约」的根：

```python
with T.Kernel(
        T.ceildiv(N, n_partition),
        M,
        threads=(reduce_thread, n_partition),
) as (
        bx,
        by,
):
```

> [benchmark_tilelang_matmul_fp16xint4.py:82-89](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L82-L89)：网格 `(ceildiv(N, n_partition), M)` 负责覆盖输出；线程块二维 `(reduce_thread, n_partition)` 把「K 规约」与「N 并行」分给两个硬件线程轴。这与块级 GEMM「threads=thread_num（一维）、T.gemm 做块乘」形成根本区别。

回写处的 `if kr == 0` 守卫——allreduce 之后只有一个线程动手写全局内存：

> [benchmark_tilelang_matmul_fp16xint4.py:152-153](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L152-L153)：`if kr == 0: C[by, bx * n_partition + ni] = reduced_accum_res[0]`。注意列号由 `ni`（threadIdx.y）决定，行号由 `by` 决定；同一个 `(bx, ni)` 下有 `reduce_thread` 个 `kr` 线程合作，必须只让其中一个回写。

#### 4.1.4 代码实践

**实践目标**：在内核里把「两条线程轴」与「它们各自负责的循环/索引」对应起来。

**操作步骤**（源码阅读型实践，不修改源码）：

1. 打开 [benchmark_tilelang_matmul_fp16xint4.py:82-153](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L82-L153)。
2. 找到 `threads=(reduce_thread, n_partition)` 与 `kr = T.thread_binding(..., "threadIdx.x")`、`ni = T.thread_binding(..., "threadIdx.y")`。
3. 在打印或笔记里画一张「线程轴 → 职责」表（见下方预期结果）。

**需要观察的现象 / 预期结果**：

| 线程轴 | 变量 | 职责 |
| --- | --- | --- |
| `threadIdx.x`（0..reduce_thread-1） | `kr` | 切分 **K 维规约**：`kr` 决定加载 A/B 的哪一段，并贡献部分和 `accum_res` |
| `threadIdx.y`（0..n_partition-1） | `ni` | 并行 **N 维输出列**：`ni` 决定当前线程算的是哪一列 \(j = bx\cdot\text{n\_partition}+ni\) |

`bx`（blockIdx.x）覆盖 N 的「大块」，`by`（blockIdx.y）覆盖 M（GEMV 下 M=1，故 by 恒为 0）。

#### 4.1.5 小练习与答案

**练习 1**：本内核的 `M=1`（见驱动脚本里所有 shape 的 `m=1`）。如果把同样的内核直接拿去跑 \(M=128\) 的 GEMM，会出现什么效率问题？

**参考答案**：网格的 `by` 维 = M，会变成 128 个 block 各算一行；但每个 block 仍然用线程级标量乘加，**完全没有利用 TensorCore**。对 \(M=128\) 这种 GEMM，算力受限，正确做法是改用块级 `T.gemm`（如 `fp16xfp4.py` 那份内核）。本内核是「为带宽瓶颈的 GEMV 量身定做」的，规模一放大就不合适。

**练习 2**：`tvm_thread_allreduce` 之后为什么要 `if kr == 0` 才写 C？如果去掉这个判断会怎样？

**参考答案**：同一个输出元素 \(C[by, j]\) 由 `reduce_thread` 个 `kr` 线程合作计算，allreduce 后它们都拿到了相同的最终和。若所有 `kr` 都写，会产生 `reduce_thread` 个线程写同一地址的**写竞争（race）**；虽然值相同，但行为未定义且低效。`if kr == 0` 保证每个输出元素只被写一次。

---

### 4.2 alloc_local：寄存器级缓冲与 micro_size_k 推导

#### 4.2.1 概念说明

TileLang 有三种片上存储分配原语，理解它们的区别是看懂本内核的前提：

| 原语 | 存储层级 | 作用域 | 本讲内核是否用到 |
| --- | --- | --- | --- |
| `T.alloc_shared` | 共享内存（shared memory） | 整个 block 共享 | 否（块级 GEMM 才用） |
| `T.alloc_fragment` | 寄存器（per-warp fragment） | 一个 warp 内分布 | 否（块级 GEMM 才用） |
| `T.alloc_local` | 寄存器（per-thread） | **每个线程私有** | **是（本讲核心）** |

`T.alloc_local((shape,), dtype)` 为**每个线程**分配一份私有的寄存器缓冲。这正是线程级调度的标志：每个线程用自己的寄存器装「一小段 A」「一小段压缩 B」「解包后的 B」「一个标量累加器」，线程之间不共享这些数据，只在最后 allreduce 时交换累加结果。

#### 4.2.2 核心流程

线程级 GEMV 要回答一个关键设计问题：**每个线程一次处理多少个 K 元素？** 这由「128 位访存事务」推导而来。GPU 上一次高效的访存事务（transaction）搬运 128 位，因此希望每个线程一次加载恰好 128 位的数据，把带宽吃满：

\[
\text{micro\_size\_k} = \frac{\text{MAX\_TRANSACTION\_SIZE\_IN\_BITS}}{\text{bits}(\text{in\_dtype})} = \frac{128}{16} = 8
\]

即每个线程一次加载 8 个 fp16（\(8\times16=128\) 位）的 A。权重 B 是压缩的，每字节存 `num_elems_per_byte` 个元素：

\[
\text{num\_elems\_per\_byte} = \frac{\text{storage\_nbit}}{\text{num\_bits}} = \frac{8}{4} = 2
\]

所以同样 8 个 K 元素的压缩权重只占 4 个字节：

\[
\text{micro\_size\_k\_compressed} = \frac{\text{micro\_size\_k}}{\text{num\_elems\_per\_byte}} = \frac{8}{2} = 4
\]

最后，`reduce_thread` 个线程每人扛 `micro_size_k` 个元素，合起来一个 block 在一次 ko 步内覆盖的 K 长度：

\[
\text{block\_K} = \text{reduce\_thread}\times \text{micro\_size\_k} = 32 \times 8 = 256
\]

#### 4.2.3 源码精读

尺寸推导全集中在内核函数开头：

```python
storage_type = "".join(c for c in storage_dtype if not c.isdigit())   # "int8" -> "int"
storage_nbit = int("".join(c for c in storage_dtype if c.isdigit()))  # "int8" -> 8
num_elems_per_byte = storage_nbit // num_bits                          # 8 // 4 = 2

MAX_TRANSACTION_SIZE_IN_BITS = 128
micro_size_k = MAX_TRANSACTION_SIZE_IN_BITS // DataType(in_dtype).bits  # 128 // 16 = 8
micro_size_k_compressed = micro_size_k // num_elems_per_byte            # 8 // 2 = 4
block_K = reduce_thread * micro_size_k                                 # 32 * 8 = 256
```

> [benchmark_tilelang_matmul_fp16xint4.py:36-43](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L36-L43)：把字符串型 `storage_dtype="int8"` 拆成类型 `int` 与位宽 `8`；再由 128 位事务推导每个线程处理的 K 元素数与压缩字节数。

五个 per-thread 寄存器缓冲：

```python
A_local = T.alloc_local((micro_size_k,), in_dtype)                    # 8 个 fp16：A 的一段
B_quant_local = T.alloc_local([micro_size_k_compressed], storage_dtype)  # 4 个 int8：压缩 B
B_dequantize_local = T.alloc_local([micro_size_k], in_dtype)           # 8 个 fp16：解包后的 B
accum_res = T.alloc_local((1,), accum_dtype)                           # 标量累加器
reduced_accum_res = T.alloc_local((1,), accum_dtype)                   # allreduce 结果
```

> [benchmark_tilelang_matmul_fp16xint4.py:90-94](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L90-L94)：注意它们都是 `alloc_local`——**每个线程各有一份**。`A_local`/`B_quant_local`/`B_dequantize_local` 体积由 4.2.2 的公式决定；`accum_res` 与 `reduced_accum_res` 都是单标量，分别是 allreduce 的输入与输出。

#### 4.2.4 代码实践

**实践目标**：亲手用公式推一遍尺寸，验证「以代码为准」。

**操作步骤**：

1. 假设把 `in_dtype` 从 `float16` 改为 `int8`（其余不变），手算新的 `micro_size_k`、`micro_size_k_compressed`、`block_K`（取 `reduce_thread=32`）。
2. 与 4.2.3 的源码逻辑对照，确认你的推导口径。

**需要观察的现象 / 预期结果**（待本地验证公式，不依赖运行）：

- `micro_size_k = 128 // 8 = 16`（int8 每元素 8 位，128/8=16）。
- 若 `num_bits` 仍为 4，`num_elems_per_byte = 2`，`micro_size_k_compressed = 16 // 2 = 8`。
- `block_K = 32 * 16 = 512`。
- 结论：**降低元素位宽 → 每线程一次能搬更多元素 → block_K 更大 → ko 循环更少**，这正是低精度对带宽受限算子更友好的体现。

#### 4.2.5 小练习与答案

**练习 1**：`accum_res` 与 `reduced_accum_res` 都是 `(1,)` 的标量缓冲，为什么必须分成两个、不能复用同一个？

**参考答案**：`accum_res[0]` 是 **allreduce 的输入**（每个线程的私有部分和），`reduced_accum_res[0]` 是 **allreduce 的输出**（归约后的完整和）。TVM 的 `tvm_thread_allreduce` 要求输入、输出是不同的缓冲（输出由 runtime 在归约后写入）。若复用，归约过程会破坏尚未消费的输入，语义不安全。

**练习 2**：本内核为什么用 `alloc_local` 而不用 `alloc_shared` 来放 `A_local`？

**参考答案**：`A_local` 是每个线程私有的「自己那段 A」，线程间不共享，放寄存器（local）访问最快、零 bank-conflict。共享内存（shared）适合 block 内多线程共用同一份数据（如块级 GEMM 的 `A_shared` 被 warp 共享）。GEMV 每个线程的 A 段互不重叠，没必要也用不着 shared。

---

### 4.3 thread_binding + T.serial/T.vectorized：线程映射与向量化加载

#### 4.3.1 概念说明

`T.thread_binding(start, end, thread="threadIdx.x")` 把一个「逻辑循环变量」直接绑定到一个**硬件线程轴**。它不是普通循环——它声明「这个变量的取值由硬件线程索引提供，不要再生成 for 循环」。本内核用两次绑定，把 `kr` 绑到 `threadIdx.x`、`ni` 绑到 `threadIdx.y`，与 4.1 节的 `threads=(reduce_thread, n_partition)` 一一对应：

```python
kr = T.thread_binding(0, reduce_thread, thread="threadIdx.x")
ni = T.thread_binding(0, n_partition,  thread="threadIdx.y")
```

绑定之后，`kr` 与 `ni` 在内核里就是「当前线程是谁」，可直接用来索引数据。

循环标注原语有两个：

- `T.serial(extent)`：**串行**循环，迭代按顺序执行，无并行承诺。本内核里 ko（K 的大块）、ki（标量乘加）、解包都用 `serial`。
- `T.vectorized(extent)`：**向量化**循环，迭代被映射到向量通道，编译器可生成向量化加载/存储指令，一次搬运多个元素。本内核用它加载 A、B 的连续段。

> 区别于 u3-l9 的 `T.Parallel`（把循环分给 warp 内线程）和 `T.Pipelined`（软件流水）。本讲是「线程级」调度，主力是 `thread_binding` + `serial/vectorized`，**没有** `T.gemm`、`T.Pipelined`、`T.copy`。

#### 4.3.2 核心流程

K 维主循环的每一 ko 步，每个线程做四件事：

```
for ko in T.serial(ceildiv(K, block_K)):            # 串行遍历 K 的 256-块
    # 1) 向量化加载 A 的一段（128 位，8 个 fp16）
    for v in T.vectorized(micro_size_k):
        A_local[v] = A[by, ko*block_K + kr*micro_size_k + v]

    # 2) 向量化加载 B 的压缩段（4 个 int8）
    for v in T.vectorized(micro_size_k_compressed):
        B_quant_local[v] = B[bx*n_partition + ni, ko*(reduce_thread*micro_size_k_compressed) + kr*micro_size_k_compressed + v]

    # 3) 反量化（fast_decoding 走 lop3；否则标量解包，见 u4-l14）
    ...

    # 4) 标量乘加累加
    for ki in T.serial(micro_size_k):
        accum_res[0] += A_local[ki] * B_dequantize_local[ki]
```

关键索引对应：A 的 K 下标 `ko*block_K + kr*micro_size_k + v` 与解包后 B 的逻辑 K 下标完全一致（见 4.3.4 的验证），保证点积对齐。

#### 4.3.3 源码精读

线程绑定：

> [benchmark_tilelang_matmul_fp16xint4.py:96-97](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L96-L97)：`kr` 绑 `threadIdx.x`（K 规约轴），`ni` 绑 `threadIdx.y`（N 输出列轴）。绑定后整个内核不再出现「for kr / for ni」的循环——线程索引直接由硬件给出。

向量化加载 A：

```python
T.clear(accum_res)
for ko in T.serial(T.ceildiv(K, block_K)):
    for v in T.vectorized(micro_size_k):
        A_local[v] = A[by, ko * block_K + kr * micro_size_k + v]
```

> [benchmark_tilelang_matmul_fp16xint4.py:101-104](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L101-L104)：`T.clear` 先把累加器清零；`T.serial` 串行走 ko；`T.vectorized(micro_size_k)` 把 8 次 A 读取向量化成一条 128 位加载。注意 `kr` 决定本线程读 K 的哪 8 个元素。

向量化加载压缩 B 与标量乘加：

```python
for v in T.vectorized(micro_size_k_compressed):
    B_quant_local[v] = B[bx * n_partition + ni, ko * (reduce_thread * micro_size_k_compressed)
                         + kr * micro_size_k_compressed + v]
...
for ki in T.serial(micro_size_k):
    accum_res[0] += A_local[ki] * B_dequantize_local[ki]
```

> [benchmark_tilelang_matmul_fp16xint4.py:106-111](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L106-L111) 是压缩 B 的向量化加载（行号 = 输出列 `bx*n_partition+ni`，列号 = `ko*128 + kr*4 + v` 字节偏移）。
>
> [benchmark_tilelang_matmul_fp16xint4.py:134-136](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L134-L136) 是标量乘加（fp16 路径，`use_dp4a=False`）。注意此处是裸的 `+= A_local[ki] * B_dequantize_local[ki]`，**没有 `T.gemm`、没有 TensorCore**——这正是线程级调度的标志。

#### 4.3.4 代码实践

**实践目标**：验证 A 与解包后 B 的 K 下标确实对齐（点积合法）。

**操作步骤**：

1. 取默认参数 `num_bits=4, storage_nbit=8`，故 `num_elems_per_byte=2`。
2. 对任一 `v ∈ [0,8)`，写出 `A_local[v]` 对应的 K 下标。
3. 写出 `B_dequantize_local[v]` 来自哪个字节、字节内的第几位，再换算回逻辑 K 下标。
4. 对照两者是否相等。

**需要观察的现象 / 预期结果**（待本地验证）：

- `A_local[v]` 的 K 下标：\(ko\cdot256 + kr\cdot8 + v\)。
- `B_dequantize_local[v]` 来自字节 `B_quant_local[v // 2]`（即 `ki = v` 时 `_tir_packed_int_to_int_convert(...)(num_bits, B_quant_local[ki // num_elems_per_byte], ki % num_elems_per_byte, ...)`，见 [benchmark_tilelang_matmul_fp16xint4.py:120-125](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L120-L125)）；该字节在 B 中的列偏移为 \(ko\cdot128 + kr\cdot4 + (v//2)\)，每字节含 2 个元素，故逻辑元素下标 \(= 2\cdot(ko\cdot128 + kr\cdot4 + v//2) + (v\bmod 2) = ko\cdot256 + kr\cdot8 + v\)。
- 两者相等 → 点积对齐 ✓。

#### 4.3.5 小练习与答案

**练习 1**：加载 A 用 `T.vectorized`，而乘加用 `T.serial`。为什么乘加不能也用 `T.vectorized`？

**参考答案**：`T.vectorized` 适合「相互独立的若干次访存」，编译器把它编成向量化 load/store 指令。但这里的乘加是「向同一个标量累加器 `accum_res[0]` 归约」——8 次乘加之间存在数据依赖（写后读同一个累加器），本质是**归约**而非独立并行，用 `serial` 让编译器生成顺序的 FMA 指令更稳妥（也便于后续 dp4a 替换）。

**练习 2**：把 `T.thread_binding` 换成普通的 `for kr in T.serial(reduce_thread)` 会怎样？

**参考答案**：那样 `kr` 就只是「每个线程串行执行 reduce_thread 次的循环变量」，即**一个线程**串行做完整个 K 规约，完全失去并行。`thread_binding` 的语义是「`kr` 的取值 = 硬件线程索引」，从而让 `reduce_thread` 个线程**各取一值、并行**执行循环体。这是「线程级并行」的关键开关。

---

### 4.4 tvm_thread_allreduce：跨线程归约与单点回写

#### 4.4.1 概念说明

每个线程在 ko 循环结束后，`accum_res[0]` 里只是**自己那段 K 的部分和** \(p_{kr}\)。要得到完整点积，必须把 `reduce_thread` 个线程的部分和加起来——这就是 `T.tvm_thread_allreduce` 干的事。

它底层会根据线程数选择实现：当 `reduce_thread=32`（恰好一个 warp）时用 **warp shuffle**（最快，不走共享内存）；当是 64（两个 warp）时需要跨 warp 归约，可能借助共享内存。这也是 `get_configs` 把 `reduce_thread` 限制在 `[16, 32, 64]`（半 warp / 一个 warp / 两个 warp）的原因之一——落在这些「规约友好」的尺寸上，allreduce 才高效。

配合它的还有 `T.comm_reducer`：它声明「用什么二元运算、用什么单位元」做归约。本内核用加法和 0：

```python
T.comm_reducer(lambda x, y: x + y, [T.Cast(accum_dtype, 0)])
```

即「归约运算 = \(x+y\)，单位元 = 0」。把它作为 `reduce_scope` 属性挂上去，告诉 allreduce 这是求和。

#### 4.4.2 核心流程

```
with T.attr(comm_reducer(+, 0), "reduce_scope", ...):    # 声明「这是求和归约」
    T.evaluate(
        T.tvm_thread_allreduce(
            size         = 1,                  # 每线程归约 1 个元素
            source       = accum_res[0],       # 输入：本线程的部分和 p_kr
            allreduce    = True,               # 归约后把结果广播给参与线程
            dest         = reduced_accum_res[0],# 输出：归约后的完整和
            reduce_idx   = kr,                 # 沿 kr（threadIdx.x）轴归约
        )
    )
if kr == 0:
    C[by, j] = reduced_accum_res[0]            # 只让一个线程回写
```

数学上：`reduced_accum_res[0]` = \(\sum_{kr} p_{kr}\) = \(C[by, j]\)。

#### 4.4.3 源码精读

完整的归约块：

```python
with T.attr(
        T.comm_reducer(lambda x, y: x + y, [T.Cast(accum_dtype, 0)]),
        "reduce_scope",
        T.reinterpret(T.uint64(0), dtype="handle"),
):
    T.evaluate(
        T.tvm_thread_allreduce(
            T.uint32(1),
            accum_res[0],
            True,
            reduced_accum_res[0],
            kr,
            dtype="handle",
        ))
```

> [benchmark_tilelang_matmul_fp16xint4.py:138-151](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L138-L151)：`tvm_thread_allreduce` 的五个位置实参依次是：
>
> 1. `T.uint32(1)` —— **归约规模**：每个线程贡献 1 个元素。
> 2. `accum_res[0]` —— **输入**：当前线程的部分和 \(p_{kr}\)。
> 3. `True` —— **是否在归约后把结果返回给参与线程**（`return_after_reduction`，即 allreduce 语义）。
> 4. `reduced_accum_res[0]` —— **输出**：归约后的完整和写入此缓冲。
> 5. `kr` —— **归约线程索引**：沿 `kr`（绑定到 `threadIdx.x`）这一轴做跨线程归约。
>
> 外层 `T.attr(... "reduce_scope" ...)` 把 `comm_reducer(+, 0)` 绑定为本次归约的运算；`T.reinterpret(T.uint64(0), "handle")` 是 TVM runtime 用来传递归约内部状态的句柄，使用时可当作固定写法。

紧随其后的单点回写：

> [benchmark_tilelang_matmul_fp16xint4.py:152-153](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L152-L153)：`if kr == 0: C[by, bx*n_partition + ni] = reduced_accum_res[0]`。注意列号由 `ni` 决定（不同 ni 写不同列，天然不冲突），但同一个 `(bx, ni)` 下有 `reduce_thread` 个 `kr`，必须只让 `kr==0` 写。

#### 4.4.4 代码实践

**实践目标**：把 `tvm_thread_allreduce` 的「输入/输出/语义」讲清楚，并与「不放 allreduce 会错成什么」对照。

**操作步骤**：

1. 在 [benchmark_tilelang_matmul_fp16xint4.py:138-153](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L138-L153) 上手动标注五个实参的含义（参考 4.4.3）。
2. 思考：若把第 3 个参数改成 `False`，并把回写改成 `if kr == 0:` 之外**没有**任何线程能拿到结果——会怎样？
3. 思考：若**删掉整段 allreduce**，直接 `if kr == 0: C[by, j] = accum_res[0]`，结果数值会变成什么？

**需要观察的现象 / 预期结果**（待本地验证，基于源码语义推断）：

- 删掉 allreduce 时，`kr==0` 写入的只是 \(p_0\)——即只含 K 中 \([0:8), [256:264), [512:520)\ldots\) 这些段（`kr=0` 负责的那部分）的贡献，其余 31 个线程的部分和全部丢失，结果会比正确值小约 `reduce_thread` 倍量级，**数值完全错误**。这说明 allreduce 是「外积-规约」调度不可或缺的一环。

#### 4.4.5 小练习与答案

**练习 1**：`get_configs` 里 `reduce_thread = [16, 32, 64]`，为什么不出现在 48 或 100 这种值？

**参考答案**：`tvm_thread_allreduce` 的效率依赖于「线程数 = warp 的整数倍/分数」。16 = 半 warp，32 = 1 warp，64 = 2 warp，这些尺寸能用最少的 shuffle/共享内存步骤完成归约。48（1.5 warp）或 100 会引入跨 warp 的不规则归约，实现复杂且低效。因此搜索空间只保留「规约友好」的尺寸。

**练习 2**：`T.comm_reducer(lambda x, y: x + y, [T.Cast(accum_dtype, 0)])` 的第二个参数为什么是 `[T.Cast(accum_dtype, 0)]` 而不是直接 `0`？

**参考答案**：单位元必须与被归约数据**同类型**。归约的是 `accum_res[0]`（类型 `accum_dtype`，本例 fp16），单位元也应是 `accum_dtype` 的 0。直接写 `0` 在 TVM 里会被当作 int64 的 0，类型不匹配；`T.Cast(accum_dtype, 0)` 显式把它转成 `accum_dtype`，保证归约类型一致。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「全链路追踪 + 调优旋钮理解」。

**任务**：选定默认 shape \(M=1, N=28762, K=8192\)（取自 [benchmark_tilelang_matmul.sh:7-12](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh#L7-L12) 的最后一组）与默认参数 `reduce_thread=32, n_partition=4`，回答下列问题，并填写一张「数据搬运路径表」。

1. **算规模**：算出 `micro_size_k`、`micro_size_k_compressed`、`block_K`、`ceildiv(K, block_K)`（ko 的迭代次数）、grid 大小。
2. **画线程布局**：一个 block 有多少线程？`kr` 和 `ni` 各自范围？一个 block 一次性负责多少个输出列？
3. **填数据路径表**：对 ko 循环里的一次迭代，列出每次 `T.copy`/`T.vectorized` 加载的「源 → 目的 → 大小 → 由谁负责（kr/ni）」。
4. **追踪一个输出元素** \(C[0, 0]\)：它由哪个 `(bx, by, ni)` 负责？哪 `reduce_thread` 个线程合作？它们的 `accum_res` 分别覆盖 K 的哪些段？最终如何合并？
5. **调优旋钮**：`get_configs`（[benchmark_tilelang_matmul_fp16xint4.py:172-187](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L172-L187)）枚举了 `n_partition ∈ {1,2,4,8,16,32}`、`reduce_thread ∈ {16,32,64}`，共 18 个配置。增大 `n_partition` 与增大 `reduce_thread` 各自会如何改变「每个 block 算多少列」与「每列用多少线程算 K」？哪个更适合「N 远大于 K」的 shape？

**预期产出（关键数值，待本地验证）**：

- `micro_size_k=8, micro_size_k_compressed=4, block_K=256`，`ko` 迭代 `ceildiv(8192, 256) = 32` 次。
- grid `(ceildiv(28762, 4), 1) = (7191, 1)`；block 线程 `(32, 4) = 128` 个（= 4 个 warp）；一个 block 一次负责 4 个输出列。
- `C[0, 0]` 由 `bx=0, by=0, ni=0` 负责，`kr=0..31` 合作；`kr` 号线程覆盖 K 段 \([kr\cdot8 + c\cdot256 \;:\; kr\cdot8 + c\cdot256 + 8]\)，\(c\in[0,32)\)；最后 allreduce 求和、`kr==0` 回写。
- `n_partition` 增大 → 每 block 算更多列、block 数减少、每列分到的线程（K 规约）不变；`reduce_thread` 增大 → 每列用更多线程算 K、ko 迭代更少。N≫K 时偏向**增大 `n_partition`**（多算几列、少算点 K），K 较大时偏向**增大 `reduce_thread`**。

> 提示：本内核用 `AutoTuner.from_kernel(...).run()` 调优（显式 AutoTuner API，详见 u6-l22），最终在日志里打印 `Latency: ... ms`（[benchmark_tilelang_matmul_fp16xint4.py:195-198](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py#L195-L198)）。若环境允许，可运行 `python benchmark_tilelang_matmul_fp16xint4.py --m 1 --n 28762 --k 8192` 观察调优过程与最优 `(n_partition, reduce_thread)`，验证你的推测。

## 6. 本讲小结

- **GEMV（M=1）带宽受限、TensorCore 利用率极低**，因此 TileLang 用「线程级外积-规约」而非块级 `T.gemm`，这是本内核与 u3-l9 块级 GEMM 的根本区别。
- **`T.alloc_local`** 为每个线程分配私有寄存器缓冲；尺寸由「128 位访存事务」推出 `micro_size_k=8`（fp16），压缩后 `micro_size_k_compressed=4`，`block_K = reduce_thread × micro_size_k`。
- **`T.thread_binding`** 把 `kr` 绑到 `threadIdx.x`（K 规约轴）、`ni` 绑到 `threadIdx.y`（N 输出列轴），把循环变量变成硬件线程索引，是「线程级并行」的开关。
- **`T.serial` / `T.vectorized`** 分别标注「串行（如 ko、标量乘加）」与「向量化（如 128 位 A/B 加载）」两类循环；本内核**没有** `T.gemm`/`T.Pipelined`/`T.copy`。
- **`T.tvm_thread_allreduce` + `T.comm_reducer(+, 0)`** 把 `reduce_thread` 个线程的部分和归约成完整点积，归约后用 `if kr == 0` 单点回写避免竞争。
- 「以代码为准」：本目录文件名（`fp16xfp4.py` 实为 int8/2-bit 块级 GEMM）与注释（多处 half-precision 字样）存在历史遗留不一致，理解调度时务必以代码结构为准。

## 7. 下一步学习建议

- **u4-l14 量化与 lop3 快速解码**：本讲有意跳过了 `fast_decoding` 分支（`T.import_source` + `T.call_extern` + lop3）与 `T.dp4a` 路径，下一讲专门讲清楚 4-bit 权重如何用 lop3 指令一次性快速解包、以及 `T.dp4a` 怎么把标量乘加换成 int8 点积。
- **u6-l22 显式 AutoTuner API**：本内核的 `AutoTuner.from_kernel(tune_kernel, get_configs()).run()` 是「显式式」调优入口，与 u3-l8 的装饰器式 `@autotune` 对照阅读，理解两种调优写法的取舍。
- **对比阅读**：把本讲内核（`fp16xint4.py`，线程级 GEMV）与同目录 `fp16xfp4.py`（块级 GEMM）并排打开，逐行比较 `alloc_local` vs `alloc_shared/fragment`、`thread_binding+serial` vs `T.gemm+T.Pipelined`、`tvm_thread_allreduce` vs `T.copy` 回写，能最直观地建立「同一算子、两种调度」的全局观。
