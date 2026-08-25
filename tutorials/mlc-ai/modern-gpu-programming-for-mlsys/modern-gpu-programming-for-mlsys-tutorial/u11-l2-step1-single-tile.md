# u11-l2 Step 1：单 tile 同步内核

## 1. 本讲目标

本讲精读 GEMM 系列的第一个完整内核：**一个 CTA 同步地计算一个 \(128\times128\) 输出 tile**。学完后你应该能够：

1. 按基础章的「四部件」划分（分配 / 加载 / 计算发起 / 回写，外加释放）复述单 tile 内核的完整结构，并把每个部件对应到内核源码的具体代码段。
2. 解释 `elect_sync` 与 mbarrier 如何配对完成 MMA 的**发起与等待**：为什么恰好一个线程发射、为什么发射后要 `tcgen05.commit`、为什么全体线程在 barrier 上等。
3. 正面回答章末练习 1：**为什么 `Tx.gemm_async` 之前必须有 `T.cuda.cta_sync()`**——这是本讲的必答题。
4. 读懂 SMEMPool 的分配细节：控制对象（`tmem_addr`、`mma_bar`）与操作数 tile 各放在哪、`move_base_to(1024)` 做什么、layout 如何绑定到 buffer。
5. 独立推导 TMEM 读回阶段「128 个线程 ↔ 128 个输出行」的一一映射，并理解 `Dreg` / `Dreg_wg` 两个寄存器缓冲的分工。

一个前置说明：Step 1 使用的内核就是 u9-l1 里逐段读过的 `hgemm_v1`——基础章明说这一步「复用而非新写」。但两讲的镜头不同：u9-l1 把它当作 **TIRx 入门标本**（关注 tile 操作与底层辅助的两层 API），本讲把它当作 **GEMM 优化系列的正确性基线**（关注四部件的交接协议、同步的必要性、以及为后续八步留好的接缝）。已熟悉的内容只做索引，不重复展开。

## 2. 前置知识

本讲默认你已完成单元九与 u11-l1。用到的旧知识快速回顾：

- **`hgemm_v1` 五阶段**（u9-l1）：分配 SMEM/TMEM → 全 CTA 协作拷贝 → 单线程发起 MMA → warpgroup 读回 TMEM 写 GMEM → 释放 TMEM。本讲把这五阶段重组为基础章的四部件视角。
- **三要素**（u9-l3、u11-l1）：scope（哪些线程执行）、layout（数据怎么摆）、dispatch（走哪条硬件路径）。基础章给每个 Step 都配了「execution structure」框，就是三要素快照。
- **tcgen05.mma 的执行模型**（u7-l1）：单线程语义的 tile 级指令，A/B 从 SMEM 描述符读取，累加器写 TMEM；指令异步执行，`tcgen05.commit` 把完成信号挂到 mbarrier，硬件完成后主动「到达」。
- **TMEM 管理纪律**（u7-l3）：`tcgen05.alloc` 沿列分配、只有 32/64/128/256/512 五档、同一 CTA 多次分配须单调不增——所以内核起步就要 512 列再按列切片。
- **tcgen05.ld 与 lane 窗口**（u7-l3、u7-l4）：warpgroup 内编号 \(w\) 的 warp 只能访问 TMEM 的 lane 区间 \([32w,\,32w+31]\)；`tcgen05.ld` 异步，用前须 `wait::ld`。
- **mbarrier 状态机**（u8-l1）：arrive 与 wait 分离；本讲只用「单次使用、等待初始相位 0」的最简形态，相位翻转是 u11-l3（Step 2）的主题。
- **数据路径**（u11-l1）：GMEM → SMEM → TMEM → RF → GMEM 六站五跳；Step 1 是这条路径「无循环走一遍」的最小实例。
- **TileLayout 记号**（u4-l2、u10）：`S[(shape):(strides@axis)]`，命名轴 `TLane`/`TCol`/`tid_in_wg` 参与真实求值。

术语提示：**elect_sync** 是 warp 级 PTX 指令，从当前 warp 的活跃线程中选出恰好一个；**epilogue（尾声）** 指 MMA 结束后把 TMEM 里的 fp32 结果读出、转成输出 dtype、写回 GMEM 的收尾段；**swizzle atom** 是 SMEM 中地址重排的最小重复单元（SWIZZLE_128B 下为 8 行 × 128 B = 1 KB，见 u5-l2/u6-l2）。

## 3. 本讲源码地图

| 文件 | 本讲涉及范围 | 作用 |
| --- | --- | --- |
| `chapter_gemm_basics/index.md` | L58–L335 | **主源码**。Step 1 全部内容：execution structure 框、五步数据流、四部件逐段讲解、完整内核、驱动与验证脚手架、局限清单 |
| `chapter_gemm_basics/index.md` | L634–L638 | 章末练习；练习 1（cta_sync 的必要性）是本讲必答题 |
| `chapter_intro_tirx/index.md` | L85 起 | `hgemm_v1` 的首次出现处（u9-l1 已按入门视角精读），本讲交叉引用 |
| `chapter_gemm_async/index.md` | 全章 | Step 4 起的续篇；本讲多处预告「换 TMA 后怎么办」时指向它 |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**单 tile 数据流**、**SMEMPool 分配**、**MMA 发起与等待**、**TMEM 读回**。

### 4.1 单 tile 数据流：正确性基线的全貌

#### 4.1.1 概念说明

GEMM 章的教学策略是「一次只加一个机制」：数据搬运、K 累加、空间分块、Tensor Core 调度若一次全塞进内核，出错就无法定位。所以系列从最小的正确内核起步，**每个旧版本保留为后续版本的参照系**——这就是「正确性基线」的含义：后面八步每改一处，都能对着 Step 1 问「我只动了什么」。

「单 tile」有两个含义：输出侧 \(M=N=128\)，即 D 只有一个 \(128\times128\) 的 tile；缩减侧 \(K=64\) 恰好等于一个 K tile 的宽度。两者共同导致**内核没有任何循环**——数据路径的每一段只出现一次。基础章原话说得很清楚：At this size no loop is needed, so each part of the path appears only once。

基础章还为 Step 1 给了三要素快照（execution structure 框）：

- **Scope**：一个 warpgroup（128 线程）串行执行全路径；
- **Layout**：A、B tile 驻留 SMEM，累加器驻留 TMEM，结果经寄存器写出；
- **Dispatch**：加载用同步的 `Tx.cta.copy`，MMA 用 `tcgen05`。

对照 u11-l1 的九步总表：Step 1 是「定义基线」的一步，三要素全都不动——它的价值不在性能（后面会看到它只有约 2 TFLOPS），而在把 GMEM → SMEM → TMEM → RF → GMEM 这条路径无循环地走通一遍。

#### 4.1.2 核心流程

基础章把单 tile 数据流总结为五步：

1. **Allocate**：经 pool 分配 SMEM，`tcgen05.alloc` 分配 TMEM，准备追踪 MMA 完成的 mbarrier；
2. **Load**：全部 128 线程用同步 `Tx.cta.copy` 协作把 A、B tile 从 GMEM 拷入 SMEM；
3. **Compute**：一个被选出的线程发射 `Tx.gemm_async` 与 `tcgen05.commit`；warpgroup 在 mbarrier 上等待；
4. **Write back**：warpgroup 把 TMEM 读进寄存器；每线程把 fp32 转 fp16 并把自己的那一行存入 GMEM；
5. **Release**：释放 TMEM 分配。

展开成时间轴（单 CTA，128 线程），注意三处 `cta_sync` 各守一道交接：

```text
t0  warp0/lane0: mbarrier.init；warp0 集体: tcgen05.alloc(512 列)
t1  fence ×2 + cta_sync ①          ← 把初始化发布给全 CTA
t2  全体: Tx.cta.copy 搬 A、B       ← GMEM→SMEM（同步、线程驱动）
t3  cta_sync ②                      ← 练习 1 的主角：等全部写完且可见
t4  warp0 中 elect_sync 选出的线程: Tx.gemm_async + tcgen05.commit
t5  全体: mbarrier.try_wait(phase 0) ← 等 Tensor Core 写完 TMEM
t6  全体: Tx.wg.copy_async + wait.ld ← TMEM→RF（tcgen05.ld，异步）
t7  每线程: cast fp32→fp16，Tx.copy 写 D 的对应行
t8  cta_sync ③ → warp0: relinquish + dealloc TMEM
```

三道 `cta_sync` 的守卫对象分别是：① 初始化（mbarrier 与 TMEM 基地址对全体使用者可见）；② 数据（SMEM 操作数对 MMA 完整）；③ 生命周期（全体线程读完 TMEM 后才释放）。这道「一交接一同步」的纪律是 u9-l1 已指出的结构，本讲在 4.3 详答第 ② 道。

#### 4.1.3 源码精读

**Step 1 的定位与三要素快照**：

- [chapter_gemm_basics/index.md:L58-L66](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L58-L66) —「## Step 1: Sequential Single-Tile GEMM」小节开头：声明本节复用 `chap_tirx_primer` 的 `hgemm_v1`、按数据路径详读并作为后续所有版本的 correctness baseline；计算一个 \(128\times128\) 输出 tile、\(K=64\)、无循环；随后的 execution structure 框给出 Scope/Layout/Dispatch 三要素快照。

**五步数据流**：

- [chapter_gemm_basics/index.md:L68-L76](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L68-L76) —「### Single-Tile Dataflow」：内核沿 `GMEM -> SMEM -> TMEM -> registers -> GMEM` 路径无循环地走一遍，Allocate / Load / Compute / Write back / Release 五步逐条列出。注意第 1 步的措辞——mbarrier 追踪的是 **MMA completion**（输出侧完成），不是输入数据是否到位；这个区分是练习 1 答案的关键一环。

**四部件的划分与参数**：

- [chapter_gemm_basics/index.md:L78-L80](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L78-L80) —「### Four Pieces of the First Kernel」：先分开看 allocation、operand loading、MMA issue、writeback，再拼成完整内核；本节固定 `BLK_M=BLK_N=128`、`BLK_K=64`；`m_st`/`n_st` 表示当前输出 tile 在 D 中的行列起点，单 tile 内核中二者皆为零。

**完整内核的骨架**（逐段精读分散在 4.2–4.4）：

- [chapter_gemm_basics/index.md:L179-L208](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L179-L208) — `hgemm_v1(M, N, K)` 构建器开头：四个 dtype 常量（fp16 输入输出、fp32 累加）、`BLK_M, BLK_N, BLK_K = 128, 128, 64`、文档性质的 `MMA_M, MMA_N, MMA_K`、两个 SMEM layout 构造；`@T.prim_func` 内 `T.device_entry()` 后用 `T.cta_id` / `T.warpgroup_id` / `T.warp_id_in_wg` / `T.lane_id` 取线程层级坐标。其中 L202–L204 的注释说明：因为 `M = BLK_M`、`N = BLK_N`，grid 是 \(1\times1\)，`(m_st, n_st)` 平凡为零，保留 `cta_id` 形式是为了让到 Step 3（多 tile）的 diff 最小。
- [chapter_gemm_basics/index.md:L186-L190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L186-L190) — `MMA_M, MMA_N, MMA_K = 128, 128, 16` 只是文档化底层硬件 MMA tile 的尺寸，**并不传给** `gemm_async`（MMA 形状由操作数与累加器 tile 推出），所以后续步骤干脆省略这三个变量。记住 `MMA_K = 16`——4.3 的指令数推演要用它。

**驱动与验证回路**（u9-l2 已详述编译流水线，这里只看本步特有之处）：

- [chapter_gemm_basics/index.md:L278-L304](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L278-L304) — 驱动脚手架：`M, N, K = 128, 128, 64`，`tvm.compile(..., tir_pipeline="tirx")` 编译，`ex.mod(A_tensor, B_tensor, D_tensor)` 直接收 PyTorch 张量；参考实现先 `.float()` 升 fp32 再乘、最后 `.half()`（与内核 fp32 累加对齐），先打印 `max_err` 再断言。容差取 `rtol=2e-2, atol=1e-2`，L301–L302 的注释给了理由：输出量级随 K 增长，固定绝对容差在大 K 时会误报。
- [chapter_gemm_basics/index.md:L276](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L276) — 一条贯穿九步的实操警示：**每个全新 Python 会话只编译一个 step**——各步示例复用内部名，且编译器持有跨调用的会话状态，换 step 前要重启内核会话。
- [chapter_gemm_basics/index.md:L307-L320](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L307-L320) — 可选的冒烟计时：3 次预热 + CUDA events 包 10 次迭代取平均，换算 TFLOPS。单 tile 规模只有 \(2\times128\times128\times64 = 2^{21}\approx 2.1\) MFLOP，这个计时仅作 sanity check；正式协议见 u15-l4。

**Step 1 的四条局限**——这就是后续八步的「待办清单」：

- [chapter_gemm_basics/index.md:L328-L335](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L328-L335) — 只能算单个 K tile（→ Step 2 加 K 循环）；M、N 被钉死在 128（→ Step 3 空间分块）；用同步 GMEM→SMEM 拷贝而非 TMA（→ Step 4）；搬运与计算从不重叠（→ Step 5/7）。

#### 4.1.4 代码实践

**实践目标**：把「五步数据流」与完整内核源码逐段对齐，建立后续三讲反复使用的代码定位能力。

**操作步骤**（源码阅读型实践）：

1. 通读 [chapter_gemm_basics/index.md:L179-L274](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L179-L274) 的完整内核（约 95 行）。
2. 自制一张「数据流步骤 → 代码段」对照表。
3. 在表上额外标注三处 `cta_sync` 的行号与其守卫的交接。

**需要观察的现象**：五步数据流是否覆盖了内核的全部语句？有没有属于「第 0 步」（线程标识获取）和「收尾」（dealloc）的代码没有被五步涵盖？

**预期结果**：对照表如下（行号属本书仓库当前 HEAD）：

| 数据流步骤 | 完整内核代码段 | 备注 |
| --- | --- | --- |
| 0 线程标识 | L205–L208 | `bx, by` / `wg_id` / `warp_id` / `lane_id` |
| 1 Allocate | L211–L232 | SMEM pool + barrier/TMEM init + `tmem` decl_buffer |
| 2 Load | L241–L243 | 两条 `Tx.cta.copy` + `cta_sync` ② |
| 3 Compute | L246–L254 | `gemm_async` + `commit` + `try_wait` |
| 4 Write back | L257–L265 | TMEM→RF→GMEM |
| 5 Release | L268–L271 | `cta_sync` ③ + relinquish + dealloc |

覆盖是完备的：第 0 步取坐标属于准备工作，Release 对应第五步。此实践无需 GPU。

#### 4.1.5 小练习与答案

**练习 1**：Step 1 为什么不需要 K 循环？去掉这个「方便」后哪一步最先受害？

**答案**：因为 \(K=64\) 恰好等于 `BLK_K`，单个 K tile 就装下了整个缩减维，路径每段只执行一次（基础章 L61 原话：At this size no loop is needed）。一旦 \(K>64\)，`Tx.cta.copy` 的切片 `A[:, :]` 就装不下 A——受害最先的是 Load 段：要么拷不完整、要么 SMEM 溢出。Step 2 的解法是循环「load → MMA → wait」并让每次 MMA 累加进同一 TMEM 槽。

**练习 2**：grid 为什么是 \(1\times1\)？`bx, by` 在本步中取什么值，为什么还要写这行代码？

**答案**：grid 形状是 `[M // BLK_M, N // BLK_N] = [1, 1]`，唯一 CTA 的 `(bx, by) = (0, 0)`，于是 `m_st = n_st = 0`。保留 `cta_id` 的调用形式并配合切片写法，是为了让 Step 3 把 grid 放大后**内核其余部分零改动**（L202–L204 注释明说这是为了让 diff 最小）。这是渐进式重构的常用手法：先按目标形态写平凡情形。

**练习 3**：4.1.3 列出的四条局限分别由哪一步解决？

**答案**：单 K tile → Step 2（K 循环累加）；M/N 钉死 128 → Step 3（空间分块、2D grid）；同步线程拷贝 → Step 4（TMA 引擎异步搬运）；搬运与计算不重叠 → Step 5 建立双缓冲结构、Step 7 角色划分后真正并发。

### 4.2 SMEMPool 分配：操作数与控制对象各就各位

#### 4.2.1 概念说明

Step 1 需要在 SMEM 里放两类东西：

- **操作数 tile**：`Asmem`（\(128\times64\) fp16，16 KiB）与 `Bsmem`（同规格）——MMA 的输入；
- **控制对象**：`tmem_addr`（4 字节，存放 `tcgen05.alloc` 写回的 TMEM 基地址）与 `mma_bar`（8 字节，mbarrier 同步对象）。

为什么控制对象必须在 SMEM 而不能放在寄存器？因为它们是**跨线程共享的交接媒介**：`tmem_addr` 由 warp 0 写、全体线程读（`T.decl_buffer` 要用 `tmem_addr[0]` 绑定 TMEM）；mbarrier 是硬件同步对象，必须驻留共享内存才能被 CTA 内的线程与异步单元访问。寄存器是线程私有的，天然无法承担这个角色（呼应 u7-l3：TMEM 地址槽要预留在 SMEM）。

TIRx 用 **`T.SMEMPool`** 统一管理这些分配：按申请顺序在线性地址空间里依次摆放，`layout=` 参数把物理排布绑定到 buffer 上，最后 `pool.commit()` 固化这一轮分配。

#### 4.2.2 核心流程

Step 1 的 SMEM 布局策略分三步：

1. **低地址放控制对象**：先分配 `tmem_addr`（uint32）与 `mma_bar`（uint64，`align=8`）——它们只有 12 字节，占据 pool 起点附近；
2. **跳到 1024 再放操作数**：`pool.move_base_to(1024)` 把分配指针直接移到字节偏移 1024，`Asmem` 从 1024 开始、`Bsmem` 紧随其后；1024 以下的剩余空间仍留给控制值（章节原话：lower addresses remain available for small control values）。1024 B 恰好等于 SWIZZLE_128B 的一个 atom（8 行 × 128 B，见 u5-l2/u6-l2），操作数区从 atom 边界起算是合理推断，但章节没有明说这一动机（**待确认**）；
3. **layout 绑定**：`mma_shared_layout(dtype, swizzle_mode, shape)` 从 dtype、swizzle 模式与 tile 形状构造 SMEM 布局，此处产生与当前 `tcgen05.mma` 派发匹配的 128 字节 swizzle 排布；`layout=A_layout` 把它绑到 `Asmem` 上——于是 Step 1 中 `Tx.cta.copy` 按这套布局写入、`tcgen05.mma` 按同一套排布读取（读写两端共享一份地址计算的纪律，u9-l3）。

容量记账：

\[ \text{Asmem} = 128 \times 64 \times 2\,\text{B} = 16384\,\text{B} = 16\,\text{KiB} \]

两个操作数共 32 KiB；加上 1 KiB 保留区，pool 总跨度 \(1024 + 32768 = 33792\,\text{B} \approx 33\,\text{KiB}\)，只占 B200 每 SM 228 KiB 共享内存的约 14%——Step 1 的 SMEM 压力微不足道，真正吃 SMEM 的是 Step 5 起的多级流水线缓冲。

TMEM 侧的策略则完全不同：一步到位 `n_cols=512`（满配 256 KiB），再用 `T.decl_buffer` 声明 \((128, 512)\) 的 fp32 buffer 并绑定标准布局 `S[(128, 512):(1@TLane, 1@TCol)]`，MMA 与读回都只使用前 `BLK_N=128` 列（`tmem[:, :BLK_N]`）。为什么不满打满算只要 128 列？u7-l3 讲过：分配只有五档、同一 CTA 的多次分配必须单调不增，所以应当**起步即申请最大需求**，之后按列切片——Step 1 干脆沿用了这个纪律。

初始化的发布协议：warp 0 内 lane 0 执行 `mbarrier.init`、warp 0 集体执行阻塞式的 `tcgen05.alloc`（把 TMEM 基地址写进 `tmem_addr` 槽）；随后两道 fence（`fence.proxy_async("shared::cta")` 与 `fence.mbarrier_init`）加一道 `cta_sync` ① 把初始化结果发布给全 CTA，之后 `decl_buffer` 才能安全地读 `tmem_addr[0]`。

#### 4.2.3 源码精读

**讲解版分配代码**（四部件小节先给出精简版）：

- [chapter_gemm_basics/index.md:L84-L92](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L84-L92) — `pool = T.SMEMPool()` 起手；`tmem_addr`（uint32）与 `mma_bar`（uint64、`align=8`）两个控制对象先行；`pool.move_base_to(1024)` 跳到偏移 1024；带 `layout=A_layout` / `layout=B_layout` 分配两个 \(128\times64\) fp16 操作数 buffer；`pool.commit()` 收尾。
- [chapter_gemm_basics/index.md:L94](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L94) — 逐句解释：`move_base_to(1024)` 把当前分配点移到字节偏移 1024，`Asmem` 从那里开始、`Bsmem` 跟在后面，低地址留给 `tmem_addr`、`mma_bar` 这类小控制值。
- [chapter_gemm_basics/index.md:L96](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L96) — `mma_shared_layout(dtype, swizzle_mode, shape)` 的三参数语义；此处生成与当前 `tcgen05.mma` 派发匹配的 128 字节 swizzle 排布。
- [chapter_gemm_basics/index.md:L98](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L98) — 一句关键纪律：Step 1 中 `Tx.cta.copy` 按这些布局**写**数据，`tcgen05.mma` 按匹配的排布**读**——同一物理排布的两端必须一致。

**完整内核中的对应段**：

- [chapter_gemm_basics/index.md:L211-L217](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L211-L217) — `# --- SMEM allocation ---` 段，与讲解版逐行相同（`BLK_M/BLK_N/BLK_K` 代入 128/128/64）。
- [chapter_gemm_basics/index.md:L219-L227](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L219-L227) — `# --- Barrier + TMEM init (warp 0 only) ---`：`if warp_id == 0` 内 lane 0 做 `mbarrier.init(mma_bar.ptr_to([0]), 1)`（期望到达数 1，见 4.3）、warp 0 集体做 `tcgen05.alloc(T.address_of(tmem_addr), n_cols=512, cta_group=1)`；随后 `fence.proxy_async` + `fence.mbarrier_init` + `cta_sync` ① 发布初始化。
- [chapter_gemm_basics/index.md:L229-L232](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L229-L232) — `tmem = T.decl_buffer((128, 512), "float32", scope="tmem", allocated_addr=tmem_addr[0], layout=TileLayout(S[(128, 512) : (1@TLane, 1@TCol)]))`：用刚拿到的基地址把 TMEM 绑定成带布局的类型化 buffer——行 \(m\) 映射到 `TLane` \(m\)、列 \(n\) 映射到 `TCol` \(n\)（cta_group::1、M=128 的恒等映射，u7-l2），MMA 的写端（`tmem[:, :BLK_N]`）与 4.4 的读端使用同一布局，正是「写的布局必须等于读的布局」。

**layout 的构造处**：

- [chapter_gemm_basics/index.md:L192-L193](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L192-L193) — `A_layout = mma_shared_layout(a_type, SwizzleMode.SWIZZLE_128B_ATOM, (BLK_M, BLK_K))`（B 同理）：swizzle 模式与 tile 形状在这里一次性定死，MMA 能直读 SMEM 依赖的正是这套排布（对照 u5-l2 的 Hopper 描述符、u6-l1 的 TMA 写入 swizzle——三代共享同一个「SMEM 里放 swizzle 过的 tile」的约定）。

交叉引用：同一构建器在入门章首次出现于 [chapter_intro_tirx/index.md:L85-L93](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_intro_tirx/index.md#L85-L93)（u9-l1 已精读，形态完全一致）。

#### 4.2.4 代码实践

**实践目标**：亲手核算 Step 1 的 SMEM 静态占用，为后续步骤的 SMEM 预算敏感度打基础。

**操作步骤**（示例代码，非项目原有）：

```python
# smem_budget.py —— 核算 Step 1 的 SMEM 静态占用（示例代码）
BLK_M, BLK_N, BLK_K = 128, 128, 64
B = 2                                   # fp16 每元素字节数

ctrl = 4 + 8                            # tmem_addr(uint32) + mma_bar(uint64, align 8)
operands = (BLK_M * BLK_K + BLK_N * BLK_K) * B   # Asmem + Bsmem
total = 1024 + operands                 # 操作数从偏移 1024 起摆放

print(f"控制对象区：{ctrl} B（位于 0..1024 的保留区内，未用满）")
print(f"操作数区  ：{operands} B（起始于 1024）")
print(f"pool 跨度 ：{total} B ≈ {total/1024:.1f} KiB")
print(f"占 B200 每 SM 228 KiB 的 {total/228/1024*100:.1f}%")
```

**需要观察的现象**：操作数区是多少 KiB？如果把 `BLK_K` 翻倍到 128，操作数区与 pool 跨度各变为多少？

**预期结果**：操作数区 32768 B = 32 KiB，pool 跨度 33792 B ≈ 33.0 KiB，约占每 SM 共享内存的 14.5%；`BLK_K=128` 时操作数区翻到 64 KiB、跨度约 65 KiB（顺带提醒：`BLK_K>64` 还会撞上 128B swizzle 的内维宽度约束，见 4.3.4）。纯 CPU 计算，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：`tmem_addr` 和 `mma_bar` 为什么必须分配在 SMEM，而不能是内核里的普通局部变量（寄存器）？

**答案**：二者都是跨线程共享的交接媒介。`tmem_addr` 由 warp 0 的 `tcgen05.alloc` 写入、随后全体线程经 `T.decl_buffer(..., allocated_addr=tmem_addr[0])` 读取；`mma_bar` 是硬件同步对象，warp 0 的 lane 0 初始化、被选线程 commit、全体线程 try_wait。寄存器线程私有，跨线程既不可见也不保证唯一实例，无法承担共享媒介的职责；SMEM 是 CTA 内天然的共享空间。

**练习 2**：TMEM 明明只需要 \(128\times128\) 个 fp32 累加器，为什么 `tcgen05.alloc` 申请 512 列？

**答案**：u7-l3 的分配纪律：`n_cols` 只有 32/64/128/256/512 五档，且同一 CTA 的多次分配必须单调不增——应当起步就申请最大需求，之后按列切片使用。128 列 fp32 累加器只占 `tmem[:, :128]`，其余列闲置也无妨；如果先申请 128 列、后续再想扩到 256 就会被单调性卡住。Step 1 沿用「一律 512 列」的写法还有个好处：后续步骤（如 Step 9 的双累加器区）无需改动分配逻辑。

**练习 3**：`mma_shared_layout` 绑定的布局同时服务于哪两个操作？如果二者描述的排布不一致会发生什么？

**答案**：同时服务于写入端 `Tx.cta.copy`（按布局把 GMEM 数据摆进 SMEM）与读取端 `tcgen05.mma`（按同一排布构造 SMEM 描述符读取）。若不一致，则「字节到了、元素认错」——MMA 会把 swizzle 后的位置当成逻辑位置去读，结果整体错乱而无越界报错。这正是 u9-l3「读写两端对同一元素给出相同位置」铁律的实例。

### 4.3 MMA 发起与等待：elect_sync 与 mbarrier 的配对

#### 4.3.1 概念说明

这一段是 Step 1 最核心的协议，也是本讲必答题（章末练习 1）的所在。要理解的设计问题有两个：

**问题一：谁来发起 MMA？** `tcgen05.mma` 是单线程语义的 tile 级指令（u7-l1）：一条指令声明整块 \(128\times128\times64\) 的乘加，由 Tensor Core 硬件完成。既然一个线程就够，就必须从 128 个线程里**恰好选出一个**——多选不行（128 个线程各发一次，计算会被启动 128 次），一个不选也不行。TIRx 的答案是双层守卫：`if warp_id == 0` 先把范围缩到 warp 0，`T.ptx.elect_sync()` 再从该 warp 的活跃 lane 中选出恰好一个。

**问题二：怎么知道算完了？** `tcgen05.mma` 异步执行——`Tx.gemm_async` 返回时 Tensor Core 可能还在更新 TMEM。完成信号走 mbarrier 通路：`tcgen05.commit` 把该线程已发起的异步 MMA 挂到 `mma_bar`，硬件在 MMA 真正完成后替它「到达」一次（这与 TMA 靠字节计数归零的完成路径不同，u7-l1/u8-l1）。因为 `mbarrier.init` 的期望到达数是 1，这一次硬件到达恰好把相位翻完成；全体线程随后 `try_wait(phase_mma=0)` 等待离开初始相位。

还有一层容易忽略的配对关系：**`try_wait` 写在守卫之外**。发起是单线程的特权，等待却是全体线程的义务——128 个线程都要读 TMEM，所以 128 个线程都要等 MMA 完成。

#### 4.3.2 核心流程

完整的发起-等待协议：

```text
if warp_id == 0:                      # 第一层守卫：缩到 warp 0
    if T.ptx.elect_sync():            # 第二层守卫：warp 内选出恰好 1 个 lane
        Tx.gemm_async(tmem[:, :BLK_N], Asmem[:, :], Bsmem[:, :],
                      accum=False, dispatch="tcgen05", cta_group=1)
        T.ptx.tcgen05.commit(mma_bar.ptr_to([0]), cta_group=1)

T.ptx.mbarrier.try_wait(mma_bar.ptr_to([0]), phase_mma)   # 全体线程等待
```

参数逐个读：累加器是 `tmem[:, :BLK_N]`（TMEM 前 128 列）；操作数是 `Asmem`、`Bsmem` 的完整切片；`accum=False` 表示**不**从 TMEM 读旧的部分和、直接开一个新累加器——本步只有一次 MMA，不存在「之前的和」；`dispatch="tcgen05"` 指定走 Tensor Core 的 tcgen05 通路；`cta_group=1` 表示单 CTA 模式（Step 8 才会用到 2）。

`Tx.gemm_async` 是 **tile 操作而非一条硬件指令**：tile 沿 K 有 64 个元素，而底层每条 MMA 指令只处理 16 个 K 元素（`MMA_K=16`），所以 TIRx 把它lowering 成一小串 `tcgen05.mma` 指令：

\[ N_{\text{instr}} = \lceil BLK_K / MMA_K \rceil = \lceil 64/16 \rceil = 4 \]

这个公式是本讲综合实践的推演主角（u9-l2 已在生成 CUDA 里验证过「一条 `Tx.gemm_async` 展开为 4 条 `tcgen05.mma`」）。

**章末练习 1 详解：为什么 `Tx.gemm_async` 之前必须有 `T.cuda.cta_sync()`？**

- [chapter_gemm_basics/index.md:L636](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L636) — 练习原文：Steps 1–3 中 `Tx.cta.copy` 先把 A、B tile 搬进 SMEM，为什么内核在 `Tx.gemm_async` 读取这些 SMEM tile 之前需要 `T.cuda.cta_sync()`？

参考答案分四层：

1. **生产者与消费者的 scope 不对称**。加载由**全部 128 个线程**协作完成（`Tx.cta.copy` 把拷贝分布到 CTA 线程上），而 MMA 只由 warp 0 中 `elect_sync` 选出的**一个线程**发起。GPU 对不同线程的执行进度没有任何顺序保证——被选中的那个线程完全可能跑到发射点时，其他 127 个线程还没写完自己那份 SMEM。
2. **竞态的后果是读到残缺操作数**。Tensor Core 一旦启动就会按 SMEM 布局整块读取 `Asmem`/`Bsmem`；若部分行尚未写入，MMA 用的是「一半新一半旧」的数据，结果错误且逐次运行不定。这是典型的 read-after-write 跨线程依赖。
3. **`cta_sync` 恰好补齐两件事**。章节原话（L108）：它 waits for every thread（等所有线程到位，即拷贝**完成**）且 makes their shared-memory writes visible（使共享内存写入对后续 MMA **可见**）。落到 CUDA 层就是 `__syncthreads()` 的「汇合 + 内存可见性」双重语义，在生产者与消费者之间建立了 happens-before 顺序。
4. **别的机制为何帮不上**。本步的 `mma_bar` 只追踪 **MMA 自身的完成**（输出侧，见 L72 的措辞 tracks MMA completion），管不着输入数据是否到位；`phase_mma` 也只是等待的相位参数。全 CTA 线程级的汇合原语只有 `cta_sync` 一个。顺带预告：Step 4 把生产者换成 TMA 引擎后，连 `cta_sync` 也不够了——线程汇合观察不到引擎进度，必须改用 mbarrier 的 `expect_tx` 字节计数（u6-l3/u8-l1），这正是「换 dispatch 牵动同步协议」的实例。

#### 4.3.3 源码精读

**讲解版（四部件小节）**：

- [chapter_gemm_basics/index.md:L112-L120](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L112-L120) — 发起与等待的完整代码：双层守卫内的 `Tx.gemm_async`（`accum=False, dispatch="tcgen05", cta_group=1`）与 `tcgen05.commit`，守卫**之外**的 `mbarrier.try_wait(..., phase_mma)`。
- [chapter_gemm_basics/index.md:L122](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L122) — 外层 `if warp_id == 0` 只留 warp 0，`elect_sync` 再从该 warp 选一个活跃 lane；因此**恰好一个线程**执行 `Tx.gemm_async` 与 `tcgen05.commit`。
- [chapter_gemm_basics/index.md:L124](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L124) — 关键澄清：单线程发射**不等于**单线程矩阵乘——硬件仍然执行由 SMEM 操作数布局与 TMEM 累加器布局描述的 tile 级 MMA；反过来说，如果 128 个线程都发射同一操作，计算会被启动 128 次。
- [chapter_gemm_basics/index.md:L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L126) — `Tx.gemm_async` 是 tile 操作而非一条指令：tile 沿 K 64 元素、每条底层指令处理 16 个，故 lowering 为一小串 `tcgen05.mma`（即 \(\lceil 64/16\rceil=4\) 条）。
- [chapter_gemm_basics/index.md:L128](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L128) — `tcgen05.mma` 异步；`tcgen05.commit` 把已发射的 MMA 关联到 `mma_bar`，warpgroup 在 mbarrier 上等待后才能从 TMEM 读结果。
- [chapter_gemm_basics/index.md:L130](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L130) — `accum=False` 开新累加器而非读旧部分和；本步只有一次 tile 操作、无更早的和；Step 2 起的后续 K 迭代改用 `accum=True`。

**完整内核中的对应段**：

- [chapter_gemm_basics/index.md:L245-L254](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L245-L254) — `# --- Compute: single elected thread issues MMA ---`：与讲解版逐句一致；注意 L254 的 `try_wait` 顶格书写（不在任何 `if` 内），全体 128 线程都执行。
- [chapter_gemm_basics/index.md:L236](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L236) — `phase_mma: T.int32 = 0`：barrier 在本步**只使用一次**，等待「离开初始相位 0」即正确，无需任何翻转；Step 2 循环复用同一 barrier 后，漏翻相位会让 wait 过早通过（u11-l3 的主题，先埋下伏笔）。
- [chapter_gemm_basics/index.md:L222](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L222) — `T.ptx.mbarrier.init(mma_bar.ptr_to([0]), 1)`：期望到达数为 1，正好对应 `tcgen05.commit` 挂靠的那**一次**硬件到达——init 的计数与完成信号的来源必须严格对账（u8-l1 的纪律）。

#### 4.3.4 代码实践

**实践目标**：用「指令数 = \(\lceil BLK_K/16 \rceil\)」这条规律做修改-预测-验证（有 Blackwell GPU 时上机，无 GPU 时做推演），体会 tile 操作与硬件指令的展开关系。

**操作步骤**：

1. 先做推演（示例代码，非项目原有）：

```python
# mma_count.py —— Tx.gemm_async 的 tcgen05.mma 指令数推演（示例代码）
MMA_K = 16                      # 底层硬件 MMA 的 K 宽度（内核 L190 文档值）
for BLK_K in (16, 32, 64, 128):
    n_instr = -(-BLK_K // MMA_K)            # ceil(BLK_K / 16)
    note = "" if BLK_K <= 64 else "  # 超出 SWIZZLE_128B 对 fp16 内维 64 元素的宽度，需重组布局"
    print(f"BLK_K={BLK_K:3d} -> {n_instr} 条 tcgen05.mma{note}")
```

2. 有 Blackwell GPU 时上机验证：把 4.1.3 驱动中的 `M, N, K` 与内核里的 `BLK_K` **一起**改成同一个值（单 tile 内核要求 \(K = BLK_K\)，否则 `A[:, :]` 切片与 `Asmem` 形状不符），分别取 32 与 16；每次修改后**重启 Python 会话**再编译（L276 警示）；用 `ex.mod.imports[0].inspect_source()`（u9-l2）打印生成 CUDA，统计其中 `tcgen05.mma` 指令（内联 PTX 字符串）的出现次数。
3. 无 GPU 时：用 `kernel.show()` / `kernel.script()` 观察 lowering 前的 tile 级 IR 中 `gemm_async` 的形态（构造与检视不需要 Blackwell，u9-l2），指令级计数标注「待本地验证」。

**需要观察的现象**：`BLK_K=32` 时生成源码中 `tcgen05.mma` 出现几次？`BLK_K=16` 呢？`BLK_K=64`（原值）应该与 u9-l2 记录的 4 条一致。

**预期结果**：16 → 1 条、32 → 2 条、64 → 4 条，严格符合 \(\lceil BLK_K/16\rceil\)；`BLK_K=128` 的推演值虽为 8，但 fp16 下 SWIZZLE_128B 的最内连续维不能超过 64 元素（128 B，u6-l1/u6-l2 的约束在此同样适用——此为推断，**待本地验证**），加大 `BLK_K` 不是本实验的推荐路径。GPU 上的计数结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把双层守卫（`if warp_id == 0` 与 `elect_sync`）全部去掉，让 128 个线程都执行 `Tx.gemm_async`，会发生什么？结果会错多少？

**答案**：章节 L124 直接回答：计算会被**启动 128 次**。每次发射都会让 Tensor Core 把 `Asmem × Bsmem` 累加进同一个 `tmem[:, :128]`：如果 128 次全部完成，结果是正确值的 128 倍；更糟的是这些异步 MMA 与 commit/barrier 的关联也乱了，完成时序不可控，输出介于 1 到 128 倍之间且逐次运行不定。守卫的职责不是「提速」，而是把发射权收敛到恰好一个线程。

**练习 2**：`mbarrier.init` 的到达计数为什么是 1？这次到达由谁完成？

**答案**：本步中 `mma_bar` 的唯一到达者是 **Tensor Core 硬件**——`tcgen05.commit` 把已发射的 MMA 挂到 barrier 后，硬件在 MMA 真正完成时替它补一次到达（u7-l1 的机制）。没有第二个到达者（TMA 的 expect_tx、普通线程的 arrive 都不涉及），所以期望计数就是 1。若误设为 2，相位永远无法翻完成，`try_wait` 将无限等待、内核挂死。

**练习 3**：`try_wait` 为什么写在守卫之外、让全体 128 线程执行？只让被选线程等行不行？

**答案**：等待的目的是「读 TMEM 之前确认 MMA 已完成」，而读 TMEM 的是**全体**线程（4.4 的 `Tx.wg.copy_async` 是 warpgroup 集体操作）。若只有被选线程等待，其余 127 个线程会带着未完成的累加器直接进入回写段，读到过期数据。发射权可以是单线程的特权，等待义务必须是全体的——这就是「发起与执行分离」在同步协议上的投影。

### 4.4 TMEM 读回：一条寄存器视图打通 epilogue

#### 4.4.1 概念说明

MMA 结束后，结果以 \(128\times128\) 的 **fp32** 累加器形态躺在 TMEM 里，而 D 要求以 **fp16** 写入 GMEM。两个约束决定了回写必须**绕道寄存器**：

- **类型转换只能在寄存器做**——章节原话：epilogue 先把累加器读进寄存器、转换在那里完成（TMEM 与 GMEM 之间没有直接通路，也没有转换能力）；
- **fp32 累加是精度纪律**——沿 K 累加的舍入误差随次数增长，fp32 能压住；输出端才降回 fp16（u11-l1 的精度约定）。

这段「TMEM → 寄存器 → GMEM」的收尾就是 **epilogue** 的最简形态（u11-l1 指出：完整版还会经 SMEM 中转走 TMA store，Step 1 的简版是 RF 直写 GMEM——站点序列一致，差的是最后一段的载具）。

本段最值得学的是一个 **TIRx 布局技巧**：同一段物理寄存器，先以每线程私有的 `Dreg` 看待，再用 `Dreg.view(...)` 套上 warpgroup 级布局 `Dreg_wg`，就变成了「128 线程 × 128 列」的集体视图——`Tx.wg.copy_async` 以集体视图为操作数，才能正确降级为 warp 集体的 `tcgen05.ld`。

#### 4.4.2 核心流程

回写的五步：

1. **私有缓冲**：`Dreg = T.alloc_local((BLK_N,), acc_type)`——每线程一段 128 元素的 fp32 私有缓冲；`Dreg_f16` 同规格的 fp16 缓冲待命；
2. **集体视图**：`Dreg_wg = Dreg.view(128, BLK_N, layout=TileLayout(S[(128, BLK_N):(1@tid_in_wg, 1)]))`——布局把第一个 tile 维映射到 `tid_in_wg`（线程 0 拥第 0 行、线程 1 拥第 1 行……直到 127），第二维留在各线程的寄存器槽位内。于是 **128 个线程与 128 个输出行一一对应，每线程持有一整行**；
3. **集体加载**：`Tx.wg.copy_async(Dreg_wg[:, :], tmem[:, :BLK_N])`——warpgroup 集体执行，降级为 Blackwell 的 `tcgen05.ld`。布局自洽性可以核对：累加器布局里行 \(m\) 落在 `TLane` \(m\)（4.2.3），而 `tid_in_wg = warp_id × 32 + lane_id`——warp \(w\) 的线程恰好只读 lane 区间 \([32w, 32w+31]\)，**正好是 u7-l3 的 warp lane 访问窗口**，布局自动合法；
4. **异步等待**：`T.ptx.tcgen05.wait.ld()`——`tcgen05.ld` 异步，任何线程使用 `Dreg` 之前必须等它完成（u7-l4 的纪律）；
5. **转换与按行写出**：`Tx.cast(Dreg_f16[:], Dreg[:])` 做 fp32→fp16；每线程算出自己的全局行

\[ m_{\text{thr}} = m_{st} + warp\_id \times 32 + lane\_id \]

（单 tile 下 \(m_{st}=0\)），然后 `Tx.copy(D[m_thr, n_st:n_st+BLK_N], Dreg_f16[:])` 写出整行。四个 warp 依次覆盖第 0–31、32–63、64–95、96–127 行。

寄存器压力粗估（上界，编译器可能复用压低）：`Dreg` 128 个 fp32 需 128 个 32 位寄存器，`Dreg_f16` 128 个 fp16 打包需 64 个，合计约 192 个/线程——数量级上解释了为什么 Blackwell 把累加器放进 TMEM、寄存器只承担 epilogue 边界的一行（u5-l3 的主题在此兑现）。

#### 4.4.3 源码精读

**讲解版（四部件小节）**：

- [chapter_gemm_basics/index.md:L134-L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L134-L143) — 回写段代码：两个 `alloc_local` 私有缓冲、`Dreg.view(128, BLK_N, layout=...)` 集体视图、`Tx.wg.copy_async` 加载、`wait.ld`、`Tx.cast` 转换、`m_thr` 行号计算与 `Tx.copy` 按行写出。
- [chapter_gemm_basics/index.md:L145](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L145) — MMA 在 TMEM 留下 \(128\times128\) fp32 累加器 tile；沿 K 用 fp32 累加减少舍入误差；因 D 是 fp16，结果必须过寄存器并在那里转换后才能存 GMEM。
- [chapter_gemm_basics/index.md:L147-L153](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L147-L153) — 两个寄存器缓冲的分工：`Dreg` 是每线程私有的 `BLK_N` 元缓冲，`Dreg_wg` 给同一段寄存器套上布局、暴露为 warpgroup 级视图；布局 `S[(128, BLK_N):(1@tid_in_wg, 1)]` 把第一维映射到 warpgroup 线程（线程 0 拥行 0……线程 127 拥行 127），第二维留在每线程寄存器内——每线程持有一整行，128 线程与 128 输出行一一对应。
- [chapter_gemm_basics/index.md:L155](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L155) — `Tx.wg.copy_async(Dreg_wg, tmem)` 经该视图读累加器，降级为 Blackwell 的 TMEM 加载指令 `tcgen05.ld`；加载是异步的，任何线程使用 `Dreg` 前必须先完成 `T.ptx.tcgen05.wait.ld()`。
- [chapter_gemm_basics/index.md:L157-L163](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L157-L163) — 等待之后每线程的 `Dreg[:]` 即其逻辑输出行的 fp32 值；`m_thr = m_st + warp_id * 32 + lane_id` 算全局行；四个 warp 分别覆盖第 0–31、32–63、64–95、96–127 行。

**完整内核中的对应段**：

- [chapter_gemm_basics/index.md:L256-L265](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L256-L265) — `# --- Writeback: TMEM -> RF -> GMEM ---` 段，与讲解版一致；注意读的是 `tmem[:, :BLK_N]`——与 MMA 写入的列区间严格相同（写读同布局的又一体现）。
- [chapter_gemm_basics/index.md:L267-L271](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L267-L271) — `# --- Deallocate TMEM ---`：先 `cta_sync` ③（全体线程的 `tcgen05.ld` 都过完各自的 `wait.ld` 并汇合，TMEM 才能安全释放），再由 warp 0 执行 `relinquish_alloc_permit` 与 `dealloc(tmem_addr[0], n_cols=512)`——释放与分配对称，都归 warp 0（u7-l3 的调用顺序模板）。

#### 4.4.4 代码实践

**实践目标**：把「128 线程 ↔ 128 输出行」的映射脚本化，并核验它自动满足 warp 的 TMEM lane 访问窗口。

**操作步骤**（示例代码，非项目原有）：

```python
# row_map.py —— Step 1 回写阶段的线程→行映射与 lane 窗口核验（示例代码）
BLK_N = 128
ok = True
for warp_id in range(4):
    rows = [warp_id * 32 + lane_id for lane_id in range(32)]   # m_thr 公式（m_st=0）
    window = (warp_id * 32, warp_id * 32 + 31)                  # u7-l3 的 warp lane 窗口
    inside = all(window[0] <= r <= window[1] for r in rows)
    ok &= inside
    print(f"warp {warp_id}: 行 {rows[0]:3d}..{rows[-1]:3d}  "
          f"TMEM lane 窗口 [{window[0]},{window[1]}]  落在窗口内: {inside}")
print("布局 S[(128,BLK_N):(1@tid_in_wg,1)] 对所有 warp 窗口合法:", ok)
```

**需要观察的现象**：每个 warp 负责的 32 个连续行是否恰好等于它被允许访问的 32 个 TMEM lane？如果布局写成 `S[(128, BLK_N):(1@laneid, ...)]` 之类的错轴，窗口核验还能过吗？

**预期结果**：四个 warp 分别映射行 0–31 / 32–63 / 64–95 / 96–127，与各自的 lane 窗口完全重合，输出 `True`。若布局错把行映射到 `laneid`（不带 warpid 分量），warp 1–3 的线程都会试图读窗口外的 lane，`tcgen05.ld` 行为非法——这正是布局记号里轴名（`tid_in_wg` 而非 `laneid`）承载语义的实例（u4-l2/u10-l2）。纯 CPU 计算，无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**：为什么回写不能从 TMEM 直接写 GMEM，必须绕道寄存器？

**答案**：两个原因。其一，类型转换只能在寄存器做：TMEM 里是 fp32 累加结果，D 要 fp16，章节明说 epilogue 先读进寄存器、转换在那里完成；其二，TMEM 与 GMEM 之间没有直接数据通路——TMEM 的常规出口就是 `tcgen05.ld` 到寄存器（u7-l4），完整版 epilogue 也是先到 RF、再经 SMEM 中转走 TMA store（u11-l1 的六站五跳）。

**练习 2**：warp 2 的 lane 17 写 D 的哪一行？它的 `Dreg` 里装的是什么？

**答案**：\(m_{thr} = 0 + 2\times32 + 17 = 81\)，即 D 的第 81 行。`Dreg` 装的是该行的 128 个 fp32 值（`Tx.cast` 后 `Dreg_f16` 是同一行的 fp16 版），由 `Tx.copy` 一次性写出 `D[81, 0:128]`。

**练习 3**：`Dreg` 与 `Dreg_wg` 是两份不同的存储吗？`view` 做了什么？

**答案**：不是——`Dreg_wg = Dreg.view(128, BLK_N, layout=...)` 只是把**同一段**每线程私有寄存器重新解释成带布局的 warpgroup 级视图，不搬任何数据（呼应 u4-l1「视图操作只改元数据」）。私有的 `Dreg` 服务于逐线程操作（`Tx.cast`、`Tx.copy`），集体的 `Dreg_wg` 服务于 warpgroup 集体操作（`Tx.wg.copy_async`）；一份存储、两种身份，由布局记号切换。

## 5. 综合实践

**任务**：完整复现 Step 1 内核，书面回答章末练习 1，再做 `BLK_K` 修改实验——把本讲四个模块串成一条「跑通 → 说清 → 改动 → 预测 → 验证」的闭环。

**操作步骤**：

1. **复现**。把 [chapter_gemm_basics/index.md:L169-L321](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L169-L321) 的 imports、`hgemm_v1` 与驱动逐字敲进一个全新 Python 会话的文件 / notebook 单元格（TIRx 靠源码检视解析内核，不能塞进 `python -c`，见 u1-l3）。有 Blackwell GPU 时运行至打印 `Max error` 与 `PASS`；无 GPU 时停在 `tvm.compile` 之前的构造与检视环节（`kernel.show()`），运行结果**待本地验证**。
2. **答题**。合上讲义，书面回答练习 1（为何 `Tx.gemm_async` 前需要 `T.cuda.cta_sync()`），要求覆盖四层：scope 不对称（128 生产者 vs 1 消费者）、竞态后果（残缺操作数）、`cta_sync` 的双重语义（完成 + 可见）、为何其他机制帮不上（`mma_bar` 只管输出侧完成）。答完对照 4.3.2 自评。
3. **实验**。按 4.3.4 做 `BLK_K ∈ {32, 16}` 的修改-预测-验证：每次同时改内核的 `BLK_K` 与驱动的 `K`（保持 \(K = BLK_K\)），**重启会话**再编译；预测表先填好，再数生成源码里的 `tcgen05.mma` 条数。无 GPU 时提交推演表与验证方案（`inspect_source()` 中统计指令出现次数），标注「待本地验证」。
4. **加分项**。把 4.2.4 的 SMEM 预算与 4.4.4 的行映射脚本跑一遍，附在实验报告里；观察驱动里可选计时段输出的 TFLOPS 量级（单 tile 只有约 2.1 MFLOP 工作量，数值极小是正常现象）。

**需要观察的现象**：`PASS` 是否出现、`max_err` 量级（应在 `rtol=2e-2, atol=1e-2` 容差内）；`BLK_K` 减半时指令数是否严格减半；两次实验之间是否遵守了「一会话一步」的纪律。

**预期结果**：基线运行 `PASS`；指令数 64→4、32→2、16→1，符合 \(\lceil BLK_K/16\rceil\)；SMEM 预算约 33 KiB；行映射四 warp 窗口全部合法。GPU 相关结果**待本地验证**。

## 6. 本讲小结

- **四部件结构**：单 tile 内核 = 分配（SMEMPool + `tcgen05.alloc(512 列)` + mbarrier）→ 加载（全 CTA 同步 `Tx.cta.copy`）→ 计算发起（单线程 `Tx.gemm_async` + `commit`，全体 `try_wait`）→ 回写（`tcgen05.ld` → 寄存器 → fp16 → 按行写 GMEM），外加 TMEM 释放；三处 `cta_sync` 各守一道交接。
- **练习 1 的答案**：加载由 128 线程协作、MMA 只由一个 `elect_sync` 选出的线程发起，跨线程无顺序保证；`cta_sync` 同时提供「等全部线程完成拷贝」与「SMEM 写入对 MMA 可见」两重保障——而 `mma_bar` 只追踪 MMA 自身完成，补不上输入侧的依赖。
- **发射权收敛**：双层守卫（`warp_id == 0` + `elect_sync`）保证恰好一个线程发射；单线程发射不等于单线程计算，若 128 线程都发射，计算会被启动 128 次。等待义务则归全体：`try_wait` 写在守卫之外。
- **tile 操作与指令的展开**：一条 `Tx.gemm_async` 按 \(\lceil BLK_K/16\rceil\) 展开为 `tcgen05.mma` 指令序列（64→4 条）；`accum=False` 开新累加器，Step 2 的 K 循环才需要 `accum=True` 与相位翻转。
- **SMEM 布局**：控制对象（`tmem_addr`、`mma_bar`）占低地址保留区，操作数从 `move_base_to(1024)` 起摆放；`mma_shared_layout` 生成的 128B swizzle 排布同时约束写端（`Tx.cta.copy`）与读端（`tcgen05.mma`）。
- **TMEM 读回**：`Dreg.view` 用布局 `S[(128, BLK_N):(1@tid_in_wg, 1)]` 把私有寄存器变成「128 线程 × 128 行」的集体视图，`tcgen05.ld` 异步读回须 `wait::ld`；行号 \(m_{thr}=m_{st}+warp\_id\times32+lane\_id\) 使每 warp 恰好落在自己的 TMEM lane 窗口内。

## 7. 下一步学习建议

下一讲 **u11-l3（Step 2：K 循环累加）**只改一件事：把 Load→MMA→wait 套进 `T.serial` 循环、让每次 MMA 累加进同一 TMEM 槽（`accum=(i != 0)`），由此引出本讲埋下的两颗种子——`phase_mma ^= 1` 的相位翻转（对照 u8-l2 的相位复用理论）与漏翻后的「wait 过早通过」。建议：读 Step 2 前重做本讲 4.3 的练习 2（到达计数对账），再自问「同一道 barrier 第二次使用时，`try_wait` 该等哪个相位」。之后 u11-l4（Step 3 空间分块）只是放大 grid，内核内部零改动。进入单元十二（Step 4 的 TMA）之前，建议回看 u6-l3（TMA 完成机制）与 u8-l1（mbarrier 字节追踪）——届时本讲练习 1 答案的第 4 层（「换引擎后 `cta_sync` 为何不够」）将成为主线。
