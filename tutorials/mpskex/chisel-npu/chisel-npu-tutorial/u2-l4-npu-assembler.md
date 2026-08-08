# Scala 汇编器 NpuAssembler

## 1. 本讲目标

前两讲（u2-l1、u2-l2）我们学会了「读」一条 32 位指令：知道 `opcode` 选家族、`funct3` 选子操作、`funct7` 带属性，也知道普通 R 型 `funct7` 里 `width / round / sat / dtype` 各占哪几个比特、`vcvt` 家族的 `funct7` 又另有一套布局。

但到了**写测试、写示例**的时候，问题反过来了：我们手里只有「我想做一条 VX 宽度、不饱和的加法」这样的意图，怎么把它变成一个能 poke 进硬件的 32 位字？总不能每次都手算「VE 宽度 + RTZ 舍入 + 饱和 + FP」拼出来的 `funct7` 是几。

本讲的主角 [`NpuAssembler`](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L21-L249) 就是解决这个问题的 Scala 端汇编器。读完本讲，你应当能够：

1. 掌握 `encR / encI / encS` 三个位拼接原语的参数与字段拼接方式。
2. 会用 `f7 / f7Cvt` 两个打包器构造普通 R 型与 CVT 型的 `funct7`。
3. 理解为什么所有助手都返回 Scala `Int`，而位 31 置位时要用 `toLong & 0xFFFFFFFFL` 才能安全交给 Chisel。
4. 能直接调用 `vadd / vcvt_s8_f32` 等命名助手，生成一条可读、可验证的 32 位指令字。

> 本讲是 u2-l5（译码器 `InstrDecoder`）的对偶：译码器把 32 位字「拆」成字段，汇编器把字段「拼」成 32 位字。两边用的位段规则完全一致。

---

## 2. 前置知识

本讲假设你已经掌握 u2-l1 与 u2-l2 的内容，这里只做最简回顾：

- **三种格式**（见 [instrFormat.scala 的注释](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L11-L15)）：R 型高位是 `funct7 | rs2`，I 型高位是 12 位立即数 `imm`，S 型（仅融合乘加用）高位是 `rs3 | rnd`。三者低位完全一样：`opcode[6:0] | rd[11:7] | funct3[14:12] | rs1[19:15]`。
- **三层译码**：`opcode` 选家族、`funct3` 选子操作、`funct7` 带属性。
- **`funct7` 有两套布局**：普通 R 型切成 `width[1:0] / round[3:2] / sat[4] / dtype[6:5]`；`vcvt` 家族换成 `src[2:0] / sat[3] / round[5:4] / bf8variant[6]`。
- 这两套布局在 Scala 侧分别由 `Funct7Attrs` 与 `CvtFunct7` 两个 `case class` 的 `encode` 方法负责打包（[instrFormat.scala:L117-L135](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L117-L135)）。本讲的 `f7 / f7Cvt` 就是它们的「函数式替身」。

还有一个 Scala 语言层面的小知识：`Int` 是 32 位**有符号**整数，取值范围是 \([-2^{31},\,2^{31}-1]\)；而一条 32 位指令字在概念上是**无符号**的，取值范围是 \([0,\,2^{32}-1]\)。这个「有符号 vs 无符号」的错位，是本讲第 4.2 节要专门处理的坑。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/main/scala/isa/NpuAssembler.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala) | **本讲主角**。一个 `object`，内含常量词表、`encR/encI/encS` 原语、`f7/f7Cvt` 打包器、几十个命名助手，以及一个把 `Int` 桥接成 Chisel `UInt` 的隐式类。 |
| [src/main/scala/isa/instrFormat.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala) | 位段常量 `InstrBits`，以及 RTL 侧的枚举 `VecWidth / VecRound / VecDtypeCls / FmtCode`，还有 `Funct7Attrs / CvtFunct7` 两个参考打包器。汇编器的数值必须与这里对齐。 |
| [src/main/scala/isa/instSetArch.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala) | `OpFamily`（opcode 家族）与各家族的 `Funct3*` 子操作枚举。汇编器里写死的 `0x10 / 0x14` 等 opcode、`0/1/2` 等 funct3 都来自这里。 |
| [src/test/scala/isa/InstrDecoderSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala) | 汇编器的「真实用户」。它 `import NpuAssembler._` 后用 `vadd(...)` 构造指令、再 poke 进译码器验证，是本讲代码实践的范本。 |

---

## 4. 核心概念与源码讲解

### 4.1 NpuAssembler 常量：汇编器的「助记词表」

#### 4.1.1 概念说明

如果汇编器里到处写 `width=0`、`round=1`、`dtype=2`，读者就得不停翻 u2-l2 的表格才能看懂含义。所以 `NpuAssembler` 第一件事就是把这些魔数起成可读的名字，集中放在文件开头。这些常量就是汇编器的「词表」——你用它们拼装意图，汇编器替你把意图翻译成比特。

#### 4.1.2 核心流程

词表分四组，分别对应 `funct7` / `funct3` 里四种语义字段：

| 组 | 成员 | 对应字段 | 数值 |
| --- | --- | --- | --- |
| 宽度选择子 | `VX / VE / VR` | 普通 R 型 `funct7[1:0]` | 0 / 1 / 2 |
| 舍入模式 | `RNE / RTZ / FLOOR / CEIL` | `funct7[3:2]` | 0 / 1 / 2 / 3 |
| dtype 类 | `INT / FP / BF` | `funct7[6:5]` | 0 / 1 / 2 |
| vcvt 格式码 | `S8 / S16 / S32 / F32 / BF16 / BF8` | `funct3`(目的) 或 `funct7[2:0]`(源) | 0 / 1 / 2 / 3 / 4 / 5 |

一个关键约束：**这些常量的数值必须与 RTL 侧的 ChiselEnum 一一对应**，否则汇编器拼出的字会被译码器理解成另一个含义。例如 `NpuAssembler.VE == 1` 必须等于 `VecWidth.VE == Value(1.U)`，`NpuAssembler.BF8 == 5` 必须等于 `FmtCode.BF8 == 5.U(3.W)`。

#### 4.1.3 源码精读

常量定义在 [NpuAssembler.scala:L23-L47](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L23-L47)，关键片段：

```scala
// Width selectors (funct7[1:0])
val VX = 0  // N(bits)-wide lanes
val VE = 1  // 2N-wide lanes
val VR = 2  // 4N-wide lanes
...
// Format codes for vcvt (funct3 = dst, funct7[2:0] = src)
val S8   = 0
...
val BF8  = 5   // BF8 variant (E4M3 vs E5M2) from bf8E5M2 parameter
```

对照 RTL 侧：`VecWidth` 枚举里 `VE = Value(1.U(2.W))`（[instrFormat.scala:L72-L77](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L72-L77)），`FmtCode` 里 `BF8 = 5.U(3.W)`（[instrFormat.scala:L102-L111](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L102-L111)）。两边数值一致，这正是汇编器产出合法指令的基础。

> 注意：词表里**故意没有「保留值」常量**（例如宽度 `3=reserved`、`RSV6/RSV7` 格式码）。因为保留值会被译码器判为非法指令（见 u2-l5），汇编器不应主动帮人生成非法字。

#### 4.1.4 代码实践

**实践目标**：核对词表与 RTL 枚举是否对齐。

**操作步骤**：

1. 打开 [NpuAssembler.scala:L23-L47](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L23-L47)，记下 `BF8` 的值。
2. 打开 [instrFormat.scala:L102-L111](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L102-L111)，记下 `FmtCode.BF8` 的值。
3. 同法核对 `VE` ↔ `VecWidth.VE`、`RTZ` ↔ `VecRound.RTZ`。

**需要观察的现象 / 预期结果**：四组常量的数值应当与对应枚举完全相同（`BF8=5`、`VE=1`、`RTZ=1`）。这是一份「待本地验证」的纯阅读核对，无需运行仿真。

#### 4.1.5 小练习与答案

**练习 1**：一条指令想要「VR 宽度、RTZ 舍入、饱和、FP dtype」，写出对应的常量组合。

**参考答案**：`width = VR`、`round = RTZ`、`sat = true`、`dtype = FP`。

**练习 2**：为什么词表里没有宽度 `3`（保留）这个常量？

**参考答案**：保留宽度会被译码器判为非法（详见 u2-l5 的 `widthIllegal`）。汇编器只应产出合法指令，因此不暴露会触发非法的常量。

---

### 4.2 encR / encI / encS：三个位拼接原语

#### 4.2.1 概念说明

「原语（primitive）」是汇编器最底层的三个函数，干同一件事：**把分散的字段按位段拼成一个 32 位字**。之所以要三个，是因为 R/I/S 三种格式在高位 `[31:20]` 的解释不同（见 u2-l1）：R 型放 `funct7 + rs2`，I 型放 12 位立即数，S 型放 `rs3 + rnd`。三个原语分别对应这三种高位的摆法。

#### 4.2.2 核心流程

`encR` 的拼接公式（字段 → 位段）：

\[
\text{word} = \text{opcode}_{[6:0]} \;\big|\; \text{rd}_{[11:7]} \;\big|\; \text{funct3}_{[14:12]} \;\big|\; \text{rs1}_{[19:15]} \;\big|\; \text{rs2}_{[24:20]} \;\big|\; \text{funct7}_{[31:25]}
\]

用位运算实现，就是把每个字段左移到它的起始位再「或」起来。`encI` 把 `funct7+rs2` 那段换成 12 位立即数（放在 `[31:20]`）；`encS` 把那段换成 `rnd[26:25] + rs3[31:27]`。

这里有一个**必须处理的问题**：`funct7` 占据最高 7 位 `[31:25]`，当它的最高位（bit 6，对应整字的 bit 31）被置 1 时，拼出来的 32 位字 ≥ \(2^{31}\)，超过了 Scala `Int` 的正数上界 \(2^{31}-1\)。如果直接用 `Int` 算术，会出现有符号溢出、产生负数，容易出错。原语的做法是：

1. 先把每个字段 `.toLong` 提升到 64 位 `Long` 再左移、或运算——`Long` 范围足够大，绝不会溢出。
2. 算完后用 `& 0xFFFFFFFFL` 把结果**掩码到低 32 位**，丢掉任何越界的高位。
3. 最后 `.toInt` 转回 `Int`。这步之后 `Int` 可能是个负数（因为 bit 31 被当成符号位），但**32 位的位模式是完全正确的**。

> 一句话：**在 `Long` 里算，掩成 32 位，用 `Int` 装（可能为负），由下游重新解释为无符号。** 代码里 `encR/encI/encS` 三个函数的最后一行都是同一句 `(w & 0xFFFFFFFFL).toInt`。

#### 4.2.3 源码精读

`encR` 的实现（[NpuAssembler.scala:L60-L68](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L60-L68)）：

```scala
def encR(opcode: Int, funct3: Int, funct7: Int, rd: Int, rs1: Int, rs2: Int): Int = {
  val w = (opcode.toLong & 0x7F) |
          ((rd.toLong & 0x1F) << 7) |
          ((funct3.toLong & 0x7) << 12) |
          ((rs1.toLong & 0x1F) << 15) |
          ((rs2.toLong & 0x1F) << 20) |
          ((funct7.toLong & 0x7F) << 25)
  (w & 0xFFFFFFFFL).toInt  // keep 32 bits, return as (possibly signed) Int
}
```

注意每个字段都先 `&` 上自己的位宽掩码（`0x7F / 0x1F / 0x7`）再做 `.toLong`——这能挡住「传进来的值超范围」的情况，只取有效位。

`encI`（[NpuAssembler.scala:L71-L79](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L71-L79)）与 `encS`（[NpuAssembler.scala:L82-L91](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L82-L91)）结构完全一样，只是高位字段不同。值得留意 `encI` 对立即数的处理：

```scala
val imm12 = imm.toLong & 0xFFF    // 只取低 12 位
...
(imm12 << 20)                     // 放到 [31:20]
```

它**只取 `imm` 的低 12 位**，并不做符号扩展。符号扩展发生在**译码端**——译码器读 `[31:20]` 时调用 `.asSInt`（[instrDecoder.scala:L56](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L56)）。所以汇编器只负责「放对 12 个比特」，至于它代表有符号还是无符号，是读的人决定的。

#### 4.2.4 代码实践

**实践目标**：手算一条 `vadd(rd=0, rs1=1, rs2=2, width=VX, sat=false)` 的 32 位十六进制值，体会位拼接。

**操作步骤**：`vadd` 内部其实只是 `encR(0x10, 0, f7(VX, sat=false), 0, 1, 2)`（见 4.4.3）。先算 `f7`：`VX=0, round=RNE=0, sat=false, dtype=INT=0`，所以 `f7 = 0`。再代入 `encR`，逐字段移位：

| 字段 | 值 | 移位 | 贡献 |
| --- | --- | --- | --- |
| opcode | 0x10 | — | 0x00000010 |
| rd | 0 | <<7 | 0 |
| funct3 | 0 | <<12 | 0 |
| rs1 | 1 | <<15 | 0x00008000 |
| rs2 | 2 | <<20 | 0x00200000 |
| funct7 | 0 | <<25 | 0 |

**预期结果**：相加得 `0x00208010`，即 `vadd(rd=0, rs1=1, rs2=2)` 的十六进制为 `208010`。bit 31 未置位，所以作为 `Int` 它是个正数 `2134096`。

**需要观察的现象**：这正是 [InstrDecoderSpec.scala:L46-L48](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L46-L48) 里那条 `vadd(rd=1, rs1=2, rs2=3, width=VX)` 测试的同类指令；你可以在本地用 4.4.4 的 `sbt console` 方法打印 `vadd(rd=0, rs1=1, rs2=2).toHexString`，预期看到 `208010`（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：手算 `vadd(rd=0, rs1=0, rs2=0, width=VX)` 的十六进制值。

**参考答案**：所有字段为 0，只剩 opcode `0x10`，结果为 `0x10`。

**练习 2**：`encR` 为什么先在 `Long` 里算、最后又 `.toInt` 回到 `Int`，而不是全程用 `Int`？

**参考答案**：32 位指令字是无符号的，当 bit 31 置位时其值 ≥ \(2^{31}\)，超出 `Int` 正数范围。在 `Long`（64 位）里计算可避免有符号溢出，`& 0xFFFFFFFFL` 保证只保留低 32 位，最后 `.toInt` 以「可能为负的 `Int`」承载正确的位模式，交由下游（`asUInt` 或 `toLong & 0xFFFFFFFFL`）重新解释为无符号。

---

### 4.3 f7 / f7Cvt：funct7 属性打包器

#### 4.3.1 概念说明

`funct7` 不是单一数值，而是一个 7 位的「属性包」，里头挤着好几个独立开关（宽度、舍入、饱和、dtype……）。`f7` 和 `f7Cvt` 就是两个**打包器**：吃进几个有名字的属性，吐出一个拼好的 7 位 `funct7` 整数。它们对应 u2-l2 讲过的两套 `funct7` 布局——普通 R 型用 `f7`，`vcvt` 家族用 `f7Cvt`。

> 它们与 `instrFormat.scala` 里的 `Funct7Attrs.encode` / `CvtFunct7.encode`（[L117-L135](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L117-L135)）是**同一套位布局的两种写法**：后者是面向对象风格（先构造 `case class` 再 `.encode`），`f7/f7Cvt` 是函数式风格（直接传参）。两者的位运算完全一致，可以互相印证。

#### 4.3.2 核心流程

普通 R 型 `funct7` 的位布局（与 [instrFormat.scala:L18-L22](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L18-L22) 一致）：

| funct7 位 | 含义 | 打包方式 |
| --- | --- | --- |
| `[1:0]` | width（VX/VE/VR） | `width & 3` |
| `[3:2]` | round（RNE/RTZ/…） | `(round & 3) << 2` |
| `[4]` | sat（0=回绕, 1=饱和） | `(if (sat) 1 else 0) << 4` |
| `[6:5]` | dtype（INT/FP/BF） | `(dtype & 3) << 5` |

`vcvt` 家族 `funct7` 布局则不同（与 [instrFormat.scala:L24-L28](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L24-L28) 一致）：

| funct7 位 | 含义 | 打包方式 |
| --- | --- | --- |
| `[2:0]` | 源格式 srcFmt（S8/…/BF8） | `srcFmt & 7` |
| `[3]` | sat | `(if (sat) 1 else 0) << 3` |
| `[5:4]` | round | `(round & 3) << 4` |
| `[6]` | BF8 变体（0=E4M3, 1=E5M2） | `(if (bf8E5M2) 1 else 0) << 6` |

注意 `sat` 和 `round` 在两套布局里的**位置不同**：R 型 sat 在 bit 4、round 在 `[3:2]`；CVT 型 sat 在 bit 3、round 在 `[5:4]`。这就是为什么要分两个打包器，不能混用。

#### 4.3.3 源码精读

`f7`（[NpuAssembler.scala:L51-L53](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L51-L53)）：

```scala
def f7(width: Int = VX, round: Int = RNE, sat: Boolean = false, dtype: Int = INT): Int =
  (width & 3) | ((round & 3) << 2) | ((if (sat) 1 else 0) << 4) | ((dtype & 3) << 5)
```

`f7Cvt`（[NpuAssembler.scala:L55-L57](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L55-L57)）：

```scala
def f7Cvt(srcFmt: Int, sat: Boolean = true, round: Int = RNE, bf8E5M2: Boolean = false): Int =
  (srcFmt & 7) | ((if (sat) 1 else 0) << 3) | ((round & 3) << 4) | ((if (bf8E5M2) 1 else 0) << 6)
```

**一个值得品味的复用**：MMA（矩阵乘）家族的 `keep`（累加使能）信号，并没有独立的编码位，而是**复用了普通 R 型 `funct7` 的 `sat` 位（bit 4）**。看 `mma` 助手（[NpuAssembler.scala:L228-L229](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L228-L229)）：

```scala
def mma(rd: Int, rs1: Int, rs2: Int, keep: Boolean = true): Int =
  encR(0x03, 0, f7(VR, sat=keep), rd, rs1, rs2)
```

它把 `keep` 塞进 `f7` 的 `sat` 槽位；译码器那边读到 `funct7[4]` 后并不叫它「饱和」，而叫它 `mma_keep`（[instrDecoder.scala:L256](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L256)）。同一段比特在不同家族里含义不同——这正是 u2-l2 强调的「家族特异性」，也是 `f7` 作为通用打包器能被多处复用的原因。

#### 4.3.4 代码实践

**实践目标**：手算两个 `funct7`，验证你对两套布局的理解。

**操作步骤**：

1. 算 `f7(VR, dtype=FP)`（例如 FP32 运算的属性包）：`width=VR=2`、`round=RNE=0`、`sat=false`、`dtype=FP=1`。
   \[
   \text{f7} = (2\,\&\,3)\;|\;(0<<2)\;|\;(0<<4)\;|\;((1\,\&\,3)<<5) = 2\;|\;32 = \text{0x22}
   \]
2. 算 `f7Cvt(F32, bf8E5M2=true)`（CVT 家族、源是 BF8 的 E5M2 变体）：`srcFmt=F32=3`、`sat=false`、`round=0`、`bf8E5M2=true`。
   \[
   \text{f7Cvt} = (3\,\&\,7)\;|\;(0<<3)\;|\;(0<<4)\;|\;(1<<6) = 3\;|\;64 = \text{0x45}
   \]

**预期结果**：分别为 `0x22` 与 `0x45`。

**需要观察的现象**：注意第二个例子里 `0x45` 的最高位（bit 6）是 1——当这个 `funct7` 被 `encR` 放到 `[31:25]` 后，整字的 **bit 31 会被置位**，于是拼出的指令字作为 Scala `Int` 会是**负数**。这正是 4.2 节那个 `toLong & 0xFFFFFFFFL` 机制要对付的情形（下一节 4.4 会给出完整例子）。

#### 4.3.5 小练习与答案

**练习 1**：计算 `f7(VX, round=RTZ, sat=true, dtype=INT)`。

**参考答案**：`(0&3) | (1<<2) | (1<<4) | (0<<5) = 4 | 16 = 0x14`。

**练习 2**：MMA 里 `keep=true` 时，`funct7[4]` 是几？为什么能复用 `sat` 位？

**参考答案**：是 1（因为 `mma` 调用 `f7(VR, sat=keep)`）。MMA 家族本身不需要「饱和」语义，bit 4 在该家族被译码器读作 `mma_keep`；复用同一段比特节省了编码空间，是 ISA 里常见的「家族特异复用」。

---

### 4.4 命名助手与 Int → UInt 桥接

#### 4.4.1 概念说明

「命名助手」是汇编器面向用户的最高层 API。`encR + f7` 虽然万能，但调用者得自己记住「加法的 opcode 是 `0x10`、funct3 是 `0`」。命名助手把这些常量固定下来，提供 `vadd(rd, rs1, rs2, width=VX, sat=false)` 这样**像汇编指令一样可读**的函数。本质上是「opcode + funct3 + 默认 f7」的薄封装。

另一个收尾问题：所有助手都返回 Scala `Int`，但我们要把它 poke 进 Chisel 的 `UInt(32.W)` 端口。`Int`（有符号、可能为负）到 `UInt`（无符号 32 位）需要一个桥接——这就是文件末尾的隐式类 `IntToUInt`，提供 `asUInt` 方法。

#### 4.4.2 核心流程

从意图到硬件的完整调用链：

```
读者意图
  └─ 命名助手 (vadd / vcvt_s8_f32 / ...)      # 固定 opcode+funct3，调 f7/f7Cvt
      └─ encR / encI / encS + f7 / f7Cvt       # 拼成 32 位字
          └─ Scala Int (可能为负，位模式正确)
              └─ asUInt  或  (x.toLong & 0xFFFFFFFFL).U   # 桥接
                  └─ Chisel UInt(32.W)   →   dut.io.instr.poke(...)
```

下游有两种等价的桥接写法：用助手的 `asUInt`（`instr.asUInt`），或像测试那样手写 `(instr.toLong & 0xFFFFFFFFL).U`。两者**完全等价**，都是先把 `Int` 提升成正 `Long`、掩码到 32 位、再 `.U` 成无符号。

#### 4.4.3 源码精读

最典型的命名助手 `vadd`（[NpuAssembler.scala:L99-L100](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L99-L100)），一行就说完：

```scala
def vadd (rd: Int, rs1: Int, rs2: Int, width: Int = VX, sat: Boolean = false): Int =
  encR(0x10, 0, f7(width, sat=sat), rd, rs1, rs2)
```

`0x10` 是 `VALU_ARITH` 家族（[instSetArch.scala:L37](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala#L37)），funct3 `0` 是 `Funct3Arith.ADD`（[instSetArch.scala:L56](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala#L56)）。其余 8 个算术助手（`vsub/vmul/vneg/...`）只是改了 funct3，结构一模一样（[L99-L114](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L99-L114)）。

`vcvt` 家族更值得看，因为它同时用到了 `f7Cvt`（[NpuAssembler.scala:L158-L162](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L158-L162)）：

```scala
def vcvt(rd: Int, rs1: Int,
         dstFmt: Int, srcFmt: Int,
         sat: Boolean = true, round: Int = RNE,
         bf8E5M2: Boolean = false): Int =
  encR(0x14, dstFmt, f7Cvt(srcFmt, sat, round, bf8E5M2), rd, rs1, 0)
```

注意这里 `funct3` 槽位放的是**目的格式 `dstFmt`**（CVT 家族用 funct3 表目的格式，见 u2-l2），而源格式进了 `f7Cvt`。在此基础上又有一层「便利别名」（[L165-L176](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L165-L176)），把常用转换固化成名字：

```scala
def vcvt_s8_f32 (rd: Int, rs1: Int, sat: Boolean = true, round: Int = RNE): Int =
  vcvt(rd, rs1, S8, F32, sat, round)
```

于是读者既可以写底层的 `vcvt(rd, rs1, S8, F32)`，也可以直接写 `vcvt_s8_f32(rd, rs1)`——后者更像传统汇编的 `cvt.s8.f32`。

最后是桥接用的隐式类（[NpuAssembler.scala:L244-L248](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L244-L248)）：

```scala
implicit class IntToUInt(val v: Int) {
  // Convert to UInt treating the int as an unsigned 32-bit bit pattern
  def asUInt: chisel3.UInt = (v.toLong & 0xFFFFFFFFL).U(32.W)
}
```

测试里的真实用法（[InstrDecoderSpec.scala:L25](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L25)）正是手写了同一套掩码：

```scala
dut.io.instr.poke((instr.toLong & 0xFFFFFFFFL).U)
```

> 为什么不直接 `instr.U`？当 `instr` 的 bit 31 置位时它是个负 `Int`，直接交给 Chisel 的隐式转换其符号语义并不直观；先用 `toLong & 0xFFFFFFFFL` 规范化成一个落在 \([0, 2^{32})\) 的正 `Long`，再 `.U(32.W)`，语义最明确、最不会踩坑。这就是 `asUInt` 存在的全部理由。

#### 4.4.4 代码实践

**实践目标**：用 `vadd` 构造一条「VX 宽度、不饱和」的加法指令，打印其 32 位十六进制值；再用 `encR + f7` 手动拼接同一条指令，验证两者完全一致。

**操作步骤**（在项目容器内，`make container` 进入后执行 `sbt console`）：

```scala
// 示例代码（在 sbt console 中执行）
import isa.NpuAssembler._

val a = vadd(rd = 0, rs1 = 1, rs2 = 2, width = VX, sat = false)   // 命名助手
val b = encR(0x10, 0, f7(width = VX, sat = false), 0, 1, 2)       // 手动拼接

println(a.toHexString)   // 预期 208010
println(a == b)          // 预期 true
```

**预期结果**：

- `a.toHexString` 输出 `208010`（与 4.2.4 手算一致）。
- `a == b` 为 `true`——因为 `vadd` 的函数体本来就是那句 `encR(0x10, 0, f7(width, sat=sat), rd, rs1, rs2)`，两者在数学上恒等，这条「验证」其实是在读一行源码。

**需要观察的现象**：把 `width` 改成 `VR`、`sat` 改成 `true` 再打印，会看到十六进制值变大（`funct7` 不再是 0）；若构造一条会置位 bit 31 的指令（例如 `vcvt_f32_bf8(rd=0, rs1=0, e5m2=true)`，其 `funct7=0x45` 会让 bit 31 置位），直接 `println(x)` 会看到一个**负的十进制数**，但 `x.toHexString` 仍是正确的 8 位无符号十六进制 `8a003014`（待本地验证）。这正好演示了「`Int` 可能为负、但位模式正确」的现象，以及为什么交给 Chisel 时必须走 `asUInt` / `toLong & 0xFFFFFFFFL`。

#### 4.4.5 小练习与答案

**练习 1**：写出 `vcvt_s32_f32(rd=1, rs1=0)` 生成的指令字的 `opcode / funct3 / funct7` 三个字段。

**参考答案**：`opcode = 0x14`（`VALU_CVT`）；`funct3 = S32 = 2`（目的格式）；`funct7 = f7Cvt(srcFmt=F32=3, sat=true, round=RNE=0, bf8E5M2=false) = (3&7)|(1<<3) = 0xB`。

**练习 2**：为什么测试里 poke 写成 `(instr.toLong & 0xFFFFFFFFL).U`，而不是 `instr.U`？

**参考答案**：`instr` 是 Scala `Int`，bit 31 置位时为负数。先 `toLong & 0xFFFFFFFFL` 把它规范化为 \([0, 2^{32})\) 内的正 `Long`，再 `.U` 成 32 位无符号 `UInt`，语义明确无歧义；这正是 `asUInt` 帮你封装的同一段逻辑。

---

## 5. 综合实践

把本讲四个模块串起来，手工汇编一条真实指令，并说明它如何进入硬件。

**任务**：手工计算 `vsub(rd=4, rs1=5, rs2=6, width=VE, sat=true)` 的 32 位十六进制值，并写出把它 poke 进译码器的 Chisel 语句。

**第一步——查词表（4.1）**：`VE=1`、`RNE=0`、`INT=0`、`sat=true`。

**第二步——打包 funct7（4.3）**：

\[
\text{f7}(1, 0, \text{true}, 0) = (1\,\&\,3)\;|\;(0<<2)\;|\;(1<<4)\;|\;(0<<5) = 1\;|\;16 = \text{0x11}
\]

**第三步——位拼接（4.2）**：`vsub` 是 `encR(0x10, funct3=1, f7, rd, rs1, rs2)`，其中 funct3 `1` 来自 `Funct3Arith.SUB`。逐字段移位：

| 字段 | 值 | 贡献 |
| --- | --- | --- |
| opcode | 0x10 | 0x00000010 |
| rd | 4 (<<7) | 0x00000200 |
| funct3 | 1 (<<12) | 0x00001000 |
| rs1 | 5 (<<15) | 0x00028000 |
| rs2 | 6 (<<20) | 0x00600000 |
| funct7 | 0x11 (<<25) | 0x22000000 |

相加得 **`0x22629210`**。bit 31 未置位（`0x22...`），作为 `Int` 是正数 `576661552`。

**第四步——桥接进硬件（4.4）**：要么 `vsub(4,5,6,VE,sat=true).asUInt`，要么照测试写法：

```scala
dut.io.instr.poke((vsub(rd=4, rs1=5, rs2=6, width=VE, sat=true).toLong & 0xFFFFFFFFL).U)
```

**验收**：在 `sbt console` 里 `println(vsub(4,5,6,VE,sat=true).toHexString)` 应输出 `22629210`（待本地验证）；再对照 [InstrDecoderSpec.scala:L67-L72](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L67-L72) 里 `vsub(rd=1, rs1=2, rs2=3)` 的同类测试，确认你的手算与汇编器输出、与译码器期望三者一致。这条任务用到了词表（4.1）、`f7`（4.3）、`encR`（4.2）和 `asUInt`（4.4）四个模块，是本讲知识的完整闭环。

---

## 6. 本讲小结

- `NpuAssembler` 是一个纯 Scala 的 `object`，提供「意图 → 32 位指令字」的汇编能力，产出可直接 poke 进 `NeuralCoreMicroOp.word`（仿真）或下游译码器。
- 四组**常量词表**（`VX/VE/VR`、`RNE/RTZ/...`、`INT/FP/BF`、`S8/.../BF8`）把魔数换成可读名字，且数值与 RTL 侧的 `VecWidth / FmtCode` 等枚举严格对齐。
- 三个原语 **`encR / encI / encS`** 分别拼 R/I/S 三种格式的高位段；它们统一在 `Long` 里计算、用 `& 0xFFFFFFFFL` 掩码到 32 位、再 `.toInt` 返回——这是为了正确处理 bit 31 置位时的「有符号 `Int`」问题。
- 两个打包器 **`f7 / f7Cvt`** 对应普通 R 型与 CVT 型两套 `funct7` 布局，`sat/round` 在两者中位置不同，不可混用；MMA 的 `keep` 还复用了 `f7` 的 `sat` 位。
- **命名助手**（`vadd / vcvt_s8_f32 / ...`）是 `encR + f7` 的薄封装，把 opcode/funct3 固化成可读 API；**`asUInt`** 隐式类负责把（可能为负的）`Int` 安全桥接成 Chisel `UInt(32.W)`。

---

## 7. 下一步学习建议

下一讲 **u2-l5：组合译码器 `InstrDecoder`** 是本讲的严格对偶——汇编器把字段「拼」成字，译码器把字「拆」回字段。建议：

1. 读 [instrDecoder.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala)，对照本讲的位段，看它如何用 `InstrBits` 常量切出 `opcode/funct3/funct7`。
2. 重点看 `illegal` 信号在哪些条件下被置位（保留 opcode、保留 funct3、保留 width、`vcvt` 源==目的）——验证本讲「词表故意不含保留值」的设计动机。
3. 跑一遍 [InstrDecoderSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala)，这是本讲汇编器与下一讲译码器的「合体验收」。
