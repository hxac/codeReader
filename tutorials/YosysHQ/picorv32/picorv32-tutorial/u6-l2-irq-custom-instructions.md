# IRQ 与自定义中断指令

## 1. 本讲目标

PicoRV32 有一套**完全自创**的中断机制——它故意不遵守 RISC-V 特权架构规范（没有 CSR、没有 `mret`、没有异常等级），而是用 6 条自定义指令 + 4 个 q 寄存器 + 一个内置中断控制器，以最小硬件代价实现了「响应外部事件、定时、处理非法指令与总线错误」的全部能力。

学完本讲，你应当能够：

1. 说出 `getq` / `setq` / `retirq` / `maskirq` / `waitirq` / `timer` 六条指令的编码字段与各自语义。
2. 解释 q0..q3 四个寄存器在硬件上**究竟是怎么实现的**（关键：它们并不是独立的触发器），以及 q0/q1 在中断进入时被自动写入、q2/q3 留作软件暂存的约定。
3. 描述一次中断从「触发 → 进入 `irq_state` 状态机 → 自动保存返回地址与中断号 → 跳到 `PROGADDR_IRQ` → 软件处理 → `retirq` 返回」的完整闭环，并理解 `eoi` 信号在其中的作用。
4. 读懂 `firmware/start.S` 的 `irq_vec` 上下文保存/恢复封装与 `firmware/irq.c` 的 C 处理函数。

本讲是专家层（advanced），承接 [u4-1 指令译码器](u4-l1-instruction-decoder.md) 的译码一位信号与 [u4-2 主状态机](u4-l2-main-fsm.md) 的 `cpu_state_fetch` 入口。

## 2. 前置知识

### 2.1 什么是中断

CPU 顺序执行指令时，有时需要「暂停当前流程，去处理一件更紧急的事，处理完再回来」。这件事可能是：

- 一定时间到了（定时器）；
- 外部设备发来信号（按键、网卡收到数据）；
- 程序自己跑飞了（执行了非法指令、访问了不对齐的地址）。

这套「暂停 → 跳到处理函数 → 返回」的机制就是**中断（Interrupt）**。处理中断的那个函数叫**中断处理程序（Interrupt Handler / ISR）**。

### 2.2 标准做法 vs PicoRV32 的做法

RISC-V 标准特权架构用一套**CSR（Control & Status Register）**和 `mret` 等指令来做中断/异常，功能强大但硬件复杂（要维护特权级、CSR 读写、cause 寄存器等）。

PicoRV32 的定位是「尺寸优先的辅助核」，所以作者**弃用标准做法**，README 明确写道：

> *Note: The IRQ handling features in PicoRV32 do not follow the RISC-V Privileged ISA specification. Instead a small set of very simple custom instructions is used to implement IRQ handling with minimal hardware overhead.*

翻译过来就是：PicoRV32 用一组极简的自定义指令实现中断，硬件开销极小。这是典型的「为了面积而违反标准」的工程取舍——只要固件配合，这套自定义机制完全够用。

### 2.3 自定义指令的编码空间

PicoRV32 把 6 条中断指令全部塞进 RISC-V 留作扩展的 `custom0` 主操作码（`opcode = 0b0001011 = 0x0B`），再用 7 位 `funct7` 字段（指令的 bits[31:25]）区分这 6 条指令。`funct3`（bits[14:12]）和 `rs2`（bits[24:20]）字段在这组指令里**被译码器忽略**，仅用于汇编宏的可读性。这一点在 [README.md:523-524](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L523-L524) 有明确说明。

### 2.4 关键术语速查

| 术语 | 含义 |
|------|------|
| `irq`（输入） | 32 位中断请求输入，每一位对应一个外部中断源 |
| `irq_pending` | 32 位「已发生但尚未处理」的中断挂起位 |
| `irq_mask` | 32 位中断屏蔽位，某位为 1 则该中断被禁用 |
| `irq_active` | 1 表示「正在处理中断」，此时不再响应新中断 |
| `eoi`（输出） | End Of Interrupt，告知外部设备当前正在被服务的中断号 |
| `q0..q3` | 4 个中断专用 32 位寄存器，用于保存返回地址、中断号和暂存 |
| `irq_state` | 进入中断时用到的 2 位内部状态机 |
| `PROGADDR_IRQ` | 中断处理程序入口地址，默认 `0x00000010` |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `picorv32.v` | 中断控制器、6 条指令的译码与执行、`irq_state` 进入序列、`timer`/`eoi`/`irq_mask`/`irq_pending` 寄存器全部在此 |
| `firmware/custom_ops.S` | 用 GNU 汇编宏 `.word` 把 6 条指令的位编码包装成可读的助记符（如 `picorv32_getq_insn`） |
| `firmware/start.S` | `reset_vec` 与 `irq_vec`：中断入口的寄存器保存/恢复封装，是「软件中断向量」的范例 |
| `firmware/irq.c` | C 语言中断处理函数 `irq()`，根据中断号分发处理 |
| `firmware/firmware.h` | 声明 `irq()` 的函数原型 |
| `README.md` | 「Custom Instructions for IRQ Handling」一节是这套机制的权威文档 |

## 4. 核心概念与源码讲解

### 4.1 中断控制器：32 个中断源、屏蔽与挂起

#### 4.1.1 概念说明

PicoRV32 内置一个**极简的 32 路中断控制器**。32 个中断位中：

- **bit 0–2 是内置源**（CPU 自己产生的中断），含义固定：

| IRQ | 中断源 |
|----:|--------|
| 0 | 定时器（Timer） |
| 1 | EBREAK/ECALL 或非法指令 |
| 2 | 总线错误（不对齐的内存访问） |

- **bit 3–31 是外部源**，由模块的 `irq[31:0]` 输入端口驱动，可接外设。

这套机制受 `ENABLE_IRQ` 参数总开关控制（默认关）。三个相关开关：

- `ENABLE_IRQ`（默认 0）：总开关，关掉则整套中断机制被综合掉。
- `ENABLE_IRQ_QREGS`（默认 1）：是否实现 q 寄存器与 `getq`/`setq`。
- `ENABLE_IRQ_TIMER`（默认 1）：是否实现 `timer` 指令与定时器中断。

见 [picorv32.v:79-81](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L79-L81)。

#### 4.1.2 核心流程

中断从「产生」到「被注意到」经过三个 32 位寄存器：

```
中断源 ──► irq_pending（挂起：发生了，还没处理）
              │
              ├──► & ~irq_mask（去掉被屏蔽的）
              │
              ▼
        若有任意一位为 1 且 !irq_active ──► 触发中断进入序列
```

三个寄存器的复位初值与行为：

- `irq_pending`：记录「已发生但尚未处理」的中断。复位为 0。
- `irq_mask`：屏蔽位，某位为 1 表示该中断被禁用。**复位为 `~0`（全 1），即复位后所有中断都被屏蔽**，必须由固件用 `maskirq` 主动打开。
- `irq_active`：1 表示「正在处理中断」，此时**不再响应新的可屏蔽中断**（防止中断嵌套把硬件搞乱）。

此外还有两个常量参数影响中断行为：

- `MASKED_IRQ`（默认 0）：某位写 1 表示该中断被**永久禁用**——即便固件用 `maskirq` 想打开它，硬件也会用 `| MASKED_IRQ` 把它强制置回屏蔽。见 [picorv32.v:84](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L84)。
- `LATCHED_IRQ`（默认 `0xffff_ffff`）：某位为 1 表示该中断是**电平锁存**的——即使 `irq` 输入只拉高一拍，挂起位也会被记住直到处理；为 0 则是电平型，输入拉低后挂起也消失。见 [picorv32.v:85](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L85)。

#### 4.1.3 源码精读

中断相关寄存器声明在 [picorv32.v:196-200](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L196-L200)：

```verilog
reg irq_delay;
reg irq_active;
reg [31:0] irq_mask;
reg [31:0] irq_pending;
reg [31:0] timer;
```

复位块一次性初始化中断状态，[picorv32.v:1471-1477](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1471-L1477)：

```verilog
irq_active <= 0;
irq_delay <= 0;
irq_mask <= ~0;        // 复位后屏蔽所有中断
next_irq_pending = 0;
irq_state <= 0;
eoi <= 0;
timer <= 0;
```

每个时钟上升沿，外部 `irq` 输入被合并进挂起位，定时器也在递减，[picorv32.v:1915-1920](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1915-L1920)：

```verilog
if (ENABLE_IRQ) begin
    next_irq_pending = next_irq_pending | irq;          // 外部中断源并 入
    if(ENABLE_IRQ_TIMER && timer)
        if (timer - 1 == 0)
            next_irq_pending[irq_timer] = 1;            // 定时器归零触发 bit0
end
```

> 注意 `irq_timer` 是值为 0 的 localparam（[picorv32.v:161-163](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L161-L163)），所以 `next_irq_pending[irq_timer]` 就是 `next_irq_pending[0]`。这种用 localparam 命名中断位号的写法让代码可读性更好。

最后，挂起位在每拍末尾更新，并强制剔除永久屏蔽位，[picorv32.v:1963](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1963)：

```verilog
irq_pending <= next_irq_pending & ~MASKED_IRQ;
```

非法指令/EBREAK 触发 bit1、总线错误触发 bit2 的逻辑分散在 [picorv32.v:1605-1623](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1605-L1623) 与 [picorv32.v:1922-1944](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1922-L1944)，套路一致：只要 `!irq_mask[irq_ebreak]`（该中断未被屏蔽）且 `!irq_active`（当前没在处理中断），就把对应挂起位置 1，否则直接进 `cpu_state_trap` 死锁。

#### 4.1.4 代码实践

**目标**：理解 `MASKED_IRQ` 参数如何「永久禁用」某个中断。

**步骤**：

1. 阅读 [README.md:299-306](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L299-L306)，确认 `MASKED_IRQ` 的语义。
2. 在 `picorv32.v` 中找到 `maskirq` 指令的执行（[picorv32.v:1678-1686](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1678-L1686)），注意 `irq_mask <= cpuregs_rs1 | MASKED_IRQ;` 这一行——它把固件写入的屏蔽字与 `MASKED_IRQ` 相或。
3. 设想参数设为 `MASKED_IRQ = 32'h0000_0004`（永久禁用 bit2 总线错误中断），即便固件执行 `maskirq x1, x0`（想清零屏蔽、打开全部中断），bit2 也会被 `| MASKED_IRQ` 强制拉回 1。

**观察现象**：固件永远无法打开 bit2，总线错误一旦发生且 `CATCH_MISALIGN` 开启，会走 `cpu_state_trap` 死锁而非中断。这是「硬件级熔断」。

**预期结果**：能用自己的话说出 `MASKED_IRQ`（编译期永久屏蔽）与 `irq_mask`（运行时可改屏蔽）的区别。**待本地验证**：若想实测，可在实例化 `picorv32` 时传 `MASKED_IRQ=32'h4` 重新综合并跑 `make test`，观察总线错误测试是否触发 trap。

#### 4.1.5 小练习与答案

**练习 1**：复位后 CPU 会立刻响应外部中断吗？为什么？
**答案**：不会。复位时 `irq_mask <= ~0`（[picorv32.v:1473](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1473)），所有中断都被屏蔽，必须由固件用 `maskirq` 主动打开。

**练习 2**：定时器从被写入值 N 到触发中断，大约经过多少个时钟周期？
**答案**：约 N 个周期。`timer` 每拍减 1（[picorv32.v:1442-1444](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1442-L1444)），当检测到 `timer - 1 == 0`（即 timer 当前值为 1，下一拍将变 0）时把 `irq_pending[0]` 置 1（[picorv32.v:1917-1919](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1917-L1919)）。

### 4.2 六条自定义指令的编码与语义

#### 4.2.1 概念说明

PicoRV32 在 `custom0` 操作码下定义了 6 条自定义指令，全部在 [README.md:480-611](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L480-L611) 文档化。它们的功能可以用一句话概括：

| 指令 | 功能 | 简单记忆 |
|------|------|---------|
| `getq rd, qs` | 把 q 寄存器 `qs` 的值读到通用寄存器 `rd` | q → x |
| `setq qd, rs` | 把通用寄存器 `rs` 的值写到 q 寄存器 `qd` | x → q |
| `retirq` | 从中断返回：`q0 → PC`，重新开中断 | 中断返回 |
| `maskirq rd, rs` | 读出旧 `irq_mask` 到 `rd`，写入新值 `rs` | 改中断屏蔽 |
| `waitirq rd` | 暂停 CPU 直到有中断挂起，挂起位写入 `rd` | 等中断（节能停机） |
| `timer rd, rs` | 读出旧 `timer` 到 `rd`，写入新初值 `rs` | 设定时器 |

#### 4.2.2 核心流程

6 条指令都采用 R 型编码，字段布局为：

```
[31:25 funct7][24:20 rs2][19:15 rs1/qs][14:12 funct3][11:7 rd/qd][6:0 opcode]
```

区分依据**只有 opcode（=0x0B）和 funct7**，funct3 与 rs2 被译码器忽略。下表给出每条指令的 funct7 与有效字段：

| 指令 | funct7 | 有效字段 | 译码位置 |
|------|:------:|----------|----------|
| `getq rd, qs` | `0000000` | rd（[11:7]）、qs（[19:15]，仅低 2 位有效） | 第二级 [picorv32.v:1090](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1090) |
| `setq qd, rs` | `0000001` | qd（[11:7]，低 2 位）、rs（[19:15]） | 第二级 [picorv32.v:1091](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1091) |
| `retirq` | `0000010` | 无（固定读 q0） | 第一级 [picorv32.v:871](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L871) |
| `maskirq rd, rs` | `0000011` | rd（[11:7]）、rs（[19:15]） | 第二级 [picorv32.v:1092](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1092) |
| `waitirq rd` | `0000100` | rd（[11:7]） | 第一级 [picorv32.v:872](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L872) |
| `timer rd, rs` | `0000101` | rd（[11:7]）、rs（[19:15]） | 第二级 [picorv32.v:1093](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1093) |

> **为什么 `retirq`/`waitirq` 在第一级译码？** 因为这两条指令需要**改写 `decoded_rs1`**（分别指向 q0 与「无源操作数」），这种改写必须在取指完成那一拍（用 `mem_rdata_latched`）就做，所以放在第一级；其余 4 条只产生 `instr_*` 一位信号，放第二级即可。这正对应 [u4-1 讲义](u4-l1-instruction-decoder.md) 讲过的「两级译码」。

汇编层面，GNU 汇编器并不认识这些助记符，所以 [firmware/custom_ops.S:82-101](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/custom_ops.S#L82-L101) 用 `.word` 直接拼出位编码，再用宏包装成可读名字：

```c
#define r_type_insn(_f7, _rs2, _rs1, _f3, _rd, _opc) \
.word (((_f7) << 25) | ((_rs2) << 20) | ((_rs1) << 15) | ((_f3) << 12) | ((_rd) << 7) | ((_opc) << 0))

#define picorv32_getq_insn(_rd, _qs) \
r_type_insn(0b0000000, 0, regnum_ ## _qs, 0b100, regnum_ ## _rd, 0b0001011)
```

于是固件里可以直接写 `picorv32_getq_insn(x2, q0)`，预处理器展开成一条 `.word`。

#### 4.2.3 源码精读

6 条指令的**执行体**全部集中在 `cpu_state_ld_rs1` 的 case 里（因为它们都只读一个源寄存器 `rs1`，不需要 `ld_rs2`），[picorv32.v:1650-1695](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1650-L1695)。逐条看：

```verilog
ENABLE_IRQ && ENABLE_IRQ_QREGS && instr_getq: begin
    reg_out <= cpuregs_rs1;          // 把读出的 q[qs] 送到写回通路
    latched_store <= 1;              // 标记「本条要写回 rd」
    cpu_state <= cpu_state_fetch;    // 直接回 fetch 完成写回
end
```

`getq` 把 `cpuregs_rs1`（此时已是 q[qs] 的值，原理见 4.3 节）送到 `reg_out`，置 `latched_store` 后回 `fetch` 状态完成对 `rd` 的写回。

```verilog
ENABLE_IRQ && ENABLE_IRQ_QREGS && instr_setq: begin
    reg_out <= cpuregs_rs1;                 // rs 的值
    latched_rd <= latched_rd | irqregs_offset; // 把目标改为 q[qd] 的物理索引
    latched_store <= 1;
    cpu_state <= cpu_state_fetch;
end
```

`setq` 的关键在于 `latched_rd | irqregs_offset`：把指令里的 `qd`（0..3）映射到 q 寄存器的物理索引（见 4.3 节），从而把 `rs` 写进 q[qd]。

```verilog
ENABLE_IRQ && instr_retirq: begin
    eoi <= 0;                        // 撤销 EOI 信号
    irq_active <= 0;                 // 重新允许响应中断
    latched_branch <= 1;             // 标记「这是一次跳转」
    latched_store <= 1;
    reg_out <= CATCH_MISALIGN ? (cpuregs_rs1 & 32'h fffffffe) : cpuregs_rs1; // q0 → 新 PC
    cpu_state <= cpu_state_fetch;
end
```

`retirq` 读出 q0（返回地址），清零 `eoi` 与 `irq_active`，然后像普通跳转一样把 `reg_out` 作为新 PC。注意 `& ~1` 是为了把可能的压缩指令 LSB 清掉，保证 PC 对齐。

```verilog
ENABLE_IRQ && instr_maskirq: begin
    latched_store <= 1;
    reg_out <= irq_mask;                 // 旧 mask 返回给 rd
    irq_mask <= cpuregs_rs1 | MASKED_IRQ; // 写新 mask，并强制叠加永久屏蔽
    cpu_state <= cpu_state_fetch;
end
```

`maskirq` 是**读改写**：先把旧 `irq_mask` 读到 `reg_out`（供 `rd` 写回），再写入新值——但**永远与 `MASKED_IRQ` 相或**，保证永久屏蔽位不被打开。

```verilog
ENABLE_IRQ && ENABLE_IRQ_TIMER && instr_timer: begin
    latched_store <= 1;
    reg_out <= timer;          // 旧 timer 值返回给 rd
    timer <= cpuregs_rs1;      // 写入新初值
    cpu_state <= cpu_state_fetch;
end
```

`timer` 同样是读改写：返回旧值，写入新值。注意写 0 会关闭定时器（因为 [picorv32.v:1442](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1442) 的递增条件是 `timer` 非零）。

`waitirq` 比较特殊——它的「执行」不在 `ld_rs1`，而在 `fetch` 状态直接处理，见 [picorv32.v:1548-1555](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1548-L1555)：

```verilog
if (ENABLE_IRQ && (decoder_trigger || do_waitirq) && instr_waitirq) begin
    if (irq_pending) begin
        latched_store <= 1;
        reg_out <= irq_pending;                 // 把挂起位写入 rd
        reg_next_pc <= current_pc + (compressed_instr ? 2 : 4); // 前进到下一条
        mem_do_rinst <= 1;
    end else
        do_waitirq <= 1;                        // 否则原地阻塞
end
```

`do_waitirq` 会让 `mem_do_rinst <= !decoder_trigger && !do_waitirq`（[picorv32.v:1492](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1492)）保持为 0，于是 CPU 不取下一条指令，**原地空转**直到 `irq_pending` 非零。这是一种「让 CPU 停下等待事件」的低开销机制。

#### 4.2.4 代码实践

**目标**：手工编码一条 `maskirq` 指令，验证对编码字段的理解。

**步骤**：

1. 查表得知 `maskirq x1, x2` 的 funct7=`0000011`、opcode=`0001011`，`rs` 字段（bits[19:15]）= x2 = `00010`，`rd` 字段（bits[11:7]）= x1 = `00001`。
2. 按 [firmware/custom_ops.S:82-83](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/custom_ops.S#L82-L83) 的 `r_type_insn` 公式拼装：

\[ \text{word} = (\text{f7} \ll 25) \,|\, (\text{rs2} \ll 20) \,|\, (\text{rs1} \ll 15) \,|\, (\text{f3} \ll 12) \,|\, (\text{rd} \ll 7) \,|\, \text{opc} \]

3. 代入：`(0b0000011 << 25) | (0b00001 << 15) | (0b110 << 12) | (0b00001 << 7) | 0b0001011`（rs2=0、rs2 字段忽略，填 0）。
4. 换算成十六进制：得到 `0x0601_5A2B`（ funct7=0x03 → bits31-25；可分段核算：`0x06000000 | 0x00010000 | 0x00003000 | 0x00000080 | 0x0000000B`）。

**预期结果**：你拼出的 32 位字应与汇编宏 `picorv32_maskirq_insn(x1, x2)` 展开的 `.word` 完全一致。**待本地验证**：可在 `firmware/` 下写一个仅含该宏的小 `.S`，用 `riscv32i-unknown-elf-gcc -c` 汇编后 `objdump -d` 看反汇编出的机器码与自己手算的对比。

> 提示：`funct3` 字段填什么不影响功能（译码器忽略它），所以即使你把 `maskirq` 的 funct3 算错，CPU 仍能正确执行——这正是 README 所说「f3 和 rs2 被忽略」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `retirq` 没有操作数，却能恢复正确的返回地址？
**答案**：译码器在 [picorv32.v:889-890](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L889-L890) 把 `decoded_rs1` 强制改为 `irqregs_offset`（即 q0 的物理索引），所以 `retirq` 执行时 `cpuregs_rs1` 读出的就是 q0（中断进入时自动保存的返回地址）。

**练习 2**：`waitirq` 等到的事件被屏蔽（在 `irq_mask` 中为 1）时，还能唤醒 CPU 吗？
**答案**：能。`waitirq` 判断的是 `irq_pending`（[picorv32.v:1549](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1549)），**不是** `irq_pending & ~irq_mask`。被屏蔽的中断依然会让 CPU 醒来并把它写入 `rd`，只是不会触发真正的中断进入序列。

### 4.3 q 寄存器：物理映射与软件约定

#### 4.3.1 概念说明

PicoRV32 宣称有「4 个额外的 32 位寄存器 q0..q3」，但**它们在硬件上并不是独立的触发器组**——这是一个精妙的实现技巧。作者把 q0..q3 直接**嫁接在通用寄存器堆 `cpuregs` 数组的末尾**，复用同一套读写端口，从而几乎零成本地「多出」4 个寄存器。

软件约定（来自 [README.md:510-521](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L510-L521)）：

| 寄存器 | 中断进入时的内容 | 用途 |
|--------|------------------|------|
| q0 | 返回地址（LSB=被中断指令是否压缩） | `retirq` 用它恢复 PC |
| q1 | 本次处理的中断号位掩码 | 告诉处理函数「是哪些中断触发的」 |
| q2 | 未初始化 | 软件暂存（如保存 ra） |
| q3 | 未初始化 | 软件暂存（如保存 sp） |

#### 4.3.2 核心流程

q 寄存器的「物理嫁接」由三个 localparam 决定，[picorv32.v:165-167](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L165-L167)：

```verilog
localparam integer irqregs_offset = ENABLE_REGS_16_31 ? 32 : 16;
localparam integer regfile_size = (ENABLE_REGS_16_31 ? 32 : 16) + 4*ENABLE_IRQ*ENABLE_IRQ_QREGS;
localparam integer regindex_bits = (ENABLE_REGS_16_31 ? 5 : 4) + ENABLE_IRQ*ENABLE_IRQ_QREGS;
```

含义：

- 当 `ENABLE_IRQ && ENABLE_IRQ_QREGS` 时，`regfile_size` 在原来 16 或 32 个通用寄存器基础上**再加 4 项**——这 4 项就是 q0..q3。
- `irqregs_offset` 是这 4 项的起始下标（启用 32 个通用寄存器时是 32，否则是 16）。
- `regindex_bits` 多了 1 位（`+ENABLE_IRQ*ENABLE_IRQ_QREGS`），让索引能寻到末尾的 4 项。

于是 `cpuregs[irqregs_offset + 0..3]` 物理上就是 q0..q3。访问映射靠译码期的两次「改写」：

- **`getq` 读**：把 `decoded_rs1` 的最高位强制置 1（[picorv32.v:886-887](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L886-L887)），使 `qs`（0..3）被读成 `irqregs_offset + qs`。
- **`setq` 写**：把 `latched_rd |= irqregs_offset`（[picorv32.v:1663](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1663)），使 `qd`（0..3）被写到 `irqregs_offset + qd`。

当 `ENABLE_IRQ_QREGS=0` 时，q 寄存器不存在，硬件改用通用寄存器 x3（gp）存返回地址、x4（tp）存中断号——见 [picorv32.v:889-890](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L889-L890) 与 [picorv32.v:1545-1546](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1545-L1546) 的三元判断。这是「省 4 个寄存器但占用 gp/tp」的取舍。

#### 4.3.3 源码精读

q 寄存器号在汇编里的命名见 [firmware/custom_ops.S:8-11](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/custom_ops.S#L8-L11)：

```c
#define regnum_q0   0
#define regnum_q1   1
#define regnum_q2   2
#define regnum_q3   3
```

注意 q0..q3 的「号」就是 0..3，它们被填进指令的 rs1/rd 字段，由译码器再映射到 `irqregs_offset + 号`。

`irqregs_offset` 这个名字本身就把意图说得很清楚：q 寄存器是寄存器堆里从 `irqregs_offset` 开始的一段偏移区。把 [picorv32.v:166](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L166) 的 `regfile_size` 与 [u5-l1 讲义](u5-l1-regfile-and-alu.md) 讲过的 `cpuregs` 数组对照，就能确认：q0..q3 并没有自己的存储，它们就是 `cpuregs` 数组最后那 4 项。

#### 4.3.4 代码实践

**目标**：通过改参数观察 q 寄存器的「有/无」对固件的影响。

**步骤**：

1. 阅读 [firmware/start.S:55-70](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L55-L70)（`#ifdef ENABLE_QREGS` 分支）：它用 `setq q2, x1`、`getq x2, q0` 等指令保存上下文。
2. 再看 [firmware/start.S:120-176](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L120-L176)（`#else` 分支，`ENABLE_QREGS` 未定义）：它改用 `sw gp, 0*4+0x200(zero)`、`sw x1, ...` 等把返回地址存到固定内存地址 `0x200`，且用 `gp`/`tp` 充当中断号载体。
3. 注意 [firmware/start.S:15-17](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L15-L17)：若不启用 q 寄存器，`ENABLE_RVTST`（跑 riscv-tests）会被强制取消——因为那套测试依赖 gp/tp，不能被中断机制占用。

**观察现象**：关闭 q 寄存器后，固件不得不多访问好几次内存（每个寄存器都要 `sw`/`lw` 到 `0x200` 区），中断延迟变大；而开 q 寄存器时，关键上下文（返回地址、ra、sp）通过 `getq`/`setq` 在硬件里直接搬运，快得多。

**预期结果**：能解释「为什么默认 `ENABLE_IRQ_QREGS=1`」——q 寄存器让中断进/出的关键路径几乎不访存，显著降低中断延迟。

#### 4.3.5 小练习与答案

**练习 1**：`irqregs_offset` 在 `ENABLE_REGS_16_31=1`（启用 x16..x31）和 `=0`（仅 x0..x15）时分别取何值？为什么？
**答案**：分别是 32 和 16。因为 q 寄存器总是挂在通用寄存器堆的**末尾**，启用 32 个通用寄存器时末尾下标是 32，仅启用 16 个时是 16。

**练习 2**：q2、q3 与 q0、q1 有何本质区别？
**答案**：q0/q1 在**中断进入时由硬件自动写入**（返回地址与中断号），是「只读」性质的中断上下文；q2/q3 **不初始化**，纯粹留给中断处理函数做软件暂存（[README.md:520-521](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L520-L521)），典型用法见 [start.S:57-58](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L57-L58) 保存 ra/sp。

### 4.4 中断的进入与返回：irq_state 状态机

#### 4.4.1 概念说明

中断「进入」不是一拍完成的——它需要：判定有未屏蔽的挂起、保存返回地址到 q0、保存中断号到 q1、把 PC 指向 `PROGADDR_IRQ`、拉高 `eoi`。这些事由一个 2 位内部状态机 `irq_state` 在 `cpu_state_fetch` 内分两三拍完成。「返回」则由 `retirq` 一条指令搞定。

理解入口判定的关键信号是 `launch_next_insn`，[picorv32.v:1400](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1400)：

```verilog
assign launch_next_insn = cpu_state == cpu_state_fetch && decoder_trigger &&
    (!ENABLE_IRQ || irq_delay || irq_active || !(irq_pending & ~irq_mask));
```

意思是：**只有当没有未屏蔽的挂起中断时，才允许启动下一条普通指令**。反过来说，一旦 `irq_pending & ~irq_mask` 非零且不在中断中，下一条「指令」就会被劫持成中断进入序列。

#### 4.4.2 核心流程

中断进入序列（在 `cpu_state_fetch` 内）的 `irq_state` 流转：

```
irq_state = 00  (空闲)
    │  decoder_trigger && |(irq_pending & ~irq_mask) && !irq_active && !irq_delay
    ▼
irq_state = 01  →  写 q0 = 返回地址 | 压缩标志；current_pc = PROGADDR_IRQ；irq_active <= 1
    │
    ▼
irq_state = 10  →  写 q1 = irq_pending & ~irq_mask；eoi <= 同值；清除已处理挂起位
    │
    ▼
irq_state = 00  →  从 PROGADDR_IRQ 正常取指执行处理程序
```

中断返回（`retirq` 一条指令）：

```
retirq →  eoi <= 0；irq_active <= 0；PC <= q0；重新允许中断
```

#### 4.4.3 源码精读

`irq_state` 的声明在 [picorv32.v:1182](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1182)：`reg [1:0] irq_state;`。

状态流转与目标寄存器选择在 [picorv32.v:1538-1546](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1538-L1546)：

```verilog
if (ENABLE_IRQ && ((decoder_trigger && !irq_active && !irq_delay && |(irq_pending & ~irq_mask)) || irq_state)) begin
    irq_state <=
        irq_state == 2'b00 ? 2'b01 :
        irq_state == 2'b01 ? 2'b10 : 2'b00;
    latched_compr <= latched_compr;
    if (ENABLE_IRQ_QREGS)
        latched_rd <= irqregs_offset | irq_state[0];   // 01 态写 q0，10 态写 q1
    else
        latched_rd <= irq_state[0] ? 4 : 3;            // 无 q 寄存器：写 x4 / x3
end
```

`irq_state[0]` 在 01 态为 1、10 态为 0，所以 `irqregs_offset | irq_state[0]` 在 01 态等于 q0、10 态等于 q1（因为 10 态 irq_state[0]=0，`irqregs_offset | 0 = irqregs_offset` = q0？这里要小心）。

> **仔细辨析**：在 01 态（即将变 10），`irq_state[0]` 当前值是 1，所以 `latched_rd = irqregs_offset | 1` = q1 的下标？不对——这里要看的是**进入这一态时 latched_rd 被设成什么**。状态机在 00→01 转移的那一拍，`irq_state[0]` 还是 0（旧值），所以 `latched_rd = irqregs_offset | 0 = irqregs_offset`，对应 q0；下一拍 01→10 转移时 `irq_state[0]` 是 1（旧值），`latched_rd = irqregs_offset | 1`，对应 q1。也就是说 **q0 先写、q1 后写**，与 [README.md:511-512](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L511-L512) 的约定一致。

实际的「写什么值」在 `fetch` 状态的 case 与 `cpuregs_write` 块里，[picorv32.v:1506-1514](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1506-L1514)：

```verilog
ENABLE_IRQ && irq_state[0]: begin               // q0 写回拍
    current_pc = PROGADDR_IRQ;
    irq_active <= 1;
    mem_do_rinst <= 1;
end
ENABLE_IRQ && irq_state[1]: begin               // q1 写回拍
    eoi <= irq_pending & ~irq_mask;
    next_irq_pending = next_irq_pending & irq_mask;  // 清除已处理位
end
```

以及 [picorv32.v:1324-1331](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1324-L1331) 给出写回数据：

```verilog
ENABLE_IRQ && irq_state[0]: begin
    cpuregs_wrdata = reg_next_pc | latched_compr;   // q0 = 返回地址 | 压缩标志
    cpuregs_write = 1;
end
ENABLE_IRQ && irq_state[1]: begin
    cpuregs_wrdata = irq_pending & ~irq_mask;       // q1 = 中断号位掩码
    cpuregs_write = 1;
end
```

注意 q0 的最低位是 `latched_compr`——如果被中断的是一条压缩指令，q0 的 LSB 会被置 1，软件据此判断该回退 2 字节还是 4 字节去解码原指令（[README.md:516-518](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L516-L518)）。

`eoi`（End Of Interrupt）输出端口在 [picorv32.v:120-121](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L120-L121) 声明，在 q1 写回拍被置为正在处理的中断号（[picorv32.v:1512](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1512)），在 `retirq` 时清零（[picorv32.v:1668](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1668)）。它告诉外部中断控制器「这位中断我正在处理，你可以撤销请求了」。

#### 4.4.4 代码实践

**目标**：把中断进入/返回的硬件流程与软件流程对应起来。

**步骤**：

1. 在 `picorv32.v` 里用文本搜索定位 `irq_state <=` （[picorv32.v:1539-1541](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1539-L1541)），确认三态流转 `00→01→10→00`。
2. 对照 [README.md:495-497](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L495-L497) 关于 `eoi` 的描述：「处理开始时 eoi 拉高，返回时拉低」。
3. 在纸上画出时序：`irq` 拉高 → 几拍后 `irq_pending` 置位 → `launch_next_insn` 为假 → `irq_state` 走完三态 → PC 跳到 `PROGADDR_IRQ` → …… → 软件执行 `retirq` → `irq_active` 清零 → `eoi` 清零。

**观察现象**：整个进入序列**不需要任何内存访问**就能把返回地址和中断号存好（都写进 q 寄存器），这是 PicoRV32 中断延迟极低的根本原因。

**预期结果**：能解释「为什么 `irq_active` 必须在 `retirq` 才清零」——若提前清零，处理程序执行期间会被同级新中断反复打断，而硬件不支持嵌套保存。**待本地验证**：运行 `make test` 后用 `showtrace.py` 解码 trace，能看到 `TRACE_IRQ` 标记的中断进出记录。

#### 4.4.5 小练习与答案

**练习 1**：中断进入时，返回地址被保存到 q0，那 q0 的原始内容去哪了？
**答案**：q0 是硬件「专用」于保存返回地址的，其原值被认为无意义（每次中断都会被覆盖）。真正需要跨中断保存的通用寄存器由软件在 `irq_vec` 里手动保存到内存（见 4.5 节）。

**练习 2**：如果在中断处理程序执行期间又发生了更高级的中断，会怎样？
**答案**：不会立刻响应。因为 `irq_active=1` 时，进入序列的触发条件 `!irq_active`（[picorv32.v:1538](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1538)）不满足，新中断只能等 `retirq` 清零 `irq_active` 后才被响应。PicoRV32 **不支持硬件中断嵌套**。

### 4.5 固件中断软件栈：start.S 与 irq.c

#### 4.5.1 概念说明

硬件只负责「跳到 `PROGADDR_IRQ` 并把上下文放进 q0/q1」，剩下的事——**保存所有通用寄存器、调用 C 处理函数、根据中断号分发处理、恢复寄存器、返回**——全部由固件完成。这套软件栈分三层：

1. `reset_vec`（复位入口）：用 `waitirq`+`maskirq` 完成中断初始化。
2. `irq_vec`（中断向量，位于 `PROGADDR_IRQ=0x10`）：汇编写的「保存/恢复 + 调 C」封装。
3. `irq()`（C 函数）：真正的中断处理逻辑。

#### 4.5.2 核心流程

完整的「一次中断」软件流程：

```
                  ┌── reset_vec (0x0) ──┐
                  │   waitirq            │  等待第一个中断（测试启动信号）
                  │   maskirq zero,zero  │  打开所有非永久屏蔽中断
                  │   j start            │
                  └──────────────────────┘

   中断发生 ──► PC = irq_vec (0x10)
                  │  setq q2,x1 ; setq q3,x2     先把 ra/sp 存进 q2/q3
                  │  getq x2,q0 ; sw → irq_regs[0]  返回地址存内存
                  │  getq x2,q2 ; sw → irq_regs[1]  ra 存内存
                  │  getq x2,q3 ; sw → irq_regs[2]  sp 存内存
                  │  sw x3..x31 → irq_regs[3..31]   其余寄存器存内存
                  │  sp = irq_stack ; a0 = &irq_regs
                  │  getq a1,q1                    a1 = 中断号位掩码
                  │  jal irq                       调 C 处理函数
                  │  （恢复寄存器，逆操作）
                  │  retirq                        返回
```

#### 4.5.3 源码精读

**reset_vec** 在 [firmware/start.S:41-45](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L41-L45)，注释强调「这里不能超过 16 字节」（因为 `irq_vec` 必须落在 `0x10`）：

```asm
reset_vec:
    // no more than 16 bytes here !
    picorv32_waitirq_insn(zero)   // 阻塞直到有中断挂起（测试台用它作启动信号）
    picorv32_maskirq_insn(zero, zero)  // 写 0，打开所有中断（再 |MASKED_IRQ）
    j start
```

注意 `waitirq` 判断的是 `irq_pending`（与屏蔽无关），所以即便复位后全屏蔽，外部 `irq` 拉高仍能唤醒它——测试台正是靠这个机制告诉 CPU「可以开始了」。

**irq_vec 的保存部分** 在 [firmware/start.S:52-117](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L52-L117)，核心前几行（启用 q 寄存器时）：

```asm
irq_vec:
    picorv32_setq_insn(q2, x1)     # ra 暂存到 q2
    picorv32_setq_insn(q3, x2)     # sp 暂存到 q3

    lui x1, %hi(irq_regs)
    addi x1, x1, %lo(irq_regs)     # x1 = &irq_regs

    picorv32_getq_insn(x2, q0)
    sw x2,   0*4(x1)               # irq_regs[0] = 返回地址

    picorv32_getq_insn(x2, q2)
    sw x2,   1*4(x1)               # irq_regs[1] = 原 ra

    picorv32_getq_insn(x2, q3)
    sw x2,   2*4(x1)               # irq_regs[2] = 原 sp
    # 接着 sw x3..x31 → irq_regs[3..31]
```

这里有个精妙之处：要把所有寄存器存到内存，需要用 `x1`（作基址）和 `x2`（作数据中转），可这俩寄存器本身存着活值（ra、sp）。于是先 `setq q2/q3` 把它们藏进 q 寄存器，腾出 x1/x2 当工具，存完别人的最后再把自己（q2/q3 的值）存进去。q2/q3 在此扮演了「保存阶段的中转寄存器」。

**调用 C 处理函数** 在 [firmware/start.S:180-195](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L180-L195)：

```asm
lui sp, %hi(irq_stack)
addi sp, sp, %lo(irq_stack)     # 给中断处理函数一个独立栈

lui a0, %hi(irq_regs)
addi a0, a0, %lo(irq_regs)      # a0 = &irq_regs（参数1：寄存器基址）

picorv32_getq_insn(a1, q1)      # a1 = 中断号位掩码（参数2）

jal ra, irq                     # 调 C 函数 irq(regs, irqs)
```

C 函数原型见 [firmware/firmware.h:15](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/firmware.h#L15)：`uint32_t *irq(uint32_t *regs, uint32_t irqs);`——参数1 是保存的寄存器数组指针，参数2 是中断号位掩码，**返回值是（可能新的）寄存器数组指针**。

**恢复与返回** 在 [firmware/start.S:199-328](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L199-L328)，关键是把 C 函数返回的指针当作新的寄存器基址（允许处理函数整体搬迁寄存器帧），最后：

```asm
picorv32_retirq_insn()          # 返回中断
```

`irq_regs` 与 `irq_stack` 是两块预留的静态内存，[firmware/start.S:330-338](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L330-L338)：32 个 32 位寄存器槽 + 128 字节中断栈。

**C 处理函数 irq()** 在 [firmware/irq.c:10-139](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/irq.c#L10-L139)，按中断号位分发：

```c
uint32_t *irq(uint32_t *regs, uint32_t irqs)
{
    static unsigned int ext_irq_4_count = 0;
    static unsigned int ext_irq_5_count = 0;
    static unsigned int timer_irq_count = 0;
    ...
    if ((irqs & (1<<4)) != 0) ext_irq_4_count++;   // 外部中断 4
    if ((irqs & (1<<5)) != 0) ext_irq_5_count++;   // 外部中断 5
    if ((irqs & 1) != 0) timer_irq_count++;        // 定时器中断（bit0）

    if ((irqs & 6) != 0) { ... }                   // bit1/bite2：非法指令/总线错误
    ...
    return regs;
}
```

值得注意的细节：

- 一次调用可能同时处理多个中断（`irqs` 是位掩码），所以用 `if` 而非 `else if`，逐位检查。
- bit1/bit2（非法指令、总线错误）是致命错误，处理函数会解码出错指令、打印完整寄存器转储，然后执行 `ebreak` 停机（[firmware/irq.c:60-135](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/irq.c#L60-L135)）。
- [firmware/irq.c:17-35](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/irq.c#L17-L35) 有一段「压缩指令 q0 校验」：它读 `regs[0]`（即保存的 q0/返回地址）的 LSB，与实际解码出的指令长度做一致性检查，若不符则报错。这正是 4.4 节所说「q0 的 LSB=压缩标志」的实际用途。

#### 4.5.4 代码实践

**目标**：用 `custom_ops.S` 的宏实现一个「周期性定时器中断翻转内存标志」的最小示例，跑通中断闭环。

**步骤**：

1. **阅读理解**：先确认定时器路径——[firmware/start.S:390-394](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L390-L394) 的 `TEST` 宏用 `picorv32_timer_insn(zero, x1)`（x1=1000）给每条 riscv-test 设了 1000 周期看门狗；定时器到点触发 bit0 中断，进入 `irq()` 的 `if ((irqs & 1) != 0)` 分支（[firmware/irq.c:47-50](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/irq.c#L47-L50)）。

2. **添加标志变量与翻转逻辑**：在 `irq.c` 顶部加一个全局标志，并在定时器分支里翻转它：

   ```c
   /* 示例代码：在 irq.c 顶部增加 */
   volatile uint32_t timer_toggle_flag = 0;
   ```

   在 `if ((irqs & 1) != 0)` 分支里追加：

   ```c
   if ((irqs & 1) != 0) {
       timer_irq_count++;
       timer_toggle_flag ^= 1;            /* 每次定时器中断翻转标志 */
       /* 重新装载定时器，使其周期性触发 */
       uint32_t old;
       __asm__ volatile (
           ".word 0x0600052B" : "=r"(old) : "0"(1000) :   /* timer x?, x? 的等价 .word */
       );
   }
   ```

   > 上面的内联汇编只是示意编码思路；更稳妥的做法是在 `custom_ops.S` 里新增一个 C 可调用的 `set_timer(uint32_t)` 函数，内部用 `picorv32_timer_insn` 宏，再在 C 里声明 `extern void set_timer(uint32_t);` 调用。这样避免手算 `.word` 出错。

3. **更简单的可验证版本**：直接在定时器分支加一句打印，观察触发次数：

   ```c
   /* 示例代码 */
   print_str("[T"); print_dec(timer_irq_count); print_str("]");
   ```

4. **构建与观察**：执行 `make test`（需已装好 RISC-V 工具链，见 [u2-l1](u2-l1-toolchain-and-hex.md)）。测试台的 `irq` 输入由 `testbench.v` 驱动，定时器中断会周期性进入 `irq()`。

**观察现象**：

- 加打印版：输出里会看到 `[T1][T2]...` 递增，证明定时器中断被反复触发并进入 C 处理函数。
- 注意：原 `irq.c` 在 bit1/bit2 时会 `ebreak` 停机，所以正常路径上只有 bit0/4/5 会被频繁触发。

**预期结果**：能说清楚「定时器中断从 `timer` 指令装载 → 倒计时 → bit0 置挂起 → `irq_state` 进入 → `irq_vec` 保存 → `irq()` 的 bit0 分支 → `retirq` 返回」的完整调用链。若改动后 `make test` 仍能跑完固件并打印 `DONE`，说明中断软件栈工作正常。**待本地验证**：实际输出取决于工具链与测试台是否就绪；若无法运行，至少完成「源码阅读」部分，对照上述调用链在源码里逐一找到对应行。

#### 4.5.5 小练习与答案

**练习 1**：`irq_vec` 为什么在保存 `x3..x31` 之前，先用 `setq q2, x1` 和 `setq q3, x2` 把 ra/sp 藏起来？
**答案**：因为保存所有寄存器到内存需要 `x1` 作基址、`x2` 作数据中转，可这俩本身存着活值（ra/sp）。先存进 q2/q3 腾出 x1/x2 当工具，最后再把 q2/q3 的值（即原 ra/sp）存进内存，避免覆盖丢失。

**练习 2**：`irq()` 函数返回 `regs` 指针，而 `irq_vec` 恢复时用 `addi x1, a0, 0`（[firmware/start.S:202](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L202)）把这个返回值作为新的基址。这种设计有什么好处？
**答案**：它允许 C 处理函数**整体搬迁寄存器帧**——只要返回一个新的 32 字连续区域指针，恢复阶段就会从那里读回寄存器。这为实现「寄存器组切换」（多任务上下文切换）留下了扩展点，而硬件无需改动。

**练习 3**：为什么 `irq.c` 里检查 bit1/bit2 错误时，要用 `regs[0]` 的 LSB 判断被中断指令是不是压缩指令（[firmware/irq.c:18](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/irq.c#L18)）？
**答案**：`regs[0]` 保存的是 q0（返回地址），硬件在写 q0 时把 `latched_compr` 放进了 LSB（[picorv32.v:1325](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1325)）。软件据此回退 2（压缩）或 4（标准）字节定位出错指令的首字节，从而正确解码它。

## 5. 综合实践

把本讲知识串起来，完成一个「端到端追踪一次定时器中断」的源码阅读任务：

**任务**：在一张大图上画出从「固件执行 `timer` 指令装载初值」到「`retirq` 返回被中断点」的全部环节，每一步标注**对应的源码文件与行号**。

**建议的追踪路径**（请逐一在源码中找到并填空）：

1. 固件执行 `picorv32_timer_insn` —— 编码见 [custom_ops.S:100-101](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/custom_ops.S#L100-L101)。
2. 译码产生 `instr_timer` —— [picorv32.v:1093](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1093)。
3. 在 `ld_rs1` 执行，写入 `timer` 寄存器 —— [picorv32.v:1687-1694](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1687-L1694)。
4. 每拍 `timer` 递减 —— [picorv32.v:1442-1444](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1442-L1444)。
5. 归零时置 `irq_pending[0]` —— [picorv32.v:1917-1919](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1917-L1919)。
6. `launch_next_insn` 因有待处理中断而为假 —— [picorv32.v:1400](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1400)。
7. `irq_state` 走 `00→01→10`，写 q0/q1、置 `eoi`、清挂起 —— [picorv32.v:1538-1546](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1538-L1546)、[picorv32.v:1506-1514](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1506-L1514)。
8. PC 跳到 `irq_vec`（`PROGADDR_IRQ=0x10`）—— [start.S:41-52](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L41-L52)。
9. `irq_vec` 保存寄存器、读 q1 得中断号、调 `irq()` —— [start.S:57-195](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L57-L195)。
10. `irq()` 的 bit0 分支处理定时器 —— [irq.c:47-50](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/irq.c#L47-L50)。
11. 返回、恢复寄存器 —— [start.S:199-262](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/start.S#L199-L262)。
12. `retirq`：`eoi<=0`、`irq_active<=0`、`PC<=q0` —— [picorv32.v:1667-1677](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1667-L1677)。

**交付物**：一张包含上述 12 步的流程图（手绘或文字版均可），每步标注 `文件:行号`。完成它，你就把 PicoRV32 中断机制的硬件与软件、自定义指令与状态机、q 寄存器与 C 处理栈全部贯通了。

## 6. 本讲小结

- PicoRV32 **不使用 RISC-V 特权架构**，而是用 `custom0` 操作码下的 6 条自定义指令（`getq`/`setq`/`retirq`/`maskirq`/`waitirq`/`timer`）+ 4 个 q 寄存器 + 内置 32 路中断控制器，以最小硬件代价实现中断。
- 中断控制器有 32 位 `irq_pending`（挂起）、`irq_mask`（屏蔽，复位全 1）、`irq_active`（处理中）三个核心寄存器；bit0/1/2 是定时器/非法指令/总线错误三个内置源，bit3–31 由 `irq` 端口外部驱动。
- 6 条指令靠 opcode（`0x0B`）+ funct7 区分，funct3 与 rs2 被译码器忽略；`retirq`/`waitirq` 在第一级译码（需改 `decoded_rs1`），其余 4 条在第二级。
- **q0..q3 不是独立触发器**，而是嫁接在通用寄存器堆 `cpuregs` 末尾的 4 项（下标从 `irqregs_offset` 起），靠译码期改写 `decoded_rs1`/`latched_rd` 映射访问——这是「零成本多出 4 个寄存器」的精妙技巧。
- 中断进入由 2 位 `irq_state` 状态机在 `fetch` 内分拍完成：自动写 q0=返回地址（LSB=压缩标志）、q1=中断号掩码、置 `eoi`、清挂起位，再跳到 `PROGADDR_IRQ`，全程不访存。
- 固件软件栈分三层：`reset_vec` 初始化、`irq_vec`（汇编）做寄存器保存/恢复与 C 调用、`irq()`（C）按位掩码分发处理；`retirq` 清 `eoi`/`irq_active` 并用 q0 恢复 PC。

## 7. 下一步学习建议

- **继续硬件扩展主题**：本讲讲了「自定义指令」做中断，下一讲 [u7-l1 AXI4-Lite 与 Wishbone 适配](u7-l1-axi-wishbone-adapters.md) 讲另一种扩展——把原生内存接口桥接到标准总线，可对照理解 PicoRV32「最小核心 + 多种适配/扩展」的设计哲学。
- **深入理解 trace**：[u8-l2 仿真测试台与执行追踪](u8-l2-simulation-and-tracing.md) 会讲 `showtrace.py` 如何解码 `TRACE_IRQ` 标记，届时可用 trace 实测本讲描述的中断进出时序。
- **建议精读的源码**：重读 [picorv32.v:1486-1565](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1486-L1565) 的整个 `cpu_state_fetch` case，把中断进入、`waitirq` 阻塞、普通指令启动三种路径放在一起比较，体会 `launch_next_insn` 作为「总开关」的作用。
- **如果想做二次开发**：尝试仿照 [firmware/custom_ops.S](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/firmware/custom_ops.S) 增加一个新的 q 寄存器访问封装，或仿照 `irq_vec` 写一个更精简的「只保存 caller-saved 寄存器」的快速中断版本（参考 `start.S` 里 `ENABLE_FASTIRQ` 的条件编译分支）。
