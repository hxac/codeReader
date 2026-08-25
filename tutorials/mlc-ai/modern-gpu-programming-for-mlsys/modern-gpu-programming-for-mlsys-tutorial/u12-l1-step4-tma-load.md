# Step 4：TMA 异步加载

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 Step 4 相对 Step 3 改了什么、没改什么：scope 与 layout 不变，只把 GMEM→SMEM 的搬运 dispatch 从「128 个线程协作执行 `Tx.cta.copy`」换成「单线程发起、TMA 引擎执行」。
2. 掌握 TMA load 的等待方式：mbarrier 的 `arrive.expect_tx` 字节计数登记 + `try_wait`，并理解为什么 `cta_sync()` 在这里失效。
3. 掌握 TMA store 的等待方式：`cp_async.bulk.commit_group()` / `wait_group(0)`，以及为什么 load 与 store 必须用两套不同的完成机制。
4. 核对 `expect_tx` 的字节数公式 \((\text{BLK\_M}\cdot\text{BLK\_K} + \text{BLK\_N}\cdot\text{BLK\_K})\times 2\)，并预测把它改小或改大后内核分别会在哪一步出错。
5. 在内核源码中定位 TMA 配置所在的代码段：`tma_config` 字典、`mma_shared_layout` 绑定、mbarrier 初始化。

## 2. 前置知识

本讲是三条已有线索的汇合点，先各用一句话回顾：

- **Step 3 基线（u11-l4）**：输出按 \(128\times 128\) 分块，grid 为 \([M/\text{BLK\_M}, N/\text{BLK\_N}]\)，每个 CTA 用 `Tx.cta.copy` 把 A/B tile 搬进 SMEM，`cta_sync()` 之后发起 MMA；回写时每线程直接 `Tx.copy` 写 GMEM。数据搬运由 CTA 的 128 个线程亲自执行。
- **TMA 完成机制（u6-l3）**：TMA 引擎异步搬运，`cta_sync` 只同步线程、观察不到引擎；load 侧用 mbarrier 追踪「在途字节数」（`expect_tx` 登记、`complete_tx` 扣减），store 侧用 bulk async group（`commit_group` / `wait_group`）判断源缓冲何时可复用。
- **mbarrier 状态机（u8-l1）**：一道 mbarrier 同时维护相位、到达计数与 tx-count；一个相位完成的充要条件是到达计数与字节数同时归零。`arrive.expect_tx` 一次做两件事——贡献一次线程到达、登记期望字节数。

一个贯穿本讲的直觉：**「已经发起」不等于「已经完成」**。线程把 TMA 指令发射出去只花几条指令，真正的数据搬运发生在 TMA 引擎上；谁要消费这批数据，谁就必须等到引擎报告的完成信号，而不是等到发起线程返回。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [chapter_gemm_async/index.md:16-240](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L16-L240) | 本讲主源码：Step 4 的动机、对比代码、同步时序、完整内核 `hgemm_v4` 与五项 TMA 配置清单 |
| [chapter_gemm_async/index.md:681](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L681) | 章末练习 1：expect_tx 字节过大/过小的行为分析 |
| [chapter_gemm_basics/index.md:624](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L624) | Step 3 基线的回写方式：每线程 `Tx.copy` 直写 GMEM（无 Dsmem） |
| [chapter_tma/index.md:129-172](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L129-L172) | TMA load 与 store 完成机制的原理出处（本讲的 u6-l3 依据） |
| [chapter_async_barriers/index.md:34-43](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L34-L43) | `arrive.expect_tx` 双重语义（到达 + 登记字节）的原理出处 |

说明：本仓库是教材仓库，「源码」即书的 Markdown 章节及其内嵌 TIRx 内核代码。书中正文从 Step 4 起使用完整规模 \(M=N=K=4096\)。

## 4. 核心概念与源码讲解

### 4.1 TMA 加载发起：从 128 线程协作到单线程派发

#### 4.1.1 概念说明

Step 1–3 用 `Tx.cta.copy` 搬 A/B tile：CTA 的全部 128 个线程各自计算地址、亲自执行 load 与 store，再用 `cta_sync` 汇合。Step 4 把这条路径换成 TMA：**一个线程提交拷贝请求，TMA 引擎异步完成剩余的全部地址生成与 tile 传输**。

用三要素（u9-l3）描述这次变化：

- **scope**：不变，仍是一个 warpgroup；但搬运的「发起者」从全 CTA 收缩为单个线程，「执行者」从 CUDA Core 换成 TMA 引擎。
- **layout**：不变，SMEM/TMEM/寄存器 tile 与 Step 3 相同（`mma_shared_layout` 的 128B swizzle 布局原样保留）。
- **dispatch**：GMEM→SMEM 加载从同步的 `Tx.cta.copy` 改走 TMA 引擎（`dispatch="tma_auto"`）。

要注意本步的**限度**：Step 4 仍然在每次 TMA load 之后立刻等待，加载与计算还没有重叠。这一步的收益是把地址计算与搬运指令从 CTA 线程上卸载到专用引擎，为 Step 5（双缓冲预取）和 Step 7（角色级重叠）铺路。

#### 4.1.2 核心流程

```text
tid = warp_id * 32 + lane_id            # warpgroup 内 0..127
for k in range(K_TILES):
    if tid == 0:                        # 恰好一个线程
        发起 A tile 的 Tx.copy_async    # 立即返回，引擎搬运
        发起 B tile 的 Tx.copy_async
        arrive.expect_tx(tma_bar, 字节数)   # 登记完成条件
    全体线程 try_wait(tma_bar, phase_tma)   # 等引擎搬完
    if tid == 0: 发起 MMA
    全体线程 try_wait(mma_bar, phase_mma)
    phase_tma ^= 1; phase_mma ^= 1
```

两个容易踩坑的细节：

- **为什么用 `tid == 0` 而不是每个 warp 各自 `elect_sync`**：`elect_sync` 在每个 warp 内各选一个活跃 lane。四个 warp 都直接调用的话，会有 4 个线程同时发起 TMA。也可以先守卫 `warp_id == 0` 再 `elect_sync`，但 `tid == 0` 更直接。
- **两次 `try_wait` 都不被 `if` 守卫**：发起是单线程的，等待是全体的——所有线程都要确认数据到位后才继续。

#### 4.1.3 源码精读

Step 3 与 Step 4 的加载代码对比（书中先给前者的三行）：

[chapter_gemm_async/index.md:31-35](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L31-L35) —— Step 3「之前」：128 个线程参与拷贝，`cta_sync` 让 SMEM 写入对全 CTA 可见：

```python
Tx.cta.copy(Asmem[:, :], A[m_st:m_st+BLK_M, i*BLK_K:(i+1)*BLK_K])   # all 128 threads
Tx.cta.copy(Bsmem[:, :], B[n_st:n_st+BLK_N, i*BLK_K:(i+1)*BLK_K])
T.cuda.cta_sync()
```

[chapter_gemm_async/index.md:39-45](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L39-L45) —— Step 4「之后」：单线程发起 TMA load，mbarrier 追踪硬件传输完成：

```python
tid = warp_id * 32 + lane_id                 # 0..127 within the warpgroup
if tid == 0:  # exactly one thread starts TMA
    Tx.copy_async(Asmem, A[...], dispatch="tma_auto")
    Tx.copy_async(Bsmem, B[...], dispatch="tma_auto")
    T.ptx.mbarrier.arrive.expect_tx(tma_bar, byte_count)  # bytes expected from TMA
T.ptx.mbarrier.try_wait(tma_bar, phase)                  # wait before MMA reads SMEM
```

[chapter_gemm_async/index.md:47](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L47) —— 解释 `tid == 0` 守卫的选型：`tid` 把 warp ID 与 lane ID 合成 warpgroup 内的线程号；若四个 warp 直接各自 `elect_sync()`，会选出 4 个发起者。

[chapter_gemm_async/index.md:49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L49) —— 本步限度：每次 TMA load 后仍立即等待，load 与 compute 尚未重叠；变化在于地址生成与 tile 搬运从 CTA 线程转移到 TMA 引擎，减少线程执行的拷贝指令数。

完整内核中的对应实现（`hgemm_v4` 的 K 循环）：

[chapter_gemm_async/index.md:171-190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L171-L190) —— `tid` 在循环外用 `T.meta_var` 定义（编译期常量折叠）；循环体内 `if tid == 0: tma_load(k_st)` 单线程发起，全体 `try_wait(tma_bar, phase_tma)` 等待；MMA 同样单线程发起、全体等待；两道屏障每轮各用一次，故 `phase_tma` 与 `phase_mma` 每轮都翻转（对比 Step 5：TMA 屏障按 stage 分道后，`phase_tma` 只在扫完一圈 ring 时翻转）。

[chapter_gemm_async/index.md:145-160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L145-L160) —— `tma_load` 内联辅助函数：`tma_config` 字典携带 `dispatch`/`cta_group`/`mbar` 三项，两次 `Tx.copy_async` 分别搬 A、B，最后 `arrive.expect_tx` 登记字节数。

#### 4.1.4 代码实践

**实践目标**：亲手把 Step 3 与 Step 4 的加载路径差异标注出来，验证「scope/layout 不变、dispatch 改变」。

**操作步骤**：

1. 打开 [chapter_gemm_async/index.md:30-45](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L30-L45)，把「Before」「After」两段代码并排抄在本子上。
2. 逐行标注变化：同步拷贝 → `copy_async`；`cta_sync` → `arrive.expect_tx` + `try_wait`；无守卫 → `if tid == 0`。
3. 打开完整内核 [chapter_gemm_async/index.md:172-190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L172-L190)，确认教材正文的两段对比代码在真实内核中对应哪几行。
4. 若本地有 Blackwell GPU 与 tvm 环境：把书中 `hgemm_v4` 抄入 `.py` 文件（TIRx 依赖 Python 源码检视，不能放进 `python -c`），编译运行观察是否 PASS——**待本地验证**。无 GPU 则只做源码标注。

**需要观察的现象**：两版代码的循环次数、grid 形状、MMA 发起方式完全一致；唯一改变的是「谁执行搬运」与「怎么知道搬完了」。

**预期结果**：得到一张三行对照表（scope / layout / dispatch），其中只有 dispatch 一行在 Step 3→4 之间发生变化。

#### 4.1.5 小练习与答案

**练习 1**：如果把关卫从 `if tid == 0` 改成「每个 warp 内各执行一次 `elect_sync` 后发起 TMA」，会发生什么？

**答案**：warpgroup 有 4 个 warp，`elect_sync` 在每个 warp 内各选出一个 lane，共 4 个线程同时发起同一次 TMA load——A/B tile 会被重复搬运 4 份、`expect_tx` 也被登记 4 次（字节数虚增 4 倍），屏障语义被破坏。书中明确说明应先守卫 `warp_id == 0` 再 `elect_sync`，或直接用 `tid == 0`。

**练习 2**：Step 4 换成 TMA 之后，加载与计算重叠了吗？

**答案**：没有。Step 4 在每次 TMA load 后立即 `try_wait`，等这次 load 完成才发起本轮 MMA，调度仍是串行的；本步的收益是把地址生成与搬运从 CTA 线程卸载到 TMA 引擎。真正的重叠需要 Step 5 的第二级 SMEM stage（预取）与 Step 7 的 warp 角色划分。

**练习 3**：`try_wait(tma_bar, phase_tma)` 为什么不放在 `if tid == 0` 里面？

**答案**：等待的目的是让**所有**即将读取 SMEM 的线程（包括发起 MMA 前的守卫判断、以及后续 epilogue 的读取者）都确认数据已就绪。发起是单线程职责，等待是全体需求；若只有 `tid == 0` 等待，其余 127 个线程会带着未就绪的 SMEM 视图继续执行。

### 4.2 load/store 同步：mbarrier 字节计数与 commit/wait group

#### 4.2.1 概念说明

TMA load 发起后，传输在 TMA 引擎上继续进行。`cta_sync()` 只能同步 CTA 线程，**判断不了异步传输是否完成**。所以 MMA 读 SMEM 之前必须经 mbarrier 等 TMA。

load 与 store 的等待问题方向相反，因此用两套机制：

- **load（GMEM→SMEM）**：等「目标可读」。mbarrier 一个相位同时维护线程到达计数与在途字节计数；`arrive.expect_tx` 贡献一次到达并登记字节数，TMA 引擎每完成一段传输以 `complete_tx` 扣减；两个计数同时归零，相位才算完成，`try_wait` 放行。
- **store（SMEM→GMEM）**：等「源缓冲可复用」。发起 `Tx.copy_async` 后用 `cp_async.bulk.commit_group()` 把此前未提交的 TMA store 打包成一个 bulk async group，`cp_async.bulk.wait_group(0)` 表示「不允许有任何已提交组还在飞行」，返回后 `Dsmem` 才能覆写。

一个数值锚点：书中的时序图用简化例子——A、B 各 2048 字节、合计 4096 字节；真实内核里 A、B 各含 \(128\times 64\) 个 fp16 元素（各 16384 字节），`arrive.expect_tx` 共登记 32768 字节。

#### 4.2.2 核心流程

load 侧的时序（五步，自上而下）：

```text
发起线程(tid==0)  ── ① copy_async(A)  ② copy_async(B)  ③ arrive.expect_tx(总字节)
                      │  一次线程到达：pending arrival 1→0
                      │  登记在途字节：tx-count = 32768
TMA 引擎          ── ④ A、B 陆续写入 SMEM，每段完成执行 complete_tx 扣减
mbarrier         ──    tx-count 归零 且 到达计数归零 → 相位完成，翻入下一相
消费者(全体线程)  ── ⑤ try_wait(phase) 通过 → MMA 读取 SMEM 中的 A/B tile
```

注意 ③ 之后屏障并未完成：到达计数已归零，但字节数仍是 32768。这正是「发起 ≠ 完成」在计数层面的体现。

store 侧的时序（epilogue 尾部）：

```text
全体线程   写 Dsmem → fence.proxy_async("shared::cta")   # 线程写对异步代理可见
           → warpgroup_sync(10)                          # 128 线程齐步（bar.sync 10, 128）
tid == 0   发起 TMA store（Dsmem → GMEM）
           → cp_async.bulk.commit_group()                # 打包成一个组
           → cp_async.bulk.wait_group(0)                 # 排空：所有已提交组完成后返回
全体线程   warpgroup_sync(10)                            # 其余线程等到排空才继续
           （此后 Dsmem 才可覆写或复用）
```

其中 `warpgroup_sync(10)` lowers 为 `bar.sync 10, 128`：`10` 是 CTA 的 16 个命名屏障槽位（ID 0–15）之一，与 TMA 没有特殊关联，本内核单 warpgroup 且该槽空闲故选用它；命名屏障与追踪 TMA load 的共享内存 mbarrier 是两类对象，且每次同步完成后自动复位、同一 ID 可复用。

#### 4.2.3 源码精读

[chapter_gemm_async/index.md:53](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L53) —— 关键论断：`cta_sync()` 只同步 CTA 线程，无法判断异步传输是否结束；MMA 读 SMEM tile 前必须经 mbarrier 等 TMA load。

[chapter_gemm_async/index.md:59-61](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L59-L61) —— 时序图的文字版：步骤 1、2 在发起线程上（两次 `copy_async` 加 `arrive.expect_tx(4096)`）——一次线程到达使 pending arrival 归零，但字节计数仍为 4096，屏障未完成；步骤 3 由 TMA 引擎执行 `complete_tx` 扣减；两笔传输都完成后字节归零，消费者的 `try_wait(phase)` 在步骤 4 通过，MMA 在步骤 5 开始读取。

[chapter_gemm_async/index.md:63](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L63) —— 真实内核的数字：A、B 各 \(128\times 64\) 个 fp16 元素、各占 16384 字节，`arrive.expect_tx` 共登记 32768 字节。

[chapter_gemm_async/index.md:65](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L65) —— store 侧机制：线程写完 `Dsmem` 后 `fence.proxy_async` 让各线程写入对异步代理可见；第一道 `warpgroup_sync(10)` 保证 128 个线程都完成写入与 fence 后，`tid == 0` 才发起 TMA store，引擎随后可异步读取完整缓冲。

[chapter_gemm_async/index.md:67](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L67) —— 命名屏障说明：`warpgroup_sync(10)` 即 `bar.sync 10, 128`；ID 10 无 TMA 特殊含义，只是 16 个槽位中一个空闲槽；它与共享内存 mbarrier 相互独立，同步完成后复位、可复用同一 ID。

[chapter_gemm_async/index.md:69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L69) —— store 完成判定：`commit_group()` 把已发未提交的 TMA store 收进一个 bulk async group；`wait_group(0)` 的 `0` 表示不允许任何已提交组仍在飞行，全部完成后才返回；第二道 `warpgroup_sync(10)` 让其余线程等到排空，在此之前 `Dsmem` 不可覆写复用。

内核中 store 路径的完整实现：

[chapter_gemm_async/index.md:198-215](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L198-L215) —— epilogue 依次执行：`Tx.wg.copy_async` 读 TMEM 入寄存器 → `wait.ld()` + `cta_sync` 确保读完成 → `Tx.cast` fp32→fp16 → `Tx.copy` 写 `Dsmem` 各行 → `fence.proxy_async` + `warpgroup_sync(10)` → `tid == 0` 发起 TMA store 并 `commit_group` / `wait_group(0)` → 最后一道 `warpgroup_sync(10)`。

对照 Step 3 的差异：[chapter_gemm_basics/index.md:624](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L624) —— Step 3 的回写是每线程 `Tx.copy(D[m_thr, n_st:n_st+BLK_N], Dreg_f16[:])` 直写 GMEM，没有 `Dsmem`；Step 4 为走 TMA store 新增了 `Dsmem` 中转（见 4.3 的 SMEM 分配），回写路径变成 TMEM→RF→Dsmem→TMA→GMEM。

#### 4.2.4 代码实践

**实践目标**：核对 `expect_tx` 字节数公式，并预测登记过小/过大时 mbarrier 分别「卡」在哪一步（章末练习 1）。

**操作步骤**：

1. 代入实际参数验证公式：
   \[
   (\text{BLK\_M}\cdot\text{BLK\_K} + \text{BLK\_N}\cdot\text{BLK\_K})\times \text{F16\_SIZE} = (128\times 64 + 128\times 64)\times 2 = 32768 \text{ 字节}
   \]
   与 [chapter_gemm_async/index.md:63](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L63) 的说法核对。
2. 场景 A（改小）：假设把 `expect_tx` 登记成 16384（只算 A 的字节）。沿 4.2.2 的时序逐步推演：A 传输完成、引擎 `complete_tx(16384)` 后各计数如何变化？`try_wait` 何时通过？此时 B tile 处于什么状态？
3. 场景 B（改大）：假设登记成 65536。推演：两笔真实传输（合计 32768 字节）全部完成后 tx-count 还剩多少？相位能否完成？内核停在哪一行？
4. 有 Blackwell GPU 时可真实改码复现（改动 [chapter_gemm_async/index.md:157-160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L157-L160) 的字节数后重编译）——**待本地验证**；无 GPU 时纸面推演即可，推演结果与 4.2.5 答案核对。

**需要观察的现象**：改小时内核**不会卡**，而是提前放行；改大时内核在 `try_wait(tma_bar)` 处永久阻塞。

**预期结果**：见 4.2.5 练习 1 的参考答案。

#### 4.2.5 小练习与答案

**练习 1**（即章末练习 1）：Step 4 的 `arrive.expect_tx` 用 \((\text{BLK\_M}\cdot\text{BLK\_K} + \text{BLK\_N}\cdot\text{BLK\_K})\times 2\) 字节。若这个字节数过小或过大，mbarrier 分别会怎样？

**答案**：
- **过小**（如只登记 16384）：到达计数本来就正确（`init` 期望 1 次到达，`expect_tx` 贡献了它），A tile 传输完成、`complete_tx` 扣满 16384 后 tx-count 即归零，相位提前完成。`try_wait`（[chapter_gemm_async/index.md:181](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L181)）在 B tile 仍在途时就放行，MMA 读到旧的/残缺的 `Bsmem`——**内核不挂死，静默产出错误结果**，这是更隐蔽的故障。
- **过大**（如登记 65536）：两笔真实传输合计只有 32768 字节，即使全部完成 tx-count 仍剩 32768，相位永远无法完成，`try_wait(tma_bar, phase_tma)` 在第一轮 K 迭代就**永久阻塞，内核挂死**。
- 共同点：两种错误都不影响线程到达计数，错的只是字节登记；结论是 `expect_tx` 必须精确等于本相位全部关联 TMA 传输的送达字节。

**练习 2**：为什么 TMA load 用 mbarrier、TMA store 却用 commit/wait group，而不是反过来？

**答案**：两者等待的对象不同。load 等的是「目标 SMEM 可读」——消费方（MMA）需要知道数据何时**到达**，mbarrier 的字节计数正好追踪引擎的送达进度（`expect_tx` 登记、`complete_tx` 扣减）。store 等的是「源 `Dsmem` 何时可复用」——发起方关心的是引擎何时**读走**了源缓冲，bulk async group 的 `commit_group`/`wait_group(0)` 只需按组排空，不需要逐字节对账。反过来的组合都提供不了所需的信号类型。

**练习 3**：store 路径上第一道 `warpgroup_sync(10)` 与最后一道 `warpgroup_sync(10)` 各自在防什么？

**答案**：第一道在发起 TMA store **之前**——保证 128 个线程都写完 `Dsmem` 并执行了 `fence.proxy_async`，防止引擎读到不完整的缓冲（写入未完成就搬运）。最后一道在 `wait_group(0)` **之后**——把「store 已排空、`Dsmem` 已可复用」这一事实同步给其余 127 个线程，防止它们在 `tid == 0` 尚未排空时覆写 `Dsmem`。两道都是命名屏障（`bar.sync 10, 128`），与 mbarrier 无关。

### 4.3 TMA 配置在内核中的位置

#### 4.3.1 概念说明

「TMA 配置」在 TIRx 中不是一段显式构造 tensor map 描述符的代码（那是 u6-l1 讲的 PTX/CUTLASS 层概念），而是分布在内核中的**五个设置点**：`tma_config` 字典、字节数、mbarrier 初始化、`@T.inline` 辅助函数、store 同步。其中 `dispatch="tma_auto"` 表示**由编译器根据 buffer 的形状、dtype 与布局自动推导 TMA 描述符**——所以 `mma_shared_layout` 绑定的 128B swizzle 布局既是 SMEM 的物理排布，也是 TMA 描述符推导的输入，这正是 u6-l1「tensor map、SMEM 布局与 MMA 指令必须描述同一物理排布」一致性纪律在 TIRx 中的落地方式。

#### 4.3.2 核心流程

五个设置点在内核里的位置：

```text
wrapper 层  hgemm_v4(M, N, K)
  ├── BLK_M/BLK_N/BLK_K、F16_SIZE          → 决定字节数公式的输入
  └── mma_shared_layout(..., SWIZZLE_128B_ATOM, (BLK_?, BLK_K))
                                            → TMA 描述符推导的布局输入
kernel 层
  ├── pool.alloc tma_bar（uint64, align=8） → TMA 完成屏障的存储
  ├── mbarrier.init(tma_bar, 1)             → 期望 1 次线程到达
  └── @T.inline tma_load(k_st)
        ├── tma_config = {dispatch: "tma_auto", cta_group: 1, mbar: tma_bar}
        ├── Tx.copy_async × 2               → A、B 两笔搬运
        └── arrive.expect_tx(字节数公式)     → 完成条件登记
```

#### 4.3.3 源码精读

[chapter_gemm_async/index.md:226-238](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L226-L238) —— 书中亲自列出的五项 TMA 设置清单：

- **TMA config**：`{"dispatch": "tma_auto", "cta_group": 1, "mbar": tma_bar.ptr_to([0])}` 告诉 `Tx.copy_async` 使用自动 TMA 派发，并把 load 完成上报到 `tma_bar`（L230）。
- **字节数**：\((\text{BLK\_M}\cdot\text{BLK\_K} + \text{BLK\_N}\cdot\text{BLK\_K})\times 2\) 是两个 fp16 操作数 tile 加载的字节数，交给 `arrive.expect_tx`（L232）。
- **mbarrier 初始化**：`init(tma_bar.ptr_to([0]), 1)` 创建 TMA load 的完成屏障（L234）。
- **`@T.inline`**：`tma_load`/`mma` 是编译期展开进内核体的辅助函数，可使用外围内核的变量（L236）。
- **store 同步**：epilogue 先写 fp16 行进 `Dsmem`，`fence.proxy_async` 与 `warpgroup_sync` 让线程写入就绪，store 用 `commit_group`/`wait_group(0)` 等待（L238）。

配置的各处实现：

[chapter_gemm_async/index.md:86-98](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L86-L98) —— wrapper 层：`BLK_M, BLK_N, BLK_K = 128, 128, 64` 与 `F16_SIZE = 2` 是字节数公式的输入；三份 `mma_shared_layout(..., SwizzleMode.SWIZZLE_128B_ATOM, ...)` 给 A/B/D 绑定 128B swizzle 布局，是 `tma_auto` 推导描述符的依据。书中强调 wrapper 模式把形状相关常量与使用它的内核放在一起。

[chapter_gemm_async/index.md:112-121](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L112-L121) —— SMEM 分配：控制对象（`tmem_addr`、`tma_bar`、`mma_bar`）在低地址，其中 L115 新增 `tma_bar = pool.alloc((1,), "uint64", align=8)`；操作数缓冲经 `move_base_to(1024)` 摆放，L120 新增 `Dsmem`（TMA store 的源缓冲）。

[chapter_gemm_async/index.md:124-126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L124-L126) —— 屏障初始化：`mma_bar` 与 `tma_bar` 各 `init(..., 1)`——期望到达数都是 1（各自由 `tid == 0` 线程的一次 `commit`/`expect_tx` 到达驱动）。

[chapter_gemm_async/index.md:145-160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L145-L160) —— **TMA 描述符配置所在的核心代码段**：`tma_load` 内的 `tma_config` 字典（L147-150）是显式配置点；两次 `Tx.copy_async`（L151-156）携带该配置；`arrive.expect_tx`（L157-160）登记字节数。描述符本身的字段内容（box 形状、swizzle 模式等）由 `tma_auto` 从 wrapper 的布局绑定自动推导。

[chapter_gemm_async/index.md:240](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L240) —— 本步结论：数据搬运路径已经正确，但调度仍是串行——每次 load 完成后 MMA 才开始，两个引擎轮流工作；下一步保持 TMA load/store 路径不变，引入可复用的 SMEM stage 做预取。

#### 4.3.4 代码实践

**实践目标**：在 `hgemm_v4` 源码中找出全部 TMA 配置代码段，并说明每段配置了什么。

**操作步骤**：

1. 打开完整内核 [chapter_gemm_async/index.md:86-223](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L86-L223)。
2. 用关键词定位：搜索 `tma_config`、`dispatch="tma_auto"`、`expect_tx`、`tma_bar`、`commit_group`，把每一处命中标注成「描述符推导输入 / 完成上报 / 字节登记 / store 排空」四类之一。
3. 回答：内核里有没有一行代码在显式填 tensor map 的字段（globalDim、boxDim、swizzle 模式）？如果没有，这些信息从哪里来？
4. 有 GPU 时把标注结果与编译后 `ex.mod.imports[0].inspect_source()` 中的 `cp.async.bulk.tensor` 指令对照——**待本地验证**。

**需要观察的现象**：显式配置只有 `tma_config` 字典三键 + 字节数公式；tile 形状与 swizzle 信息全部来自 `mma_shared_layout` 的布局绑定。

**预期结果**：一张配置点清单表（见本讲 4.3.2 的结构图），并能回答步骤 3：`tma_auto` 派发下描述符由编译器依据 buffer 形状、dtype 与 `SWIZZLE_128B_ATOM` 布局自动推导，内核不手写描述符字段。

#### 4.3.5 小练习与答案

**练习 1**：`tma_config` 里的 `mbar: tma_bar.ptr_to([0])` 这一项删掉行不行？

**答案**：不行。它告诉 TMA 引擎把完成信号（`complete_tx` 扣减）上报到哪道 mbarrier；没有它，`arrive.expect_tx` 登记的字节永远无人核减，tx-count 永不归零，`try_wait` 永久阻塞——效果与 4.2.5 中「登记过大」同类：内核挂死在 TMA 等待处。

**练习 2**：`mbarrier.init(tma_bar.ptr_to([0]), 1)` 的第二个参数 `1` 是什么意思？为什么是 1？

**答案**：它是期望的线程到达数（expected arrival count）。本内核中每轮 K 迭代只有 `tid == 0` 这一个线程对 `tma_bar` 执行到达（`arrive.expect_tx` 自带一次到达），所以期望数为 1。相位的完成条件是到达数与字节数同时归零，二者缺一不可。

**练习 3**：`tma_load` 和 `mma` 为什么写成 `@T.inline` 辅助函数而不是普通 Python 函数？

**答案**：它们在**编译期**被展开进内核体，可以直接使用外围内核的变量（`m_st`、`n_st`、`tma_bar`、`Asmem` 等）；TIRx 通过 Python 源码检视解析内核，普通运行期函数调用无法进入 IR。这一设计也让 Step 5/6 复用同一辅助函数时改动极小（如 `tma_load` 增加 `stage` 参数）。

## 5. 综合实践

把本讲三块内容串成一份「Step 4 变更审计报告」：

1. **对比审计**：以 [chapter_gemm_async/index.md:86-223](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L86-L223) 的 `hgemm_v4` 为对象、Step 3（[chapter_gemm_basics/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md) 的 `hgemm_v3`）为基线，逐段标注差异，按 scope / layout / dispatch 三列归类，验证「只有 dispatch 变了」。
2. **字节账本**：计算 \(M=N=K=4096\)、`BLK_M=BLK_N=128`、`BLK_K=64` 下每 CTA 每轮 K 迭代的 TMA 传送字节数（应为 32768），以及整个内核（\(K\_TILES=64\) 轮、grid \(32\times 32=1024\) 个 CTA）的名义总流量，并与 u11-l4「名义流量放大 32 倍」的伏笔对照。
3. **故障预测表**：写满下表并留档，供将来有 GPU 时逐一验证（**待本地验证**）：

| 改动 | mbarrier 行为 | 内核症状 |
| --- | --- | --- |
| `expect_tx` 改小（16384） | tx-count 提前归零、相位提前完成 | 不挂死，静默错果 |
| `expect_tx` 改大（65536） | tx-count 永不归零 | 挂死在 `try_wait(tma_bar)` |
| 去掉 `tma_config` 的 `mbar` 键 | 无 `complete_tx` 上报 | 挂死在 `try_wait(tma_bar)` |
| 去掉 store 侧 `wait_group(0)` | 引擎仍读旧 `Dsmem` | 单 tile 下可能碰巧正确，多 tile 复用 `Dsmem` 时数据损坏 |

4. **配置定位**：给出 4.3.4 的 TMA 配置点清单表，注明每处「配置了什么、被谁消费」。

## 6. 本讲小结

- Step 4 只改 dispatch：GMEM→SMEM 搬运从 128 线程协作的 `Tx.cta.copy` + `cta_sync` 换成 `tid == 0` 单线程发起的 `Tx.copy_async(dispatch="tma_auto")` + TMA 引擎执行；scope 与 layout 均不变，回写新增 `Dsmem` 中转走 TMA store。
- `cta_sync` 只能同步线程、观察不到 TMA 引擎：load 的完成靠 mbarrier——`arrive.expect_tx` 一次贡献到达并登记 \((128\cdot 64 + 128\cdot 64)\times 2 = 32768\) 字节，引擎 `complete_tx` 扣减，两个计数同时归零相位才完成。
- `expect_tx` 登记过小 → 相位提前完成、静默读错数据；过大 → 永不完成、挂死在 `try_wait`；字节账必须与实际传送精确相等。
- store 方向相反：等的是源缓冲可复用，用 `commit_group()` + `wait_group(0)` 排空；两道 `warpgroup_sync(10)`（`bar.sync 10, 128`，命名屏障 ID 与 TMA 无关）分别防护「写入未完就搬运」与「未排空就覆写」。
- TMA 配置在 TIRx 中是五个设置点：`tma_config` 字典、字节数公式、mbarrier 初始化、`@T.inline` 辅助函数、store 同步；tensor map 描述符由 `tma_auto` 从 buffer 形状与 `SWIZZLE_128B_ATOM` 布局自动推导，内核不手写字段。
- 本步数据路径已正确但调度仍串行：load 完成后 MMA 才开始，两个引擎轮流空闲——这正是 Step 5 双缓冲流水线要解决的问题。

## 7. 下一步学习建议

下一讲（u12-l2，Step 5：双缓冲软件流水线）在本讲的 TMA 路径上引入 `PIPE_DEPTH=2` 的 SMEM 环形缓冲：`Asmem`/`Bsmem` 增加前导 stage 维、`tma_bar` 变成每 stage 一道、主循环前预取前两级。阅读时重点对照本讲的两处伏笔：一是 4.1.3 提到的相位差异——Step 4 中两道屏障每轮各用一次故每轮翻转，Step 5 中 TMA 屏障按 stage 分道后 `phase_tma` 只在扫完 ring 时翻转；二是 u8-l2 的 full/empty 双向屏障理论如何落到代码。源码阅读顺序建议：[chapter_gemm_async/index.md:242-293](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L242-L293)（Step 5 讲解）→ [chapter_gemm_async/index.md:295-455](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L295-L455)（`hgemm_v5` 完整内核），并完成章末练习 2（为什么每个 stage 需要独立的 TMA 屏障）。
