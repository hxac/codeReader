# 重叠、Occupancy 与优化阶梯

## 1. 本讲目标

本讲是单元三「性能模型与优化方法」的收尾。前两讲（u3-l1、u3-l2）解决了两个问题：性能上限怎么算（roofline 模型）、内存受限内核怎么优化（减字节、融合、复用）。本讲回答剩下的三个问题：

1. **优化阶梯**：从「一个能跑的内核」到「逼近屋顶的内核」，动作应该按什么顺序排？先做什么、后做什么、什么不做？
2. **重叠（overlap）**：对已经被判定为计算受限（compute-bound）的内核，剩下的差距来自哪里？如何用重叠减少计算路径上的空闲？
3. **Occupancy 与资源压力**：SMEM、寄存器、TMEM、warp 槽位如何限制一个 SM 上能同时驻留的 CTA 数？为什么现代 Tensor Core 内核常常故意选择低 occupancy？

学完本讲，你应该能给任何一个内核做一次「体检」：它在阶梯的哪一级、下一步最值得做什么动作、它在每个 SM 上能驻留几个 CTA、限制它的是哪一种资源。

## 2. 前置知识

本讲默认你已掌握前两讲的结论，这里只做最简回顾，不再重新推导：

- **Roofline 模型（u3-l1）**：内核性能上限取「峰值算力」与「带宽 × 算术强度」的较小者；B200 取整值约为 2 PFLOP/s 与 8 TB/s，拐点约 250 FLOP/byte。算术强度低于拐点为 memory-bound，高于拐点为 compute-bound。
- **内存受限优化（u3-l2）**：低算术强度内核的出路是减少 HBM 字节（融合、复用、更小 dtype）或把实际搬运率逼近带宽屋顶；一旦贴住带宽屋顶，计算侧优化零收益。

在此基础上，本讲引入四个新概念：

- **空闲（idle）**：硬件单元没有有用工作可做的时段。一个内核慢，往往不是因为它每个阶段都慢，而是因为各阶段轮流让硬件闲置。
- **重叠（overlap）**：让相互独立的阶段（加载、计算、回写）同时执行，使不同硬件单元同时忙碌。重叠不消除依赖，只是推进独立的工作。
- **Occupancy（占用率）**：一个 SM 上同时驻留的工作量。当一个 warp 阻塞时，调度器可以切换到另一个就绪的 warp，用「驻留更多 warp」来隐藏延迟——这是与显式重叠并列的另一条隐藏延迟途径。
- **资源压力（resource pressure）**：寄存器、SMEM、TMEM、warp 槽位、CTA 槽位对驻留 CTA 数的上限约束。资源用得越重，驻留的 CTA/warp 越少，occupancy 越低。

还需要两个已经建立的事实（分别来自 u2-l2 与 u2-l3，本讲直接使用）：

- B200 的 TMEM 是一个 128 lane × 最多 512 列 × 每列 32 bit 的二维片上空间，按列分配、须显式释放。
- 一个 GEMM tile 的生命期分三段：Load（GMEM→SMEM）、Compute（SMEM→TMEM 累加）、Epilogue（TMEM→寄存器→GMEM）；三段由 TMA 引擎、Tensor Core、CUDA core 分工执行，交接处需要完成信号。

## 3. 本讲源码地图

本讲的主战场是性能章的后四节；两个 GEMM 章提供真实内核作为分析对象。

| 文件 | 作用 |
|------|------|
| `chapter_performance/index.md` | 本讲核心：优化阶梯、重叠、occupancy 与资源压力、roofline 三步分析法 |
| `chapter_gemm_basics/index.md` | Step 1 单 tile 内核 `hgemm_v1` 的完整源码，用于 occupancy 估算的第一个对象 |
| `chapter_gemm_advanced/index.md` | Step 7 warp 特化内核的角色/屏障表、PIPE_DEPTH 的 SMEM 成本、九步端到端性能表 |

阅读建议：先通读 `chapter_performance/index.md` 的 L248–L344（四个小节连起来正好是本讲全部内容），再带着问题去看两个 GEMM 章。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：优化阶梯、重叠、occupancy 与资源压力。

### 4.1 优化阶梯

#### 4.1.1 概念说明

Roofline 模型告诉你天花板有多高，但**不告诉你怎么盖房子**。一个大 fp16 GEMM 在理论上可能是 compute-bound——这只说明 HBM 带宽不是主要限制，绝不说明随便一个实现就能达到 Tensor Core 的算力屋顶。书中原文点明了这一点：缩小差距需要「正确的指令、布局、staging、同步与调度」（the right instructions, layouts, staging, synchronization, and scheduling）。

优化阶梯就是把这条漫长的路排成有先后的台阶。本手册把书中 GEMM 篇章的做法总结为四级：

| 阶梯 | 目标 | 对应 GEMM 步骤 | 改变的东西 |
|------|------|----------------|------------|
| ① 更好的算法 | 抬高算术强度、减少总工作量 | 算法选择（分块、融合、小 dtype） | 数学层面搬多少字节、做多少 FLOP |
| ② 更高并行 | 让整个 GPU 都有活干 | Step 2 K 循环、Step 3 空间分块 | 工作怎么切给多个 CTA |
| ③ 更合适的引擎 | 每类工作交给专职硬件 | Step 4 TMA、贯穿全书的 tcgen05 | 数据搬运与计算的 dispatch 路径 |
| ④ 重叠 + 资源调优 | 消除引擎间相互等待 | Step 5–9 | 调度、角色划分、缓冲深度 |

关键在于：**阶梯上低级的动作不做，高级动作无从谈起**。不先分块就没有 tile 可流水；不先上 TMA，加载就不能真正与计算异步。反过来说，低级动作做完之后，收益的边际会递减——书中九步性能表显示，前半段（算法/并行/引擎）吃掉了约 142 倍加速，后半段（重叠与调度）再吃掉约 5 倍，最终对齐 cuBLAS。

还有一个容易被忽视的事实：**有些结构性改动不会立刻带来性能**。书中明确警告，warp specialization（Step 7）这类改动可能暂时增加资源占用、不立即提速，但它为后续更精细的重叠（Step 8 的双 CTA、Step 9 的多消费者）提供了结构。阶梯不是「每上一级都必须立刻涨分」的楼梯，而是「先把骨架搭对，再逐级兑现」的脚手架。

#### 4.1.2 核心流程

用书中 GEMM 九步给阶梯「对号入座」：

```text
Step 1  同步加载 + MMA        —— 骨架：GMEM→SMEM→TMEM→RF→GMEM 数据路径打通（正确性优先）
Step 2  K 循环累加            —— ② 更高并行：K 维切分，TMEM 累加器复用
Step 3  空间分块              —— ② 更高并行：M/N 切分，grid 覆盖整个输出
Step 4  TMA 异步加载          —— ③ 更合适的引擎：整块搬运交给 TMA 硬件引擎
Step 5  软件流水线            —— ④ 重叠：双缓冲 SMEM 环，加载与计算错开
Step 6  持久内核              —— ④ 重叠/调度：常驻 CTA + tile scheduler
Step 7  warp 特化             —— ④ 重叠：TMA/MMA/回写拆给并发角色
Step 8  双 CTA cluster        —— ④ 重叠 + 复用：跨 CTA 协作 MMA
Step 9  多消费者              —— ④ 重叠 + 复用：两个 MMA 消费者共享 B tile
```

每一步都保持同一个基本算法（D = A·Bᵀ），改变的只是「tile 怎么搬、怎么算、怎么调度」。这正是阶梯的精髓：**算法定了上限，阶梯逐级把实现推向那个上限**。

#### 4.1.3 源码精读

**优化阶梯的定义**，见[chapter_performance/index.md:L248-L270](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L248-L270)。这段是本模块的总纲：roofline 只给上限不给实现；第一个大跳变是从线程搬运（thread-copy）到 TMA 搬运；此后的优化都在回答「如何减少数据搬运、Tensor Core 计算与 epilogue 之间的相互等待」；最后警告结构性改动（warp specialization）可能暂时不涨性能。

**九步的实测结果与四段归因**，见[chapter_gemm_advanced/index.md:L862-L896](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L862-L896)。B200 上 M=N=K=4096：Step 1 的 70 ms 一路降到 Step 9 的 0.094 ms，与 cuBLAS 参考持平。四个比较区间：

- Step 1 → Step 4：70 ms → 0.49 ms，约 142×（含 K 循环、空间分块、多 CTA 并行与 TMA，不能全记在 TMA 头上）；
- Step 4 → Step 7：0.49 ms → 0.23 ms，约 2.2×（软件流水线 + 持久调度 + warp 特化）；
- Step 7 → Step 8：0.23 ms → 0.104 ms，约 2.2×（双 CTA 协作 MMA 提高暂存操作数复用）；
- Step 8 → Step 9：0.104 ms → 0.094 ms，约 10%（第二个 MMA 消费者复用同一批 B tile）。

注意两个细节，避免误读表格：Step 1 的 70 ms 来自**同数据路径的全矩阵基线**，不是单 tile 内核 `hgemm_v1` 的一次运行（[chapter_gemm_advanced/index.md:L879-L881](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L879-L881)）；Step 2/5/6 是中间版本，表中以破折号略去。

**roofline 三步分析法**收束整个性能章，见[chapter_performance/index.md:L327-L340](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L327-L340)：①估算算术强度；②与拐点比较判断受限类型；③测量离相关屋顶的距离，优化**真正绑定的资源**。这套流程就是「给内核定级、再选动作」的操作化表达。

#### 4.1.4 代码实践

**实践目标**：把书中的 GEMM Step 1 放到优化阶梯上，说出它缺什么、下一个最值得做的动作是什么。

**操作步骤**（源码阅读型实践）：

1. 打开 [chapter_gemm_basics/index.md:L238-L254](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L238-L254)，确认 Step 1 的加载方式：`Tx.cta.copy` 由 CTA 的全部线程同步搬运 A、B 两个 tile，搬完 `cta_sync()` 之后才发起 MMA。
2. 打开 [chapter_gemm_advanced/index.md:L12-L14](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L12-L14)，对照单 warpgroup 内核的问题描述：加载、计算、回写的控制集中在同一组 warp，等数据时 Tensor Core 没活干，算的时候 TMA/搬运路径闲着。
3. 填写下面这张「阶梯定位表」（以 Step 1 为例，答案已给出，请换 Step 7 再填一遍）：

| 问题 | Step 1 的回答 |
|------|---------------|
| 算法层面算术强度够吗 | 方阵 GEMM 的 AI ≈ N/3，远高于拐点 250（当 N 足够大），算法本身不是瓶颈 |
| 整个 GPU 有活干吗 | 否——单 CTA 只算一个 128×128 tile，grid 为 1×1 |
| 用对引擎了吗 | 部分——MMA 走 tcgen05，但搬运用 CTA 线程而非 TMA |
| 引擎之间有重叠吗 | 否——load→compute→store 串行，硬件轮流空闲 |
| 下一个最值得做的动作 | 上阶梯②：切 K 循环 + 空间分块，让多个 CTA 并行（对应 Step 2/3） |

**需要观察的现象**：串行内核里三类引擎的空闲——加载阶段 Tensor Core 空闲，MMA 阶段搬运路径空闲，回写阶段两者都空闲。

**预期结果**：Step 1 位于阶梯②以下——它刚打通数据路径（正确性），既没有多 CTA 并行，也没有引擎间重叠；性能表上同路径的全矩阵基线要 70 ms，而做完②③④之后的 Step 9 只要 0.094 ms。待本地验证：有 Blackwell GPU 时可按 u1-l3 的回路分别编译 Step 1 与后续步骤并计时对比；无 GPU 时以九步性能表为依据做推演。

#### 4.1.5 小练习与答案

**练习 1**：为什么「理论 compute-bound」不等于「实现能达到算力屋顶」？

**参考答案**：compute-bound 只说明 HBM 带宽 × 算术强度已超过峰值算力，即内存屋顶不再是主要限制；但实现要真正达到算力屋顶，还需要正确的指令（tcgen05）、正确的布局（SMEM/TMEM 摆放）、staging（多级缓冲）、同步（mbarrier）与调度（流水线/角色划分），任何一处不对都会让 Tensor Core 出现空闲，性能落在屋顶之下。

**练习 2**：性能表中 Step 7 → Step 8 又涨了约 2.2×，但这属于阶梯的哪一级？改变了什么、没改变什么？

**参考答案**：属于第④级（重叠与复用），更准确地说是「用更大的协作范围换复用」。改变的是执行 scope——MMA 从单 CTA（cta_group::1）变为 CTA 对（cta_group::2），两个 CTA 各存 A/B 切片并经 DSMEM 互访，暂存的操作数参与约两倍的计算；没变的是算法本身、三段式数据路径与各引擎的分工。

**练习 3**：既然 warp specialization（Step 7）在表里只带来 Step 4→7 区间的一部分收益，为什么还要在 Step 7 就引入它？

**参考答案**：因为它是结构性改动，为后续重叠提供骨架——先有独立的 producer/consumer 角色，Step 8 的跨 CTA 屏障交接和 Step 9 的第二个消费者才有挂载点。书中也提醒这类改动可能暂时增加资源占用而不立即提速；阶梯的排序原则是「先把结构搭对，再逐级兑现性能」。

### 4.2 重叠：减少计算路径空闲

#### 4.2.1 概念说明

一个已经 compute-bound、已经在用 Tensor Core 的内核，离算力屋顶的剩余差距来自哪里？来自**一条或多条执行路径没有被充分利用的时段**——也就是空闲。

最朴素的内核按顺序执行：

```text
load tile k
compute tile k
store tile k
load tile k + 1
compute tile k + 1
store tile k + 1
```

这个调度让硬件轮流闲置：加载时 Tensor Core 在等，计算时搬运引擎闲着，回写时两者都在等落盘。流水线内核则设法把相互独立的阶段凑到同一时刻：

```text
load tile k + 1
compute tile k
store tile k - 1
```

在 Blackwell 上，这三个阶段分别由 TMA、`tcgen05.mma` 与 epilogue/store 路径执行，`mbarrier` 负责在它们之间协调完成信号与缓冲所有权（这与 u2-l3 建立的三段式流水线完全对应，本讲补充的是「为什么必须这样调度」）。

必须强调的边界：**重叠不消除依赖**。tile k 的 MMA 仍然必须等 tile k 加载完，epilogue 仍然必须等 MMA 完成；内核能做的，是利用等待的空档推进独立的工作——预加载 tile k+1、回写 tile k-1。稳态下吞吐由最慢的一段决定（短板效应），所以重叠的收益上限是把「三段串行时间」压缩到「最慢一段的时间」。

#### 4.2.2 核心流程

重叠的前提是「多个角色各自推进」。以 Step 7 为例，控制流可以概括为：

```text
角色划分（2 个 warpgroup，8 个 warp）:
  TMA producer   (WG1 warp3): 循环 { 等 SMEM stage 空闲 → TMA 加载 A/B 到 stage s }
  MMA consumer   (WG1 warp0): 循环 { 等 stage s 数据就绪 → 发起 tcgen05 MMA }
  writeback      (WG0 全部): 循环 { 等 TMEM 结果就绪 → 读寄存器 → 转 fp16 → TMA store }

稳态（同一时刻三个角色分别处理不同 tile）:
  TMA 加载 tile k+1  ‖  MMA 计算 tile k  ‖  writeback 回写 tile k-1

交接协议（四道屏障，full/empty 各司其职）:
  tma2mma: "SMEM 数据就绪"     （full：TMA → MMA）
  mma2tma: "SMEM 缓冲可复用"   （empty：MMA → TMA）
  mma2ld: "TMEM 结果就绪"      （full：MMA → writeback）
  ld2mma: "TMEM 可供下个 tile" （empty：writeback → MMA）
```

关键机制有两条：

1. **full/empty 双向屏障**：前向路径报告「数据好了」，反向路径归还「缓冲可以复用了」。只有 full 屏障会造成生产者无限超前、覆盖尚未消费的数据；只有 empty 语义则消费者无从得知数据何时就绪。
2. **环形缓冲 + 相位**：`PIPE_DEPTH=2` 提供两对 A/B stage，MMA 读一对时 TMA 填另一对；每道屏障靠相位奇偶区分「这一轮」与「上一轮」（相位的细节在 u8-l2 展开，本讲只需知道它是 stage 复用的前提）。

#### 4.2.3 源码精读

**重叠的动机与两种调度的对比**，见[chapter_performance/index.md:L272-L303](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L272-L303)。文中先给出串行调度及其空闲分析，再给出流水线调度，并点明 Blackwell 上三个阶段的执行者（TMA、`tcgen05.mma`、epilogue/store 路径）与协调者（`mbarrier`）；最后一段强调重叠不消除依赖。

**单 warpgroup 内核为什么无法持续重叠**，见[chapter_gemm_advanced/index.md:L20-L27](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L20-L27)。原文说得很直白：单 warpgroup 内核里「Tensor Cores 在加载数据时没有工作，TMA 引擎在计算时可能闲置」；warp specialization 把这些工作分给不同 warp、用软件流水线在它们之间传数据，多个阶段才能并发。注意该页执行结构框中 Layout 与 Dispatch 都标注 unchanged——Step 7 改的只是 scope（角色划分），这正是三要素框架（u9-l3 将正式展开）的一次预演。

**角色表与四道屏障表**，见[chapter_gemm_advanced/index.md:L50-L69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L50-L69)。角色表给出 `WG_NUMBER=2` 下的分工：TMA producer 在 Warpgroup 1 的 warp 3，MMA consumer 在 Warpgroup 1 的 warp 0，writeback 由 Warpgroup 0 全部 warp 承担；屏障表给出 tma2mma/mma2tma/mma2ld/ld2mma 的类型（`TMABar`/`TCGen05Bar`/`MBarrier`）与含义——屏障类型取决于生产者如何报告完成：TMA 装载用字节计数，MMA 用 `tcgen05.commit`，TMA store 用 async commit group。

**PIPE_DEPTH 的最小值与代价**，见[chapter_gemm_advanced/index.md:L122-L123](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L122-L123)。`PIPE_DEPTH=2` 是让加载与计算重叠的**最小**深度；更深的流水线能隐藏更多内存延迟，但也吃掉更多 SMEM——这正是下一模块「资源压力」的伏笔。

#### 4.2.4 代码实践

**实践目标**：验证「三角色 + 四屏障」确实拼出了稳态三路重叠，并能指出每个交接由哪道屏障保护。

**操作步骤**（源码阅读型实践）：

1. 重读 [chapter_gemm_advanced/index.md:L41-L46](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L41-L46)，理解 `mma2tma` 为什么「跳过一个 stage」：`PIPE_DEPTH=2` 时 TMA Load k=0 填 stage 0、Load k=1 填 stage 1；MMA Compute k=0 读完 stage 0 后，下一个需要这个槽位的是 TMA Load k=2 而不是 k=1。
2. 画一张三行时间线（行 1 = TMA producer，行 2 = MMA consumer，行 3 = writeback），手工排出 k=0,1,2 三个 tile 的稳态：TMA 在填 k+1 的同时 MMA 算 k、writeback 回写 k-1。
3. 在时间线的每次「交接」处标注屏障名，并写清它保护的是哪块缓冲（Asmem/Bsmem 的某 stage、TMEM 累加器、Dsmem）。

**需要观察的现象**：稳态中三条时间线在同一时刻覆盖三个不同的 tile 序号；每条箭头都从「用完资源的一方」指向「下一个要用的一方」。

**预期结果**：得到与下图等价的交接表（`k` 为任意稳态迭代）：

| 屏障 | 谁到达（arrive） | 谁等待（wait） | 保护的资源 |
|------|------------------|----------------|------------|
| tma2mma | TMA（硬件字节计数） | MMA consumer | Asmem/Bsmem stage s 的数据就绪 |
| mma2tma | MMA（tcgen05.commit） | TMA producer | Asmem/Bsmem stage s 可被 k+2 覆盖 |
| mma2ld | MMA（K 循环结束后的最终通知） | writeback | TMEM 累加器结果就绪 |
| ld2mma | writeback 全部 128 线程 | MMA consumer | TMEM 区域可被下一个 tile 复用 |

可对照 [chapter_gemm_advanced/index.md:L305-L311](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L305-L311) 的「Checking Barrier Handoffs」自查：那一节要求读者沿一道屏障跟踪一个 K tile，问的正是谁等、谁到、哪个数据可读、哪个缓冲可复用。

#### 4.2.5 小练习与答案

**练习 1**：流水线内核把调度改成「load k+1 / compute k / store k-1」，这消除了 tile k 的 MMA 对 tile k 加载的依赖吗？

**参考答案**：没有。重叠不消除依赖：MMA for tile k 仍必须等 tile k 加载完成，epilogue 仍必须等 MMA 完成。流水线做的是在等待空档推进**独立**的工作——加载 k+1、回写 k-1；依赖本身由屏障（tma2mma、mma2ld）照常守护。

**练习 2**：为什么需要 full 和 empty 两个方向的屏障，只留 tma2mma/mma2ld 这类「数据就绪」信号行不行？

**参考答案**：不行。只有就绪信号时，生产者无从得知消费者是否已读完某个 stage，继续加载会覆盖尚未消费的数据（写-写冲突）；同理，writeback 不通过 ld2mma 报告「TMEM 用完了」，下一个 tile 的 MMA 就可能覆盖还在被读的累加器。empty 方向的屏障把缓冲所有权显式归还给前一个角色，是 stage 安全复用的前提。

**练习 3**：三段稳态流水线的吞吐由什么决定？如果 load 段最慢，加大 PIPE_DEPTH 一定有帮助吗？

**参考答案**：稳态吞吐由最慢的一段（短板）决定。若 load 是短板，加深流水线让生产者提前准备更多 tile，能隐藏更多内存延迟、有帮助；但一旦加载速率已贴住 TMA/HBM 能力上限，再加深度只是囤积 SMEM 而不提升速率，反而挤占驻留资源（见 4.3）。此外书里明确说「更深的流水线不必然更快，甚至可能让当前 tile 形状无法启动」。

### 4.3 Occupancy 与资源压力

#### 4.3.1 概念说明

重叠之外，GPU 隐藏延迟的另一条途径是 **SM occupancy**：让一个 SM 上同时驻留更多工作。当一个 warp 因等数据而阻塞时，warp 调度器可以切换到另一个就绪的 warp，让 SM 的执行部件不至于停转。这条途径对访存不规则、难以搭建显式流水线的内核尤其重要。

但驻留数量不是免费的，它受四类硬件上限约束（Blackwell 上还要加上 TMEM）：

- **寄存器**：每线程用得越多，能驻留的 warp/CTA 越少；
- **共享内存**：每 CTA 的 SMEM 越大（多级流水线、大 tile），驻留 CTA 越少；
- **warp 槽位与 CTA 槽位**：SM 同时驻留的 warp/CTA 数的硬上限；
- **TMEM 列**（Blackwell 特有）：TMEM 分配吃掉 Tensor Memory 容量。

于是产生了一个关键取舍：**现代 Tensor Core 内核常常故意把资源花在降低 occupancy 的地方**——多级 SMEM 流水线、大寄存器 fragment、TMEM 分配、为 producer/consumer 角色保留整个 warp。它们不是靠「驻留很多 warp」来隐藏延迟，而是**在少数驻留 CTA 内部显式重叠各个阶段**。只要流水线让 TMA、Tensor Core、store 路径都保持活跃，低 occupancy 内核照样能跑得很快。

所以结论是：**occupancy 本身不是质量的度量**。真正的问题永远是——关键硬件单元是否保持活跃。高 occupancy 与显式重叠是达到同一目标的两条路径，各有适用场景。

#### 4.3.2 核心流程

「每 SM 可驻留的 CTA 数」是一个对多项资源取最小值的问题：

\[
\text{CTAs per SM}
= \min\left(
\left\lfloor \frac{\text{SMEM}_{\text{SM}}}{\text{SMEM}_{\text{CTA}}} \right\rfloor,\;
\left\lfloor \frac{\text{Reg}_{\text{SM}}}{\text{Reg}_{\text{CTA}}} \right\rfloor,\;
\left\lfloor \frac{\text{TMEMcols}_{\text{SM}}}{\text{TMEMcols}_{\text{CTA}}} \right\rfloor,\;
\text{warp 槽位},\; \text{CTA 槽位}
\right)
\]

其中 \(\text{Reg}_{\text{CTA}} = \text{每线程寄存器数} \times \text{每 CTA 线程数}\)。使上式最小的资源称为**绑定资源（binding resource）**——优化驻留数只有降低绑定资源的用量才有效，这与 roofline 里「优化真正绑定的资源」是同一个思想在不同层面的应用。

以书中两个内核为例做估算（B200 每 SM 228 KB SMEM 为书中给出的值；每 SM 64K 个 32-bit 寄存器、64 个 warp 槽、32 个 CTA 槽为常见公开规格，书中未给出，**待本地验证**——可用 NCU 的 Launch Statistics 或 CUDA occupancy API 核实）：

**Step 1（`hgemm_v1`，BLK_M=BLK_N=128、BLK_K=64、fp16、单 warpgroup 128 线程）**：

- SMEM：\(\text{Asmem} + \text{Bsmem} = 2 \times 128 \times 64 \times 2\,\text{B} = 32\,\text{KB}\)，加上低地址约 1 KB 的控制区，约 33 KB → \(228/33 \approx 6\)；
- TMEM：分配 `n_cols=512`，即整块 TMEM → 上限 1；
- 寄存器：回写阶段每线程持有 `Dreg`（128 个 fp32）+ `Dreg_f16`（128 个 fp16，打包约占 64 个 32-bit 寄存器），仅此两项已 ≥ 192 个/线程，128 线程合计 ≥ 24576 → \(65536/24576 \approx 2\)；
- 取最小值：**1 个 CTA/SM，绑定资源是 TMEM**。

**Step 7（`hgemm_v7`，同 tile 尺寸、PIPE_DEPTH=2、两个 warpgroup 共 256 线程、持久内核）**：

- SMEM：每级 stage \((128\times64 + 128\times64)\times2\,\text{B} = 32\,\text{KB}\)，两级 64 KB，加 32 KB 的 `Dsmem` 回写缓冲，共约 96 KB → \(228/96 = 2\)；
- TMEM：同样分配 512 列 → 1；
- 寄存器与 warp 槽：8 个 warp 远低于 64 个 warp 槽，不绑定；
- 取最小值：**1 个 CTA/SM**。而持久内核的 grid 恰好只启动 `SM_COUNT = 148` 个 CTA（每 SM 一个），设计与资源上限正好咬合——这是「故意低 occupancy + 显式重叠」的典型样本。

再把资源压力沿 PIPE_DEPTH 画成曲线（书中的现成算术）：每加一级深度多一对 32 KB 的 A/B stage，总 SMEM 约为 \(\text{depth} \times 32 + 32\) KB。depth=4 约 160 KB（每 SM 1 个 CTA），depth=6 约 224 KB，几乎耗尽 228 KB——**缓冲深度与驻留数此消彼长**，这正是「重叠换 occupancy」的定量表达。

#### 4.3.3 源码精读

**Occupancy 的定义与取舍**，见[chapter_performance/index.md:L305-L325](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L305-L325)。这段给出：occupancy 的定义（一个 SM 上同时驻留多少工作）；四类限制（寄存器、SMEM、warp 槽、CTA 槽）；现代 Tensor Core 内核的四类「故意降 occupancy」开销（多级 SMEM 流水线、大寄存器 fragment、TMEM 分配、warp specialization 保留整 warp）；以及结论——occupancy 不是质量度量，问题是关键单元是否活跃。

**Step 1 的 SMEM 与 TMEM 分配**，见[chapter_gemm_basics/index.md:L210-L232](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L210-L232)。`pool.alloc` 分出 `tmem_addr`（4 B）与 `mma_bar`（8 B）后 `move_base_to(1024)` 把分配点挪到 1 KB 处，随后分配 128×64 的 `Asmem`/`Bsmem`（各 16 KB）；warp 0 的单线程执行 `tcgen05.alloc(..., n_cols=512, cta_group=1)`，一次性占满整块 TMEM 的列预算。

**Step 1 每线程的寄存器负担**，见[chapter_gemm_basics/index.md:L256-L265](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L256-L265)。`Dreg = T.alloc_local((BLK_N,), acc_type)` 给**每个线程** 128 个 fp32 寄存器槽位，`Dreg_f16` 再来 128 个 fp16；`Tx.wg.copy_async` 经 warpgroup 视图把 TMEM 的 128×128 累加器摊到 128 个线程上（每线程一行），随后逐线程转 fp16 写 GMEM。这就是 4.3.2 中寄存器项的出处。

**PIPE_DEPTH 的 SMEM 成本算术**，见[chapter_gemm_advanced/index.md:L313-L323](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L313-L323)。书中给出公式化核算：每级 stage 32 KB，`Dsmem` 再 32 KB，depth=4 约 160 KB、depth=6 约 224 KB，而「B200 每 SM 提供 228 KB 共享内存」，depth=6 几乎耗尽容量，且明确警告「更深的流水线不必然更快，甚至可能让当前 tile 形状无法启动」。

**持久内核的 grid 设计**，见[chapter_gemm_advanced/index.md:L133-L136](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L133-L136)。`SM_COUNT = 148  # Number of SMs on NVIDIA B200 GPU`——持久内核按 SM 数启动 CTA，每 SM 常驻一个，正是「低 occupancy + 流水线重叠」策略的 grid 侧配套。

#### 4.3.4 代码实践

**实践目标**：亲手算出 Step 1 与 Step 7 的每 SM 驻留 CTA 数，找出绑定资源，并验证「PIPE_DEPTH 与驻留数的此消彼长」。

**操作步骤**：

1. 用下面这段**示例代码**（非项目文件，可存为本地脚本运行）复算 4.3.2 的两张账：

   ```python
   # 示例代码：按 SMEM/寄存器/TMEM 估算每 SM 驻留 CTA 数
   SMEM_SM_KB   = 228    # B200 每 SM SMEM，书中给出
   REG_SM       = 65536  # 每 SM 32-bit 寄存器数，常见公开规格，待本地验证
   TMEM_COLS_SM = 512    # TMEM 总列数
   WARP_SLOTS   = 64     # 常见公开规格，待本地验证
   CTA_SLOTS    = 32     # 常见公开规格，待本地验证

   def resident_ctas(smem_kb, regs_per_thread, threads, tmem_cols, warps):
       limits = {
           "SMEM":  SMEM_SM_KB // smem_kb,
           "REG":   REG_SM // (regs_per_thread * threads),
           "TMEM":  TMEM_COLS_SM // tmem_cols,
           "warp":  WARP_SLOTS // warps,
           "CTA":   CTA_SLOTS,
       }
       binding = min(limits, key=limits.get)
       return limits, binding

   # Step 1: 33KB SMEM；每线程 >=192 寄存器；TMEM 512 列；4 个 warp
   print(resident_ctas(33, 192, 128, 512, 4))
   # Step 7 (PIPE_DEPTH=2): 96KB SMEM；TMEM 512 列；8 个 warp（寄存器按保守 128 估）
   print(resident_ctas(96, 128, 256, 512, 8))
   ```

2. 把 `PIPE_DEPTH` 从 2 扫到 7，仅变化 SMEM 一项（每级 +32 KB），观察 SMEM 限制与总驻留数如何变化，标出 228 KB 耗尽的临界深度。
3. 回答：两个内核的绑定资源各是什么？如果把 Step 1 的 TMEM 分配从 512 列降到 128 列（够放一个 128×128 fp32 累加器吗？注意每列 32 bit、fp32 累加器需要 128 列），驻留数会变成多少？

**需要观察的现象**：两个内核的 `min` 都被 TMEM 项卡在 1；Step 1 的寄存器项（≈2）比 SMEM 项（≈6）更紧；扫深度时 SMEM 项在 depth=6 处跌到 1 且逼近上限。

**预期结果**：Step 1 → 各项限制约为 {SMEM: 6, REG: 2, TMEM: 1, warp: 16, CTA: 32}，绑定资源 TMEM，驻留 1；Step 7 → {SMEM: 2, REG: 2 左右（取决于寄存器估计）, TMEM: 1, ...}，绑定资源 TMEM，驻留 1。第 3 问的答案：128×128 的 fp32 累加器恰好占 128 lane × 128 列，128 列分配刚好够用，TMEM 项变为 512//128 = 4，此时 Step 1 的绑定资源转为寄存器（2）。待本地验证：真实寄存器用量与驻留数应以 NCU（Launch Statistics 里的 Registers Per Thread、Achieved Occupancy）或 CUDA occupancy API 的结果为准。

#### 4.3.5 小练习与答案

**练习 1**：一个内核每线程使用 255 个寄存器、每 CTA 1024 线程，另一个每线程 64 个寄存器、每 CTA 128 线程。只看寄存器项，64K 寄存器/SM 的 SM 上各能驻留几个 CTA？

**参考答案**：前者 \(65536 / (255 \times 1024) = 0.25\)，向下取整为 0——实际上这个配置根本无法启动（超过每 SM 寄存器总量），编译器会强制减少线程数或溢出寄存器到本地内存。后者 \(65536 / (64 \times 128) = 8\) 个 CTA。可见重寄存器内核天然只能靠显式重叠而非高 occupancy。

**练习 2**：书中说「occupancy 本身不是质量的度量」。给出一个低 occupancy 但高性能、一个高 occupancy 但性能一般的具体情形。

**参考答案**：低 occupancy 高性能——Step 7/8/9 这类 warp 特化 GEMM：每 SM 仅 1 个 CTA、8 个 warp，但 TMA/Tensor Core/store 三路持续重叠，最终对齐 cuBLAS。高 occupancy 却一般——一个未优化的 elementwise 内核可以驻留满 warp，但受限于 HBM 带宽（memory-bound），算力路径大量空闲；对它提 occupancy 毫无意义，应该去减字节。两种情形的判据相同：关键硬件单元是否活跃。

**练习 3**：为什么书中说 depth=6 的流水线「可能让当前 tile 形状无法启动」？如果坚持要更深的流水线，有哪些出路？

**参考答案**：depth=6 时 SMEM 约 224 KB，逼近 228 KB 上限；再考虑屏障等元数据就可能超过每 CTA 可用的 SMEM 上限，启动直接失败。出路包括：减小 tile（如 BLK_K 从 64 降到 32，每级 stage 减半）；换更小 dtype（fp8）；或接受较浅的流水线、用持久内核 + 多消费者在别处找回吞吐——即回到优化阶梯重新权衡，而不是硬顶单一资源。

## 5. 综合实践

**任务**：为书中的 GEMM 内核做一次完整的「阶梯定位 + 资源体检」，产出一份可复查的工作表。这是本讲三个模块的综合运用，也是阅读后续 GEMM 单元（u11–u13）前的热身。

**要求**：

1. 任选 Step 1（[chapter_gemm_basics/index.md:L180-L273](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L180-L273)）或 Step 7（[chapter_gemm_advanced/index.md:L136-L302](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L136-L302)），通读内核源码。
2. **阶梯定位**：填写四行表——算法强度够不够 / 全 GPU 有没有活干 / 引擎选对没有 / 引擎间有没有重叠；给出「当前所处级别」与「下一个最值得做的动作」，并把该动作对应到九步中的某一步。
3. **资源体检**：从源码中读出 SMEM（各 `pool.alloc` 的形状与 dtype）、TMEM（`tcgen05.alloc` 的 `n_cols`）、每线程寄存器（各 `T.alloc_local` 的形状与 dtype）三项用量，用 4.3.4 的示例代码算出各资源项的驻留上限与绑定资源。
4. **交叉验证**：把你的「下一步动作」对照九步性能表（[chapter_gemm_advanced/index.md:L866-L890](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L866-L890)），估计该动作落在哪个比较区间、量级上能指望多少收益。
5. 有 Blackwell GPU 时，按 u1-l3 的编译回路实际运行所选内核并核对数值正确性（PASS），再对照 NCU 的 Achieved Occupancy 与你算出的驻留数；无 GPU 时明确标注「待本地验证」并给出推演过程。

**交付物**：一张阶梯定位表 + 一张资源体检表（含绑定资源与驻留 CTA 数）+ 一段「下一步动作与预期收益」的分析。

## 6. 本讲小结

- 优化阶梯把「逼近屋顶」排成有先后的动作序列：更好的算法（抬 AI）→ 更高并行（分块/多 CTA）→ 更合适的引擎（TMA/tcgen05）→ 重叠与资源调优（流水线/warp 特化/cluster）；低级动作是高级动作的前提，结构性改动可能先搭骨架后兑现收益。
- 书中 GEMM 九步是阶梯的完整实例：Step 1→4 约 142×（并行 + TMA），Step 4→7 约 2.2×（流水线 + 持久调度 + warp 特化），Step 7→8 约 2.2×（双 CTA 复用），Step 8→9 约 10%（多消费者），最终 0.094 ms 对齐 cuBLAS。
- 重叠针对 compute-bound 内核的剩余差距——执行路径空闲：让 TMA、`tcgen05.mma`、epilogue/store 三路同时处理相邻 tile，用 full/empty 双向屏障交接缓冲所有权；重叠不消除依赖，只推进独立工作，稳态吞吐由最慢段决定。
- Occupancy 是隐藏延迟的另一条途径（多驻留 warp 互相顶替），受寄存器、SMEM、warp/CTA 槽位与 Blackwell 的 TMEM 列约束；驻留 CTA 数 = 各资源上限取最小值，使最小的那项是绑定资源。
- 现代 Tensor Core 内核故意用多级 SMEM、大 fragment、TMEM、专属 warp 换低 occupancy，靠显式重叠让关键单元保持活跃；occupancy 本身不是质量度量，判据永远是关键硬件单元是否活跃。
- 与 roofline 三步分析法一脉相承：先定级（哪级阶梯）、再体检（哪个资源绑定），把力气花在真正绑定的资源上。

## 7. 下一步学习建议

本讲讲完，单元三「性能模型与优化方法」收官，你已经具备读内核前「先定级、再体检」的方法论。接下来两条路：

1. **正路（按大纲）**：进入单元四 u4-l1「Shape-Stride 模型与 Tile Layout 函数」。本讲反复出现的「布局」——SMEM 里 tile 怎么摆、TMEM 累加器怎么映射——将从直觉走向精确的数学记号，这是读懂 TMA、tcgen05 与后续所有 GEMM 源码的语言基础。
2. **提前热身（可选）**：若想立即检验本讲方法论，可先跳读 [chapter_gemm_basics/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md) 的 Step 1–3，用综合实践的工作表格式给它们逐一定级；等学完 u11–u13 再回头核对你的预判。
