# 32 位指令编码格式 R/I/S

## 1. 本讲目标

本讲承接 u1-l4「全局参数 N/L/K 与寄存器类概念」。在上一讲里，我们知道了**一条 32 位指令会通过 `funct7[1:0]` 选择操作哪类寄存器（VX/VE/VR）**，但当时只看了这一个片段。本讲要把这「32 位指令字」整张地图摊开：

学完本讲，你应该能够：

- 画出 R 型、I 型、S 型三种指令格式在 32 位字里的位段布局，说出每个字段的位边界。
- 说清 `opcode` / `funct3` / `funct7` / `rd` / `rs1` / `rs2` / `rs3` 各自的作用，以及它们「分层选择」的关系。
- 用 `instrFormat.scala` 里的 `InstrBits` 常量，写出定位任意字段的 Scala 位运算表达式。
- 理解 I 型立即数与 S 型 FMA 字段如何**复用** R 型的 `funct7`/`rs2` 位段。
- 看懂 `Funct7Attrs` / `CvtFunct7` 两个 case class 如何把 `funct7` 的属性位打包/解包成一个整数。

> 本讲只讲「指令字长什么样」，不讲「opcode 有哪些家族」（那是 u2-l2）、「汇编器怎么拼指令」（u2-l4）、「译码器怎么判非法」（u2-l5）。先把编码格式这块地基打牢，后面三讲才能顺畅。

## 2. 前置知识

本讲需要你已经掌握 u1-l4 的两个结论，这里只做一句话回顾、不展开：

1. **三个全局参数**：`N(bits)` 是基础通道位宽（默认 8）；`L` 是 VX 寄存器数量（默认 32，须被 4 整除）；`K` 是每寄存器 SIMD 通道数。本讲基本不直接用到它们，但 `funct7[1:0]` 选出的 VX/VE/VR 宽度正是 `N`、`2N`、`4N`。
2. **三类寄存器是同一块物理存储的三种视图**：VX 是 `N` 位、VE 是 `2N` 位、VR 是 `4N` 位。指令里的 `rd/rs1/rs2` 默认是 **VX 编号**，VE 用低 4 位、VR 用低 3 位（因为 VE 只有 `L/2` 条、VR 只有 `L/4` 条）。

还需要一点点「按位运算」的常识：右移 `>>`、按位与 `&`。本讲会把这些运算直接对应到代码常量上，不需要你预先很熟。

> 名词小贴士：**opcode**（操作码）、**funct3/funct7**（功能位）、**立即数 imm**（immediate，直接编码在指令里的常数，而不是寄存器号）。这些词来自 RISC-V，chisel-npu 借用了它的风格但不是真正的 RISC-V。

## 3. 本讲源码地图

本讲只聚焦一个文件，并参考一份设计文档：

| 文件 | 作用 | 本讲用到哪部分 |
|:---|:---|:---|
| [`src/main/scala/isa/instrFormat.scala`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala) | 整个 ISA 的「编码格式定义文件」：字段位段常量、三种格式的注释、宽度/舍入/数据类型枚举、`funct7` 属性的打包 case class | 全部内容 |
| [`docs/designs/01.isa.md`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/01.isa.md) | ISA 设计文档，含波形图（wavedrom）和字段表 | 「Instruction Word Layout」一节 |

辅助理解（不属于本讲最小模块，但实践会碰到）：

| 文件 | 作用 |
|:---|:---|
| [`src/main/scala/isa/NpuAssembler.scala`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala) | Scala 汇编器，`encR/encI/encS` 把字段拼成 32 位字 |
| [`src/main/scala/isa/instrDecoder.scala`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala) | 译码器，用 `InstrBits` 常量从 32 位字里把字段抠出来 |
| [`src/test/scala/isa/InstrDecoderSpec.scala`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala) | 译码器测试，展示了「拼指令 → poke → 检查字段」的标准写法 |

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：

- **4.1 三种指令格式 R/I/S 的 32 位布局**（对应「指令格式注释」）
- **4.2 `InstrBits` 位段常量**（用代码精确定位任意字段）
- **4.3 `funct7` 属性字段：`Funct7Attrs` 与 `CvtFunct7`**

### 4.1 三种指令格式 R/I/S 的 32 位布局

#### 4.1.1 概念说明

chisel-npu 的所有指令都是**定长 32 位**的字。借鉴 RISC-V 的做法，它把 32 位切成若干「字段（field）」，不同指令类型只是字段含义不同，但**低 7 位的 `opcode` 和 `rd/funct3/rs1` 的位置在三种格式里完全固定**。这样做的好处是：译码器可以先无条件地把 `opcode/rd/funct3/rs1` 抠出来，再根据 `opcode` 决定高位那 12 位怎么解释。

三种格式分别是：

- **R 型（register-register）**：两个源寄存器 + 一个目的寄存器，高位放 `funct7` 属性。绝大多数向量运算用 R 型。
- **I 型（register-immediate）**：一个源寄存器 + 一个 12 位立即数，用于 `bcast.imm`、`ld/st`、`vsetlut` 等。
- **S 型（three-source FMA）**：三个源寄存器 + 一个舍入位，**只被 `VALU_FP_FMA`（融合乘加）一家使用**，因为它需要 `rs1×rs2+rs3` 三个源。

为什么需要三种？因为不同运算的「输入个数」和「是否需要常数」不同。如果硬塞进一种格式，要么浪费位、要么表达不下。RISC-V 用同样的思路解决了同样的问题。

#### 4.1.2 核心流程：三种格式的位段图

下面三张图都按 **MSB（bit 31）在左、LSB（bit 0）在右** 画，括号里是该字段的位数。

**R 型**（[`instrFormat.scala:13`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L13)）：

```
 31-----------25 24----20 19----15 14--12 11-----7 6------0
|   funct7(7)   |  rs2(5) |  rs1(5) |funct3|  rd(5) |opcode(7)|
```

**I 型**（[`instrFormat.scala:14`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L14)）：

```
 31-------------------20 19----15 14--12 11-----7 6------0
|       imm[11:0](12)    |  rs1(5) |funct3|  rd(5) |opcode(7)|
```

**S 型**（[`instrFormat.scala:15`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L15)）：

```
 31----27 26--25 24----20 19----15 14--12 11-----7 6------0
| rs3(5) |rnd(2)|  rs2(5) |  rs1(5) |funct3|  rd(5) |opcode(7)|
```

注意一个**关键观察**：从 bit 19 往下，三种格式完全一样（`rs1/funct3/rd/opcode`）；区别只在 bit 31~20 这高 12 位怎么切。把这 12 位拎出来对齐看：

| 位段 | R 型 | I 型 | S 型 |
|:---|:---|:---|:---|
| `[31:27]`（5 位） | `funct7[6:2]` | `imm[11:7]` | **`rs3`** |
| `[26:25]`（2 位） | `funct7[1:0]` | `imm[6:5]` | **`rnd`** |
| `[24:20]`（5 位） | `rs2` | `imm[4:0]` | `rs2` |

也就是说：

- I 型的 12 位立即数 `imm[11:0]` **正好压在 R 型的 `funct7(7)+rs2(5)` 上**——`imm` 高 7 位占了 `funct7`，低 5 位占了 `rs2`。
- S 型把 `funct7` 那 7 位拆成 `rs3(5)`（占 `funct7[6:2]`）和 `rnd(2)`（占 `funct7[1:0]`），`rs2` 位置不变。

这种「高位复用」是理解后面译码器为什么能共用一套字段提取代码的钥匙。

各字段的职责（摘自设计文档 [`01.isa.md:78-88`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/01.isa.md#L78-L88)）：

| 字段 | 位数 | 作用 |
|:---|:---:|:---|
| `opcode` | [6:0] | 选**功能家族**（13 个家族之一，详见 u2-l2） |
| `rd` | [11:7] | 目的寄存器号；VE 用低 4 位、VR 用低 3 位 |
| `funct3` | [14:12] | 家族内的**子操作**（如 add/sub/mul） |
| `rs1` | [19:15] | 源寄存器 1 |
| `rs2` | [24:20] | 源寄存器 2 |
| `funct7` | [31:25] | **属性字段**：width/round/sat/dtype |
| `imm[11:0]` | [31:20] | I 型的 12 位有符号立即数 |
| `rnd` | [26:25] | S 型 FMA 的本指令舍入模式 |
| `rs3` | [31:27] | S 型 FMA 的第三个源（加数） |

这里有一个**三层选择**的设计思想，值得记住：

1. `opcode`（7 位）选**家族**——「这是一类什么操作」（算术？逻辑？矩阵乘？）。
2. `funct3`（3 位）选家族内的**子操作**——「add 还是 sub？」
3. `funct7`（7 位）携带**属性**——「在哪种宽度上、要不要饱和、什么数据类型？」

这种 `opcode → funct3 → funct7` 的分层，让 32 位能表达 `13 × 8 × …` 种组合而字段不互相打架。

#### 4.1.3 源码精读：文件头部的格式注释

整张编码地图其实就写在 `instrFormat.scala` 文件最开头的注释里，这是全项目最该先读的一段：

[src/main/scala/isa/instrFormat.scala:11-31](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L11-L31) — 用三行注释给出三种格式，再用两个小节给出 R 型与 CVT 型的 `funct7` 内部布局：

```scala
//  R-type  [funct7(7) | rs2(5) | rs1(5) | funct3(3) | rd(5) | opcode(7)]
//  I-type  [    imm[11:0](12)  | rs1(5) | funct3(3) | rd(5) | opcode(7)]
//  S-type  [rs3(5)|rnd(2)| rs2(5) | rs1(5) | funct3(3) | rd(5) | opcode(7)]
```

紧跟着的 [`instrFormat.scala:17-28`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L17-L28) 用注释把 `funct7` 的两种用法讲清楚——**普通 R 型**和 **`vcvt`（类型转换）家族**用的是两套完全不同的 `funct7` 布局：

```scala
//  funct7 attribute layout (R-type):
//    [1:0] width  : 00=VX (N bits)  01=VE (2N bits)  10=VR (4N bits)  11=reserved
//    [3:2] round  : 00=RNE  01=RTZ  10=floor  11=ceil
//    [4]   sat    : 0=wrap  1=saturate
//    [6:5] dtype  : 00=INT  01=FP   10=BF     11=reserved
//
//  vcvt (VALU_CVT family) uses funct7 differently:
//    [2:0] src format code  (see FmtCode enum)
//    [3]   saturate
//    [5:4] round mode
//    [6]   BF8 variant      0=E4M3  1=E5M2
```

> 重点：同 7 个比特，普通指令把它当「width/round/sat/dtype」，而 `vcvt` 把它当「src格式/饱和/舍入/BF8变体」。译码器（u2-l5）会根据 `opcode` 决定按哪种布局解释。

设计文档里的字段表与之一一对应，见 [`docs/designs/01.isa.md:78-88`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/01.isa.md#L78-L88)。

#### 4.1.4 代码实践：手推一个真实指令字

**实践目标**：用真实汇编器产物，验证你对 R 型布局的理解。

**操作步骤**：

1. 调用汇编器构造一条最简单的 R 型加法 `vadd(rd=1, rs1=2, rs2=3, width=VX)`（`width=VX` 表示 `funct7[1:0]=0`，且不饱和、INT、RNE，所以整个 `funct7=0`）。
2. 手动按 R 型布局拼出 32 位字。
3. 把字按 7/5/5/3/5/7 切开，回读每个字段。

**手算过程**（这是「示例推演」，**待本地用 `sbt console` 实跑确认**）：

字段值：`opcode=0x10`(=0b0010000)、`rd=1`、`funct3=0`、`rs1=2`、`rs2=3`、`funct7=0`。按 R 型位移拼装：

\[
\text{word} = \text{opcode} + (\text{rd}\ll 7) + (\text{funct3}\ll 12) + (\text{rs1}\ll 15) + (\text{rs2}\ll 20) + (\text{funct7}\ll 25)
\]

代入：

\[
\text{word} = 0\text{x}10 + (1\ll 7) + (2\ll 15) + (3\ll 20) = 0\text{x}00310090
\]

写成 32 位二进制（按 R 型字段对齐）：

```
funct7   rs2   rs1 f3  rd   opcode
0000000  00011 00010 000 00001 0010000   = 0x00310090
```

**需要观察的现象**：把这 32 位从 bit 31 往下按 7/5/5/3/5/7 切，应当恰好得到 `funct7=0`、`rs2=3`、`rs1=2`、`funct3=0`、`rd=1`、`opcode=0x10`，与输入完全一致。

**预期结果**：在 `sbt console` 里执行下面这句，应打印出 `-3211392` 或十六进制 `0x00310090`（Scala `Int` 有符号，最高位为 0 时两者数值相等）：

```scala
import isa.NpuAssembler._
println(f"${vadd(rd=1, rs1=2, rs2=3, width=VX)}%08X")   // 预期 00310090
```

> 说明：汇编器的 `encR` 实现见 [`NpuAssembler.scala:60-68`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L60-L68)，它的位移顺序与上面手算完全相同，所以两者必然一致。

#### 4.1.5 小练习与答案

**练习 1**：I 型的 12 位立即数 `imm[11:0]` 占据 `[31:20]`，它压在了 R 型的哪两个字段上？分别对应 `imm` 的哪几位？

> **答案**：压在 `funct7[31:25]`（对应 `imm[11:5]`，共 7 位）和 `rs2[24:20]`（对应 `imm[4:0]`，共 5 位）上。所以 `imm` 高位复用了 `funct7`、低位复用了 `rs2`。

**练习 2**：S 型 FMA 为什么需要 `rs3`？它放在哪 5 位？这 5 位在 R 型里原本是什么？

> **答案**：FMA 计算 `rs1×rs2+rs3`，需要第三个源寄存器 `rs3` 当加数。`rs3` 放在 `[31:27]`，这 5 位在 R 型里属于 `funct7` 的高 5 位（`funct7[6:2]`）。S 型把它「借」走当寄存器号。

---

### 4.2 `InstrBits` 位段常量

#### 4.2.1 概念说明

光有注释里的「`funct7` 在 `[31:25]`」这种文字描述还不够——译码器和测试都需要**用代码**从 32 位字里把字段抠出来。如果每个地方都写魔数 `(31, 25)`，一旦格式改动就要满代码库改。

`instrFormat.scala` 里的 `object InstrBits` 就是来解决这个问题的：它把**每一个字段的起止位边界**定义成具名常量。所有「切字段」的代码都引用这些常量，格式改了只改一处。

#### 4.2.2 核心流程：从「位边界」到「字段值」

给定 32 位指令字 `instr`，提取一个 `[hi:lo]` 字段的标准 Chisel 写法是：

\[
\text{field} = \text{instr}(\text{HI},\ \text{LO})
\]

即 `instr(hi, lo)`——Chisel 的 `UInt` 支持 `(hi, lo)` 切片语法，取的是闭区间 `[hi:lo]`。所以只要知道每个字段的 `hi` 和 `lo`，就能定位。

字段值（无符号整数）就是 `instr(hi, lo)` 的数值；若要符号扩展（如 I 型立即数），再加 `.asSInt`。

`InstrBits` 的命名约定是 `字段_HI` / `字段_LO`，成对出现，值就是该字段的最高位/最低位编号。

#### 4.2.3 源码精读：`InstrBits` 全部常量

[src/main/scala/isa/instrFormat.scala:41-67](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L41-L67) 定义了所有位边界常量。关键部分：

```scala
object InstrBits {
  val OPCODE_LO  =  0;  val OPCODE_HI  =  6   // 7 bits
  val RD_LO      =  7;  val RD_HI      = 11   // 5 bits
  val FUNCT3_LO  = 12;  val FUNCT3_HI  = 14   // 3 bits
  val RS1_LO     = 15;  val RS1_HI     = 19   // 5 bits
  val RS2_LO     = 20;  val RS2_HI     = 24   // 5 bits
  val FUNCT7_LO  = 25;  val FUNCT7_HI  = 31   // 7 bits

  // I-type immediate: bits[31:20]
  val IMM_I_LO   = 20;  val IMM_I_HI   = 31   // 12 bits (sign-extended)

  // S-type (FMA): rs3 at [31:27], round at [26:25]
  val RS3_LO     = 27;  val RS3_HI     = 31   // 5 bits
  val RND_S_LO   = 25;  val RND_S_HI   = 26   // 2 bits
  ...
}
```

注意三件事：

1. **`IMM_I` 的边界 `[31:20]` 与 `FUNCT7[31:25]`+`RS2[24:20]` 重叠**——同一块 12 位，I 型看成立即数、R 型看成 `funct7+rs2`。常量各自命名，互不干扰。
2. **`RS3[31:27]` 与 `FUNCT7` 的高 5 位重叠**，**`RND_S[26:25]` 与 `FUNCT7` 的低 2 位重叠**——S 型把 `funct7` 拆成了 `rs3` 和 `rnd`。
3. 常量只描述「在 32 位字里的绝对位置」；而 `funct7` **内部**子字段（如 `F7_WIDTH`）描述的是「在 7 位 `funct7` 里的相对位置」，两者坐标系不同（见 4.2.4 的译码器用法）。

后半段 [`instrFormat.scala:56-66`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L56-L66) 是 `funct7` 内部子字段常量，分两套（R 型属性 / CVT 型），4.3 节会用到。

**这些常量怎么被使用？** 译码器 `instrDecoder.scala` 是最佳示例，它几乎逐行引用 `InstrBits`：

[src/main/scala/isa/instrDecoder.scala:48-60](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L48-L60) — 用 `InstrBits` 把所有顶层字段一次性抠出来：

```scala
val opBits  = io.instr(InstrBits.OPCODE_HI, InstrBits.OPCODE_LO)  // [6:0]
val rdBits  = io.instr(InstrBits.RD_HI,     InstrBits.RD_LO)      // [11:7]
val f3      = io.instr(InstrBits.FUNCT3_HI,  InstrBits.FUNCT3_LO)  // [14:12]
val rs1Bits = io.instr(InstrBits.RS1_HI,     InstrBits.RS1_LO)     // [19:15]
val rs2Bits = io.instr(InstrBits.RS2_HI,     InstrBits.RS2_LO)     // [24:20]
val f7      = io.instr(InstrBits.FUNCT7_HI,  InstrBits.FUNCT7_LO)  // [31:25]

// I-type immediate (sign-extended 12 bits)
val immI    = io.instr(InstrBits.IMM_I_HI, InstrBits.IMM_I_LO).asSInt

// S-type fields (FMA)
val rs3Bits = io.instr(InstrBits.RS3_HI, InstrBits.RS3_LO)
val rndS    = io.instr(InstrBits.RND_S_HI, InstrBits.RND_S_LO)
```

注意 `immI` 末尾的 `.asSInt`——这就是 I 型立即数的**符号扩展**：12 位有符号数被扩展成全宽有符号数。译码器把它直接送进 `decoded.valu.imm`（[`instrDecoder.scala:299`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L299)）。

#### 4.2.4 代码实践：核对你的位运算与 `InstrBits` 一致

**实践目标**：给你一个 32 位指令字，先凭直觉写位运算提取字段，再用 `InstrBits` 常量核对，确认两者位移/掩码一致。

**操作步骤**：

1. 取 4.1.4 推出的 `word = 0x00310090`（对应 `vadd(rd=1, rs1=2, rs2=3, width=VX)`）。
2. 用「掩码 + 移位」的方式手写提取表达式（这是大多数 ISA 文档里的写法）：
   - `opcode = (word >> 0)  & 0x7F`
   - `rd     = (word >> 7)  & 0x1F`
   - `funct3 = (word >> 12) & 0x7`
   - `rs1    = (word >> 15) & 0x1F`
   - `rs2    = (word >> 20) & 0x1F`
   - `funct7 = (word >> 25) & 0x7F`
3. 对照 `InstrBits`：移位量 = `字段_LO`；掩码宽度 = `字段_HI - 字段_LO + 1` 位。例如 `rd` 移位 `7`=`RD_LO`，掩码 `0x1F`=5 位=`RD_HI-RD_LO+1=11-7+1`。
4. 在 `sbt console` 里用 Scala 验证（**待本地验证**）：

```scala
import isa.InstrBits._
import isa.NpuAssembler._
val w = vadd(rd=1, rs1=2, rs2=3, width=VX)
val opcode = (w >>> OPCODE_LO) & ((1 << (OPCODE_HI-OPCODE_LO+1)) - 1)  // 预期 0x10
val rd     = (w >>> RD_LO)     & ((1 << (RD_HI-RD_LO+1)) - 1)          // 预期 1
val rs2    = (w >>> RS2_LO)    & ((1 << (RS2_HI-RS2_LO+1)) - 1)        // 预期 3
println(opcode, rd, rs2)   // (16, 1, 3)
```

**需要观察的现象**：手写掩码的移位量/掩码宽度，应与 `InstrBits` 里的 `_LO` 值和「`_HI - _LO + 1`」逐一对上，没有任何一个字段对不上。

**预期结果**：6 个字段全部回读正确（`opcode=16, rd=1, funct3=0, rs1=2, rs2=3, funct7=0`），证明你的位运算与项目常量定义完全一致。

> 拓展：把 `word` 换成一条 I 型指令 `vbcastImm(rd=3, imm=42, width=VX)`（值 `0x02A01195`），用同样的掩码去读 `rs2 = (w>>20)&0x1F` 会得到 `10`——这正是 `imm[4:0]`，因为 I 型立即数低 5 位压在 `rs2` 位置上（译码器测试 [`InstrDecoderSpec.scala:163-166`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L163-L166) 正是用这个现象来间接验证立即数编码的）。

#### 4.2.5 小练习与答案

**练习 1**：用 `InstrBits` 的常量写出「提取 `funct3`」的 Chisel 表达式。它的位移量和掩码宽度分别是多少？

> **答案**：`io.instr(InstrBits.FUNCT3_HI, InstrBits.FUNCT3_LO)`，即 `io.instr(14, 12)`。若用掩码写法：移位 `12`（=`FUNCT3_LO`），掩码 `0x7`（3 位，=`14-12+1`）。

**练习 2**：为什么 `IMM_I`、`RS3`、`RND_S` 的常量值会和 `FUNCT7`/`RS2` 的值「撞」在一起？这是 bug 吗？

> **答案**：不是 bug，是故意的字段复用。不同指令格式对同一段比特有不同解释：I 型把 `[31:20]` 整体当立即数，S 型把 `[31:25]` 拆成 `rs3`+`rnd`，R 型把 `[31:25]` 当 `funct7`。`InstrBits` 为每种解释各起一个名字，方便对应格式的代码引用。

---

### 4.3 `funct7` 属性字段：`Funct7Attrs` 与 `CvtFunct7`

#### 4.3.1 概念说明

`funct7` 那 7 位（`[31:25]`）不是单一字段，而是一个**属性包**：它一次性携带「在什么宽度上运算、用什么舍入模式、要不要饱和、什么数据类型」四个属性。这样一条指令就能精确描述自己的执行方式，而不需要额外的配置寄存器。

但 7 位里塞 4 个属性，手算位移很容易错。于是项目提供了两个 Scala case class：

- **`Funct7Attrs`**：面向**普通 R 型**指令，把 `width/round/sat/dtype` 四个属性打包成一个 `Int`。
- **`CvtFunct7`**：面向 **`vcvt`（类型转换）** 指令，因为它的 `funct7` 布局完全不同（塞的是源格式/BF8 变体等）。

> 这两个 case class 是**纯 Scala**对象（不是 Chisel 硬件 Bundle），用在汇编器和测试里拼指令字；硬件译码侧则是直接用 `InstrBits` 的子字段常量切位（见 4.3.3）。

#### 4.3.2 核心流程：属性的打包公式

**R 型 `funct7`** 的 7 位布局（来自 [`instrFormat.scala:18-21`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L18-L21)）：

| 子字段 | 在 `funct7` 里的位 | 宽度 | 取值 |
|:---|:---:|:---:|:---|
| `width` | `[1:0]` | 2 | `00`=VX, `01`=VE, `10`=VR, `11`=reserved |
| `round` | `[3:2]` | 2 | `00`=RNE, `01`=RTZ, `10`=floor, `11`=ceil |
| `sat` | `[4]` | 1 | `0`=wrap, `1`=saturate |
| `dtype` | `[6:5]` | 2 | `00`=INT, `01`=FP, `10`=BF, `11`=reserved |

打包公式（即 `Funct7Attrs.encode`）：

\[
\text{funct7} = (\text{width}\ \&\ 3) \;|\; ((\text{round}\ \&\ 3) \ll 2) \;|\; (\text{sat} \ll 4) \;|\; ((\text{dtype}\ \&\ 3) \ll 5)
\]

**CVT 型 `funct7`** 的 7 位布局（来自 [`instrFormat.scala:24-27`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L24-L27)）：

| 子字段 | 在 `funct7` 里的位 | 宽度 | 取值 |
|:---|:---:|:---:|:---|
| `src` 源格式 | `[2:0]` | 3 | 见 `FmtCode`（s8/s16/s32/f32/bf16/bf8） |
| `sat` | `[3]` | 1 | 输出收窄时是否饱和 |
| `round` | `[5:4]` | 2 | 舍入模式 |
| `bf8 变体` | `[6]` | 1 | `0`=E4M3, `1`=E5M2 |

打包公式（即 `CvtFunct7.encode`）：

\[
\text{funct7}_{\text{cvt}} = (\text{src}\ \&\ 7) \;|\; (\text{sat} \ll 3) \;|\; ((\text{round}\ \&\ 3) \ll 4) \;|\; (\text{bf8E5M2} \ll 6)
\]

> 对比可见：两种布局的 `sat`、`round` 位置都不同（R 型 round 在 `[3:2]`、sat 在 `[4]`；CVT 型 round 在 `[5:4]`、sat 在 `[3]`）。这正是译码器要用 `Mux(family === VALU_CVT, …, …)` 分别取的原因（[`instrDecoder.scala:287`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L287)）。

#### 4.3.3 源码精读：枚举 + 两个 case class

`funct7` 用到的几个属性枚举都在 `instrFormat.scala` 里，紧挨着 `InstrBits`：

- [`VecWidth`（宽度）`:72-77`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L72-L77)：`VX=0/VE=1/VR=2/VW_RSV=3`，2 位。
- [`VecRound`（舍入）`:82-87`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L82-L87)：`RNE=0/RTZ=1/FLOOR=2/CEIL=3`，2 位。
- [`VecDtypeCls`（数据类型大类）`:92-97`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L92-L97)：`INT=0/FP=1/BF=2/DC_RSV=3`，2 位。
- [`FmtCode`（CVT 用格式码）`:102-111`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L102-L111)：`S8=0/S16=1/S32=2/F32=3/BF16=4/BF8=5`，3 位。

> 这里的 `VecWidth` 就是 u1-l4 讲过的「`funct7[1:0]` 选寄存器类」的那 2 位；u1-l4 讲了它的语义，本讲给出它的**代码位置和打包公式**。

两个打包 case class：

[src/main/scala/isa/instrFormat.scala:117-135](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L117-L135) — `Funct7Attrs` 与 `CvtFunct7`，`encode` 方法就是把上面的公式落成代码：

```scala
case class Funct7Attrs(
  width:  Int = 0,   // VecWidth value
  round:  Int = 0,   // VecRound value
  sat:    Boolean = false,
  dtype:  Int = 0,   // VecDtypeCls value
) {
  def encode: Int =
    (width & 3) | ((round & 3) << 2) | ((if (sat) 1 else 0) << 4) | ((dtype & 3) << 5)
}

case class CvtFunct7(
  srcFmt: Int = 0,
  sat:    Boolean = true,
  round:  Int = 0,
  bf8E5M2: Boolean = false,
) {
  def encode: Int =
    (srcFmt & 7) | ((if (sat) 1 else 0) << 3) | ((round & 3) << 4) | ((if (bf8E5M2) 1 else 0) << 6)
}
```

汇编器里有一对完全等价的辅助函数 `f7` / `f7Cvt`（[`NpuAssembler.scala:52-57`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L52-L57)），是同一公式的函数版；所有 `vadd/vmul/...` 都通过 `f7(...)` 生成 `funct7`，再交给 `encR` 拼进 `[31:25]`。

#### 4.3.4 代码实践：手算一个属性 funct7

**实践目标**：用 `Funct7Attrs.encode` 推算一个真实指令的 `funct7`，并用汇编器验证。

**操作步骤**：

1. 想要一条「VE 宽度、RTZ 舍入、饱和、FP 数据类型」的 R 型指令属性。
2. 查枚举值：`width=VE=1`、`round=RTZ=1`、`sat=true`、`dtype=FP=1`。
3. 代入打包公式：

\[
\text{funct7} = (1\ \&\ 3) \;|\; ((1\ \&\ 3) \ll 2) \;|\; (1 \ll 4) \;|\; ((1\ \&\ 3) \ll 5)
\]

\[
= 1 \;|\; 4 \;|\; 16 \;|\; 32 = 0\text{x}35 = 0\text{b}0110101
\]

4. 用 case class 核对（**待本地验证**）：

```scala
import isa.Funct7Attrs
println(Funct7Attrs(width=1, round=1, sat=true, dtype=1).encode)  // 预期 53 = 0x35
```

5. 用汇编器的 `f7` 函数交叉验证，结果应同为 `0x35`：

```scala
import isa.NpuAssembler._
println(f7(width=VE, round=RTZ, sat=true, dtype=FP))   // 预期 53
```

**需要观察的现象**：`Funct7Attrs.encode` 与 `NpuAssembler.f7` 两个独立实现给出**完全相同**的 `0x35`，证明公式无误。

**预期结果**：三种方式（手算公式、case class、汇编器函数）结果一致，均为 `0x35`（二进制 `0110101`，从低到高正是 `width=01`、`round=01`、`sat=1`、`dtype=01`）。

#### 4.3.5 小练习与答案

**练习 1**：`vcvt` 家族为什么不能用 `Funct7Attrs`，必须用 `CvtFunct7`？

> **答案**：因为 `vcvt` 的 `funct7` 布局和普通 R 型完全不同：普通型把 `[1:0]` 当 width、`[3:2]` 当 round、`[4]` 当 sat；而 `vcvt` 把 `[2:0]` 当源格式码、`[3]` 当 sat、`[5:4]` 当 round、`[6]` 当 BF8 变体。用错打包函数会导致属性位错位，译码出错误属性。

**练习 2**：一条 R 型指令的 `funct7 = 0x35`，它的 `width`、`round`、`sat`、`dtype` 分别是什么？

> **答案**：`0x35 = 0b0110101`。`width[1:0]=01`=VE；`round[3:2]=01`=RTZ；`sat[4]=1`=饱和；`dtype[6:5]=01`=FP。与 4.3.4 的构造互为逆运算。

---

## 5. 综合实践

把三种格式 + `InstrBits` + `funct7` 属性串起来，做一次「反向工程师」练习。

**任务**：下面是三条真实指令的 32 位十六进制值（用汇编器 `encR/encI/encS` 生成），请你**不查汇编器源码**，仅凭本讲学到的格式地图，反推出每条指令的 `opcode/funct3/rd/rs1/rs2` 以及它最可能是哪种格式（R/I/S），最后用 `sbt console` 跑汇编器核对。

| 编号 | 32 位字 | 对应汇编器调用 |
|:---:|:---|:---|
| A | `0x00310090` | `vadd(rd=1, rs1=2, rs2=3, width=VX)` |
| B | `0x02A01195` | `vbcastImm(rd=3, imm=42, width=VX)` |
| C | `0x18208017` | `vfma(rd=0, rs1=1, rs2=2, rs3=3, round=RNE)` |

**操作步骤**：

1. 对 A、B、C 分别写出提取 6 个公共字段（`opcode/rd/funct3/rs1/rs2/funct7`）的值。提示：C 的高位按 S 型解释，`funct7` 位置其实是 `rs3(5)+rnd(2)`。
2. 判断格式：A 的 `funct7=0` 且 `rs1/rs2` 都非零 → R 型；B 的 `[31:20]` 是个合理的立即数 → I 型；C 的 `opcode=0x17` 是 FMA 家族 → S 型。
3. 对 C，额外回答：`rs3` 和 `rnd` 分别是几？（预期 `rs3=3, rnd=0`）
4. 核对：在 `sbt console` 里 `import isa.NpuAssembler._` 后打印三条指令的 `f"${x}%08X"`，应与表格完全一致。

**预期结果**：

- A：`opcode=0x10, rd=1, funct3=0, rs1=2, rs2=3, funct7=0`（R 型）。
- B：`opcode=0x15, rd=3, funct3=1, rs1=0`，`[31:20]` 作为 12 位立即数 = `42`（I 型）；若误读 `rs2` 会得到 `10`（=`imm[4:0]`）。
- C：`opcode=0x17, rd=0, funct3=0, rs1=1, rs2=2, rs3=3, rnd=0`（S 型）。

> 这个练习把本讲三个模块全用上了：4.1 的格式布局（判断 R/I/S）、4.2 的 `InstrBits`（定位字段）、4.3 的 `funct7` 解释（A 的 `funct7=0` 对应 VX/不饱和/INT）。能独立做完，说明你已经真正掌握了 32 位编码格式。

## 6. 本讲小结

- chisel-npu 所有指令都是**定长 32 位**，分 **R / I / S** 三种格式；它们的 `opcode[6:0]/rd[11:7]/funct3[14:12]/rs1[19:15]` 位置完全固定，区别只在高位 `[31:20]` 怎么切。
- I 型立即数 `imm[11:0]` 复用 R 型的 `funct7+rs2` 位段；S 型把 `funct7` 拆成 `rs3[31:27]` 和 `rnd[26:25]`——**同一段比特，不同格式有不同解释**。
- `opcode → funct3 → funct7` 是三层选择：opcode 选家族、funct3 选子操作、funct7 携带属性。
- `object InstrBits` 把每个字段的位边界定义成具名常量，译码器用 `instr(HI, LO)` 统一切字段，格式改动只改一处。
- `funct7` 是个属性包，普通 R 型用 `Funct7Attrs`（width/round/sat/dtype）打包，`vcvt` 用 `CvtFunct7`（src/sat/round/bf8 变体）打包，两者的位布局不同，不能混用。

## 7. 下一步学习建议

本讲只讲了「指令字长什么样」。接下来三讲会从不同角度使用这套格式：

- **u2-l2 opcode 家族与 funct3/funct7 属性**：把 13 个 `opcode` 家族、每个家族的 `funct3` 子操作、`funct7` 属性的完整取值表填满。本讲的格式是「容器」，u2-l2 往容器里装「内容」。
- **u2-l4 Scala 汇编器 NpuAssembler**：本讲多次出现的 `encR/encI/encS` 与 `f7/f7Cvt` 会在那一讲系统讲解，你会看到「字段 → 32 位字」的正向拼装过程（本讲练的是反向拆解）。
- **u2-l5 组合译码器 InstrDecoder**：看译码器如何用本讲的 `InstrBits` 常量把 32 位字拆成 `DecodedMicroOp`，并判定哪些编码是非法指令。

建议阅读顺序：先 u2-l2（认识所有家族），再 u2-l4（会拼指令），最后 u2-l5（看硬件怎么拆指令）。如果你已经对本讲的位运算很熟，u2-l5 会非常顺。
