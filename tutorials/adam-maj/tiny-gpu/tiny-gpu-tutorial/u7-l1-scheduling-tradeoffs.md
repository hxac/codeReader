# 被简化的机制：分支分歧、流水线、warp 调度

## 1. 本讲目标

本讲是 Unit 7（架构取舍与扩展方向）的第一篇，也是整本手册第一次「向后看」——前六单元我们一直在解释 tiny-gpu **做了什么**，本讲要解释它**故意没做什么**，以及这些被砍掉的机制在真实 GPU 里为何至关重要。学完本讲后，你应该能够：

1. 精确定位 `scheduler` 里的 **PC 收敛假设**（`current_pc <= next_pc[THREADS_PER_BLOCK-1]`），并解释它为什么是整个调度模型的「天真基础」。
2. 说清「无流水线」「单 block 串行」「无 warp」三件事各自的含义，以及它们共同造成的资源浪费。
3. 用自己的话讲明白 **分支分歧（branch divergence）**：它为什么会发生、真实 GPU 如何用掩码/重汇聚栈处理、tiny-gpu 为什么干脆不支持。
4. 在源码与 README 里找到所有标注「简化/TODO/WIP」的位置，并能据此判断「哪些内核在当前硬件上一定跑错」。
5. 设计一个会让当前 tiny-gpu **算错** 的分支分歧内核，并逐拍解释错误是如何发生的。

## 2. 前置知识

本讲是 advanced 层级，不引入新的硬件部件，而是把已学过的部件放回「真实 GPU」的坐标系里重新审视。请确认你已经掌握下面三件事，否则建议先补对应讲义。

### 2.1 scheduler 的七阶段状态机

在 [u4-l2](u4-l2-scheduler-fsm.md) 里我们讲过：scheduler 把执行一条指令切成 `IDLE→FETCH→DECODE→REQUEST→WAIT→EXECUTE→UPDATE→DONE` 七个阶段，用一个 3 位信号 `core_state` 广播给 core 内所有子模块。本讲要反复回到其中的 **UPDATE 阶段**——因为 PC 收敛假设就藏在那里。如果你对 `core_state` 如何驱动 fetcher / decoder / LSU / ALU / PC 还不熟，请先读 [u4-l2](u4-l2-scheduler-fsm.md)。

### 2.2 PC 与 next_pc 的计算

在 [u5-l4](u5-l4-registers-pc.md) 里我们讲过：每个线程有自己的 PC 单元，在 `EXECUTE` 阶段计算 `next_pc`——默认 `PC+1`，遇到 `BRnzp` 且 `(nzp & decoded_nzp) != 0` 时跳到立即数。关键点是：**`next_pc` 是「每线程一份」的数组**，但 scheduler 只从中读取一个固定下标。这个「每线程算、但只取一个」的不对称，正是本讲 4.1 节的核心。

### 2.3 SIMD 与「单指令流」

在 [u1-l1](u1-l1-project-overview.md) 与 [u4-l1](u4-l1-core-anatomy.md) 里我们建立过 tiny-gpu 的 SIMD 模型：一个 core 共享**一条指令流**（单实例的 fetcher / decoder / scheduler），但为**每个线程复制一份数据通路**（ALU / LSU / registers / PC）。SIMD 的前提是「同一条指令同时作用于所有线程」——本讲会反复追问：**如果不同线程想走不同的下一条指令，这个前提还成立吗？**

> 一句话提醒：本讲讨论的「简化」**不是 bug，而是设计选择**。tiny-gpu 的目标是「用最少代码讲清 GPU 原理」，每砍掉一个机制，代码量就少一截、可读性就高一截，但也离真实 GPU 远一截。理解这条取舍链，是本讲的真正目的。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/scheduler.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv) | core 控制流状态机 | **本讲主角**：UPDATE 阶段的 PC 收敛代码、顶部关于分支分歧的注释、无流水线的七阶段串行 |
| [src/core.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv) | 计算核心 | 单实例 scheduler、`generate` 循环为每线程复制执行单元（无 warp 切分） |
| [src/pc.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv) | 程序计数器 | 每线程 `next_pc` 如何算出、顶部注释里的「不支持分支分歧」 |
| [src/dispatch.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv) | block 派发器 | 「一个 core 一次一个 block」的串行派发/回收逻辑 |
| [README.md](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md) | 项目文档 | Advanced Functionality 章节对流水线 / warp / 分支分歧 / barrier 的原文描述 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块。它们不是平行的五块，而是一条递进的「天真链」：**4.1 的 PC 收敛假设**是地基，**4.2 / 4.3** 是它在时间维度（无流水线）与空间维度（单 block）上的直接后果，**4.4 分支分歧**是 PC 收敛假设被打破时会发生什么，**4.5 warp 调度**则是真实 GPU 用来同时缓解 4.2 / 4.4 的进阶机制。

### 4.1 PC 收敛假设：调度器最核心的天真假设

#### 4.1.1 概念说明

回顾 [u5-l4](u5-l4-registers-pc.md)：每个线程在 `EXECUTE` 阶段都会算出自己的 `next_pc`——有的线程可能是 `PC+1`，有的线程（命中 `BRnzp`）可能跳到某个立即数。所以严格来说，`next_pc` 是一个长度为 `THREADS_PER_BLOCK` 的数组，**每个元素可能不一样**。

那么 scheduler 在 UPDATE 阶段把「全 core 共享的 `current_pc`」更新成什么呢？理论上，如果不同线程的 `next_pc` 不同，你就不能再维护一个单一的 `current_pc`——你必须允许线程分裂成多条执行路径。tiny-gpu 没有这样做。它做了一个**天真假设**：

> **PC 收敛假设**：所有线程在每条指令结束后都汇聚到**同一个** PC。因此 scheduler 只需从 `next_pc` 数组里挑一个值，就能代表整个 block 的下一条指令地址。

这个假设一旦成立，整个 SIMD 模型就极其简单：一条指令流、一个 PC、所有线程齐步走。代价是——一旦有内核让不同线程真的想走不同 PC，硬件就会悄悄算错。这就是 4.4 节要讲的分支分歧。

#### 4.1.2 核心流程

PC 收敛假设在数据通路上的流程是：

```
每线程 PC 单元 (pc.sv)              scheduler (scheduler.sv)
─────────────────────              ─────────────────────────
EXECUTE 拍:                        UPDATE 拍:
  各自算 next_pc[i]                  读取 next_pc[THREADS_PER_BLOCK-1]
  (可能 PC+1，可能跳转)               （只取最后一个线程的下标！）
       │                                  │
       └──────────► next_pc[] ────────────┘
                                          │
                                          ▼
                          current_pc <= next_pc[TPB-1]   ← 全 core 共享
                                          │
                                          ▼
                          下一拍 FETCH 用这个 current_pc 去取「同一条」指令
```

关键的不对称是：**计算时每线程一份，使用时只取一个固定下标 `THREADS_PER_BLOCK-1`**。注意它取的不是「最后一个**活跃**线程」，而是**固定下标 `TPB-1`**——哪怕这个 block 实际只有 2 个线程，它也读 `next_pc[3]`。这个细节在 4.1.4 的实践里会引出一个值得思考的边界情况。

#### 4.1.3 源码精读

scheduler 顶部注释把这条假设写得非常坦白（[src/scheduler.sv:14-15](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L14-L15)）：

```systemverilog
// > Technically, different instructions can branch to different PCs, requiring "branch divergence." In
//   this minimal implementation, we assume no branch divergence (naive approach for simplicity)
```

而假设真正落地的地方，是 UPDATE 分支里的这一行（[src/scheduler.sv:97-108](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L97-L108)）：

```systemverilog
UPDATE: begin 
    if (decoded_ret) begin 
        // If we reach a RET instruction, this block is done executing
        done <= 1;
        core_state <= DONE;
    end else begin 
        // TODO: Branch divergence. For now assume all next_pc converge
        current_pc <= next_pc[THREADS_PER_BLOCK-1];

        // Update is synchronous so we move on after one cycle
        core_state <= FETCH;
    end
end
```

阅读这行代码时要抓住三件事：

1. `next_pc` 是输入端口声明的数组（[src/scheduler.sv:34](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L34)），每线程一格；
2. 下标写死成 `THREADS_PER_BLOCK-1`，注释 `// TODO: Branch divergence` 直接承认这是一个待办；
3. `current_pc` 是单数（[src/scheduler.sv:33](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L33)），全 core 共享，下一拍喂给 fetcher 取指。

`pc.sv` 的顶部注释呼应了同一个假设（[src/pc.sv:4-9](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L4-L9)）：

```systemverilog
// PROGRAM COUNTER
// > Calculates the next PC for each thread to update to (but currently we assume all threads
//   update to the same PC and don't support branch divergence)
```

而每个线程的 `next_pc` 实际计算在 `EXECUTE` 拍完成（[src/pc.sv:42-55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L42-L55)）：默认 `current_pc + 1`，`BRnzp` 命中 `(nzp & decoded_nzp) != 0` 时改为 `decoded_immediate`。

> 一句话总结：**计算端是「每线程一份」，消费端是「只取一格」**——PC 收敛假设就是连接这两端的那个粗暴的一对一映射。

#### 4.1.4 代码实践

**实践目标**：亲手确认「scheduler 真的只读 `next_pc` 的最后一个下标」，并发现一个潜在边界。

**操作步骤**：

1. 打开 [src/scheduler.sv:97-108](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L97-L108)，确认 UPDATE 分支里 `current_pc` 的来源是 `next_pc[THREADS_PER_BLOCK-1]`。
2. 打开 [src/core.sv:49](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L49)，确认 `next_pc` 是长度为 `THREADS_PER_BLOCK` 的数组；再追到 [src/core.sv:208](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L208)，确认每个线程的 PC 实例各自驱动 `next_pc[i]` 一格。
3. 现在思考一个边界：假设某次内核 `thread_count=6`、`THREADS_PER_BLOCK=4`，那么最后一个 block 只有 2 个线程。结合 [src/core.sv:139](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L139) 的 `enable(i < thread_count)` 与 [src/pc.sv:36-40](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L36-L40)（`enable=0` 时 `next_pc` 不更新、保持复位值 0），推导：scheduler 此刻读到的 `next_pc[3]` 是多少？

**需要观察的现象 / 预期结果**：

- 第 3 步的推导结论应是：不活跃线程（`i >= thread_count`）的 `next_pc` 永远停在复位值 `0`，于是 scheduler 在尾 block 里会把 `current_pc` 更新成 `0`，导致 PC 跳回程序开头。
- 这说明 PC 收敛假设还有一个**隐含前提**：「最后一个线程必须是活跃线程」。官方自带的两个内核（`matadd` 8 线程、`matmul` 4 线程，`TPB=4`）恰好都是满 block，回避了这个问题。
- **待本地验证**：写一个 `thread_count=6` 的小内核实际仿真，观察尾 block 的 PC 是否真的在日志里出现「跳回 0」的现象（读法见 [u6-l2](u6-l2-execution-trace.md)）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `current_pc <= next_pc[THREADS_PER_BLOCK-1]` 改成 `current_pc <= next_pc[0]`，对 `matmul` 内核的运行结果有影响吗？为什么？

> **答案**：没有影响。因为 `matmul` 里所有线程在每个分支点都走同一条路（所有 `next_pc[i]` 相等），取哪个下标都一样——这正是 PC 收敛假设「成立」的情形。这也解释了为什么官方内核能在当前硬件上跑对。

**练习 2**：`next_pc` 为什么设计成「每线程一份」而不是直接「全 core 一个」？

> **答案**：因为 `BRnzp` 是否跳转依赖每个线程自己的 NZP 寄存器（由 CMP 写入），而各线程的数据不同，比较结果可能不同。硬件在「计算层」老老实实为每个线程各算一次 `next_pc`，是在为将来支持分支分歧留接口；只是在「消费层」用 PC 收敛假设把它压扁了。

---

### 4.2 无流水线：指令的串行执行

#### 4.2.1 概念说明

回头看 scheduler 的七阶段状态机：`FETCH→DECODE→REQUEST→WAIT→EXECUTE→UPDATE`。这条链是**一条指令**的生命周期。关键问题是：**第 N+1 条指令的 FETCH，能在第 N 条指令的 WAIT 期间就开始吗？**

答案是不能。tiny-gpu **没有流水线**：scheduler 必须等一条指令完整跑完 UPDATE（或进入 DONE），才会回到 FETCH 取下一条。换句话说，这六个阶段构成的是一道**屏障（barrier）**，而不是一条流水线（pipeline）。

真实 GPU（以及现代 CPU）都用流水线来重叠多条指令的执行：当指令 N 在 WAIT 等内存时，指令 N+1 可以同时在做 DECODE，指令 N+2 可以在 FETCH——各阶段像工厂流水线一样并行推进，只要相邻指令没有数据依赖。tiny-gpu 完全不做这种重叠，于是执行单元在 WAIT 期间就只能干等。

#### 4.2.2 核心流程

串行执行的时间线如下（每格 1 拍，`WAIT` 长度可变）：

```
指令 N:   |FETCH|DECODE|REQUEST|WAIT ......... |EXECUTE|UPDATE|
指令 N+1:                                          |FETCH|DECODE|...
                                                  ▲
                                          必须等 N 走到 UPDATE 完，N+1 才能 FETCH
                                          （ALU/LSU 在 WAIT 期间全部闲置）
```

对比理想流水线（本讲用来说明差距，**tiny-gpu 并未实现**）：

```
指令 N:   |FETCH|DECODE|REQUEST|WAIT |EXECUTE|UPDATE|
指令 N+1:       |FETCH|DECODE |REQUEST|WAIT |EXECUTE|UPDATE|
指令 N+2:              |FETCH |DECODE |REQUEST|WAIT |...
                        ↑ 同一拍里不同指令处于不同阶段，资源被填满
```

沿用 [u4-l2](u4-l2-scheduler-fsm.md) 的记号，一条指令的耗时是：

\[
T_{\text{inst}} = t_{\text{FETCH}} + t_{\text{WAIT}} + 4
\]

其中 `4` 是 `DECODE/REQUEST/EXECUTE/UPDATE` 四个固定单拍阶段，`t_FETCH` 与 `t_WAIT` 是变长握手。在串行模型下，一个 block 跑完 K 条指令的总周期数就是把 K 个 `T_inst` 逐条相加——`WAIT` 造成的等待**完全无法被下一条指令重叠吸收**。这就是为什么内存密集型内核（如 `matmul` 的循环体里有两条 `LDR`）在 tiny-gpu 上跑得明显更慢。

#### 4.2.3 源码精读

无流水线的根因就在 scheduler 的状态机写法里。注意 UPDATE 之后回到的是 `FETCH`，且 `core_state` 是**单数**（[src/scheduler.sv:37](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L37)），全 core 同一时刻只能处于**一个**阶段：

```systemverilog
UPDATE: begin 
    ...
    end else begin 
        current_pc <= next_pc[THREADS_PER_BLOCK-1];
        // Update is synchronous so we move on after one cycle
        core_state <= FETCH;   // ← 回到 FETCH 取「下一条」指令，绝不与当前指令重叠
    end
end
```

因为是「单 `core_state` + 单 `instruction` + 单 `current_pc`」，硬件里根本**没有地方**同时保存两条指令的进度。fetcher 也只在 `FETCH` 拍工作（[src/scheduler.sv:63-68](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L63-L68)），ALU 只在 `EXECUTE` 拍工作——它们在彼此的阶段里都是闲置的。

README 在两处明确点出了这一点。Scheduler 小节（[README.md:137](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L137)）说 tiny-gpu 「executes instructions for all threads in-sync and sequentially」；Advanced Functionality 的 Pipelining 小节（[README.md:354-360](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L354-L360)）则直接指出：

> In the control flow for tiny-gpu, cores wait for one instruction to be executed on a group of threads before starting execution of the next instruction. ... This helps to maximize resource utilization within cores as resources are not sitting idle while waiting (ex: during async memory requests).

最后一句「resources are not sitting idle while waiting」描述的正是流水线**想要解决**的问题——而 tiny-gpu 选择不解决。

#### 4.2.4 代码实践

**实践目标**：从仿真日志里量化「WAIT 期间执行单元闲置」的浪费。

**操作步骤**：

1. 按 [u1-l3](u1-l3-build-and-simulation.md) 跑 `make test_matmul`，打开 `test/logs/` 下最新日志。
2. 用 [u6-l2](u6-l2-execution-trace.md) 学到的方法，定位到循环体里某条 `LDR` 指令所在的 cycle。
3. 从该 cycle 起，逐拍统计 `core_state` 停在 `WAIT` 连续多少拍；同时观察这几拍里 ALU 相关字段（`RS/RT`）是否变化。

**需要观察的现象 / 预期结果**：

- `core_state` 会在 `WAIT` 停留多拍（因为要等两条 `LDR` 经 controller 往返，详见 [u3-l2](u3-l2-memory-controller.md) 与 [u5-l3](u5-l3-lsu-async-memory.md)）。
- 这几拍里 ALU 没活干、寄存器堆也没活干——它们在等内存。这就是无流水线最直观的代价。
- **待本地验证**：记下你观察到的 `WAIT` 拍数，用它估算 `matmul` 一个线程跑完整条循环比「理想流水线」多花了多少周期。

#### 4.2.5 小练习与答案

**练习 1**：为什么即便没有数据依赖，tiny-gpu 也无法让指令 N+1 在指令 N 的 WAIT 期间开始 FETCH？

> **答案**：因为整个 core 只有一份 `core_state`、一份 `current_pc`、一份 `instruction` 寄存器（[src/core.sv:43-45](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L43-L45)）。要重叠两条指令，就需要两套这样的状态分别跟踪各指令的阶段——也就是流水线寄存器，tiny-gpu 没有这些。

**练习 2**：如果把六阶段改成真正的六级流水线，最先遇到的障碍是什么？

> **答案**：是 `WAIT` 的「变长」与「全线程同步」特性。scheduler 的 WAIT 要等**所有**线程的 LSU 收到应答（[src/scheduler.sv:77-92](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L77-L92)），等待拍数不固定；变长阶段很难直接塞进定宽流水线，需要额外的冒险（hazard）检测与暂停（stall）逻辑——这正是真实 GPU 流水线控制器要解决的难题。

---

### 4.3 单 block 串行处理：core 一次只啃一个 block

#### 4.3.1 概念说明

PC 收敛假设限定了一个 core **内部**「一个 block 内所有线程齐步走」。本节把视角抬高一档，看 core **之间**与 block **之间**的调度：dispatcher 把线程切成 block 后，**一个 core 一次只处理一个 block，跑完才能接下一个**。这一点 README 说得很直白（[README.md:137](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L137)）：*“executes instructions for a single block to completion before picking up a new block”*。

这条规则叠加 4.2 的「无流水线」，意味着：**同一个 core 上，block A 的最后一条指令与 block B 的第一条指令也不会重叠**。core 的全部执行单元（ALU/LSU/registers/PC × TPB）在 block 切换的间隙里会被 reset 清零、重新装填。

#### 4.3.2 核心流程

dispatcher 的主循环在每个上升沿做两件事——「派发」与「回收」（[src/dispatch.sv:65-89](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L65-L89)）：

```
每个上升沿:
  ┌─ 派发循环 (for each core i):
  │    if core_reset[i]:                     # 该 core 空闲
  │        if 还有 block 未派:                 # 给它派一个新 block
  │            core_start[i]   <= 1
  │            core_block_id[i]<= blocks_dispatched
  │            blocks_dispatched += 1
  │
  └─ 回收循环 (for each core i):
       if core_start[i] && core_done[i]:     # 该 core 跑完当前 block
           core_reset[i] <= 1                # 复位，下一拍才能接新 block
           core_start[i] <= 0
           blocks_done   += 1
```

关键约束是 `core_done`——它来自 core 内 scheduler 跑到 `RET` 后拉起的 `done`（[src/scheduler.sv:98-101](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L98-L101)）。也就是说，**只有当一个 block 完整跑到 RET，dispatcher 才会回收这个 core 并考虑派下一个 block**。这就是「串行」的硬件表达。

#### 4.3.3 源码精读

core 端口只接收**一个** `block_id` 和**一个** `thread_count`（[src/core.sv:22-24](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L22-L24)），没有「下一个 block 预取」的接口；core 内部也只有**一个** scheduler 实例（[src/core.sv:113-129](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L113-L129)）。dispatcher 端的派发/回收逻辑（[src/dispatch.sv:65-89](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L65-L89)）配合 `core_reset` 握手，保证一个 core 在任一时刻只背着至多一个 block。

把 4.1 / 4.2 / 4.3 串起来，得到 tiny-gpu 调度模型的完整画像：

| 维度 | tiny-gpu 的选择 | 真实 GPU 的进阶做法 |
| --- | --- | --- |
| block 内 PC | **PC 收敛**：一个 PC 代表全部线程 | 分支分歧 + 重汇聚栈 |
| 指令间 | **串行**：一条跑完再取下一条 | 流水线 |
| block 间 | **串行**：一个 core 一次一个 block | warp 调度（见 4.5） |

#### 4.3.4 代码实践

**实践目标**：用 [u2-l2](u2-l2-kernel-launch-dcr-dispatcher.md) 的参数手算 dispatcher 的时间线，体会「block 串行」如何拉长总耗时。

**操作步骤**：

1. 假设 `thread_count=8`、`THREADS_PER_BLOCK=4`、`NUM_CORES=2`。先用 [src/dispatch.sv:30-31](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L30-L31) 的公式算 `total_blocks`。
2. 画出每个周期每个 core 背着的 `block_id`，直到 `blocks_done == total_blocks`。
3. 标注：两个 block 是真正「并行」（分别落在 core 0 / core 1）还是「串行」（落在同一个 core 的不同时间）。

**需要观察的现象 / 预期结果**：

- `total_blocks = (8+4-1)/4 = 2`，恰好 2 个 block 分给 2 个 core，每个 core 一个，二者并行。
- 但如果改成 `thread_count=12`，则 `total_blocks=3`，2 个 core 先并行啃掉 2 个 block，第 3 个 block 必须等某个 core 回收后才能上——这就是「block 串行」造成的排队。
- 这个手算过程纯逻辑推导，结论确定；若要核对，可在 dispatcher 源码里逐步对照。

#### 4.3.5 小练习与答案

**练习 1**：为什么 dispatcher 用「复位 core 再派下一个 block」而不是「让 core 同时背着两个 block 交替执行」？

> **答案**：因为 core 内只有一个 scheduler、一套 `core_state` 与一套 `current_pc`（见 4.3.3），硬件上没有「第二套上下文」来同时推进第二个 block。要让一个 core 同时处理多个 block，就需要 warp 调度那套机制（4.5 节）——为每个 warp 保存独立的 PC / 寄存器映像，按时分片切换。

**练习 2**：在 `NUM_CORES=1` 时，「block 串行」会让总耗时变成多少？

> **答案**：所有 block 只能在一个 core 上依次跑完，总耗时约为 `Σ(每个 block 的指令总周期)`，block 之间无任何并行——这是单核 tiny-gpu 的最慢情形，也最能体现 block 间串行的代价。

---

### 4.4 分支分歧：被刻意省略的关键机制

#### 4.4.1 概念说明

现在正面回答 4.1 留下的问题：**如果不同线程真的想走不同的 PC，会发生什么？**

设想一个 SIMD block 里有 4 个线程，执行到一条 `BRz`（「结果为零则跳转」）。线程 0 刚才 `CMP` 出「相等」（Z 位为 1），想跳转；线程 1/2/3 是「不等」（Z 位为 0），不想跳转。此时它们的 `next_pc` 不再相同：线程 0 想去 `LOOP`，其余线程想去 `PC+1`。

这就是**分支分歧（branch divergence）**：同一个 block 内的线程，因为各自的数据不同，在同一条分支指令上走向了不同的 PC。README 的 Branch Divergence 小节（[README.md:368-372](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L368-L372)）把它定义为：*“individual threads could diverge from each other and branch to different lines based on their data.”*

真实 GPU 如何处理？它不分裂硬件，而是**时分**：先执行「跳转」那一支，把其余线程用掩码（predicate mask）暂时禁用；等这一支走到汇聚点，再回头执行「不跳转」那一支；最后所有线程在汇聚点重新合流（reconverge）。整个过程需要维护一个「哪些线程活跃」的位掩码和一个记录嵌套分支的重汇聚栈。

tiny-gpu 完全没有这套机制。它用 PC 收敛假设把分歧直接压扁：scheduler 只读 `next_pc[TPB-1]`，**所有线程被迫跟随最后一个线程的选择**。于是分歧内核会**悄悄算错**，而且不报错——这正是本讲实践要构造的场景。

#### 4.4.2 核心流程

分支分歧在「真实 GPU」与「tiny-gpu」上的对照流程：

```
线程:        T0          T1          T2          T3
CMP 结果:    相等(Z=1)    不等(Z=0)   不等(Z=0)   不等(Z=0)
next_pc:     跳LOOP      PC+1        PC+1        PC+1   ← 分歧出现

【真实 GPU 做法】用活跃掩码分两段串行执行:
   段A: T0 走 LOOP 分支;        T1/T2/T3 掩码禁用
   段B: T1/T2/T3 走 PC+1 分支;  T0 掩码禁用
   汇聚点: 4 个线程重新合流           ← 结果正确

【tiny-gpu 做法】PC 收敛，只取 next_pc[TPB-1] = next_pc[T3] = PC+1:
   所有线程都走 PC+1, T0 想跳的 LOOP 分支被丢弃     ← 结果错误
```

注意 `matmul` 内核（[README.md:289-306](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L289-L306)）里也用了 `CMP + BRn` 循环，但它**不发散**：所有线程的 `k` 都从 0 走到 `N`，在每个分支点都做同样的跳转决定，所以 `next_pc[i]` 全都相等——PC 收敛假设恰好成立。README 特意注明（[README.md:264](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L264)）：*“all branches converge so this kernel works on the current tiny-gpu implementation”*。这是「能跑」与「发散」的分界线。

#### 4.4.3 源码精读

scheduler 里有两处关于分支分歧的标注。第一处在顶部注释（[src/scheduler.sv:14-15](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L14-L15)），明言这是「naive approach for simplicity」；第二处就是 UPDATE 分支里那行 `// TODO: Branch divergence`（[src/scheduler.sv:103-104](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L103-L104)）。`pc.sv` 顶部也呼应（[src/pc.sv:5-6](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L5-L6)）：*“we assume all threads update to the same PC and don't support branch divergence”*。

更宏观地，README 把「Add basic branch divergence」列进了未来计划（[README.md:386](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L386)），说明作者清楚这是一项**待补全的核心能力**，而非细节疏漏。

把这三处标注和 4.1 的代码事实合起来，结论非常清楚：

> 在 tiny-gpu 上写内核，**必须保证 block 内所有线程在每个分支点走同一条路**。任何依赖线程数据（如 `%threadIdx`）产生不同跳转结果的内核，都会因为 `current_pc <= next_pc[TPB-1]` 而得到错误结果。

#### 4.4.4 代码实践

**实践目标**：定位所有「分支分歧」相关的源码标注，理解它们对内核作者的约束。

**操作步骤**：

1. 在 `src/` 下搜索 `branch divergence`、`TODO`、`converge`，把命中的文件与行号列出来（应至少包含 `scheduler.sv` 两处、`pc.sv` 一处）。
2. 打开 [README.md:368-372](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L368-L372) 的 Branch Divergence 小节，把它与 [README.md:264](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L264) 对 `matmul`「all branches converge」的说明对照阅读。
3. 用一句话写下：`matmul` 的循环为什么「不发散」，而一个「偶数线程跳转、奇数线程不跳转」的内核为什么「发散」。

**需要观察的现象 / 预期结果**：

- 你会发现所有官方内核都刻意回避了发散；这不是巧合，而是 PC 收敛假设对内核设计的硬约束。
- 区分「不发散」的关键是：**分支条件是否依赖会因线程而异的数据**。`matmul` 的循环条件 `k < N` 对所有线程都相同（`k` 与 `N` 不依赖 `%threadIdx` 的差异），故不发散。

#### 4.4.5 小练习与答案

**练习 1**：真实 GPU 处理分支分歧时，「先执行跳转支、再执行不跳转支」会带来什么性能代价？

> **答案**：两段串行执行，相当于把分支处的并行度减半——发散的线程在对方那一支里被掩码禁用、执行单元空转。发散越深、重汇聚点越远，浪费越多。这也是为什么 GPU 编程指南总建议「尽量让同一 warp 内的线程走同一条分支」。

**练习 2**：如果要在 tiny-gpu 上「最小化」地支持分支分歧，scheduler 的 UPDATE 阶段至少要新增什么？

> **答案**：至少需要 (1) 一组「活跃线程掩码」，记录当前哪些线程还在执行；(2) 当检测到 `next_pc` 不全相等时，选一个 PC 值继续、同时把走向其他 PC 的线程从掩码里摘掉；(3) 一个重汇聚点记录，等被摘掉的线程重新激活。这远不止改一行 `current_pc <= next_pc[...]` 就能完成。

---

### 4.5 Warp 调度：被简化掉的并发维度

#### 4.5.1 概念说明

4.2 节我们看到：无流水线导致 core 在 `WAIT` 等内存时，执行单元全部闲置。真实 GPU 还有一招来填这些空隙——**warp 调度（warp scheduling）**。

一个 block 包含很多线程，真实 GPU 把它们切成若干个固定大小的**线程束（warp，NVIDIA 术语里通常是 32 线程一组）**，让一个 core 同时「背负」多个 warp。调度器的把戏是：**当 warp A 在等内存时，立刻切换到 warp B 执行它的指令**。这样 core 的执行单元始终有事干，内存延迟被「别的 warp 的计算」隐藏掉了——这叫**延迟隐藏（latency hiding）**。

warp 调度与流水线（4.2）常被放在一起，但层次不同：流水线是「**同一线程**的多条指令」在重叠；warp 调度是「**同一 core**的多个 warp」在时分切换。README 把两者并列（[README.md:139](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L139)）：*“pipelining ... warp scheduling can be use to execute multiple batches of threads within a block in parallel.”*

tiny-gpu 没有 warp 概念——**一个 block 就是它的「唯一 warp」**，且 core 一次只背一个 block（4.3）。所以它既不能在指令间重叠，也不能在 warp 间切换，内存延迟完全暴露。

#### 4.5.2 核心流程

warp 调度的时分切换示意（**tiny-gpu 未实现，仅作对照**）：

```
warp A: |FETCH|DECODE|REQ|WAIT .............. |EXEC|UPDATE|
warp B:                       |FETCH|DECODE|EXEC|...|     ← A 等
warp C:                                   |FETCH|...|     ← 内存时，切到 B/C
        └─ core 执行单元几乎不闲置，内存延迟被 B/C 的计算填满
```

对比 tiny-gpu 的实际行为（4.2 + 4.3）：

```
block A (唯一 warp): |FETCH|DECODE|REQ|WAIT .............. |EXEC|UPDATE|
                                       ↑ 执行单元全闲置，无人顶替
```

要想支持 warp 调度，硬件必须为**每个 warp** 独立保存一套 PC、寄存器映像与 `core_state`，调度器在每个周期挑一个「就绪」的 warp 来执行。这套「上下文」的规模与切换逻辑，正是 tiny-gpu 为了「tiny」而砍掉的并发维度。

#### 4.5.3 源码精读

tiny-gpu 没有 warp 的直接证据在 core 的实例化结构里。注意 scheduler 是**单实例**（[src/core.sv:113-129](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L113-L129)），`generate` 循环（[src/core.sv:132-211](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L132-L211)）只为**每个线程**复制执行单元，没有任何「按 warp 分组、保存多套 PC 映像」的结构。换句话说：

- `current_pc`（[src/core.sv:48](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L48)）全 core **唯一**，不存在「每 warp 一个 PC」；
- `core_state`（[src/core.sv:43](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L43)）全 core **唯一**，不存在「每 warp 一个状态机」。

README 的 Warp Scheduling 小节（[README.md:362-366](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L362-L366)）把这条进阶路径写得很清楚：

> Multiple warps can be executed on a single core simultaneously by executing instructions from one warp while another warp is waiting. This is similar to pipelining, but dealing with instructions from different threads.

并把它与流水线一起列进了未来计划（[README.md:388](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L388)）。结合 4.3 的 dispatcher 可知：tiny-gpu 的一个 core 在 block 切换时还要整个 reset，连「保留多个 block 的上下文」都没做到，更谈不上 warp 级的时分切换。

#### 4.5.4 代码实践

**实践目标**：在源码里确认「不存在 warp 级上下文」，从而理解 warp 调度为何无法「小改」实现。

**操作步骤**：

1. 在 [src/core.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv) 里统计：`current_pc`、`core_state`、`fetcher_state`、`instruction` 各有几份（答案：各 1 份，全 core 共享）。
2. 再统计：`registers` / `lsu_state` / `next_pc` 各有几份（答案：各 `THREADS_PER_BLOCK` 份，每线程一份）。
3. 思考：要支持 2 个 warp 时分切换，上述「单份」的信号各需要复制几套？

**需要观察的现象 / 预期结果**：

- 「全 core 共享」的那几个信号，正是 warp 调度必须改成「每 warp 一份」的对象。它们今天之所以能共享，正是因为只有一个 warp 在跑。
- 这一练习是纯结构阅读，结论确定。它说明 warp 调度不是「加几行代码」，而是要重构 core 的状态保存模型。

#### 4.5.5 小练习与答案

**练习 1**：warp 调度与流水线都为了「填满执行单元」，两者填的是同一种「空隙」吗？

> **答案**：不是同一种。流水线填的是「**同一线程内**，相邻指令之间的阶段空隙」（例如指令 N 在 WAIT 时，指令 N+1 在 DECODE）；warp 调度填的是「**同一 core 内**，某 warp 等内存时切到另一 warp」。前者是单线程的指令级并行，后者是多 warp 的线程级并行，二者可以叠加。

**练习 2**：为什么 warp 调度特别擅长隐藏「内存延迟」，却不太能隐藏「计算延迟」？

> **答案**：因为内存延迟（`WAIT`）很长，期间有足够时间切换到别的 warp 跑很多拍计算；而单条算术指令的 `EXECUTE` 只占 1 拍，切换开销可能比节省的时间还大。所以 warp 调度对访存密集型内核收益最大——这恰好是 tiny-gpu 最吃亏的场景（见 4.2 的 `WAIT` 闲置）。

---

## 5. 综合实践

本讲的综合实践，是把 4.1–4.4 串起来，亲手设计一个**会让当前 tiny-gpu 算错**的分支分歧内核。

### 5.1 实践目标

设计一段汇编内核，让 block 内**不同线程在同一条 `BRnzp` 上走向不同 PC**；然后基于 `current_pc <= next_pc[THREADS_PER_BLOCK-1]`（[src/scheduler.sv:103-104](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L103-L104)）**预测**硬件会怎么算错，并解释根因。

### 5.2 内核场景：按 `%threadIdx` 分流

目标：4 个线程，往 `mem[base + threadIdx]` 写值。规则是——

- **线程 0**（`threadIdx == 0`）：写入 `100`
- **线程 1/2/3**（`threadIdx != 0`）：写入 `200`

这依赖 `%threadIdx` 这个**因线程而异**的只读寄存器（见 [u5-l4](u5-l4-registers-pc.md)），必然引发分歧。下面是示例汇编（**示例代码**，非项目自带内核；编码方式参考 [u5-l1](u5-l1-isa-encoding.md)，接入仿真参考 [u6-l3](u6-l3-writing-kernels.md)）：

```asm
.threads 4
.data 0 0 0 0          ; 输出区 base=0

; 用 CMP 判断 threadIdx 是否等于 0
CONST R1, #0           ; 阈值 0
CMP  %threadIdx, R1    ; 比较 threadIdx 与 0：线程0→Z=1；其余→Z=0,P=1
BRz  T0_PATH           ; 「相等(Z)则跳转」：只有线程0想跳

; —— fall-through 分支（线程 1/2/3 走这里）——
CONST R2, #200
STR  ...               ; 把 200 写入 mem[base+threadIdx]
RET

T0_PATH:               ; —— 跳转分支（只有线程 0 想走这里）——
CONST R2, #100
STR  ...               ; 把 100 写入 mem[base+threadIdx]
RET
```

### 5.3 操作步骤

1. **确认分歧**：在 `CMP %threadIdx, R1` 之后，对照 [u5-l2](u5-l2-alu-nzp.md) 的 NZP 编码，确认线程 0 与线程 1/2/3 的 `next_pc` 不再相同（线程 0 命中 `BRz` 要跳到 `T0_PATH`，其余线程走 `PC+1`）。
2. **预测硬件行为**：在 `BRz` 这条指令的 UPDATE 拍，scheduler 会执行 `current_pc <= next_pc[THREADS_PER_BLOCK-1]`，即 `next_pc[3]`。线程 3 属于「不等」，其 `next_pc = PC+1`（fall-through）。于是 `current_pc` 被设成 `PC+1`，**线程 0 想跳的 `T0_PATH` 被整个丢弃**。
3. **预测结果**：所有线程（包括线程 0）都走 fall-through，写入 `200`。最终 `mem[0]` 也会是 `200`，而正确答案应是 `100`——内核算错，但硬件不报错。
4.（可选）**接入仿真**：仿照 [test/test_matadd.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py) 的三要素（程序内存/数据内存/线程数）把上述内核手工编码成 16 位指令列表，写一个 `test_divergence.py`，按 [u6-l3](u6-l3-writing-kernels.md) 的五步流程跑通。

### 5.4 需要观察的现象 / 预期结果

- **根因（确定）**：错误源自 [src/scheduler.sv:103-104](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L103-L104) 的 PC 收敛——scheduler 只读 `next_pc[TPB-1]`，单数 `current_pc` 无法表达「线程分裂」。
- **预测输出（确定）**：线程 0 不会跳转，所有线程写同一个值。
- **实际仿真数值**：待本地验证。若你完成了第 4 步，用 `assert data_memory.memory[0] == 100` 会**失败**——这个失败的 assert 本身就是对「分支分歧不被支持」的最直接证据。
- **进阶思考**：如果把分流条件换成「不依赖 `%threadIdx`」的常量条件（例如所有线程都 `CMP R1, R1`，恒等），分歧会消失，内核又能跑对——这反向印证了 4.4.4 的结论：**分歧只发生在「分支条件依赖线程间不同数据」时**。

> 这个实践不需要你真的改一行源码，但要求你能把「一行 `current_pc <= next_pc[TPB-1]`」与「一个具体内核的错误输出」对应起来。能做到这一点，你就真正理解了 tiny-gpu 调度模型的天花板在哪里。

## 6. 本讲小结

- **PC 收敛假设**是 tiny-gpu 调度模型的地基：scheduler 在 UPDATE 阶段只取 `next_pc[THREADS_PER_BLOCK-1]` 一个值（[src/scheduler.sv:103-104](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L103-L104)），把「每线程一份的 next_pc」压扁成「全 core 一个 current_pc」。
- **无流水线**：一条指令必须跑完六阶段才会取下一条，`WAIT` 期间 ALU/LSU/寄存器堆全部闲置，内存延迟无法被重叠吸收。
- **单 block 串行**：一个 core 一次只背一个 block，跑完 reset 再接下一个（[src/dispatch.sv:65-89](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/dispatch.sv#L65-L89)），block 间无并行。
- **分支分歧**被刻意省略：不同线程想走不同 PC 时，硬件只会跟随最后一个线程，悄悄算错；官方内核（如 `matmul`）靠「所有分支收敛」回避了这个问题。
- **warp 调度**被简化为「一个 block = 一个 warp」：core 里没有多套 PC/`core_state` 上下文，无法在 warp 间时分切换来隐藏延迟。
- 这五项简化是**一条递进的取舍链**，每砍一项都换来代码可读性、付出真实 GPU 的性能/表达能力；README 的 Advanced Functionality 与 Next Steps 章节把它们逐一列为待补全能力。

## 7. 下一步学习建议

本讲聚焦「调度」维度的取舍，建议接着从两个方向深入：

1. **内存维度的取舍**：读 [u7-l2 内存优化与缓存](u7-l2-memory-optimizations.md)。它承接本讲的「无 warp / 无流水线导致 WAIT 闲置」，转向讨论缓存层次、内存合并（coalescing）、共享内存与 barrier——这些是真实 GPU 用来缓解内存带宽瓶颈的另一组关键机制，对应 [README.md:338-378](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L338-L378)。
2. **动手扩展**：如果想把本讲的理论变成代码，可以从最小的「活跃线程掩码」入手，尝试在 [src/scheduler.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv) 里实现一个最朴素的分支分歧原型（参考 4.4.5 练习 2 的清单），并用本讲 5.2 的发散内核做回归测试——这是把「读硬件」升级为「改硬件」的第一步。
