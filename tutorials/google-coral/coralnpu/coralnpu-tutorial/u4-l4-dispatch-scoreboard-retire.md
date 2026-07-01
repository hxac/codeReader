# 派发规则、记分板与退休

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 CoralNPU 标量核「**按序派发、乱序完成、按序退休**」这条主线，以及它为什么这样设计。
- 对照 `dispatch.md` 列出每周期各类指令（ALU/BRU/MLU/DVU/LSU）的派发上限，并能在源码里找到 enforcing 它们的真实机制。
- 解释记分板（scoreboard）如何用位掩码防止 RAW/WAW 冒险，并能回答「**为什么 CoralNPU 没有 WAR 冒险**」。
- 读懂 `RetirementBuffer.scala`，讲清楚一条 2 周期的 MLU 和一条 1 周期的 ALU 同周期派发后，它们的退休顺序与「按序提交」保证。
- 理解 `FaultManager` 如何把各种异常汇聚成一次精确异常（precise exception），并由 ROB 按序提交。

本讲是第 4 单元「标量核前端与流水线」的收口：取指（u4-l2）、译码（u4-l3）之后，指令终于要被「派发出去执行、再按序退休」。这一段决定了内核的性能上限与正确性边界。

## 2. 前置知识

本讲假设你已经掌握：

- **流水线与冒险**：处理器把一条指令切成多级，相邻指令可能因为数据依赖而互相等待，这种等待叫「冒险（hazard）」。
- **三类数据冒险**：
  - **RAW**（Read After Write，先写后读）：后一条要读前一条还没写完的结果——这是「真依赖」，必须等。
  - **WAW**（Write After Write，先写后写）：两条指令写同一个寄存器——写顺序不能乱，否则最终值错。
  - **WAR**（Write After Read，先读后写）：后一条要写前一条正在读的寄存器——如果写得太快，前一条会读到新值。
- **ready-valid 握手**：Chisel 里 `Decoupled` 接口用 `valid`（我有数据）+ `ready`（我接收）表示一次成功传输，二者同周期为真才算 `fire`。
- **Reorder Buffer（ROB，重排序缓冲）**：一块按程序顺序排列的表，每条指令派发时入队、执行完打标记、再从队首按序出队提交，从而实现「乱序执行、按序提交」。

> 一个直觉：CoralNPU 是个**不投机的顺序核**，它不需要复杂的乱序唤醒/选择逻辑，但仍允许「执行完的先后」与「程序顺序」不一致——靠的就是 ROB。本讲要回答的核心问题正是：**它怎么在不投机的前提下，安全地把执行顺序和提交顺序解耦？**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [doc/microarch/dispatch.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/dispatch.md) | 派发规则的「规格说明书」，用自然语言写清楚 in-order、冒险、执行单元约束、控制流屏障、特殊指令五条规则。 |
| [hdl/chisel/src/coralnpu/scalar/Decode.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala) | 译码 + 派发器。其中的 `DispatchV2` 类是本讲的真正主角——记分板与 `canDispatch` 总闸都在这里，`dispatch.md` 的每条规则都在此落地。 |
| [hdl/chisel/src/coralnpu/scalar/SCore.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala) | 标量核顶层，把派发器、各执行单元、寄存器堆、ROB、FaultManager 连成一体。从这里能看到「MLU/DVU 全核只有 1 个实例」的硬件事实。 |
| [hdl/chisel/src/coralnpu/RetirementBuffer.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala) | 重排序缓冲。跟踪每条已派发指令的完成状态，按序退休，并处理精确异常。 |
| [hdl/chisel/src/coralnpu/scalar/FaultManager.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FaultManager.scala) | 异常汇聚器。把译码/访存/取指/RVV 各路 fault 归并成一次 `mepc/mcause/mtval`，交给 ROB 按序提交。 |
| [hdl/chisel/src/coralnpu/scalar/Mlu.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala) | 乘法单元。内部用一个 `Arbiter` 把「每周期最多 1 条乘法」这个约束坐实。 |
| [hdl/chisel/src/coralnpu/Parameters.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala) | 全局参数：`instructionLanes = 4`、`retirementBufferSize = 8`，决定了「每周期派发 4 条、ROB 容量 8」的硬件规模。 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：派发总闸、记分板与冒险、执行单元约束与控制流屏障、ROB 与乱序退休、精确异常。

### 4.1 派发模型与 `canDispatch` 总闸

#### 4.1.1 概念说明

CoralNPU 标量核每周期从取指缓冲拿到最多 `instructionLanes = 4` 条已译码指令，但「**拿到**」不等于「**能派发**」。派发器（`DispatchV2`）要对每一条指令做一连串检查：核有没有 halt？有没有数据冒险？执行单元收不收？ROB 有没有空位？……只有全部通过，这条指令才真正 `fire`，被送往对应执行单元。

「按序派发（in-order dispatch）」是总纲：[doc/microarch/dispatch.md:7-10](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/dispatch.md#L7-L10) 明确写——如果地址 n 的指令本周期不能派发，那么 n+4 也不予考虑。也就是说，4 条指令是「从低地址到高地址」依次判断，遇到第一条不能派的就停，后面不再看。这条规则由 `lastReady` 链在硬件里实现。

#### 4.1.2 核心流程

每个周期，`DispatchV2` 对 4 个 lane 做如下处理（伪代码）：

```
lastReady[0] = true                         # lane 0 之前没有指令挡路
for i in 0..3:
    canDispatch[i] = (一长串互锁条件的与)     # 见 4.1.3
    tryDispatch[i] = lastReady[i] && canDispatch[i]
    # 把 tryDispatch 翻译成各执行单元的命令（ALU/BRU/MLU/DVU/LSU/CSR/RVV/Float）
    fired[i]   = (某个单元真的接收了)          # io.alu(i).fire || io.bru(i).fire || ...
    lastReady[i+1] = fired[i]                 # 只有本 lane 发出去了，下一 lane 才有机会
io.inst[i].ready = lastReady[i+1]
```

关键点：`lastReady` 是一条**顺序传播的链**——lane i 只有在前一个 lane `fired` 之后才会被尝试；一旦某 lane 没派出去，`lastReady[i+1]` 变假，后面所有 lane 都被屏蔽。这正是「按序」二字的硬件实现，也是后续「乱序完成」得以安全进行的前提（派发顺序 = 程序顺序）。

#### 4.1.3 源码精读

`canDispatch` 是把 `dispatch.md` 全部规则编码进一个布尔表达式的「总闸」，见 [hdl/chisel/src/coralnpu/scalar/Decode.scala:497-520](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L497-L520)：

```scala
val canDispatch = (0 until p.instructionLanes).map(i =>
    !io.halted &&          // 核未 halt
    !io.interlock &&       // 未被 interlock（如 fence）
    io.inst(i).valid &&    // 取指缓冲有指令
    !jumped(i) &&          // 本周期前面已有跳转，控制流屏障（4.3）
    !readAfterWrite(i) &&  // RAW 冒险（4.2）
    !writeAfterWrite(i) && // WAW 冒险（4.2）
    !floatReadAfterWrite(i) &&  !floatWriteAfterWrite(i) && // 浮点同款
    !branchInterlock(i) && // 分支后只能跟 ALU/BRU
    !fence(i) &&           // fence 屏障
    slot0Interlock(i) &&   // 特殊指令只能出 slot0
    rvvConfigInterlock(i) && rvvVstartInterlock(i) &&
    lsuInterlock(i) && rvvInterlock(i) &&   // 队列容量反压（4.3）
    !undefInterlock(i) &&
    (i.U < io.retirement_buffer_nSpace) &&               // ROB 有空位
    !io.retirement_buffer_trap_pending &&                // 没有未处理 trap
    (!decodedInsts(i).isCsr() || io.retirement_buffer_empty) && // CSR 等 ROB 清空
    singleStepInterlock(i) && mpauseInterlock(i)
)
```

而 `lastReady` 链与 `tryDispatch` 见 [Decode.scala:525-528](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L525-L528)：

```scala
val lastReady = Wire(Vec(p.instructionLanes + 1, Bool()))
lastReady(0) := true.B
for (i <- 0 until p.instructionLanes) {
  val tryDispatch = lastReady(i) && canDispatch(i)
  ...
```

每个 lane 的 `fired` 收集见 [Decode.scala:724-728](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L724-L728)：把 ALU/BRU/MLU/DVU/LSU（以及 slot0 的 CSR、RVV、Float）的 `fire`「或」起来，任一成立即认为本 lane 已派发，于是 `lastReady(i+1) := dispatched.reduce(_||_)`。

#### 4.1.4 代码实践

**目标**：亲手确认「按序派发」在硬件里的体现，并理解 `canDispatch` 里每一项对应 `dispatch.md` 的哪条规则。

**步骤**：
1. 打开 [Decode.scala:497-520](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L497-L520)，给 `canDispatch` 的每一行注释上它对应 `dispatch.md` 的哪一节（In-order / Hazard / Execution Unit / Control Flow / Special）。
2. 打开 [Decode.scala:525-533](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L525-L533)，跟踪 `lastReady` 如何从 lane 0 传到 lane 3。
3. 思考：如果 lane 1 的指令因为 RAW 不能派发，lane 2 的一条无冒险 ALU 会被派发吗？

**预期结果**：lane 1 的 `fired` 为假 → `lastReady(2)` 为假 → lane 2 即使 `canDispatch(2)` 为真，`tryDispatch(2)` 也为假，**不会被派发**。这就是「按序」的代价：后面更好的指令也得等前面卡住的。**待本地验证**：可在 cocotb 里构造一段 `mul` 后紧跟一条依赖它的 `add`，再用 `--instr_trace` 观察派发停顿。

#### 4.1.5 小练习与答案

**练习 1**：`canDispatch` 里为什么要有 `i.U < io.retirement_buffer_nSpace` 这一项？
**答案**：ROB 容量有限（`retirementBufferSize = 8`）。派发一条指令就要在 ROB 里占一个表项；如果 ROB 满了还派发，就会丢失「这条指令还没退休」的记录，无法保证按序提交。`nSpace` 是 ROB 当前剩余空位数，派发量不得超过它。

**练习 2**：`lastReady(0)` 为什么恒为 `true.B`？能不能把它接成 `!io.halted`？
**答案**：lane 0 之前没有更早的指令挡路，所以链的起点恒真；halt 已经在 `canDispatch(0)` 的 `!io.halted` 里处理了，无需在链起点重复。注释里作者也留了 `// TODO(derekjchow): Set to halted?`，说明这是有意为之的简化。

---

### 4.2 记分板：RAW/WAW 与「无 WAR」

#### 4.2.1 概念说明

[doc/microarch/dispatch.md:12-17](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/dispatch.md#L12-L17) 写得很直白：

> CoralNPU uses scoreboarding to track dependencies across instructions. This prevents RAW and WAW data hazards. All execution units read their operands from the register file **the cycle after** the instructions are dispatched. Therefore, **WAR hazard never occurs**.

记分板（scoreboard）是一张「**哪些寄存器正在被等待写回**」的位掩码表：第 i 位为 1，表示有一个已派发但尚未写回的操作要写 `x[i]`。派发新指令时，若它要读/写的寄存器在表里命中，就停下。

**为什么没有 WAR？** 这是 CoralNPU 一个精妙的时间点设计：执行单元**在派发的下一拍才读寄存器堆**。所以「读」永远发生在「写标记置位」之后——后一条指令不可能在前一条还没读完时就抢先写进去，WAR 自然不存在。于是记分板只需防 RAW 和 WAW 两类。

#### 4.2.2 核心流程

记分板用「**累加扫描（scan）**」在同一周期内传递依赖信息：

```
对 4 个 lane：
  rdScoreboard[i] = 本 lane 要写的 rd 的 one-hot 掩码
  scoreboardScan  = rdScoreboard 的前缀或（lane i 看到的是 lane 0..i-1 写过的并集）
  regd[i] = scoreboardScan[i] | io.scoreboard.regd   # 叠加上一拍遗留的「未写回」位
  comb[i] = scoreboardScan[i] | io.scoreboard.comb   # comb 带写回转发

  RAW : (本 lane 要读的寄存器掩码) & (regd 或 comb) != 0   → 停
  WAW : (本 lane 要写的 rd 掩码)   & comb             != 0   → 停
```

这里有两个来源：`io.scoreboard.regd` 来自寄存器堆的寄存器端口（**本拍即可读**），`io.scoreboard.comb` 还包含**写回转发（write forwarding）**——即本拍正好写回的值也能被同拍读到。派发器在 [Decode.scala:343-344](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L343-L344) 把扫描结果与这两个来源相或。

#### 4.2.3 源码精读

写掩码与扫描，[Decode.scala:331-344](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L331-L344)：

```scala
val rdAddr = io.inst.map(_.bits.inst(11,7))           // rd 字段
val writesRd = decodedInsts.map(d => (!d.isScalarStore() && !d.isCondBr()) || ...)
val rdScoreboard = (0 until p.instructionLanes).map(i =>
    Mux(writesRd(i), UIntToOH(rdAddr(i), p.scalarRegCount), 0.U(p.scalarRegCount.W)))
val scoreboardScan = rdScoreboard.scan(0.U(p.scalarRegCount.W))(_ | _)
val regd =  scoreboardScan.map(_ | io.scoreboard.regd)
val comb =  scoreboardScan.map(_ | io.scoreboard.comb)
```

注意 `writesRd` 把**条件分支**和**store**排除在外——分支不写 rd，store 的 rd 是「store 完成哨兵」而非通用寄存器，所以不计入标量写掩码。

RAW 与 WAW 的最终判定，[Decode.scala:359-363](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L359-L363)：

```scala
val readAfterWrite = (0 until p.instructionLanes).map(i =>
    (readScoreboardRegd(i) & regd(i)) =/= 0.U(32.W) ||
    (readScoreboardComb(i) & comb(i)) =/= 0.U(32.W))
val writeAfterWrite = (0 until p.instructionLanes).map(i =>
    (rdScoreboard(i) & comb(i)) =/= 0.U(32.W))
```

- `readAfterWrite`（RAW）：本 lane 要读的寄存器命中了「待写回」集合 → 停。
- `writeAfterWrite`（WAW）：本 lane 要写的 rd 命中了 `comb`（含同周期前序 lane 的写）→ 停，保证写顺序。
- 注意 WAW 只比对 `comb`（含转发）而不额外比对 `regd`，因为同周期内的写顺序由 `scan` + `comb` 已经覆盖；跨周期的 WAW 由 `regd` 中的遗留位体现，并经 `comb` 路径一并捕获。

`readScoreboardComb`/`readScoreboardRegd` 的构建见 [Decode.scala:347-357](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L347-L357)：只有 `jalr`、`lsu`、`store` 这类**真正需要从寄存器堆读地址**的指令才计入读掩码（`usesRs1Regd`/`usesRs2Regd`），其余读操作走 `comb`。

> 浮点寄存器堆有一套完全平行的记分板（`fcomb`/`floatReadAfterWrite`/`floatWriteAfterWrite`），见 [Decode.scala:367-386](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L367-L386)，逻辑与标量同构，不再赘述。

#### 4.2.4 代码实践

**目标**：用一段汇编理解 RAW 如何在派发阶段制造停顿。

**步骤**：
1. 阅读上面的 [Decode.scala:359-363](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L359-L363)。
2. 设想指令序列（同周期 4 条）：`mul a5,a4,a3`（lane0）、`add a6,a5,a2`（lane1，依赖 a5）。
3. 推演：lane0 写 a5 → `rdScoreboard[0]` 的 a5 位置 1 → `scoreboardScan[1]` 含 a5 → lane1 的 `readScoreboardComb[1]`（读 a5）与之相与非零 → `readAfterWrite[1] = true` → lane1 被停。
4. **预期结果**：lane1 本周期不派发，下一周期（a5 已写回、记分板清位）才派发。**待本地验证**：可用 `tests/cocotb` 下任一含乘后立即用的 ISA 测试，配合指令轨迹确认。

#### 4.2.5 小练习与答案

**练习 1**：既然执行单元「下一拍才读操作数」消除了 WAR，为什么不能同样消除 RAW？
**答案**：RAW 是真数据依赖——后一条指令需要前一条的**结果**。即使读操作发生在派发后一拍，前一条（比如 2 周期的 MLU）那时还没把结果写回寄存器堆，后一条会读到旧值。WAR 不同，它关心的是「写不能抢在读之前」，而读固定在派发后一拍、写更靠后，时序上天然安全。

**练习 2**：`regd` 和 `comb` 的区别是什么？为什么 RAW 判定里两个都要查？
**答案**：`regd` 是寄存器堆里**已寄存**的「待写回」位（本拍即可从总线端口读出）；`comb` 还叠加了**本拍正在写回的转发**。一条依赖指令的源数据可能来自更早的未写回操作（查 `regd`），也可能正好来自本拍写回（查 `comb`），所以两者都要相与。

---

### 4.3 执行单元约束与控制流屏障

#### 4.3.1 概念说明

[doc/microarch/dispatch.md:19-41](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/dispatch.md#L19-L41) 给出三类约束：

1. **执行单元数量约束**：ALU/BRU 每条 lane 各配一个（够用），但全核**只有 1 个 MLU**、**1 个 DVU**，因此每周期乘/除指令有上限；非流水化的 DVU 还会主动反压。
2. **控制流屏障**：保守起见，遇到 `jal/jalr/ebreak/ecall/mret/wfi` 就不再派发其后的指令（同一周期也不再派后续）。
3. **特殊指令**：会改变核状态（PC/寄存器堆之外）的指令（`csrrw/csrrs/csrrc/ebreak/ecall/mret/fence/fenci/wfi`）只能从 **slot 0** 发出，且通常当作控制流指令，独占一个周期。

#### 4.3.2 核心流程

各类指令的「每周期上限」与 enforcing 机制对照：

| 指令类 | 每周期上限 | enforcing 机制（源码位置） |
| --- | --- | --- |
| ALU / BRU | 各 4 条（每 lane 一个） | 每 lane 一个 `Alu`/`Bru` 实例，`tryDispatch` 直接驱动 |
| MLU（乘） | **1 条** | 全核 1 个 `Mlu`，**内部 `Arbiter` 每 cycle 只授权 1 路** |
| DVU（除） | **1 条，且只能 slot 0** | 全核 1 个 `Dvu`，SCore **只连 `dvu(0)`**，lane 1–3 `.ready := false` |
| LSU（访存） | **1 条**（文档） | `lsuInterlock` 基于 `io.lsuQueueCapacity` 计数反压 + LSU 单元 ready |

控制流屏障与特殊指令在 `canDispatch` 里体现为 `!jumped(i)`、`!branchInterlock(i)`、`slot0Interlock(i)`、`!fence(i)` 等项。

#### 4.3.3 源码精读

**「全核只有 1 个 MLU / 1 个 DVU」的硬件事实**，在 SCore 顶层实例化处见 [SCore.scala:94-97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L94-L97)：

```scala
val alu = Seq.fill(p.instructionLanes)(Alu(p))   // 4 个 ALU
val bru = (0 until p.instructionLanes).map(...).reduce(_ ++ _)  // 4 个 BRU
val mlu = Mlu(p)                                  // 仅 1 个
val dvu = Dvu(p)                                  // 仅 1 个
```

**MLU 的「每周期 1 条」由内部仲裁器坐实**，[Mlu.scala:67-68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L67-L68)：

```scala
val arb = Module(new Arbiter(new MluCmd(p), p.instructionLanes))
arb.io.in <> io.req
```

虽然派发器给 4 个 lane 都准备了 `io.mlu(i)` 请求端口（[SCore.scala:248-252](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L248-L252) 把它们逐一接到 MLU 的 `req`），但 `Arbiter` 每 cycle 只会 `chosen` 一路、其余路的 `ready` 为假，于是至多 1 条乘法真正 `fire`。被挡下的乘法指令因为 `lastReady` 链，也会让其后 lane 停下。

**DVU 的「只能 slot 0」更直接**，[SCore.scala:256-264](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L256-L264)：

```scala
dvu.io.req <> dispatch.io.dvu(0)        // 只接 lane0
dvu.io.rd.ready := !mlu.io.rd.valid
for (i <- 1 until p.instructionLanes) {
  dispatch.io.dvu(i).ready := false.B   // lane1..3 的 DVU 永远不 ready
}
```

派发侧的 MLU/DVU 命令选择见 [Decode.scala:610-630](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L610-L630)，用 `SafeMuxUpTo1H` 把译码位翻成 `MluOp`/`DvuOp` 枚举；`io.mlu(i).valid := tryDispatch && mlu.valid` 决定是否真的发出请求。

**控制流屏障**：`jumped` 是「本周期前面是否已出现跳转」的前缀或，见 [Decode.scala:313-318](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L313-L318)；`canDispatch` 里的 `!jumped(i)` 保证跳转之后的指令本周期不派。`slot0Interlock` 见 [Decode.scala:398-404](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L398-L404)：lane0 恒允许，其余 lane 仅当前面没有 `forceSlot0Only()` 指令、且自己也不是 `forceSlot0Only()` 时才允许。

#### 4.3.4 代码实践

**目标**：把 `dispatch.md` 的「每周期上限」逐一在源码里坐实。

**步骤**：
1. 在 [SCore.scala:94-97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L94-L97) 数一下 ALU/BRU/MLU/DVU 各实例化了几个。
2. 在 [Mlu.scala:67-68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Mlu.scala#L67-L68) 解释为什么有 4 个请求口却只能发 1 条。
3. 在 [SCore.scala:256-264](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L256-L264) 解释 DVU 为何「只能 slot 0」。

**预期结果**：ALU=4、BRU=4（够每 lane 一个，故无上限）；MLU 虽有 4 个请求口但 `Arbiter` 每 cycle 仅授权 1 路 → 每周期至多 1 条乘法；DVU 物理上只接 lane0，其余 lane `.ready=false` → 每周期至多 1 条除法且必须在 slot0；LSU 由 `lsuInterlock`（容量计数）+ LSU 单元 ready 反压，文档限定每周期 1 条访存。

#### 4.3.5 小练习与答案

**练习 1**：为什么 MLU 要在**单元内部**用 `Arbiter` 限制，而不是在派发器里像 LSU 那样用计数 interlock？
**答案**：两种实现都可行。MLU 选择「内部仲裁」是因为乘法是 3 级流水（见 u5-l2），仲裁后单条进入流水即可，结构简单；而 LSU 涉及队列深度、scatter/gather、外部总线未完成事务等更复杂的状态，用基于 `queueCapacity` 的计数反压更自然。设计上是「能简单就简单」的取舍。

**练习 2**：`csrrw` 为什么被限定为「只能 slot 0、且当作控制流」？
**答案**：CSR 写会改变核的全局状态（如中断使能、向量配置），且 CSR 单元全核只有 1 个端口（`io.csr` 只在 `i==0` 时驱动，见 [Decode.scala:678-699](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L678-L699)）。把它当控制流、独占一周期，可避免同一周期内其他指令基于旧 CSR 值执行而造成不可恢复的副作用，也便于精确异常处理。

---

### 4.4 RetirementBuffer：乱序完成、按序退休

#### 4.4.1 概念说明

派发是按序的，但**执行完成是乱序的**：一条 1 周期的 ALU 会比同周期派发的 2 周期 MLU 先出结果。如果直接把结果写回并对外可见，程序顺序就被破坏了，异常也会变得不精确。

`RetirementBuffer`（ROB）就是解决这个问题的「收尾者」。它在类注释里写得很清楚，见 [RetirementBuffer.scala:42-51](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L42-L51)：每条指令经历 **Dispatched → Completed → Retired** 三态，退休时「**当它和它之前所有指令都已完成**」才从队首出队。这正是「in-order dispatch, out-of-order retire」的实现。

#### 4.4.2 核心流程

ROB 由两个并列的环形结构组成：

```
instBuffer (CircularBufferMulti, 深度 8):  派发时入队，存指令元信息（addr/idx/trap/...）
resultBuffer (Vec(8), 寄存器):            跟踪每个槽的完成状态 (dataDone / cfDone / trap)

每周期：
  1. 入队：把本周期 fire 的指令按 lane 顺序写入 instBuffer 头部
  2. 更新 resultBuffer：对每个槽，检查
       - dataReady = 数据已写回 / 无需写回 / store 已完成
       - cfReady   = 控制流已确认（非 CF 指令恒真）
       - 标记 dataDone、cfDone；检测 trap
  3. 计算可退休数：
       countValid = 从队首起连续 (dataDone && cfDone) 的槽数
       若有 trap：limit = 首个 trap 槽 +1，deqReady = min(limit, countValid)
       否则      ：deqReady = countValid
  4. 出队：退休 deqReady 条；若有 trap 则退休到 trap 槽为止并 flush 整表
  5. 左移 resultBuffer，把未退休项对齐到队首
```

可退休数的数学表达：

\[
\text{countValid} = \max\{\,k \;\big|\; \forall\, i<k,\; \text{ready}[i]=1\,\}
\]

即「队首起最长的连续 ready 前缀长度」。这正是 `Cto(...)`（count-trailing-ones）所算的。

#### 4.4.3 源码精读

两个核心存储，[RetirementBuffer.scala:78-80](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L78-L80) 与 [RetirementBuffer.scala:249](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L249)：

```scala
val instBuffer = Module(new CircularBufferMulti(new Instruction, bufferSize, bufferSize))
...
val resultBuffer = RegInit(VecInit(Seq.fill(bufferSize)(MakeInvalid(new InstructionUpdate))))
```

每个槽的完成判定 `dataReady`，[RetirementBuffer.scala:344](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L344)：

```scala
val dataReady = (scalarWriteIdxMap.reduce(_|_) || floatWriteIdxMap.reduce(_|_) ||
                 vectorReady || nonWritingInstr ||
                 (storeInstr && storeComplete.valid && storeComplete.bits === bufferEntry.addr))
```

它把「写回端口命中」「无需写回（如分支）」「store 完成」统合成一个布尔。`cfReady` 见 [RetirementBuffer.scala:376](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L376)：`val cfReady = !isControlFlow || nextAddrValid`——非控制流指令恒 ready，控制流指令要等下一条地址可见以核验控制流连续性（`linkOk`）。

退休数量的核心计算，[RetirementBuffer.scala:423-435](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L423-L435)：

```scala
val hasTrap = resultUpdate.map(x => x.valid && x.bits.trap).reduce(_||_)
val trapDetected = VecInit(resultUpdate.map(x => x.valid && x.bits.trap))
val firstTrapIdx = PriorityEncoder(trapDetected)
val countValid = Cto(VecInit(resultUpdate.map(x => x.valid && x.bits.cfDone)).asUInt)

val limit = firstTrapIdx + 1.U
val trapReadyToRetire = hasTrap && (limit <= countValid)
val deqReady = Mux(trapReadyToRetire, limit, countValid)

instBuffer.io.deqReady := deqReady
val trapRetired = trapReadyToRetire
instBuffer.io.flush := trapRetired
```

注意 `countValid` 用的是 `valid && cfDone`——一个槽既要数据完成（`valid`）又要控制流确认（`cfDone`）才算 ready。`deqReady` 取「连续 ready 前缀」与「首个 trap 槽」的较小值，保证**trap 之后的指令绝不退休**。退休后，`resultBuffer` 左移对齐并在 trap 时清空，[RetirementBuffer.scala:447-449](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L447-L449)：

```scala
resultBuffer := Mux(trapRetired,
    VecInit(Seq.fill(bufferSize)(MakeInvalid(new InstructionUpdate))),
    ShiftVectorRight(resultUpdate, deqReady))
```

最终对外报告的退休数 `nRetired` 还要扣掉 `ecall`，因为 ecall 走单独的异常路径，[RetirementBuffer.scala:451-452](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L451-L452)。

> **mini 模式**：`RetirementBuffer` 有一个 `mini` 参数（SCore 里 `mini = !p.useRetirementBuffer`，见 [SCore.scala:64](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L64)）。mini 模式丢弃指令位与结果数据以省面积（`if (mini) instr.inst := 0.U`），仍保留按序退休逻辑；完整 ROB 模式（`enableVerification = true`）才保留指令位用于产生精确的指令轨迹。详见 [Parameters.scala:75-99](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L75-L99) 的注释。

#### 4.4.4 代码实践

**目标**：解释「一条 MLU（2 周期）与一条 ALU（1 周期）同周期派发」后的退休顺序。

**步骤**：
1. 假设两个场景，分别推演 `countValid` 与 `deqReady`：
   - **场景 A**：lane0 = ALU（程序在前），lane1 = MLU（在后）。
   - **场景 B**：lane0 = MLU（程序在前），lane1 = ALU（在后）。
2. 用 [RetirementBuffer.scala:344](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L344)（dataReady）、[:426](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L426)（countValid）推演每拍的 ready 位图。

**推演与预期结果**：
- **场景 A**（ALU 在前）：派发后第 1 拍，槽0（ALU）dataDone=1、槽1（MLU）=0 → `countValid = 1` → 仅 ALU 退休；第 2 拍 MLU done → MLU 退休。顺序：ALU、MLU，与程序序一致。
- **场景 B**（MLU 在前）：第 1 拍，槽0（MLU）=0、槽1（ALU）=1 → 队首不 ready → `countValid = 0` → **本拍无人退休**，尽管 ALU 已完成；第 2 拍 MLU done → 槽0、槽1 都 ready → `countValid = 2` → **两条同拍退休**，顺序仍是 MLU、ALU。

**结论**：无论执行谁先完成，**退休顺序永远等于派发顺序（= 程序顺序）**。ALU 即使先算完，也会在 ROB 里「等」前面的 MLU，直到二者一起按序出队。这就是「乱序完成、按序退休」的精确含义。**待本地验证**：可在 cocotb 里跑一段 `mul` 紧跟 `add` 的序列，用指令轨迹核对退休顺序。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `countValid` 用 `Cto`（连续前缀）而不是「统计所有 ready 槽的总数」？
**答案**：因为退休必须按序。即便槽2、槽3 已 ready，只要槽1 未完成，槽2、槽3 就不能先退休——否则程序顺序被破坏。连续前缀正好表示「从队首起、可以连续出队多少条」，是按序提交的正确度量。

**练习 2**：`trapRetired` 时为什么要 `flush` 整个 `instBuffer` 与 `resultBuffer`？
**答案**：trap 表示某条指令触发了异常，需要精确地「停在它身上」并交给异常处理。它之后的指令本来就不该执行（已派发但未退休），必须全部丢弃；它本身则作为最后一条退休指令提交，随后 CSR 把 PC 改向异常向量。flush 保证异常后 ROB 干净，从处理程序重新取指。

---

### 4.5 精确异常与 FaultManager 的协作

#### 4.5.1 概念说明

「精确异常（precise exception）」要求：当一条指令出错时，**它之前的所有指令都已完整执行，它之后的指令都像没执行过一样**。CoralNPU 没有投机，本来就容易做到精确，但异常来源五花八门——译码期就发现的非法指令、访存时的对齐/访问错误、取指错误、RVV 后端的 trap……需要一个汇聚点。

`FaultManager` 就是这个汇聚点。它把所有来源归并成**一次** `Valid(FaultManagerOutput)`，其中带好 `mepc`（出错 PC）、`mcause`（原因码）、`mtval`（附加信息）、`decode`（是否译码期 fault）四个字段，然后交给 ROB：ROB 把对应指令标记为 trap，按序退休到它为止，再 flush。这样，无论异常在流水线哪一阶段被发现，最终都**在 ROB 队首按序提交**，保证精确性。

#### 4.5.2 核心流程

```
各来源 → FaultManager.in:
  译码期 fault (csr/jal/jalr/bxx/undef/rvv)   每 lane 一个布尔
  memory_fault (load/store 对齐/访问错误)      Valid
  fetchFault (取指错误)                        Valid
  rvv_fault (向量后端 trap)                    Optional Valid
        ↓ 归并
  out.valid = 任一来源有效
  out.bits.mepc/mcause/mtval/decode = 按优先级 MuxCase 选取
        ↓ 喂给 ROB
  RetirementBuffer.io.fault:
  - decode 类 fault → 在派发时作为 trap 项入队
  - 访存/fetch 类 fault → 命中已派发的对应槽，置 trap
        ↓
  ROB 按序退休到 trap 槽，flush，触发 CSR 异常处理
```

#### 4.5.3 源码精读

FaultManager 把每 lane 的多个 fault 位「或」成一个总 fault 向量，再 `PriorityEncoder` 找首个，[FaultManager.scala:51-59](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FaultManager.scala#L51-L59)：

```scala
val faults = VecInit((0 until p.instructionLanes).map(x => (
    io.in.fault(x).csr | io.in.fault(x).jal | io.in.fault(x).jalr |
    io.in.fault(x).bxx | io.in.fault(x).undef | io.in.fault(x).rvv.getOrElse(false.B))))
val fault = faults.reduce(_|_)
val first_fault = PriorityEncoder(faults)
```

输出有效与各字段的优先级选取，[FaultManager.scala:77-84](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FaultManager.scala#L77-L84)：

```scala
io.out.valid := fault || instr_access_fault || load_fault || store_fault || rvv_fault
io.out.bits.mepc := MuxCase(0.U(p.programCounterBits.W), Seq(
    load_fault -> io.in.memory_fault.bits.epc,
    store_fault -> io.in.memory_fault.bits.epc,
    rvv_fault -> io.in.rvv_fault.map(_.bits.mepc).getOrElse(0.U),
    fault -> io.in.pc(first_fault).pc,
    instr_access_fault -> io.in.fetchFault.bits,
))
```

`mcause` 按 RISC-V 规范填标准码（load fault=5、store fault=7、非法指令=2、指令访问=1、断点/跳转目标错=0），见 [FaultManager.scala:92-103](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FaultManager.scala#L92-L103)；`decode` 标志区分「译码期就发现的 fault」（见 [:116-123](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FaultManager.scala#L116-L123)）。

ROB 侧消费这个 fault：`decodeFaultValid` 把译码期 fault 在派发时直接作为 trap 项入队，`noFire0Fault` 处理 lane0 因故未 fire 但确实 fault 的情况，[RetirementBuffer.scala:87-89](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L87-L89)。SCore 把 FaultManager 输出接到 ROB，[SCore.scala:87](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L87)：`rob_io.fault := fault_manager.io.out`。随后 ROB 用 4.4 节的 `firstTrapIdx`/`trapReadyToRetire` 机制，精确地退休到这条指令并 flush。

#### 4.5.4 代码实践

**目标**：跟踪一次「非法指令」异常从被发现到被精确提交的全链路。

**步骤**：
1. 在 [Decode.scala:466-469](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L466-L469) 找到 `undef` fault 如何只在 slot0 产生。
2. 在 [FaultManager.scala:51-103](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/FaultManager.scala#L51-L103) 跟踪它如何变成 `mcause=2`（非法指令）、`decode=true`。
3. 在 [RetirementBuffer.scala:87-89](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L87-L89) 与 [:423-435](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L423-L435) 跟踪它如何被标记为 trap 并按序退休。

**预期结果**：非法指令在 slot0 被识别 → FaultManager 报 `mcause=2, decode=true` → ROB 把它作为 trap 项入队 → 当它到达队首（前面都退休完）时 `trapReadyToRetire` 成立 → 退休到它为止并 flush → CSR 把 `mepc` 存好、PC 跳向异常向量。**待本地验证**：`tests/cocotb` 下应有非法指令异常的回归用例可对照。

#### 4.5.5 小练习与答案

**练习 1**：为什么访存 fault（load/store）不设 `decode=true`？
**答案**：访存 fault 是在**执行/访存阶段**才发现的（地址对齐、访问权限），此时指令已经派发并在 ROB 里占了槽。所以它不需要在派发时入队，而是事后用 `bufferEntry.addr === faultPc` 命中已派发的那个槽并置 trap（见 [RetirementBuffer.scala:291](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L291)）。`decode=true` 只用于「译码期就知道错」的情况。

**练习 2**：如果一条 load 在 ROB 里排在一条很慢的 MLU 之后出错，异常会立刻生效吗？
**答案**：不会立刻生效。load fault 会把对应槽标 trap，但要等它之前的 MLU（以及更早指令）全部按序退休、使 load 槽升到队首时，`trapReadyToRetire`（`limit <= countValid`）才成立，异常才被「提交」。这正是精确异常的体现——之前的指令完整执行，之后的被 flush。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个**源码阅读 + 推演**任务（无需运行硬件，重在理解）：

**任务背景**：你的同事声称「CoralNPU 既然是顺序核，就不需要 ROB，直接把结果写回寄存器堆即可」。请你用本讲学到的知识**反驳**他，并给出一个具体的指令序列作为证据。

**操作步骤**：

1. **构造序列**（4 条，按程序顺序）：
   - `I0`: `mul  a5, a4, a3`  （MLU，2 周期，slot 任意但只能 1 条/周期）
   - `I1`: `add  a6, a0, a0`  （ALU，1 周期）
   - `I2`: `lw   a7, 0(a5)`   （LSU，依赖 I0 的 a5，且多周期）
   - `I3`: `add  a8, a1, a1`  （ALU，1 周期）

2. **派发分析**：用 4.1–4.3 的规则判断这 4 条能否在**同一周期**全部派发。提示：
   - I0 是唯一的 MLU，受 Mlu 仲裁（1 条/周期）✓。
   - I2 读 a5，与 I0 形成 RAW（4.2）→ I2 **不能**与 I0 同周期派发。
   - I2 是访存，受 LSU 每周期 1 条限制。
   - 结论：I0、I1 可同周期派（I1 无依赖）；I2、I3 要等下一周期（且 I2 要等 a5 写回）。

3. **退休分析**：假设 I0、I1 同周期派发进入 ROB 槽 0、1。用 4.4 的结论回答：
   - 第 1 拍谁先完成？谁先退休？（答：I1 先完成，但因 I0 在槽0 未完成，`countValid=0`，**无人退休**。）
   - 第 2 拍发生了什么？（答：I0 完成，槽0、1 均 ready，`countValid=2`，I0、I1 **同拍按序退休**。）
   - 如果没有 ROB、直接写回，会出什么问题？（答：I1 会先于 I0 体现在架构状态里，破坏程序顺序；一旦 I0 后续触发异常，就无法实现「I0 之前的都完成、之后的都撤销」的精确异常。）

4. **核验**：把上述推演与 [RetirementBuffer.scala:423-435](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/RetirementBuffer.scala#L423-L435) 的 `countValid`/`deqReady` 逐行对照，确认你的 ready 位图与代码一致。

**预期产出**：一段文字 + 一张「拍 × 槽 ready 位图」的小表，能清晰说明「即使顺序核也需要 ROB 来解耦完成顺序与提交顺序，并支撑精确异常」。

## 6. 本讲小结

- CoralNPU 标量核是**按序派发、乱序完成、按序退休**：派发顺序 = 程序顺序（`lastReady` 链保证），但执行完成先后不限，最终由 ROB 按序提交。
- `canDispatch` 是把 `dispatch.md` 全部规则编码进一个布尔表达式的总闸；`lastReady` 链把「按序」落地——前一条没派出去，后一条就没机会。
- 记分板用位掩码防 RAW/WAW；因为执行单元在派发**下一拍**才读操作数，**WAR 天然不存在**，所以无需防范。
- 执行单元约束：ALU/BRU 每 lane 一个（无上限）；MLU 全核 1 个、内部 `Arbiter` 每 cycle 仅授权 1 条；DVU 全核 1 个且**只接 slot0**；LSU 每周期 1 条（文档约定，经 `lsuInterlock` + 单元 ready 反压）。
- `RetirementBuffer` 用 `instBuffer`（环形）+ `resultBuffer`（完成状态）跟踪每条指令；`countValid` 取队首连续 ready 前缀，`deqReady = min(countValid, 首个trap槽)`，保证乱序完成下仍按序提交。
- `FaultManager` 把译码/访存/取指/RVV 各路异常归并成一次 `mepc/mcause/mtval`，交 ROB 在队首按序提交，实现**精确异常**。

## 7. 下一步学习建议

本讲把「前端 → 派发 → 退休」的标量核数据通路讲完了。接下来建议：

- **u5-l1 / u5-l2**：进入执行单元本体——读 `Alu.scala`/`Bru.scala` 看单周期执行，读 `Mlu.scala` 的 3 级流水与 `Dvu.scala`/`IDiv.scala` 的可变延迟除法，把本讲「2 周期 MLU、可变延迟 DVU」的延迟来源看透。
- **u5-l3**：读 `Regfile.scala`/`Csr.scala`，理解记分板的 `regd`/`comb` 信号在寄存器堆侧如何产生、写回转发如何实现。
- **u9-l1（Debug 模块）**：本讲提到的 `single_step`/`mpause`/精确异常，正是 Debug 模块实现 halt/resume/single-step 的基础，学完异常再读 Debug 会非常顺。
- 若对「精确异常后的 CSR 行为」感兴趣，可先跳读 `Csr.scala` 中 `mepc`/`mcause`/`mtval` 与异常向量的写入逻辑，再回头看本讲 4.5 的全链路。
