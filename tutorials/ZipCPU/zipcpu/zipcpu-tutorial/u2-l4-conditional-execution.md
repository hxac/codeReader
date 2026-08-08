# 条件执行机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说明 ZipCPU 几乎所有指令都可以「条件执行」这一特性，理解 3 位条件码字段 `Cnd` 如何决定一条指令是否真正生效。
- 把 8 种条件（`.Z / .LT / .C / .V / .NZ / .GE / .NC` 以及「无条件」）与状态寄存器 `CC` 的四个标志位 `Z/C/N/V` 对应起来。
- 理解条件指令「不冲刷流水线」「可串联」的运行时语义，以及 `CMP`/`TST` 为何是例外。
- 掌握「修正条件（Modifying Conditions）」技巧——如何用调整比较指令的方式造出 ZipCPU 没有直接提供的条件。
- 看懂「派生指令（Derived Instructions）」，特别是为什么汇编器会把条件 `LDI` 悄悄展开成 `BREV`+`LDILO` 两条指令。

本讲承接 [u2-l2 指令格式与编码](u2-l2-instruction-format-encoding.md)，专注「条件」这一个主题；它也是后续阅读 [u3-l1 zipcore 流水线](u3-l1-zipcore-structure-pipeline.md) 中「条件分支为何产生停顿」的规范基础。

## 2. 前置知识

在进入本讲前，你需要先了解两个概念（它们在前两讲已建立）：

- **状态寄存器 `CC` 的标志位**：ZipCPU 的 `CC` 寄存器低 4 位是最近一次（设置了标志的）ALU 运算留下的结果标志——第 0 位 `Z`（零）、第 1 位 `C`（进位）、第 2 位 `N`（负）、第 3 位 `V`（溢出）。例如做减法得到 0，`Z` 就会被置 1。
- **指令格式中的字段**：标准指令是 32 位定长，其中 `DR`（目的寄存器，位 30–27）、`OpCode`（操作码，位 26–22）、`Cnd`（条件码，位 21–19）等字段已在前一讲拆解过。本讲的主角就是这个 `Cnd` 字段。

一个直白的类比：大多数 CPU 里，「判断条件」是「分支指令」的专利——你写 `if (x) goto label;`，CPU 靠一条条件跳转决定走不走那段代码。ZipCPU 的做法更激进：**几乎每条普通指令自己就带一个 3 位的「开关」**，开关关上时这条指令就像没执行过一样（不写寄存器、不算结果）。这就是「条件执行（conditional execution）」。

## 3. 本讲源码地图

本讲主要围绕规范与汇编器两侧的源码：

| 文件 | 作用 |
| --- | --- |
| `doc/src/spec.tex` | ISA 规范。本讲聚焦三个子节：*Conditional Instructions*、*Modifying Conditions*、*Derived Instructions*，以及 `CC` 标志位定义。 |
| `sw/zasm/zopcodes.cpp` | 操作码表 / 反汇编表。可看到 `BREV`、`LDILO` 表项里的条件字段描述符 `ZIP_BITFIELD(3,19)`，以及「不带条件」的 `LDI` 表项和 `BREV`+`LDILO` 配对识别函数。 |
| `sw/zasm/asmdata.cpp` | 汇编器把条件 `LDI` 展开为 `BREV`+`LDILO` 的核心逻辑（`OP_LDI` 分支）。 |
| `sw/zasm/zparser.cpp` | `op_ldilo` / `op_brev` 等指令字的构造函数，辅助理解展开后两条指令如何编码。 |
| `sw/zasm/test.S` | 汇编测试用例，包含 `LDI`/`LDIHI`/`LDILO` 与 `BREV`+`LDILO` 配对的实测样例。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **条件执行机制与 8 种条件码**——`Cnd` 字段是什么、有哪些取值、如何对照 `CC` 标志。
2. **条件执行的运行时语义**——不冲刷流水线、可串联、`CMP`/`TST` 例外，以及「修正条件」技巧。
3. **派生指令与条件 `LDI` 的展开**——为什么 `LDI` 不能带条件，汇编器如何用 `BREV`+`LDILO` 补救。

### 4.1 条件执行机制与 8 种条件码

#### 4.1.1 概念说明

ZipCPU 的设计哲学之一是「让大多数指令自己携带条件」。规范在 *Conditional Instructions* 子节开篇就点明了这一点：

> Most, although not quite all, instructions may be conditionally executed.

也就是说，你在任意一条普通 ALU 指令、访存指令后面加上一个 `.Z` 或 `.NZ` 之类的后缀，这条指令就只在该条件成立时才真正写回结果。条件不成立时，它不写寄存器、也（通常）不动标志位，相当于「空转一条」。

这带来两个直接好处：

- **短小的 if/else 不必用分支**：避免分支带来的流水线停顿（下文会讲）。
- **可串联的条件序列**：一连串带相同条件的指令可以一起做或不做，像「逻辑与」一样叠加判断。

规范同时列出了**例外**：23 位立即数加载 `LDI`，以及特殊指令 `NOOP`、`SIM`、`BREAK`、`LOCK`，这几条**不能**条件执行。为什么 `LDI` 是例外，是本讲模块 4.3 的核心，这里先记住结论。

#### 4.1.2 核心流程

条件执行的判定逻辑可以写成一行：在指令进入执行阶段时，硬件用 `Cnd` 字段（3 位）去查 `CC` 寄存器里的标志位，得到一个布尔值 `cond_ok`：

```
取指 → 译码（读出 Cnd）→ 用 Cnd 查表得到 cond_ok
                         → 若 cond_ok 为真：正常执行、写回
                         → 若 cond_ok 为假：不写回（结果丢弃），指令继续往下走
```

`Cnd` 字段只有 3 位，所以一共 8 个取值。规范 *Conditions for conditional operand execution* 表把这 8 个取值与 `CC` 标志的对应关系列得很清楚：

| `Cnd`（3 位） | 助记符 | 含义（何时执行） | 依据的 `CC` 标志 |
| --- | --- | --- | --- |
| `3'h0` | （无后缀） | 总是执行 | —— |
| `3'h1` | `.Z` | 结果为零时（Z=1） | 位 0 `Z` |
| `3'h2` | `.LT` | 小于时（N=1，有符号） | 位 2 `N` |
| `3'h3` | `.C` | 进位置位时（C=1，亦即无符号小于） | 位 1 `C` |
| `3'h4` | `.V` | 溢出时（V=1） | 位 3 `V` |
| `3'h5` | `.NZ` | 非零时（Z=0） | 位 0 `Z` |
| `3'h6` | `.GE` | 大于等于时（N=0，有符号） | 位 2 `N` |
| `3'h7` | `.NC` | 无进位时（C=0，亦即无符号大于等于） | 位 1 `C` |

一个关键观察：**条件字段只直接测试 4 个标志中的每一个**，而且并不是所有组合都有。例如有 `.Z`/`.NZ`，但没有「小于等于 `.LE`」「大于 `.GT`」（无论有符号还是无符号），也没有「不溢出 `.NV`」。这些「缺失的条件」怎么办？见模块 4.2 的「修正条件」。

#### 4.1.3 源码精读

**条件字段的格式定义**。规范在指令格式图里把 `Cnd` 画成了一个独立的 3 位字段，位于操作码与 Operand B 选择位之间：

- [doc/src/spec.tex:649](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L649)：指令格式图中标注的 `Cnd` 字段（标准格式的第二行），明确它是 3 位。
- [doc/src/spec.tex:672-678](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L672-L678)：紧随其后的文字说明「某个由 `OpCode` 定义的操作，**当条件 `Cnd` 为真时**才执行，结果写入 `DR`」。这是条件执行的总纲。

**8 种条件表**。规范把上面的条件表放在 *Conditional Instructions* 子节：

- [doc/src/spec.tex:753-766](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L753-L766)：`tbl:conditions`，列出 8 个 `Cnd` 取值、助记符与含义。本模块那张对照表就是据此整理的。

**`CC` 标志位定义**（条件的判定来源）：

- [doc/src/spec.tex:456-461](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L456-L461)：`CC` 寄存器低 4 位——位 3 `V`、位 2 `N`、位 1 `C`、位 0 `Z`。条件字段就是去读这 4 位。

**汇编器/反汇编器如何描述这个字段**。在 `zopcodes.cpp` 的操作码表里，每条「可带条件」的指令表项都以一个 `ZIP_BITFIELD(3,19)` 结尾——这正是告诉反汇编器「条件是一个 3 位字段，起始位为 19」（即位 21–19）：

- [sw/zasm/zopcodes.cpp:156](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L156)：`BREV` 表项，最后一项 `ZIP_BITFIELD(3,19)` 即条件字段描述符。
- [sw/zasm/zopcodes.cpp:159](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L159)：`LDILO` 表项，同样以 `ZIP_BITFIELD(3,19)` 结尾。

对比之下，`LDI` 表项就**没有**条件描述符：

- [sw/zasm/zopcodes.cpp:361](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L361)：`LDI` 表项，掩码是 `0x80000700`（4 位操作码），最后一项是 `ZIP_OPUNUSED`——表项层面就体现了「`LDI` 不带条件」。

#### 4.1.4 代码实践

**实践目标**：手工从一条 32 位指令字里抠出 `Cnd` 字段，并判断它的条件含义。

**操作步骤**：

1. 拿到下面这条已经编码好的指令字：`0x10A80001`。它对应的汇编写法是 `ADD.NZ 1,R2`（「当 Z=0 时，把 R2 加 1」）。
2. 把 `0x10A80001` 展成二进制，取出位 21–19。
3. 对照本模块的条件表，确认这个 3 位值对应哪个条件。

**需要观察的现象**：

- 位 21–19 = `101`（二进制）= `3'h5`。
- 查表得 `3'h5` = `.NZ`，即「Z 标志为 0 时执行」。
- 同时位 26–22 = `00010` = 操作码 `ADD`，位 30–27 = `0010` = 目的寄存器 `R2`，位 0 = `1`（立即数 1）。

**预期结果**：`0x10A80001` 解码为 `ADD.NZ #1,R2`。这条指令只有在上一次运算结果非零（`Z=0`）时才会真正把 R2 加 1；否则它不写回、相当于空转。

> 说明：上面的编码结果待本地用 `zip-objdump` 反汇编验证；若你已按 [u1-l2](u1-l2-repo-layout-and-build.md) 构建好工具链，可把这条 `.word 0x10A80001` 放进一段汇编，再用 `zip-objdump -d` 查看反汇编结果是否为 `add.nz $1,r2`。

#### 4.1.5 小练习与答案

**练习 1**：条件 `.GE` 测试的是哪个 `CC` 标志？它和 `.LT` 是什么关系？
**答案**：`.GE`（`3'h6`）测试位 2 `N`，当 `N=0` 时执行；`.LT`（`3'h2`）同样测试 `N`，但当 `N=1` 时执行。二者互为「同一位的两种取值」。

**练习 2**：为什么条件表里没有 `.NV`（不溢出）这一项？
**答案**：因为 3 位 `Cnd` 字段只能编码 8 个取值，规范的设计者选择了「总是执行」+ 7 个常用条件，把溢出相关的 `.NV` 舍弃了。需要 `.NV` 这种条件的场合，要用模块 4.2 的「修正条件」技巧绕过（或者改写算法）。

---

### 4.2 条件执行的运行时语义：标志连锁与流水线

#### 4.2.1 概念说明

光知道「条件字段长什么样」还不够，要真正会用条件指令，必须理解它的两条运行时规则：

1. **条件指令不冲刷流水线**。这是条件执行相对条件分支最大的优势。一条「没被执行」的条件指令，不会像「预测错误的分支」那样把流水线里后续指令全部作废、产生若干个气泡周期；它只是安静地空转一条。
2. **条件指令默认不修改标志，可以串联**。规范明确：除了 `CMP` 和 `TST`，条件执行的指令**不会**进一步改动条件码。这样你才能把一连串带相同条件的指令排在一起，让它们要么全做、要么全不做，而不必担心中间某条把标志改掉、破坏后面的判定。

`CMP`/`TST` 的「例外」是有意为之：它们本来就是用来**设置**标志的，所以即便带条件、即便执行了，也会更新标志。规范甚至给出一个漂亮的例子——用两条带 `.Z` 的指令实现「R0 等于 1 **且** R1 等于 2」的复合判断，全程不用分支。

此外，本模块还覆盖**「修正条件（Modifying Conditions）」**：当你要的条件不在那 8 个里（比如 `.LE` 小于等于），可以通过微调比较指令的立即数或交换操作数顺序，「免费」地造出来——规范声称这样做「不增加任何额外时钟周期」。

#### 4.2.2 核心流程

**串联条件（逻辑与）的流程**，以「R0==1 且 R1==2 时执行某操作」为例：

```
CMP  1,R0        ; 计算 R0-1，按结果设标志（Z 反映 R0 是否等于 1）
CMP.Z 2,R1       ; 仅当上一行条件成立(Z=1)时才真正执行；执行则按 R1-2 重设标志
                 ; 若上一行不成立(Z=0)，本行不执行、标志保持不变(Z 仍为 0)
<某指令>.Z ...   ; 此时 Z=1 当且仅当「R0==1 且 R1==2」
```

关键点：第二行 `CMP.Z` 是 `CMP`，属于「例外」，所以它一旦执行就会**重新设置**标志；而它带 `.Z`，意味着只有前一个条件成立（Z=1）它才执行。于是最终 `Z=1` 等价于两个条件都成立——这就是「逻辑与」。规范特别说明，编译器比较 64 位整数时大量使用这一招。

**修正条件的原理**，以「有符号小于等于 `.LE`」为例。直接想要的条件是 \( \text{Ry} \le \text{Rx} \)，但 `Cnd` 里没有 `.LE`。注意到整数比较中：

\[
\text{Ry} \le \text{Rx} \;\Longleftrightarrow\; \text{Ry} < \text{Rx}+1
\]

于是把比较立即数加 1，改用已有的 `.LT`（小于）即可：

```
CMP  1+Imm,Ry    ; 而不是 CMP Imm,Ry
BLT  label       ; BLT 就是 ADD.LT 偏移,PC（派生指令，见模块 4.3）
```

这样 `.LE` 就用 `.LT` 实现了，没有额外指令、没有额外周期。规范用一张完整的「修正条件」表给出了 `.LE / .GT / .LEU / .GTU` 等所有缺失条件的等价写法。

#### 4.2.3 源码精读

**标志连锁与 `CMP`/`TST` 例外**：

- [doc/src/spec.tex:772-777](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L772-L777)：原文「With the exception of `CMP` and `TST` instructions, conditionally executed instructions will not further adjust the condition codes.」，并说明这使条件序列可以串联，形成「逻辑与」。
- [doc/src/spec.tex:781-795](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L781-L795)：`tbl:dbl-condition`，即上面那段「R0==1 且 R1==2」的双重条件示例。
- [doc/src/spec.tex:796-797](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L796-L797)：说明编译器比较 64 位数时大量依赖此能力。
- [doc/src/spec.tex:799-801](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L799-L801)：点明条件执行的最大实用价值——「unlike conditional branches, conditionally executed instructions will not clear the pipeline if they are not executed」。

**修正条件表**：

- [doc/src/spec.tex:808-836](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L808-L836)：`tbl:creating-conditions`，逐条给出 `.LE/.GT/.LEU/.GTU` 等缺失条件的「改比较指令」等价写法。
- [doc/src/spec.tex:843-844](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L843-L844)：说明其中很多等价写法由 ZipCPU 编译器自动选择。

#### 4.2.4 代码实践

**实践目标**：用条件指令（**不使用任何分支指令**）实现 `if (R1 != 0) R2 = R2 + 1;`，并解释它为何不产生分支停顿。

**操作步骤**：

1. 写出下面两行汇编：

   ```asm
   TST  R1,R1       ; 测试 R1 & R1：结果为 0 当且仅当 R1 == 0，据此置 Z
   ADD.NZ  1,R2     ; 仅当 Z == 0（即 R1 != 0）时，才把 R2 加 1
   ```

2. 分析执行路径：
   - 若 `R1 == 0`：`TST` 置 `Z=1`；`ADD.NZ` 条件不成立，**不写回** R2，相当于空转一条。
   - 若 `R1 != 0`：`TST` 置 `Z=0`；`ADD.NZ` 条件成立，`R2 ← R2 + 1`。
3. 把它和「传统写法」对比：传统做法是 `TST R1,R1; BNZ skip; ADD 1,R2; skip:`，用了条件分支 `BNZ`。

**需要观察的现象 / 预期结果**：

- 两种写法语义相同，但本写法**没有分支**，因此不会因为分支预测错误而冲刷流水线（参见规范的「条件分支产生 4 个停顿周期」之说，[doc/src/spec.tex:1636](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1636)）。
- 第二行 `ADD.NZ` 不是 `CMP`/`TST`，所以即便它执行了，也不会改动 `Z` 标志——这保证了若你后面还想基于「R1 是否为 0」继续做事，标志仍然有效。

> 说明：上述汇编的行为待本地在 [u1-l4](u1-l4-first-simulation.md) 介绍的模拟器中验证。可把这两行放入一段小程序，分别令 R1=0 与 R1=5 运行，检查 R2 是否只在后者加 1。

#### 4.2.5 小练习与答案

**练习 1**：用条件指令（不用分支）实现「若 `R0 > R1`（有符号）则 `R3 = R3 + R1`」。
**答案**：
```asm
CMP  R1,R0        ; 计算 R0-R1：R0>R1 时不会产生负号 → N=0
ADD.GE  R1,R3     ; .GE 在 N=0 时执行。注意 CMP R1,R0 后 N=0 等价于 R0>=R1；
                  ; 若要严格 >（大于），改用「修正条件」：CMP R0,R1 后用 .LT 取反，
                  ; 或按规范 tbl:creating-conditions 用 CMP R1,R0 + BLT 风格处理。
```
严格「大于」没有直接条件，需借助修正条件；本题若放宽为 `>=` 则上面 `ADD.GE` 即正确。

**练习 2**：为什么规范说 `CMP`/`TST` 是「会改标志的例外」？如果它们也像别的条件指令那样不改标志，双重条件示例还能成立吗？
**答案**：双重条件示例依赖第二行 `CMP.Z` 在执行后**重新设置**标志，才能把「第二个条件是否成立」反映到 `Z` 上。若 `CMP`/`TST` 也不改标志，第二行就无法传递新的判定结果，整条「逻辑与」链路就断了。所以这个「例外」是串联判断能够工作的前提。

---

### 4.3 派生指令与条件 `LDI` 的展开

#### 4.3.1 概念说明

ZipCPU 只有 29 条「原生」指令，但很多常见指令（`BRA/BNZ/CLR/NEG/NOT/JMP/JSR` 等）都能由这些原生指令**组合**出来，规范称之为「派生指令（Derived Instructions）」。其中和「条件」关系最密切的是两类：

1. **带条件的清零 `CLR.NZ Rx`**。原生 `CLR Rx` 实为 `LDI 0,Rx`，而 `LDI` 不能带条件；于是汇编器改用 `BREV.NZ 0,Rx`（`BREV` 是「按位反转」指令，反转 0 仍是 0，且 `BREV` **能**带条件）。规范原文说：汇编器会「根据是否存在条件，在 `LDI` 与 `BREV` 之间悄悄选择」。
2. **带条件的 32 位立即数加载**。原生 `LDI` 只能装 23 位有符号立即数，且**不带条件**。要装入完整 32 位、又要带条件，就必须拆成两条：`BREV.<cond>`（装高 16 位的「按位反转」）+ `LDILO.<cond>`（装低 16 位）。这就是本模块的核心——**汇编器把一条条件 `LDI` 展开成 `BREV`+`LDILO` 两条同条件指令**。

之所以需要 `BREV`，是个巧妙的编码技巧：`LDILO` 只能写低 16 位，要设置高 16 位，用 `BREV` 把「高 16 位的按位反转值」放进去，再配合 `LDILO` 写低 16 位，两次操作合起来正好把 32 位凑齐。规范在重定位一节也强调：「装入任意 32 位值的指令会被拆成 `BREV`+`LDILO` 一对」。

#### 4.3.2 核心流程

汇编器遇到一条 `LDI` 时（无论是否带条件），其判定流程见 `asmdata.cpp` 的 `OP_LDI` 分支，可以概括为：

```
读入：立即数 imm，条件 m_cond，目的寄存器 m_opa

if (bitreverse(imm) 能放进 18 位):
    → 输出单条 BREV m_cond, bitreverse(imm), m_opa     ; BREV 可带条件，单条搞定
elif (imm 放不进 23 位) 或 (m_cond 不是「无条件」):
    → 禁止装入 PC（否则报错 "Cannot LDI 32-bit addresses into PC register!"）
    → 输出两条指令，二者条件相同：
        BREV.<cond>  bitreverse(imm) 的高 18 位, m_opa
        LDILO.<cond> imm & 0x0ffff,        m_opa
else:
    → 输出单条原生 LDI imm, m_opa                       ; 23 位内、无条件：最普通情况
```

注意三个分支的触发条件：

- **第 1 分支**：值很小或「反转后」很小，单条 `BREV` 即可，且天然支持条件。
- **第 2 分支**：值需要 32 位，**或**带了条件——这两种情况都走两指令展开。
- **第 3 分支**：值在 23 位以内、且无条件，才用原生 `LDI`。

> 换句话说：**只要你给 `LDI` 加了条件**（哪怕值很小），汇编器就一定会把它展开（要么成单条 `BREV`，要么成 `BREV`+`LDILO`），因为原生 `LDI` 字段里根本没有 `Cnd` 位。

#### 4.3.3 源码精读

**派生指令表（规范）**：

- [doc/src/spec.tex:1354-1359](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1354-L1359)：`CLR.NZ Rx → BREV.NZ 0,Rx`，并注明「汇编器会根据是否存在条件，在 `LDI` 与 `BREV` 之间悄悄选择」。
- [doc/src/spec.tex:1394-1405](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1394-L1405)：`LDI val,Rx → BREV REV(val)&0x0ffff,Rx ; LDILO val&0x0ffff,Rx`，说明完整 32 位装入需要两周期，`LDILO` 正是为此与 `BREV` 配套而生。
- [doc/src/spec.tex:2423-2426](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2423-L2426)：重定位一节确认「装入任意 32 位值的指令会被拆成 `BREV`+`LDILO` 一对，待链接时填入立即数」。

**`BREV` / `LDILO` 原生操作码**：

- [doc/src/spec.tex:708-709](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L708-L709)：操作码表，`BREV`（5'h08，按位反转 B 操作数）与 `LDILO`（5'h09，装入立即数低半）。

**汇编器展开逻辑（核心）**：

- [sw/zasm/asmdata.cpp:383-401](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/asmdata.cpp#L383-L401)：`OP_LDI` 分支。第 384–386 行是「单条 BREV」分支；第 387 行的判断 `(!fitsin(imm, 23))||(m_cond != zp.ZIPC_ALWAYS)` 正是「值超 23 位 **或** 带了条件」的展开触发条件；第 392、396 行分别生成 `BREV.<cond>` 与 `LDILO.<cond>` 两条指令（注意二者都传入同一个 `m_cond`，所以条件相同）；第 399–400 行才是「原生 LDI」分支。

**展开后两条指令如何构造**：

- [sw/zasm/zparser.cpp:145-148](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zparser.cpp#L145-L148)：`op_ldilo(cnd, imm, a)`，把立即数截到低 16 位（`imm & 0x0ffff`）并编入 `LDILO` 操作码。
- [sw/zasm/zparser.cpp:188-192](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zparser.cpp#L188-L192)：`op_brev(cnd, imm, a)`，构造 `BREV` 指令字。

**反汇编器如何把这对指令「认回来」**：

- [sw/zasm/zopcodes.cpp:378-386](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zopcodes.cpp#L378-L386)：`TWOWORD_LOAD(one, two)` 函数，判定「`BREV` 紧跟 `LDILO`、且两者**目标寄存器相同、条件相同**」时，把它们识别为一次逻辑上的 32 位装入。第 383 行的 `(one^two)&0xf8380000)==0` 就是「同寄存器、同条件」的位级检查。

**实测样例**：

- [sw/zasm/test.S:155-157](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/test.S#L155-L157)：测试程序里手工写出 `brev 0xb57b,r8` 与 `ldilo 0xbeef,r8` 配对，随后用 `cmp r7,r8; bnz test_failure` 验证它等价于一次 `0xdeadbeef` 的完整装入。

#### 4.3.4 代码实践

**实践目标**：说明汇编器对「条件 `LDI`」到底做了什么转换，并预测一段具体输入的输出。

**操作步骤**：

1. 阅读 [sw/zasm/asmdata.cpp:383-401](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/asmdata.cpp#L383-L401) 的 `OP_LDI` 分支，确认三个分支的触发条件。
2. 注意 `brev()` 会把 32 位全部按位反转（[zparser.cpp:66-73](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zparser.cpp#L66-L73)），所以即便很小的值（如 5）反转后也会变成很大的数。据此预测下面三条汇编各会展开成什么（按代码中三个分支的出现顺序排列）：

   | 输入汇编 | 预期输出（命中分支） |
   | --- | --- |
   | `CLR.Z R3`（即 `LDI.Z 0,R3`） | 单条 `BREV.Z 0,R3`（**第 1 分支**：`brev(0)=0` 能放进 18 位） |
   | `LDI.Z 0x12345678,R3`（32 位、带条件） | 两条：`BREV.Z <bitreverse 的高位>,R3` + `LDILO.Z 0x5678,R3`（**elif 分支**：带条件） |
   | `LDI 5,R3`（小立即数、无条件） | 单条原生 `LDI 5,R3`（**else 分支**：`brev(5)` 不在 18 位内、值在 23 位内且无条件） |

3. 解释「为什么」：原生 `LDI` 的字段布局（4 位操作码 + 23 位立即数）里**没有 `Cnd` 位**，所以一旦带条件，就必须改用能带条件的 `BREV`/`LDILO` 来表达。

**需要观察的现象 / 预期结果**：

- 三种输入依次命中「第 1 分支 / elif 分支 / else 分支」，输出指令条数依次为 **1、2、1**。
- 第 2 种（32 位带条件）里，`LDILO` 那条的低 16 位立即数是 `0x12345678 & 0x0ffff = 0x5678`（见 [zparser.cpp:146](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zparser.cpp#L146) 的 `imm & 0x0ffff`）。
- 展开后的两条指令**都带同一个条件 `.Z`**——这是 `TWOWORD_LOAD` 能把它们「认回成一次装入」的前提。

> 说明：上表的精确立即数（尤其 `BREV` 那条的「反转后的高位」）待本地用 `zasm`/`zip-as` 汇编后、用 `zip-objdump -d` 反汇编核对；本实践重点在「条件 `LDI` 必然被展开、且展开后各指令条件一致」这一规律。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CLR Rx`（无条件清零）可以压缩成 CIS 压缩指令，而 `CLR.NZ Rx`（带条件清零）不行？（提示：见规范关于压缩指令与条件的说明）
**答案**：规范在 [doc/src/spec.tex:1047](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1047) 明确「压缩指令不支持条件执行」。`CLR Rx` 实为 `LDI 0,Rx`，是无条件的，可压缩；而 `CLR.NZ Rx` 被展开成 `BREV.NZ 0,Rx`，带了条件，自然无法压缩。

**练习 2**：如果汇编器**不**做 `LDI → BREV+LDILO` 的展开，程序员要装入一个带条件的 32 位常量，会有什么麻烦？
**答案**：原生 `LDI` 只能装 23 位且不带条件，程序员只能手写两条 `BREV.<cond>`/`LDILO.<cond>`，还要自己算「高 16 位的按位反转值」和「低 16 位」，既易错又啰嗦。汇编器的展开让上层代码可以用统一的 `LDI.<cond> val,Rx` 写法，由工具自动处理拆分与编码。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个任务（即本讲规格中的综合实践）：

> 用条件指令实现 `if (R1 != 0) R2 = R2 + 1;`（**不用分支指令**），并说明汇编器对「条件 `LDI`」做了什么转换。

**第 1 步——条件判断部分（模块 4.1 + 4.2）**：

```asm
TST     R1,R1        ; 设标志：Z = (R1 == 0)
ADD.NZ  1,R2         ; R1 != 0 时（Z=0）才把 R2 加 1
```

请回答：这里 `.NZ` 对应 `Cnd` 的哪个 3 位值？它测试的是 `CC` 的哪一位？（答：`3'h5`，测试位 0 `Z`，要求 Z=0。）

**第 2 步——把它扩展成「带条件的常量加载」**：把上面的 `ADD.NZ 1,R2` 换成「当 R1!=0 时，把一个 32 位常量 `0xCAFE0000` 装入 R2」。你会写成：

```asm
TST        R1,R1
LDI.NZ     0xCAFE0000,R2     ; 注意：这是「带条件的 LDI」
```

**第 3 步——解释汇编器的转换（模块 4.3）**：第 2 步里的 `LDI.NZ 0xCAFE0000,R2` 是一条「带条件的 LDI」。根据 [asmdata.cpp:383-401](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/asmdata.cpp#L383-L401)，汇编器不会输出原生 `LDI`（它没有 `Cnd` 位），而是把它展开为：

```asm
BREV.NZ   <bitreverse(0xCAFE0000) 的高位部分>,R2
LDILO.NZ  0x0000,R2                      ; 0xCAFE0000 & 0x0ffff = 0x0000
```

两条指令**都带 `.NZ`**，条件与原意一致；二者配对后，反汇编器（`TWOWORD_LOAD`）还能把它们识别为一次 32 位装入。

**验收**：

- 能讲清 `.NZ = 3'h5`、测试 `Z` 位；
- 能讲清条件 `LDI` 因原生 `LDI` 无 `Cnd` 位而被展开为 `BREV.<cond>`+`LDILO.<cond>`，二者条件相同；
- （可选）在 [u1-l4](u1-l4-first-simulation.md) 的模拟器中跑第 1 步，确认 R1=0 时 R2 不变、R1≠0 时 R2 自增。

## 6. 本讲小结

- ZipCPU **几乎所有指令都可条件执行**：3 位 `Cnd` 字段（指令位 21–19）决定一条指令是否真正写回结果；`LDI` 与 `NOOP/SIM/BREAK/LOCK` 是例外。
- 8 种条件直接测试 `CC` 的 4 个标志位：`.Z/.NZ`→`Z`、`.LT/.GE`→`N`、`.C/.NC`→`C`、`.V`→`V`；其中没有 `.LE/.GT/.NV` 等组合。
- 条件指令**不冲刷流水线**，且除 `CMP`/`TST` 外**不改标志**，因此可串联成「逻辑与」式的复合判断，编译器比较 64 位数时大量使用。
- 缺失的条件可用「修正条件」技巧免费造出：调整比较立即数或交换操作数，把 `.LE` 等改写成已有的 `.LT` 等。
- 「派生指令」让 29 条原生指令覆盖大量常见操作；其中 `CLR.NZ → BREV.NZ`、条件 `LDI → BREV.<cond>+LDILO.<cond>` 是条件相关的关键展开。
- 汇编器在 `OP_LDI` 分支（[asmdata.cpp:383-401](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/asmdata.cpp#L383-L401)）按「能否单条 `BREV` / 是否带条件或超 23 位 / 否则原生 `LDI`」三路展开，反汇编器用 `TWOWORD_LOAD` 把 `BREV`+`LDILO` 认回成一次装入。

## 7. 下一步学习建议

- 读完本讲后，建议进入 **[u2-l5 中断处理与双寄存器组](u2-l5-interrupts-dual-regset.md)**，看条件标志与 `CC` 的控制位如何在中断进入/返回时被硬件保存与切换。
- 之后进入第 3 单元 **[u3-l1 zipcore 总体结构与流水线](u3-l1-zipcore-structure-pipeline.md)**，重点关注规范 *Pipeline Operation / Pipeline Stalls* 子节——你会看到本讲提到的「条件分支产生 4 个停顿周期」在硬件上是如何发生的，从而真正体会「条件执行不冲刷流水线」的价值。
- 若想立刻动手验证条件指令的编码，可回到 **[u2-l2 指令格式与编码](u2-l2-instruction-format-encoding.md)** 复习 `ZIP_BITFIELD`/`ZIP_REGFIELD` 等字段描述符，再用 `zip-objdump` 反汇编本讲的示例指令。
