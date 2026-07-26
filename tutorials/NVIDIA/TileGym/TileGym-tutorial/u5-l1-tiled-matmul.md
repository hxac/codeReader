# 分块矩阵乘内核 matmul

## 1. 本讲目标

矩阵乘（GEMM）是 LLM 推理与训练里被调用次数最多、最吃性能的算子。本讲以 cuTile 版 `matmul` 为主样本，带你看懂「如何把一个巨大的矩阵乘拆成许多小瓦片，让 GPU 的每个线程块（CTA）各算一块，并沿缩减维度累加」。

学完本讲你应当能够：

- 说清 `TILE_SIZE_M / TILE_SIZE_N / TILE_SIZE_K` 三个分块参数各自的几何含义，以及一个 CTA 究竟算了输出矩阵的哪一块。
- 读懂 `_swizzle_2d` 块重排公式，解释它为什么能提升 L2（二级缓存）命中率。
- 理解 K 方向的分块循环 `for k in range(num_tiles_k)` 与 `ct.num_tiles` 的关系。
- 理解为何累加器必须用 `float32`、为何输入会在加载后升成 `tf32` 再喂给 `ct.mma`（张量核心）。
- 把同样的分块思想迁移到批量矩阵乘（BMM），看懂 3D 网格版本 `_bmm_kernel`。

## 2. 前置知识

本讲默认你已经学完：

- **u3-l1（cuTile 内核基础）**：`@ct.kernel` 把 Python 函数交给 `tileiras` JIT 编译成 GPU 代码、`ConstInt`（`ct.Constant[int]`）是编译期常量、`ct.bid(0)` / `ct.num_blocks(0)` 对应 blockIdx / gridDim。
- **u3-l2（数据搬运原语）**：`ct.load` / `ct.store` 用「锚点 `index` + 矩形 `shape`」描述一个二维瓦片；`padding_mode`（如 `ZERO`）处理越界；`ct.astype` 在「加载后升精度」与「写回前降精度」两处出现。
- **u3-l3（启动模式）**：主机侧计算 grid、`ct.launch(stream, grid, kernel, args)` 四参约定、输出张量由主机 `torch.empty` 分配。

下面用到的几个本讲新术语先给直觉：

- **GEMM（General Matrix Multiply）**：通用矩阵乘 \(C = A \times B\)。\(A\) 是 \(M \times K\)，\(B\) 是 \(K \times N\)，\(C\) 是 \(M \times N\)。\(K\) 是「缩减维度」（内积维度），因为输出每个元素都要沿 \(K\) 方向做点积。
- **瓦片（tile）**：把大矩阵切成的小矩形块。本讲里 `TILE_SIZE_M × TILE_SIZE_N` 是「输出瓦片」的大小，`TILE_SIZE_K` 是每次沿缩减方向搬运的厚度。
- **累加器（accumulator）**：存放「部分和」的片上寄存器/共享内存变量，K 循环每跑一轮就往里加一项。
- **张量核心（Tensor Core）/ mma**：GPU 上专门做「小矩阵乘加」\(D = A \times B + C\) 的硬件单元，`ct.mma(a, b, accumulator)` 就是它的 cuTile 接口。
- **L2 缓存（二级缓存）**：所有 SM 共享的片外高速缓存。多个 CTA 读同一块数据时，若数据已在 L2，就不必再跑一趟显存（HBM）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/ops/cutile/matmul.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py) | 本讲主样本。含两个内核：非持久化 `_matmul_kernel`（一块算一个输出瓦片）与持久化 `_static_persistent_matmul_kernel`（一块跨步处理多个输出瓦片），以及主机侧 autotune 启动函数与 `@register_impl("matmul", backend="cutile")` 入口。 |
| [src/tilegym/ops/cutile/bmm.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/bmm.py) | 批量矩阵乘的 cuTile 实现。`_bmm_kernel` 用 3D 网格（batch/M/N 各占一维）演示同一套分块思想在三维下的写法，是本讲的对照样本。 |
| [src/tilegym/ops/ops.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py) | 统一算子接口。`matmul` 只是一个带 `@dispatch("matmul")` 的 stub，函数体抛 `NotImplementedError`，真正计算由本讲的 cuTile 实现经注册表分发完成。 |
| [src/tilegym/ops/cutile/\_\_init\_\_.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/__init__.py) | 仅当 cutile 后端可用时才 `from . import matmul`、`bmm`，导入副作用即完成 `@register_impl` 注册。 |
| [tests/ops/test_matmul.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_matmul.py) | 用法与容差范例。`reference` 就是 `a @ b`，容差 `atol=1e-2, rtol=1e-2`，是验证你自己调用是否正确的最权威参照。 |

> 持久化调度（`_static_persistent_matmul_kernel` 的 grid-stride 循环、`num_ctas`/CGA）与 autotuning 机制是本讲的「邻居」，分别在 **u5-l2** 与 **u5-l3** 详解。本讲只聚焦非持久化 `_matmul_kernel` 这一最干净的分块 GEMM 样本。

## 4. 核心概念与源码讲解

### 4.1 TILE_M/N/K 分块：一个 CTA 算一个输出瓦片

#### 4.1.1 概念说明

朴素地实现 \(C = A \times B\)，最坏情况是每个输出元素 \(C_{ij}\) 都独立算一遍 \(\sum_k A_{ik} B_{kj}\)，于是 \(A\) 和 \(B\) 的同一批数据会被反复从显存搬运无数次。分块矩阵乘的核心思想是：**把输出矩阵切成许多 `TILE_SIZE_M × TILE_SIZE_N` 的小瓦片，让一个 CTA（线程块）专门负责一个输出瓦片**。这样：

- 该 CTA 只需要读取 A 对应的 `TILE_SIZE_M` 行、B 对应的 `TILE_SIZE_N` 列，数据搬进来后可在片上反复复用。
- 输出瓦片一个一个地、互不重叠地填满整个 \(M \times N\) 的 C。

三个分块参数的几何含义：

| 参数 | 几何含义 | 体现在 |
| --- | --- | --- |
| `TILE_SIZE_M` | 输出瓦片的高（C 的行方向，也是 A 的行方向） | 从 A 取 `TILE_SIZE_M` 行 |
| `TILE_SIZE_N` | 输出瓦片的宽（C 的列方向，也是 B 的列方向） | 从 B 取 `TILE_SIZE_N` 列 |
| `TILE_SIZE_K` | 沿缩减维度 K 一次搬运的厚度（A 的列 / B 的行） | K 循环每轮的步长 |

输出矩阵在「瓦片网格」上是 `num_bid_m × num_bid_n` 的棋盘：

\[
\text{num\_bid\_m} = \lceil M / \text{TILE\_SIZE\_M} \rceil,\quad
\text{num\_bid\_n} = \lceil N / \text{TILE\_SIZE\_N} \rceil
\]

#### 4.1.2 核心流程

```
输出矩阵 C (M×N) 被切成 num_bid_m × num_bid_n 个输出瓦片
每个 CTA 负责一个输出瓦片 (bidx, bidy)
  - bidx ∈ [0, num_bid_m)：A 的行瓦片号
  - bidy ∈ [0, num_bid_n)：B 的列瓦片号
主机侧 grid = (num_bid_m * num_bid_n, 1, 1)
```

关键问题是：一维的 `ct.bid(0)` 如何映射成二维的 `(bidx, bidy)`？最朴素的办法是 `bidx = bid // num_bid_n; bidy = bid % num_bid_n`（行主序铺平）。`_matmul_kernel` 没有这么做，而是先调用 `_swizzle_2d` 做了一次「块重排」——这是 4.2 的主题。这里你只需记住：经过 `_swizzle_2d`，每个 CTA 拿到属于自己的 `(bidx, bidy)`，对应输出 C 的一个瓦片。

#### 4.1.3 源码精读

内核签名只接收三个 `ConstInt` 分块参数，`A`、`B`、`C` 是运行期张量指针：

[src/tilegym/ops/cutile/matmul.py:L139-L147](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L139-L147) —— `@ct.kernel` 装饰 `_matmul_kernel`，`TILE_SIZE_M/N/K` 标注为 `ConstInt`（编译期常量，会被烤进特化内核）。

内核一进来就读出 M、N，并调 `_swizzle_2d` 拿到本块的瓦片坐标：

[src/tilegym/ops/cutile/matmul.py:L166-L169](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L166-L169) —— `GROUP_SIZE_M = 8` 是 swizzle 的分组参数（4.2 详解）；`M = A.shape[0]`、`N = B.shape[1]` 直接从张量形状取，于是内核无需把 M、N 作为参数传入（对比持久化内核显式传 M/N/K）。`bidx, bidy = _swizzle_2d(...)` 即「本 CTA 负责的输出瓦片坐标」。

主机侧 grid 的计算（在 autotune 启动函数里）正好印证了「瓦片数 = 输出瓦片总数」：

[src/tilegym/ops/cutile/matmul.py:L331-L334](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L331-L334) —— grid 第一维 = `ceil(M/TILE_SIZE_M) * ceil(N/TILE_SIZE_N)`，即输出瓦片总数；后两维为 1。这正是一个 CTA 算一个输出瓦片的直接体现：grid 大小 = 输出瓦片数。

**BMM 对照（3D 网格）**：BMM 把「批量」也摊到 grid 上，用真正的三维 block 索引，于是无需 swizzle：

[src/tilegym/ops/cutile/bmm.py:L68-L70](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/bmm.py#L68-L70) —— `bidx = ct.bid(0)`（M 维）、`bidy = ct.bid(1)`（N 维）、`bidz = ct.bid(2)`（batch 维）。三维网格天然把 batch、M、N 各占一维，所以 BMM 不需要像 2D GEMM 那样把一维 bid 映射回二维——这也是 BMM 通常不需要 swizzle 的原因（但它仍可选择持久化调度）。

#### 4.1.4 代码实践

1. **目标**：建立「grid 大小 = 输出瓦片数」的直觉。
2. **操作**：在 [src/tilegym/ops/cutile/matmul.py:L331](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L331) 旁边手算一个例子：设 `M=1024, N=2048, TILE_SIZE_M=128, TILE_SIZE_N=128`，写出 `ceil(1024/128) * ceil(2048/128)` 的值。
3. **观察**：grid 第一维 = `8 * 16 = 128`，即 128 个 CTA 各算一个 `128×128` 的输出瓦片，正好铺满 `1024×2048` 的 C。
4. **预期结果**：128。你可以把这个手算推广到 BMM 的 [bmm.py:L342](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/bmm.py#L342)（非持久化路径）`grid = (ceil(M/TILE_M), ceil(N/TILE_N), Q)`，三维 grid 各维含义一目了然。

#### 4.1.5 小练习与答案

**练习 1**：如果 `TILE_SIZE_M` 变成原来的 2 倍、其他不变，grid 第一维会变成多少？每个 CTA 的工作量呢？

**参考答案**：`num_bid_m` 减半，grid 第一维也大致减半（因为 `num_bid_m` 翻倍除以 2），但每个 CTA 现在要算 `2×` 高的输出瓦片，单块工作量翻倍。总工作量不变，只是「并行度」与「单块负载」之间的权衡变了。

**练习 2**：为何 `_matmul_kernel` 能直接 `M = A.shape[0]` 取维度，而 `_static_persistent_matmul_kernel` 却要把 M/N/K 作为参数显式传入？

**参考答案**：非持久化内核里 `M = A.shape[0]`、`N = B.shape[1]` 在内核内读取张量形状即可，因为它的 grid 由主机侧算好（一块一瓦片）。持久化内核需要在内核内用 `ct.cdiv(M, TILE_M)` 算瓦片总数并做 grid-stride 循环，把 M/N/K 作为 `int` 参数传入更便于编译器追踪常量；两种写法都能拿到 M/N/K，只是来源不同。

---

### 4.2 _swizzle_2d 块重排与 L2 局部性

#### 4.2.1 概念说明

如果把输出瓦片按「行主序」依次分配给 CTA（`bidx = bid // num_bid_n; bidy = bid % num_bid_n`），看似自然，却会浪费 L2 缓存。原因是：**GPU 的调度器倾向于让编号相近的 CTA 几乎同时运行**。行主序下，相邻 CTA 算的是同一行（同一 `bidx`）里相邻列的瓦片——它们共享 A 的同一批行，但 B 的列各不相同，且很快就会把 A 的行挤出 L2，再被后面的 CTA 重新从显存搬一遍。

**Super-grouping（超组）技巧**：把瓦片空间按 `GROUP_SIZE_M` 行为一组切成「超组」，让调度器先集中算完一个超组里的所有瓦片，再算下一个超组。这样同一块 A 的行在一个超组内被 `GROUP_SIZE_M × num_bid_n` 个 CTA 反复复用，L2 命中率显著提升。这正是 cuTile/Triton 教科书 GEMM 里常见的 `_swizzle_2d`。

#### 4.2.2 核心流程

`_swizzle_2d` 把一维的 `bid` 映射成 `(bid_m, bid_n)`，但不是朴素行主序，而是「按超组遍历」。设 `GROUP_SIZE_M = G`：

\[
\begin{aligned}
\text{num\_bid\_in\_group} &= G \times \text{num\_bid\_n} \\
\text{group\_id} &= \text{bid} \ //\ \text{num\_bid\_in\_group} \\
\text{first\_bid\_m} &= \text{group\_id} \times G \\
\text{bid\_m} &= \text{first\_bid\_m} + (\text{bid} \bmod \text{group\_size\_m}) \\
\text{bid\_n} &= (\text{bid} \bmod \text{num\_bid\_in\_group})\ //\ \text{group\_size\_m}
\end{aligned}
\]

直觉上：`bid` 每增加 1，就落在「当前超组」内的下一个位置；一个超组（`G × num_bid_n` 个瓦片）填满后，`group_id` 才跳到下一组，`first_bid_m` 随之增加 `G`。于是编号相近的 CTA 聚焦在少数几行内，A 的行块在 L2 里驻留时间变长。

#### 4.2.3 源码精读

[src/tilegym/ops/cutile/matmul.py:L24-L35](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L24-L35) —— `_swizzle_2d` 的完整实现。注意它是一个**普通 Python 函数**，但在 `@ct.kernel` 内部被调用，于是由 tileiras tracer 跟踪成 Tile IR——所以函数体里用的 `ct.bid(0)`、`//`、`%`、`min` 都是在构造 IR，而非 Python 解释器执行。`group_size_m = min(num_bid_m - first_bid_m, GROUP_SIZE_M)` 处理最末一个不完整超组的边界（最后一组可能不足 G 行）。

持久化内核里有一个等价的 `_compute_bid`：

[src/tilegym/ops/cutile/matmul.py:L38-L44](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L38-L44) —— 区别仅在于它接收 `tile_id` 作为参数（持久化内核用 grid-stride 循环遍历多个 `tile_id`），并把 `min` 换成 cuTile IR 版的 `ct.minimum`（因为在内核内对 IR 值取 min 必须用 `ct.minimum`，不能用 Python `min`）。

`GROUP_SIZE_M` 在非持久化内核里是硬编码常量：

[src/tilegym/ops/cutile/matmul.py:L166](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L166) —— `GROUP_SIZE_M = 8`。在持久化内核和 BMM 里它则是 autotune 参数 `cfg.GROUP_SIZE_M`（见 [bmm.py:L134](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/bmm.py#L134) 的 `num_bid_in_group = GROUP_SIZE_M * num_bid_n`），说明这个值是可调的——太大太小都不好，最优值依赖矩阵形状与 L2 大小。

#### 4.2.4 代码实践

1. **目标**：亲手走一遍 `_swizzle_2d`，看清「块重排」相对朴素行主序的差别。
2. **操作**：在 Python 里（**不是** GPU 内核，只是纯算术模拟）复现 `_swizzle_2d` 与朴素映射，设 `M=512, N=512, TILE_SIZE_M=TILE_SIZE_N=128, GROUP_SIZE_M=2`，于是 `num_bid_m=4, num_bid_n=4`。打印 `bid` 从 0 到 15 时两种映射各自的 `(bid_m, bid_n)`。
   ```python
   # 示例代码（纯 CPU 模拟，用于理解映射关系，非项目源码）
   def naive(bid, num_bid_n):
       return bid // num_bid_n, bid % num_bid_n
   def swizzle(bid, num_bid_m, num_bid_n, G):
       num_in_group = G * num_bid_n
       gid = bid // num_in_group
       first_m = gid * G
       gsm = min(num_bid_m - first_m, G)
       return first_m + (bid % gsm), (bid % num_in_group) // gsm
   ```
3. **观察**：朴素映射下 bid=0..3 全部落在 `bid_m=0`（同一行）；swizzle 下 bid=0..7（一个超组，`G*num_bid_n=2*4=8`）落在 `bid_m∈{0,1}` 的两行里。也就是说 swizzle 让前 8 个 CTA 聚焦在前 2 行，A 的前 2 个行块在 L2 里被这 8 个 CTA 充分复用。
4. **预期结果**：你应看到 swizzle 的前若干个 `(bid_m, bid_n)` 落在少数几行内；这正是「super-grouping 提升 L2 命中率」的直观来源。
5. **待本地验证**：`GROUP_SIZE_M` 的最优值随硬件 L2 容量与矩阵形状变化，本模拟只验证映射关系，不验证性能。

#### 4.2.5 小练习与答案

**练习 1**：把 `_swizzle_2d` 里的 `GROUP_SIZE_M` 设成 1，映射会退化成什么？

**参考答案**：`GROUP_SIZE_M=1` 时 `num_bid_in_group = num_bid_n`，`group_size_m = min(..., 1) = 1`，于是 `bid_m = first_bid_m + 0 = group_id`，`bid_n = (bid % num_bid_n) // 1 = bid % num_bid_n`，等价于朴素行主序。即「无分组」就是朴素映射，印证了 `GROUP_SIZE_M` 是「分组力度」旋钮。

**练习 2**：为什么持久化内核里的 `_compute_bid` 用 `ct.minimum` 而非 Python `min`？

**参考答案**：因为 `_compute_bid` 在 `@ct.kernel` 内部被调用，它的参数 `num_bid_m`、`first_bid_m` 此时是 Tile IR 值（由 `ct.cdiv` 等产生），Python 内建 `min` 无法对 IR 值求值；必须用 cuTile 提供的 IR 算子 `ct.minimum`。而非持久化路径的 `_swizzle_2d` 同样在内核内被调用，它用 Python `min` 能工作，是因为 tracer 对 `min` 做了特殊支持——两种写法都正确，`ct.minimum` 是更显式的 IR 写法。

---

### 4.3 K-tile 循环与 num_tiles：沿缩减维度分块

#### 4.3.1 概念说明

一个输出瓦片 \(C_{ij}\)（`TILE_SIZE_M × TILE_SIZE_N`）的计算是：

\[
C_{ij} = \sum_{k=0}^{K-1} A_{i,k} \cdot B_{k,j}
\]

\(K\) 可能很大（LLM 里动辄几千上万），不可能一次性把整条 \(K\) 都搬进片上存储。于是沿 \(K\) 再切一刀，每次只搬 `TILE_SIZE_K` 厚的一层：从 A 取 `TILE_SIZE_M × TILE_SIZE_K` 的瓦片，从 B 取 `TILE_SIZE_K × TILE_SIZE_N` 的瓦片，做一次小矩阵乘加，把结果累加进 accumulator。循环 `num_tiles_k = ceil(K / TILE_SIZE_K)` 次后，accumulator 里就是完整的输出瓦片。

这本质上和 u3-l4 softmax 的「分块」是同一个套路：**缩减维度太长就分块，每块算一个部分结果，最后合并**。区别只在 softmax 合并的是统计量（max/sum），GEMM 合并的是部分和（直接相加）。

#### 4.3.2 核心流程

```
num_tiles_k = ceil(K / TILE_SIZE_K)        # 沿 K 方向要跑几轮
accumulator = 0  (TILE_SIZE_M × TILE_SIZE_N 的 float32 矩阵)
for k in range(num_tiles_k):
    a = load(A, index=(bidx, k), shape=(TILE_SIZE_M, TILE_SIZE_K))   # A 的第 bidx 个行块、第 k 个 K-块
    b = load(B, index=(k, bidy), shape=(TILE_SIZE_K, TILE_SIZE_N))   # B 的第 k 个 K-块、第 bidy 个列块
    accumulator = mma(a, b, accumulator)   # accumulator += a @ b
store(C, index=(bidx, bidy), tile=accumulator)
```

注意 index 的「转置感」：A 的 K 维是它的第 1 轴（`A.shape = (M, K)`），B 的 K 维也是它的第 0 轴（`B.shape = (K, N)`）。所以 A 的瓦片索引是 `(bidx_M_tile, k_K_tile)`，B 的瓦片索引是 `(k_K_tile, bidy_N_tile)`——K 块在两个矩阵里分别落在不同轴上。

#### 4.3.3 源码精读

K 方向的瓦片数由 `ct.num_tiles` 计算：

[src/tilegym/ops/cutile/matmul.py:L171-L175](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L171-L175) —— `num_tiles_k = ct.num_tiles(A, axis=1, shape=(TILE_SIZE_M, TILE_SIZE_K))`。`ct.num_tiles(张量, axis, shape)` 的语义是：把张量看成由 `shape` 大小的瓦片铺满，问 `axis` 那一轴上有多少个瓦片，等价于对 A 的第 1 轴（K 轴）做 `ceil(K / TILE_SIZE_K)`。用 `ct.num_tiles` 而非 `K // TILE` 的好处是它由 tracer 直接生成边界安全的 IR，且能配合 `ct.load` 的瓦片索引协议。

K 循环主体——加载、mma、累加：

[src/tilegym/ops/cutile/matmul.py:L189-L202](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L189-L202) —— 每轮：`a = ct.load(A, index=(bidx, k), shape=(TILE_SIZE_M, TILE_SIZE_K), padding_mode=zero_pad)`（A 的行块 `bidx`、K 块 `k`）；`b = ct.load(B, index=(k, bidy), shape=(TILE_SIZE_K, TILE_SIZE_N), ...)`；`accumulator = ct.mma(a, b, accumulator)`。`zero_pad` 处理 K 不能被 `TILE_SIZE_K` 整除时最末一块的越界（补 0，乘加后不影响累加结果）。`.astype(dtype)` 的作用见 4.4。

写回：

[src/tilegym/ops/cutile/matmul.py:L204-L210](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L204-L210) —— K 循环结束后，把 fp32 accumulator 降回 `C.dtype`，再用 `ct.store(C, index=(bidx, bidy), tile=accumulator)` 写回输出瓦片。store 的 index 与每轮 load 的 A/B index 一一对应同一个 `(bidx, bidy)` 输出瓦片。

**BMM 对照**：3D 版的 K 循环几乎一样，只是 load 带 batch 维、加载后要 reshape 回 2D 喂给 mma：

[src/tilegym/ops/cutile/bmm.py:L72-L95](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/bmm.py#L72-L95) —— `num_k_tiles = ct.num_tiles(A, axis=2, ...)`（A 是 3D `(Q,M,K)`，K 在 axis=2）；每轮 `ct.load(A, index=(bidz, bidx, k), shape=(1, TM, TK))` 取出 `1×TM×TK` 的 3D 瓦片，`ct.reshape` 成 2D 后做 `ct.mma`；最后把结果 reshape 回 `1×TM×TN` 用 3D index store。可以看到 BMM 的 K 循环骨架与 2D GEMM 完全同构，只是多了 batch 维与 reshape。

#### 4.3.4 代码实践

1. **目标**：用 `ct.num_tiles` 的语义手算 K 分块数，并理解 padding 的作用。
2. **操作**：设 `K=1023, TILE_SIZE_K=32`。手算 `ceil(1023/32)`，并解释最末一块的实际有效厚度。
3. **观察**：`1023 / 32 = 31.9...`，故 `num_tiles_k = 32`；前 31 块各 32 列满块，第 32 块只有 `1023 - 31*32 = 31` 列有效，剩余 1 列由 `padding_mode=ZERO` 补 0。
4. **预期结果**：32 块，最末块 31 列有效。这也解释了 [test_matmul.py:L130-L131](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_matmul.py#L130-L131) 为什么对 `k==1023` 的某些组合会 `pytest.skip("...result mismatch when cannot divide BLOCK")`——分块边界在某些后端/精度组合下会引入误差，所以测试显式跳过。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接 `for k in range(K)` 逐元素累加，而要分块？

**参考答案**：逐元素累加无法利用张量核心（张量核心一次算的是一个小矩阵乘 `TILE_K × ...`），且每个元素都要单独从显存读 A、B，带宽完全浪费。分块后，一次 `ct.load` 搬进一整块 `TILE_SIZE_K` 厚的数据，一次 `ct.mma` 调用硬件矩阵乘单元，带宽与算力都被充分利用。

**练习 2**：BMM 里 `ct.num_tiles(A, axis=2, ...)` 的 axis 为什么是 2？

**参考答案**：BMM 的 A 形状是 `(Q, M, K)`，K 是第 2 轴（0-indexed），所以 `axis=2`。而 2D GEMM 的 A 是 `(M, K)`，K 在第 1 轴，所以 `axis=1`。`ct.num_tiles` 的 axis 永远指向「该张量里 K 所在的那个轴」。

---

### 4.4 float32 累加器、mma 与 tf32 张量核心

#### 4.4.1 概念说明

GEMM 的累加是一个长链求和。如果输入是 fp16/bf16，直接用 fp16 做累加会在几百次相加后丢失精度（fp16 只有约 3 位十进制有效数字）。**行业标准做法：累加用 fp32，输入加载后升成适合张量核心的精度，最后再降回输出精度。**

cuTile 这里的精度链是：

1. accumulator 初始化为 fp32 的 0。
2. 每轮 load 出来的 A/B 瓦片，若原是 fp32，先 `astype` 成 **tf32**（TensorFloat-32）；若是 fp16/bf16 则保持。
3. `ct.mma(a, b, accumulator)` 调用张量核心，做 `accumulator += a @ b`（累加在 fp32 进行）。
4. 循环结束后，accumulator 降回 `C.dtype` 写回。

**tf32 是什么**：NVIDIA Ampere 起张量核心支持的「截尾 fp32」格式——指数位与 fp32 相同（数值范围大），尾数截到 10 位（精度接近 fp16）。用它做矩阵乘的乘法，能在「几乎不损失实际精度」的前提下跑满张量核心吞吐。把 fp32 输入转成 tf32 喂给 mma，本质是「用 1 位尾数精度换数倍吞吐」。

`ct.mma(a, b, accumulator)` 与 `ct.mma(a, b, acc=accumulator)` 两种写法等价：第三个参数是「累加进」的接收者，即 \(D = A \times B + C\) 里的 \(C\)。

#### 4.4.2 核心流程

```
dtype = ct.tfloat32 if A.dtype == ct.float32 else A.dtype   # fp32→tf32，其他不动
accumulator = ct.full((TILE_M, TILE_N), 0, dtype=ct.float32) # 累加器恒为 fp32
for k in range(num_tiles_k):
    a = load(A, ...).astype(dtype)   # 升/转精度喂张量核心
    b = load(B, ...).astype(dtype)
    accumulator = ct.mma(a, b, accumulator)   # fp32 累加
accumulator = ct.astype(accumulator, C.dtype)  # 降回输出精度
store(C, ..., tile=accumulator)
```

#### 4.4.3 源码精读

[src/tilegym/ops/cutile/matmul.py:L177-L184](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L177-L184) —— 关键三行：`accumulator = ct.full((TILE_SIZE_M, TILE_SIZE_N), 0, dtype=ct.float32)` 把累加器钉死为 fp32；`zero_pad = ct.PaddingMode.ZERO` 设定越界补 0；`dtype = ct.tfloat32 if A.dtype == ct.float32 else A.dtype` 决定喂给 mma 的精度——fp32 输入降为 tf32，fp16/bf16 保持原样。

[src/tilegym/ops/cutile/matmul.py:L193-L202](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L193-L202) —— `a = ct.load(...).astype(dtype)`、`b = ct.load(...).astype(dtype)` 在加载后立刻转精度；`accumulator = ct.mma(a, b, accumulator)` 累加。注意非持久化内核用位置参数 `accumulator`，持久化内核 [matmul.py:L315](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L315) 用关键字 `acc=accumulator`，两者等价。

[src/tilegym/ops/cutile/matmul.py:L204-L206](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L204-L206) —— 循环结束后 `accumulator = ct.astype(accumulator, C.dtype)`，把 fp32 部分和降回输出 dtype（如 fp16），再 store。这一步是「精度→存储」的妥协：累加用 fp32 保精度，存储用低精度省显存/带宽。

**BMM 对照**：BMM 的累加器同样是 fp32，但加载后**没有**做 fp32→tf32 转换：

[src/tilegym/ops/cutile/bmm.py:L75-L89](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/bmm.py#L75-L89) —— `sum = ct.full((TM, TN), 0.0, dtype=ct.float32)` 是 fp32 累加器；但 load 之后直接 `ct.mma(a, b, acc=sum)`，没有 `.astype(tfloat32)`。也就是说 BMM 非持久化内核对 fp32 输入不强制走 tf32（由底层按 A.dtype 处理）。这是两个内核在精度策略上的一个细微差异，阅读时值得留意。

#### 4.4.4 代码实践

1. **目标**：体会「累加用 fp32」对精度的必要性。
2. **操作（源码阅读型）**：打开 [test_matmul.py:L148-L149](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_matmul.py#L148-L149)，找到容差设置 `atol=1e-2, rtol=1e-2`。再设想：若把 `accumulator` 的 `dtype=ct.float32` 改成 `ct.float16`，在一个 `K=8192` 的乘法里，8192 次 fp16 相加会怎样？
3. **观察与预期**：fp16 的累加误差会随项数增长，8192 项后误差远超 `1e-2`，测试会大面积失败。这正说明 cuTile 把累加器钉为 fp32 不是随便写的，而是为了通过这个容差。**这一步只需阅读与推理，不要真的去改源码（本讲禁止修改源码）。**
4. **待本地验证**：若你另有可改动的实验环境，可在副本里把 accumulator 降到 fp16 跑 `test_matmul.py`，观察失败比例上升——但这超出本讲「只读」范围，仅作理解验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `dtype = ct.tfloat32 if A.dtype == ct.float32 else A.dtype` 这一行的 `else` 分支不把 fp16/bf16 也转成 tf32？

**参考答案**：tf32 是「fp32 的截尾」，只有 fp32 输入转 tf32 才有意义（同数值范围、降尾数）。fp16/bf16 本身就是张量核心原生支持的低精度格式，直接喂给 mma 即可，转成 tf32 反而要做无谓的格式转换、且可能改变数值范围（bf16 的指数位比 fp32 还宽），得不偿失。

**练习 2**：`ct.mma(a, b, accumulator)` 返回的新值赋回 `accumulator`，这和「原地累加」有什么区别？

**参考答案**：在 cuTile 的 IR 语义里，`accumulator = ct.mma(a, b, accumulator)` 表达的是 \(D = A \times B + C\)，编译器可以把它Lower成张量核心的 `mma` 指令并把结果写回同一组寄存器（事实上的原地更新），但源码层面它是一个「返回新瓦片」的纯函数式写法。这样写便于 tracer 追踪数据流，编译器再决定是否复用寄存器，比显式「原地修改」更安全、更易优化。

---

## 5. 综合实践

把本讲四块知识串起来，完成下面这个「读图标注」任务。

**任务**：打印或手抄 [src/tilegym/ops/cutile/matmul.py:L139-L210](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L139-L210) 的 `_matmul_kernel` 全文，用三种颜色笔标注：

1. **块坐标（对应 4.1 + 4.2）**：圈出 `bidx, bidy = _swizzle_2d(M, N, TILE_SIZE_M, TILE_SIZE_N, GROUP_SIZE_M)`（[L169](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L169)）。在旁边写明：`bidx/bidy` 是「本 CTA 负责的输出瓦片坐标」，由一维 `bid` 经 super-grouping 重排得到，`GROUP_SIZE_M=8` 控制分组力度。
2. **K 循环（对应 4.3）**：框出 `for k in range(num_tiles_k):` 整个循环体（[L189-L202](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L189-L202)）。在 A 的 load 旁注 `(bidx, k) = (M-瓦片号, K-瓦片号)`，在 B 的 load 旁注 `(k, bidy) = (K-瓦片号, N-瓦片号)`，说明 K 块在 A 的 axis=1、在 B 的 axis=0。
3. **精度链（对应 4.4）**：下划线标出 `ct.full(..., dtype=ct.float32)`（[L180](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L180)）、`.astype(dtype)`（[L193](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L193)/[L198](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L198)）、`ct.astype(accumulator, C.dtype)`（[L206](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L206)）。在旁边用箭头串成：`fp32 累加器 → load 后转 tf32 喂 mma → 降回 C.dtype 写回`。

**进阶**：完成上述标注后，对照 [src/tilegym/ops/cutile/bmm.py:L62-L95](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/bmm.py#L62-L95) 的 `_bmm_kernel`，列出两者的三点关键差异：（a）BMM 用 3D 网格无需 swizzle；（b）BMM 的 load 多一个 batch 维且加载后 reshape 回 2D；（c）BMM 未做 fp32→tf32 转换。

> 本任务全程只读源码、无需运行 GPU；若你想验证理解，可在本地写一个最小脚本调用 `tilegym.ops.matmul(a, b)` 并与 `a @ b` 比较（容差参考 [test_matmul.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_matmul.py) 的 `atol=1e-2, rtol=1e-2`），但能否跑通取决于本地是否具备 cutile 后端与对应 GPU，**结果待本地验证**。

## 6. 本讲小结

- **分块几何**：`_matmul_kernel` 让一个 CTA 算一个 `TILE_SIZE_M × TILE_SIZE_N` 的输出瓦片，主机侧 grid = 输出瓦片总数 `ceil(M/TM) * ceil(N/TN)`；BMM 把 batch 也摊到 grid，用 3D block 索引，于是无需 swizzle。
- **块重排**：`_swizzle_2d` 用 `GROUP_SIZE_M`（默认 8）做 super-grouping，让编号相近的 CTA 聚焦在少数几行，提升 A 的行块在 L2 的复用率；`GROUP_SIZE_M=1` 即退化为朴素行主序。
- **K 循环**：缩减维度 K 被 `TILE_SIZE_K` 切成 `num_tiles_k = ct.num_tiles(A, axis=1, ...)` 块，每轮 load 一对 A/B 瓦片做 `ct.mma` 累加；越界由 `PaddingMode.ZERO` 补 0。
- **精度链**：累加器恒为 fp32（`ct.full(..., dtype=ct.float32)`）；fp32 输入加载后降为 tf32 喂张量核心、fp16/bf16 保持原样；循环结束再降回 `C.dtype` 写回。
- **同构性**：2D GEMM 与 3D BMM 的 K 循环骨架完全同构，差异只在 batch 维与 reshape；持久化版本（`_static_persistent_matmul_kernel`）把 `GROUP_SIZE_M`、`num_ctas`、`LOAD_LATENCY` 都变成 autotune 参数。
- **入口**：这些内核经 [matmul.py:L416-L450](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/matmul.py#L416-L450) 的 `@register_impl("matmul", backend="cutile")` 注册，由 `ops.py` 的 `@dispatch("matmul")` stub 分发，用户侧只看到 `tilegym.ops.matmul(a, b)`。

## 7. 下一步学习建议

- **u5-l2（静态持久化调度与 group_gemm）**：本讲的 `_matmul_kernel` 是「一块一瓦片」，而 `_static_persistent_matmul_kernel` 是「一块用 grid-stride 循环处理多个输出瓦片」，并引入 `num_ctas`/CGA 与 `replace_hints`。学完它能理解「瓦片数多于 SM 时如何复用 CTA」。
- **u5-l3（Autotuning 机制）**：本讲里 `TILE_SIZE_M/N/K`、`occupancy`、`num_ctas` 都来自 `_matmul_autotune_configs()` 与 `exhaustive_search`，下一讲系统讲解这套调优框架、tune cache 与 `TILEGYM_DISABLE_AUTOTUNE` 开关。
- **延伸阅读**：直接对照 [bmm.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/bmm.py) 的 `_static_persistent_bmm_kernel`，看持久化 + 3D + transpose（`order`/`ct.permute`）如何叠加，是把本讲四块知识融会贯通的最好练习。
