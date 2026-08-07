# 组合译码器 InstrDecoder

## 1. 本讲目标

本讲讲解 chisel-npu 把一条 32 位指令字「翻译」成执行单元能直接消费的控制信号的部件——`InstrDecoder`。学完后你应该能够：

- 说出 `InstrDecoder` 是一个**纯组合逻辑**模块（无寄存器、单拍输出），并解释它为什么能放在发射（issue）的同拍完成。
- 描述译码的两层结构：opcode 选**家族（family）**、funct3 选**子操作（VecOp）**，再从 funct7 解出 width/round/sat/dtype 等属性。
- 读懂译码输出 `DecodedMicroOp` 这个 Bundle，知道它的每个字段送给谁（VALU / MMALU / 寄存器堆）。
- 准确列出非法指令（illegal）被判定的**五道关卡**：保留 opcode、非法 funct3、保留 width、保留 dtype、以及 CVT 的 `src == dst`。
- 参照 `InstrDecoderSpec`，自己用 `NpuAssembler` 构造一条合法指令和一条非法指令，在仿真里验证 `illegal` 与 `family` 输出。

本讲是 u2-l1（指令格式）、u2-l2（opcode 家族与 funct3/funct7 属性）和 u2-l4（汇编器）的「正向收口」：汇编器解决「意图 → 32 位字」，译码器解决「32 位字 → 控制信号」，两者互为对偶。下一单元 u3 将开始消费这些控制信号。

## 2. 前置知识

在读懂本讲之前，请确认你已经掌握以下概念（来自前置讲义）：

- **三种指令格式 R/I/S**：32 位字里 opcode[6:0]、rd[11:7]、funct3[14:12]、rs1[19:15] 是共用的，高位 [31:20] 随格式而变（见 u2-l1）。
- **三层译码模型**：opcode 选家族、funct3 选子操作、funct7 带属性（见 u2-l2）。
- **funct7 属性包**：普通 R 型用 `Funct7Attrs`（width/round/sat/dtype），类型转换家族 vcvt 用 `CvtFunct7`（src/sat/round/bf8 变体），两者位布局不同，不可混用（见 u2-l2、u2-l4）。
- **OpFamily 枚举**：13 个家族，编码不连续，`0x04–0x0F` 与 `0x19–0x7F` 是保留区（见 u2-l2）。
- **NpuAssembler 的 `encR/encI/encS` 与 `f7/f7Cvt`**：手工拼接 32 位指令字的原语（见 u2-l4）。
- **寄存器类 VX/VE/VR**：funct7[1:0] 这 2 个比特选择操作哪类寄存器，译码后在 VALU 控制包里被改名为 `regCls`（见 u1-l4）。

如果你对上述任一条还陌生，建议先回看对应讲义。本讲会直接使用 `instr(HI, LO)` 切片、`OpFamily.safe()` 安全转换、`VecOp` 枚举等机制，不再从零解释。

## 3. 本讲源码地图

本讲涉及的关键文件与各自职责：

| 文件 | 作用 |
| --- | --- |
| [src/main/scala/isa/instrDecoder.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala) | **本讲主角**。纯组合译码器，输入 32 位 `instr`，输出 `DecodedMicroOp` 与 `illegal` 标志。 |
| [src/main/scala/isa/instrFormat.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrFormat.scala) | 位段常量 `InstrBits`，以及 `VecWidth`、`FmtCode`、`Funct7Attrs`、`CvtFunct7` 等编码定义。译码器切字段全靠它。 |
| [src/main/scala/isa/instSetArch.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala) | `OpFamily` 家族枚举与每家族的 `Funct3*` 子操作码表。译码器的 `switch` 就是照着它写的。 |
| [src/main/scala/isa/micro_op/VALUMicroCode.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala) | `VecOp` 枚举与 `NCoreVALUBundle`。译码器要把 `vecOp` 与属性填进这个包。 |
| [src/main/scala/isa/NpuAssembler.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala) | Scala 汇编器。本讲的代码实践用它构造测试指令字。 |
| [src/test/scala/isa/InstrDecoderSpec.scala](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala) | 译码器的仿真测试，是本讲实践的模板。 |

阅读建议：先扫一眼 `instrDecoder.scala` 顶部 L1–L14 的注释，作者把非法判定的五条规则全列在那里了，本讲 4.3 就是把那五条逐条对应到代码。

## 4. 核心概念与源码讲解

### 4.1 InstrDecoder 模块：组合译码的整体流程

#### 4.1.1 概念说明

「译码（decode）」是 CPU/NPU 前端的核心动作：取回来的指令是一串冷冰冰的 0/1（一个 32 位字），执行单元（这里是 VALU 与 MMALU）并不关心原始比特，它们只想知道「做什么操作、用哪几个寄存器、用什么宽度、要不要饱和」。译码器就是把原始字翻译成这套「控制信号」的翻译官。

chisel-npu 的 `InstrDecoder` 有一个关键设计选择：**它是纯组合逻辑**。模块顶部注释写得很直白：

> One clock cycle: combinational only, no registers. The decoded bundle reaches execution units in the same issue cycle.

也就是说它内部**没有任何寄存器**，输入 `instr` 一变，输出 `DecodedMicroOp` 在同一拍立刻就绪。这带来两个后果：

1. **延迟低**：译码不占流水级，发射（issue）这一拍就能把控制信号送到执行单元。
2. **综合后是一坨布尔逻辑**：你看到的所有 `switch/is` 在综合后都变成多路选择器（MUX），非法判定是一棵 OR 树，全部在组合路径上。这也是为什么后面的 `MMALU` 要专门插流水寄存器修时序——译码组合路径若太长会成为关键路径（见 u4-l5）。

#### 4.1.2 核心流程

`InstrDecoder` 的执行过程可以拆成六个阶段，全部在同一拍内完成：

```text
32 位 instr
   │
   ├─① 切字段 (InstrBits 常量切 opcode/funct3/funct7/rd/rs1/rs2/imm)
   │
   ├─② 定家族  opcode → OpFamily.safe() → (family, familyOK)
   │            (保留 opcode 命中 → familyOK=false)
   │
   ├─③ 选子操作  (family, funct3) → VecOp   (两层 switch)
   │            (保留 funct3 → f3Valid=false，vecOp 保持默认 vadd)
   │
   ├─④ 解属性    funct7 → width / dtype / round / sat
   │            (保留 width=11 / dtype=11 → widthIllegal / dtypeIllegal)
   │            特殊家族：FP/CVT 强制 VR；MMA 从 f7Sat 复用出 keep
   │
   ├─⑤ 判非法    illegal = !familyOK || !f3Valid || widthIllegal || dtypeIllegal
   │
   └─⑥ 驱动输出  把 family / vecOp / 属性 / rd/rs1/rs2 / mma_* 填进 DecodedMicroOp
```

注意第 ③ 步的兜底设计：`vecOp` 默认是 `vadd`（一个无害操作）。即使 funct3 落在保留值上、`vecOp` 没被赋新值，输出也不会是垃圾——它会保持 `vadd`，但此时 `illegal` 已被置位，后端会据此**抑制写回**，所以这条「假 vadd」不会污染寄存器堆。注释里明确写了这一点：`Default = vadd (harmless; illegal flag suppresses write-back)`。

#### 4.1.3 源码精读

**IO 定义**——输入一个 32 位字，输出一个解码包加一个 illegal 标志：

[InstrDecoder 的 IO（src/main/scala/isa/instrDecoder.scala:L40-L45）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L40-L45) — 三个端口：`instr`（输入 32 位）、`decoded`（输出 `DecodedMicroOp`）、`illegal`（输出 Bool）。

**① 切字段**——用 `InstrBits` 里的具名常量统一切片，避免到处写魔数 `[6:0]`：

```scala
val opBits  = io.instr(InstrBits.OPCODE_HI, InstrBits.OPCODE_LO)  // [6:0]
val rdBits  = io.instr(InstrBits.RD_HI,     InstrBits.RD_LO)      // [11:7]
val f3      = io.instr(InstrBits.FUNCT3_HI,  InstrBits.FUNCT3_LO) // [14:12]
...
val f7Width = f7(InstrBits.F7_WIDTH_HI, InstrBits.F7_WIDTH_LO)    // funct7 的 width 子段
```

这一段对应 [字段抽取（src/main/scala/isa/instrDecoder.scala:L47-L72）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L47-L72)。其中 I 型立即数还做了符号扩展：`immI = io.instr(...).asSInt`；CVT 家族专属子段 `f7CvtSrc/f7CvtSat/f7CvtRnd/f7Bf8` 也在这里一并切出（注意它们和普通 R 型的 width/round/sat/dtype **共用同一片 funct7 比特但解释不同**，这正是 u2-l2 强调的「funct7 是属性包」）。

**② 定家族**——`OpFamily` 是 ChiselEnum，最大值 `0x18=24`，自动推断只需 5 位；而 opcode 是 7 位，所以先截断再做「安全转换」：

[安全转换家族（src/main/scala/isa/instrDecoder.scala:L74-L82）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L74-L82) — `OpFamily.safe(opBitsTrunc)` 返回一个 `(value, valid)` 元组：若截断后的 5 位值能映射到某个枚举成员，`familyOK=true`，否则 `false`。这是 ChiselEnum 推荐的「非法值不抛异常、而是给一个有效标志」的写法，译码器据此判保留 opcode。

> 小知识：为什么先截断再 safe？因为 `OpFamily` 枚举宽度是按最大值 `0x18` 算出来的 5 位，而 `opBits` 是 7 位，宽度不匹配无法直接 safe-cast。截到低 5 位后，`0x7F`（保留）截成 `0x1F=31`，不在枚举里，于是 `familyOK=false`——这正是测试里 `0x7F` 被判非法的原理。

**③ 选子操作**——一个嵌套两层的大 `switch`，外层按 family，内层按 funct3：

[VALU 子操作译码（src/main/scala/isa/instrDecoder.scala:L91-L200）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L91-L200) — 这就是 u2-l2 讲的「opcode 选家族、funct3 选子操作」落到代码里的样子。例如 `VALU_ARITH` 家族里 `funct3=ADD(0)` → `vecOp := VecOp.vadd`。每个家族的内层 `switch` 只列出该家族已定义的 funct3 值，没列到的（保留值）会让 `vecOp` 保持默认 `vadd`，同时在能判定的地方把 `f3Valid` 拉低（详见 4.3）。

CVT 家族是这里的「异类」：它不是用 funct3 选子操作，而是用 `(dst=f3, src=f7CvtSrc)` 的组合，通过一个 `MuxCase` 选出具体的 `vcvt_*` VecOp（见 L143–L165）。MMA、LD、ST、NOP 家族根本不产生 vecOp（`vecOp` 保持默认），它们走的是另一套控制字段（mma_keep/last/reset、mem_width）。

**④ 解属性**——从 funct7 解出 width/dtype/round/sat，并对特殊家族做覆盖：

[width 译码与覆盖（src/main/scala/isa/instrDecoder.scala:L204-L230）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L204-L230) — 默认按 `f7Width` 选 VX/VE/VR；但 FP 与 FMA 家族恒为 VR、CVT 保守取 VR、BCAST-IMM 取 VX、vsetlut 取 VR。这些覆盖反映了一个事实：**有些家族的宽度不由 funct7[1:0] 决定，而是由操作语义隐含**（FP 只在 VR 上做；BCAST-IMM 立即数总是进 VX）。

**MMA 控制**——keep/last/reset 三位，其中 `keep` 复用了 funct7 的 sat 位：

[MMA 控制译码（src/main/scala/isa/instrDecoder.scala:L254-L260）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L254-L260) — `mmaKeep := f7Sat.asBool`，即 MMA 家族把 funct7[4]（别处的「饱和位」）重新解释成「累加使能」。这就是 u2-l4 里 `mma(..., keep=true)` 用 `f7(VR, sat=keep)` 来编码 keep 的原因——同一段比特，在 ARITH 家族是 sat，在 MMA 家族是 keep。

**⑥ 驱动输出**——把上面算出的所有信号填进 `DecodedMicroOp`：

[驱动输出（src/main/scala/isa/instrDecoder.scala:L269-L299）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L269-L299) — 注意 `round` 字段用了一个三层 `Mux` 链：FMA 家族取 S 型的 `rndS`、CVT 取 `f7CvtRnd`、LUT 取 `Cat(0.U(1.W), f3(0))`（即把 funct3 的最低位当 bank 选择塞进 round[0]）、其余取 `f7Round`。一个字段、四种来源，全靠家族区分——这是「瘦指令 + 胖译码」的典型体现。

#### 4.1.4 代码实践

**实践目标**：跑一遍现成的 `InstrDecoderSpec`，亲眼看到译码器把汇编器造的字正确解出来。

**操作步骤**：

1. 在仓库根目录执行单测快捷脚本（它内部是 `docker run ... sbt "testOnly ..."`，见 u1-l2）：
   ```bash
   ./tool/test-specific-spec.sh isa.InstrDecoderSpec
   ```
2. 观察输出里每个 `"InstrDecoder" should "..."` 用例是否全绿。
3. 挑一个用例，比如 `decode vadd VX`，对照 [InstrDecoderSpec:L44-L50](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L44-L50)，理解它 `poke` 的是 `vadd(rd=1, rs1=2, rs2=3, width=VX)` 这个字，然后 `expect` 出 `rd=1, rs1=2, rs2=3, regCls=0(VX)`。

**需要观察的现象**：

- 所有 `expect` 通过，`illegal` 在合法用例里为 `false`、在非法用例里为 `true`。
- 用例注释（L38–L40）提到：`family`、`op`、`dtype` 这几个 ChiselEnum 字段**没有用 `.expect()` 直接比对**，而是「间接验证」。原因是 Chisel 6 的 `EphemeralSimulator` 里对枚举字段直接 `expect` 不便，正确性交给下游 VALU 功能测试兜底。这个细节会在 u9-l1 详细讲。

**预期结果**：测试全部通过（绿）。如果你看到红，多半是环境问题（Docker 镜像未拉取、firtool 缺失），回到 u1-l2 排查。若你无法本地运行，记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `vecOp` 的默认值是 `VecOp.vadd` 而不是 0 或随便一个值？如果默认成一个「会写回」的操作会怎样？

> **参考答案**：`vadd` 被选作默认是因为它「无害」——即便一条非法指令让 `vecOp` 保持默认，只要 `illegal=1`，后端就会抑制写回，这条假 `vadd` 不会真的执行出可见效果。如果默认成一个危险的写回操作，且后端的 illegal 抑制逻辑有 bug，就会污染寄存器堆。这是一种「故障安全（fail-safe）」的默认值选择。

**练习 2**：`OpFamily.safe()` 为什么要返回 `(value, valid)` 元组，而不是直接 `OpFamily.fromBits` 抛异常？

> **参考答案**：这是硬件，不是软件。运行时抛异常没有意义——综合后的电路必须在**每一个**可能的 7 位输入上给出确定的输出。`safe` 把「输入是否合法」编码成一个硬件信号 `familyOK`，让电路对保留 opcode 也能稳定地产出一个「非法」结论，而不是崩溃。这正是非法判定第一道关卡（`!familyOK`）的来源。

---

### 4.2 DecodedMicroOp Bundle：译码结果的统一表示

#### 4.2.1 概念说明

译码器不能只吐出一个 `VecOp` 就完事——执行一条指令还需要目标寄存器号、源寄存器号、宽度、饱和、舍入、立即数……以及区分「这条指令是发给 VALU 的，还是发给 MMALU 的」。`DecodedMicroOp` 就是把这些信息打包成一个 Bundle，作为译码器的**单一输出**，下游所有执行单元都从它各取所需。

它和原始指令字的关系是：原始字是「压缩态」（所有信息塞进 32 位），`DecodedMicroOp` 是「展开态」（每个字段独立成线）。展开后，VALU 不必再去切比特，直接读 `decoded.valu.op` 就知道自己要做什么。

#### 4.2.2 核心流程

`DecodedMicroOp` 的字段流向三个消费者：

```text
DecodedMicroOp
   ├─ family     → backend 用它做总分发（VALU 分支 vs MMA 分支）
   ├─ valu       → 整个 NCoreVALUBundle 直接接 VALU.io.ctrl
   │     ├─ op/regCls/dtype/saturate/round/rs3_idx/imm
   ├─ mma_keep   ┐
   ├─ mma_last   ├→ MMALU 的累加/收集/复位控制
   ├─ mma_reset  ┘
   ├─ rd/rs1/rs2 → 寄存器堆地址译码（按 regCls 决定实际索引哪类寄存器）
   └─ mem_width  → LD/ST 传输宽度（funct3，见 u2-l3）
```

注意 `valu` 字段本身又是一个嵌套 Bundle（`NCoreVALUBundle`），所以 `DecodedMicroOp` 是「包中包」。这种分层让 VALU 的控制信号自成一体，可以整块复制、整块接线，非常干净。这些字段在 u6（NCoreBackend 集成）里会被实际连线，本讲只关心它们「是什么、从哪来」。

#### 4.2.3 源码精读

**DecodedMicroOp 定义**：

```scala
class DecodedMicroOp extends Bundle {
  val family    = OpFamily()
  val valu      = new NCoreVALUBundle
  val mma_keep  = Bool()   // MMALU: keep/accumulate signal
  val mma_last  = Bool()   // MMALU: assert clct
  val mma_reset = Bool()   // MMALU: clear accumulator
  val rd        = UInt(5.W)
  val rs1       = UInt(5.W)
  val rs2       = UInt(5.W)
  val mem_width = UInt(3.W) // ld/st funct3
}
```

对应 [DecodedMicroOp（src/main/scala/isa/instrDecoder.scala:L25-L35）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L25-L35)。可以看到它把一条指令的全部解码信息都铺平了：家族、VALU 控制包、MMALU 三位控制、三个寄存器号、访存宽度。

**嵌套的 NCoreVALUBundle**——VALU 的完整控制包：

[NCoreVALUBundle（src/main/scala/isa/micro_op/VALUMicroCode.scala:L138-L146）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/micro_op/VALUMicroCode.scala#L138-L146) — 字段包括 `op`（VecOp 枚举）、`dtype`（数据类型/BF8 变体）、`saturate`、`round`、`regCls`（0=VX/1=VE/2=VR）、`rs3_idx`（FMA 第三源）、`imm`（I 型符号扩展立即数）。注释特意说明 `regCls` 这个名字是为了**避开与 `chisel3.Width` 的命名冲突**——这就是 u1-l4 提到的「width 在送给 VALU 时被改名为 regCls」的落点。

> 术语解释：**ChiselEnum 字段**（如 `family = OpFamily()`、`op`、`dtype`）综合后会变成固定宽度的 UInt，但在 Scala/仿真层面它带类型信息，不能和普通 UInt 直接混用。这也是为什么测试里要绕一下来验证它们（见 4.1.4 的观察项）。

#### 4.2.4 代码实践

**实践目标**：用源码阅读的方式，把一条具体的 `vadd` 指令字「手算」成 `DecodedMicroOp` 的字段值，验证你真的读懂了字段流向。

**操作步骤**：

1. 在 sbt console（或纸上）算出这条指令字：
   ```scala
   import isa.NpuAssembler._
   val w = vadd(rd=1, rs1=2, rs2=3, width=VX)   // width=VX=0, sat=false
   ```
   回顾 u2-l4：`vadd` 走 `encR(0x10, 0, f7(width=0), 1, 2, 3)`。
2. 手工把这个字按 `InstrBits` 切开，填出下表：

   | DecodedMicroOp 字段 | 来源比特 | 值 |
   | --- | --- | --- |
   | `family` | opcode[6:0]=0x10 | `VALU_ARITH` |
   | `valu.op` | (family=ARITH, funct3=0) | `VecOp.vadd` |
   | `valu.regCls` | funct7[1:0]=0 | 0 (VX) |
   | `valu.saturate` | funct7[4]=0 | false |
   | `valu.round` | funct7[3:2]=0 | 0 (RNE) |
   | `rd` | [11:7] | 1 |
   | `rs1` | [19:15] | 2 |
   | `rs2` | [24:20] | 3 |
   | `mma_keep/last/reset` | family≠MMA | 全 false |

3. 对照 [4.1.3 的驱动输出代码](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L269-L299)，确认每一行赋值都对应你表里的一项。

**需要观察的现象 / 预期结果**：你的手算表应与 `InstrDecoderSpec` 里 `decode vadd VX` 用例（[L44-L50](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L44-L50)）的期望值完全一致：`rd=1, rs1=2, rs2=3, regCls=WX(0)`，`illegal=false`。这是「待本地验证」型的练习——重点是建立「字 → 字段」的映射直觉，而不是真去跑命令。

#### 4.2.5 小练习与答案

**练习 1**：一条 `vfma`（S 型）指令的 `DecodedMicroOp.valu.rs3_idx` 从哪个比特来？为什么 `DecodedMicroOp` 里没有单独的 `rs3` 字段？

> **参考答案**：`rs3_idx` 来自 S 型的 [31:27]（`InstrBits.RS3_HI/LO`）。它被收进嵌套的 `NCoreVALUBundle.rs3_idx` 而不是顶层，是因为只有 FMA 家族用得到第三源操作数，把它归到 VALU 控制包里更内聚——MMALU 和访存单元根本不需要 rs3。

**练习 2**：`DecodedMicroOp` 里 `mem_width` 字段宽度是 3 位，为什么直接存的是 funct3 而不是已经译码过的「字节/半字/字」枚举？

> **参考答案**：因为 LD/ST 子系统当前仍是脚手架（见 u2-l3），译码器只把 funct3 原样透传出去，把「传输宽度」的具体解释推迟到将来访存单元实现时再做。这是一种「先留管道、后填语义」的增量设计。注意 LD/ST 家族在译码器里**不产生 VecOp**，也不判 funct3 非法——保留值暂时被允许通过。

---

### 4.3 illegal 判定逻辑：非法指令的五道关卡

#### 4.3.1 概念说明

真实的程序（或固件）可能因为 bug、损坏或攻击，送进来一些「看起来像指令、但根本没定义」的 32 位字。如果硬件把这些字硬解释成某个操作并执行、写回，后果不可控。所以译码器必须在同一拍里**判定这条指令是否合法**，给出一个 `illegal` 标志，让后端据此抑制它的所有副作用（不写回、不发起访存、不累加）。

chisel-npu 的非法判定是**纯组合的、悲观（fail-safe）的**：只要任一一关命中，`illegal` 就拉高。模块顶部注释把规则总结成五条，本节把它们一一对应到代码。需要强调：译码器**只负责置标志**，真正的「抑制写回」动作发生在后端 `SimpleBackend`（见 u6-l2）；译码器不阻塞、不抛异常，它只是把球传给后端。

#### 4.3.2 核心流程

`illegal` 是四个条件信号的「或」：

```text
illegal = !familyOK        // 关卡1: 保留 opcode (例: 0x7F)
       || !f3Valid         // 关卡2: 保留 funct3 / CVT src==dst / LUT 保留值
       || widthIllegal     // 关卡3: funct7[1:0]=11(保留 width)，且非 FP/FMA/CVT
       || dtypeIllegal     // 关卡4: funct7[6:5]=11(保留 dtype)
```

注意关卡 2 其实「内含」了好几个子情形，因为不同家族对 funct3 合法性的要求不同。下面逐条看代码。

#### 4.3.3 源码精读

**关卡 1：保留 opcode**——来自 4.1.3 讲的 `familyOK`：

[!familyOK → illegal（src/main/scala/isa/instrDecoder.scala:L264）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L264) — opcode 截断后落在 `0x04–0x0F` 或 `0x19–0x7F` 这两个保留区（见 u2-l2 的 OpFamily 表），`OpFamily.safe()` 返回 `familyOK=false`，于是 illegal 拉高。测试里用 `0x7F` 验证（[InstrDecoderSpec:L238-L245](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L238-L245)）。

**关卡 2：保留 funct3 / CVT src==dst / LUT 保留值**——`f3Valid` 默认 true，由各家族按需拉低：

- **LUT 家族的保留 funct3**（2/3/6/7）：

[LUT 保留 funct3 判非法（src/main/scala/isa/instrDecoder.scala:L139-L141）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L139-L141) — `when (f3 === 2.U || f3 === 3.U || f3 === 6.U || f3 === 7.U) { f3Valid := false.B }`。测试见 [InstrDecoderSpec:L146-L157](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L146-L157)，它对 `encR(0x13, f3, f7(VX), 0, 1, 0)` 在 f3∈{2,3,6,7} 上逐个验证 illegal。

- **CVT 家族的 `src == dst`**：

[CVT src==dst 判非法（src/main/scala/isa/instrDecoder.scala:L164）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L164) — `when (dst === src) { f3Valid := false.B }`。把一个数「转成它自己」是没意义的操作，ISA 直接判非法。测试构造 `encR(0x14, 3, f7Cvt(srcFmt=3), 0, 0, 0)`（dst=F32=3、src=F32=3）来命中（[InstrDecoderSpec:L247-L255](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/isa/InstrDecoderSpec.scala#L247-L255)）。

> 为什么其他家族（如 ARITH、FP）的保留 funct3 不需要显式拉低 `f3Valid`？因为它们的 `switch` 只列合法值，保留值会让 `vecOp` 保持默认 `vadd`，虽然功能上「无害」，但严格说它们**当前并没有被判非法**——这是一种务实取舍：已实现子操作的家族容错，未实现/部分实现的家族（LUT）才精细判非法。换句话说，关卡 2 目前是「按需开启」的，不是全覆盖。

**关卡 3：保留 width**——`widthIllegal`：

[widthIllegal 判定（src/main/scala/isa/instrDecoder.scala:L227-L230）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L227-L230) — `(f7Width === 3.U) && family ∉ {FP, FMA, CVT}`。funct7[1:0]=11 是保留 width（见 u2-l1 的 VecWidth 表），命中即非法；但 FP/FMA/CVT 这三个家族的 funct7[1:0] 被复用作别的含义（不是 width），所以排除在外。

**关卡 4：保留 dtype**——`dtypeIllegal`：

[dtypeIllegal 判定（src/main/scala/isa/instrDecoder.scala:L248）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L248) — `f7Dtype === 3.U`，即 funct7[6:5]=11（保留 dtype）一律非法。这一关对所有家族生效，没有例外。

**最终汇总**——四路 OR：

[illegal 汇总（src/main/scala/isa/instrDecoder.scala:L262-L267）](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L262-L267) — 四个独立的 `when` 依次把 `illegal := true.B`，等价于一个四输入 OR。因为是组合逻辑，只要任一条件成立，输出立即反映。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手写一个最小译码器测试，用 `NpuAssembler` 造一条**合法 vadd** 和一条**故意填错 funct3 的非法指令**，在仿真里分别 poke 进去，验证 `illegal` 输出与 `family` 字段。

**操作步骤**：

1. 在 `src/test/scala/isa/` 下新建一个临时 spec 文件（例如 `MyDecoderProbe.scala`），内容如下。它直接模仿 `InstrDecoderSpec` 的写法：

   ```scala
   // 示例代码：最小译码器探针，用于本讲实践
   package isa

   import chisel3._
   import chisel3.simulator.EphemeralSimulator._
   import org.scalatest.flatspec.AnyFlatSpec
   import isa.micro_op._

   class MyDecoderProbe extends AnyFlatSpec {
     import NpuAssembler._

     "MyDecoderProbe" should "tell legal vadd from illegal funct3" in {
       simulate(new InstrDecoder) { dut =>

         // —— 合法指令：vadd rd=1, rs1=2, rs2=3, width=VX ——
         val legal = vadd(rd=1, rs1=2, rs2=3, width=VX)
         dut.io.instr.poke((legal.toLong & 0xFFFFFFFFL).U)
         dut.clock.step(0)                       // 组合逻辑，不推进时钟
         assert(!dut.io.illegal.peek().litToBoolean, "vadd 必须合法")
         // family 是 ChiselEnum：用 litValue 间接比较（详见 u9-l1）
         assert(dut.io.decoded.family.peek().litValue == OpFamily.VALU_ARITH.litValue,
           "vadd 的家族应为 VALU_ARITH")
         dut.io.decoded.valu.regCls.expect(0.U)  // VX
         dut.io.decoded.rd.expect(1.U)

         // —— 非法指令：VALU_LUT 家族(opcode=0x13) + 保留 funct3=2 ——
         val illegalWord = encR(0x13, /*funct3=*/2, f7(VX), /*rd=*/0, /*rs1=*/1, /*rs2=*/0)
         dut.io.instr.poke((illegalWord.toLong & 0xFFFFFFFFL).U)
         dut.clock.step(0)
         assert(dut.io.illegal.peek().litToBoolean, "LUT 的保留 funct3=2 必须非法")
         // 此时 family 仍是合法的 VALU_LUT，只是 funct3 非法
         assert(dut.io.decoded.family.peek().litValue == OpFamily.VALU_LUT.litValue,
           "opcode=0x13 仍应译成 VALU_LUT 家族")
       }
     }
   }
   ```

2. 运行你这个临时 spec：
   ```bash
   ./tool/test-specific-spec.sh isa.MyDecoderProbe
   ```
3. 实验成功后**删除**这个临时文件，不要把它留在仓库里（本讲只读源码，禁止修改/新增源码，这是本练习的纪律）。

**需要观察的现象**：

- poke 合法 vadd 后，`illegal` 为 `false`，`family` 译为 `VALU_ARITH`，`regCls=0(VX)`。
- poke 非法字（0x13 + funct3=2）后，`illegal` 翻为 `true`，但 `family` **仍是** `VALU_LUT`——这印证了关卡 1（opcode 合法）和关卡 2（funct3 非法）是**独立**判定的：opcode 合法不代表整条指令合法。
- 两条指令都不需要 `clock.step(n>0)`，`step(0)` 即可读到结果——再次验证这是组合逻辑。

**预期结果**：两条断言全部通过。如果你把非法字里的 funct3 从 2 改成 0（合法的 `VLUT_A`），你会看到 `illegal` 变回 `false`——可以多做几组对照实验加深理解。若无法本地运行，记为「待本地验证」，但请先把上面 `legal` 和 `illegalWord` 两个数的位模式在纸上推开，确认 funct3 确实落在期望位段。

#### 4.3.5 小练习与答案

**练习 1**：构造一条能命中「关卡 3（保留 width）」的非法指令。提示：在 `VALU_ARITH` 家族里把 width 设成保留值。

> **参考答案**：用 `encR(0x10, 0, f7(width=3), 0, 1, 2)`。这里 opcode=0x10（ARITH，合法）、funct3=0（ADD，合法），但 funct7[1:0]=11（保留 width）。由于 ARITH 不在排除集合 {FP, FMA, CVT} 里，`widthIllegal` 为真，整条指令非法。注意：`NpuAssembler.f7` 会对 width 做 `& 3`，所以传 `width=3` 即可得到 funct7[1:0]=11。

**练习 2**：为什么关卡 3 要把 FP/FMA/CVT 三个家族排除？如果不排除，会发生什么误判？

> **参考答案**：这三个家族的 funct7[1:0] 不表示 width——FP/FMA 恒为 VR、CVT 的 funct7[1:0] 是 src 格式码的一部分。如果不排除，一条合法的 `vfadd`（其 funct7[1:0] 可能恰好是 3）会被误判为非法 width，导致正常浮点指令无法执行。所以排除集合精确刻画了「funct7[1:0] 在哪些家族里才真是 width」。

**练习 3**：`dtypeIllegal`（关卡 4）为什么没有任何家族例外，而 `widthIllegal`（关卡 3）有？

> **参考答案**：funct7[6:5] 在**所有**家族里都表示 dtype 类（INT/FP/BF），没有任何家族把这两位挪作他用，所以保留值 11 在任何家族都无意义，可一刀切判非法。这和 width 形成对比：width 位被多个家族复用，必须逐家族豁免。这也提醒我们：funct7 各子段的「通用性」并不相同，dtype 比 width 更「专一」。

## 5. 综合实践

把本讲三个模块（译码流程、输出 Bundle、非法判定）串起来，完成下面这个综合任务：

**任务**：用 `NpuAssembler` 构造 4 条指令，每条各命中**一道不同的非法关卡**（保留 opcode、LUT 保留 funct3、CVT src==dst、保留 width），外加 1 条完全合法的 `vsub`。对每一条：

1. 先在纸上写出它的 32 位十六进制字，并标注哪个比特段触发了哪道关卡（或为何合法）。
2. 写一个最小 spec（参考 4.3.4 的模板），把这 5 个字依次 poke 进 `InstrDecoder`，断言 `illegal` 与 `family` 符合预期。
3. 运行 `./tool/test-specific-spec.sh <你的Spec名>`，确认全绿。

**提示与检查点**：

- 保留 opcode：直接 `poke(0x7F.U)`，期望 `family` 译为某个无效值且 `illegal=true`。注意此时 `family.peek().litValue` 应**不在** `OpFamily` 任何成员的 litValue 集合里。
- LUT 保留 funct3：`encR(0x13, 6, f7(VX), 0, 1, 0)`，期望 `family=VALU_LUT`、`illegal=true`。
- CVT src==dst：`encR(0x14, 3, f7Cvt(srcFmt=3), 0, 0, 0)`（dst=src=F32），期望 `family=VALU_CVT`、`illegal=true`。
- 保留 width：`encR(0x10, 0, f7(width=3), 0, 1, 2)`，期望 `family=VALU_ARITH`、`illegal=true`。
- 合法 vsub：`vsub(rd=1, rs1=2, rs2=3)`，期望 `family=VALU_ARITH`、`illegal=false`、`regCls=0`。

完成这个任务意味着你已经能：读懂字段切片（4.1）、看懂输出包（4.2）、并自如地构造与判定非法指令（4.3）——这就是译码器的全部核心能力。完成后记得删除临时 spec，保持源码只读。

## 6. 本讲小结

- `InstrDecoder` 是**纯组合逻辑**模块，输入 32 位 `instr`、同拍输出 `DecodedMicroOp` 与 `illegal`，无寄存器、无流水级。
- 译码是「切字段 → `OpFamily.safe()` 定家族 → 两层 `switch` 选 VecOp → 解 funct7 属性 → 驱动输出」的流水，`vecOp` 默认 `vadd` 作为故障安全兜底。
- `DecodedMicroOp` 是「包中包」：顶层有 family/rd/rs1/rs2/mma_*/mem_width，嵌套的 `valu`（`NCoreVALUBundle`）整块送给 VALU；`regCls` 是 width 的改名，为避开 `chisel3.Width` 冲突。
- 非法判定是四路 OR：保留 opcode（`!familyOK`）、保留 funct3/CVT src==dst/LUT 保留值（`!f3Valid`）、保留 width（`widthIllegal`，FP/FMA/CVT 豁免）、保留 dtype（`dtypeIllegal`，无豁免）。
- 关卡之间相互独立：opcode 合法不代表整条指令合法（如 LUT 的保留 funct3）；译码器只置 `illegal` 标志，真正的写回抑制发生在后端 `SimpleBackend`（见 u6-l2）。
- 测试中 ChiselEnum 字段（family/op/dtype）不便直接 `expect`，用 `.peek().litValue` 间接比较，正确性另由 VALU 功能测试兜底（详见 u9-l1）。

## 7. 下一步学习建议

至此你已完成 ISA 子系统（u2）的全部内容：格式（u2-l1）→ 家族与属性（u2-l2）→ 装载存储与数据类型（u2-l3）→ 汇编器（u2-l4）→ 译码器（本讲）。译码器输出的 `DecodedMicroOp` 已经是执行单元能直接消费的形态，下一单元 u3 正式进入「控制与存储基础」：

- **u3-l1 微操作与控制 Bundle**：深入 `NCoreVALUBundle`、`NCoreMMALUCtrlBundle` 与 `VecOp` 枚举，看本讲输出的 `valu` 包是如何被 VALU/MMALU 消费的。
- **u3-l2 多宽度寄存器堆**：本讲频繁出现的 `regCls`（VX/VE/VR）会决定 `rd/rs1/rs2` 实际索引哪类寄存器，u3-l2 讲清这块物理存储的别名机制。

建议在进入 u3 前，先动手把本讲「综合实践」做完——能自如构造合法/非法指令字，是后续理解后端分发与写回时序（u6）的前提。如果想看译码器在真实后端里如何被接线、`illegal` 如何抑制写回，可以直接跳到 u6-l2，但需要先补 u3 与 u4/u5 的计算单元基础。
