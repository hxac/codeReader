# 指令集（ISA）与编码

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 tiny-gpu 全部 **11 条指令** 的助记符、操作码与用途。
- 画出 **16 位定长指令** 的位域分配（opcode / rd / rs / rt），并解释同一个位域为何能被不同指令「复用」。
- 区分 **R0–R12 自由寄存器** 与 **R13–R15 只读寄存器**（`%blockIdx` / `%blockDim` / `%threadIdx`），并说明只读寄存器为何是 SIMD 的根基。
- 读懂 `BRnzp` 的 **nzp 条件位**，并解释它如何与 `CMP` 写入的 NZP 寄存器做「逐位与」来决定是否跳转。
- 用 `format.py` 的 `format_instruction` 把一段二进制反汇编成可读指令，反之也能把汇编手工编码成二进制。

本讲承接 [u4-l3 Fetcher 与 Decoder](u4-l3-fetcher-decoder.md)：那里讲过 decoder 在 DECODE 拍如何切字段、拉控制信号；本讲把镜头从「切字段这件事」拉远到「指令本身长什么样、有哪些、怎么读写」。

## 2. 前置知识

- **ISA（Instruction Set Architecture，指令集架构）**：软件与硬件之间的契约。软件按 ISA 把意图编码成一串二进制指令，硬件按 ISA 解释这些指令。本讲关心的就是这份「契约文本」。
- **助记符（mnemonic）vs 操作码（opcode）**：`ADD` 是给人看的助记符，`0011` 是给机器看的 4 位操作码。两者一一对应。
- **位域（bit field）**：把一个固定宽度的二进制数切成几段，每段表达一个字段。tiny-gpu 的指令是 16 位定长。
- **SIMD（单指令多数据）**：一条指令被同 一个 core 内的所有线程同时执行，但每个线程操作自己寄存器堆里的不同数据。`%threadIdx` 让每个线程知道自己是谁，从而取到不同的数据——这正是本讲要讲的「只读寄存器」存在的意义。
- 如果你还没读过 [u4-l3](u4-l3-fetcher-decoder.md)，建议先看，了解 decoder 切字段的基本套路。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/decoder.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv) | **ISA 的权威定义**。`localparam` 列出 11 个操作码，`case` 表给出每条指令拉高的控制信号。位域切分也在这里。 |
| [test/helpers/format.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py) | **反汇编器**。`format_instruction` 把 16 位二进制翻回可读汇编，`format_register` 把寄存器号翻成 `R0`/`%blockIdx` 等名字。 |
| [src/registers.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv) | 定义 16 个寄存器的初始化与读写保护，印证 R13–R15 的只读约定。 |
| [src/pc.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv) | 用 `decoded_nzp` 与 NZP 寄存器做「逐位与」决定 `BRnzp` 是否跳转，是 nzp 条件位的消费者。 |
| [test/test_matadd.py](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py) | matadd 内核的「程序内存」内容，本讲综合实践的核对基准。 |
| [docs/images/isa.png](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/docs/images/isa.png) | 项目自带的 ISA 速查图，本讲文字描述的总览版。 |

> 一句话定位：`decoder.sv` 定义「指令有哪些、长什么样」，`format.py` 是它的「逆运算」，`registers.sv` 与 `pc.sv` 是两类关键指令（访存/算术、跳转）的消费者。

## 4. 核心概念与源码讲解

### 4.1 ISA 全景：11 条指令清单

#### 4.1.1 概念说明

tiny-gpu 的 ISA 只有 **11 条指令**，刻意保持极简，目的是「刚好够写矩阵加法、矩阵乘法这类概念验证内核」。指令按用途可分四类：

- **跳转类**：`BRnzp`（条件分支）、`RET`（线程结束）。
- **比较类**：`CMP`（比较两寄存器，结果写入 NZP）。
- **算术类**：`ADD` / `SUB` / `MUL` / `DIV`（四则运算）。
- **访存/常数类**：`LDR`（从全局内存读）、`STR`（向全局内存写）、`CONST`（装载立即数）。
- 此外还有 `NOP`（空操作，占一个操作码但不做任何事）。

注意：算术指令**直接在寄存器间运算**，没有「寄存器-内存」混合寻址；要操作内存必须先用 `LDR` 取到寄存器，算完再用 `STR` 写回。这是一种典型的 **load-store 架构**。

#### 4.1.2 核心流程

操作码到助记符的映射在 `decoder.sv` 顶部用一串 `localparam` 写死，流程是：

1. decoder 取 `instruction[15:12]` 这 4 位作为 opcode。
2. 在 `case` 表里查到对应的助记符分支。
3. 该分支拉高一组 `decoded_*` 控制信号，告诉 ALU / LSU / registers / PC 该做什么。

完整的 11 个操作码如下（按数值升序）：

| 操作码 | 助记符 | 类别 | 一句话语义 |
|--------|--------|------|-----------|
| `0000` | `NOP`   | —     | 空操作，decoder 不拉任何控制信号 |
| `0001` | `BRnzp` | 跳转  | 若 NZP 寄存器匹配指令的 nzp 位，跳到 imm 指定行 |
| `0010` | `CMP`   | 比较  | 比较 rs、rt，把 N/Z/P 结果写入 NZP 寄存器 |
| `0011` | `ADD`   | 算术  | `rd ← rs + rt` |
| `0100` | `SUB`   | 算术  | `rd ← rs - rt` |
| `0101` | `MUL`   | 算术  | `rd ← rs * rt` |
| `0110` | `DIV`   | 算术  | `rd ← rs / rt` |
| `0111` | `LDR`   | 访存  | `rd ← mem[rs]` |
| `1000` | `STR`   | 访存  | `mem[rs] ← rt` |
| `1001` | `CONST` | 常数  | `rd ← imm` |
| `1111` | `RET`   | 结束  | 通知 scheduler 当前线程执行完毕 |

注意操作码并非连续：`1010`–`1110` 这 6 个编码未被使用，留给将来扩展。

#### 4.1.3 源码精读

11 个操作码的 `localparam` 定义在 decoder 的开头，这是整份 ISA 的「单一事实来源」：

[src/decoder.sv:34-44](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L34-L44) —— 用 `localparam` 把 4 位操作码逐个绑定到助记符常量，便于在 `case` 表里用名字而非魔数引用。

`case (instruction[15:12])` 这张大表则是「每条指令该做什么」的总账：

[src/decoder.sv:84-130](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L84-L130) —— 按 opcode 分支，每个分支拉高对应控制信号。例如 `ADD` 拉高 `reg_write_enable`、设 `reg_input_mux=2'b00`（写回 ALU 结果）、设 `alu_arithmetic_mux=2'b00`（选加法）。

#### 4.1.4 代码实践

**目标**：把操作码表内化成肌肉记忆。

**步骤**：
1. 打开 [src/decoder.sv:34-44](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L34-L44)。
2. 在纸上画一张三列空表：助记符 | 操作码（二进制）| 类别。
3. 不看答案，凭记忆把 11 行填满。
4. 对照本讲 4.1.2 的表核对。

**预期结果**：能默写出 11 个操作码及其类别。`NOP=0000`、`RET=1111` 这两个边界值最好记：一头一尾。

#### 4.1.5 小练习与答案

**练习 1**：操作码 `1010` 为什么没有对应指令？tiny-gpu 会怎样处理它？
**答案**：`1010` 属于未分配区间 `1010`–`1110`，decoder 的 `case` 表没有匹配分支，因此不会拉高任何控制信号（等价于 `NOP` 的效果），属于留给扩展的「空位」。

**练习 2**：`ADD` 与 `LDR` 都会拉高 `decoded_reg_write_enable`，它们写回寄存器的内容有何不同？
**答案**：`ADD` 写回的是 ALU 算出的 `rs+rt`（`reg_input_mux=2'b00` 选 ALU 输出）；`LDR` 写回的是从内存读回的数据（`reg_input_mux=2'b01` 选 LSU 输出）。区别由 `reg_input_mux` 这只多路选择开关决定。

---

### 4.2 16 位定长编码：位域分配

#### 4.2.1 概念说明

tiny-gpu 的每条指令都是 **16 位定长**。定长的好处是取指简单：fetcher 不必判断「这条指令有几个字节」，固定取 16 位即可（见 [u4-l3](u4-l3-fetcher-decoder.md) 的 fetcher）。

16 位被切成 4 个 **4 位字段**：

| 位域 | 字段 | 默认含义 |
|------|------|----------|
| `[15:12]` | opcode | 操作码，决定指令类型 |
| `[11:8]`  | rd     | 目标寄存器号 |
| `[7:4]`   | rs     | 源寄存器 1 |
| `[3:0]`   | rt     | 源寄存器 2 |

关键设计：**位域是复用的**。同一个 4 位字段在不同指令里含义不同。例如：
- 算术指令里 `[7:4]` 是 `rs`；但在 `CONST` / `BRnzp` 里，`[7:0]` 这 8 位整体被当作 **立即数 imm**。
- `[11:9]` 这 3 位在算术指令里属于 `rd` 的一部分，但在 `BRnzp` 里被当作 **nzp 条件位**。

这不是 bug，而是 ISA 设计的常规手法——用 opcode 决定「此刻这些位该怎么解读」，从而在 16 位里塞下足够多的信息。

#### 4.2.2 核心流程

给定一条汇编，编码成 16 位二进制的步骤：

1. 查表得到 opcode（4 位），填入 `[15:12]`。
2. 把每个寄存器操作数转成 4 位号（`R0=0000` … `R15=1111`），按 rd/rs/rt 顺序填入对应位域。
3. 若是 `CONST`/`BRnzp`，把立即数转成 8 位填入 `[7:0]`（rd 填 `[11:8]`，`[7:0]` 填 imm）。
4. 若是 `BRnzp`，把条件位填入 `[11:9]`（见 4.4）。
5. 未用到的位填 0。

解码（反汇编）则是逆过程：切字段 → 查 opcode → 按 opcode 决定其余字段的解读。

各指令实际用到的字段一览：

| 指令 | rd `[11:8]` | rs `[7:4]` | rt `[3:0]` | 复用说明 |
|------|------------|------------|------------|----------|
| `ADD/SUB/MUL/DIV rd,rs,rt` | ✓ 目标 | ✓ 源1 | ✓ 源2 | 三寄存器型 |
| `CMP rs,rt` | × | ✓ 源1 | ✓ 源2 | rd 不用 |
| `LDR rd,rs` | ✓ 目标 | ✓ 地址 | × | rt 不用 |
| `STR rs,rt` | × | ✓ 地址 | ✓ 数据 | rs=地址、rt=数据；rd 不用 |
| `CONST rd,#imm` | ✓ 目标 | imm 高 4 位 | imm 低 4 位 | `[7:0]`=imm |
| `BRnzp nzp,imm` | nzp 占 `[11:9]` | imm 高 4 位 | imm 低 4 位 | `[11:9]`=nzp、`[7:0]`=目标行 |
| `NOP` / `RET` | × | × | × | 无操作数 |

#### 4.2.3 源码精读

decoder 在 DECODE 拍切字段，这段代码同时定义了位域的物理位置：

[src/decoder.sv:66-70](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L66-L70) —— 把 `instruction` 的各段切给 `decoded_rd/rs/rt_address`、`decoded_immediate`（=`[7:0]`）、`decoded_nzp`（=`[11:9]`）。注意 `decoded_immediate` 与 `decoded_rs/rt_address` 在位域上是**重叠**的，谁有意义取决于 opcode。

`format.py` 做的是镜像切片。因为 cocotb 把信号值转成字符串时是 **MSB 在前**（`str[0]` 对应 `bit[15]`），所以 Python 的字符串切片与 Verilog 的位选一一对应：

[test/helpers/format.py:14-22](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L14-L22) —— `opcode=instruction[0:4]`（=`[15:12]`）、`rd=instruction[4:8]`（=`[11:8]`）、`rs=instruction[8:12]`（=`[7:4]`）、`rt=instruction[12:16]`（=`[3:0]`）、`imm=instruction[8:16]`（=`[7:0]`）。这正是 decoder 切片的逆运算。

#### 4.2.4 代码实践

**目标**：手工编码 `ADD R0, R0, %threadIdx`，逐位验证。

**步骤**：
1. opcode `ADD` = `0011`，填 `[15:12]`。
2. rd = `R0` = `0000`，填 `[11:8]`。
3. rs = `R0` = `0000`，填 `[7:4]`。
4. rt = `%threadIdx` = `R15` = `1111`，填 `[3:0]`。
5. 拼接：`0011 0000 0000 1111` = `0011000000001111`。

**预期结果**：得到 `0b0011000000001111`，与 [test/test_matadd.py:14](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L14) 第二条指令完全一致。这证明你的编码规则与项目一致。

#### 4.2.5 小练习与答案

**练习 1**：`CONST R1, #0` 应编码成什么？
**答案**：opcode `CONST`=`1001`；rd `R1`=`0001`；imm `#0`=`00000000`。拼接 `1001 0001 0000 0000` = `0b1001000100000000`。与 [test/test_matadd.py:15](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L15) 一致。

**练习 2**：`decoded_immediate` 与 `decoded_rs_address` 在物理上是同一些位，为什么不会冲突？
**答案**：因为它们由不同的 opcode 触发使用。算术指令只看 `rs/rt` 不会用 imm，`CONST` 只用 imm 不会用 rs/rt。decoder 切出全部字段，但每条指令实际消费哪些字段由 `case` 表里的控制信号决定。

---

### 4.3 寄存器约定：R0–R12 自由，R13–R15 只读

#### 4.3.1 概念说明

指令里的 rd/rs/rt 各是 4 位，因此一共能寻址 \(2^4 = 16\) 个寄存器，编号 `R0`–`R15`。这 16 个寄存器分成两组：

- **自由寄存器 `R0`–`R12`**（共 13 个）：可读可写，内核随便用作临时变量。
- **只读寄存器 `R13`–`R15`**：硬件在复位时写死，软件**不能写**，只能读。它们是 SIMD 的命脉：
  - `R13` = `%blockIdx`：当前 block 在整个 kernel 里的编号。
  - `R14` = `%blockDim`：一个 block 里有多少线程（即 `THREADS_PER_BLOCK`）。
  - `R15` = `%threadIdx`：当前线程在 block 内的编号。

为什么需要这三个只读寄存器？因为 SIMD 下所有线程跑**同一段代码**，要让他们处理**不同数据**，唯一的办法是让每个线程通过 `%threadIdx`/`%blockIdx` 算出「我该处理第几个元素」。matadd 内核第一行的 `i = blockIdx * blockDim + threadIdx` 就是经典用法。

#### 4.3.2 核心流程

寄存器的生命周期：

1. **复位**：`R0`–`R12` 清零；`R13` 暂置 0（运行中由 block_id 覆盖）；`R14 ← THREADS_PER_BLOCK`；`R15 ← THREAD_ID`（每个线程的硬件实例有自己的 `THREAD_ID` 参数）。
2. **运行中**：每个周期 `R13 ← block_id`，让寄存器堆随时反映当前正在跑的 block（dispatcher 切换 block 时无需软件干预）。
3. **写回保护**：UPDATE 拍只有当 `rd < 13` 时才允许写，从硬件层面保证 `R13`–`R15` 永远不被软件篡改。

`format.py` 用 `format_register` 把编号翻译回名字，让日志可读：

- `0`–`12` → `R0`–`R12`
- `13` → `%blockIdx`，`14` → `%blockDim`，`15` → `%threadIdx`

#### 4.3.3 源码精读

只读寄存器在复位时被赋初值，`THREADS_PER_BLOCK` 与 `THREAD_ID` 都是 core 例化时传入的参数：

[src/registers.sv:66-69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L66-L69) —— `registers[13]` 复位为 0（运行中再被 block_id 覆盖）、`registers[14] ← THREADS_PER_BLOCK`（`%blockDim`）、`registers[15] ← THREAD_ID`（`%threadIdx`）。

运行中每拍刷新 `%blockIdx`，是为了让 dispatcher 派发新 block 时寄存器堆能立刻反映新 block 的编号：

[src/registers.sv:72](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L72) —— `registers[13] <= block_id;`，注释坦承「这是不得已的笨办法，本不该每拍都写」。

写回保护是「只读」二字的硬件兑现：UPDATE 拍的写回条件里多了 `decoded_rd_address < 13` 这一关：

[src/registers.sv:83](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L83) —— 只有 `decoded_reg_write_enable && decoded_rd_address < 13` 同时成立才写寄存器，挡住一切对 `R13`–`R15` 的写企图。

`format_register` 则是这组约定的「显示端」：

[test/helpers/format.py:4-12](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L4-L12) —— 按编号返回 `R0`–`R12` 或三个百分号名字，所以日志里你看到的是 `%threadIdx` 而不是 `R15`。

#### 4.3.4 代码实践

**目标**：体会「只读」是硬件强制的，而非君子协定。

**步骤**：
1. 假设你写一条 `ADD R15, R0, R0`（企图把 0 写进 `%threadIdx`）。
2. 走一遍 decoder：`ADD` 会拉高 `reg_write_enable`、`rd=R15=1111`。
3. 查 [src/registers.sv:83](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L83) 的写回条件。
4. 判断 `R15` 是否被改写。

**预期结果**：因为 `1111 = 15`，不满足 `< 13`，写回被硬件拒绝，`%threadIdx` 保持不变。说明只读约束由数据通路保证，软件绕不过去。（这是一个「源码阅读型实践」，无需运行仿真。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `%blockIdx`（R13）在复位时被置 0，却又能随 block 变化，而 `%threadIdx`（R15）复位后就固定？
**答案**：一个线程的 `%threadIdx` 由它在硬件里的位置（`THREAD_ID` 参数）决定，永远不变；而同一个硬件线程实例会被 dispatcher 依次派去跑不同的 block，所以 `%blockIdx` 必须随 `block_id` 输入每拍更新（[registers.sv:72](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L72)）。

**练习 2**：matadd 内核用 `MUL R0, %blockIdx, %blockDim` 开头，这条指令读的是哪几个寄存器号？
**答案**：`%blockIdx`=`R13`=`1101`（rs），`%blockDim`=`R14`=`1110`（rt），`R0`=`0000`（rd）。这正是 [test/test_matadd.py:13](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L13) 的编码 `0b0101000011011110`。

---

### 4.4 BRnzp 的 nzp 条件位与跳转

#### 4.4.1 概念说明

`BRnzp` 是 tiny-gpu **唯一的控制流指令**，靠它实现循环和条件分支（matmul 内核的 `LOOP` 就靠它）。它的名字拆开看：

- `BR` = branch（分支）。
- `nzp` = 三位条件位 N / Z / P，分别表示 negative（负）/ zero（零）/ positive（正）。

工作原理是「**掩码匹配**」：
1. 先用 `CMP rs, rt` 比较，把比较结果（rs-rt 是负/零/正）写进 PC 单元里的一个 3 位 **NZP 寄存器**。
2. 再用 `BRnzp nzp, imm`：指令里的 nzp 三位是一个**掩码**，与 NZP 寄存器做**按位与**；只要结果非零，就跳转到 `imm` 指定的程序行，否则顺序执行下一行（PC+1）。

也就是说，`BRnzp` 不是「无条件跳转」，而是「条件命中才跳」。把 nzp 三位全填 1（`111`）就是无条件跳；只填某一位就是条件跳（如 matmul 的 `BRn` 只填 N 位，意为「上一条 CMP 结果为负则跳」）。

#### 4.4.2 核心流程

跳转判定由 PC 单元在 EXECUTE 拍完成，伪代码：

```
if decoded_pc_mux == 1:              # 这是 BRnzp 指令
    if (nzp_register & decoded_nzp) != 0:   # 掩码逐位与，非零即命中
        next_pc = decoded_immediate          # 跳到 imm 指定行
    else:
        next_pc = current_pc + 1             # 不命中，顺序往下
else:
    next_pc = current_pc + 1                 # 非跳转指令，默认 PC+1
```

指令里的 nzp 三位来自 `[11:9]`，跳转目标 imm 来自 `[7:0]`（即目标程序行的行号，范围 0–255）。NZP 寄存器则在 UPDATE 拍由 `CMP` 写入（写哪一位由 ALU 的比较结果决定，细节留给 [u5-l2 ALU](u5-l2-alu-nzp.md)）。

> 注意一个工程细节：ALU 在算 `rs - rt` 时用的是无符号 8 位运算，所以「负数」会回绕成大正数。这意味着实际置位的是哪一位，要看 [u5-l2](u5-l2-alu-nzp.md) 的 ALU 分析，本讲只关心**编码与匹配机制**本身。

#### 4.4.3 源码精读

`BRnzp` 在 decoder 里只做一件事：把 `decoded_pc_mux` 置 1，告诉 PC 单元「下一条要按跳转逻辑算 next_pc」：

[src/decoder.sv:88-90](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L88-L90) —— `BRnzp` 分支仅拉高 `decoded_pc_mux <= 1`，其余控制信号不动。

真正的跳转判定在 PC 单元，`decoded_nzp`（指令掩码）与 `nzp`（CMP 写入的寄存器）在这里相遇：

[src/pc.sv:42-55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L42-L55) —— EXECUTE 拍算 next_pc：若 `decoded_pc_mux==1` 且 `(nzp & decoded_nzp) != 0` 则 `next_pc <= decoded_immediate`，否则 `next_pc <= current_pc + 1`。

`decoded_nzp` 的位域来源就是 `[11:9]`：

[src/decoder.sv:70](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L70) —— `decoded_nzp <= instruction[11:9]`，这 3 位与 `rd` 字段的高 3 位重叠，仅在 `BRnzp` 时被当作条件掩码。

`format.py` 把这 3 位拆成 N/Z/P 三个字母显示：

[test/helpers/format.py:19-22](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L19-L22) —— `n/z/p` 分别取 `instruction[4]/[5]/[6]`（即 bit `[11]/[10]/[9]`），对应位为 1 则显示对应字母，imm 取 `[8:16]`（即 `[7:0]`）作为目标行号。

#### 4.4.4 代码实践

**目标**：拆解 matmul 的 `BRn LOOP` 编码，验证 nzp 位与跳转目标。

**步骤**：
1. 从 [test/test_matmul.py:38](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L38) 取出编码 `0b0001100000001100`。
2. 切位域：`[15:12]=0001`（BRnzp）；`[11:9]` 取第 11、10、9 位；`[7:0]` 取低 8 位。
3. 写成 `0001 | 1000 | 0000 | 1100`，读出 `[11:9]=100`、`[7:0]=00001100=12`。
4. 数一下 matmul.asm 里 `LOOP:` 标签位于第几行（从 0 开始数指令）。

**预期结果**：`[11:9]=100` 对应「只勾选第 2 位」（`format.py` 会显示成 `BRn`，即只检查 N 条件）；`[7:0]=12` 正是 `LOOP` 标签所在的 PC=12。证明 imm 字段就是目标行号。**待本地验证**：跑 `make test_matmul`，在日志里确认这条指令被反汇编成 `BRn LOOP` 形态（显示为 `BRnzp N, #12`）。

#### 4.4.5 小练习与答案

**练习 1**：若要写一条「无条件跳转到第 5 行」的指令，nzp 三位该怎么填？
**答案**：填 `111`（三位全 1）。这样 `(nzp_register & 111)` 只要 NZP 寄存器任意一位为 1 就非零。但要注意：若此前从未执行过 `CMP`，NZP 寄存器是复位值 `000`，`000 & 111 = 0` 仍不跳。因此真正无条件跳转需要先 `CMP` 置好至少一位，这是本 ISA 的一个局限。

**练习 2**：`BRnzp` 的跳转目标 imm 是 8 位，这对程序规模有什么限制？
**答案**：imm 8 位只能表示 0–255，而程序内存恰好是 8 位地址（256 行，见 [u3-l1](u3-l1-memory-model-interface.md)）。两者正好匹配，意味着任何程序行都能作为跳转目标，但程序总长不能超过 256 条指令。

---

### 4.5 format_instruction：用 Python 反汇编

#### 4.5.1 概念说明

硬件跑起来后，仿真日志里打印的是一串 16 位二进制，对人来说像天书。`format.py` 的 `format_instruction` 就是「翻译官」，把 16 位二进制翻回形如 `ADD R0, R0, %threadIdx` 的可读汇编。它和 decoder 是一对**互逆运算**：decoder 把指令切成字段去驱动硬件，`format_instruction` 把同样的字段拼回人话去驱动调试。

理解 `format_instruction` 有两层价值：
1. **调试**：看仿真日志时能立刻知道每个周期在执行什么。
2. **验证编码**：你手工编出来的二进制，喂给 `format_instruction` 看输出对不对，就知道自己编码有没有错。

#### 4.5.2 核心流程

`format_instruction(instruction)` 接收一个 16 字符的二进制字符串（cocotb 把信号值 `str()` 后即是此格式），流程：

1. 切出 opcode（`[0:4]`）、rd、rs、rt、nzp 各位、imm。
2. 用 `format_register` 把寄存器号翻译成 `R0`/`%blockIdx` 等名字。
3. 按 opcode 查表，拼出对应助记符的字符串。每条指令的**操作数顺序与个数**在这里定死：
   - 算术：`ADD rd, rs, rt`
   - `CMP rs, rt`（注意不显示 rd）
   - `LDR rd, rs`
   - `STR rs, rt`（rs 是地址在前，rt 是数据在后）
   - `CONST rd, #imm`
   - `BRnzp {n}{z}{p}, #imm`
   - `NOP` / `RET` 无操作数
4. opcode 不在表内则返回 `"UNKNOWN"`。

#### 4.5.3 源码精读

`format_instruction` 全貌就是一张 opcode→字符串的映射表：

[test/helpers/format.py:14-46](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L14-L46) —— 逐 opcode 拼 f-string。注意它对 `STR` 的处理：`f"STR {rs}, {rt}"`，与 decoder 里「rs=地址、rt=数据」的约定一致；对 `CMP` 只显示 `rs, rt`，因为比较结果不写 rd 而写 NZP 寄存器。

寄存器名的翻译独立成一个函数，被 `format_instruction` 和 `format_registers` 共用：

[test/helpers/format.py:4-12](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L4-L12) —— `format_register` 把 0–12 映射成 `R0`–`R12`，13/14/15 映射成三个百分号寄存器。

`format_instruction` 的调用点在 `format_cycle`，每个仿真周期对当前指令做一次反汇编并写日志：

[test/helpers/format.py:127](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L127) —— `logger.debug("Instruction:", format_instruction(instruction))`，这就是日志里每行 `Instruction: ...` 的来源。

#### 4.5.4 代码实践

**目标**：用 `format_instruction` 反查一条二进制，验证你的解码能力。

**步骤**：
1. 取 [test/test_matadd.py:24](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L24) 的 `0b1000000001110110`（注释是 `STR R7, R6`）。
2. 手工切：opcode=`1000`(STR)、`[11:8]=0000`、rs=`[7:4]=0111`=R7、rt=`[3:0]=0110`=R6。
3. 预测 `format_instruction` 的输出。
4. 与注释 `STR R7, R6` 比对。

**预期结果**：预测输出 `STR R7, R6`，与注释一致。进一步可在本地仿真时打开日志确认。**待本地验证**：跑 `make test_matadd`，在 `test/logs` 里找到这条 `Instruction: STR R7, R6`。

#### 4.5.5 小练习与答案

**练习 1**：给定 `0b0010000010010010`，`format_instruction` 会输出什么？（提示：这是 matmul 的 `CMP R9, R2`）
**答案**：opcode=`0010`(CMP)；rd=`[11:8]=0000`（CMP 不用 rd，不显示）；rs=`[7:4]=1001`=R9；rt=`[3:0]=0010`=R2。输出 `CMP R9, R2`。与 [test/test_matmul.py:37](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matmul.py#L37) 一致。

**练习 2**：为什么 `format_instruction` 对 `CMP` 只显示 `rs, rt`，而对 `ADD` 显示 `rd, rs, rt`？
**答案**：`ADD` 的结果写回 `rd`，所以 rd 是有效操作数必须显示；`CMP` 的结果不写 rd，而是写 PC 单元的 NZP 寄存器，rd 字段被忽略，故不显示。这体现了「opcode 决定字段含义」的设计。

---

## 5. 综合实践

把本讲全部知识串起来：**手工编码 matadd 内核的前 4 条指令，并逐条核对项目源码**。

### 背景

[test/test_matadd.py:12-26](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L12-L26) 的 `program` 列表是 matadd 内核的「程序内存」内容，每行一条 16 位指令。对应汇编见 [README.md 的 matadd.asm](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md)（`.threads`/`.data` 是伪指令，不计入程序内存）。前 4 条指令是：

```asm
MUL R0, %blockIdx, %blockDim       ; 第 1 条
ADD R0, R0, %threadIdx             ; 第 2 条
CONST R1, #0                        ; 第 3 条
CONST R2, #8                        ; 第 4 条
```

### 步骤

1. 为每条汇编，按 4.2 的流程填出一张 4 段位域表（opcode / rd / rs / rt 或 imm）。
2. 拼成 16 位二进制字符串。
3. 与下表（取自 `test_matadd.py`）逐位对照。

### 参考编码（请先自己做再对照）

| # | 汇编 | opcode | rd | rs | rt / imm | 编码 |
|---|------|--------|----|----|----------|------|
| 1 | `MUL R0, %blockIdx, %blockDim` | `0101` | `0000` | `1101` (R13) | `1110` (R14) | `0101000011011110` |
| 2 | `ADD R0, R0, %threadIdx` | `0011` | `0000` | `0000` | `1111` (R15) | `0011000000001111` |
| 3 | `CONST R1, #0` | `1001` | `0001` | — | imm=`00000000` | `1001000100000000` |
| 4 | `CONST R2, #8` | `1001` | `0010` | — | imm=`00001000` | `1001001000001000` |

逐位核对 [test/test_matadd.py:13-16](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/test_matadd.py#L13-L16)，应当完全一致。

### 进阶（可选）

5. 把第 1 条 `0101000011011110` 喂给 `format_instruction`（在本地起一个 Python 解释器，`from test.helpers.format import format_instruction`），确认输出 `MUL R0, %blockIdx, %blockDim`——这就完成了一次「编码 → 反汇编」的闭环验证。**待本地验证**。

## 6. 本讲小结

- tiny-gpu 的 ISA 只有 **11 条指令**，操作码定义在 [decoder.sv:34-44](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/decoder.sv#L34-L44)，是整份指令集的权威来源。
- 指令是 **16 位定长**，切成 `opcode[15:12] / rd[11:8] / rs[7:4] / rt[3:0]` 四段；同一字段在不同指令里可被**复用**（如 `[7:0]` 在算术里是 rs/rt、在 CONST/BRnzp 里是 imm）。
- 16 个寄存器中 `R0`–`R12` 自由读写，`R13`–`R15` 是只读的 `%blockIdx`/`%blockDim`/`%threadIdx`，写回保护由 [registers.sv:83](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/registers.sv#L83) 的 `rd < 13` 判断实现。
- `BRnzp` 用 `[11:9]` 作 nzp 掩码、`[7:0]` 作跳转目标，与 CMP 写入的 NZP 寄存器做**逐位与**决定是否跳转（[pc.sv:42-55](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/pc.sv#L42-L55)）。
- `format.py` 的 `format_instruction` 是 decoder 的**逆运算**，用于把二进制反汇编成人话，是读仿真日志与验证手工编码的关键工具。
- 编码能力可通过与 `test_matadd.py` / `test_matmul.py` 里的 `program` 列表逐位比对来校验，无需运行仿真也能确认对错。

## 7. 下一步学习建议

- 想搞清 `CMP` 到底把哪一位置进 NZP 寄存器、以及无符号运算带来的「负数回绕」现象，请接着读 **[u5-l2 ALU 与比较/NZP](u5-l2-alu-nzp.md)**——那里从 `alu.sv` 的比较表达式逐位解释 alu_out[2:0]。
- 想了解 `LDR`/`STR` 这两条访存指令在硬件里如何异步握手，请读 **[u5-l3 LSU 异步访存](u5-l3-lsu-async-memory.md)**。
- 想亲手写一个新内核并接入仿真，跳到 **[u6-l3 编写与仿真内核](u6-l3-writing-kernels.md)**，那里以 matadd/matmul 为范本教你用本讲的 ISA 写程序。
- 推荐先在本地跑通 `make test_matadd`，打开 `test/logs` 下的日志，对照本讲学的编码规则去读每条 `Instruction:` 行——能把「纸面编码」和「真实执行轨迹」对上号，本讲才算真正落地。
