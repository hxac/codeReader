# 乘法 MLU 与除法 DVU 单元

## 1. 本讲目标

本讲聚焦 CoralNPU 标量核中两个「重计算」执行单元：乘法单元 **MLU（Multiply Unit）** 与除法单元 **DVU（Divide Unit）**。学完后你应当能够：

1. 说清楚为什么 CoralNPU 全核只有一个 MLU、一个 DVU，以及这种「单实例」设计如何反过来约束派发。
2. 读懂 MLU 的三级流水线（派发 → 计算 → 写回），并用符号扩展（sign extension）解释 MUL / MULH / MULHSU / MULHU 四条指令的差异。
3. 理解 DVU 为什么是「可变延迟」的——它用一位一拍的迭代（移位减法）做除法，并用前导零计数（CLZ）实现提前结束。
4. 解释 DVU 如何通过 `req.ready` 向派发单元施加反压（backpressure），以及它和 MLU 如何共享一个寄存器堆写口。
5. 对比 `Dvu.scala` 与 `common/IDiv.scala` 两种除法器实现的设计取舍。

## 2. 前置知识

阅读本讲前，建议你已经掌握 u5-l1 的内容，至少理解下面几个概念：

- **执行单元（execution unit）**：标量核把指令按类型送到不同的硬件单元执行。ALU 做加减逻辑，BRU 做分支跳转，**MLU 做乘法，DVU 做除法/取余**，LSU 做访存。每个单元都遵循 ready-valid 握手契约。
- **指令 lane（指令槽）**：CoralNPU 每周期最多派发 4 条标量指令，分别走 4 个 lane。`instructionLanes = 4`（见 [Parameters.scala:L73](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L73)）。ALU/BRU 是「每 lane 各一个」，但 **MLU/DVU 是全核唯一实例**——这是本讲的核心约束。
- **ready-valid 握手**：`valid` 表示「我有数据/命令给你」，`ready` 表示「我现在能收」。只有双方同时为真，这一拍才完成一次传递（称为 fire）。
- **RISC-V M 扩展**：整数乘除法指令集。本讲涉及的 8 条指令是 `mul / mulh / mulhsu / mulhu` 与 `div / divu / rem / remu`。
- **恢复余数除法（restoring division）**：手算除法的硬件版——把余数左移一位、拉入被除数的一位，试着减去除数；够减则商位为 1 并保留差，不够减则商位为 0 并恢复。DVU 用的就是这个算法。

> 名词速查：**Arbiter（仲裁器）** 在多个请求者中按优先级选一个；**反压（backpressure）** 指下游用 `ready=0` 让上游停下来等；**CLZ（Count Leading Zeros，前导零计数）** 统计一个数二进制高位连续 0 的个数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [doc/microarch/mlu.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/mlu.md) | MLU 的官方微架构说明：接口信号表与三级流水线描述、波形图。本讲的「权威定义」来源。 |
| [hdl/chisel/src/coralnpu/scalar/Mlu.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala) | MLU 的 Chisel 实现：输入仲裁、三级流水、符号扩展、乘积位选。 |
| [hdl/chisel/src/coralnpu/scalar/Dvu.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala) | DVU 的 Chisel 实现：可变延迟迭代除法、提前结束、反压。标量核实际用于整数除法的单元。 |
| [hdl/chisel/src/coralnpu/scalar/MluTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/MluTest.scala) | MLU 的 Chisel 仿真测试，给出乘法时序的金标准（本讲用来验证延迟结论）。 |
| [hdl/chisel/src/common/IDiv.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/IDiv.scala) | `common` 包里的另一种除法器：每周期算 4 位、固定 8 周期，注释说「准备与 fdiv（浮点除法）融合」。作为对比阅读。 |
| [hdl/chisel/src/coralnpu/scalar/SCore.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala) | 标量核顶层，把唯一的 MLU/DVU 实例接到派发器与寄存器堆上。证明「单实例 + 槽位约束」的连线事实。 |
| [hdl/chisel/src/coralnpu/scalar/Decode.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala) | 译码端把 `mul/div/rem` 译成 MLU/DVU 命令，并把它们纳入冒险检测。 |

## 4. 核心概念与源码讲解

### 4.1 MLU 的定位：为什么全核只有一个乘法器

#### 4.1.1 概念说明

CoralNPU 每周期最多派发 4 条指令，但**乘法器只有一个**。这不是偷工减料，而是典型的面积/功耗与性能的取舍：一个 32×32 的乘法器在硅片上代价不小，而程序里相邻两条乘法指令的频率通常远低于加减逻辑。于是设计者选择「单实例 + 仲裁」——任何 lane 的乘法指令都能用这个乘法器，但**每个周期只服务一条**。

这个决定带来两个直接后果，贯穿本讲：

1. **派发端有仲裁**：MLU 内部用 Arbiter 在 4 个 lane 的请求里挑一个（低编号优先），同周期其余乘法指令要排队等。
2. **写回口要共享**：MLU 和 DVU 的结果都写回寄存器堆，但只分配了**一个**写口，二者再经一次仲裁。

mlu.md 的接口表把这件事说得很直白：「The single MLU in the CoralNPU core can service instructions from any of the four instruction lanes, but only one command is dispatched in any cycle.」（见 [mlu.md:L8-L12](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/mlu.md#L8-L12)）。

#### 4.1.2 核心流程

从「核顶层视角」看一条乘法指令的生命周期：

```text
4 个 lane 的译码输出
   │  io.mlu(i).valid / bits(addr, op)
   ▼
┌─────────┐  每 lane 一路请求（共 4 路）
│  MLU    │── Arbiter 选 1 路进入三级流水 ──► 结果 io.rd
└─────────┘
   ▲
   │ rs1/rs2：由派发器读地址驱动寄存器堆，读出 4 lane 的数据
```

- **命令来源**：译码器为每个 lane 产生一路 `MluCmd(addr, op)`；MLU 用 Arbiter 收下第一个有效的（[Mlu.scala:L67-L68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L67-L68)）。
- **操作数来源**：rs1/rs2 由派发器把读地址送给寄存器堆，寄存器堆按 lane 返回数据；MLU 用仲裁时记下的 `sel` 选出对应 lane 的那一对（[Mlu.scala:L84-L85](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L84-L85)）。
- **结果去向**：MLU 的 `io.rd` 与 DVU 的 `io.rd` 一起进一个 Arbiter，抢同一个寄存器堆写口（[SCore.scala:L403-L414](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L403-L414)）。

#### 4.1.3 源码精读

**唯一实例**——标量核顶层只 new 了一次：

[SCore.scala:L96-L97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L96-L97) —— 声明全核唯一的 `mlu` 与 `dvu`：

```scala
val mlu = Mlu(p)
val dvu = Dvu(p)
```

**MLU 的对外接口**——`req` 是一个长度为 `instructionLanes`（4）的 Vec，每 lane 一路；而 `rd` 只有一个写口：

[Mlu.scala:L55-L64](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L55-L64) —— 接口声明，注意 req/rs1/rs2 都是 Vec，rd 是单个 Decoupled：

```scala
val io = IO(new Bundle {
  // Decode cycle.
  val req = Vec(p.instructionLanes, Flipped(Decoupled(new MluCmd(p))))
  // Execute cycle.
  val rs1 = Vec(p.instructionLanes, Flipped(new RegfileReadDataIO(p)))
  val rs2 = Vec(p.instructionLanes, Flipped(new RegfileReadDataIO(p)))
  val rd  = Decoupled(Flipped(new RegfileWriteDataIO(p)))
})
```

**派发端的命令产生**——译码器把 `mul/mulh/mulhsu/mulhu` 译成对应的 `MluOp`，每个 lane 一路输出：

[Decode.scala:L609-L618](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L609-L618) —— 用 `SafeMuxUpTo1H` 把指令的布尔位翻成 `MluOp` 枚举：

```scala
val mlu = SafeMuxUpTo1H(MakeValid(false.B, MluOp.MUL), Seq(
  d.mul    -> MakeValid(true.B, MluOp.MUL),
  d.mulh   -> MakeValid(true.B, MluOp.MULH),
  d.mulhsu -> MakeValid(true.B, MluOp.MULHSU),
  d.mulhu  -> MakeValid(true.B, MluOp.MULHU),
), MluOp)
io.mlu(i).valid := tryDispatch && mlu.valid
io.mlu(i).bits.addr := rdAddr(i)
io.mlu(i).bits.op := mlu.bits
```

**读写口的连线**——顶层把 4 个 lane 的派发命令与寄存器堆读口接上：

[SCore.scala:L248-L252](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L248-L252) —— MLU 的每路请求对应一对读口（rs1=读口 2i，rs2=读口 2i+1）：

```scala
for (i <- 0 until p.instructionLanes) {
  mlu.io.req(i) <> dispatch.io.mlu(i)
  mlu.io.rs1(i) := regfile.io.readData(2 * i)
  mlu.io.rs2(i) := regfile.io.readData((2 * i) + 1)
}
```

**写回口共享**——MLU 与 DVU 抢同一个寄存器堆写口 `writeData(instructionLanes)`：

[SCore.scala:L403-L414](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L403-L414) —— 把 `mlu.io.rd`、`dvu.io.rd` 等汇入一个 Arbiter 再写回：

```scala
val mluDvuOffset = p.instructionLanes
val mluDvuInputs = Seq(mlu.io.rd, dvu.io.rd) ++ ...
val arb = Module(new Arbiter(new RegfileWriteDataIO(p), mluDvuInputs.length))
arb.io.in <> mluDvuInputs
regfile.io.writeData(mluDvuOffset).valid := arb.io.out.valid
regfile.io.writeData(mluDvuOffset).bits.addr := arb.io.out.bits.addr
regfile.io.writeData(mluDvuOffset).bits.data := arb.io.out.bits.data
```

Arbiter 的输入顺序里 `mlu.io.rd` 排在 `dvu.io.rd` 前面，意味着二者同周期都有结果时 **MLU 优先**写回；配合下文 4.3 节的 `dvu.io.rd.ready := !mlu.io.rd.valid`，DVU 会主动让路。

#### 4.1.4 代码实践

**实践目标**：用源码确认「全核唯一实例」与「写回共享」这两个事实。

**操作步骤**：

1. 打开 [SCore.scala:L96-L97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L96-L97)，确认 `mlu`/`dvu` 在顶层只各实例化一次（不像 ALU 那样写在 lane 循环里）。
2. 对照 [SCore.scala:L248-L252](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L248-L252) 与 ALU 的连线（在同一个文件里搜索 `alu`），体会「ALU 每 lane 一个、MLU 全核一个」的区别。
3. 在 [SCore.scala:L403-L414](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L403-L414) 找到写回仲裁器，数一数它的输入路数，确认 MLU 与 DVU 共享一个写口。

**需要观察的现象**：`mlu`/`dvu` 不在 `for (i <- 0 until instructionLanes)` 循环内；而写回 Arbiter 的输入列表把 `mlu.io.rd`、`dvu.io.rd` 与若干其它来源合并。

**预期结果**：你能用一句话回答「为什么 CoralNPU 每周期能派发 4 条指令、却不能同周期执行 4 条乘法」。**待本地验证**：在仓库里执行 `git grep -n "Mlu(p)\|Dvu(p)" hdl/` 应只命中 SCore.scala 一处实例化。

#### 4.1.5 小练习与答案

**练习 1**：如果把 MLU 改成「每 lane 一个」，最大好处和最大代价分别是什么？
**参考答案**：好处是同周期可并行执行多条乘法、提升吞吐；代价是面积/功耗上升（4 个 32×32 乘法器）。CoralNPU 选了单实例，说明在它的目标负载（ML 推理里标量乘法不密集）下，省面积更划算。

**练习 2**：译码器为什么为「每个 lane」都生成一路 `io.mlu(i)`，而不是只生成一路？
**参考答案**：因为乘法指令可能出现在任意 lane 上（派发器按程序顺序把指令填进 4 个槽）。译码器不知道某条乘法会落在哪个 lane，所以每 lane 都备一路 MLU 命令输出，再由 MLU 内部的 Arbiter 在运行时挑一个。

---

### 4.2 MLU 三级流水线：从仲裁到写回

#### 4.2.1 概念说明

mlu.md 把 MLU 描述成一个**三级流水线**（见 [mlu.md:L46-L56](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/mlu.md#L46-L56)）：

1. **派发级（Dispatch）**：Arbiter 在 4 个 lane 里挑出本周期要执行的乘法，记录它来自哪个 lane（`sel`），命令在下一拍进入计算级。
2. **计算级（Compute）**：用 rs1/rs2 算出 64 位以上的乘积（带符号扩展），这是真正做乘法的地方。
3. **写回级（Writeback）**：从乘积里截取需要的 32 位，送上 `io.rd` 写回寄存器堆。

M 扩展的四条乘法指令的差别，全在「符号怎么扩展」和「取乘积的哪 32 位」上：

| 指令 | rs1 | rs2 | 结果 |
| --- | --- | --- | --- |
| `mul` | 低 32 位（符号无关） | 低 32 位（符号无关） | 乘积低 32 位 |
| `mulh` | 有符号 | 有符号 | 乘积高 32 位 |
| `mulhsu` | 有符号 | 无符号 | 乘积高 32 位 |
| `mulhu` | 无符号 | 无符号 | 乘积高 32 位 |

> 为什么 `mul` 对符号「无所谓」？因为两个 32 位数乘积的**低 32 位**在有符号和无符号解释下完全相同（差异只出现在高位）。所以 `mul` 直接取低 32 位即可，不必关心符号。

#### 4.2.2 核心流程

一条 `mulh` 在三级流水里的数据流动（N 为派发周期）：

```text
周期 N   派发级：Arbiter 选定 lane k → 记 sel=OneHot(k)、op=MULH、rd
            │ （Queue 寄存一拍）
            ▼
周期 N+1 计算级：按 sel 选出 rs1/rs2 → 符号扩展(rs1,rs2 均有符号) → prod=rs1s*rs2s
            │ （Queue 寄存一拍）
            ▼
周期 N+2 写回级：取 prod 的高 32 位 → io.rd.valid=1
```

符号扩展用一条简单规则统一处理四种指令：

\[
\text{prod} = \text{sext}(rs1,\, s_1) \times \text{sext}(rs2,\, s_2),\qquad s_1,s_2\in\{0,1\}
\]

其中 \(s_1\) 为 1 表示 rs1 当有符号数（前面补符号位），为 0 表示无符号（前面补 0）。代码用 `Cat(sign && rs(31), rs)` 一次实现：当需要符号且最高位为 1 时补 1，否则补 0，把 32 位扩展成 33 位有符号数再相乘。

#### 4.2.3 源码精读

**派发级——仲裁与 lane 记录**：

[Mlu.scala:L66-L76](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L66-L76) —— Arbiter 选 lane，`UIntToOH(chosen)` 生成 OneHot 的 `sel`，`Queue(stage1, 1, true)` 当作级间寄存器：

```scala
// Stage 1 select and decode instruction
val arb = Module(new Arbiter(new MluCmd(p), p.instructionLanes))
arb.io.in <> io.req

val stage1 = Wire(Decoupled(new MluStage1(p)))
stage1.valid := arb.io.out.valid
stage1.bits.rd := arb.io.out.bits.addr
stage1.bits.op := arb.io.out.bits.op
stage1.bits.sel := UIntToOH(arb.io.chosen)   // 记住来自哪个 lane
arb.io.out.ready := stage1.ready
val stage2Input = Queue(stage1, 1, true)
```

> 小贴士：`Queue(stage1, 1, true)` 里的 `true` 是 `pipe=true`，表示队列空且下游能收时可以「直通」。它在这里扮演级间锁存器，把派发级与计算级解耦。

**计算级——按 sel 选操作数 + 符号扩展 + 乘法**：

[Mlu.scala:L84-L92](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L84-L92) —— 这段是理解四种乘法差异的关键：

```scala
val rs1 = (0 until p.instructionLanes).map(x => MuxOR(valid2in & sel2in(x), io.rs1(x).data)).reduce(_ | _)
val rs2 = (0 until p.instructionLanes).map(x => MuxOR(valid2in & sel2in(x), io.rs2(x).data)).reduce(_ | _)

val rs2signed = op2in.isOneOf(MluOp.MULH)                          // 只有 MULH：rs2 有符号
val rs1signed = op2in.isOneOf(MluOp.MULHSU) || rs2signed           // MULHSU 或 MULH：rs1 有符号
val rs1s = Cat(rs1signed && rs1(p.xlen - 1), rs1).asSInt
val rs2s = Cat(rs2signed && rs2(p.xlen - 1), rs2).asSInt
val prod = rs1s * rs2s
assert(prod.getWidth == (2 * p.xlen + 2))
```

读懂这两行 `signed` 逻辑就等于读懂了四种乘法：

- `mulh`（有符号×有符号）：`rs2signed=true`，`rs1signed=true`。
- `mulhsu`（有符号×无符号）：`rs2signed=false`，`rs1signed=true`。
- `mulhu`（无符号×无符号）：`rs2signed=false`，`rs1signed=false`。
- `mul`：两个都 false，但因为它只取低 32 位，符号无所谓。

`prod` 宽度为 \(2\times\text{xlen}+2=66\) 位（两个 33 位有符号数相乘）。

**写回级——位选与输出**：

[Mlu.scala:L101-L118](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L101-L118) —— `MUL` 取低 32 位，其余取高 32 位：

```scala
val stage3Input = Queue(stage2, 1, true)
val op3in = stage3Input.bits.op
val prod3in = stage3Input.bits.prod

// To be guarded by stage3Input.valid
val mul = Mux(
    op3in === MluOp.MUL,
    prod3in(p.xlen - 1, 0),           // MUL：乘积低 32 位
    prod3in(2 * p.xlen - 1, p.xlen))  // MULH/MULHSU/MULHU：乘积高 32 位

stage3Input.ready := io.rd.ready
io.rd.valid     := stage3Input.valid
io.rd.bits.addr := stage3Input.bits.rd
io.rd.bits.data := mul
```

**时序金标准——MluTest**：

[MluTest.scala:L31-L58](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/MluTest.scala#L31-L58) —— 给 lane 0 发一个 `MUL`，rs1=rs2=2，预期结果 4：

```scala
dut.io.req(0).bits.addr.poke(13)
dut.io.req(0).bits.op.poke(MluOp.MUL)
dut.io.req(0).valid.poke(true.B)
...
dut.clock.step()                       // 第 1 拍：派发级
dut.io.req(0).valid.poke(false.B)
dut.io.rd.ready.poke(true.B)
dut.clock.step()                       // 第 2 拍：计算级
dut.io.rd.valid.expect(1)              // 写回级：结果有效
dut.io.rd.bits.addr.expect(13)
dut.io.rd.bits.data.expect(4)          // 2 * 2 = 4
```

这个测试对应 mlu.md 的三级流水模型：派发级（第 1 拍）→ 计算级 → 写回级（`rd.valid` 拉高）。从 `req` 有效到 `rd.valid` 拉高，经历约两个时钟周期。

#### 4.2.4 代码实践

**实践目标**：亲手验证「符号扩展规则」与「位选规则」对应到四种乘法的正确结果。

**操作步骤**（纯源码阅读型，无需运行硬件）：

1. 在 [Mlu.scala:L87-L88](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L87-L88) 列出四条指令各自对应的 `(rs1signed, rs2signed)` 取值（参考 4.2.3 的清单）。
2. 取一个具体例子手算：`mulh`，rs1 = `0xFFFFFFFF`（即 -1），rs2 = `0x00000002`（即 2）。两数按有符号扩展后相乘得 `-2`，其 64 位补码为 `0xFFFFFFFFFFFFFFFE`，高 32 位 = `0xFFFFFFFF`（即 -1）。这与 RISC-V 规范一致（`(-1)*2 = -2`，高 32 位为 `-1`）。
3. 同样两数做 `mulhsu`：rs1 有符号 = -1，rs2 无符号 = 2，乘积 = `0x00000001FFFFFFFE`，高 32 位 = `0x00000001`。体会 `mulh` 与 `mulhsu` 仅因 rs2 符号不同而结果不同。
4. 在 [Mlu.scala:L106-L110](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L106-L110) 确认 `mul` 走低 32 位分支、其它走高 32 位分支。

**需要观察的现象**：`mulh` 与 `mulhsu` 在「同样的操作数」下给出不同的高位结果，差异完全由 `rs2signed` 一行决定。

**预期结果**：你能不查手册地说出「`mulhsu` 的 rs1 有符号、rs2 无符号」这一关键区别，并指出是哪两行代码负责。**待本地验证**：如想跑测试，可执行 `bazel test //hdl/chisel/src/coralnpu/scalar:MluTest`（目标名以 BUILD 为准）。

#### 4.2.5 小练习与答案

**练习 1**：`prod` 为什么是 66 位而不是 64 位？
**参考答案**：rs1s/rs2s 各被扩展成 33 位有符号数（`Cat(sign && msb, 32位)`），两个 33 位有符号数相乘得 \(33+33=66\) 位。多出来的 2 位是符号扩展位，保证高 32 位（`prod(63,32)`）在有符号乘法下被正确符号填充。

**练习 2**：把测试里的 `op` 从 `MUL` 改成 `MULHU`，rs1=rs2=2，预期 `rd.bits.data` 是多少？
**参考答案**：`MULHU` 取乘积高 32 位。\(2\times2=4\)，64 位乘积为 `0x0000000000000004`，高 32 位 = `0`。所以 `rd.bits.data` 应为 0。

**练习 3**：为什么 `mul` 不需要关心符号，却仍走「无符号扩展（两个 signed 都为 false）」？
**参考答案**：`mul` 只取低 32 位，而低 32 位与符号无关；代码顺手用无符号扩展（补 0）算乘积，再取低 32 位，结果与规范一致。这也让 `mul` 的符号逻辑最简单。

---

### 4.3 DVU：可变延迟的迭代除法器

#### 4.3.1 概念说明

除法比乘法麻烦得多：乘法可以用组合电路一个周期（或流水）算完，除法在硬件里几乎总是「迭代」——一位一位地试商。CoralNPU 的 DVU 采用**一位一拍**的恢复余数除法，并且做了一个聪明优化——**提前结束（early termination）**：先数被除数的前导零，跳过那些必然商 0 的高位迭代。

后果是 DVU 的延迟**不固定**：

- 被除数小（前导零多）→ 跳过的迭代多 → 很快出结果；
- 被除数大（前导零少）→ 几乎要跑满 32 拍。

而且，与 ALU/MLU 不同，**DVU 在计算期间会持续向派发端拉低 `req.ready`**——派发器送不进新的除法指令，只能等当前这条算完。这就是「可变延迟 + 反压」的含义。

DVU 的源码头注释直说了它与 `common/IDiv` 的区别：[Dvu.scala:L50-L51](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L50-L51) ——「This implementation differs to common::idiv by supporting early termination, and only performs one bit per cycle.」

#### 4.3.2 核心流程

DVU 把一次除法分成「准备 → 迭代 → 输出」三段，用一个状态机驱动：

```text
① fire：收下命令，记录 addr / 是否有符号 / 是 div 还是 rem，active=1
② 准备拍（active && !compute）：
     - 取被除数、除数的绝对值（有符号时按补码取负）
     - clz = 前导零计数（被除数高位有几个 0）
     - 把被除数左移 clz 位（跳过高位的 0 商）
     - count = clz，remain = 0，denom = |除数|
③ 迭代拍（compute && count < 32）：每拍做一次「移位-试减」：
     - shfRemain = (remain << 1) | divide 的最高位
     - 若 shfRemain >= denom：商位=1，新余=shfRemain - denom
     - 否则：商位=0，新余=shfRemain（恢复）
     - divide 左移并追加商位，count++
④ 当 count 的最高位置位（count 到达 32）：io.rd.valid=1，输出 div 或 rem（按符号还原）
```

迭代次数约为 \(32 - \text{clz}\)，所以总延迟约为：

\[
T_{\text{DVU}} \approx 1\text{（准备拍）} + (32 - \text{clz}(|\text{被除数}|))\text{（迭代拍）}
\]

被除数越小，`clz` 越大，延迟越短。除零（`denom=0`）走满延迟以简化逻辑（见 [Dvu.scala:L122-L124](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L122-L124) 的注释）。

> 恢复余数除法的本质和你小学竖式除法一样：把上一次的余数左移一位、拉下被除数的下一位，试着减去除数；够减就商 1、保留差；不够减就商 0、恢复原值。硬件每拍处理一位商。

#### 4.3.3 源码精读

**反压的核心一行**：

[Dvu.scala:L90](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L90) —— DVU 只在完全空闲时才接收新命令：

```scala
io.req.ready := !active && !compute && !count(log2Ceil(p.xlen))
```

`active`/`compute` 标志计算进行中，`count` 的最高位（`count(log2Ceil(xlen))` = `count(5)`）标志结果待取走。三者任一为真，`req.ready=0`，派发器就送不进来——这就是反压。

**移位-试减的核心函数**：

[Dvu.scala:L53-L69](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L53-L69) —— 一位一拍的恢复余数除法：

```scala
def Divide(prvDivide: UInt, prvRemain: UInt, denom: UInt): (UInt, UInt) = {
  val shfRemain = Cat(prvRemain(p.xlen-2,0), prvDivide(p.xlen-1))  // 余数左移 + 拉入被除数最高位
  val subtract = shfRemain -& denom                                // -& 是「扩展减」，保留借位
  val divDivide = Wire(UInt(p.xlen.W))
  val divRemain = Wire(UInt(p.xlen.W))
  when (!subtract(p.xlen)) {            // 没借位 → 够减
    divDivide := Cat(prvDivide(p.xlen-2,0), 1.U(1.W))   // 商位=1
    divRemain := subtract(p.xlen-1,0)                    // 余=差
  } .otherwise {                        // 借位 → 不够减
    divDivide := Cat(prvDivide(p.xlen-2,0), 0.U(1.W))   // 商位=0
    divRemain := shfRemain                                // 余=恢复（移位后原值）
  }
  (divDivide, divRemain)
}
```

注意 `Cat(prvDivide(p.xlen-2,0), bit)`：把 `divide` 左移一位并在最低位追加本拍的商。迭代 32 拍后，`divide` 里就装满了 32 位商。

**准备拍——绝对值、前导零、左移跳过**：

[Dvu.scala:L114-L129](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L114-L129) —— 提前结束的关键：

```scala
when (active && !compute) {
  addr2    := addr1
  signed2d := signed1 && (io.rs1.data(p.xlen-1) =/= io.rs2.data(p.xlen-1)) && !divByZero  // 商的符号
  signed2r := signed1 && io.rs1.data(p.xlen-1)                                              // 余数的符号
  divide2  := divide1

  val inp = Mux(signed1 && io.rs1.data(p.xlen-1), ~io.rs1.data + 1.U, io.rs1.data)  // |被除数|
  val clz = Mux(io.rs2.data === 0.U, 0.U, Clz1(inp))                                 // 前导零
  denom  := Mux(signed1 && io.rs2.data(p.xlen-1), ~io.rs2.data + 1.U, io.rs2.data)   // |除数|
  divide := inp << clz           // 左移跳过高位的 0 商
  remain := 0.U
  count  := clz                  // 从 clz 开始计数
}
```

`Clz1` 的定义见 [Dvu.scala:L93-L96](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L93-L96)，注释提醒它「比真正的 CLZ 小 1」。把 `count` 初始化为 `clz` 而非 0，正是为了在迭代段跳过那些高位——这就是「early termination」。

**迭代拍——调用 Divide**：

[Dvu.scala:L130-L137](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L130-L137)：

```scala
.elsewhen (compute && count < p.xlen.U) {
  val (div, rem) = Divide(divide, remain, denom)
  divide := div
  remain := rem
  count := count + 1.U
} .elsewhen (io.rd.valid && io.rd.ready) {
  count := 0.U     // 结果被取走，复位
}
```

**输出——符号还原与结果选择**：

[Dvu.scala:L139-L144](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L139-L144)：

```scala
val div = Mux(signed2d, ~divide + 1.U, divide)   // 商按需取负
val rem = Mux(signed2r, ~remain + 1.U, remain)    // 余数按需取负
io.rd.valid := count(log2Ceil(p.xlen))            // count 到 32 时结果有效
io.rd.bits.addr := addr2
io.rd.bits.data := Mux(divide2, div, rem)         // div/divu 选 div，rem/remu 选 rem
```

RISC-V 规定余数的符号跟随被除数、商的符号由两数是否同号决定，上面 `signed2r`/`signed2d` 正好对应。

**DVU 只接 slot0**——派发约束在顶层连线里写死：

[SCore.scala:L256-L264](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L256-L264)：

```scala
dvu.io.req <> dispatch.io.dvu(0)          // 只接 lane 0
dvu.io.rs1 := regfile.io.readData(0)
dvu.io.rs2 := regfile.io.readData(1)
dvu.io.rd.ready := !mlu.io.rd.valid       // 与 MLU 抢写口时让路

// TODO: make port conditional on pipeline index.
for (i <- 1 until p.instructionLanes) {
  dispatch.io.dvu(i).ready := false.B     // 其余 lane 的除法一律不收
}
```

这意味着：除法指令**只能从 lane 0 派发**，且同周期不能与 MLU 的写回冲突。

#### 4.3.4 代码实践

**实践目标**：解释「除法为什么可变延迟」以及「反压如何传递到派发端」。

**操作步骤**（源码追踪型）：

1. 在 [Dvu.scala:L90](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L90) 找到 `io.req.ready` 的表达式，列出三种让 `ready=0` 的条件。
2. 跟踪 [Dvu.scala:L114-L129](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L114-L129) 的准备拍：为什么把 `count` 初始化成 `clz`、把 `divide` 左移 `clz` 位，就能跳过高位的迭代？
3. 用两个极端例子估算延迟：
   - 被除数 = `0x00000001`（`Clz1` 返回 30）：迭代约 \(32-30=2\) 拍，总延迟很短。
   - 被除数 = `0x80000000`（最高位为 1，`Clz1` 返回 0）：迭代约 32 拍，延迟最长。
4. 在 [SCore.scala:L256-L264](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L256-L264) 确认除法只能从 lane 0 进，并思考：若一拍里 lane 0 是除法、lane 1 是乘法，会发生什么？（答：乘法可正常经 Arbiter 进入 MLU，除法独占 DVU，二者不冲突。）

**需要观察的现象**：`req.ready` 在整个计算与结果待取期间始终为 0，新的除法进不来；`count` 从 `clz` 起累加到 32 才让 `rd.valid` 拉高。

**预期结果**：你能用一句话解释「为什么小被除数的除法比大被除数快」。**待本地验证**：DVU 没有像 MluTest 那样的独立单测，行为主要靠 `tests/cocotb` 下的 ISA 回归验证（可在 `tests/cocotb` 搜索 `div`/`rem` 相关用例）。

#### 4.3.5 小练习与答案

**练习 1**：除法指令 `div a0, a0, a1` 中 a0=-6、a1=2，商和余数各是多少？符号怎么定？
**参考答案**：RISC-V 规定商向零截断、余数符号跟被除数。\(-6/2=-3\) 余 0。商 = -3（`0xFFFFFFFD`），余数 = 0。代码里 `signed2d`（两数异号）为真 → 商取负；`signed2r`（被除数为负）为真，但余数为 0 取负仍是 0。

**练习 2**：如果把 `io.req.ready` 改成恒为 1，会发生什么？
**参考答案**：DVU 会丢掉正在进行的中间状态、被新命令覆盖，产生错误的除法结果。反压正是为了串行化除法、保证一次算完再收下一条。

**练习 3**：为什么 DVU 的 `rd.ready` 要写成 `!mlu.io.rd.valid`？
**参考答案**：MLU 与 DVU 共享同一个寄存器堆写口（见 4.1.3 的写回 Arbiter）。当 MLU 本周期有结果要写时，DVU 主动把 `rd.ready` 拉低、暂缓一拍，避免两个结果抢同一个写口。

---

### 4.4 IDiv：每周期多位的并行除法器（对比阅读）

#### 4.4.1 概念说明

`common/IDiv.scala` 是另一种除法器实现。它的头注释写明用途：「An integer divide unit, to be fused with fdiv.」（[IDiv.scala:L21](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/IDiv.scala#L21)）——它是为**与浮点除法（fdiv）融合**而准备的通用整数除法器。

与 DVU 相比，IDiv 的设计取向相反：**不提前结束，但每周期算多位**，延迟更可预测。它和 DVU 共享同一种「移位-试减」算法，区别只在于「一位一拍」还是「多位一拍」。

> 说明：在当前 HEAD，标量核的整数除法用的是 `Dvu.scala`（见 [SCore.scala:L97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L97)）；`IDiv` 在仓库里只被它自己的 `EmitIDiv` App 引用（可用 `git grep "new IDiv"` 验证），尚未被核实例化。这里把它作为「另一种设计」来对比阅读，帮助理解取舍。

#### 4.4.2 核心流程

IDiv 的两个关键常量决定了它的吞吐：

[IDiv.scala:L28-L29](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/IDiv.scala#L28-L29)：

```scala
val Stages = 4        // 每拍算 4 位商
val Rcnt = 32 / Stages // = 8，满负荷 8 拍完成一次 32 位除法
```

所以 IDiv 是**固定延迟**（约 8 拍迭代），但代价是每拍要做 4 次「移位-试减」的组合逻辑，关键路径更长。对比：

| 维度 | Dvu（标量核用） | IDiv（common 库） |
| --- | --- | --- |
| 每拍商位数 | 1 | 4 |
| 延迟 | 可变（约 \(1+(32-\text{clz})\) 拍） | 固定（约 Rcnt=8 拍） |
| 提前结束 | 有（CLZ 跳过高位） | 无 |
| 用途 | 整数 div/rem 指令 | 预留与浮点 fdiv 融合 |
| 关键路径 | 短（一位试减） | 长（四位试减级联） |

#### 4.4.3 源码精读

**IDivComb2——每拍做 4 次试减**：

[IDiv.scala:L141-L149](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/IDiv.scala#L141-L149) —— `Stages==4` 分支里级联 4 次 `Divide`：

```scala
} else if (IDiv.Stages == 4) {
  val (div2, rem2) = Divide(div1, rem1, in.denom)
  val (div3, rem3) = Divide(div2, rem2, in.denom)
  val (div4, rem4) = Divide(div3, rem3, in.denom)
  out.divide := div4
  out.remain := rem4
}
```

这里的 `Divide`（[IDiv.scala:L159-L175](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/IDiv.scala#L159-L175)）与 Dvu 的 `Divide` 几乎一模一样——同一套恢复余数算法，只是 IDiv 把 4 拍的活儿压到 1 拍的组合电路里。

**与 DVU 同源的算法**——对比 [Dvu.scala:L53-L69](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L53-L69) 与 [IDiv.scala:L159-L175](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/IDiv.scala#L159-L175)，二者的 `Divide` 函数体结构完全一致，差别只是位宽写死成 32（IDiv）还是参数化（Dvu）。

#### 4.4.4 代码实践

**实践目标**：通过对比两个 `Divide` 函数，理解「同算法、不同并行度」的设计取舍。

**操作步骤**：

1. 并排打开 [Dvu.scala:L53-L69](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L53-L69) 与 [IDiv.scala:L159-L175](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/IDiv.scala#L159-L175)，逐行比对，确认它们是同一个算法。
2. 在 [IDiv.scala:L127-L157](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/common/IDiv.scala#L127-L157) 数一下 `Stages==4` 分支里调用了几次 `Divide`（应为 4 次，对应每拍 4 位）。
3. 思考：为什么标量核选 Dvu（一位一拍 + 提前结束）而不是 IDiv？提示：标量除法指令频率低，单拍关键路径短更重要；而 fdiv 在浮点流水里更需要可预测延迟。

**需要观察的现象**：两个 `Divide` 函数体几乎逐行相同；IDiv 把 4 次 `Divide` 级联在一拍内。

**预期结果**：你能说清「同一种恢复余数除法，Dvu 用时间换面积（一位一拍）、IDiv 用面积换时间（一拍四位）」。

#### 4.4.5 小练习与答案

**练习 1**：把 IDiv 的 `Stages` 从 4 改成 1，`Rcnt` 会变成多少？延迟如何变化？
**参考答案**：`Rcnt = 32/1 = 32`，每拍只算 1 位，满负荷需要 32 拍——退化和 Dvu（无提前结束时）一样。

**练习 2**：IDiv 为什么不带 CLZ 提前结束？
**参考答案**：IDiv 面向浮点 fdiv，追求**延迟可预测**（便于融入浮点流水线的时序规划）；提前结束会让延迟随操作数变化，反而不利于流水线设计。Dvu 面向标量，更在意平均情况下省周期。

---

## 5. 综合实践

**任务**：给一小段汇编，画出它的派发与执行时序，重点体现「单实例约束」与「可变延迟」。

考虑下面 4 条连续指令（假设无寄存器冒险）：

```text
mul  a2, a0, a1     # lane k0
mul  a3, a0, a1     # lane k1（同周期另一槽？）
add  a4, a0, a1     # ALU
div  a5, a0, a1     # 除法，a0 较小
```

请完成：

1. **单实例约束**：如果前两条 `mul` 落在**同一周期**的两个不同 lane，它们能否都进 MLU？参考 [Mlu.scala:L66-L76](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L66-L76) 的 Arbiter 与 u4-l4 的派发规则，画出第二条 `mul` 的等待过程。
2. **写回共享**：若同周期 MLU 与 DVU 都想写回，谁先？参考 [SCore.scala:L256-L264](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L256-L264) 的 `dvu.io.rd.ready := !mlu.io.rd.valid`。
3. **可变延迟**：设 a0=1、a1=3，用 4.3.2 的公式估算 `div` 大约要多少拍（被除数 1 的 `Clz1`≈30，迭代约 2 拍）。再设 a0=`0x80000000`，估算最坏情况拍数。
4. **反压传递**：在 `div` 计算期间，如果下一条又是指向 DVU 的除法，派发器会怎样？参考 [Dvu.scala:L90](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L90)。

**预期产出**：一张时序表，列出每个周期 MLU/DVU 的状态（空闲/仲裁中/计算中/写回）与 `req.ready` 取值。

> 本实践为源码阅读+推理型，**待本地验证**：若想用仿真确认，可在 `tests/cocotb` 下找到除法/乘法相关的 ISA 回归用例（搜索 `mul`/`div`），用 cocotb 跑一遍并观察指令轨迹（参考 u2-l4 的 `execute_from` + 指令 trace 流程）。

## 6. 本讲小结

- CoralNPU 全核只有**一个 MLU** 和**一个 DVU**（[SCore.scala:L96-L97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L96-L97)），是面积/功耗与吞吐的取舍。
- MLU 是**三级流水线**（派发→计算→写回），用 Arbiter 在 4 个 lane 里选一个；MUL/MULH/MULHSU/MULHU 的差别全在「符号扩展」与「取乘积的哪 32 位」([Mlu.scala:L87-L110](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L87-L110))。
- MLU 的时序以 [MluTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/MluTest.scala) 为金标准：`req` 有效后约两拍 `rd.valid` 拉高。
- DVU 用**一位一拍的恢复余数除法** + **CLZ 提前结束**，所以是**可变延迟**（[Dvu.scala:L50-L51](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L50-L51)）。
- DVU 靠 `io.req.ready := !active && !compute && !count(MSB)` 在计算期间**反压**派发端（[Dvu.scala:L90](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Dvu.scala#L90)），且只能从 **lane 0** 派发（[SCore.scala:L256-L264](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L256-L264)）。
- MLU 与 DVU **共享一个寄存器堆写口**，经 Arbiter 仲裁，MLU 优先；`common/IDiv.scala` 是同算法但「每拍 4 位、固定延迟」的另一种实现，预留与浮点 fdiv 融合。

## 7. 下一步学习建议

- **往寄存器堆侧**：MLU/DVU 的结果都写回寄存器堆，建议下一讲读 u5-l3（整数/浮点寄存器堆与 CSR），看写口仲裁的「另一端」如何接收这些结果，以及 CSR 如何参与派发约束。
- **往浮点侧**：本讲提到 IDiv 将与 fdiv 融合，可预习 u5-l4（FPU），了解浮点通路如何复用整数除法器。
- **往验证侧**：DVU 没有独立单测，其行为由 `tests/cocotb` 的 ISA 回归保障。学完 u2-l4（cocotb）后，可去 `tests/cocotb` 找 `mul`/`div`/`rem` 用例，对照本讲的时序结论做端到端验证。
- **往微架构纵深**：想了解「单实例约束」如何在派发器里形式化为记分板与槽位规则，可重读 u4-l4（派发规则、记分板与退休）的执行单元约束一节。
