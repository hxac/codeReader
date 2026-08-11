# 辅助寄存器 ARP/AR 与数据页指针 DP

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 **ARP、AR、DP 这三组寄存器各自管什么**：ARP 是「辅助寄存器指针」（指向当前用哪一个 AR），AR[0]/AR[1] 是两个「辅助寄存器」（主要当地址指针用），DP 是「数据页指针」（直接寻址时给出 RAM 地址的最高位）。
- 读懂 [ARP/AR 的更新块](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L290-L329) 与 [DP 的更新块](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L331-L343)，理解它们都用同一套 **`set`/`rst` 锁存 + `inc`/`dec` 更新** 的组合模式。
- 弄清 [AR 的自增/自减只作用在低 9 位 `[8:0]`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L321-L326)，并能把它对应到官方手册「page 2-9」描述的辅助寄存器算术行为。
- 看懂微码里那个反复出现的「间接寻址操作数译码片段」如何由指令字的第 7/5/4/3/0 位驱动 `reg_ar_inc/dec` 与 `reg_arp_set/rst`。

本讲是进阶层（u2）的第四讲，承接 u2-l1（内部写总线 `reg_wrbus`）。u2-l1 讲的是「数据从哪条通路流过来」，本讲回答的是「**地址**从哪里来」——具体说，是数据 RAM 的 8 位地址 `ram_addr` 是怎么由 ARP/AR/DP 三组寄存器拼出来的。把这些寻址寄存器看明白，再去读后面 u2-l5（数据 RAM 与寻址方式）、u3-l4（辅助寄存器类指令译码）就会非常顺。

## 2. 前置知识

### 2.1 为什么需要「专门的地址寄存器」

TMS32010 是一颗 16 位定点 DSP，它要在 256 字的数据 RAM 里频繁读写中间结果。如果你每次访问 RAM 都要把地址先搬到某个通用寄存器、再送去地址端口，来回搬运很费周期。DSP 的常见做法是**设置若干「专职地址寄存器」**，让它们能够：

1. **直接当 RAM 地址用**（不用再经累加器中转）；
2. **在访问的同时自动 ±1**（自增/自减），方便遍历数组、实现循环缓冲；
3. **在多条指令之间快速切换**当前使用哪一个。

TMS32010 提供了两个这样的辅助寄存器 AR0、AR1，外加一个 1 位的指针 ARP 来指明「现在用哪一个」。这就是本讲的 ARP/AR 子系统。

### 2.2 直接寻址与间接寻址

TMS32010 的很多指令（ADD、LAC、SACH……）的最后一个操作数是「数据 RAM 地址」。这个地址有两种给出方式，由指令字的**第 7 位**选择：

| `if_opcodereg[7]` | 寻址方式 | 地址来源 |
|-------------------|----------|----------|
| `0` | **直接寻址** | `{reg_dp, if_opcodereg[6:0]}`——DP 给最高位，指令字低 7 位给偏移 |
| `1` | **间接寻址** | `reg_ar[reg_arp][7:0]`——由 ARP 选中的那个 AR 给出地址 |

这正是源码里 [第 488 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L488) 的那一行 `ram_addr` 选择逻辑。本讲的主角——ARP、AR、DP——就是为这两种寻址方式服务的：

- **间接寻址**用 ARP + AR；
- **直接寻址**用 DP。

### 2.3 `set`/`rst` 与 `inc`/`dec`：四种「如何改一个寄存器」的控制

本讲的寄存器更新逻辑反复用到两套控制信号，先在这里统一解释，后面就不再重复。

**第一套：`set` / `rst`（置位 / 复位）**——用于 1 位寄存器（ARP、DP）。两个信号拼成一对 `{set, rst}`，在每个时钟沿按真值表决定新值：

| `{set, rst}` | 新值 |
|--------------|------|
| `2'b10` | `1`（置位） |
| `2'b01` | `0`（复位） |
| `2'b00` | 保持原值 |
| `2'b11` | 保持原值（两个都拉高，按保持处理） |

> 注意：源码里的 `set`/`rst` 是「**这一拍要不要把它改成 1/0**」的**脉冲式**控制，而不是电平。每拍微码都会重新给它们赋默认值 `NO`，只有需要改写的指令才把它拉成 `YES`。所以它更像是「本拍请求改写」，而非持续的电平输入。

**第二套：`inc` / `dec`（自增 / 自减）**——用于多位的 AR。两个信号拼成 `{inc, dec}`：

| `{inc, dec}` | 新值 |
|--------------|------|
| `2'b00` | 保持 |
| `2'b10` | `AR[8:0] + 1` |
| `2'b01` | `AR[8:0] - 1` |
| `2'b11` | 保持（两个都拉高，按保持处理） |

记住这两张表，本讲后面所有的更新逻辑都只是「谁来拉这些信号」的问题。

### 2.4 `cyc_ncen`：唯一的写入节拍

和 u2-l1、u2-l2 一样，ARP/AR/DP 这些寄存器**只在 `cyc_ncen`（`cyclecntr==3`，机器周期的第 3 相）那个 `i_EMUCLK` 上升沿**才会更新。这一点在源码里体现为更新块都被包在 `else begin if(cyc_ncen) begin ... end end` 里。换句话说：**一个机器周期内这些寄存器最多变化一次**，且发生在周期末尾的那个节拍。忘了这一点在看波形时会非常困惑。

## 3. 本讲源码地图

本讲涉及两个源文件，引用其中若干段。

| 文件 | 本讲关注的位置 | 作用 |
|------|--------------|------|
| `src/IKA32010.sv` | [第 290–299 行：ARP/AR 声明与输出](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L290-L299) | 声明 ARP、两个 AR，以及把 AR 当地址/数据输出的连线 |
| `src/IKA32010.sv` | [第 302–329 行：ARP/AR 更新块](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L302-L329) | `set/rst` 改 ARP、`ld/inc/dec` 改 AR 的时序逻辑 |
| `src/IKA32010.sv` | [第 331–343 行：DP 更新块](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L331-L343) | `set/rst` 改 DP 的时序逻辑 |
| `src/IKA32010.sv` | [第 479 行：`flag_output` 状态字](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L479) | 把 ARP、DP 拼进 16 位状态寄存器（供 LST/SSR 读写） |
| `src/IKA32010.sv` | [第 488 行：`ram_addr`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L488) | 直接/间接寻址的地址选择，ARP/AR/DP 的「出口」 |
| `src/IKA32010.sv` | [第 564–570 行：默认值](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L564-L570) | 微码块顶部给 ARP/AR/DP 所有控制信号的默认 `NO` |
| `src/IKA32010.sv` | [第 786–802 行：ADD 的间接寻址片段](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L786-L802) | 典型的「操作数译码片段」，几乎所有算逻指令都长这样 |
| `src/IKA32010.sv` | [第 1092–1184 行：辅助寄存器类指令](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1092-L1184) | LAR / LARK / MAR(LARP) / LDP / LDPK / MAR(NOP) / SAR 的译码 |
| `src/IKA32010.sv` | [第 663–688 行：LST](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L663-L688) 与 [第 752–768 行：SSR](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L752-L768) | 状态寄存器的装入/存储，会同时动 ARP 和 DP |
| `src/IKA32010.sv` | [第 1213–1231 行：BANZ](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1213-L1231) | 「AR 不为零则跳转」，用到 `AR[8:0]` 与 `reg_ar_dec` |
| `src/IKA32010_mnemonics.sv` | [第 59–63 行：`YES/NO/HIGH/LOW`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L59-L63) | 微码里 `set=YES`/`rst=NO` 等用的语义常量 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 辅助寄存器指针 ARP 与辅助寄存器 AR[0:1]**：谁是「指针的指针」，AR 如何被加载/自增自减、如何当 RAM 地址用。
- **4.2 数据页指针 DP**：直接寻址的最高位，以及它和状态寄存器的关系。
- **4.3 `set`/`rst`/`inc`/`dec` 控制逻辑**：微码里那个反复出现的操作数译码片段，把指令字的位映射到这些控制信号。

### 4.1 辅助寄存器指针 ARP 与辅助寄存器 AR[0:1]

#### 4.1.1 概念说明

TMS32010 有两个辅助寄存器 **AR0、AR1**，它们最主要的用途是**当数据 RAM 的地址指针**，并在被访问的同时自动增减——这对 DSP 做 FIR 滤波、卷积、数组遍历非常合用（不用额外花周期去更新地址）。

既然有两个 AR，就需要一个 1 位的「**辅助寄存器指针 ARP**」来指明「当前这条间接寻址指令用的是 AR0 还是 ARP」。所以这是一个两层结构：

- **ARP**（1 位）：指针的指针，选 AR0 还是 AR1；
- **AR[0]、AR[1]**（各 16 位）：真正的地址寄存器。

源码里把它们写成数组 `reg_ar[0:1]`，于是 `reg_ar[reg_arp]` 就是「当前选中的 AR」，读起来很直观。

#### 4.1.2 核心流程

每个机器周期末尾（`cyc_ncen` 拍），ARP/AR 的更新按下面的优先级进行：

```text
复位 (i_RS_n=0)?
 ├─ 是：ARP←0, AR0←0, AR1←0   （同步复位，清零）
 └─ 否：在 cyc_ncen 拍：
      ├─ ARP：按 {reg_arp_set, reg_arp_rst} 改写（见 2.3 节真值表）
      └─ AR：
           ├─ 若 reg_ar_ld=1：AR[ opcode[8] ] ← reg_wrbus   （LAR/LARK 加载）
           └─ 否：按 {reg_ar_inc, reg_ar_dec} 改写 AR[ reg_arp ][8:0]
                 （只动低 9 位，高 7 位不动）
```

三个要点先记住，源码精读里会逐一对应：

1. **加载（`reg_ar_ld`）和增减（`inc/dec`）互斥**：加载时整字 `reg_wrbus`（16 位）一次写入，增减时只动低 9 位。
2. **加载的目标由指令字第 8 位 `if_opcodereg[8]` 决定**，而**不是**由 ARP 决定——也就是说 LAR/LARK 可以显式指定写到 AR0 还是 AR1，与「当前用哪个」无关。
3. **增减的目标永远是「当前 ARP 选中的那个 AR」**（`reg_ar[reg_arp]`），且**只改低 9 位 `[8:0]`**。

#### 4.1.3 源码精读

先看声明与输出（[第 290–299 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L290-L299)）：

```verilog
//auxillary register pointer and register
reg             reg_arp_set, reg_arp_rst;   // 改写 ARP 的脉冲控制
reg             reg_arp;                     // ARP 本体（1 位）

reg             reg_ar_ld;                   // AR 加载使能（LAR/LARK 用）
reg             reg_ar_inc, reg_ar_dec;      // AR 自增/自减使能
reg     [15:0]  reg_ar[0:1];                 // 两个 16 位辅助寄存器

assign  ar_data_output = reg_ar[if_opcodereg[8]];        // SAR 存 AR 时，由 opcode[8] 选哪个
wire    [7:0]   ar_addr_output = reg_ar[reg_arp][7:0];   // 间接寻址地址，由 ARP 选哪个，取低 8 位
```

注意两处「选哪个 AR」的依据不同：**存数据（SAR）和加载（LAR）用 `opcode[8]`**，**当地址用用 `reg_arp`**。这是初学者最容易看混的地方——同样是「选 AR0 还是 AR1」，依据却分两套。

再看更新块本体（[第 302–329 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L302-L329)）：

```verilog
always @(posedge i_EMUCLK) begin
    if(!i_RS_n) begin                  // 复位（低有效）
        reg_arp <= 1'b0;
        reg_ar[0] <= 16'h0000;
        reg_ar[1] <= 16'h0000;
    end
    else begin if(cyc_ncen) begin      // 只在 cyc_ncen 拍更新
        // ARP：set/rst 真值表
        case({reg_arp_set, reg_arp_rst})
            2'b10:   reg_arp <= 1'b1;
            2'b01:   reg_arp <= 1'b0;
            default: reg_arp <= reg_arp;
        endcase

        // AR：加载优先，否则按 inc/dec 改低 9 位
        if(reg_ar_ld) begin
            reg_ar[if_opcodereg[8]] <= reg_wrbus;          // 整字加载，目标由 opcode[8] 定
        end
        else begin
            case({reg_ar_inc, reg_ar_dec})
                2'b00:   reg_ar[reg_arp]      <= reg_ar[reg_arp];            // 保持
                2'b01:   reg_ar[reg_arp][8:0] <= reg_ar[reg_arp][8:0] - 9'd1; // 自减（见手册 p2-9）
                2'b10:   reg_ar[reg_arp][8:0] <= reg_ar[reg_arp][8:0] + 9'd1; // 自增（见手册 p2-9）
                2'b11:   reg_ar[reg_arp]      <= reg_ar[reg_arp];            // 都拉高 = 保持
            endcase
        end
    end end
end
```

对照 [官方手册 page 2-9（PDF 第 32 页）](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/docs)——该页描述辅助寄存器算术：AR 的算术只作用在低 9 位，而 RAM 地址只取其中低 8 位。也就是说：

- AR 的**第 8 位**就像一个「额外的可见位」：它参与自增自减、也会被 `BANZ` 指令的判零逻辑看到（见 4.1 节末与 [第 1219 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1219)），但它**不会**出现在送给 RAM 的 8 位地址 `ar_addr_output`（`[7:0]`）里。
- 高 7 位（`[15:9]`）在自增自减时完全不动，只有 `reg_ar_ld` 整字加载时才会被 `reg_wrbus` 覆盖。

这种「存 16 位、算 9 位、寻址用 8 位」的设计正是源码注释反复指向手册 p2-9 的原因。

#### 4.1.4 代码实践

**实践目标**：验证 AR 自增只动低 9 位、且寻址只取低 8 位。

**操作步骤（源码阅读型）**：

1. 打开 [第 321–326 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L321-L326)，确认自增/自减的左值是 `reg_ar[reg_arp][8:0]`，而不是整个 `reg_ar[reg_arp]`。
2. 打开 [第 299 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L299)，确认送给 RAM 的地址是 `reg_ar[reg_arp][7:0]`。
3. 思考一个边界情形：假设 `reg_ar[1] = 16'h0100`（即低 9 位 = `9'b001000000`，低 8 位 = `8'h00`，第 8 位 = 1），此时执行一条「`*+`」型间接寻址指令（自增）。
4. 推算：自增后低 9 位 `9'b001000000 + 1 = 9'b001000001`，即 `reg_ar[1]` 变成 `16'h0101`；而**本拍**送给 RAM 的地址是自增**前**的 `ar_addr_output = 8'h00`（地址在节拍末才更新，本拍用的是旧值）。

**需要观察的现象 / 预期结果**：

- 自增不会把 `16'h01xx` 的「01」高位抹掉——高位不受影响。
- RAM 地址恒为低 8 位，所以 AR 在 `0x100` 与 `0x000` 指向的是**同一个** RAM 单元（都映射到地址 `0x00`）。第 8 位的区别只有 `BANZ` 看得见。

> 待本地验证：上述「本拍用旧地址、节拍末才更新」的时序结论，建议在你的仿真器里用一条 `LARK 1,0x100` + 带自增的间接读指令实测，看波形里 `ram_addr` 与 `reg_ar[1]` 的相对先后。

#### 4.1.5 小练习与答案

**练习 1**：若当前 `reg_arp=0`、`reg_ar[0]=16'h00FF`，执行一条「AR0 自增」的间接寻址指令后，`reg_ar[0]` 变成多少？如果接着再执行一次自增呢？

**参考答案**：第一次自增后低 9 位 `9'b001111111 + 1 = 9'b010000000`，即 `reg_ar[0] = 16'h0100`（第 8 位变 1，低 8 位归 0）。第二次自增得到 `16'h0101`。注意第一次自增后，虽然 AR 值变成了 `0x100`，但它指向的 RAM 地址仍是 `0x00`。

**练习 2**：为什么 `ar_data_output`（SAR 用）按 `if_opcodereg[8]` 选 AR，而 `ar_addr_output`（间接寻址用）却按 `reg_arp` 选 AR？

**参考答案**：因为 SAR/LAR/LARK 这类指令的指令字里**自带**「目标 AR 编号」字段（bit 8），允许你显式存取任意一个 AR，与当前 ARP 无关；而间接寻址的语义是「访问 ARP 当前指向的 AR 所指的单元」，所以必须用 ARP 来选。两套选择依据对应两种不同的使用场景。

---

### 4.2 数据页指针 DP

#### 4.2.1 概念说明

**DP（Data memory Page pointer）** 只有 1 位，作用非常单一：在**直接寻址**时，给出 RAM 地址（8 位）的**最高位**。

回顾 2.2 节，直接寻址时 `ram_addr = {reg_dp, if_opcodereg[6:0]}`：

- 指令字低 7 位（`[6:0]`）给出页内偏移（0~127）；
- DP 给出「当前在哪个页」——因为只有 1 位，所以只有**两页**，每页 128 字，合起来正好覆盖 256 字 RAM。

所以 DP=0 时直接寻址访问第 0 页（地址 0~127），DP=1 时访问第 1 页（地址 128~255）。修改它靠 `LDP`（从内存装）和 `LDPK`（装立即数）两条指令。

> 顺带一提：TMS32010 状态寄存器里 DP 的官方定义就是 1 位，源码忠实复刻了这一点，不要把它和某些 later DSP（DP 是多位）混淆。

#### 4.2.2 核心流程

DP 的更新流程与 ARP 几乎一模一样，只是少了「增减」这套（DP 只有 1 位，没有自增自减的概念）：

```text
在 cyc_ncen 拍：
  按 {reg_dp_set, reg_dp_rst} 改写 DP：
     2'b10 → DP←1
     2'b01 → DP←0
     其它  → 保持
```

`reg_dp_set/rst` 由 `LDP`、`LDPK`、`LST` 三类指令驱动（详见 4.3）。

#### 4.2.3 源码精读

DP 更新块（[第 331–343 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L331-L343)）：

```verilog
//data memory page pointer
reg             reg_dp_set, reg_dp_rst;
reg             reg_dp;
always @(posedge i_EMUCLK) begin
    if(i_RS_n) reg_dp <= 1'b0;            // ← 注意：这里是 i_RS_n（没有取反 !）
    else begin if(cyc_ncen) begin
        case({reg_dp_set, reg_dp_rst})
            2'b10:   reg_dp <= 1'b1;
            2'b01:   reg_dp <= 1'b0;
            default: reg_dp <= reg_dp;
        endcase
    end end
end
```

⚠️ **请仔细对比这一段和 4.1 的 ARP 块**：

- ARP 块（[第 303 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L303)）写作 `if(!i_RS_n)`——「**复位有效时**（RS_n=0）清零」，这是标准的低有效同步复位写法。
- DP 块（[第 335 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L335)）却写作 `if(i_RS_n)`——**没有取反 `!`**，极性与上一段相反。

按字面直译，DP 块的含义是「**非复位态**（RS_n=1）时每个时钟沿把 DP 清零，`case` 逻辑反而只在**复位态**（RS_n=0）才生效」。这与「复位清零、正常时按 set/rst 改写」的直觉正好相反，也和 ARP/AR 块的写法不一致。

这是一处值得你**亲自在仿真里验证**的地方：实测 DP 在 `LDP`/`LDPK` 之后能否真正被置位、并影响直接寻址的地址最高位。本讲义不臆断它是不是笔误，只如实记录源码现状——这种「两个相邻块写法不一致」的细节，恰恰是读真实源码时最该停下来确认的地方。

> 待本地验证：写一段最小激励——复位后执行 `LDPK 1`，再用一条直接寻址的 `LAC` 读地址 `0x00`，观察 `reg_dp` 是否变成 1、`ram_addr` 最高位是否随之翻转。

DP 的另一个「出口」是状态寄存器。看 [第 479 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L479)：

```verilog
assign  flag_output = {alu_flag_ovfl, reg_ovm, reg_intm, 4'b1111, reg_arp, 6'b111111, 1'b1, reg_dp};
//bit1 is don't care
```

这拼出的 16 位状态字，位布局是（bit15 在最左）：

| bit | 15 | 14 | 13 | 12..9 | 8 | 7..2 | 1 | 0 |
|-----|----|----|----|-------|---|------|---|---|
| 内容 | V (溢出) | OVM | INTM | 1111 | **ARP** | 111111 | 1 (don't care) | **DP** |

可以看到 **ARP 落在 bit 8、DP 落在 bit 0**——这正是 `LST`/`SSR` 读写状态寄存器时的位映射依据（4.3 节会用到）。

#### 4.2.4 代码实践

**实践目标**：把 DP 的「输入（set/rst）」和「输出（状态字 bit0、ram_addr 最高位）」串起来看一遍。

**操作步骤（源码阅读型）**：

1. 在 [第 1132–1148 行 `LDP`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1132-L1148) 找到：`if(reg_wrbus[0]) reg_dp_set = YES; else reg_dp_rst = YES;`——LDP 是从内存读一个字，用它的 **bit 0** 来置/复位 DP。
2. 在 [第 1150–1158 行 `LDPK`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1150-L1158) 找到：`if(if_opcodereg[0]) reg_dp_set = YES; else reg_dp_rst = YES;`——LDPK 用**指令字 bit 0**（立即数）来置/复位 DP。
3. 回到 [第 488 行 `ram_addr`](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L488)，确认直接寻址时 DP 是地址的最高位。

**预期结果**：你能画出一条完整链路 `LDPK 的立即数 bit0 → reg_dp_set/rst → reg_dp → ram_addr[7]`。

#### 4.2.5 小练习与答案

**练习 1**：DP 的取值范围是多少？它能把 256 字 RAM 分成几页、每页多大？

**参考答案**：DP 只有 1 位，取值 0 或 1。它把 256 字 RAM 分成 2 页，每页 128 字（由指令字低 7 位编址 0~127）。DP 给地址最高位，页内偏移由指令字给出。

**练习 2**：状态寄存器里 DP 在 bit 0、ARP 在 bit 8。`LST` 指令从内存装状态字时，分别用 `reg_wrbus` 的哪几位去驱动 DP 和 ARP（直接寻址形态下）？

**参考答案**：见 [第 669 行与第 681–682 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L663-L688)：直接寻址时，DP 由 `reg_wrbus[0]` 驱动（`reg_dp_set/rst`），ARP 由 `reg_wrbus[8]` 驱动（`reg_arp_set/rst`）。正好对应状态字里 bit0=DP、bit8=ARP 的布局。

---

### 4.3 set/rst/inc/dec 控制逻辑

#### 4.3.1 概念说明

前两节讲了 ARP/AR/DP「**怎么改**」（set/rst/inc/dec 真值表）。本节回答最后一个问题：「**谁来改、改谁**」——也就是微码如何根据指令字，把这些控制信号拉成 `YES`。

这里有一个贯穿全芯片的关键设计：**几乎所有带数据操作数的指令（ADD、LAC、SACH、SAR、LAR、LDP……）都共用同一套「间接寻址操作数译码片段」**。这段片段读指令字的第 7/5/4/3/0 位，决定本拍要不要改 ARP、要不要增减 AR。它出现得如此频繁，以至于你只要看懂这一段，就等于看懂了几十条指令对 ARP/AR 的副作用。

#### 4.3.2 核心流程

先把这段「万能片段」的位映射关系列清楚。**只有当指令字 bit7=1（间接寻址）时它才生效**：

```text
if (if_opcodereg[7] == 1) begin        // 进入间接寻址分支
    reg_ar_inc = if_opcodereg[5];       // bit5 → AR 自增
    reg_ar_dec = if_opcodereg[4];       // bit4 → AR 自减
    if (if_opcodereg[3] == 0) begin     // bit3=0 表示「本条要更新 ARP」
        if (if_opcodereg[0]) reg_arp_set = YES;   // bit0=1 → ARP←1
        else                reg_arp_rst = YES;    // bit0=0 → ARP←0
    end
end
```

整理成「指令字位 → 控制信号」对照表：

| 指令字位 | 含义 | 驱动的控制信号 |
|----------|------|----------------|
| `[7]` | 寻址方式：0=直接，1=间接 | 进入本片段的闸门 |
| `[5]` | （间接时）AR 自增请求 | `reg_ar_inc` |
| `[4]` | （间接时）AR 自减请求 | `reg_ar_dec` |
| `[3]` | （间接时）0=更新 ARP，1=保持 ARP | 是否进入 ARP 改写 |
| `[0]` | （间接且 `[3]=0` 时）新 ARP 值 | `reg_arp_set`（=1）/ `reg_arp_rst`（=0） |

> 注意位 6 没有出现在这里——它在源码的这套译码里未被使用（直接寻址时它是 7 位页内偏移的最高位 `if_opcodereg[6]`，参与 `ram_addr` 而非 ARP/AR 控制）。

而 DP 的控制信号只由三条指令产生：`LDP`（用 `reg_wrbus[0]`）、`LDPK`（用 `if_opcodereg[0]`）、`LST`（用 `reg_wrbus[0]`）。它们都遵循同一个套路：取某一个数据位，为 1 则 `set`、为 0 则 `rst`。

#### 4.3.3 源码精读

先看这段「万能片段」最典型的实例——[ADD 指令（第 786–802 行）](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L786-L802)：

```verilog
//ADD - Add to accumulator with shift
16'b0000_????_????_????: begin
    alu_modesel = ALU_ADD;
    alu_acc_ld = YES;
    sha_amt = {1'b0, if_opcodereg[11:8]};

    if(if_opcodereg[7]) begin                       // ← 万能片段开始
        reg_ar_inc = if_opcodereg[5];
        reg_ar_dec = if_opcodereg[4];
        if(!if_opcodereg[3]) begin
            if(if_opcodereg[0]) reg_arp_set = YES;
            else                reg_arp_rst = YES;
        end
    end                                              // ← 万能片段结束
    ...
end
```

ADD 的真正本职（ALU 加法、移位）只占开头三行；后面这一整段都在处理「间接寻址操作数对 ARP/AR 的副作用」。把这段贴到 grep 里搜一下（见 4.3.4 实践），你会发现它在 ADD、ADDH、ADDS、SUB、SUBH、SUBS、AND、OR、XOR、LAC、SACH、SACL、LAR、SAR、LST、SSR、LDP……几十条指令里**几乎一字不差**地重复。

再看几条「专职」管理这些寄存器的指令（[第 1092–1184 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1092-L1184)）：

```verilog
//LAR - Load Auxillary Register  (16'b0011_100?_????_????)
16'b0011_100?_????_????: begin
    reg_ar_ld = YES;                       // 整字加载 AR，目标由 opcode[8] 定
    if(if_opcodereg[7]) begin /* 万能片段 */ end
end

//LARK - Load Auxillary Register Immediate  (16'b0111_000?_????_????)
16'b0111_000?_????_????: begin
    register_wrbus_source_sel = WRBUS_SOURCE_IMM;  // 取指令字低 8 位作立即数
    reg_ar_ld = YES;                       // 写入 AR[ opcode[8] ]
end

//MAR(LARP) - 修改 AR/ARP（仅间接寻址形态，16'b0110_1000_1???_????）
16'b0110_1000_1???_????: begin
    reg_ar_inc = if_opcodereg[5]; reg_ar_dec = if_opcodereg[4];
    if(!if_opcodereg[3]) begin
        if(if_opcodereg[0]) reg_arp_set = YES;
        else                reg_arp_rst = YES;
    end
end

//LDPK - Load DP Immediate  (16'b0110_1110_0000_000?)
16'b0110_1110_0000_000?: begin
    if(if_opcodereg[0]) reg_dp_set = YES;   // 立即数 bit0 → DP
    else                reg_dp_rst = YES;
end

//SAR - Store Auxillary Register  (16'b0011_000?_????_????)
16'b0011_000?_????_????: begin
    register_wrbus_source_sel = WRBUS_SOURCE_AR;   // 把 AR[ opcode[8] ] 送上总线
    ram_wr = YES;                                  // 写进 RAM
    if(if_opcodereg[7]) begin /* 万能片段 */ end
end
```

几个值得记住的点：

- **LARK** 不含万能片段——它是立即数加载，操作数不是「内存地址」，所以不需要间接寻址的副作用。它的目标 AR 由 `opcode[8]` 选，立即数由指令字低 8 位经 `WRBUS_SOURCE_IMM`（零扩展到 16 位）提供。
- **LAR**（从内存加载 AR）含万能片段：因为它带一个真正的数据内存操作数，间接寻址时同样要处理 ARP/AR 副作用。注意它用 `reg_ar_ld`（整字加载，目标 `opcode[8]`），而**不是** `inc/dec`。
- **MAR** 在源码里被拆成两种编码：`MAR(LARP)`（操作数 bit7=1，真正修改 AR/ARP）和 `MAR(NOP)`（[第 1160–1166 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1160-L1166)，操作数 bit7=0，什么都不做）。这和官方手册对 MAR 的描述是对得上的——直接寻址形态下的 MAR 不修改任何辅助寄存器。
- 最后别忘了 **BANZ**（[第 1213–1231 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1213-L1231)），它直接用 `reg_ar_dec = YES` 无条件自减当前 AR，并用 `reg_ar[reg_arp][8:0] != 0` 决定是否跳转——这是 AR 低 9 位被「完整」使用的又一个证据。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：在源码中找出**所有**驱动 `reg_ar_inc/dec` 与 `reg_arp_set/rst` 的微码片段，总结这些控制信号由指令字的哪些位决定。这正是本讲规格里指定的实践任务。

**操作步骤**：

1. 在仓库根目录用 ripgrep 搜索这三个信号被赋值的位置（注意排除声明行和默认值行）：

   ```bash
   rg -n 'reg_ar_inc\s*=' src/IKA32010.sv
   rg -n 'reg_ar_dec\s*=' src/IKA32010.sv
   rg -n 'reg_arp_set\s*=' src/IKA32010.sv
   rg -n 'reg_arp_rst\s*=' src/IKA32010.sv
   ```

2. 你会得到大约 30+ 处命中。把它们分类：
   - **绝大多数**命中的右值都是同一个表达式 `if_opcodereg[5] / if_opcodereg[4] / if_opcodereg[0]`，且都包在 `if(if_opcodereg[7])` 里——这就是 4.3.2 节的「万能片段」。
   - **唯一**一个例外是 [BANZ（第 1222 行）](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1222)：`reg_ar_dec = YES;`，无条件自减，不依赖操作数位。
3. 再搜 DP 的控制信号：

   ```bash
   rg -n 'reg_dp_set\s*=|reg_dp_rst\s*=' src/IKA32010.sv
   ```

   应该只在 `LDP`、`LDPK`、`LST` 三条指令里出现（以及默认值 `NO`）。

4. 把结果填进下面这张「位映射总结表」：

   | 控制信号 | 由谁驱动 | 依据的指令字位 / 数据 |
   |----------|----------|----------------------|
   | `reg_ar_inc` | 万能片段 | `if_opcodereg[5]`（仅 `[7]=1`） |
   | `reg_ar_dec` | 万能片段 / BANZ | `if_opcodereg[4]` / 无条件 `YES` |
   | `reg_arp_set` | 万能片段 | `if_opcodereg[0]==1`（仅 `[7]=1, [3]=0`） |
   | `reg_arp_rst` | 万能片段 / LST(间接) | `if_opcodereg[0]==0` / 同上 |
   | `reg_dp_set` | LDP / LDPK / LST | `reg_wrbus[0]` / `if_opcodereg[0]` / `reg_wrbus[0]` |
   | `reg_dp_rst` | LDP / LDPK / LST | 同上，取反 |

**需要观察的现象 / 预期结果**：你会确认「ARP/AR 的副作用由指令字第 7/5/4/3/0 位统一编码」这一设计——也就是说，**同一条 ADD 指令，只要操作数那几位不同，就可以在「直接寻址」「间接寻址且 AR 自增并切换 ARP」等模式之间切换**，而无需换指令。这正是 TMS32010 指令集紧凑的来源。

#### 4.3.5 小练习与答案

**练习 1**：假设指令字是 `16'b0000_0000_1100_0000`（即 ADD 的操作码 `0000` + 移位 `0000` + 操作数 `1100_0000`）。这条 ADD 走的是直接还是间接寻址？会对 ARP/AR 产生什么副作用？

**参考答案**：操作数 bit7=`1`，所以是间接寻址。`bit5=1, bit4=0` → `reg_ar_inc=YES, reg_ar_dec=NO`，当前 AR 自增 1。`bit3=1` → 不更新 ARP。所以这条指令是「用当前 AR 间接寻址、访问后 AR 自增、ARP 保持不变」。

**练习 2**：为什么 `reg_ar_inc` 和 `reg_ar_dec` 可以同时为 1（见真值表 `2'b11`→保持），而真值表把它定义成「保持」而不是「未定义」？

**参考答案**：因为 `{inc, dec}` 直接来自指令字的两个独立位（`[5]` 和 `[4]`），编译器/汇编器可能产生 `11` 这种组合（对应手册里某些保留或等价于 NOP 的编码）。把 `11` 明确定义为「保持」可以让硬件在遇到这种输入时表现确定、无毛刺，而不是落入未覆盖的 `default`。这与 set/rst 的 `11→保持` 是同一种防御性设计。

## 5. 综合实践

把本讲三组寄存器串起来，做一次「手工执行」训练。

**任务**：给定下面的指令序列（操作数为示意，帮助你聚焦在 ARP/AR/DP 上），逐条推演 `reg_arp`、`reg_ar[0]`、`reg_ar[1]`、`reg_dp` 以及下一条间接/直接寻址指令会用到的 `ram_addr`。假设初始 `ARP=0, AR0=0x000, AR1=0x000, DP=0`。

1. `LARK 0, 0x05`——立即数 `0x05` 装入 AR0。
2. `LARK 1, 0x80`——立即数 `0x80` 装入 AR1。
3. `LDPK 1`——DP 置 1。
4. 一条 `ADD`，操作数编码为 `1100_0000`（间接、AR 自增、不更新 ARP）。
5. 一条 `ADD`，操作数编码为 `0000_0010`（直接寻址、页内偏移 2）。

**参考推演**：

| 步骤 | 指令 | 关键控制信号 | ARP | AR0 | AR1 | DP | 本条 ram_addr |
|------|------|--------------|-----|-----|-----|----|----------------|
| 初值 | — | — | 0 | 0x000 | 0x000 | 0 | — |
| 1 | `LARK 0,0x05` | `reg_ar_ld=YES`（目标 `op[8]=0`） | 0 | **0x005** | 0x000 | 0 | 不用（立即数加载） |
| 2 | `LARK 1,0x80` | `reg_ar_ld=YES`（目标 `op[8]=1`） | 0 | 0x005 | **0x080** | 0 | 不用 |
| 3 | `LDPK 1` | `reg_dp_set=YES` | 0 | 0x005 | 0x080 | **1** | 不用 |
| 4 | `ADD` 间接+自增 | `inc=YES`（ARP=0，故改 AR0） | 0 | **0x006**（节拍末更新） | 0x080 | 1 | `AR0[7:0]=0x05`（用旧值） |
| 5 | `ADD` 直接 | 无 ARP/AR 副作用 | 0 | 0x006 | 0x080 | 1 | `{DP, op[6:0]}={1,0000010}=0x82` |

**关键观察**：

- 步骤 4 的间接寻址用 **ARP 选中的 AR0** 当地址，且**本拍用的是自增前的旧值** `0x05`；AR0 要等到 `cyc_ncen` 节拍末才变成 `0x006`。
- 步骤 5 的直接寻址地址最高位来自 **DP=1**，所以指向第 1 页的 `0x02` 单元，即 RAM 物理地址 `0x82`，而不是第 0 页的 `0x02`。
- 整个过程没有一条指令需要单独「设置地址」——地址的产生和增减都搭车在数据访问指令里完成，这正是辅助寄存器作为「专职地址寄存器」的价值。

> 待本地验证：把上述序列手编成机器码（对照 `docs` 里的 opcode table）烧进一个最小 testbench（参考 u1-l5 的 testbench 框架），在波形里核对 `reg_arp/reg_ar/reg_dp/ram_addr` 是否与上表一致。

## 6. 本讲小结

- **ARP（1 位）** 是「辅助寄存器指针」，选当前用 AR0 还是 AR1；**AR[0]/AR[1]（各 16 位）** 是真正的地址寄存器，二者构成两层结构。DP（1 位）是直接寻址时 RAM 地址的最高位。
- 三组寄存器都只在 **`cyc_ncen`（`cyclecntr==3`）节拍**更新，一个机器周期最多变一次。
- ARP 与 DP 用 **`{set, rst}` 真值表**（`10→1`、`01→0`、其余保持）改写；AR 用 **`{inc, dec}` 真值表**改写，且**自增自减只作用在低 9 位 `[8:0]`**，高 7 位只在 `reg_ar_ld` 整字加载时才变。
- AR 当地址用取**低 8 位 `[7:0]`**，第 8 位只有 `BANZ` 的判零逻辑能看到；RAM 实际只寻址 256 字。
- 几乎所有带数据操作数的指令共用一段「**间接寻址操作数译码片段**」，由指令字 **bit7（闸门）/ bit5（inc）/ bit4（dec）/ bit3（是否更新 ARP）/ bit0（新 ARP）** 驱动 ARP/AR 的副作用。
- 加载/存储 AR 的目标由**指令字 bit8** `if_opcodereg[8]` 决定（LAR/LARK/SAR），与 ARP 无关；这是「显式指定 AR」与「ARP 隐式选 AR」两套并存的选择机制。
- ⚠️ DP 的复位分支 [第 335 行](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L335) 写作 `if(i_RS_n)`（无取反），与 ARP/AR 块的 `if(!i_RS_n)` 极性相反，是一处需要在仿真中实测确认的细节。

## 7. 下一步学习建议

- **u2-l5（数据 RAM 与寻址方式）**：本讲的 `ram_addr` 正是 RAM 子模块的地址输入。下一讲会讲清 256×16 双口 RAM 的读写时序、`DMOV` 数据搬移，以及直接/间接寻址在 RAM 端的完整表现——和本讲无缝衔接。
- **u3-l4（控制类与辅助寄存器类指令译码）**：本讲只点到了 LAR/LARK/MAR/LARP/LDP/LDPK/SAR 的 ARP/AR/DP 副作用，专家层那一讲会完整剖析这些指令的微码全貌（包括 `LST`/`SSR` 对状态字各位的读写映射）。
- **建议同步阅读**：`docs` 目录下 TMS32010 用户手册的 **page 2-9（辅助寄存器算术）** 和 **page 3-16（BANZ）**，对照源码注释里给出的页码，把「手册描述 ↔ 源码实现」的对应关系亲手核对一遍。
