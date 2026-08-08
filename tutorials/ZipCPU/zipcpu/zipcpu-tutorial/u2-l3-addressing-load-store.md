# 寻址方式与 Load/Store/Mov

## 1. 本讲目标

本讲紧接 u2-l2（指令格式与编码），把视角从「一条指令长什么样」推进到「指令如何访问内存」。

读完本讲你应该能够：

1. 说清为什么 ZipCPU 是 **load/store 架构**，以及哪 6 条指令（`LW/SW/LH/SH/LB/SB`）真正会触碰内存。
2. 写出访存指令的地址是如何由 **Operand B** 形成的：寄存器 + 14 位偏移，或 18 位立即数；并能解释 `PC` 作为基址时立即数会被乘 4。
3. 说明 `MOV` 指令为什么是 OpB 编码的「例外」，以及它的 A/B 位如何让监管模式访问用户寄存器组。
4. 描述 ZipCPU 的内存模型：统一 32 位地址空间、内存映射外设、大端字节序、对齐要求，以及「本地总线」地址段。
5. 读懂 [rtl/core/memops.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v)，说清一次单字访存如何用 `o_wb_cyc` / `o_wb_stb` / `o_wb_we` / `o_wb_sel` 在 Wishbone 总线上表达出来。

---

## 2. 前置知识

本讲默认你已经学过 u2-l1（寄存器组与状态寄存器）和 u2-l2（指令格式与编码）。下面几条是承接要点，本讲不再重复证明：

- **32 位定长指令**。标准指令分为若干字段，其中 5 位 OpCode（26–22 位）、3 位条件码 Cnd（21–19 位）、4 位目的寄存器 DR（30–27 位）、4 位基址寄存器 BR（17–14 位）。
- **Operand B（OpB）**。大多数指令有一个 19 位的源操作数 OpB。当指令第 18 位为 0 时，OpB 是一个 **18 位有符号立即数**；为 1 时，OpB 是 **BR + 14 位有符号偏移**。
- **双模式与双寄存器组**。CPU 有 supervisor / user 两套寄存器组，模式切换即整体换组。
- **DR 同时充当源操作数 A**。所以 `ADD Rb,Ra` 的语义是 `Ra = Ra + OpB`，是「读旧值、写新值」。

如果你对上述任何一点陌生，建议先回看 u2-l2 再继续。

> 名词速查
> - **load/store 架构**：只有专门的取数（load）/存数（store）指令才能读写内存，运算指令只在寄存器之间工作。
> - **内存映射 I/O（memory-mapped I/O）**：外设寄存器被映射到普通内存地址，CPU 用访存指令即可访问，不需要专门的 I/O 指令。
> - **字节使能（byte enable / byte select）**：一次总线传输里，用每根线对应一个字节，标记当前这一笔真正要写（或要读）哪几个字节。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | ISA 规范。本讲重点读「Address Modes」「Move Operands」「Operand B」「Instruction Format」「Memory Architecture」几节，以及 OpCode 表中的 6 条访存指令。 |
| [rtl/core/memops.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v) | **单次访存单元**。把 CPU 内部的「读/写一个字节/半字/字」请求翻译成一笔 Wishbone 总线交易；负责地址形成、字节使能、对齐检查、本地/全局总线选择、应答与错误。它是 `pipemem`（流水线访存）和 `dcache`（数据缓存）等更复杂访存模块的「最小公共实现」，先把它读懂，后面几讲都好理解。 |

辅助验证用的真实汇编可在 [bench/asm/simtest.s](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/simtest.s) 与 [bench/asm/simuart.s](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/simuart.s) 中找到，本讲的汇编写法都以这两个文件为准。

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：

- 4.1 Load/Store 架构与六条访存指令
- 4.2 寻址方式：Operand B 如何形成地址
- 4.3 `MOV` 指令的特殊 operand 形式
- 4.4 内存模型、字节序与对齐
- 4.5 `memops.v`：单次访存的硬件实现

### 4.1 Load/Store 架构与六条访存指令

#### 4.1.1 概念说明

很多初学者会问：CPU 不是「算东西」的吗，为什么还要专门讨论「访问内存」？

因为内存离 ALU 很远：寄存器在 CPU 内部、一个时钟就能读到；而内存在 CPU 外面，要通过**总线**（Wishbone / AXI）才能访问，一次访问可能要花好几个时钟。为了保持流水线简单、时钟频率高，RISC 设计通常遵守一条铁律：

> **运算指令只碰寄存器，只有专门的访存指令才碰内存。**

这就是 **load/store 架构**。ZipCPU 明确把自己定义为这种架构：

- 规范原文：[doc/src/spec.tex:284-291](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L284-L291) ——「A Load/Store architecture. Only load and store instructions …」「There are no I/O instructions. Attached peripherals are memory …」。

这句话有两层含义：

1. 只有访存指令访问内存；
2. **没有专门的 I/O 指令**，外设也被当作内存地址来访问（内存映射 I/O）。所以「读写一个外设寄存器」和「读写一段 RAM」在指令层面是同一件事，区别只在地址。

#### 4.1.2 核心流程

ZipCPU 一共只有 **6 条访存指令**，按「读/写」与「宽度」两个维度整齐排列：

| 读（load） | 写（store） | 宽度 | 读写后高位如何处理 |
| --- | --- | --- | --- |
| `LW` | `SW` | 32 位字 | 整个 32 位 |
| `LH` | `SH` | 16 位半字 | load 时「清高位」（零扩展到 32 位） |
| `LB` | `SB` | 8 位字节 | load 时「清高位」（零扩展到 32 位） |

执行流程上，一次访存指令大致经历：

1. **译码**：从指令字里取出 OpCode、OpB（地址）、目的/源寄存器 Ra。
2. **计算地址**：把 OpB 解析成有效地址（见 4.2）。
3. **发起总线交易**：访存模块（`memops`/`pipemem`/`dcache`）在总线上发起一次读或写。
4. **收尾**：读到数据则（按需要截取/扩展后）写回 Ra；写则等总线应答；期间流水线可能需要停顿等待。

注意一个细节：**load 是「读出后清高位」（零扩展），而不是符号扩展**。规范对 `LH` 的描述是「clear upper 16 bits」，对 `LB` 是「clear upper 24 bits」。

#### 4.1.3 源码精读

六条访存指令的 OpCode 表（注意它们的编码是连续排列的，便于译码）：

[doc/src/spec.tex:719-724](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L719-L724) 给出：

```
5'h12 & LW & Load a 32-bit word from memory (OpB) into Ra
5'h13 & SW & Store a 32-bit word from Ra into memory at (OpB)
5'h14 & LH & Load 16-bits from memory (opB) into Ra, clear upper 16 bits   (sets N)
5'h15 & SH & Store the lower 16-bits of Ra into memory at (OpB)
5'h16 & LB & Load 8-bits from memory (OpB) into Ra, clear upper 24 bits
5'h17 & SB & Store the lower 8-bits of Ra into memory at (OpB)
```

要点：

- 地址统一由 **OpB** 给出（表里的 `(OpB)`）。
- 读是把内存里的值放进 `Ra`；写是把 `Ra` 的低若干位写进内存。
- 表末列「Sets CC」标注哪些指令会改条件码：`LH` 会设置 `N`（因为它是 16 位加载，按规范会更新标志位以反映结果），`LB/SW/SH/SB/LW` 这一列基本为空（store 类不写寄存器，自然不影响 ALU 标志）。

> 这张表回答了一个常见疑问：「ZipCPU 怎么访问一个字节？」——用 `LB`/`SB`，地址用 OpB 给出，字节在 32 位总线上的具体位置由 4.4、4.5 讲的字节使能决定。

#### 4.1.4 代码实践

**目标**：从真实测试程序里认出 6 条访存指令的实际用法。

**步骤**：

1. 打开 [bench/asm/simtest.s](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/simtest.s)。
2. 找到把寄存器压栈/出栈的片段（约 659–697 行），你会看到一连串 `SW Rn,offset(SP)` 和对应的 `LW offset(SP),Rn`。
3. 打开 [bench/asm/simuart.s:42-49](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/simuart.s#L42-L49)，你会看到 `LB (r2),R4`、`LW 12(r3),R5`、`SW r4,12(r3)`。

**观察**：注意汇编里 load 和 store 的操作数顺序是**不对称**的：

- 读：`LW  <地址>, R目的`（地址在前，例如 `LW 12(r3),R5`）
- 写：`SW  R源, <地址>`（源在前，例如 `SW r4,12(r3)`）

这是一种「读起来顺」的助记写法：读作「从地址读入寄存器」，写也读作「把寄存器写进地址」。底层都是 `OpB, Ra`，只是汇编器对 store 做了换位。

**预期结果**：你能指认每条 `LW/SW/LH/SH/LB/SB` 分别对应表里的哪个 OpCode、读还是写、几个字节。

#### 4.1.5 小练习与答案

1. **问**：为什么 RISC CPU 不让 `ADD` 指令直接从内存取操作数（像 x86 那样）？
   **答**：那样每条运算指令都可能触发一次慢速访存，流水线要为不确定的内存延迟插入复杂逻辑，难以做到单周期发射和高时钟频率。load/store 架构把「访存」和「运算」解耦，运算指令永远只依赖寄存器（一拍可读），访存延迟被隔离在少数几条指令里。

2. **问**：`LH` 加载一个 16 位半字，为什么规范说「clear upper 16 bits」而不是符号扩展？
   **答**：ZipCPU 的 `LH` 设计为零扩展（高位补 0）。如果你需要符号扩展，要用额外指令（例如先 `TST`/比较再处理高位）实现。规范在 OpCode 表里明确写的就是 clear，这是设计取舍。

---

### 4.2 寻址方式：Operand B 如何形成地址

#### 4.2.1 概念说明

6 条访存指令的地址都来自 OpB。那么 OpB 能表达哪些「寻址方式」？答案只有两种，而且**没有专门的寻址模式字段**——它们复用了 u2-l2 讲过的 OpB 编码：

1. **寄存器 + 立即数偏移**：`BR + 14 位有符号偏移`（指令第 18 位 = 1）。这是最常见的「基址寻址」，例如栈操作 `4(SP)`。
2. **纯立即数**：`18 位有符号立即数`（指令第 18 位 = 0）。这是「立即寻址」，可直接给出一个绝对地址（只要落在 18 位有符号范围内）。

规范用一句话总结了这一点：

[doc/src/spec.tex:873-878](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L873-L878)（Address Modes 子节）：

> Load and store instructions use the OpB field for their address, whether source or destination. As a result, the ZipCPU can support both register plus immediate addressing, as well as a limited amount of immediate addressing.

注意「a **limited** amount of immediate addressing」——立即寻址受限于 18 位有符号立即数的范围 \([-131072, +131071]\)，更大的地址要先 `LDI` 装进寄存器再用基址寻址。

> 这与很多其它 RISC（如 RISC-V、OpenRISC 用 R0 表示零）不同：ZipCPU 不靠「某个寄存器恒为 0」来合成寻址模式，而是用指令里的一个比特位来切换「立即数 vs 寄存器+偏移」。详见 [doc/src/spec.tex:862-866](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L862-L866)。

#### 4.2.2 核心流程

地址形成的伪代码：

```
if 指令第18位 == 0:          # 立即寻址
    有效地址 = sign_extend_18(指令的 18 位立即数)
else:                         # 基址寻址
    基址 = REG[BR]            # 4 位 BR 字段选出基址寄存器
    偏移 = sign_extend_14(指令的 14 位立即数)
    有效地址 = 基址 + 偏移
```

**`PC` 作为基址时的特殊规则**：如果基址寄存器是 `PC`（R15），偏移量会先乘以 4 再相加。规范原文 [doc/src/spec.tex:356-358](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L356-L358)：

> If the source register, `Rb`, is the program counter register, `PC`, than the immediate will be multiplied by four prior to adding it's value to `PC` to generate the `Rb` value.

为什么乘 4？因为指令按 32 位（4 字节）对齐，用「指令条数」作偏移比用「字节数」更紧凑、也更好理解（跳过 N 条指令 = 偏移 N）。这条规则让 `LW offset(PC),Rx`、`MOV offset(PC),PC`（等价于相对跳转）都很自然——后两讲你会看到 `LW (PC),PC` 正是 ZipCPU 实现跳转/返回的核心手段。

#### 4.2.3 源码精读

OpB 的位分配（这是 4.2 一切的基础）：

[doc/src/spec.tex:854-861](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L854-L861) 用一张位域图给出 19 位 OpB 的两种打包：

```
位18=0 : [0] [ 18 位有符号立即数 ]                 # 立即寻址
位18=1 : [1] [4 位 Reg] [ 14 位有符号立即数 ]      # 基址寻址
```

完整指令格式见 [doc/src/spec.tex:635-671](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L635-L671)（Instruction Format 子节）。其中「Standard」格式里，bit 18 就是这个「立即/寄存器」切换位：为 0 时低 18 位是立即数，为 1 时低 18 位拆成 `4 位 BR + 14 位偏移`。

通用指令形式 [doc/src/spec.tex:337-349](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L337-L349) 写作 `OP.C  Rb+#Imm, Ra`——注意对于访存指令，`Rb+#Imm` 就是地址，`Ra` 是被读/写的寄存器。

#### 4.2.4 代码实践

**目标**：把两种寻址方式写成可汇编的代码，并指出地址来源。

**步骤**：下面是一段「示例代码」（非项目原有，仅为说明；语法取自 `bench/asm`）：

```asm
; (A) 基址寻址：把栈顶下一个字读进 R1
    LW   (SP), R1          ; 地址 = SP + 0   →  基址寻址（偏移=0）

; (B) 基址寻址带偏移
    LW   4(SP), R2         ; 地址 = SP + 4

; (C) 立即寻址：地址直接写成立即数
    LW   0x1000, R3        ; 地址 = 0x1000（18 位有符号立即数范围内）

; (D) 大地址必须先装入寄存器再基址寻址
    LDI  0x10000000, R4    ; R4 = 一个 18 位装不下的地址
    LW   (R4), R5          ; 地址 = R4
```

**观察与预期**：

- (A)(B) 走「基址寻址」路径：BR=SP，偏移分别是 0 和 4。
- (C) 走「立即寻址」路径：OpB 直接是 0x1000（4096，落在 18 位有符号范围内，合法）。
- (D) 因为 0x10000000 超出 18 位立即数范围，必须先用 `LDI`（23 位有符号立即数指令）装进寄存器，再用基址寻址。这也是为什么模拟器常把 RAM 放在 `0x10000000` 这类「大地址」上（见 u1-l4）。
- 待本地验证：用 `zip-gcc` 或 `zasm` 汇编上述片段，反汇编确认 (C) 是否真的编码成「bit18=0 + 18 位立即数」。

#### 4.2.5 小练习与答案

1. **问**：`SW R6, 8(R5)` 的有效地址由哪些字段决定？指令第 18 位是 0 还是 1？
   **答**：地址 = `R5 + 8`。BR 字段 = R5，14 位偏移 = 8，因此指令第 18 位 = 1（基址寻址）。

2. **问**：为什么 `LW 0x40000, R1`（0x40000 = 262144）无法用立即寻址，而 `LW 0x1000, R1` 可以？
   **答**：18 位有符号立即数范围是 \([-131072, +131071]\)。0x1000 = 4096 在范围内；0x40000 = 262144 超出范围，必须改用 `LDI` + 基址寻址。

3. **问**：`LW 1(PC), R2` 实际访问的地址比当前 PC 大多少字节？
   **答**：大 4 字节。因为基址是 PC 时，偏移会先乘 4：\( 1 \times 4 = 4 \)。

---

### 4.3 `MOV` 指令的特殊 operand 形式

#### 4.3.1 概念说明

`MOV`（OpCode = `5'hd`）是 OpB 编码的**唯一例外**。它不遵守「bit18 切立即/寄存器」的规则，而是把那两个比特（标准格式图里标为 A、B 的位）挪作他用：**选择源/目的寄存器属于用户组还是监管组**。

为什么需要这个例外？回忆 u2-l1：中断/异常时 CPU 在用户组和监管组之间整体切换，正常指令只能看到「当前组」。但监管代码（如上下文切换、调试器）经常需要读写**另一组**的寄存器（比如保存被中断的用户现场）。`MOV` 就是为此设计的「跨组搬运」指令。

[doc/src/spec.tex:879-904](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L879-L904)（Move Operands 子节）说明：

- B 位 = 1 → 源操作数取自**用户**寄存器组；0 → 当前组。
- A 位 = 1 → 目的操作数在**用户**寄存器组；0 → 当前组。
- 用户模式下这些位被忽略。

#### 4.3.2 核心流程

`MOV` 的源操作数形式被刻意简化：

```
源 = REG[BR] + 13 位有符号偏移        # 只有「寄存器+偏移」，没有纯立即数形式
目的 = Ra（A 位决定是否用户组）
```

也就是说，`MOV` 没有「18 位立即数」这种 OpB 形式。原因规范讲得很直白：**加载立即数已经有 `LDI` 了**，`MOV` 没必要再重复这个能力，省下的位拿来选寄存器组更划算（[doc/src/spec.tex:895-898](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L895-L898)）。

由此带来一个有趣的副作用：因为 `MOV` 的源是「寄存器 + 偏移」，它实际上可以当成一条**不影响条件码的三操作数 ADD** 来用——`MOV Rb+off, Ra` 等价于 `Ra = Rb + off`，但不改 Z/N/C/V。编译器在做地址计算时常用这一招（[doc/src/spec.tex:900-903](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L900-L903)）。

#### 4.3.3 源码精读

指令格式图里 `MOV` 单独画了一行 [doc/src/spec.tex:653-659](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L653-L659)：

```
MOV : [0][DR(4)] [5'hd] [3] [A(1)] [BR(4)] [B(1)] [13 位有符号立即数]
```

对比「Standard」格式 [doc/src/spec.tex:642-652](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L642-L652)，你能看到 `MOV` 把标准格式里 bit 13 和 bit 18 的位置改成了 A、B 两个「组选择」位，并把立即数压到 13 位。规范也在 [doc/src/spec.tex:679-690](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L679-L690) 明确点名 `MOV` 是「exceptions to this general instruction model」之一。

#### 4.3.4 代码实践

**目标**：理解 `MOV` 的两个用途（跨组搬运、当地址加法）。

**步骤**：阅读下面「示例代码」并回答问题。

```asm
; 监管模式下，把用户组的 R5 读到监管组的 R1
; （汇编器提供特殊助记符访问跨组 MOV，这里用注释示意语义）
    MOV.uR5, R1            ; 源=用户R5(B=1)，目的=当前(监管)R1(A=0)

; 用 MOV 当「不改条件码的 ADD」做地址计算
    MOV  R6+16, R7         ; R7 = R6 + 16，且不影响 Z/N/C/V
```

**观察与预期**：

- 第一条：只有在 supervisor 模式下，B 位才生效；若在 user 模式，B 位被忽略。
- 第二条：因为 `MOV` 不写条件码，常被编译器插在「需要保住当前比较结果」的地址计算里。
- 待本地验证：在 `bench/asm` 里搜索跨组 MOV 的真实用法（例如上下文切换保存用户现场处），确认汇编器写法。

#### 4.3.5 小练习与答案

1. **问**：为什么 `MOV` 没有「18 位纯立即数」形式？
   **答**：加载立即数是 `LDI` 的职责（它能装 23 位有符号立即数）。`MOV` 把那一位省下来用作「寄存器组选择」，避免功能重复。

2. **问**：`MOV R6+16, R7` 和 `ADD 16, R7`（假设 R7 此时已是 R6）有何区别？
   **答**：语义上都得到 `R6+16`，但 `ADD` 会改条件码（Z/N/C/V），`MOV` 不会。当你刚做完一次比较、不希望地址计算破坏标志位时，用 `MOV` 更合适。

---

### 4.4 内存模型、字节序与对齐

#### 4.4.1 概念说明

知道了「地址怎么算」，还要知道「CPU 眼里的内存长什么样」。规范在 Memory Architecture 一节给出三点关键约定（[doc/src/spec.tex:1789-1864](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1789-L1864)）：

1. **统一 32 位地址空间**。CPU 不区分片上/片外内存，也不区分内存与外设（数据缓存除外）——一切靠地址说话。
2. **大端字节序（big endian）**。总线一次传 32 位时，最高字节（MSB）放在最低总线地址。
3. **内存映射外设 + 本地总线段**。在 `ZipSystem` 封装里，地址最高 8 位（`addr[31:24]`）全为 1（即 `0xFFxxxxxx`）的那一段，保留给片上外设（定时器、中断控制器、看门狗等），叫**本地总线（local bus）**。其它封装会把这类地址直接转发给外部内存。

#### 4.4.2 核心流程

**字节序**：对于一个 32 位字 `0xAABBCCDD`，大端存放在地址 `A` 处时：

| 字节地址 | A | A+1 | A+2 | A+3 |
| --- | --- | --- | --- | --- |
| 内容 | AA | BB | CC | DD |

即 MSB（`AA`）在最低地址。这直接影响 4.5 里字节使能的取值。

**对齐**：ZipCPU 要求访存地址按访问宽度对齐，否则触发对齐错误（由 `OPT_ALIGNMENT_ERR` 控制）：

- 字（32 位）访问：地址必须 4 字节对齐（低 2 位 = 00）。
- 半字（16 位）访问：地址必须 2 字节对齐（最低位 = 0）。
- 字节（8 位）访问：任意地址都行。

字节地址到「字地址 + 字节内偏移」的换算（总线宽度为 32 位时，\( B = 4 \) 字节/字）：

\[
A_{\text{word}} = \left\lfloor \frac{A_{\text{byte}}}{B} \right\rfloor, \qquad \text{lane} = A_{\text{byte}} \bmod B
\]

#### 4.4.3 源码精读

内存模型与本地总线段：

[doc/src/spec.tex:1836-1851](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1836-L1851)：说明地址空间是「uniform 32-bit address space」，并指出 `ZipSystem` 把 `addr[31:24]` 全 1 的地址留给本地外设，其它封装则把这些地址转发给外部内存。

[doc/src/spec.tex:1808](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1808)：明确「the ZipCPU is big endian in how it uses the bus」。AXI 的字节序问题与 `SWAP_WSTRB` 选项见 [doc/src/spec.tex:1810-1834](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1810-L1834)，本讲了解即可（AXI 封装在 u4-l3 详讲）。

#### 4.4.4 代码实践

**目标**：在 `memops.v` 里亲眼看到「本地总线段」和「对齐检查」是怎么实现的。

**步骤**：

1. 打开 [rtl/core/memops.v:143-145](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L143-L145)，你会看到：

   ```verilog
   assign lcl_bus = (WITH_LOCAL_BUS)&&(i_addr[31:24]==8'hff);
   assign lcl_stb = (i_stb)&&( lcl_bus)&&(!misaligned);
   assign gbl_stb = (i_stb)&&(!lcl_bus)&&(!misaligned);
   ```

   这正是「最高字节 == 0xff → 本地总线」的硬件实现，与 spec 的内存模型一一对应。

2. 看对齐检查 [rtl/core/memops.v:122-138](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L122-L138)：

   ```verilog
   casez({ i_op[2:1], i_addr[1:0] })
   4'b01?1: r_misaligned = i_stb; // Words must be halfword aligned
   4'b0110: r_misaligned = i_stb; // Words must be word aligned
   4'b10?1: r_misaligned = i_stb; // Halfwords must be aligned
   default: r_misaligned = 1'b0;
   ```

**观察与预期**：把 `i_op[2:1]` 解释为宽度（01=字、10=半字、11=字节，见 4.5），你能用这张表逐条验证 4.4.2 的对齐规则：字访问地址低 2 位只要非 00 就算未对齐；半字访问最低位为 1 就算未对齐；字节访问永不未对齐。

#### 4.4.5 小练习与答案

1. **问**：一次 `LH` 访问地址 `0x1003`，会发生什么？
   **答**：`0x1003` 最低位为 1，半字访问要求 2 字节对齐，所以触发对齐错误（`misaligned=1`），`memops` 不会发起总线交易，而是直接返回 `o_err`（见 4.5）。

2. **问**：地址 `0xFF000010` 在 `ZipSystem` 里指向什么？
   **答**：`addr[31:24] = 0xFF`，落在本地总线段，访问的是片上外设（具体是哪个外设由 `ZipSystem` 内部地址译码决定，u4-l2 会讲），而不是外部内存。

---

### 4.5 `memops.v`：单次访存的硬件实现

前面四节都在讲「规范」，本节进入实现：`memops.v` 是 ZipCPU 里**最朴素的访存单元**——一次只做一笔交易，不能流水线重叠。它是理解 `pipemem`（u3-l6）和 `dcache`（u3-l6）的基础。

#### 4.5.1 概念说明

`memops` 要解决的问题是：把 CPU 流水线送来的一次「读/写一个字节/半字/字」的请求，翻译成一次合法的 **Wishbone** 总线交易，并把结果（或错误）回送给 CPU。它要处理：

- 读还是写、访问几个字节（来自 `i_op`）；
- 地址落在本地总线还是全局总线；
- 32 位总线只传「字」，子字访问要靠**字节使能** `o_wb_sel` 选中目标字节，并靠移位把数据摆正；
- 对齐错误要拦下来、不能上总线；
- 总线握手时序：什么时候拉高 `cyc`/`stb`，什么时候根据 `ack`/`err` 收尾。

注意文件头部的大端注释 [rtl/core/memops.v:14-16](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L14-L16)：本模块假设一条**大端**总线，MSB 对应最低总线地址。

#### 4.5.2 核心流程

**输入请求**（来自 CPU）—— [rtl/core/memops.v:65-79](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L65-L79)：

- `i_stb`：有访存请求。
- `i_op[2:0]`：操作码。`i_op[0]` = 读(0)/写(1)；`i_op[2:1]` = 宽度，**01=字、10=半字、11=字节**（这一点同时由 [zipcore.v 的调试打印](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L3778-L3787) `LW=010/LH=100/LB=110` 和 `memops` 内部的字节使能表印证）。
- `i_addr[31:0]`：有效地址。
- `i_data`：要写的数据（写时有效）。
- `i_oreg`：结果要写回的目标寄存器号。

**输出到 Wishbone** —— [rtl/core/memops.v:82-90](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L82-L90)：每组信号都分「全局 `_gbl` / 本地 `_lcl`」两套（因为本地外设和外部内存走两条物理总线）：

| 信号 | 含义 |
| --- | --- |
| `o_wb_cyc_gbl/lcl` | **cycle**：一次交易的「总开关」，拉高期间表示交易未结束 |
| `o_wb_stb_gbl/lcl` | **strobe**：请求这一拍进行传输 |
| `o_wb_we` | 写使能（1=写，0=读） |
| `o_wb_addr` | 字地址（已丢掉低位字节偏移） |
| `o_wb_data` | 要写的数据（已摆放到正确字节通道） |
| `o_wb_sel` | 字节使能（4 位，每位对应 32 位里的 1 字节） |

**单次访问时序**（读为例）：

```
1. i_stb=1, i_op=010(LW), i_addr=A, i_oreg=目标寄存器
2. 若 A 未对齐      → o_err=1，不上总线，结束
   若 A[31:24]==0xff → 走本地总线 (lcl)，否则走全局 (gbl)
3. 拉高 o_wb_cyc_* 与 o_wb_stb_*，给出 o_wb_addr/o_wb_sel，o_wb_we=0
4. 等待 i_wb_ack（或 i_wb_err）
   - ack 到来：o_valid=1，o_result=按宽度截取后的数据，o_wreg=目标寄存器
   - err 到来：o_err=1
5. 拉低 cyc/stb，交易结束；CPU 把 o_result 写回寄存器堆
```

写时序类似，区别是 `o_wb_we=1`、要给出 `o_wb_data`，且完成**不产生 `o_valid`**（写不回寄存器）。

#### 4.5.3 源码精读

**(a) 读写与宽度：`o_wb_we`**

[rtl/core/memops.v:253-256](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L253-L256) 直接把 `i_op[0]` 接到写使能：

```verilog
always @(posedge i_clk)
if (i_stb)
begin
    o_wb_we   <= i_op[0];   // 0=读, 1=写
```

**(b) 字节使能 `o_wb_sel`（关键！）**

`oword_sel` 由 `{大端/小端, i_op[2:1], i_addr[1:0]}` 查表得到，大端部分见 [rtl/core/memops.v:205-225](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L205-L225)：

```verilog
casez({ OPT_LITTLE_ENDIAN, i_op[2:1], i_addr[1:0] })
5'b00???: oword_sel[3:0] = 4'b1111;   // 字：全选（4 个字节都要）
5'b0100?: oword_sel[3:0] = 4'b1100;   // 半字, addr[1]=0：高 2 字节
5'b0101?: oword_sel[3:0] = 4'b0011;   // 半字, addr[1]=1：低 2 字节
5'b01100: oword_sel[3:0] = 4'b1000;   // 字节, addr[1:0]=00：最高字节
5'b01101: oword_sel[3:0] = 4'b0100;   // 字节, addr=01
5'b01110: oword_sel[3:0] = 4'b0010;   // 字节, addr=10
5'b01111: oword_sel[3:0] = 4'b0001;   // 字节, addr=11：最低字节
```

对照 4.4 的大端布局：`4'b1000` 选中最低地址的那个字节，正是 MSB 字节，符合大端约定。`o_wb_sel` 最终在 [rtl/core/memops.v:297-304](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L297-L304) 被寄存输出。对全局总线，`o_wb_addr` 取 `i_addr[WBLSB +: AW]`（丢掉低位字节偏移，得到字地址），见 [rtl/core/memops.v:300-303](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L300-L303)（`WBLSB = $clog2(BUS_WIDTH/8)`，32 位总线时为 2）。

**(c) cycle 与 strobe 的产生**

[rtl/core/memops.v:150-168](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L150-L168)：`r_wb_cyc_gbl/lcl` 在新请求到来时拉高，在收到 `i_wb_ack` 或 `i_wb_err` 时拉低——这就是「cycle 在整个交易期间保持高」。

[rtl/core/memops.v:173-183](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L173-L183)：`o_wb_stb_gbl` 在请求到来时拉高；若从机用 `i_wb_stall` 反压，则保持 `stb` 直到不 stall——这是 Wishbone「pipelined」握手的标准写法（stb 随 stall 保持）。

经 `OPT_LOCK` 处理后，`o_wb_cyc_gbl/lcl` 在 [rtl/core/memops.v:462-463](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L462-L463) 对外输出。

**(d) 应答、错误与忙**

- `o_valid`：[rtl/core/memops.v:316-325](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L316-L325) ——交易中、收到 `ack`、且不是写时，拉高一个节拍，表示「读数据有效」。
- `o_err`：[rtl/core/memops.v:327-339](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L327-L339) ——总线返回 `i_wb_err`，或请求**未对齐**（`misaligned`）时拉高。注意未对齐请求根本不会上总线，直接以 `o_err` 收尾。
- `o_busy`：[rtl/core/memops.v:341](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L341) ——只要 cycle 还在就忙；CPU 流水线据此停顿等待。

#### 4.5.4 代码实践（本讲主实践）

**目标**：把一段汇编与 `memops.v` 的总线信号一一对应起来。任务（来自讲义规格）：用汇编写出「把内存地址 `0x1000` 处的字加载到 R1、再加立即数 5 后写回」的序列，并指出 `o_wb_cyc` / `o_wb_stb` 等信号如何表达这次访问。

**步骤 1：汇编序列（示例代码，语法取自 `bench/asm`）**

```asm
    LDI  0x1000, R2      ; R2 = 0x1000（用基址寻址最稳妥）
    LW   (R2), R1        ; R1 = mem[0x1000]          ← 读一次字
    ADD  5, R1           ; R1 = R1 + 5
    SW   R1, (R2)        ; mem[0x1000] = R1           ← 写一次字
```

> 也可以写成 `LW 0x1000,R1`（立即寻址，因为 0x1000 在 18 位有符号范围内）。这里采用 `LDI`+基址的写法，是因为它对任意地址都成立，且与 `bench/asm/simuart.s` 的写法一致。

**步骤 2：对应到 `memops.v` 的信号**

对 `LW (R2),R1` 这条读字指令，当流水线把请求送到 `memops` 时：

| 信号 | 取值 | 出处 |
| --- | --- | --- |
| `i_stb` | 1（有请求） | 输入端口 |
| `i_op` | `3'b010`（读 + 字宽 `[2:1]=01`） | 见 4.5.2 编码 |
| `i_addr` | `0x00001000` | OpB 计算出的有效地址 |
| `i_oreg` | R1 的寄存器号 | 译码给出 |
| 对齐检查 | `i_op[2:1]=01`、`i_addr[1:0]=00` → `misaligned=0`，放行 | [L122-138](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L122-L138) |
| 总线选择 | `i_addr[31:24]=0x00 ≠ 0xff` → 走**全局**总线：`gbl_stb=1`、`lcl_stb=0` | [L143-145](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L143-L145) |
| `o_wb_cyc_gbl` | 拉高（整个交易期间） | [L150-168](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L150-L168) |
| `o_wb_stb_gbl` | 拉高（请求传输） | [L173-183](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L173-L183) |
| `o_wb_we` | `0`（读，等于 `i_op[0]`） | [L253-256](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L253-L256) |
| `o_wb_addr` | `0x1000 >> 2 = 0x400`（字地址） | [L300-303](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L300-L303) |
| `o_wb_sel` | `4'b1111`（字，4 字节全选） | [L205-225](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L205-L225) |
| 收到 `i_wb_ack` | → `o_valid=1`，`o_result=读回的字`，`o_wreg=R1` | [L316-325](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L316-L325) |

随后 `ADD 5,R1` 不访存（纯寄存器运算）。

对 `SW R1,(R2)` 这条写字指令，差别仅在于：`i_op=3'b011`（写+字）、`o_wb_we=1`、`o_wb_data=R1` 的值（已按字宽摆好）、`o_wb_sel=4'b1111`；收到 `ack` 后交易完成，**不产生** `o_valid`（写不回寄存器）。

**步骤 3：需要观察的现象 / 预期结果**

- 这三条访存（1 读 + 1 写）之间插了一条 `ADD`，所以两次 `o_wb_cyc` 脉冲是**分开**的（`memops` 一次只做一笔交易，做完拉低 `cyc` 才能接下一笔——见 [L157-168](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L157-L168)）。
- 若把读改成 `LB (R2),R1`（读字节，`i_op=3'b110`），则 `o_wb_sel=4'b1000`、`o_result` 会把读回字节的值放到最低字节并「清高位」补零。
- 待本地验证：可在 Verilator 仿真（u1-l4 / u5-l3 讲的 `zipcpu_tb`）里运行这段汇编，在波形里抓 `memops` 的 `o_wb_cyc_gbl/o_wb_stb_gbl/o_wb_we/o_wb_sel`，确认与上表一致。

> **关于 `o_wb_cyc` 与 `o_wb_stb` 的关系（一句话总结）**：`cyc` 是「这笔交易还没结束」的总标志，`stb` 是「这一拍请你传输」的请求。在 `memops` 这种单次实现里，二者通常同时拉高、同时随 `ack`/`err` 拉低；它们的区别要到 `pipemem`（流水线访存，u3-l6）里才会真正显现——那里 `stb` 可在一笔交易未结束时就为下一笔请求发起新的 strobe。

#### 4.5.5 小练习与答案

1. **问**：一条 `SB R3,(R4)`，已知 `R4=0x2001`。`o_wb_sel` 是什么？`o_wb_addr` 是什么？
   **答**：字节写在 `addr[1:0]=01`，大端字节使能查表得 `4'b0100`。`o_wb_addr = 0x2001 >> 2 = 0x800`（字地址）。`o_wb_data` 会把 `R3` 的低 8 位摆到对应字节通道上。
2. **问**：为什么 `memops` 在请求「未对齐」时选择不上总线、直接返回 `o_err`？
   **答**：未对齐访问语义上就非法（例如字读半个跨字的数据），与其在总线上产生一次含义不明的传输，不如在入口拦下，既节省总线带宽，也把错误更早暴露给 CPU（触发总线/对齐异常）。
3. **问**：`memops` 一次能处理几笔在途交易？依据是什么？
   **答**：最多 1 笔（`OPT_LOCK` 时也只允许极小的在途深度）。依据见形式化属性 [rtl/core/memops.v:603-621](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L603-L621)：「This core only ever has zero or one outstanding transaction(s)」。这正是它和 `pipemem` 的根本区别。

---

## 5. 综合实践

**任务：预测一段「小块复制」的全部总线行为。**

给定如下「示例代码」（语法取自 `bench/asm`）：

```asm
    LDI  0x1000, R1          ; 源地址
    LDI  0xFF000100, R2      ; 目标地址（落在本地总线段）
    LW   (R1), R3            ; 读 [0x1000]
    SW   R3, (R2)            ; 写 [0xFF000100]
    LH   4(R1), R4           ; 读半字 [0x1004]
    SH   R4, 4(R2)           ; 写半字 [0xFF000104]
    LB   0x1006, R5          ; 读字节（立即寻址）
```

请回答（可画一张时序表）：

1. 每一条访存指令分别走**本地**还是**全局**总线？依据 `i_addr[31:24]`。
2. 对每条访存，写出 `i_op[2:0]`、`o_wb_we`、`o_wb_sel`（按大端）、`o_wb_addr`（字地址）。
3. 如果把 `LH 4(R1),R4` 改成 `LH 5(R1),R4`（地址 `0x1005`），会发生什么？`o_wb_cyc` 还会拉高吗？
4. 这 4 笔访存之间没有数据依赖以外的停顿——用本讲知识解释：为什么 `memops` 仍然做不到「重叠」，而必须一笔一笔来？这一限制在哪个模块（u3-l6）被解除？

**参考思路**：

- 第 1 问：`0x1000`/`0x1004`/`0x1006` 的最高字节都是 `0x00` → 全局；`0xFF000100`/`0xFF000104` 最高字节 `0xFF` → 本地。
- 第 2 问：`LW`→`010/we=0/sel=1111/addr=0x400`；`SW`→`011/we=1/sel=1111`；`LH 4(R1)`→地址 `0x1004`、`addr[1]=0`→`sel=1100`、`addr=0x401`；`SH`→`101/we=1/sel=1100`；`LB 0x1006`→`110/we=0`、`addr[1:0]=10`→`sel=0010`、`addr=0x401`。
- 第 3 问：`0x1005` 最低位为 1，半字访问未对齐 → `misaligned=1` → **不上总线**（`o_wb_cyc` 不拉高），直接返回 `o_err`。
- 第 4 问：`memops` 任意时刻在途交易 ≤ 1（形式化属性已断言），所以必须等上一笔 `ack`/`err` 拉低 `cyc` 后才能发起下一笔。`pipemem`（u3-l6）允许在数据冒险允许的前提下把连续访存重叠到流水线里，从而提升吞吐。

---

## 6. 本讲小结

- ZipCPU 是 **load/store 架构**：只有 6 条访存指令（`LW/SW/LH/SH/LB/SB`）访问内存，且**没有 I/O 指令**——外设是内存映射的。
- 访存地址全部来自 **Operand B**：基址寻址（`BR + 14 位偏移`）或有限立即寻址（`18 位有符号立即数`）；`PC` 作基址时偏移自动乘 4。
- `MOV` 是 OpB 编码的唯一例外：用 A/B 位选择「用户/监管寄存器组」，只有「寄存器+偏移」源形式，且可当作不改条件码的「三操作数 ADD」用于地址计算。
- 内存模型是统一 32 位地址空间、**大端**字节序；`ZipSystem` 把 `0xFFxxxxxx` 段留给本地外设。访存必须按宽度对齐，否则 `memops` 直接返回 `o_err`。
- `memops.v` 把一次访存翻译成一笔 Wishbone 交易：`o_wb_cyc`（交易总开关）/`o_wb_stb`（请求）/`o_wb_we`（读/写）/`o_wb_sel`（字节使能，按大端查表）/`o_wb_addr`（字地址）；读在 `ack` 后产生 `o_valid`，未对齐或 `i_wb_err` 产生 `o_err`。

---

## 7. 下一步学习建议

- **继续 ISA 单元**：u2-l4（条件执行）会讲解 3 位条件码字段如何让几乎所有指令（包括本讲的访存指令，如 `SW.Z R0,(R2)`）条件化；u2-l5（中断与双寄存器组）会解释本讲 `MOV` 的「跨组」能力在中断现场保存里扮演的角色。
- **进入实现单元**：本讲的 `memops.v` 是 u3-l1（zipcore 流水线）里「执行/访存」阶段的核心部件之一，也是 u3-l6（访存模块族：`memops`/`pipemem`/`dcache`）的起点。要理解「为什么 `memops` 不够用、需要 `pipemem` 和 `dcache`」，本讲对单次交易时序的讲解是直接前置。
- **建议精读的源码**：先把本讲的 [rtl/core/memops.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v) 形式化属性部分（`fwb_master` 的用法，[L550-563](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L550-L563)）扫一遍，这会为 u5-l2（形式化验证）打下直觉。
