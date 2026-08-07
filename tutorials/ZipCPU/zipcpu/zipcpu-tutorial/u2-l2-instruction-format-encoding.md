# 指令格式与编码

## 1. 本讲目标

本讲是 ISA（指令集架构）单元的第二讲。上一篇（u2-l1）我们看清了 ZipCPU 的寄存器组与状态寄存器 CC；本篇把镜头拉近，拆开一条 32 位指令字，看清「一个数到底怎么代表一条指令」。

学完本讲你应该能够：

- 画出 ZipCPU **标准指令格式**的字段布局，说出 DR、OpCode、Cnd、OpB 各自的位置与位宽。
- 读懂 `spec.tex` 里的 OpCode 分配表，知道 5 位操作码如何编码 29 条指令。
- 读懂 `sw/zasm/zopcodes.h` 里的 `ZIP_REGFIELD` / `ZIP_IMMFIELD` / `ZIP_BITFIELD` 等宏，理解汇编器/反汇编器是如何「在一条 32 位字里定位某个字段」的。
- **手工编码一条指令**：给定汇编（如 `SUB R1,R2`），逐步填出 32 位十六进制机器码。

## 2. 前置知识

在动手拆指令之前，先对齐几个术语。如果你已熟悉，可跳过本节。

- **bit（位）与字段（field）**：一条 32 位指令就是 32 个 0/1，从最高位（bit 31）到最低位（bit 0）。把其中连续的若干位划出来表示一个含义，就叫一个「字段」。
- **位宽**：一个字段占几位。例如「4 位 DR」表示用 4 个 bit 来表示寄存器号，刚好够表示 0–15 号寄存器。
- **大端（big endian）**：ZipCPU 是大端机器，即「高位字节存放在低地址」。`spec.tex` 的指令格式图都按 bit 31 在最左、bit 0 在最右来画。本讲我们只关心「字内的 bit 排布」，不涉及字节在内存中的顺序。
- **有符号立即数与符号扩展**：很多立即数字段比较窄（如 14 位、18 位），但要把数值当作有符号整数参与运算。把最高位（符号位）向高位复制填充，就叫符号扩展（sign extension）。例如 4 位 `1001`（= −7）符号扩展到 8 位就是 `11111001`（= −7）。
- **操作码（OpCode）/ 寄存器号 / 条件码（Cnd）**：操作码告诉 CPU「做什么运算」；寄存器号告诉它「用哪些寄存器」；条件码告诉它「在什么前提成立时才执行」。条件码的取值在上一篇 u2-l1 的状态寄存器 CC 中已经讲过基础（Z/C/N/V 四个标志位）。
- **掩码（mask）+ 匹配值（value）的查表思想**：这是理解 `zopcodes.cpp` 指令表的关键，后面会专门讲。先记住一句话：**「指令字 & 掩码 == 匹配值」就认为它是这条指令**。

## 3. 本讲源码地图

本讲主要在三处源码之间来回对照：规范讲「应该是什么样」，工具源码讲「汇编器/反汇编器实际怎么识别」。

| 文件 | 作用 | 本讲用到的小节 |
|------|------|----------------|
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | ISA 规范（LaTeX） | Instruction Format、Instruction OpCodes、Conditional Instructions、Operand B、Derived Instructions |
| [sw/zasm/zopcodes.h](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.h) | 指令表的数据结构与字段定位宏 | `ZOPCODE` 结构体、`ZIP_*FIELD` 宏 |
| [sw/zasm/zopcodes.cpp](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp) | 指令表本体 + 反汇编主逻辑 | `zip_oplist_raw` 指令表、`zip_getbits`、`zipi_to_halfstring` |
| [sw/zasm/zdump.cpp](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zdump.cpp) | 反汇编器命令行工具 | 用它来验证你手工编码的结果 |

> 小贴士：规范（spec）描述的是「硬件认什么」，`zopcodes.cpp` 描述的是「软件（汇编/反汇编/模拟器）怎么认」。两者必须一致，本讲会经常把它们并排放在一起看。

## 4. 核心概念与源码讲解

### 4.1 32 位标准指令格式：四大字段加一个选择位

#### 4.1.1 概念说明

很多 CPU 的指令字长是固定的。ZipCPU 的主指令是 **32 位定长**（另有可选的 16 位压缩指令 CIS，本讲先不展开，留到以后讲）。一条标准指令要回答四个问题：

1. **做什么运算？** → 操作码 OpCode
2. **结果写回哪个寄存器？** → 目的寄存器 DR（它同时也是「A 操作数」的来源）
3. **什么条件下才执行？** → 条件码 Cnd
4. **第二个操作数（Operand B）从哪来？** → 立即数，或「寄存器 + 偏移」

注意一个关键设计：**目的寄存器 DR 同时充当源操作数 A**。也就是说，像 `SUB R1,R2` 这样的写法，实际语义是 `R1 = R1 - R2`，R1 既是输入又是输出。这就是为什么 ZipCPU 很多指令看起来像「两操作数」，本质是「目的寄存器读旧值、写新值」。

#### 4.1.2 核心流程

把 32 位从高到低切成字段，标准指令格式如下：

```
 bit:  31 | 30-27 | 26-22 | 21-19 | 18  | 17-14 | 13-0
       ─── ─────── ─────── ─────── ──── ─────── ───────
        0    DR      OpCode  Cnd   选择位   BR    立即数
       恒0  目的寄存器 操作码  条件  OpB模式 B寄存器 偏移
```

各字段含义与位宽：

| 字段 | bit 位置 | 位宽 | 含义 |
|------|----------|------|------|
| 保留 | 31 | 1 | 标准指令恒为 0（置 1 表示这是 CIS 压缩指令） |
| DR | 30–27 | 4 | 目的寄存器号（0–15），同时是 A 操作数 |
| OpCode | 26–22 | 5 | 操作码，最多 32 种运算 |
| Cnd | 21–19 | 3 | 条件码，8 种条件（见 4.4） |
| OpB 选择位 | 18 | 1 | **0 = Operand B 是 18 位立即数；1 = Operand B 是「BR + 14 位偏移」** |
| BR | 17–14 | 4 | B 寄存器号（仅当选择位 = 1 时有意义） |
| 立即数 | 13–0 | 14 | 有符号偏移（选择位 = 1 时）；选择位 = 0 时则把 bit 17–0 整体当作 18 位立即数 |

也就是说，**Operand B 有两种形态，靠 bit 18 一位来切换**：

- 选择位 = 0：`OpB = 18 位有符号立即数`（占据 bit 17–0）
- 选择位 = 1：`OpB = (BR 寄存器) + 14 位有符号偏移`（BR 在 bit 17–14，偏移在 bit 13–0）

#### 4.1.3 源码精读

规范在 [doc/src/spec.tex:635-691](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L635-L691) 的 Instruction Format 小节用 bytefield 画了标准格式的两张子图（立即数形态与寄存器形态），并用一句话总结：

> 某个由 OpCode 定义的操作，在条件 Cnd 为真时执行，结果放入目的寄存器 DR；而「B 操作数」要么是一个 18 位有符号立即数，要么是「14 位有符号立即数 + 某寄存器的值」。

工具源码侧，`zopcodes.cpp` 在每条指令前都留了一行注释，把整条 32 位字按 bit 描述出来。最关键的一行（通用指令模板）是 [sw/zasm/zopcodes.cpp:131](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L131)：

```cpp
// 0rrr.rooo.oocc.cxrr.rrii.iiii.iiii.iiii
```

把这串从左到右对应到 bit 31→0：开头的 `0` 就是「保留位恒 0」；`rrrr`（跨过两个点号）是 4 位 DR；`ooooo` 是 5 位 OpCode；`ccc` 是 3 位 Cnd；紧接的 `x` 就是 bit 18 的 OpB 选择位；`rrrr` 是 BR；最后 `iiiiiiiiiiiiii` 是 14 位立即数。这行注释和上面的字段表完全对应——它就是字段表的「压缩记法」。

#### 4.1.4 代码实践

**实践目标**：用一行注释式记法自己描述一条指令的字段布局。

**操作步骤**：

1. 打开 [spec.tex 的 Instruction Format 图](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L635-L691)。
2. 打开 [zopcodes.cpp:131](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L131) 的注释。
3. 在纸上把两份信息对齐，逐字段确认 bit 区间。

**需要观察的现象**：规范图里把「立即数形态」和「寄存器形态」画成上下两行，而注释行用 `x`（选择位）一行兼顾了两种形态。

**预期结果**：你能不看本讲，自己复述出 DR=30–27、OpCode=26–22、Cnd=21–19、选择位=18、BR=17–14、立即数=13–0。

#### 4.1.5 小练习与答案

**练习**：标准指令格式里，bit 18 这一位为什么是「一票否决式」的关键位？

**参考答案**：因为它单独决定了 Operand B 的解释方式。bit 18 = 0 时，bit 17–0 整体是一个 18 位立即数；bit 18 = 1 时，bit 17–14 是寄存器号、bit 13–0 是偏移。同一段 bit，因为这一位不同而被解释成完全不同的东西，所以它是整条指令里最关键的「模式开关」。

---

### 4.2 操作码 OpCode：5 位如何编码 29 条指令

#### 4.2.1 概念说明

OpCode 占 5 位，理论上能表示 \( 2^5 = 32 \) 种运算。ZipCPU 实际定义了 **29 条指令**，剩下 6 个编码预留给（可选的、未来的）单精度浮点加速器，还有几个编码被 NOOP/BREAK/LOCK/SIM 这一组「特殊指令」复用。所以 OpCode 不是「一个编码严格对应一条指令」那么简单，而是一张分配表。

#### 4.2.2 核心流程

OpCode 分配表（节选，完整表见 spec）如下，注意第二列是 5 位十六进制操作码（对应 bit 26–22）：

| OpCode | 助记符 | 含义 |
|--------|--------|------|
| `5'h00` | SUB | 减法 |
| `5'h01` | AND | 按位与 |
| `5'h02` | ADD | 加法 |
| `5'h03` | OR  | 按位或 |
| `5'h04` | XOR | 按位异或 |
| `5'h05–07` | LSR / LSL / ASR | 逻辑/算术移位 |
| `5'h08` | BREV | 把 B 操作数按位反转写入结果 |
| `5'h09` | LDILO | 装入低位立即数 |
| `5'h0a–0c` | MPYUHI / MPYSHI / MPY | 乘法（高/低 32 位） |
| `5'h0d` | MOV | 把 OpB 送入 Ra |
| `5'h0e–0f` | DIVU / DIVS | 除法（无符号/有符号） |
| `5'h10` | CMP | 比较（Ra − OpB 与零比） |
| `5'h11` | TST | 测试（按位与，不写结果） |
| `5'h12–17` | LW/SW/LH/SH/LB/SB | 读/写 字/半字/字节 |
| `5'h18/19` | LDI | 装入 23 位有符号立即数（特例格式） |
| `5'h1a–1f` | FPADD…FPF2I | 预留浮点 |

可以看到：算术逻辑运算集中在 `5'h00–0f`，访存指令整齐地排在 `5'h12–17`，这能让译码器用「看高几位」就快速分类。

#### 4.2.3 源码精读

规范的操作码分配表在 [doc/src/spec.tex:692-743](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L692-L743)，并点明「32 个编码实现了 29 条指令，另 6 个留给浮点」。

工具侧，`zopcodes.cpp` 的指令表用「掩码 + 匹配值」来表达每条指令。以 SUB 为例（[sw/zasm/zopcodes.cpp:132-133](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L132-L133)）：

```cpp
{ "SUB", 0x87c40000, 0x00000000, ZIP_REGFIELD(27), ZIP_REGFIELD(27), ZIP_OPUNUSED,        ZIP_IMMFIELD(18,0), ZIP_BITFIELD(3,19) },
{ "SUB", 0x87c40000, 0x00040000, ZIP_REGFIELD(27), ZIP_REGFIELD(27), ZIP_REGFIELD(14),    ZIP_IMMFIELD(14,0), ZIP_BITFIELD(3,19) },
```

第一列 `s_opstr="SUB"`；第二列 `s_mask=0x87c40000` 是掩码；第三列 `s_val` 是匹配值。判定规则就是：**`(指令字 & s_mask) == s_val` 则命中**。

来拆掩码 `0x87c40000`，把它写成二进制看哪些 bit 被「钉死」：

```
0x87c40000 = 1000 0111 1100 0100 0000 0000 0000 0000
             b31                b18
```

- bit 31 = 1 且匹配值该位 = 0 → **bit 31 必须为 0**（不是 CIS）。
- bit 26–22（OpCode 区）对应掩码位为 1，匹配值为 0 → **OpCode 必须为 `00000` = SUB**。
- bit 18 = 1 且匹配值该位决定形态：第一行 `s_val=0x00000000`（bit18=0 → 立即数形态）；第二行 `s_val=0x00040000`（bit18=1 → 寄存器形态）。

所以 SUB 的两条表项分别对应 `SUB $imm,Rd`（立即数）和 `SUB off(Rb),Rd`（寄存器+偏移）。**同一条助记符因 Operand B 形态不同而拆成两条表项**——这是后面手工编码时要注意的。

> 字段宏 `ZIP_REGFIELD(27)` 等的含义在 4.3 节细讲，这里先知道它们告诉反汇编器「DR 在 bit 27 起、宽 4 位」即可。

对照 ADD（`5'h02`）：[sw/zasm/zopcodes.cpp:138-139](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L138-L139) 的匹配值是 `0x00800000`，其二进制 bit 23 = 1，即 OpCode 区 `00010` = `5'h02`，与表一致。MOV（`5'h0d`）匹配值 `0x03400000`，OpCode 区 = `01101` = `5'h0d`，同样吻合。

#### 4.2.4 代码实践

**实践目标**：从指令表反推助记符对应的 OpCode 数值。

**操作步骤**：

1. 在 [zopcodes.cpp 的通用指令区](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L132-L204) 任选一条，比如 `XOR`（[第 144 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L144)），记下它的 `s_val`。
2. 把 `s_val` 与掩码 `0x87c40000` 相与，取出 bit 26–22 这 5 位。
3. 与 [spec 操作码表](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L697-L736) 对照，确认得到的数值与助记符一致。

**需要观察的现象**：XOR 的 `s_val=0x01000000`，bit 24 = 1，OpCode 区 = `00100` = `5'h04`，正是表中 XOR。

**预期结果**：你为 `AND/OR/XOR/LSR/LSL/ASR` 各算出一个 5 位 OpCode，且与 spec 表完全对得上。

#### 4.2.5 小练习与答案

**练习 1**：为什么 SUB 需要两条表项，而表里它只占一个 OpCode `5'h00`？

**参考答案**：OpCode 只决定「做什么运算」，不决定「操作数 B 是立即数还是寄存器」。后者由 bit 18 选择位决定，并对应两套字段布局（18 位立即数 vs 14 位偏移+寄存器号）。因此反汇编器要用两条「掩码+匹配值」表项分别识别这两种形态，但它们共享同一个 OpCode `5'h00`。

**练习 2**：访存指令 `LW/SW/LH/SH/LB/SB` 的 OpCode 依次是 `5'h12–17`，相邻只差 1。这种紧凑排列对译码器有什么好处？

**参考答案**：它们的高位 bit 完全相同、只最低几位区分「读/写」与「字/半字/字节」，译码器可以先用高位快速判定「这是访存类」，再用低位几个 bit 选择具体粒度和方向，逻辑简单、延迟低。

---

### 4.3 Operand B 与 zopcodes.h 的字段定位宏

#### 4.3.1 概念说明

规范定义了字段布局，但软件（汇编器、反汇编器、模拟器）需要一种**统一的办法来描述「某个字段在指令字的哪几位、有多宽」**，并能据此从一条 32 位字里把字段「抠」出来或「塞」进去。ZipCPU 的做法是在 `zopcodes.h` 里定义一组宏，把「位置 + 宽度 + 类型」编码进一个整数里，存在 `ZOPCODE` 结构体的字段里。理解这组宏，是看懂整张指令表的前提。

#### 4.3.2 核心流程

Operand B 的两种形态（规范 [Operand B 小节](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L846-L872)）：

```
选择位=0:  0 | 18 位有符号立即数
选择位=1:  1 | 4 位 Reg | 14 位有符号立即数
```

宏把字段的「最低位位置」和「位宽」打包。读取时的核心算法是「先右移、再按位宽截取、必要时符号扩展」：

- 截取无符号 N 位字段：`value & ((1<<N) - 1)`
- 符号扩展：若最高位为 1，则高位全填 1。用公式描述，对一个 N 位字段值 \( v \)：

\[
\text{signed}(v) = \begin{cases} v & v < 2^{N-1} \\ v - 2^{N} & v \ge 2^{N-1} \end{cases}
\]

#### 4.3.3 源码精读

字段宏定义在 [sw/zasm/zopcodes.h:44-55](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.h#L44-L55)：

```cpp
#define ZIP_OPUNUSED   -1
#define ZIP_BITFIELD(LN,MN)  (((LN&0x0ff)<<8)+(MN&0x0ff))            // 通用位段
#define ZIP_REGFIELD(MN)     (0x00000400 +(MN&0x0ff))                // 普通寄存器字段（4 位）
#define ZIP_URGFIELD(MN)     (0x0100400 +(MN&0x0ff))                 // 用户寄存器字段
#define ZIP_SRGFIELD(MN)     (0x0200400 +(MN&0x0ff))                 // 监管寄存器字段
#define ZIP_IMMFIELD(LN,MN)  (0x40000000 + (((LN&0x0ff)<<8)+(MN&0x0ff))) // 有符号立即数
```

怎么读这些宏？把宏的数值看成「一个打包的描述符」：

- **低 8 位（`MN & 0xff`）**：字段的**起始 bit 号**（右移的位数）。例如 `ZIP_REGFIELD(27)` 里 `MN=27`，表示「从 bit 27 开始」。
- **第 8–15 位（`(LN & 0xff)<<8`）**：字段的**位宽**。例如 `ZIP_IMMFIELD(18,0)` 里 `LN=18`，表示「18 位宽」。
- **第 30 位（`0x40000000`）**：是否为**有符号立即数**（需要符号扩展）。`ZIP_IMMFIELD` 置位，`ZIP_REGFIELD` / `ZIP_BITFIELD` 不置位。
- **第 16–20 位**：寄存器组的「基地址偏移」，用于区分普通/用户/监管寄存器组（与 u2-l1 的双寄存器组、GIE 作为第 5 位的思想一致）。
- `ZIP_OPUNUSED (-1)`：该指令「不使用」这个字段。

`ZOPCODE` 结构体（[zopcodes.h:58-76](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.h#L58-L76)）就是把这些描述符存起来：

```cpp
typedef struct {
    char    s_opstr[8];   // 助记符
    ZIPI    s_mask, s_val; // 掩码 + 匹配值
    int     s_result,      // 结果寄存器字段
            s_ra,          // A 寄存器字段
            s_rb,          // B 寄存器字段
            s_i,           // 立即数字段
            s_cf;          // 条件码字段
} ZOPCODE;
```

于是 `s_rb = ZIP_REGFIELD(14)` 的含义就是「B 寄存器从 bit 14 起、宽 4 位、无符号」；`s_i = ZIP_IMMFIELD(18,0)` 的含义就是「立即数从 bit 0 起、宽 18 位、有符号」。

真正「按描述符抠字段」的代码是 `zip_getbits`（[sw/zasm/zopcodes.cpp:516-525](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L516-L525)）：

```cpp
static int zip_getbits(const ZIPI ins, const int which) {
    if (which & 0x40000000) {                       // 有符号立即数
        return zip_sbits(ins>>(which & 0x03f), (which>>8)&0x03f);
    } else {                                         // 无符号位段/寄存器
        return zip_ubits(ins>>(which & 0x03f), (which>>8)&0x03f)
               + ((which>>16)&0x0ff);
    }
}
```

它正好按上面的「右移 + 截取 +（符号扩展 / 加寄存器组偏移）」执行：`which & 0x3f` 取起始位，`(which>>8)&0x3f` 取宽度，`0x40000000` 决定要不要符号扩展，`(which>>16)&0xff` 给寄存器组加偏移。

#### 4.3.4 代码实践

**实践目标**：手算 `zip_getbits` 对一个已知字段描述符的返回值。

**操作步骤**：

1. 取指令字 `0x08048000`（这是第 5 节我们将手工编出的 `SUB R1,R2`，你可以先相信它）。
2. 对 DR 字段描述符 `ZIP_REGFIELD(27) = 0x041b` 手算：起始位 = `0x1b = 27`，宽度 = `(0x041b>>8)&0x3f = 4`，无符号。
3. 计算 `0x08048000 >> 27`，再取低 4 位。

**需要观察的现象**：`0x08048000` 的高字节是 `0x08` = `0000 1000`，bit 27 恰为 1，更高位为 0；右移 27 后得到 `0001` = 1。

**预期结果**：`zip_getbits(0x08048000, ZIP_REGFIELD(27)) == 1`，即 DR = R1，这正是 `SUB R1,...` 的目的寄存器。

#### 4.3.5 小练习与答案

**练习**：`ZIP_IMMFIELD(14,0)` 和 `ZIP_IMMFIELD(18,0)` 分别用在 SUB 的哪条表项上？为什么？

**参考答案**：`ZIP_IMMFIELD(18,0)` 用于 SUB 的**立即数形态**（bit 18 = 0，Operand B 占 bit 17–0，宽 18 位）；`ZIP_IMMFIELD(14,0)` 用于 SUB 的**寄存器形态**（bit 18 = 1，偏移只占 bit 13–0，宽 14 位）。Operand B 两种形态下「立即数部分」的位宽不同，所以用不同的 `LN` 参数。

---

### 4.4 条件字段 Cnd：3 位 8 种条件，与派生指令

#### 4.4.1 概念说明

ZipCPU 的一大特色是「几乎所有指令都可条件执行」。这在硬件上的体现就是：标准指令里专门留了 3 位 Cnd（bit 21–19），共 8 种条件。当条件不满足时，指令「不生效」（不写结果、也不清流水线），等价于一条空操作。注意有几个例外**不能**条件执行：`LDI`（23 位立即数装入）、以及 `NOOP/SIM/BREAK/LOCK` 这组特殊指令——因为它们的编码空间被挪去存大立即数或调试信息了。

#### 4.4.2 核心流程

8 种条件（与 CC 里的 Z/C/N/V 四个标志位挂钩）：

| Cnd 编码 | 助记符后缀 | 执行条件 |
|----------|-----------|----------|
| `3'h0` | （无） | 恒执行 |
| `3'h1` | `.Z`  | Z = 1（结果为零） |
| `3'h2` | `.LT` | N = 1（负） |
| `3'h3` | `.C`  | C = 1（有进位 / 无符号小于） |
| `3'h4` | `.V`  | V = 1（溢出） |
| `3'h5` | `.NZ` | Z = 0（非零） |
| `3'h6` | `.GE`| N = 0（非负 / 有符号大于等于） |
| `3'h7` | `.NC` | C = 0（无进位 / 无符号大于等于） |

注意并不直接提供「小于等于」「大于」「非 V」等条件——它们靠「调整比较指令」间接实现（派生指令，见下文）。

#### 4.4.3 源码精读

规范的条件表在 [doc/src/spec.tex:744-802](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L744-L802)。工具侧，条件后缀字符串就是一张 8 元素表 [sw/zasm/zopcodes.cpp:67-70](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L67-L70)：

```cpp
const char *zip_ccstr[8] = {
    "",  ".Z",  ".LT", ".C",
    ".V",".NZ", ".GE", ".NC"
};
```

下标就是 Cnd 的数值。在指令表里，条件字段统一用 `ZIP_BITFIELD(3,19)` 描述（如 [SUB 表项最后一列](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L132)），即「bit 19 起、宽 3 位、无符号」。反汇编时，`zipi_to_halfstring` 取出这个 3 位值，去 `zip_ccstr` 里查后缀，拼到助记符后面（[zopcodes.cpp:575-578](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L575-L578)）。

派生指令（Derived Instructions）说明了一个重要事实：**很多「常见指令」其实不是一条新指令，而是已有指令的特定用法**。例如 [spec.tex:1290 起](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1290-L1396) 的派生表里：

- `CLR Rx`（清零）= `LDI $0,Rx`
- `BRA / BZ / BNZ / BLT ...`（条件分支）= `ADD.<cond> $Addr+PC, PC`（给 PC 加偏移）
- `BUSY`（死循环）= `ADD $-1, PC`
- `LDI $val,Rx`（装 32 位立即数）= `BREV REV(val)&0xffff,Rx` 接 `LDILO (val&0xffff),Rx`（两条指令拼出一个 32 位常数）

这些派生关系在 `zopcodes.cpp` 里直接以独立表项出现，比如 `BUSY`、`BZ`、`CLR`、`HALT`、`RTU` 等（[第 78–115 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L78-L115)）。所以读指令表时会看到比 spec 的 29 条多得多的助记符——它们大多是「有自己名字的派生指令」。

#### 4.4.4 代码实践

**实践目标**：把一个条件后缀翻译成 Cnd 的 3 位编码。

**操作步骤**：

1. 想要给 `SUB R1,R2` 加上 `.NZ`（非零才执行）。
2. 在 [zip_ccstr](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L67-L70) 表里找 `.NZ` 的下标。
3. 该下标就是 Cnd 的 3 位值，要放进 bit 21–19。

**需要观察的现象**：`.NZ` 在表中下标为 5，即 `3'h5 = 101`。

**预期结果**：`SUB.NZ R1,R2` 的 Cnd 字段 = `101`（填到 bit 21–19）。

#### 4.4.5 小练习与答案

**练习**：为什么 `LDI` 不能像 `SUB` 那样加条件后缀（如 `LDI.Z`）？

**参考答案**：`LDI` 用 4 位 OpCode + 23 位大立即数的特例格式，把本该放 Cnd 的 3 位也用来存立即数了，编码空间里没有给条件字段留位置。如果确实需要「条件装入」，派生指令表给出的做法是改用 `BREV.<cond>` + `LDILO`（参见 `CLR.NZ` 的实现），由汇编器在 `LDI` 和 `BREV` 之间自动选择。

---

### 4.5 三种特例格式：MOV / LDI / NOOP

#### 4.5.1 概念说明

标准格式（4.1）是「主力」，但有三种特例格式为了腾出编码空间做了让步：

- **MOV**：偷了 2 个 bit（A、B 位，即图中的 bit 18 与 bit 13）用来在监管模式下访问「用户寄存器组」，因此它的 Operand B 没有「纯立即数」形态，只有「寄存器 + 13 位偏移」。
- **LDI**：装入 23 位大立即数，4 位 OpCode，**无条件字段**，Operand B 全部 23 位都给立即数。
- **NOOP 组**（NOOP/SIM/BREAK/LOCK）：忽略寄存器和立即数，把那些 bit 留给仿真/调试用途。

#### 4.5.2 核心流程

三种特例的字段布局：

```
MOV : 0 | DR(4) | 5'hd | A(1) | BR(4) | B(1) | 13 位有符号立即数
       (A=1 目的为用户组；B=1 源为用户组)
LDI : 0 | DR(4) | 4'hc | 23 位有符号立即数
NOOP: 0 | 3'h7 | .. | 11 | xxx | 22 位(被忽略/留给仿真)
```

#### 4.5.3 源码精读

规范对三种特例的说明在 [Instruction Format 小节末尾](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L679-L690) 与 [Move Operands 小节](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L879-L904)。

工具侧，MOV 因为多了「访问用户组」的能力，在指令表里有 **4 条**表项，对应「目的/源」是否在用户组的四种组合（[sw/zasm/zopcodes.cpp:172-175](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L172-L175)）：

```cpp
{ "MOV", 0x87c42000, 0x03400000, ZIP_REGFIELD(27), ..., ZIP_REGFIELD(14), ZIP_IMMFIELD(13,0), ... }, // 目的=当前组, 源=当前组
{ "MOV", 0x87c42000, 0x03440000, ZIP_URGFIELD(27),..., ZIP_REGFIELD(14), ZIP_IMMFIELD(13,0), ... }, // 目的=用户组
{ "MOV", 0x87c42000, 0x03402000, ZIP_REGFIELD(27), ..., ZIP_URGFIELD(14), ZIP_IMMFIELD(13,0), ... }, // 源=用户组
{ "MOV", 0x87c42000, 0x03442000, ZIP_URGFIELD(27),..., ZIP_URGFIELD(14), ZIP_IMMFIELD(13,0), ... }, // 都是用户组
```

注意两点：① 掩码 `0x87c42000` 比 SUB 的 `0x87c40000` 多钉了一位（bit 13，即 `0x2000`），因为 MOV 的 B 位固定占 bit 13；② 立即数字段是 `ZIP_IMMFIELD(13,0)`，宽 13 位（不是 14 位），印证了「MOV 偷了 1 位给 B 标志」。

LDI 表项（[第 207 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L207)）：掩码 `0x87800000`、匹配值 `0x06000000`，OpCode 区 = `0110`（4 位，对应 `5'h18/19`，因为 LDI 只用 4 位 OpCode），立即数 `ZIP_IMMFIELD(23,0)`，**没有条件字段**（最后一列为 `ZIP_OPUNUSED`）。

#### 4.5.4 代码实践

**实践目标**：理解 MOV 的「跨寄存器组」能力从哪个 bit 来。

**操作步骤**：

1. 对比 MOV 第一条和第二条表项的 `s_val`：`0x03400000` vs `0x03440000`，差异在 bit 18（`0x40000`）。
2. 再看第一条和第三条：`0x03400000` vs `0x03402000`，差异在 bit 13（`0x2000`）。

**需要观察的现象**：bit 18 控制「目的是否用户组」，bit 13 控制「源是否用户组」。这与 spec「A 位 / B 位」的描述对应。

**预期结果**：你能说出监管模式代码要读一个用户寄存器，需把 MOV 的 B 位（bit 13）置 1。

#### 4.5.5 小练习与答案

**练习**：MOV 的 Operand B 为什么不像 SUB 那样支持「纯立即数」形态？

**参考答案**：因为「装入立即数」已经有专门的 `LDI` 指令，MOV 不需要重复这个能力；而且 MOV 把 bit 13（B 位）拿去标记「源是否在用户组」，于是 Operand B 只剩「寄存器 + 13 位偏移」这一种形态。这也让 MOV 可以被编译器当成「不影响条件码的三操作数 ADD」用于地址计算（见 [Move Operands 小节](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L879-L904)）。

## 5. 综合实践：手工编码 `SUB R1,R2` 并验证

把本讲四个模块串起来，完成规格里要求的核心任务：**从汇编 `SUB R1,R2` 推出 32 位十六进制机器码**。

**任务语义**：`SUB R1,R2` 表示 `R1 = R1 - R2`，目的寄存器 DR = R1，源 B 寄存器 BR = R2，无条件，偏移为 0。因为操作数 B 是寄存器，所以走 **寄存器形态**（bit 18 = 1）。

### 第一步：逐字段填值

| 字段 | bit | 值 | 二进制 |
|------|-----|----|--------|
| 保留 | 31 | 0 | `0` |
| DR | 30–27 | 1（R1） | `0001` |
| OpCode | 26–22 | 0（SUB） | `00000` |
| Cnd | 21–19 | 0（恒执行） | `000` |
| 选择位 | 18 | 1（寄存器形态） | `1` |
| BR | 17–14 | 2（R2） | `0010` |
| 立即数 | 13–0 | 0 | `00000000000000` |

### 第二步：拼成 32 位并转十六进制

把上表从 bit 31 拼到 bit 0：

```
0 0001 00000 000 1 0010 00000000000000
```

按 4 位一组重新切分（从 bit 31 起）：

```
0000 1000 0000 0100 1000 0000 0000 0000
 0     8    0    4    8    0    0    0
```

所以 **`SUB R1,R2` = `0x08048000`**。

### 第三步：用源码交叉验证

用 4.2 学到的「掩码 + 匹配值」规则验证：对 SUB 寄存器形态表项（[zopcodes.cpp:133](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L133)，`s_mask=0x87c40000, s_val=0x00040000`）：

```
0x08048000 & 0x87c40000 == 0x00040000   ✅  命中 SUB 寄存器形态
```

再用 4.3 的字段提取复核：DR = `zip_getbits(0x08048000, ZIP_REGFIELD(27))` = 1（R1 ✓）；BR = `zip_getbits(0x08048000, ZIP_REGFIELD(14))`：`0x08048000 >> 14` 的低 4 位 = `0010` = 2（R2 ✓）；Cnd = bit 21–19 = 0（无条件 ✓）。

### 第四步（可选，待本地验证）：用反汇编器 `zdump` 验证

`zdump`（[sw/zasm/zdump.cpp](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zdump.cpp)）按机器字反汇编，它直接复用 `zipi_to_double_string`（[zopcodes.cpp:670-678](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L670-L678)）。默认按小端读取 32 位字（`-B` 切大端）。验证思路：

1. 先构建工具链（按 u1-l2 的 `make sw`，把 `sw/install/cross-tools/bin` 加入 PATH 后续编）。
2. 把机器码 `0x08048000` 以小端 4 字节（`00 80 04 08`）写入一个文件，例如 `sub.bin`。
3. 运行 `zdump sub.bin`。

**预期结果**（待本地验证）：输出应包含形如 `SUB R2,R1` 或 `SUB $0+R2,R1` 的反汇编（具体书写格式由 `zipi_to_halfstring` 决定，见 [第 567–663 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L567-L663)）；关键是操作码 SUB、B 寄存器 = R2、目的 = R1 都对得上，证明你的手工编码正确。若工具链尚未就绪，第三步的源码交叉验证已足以确认结果。

> 说明：本讲所有十六进制结果都是按 spec 字段表与 `zopcodes.cpp` 掩码手工推导并交叉验证得到的；标注「待本地验证」的步骤需要你本地构建好工具链后实际运行确认。

## 6. 本讲小结

- ZipCPU 标准指令是 **32 位定长**，核心字段为：保留位(31) / DR(30–27) / OpCode(26–22) / Cnd(21–19) / OpB 选择位(18) / BR(17–14) / 立即数(13–0)。
- **5 位 OpCode 编码 29 条指令**，访存类整齐排列便于译码；同一助记符常因 Operand B 形态不同拆成多条「掩码 + 匹配值」表项。
- **Operand B 靠 bit 18 切换**两种形态：18 位立即数，或「BR + 14 位偏移」。
- `zopcodes.h` 的 `ZIP_REGFIELD / ZIP_IMMFIELD / ZIP_BITFIELD` 宏把「起始位 + 宽度 + 是否符号扩展」打包成描述符，`zip_getbits` 据此从指令字里抠字段。
- **3 位 Cnd 给出 8 种条件**，使几乎所有指令可条件执行；`LDI` 和 NOOP 组是例外。很多「常见指令」（CLR/BRA/BUSY 等）是派生指令。
- 综合实践：`SUB R1,R2 = 0x08048000`，可用掩码匹配规则与字段提取交叉验证。

## 7. 下一步学习建议

- **下一篇 u2-l3（寻址与 Load/Store）**：本讲的 Operand B 字段直接决定了寻址能力，下一篇会把这些编码落到真实的 `LW/SW/LH/SH/LB/SB` 指令和 `rtl/core/memops.v` 的访存信号上。
- **u2-l4（条件执行）**：深入练习如何用条件字段写出「无分支」的条件代码，以及汇编器如何在 `LDI` 与 `BREV` 之间自动派生。
- **动手验证**：如果已构建工具链，试着把本讲的 `SUB R1,R2 = 0x08048000` 喂给 `zdump` 反汇编，或反过来用 `zasm` 汇编 `SUB R1,R2` 再查看其机器码，把「手工编码 ↔ 工具」这条闭环跑通。
- **进阶阅读**：浏览 `zopcodes.cpp` 里 CIS（16 位压缩指令）表项（[第 265 行起](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L265-L313)），预习压缩指令如何用 bit 31 = 1 复用同一套字段思想。
