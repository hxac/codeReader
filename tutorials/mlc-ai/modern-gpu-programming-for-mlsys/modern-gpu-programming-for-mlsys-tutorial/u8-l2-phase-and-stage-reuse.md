# phase 相位与多级流水线的 stage 复用

## 1. 本讲目标

上一讲（u8-l1）我们弄清了**一道** mbarrier 的状态机：它把线程到达、硬件完成通知和 TMA 在途字节合并在一个对象里，用 `arrive` 与 `wait` 的分离解耦生产者和消费者。本讲回答随之而来的两个新问题：

1. **一道屏障怎么被反复使用？** K 循环每轮都要交接数据，不可能每轮都新分配一道屏障。答案是屏障每完成一轮就自动翻入下一个**相位（phase）**，软件只跟踪相位的奇偶（parity）就能区分"这是第 i 轮的完成，还是第 i-1 轮的完成"。
2. **多级流水线的缓冲怎么安全复用？** 双缓冲/多级 SMEM 环里，每个 stage 需要 `full`（数据就绪）和 `empty`（缓冲归还）**两道**屏障，而且两道屏障的相位要**分开**跟踪。

学完本讲你应当能够：

- 解释 phase 如何区分同一道 barrier 的先后两次使用，并写出 `try_wait` 的奇偶语义。
- 画出 full/empty 双屏障构成的 stage 复用环，标出数据流向与缓冲所有权的变化。
- 手推 depth=2 流水线连续 6 次迭代中每道屏障的相位翻转序列，验证奇偶公式，并推演"漏掉一次异或翻转"后的两种故障（静默错果与死锁）。

## 2. 前置知识

本讲直接建立在以下已建立的概念之上（来自 u8-l1 与单元二、六）：

- **mbarrier 的四个内部量**：phase parity（当前相位奇偶）、pending arrival count（本相还差的到达数）、expected arrival count（每相要求的到达总数，由 `init` 设定）、tx-count（在途传输字节数）。当前相完成的充要条件是 pending 与 tx-count 同时归零。
- **三条到达路径**：普通线程 `mbarrier.arrive`；发起线程 `arrive.expect_tx(bytes)`（到达一次并登记在途字节）；`tcgen05.commit`（硬件在异步 MMA 完成后补一次到达）。
- **「已发起」不等于「已完成」**：`cta_sync` 只能同步线程，观察不到 TMA 引擎和 Tensor Core 的进度，异步交接必须依赖显式完成信号。
- **双缓冲的动机**（u2-l3、Step 5）：只有给 load 和 compute 各自独立的 SMEM slot，两条路径才可能重叠；多个 slot 组成一个环形缓冲（ring），逐个 stage 轮转使用。

一个形象的说法：屏障的相位就像医院叫号系统的"当前服务的轮次编号取最低位"。如果只记"叫过号"而不记"叫到第几轮"，你会把上一轮的铃声当成这一轮的铃声——数据交接中这就是灾难。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [chapter_async_barriers/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md) | 本讲主教材：phase 机制、双 stage 示例的四迭代相位表、full/empty 屏障与 stage 复用协议 |
| [_extra/demo/mbarrier_mechanism.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/mbarrier_mechanism.html) | 交互演示：mbarrier 对象的四个字段、相位完成条件、五条核心指令 API 表 |
| [_extra/demo/phase_tracking.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/phase_tracking.html) | 交互演示：同一道屏障在 8 次迭代中相位 0↔1 交替，可点选/动画播放 |
| [chapter_gemm_basics/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md) | GEMM Step 2 内核：单道 `mma_bar` 每轮翻转的最小真实实例 |
| [chapter_gemm_async/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md) | GEMM Step 5 内核：`PIPE_DEPTH=2` 的 SMEM 环 + 每阶段一道 `tma_bar`，含 `phase_tma` 的翻转位置 |
| [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) | GEMM Step 7 内核：full/empty 四道屏障、`mma2tma` 跳过一个 stage 的环序、`PipelineState` 与初始相位不对称 |

本仓库是教材仓库（Sphinx 书站），没有可执行的测试目录；"源码"即书中正文、内嵌内核代码与交互演示资产。书站在线阅读方式见 u1-l2，本地构建后可在 `_build/html` 中打开对应页面操作演示。

## 4. 核心概念与源码讲解

### 4.1 phase 相位翻转：让一道屏障服务无数轮

#### 4.1.1 概念说明

同一道 mbarrier 可以被复用，每一轮称为一个**相位（phase）**。一相完成（所有期望到达与关联传输都结束）后，屏障**原子地**自动进入下一相：parity 在 0 与 1 之间翻转，pending arrival count 从 expected 重装。硬件不需要、也不允许软件每轮重新 `init`——重新初始化反而会与正在等待的线程竞争。

如果消费者只记录"这道屏障曾经完成过"，它就可能把**上一轮的完成**误认为**本轮数据已就绪**。parity 以 0/1 交替，恰好提供了区分两次相邻完成的最低成本记号：软件只需维护一个本地比特。

一个必须先讲清楚的语义细节：`try_wait(bar, P)` 等的不是"屏障当前处于相位 P"，而是**"屏障离开相位 P"**——即"奇偶为 P 的那一轮已经完成"。操作上可以记为：

\[ \text{try\_wait}(P)\ \text{阻塞当且仅当}\ \text{current\_parity} = P \]

由此推出一个后面反复用到的事实：若调用 `try_wait(bar, 1)` 时屏障正处在相位 0，它**立即返回**（因为对它而言"最近的相位 1 一轮"已经结束）；若调用 `try_wait(bar, 0)` 时屏障处在相位 0，它会**阻塞**到这一轮完成为止。生产者/消费者正是利用这个不对称让流水线的两端有相反的起始行为（见 4.3）。

#### 4.1.2 核心流程

单道屏障被每轮迭代复用时的状态循环：

```text
init(expected = E)
   │  parity = 0, pending = E, tx = 0
   ▼
┌──────────────── 本相进行中 ────────────────┐
│ arrive / expect_tx / complete-tx 不断核减   │
└──────────────────┬─────────────────────────┘
                   │ pending == 0 且 tx-count == 0
                   ▼
        原子翻入下一相：parity 0↔1，pending ← E
                   │
                   ▼
       消费者 try_wait(旧 parity) 的等待解除
```

单道屏障每轮用一次时，软件相位变量**每轮**翻转一次：

\[ \text{phase}(k) = k \bmod 2 \]

#### 4.1.3 源码精读

主教材首先点明复用与奇偶跟踪这件事（概览第 3 条）：

- [chapter_async_barriers/index.md:L7-L9](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L7-L9)：本讲的总纲——多级流水线用 `full` 屏障表示 stage 就绪、`empty` 屏障归还缓冲，**屏障经相位复用，内核必须跟踪相位奇偶来区分先后使用**。
- [chapter_async_barriers/index.md:L53-L55](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L53-L55)：同一条 mbarrier 可复用、每轮叫一个 phase；屏障完成一相后自动进入下一相。原文明确指出：若消费者只记录"完成过"，就可能把上一轮的完成误当本轮就绪——parity 交替让消费者能认出自己要的那一次完成。
- [chapter_async_barriers/index.md:L36-L41](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L36-L41)：相位完成的两个条件（pending==0 且 tx==0）以及完成后 parity 在 0/1 间翻转——这是复用机制的硬件根基。

交互演示把这套状态画成了可点击的卡片：

- [_extra/demo/mbarrier_mechanism.html:L119-L123](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/mbarrier_mechanism.html#L119-L123)：mbarrier 对象（SMEM 中的 64 位值）的四个槽位：phase parity、pending arrival count、expected arrival count、tx-count。
- [_extra/demo/mbarrier_mechanism.html:L144-L152](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/mbarrier_mechanism.html#L144-L152)：相位完成条件卡片。注意其中一句关键描述：屏障**原子地**进入下一相并从 expected 重装 pending——"原子"意味着完成事件与翻相不会被打断成两半，`wait` 不会观察到中间态。
- [_extra/demo/mbarrier_mechanism.html:L184-L187](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/mbarrier_mechanism.html#L184-L187)：API 表中 `T.ptx.mbarrier.try_wait` 一行：**不修改屏障状态**，内部重试直到请求的相位完成。等待是纯观察，这是生产者可以继续干活的前提。
- [_extra/demo/phase_tracking.html:L122](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/phase_tracking.html#L122)：演示的机制说明——每个屏障有一个 phase bit，所有期望到达与被跟踪的异步传输完成后屏障前进并复位，**无需重新初始化**。
- [_extra/demo/phase_tracking.html:L107-L116](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/phase_tracking.html#L107-L116)：8 个迭代块交替着 `ph0`/`ph1` 样式；脚本在 [_extra/demo/phase_tracking.html:L163-L165](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/phase_tracking.html#L163-L165) 用 `var ph = s % 2` 计算标签——演示本身就在展示 parity 公式。
- [_extra/demo/phase_tracking.html:L134-L141](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/phase_tracking.html#L134-L141)：演示用一句话点破 phase 解决的问题——**"这道屏障是为第 i 次迭代触发的，还是第 i-1 次？"**，并给出最小循环骨架：`try_wait(tma_bar, phase_tma)` 后 `phase_tma ^= 1`。

真实内核中的最小实例是 GEMM Step 2：K 循环每轮复用同一道 `mma_bar`，

- [chapter_gemm_basics/index.md:L346](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L346)：正文警告——每次迭代复用同一道 `mma_bar`，屏障每次完成后进入新相位，`phase_mma` 标识当前等待的是哪一次迭代；**跟踪错误时，wait 会把上一迭代的完成误当本次 MMA，静默污染结果**。
- [chapter_gemm_basics/index.md:L360-L374](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L360-L374)：书中给出的相位表（迭代 0 等相位 0、完成后屏障到 1；迭代 1 等相位 1、完成后回 0……），以及 `try_wait(bar, phase_mma)` 的语义（**屏障离开指定相位后返回**）和每次成功等待后的 `phase_mma ^= 1`。
- [chapter_gemm_basics/index.md:L438](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L438) 与 [L457-L459](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L457-L459)：内核源码——`phase_mma: T.int32 = 0` 初始化，K 循环内 `T.ptx.mbarrier.try_wait(mma_bar.ptr_to([0]), phase_mma)` 后紧跟 `phase_mma ^= 1`。三行就是单道屏障复用的全部软件开销。

#### 4.1.4 代码实践

**实践目标**：把"相位每轮翻转"内化成可直接写出的表。

**操作步骤**：

1. 打开书站异步屏障章节（或本地构建后的页面），找到 phase_tracking 演示，点击 Iter 0 到 Iter 7 的每个方块，观察标签 `phase = s % 2` 的交替；再点 "▶ Animate" 看连续播放。
2. 打开 [chapter_gemm_basics/index.md:L362-L367](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L362-L367) 的相位表，把它向 k=4 延拓一行（自己动笔）。
3. 对照内核代码 [chapter_gemm_basics/index.md:L457-L459](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L457-L459)，确认表中的"等待相位"就是传给 `try_wait` 的第二个实参。

**需要观察的现象**：parity 严格按 0,1,0,1,… 交替；屏障完成后的状态恰好等于下一轮要等待的 parity——所以"翻转"不是可选的修饰，而是让本地变量与硬件状态保持同步的唯一手段。

**预期结果**：延拓后的 Step 2 表为：

| K 迭代 k | 传入 try_wait 的 phase_mma | MMA 完成后屏障相位 |
|---:|---:|---:|
| 0 | 0 | 1 |
| 1 | 1 | 0 |
| 2 | 0 | 1 |
| 3 | 1 | 0 |
| 4 | 0 | 1 |

本表由书中表格按 `phase(k) = k mod 2` 规律手推延拓（待本地验证：无 GPU 也可在纸上或用 4.3 节的模拟脚本复核）。

#### 4.1.5 小练习与答案

**练习 1**：一相完成后，为什么软件不能重新调用 `mbarrier.init` 来"复位"屏障？

**答案**：相位完成时硬件已经**原子地**翻入下一相并从 expected 重装 pending（见 mbarrier_mechanism 演示的完成条件卡片说明），屏障天然就绪可复用、无需重init；相反，其他线程可能正 `wait` 在旧相位上，重新 init 会改写 expected/parity，与等待中的观察者竞争，行为不可预测。

**练习 2**：屏障当前 parity 为 1 时调用 `try_wait(bar, 0)` 会怎样？parity 为 1 时调用 `try_wait(bar, 1)` 又会怎样？

**答案**：前者立即返回——奇偶为 0 的那一轮已经结束（屏障已离开相位 0）；后者阻塞到当前相完成为止。记住判据：`try_wait(P)` 阻塞当且仅当当前 parity 等于 P。

**练习 3**：Step 2 中 `phase_mma` 每轮都翻，4.3 节将看到 Step 5 的 `phase_tma` 每两轮才翻一次。直觉上差异来自哪里？

**答案**：Step 2 只有**一道**屏障，每轮迭代都开始它的新一轮，故 parity 每轮翻；Step 5 给**每个 stage 各配一道**屏障，某道屏障只有在环形缓冲转回到该 stage 时才进入新一轮，故全局相位变量要走完一圈（访问完所有 stage）才翻一次。

### 4.2 full/empty 双屏障：stage 复用环的所有权交接

#### 4.2.1 概念说明

一道屏障只能表达一个方向的意义："某事完成了"。但一个 SMEM stage 在流水线里需要**两个**方向的信息：

- **`full[stage]`（满）**：生产者说"数据已经装好"——把**数据**从生产者交給消费者。
- **`empty[stage]`（空）**：消费者说"我用完了"——把**缓冲**的所有权归还给生产者。

只有 full 的流水线里，生产者不知道何时可以覆写缓冲；只有 empty 的流水线里，消费者不知道数据是否到达。两者合起来，一个 stage 才构成完整的**复用环**：

```text
            empty[s]（缓冲归还：消费者 → 生产者）
   ┌──────────────────────────────────────────────┐
   │                                              ▼
[ EMPTY 状态 ]                          生产者 wait(empty[s]) 后
   缓冲可覆写                             发起 TMA load 覆写 stage s
   ▲                                              │
   │                                              ▼
消费者读完，arrive(empty[s])              [ FULL 状态 ] 数据就绪
   │                                              │
   │                                    消费者 wait(full[s]) 后消费
   └──────────────────────────────────────────────┘
            full[s]（数据就绪：生产者 → 消费者）
```

缓冲所有权沿 `生产者 → 数据 → 消费者 → 缓冲 → 生产者` 循环。每道屏障各自的 expected arrival count 取决于有多少线程/引擎报告完成（例如 writeback 的 128 个线程每人都 arrive 一次）；复用同一对屏障时，**full 与 empty 的相位必须分别跟踪**——它们由不同角色翻转、翻转时机也不同。

读流水线内核的通用方法：先认清**生产者、消费者、被交接的资源**三件事，再把每条 wait/arrive 绑定到具体事件（数据就绪 / 结果可读 / 缓冲可复用）。

#### 4.2.2 核心流程

以"生产者=TMA 加载、消费者=MMA"为例，稳态下每个角色的每轮迭代：

```text
生产者（对 stage s = k mod S）：                消费者（对同一 k）：
1. wait(empty[s], phase_empty)                 1. wait(full[s], phase_full)
   # 等缓冲归还（首轮靠初始相位放行）              # 等数据就绪
2. 发起 TMA load → stage s                     2. 消费 stage s（MMA 读 SMEM）
3. expect_tx(full[s], bytes)                   3. arrive(empty[s])
   # 到达一次并登记在途字节                        # 归还缓冲
4. if s == S-1: phase_empty ^= 1               4. if s == S-1: phase_full ^= 1
```

深度为 \(S\) 的环里，同一道屏障相邻两次使用之间隔着 \(S\) 次迭代，因此全局相位变量的翻转频率是单屏障情形的 \(1/S\)。

#### 4.2.3 源码精读

- [chapter_async_barriers/index.md:L110-L118](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L110-L118)：full/empty 协议的定义段——屏障既可以表示"数据就绪"也可以表示"缓冲不再使用"，流水线内核因此给每个 SMEM stage 配一对屏障：`full[stage]` 表示 TMA 已装满，`empty[stage]` 表示消费者已用完；full 把数据交给消费者、empty 把缓冲还给生产者；复用时**两者的相位奇偶须分开跟踪**；末尾给出三问法（生产者？消费者？交接的资源？）。
- [chapter_async_barriers/index.md:L114](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L114)：书中配图 `mbarrier_stage_reuse_v2.svg` 直观画出单个 stage 的复用环（本讲的文字环图即按它整理）。
- [chapter_gemm_advanced/index.md:L41-L47](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L41-L47)：真实内核（Step 7）中的同一协议——`tma2mma` 即 full（"SMEM 数据就绪"），`mma2tma` 即 empty（"SMEM 缓冲可复用"）。注意其中的环序细节：**`mma2tma` 的信号要跳过一个 stage**——MMA k=0 读完 stage 0 后，下一个需要这个槽位的是 TMA Load k=2 而不是 k=1，所以这次归还释放给的是两步之后的加载。
- [chapter_gemm_advanced/index.md:L60-L67](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L60-L67)：完整流水线把双向交接扩展成**四道**屏障：正向 `tma2mma`、`mma2ld` 报告数据/结果就绪，反向 `mma2tma`、`ld2mma` 归还 SMEM 与 TMEM。命名法 `source2destination` 直接标注了信号流向。
- [chapter_gemm_advanced/index.md:L69-L71](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L69-L71)：屏障类型取决于生产者如何报告完成——TMA 装载用带字节计数的 `TMABar`，MMA 用 `TCGen05Bar`（`tcgen05.commit` 完成后更新），线程直接 arrive 用 `MBarrier`。full/empty 是**协议角色**，不是新的硬件类型。

#### 4.2.4 代码实践

**实践目标**：用三问法把 Step 7 的四道屏障逐一刻成表。

**操作步骤**：

1. 通读 [chapter_gemm_advanced/index.md:L60-L67](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L60-L67) 的四屏障表。
2. 对每道屏障回答四列：**生产者（谁 arrive）**、**消费者（谁 wait）**、**交接的资源**、**屏障类型**。
3. 对照 [chapter_gemm_advanced/index.md:L111-L113](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L111-L113)（writeback 侧对 `mma2ld` 的 wait 与对 `ld2mma` 的 arrive）核对答案。

**需要观察的现象**：`mma2tma` 的箭头在书的时间线图上不指向紧邻的下一格，而是指向两步之后的 TMA Load——这就是"跳过一个 stage"的归还次序。

**预期结果**（可直接核对）：

| 屏障 | 角色 | 生产者（arrive） | 消费者（wait） | 交接资源 | 类型 |
|---|---|---|---|---|---|
| tma2mma | full | TMA 引擎（expect_tx + complete-tx） | MMA warp | SMEM stage 的**数据** | TMABar |
| mma2tma | empty | tcgen05.commit（MMA 完成后） | TMA producer warp | SMEM stage 的**缓冲** | TCGen05Bar |
| mma2ld | full | tcgen05.commit | writeback warpgroup | TMEM 累加器**结果** | TCGen05Bar |
| ld2mma | empty | 128 个 writeback 线程各 arrive 一次 | MMA 侧 | TMEM 区域的**使用权** | MBarrier |

#### 4.2.5 小练习与答案

**练习 1**：只有 full 没有 empty 的流水线会发生什么？

**答案**：消费者知道数据何时就绪，但生产者不知道缓冲何时可以覆写。要么生产者盲目覆写、破坏尚未消费的数据（错果），要么生产者必须额外串行等待消费者（退化为不重叠，Step 5 之前正是靠"等 MMA 完成再发起下一次加载"隐式地借用完成信号充当 empty）。

**练习 2**：为什么 `mma2tma`（empty）的信号要"跳过一个 stage"，而不是交给紧邻的下一次加载？

**答案**：环序使然（[chapter_gemm_advanced/index.md:L46](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L46)）：`PIPE_DEPTH=2` 时 TMA Load k=0 占 stage 0、k=1 占 stage 1；MMA k=0 释放的 stage 0，下一个使用者是 TMA Load k=2（k=1 有自己的 stage 1）。归还的对象是"槽位"，不是"序号相邻的加载"。

**练习 3**：full 与 empty 的相位为什么必须分别跟踪，而不能共用一个 `phase` 变量？

**答案**：它们由不同角色在不同事件上翻转：full 的相位随"数据装好"前进，empty 的相位随"缓冲归还"前进；两者的初始奇偶还刻意相反（见 4.3）。共用一个变量必然在某一侧错相——要么提前放行读到旧数据，要么死锁。

### 4.3 stage 复用环：depth=2 六次迭代的相位手推

#### 4.3.1 概念说明

多级流水线给每个 stage 配独立屏障后，出现了一个容易搞混的"两个 4"式问题：**每道屏障自己的相位**（每被完整使用一轮就翻一次）与**软件的全局相位变量**（跟踪"现在是第几圈扫过这个环"）不是一回事。设深度为 \(S\)、迭代号为 \(k\)：

\[ \text{stage}(k) = k \bmod S, \qquad \text{phase}(k) = \left\lfloor \frac{k}{S} \right\rfloor \bmod 2 \]

单屏障（\(S=1\)）时退化为 \(k \bmod 2\)，正是 4.1 的结论。全局变量只需在**访问完环上最后一个 stage 时**翻转一次：

```text
if stage == S - 1:
    phase ^= 1
```

另一个关键设计是**初始相位不对称**：缓冲一开始是空的、合法可写，所以生产者第一次 `wait(empty, 1)` 应当立即通过（屏障初始 parity 为 0，\(1 \ne 0\)，按 `try_wait` 语义即刻返回）；而数据一开始并不存在，消费者第一次 `wait(full, 0)` 必须阻塞。生产者从 1、消费者从 0 起步，两边在相同的位置（`stage == S-1`）翻转，奇偶永远互补。

`phase_tma`/`phase_full` 这类全局变量描述的是**软件**扫过环形缓冲的进度，它不假设两次硬件传输按什么顺序完成——屏障只保证"你等待的那一轮已完成"，与到达顺序无关。

最后，真实内核里 stage 与 phase 这对值由 `PipelineState` 捆绑维护：手工分开维护极易出 off-by-one 并导致死锁。

#### 4.3.2 核心流程

depth=2（双缓冲）、生产者=TMA、消费者=MMA 的六次迭代全景。初始：`full[0]=full[1]=empty[0]=empty[1]` 的 parity 均为 0；消费者 `phase_full=0`，生产者 `phase_empty=1`。

```text
k : 动作序列（P=生产者, C=消费者, s=k%2）
0 : P.wait(empty[0],1)→立即过 | P.load0 | C.wait(full[0],0)→阻塞到load0完成
    C.mma0 | C.arrive(empty[0])
1 : P.wait(empty[1],1)→立即过 | P.load1 | C.wait(full[1],0) | C.mma1 | C.arrive(empty[1])
    ★ s==1：phase_full 0→1，phase_empty 1→0
2 : P.wait(empty[0],0) | P.load2 | C.wait(full[0],1) | C.mma2 | C.arrive(empty[0])
3 : P.wait(empty[1],0) | P.load3 | C.wait(full[1],1) | C.mma3 | C.arrive(empty[1])
    ★ s==1：phase_full 1→0，phase_empty 0→1
4 : P.wait(empty[0],1) | P.load4 | C.wait(full[0],0) | C.mma4 | C.arrive(empty[0])
5 : P.wait(empty[1],1) | P.load5 | C.wait(full[1],0) | C.mma5 | C.arrive(empty[1])
    ★ s==1：phase_full 0→1，phase_empty 1→0
```

推演要点（用 `try_wait(P)` 阻塞当且仅当当前 parity==P 逐条核对）：

- k=0 的两个"立即过"来自初始不对称（empty[0] 在 parity 0，等 1 即过；随后消费者归还使其翻到 1）。
- k=2 时 `empty[0]` 的 parity 已是 1，生产者等 0，立即通过——它等待的"parity 为 0 的那轮"（由消费者 k=0 的 arrive 完成）确实已结束，缓冲已归还。
- k=2 时 `full[0]` 的 parity 在 TMA load2 完成前是 1，消费者等 1 → 阻塞，直到 load2 把相位 1 那轮完成、翻回 0 才放行。**屏障每被使用一轮，下一次等待的奇偶就换一次**。

#### 4.3.3 源码精读

- [chapter_async_barriers/index.md:L65-L67](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L65-L67)：双 stage 流水线的设定——两个 SMEM 缓冲各配一道 TMA 屏障，使两个 stage 可以**独立**等待；环上固定轮转时，一个 `phase_tma` 就能表示当前扫过环形缓冲的进度，**访问完两个 stage 后才翻转**。
- [chapter_async_barriers/index.md:L69-L75](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L69-L75)：官方伪代码——`stage = iteration % 2`，`try_wait(tma_bar.ptr_to([stage]), phase_tma)`，`if stage == 1: phase_tma ^= 1`。
- [chapter_async_barriers/index.md:L77-L88](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L77-L88)：前四次迭代的官方演化表（等待相位 0,0,1,1；屏障完成后的奇偶 1,1,0,0；`phase_tma` 迭代后取值 0,1,1,0）及逐行解释——迭代 0 完成 stage 0 的相位 0，但一圈没扫完，`phase_tma` 不动；两次都访问过后才翻 1；迭代 2 回到 stage 0 改等相位 1。本讲 4.3.2 的六行表正是这张表的 full/empty 双屏障延拓。
- [chapter_async_barriers/index.md:L90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L90)：一般化结论——`phase_tma` 描述软件扫环的进度，**不假设两次 TMA 传输的硬件完成顺序**；深度为 \(S\) 的流水线典型做法是每 stage 一道 TMA 完成（full）屏障 + 相位奇偶区分先后；完整的缓冲复用协议再加上 empty 屏障。
- [chapter_gemm_async/index.md:L260-L265](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L260-L265)：Step 5 相对 Step 4 的四处改动，其中两处直接对应本讲——缓冲加 `PIPE_DEPTH` 前导维、`tma_bar` 变成**每 stage 一道**的数组；主循环用 `stage = k % PIPE_DEPTH` 等待/计算/复用。
- [chapter_gemm_async/index.md:L285-L291](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L285-L291)：相位管理的正文明示**两个本地相位变量推进速率不同**——`phase_mma` 跟踪单道累加器屏障、每轮翻；TMA 的屏障每 stage 一道、只在环回到该 stage 时开始新一轮，故 `phase_tma` 在 `stage == PIPE_DEPTH-1` 时才翻（L289-L291 的代码）。
- [chapter_gemm_async/index.md:L345-L346](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L345-L346) 与 [L373-L374](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L373-L374)：内核里的实体——`tma_bar = pool.alloc((PIPE_DEPTH,), "uint64")` 与 `phase_tma/phase_mma: T.int32 = 0` 两个本地变量。
- [chapter_gemm_async/index.md:L405-L427](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L405-L427)：主循环全文——L410 `try_wait(tma_bar[stage], phase_tma)`，L416-L417 等 MMA 并 `phase_mma ^= 1`，L420-L423 把刚释放的 stage 用于 `k+PIPE_DEPTH` 的预取（这一步就是隐式的"缓冲归还"），L426-L427 `if stage == PIPE_DEPTH - 1: phase_tma ^= 1`。注意此内核没有独立的 empty 屏障：它用"先等 MMA 完成再覆写"把归还语义折叠进了同一个循环（对比 4.2 的双屏障协议）。
- [chapter_gemm_async/index.md:L293](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L293)：书中留下的"Trace the pipeline"练习（PIPE_DEPTH=2、K_TILES=5，记录每 k 的 stage/两个相位/是否预取，问 `phase_tma` 在哪翻、最后两轮为何不预取）——本讲实践是它的加强版。
- [chapter_gemm_advanced/index.md:L73-L82](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L73-L82)：`PipelineState`——四道屏障只回答"缓冲可用吗"，`PipelineState` 同时记录**当前 stage 与应等待的相位**；手工分开维护极易出 off-by-one 死锁，所以把两者捆绑推进（`advance()`）。
- [chapter_gemm_advanced/index.md:L84-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L84-L89)：初始相位不对称的原文——生产者 `phase=1` 首个 wait 立即通过（缓冲生来为空、可以开始填）；消费者 `phase=0` 首个 wait 阻塞（数据要等第一次装载）；**两端若用相同初始相位，内核可能死锁、也可能在数据就绪前继续执行**。

#### 4.3.4 代码实践

**实践目标**：亲手完成本讲的核心推导——depth=2 流水线连续 6 次迭代中每道 full/empty 屏障的相位翻转序列，验证奇偶公式；再推演漏掉一次异或翻转的后果。

**操作步骤**：

1. 先自己填表（盖住答案）：六次迭代 k=0..5，对每个 k 记录 5 列——`stage`、消费者 `wait(full[s], phase_full)` 的 `phase_full`、TMA 完成后 `full[s]` 的 parity、生产者 `wait(empty[s], phase_empty)` 的 `phase_empty`、消费者 arrive 后 `empty[s]` 的 parity。初始条件：四道屏障 parity 全 0，`phase_full=0`，`phase_empty=1`，每轮顺序为 P.wait → P.load → C.wait → C.arrive，`stage==1` 时两个软件相位各翻一次。
2. 用两条规则核对每一行：屏障完成一轮就翻一次 parity；`try_wait(P)` 阻塞当且仅当当前 parity==P（所以表中每个"等待奇偶"在等待发起时刻必须与屏障当时的状态一致或可被该轮完成解锁）。
3. 验证公式：把 k=0..5 代入 \(\text{phase}(k)=\lfloor k/2 \rfloor \bmod 2\)，应与表中 `phase_full` 列一致；`phase_empty` 应与之互补。
4. 故障推演（两个分支分别做）：
   a. 假设消费者**忘记**在 k=1 末尾翻转 `phase_full`（此后一直停在 0）。逐格推 k=2：`full[0]` 此时 parity 为 1，`wait(full[0], 0)` 按语义立即返回——消费者会怎样？
   b. 假设生产者**忘记**翻转 `phase_empty`（停在 1）。逐格推 k=2：`empty[0]` parity 为 1，`wait(empty[0], 1)` 会怎样？顺着"谁在等谁"画出依赖环。

**需要观察的现象**：正常序列中，同一道 stage 屏障相邻两次使用的等待奇偶恰好相反（stage 0 的三次消费分别等 0、1、0）；`phase_full` 呈 0,0,1,1,0,0 的周期 4 模式——每扫完一圈翻一次。

**预期结果**（手推答案表；本书为文档仓库、无 GPU 亦可完成，脚本复核见第 5 节，运行输出待本地验证）：

| k | stage | C 等待 `full[s]` 的 `phase_full` | TMA 完成后 `full[s]` | P 等待 `empty[s]` 的 `phase_empty` | C 归还后 `empty[s]` |
|---:|---:|---:|---:|---:|---:|
| 0 | 0 | 0 | 0→1 | 1（初始，立即通过） | 0→1 |
| 1 | 1 | 0 | 0→1 | 1（初始，立即通过） | 0→1 |
| 2 | 0 | 1 | 1→0 | 0 | 1→0 |
| 3 | 1 | 1 | 1→0 | 0 | 1→0 |
| 4 | 0 | 0 | 0→1 | 1 | 0→1 |
| 5 | 1 | 0 | 0→1 | 0 | 0→1 |

公式验证：`phase_full` = 0,0,1,1,0,0 = \(\lfloor k/2\rfloor\bmod 2\) ✓；`phase_empty` = 1,1,0,0,1,1 与之互补 ✓；前 4 行与书中官方表（[chapter_async_barriers/index.md:L79-L84](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_async_barriers/index.md#L79-L84)）逐项一致 ✓。

故障推演结论：

- **漏翻 `phase_full`（消费者侧）**：k=2 时 `wait(full[0], 0)` 见到 parity 1 立即放行，MMA 在 TMA load2 的数据落盘前就读 stage 0——读到上一轮的旧数据，**静默出错果**（这正是 [chapter_gemm_basics/index.md:L346](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L346) 警告的情形；若时序相反、wait 发出时 load2 已完成，则该 wait 会一直阻塞到 k=4 的装载，表现为**多等一整圈的停顿**）。两种症状同根：奇偶不再标识预期的轮次。
- **漏翻 `phase_empty`（生产者侧）**：k=2 时 `wait(empty[0], 1)` 阻塞（parity==1），要等"parity 1 这轮"完成即消费者 k=2 的 arrive；而消费者 k=2 在等 `full[0]`，`full[0]` 又等被阻塞生产者的 load2——**循环等待，死锁**。
- 两相对照即 [chapter_gemm_advanced/index.md:L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L89) 的总结：相位用错，"可能死锁，也可能在数据就绪前继续执行"。

#### 4.3.5 小练习与答案

**练习 1**：depth=3（三个 stage）时，第 7 次迭代（k=6，从 0 计）消费者应用哪个奇偶等待 full？

**答案**：\( \lfloor 6/3 \rfloor \bmod 2 = 2 \bmod 2 = 0 \)。翻转条件相应改为 `if stage == 2: phase ^= 1`；k=0..6 的 `phase_full` 序列为 0,0,0,1,1,1,0。

**练习 2**：生产者初始 `phase_empty=1`、消费者初始 `phase_full=0`。若把生产者初值也设为 0，第一次迭代会发生什么？

**答案**：k=0 时 `wait(empty[0], 0)` 与屏障初始 parity 0 相同，按语义阻塞——可缓冲本来是空的、不存在要等的前一轮，生产者永远等不到那次 arrive（消费者在等 full，full 又等这次加载），启动即**死锁**。这正演示了"两端必须初始相位相反"。

**练习 3**：Step 5 的内核只有 `tma_bar`（full）数组，没有 empty 屏障，它靠什么保证覆写安全？

**答案**：靠**程序次序**隐式归还（[chapter_gemm_async/index.md:L416-L423](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L416-L423)）：先 `try_wait(mma_bar, phase_mma)` 等 MMA 真正完成，才对同一 stage 发起 `k+PIPE_DEPTH` 的加载。代价是装载与计算无法真正并发（角色未分工）；Step 7 把归还升级为显式 empty 屏障后才获得完全重叠。

## 5. 综合实践

把本讲三个模块串成一个可复算的实验（无需 GPU，纯 Python；仓库本身无相关测试可用，以下脚本为**示例代码**）。

**任务**：为 full/empty 双屏障环写一个 30 行左右的最小相位模拟器，用它机器验证 4.3.4 的手推表，再做两个"破坏性实验"。

1. **建模**：按 [mbarrier_mechanism 演示的四个字段](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/mbarrier_mechanism.html#L119-L123) 写一个 `Barrier` 类：`parity`、`pending`、`expected`、`tx`；`arrive()`/`expect_tx(n)`/`complete_tx(n)` 各自核减，`pending==0 且 tx==0` 时翻 parity 并重装 pending；`try_wait(P)` 断言 `parity != P`，相等即抛异常（模拟"将永久阻塞"）。
2. **复现**：按 4.3.2 的每轮顺序（P.wait_empty → P.load(expect_tx+complete_tx) → C.wait_full → C.arrive_empty）跑 k=0..5，打印 4.3.4 答案表的 5 列；末尾断言 `phase_full == [(k//2)%2 for k in range(6)]`、`phase_empty` 互补。
3. **破坏实验 A**：删掉消费者 `if stage==1: phase_full ^= 1` 中的一次翻转。在顺序模型里 k=2 的 `wait(full[0], 0)` 发生在 load2 完成之后（parity 已回到 0），断言触发"将永久阻塞"；结合 4.3.4 的分析说明真实并发硬件上更可能的表现是**提前放行读旧数据**——两种症状都源于"奇偶不再标识预期轮次"。
4. **破坏实验 B**：删掉生产者的一次 `phase_empty` 翻转。断言在 k=2 的 `wait(empty[0], 1)` 触发；写出"生产者→full→消费者→empty→生产者"的循环依赖，解释为何必然死锁。
5. **推广**：把 `S` 改为 3 跑 9 轮，验证 \(\text{phase}(k)=\lfloor k/3\rfloor\bmod 2\) 与翻转条件 `stage == S-1`。

**预期结果**：第 2 步输出与 4.3.4 的手推表逐格一致；第 3、4 步分别在 k=2 处抛出阻塞异常，异常信息能对应到书上两处警告（静默错果 / 死锁）。脚本为示例代码、本讲义撰写环境未执行，具体打印格式**待本地验证**，但表值与故障点均经手推并与书中四迭代官方表核对。

**延伸（有 Blackwell GPU 时）**：把 [chapter_gemm_async/index.md:L405-L427](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L405-L427) 主循环里的 `phase_tma ^= 1` 注释掉一行，编译运行 Step 5（编译与验证回路见 u9-l2），观察结果是错数还是挂起，与你的推演对照；无 GPU 时此项改为纯源码推演。

## 6. 本讲小结

- mbarrier 经**相位复用**：一相完成（pending 与 tx-count 同时归零）后硬件原子翻入下一相并重装 pending，无需重新 init；软件只跟踪 `phase % 2`。
- `try_wait(P)` 的语义是"**屏障离开相位 P 后返回**"——阻塞当且仅当前 parity 等于 P。这个判据是手推一切相位序列的工具。
- 深度为 \(S\) 的环：`stage = k mod S`，全局 `phase = ⌊k/S⌋ mod 2`，翻转只发生在 `stage == S-1`；同一道 stage 屏障相邻两次使用的等待奇偶必然相反。
- 完整的缓冲复用协议给每个 stage 配 **full/empty 两道屏障**：full 交数据（生产者→消费者），empty 还缓冲（消费者→生产者）；两者相位分开跟踪，初始奇偶刻意相反（生产者 1 立即放行、消费者 0 强制阻塞）。
- 相位跟踪错误的两种症状：消费者侧漏翻 → wait 提前通过、**静默读旧数据**（或多等一圈）；生产者侧漏翻 → 循环等待、**死锁**。真实内核用 `PipelineState` 把 stage 与 phase 捆绑，避免 off-by-one。

## 7. 下一步学习建议

- **本单元收尾**：下一讲 u8-l3 讲 Cluster Launch Control——运行中的 CTA 异步"取消并继承"未启动的 launch，请求结果经 SMEM 与 **mbarrier** 应答，是本讲 arrive/wait 协议在调度层面的直接应用。
- **向前回看**：带着本讲的相位表重读 [chapter_gemm_async/index.md:L293](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L293) 的官方练习（PIPE_DEPTH=2、K_TILES=5 的 trace），你应能不假思索地写出每一行的两个相位。
- **向后展望**：u13-l1（Step 7 warp 特化）会把本讲的双屏障环扩展成四道屏障 + 三类并发角色，`PipelineState` 与初始相位不对称将在完整内核里落地；届时死锁推演方法（谁在等谁）就是你的主要分析工具。
- **工具准备**：异步内核一旦相位用错，症状常是"错数"或"挂起"而非报错；提前浏览 [appendix/debugging_warp_specialized.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md) 的 roles/storage/handoff/lifetime 工作表，本讲的屏障表就是它的雏形。
