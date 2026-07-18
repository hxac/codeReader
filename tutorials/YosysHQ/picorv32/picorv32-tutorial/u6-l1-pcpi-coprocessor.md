# PCPI 协处理器接口

## 1. 本讲目标

PicoRV32 的核心（`picorv32` 模块）只「原生认识」基础整数指令集 RV32I（外加可选的 C 压缩扩展）。那乘法 `mul`、除法 `div`、求余 `rem` 这些属于 RISC-V **M 扩展**的指令，甚至用户自己发明的**自定义指令**，该由谁来执行？

答案是：**Pico Co-Processor Interface（PCPI，Pico 协处理器接口）**。它把核心当作一个「前台接待」——遇到自己不认识的指令，核心不直接报错，而是把指令字和两个源操作数递交给一个（或多个）协处理器，等协处理器算完后把结果收回来写回寄存器。

学完本讲，你应当能够：

1. 说清 PCPI 七根信号（`pcpi_valid/insn/rs1/rs2/wr/rd/wait/ready`）的方向、含义与握手时序，并解释「CPU 负责读寄存器、协处理器只负责出结果」的分工。
2. 看懂 `picorv32_pcpi_mul`、`picorv32_pcpi_fast_mul`、`picorv32_pcpi_div` 三个内置协处理器如何用同一套 PCPI 协议实现整个 M 扩展，并理解它们各自的多周期算法（进位保留乘法 / 单周期硬乘法器 / 恢复除法）。
3. 解释核心遇到真正非法的指令时，如何用 `pcpi_timeout_counter` 给协处理器 16 个时钟周期的窗口，超时后如何转入非法指令陷入（trap）或 EBREAK 中断。
4. 能照着 `picorv32_pcpi_mul` 的写法，自己用 Verilog 写一个最小的 PCPI 协处理器（例如实现一条 `popcount` 自定义指令），并正确驱动 `pcpi_wait` 防止超时。

## 2. 前置知识

本讲是「专家层」内容，建立在前面几讲已建立的认知之上，这里只做最简回顾：

- **主状态机 `cpu_state`（u4-l2）**：核心是台多周期 CPU，指令在 `fetch`（取指/译码/回写）→ `ld_rs1`/`ld_rs2`（读源寄存器）→ `exec`（执行）等状态间流转。`ld_rs1` 是按指令类型分流的「道岔」，PCPI 派发就发生在 `ld_rs1`（双端口寄存器堆）或 `ld_rs2`（单端口）状态。
- **端口与接口（u3-l2）**：核心有六类端口，PCPI 是其中独立的一类。回忆 `trap` 是「不可恢复的死锁」，只能靠 `resetn` 恢复；而中断（IRQ）是「可恢复」的——本讲会看到非法指令既可以走到 `trap`，也可以走 EBREAK 中断。
- **寄存器堆与 ALU（u5-l1）**：`reg_op1`/`reg_op2` 是「锁存好的源操作数」，PCPI 正是把它们原样送上 `pcpi_rs1`/`pcpi_rs2`。
- **译码器（u4-l1）**：`instr_trap` 是「这条指令我没认出来」的一位信号，它是触发 PCPI 派发的总开关。

一个关键的术语准备：**M 扩展（M Standard Extension）**是 RISC-V 的乘除法指令子集，包含 `mul`、`mulh`、`mulhsu`、`mulhu`、`div`、`divu`、`rem`、`remu` 共 8 条。它们的 opcode 都是 `0110011`，用 `funct7=0000001` 与普通的 `add/sub/sll` 等区分，再用 `funct3` 区分具体是哪一条。本讲会反复用到这个编码。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [picorv32.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v) | CPU 主体。前 ~2200 行是 `picorv32` 核心，包含 PCPI 端口、派发逻辑、超时计数器；后 ~800 行是三个内置协处理器模块（`picorv32_pcpi_mul`/`fast_mul`/`div`）和 AXI/Wishbone 适配器。 |
| [README.md](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md) | 「Pico Co-Processor Interface (PCPI)」一节给出协议的文字定义；`ENABLE_PCPI/MUL/FAST_MUL/DIV` 参数说明；以及 M 指令的周期数。 |
| [testbench.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v) | 默认 `make test` 用的测试台，实例化时开启了 `ENABLE_MUL(1)` 与 `ENABLE_DIV(1)`，所以日常测试就在练习 PCPI。 |
| [scripts/smtbmc/mulcmp.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/mulcmp.v) | 形式化验证用的「对照器」：把 `picorv32_pcpi_mul` 与 `picorv32_pcpi_fast_mul` 同时实例化、喂同样的输入，比对结果是否一致。是学习「如何正确连线一个 PCPI 协处理器」的好范例。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**PCPI 握手协议**、**内置 MUL/DIV 协处理器**、**超时与非法指令陷入**。三者构成一条完整链路：协议（怎么对话）→ 实现（内置协处理器怎么算）→ 兜底（没人应答怎么办）。

### 4.1 PCPI 握手协议

#### 4.1.1 概念说明

PCPI 的设计思想非常简单：**核心自己不动手算复杂指令，而是当个传话筒**。

设想你（协处理器）和前台（CPU 核心）的关系：

1. 前台收到一张看不懂的工单（非法/未实现指令）。
2. 前台把工单原文（`pcpi_insn`）和工单里点名的两个原料（`pcpi_rs1`、`pcpi_rs2`）摆到柜台上，按铃通知（`pcpi_valid`）。
3. 你看到铃响了，扫一眼工单：
   - **是我的活**：马上喊「在做了别催」（`pcpi_wait`），开始算；算完喊「好了，结果在这」（`pcpi_ready` + `pcpi_rd` + `pcpi_wr`）。
   - **不是我的活**：什么都不做，假装没听见。
4. 前台收到 `pcpi_ready` 后，把 `pcpi_rd` 写回到工单指定的目标寄存器 `rd`，然后继续取下一条指令。
5. 如果 16 个时钟周期内**没有任何人**应答，前台判定这是一张真正的废工单，转入非法指令异常。

这里有一个极其重要的分工，初学者常看漏：

> **CPU 自己负责解码 `rd/rs1/rs2` 字段并读出 rs1、rs2 的值；协处理器只需要吐出一个 `pcpi_rd`，CPU 负责把它写到 `rd`。**

也就是说，协处理器**不需要**自己再去碰寄存器堆，它只面对「指令字 + 两个操作数」这三样纯输入，和「一个结果 + 两根控制线」这些纯输出。这让协处理器可以做得很纯粹、很容易接。

#### 4.1.2 核心流程

把上面的故事翻译成时序（所有信号在 `posedge clk` 采样）：

```
          ┌───────── CPU 核心 (picorv32) ─────────┐         ┌── 协处理器 ──┐
 取指/译码 │  识别到 instr_trap 且 WITH_PCPI=1      │         │              │
    │     │  在 ld_rs1/ld_rs2 锁存 reg_op1/op2    │         │              │
    ▼     │  pcpi_valid ────────────────────────►│────────►│ 看 pcpi_insn │
          │  pcpi_insn  = mem_rdata_q(指令原字)   │         │ 是不是我的?  │
          │  pcpi_rs1   = reg_op1                │         │              │
          │  pcpi_rs2   = reg_op2                │         │   是→pcpi_wait│◄─ 冻结超时
          │                                       │         │   开始计算   │
          │  ◄──────────────── pcpi_wait ────────│◄────────│              │
          │      （等待期间，超时计数器被冻结）    │         │   算完→      │
          │  ◄──────────────── pcpi_ready ───────│◄────────│ pcpi_ready=1 │
          │  ◄──────────────── pcpi_wr ──────────│◄────────│ pcpi_wr=1    │
          │  ◄──────────────── pcpi_rd[31:0] ────│◄────────│ pcpi_rd=结果 │
          │  reg_out <= pcpi_int_rd              │         │              │
          │  latched_store <= pcpi_int_wr        │         │              │
          │  pcpi_valid <= 0                     │         │              │
          │  cpu_state <= fetch（回写下一条）     │         │              │
          └───────────────────────────────────────┘         └──────────────┘
```

三个要点：

- **`pcpi_valid` 是电平**：核心一旦拉高，会一直保持到收到 `pcpi_ready` 或超时，期间 `pcpi_insn/rs1/rs2` 保持稳定。
- **`pcpi_wait` 是「续命线」**：协处理器只要还在算，就持续拉高 `pcpi_wait`；它一拉高，核心的超时计数器就被冻结（见 4.3）。算多久都不怕。
- **`pcpi_ready` 是「成交」**：这一拍 `pcpi_int_ready` 为高，核心在**同一拍**就把结果收走并回到 `fetch`。`pcpi_wr` 决定「要不要写回」（例如某些指令没有 `rd`、或协处理器只是借机做副作用，可以不写回）。

#### 4.1.3 源码精读

**(1) 端口契约** —— PCPI 的全部外部信号定义在模块端口列表里，CPU 侧输出 4 根、输入 4 根：

[picorv32.v:109-117](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L109-L117) 这段声明了 PCPI 的 8 根信号及其方向：`pcpi_valid`/`pcpi_insn`/`pcpi_rs1`/`pcpi_rs2` 是核心输出，`pcpi_wr`/`pcpi_rd`/`pcpi_wait`/`pcpi_ready` 是核心输入。

注意 `pcpi_valid` 与 `pcpi_insn` 是 `output reg`（核心在状态机里驱动它们），而 `pcpi_rs1`/`pcpi_rs2` 只是普通 `output`（组合输出，下一行就给出）。

**(2) 操作数从哪来** —— 就是上一讲讲的 `reg_op1`/`reg_op2`：

[picorv32.v:191-192](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L191-L192) 把已经锁存好的源操作数直接送上 PCPI：`assign pcpi_rs1 = reg_op1; assign pcpi_rs2 = reg_op2;`。这正是「CPU 负责读寄存器、协处理器只出结果」分工的物证。

**(3) `WITH_PCPI` 总开关** —— 只要任一相关特性被打开，PCPI 派发通路就启用：

[picorv32.v:169](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L169) 定义 `localparam WITH_PCPI = ENABLE_PCPI || ENABLE_MUL || ENABLE_FAST_MUL || ENABLE_DIV;`。注意这里的「或」：即便你没开外部的 `ENABLE_PCPI`，只要开了内置的乘除法（`ENABLE_MUL/DIV`），派发逻辑也会被编译进来——因为内置乘除法核本身就是挂在 PCPI 上的协处理器。

**(4) 多协处理器的「汇流」** —— 核心可以同时挂外部协处理器 + 内置 mul + 内置 div，它们的应答线先 OR 起来，再用优先级 `case` 选出谁的结果：

[picorv32.v:325-346](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L325-L346) 这段 `always @*` 是关键的汇流逻辑。`pcpi_int_wait`/`pcpi_int_ready` 是三路（外部 `pcpi_wait/ready`、内置 `pcpi_mul_*`、内置 `pcpi_div_*`）的按位或——只要有任何一方在 wait，核心就继续等；任何一方 ready，核心就当成交。而 `pcpi_int_wr`/`pcpi_int_rd` 则由 `case (1'b1)` 按「外部 > mul > div」的优先级选出实际写回的结果。被关闭的特性（如 `ENABLE_PCPI=0`）对应的项与 0 相与，自然不参与。

**(5) 触发条件 `instr_trap`** —— 什么样的指令才会被送去 PCPI？答案是「核心没有原生译码的指令」：

[picorv32.v:679-685](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L679-L685) 给出 `instr_trap` 的定义：把所有原生支持的 `instr_*` 一位信号拼成一个大向量，取反后做与。也就是说，**只要这条指令没匹配上任何一条原生 RV32I 指令，`instr_trap` 就为 1**。请仔细看这个列表里**没有** `instr_mul`/`instr_div`——这正是因为核心从不原生译码 M 扩展，所以 `mul`/`div` 永远命中 `instr_trap`，永远走 PCPI。这就是「M 扩展完全由协处理器实现」的根因。

> 注意 `instr_trap` 还受 `(CATCH_ILLINSN || WITH_PCPI)` 前缀门控：两者都为 0 时，非法指令会被静默当 NOP（不陷入也不派发），这是给极简核的选项；只要开了 PCPI，`instr_trap` 就会正常生成。

#### 4.1.4 代码实践

**实践目标**：用一条真实的 `mul` 指令，把「取指 → 译码命中 `instr_trap` → PCPI 派发 → 协处理器应答 → 回写」这条链路在源码里走一遍，建立协议的具象感受。

**操作步骤**：

1. 打开 [tests/mul.S](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/tests/mul.S)，看一条最简单的 `mul` 测试用例的汇编写法（它会被默认 `make test` 编进固件）。
2. 打开 [picorv32.v:1585-1616](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1585-L1616) 的 `cpu_state_ld_rs1` 中 `(CATCH_ILLINSN || WITH_PCPI) && instr_trap` 分支。逐行标注：
   - `reg_op1 <= cpuregs_rs1;` / `reg_op2 <= cpuregs_rs2;`（在双端口分支里）—— 这两行就是把源操作数送上 PCPI 的源头。
   - `pcpi_valid <= 1;` —— 按铃。
   - `if (pcpi_int_ready) ... reg_out <= pcpi_int_rd; latched_store <= pcpi_int_wr;` —— 收货并准备回写。
3. 再看 [picorv32.v:1768-1786](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1768-L1786)，这是**单端口寄存器堆**（`ENABLE_REGS_DUALPORT=0`）时走 `cpu_state_ld_rs2` 的「同一套逻辑的第二份」——因为单端口要先读 rs1、再读 rs2，PCPI 派发被推迟到读完 rs2 之后。

**需要观察的现象**：PCPI 派发逻辑在源码里出现了**两次**（`ld_rs1` 与 `ld_rs2`），结构几乎一致。这是单/双端口寄存器堆带来的代码重复。

**预期结果**：你能用一句话讲清「为什么 `mul` 指令在 PicoRV32 里等价于一次 PCPI 事务，而不是一次 ALU 运算」。

**运行验证（可选）**：因为 [testbench.v:168-173](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L168-L173) 默认开了 `ENABLE_MUL(1)` 与 `ENABLE_DIV(1)`，所以 `make test`（需先按 u2-l1 装好 RISC-V 工具链）会跑过全部 `tests/*mul*.S`、`tests/*div*.S`，若全部 `OK` 即说明 PCPI 链路正常。

#### 4.1.5 小练习与答案

**练习 1**：如果同一个周期里，外部协处理器和内置 `picorv32_pcpi_div` 同时拉高了自己的 `pcpi_ready`，核心会用谁的结果？为什么？

**答案**：用外部协处理器的结果。看 [picorv32.v:331-345](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L331-L345) 的 `(* parallel_case *) case (1'b1)`：第一项 `ENABLE_PCPI && pcpi_ready` 命中后，`parallel_case` 保证不再评估后面的 mul/div 项。注意 `pcpi_int_wait`/`ready` 是按位或，所以双方都会让核心「成交」，但**结果二选一**按优先级取外部。实际中不应让两个协处理器同时认领同一条指令。

**练习 2**：`pcpi_insn` 送出去的是「译码后的一位信号」还是「指令原始 32 位字」？协处理器为什么需要它？

**答案**：是**原始 32 位指令字**。见 [picorv32.v:1037-1038](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1037-L1038)，`pcpi_insn <= WITH_PCPI ? mem_rdata_q : 'bx;`。因为核心把 M 扩展（以及任何自定义指令）整体外包，协处理器必须自己从 `funct3`/`funct7` 判断「具体是哪一条、操作数怎么解释」（如有符号/无符号）。这也意味着协处理器要自带一个小译码器。

---

### 4.2 内置 MUL/DIV 协处理器

#### 4.2.1 概念说明

上一节说「M 扩展由协处理器实现」，那这些协处理器在哪？答案是：**就内嵌在 `picorv32.v` 同一个文件里**，与核心一起综合。PicoRV32 提供三个内置 PCPI 协处理器模块：

| 模块 | 实现的指令 | 算法 | 速度（README 实测） |
|------|-----------|------|---------------------|
| `picorv32_pcpi_mul` | `mul` / `mulh` / `mulhsu` / `mulhu` | 多周期**进位保留（carry-save）**移位累加 | `mul` 40 周期，`mulh*` 72 周期 |
| `picorv32_pcpi_fast_mul` | 同上 | **单周期硬乘法器**（用 `*` 综合成 DSP） | ~1–2 周期 |
| `picorv32_pcpi_div` | `div` / `divu` / `rem` / `remu` | 多周期**恢复除法（restoring division）** | 40 周期 |

它们由 `ENABLE_MUL` / `ENABLE_FAST_MUL` / `ENABLE_DIV` 三个参数分别打开（[README.md:239-264](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L239-L264)）。一个关键点要分清：

> `ENABLE_MUL/FAST_MUL/DIV` 开启的是**内部**协处理器核；而 `ENABLE_PCPI` 开启的是把 `pcpi_*` 信号**引到模块外部**的端口。两者独立。你可以只开内置乘法（不引出外部端口），也可以只引出外部端口（自己外接协处理器，内部不实例化任何核）。

这一设计的精妙之处在于**复用**：实现 M 扩展用的是「核心遇到陌生指令 → 派发 → 收结果」这套通用机制，与外接用户自定义协处理器**完全相同**。所以读这三个模块，既是在学 M 扩展怎么实现，也是在学「怎么写一个 PCPI 协处理器」的标准范式。

#### 4.2.2 核心流程

三个模块都遵循同一套与核心对话的状态机骨架：

```
        ┌─ 收到 pcpi_valid ─┐
        ▼                   │
  解码 pcpi_insn：          │
  opcode=0110011?           │
  funct7=0000001?           │
  funct3=? (mul/div/...)    │
        │                   │
   是我的指令?──否──► 什么都不做（pcpi_wait/ready 保持 0）
        │是                  ▲
        ▼                   │（这样核心若没人应答会超时陷入，
  pcpi_wait <= 1 ◄──────────┘ 但这条指令不是我的，本就该陷）
        │
        ▼ （多周期计算：移位累加 / 恢复除法）
        │ 每周期检查「算完了吗?」
        │
       算完
        │
        ▼
  pcpi_ready <= 1
  pcpi_wr    <= 1
  pcpi_rd    <= 计算结果
```

「是我的指令吗」这一步是关键：模块用 `pcpi_insn[6:0]`（opcode）和 `pcpi_insn[31:25]`（funct7）判断是否属于 M 扩展，再用 `pcpi_insn[14:12]`（funct3）区分具体指令。只有命中时才拉 `pcpi_wait`——这保证「不是我的指令」不会被错误地拖延核心。

#### 4.2.3 源码精读

**(1) 多周期乘法器 `picorv32_pcpi_mul`** —— 用进位保留累加器实现，避免长进位链拖慢 fmax：

[picorv32.v:2193-2211](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2193-L2211) 模块头与端口。参数 `STEPS_AT_ONCE`（每拍算几位）与 `CARRY_CHAIN`（进位链长度）可调面积与速度。

[picorv32.v:2221-2238](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2221-L2238) 译码与 `pcpi_wait` 生成。`pcpi_insn[6:0]==7'b0110011 && pcpi_insn[31:25]==7'b0000001` 判定是 M 扩展，`case (pcpi_insn[14:12])` 分出 `mul/mulh/mulhsu/mulhu`。`pcpi_wait <= instr_any_mul;`——命中即拉 wait。`mul_start = pcpi_wait && !pcpi_wait_q;` 用一拍延迟造出上升沿，作为真正启动计算的触发。

[picorv32.v:2248-2271](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2248-L2271) 进位保留累加的核心。它维护**两个** 64 位寄存器 `rd`（部分和）与 `rdx`（进位），而不是立刻把它们加起来——这样每拍只是异或 + 与/或，没有 64 位加法的长进位链，时序很友好。每拍看 `rs1` 最低位决定要不要把移位后的 `rs2` 累加进来，然后 `rs1>>1`、`rs2<<1`。

[picorv32.v:2307-2315](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2307-L2315) 收尾：`mul_counter` 减到最高位变 1 时置 `mul_finish`，随后一拍拉高 `pcpi_ready`/`pcpi_wr`，并根据是否 `mulh*`（需要高 32 位）选择 `rd` 还是 `rd>>32` 送 `pcpi_rd`。这也解释了为什么 `mulh*` 比 `mul` 慢——它要把 64 位全算完（计数器初值 63 vs 31）。

**(2) 单周期硬乘法器 `picorv32_pcpi_fast_mul`** —— 直接用 Verilog 的 `*`，让综合工具映射到 FPGA 的 DSP 块：

[picorv32.v:2364-2376](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2364-L2376) 用一个 `active[3:0]` 移位寄存器做流水线：`rd <= $signed(rs1) * $signed(rs2);` 是真正的乘法，`active` 逐拍移位标记进度。注意它的 `pcpi_wait = 0`（[picorv32.v:2402](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2402)）——因为够快，不需要「续命」。

[picorv32.v:2401-2412](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2401-L2412) 输出：`pcpi_ready` 直接取 `active` 的某一位（取决于要不要插额外寄存器 `EXTRA_MUL_FFS`），`pcpi_rd` 据是否取高半字。`RISCV_FORMAL_ALTOPS` 宏分支是给形式化验证用的「简化替代运算」，正常综合走 `else` 分支。

**(3) 恢复除法器 `picorv32_pcpi_div`** —— 经典的「试探减法」二进制长除法：

[picorv32.v:2438-2455](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2438-L2455) 译码逻辑：`funct3` 的 `100/101/110/111` 分别对应 `div/divu/rem/remmu`，同样 `pcpi_wait <= instr_any_div_rem && resetn;`。

[picorv32.v:2464-2509](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2464-L2509) 除法主循环。算法是恢复除法：把除数左移 31 位对齐到最高位，每拍比较「当前除数 ≤ 被除数吗」，是则相减并在商的对应位上置 1，然后除数右移 1 位、商掩码右移 1 位。32 拍后 `quotient_msk` 归零，触发 `pcpi_ready`。

被除数与除数的符号处理在 `start` 那一拍一次性完成（[picorv32.v:2474-2476](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2474-L2476)）：对有符号指令先取绝对值参与运算，再用 `outsign` 记下结果应为负，最后还原。余数留在 `dividend` 里。整体周期数 README 标注为 40（[README.md:356-360](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L356-L360)）。

#### 4.2.4 代码实践

**实践目标**：用形式化对照器 `mulcmp.v` 理解「如何正确实例化一个 PCPI 协处理器」，并对比两种乘法器的应答时序。

**操作步骤**：

1. 打开 [scripts/smtbmc/mulcmp.v:29-41](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/mulcmp.v#L29-L41)，这是一个把 `picorv32_pcpi_mul` 实例化的干净范例：注意它把**同一组** `pcpi_insn/rs1/rs2` 喂给乘法器，然后把输出的 `pcpi_wr/rd/wait/ready` 接出来——这就是 PCPI 协处理器完整的 9 根连线（clk/resetn + 7 根协议信号）。
2. 在 [picorv32.v:272-323](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L272-L323) 的 `generate` 里，对照看核心内部是怎么把同一个 `picorv32_pcpi_mul`（或 `fast_mul`、`div`）接上去的：所有协处理器**共用** `pcpi_valid/insn/rs1/rs2` 这组输出（并联），各自独立回 `pcpi_*_wr/rd/wait/ready`，再汇流到 4.1.3(4) 看过的 `pcpi_int_*`。
3. 比较 [picorv32.v:2236](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2236)（`pcpi_wait <= instr_any_mul;`，多周期乘法要 wait）与 [picorv32.v:2402](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2402)（`assign pcpi_wait = 0;`，快速乘法不要 wait）。

**需要观察的现象**：`fast_mul` 的 `pcpi_wait` 恒为 0，意味着它要么在「16 周期内」就 ready，要么会因为来不及应答而超时——实际上它靠流水线在数拍内 ready，所以安全。

**预期结果**：你能解释「为什么把 `ENABLE_MUL` 换成 `ENABLE_FAST_MUL` 后，`mul` 指令的 CPI 显著下降，但综合后大概率会用到 FPGA 的 DSP 资源」。

**待本地验证**：若你装了 yosys，可 `make test` 后对比两种配置下的 cycle 计数差异。

#### 4.2.5 小练习与答案

**练习 1**：`picorv32_pcpi_mul` 为什么用「进位保留」（维护 `rd` 和 `rdx` 两个寄存器）而不是每拍直接做 64 位加法？

**答案**：为了保护时序（fmax）。每拍做 64 位全加会产生很长的进位传播链，严重拖慢最高时钟频率。进位保留每拍只做异或和按位与/或（无长进位链），把「合并进位」推迟到计算末尾，单拍组合深度很浅。代价是需要的周期数多（约 40 拍），但 PicoRV32 本就是多周期、以 CPI 换面积的核，这个取舍符合其定位。

**练习 2**：`picorv32_pcpi_div` 里 `quotient_msk` 从 `1<<31` 开始每拍右移 1 位，到 0 时结束。这为什么恰好是 32 拍？

**答案**：因为商有 32 位、要从最高位（bit31）逐位确定到最低位（bit0）。`quotient_msk` 是个「1 游标」，标记当前在确定商的哪一位：初始指向 bit31，每确定一位就 `>>1` 移向下一位，移 32 次后变为 0，表示 32 位商全部求完。这正是二进制长除法「从高到低逐位试商」的硬件实现。

---

### 4.3 超时与非法指令陷入

#### 4.3.1 概念说明

设想一个棘手的情形：核心遇到一条指令，把它送上 PCPI，然后……**没有任何协处理器应答**。可能是因为：

- 这是一条真正非法的指令（编码错误、固件 bug）；
- 或者外接协处理器坏了/没接/还没算完且忘了拉 `pcpi_wait`。

核心不能就这么无限等下去。PCPI 的兜底机制是：**给所有协处理器 16 个时钟周期的窗口**。如果窗口内既没有 `pcpi_wait`（有人在算）也没有 `pcpi_ready`（有人交差），核心就判定这是非法指令，转入异常处理。

这里要严格区分两个概念（承接 u3-l2）：

- **`trap`（不可恢复死锁）**：核心进入 `cpu_state_trap`，拉高 `trap` 端口，彻底停下，只能靠 `resetn` 恢复。
- **EBREAK 中断（可恢复）**：如果开了中断且 `irq_ebreak` 未被屏蔽，非法指令（含 `ebreak`）会触发一次中断，CPU 跳到 `PROGADDR_IRQ` 去执行处理程序，处理完还能回来。

PCPI 超时后走哪条路，取决于 `ENABLE_IRQ` 与 `irq_mask[irq_ebreak]`。

#### 4.3.2 核心流程

超时机制由一个 **4 位下行计数器** `pcpi_timeout_counter` 驱动。它的行为可以浓缩成一张状态表：

| 条件 | 计数器动作 | 含义 |
|------|-----------|------|
| `pcpi_valid=0` 或 `pcpi_int_wait=1` | 重装为 `~0`（=15） | 没在派发，或有人在算（wait）→ 计时冻结 |
| `pcpi_valid=1` 且 `pcpi_int_wait=0` | 每拍 -1 | 派发中且无人 wait → 开始倒计时 |
| 计数器减到 0 | `pcpi_timeout <= 1` | 16 拍窗口耗尽 → 判非法 |

为什么是「16」拍？因为 4 位计数器从 15 数到 0 共经历 16 个值。这个窗口的意义是：给「够快但非单周期」的协处理器（比如 `fast_mul` 的 2–3 拍流水线）留出余量，同时不让真正无主的指令无限拖延 CPU。

一旦 `pcpi_timeout` 为 1（或指令本身就是 `ebreak`），`ld_rs1`/`ld_rs2` 里的派发分支会执行收尾：

```
if (CATCH_ILLINSN && (pcpi_timeout || instr_ecall_ebreak)) begin
    pcpi_valid <= 0;                       // 撤销派发
    if (ENABLE_IRQ && !irq_mask[irq_ebreak] && !irq_active) begin
        next_irq_pending[irq_ebreak] = 1;  // 触发 EBREAK 中断（可恢复）
        cpu_state <= cpu_state_fetch;
    end else
        cpu_state <= cpu_state_trap;       // 否则死锁（不可恢复）
end
```

注意一个**容易踩的坑**：超时检查被 `CATCH_ILLINSN` 门控。如果你设了 `ENABLE_MUL`（于是 `WITH_PCPI=1`）但 `CATCH_ILLINSN=0`，那么派发通路会启用、超时计数器却**不工作**（见 [picorv32.v:1423](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1423) 的 `if (WITH_PCPI && CATCH_ILLINSN)`）。此时若来一条既非 M 又非法的指令，没有任何协处理器应答，核心会**无限期停在 PCPI 派发**——直到复位。所以实践中开了 PCPI 就应保留 `CATCH_ILLINSN=1`。

#### 4.3.3 源码精读

**(1) 超时计数器声明** —— 4 位宽，决定窗口长度：

[picorv32.v:1215-1216](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1215-L1216) 声明 `reg [3:0] pcpi_timeout_counter;` 与 `reg pcpi_timeout;`。4 位 → 重装值 `~0` = `4'b1111` = 15 → 倒数 16 拍触发。

**(2) 倒计时逻辑** —— 这是本模块最精巧的一段：

[picorv32.v:1423-1430](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1423-L1430) 的读法：`if (resetn && pcpi_valid && !pcpi_int_wait)` 时才递减；`else`（即未复位、或没在派发、或有人在 wait）时重装为 `~0`。`pcpi_timeout <= !pcpi_timeout_counter;`——计数器归零的那一拍，`pcpi_timeout` 拉高。这段同时解释了 `pcpi_wait` 的「续命」作用：协处理器一拉 `pcpi_wait`，`pcpi_int_wait` 即为 1，计数器立刻被重装回 15，等于「时间清零」，于是多周期协处理器可以慢慢算而不触发超时。

**(3) 复位初值** —— 上电时 `pcpi_valid` 必须为 0，否则会误派发：

[picorv32.v:1469-1470](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1469-L1470) 在复位块里 `pcpi_valid <= 0; pcpi_timeout <= 0;`，确保复位释放前不会向协处理器发无效事务。

**(4) 派发分支里的超时出口** —— 在 `ld_rs1`（双端口）与 `ld_rs2`（单端口）两处都各有一份：

[picorv32.v:1605-1613](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1605-L1613) 是 `ld_rs1` 的超时出口：`CATCH_ILLINSN && (pcpi_timeout || instr_ecall_ebreak)` 命中后，先 `pcpi_valid <= 0` 撤销派发，再按「能否触发 EBREAK 中断」二选一回到 `fetch`（中断）或进入 `trap`（死锁）。

[picorv32.v:1777-1785](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1777-L1785) 是 `ld_rs2` 中**结构完全相同**的另一份出口。两处并存再次体现单/双端口寄存器堆带来的代码重复。

#### 4.3.4 代码实践

**实践目标**：亲手写一个最小的 PCPI 协处理器，实现一条自定义 `popcount`（统计 32 位数中 1 的个数）指令，并把本讲三块知识（协议握手、多周期计算拉 `pcpi_wait`、16 周期超时）全部用上。

**自定义指令编码**：用 RISC-V 的 `custom-0` 空间，opcode = `0001011`，`funct3=000`，形式为 `popcount rd, rs1`（只用 rs1，忽略 rs2）。下面是**示例代码**（非项目原有文件）：

```verilog
// 示例代码：最小 PCPI 协处理器，实现自定义 popcount 指令
// 编码：custom-0(opcode=0001011), funct3=000  =>  popcount rd, rs1
module pcpi_popcount (
    input         clk, resetn,
    input         pcpi_valid,
    input  [31:0] pcpi_insn,
    input  [31:0] pcpi_rs1,
    input  [31:0] pcpi_rs2,        // popcount 不用 rs2
    output reg    pcpi_wr,
    output reg [31:0] pcpi_rd,
    output reg    pcpi_wait,
    output reg    pcpi_ready
);
    // 1) 译码：是不是“我的”指令？
    wire is_popcount = pcpi_valid &&
                       pcpi_insn[6:0]   == 7'b0001011 &&
                       pcpi_insn[14:12] == 3'b000;

    reg        busy;
    reg [31:0] shreg;    // 移位消化 rs1
    reg [5:0]  count;    // 1 的个数 (0..32)
    reg [5:0]  remain;   // 剩余待处理位数

    always @(posedge clk) begin
        // 2) 关键：命中即拉 pcpi_wait，冻结核心的超时计数器
        pcpi_wait  <= is_popcount;
        pcpi_ready <= 0;
        pcpi_wr    <= 0;

        if (!resetn) begin
            busy <= 0;
        end else if (is_popcount && !busy) begin
            // 3) 启动：载入操作数
            busy   <= 1;
            shreg  <= pcpi_rs1;
            count  <= 0;
            remain <= 32;
        end else if (busy) begin
            // 4) 每拍处理 1 位：累加最低位，右移消化
            count <= count + shreg[0];
            shreg <= shreg >> 1;
            if (remain == 1) begin
                // 处理最后一位，收尾
                busy       <= 0;
                pcpi_ready <= 1;                 // “成交”
                pcpi_wr    <= 1;                 // 要写回 rd
                pcpi_rd    <= count + shreg[0];  // 旧计数 + 最后一位
            end else begin
                remain <= remain - 1;
            end
        end
    end
endmodule
```

**操作步骤**：

1. 把上面这个模块放进你的工程（或临时塞进一个 testbench 文件末尾）。
2. 实例化 `picorv32`，参数设 `ENABLE_PCPI(1)`（这样 `pcpi_*` 才引到外部），把 `pcpi_valid/insn/rs1/rs2` 接到本模块，`pcpi_wr/rd/wait/ready` 接回来。可参照 [scripts/smtbmc/mulcmp.v:29-41](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/mulcmp.v#L29-L41) 的连线风格。
3. 写一段固件，用 `.word` 手工放出一条 `popcount` 指令（例如对 `0xFF00FF00` 做 popcount，期望结果 `0x10` 即 16）。

**需要观察的现象**：

- 因为 `popcount` 要 32 拍才完成（>16），若**删掉** `pcpi_wait <= is_popcount;` 这一行，核心会在第 16 拍超时，把这条指令判为非法、陷入 `trap`（或 EBREAK 中断）——这正是 16 周期超时机制在起作用。
- 保留 `pcpi_wait` 时，计数器被持续冻结，核心安静等到 `pcpi_ready`，最终 `rd` 收到正确的 popcount 值。

**预期结果**：你能分别演示「有 `pcpi_wait` → 正确返回 16」「无 `pcpi_wait` → 超时陷入」两种结果，从而亲眼验证超时机制。

**待本地验证**：上面的 32 拍逐位实现是最直观的教学版；若你把它改成组合逻辑一次性算出 popcount（1–2 拍），就可以不拉 `pcpi_wait` 而不超时——可自行对比。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `pcpi_timeout_counter` 从 4 位改成 6 位，超时窗口会变成多少拍？这对系统有什么影响？

**答案**：`~0` 对 6 位是 63，倒数到 0 共 **64 拍**。窗口变长意味着「允许更慢的协处理器不被打断」，给设计留更多余量；代价是遇到真正非法指令时，CPU 要等更久（最多 64 拍）才能陷入异常，异常响应变慢。这是「协处理器友好度」与「非法指令响应速度」之间的权衡。注意修改后需自行核对 [picorv32.v:1423-1430](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1423-L1430) 的逻辑仍成立。

**练习 2**：一条既不是 M 扩展、也不是任何协处理器认领的非法指令，在 `ENABLE_PCPI=1` 且 `CATCH_ILLINSN=1` 时，最终会停在哪个 `cpu_state`（假设 `ENABLE_IRQ=0`）？

**答案**：停在 `cpu_state_trap`。流程是：`instr_trap=1` → `ld_rs1` 进入 PCPI 派发 → 拉高 `pcpi_valid` → 16 拍内无人 wait 也无人 ready → `pcpi_timeout=1` → 命中 [picorv32.v:1605-1613](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1605-L1613) 的 `CATCH_ILLINSN && pcpi_timeout` → 因 `ENABLE_IRQ=0` 不走中断分支 → `cpu_state <= cpu_state_trap`，并最终拉高 `trap` 端口，直至复位。

---

## 5. 综合实践

把本讲三块内容串成一个完整任务：**给 PicoRV32 加一条「位反转（bit-reverse）」自定义指令，并完整验证 PCPI 链路**。

1. **设计指令**：用 `custom-0`（opcode `0001011`），`funct3=001`，形式 `bitrev rd, rs1`，把 rs1 的 32 位按位反转（bit0↔bit31、bit1↔bit30……）写入 rd。
2. **写协处理器**：参照 4.3.4 的 `pcpi_popcount` 骨架写一个 `pcpi_bitrev` 模块。
   - 挑战 A：用「逐位串行」实现（每拍搬一位，需 32 拍），**必须**正确拉 `pcpi_wait`。
   - 挑战 B：用「组合逻辑生成器」实现（`for` 循环反转，1 拍完成），思考此时还要不要拉 `pcpi_wait`，并解释为什么。
3. **接线**：设 `ENABLE_PCPI(1)` 实例化核心，按 [scripts/smtbmc/mulcmp.v:29-41](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/mulcmp.v#L29-L41) 的风格把 7 根 PCPI 信号接好。
4. **验证超时机制**：故意把 `funct3` 译码写错（让模块不认领这条指令），观察核心是否在 16 拍后陷入 `trap`——以此证明 4.3 的超时兜底确实在工作。
5. **对照**：再开一组 `ENABLE_MUL(1)` 实例化内置乘法器，确认你的自定义指令与内置 `mul` 能**共存**（它们共用 `pcpi_valid/insn/rs1/rs2`，靠各自的 funct 译码区分，靠 [picorv32.v:325-346](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L325-L346) 的汇流与优先级 `case` 仲裁）。

完成本任务后，你不仅理解了 PCPI 协议，还具备了自己扩展 PicoRV32 指令集的实操能力。

## 6. 本讲小结

- PCPI 是 PicoRV32 把「未实现指令」外包给协处理器的统一机制：核心输出 `pcpi_valid/insn/rs1/rs2`，协处理器回 `pcpi_wait/ready/wr/rd`；分工是「核心读寄存器、协处理器出结果」。
- M 扩展（`mul/div/rem` 全家）**不是**核心原生译码的，而是命中 `instr_trap` 后整体送 PCPI；它由同文件内的三个内置协处理器 `picorv32_pcpi_mul`（进位保留多周期）、`picorv32_pcpi_fast_mul`（单周期硬乘法器）、`picorv32_pcpi_div`（恢复除法）实现。
- `ENABLE_MUL/FAST_MUL/DIV` 开**内部**核，`ENABLE_PCPI` 开**外部**端口，二者独立；任一开启都会让 `WITH_PCPI=1` 从而激活派发通路。
- 多个协处理器的 `wait`/`ready` 先按位或汇流，结果按「外部 > mul > div」优先级 `case` 选取。
- `pcpi_wait` 是「续命线」：协处理器拉高它，核心的 4 位 `pcpi_timeout_counter` 就被重装回 15、冻结计时；这对所有多周期协处理器必不可少。
- 16 周期窗口耗尽（`pcpi_timeout=1`）或遇到 `ebreak` 时，核心撤销 `pcpi_valid`，按是否开启 EBREAK 中断选择「可恢复中断」或「不可恢复 trap」；注意超时检查被 `CATCH_ILLINSN` 门控，开了 PCPI 就应保留它以免死锁。

## 7. 下一步学习建议

- **AXI/Wishbone 适配（u7-l1）**：PCPI 是「指令扩展」的外包机制；而 AXI4-Lite/Wishbone 适配器则是「**内存总线**」的外包机制。两者设计哲学相似（核心出一组简单信号，适配器翻译成标准协议），对照阅读 `picorv32_axi_adapter` 会很有启发。
- **IRQ 与自定义中断指令（u6-l2）**：本讲多次提到 `irq_ebreak` 与 EBREAK 中断。下一讲会完整讲解 PicoRV32 的中断系统与 `getq/setq/retirq/maskirq/waitirq/timer` 六条自定义指令——你会发现它们和 PCPI 一样，都是「用自定义指令扩展核心能力」的范例。
- **形式化验证（u8-l3）**：本讲引用的 [scripts/smtbmc/mulcmp.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/mulcmp.v) 正是 `make check` 用的检查器之一。学到 u8-l3 后，你会理解 `RISCV_FORMAL_ALTOPS` 宏（[picorv32.v:2404-2409](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2404-L2409)、[picorv32.v:2484-2490](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2484-L2490)）为什么要把乘除法换成简化的异或运算——那是为了让 SMT 求解器能在合理时间内完成等价性证明。
- **动手方向**：把你写的 `pcpi_popcount`/`pcpi_bitrev` 接进 PicoSoC（u8-l1），做成一条真正可被 C 代码调用的自定义指令，体验「从指令集扩展到 SoC 集成」的完整闭环。
