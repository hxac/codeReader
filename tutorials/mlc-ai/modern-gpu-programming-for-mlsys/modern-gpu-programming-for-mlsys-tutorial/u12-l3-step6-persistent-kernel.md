# Step 6：持久内核与 tile scheduler

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 **persistent（持久）内核**减少 launch 与重复初始化开销的机制：为什么 Step 5 的 1024 个「一 CTA 一 tile」要换成固定 148 个「常驻 CTA 循环认领」，哪些初始化从「每 tile 一次」变成「每 CTA 一次」。
2. 掌握 tile scheduler（`ClusterPersistentScheduler2D`）的使用方式：`init(bx)` → `valid()` → `m_idx` / `n_idx` → `next_tile()` 这套外层循环接口，并会计算每个 persistent CTA 平均要处理多少个输出 tile（章末练习 3）。
3. 说明 `l2_group_size=8` 决定的 tile 遍历顺序**如何改善 L2 命中**：共享同一个 B tile 的任务在调度顺序中彼此靠近、同一组 A tile 在短时间窗内反复出现。
4. 推导 Step 6 特有的**相位复位约束**：屏障跨 tile 复用时，每个 tile 内的使用次数必须是偶数，才能把局部相位变量安全地归零——这正是 wrapper 里 `assert K_TILES % (2 * PIPE_DEPTH) == 0` 的由来。
5. 用三要素框架给 Step 6 定位：scope 变（固定 CTA 池 + 外层 tile 循环），layout 与 dispatch 都不变。

## 2. 前置知识

本讲建立在四条已有线索之上，先各用一句话回顾：

- **Step 5 双缓冲（u12-l2）**：A/B 缓冲带 `PIPE_DEPTH` 前导维构成 SMEM 环，每个 stage 一道 `tma_bar`、全内核共用一道 `mma_bar`，主循环按 `stage = k % PIPE_DEPTH` 消费并回填。但每个 CTA 只算一个输出 tile，算完就退出。
- **相位复用理论（u8-l2）**：mbarrier 每完成一相就原子翻入下一相；软件用奇偶（parity）区分同一屏障的先后使用，判据是「`try_wait(P)` 在屏障离开相位 P 后返回」。**屏障使用偶数次后回到初始奇偶**——本讲的相位归零就靠这一条。
- **空间分块的重复读取（u11-l4）**：输出按 128×128 分块后，同 `bx` 的 32 个 CTA 各自重复读同一 A 行块、同 `by` 的 32 个 CTA 各自重复读同一 B 行块；内核不显式共享这些 tile，只能靠 L2 消化。Step 6 第一次**主动帮 L2**。
- **静态调度的局限（u8-l3）**：persistent 内核把 tile 分配交给一个开工前就冻结的公式；worker 起步晚或 tile 代价不均会产生尾部空闲——这正是后文 CLC 要解决的问题，本讲先看清它的「静态」一面。

两个本讲新术语，先用一句话建立直觉：

- **persistent kernel（持久内核）**：launch 一批数量固定、寿命很长的 CTA，每个 CTA 在循环里连续处理多个输出 tile，而不是「一个 CTA 一个 tile、算完即退」。
- **tile scheduler（tile 调度器）**：内核里的一个对象，负责回答每个 CTA「下一个该算哪个 tile」。它把 tile 分配从「grid 形状隐式决定」变成「内核内显式可编程」，因此可以顺便挑选对 L2 友好的遍历顺序。

一个贯穿本讲的比喻：**tile 是任务单，CTA 是工人**。Step 5 之前是「1024 个工人每人领一张任务单，领单前各自办一套入职手续（初始化），干完就走」；Step 6 改成「固定雇 148 个工人，办一次入职手续，然后反复到调度台领单，直到任务池清空」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [chapter_gemm_async/index.md:457-477](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L457-L477) | Step 6 小节开头：从「每 CTA 一个 tile」到持久内核的动机、三要素变化声明、`SM_COUNT=148` 的一维 grid 说明、L2 友好遍历顺序的文字描述 |
| [chapter_gemm_async/index.md:479-503](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L479-L503) | 调度器构造代码片段；跨 tile 复用屏障时的相位奇偶约束（每 tile 使用 64/32 次均为偶数）与断言说明 |
| [chapter_gemm_async/index.md:505-535](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L505-L535) | 完整内核 `hgemm_v6` 的 imports（唯一新依赖是调度器）、`SM_COUNT=148`、参数合法性断言 |
| [chapter_gemm_async/index.md:544-592](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L544-L592) | 一维 grid、与 Step 5 相同的一次性 SMEM/屏障/TMEM 初始化、`ClusterPersistentScheduler2D` 的构造与 `init(bx)` |
| [chapter_gemm_async/index.md:618-668](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L618-L668) | 本讲核心：外层 `while tile_scheduler.valid()` 循环——从调度器取 `m_st`/`n_st`、每 tile 相位归零、内层 K 循环与回写（与 Step 5 相同）、`cta_sync` 后 `next_tile()` |
| [chapter_gemm_async/index.md:679-683](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L679-L683) | 章末练习，练习 3 是本讲综合实践的原文 |
| [chapter_gemm_basics/index.md:526-528](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L526-L528) | 「同 `bx` 的 CTA 读同一 A tile、同 `by` 的读同一 B tile、内核不显式共享」——L2 局部性模块的动机出处 |
| [chapter_gemm_advanced/index.md:29-37](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L29-L37) | Step 7 声明：多级 SMEM 流水线与持久 `ClusterPersistentScheduler2D` 原样从 Step 5/6 延续，只改 warp 角色划分 |
| [chapter_gemm_advanced/index.md:490-492](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L490-L492) | Step 8（双 CTA cluster）里同一调度器的参数变化：`num_clusters=SM_COUNT // CTA_GROUP` |
| [chapter_flash_attention/index.md:841-846](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L841-L846) | B200 的可用 L2 容量口径（`L2_SIZE=50 MiB`）与 `max_ctas=148` 的出处，供本讲 L2 容量估算使用 |
| [chapter_clc/index.md:20-35](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L20-L35) | 静态 persistent 调度的局限：分配开工前冻结、无法响应实际完成时间——本讲调度器的「上限说明书」 |

说明：本仓库是教材仓库，「源码」即书的 Markdown 章节及其内嵌 TIRx 内核代码。`ClusterPersistentScheduler2D` 的实现本体在 Apache TVM 包（`tvm.backend.cuda.lang.tile_scheduler`）中，不在本仓库，本讲会明确区分「书里说了什么」与「需要到 TVM 源码里核对什么」。

## 4. 核心概念与源码讲解

### 4.1 persistent 调度：从「一 CTA 一 tile」到「常驻 CTA 循环认领」

#### 4.1.1 概念说明

先看 Step 5 的 launch 形态有多大浪费。Step 5 的 grid 是 `[M // BLK_M, N // BLK_N]`：每个 128×128 输出 tile 配一个 CTA。对 \(4096\times4096\) 的输出，这意味着 \(32\times32=1024\) 个 CTA，**每个 CTA 都要完整走一遍固定成本**：SMEM pool 分配、`mbarrier.init`、`tcgen05.alloc`、fence 与 `cta_sync`、最后的 `relinquish` / `dealloc`——然后只算一个 tile 就退出。书的原话是：

> Each CTA performs its own initialization and exits after computing one tile.
> （[chapter_gemm_async/index.md:462-464](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L462-L464)）

persistent 内核把方向反过来：**launch 一个更小的一维 grid**，本例 `SM_COUNT=148`（B200 的 SM 数），每个 CTA 用循环从调度器领 tile、算完再领，直到没有 tile 剩余。收益有两层：

1. **初始化摊销**：TMEM 分配、屏障初始化、调度器状态只做一次，资源原地保留到该 CTA 干完所有活（[chapter_gemm_async/index.md:475](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L475)）。
2. **tile 分配进内核**：调度顺序变成内核代码的一部分，于是可以挑选对 L2 友好的遍历顺序（4.3 节的主题）。

两个容易误解的点，书里都特意澄清过：

- `SM_COUNT` 只决定 **launch 多少个** persistent CTA；occupancy 与硬件调度决定**同时驻留多少个**、落在哪些 SM 上，「没有任何 CTA 永久绑定某个 SM」（[chapter_gemm_async/index.md:473](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L473)）。所以「一 CTA 一 SM」只是方便记忆的近似。
- persistent 化**不改变任何一个 tile 内部的执行方式**：三要素声明里 scope 的变化是「一个固定的 persistent CTA 池，每个 CTA 经调度器循环处理多个输出 tile」，layout 与 dispatch 都不变（[chapter_gemm_async/index.md:466-469](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L466-L469)）。

#### 4.1.2 核心流程

`hgemm_v6` 的骨架可以抽象成：

```text
bx = cta_id([SM_COUNT])                  # 一维持久 grid，148 个 CTA

# ===== 每 CTA 一次（移出了 tile 循环）=====
SMEMPool 分配 Asmem/Bsmem/Dsmem/屏障
mbarrier.init(mma_bar 与每 stage 的 tma_bar)
tcgen05.alloc(512 列 TMEM)
fence + cta_sync
tile_scheduler.init(bx)                  # 用 bx 播种本 CTA 的起始 tile

# ===== 每 tile 一轮 =====
while tile_scheduler.valid():            # 任务池还没清空
    m_st = tile_scheduler.m_idx * BLK_M  # 当前 tile 的行列起点
    n_st = tile_scheduler.n_idx * BLK_N
    phase_tma = 0                        # 相位归零（有前提，见下）
    phase_mma = 0
    prefetch 前 PIPE_DEPTH 个 K tile     # ┐
    for k in range(K_TILES):             # │ 与 Step 5 逐行相同的
        等待/发起/回填（双缓冲循环）       # ┘ 内层流水线
    epilogue：TMEM→RF→Dsmem→TMA store
    cta_sync()
    tile_scheduler.next_tile()           # 领下一张任务单

# ===== 每 CTA 一次 =====
relinquish_alloc_permit + tcgen05.dealloc
```

**相位复位约束**是本步最容易埋雷的地方，值得单独推导：

- 一个输出 tile 内有 \(K\_TILES = 4096/64 = 64\) 次 K 迭代。`mma_bar` 每 K 迭代用一次 → 每 tile 用 **64 次**；每道 TMA stage 屏障只在自己 stage 被回填时用 → 每 tile 用 \(64/2=32\) 次（[chapter_gemm_async/index.md:494](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L494)）。
- 屏障每完成一轮就翻一次相（u8-l2），所以**使用偶数次后奇偶回到初始值 0**。跨 tile 复用同一物理屏障时，下一个 tile 的局部相位变量才能安全地从 0 重新开始（[chapter_gemm_async/index.md:492-493](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L492-L493)）。
- 条件用公式表达：需要 \(\dfrac{K\_TILES}{PIPE\_DEPTH}\)（每道 stage 屏障次数）与 \(K\_TILES\)（mma 屏障次数）同为偶数，前者蕴含后者，所以一个断言就够了：

\[ K\_TILES \bmod (2 \times PIPE\_DEPTH) = 0 \]

- 若改 \(K\)、\(BLK\_K\)、\(PIPE\_DEPTH\) 使某道屏障每 tile 用**奇数次**，奇偶无法复位，相位不能简单归零——wrapper 用 `assert` 直接限制掉这类参数组合（[chapter_gemm_async/index.md:503](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L503)）。

一个对照能帮你看清「为什么 Step 5 没这个问题」：Step 5 每个 CTA 终其一生只算一个 tile，屏障只在这一个 tile 的生命周期里使用；**是 Step 6 的外层循环第一次让屏障跨 tile 复用**，相位归零问题才随之出现。

#### 4.1.3 源码精读

**（1）持久化声明：一维 grid 取代二维 tile grid。**

```python
# 1D grid: one CTA per SM (not a 2D grid anymore!)
bx = T.cta_id([SM_COUNT])
```

这一行在 [chapter_gemm_async/index.md:550-552](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L550-L552)：grid 从 Step 3–5 的 `[M // BLK_M, N // BLK_N]`（32×32）变成 `[SM_COUNT]`（148），`by` 从此消失，tile 的 M/N 起点不再来自 blockIdx，而是来自调度器。`SM_COUNT = 148` 定义在 [chapter_gemm_async/index.md:521](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L521)，注释标明它是 B200 的 SM 数。

**（2）一次性初始化：SMEM、屏障、TMEM 都在 tile 循环之外。**

[chapter_gemm_async/index.md:557-577](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L557-L577) 的分配与初始化代码和 Step 5 逐行相同（`pool.alloc` 三块 SMEM、`mbarrier.init` 一道 mma 屏障加 `PIPE_DEPTH` 道 TMA 屏障、`tcgen05.alloc` 512 列、两道 fence 加 `cta_sync`），唯一的区别是**位置**：它们现在只执行一次，供后续几十个 tile 反复使用。对照 Step 5（[chapter_gemm_async/index.md:341-364](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L341-L364)）可以确认这不是新代码，而是同样的代码换了寿命。

**（3）外层循环：从调度器取坐标，相位归零。**

```python
while tile_scheduler.valid():
    m_st = T.meta_var(tile_scheduler.m_idx * BLK_M)
    n_st = T.meta_var(tile_scheduler.n_idx * BLK_N)
    phase_tma: T.int32 = 0
    phase_mma: T.int32 = 0
```

见 [chapter_gemm_async/index.md:618-626](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L618-L626)。四行做了两件事：把调度器给出的 tile 序号 `m_idx` / `n_idx` 换算成全局行列起点（与 Step 3 的 `bx * BLK_M` 同构，只是坐标来源换了）；把两个局部相位变量重置为 0——合法性由上面的偶数次约束保障。

**（4）tile 末尾：先同步再领下一张任务单。**

```python
T.cuda.cta_sync()
tile_scheduler.next_tile()  # Move to next tile
```

见 [chapter_gemm_async/index.md:667-668](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L667-L668)。从代码可以观察到：进入下一个 tile 之前全体线程先在 CTA 屏障汇合。合理的解读（源码阅读型推理）是：回写阶段各线程读写 `Dsmem`、`tid == 0` 线程在等 store 排空，任何线程若抢跑进入下一个 tile 的 prefetch，都可能覆写还在被同伴使用的 SMEM；`cta_sync` 保证整个 CTA 齐步换任务单。

**（5）参数断言：把相位约束写进 wrapper。**

[chapter_gemm_async/index.md:531-535](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L531-L535) 的两条断言（`K % BLK_K == 0` 与 `K_TILES % (2 * PIPE_DEPTH) == 0`）把「本实现支持哪些参数组合」显式化——这是教材工程性的一个好习惯：与其在内核里处理任意奇偶性，不如把不支持的组合挡在编译前。

**（6）收尾：TMEM 释放仍在循环外。**

[chapter_gemm_async/index.md:670-674](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L670-L674) 的 `relinquish_alloc_permit` + `dealloc` 与 Step 5 相同，在所有 tile 完成后执行一次——与开头的一次 `alloc` 首尾呼应，正好框出 CTA 的「入职—退休」区间。

#### 4.1.4 代码实践

**实践目标**：把 `hgemm_v6` 里「每 CTA 一次」与「每 tile 一次」的代码彻底分开，亲手验证 persistent 化改的是执行结构而不是计算本身。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [chapter_gemm_async/index.md:544-676](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L544-L676) 的完整内核，画两条竖线：一条在 `while tile_scheduler.valid():`（L619）之前，一条在循环结束之后（L670 的 dealloc 之前）。
2. 建一张两列清单，把 L544–L676 的每个代码段归入「循环外（每 CTA 一次）」或「循环内（每 tile 一次）」：grid 声明、SMEMPool 分配、屏障初始化、`tcgen05.alloc`、`tmem` 声明、调度器构造与 `init`、`tma_load` / `mma` 两个 `@T.inline` 辅助函数、prefetch、K 循环、`Dreg` 寄存器分配、epilogue、`cta_sync`、`next_tile`、`dealloc`。
3. 对照 Step 5 的内核（[chapter_gemm_async/index.md:329-454](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L329-L454)）逐行 diff：确认内层 K 循环与 epilogue 除 `m_st` / `n_st` 的来源外没有别的变化。

**需要观察的现象**：

- 归入「循环外」的应该恰好是全部资源管理代码（SMEM、屏障、TMEM、调度器状态）；归入「循环内」的应该恰好是全部数据通路代码（加载、MMA、回写）。
- `Dreg` / `Dreg_f16` 的 `T.alloc_local` 写在循环体内（[chapter_gemm_async/index.md:649-650](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L649-L650)），每个 tile 复用同一批寄存器槽位。

**预期结果**：得到一张「初始化只做一次」的完整清单；并能口头回答——若把 `mbarrier.init` 挪进 while 循环会发生什么（提示：屏障在 TMEM 还持有上一个 tile 的累加状态时被重置，且 u8-l2 的相位模型会被打断）。

#### 4.1.5 小练习与答案

**练习 1**：把 `SM_COUNT` 从 148 改成 296（假设 GPU 还是 148 个 SM），内核还正确吗？性能大概会怎么变？

**参考答案**：正确性不受影响——调度器以 `num_clusters` 为模给 CTA 分 tile，296 个 CTA 同样能覆盖全部 1024 个 tile。但 B200 只有 148 个 SM，一维 grid 296 个 CTA 意味着硬件要分批驻留（后 148 个等前面退出才能上），persistent「一次入住、循环领单」的摊销收益被抵消，还多付一次 wave 切换的尾部。反过来把 `SM_COUNT` 改得远小于 148（如 74）也会损失并行度。（推演题，结论待本地验证。）

**练习 2**：为什么 Step 5 不需要担心「屏障使用次数的奇偶性」，而 Step 6 需要？

**参考答案**：Step 5 每个 CTA 只算一个 tile，屏障在一个 CTA 生命周期内只服务这一个 tile，局部相位变量从 0 开始跟踪到内核结束即可。Step 6 的外层循环让同一物理屏障跨 tile 复用；只有当每 tile 的使用次数为偶数、屏障奇偶在 tile 边界回到初始值时，下一个 tile 才能把 `phase_tma` / `phase_mma` 重新初始化为 0。

**练习 3**：若 `PIPE_DEPTH=2` 不变、`K=4032`（`K_TILES=63`），书中的断言会怎样？为什么这不仅是「断言挑剔」？

**参考答案**：`63 % 4 != 0`，断言失败、wrapper 拒绝构造内核。实质原因是：`mma_bar` 每 tile 用 63 次（奇数），tile 结束时屏障奇偶为 1 而非 0；下一个 tile 若仍从 `phase_mma=0` 开始，`try_wait` 会把上一个 tile 的最后一次完成误认成本 tile 的第一次完成（u8-l2 的提前通过故障），静默产出错误结果。正确做法是把相位变量提升到循环外延续跟踪，或调整分块参数——教材选择了用断言限制参数域。

### 4.2 tile scheduler：ClusterPersistentScheduler2D

#### 4.2.1 概念说明

调度器是**内核里的一个 Python 对象**，专职回答一个问题：「CTA `bx` 接下来该算哪个 tile」。它是 Step 6 唯一的新依赖：

```python
from tvm.backend.cuda.lang.tile_scheduler import ClusterPersistentScheduler2D
```

（[chapter_gemm_async/index.md:509-516](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L509-L516)，这是 imports 里相对 Step 5 唯一新增的一行。）

四个构造参数各自回答一个问题：

| 参数 | 本例取值 | 回答的问题 |
| --- | --- | --- |
| `"ts"` | 名字 | 调度器实例名 |
| `num_m_tiles` | `M // BLK_M = 32` | M 方向有多少个 tile 行 |
| `num_n_tiles` | `N // BLK_N = 32` | N 方向有多少个 tile 列 |
| `l2_group_size` | `8` | 每 8 个连续 M 行分一组，决定遍历顺序（4.3 节） |
| `num_clusters` | `SM_COUNT = 148` | 有多少个认领单位来瓜分这些 tile |

使用侧是一套**四方法循环接口**：

```text
init(bx)        # 用 CTA 编号播种，确定本 CTA 的第一个 tile
valid()         # 还有 tile 要算吗？（while 循环条件）
m_idx / n_idx   # 当前 tile 的行列序号（0..num_*_tiles-1）
next_tile()     # 前进到本 CTA 的下一个 tile
```

要点与边界：

- **这是静态调度**：每个 CTA 的 tile 序列在内核开始执行前就由公式决定，运行中不看实际进度。它的局限（起步晚的 worker、代价不均的 tile 造成尾部空闲）在 CLC 章（u8-l3）有系统讨论，本讲先记住「静态」这个定语。
- **实现不在本仓库**：`ClusterPersistentScheduler2D` 是 `apache-tvm` 包里的类（`tvm/backend/cuda/lang/tile_scheduler.py`）。书里说清了行为契约（「每个 CTA 从调度器取一个 tile，算完再请求，直到没有 tile」），但**没有给出 `bx` ↔ tile 序列的具体公式**——这一点要到 TVM 源码里核对，本讲 4.2.4 的实践就是去做这件事。
- **向前延续**：Step 7 原样保留这个调度器，只改 warp 角色（[chapter_gemm_advanced/index.md:29](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L29)）；Step 8 引入双 CTA cluster 后，认领单位从 CTA 变成 cluster，参数相应改为 `num_clusters=SM_COUNT // CTA_GROUP`（[chapter_gemm_advanced/index.md:490-492](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L490-L492)）。

#### 4.2.2 核心流程

调度器在内核里的生命周期与 while 循环严格重合：

```text
tile_scheduler.init(bx)          # 进 while 之前：播种
while tile_scheduler.valid():    # 每轮先问"还有吗"
    读 m_idx、n_idx → 换算 m_st、n_st
    ...内层流水线与回写...
    tile_scheduler.next_tile()   # 算完才前进
```

每次 `next_tile()` 之后，`m_idx` / `n_idx` 指向该 CTA 序列中的下一个 tile；当序列耗尽时 `valid()` 变假、循环退出。注意 `next_tile()` 在 `cta_sync()` 之后执行——前进是全 CTA 的集体动作，不是单个线程的私事。

#### 4.2.3 源码精读

**（1）教学片段：一维 grid + 调度器构造。**

```python
bx = T.cta_id([SM_COUNT])  # 1D persistent grid

tile_scheduler = ClusterPersistentScheduler2D(
    "ts",
    num_m_tiles=M // BLK_M,
    num_n_tiles=N // BLK_N,
    l2_group_size=8,
    num_clusters=SM_COUNT
)
tile_scheduler.init(bx)
```

见 [chapter_gemm_async/index.md:479-490](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L479-L490)。这段是书中「持久调度」小节的演示代码：先声明一维 grid，再构造调度器并 `init`。完整内核里的对应代码在 [chapter_gemm_async/index.md:584-592](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L584-L592)，参数完全一致。

**（2）完整内核中的消费点：坐标换算。**

```python
m_st = T.meta_var(tile_scheduler.m_idx * BLK_M)
n_st = T.meta_var(tile_scheduler.n_idx * BLK_N)
```

见 [chapter_gemm_async/index.md:620-622](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L620-L622)。与 Step 3 的 `m_st = bx * BLK_M` 对照：乘法相同，乘数从 blockIdx 换成调度器索引——这就是「tile 分配进内核」在代码层面的全部样子。下游的 `tma_load(stage, k_offset, m_st, n_st)`（[chapter_gemm_async/index.md:596-610](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L596-L610)）比 Step 5 版本多了 `m_st` / `n_st` 两个参数，正是因为起点每个 tile 都在变。

**（3）前进点。**

`tile_scheduler.next_tile()` 出现在 [chapter_gemm_async/index.md:668](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L668)，紧跟在 L667 的 `cta_sync()` 之后，是 while 循环体的最后一条语句。

#### 4.2.4 代码实践

**实践目标**：到 TVM 包里找到 `ClusterPersistentScheduler2D` 的实现本体，核对「`bx` 如何映射到 tile 序列」这一书中未展开的公式。

**操作步骤**：

1. 在装好 `apache-tvm==0.26.0` 的环境（u1-l3 的环境）里执行：

   ```python
   import tvm.backend.cuda.lang.tile_scheduler as ts
   print(ts.__file__)          # 打印 tile_scheduler.py 的绝对路径
   ```

2. 用编辑器打开该文件，定位 `ClusterPersistentScheduler2D` 类，重点读三个方法：`init`（如何用 `bx` 算出第一个 tile 序号）、`next_tile`（序号如何前进——跨步 `+num_clusters` 还是连续块）、`valid` / `m_idx` / `n_idx`（序号如何解码成二维坐标、`l2_group_size` 在解码里起什么作用）。
3. 把读到的公式抄进你的笔记，与 4.3.2 中按书内描述推演的逻辑顺序对表。

**需要观察的现象**：

- `m_idx` / `n_idx` 的解码中是否出现 `l2_group_size` 的除法/取模（组号 = m 序号 // 8 之类）；
- `next_tile` 的步长是否与 `num_clusters` 有关。

**预期结果**：能写出一条「`bx` → 第 i 个 tile 坐标」的显式公式，并判断每个 CTA 拿到的 tile 是跨步交错（tile 序号 ≡ bx mod num_clusters）还是连续分块。书中未给出该公式，**具体结论待本地验证**；无 TVM 环境的读者可直接做 4.3.4 的顺序推演实践，不影响本讲主线。

#### 4.2.5 小练习与答案

**练习 1**：为什么调度器的第四个参数叫 `num_clusters` 而不是 `num_ctas`？

**参考答案**：认领 tile 的单位是 cluster（本例 `cta_group=1`，一个 cluster 恰好一个 CTA，两者重合）。到 Step 8 采用 `cta_group::2` 后，一个 cluster 是一对 CTA、共同算一个 tile，此时认领单位数是 `SM_COUNT // CTA_GROUP = 74`，参数名如实反映了这个抽象层次。

**练习 2**：如果去掉 `while tile_scheduler.valid():` 改成 `for t in range(1024):`（每个 CTA 都遍历全部 tile），会发生什么？

**参考答案**：每个 tile 会被 148 个 CTA 各算一遍，结果虽然仍是正确的（写同一块 D），但做了 148 倍的冗余功。`valid()` 的职责正是让每个 CTA 只认领属于自己的那一小段子序列，合起来恰好不重不漏地覆盖 1024 个 tile——调度器的本质就是「对 tile 全集做一次划分」。

### 4.3 L2 局部性：tile 遍历顺序为什么重要

#### 4.3.1 概念说明

回顾 u11-l4 留下的问题：空间分块后，**同 `bx` 的 32 个 CTA 各自重复读同一个 A 行块，同 `by` 的 32 个 CTA 各自重复读同一个 B 行块**，而内核的 SMEM 是私有的、不显式共享这些 tile（[chapter_gemm_basics/index.md:526-528](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L526-L528)）。这批重复读全靠 **L2 cache**（所有 SM 共享的片外缓存）消化：第一个 CTA 把 A 行块从 HBM 拉进 L2，后来的 CTA 命中 L2 就不必再走 HBM。

L2 命中与否，取决于「**时间上相近的访问是否用到同一批数据**」——而这恰好是 tile 遍历顺序能影响的量。Step 6 之前，顺序由 blockIdx 的硬件发射顺序隐式决定；Step 6 把顺序交给调度器，于是可以**故意**让共用数据的 tile 在调度序列中彼此靠近。

书中对 `l2_group_size=8` 顺序的描述（[chapter_gemm_async/index.md:477](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L477)）：

- 把 M 方向上**连续 8 行**输出 tile 分为一组；
- 组内**先固定一个 N tile 列**，让 tile 序号沿这 8 行 M 递增，走完再换下一个 N 列；
- 效果：**共用同一个 B tile 的任务在调度顺序中彼此接近；同一组 A tile 在较短区间内反复出现**。

两句话要诚实地补上书里的限定：各 CTA 仍然独立搬运数据，硬件实际执行顺序也可能不同，**这种编号方式只是让 L2 复用更可能（more likely），不是保证**（[chapter_gemm_async/index.md:477](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L477)）。调度器改善的是统计意义上的时间局部性，不是确定性协议。

#### 4.3.2 核心流程

按书中描述，\(32\times32\) 的 tile 网格被划成 \(\lceil 32/8 \rceil = 4\) 个组，逻辑序号这样生成：

```text
for g in 0..3:                # 组：M 方向连续 8 行
    for n in 0..31:           #   组内先固定 N 列
        for m_local in 0..7:  #     序号沿 8 行 M 递增
            发放 tile (m = 8*g + m_local, n)
```

即前 16 个逻辑序号依次是：(0,0) (1,0) … (7,0)，(0,1) (1,1) … (7,1)，……同一组的 256 个 tile 全部发完才进入下一组。

用这本书自己的数字做一遍容量账（B200、fp16、\(K=4096\)；L2 可用容量取 50 MiB 口径，见 [chapter_flash_attention/index.md:844](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L844)）：

- 一个 A 行块（128 行 × 全 K）fp16 体积：\(128 \times 4096 \times 2\,\text{B} = 1\,\text{MiB}\)；一组 8 行 = **8 MiB**。
- 一个 B 列块（128 列 × 全 K）fp16 体积同为 **1 MiB**。
- A 与 B 全量：\(2 \times 4096 \times 4096 \times 2\,\text{B} = 128\,\text{MiB} \gg 50\,\text{MiB}\)——**总账放不进 L2，顺序必须挑**。
- 组内扫描期间，A 侧工作集恒定为该组的 8 MiB（远小于 L2），因此整组 256 个 tile 用的 A 都能留在 L2 里反复命中（「同一组 A tile 短窗重现」的量化版）；B 侧每换一个 N 列流入 1 MiB，同列的 8 个连续 tile 里后 7 个大概率命中（「共享 B tile 的任务彼此接近」的量化版）。

一般化的两个计数公式（综合实践会用到）：

\[ T_{\text{tiles}} = \frac{M}{BLK_M} \times \frac{N}{BLK_N}, \qquad \bar{T}_{\text{per CTA}} = \frac{T_{\text{tiles}}}{SM\_COUNT} \]

#### 4.3.3 源码精读

**（1）顺序描述原文。** [chapter_gemm_async/index.md:477](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L477) 给出分组规则、两条收益（B 靠近 / A 重现）与「更可能而非保证」的限定——本节全部论证的原始出处。

**（2）参数落点。** 完整内核里 `l2_group_size=8` 出现在调度器构造的参数表中（[chapter_gemm_async/index.md:585-591](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L585-L591)）。注意它**只**出现在这里：内层 K 循环、MMA、epilogue 对分组一无所知——遍历顺序是纯调度层概念，这也是「计算与调度解耦」的一个活例。

**（3）收益归因的出处。** Step 4 → Step 7 合计约 2.2×（0.49 ms → 0.23 ms）的区间里，persistent 调度是三个机制之一（另两个是软件流水线与 warp 特化，[chapter_gemm_advanced/index.md:888](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L888)）；书没有单独给出「Step 6 一项贡献多少毫秒」，归因到具体数字时不要夸大。

#### 4.3.4 代码实践

**实践目标**：用一段独立脚本复现逻辑 tile 顺序，验证「8 个共享 B tile 的任务连续」与「一组 A 工作集 8 MiB」两个论断。

**操作步骤**：

1. 把下面脚本存成 `tile_order.py` 并运行（**示例代码**：按书中 L477 的文字描述建模，不是 TVM 调度器的实现副本）：

   ```python
   def tile_order(num_m_tiles, num_n_tiles, l2_group_size):
       """按书中描述生成逻辑 tile 顺序（示例代码）"""
       order = []
       for g in range(num_m_tiles // l2_group_size):
           for n in range(num_n_tiles):
               for m_local in range(l2_group_size):
                   order.append((g * l2_group_size + m_local, n))
       return order

   order = tile_order(num_m_tiles=32, num_n_tiles=32, l2_group_size=8)
   print("tile 总数:", len(order))            # 1024
   print("前 10 个:", order[:10])
   ```

2. 在脚本里补两段检查：
   - 断言 `order[i]` 与 `order[i+1]` 在 i 每 8 步内 `n` 相同（共享同一 B tile 的 8 个任务连续）；
   - 统计任取一个长度 256 的窗口（恰好一组），`m` 坐标是否只落在同一组的 8 个值上。

3. 加上容量账：

   ```python
   K = 4096
   a_block = 128 * K * 2            # 一个 A 行块 1 MiB
   group_a = 8 * a_block            # 组内 A 工作集 8 MiB
   total = 2 * 4096 * K * 2         # A+B 全量 128 MiB
   print(group_a / 2**20, total / 2**20)
   ```

**需要观察的现象**：前 10 个坐标形如 `(0,0)…(7,0),(0,1),(1,1)`；窗口统计里 `m` 的取值个数恒为 8；`group_a ≈ 8 MiB < 50 MiB < 128 MiB ≈ total`。

**预期结果**：两个论断都在脚本输出里得到机器验证；随后把 `l2_group_size` 改成 `1`（退化为「按 N 列优先、M 全扫」的顺序）重跑，观察「共享 B 的任务连续」这一条是否被破坏——这就是分组参数存在的理由。

#### 4.3.5 小练习与答案

**练习 1**：为什么调度器让「共享 B tile 的任务靠近」而不同时追求「共享 A tile 的任务也靠近」？

**参考答案**：两个目标在序号上冲突——让同 n 的 tile 连续，m 就必然分散；反之亦然。分组的解法是给两侧定不同的「时间尺度」：B 侧在 8 步的短窗内连续复用（组内同 n 的 8 个 tile），A 侧在 256 步的组长窗内持续复用（整组共用那 8 MiB）。8 MiB 的 A 工作集必须塞得进 L2，这正是 `l2_group_size` 不能取太大的现实约束。

**练习 2**：把 `l2_group_size` 从 8 改成 32（每组的 A 工作集 32 MiB），直觉上 L2 命中会更好吗？

**参考答案**：不一定。A 侧潜在复用窗变长，但 32 MiB 已占去 50 MiB 可用 L2 的大半，B 侧的流动数据容易被挤出 L2，且同 n 的 32 个任务连续意味着「回到同一个 B」的间隔变长。分组大小本质是在 A、B 两个时间尺度之间配平 L2 容量——8 是针对 B200 的调参结果，不是普适常数（同章 FA 部分也强调调度常量须随硬件重调，[chapter_flash_attention/index.md:844](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L844)）。（推演题，待本地验证。）

## 5. 综合实践

综合实践就是完整解决章末练习 3（[chapter_gemm_async/index.md:683](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L683)），并把 4.3 的顺序生成脚本接上，形成一份可复查的推演记录。

**实践目标**：算出 tile 总数与每 CTA 平均负载，写出调度器生成的遍历顺序，解释它如何改善 L2 命中。

**第一步：tile 总数。**

\[ T_{\text{tiles}} = \frac{4096}{128} \times \frac{4096}{128} = 32 \times 32 = 1024 \]

这也和书中「A \(4096\times4096\) output therefore requires 1024 CTAs」（Step 5 形态，[chapter_gemm_async/index.md:462](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L462)）相互印证：Step 5 一个 tile 一个 CTA，正好 1024 个。

**第二步：每 CTA 平均 tile 数。**

\[ \bar{T} = \frac{1024}{148} \approx 6.92 \]

即平均每个 persistent CTA 处理约 7 个输出 tile。若 TVM 的分配是跨步式（tile 序号按 `num_clusters` 取模归属，类似 CLC 章的 grid-stride 例子，[chapter_clc/index.md:24](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L24)），则 \(1024 = 6\times148 + 136\)：136 个 CTA 算 7 个、12 个算 6 个。**具体的分配式与「6/7 分裂」待按 4.2.4 的实践到 TVM 源码核对**；平均数 6.92 与「任务池不重不漏划分」的结论不依赖分配式细节。

**第三步：遍历顺序（示例代码）。**

```python
order = tile_order(32, 32, 8)     # 4.3.4 的函数
for i, (m, n) in enumerate(order[:18]):
    print(f"逻辑序号 {i:2d} -> tile (m={m}, n={n})")
```

预期输出开头为 `(0,0)(1,0)…(7,0)(0,1)(1,1)…`：每 8 个连续序号共享一个 B tile；每 256 个序号共享同一组 8 个 A 行块。

**第四步：L2 解释（写进你的记录）。**

A+B 全量 128 MiB 远超 B200 可用 L2 的 50 MiB，必须靠顺序留「近期会用」的数据在缓存里：组内 A 工作集恒为 8 MiB，整组 256 个 tile 的 A 读取都命中；B 每 N 列 1 MiB，同列 8 个连续任务只有第一个走 HBM。重复读不经过任何显式共享协议，纯粹是「编号让复用在时间上靠近」——且如书中限定，这是统计性改善而非保证。

**第五步（有 Blackwell GPU 时，可选）**：按 u9-l2 的回路编译并验证 `hgemm_v6`——`tvm.compile(..., tir_pipeline="tirx")`、PyTorch 参考断言（`rtol=2e-2, atol=1e-2`）。注意书中的提醒：一个全新 Python 会话只编译一个 step（[chapter_gemm_basics/index.md:276](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L276)）。**运行结果待本地验证**；无 GPU 环境完成前四步即达标。

## 6. 本讲小结

- **persistent 调度**：launch 固定 `SM_COUNT=148` 个一维 grid CTA，每个 CTA 用 `while tile_scheduler.valid()` 循环认领多个输出 tile；SMEM/屏障/TMEM/调度器状态的初始化从「每 tile 一次」摊销为「每 CTA 一次」，TMEM 的 alloc 与 dealloc 首尾框出 CTA 的整个工作寿命。
- **tile scheduler**：`ClusterPersistentScheduler2D` 用 `init(bx) / valid() / m_idx / n_idx / next_tile()` 五件套把 tile 分配移进内核；构造参数 `num_m_tiles`、`num_n_tiles`、`l2_group_size=8`、`num_clusters=SM_COUNT`，实现位于 TVM 包（`bx` ↔ tile 序列的具体公式需到源码核对）。
- **L2 局部性**：同 bx / 同 by 的 CTA 重复读同一 A / B 行块且无显式共享（u11-l4 遗留问题），靠 L2 消化；`l2_group_size=8` 的顺序让共享 B tile 的 8 个任务连续、整组 8 MiB 的 A 工作集在组长窗内反复命中——统计性改善，非保证。
- **相位复位约束**：屏障跨 tile 复用要求每 tile 使用次数为偶数（本参数下 `mma_bar` 64 次、每道 stage 屏障 32 次），奇偶才能在 tile 边界归零；`assert K_TILES % (2 * PIPE_DEPTH) == 0` 把这一约束挡在编译之前。
- **三要素定位**：scope 变（固定 CTA 池 + 外层 tile 循环），layout 与 dispatch 不变；内层 K 循环与 epilogue 从 Step 5 原样搬入，仅 `m_st` / `n_st` 的来源从 blockIdx 换成调度器。
- **量化结论（练习 3）**：4096×4096、BLK=128 共 1024 个输出 tile；SM_COUNT=148 时平均每个 persistent CTA 约 6.92 个 tile。

## 7. 下一步学习建议

- **下一讲 u13-l1（Step 7：warp 特化）**：多级 SMEM 流水线与本讲的持久调度器原样延续（[chapter_gemm_advanced/index.md:29](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L29)），变化的是把 TMA 与 MMA 拆给不同 warp 角色并用四道屏障交接——Step 5 埋下的重叠目标至此才真正兑现。建议带着「外层 tile 循环不动、内层换角色」的预期去读。
- **Step 8（u13-l2）**：观察同一调度器的参数从 `num_clusters=SM_COUNT` 变为 `SM_COUNT // CTA_GROUP`（[chapter_gemm_advanced/index.md:490-492](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L490-L492)），体会「认领单位从 CTA 升级为 cluster」。
- **回看 u8-l3（CLC 章）**：本讲的调度器是静态的——分配开工前冻结、无法响应实际完成时间；[chapter_clc/index.md:20-35](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_clc/index.md#L20-L35) 对这套局限的分析与 CLC 的动态认领方案，是对本讲自然的「然后呢」。
- **横向对照 FA 章**：Flash Attention 4 的 `FlashAttentionLinearScheduler` / `FlashAttentionLPTScheduler` 暴露与本讲完全相同的循环接口，且 LPT 调度器同样用分组（`L2_SWIZZLE`）保 L2 复用、用「重块先排」抗负载不均（[chapter_flash_attention/index.md:841-846](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L841-L846)）——学完 u14 后回来重读，会发现「调度器只换坐标来源、主循环无感」这一设计在此处再次出现。
- **源码延伸**：完成 4.2.4 的 TVM `tile_scheduler.py` 检视后，可以进一步看 `tirx_guide` 的编译器附录（u15-l3），理解调度器这类 `@T.inline` 展开的对象如何随 LowerTIRx 一起降级为线程级索引计算。
