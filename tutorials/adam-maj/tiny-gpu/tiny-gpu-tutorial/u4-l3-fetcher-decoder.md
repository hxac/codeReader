# Fetcher 与 Decoder

## 1. 本讲目标

本讲聚焦 core 内部「单指令流」的前两个环节：**取指（fetch）**与**译码（decode）**。学完后你应当能够：

- 读懂 fetcher 的 `IDLE / FETCHING / FETCHED` 三态状态机，并能解释它与程序内存之间的异步握手过程；
- 把一条 16 位指令手动切成 `opcode / rd / rs / rt / imm / nzp` 等字段；
- 对着 decoder 的 `case` 表，写出任意一条指令会被拉高哪些控制信号；
- 说清楚 `core_state` 是如何像「节拍器」一样，规定 fetcher 与 decoder 各自工作的那一个周期的。

本讲承接 [u4-l2 Scheduler 核心状态机](u4-l2-scheduler-fsm.md)——那里讲的是 scheduler 用 `core_state` 在七阶段间推进；本讲回答的是：当 `core_state` 走到 `FETCH` 和 `DECODE` 这两拍时，core 内部到底发生了什么。

## 2. 前置知识

在继续之前，请确认你已经理解下面这些来自前置讲义的概念：

- **core 的三剑客**（来自 [u4-l1 Core 解剖结构](u4-l1-core-anatomy.md)）：每个 core 只实例化 **1 个 fetcher、1 个 decoder、1 个 scheduler**，它们对应「单指令流」，产出的控制信号被全 core 的所有线程共享；而 ALU/LSU/registers/PC 是每线程一份。本讲的 fetcher 与 decoder 正是这两个单实例部件。
- **`core_state` 七阶段**（来自 [u4-l2](u4-l2-scheduler-fsm.md)）：scheduler 把执行一条指令切成 `IDLE→FETCH→DECODE→REQUEST→WAIT→EXECUTE→UPDATE→DONE`，用 3 位信号广播。本讲只关心其中两拍：
  - `3'b001` = `FETCH`（取指）
  - `3'b010` = `DECODE`（译码）
- **valid/ready 异步握手**（来自 [u3-l1 内存模型与外部接口](u3-l1-memory-model-interface.md)）：请求方拉高 `valid`，应答方准备好后拉高 `ready`，一次事务完成。fetcher 向程序内存取指用的就是这套握手。
- **程序内存是只读的 16 位指令存储**（来自 u3-l1）：每条指令占一个地址，位宽 16 位。

一句话复习：scheduler 是「指挥」，fetcher 是「跑腿去程序内存拿指令的快递员」，decoder 是「把指令翻译成一排开关（控制信号）的翻译官」。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/fetcher.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv) | 取指单元：异步从程序内存取出当前 PC 处的 16 位指令并锁存。 |
| [src/decoder.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv) | 译码单元：把 16 位指令切成字段，并按 opcode 拉高一排控制信号。 |
| [src/core.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv) | 实例化 fetcher 与 decoder，并把它们的输出连线广播给所有线程。 |
| [test/helpers/format.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py) | 反汇编逻辑：把 16 位指令翻译成可读字符串，是本讲实践任务的核对工具。 |
| [test/test_matadd.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py) | 矩阵加法示例内核，提供了真实可用的指令二进制编码，供实践任务逐位拆解。 |

---

## 4. 核心概念与源码讲解

### 4.1 Fetcher：异步取指状态机

#### 4.1.1 概念说明

CPU/GPU 执行指令的第一步永远是「把指令从指令存储里拿出来」，这一步叫**取指（fetch）」。

在 tiny-gpu 里，指令放在外部的**程序内存**（program memory）中，core 不能「瞬间」拿到它，而要走 valid/ready 握手：

- core 这边发出「我要读 `current_pc` 这个地址」的请求（`mem_read_valid=1, mem_read_address=current_pc`）；
- 程序内存（经控制器中继）准备好后回送 `mem_read_ready=1` 和 `mem_read_data=<16 位指令>`；
- core 收到后才算取到这条指令。

因为这次往返可能花费不止一拍，fetcher 不能用一个组合逻辑表达式敷衍过去，而必须用一个**状态机**来记住「我现在是还没发请求、还是已经发了在等回应、还是已经拿到了」。这就是 fetcher 存在的意义。

#### 4.1.2 核心流程

fetcher 是一个三态有限状态机（FSM）：

```text
                 core_state == FETCH
        ┌────────────────────────────────────┐
        ▼                                     │
     ┌──────┐  core_state==FETCH   ┌──────────┐  mem_read_ready   ┌─────────┐
     │ IDLE │ ───────────────────► │ FETCHING │ ────────────────► │ FETCHED │
     └──────┘  发 mem_read_valid   └──────────┘  锁存 instruction └─────────┘
        ▲                                     等回应                  │
        └───────────────────────────────────────────────────── core_state==DECODE
```

三个状态的职责：

1. **`IDLE`（000）**：空闲。当它看到 scheduler 把 `core_state` 推进到 `FETCH` 时，就发出读请求，自己进入 `FETCHING`。
2. **`FETCHING`（001）**：已发请求，等回应。一旦 `mem_read_ready` 拉高，就把 `mem_read_data` 锁存进 `instruction`，撤回 `mem_read_valid`，进入 `FETCHED`。
3. **`FETCHED`（010）**：指令已到手。停在原地等 scheduler 把 `core_state` 推进到 `DECODE`，确认下游开始用了，就回到 `IDLE`，为取下一条指令做准备。

> 注意「谁来推进状态」的分工：`IDLE→FETCHING` 和 `FETCHING→FETCHED` 由 fetcher **自己**根据握手信号决定；而 `FETCHED→IDLE` 则要**等 scheduler 把 `core_state` 切到 `DECODE`**。这正是 u4-l2 讲的「scheduler 在 FETCH 阶段会一直等 `fetcher_state==FETCHED`」的另一面——两者互相握手。

#### 4.1.3 源码精读

fetcher 的端口分为三组：时钟复位、与 scheduler 同步的 `core_state` 和 `current_pc`、以及与程序内存的 4 根握手线，外加它回送给 core 的 `fetcher_state` 和锁存好的 `instruction`：

[src/fetcher.sv:L7-L27](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L7-L27) — 定义了 fetcher 的参数化端口；`mem_read_valid/address` 是发给程序内存的请求，`mem_read_ready/data` 是程序内存的应答。

三个状态用 `localparam` 编码为 3 位（与 LSU 等模块保持同样宽度）：

[src/fetcher.sv:L28-L30](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L28-L30) — `IDLE=000, FETCHING=001, FETCHED=010`。

`IDLE` 态：检测到 `core_state == 3'b001`（即 FETCH），就发出请求并切到 `FETCHING`：

[src/fetcher.sv:L40-L47](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L40-L47) — 关键是 `mem_read_address <= current_pc`，即「取 PC 处那条指令」。

`FETCHING` 态：等程序内存回应，回应到了就把指令**锁存**到 `instruction` 寄存器：

[src/fetcher.sv:L48-L55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L48-L55) — `instruction <= mem_read_data` 是整段代码的灵魂：从这一刻起，这条 16 位指令就被稳定地保存在 fetcher 内部，供 decoder 使用。

`FETCHED` 态：等 scheduler 切到 `DECODE` 后回到 `IDLE`：

[src/fetcher.sv:L56-L61](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L56-L61) — 这里**不清零** `instruction`，所以译码乃至后续阶段读到的都是同一条指令，直到下一次取指覆盖它。

整段 `always @(posedge clk)` 全部使用非阻塞赋值 `<=`，因此状态翻转和指令锁存都要到「下一个时钟沿」才生效——这正是 u4-l2 强调的「单周期阶段会带 1 拍延迟」的根源。

#### 4.1.4 代码实践

**实践目标**：在仿真日志里亲眼看到 fetcher 的三态翻转与指令锁存。

**操作步骤**：

1. 按 [u1-l3 构建与仿真工具链](u1-l3-build-and-simulation.md) 跑一次矩阵加法内核：`make test_matadd`。
2. 打开 `test/logs/` 下新生成的时间戳日志。
3. 在日志里搜索 `Fetcher State:` 字样（由 [format.py 的 `format_fetcher_state`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L61-L67) 打印），找到一条指令从 `IDLE → FETCHING → FETCHED` 的连续几拍。

**需要观察的现象**：

- 某拍 `Core State: FETCH` 且 `Fetcher State: IDLE`；下一拍 `Fetcher State: FETCHING`；
- 再过若干拍 `Fetcher State: FETCHED`，同时 `Instruction:` 一行从空变成一条具体指令（例如 `ADD R0, R0, %threadIdx`）；
- 之后 `Core State` 切到 `DECODE`，`Fetcher State` 回到 `IDLE`。

**预期结果**：`Instruction` 在 `FETCHED` 那一拍才被填上，并在此后几拍保持不变，验证了「`instruction` 在 `FETCHING→FETCHED` 边界锁存、在 `FETCHED→IDLE` 时不被清零」的设计。若日志里 `Fetcher State` 一直停在 `FETCHING`，说明程序内存的 `ready` 没回上来——可结合 [u3-l2 内存控制器](u3-l2-memory-controller.md) 排查。

> 待本地验证：具体的 `FETCHING` 停留拍数取决于程序内存控制器中继耗时，不同环境可能差 1–2 拍。

#### 4.1.5 小练习与答案

**练习 1**：如果把 fetcher 的 `mem_read_valid <= 0;`（[L53](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L53)）删掉，会出现什么问题？

> **参考答案**：取到指令后请求线不会撤回，程序内存会以为还有一个未完成的读请求，可能反复回应或占用通道，导致 fetcher 状态机与控制器握手错乱。撤回 `valid` 是「事务完成」的必要信号。

**练习 2**：为什么 `FETCHED→IDLE` 的条件是 `core_state == DECODE`，而不是取到指令后立刻回 `IDLE`？

> **参考答案**：因为 decoder 要在 `DECODE` 这一拍读 `instruction`。如果 fetcher 过早回到 `IDLE` 并开始取下一条指令，`instruction` 寄存器可能被新请求覆盖（或时序错位），decoder 就读不到正确的当前指令。等 `DECODE` 才回 `IDLE`，保证了「一条指令的生命周期」不被打断。

---

### 4.2 指令的 16 位编码与字段切分

#### 4.2.1 概念说明

fetcher 取回的 `instruction` 是一串 16 位的 0/1，它本身没有任何「含义」。要让它驱动硬件，必须先把这 16 位**切成几个字段**，每个字段表达一个意思：操作码（做什么运算）、目的寄存器（结果写到哪）、源寄存器（操作数从哪来）等等。

这就是**译码（decode）」的第一项工作：字段切分。它纯粹是「按位截取」，没有任何状态机，组合逻辑就能完成。

#### 4.2.2 核心流程

tiny-gpu 的 16 位指令布局如下（与 [format.py 的反汇编](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L14-L22) 完全一致）：

| 位段 | 字段 | 宽度 | 含义 |
|------|------|------|------|
| `[15:12]` | opcode | 4 位 | 操作码，决定指令类型（共 11 种） |
| `[11:8]` | rd | 4 位 | 目的寄存器编号（结果写回这里） |
| `[7:4]` | rs | 4 位 | 源寄存器 1 |
| `[3:0]` | rt | 4 位 | 源寄存器 2 |
| `[11:9]` | nzp | 3 位 | 条件位 N/Z/P（仅 `BRnzp` 用） |
| `[7:0]` | imm | 8 位 | 立即数（`CONST`、`BRnzp` 的跳转目标等用） |

注意几个字段是**重叠复用**的：`nzp` 占用 `rd` 的高 3 位、`imm` 占用 `rs`+`rt` 拼起来的 8 位。同一段二进制对不同指令有不同含义，这正是定长编码省位的常见技巧。

整条指令的数值可以写成：

\[
\text{instruction} = (\text{opcode} \ll 12)\;\big|\;(\text{rd} \ll 8)\;\big|\;(\text{rs} \ll 4)\;\big|\;\text{rt}
\]

#### 4.2.3 源码精读

decoder 用 `localparam` 把 11 个 opcode 定义成 4 位常量，便于在 `case` 里用名字而非魔法数字：

[src/decoder.sv:L34-L44](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L34-L44) — 注意 `NOP=0000`、`RET=1111`，opcode 并非连续，但 4 位空间足够容纳 11 条指令。

字段切分发生在「`core_state == DECODE`」这一拍，是 5 条单纯的位截取语句：

[src/decoder.sv:L66-L70](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L66-L70) — `rd=instruction[11:8]`、`rs=instruction[7:4]`、`rt=instruction[3:0]`、`imm=instruction[7:0]`、`nzp=instruction[11:9]`。注释里的 `// Get instruction signals from instruction every time` 说明这些字段**每次译码都重新切**，无条件执行。

切出来的 `rd/rs/rt/imm/nzp` 通过 decoder 的输出端口（[L15-L19](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L15-L19)）送给 core，最终被 registers（用 `rd/rs/rt` 选寄存器）和 PC（用 `imm/nzp`）消费。

#### 4.2.4 代码实践

**实践目标**：手动把一条真实的 ADD 指令逐位切成字段。

**操作步骤**：以 [test_matadd.py 里的内核](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L12-L26)第 6 行的 `ADD R4, R1, R0` 为例，其二进制是 `0b0011010000010000`。

1. 把 16 位写成 4 位一组：`0011 0100 0001 0000`；
2. 按布局表切分：
   - opcode `[15:12]` = `0011` → 查表是 `ADD`；
   - rd `[11:8]` = `0100` = 4 → `R4`；
   - rs `[7:4]` = `0001` = 1 → `R1`；
   - rt `[3:0]` = `0000` = 0 → `R0`；
3. 拼回汇编：`ADD R4, R1, R0`，与注释完全一致 ✓。

**需要观察的现象**：切出的 rd/rs/rt 三个字段分别落在哪 4 位，以及为什么 `imm`（`[7:0]=00010000=16`）在 ADD 指令里「虽然被切出来了但用不上」。

**预期结果**：你会清楚地看到，同一段 `[7:0]` 在 ADD 里被当作 rs/rt 两个 4 位寄存器号，而在 CONST/BRnzp 里会被整体当作 8 位立即数——字段含义由 opcode 决定。

#### 4.2.5 小练习与答案

**练习 1**：内核里有一条 `ADD R0, R0, %threadIdx`，二进制是 `0b0011000000001111`。请切出 rs 和 rt，并说明 rt 为什么对应「`%threadIdx`」。

> **参考答案**：opcode=`0011`(ADD)，rd=`0000`(R0)，rs=`0000`(R0)，rt=`1111`=15。寄存器编号 15 在 [format.py 的 `format_register`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L4-L12) 里映射为 `%threadIdx`——这是一个只读的特殊寄存器，硬件会自动填入当前线程号（详见 [u5-l4 寄存器堆与程序计数器](u5-l4-registers-pc.md)）。

**练习 2**：`CONST R1, #0` 的二进制是 `0b1001000100000000`。请切出 rd 和 imm。

> **参考答案**：opcode=`1001`(CONST)，rd=`0001`(R1)，`[7:0]`=`00000000`=0，即 imm=`#0`。CONST 只用 rd 和 imm 两个字段，rs/rt 被忽略。

---

### 4.3 控制信号译码表

#### 4.3.1 概念说明

切出字段只是译码的「前菜」。译码的真正产物是一组**控制信号**——它们像一排开关，告诉 core 里的其它部件（ALU、LSU、registers、PC）该干嘛：要不要写寄存器、寄存器写回的值从哪路来、ALU 做哪种运算、要不要访存、要不要跳转……

decoder 是一个**纯查表逻辑**：以 opcode 为索引，每条指令对应一组固定的控制信号组合。它不记状态、不做运算，只负责「翻译」。

#### 4.3.2 核心流程

decoder 在 `core_state == DECODE` 这一拍做两件事，顺序很关键：

1. **先把所有控制信号清零**（`decoded_* <= 0`）；
2. **再用 `case (instruction[15:12])` 按opcode有条件地拉高某些信号**。

为什么要先清零？因为 decoder 是单实例、`decoded_*` 是寄存器输出，上一条指令留下的信号如果不清，会「串」到这条指令。先全部归零再按需置位，保证每条指令的控制信号都干净、自洽。

下表汇总了 11 条指令各自拉高的控制信号（✓=置 1/置位，空=保持默认 0）：

| 指令 | opcode | reg_write_enable | reg_input_mux | alu_arithmetic_mux | alu_output_mux | mem_read_enable | mem_write_enable | nzp_write_enable | pc_mux | ret |
|------|--------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| NOP | 0000 |  |  |  |  |  |  |  |  |  |
| BRnzp | 0001 |  |  |  |  |  |  |  | ✓ |  |
| CMP | 0010 |  |  |  | ✓ |  |  | ✓ |  |  |
| ADD | 0011 | ✓ | 00 | 00 |  |  |  |  |  |  |
| SUB | 0100 | ✓ | 00 | 01 |  |  |  |  |  |  |
| MUL | 0101 | ✓ | 00 | 10 |  |  |  |  |  |  |
| DIV | 0110 | ✓ | 00 | 11 |  |  |  |  |  |  |
| LDR | 0111 | ✓ | 01 |  |  | ✓ |  |  |  |  |
| STR | 1000 |  |  |  |  |  | ✓ |  |  |  |
| CONST | 1001 | ✓ | 10 |  |  |  |  |  |  |  |
| RET | 1111 |  |  |  |  |  |  |  |  | ✓ |

几个关键多路选择器（mux）的含义：

- `reg_input_mux`：寄存器写回的数据从哪来——`00`=ALU 输出、`01`=LSU 输出（内存读回）、`10`=立即数（CONST）。
- `alu_arithmetic_mux`：ALU 做哪种算术——`00`=ADD、`01`=SUB、`10`=MUL、`11`=DIV。
- `alu_output_mux`：ALU 工作模式——`0`=算术运算、`1`=比较运算（CMP，结果写进 NZP 位）。

#### 4.3.3 源码精读

decoder 的输出端口就是这一排控制信号（共 10 个），每个端口名都以 `decoded_` 开头，对应 core.sv 里的同名 `reg`：

[src/decoder.sv:L21-L32](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L21-L32) — 注释里写清了每个信号的用途，例如 `decoded_reg_input_mux` 是「Select input to register」。

「先清零」的 9 条语句紧跟在字段切分之后：

[src/decoder.sv:L72-L81](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L72-L81) — 这是「每条指令信号干净」的保障。

核心的 `case` 查表，以 ADD 为例：

[src/decoder.sv:L95-L99](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L95-L99) — ADD 拉高 `reg_write_enable=1`（要写寄存器）、`reg_input_mux=00`（写回值取自 ALU）、`alu_arithmetic_mux=00`（ALU 做加法）。注意它**不碰** `alu_output_mux`，所以该信号保持默认 `0`，即 ALU 处于算术模式而非比较模式。

完整的 11 分支 `case` 表在这里：

[src/decoder.sv:L83-L130](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L83-L130) — 可以对照上面的汇总表逐条核对。例如 CMP（[L91-L94](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L91-L94)）拉高 `alu_output_mux=1`（切到比较模式）和 `nzp_write_enable=1`（把比较结果写进 NZP 寄存器），却**不**写通用寄存器。

这些控制信号在 core.sv 里被广播给所有线程的执行单元——单实例 decoder 的输出，喂给每线程一份的 ALU/LSU/registers/PC，这正是「单指令流驱动多数据（SIMD）」的硬件连线体现：

- `decoded_alu_*` → 每线程的 ALU：[core.sv:L141-L142](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L141-L142)
- `decoded_mem_*` → 每线程的 LSU：[core.sv:L154-L155](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L154-L155)
- `decoded_reg_*` → 每线程的寄存器堆：[core.sv:L181-L186](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L181-L186)
- `decoded_pc_mux / decoded_nzp_*` → 每线程的 PC：[core.sv:L202-L205](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L202-L205)
- `decoded_ret` → scheduler（用于判定内核结束）：[core.sv:L124](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L124)

#### 4.3.4 代码实践

**实践目标**：给一条 ADD 指令，准确写出 decoder 会拉高哪些控制信号（本讲规格要求的实践任务）。

**操作步骤**：

1. 选定指令：`ADD R4, R1, R0`，二进制 `0b0011010000010000`（来自 [test_matadd.py:L18](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L18)）。
2. 切出 opcode = `0011` = ADD。
3. 翻到 [decoder 的 ADD 分支 L95-L99](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L95-L99)，列出会被赋值的信号。

**预期结果**：decoder 在 `DECODE` 拍拉高的控制信号为：

| 信号 | 值 | 含义 |
|------|:--:|------|
| `decoded_reg_write_enable` | `1` | 结果要写回寄存器 |
| `decoded_reg_input_mux` | `2'b00` | 写回值取自 ALU 输出 |
| `decoded_alu_arithmetic_mux` | `2'b00` | ALU 做加法 |
| `decoded_alu_output_mux` | `0`（默认，未碰） | ALU 处于算术模式 |
| 其余（mem/nzp/pc/ret） | `0` | 不访存、不比较、不跳转、非返回 |

**需要观察的现象**：注意 `alu_output_mux` 并没有出现在 ADD 分支里，它是被 [L79 的清零语句](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L79)置为 0 的——理解「先清零再置位」才能正确预测那些「没写」的信号。

#### 4.3.5 小练习与答案

**练习 1**：`LDR R4, R4` 的二进制是 `0b0111010001000000`。请写出它拉高的全部控制信号，并解释 `reg_input_mux=01` 的作用。

> **参考答案**：opcode=`0111`(LDR)。拉高：`reg_write_enable=1`、`reg_input_mux=01`、`mem_read_enable=1`。`reg_input_mux=01` 表示写回寄存器的值来自 LSU（即从内存读回的数据），而不是 ALU——这正是「LDR = 把内存里的数载入寄存器」的硬件表达。

**练习 2**：为什么 ADD/SUB/MUL/DIV 四条指令的 `reg_input_mux` 都设成 `00`？

> **参考答案**：这四条都是算术指令，结果由 ALU 计算产生，自然要把 ALU 的输出接到寄存器写回端，即 `reg_input_mux=00`（选 ALU）。它们之间靠 `alu_arithmetic_mux` 的 `00/01/10/11` 区分具体运算，而写回通路完全相同。

**练习 3**：`CMP R1, R0` 不会写通用寄存器，那它的结果存到哪去了？

> **参考答案**：CMP 拉高 `alu_output_mux=1`（比较模式）和 `nzp_write_enable=1`，把比较结果编码成 N/Z/P 三位写进 PC 模块里的 NZP 寄存器，供后续 `BRnzp` 判断跳转（详见 [u5-l2 ALU 与比较/NZP](u5-l2-alu-nzp.md)）。

---

### 4.4 core_state：fetcher 与 decoder 的工作时机

#### 4.4.1 概念说明

fetcher 和 decoder 各有自己的内部逻辑，但它们「在哪一拍动手」完全由 scheduler 广播的 `core_state` 决定。可以把 `core_state` 想象成一根「节拍线」：它走到 `FETCH`，fetcher 才开始取指；走到 `DECODE`，decoder 才开始译码；其它阶段这两个部件基本静止。

这种「 centralized 时序 + 分布式动作」的设计，让单实例的 fetcher/decoder 与每线程一份的 ALU/LSU/registers/PC 严格同步——这正是 SIMD 能跑对的根基。

#### 4.4.2 核心流程

把本讲两个部件的工作时机挂到 u4-l2 的七阶段时间线上：

```text
core_state:  IDLE → FETCH → DECODE → REQUEST → WAIT → EXECUTE → UPDATE → DONE
                     │         │
   fetcher:  发请求─►锁存     │
                             │
   decoder:                 切字段+译码──►decoded_* 信号生效，供后续阶段使用
```

- **FETCH 拍**：fetcher 检测 `core_state==3'b001`，从 `IDLE` 切到 `FETCHING` 并发出读请求；scheduler 此时「卡」在 FETCH 阶段，直到看到 `fetcher_state==FETCHED` 才推进。
- **DECODE 拍**：fetcher 已是 `FETCHED`，decoder 检测 `core_state==3'b010`，在这**一拍内**完成字段切分与控制信号查表（清零 + case）。由于全用 `<=`，`decoded_*` 要到下一拍（REQUEST）才真正可见，被 ALU/LSU/registers 在 REQUEST/EXECUTE/UPDATE 使用。
- **REQUEST 之后的拍**：fetcher 回到 `IDLE`（因为 `core_state` 已离开 `DECODE`），decoder 也停止动作（`core_state != DECODE`，`decoded_*` 保持上一条译出的值不变），把舞台让给执行单元。

#### 4.4.3 源码精读

fetcher 与 decoder 都是「被 `core_state` 选通」的——核心 `if` 条件就是对 `core_state` 的比较：

- fetcher 在 `IDLE` 态看 `FETCH`：[src/fetcher.sv:L42](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L42)（`if (core_state == 3'b001)`）
- fetcher 在 `FETCHED` 态看 `DECODE`：[src/fetcher.sv:L58](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/fetcher.sv#L58)（`if (core_state == 3'b010)`）
- decoder 整段译码逻辑被 `if (core_state == 3'b010)` 包住：[src/decoder.sv:L64](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L64)

也就是说：**只要 `core_state` 不是 `DECODE`，decoder 的 `always` 块里就什么都不做**，`decoded_*` 寄存器保持原值。这就是为什么 REQUEST/WAIT/EXECUTE/UPDATE 阶段读到的 `decoded_*` 仍是 DECODE 那拍译出来的值。

在 core.sv 里，fetcher、decoder、scheduler 三者通过共享的 `core_state` 串成一条链：scheduler 写 `core_state`，fetcher 与 decoder 读它：

- fetcher 实例：[core.sv:L74-L89](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L74-L89)（`.core_state(core_state)`）
- decoder 实例：[core.sv:L91-L111](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L91-L111)（`.core_state(core_state)`）
- scheduler 实例（产出 `core_state`）：[core.sv:L113-L129](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/core.sv#L113-L129)

#### 4.4.4 代码实践

**实践目标**：在日志里沿 `core_state` 的时间线，确认 fetcher 与 decoder 的动作时序。

**操作步骤**：

1. 重看 `make test_matadd` 的日志；
2. 跟踪某条指令（比如 PC=1 的 `ADD R0, R0, %threadIdx`）的连续几拍；
3. 记录每拍的 `Core State` 与 `Fetcher State`，并注意 `Instruction` 字段何时出现。

**需要观察的现象**：

| 拍 | Core State | Fetcher State | Instruction |
|:--:|:--:|:--:|:--:|
| n | FETCH | IDLE → FETCHING | 空 |
| n+1.. | FETCH | FETCHING | 空 |
| n+k | FETCH | FETCHED | ADD R0, R0, %threadIdx |
| n+k+1 | DECODE | FETCHED → IDLE | ADD R0, R0, %threadIdx（保持） |
| n+k+2 | REQUEST | IDLE | ADD R0, R0, %threadIdx（保持） |

**预期结果**：你能看到 `Instruction` 恰好在 `Fetcher State` 变为 `FETCHED` 那一拍被填上，并在随后的 DECODE/REQUEST 拍保持不变——这同时验证了 4.1（锁存时机）和 4.4（core_state 选通）两个机制。

> 待本地验证：由于格式化打印发生在每个时钟沿的 `ReadOnly()` 时刻（见 [test_matadd.py:L54-L57](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L54-L57)），你看到的 `Instruction` 字段对应的是「上一拍 `FETCHED` 锁存、本拍可读」的值，时序上会与上表略有偏移，但整体趋势一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 decoder 的译码条件是 `core_state == DECODE`，而不是「只要 `instruction` 变了就译码」？

> **参考答案**：因为 `instruction` 在 `FETCHED` 那拍就已锁存，但此时 `core_state` 还是 `FETCH`。如果此时就译码，`decoded_*` 会在 FETCH 拍就改写，可能干扰正在收尾的上一次执行；而且与 scheduler 的七阶段节拍脱钩。用 `core_state == DECODE` 选通，保证译码与全 core 的时序严格对齐。

**练习 2**：在 REQUEST/WAIT/EXECUTE/UPDATE 这四个阶段，fetcher 和 decoder 分别处于什么状态、在做什么？

> **参考答案**：fetcher 已回到 `IDLE`，但因为 `core_state != FETCH`，它不会发起新请求（要等下一条指令的 FETCH 拍）；decoder 因为 `core_state != DECODE`，其 `always` 块不动作，`decoded_*` 保持 DECODE 拍译出的值不变，持续供 ALU/LSU/registers/PC 使用。两者都「静止待命」。

---

## 5. 综合实践

把本讲的取指、字段切分、控制信号译码、时序选通串起来，完成下面这个「手动译码 + 仿真核对」的小任务。

**任务**：从 [test_matadd.py 的内核](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L12-L26)中任选 3 条**不同类型**的指令（建议覆盖：一条算术类如 ADD、一条访存类如 LDR/STR、一条常量类如 CONST），完成下表，然后用仿真日志核对。

| 指令二进制 | 切出的 opcode | 切出的 rd/rs/rt | 拉高的控制信号 | 反汇编字符串 |
|------------|:--:|:--:|:--:|:--:|

**操作步骤**：

1. 对每条指令，按 4.2 的布局表切字段；
2. 按 4.3 的 `case` 表（[decoder.sv:L83-L130](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L83-L130)）写出拉高的控制信号；
3. 运行 `make test_matadd`，在日志里找到这条指令的 `Instruction:` 行（由 [format.py 的 `format_instruction`](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L14-L46) 打印），核对你手写的反汇编是否与日志一致；
4. 再找到该指令 `Fetcher State` 从 `IDLE→FETCHING→FETCHED` 的几拍，确认 `Instruction` 出现的时机与你对 4.1/4.4 的分析吻合。

**预期结果**：3 条指令的手写反汇编全部与日志一致；`Instruction` 字段在 `FETCHED` 拍出现并在 DECODE 拍保持。这就说明你已经同时掌握了「指令长什么样（编码）」「指令怎么变成开关（译码）」和「什么时候变（时序）」三件事。

---

## 6. 本讲小结

- **fetcher** 是一个三态机 `IDLE→FETCHING→FETCHED`，通过 valid/ready 握手异步地从程序内存取回当前 PC 处的 16 位指令，并在 `FETCHING→FETCHED` 边界把它锁存进 `instruction` 寄存器。
- **取指的时机由 `core_state` 选通**：fetcher 在 `FETCH` 拍发请求、在 `DECODE` 拍回到 `IDLE`；它不清零 `instruction`，所以同一条指令在译码与执行阶段都稳定可读。
- **指令是 16 位定长编码**：`[15:12]`=opcode、`[11:8]`=rd、`[7:4]`=rs、`[3:0]`=rt，而 `[11:9]`=nzp、`[7:0]`=imm 是对 rd/rs/rt 字段的复用，含义由 opcode 决定。
- **decoder 先清零再查表**：在 `DECODE` 拍先把所有 `decoded_*` 归零，再用 `case (instruction[15:12])` 按 opcode 有条件地拉高控制信号，保证每条指令的信号干净自洽。
- **控制信号是一排「开关」**，被单实例 decoder 产出后广播给每线程一份的 ALU/LSU/registers/PC，这是 SIMD「单指令流驱动多数据」的硬件体现。
- **`core_state` 是节拍器**：它统一选通 fetcher（FETCH）和 decoder（DECODE）的动作时机，使单实例部件与每线程执行单元严格同步。

## 7. 下一步学习建议

本讲讲完了「指令怎么进来、怎么变成控制信号」，但**控制信号如何被消费**留给了后续讲义：

- 想知道 `decoded_alu_arithmetic_mux` / `decoded_alu_output_mux` 怎样真正驱动加法与比较，看 [u5-l2 ALU 与比较/NZP](u5-l2-alu-nzp.md)；
- 想知道 `decoded_mem_read_enable` / `decoded_mem_write_enable` 如何触发一次异步访存，看 [u5-l3 LSU 异步访存](u5-l3-lsu-async-memory.md)；
- 想知道 `decoded_reg_input_mux` 的三路写回与 `decoded_pc_mux` 的跳转如何实现，看 [u5-l4 寄存器堆与程序计数器](u5-l4-registers-pc.md)；
- 想从整体上理解 11 条指令的完整语义与编码约定，看 [u5-l1 指令集（ISA）与编码](u5-l1-isa-encoding.md)。

建议的阅读顺序：先 u5-l1 建立全 ISA 视野，再 u5-l2 / u5-l3 / u5-l4 分别下钻到 ALU、LSU、registers/PC 的内部实现。
