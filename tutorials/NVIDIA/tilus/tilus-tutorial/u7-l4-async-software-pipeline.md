# 异步软件流水线（Software Pipeline）

## 1. 本讲目标

本讲是「GPU 架构特性与高性能内核实践」单元的第四讲，承接 u7-l1（Ampere 共享内存分块与多级缓冲 `num_stages`）、u7-l2（Hopper `wgmma`/`cp_async`/`mbarrier`）与 u7-l3（Blackwell `tma`/`tcgen05`/TMEM）已经建立的异步硬件能力，回答一个核心问题：**有了异步搬运与异步计算指令后，如何把它们编排起来，让「搬运」与「计算」真正在时间上重叠，从而把全局访存延迟藏起来？**

学完本讲，你应当能够：

- 说清软件流水线（software pipeline）掩盖访存延迟的直觉与原理；
- 读懂 Tilus 里两种真实写法——单 warp「预取错位」流水线（Blackwell `matmul_v2`）与 warp 专用化的生产者—消费者流水线（Hopper `matmul_v3`）；
- 掌握多级环形缓冲（ring buffer）、双屏障（full/empty）与相位（phase）这三件套的协作机制；
- 理解 `lang/classes/pipeline.py` 里的 `Pipeline` 抽象如何把上述机制封装成可复用类，并知道它被 v4 及之后的高性能示例采用。

## 2. 前置知识

本讲默认你已经具备以下认知（来自前置讲义）：

- **张量与内存空间**（u4-l1）：寄存器张量做运算、共享内存做中转、显存做源头；`shared_tensor` 显式分配共享内存。
- **mbarrier 异步同步**（u7-l2/u7-l3）：`mbarrier` 用「待到达计数 + tx-count」与「相位翻转」模型把异步事务（TMA、`cp_async`）的完成与线程同步绑定；`arrive_and_expect_tx` 声明期望字节数，事务完成时硬件自动扣减，二者都归零才翻转相位。
- **三类同步不可互换**（u7-l2/u7-l3）：内存序 fence（如 `wgmma.fence` / `fence.proxy_async`）、异步事务完成等待（`mbarrier.wait`）、线程执行同步（`self.sync()`）各司其职。
- **`thread_group` 收窄执行线程**（u2-l3）：`with self.thread_group(thread_begin, num_threads)` 把一段代码的执行权交给一段连续线程，可嵌套做生产者—消费者分工。

如果对「为什么要把数据先搬进共享内存再算」还存疑，建议先回顾 u7-l1 的共享内存分块一节。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `python/tilus/lang/classes/pipeline.py` | `Pipeline(tilus.Class)` 抽象：把多级环形缓冲 + 双屏障 + 相位封装为 `producer/consumer_acquire/advance/release_barrier` 的可复用类。 |
| `examples/blackwell_matmul/matmul_v2.py` | **手写**的单 warp 多级软件流水线：prefill + 主循环「预取下一块 + 算当前块」，用 `tma_barriers[stage]` + 1 个 `mma_barrier`。 |
| `examples/hopper_matmul/matmul_v3.py` | **手写**的 warp 专用化流水线：TMA 生产者 warp + WGMMA 消费者 warps，用 `consumer_barriers[stage]` + `producer_barriers[stage]` 两组屏障通信。 |
| `python/tilus/lang/instructions/mbarrier.py` | mbarrier 指令组：`alloc/arrive/arrive_and_expect_tx/wait`，以及 `producer_initial_phase=1`、`consumer_initial_phase=0` 两个初值常量。 |
| `python/tilus/lang/constructs/state.py` | `tilus.Class` 基类：像 `Script` 一样可分配 mbarrier/共享张量/TMEM、使用全部指令，但不是内核本身，用于编写可复用组件（如 `Pipeline`）。 |

> 说明：本讲引用的两个示例（Blackwell v2、Hopper v3）都是**手写**流水线——它们直接操作原始 mbarrier，目的是让你看清机制。`pipeline.py` 的 `Pipeline` 类正是把这些机制抽离出来的产物，被 `matmul_v4` 及之后的示例采用。三者构成「先手写、再抽象」的学习链条。

永久链接基准（当前 HEAD）：
`https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/`

## 4. 核心概念与源码讲解

本讲按「动机 → 机制 → 抽象」三个最小模块展开：先讲为什么要 overlap（4.1），再讲多级缓冲 + 双屏障 + 相位的协作机制（4.2），最后讲 `Pipeline` 抽象（4.3）。

### 4.1 overlap 计算与搬运：软件流水线的动机与原理

#### 4.1.1 概念说明

矩阵乘 \(C = A \times B\) 的 K 维循环里，每个分块迭代要做两件事：

1. **搬运**：把 A、B 的下一块从显存（DRAM）搬进共享内存（SRAM）；
2. **计算**：用张量核（MMA/wgmma/tcgen05）把这块乘进累加器。

如果写成朴素的串行循环——「搬一块、等它到、算一块、等它完、再搬下一块」——那么张量核在搬运期间是**闲置**的，搬运在计算期间也是**闲置**的，二者永远不重叠：

```text
朴素（无流水线）：
搬运0 ████          搬运1          ████          搬运2
计算0     ████          ████
时间   ─────────────────────────────────►   （大量空闲）
```

显存带宽通常是高性能 GEMM 的瓶颈。搬运一块要数百上千个时钟周期，而一次 MMA 只要几十个时钟周期——如果每轮都「搬完才算」，张量核的吞吐会被访存拖垮。

**软件流水线**的核心思路是：与其让搬运和计算轮流独占时间轴，不如在**算第 i 块的同时，提前搬运后面几块**，让二者在时间上重叠。代价是需要多份共享内存缓冲（每个「在途」的块各占一份），换来的是张量核几乎不必等待搬运：

```text
两级流水线（搬运与计算重叠）：
搬运0 ████
搬运1     ████
搬运2         ████                 ← 搬运持续在飞
计算0     ████
计算1         ████
计算2             ████             ← 计算持续在飞
时间   ─────────────────────────────────►   （空闲被填满）
```

关键直觉：**缓冲深度（num_stages）越多，能提前搬运的块越多，越能掩盖更长的访存延迟**；但每多一级就多一份共享内存，所以要在「掩盖延迟」与「共享内存容量」之间权衡（这也是 autotune 要搜 `num_stages` 的原因）。

#### 4.1.2 核心流程

实现 overlap 需要三样东西配合：

1. **多级共享内存缓冲**：用 `shared_tensor` 声明一个「前置 stage 维」的张量，如 `shape=[num_stages, block_m, block_k]`，第 0 维把 `num_stages` 份缓冲拼在一起，形成一个**环形缓冲（ring buffer）**——用 `stage % num_stages` 索引。
2. **异步搬运**：用 `tma.global_to_shared` 或 `copy_async` 发起搬运，**不阻塞**当前线程，搬完由硬件通知。
3. **错位调度**：主循环第 i 轮里，一边「等待并消费第 i 块」，一边「预取第 i + (num_stages-1) 块」。两者下标相差 `num_stages-1`，正好让当前块被算完腾出的缓冲槽，被远处的新块填上。

伪代码（单 warp 错位流水线，对应 Blackwell v2）：

```text
prefill：先把前 (num_stages-1) 块发出去（不等）
for i in 0 .. K:
    preload_offset = i + (num_stages - 1)        # 预取远处的新块
    发起 TMA：把第 preload_offset 块搬进 stage = preload_slot
    等待：第 current_stage 块的 TMA 数据到达
    计算：用第 current_stage 块做 MMA
    current_stage, preload_slot 各自环转 +1 % num_stages
```

> 这里有一个精妙之处：循环开始前先 prefill 掉前 `num_stages-1` 块，是为了在主循环第一轮就有「已就绪的当前块」可算；循环结束后还要把在途的最后几块算完（drain）。prefill 与 drain 是流水线必备的「填充」与「排空」阶段。

#### 4.1.3 源码精读：Blackwell v2 的多级共享内存与 prefill

先看多级缓冲的声明。Blackwell v2 给 A、B 各开了一份带 stage 前缀的共享张量，`self.stages` 即流水深度，第 0 维索引缓冲级：

声明多级共享内存缓冲（环形缓冲的物理载体）：
[examples/blackwell_matmul/matmul_v2.py:48-54](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v2.py#L48-L54) —— 第 0 维 `self.stages` 把多份缓冲拼成环形，`s_a[i]`/`s_b[i]` 取第 i 级。

接下来是 per-stage 的 TMA 屏障与一个 MMA 屏障，外加两个相位变量。`tma_barriers` 每级一个，追踪「这一级的 TMA 数据是否到齐」；`mma_barrier` 全局一个，追踪「当前 MMA 是否写完 TMEM」：

[examples/blackwell_matmul/matmul_v2.py:58-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v2.py#L58-L63) —— `tma_barriers`（每级一个，count=1）+ `mma_barrier`（count=1）+ `tma_phase`/`mma_phase`。

prefill 阶段：循环开始前先把前 `stages-1` 块**只发起、不等待**地塞进缓冲，让主循环第一轮就有就绪数据可算：

[examples/blackwell_matmul/matmul_v2.py:65-86](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v2.py#L65-L86) —— 对前 `stages-1` 块：单线程 `arrive_and_expect_tx` 声明字节数，再发起 `tma.global_to_shared`；注意 prefill 只发起不 `wait`，末尾一次 `self.sync()` 让全 warp 对齐。

主循环是「预取 + 计算」错位的本体。每轮：先发起远处新块的 TMA（写入 `preload_stage`），再 `wait` 当前块的 TMA（`current_stage`），然后立刻发起当前块的 `tcgen05.mma` 并 `commit`+`wait`，最后环转下标、翻转相位：

[examples/blackwell_matmul/matmul_v2.py:88-138](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v2.py#L88-L138) —— 关键三步：① 预取 `preload_offset_k = offset_k + (stages-1)*block_k` 写入 `preload_stage`（[L95-L113](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v2.py#L95-L113)）；② `wait` 当前级 TMA 到达（[L114-L120](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v2.py#L114-L120)）；③ 对当前级做 MMA 并 `commit`+`wait`（[L121-L131](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v2.py#L121-L131)）；④ 环转 `preload_stage`/`current_stage` 并按需翻转 `tma_phase`/`mma_phase`（[L133-L137](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/blackwell_matmul/matmul_v2.py#L133-L137)）。

注意两个相位翻转规则的差异：

- `tma_phase ^= current_stage == 0`：仅当 `current_stage` 环转回 0 时翻转——因为每个 `tma_barriers[stage]` 要被反复使用（每个 stage 维护自己的相位周期），只有绕完一圈才需要换相位；
- `mma_phase ^= 1`：每轮都翻转——因为只有一个全局 `mma_barrier`，每次 `commit`+`wait` 就是一个完整周期。

这就是「同一个屏障被多次复用时，靠相位区分第几轮」的典型用法（相位机制见 4.2）。

#### 4.1.4 代码实践：调 `stages` 观察共享内存与延迟

1. **实践目标**：直观感受「多一级缓冲 = 多一份共享内存 = 更能掩盖延迟，但显存占用更大」。
2. **操作步骤**：
   - 打开 `examples/blackwell_matmul/matmul_v2.py`，用 `tilus.option.debug.dump_ir()` 或设 `tilus.option.cache_dir("./cache")`（文件里已设）。
   - 用 `@tilus.autotune("stages", [2, 3, 4])` 把搜索空间临时收窄到单一配置，例如在脚本里加 `debug_schedule` 或直接改装饰器为 `@tilus.autotune("stages", [2])` 单独编译 `stages=2`，再换成 `[4]` 各编译一次。
   - 在生成的缓存目录 `./cache/.../source.cu` 里搜索 `__shared__`，统计 `s_a`/`s_b` 的共享内存大小。
3. **需要观察的现象**：`stages=2` 时 `s_a`/`s_b` 各占 2 份分块大小；`stages=4` 时占 4 份——共享内存随 `stages` 线性增长。
4. **预期结果**：在不爆共享内存的前提下，更大的 `stages` 通常带来更低的延迟（更强的延迟掩盖能力）；但 `stages` 过大会因共享内存压力反而变慢（v4 注释里就提到「`num_stages=7` 在 256×256 时 smem-thrashing」）。
5. 若无 Blackwell GPU，本实践为「源码阅读型」：可只做共享内存大小推算，标注「待本地验证」性能数字。

#### 4.1.5 小练习与答案

**练习 1**：如果 `stages=1`，Blackwell v2 的 prefill 循环 `for i in range(self.stages - 1)` 会执行几次？此时流水线退化成什么？

**参考答案**：执行 0 次。此时没有预取，主循环里 `preload_offset_k = offset_k + 0*block_k = offset_k`，预取的就是当前块本身，搬运与计算不再错位，退化成「搬一块、算一块」的朴素串行循环，等于关闭了流水线。

**练习 2**：为什么 prefill 只对前 `stages-1` 块、而不是前 `stages` 块发起 TMA？

**参考答案**：因为主循环第 0 轮还会再发起一次预取（`preload_offset_k = 0 + (stages-1)*block_k`，即第 `stages-1` 块）。如果 prefill 已经发了 `stages` 块，主循环第 0 轮的预取就会和 prefill 重复/越界。prefill 发 `stages-1` 块 + 主循环每轮发 1 块，恰好填满 `stages` 个缓冲槽，不会重复搬运同一块。

### 4.2 多级缓冲调度：环形缓冲、双屏障与相位

#### 4.2.1 概念说明

4.1 的 Blackwell v2 是**单 warp** 错位流水线——同一个 warp 既要发 TMA 又要做 MMA，靠「错位下标」让二者在时间上重叠。但单 warp 的重叠是有限的：warp 发完 TMA 指令后仍要「穿插」去做 MMA，两种工作抢同一个 warp 的指令槽。

更彻底的做法是 **warp 专用化（warp specialization）**：把一个线程块里的 warp 分成两组，一组专门做搬运（生产者），一组专门做计算（消费者），各自跑各自的循环，**真正并行**。Hopper `matmul_v3` 就是这种写法：1 个 warp 当 TMA 生产者，4 个 warp（128 线程）当 WGMMA 消费者。

此时两个 warp 组之间没有共享的程序计数器，必须靠**显式的同步原语**通信，于是引出本模块的三件套：

- **环形缓冲**：多级共享内存，用 `stage % num_stages` 取槽；
- **双屏障（两组 mbarrier）**：一组表示「这块数据已经填好，消费者可以读了」（data-ready），另一组表示「这块数据消费者用完了，生产者可以覆盖了」（slot-free）。两组分别对应两个方向的通知；
- **相位（phase）**：每个 mbarrier 只有 0/1 两个相位，被反复复用时靠「等待相位翻转」区分是第几轮。

#### 4.2.2 核心流程

生产者—消费者的握手协议（每个 stage 重复一遍）：

```text
生产者（搬运 warp）：               消费者（计算 warp）：
  producer_acquire()                   consumer_acquire()
    └─ 等待：这块槽位空闲               └─ 等待：这块数据就绪
       （消费者用完通知过）               （生产者填好通知过）
  发起 TMA 搬运这块                     做 MMA 消费这块
  arrive（填好）→ 通知消费者            arrive（用完）→ 通知生产者
  producer_advance()                   consumer_advance()
    └─ stage 指针环转，相位按需翻转       └─ 同上
```

两组屏障的分工：

- **data-ready 屏障**：生产者搬运完到达（arrive），消费者在此等待（wait）。在 Hopper v3 里叫 `consumer_barriers`（消费者等它），count=1——因为只有 1 个生产者线程做 `arrive_and_expect_tx`，且 TMA 字节数由硬件扣减。
- **slot-free 屏障**：消费者用完到达（arrive），生产者在此等待（wait）。在 Hopper v3 里叫 `producer_barriers`（生产者等它），count=128——因为 128 个消费者线程每个都要 `arrive` 一次。

相位模型（来自 u7-l2，此处复用以解释初值）：mbarrier 初始相位为 0。`wait(barrier, phase)` 会阻塞到「当前相位 ≠ phase」。为了让生产者一开始能**无阻塞地把前 `num_stages` 块都搬进去**（缓冲起初全是空的，本就不该等消费者），生产者的初值相位取 `1`（与屏障初值 0 相反，于是第一批 wait 立刻通过）；而消费者的初值相位取 `0`（与屏障初值相同，于是必须等生产者真正填好翻转相位才通过）。这正是 mbarrier 指令组里两个常量的用途：

[python/tilus/lang/instructions/mbarrier.py:29-37](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/mbarrier.py#L29-L37) —— `producer_initial_phase = 1`（生产者起初等待离开相位 1，即等消费者释放槽位，故首批通过）、`consumer_initial_phase = 0`（消费者起初等待离开相位 0，即等生产者填好）。

#### 4.2.3 源码精读：Hopper v3 的生产者—消费者双循环

Hopper v3 把 5 个 warp（`attrs.warps = 5`，共 160 线程）分成两组：线程 128–159（warp 4）当 TMA 生产者，线程 0–127（warp 0–3）当 WGMMA 消费者。

共享内存仍是带 stage 前缀的多级缓冲：

[examples/hopper_matmul/matmul_v3.py:53-54](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L53-L54) —— `sa`/`sb` 第 0 维 `self.num_stages` 是环形缓冲。

两组屏障，count 不同，对应两个通知方向：

[examples/hopper_matmul/matmul_v3.py:57-62](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L57-L62) —— `consumer_barriers`（data-ready，count=1，生产者 `arrive_and_expect_tx`）、`producer_barriers`（slot-free，count=128，消费者 `arrive`）。

**生产者循环**（warp 4）：先 `wait(producer_barriers[stage])` 等槽位空闲（首批因相位初值=1 而立刻通过，等价于隐式 prefill），再单线程 `arrive_and_expect_tx` 声明字节数并发起两路 TMA（A、B），然后环转 stage：

[examples/hopper_matmul/matmul_v3.py:64-89](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L64-L89) —— 注意 `producer_phases` 初值为 `1`（[L66-L68](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L66-L68)），每个 stage 用一次后 `^= 1` 翻转（[L71](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L71)）；`arrive_and_expect_tx` + 两路 `tma.global_to_shared` 都绑定同一个 `consumer_barriers[stage]`（[L73-L88](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L73-L88)）。

生产者还有一段 **drain 循环**：K 维循环结束后，缓冲里还有若干「消费者尚未用完」的槽位，生产者必须继续 `wait(producer_barriers[stage])` 把它们排空，否则消费者那边的环转会死锁：

[examples/hopper_matmul/matmul_v3.py:91-96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L91-L96) —— 排空次数取 `min(num_stages, cdiv(k_size, block_k))`，避免 K 太短时多等。

**消费者循环**（warp 0–3）：`wait(consumer_barriers[stage])` 等数据就绪，`wgmma.fence`→`mma`→`commit_group`→`wait_group(0)` 完成一次 MMA，再 `arrive(producer_barriers[stage])` 通知生产者「这块我用完了」，最后环转 stage：

[examples/hopper_matmul/matmul_v3.py:98-112](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L98-L112) —— `consumer_phases` 初值 `0`（[L99-L101](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L99-L101)）；`wait_group(0)` 表示「等到所有在飞 MMA 全部完成」（关于 `wait_group(n)` 的 n 含义见 u7-l2）；128 线程每人 `arrive` 一次凑满 `producer_barriers` 的 count=128（[L110](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L110)）。

对照 4.1 的 Blackwell v2：两者都用多级缓冲掩盖延迟，但 v2 是单 warp「错位预取」（搬运与计算在同一 warp 内穿插重叠），v3 是双 warp 组「真正并行」（生产者 warp 独立跑在消费者前面）。`examples/blackwell_matmul/README.md` 与 `docs/.../v3.rst` 把这一步演进归纳为「TMA/MMA 在迭代间重叠 → 在独立 warp 上真并行」。

#### 4.2.4 代码实践：跟踪一轮握手，画出时序

1. **实践目标**：用一个具体的小例子把「双屏障 + 相位」的握手在纸面上跑一遍，确认无死锁。
2. **操作步骤**：
   - 设 `num_stages=2`、K 方向共 4 块（`cdiv(k, block_k)=4`）。
   - 画一张表，行是时间步，列是「生产者 stage / 生产者相位 / 消费者 stage / 消费者相位 / 各屏障当前相位」，逐步模拟 Hopper v3 的两个循环。
   - 重点关注：生产者首批两次 `wait(producer_barriers[0])`、`wait(producer_barriers[1])` 为何不阻塞；消费者首次 `wait(consumer_barriers[0])` 为何必须等生产者填好。
3. **需要观察的现象**：生产者会「跑在」消费者前面最多 `num_stages` 步，到第 3 步开始被 slot-free 屏障挡住，等消费者释放才继续。
4. **预期结果**：整个模拟中任何一方的 `wait` 最终都能被对方的 `arrive` 解除，无死锁；生产者比消费者始终领先不超过 `num_stages` 个 stage。
5. 若不确定相位细节，可在生成的 `source.cu` 里搜索 `mbarrier.try_wait.parity` 与 `mbarrier.arrive`，对照 PTX 验证（标注「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `consumer_barriers` 的 count 是 1，而 `producer_barriers` 的 count 是 128？

**参考答案**：`consumer_barriers` 是 data-ready 屏障，由**生产者**到达。生产者用 `arrive_and_expect_tx`（只 1 个线程发起，字节数由 TMA 引擎扣减），所以 count=1。`producer_barriers` 是 slot-free 屏障，由**消费者**到达——消费者是 128 个线程，每个线程都要 `arrive` 一次表示「我用完了」，所以 count=128。count 必须严格等于「实际会到达的线程/事务数」，否则相位永不翻转、`wait` 永久阻塞。

**练习 2**：如果删掉生产者的 drain 循环（`matmul_v3.py:91-96`），在 K 较大时会怎样？

**参考答案**：不一定立刻出错，但会破坏握手对称性。更关键的是：drain 保证生产者把「消费者尚未释放」的 stage 计数对齐，避免在下一轮（若有外层循环）或与后续 epilogue 的 `sync()` 配合时出现某个 `producer_barriers` 永远差几次到达而阻塞。简言之，drain 是流水线「排空」阶段的对称收尾，省略它会让屏障状态停在半途，容易在 `self.sync()` 处挂死。预期现象是内核可能 hang 或在某些形状下正确但脆弱——**待本地验证**。

### 4.3 Pipeline 抽象：把流水线封装成可复用类

#### 4.3.1 概念说明

4.2 里手写的生产者—消费者协议有大量样板：两组屏障、两个 stage 指针、两个相位变量、四段 acquire/advance/release 逻辑。这些样板与具体算子无关，每次写异步内核都要重抄一遍，且极易写错（count 不匹配、相位初值搞反、忘 drain）。

Tilus 把这套样板抽成 `Pipeline` 类，放在 `python/tilus/lang/classes/pipeline.py`。它是一种 `tilus.Class`——一种「像 `Script` 但不是内核」的可复用组件：

[python/tilus/lang/constructs/state.py:18-46](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/constructs/state.py#L18-L46) —— `tilus.Class` 能分配 mbarrier/共享张量/TMEM、使用全部指令；其 `__init__` 会随使用它的内核一起被转译。

`Pipeline` 暴露的就是 4.2 那套握手的高级接口：`producer_acquire/advance/release_barrier` 与 `consumer_acquire/advance/release_barrier`。使用者无需再碰原始 `wait`/`arrive` 与相位算术。

#### 4.3.2 核心流程

`Pipeline` 的使用契约（与 4.2.2 的握手一一对应）：

```text
生产者：                          消费者：
  pipe.producer_acquire()           pipe.consumer_acquire()
    └─ 等待槽位空闲                     └─ 等待数据就绪
  <做搬运，并在 pipe.producer_        <做计算>
     release_barrier() 上 arrive>     pipe.consumer_release_barrier() 上 arrive
  pipe.producer_advance()            pipe.consumer_advance()
```

- `producer_acquire()` / `consumer_acquire()`：**等待**自己的槽位就绪（生产者等空闲、消费者等数据）。
- `producer_release_barrier()` / `consumer_release_barrier()`：返回**本角色应当到达（arrive）**的那组屏障——生产者填好后用它通知消费者，消费者用完后用它通知生产者。
- `producer_advance()` / `consumer_advance()`：把本角色的 stage 指针环转 `+1 % num_stages`，并在绕回 0 时翻转相位。

两个构造参数决定两组屏障的到达计数：

- `producer_arrive_count`：生产者侧（data-ready 通知）每次有多少次到达——单线程 `arrive_and_expect_tx` 时取 1；
- `consumer_arrive_count`：消费者侧（slot-free 通知）每次有多少次到达——消费者 warp 组的总线程数，如 128。

#### 4.3.3 源码精读：`Pipeline` 类的实现

`__init__` 分配两组屏障（各 `num_stages` 个），并初始化两个 stage 指针、两个相位。相位初值直接取自 mbarrier 指令组的两个常量，复用了 4.2.2 解释的「生产者首批不阻塞、消费者首批必等待」语义：

[python/tilus/lang/classes/pipeline.py:20-32](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/classes/pipeline.py#L20-L32) —— `_full_barriers = alloc([consumer_arrive_count ...])`、`_empty_barriers = alloc([producer_arrive_count ...])`、`_producer_phase = producer_initial_phase`(=1)、`_consumer_phase = consumer_initial_phase`(=0)。

四个核心方法的接线遵循「acquire 等一组、release_barrier 给另一组」的交叉规则——生产者等 `_full_barriers`、释放 `_empty_barriers`；消费者等 `_empty_barriers`、释放 `_full_barriers`，使一方的释放恰好解除另一方的等待：

[python/tilus/lang/classes/pipeline.py:34-51](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/classes/pipeline.py#L34-L51) —— `producer_acquire` 在 `_full_barriers[producer_stage]` 上 `wait`（[L43](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/classes/pipeline.py#L43)）。

[python/tilus/lang/classes/pipeline.py:65-66](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/classes/pipeline.py#L65-L66) —— `producer_release_barrier()` 返回 `_empty_barriers[producer_stage]`（生产者搬运完后在这组上 arrive，通知消费者）。

[python/tilus/lang/classes/pipeline.py:68-85](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/classes/pipeline.py#L68-L85) —— `consumer_acquire` 在 `_empty_barriers[consumer_stage]` 上 `wait`（[L77](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/classes/pipeline.py#L77)）。

[python/tilus/lang/classes/pipeline.py:99-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/classes/pipeline.py#L99-L100) —— `consumer_release_barrier()` 返回 `_full_barriers[consumer_stage]`（消费者算完后在这组上 arrive，通知生产者）。

advance 方法做两件事：stage 指针模运算环转，并在绕回 0 时用 XOR 翻转相位：

[python/tilus/lang/classes/pipeline.py:53-63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/classes/pipeline.py#L53-L63) —— `producer_stage = (producer_stage + 1) % num_stages`、`_producer_phase ^= (producer_stage == 0)`；`consumer_advance`（[L87-L97](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/classes/pipeline.py#L87-L97)）同理。

> 命名提示：本类内部把「消费者到达、生产者等待」的那组叫 `_full_barriers`，把「生产者到达、消费者等待」的那组叫 `_empty_barriers`。读者只需记住接线规则（生产者等 `_full`/释放 `_empty`，消费者等 `_empty`/释放 `_full`），不必纠结字面「full/empty」与某个示例是否一致——关键是两组屏障分别承担「槽位空闲」与「数据就绪」两种方向相反的通知。

**如何被消费**：`examples/hopper_matmul/matmul_v4.py` 等高性能示例会内联一个等价的 `Pipeline` 类并用它改写 v3 的手写协议。对照 v3 与 v4 的消费者循环，能看到手写的 `wait(consumer_barriers[stage])` + `arrive(producer_barriers[stage])` + stage 环转，被替换成了 `tma_pipe.consumer_acquire()` + `mbarrier.arrive(tma_pipe.prev_consumer_barrier())` + `tma_pipe.consumer_advance()` 三行，样板逻辑被收进类里：

[examples/hopper_matmul/matmul_v4.py:149-151](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v4.py#L149-L151) —— 构造 `tma_pipe = Pipeline(num_stages, producer_arrive_count=1, consumer_arrive_count=128)`，与 v3 的两组屏障参数一一对应。

[examples/hopper_matmul/matmul_v4.py:184-194](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v4.py#L184-L194) —— 消费者序章：`consumer_acquire()` 等数据就绪，做首个 MMA 后 `consumer_advance()`。

#### 4.3.4 代码实践：用 `Pipeline` 重写一段手写协议

1. **实践目标**：体会抽象的收益——用 `Pipeline` 把一段手写握手改短，并验证行为不变。
2. **操作步骤**：
   - 打开 `examples/hopper_matmul/matmul_v3.py`，聚焦消费者循环（[L98-L112](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/hopper_matmul/matmul_v3.py#L98-L112)）。
   - 在脑中（或复制一份脚本到 `tilus-tutorial/` 之外自行实验，**不要改动 `examples/` 源码**）把 `self.mbarrier.wait(consumer_barriers[stage], phase=consumer_phases[stage])` 替换为 `tma_pipe.consumer_acquire()`，把 `self.mbarrier.arrive(producer_barriers[stage])` 替换为对 `tma_pipe.consumer_release_barrier()` 的 `arrive`，把 `stage = (stage+1) % num_stages; consumer_phases[stage] ^= 1` 替换为 `tma_pipe.consumer_advance()`。
   - 对照 `matmul_v4.py` 的消费者循环确认你的改写与官方抽象一致。
3. **需要观察的现象**：原本 ~6 行的手写握手被压缩成 3 行高级调用，且不再出现裸 phase 算术。
4. **预期结果**：改写后的语义与 v3 等价；`Pipeline` 把「屏障选择 + 相位翻转 + stage 环转」三件事的正确性收敛进类里，使用者只需关心 acquire/release/advance 的调用顺序。
5. 本实践为「源码阅读 + 思维改写」型；若在 Hopper GPU 上运行，可跑 v4 的 `main()` 做 correctness 校验（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`Pipeline.__init__` 里 `_producer_phase` 初值取 `producer_initial_phase`（=1）。如果有人误把它也设成 0，会出现什么现象？

**参考答案**：生产者的首批 `producer_acquire` 会 `wait(_full_barriers[0], phase=0)`，而屏障初值相位正是 0，于是 `wait` 条件「当前相位 ≠ 0」不成立，生产者会**阻塞**，等待消费者先到达 `_full_barriers`。但消费者此时也在等生产者填数据（死等），双方互锁——典型的初始化相位搞反导致的死锁。这正是 `producer_initial_phase` 取反值的必要性。

**练习 2**：`Pipeline` 把 `num_stages`、两组屏障、两个 stage 指针、两个相位都做成**实例属性**（`self.xxx`），而不是用 Python 闭包/局部变量。结合 `tilus.Class` 的转译机制，这样做的意义是什么？

**参考答案**：`tilus.Class` 的 `__init__` 会被转译器随内核一起转译成 Tilus IR——`self.xxx` 这些「赋值」实际上是在构造寄存器张量（如 `producer_stage: int32`）并跨方法共享。把状态放成实例属性，才能让 `producer_acquire`/`advance` 等多个方法读写同一组寄存器（stage 指针、相位），从而在转译后表现为内核里持久存在的标量变量。用闭包/局部变量无法跨 `tilus.Class` 方法共享这种被转译的状态。

## 5. 综合实践：对比无流水线与多级流水线

把本讲三模块串起来的综合任务：**量化多级缓冲到底带来多少收益，并解释它来自哪里。**

1. **实践目标**：在同一形状下对比「朴素（无流水线）」与「多级软件流水线」的性能，分析延迟掩盖的效果。
2. **操作步骤**：
   - 选定一个架构方向：
     - 若有 **Blackwell** GPU：用 `examples/blackwell_matmul/benchmark.py`，按 README 的用法分别跑无流水线的 v1 与多级流水线的 v2，例如 `python benchmark.py --versions v1 v2 --size 8192 8192 8192`，记录 latency 与 TFLOPS。
     - 若有 **Hopper** GPU：对比 `examples/hopper_matmul/` 里无/少流水线的版本与 `matmul_v3.py`（warp 专用化流水线）的 `main()` 输出。
   - 若无相应 GPU，做源码对照分析：把 `matmul_v1.py`（朴素：搬一块算一块、无 stage 维）与 `matmul_v2.py`（多级缓冲 + prefill + 错位预取）并排阅读，列出 v2 相对 v1 新增的「多级缓冲 / prefill / 错位预取 / 相位」四类机制各自的作用。
3. **需要观察的现象**：多级流水线版本的延迟显著低于朴素版本，TFLOPS 明显更高；且在能跑的前提下，适当增大 `num_stages`（如 2→3→4）延迟进一步下降，直到共享内存压力反噬。
4. **预期结果**：性能提升主要来自「计算与搬运重叠」——朴素版张量核在搬运时空闲，多级版用预取把搬运塞进了计算的时间隙。可结合 `ncu` 报告（`/ncu-report` skill 或 `benchmark.py --ncu`）观察「warp 等待全局访存」类指标（如 stall 长程停顿）的下降。
5. 若无可用硬件，明确标注「待本地验证」，仅完成源码对照分析部分即可。

## 6. 本讲小结

- 软件流水线的目的是让**搬运与计算在时间上重叠**，用多份共享内存缓冲把长延时的全局访存藏进计算的时间隙，缓解显存带宽瓶颈。
- 实现需要三件套：**多级环形缓冲**（`shape=[num_stages, ...]`，用 `stage % num_stages` 取槽）、**异步搬运**（TMA/`cp_async`，发起即返回）、**错位调度**（算第 i 块同时预取第 i+num_stages-1 块）。
- 两种真实写法：Blackwell v2 是**单 warp 错位预取**（prefill + 主循环「预取远处块 / 算当前块」）；Hopper v3 是**warp 专用化**（生产者 warp 跑 TMA、消费者 warp 跑 MMA，真正并行），后者依赖**双屏障 + 相位**在生产者与消费者间握手。
- **双屏障**分别承担 data-ready（生产者到达、消费者等待）与 slot-free（消费者到达、生产者等待）两个方向的通知；count 必须等于实际到达数（Hopper v3 中为 1 与 128）。
- **相位**让单个 mbarrier 被反复复用：`producer_initial_phase=1` 使生产者首批不阻塞（隐式 prefill），`consumer_initial_phase=0` 使消费者必须等数据真正就绪；环转回 stage 0 时翻转相位。
- `lang/classes/pipeline.py` 的 `Pipeline(tilus.Class)` 把上述样板封装为 `producer/consumer_acquire/advance/release_barrier` 高级接口，被 v4 及之后的高性能示例采用——先读懂手写 v2/v3，就能完全读懂这个抽象。

## 7. 下一步学习建议

- **动手采用抽象**：阅读 `examples/hopper_matmul/matmul_v4.py` 与 `examples/blackwell_matmul/matmul_v4.py`，看它们如何用 `Pipeline` 类 + tile 光栅化（swizzle）把 v3 的手写协议改写得既短又快。
- **更深的流水线**：`matmul_v5`/`v6`（Blackwell）把流水线思想用到 epilogue 与多级生产者—消费者（TMA pipe、MMA pipe、CLC pipe 各一个 `Pipeline`），是「多级流水线」的进阶范本；对应 `docs/source/tutorials/matmul-blackwell/v5.rst` 的两级流水线图值得一看。
- **回到工程化**：流水线深度 `num_stages` 是 autotune 的关键搜索维度（u2-l4、u8-l2）。学完本讲后，可去 `python/tilus/lang/instantiated_script.py` 与 `examples/**/benchmark.py` 看 `num_stages` 如何进入调优空间、dispatch 缓存如何记录选中的级数。
- **调试手段**：用 `tilus.option.debug.dump_ir()`（u8-l4）导出流水线内核各 Pass 后的 IR，在 `source.cu` 里搜索 `mbarrier.try_wait.parity` / `cp.async.bulk` / `wgmma` 验证你预想的「预取—等待—计算」时序是否真的被生成。
