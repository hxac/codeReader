# FA4 的 warp 角色与 barrier 协议

## 1. 本讲目标

上一讲（u14-l1）确立了 FA4 的算法链：`QKᵀ MMA → softmax → PV MMA`，`S`、`P`、`O` 都躺在 TMEM 里传递。本讲把这条链落实到**线程**，回答三个问题：

1. **活儿怎么分？** 一个 CTA 的 512 个线程（4 个 warpgroup）怎么划分成 TMA 发起、MMA 发起、两个 softmax、校正/回写这几份工？每个角色用什么线程坐标和守卫进入自己的分支？
2. **屏障怎么交接？** FA4 比 GEMM 多出哪些屏障？每道屏障**谁到达、等几次、完成后什么变得安全**？为什么 `p_o_rescale` 要凑满 256 次到达而 `s_ready` 只需要 1 次？
3. **寄存器怎么再分？** softmax 组每线程要装下一整行 128 个 fp32 分数，指令发起组却几乎不存数据——`setmaxnreg` 如何按角色重配寄存器上限？总预算是多少？

学完本讲，你应该能独立填出 FA4 的 roles / scope / barrier 三张规格表，并按「屏障类型由完成者决定」的原则推导每道屏障的期望到达数。softmax 的数值递推归 u14-l3，TMEM 物理列布局归 u14-l4，本讲只在必要处引用。

## 2. 前置知识

本讲默认你已掌握以下前置内容，直接使用不再展开：

- **FA4 算法链（u14-l1）**：`S = QKᵀ` 写入 TMEM，softmax 把 `S` 读进寄存器算出 `P` 再写回 TMEM，PV MMA 用 TMEM 的 `P` 加 SMEM 的 `V` 累加进 TMEM 的 `O`；换指数参考时旧 `O` 要乘 `acc_scale`。内核保持**两个 Q stage 在飞**。
- **操作 scope（u2-l1、u9-l3）**：一个操作由「谁发起」和「谁执行」共同刻画。TMA 与 `tcgen05.mma` 都是**单线程发起**（warp 内被 `elect_sync` 选中的一个 lane 提交），引擎执行；`tcgen05.ld/st` 与 softmax 是 **warpgroup 集体执行**。
- **warp specialization 三角色（u13-l1）**：GEMM Step 7 把单 warpgroup 拆成 TMA 生产者、MMA 消费者、回写 warpgroup，用 full/empty 屏障交接；并确立了「**屏障类型由完成者决定**」：TMA 引擎完成 → 带字节计数的 `TMABar`；Tensor Core 完成 → 靠 `tcgen05.commit` 的 `TCGen05Bar`；普通线程完成 → `MBarrier`（数到达次数）。本讲全盘沿用这套分类。
- **mbarrier 相位复用（u8-l1、u8-l2）**：一道屏障每完成一相（到达数与字节计数同时归零）就自动翻入下一相；软件用奇偶 parity 区分同一条屏障的先后两次使用；多级流水线用 `PipelineState` 把 stage 下标与 phase 捆绑。
- **`tcgen05.commit` 与 `wait::st`（u7-l1、u7-l4）**：commit 把该线程已发的异步 tcgen05 操作挂到 mbarrier 上，硬件完成后主动补一次到达；`tcgen05.wait::st()` 等待本线程此前发出的 TMEM 写真正落地。

一个本讲新引入、需要先解释的术语：**`setmaxnreg`**。GPU 的寄存器是按线程分配的，常规内核里所有线程共享同一个「每线程寄存器上限」。warp specialization 之后，不同角色需要的寄存器量天差地别，PTX 提供了 `setmaxnreg` 指令允许**以 warpgroup 为单位动态上调（inc）或下调（dec）每线程寄存器上限**——占用少的组把配额让给占用多的组。它是 Hopper 起 warp-specialized 内核的标配，FA4 用它完成本讲第三模块的主题。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [chapter_flash_attention/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md) | FA4 章正文。本讲精读 `Warp Roles and Scope`（L202 起）、`Redistributing Registers Across Roles`（L236 起）、`Conventions for Reading the Code` 与 `Barrier Roles and Completion Conditions`（L268–L311）、`Key Barrier Protocols`（L624–L665）、`Pipeline Timeline` 中的相位追踪（L708–L710）、`Rescaling and Writeback` 中的到达纪律（L737–L768），以及章末练习 4、5（L913–L914） |
| [img/scripts/gen_flash_attention_barrier_flow.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_barrier_flow.py) | 用 matplotlib 生成两张屏障交接图：`gen_main_handoff` 画两个 MMA 的启动条件门（L83 起），`gen_softmax_correction` 画 softmax 与 WG2 的邮箱交接环（L161 起）。脚本逐字写出了每道门放行什么、不证明什么，可脱离 GPU 运行，是本讲读图与动手实践的素材 |
| `zh/chapter_flash_attention/index.md` | 上述正文的中文镜像（英文路径加 `zh/` 前缀），双语对照阅读 |

外部引用（正文给出，非本仓库文件）：章节代码节选自 [tirx-kernels 的 `flash_attention4.py`](https://github.com/mlc-ai/tirx-kernels/blob/5be39749e7dfd2c4bdae9b4d396f8ec35af07126/tirx_kernels/attention/flash_attention4.py)。

## 4. 核心概念与源码讲解

### 4.1 warp 角色与 scope：512 线程的六份工

#### 4.1.1 概念说明

GEMM Step 7（u13-l1）已经演示过 warp specialization：TMA 生产者 warp、MMA 消费者 warp、回写 warpgroup，三个角色用四道屏障连成流水线。FA4 面临的新问题是：**softmax 插在两个 MMA 之间**，而且它是全内核最重的寄存器计算（每行 128 个 `exp2`、归约、再写回），单个 warpgroup 跑不完两个 Q stage 的 softmax。同时 u14-l1 讲过内核让**两个 Q tile 在飞**来填满流水线——于是 softmax 也需要双份劳动力。

FA4 的答案是把一个 CTA 的 4 个 warpgroup（每个 4 warp、128 线程，共 512 线程）分配成**软件流水线的固定角色**：WG0 与 WG1 各跑一个 Q stage 的 softmax，WG3 集中发起所有异步硬件指令（TMA 装载、两个 MMA、TMA 存储），WG2 负责 `O` 的校正（rescaling）与非 causal 路径的 epilogue。这与 GEMM「一个 warpgroup 内分 warp」的粒度不同：FA4 直接以 **warpgroup 为单位**切角色，只有 WG3 内部再按 warp 细分。

每个 Q stage 是一个可复用的槽位，包含 SMEM 里的 Q 缓冲、TMEM 里对应的 `S`/`P`/`O` 区域，以及保护它们的那批屏障。

#### 4.1.2 核心流程

非 causal 路径（本讲主线）的角色分工表：

| 所属 | 角色 | 做什么 |
|------|------|--------|
| WG3, warp 1 | TMA load | 把 Q、K、V tile 从 GMEM 装入 SMEM |
| WG3, warp 0 | MMA | 发起 QKᵀ MMA 与 PV MMA 两类矩阵乘 |
| WG3, warp 2 | TMA store | 把最终 O tile 从 SMEM 存回 GMEM |
| WG0 | Q stage 0 的 softmax | 从 TMEM 读 `S`，算 `P`，写回 TMEM |
| WG1 | Q stage 1 的 softmax | 为第二个 Q 流水级做同样的工作 |
| WG2 | 校正与 epilogue | 必要时重缩放 TMEM 中的 `O`；最后归一化、转型并写入 SMEM 中转缓冲 |

角色选择的控制流可以概括为：

```text
wg_id   = warpgroup_id(4)      # 0..3，选 warpgroup
warp_id = warp_id_in_wg(4)     # 0..3，选 warpgroup 内的 warp

if wg_id == 3:                 # 指令发起组，再按 warp 细分
    if   warp_id == 1: TMA load  分支（elect_sync 单线程提交）
    elif warp_id == 0: MMA       分支（elect_sync 单线程提交 QKᵀ/PV）
    elif warp_id == 2: TMA store 分支
elif wg_id < 2:                # WG0 / WG1：本组 wg_id 即 Q stage 编号
    softmax(q_stage = wg_id)
else:                          # WG2
    correction + (非 causal) epilogue
```

两条重要约定：

- **softmax 组的 `wg_id` 就是 Q stage 编号**。WG0 处理 stage 0、WG1 处理 stage 1，代码里 `q_stage`/`i_q` 与 softmax 分支内的 `wg_id` 是同一个数，读代码时不要当成两个变量。
- **causal 特化移动 epilogue 的归属**：最终归一化从 WG2 挪到 WG0/WG1（softmax 干完活顺手做），WG2 仍做校正，但省掉了最后的 `row_sum` 邮箱往返。分析屏障时必须先确认自己处在哪条路径。

#### 4.1.3 源码精读

正文先交代 CTA 规模与双 Q stage 的动机：[chapter_flash_attention/index.md:L204-L206](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L204-L206) 说明一个 CTA 含四个 warpgroup（WG0–WG3、共 512 线程），内核保持两个 Q tile 在飞，每个 tile 占用一个含 SMEM Q 缓冲、TMEM 的 `S`/`P`/`O` 区域与配套屏障的可复用槽位，WG0/WG1 各跑一个 stage 的 softmax、WG3 为两个 stage 发起 TMA 与 MMA、WG2 处理两者的校正加非 causal epilogue。

角色分工表在 [chapter_flash_attention/index.md:L212-L219](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L212-L219)：WG3 的 warp 1 做 TMA load、warp 0 发起两类 MMA、warp 2 做 TMA store；WG0/WG1 做 softmax；WG2 做校正与 epilogue。causal 差异见 [chapter_flash_attention/index.md:L221-L223](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L221-L223)。

线程坐标的取法是一段真实代码：

```python
wg_id = T.warpgroup_id([4])
warp_id = T.warp_id_in_wg([4])
```

见 [chapter_flash_attention/index.md:L227-L232](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L227-L232)，两者都取值 0–3，前者选 warpgroup、后者选组内 warp，内核按这两个值分支进入角色。[chapter_flash_attention/index.md:L234](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L234) 再次强调发起方式：WG3 中由对应 warp 里**被选中的一个 lane** 提交每条异步指令，TMA 引擎或 Tensor Core 执行实际搬运与计算；WG0/WG1 各用满 128 线程跑 softmax；WG2 也是 warpgroup scope。

TMEM 分配代码里还藏着一个角色自洽性证据：

```python
tmem_pool = T.TMEMPool(
    pool, total_cols=N_COLS_TMEM, cta_group=CTA_GROUP, tmem_addr=tmem_addr,
    alloc_warp=12, dealloc_warp=0,
)
```

见 [chapter_flash_attention/index.md:L545-L552](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L545-L552)。`alloc_warp=12` 正是 **WG3 的 warp 0**（全局 warp 编号 = `wg_id × 4 + warp_id` = 3×4+0 = 12）：TMEM 分配与 MMA 发起落在同一个 warp 上，`dealloc_warp=0` 则交给 WG0。坐标体系对得上，是验证你角色表没填错的一个交叉检查点。

读 FA4 代码还有一批反复出现但含义不直观的名字，正文专门整理成约定表，见 [chapter_flash_attention/index.md:L272-L283](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L272-L283)。与本讲最相关的几条：`q_stage`/`i_q` 是当前 Q 流水级（0 或 1）；`MMA_N` 是 score tile 与 TMEM 区域的基础宽度（当前为 128 列）；`should_accumulate` 表示当前 PV MMA 是初始化 `O` 还是累加进已有 `O`；`phase_tmem` 是当前 `P`/`O` 迭代关联的屏障所期望的相位奇偶；`should_rescale` 是「旧行 `O` 是否需要重缩放」的逐行标志；`acc_scale` 由 softmax 算出，softmax 自己用它更新 `row_sum`，同时经邮箱交给 WG2 去重缩放 TMEM 里的旧 `O`。完整表建议抄进你的笔记本，后四讲都要用。

#### 4.1.4 代码实践

**实践目标**：不看本讲 4.1.2 的表格，只从源码出发，亲手重建 FA4 的「roles/scope 表」，并用第二个证据源交叉验证。

**操作步骤**：

1. 通读 [chapter_flash_attention/index.md:L202-L234](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L202-L234)，为每个角色填一行五列表格：`角色 | 线程范围 | 发起方式（单线程 elect / warpgroup 集体）| 主要 tile 操作 | 读写的存储空间`。存储空间提示：softmax 是 TMEM→寄存器→TMEM；TMA load 是 GMEM→SMEM；MMA 是 SMEM(+TMEM)→TMEM；TMA store 是 SMEM→GMEM。
2. 交叉验证一：打开 [chapter_flash_attention/index.md:L683-L689](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L683-L689)，时间线图的泳道说明与角色分支一一对应（五条泳道：TMA load、两类 MMA、WG0/WG1 softmax、WG2、TMA store）。
3. 交叉验证二：核对 `alloc_warp=12` 是否等于你表里 MMA 角色的全局 warp 编号。
4. 思考并记录：角色表里 WG3 只有 warp 0/1/2 三个角色，warp 3 没有出现在正文的分工表中——在正文中检索不到它的职责（这是「正文未说明」而非你漏读）。

**需要观察的现象**：六个角色全部能落到「WG 编号 + warp 编号」的坐标上，没有悬空角色；两个 softmax 组的 `wg_id` 直接等于各自服务的 Q stage 编号。

**预期结果**：得到一张 6 行的 roles/scope 表，且 `alloc_warp=12` 的换算通过。本实践为纯源码阅读，无需 GPU；运行结果**待本地验证**的只有第 4 步的检索结论。

#### 4.1.5 小练习与答案

**练习 1**：GEMM Step 7 的角色划分粒度与 FA4 有什么不同？为什么 FA4 需要这个差别？

> **答案**：Step 7 在**一个 warpgroup 内**按 warp 分三角色（TMA warp、MMA warp、其余 128 线程回写），外加另一个 warpgroup；FA4 直接以 **warpgroup 为单位**分角色，且 softmax 占了整整两个 warpgroup（WG0/WG1 各服务一个 Q stage）。原因是 softmax 成为两个 MMA 之间最重的寄存器计算段，单个 softmax 劳动力跟不上两个 Q stage 的吞吐，双 Q stage 要求双 softmax 组。

**练习 2**：WG3 的 warp 0 同时发起 QKᵀ MMA 和 PV MMA，这两类指令会不会互相竞争？

> **答案**：不会产生正确性问题，反而是刻意设计。两类 MMA 由同一个 warp 按固定顺序提交，程序顺序本身就是一种排序手段（正文 L620 指出 WG3 warp 0 把 PV MMA 与后续 QKᵀ MMA 作为同一线程发出的固定 `tcgen05` 序列，lowering 须保留其依赖）；时间线上二者交错发起（`score Q0·K[n-1]`、`value P0·V[n-1]`、`score Q0·K[n-2]`……），让 Tensor Core 持续有活干。竞争的只是 Tensor Core 吞吐，这正是流水线想填满的资源。

**练习 3**：如果把 WG0 的角色从「Q stage 0 的 softmax」改成「所有 K/V 块的 V 装载检查」，双 Q stage 的设计会被破坏吗？

> **答案**：会。两个 Q stage 的意义是让 WG0 与 WG1 错相处理两个查询 tile，使 QKᵀ、softmax、PV 三段在两个 stage 间交错填满流水线（时间线上 softmax S0 与 S1 错开一格）。让一个 warpgroup 去做全局性的装载检查，等于把 softmax 的第二路劳动力抽走，流水线退化为单 Q stage 的串行链，Tensor Core 等数据的空闲重现。这是思想实验，用来说明「角色表不是任意的，它由流水线形状决定」。

### 4.2 屏障完成条件：每道屏障等什么、等几次

#### 4.2.1 概念说明

FA4 的依赖链比 GEMM 长：GEMM 只有 `load → MMA → epilogue` 三段，FA4 在两个 MMA 之间塞进了 softmax（寄存器计算 + TMEM 写回）与校正（条件性的 `O` 重缩放）。正文有一句概括：**FA4 在 GEMM 之外新增的屏障几乎都围着 softmax 转**——寄存器计算、`P` 的 TMEM 重写、`O` 的可选重缩放现在夹在 QKᵀ 与 PV 之间，每个边界都需要显式的就绪或归还信号（[chapter_flash_attention/index.md:L665](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L665)）。

分析每道屏障要抓三个属性：

1. **谁到达**（完成者是谁）——这决定屏障类型：TMA 引擎完成 → `TMABar`；Tensor Core 完成 → `TCGen05Bar`；普通线程完成 → `MBarrier`。
2. **到达多少次算一相完成**——注意初始化计数不一定是线程数，只有当 128 线程的组里每线程都 `arrive` 一次时才是 128。
3. **完成后什么变得安全**——即它放行哪个消费者的哪次读/写。

另外两条通用语义：`full`/`ready` 表示「生产者写完了、消费者可读」；`empty` 表示「消费者用完了、生产者可覆写」。存储被循环复用时交接通常是双向的。

#### 4.2.2 核心流程

先把 FA4 的屏障全表列出来（综合正文 L296–L309 的表格与 L292 的槽数说明，补充类型与相位追踪方式两列）：

| 屏障 | 类型 | 槽数 | 到达者（每相） | 完成条件 | 放行什么 |
|------|------|------|----------------|----------|----------|
| `q_load.full` | TMABar | 2 | 1 个被选 TMA-load 线程 | 1 次到达 + Q 流量 `CTA_GROUP × BLK_M × HEAD_DIM × 2` 字节清零 | QKᵀ MMA 可读该 Q SMEM tile |
| `q_load.empty` | TCGen05Bar | 2 | 1 个被选 MMA 线程 | 仍读此 Q stage 的 QKᵀ 完成后 Tensor Core 通知 | TMA 可用下一查询 tile 覆写该 stage |
| `kv_load.full` | TMABar | 3 | 1 个被选 TMA-load 线程 | 1 次到达 + K/V 流量 `CTA_GROUP × BLK_N × HEAD_DIM × 2` 字节清零 | QKᵀ 或 PV MMA 可读当前 K/V tile |
| `kv_load.empty` | TCGen05Bar | 3 | 1 个被选 MMA 线程 | 读该 stage 的两类 MMA 都完成后通知 | TMA 可复用该 K/V stage |
| `s_ready` | TCGen05Bar | 2 | 1 个被选 MMA 线程 | QKᵀ MMA 完成时 Tensor Core 报告 1 次 | softmax 可读 `S` TMEM tile |
| `p_o_rescale` | MBarrier | 2 | 128 softmax 线程 + 128 WG2 线程 | 两组共 256 次到达 | 第一段 PV MMA 可读 `P[:, 0:K_SPLIT]` 并初始化/累加 `O` |
| `p_ready_2` | MBarrier | 2 | softmax 组 128 线程 | 128 次到达 | 第二段 PV MMA 可读 `P[:, K_SPLIT:128]` |
| `o_ready` | TCGen05Bar | 2 | 1 个被选 MMA 线程 | 最终 PV MMA 段完成时通知 | epilogue 可读最终 `O` 累加器 |
| statistics 命名屏障 | 硬件命名屏障 | 每 Q stage 一道 | softmax 组 `ptx_bar_arrive`，WG2 `ptx_bar_sync`（合计 256 线程；`GQA_RATIO=1` 时改为四对 64 线程） | 双方到齐 | WG2 可从邮箱读 `acc_scale`（或非 causal 最终 `row_sum`） |
| `softmax_corr.empty` | MBarrier | 2 | WG2 的 128 线程 | 128 次到达 | softmax 可推进并复用邮箱 |
| `corr_epi.full` | MBarrier | 2 | WG2 的 128 线程 | 128 次到达 | TMA-store warp 可读完成的 `O_smem` tile |
| `corr_epi.empty` | MBarrier | 2 | TMA-store warp 的 32 线程 | 等 TMA store 排空后 32 次到达 | epilogue 可复用该 `O_smem` stage |

关于槽数与相位：Q 流水线 2 槽、K/V 流水线 3 槽、表中其余分槽屏障各 2 槽；**多个槽各自维护独立屏障状态，不放大期望到达数**——表中的计数都是「单个槽的当前一相」。相位追踪分三路：K/V 环用 `PipelineState`（stage+phase 捆绑推进），Q 与 TMEM 槽各用独立的本地相位变量（`phase_q`、`phase_tmem`）；命名屏障**没有显式相位参数**，靠参与线程数复用（[chapter_flash_attention/index.md:L708-L710](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L708-L710)、[L657](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L657)）。

两个 MMA 的启动条件门（即图 `flash_attention_main_handoff.png` 的内容）：

```text
QKᵀ MMA      : wait q_load.full ┐
                wait kv_load.full┴→ 发起，产出 S

第一段 PV MMA : wait kv_load.full（整块 V 就绪）─┐
                wait p_o_rescale（P 前 K_SPLIT 列 + O 槽就绪）┴→ 内层 K 取 0:K_SPLIT

第二段 PV MMA : （V 已由 kv_load.full 证明，不再等）
                wait p_ready_2（P 剩余列就绪）→ 内层 K 取 K_SPLIT:128，accum=True
```

`p_o_rescale` 期望 256 次到达的构成（章末练习 4 的核心）：

- **softmax 侧贡献 128 次**：softmax 组把 `P` 分四个 32 列块写回 TMEM，前 `P_SPLIT_Q` 块（causal 2 块、非 causal 3 块，即前 `K_SPLIT` 列）写完后先执行 `tcgen05.wait::st()` 确认写落地，然后全组 128 线程各到达一次；
- **WG2 侧贡献 128 次**：第一个 K/V 块没有旧 `O`，WG2 在主循环前**预释放**（提前到达）它的份额；后续块则在完成重缩放或确认本轮无需重缩放之后到达。

两组到达汇入同一道 `MBarrier` 的原因：第一段 PV MMA 必须同时满足两个**相互独立**的条件——「`P` 的前 `K_SPLIT` 列已写入 TMEM」与「`O` 槽可被初始化/累加」。合成一道屏障后，MMA 侧只需一次 `wait` 同时覆盖两者。而它的类型必须是 `MBarrier`：两侧的完成者都是**线程组**（softmax 线程、WG2 线程），不是 TMA 引擎也不是 Tensor Core。

`K_SPLIT` 分段换来的重叠：causal 按 64+64、非 causal 按 96+32 把 128 个归约位拆成两段。若整块 `P` 一起交接，PV MMA 必须等全部四个块写完才能启动；分段后它在 softmax 还在写剩余块时就开工了，**减少 Tensor Core 对 `P` 写回的等待时间**。

softmax 与 WG2 之间的邮箱（mailbox）交接环，是 `sScale` SMEM 缓冲里每 Q stage 一个的可复用槽位，协议共六步：

```text
1. softmax 等 softmax_corr.empty（槽位已归还）
2. softmax 把 acc_scale（或最终 row_sum）写入槽位
3. softmax 执行 ptx_bar_arrive（命名屏障到达）
4. WG2 执行 ptx_bar_sync 加入同一命名屏障，然后读槽位
5. WG2 全组到达 softmax_corr.empty
6. softmax 下一相可复用该槽位
```

务必区分 `softmax_corr.empty` 与 `p_o_rescale`：前者推进 softmax 的邮箱协议（归还统计值槽位），后者向 PV MMA 证明 `P` 与 `O` 同时满足第一段 MMA 的输入条件，二者不可互相替代。

#### 4.2.3 源码精读

**屏障类型的划分依据**在 [chapter_flash_attention/index.md:L290-L292](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L290-L292)：常规 `MBarrier` 数显式到达（仅当 128 线程组的每线程各到达一次时计数才是 128）；`TMABar` 等 1 次生产者到达加登记传输字节数扣减到零；`TCGen05Bar` 等 `tcgen05.commit` 挂上的 Tensor Core 完成通知。当前实现中 `q_load.full`/`kv_load.full` 用 TMABar，`q_load.empty`/`kv_load.empty`/`s_ready`/`o_ready` 用 TCGen05Bar，其余分槽屏障用常规 MBarrier；softmax→WG2 的统计就绪边用硬件命名屏障。正文还提醒（[L294](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L294)）：对 `TCGen05Bar`，表格描述的是**逻辑契约**——保护什么数据、完成后谁可前进；实际的 `tcgen05.commit` 会让屏障追踪同一发射线程此前发出的相关异步 `tcgen05` 操作，未必只限于表中点名的那一次 MMA。

**完整屏障表**在 [chapter_flash_attention/index.md:L296-L309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L296-L309)，[L311](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L311) 强调所有计数都是单槽单相的口径。

**`s_ready` 的到达现场**（QKᵀ MMA 之后）：

```python
Tx.warp.gemm_async(
    S_region[q_stage, :, :],
    Q_smem[q_stage, 0:BLK_M, 0:HEAD_DIM],
    K_smem[kv_stage, 0:BLK_N, 0:HEAD_DIM],
    dispatch="tcgen05",
    cta_group=CTA_GROUP,
)
if T.ptx.elect_sync():
    s_ready.arrive(q_stage)
```

见 [chapter_flash_attention/index.md:L337-L346](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L337-L346)。发起者是 WG3 warp 0，`elect_sync` 保证恰好一个 lane 提交 MMA 与随后的 commit。[L355](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L355) 解释 `s_ready.arrive(q_stage)` 发出的是把此前 QKᵀ MMA 挂到该 stage 屏障的 `tcgen05.commit`，硬件在 Tensor Core 写完 `S` 之后才报告完成，所以 softmax 组等待 `s_ready` 后再读 `S_region` 是安全的——完成条件是「1 次 Tensor Core 通知」，不是 1 次线程到达。

**`p_o_rescale` 与 `p_ready_2` 的到达现场**（softmax 写回 `P`）：

```python
P_SPLIT_Q = T.meta_var(2 if is_causal else 3)
for i in T.unroll(P_SPLIT_Q):
    Tx.wg.copy_async(
        P_region[wg_id, 1, :, i * BLK_N // 4 : (i + 1) * BLK_N // 4],
        p_chunk[:, i * BLK_N // 4 : (i + 1) * BLK_N // 4],
    )
T.ptx.tcgen05.wait.st()
p_o_rescale.arrive(wg_id)

for i in T.unroll(4 - P_SPLIT_Q):
    ...
T.ptx.tcgen05.wait.st()
p_ready_2.arrive(wg_id)
```

见 [chapter_flash_attention/index.md:L441-L459](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L441-L459)。注意顺序纪律：先 `wait.st()` 确认 TMEM 写落地、再到达——屏障到达本身不保证异步存储已完成，必须由发射线程先等待自己的写。 softmax 组的 128 线程都执行这条 `arrive`，合计贡献 128 次。正文 [L474](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L474) 点明：第一段 PV MMA 要等两个独立条件，`p_o_rescale` 把它们**并入同一道屏障**；剩余列走单独的 `p_ready_2`，所以第一段 MMA 不必等最后的 TMEM 写。

**PV MMA 的等待现场**：

```python
# 第一段：P[:, :K_SPLIT] 与 V 的对应行
Tx.warp.gemm_async(
    O_region[SMEM_PIPE_DEPTH_Q + i_q, :, :],
    P_region[i_q, 1, :, 0:K_SPLIT],
    V_smem[kv_stage, 0:K_SPLIT, 0:HEAD_DIM],
    transB=True, accum=should_accumulate,
    dispatch="tcgen05", cta_group=CTA_GROUP,
)

p_ready_2.wait(i_q, phase_tmem)
# 第二段：P[:, K_SPLIT:BLK_N]，恒为累加
```

见 [chapter_flash_attention/index.md:L491-L515](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L491-L515)，其中 `K_SPLIT = (4 if is_causal else 6) * MMA_K`（`MMA_K=16`，即 causal 64、非 causal 96）。[L523-L532](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L523-L532) 解释：`kv_load.full` 证明整块 `V` 就绪；`p_o_rescale` 同时证明 `P` 前 `K_SPLIT` 列就绪与 `O` 可初始化/累加；发完第一段后等 `p_ready_2`，第二段恒 `accum=True`（即使是第一个 K/V 块，`O` 里也已有第一段的部分和）。分段收益的原文结论在 [L532](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L532)：若 128 个归约位一体交接，PV MMA 要等全部四个 `P` 块写入 TMEM 才能开始；分段后它在前 `K_SPLIT` 列上先开工，softmax 同时完成剩余块的写回与交接。

**256 次到达的原文依据**在 [chapter_flash_attention/index.md:L640](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L640)：softmax 组存完前 `K_SPLIT` 列贡献 128 次，WG2 在 `O` 就绪后贡献另外 128 次；第一个 K/V 块没有旧 `O`，WG2 **提前**贡献它那一半；256 次全部到齐前第一段 PV MMA 不得开始。`p_ready_2` 的期望计数是 128，只由 softmax 组在存完剩余列后贡献、只放行第二段。`o_ready` 则在最终 PV MMA 段完成后把最终 `O` 交给 epilogue（表中 [L305](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L305)）。主循环前的预释放事件见 [L708](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L708)：TMEM 尚无旧 `O`，WG2 立即向两个 `p_o_rescale` 槽贡献到达，让首批 PV MMA 用 `accum=False` 初始化 `O0`/`O1`。

**邮箱协议**的六步原文在 [chapter_flash_attention/index.md:L649-L655](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L649-L655)。参与范围随 GQA 配置变化（[L657](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L657)）：`GQA_RATIO != 1` 时一道 256 线程命名屏障把完整 softmax 组与 WG2 配对；`GQA_RATIO == 1` 时改为四道 64 线程屏障配对相应的 32 线程 warp；命名屏障无显式相位参数、按参与数复用，而 `softmax_corr.empty` 是带相位的 MBarrier 流水线。第一个 K/V 块不需要 `acc_scale`，但双方仍同步一次并归还槽位，保证后续迭代对齐（[L659](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L659)）。WG2 交错处理两个 Q stage：处理完 stage `i_q` 后调用 `softmax_corr.empty.arrive(1 - i_q)` 释放**另一个** softmax stage，让 WG0/WG1 保持固定的交替顺序（[L661](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L661)）。softmax 侧等邮箱归还并翻相位的代码在 [L464-L466](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L464-L466)（`softmax_corr.empty.wait(wg_id, phase_q)` 后紧跟 `phase_q ^= 1`）。

**WG2 侧「跳过数据路径也不跳过同步」**的纪律：

```python
should_rescale = T.Select(acc_scale < T.float32(1.0), 1, 0)
any_needs_rescale = T.ptx.any_sync(0xFFFFFFFF, should_rescale)

if any_needs_rescale != 0:
    # 本 warp：TMEM -> registers -> 乘 -> TMEM
    ...

p_o_rescale.arrive(i_q)
softmax_corr.empty.arrive(1 - i_q)
```

见 [chapter_flash_attention/index.md:L737-L750](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L737-L750)。WG2 的每个 warp 用 `any_sync` 汇总自己 32 行的重缩放标志，全为 1 时整 warp 跳过 TMEM→寄存器→TMEM 往返，但**两道屏障的到达一次不能少**——否则 PV MMA 永远等不齐 256 次、softmax 的邮箱也还不回来。这就是「条件 rescaling」只裁剪数据搬运、不裁剪同步协议的含义。

图脚本与正文互为印证：`gen_main_handoff` 画出 QKᵀ 的两道门与 PV 两段的门（[img/scripts/gen_flash_attention_barrier_flow.py:L101-L136](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_barrier_flow.py#L101-L136)），并注明 `K_SPLIT = 64（causal）/ 96（non-causal）` 与「同一 `kv_load.full` 流水线随迭代分别追踪 K stage 或 V stage」（[L152-L155](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_barrier_flow.py#L152-L155)）；`gen_softmax_correction` 画出邮箱生命周期与两方向证明（[L179-L191](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_barrier_flow.py#L179-L191)）。脚本特意写了「这对屏障**不能**证明什么」：不能证明 `P` 已写进 TMEM、不能证明 `O` 已重缩放、不能证明任一段 PV MMA 可以开始——第一段由 `p_o_rescale` 放行、第二段由 `p_ready_2` 放行（[L212-L229](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_flash_attention_barrier_flow.py#L212-L229)）。这个「不能证明」清单正是初学者最容易混淆的地方。

#### 4.2.4 代码实践

**实践目标**：完成章末练习 4——追踪一个 K/V 块依次穿过 `s_ready`、`p_o_rescale`、`p_ready_2`、`o_ready`，填出 barrier 表的「谁等待 / 谁到达 / 到达数 / 相位」四列，并论证 256 这个数。

**操作步骤**：

1. 为四道屏障各画一行：`屏障 | 谁等待 | 谁贡献到达（各多少次）| 相位如何追踪`。等待者提示：`s_ready` 的等待者是 softmax 组；`p_o_rescale`/`p_ready_2` 的等待者是 WG3 warp 0；`o_ready` 的等待者是 WG2（epilogue）。相位提示：这四道都属 TMEM 侧双槽屏障，用 `phase_tmem`；`softmax_corr.empty` 用 `phase_q` 且每次 wait 后翻 1。
2. 论证 256：写出加法式 \(128 + 128 = 256\)，并分别指出两个 128 的**触发时机**（softmax 侧：前 `K_SPLIT` 列 `wait.st()` 之后；WG2 侧：第一块预释放 / 后续块重缩放决策之后）。
3. 回答练习 4 的后半问：64+64 与 96+32 的分段各换来什么重叠？（对照 [L532](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L532)。）
4. （可选，无需 GPU）在 `img/scripts/` 目录下运行 `python gen_flash_attention_barrier_flow.py`，重新生成 `img/flash_attention_main_handoff.png` 与 `img/flash_attention_softmax_correction.png`，把你的表格逐行与图中标签核对。

**需要观察的现象**：四道屏障的到达者恰好覆盖三类完成者（Tensor Core、softmax 线程组、WG2 线程组），类型选择与「完成者决定类型」原则一致；第 4 步图中的 `p_o_rescale` 标签同时压在「TMEM 中 P 的列 0:K_SPLIT」与「O slot 已准备好」两个方框上。

**预期结果**：`s_ready`＝1 次 Tensor Core 通知；`p_o_rescale`＝256 次线程到达；`p_ready_2`＝128 次线程到达；`o_ready`＝1 次 Tensor Core 通知。分段收益：第一段 PV MMA 与 softmax 的剩余列写回重叠。第 4 步脚本运行**待本地验证**（需要 matplotlib；中文版需另备 `--font-path` 指向 CJK 字体，见脚本 L16–L26 的字体检查）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `s_ready` 用 `TCGen05Bar` 而 `p_o_rescale` 用普通 `MBarrier`？换成反过来会发生什么？

> **答案**：`s_ready` 的完成者是 **Tensor Core**（QKᵀ 异步写完 `S`），只有 `tcgen05.commit` 挂上的硬件完成通知能观察到它；`p_o_rescale` 的完成者是**两组线程**（softmax 128 + WG2 128），数线程到达即可。反过来：`s_ready` 用 MBarrier 没有任何线程能代表 Tensor Core 报告「写完了」（softmax 自己到达只会证明自己跑到过这里，不证明数据落地），除非再引入一次读回验证，成本不可接受；`p_o_rescale` 用 TCGen05Bar 则语义不匹配——它要汇合的是两条**线程侧**条件，与 Tensor Core 完成无关。

**练习 2**：如果把 `p_o_rescale` 的期望到达数误设为 128（只算 softmax 一侧），内核会怎样？

> **答案**：第一相 softmax 的 128 次到达就凑满计数，屏障提前翻相，PV MMA 在 WG2 尚未确认 `O` 就绪时启动——**静默产出错误结果**（旧 `O` 未重缩放就被累加，或读到未初始化区域）。随后 WG2 的 128 次到达会溢出到下一相，使下一相又提前完成，错误逐块传播。这正是 u8-l1 讲过的「登记过小→提前放行→静默错果」在到达计数上的翻版；它不会挂死，因此比「登记过大→挂死」更难排查。

**练习 3**：命名屏障（statistics）为什么没有相位参数也能安全复用？`softmax_corr.empty` 为什么必须有相位？

> **答案**：命名屏障是**双方会合**语义——softmax 组 `bar_arrive`、WG2 `bar_sync`，两侧按固定交替顺序一对一配对，第 \(k\) 轮的到达与等待天然配对，不需要区分轮次。`softmax_corr.empty` 是**单向通知**（只有 WG2 到达、只有 softmax 等待），同一道屏障会被连续多轮使用，必须靠相位奇偶区分「这次归还」与「上次归还」，否则 softmax 的 `try_wait` 可能在上一轮就已满足的相位上立即通过（u8-l2 的相位复用原理）。

**练习 4**：第一个 K/V 块没有旧 `O` 也不需要 `acc_scale`，为什么 WG2 还要参与 `p_o_rescale` 与邮箱同步？

> **答案**：为了**相位对齐**。若第一轮 WG2 缺席，`p_o_rescale` 第一相凑不齐 256 次（除非特判），后续每轮的等待奇偶全部错位；邮箱同理——softmax 与 WG2 仍走一遍「写空值/读空值/归还」的六步协议，保证从第二轮起双方的槽位状态与相位完全一致。原文表述见 [chapter_flash_attention/index.md:L659](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L659)：双方仍同步一次并归还槽位，使后续迭代保持对齐。

### 4.3 角色间寄存器再分配：setmaxnreg

#### 4.3.1 概念说明

warp specialization 切分的不只是工作，还有**寄存器**。四个角色的寄存器胃口极不均衡：

- **WG3** 只发起 TMA/MMA 指令、维护描述符与少量循环状态，几乎不保留大块中间 tile；
- **WG0/WG1** 的每个线程要**装下完整的一行 128 个 fp32 分数**（`s_chunk_buf`）外加 softmax 临时量——`Tx.max`、`exp2`/多项式近似、`Tx.sum`、fp16 转换都要在同一行上做；
- **WG2** 做校正时按 warp 各处理 32 行的条带，每线程只需 `RESCALE_TILE=16` 宽的片段。

如果给全部 512 线程都按最坏情况预留 200 个寄存器，正文明确指出这会**超出可用寄存器容量**（[chapter_flash_attention/index.md:L238](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L238)）。解法是 `setmaxnreg`：按角色动态调整每线程寄存器上限，让不占数据的组把配额让给 softmax 组。这与 u3-l3 讲过的「occupancy 不是质量度量、现代 Tensor Core 内核故意用低 occupancy 换显式重叠」一脉相承——FA4 干脆在寄存器维度也做了「按需分配」。

#### 4.3.2 核心流程

内核在各角色分支开头执行 `setmaxnreg`：

```python
if wg_id == 3:
    T.ptx.setmaxnreg(False, 48)        # WG3 释放多余寄存器
elif wg_id < 2:
    T.ptx.setmaxnreg(True, 200)        # WG0/WG1 为 softmax 获取寄存器
elif wg_id == 2:
    T.ptx.setmaxnreg(False, 64)        # WG2 做校正 / 非 causal epilogue
```

第一个参数是方向：`False`＝下调（dec，释放）、`True`＝上调（inc，获取）。`setmaxnreg` 是 warpgroup 对齐的指令，组内所有 warp 统一执行同样的调整。

每线程上限配置为：WG0/WG1＝200，WG2＝64，WG3＝48。四个 128 线程的 warpgroup 合计总预算：

\[ 128 \times (200 + 200 + 64 + 48) = 128 \times 512 = 65{,}536 \ \text{个 32-bit 寄存器} \]

对照方案——给 CTA 内全部 512 线程统一 200 个：

\[ 512 \times 200 = 102{,}400 \]

分级方案把总需求压到统一方案的 64%（省下 36,864 个）。作为背景参考（正文未陈述）：NVIDIA GPU 每 SM 的寄存器堆常规规格正是 65,536 个 32-bit 寄存器，分级方案的总数恰好贴着这个上限，而统一 200 的方案将超出它。

#### 4.3.3 源码精读

动机陈述在 [chapter_flash_attention/index.md:L236-L240](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L236-L240)：Warp specialization 分的不只是工作，还让内核把寄存器容量集中到需要的角色；WG3 主要发起指令不保留大中间 tile，WG0/WG1 却要每线程持有整行 128 个 fp32 分数加 softmax 临时量，若给全部 512 线程都预留这个最坏预算将超出可用容量，因此内核用 `setmaxnreg` 按角色动态调整每线程上限。

`setmaxnreg` 代码在 [chapter_flash_attention/index.md:L242-L250](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L242-L250)（上文 4.3.2 所引）。总数计算在 [chapter_flash_attention/index.md:L252-L256](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L252-L256)：

```text
128 × (200 + 200 + 64 + 48) = 65,536 32-bit registers
```

[L258](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L258) 总结：再分配让 softmax 线程有足够寄存器保留完整分数行，而不必给 WG3 的指令发射线程预留同等大块。

两个交叉验证：

- **200 的去向**：softmax 每线程把整行 128 个分数放进名为 `s_chunk_buf` 的 128 值 fp32 寄存器缓冲，分四次 32 列 `tcgen05.ld` 装入（[chapter_flash_attention/index.md:L369-L384](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L369-L384)），正文明说 200 上限**主要为这个缓冲与其余 softmax 临时量**腾地方。仅 `s_chunk_buf` 就占 128 个寄存器，可见 200 相当紧凑。
- **64 的去向**：WG2 校正时每线程操作 `RESCALE_TILE=16` 宽的 `o_row` 寄存器 tile（`o_row = T.wg_reg_tile(RESCALE_TILE)`，见 [chapter_flash_attention/index.md:L718-L720](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L718-L720)），16 个值远小于 128，64 的上限绰绰有余。

#### 4.3.4 代码实践

**实践目标**：完成章末练习 5——计算四个 warpgroup 的寄存器总预算，与「人人 200」的统一方案对比，并用源码证据解释为什么 softmax 需要最大配额、下调 WG3 又如何使这成为可能。

**操作步骤**：

1. 手算（或用 Python）复算两个数：分级方案 \(128 \times (200+200+64+48)\) 与统一方案 \(512 \times 200\)，求差值与比值。
2. 写三行 Python 验证（示例代码）：

   ```python
   tiered = 128 * (200 + 200 + 64 + 48)
   uniform = 512 * 200
   print(tiered, uniform, uniform - tiered, tiered / uniform)
   ```

3. 从源码找证据链：`s_chunk_buf` 是多少个 fp32（L369）？`RESCALE_TILE` 是多少（L719）？据此说明 200/64/48 三档各自由哪块数据决定。
4. 回答练习 5 的两个「为什么」：softmax 为什么最大？WG3 降到 48 为什么反而**使** softmax 的 200 成为可能？（提示：寄存器总量守恒，一组释放的配额才可能被另一组获取。）

**需要观察的现象**：`tiered = 65536`、`uniform = 102400`、差 36864、比值 0.64；`s_chunk_buf` 的 128 个 fp32 恰好解释了 200 里的大头。

**预期结果**：分级方案总预算 65,536，统一方案需 102,400——统一方案比分级多 36,864 个（多 56.25%），分级方案只花统一方案的 64%。softmax 每线程要持有整行 128 个 fp32 分数加临时量所以最大；WG3 只发射指令、不保留 tile 数据，把上限压到 48 后释放出的配额正是 softmax 组上调到 200 的空间来源。第 2 步脚本可在任何有 Python 的机器运行；它与 B200 实际行为的对应**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 WG2 的上限从 64 也提到 200（其余不变），总数变成多少？这一定可行吗？

> **答案**：\(128 \times (200+200+200+48) = 82{,}432\)。不一定可行——总数逼近甚至超过每 SM 寄存器堆容量（背景规格 65,536），内核可能无法按 1 CTA/SM 驻留甚至无法编译通过；而 WG2 的实际需求由 `RESCALE_TILE=16` 决定，提到 200 纯属浪费。寄存器再分配的价值恰恰在于**按数据足迹定量**。

**练习 2**：`setmaxnreg` 为什么放在每个角色分支的开头、而不是内核入口统一执行一次？

> **答案**：因为它调整的是**每线程上限**，而不同 warpgroup 进入不同分支——必须「谁进哪个分支、谁就按该角色的配额执行」。在内核入口统一执行等于没有分级。这也解释了它是 warpgroup 对齐指令：同一 warpgroup 的 128 线程走同一分支、做同样的调整。

**练习 3**：GEMM Step 7（u13-l1）没有讨论 `setmaxnreg`，FA4 为什么需要？

> **答案**：Step 7 的回写 warpgroup 每线程只持有一个输出行片段，各角色寄存器足迹差距不大，统一上限即可；FA4 的 softmax 把「整行 128 个 fp32 常驻寄存器」变成硬需求，同时又有四个 warpgroup 的富余劳动力格局，差距大到必须显式再分配。可以说：**softmax 进驻 warpgroup 是 FA4 引入 setmaxnreg 的直接原因**——这也是「FA4 的改动都被 softmax 逼出来」这句话在资源维度的体现。

## 5. 综合实践

**任务：为 FA4 编写一份完整的「交接规格说明」（handoff spec）——三张表加一个故障预测，作为你后续精读 u14-l3～u14-l6 的随身参考。**

1. **roles 表**（4.1 的产出）：六行，列为「角色 | 线程坐标（`wg_id`, `warp_id`）| 发起方式 | 主要 tile 操作 | 读写存储空间」。要求把 causal 路径的 epilogue 归属变化写成表下的一行脚注。
2. **scope 表**：把 roles 表中的每个 tile 操作改按三要素登记（scope / layout / dispatch），layout 列此阶段只写存储空间与区域名（如 `S_region[q_stage]`），物理列号留给 u14-l4 补全。
3. **barrier 表**（4.2 的产出）：沿用 4.2.2 的十二行全表，但把「相位追踪」列补成三类的明确写法：`PipelineState`（K/V 环）、本地相位变量 `phase_q`/`phase_tmem`（Q/TMEM 槽）、无相位（命名屏障）。
4. **故障预测**：针对以下三个改动，各写一段「症状 + 机理 + 排查入口」：
   - (a) `p_o_rescale` 期望计数误设为 128——预测属于「静默错果」还是「挂死」？错误最先出现在哪次 MMA？（对照 4.2.5 练习 2。）
   - (b) WG2 忘记 `softmax_corr.empty.arrive(1 - i_q)`——哪一方在哪个 wait 上挂起？
   - (c) 删掉所有 `setmaxnreg`、统一 200——是数值错误还是资源问题？依据 4.3 的总数计算。
5. （可选，无需 GPU）运行 `python img/scripts/gen_flash_attention_barrier_flow.py` 与 `python img/scripts/gen_flash_attention_pipeline.py`（后者是 u14-l1 用过的时间线脚本），对照正文 [L681](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L681) 的分工使用两张图：**barrier-flow 图查正确性依赖（谁等谁），timeline 图查执行重叠（谁与谁同时活）**，把你的 barrier 表逐行在两张图里找到落点。

**验收标准**：三张表能独立于本讲义重建（遮住讲义、只看 [chapter_flash_attention/index.md:L202-L311](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L202-L311) 与 [L624-L665](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L624-L665) 即可填出）；三个故障预测都能归入 u15-l7 调试附录的四类症状（死锁/崩溃/错果/慢）之一。

## 6. 本讲小结

- FA4 把 512 线程划成固定角色：WG3 的 warp 1/0/2 分别发起 TMA load、两类 MMA、TMA store（均 `elect_sync` 单线程提交、引擎执行）；WG0/WG1 各用一个完整 warpgroup 跑一个 Q stage 的 softmax（`wg_id` 即 stage 编号）；WG2 做校正与非 causal epilogue，causal 路径把 epilogue 挪进 WG0/WG1。
- 屏障类型仍由完成者决定：`q_load.full`/`kv_load.full` 是带字节计数的 TMABar；`q_load.empty`/`kv_load.empty`/`s_ready`/`o_ready` 是靠 `tcgen05.commit` 的 TCGen05Bar；其余分槽屏障是数线程到达的 MBarrier；softmax→WG2 的统计就绪边是硬件命名屏障。
- `p_o_rescale` 期望 256 次到达＝softmax 组存完 `P` 前 `K_SPLIT` 列后的 128 次＋WG2 使 `O` 就绪后的 128 次（首块由 WG2 预释放），把两个独立条件并成一次 `wait`；`p_ready_2` 只数 softmax 的 128 次、只放行第二段 PV MMA。
- QKᵀ 等 `q_load.full`+`kv_load.full`；第一段 PV 等 `kv_load.full`+`p_o_rescale`；第二段只等 `p_ready_2`（整块 V 已被证明）。`K_SPLIT` 64+64 / 96+32 的分段让 PV MMA 与 softmax 的剩余列写回重叠。
- softmax 与 WG2 用 SMEM 邮箱交换 `acc_scale`/`row_sum`：命名屏障报就绪、`softmax_corr.empty` 归还槽位（`phase_q` 逐次翻转）；WG2 跳过重缩放数据路径时**不跳过**任何屏障到达。
- `setmaxnreg` 按角色重配寄存器：200/200/64/48，总计 \(128\times512=65{,}536\) 个 32-bit 寄存器，是「人人 200」方案（102,400）的 64%；softmax 的 200 主要供养每线程整行 128 个 fp32 的 `s_chunk_buf`，WG3 压到 48 释放的配额正是 softmax 上调的空间来源。

## 7. 下一步学习建议

本讲解决了「谁干活、怎么交接、寄存器怎么分」，但刻意绕开了三块硬骨头：

1. **u14-l3「QKᵀ MMA 与夹在中间的 softmax」**：softmax 组内部到底算什么——`delta`/`acc_scale`/`P` 的数值递推、`row_max_safe` 的 \(-\infty\) 兜底、硬件 `exp2` 与 FMA 多项式近似如何分担指数计算。本讲的 `s_chunk_buf` 到了那一讲才真正展开。
2. **u14-l4「PV MMA 与 TMEM 布局复用」**：本讲反复出现的 `S_region[q_stage]`、`P_region[wg_id, 1, :, :]`、`O_region[SMEM_PIPE_DEPTH_Q + i_q]` 这些视图背后的物理列区间与重叠（`S0`/`P0` 共用 `[0,128)` 的后半段），以及防过早读写的屏障如何与本讲的表对上。
3. **u14-l5「条件 rescaling 与 writeback」**：本讲只讲了 WG2 到达的时机，那一讲跟踪 `O` 从 TMEM 读回、乘 `acc_scale`、写回、最终归一化经 SMEM 由 TMA store 写出 GMEM 的完整路径。

建议先把综合实践的三张表做完再前进——它们是后四讲的索引。若想先横向巩固屏障功底，可回看 u8-l1/u8-l2 的 mbarrier 状态机与相位复用，并对照 u13-l1 的四道屏障版（单 CTA、三角色）体会「FA4 多出的屏障全部来自 softmax 插进两个 MMA 之间」这句话。
