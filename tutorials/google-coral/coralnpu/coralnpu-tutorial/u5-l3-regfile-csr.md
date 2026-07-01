# 整数/浮点寄存器堆与 CSR

> 单元 5 · 讲义 3 · 中级
> 依赖：u4-l3（指令译码）、u4-l4（派发/记分板/退休）、u5-l1（ALU/BRU 执行单元）

## 1. 本讲目标

标量核每周期最多派发 4 条指令、让多条指令乱序完成再按序退休，这一切的前提是：**操作数从哪里来、写回值落到哪里去、对机器状态的访问如何保持精确**。本讲聚焦支撑这一机制的三大存储单元。读完本讲你应当能够：

- 数出整数寄存器堆 `Regfile` 的读/写端口数量，并解释它们为何刚好支撑 4 发射派发；
- 说清楚 `Regfile` 内部那张「记分板（scoreboard）」如何用一张位图解决 RAW/WAW 数据冒险，以及为何天然不存在 WAR；
- 描述浮点寄存器堆 `FRegfile` 与整数寄存器堆的同构与差异；
- 追踪一条 `csrrw`/`csrrs`/`csrrc` 指令在 `Csr.scala` 内部的完整执行路径（读旧值、算新值、写 CSR、把旧值写回 rd）；
- 解释 CSR 指令为何被强制约束在「首槽、独占当周期派发、且必须等 ROB 排空」，即「当作控制流」处理的根本原因。

## 2. 前置知识

在进入源码前，先用三段话把概念铺平。

**寄存器堆（Register File）与端口。** 寄存器堆是 CPU 内最快的存储，由一组同宽的寄存器组成（RISC-V 整数有 `x0..x31` 共 32 个）。所谓「端口」指可以同时访问的独立通道：一个**读端口**包含「地址 + 数据」一对线，能在当拍读出一个寄存器；一个**写端口**包含「地址 + 数据 + 有效」一组线，能在时钟沿写入一个寄存器。端口越多面积越大，但能并行读写越多操作数。CoralNPU 是超标量核，每条派发指令要两个源操作数（rs1、rs2）和一个目的（rd），所以端口数与「每周期派发几条」直接挂钩。

**记分板（Scoreboard）。** 当一条指令的 rd 还没写回，下一条指令又要读同一个寄存器，就会读到旧值（RAW，Read After Write 冒险）。记分板用「每个寄存器一位」的位图标记「该寄存器有未完成的写」：派发时把目的位置 1（set），真正写回时清 0（clr）；派发器看到源寄存器在记分板里为 1 就停拍（interlock）。CoralNPU 把记分板直接做进寄存器堆，让它作为「数据冒险的真相源」对外暴露给译码器。

**CSR（Control and Status Register）。** 除了 32 个通用寄存器，RISC-V 还有一组「控制状态寄存器」，如 `mstatus`（机器状态）、`mtvec`（异常入口）、`mepc`（异常返回地址）、`mie`/`mip`（中断）、`mcycle`（周期计数）等。它们由 `Zicsr` 扩展的三条指令访问：`csrrw`（读旧值并写新值）、`csrrs`（置位）、`csrrc`（清位）。CSR 描述的是「机器整体的状态」，改一个就可能改变后续所有指令的行为（比如关中断、改异常入口），所以访问必须**严格按程序顺序、精确执行**——这是本讲后半段「为何 CSR 被当作控制流」的全部根因。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/chisel/src/coralnpu/scalar/Regfile.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala) | 整数寄存器堆：32 项、8 读 6 写端口、内含记分板与写转发 |
| [hdl/chisel/src/coralnpu/scalar/FRegfile.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FRegfile.scala) | 浮点寄存器堆：32 项 Fp32、可配端口数、自带浮点记分板 |
| [hdl/chisel/src/coralnpu/scalar/Csr.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala) | CSR 文件：所有机器/调试/向量 CSR 的存储、读写与异常/中断逻辑 |
| [hdl/chisel/src/coralnpu/Interfaces.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala) | 端口与命令的 Bundle 定义（`CsrOp`、`CsrCmd`、各 `RegfileIO`） |
| [hdl/chisel/src/coralnpu/scalar/SCore.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala) | 标量核顶层：把寄存器堆、CSR、派发器、各执行单元连起来 |
| [hdl/chisel/src/coralnpu/scalar/Decode.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala) | 译码+派发器：CSR 的「首槽、排空 ROB」约束在此强制 |

## 4. 核心概念与源码讲解

### 4.1 整数寄存器堆 Regfile：端口组织与 4 发射支撑

#### 4.1.1 概念说明

`Regfile` 是标量核的整数寄存器堆。它要做两件事：第一，给每条正在派发的指令提供两个源操作数、接收它的目的写回；第二，维护一张记分板，告诉派发器「哪些寄存器还在被写、不能读」。本模块只讲端口组织，记分板单独放到 4.2。

文件开头的注释就把结论摆出来了：

```scala
// Regfile: 32 entry scalar register file with 8 read ports and 6
// write ports. Houses a global scoreboard that informs of interlock
// deps inside the decoders.
```

——32 项、8 个读端口、6 个写端口，并内含一张供译码器判断互锁的全局记分板。见 [hdl/chisel/src/coralnpu/scalar/Regfile.scala:15-17](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L15-L17)。

#### 4.1.2 核心流程：端口数怎么来的

派发宽度 `instructionLanes = 4`（见 [hdl/chisel/src/coralnpu/Parameters.scala:73](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L73)）。每条指令要 rs1、rs2 两个源，所以读端口数：

\[
\text{读端口} = 2 \times \text{instructionLanes} = 2 \times 4 = 8
\]

写端口分两部分：每个 lane 一个（共 4 个），再加 2 个「额外」端口给多周期单元：

```scala
val extraWritePorts = 2
// ...
val readAddr = Vec(p.instructionLanes * 2, new RegfileReadAddrIO(p))   // 8 读地址
val readSet  = Vec(p.instructionLanes * 2, new RegfileReadSetIO(p))    // 8 立即数注入
val writeAddr = Vec(p.instructionLanes, new RegfileWriteAddrIO(p))     // 4 写标记（派发端）
val readData = Vec(p.instructionLanes * 2, new RegfileReadDataIO(p))   // 8 读数据
val writeData = Vec(p.instructionLanes + extraWritePorts, ...)         // 6 写数据（写回端）
```

写端口数：

\[
\text{写端口} = \text{instructionLanes} + \text{extraWritePorts} = 4 + 2 = 6
\]

见 [hdl/chisel/src/coralnpu/scalar/Regfile.scala:52-78](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L52-L78)。

这里有个容易混的关键区分：

- `writeAddr`（4 个）是**派发端**的「写标记」端口，每周期至多 4 条派发指令各标记一个 rd，用来 **set 记分板**；
- `writeData`（6 个）是**写回端**的「写数据」端口，用来真正写入并 **clr 记分板**。

多出来的 2 个写回端口服务于「结果晚几拍才到」的单元。在顶层 `SCore` 里可以看到它们的接线：第 4 号端口（`mluDvuOffset = instructionLanes`）接 MLU 与 DVU 经仲裁后的写回，第 5 号端口（`lsuOffset = instructionLanes + 1`）接 LSU 的写回：

```scala
val mluDvuOffset = p.instructionLanes          // = 4
regfile.io.writeData(mluDvuOffset).valid := arb.io.out.valid          // MLU/DVU 仲裁输出
val lsuOffset    = p.instructionLanes + 1      // = 5
regfile.io.writeData(lsuOffset).valid := lsu.io.rd.valid              // LSU 写回
```

见 [hdl/chisel/src/coralnpu/scalar/SCore.scala:403-421](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L403-L421)。于是 4 个 lane 写端口覆盖 ALU/BRU/CSR/RVV/Float 的当拍写回，2 个额外端口覆盖 MLU/DVU 与 LSU 的延迟写回——这正好对应 u5-l2 讲过的「MLU 三级流水、DVU 可变延迟、LSU 多周期」。

> 顺带一提：派发端只有 4 个 set 端口，但写回端有 6 个 clr 端口，并不矛盾——每周期至多派发 4 条（至多 4 个 set），但此刻可能有「上一拍派发的 MLU」和「更早派发的 LSU」同时写回（至多 6 个 clr）。

#### 4.1.3 源码精读：x0 优化与读端口的写转发

RISC-V 的 `x0` 永远是 0，写它等于丢弃。`Regfile` 干脆把 `x0` 优化掉，既省存储又免去对 index 0 的特判：

```scala
writeValid(0) := true.B  // do not require special casing of indices
writeData(0)  := 0.U     // regfile(0) is optimized away
```

见 [hdl/chisel/src/coralnpu/scalar/Regfile.scala:131-132](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L131-L132)。其余 31 个寄存器（index 1..31）才真正参与「哪个写端口命中我」的译码与数据选择，并对「同时命中多个端口」做断言（保证 6 个写端口不会撞同一个寄存器）：

```scala
for (i <- 1 until p.scalarRegCount) {
  val valid = (0 until p.instructionLanes + extraWritePorts).map(j => {
      val addrValid = (io.writeData(j).bits.addr === i.U)
      (io.writeData(j).valid && addrValid && !io.writeMask(j).valid)})
  // ...
  assert(PopCount(valid) <= 1.U)
}
```

见 [hdl/chisel/src/coralnpu/scalar/Regfile.scala:134-146](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L134-L146)。注意 `!io.writeMask(j).valid`：被 mask 的写（错误路径上的投机写）不真正落盘，但仍会清记分板（见 4.2）。

读端口带**写转发**：当拍若有写命中正在读的寄存器，直接用当拍要写入的新值，避免多等一拍：

```scala
for (i <- 0 until (p.instructionLanes * 2)) {
  val idx = io.readAddr(i).addr
  val write = VecAt(writeValid, idx)
  rdata(i) := VecAt(regfile, idx)
  wdata(i) := VecAt(writeData, idx)
  rwdata(i) := Mux(write, wdata(i), rdata(i))   // 当拍写命中则转发新值
}
```

见 [hdl/chisel/src/coralnpu/scalar/Regfile.scala:164-173](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L164-L173)。最后还有 `readSet` 优先级：当派发器声明「这个源用立即数/PC 注入」时（如 `addi` 的 rs2、CSR 立即数变体的 rs1），读端口直接采用注入值而非寄存器内容：

```scala
readDataBits(i) := MuxCase(readDataBits(i), Seq(
    io.readSet(i).valid -> io.readSet(i).value,
    io.readAddr(i).valid -> rwdata(i)
))
```

见 [hdl/chisel/src/coralnpu/scalar/Regfile.scala:176-185](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L176-L185)。

#### 4.1.4 代码实践：数端口并解释 4 发射

1. **实践目标**：亲自把 `Regfile` 的读/写端口数算出来，并验证它们与 4 发射派发的需求一一对应。
2. **操作步骤**：
   - 打开 [hdl/chisel/src/coralnpu/scalar/Regfile.scala:52-83](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L52-L83)，对每个 `Vec(...)` 字段写下它的元素个数与用途。
   - 打开 [hdl/chisel/src/coralnpu/scalar/SCore.scala:269-302](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L269-L302)，看 lane `i` 的两个读地址 `readAddr(2*i)`、`readAddr(2*i+1)` 分别接到派发器的 `rs1Read(i)`、`rs2Read(i)`。
   - 再到 [hdl/chisel/src/coralnpu/scalar/SCore.scala:403-421](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L403-L421) 确认第 4、5 号写端口分别接 MLU/DVU 仲裁器和 LSU。
3. **需要观察的现象**：读端口 `8 = 2×4`、写端口 `6 = 4+2`，与文件头注释完全一致；每个 lane 占用恰好 2 个读端口和 1 个 lane 写端口。
4. **预期结果**：填出一张表——「读端口 8 个（每 lane 2，供 rs1/rs2）」「lane 写端口 4 个（ALU/BRU/CSR/RVV/Float 当拍写回）」「额外写端口 2 个（MLU/DVU、LSU 延迟写回）」。
5. **待本地验证**：若想眼见为实，可用 `EmitRegfile`（[Regfile.scala:236-239](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L236-L239)）导出 SystemVerilog，在生成的 `Regfile.sv` 里数 `input ... readAddr` 与 `input ... writeData` 的组数。

#### 4.1.5 小练习与答案

**练习 1**：为什么读端口数是 `instructionLanes*2` 而不是 `instructionLanes`？
**答**：每条派发指令有两个源操作数 rs1、rs2，需要两个独立的读端口才能在同一拍同时读出，所以是 `2×4=8`。

**练习 2**：`writeAddr` 有 4 个、`writeData` 有 6 个，为何不对称？
**答**：`writeAddr` 在派发端标记「将要写的 rd」（至多 4 条派发），`writeData` 在写回端真正写值；MLU/DVU、LSU 是多周期单元，结果晚几拍经额外端口写回，所以写回端比派发端多 2 个。

---

### 4.2 记分板：RAW/WAW 互锁的硬件实现

#### 4.2.1 概念说明

记分板是 `Regfile` 内部一张 `scalarRegCount`（=32）位的寄存器 `scoreboard`，第 `i` 位为 1 表示「寄存器 `xi` 有一个尚未写回的写，读它要等」。派发器（u4-l4）在判断 RAW/WAW 时直接消费这张表的两个视图：`regd`（已寄存的值）和 `comb`（扣除本拍即将完成写后的值）。

#### 4.2.2 核心流程：set / clr / 更新

记分板的更新逻辑是一句位运算：

```scala
val nxtScoreboard = (scoreboard & ~scoreboard_clr) | scoreboard_set
```

即「先清掉本拍真正写回的位（clr），再置上本拍新派发的目的位（set）」。其中：

- `scoreboard_set` 由派发端的 `writeAddr`（4 个 lane）求或——派发即标记；
- `scoreboard_clr0` 由写回端的 `writeData`（6 个端口）求或——写回即清除，并被 `writeMask` 屏蔽的投机写也照常 clr；
- `scoreboard_clr` 在此基础上把第 0 位（x0）强制清 0，因为 x0 永远可读。

```scala
val scoreboard_set = io.writeAddr
    .map(x => MuxOR(x.valid, UIntToOH(x.addr, p.scalarRegCount))).reduce(_|_)
val scoreboard_clr0 = io.writeData
    .map(x => MuxOR(x.valid, UIntToOH(x.bits.addr, p.scalarRegCount))).reduce(_|_)
val scoreboard_clr = Cat(scoreboard_clr0(p.scalarRegCount - 1, 1), 0.U(1.W))
when (scoreboard_set =/= 0.U || scoreboard_clr =/= 0.U) {
  val nxtScoreboard = (scoreboard & ~scoreboard_clr) | scoreboard_set
  scoreboard := Cat(nxtScoreboard(p.scalarRegCount - 1, 1), 0.U(1.W))
}
io.scoreboard.regd := scoreboard
io.scoreboard.comb := scoreboard & ~scoreboard_clr
```

见 [hdl/chisel/src/coralnpu/scalar/Regfile.scala:92-111](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L92-L111)。

两个视图的区别很关键：

- `regd`：到上一拍为止「仍在写」的寄存器集合。派发器用它判断 RAW——若源寄存器在 `regd` 中为 1，说明它的最新值还没就绪，必须停拍。
- `comb`：`regd` 减去「本拍正在写回」的位。配合读端口的写转发（4.1.3），若生产者本拍就写回并转发，消费者可在 `comb` 视图下不停拍地紧随其后派发。

#### 4.2.3 源码精读：写契约与投机一致性

记分板有一段非常重要的注释，揭示了它和「错误路径上的投机指令」之间的契约：

```scala
// The write Addr:Data contract is against speculated opcodes. If an opcode
// is in the shadow of a taken branch it will still Set:Clr the scoreboard,
// but the actual write will be Masked.
```

见 [hdl/chisel/src/coralnpu/scalar/Regfile.scala:94-97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L94-L97)。含义是：分支预测错误时，错误路径上的指令**仍会 set/clr 记分板**，但它们对寄存器堆的真实写会被 `writeMask` 屏蔽掉（见 4.1.3 的 `!io.writeMask(j).valid`）。这样设计的目的是让记分板「自洽」——只要某条指令曾被派发，它的 set 一定有对应的 clr 配对，不会留下永久置位导致死锁；而错误路径不污染寄存器值。

最后还有两道断言守门：写端口冲突检测（任意两个写端口不能在同一拍写同一个非 0 寄存器）和记分板一致性检测（一个被 clr 的位必须在 `scoreboard` 里曾经被 set 过，否则说明清了一个根本没在写的寄存器——除非是调试模块直写）：

```scala
scoreboard_error := ((scoreboard & scoreboard_clr) =/= scoreboard_clr) && !dm_write_valid
assert(!scoreboard_error)
```

见 [hdl/chisel/src/coralnpu/scalar/Regfile.scala:230-233](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L230-L233)。

> **为何没有 WAR？** WAR（Write After Read）要求「先读后写」保序。但 CoralNPU 的执行单元是在派发的**下一拍**才读操作数，而派发本身是 in-order 的（u4-l4 的 `lastReady` 链）。也就是说，一条写指令不可能在它之前的读指令真正读到旧值之前就把新值写回——读动作总是先于同寄存器的后续写发生，所以记分板只需防 RAW（位被 set 时阻止读）和 WAW（位被 set 时阻止再 set 同一寄存器），不需要防 WAR。

#### 4.2.4 代码实践：预测记分板演化

1. **实践目标**：用源码逻辑推演一段连续指令的记分板位图变化。
2. **操作步骤**：
   - 假设流水线稳态，第 N 拍派发 `mul a5, a6, a7`（MLU，2 拍后写回）。在第 N 拍，`writeAddr(lane_k)` 标记 `a5` → `scoreboard_set` 第 5 位置 1。
   - 第 N 拍派发器试图紧接着派发 `add a4, a5, a0`（读 `a5`）。问：它能否与 `mul` 同组派发？
3. **需要观察的现象**：`a5` 在 `regd` 中为 1，触发 RAW 互锁，`add` 不能同组派发。
4. **预期结果**：`add` 必须等到第 N+2 拍 MLU 写回（`writeData(4)` 命中 `a5` → clr），且 `a5` 从记分板消失后才能派发；或借助读端口的写转发，在写回当拍用 `comb` 视图提前一拍。
5. **待本地验证**：在 cocotb 测试里对 `mul`/依赖 `add` 序列开 `--instr_trace`，观察 `add` 是否如预测那样延后派发。

#### 4.2.5 小练习与答案

**练习 1**：`io.scoreboard.comb` 比 `regd` 少了哪些位？为什么这能让背靠背依赖指令更快？
**答**：少了「本拍正在写回（clr）」的位。因为读端口有写转发，本拍写回的值能直接喂给同拍派发的消费者，所以 `comb` 视图允许消费者在写回当拍就派发，省一拍。

**练习 2**：错误路径上的投机指令把某位置了 set 却永远不写回真实值，记分板会死锁吗？
**答**：不会。投机指令虽被 `writeMask` 屏蔽真实写，但它的 clr 照常发生（见写契约），set 与 clr 仍配对，位最终会被清掉。

---

### 4.3 浮点寄存器堆 FRegfile

#### 4.3.1 概念说明

`FRegfile` 是 RV32F 的浮点寄存器堆，结构与整数寄存器堆同构：32 项（`floatRegCount = 32`），每项是 32 位单精度 `Fp32`。它同样自带一张浮点记分板，独立于整数记分板，供派发器判断浮点 RAW/WAW。端口数通过构造参数 `n_read`/`n_write` 可配。

#### 4.3.2 核心流程

在顶层 `SCore` 中实例化时，浮点读端口取 3、写端口取 2：

```scala
val floatReadPorts  = 3
val floatWritePorts = 2
val fRegfile = Option.when(p.enableFloat)(Module(new FRegfile(p, floatReadPorts, floatWritePorts)))
```

见 [hdl/chisel/src/coralnpu/scalar/SCore.scala:316-318](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L316-L318)。浮点运算集中由 `FloatCore`（u5-l4 详述）处理，每拍至多一条浮点指令（首槽），所以端口数远少于整数堆的 8 读 6 写。

记分板更新与整数堆完全同构：

```scala
val scoreboard_clr = io.write_ports.map(x =>
    Mux(x.valid, UIntToOH(x.addr, p.floatRegCount), 0.U(p.floatRegCount.W))).reduce(_|_)
scoreboard := (scoreboard & ~scoreboard_clr) | io.scoreboard_set
io.scoreboard := scoreboard
```

见 [hdl/chisel/src/coralnpu/scalar/FRegfile.scala:39-42](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FRegfile.scala#L39-L42)。读端口在无效时返回 `Fp32.Zero`（而非整数堆那样返回转发值），写端口则用 `PriorityMux` 选数据，并对「同一寄存器被多个写端口同时写」立即报 `exception`（比整数堆的延迟断言更严格）：

```scala
register_write_error(i) := PopCount(valid) > 1.U
// ...
io.exception := register_write_error.reduce(_|_)
```

见 [hdl/chisel/src/coralnpu/scalar/FRegfile.scala:50-59](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FRegfile.scala#L50-L59)。

#### 4.3.3 源码精读：读端口与 busPort

读逻辑非常简洁——有效则读、无效则给零：

```scala
for (i <- 0 until n_read) {
  val read_port = io.read_ports(i)
  read_port.data := Mux(read_port.valid, fregfile(read_port.addr), Fp32.Zero(false.B))
}
```

见 [hdl/chisel/src/coralnpu/scalar/FRegfile.scala:62-67](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FRegfile.scala#L62-L67)。另外浮点堆也提供一个 `busPort` 给 LSU 做浮点 load/store 的地址计算数据通路，并在 `n_read < 2` 时退化为 0（[FRegfile.scala:69-80](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FRegfile.scala#L69-L80)）。

> **与整数堆的对照**：两者都用「位图记分板 + set/clr」防 RAW/WAW；差异在于浮点堆端口少（3 读 2 写）、每拍派发一条、用 `Fp32` 类型而非 `UInt`、且写冲突直接产生 `exception` 输出。

#### 4.3.4 代码实践

1. **实践目标**：对比整数堆与浮点堆的记分板实现，确认它们是「同构」的。
2. **操作步骤**：把 [Regfile.scala:97-108](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L97-L108) 与 [FRegfile.scala:39-42](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FRegfile.scala#L39-L42) 并排阅读。
3. **需要观察的现象**：两者都是 `(scoreboard & ~clr) | set` 的同一套位运算。
4. **预期结果**：写出差异表——端口数（8/6 vs 3/2）、元素类型（UInt vs Fp32）、写冲突处理（延迟断言 vs 即时 exception）、x0 优化（有 vs 无）。
5. **待本地验证**：可选阅读 [FRegfileTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FRegfileTest.scala) 的断言，确认写冲突确实拉高 `exception`。

#### 4.3.5 小练习与答案

**练习**：浮点堆的记分板为什么需要独立，而不能复用整数堆那张？
**答**：浮点寄存器 `f0..f31` 与整数寄存器 `x0..x31` 是两套独立的名字空间，写 `f5` 不应阻塞读 `x5`。用独立的 32 位记分板才能分别追踪两套寄存器的待写状态。

---

### 4.4 CSR 文件与 csrrw/csrrs/csrrc 处理

#### 4.4.1 概念说明

`Csr` 模块是机器状态的「大本营」：它持有 `mstatus`、`mtvec`、`mepc`、`mcause`、`mie`/`mip`、`mcycle`/`minstret`、调试用 `dcsr`/`dpc`、向量用 `vstart`/`vl`/`vtype` 等几十个 CSR，并响应 `Zicsr` 的三条指令。它的接口很特别：**不走 ROB、不进执行单元流水线**，而是在 `io.req`（译码周期的 `Valid(CsrCmd)`）当拍就用组合逻辑完成「读旧值 → 算新值 → 写 CSR → 把旧值写回 rd」全流程。

#### 4.4.2 核心流程：CSR 指令的读改写

`CsrOp` 枚举定义了三种操作（[Interfaces.scala:238-242](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala#L238-L242)）：

```scala
object CsrOp extends ChiselEnum {
  val CSRRW = Value
  val CSRRS = Value
  val CSRRC = Value
}
```

一条 CSR 指令的三步是：

1. **读旧值 rdata**：按 `CsrAddress` 把指令里的 12 位 CSR 索引译成一组 one-hot 使能信号，再用 `MuxUpTo1H` 选出当前值；
2. **算新值 wdata**：根据 op 计算——`CSRRW` 直接用 rs1，`CSRRS` 是 `rdata | rs1`，`CSRRC` 是 `rdata & ~rs1`；
3. **写 CSR + 写回 rd**：把 wdata 写进被使能的 CSR 寄存器，同时把旧值 rdata 经 `io.rd` 送回整数寄存器堆的 rd。

第 2 步的新值计算是整张表的全部精髓：

```scala
val wdata = MuxLookup(req.bits.op, 0.U)(Seq(
    CsrOp.CSRRW -> rs1,
    CsrOp.CSRRS -> (rdata | rs1),
    CsrOp.CSRRC -> (rdata & ~rs1)
))
```

见 [hdl/chisel/src/coralnpu/scalar/Csr.scala:467-471](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L467-L471)。

> **立即数变体去哪了？** RISC-V 还有 `csrrwi/csrrsi/csrrci`（用 5 位立即数代替 rs1）。`Csr.scala` 里并没有专门处理它们——因为译码器把 5 位立即数 `immcsr` 经整数寄存器堆的 `readSet` 注入到了 `rs1` 数据线上（见 [Decode.scala:212](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L212) 的 `rs1Set = auipc || isCsrImm()` 与 [Decode.scala:761](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L761)）。所以 CSR 模块拿到的 `rs1` 对立即数变体而言就是零扩展后的立即数，三种 op 完全通用。

#### 4.4.3 源码精读：地址译码与「读而不写」

`CsrAddress` 枚举（[Csr.scala:42-97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L42-L97)）把每个支持的 CSR 地址列成常量。运行时用 `CsrAddress.safe(req.bits.index)` 把 12 位索引安全译码成 `(address, valid)`，再派生出几十个 `xxEn` 布尔使能（如 `fflagsEn`、`mtvecEn`、`mepcEn`），见 [Csr.scala:322-380](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L322-L380)。

读旧值用一张巨大的 `MuxUpTo1H` 把使能信号与对应寄存器值配对（节选）：

```scala
val rdata = MuxUpTo1H(0.U(32.W), Seq(
    fflagsEn    -> Cat(0.U(27.W), fflags),
    frmEn       -> Cat(0.U(29.W), frm),
    misaEn      -> misa,
    mieEn       -> mie,
    mtvecEn     -> mtvec,
    mepcEn      -> mepc,
    mcauseEn    -> mcause,
    // ... 以及向量、调试、计数器等 CSR
))
```

见 [hdl/chisel/src/coralnpu/scalar/Csr.scala:405-465](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L405-L465)。写入则是一串 `when (xxEn) { xx := wdata }`，例如：

```scala
when (req.valid) {
  when (mtvecEn)    { mtvec     := wdata }
  when (mepcEn)     { mepc      := wdata }
  when (mcauseEn)   { mcause    := wdata }
  // ...
}
```

见 [hdl/chisel/src/coralnpu/scalar/Csr.scala:473-500](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L473-L500)。

一个精巧的细节：**「纯读」语义**。当 `csrrs`/`csrrc` 的 rs1 为 0 时，新值与旧值相同（`rdata|0`、`rdata&~0`），等于不修改 CSR。模块用 `is_csr_write` 把这种情况排除掉，使其不计入写追踪与计数器更新：

```scala
val is_csr_write = req.valid && !(req.bits.op.isOneOf(CsrOp.CSRRS, CsrOp.CSRRC) && req.bits.rs1 === 0.U)
```

见 [hdl/chisel/src/coralnpu/scalar/Csr.scala:512](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L512)。注意 `req.bits.rs1` 在这里是**寄存器号**（不是数据），所以 `rs1 === 0` 判的是「源寄存器是 x0」，正对应汇编里 `csrr t0, mstatus`（即 `csrrs t0, mstatus, zero`）这种纯读。

最后，旧值写回 rd 的出口是：

```scala
io.rd.valid := req.valid
io.rd.bits.addr  := req.bits.addr
io.rd.bits.data  := rdata
```

见 [hdl/chisel/src/coralnpu/scalar/Csr.scala:637-639](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L637-L639)。这个 `io.rd` 在顶层被 OR 进 lane 0 的整数寄存器堆写端口（见 4.5.3）。

#### 4.4.4 代码实践：追踪一条 csrrw

1. **实践目标**：手动跟踪 `csrrw a0, mscratch, a1`（把 `mscratch` 旧值读到 `a0`，把 `a1` 写进 `mscratch`）在 `Csr.scala` 里的数据流。
2. **操作步骤**：
   - 译码器产出 `CsrCmd{addr=a0(10), index=0x340(mscratch), rs1=a1(11), op=CSRRW}`，经 [SCore.scala:196-203](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L196-L203) 的仲裁器送到 `csr.io.req`。
   - `rs1` 数据来自整数堆 lane0 读端口 `readData(0)`，即 `a1` 的值（[SCore.scala:208](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L208)）。
   - 在 [Csr.scala:322-380](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L322-L380) 找到 `mscratchEn`（`csr_address === CsrAddress.MSCRATCH`）。
   - 走 [Csr.scala:405-465](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L405-L465)：`rdata = mscratch`（旧值）。
   - 走 [Csr.scala:467-471](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L467-L471)：op=CSRRW → `wdata = rs1 = a1 的值`。
   - 走 [Csr.scala:482](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L482)：`mscratch := wdata`。
   - 走 [Csr.scala:637-639](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L637-L639)：`io.rd.data = rdata`（旧值）写回 `a0`。
3. **需要观察的现象**：旧 `mscratch` 进 `a0`、`a1` 进 `mscratch`，全过程在 `io.req` 有效的同一拍内组合完成。
4. **预期结果**：画出「rs1(a1) → wdata → mscratch」「mscratch(旧) → rdata → rd(a0)」两条数据流。
5. **待本地验证**：在 cocotb 里跑一段 `csrrw` 后读回 `mscratch` 与 `a0` 比对。

#### 4.4.5 小练习与答案

**练习 1**：`csrr t0, mcycle`（即 `csrrs t0, mcycle, zero`）会修改 `mcycle` 吗？
**答**：不会。op=CSRRS 且 rs1 寄存器号为 0（x0），`is_csr_write` 为假，且 `wdata = rdata | 0 = rdata`，写回等于不变；它只是把 `mcycle` 旧值读到 `t0`。

**练习 2**：`csrrwi`（立即数变体）在 `Csr.scala` 里为何看不到单独分支？
**答**：译码器把 5 位立即数经整数堆 `readSet` 注入到 `rs1` 数据线（`rs1Set` 含 `isCsrImm()`），CSR 模块拿到的 `rs1` 即零扩展立即数，故三种 op 的计算公式天然适用，无需特判。

---

### 4.5 CSR 的「仅首槽、当作控制流」约束

#### 4.5.1 概念说明

CSR 指令访问的是机器全局状态，改一个（如 `mtvec`、`mstatus`、`mie`）就可能改变后续所有指令的行为，还涉及异常/中断的精确性。因此 CoralNPU 对 CSR 指令施加了三重约束：**只能在 lane 0 派发**、**当周期独占派发（同组其他 lane 屏蔽）**、**必须等 ROB 排空**。这与 `fence`/`ebreak`/`wfi` 等同列，正是「当作控制流」处理的含义。

#### 4.5.2 核心流程：三重约束如何强制

约束 1——**首槽生成**：派发器只在 `i == 0` 时生成 CSR 命令，其他 lane 的 `csrFault` 恒为假：

```scala
if (i == 0) {
  // ... 组装 csr 命令，io.csr.valid := tryDispatch && csr.valid && ...
  io.csrFault(0) := csr.valid && !csr_address_valid && tryDispatch
} else {
  io.csrFault(i) := false.B
}
```

见 [hdl/chisel/src/coralnpu/scalar/Decode.scala:678-699](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L678-L699)。

约束 2——**当周期排他**：译码结果里 `forceSlot0Only()` 对 CSR 返回真：

```scala
def forceSlot0Only(): Bool = {
  isFency() || isCsr() || isFloat() || rvvReadsFloatRs1() || rvvWritesFrd()
}
```

见 [hdl/chisel/src/coralnpu/scalar/Decode.scala:167-169](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L167-L169)。它驱动 `slot0Interlock`——只要 lane 0 或 lane i 是 forceSlot0Only，lane i（i≠0）就被屏蔽：

```scala
val slot0Interlock = (0 until p.instructionLanes).map(i =>
  if (i == 0) { true.B }
  else { !decodedInsts(0).forceSlot0Only() && !decodedInsts(i).forceSlot0Only() }
)
```

见 [hdl/chisel/src/coralnpu/scalar/Decode.scala:398-404](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L398-L404)。也就是说，CSR 派发的那拍，同组其他 lane 全部停摆。

约束 3——**ROB 排空**：`canDispatch` 总闸里专门为 CSR 加了一条——CSR 指令必须等到退休缓冲（ROB）为空才能派发：

```scala
(!decodedInsts(i).isCsr() || io.retirement_buffer_empty) && // CSRs must wait for ROB to be empty
```

见 [hdl/chisel/src/coralnpu/scalar/Decode.scala:517](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L517)。这条是「精确性」的根本保证：因为 CSR 不进 ROB（它当拍就改机器状态），若不排空 ROB，此前已派发但未退休的指令就会落在「CSR 修改之后」的机器状态里，破坏精确异常与程序语义。

#### 4.5.3 源码精读：CSR 在顶层「占用」lane 0 的硬件证据

三重约束在顶层 `SCore` 有对应的硬件落点，证明 CSR 确实「霸占」了 lane 0 的全部数据通道：

- **rs1 读自 lane0 读端口**：`csr.io.rs1 := Mux(RegNext(dispatch.io.csr.valid, false.B), regfile.io.readData(0), dmRs1)`——见 [SCore.scala:208](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L208)（调试时改用 `dmRs1`）。
- **rd 写回经 lane0 写端口**：CSR 的 `io.rd` 被 OR 进 `regfile.io.writeData(0)`，并断言它与 lane0 的 ALU/BRU/RVV 写回互斥（当拍至多一个）：

```scala
val csr0Valid = if (i == 0) csr.io.rd.valid else false.B
// ...
regfile.io.writeData(i).valid := csr0Valid || alu(i).io.rd.valid || bru(i).io.rd.valid || rvvCoreRdValid
// ...
assert((csr0Valid +& alu(i).io.rd.valid +& bru(i).io.rd.valid) <= 1.U)
```

见 [hdl/chisel/src/coralnpu/scalar/SCore.scala:278-311](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L278-L311)。

- **异常/中断经 lane0 的 BRU**：只有 `bru(0)` 接到 `csr.io.bru`（trap 入口的 mcause/mepc/mtval、mode、中断都走它）——见 [SCore.scala:185](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L185)。
- **rd 标记也只在 lane0**：派发器里 `rdMark_valid` 对 CSR 只在 `i==0` 计入——见 [Decode.scala:774](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L774)。

把这些证据合起来看：CSR 的 rs1、rd、trap 入口三条通道全部绑死在 lane 0，硬件上根本不可能让 CSR 在其他 lane 执行——这就是「仅首槽」的物理根因。而「排空 ROB + 当周期独占」则保证它对机器状态的修改是严格按序、精确可见的，行为上等同于一条 `fence`/控制流指令。

#### 4.5.4 代码实践：解释「当作控制流」

1. **实践目标**：用自己的话讲清 CSR 为何被当作控制流处理，并能在源码里逐条指证。
2. **操作步骤**：
   - 读 [Decode.scala:165-169](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L165-L169)：注意 `forceSlot0Only` 把 `isCsr()` 与 `isFency()`（`fencei/ebreak/wfi/mpause`）并列。
   - 读 [Decode.scala:517](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L517) 与 [Decode.scala:398-404](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L398-L404)：CSR 既需 ROB 排空、又需当周期独占。
   - 读 [SCore.scala:185-208](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L185-L208) 与 [SCore.scala:278-311](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L278-L311)：确认 CSR 的 rs1/rd/trap 全绑 lane 0。
3. **需要观察的现象**：CSR 与 fence 类指令在「首槽+独占+排空」三方面约束完全相同。
4. **预期结果**：写出三句解释——① 副作用全局性：改 `mtvec`/`mstatus` 等影响后续所有指令，必须精确按序；② 不进 ROB：CSR 当拍改状态，故须排空 ROB 才能保证此前指令都已退休；③ 资源独占：rs1/rd/trap 绑死 lane 0，硬件上只能首槽执行。三者合起来即「当作控制流」。
5. **待本地验证**：可选——读 [Csr.scala:575-615](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L575-L615)，看 trap 入口如何保存 `mstatus`、`mret` 如何恢复，体会 CSR 与控制流的紧耦合。

#### 4.5.5 小练习与答案

**练习 1**：为何 CSR 必须等 ROB 排空，而普通 ALU 指令不用？
**答**：普通指令进 ROB、按序退休，状态改变被 ROB 序列化；CSR 不进 ROB、当拍直接改机器状态，若不排空 ROB，未退休的旧指令会落在 CSR 修改后的状态里，破坏精确性。

**练习 2**：如果允许 CSR 在 lane 2 派发，顶层哪条接线会先出错？
**答**：`csr.io.rs1` 写死读 `regfile.io.readData(0)`（lane0 的 rs1），`io.rd` 写死 OR 进 `writeData(0)`——lane 2 派发的 CSR 拿不到正确的 rs1、也写不进 lane 2 的 rd，硬件上根本不支持。

**练习 3**：`csrrs t0, mscratch, zero` 与 `csrrw t0, mscratch, t1` 在「是否独占当周期派发」上有区别吗？
**答**：没有。两者 `isCsr()` 都为真 → `forceSlot0Only()` 都为真 → 都触发首槽+独占+排空 ROB，与是否真正写 CSR 无关。

---

## 5. 综合实践

把本讲三个存储单元串起来，完成下面这个「端到端追踪」小任务。

**场景**：依次执行下面 4 条指令（假设从空流水线起步）：

```text
1. mul  a5, a6, a7      # MLU，2 拍后写回 a5
2. add  a4, a5, a0      # 读 a5（依赖第 1 条）
3. csrrw a3, mscratch, a4   # 把 mscratch 旧值读到 a3，a4 写进 mscratch
4. add  a2, a3, a0      # 读 a3（依赖第 3 条）
```

**任务**：

1. **记分板演化**（用 4.2 的规则）：画出从第 1 条派发起，`scoreboard` 中 `a5`、`a3` 两位在随后若干拍的 set/clr 时序。指出第 2 条 `add` 因 RAW 被记分板停拍、又在 MLU 写回当拍靠 `comb` 视图与读转发提前一拍解阻塞的过程。
2. **CSR 约束**（用 4.5 的规则）：说明第 3 条 `csrrw` 为何必须等到第 1、2 条都退休（ROB 排空）才能派发，并指出它当周期独占 lane 0、其 `a4` 读自 `readData(0)`、旧 `mscratch` 经 `writeData(0)` 写回 `a3`。
3. **数据流核对**（用 4.4 的规则）：在 [Csr.scala:467-471](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L467-L471) 与 [Csr.scala:637-639](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Csr.scala#L637-L639) 标出第 3 条的 `wdata`（=a4 值）与 `rdata`（=mscratch 旧值）各去了哪里。
4. **预期产物**：一张时序表 + 一段话，解释「整数堆记分板如何调度 RAW、CSR 如何作为控制流屏障插队、两者如何共用 lane 0 的读写端口而不冲突」。

**待本地验证**：若有仿真环境，把这 4 条指令编进一个 cocotb 测试，开 `--instr_trace`，比对 `a2`/`a3`/`a5`/`mscratch` 的最终值是否与你推演的一致。

## 6. 本讲小结

- 整数寄存器堆 `Regfile` 是 32 项、**8 读 6 写**端口的存储，端口数由 `instructionLanes=4` 决定：读 `2×4=8`，写 `4+2=6`（4 个 lane 端口 + MLU/DVU 与 LSU 两个延迟写回端口）。
- 它内含一张 32 位**记分板**，用 set（派发端 writeAddr）/clr（写回端 writeData）位运算防 RAW/WAW；因执行单元在派发下一拍才读操作数且派发按序，故天然无 WAR。投机指令照常 set/clr 但写被 `writeMask` 屏蔽，保证记分板自洽。
- 浮点堆 `FRegfile` 与整数堆同构（同样的位图记分板），但端口少（3 读 2 写）、用 `Fp32`、写冲突即时报 `exception`，是独立名字空间。
- `Csr` 模块用组合逻辑一拍完成 CSR 指令的「读旧值 rdata → 算新值 wdata（CSRRW/CSRRS/CSRRC）→ 写 CSR → 旧值写回 rd」，不进 ROB；立即数变体由译码器经 `readSet` 注入 rs1，故无需特判。
- CSR 指令被三重约束——`forceSlot0Only` 首槽独占、`retirement_buffer_empty` 排空 ROB、rs1/rd/trap 在顶层绑死 lane 0——使其行为等同 `fence`/控制流，保证对机器全局状态的修改精确且按序。

## 7. 下一步学习建议

- **u5-l4 浮点运算单元 FPU**：本讲只讲了浮点寄存器堆的存储与记分板，浮点运算（FMA、加减乘除、舍入）由 `Fpu`/`FloatCore`/`Fma` 完成，且 CSR 的 `frm`（舍入模式）字段如何喂给浮点单元正是下一讲的接口。
- **u9-l1 RISC-V Debug 模块**：本讲多次提到 `dm_write_valid`、`CoreDMIO`、调试模式 CSR（`dcsr`/`dpc`），调试模块如何经抽象命令读写这些寄存器堆与 CSR 是专门一讲的主题。
- **延伸阅读**：直接对照 [doc/microarch/dispatch.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/dispatch.md) 与 [doc/microarch/microarch.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/microarch.md)，把本讲的记分板/CSR 约束放回整核微架构图里理解。
