# Core 解剖结构

## 1. 本讲目标

在前几讲里，我们已经从顶层 `gpu.sv` 看到：dispatch 把一个 block 派给某个 core，再由 core 把这个 block 跑完。但 core 内部到底长什么样？一堆 ALU、LSU、寄存器、PC 是怎么组织起来的？本讲就带你把 `core.sv` 拆开看。

学完本讲，你应当能够：

- 说清楚 core「一次处理一个 block」的工作模型，以及它如何从外部拿到 `block_id` 和 `thread_count` 两项 block 元数据。
- 识别 core 里**单实例**的三个部件（fetcher / decoder / scheduler）和**每线程一份**的四个部件（ALU / LSU / registers / PC），并理解为什么这样分。
- 读懂 `generate for` 循环是如何为每个线程复制一套执行单元的。
- 解释 `enable = (i < thread_count)` 这个门控表达式的作用。
- 画出一条 ADD 指令在 core 内部的数据通路，标注每条连线连接的端口名。

本讲**只拆 `core.sv` 这一个文件的连线**，不展开 fetcher/decoder/scheduler/ALU/LSU/registers/PC 各自的内部实现（它们分别属于后续讲义）。

## 2. 前置知识

在开始之前，请确认你已理解下面这些概念（它们都来自前置讲义）：

- **module 与实例化**：SystemVerilog 里一个 `module` 是带端口的电路积木；在另一个 module 里写 `module_name instance_name ( ... )` 就叫实例化，相当于把一块电路连进来。
- **generate 循环**：在编译期把一段电路复制 N 份。tiny-gpu 用它来复制多个 core、多个线程的执行单元。
- **SIMD（单指令多数据）**：所有线程在同一时刻执行**同一条指令**，只是各自操作自己寄存器里的不同数据。这是理解 core 结构的钥匙。
- **block / thread_count / THREADS_PER_BLOCK**：dispatcher 把线程切成 block，每个 core 一次处理一个 block；`thread_count` 是当前 block 实际的线程数（最后一块可能不足 `THREADS_PER_BLOCK`）。
- **core_state 七阶段状态机**：scheduler 驱动 core 在 `IDLE→FETCH→DECODE→REQUEST→WAIT→EXECUTE→UPDATE→DONE` 之间流转。本讲会反复用到其中三个状态：`REQUEST(3'b011)`、`EXECUTE(3'b101)`、`UPDATE(3'b110)`。
- **valid/ready/data 握手**：core 对外的 data 内存、program 内存接口都用这套握手（来自 u3-l1）。

一句话回顾：顶层 `gpu.sv` 用 `generate for` 展开 `NUM_CORES` 个 core，每个 core 经穿透寄存器接到 data/program 内存控制器（详见 u2-l1、u3-l2）。本讲从「core 的边界」往里看。

## 3. 本讲源码地图

本讲只围绕一个主文件，但会顺带引用它实例化的几个子模块的端口，帮助你理解连线方向。

| 文件 | 角色 | 本讲如何使用 |
| --- | --- | --- |
| [src/core.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv) | **计算核心本体**，本讲主角 | 逐段精读：端口、中间信号、单实例三件套、generate 多线程复制 |
| src/scheduler.sv | core 的控制流状态机（core_state 的来源） | 仅引用其中的状态编码常量，确认 core_state 各值含义 |
| src/alu.sv / registers.sv | ADD 数据通路的两端 | 确认 rs/rt、alu_out 端口方向，供综合实践标注连线 |

## 4. 核心概念与源码讲解

### 4.1 core 端口与 block 元数据

#### 4.1.1 概念说明

一个 core 是「**一次吃完一个 block 的计算单元**」。dispatcher 给它派活时，会递上两项关键信息：

- `block_id`：当前 block 的编号，会映射成线程可见的 `%blockIdx`。
- `thread_count`：当前 block 实际包含多少个线程。

为什么需要 `thread_count`？因为硬件是按最大容量 `THREADS_PER_BLOCK` 建造的（比如 4 套执行单元），但最后一块可能只有 1～3 个线程。core 必须知道「这次只有几个线程真干活」，才能把多余的执行单元关掉（见 4.4 节的门控）。

#### 4.1.2 核心流程

core 的端口可以分成四组：

1. **时钟与复位**：`clk`、`reset`（注意：顶层给 core 的 `reset` 其实是 `core_reset[i]`，由 dispatcher 控制，详见 u2-l2）。
2. **内核生命周期**：`start`（开始）→ `done`（本 block 跑完，回传给 dispatcher）。
3. **block 元数据**：`block_id`、`thread_count`。
4. **两套内存接口**：
   - program 内存：单通道、只读，地址/数据/握手若干根线（取指用）。
   - data 内存：**按线程复制**的多通道读写接口，每根信号都是长度为 `THREADS_PER_BLOCK` 的数组（访存用）。

注意 data 内存的端口长这样：`output reg [THREADS_PER_BLOCK-1:0] data_mem_read_valid`，也就是「每线程一根 valid 线」。这是因为每个线程的 LSU 都可能独立发起访存请求。

#### 4.1.3 源码精读

core 的模块声明与参数化（数据/程序内存的位宽、`THREADS_PER_BLOCK`）：

[src/core.sv:4-14](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L4-L14) — 头部注释点明「一次处理 1 个 block、含 1 个 fetcher & decoder、每线程一套 register/ALU/LSU/PC」；`THREADS_PER_BLOCK` 决定了 core 的计算容量。

block 元数据端口：

[src/core.sv:22-24](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L22-L24) — `block_id` 为 8 位；`thread_count` 的位宽是 `[$clog2(THREADS_PER_BLOCK):0]`，比单纯容纳 0～TPB-1 多一位，是为了能表示「满员」值 `THREADS_PER_BLOCK` 本身（例如 TPB=4 时 `$clog2(4)=2`，`[2:0]` 共 3 位，可表示 0～7，足够装下 4）。

data 内存端口（按线程复制）：

[src/core.sv:32-40](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L32-L40) — `read_valid`/`write_valid`/`read_ready`/`write_ready` 都是 `[THREADS_PER_BLOCK-1:0]` 的位向量；`read_address`/`read_data`/`write_address`/`write_data` 则是「每线程一根 N 位线」的数组。这些最终连到每个线程的 LSU。

#### 4.1.4 代码实践

**实践目标**：建立「端口分组」的直觉。

1. 打开 [src/core.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv) 的端口声明（L15–L40）。
2. 数一数：哪些端口是标量（单根线），哪些是「按线程复制」的数组或位向量？
3. 对照顶层 [src/gpu.sv:186-214](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L186-L214)，看 core 实例化时这些端口分别接到了 `core_lsu_*`（穿透寄存器）还是 `fetcher_read_*`。

**预期结果**：你会发现 program 内存端口是标量（一个 core 只有一个 fetcher），data 内存端口全部按线程复制（每个线程一个 LSU）。这就预告了下一节的「单实例 vs 每线程一份」之分。

#### 4.1.5 小练习与答案

**练习 1**：如果 `THREADS_PER_BLOCK = 4`，`thread_count` 端口最少需要几位？为什么题目里写成 `[$clog2(THREADS_PER_BLOCK):0]`？

> **答案**：`$clog2(4) = 2`，`[2:0]` 是 3 位。因为 `thread_count` 的取值范围是 1～4（要能表示「满员 4」），而 2 位只能表示 0～3 装不下 4，所以多取一位到 3 位。

**练习 2**：core 的 `reset` 端口和顶层 `gpu.sv` 的全局 `reset` 是同一根线吗？

> **答案**：不是。顶层实例化 core 时接的是 `core_reset[i]`（见 [src/gpu.sv:195](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L195)），它由 dispatcher 单独控制，这样 dispatcher 才能逐个 core 复位、派活。

---

### 4.2 单实例三剑客：fetcher / decoder / scheduler

#### 4.2.1 概念说明

core 里有三个部件**只实例化一次**：

- **fetcher（取指器）**：从 program 内存把当前 PC 处的 16 位指令取回来。
- **decoder（译码器）**：把这条指令切成字段、翻译成一组控制信号。
- **scheduler（调度器）**：core 的「总指挥」，驱动 `core_state` 状态机。

为什么它们是单实例？因为 SIMD 要求**所有线程在同一时刻执行同一条指令**：

- 只有一条指令流 → 只需要一个 fetcher 取指、一个 decoder 译码。
- decoder 产出的那**一组控制信号**会被**广播**给所有线程，每个线程拿到的是同一份「命令」，只是各自用自己的数据去执行。

这正好是 SIMD 的硬件写照：**指令只有一份，数据有多份**。

#### 4.2.2 核心流程

这三者的协作走的是 scheduler 定义的状态机（编码见 [src/scheduler.sv:40-47](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L40-L47)）：

```
IDLE(000) ──start──▶ FETCH(001) ──fetcher 取到指──▶ DECODE(010)
   ▲                                                   │
   │                                                   ▼
DONE(111) ◀──RET── UPDATE(110) ◀── EXECUTE(101) ◀── WAIT(100) ◀── REQUEST(011)
```

- `FETCH`：scheduler 等 fetcher 把指令取回（`fetcher_state == FETCHED`）。
- `DECODE`：decoder 把 `instruction` 译成控制信号（单拍）。
- `REQUEST`：广播控制信号，各线程开始读寄存器/发起访存。
- `EXECUTE`：ALU 做运算。
- `UPDATE`：写回寄存器、更新 PC；若遇 `RET` 则进入 `DONE` 并拉高 `done`。

scheduler 把 `core_state` 作为**输出**广播出去；fetcher、decoder 以及所有每线程单元都把 `core_state` 当**输入**，据此决定自己这一拍该不该干活。

#### 4.2.3 源码精读

三个单实例的实例化，注意每个端口连到了 core 里哪个中间信号：

**fetcher**（取指，输出 `instruction` 和 `fetcher_state`）：

[src/core.sv:74-89](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L74-L89) — 把 `current_pc`、program 内存握手信号接进去；产出 `fetcher_state`（回报取指进度）和 `instruction`（16 位指令）。

**decoder**（译码，把 `instruction` 翻译成一批 `decoded_*` 控制信号）：

[src/core.sv:91-111](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L91-L111) — 输入是 `instruction`；输出是一长串 `decoded_*`（地址字段、立即数、各路 mux、写使能、`decoded_ret` 等）。这些信号随后会被**所有线程**共享。

**scheduler**（输出 `core_state`、`current_pc`、`done`）：

[src/core.sv:113-129](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L113-L129) — 它接收 `fetcher_state`、各线程的 `lsu_state`（用于 WAIT 阶段判断访存是否完成）、以及 `next_pc` 数组；输出全局的 `core_state`、`current_pc` 和 `done`。

特别留意：`core_state` 在 core 内部声明为 `reg [2:0] core_state;`（[src/core.sv:43](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L43)），它是 scheduler 的**输出**（L121），又是 fetcher/decoder/ALU/LSU/registers/PC 的**输入**——这就是「广播控制信号」的那根总线。

#### 4.2.4 代码实践

**实践目标**：亲眼确认 `core_state` 是「一写多读」的广播信号。

1. 在 [src/core.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv) 中搜索 `.core_state(`。
2. 数一下它出现在多少个实例里，并判断哪一个是输出方向（连到 scheduler 的 `core_state` 端口）、其余是输入方向。
3. 再到 [src/scheduler.sv:37](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L37) 确认 scheduler 端 `core_state` 是 `output`。

**预期结果**：`core_state` 出现在 scheduler（输出）+ fetcher + decoder + 每个线程的 ALU/LSU/registers/PC（输入）。一个 scheduler 驱动，全 core 同步。

#### 4.2.5 小练习与答案

**练习 1**：为什么 decoder 只有一份，而不是每线程一份？

> **答案**：因为 SIMD 下所有线程同一时刻执行同一条指令，只需要把这条指令译码一次；译出的控制信号广播给所有线程即可。每线程一份 decoder 会重复劳动、浪费硬件。

**练习 2**：scheduler 的 `done` 信号最终被谁消费？

> **答案**：`done` 是 core 的输出端口，回传给顶层，被 dispatcher 用来判断这个 core 是否跑完了当前 block（从而回收该 core、派发下一个 block），详见 u2-l2。

---

### 4.3 per-thread 资源复制：ALU / LSU / registers / PC

#### 4.3.1 概念说明

与「单实例三剑客」相对，core 里还有四个部件是**每个线程各有一份**：

- **registers（寄存器堆）**：每线程 16 个寄存器，存各自的数据。
- **ALU（算术逻辑单元）**：每线程一个，做 ADD/SUB/MUL/DIV。
- **LSU（访存单元）**：每线程一个，做 LDR/STR。
- **PC（程序计数器）**：每线程一个，算自己的 `next_pc`。

为什么这四个要复制？因为 SIMD 是「**单指令、多数据**」——指令只有一份（所以 decoder 单实例），但**数据是每线程独立的**：每个线程有自己的寄存器值、自己的访存地址、自己的运算结果。所以承载「数据」的部件必须每线程一份。

#### 4.3.2 核心流程

复制靠 `generate for` 循环完成。伪代码：

```
genvar i;
generate
  for (i = 0; i < THREADS_PER_BLOCK; i = i + 1) begin : threads
      实例化 ALU   (索引 i)
      实例化 LSU   (索引 i)
      实例化 registers (索引 i，并把 i 作为 THREAD_ID 传进去)
      实例化 PC    (索引 i)
  end
endgenerate
```

注意：这个循环在**编译期**就展开，所以硬件永远建出 `THREADS_PER_BLOCK` 套执行单元，无论当前 block 实际有几个线程。至于「哪些套真正通电干活」，由下一节的 `enable` 门控决定。

每个每线程实例都用 `[i]` 下标去访问 core 内部的数组型中间信号（如 `rs[i]`、`alu_out[i]`），这样第 i 套执行单元就连到第 i 个槽位，互不串扰。

一个 core 的执行单元总数为：

\[
\text{每个 core 的执行单元套数} = \text{THREADS\_PER\_BLOCK}
\]

而整片 GPU 的同类单元总数为：

\[
\text{全 GPU 的 ALU 总数} = \text{NUM\_CORES} \times \text{THREADS\_PER\_BLOCK}
\]

（这也正是顶层 `localparam NUM_LSUS = NUM_CORES * THREADS_PER_BLOCK` 的来历，见 u2-l1。）

#### 4.3.3 源码精读

generate 循环开头：

[src/core.sv:131-134](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L131-L134) — 头部注释明确「为每个线程配备专用的 ALU/LSU/registers/PC」；`genvar i` + `for` 循环 `THREADS_PER_BLOCK` 次，循环体命名为 `threads`。

四类每线程实例（都出现在同一个循环体内）：

- ALU：[src/core.sv:135-146](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L135-L146)，输入 `rs[i]`/`rt[i]`，输出 `alu_out[i]`。
- LSU：[src/core.sv:148-168](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L148-L168)，输入 `rs[i]`/`rt[i]`，输出 `lsu_state[i]`/`lsu_out[i]`，并把第 i 槽的 data 内存线接上。
- registers：[src/core.sv:170-191](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L170-L191)，注意它多传了一个参数 `THREAD_ID(i)`——这会让该线程的只读寄存器 `%threadIdx` 初始化为 `i`；它输出 `rs[i]`/`rt[i]`，接收 `alu_out[i]`/`lsu_out[i]` 用于写回。
- PC：[src/core.sv:193-209](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L193-L209)，输出 `next_pc[i]`。

循环收尾：[src/core.sv:210-211](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L210-L211)。

#### 4.3.4 代码实践

**实践目标**：体会「编译期复制」的规模。

1. 假设默认参数 `THREADS_PER_BLOCK = 4`、`NUM_CORES = 2`。
2. 只看 [src/core.sv:133-211](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L133-L211) 这段 generate，回答：单个 core 综合出多少个 ALU、LSU、registers、PC？整片 GPU 呢？
3. 把 `THREADS_PER_BLOCK` 改成 8（心里改，不要动源码），重新算 ALU 总数。

**预期结果**：单 core 各 4 个；全 GPU 各 8 个（2×4）。改成 8 后，全 GPU 共 2×8 = 16 个 ALU。这说明 `THREADS_PER_BLOCK` 是 core「宽度」的旋钮，直接决定硬件面积。

#### 4.3.5 小练习与答案

**练习 1**：registers 实例化时为什么要把循环变量 `i` 作为 `THREAD_ID` 传进去？

> **答案**：因为每个线程的只读寄存器 `%threadIdx`（registers[15]）要在复位时初始化为各自的线程号。把 `i` 传进去，第 i 套 registers 就把自己的 `%threadIdx` 设成 i，线程才能用 `%threadIdx` 区分彼此（见 [src/registers.sv:69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L69)）。

**练习 2**：fetcher 在这个 generate 循环里吗？

> **答案**：不在。fetcher/decoder/scheduler 在循环**之外**单实例化（L74–L129），只有 ALU/LSU/registers/PC 在循环内被复制。

---

### 4.4 enable = (i < thread_count) 门控

#### 4.4.1 概念说明

上一节说过：硬件总是按 `THREADS_PER_BLOCK` 建满。但当前 block 可能只有 `thread_count` 个线程（`thread_count ≤ THREADS_PER_BLOCK`）。那么多出来的那些执行单元怎么办？——**用 `enable` 把它们关掉**。

每个每线程实例都有一个 `enable` 输入端口，core 把它统一接成：

```
.enable(i < thread_count)
```

也就是「线程号小于实际线程数的才启用」。被禁用的单元虽然物理存在，但在 `always` 块里走 `else` 分支或不动作，相当于不参与本次计算。

这是硬件里很常见的「**按最大容量建、按实际需求开**」的模式：建满是图省事（参数化、规整），用 `enable` 来适配可变的运行时规模。

#### 4.4.2 核心流程

门控的工作方式（以 ALU 为例，对应 [src/alu.sv:31-59](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L31-L59)）：

```
每个时钟上升沿：
  if (reset)        清零
  else if (enable)  只有使能的线程才真正计算 alu_out
  else              不动作（保持原值）
```

- `enable = 1`（`i < thread_count`）：该线程的单元正常工作。
- `enable = 0`（`i >= thread_count`）：该线程的单元被冻结，既不读也不写，避免污染寄存器堆、避免发出无意义的访存请求。

四个每线程部件（ALU/LSU/registers/PC）都用**同一个**表达式 `i < thread_count`，保证「要开一起开、要关一起关」。

#### 4.4.3 源码精读

四处 `enable` 接线，表达式完全一致：

- ALU：[src/core.sv:139](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L139) — `.enable(i < thread_count)`
- LSU：[src/core.sv:152](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L152) — 同上
- registers：[src/core.sv:177](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L177) — 同上
- PC：[src/core.sv:200](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L200) — 同上

注意 `thread_count` 是 core 的输入端口（运行时由 dispatcher 给定），而 `i` 是编译期的 genvar。所以这个比较是「**编译期固定下标 i** 与 **运行时信号 thread_count**」的比较，综合后就是一组普通的比较器+门控逻辑。

对照 ALU 内部如何使用 enable：[src/alu.sv:31-34](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/alu.sv#L31-L34)——`else if (enable)` 才进入计算逻辑。

#### 4.4.4 代码实践

**实践目标**：验证门控的一致性与必要性。

1. 在 [src/core.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv) 中搜索 `i < thread_count`，确认四个每线程实例用的都是这同一个表达式。
2. 思考：如果某个被禁用的 LSU **没有** `enable` 门控，它仍可能发出 `data_mem_read_valid`，会带来什么后果？

**预期结果**：四处的 enable 表达式完全相同。没有门控的话，多出的 LSU 会向 data 内存控制器发出无效的访存请求，浪费本来就紧张的带宽（4 通道，见 u3-l2），甚至读到垃圾数据写回寄存器，污染结果。

#### 4.4.5 小练习与答案

**练习 1**：`THREADS_PER_BLOCK = 4`，当前 block 的 `thread_count = 2`。哪几个 ALU 真正计算？`alu_out[2]`、`alu_out[3]` 有意义吗？

> **答案**：只有 `i = 0` 和 `i = 1` 的 ALU 被使能（`0 < 2`、`1 < 2` 为真），真正计算。`i = 2`、`i = 3` 的 ALU 被禁用，`alu_out[2]`/`alu_out[3]` 保持旧值、不参与本次指令。

**练习 2**：为什么不直接根据 `thread_count` 来「少建」几个执行单元，而要先建满再门控？

> **答案**：因为 `thread_count` 是运行时信号，每个 block 可能不同；而硬件实例数量必须在编译期固定。无法在运行时增减电路，所以只能建满最大容量，再用 `enable` 在运行时按需启停。

---

### 4.5 中间信号走线：rs / rt / alu_out / lsu_out

#### 4.5.1 概念说明

单实例三剑客和每线程四件套之间，靠 core 内部声明的一批**中间信号**连起来。理解这些信号的「谁生产、谁消费」，就等于看懂了 core 的数据通路。

最关键的几条（都按线程下标 `[i]`）：

| 信号 | 类型 | 生产者 | 消费者 |
| --- | --- | --- | --- |
| `rs[i]` / `rt[i]` | `reg` | registers（输出端口 `rs`/`rt`） | ALU、LSU（输入） |
| `alu_out[i]` | `wire` | ALU（输出端口 `alu_out`） | registers、PC（输入） |
| `lsu_out[i]` | `reg` | LSU（输出端口 `lsu_out`） | registers（输入） |

此外还有跨「单实例 ↔ 每线程」的广播信号：

- `core_state`：scheduler（输出）→ 所有人（输入）。
- `instruction`：fetcher（输出）→ decoder（输入）。
- `current_pc`：scheduler（输出）→ fetcher、PC（输入）。
- `next_pc[i]`：PC（输出）→ scheduler（输入）。
- 一长串 `decoded_*`：decoder（输出）→ 所有每线程单元（输入）。

注意类型上的细节：`rs`/`rt`/`lsu_out` 是 `reg`（因为它们由子模块的 `output reg` 驱动），而 `alu_out` 是 `wire`（ALU 内部用 `assign alu_out = alu_out_reg` 驱动，对 core 而言就是个连线）。这种 `reg`/`wire` 的区分本质上是「子模块用什么方式驱动它」。

#### 4.5.2 核心流程

一条指令的数据在 core 内部是这样流动的（以运算类指令为例）：

```
                    ┌────────── decoder（单实例）──────────┐
                    │  decoded_rs_address / rt_address /    │
                    │  decoded_alu_arithmetic_mux / ...     │  广播给所有线程
                    └───────────────────────────────────────┘
                                      │
   REQUEST 阶段(core_state=011)       ▼
   registers[i] 读出 ──rs[i]/rt[i]──▶ ALU[i] 与 LSU[i]   （每线程一份）
                                      │
   EXECUTE 阶段(core_state=101)       ▼
                          ALU[i] 计算 ──alu_out[i]──▶ registers[i]（写回）、PC[i]
                          LSU[i] 访存 ──lsu_out[i]──▶ registers[i]（写回）
                                      │
   UPDATE 阶段(core_state=110)        ▼
                          registers[i] 按 mux 选 alu_out/lsu_out/immediate 写入 rd
```

三个阶段都用 `core_state` 来对齐：REQUEST 读、EXECUTE 算、UPDATE 写。每个部件内部都有 `if (core_state == ...)` 的判断，保证「该读的时候读、该算的时候算、该写的时候写」。

#### 4.5.3 源码精读

中间信号声明：

[src/core.sv:42-54](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L42-L54) — 这里集中声明了 `core_state`、`fetcher_state`、`instruction`、`current_pc`、`next_pc[]`、`rs[]`、`rt[]`、`lsu_state[]`、`lsu_out[]`、`alu_out[]`。注意 `rs/rt/lsu_out` 是 `reg`，`alu_out/next_pc` 是 `wire`。

decoder 产出的 `decoded_*` 信号声明：

[src/core.sv:56-72](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L56-L72) — 字段类（`decoded_rd/rs/rt_address`、`decoded_nzp`、`decoded_immediate`）和控制类（各种 `*_enable`、各种 `*_mux`、`decoded_ret`）。这些是单实例 decoder 的输出，会被每个线程的 ALU/LSU/registers/PC 共享。

然后看「同一个 `rs[i]` 被三处引用」——这是理解走线的关键：

- registers 输出 `rs[i]`/`rt[i]`：[src/core.sv:189-190](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L189-L190)（`.rs(rs[i])`、`.rt(rt[i])`，方向为输出）。
- ALU 输入 `rs[i]`/`rt[i]`：[src/core.sv:143-144](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L143-L144)。
- LSU 输入 `rs[i]`/`rt[i]`：[src/core.sv:164-165](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L164-L165)。

`alu_out[i]` 同理一处生产、两处消费：

- ALU 输出：[src/core.sv:145](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L145)（`.alu_out(alu_out[i])`）。
- registers 输入：[src/core.sv:187](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L187)。
- PC 输入：[src/core.sv:206](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L206)（PC 用 `alu_out[2:0]` 做 CMP 后的 NZP 比较）。

`lsu_out[i]` 一处生产、一处消费：

- LSU 输出：[src/core.sv:167](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L167)。
- registers 输入：[src/core.sv:188](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L188)。

把这些点连起来，你会看到：在每个线程 i 内部，registers ↔ ALU ↔ LSU 形成了一个**闭合的数据环路**（读出 → 运算 → 写回），这正是「一个线程的执行单元」的物理形态。

#### 4.5.4 代码实践

**实践目标**：把「生产者—消费者」表填出来。

1. 准备一张表，列为：信号、类型(reg/wire)、生产者实例、消费者实例。
2. 对照 [src/core.sv:42-72](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L42-L72) 的声明和 L74–L211 的实例化，逐个填入 `rs`、`rt`、`alu_out`、`lsu_out`、`instruction`、`core_state`、`current_pc`、`next_pc`。
3. 验证：每个 `reg`/`wire` 有且仅有一个驱动源（生产者），但可以有多个消费者。

**预期结果**：例如 `alu_out[i]` 的生产者是 `alu_instance`，消费者是 `register_instance` 和 `pc_instance`；`core_state` 的生产者是 `scheduler_instance`，消费者是 fetcher/decoder 及所有每线程单元。无信号被两个模块同时驱动（否则就是多驱动冲突）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `alu_out` 声明成 `wire` 而 `lsu_out` 声明成 `reg`？

> **答案**：因为 ALU 模块内部用 `assign alu_out = alu_out_reg;`（连续赋值）驱动它，对外表现为组合连线，所以 core 这边用 `wire` 接；而 LSU 模块把 `lsu_out` 声明为 `output reg`（在 `always` 块里赋值），所以 core 这边对应声明成 `reg`。类型取决于子模块的驱动方式。

**练习 2**：`next_pc` 是每线程一个，但 scheduler 只取 `next_pc[THREADS_PER_BLOCK-1]` 来更新 `current_pc`（见 [src/scheduler.sv:104](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/scheduler.sv#L104)）。这暗示了一个什么假设？

> **答案**：暗示了「PC 收敛」假设——认为所有线程算出的 `next_pc` 都一样，所以只取最后一个线程的值即可。这就是 tiny-gpu 暂不支持分支分歧（branch divergence）的体现，详见 u7-l1。

---

## 5. 综合实践

**任务**：画出一条 ADD 指令在 core 内部、针对**单个线程 i** 的完整数据通路，并标注每条连线连接的端口名。

ADD 指令的语义是 `R[rd] = R[rs] + R[rt]`。请按下面三个阶段，把数据从寄存器堆取出来、经 ALU 算出、再写回寄存器堆，画成一张带箭头的图：

1. **REQUEST 阶段**（`core_state == 3'b011`）：
   - decoder 广播 `decoded_rs_address`、`decoded_rt_address`（共享）。
   - `register_instance`（第 i 套）据此读出 `registers[decoded_rs_address]` 和 `registers[decoded_rt_address]`，从它的 `rs`、`rt` 输出端口送出。
   - 这两个值经 core 中间信号 `rs[i]`、`rt[i]` 分别送进 `alu_instance` 的 `rs`、`rt` 输入端口。
2. **EXECUTE 阶段**（`core_state == 3'b101`）：
   - decoder 广播 `decoded_alu_arithmetic_mux = 2'b00`（ADD）、`decoded_alu_output_mux = 0`（算术模式）。
   - `alu_instance` 执行 `alu_out_reg <= rs + rt`，从 `alu_out` 输出端口送出。
   - 该值经 core 中间信号 `alu_out[i]` 回到 `register_instance` 的 `alu_out` 输入端口（同时也送到 `pc_instance`，但 ADD 不用）。
3. **UPDATE 阶段**（`core_state == 3'b110`）：
   - decoder 广播 `decoded_reg_write_enable = 1`、`decoded_reg_input_mux = 2'b00`（ARITHMETIC）、`decoded_rd_address`。
   - `register_instance` 执行 `registers[decoded_rd_address] <= alu_out`，完成写回。

**你需要交付的图**大致形如（请自己画并补全端口名标注）：

```
   decoder ──decoded_rs_address/decoded_rt_address─────────────┐
                                                               ▼
   register_instance[i] ──(rs)──▶ rs[i] ──┐               (读哪个寄存器)
      │                                   ├──▶ alu_instance[i].rs
      │                                   │
      └──(rt)──▶ rt[i] ───────────────────┴──▶ alu_instance[i].rt
                                                   │
                                          (EXECUTE: rs+rt)
                                                   │
   register_instance[i].alu_out ◀── alu_out[i] ◀──┘ (alu_instance.alu_out)
          │
   (UPDATE: registers[rd] <= alu_out, mux=ARITHMETIC)
```

**操作步骤**：

1. 阅读上面三段，确认每条连线的端口名（如 `registers.rs`、`alu.rs`、`alu.alu_out`、`registers.alu_out`）。
2. 在纸上画出这个闭环，标出三个 `core_state` 值分别卡在哪个环节。
3. 自检：你的图里 `rs[i]`/`rt[i]` 是否只有一个驱动源（registers）？`alu_out[i]` 是否回到了 registers？

**预期结果**：你会得到一个「registers → ALU → registers」的闭环，并且清楚地看到 decoder 的控制信号（地址、mux、写使能）像指挥棒一样决定数据在哪个阶段流动。这张图就是 core 里**一个线程**的执行单元全貌。

> 说明：本实践为源码阅读型实践，无需运行仿真；若想用日志验证，可在学完 u6-l2 后跑 `make test_matadd`，在执行轨迹里找到某条 ADD 指令对应的三个周期，对照本图核对 `rs`/`rt`/`alu_out` 的数值变化。

## 6. 本讲小结

- core 是「**一次处理一个 block**」的计算单元，从 dispatcher 拿到 `block_id` 和 `thread_count` 两项元数据。
- core 内部有**单实例三剑客**（fetcher/decoder/scheduler）——它们对应「单指令流」，产出被全 core 共享的控制信号（`core_state`、`instruction`、`decoded_*`）。
- 另有**每线程一份**的四个部件（ALU/LSU/registers/PC），由 `generate for` 在编译期复制 `THREADS_PER_BLOCK` 套——它们对应「多数据」，承载每个线程各自的运算与状态。
- `enable = (i < thread_count)` 是运行时门控：硬件建满，但只有实际线程数内的单元通电工作，多出的单元被冻结。
- core 的数据通路由若干中间信号缝合：`rs[i]`/`rt[i]`（registers→ALU/LSU）、`alu_out[i]`（ALU→registers/PC）、`lsu_out[i]`（LSU→registers）；这些信号在 `core_state` 的调度下分 REQUEST/EXECUTE/UPDATE 三拍流动。
- 「单指令、多数据」的 SIMD 本质，就体现在「decoder 一份 + 执行单元多份」这一不对称结构上。

## 7. 下一步学习建议

本讲只画出了 core 的「骨架与连线」，还没进入各部件的内部逻辑。建议按以下顺序继续：

1. **u4-l2 Scheduler 核心状态机**：搞懂 scheduler 如何逐拍驱动 `core_state` 走完七阶段，特别是 WAIT 阶段如何等待所有 LSU。
2. **u4-l3 Fetcher 与 Decoder**：看 fetcher 怎么异步取指、decoder 怎么把 16 位指令切成这讲里反复出现的 `decoded_*` 控制信号。
3. **u5 系列（ISA/ALU/LSU/registers/PC）**：下探到每个执行单元内部，把这讲里「黑盒端口」的 ALU、LSU、registers、PC 一一打开。
4. 想立刻看到 core 跑起来的效果，可先跳到 **u6-l2 执行轨迹格式化与阅读**，用日志里逐周期的寄存器值反向印证本讲的数据通路图。
