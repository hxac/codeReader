# u8-l3 Cluster Launch Control：动态认领工作

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说明静态 persistent 调度器（static persistent scheduler）在什么条件下会出现「尾部空闲」，以及为什么它无法在运行中重新分配工作。
2. 描述一次 CLC 请求的完整生命周期：谁发起 `try_cancel`、硬件把什么写到哪里、如何用 `is_canceled` 与 `get_first_ctaid` 消费结果、失败后必须做什么。
3. 解释 CLC 应答为什么必须走 mbarrier（16 字节、一次到达、tx-count 记账），以及「先请求、后计算」如何隐藏调度延迟。
4. 对一个具体内核判断：值得用静态公式还是值得引入 CLC。

本讲是「异步协调」单元的收官。前两讲解决的是**数据与计算**的异步交接（mbarrier 状态机、phase 复用），本讲把同一套 mbarrier 机制用在一件新事情上：**向硬件调度器异步地要工作**。

## 2. 前置知识

本讲默认你已读过前置讲义 u2-l2（内存空间）与 u8-l2（phase 相位与 stage 复用）。用到的概念先通俗地过一遍：

- **grid 与 launch**：一次内核启动（launch）会创建一个 grid，grid 里每个 CTA 有自己的坐标 `blockIdx`。GPU 不会一次性跑完 grid 里所有 CTA——它先启动一部分，等有 CTA 退出释放资源后再启动后面的。**尚未启动的 CTA 就排在发射队列（launch queue）里**。
- **persistent 内核**：与其为每个输出 tile 启动一个 CTA，不如只启动固定数量的一批常驻 CTA，让每个 CTA 在循环里连续处理多个 tile，从而摊薄初始化开销。一个已经跑起来、能反复领任务的 CTA 或 cluster，本讲称为 **worker**。
- **cluster**：Hopper 起支持的线程块簇，是若干一起调度、可在 cluster 范围同步并互访 DSMEM 的 CTA（见 u2-l2）。CLC 的调度单位可以是单个 CTA，也可以是一整个 cluster。
- **mbarrier 与 phase**：共享内存中的硬件同步对象，同时维护线程到达计数和在途字节计数（tx-count），两者都清零才算一个 phase 完成，随后屏障翻转相位。见 u8-l1。
- **phase 复用**：同一道屏障被反复使用时，软件必须自己翻转所等待的奇偶值，否则要么提前放行读到旧数据，要么死锁。见 u8-l2。
- **尾部（tail）与 work stealing（工作窃取）**：内核接近结束时，只剩少数 worker 还在干活、其余 SM 闲置的阶段叫尾部；让先完工的 worker 把排队中的工作「偷」过来做，就是工作窃取。CLC 是它在 Blackwell 上的硬件实现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [chapter_clc/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md) | 本讲主源码：CLC 章正文，含静态调度局限、请求协议、重叠循环与选型建议。本章没有配套 `.py` 内核，是纯概念章。 |
| [chapter_gemm_async/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md) | GEMM Step 6（persistent 内核 + tile scheduler）所在章，本讲用它做静态 persistent 调度的真实代码对照。 |
| [chapter_flash_attention/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md) | FA4 章，其「Tile Scheduling」小节展示了「tile 代价不均」的真实来源（causal 掩码），是本讲选型讨论的实例。 |
| [README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md) | 全书结构说明，确认 CLC 属于 Part I「Understanding the GPU」的高级调度主题。 |

CLC 章在全书 toctree 中的位置紧跟异步屏障章之后（[index.md:56-57](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/index.md#L56-L57)），README 中也把「advanced scheduling (CLC)」列为 Part I 的收尾主题（[README.md:18-20](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L18-L20)）——也就是说，它是与 mbarrier 同级的「异步协调」基础设施，而不是某个 GEMM 步骤的附属技巧。

## 4. 核心概念与源码讲解

### 4.1 persistent 内核与静态调度器的尾部空闲

#### 4.1.1 概念说明

前几章都在回答「一个 tile 怎么算」：A、B 怎么进 SMEM，Tensor Core 怎么执行 MMA，异步操作怎么通过 mbarrier 交接。当输出矩阵被切成很多 tile 之后，出现了另一个问题：**这些 tile 按什么顺序、分给哪些 CTA 或 cluster？**

[chapter_clc/index.md:12-16](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L12-L16) 给出两种基本答案：

1. **一个 tile 一个 CTA**：100 个 tile 就启动 100 个 CTA，CTA i 算 tile i。GPU 分批启动它们，直到全部算完。每个 CTA 都要自己做一遍初始化、算完一个 tile 就退出。
2. **persistent 内核**：启动固定一批长寿命 CTA/cluster，每个 worker 在循环里算多个 tile。省掉了重复的启动与初始化开销，但立刻引出新问题：worker 算完当前 tile 后，**下一个 tile 从哪里来？**

最简单的答案是静态调度：开工前就用公式把每个 worker 的 tile 列表定死。它的问题是**承诺得太早**——执行还没开始就把分配定死了，运行中无论发生什么都不能再调整。

#### 4.1.2 核心流程

CLC 章用一个 12 tile、4 worker 的例子说明静态分配（[chapter_clc/index.md:24-31](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L24-L31)）：

```text
worker 0: tile 0, 4, 8
worker 1: tile 1, 5, 9
worker 2: tile 2, 6, 10
worker 3: tile 3, 7, 11
```

这种 grid-stride 式分配在两个前提同时成立时工作良好：四个 worker 真的能同时跑，且各 tile 代价大致相同。而这两个前提都可能被打破（[chapter_clc/index.md:33-35](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L33-L35)）：

- **SM 可用性不可预知**。内核启动时不知道有多少 SM 空闲——可能另一个内核正占着一部分 GPU。若 worker 3 被延迟，worker 0/1/2 可能在 worker 3 还没开始 tile 3、7、11 之前就干完了自己的全部三个 tile 并退出，留下一个 worker 独自执行漫长的发射尾部。
- **tile 代价不均**。边界处理、掩码、稀疏计算或融合在 GEMM 周围的工作都可能让某些 tile 明显更慢。

把这件事写成公式：设 tile \(i\) 的代价为 \(c_i\)，worker 集合为 \(W\)，静态分配 \(A(w)\) 把 tile 集合划给各 worker，则总完工时间（makespan）由最重的 worker 决定：

\[
T_{\text{static}} = \max_{w \in W} \; t_{\text{start}}(w) + \sum_{i \in A(w)} c_i
\]

而任何调度（包括 CLC）的下界是总工作量除以并行度：

\[
T_{\text{ideal}} = \frac{\sum_i c_i}{|W|}
\]

静态分配的问题在于 \(A(w)\) 在 \(t=0\) 就冻结，一旦某个 worker 起步晚或某组 tile 偏贵，\(T_{\text{static}}\) 与 \(T_{\text{ideal}}\) 之间就会出现只能干瞪眼的空隙；CLC 做的事就是允许运行中把还没被启动的 tile 挪给先空闲的 worker，把这段空隙压小。

#### 4.1.3 源码精读

静态 persistent 调度器在书中的真实实现是 GEMM Step 6 的 `ClusterPersistentScheduler2D`：

- [chapter_gemm_async/index.md:462-464](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L462-L464)：Step 5 每个 \(128\times128\) 输出 tile 启动一个 CTA，\(4096\times4096\) 要 1024 个 CTA；Step 6 改为固定数量的 CTA 顺序处理多个 tile，把初始化摊到多个 tile 上，并把 tile 分配搬进内核内部。
- [chapter_gemm_async/index.md:473-477](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L473-L477)：`SM_COUNT=148`，启动 148 个 persistent CTA，每个从调度器取 tile、算完再取。注意书里明确说了局限的另一半——`SM_COUNT` 决定启动多少 CTA，但 **occupancy 与硬件调度决定其中多少能同时驻留**，没有任何 CTA 被永久绑定在某个 SM 上。也就是说「148 个 worker 是否真的同时跑」本身就不由内核控制。
- [chapter_gemm_async/index.md:479-490](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L479-L490)：调度器在内核里构造，`l2_group_size=8` 让 tile 编号按对 L2 友好的顺序排列。**tile 顺序是调度器预先算好的公式**，不依赖任何运行时完成信息。

把这段代码与 CLC 章的静态例子对照：`ClusterPersistentScheduler2D` 就是「worker ID + 迭代轮数推出下一个坐标」这种静态公式的工业版，它优化的方向是 L2 局部性，而**没有**处理「worker 起步晚 / tile 代价不均」——那正是 CLC 章要补的缺口。

顺带一提，「tile 代价不均」并不是假设。FA4 章的调度小节写道：causal 掩码让任务代价天然不均——靠前的 Q block 可能只访问一个 K/V block，靠后的要访问全部（[chapter_flash_attention/index.md:839-842](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L839-L842)），所以 FA4 为 causal 模式专门设计了重排 m_block 顺序的 `FlashAttentionLPTScheduler` 来缓解尾部负载不均。这是「负载不均是真实问题」的书中证据，我们在 4.4 再回到它。

#### 4.1.4 代码实践

**实践目标**：亲手把「静态分配 + worker 延迟 → 尾部空闲」变成一张数字表。

**操作步骤**（源码阅读 + 手推，无需 GPU）：

1. 阅读 [chapter_clc/index.md:24-35](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L24-L35)，把 12 tile / 4 worker 的 grid-stride 分配表抄下来。
2. 给每个 tile 记代价：tile 0–5 记 1 个单位，tile 6–11 记 3 个单位（模拟掩码/边界让后半批 tile 更贵）；再假设 worker 3 因为别的内核占用 SM，到第 4 个单位时间才起步。
3. 手算每个 worker 的完工时刻与整个 grid 的 makespan，并回答：从哪个时刻起只剩 worker 3 一个在干活？它独占了多久？

**需要观察的现象**：worker 0/1/2 早早干完退出，机器的大部分算力在最后一段里闲置；而这一切在分配公式定下的瞬间就注定了。

**预期结果**：worker 0/1 各自在第 5 个单位时间完工（1+1+3），worker 2 在第 7 个完工（1+3+3），worker 3 从第 4 步起步、第 11 个单位时间才完工；从第 7 到第 11 个单位时间只有 worker 3 活跃，独占 4 个单位时间。总工作量 24、理想并行下界是 6，静态方案做到 11。

#### 4.1.5 小练习与答案

**练习 1**：把上例中 worker 3 的起步延迟改成 0（四个 worker 同时起步），静态分配的 makespan 变成多少？这说明尾部问题只跟「起步晚」有关吗？

**答案**：worker 3 的完工时刻变为 \(0+1+3+3=7\)，makespan 为 7（worker 2 同样是 7）。可见起步延迟消除后，**代价不均本身**仍让 makespan 停在 7，而理想下界是 \(24/4=6\)。两个因素（起步晚、代价不均）各自独立地拉大 \(T_{\text{static}}\) 与 \(T_{\text{ideal}}\) 的差距。

**练习 2**：为什么静态 persistent 调度器不能在运行中发现「worker 3 还没起步」然后把它的 tile 挪给别人？

**答案**：因为分配在执行前就由公式（worker ID + 迭代轮数）完全确定，内核里没有任何一处代码观察其它 worker 的完成状态；CTA 之间也没有为此准备通信或同步。要运行中重分配，需要一个所有 worker 都能访问的「下一个任务」来源——这正是 CLC 提供的东西：把「下一个坐标」的裁决权交给网格调度器硬件。

### 4.2 CLC：取消未启动的 launch 并继承坐标

#### 4.2.1 概念说明

CLC（Cluster Launch Control）是 Blackwell 提供的硬件机制，它对「下一个 tile 从哪来」给出了第三种答案：

> CLC 内核仍然启动一个覆盖完整输出空间的 grid，但**正在运行的 worker 可以取消一个还没开始执行的 CTA/cluster launch，并继承它的坐标**（[chapter_clc/index.md:18](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L18)）。

它的精妙之处在于利用了 grid 的一个既有事实：**每个 CTA 的 `blockIdx` 坐标本来就被内核当作任务 ID 使用**（CTA i 负责解码坐标 i 去算 tile i）。于是「转移工作」不需要搬任何数据或状态，只需要转移一个坐标整数。

关键性质（[chapter_clc/index.md:41-43](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L41-L43)）：

- 被取消的 CTA **从未启动过**，没有寄存器状态、没有执行状态需要迁移；硬件只把坐标 3 交给 CTA 0。
- 每个坐标**恰好被处理一次**：它的 CTA 要么正常启动，要么 launch 先被取消、坐标交给某个已在运行的 worker。二者必居其一。
- 只要发射队列里还有可取消的坐标，刚空闲下来的 worker 就能继续算，而不必等某个「命中注定」的 CTA 被调度起来。

这就是 CLC 意义下的工作窃取：**把一个 pending launch 的坐标重新指派给运行中的 worker**。

#### 4.2.2 核心流程

以章中 12 tile 的例子走一遍（[chapter_clc/index.md:37-43](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L37-L43)）：

```text
grid: 12 个 CTA，blockIdx = 0..11，CTA i 负责解码坐标 i → tile i
初始：资源只够先跑 3 个，调度器启动 CTA 0、1、2
      CTA 3..11 留在发射队列中 pending

t1: CTA 0 算完 tile 0，不退出，向硬件请求「还没启动的 launch」
t2: 调度器选中 CTA 3 → 取消 CTA 3 的 launch，返回它本要使用的 blockIdx
t3: CTA 0 解码该坐标 → 算 tile 3 → 再发起下一次请求 ……
```

如果调度单位是 cluster，则一次请求接管**一整个 cluster**，`get_first_ctaid` 返回的是 cluster 中第一个 CTA 的坐标，cluster 内各 CTA 再用它组合自己的本地 block index 恢复各自的 grid 坐标（[chapter_clc/index.md:45](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L45)、[chapter_clc/index.md:77](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L77)）。注意书里特意澄清：CLC 动态调度的是 CTA/cluster **坐标**，与 cluster 执行模型本身（cluster scope 同步、DSMEM）是两回事。

#### 4.2.3 源码精读

一次 CLC 请求涉及三条指令，全部来自「One CLC request」一节：

1. **提交请求**：[chapter_clc/index.md:51-55](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L51-L55)。`clusterlaunchcontrol.try_cancel.async` 请求网格调度器取消一个尚未开始执行的 CTA/cluster；硬件把应答编码成一条 **16 字节记录写入共享内存**。内核通常**选一个线程**提交；如果多个线程都发这条指令，就产生多个取消请求，必须为它们准备各自独立的应答位置，并在屏障的到达计数与 tx-count 里把每一个都记上账。
2. **查询是否成功**：[chapter_clc/index.md:61-63](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L61-L63)。`clusterlaunchcontrol.query_cancel.is_canceled` 返回一个谓词：`true` 表示调度器确实取消了一个 pending launch。
3. **取回坐标**：[chapter_clc/index.md:67-71](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L67-L71)。`clusterlaunchcontrol.query_cancel.get_first_ctaid` 取得被取消 CTA（或被取消 cluster 的第一个 CTA）的 \((x, y, z)\) 坐标，内核再把坐标换算成对应的输出 tile。

两条使用纪律务必记住（[chapter_clc/index.md:73-75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L73-L75)）：

- `is_canceled` 为 `false` 的常见原因是发射队列里已没有 pending 工作（也可能是调度器正准备调度更高优先级的内核）。**一旦观察到失败，worker 必须退出请求循环**——PTX 规定之后再发取消请求是未定义行为。
- CLC **不用哨兵数字表示「没有任务」**。`get_first_ctaid` 的返回值只在 `is_canceled == true` 时有效；请求失败后去查询坐标同样是未定义行为。判断顺序永远是「先验谓词、后取坐标」。

#### 4.2.4 代码实践

**实践目标**：把「坐标即任务 ID」这件事落到具体的解码函数上。

**操作步骤**（源码推演，无需 GPU）：

1. 设输出矩阵 \(M \times N = 256 \times 384\)，`BLK_M = BLK_N = 128`，则 num_m_tiles = 2、num_n_tiles = 3，grid 需要覆盖 \(2 \times 3 = 6\) 个坐标。写出一个一维 `blockIdx.x` → `(m_tile, n_tile)` 的解码函数（例如 `m_tile = bx // 3`、`n_tile = bx % 3`，也可以用列主序，任选一种并保持一致）。
2. 用 Python 实现这个 `decode(bx)`，打印 `bx = 0..5` 的解码结果。
3. 把 `decode` 包进 CLC 的任务循环骨架里：

```python
# 示例代码（非项目源码）：演示「坐标即任务 ID」的解码与循环骨架
def decode(bx, num_n_tiles=3):
    return bx // num_n_tiles, bx % num_n_tiles   # (m_tile, n_tile)

tile = decode(my_block_idx)          # 我自己的 blockIdx 就是第一个任务
while True:
    result = async_try_cancel()       # 请求下一个坐标（4.3 展开其协议）
    compute(tile)
    wait(result)
    if not is_canceled(result):
        break                          # 失败必须退出请求循环
    tile = decode(get_first_ctaid(result))
```

4. 再推演 cluster 情形：若调度单位是含 2 个 CTA 的 cluster、`get_first_ctaid` 返回 cluster 首个 CTA 的坐标 `first`，写出本 CTA 恢复自己坐标的式子（用 cluster 内本地编号 `%ctaid` 参与）。

**需要观察的现象**：静态内核与 CLC 内核可以用**完全相同**的 `decode` 与 `compute`——两种调度器只是「下一个 bx 从哪来」不同。

**预期结果**：步骤 2 打印 `(0,0),(0,1),(0,2),(1,0),(1,1),(1,2)`；步骤 4 中本 CTA 坐标 = 按 cluster 尺寸对 `first` 做行/块偏移后加上本地编号（对书中 2-CTA 的 1D 约定，即 `first + %ctaid` 这类形式；具体偏移方向取决于 grid 维度的排布方式，**待本地验证**你的排布约定）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CLC 转移工作时不需要迁移任何寄存器或执行状态？

**答案**：因为被取消的 CTA 从未开始执行，根本没有产生过任何状态。硬件交出的只是一个本该由那个 CTA 使用的 `blockIdx` 坐标；而这个坐标在内核里本来就只是任务 ID，接手的 worker 用与当初相同的 `decode` 就能得到 tile。

**练习 2**：书中说「每个坐标恰好被处理一次」。请说明保证这一点的机制，以及为什么不会出现两个 worker 算同一个 tile。

**答案**：坐标的归宿只有两条互斥的路：它的 CTA 正常启动并处理它，或它的 launch 先被调度器取消、坐标被移交给某个运行中的 worker。取消动作由网格调度器统一裁决并从发射队列里摘除该 launch，因此一个坐标不可能既被自己的 CTA 执行、又被窃取；而窃取者拿到的是队列里同一个被摘除的坐标，也不会有两个窃取者同时得到它。

**练习 3**：如果内核在 `is_canceled` 返回 `false` 之后仍然调用 `get_first_ctaid`，会发生什么？

**答案**：按 [chapter_clc/index.md:75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L75) 这是未定义行为。CLC 没有「无任务」哨兵值，失败请求的应答记录里不保证有合法坐标，读它是没有意义的；正确写法是看到 `false` 就退出请求循环。

### 4.3 异步请求与 mbarrier 应答

#### 4.3.1 概念说明

`try_cancel` 的全名里带着 `.async`——请求是异步的。worker 发出指令后可以继续算当前 tile，但**不能立刻去读共享内存里的应答**，因为硬件（经由 async proxy）可能还没把那 16 个字节写完。

这正是 u8-l1 的老问题在新场景下的复现：**「已发起」不等于「已完成」**，程序顺序与 `cta_sync` 只能同步线程，观察不到网格调度器的进度。交接依然要靠 mbarrier，而且记账方式与 TMA load 完全同构（[chapter_clc/index.md:57](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L57)）：

- 发起线程执行一次到达（`arrive.expect_tx` 风格），同时向 tx-count 登记 **16 字节**；
- 硬件写完 16 字节应答后 complete-tx 核减 16；
- 到达计数与 tx-count 同时归零，本 phase 完成，`try_wait` 返回。

与 u8-l1 中 fp16 双 128×64 tile 的 32768 字节相比，这里只有 16 字节——但机制一模一样，字节多少不影响协议。

#### 4.3.2 核心流程

worker 的第一个任务来自自己的 `blockIdx`，之后每次迭代五步（[chapter_clc/index.md:81-87](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L81-L87)）：

```text
tile = decode(blockIdx)                 # 初始任务：自己的坐标

while true:
    ① 尽早提交 try_cancel，请求可能的下一个任务
    ② 计算当前 tile（请求在途，与之重叠）
    ③ tile 算完后，在 CLC 请求的 mbarrier 上等待
    ④ 查询 is_canceled 判断取消是否成功
    ⑤ 成功则带着返回的坐标继续，失败则退出
```

为什么要在**计算之前**发请求（[chapter_clc/index.md:105-107](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L105-L107)）？网格调度器处理请求需要时间；若等 tile 算完才发请求，这段延迟就直接落在两个 tile 之间，worker 白白闲置。先发请求，让调度与计算重叠——到 tile 算完时，下一个任务的坐标往往已经躺在共享内存里了。书里的类比很精确：**TMA 用流水线把数据搬运延迟藏进计算，CLC 用同样的异步流水线思想把调度延迟藏进计算**。

还有一层 u8-l1 讲过的 proxy 细节（[chapter_clc/index.md:109](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L109)）：CLC 应答经 **async proxy** 写共享内存，而普通线程经 **generic proxy** 查询它。mbarrier 等待确认的是「异步应答写入已完成」；真实代码还必须执行 PTX 要求的 proxy fence——**提交新请求之前**与**读完应答之后**各一次——来为应答缓冲区在这两个 proxy 之间建立顺序，防止后一次异步写与还在被读的数据竞争。此外屏障的 phase 管理与 CTA/cluster 级线程同步也一个都不能少。

#### 4.3.3 源码精读

书中给出的极简伪代码在 [chapter_clc/index.md:91-103](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L91-L103)：

```text
tile = decode(blockIdx)

while true:
    async_try_cancel(result, barrier)
    compute(tile)
    wait(barrier)

    if not is_canceled(result):
        break

    tile = decode(get_first_ctaid(result))
```

注意 L89 明确声明这段伪代码**省略了**屏障初始化、phase 更新与应答缓冲所需的 async-proxy fence，只展示「取任务」与「计算」之间的顺序关系。本讲 4.3.4 的实践就是把这些省略项按 u8-l1/u8-l2 的规则补齐。

回到本单元的知识主线做两处对照：

- **与 TMA load 的同构**：[chapter_clc/index.md:55-57](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L55-L57) 把 CLC 应答的记账等同于 TMA load——单线程发起、一次 arrive、按字节登记 tx。差异只在「写入者」从 TMA 引擎换成网格调度器，以及字节数是固定的 16。
- **与 u8-l2 的衔接**：这道屏障在每个循环迭代里被复用一次，所以等待侧必须维护本地 phase 奇偶并在每次成功等待后翻转（`phase ^= 1`）。漏翻的两个后果你在 u8-l2 已经推演过：等待侧提前放行读到上一轮的旧应答，或永久等待而死锁。这里读旧应答尤其危险——旧记录里的 `is_canceled`/坐标属于上一轮请求。

#### 4.3.4 代码实践

**实践目标**：把书中省略了同步细节的伪代码，扩写成一份「可以对照 PTX 心里过一遍」的完整请求-应答伪代码。

**操作步骤**（源码推演 + 写伪代码，无需 GPU）：

1. 重读 [chapter_clc/index.md:47-57](https://github.com/mlc-ai/modern-gpu-programming-for-mls/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L47-L57) 与 [chapter_clc/index.md:109](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L109)，列出完整协议需要的全部要素：应答缓冲、屏障（期望到达数）、phase、两次 proxy fence、单线程提交。
2. 写出如下形态的伪代码（**示例代码**，标注了书中伪代码未展开的部分）：

```text
# ---- 每个 worker 一次的初始化（书中伪代码省略）----
smem:  clc_result[16B]                  # 硬件写入的应答记录（async proxy 写）
smem:  clc_bar = mbarrier(expected_arrivals = 1)   # 只登记提交线程这一次到达
phase: T.int32 = 0

tile = decode(blockIdx)                 # 初始任务来自自己的坐标

# ---- 由一个被选出的线程提交请求（书中伪代码省略）----
if elected_thread:                      # 例如 warp_id==0 且 lane_id==0
    fence.proxy_async.shared::cta       # 提交新请求前所需的 proxy fence
    clusterlaunchcontrol.try_cancel.async(
        dst = clc_result, mbar = clc_bar)
    mbarrier.arrive.expect_tx(clc_bar, bytes = 16)  # 1 次到达 + 16 字节 tx-count

# ---- 主循环：请求在途时计算当前 tile ----
while true:
    compute(tile)                       # 与请求重叠执行

    if elected_thread:
        mbarrier.try_wait(clc_bar, phase)   # 到达数与 tx-count 同时清零才返回
        phase ^= 1                           # 复用屏障必须翻转（u8-l2）
    cta_sync（或 cluster_sync）          # 把结果发布给 worker 内其余线程

    if not clusterlaunchcontrol.query_cancel.is_canceled(clc_result):
        break                                # 失败必须退出请求循环（UB 纪律）
    fence（读完应答后的 proxy fence，书中伪代码省略）
    tile = decode(clusterlaunchcontrol.query_cancel.get_first_ctaid(clc_result))

    if elected_thread:                   # 为下一轮迭代重新提交请求
        fence.proxy_async.shared::cta
        clusterlaunchcontrol.try_cancel.async(dst = clc_result, mbar = clc_bar)
        mbarrier.arrive.expect_tx(clc_bar, bytes = 16)
```

3. 对照检查三件事：(a) 应答缓冲在「提交前」与「读完后」是否各有 fence；(b) 每次 `try_wait` 之后 phase 是否翻转；(c) `is_canceled` 是否永远先于 `get_first_ctaid`。
4. 回答：如果图省事让 4 个线程各发一次 `try_cancel` 共用一道屏障，`expected_arrivals` 与 tx-count 各要改成多少？

**需要观察的现象**：伪代码里「取任务」的异步协议（提交→重叠计算→等待→解码）与 Step 5 TMA 双缓冲的「预取→计算→等 full」结构完全同构，只是把数据预取换成了任务预取。

**预期结果**：(a)(b)(c) 三处都能对上；第 4 步答案是 `expected_arrivals = 4`、tx-count 登记共 \(4 \times 16 = 64\) 字节，且 4 个线程必须各自有独立的 16 字节应答位置（依据 [chapter_clc/index.md:55](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L55)）。

#### 4.3.5 小练习与答案

**练习 1**：如果把「发请求」从循环开头挪到 `compute(tile)` 之后，会损失什么？

**答案**：网格调度器处理请求的延迟不再与计算重叠，而是直接插在两个 tile 之间，worker 在每次换任务时都空转一段调度延迟（[chapter_clc/index.md:105-107](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L105-L107)）。正确性不变（最终仍能等到应答），损失的是吞吐。

**练习 2**：CLC 应答为什么不能靠 `cta_sync` 加「先睡一会儿再读」来代替 mbarrier？

**答案**：`cta_sync` 只同步线程，观察不到 async proxy 上硬件写入的进度（u8-l1 的核心结论）；「睡一会儿」则没有任何正确性依据——16 字节何时写完由网格调度器决定，与时间长短没有契约。mbarrier 的 tx-count 机制给出的是精确的因果信号：字节核减到零才算写完。

**练习 3**：同一道 CLC 屏障每迭代用一次，若忘记 `phase ^= 1`，最坏会发生什么？

**答案**：与 u8-l2 的分析一致，取决于翻转遗漏发生在等待侧的哪个方向：可能 `try_wait` 用旧 phase 立即「通过」，worker 读到上一轮的旧应答、把旧坐标再算一遍或提前判失败退出；也可能用新 phase 永远等不到，worker 卡死在等待里。两种都是静默错误，必须靠 PipelineState 式的 stage+phase 捆绑来防。

### 4.4 何时值得引入 CLC

#### 4.4.1 概念说明

CLC 不是免费的银弹。书末的选型结论非常克制（[chapter_clc/index.md:113-122](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L113-L122)）：静态调度与 CLC **可以共享完全相同的 tile 计算**，区别只在于下一个坐标怎么来——

```text
静态调度：从 worker ID 和迭代轮数推导下一个坐标
CLC 调度：从硬件接收一个 pending 的 CTA/cluster 坐标
```

静态调度几乎为零的取任务开销。SM 可用性稳定、tile 代价相近时，一个静态公式通常就够了。CLC 的价值在**资源可用性或 tile 代价难以预测**时兑现：别的内核占了部分 SM、或 tile 执行时间参差时，先完工的 worker 能认领 pending 坐标，缩短「只剩少数 worker 活跃」的发射尾部。

#### 4.4.2 核心流程

判断流程可以整理为一棵简单的决策树：

```text
tile 代价是否相近且可预知？
├─ 是 → SM 可用性是否稳定（独占 GPU、无并发内核）？
│        ├─ 是 → 静态公式即可（零取任务开销，且可为 L2 局部性定制顺序）
│        └─ 否 → 值得试 CLC（主要收益：消除起步晚造成的尾部）
└─ 否（掩码/边界/稀疏/融合导致参差）→ 值得试 CLC
        （主要收益：消除代价不均造成的尾部；可同时保留 L2 友好的坐标排列）
```

在 TIRx 中的落地方式（[chapter_clc/index.md:124](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L124)）：CLC 可以包装成一个**动态 tile 调度器**——GEMM 主循环与 epilogue 只接收「当前 tile 坐标」，不需要知道它来自静态公式还是 CLC 应答；计算不变，只替换供应坐标的调度器。这与 Step 6 中 `ClusterPersistentScheduler2D` 暴露 `valid()/next_tile()` 的循环接口（[chapter_flash_attention/index.md:846-855](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L846-L855) 展示的同形接口）正好对得上：调度器是实现细节，主循环只认接口。

一个值得注意的书中事实：FA4 的 causal 模式**没有**用 CLC，而是用 `FlashAttentionLPTScheduler` 重排 m_block 顺序（重的先调度）来缓解负载不均（[chapter_flash_attention/index.md:839-844](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L839-L844)）。这说明「负载不均」有多条出路——静态重排是零运行时开销的近似手段，CLC 是运行时精确手段——选型时两者都该放进备选。

#### 4.4.3 源码精读

- [chapter_clc/index.md:115-118](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L115-L118)：静态与 CLC 的差异被压缩成两行对照——坐标的来源不同，其余完全一致。
- [chapter_clc/index.md:120](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L120)：静态调度「几乎没有取任务开销」，条件好时静态公式往往足够。
- [chapter_clc/index.md:122](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L122)：CLC 在资源/代价难预测时更有用，收益是缩短发射尾部。
- [chapter_clc/index.md:124](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L124)：TIRx 中包成动态 tile 调度器，主循环与 epilogue 不感知坐标来源。

#### 4.4.4 代码实践

**实践目标**：为三个具体内核场景做一次 CLC 选型判断。

**操作步骤**（源码阅读，无需 GPU）：

1. 场景 A：Step 6 的 HGEMM，\(4096\times4096\)、`BLK_M=BLK_N=128`，独占 B200 跑基准。读 [chapter_gemm_async/index.md:473-477](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L473-L477)，判断静态 `ClusterPersistentScheduler2D` 是否够用。
2. 场景 B：同一个 GEMM，但与另一个常驻内核共享 GPU（部分 SM 被占）。再判断。
3. 场景 C：FA4 causal 模式，任务代价因掩码而参差。读 [chapter_flash_attention/index.md:839-844](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L839-L844)，先写下「如果换成 CLC 会有什么不同」，再对照书中实际选择（LPT 重排 + 每任务一个 CTA）。
4. 为三个场景各写一行结论：「静态够用 / 值得 CLC / 两者皆可，理由……」。

**需要观察的现象**：选型的驱动因素是「不确定性的来源」，不是内核类型；同一内核在不同运行环境下答案可以不同。

**预期结果**：A 静态够用（tile 代价均匀、独占 GPU，静态公式还能定制 L2 顺序）；B 值得试 CLC（SM 可用性不可预知，正是 [chapter_clc/index.md:122](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L122) 描述的场景）；C 属于「代价不均」情形，CLC 是合理候选，但书中实际选择了零开销的静态重排方案——说明答案不止一个，需要实测裁决（**待本地验证**：在有 Blackwell GPU 的环境对比两种方案的 makespan）。

#### 4.4.5 小练习与答案

**练习 1**：既然 CLC 能动态平衡负载，为什么不把所有内核都改成 CLC？

**答案**：静态调度几乎零取任务开销，且允许为 L2 局部性定制坐标顺序；CLC 每次取任务都要走一遍「提交请求→等 mbarrier→查询解码」的异步协议，还要管理 phase 与 proxy fence。SM 稳定、tile 代价相近时这些复杂度买不到收益（[chapter_clc/index.md:120-122](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L120-L122)）。

**练习 2**：书里说 TIRx 中可以把 CLC 包成动态 tile 调度器。这个设计为什么能保证「计算不变」？

**答案**：因为主循环与 epilogue 只依赖「当前 tile 坐标」这一个输入，坐标由静态公式还是 CLC 应答供应对它们是透明的（[chapter_clc/index.md:124](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L124)）。只要两种调度器暴露相同的循环接口（`valid()` / 取坐标 / `next_tile()`），替换就不触碰计算代码。

## 5. 综合实践

**任务：用 Python 模拟对比「静态 round-robin persistent 调度」与「CLC 式动态认领」在负载不均 + worker 延迟下的尾部行为，并把 CLC 请求-应答协议写成完整伪代码。**

这个任务把本讲三个模块串起来：4.1 的静态分配与尾部度量、4.2 的坐标认领语义、4.3 的异步请求协议。

第一步，跑下面的离散事件模拟（**示例代码**，非项目源码，无 GPU 也能运行）：

```python
# clc_vs_static.py —— 对比静态 round-robin 与 CLC 式动态认领（示例代码）
TILES = 12
WORKERS = 4
costs = [1.0] * 6 + [3.0] * 6      # tile 6..11 更贵：模拟掩码/边界/融合导致的不均
start = [0.0, 0.0, 0.0, 4.0]       # worker 3 被其他内核占用，第 4 个单位时间才起步

# 1) 静态 grid-stride：worker w 固定处理 tile w, w+WORKERS, ...
static_done = []
for w in range(WORKERS):
    t = start[w]
    for tile in range(w, TILES, WORKERS):
        t += costs[tile]
    static_done.append(t)

# 2) CLC 式动态认领：worker 一空闲就接管下一个尚未启动的坐标
#    （贪心近似：把 tile 按坐标顺序交给最早空闲的 worker，
#      这正是「free worker 认领 pending 坐标」的理想化语义）
busy = list(start)
for tile in range(TILES):
    w = min(range(WORKERS), key=lambda i: busy[i])
    busy[w] += costs[tile]
dyn_done = busy

print("static :", [round(x, 1) for x in static_done], "makespan =", max(static_done))
print("dynamic:", [round(x, 1) for x in dyn_done],  "makespan =", max(dyn_done))
```

**操作步骤**：

1. 运行程序，记录两种调度各自的 makespan 与各 worker 的完工时刻。
2. 为两种调度各记录一份「任务时间表」（哪个 worker 在哪些时间区间处理哪个 tile），据此自己补一个尾部指标，例如：对每个整数时刻统计活跃 worker 数，输出「活跃 worker 数 ≤ 1 的时间区间总长」。
3. 修改参数做两组敏感性实验：(a) `start = [0,0,0,0]`（无延迟，只剩代价不均）；(b) `costs` 全部相等（只剩延迟）。分别记录两种调度的差距，验证 4.1.5 练习 1 的手推结论。
4. 把 4.3.4 写好的 CLC 请求-应答伪代码贴在模拟结果旁边，逐行标注它对应模拟里的哪一步（提交请求 ↔ worker 在忙、等 mbarrier ↔ 认领时刻、is_canceled=false ↔ 队列已空、break ↔ worker 退出）。

**需要观察的现象**：动态认领把昂贵 tile 分散给了先空闲的 worker（包括迟到的 worker 3），使完工时刻的分布更均匀；静态方案里 worker 3 的三个 tile 全堆在自己身上，尾部独占明显。

**预期结果**：主参数下输出 `static: [5.0, 5.0, 7.0, 11.0] makespan = 11.0`、`dynamic: [8.0, 8.0, 5.0, 7.0] makespan = 8.0`（此代码完全确定，可手推复核）；实验 (a) 两方案分别约为 7.0 与 7.0——说明消除延迟后仅靠动态认领不再有收益；实验 (b) 差距重新拉开——说明延迟本身就是动态认领的收益来源。理想下界均为 \(24/4 = 6\)，两种方案都到不了，因为任务不可抢占且迟到 worker 造成了前期产能空缺。

## 6. 本讲小结

- 静态 persistent 调度器在开工前用「worker ID + 迭代轮数」冻结分配，SM 可用性波动或 tile 代价不均都会造成「少数 worker 独撑尾部」的空闲，且运行中无法修正。
- CLC 让运行中的 worker 取消一个尚未启动的 CTA/cluster launch 并继承其网格坐标；坐标本来就是任务 ID，因此转移工作零状态迁移，每个坐标恰好被处理一次。
- 一次请求 = 单线程提交 `try_cancel.async`，硬件把 16 字节应答写进共享内存；消费顺序固定为 mbarrier 等待 → `is_canceled` 谓词 → `get_first_ctaid` 坐标，失败后必须退出请求循环（否则未定义行为）。
- 应答完成检测与 TMA load 同构：发起线程一次到达 + tx-count 登记 16 字节；屏障逐迭代复用，必须维护 phase 奇偶，且读写应答缓冲要跨 async/generic 两个 proxy 加 fence。
- 「先请求、后计算」把网格调度器的裁决延迟藏进当前 tile 的计算里——CLC 用 TMA 同款流水线思想隐藏的是**调度**延迟。
- 选型：SM 稳定、tile 代价相近用静态公式；资源或代价难预测时 CLC 才划算；TIRx 里 CLC 可包成动态 tile 调度器，主循环与 epilogue 不感知坐标来源。

## 7. 下一步学习建议

本讲结束了「异步协调」单元（mbarrier → phase 复用 → CLC），也结束了 Part I 硬件篇的全部机制铺垫。接下来：

1. **进入 Part II / 编程模型**：按大纲继续学 u9-l1（TIRx 是什么与第一个内核 hgemm_v1），把本单元的硬件机制放回 TIRx 的 scope/layout/dispatch 三要素里看。
2. **看静态调度器的工业实现**：精读 [chapter_gemm_async/index.md:457-503](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L457-L503)（Step 6 persistent 内核 + `ClusterPersistentScheduler2D`），重点关注 `l2_group_size=8` 如何为 L2 局部性定制坐标顺序，以及「每 tile 屏障使用次数为偶数所以 phase 可清零」的约定——那是 u8-l2 相位规则在真实内核里的直接应用。
3. **看负载不均的静态解法**：读 FA4 章的 Tile Scheduling 小节（[chapter_flash_attention/index.md:837-857](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L837-L857)），比较 `FlashAttentionLinearScheduler` 与 `FlashAttentionLPTScheduler` 两种策略，思考它们与 CLC 的关系。
4. 有 Blackwell GPU 时，可在安装 tvm.tirx 后（见 u1-l3）尝试为 Step 6 内核把调度器替换成 CLC 版本，用 u15-l4 将学到的基准方法对比 makespan——这也可以作为 u16-l2 毕业实践的候选题目。
