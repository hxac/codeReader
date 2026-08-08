# opcode 家族与 funct3/funct7 属性

## 1. 本讲目标

在上一讲（u2-l1）里，我们已经看清了 32 位指令字的三种格式（R/I/S）和各字段的位段边界。本讲要回答下一个问题：**这 32 位里的 `opcode`、`funct3`、`funct7` 三段到底各自承担什么职责？**

读完本讲，你应当能够：

1. 列出 chisel-npu 全部 **13 个 opcode 家族**及其 7 位编码值，并指出哪些编码区段被保留。
2. 说出「**opcode 选家族 → funct3 选子操作 → funct7 带属性**」这条三层译码逻辑。
3. 准确切分普通 R 型 `funct7` 里的 `width / round / sat / dtype` 四个子段。
4. 理解 `vcvt`（类型转换）家族**复用 `funct7` 但布局完全不同**的特殊编码，并用 `FmtCode` 解释源/目的格式。

本讲是理解译码器（u2-l5）和汇编器（u2-l4）的前置课——译码器和汇编器所做的，本质上就是把这三层规则翻译成代码。

## 2. 前置知识

本讲假设你已掌握 u2-l1 的内容，尤其：

- 32 位指令字的字段边界：`opcode[6:0]`、`rd[11:7]`、`funct3[14:12]`、`rs1[19:15]`、`rs2[24:20]`、`funct7[31:25]`。
- `funct7` 是「属性包」这一概念：它通常不再选子操作，而是携带执行属性。
- 全局参数 **N(bits)、L、K** 以及 **VX/VE/VR** 三类寄存器的别名关系（来自 u1-l4）。

如果你对这些还不熟悉，请先回到 u2-l1 和 u1-l4 复习。

补充两个本讲会用到的术语：

- **家族（family）**：一组功能相近的指令，例如「整数算术」「浮点算术」「矩阵乘」。`opcode` 决定属于哪个家族。
- **子操作（sub-operation）**：家族内部的具体动作，例如算术家族里的「加」「减」「乘」。`funct3` 决定哪个子操作。
- **属性（attribute）**：不改变操作本身、但改变执行方式的开关，例如「运算按哪种宽度」「舍入到哪个方向」「结果是否饱和」。这些塞在 `funct7` 里。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|:---|:---|
| [src/main/scala/isa/instSetArch.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala) | 定义 13 个 opcode 家族（`OpFamily` 枚举）和每个家族的 `funct3` 子操作常量。 |
| [src/main/scala/isa/instrFormat.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala) | 定义字段位段常量（`InstrBits`）、`funct7` 的两种属性布局，以及 `Funct7Attrs` / `CvtFunct7` / `FmtCode`。 |
| [docs/designs/01.isa.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/01.isa.md) | ISA 设计文档，含家族总表、funct7 子段表与各家族参考。 |

一句话记住分工：**`instSetArch.scala` 管「opcode → 家族」和「funct3 → 子操作」两层；`instrFormat.scala` 管「funct7 → 属性」这一层以及字段位段。**

---

## 4. 核心概念与源码讲解

本讲按译码的顺序拆成 4 个最小模块：先看 `opcode` 如何选家族（4.1），再看 `funct3` 如何选子操作（4.2），然后看普通 `funct7` 如何携带属性（4.3），最后看 `vcvt` 家族如何把 `funct7` 换成另一套布局（4.4）。

### 4.1 opcode 选家族：OpFamily 枚举

#### 4.1.1 概念说明

`opcode` 是 32 位指令字最低 7 位（`[6:0]`）的主译码字段。译码器拿到一条指令，**第一步就是看这 7 位**，判断它属于哪个功能家族。你可以把 opcode 想象成「大楼的门牌号」：先确定进哪扇门（家族），门内的楼层和房间（funct3、funct7）才有意义。

chisel-npu 一共定义了 13 个家族，但 7 位总共能表示 128 个值，所以大量编码被保留（reserved）。保留编码不是「没用」，而是「故意空出来」，译码器一旦遇到保留值就会判定为**非法指令**。这是 ISA 留出未来扩展空间的常见做法。

#### 4.1.2 核心流程

opcode 的译码流程是：

1. 从指令字提取 `instr[6:0]`。
2. 在 `OpFamily` 枚举里查这个值命中哪个家族。
3. 命中后，后续对 `funct3` 和 `funct7` 的解释就**完全依赖这个家族**——同一段比特在不同家族下含义不同。
4. 若 opcode 落在保留区段，直接判非法。

chisel-npu 的 13 个家族在 opcode 空间里**并不连续**，而是分成三簇：

- `0x00–0x03`：控制与访存（NOP、LD、ST、MMA）。
- `0x04–0x0F`：**保留**。
- `0x10–0x18`：VALU 向量运算大家族（算术、逻辑、归约、LUT、转换、广播、浮点、FMA、移动）。
- `0x19–0x7F`：**保留**。

为什么 `0x10` 开始是向量运算？这样用 `opcode[6:4]` 这一段就能快速区分「控制/访存/MMA（高位为 0）」与「VALU 运算（`0x1x`）」，便于译码器做初步分流。

#### 4.1.3 源码精读

`OpFamily` 是一个 ChiselEnum，每个家族绑定一个 7 位编码值：

[src/main/scala/isa/instSetArch.scala:L32-L46](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala#L32-L46) — 定义全部 13 个 opcode 家族及其编码值。

关键片段（仅列前几项）：

```scala
object OpFamily extends ChiselEnum {
  val NOP          = Value(0x00.U(7.W))
  val LD           = Value(0x01.U(7.W))
  val ST           = Value(0x02.U(7.W))
  val MMA          = Value(0x03.U(7.W))
  val VALU_ARITH   = Value(0x10.U(7.W))
  // ... 其余 VALU_* 家族
  val VALU_MOV     = Value(0x18.U(7.W))
}
```

注意几个细节：

- 用 `ChiselEnum` 而不是裸整数，是为了让译码器（u2-l5）能用 `instr.opcode === OpFamily.VALU_ARITH` 这样的语义化比较，而不是魔法数字。
- `0x00` 是 NOP，方便流水线塞空泡。
- `VALU_MOV`（`0x18`）是最后定义的家族；设计文档特别标注它「由 agent 在实现阶段加入、未经测试」，属于**提议中的扩展**，使用时要谨慎（详见 u2-l1 提到的注意点）。

设计文档里有一张家族总表，把 13 个家族按功能归类：

[docs/designs/01.isa.md:L150-L164](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/01.isa.md#L150-L164) — opcode 家族地图，明确写出保留区段 `0x04–0x0F` 与 `0x19–0x7F`。

把代码和文档对照，13 个家族的完整清单如下：

| 家族 | opcode | 簇 | 典型用途 |
|:---|:---:|:---:|:---|
| `NOP` | `0x00` | 控制/访存 | 空操作 |
| `LD` | `0x01` | 控制/访存 | 从内存装载到寄存器堆 |
| `ST` | `0x02` | 控制/访存 | 把寄存器堆存回内存 |
| `MMA` | `0x03` | 控制/访存 | 矩阵乘累加（脉动阵列） |
| *(保留)* | `0x04–0x0F` | — | 非法 |
| `VALU_ARITH` | `0x10` | VALU | 整数加减乘、max/min 等 |
| `VALU_LOGIC` | `0x11` | VALU | 移位、与或非异或 |
| `VALU_REDUCE` | `0x12` | VALU | 水平归约（sum/max/min） |
| `VALU_LUT` | `0x13` | VALU | 可编程查找表（vlut/vsetlut） |
| `VALU_CVT` | `0x14` | VALU | 类型转换 |
| `VALU_BCAST` | `0x15` | VALU | 标量广播到所有通道 |
| `VALU_FP` | `0x16` | VALU | FP32 浮点算术 |
| `VALU_FP_FMA` | `0x17` | VALU | 浮点融合乘加（S 型） |
| `VALU_MOV` ⚠ | `0x18` | VALU | 寄存器搬运/立即数装载（提议中） |
| *(保留)* | `0x19–0x7F` | — | 非法 |

#### 4.1.4 代码实践

**实践目标**：亲手把 13 个家族从源码里「统计」出来，建立 opcode 空间的全局心智地图。

**操作步骤**：

1. 打开 `src/main/scala/isa/instSetArch.scala`，定位 `object OpFamily`（第 32 行起）。
2. 逐行数 `Value(0xNN.U(7.W))`，记录每个家族名和它的十六进制编码。
3. 在纸上（或文本里）画一根从 `0x00` 到 `0x7F` 的数轴，标出哪几段有家族、哪几段是保留。
4. 对照 `docs/designs/01.isa.md` 的「Opcode Family Map」表格（第 166–181 行），核对你的清单是否一致。

**需要观察的现象**：

- 家族并不连续，`0x04–0x0F` 和 `0x19–0x7F` 是两段保留区。
- 控制类（NOP/LD/ST/MMA）挤在 `0x00–0x03`，而所有 VALU 运算挤在 `0x10–0x18`。

**预期结果**：你应得到上面那张 13 行的家族表，并且能口头说出「`0x10` 以上基本都是 VALU 家族」。

> 说明：本实践是源码阅读型，不需要运行硬件。表格内容已在 4.1.3 给出，可作为自检答案。

#### 4.1.5 小练习与答案

**练习 1**：opcode 有 7 位（128 个值），但只定义了 13 个家族。为什么不把 opcode 压缩到 4 位？

**参考答案**：留保留区有两个好处——一是给未来扩展新家族留位（比如新增卷积家族、DMA 家族）；二是 7 位 opcode 借鉴 RISC-V 的编码习惯，译码器可以用高位比特做粗分流（如 `opcode[6:4]` 区分控制类与 VALU 类），保留区还能直接当作非法指令的天然判定依据。

**练习 2**：如果有人提交一条 opcode = `0x07` 的指令，译码器会怎么处理？

**参考答案**：`0x07` 落在保留区 `0x04–0x0F`，`OpFamily` 里没有对应枚举值，译码器会把它判为非法指令（具体由 u2-l5 讲的 `illegal` 逻辑置位）。

---

### 4.2 funct3 选子操作：每个家族的子操作划分

#### 4.2.1 概念说明

确定了家族之后，还要知道「在这个家族里具体做哪一件事」。这就是 `funct3`（`[14:12]`，3 位）的职责：**它在家族内部选子操作**。3 位共 8 个槽，足够大多数家族使用；用不满的家族就把剩余槽标为保留。

需要强调的是：`funct3` 的含义**完全依赖家族**。同样是 `funct3 = 0b000`，在 `VALU_ARITH` 家族里是 `add`，在 `VALU_LOGIC` 里是 `sll`（左移），在 `MMA` 里是 `mma`，在 `VALU_FP` 里是 `fadd`。这正是「opcode 选家族 → funct3 选子操作」分层设计的体现。

#### 4.2.2 核心流程

funct3 的查表流程：

1. opcode 确定家族（如 `VALU_ARITH`）。
2. 提取 `instr[14:12]` 作为 funct3。
3. 在该家族对应的 `Funct3Xxx` 对象里查子操作。
4. 没有对应定义的 funct3 值 → 保留 → 判非法。

代码里每个家族都有一个独立的 `object Funct3Xxx`，把 funct3 值映射成语义化的常量名（如 `ADD`、`SUB`）。这种「一个家族一个 object」的写法，是为了让 funct3 的含义有明确的作用域，避免不同家族之间重名打架。

#### 4.2.3 源码精读

以算术家族为例，8 个子操作刚好用满 funct3 的 8 个槽：

[src/main/scala/isa/instSetArch.scala:L55-L64](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala#L55-L64) — `VALU_ARITH` 家族的 funct3 子操作表。

```scala
object Funct3Arith {
  val ADD  = 0.U(3.W)   // rd = rs1 + rs2
  val SUB  = 1.U(3.W)   // rd = rs1 - rs2
  val MUL  = 2.U(3.W)   // rd = rs1 * rs2
  val NEG  = 3.U(3.W)   // rd = -rs1  (rs2 ignored)
  val ABS  = 4.U(3.W)   // rd = |rs1| (rs2 ignored)
  val MAX  = 5.U(3.W)
  val MIN  = 6.U(3.W)
  val RSUB = 7.U(3.W)   // rd = rs2 - rs1  (reverse subtract)
}
```

注意几个家族在 funct3 分配上的特点：

- **逻辑家族尽量对齐 RISC-V**：`VALU_LOGIC` 的 `XOR=4`、`OR=6`、`AND=7` 与 RISC-V 的 funct3 一致，方便熟悉 RV 的人快速上手。但移位类（`SLL/SRL/SRA`）的取值顺序是项目自定义的，并不完全照搬 RV：

[src/main/scala/isa/instSetArch.scala:L66-L77](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala#L66-L77) — `VALU_LOGIC` 家族，注释自称「RISC-V aligned where possible」，实际只有高半段（XOR/OR/AND）真正对齐。

- **用不满 8 个槽的家族**：例如归约家族（`Funct3Reduce`，第 80–88 行）只定义了 6 个，注释明确写出「6, 7 reserved」；MMA 家族（`Funct3Mma`，第 161–166 行）只用了 3 个（`mma`/`mma.last`/`mma.reset`）。
- **混合格式的家族**：`VALU_LUT`（第 108–115 行）最特殊——它**同时用 R 型和 I 型**：`funct3=0/1` 是 R 型查表（`vlut`），`funct3=4/5` 是 I 型写表（`vsetlut`）。这是 funct3 还兼任「格式选择」的少数例子。

设计文档把所有家族的 funct3 表整理在一张大表里，方便速查：

[docs/designs/01.isa.md:L166-L181](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/01.isa.md#L166-L181) — 全家族 funct3 子操作对照表（含格式 R/I/S）。

#### 4.2.4 代码实践

**实践目标**：体会「同一个 funct3 值在不同家族含义不同」。

**操作步骤**：

1. 在 `instSetArch.scala` 里，分别查 `Funct3Arith`、`Funct3Logic`、`Funct3Fp`、`Funct3Mma` 四个对象中 `funct3 = 0`（即 `0.U(3.W)`）对应的子操作。
2. 再查 `funct3 = 1` 在这四个家族里分别是什么。
3. 把结果填进一张 4×2 的小表。

**需要观察的现象**：`funct3 = 0` 在不同家族对应完全不同的操作。

**预期结果**：

| funct3 | `VALU_ARITH` | `VALU_LOGIC` | `VALU_FP` | `MMA` |
|:---:|:---|:---|:---|:---|
| `0` | `ADD`（加） | `SLL`（逻辑左移） | `FADD`（浮点加） | `MMA`（开始累加） |
| `1` | `SUB`（减） | `SRL`（逻辑右移） | `FSUB`（浮点减） | `MMA_LAST`（收集结果） |

> 说明：本实践为源码阅读型，结论可直接对照 `Funct3Arith`(55–64)、`Funct3Logic`(68–77)、`Funct3Fp`(131–140)、`Funct3Mma`(161–166) 自检。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `funct3` 用 3 位就够，而不是给每个子操作也分配独立的 opcode？

**参考答案**：3 位能区分 8 个子操作，对绝大多数家族足够；如果每个子操作都占一个 opcode，128 个 opcode 空间很快用完，且无法体现「这些操作属于同一类」的语义。用「opcode 分家族 + funct3 分子操作」能高效复用编码空间。

**练习 2**：`Funct3Reduce` 用了 funct3 的哪几个值？哪几个被保留？

**参考答案**：用了 `0–5`（SUM/RMAX/RMIN/RAND/ROR/RXOR），`6` 和 `7` 保留。译码器遇到 funct3=6/7 的归约指令会判非法（见 `instSetArch.scala` 第 86–87 行注释）。

---

### 4.3 funct7 带属性：普通 R 型的属性编码

#### 4.3.1 概念说明

如果说 opcode 和 funct3 决定「做什么」，那么 `funct7`（`[31:25]`，7 位）在普通 R 型里决定「**怎么做**」——它携带一组执行属性。最关键的一点：**`funct7` 不再选子操作，而是被切成几个子段，每个子段是一个独立的开关。**

普通 R 型的 `funct7` 被切成 4 个子段：

| 子段 | funct7 内位段 | 含义 | 取值 |
|:---|:---:|:---|:---|
| `width` | `[1:0]` | 操作哪类寄存器（通道宽度） | VX=N 位 / VE=2N 位 / VR=4N 位 / 保留 |
| `round` | `[3:2]` | 舍入模式 | RNE / RTZ / floor / ceil |
| `sat` | `[4]` | 结果是否饱和 | wrap（回绕）/ saturate（饱和） |
| `dtype` | `[6:5]` | 数据类型大类 | INT / FP / BF / 保留 |

这里的 `width` 子段就是上一讲（u2-l1）和 u1-l4 提到的、用 2 个比特选择 VX/VE/VR 的那个字段——译码后它会被改名为 `regCls` 送给 VALU。所以 funct7 不只是「属性」，它还间接决定了指令操作哪一类寄存器。

#### 4.3.2 核心流程

funct7 的拆解流程：

1. 提取 `instr[31:25]` 作为 funct7。
2. 按 4 个子段的边界切片：`funct7[1:0]`、`funct7[3:2]`、`funct7[4]`、`funct7[6:5]`。
3. 每个子段到对应枚举（`VecWidth` / `VecRound` / `VecDtypeCls`）里查含义。
4. 把这些属性随译码结果一起送给执行单元。

为了让子段边界不出错，`instrFormat.scala` 用一组具名常量把每个子段的起止位钉死：

[src/main/scala/isa/instrFormat.scala:L56-L60](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L56-L60) — funct7 各属性子段的位段常量，译码器据此切字段。

```scala
val F7_WIDTH_LO = 0;  val F7_WIDTH_HI = 1   // [1:0] in funct7
val F7_ROUND_LO = 2;  val F7_ROUND_HI = 3   // [3:2] in funct7
val F7_SAT      = 4                          // [4]   in funct7
val F7_DTYPE_LO = 5;  val F7_DTYPE_HI = 6   // [6:5] in funct7
```

四个子段对应的枚举分别定义在：

- `VecWidth`：`VX=0 / VE=1 / VR=2 / VW_RSV=3`（[instrFormat.scala:L72-L77](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L72-L77)）。
- `VecRound`：`RNE=0 / RTZ=1 / FLOOR=2 / CEIL=3`（[instrFormat.scala:L82-L87](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L82-L87)）。
- `VecDtypeCls`：`INT=0 / FP=1 / BF=2 / DC_RSV=3`（[instrFormat.scala:L92-L97](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L92-L97)）。

#### 4.3.3 源码精读

`funct7` 的整体布局在文件头注释里写得最清楚：

[src/main/scala/isa/instrFormat.scala:L17-L28](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L17-L28) — 同时给出普通 R 型 funct7 布局和 vcvt 特殊布局的注释，是理解两种布局的权威说明。

把「属性如何打包成一个整数」封装成 case class 的是 `Funct7Attrs`：

[src/main/scala/isa/instrFormat.scala:L117-L125](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L117-L125) — `Funct7Attrs` 把 width/round/sat/dtype 打包成 7 位 funct7 整数，供汇编器和测试使用。

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
```

`encode` 就是把 4 个属性按位段拼起来的公式：

\[
\text{funct7} = \text{width} \;\big|\; (\text{round} \ll 2) \;\big|\; (\text{sat} \ll 4) \;\big|\; (\text{dtype} \ll 5)
\]

注意 `& 3` / `& 7` 这类掩码：它们防止某个属性值超出自己的位段宽度（比如 width 只有 2 位，写 5 也会被截成 1）。

> 小贴士：汇编器 `NpuAssembler.f7`（[NpuAssembler.scala:L52](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L52)）就是直接调用 `Funct7Attrs(...).encode` 的薄封装。所以理解了 `encode`，就理解了汇编器怎么造 funct7。这会在 u2-l4 详细讲。

一个值得注意的细节：**funct7 的语义也会随家族微调**。比如 `MMA` 家族里，`funct7[4]`（本应是 `sat`）被复用为 `keep`（累加使能）信号——见 `Funct3Mma` 注释「keep from funct7[4]」（[instSetArch.scala:L162](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala#L162)）与设计文档（[01.isa.md:L347](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/designs/01.isa.md#L347)）。这说明 funct7 是「按家族解释」的，但比 funct3 的变化小得多。

#### 4.3.4 代码实践

**实践目标**：手算一条「VE 宽度、RTZ 舍入、饱和、FP 类型」指令的 funct7 值，验证对属性布局的理解。这正是本讲规格里要求的实践任务。

**操作步骤**：

1. 先确定每个属性对应的枚举值：
   - `width = VE` → 查 `VecWidth`，`VE = 1`。
   - `round = RTZ` → 查 `VecRound`，`RTZ = 1`。
   - `sat = 饱和` → `true`（即 1）。
   - `dtype = FP` → 查 `VecDtypeCls`，`FP = 1`。
2. 套用 `encode` 公式手算。
3. （可选）若本地有 sbt 环境，在 `sbt console` 里 `import isa.instrFormat._` 后执行 `Funct7Attrs(width=1, round=1, sat=true, dtype=1).encode`，核对结果。若没有环境，按「源码阅读 + 手算」完成即可。

**需要观察的现象 / 预期结果**：

代入公式：

\[
\text{funct7} = 1 \;\big|\; (1 \ll 2) \;\big|\; (1 \ll 4) \;\big|\; (1 \ll 5)
            = 1 + 4 + 16 + 32 = 53 = \texttt{0x35} = \texttt{0b0110101}
\]

所以 funct7 = **`0x35`（十进制 53）**。逐位验证：

| 子段 | 位段 | 值 |
|:---|:---:|:---:|
| width=VE(1) | `[1:0]` | `01` |
| round=RTZ(1) | `[3:2]` | `01` |
| sat=1 | `[4]` | `1` |
| dtype=FP(1) | `[6:5]` | `01` |

拼起来 `0 1 1 0 1 0 1`（从 `[6]` 到 `[0]`）正是 `0x35`。由于 `funct7` 落在指令字的 `[31:25]`，它在完整 32 位字里的贡献是 `0x35 << 25 = 0x6A000000`（最高位 bit31=0，所以在 Scala 里是正数，poke 时不必特殊处理）。

> 说明：以上为确定性的手算结果，可作标准答案。本地若有 sbt 可按步骤 3 实跑核对。

#### 4.3.5 小练习与答案

**练习 1**：把上面例子里的「饱和」改成「不饱和」，其余不变（VE、RTZ、FP），funct7 变成多少？

**参考答案**：`sat=false` 后 `sat<<4` 这项为 0，于是 funct7 = `1 | 4 | 0 | 32 = 37 = 0x25`。

**练习 2**：为什么 `sat` 只占 1 位，而 `width/round/dtype` 各占 2 位？

**参考答案**：饱和是一个二元开关（饱和/回绕），1 位足够；而宽度有 VX/VE/VR 三种（加保留共 4 种）、舍入有 4 种模式、dtype 有 INT/FP/BF 三种（加保留共 4 种），都需要 2 位才能区分。funct7 总共 7 位 = 2+2+1+2，正好用满。

---

### 4.4 vcvt 的特殊 funct7：FmtCode 与 CvtFunct7

#### 4.4.1 概念说明

绝大多数家族的 funct7 都遵循 4.3 讲的 `width/round/sat/dtype` 布局，但**类型转换家族 `VALU_CVT` 是个例外**——它把 funct7 **完全换了一套布局**，因为转换指令的核心信息是「从哪种格式转成哪种格式」，这需要两个格式码，塞不进普通布局。

`vcvt` 的做法是：

- `funct3` 放**目的格式**（dst）。
- `funct7[2:0]` 放**源格式**（src）。
- 剩下的位放 sat、round、以及 BF8 的变体选择。

格式码统一由 `FmtCode` 定义，源和目的都用同一套编码：`s8 / s16 / s32 / f32 / bf16 / bf8`。

#### 4.4.2 核心流程

vcvt 指令的译码流程：

1. opcode 命中 `VALU_CVT`（`0x14`）。
2. **切换 funct7 解释方式**：不再按 width/round/sat/dtype 切，而是按 src/sat/round/bf8var 切。
3. `funct3` 查 `FmtCode` 得到 dst 格式；`funct7[2:0]` 查 `FmtCode` 得到 src 格式。
4. 若 `src == dst`（没意义），判非法。
5. 输出寄存器类别由 dst 格式决定（s8→VX、s16/bf16→VE、s32/f32→VR）。

vcvt 的 funct7 布局如下（与 4.3 截然不同）：

| 子段 | funct7 内位段 | 含义 |
|:---|:---:|:---|
| `src fmt` | `[2:0]` | 源格式码（FmtCode） |
| `sat` | `[3]` | 输出窄化时是否饱和 |
| `round` | `[5:4]` | 舍入模式 |
| `BF8 var` | `[6]` | BF8 变体：0=E4M3，1=E5M2 |

#### 4.4.3 源码精读

格式码 `FmtCode` 是 vcvt 的「字典」：

[src/main/scala/isa/instrFormat.scala:L102-L111](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L102-L111) — `FmtCode` 定义 6 种数据格式码，src 和 dst 共用。

```scala
object FmtCode {
  val S8   = 0.U(3.W)
  val S16  = 1.U(3.W)
  val S32  = 2.U(3.W)
  val F32  = 3.U(3.W)
  val BF16 = 4.U(3.W)
  val BF8  = 5.U(3.W)   // variant (E4M3/E5M2) comes from funct7[6]
  val RSV6 = 6.U(3.W)
  val RSV7 = 7.U(3.W)
}
```

注意 `BF8` 自身不区分 E4M3/E5M2，那个区分单独由 funct7 的第 6 位（`BF8 var`）决定——这是 funct7 多复用一个比特的巧妙之处。

vcvt 的特殊 funct7 注释在两个文件里都有：

- [src/main/scala/isa/instrFormat.scala:L23-L28](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L23-L28) — funct7 文件头注释里关于 vcvt 布局的说明。
- [src/main/scala/isa/instSetArch.scala:L117-L120](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala#L117-L120) — instSetArch 里对 `VALU_CVT` 的 funct3/funct7 用法注释（注意此处没有独立 `Funct3Cvt` 对象，因为 funct3 直接就是 FmtCode）。

把 vcvt 的 funct7 打包逻辑封装成 case class 的是 `CvtFunct7`：

[src/main/scala/isa/instrFormat.scala:L127-L135](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L127-L135) — `CvtFunct7` 把 src 格式、饱和、舍入、BF8 变体打包成 funct7。

```scala
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

对应的位段常量在 `InstrBits` 里另起一组 `F7_CVT_*`，和普通布局的 `F7_*` 区分开（[instrFormat.scala:L63-L66](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala#L63-L66)）。

> 小贴士：和普通 funct7 一样，汇编器有对应的薄封装 `NpuAssembler.f7Cvt`（[NpuAssembler.scala:L56](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L56)）。命名约定 `vcvt_<dst>_<src>`：`vcvt_s8_f32` = FP32 输入 → INT8 输出（窄输出，写 VX）。

#### 4.4.4 代码实践

**实践目标**：手算一条 vcvt 指令的 funct7，体会「vcvt 用另一套 funct7 布局」。

**操作步骤**：

1. 选一条具体的转换：`vcvt_s8_f32`（FP32 → INT8，饱和），即 src=`F32`、dst=`S8`。
2. 查 `FmtCode`：`F32 = 3`，`S8 = 0`。所以 `funct7[2:0] = src = 3`，`funct3 = dst = 0`。
3. 设 `sat=true`、`round=RNE=0`、`bf8E5M2=false`，套用 `CvtFunct7.encode` 公式手算。

**需要观察的现象 / 预期结果**：

\[
\text{funct7}_{\text{cvt}} = (3 \;\&\; 7) \;\big|\; (1 \ll 3) \;\big|\; (0 \ll 4) \;\big|\; (0 \ll 6)
                          = 3 + 8 = 11 = \texttt{0x0B} = \texttt{0b0001011}
\]

逐位验证：

| 子段 | 位段 | 值 |
|:---|:---:|:---:|
| src=F32(3) | `[2:0]` | `011` |
| sat=1 | `[3]` | `1` |
| round=RNE(0) | `[5:4]` | `00` |
| BF8var=0(E4M3) | `[6]` | `0` |

所以这条 `vcvt_s8_f32.sat` 的 funct7 = **`0x0B`**，funct3 = `0x0`（dst=S8）。注意它和普通 funct7 的布局不可混用——同一个 `0x0B`，在普通家族里会被解释成「width=`11`(保留)、round=`00`、sat=`0`、dtype=`00`」，完全是无意义的保留组合。

> 说明：本实践为确定性手算，结果可作标准答案。本地有 sbt 时可用 `CvtFunct7(srcFmt=3, sat=true).encode` 核对。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `vcvt_s8_s8`（src 和 dst 都是 INT8）会被判非法？

**参考答案**：源和目的格式相同意味着「什么也不转」，没有意义。译码器据此把 `src == dst` 的 cvt 判为非法指令（详见 u2-l5 的非法判定逻辑）。

**练习 2**：BF8 有 E4M3 和 E5M2 两种变体，但 `FmtCode.BF8` 只有一个码。如何区分这两种变体？

**参考答案**：靠 funct7 的第 6 位 `BF8 var`：`0`=E4M3，`1`=E5M2。这样 `FmtCode` 用一个码（`5`）表示「是 BF8」，再用 funct7 第 6 位区分具体变体，省下了 funct3/funct7[2:0] 的编码空间。

---

## 5. 综合实践

把本讲的三层译码知识串起来，完成下面这张「完整译码工作单」。任选 **3 条不同家族的真实指令**（例如一条 `vadd`、一条 `mma.last`、一条 `vcvt_f32_s8`），对每一条指令填出下表：

| 项目 | 指令 1（ARITH） | 指令 2（MMA） | 指令 3（CVT） |
|:---|:---|:---|:---|
| opcode（家族） | `0x10` = VALU_ARITH | `0x03` = MMA | `0x14` = VALU_CVT |
| funct3（子操作） | ? | ? | ? |
| funct7 布局类型 | 普通（width/round/sat/dtype） | 普通（但 `[4]`=keep） | **特殊（src/sat/round/bf8var）** |
| funct3 怎么解释 | 选算术子操作 | 选 mma 子操作 | = dst 格式码 |
| funct7 怎么解释 | 4 属性 | 4 属性（keep 复用 `[4]`） | src 格式 + 舍入 + 饱和 + BF8 变体 |
| 输出写哪类寄存器 | 由 width 决定 | VR（MMALU 直写 VR） | 由 dst 格式决定 |

**完成步骤**：

1. 对每条指令，从 `instSetArch.scala` 查 opcode 家族和 funct3 子操作。
2. 判断 funct7 用普通布局还是 cvt 特殊布局。
3. 若是普通布局，用 4.3 的方法确定 width（决定写哪类寄存器）；若是 cvt，用 4.4 的方法确定 dst 格式。
4. 用一句话总结：**「opcode 定家族、funct3 定子操作、funct7 定属性，而 vcvt 把后两层都换成了格式码」**。

**预期结果**：你能对着任意一条 32 位指令，说出它的三层含义，并且不再混淆普通 funct7 与 cvt funct7。这是进入 u2-l5（译码器）和 u2-l4（汇编器）的必备能力。

> 说明：本实践以源码阅读和填表为主，不依赖硬件运行。表格中带「?」的格子由读者自行查源码填写。

## 6. 本讲小结

- chisel-npu 有 **13 个 opcode 家族**（`OpFamily` 枚举），opcode 是最低 7 位的主译码字段；`0x04–0x0F` 与 `0x19–0x7F` 是保留区，译码器遇之判非法。
- 译码遵循三层结构：**opcode 选家族 → funct3 选子操作 → funct7 带属性**；同一段 funct3/funct7 比特在不同家族含义不同。
- 普通 R 型的 `funct7` 切成 4 个属性子段：`width[1:0]` / `round[3:2]` / `sat[4]` / `dtype[6:5]`，由 `Funct7Attrs.encode` 打包，汇编器 `f7` 直接复用。
- `vcvt` 家族**复用 funct7 但布局完全不同**：`funct3`=dst 格式、`funct7[2:0]`=src 格式，外加 sat/round/BF8 变体，由 `CvtFunct7` 与 `FmtCode` 支持。
- funct7 的语义也会随家族微调（如 MMA 把 `funct7[4]` 复用为 `keep`），但变化远小于 funct3。

## 7. 下一步学习建议

- **u2-l4（Scala 汇编器 NpuAssembler）**：本讲多次提到的 `f7` / `f7Cvt` 就在那里，你会看到 `encR/encI/encS` 如何把 opcode、funct3、funct7 拼成完整 32 位指令字。
- **u2-l5（组合译码器 InstrDecoder）**：译码器做的就是本讲三层规则的「逆运算」——从 32 位字里切出 opcode/funct3/funct7 并判定非法，建议紧接着学。
- **复习建议**：若对 width 与 VX/VE/VR 的关系仍不熟，回到 u1-l4；若对指令字位段边界不熟，回到 u2-l1。
