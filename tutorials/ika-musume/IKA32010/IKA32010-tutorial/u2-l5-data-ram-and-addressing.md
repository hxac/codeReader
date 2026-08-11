# 数据 RAM 与寻址方式

## 1. 本讲目标

本讲聚焦 IKA32010 片内**数据 RAM** 这条通路：它有多大、怎么读、怎么写、地址从哪里来。

学完后你应该能够：

- 说出 `IKA32010_ram` 子模块的容量、端口含义，以及「读端口寄存输出」的时序含义。
- 解释 `ram_addr` 这 8 位地址是如何由「直接寻址（DP+位移）」或「间接寻址（AR 低 8 位）」组合出来的，并能指出由指令字哪一位决定走哪条路。
- 看懂 `DMOV` 指令「把当前单元内容搬移到下一高地址」的特殊写时序，以及它如何与 `LTD` 这类复合指令共用。
- 自己跟踪一条读写指令在源码里的完整数据流向。

本讲不重复 ALU、乘法器、总线控制器的内部细节（那些分别属于 u2-l7、u2-l8、u2-l3），只关心数据是如何进出 RAM 的。

## 2. 前置知识

本讲承接 u2-l1（内部写总线 `reg_wrbus`）与 u2-l4（辅助寄存器 ARP/AR 与数据页指针 DP）。在进入正文前，请确认你已经掌握下面几个结论：

- **`reg_wrbus` 是全局数据汇流**：它有一个写入点（由 `register_wrbus_source_sel` 选源），多个读取点（PC 取低 12 位、AR、T 寄存器、RAM 写口、移位器 A、栈、`o_DOUT` 等）。各消费者靠自己的写使能决定何时采信总线，避免冲突。
- **ARP/AR/DP 是寻址寄存器**：ARP（1 位）选当前用哪个辅助寄存器 `AR0/AR1`；AR 当地址指针，其低 9 位可自增/自减；DP（1 位）是直接寻址时 RAM 地址的最高位。三组寄存器都只在 `cyc_ncen`（`cyclecntr==3`，机器周期第 4 拍）节拍更新。
- **水平微码风格**：顶层有一个大 `always @(*)` 块，先给所有控制信号赋默认值，再用 `casez(if_opcodereg)` 按指令覆盖。RAM 的默认值是「读 RAM、不写、不搬移」。
- **四分频时钟**：4 个 `i_EMUCLK` = 1 个机器周期；`cyc_ncen` 是核心工作拍，几乎所有寄存器更新都在这一拍发生。

一个关键直觉：IKA32010 **没有片内程序 ROM**（指令从外部总线读），但**有片内数据 RAM**。本讲研究的就是这一小块片内存储。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/IKA32010.sv` | 顶层模块。包含 `ram_addr` 生成（L488）、RAM 实例化（L491–494）、读写数据通路（移位器 A、ALU 端口 B、`reg_wrbus` MUX）、各指令译码中 `ram_wr`/`ram_dmov` 的设置，以及文件末尾的 `IKA32010_ram` 子模块（L1909–1940）。 |
| `src/IKA32010_mnemonics.sv` | 常量字典。`WRBUS_SOURCE_*` 等常量在这里定义，帮助理解读路径上各数据源的命名。 |
| `src/IKA32010_tb.v` | 唯一 testbench。实践任务会以它为基础做改造。 |

本讲涉及的核心源码点集中在两处：子模块本体（文件末尾），以及顶层的地址生成与数据通路（文件中部）。

## 4. 核心概念与源码讲解

### 4.1 IKA32010_ram 子模块：256×16 双口 RAM

#### 4.1.1 概念说明

`IKA32010_ram` 是 IKA32010 的片内数据存储器，规模为 **256 字 × 16 位**（共 512 字节）。它是一个**简单双口 RAM（simple dual-port RAM）**：

- 一个**读端口**：给出地址，下一拍得到数据。
- 一个**写端口**：给出地址 + 数据 + 写使能，下一拍写入。

「双口」并不意味着像 cache 那样的真双口，而是「读、写各有独立端口与时序」，可同时进行。注释里写的 `simple dual port RAM` 正是这个意思。这种结构能被 FPGA 综合工具直接映射到片上 BRAM 块（在 Altera/Intel 叫 M9K/M20K，在 Xilinx 叫 Block RAM），这是它能做到「FPGA proven」的一个细节。

#### 4.1.2 核心流程

RAM 子模块对外只看四个控制输入：`i_DMOV`（特殊搬移）、`i_WE`（普通写使能）、`i_ADDR`（8 位地址）、`i_DIN`（16 位写数据）。子模块内部先用三个组合式 `wire` 把这些输入「翻译」成真正的读写控制：

1. **读**：读地址恒等于输入地址 `ram_rdaddr = i_ADDR`；在每个 `i_EMUCLK` 上升沿把 `RAM[ram_rdaddr]` 锁存进 `ram_dout`，对外经 `o_DOUT` 输出。
2. **写**：写地址 `ram_wraddr`、写数据 `ram_din`、写使能 `ram_we` 三者都受 `i_DMOV` 影响：
   - 普通写：三者分别等于 `i_ADDR`、`i_DIN`、`i_WE`。
   - DMOV 搬移：`ram_we` 被强制为 1，`ram_wraddr` 变成「同一页内 +1」，`ram_din` 改用刚读出的 `ram_dout`（详见 4.3）。
3. **初始化**：`initial` 块把全部 256 个字清零。

数据流可以画成：

```text
   i_ADDR ──► (ram_rdaddr) ──► [ RAM 读端口 ] ──► ram_dout ──► o_DOUT
                                                                │
                                                                ▼ (DMOV 时回灌为写数据 ram_din)
  i_DIN ──────────────────────► [ RAM 写端口 ] ◄── ram_wraddr ◄── (DMOV? addr+1 : addr)
                                        ▲
                              ram_we ◄── (DMOV? 1 : i_WE)
```

**关于寄存输出时序**：读端口是「寄存输出」，即 `o_DOUT` 滞后 `i_ADDR` 一个 `i_EMUCLK`。由于一个机器周期有 4 个 `i_EMUCLK`，而指令译码（组合）发生在周期内、写回发生在 `cyc_ncen`，所以 `ram_dout` 在机器周期很早的阶段就已经稳定，本周期内即可被 ALU 使用。写端口则挂在 `i_EMUCLK` 上，理论上一个机器周期里会被写 4 次，但因为 `ram_wraddr`、`ram_din` 在整个周期内保持不变，这 4 次写是**幂等**的，净效果等于一次写入。

#### 4.1.3 源码精读

子模块完整定义在文件末尾：

[IKA32010_ram 子模块 — src/IKA32010.sv:1909-1940](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1909-L1940)

端口声明说明了它的全部接口：

```sv
module IKA32010_ram (
    input   wire            i_EMUCLK,
    input   wire            i_DMOV, //one-cycle special command
    input   wire            i_WE,
    input   wire    [7:0]   i_ADDR,
    input   wire    [15:0]  i_DIN,
    output  wire    [15:0]  o_DOUT
);
```

注意 `i_ADDR` 是 8 位，正好对应 256 字；`o_DOUT` 是 16 位，对应每字 16 位。

接下来的三行 `wire` 是 RAM 的「内部译码」，决定读写端口究竟在哪个地址、写什么、是否写：

```sv
wire            ram_we    = i_DMOV ? 1'b1 : i_WE;
wire    [7:0]   ram_rdaddr= i_ADDR;
wire    [7:0]   ram_wraddr= i_DMOV ? {i_ADDR[7], i_ADDR[6:0] + 7'd1} : i_ADDR;
reg     [15:0]  ram_dout;
wire    [15:0]  ram_din   = i_DMOV ? ram_dout : i_DIN;
```

- `ram_we`：DMOV 一旦触发就强制写，否则跟随 `i_WE`。
- `ram_wraddr`：DMOV 时把地址的低 7 位 +1，**保留最高位 `i_ADDR[7]`**——这保证搬移「不跨页」，始终在同一个 128 字页内进行。
- `ram_din`：DMOV 时写数据来自读端口的输出 `ram_dout`，于是「读到什么就写什么」；普通写则来自外部 `i_DIN`。

真正的存储体与两个读写 `always` 块：

```sv
//simple dual port RAM
reg     [15:0]  RAM[0:255];
always @(posedge i_EMUCLK) ram_dout <= RAM[ram_rdaddr];          // 读端口：寄存输出
always @(posedge i_EMUCLK) if(ram_we) RAM[ram_wraddr] <= ram_din; // 写端口
```

> 子模块只认 `i_EMUCLK`，**不认** `cyc_ncen`。这是它和顶层大多数寄存器（更新受 `cyc_ncen` 门控）的一个重要区别：RAM 读写每拍都在动，靠「整个周期信号不变」来保证结果正确。

**RAM 在顶层怎么被实例化、数据怎么进出**——这是读/写通路的总开关：

[RAM 实例化与地址生成 — src/IKA32010.sv:487-494](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L487-L494)

```sv
//RAM address source select(0 = direct, 1 = indirect)
wire    [7:0]   ram_addr = if_opcodereg[7] ? ar_addr_output : {reg_dp, if_opcodereg[6:0]};
reg             ram_dmov, ram_rd, ram_wr;

IKA32010_ram u_ram (
    .i_EMUCLK(i_EMUCLK),
    .i_DMOV(ram_dmov), .i_WE(ram_wr), .i_ADDR(ram_addr), .i_DIN(reg_wrbus), .o_DOUT(ram_output)
);
```

记住两个映射：

- **写数据** `i_DIN` 接的是内部写总线 `reg_wrbus`。所以凡是写 RAM 的指令，本质上都是「先把某个数据源选上 `reg_wrbus`，再让 `ram_wr=YES`」。
- **读数据** `o_DOUT` 接的是 `ram_output`，它流向移位器 A → ALU（算术读），也流向 `reg_wrbus`（当默认选源为 `WRBUS_SOURCE_RAM` 时）。

**读路径全链路**（RAM 数据如何流到 ALU 被运算）：

1. RAM 读出 → `ram_output`：[src/IKA32010.sv:128](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L128)
2. 默认选源把 `ram_output` 送上 `reg_wrbus`：`WRBUS_SOURCE_RAM : reg_wrbus = ram_output;` —— [写总线选源 MUX — src/IKA32010.sv:136](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L136)
3. 移位器 A 把 `reg_wrbus` 符号扩展到 32 位再左移：[src/IKA32010.sv:428-431](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L428-L431)
4. ALU 端口 B 在默认 `ALU_SOURCE_SHFT` 下取移位器 A 输出：`i_ALU_PB(alu_pbsel ? reg_p : sha_output)` —— [ALU 实例化 — src/IKA32010.sv:448-456](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L448-L456)

正是因为微码默认 `register_wrbus_source_sel = WRBUS_SOURCE_RAM`（见 4.2.3），绝大多数算术指令无需额外声明，就能自动把被寻址的 RAM 字送进 ALU。

#### 4.1.4 代码实践

**实践目标**：从源码层面跟踪一次「RAM 写」，确认写数据来源与写使能的来源。

**操作步骤**：

1. 打开 `src/IKA32010.sv`，定位到 SACL（低字存累加器）的译码：[src/IKA32010.sv:927-939](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L927-L939)。记下它设置了哪两件事：`register_wrbus_source_sel = WRBUS_SOURCE_SHB`（写总线取移位器 B 的输出，即累加器低 16 位）和 `ram_wr = YES`（写使能）。
2. 顺着 `reg_wrbus` 往 RAM 走：SACL 把 `shb_output` 选上总线 → 实例化处 `i_DIN(reg_wrbus)` → 子模块里 `ram_din = i_DIN`（非 DMOV 路径）→ `RAM[ram_wraddr] <= ram_din`。
3. 对比 SAR（存辅助寄存器）：[src/IKA32010.sv:1168-1171](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1168-L1171)，它的 `register_wrbus_source_sel = WRBUS_SOURCE_AR`，但同样 `ram_wr = YES`。

**需要观察的现象**：SACL 和 SAR 用的是**同一个 RAM 写端口**，差别仅在于「写总线选了哪个数据源」。这就是 `reg_wrbus` 作为汇流总线的价值——RAM 不必关心数据从哪儿来，只看 `i_DIN`。

**预期结果**：你能用一句话概括 RAM 写通路：「`<某数据源>` → `reg_wrbus` → `u_ram.i_DIN` → `RAM[ram_addr]`（受 `ram_wr` 门控）」。

> 待本地验证：可在仿真波形里观察一次 SACL 执行后 `main.u_ram.RAM[<地址>]` 的变化，确认写入值与 `shb_output` 一致。

#### 4.1.5 小练习与答案

**练习 1**：子模块的写端口 `always @(posedge i_EMUCLK) if(ram_we) RAM[ram_wraddr] <= ram_din;` 没有 `cyc_ncen` 门控，这意味着什么？为什么不会出错？

> **答案**：意味着每个 `i_EMUCLK` 上升沿只要 `ram_we` 为高就会写。一个机器周期有 4 个上升沿，所以会重复写 4 次。但因为译码产生的 `ram_wraddr`、`ram_din` 在整个机器周期内保持稳定，4 次写的是同一地址同一数据，互不冲突，净效果是一次有效写入。

**练习 2**：`ram_dout` 为什么声明成 `reg` 却是「连线」性质？

> **答案**：它在 `always @(posedge i_EMUCLK)` 里被赋值，是真正的时序寄存器（寄存输出）。它不是「组合连线」——这点和顶层的 `reg_wrbus`（声明为 `reg` 但由组合块驱动）不同。`ram_dout` 是地地道道的寄存器，承担「读端口寄存一拍」的作用。

---

### 4.2 ram_addr 生成逻辑：直接寻址与间接寻址

#### 4.2.1 概念说明

RAM 有 256 个字，需要 8 位地址 `ram_addr`。TMS32010 提供两种寻址方式来产生这 8 位地址，IKA32010 用一条 `wire` 一行代码就把两种方式合并了：

- **直接寻址（direct）**：地址 = `{DP, 指令字低 7 位位移}`。DP 是数据页指针（1 位），把 256 字 RAM 切成两页、每页 128 字；位移来自指令本身。直接寻址**只能定位到 DP 当前页内的 128 字**。
- **间接寻址（indirect）**：地址 = `AR[ARP]` 的低 8 位，其中 ARP 选当前辅助寄存器、AR 是地址指针寄存器。间接寻址**可达全部 256 字**，并且能在同一条指令里对 AR 自增/自减、改写 ARP。

由指令字的**第 7 位**决定走哪条路：`if_opcodereg[7]` 为 1 是间接，为 0 是直接（注释 `0 = direct, 1 = indirect`）。这一位对几乎所有带数据操作数的指令都适用。

#### 4.2.2 核心流程

`ram_addr` 的生成是一个**纯组合**的三元选择：

```text
              ┌── bit7 == 1 (indirect) ──► ar_addr_output = reg_ar[reg_arp][7:0]   ──┐
 ram_addr  ◄──┤                                                                       ├─► 8 位地址
              └── bit7 == 0 (direct)  ──► {reg_dp, if_opcodereg[6:0]}               ┘
```

注意两种方式生成的都是 8 位：

- 直接：`{1 位 DP, 7 位位移}` = 8 位。DP=0 → 0x00–0x7F；DP=1 → 0x80–0xFF。
- 间接：取 AR 的低 8 位 `[7:0]`（AR 本身 16 位，但寻址只用低 8 位；低 9 位中的第 8 位仅供 `BANZ` 判零可见，详见 u2-l4）。

**间接寻址的副作用**：当 `bit7=1` 时，指令还可以附带对 AR 的自增/自减和对 ARP 的改写。这部分由一段几乎逐字相同的「间接寻址操作数译码片段」驱动，它被复制在所有带数据操作数的指令译码里（ADD、LAC、SACL、DMOV、LTD、MPY……都有）。片段由指令字的 bit5/4/3/0 控制：

| 指令位 | 含义 | 作用 |
|--------|------|------|
| bit7 | 直接/间接 | 1=间接；该片段整体被它门控 |
| bit5 | AR 自增 | 作用在 `reg_ar[reg_arp]` 低 9 位 +1 |
| bit4 | AR 自减 | 作用在 `reg_ar[reg_arp]` 低 9 位 −1 |
| bit3 | 是否改写 ARP | 0=改写（按 bit0 设 ARP），1=不改写 |
| bit0 | 新 ARP 值 | 仅当 bit3=0 时生效 |

直接寻址（bit7=0）时，这段片段被整体跳过——既不改 AR，也不改 ARP，地址完全来自 DP 与位移。这正是「直接寻址是廉价寻址、间接寻址带指针运算」的硬件体现。

#### 4.2.3 源码精读

地址生成的一行核心代码：

[ram_addr 生成 — src/IKA32010.sv:487-488](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L487-L488)

```sv
//RAM address source select(0 = direct, 1 = indirect)
wire    [7:0]   ram_addr = if_opcodereg[7] ? ar_addr_output : {reg_dp, if_opcodereg[6:0]};
```

间接分支用到的 `ar_addr_output` 定义在辅助寄存器区段，取 ARP 当前指向的 AR 低 8 位：

[ar_addr_output — src/IKA32010.sv:299](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L299)

```sv
wire    [7:0]   ar_addr_output = reg_ar[reg_arp][7:0]; //use AR data as RAM address
```

直接分支用到的 `reg_dp` 是数据页指针，复位初值为 0，由 `LDP`/`LDPK` 指令改写：

[reg_dp 定义与更新 — src/IKA32010.sv:331-342](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L331-L342)

> 注意此处复位分支写为 `if(i_RS_n) reg_dp <= 1'b0;`，与 ARP/AR 块（`if(!i_RS_n)` 复位）极性相反。这看上去反直觉，**待本地验证**其复位行为是否符合预期——在仿真里对 `reg_dp` 复位值做一次确认是稳妥的做法（u2-l4 已提示过这一点）。

**间接寻址操作数译码片段**（以 ADD 为例，几乎所有数据指令都包含同样一段）：

[ADD 指令的间接寻址片段 — src/IKA32010.sv:791-797](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L791-L797)

```sv
if(if_opcodereg[7]) begin 
    reg_ar_inc = if_opcodereg[5]; reg_ar_dec = if_opcodereg[4]; 
    if(!if_opcodereg[3]) begin //AR register
        if(if_opcodereg[0]) reg_arp_set = YES;
        else                reg_arp_rst = YES;
    end
end
```

这段正是 4.2.2 表格里四条规则的直译：`bit7` 门控整段；`bit5/4` 决定 AR 增/减；`bit3=0` 时按 `bit0` 改写 ARP。它最终作用在 ARP/AR 的存储更新上（见 u2-l4 的 `case({reg_arp_set, reg_arp_rst})` 与 `case({reg_ar_inc, reg_ar_dec})`）。

**与默认值的关系**：在微码默认值区段，`register_wrbus_source_sel = WRBUS_SOURCE_RAM`：

[微码默认值 — src/IKA32010.sv:575-590](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L575-L590)

```sv
//RAM write
ram_wr = NO;
ram_dmov = NO;
...
//read source
register_wrbus_source_sel = WRBUS_SOURCE_RAM;
```

这两条默认值是理解整个 RAM 通路的关键：**默认状态下，RAM「只读不写」，读出的数据自动经 `reg_wrbus` 喂给移位器/ALU**。所以一条 ADD 指令什么都不用额外声明（除了 `alu_modesel`、`alu_acc_ld`、移位量），就能完成「读 RAM → 移位 → 加到累加器」，正是因为地址生成与读通路在默认值下已经接好。

#### 4.2.4 代码实践

**实践目标**：手工计算两条指令的 `ram_addr`，并据此预测写入地址。

**操作步骤**：

1. 假设当前 `reg_dp = 1`、`reg_arp = 0`、`reg_ar[0] = 0x0010`。
2. 指令 A：SACL **直接寻址**，指令字低字节位移 = `0x05`（即 `if_opcodereg[6:0] = 7'b0000101`）。
   - bit7 = 0 → 直接分支。
   - `ram_addr = {reg_dp, if_opcodereg[6:0]} = {1'b1, 7'd5} = 8'b1000_0101 = 0x85`。
3. 指令 B：SACL **间接寻址**，`*, AR0`（bit7=1，无增减、不改 ARP）。
   - bit7 = 1 → 间接分支。
   - `ram_addr = reg_ar[reg_arp][7:0] = reg_ar[0][7:0] = 0x10`。

**需要观察的现象**：同样一条「存累加器」指令，因 bit7 不同，写到了完全不同的地址（0x85 vs 0x10）。

**预期结果**：指令 A 写到 RAM[0x85]（DP=1 页内的第 5 个字），指令 B 写到 RAM[0x10]（AR0 指向的字）。

> 待本地验证：在仿真里施加上述指令字，检查 `main.ram_addr` 在两种情况下分别呈现 0x85 与 0x10。

#### 4.2.5 小练习与答案

**练习 1**：直接寻址为什么最多只能访问「当前页」128 字？

> **答案**：因为直接分支只用 7 位位移 `if_opcodereg[6:0]`（范围 0–127），最高位由 DP 提供。DP 不变时，地址被锁死在 `{DP, 7位位移}` 决定的那一页（0x00–0x7F 或 0x80–0xFF）。要换页必须用 `LDP`/`LDPK` 改 DP。

**练习 2**：间接寻址能访问全部 256 字吗？为什么它不受 DP 限制？

> **答案**：能。间接分支取 `reg_ar[reg_arp][7:0]`，完整的 8 位直接作为地址，不经过 DP 拼接，因此可覆盖 0x00–0xFF 全部 256 字。这是间接寻址比直接寻址更灵活的根本原因。

**练习 3**：一条 `ADD *, AR1`（间接、不改 AR、把 ARP 设为 1）的指令字低字节应该长什么样？

> **答案**：bit7=1（间接），bit5=0（不增），bit4=0（不减），bit3=0（要改 ARP），bit0=1（新 ARP=1），其余位（bit6/2/1）任意填 0。即 `[7:0] = 1_0_0_0_0_000 | bit0=1 = 8'b1000_0001 = 0x81`。

---

### 4.3 DMOV 数据搬移逻辑

#### 4.3.1 概念说明

`DMOV`（Data Move）是 TMS32010 的一条特色指令：把**当前寻址单元的内容复制到下一个更高地址**，即 `RAM[addr+1] ← RAM[addr]`。它存在的意义是高效实现 **FIR 滤波器抽头延迟线 / 移位寄存器**——把一串采样逐级向后挪一格，配合 `LTD` 指令在一个周期内完成「取采样 + 乘加 + 数据移位」。

DMOV 的特别之处在于：它**不是普通的「读-改-写」**，而是让 RAM 子模块的**读端口输出回灌成写端口输入**，从而一条指令、一次写就完成搬移，无需占用累加器或 `reg_wrbus`。

#### 4.3.2 核心流程

DMOV 全靠子模块内部那三根 `wire` 改写读写端口的语义（见 4.1.3）。当顶层把 `ram_dmov` 拉高（`i_DMOV=1`）时：

```text
  ram_we    = 1                        （强制写，无需 ram_wr）
  ram_wraddr = { i_ADDR[7], i_ADDR[6:0] + 1 }   （同页 +1）
  ram_din    = ram_dout                 （写数据 = 刚读出的当前单元内容）
```

综合起来就是：读端口照常读 `RAM[addr]`（结果进 `ram_dout`），写端口把 `ram_dout` 写到 `ram_wraddr = addr+1`（同页）。净效果 `RAM[addr+1] ← RAM[addr]`，地址 `addr` 仍由 4.2 的直接/间接寻址产生（所以 DMOV 也支持 `*, AR0+` 这类带自增的间接寻址，每拍搬一格、指针同时前移）。

数学上，一次 DMOV 的语义是：

\[
\text{RAM}\big[\,\text{addr}+1\,\big] \;\leftarrow\; \text{RAM}\big[\,\text{addr}\,\big]
\]

其中加法限制在 7 位内（保留 bit7），所以是「同页 +1」：

\[
\text{ram\_wraddr} = \{\,\text{addr}[7],\; (\text{addr}[6{:}0] + 1) \bmod 128\,\}
\]

#### 4.3.3 源码精读

DMOV 的「魔法」全部在子模块的三行译码里（已在 4.1.3 引用，这里聚焦 DMOV 含义）：

[IKA32010_ram 译码 wire — src/IKA32010.sv:1919-1923](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1919-L1923)

```sv
wire            ram_we = i_DMOV ? 1'b1 : i_WE;
wire    [7:0]   ram_rdaddr = i_ADDR;
wire    [7:0]   ram_wraddr = i_DMOV ? {i_ADDR[7], i_ADDR[6:0] + 7'd1} : i_ADDR;
wire    [15:0]  ram_din = i_DMOV ? ram_dout : i_DIN;
```

注意三个 `i_DMOV ?` 分支的协同：只有三者**同时**切换到 DMOV 语义，搬移才成立（强制写、写地址 +1、写数据取读输出）。这是典型的「用一组相关 MUX 表达一个特殊事务」。

顶层侧，DMOV 指令的译码极其简短——它几乎只做一件事：把 `ram_dmov` 拉高，其余交给默认值与间接寻址片段：

[DMOV 指令译码 — src/IKA32010.sv:1587-1602](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1587-L1602)

```sv
//DMOV - Copy contents of data memory location into next higher location
16'b0110_1001_????_????: begin
    ram_dmov = YES;

    if(if_opcodereg[7]) begin 
        reg_ar_inc = if_opcodereg[5]; reg_ar_dec = if_opcodereg[4]; 
        if(!if_opcodereg[3]) begin //AR register
            if(if_opcodereg[0]) reg_arp_set = YES;
            else                reg_arp_rst = YES;
        end
    end
    ...
end
```

注意 DMOV **没有显式设置 `ram_wr`**——它依赖子模块里 `ram_we = i_DMOV ? 1 : i_WE` 自动强制写。这是 DMOV 区别于 SACL/SAR（后者必须显式 `ram_wr = YES`）的关键。

**LTD 复用 DMOV**：`LTD` 一条指令同时完成 LT（加载 T 寄存器）+ APAC（P 寄存器加到累加器）+ DMOV（数据搬移），它的译码里同样出现 `ram_dmov = YES;`：

[LTD 指令译码（复用 DMOV） — src/IKA32010.sv:1514-1532](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1514-L1532)

```sv
//LTD - LTD combines LT, APAC, and DMOV into one instruction
16'b0110_1011_????_????: begin
    alu_pbsel = ALU_SOURCE_MUL; alu_modesel = ALU_ADD;
    alu_acc_ld = YES;
    reg_t_ld = YES;
    ram_dmov = YES;
    ...
end
```

这就是为什么 DMOV 被做成子模块级别的硬件特性——一旦 `ram_dmov` 能独立触发搬移，LTD 就能「免费」捎带一次数据移位，无需额外周期。这是 TMS32010 适合做实时信号处理的精髓所在。

> 关于「读端口寄存输出」的一个微妙点：`ram_din = ram_dout` 用的是 `ram_dout` 的**当前值**（上一拍锁存的读结果）。在一个机器周期内，由于 `ram_addr` 稳定、且搬移只写到 `addr+1`（不污染 `addr` 本身），经过最初一两个 `i_EMUCLK` 的对齐后，`ram_dout` 即等于 `RAM[addr]`，最终 `RAM[addr+1]` 被正确写成 `RAM[addr]`。逐拍对齐建议在仿真波形中确认。

#### 4.3.4 代码实践

**实践目标**：从源码确认 DMOV 与普通写在控制信号上的唯一差别。

**操作步骤**：

1. 打开 DMOV 译码（L1587–1602）和 SACL 译码（[L927–939](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L927-L939)）。
2. 列一张对比表，记录两条指令分别设置了什么：

   | 控制信号 | SACL | DMOV |
   |----------|------|------|
   | `ram_wr` | `YES` | 未设（默认 NO） |
   | `ram_dmov` | 未设（默认 NO） | `YES` |
   | `register_wrbus_source_sel` | `WRBUS_SOURCE_SHB` | 未设（默认 RAM） |
   | `alu_*` | 未设（默认 ADD/不写回 ACC） | 未设 |

3. 回到子模块：SACL 靠 `i_WE=1` 触发写、数据来自 `i_DIN`；DMOV 靠 `i_DMOV=1` 触发写、数据来自 `ram_dout`、地址 +1。

**需要观察的现象**：两条指令都最终把数据写进 RAM，但**触发写的信号不同、数据来源不同、目标地址不同**。

**预期结果**：你能总结出「普通写 = `ram_wr` + `reg_wrbus` 选源；DMOV = `ram_dmov` 触发回灌搬移，二者互不干扰」。

> 待本地验证：在仿真里执行一条 `DMOV *, AR0+`（`ram_addr` 由 AR0 给出），观察 `RAM[addr]` 与 `RAM[addr+1]` 的值，确认搬移成功且 AR0 自增。

#### 4.3.5 小练习与答案

**练习 1**：为什么 DMOV 的 `ram_wraddr` 要保留 `i_ADDR[7]` 而不是简单的 `i_ADDR + 1`？

> **答案**：保留 `i_ADDR[7]` 并只让低 7 位 +1，保证搬移在**同一个 128 字页内**进行，地址在页边界处回绕（例如 0x7F → 0x00 仍在 page 0，而不是越界到 0x80）。这对应 TMS32010 数据搬移「不跨页」的语义，也和直接寻址的页边界（DP 决定 bit7）保持一致。

**练习 2**：DMOV 指令译码里没有 `ram_wr = YES`，它靠什么把数据写进 RAM？

> **答案**：靠子模块内部的 `ram_we = i_DMOV ? 1'b1 : i_WE`。只要 `ram_dmov`（即 `i_DMOV`）为高，`ram_we` 就被强制为 1，无需 `ram_wr` 配合。这是 DMOV 作为「子模块级硬件特性」的体现。

**练习 3**：LTD 如何做到「一个周期完成三件事」？DMOV 在其中扮演什么角色？

> **答案**：LTD 在一个机器周期内同时设置：`reg_t_ld=YES`（锁存 T 寄存器，配合乘法器）、`alu_modesel=ALU_ADD; alu_acc_ld=YES; alu_pbsel=ALU_SOURCE_MUL`（把 P 寄存器加到累加器，即 APAC）、`ram_dmov=YES`（数据搬移）。其中数据搬移完全由 RAM 子模块自行完成，不占用 ALU 或 `reg_wrbus`，所以可以「免费」叠加在另外两件事上。

---

## 5. 综合实践：一段可跟踪的 RAM 读写小程序

把本讲三块内容（双口 RAM、两种寻址、DMOV）串起来，最好的方式是构造一段**最小可跟踪**的程序，分别用直接寻址、间接寻址写入 RAM，再用 DMOV 搬一格，最后在波形/反汇编里逐一核对。

**实践目标**：用一条数据通路串起「LACK 装入累加器 → SACL 直接寻址写 → SACL 间接寻址写 → DMOV 搬移」。

### 5.1 程序与指令字

下表的指令字是按源码 `casez` 模式手工编码的（每条都给出了对应的源码行，便于核对）。操作数域 `[7:0]`：bit7=直接/间接，bit5/4=AR 增/减，bit3=是否改 ARP，bit0=新 ARP。

| 步 | 指令（助记符） | 指令字 | 出处（casez 模式） | 作用 |
|----|----------------|--------|--------------------|------|
| 0 | `LACK 0x34` | `0x7E34` | [LACK L879-887](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L879-L887) | 累加器 ← 0x0034 |
| 1 | `LARK AR0, 0x10` | `0x7010` | [LARK L1109-1116](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1109-L1116) | AR0 ← 0x10（间接指针） |
| 2 | `LDPK 1` | `0x6E01` | [LDPK L1150-1157](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1150-L1157) | DP ← 1（选 page 1） |
| 3 | `SACL 5`（直接） | `0x5005` | [SACL L927-939](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L927-L939) | 写 RAM[ {1,0x05} = **0x85** ] ← 0x0034 |
| 4 | `SACL *, AR0`（间接） | `0x5088` | 同 SACL（bit7=1） | 写 RAM[ AR0[7:0] = **0x10** ] ← 0x0034 |
| 5 | `DMOV *, AR0`（间接） | `0x6988` | [DMOV L1587-1602](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1587-L1602) | RAM[**0x11**] ← RAM[0x10] |

> 编码自查：
> - `0x5088 = 0101_0000_1000_1000`：bit7=1（间接），bit5=0/​bit4=0（不增不减），bit3=1（不改 ARP），符合 4.2.3 的间接片段。
> - `0x6988` 同理，bit7=1 走间接，DMOV 靠 `ram_dmov` 触发搬移。

### 5.2 操作步骤（基于现有 testbench 改造）

1. 复制 `src/IKA32010_tb.v` 为一份实验用 tb。
2. 现有 tb 用 `$readmemh` 从外部 ROM 文件加载指令（路径是作者的 `D:/...`，在你环境里不存在）。把它替换成一个小型内置程序：声明 `reg [7:0] dsp_hi[0:2047]; reg [7:0] dsp_lo[0:2047];`，在 `initial` 里把上表的 6 个指令字按字节拆成 hi/lo 写入前 6 项，例如：
   ```verilog
   // 示例代码：手工加载最小程序（替换原 $readmemh）
   integer k;
   initial begin
       for(k=0;k<2048;k=k+1) begin dsp_hi[k]=8'h00; dsp_lo[k]=8'h00; end // 先清零（NOP-ish）
       // 地址 0: 0x7E34
       dsp_hi[0]=8'h7E; dsp_lo[0]=8'h34;
       // 地址 1: 0x7010
       dsp_hi[1]=8'h70; dsp_lo[1]=8'h10;
       // 地址 2: 0x6E01
       dsp_hi[2]=8'h6E; dsp_lo[2]=8'h01;
       // 地址 3: 0x5005
       dsp_hi[3]=8'h50; dsp_lo[3]=8'h05;
       // 地址 4: 0x5088
       dsp_hi[4]=8'h50; dsp_lo[4]=8'h88;
       // 地址 5: 0x6988
       dsp_hi[5]=8'h69; dsp_lo[5]=8'h88;
   end
   ```
3. 编译时定义宏 `IKA32010_DISASSEMBLY`（如 Icarus：`iverilog -DIKA32010_DISASSEMBLY ...`），这样每条指令执行时会通过 `disasm_type*` 打印 `PC=... | 助记符 操作数`，省去你逐字核对。
4. 用 `$dumpfile/$dumpvars` 或 GUI 工具把波形导出，重点观察以下信号（层级名以 DUT 实例 `main` 为前缀）：
   - `main.if_opcodereg`、`main.if_pc`：当前指令与 PC。
   - `main.ram_addr`：本讲核心，看它是否如 5.1 预期变化。
   - `main.reg_dp`、`main.reg_arp`、`main.reg_ar[0]`、`main.reg_ar[1]`。
   - `main.u_ram.RAM[0x10]`、`main.u_ram.RAM[0x11]`、`main.u_ram.RAM[0x85]`：看写入与搬移结果。
   - `main.ram_dmov`、`main.ram_wr`：看 DMOV 与普通写的差别。

### 5.3 需要观察的现象与预期结果

| 时刻（执行完哪步） | `ram_addr` 期望值 | RAM 期望变化 |
|--------------------|-------------------|--------------|
| 步 3（SACL 直接） | `0x85` | `RAM[0x85] = 0x0034` |
| 步 4（SACL 间接） | `0x10` | `RAM[0x10] = 0x0034` |
| 步 5（DMOV 间接） | `0x10`（读）/ `0x11`（写） | `RAM[0x11] = 0x0034`，`RAM[0x10]` 保持 |

执行步 3 与步 4 时，`ram_addr` 应分别为 `0x85` 与 `0x10`，这正是 4.2.4 手算的两个值。执行步 5 后，`RAM[0x11]` 应等于 `RAM[0x10]`（都是 `0x0034`），验证 DMOV 搬移正确。

> 待本地验证：本实践涉及完整仿真（需要 Verilog 仿真器与正确的指令编码命中 `casez` 分支），具体波形以本地运行为准。若某条指令字意外命中更高优先级的 `casez` 分支，反汇编打印会立刻暴露（助记符对不上），据此修正即可。

## 6. 本讲小结

- `IKA32010_ram` 是 **256×16 的简单双口 RAM**：读端口寄存输出（`o_DOUT` 滞后 `i_ADDR` 一拍），写端口受 `i_WE`/`i_DMOV` 控制；子模块只认 `i_EMUCLK`，靠「整周期信号稳定」保证写入正确。
- **`ram_addr` 由指令字 bit7 二选一**：bit7=0 直接寻址 `{DP, 位移[6:0]}`（限当前 128 字页），bit7=1 间接寻址 `AR[ARP][7:0]`（可达全 256 字）。
- **读通路默认接好**：默认 `register_wrbus_source_sel = WRBUS_SOURCE_RAM`，所以 RAM 读出的数据自动经 `reg_wrbus` → 移位器 A → ALU 端口 B，算术指令无需额外声明。
- **写通路靠 `reg_wrbus` 选源 + `ram_wr` 门控**：SACL/SACH 取累加器，SAR 取 AR，SSR 取标志位，IN 取外部输入锁存——共用同一写端口。
- **DMOV 是子模块级硬件特性**：`ram_dmov=1` 时强制写、写地址同页 +1、写数据取读端口输出，实现 `RAM[addr+1] ← RAM[addr]`，并被 `LTD` 复用以在一个周期内完成「取数+乘加+移位」。
- 间接寻址的 AR 自增/自减与 ARP 改写由一段**几乎逐字复制**的操作数译码片段驱动，受指令字 bit5/4/3/0 控制。

## 7. 下一步学习建议

- **向下游看乘法器**：本讲的 DMOV/LTD 已经引出了 T 寄存器、P 寄存器与乘法器的交互。下一讲 **u2-l8（16×16 乘法器与 T/P 寄存器）** 会讲清 `reg_t_ld`、`mul_en`、`mul_op1_source_sel` 如何把 RAM 里的数据变成一次有符号乘法，并解释 LTD 的另外两件事（LT、APAC）。
- **向 ALU 看数据终点**：本讲只跟踪到「读 RAM → 移位器 A → ALU 端口 B」，至于 ALU 怎么做加减/逻辑、怎么写回累加器、怎么更新 Z/N/V 标志，见 **u2-l7（ALU、移位器、累加器与标志位）**。
- **想看写 RAM 的指令全景**：进入专家层后，**u3-l5（累加器算术逻辑类指令译码）** 会系统梳理 ADD/LAC/SACH/SACL 等指令如何同时驱动 ALU、移位器与 `ram_wr`。
- **建议顺带阅读**：`docs/` 下 TMS32010 用户手册关于「Direct/Indirect Addressing」与「DMOV/LTD」的章节，把本讲的源码实现对照回官方语义，尤其确认 `reg_dp` 复位极性（4.2.3 提到的 `if(i_RS_n)`）这一待验证点。
