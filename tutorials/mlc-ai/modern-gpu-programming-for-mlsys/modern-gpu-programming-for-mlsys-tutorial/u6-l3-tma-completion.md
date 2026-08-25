# TMA 完成机制：load 的 mbarrier 与 store 的 commit group

## 1. 本讲目标

学完本讲，你应该能够：

1. **写出 load 侧的完成检测用法**：解释 mbarrier 一个 phase 里「线程到达计数 + 在途字节计数」双计数器的语义，写出 `mbarrier.arrive.expect_tx` / `mbarrier.try_wait(phase)` 的正确调用顺序，并为给定 tile 配置算出应登记的字节数。
2. **写出 store 侧的完成检测用法**：写出 `cp.async.bulk.commit_group` / `cp.async.bulk.wait_group 0` 的四步套路，解释它保护的是「源缓冲何时可复用」而不是「数据何时到达」。
3. **解释两种机制为什么不同**：从数据搬运方向、等待者身份、被保护的资源三个角度，说清为什么 load 用字节追踪、store 用 commit/wait group，并把两者放进一个双 stage 流水线骨架。

本讲是「TMA 异步数据搬运」单元的收尾。前两讲回答的是「怎么搬」——tensor map 描述符（u6-l1）、3D 坐标与 swizzle 行布局（u6-l2）；本讲回答的是**「怎么知道搬完了」**。这个问题不解决，异步搬运只能退化成「发起后立刻干等」，重叠也就无从谈起。

## 2. 前置知识

本讲直接建立在 u6-l1、u6-l2 与 u2-l3 之上，先用四段话把需要的结论串起来。

**来自 u6-l1（TMA 基本模型）**：TMA 的本质是「发起与执行分离」——warp 内单个线程提交 `cp.async.bulk.tensor`，其余线程瞬间掩蔽，随后 TMA 引擎异步完成全部地址计算与搬运。当时留下一句结论：**传输完成必须靠 mbarrier 字节计数追踪，CTA 栅栏无效**——因为 CTA 栅栏只能同步线程，而 TMA 引擎不是线程。本讲就把这句话展开成可操作的调用序列。

**来自 u6-l2（3D TMA）**：一条 3D TMA 指令能一次搬多个 swizzle atom——演示里的 16×128 fp16 切片共 4096 字节。这个数字不是孤例：本讲正文的例子恰好也是「两个 tile 各 2048 字节、共 4096 字节」，而真实 GEMM 内核里 A、B 两个 128×64 fp16 tile 各 16384 字节、共 32768 字节。**异步搬运的量越大，「什么时候算搬完」就越需要一个精确的判据**——这个判据就是字节数。

**来自 u2-l3（GEMM 数据流水线）**：一个 GEMM tile 分三段——Load（GMEM→SMEM）、Compute（SMEM→TMEM）、Epilogue（TMEM→寄存器→SMEM→GMEM），每段交接处都需要**完成信号**：字节计数 barrier（load 侧）、MMA 完成信号、stage 复用信号。本讲把其中两个信号具体化：字节计数 barrier 就是本讲的模块一，stage 复用信号对应本讲的模块二和模块三。

**三个关键身份**，读后续内容前先分清：

| 身份 | load 方向（GMEM→SMEM） | store 方向（SMEM→GMEM） |
| --- | --- | --- |
| 发起者 | 内核里被选中的单个线程（`tid == 0`） | 内核里被选中的单个线程（`tid == 0`） |
| 执行者 | TMA 引擎（硬件） | TMA 引擎（硬件） |
| 等待者 | **消费者**（要读 SMEM 的 MMA） | **生产者**（要复用 SMEM 源缓冲的内核自身） |
| 等的问题 | 数据何时**可以读** | 源缓冲何时**可以改写** |

两个方向等待的问题不同、等待者身份不同——这就是两套完成机制的根源，模块二会详细对比。

## 3. 本讲源码地图

本讲的"源码"是教材正文、一张时序图，以及 GEMM 章里把这些机制写成真实 TIRx 代码的内核：

| 文件 | 作用 |
| --- | --- |
| [chapter_tma/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md) | TMA 章正文。本讲主要读 L129-L186 三节：`How to Wait for a TMA Load`、`How to Wait for a TMA Store`、`Putting TMA into a Pipeline` |
| [zh/chapter_tma/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_tma/index.md) | 上述三节的中文镜像（L129-L185），内容同构，可对照术语 |
| [img/tma_sync_flow.svg](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/tma_sync_flow.svg) | load 交接时序图：四条生命线分别是发起线程、TMA 引擎、mbarrier、消费数据的 MMA；经正文 L150 与 GEMM 章 L57 两处嵌入 |
| [chapter_gemm_async/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md) | GEMM 章 Step 4 的完整 TIRx 内核：L37-L69 代码对照与逐行讲解、L92-L126 配置、L146-L190 load 循环、L204-L215 store 回写、L679-L683 章末练习。本讲把它当作机制的"实物证据"，逐行精读留给 u12-l1 |

需要说明：GEMM 章属于单元十二的领地，本讲只摘取与本讲三问直接相关的片段。读到不认识的 TIRx 语法（`Tx.copy_async`、`T.ptx.*`）时不必深究，把它们当成「内核向 TMA 引擎和 barrier 发出的调用」即可，编程模型本身由单元九系统讲解。

## 4. 核心概念与源码讲解

### 4.1 模块一：load 完成追踪——mbarrier 的字节计数

#### 4.1.1 概念说明

TMA load 是异步操作：发出指令只表示搬运已经开始，consumer 还不能读目标 tile（[chapter_tma/index.md:L131](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L131)）。这里有个初学者最容易犯的错误：用 `cta_sync()`（CTA 栅栏）来等——GEMM 章明确指出 `cta_sync()` 只同步 CTA 线程，**无法判断异步搬运是否结束**（[chapter_gemm_async/index.md:L53](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L53)）。理由在 u6-l1 就埋下了：TMA 引擎不是线程，它的进度对线程栅栏不可见。

TMA 用 **mbarrier**（memory barrier，驻留在共享内存中的硬件同步原语）完成这次交接。它一个 phase（相位，同一 barrier 先后两次使用的区分标记）里同时维护**两个计数器**：

- **arrival count（到达计数）**：还有多少个线程「到达」没到——和普通屏障的计数一样；
- **pending transaction bytes（在途事务字节数）**：还有多少字节的异步搬运没有落地——这是为 TMA 引擎准备的。

正文一句话给出完成判据：**两个计数都归零，这个 phase 才算完成**（[chapter_tma/index.md:L133](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L133)）。为什么第二个计数器必须是「字节」？因为完成事件的报告者是硬件引擎，不是线程——线程能「到达」屏障，引擎只能报告「我写完了多少字节」。把字节数当作两边通用的记账单位，线程侧登记、引擎侧扣减，两边的账对上时数据就齐了。

#### 4.1.2 核心流程

完整协议分三步：**producer 告诉 barrier 本轮预计传输多少字节 → TMA 引擎写完那些字节后更新 barrier → consumer 等待该 phase 完成**（[chapter_tma/index.md:L131](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L131)）。

用正文的数值例子（A、B 两个 tile 各 2048 字节，共用一个 mbarrier，期望到达数初始化为 1）把计数器演化逐时刻列出：

| 时刻 | 动作 | 执行者 | arrival count | pending bytes | phase 状态 |
| --- | --- | --- | --- | --- | --- |
| 初始化 | `mbarrier.init(bar, count=1)` | 线程 | 1 | 0 | 未完成 |
| 发起 | 两条 TMA load 关联到 `bar` | 线程 `tid==0` | 1 | 0 | 未完成 |
| 登记 | `mbarrier.arrive.expect_tx(4096)` | 线程 `tid==0` | **0** | **4096** | 未完成 |
| A 落地 | complete-tx 扣减 2048 | TMA 引擎 | 0 | 2048 | 未完成 |
| B 落地 | complete-tx 扣减 2048 | TMA 引擎 | 0 | **0** | **完成** |
| 通过 | `try_wait(phase)` 返回 | consumer | — | — | 可以读 SMEM |

对应正文的两行状态记录（[chapter_tma/index.md:L143-L146](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L143-L146)）。要登记的字节数是一条简单公式：

\[
\text{expect\_tx} = (\text{A 元素数} + \text{B 元素数}) \times \text{每元素字节数}
\]

真实内核里 A、B 各为 128×64 fp16（16384 字节），登记总数 32768（[chapter_gemm_async/index.md:L63](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L63)）。注意 `arrive.expect_tx` 这一条指令同时干了两件事：**完成该线程的一次到达**（arrival count 1→0）**并把 pending bytes 设为 4096**——所以它必须出现在两条 load 发起之后、且由同一个被选中的线程执行。

#### 4.1.3 源码精读

**（1）正文的双计数器定义。** [chapter_tma/index.md:L129-L134](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L129-L134)：`How to Wait for a TMA Load` 一节开头即给出三步协议，并明确「一个 phase 同时记录 arrival count 和 pending transaction bytes，两者都归零 phase 才完成」。

**（2）正文的数值演算与完成图。** [chapter_tma/index.md:L135-L150](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L135-L150)：L137-L139 算出 2048+2048=4096；L141 解释 `arrive.expect_tx(4096)` 的双重作用；L144-L146 给出两行状态；L148 说明每次 copy 完成时引擎用 complete-tx 扣减相应字节数、consumer 用 `try_wait(phase)` 等待；L150 嵌入时序图 `tma_sync_flow.svg`。GEMM 章对同一张图有更细的文字解说：四条生命线是发起线程、TMA 引擎、mbarrier、MMA，图中的步骤 1、2 发起在线程上，步骤 3 由引擎执行 complete-tx，步骤 4 consumer 的 `try_wait` 通过、步骤 5 MMA 开始读取（[chapter_gemm_async/index.md:L55-L61](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L55-L61)）。

**（3）真实 TIRx 内核：Step 3 与 Step 4 的对照。** [chapter_gemm_async/index.md:L37-L45](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L37-L45)：Step 3 里全部 128 个线程执行 `Tx.cta.copy` 后 `cta_sync`；Step 4 换成 `if tid == 0:` 内两条 `Tx.copy_async(..., dispatch="tma_auto")` 加一条 `T.ptx.mbarrier.arrive.expect_tx(tma_bar, byte_count)`，随后**所有线程**执行 `T.ptx.mbarrier.try_wait(tma_bar, phase)` 才轮到 MMA 读 SMEM。两个细节值得注意：发起在单线程的 `if` 里，等待在 `if` 外面——消费者全体都要等；`try_wait` 带的 `phase` 参数用来区分同一 barrier 的先后两次使用（每轮循环末尾 `phase_tma ^= 1` 翻转，见 L189），其机制留待单元八展开。

**（4）内核里的字节数与屏障初始化。** 完整内核中，`tma_load` 辅助函数把字节数写成 `(BLK_M * BLK_K + BLK_N * BLK_K) * F16_SIZE`（[chapter_gemm_async/index.md:L146-L160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L146-L160)），其中 `BLK_M, BLK_N, BLK_K = 128, 128, 64`、`F16_SIZE = 2`（[L92-L94](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L92-L94)）；两个 barrier（MMA 用与 TMA 用）在初始化阶段以期望到达数 1 创建（[L125-L126](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L125-L126)），正文 L232-L234 逐条解释了字节数公式与初始化。

#### 4.1.4 代码实践

**实践：为几组 tile 配置计算 `expect_tx`，并预测字节数写错后的行为（纸笔推导，无需 GPU）。**

1. **实践目标**：把字节数公式用熟，并依据正文的两条计数规则推导「登记过少 / 过多」各自的后果——这正是 GEMM 章章末练习 1（[chapter_gemm_async/index.md:L681](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L681)）。
2. **操作步骤**：
   - 对下面三组配置计算应登记的字节数（A、B 共用同一个 barrier）：
     - ① 正文例子：两个 tile 各 2048 字节；
     - ② Step 4 内核：`BLK_M=BLK_N=128, BLK_K=64`，fp16；
     - ③ 同 ②，但 dtype 换成 fp8（每元素 1 字节）。
   - 假设 ② 中程序员漏算了 B tile（只登记 16384 字节），依据「phase 在两个计数都归零时完成」与「每次 copy 完成扣减对应字节数」两条规则（[chapter_tma/index.md:L133](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L133)、[L148](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L148)），推演 `try_wait` 何时通过、消费者会读到什么。
   - 再假设登记数翻倍（65536），重复推演。
3. **需要观察的现象**：过小与过大分别对应「提前通过」还是「永不通过」；两种错误的症状（错误结果 vs 挂死）有什么本质区别。
4. **预期结果**：
   - ① 4096 字节（正文 L137-L139）；② (128×64 + 128×64)×2 = 32768 字节（正文 L63）；③ (8192+8192)×1 = 16384 字节。
   - **过小（16384）**：A 的 complete-tx（16384）一步就把 pending bytes 扣到 0，phase 在**只有 A 落地时**即告完成——`try_wait` 提前通过，MMA 可能在 B 尚在途中就读 SMEM，读到的 B 是旧数据。症状是**结果错误**，且随引擎时序间歇出现。
   - **过大（65536）**：引擎实际只完成 32768 字节的扣减，pending bytes 永远剩 32768，phase 永不完成——`try_wait` 永远等不到。症状是**内核挂死**。
   - 两条结论都由正文规则直接推出；具体在硬件上的表现（错误结果的具体数值、挂死还是超时）待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么发起 TMA 的线程要「先发两条 load、再执行 `arrive.expect_tx`」，顺序能颠倒吗？

**答案**：`expect_tx` 只是把本轮字节数登记进 barrier，它本身不感知有几次拷贝；真正让计数归零的是引擎的 complete-tx。顺序上，只要 `expect_tx` 与两次 load 都在 consumer 的 `try_wait` 之前完成即可。但惯用顺序（先 load 后 expect_tx，见 [chapter_gemm_async/index.md:L151-L160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L151-L160)）有一个好处：从 load 发出到登记之间没有窗口让「字节先到、账后记」造成计数瞬时为负的歧义。此外 `arrive.expect_tx` 同时完成该线程唯一的一次到达，所以它只能被执行一次、且必须由那个被选中的 `tid == 0` 线程执行。

**练习 2**：两个 tile 共用一个 barrier，与各用一个 barrier，等待语义有什么差别？

**答案**：共用一个 barrier：两次 complete-tx 扣同一个账，consumer **一次 `try_wait` 就等到两个 tile 都齐**——Step 4 正是这么做的（A、B 同用 `tma_bar`）。各用一个 barrier：可以分别等待、先到先用，代价是两次 `try_wait`。到 Step 5 的双缓冲里，按 stage 而不是按操作数分 barrier（每 stage 一个 `tma_bar.ptr_to([s])`，[chapter_gemm_async/index.md:L356-L358](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L356-L358)），原因见模块三与 GEMM 章练习 2（[L682](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L682)）。

**练习 3**：`cta_sync()` 之后紧跟 TMA 发起，再 `cta_sync()` 一次，能不能替代 mbarrier？

**答案**：不能。两次 `cta_sync` 之间只发生了「发起」这个动作，发起线程立刻就返回了；TMA 引擎的搬运与线程栅栏毫无关系，第二次 `cta_sync` 可能在字节还在 HBM 到 SMEM 的路上时就通过。正文说得直接：`cta_sync` 只同步 CTA 线程，无法判断异步传输是否结束（[chapter_gemm_async/index.md:L53](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L53)）。唯一能观察到引擎进度的是引擎自己发动的 complete-tx——也就是 mbarrier 的字节计数。

### 4.2 模块二：store 的 commit group 与 wait group

#### 4.2.1 概念说明

TMA store 沿**相反方向**搬运：从 shared memory 写回 global memory。方向一变，同步问题随之改变——正文的原话是：**load 的 consumer 要知道目标 tile 何时可以读取，store 的 producer 则要知道源 buffer 何时可以复用**（[chapter_tma/index.md:L152-L154](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L152-L154)）。

看一个具体险情：epilogue 已把输出 tile 写进 `Dsmem`，随后发起 TMA store 把它写回全局的 `D`。发出 store 后，内核**不能立刻覆盖 `Dsmem`**——TMA 引擎可能还没读完，覆写会让引擎读到下一轮迭代的数据（[chapter_tma/index.md:L156](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L156)）。注意这次要保护的不是「数据的到达」，而是**源缓冲的独占写权**：在引擎读完之前，内核这个「生产者」不能动手改写。

store 路径为此使用 **bulk async group**（批量异步组），四步套路（[chapter_tma/index.md:L158-L165](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L158-L165)）：

```text
发起一个或多个 TMA stores
cp.async.bulk.commit_group
cp.async.bulk.wait_group 0
复用 Dsmm
```

- `commit_group` 把此前**尚未提交**的 stores 打包成一个 bulk async group；
- `wait_group 0` 等到先前提交的 groups **全部完成**——参数是「允许剩几个未完成组」，0 就是全部排空；
- 它返回之后，`Dsmm` 才能安全复用。

一句话对比两套机制（正文的收束，[chapter_tma/index.md:L167-L172](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L167-L172)）：**TMA load：consumer 通过带 byte count 的 mbarrier 等待数据到达；TMA store：producer 通过 commit group 和 wait group 等待 source 可复用。**

#### 4.2.2 核心流程

把 store 回写放进一次 K 迭代的时间轴（角色见第 2 节的表格——此时内核是源缓冲的生产者，引擎是它的消费者）：

```text
时刻 1  全部 128 线程：把结果行写入 Dsmem
时刻 2  全部线程：fence.proxy_async + warpgroup_sync     # 线程写对引擎可见、且全组写完
时刻 3  tid == 0：发起 TMA store（Dsmem -> D）           # 引擎开始异步读 Dsmem
时刻 4  tid == 0：cp.async.bulk.commit_group             # 打包成一个 group
时刻 5  tid == 0：cp.async.bulk.wait_group 0             # 排空：引擎已读完 Dsmem
时刻 6  warpgroup_sync                                    # 把"可复用"广播给全组
时刻 7  此后任何线程才允许覆写 Dsmm
```

组的纪律是理解 `wait_group N` 的关键：

- 两次 `commit_group` 之间的所有 store 同属一组；
- `wait_group N` 的语义是「等到**至多剩 N 个**已提交组未完成」：`N=0` 全部排空（本书回写都用 0）；`N=1` 允许一个组在途——若源缓冲有双份，就能让「本轮 store」与「下轮写缓冲」重叠；
- 忘记 `commit_group` 时，已发起的 store 不属于任何组，`wait_group` 管不到它们——这是比忘记 wait 更隐蔽的错误。

#### 4.2.3 源码精读

**（1）正文的问题转换。** [chapter_tma/index.md:L152-L156](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L152-L156)：方向反转 → 等待问题从「目标可读」变为「源可复用」；`Dsmm` 例子说明覆写与引擎读取的竞争。

**（2）正文的四步套路与两行总结。** [chapter_tma/index.md:L158-L172](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L158-L172)：四行伪代码、`commit_group`/`wait_group 0` 的语义解释（L165），以及 L167-L172 的两条路径对照。

**（3）真实内核的 epilogue。** [chapter_gemm_async/index.md:L204-L215](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L204-L215)：寄存器写入 `Dsmem` → `T.ptx.fence.proxy_async("shared::cta")` → `T.cuda.warpgroup_sync(10)` → `if tid == 0:` 内 `Tx.copy_async(D[...], Dsmem, dispatch="tma_auto")` + `T.ptx.cp_async.bulk.commit_group()` + `T.ptx.cp_async.bulk.wait_group(0)` → 最后一道 `T.cuda.warpgroup_sync(10)`。对照第 2 节的时间轴：时刻 1 对应 L205，时刻 2 对应 L206-L207，时刻 3–5 对应 L210-L214，时刻 6 对应 L215。

**（4）正文对两个前置动作与命名屏障的解释。** [chapter_gemm_async/index.md:L65-L69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L65-L69)：store 之前，`fence.proxy_async` 让每个线程的写入对 async proxy（异步代理，即 TMA 引擎所处的访存路径）可见；第一道 `warpgroup_sync(10)` 确保全组 128 线程都写完并做了 fence，`tid == 0` 才发起 store。`warpgroup_sync(10)` 低级实现为 `bar.sync 10, 128`——`10` 只是 CTA 的 16 个命名屏障槽位（ID 0–15）之一，**对 TMA 没有特殊含义**；它与追踪 TMA load 的共享内存 mbarrier 是两套东西，每次同步完成后自动复位、可重复使用同一 ID。第二道 `warpgroup_sync(10)` 把其他线程拦到 `tid == 0` 排空 store group 为止——在那之前 `Dsmm` 不能被覆写或复用。`wait_group(0)` 里的 `0` 表示「不允许有任何已提交组未完成」。

#### 4.2.4 代码实践

**实践：给 epilogue 的六行代码做角色标注，并预测两处典型改错（源码阅读型，无需 GPU）。**

1. **实践目标**：把「谁执行、保护什么资源」落实到每一行；用两个改错实验检验对 commit/wait 语义的理解。
2. **操作步骤**：
   - 对 [chapter_gemm_async/index.md:L205-L215](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L205-L215) 的每一行填写三列：执行者（全组 128 线程 / 仅 `tid==0` / TMA 引擎）、同步对象（无 / 命名屏障 10 / bulk async group）、删掉它会破坏什么。
   - 改错 A：把 `commit_group()`（L213）移到 `Tx.copy_async`（L211-L212）**之前**，推演 `wait_group(0)` 的行为。
   - 改错 B：把 `wait_group(0)` 改成 `wait_group(1)`，推演本内核（整个 epilogue 只提交一个组）的行为。
3. **需要观察的现象**：两种改错分别让「排空」失效在哪一步——是组里没有 store，还是组允许留在途。
4. **预期结果**：
   - 标注表：L205 全组写 SMEM；L206 fence（每线程各自的写对引擎可见）；L207 / L215 命名屏障 10（前者保证全组写完才发起，后者把排空结果广播给全组）；L210 的 `if` 内三行仅 `tid==0` 执行，其中 copy 的执行者是 TMA 引擎。
   - 改错 A：commit 时还没有任何未提交的 store，于是提交了一个**空组**；随后的 store 不属于任何组，`wait_group(0)` 立即返回——排空失效，`Dsmm` 可能在引擎读取时被覆写，输出损坏。
   - 改错 B：`wait_group(1)` 允许剩 1 个组在途，而此刻恰好只有 1 个已提交组——条件立即满足、同样立刻返回，效果与改错 A 相同：源缓冲提前复用。只有当存在第二份缓冲、上一组确已提交时，`N=1` 才是「双缓冲 store」的正确参数。
   - 两个推演均由 `wait_group N` 的语义（[chapter_tma/index.md:L165](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L165)、[chapter_gemm_async/index.md:L69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L69)）直接推出；实际硬件症状待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：load 侧等的是「4096 字节都到了」，store 侧为什么不能也用「D tile 的字节数都发出去了」这种字节计数来等？

**答案**：因为两个等待回答的是不同的问题。load 的完成事件是**字节落入 SMEM**——引擎每写完一段就能报告一段，字节计数天然贴合「数据到齐」；consumer 等的是数据有效性。store 的完成事件对内核而言是**引擎对 `Dsmm` 的读取结束**——内核关心的是源缓冲的独占权何时归还，而不是全局内存那边收到了多少字节（那是接收方的事，内核无需过问）。「还有几个已提交组未完成」正好直接回答占有权问题，所以 bulk async group 是这个方向的自然表达。正文的分工总结见 [chapter_tma/index.md:L167-L172](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L167-L172)。

**练习 2**：`wait_group 0` 和 `wait_group 1` 各适合什么场景？

**答案**：`0` = 一个已提交组都不许剩，源缓冲立即完全回收——单份 `Dsmm` 的同步回写用它（本内核 L214）。`1` = 允许一个组在途，配合**两份**源缓冲交替使用：本轮写缓冲 X 时，上一轮对缓冲 Y 的 store 还在飞，只要等「至多剩 1 组」即可——这与 load 侧双缓冲的思想同构，只是方向相反。

**练习 3**：store 之前那道 `fence.proxy_async` 与 `warpgroup_sync(10)` 各自解决什么？少一道会怎样？

**答案**：`fence.proxy_async` 解决**可见性**——线程写的 SMEM 数据要经过代理栅栏才对 TMA 引擎所处的 async proxy 可见，否则引擎可能读到旧值；`warpgroup_sync(10)` 解决**齐步走**——确保全组 128 线程都完成了写入与 fence，`tid == 0` 才发起 store，否则引擎会读到只写了一半的 tile（[chapter_gemm_async/index.md:L65](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L65)）。少 fence：数据可能不完整/过期；少 sync：部分线程还在写、store 已经开读——两者都产生错误结果而非挂死。注意这道屏障与 TMA load 的 mbarrier 互不替代：一个是线程间命名屏障，一个是线程与引擎之间的字节账本（[chapter_gemm_async/index.md:L67](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L67)）。

### 4.3 模块三：流水线中的 TMA——一个 stage 的两个等待方向

#### 4.3.1 概念说明

TMA 能减少 copy 指令数，但正文强调它**更大的收益是让数据搬运与计算重叠**（[chapter_tma/index.md:L176](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L176)）。回想 u2-l3 的三段式流水线：若下一块 A、B tile 没在当前 MMA 结束前到达 SMEM，Tensor Core 只能停下等待，流水线出现**气泡**（bubble）。消除气泡的手段是给 SMEM 配多个 stage（缓冲份），让「搬运下一块」与「计算当前块」同时进行。

两份 stage 的交替就是正文的示例（[chapter_tma/index.md:L178-L181](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L178-L181)）：

```text
时间 t:    MMA 读取 stage 0    TMA 填充 stage 1
时间 t+1:  MMA 读取 stage 1    TMA 填充 stage 0
```

关键在于：**每个 stage 同时牵涉两个方向的等待**。正文各用一句话点出（[chapter_tma/index.md:L183](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L183)）：

1. **MMA 读一个 stage 之前**，要等对应的 TMA load 完成——这正是模块一的字节追踪（数据到齐了吗）；
2. **TMA 覆盖一个 stage 之前**，要确认上一轮计算已不再使用其中的数据——这正是模块二那个「源缓冲可复用」问题，只是把 `Dsmm` 换成了流水线 stage（计算用完了吗）。

于是本讲两个机制在一个流水线里会师：**TMA 负责异步搬运，barrier 负责在 producer 和 consumer 之间交接每个 stage**；两者配合，等待未来数据的时间才有机会被当前 tile 的计算隐藏（[chapter_tma/index.md:L183-L185](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L183-L185)）。u2-l3 与 u3-l3 说过，这两道屏障习惯上叫 full（数据就绪）与 empty（缓冲归还）。

#### 4.3.2 核心流程

双 stage 主循环的骨架（示例伪代码，按模块一、二的机制与 Step 4/5 内核的形态改写，非项目原有代码）：

```text
初始化:  full[0..1] 与 empty[0..1] 各为一个 mbarrier;  phase 各自跟踪
预取:    TMA load -> stage 0;  (消费者对 empty[0] 先视为已归还)

for k in 0 .. K_TILES-1:
    s = k % 2
    # --- 生产方向的等待: stage (k+1)%2 空了才能填 ---
    wait empty[(k+1) % 2]                     # 上一轮消费者已用完该 stage
    TMA load 下一 tile -> stage (k+1) % 2     # 模块一: expect_tx 登记字节数
    # --- 消费方向的等待: stage s 满了才能算 ---
    wait full[s]                              # 模块一: try_wait 等字节归零
    MMA 读取 stage s, 累加进 TMEM
    arrive empty[s]                           # 计算完毕, 归还 stage s
```

把开头两轮迭代铺开成时间表（稳态后按此循环）：

| 迭代 | TMA 填充 | MMA 读取 | MMA 开算前等什么 | TMA 覆盖前等什么 |
| --- | --- | --- | --- | --- |
| k=0 | stage 1 | stage 0 | full[0]（load 完成） | empty[1]（初始视为空） |
| k=1 | stage 0 | stage 1 | full[1] | empty[0]（k=0 的 MMA 已用完） |
| k=2 | stage 1 | stage 0 | full[0]（第二轮相位） | empty[1]（k=1 的 MMA 已用完） |

三个结构性观察：

- **每轮每 stage 各有一次「等满」与一次「等空」**——两个方向缺一不可：只等满会产生「生产者覆写未释放的缓冲」的竞争，只等空会读到旧数据；
- **同一 barrier 每轮被复用一次**，所以等待必须带 phase（第 2 列 k=0 与 k=2 等的是同一个 full[0] 的两次不同使用）——这是单元八的主角，本讲只需记住「try_wait 带 phase 参数」这一用法；
- **stage 数就是流水的深度**：2 份允许加载领先计算 1 个 tile，3 份允许领先 2 个，代价是 SMEM 线性增长（u12-l2 会核算成本）。

#### 4.3.3 源码精读

**（1）正文的重叠收益与交替表。** [chapter_tma/index.md:L174-L185](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_tma/index.md#L174-L185)：L176 点明重叠才是主要收益；L178-L181 给出双 stage 交替；L183 给出双向等待；L185 收束为「TMA 搬运、barrier 交接」。

**（2）校准预期：Step 4 尚未重叠。** GEMM 章在 Step 4 结束处明确说明：该内核**每次 TMA load 后立即等待**，load 与 compute 仍未重叠——这一步改变的只是地址生成与搬运从 CTA 线程转移到 TMA 引擎；Step 5 加入第二份 SMEM stage 做预取，完整的角色级重叠要到 Step 7（[chapter_gemm_async/index.md:L49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L49)）。这提醒我们：**完成机制是重叠的前提，不是重叠本身**——先有可等待的原语，才有资格谈把等待挪出关键路径。

**（3）Step 4 的串行 K 循环。** [chapter_gemm_async/index.md:L172-L190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L172-L190)：`if tid == 0: tma_load(k_st)` → `try_wait(tma_bar, phase_tma)` → `if tid == 0: mma(...)` → `try_wait(mma_bar, phase_mma)` → 两个 phase 翻转。把它与 4.3.2 的骨架对照：结构已经具备（单 stage、两道 mbarrier），只是还没有第二份缓冲让「填下一块」提前。

**（4）Step 5 的按 stage 分 barrier。** 到双缓冲内核，初始化处变为**每个 stage 一个 TMA barrier**（`for s in range(PIPE_DEPTH): T.ptx.mbarrier.init(tma_bar.ptr_to([s]), 1)`，[chapter_gemm_async/index.md:L356-L358](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L356-L358)）——正是 4.1.5 练习 2 预告的形态；GEMM 章练习 2（[L682](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L682)）问的「为什么两个 stage 不能共用一个 tma_bar」就是模块三的思考题，完整所有权表留给 u12-l2。

#### 4.3.4 代码实践

**实践：手推双 stage 调度时间表，并标出每个交接点的等待方向（纸笔推导，无需 GPU）。**

1. **实践目标**：把「一个 stage、两个等待方向」内化成一张可以逐格核对的时间表。
2. **操作步骤**：
   - 按 4.3.2 骨架，对 k = 0..4 共 5 轮迭代填写六列：迭代号、TMA 填充的 stage、MMA 读取的 stage、TMA 覆盖前等的 empty、MMA 开算前等的 full、本轮结束后被归还的 stage；
   - 在 k=0 行特别标注：为什么 empty[1] 不需要真实等待（初始态）；
   - 回答：如果两个 stage 共用一个 full barrier（所有 load 的 complete-tx 都扣同一本账），consumer 在 k=1 时等「stage 1 就绪」会不会误判？误判成什么？
3. **需要观察的现象**：等待方向是否每行恰好两条；full 与 empty 的下标是否随 k 交替；共屏障时「等谁」的信息丢在哪里。
4. **预期结果**：
   - 时间表与 4.3.2 的示例一致（k 偶数填 1 读 0、奇数填 0 读 1，empty/full 下标随 `(k+1)%2`、`k%2` 交替；k=0 时 stage 1 尚无消费者，empty[1] 视为已归还）；
   - 共用 full barrier 时：k=0 与 k=1 的 load 字节全部扣进同一个 phase 的同一本账，consumer 的 `try_wait` 只能知道「一共 2×32768 字节到齐了」，**无法知道其中哪一半属于 stage 1**——它可能在 stage 1 的字节只到一半时（总量恰好补齐 stage 0 那份的缺口的瞬间）误判为就绪；同时同一 barrier 每轮被使用两次而 phase 只翻一次，等待会与错误的一轮配对。这就是 Step 5 按 stage 分 barrier 的原因（[chapter_gemm_async/index.md:L356-L358](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L356-L358)、[L682](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L682)）。以上为机制推导；在 GPU 上的实测复现待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么「等数据到齐」和「等缓冲归还」必须分成两道屏障，而不能合成一道？

**答案**：因为两个事件由**不同的角色在不同时刻**触发：「满」由 TMA 引擎的 complete-tx 触发（字节到齐），「空」由消费者触发（MMA 用完这个 stage）。合成一道屏障的话，生产者与消费者会在同一个计数器上互相干扰——生产者无从知道「缓冲已被释放」，只能盲目等待或者干脆不等待就开始覆写（丢失上一轮数据）；消费者也无从知道「新数据已就绪」，可能读到旧数据。两道屏障各自维护一本账，才构成完整的所有权交接（full = 数据所有权 producer→consumer，empty = 缓冲所有权 consumer→producer）。

**练习 2**：把双 stage 扩成三 stage，收益和代价各是什么？

**答案**：收益是加载可以领先计算最多 2 个 tile，HBM 延迟与带宽波动被更深的缓冲吸收，气泡进一步缩小；代价是 SMEM 占用按 stage 数线性增长（A、B 各一份每 stage，fp16 下每 stage 32KB），挤占每 SM 可驻留的 CTA 数（u3-l3 讲过的资源压力），B200 每 SM 228KB 的上限很快会到。深度的取舍正是 u12-l2 的核算内容。

**练习 3**：Step 4 已经用上了 mbarrier 和 TMA，为什么还说它「load 与 compute 尚未重叠」？

**答案**：因为它的等待仍在关键路径上：每轮 `tma_load` 之后**立即** `try_wait`，等到数据到了才发起 MMA——「发起后立刻干等」只是把搬运者从线程换成了引擎（copy 指令数减少了），等待本身没有被藏起来。重叠需要第二份缓冲让 load 提前（Step 5）、需要把加载与计算分给并发角色（Step 7）。完成机制提供的是「可等待的原语」，重叠是后续用流水线结构对这些原语的编排（[chapter_gemm_async/index.md:L49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L49)）。

## 5. 综合实践

把三个模块串成一个任务：**分别写出 TMA load 与 store 的完成检测伪代码，并用两段文字解释为什么二者需要不同机制、各自保护什么资源**——这正是本讲规格指定的实践。全程纸笔即可完成；伪代码以 GEMM Step 4 内核为模板。

**任务 A：写 load 侧完成检测伪代码（示例代码，非项目原有代码，逐行对应 Step 4 真实内核）。**

```python
# 示例伪代码: TMA load 的完成追踪(以 chapter_gemm_async Step 4 为模板)
# 配置: BLK_M=BLK_N=128, BLK_K=64, fp16 -> A、B 各 16384 字节
tma_bar = init_mbarrier(expected_arrival=1)        # L125-L126: init(bar, 1)
phase = 0
for k in range(K_TILES):
    if tid == 0:                                    # 单线程发起(L176-L177)
        Tx.copy_async(Asmem, A_tile_k, mbar=tma_bar)     # 引擎完成后 complete-tx
        Tx.copy_async(Bsmem, B_tile_k, mbar=tma_bar)     # 同一 barrier, 共一本账
        T.ptx.mbarrier.arrive.expect_tx(                 # 一次指令做两件事:
            tma_bar,                                     #   到达计数 1->0
            (BLK_M*BLK_K + BLK_N*BLK_K) * F16_SIZE)      #   在途字节 = 32768 (L157-L160)
    T.ptx.mbarrier.try_wait(tma_bar, phase)         # 全体消费者等字节归零(L181)
    mma(...)                                       # 此后才能读 SMEM
    phase ^= 1                                      # 复用同一 barrier 的下一相位(L189)
```

**任务 B：写 store 侧完成检测伪代码（示例代码，非项目原有代码，对应 Step 4 epilogue）。**

```python
# 示例伪代码: TMA store 的完成检测(以 Step 4 epilogue 为模板, L204-L215)
Tx.copy(Dsmm[row, 0:BLK_N], Dreg_f16[:])            # 1. 全组 128 线程写入源缓冲
T.ptx.fence.proxy_async("shared::cta")               # 2. 线程写对异步代理可见
T.cuda.warpgroup_sync(10)                            # 3. 全组写完才允许发起
if tid == 0:
    Tx.copy_async(D_tile, Dsmem, dispatch="tma_auto")  # 4. 引擎开始异步读 Dsmm
    T.ptx.cp_async.bulk.commit_group()                # 5. 把未提交的 store 打包成组
    T.ptx.cp_async.bulk.wait_group(0)                 # 6. 排空: 引擎已读完源缓冲
T.cuda.warpgroup_sync(10)                            # 7. 广播"可复用"给全组
# 此后 Dsmm 才允许被覆写
```

**任务 C：写两段解释（参考答案）。**

**第一段——为什么需要两套机制。** 两个方向的搬运改变的不是数据，而是「谁在等、等什么」。load 的完成事件发生在内核这一侧（字节落入 SMEM），报告者是 TMA 引擎；引擎不是线程，不能参加线程栅栏，它的进度只能以「已交付字节数」的形式记账，于是 mbarrier 在一个 phase 里同时收线程的到达与引擎的 complete-tx，consumer 用 `try_wait(phase)` 等这本账清零——问题形如「数据到齐了吗」。store 的完成事件对内核而言不是「字节送达全局内存」（那是接收方视角），而是「引擎对源缓冲的占用何时结束」；这本质是一个排他权归还问题，最自然的表达是「还有几个已提交组未完成」，于是硬件提供 bulk async group：`commit_group` 立界、`wait_group N` 数组。一边是增量式字节账本，一边是组粒度的排空计数——形态不同，是因为被等待事件的性质不同。

**第二段——各自保护什么资源。** load 侧保护的是 **SMEM 目的 tile 的数据有效性**：`expect_tx/try_wait` 保证 consumer（MMA）不在字节到齐前读取，防止读到旧数据或半新半旧的数据——写早了不会损坏硬件，只会算错。store 侧保护的是 **SMEM 源缓冲的独占写权**：`commit_group/wait_group` 保证生产者（内核）不在引擎读完后前覆写 `Dsmm`，防止引擎把下一轮的数据当本轮的写回全局——读早了同样只是算错，但方向相反。一个防「读早」，一个防「写早」；一个守消费端，一个守生产端。放进流水线（模块三）后正好一满一空，合成每个 stage 的完整所有权交接。

**任务 D：数值自查与可选验证。**

- 核对任务 A 中字节数：(128×64 + 128×64) × 2 = 32768，与正文 [chapter_gemm_async/index.md:L63](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_async/index.md#L63) 一致；再把 `expect_tx` 改错（16384 / 65536）按 4.1.4 的推演各写一句症状预测。
- 有 Blackwell GPU 时（需按 u1-l3 装好 `apache-tvm==0.26.0` 与 `cuda-bindings`）：把 Step 4 内核跑通后故意改错字节数与 `wait_group` 参数，观察 4.1.4 / 4.2.4 的预测是否应验（错误结果 vs 挂死）——**待本地验证**。无 GPU 时任务 A–C 已构成完整闭环。

## 6. 本讲小结

- **mbarrier 的一个 phase 管两本账**：arrival count（线程到达）与 pending transaction bytes（在途字节），两者都归零 phase 才完成。`arrive.expect_tx(N)` 一条指令同时完成一次到达并登记 N 字节；引擎每完成一次 copy 就 complete-tx 扣减；consumer `try_wait(phase)` 等账清零。`cta_sync` 只同步线程，观察不到引擎。
- **字节数必须算准**：\(\text{expect\_tx} = (\text{A}+\text{B})\text{ 元素数} \times \text{元素字节数}\)，Step 4 为 32768。登记过小 → phase 提前完成、读到半新数据（错误结果）；过大 → 账永远清不了（挂死）。
- **store 问的是另一个问题**：方向反转后，等待从「目标可读」变成「源可复用」。四步套路：发起 store → `commit_group` 打包 → `wait_group 0` 排空 → 复用 `Dsmm`；参数 N 是允许在途的组数。发起前的 `fence.proxy_async` + `warpgroup_sync` 解决可见性与齐步走，命名屏障槽位与 TMA 的 mbarrier 是两套东西。
- **两个机制保护不同的资源**：load 侧守消费端（不早读，保数据有效性）；store 侧守生产端（不早写，保源缓冲独占权）。一防读早、一防写早，根源是两个方向上被等待事件的性质不同。
- **流水线里一个 stage 两个等待方向**：MMA 读前等 full（数据就绪），TMA 覆盖前等 empty（缓冲归还）；TMA 负责异步搬运，barrier 负责在 producer 与 consumer 之间交接 stage 所有权。Step 4 已具备原语但仍在「发起后立即等待」——完成机制是重叠的前提，重叠本身要靠 Step 5 的双缓冲与 Step 7 的角色划分。

## 7. 下一步学习建议

- **u7-l1（tcgen05.mma 的执行方式）**：按书站顺序的下一讲。本讲反复出现的「consumer 等 full 后读 SMEM」的另一端就是 tcgen05——看它如何经矩阵描述符读 A/B、把累加写进 TMEM，以及 MMA 侧自己的完成信号（`tcgen05.commit`，已在 Step 4 循环里露过面）。
- **单元八（异步协调）**：本讲刻意浅处理的 phase 相位、屏障复用、full/empty 双屏障机制将在 u8-l1、u8-l2 系统展开；读完再回看 Step 4 的 `phase_tma ^= 1` 会有完整图景。
- **单元十二（GEMM Step 4–6）**：把本讲当作预告——u12-l1 逐行精读 Step 4 完整内核（含 TMA 配置五要素），u12-l2 做双缓冲所有权表并核算 PIPE_DEPTH 的 SMEM 成本，u12-l3 看持久内核。届时重读本讲 4.3.2 的骨架，应能逐行对上真实代码。
- **动手延伸**：把第 5 节任务 A 的伪代码改写成「A、B 各用一个 barrier」的版本（4.1.5 练习 2 的形态），写出两次 `try_wait` 的顺序并讨论哪种形态允许「A 到了先算 A 相关的部分」——为将来读 FA4（单元十四）里多 barrier 交织的协议热身。
