# 微操作与控制 Bundle

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `DecodedMicroOp` 这个「包中包」如何把一条 32 位指令字的译码结果，拆分成送往 VALU、MMALU、寄存器堆的三股控制信号。
- 逐字段解释 `NCoreVALUBundle`（`op` / `regCls` / `dtype` / `saturate` / `round` / `rs3_idx` / `imm`）每一项的来源与作用。
- 逐位解释 `NCoreMMALUCtrlBundle` 的 `keep` / `use_accum` / `busy` 三位控制，并区分哪些是译码来的、哪些是后端派生的。
- 看懂 `VecOp` 这个 `ChiselEnum` 的取值布局与位宽约束，能核对其最大值是否仍落在声明的位宽内。
- 理解 `regCls`（寄存器类）这个字段为何不叫 `width`，以及它如何直接决定后端把结果写回哪类寄存器堆端口。

本讲是 u2（指令集 ISA）的下游、u3-l2（多宽度寄存器堆）与 u6（NCoreBackend 集成）的上游：它定义了译码器与执行单元之间的「控制契约」。

## 2. 前置知识

在进入源码前，先用三句话建立直觉。下面这些概念在 u1-l4 与 u2 中已经建立，这里只做最小回顾：

- **译码（decode）**：把一个 32 位指令字（一串 0/1）翻译成一组「告诉硬件该做什么」的控制信号。chisel-npu 的译码器是纯组合逻辑、单拍完成（详见 u2-l5）。
- **三类寄存器 VX/VE/VR**：它们不是三块独立存储，而是同一块物理存储的三种「视角」。一条指令通过 `funct7[1:0]` 这 2 个比特选择当前操作哪类寄存器：`0=VX`（N 位通道）、`1=VE`（2N 位）、`2=VR`（4N 位）、`3=保留`。
- **Chisel 的 Bundle**：可以理解成一个「带名字的电线捆」。一个 `Bundle` 把若干根相关信号捆在一起整体传递，类似 C 的 struct。本讲讲的所有「控制 Bundle」本质上都是 struct。
- **ChiselEnum**：Chisel 里定义「带固定取值的枚举类型」的机制，每个成员是一个具名的硬件常量。`VecOp` 就是一个 `ChiselEnum`。

一句话定位本讲：**译码器把指令字切成字段后，要重新打包成几个 Bundle，分别递给 VALU、MMALU 和寄存器堆。本讲就是拆开这几个「快递盒」看里面装了什么。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/main/scala/isa/micro_op/VALUMicroCode.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala) | 定义 `VecOp` 枚举、`VecDType` 枚举与 `NCoreVALUBundle`——VALU 的全部控制契约。本讲主战场。 |
| [src/main/scala/isa/micro_op/MMALUMicroCode.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/MMALUMicroCode.scala) | 定义 `NCoreMMALUCtrlBundle`——MMALU 的三位控制包。文件极小。 |
| [src/main/scala/isa/instrDecoder.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala) | 定义 `DecodedMicroOp`（顶层包）与 `InstrDecoder`（负责把字段填进各个 Bundle）。 |
| [src/main/scala/backend/SimpleBackend.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala) | 后端消费方：演示 `regCls` 如何决定写回端口（本讲用它佐证字段语义，深入留在 u6）。 |
| [src/main/scala/isa/instrFormat.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala) | 提供 `VecWidth` 枚举与 `InstrBits` 位段常量，是字段的「来源」。 |
| [src/main/scala/isa/instSetArch.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala) | 定义 `OpFamily`（13 个 opcode 家族），是 `DecodedMicroOp.family` 的取值。 |

数据流方向（记住这张图）：

```
32-bit instr
   │  InstrDecoder（组合译码，u2-l5）
   ▼
DecodedMicroOp  ──┬── .family     → 后端判定 isVALU / isMMA 分发
（顶层包中包）     ├── .valu       → 整块送给 VALU.io.ctrl   ← NCoreVALUBundle
                  ├── .mma_keep  ┐
                  ├── .mma_last  ├→ 组装成 MMALU.io.ctrl   ← NCoreMMALUCtrlBundle
                  ├── .mma_reset ┘
                  └── .rd/.rs1/.rs2/.mem_width → 寄存器堆寻址 / 访存宽度
```

---

## 4. 核心概念与源码讲解

### 4.1 DecodedMicroOp —— 译码器的「包中包」

#### 4.1.1 概念说明

译码器 `InstrDecoder` 的产物不是一根线，而是一个 `DecodedMicroOp`。它的设计哲学是「**包中包**」：

- 顶层包里有若干**标量字段**（`family`、`rd`、`rs1`、`rs2`、三个 `mma_*`、`mem_width`），这些是所有执行单元都可能用到的公共信息。
- 顶层包里还**嵌套了整个 `NCoreVALUBundle`**（字段名 `valu`），VALU 需要的全部控制信号被打成一捆，整体递给 VALU，后端只需一句 `valu.io.ctrl := dec.valu`。

这样做的好处是：VALU 的控制契约（哪些信号、什么位宽、什么含义）被封装在一个 `Bundle` 类型里，VALU 模块和译码器都引用同一个类型，新增/修改 VALU 控制字段时不会漏接某一根线。

#### 4.1.2 核心流程

译码器填写 `DecodedMicroOp` 的过程可以概括为三步：

1. **切字段**：用 `InstrBits` 常量把 32 位 `instr` 切成 `opcode / funct3 / funct7 / rd / rs1 / rs2` 等位段。
2. **组合判定**：由 `opcode → family`、`(family, funct3) → VecOp`、`funct7 → width/dtype/round/sat`，并把 `illegal` 标志算出来（详见 u2-l5）。
3. **驱动输出**：把算好的各路信号分别赋给 `io.decoded.*` 的各个字段，其中 `io.decoded.valu.*` 整块对应 `NCoreVALUBundle`。

注意：`DecodedMicroOp` **本身不携带 illegal 标志**。`illegal` 是译码器 `io` 上一个独立的输出，真正的写回抑制发生在后端 `SimpleBackend`（u6 会讲）。

#### 4.1.3 源码精读

先看顶层 `DecodedMicroOp` 的字段定义：

[VALUMicroCode.scala 的姊妹定义在 instrDecoder.scala:L25-L35](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L25-L35) 定义了顶层包，要点是 `val valu = new NCoreVALUBundle` 把整块 VALU 控制嵌进来，而 MMALU 的控制则被拆成三个独立的 `Bool`（`mma_keep` / `mma_last` / `mma_reset`），并没有嵌套 `NCoreMMALUCtrlBundle`——这是一个值得注意的不对称（见 4.4 节）。

再看译码器如何把这些字段填满。公共标量字段的赋值在这里：

[instrDecoder.scala:L272-L280](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L272-L280) 把 `family / rd / rs1 / rs2 / mem_width / mma_keep / mma_last / mma_reset` 分别接到 `io.decoded` 上。其中 `mma_keep` 来自 `f7Sat`（复用 funct7 的 saturate 位），`mem_width` 直接复用 `funct3`（LD/ST 家族把 funct3 当传输宽度用，见 u2-l3）。

VALU 控制包的赋值（本讲的重点区域）：

[instrDecoder.scala:L283-L299](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L283-L299) 把 `vecOp / width→regCls / dtype / saturate / round / rs3_idx / imm` 七项逐一写进 `io.decoded.valu.*`。注意 `round` 字段对不同家族从不同来源取值（FMA 用 S 型 `rndS`、CVT 用 `f7CvtRnd`、LUT 用 funct3[0] 当 bank 选择）——这就是「同一段比特在不同家族解释不同」在 Bundle 层面的落地。

#### 4.1.4 代码实践

**实践目标**：在译码器里追踪一条具体指令，确认 `DecodedMicroOp` 各字段的取值。

**操作步骤**：

1. 打开 `instrDecoder.scala`，定位 4.1.3 引用的三段代码。
2. 假想一条 `vadd.vx rd, rs1, rs2`（VALU_ARITH 家族、funct3=ADD、width=VX）。手动推演：
   - `family` = `OpFamily.VALU_ARITH`（opcode=0x10）
   - `vecOp` = `VecOp.vadd`（由 `Funct3Arith.ADD` 命中）
   - `regCls` = 0（VX，`width` 默认值）
   - `mma_keep/last/reset` = false（非 MMA 家族，默认值）
3. 对照 [instrDecoder.scala:L88](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L88)（`val vecOp = WireDefault(VecOp.vadd)`），理解为何默认值是 `vadd`：作为故障安全（failsafe）兜底，即使 funct3 未命中任何分支，输出也是一个无害的操作，再由 `illegal` 抑制写回。

**需要观察的现象 / 预期结果**：你应能对任意一条 VALU 指令，不看运行结果就说出 `DecodedMicroOp` 中每个字段的预期值。

**待本地验证**：如需确认，可在 u9 介绍的 `EphemeralSimulator` 测试里 `peek` 这些字段（注意 ChiselEnum 字段要用 `litValue` 比较，不能直接 `expect`，见 u9-l1）。

#### 4.1.5 小练习与答案

**练习 1**：`DecodedMicroOp` 里为什么把 VALU 控制做成嵌套 `Bundle`，而 MMALU 控制却拆成三个独立 `Bool`？

> **参考答案**：VALU 的控制信号多（7 个字段、含义复杂），封装成 `NCoreVALUBundle` 后，译码器与 VALU 共享同一类型，接线只需 `valu.io.ctrl := dec.valu`，不易漏接。MMALU 的控制只有 3 个布尔位，且其中 `busy`/`use_accum` 在后端才派生（见 4.4），没有单独建包的必要。这是一种「按复杂度选择封装粒度」的工程取舍。

**练习 2**：`illegal` 标志为什么不在 `DecodedMicroOp` 里，而是译码器的独立输出？

> **参考答案**：`illegal` 是面向「后端是否抑制写回」的控制信号，不属于任何单一执行单元的控制契约；把它放在 `DecodedMicroOp` 里反而会让 VALU/MMALU 都被迫关心一个与己无关的信号。独立输出让职责更清晰。

---

### 4.2 VecOp 枚举 —— VALU 的内部操作码

#### 4.2.1 概念说明

`VecOp` 是一个 `ChiselEnum`，它定义了 VALU 内部的全部操作码。关键设计是：**VALU 模块永远只看 `VecOp`，看不到原始指令比特**。也就是说，把 `(opcode, funct3)` 这两层 RISC-V 编码「拍扁」成了一维的 `VecOp`，VALU 内部用一个 `MuxLookup`/`switch` 在 `VecOp` 上分发即可，不必再关心家族层。

这是「**译码层吸收复杂性、执行层保持简单**」的典型分层：复杂的两层译码（opcode 选家族、funct3 选子操作）一次性折叠成单层操作码。

#### 4.2.2 核心流程

`VecOp` 的取值是手动分配的、按家族分段（每段留有余量以便未来扩展）。赋值方式为 `Value(0xNN.U(W.W))`，其中 `W` 是显式声明的位宽。译码器用一个嵌套 `switch(family) { switch(funct3) { ... } }` 把每个 `(family, funct3)` 组合映射到一个 `VecOp` 成员（见 u2-l5 与 [instrDecoder.scala:L91-L200](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L91-L200)）。

位宽方面有一个值得推敲的细节，见 4.2.4 实践。

#### 4.2.3 源码精读

`VecOp` 的完整定义按家族分段：

[VALUMicroCode.scala:L41-L116](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala#L41-L116) 定义了全部成员，按家族分段：ARITH(0x00–0x07)、LOGIC(0x08–0x0F)、REDUCE(0x10–0x15)、LUT(0x18–0x19)、CVT(0x20–0x2B)、BCAST(0x30–0x31)、FP ARITH(0x38–0x3E)、FP FMA(0x3F–0x42)、MOV(0x43–0x45)。每段之间留有空隙（如 0x1A–0x1F、0x2C–0x2F）供未来新增操作。

注意头注释与实际声明的一处不一致（这是本讲实践要核对的点）：

[VALUMicroCode.scala:L36-L40](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala#L36-L40) 的注释自称「Values are compact (5-bit)」，但实际成员最大值是 `vmovh = 0x45 = 69`，而 \( \lceil \log_2(69+1) \rceil = \lceil \log_2 70 \rceil = 7 \)，所以实际声明用的是 `U(7.W)`，5 位放不下。注释里「5-bit」是早期遗留，已与代码不符。

#### 4.2.4 代码实践（本讲的主实践任务之一）

**实践目标**：核对新加操作时 `VecOp` 的位宽是否仍够用。

**操作步骤**：

1. 在 [VALUMicroCode.scala:L41-L116](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala#L41-L116) 中把 `VecOp` 的全部成员按家族列成一张表，统计总数与最大值。
2. 计算表示最大值所需的位数：最大值 `0x45 = 69`，公式为

\[
\text{所需位数} = \lceil \log_2(\text{max} + 1) \rceil = \lceil \log_2 70 \rceil = 7
\]

3. 对照声明 `U(7.W)`：7 位可表示 0–127，69 落在内，**正好够用且必不可少**（6 位最多到 63，放不下 69）。

**需要观察的现象 / 预期结果**：

- 全部成员总数 = 52（8+8+6+2+12+2+7+4+3）。
- 最大值 0x45=69，需 7 位。
- 结论：当前 `U(7.W)` 声明正确；但余量只剩到 127，若再新增一整个家族（例如占用 0x50–0x57），就要检查是否溢出 7 位、必要时把声明改成 `U(8.W)`。

**待本地验证**：可在 Scala REPL 用 `VecOp.all.length` 与 `(VecOp.all.map(_.litValue).max)` 验证总数与最大值（`litValue` 返回枚举的整数编码）。

#### 4.2.5 小练习与答案

**练习 1**：为什么把 `(opcode, funct3)` 两层编码折叠成一维 `VecOp`，而不是让 VALU 直接看 `funct3`？

> **参考答案**：`funct3` 只有 3 位（0–7），在不同家族里含义完全不同（例如 funct3=0 在 ARITH 是 ADD、在 LOGIC 是 SLL、在 MMA 是普通累加）。如果 VALU 直接看 `funct3`，就必须同时看 `family` 才能消歧，等于把两层译码逻辑散布到执行单元里。折叠成 `VecOp` 后，VALU 只需在一个一维空间上分发，执行单元保持简单。

**练习 2**：若要把 `VecOp` 的位宽从 7 改成 8，需要改动哪些地方？

> **参考答案**：只需把 `VecOp` 每个成员的 `U(7.W)` 改成 `U(8.W)`。消费方（VALU 的 `switch`、后端的比较）都用 `VecOp()` 类型推断位宽，会自动跟随，无需逐处修改。这正是用 `ChiselEnum` 而非裸 `UInt` 的好处。

---

### 4.3 NCoreVALUBundle —— VALU 的解码控制包

#### 4.3.1 概念说明

`NCoreVALUBundle` 是 VALU 的「**控制契约**」：译码器把一条 VALU 指令的全部控制信息打成这一捆，整体递给 `VALU.io.ctrl`。它有 7 个字段，每个字段都对应指令字里某个位段的「解读结果」。

这里要特别讲清一个改名：本字段在概念上是「指令操作的寄存器宽度类别」（VX/VE/VR），但**字段名不叫 `width` 而叫 `regCls`**。原因是 Chisel 3 里 `Width` 是核心类型（你天天写的 `32.W` 里的 `W` 就来自它），在 `Bundle` 内部用一个叫 `width` 的字段名会与 Chisel 自身的 `width` 概念冲突/造成混淆。于是改名为 `regCls`（register class，寄存器类）。

#### 4.3.2 核心流程

`NCoreVALUBundle` 的字段与指令位段的对应关系如下表（这是本讲最重要的查表）：

| 字段 | 类型 | 来源位段 | 含义 |
| --- | --- | --- | --- |
| `op` | `VecOp()` | opcode + funct3（折叠） | 内部操作码，VALU 在此分发 |
| `regCls` | `UInt(2.W)` | funct7[1:0] | 寄存器类：0=VX, 1=VE, 2=VR |
| `dtype` | `VecDType()` | funct7[6:5]（CVT 时为 BF8 变体） | 数据类型/BF8 子格式选择 |
| `saturate` | `Bool()` | funct7[4]（CVT 时为 funct7[3]） | 是否饱和 |
| `round` | `UInt(2.W)` | funct7[3:2]（FMA 用 S 型 rnd；LUT 用 funct3[0] 当 bank） | 舍入模式 |
| `rs3_idx` | `UInt(5.W)` | S 型的 rs3（指令字 [31:27]） | 第三源寄存器（FMA 专用） |
| `imm` | `SInt(12.W)` | I 型立即数 [31:20]（符号扩展） | 立即数（bcast.imm / movi 用） |

注意「来源位段」列在不同家族会复用：同一片 funct7 比特，在普通 R 型是属性、在 CVT 是源格式码、在 MMA 是 keep——这就是 u2 反复强调的「同一段比特在不同格式/家族下解释不同」。`NCoreVALUBundle` 是这种复用的最终落点。

#### 4.3.3 源码精读

字段定义本身很简洁：

[VALUMicroCode.scala:L138-L146](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala#L138-L146) 定义了 7 个字段。注意 `regCls` 行尾注释明确写了改名原因「avoids name conflict with chisel3 Width」。文件里其实并存两段头部注释（[L118-L129](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala#L118-L129) 仍写 `width`，[L130-L137](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala#L130-L137) 已更新为 `regCls`），正好折射出这次改名的痕迹。

译码器对 `regCls` 的填写逻辑（width 解码）：

[instrDecoder.scala:L202-L225](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L202-L225) 给出了 `width` 的取值规则：默认 VX，funct7[1:0] 为 1→VE、2→VR；FP/FMA 家族强制 VR；CVT 保守置 VR（实际由后端按 `VecOp` 修正）；I 型的 BCAST.IMM 与 vsetlut 强制特定宽度。最终在 [L284](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L284) 把 `width` 接到 `io.decoded.valu.regCls`——变量名是 `width`，字段名是 `regCls`，二者在赋值瞬间对齐。

`regCls` 如何指导写回端口选择（在消费方后端）：

[SimpleBackend.scala:L48-L51](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L48-L51) 定义了 `W` 常量对象（`VX=0, VE=1, VR=2`），专门用来和 `UInt(2.W)` 类型的 `regCls` 比较，从而不必把 `VecWidth` 这个 `ChiselEnum` 导进后端。

[SimpleBackend.scala:L222-L239](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L222-L239) 是核心：`regCls === W.VX` 使能 VX 写端口、`regCls === W.VE` 使能 VE 写端口、`regCls === W.VR` 使能 VR 写端口。三类寄存器对应三个物理写端口，`regCls` 就是「把结果写回哪类寄存器」的选择信号。式中附加的 `isNarrowCvtOut` / `isWideCvtOut` / `isReduceToVR` / `isSetLut` 是对「输入类 ≠ 输出类」的少数操作的修正（如 `vsum.vx` 输入 VX 但输出 VR），这部分属于 u6-l2 的写回时序主题，本讲只需知道：**默认情况下，`regCls` 一比一地决定写回端口**。

#### 4.3.4 代码实践（本讲主实践任务之二）

**实践目标**：说清 `regCls` 为何改名，以及它如何决定写回端口。

**操作步骤**：

1. 阅读 [VALUMicroCode.scala:L143](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala#L143) 的行尾注释，写下改名原因。
2. 打开 [SimpleBackend.scala:L222-L239](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L222-L239)，把三条写回使能表达式抄下来。
3. 用一句话总结映射规则。

**需要观察的现象 / 预期结果**：

- 改名原因：避免与 chisel3 的 `Width` 类型/概念同名冲突。
- 写回端口映射规则（默认情形）：

| `regCls` 值 | 使能的写端口 | 写入数据源 |
| --- | --- | --- |
| 0 (VX) | `vx_w_en(0)` | `valu.io.out_vx` |
| 1 (VE) | `ve_w_en(0)` | `valu.io.out_ve` |
| 2 (VR) | `vr_w_en(0)` | `valu.io.out_vr` |

- 进一步观察：CVT/归约/LUT 类操作会被 `isNarrowCvtOut`/`isReduceToVR`/`isSetLut` 等「修正函数」改写默认映射。试着在 [SimpleBackend.scala:L245-L285](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L245-L285) 找到这些修正函数的定义，预告 u6-l2 会专门讲。

**待本地验证**：可选——参考 u9-l1，用 `EphemeralSimulator` 跑一条 `vadd.vx` 与一条 `vadd.vr`，`peek` 后端 `rf.io.vx_w_en(0)` 与 `rf.io.vr_w_en(0)`，验证恰好只有一个被拉高。

#### 4.3.5 小练习与答案

**练习 1**：为什么后端用 `private object W` 而不是直接 `import VecWidth`？

> **参考答案**：`regCls` 字段被声明为裸 `UInt(2.W)`（不是 `VecWidth` 枚举类型），因此后端要和它比较，最直接的方式就是用同样数值的 `UInt` 常量。`W` 对象把 `0/1/2` 三个魔数换成 `W.VX/W.VE/W.VR` 具名常量，既避免魔数，又不必引入 `ChiselEnum` 的类型转换开销。注释里也写明了「without importing ChiselEnum」。

**练习 2**：`vsum.vx`（对 VX 向量求水平归约）的 `regCls` 是多少？它的结果会写回哪类寄存器？为什么需要 `isReduceToVR` 修正？

> **参考答案**：`regCls=0`（VX），因为 ISA 用输入类编码归约指令（`vsum.vx` 的 `.vx` 表示对 VX 输入归约）。但水平归约的结果是单个标量、按 VR 宽度广播到 K 个通道，所以输出永远是 VR 宽度，应写回 VR。若只按 `regCls===VR` 守卫，`vsum.vx` 会因为 `regCls=VX` 而错误地使能 VX 写端口、漏掉 VR 写端口。`isReduceToVR` 就是来覆盖这一点的：对 `vsum/vrmax/vrmin` 强制使能 VR 写端口、抑制 VX 写端口。（详见 u6-l2、u5-l4）

---

### 4.4 NCoreMMALUCtrlBundle —— MMALU 的三位控制

#### 4.4.1 概念说明

相比 VALU 的 7 字段控制包，MMALU 的控制极其精简：只有 3 个 `Bool`——`keep`、`use_accum`、`busy`。这是因为 MMALU 的「操作」由脉动阵列的硬件结构固定（就是矩阵乘加），不需要像 VALU 那样在几十种操作间分发；控制器只需要告诉它「累加还是覆盖」「是否用外部累加器」「现在忙不忙」。

值得注意的是一个**不对称**：`DecodedMicroOp` 里没有嵌套 `NCoreMMALUCtrlBundle`，而是把三个 `Bool` 平铺成 `mma_keep/last/reset`，由后端在接线时再组装成 `NCoreMMALUCtrlBundle`（见 4.4.3）。换句话说，这个 Bundle 是 **MMALU 模块自己的 IO 类型**，译码器并不直接产出它。

#### 4.4.2 核心流程

三位控制的语义：

| 字段 | 类型 | 语义 | 来源 |
| --- | --- | --- | --- |
| `keep` | `Bool` | true=本拍结果累加进累加器；false=覆盖累加器 | 译码来：`dec.mma_keep`（复用 funct7 的 sat 位） |
| `use_accum` | `Bool` | 是否使用外部送入的 `in_accum` | **后端当前硬编码 `false.B`**（预留接口） |
| `busy` | `Bool` | MMALU 是否处于工作态 | **后端派生**：`family === OpFamily.MMA` |

`keep` 是「流式归约」的关键：连续多拍 `keep=true` 即可把 M×K 的部分和累加在一起，每 K 拍输出一个累积结果（详见 u4-l5）。`use_accum` 与 `busy` 并非来自译码，而是后端根据当前状态决定的——这是「**Bundle 混合了译码字段与派生信号**」的实例。

#### 4.4.3 源码精读

Bundle 定义只有四行：

[MMALUMicroCode.scala:L7-L11](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/MMALUMicroCode.scala#L7-L11) 定义了 `keep` / `use_accum` / `busy` 三个 `Bool`。文件极小，是全项目最简洁的控制契约。

译码器如何产生 `keep`（注意它复用了 sat 位）：

[instrDecoder.scala:L254-L260](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L254-L260) 在 MMA 家族里：`MMA` 子操作把 `mmaKeep` 设为 `f7Sat.asBool`（复用 funct7[4] 的 saturate 位当 keep），`MMA_LAST` 置 `mmaLast`（拉高 collect 信号，finalize），`MMA_RESET` 置 `mmaReset`（清累加器）。这与 u2-l4 讲过的「MMA 的 keep 复用 f7 的 sat 位」完全对应。

后端如何把三个 `Bool` 组装成 `NCoreMMALUCtrlBundle`：

[SimpleBackend.scala:L170-L172](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L170-L172) 把 `dec.mma_keep` 接到 `keep`、`use_accum` 硬接 `false.B`、`busy` 接 `(dec.family === OpFamily.MMA)`。可以清楚看到：只有 `keep` 是真正译码来的，另外两位是后端派生/占位。

#### 4.4.4 代码实践

**实践目标**：区分 `NCoreMMALUCtrlBundle` 三位中哪些是译码来的、哪些是后端派生的。

**操作步骤**：

1. 阅读 [MMALUMicroCode.scala:L7-L11](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/MMALUMicroCode.scala#L7-L11) 与 [SimpleBackend.scala:L170-L172](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L170-L172)。
2. 追踪 `keep` 的源头：`mmalu.io.ctrl.keep` ← `dec.mma_keep` ←（译码器内）`f7Sat.asBool` ← funct7[4]。
3. 追踪 `busy` 的源头：`mmalu.io.ctrl.busy` ← `dec.family === OpFamily.MMA`（后端组合逻辑，不经译码字段）。
4. 追踪 `use_accum`：硬接 `false.B`，确认当前是占位。

**需要观察的现象 / 预期结果**：

- `keep`：译码来（经 funct7[4]）。
- `busy`：后端派生（看 family）。
- `use_accum`：占位 `false.B`，当前未真正使用外部 `in_accum`（后端在 [SimpleBackend.scala:L167](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L167) 把 `in_accum` 硬接为全 0）。

**待本地验证**：可选——参考 u4 的 MMALU 测试，连续两拍 `keep=true` 喂入数据，观察累加器是否累加而非覆盖（这是 u4-l1/u4-l5 的实践内容）。

#### 4.4.5 小练习与答案

**练习 1**：`keep` 为什么复用 funct7 的 saturate 位，而不是新增一个独立位？

> **参考答案**：MMA 家族的 funct7 不需要 saturate 属性（MMALU 输出的是 INT32 累加结果，直写 VR、不截断，见 u6），所以 funct7[4] 在 MMA 家族里是空闲的，复用它当 `keep` 既省编码空间又保持 7 位 funct7 不变。这是「同一段比特在不同家族复用」的又一实例（u2-l2 主题）。

**练习 2**：`busy` 为什么由后端派生而不是译码产生？

> **参考答案**：`busy` 表达的是「MMALU 当前是否处于多拍运算中」，这是一个**运行时状态**，取决于之前若干拍是否 issue 过 MMA 指令，而非单条指令的静态属性。译码器只看当前这一条指令，无法知道历史；所以 `busy` 必须由后端（或 MMALU 内部的状态机）根据 `family` 与节拍派生。当前后端用 `family===MMA` 作简化近似。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**跟踪一条指令穿过控制契约**」的任务。

**任务**：选取一条 `vfma`（FP 融合乘加，S 型格式，`rd = rs1*rs2 + rs3`）指令，画出从 32 位指令字到 MMALU/VALU 控制信号的完整填表过程。

**要求**：

1. 写出 `vfma` 的 32 位字中各字段取值（opcode=VALU_FP_FMA、funct3=FMA、rs3 在 [31:27]、rnd 在 [26:25]）。
2. 在 `DecodedMicroOp` 层面列出：`family`、`valu.op`（应为 `VecOp.vfma`）、`valu.regCls`（FP 家族强制 VR，参考 [instrDecoder.scala:L208-L210](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L208-L210)）、`valu.round`（FMA 走 S 型 `rndS`，参考 [instrDecoder.scala:L291-L297](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L291-L297)）、`valu.rs3_idx`、以及三个 `mma_*`（应为全 false，因为不是 MMA 家族）。
3. 说明这条指令在后端会走 VALU 分支（`isVALU` 为真，见 [SimpleBackend.scala:L204-L212](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L204-L212)），其结果因 `regCls===VR` 写回 VR 端口。
4. 用一段话总结：「控制 Bundle 把译码结果按执行单元分包，VALU 收到一整捆、MMALU 收到三位、寄存器堆收到地址与宽度」。

**预期成果**：一张完整的「指令字 → 字段 → Bundle 字段 → 执行单元输入」映射表。这个练习把 `DecodedMicroOp`、`NCoreVALUBundle`、`NCoreMMALUCtrlBundle`、`VecOp` 四个模块一次打通，并为 u6 的 backend 连线做好铺垫。

## 6. 本讲小结

- `DecodedMicroOp` 是译码器的「包中包」：顶层含公共标量字段，并整体嵌套 `NCoreVALUBundle`；`illegal` 是独立输出，不在此包内。
- `VecOp` 是把 `(opcode, funct3)` 两层编码折叠成的一维内部操作码，VALU 只在它上面分发，看不到原始指令比特；当前 52 个成员、最大值 0x45=69，正好用满 7 位。
- `NCoreVALUBundle` 是 VALU 的 7 字段控制契约（`op`/`regCls`/`dtype`/`saturate`/`round`/`rs3_idx`/`imm`），每个字段都对应指令字某位段的解读，且同一段比特在不同家族会被复用。
- `regCls`（原 `width`）因避开 chisel3 `Width` 命名冲突而改名，它一比一地决定后端写回哪类寄存器端口（VX/VE/VR）。
- `NCoreMMALUCtrlBundle` 只有 `keep`/`use_accum`/`busy` 三位，其中只有 `keep` 是译码来（复用 funct7 的 sat 位），`busy`/`use_accum` 由后端派生或占位。
- 控制 Bundle 体现了「译码层吸收复杂性、执行层保持简单」的分层，也展示了「译码字段与运行时派生信号混在同一包」的务实设计。

## 7. 下一步学习建议

- **紧接着读 u3-l2（多宽度寄存器堆）**：本讲的 `regCls` 选择 VX/VE/VR 三类寄存器，下一讲就讲这块物理存储如何用三种视图别名实现，以及 `vx/ve/vr` 读写端口的真实布局——本讲看到的 `W.VX/VE/VR` 写端口在那里有了物理对应。
- **回头印证 u2-l5**：如果对 `VecOp`、`regCls`、`mma_keep` 的「来源位段」还有疑惑，重看译码器的字段抽取与 `switch` 分发会更清楚。
- **预告 u6-l1 / u6-l2**：本讲只点了 `regCls`→写回端口的默认映射，CVT/归约/LUT 的修正（`isNarrowCvtOut`/`isReduceToVR`/`isSetLut`）与 VALU 1 拍输出导致的 2 拍写回时序，留到 backend 集成讲。
- **若想动手**：按 u9-l1 学会 `EphemeralSimulator` 后，回来把本讲的「待本地验证」项实际跑一遍，用 `peek … litValue` 核对你手推的 `DecodedMicroOp` 字段值。
