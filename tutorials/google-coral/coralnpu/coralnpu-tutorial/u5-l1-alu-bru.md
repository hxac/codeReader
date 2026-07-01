# ALU 与 BRU 执行单元

## 1. 本讲目标

经过第 4 单元的学习，我们已经知道一条指令在 CoralNPU 标量核里如何被**取指 → 译码 → 派发**。本讲把视线推进到流水线的最后一站——**执行（Execute）阶段**，集中讲两个最基础、最高频的执行单元：

- **ALU（算术逻辑单元）**：负责 `add / sub / xor / sll ...` 这类「算一算、当周期出结果」的运算。
- **BRU（分支单元）**：负责 `jal / jalr / bge / ebreak ...` 这类「改变控制流」的指令，同时兼任异常与中断的提交点。

学完本讲，你应当能够：

1. 说清 ALU 如何用一张 `MuxLookup` 选通表实现全部运算，以及它为何是「1 周期延迟」。
2. 说清 BRU 如何计算 `jal / jalr` 的目标地址、如何用 `fwd` 字段把「分支预测」和「实际跳转」解耦，以及它如何充当派发屏障。
3. 说清执行单元与**派发（Dispatch）**、**寄存器堆（Regfile）**之间的 ready-valid 握手契约与时序。

## 2. 前置知识

在进入源码前，先统一几个本讲反复出现的概念：

- **三级流水线与指令延迟**：CoralNPU 标量核是「取指 → 译码/派发 → 执行/写回」三级、每周期最多派发 4 条指令的 in-order 流水线。不同执行单元延迟不同：ALU、CSR、BRU 都是 1 周期，MLU 是 2 周期，DVU 可变，LSU 2 周期以上。详见 [microarch.md:23-30](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/microarch.md#L23-L30)（这张延迟表是本讲「1 周期」结论的依据）。

- **lane（指令槽）**：派发器每周期把最多 4 条指令排成 4 个「槽」（`instructionLanes = 4`，见 [Parameters.scala:73](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L73)）。每个槽各挂一个 ALU、一个 BRU，所以 ALU/BRU 是「每槽一份」。

- **ready-valid（Decoupled / Valid）握手**：Chisel 里表示「这一拍数据有效」的标准做法是配一个 `valid` 布尔位。本讲的执行单元大量使用 `Valid(...)` 包裹命令与结果，`valid=1` 表示「本拍请使用我提供的数据」。

- **写回屏障（write mask）**：当某条分支「实际跳转」后，排在它后面的同周期指令属于错误路径，其写回必须被屏蔽。这是 BRU 影响派发的关键，后文细讲。

如果你还没读过 **u4-l4（派发规则、记分板与退休）**，建议先读：本讲的「派发屏障」「分支冲刷」正是在 u4-l4 的 in-order 派发模型之上展开的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/chisel/src/coralnpu/scalar/Alu.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala) | ALU 执行单元：定义全部运算操作码与结果选通表。 |
| [hdl/chisel/src/coralnpu/scalar/Bru.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala) | BRU 执行单元：分支判定、目标地址计算、异常/中断提交。 |
| [hdl/chisel/src/coralnpu/scalar/AluTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/AluTest.scala) | ALU 的 ChiselSim 单元测试，是理解运算语义的「黄金参考」。 |
| [hdl/chisel/src/coralnpu/scalar/SCore.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala) | 标量核顶层：实例化 ALU/BRU、把它们与派发器和寄存器堆接线。 |
| [hdl/chisel/src/coralnpu/Interfaces.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala) | 公共 IO bundle：`RegfileReadDataIO`、`BranchTakenIO` 等握手契约。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先讲执行单元在流水线中的「位置与握手契约」（4.1），再分别精读 ALU（4.2）和 BRU（4.3）。

### 4.1 执行单元的位置与握手契约

#### 4.1.1 概念说明

ALU 和 BRU 都属于**执行阶段**的单元。一条指令的生命周期是：

1. **取指**（Fetch）：从 ITCM 取出 32 位指令。
2. **译码/派发**（Decode/Dispatch）：译码器把指令「翻译」成一束控制信号，派发器判断它能否本周期发出；若能，就把对应的命令（`AluCmd` / `BruCmd`）送到目标执行单元的 `req` 端口。
3. **执行/写回**（Execute/Writeback）：执行单元从寄存器堆读操作数、算出结果，并在同一周期把结果写回寄存器堆。

理解 ALU/BRU 的关键，是先把这套「派发 → 执行单元 → 寄存器堆」的三方握手契约看清楚。这个契约对 ALU、BRU、MLU、LSU 都是统一的。

#### 4.1.2 核心流程

ALU 与 BRU 的对外接口（IO）几乎是同构的，都包含四类信号：

```
        ┌──────────── 派发器 ────────────┐
        │  dispatch.io.alu(i) / bru(i)   │   Valid(AluCmd/BruCmd)
        └───────────────┬────────────────┘
                        │ req（译码周期送来命令）
                        ▼
                 ┌──────────────┐
   rs1, rs2 ───►│   ALU / BRU  │─── rd（结果，写回寄存器堆）
   （寄存器堆    │  （执行周期）  │
    读端口）     └──────────────┘
```

- `req`：派发器在**译码周期**送来的命令（`Valid(new AluCmd(p))` / `Valid(new BruCmd(p))`），含操作码与目标寄存器号。
- `rs1` / `rs2`：寄存器堆的**读端口数据**，在**执行周期**送来操作数（`RegfileReadDataIO`，带 `valid` + `data`）。
- `rd`：执行单元的**结果**（`Valid(Flipped(new RegfileWriteDataIO(p)))`），含写回地址与数据。

注意「译码周期」与「执行周期」相差一拍：派发器在周期 N 把命令锁进执行单元，执行单元在周期 N+1 用寄存器堆送来的操作数算结果。这正是「1 周期延迟」的来源——不是 ALU 内部有多级流水，而是它跨在两级流水之间。

#### 4.1.3 源码精读

先看公共握手 bundle 的定义。读端口与写端口都是「带 valid 的数据」：

[Interfaces.scala:202-215](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala#L202-L215) 定义了 `RegfileReadDataIO`（`valid` + `data`）和 `RegfileWriteDataIO`（`addr` + `data`）。读端口的 `valid` 由寄存器堆在**执行周期**置位（见 [Regfile.scala:120-123](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L120-L123) 中 `readDataReady` 是寄存器），这也是「操作数晚一拍到达」的实现原因。

再看顶层如何实例化与接线。SCore 在每个 lane 各放一个 ALU、一个 BRU：

[SCore.scala:94-95](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L94-L95) —— `val alu = Seq.fill(p.instructionLanes)(Alu(p))` 与 `val bru = ... Bru(p, x == 0)`。注意 BRU 有个 `first` 参数：只有 lane 0 的 BRU 是 `first=true`，独占了 CSR、异常、中断等「特权」，其余 lane 的 BRU 只处理普通分支。这是 u4-l4 里「控制流类指令只能进首槽」的硬件落点。

ALU 的接线（命令来自派发、操作数来自寄存器堆、结果送回写回仲裁）：

[SCore.scala:167-171](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L167-L171) —— `alu(i).io.req := dispatch.io.alu(i)`，`rs1/rs2` 接到 `regfile.io.readData(2*i)` 与 `readData(2*i+1)`。每个 lane 占用 2 个读端口（一个给 rs1、一个给 rs2），所以 4 lane = 8 读端口。

写回仲裁在 [SCore.scala:288-312](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L288-L312)：同一 lane 里 CSR、ALU、BRU、RVV 的写回被 `MuxOR` 合并到一个写端口，并用断言保证「同一周期同一 lane 最多一个写回」（`alu.rd.valid +& bru.rd.valid <= 1`）。这说明 ALU 与 BRU **共享** lane 的写回槽——一条指令要么走 ALU、要么走 BRU，不会同时写。

> 小结：执行单元的契约是「**周期 N 收命令、周期 N+1 用操作数出结果并写回**」。理解了这套握手，下面 ALU 和 BRU 内部就只是在「周期 N+1」里做不同的事。

#### 4.1.4 代码实践

1. **实践目标**：在顶层验证「命令→操作数→结果」的跨拍时序。
2. **操作步骤**：
   - 打开 [SCore.scala:94-95](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L94-L95)，确认 ALU/BRU 各有 4 份实例。
   - 顺着 [SCore.scala:167-171](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L167-L171) 把 `dispatch.io.alu(0) → alu(0).io.req → regfile.io.readData(0/1) → alu(0).io.rd` 这条链路在纸上画出来。
   - 注意 `readData` 是**寄存器输出**（[Regfile.scala:116-117](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L116-L117) 的 `readDataReady` / `readDataBits` 都是 `RegInit`）。
3. **需要观察的现象**：`req` 在译码周期有效，`rs1/rs2.valid` 与 `rd.valid` 都推迟一拍。
4. **预期结果**：你能解释为何 ALU/BRU 的「1 周期延迟」实际来自「命令锁存一拍 + 操作数晚一拍」，而非单元内部有额外流水级。
5. 本实践为源码阅读型，不产生可执行产物。

#### 4.1.5 小练习与答案

- **练习**：为什么 4 个 lane 需要 8 个寄存器堆读端口？BRU 的 `first` 参数为什么只有 lane 0 为真？
- **答案**：每条指令有两个源操作数 rs1、rs2，4 lane 就是 8 个读端口（见 SCore 的 `2*i` / `2*i+1`）。`first=true` 让 lane 0 独占 CSR 接口、异常与中断提交，把 `ebreak/ecall/mret/...` 这类特权指令约束在首槽，避免多 lane 并发提交异常造成歧义。

---

### 4.2 ALU：算术逻辑单元

#### 4.2.1 概念说明

ALU 是最「纯粹」的执行单元：输入两个操作数、一个操作码，组合逻辑算出结果，下一拍写回。它没有状态机、没有握手反压（永远 ready），也不参与控制流。

CoralNPU 的 ALU 同时支持两套指令集扩展：

- **RV32IM 基础运算**：`ADD / SUB / SLT / SLTU / XOR / OR / AND / SLL / SRL / SRA / LUI`。
- **ZBB 位操作扩展**：`ANDN / ORN / XNOR / CLZ / CTZ / CPOP / MAX[U] / MIN[U] / SEXT[B/H] / ROL / ROR / ORCB / REV8 / ZEXTH`。

ZBB 是 RISC-V「位操作」标准扩展之一，CLZ/CTZ/CPOP 这类「数位」运算对 ML 与编译器后端很有用，CoralNPU 把它们直接做进 ALU。

#### 4.2.2 核心流程

ALU 的核心可以用一句话概括：**一张大 Mux 选通表**。流程是：

```
周期 N（译码）：req.valid=1，锁存 op 与 addr 到内部寄存器
周期 N+1（执行）：
   rs1, rs2 = 寄存器堆读端口送来的操作数
   shamt    = rs2 的低 5 位（移位量，rv32 只用低 5 位）
   data     = MuxLookup(op, 0)( ...按 op 选运算... )
   rd.valid := 1 ; rd.bits.data := data ; rd.bits.addr := addr
```

关键设计细节：

- **延迟 1 周期**：`valid` 是 `RegInit`，在 `req` 下一拍才拉高（见源码注释 "Pulse the cycle after the decoded request"）。
- **保持输出不抖动**：`addr/op` 只在 `req.valid` 时更新，其余周期保持上一次的值，避免组合毛刺——这是低功耗设计的常见手法。
- **移位量取模**：`shamt = rs2(log2Ceil(xlen)-1, 0)`，即 rv32 取 rs2 低 5 位，自动满足「移位量对 32 取模」的语义。

signed/unsigned 比较的辅助信号也提前算好：`r2IsGreater = rs1.asSInt < rs2.asSInt`（有符号）、`r2IsGreaterU = rs1 < rs2`（无符号），供 SLT/SLTU 与 MAX/MIN 复用。

#### 4.2.3 源码精读

**操作码枚举**：[Alu.scala:30-61](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L30-L61) 用 `ChiselEnum` 列出全部 28 个运算。`ChiselEnum` 会自动给每个值分配一段独热编码，是 Chisel 里定义「一类操作」的标准写法。注意分两段注释 `// RV32IM` 与 `// ZBB`，正好对应上面两套扩展。

**命令 bundle**：[Alu.scala:63-66](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L63-L66) 定义 `AluCmd`，只有 `addr`（写回寄存器号）和 `op`（操作码）。派发器送来的就是这两个字段。

**1 周期延迟的锁存逻辑**：[Alu.scala:79-91](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L79-L91)。`valid := io.req.valid`（寄存器，下一拍生效），`when(io.req.valid) { addr := ...; op := ... }`。注释明确写了「Pulse the cycle after the decoded request」和「Avoid output toggles by not updating state between uses」——这就是 4.2.2 里说的两点设计。

**结果选通表（全 ALU 的灵魂）**：[Alu.scala:119-149](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L119-L149)。`MuxLookup(op, 0.U)(...)` 是一个「以 `op` 为键、查表选结果」的组合 mux。逐条看几个要点：

- `ADD -> (rs1 + rs2)`：`add / addi / auipc` 都走这一格——`addi` 的立即数在派发端被注入 rs2，`auipc` 的 PC 被注入 rs1，差异由「操作数来源」吸收（详见 u4-l3）。
- `SUB -> (rs1 - rs2)`：减法。
- `SLT -> r2IsGreater`、`SLTU -> r2IsGreaterU`：set-less-than 直接复用 4.2.2 的比较信号。
- `SLL -> ((rs1 << shamt)(xlen-1,0))`、`SRL / SRA`：移位用 `shamt`（rs2 低 5 位）；`SRA` 先 `asSInt` 再算术右移，保证符号位扩展。
- `LUI -> rs2`：`lui` 的立即数从 rs2 走进来，所以结果就是 rs2（不需要 rs1，下面断言会放行它）。
- ZBB 部分按字面含义实现，例如 `CLZ -> Clz(rs1)`、`CPOP -> PopCount(rs1)`、`ROL/ROR -> rs1.rotateLeft/Right(shamt)`、`ORCB / REV8` 用辅助函数 `Orcb` 与 `Cat(UIntToVec(rs1, 8))` 实现字节级处理。

**断言（语义校验）**：[Alu.scala:153-159](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L153-L159)。`rs1Only` 列出「只用到 rs1、不用 rs2」的单目运算（CLZ/CTZ/CPOP/SEXT*/ORCB/REV8/ZEXTH）。两条断言的含义是：

- `LUI` 不要求 rs1 有效（它只用 rs2）；
- `rs1Only` 中的运算不要求 rs2 有效；
- 其余运算必须 rs1、rs2 都有效，否则触发仿真断言失败。

这其实是 ALU 对派发器提出的「操作数依赖」契约：派发器只有当所需操作数就绪（记分板清位）时才会发命令，所以正常运行下断言永不为真。

#### 4.2.4 代码实践

1. **实践目标**：列出 ALU 全部运算并验证语义。
2. **操作步骤**：
   - 打开 [Alu.scala:30-61](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L30-L61)，把 28 个操作码分成「RV32IM / ZBB」两组抄成一张表，并在 [Alu.scala:119-149](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L119-L149) 里给每个 op 标注它的算式。
   - 打开 [AluTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/AluTest.scala)，挑一个测试（例如 `"CLZ"`，[AluTest.scala:92-112](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/AluTest.scala#L92-L112)），读懂它如何 `poke` rs1、`step()` 一拍、再 `peek` `rd.bits.data` 与期望值比较。
3. **需要观察的现象**：注意测试里 `poke req → step() → peek rd` 的节奏正好是「一拍命令、一拍结果」，对应 4.1 的握手时序。
4. **预期结果**：你整理出的运算表应与选通表一一对应；能解释 `CLZ(0x00800000) = 8`（前导零个数）这类用例。
5. 若要实际跑测试，命令为（**待本地验证**，需 Bazel 与 ChiselSim 环境）：

   ```bash
   bazel test //hdl/chisel/src/coralnpu/scalar:AluTest
   ```

#### 4.2.5 小练习与答案

- **练习 1**：`SLT` 与 `SLTU` 在选通表里分别用 `r2IsGreater` 和 `r2IsGreaterU`，二者有何区别？为什么 `LUI` 的结果直接等于 `rs2`？
- **答案**：`SLT` 是有符号比较（先 `asSInt`），`SLTU` 是无符号比较。`LUI` 的立即数在派发端被放进 rs2 通道送达 ALU，所以 ALU 只需把 rs2 原样输出，不需要 rs1（断言里 `LUI` 被豁免 rs1 有效性检查）。
- **练习 2**：为什么移位用 `shamt = rs2(4,0)` 而不是整个 rs2？
- **答案**：RV32 规定移位量对 32 取模，等价于只取 rs2 低 5 位。这样 `sll x1, x2, x3` 中 `x3=32` 与 `x3=0` 移位效果相同，符合 ISA 语义。

---

### 4.3 BRU：分支与控制流单元

#### 4.3.1 概念说明

BRU 比 ALU 复杂得多，因为它身兼数职：

1. **分支判定**：条件分支 `beq/bne/blt/bge/bltu/bgeu` 要比较 rs1、rs2 决定跳不跳。
2. **无条件跳转**：`jal`（PC 相对）、`jalr`（寄存器间接），还要把返回地址（pc+4）写回 rd。
3. **特权与控制流**：`ebreak/ecall/mret/wfi/mpause`，以及异常（FAULT）和中断。
4. **派发屏障**：一旦某槽的分支「需要重定向取指」，同周期排在它后面的指令都要被冲刷。

BRU 用一个关键设计把「分支预测」和「实际跳转」解耦：每个分支命令带一个 `fwd`（forward）位，表示「取指端的静态预测器**已经**把 PC 跳到了目标」。这样 BRU 只需判断「预测是否正确」，预测正确就不打扰取指，预测错误才发出重定向。

#### 4.3.2 核心流程

BRU 的时序与 ALU 一样跨在「译码/执行」两级之间，但内部多了一个 `stateReg`（把命令再锁一拍）来承载执行周期的状态：

```
周期 N（译码）：req 送来 {fwd, op, pc, target, link, inst}
              → 组合算出 nextState，锁进 stateReg
周期 N+1（执行）：
   rs1, rs2 = 寄存器堆操作数
   比较：eq/lt/ltu ... = rs1 与 rs2 的关系
   实际跳转 actually_taken = 按 op 决定（jal/jalr 恒真；beq→eq；bge→ge ...）
   重定向 taken.valid = (实际结果 =/= 预测 fwd)   ← 预测错误才重定向
   目标 real_target / taken.value = 跳转目标地址
   链接 rd = pc+4（仅 jal/jalr 写回返回地址）
```

**目标地址的计算**分两类：

- **`jal`（PC 相对）**：目标 `= pc + immjal`。这个加法在**译码端**就算好了（见 [Decode.scala:587-588](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L587-L588) 的 `bru_target = addr + immjal`），通过 `req.bits.target` 传进来。BRU 只是验证/重定向。
- **`jalr`（寄存器间接）**：目标 `= (rs1 & ~1)`，即 rs1 的值并把最低位清零（RISC-V 规范要求跳转目标地址最低位清零）。rs1 在执行周期才从寄存器堆读到，所以 `jalr` 目标在执行周期才算定。

**链接寄存器（link）**：`jal/jalr` 要把「下一条指令地址」`pc+4` 写回 rd（`linkData := pc4De`），供函数返回使用。

**派发屏障**：BRU 的 `taken.valid`（重定向信号）在顶层被 OR 起来送给派发器（[SCore.scala:100](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L100) 的 `branchTaken`）。一旦某槽重定向，排在后面的槽被屏蔽写回（`writeMask`），下一周期取指被引到新目标——这就是「分支是指令组的终点」。

#### 4.3.3 源码精读

**操作码与命令**：[Bru.scala:27-51](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L27-L51)。`BruOp` 枚举覆盖三类：跳转（JAL/JALR）、条件分支（BEQ..BGEU）、特权/控制流（EBREAK/ECALL/MPAUSE/MRET/WFI/FAULT）。`BruCmd` 比 `AluCmd` 丰富得多，含 `fwd`（预测位）、`pc`、`target`（译码算好的目标）、`link`（链接寄存器号）、`inst`（原始编码，供异常报告）。

**pc+4 与目标候选**：[Bru.scala:111-144](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L111-L144)。`pc4De = pc + instructionBits/8 = pc + 4`。`nextState.target` 用 `MuxCase` 在三个候选里选：

- 故障（`fault_manager_valid`）→ 跳到 `mtvec`（异常向量）；
- `fwd` 为真（取指已预测跳转）→ 目标设为 `pc4De`（用于「预测跳了但其实不该跳」时退回顺序 PC）；
- `JALR` → `(io.target.data & ~1)`（rs1 值清低位）；
- 否则 → 译码端算好的 `pipeline0Target`（普通分支/jal 目标，首槽还特判了 ecall→mtvec、mret→mepc、wfi→pc4De）。

`linkData := pc4De`、`linkValid` 仅在 JAL/JALR 且 `link /== 0` 时成立（[Bru.scala:116-117](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L116-L117)）——即「写 x0 不算写」。

**分支比较**：[Bru.scala:155-160](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L155-L160)。`eq/neq/lt/ge/ltu/geu` 一组比较，`lt` 用 `asSInt`（有符号），`ltu` 用裸 `<`（无符号）。这组信号既喂给「实际跳转」也喂给「重定向判定」。

**实际跳转（actually_taken）**：[Bru.scala:190-200](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L190-L200)，`isTaken` 按 op 选：JAL/JALR 恒真、BEQ→eq、BGE→ge …。`pipeline0Taken` 单独处理首槽特权指令（ECALL/MRET/WFI 取真、EBREAK/MPAUSE 取假）与中断。这个 `actually_taken` 与预测无关，是「这条分支客观上跳不跳」。

**重定向判定（taken.valid）——本模块最关键的一段**：[Bru.scala:202-221](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L202-L221)。看 JAL 这行：

```scala
BruOp.JAL -> (true.B =/= stateReg.bits.fwd)
```

含义是「JAL 客观必跳；要不要重定向取指，取决于预测有没有提前跳」。展开成真值：

| 预测 fwd | 客观结果 | `taken.valid = 结果 =/= fwd` | 动作 |
| --- | --- | --- | --- |
| 0（未预测跳） | JAL 必跳 | 1 | 重定向到 JAL 目标 |
| 1（已预测跳） | JAL 必跳 | 0 | 不打扰取指（预测正确） |

条件分支同理，`BEQ -> (eq =/= fwd)` 表示「预测与实际不一致时才重定向」。`io.taken.value` 给出重定向目标（`Mux(interrupt_taken, mtvec, stateReg.bits.target)`）。`real_target`（[Bru.scala:214-218](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L214-L218)）则给出「无论预测如何，客观目标是什么」，专门供 CSR 单步调试用（`actually_taken` + `real_target` 见 [SCore.scala:210-215](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L210-L215) 算 `nextInstPC`）。

**链接写回**：[Bru.scala:223-225](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L223-L225)，`rd.valid := stateReg.valid && linkValid`，写回 `pc+4`。这就是 `jal ra, ...` 后 `ra` 等于返回地址的来源。

**首槽的异常/中断提交**：[Bru.scala:227-277](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L227-L277)（`if (first)`）。只有 lane 0 的 BRU 才执行这段，它把 ECALL/EBREAK/中断/故障翻译成对 CSR 的写入：`mepc`（异常返回 PC）、`mcause`（原因码，例如 ECALL=11）、`mtval`、`halt`、`fault`。注意 `mcause` 里 EBREAK 被映射为 `24+1`（自定义 usage fault），落在 RISC-V 保留编码区——这与 u1-l1 提到的「CoralNPU 用自定义异常码」一致。这段还产生 `interlock`（[Bru.scala:228-231](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L228-L231)）：遇到 EBREAK/ECALL/MPAUSE/MRET/FAULT 时拉高，通知派发器停拍。

**派发屏障的顶层实现**：BRU 的 `taken.valid` 在顶层被两处复用——

- [SCore.scala:136](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L136) 把 `branchTaken` 送进 `dispatch.io.branchTaken`，派发器据此冲刷后续 lane；
- [SCore.scala:424-427](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L424-L427) 用 `scan` 算累积 OR 得到 `writeMask`：第 i 槽之前的任意 BRU 重定向，都会屏蔽第 i 槽的寄存器堆写回。这正是 [Regfile.scala:94-96](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L94-L96) 注释说的「在已跳转分支阴影里的指令仍会动记分板，但实际写回被 Mask」。

#### 4.3.4 代码实践

1. **实践目标**：厘清 `jal/jalr` 目标计算与「分支屏障」。
2. **操作步骤**：
   - 在 [Bru.scala:111-144](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L111-L144) 标出 `pc4De`、`nextState.target` 的三条候选分支，说明 `jal` 目标来自哪里、`jalr` 目标为什么用 `io.target.data & ~1`。
   - 对照 [Decode.scala:587-605](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Decode.scala#L587-L605)，确认 `fwd = io.inst.bits.brchFwd`（来自取指的预测）与 `target = addr + immjal/immbr`（译码端算好）。
   - 在 [SCore.scala:424-427](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L424-L427) 旁注一句：「lane i 之前任意分支重定向 → lane i 写回被屏蔽」。
3. **需要观察的现象**：思考这样一个场景——同一周期 lane0 是 `beq`（预测不跳、实际跳了），lane1 是一条 `add`。lane0 的 `taken.valid=1` 会怎样影响 lane1？
4. **预期结果**：lane1 的 `add` 命令虽已发出、记分板也已动，但因为 `writeMask(1)=taken(0)=1`，它的寄存器堆写回被屏蔽，等于「作废」。你能用 `scan` 的累积 OR 推出这个结论。
5. 本实践为源码阅读型，结论可手工推导，**无需运行**即应得出。

#### 4.3.5 小练习与答案

- **练习 1**：`fwd` 字段表示什么？为什么 `JAL` 的 `taken.valid = (true.B =/= fwd)` 而不是恒为真？
- **答案**：`fwd` 表示取指端的静态预测器已把 PC 跳到目标（来自 `brchFwd`）。JAL 客观必跳，但若预测已经跳了（`fwd=1`），取指方向已对，无需再重定向；只有预测没跳（`fwd=0`）时才需要把取指引到 JAL 目标。把「实际结果」与「预测」异或，正好得到「是否需要纠正」。
- **练习 2**：为什么 `jalr` 的目标在执行周期才算定，而 `jal` 在译码周期就能算好？
- **答案**：`jal` 是 PC 相对，`pc + immjal` 只依赖当前 PC 与立即数，译码端就能算。`jalr` 是 `rs1 + imm`，依赖寄存器 rs1 的值，而 rs1 要到执行周期才从寄存器堆读出，所以必须等执行周期（见 `io.target.data` 来自 [Regfile.scala:201-204](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Regfile.scala#L201-L204) 的 `io.target(i).data := busAddr(i)`）。
- **练习 3**：EBREAK 为什么会被映射成 `mcause = 24+1` 而不是标准值？
- **答案**：CoralNPU 把 EBREAK 当作「usage fault」用自定义原因码上报，落在 RISC-V `mcause` 编码空间的保留区（见 [Bru.scala:253-261](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L253-L261)），避免与标准异常码冲突。

## 5. 综合实践

把 ALU 和 BRU 串起来，追踪一段真实的双指令序列在整个执行阶段的流转。考察下面这段汇编（仅用于阅读追踪，**示例代码**，不必编译）：

```asm
# lane0:  add  a0, a1, a2      # ALU：a0 = a1 + a2
# lane1:  beq  a3, a4, target  # BRU：若 a3==a4 则跳 target（假设取指预测「不跳」）
```

请完成以下任务：

1. **画时序图**：画出周期 N（译码）与周期 N+1（执行）两拍里，lane0 的 ALU 和 lane1 的 BRU 各自 `req / rs1 / rs2 / rd / taken.valid` 的取值。注意 `add` 走 ALU、`beq` 走 BRU，二者共享一个写回槽但互不冲突。
2. **分析屏障影响**：假设 `a3 == a4`（beq 实际跳转）且取指预测不跳（`fwd=0`）。
   - lane1 的 `taken.valid` 是否为真？依据 [Bru.scala:202-212](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Bru.scala#L202-L212) 推导。
   - 由于 lane0 在 lane1 之前，`writeMask(0)` 是否受 lane1 影响？结合 [SCore.scala:424-427](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/SCore.scala#L424-L427) 的 `scan` 方向回答（提示：`scan` 从左往右累积，屏蔽的是「排在分支之后」的槽）。
   - 结论：lane0 的 `add` 结果能否正常写回？下一周期取指会被引到哪里？
3. **对照延迟表**：用 [microarch.md:23-30](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/microarch/microarch.md#L23-L30) 确认这两条指令都是 1 周期延迟，解释为何它们能在同一周期并发派发与执行。

**参考结论**：lane1 的 `beq` 因 `fwd=0` 且 `eq=1` 触发 `taken.valid=1`，重定向取指到 `target`。`writeMask` 只屏蔽分支**之后**的槽，lane0 在前不受影响，`add` 正常写回。这正体现了 in-order 派发下「分支终结其后的同组指令、但不妨碍其前的指令」。

## 6. 本讲小结

- ALU 与 BRU 都是「1 周期延迟」的执行单元，延迟来自「命令在译码周期锁存、操作数在执行周期到达」的跨拍时序，而非单元内部多级流水。
- ALU 的核心是 [Alu.scala:119-149](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/scalar/Alu.scala#L119-L149) 的一张 `MuxLookup` 选通表，覆盖 RV32IM 与 ZBB 共 28 种运算；它永远 ready、无状态，结果组合给出。
- BRU 用 `fwd` 字段把「取指预测」与「客观跳转」解耦：`taken.valid = (实际结果 =/= fwd)`，只在预测错误时重定向取指，避免不必要的冲刷。
- `jal` 目标在译码端算好（PC 相对），`jalr` 目标在执行端才算定（依赖 rs1，且清最低位）；`jal/jalr` 还会把 `pc+4` 写回 rd 作为返回地址。
- 只有 lane 0 的 BRU（`first=true`）承担 ECALL/EBREAK/MRET/中断/故障的 CSR 提交，并发出 `interlock` 让派发器停拍。
- 分支通过 `branchTaken` 与 `writeMask` 充当派发屏障：重定向后，排在分支之后的同组指令写回被屏蔽、取指被引向新目标，但分支之前的指令不受影响。

## 7. 下一步学习建议

本讲只覆盖了「单周期、组合型」的两个执行单元。标量核里还有几类延迟更复杂的单元，建议按下列顺序继续：

1. **u5-l2（MLU 与 DVU）**：MLU 是 3 级乘法流水、且全核唯一实例（多 lane 要仲裁），DVU 是可变延迟除法并会反压派发——它们会打破本讲「永远 ready」的简化假设，是理解执行单元握手反压的下一步。
2. **u5-l3（整数/浮点寄存器堆与 CSR）**：深入寄存器堆的 8 读 / 多写端口、写回仲裁与记分板，以及 CSR 文件如何与本讲 BRU 的 `CsrBruIO`（`mepc/mcause/mtvec`）对接。
3. 回头重读 **u4-l4（派发与退休）**：现在你已经知道 `taken.valid / writeMask / interlock` 是怎么产生的，可以更扎实地理解 in-order 派发的冲刷与乱序退休如何在这些信号之上运作。
