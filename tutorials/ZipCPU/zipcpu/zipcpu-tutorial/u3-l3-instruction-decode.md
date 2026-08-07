# 指令译码 idecode

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `idecode` 模块在 ZipCPU 五级流水线里的位置，以及它输入一个 32 位指令字后输出哪些控制信号。
- 读懂译码器如何从指令字中识别**操作码**、**指令类别**（ALU/访存/乘除/浮点/特殊）、**寄存器号**、**立即数**、**条件码**。
- 手工把一条 32 位指令字「喂」给译码逻辑，预测出 `o_op`、`o_cond`、`o_I`、`o_dcdR` 等输出的值。
- 理解压缩指令（CIS）为何要分两个时钟周期（两阶段）译码，以及 `o_phase` 的作用。
- 对照 RTL 译码器与软件反汇编表 `zopcodes.cpp`，理解「硬件译码」与「软件编码表」是同一份 ISA 的两种表达。

---

## 2. 前置知识

本讲默认你已经掌握：

- **指令格式与字段划分**（见 u2-l2）：32 位定长指令被切成 保留位 / DR / OpCode / Cnd / OpB 选择位 / BR / 立即数 七个字段，DR 同时充当源操作数 A。
- **流水线与信号前缀**（见 u3-l1）：五级流水线 `取指→译码→读操作数→执行+访存→写回`，对应 `pf_`/`dcd_`/`op_`/`alu_`/`mem_`/`wr_` 前缀；`OPT_*` 参数是「综合期剪刀」。
- **条件执行**（见 u2-l4）：3 位条件码字段 `Cnd`（指令位 21:19）查 CC 标志位决定是否写回。

一个关键提醒：在 u3-l1 里我们说过，`zipcore` 只在内部实例化 `idecode`、`cpuops`、`div` 三个子模块，**取指缓存和访存控制器并不在内核内**。本讲聚焦的就是这三个子模块里的第一个——**译码器 `idecode`**。它处在「取指」与「读操作数」之间，是流水线第 2 级（`dcd_` 前缀）的核心。

> 术语速查：**译码（decode）**＝把二进制指令字翻译成一堆控制信号；**控制信号**＝告诉后续各级「做什么运算、读哪个寄存器、写哪个寄存器、要不要改标志」的单比特或多比特连线。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/core/idecode.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v) | **本讲主角**。纯组合 + 寄存器输出的译码器，把 32 位指令字解析为各路控制信号。功能逻辑集中在文件前 ~900 行；第 924 行起 `\ifdef FORMAL` 段是形式化验证用的断言，不属于功能逻辑（留给 u5-l2）。 |
| [rtl/core/zipcore.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v) | 实例化 `idecode` 的地方，第 649 行起。从这里能看到译码输出如何被改名成 `dcd_*` 信号供后续级使用。 |
| [sw/zasm/zopcodes.cpp](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp) | 软件侧的「操作码表」与反汇编器，是 RTL 译码逻辑的镜像。本讲用它对照「同一条指令在硬件和软件里分别怎么被识别」。 |
| [sw/zasm/zopcodes.h](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.h) | 定义 `ZOPCODE` 结构与 `ZIP_REGFIELD`/`ZIP_IMMFIELD`/`ZIP_BITFIELD` 字段描述宏（u2-l2 已介绍，本讲复用）。 |
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | ISA 规范。本讲引用 Instruction Format（635 行）、Instruction OpCodes（692 行）、Compressed Instructions（977 行）三节作为权威。 |

---

## 4. 核心概念与源码讲解

### 4.1 译码器的位置与输入输出端口

#### 4.1.1 概念说明

译码器本质上是一个「翻译机」：输入是取指级送来的一个 32 位指令字（以及一些上下文，如当前是否处于用户模式、当前 PC），输出是一大把控制信号。后续的「读操作数」级根据 `o_dcdR/o_dcdA/o_dcdB` 决定读哪些寄存器；「执行」级根据 `o_op/o_ALU/o_M/o_DV/o_FP` 决定把指令送到 ALU、访存单元、除法器还是浮点单元；「写回」级根据 `o_wR` 决定是否把结果写回寄存器堆。

可以把译码器想象成一张「指令身份证扫描仪」：扫一下 32 位条码，吐出一张明细单（这一堆 `o_*` 信号）。

#### 4.1.2 核心流程

```
i_instruction (32位) ──┐
i_gie (当前模式)     ──┤
i_pc (当前PC)        ──┼──▶  idecode  ──▶  控制信号集合 o_*
i_pf_valid (指令有效)──┤                  (o_op, o_cond, o_I,
i_illegal (取指出错)──┘                   o_dcdR/A/B, o_ALU, o_M, ...)
```

译码是**单周期**完成的：组合逻辑算出一堆 `w_*` 中间信号，在时钟上升沿（`i_ce` 有效时）把它们锁存成稳态的 `o_*` 输出。也就是说，`w_*` 是「这一拍正在算的中间值」，`o_*` 是「锁存后给下游用的结果」。

#### 4.1.3 源码精读

先看模块端口，了解译码器到底输出哪些信号。[rtl/core/idecode.v:40-90](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L40-L90) 给出了完整的参数与端口列表。最关键的输出有：

- `o_op [3:0]`：4 位精简操作码（注意不是 5 位，原因见 4.2）。
- `o_cond [3:0]`：4 位条件码（最高位为 1 表示「无条件执行」）。
- `o_I [31:0]`：符号扩展到 32 位的立即数。（规范里把它叫「立即数输出」，信号名是 `o_I`，没有单独的 `o_imm`。）
- `o_dcdR / o_dcdA / o_dcdB [6:0]`：目的寄存器、源操作数 A 寄存器、源操作数 B 寄存器，各 7 位（含「是不是 PC/CC」和「寄存器组」标志，见 4.3）。
- `o_ALU / o_M / o_DV / o_FP`：四个单比特信号，指示指令该送往哪个执行单元。
- `o_wR / o_rA / o_rB`：是否写回结果、是否读 A、是否读 B。
- `o_wF`：是否更新条件标志（CC）。
- `o_phase`：CIS 压缩指令的「半条指令」标志（见 4.5）。
- `o_illegal`：是否非法指令。

几个关键常量定义在 [rtl/core/idecode.v:94-99](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L94-L99)：

```verilog
localparam   CISBIT    = 31,    // 指令字最高位：1 表示这是 CIS 压缩指令
                CISIMMSEL = 23,    // CIS 内部「立即数/寄存器」选择位
                IMMSEL    = 18;    // 标准指令的 OpB 选择位（位 18）
```

> 小贴士：`IMMSEL = 18` 正是 u2-l2 讲过的 OpB 选择位——为 0 时 Operand B 是 18 位立即数，为 1 时是「BR + 14 位偏移」。译码器到处用 `iword[IMMSEL]` 也就是 `iword[18]` 来判断这一点。

再看 `zipcore` 怎么连这些端口：[rtl/core/zipcore.v:649-695](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L649-L695)。例如 `o_op` 被改名成 `dcd_opn`、`o_cond` 改名成 `dcd_F`、`o_I` 改名成 `dcd_I`，这正是 u3-l1 提到的 `dcd_` 前缀信号的来源。

#### 4.1.4 代码实践

1. **目标**：建立「译码器输出 = 下游 `dcd_*` 信号」的对应关系。
2. **步骤**：
   - 打开 [rtl/core/zipcore.v:665-688](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L665-L688)，逐行对照 `idecode` 的端口连接。
   - 建一张表：左列写 `o_*` 端口名，右列写它在 `zipcore` 里被连接到的 `dcd_*` 信号名。
3. **观察**：注意 `o_dcdR` 连到 `dcd_full_R`（7 位），随后在 [zipcore.v:697](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L697) 被拆成 `{ dcd_Rcc, dcd_Rpc, dcd_R }`——这正是 4.3 要讲的「7 位寄存器号 = 两个标志位 + 5 位地址」。
4. **预期结果**：你会发现 `o_op → dcd_opn`、`o_cond → dcd_F`、`o_I → dcd_I`、`o_ALU → dcd_ALU` 等一一对应。

#### 4.1.5 小练习与答案

**练习**：译码器输出的 `o_op` 是 4 位，而 ISA 的操作码字段是 5 位。为什么译码后反而变「窄」了？

**参考答案**：因为其余的区分信息已经由其它单比特信号承担了。例如 5 位操作码 `5'h1a–5'h1f` 都属浮点，但译码器同时输出 `o_FP=1`，于是 `o_FP` 加上 `o_op` 的低 4 位就足以唯一确定一条浮点指令；除法同理用 `o_DV` 区分。源码注释在 [idecode.v:582-587](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L582-L587) 明确说明了这一点。

---

### 4.2 操作码识别与指令分类（含 zopcodes.cpp 操作码表对照）

#### 4.2.1 概念说明

拿到 32 位指令字后，译码器做的第一件事是「抠出 5 位操作码」，然后判断它属于哪一大类。ZipCPU 把所有指令分成几个互斥的类别：**ALU 运算**、**比较/测试（CMP/TST）**、**访存（LW/SW/LH/SH/LB/SB）**、**乘法**、**除法**、**浮点**、**装入立即数（LDI）**、**传送（MOV）**、以及 **特殊指令（BREAK/LOCK/SIM/NOOP）**。

类别判断（`w_ALU/w_mem/w_mpy/w_div/w_fpu/w_ldi/w_mov/w_cmptst/w_special`）是后续一切控制信号的基础。值得注意的是，硬件译码器（`idecode.v`）和软件反汇编表（`zopcodes.cpp`）做的是同一件事——把操作码映射到指令——但用了完全不同的风格：硬件用「位切片 + 比较器」，软件用「掩码 + 匹配值表」。

#### 4.2.2 核心流程

硬件侧的识别分两步：

1. 从指令字抠出 5 位原始操作码 `w_op = iword[26:22]`。
2. 若是 CIS 压缩指令，先把 3 位 CIS 操作码 `iword[26:24]` 经一张映射表「升级」成等价的 5 位操作码 `w_cis_op`；否则 `w_cis_op = w_op`。后续统一用 `w_cis_op` 做分类。

分类用的全是 `w_cis_op` 的位模式比较，例如「`w_cis_op[4:1]==4'h8` 就是 CMP/TST」。

软件侧（`zopcodes.cpp`）则把每条指令描述成一行 `{助记符, 掩码, 匹配值, ...}`，匹配规则是经典的「`(指令字 & 掩码) == 匹配值`」。

#### 4.2.3 源码精读

**硬件：抠操作码 + CIS 升级映射**。[rtl/core/idecode.v:202-204](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L202-L204) 先抠出原始操作码：

```verilog
assign  w_op = iword[26:22];   // 5 位操作码，标准指令专用
```

CIS 的 3 位操作码「升级」成 5 位发生在 [idecode.v:180-192](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L180-L192)，这张表和 spec 的 CIS OpCodes 表（[spec.tex:1011-1024](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1011-L1024)）一一对应：

```verilog
if (!iword[CISBIT])
    w_cis_op = iword[26:22];          // 非压缩：直接用 5 位操作码
else case(iword[26:24])               // 压缩：3 位映射成 5 位
3'h0: w_cis_op = 5'h00;   // SUB
3'h1: w_cis_op = 5'h01;   // AND
3'h2: w_cis_op = 5'h02;   // ADD
3'h3: w_cis_op = 5'h10;   // CMP   ← 注意：CIS 用 3'h3，标准用 5'h10
3'h4: w_cis_op = 5'h12;   // LW
3'h5: w_cis_op = 5'h13;   // SW
3'h6: w_cis_op = 5'h18;   // LDI
3'h7: w_cis_op = 5'h0d;   // MOV
endcase
```

**硬件：按位模式分类**。[idecode.v:205-228](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L205-L228) 给出全部类别信号，几个关键的：

```verilog
assign  w_mov    = (w_cis_op      == 5'h0d);          // MOV
assign  w_ldi    = (w_cis_op[4:1] == 4'hc);           // LDI（5'h18/5'h19）
assign  w_mpy    = (w_cis_op[4:1] == 4'h5)||(w_cis_op[4:0]==5'h0c); // 三种乘法
assign  w_cmptst = (w_cis_op[4:1] == 4'h8);           // CMP(5'h10)/TST(5'h11)
assign  w_ALU    = (!w_cis_op[4])                     // 高位为 0 ...
                &&(w_cis_op[3:1] != 3'h7);            // ... 且不是除法(5'h0e/5'h0f)
assign  w_mem    = (w_cis_op[4:3] == 2'b10)&&(w_cis_op[2:1] !=2'b00); // 访存
```

> 读位模式的小窍门：`w_cis_op[4:1]==4'h8` 即 4'b1000，意味着 5 位值的形状是 `1_000_x`，也就是 `5'h10`（CMP）或 `5'h11`（TST）。`w_ldi` 用 `w_cis_op[4:1]==4'hc`（4'b1100，形状 `1_100_x`）覆盖 `5'h18` 和 `5'h19` 两个 LDI 编码。

**软件：掩码表对照**。同样这条 SUB，软件侧 [zopcodes.cpp:132-133](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L132-L133) 用两行描述（立即数形式 / 寄存器形式）：

```c
{ "SUB", 0x87c40000, 0x00000000, ZIP_REGFIELD(27), ZIP_REGFIELD(27), ZIP_OPUNUSED, ZIP_IMMFIELD(18,0), ZIP_BITFIELD(3,19) },
{ "SUB", 0x87c40000, 0x00040000, ZIP_REGFIELD(27), ZIP_REGFIELD(27), ZIP_REGFIELD(14), ZIP_IMMFIELD(14,0), ZIP_BITFIELD(3,19) },
```

第二行的匹配值 `0x00040000` 正是「位 18 = 1」（寄存器形式）。匹配逻辑在 [zopcodes.cpp:571](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L571)：`(ins & listp[i].s_mask) == listp[i].s_val`。把 0x08048000 套进第二行：`0x08048000 & 0x87c40000 = 0x00040000 == 0x00040000` ✓，命中 SUB 寄存器形式。这与 u2-l2 给出的 `SUB R1,R2 = 0x08048000` 完全自洽。

操作码编号全集见 spec 的 OpCodes 表 [spec.tex:696-738](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L696-L738)。

#### 4.2.4 代码实践

1. **目标**：用一条已知指令验证硬件操作码识别。
2. **步骤**：取 u2-l2 的 `SUB R1,R2 = 0x08048000`。在纸上算：`iword[31]=0`（非 CIS）→ `w_op = iword[26:22]`。
   - 把 0x08048000 展开二进制：`0000 1000 0000 0100 1000 0000 0000 0000`。
   - 数出位 26:22 = `0 0000` → `w_op = 5'h00`（SUB）。
   - 因为非 CIS，`w_cis_op = w_op = 5'h00`；查分类：`w_ALU=1`，其它 `w_mem/w_ldi/w_mov/...=0`。
3. **预期结果**：`w_cis_op = 5'h00`，类别 = ALU。这条指令最终 `o_op = w_cis_op[3:0] = 4'h0`（见 4.4）。
4. **待本地验证**：若你装好了 Verilator，可在 `bench/formal` 的 `IDECODE` 模式下用形式化断言核对（[idecode.v:1071-1098](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L1071-L1098) 对 SUB/AND/ADD 类有一整段 `ASSERT`）。

#### 4.2.5 小练习与答案

**练习 1**：`w_mpy` 写成 `(w_cis_op[4:1]==4'h5)||(w_cis_op[4:0]==5'h0c)`，覆盖了哪几个操作码？为什么不全用 `[4:0]==` 形式？

**参考答案**：`[4:1]==4'h5`（形状 `0_101_x`）覆盖 `5'h0a`（MPYUHI）和 `5'h0b`（MPYSHI）；再加 `5'h0c`（MPY）。前两个的高位模式相同，用 `[4:1]` 一次覆盖更省逻辑门。

**练习 2**：在 `zopcodes.cpp` 里，LDI 只有「立即数形式」却为何在 [zopcodes.cpp:207](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L207) 用 `ZIP_IMMFIELD(23,0)`？

**参考答案**：LDI 没有 OpB 选择位、没有条件码，整个低 23 位都是立即数（见 spec 的 LDI 格式 [spec.tex:660-662](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L660-L662)），所以立即数字段从位 0 开始、长 23 位。

---

### 4.3 寄存器号、操作数 B 与立即数提取

#### 4.3.1 概念说明

知道了「做什么运算」还不够，还得知道「用哪些数据」。译码器要从指令字里抠出三件事：

- **目的寄存器 DR**（同时也是源操作数 A 的寄存器号）；
- **源操作数 B**：要么是一个「寄存器 + 偏移」，要么是一个纯立即数；
- **立即数的具体数值**（以及它有多少位、从哪些位抠）。

ZipCPU 有一个贯穿全程的简化：**A 寄存器永远等于目的寄存器**（因为 DR 同时是源 A）。所以 `o_dcdA` 永远等于 `o_dcdR`，形式化段甚至直接断言 `o_dcdR == o_dcdA`（[idecode.v:2045-2046](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L2045-L2046)）。

另一个关键点：寄存器号不是 4 位，而是 **7 位**。多出来的 3 位分别标记「这套寄存器属于用户组还是监管组」「是不是 PC」「是不是 CC」——这源于 u2-l1 讲过的双寄存器组（GIE 位兼作寄存器地址的第 5 位）。

#### 4.3.2 核心流程

```
标准指令字
├─ DR  = iword[30:27]  ──▶  w_dcdR[3:0]
├─ BR  = iword[17:14]  ──▶  w_dcdB[3:0]   (当 iword[18]=1，寄存器形式)
├─ 立即数来源选择 w_immsrc:
│    LDI      → iword[22:0]      (23 位)
│    MOV      → iword[12:0]      (13 位，符号扩展)
│    iword[18]=0 → iword[17:0]   (18 位，符号扩展)
│    iword[18]=1 → iword[13:0]   (14 位，符号扩展)
└─ 寄存器组位 = i_gie（MOV 在监管态下可改写以访问对方组）
最终：o_dcdR = { 是否CC, 是否PC, 寄存器组位, 4位寄存器号 }
```

#### 4.3.3 源码精读

**目的寄存器 w_dcdR（5 位）** 在 [idecode.v:243-244](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L243-L244)：

```verilog
assign  w_dcdR = { ((!iword[CISBIT])&&(OPT_USERMODE)&&(w_mov)&&(!i_gie))
                      ? iword[IMMSEL] : i_gie,    // 寄存器组位（第 5 位）
                  iword[30:27] };                  // 4 位寄存器号
```

正常情况下第 5 位就是 `i_gie`（当前模式决定读 user 组还是 supervisor 组）。唯一例外是 MOV 在监管模式下可以「跨组」访问用户寄存器——这时用 `iword[18]`（MOV 的 A 位）决定目的属于哪一组。这正是 u2-l3 讲过的「MOV 借 A/B 位跨组搬运」。

A 恒等于 R，并且额外算出两个标志位 [idecode.v:247-253](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L247-L253)：

```verilog
assign  w_dcdA = w_dcdR;                              // A 就是目的寄存器
assign  w_dcdR_pc = (w_dcdR == {i_gie, CPU_PC_REG});   // 结果要写 PC 吗？
assign  w_dcdR_cc = (w_dcdR == {i_gie, CPU_CC_REG});   // 结果要写 CC 吗？
```

其中 `CPU_PC_REG = 4'hf`、`CPU_CC_REG = 4'he`、`CPU_SP_REG = 4'hd`（[idecode.v:94-96](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L94-L96)）。这两个标志位非常重要：写 PC 就是跳转，写 CC 会改控制状态，下游各级对它们要特殊处理。

最终锁存成 7 位输出在 [idecode.v:593-595](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L593-L595)：`o_dcdR <= { w_dcdR_cc, w_dcdR_pc, w_dcdR }`（1+1+5=7 位）。

**立即数提取** 分两步：先用 [idecode.v:332-340](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L332-L340) 选来源 `w_immsrc`，再用 [idecode.v:345-351](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L345-L351) 按来源抠位并符号扩展到 23 位：

```verilog
case(w_immsrc)
2'b00: w_fullI = { iword[22:0] };                              // LDI：23 位
2'b01: w_fullI = { {(23-13){iword[12]}}, iword[12:0] };        // MOV：13 位
2'b10: w_fullI = { {(23-18){iword[17]}}, iword[17:0] };        // 纯立即数：18 位
2'b11: w_fullI = { {(23-14){iword[13]}}, iword[13:0] };        // 寄存器+偏移：14 位
endcase
```

`{(23-N){iword[符号位]}}` 是 Verilog 符号扩展的惯用写法：把最高位复制填充到 23 位宽。最后 [idecode.v:903](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L903) 再把 23 位 `r_I` 符号扩展到 32 位对外输出：

```verilog
assign  o_I = { {(32-22){r_I[22]}}, r_I[21:0] };
```

> 关于规范里提到的 `o_imm`：本模块**没有**叫 `o_imm` 的端口，立即数输出的真实信号名是 `o_I`（外加一个「立即数是否为 0」的辅助位 `o_zI`，[idecode.v:71](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L71)）。`o_zI` 用来让下游快速判断「这个立即数是不是 0」，例如 `LW (PC),PC` 这种立即数为 0 的特殊跳转。

#### 4.3.4 代码实践

1. **目标**：手工提取一条带立即数指令的 `o_I`。
2. **步骤**：构造 `ADD R3,#5`（立即数形式，无条件）。
   - 字段：DR=R3=`0011`，op=ADD=`00010`，cond=`000`，位 18=0（立即数），低 18 位=5。
   - 拼出指令字：位 31=0；30:27=0011；26:22=00010；21:19=000；18=0；17:0=0x00005。
   - 按字节算：`0x18880005`（你应当自己复核一遍）。
3. **走译码**：
   - `w_immsrc`：非 LDI、非 MOV、`iword[18]=0` → `w_immsrc=2`（18 位立即数）。
   - `w_fullI = 符号扩展(iword[17:0]=5) = 5`。
   - `o_I = 符号扩展到 32 位 = 0x00000005`。
4. **预期结果**：`o_I = 0x00000005`，`o_dcdR` 的低 4 位 = `0011`（R3）。
5. **待本地验证**：在 Verilator 仿真里把 `0x18880005` 作为指令注入，观察 `dcd_I` 信号是否等于 5。

#### 4.3.5 小练习与答案

**练习**：同一条 `ADD R3,R2,#5`（寄存器 + 偏移形式）和 `ADD R3,#5`（纯立即数形式），它们的 `w_immsrc` 分别是多少？立即数字段分别从指令字的哪些位取？

**参考答案**：前者位 18=1 → `w_immsrc=3`（14 位），取 `iword[13:0]=5`；后者位 18=0 → `w_immsrc=2`（18 位），取 `iword[17:0]=5`。两者 `o_I` 数值都是 5，但来源字段宽度不同——寄存器形式因为要腾出 4 位给 BR，立即数只剩 14 位。

---

### 4.4 条件码、写回、标志与执行单元选择

#### 4.4.1 概念说明

操作码和数据来源都就位后，译码器还要回答四个问题：

1. **什么条件下执行？**（条件码 `o_cond`）
2. **结果要不要写回寄存器堆？**（`o_wR`）
3. **要不要更新条件标志 CC？**（`o_wF`）
4. **送到哪个执行单元？**（`o_ALU/o_M/o_DV/o_FP`）

这里有一个设计上的「魔法」：译码器会把一些指令**改写**成等价的更基础指令。最典型的是 **LDI 被当成 MOV 走 ALU**、**CMP 被当成 SUB**、**TST 被当成 AND**。这样执行单元就不用为 LDI/CMP/TST 单独做数据通路——复用 ALU 即可。

#### 4.4.2 核心流程

- **条件码**：3 位 `Cnd` 字段（指令位 21:19）映射成 4 位 `o_cond`，最高位（bit 3）为 1 表示「无条件」。LDI、特殊指令、CIS 指令强制为无条件 `4'h8`。
- **写回** `o_wR`：除了「存储指令（不写回，只往外写内存）」「比较/测试（只设标志不写回）」「特殊指令」之外，都要写回。
- **标志** `o_wF`：只有「无条件执行的 ALU 运算」或「CMP/TST」才更新标志——这正对应 spec 里「条件指令不改标志（CMP/TST 除外）」的规则。
- **执行单元**：`o_ALU/o_M/o_DV/o_FP` 四选一（加上 `o_lock/o_break` 等附加位）。

#### 4.4.3 源码精读

**条件码** [idecode.v:276-277](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L276-L277)：

```verilog
assign  w_cond = ((w_ldi)||(w_special)||(iword[CISBIT])) ? 4'h8 :
                { (iword[21:19]==3'h0), iword[21:19] };
```

解读：最高位 `(iword[21:19]==3'h0)` 即「条件字段为 000 = 无条件」时置 1；低 3 位原样保留条件编号。所以 `4'h8 = 4'b1000` 表示无条件，`4'b0xx` 表示有条件。这与 spec 条件表 [spec.tex:753-766](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L753-L766) 一致：`3'h0`=None（总是执行）、`3'h1`=.Z、`3'h2`=.LT……`3'h7`=.NC。下游执行级用 `o_cond` 的低 3 位去查 CC 的 Z/N/C/V 标志，决定是否真正写回。

**写回与标志** [idecode.v:311-323](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L311-L323)：

```verilog
assign  w_wR_n = (w_sto)||(w_special)||(w_cmptst);   // 这三类不写回
assign  w_wR    = !w_wR_n;
assign  w_wF    = (w_cmptst)                          // CMP/TST 必设标志
        ||((w_cond[3])&&(w_fpu||w_div
            ||((w_ALU)&&(!w_mov)&&(!w_ldilo)&&(!w_brev)
                &&(w_dcdR[3:1] != 3'h7))));           // 无条件 ALU 才设标志
```

注意 `w_dcdR[3:1] != 3'h7`：若目的寄存器是 PC/CC（编号 `0x?e/0x?f`，`[3:1]==111`），即使是无条件 ALU 也不走普通标志路径——因为写 PC/CC 是控制操作，有专门处理。

**「魔法改写」与执行单元** 集中在 [idecode.v:575-614](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L575-L614)：

```verilog
o_op <= w_cis_op[3:0];
if ((w_ldi)||(w_noop)||(w_lock))
    o_op <= 4'hd;          // LDI/NOOP/LOCK 改写成 MOV(4'hd)，走 ALU 透传
...
o_ALU <= (w_ALU)||(w_ldi)||(w_cmptst)||(w_noop)||((!OPT_LOCK)&&(w_lock));
o_M   <= w_mem;
o_DV  <= (OPT_DIVIDE)&&(w_div);
o_FP  <= (OPT_FPU)&&(w_fpu);
```

注释 [idecode.v:577-587](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L577-L587) 解释得很清楚：LDI 当成 MOV（让立即数「穿过」ALU 写回）；CMP/TST 不写回结果，但 `o_ALU` 仍置 1，因为它们的实际运算是 SUB/AND（只是结果丢弃、只留标志）。于是 `o_op` 配合 `o_ALU/o_M/o_DV/o_FP` 就完整描述了执行动作。

#### 4.4.4 代码实践

1. **目标**：把 4.3 构造的 `ADD R3,#5 = 0x18880005` 的 `o_op/o_cond/o_wF` 全部算出来。
2. **步骤**：
   - `w_cis_op = 5'h02`（ADD），非 LDI/NOOP/LOCK → `o_op = w_cis_op[3:0] = 4'h2`。
   - `iword[21:19]=000`、非 LDI/special/CIS → `w_cond = {1, 000} = 4'h8`（无条件）→ `o_cond = 4'h8`。
   - `w_ALU=1`、`w_cond[3]=1`、非 mov/ldilo/brev、`w_dcdR[3:1]=001≠111` → `w_wF=1` → `o_wF=1`。
3. **预期结果**：`o_op=4'h2`、`o_cond=4'h8`、`o_wF=1`、`o_wR=1`、`o_ALU=1`、`o_M/o_DV/o_FP=0`。
4. **观察**：对照 [idecode.v:1086-1097](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L1086-L1097) 的形式化断言——它对 SUB/AND/ADD 类断言了 `(w_rA)&&(w_wR)&&(w_ALU)` 和 `(w_wF==w_cond[3])||(w_dcdA[3:1]==3'b111)`，与你的手算一致。

#### 4.4.5 小练习与答案

**练习**：`CMP R1,R2` 译码后 `o_op` 和 `o_wR` 分别是什么？为什么？

**参考答案**：CMP 的 `w_cis_op=5'h10`，低 4 位 `o_op=4'h0`（SUB）；又 `w_cmptst=1` → `w_wR_n=1` → `o_wR=0`（不写回结果）。也就是说 CMP 在硬件上就是「做减法、丢弃结果、只留标志」，与 spec 描述完全吻合。

---

### 4.5 压缩指令（CIS）的两阶段译码

#### 4.5.1 概念说明

CIS（Compressed Instruction Set）把**两条 16 位指令塞进一个 32 位字**（最高位 `CISBIT=1` 标识），只支持 8 种操作（SUB/AND/ADD/CMP/LW/SW/LDI/MOV），且不支持条件执行（见 spec [spec.tex:977-1047](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L977-L1047)）。

难点在于：流水线一次只能处理「一条」指令，而一个 CIS 字里有两「半条」。译码器的解决办法是用 `o_phase` 标志分两拍处理——**第一拍（phase=0）译高半字并锁存低半字，第二拍（phase=1）把锁存的低半字重新组装成一个完整字再译一次**。两拍之间禁止中断，免得停在「半条指令」中间。

#### 4.5.2 核心流程

```
CIS 字到达（CISBIT=1）
  ├─ phase=0：iword = i_instruction
  │     · 译高半字（iword[31:16]）
  │     · 把低半字 iword[14:0] 存进 r_nxt_half
  │     · 若是 CIS 且非法性正常 → 下一拍置 o_phase=1
  ├─ phase=1：iword = {1'b1, r_nxt_half[14:0], i_instruction[15:0]}
  │     · 组装出「低半字当成新指令」的 32 位字
  │     · 译这半条
  │     · 下一拍 o_phase 回到 0
```

注意 phase=1 时 `i_instruction` 已经是**下一条**指令字了，但译码器用上一拍锁存的 `r_nxt_half` 配合当前 `i_instruction[15:0]` 重建出本条 CIS 的低半字——这就是 [idecode.v:133-137](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L133-L137) 的玄机。

#### 4.5.3 源码精读

**iword 重组** 是 CIS 译码的核心，[idecode.v:130-137](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L130-L137)：

```verilog
if (OPT_CIS)
    assign iword = (o_phase)
        ? { 1'b1, r_nxt_half[14:0], i_instruction[15:0] }  // 第二拍：重建低半字
        : i_instruction;                                    // 第一拍/非CIS：原样
```

**低半字锁存** 在 [idecode.v:622-623](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L622-L623)：`r_nxt_half <= { iword[14:0] }`（即当前 CIS 字的低 15 位，最高位 1 是固定的 CIS 标志，不必存）。

**phase 状态机** 在 [idecode.v:408-423](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L408-L423)：

```verilog
if (o_phase)
    r_phase <= 0;                       // 第二拍后回到第一拍
else
    r_phase <= (i_instruction[CISBIT])&&(!i_illegal);  // 是 CIS 就进第二拍
```

**CIS 操作码映射** 已在 4.2.3 给出（[idecode.v:183-192](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L183-L192)）。CIS 的立即数也更窄：LDI 仅 8 位，其它 ALU 指令的立即数有「7 位」和「3 位 + 寄存器」两种紧凑形式，提取逻辑在 [idecode.v:367-376](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L367-L376)。CIS 的访存指令还有个特殊语法糖：当编码本应是「纯立即数」时，被重新解释成「以 SP 为基址」的访存（spec [spec.tex:1026-1033](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1026-L1033)），对应 [idecode.v:259-262](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L259-L262) 里 `CPU_SP_REG` 的替换。

软件侧，CIS 的两半分别用两张表：高半字用 `zip_oplist_raw`（[zopcodes.cpp:265-306](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L265-L306)），低半字用 `zip_opbottomlist_raw`（[zopcodes.cpp:315-369](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L315-L369)），反汇编时由 [zopcodes.cpp:671-678](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L671-L678) 分别调用，中间用竖杠 `|` 分隔两半。

#### 4.5.4 代码实践

1. **目标**：理解 CIS 高半字如何被识别，以及 phase 如何翻转。
2. **步骤**：构造一个 CIS 高半字 `LDI #5,R1`：
   - CIS LDI 高半字格式：位 31=1；30:27=R1=0001；26:24=110（LDI）；23:16=8 位立即数=0x05。
   - 高 16 位 = `1 0001 110 00000101` = `1000 1110 0000 0101` = `0x8E05`。
   - 配一个任意低半字（例如 `0x9603`），整字 = `0x8E059603`。
3. **走译码（仅看第一拍）**：
   - `iword[31]=1` → 是 CIS；`iword[26:24]=110` → `w_cis_op=5'h18`（LDI）。
   - 因为是 CIS，`w_cond=4'h8`（CIS 不支持条件）。
   - `r_nxt_half <= iword[14:0]`（锁存低半字 `0x1603` 的低 15 位）。
   - 下一拍 `o_phase` 置 1。
4. **预期结果**：第一拍输出一条 LDI（`o_op` 因 LDI 改写为 `4'hd`，`o_I` 来自 8 位立即数=5）；第二拍 `iword` 被重组为低半字内容，译出第二条指令。
5. **待本地验证**：用 `zip-objdump` 反汇编一个含 CIS 的 ELF，观察同一行里两条指令被竖杠分隔，对照上述两拍过程。

#### 4.5.5 小练习与答案

**练习**：为什么 `o_phase` 必须告诉下游「现在不能打断」？如果允许在两半之间中断会怎样？

**参考答案**：CIS 的两半逻辑上是一条指令流的两步，但 PC 只在整字边界前进。若中断发生在两半之间，中断返回时 PC 指向本 CIS 字的开头，会重新执行高半字，导致低半字被执行两次。所以 spec 明确「中断在两半之间被禁止」（[spec.tex:1037-1042](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1037-L1042)），`o_phase` 正是把这个约束传递给中断控制逻辑的信号。

---

## 5. 综合实践

**任务**：完整译码一条指令，串起本讲全部模块。

给定指令字 `0x18880005`（即 4.3/4.4 用过的 `ADD R3,#5`）。请按本讲四个模块的顺序，逐项写出译码器在 `i_ce` 上升沿后应输出的全部关键信号，并逐条给出依据。

参考解答（请先自己做完再对照）：

1. **是不是 CIS？**（模块 4.5）`iword[31] = (0x18880005 >> 31) & 1 = 0` → 非 CIS，`o_phase=0`，`iword = i_instruction`。
2. **操作码与类别**（模块 4.2）`w_op = iword[26:22]`。0x18880005 二进制 `0001 1000 1000 1000 0000 0000 0000 0101`，位 26:22 = `0 0010` → `w_op=5'h02`（ADD）。非 CIS 故 `w_cis_op=w_op=5'h02`；`w_ALU=1`，其余类别为 0。`w_add=1` 但因目的寄存器不是 PC，不触发早分支。
3. **寄存器号与立即数**（模块 4.3）
   - `w_dcdR = {i_gie, iword[30:27]=0011}` → R3；`w_dcdR_pc=0`、`w_dcdR_cc=0`；`o_dcdR={0,0,00011}`。
   - 位 18=0 → `w_immsrc=2`（18 位立即数）→ `w_fullI = 符号扩展(iword[17:0]=5) = 5` → `o_I = 0x00000005`，`o_zI=0`。
   - `w_rA=1`、`w_rB=0`（纯立即数，不读 B 寄存器）。
4. **条件、写回、标志、执行单元**（模块 4.4）
   - `iword[21:19]=000` → `w_cond={1,000}=4'h8` → `o_cond=4'h8`（无条件）。
   - 非 sto/special/cmptst → `o_wR=1`。
   - `w_ALU=1`、`w_cond[3]=1`、目的非 PC/CC → `o_wF=1`。
   - `o_op = w_cis_op[3:0] = 4'h2`（ADD）；`o_ALU=1`、`o_M=o_DV=o_FP=0`。

**最终一句总结这条指令**：无条件地把立即数 5 加到 R3，结果写回 R3 并更新 CC 标志，走 ALU 通路。

> 进阶：再取 `0x08048000`（SUB R1,R2）重做一遍。注意它的位 18=1（寄存器形式），`w_immsrc` 会变成 3、`w_rB=1`、`o_I` 来自 14 位偏移（此处为 0）。两道题对照，你就掌握了「立即数形式 vs 寄存器形式」的全部差异。

---

## 6. 本讲小结

- `idecode` 是流水线第 2 级的核心，输入 32 位指令字，输出一整套 `o_*` 控制信号；功能逻辑集中在 [idecode.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v) 前 ~900 行。
- 操作码识别分两步：抠 5 位 `w_op`，CIS 再用 [idecode.v:183-192](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L183-L192) 的映射表把 3 位升级成 5 位 `w_cis_op`，再用位模式比较分到 ALU/访存/乘除/浮点/特殊等类。
- 寄存器号是 7 位（`{CC标志, PC标志, 组位, 4位号}}`），A 恒等于目的寄存器 R；立即数经 `w_immsrc` 四选一抠出（23/13/18/14 位），符号扩展到 32 位从 `o_I` 输出（**没有** `o_imm` 这个端口）。
- 条件码 `o_cond` 最高位为 1 表示无条件；LDI/CMP/TST 会被「魔法改写」成 MOV/SUB/AND 复用 ALU；`o_wF` 只在无条件 ALU 运算或 CMP/TST 时置位，对应 spec「条件指令不改标志」的规则。
- CIS 用 `o_phase` 分两拍译码：第一拍译高半字并锁存低半字到 `r_nxt_half`，第二拍用 [idecode.v:133-137](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L133-L137) 重组低半字再译一次，两拍间禁止中断。
- 硬件译码（位切片 + 比较器）与软件表 `zopcodes.cpp`（掩码 + 匹配值）是同一份 ISA 的两种表达，可用 `(ins & mask)==val` 互相印证。

---

## 7. 下一步学习建议

- **顺着数据流往下读**：译码输出的 `dcd_*` 信号在「读操作数」级如何被用来读寄存器堆、在「执行」级如何驱动 `cpuops`（ALU）。建议进入 u3-l4（ALU 运算单元 cpuops），那里会用到本讲的 `o_op` 与 `o_wF`。
- **看冒险如何与译码交互**：u3-l7（流水线冒险与停顿）会讲到 `dcd_stalled` 如何反压译码器，以及条件执行在第 4 级由 `set_cond` 判决的细节。
- **形式化验证的对照参考解码器**：`idecode.v` 第 924 行起的 `\ifdef FORMAL` 段里实例化了一个纯组合的参考解码器 `f_idecode`（[idecode.v:2082-2106](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/idecode.v#L2082-L2106)），用它的输出逐项比对主译码器的输出——这是 u5-l2（形式化验证）的绝佳案例，等你学完本讲再看那段断言会豁然开朗。
- **动手编码练习**：用 `zip-gcc` 编一段小程序，`zip-objdump -d` 反汇编后挑几条指令字，用本讲的方法手工译码，再与反汇编结果对照，检验自己的理解。
