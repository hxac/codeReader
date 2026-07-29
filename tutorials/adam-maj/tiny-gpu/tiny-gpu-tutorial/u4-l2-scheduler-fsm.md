# Scheduler 核心状态机

## 1. 本讲目标

学完本讲后，你应该能够：

1. 画出 `scheduler` 驱动一条指令执行完整生命周期的七阶段状态机。
2. 说明 `core_state` 如何被 `scheduler` 产出、又被 core 内的 fetcher / decoder / LSU / ALU / PC 共享消费。
3. 解释为什么 `DECODE / REQUEST / EXECUTE / UPDATE` 是「单周期同步推进」，而 `FETCH` 与 `WAIT` 是「变长握手等待」。
4. 理解 `WAIT` 阶段为什么要轮询所有线程的 `lsu_state`，以及它如何成为同步指令与异步访存的分水岭。
5. 解释 `UPDATE` 阶段里 `current_pc <= next_pc[THREADS_PER_BLOCK-1]` 这行代码背后的 **PC 收敛假设**，以及 `RET → DONE` 如何标志一个 block 结束。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 什么是「调度器」

在前一讲 [u4-l1](u4-l1-core-anatomy.md) 里，我们把 core 拆成了「单实例三剑客（fetcher / decoder / scheduler）」+「每线程一份（ALU / LSU / registers / PC）」两大类部件。其中 **scheduler 就是整个 core 的「指挥」**：它不计算数据，也不读写内存，它的唯一职责是决定「现在该让哪个部件干活」。

scheduler 把这个决定编码成一个 3 位信号 `core_state` 广播给全 core。core 里的每个子模块都盯着 `core_state`，只在属于自己的那一拍里工作。理解了 scheduler，就理解了 core 的「心跳节拍」。

### 2.2 非阻塞赋值与时钟节拍（关键）

本讲要频繁判断「某阶段占几个周期」。tiny-gpu 全部用同一时钟 `clk`，并且 `scheduler` 内部一律使用 **非阻塞赋值 `<=`**。它的后果是：

> 在第 N 个上升沿写下的 `core_state <= FETCH`，要到第 N+1 个上升沿才被别的模块「看见」。

因此「scheduler 决定进入 FETCH」和「fetcher 真的开始干活」之间天然隔 1 拍。这就是为什么状态机里大量阶段注释写着 *"synchronous so we move on after one cycle"*——它们各自固定占 1 拍，根源正是非阻塞赋值引入的 1 拍延迟。如果你对 `<=` 与 `=` 的区别还不熟，可以回头参考 [u2-l2](u2-l2-kernel-launch-dcr-dispatcher.md) 与 [u3-l2](u3-l2-memory-controller.md) 的对比说明。

### 2.3 同步指令 vs 异步访存

tiny-gpu 的 11 条指令里，绝大多数（ADD / SUB / MUL / DIV / CMP / CONST / BRnzp / NOP）是 **同步指令**——数据已经躺在寄存器里，ALU 一拍就能算完。只有 LDR / STR 是 **异步访存**——它们要请 LSU 去外部 data 内存取/存数据，而外部内存经由 controller 中继、响应时间不固定（详见 [u3-l2](u3-l2-memory-controller.md)）。

scheduler 的状态机之所以专门设计一个 `WAIT` 阶段，就是为了把「等不等内存」这件事统一抽象出来：是同步指令就 1 拍走过，是异步访存就一直等到所有 LSU 都收到应答为止。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/scheduler.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv) | core 的控制流状态机 | **本讲主角**：七阶段定义与每个阶段的转移条件 |
| [src/core.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv) | 计算核心 | scheduler 如何被实例化、`core_state` 如何广播给各子模块 |
| [src/fetcher.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv) | 取指单元 | FETCH 阶段等待的 `fetcher_state == FETCHED` 从何而来 |
| [src/lsu.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv) | 访存单元 | WAIT 阶段轮询的 `lsu_state` 四个取值 |
| [src/pc.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv) | 程序计数器 | UPDATE 阶段读取的 `next_pc` 是如何算出的 |
| [test/helpers/format.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py) | 仿真轨迹格式化 | 把 3 位 `core_state` 翻译成可读字符串，供实践时读日志 |

## 4. 核心概念与源码讲解

### 4.1 scheduler 的职责与七阶段状态机定义

#### 4.1.1 概念说明

一句话定位：**scheduler 把「执行一条指令」这件事，切成一段固定的流水节拍。**

它只解决一个问题——「现在轮到哪个子模块工作」。它把答案写进一个 3 位输出信号 `core_state`，core 里所有子模块（fetcher / decoder / LSU / ALU / registers / PC）都根据 `core_state` 决定自己这一拍要不要动。这种「单指令流驱动多数据通路」正是 SIMD（单指令多数据）的核心思想——同一个 `core_state` 同时驱动 block 内所有线程的执行单元。

scheduler 处理的粒度是 **一个 block**：dispatcher 给 core 派活后，scheduler 就从这个 block 的第一条指令（PC=0）开始，一条一条地跑，直到遇到 `RET` 指令为止。

#### 4.1.2 核心流程

源码顶部的注释把整条指令的生命周期写得很清楚（[src/scheduler.sv:1-15](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L1-L15)）。一条指令要依次穿过七个阶段，外加首尾两个「空闲/结束」态：

```
        start=1                        fetcher_state==FETCHED
  IDLE ─────────► FETCH ─────────────────────────► DECODE
   ▲                                                    │
   │                                              (1拍) │
   │ reset                                              ▼
  DONE ◄── UPDATE ◄── EXECUTE ◄── WAIT ◄── REQUEST ◄──┘
   ▲     ▲    │        │         │         │
   │     │    └─ 非RET: current_pc<=next_pc ─► FETCH (下一条)
   │     └──────── RET: done<=1 ────────────► DONE
   │
   └─ scheduler 跑到 RET，本 block 处理完成
```

注意一个关键结构：**除了首尾，中间是一个 FETCH→DECODE→REQUEST→WAIT→EXECUTE→UPDATE 的循环**。每执行完一条非 RET 指令，UPDATE 把 PC 推进一位，然后回到 FETCH 取下一条——这正是「程序计数器驱动指令序列」的硬件实现。

七个阶段的语义一句话概括：

| 阶段 | 语义 | 谁在这一拍工作 |
| --- | --- | --- |
| `FETCH` (001) | 按 PC 从程序内存取指令 | fetcher |
| `DECODE` (010) | 把 16 位指令译成控制信号 | decoder |
| `REQUEST` (011) | 若是访存指令，触发 LSU 发起异步请求 | LSU（仅 LDR/STR） |
| `WAIT` (100) | 等所有 LSU 拿到内存应答 | LSU（仅 LDR/STR） |
| `EXECUTE` (101) | 执行 ALU 运算与 PC 计算 | ALU、PC |
| `UPDATE` (110) | 写回寄存器、NZP、PC | registers、PC |

#### 4.1.3 源码精读

七阶段状态用 `localparam` 定义在模块顶部，每个状态是一个 3 位编码（[src/scheduler.sv:40-47](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L40-L47)）：

```systemverilog
localparam IDLE = 3'b000, // Waiting to start
    FETCH = 3'b001,       // Fetch instructions from program memory
    DECODE = 3'b010,      // Decode instructions into control signals
    REQUEST = 3'b011,     // Request data from registers or memory
    WAIT = 3'b100,        // Wait for response from memory if necessary
    EXECUTE = 3'b101,     // Execute ALU and PC calculations
    UPDATE = 3'b110,      // Update registers, NZP, and PC
    DONE = 3'b111;        // Done executing this block
```

这正是 [test/helpers/format.py:48-59](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L48-L59) 里 `format_core_state` 那张映射表的来源——仿真日志里出现的字符串 `FETCH`、`WAIT` 等，就是把这 3 位二进制查表翻译出来的。

scheduler 的端口（[src/scheduler.sv:16-39](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L16-L39)）可以分成三类，值得记住，因为后面每个阶段的转移条件都来自这里：

- **输出**：`core_state`（广播给全 core）、`current_pc`（当前指令地址）、`done`（block 是否完成）。
- **握手输入**：`fetcher_state`（取指是否完成）、`lsu_state[TPB]`（每个线程的访存进度）。
- **控制输入**：`decoded_mem_read_enable` / `decoded_mem_write_enable`（本条指令是否访存）、`decoded_ret`（是否 RET）、`next_pc[TPB]`（每个线程算出的下一条 PC）。

整个状态转移写在一个 `always @(posedge clk)` + `case (core_state)` 里（[src/scheduler.sv:49-115](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L49-L115)）。`reset` 时把 `current_pc`、`core_state`、`done` 全部清零（[src/scheduler.sv:50-53](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L50-L53)），其余靠 `case` 分派。后面几节会逐个阶段展开。

#### 4.1.4 代码实践

1. **实践目标**：建立「状态编码 ↔ 字符串 ↔ 含义」的对照记忆。
2. **操作步骤**：打开 [src/scheduler.sv:40-47](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L40-L47) 与 [test/helpers/format.py:48-59](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L48-L59) 并排对照。
3. **观察现象**：两者的顺序、取值完全一致——`000=IDLE`、`001=FETCH`、…、`111=DONE`。
4. **预期结果**：你会确认日志里任何一行 `Core State:` 后面的字符串，都能反查回 scheduler 的某个 `case` 分支。

#### 4.1.5 小练习与答案

**练习**：scheduler 有 8 个状态编码（`000`~`111`），其中真正出现在「单条指令执行循环」里的有哪几个？哪两个是循环之外的「边界态」？

**答案**：循环内是 `FETCH(001) / DECODE(010) / REQUEST(011) / WAIT(100) / EXECUTE(101) / UPDATE(110)` 共 6 个；边界态是首部的 `IDLE(000)`（等 start）和尾部的 `DONE(111)`（遇到 RET 后停留，等 reset 回收）。

---

### 4.2 FETCH 阶段：与 fetcher 的握手等待

#### 4.2.1 概念说明

`FETCH` 是状态机里第一个「变长等待」阶段。它的工作很简单：**等 fetcher 把当前 PC 处的指令从程序内存取回来。**

为什么不能固定 1 拍？因为取指要经过 program controller 中继到外部程序内存（见 [u3-l1](u3-l1-memory-model-interface.md) / [u3-l2](u3-l2-memory-controller.md)），这一趟往返耗时不确定。所以 scheduler 在这里「卡住」，靠 `fetcher_state` 这个握手信号来判断「取回来了没」。

#### 4.2.2 核心流程

scheduler 与 fetcher 在 FETCH 阶段的配合是一个经典的两方握手：

```
scheduler:  core_state=FETCH ───────────────────────► (卡住，等)
                                    │
fetcher:    见 FETCH ─► IDLE ─► FETCHING ─► FETCHED ─┐
                     发起请求    收到数据              │
                                                     ▼
scheduler:  fetcher_state==FETCHED(3'b010) ? ──是──► 进入 DECODE
```

#### 4.2.3 源码精读

scheduler 的 FETCH 分支只做一件事——等 `fetcher_state` 变成 `FETCHED`（[src/scheduler.sv:63-68](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L63-L68)）：

```systemverilog
FETCH: begin 
    // Move on once fetcher_state = FETCHED
    if (fetcher_state == 3'b010) begin 
        core_state <= DECODE;
    end
end
```

这里的 `3'b010` 是 fetcher 的 `FETCHED` 编码，定义在 [src/fetcher.sv:28-30](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L28-L30)：

```systemverilog
localparam IDLE = 3'b000, 
    FETCHING = 3'b001, 
    FETCHED = 3'b010;
```

fetcher 自己则是一个三态小状态机（[src/fetcher.sv:39-62](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L39-L62)）：在 `core_state==FETCH` 时从 `IDLE` 进入 `FETCHING` 并把 `mem_read_valid` 拉高、地址设为 `current_pc`；收到 `mem_read_ready` 后锁存 `instruction` 并进入 `FETCHED`；等 scheduler 转到 `DECODE` 时再回到 `IDLE` 等下一条。

注意这里互为触发的关系：scheduler 等 fetcher，fetcher 又在 `FETCHED` 后等 scheduler 进入 `DECODE` 才复位——两方靠 `core_state` 与 `fetcher_state` 互相「盯」着对方，这正是握手协议的精髓。

#### 4.2.4 代码实践

1. **实践目标**：理解「scheduler 等 fetcher、fetcher 等 scheduler」的双向握手。
2. **操作步骤**：对照阅读 [src/scheduler.sv:63-68](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L63-L68) 与 [src/fetcher.sv:40-61](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L40-L61)。
3. **观察现象**：fetcher 的 `FETCHED` 分支里有一句 `if (core_state == 3'b010) fetcher_state <= IDLE;`——它在等 scheduler 离开 FETCH。
4. **预期结果**：你能说出「为什么 fetcher 取完指令后不立即回 IDLE，而要等 DECODE」——因为 scheduler 在 FETCH 阶段持续读 `fetcher_state`，若 fetcher 提前回 IDLE，scheduler 可能错过 `FETCHED` 信号而卡死。

#### 4.2.5 小练习与答案

**练习**：如果外部程序内存永远不回 `mem_read_ready`，scheduler 会停在哪个状态？为什么不会停在 FETCHING？

**答案**：停在 `FETCH`。因为 scheduler 自己只认 `core_state`，它并不感知 `FETCHING`；真正卡在 `FETCHING` 的是 fetcher（[src/fetcher.sv:48-55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L48-L55)），而 scheduler 一直停在 FETCH 等 `fetcher_state==FETCHED` 这个永远不来的信号。

---

### 4.3 同步推进的三阶段：DECODE / REQUEST / EXECUTE

#### 4.3.1 概念说明

这三个阶段是状态机里最「省心」的部分：它们各自固定占 **1 个周期**，无条件进入下一阶段。源码里它们的注释都写着同一句话——*"synchronous so we move on after one cycle"*（同步，所以一拍后进入下一阶段）。

它们占 1 拍的根源不是「计算特别快」，而是 [2.2 节](#22-非阻塞赋值与时钟节拍关键) 讲的非阻塞赋值：本拍写下 `core_state <= DECODE`，相关子模块下一拍才真正工作，scheduler 也就顺势下一拍再走。

#### 4.3.2 核心流程

把三个单周期阶段与各自激活的子模块列在一起：

| scheduler 阶段 | `core_state` | 被激活的子模块 | 持续周期 |
| --- | --- | --- | --- |
| DECODE | 010 | decoder 译出 `decoded_*` 控制信号 | 1 |
| REQUEST | 011 | LSU 见 `REQUEST`，从 IDLE 转入 REQUESTING | 1 |
| EXECUTE | 101 | ALU 算结果、PC 算 `next_pc` | 1 |

注意 REQUEST 阶段有个「门槛」：它无条件只停 1 拍，**不管本条指令是不是访存指令**。如果是 ADD 这种同步指令，LSU 根本不会被触发（`decoded_mem_read_enable/write_enable` 都为 0），REQUEST 这拍对它们来说就是空过；只有 LDR/STR 才在 REQUEST 这一拍真正发起请求。这种「统一节拍、按需工作」的设计，让状态机对所有指令都走同一条时间轴。

#### 4.3.3 源码精读

三个分支几乎一模一样，都是「无条件下一拍走人」（[src/scheduler.sv:69-76](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L69-L76) 与 [src/scheduler.sv:93-96](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L93-L96)）：

```systemverilog
DECODE: begin
    // Decode is synchronous so we move on after one cycle
    core_state <= REQUEST;
end
REQUEST: begin 
    // Request is synchronous so we move on after one cycle
    core_state <= WAIT;
end
...
EXECUTE: begin
    // Execute is synchronous so we move on after one cycle
    core_state <= UPDATE;
end
```

对应地，子模块用 `core_state` 当门控。比如 LSU 只在 `core_state == 3'b011`（REQUEST）时才从 IDLE 起步（[src/lsu.sv:53-57](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L53-L57)）；PC 只在 `core_state == 3'b101`（EXECUTE）时才计算 `next_pc`（[src/pc.sv:42-55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L42-L55)）。scheduler 把阶段编号当成了「全局使能信号」。

#### 4.3.4 代码实践

1. **实践目标**：体会「`core_state` 当全局门控」这一设计。
2. **操作步骤**：在 [src/lsu.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv) 与 [src/pc.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv) 里搜索 `core_state == 3'b` 出现的位置。
3. **观察现象**：每个子模块都恰好在自己该工作的那一拍判断 `core_state`，其余拍静默。
4. **预期结果**：你能总结出「scheduler 不直接调用子模块，而是通过移动 `core_state` 来隐式调度它们」。

#### 4.3.5 小练习与答案

**练习**：REQUEST 阶段对一条 ADD 指令也停了 1 拍，这拍里 LSU 在干什么？这算不算浪费？

**答案**：对 ADD，`decoded_mem_read_enable` 与 `decoded_mem_write_enable` 都为 0，LSU 的两个 `case` 都进不去，`lsu_state` 保持 `IDLE`，这拍对 LSU 而言是空过。从「统一节拍、简化控制流」的角度看不算浪费——保留这 1 拍让所有指令的时间轴对齐，scheduler 不必为每条指令单独决定要不要跳过 REQUEST。

---

### 4.4 WAIT 阶段：轮询所有 LSU 的同步等待

#### 4.4.1 概念说明

`WAIT` 是整个状态机里**最重要的变长阶段**，也是本讲的核心。它解决一个问题：

> 一条指令可能让 block 内的多个线程同时发起异步访存（比如 4 个线程同时执行 LDR）。这些请求经由 controller 节流到有限的外部通道（见 [u3-l2](u3-l2-memory-controller.md)），完成时间参差不齐。scheduler 必须等到 **所有线程的 LSU 都拿到应答**，才能安全进入 EXECUTE 去用这些数据。

对同步指令（ADD 等），LSU 从头到尾停在 `IDLE`，WAIT 会在 1 拍内放行；对 LDR/STR，WAIT 会一直占着，直到最慢的那个 LSU 完成。所以 **WAIT 是「同步指令」与「异步访存」在时间上的分水岭**。

#### 4.4.2 核心流程

LSU 有四个状态（[src/lsu.sv:38](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L38)）：

```
IDLE(00) → REQUESTING(01) → WAITING(10) → DONE(11)
```

scheduler 的 WAIT 逻辑只关心一件事：**还有没有 LSU 处在 `REQUESTING(01)` 或 `WAITING(10)`**。只要有一个还没到 `DONE`，就继续等。判别可以用一个简单的位运算直觉：

\[
\text{any\_lsu\_waiting} = \bigvee_{i=0}^{\text{TPB}-1} \mathbb{1}\!\left[\,\text{lsu\_state}[i] \in \{01, 10\}\,\right]
\]

只要 `any_lsu_waiting == 1`，scheduler 就留在 WAIT；一旦所有 LSU 都到 `DONE(11)` 或本就没动过（`IDLE(00)`），就进入 EXECUTE。

#### 4.4.3 源码精读

WAIT 分支是状态机里最长的一段（[src/scheduler.sv:77-92](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L77-L92)）：

```systemverilog
WAIT: begin
    // Wait for all LSUs to finish their request before continuing
    reg any_lsu_waiting = 1'b0;
    for (int i = 0; i < THREADS_PER_BLOCK; i++) begin
        // Make sure no lsu_state = REQUESTING or WAITING
        if (lsu_state[i] == 2'b01 || lsu_state[i] == 2'b10) begin
            any_lsu_waiting = 1'b1;
            break;
        end
    end
    // If no LSU is waiting for a response, move onto the next stage
    if (!any_lsu_waiting) begin
        core_state <= EXECUTE;
    end
end
```

几个要点：

1. **局部变量 `reg any_lsu_waiting`** 直接声明在 `always` 块内部（SystemVerilog 特性），每拍重新初始化为 0。
2. **`for` 循环 + `break`**：从线程 0 扫到 `THREADS_PER_BLOCK-1`，一旦发现某个 LSU 还在 `REQUESTING/WAITING` 就置位并 `break` 提前退出——找到一个「还没好」的就够了吗？不够，但因为只要存在任何一个 waiting 就要继续等，所以「发现一个即可下结论」是正确的短路。
3. **轮询所有线程**：注意循环覆盖的是 **全部 `THREADS_PER_BLOCK` 个 LSU**，而不是只看 `thread_count` 个。这没问题——被 `enable` 门控冻结的多余 LSU 永远停在 `IDLE(00)`，不会被误判为 waiting（[u4-l1](u4-l1-core-anatomy.md) 讲过 `enable = (i < thread_count)` 的门控）。

对比 LSU 侧：读类 LDR 在 `WAITING` 拿到 `mem_read_ready` 后写 `lsu_out` 并转 `DONE`（[src/lsu.sv:64-70](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L64-L70)），写类 STR 则只确认完成、不写 `lsu_out`（[src/lsu.sv:95-100](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L95-L100)）。两种指令的 `DONE` 状态都由 `core_state == UPDATE` 复位回 `IDLE`（[src/lsu.sv:71-76](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L71-L76) 与 [src/lsu.sv:101-106](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L101-L106)）。

#### 4.4.4 代码实践

1. **实践目标**：对比同步指令与异步访存在 WAIT 阶段的停留周期数。
2. **操作步骤**：阅读 [test/test_matadd.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py) 的内核，找出一条 LDR（`0b0111010001000000  # LDR R4, R4`）和它前后的一条 ADD。运行 `make test_matadd`，打开 `test/logs/` 下最新日志，定位到这条 LDR 对应的若干个连续 cycle。
3. **观察现象**：你会看到 `Core State:` 在 `WAIT` 上连续停留好几拍，而相邻 ADD 指令的 `WAIT` 只出现 1 次。同时观察 `LSU State:` 字段在 LDR 期间如何从 `REQUESTING` → `WAITING` → `DONE`。
4. **预期结果**：确认「WAIT 对 ADD = 1 拍，对 LDR = 多拍」。具体 LDR 多停几拍取决于 controller 通道争用与外部内存响应，**待本地验证**。
5. 若暂无法运行仿真，可改为源码阅读型实践：在 [src/lsu.sv:52-77](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L52-L77) 跟踪 LDR 的四态转移，说明 LSU 至少要经历 `IDLE→REQUESTING→WAITING→DONE` 共 3 次状态跳变，因此 scheduler 的 WAIT 至少要持续到这些跳变全部完成。

#### 4.4.5 小练习与答案

**练习 1**：WAIT 的 `for` 循环用的是「发现一个 waiting 就 break」，而不是「统计总共有几个 waiting」。这两种写法在「是否进入 EXECUTE」的判定上等价吗？为什么？

**答案**：等价。因为进入 EXECUTE 的条件是「没有任何 LSU 在 waiting」，即 `any_lsu_waiting == 0`。只要存在哪怕一个 waiting，结论就是「继续等」——所以发现第一个就足以下结论，无需统计总数。`break` 是正确的短路优化。

**练习 2**：如果一个 block 只有 3 个线程（`thread_count=3`，`THREADS_PER_BLOCK=4`），第 4 个 LSU 会不会让 WAIT 永远卡住？

**答案**：不会。第 4 个 LSU 因 `enable=0` 被门控（[src/lsu.sv:49](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L49) 的 `else if (enable)`），其 `lsu_state` 永远停在 `IDLE(00)`，既不是 `01` 也不是 `10`，不会触发 `any_lsu_waiting`。

---

### 4.5 UPDATE 阶段：写 PC、next_pc 收敛假设与 RET→DONE

#### 4.5.1 概念说明

`UPDATE` 是一条指令的「收尾」阶段，做三件事：

1. **推进 PC**：把 `current_pc` 更新为下一条指令的地址，好让下一个 FETCH 取到正确的指令。
2. **判定结束**：如果本条指令是 `RET`，说明整个 block 跑完了，置 `done=1` 并进入 `DONE`。
3. （由 registers/PC 模块在 `core_state==UPDATE` 这一拍各自完成寄存器与 NZP 的写回，详见 [u5-l4](u5-l4-registers-pc.md)。）

这里藏着一个贯穿全项目的 **天真假设**：所有线程的 PC 收敛到同一个值。这是 tiny-gpu 刻意省略「分支分歧（branch divergence）」的体现，留待 [u7-l1](u7-l1-scheduling-tradeoffs.md) 专门讨论。

#### 4.5.2 核心流程

UPDATE 的两条分支：

```
                 ┌── decoded_ret==1 ──► done<=1, core_state<=DONE
   UPDATE ───────┤
                 └── decoded_ret==0 ──► current_pc<=next_pc[TPB-1]
                                        core_state<=FETCH  (回循环取下一条)
```

每条非 RET 指令的耗时可以用一个简洁的公式概括。记 `t_FETCH`、`t_WAIT` 为两个变长阶段的周期数，其余四个阶段各 1 拍：

\[
T_{\text{inst}} = t_{\text{FETCH}} + \underbrace{1}_{\text{DECODE}} + \underbrace{1}_{\text{REQUEST}} + t_{\text{WAIT}} + \underbrace{1}_{\text{EXECUTE}} + \underbrace{1}_{\text{UPDATE}} = t_{\text{FETCH}} + t_{\text{WAIT}} + 4
\]

对同步指令 \(t_{\text{WAIT}}=1\)，对 LDR/STR \(t_{\text{WAIT}}\) 取决于内存往返。这就是为什么「内存密集型内核」会比「计算密集型内核」慢得多——每条访存指令都付一整次 `t_WAIT` 的代价。

#### 4.5.3 源码精读

UPDATE 分支（[src/scheduler.sv:97-109](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L97-L109)）：

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

两个关键点：

1. **`current_pc <= next_pc[THREADS_PER_BLOCK-1]`**：`next_pc` 是一个每线程一份的数组（由 [src/pc.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv) 在 EXECUTE 阶段各自算出，[src/core.sv:49](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L49) 声明为 `wire [7:0] next_pc[TPB]`）。但 PC 是「单指令流」共享的，scheduler 只能选 **一个** 值推进——它直接取了最后一个线程（下标 `THREADS_PER_BLOCK-1`）的 `next_pc`。注释 `// TODO: Branch divergence` 明确承认了这是个简化：**它假设所有线程算出的 `next_pc` 都相同**。

   这个假设在「所有线程走同一条控制流」时成立（例如 matadd 内核，每个线程都顺序执行到底）。一旦不同线程因 BRnzp 跳到不同 PC（分支分歧），这个 `current_pc` 就会「只跟最后一个线程走」，导致其他线程的 PC 被强行覆盖——这正是 [u7-l1](u7-l1-scheduling-tradeoffs.md) 要剖析的缺陷。

2. **`RET → DONE`**：遇到 `RET`（`decoded_ret` 由 decoder 译出，见 [u5-l1](u5-l1-isa-encoding.md)），scheduler 置 `done=1` 并进入 `DONE`。`done` 信号经 core 传到 dispatcher，触发 block 回收（见 [u2-l2](u2-l2-kernel-launch-dcr-dispatcher.md) 的 `core_done` 握手）。`DONE` 分支本身是个 no-op（[src/scheduler.sv:110-112](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L110-L112)），core 会一直停在这里，直到 dispatcher 重新拉 `reset` 把它打回 `IDLE` 接下一个 block。

#### 4.5.4 代码实践

1. **实践目标**：验证 PC 收敛假设在「无分支」内核里确实成立。
2. **操作步骤**：阅读 [test/test_matadd.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py) 的内核，确认 13 条指令里没有任何 BRnzp，每个线程都顺序执行到 RET。
3. **观察现象**：因为没有分支，每个线程在每条指令上算出的 `next_pc` 都是 `current_pc + 1`（[src/pc.sv:49](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L49) 与 [src/pc.sv:53](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L53)），所以「取最后一个线程的 next_pc」与「取任意线程的 next_pc」结果完全相同——收敛假设无害。
4. **预期结果**：你能用一句话解释「为什么 matadd 不需要分支分歧支持也能跑对」。
5. 进阶（可选）：对比 [test/test_matmul.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py) 里的 BRnzp，思考它是否仍满足「所有线程走同一 PC」——**待本地验证**。

#### 4.5.5 小练习与答案

**练习**：把 `current_pc <= next_pc[THREADS_PER_BLOCK-1]` 改成 `current_pc <= next_pc[0]`，在 matadd 这种无分支内核下结果会变吗？在有分支分歧的内核下呢？

**答案**：无分支内核下不变——所有线程 `next_pc` 相同，取任意下标都一样。有分支分歧内核下结果也「一样地错」：无论取哪个下标，都只能代表一个线程的 PC，其余线程的跳转意图仍被丢弃。要真正解决，需要让每个线程持有独立 PC 并支持逐线程调度（即 warp/分支分歧处理），这正是 TODO 指向的方向。

---

## 5. 综合实践

> 这是本讲的核心实践任务，对应规格里的「给一条 LDR 指令，标出它在 scheduler 每个阶段分别停留几个周期，并解释 WAIT 阶段为什么可能多停几拍」。

**任务**：以 matadd 内核里的 LDR 指令 `LDR R4, R4`（二进制 `0b0111010001000000`，[test/test_matadd.py:19](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L19)）为对象，**完整画出它在 scheduler 七个阶段的周期占用表**，并解释 WAIT。

**操作步骤**：

1. 运行 `make test_matadd`（工具链见 [u1-l3](u1-l3-build-and-simulation.md)），打开 `test/logs/` 下最新生成的 `log_<时间戳>.txt`。
2. 在日志里搜索 `LDR R4, R4`，定位它对应的连续若干个 cycle（`format_cycle` 会逐拍打印 `Core State` 与每个线程的 `LSU State`，见 [test/helpers/format.py:97-141](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L97-L141)）。
3. 数出 `Core State` 字段在 `FETCH / DECODE / REQUEST / WAIT / EXECUTE / UPDATE` 各自连续出现的次数，填入下表（左侧为预期，右侧留给你填实测）：

   | 阶段 | 预期停留 | 实测停留 | 说明 |
   | --- | --- | --- | --- |
   | FETCH | 多拍（≥2，含 fetcher 取指往返） | | 等 `fetcher_state==FETCHED` |
   | DECODE | 1 拍 | | 同步 |
   | REQUEST | 1 拍 | | 同步；LSU 在此转入 REQUESTING |
   | WAIT | 多拍（≥2，含 LSU REQUESTING→WAITING→DONE + controller 中继） | | **本讲重点** |
   | EXECUTE | 1 拍 | | 同步 |
   | UPDATE | 1 拍 | | 写回 PC，回 FETCH |

4. **解释 WAIT 为什么可能多停几拍**：在你的报告里至少覆盖以下三点——
   - LSU 自身要经历 `IDLE→REQUESTING→WAITING→DONE` 至少 3 次状态跳变（[src/lsu.sv:52-77](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L52-L77)），每次跳变因非阻塞赋值占 1 拍。
   - `WAITING` 子态要等 controller 把请求中继到外部 data 内存、再把应答中继回来（[u3-l2](u3-l2-memory-controller.md) 的五态机），这一往返本身就占若干拍。
   - scheduler 的 WAIT 是 **所有线程 LSU 的「合取」**：只要最慢的那个线程没到 `DONE`，整个 core 就得等（[src/scheduler.sv:80-86](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L80-L86)）。多个线程争用有限的外部通道时，最慢线程会被进一步拖长。

**预期结果**：得到一张填满实测数据的周期表，并能用一段话讲清「WAIT 的多拍来自 LSU 状态跳变 + controller 中继往返 + 多线程合取」三个叠加因素。具体数值因机器与通道争用而异，**待本地验证**。

> 若暂无法运行仿真，可降级为「源码阅读型实践」：不填实测列，而是基于本讲的公式 \(T_{\text{inst}} = t_{\text{FETCH}} + t_{\text{WAIT}} + 4\)，推导出 LDR 的 \(t_{\text{WAIT}}\) 下界（至少 3 拍），并写出推导过程。

## 6. 本讲小结

- `scheduler` 是 core 的「指挥」，把执行一条指令切成 `IDLE→FETCH→DECODE→REQUEST→WAIT→EXECUTE→UPDATE→DONE` 八态（其中六态构成循环），用 3 位信号 `core_state` 广播给所有子模块。
- **同步阶段** `DECODE / REQUEST / EXECUTE / UPDATE` 各固定占 1 拍，根源是非阻塞赋值 `<=` 引入的 1 拍延迟；它们对任何指令都一视同仁。
- **变长阶段** `FETCH` 等 `fetcher_state==FETCHED`、`WAIT` 等所有 `lsu_state` 脱离 `REQUESTING/WAITING`；两者是状态机里唯一的不确定耗时点。
- `WAIT` 是同步指令与异步访存的分水岭：对 ADD 等 = 1 拍放行，对 LDR/STR = 等到最慢线程的内存往返完成，且用 `for`+`break` 做「存在性」短路判定。
- `UPDATE` 用 `current_pc <= next_pc[THREADS_PER_BLOCK-1]` 推进 PC，隐含 **PC 收敛假设**（忽略分支分歧），遇到 `RET` 才置 `done` 进 `DONE`。
- 单条指令耗时可概括为 \(T_{\text{inst}} = t_{\text{FETCH}} + t_{\text{WAIT}} + 4\)，解释了为何内存密集型内核更慢。

## 7. 下一步学习建议

- 想搞清 `core_state` 是如何「翻译成」每条指令的控制信号的，进入 [u4-l3 Fetcher 与 Decoder](u4-l3-fetcher-decoder.md)，看 fetcher 的三态机与 decoder 的译码表。
- 想看 WAIT 等待的那些 LSU 内部到底怎么跑，进入 [u5-l3 LSU 异步访存](u5-l3-lsu-async-memory.md)，逐态拆解 LDR/STR。
- 想理解 UPDATE 写回的寄存器堆与 PC 细节，进入 [u5-l4 寄存器堆与程序计数器](u5-l4-registers-pc.md)。
- 想知道「PC 收敛假设」去掉之后真实 GPU 怎么做，直接跳到 [u7-l1 被简化的机制：分支分歧、流水线、warp 调度](u7-l1-scheduling-tradeoffs.md)。
