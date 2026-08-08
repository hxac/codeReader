# Softmax 前向内核逐行解读

## 1. 本讲目标

本讲是「读懂一个完整 CuTe-DSL 归约内核」的第一个实战。我们以 `Softmax` 前向内核为标本，把它从「被调用」到「写回结果」整条链路拆成三段：

1. **主机侧启动**：`__call__` 如何根据 `N` 与硬件架构推导 grid/block/cluster，并启动设备内核。
2. **数据搬运与边界**：内核如何把数据从全局内存（gmem）经共享内存（smem）搬进寄存器（rmem），并用谓词（predicate）与 `-inf` 填充处理非整除边界。
3. **online softmax 归约**：内核如何用耦合的「最大值 + 求和」两阶段归约得到数值稳定的 softmax。

读完本讲，你应当能够：

- 独立读通 `quack/softmax.py` 中 `Softmax` 类的 `__call__` 与 `kernel`。
- 理解 `local_tile` / `partition_S` / `partition_D` / `predicate_k` 这一组 CuTe 拷贝原语的用法。
- 说清 `is_even_N` 为 `False` 时边界元素被如何屏蔽，以及为什么用 `-inf` 填充。
- 解释 online softmax 的「重缩放合并」数学，以及它如何把 max 和 sum 打包进同一次跨 CTA 同步。

---

## 2. 前置知识

在进入内核之前，先用三段话建立直觉。

**softmax 的数学。** 给定一行向量 \( x \in \mathbb{R}^{N} \)，softmax 把它归一化成一组概率：

\[
y_i = \frac{e^{x_i}}{\sum_{j=1}^{N} e^{x_j}}
\]

直接算 \( e^{x_i} \) 会溢出（\( x_i \) 稍大就得到 `inf`）。数值稳定的写法是先减去行内最大值 \( m = \max_j x_j \)：

\[
y_i = \frac{e^{x_i - m}}{\sum_{j=1}^{N} e^{x_j - m}}
\]

减去最大值不改变结果（分子分母同乘 \( e^{-m} \)），却让指数自变量 \( \le 0 \)，于是 \( e^{x_i - m} \in (0, 1] \)，绝不溢出。整个内核的核心就是「高效地算 \( m \) 和分母」。

**online（在线）softmax。** 如果一行很长，要把它切给多个线程、甚至多个 CTA（线程块/SM 内的协作单元）各算一段。每段各自得到一个局部最大值 \( m_k \) 和局部指数和 \( s_k \)。问题是如何合并两段 \( (m_1, s_1) \) 与 \( (m_2, s_2) \) 而不重新遍历数据？答案是重缩放：

\[
m = \max(m_1, m_2), \qquad s = s_1 \, e^{m_1 - m} + s_2 \, e^{m_2 - m}
\]

这个公式是 flash-attention 类算法的基石，也是本讲归约原语 `online_softmax_reduce` 的内核。它让 max 和 sum 这两个「看似独立」的量耦合在同一次归约里，只需一次跨线程通信。

**从「行」到「tile」。** QuACK 的 softmax 处理的是形状 `(M, N)` 的张量：`M` 行，每行 `N` 列。归约沿最后一维 `N` 进行。CTA 一次处理若干行（`tiler_mn[0]` 行），每行内部的 `N` 维被一组线程（`threads_per_row`）和可能的多个 peer CTA（`cluster_n`）协作归约。这些量来自上一讲 `ReductionBase` 的配置，本讲直接使用。

> 关键术语回顾（来自 u2-l1）：CTA（协作线程阵列，一个线程块）、cluster（一组协作的 CTA）、warp（32 个线程）、smem（共享内存，CTA 内可见）、rmem（每线程的寄存器）、tiler_mn（CTA 处理的数据块形状）、cluster_n（沿 N 维协作的 CTA 数）。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
|------|------|----------------|
| `quack/softmax.py` | softmax 前向/反向内核与 autograd 包装 | `Softmax` 类（前向） |
| `quack/reduce.py` | 归约原语库 | `online_softmax_reduce`、`row_reduce`、`warp_reduce` |
| `quack/reduction_base.py` | 归约内核共享基类（u2-l1） | `_get_tiled_copy`、`_allocate_reduction_buffer_and_mbar`、`_initialize_cluster` |
| `quack/copy_utils.py` | 拷贝原语（u3-l1 详讲） | `copy`、`tiled_copy_2d`、`predicate_k` |
| `quack/utils.py` | 杂项工具 | `fill_oob`、`f32x2_to_i64` |

本讲的重点是 `softmax.py` 的 `Softmax` 类与 `reduce.py` 的 `online_softmax_reduce`；其余文件只摘取与流程直接相关的片段。

---

## 4. 核心概念与源码讲解

### 4.1 Softmax.__call__ 配置与启动

#### 4.1.1 概念说明

`Softmax` 继承自 `ReductionBase`（见 u2-l1）。它的职责是：

- 在**主机侧**（`@cute.jit` 的 `__call__`）确定本次运行用多少线程、多少 CTA、cluster 多大，然后启动设备内核。
- 在**设备侧**（`@cute.kernel` 的 `kernel`）执行真正的数据搬运与归约。

`__call__` 是主机侧编排者（host orchestrator）：它本身被编译（`@cute.jit`），但它的工作是「准备 tile、配置 launch」，最后通过 `kernel(...).launch(...)` 把设备内核发射到 GPU。这里体现了 u1-l4 讲过的「`@cute.jit` 既可做主机编排，也可做设备内联函数」的双重身份。

`Softmax` 在构造时做了一个重要选择：是否使用 online softmax。这个选择决定了归约缓冲区（reduction buffer）的**槽数**与**数据类型**。

#### 4.1.2 核心流程

`__call__` 的流程：

1. 断言输入元素类型正确。
2. 调用 `_set_cluster_n()`，按硬件架构与 `N` 选定 `cluster_n`。
3. 计算 `vecsize = 128 // largest_dtype_width`（16 位类型 → 8 元素，32 位 → 4 元素），调用基类 `_get_tiled_copy` 得到 `tiled_copy`、`tiler_mn`、`threads_per_row`。
4. 用三者组装 launch 参数：
   - `grid = [ceil_div(M, tiler_mn[0]), cluster_n, 1]`：行方向按 tile 切，N 方向按 cluster 切。
   - `block = [num_threads, 1, 1]`。
   - `cluster = [1, cluster_n, 1]`（仅当 `cluster_n > 1`）。

构造函数的 online 选择之所以影响 `stage`，是因为：
- **online 路径**：max 和 sum 在同一次归约里耦合完成，只需 **1 个缓冲槽**；但跨 CTA 合并时要打包两个 f32，缓冲类型用 `Int64`。
- **非 online 路径**：先做一次 MAX 归约，再做一次 ADD 归约，需要 **2 个缓冲槽**（`stage=2`），类型是普通的 `Float32`。

> 小结：online = 1 槽 + Int64；非 online = 2 槽 + Float32。这个对应关系是理解后续归约原语选型的钥匙。

#### 4.1.3 源码精读

构造函数，根据 online 与否设置槽数与类型：

[quack/softmax.py:26-35](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L26-L35) —— `Softmax.__init__`：online 时 `stage=1`、`reduction_dtype=Int64`；否则 `stage=2`、`reduction_dtype=Float32`。

`_threads_per_row` 决定每行用多少线程协作，按 `N` 分档：

[quack/softmax.py:37-42](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L37-L42) —— 行越宽，参与归约的线程越多（最多 256，即 8 个 warp）。

`_set_cluster_n` 按架构与 `N`、位宽选定 cluster 大小（SM90 之前无 cluster，SM12x 上限 8，SM9x 上限 16）：

[quack/softmax.py:44-64](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L44-L64) —— 阈值表随位宽变化，fp32 在 SM12x（仅 99 KB SMEM）要更紧的聚类。

主机侧 `__call__` 与 launch 配置：

[quack/softmax.py:66-85](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L66-L85) —— `_get_tiled_copy` 返回三件套；`grid` 第二维是 `cluster_n`；`cluster` 仅在 `cluster_n > 1` 时给出。注意 `cluster=[1, cluster_n, 1]` 表示 cluster 沿 N（grid 的第二维）协作。

`_get_tiled_copy` 的推导（来自 u2-l1）：

[quack/reduction_base.py:42-50](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L42-L50) —— 由 `N // vecsize`、`threads_per_row`、`cluster_n` 推出 `num_blocks_N`，再得 `tiler_mn = (num_threads // threads_per_row, vecsize * num_blocks_N * threads_per_row)`。`tiler_mn[0]` 是每 CTA 处理的行数，`tiler_mn[1]` 是每 CTA 处理的列数。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：弄清 `vecsize`、`threads_per_row`、`tiler_mn` 之间的数量关系。
2. **步骤**：取 `dtype = bfloat16`（width=16）、`N = 4096`、假设架构为 SM9x（`cluster_n` 由阈值表得 4）。手算：
   - `vecsize = 128 // 16 = 8`。
   - `_threads_per_row(4096)`：`N <= 3072` 为假、`N <= 6144` 为真 → `threads_per_row = 64`。
   - `_num_threads`：`N <= 16384` → `num_threads = 128`。
   - `num_blocks_N = ceil_div(4096//8, 64*4) = ceil_div(512, 256) = 2`。
   - `tiler_mn = (128//64, 8 * 2 * 64) = (2, 1024)`。
3. **观察**：每 CTA 处理 2 行 × 1024 列，4 个 CTA 覆盖 `4 × 1024 = 4096` 列，正好等于 `N`。
4. **预期结果**：你应能复现 `(2, 1024)`；并理解 `cluster_n=4` 把一行 4096 拆给 4 个 CTA 各 1024。

> 待本地验证：上述数值依赖 `_set_cluster_n` 的阈值表，建议在装好工具链的机器上打印 `self.cluster_n`、`tiler_mn` 核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `vecsize` 用 `128 // largest_dtype_width` 而不是固定常数？
**答案**：128 位是 GPU 一次向量化访存的最大宽度（如一条 128-bit load）。`vecsize` 是「一次拷贝的元素数」＝ 128 位 / 单元素位宽。位宽越大（fp32=32）每向量化单位元素越少（4 个），位宽越小（fp16=16）越多（8 个），从而保持每次拷贝都是满 128 位。

**练习 2**：online 路径为何能把缓冲槽数从 2 降到 1？
**答案**：非 online 要分两次独立归约（先 MAX 后 ADD），每次各占一槽；online 把 max 与 sum 耦合在一次归约里完成，且跨 CTA 时把 `(max, sum)` 打包成一个 `Int64` 写入同一槽，因此只需 1 槽。

---

### 4.2 kernel 的加载、谓词与 OOB 填充

#### 4.2.1 概念说明

设备内核 `kernel` 是真正在 GPU 上并行执行的部分。每个 CTA 处理 `tiler_mn[0]` 行、`tiler_mn[1]` 列的一块数据。它的数据流是一条经典的三级搬运链：

\[
\text{gmem} \xrightarrow{\text{cp.async}} \text{smem} \xrightarrow{\text{autovec}} \text{rmem} \xrightarrow{\text{reduce}} \text{rmem} \xrightarrow{\text{store}} \text{gmem}
\]

- **gmem → smem**：用异步拷贝 `cp.async` 把全局内存的数据搬到 CTA 共享的 smem（所有线程可见）。
- **smem → rmem**：每个线程从 smem 读自己负责的那一片进寄存器。
- **归约**：在寄存器/跨线程层面做 max 与 sum。
- **rmem → gmem**：把结果写回。

> 注意：本讲的前向 softmax 内核走的是 **cp.async** 路径（`copy(..., is_async=True)` → `CopyG2SOp`），**不是** TMA。TMA 是 Hopper/Blackwell 上的张量内存加速器，主要用于项目里的 GEMM（见 u5）。topic 中「TMA/cp.async」是泛指两类异步拷贝；这里实际是 cp.async。

**边界处理是本模块的重点。** 两个维度都要处理「数据不能整除 tile」的情况，但手法不同：

- **M 维（行）**：用普通 `if` 守卫。因为整行要么存在要么不存在，没有「半个行」。
- **N 维（列）**：用**谓词**（predicate）。一行里可能只有部分列有效，需要对每个加载位置单独判断是否越界。越界位置在 smem 里被填成 `-inf`，这样它们对 max 归约无贡献（`max(x, -inf) = x`），对 sum 也无贡献（`e^{-inf}=0`）。

#### 4.2.2 核心流程

`kernel` 的执行步骤（按源码顺序）：

```
1. 取 thread/block/cluster 索引；cluster_y = block_idx[1]（cluster_n==1 时为 0）
2. 构造 identity 张量 idX，用 local_tile 把 mX/mO/idX 切到本 CTA 的 tile (gX/gO/cX)
3. 在 smem 分配 sX；分配归约缓冲 reduction_buffer 与（若 cluster）mbar_ptr
4. thr_copy = tiled_copy.get_slice(tidx)；用 partition_S / partition_D 切出每线程的源/目的分片
5. 算 is_even_N；若非偶，用 predicate_k 生成谓词 tXpX
6. 初始化 cluster 的 mbarrier（_initialize_cluster）
7. 若本 CTA 的行有效：cp.async 把 gX → sX；commit + wait
8. 若非偶：fill_oob 把越界 smem 位置填 -inf
9. autovec 把 sX → rmem；x = load().to(Float32)
10. 归约（online 或非 online）
11. y = exp_x / denom；存入 rO；若行有效：把 rO → gO
```

`local_tile`、`partition_S`、`partition_D` 是 CuTe 的「切片三件套」：`local_tile` 按 tile 坐标切出整个 tile；`partition_S(partition_Source)` 把这个 tile 进一步按线程拷贝布局切成「每个线程拥有的源分片」；`partition_D(partition_Destination)` 切出目的分片。这样每个线程只搬运自己那一小片，整体合起来正好覆盖整个 tile。

#### 4.2.3 源码精读

线程/块/cluster 索引与 tile 切分：

[quack/softmax.py:98-105](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L98-L105) —— `shape = (M, N)`；`local_tile(mT, tiler_mn, (bidx, cluster_y))` 用 `(x 方向 tile 号, cluster 内 y 号)` 把 `mX/mO/idX` 三者切到同一坐标。`idX` 是「坐标张量」，用来跟踪每个元素的全局位置（供谓词判断）。

smem 分配与归约缓冲分配：

[quack/softmax.py:107-111](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L107-L111) —— `sX` 用列主序 `order=(1,0)`（N 维连续），16 字节对齐；归约缓冲与 mbarrier 由基类分配。

每线程分片：

[quack/softmax.py:113-119](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L113-L119) —— `tXgX = partition_S(gX)`（源，从 gmem 读）、`tXsX = partition_D(sX)`（目的，写进 smem）、`tXgO = partition_D(gO)`（目的，写回 gmem）；`tXcX` 是坐标分片的第一行坐标，用来做 M 维 `if` 守卫。

谓词与 `is_even_N`：

[quack/softmax.py:121-128](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L121-L128) —— `is_even_N = (N == tiler_mn[1] * cluster_n)`，即「N 能否被所有 CTA 的 tile 整除」。非偶时调用 `predicate_k` 生成谓词，并用 `partial(copy, pred=tXpX)` 让后续每次拷贝都带上同一谓词。

`predicate_k` 只对 K（这里即 N）维算谓词：

[quack/copy_utils.py:383-396](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/copy_utils.py#L383-L396) —— 对每个 `(rest_v, rest_k)` 位置用 `cute.elem_less(coord, limit)` 判断该列坐标是否 `< N`，得到布尔谓词。注释点明：M 维用 `if`，N 维才用谓词。

gmem → smem 的 cp.async 加载，以及 M 维守卫：

[quack/softmax.py:133-139](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L133-L139) —— `if tXcX[0][0] < shape[0]` 守住 M 维边界；`copy(tXgX, tXsX, is_async=True)` 发起 cp.async；`cp_async_commit_group` + `cp_async_wait_group(0)` 等待全部完成；非偶时 `fill_oob(..., -inf)` 填充越界。

`fill_oob` 的实现：

[quack/utils.py:103-119](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/utils.py#L103-L119) —— 遍历每个越界位置，用预先填好 `-inf` 的寄存器张量覆盖 smem 中对应位置。这样归约时越界元素恒为 `-inf`。

smem → rmem 加载与类型提升：

[quack/softmax.py:141-142](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L141-L142) —— `autovec_copy(sX→rX)`，再 `load().to(Float32)`。提升到 fp32 做归约是为了数值精度（fp16/bf16 累加会丢精度）。

结果写回：

[quack/softmax.py:173-176](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L173-L176) —— `y = exp_x * rcp_approx(denom)`（用硬件近似倒数代替除法），存回原 dtype，再写回 gmem，同样带 M 维 `if` 守卫。

#### 4.2.4 代码实践（可运行 + 阅读）

**实践 A（可运行，需 GPU 与工具链）**：观察非整除边界的数值正确性。

1. **目标**：验证 `is_even_N=False` 时内核结果与 PyTorch 参考一致。
2. **步骤**：运行一个故意取非整除 `N` 的用例。
   ```bash
   pytest tests/test_softmax.py::test_softmax -x -k "bfloat16 and 760 and 199"
   ```
   这里 `N=760` 不是任何 `tiler_mn[1]*cluster_n` 的整倍数，必然走 `is_even_N=False` 分支。
3. **观察**：测试会先编译内核（冷启动较慢），然后比对 `torch.testing.assert_close(out, F.softmax(x_ref, dim=-1))`，并断言 `sums ≈ 1`、`0 ≤ out ≤ 1`。
4. **预期结果**：用例通过，说明谓词 + `-inf` 填充的边界处理数值正确。
5. 若无法本地运行，标注「待本地验证」，改为阅读型实践（见 B）。

**实践 B（阅读型）**：在 `kernel` 源码上手工标注 7 个阶段。打印 `quack/softmax.py`，用不同颜色/记号标出：
- ① cp.async 加载（`copy(..., is_async=True)`，L134）
- ② commit/wait（L135-136）
- ③ OOB 填 `-inf`（L138-139）
- ④ smem→rmem（L141）
- ⑤ 归约（L143-171，见 4.3）
- ⑥ 除法 + 存 rmem（L173-174）
- ⑦ rmem→gmem store（L175-176）
然后回答：**`is_even_N=False` 时，边界元素先被谓词挡在加载之外（cp.async 不写这些 smem 位置），再被 `fill_oob` 显式填成 `-inf`，因此归约时它们贡献为 0。** 这两层保险确保无论 `N` 多奇怪，softmax 分母都不会被垃圾数据污染。

#### 4.2.5 小练习与答案

**练习 1**：为什么 M 维用 `if` 而 N 维用 predicate？
**答案**：M 维（行）的边界是「整行存在或不存在」，CTA 只需在进入加载前判断一次本 CTA 是否有有效行，用 `if` 跳过整个加载即可。N 维（列）的边界是「同一行内部分列有效、部分越界」，必须对每个细粒度的加载位置单独判断，所以需要逐元素的 predicate 张量。

**练习 2**：`fill_oob` 用 `-inf` 而不是 `0` 填充，为什么？
**答案**：softmax 归约包含 MAX 与「指数求和」。若填 0：MAX 不会被影响（除非全行为 0），但 `e^{0}=1` 会被错误地加进分母，使结果偏小。填 `-inf` 则 `e^{-\inf}=0`，既不影响 max（`max(x,-inf)=x`），也不增加分母，是唯一正确的填充值。

**练习 3**：`is_even_N` 的判据是 `shape[1] == tiler_mn[1] * self.cluster_n`。若 `N` 能被 `tiler_mn[1]` 整除但不能被 `tiler_mn[1]*cluster_n` 整除，会发生什么？
**答案**：仍判为 `is_even_N=False`，走谓词路径。因为多个 peer CTA 沿 N 维拼接，只有当「单 CTA tile × cluster_n」正好等于 `N` 时，所有 CTA 的 tile 才都填满无越界；否则至少有一个 CTA 的 tile 是部分越界的，需要谓词保护。

---

### 4.3 online_softmax_reduce 调用

#### 4.3.1 概念说明

数据进了寄存器后，剩下就是「对每个线程持有的那一段做局部 max/sum，再跨线程、跨 CTA 合并」。`Softmax.kernel` 把这件事委托给 `reduce.py` 里的原语：

- **online=True**（默认）：调用 `online_softmax_reduce`，一次耦合归约得到 `(max_x, denom, exp_x)`。
- **online=False**：先 `row_reduce(..., MAX)` 得 `max_x`，用它算 `exp_x`，再 `row_reduce(..., ADD)` 得 `denom`。两次独立归约。

`online_softmax_reduce` 的核心是 4.2 节讲过的「重缩放合并」。它的设计目标：**让 max 和 sum 用同一条通信路径、同一次同步完成**，从而把跨 warp/跨 CTA 的通信开销减半。

#### 4.3.2 核心流程

`online_softmax_reduce(x, threads_per_row, reduction_buffer, mbar_ptr, hook_fn, return_exp_x=True)` 的内部流程：

```
1. warp 内局部：max_x = warp_reduce(x.reduce(MAX))           # 每 lane 先各自 reduce 片段，再 warp 蝶形
2. exp_x = exp2(x * log2_e - max_x * log2_e)                  # 用减去局部 max 的 exp（数值稳定）
3. sum_exp_x = warp_reduce(exp_x.reduce(ADD))                 # 局部指数和
4. hook_fn()（若提供）—— 这里是 cluster_wait，等所有 CTA 到齐
5. 若需要跨 warp/cluster 合并（warps_per_row>1 或 cluster_n>1）：
   a. 把 (max_x, sum_exp_x) 用 f32x2_to_i64 打包成一个 64 位整数
   b. 经归约缓冲 + mbarrier 做跨 warp/cluster 交换
   c. 合并：m = max(所有段)；s = Σ s_k * exp(m_k - m)        # 重缩放公式
   d. 若 return_exp_x：exp_x *= exp(max_x - m_final)          # 把本段 exp 重新对齐到全局 max
6. 返回 (max_x, denom=sum_exp_x, exp_x)
```

关键巧思有二：

- **`exp2` 代替 `exp`**：GPU 的 `exp2`（\( 2^x \)）比 `exp`（\( e^x \)）快，所以用 \( e^{x-m} = 2^{(x-m)\log_2 e} \)，即 `exp2(x*log2_e - max_x*log2_e)`。
- **`f32x2_to_i64` 打包**：跨 CTA 通信时，把 max 和 sum 两个 f32 拼成一个 i64 写入归约缓冲。这样一次 STAS（shared-memory arrive-store）就同时投递了两个值，mbarrier 只需 arm 一次、wait 一次，把通信往返从「每值一次」降到「总共一次」。

> 数学复习：合并两段 \( (m_1, s_1) \)、\( (m_2, s_2) \)：
> \[ m = \max(m_1, m_2), \quad s = s_1 e^{m_1 - m} + s_2 e^{m_2 - m} \]
> 多段推广即代码里的 `Σ s_k * exp(m_k - m_final)`。

#### 4.3.3 源码精读

`kernel` 里的 online 分支调用：

[quack/softmax.py:163-171](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L163-L171) —— 一行调用拿到 `(max_x, denom, exp_x)`；`reduction_buffer[None, None, 0]` 选第 0 槽；`hook_fn=cluster_wait`（仅 cluster 时）让所有 CTA 在合并前同步。

对照非 online 分支（两次独立 `row_reduce`）：

[quack/softmax.py:143-162](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L143-L162) —— 先 MAX（L144-152，用 `reduction_buffer[...,0]`），用 `exp2` 算 `exp_x`（L153-154），再 ADD（L155-162，用 `reduction_buffer[...,1]`，即第二槽）。注意非 online 用了 `stage=2` 的两个槽。

`online_softmax_reduce` 的 warp 内局部归约：

[quack/reduce.py:282-294](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L282-L294) —— 先 `x.reduce(MAX)`（每 lane 片段内 MAX）→ `warp_reduce(fmax)`（warp 蝶形）；再用 `log2_e` 算 `exp_x`；`exp_x.reduce(ADD)` → `warp_reduction(add)` 得局部和。

跨 CTA 合并：打包、远程写、重缩放：

[quack/reduce.py:324-342](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L324-L342) —— `f32x2_to_i64(max_x, sum_exp_x)` 打包；warp0 用 `mbarrier_arrive_and_expect_tx` 武装屏障并预报字节数；各 CTA 用 `store_shared_remote` 把打包值写到 peer CTA 的 smem（带 `peer_cta_rank`）；`mbarrier_wait` 等待。

合并与重缩放求和：

[quack/reduce.py:343-366](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L343-L366) —— 读回各段的 `(m_k, s_k)`；`max_x_final = warp_reduce(max of m_k)`；`sum_exp_x = Σ s_k * exp(m_k - max_x_final)`；若 `return_exp_x`，把本段 `exp_x` 乘以 `exp(max_x - max_x_final)` 重新对齐。

打包/解包工具：

[quack/utils.py:138-147](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/utils.py#L138-L147) —— `f32x2_to_i64` 把两个 f32 bitcast 成一个 i64；`i64_to_f32x2` 是逆操作。

`warp_reduce` 的 redux/shuffle 选择（u2-l4 详讲，此处只点出）：

[quack/reduce.py:21-89](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L21-L89) —— 硬件有 `redux.sync`（如 Int32 全部、SM100 的 fp32 min/max）就走单条指令，否则退回 shuffle 蝶形。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：跟踪 online 路径的「重缩放合并」，验证它与非 online 路径数学等价。
2. **步骤**：
   - 在 `reduce.py` 的 `online_softmax_reduce` 中找到合并公式 `sum_exp_x += sum_exp_x_single_warp[i] * exp(max_x_single_warp[i] - max_x_final)`（L359-362）。
   - 把它对照本讲 4.3.2 的多段合并公式 \( s = \sum_k s_k e^{m_k - m} \)，确认形式一致。
   - 再读 `softmax.py` 非 online 分支的两次 `row_reduce`（L144-162），确认它也得到同样的 \( m \) 与 \( \sum e^{x_j-m} \)，只是分两次通信。
3. **观察**：两条路径在数学上等价；差异在「通信次数」（online 1 次 vs 非 online 2 次）与「缓冲槽数」（1 vs 2）。
4. **预期结果**：你能用自己的话讲清「online 把 max 与 sum 的通信合并成一次，靠的是重缩放公式 + f32x2 打包」。

> 待本地验证：可在测试里把 `Softmax(dtype, N, online_softmax=False)` 手动构造一次（需写小脚本调用 `Softmax.compile`），与默认 online 版本对比输出是否一致（应 bitwise 一致或仅末位差异）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `online_softmax_reduce` 要求 `reduction_buffer` 是 `Int64` 类型？
**答案**：跨 warp/cluster 合并时要把 `(max_x, sum_exp_x)` 两个 f32 打包成一个 i64 写入缓冲（`f32x2_to_i64`），这样一次写、一次屏障等待就能同时投递两个值。代码里也显式断言 `reduction_buffer.element_type == Int64`。

**练习 2**：`return_exp_x=True` 时，合并后为什么要做 `exp_x *= exp(max_x - max_x_final)`？
**答案**：本段在 warp 内算 `exp_x` 时减的是「本段局部 max」，但最终分母要用「全局 max」。全局 max ≥ 局部 max，所以要把本段 `exp_x` 再乘以 \( e^{\text{局部max} - \text{全局max}} \) 重新对齐，否则分母与分子的参考点不一致，结果会错。

**练习 3**：`hook_fn=cluster_wait` 在归约里起什么作用？
**答案**：当 `cluster_n>1` 时，多个 CTA 各自算出本段的 `(max, sum)`，必须先到齐才能合并。`cluster_wait`（在 `row_reduce`/`online_softmax_reduce` 内部于 warp 归约之后调用）让当前 CTA 等待 cluster 内所有 CTA 都抵达，确保跨 CTA 写入的打包值已就绪，再继续读缓冲做合并。

---

## 5. 综合实践

**任务：为 `Softmax.kernel` 绘制一张完整的「数据流 + 边界 + 归约」时序图，并用一个小实验验证你的理解。**

1. **画图**：取一组具体参数（如 `bfloat16, N=4096, M=199, SM9x`），按 4.1.4 的方法算出 `cluster_n=4, threads_per_row=64, tiler_mn=(2, 1024), num_threads=128`。画出：
   - 4 个 CTA（cluster）如何沿 N=4096 切分（每 CTA 1024 列）。
   - 每个 CTA 内 128 线程 = 2 行 × 64 线程/行，如何并行搬运与归约。
   - 标出 gmem→smem→rmem→归约→rmem→gmem 七个阶段（对应 4.2.4 实践 B）。
   - 标出 `M=199` 时最后一个 CTA 行（行 198）如何被 `if tXcX[0][0] < 199` 守卫。
2. **实验**：再用 `N=760`（非整除）重画，标出哪些 CTA 的 tile 越界、`is_even_N=False`、`predicate_k` 与 `fill_oob(-inf)` 在哪里生效。
3. **验证**：运行
   ```bash
   pytest tests/test_softmax.py::test_softmax -x -k "bfloat16 and 760"
   pytest tests/test_softmax.py::test_softmax -x -k "bfloat16 and 4096"
   ```
   两组都应通过，证明整除与非整除两条边界路径都数值正确。
4. **反思**：把 `__init__` 里的 `online_softmax=True` 改为 `False`（仅在本地实验脚本里构造 `Softmax`，不要改源码），对比两次 launch 的不同——你会看到非 online 多用了一个归约缓冲槽、多一次跨线程通信。

> 这个任务把本讲三个最小模块（启动配置、加载与边界、online 归约）串成一条完整链路，做完后你应能独立向别人讲清「QuACK 的 softmax 前向内核从一行数据到一行概率经历了什么」。

---

## 6. 本讲小结

- `Softmax.__call__` 是主机侧编排者，由 `_set_cluster_n` + `_get_tiled_copy` 推出 grid/block/cluster，再 `.launch` 设备内核；构造时 online 与否决定归约缓冲的槽数（1 vs 2）与类型（Int64 vs Float32）。
- 设备内核 `kernel` 的数据流是 gmem →(cp.async)→ smem →(autovec)→ rmem →(归约)→ rmem →(store)→ gmem；注意前向走的是 cp.async 而非 TMA。
- 边界处理「双保险」：M 维用 `if` 守卫整行；N 维用 `predicate_k` 逐元素谓词 + `fill_oob(-inf)` 把越界 smem 位置填成 `-inf`，使其对 max/sum 归约零贡献。
- `is_even_N = (N == tiler_mn[1]*cluster_n)` 是判断是否需要谓词的编译期开关；非整除时必走谓词路径。
- online softmax 用「重缩放合并」\( s = \sum_k s_k e^{m_k-m} \) 把 max 与 sum 耦合在一次归约里，并用 `f32x2_to_i64` 把两个 f32 打包成一个 i64，使跨 CTA 通信只走一次 mbarrier 往返。
- 非 online 路径用两次独立 `row_reduce`（MAX 然后 ADD），数学等价但通信多一倍、缓冲多一槽。

---

## 7. 下一步学习建议

- **u2-l3（softmax 反向与 autograd）**：本讲只讲了前向。反向内核 `SoftmaxBackward` 复用同一套 `ReductionBase` 与 `row_reduce`，但需要双输入加载与一个 `dot` 归约，还涉及 `torch.autograd.Function` 包装。建议紧接着读。
- **u2-l4（归约原语：warp/row/online）**：本讲对 `warp_reduce`、`row_reduce`、`cluster_reduce` 只是点到为止。要彻底理解 redux.sync 与 shuffle 蝶形的选型、跨 warp mbarrier 同步、`f32x2` 打包的完整协议，请深入 `reduce.py`。
- **u3-l1（copy_utils）**：本讲的 `copy`、`tiled_copy_2d`、`predicate_k` 都来自 `copy_utils.py`，那里有更系统的拷贝原语讲解。
- **延伸阅读**：对照 FlashAttention 的 online softmax 推导，能加深对「重缩放合并」为何能省通信的理解。
