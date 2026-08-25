# Step 5：双缓冲软件流水线

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 Step 5 相对 Step 4 改了什么、没改什么：scope 与 dispatch 不变，layout 从「单个 SMEM tile 对」变成 `PIPE_DEPTH` 级环形缓冲，并理解**存储冲突**是 Step 4 无法重叠的根源。
2. 掌握 prefetch（预取）与 stage 复用的完整协议：启动时装满前 `PIPE_DEPTH` 个 stage，主循环里每消费一个 stage 就把 `k + PIPE_DEPTH` 的 tile 装回刚释放的 stage。
3. 推导 stage 所有权与 barrier 的对应关系：为什么每个 SMEM stage 必须有自己的 `tma_bar`（章末练习 2），而 MMA 只需共用一道 `mma_bar`。
4. 手推 `phase_tma` 与 `phase_mma` 的演化表，说清这两个相位变量**为什么以不同速率翻转**（`phase_mma` 每迭代翻一次，`phase_tma` 扫完整个环才翻一次）。
5. 用 tile 字节数估算流水线 SMEM 成本：每级 stage 32 KB、`Dsmem` 32 KB，据此判断 `PIPE_DEPTH=3` 是否超过 B200 每 SM 228 KB 上限。

## 2. 前置知识

本讲建立在三条已有线索之上，先各用一句话回顾：

- **Step 4 基线（u12-l1）**：单线程发起 `Tx.copy_async(dispatch="tma_auto")`，TMA 引擎异步搬运；load 用 mbarrier 追踪（`arrive.expect_tx` 登记 32768 字节、`complete_tx` 扣减、`try_wait` 等待）；store 用 `commit_group` / `wait_group(0)`。但**每次 load 之后立刻等待**，加载与计算仍然串行。
- **相位复用理论（u8-l2）**：同一道 mbarrier 每完成一相就原子翻入下一相；软件用奇偶（parity）区分同一屏障的先后使用，判据是「`try_wait(P)` 在屏障离开相位 P 后返回」。深度为 \(S\) 的环满足 `stage = k mod S`、`phase = ⌊k/S⌋ mod 2`。
- **三要素框架（u9-l3）**：每个 tile 操作由 scope（谁执行）、layout（数据怎么摆）、dispatch（走哪条硬件路径）刻画，用它可以一句话说清每步优化的变化量。

一个贯穿本讲的直觉：**重叠的前提是有「两个地方可以同时放数据」**。Step 4 里加载和计算之所以只能轮流，不是因为 TMA 不够快，而是因为 SMEM 中只有一对操作数 tile——下一次 load 没有独立的目的地，提前发起就会覆盖当前 MMA 还在读的数据。这是**存储冲突**，不是同步问题；解决它要靠加缓冲，而不是加屏障。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [chapter_gemm_async/index.md:242-258](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L242-L258) | Step 5 小节开头：存储冲突的动机、三要素变化声明、双缓冲走读与目标时序图 `pipe_depth2.png` |
| [chapter_gemm_async/index.md:260-293](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L260-L293) | 相对 Step 4 的四处差异清单、prefetch 与主循环的机制伪代码、相位管理说明与「跟踪流水线」练习 |
| [chapter_gemm_async/index.md:307-455](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L307-L455) | 完整内核 `hgemm_v5`：双缓冲布局、per-stage 屏障、prefetch、带相位管理的主循环、回写与 TMEM 释放 |
| [chapter_gemm_async/index.md:457-503](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L457-L503) | Step 6 开头与「屏障使用次数为偶数才能把相位复位为 0」的约束（本讲 4.3 的延伸） |
| [chapter_gemm_async/index.md:679-683](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L679-L683) | 章末练习，其中练习 2 是本讲综合实践的核心 |
| [chapter_async_barriers/index.md:53-90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L53-L90) | 相位如何区分同一屏障的先后使用；两 stage 流水线前四次迭代的相位演化表（本讲的 u8-l2 依据） |
| [chapter_async_barriers/index.md:112-116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L112-L116) | full/empty 双屏障协议——对照理解 Step 5 为什么还没有显式 empty 屏障 |
| [chapter_gemm_advanced/index.md:315-323](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L315-L323) | 每级 stage 的 SMEM 成本公式与 B200 228 KB 上限的出处 |
| [img/pipe_depth2.png](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/pipe_depth2.png) | `PIPE_DEPTH=2` 的目标时序图：TMA 加载与 MMA 在不同 stage 上交错进行 |

说明：本仓库是教材仓库，「源码」即书的 Markdown 章节及其内嵌 TIRx 内核代码。本讲正文沿用 Step 4 起的完整规模 \(M=N=K=4096\)，即 `K_TILES = 4096 // 64 = 64`。

## 4. 核心概念与源码讲解

### 4.1 双缓冲 SMEM 环：解除加载与计算之间的存储冲突

#### 4.1.1 概念说明

「软件流水线」（software pipeline）指由内核代码（软件）组织的多级缓冲流水线，与之相对的是硬件自动预取。Step 5 要解决的问题只有一个：**SMEM 里只有一对操作数 tile，加载和计算抢同一块地**。

- 当前 MMA 正在读 stage 里的 A/B tile 时，下一次 TMA load 唯一能写的地方就是这块地——提前发起 load 会破坏正在被读的数据。
- 所以 Step 4 只能「load → 等完 → MMA → 等完 → 下一次 load」，TMA 引擎与 Tensor Core 轮流空闲。
- Step 5 的解法是**双缓冲**（double buffering）：给 A、B 各分配两份 SMEM（两级 stage），当前 MMA 读 stage 0 时，下一次 load 写 stage 1，两件事从此互不干扰。

用三要素描述本步的变化（对照 Step 5 的执行结构声明）：

- **scope**：不变，仍是一个 warpgroup（128 线程）。
- **layout**：单个 SMEM tile 对变成 `PIPE_DEPTH` 级**环形缓冲**（ring buffer）——这是本步唯一的结构性变化。
- **dispatch**：不变，仍是 TMA load + `tcgen05` MMA；本步新增的是 prefetch 与 stage 复用机制。

同时要看清本步的**限度**：`hgemm_v5` 仍是单 warpgroup 串行循环，主循环里依然「等完 MMA 才发起下一个 TMA load」。`pipe_depth2.png` 画的是双缓冲**支持**的目标时序，要等 Step 7 把 TMA 与 MMA 拆给不同 warp 角色后才能真正达到。本步的贡献是把**缓冲结构**建好。

#### 4.1.2 核心流程

书中的机制描述可以画成下面的环形流程（`PIPE_DEPTH=2`、`K_TILES` 个 K tile）：

```text
启动（prefetch）：
  装第 0 块 → stage 0
  装第 1 块 → stage 1          # 两个 stage 同时在途，流水线"注满"

主循环 k = 0 .. K_TILES-1：
  stage = k % PIPE_DEPTH        # 环形映射：0,1,0,1,...
  等待 tma_bar[stage]           # 这一 stage 的这次填充完成
  发起 MMA（读 stage 的 A/B，累加进 TMEM）
  等待 mma_bar                  # MMA 完成 ⇒ stage 的数据已消费完
  若 k + PIPE_DEPTH < K_TILES：
      发起 TMA load（第 k+PIPE_DEPTH 块 → 刚释放的 stage）  # stage 复用
```

关键点有三个：

1. **stage 与 tile 的绑定是动态的**：stage 0 依次装第 0、2、4…块 tile，stage 1 依次装第 1、3、5…块。stage 是「槽位」，tile 是「内容」。
2. **释放时机由 MMA 完成信号定义**：`try_wait(mma_bar)` 通过意味着 Tensor Core 已经读完该 stage 的 A/B，此刻这个 stage 才能被下一次 load 覆写。
3. **尾部自然收缩**：当 `k + PIPE_DEPTH ≥ K_TILES` 时不再发 prefetch，循环最后几次迭代只消费已装好的 tile，不需要专门的排空逻辑。

#### 4.1.3 源码精读

**(1) 为什么必须双缓冲**——书中动机原文，指出 Step 4 的障碍是存储而非同步：

> Step 4 cannot overlap load and compute because SMEM contains only one operand-tile pair. The next load has no independent destination; starting it early would overwrite data that the current MMA is still reading.（[chapter_gemm_async/index.md:245](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L245)）

**(2) 三要素变化声明**：scope 不变、layout 变为 `PIPE_DEPTH` 级环、dispatch 不变且注明「完整重叠在 Step 7 到来」（[chapter_gemm_async/index.md:247-250](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L247-L250)）。

**(3) 缓冲区的物理实现**——`Asmem` / `Bsmem` 增加一个前导 `PIPE_DEPTH` 维，每个 stage 拥有独立的 SMEM 存储：

```python
PIPE_DEPTH = 2
...
Asmem = pool.alloc((PIPE_DEPTH, BLK_M, BLK_K), a_type, layout=A_layout)
Bsmem = pool.alloc((PIPE_DEPTH, BLK_N, BLK_K), b_type, layout=B_layout)
```

（[chapter_gemm_async/index.md:348-349](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L348-L349)）这两行分配了 \(2 \times 128 \times 64\) 个 fp16 元素的 A 缓冲与同样大小的 B 缓冲；布局 `mma_shared_layout` 的 swizzle 作用于每个 stage 内部，前导维只是把两个同构的 stage 叠在一起。`Dsmem` 回写缓冲**不带**前导维（[chapter_gemm_async/index.md:350](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L350)），它只在 K 循环结束后用一次，不需要多级。

**(4) prefetch 的实现**——主循环开始前，由 `tid == 0` 的单线程把前两个 tile 装入两个 stage：

```python
# === Prefetch: load first PIPE_DEPTH stages ===
if tid == 0:
    for s in range(min(PIPE_DEPTH, K_TILES)):
        tma_load(s, s * BLK_K)
```

（[chapter_gemm_async/index.md:400-403](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L400-L403)）注意 `min(PIPE_DEPTH, K_TILES)`：当问题规模小到 `K_TILES < PIPE_DEPTH` 时只装实际存在的 tile，避免越界加载。两次 `tma_load` 连续发射、**中间不等待**，两块数据同时在途——这就是流水线的「注满」动作。

**(5) 主循环中的 stage 复用**——每迭代消费一个 stage、再往同一个 stage 装入 `k + PIPE_DEPTH`：

```python
for k in range(K_TILES):
    stage = k % PIPE_DEPTH
    T.ptx.mbarrier.try_wait(tma_bar.ptr_to([stage]), phase_tma)   # 等本 stage 就绪
    if tid == 0:
        mma(stage, accum=(k != 0))                                 # 消费本 stage
    T.ptx.mbarrier.try_wait(mma_bar.ptr_to([0]), phase_mma)        # 等 MMA 完成
    phase_mma ^= 1
    next_k = k + PIPE_DEPTH
    if next_k < K_TILES:
        if tid == 0:
            tma_load(stage, next_k * BLK_K)                        # 复用刚释放的 stage
    if stage == PIPE_DEPTH - 1:
        phase_tma ^= 1
```

（[chapter_gemm_async/index.md:405-427](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L405-L427)）`tma_load` 与 `mma` 都带上了 `stage` 参数（定义在 [chapter_gemm_async/index.md:376-396](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L376-L396)），分别读写 `Asmem[stage, :, :]` / `Bsmem[stage, :, :]`——同一 stage 号把「装载者」和「消费者」绑定到同一块物理 SMEM。回写段（[chapter_gemm_async/index.md:429-446](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L429-L446)）与 Step 4 完全相同，本讲不再重复。

#### 4.1.4 代码实践：对照时序图找出「串行点」

1. **实践目标**：亲眼确认「Step 5 建好了缓冲结构，但单 warpgroup 内仍是串行等待」，为 Step 7 的角色划分找到要消灭的等待。
2. **操作步骤**：
   - 打开目标时序图 [img/pipe_depth2.png](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/pipe_depth2.png)，图中 TMA 装载与 MMA 计算在不同 stage 上交错。
   - 读 Step 4 的主循环（[chapter_gemm_async/index.md:170-190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L170-L190)），标出每迭代中「发起 load → try_wait(tma) → MMA → try_wait(mma)」四个动作的先后顺序。
   - 再读 Step 5 的主循环（[chapter_gemm_async/index.md:405-427](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L405-L427)），找出 prefetch 与 stage 复用两条新语句。
   - 回答：在 Step 5 的循环里，第 `k+1` 次 TMA load 最早能在什么时刻发出？（提示：看 `try_wait(mma_bar)` 在 `tma_load(stage, next_k)` 之前的顺序。）
3. **需要观察的现象**：Step 5 中发起下一次 load 的语句位于 `try_wait(mma_bar)` **之后**，也就是必须等当前 MMA 做完；而图中的目标时序里，下一次 load 与当前 MMA 是并行的。
4. **预期结果**：你会得出结论——Step 5 中「下一次 load」最早也只能在「当前 MMA 完成」之后发出；限制来自单 warpgroup 串行执行与「先等 MMA 释放 stage 再覆写」的顺序，而不是缓冲不够。这正是书中「full role-level overlap arrives in Step 7」的含义。有 Blackwell GPU 的读者可把 `hgemm_v5` 抄入 `.py` 文件编译运行验证正确性（编译与验证流程见 u9-l2）；无 GPU 时本实践为源码阅读型，结论可直接从语句顺序推出，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Asmem`/`Bsmem` 保持双份、但主循环仍按 Step 4 的方式「先 load 到 `stage=0`、等完、MMA、再 load 到 `stage=1`、等完、MMA……」，TMA 与 MMA 能重叠吗？

**答案**：不能。虽然有两个缓冲，但「发起下一次 load」仍然排在「等待当前 MMA 完成」之后，load 与 MMA 在时间上不交叠。双缓冲只是提供了**可以并行使用**的两块地，是否真的并行取决于控制流何时发起 load。这也说明 Step 5 的价值是「结构就绪 + 预取提前量」，真正让两条链路并发要靠 Step 7 的角色划分。

**练习 2**：`Dsmem` 为什么不需要像 `Asmem`/`Bsmem` 一样加 `PIPE_DEPTH` 前导维？

**答案**：`Dsmem` 是 epilogue 的回写中转缓冲，只在 K 循环全部结束、最终结果从 TMEM 读回后使用一次（[chapter_gemm_async/index.md:429-446](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L429-L446)），不存在「边读边写」的冲突，也没有多个在途版本需要暂存；给它加倍只会浪费 32 KB SMEM。

### 4.2 prefetch 与 stage 复用：环形缓冲的所有权与 per-stage 屏障

#### 4.2.1 概念说明

「所有权」（ownership）视角是把流水线看对的关键：**任意时刻，每个 stage 处于且仅处于一种角色手中**——

- **TMA 引擎正在填**（load 在途，stage 数据不完整）；
- **等待消费**（填充完成，数据有效，等 MMA 来读）；
- **Tensor Core 正在读**（MMA 在途）；
- 读完即释放，回到「可被下一次 load 覆写」状态。

在 Step 5 的单 warpgroup 结构里，这些状态的切换全部由两道屏障驱动：`tma_bar[stage]` 标记「填充完成」（对应 u8-l2 中的 **full** 方向），`mma_bar` 标记「消费完成」。注意 `mma_bar` 只有一道、为全体迭代共用——因为所有 K 迭代累加的是**同一个 TMEM 累加器**，MMA 的完成事件天然只有一条时间线；而 TMA 的完成事件按 stage 分成 `PIPE_DEPTH` 条独立时间线，所以 **`tma_bar` 必须每个 stage 一道**（数组分配见 [chapter_gemm_async/index.md:345](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L345)，初始化循环见 [chapter_gemm_async/index.md:353-358](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L353-L358)）。

这里还藏着一个对照 u8-l2 的要点：完整的缓冲复用协议应有 full/empty **两道**屏障（full 交数据、empty 还缓冲，见 [chapter_async_barriers/index.md:112-116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L112-L116)）。Step 5 里**没有显式 empty 屏障**——因为单 warpgroup 里 `try_wait(mma_bar)` 与下一次 `tma_load` 发射在同一个线程流程中先后执行，MMA 完成等待**兼任**了 empty 信号（「缓冲已归还」）。一旦 Step 7 把生产者与消费者拆成不同 warp，两件事不再同线程串行，显式 empty 屏障就必不可少了。

#### 4.2.2 核心流程

把 Step 5 的屏障与 stage 归属整理成所有权协议（`K_TILES=64`、`PIPE_DEPTH=2`）：

| 屏障 | 数量 | 每相位的完成条件 | 生产者（谁让它完成） | 消费者（谁在等） | 每 tile 使用次数 |
| --- | --- | --- | --- | --- | --- |
| `tma_bar[s]`（s=0,1） | 2 | 1 次线程到达（`arrive.expect_tx`）+ 32768 字节 `complete_tx` 扣减到零 | 发起线程 + TMA 引擎 | 全体 128 线程 `try_wait` | 各 32 次 |
| `mma_bar[0]` | 1 | `tcgen05.commit` 登记的 1 次硬件到达（MMA 完成后补达） | Tensor Core | 全体 128 线程 `try_wait` | 64 次 |

stage 内容随迭代演化的所有权时间线（以 `K_TILES=5` 为例，便于手推）：

```text
启动预取后：   stage 0 = tile 0   stage 1 = tile 1
k=0 迭代内：   消费 stage 0(tile 0)，随后把 tile 2 装入 stage 0
k=1 迭代内：   消费 stage 1(tile 1)，随后把 tile 3 装入 stage 1
k=2 迭代内：   消费 stage 0(tile 2)，随后把 tile 4 装入 stage 0
k=3 迭代内：   消费 stage 1(tile 3)，无新装（next_k=5 已越界）
k=4 迭代内：   消费 stage 0(tile 4)，无新装
```

规律：**stage `k % 2` 永远只装偶偶交替的 tile 序列**——stage 0 装 0,2,4…，stage 1 装 1,3,5…。每 stage 一道屏障，恰好为这条独立的装载时间线记账。

#### 4.2.3 源码精读

**(1) 四处代码差异清单**——书中把 Step 5 相对 Step 4 的变化压缩为四条：缓冲加前导维、`tma_bar` 变数组、循环前 prefetch、循环内 `stage = k % PIPE_DEPTH` 复用（[chapter_gemm_async/index.md:260-265](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L260-L265)）。逐条对照源码：前两条在分配与初始化段（[chapter_gemm_async/index.md:341-358](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L341-L358)），后两条在循环段（[chapter_gemm_async/index.md:400-427](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L400-L427)）。

**(2) per-stage 屏障的分配与初始化**：

```python
# Double-buffered TMA barriers (one per stage), single MMA barrier
tma_bar = pool.alloc((PIPE_DEPTH,), "uint64", align=8)
mma_bar = pool.alloc((1,), "uint64", align=8)
...
if warp_id == 0:
    if lane_id == 0:
        T.ptx.mbarrier.init(mma_bar.ptr_to([0]), 1)
        for s in range(PIPE_DEPTH):
            T.ptx.mbarrier.init(tma_bar.ptr_to([s]), 1)
```

（[chapter_gemm_async/index.md:345-358](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L345-L358)）两道 `tma_bar` 与一道 `mma_bar` 的期望到达数都是 1：TMA 侧的「一次到达」由发起线程的 `arrive.expect_tx` 贡献（字节另记），MMA 侧的「一次到达」由 `tcgen05.commit` 挂接的硬件补达贡献。

**(3) `tma_load` 把字节账本绑定到 stage**：

```python
@T.inline
def tma_load(stage, k_offset):
    tma_config = T.meta_var({
        "dispatch": "tma_auto", "cta_group": 1,
        "mbar": tma_bar.ptr_to([stage])          # ← 完成信号登记到本 stage 的屏障
    })
    Tx.copy_async(Asmem[stage, :, :], ...)
    Tx.copy_async(Bsmem[stage, :, :], ...)
    T.ptx.mbarrier.arrive.expect_tx(
        tma_bar.ptr_to([stage]),
        (BLK_M * BLK_K + BLK_N * BLK_K) * F16_SIZE)   # 32768 字节
```

（[chapter_gemm_async/index.md:376-390](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L376-L390)）每次调用把 \((128 \times 64 + 128 \times 64) \times 2 = 32768\) 字节登记到**本 stage** 的屏障上。注意与 Step 4 的差别：Step 4 的 `tma_load` 没有参数、永远写唯一的缓冲与唯一的屏障；Step 5 的 `stage` 参数同时决定了数据落点（`Asmem[stage]`）与账本归属（`tma_bar[stage]`）——**数据在哪，账就记在哪**。

**(4) `mma` 只关心 stage 的数据、共用一道完成屏障**（[chapter_gemm_async/index.md:392-396](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L392-L396)）：`Tx.gemm_async(tmem[:, :BLK_N], Asmem[stage], Bsmem[stage], ...)` 读的是本 stage 的操作数，但 `tcgen05.commit` 永远挂到 `mma_bar[0]`——因为累加器 `tmem` 只有一份。

#### 4.2.4 代码实践：手推并验证 stage 所有权表

1. **实践目标**：把 4.2.2 的所有权时间线从「读结论」变成「自己推出来的结论」，并用一段普通 Python 脚本交叉验证。
2. **操作步骤**：
   - 取 `PIPE_DEPTH=2`、`K_TILES=5`，逐迭代手写两行记录：本迭代消费哪个 stage 的哪个 tile；本迭代把哪个新 tile 装入哪个 stage。
   - 写一个小脚本模拟同样的分配规则（**示例代码**，不依赖 tvm，任何有 Python 的机器都能跑）：

     ```python
     # 示例代码：模拟 PIPE_DEPTH=2 的 stage 分配，验证手推的所有权表
     PIPE_DEPTH, K_TILES = 2, 5
     loaded_by = {}
     for t in range(min(PIPE_DEPTH, K_TILES)):
         loaded_by[t] = "prefetch"            # 启动阶段直接装入 stage t
     for k in range(K_TILES):
         stage = k % PIPE_DEPTH
         next_k = k + PIPE_DEPTH
         new_load = None
         if next_k < K_TILES:
             loaded_by[next_k] = f"k={k} 迭代装入 stage {stage}"
             new_load = f"tile {next_k} -> stage {stage}"
         print(f"k={k}  stage={stage}  消费 tile {k}（由 {loaded_by[k]}）  新装入: {new_load}")
     ```
   - 核对脚本输出与手推表是否一致，再数一数：每个 stage 各被装了几次？每道 `tma_bar` 各完成几相？
3. **需要观察的现象**：stage 0 的装载序列是 tile 0,2,4，stage 1 是 tile 1,3；两个 stage 各自形成一个独立的「装载→完成→复用」循环。
4. **预期结果**：手推表与脚本输出一致；`tma_bar[0]` 完成 3 相、`tma_bar[1]` 完成 2 相、`mma_bar` 完成 5 相（`K_TILES=5` 时）。这一不均等正是下一模块相位管理要处理的现象。

#### 4.2.5 小练习与答案

**练习 1**（即章末练习 2，[chapter_gemm_async/index.md:682](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L682)）：为什么每个 SMEM stage 需要自己的 TMA 屏障，而不是两个 stage 共用一道 `tma_bar`？

**答案**：三个角度合起来看。(a) **等待粒度**：`try_wait` 的单位是「一道屏障的一个相位」。要表达「只等 stage 0 就绪、不等 stage 1」，stage 0 的完成就必须是一道独立屏障上的独立相位。(b) **字节账本会串账**：`expect_tx` 登记的在途字节是每道屏障一份。共用时，两次 load 的 32768+32768 字节记进同一相位，`try_wait` 通过的条件变成「所有已登记字节全部到齐」——消费者被迫等待所有在途传输，预取深度退化为 1，双缓冲失去意义。(c) **相位翻转节奏对不上**：屏障硬件每完成一相就翻转一次奇偶；两个 stage 的完成事件是两条独立时间线（各自 32 次），一道屏障无法同时承载两条时间线各自的奇偶，软件里单一 `phase_tma` 变量也就无从跟踪。per-stage 屏障让「数据、账本、相位」三者在 stage 粒度上对齐。

**练习 2**：`mma_bar` 为什么可以只有一道、全程共用？

**答案**：所有 K 迭代的 MMA 写的都是同一个 TMEM 累加器 `tmem[:, :BLK_N]`（首块 `accum=False` 覆盖、其后 `accum=True` 累加，见 [chapter_gemm_async/index.md:413-414](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L413-L414)），完成事件只有一条时间线，每迭代恰好一相、奇偶交替，一道屏障加一个每迭代翻转的 `phase_mma` 即可精确记账。

### 4.3 相位管理：两个相位变量以不同速率前进

#### 4.3.1 概念说明

Step 5 之前（u11-l3 的 Step 2）我们已经见过「一道屏障被 K 循环复用、每轮翻转相位」的模式。Step 5 的新东西是：**`phase_tma` 与 `phase_mma` 的翻转速率不同**，因为它们追踪的对象粒度不同：

- `phase_mma` 追踪**同一道** `mma_bar[0]`——每个 K 迭代用一次，所以**每迭代翻一次**（`phase_mma ^= 1`）。
- `phase_tma` 追踪的是**一圈 stage**——一道 stage 屏障只在环回到该 stage 时才开始新一轮，所以软件的 `phase_tma` 变量要**扫完整个环（`stage` 走到 `PIPE_DEPTH-1`）才翻一次**（`if stage == PIPE_DEPTH - 1: phase_tma ^= 1`）。

这正是 u8-l2 公式 `stage = k mod S`、`phase = ⌊k/S⌋ mod 2` 的代码化身。书中还有一个精辟概括：`phase_tma` 描述的是**软件在环形缓冲上走过的圈数**，它不假设两次 TMA 传输在硬件上以什么顺序完成（[chapter_async_barriers/index.md:90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L90)）。

漏翻或错翻的后果（承接 u8-l2）：该翻不翻 → `try_wait` 用旧相位等待，屏障其实早已翻过去，**提前通过、静默读旧数据**；不该翻乱翻 → 等一个尚未到来的相位，**循环等待挂死**。

#### 4.3.2 核心流程

手推 `PIPE_DEPTH=2`、`K_TILES=5` 的完整演化（这正是书中「Trace the pipeline」练习要求的内容，见 [chapter_gemm_async/index.md:293](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L293)；前四行的原理出处是 [chapter_async_barriers/index.md:77-88](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L77-L88) 的相位演化表）：

| 迭代 k | stage | `try_wait(tma)` 用的 `phase_tma` | `try_wait(mma)` 用的 `phase_mma` | 迭代后 `phase_tma` | 迭代后 `phase_mma` | 是否再发 prefetch |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 0 | 0 | 0 | 0 | 0 | 1 | 是（tile 2 → stage 0） |
| 1 | 1 | 0 | 1 | **1** | 0 | 是（tile 3 → stage 1） |
| 2 | 0 | 1 | 0 | 1 | 1 | 是（tile 4 → stage 0） |
| 3 | 1 | 1 | 1 | **0** | 0 | 否（next_k=5 ≥ 5） |
| 4 | 0 | 0 | 0 | 0 | 1 | 否（next_k=6 ≥ 5） |

读表的方法：

- **`phase_tma` 在哪翻**：只在 `stage == PIPE_DEPTH-1` 的迭代末尾，即 k=1 与 k=3 两行（加粗处）。因为 k=1 消费完 stage 1 才算「第 0 圈扫完」，k=3 同理是第 1 圈的收尾。
- **验证与公式一致**：k=2 时 `⌊2/2⌋ mod 2 = 1` ✓；k=4 时 `⌊4/2⌋ mod 2 = 0` ✓。而 `phase_mma = k mod 2`，每一行都与上一行相反。
- **为何最后两次迭代不发 prefetch**：`next_k = k + PIPE_DEPTH` 已越过 `K_TILES-1`，所有还没消费的 tile 早已躺在 SMEM 里，无 tile 可装。流水线的「排空」不需要额外代码，由装载条件自然收尾。

#### 4.3.3 源码精读

**(1) 书中的相位管理说明**——解释两个相位变量速率不同的原文：所有 K 迭代用 `mma_bar.ptr_to([0])` 追踪同一个 TMEM 累加器，故 `phase_mma` 每迭代翻转；TMA 给每个 stage 配一道屏障，一道 stage 屏障只在环回到该 stage 时才开始新一轮，故 `phase_tma` 在 stage 序号到达环尾后才翻转（[chapter_gemm_async/index.md:285-291](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L285-L291)）。

**(2) 相位变量的初始化与使用位置**：

```python
phase_tma: T.int32 = 0
phase_mma: T.int32 = 0
...
T.ptx.mbarrier.try_wait(tma_bar.ptr_to([stage]), phase_tma)   # 用旧值等待
...
T.ptx.mbarrier.try_wait(mma_bar.ptr_to([0]), phase_mma)
phase_mma ^= 1                                                # 每迭代翻
...
if stage == PIPE_DEPTH - 1:
    phase_tma ^= 1                                            # 扫完一圈才翻
```

（初始化见 [chapter_gemm_async/index.md:373-374](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L373-L374)；主循环中的三处使用见 [chapter_gemm_async/index.md:410-427](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L410-L427)。）注意语句顺序：两次翻转都发生在对应 `try_wait` **之后**，保证等待用的是本轮的正确相位。

**(3) 相位复位约束（Step 6 预告，本讲的直接延伸）**——当同一批屏障要跨输出 tile 复用时（Step 6 的持久内核），每 tile 的使用次数必须为偶数，屏障奇偶才能回到初始值、局部相位变量才能重置为 0：

> With the current parameters, each output tile contains 64 K iterations. `mma_bar` is used 64 times, while each of the two TMA stage barriers is used 32 times. Because all of these counts are even, every barrier returns to its initial parity at the end of a tile…（[chapter_gemm_async/index.md:492-503](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L492-L503)）

Step 6 的 wrapper 因此用断言 `K_TILES % (2 * PIPE_DEPTH) == 0` 限制参数组合（[chapter_gemm_async/index.md:531-535](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L531-L535)）——它等价于「每 stage 屏障每 tile 使用次数 \(K_{\text{TILES}}/S\) 为偶数」。Step 5 本身每个 CTA 只算一个 tile、算完即退出，所以暂时不受此约束，但改参数做实验时要预见到它。

#### 4.3.4 代码实践：把 `PIPE_DEPTH` 改成 3，推相位、算 SMEM

1. **实践目标**：通过修改一个参数，同时检验「相位公式」和「SMEM 成本公式」两件武器，并判断新配置是否可行。
2. **操作步骤**：
   - **相位推演**（纸笔即可）：设 `PIPE_DEPTH=3`、`K_TILES=8`，列出 k=0..7 每行的 `stage`、`phase_tma`（公式 \(\lfloor k/3 \rfloor \bmod 2\)）、`phase_mma`（\(k \bmod 2\)）。核对代码改法：翻转条件应改为 `if stage == 2`。
   - **SMEM 估算**：用书中公式（[chapter_gemm_advanced/index.md:317-323](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L317-L323)）计算每级 stage 的字节数：

     \[ \text{每级 stage} = (128 \times 64 + 128 \times 64) \times 2\ \text{B} = 32768\ \text{B} = 32\ \text{KB} \]

     加上 `Dsmem` 的 32 KB 与 `pool.move_base_to(1024)` 预留的约 1 KB 控制区（[chapter_gemm_async/index.md:347](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L347)），分别算 `PIPE_DEPTH=2/3/6` 的总需求，与 B200 每 SM 228 KB（[chapter_background/index.md:76](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_background/index.md#L76)）对比。
   - **奇偶检查**：对完整规模 `K_TILES=64`，算 `64 % (2*3)`，判断该配置若放进 Step 6 式持久内核是否违反相位复位约束。
3. **需要观察的现象**：`phase_tma` 变成每 3 次迭代才翻转；SMEM 需求随深度线性增长；奇偶约束在 `PIPE_DEPTH=3` 时失效。
4. **预期结果**：
   - 相位表：k=0,1,2 的 `phase_tma=0`；k=3,4,5 为 1；k=6,7 回到 0。翻转发生在 k=2 与 k=5 迭代末（stage==2）。
   - SMEM：depth=2 约 \(2\times32+32=96\) KB；depth=3 约 \(3\times32+32=128\) KB，**增量 +32 KB，远低于 228 KB 上限（约 56%）**，单看容量可行；depth=6 约 \(6\times32+32=224\) KB，几乎耗尽每 SM 容量（[chapter_gemm_advanced/index.md:323](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L323)）。更精确地说，单个 CTA 可用的动态共享内存上限是 213.28 KB（[appendix/benchmarking_gpu_kernels.md:945-951](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L945-L951)），depth=3 的 128 KB 同样低于该值。
   - 奇偶：\(64 \bmod 6 = 4 \ne 0\)，每 stage 屏障被用 \(64/3\) 次（stage 0 为 22 次、stage 1/2 各 21 次），21 次为奇数 → 屏障终止奇偶为 1，不能在下一 tile 开始时把局部相位重置为 0；Step 6 的断言会直接拒绝该组合。depth=3 在 Step 5（一 CTA 一 tile）中可运行，但在持久内核中需要携带相位或改用满足整除关系的 `K_TILES`。以上推演均为纸面结论，实际运行需 Blackwell GPU（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `if stage == PIPE_DEPTH - 1: phase_tma ^= 1` 误写成每迭代都翻（即与 `phase_mma` 同节奏），内核会发生什么？

**答案**：以 k=2 为例，正确 `phase_tma` 应为 1，误写后在 k=1 迭代末已翻成 1、k=2 迭代 try_wait 用 0 等待——而 stage 0 屏障此刻正处于相位 0（它的第 0 相早在 k=0 就完成翻到 1，要等 k=2 的这次使用完成才回到 0）。于是 `try_wait(0)` 面对的是一个已经离开相位 0 的屏障，**立即通过**，MMA 读到的是 stage 0 里尚未更新的旧 tile，内核静默产出错误结果；在另一些时序下则可能等一个永远不来的相位而挂死。这正是 u8-l2 总结的两类故障。

**练习 2**：为什么 `phase_tma` 的翻转条件是「stage 到达环尾」而不是「每完成一次 TMA 传输」？

**答案**：`phase_tma` 是一个**软件变量**，被两道屏障（stage 0 与 stage 1）交替使用；它必须等于「软件在环上走过的圈数」才能同时与两道屏障各自的硬件奇偶对齐（每道屏障每圈恰好被用一次）。如果按「传输完成次数」翻，两次完成对应两道不同屏障，一个变量无法同时匹配两个互不相关的翻转序列。

**练习 3**：完整规模 `K_TILES=64`、`PIPE_DEPTH=2` 时，内核结束时 `mma_bar` 与两道 `tma_bar` 各处于什么相位？

**答案**：`mma_bar` 完成 64 相（偶数），终止相位奇偶回到 0；每道 `tma_bar` 各完成 32 相（偶数），也回到 0。三个全部归零——这就是 Step 6 敢在每 tile 开始处把 `phase_tma`/`phase_mma` 重置为 0 的依据（[chapter_gemm_async/index.md:492-499](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L492-L499)）。

## 5. 综合实践

把本讲三个模块串成一个任务：**为 `hgemm_v5` 写一份「PIPE_DEPTH=2 流水线档案」，再评估 depth=3 方案**。具体交付四件东西：

1. **章末练习 2 的完整解答**（[chapter_gemm_async/index.md:682](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L682)）：从等待粒度、字节账本、相位节奏三个角度论证 per-stage TMA 屏障的必要性（参考 4.2.5 练习 1 的答案组织你自己的版本，要求能向别人讲明白）。
2. **所有权表**（`PIPE_DEPTH=2`、`K_TILES=5`）：合并 4.2.2 的 stage 时间线与 4.3.2 的相位表，做成一张每迭代一行的总表，六列：`k`、`stage`、消费的 tile、新装入的 tile 与目的 stage、`try_wait` 用的两个相位、本迭代末的两个相位值。用 4.2.4 的示例脚本核对「tile → stage」两列。
3. **depth=3 评估**：SMEM 需求 128 KB（+32 KB，低于 228 KB 与 213.28 KB 两个上限，容量可行）；翻转条件改为 `stage == 2`；但 \(64 \bmod 6 \ne 0\)，各 stage 屏障每 tile 使用 22/21/21 次，奇数次使终止奇偶为 1，无法在持久内核中按 Step 6 的方式复位相位——结论：**depth=3 对 Step 5 可行、对 Step 6 需改参数或携带相位**。把三个 depth（2/3/6）的 SMEM 对比写成一页备忘。
4. **（可选，需 Blackwell GPU）**：把 `hgemm_v5` 抄入 `.py` 文件，按 u9-l2 的流程用 `tvm.compile(tir_pipeline="tirx")` 编译、用 PyTorch fp32 参考断言数值；然后把 `PIPE_DEPTH` 改为 3 重跑，验证你的推演（**待本地验证**）。无 GPU 时以第 1–3 项的纸面推演为完成标准。

## 6. 本讲小结

- Step 4 无法重叠的根源是**存储冲突**：SMEM 只有一对操作数 tile，下一次 load 没有独立目的地；Step 5 用 `PIPE_DEPTH=2` 的 SMEM 环解除它——layout 变了，scope 与 dispatch 不变。
- 流水线协议三段式：启动 prefetch 装满两级 stage → 主循环消费 `stage = k % PIPE_DEPTH` → 把 `k + PIPE_DEPTH` 的 tile 装回刚释放的 stage；尾部由 `next_k < K_TILES` 条件自然排空。
- **数据、账本、相位在 stage 粒度对齐**：每个 stage 一道 `tma_bar`（字节记账 + 独立等待），MMA 共用一道 `mma_bar`（同一 TMEM 累加器、单一时间线）。
- 两个相位变量速率不同：`phase_mma` 每迭代翻一次，`phase_tma` 扫完一圈（`stage == PIPE_DEPTH-1`）才翻一次；公式 `stage = k mod S`、`phase = ⌊k/S⌋ mod 2`。漏翻 → 静默读旧数据；乱翻 → 挂死。
- 本步仍无显式 empty 屏障：单 warpgroup 里 `try_wait(mma_bar)` 兼任「缓冲归还」信号；这也是 load 与 MMA 尚未真正并行的原因，Step 7 拆角色后必须补上 empty 方向。
- SMEM 成本公式：\(S \times 32\ \text{KB} + 32\ \text{KB}\)（S 为 `PIPE_DEPTH`）；B200 每 SM 228 KB、每 block 动态上限 213.28 KB，depth=3 的 128 KB 可行，depth=6 的 224 KB 几乎耗尽。

## 7. 下一步学习建议

- **下一讲 u12-l3（Step 6）**：把本讲的分 stage K 循环套进外层输出 tile 循环，引入持久内核与 tile scheduler。重点留意它如何处理本讲 4.3.3 的约束——每 tile 屏障使用次数为偶数才能把 `phase_tma`/`phase_mma` 复位为 0，以及 `K_TILES % (2*PIPE_DEPTH) == 0` 断言的由来。
- **u13-l1（Step 7）**：把 TMA 与 MMA 拆给不同 warp 角色、补上 empty 屏障，真正达到 `img/pipe_depth2.png` 画的目标时序——届时回看本讲「4.1.4 串行点」，那条等待会被消灭。
- **延伸阅读**：回读 [chapter_async_barriers/index.md:53-116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L53-L116)，把 full/empty 双屏障协议与 Step 7 的四道屏障对照，理解「为什么角色一拆分，empty 方向就必须显式化」。
