# 指令分发与写回时序

## 1. 本讲目标

承接 [u6-l1](u6-l1-ncore-backend-wiring.md) 的总体连线，本讲聚焦 `NCoreBackend` 内部最容易被忽略、却最容易写错的一段逻辑：**一条指令被译码之后，后端如何决定「要不要写回、写到哪类寄存器、在哪一拍写回」**。

学完本讲，你应当能够：

- 说出 `isVALU` 判定覆盖的 9 个 VALU 家族，以及 MMA 走的独立分支。
- 读懂 VX/VE/VR 三类写回端口的使能表达式，理解 `regCls` 守卫的作用。
- 解释 `isNarrowCvtOut` / `isWideCvtOut` / `isReduceToVR` / `isSetLut` 四个辅助函数为何必须存在。
- 说明 VALU 的 1 拍输出寄存器如何迫使后端把指令「保持 2 拍」才能完成写回。

## 2. 前置知识

本讲需要你已经掌握以下内容：

- **u6-l1**：`NCoreBackend` 把译码器（`InstrDecoder`）、多宽度寄存器堆（`MultiWidthRegisterBlock`）、MMALU、VALU 四者连线；寄存器堆只有一块物理存储，VX/VE/VR 是它的三种「别名视图」，各自带独立的读写端口，且写端口是稀缺资源。
- **u2-l5 / u3-l1**：译码器输出 `DecodedMicroOp`，其中嵌套的 `NCoreVALUBundle` 有一个 `regCls` 字段（`0=VX`，`1=VE`，`2=VR`），它取自指令 `funct7[1:0]` 的宽度选择位。`VecOp` 是把 `(opcode, funct3)` 折叠成的一维内部操作码，VALU 只在它上面分发。

本讲反复用到两个关键事实，请先记在心里：

1. **VALU 的三个输出 `out_vx/out_ve/out_vr` 都经过 `RegNext` 寄存**，比输入晚整整 1 拍。
2. **译码器对 CVT 家族会把 `regCls` 一律置为 VR**（详见 4.4 节），这正是若干「越级写回」问题的根源。

几个术语约定：

- **写回（write-back）**：把执行单元的结果写进寄存器堆。
- **守卫（guard）**：用一个布尔表达式门控某个写端口是否使能（enable）。
- **越级写回**：结果真正落点的寄存器类，与指令 `regCls` 字段不一致，必须用辅助函数修正。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| `src/main/scala/backend/SimpleBackend.scala` | `NCoreBackend` 主体；本讲**全部**分发与写回逻辑都在此文件 |
| `src/main/scala/isa/instrDecoder.scala` | 译码器，产出 `family` / `VecOp` / `regCls` |
| `src/main/scala/isa/micro_op/VALUMicroCode.scala` | `VecOp` 枚举与 `NCoreVALUBundle`（含 `regCls` 字段） |
| `src/main/scala/isa/instSetArch.scala` | `OpFamily` 枚举（13 个家族） |
| `src/main/scala/alu/vec/vec.scala` | VALU 的 `RegNext` 输出（1 拍延迟的来源） |
| `docs/implementations/NeuralCore.md` | 写回时序的官方说明 |
| `src/test/scala/backend/NCoreBackendQuantSpec.scala` | `issue()` 辅助函数，演示 2 拍保持 |

## 4. 核心概念与源码讲解

### 4.1 分发判定：isVALU 的 9 个家族与 MMA 分支

#### 4.1.1 概念说明

译码器把 32 位指令字翻译成 `DecodedMicroOp` 之后，后端面临的第一个问题是：**这条指令归谁执行？**

在 chisel-npu 里，执行单元只有两个——MMALU（矩阵乘）与 VALU（向量运算）。因此分发不是一张庞大的路由表，而是一个干净的「二选一」：

- `family === MMA` → 走 MMALU 写回（VR 写端口 1）。
- `family` 落在 9 个 VALU 家族之一 → 走 VALU 写回（VX/VE/VR 写端口 0）。
- 其余（NOP / LD / ST）→ 不写回。

为什么 VALU 要拆成 9 个家族？因为向量运算按功能细分：算术、逻辑、归约、LUT、CVT、广播、浮点、浮点 FMA、MOV。它们**共享同一套写回端口**，所以后端把 9 个家族「并联」成一个标量 `isVALU`，让 `when(isVALU)` 一个块就能覆盖全部 VALU 指令的写回。

#### 4.1.2 核心流程

```
dec = 译码器输出（DecodedMicroOp）

if (dec.family === MMA):
    仅启用 MMALU 写回  → VR 写端口 1
elif (dec.family ∈ 9 个 VALU 家族):     # 即 isVALU
    启用 VALU 写回      → VX/VE/VR 写端口 0（由 regCls + 修正函数选择）
else:
    所有写端口保持「默认关闭」           # NOP / LD / ST 不写回
```

> 注意：写回是否发生**完全由 `family` 决定**，后端写回逻辑里**不重新检查 `illegal` 标志**。u2-l5 已说明 `illegal` 只是译码器的一个独立输出；保留 opcode 会落到不执行任何写回的 family，从而天然不会造成有意义的写回。

#### 4.1.3 源码精读

`isVALU` 是 9 个家族的按位或，定义在 [SimpleBackend.scala:203-212](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L203-L212)：这 9 个家族分别是 `VALU_ARITH / VALU_LOGIC / VALU_REDUCE / VALU_LUT / VALU_CVT / VALU_BCAST / VALU_FP / VALU_FP_FMA / VALU_MOV`。对应的家族编码见 [instSetArch.scala:32-46](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instSetArch.scala#L32-L46)（opcode `0x10`–`0x18`）。

MMA 分支独立成块，见 [SimpleBackend.scala:176-182](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L176-L182)：当 `family === MMA` 时，开启 VR 写端口 1，把 MMALU 的 INT32 累加结果**无截断直写** VR。MMA 的控制信号（`keep`）来自译码，见 [SimpleBackend.scala:169-172](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L169-L172)。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认「9 个 VALU 家族」与「MMA」确实构成了完整的分发覆盖。
2. **操作步骤**：打开 `OpFamily` 枚举，数一共有多少个家族；再数 `isVALU` 表达式里的家族数。
3. **观察**：13 个家族中，9 个进 `isVALU`，1 个是 MMA，剩下 NOP/LD/ST 三个不写回。
4. **预期结果**：分发只有「MMA / VALU / 不写回」三条出路，没有任何第四类执行单元。

#### 4.1.5 小练习与答案

- **练习**：如果未来新增一个 VALU 子家族（例如 `VALU_COMPARE`，opcode `0x19`），后端最少要改哪一处？
- **答案**：必须在 `isVALU` 的按位或里加上 `dec.family === OpFamily.VALU_COMPARE`，否则新家族指令会被分到「不写回」分支，结果丢失。这正是把 9 个家族显式列举（而非用范围判断）的原因——它既是路由，也是清单。

### 4.2 写回端口的使能守卫与默认关闭

#### 4.2.1 概念说明

确定了「归 VALU 执行」之后，第二个问题是：**结果写回哪类寄存器？**

答案表面很简单——看 `regCls`：`0` 写 VX、`1` 写 VE、`2` 写 VR。但 chisel-npu 用了一个非常重要的实现技巧来保证安全：**先把所有写端口默认关闭，再用 `when` 条件性地打开。**

为什么这样做？因为 Chisel 的语义是「**最后连接胜出（last connection wins）**」。如果在某种指令组合下某个写端口既没人显式关闭、也没人显式打开，它就会悬空（被推断成保持上一次的值），可能造成多驱动或写错。所以后端的范式是：

```scala
rf.io.vx_w_en := false.B        // 默认全关
// ...
when (条件) { rf.io.vx_w_en(0) := true.B }   // 再按条件打开
```

这样任何未被命中的路径都安全地落到「关闭」。

#### 4.2.2 核心流程

VX/VE/VR 三类写端口（port 0，归 VALU 用）的使能逻辑可以总结为下表（最外层都套在 `when(isVALU)` 里）：

| 写端口 | 使能表达式（简化） |
|:---|:---|
| VX port 0 | `(regCls===VX \|\| isNarrowCvtOut) && !isReduceToVR && !isSetLut` |
| VE port 0 | `regCls===VE` |
| VR port 0 | `(regCls===VR \|\| isWideCvtOut \|\| isReduceToVR) && !isSetLut` |
| VR port 1 | `family===MMA`（MMALU 专用，不在 `isVALU` 块内） |

其中 `regCls` 是「常规指令」的主开关；三个修正函数（4.4 节详解）负责处理 `regCls` 与真实落点不一致的「越级」指令。

#### 4.2.3 源码精读

默认关闭所有写端口的代码在 [SimpleBackend.scala:139-148](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L139-L148)：`vx_w_en`/`ve_w_en`/`vr_w_en` 全部填 `false.B`，地址填 0，数据填 0。

宽度比较用的常量定义在 [SimpleBackend.scala:48-50](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L48-L50)——一个私有 `object W`，把 `VX/VE/VR` 写成 `UInt(2.W)` 字面量。注释点明了原因：**避免在比较 `dec.valu.regCls` 时还要 import `VecWidth` 这个 ChiselEnum**，直接用 UInt 比较更简单。

三类写回的 `when(isVALU)` 块见 [SimpleBackend.scala:214-240](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L214-L240)，其中：

- VX 写回（[SimpleBackend.scala:222-226](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L222-L226)）。
- VE 写回（[SimpleBackend.scala:228-231](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L228-L231)）。
- VR 写回（[SimpleBackend.scala:233-239](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L233-L239)）。

注释里也点明了每个修正项的用意，例如「`isReduceToVR` 会误开 vx_w_en，必须用 `!isReduceToVR` 抑制」。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「默认关闭 + 条件打开」范式确实覆盖了所有写端口。
2. **操作步骤**：在 `SimpleBackend.scala` 里数 `:= false.B` 出现的写端口数量，再数 `when` 块里被改成 `true.B` 的端口数量。
3. **观察**：VX/VE/VR 各自的写端口都有且仅有「一处默认关闭」与「一处（或两处）条件打开」。
4. **预期结果**：没有任何写端口处于「可能悬空」的状态。

#### 4.2.5 小练习与答案

- **练习**：为什么 VE 写回的守卫只有 `regCls===VE`，而不像 VX 那样带一串修正？
- **答案**：因为「越级」的指令（窄 CVT、归约、LUT）要么落 VX、要么落 VR，没有任何一条会把结果写到 VE。VE 是「最普通」的宽度，没有需要修正的特例。

### 4.3 写回时序：VALU 的 1 拍寄存器与 2 拍保持

#### 4.3.1 概念说明

确定了「写哪类寄存器」之后，最后一个问题是：**在哪一拍写？**

这看起来是组合逻辑该自然解决的问题——`when(isVALU)` 一旦为真就写。但 VALU 的输出端**有一层 `RegNext` 寄存器**（详见 u5-l1）：指令在第 0 拍被译码并送进 VALU，VALU 的组合结果在第 0 拍算出，但要到**第 1 拍**才出现在 `out_vx/out_ve/out_vr` 上。

而后端的写回使能 `isVALU` 是**纯组合**的——它跟随**当前拍**的指令字。这就产生了一个错位：

- 第 0 拍：`instr = vadd`，`isVALU=true`，但 `out_vx` 还是**上一条指令**残留的结果。
- 第 1 拍：如果指令已经换成下一条，`isVALU` 可能变 false，而此时 `out_vx` 才是 vadd 的**正确结果**——却没人去写它。

解决办法：**把指令保持 2 拍**。第 1 拍的 `isVALU` 仍然为真，于是第 1 拍（在拍末时钟沿）把正确的 vadd 结果写进寄存器堆。第 0 拍那次「写残留值」虽然也发生了，但因为写的是**同一个目的地址**，会被第 1 拍的正确值覆盖，无害。

> 这是一种简化的时序协议：它假设前端按序发射、且每条 VALU 指令独占 2 拍。生产级前端应当改用 1 拍流水线暂停（stall）或前递（forwarding），文档 [NeuralCore.md:113-116](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md#L113-L116) 已明确指出这一点。

#### 4.3.2 核心流程

```
拍 0 (issue):  instr=vadd   → isVALU=true,  out_vx=残留值 → 写入(同地址,稍后被覆盖)
拍 1 (hold):   instr=vadd   → isVALU=true,  out_vx=正确值 → 写入 ✓(最终生效)
拍 2 (drain):  instr=nop    → isVALU=false                       → 不写
```

用文字总结时序契约：**issue 1 拍 + hold 1 拍 = 写回在第 1 拍末沿生效**，共需把指令保持 2 拍。MMALU 则不同——它的流水线长达 `3K-2` 拍，写回发生在更晚的 `clct` 时刻（u4-l5），与 VALU 的 2 拍互不干扰，二者可以重叠执行。

#### 4.3.3 源码精读

VALU 输出寄存的来源在 [vec.scala:453-456](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L453-L456)：`io.out_vx := RegNext(rawVX)` 等三行。这正是 1 拍延迟的物理来源。

后端把译码结果保持 2 拍的契约，体现在测试辅助函数 `issue()` 里。[NCoreBackendQuantSpec.scala:54-59](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L54-L59) 的实现是：

```scala
def issue(dut, instr, cycles = 2): Unit = {
  dut.io.instr.poke(instr.U)
  for (_ <- 0 until cycles) dut.clock.step()   // 保持 2 拍
  dut.io.instr.poke(nop.U)
  dut.clock.step()                              // 1 拍排空
}
```

官方时序图见 [NeuralCore.md:92-111](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md#L92-L111)（Cycle 0 fetch → Cycle 1 compute+latch → Cycle 2 write-back）。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证「保持 2 拍」的必要性。
2. **操作步骤**：阅读 `runExecuteVadd`（[NCoreBackendQuantSpec.scala:92-109](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendQuantSpec.scala#L92-L109)），它先用 `extWrite` 装入两个 VX 向量，再 `issue(vadd(...))`，最后读回 `vx_out_addr` 比对。
3. **思考**：如果把 `issue` 的 `cycles` 改成 1，读回的结果会是什么？
4. **预期结果（待本地验证）**：改成 1 拍时，写回的是「上一拍残留值」，结果会出错；保持 2 拍才正确。这正是该默认值的理由。

#### 4.3.5 小练习与答案

- **练习**：`vfma`（浮点融合乘加）在 VALU 内部要串接两次浮点运算，u5-l3 说它按 2 拍计算。那么用 `issue(cycles=2)` 驱动 `vfma` 会出什么问题？
- **答案**：`vfma` 的结果比普通 VALU 指令再多 1 拍，2 拍保持不足以让正确结果落到输出寄存器并写回，需要在发射后多等 1 拍（或用更大的 `cycles`）。这是当前简化协议对 FMA 这类长延迟指令的一个已知粗糙之处。

### 4.4 越级写回修正：CVT / 归约 / LUT 四个辅助函数

#### 4.4.1 概念说明

如果每条指令的「结果落点」都和它的 `regCls` 一致，那么 4.2 节的 `regCls` 守卫就足够了。但 chisel-npu 里有三类指令会**打破这种一致性**——它们的 `regCls` 字段表达的并不是结果落点：

1. **窄输出 CVT**（如 `vcvt_f32_s8`，FP32→INT8）：结果是 INT8，应当落 VX；但译码器把所有 CVT 的 `regCls` 都强制设成了 VR。
2. **水平归约**（如 `vsum.vx`）：结果恒为 VR 宽度并广播；但 `regCls` 编码的是**输入**宽度（`vsum.vx` 的 `regCls=VX`），不是输出宽度。
3. **`vsetlut`**：它根本不写寄存器堆，只写 VALU 内部的 LUT bank；但译码器为了把 `in_a_vr` 路由对，强行把它的 `regCls` 设成了 VR。

这三类就是「越级写回」。后端用四个辅助函数把它们逐一修正：`isNarrowCvtOut`、`isWideCvtOut`、`isReduceToVR`、`isSetLut`。

> 设计要点：VALU 一侧也对这些 op 做了同样的特判（见 [vec.scala:425-450](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L425-L450) 的宽度门控赋值），驱动正确的 `rawVX/rawVR`。**两侧必须成对一致**：VALU 把结果送到对的输出端口，后端把对的写端口打开。本讲只讲后端这一侧。

#### 4.4.2 核心流程

四个辅助函数的职责：

| 函数 | 覆盖的 VecOp | 作用 |
|:---|:---|:---|
| `isNarrowCvtOut` | `vcvt_s8_s32`、`vcvt_f32_s8` | 结果是 INT8 → **补开 VX 写端口**（`regCls===VX` 抓不到它们） |
| `isWideCvtOut` | `vcvt_s8_f32` 等一串宽输出 CVT | 结果是宽（VR）→ 显式纳入 VR 写（与 `regCls===VR` 形成双保险） |
| `isReduceToVR` | `vsum`、`vrmax`、`vrmin` | 结果恒落 VR → **补开 VR 写端口**，同时**抑制 VX 写** |
| `isSetLut` | `vsetlut` | 不写 RF → **抑制全部 RF 写端口** |

把 4.2 节的使能表达式代入真实指令，得到下面这张「以代码为准」的落点表（✓=该端口被使能）：

| 指令 | 译码后 regCls | VX port 0 | VE port 0 | VR port 0 | 说明 |
|:---|:---:|:---:|:---:|:---:|:---|
| `vadd.vx` | VX | ✓ | | | 常规，regCls 即落点 |
| `vadd.ve` | VE | | ✓ | | 常规 |
| `vfadd`（FP） | VR | | | ✓ | 常规 |
| `vcvt_f32_s8`（窄） | VR（强制） | ✓ | | ✓ | `isNarrowCvtOut` 补开 VX；同时因 regCls=VR 也开 VR |
| `vcvt_s8_f32`（宽） | VR（强制） | | | ✓ | regCls=VR 命中 |
| `vsum.vx`（归约） | VX | ✗ | | ✓ | `isReduceToVR` 抑制 VX、补开 VR |
| `vsetlut` | VR（强制） | ✗ | | ✗ | `isSetLut` 全部抑制 |
| `mma` | — | | | | ✓（VR port 1） |

> 关于 `vcvt_f32_s8` 那一行：因为译码器把所有 CVT 的 `regCls` 强制为 VR，`regCls===VR` 这一条件本身就会开 VR 端口；`isNarrowCvtOut` 的**关键贡献**是把 VX 端口也补上——否则这个 INT8 结果将无处可写。表中同时打勾的两处是代码的真实行为，不是笔误（以 [SimpleBackend.scala:222-239](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L222-L239) 为准）。

#### 4.4.3 源码精读

**根因一：CVT 的 `regCls` 被强制为 VR。** 见 [instrDecoder.scala:212-215](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L212-L215)：`when (family === OpFamily.VALU_CVT) { width := 2.U }`。注释直言「conservative; backend picks correct src/dst via VecOp」——译码层图省事，把分辨源/目宽度的责任推给了后端。

**`isNarrowCvtOut`** 见 [SimpleBackend.scala:245-249](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L245-L249)：列出两个窄输出 CVT，注释标注「FP32 → INT8 narrow」。

**`isWideCvtOut`** 见 [SimpleBackend.scala:251-262](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L251-L262)：列出全部宽输出 CVT。

**根因二：归约指令在 `funct7[1:0]` 编码的是输入宽度。** 见译码器的归约分支 [instrDecoder.scala:116-125](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L116-L125)，以及 `isReduceToVR` 的详细注释 [SimpleBackend.scala:270-285](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L270-L285)。注释把因果讲得很清楚：`vsum.vx` 用 `regCls=VX` 只是为了让 VALU 选 VX 归约树，但输出永远是 VR 宽度。

**根因三：`vsetlut` 强制 `regCls=VR` 只为路由输入。** 见 [instrDecoder.scala:221-225](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L221-L225)；`isSetLut` 见 [SimpleBackend.scala:264-268](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L264-L268)，注释说明它「只写 VALU 内部 bank，必须抑制全部 RF 写端口」。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：确认 VALU 一侧与后端一侧的特判是一一对应的。
2. **操作步骤**：对照 [vec.scala:425-450](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/alu/vec/vec.scala#L425-L450) 的 `rawVX`/`rawVR` 门控赋值，与后端四个辅助函数逐项比对。
3. **观察**：`rawVX` 里出现的 `vcvt_f32_s8` 对应后端的 `isNarrowCvtOut`；`rawVR` 里的 `vsum/vrmax/vrmin` 对应 `isReduceToVR`。
4. **预期结果**：两侧覆盖的 VecOp 集合一致——任何「越级」op 都在两侧被同等地特殊处理，不会出现「VALU 送了 VX、后端却没开 VX 写」的错配。

#### 4.4.5 小练习与答案

- **练习 1**：`vsetlut` 的 `regCls` 被强制为 VR。如果后端**没有** `isSetLut` 这个守卫，会发生什么？
- **答案**：`regCls===VR` 会让 `vr_w_en(0)` 为真，于是 `vsetlut` 会把 `out_vr`（此时是上一条指令的残留值）误写进 `vr_out_addr` 指向的 VR 寄存器，破坏寄存器堆状态。`isSetLut` 的 `&& !isSetLut(...)` 正是用来堵这个洞。
- **练习 2**：`vrmax.vx`（输入 VX，结果广播到 VR）的 `regCls=VX`。如果不加 `isReduceToVR`，VX 写端口会不会被误开？
- **答案**：会。`regCls===VX` 为真，`vx_w_en(0)` 会触发，把广播结果（VR 宽度）的低字节误写进 VX，覆盖目的 VX 寄存器。所以 VX 守卫里的 `&& !isReduceToVR(...)` 和 VR 守卫里的 `|| isReduceToVR(...)` 缺一不可。

## 5. 综合实践

**任务**：追踪一条 `vcvt_f32_s8`（窄输出 CVT）和一条 `vsum.vx`（水平归约）在 `NCoreBackend` 中的完整写回路径，解释它们为什么必须分别用 `isNarrowCvtOut` 与 `isReduceToVR` 特殊处理，否则会写错寄存器类。

**操作步骤（源码追踪型）**：

1. **构造指令字**。在 `NpuAssembler` 里：
   - `vcvt_f32_s8(rd, rs1)` = `vcvt(rd, rs1, F32, S8, sat=false)`，编码成 opcode `0x14`、funct3=`F32`、funct7 低 3 位=`S8`。参见 [NpuAssembler.scala:169](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L169)。
   - `vsum(rd, rs1, width=VX)` = opcode `0x12`、funct3=0、funct7[1:0]=`VX`。参见 [NpuAssembler.scala:129](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/NpuAssembler.scala#L129)。

2. **走译码器**。确认两条指令译码后：
   - `vcvt_f32_s8`：`family=VALU_CVT`，`VecOp=vcvt_f32_s8`，`regCls=VR`（被 [instrDecoder.scala:212-215](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/isa/instrDecoder.scala#L212-L215) 强制）。
   - `vsum.vx`：`family=VALU_REDUCE`，`VecOp=vsum`，`regCls=VX`（输入宽度）。

3. **走后端写回**。代入 [SimpleBackend.scala:222-239](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/main/scala/backend/SimpleBackend.scala#L222-L239) 的使能表达式，填出两条指令各自命中哪些写端口（应与 4.4.2 节落点表一致）。

4. **回答「为什么必须特殊处理」**：
   - **`vcvt_f32_s8`**：若没有 `isNarrowCvtOut`，VX 守卫只剩 `regCls===VX`；而它的 `regCls=VR`，于是 VX 写端口**永远不开**，INT8 结果丢失。`isNarrowCvtOut` 把它 OR 进 VX 守卫，才让结果落到 VX。
   - **`vsum.vx`**：若没有 `isReduceToVR`，一方面 `regCls===VX` 会**误开 VX 写端口**（把广播的 VR 结果低字节写进 VX），另一方面 `regCls===VR` 为假、VR 写端口**不开**，归约结果无处可去。`isReduceToVR` 同时完成「抑制 VX、补开 VR」两端修正。

5. **可选的仿真验证（待本地验证，需 Docker 容器）**：参考 [NCoreBackendGemmSoftmaxSpec.scala:321-325](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/src/test/scala/backend/NCoreBackendGemmSoftmaxSpec.scala#L321-L325) 的 Phase 5（`vsum VX[2] → VR[4]`），它正是一条 `vsum.vx` 落到 VR 的真实用例。可在容器内 `sbt "testOnly backend.NCoreBackendGemmSoftmaxSpec"` 跑通，观察归约结果确实写进了 VR（用 `peekVR0(dut, 4)` 读回）。

**预期结果**：你能用一句话说清——「`regCls` 在这三类指令上撒了谎，辅助函数负责把谎圆回来，让结果落到正确的寄存器类」。

## 6. 本讲小结

- 分发是干净的二选一：`family===MMA` 走 MMALU，9 个 VALU 家族由 `isVALU` 并联走 VALU，其余不写回。
- 写回端口采用「默认全关 + 条件打开」范式，靠 Chisel「最后连接胜出」保证未命中路径安全。
- `regCls`（`VX/VE/VR`）是常规指令的主开关，用私有 `object W` 的 UInt 字面量比较，避免 import ChiselEnum。
- CVT/归约/LUT 三类指令的 `regCls` 与真实落点不一致（越级），由 `isNarrowCvtOut`/`isWideCvtOut`/`isReduceToVR`/`isSetLut` 四个辅助函数修正；VALU 一侧有对等的宽度门控，两侧必须成对一致。
- VALU 输出经 `RegNext` 滞后 1 拍，迫使后端把指令保持 2 拍才能让正确结果写回；MMALU 的长流水线（`3K-2` 拍）与 VALU 互不干扰，可重叠。

## 7. 下一步学习建议

- 想看「越级写回」在真实算法里的串联效果，进入 [u7-l1（后量化流水线）](u7-l1-quantization-pipeline.md)：`MMA → vcvt → vfma → vcvt` 链正是窄/宽 CVT 与累加器直写 VR 的综合舞台。
- 想深入「辅助函数」对应的数据通路细节，回顾 [u5-l3（浮点）](u5-l3-floating-point.md) 与 [u5-l4（归约与广播）](u5-l4-reduce-and-broadcast.md)，理解 VALU 一侧为何要那样门控 `rawVX/rawVR`。
- 想了解 2 拍保持协议在生产级前端的改进方向，可阅读 [NeuralCore.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/NeuralCore.md) 中关于 stall/forwarding 的说明，并对照 `issue()` 辅助函数思考前端调度器应承担的职责。
