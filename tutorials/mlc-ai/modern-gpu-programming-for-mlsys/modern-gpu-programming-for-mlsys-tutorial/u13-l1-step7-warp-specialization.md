# Step 7：warp 角色划分与四道屏障

## 1. 本讲目标

本讲精读 GEMM 优化路线的第七步 `hgemm_v7`。学完后你应该能够：

1. 说清 Step 7 相对 Step 6 改了什么、没改什么（三要素中只有 scope 变化）。
2. 画出 TMA producer、MMA consumer、writeback 三类角色的分工图，并在内核源码中指出每个角色由哪些 warp 承担。
3. 逐条解释四道屏障（`tma2mma`、`mma2tma`、`mma2ld`、`ld2mma`）各自保护什么资源、谁等待、谁到达、为什么选用 `TMABar`/`TCGen05Bar`/`MBarrier` 三种不同类型。
4. 推演 `PipelineState` 的 stage 与 phase 序列，并独立完成章末练习 1：初始相位设错时的死锁场景。
5. 解释 `warpgroup_sync(10)` 为什么不能换成 `cta_sync()`，以及回写路径上两道命名屏障各防什么。

## 2. 前置知识

本讲建立在以下已建立的认知之上（对应讲义 u6-l3、u7-l1、u7-l4、u8-l1、u8-l2、u12-l1～u12-l3），这里只做要点回顾：

- **mbarrier 状态机**：一道 mbarrier 同时维护相位奇偶（phase parity）、到达计数与在途字节计数（tx-count）；`try_wait(P)` 在屏障离开相位 \(P\) 后才返回，当前 parity 等于 \(P\) 时阻塞。新初始化的屏障从相位 0 出发。
- **TMA 完成机制**：load 用 mbarrier 字节计数追踪——发起线程 `arrive.expect_tx` 登记字节数，TMA 引擎每完成一段传输扣减一次；store 反过来关心「源缓冲何时可复用」，用 `commit_group` / `wait_group(0)` 排空。
- **tcgen05 的完成信号**：`tcgen05.commit` 把已发起的异步 MMA 挂到 mbarrier 上，由**硬件在 MMA 真正完成后**补一次到达；`tcgen05.wait.ld()` 确认异步的 TMEM→寄存器读取已填充目的寄存器。
- **Step 5 双缓冲**：SMEM 操作数缓冲带 `PIPE_DEPTH` 前导维构成环形缓冲，第 \(k\) 个 K tile 使用 stage \(= k \bmod S\)。
- **Step 6 持久内核**：launch 固定 `SM_COUNT=148` 个 CTA，`ClusterPersistentScheduler2D` 用 `init/valid/next_tile` 接口让每个 CTA 循环认领输出 tile。
- **三要素分析框架**：每个 tile 操作由 scope（谁执行）、layout（数据怎么摆）、dispatch（走哪条硬件路径）刻画。

Step 6 结束时内核的处境是：SMEM 环形流水与持久调度都已就位，**但整个流水线仍由一个 warpgroup 串行控制**——发 load、等数据、发 MMA、回写全部由同一组线程依次完成。Step 7 要解决的就是这个「控制权集中」的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) | 本讲主源码。L20–123 是 Step 7 的机制讲解（角色、屏障、PipelineState、warpgroup_sync、epilogue），L124–303 是完整 `hgemm_v7` 内核，L305–323 是排查清单与 SMEM 成本分析，L907 是章末练习 1 |
| [img/scripts/gen_warp_specialization_timeline.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_warp_specialization_timeline.py) | 生成书中「串行 vs 并发」时间线对比图（`img/warp_specialization_timeline.png`）的 matplotlib 脚本，三条泳道对应三类角色 |

另外，内核 import 的 `TMABar`、`TCGen05Bar`、`MBarrier`、`PipelineState` 来自 `tvm.backend.cuda.lang.pipeline`（Apache TVM 包），`ClusterPersistentScheduler2D` 来自 `tvm.backend.cuda.lang.tile_scheduler`——它们不在本仓库内，本讲按书中用法讲解其行为。

## 4. 核心概念与源码讲解

### 4.1 warp 角色：从串行到并发

#### 4.1.1 概念说明

到 Step 6 为止，虽然 TMA 引擎、Tensor Core、回写路径是三套独立硬件，但**发指令和等同步的是同一组线程**。单 warpgroup 在等数据时不能发 MMA，在算 MMA 时不能发下一个 load——硬件各自有空闲，控制流却是串行的。书中时间线图的上半部分（改编自 Step 4 时序）把这种形态总结为 "Sequential - 50% hardware idle"：Load 与 MMA 交替占用时间轴，TMA 引擎与 Tensor Core 各闲一半。

**warp specialization（warp 特化）** 的思路是：既然控制流串行是瓶颈，就把控制权拆开——让不同的 warp 常年驻留在流水线的不同段上，各自跑自己的小循环，用屏障交接数据与缓冲所有权。这样 TMA producer 在预取下一个 tile 的同时，MMA consumer 正在算当前 tile，回写组独立地把上一个 tile 的结果搬走。

Step 7 的执行结构变化，书中用三要素明确框定（[chapter_gemm_advanced/index.md:L24-L27](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L24-L27)）：

- **Scope**：单 warpgroup 的串行 load→MMA→writeback 路径，变成三个并发角色（TMA producer、MMA consumer、writeback），用 full/empty 屏障连接。
- **Layout**：不变，沿用 Step 6 的 SMEM 多级 stage 与 TMEM 累加器。
- **Dispatch**：不变，仍是 TMA load 与 `tcgen05` MMA。

也就是说，**Step 7 只改 scope**——这是纯调度层面的重构，数据摆法与硬件路径原封不动。

#### 4.1.2 核心流程

内核用 `WG_NUMBER=2` 启动两个 warpgroup（每 CTA 共 256 线程），角色分配如下（[chapter_gemm_advanced/index.md:L50-L56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L50-L56)）：

| 角色 | 位置 | 职责 |
|------|------|------|
| TMA Producer | Warpgroup 1, warp 3 | 持续用 TMA 装载 A、B tile |
| MMA Consumer | Warpgroup 1, warp 0 | 数据一就绪就发起 MMA |
| Writeback | Warpgroup 0（全部 4 个 warp） | 读 TMEM 结果，写回 GMEM |

并发形态（对应时间线图下半部分）：

```text
WG1 warp 3 (TMA):   Load k=0 → Load k=1 → Load k=2 →  Load k=3 → ...
                       │full     │empty      │full
WG1 warp 0 (MMA):   （等）→  MMA k=0 →   MMA k=1  →   MMA k=2 → ...
                                │结果就绪
WG0 (回写):                        （等）→ Writeback tile0 → ...
```

三个角色各自持有私有的循环与流水线状态，唯一的交互方式是屏障；稳态下三段硬件（TMA 引擎、Tensor Core、回写所用的 TMA store + CUDA core）同时忙碌。注意图中 `mma2tma`（empty 方向）的箭头会**跨过一级 stage**——原因见 4.2.2 的环形复用分析。

#### 4.1.3 源码精读

**① 角色常量与布局声明**。块大小、流水线深度、warpgroup 数在内核外层定义（[chapter_gemm_advanced/index.md:L142-L149](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L142-L149)）：

```python
BLK_M, BLK_N, BLK_K = 128, 128, 64
K_TILES = K // BLK_K
PIPE_DEPTH = 2
WG_NUMBER = 2

A_layout = mma_shared_layout(a_type, SwizzleMode.SWIZZLE_128B_ATOM, (PIPE_DEPTH, BLK_M, BLK_K))
B_layout = mma_shared_layout(b_type, SwizzleMode.SWIZZLE_128B_ATOM, (PIPE_DEPTH, BLK_N, BLK_K))
```

`PIPE_DEPTH=2` 是让 load 与 compute 重叠的**最小**深度；更深的流水能隐藏更多访存延迟，但吃更多 SMEM（成本账见第 5 节综合实践）。布局带 `PIPE_DEPTH` 前导维，与 Step 5 完全一致。

**② 线程标识与角色守卫**。内核入口取得四级线程标识（[chapter_gemm_advanced/index.md:L157-L161](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L157-L161)）：

```python
T.device_entry()
bx = T.cta_id([SM_COUNT])
wg_id = T.warpgroup_id([WG_NUMBER])
warp_id = T.warp_id_in_wg([4])
lane_id = T.lane_id([32])
```

角色划分就是用这些标识做 if 守卫：`if wg_id == 1: if warp_id == 3:` 进入 TMA producer，`elif warp_id == 0:` 进入 MMA consumer，`elif wg_id == 0:` 进入回写（[chapter_gemm_advanced/index.md:L205-L206](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L205-L206) 与 [L259](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L259)）。这正是 u9-l3 所说「scope 由操作名前缀与 if 守卫表达」的典型现场：每个 warp 编译后走各自分支，互不知道对方的循环。

**③ 单线程发起用 `elect_sync` + `T.filter`**。两个 WG1 角色的循环都包在同一模式里（[chapter_gemm_advanced/index.md:L221-L222](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L221-L222)，消费者侧在 [L236-L237](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L236-L237)）：

```python
if T.filter(lane_id, T.ptx.elect_sync()):
    while tile_scheduler.valid():
        for k in range(K_TILES):
            ...
```

`elect_sync` 在 warp 内选出唯一 lane，`T.filter` 只让被选中的 lane 真正执行循环体——TMA load 与 MMA 都只需一个线程发起（发起与执行分离），其余 lane 掩蔽。持久调度器 `ClusterPersistentScheduler2D` 原样从 Step 6 继承（[chapter_gemm_advanced/index.md:L195-L200](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L195-L200)）。

**④ 时间线图脚本**。书中的前后对比图由 [gen_warp_specialization_timeline.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_warp_specialization_timeline.py) 生成：上半部分画串行 Load/MMA 交替（[L12-L24](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_warp_specialization_timeline.py#L12-L24)，标题即 "50% hardware idle"）；下半部分画三条泳道 `TMA (WG1 warp 3)`、`MMA (WG1 warp 0)`、`Writeback (WG0)`（[L26-L72](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_warp_specialization_timeline.py#L26-L72)），并用虚线箭头标出 `tma2mma` 与 `mma2tma` 两道屏障的交接方向（[L74-L86](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_warp_specialization_timeline.py#L74-L86)）。图中下半部分标题写明 "TMA at most 1 stage ahead of MMA"——深度为 2 时生产者最多领先消费者一级。

顺带一个容易忽略的细节：WG1 的 warp 1、warp 2 不满足任何角色守卫，直接落到函数末尾的全 CTA `cta_sync()`（[chapter_gemm_advanced/index.md:L297](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L297)）等待收尾。它们是给后续步骤的角色（如 Step 9 的第二个消费者与回写组）预留的空间。

#### 4.1.4 代码实践

**实践目标**：把「角色—代码位置」对应关系落实到行号，并从图中读出并发语义。

**操作步骤**：

1. 打开 [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) 的 L202–L294，用三色笔分别标出三个角色的代码区间。
2. 运行时间线图脚本，本地重生成对比图：

   ```bash
   cd img/scripts
   python gen_warp_specialization_timeline.py
   ```

3. 对照生成出的 `img/warp_specialization_timeline.png`（或直接读脚本 L48–L51 的标签数组 `shown_k`、`buf_labels`），确认下半部分的缓冲标注序列 `buf 0, buf 1, buf 0, …` 与 \(k \bmod 2\) 一致。

**需要观察的现象**：图中 MMA 泳道的第一个方框相对 TMA 泳道右移了一段（生产者先跑）；`mma2tma` 箭头从 `MMA k=0` 指回的是 `Load k=2` 而非 `Load k=1`。

**预期结果**：三角色区间分别落在 L206–L229（producer）、L231–L254（consumer）、L259–L294（writeback）；脚本运行后打印 `Saved warp_specialization_timeline.png`。本实践不依赖 GPU。

#### 4.1.5 小练习与答案

**练习 1**：时间线图上半部分为什么标注 "50% hardware idle"？

**答案**：串行调度下同一时刻只有 Load 或 MMA 之一在推进，TMA 引擎与 Tensor Core 各自有约一半时间没有工作；脚本 L16 的标题文本即此含义。Steps 5/6 的双缓冲与持久调度改善了供给，但没有把 load 和 compute 拆成独立角色，所以这个串行形态一直延续到 Step 6。

**练习 2**：TMA producer 和 MMA consumer 同在 Warpgroup 1，会不会互相干扰？

**答案**：不会。两个角色由互斥的 if 分支（`warp_id == 3` 与 `warp_id == 0`）划分，各自持有独立的 `while` 循环与私有 `PipelineState`，唯一的交互是屏障；而且两者都只需单线程发起（`elect_sync`），一个 warp 内 32 个 lane 中 31 个被掩蔽，执行资源占用极小。

**练习 3**：Step 7 的 layout 与 dispatch 相对 Step 6 完全不变，这说明了什么？

**答案**：说明「角色划分」是独立于数据摆法与硬件路径的第三根杠杆。u9-l3 的三要素框架里，Step 7 是一次纯 scope 重构：同样的 SMEM 环、同样的 TMEM 累加器、同样的 TMA 与 tcgen05 路径，只因控制流从一组线程分散到三类角色，就让三段硬件得以并发。

### 4.2 四道屏障

#### 4.2.1 概念说明

三个并发角色共享两类稀缺资源：**SMEM 的 stage 缓冲**（TMA 写、MMA 读）与 **TMEM 累加器**（MMA 写、回写读）。并发使用共享资源的正确性完全靠屏障维护：前向路径报告「数据就绪」，反向路径归还「缓冲可复用」。

书中屏障按 `source2destination` 命名，四道屏障一张表（[chapter_gemm_advanced/index.md:L58-L67](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L58-L67)）：

| 屏障 | 类型 | 方向 | 含义 |
|------|------|------|------|
| `tma2mma` | `TMABar` | TMA → MMA | 「SMEM 数据已就绪」（full） |
| `mma2tma` | `TCGen05Bar` | MMA → TMA | 「SMEM 缓冲可复用」（empty） |
| `mma2ld` | `TCGen05Bar` | MMA → 回写 | 「TMEM 结果已就绪」（full） |
| `ld2mma` | `MBarrier` | 回写 → MMA | 「TMEM 已腾空」（empty） |

注意这是 u8-l2「full/empty 双屏障」理论在两个资源圈上的完整落地：SMEM 环用 `tma2mma`(full) + `mma2tma`(empty)，深度为 `PIPE_DEPTH`；TMEM 单缓冲用 `mma2ld`(full) + `ld2mma`(empty)，深度为 1。图中标注的 `smem_pipe.full` / `smem_pipe.empty` 在实现里就对应 `tma2mma` / `mma2tma`（[chapter_gemm_advanced/index.md:L39](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L39)）。

**为什么类型不同？** 屏障类型由「生产者如何报告完成」决定（[chapter_gemm_advanced/index.md:L69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L69)）：

- TMA load 的完成者是把数据搬进 SMEM 的 **TMA 引擎**，要用带字节计数的 `TMABar`；
- MMA 的完成者是 **Tensor Core 硬件**，`TCGen05Bar` 底层靠 `tcgen05.commit`，硬件在异步 MMA 真正完成后补到达；
- 回写线程读走 TMEM 是普通线程行为，用最朴素的 `MBarrier` 计数即可；
- TMA store 甚至不用 mbarrier——发起线程自己用 `cp_async.bulk.commit_group()` + `wait_group(0)` 追踪。

#### 4.2.2 核心流程

把一个输出 tile 的完整生命周期展开（`S = PIPE_DEPTH = 2`）：

```text
对每个输出 tile（tile_scheduler 认领）：
  MMA 消费者先等 ld2mma：上一个 tile 的回写已腾空 TMEM
  for k in 0..K_TILES-1:
    TMA:  mma2tma.wait(stage k%S, phase)   # 等 MMA 读完这个 stage
    TMA:  装载 A/B 的第 k 个 K 块到 stage k%S
    TMA:  tma2mma.arrive(stage k%S, 32768 字节)
    MMA:  tma2mma.wait(stage k%S, phase)   # 等数据落齐
    MMA:  Tx.gemm_async(accum = (k != 0))
    MMA:  mma2tma.arrive(stage k%S)        # 硬件在 MMA 完成后到达
  MMA:  mma2ld.arrive(0)                   # 全部 K 块算完，结果就绪
  回写:  mma2ld.wait → 读 TMEM → ld2mma.arrive(0) ×128 线程 → 写回 GMEM
```

**`mma2tma` 为什么「跨过一级 stage」**（[chapter_gemm_advanced/index.md:L46](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L46)）：环形缓冲的复用顺序使然。`PIPE_DEPTH=2` 时 Load k=0 填 stage 0、Load k=1 填 stage 1；MMA k=0 读完 stage 0 后，下一个需要这个槽位的是 **Load k=2**（k=1 用的是 stage 1），所以 `MMA k=0` 的完成信号释放给 `Load k=2`。

**字节账本**：`tma2mma.arrive` 登记的字节数是

\[(BLK\_M \times BLK\_K + BLK\_N \times BLK\_K) \times 2 = (128 \times 64 + 128 \times 64) \times 2 = 32768 \text{ 字节}\]

一次 arrive 覆盖同一 stage 上 A、B 两条 `copy_async`（它们指向同一个 mbarrier，见 4.2.3 代码），这继承自 u8-l1 的纪律：**登记字节数必须精确等于本相位全部关联 TMA 传输的送达字节**——过小提前放行读旧数据，过大永久挂死。

**`cta_mask`**：Step 7 的每个完成信号只需更新本 CTA 的屏障，所以 `arrive` 都带 `cta_mask=0`；Step 8 组成双 CTA cluster 后改用 `cta_mask=3`（二进制 `11`）同时更新两侧 CTA 的对应屏障（[chapter_gemm_advanced/index.md:L71](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L71)）。这是为下一讲埋的钩子。

#### 4.2.3 源码精读

**① 屏障的分配与初始化**（[chapter_gemm_advanced/index.md:L163-L180](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L163-L180)）：

```python
pool = T.SMEMPool()
tmem_addr = pool.alloc((1,), "uint32")
tma2mma = TMABar(pool, PIPE_DEPTH)
mma2tma = TCGen05Bar(pool, PIPE_DEPTH)
mma2ld  = TCGen05Bar(pool, 1)
ld2mma  = MBarrier(pool, 1)
pool.move_base_to(1024)
Asmem = pool.alloc((PIPE_DEPTH, BLK_M, BLK_K), a_type, layout=A_layout)
Bsmem = pool.alloc((PIPE_DEPTH, BLK_N, BLK_K), b_type, layout=B_layout)
Dsmem = pool.alloc((BLK_M, BLK_N), d_type, layout=D_layout)

# --- Barrier init ---
tma2mma.init(1)
mma2tma.init(1)
mma2ld.init(1)
ld2mma.init(128)   # all 128 Warpgroup 0 threads arrive
pool.commit()
```

这段代码浓缩了四个事实：构造参数 `PIPE_DEPTH` 表示 SMEM 环每级 stage 配一道独立屏障，`1` 表示 TMEM 圈只有单缓冲；三道屏障 `init(1)` 意味着每相位只等 **1 次到达**（单线程发起的操作只报告一次）；`ld2mma.init(128)` 则等 **128 次到达**——Warpgroup 0 全部 128 个回写线程各 arrive 一次；控制对象占 `move_base_to(1024)` 之前的低地址区，操作数缓冲摆在其后（与 u11-l2 的 Step 1 布局纪律一致）。

**② TMA producer 的 wait→load→arrive→advance 四拍循环**（[chapter_gemm_advanced/index.md:L210-L229](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L210-L229)）：

```python
@T.inline
def tma_load(k_offset):
    Tx.copy_async(Asmem[tma_ps.stage, :, :],
                  A[m_st:m_st+BLK_M, k_offset:k_offset+BLK_K],
                  dispatch="tma_auto", cta_group=1,
                  mbar=tma2mma.ptr_to([tma_ps.stage]))
    Tx.copy_async(Bsmem[tma_ps.stage, :, :],
                  B[n_st:n_st+BLK_N, k_offset:k_offset+BLK_K],
                  dispatch="tma_auto", cta_group=1,
                  mbar=tma2mma.ptr_to([tma_ps.stage]))

if T.filter(lane_id, T.ptx.elect_sync()):
    while tile_scheduler.valid():
        for k in range(K_TILES):
            mma2tma.wait(tma_ps.stage, tma_ps.phase)
            tma_load(k * BLK_K)
            tma2mma.arrive(tma_ps.stage,
                           (BLK_M * BLK_K + BLK_N * BLK_K) * F16_SIZE)
            tma_ps.advance()
        tile_scheduler.next_tile()
```

读法：先等 `mma2tma`（stage 空闲）→ 发两条 TMA load（A、B 都把完成挂在 `tma2mma.ptr_to([stage])` 这**一道** mbarrier 上）→ `tma2mma.arrive` 一次性登记两条传输共 32768 字节（线程到达 + expect_tx）→ `advance()` 推进到下一 stage。

**③ MMA consumer 的对称四拍**（[chapter_gemm_advanced/index.md:L236-L254](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L236-L254)）：

```python
if T.filter(lane_id, T.ptx.elect_sync()):
    while tile_scheduler.valid():
        # Wait for TMEM to be free from previous tile's writeback
        ld2mma.wait(ld_ps.stage, ld_ps.phase)
        ld_ps.advance()

        for k in range(K_TILES):
            tma2mma.wait(mma_ps.stage, mma_ps.phase)
            Tx.gemm_async(
                tmem[:, :BLK_N],
                Asmem[mma_ps.stage, :, :],
                Bsmem[mma_ps.stage, :, :],
                accum=(k != 0), dispatch="tcgen05", cta_group=1)
            mma2tma.arrive(mma_ps.stage, cta_group=1, cta_mask=0)
            mma_ps.advance()

        # Signal results ready for writeback
        mma2ld.arrive(0, cta_group=1, cta_mask=0)
        tile_scheduler.next_tile()
```

读法：每个 tile 先等 `ld2mma`（TMEM 已被上一个 tile 的回写腾空）→ K 循环里等 `tma2mma`（数据齐）→ 发 `Tx.gemm_async`（`accum=(k != 0)`：首块覆盖垃圾初值，其后累加，纪律承自 u11-l3）→ `mma2tma.arrive`。**关键**：这个 arrive 紧跟在 `gemm_async` 发起之后，却不会提前放行——`TCGen05Bar` 底层的 `tcgen05.commit` 语义保证到达发生在硬件真正完成 MMA（即读完 SMEM）之后。若这里是普通 MBarrier，TMA 会在 MMA 还在读 stage 时就覆写它。K 循环结束后 `mma2ld.arrive(0)` 通知回写组结果就绪。

**④ wait/arrive 配对总表**（依据上两段源码整理）：

| 屏障 | 初始化 | 谁 wait | 谁 arrive | 保护什么 |
|------|--------|---------|-----------|----------|
| `tma2mma` | init(1)，depth 2 | MMA consumer（L243） | producer arrive+登记字节（L226–227），TMA 引擎 complete-tx 扣减 | SMEM stage 的数据就绪 |
| `mma2tma` | init(1)，depth 2 | TMA producer（L224） | `tcgen05.commit` 在 MMA 完成后（L249） | SMEM stage 可被覆写 |
| `mma2ld` | init(1)，depth 1 | 回写组（L265） | 同上（L253） | TMEM 结果就绪 |
| `ld2mma` | init(128)，depth 1 | MMA consumer（L239） | 回写组 128 线程各一次（L277） | TMEM 可被下一 tile 覆写 |

#### 4.2.4 代码实践

**实践目标**：完成书中 "Trace the barriers" 建议（[chapter_gemm_advanced/index.md:L311](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L311)）——跟踪一个 K tile 走完四道屏障。

**操作步骤**：

1. 取 \(k=1\)（第二个 K tile，落在 stage 1），在纸上按时间顺序写出它触发的每一次 wait 与 arrive，标注发起角色的名字。
2. 每写一步，回答书中给定的四问：谁等待？谁到达？哪些数据变得可读？哪个缓冲变得可复用？
3. 对照 4.2.3 的总表核对。

**需要观察的现象**：\(k=1\) 的 `mma2tma.wait` 与 \(k=0\) 的 `mma2tma.arrive` 是同一道屏障（stage 0 与 stage 1 是**不同**的屏障），两个 stage 的等待互不阻塞——这正是双缓冲并发的根源。

**预期结果**：得到一条形如「producer: mma2tma.wait(1,·) → producer: 2×copy_async → producer: tma2mma.arrive(1, 32768B) → consumer: tma2mma.wait(1,·) → consumer: gemm_async → (硬件) mma2tma.arrive(1)」的轨迹；尾块之后另有 `mma2ld.arrive(0)` 与回写侧的 `ld2mma.arrive(0)`。纯纸面推演，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：`tma2mma.arrive` 登记的字节数是怎么算出来的？为什么 A、B 两条传输只登记一次？

**答案**：\((BLK\_M \cdot BLK\_K + BLK\_N \cdot BLK\_K) \times F16\_SIZE = (8192+8192)\times 2 = 32768\) 字节。因为两条 `copy_async` 通过 `mbar=tma2mma.ptr_to([stage])` 挂在同一道 mbarrier 上，字节计数是按屏障聚合的，一次 `arrive.expect_tx` 登记本相位全部在途字节即可。

**练习 2**：如果把 `mma2tma` 换成普通 `MBarrier`（线程发起后立即到达），会发生什么？

**答案**：到达会发生在 `gemm_async` **发射**之时而非 MMA 完成之时，TMA producer 随即获准覆写该 stage，而 Tensor Core 可能还在从里面读 A/B——读到的数据新旧混杂，产生错误结果。`TCGen05Bar` 的存在正是为了让「完成」由硬件在 MMA 真正结束后报告。

**练习 3**：`ld2mma.init(128)` 为什么要 128，而其他三道屏障都是 `init(1)`？

**答案**：`ld2mma` 的到达方是 Warpgroup 0 的全部 128 个回写线程（每个线程读完自己的 TMEM 窗口后各 arrive 一次，L277），屏障必须集齐 128 次到达才算「TMEM 整体腾空」；其余屏障的到达方都是单个发起线程（或挂在其上的硬件完成信号），一次到达即完成一相。

### 4.3 PipelineState：stage 与 phase 的捆绑

#### 4.3.1 概念说明

四道屏障只回答「某个缓冲此刻可不可用」；每个角色还必须知道自己**当前该用第几个 stage、该等第几相**。这两件事由 `PipelineState` 捆绑维护（[chapter_gemm_advanced/index.md:L73-L82](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L73-L82)）：

```python
tma_ps = PipelineState(PIPE_DEPTH, phase=1)   # Producer starts ready (phase=1)
# tma_ps.stage = current stage index
# tma_ps.phase = current phase (0 or 1)
tma_ps.advance()                          # Advance to next stage
```

为什么不手写两个整型变量？因为手工维护极易产生 off-by-one，而相位差一就会死锁或静默读错数据（u8-l2 已推演过两类故障）。`PipelineState` 把 `stage`（模 \(S\) 循环）与 `phase`（扫完一圈才翻转）绑成一个对象、一次 `advance()` 同步推进，把这类错误压到最少。

#### 4.3.2 核心流程

深度 \(S\) 的环形流水上，第 \(k\) 个 K tile 满足：

\[\text{stage}(k) = k \bmod S, \qquad \text{phase}(k) = \lfloor k / S \rfloor \bmod 2\]

初始相位决定角色的**第一次 wait 是通过还是阻塞**，两端必须相反（[chapter_gemm_advanced/index.md:L84-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L84-L89)）：

- `phase=1`（资源起始可用的一端）：首次 `wait(phase=1)` 遇到的是 fresh 屏障的相位 0，\(0 \neq 1\)，立即通过；
- `phase=0`（资源起始不可用的一端）：首次 `wait(phase=0)` 遇到相位 0，阻塞，直到对端完成第一次 arrive。

内核里四个 `PipelineState` 的完整初始状态：

| PipelineState | 所属角色 | depth | 初始 phase | 首次 wait 在哪 | 首次行为 |
|---|---|---|---|---|---|
| `tma_ps`（L208） | TMA producer | 2 | **1** | `mma2tma`（L224） | 立即通过：SMEM 起始为空，可直接填充 |
| `mma_ps`（L233） | MMA consumer | 2 | **0** | `tma2mma`（L243） | 阻塞：等首批数据落齐 |
| `ld_ps`（L234） | MMA consumer | 1 | **1** | `ld2mma`（L239） | 立即通过：TMEM 起始空闲 |
| `wb_ps`（L260） | 回写组 | 1 | **0** | `mma2ld`（L265） | 阻塞：等首批结果 |

规律：**谁等的那件东西一开始就到手，谁就给 phase=1；一开始没有，就给 phase=0**。两个资源圈（SMEM 环、TMEM）各自独立套用这一规律。

**死锁推演（章末练习 1，[chapter_gemm_advanced/index.md:L907](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L907)）**——把 TMA 与 MMA 两个 PipelineState 的初始 phase 都设为 0（即把 `tma_ps` 从 1 改成 0）：

```text
TMA producer (WG1 warp 3)                MMA consumer (WG1 warp 0)
─────────────────────────                ─────────────────────────
第 1 拍: mma2tma.wait(0, phase=0)        ld2mma.wait(0, phase=1) → 通过
        ↑ fresh 屏障当前 parity=0                ↓
        ↑ wait(0) = 等屏障离开相位 0        第 2 拍: tma2mma.wait(0, phase=0)
        ↑ 需要 MMA 侧 1 次 arrive                  ↑ fresh 屏障 parity=0，阻塞
        ↑ 而 MMA 永远不来……                        ↑ 需要 32768 字节送达
   【阻塞】◄────── 循环等待 ──────►【阻塞】        ↑ 而 TMA 永远不发 load……
```

死锁链：TMA 等 MMA 归还 stage 0（`mma2tma`），MMA 等 TMA 装满 stage 0（`tma2mma`），双方都在等对方先 arrive，内核在**第一个 K tile** 就永久挂起。反之若把消费者一侧改错（`mma_ps` 设为 1），故障形态不同：`tma2mma.wait(0,1)` 在 fresh 屏障上立即通过，MMA 在数据到达前读 SMEM，**静默产出错误结果**而非死锁——对应书中「may deadlock or continue before the data is ready」两种结局（[L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L89)）。

另一个值得注意的设计差异：Step 6 中屏障相位依赖「每 tile 使用次数为偶数」才能在 tile 边界归零（断言 `K_TILES % (2*PIPE_DEPTH) == 0`）；Step 7 的四个 `PipelineState` 都在 `while tile_scheduler.valid()` **之外**创建（L208、L233–234、L260），跨 tile 连续推进，相位历史保存在状态对象里，不再需要 tile 边界归零的约束——两角色靠「每个 tile 各推进相同次数」保持对齐。

#### 4.3.3 源码精读

**① producer 侧状态**（[chapter_gemm_advanced/index.md:L206-L208](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L206-L208)）：

```python
if wg_id == 1:
    if warp_id == 3:
        # === TMA Producer ===
        tma_ps = PipelineState(PIPE_DEPTH, phase=1)
```

**② consumer 侧双状态**（[chapter_gemm_advanced/index.md:L231-L234](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L231-L234)）：

```python
elif warp_id == 0:
    # === MMA Consumer ===
    mma_ps = PipelineState(PIPE_DEPTH, phase=0)
    ld_ps = PipelineState(1, phase=1)
```

MMA consumer 同时持有两个状态：`mma_ps` 走 SMEM 环（每 K 块推进一次），`ld_ps` 走 TMEM 圈（每 tile 推进一次，L239–240）。

**③ 回写侧状态**（[chapter_gemm_advanced/index.md:L259-L266](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L259-L266)）：

```python
elif wg_id == 0:
    wb_ps = PipelineState(1, phase=0)
    reg_f16 = T.alloc_local((BLK_N,), d_type)

    while tile_scheduler.valid():
        # Wait for MMA results
        mma2ld.wait(wb_ps.stage, wb_ps.phase)
        wb_ps.advance()
        T.ptx.tcgen05.fence.after_thread_sync()
```

回写组每个 tile 在 `mma2ld` 上等一相，等的是「本 tile 的全部 K 块 MMA 已完成」。注意 `advance()` 紧跟 wait 之后、在任何后续操作之前——先推进再干活，避免下轮忘记。

**④ 推进次数的对应关系**：每个输出 tile 内，`tma_ps` 与 `mma_ps` 各推进 `K_TILES` 次（两个 for 循环迭代数相同），`ld_ps` 与 `wb_ps` 各推进 1 次。角色间不共享这些对象，一致性完全由「相同迭代次数」这一结构性质保证。

#### 4.3.4 代码实践

**实践目标**：用一张相位推演表验证 \( \text{stage}(k) = k \bmod 2,\ \text{phase}(k) = \lfloor k/2 \rfloor \bmod 2 \)，并亲手复现 off-by-one 的判断力。

**操作步骤**：

1. 纸面上为 \(k = 0,1,2,3,4,5\) 列出 `tma_ps` 与 `mma_ps` 在每次循环体执行**开头**的 `(stage, phase)` 值。
2. 对每个 \(k\)，标出 producer 在 `mma2tma` 上等待的相位与 consumer 在 `tma2mma` 上等待的相位。
3. 检查：\(k=2\) 时 producer 等的 stage 0 屏障相位，是否恰好是 \(k=0\) 那一轮 consumer 完成后翻转到的新相位。

**需要观察的现象**：相位翻转只发生在 \(k\) 为偶数处（扫完一圈），stage 每步都变；\(k=2\) 与 \(k=0\) 用同一道 stage 0 屏障但等不同相位——这就是同一屏障先后两次使用被区分开的方式。

**预期结果**：序列为 `(0,1)→(1,1)→(0,0)→(1,0)→(0,1)→(1,1)…`（producer 视角，首次 phase=1），consumer 视角整体相同但首次 phase=0。纯推演，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：把 `mma_ps` 的初始 phase 从 0 改成 1，内核会发生什么？和把 `tma_ps` 改成 0 有何不同？

**答案**：`mma_ps=1` 时 consumer 的首次 `tma2mma.wait(0, 1)` 在 fresh 屏障（parity 0）上立即通过，MMA 在 TMA 送达任何字节前就读 SMEM——读到未初始化数据，**静默产出错误结果**；`tma_ps=0` 则是双向循环等待，**死锁挂死**。同为初始相位错误，故障形态由「错在哪一端」决定。

**练习 2**：Step 7 为什么不再需要 Step 6 的 `K_TILES % (2*PIPE_DEPTH) == 0` 断言？

**答案**：Step 6 在 tile 边界把屏障相位隐式归零，因此要求每 tile 使用次数为偶数；Step 7 的 `PipelineState` 在 while 循环外创建、跨 tile 连续 `advance()`，相位历史由对象携带，tile 边界不再是相位复位点，偶数约束随之消失。

**练习 3**：`PipelineState` 是共享内存对象还是每角色的私有状态？这带来什么要求？

**答案**：私有状态——producer 与 consumer 各有自己的 `tma_ps`/`mma_ps`，它们不是同一份数据的读写副本，而是各自视角下的游标。这要求两个角色在每个 tile 内推进**相同次数**（都跑满 `K_TILES` 次），否则游标错位、等待的相位对不上，表现为死锁或读到旧数据。

### 4.4 warpgroup_sync 与回写路径

#### 4.4.1 概念说明

回写组的 128 个线程要把 TMEM 中的 fp32 结果转成 fp16 写进 `Dsmem`，再由一个线程发起 TMA store。这里有一个 Step 1～6 没有暴露过的同步难题：**回写分支内部不能使用 `cta_sync()`**。另一个 warpgroup 正跑在 producer/consumer 的无限循环里，永远不会到达回写分支中的同步点；全 CTA 等待等不到另一半，直接死锁（[chapter_gemm_advanced/index.md:L93](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L93)）。

warp 特化之后，「同步谁」必须精确到子集。PTX 提供的机制是 **named barrier（命名屏障）**：`warpgroup_sync(10)` 降低为（[chapter_gemm_advanced/index.md:L95-L101](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L95-L101)）：

```text
bar.sync 10, 128
```

`10` 是屏障 ID，`128` 是要求到达的线程数。指令阻塞执行线程，直到有 128 个线程到达了同一 ID 的屏障。它与 mbarrier 是**两套不同机制**：`bar.sync` 同步的是「线程到达」这一行为本身（软件同步），mbarrier 追踪的是异步硬件操作的完成（TMA 字节、MMA 完成）。

两条使用纪律（[chapter_gemm_advanced/index.md:L103-L105](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L103-L105)）：

- 指令本身**不识别 warpgroup**——它同步 Warpgroup 0 仅仅因为只有这 128 个线程执行了这段代码且都用 ID 10；
- 每个 CTA 有 16 个屏障槽位（ID 0–15），参与同一同步的线程必须用同一 ID，相互独立的同步必须用不同 ID。Step 7 只有一个回写组，固定用 10；Step 9 有两个回写组，改用 `warpgroup_sync(wg_id + 10)` 分配 10 和 11，避免两组到达被混计。

（最近的提交 0fdad07 "Explain warpgroup named barrier ID" 正是补充了这一段 ID 语义的解释。）

#### 4.4.2 核心流程

`BLK_N=128` 时回写组一次读完整个 TMEM tile 并发起一次 TMA store，六步序列（[chapter_gemm_advanced/index.md:L109-L116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L109-L116)）：

```text
① mma2ld.wait(wb_ps) → tcgen05.fence.after_thread_sync()
② Tx.wg.copy_async(reg_wg, tmem) → tcgen05.wait.ld()   # 每线程 128 个 fp32
③ 全部 128 线程 ld2mma.arrive(0)                        # TMEM 腾空
④ Tx.cast: fp32 → fp16（寄存器内）
⑤ 写 Dsmem → fence.proxy_async → warpgroup_sync(10)     # 整 tile 写齐
⑥ warp0.lane0 发 TMA store → commit_group → wait_group(0) → warpgroup_sync(10)
```

三种等待各司其职，不可互相替代（[chapter_gemm_advanced/index.md:L118](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L118)）：

- `mma2ld` 的 mbarrier wait：确认 **MMA 已完成**（别的线程发起的硬件操作）；
- `tcgen05.fence.after_thread_sync()`：在跨线程完成通知之后建立 `tcgen05` 操作的**顺序**，保证后续 `tcgen05.ld` 排在通知之后；
- `tcgen05.wait.ld()`：确认异步 TMEM 读取**已填充目的寄存器**。

两道 `warpgroup_sync(10)` 各守一道交接：第一道保证 `Dsmem` 整 tile 写齐后才发起 TMA store；第二道保证 warp 0 的 `wait_group(0)` 排空 store 后，其余线程才进入下一 tile 去覆写 `Dsmem`。

#### 4.4.3 源码精读

**① 回写主体**（[chapter_gemm_advanced/index.md:L263-L294](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L263-L294)）：

```python
while tile_scheduler.valid():
    # Wait for MMA results
    mma2ld.wait(wb_ps.stage, wb_ps.phase)
    wb_ps.advance()
    T.ptx.tcgen05.fence.after_thread_sync()

    # Read TMEM -> registers (warpgroup scope)
    reg = T.alloc_local((BLK_N,), acc_type)
    reg_wg = reg.view(128, BLK_N,
        layout=TileLayout(S[(128, BLK_N) : (1@tid_in_wg, 1)]))
    Tx.wg.copy_async(reg_wg[:], tmem[:, :BLK_N])
    T.ptx.tcgen05.wait.ld()

    # Signal TMEM free (all 128 threads arrive)
    ld2mma.arrive(0)

    # Cast fp32 -> fp16
    Tx.cast(reg_f16[:], reg[:])

    # Write to Dsmem + TMA store
    Tx.copy(Dsmem[warp_id * 32 + lane_id, :], reg_f16[:])
    T.ptx.fence.proxy_async("shared::cta")
    T.cuda.warpgroup_sync(10)
    if warp_id == 0:
        if lane_id == 0:
            Tx.copy_async(D[m_st:m_st+BLK_M, n_st:n_st+BLK_N],
                          Dsmem[:, :], dispatch="tma_auto")
            T.ptx.cp_async.bulk.commit_group()
            T.ptx.cp_async.bulk.wait_group(0)
    T.cuda.warpgroup_sync(10)

    tile_scheduler.next_tile()
```

逐段读法：

- `reg.view(128, BLK_N, layout=TileLayout(S[(128, BLK_N):(1@tid_in_wg, 1)]))` 把每线程 128 个本地寄存器重解释为 warpgroup 集体视图，`Tx.wg.copy_async` 是 warpgroup 集体操作（u7-l4：tcgen05.ld 由整 warp 执行、按 lane 分发），之后必须 `wait.ld` 才能使用 `reg`；
- `ld2mma.arrive(0)` 由**全部 128 个线程**执行（无 remote rank，`arrive()` 默认指向本 CTA 屏障），对应 `init(128)`；此时数据已经安全落在寄存器里，TMEM 可以交给下一个 tile 的 MMA——这一步放在 cast 与写回**之前**，正是为了让下一 tile 的 MMA 尽早开工，回写继续在寄存器/SMEM 里慢悠悠进行；
- `Tx.copy(Dsmem[warp_id * 32 + lane_id, :], reg_f16[:])`：每个线程写 `Dsmem` 的一整行（行号 \(= warp\_id \times 32 + lane\_id\)，128 线程恰好覆盖 128 行，每行 128 个 fp16），普通 SMEM 写；
- `fence.proxy_async("shared::cta")`：通用 proxy 的写入要对 async proxy（TMA）可见，才能被 store 搬运（u12-l1 同款纪律）；
- `warpgroup_sync(10)`（第一道）：集齐 128 线程，`Dsmem` 完整；
- `warp_id == 0` 且 `lane_id == 0` 的单线程发起 `Tx.copy_async`（TMA store，`Dsmem → GMEM`）并 `commit_group` + `wait_group(0)` 排空；
- `warpgroup_sync(10)`（第二道，在 if 之外，全体到达）：确保 store 排空后大家才一起进入 `next_tile`。

**② 收尾清理**（[chapter_gemm_advanced/index.md:L296-L300](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L296-L300)）：

```python
# --- Cleanup ---
T.cuda.cta_sync()
if warp_id == 0:
    T.ptx.tcgen05.relinquish_alloc_permit(cta_group=1)
    T.ptx.tcgen05.dealloc(tmem_addr[0], n_cols=512, cta_group=1)
```

所有角色退出各自循环后，在此处汇合（这里可以用 `cta_sync`，因为两个 warpgroup 都会到达），再释放 TMEM（先 relinquish 分配许可、再 dealloc，纪律承自 u7-l3）。

#### 4.4.4 代码实践

**实践目标**：把回写路径上的每次同步与其保护的对象一一对应，练习「删掉某道同步会坏什么」的诊断式阅读。

**操作步骤**：

1. 在 4.4.3 的代码上做五种标注：mbarrier wait、`tcgen05.fence`、`tcgen05.wait.ld`、`fence.proxy_async`、`warpgroup_sync(10)` ×2。
2. 对每道同步写一句话：「它在谁与谁之间建立什么顺序」。
3. 思考实验（不真改代码）：分别删掉第一道与第二道 `warpgroup_sync(10)`，推演各自的故障。

**需要观察的现象**：`ld2mma.arrive(0)` 的位置在 TMEM 读回之后、cast/写回之前——「腾空信号」尽可能早发，让 MMA consumer 的下一个 tile 不必等回写全部完成。

**预期结果**：删第一道 sync：TMA store 可能在 128 行没写齐时就搬运，GMEM 里出现旧 tile 的残行（错误结果）；删第二道 sync：快线程进入下一 tile 覆写 `Dsmem` 时，store 可能尚未排空，同样静默出错。两道都是正确性同步，不是性能优化。纯推演即可，若在 Blackwell GPU 上实测标「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`bar.sync 10, 128` 里的 10 和 128 分别是什么？这条指令怎么「知道」要同步 Warpgroup 0？

**答案**：10 是 named barrier 的 ID（每 CTA 有 0–15 共 16 个槽位），128 是要求到达的线程数。指令本身不识别 warpgroup——之所以等价于「同步 Warpgroup 0」，是因为只有 WG0 的 128 个线程执行了这段代码，且它们全部使用 ID 10；若 WG1 的线程也用 ID 10 到达，计数就会被混入。

**练习 2**：回写分支里为什么不能把 `warpgroup_sync(10)` 换成 `cta_sync()`？

**答案**：`cta_sync()` 等待整个 CTA 的 256 个线程，而 Warpgroup 1 正在 producer/consumer 角色的循环里，永远不会到达回写分支内的同步点——一半线程等另一半，必然死锁。warp 特化之后，分支内的同步范围必须与分支的执行者集合一致。

**练习 3**：Step 9 为什么要写 `warpgroup_sync(wg_id + 10)` 而不是两个组都用 10？

**答案**：Step 9 有两个回写 warpgroup，若都用 ID 10，两组共 256 个线程的到达会被计入同一计数器，第 128 次到达时另一组可能才到一半，同步语义被破坏。`wg_id + 10` 分配 ID 10 与 11，让两组各自集齐自己的 128 次。

## 5. 综合实践

本综合实践完成规格指定的两项任务：**章末练习 1 的死锁场景图** 与 **PIPE_DEPTH=2/3/4 的 SMEM 成本对比表**。

### 任务一：初始相位双零的死锁场景图

**实践目标**：把 4.3.2 的文字推演固化为一张可复查的图。

**操作步骤**：

1. 纸笔或绘图工具画出两个角色框「TMA producer (WG1 warp3)」「MMA consumer (WG1 warp0)」。
2. 从 producer 画一条阻塞箭头指向 `mma2tma` 屏障，标注 `wait(stage=0, phase=0)｜阻塞：等 MMA 完成 stage 0`；从 consumer 画一条阻塞箭头指向 `tma2mma` 屏障，标注 `wait(stage=0, phase=0)｜阻塞：等 32768 字节送达`。
3. 把两个屏障用「永远无人 arrive」连成闭合环，环上标注重复出现的故障判据：fresh 屏障 parity=0，`wait(…, 0)` 恰好卡在等它离开相位 0。
4. 有条件的话改编 [gen_warp_specialization_timeline.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_warp_specialization_timeline.py#L74-L86) 的 `ax2.annotate` 箭头代码，把两条虚线箭头改成首尾相接的阻塞环，输出死锁版时间线图。

**需要观察的现象**：死锁发生在第一个 K tile、双方都还没执行过任何 arrive；图上不存在任何一条能推进的边。

**预期结果**：一张双向阻塞环图，配一句话结论——生产者等 `mma2tma`（empty 屏障）、消费者等 `tma2mma`（full 屏障），循环等待且无外力打破。若要在真实 Blackwell GPU 上把 `tma_ps` 改为 `phase=0` 复现挂死：**待本地验证**。

### 任务二：PIPE_DEPTH=2/3/4 的 SMEM 成本对比表

**实践目标**：核算流水线深度的资源代价，对照 B200 每 SM 228 KB 上限。

依据（[chapter_gemm_advanced/index.md:L317-L323](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L317-L323)）：每级 stage 一对 A/B 操作数缓冲，成本

\[(BLK\_M \times BLK\_K + BLK\_N \times BLK\_K) \times 2\ \text{bytes} = (128 \times 64 + 128 \times 64) \times 2 = 32\ \text{KB}\]

`Dsmem` 回写缓冲另需 \(128 \times 128 \times 2 = 32\) KB，总成本约为

\[\text{SMEM} \approx S \times 32\ \text{KB} + 32\ \text{KB}\quad (\text{外加约 1 KB 屏障等元数据})\]

**操作步骤**：

1. 用上式计算 \(S = 2, 3, 4\) 的总成本与占 228 KB 的比例，填入下表。
2. 补算书中提到的 \(S=6\)，判断是否可行。
3. 回答：如果只想加深流水而不超上限，还有哪些杠杆？

**预期结果**（对照表）：

| PIPE_DEPTH | 操作数 stage 成本 | Dsmem | 总计 | 占 B200 228 KB |
|:---:|:---:|:---:|:---:|:---:|
| 2（书中取值） | 2×32 = 64 KB | 32 KB | **96 KB** | ≈ 42% |
| 3 | 3×32 = 96 KB | 32 KB | **128 KB** | ≈ 56% |
| 4 | 4×32 = 128 KB | 32 KB | **160 KB** | ≈ 70% |
| （6，书中给的反例） | 6×32 = 192 KB | 32 KB | 224 KB | ≈ 98% |

结论要点：深度 6 几乎榨干 228 KB（书中原话 "nearly exhausts the available capacity"），更深的流水不一定更快，还可能让当前 tile 形状根本无法 launch；若要加深，可考虑缩小 `BLK_K` 或 `Dsmem` 改分块多次 store 来腾出空间——代价是换乘更多次，需要重新核算收益。

## 6. 本讲小结

- Step 7 是一次**纯 scope 重构**：layout（SMEM 环 + TMEM 累加器）与 dispatch（TMA + tcgen05）原样继承 Step 5/6，只把串行控制权拆给三个并发角色——WG1 warp 3 做 TMA producer、WG1 warp 0 做 MMA consumer、WG0 全体 128 线程做回写。
- 四道屏障按 `source2destination` 命名，构成两个 full/empty 资源圈：SMEM 环用 `tma2mma`+`mma2tma`（深度 2），TMEM 单缓冲用 `mma2ld`+`ld2mma`；屏障类型由完成者决定——TMA 引擎用带字节计数的 `TMABar`，Tensor Core 用靠 `tcgen05.commit` 的 `TCGen05Bar`，普通线程用 `MBarrier`（`ld2mma` 等 128 次到达）。
- `tma2mma.arrive` 一次登记 A、B 两条传输共 32768 字节；`mma2tma` 的完成信号因环形复用顺序「跨过一级 stage」——MMA k=0 读完 stage 0，释放给的是 Load k=2。
- `PipelineState` 把 stage 与 phase 捆绑推进（\(\text{stage}=k \bmod S\)，\(\text{phase}=\lfloor k/S \rfloor \bmod 2\)），初始相位规则是「资源起始可用的一端给 1，起始不可用的一端给 0」；producer 侧设错成 0 会在第一个 K tile 形成双向循环等待而死锁，consumer 侧设错成 1 则提前放行、静默读旧数据。
- 回写分支内不能用 `cta_sync()`（另一半 warpgroup 永远到不了），改用 named barrier `warpgroup_sync(10)` → `bar.sync 10, 128`；两道 sync 分别保证「Dsmem 整 tile 写齐后才 store」与「store 排空后才覆写 Dsmem」。
- SMEM 成本约为 \(S \times 32 + 32\) KB：深度 2/3/4 分别为 96/128/160 KB，深度 6 的 224 KB 已逼近 B200 每 SM 228 KB 上限——更深的流水不一定更快。

## 7. 下一步学习建议

Step 7 把三个角色的协作封闭在**单个 CTA 内部**；下一讲 [u13-l2]（Step 8：双 CTA cluster 协作 MMA）把协作范围扩到 cluster：`cta_group::2` 让一对 CTA 发起协作 MMA 产出 256×256 的大 tile，A/B 切片分驻两侧 SMEM 并经 DSMEM 互访，本讲的 `cta_mask=0` 也将升级为 `cta_mask=3`（同时通知两侧 CTA 的屏障）。建议在读下一讲前：

1. 重做 4.2.4 的「跟踪一个 K tile」，直到不看书能写出四道屏障的 wait/arrive 顺序。
2. 回顾 u2-l2 的 DSMEM 与 u7-l2 的 `cta_group::2` TMEM 映射（Layout A/B），那是 Step 8 的直接前置。
3. 有兴趣的读者可预先浏览 [chapter_gemm_advanced/index.md:L325 起](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L325) 的 Step 8 正文，重点找 `cta_mask=3` 与 `arrive.expect_tx` 字节数乘 `CTA_GROUP` 的位置（章末练习 2，[L908](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L908)）。
