# GlobalISel 新一代指令选择

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 GlobalISel 是什么，以及它用一条怎样的四阶段流水线（IRTranslator → Legalizer → RegBankSelect → InstructionSelect）把 LLVM IR 翻译成目标机器指令。
- 理解 GMI（Generic Machine IR，通用机器 IR）这一中间形态的作用：它和后端 MIR 共用同一套数据结构，但约束更宽松，是贯穿四个阶段的「半成品」。
- 区分四个阶段各自的「契约」——每个 pass 用 `MachineFunctionProperties` 声明自己要求什么、产出什么，从而保证阶段之间能正确衔接。
- 能在 `llc` 上用 `-global-isel` 与 `-debug` / `-debug-only=...` 观察一条 IR 经过四个阶段后的中间产物。
- 认识 GlobalISel 相对上一讲（u6-l2）SelectionDAG 的设计取舍：为什么它是「全局（函数级）」的，代价与收益各是什么。

## 2. 前置知识

本讲承接 **u6-l1（后端流水线总览）** 与 **u6-l2（SelectionDAG 指令选择）**，默认你已经知道：

- 后端的输入是优化后的 LLVM IR，输出是目标汇编/目标文件；指令选择（Instruction Selection）是其中「把 IR 操作翻译成机器指令」的关键阶段。
- 后端 IR 是 **MIR**（MachineIR），其层次为 `MachineFunction ⊃ MachineBasicBlock ⊃ MachineInstr`，与前端 IR 结构相似但操作的是寄存器与机器操作数（u6-l1）。
- SelectionDAG 是「**基本块级**」的指令选择框架：每个基本块建一张临时 DAG，合法化、模式匹配、调度后再销毁（u6-l2）。

本讲还需要两个基础概念：

- **虚拟寄存器（virtual register, vreg）与寄存器类（register class）**：寄存器分配之前，后端用无限多个虚拟寄存器编程；寄存器类是一组「可互换」的物理寄存器（如 X86 的 `GR32`）。GlobalISel 在此之间多引入一层「寄存器库（register bank）」，本讲会讲清它和寄存器类的关系。
- **SSA（静态单赋值）**：每个值只定义一次（u3-l2）。GlobalISel 的前三个阶段都要求 MIR 处于 SSA 形态。

如果你对「指令选择到底在选什么」还模糊，可以先快速回顾 u6-l2 的「合法化 → 模式匹配选指令」主线，再回到本讲。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `llvm/docs/GlobalISel/Pipeline.rst` | 官方对四阶段流水线与各阶段契约的权威说明 |
| `llvm/docs/GlobalISel/GMIR.rst` | 解释什么是 gMIR（Generic Machine IR）及其与 MIR 的关系 |
| `llvm/lib/CodeGen/TargetPassConfig.cpp` | 决定是否启用 GlobalISel（`-global-isel`）并把四个 pass 按序接入后端流水线 |
| `llvm/lib/Target/AArch64/AArch64TargetMachine.cpp` | 一个具体目标（AArch64）如何覆写钩子、实例化这四个 pass |
| `llvm/include/llvm/CodeGen/MachineFunction.h` | `MachineFunctionProperties` 属性枚举——阶段间的契约机制 |
| `llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp`（+ `.h`） | **阶段一**：LLVM IR → gMIR |
| `llvm/lib/CodeGen/GlobalISel/Legalizer.cpp` | **阶段二**：把目标不支持的 gMIR 操作塑形成支持的形态 |
| `llvm/lib/CodeGen/GlobalISel/LegalizerHelper.cpp`（+ `LegalizerInfo.h`） | 合法化的具体动作与 `LegalizeAction` 枚举 |
| `llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp`（+ `.h`） | **阶段三**：给每个通用虚拟寄存器选定寄存器库 |
| `llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp`（+ `.h`） | **阶段四**：把通用指令选定为目标指令，gMIR 此时变成 MIR |
| `llvm/include/llvm/CodeGen/GlobalISel/GenericMachineInstrs.h` | `G_ADD`/`G_LOAD` 等「通用操作码」的 C++ 包装类 |

> 全局心法：四个 pass 都位于 `llvm/lib/CodeGen/GlobalISel/`，**与目标无关**；目标相关的差异（哪些类型合法、寄存器库长什么样、怎么选指令）通过子类/hook 注入。这与 SelectionDAG「框架在 `lib/CodeGen/SelectionDAG`、目标描述在 `lib/Target`」的分工如出一辙。

## 4. 核心概念与源码讲解

### 4.1 GlobalISel 全景与 GMI（Generic Machine IR）

#### 4.1.1 概念说明

**GlobalISel** 是 LLVM 的「新一代」指令选择框架。名字里的 **Global（全局）** 是相对 SelectionDAG 的「**局部（基本块级）**」而言的：SelectionDAG 一次只看一个基本块、为它建一张临时 DAG 再丢弃；GlobalISel 则直接在整个 `MachineFunction`（函数级）上工作，四个 pass 串成一条**持久的、函数级的**流水线，中间结果一直以 MIR 的形式留在函数里，可以用 `-print-after-all` / `-debug` 像观察普通后端 pass 一样观察。

它要解决的核心问题是：SelectionDAG 虽然成熟、优化能力强，但它有一套**自成一体的 DAG 数据结构**，与后端主流的 MIR 世界割裂——DAG 里做的很多分析、变换难以被后续 MIR pass 复用，且每个基本块独立建图带来可观的编译时间开销。GlobalISel 的设计选择是：**放弃专用 DAG，直接复用 MIR 的数据结构**，只在上面放宽约束，得到一种叫 **gMIR（Generic Machine IR）** 的中间形态，然后分四个阶段逐步「收紧约束」，直到 gMIR 变成普通 MIR。

那 gMIR 到底「宽」在哪里？官方文档一句话说清：

> [llvm/docs/GlobalISel/GMIR.rst:7-15](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/docs/GlobalISel/GMIR.rst#L7-L15) —— gMIR 与 MIR 共用同一套数据结构，但约束更宽松；随着流水线推进，约束逐步收紧，gMIR 最终变成 MIR。

具体地，gMIR 相对 MIR 的两点「宽松」：

1. **通用操作码（Generic Opcodes）**：MIR 主要用**目标指令**（如 AArch64 的 `ADDXrr`），只有 `COPY`/`PHI` 等少数目标无关操作码；gMIR 则定义了一整套目标无关的 `G_` 前缀操作码，例如 `G_ADD`（整数加）、`G_LOAD`（加载）、`G_ICMP`（整数比较）、`G_PTR_ADD`（指针加）。它们描述「通常所有目标都能支持」的操作语义，而不绑定具体机器编码。
   - [llvm/docs/GlobalISel/GMIR.rst:26-32](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/docs/GlobalISel/GMIR.rst#L26-L32) 说明了这一点，并以 `G_ADD` 为例。
   - 这些操作码在源码里有对应的 C++ 包装类，如 `GAdd`、`GPtrAdd`、`GICmp`、`GLoad`、`GStore`：[llvm/include/llvm/CodeGen/GlobalISel/GenericMachineInstrs.h:811](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/GlobalISel/GenericMachineInstrs.h#L811)（`GAdd`）、[:365](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/GlobalISel/GenericMachineInstrs.h#L365)（`GPtrAdd`）、[:411](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/GlobalISel/GenericMachineInstrs.h#L411)（`GICmp`）。

2. **通用虚拟寄存器（generic vreg）**：MIR 的虚拟寄存器通常已被约束到某个**寄存器类**；gMIR 的虚拟寄存器只带一个**低层类型（LLT）**，不绑定寄存器类——类型可以是 `s32`（32 位标量）、`p0`（指针）、`<4 x s16>`（4×16 位向量）等。

一句话总结：**gMIR = 同样的 MIR 数据结构 + 一套 `G_` 通用操作码 + 只带 LLT 的通用虚拟寄存器**。四个阶段做的事，就是逐步把 `G_` 操作码换成目标指令、把「只带类型」的寄存器逐步绑定到寄存器库再到寄存器类。

#### 4.1.2 核心流程

GlobalISel 的核心流水线是固定的四个 pass，官方图示与说明见 [llvm/docs/GlobalISel/Pipeline.rst:6-63](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/docs/GlobalISel/Pipeline.rst#L6-L63)：

```
LLVM IR
  │
  ▼  ① IRTranslator        —— 几乎逐条翻译，IR → gMIR（带 G_ 操作码、通用 vreg）
  │
  ▼  ② Legalizer           —— 把目标不支持的操作/类型，塑形成目标能接受的形态
  │
  ▼  ③ RegBankSelect       —— 给每个通用 vreg 选定一个「寄存器库」（如 GPR/FPR）
  │
  ▼  ④ InstructionSelect   —— 依据目标 InstructionSelector，把 G_ 选定为目标指令
  │
  ▼
目标 MIR（之后进入寄存器分配等常规后端 pass）
```

每个阶段都有一条**契约（constraint）**，描述它结束后 MIR 必须满足的状态（见 Pipeline.rst:44-63）：

| 阶段 | 结束后必须满足 |
|------|----------------|
| IRTranslator | 表示为 gMIR / MIR 或二者混合（绝大多数是 gMIR） |
| Legalizer | 不再有任何非法操作 |
| RegBankSelect | 所有虚拟寄存器都已分配寄存器库 |
| InstructionSelect | 不再有任何 gMIR 残留——gMIR 已完全变成 MIR |

这条「契约链」不是靠注释维护，而是由 **`MachineFunctionProperties`** 这套位标志在代码层强制：每个 pass 通过 `getRequiredProperties()` / `getSetProperties()` / `getClearedProperties()` 声明它**要求输入具备**、**保证输出具备**、**可能破坏**哪些属性。属性枚举定义在：

- [llvm/include/llvm/CodeGen/MachineFunction.h:188-202](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/MachineFunction.h#L188-L202) —— `Property` 枚举，含 `IsSSA`、`Legalized`、`RegBankSelected`、`Selected`、`FailedISel` 等。

其中三个 GlobalISel 专用属性的含义，源码注释写得非常清楚：

- [llvm/include/llvm/CodeGen/MachineFunction.h:163-176](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/MachineFunction.h#L163-L176) —— `Legalized`（Legalizer 跑过且无非法指令）、`RegBankSelected`（所有通用 vreg 已分到寄存器库）、`Selected`（InstructionSelect 跑过，所有 pre-isel 通用指令已消除）。

这套机制让阶段之间的依赖变成**可机器校验**的：如果 InstructionSelect 运行时发现输入没有 `Legalized` 标志，就是配置错误。后续 4.2–4.4 会逐一展示每个 pass 是如何声明这三件套的。

**怎么启用 GlobalISel？** 后端默认仍走 SelectionDAG。是否启用 GlobalISel 由 `-global-isel` 命令行开关（或目标的 `setGlobalISel(true)`）控制：

- [llvm/lib/CodeGen/TargetPassConfig.cpp:170-172](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/TargetPassConfig.cpp#L170-L172) —— `-global-isel` 这个 `cl::opt`，描述为「启用全局指令选择器」。

`TargetPassConfig::addISelPasses` 据此在 `SelectionDAG` / `FastISel` / `GlobalISel` 三者间选一个：

- [llvm/lib/CodeGen/TargetPassConfig.cpp:1000-1013](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/TargetPassConfig.cpp#L1000-L1013) —— `SelectorType` 三选一逻辑：`-global-isel` 为真或目标默认开启且未显式关闭时选 `GlobalISel`。

选中 GlobalISel 后，框架把四个 pass 按固定顺序接入，每个之间还留了 `addPreXxx()` 钩子给目标插入自定义 pass：

- [llvm/lib/CodeGen/TargetPassConfig.cpp:1038-1059](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/TargetPassConfig.cpp#L1038-L1059) —— 依次调用 `addIRTranslator()` → `addPreLegalizeMachineIR()` → `addLegalizeMachineIR()` → `addPreRegBankSelect()` → `addRegBankSelect()` → `addPreGlobalInstructionSelect()` → `addGlobalInstructionSelect()`。

注意紧随其后还加了一个 `ResetMachineFunctionPass`（TargetPassConfig.cpp:1063-1065）：当 GlobalISel 在某个函数上失败（设置了 `FailedISel`）且未启用「失败即 abort」时，它会**把该函数回退（fallback）到 SelectionDAG** 重新选择。这正是 GlobalISel 在生产中能渐进铺开的关键——跑不动就退回老路径。

#### 4.1.3 源码精读：一个目标如何接入这四个 pass

`addIRTranslator()` 等四个方法在基类里是**虚钩子**，默认实现「不接入」（返回 true 表示该目标不支持）：

- [llvm/include/llvm/CodeGen/TargetPassConfig.h:269-296](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/TargetPassConfig.h#L269-L296) —— 四个虚方法的声明，注释点明「想用 GlobalISel 的目标应实现它们」。

以 AArch64 为例，它把这四个钩子实现为直接 `addPass(new ...)`：

- [llvm/lib/Target/AArch64/AArch64TargetMachine.cpp:771-814](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/Target/AArch64/AArch64TargetMachine.cpp#L771-L814) —— AArch64 依次实例化 `IRTranslator`、`Legalizer`、`RegBankSelect`、`InstructionSelect`（外加一个 `AArch64PostSelectOptimize`）。这段代码就是「GlobalISel 在某目标上落地」的最小证据。

> 阅读提示：你会在很多目标（X86、RISCV、AMDGPU…）里看到同样的四个覆写。这说明 GlobalISel 的**框架是共享的**，目标只需提供四样东西：`CallLowering`（参数/返回如何 lowering，给 IRTranslator 用）、`LegalizerInfo`（什么合法，给 Legalizer 用）、`RegisterBankInfo`（寄存器库长什么样，给 RegBankSelect 用）、`InstructionSelector`（怎么选指令，给 InstructionSelect 用）。

#### 4.1.4 代码实践：开启并观察 GlobalISel

1. **实践目标**：确认 `llc` 能用 GlobalISel 跑通一个最简函数，并验证默认走的是 SelectionDAG。
2. **操作步骤**：
   - 准备一段最简 IR `add.ll`：
     ```llvm
     define i32 @add(i32 %a, i32 %b) {
       %r = add i32 %a, %b
       ret i32 %r
     }
     ```
   - 用 AArch64 目标、显式开启 GlobalISel 编译为汇编：
     ```bash
     llc -mtriple=aarch64 -global-isel -debug-only=instruction-select add.ll -o add.s
     ```
   - 对照地，不开启 GlobalISel 再跑一次（默认 SelectionDAG）。
3. **需要观察的现象**：开启 `-global-isel` 时，`-debug-only=instruction-select` 会打印 `Selecting function: add` 及逐条 `Select: ...` 日志；最终 `add.s` 中应能看到一条目标加法指令（如 `add w0, w0, w1`）。
4. **预期结果**：两条路径生成的核心加法指令一致；区别只在指令选择的中间过程。
5. 若本地未构建带 AArch64 后端的 `llc`，可换用你已构建的目标（如 `-mtriple=x86_64`），命令等价；若 `llc` 未开启断言/调试，`-debug-only` 选项会无效——此时记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：GlobalISel 里「Global」是相对什么而言的？
> **答**：相对 SelectionDAG 的「基本块级（局部）」。SelectionDAG 一次只为一个基本块建临时 DAG；GlobalISel 在整个 `MachineFunction` 上跑，中间结果持久保留为 gMIR。

**练习 2**：gMIR 和 MIR 用的是同一套数据结构吗？它「宽松」在哪两点？
> **答**：是同一套数据结构。两点宽松：(1) 用一整套目标无关的 `G_` 通用操作码（`G_ADD` 等）而非目标指令；(2) 虚拟寄存器只带低层类型 LLT，不绑定寄存器类。

**练习 3**：GlobalISel 在某函数上选不动时会发生什么？
> **答**：若未启用「失败即 abort」，`ResetMachineFunctionPass` 会清除失败状态，把该函数**回退到 SelectionDAG** 重新选择，从而保证编译仍能成功。

---

### 4.2 IRTranslator：从 LLVM IR 到 gMIR

#### 4.2.1 概念说明

**IRTranslator** 是第一个 pass，职责是把 LLVM IR **几乎逐条**翻译成 gMIR。官方类比：它「类似于 SelectionDAGBuilder，但产物是 gMIR 而非专用 DAG 表示」（见 Pipeline.rst:14-20）。它本身基本不做目标定制，只在涉及 ABI（函数调用、参数传递、返回）的地方调用目标提供的 `CallLowering` hook。

它的输出是一份「纯净」的 gMIR：所有 IR 指令变成了对应的 `G_` 操作码，所有 IR 值变成了带 LLT 的通用虚拟寄存器，函数仍处于 SSA 形态。一句话：**IRTranslator 是一次「换皮」——把 IR 的语义用 gMIR 的词汇重新表达一遍，几乎不改语义。**

#### 4.2.2 核心流程

`IRTranslator::runOnMachineFunction` 的算法在其头文件注释里写得明明白白（IRTranslator 类继承自 `MachineFunctionPass`）：

- [llvm/include/llvm/CodeGen/GlobalISel/IRTranslator.h:66-68](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/GlobalISel/IRTranslator.h#L66-L68) —— `class IRTranslator : public MachineFunctionPass`。

主循环（伪代码，整理自头文件 :795-807 的注释）：

```
CLI = subtarget.getCallLowering()          # 取目标的 ABI lowering hook
创建一个专门的「入口基本块」（放参数 lowering 与常量物化）
CLI->lowerFormalArguments(...)              # 经目标 hook 把形参 lower 进 vreg
for 每个 IR 基本块 bb（按逆后序 RPOT）:
    CurBuilder.setMBB(对应的 MachineBasicBlock)
    for bb 中每条指令 inst:
        translate(inst)                    # 一条 IR → 一条/数条 G_ 指令
    finalizeBasicBlock(bb)                  # 处理跳转表、栈保护等
finishPendingPhis()                         # 补齐之前留空的 PHI 操作数
合并入口块与 IR 入口块
```

其中最关键的是 **`translate(Instruction&)` 的分发**：它用一个巨大的 `switch`，按 IR 指令操作码（`Instruction::Add`、`Instruction::Load`…）调用对应的 `translateXxx`，后者再产出对应的 `G_` 操作码。例如 `add` 走 `translateAdd`，它只是把 `G_ADD` 透传给通用的 `translateBinaryOp`：

- [llvm/include/llvm/CodeGen/GlobalISel/IRTranslator.h:459-461](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/GlobalISel/IRTranslator.h#L459-L461) —— `translateAdd` 调 `translateBinaryOp(TargetOpcode::G_ADD, ...)`。

`translateBinaryOp` 的实现极其简洁，体现了「换皮」本质——取两个操作数的 vreg、取/建结果的 vreg、发一条带 `G_ADD` 操作码的机器指令：

- [llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp:314-334](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp#L314-L334) —— `translateBinaryOp`：为两个操作数和结果各取/建 vreg，然后 `MIRBuilder.buildInstr(Opcode, {Res}, {Op0, Op1}, Flags)`。

分发开关本身用了一个巧妙的宏展开，遍历 `llvm/IR/Instruction.def` 里所有指令，自动生成 `case Instruction::OPCODE: return translate##OPCODE(...)`：

- [llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp:3853-3869](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp#L3853-L3869) —— `translate` 的核心：设调试位置后，若目标要求该指令回退 DAG 则返回 false，否则 `switch` 按 `Instruction.def` 宏分发。注意开头的 `TLI->fallBackToDAGISel(Inst)`——这是目标「点名某条指令不让 GlobalISel 处理」的逃生口。

#### 4.2.3 源码精读：主循环与常量物化

入口 `runOnMachineFunction` 做了大量初始化：取出 `CallLowering`、构造两个 `MachineIRBuilder`（`CurBuilder` 用于当前块、`EntryBuilder` 用于入口块）、判定能否降低返回类型、为大端做检查等：

- [llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp:4226-4268](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp#L4226-L4268) —— 初始化：建专用入口块、配置 builder、检查大端、设置 `scope_exit` 在返回时 `finalizeFunction()`。

主翻译循环在 RPOT（逆后序，保证先访问定义后访问使用）下遍历每个 IR 块，逐条调用 `translate`，失败则经 `reportTranslationError` 设置 `FailedISel`（触发后续回退）：

- [llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp:4374-4427](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp#L4374-L4427) —— `RPOT` 遍历 + `for (const Instruction &Inst : *BB)` + `translate(Inst)`；翻译失败时发出 `gisel-irtranslator` 优化备注并 `return false`。

**常量物化**是 IRTranslator 的一个重要细节：IR 里的 `Constant` 并不立即变成指令，而是先记在 `VMap` 里（为常量分配一个 vreg），最后在 `finalizeFunction` 阶段统一在入口块用 `G_CONSTANT`/`G_FCONSTANT` 等物化（见头文件 :677-681 的注释「Insert all the code needed to materialize the constants」）。这样做便于对这些常量做 CSE（公共子表达式消除）。

#### 4.2.4 代码实践：观察 IRTranslator 的输出

1. **实践目标**：看清一条 `add` IR 是如何变成 `G_ADD` gMIR 的。
2. **操作步骤**：
   ```bash
   llc -mtriple=aarch64 -global-isel -debug-only=irtranslator add.ll -o /dev/null
   ```
3. **需要观察的现象**：日志里应出现 `IRTranslator LLVM IR -> MI` 之后的 gMIR 片段，类似：
   ```
   %0:_(s32) = COPY $w0
   %1:_(s32) = COPY $w1
   %2:_(s32) = G_ADD %0, %1
   $w0 = COPY %2
   ```
   注意所有寄存器都是 `%n:_(s32)` 形式——下划线表示「通用 vreg」，`(s32)` 是其 LLT。
4. **预期结果**：你能把每一条 gMIR 指令对应回原始 IR 指令，且看不到任何目标专属指令（如 `ADDXrr`），证明 IRTranslator 产出的确实是「纯净」gMIR。
5. 若 `-debug-only` 无效（Release 构建），改用 `-global-isel -print-after-all`，在 `IRTranslator` 之后的 MIR 打印中同样能看到 `G_ADD`；现象记为「待本地验证」直至实际运行。

#### 4.2.5 小练习与答案

**练习 1**：IRTranslator 遍历基本块用的是什么序？为什么？
> **答**：逆后序（RPOT，`ReversePostOrderTraversal`）。因为翻译一条指令时要先能为它的操作数（定义）拿到 vreg，逆后序保证定义先于使用被访问。

**练习 2**：IR 里的整数常量（如 `i32 42`）是在翻译到使用点时立即生成指令的吗？
> **答**：不是。IRTranslator 先为常量分配一个通用 vreg 并记录在 `VMap`，所有使用点引用这个 vreg；常量真正被 `G_CONSTANT` 物化是在函数末尾 `finalizeFunction` 阶段统一在入口块完成，便于做 CSE。

**练习 3**：`TLI->fallBackToDAGISel(Inst)` 在 `translate` 开头起什么作用？
> **答**：它给目标一个「点名」机会：若目标认为某条具体指令自己处理不了（或不值得处理），可让它返回 true，`translate` 直接返回 false，从而标记 `FailedISel`、回退到 SelectionDAG。

---

### 4.3 Legalizer：把非法操作塑形成目标可接受的形态

#### 4.3.1 概念说明

IRTranslator 产出的 gMIR 是「目标无关」的，但真实 CPU 并不支持任意类型上的任意操作——比如某目标可能没有 `<2 x s8>` 的向量加法、或没有 `s8` 的除法。**Legalizer** 的职责就是把这些**目标不支持的操作/类型组合（illegal）**，**改写（塑形）成目标支持的（legal）**。

关键点：Legalizer 是第一个**强目标相关**的阶段——它完全依赖目标提供的 `LegalizerInfo`（一张「什么操作在什么类型上合法、不合法时该怎么办」的查询表）。框架本身（`Legalizer.cpp`）只负责**驱动**（一个工作表算法），具体怎么改写由 `LegalizerHelper` 按 `LegalizerInfo` 给出的动作执行。

类比 u6-l2 的 SelectionDAG：那里合法化也分「类型合法化」和「操作合法化」两轮；GlobalISel 把它们统一进同一个 worklist 驱动循环，且对类型与向量元素数一视同仁。

#### 4.3.2 核心流程

Legalizer 用一个 **worklist（工作表）算法** 反复处理非法指令，直到不再产生新的非法指令为止（迭代到不动点）。它维护两张工作表：

- `InstList`：普通通用指令；
- `ArtifactList`：「工件（artifact）」——一类廉价、可被组合掉的转换指令，如 `G_TRUNC`/`G_ZEXT`/`G_SEXT`/`G_MERGE_VALUES` 等（见 [llvm/lib/CodeGen/GlobalISel/Legalizer.cpp:100-117](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/Legalizer.cpp#L100-L117) 的 `isArtifact`）。工件单拿出来是为了优先尝试把它们「组合掉」（消除冗余的位宽转换），从而避免无谓的合法化。

主循环（伪代码，整理自 legalizeMachineFunction）：

```
把所有 pre-isel 通用指令按 RPOT 填入 InstList / ArtifactList
do:
    while InstList 非空:
        MI = InstList.pop_back()
        若 MI 已无使用（trivially dead）→ 删除，继续
        Res = LegalizerHelper.legalizeInstrStep(MI)   # 查 LegalizerInfo 取动作并执行
        若 Res == UnableToLegalize:
            若是 artifact → 暂存 RetryList，等会再试
            否则 → 整个函数合法化失败（FailedOn = MI）
        # 执行过程中新生成的指令经 observer 自动入表
    把 RetryList 里还没被组合掉的，转交 ArtifactList 或宣告失败
    while ArtifactList 非空:
        MI = ArtifactList.pop_back()
        若 ArtifactCombiner 能把它组合掉 → 删除冗余，继续
        否则 → 它其实是「真指令」，塞回 InstList 走正常合法化
while InstList 非空   # 因为合法化可能产生新的非法指令，需迭代
```

每条指令具体怎么合法化，由 `LegalizerHelper::legalizeInstrStep` 查询 `LegalizerInfo::getAction(MI)` 得到一个 **`LegalizeAction`**，再 switch 分发执行：

- [llvm/lib/CodeGen/GlobalISel/LegalizerHelper.cpp:122-164](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/LegalizerHelper.cpp#L122-L164) —— `legalizeInstrStep`：先处理 intrinsic，再 `LI.getAction(MI, MRI)` 取动作，`switch (Step.Action)` 分发到 `narrowScalar`/`widenScalar`/`fewerElementsVector`/`moreElementsVector`/`lower`/`libcall`/`custom` 等。

`LegalizeAction` 枚举定义了所有合法化动作，每个都附有贴切的例子：

- [llvm/include/llvm/CodeGen/GlobalISel/LegalizerInfo.h:44-95](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/GlobalISel/LegalizerInfo.h#L44-L95) —— `LegalizeAction` 枚举。摘录要点：

| 动作 | 含义 | 举例 |
|------|------|------|
| `Legal` | 直接可选，无需变换 | 目标原生支持 `G_ADD s32` |
| `NarrowScalar` | 用更窄的标量类型实现 | 64 位加法用两个 32 位带进位加法实现 |
| `WidenScalar` | 用更宽的标量类型实现 | `<2 x s8>` 加法当作 `<2 x s32>` 做，忽略高位 |
| `FewerElements` | 拆成更短的子向量 | `<8 x s64>` 拆成 4 个 `<2 x s64>` |
| `MoreElements` | 加宽向量后忽略多余 lane | `<2 x i8>` 当 `<8 x i8>` 做 |
| `Lower` | 用更简单的操作展开 | `SREM` 展成 `SDIV` + 减法 |
| `Libcall` | 展开为运行时库调用 | 无浮点硬件的目标把 `G_FDIV` 变成 `__divsf3` |
| `Custom` | 交给目标的 `legalizeCustom` 回调 | 目标想特殊处理某组合 |
| `Unsupported` / `NotFound` | 完全不支持 / 表里查不到 | 编程错误 |

注意动作可以**复合**：合法化一条指令常常产生若干条新的、类型仍非法的指令，于是回到工作表再合法化，直到全部 `Legal`——这就是外层 `do...while` 的意义。

#### 4.3.3 源码精读

Legalizer pass 的属性契约非常典型，是理解「阶段衔接」的范本：

- [llvm/include/llvm/CodeGen/GlobalISel/Legalizer.h:60-70](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/GlobalISel/Legalizer.h#L60-L70) —— **要求** `IsSSA`（输入必须是 SSA）；**保证** `Legalized`（输出无非法指令）；**声明破坏** `NoPHIs` 与 `NoVRegs`（合法化过程中可能引入 PHI、并始终使用虚拟寄存器）。

`Legalizer::legalizeMachineFunction` 是上述 worklist 算法的完整实现，注意它如何用 `GISelChangeObserver`（`LegalizerWorkListManager`）在改写 IR 时**自动把新指令入表**、把被删指令出表，从而无须手动维护工作表：

- [llvm/lib/CodeGen/GlobalISel/Legalizer.cpp:177-308](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/Legalizer.cpp#L177-L308) —— `legalizeMachineFunction`：初始化两张工作表（:185-207）、安装 observer（:209-221）、外层 `do...while` 双循环处理 InstList 与 ArtifactList（:224-305），失败时返回 `{Changed, FailedOn}`。

pass 入口 `runOnMachineFunction` 负责装配依赖：取目标的 `LegalizerInfo`、可选开启 CSE、构造 `LegalizerHelper` 与 `LegalizationArtifactCombiner`，然后调用 `legalizeMachineFunction`，失败则报 `gisel-legalize`：

- [llvm/lib/CodeGen/GlobalISel/Legalizer.cpp:310-391](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/Legalizer.cpp#L310-L391) —— `runOnMachineFunction`：开头 `if (MF.getProperties().hasFailedISel()) return false;`（IRTranslator 失败则跳过本 pass，:311-313），末段调用 `legalizeMachineFunction` 并处理失败与调试位置丢失告警。

#### 4.3.4 代码实践：观察合法化过程

1. **实践目标**：观察一个「非法」类型如何被合法化成目标支持的类型。
2. **操作步骤**：构造一段含窄向量加法的 IR `vec.ll`（目标大概率没有原生 `<2 x s8>` 加法）：
   ```llvm
   define <2 x i8> @vadd(<2 x i8> %a, <2 x i8> %b) {
     %r = add <2 x i8> %a, %b
     ret <2 x i8> %r
   }
   ```
   ```bash
   llc -mtriple=aarch64 -global-isel -debug-only=legalizer vec.ll -o /dev/null
   ```
3. **需要观察的现象**：日志里会出现反复的 `Legalizing: ...`、`.. Widen scalar` / `.. Reduce number of elements` / `.. Already legal` 等行，展示一条 `<2 x s8>` 的 `G_ADD` 如何被改写成更宽类型（如 `<2 x s32>` 或 `<8 x s8>`）上的等价操作，最后落到 `Legal`。注意 `=== New Iteration ===` 标记外层迭代。
4. **预期结果**：你能复述出这条指令经过了哪几次动作、最终变成什么类型；并理解「合法化产生新指令 → 回到工作表」的迭代过程。
5. 由于合法化行为高度依赖具体目标的 `LegalizerInfo`，不同 `-mtriple` 看到的动作链可能不同；具体动作链「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：Legalizer 为什么要分 `InstList` 和 `ArtifactList` 两张工作表？
> **答**：因为像 `G_TRUNC`/`G_ZEXT` 这类「工件」往往是无谓的位宽转换，优先用 `LegalizationArtifactCombiner` 把它们组合消除，比走完整合法化更省事、产出更优；只有组合不掉的工件才回退为普通指令去合法化。

**练习 2**：`LegalizeAction::Lower` 和 `Libcall` 有何区别？各举一例。
> **答**：`Lower` 是用**更简单的同类机器操作**展开（如 `SREM` → `SDIV` + 减法）；`Libcall` 是展开为一次**运行时库函数调用**（如无浮点硬件时 `G_FDIV` → `__divsf3`）。

**练习 3**：Legalizer 的属性契约里为什么 `getClearedProperties()` 要 `setNoPHIs()`？
> **答**：合法化（尤其涉及控制流或拆分）可能引入 `PHI`，因此它声明不再保证「无 PHI」属性（`NoPHIs`），以便后续 pass 不基于「无 PHI」做错误假设。

---

### 4.4 RegBankSelect 与 InstructionSelect：寄存器库选定与目标指令选定

#### 4.4.1 概念说明

合法化之后，所有 gMIR 操作都是目标「能选」的了，但还有两个维度没确定：

1. **每个通用 vreg 该住在哪个「寄存器库」里？** 真实 CPU 的寄存器往往按用途分库——如 AArch64 有 GPR（通用整数寄存器，如 `w0`/`x0`）和 FPR（浮点/向量寄存器，如 `s0`/`v0`）；X86 有 GPR、MMX、XMM 等。一个 `s32` 的值既可放 GPR 也可放 FPR，但**跨库搬动需要一条 `COPY`，有代价**。**RegBankSelect** 的任务就是给每个通用 vreg 选一个寄存器库，目标是让相关指令的操作数都落在「能直接执行该指令」的库上，尽量减少跨库拷贝。
   - 寄存器库（register bank）是「一组物理寄存器的集合」，比寄存器类（register class）更粗：一个库通常对应一个寄存器文件（如整个 GPR），到 InstructionSelect 阶段才进一步约束到具体的寄存器类。

2. **每条 `G_` 通用指令该换成哪条具体目标指令？** **InstructionSelect** 用目标提供的 `InstructionSelector`（通常由 TableGen 生成的匹配表）做最终拍板：依据每条指令的操作码、操作数类型与（现已确定的）寄存器库/类，把它「变形成」目标指令。这一步完成后，gMIR 就正式变成 MIR——不再有任何 `G_` 前缀操作码残留，通用 vreg 也都被约束到了具体寄存器类。

#### 4.4.2 核心流程

**RegBankSelect** 有两种模式（头文件枚举）：

- [llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h:91-103](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h#L91-L103) —— `Mode { Fast, Greedy }`。`Fast` 只看一条指令、用目标的默认映射（编译快）；`Greedy` 会比较多种映射并计入跨库修复代价、选局部最优（代码质量好、编译慢）。

`Greedy` 模式的代价模型在头文件开头用一段公式 + 数值例子讲得很清楚：

- [llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h:31-60](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h#L31-L60) —— 代价公式：`cost(I, RegBank) = cost(I.Opcode, RegBank) + Σ costCrossCopy(arg.RegBank, RegBank)`。例中目标说 `G_ADD` 在库 A 代价 5、库 B 代价 1，但两个操作数都在库 A、跨库拷贝代价各 1，最终算出选 B 总代价 3 < 选 A 总代价 5，于是选 B 并插入两条 `COPY`。

模式由命令行 `-regbankselect-fast` / `-regbankselect-greedy` 控制（默认 Fast）：

- [llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp:61-87](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp#L61-L87) —— `RegBankSelectMode` 这个 `cl::opt` 与构造函数（命令行覆盖构造时传入的模式）。

逐指令分配的核心是 `assignInstr`——`Fast` 直接取目标默认映射，`Greedy` 则枚举候选映射、用 `computeMapping` 估代价挑最优：

- [llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp:649-680](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp#L649-L680) —— `assignInstr` 开头：处理 `G_ASSERT_*` 提示（直接用源的库），否则按 `OptMode` 走 Fast（取 `RBI->getInstrMapping`）或 Greedy（枚举 + 计价）分支。

选定映射后，需要把每个操作数「搬」到它该去的库——若操作数当前不在目标库，就插入修复代码（`COPY` 或拆分），这由 `applyMapping` 完成：

- [llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp:595-647](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp#L595-L647) —— `applyMapping`：对每个修复点，按 `Reassign`（仅改库标记）或 `Insert`（真的插入修复指令）处理，最后调 `RBI->applyMapping` 重写指令。

整个 pass 的入口与遍历结构：

- [llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp:750-771](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp#L750-L771) —— `runOnMachineFunction`：失败则跳过；`optnone` 函数强制 Fast；调 `assignRegisterBanks`。
- [llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp:700-735](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp#L700-L735) —— `assignRegisterBanks`：RPOT 遍历每个块、块内逆序遍历指令，逐条 `assignInstr`。

RegBankSelect 的属性契约：

- [llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h:624-634](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h#L624-L634) —— **要求** `IsSSA + Legalized`；**保证** `RegBankSelected`；**声明破坏** `NoPHIs`。

**InstructionSelect** 则是「最终拍板」。它的遍历顺序很特别：**基本块按后序（post-order）、块内指令按逆序**——这样选一条指令时，它的所有使用都已经被选过，操作数的寄存器类已经确定，便于做操作数匹配与折叠：

- [llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp:157-221](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp#L157-L221) —— `selectMachineFunction`：`for (MBB : post_order(&MF))` 外层、`MIIMaintainer.MII = MBB->rbegin()` 内层逆序，逐条 `selectInstr(MI)`；还顺便删除后序不可达的块（:223-235），以及消除同寄存器类间的冗余 `COPY`（:236-256）。

每条指令的选择最终落到目标的 `InstructionSelector::select`：

- [llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp:348-386](https://github.com/llvm/llvm-project/blob/e096d2f60dbc694dc/llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp#L348-L386) —— `selectInstr`：先删除已变 dead 的指令、消除 `G_ASSERT_*`/`G_CONSTANT_FOLD_BARRIER` 提示、擦除 `G_INVOKE_REGION_START`，最后 `return ISel->select(MI)`（目标的匹配表执行在此）。

pass 入口取出目标的 `InstructionSelector` 并按优化等级决定是否启用 profile 信息：

- [llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp:134-155](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp#L134-L155) —— `runOnMachineFunction`：`ISel = MF.getSubtarget().getInstructionSelector()`，`optnone` 强制 `CodeGenOptLevel::None`，调 `selectMachineFunction`。

InstructionSelect 的属性契约最「挑剔」——它要求前三步都已完成：

- [llvm/include/llvm/CodeGen/GlobalISel/InstructionSelect.h:43-52](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/GlobalISel/InstructionSelect.h#L43-L52) —— **要求** `IsSSA + Legalized + RegBankSelected`；**保证** `Selected`。这正是「契约链」的最后一环。

> 贯穿提示：把这四个 pass 的 `getRequiredProperties/getSetProperties` 串起来看，就是 `IsSSA → +Legalized → +RegBankSelected → +Selected` 的逐级累积，与 4.1.2 的契约表完全对应。`MachineFunctionProperties` 把「流水线阶段」变成了「可校验的状态机」。

#### 4.4.3 源码精读：GlobalISel vs SelectionDAG 的取舍

把本讲与 u6-l2 对照，可以清晰地看到 GlobalISel 的设计取舍：

| 维度 | SelectionDAG（u6-l2） | GlobalISel（本讲） |
|------|----------------------|-------------------|
| 作用域 | 基本块级，每块建临时 DAG 再销毁 | 函数级，gMIR 持久保留，四 pass 串联 |
| 数据结构 | 专用 `SDNode`/`SDValue` DAG | 复用 MIR 的 `MachineInstr`，加 `G_` 操作码与 LLT |
| 合法化 | 分类型合法化、操作合法化两轮，DAGCombiner 穿插 | 单一 worklist，类型/元素数/操作统一用 `LegalizeAction` |
| 指令选择 | `SelectCodeCommon` 跑 TableGen MatcherTable | 目标 `InstructionSelector::select`（同样用 TableGen 匹配表） |
| 寄存器约束 | 选择后即约束到具体寄存器类 | 多一步 RegBankSelect，先选库再选类 |
| 与 MIR 集成 | 弱（DAG 与 MIR 两个世界） | 强（本就是 MIR） |
| 成熟度/优化能力 | 高（多年深耕） | 持续提升中，部分目标仍回退 SDAG |

简言之：GlobalISel 用「更直接的 MIR 集成 + 函数级持久表示」换取更好的可观测性、可复用性与编译速度；代价是引入了 RegBankSelect 这一步、且优化深度在部分场景尚不及 SDAG——这就是它需要 fallback 机制（4.1.2）的原因。

#### 4.4.4 代码实践：观察寄存器库与最终选定

1. **实践目标**：看到一个浮点值被分到 FPR 库，并最终被选定为目标浮点指令。
2. **操作步骤**：
   ```llvm
   define float @fadd(float %a, float %b) {
     %r = fadd float %a, %b
     ret float %r
   }
   ```
   ```bash
   # 观察寄存器库选定
   llc -mtriple=aarch64 -global-isel -debug-only=regbankselect fadd.ll -o /dev/null
   # 观察最终指令选定
   llc -mtriple=aarch64 -global-isel -debug-only=instruction-select fadd.ll -o fadd.s
   ```
3. **需要观察的现象**：
   - RegBankSelect 日志会出现 `Assign: ...`，并能看到某个 vreg 被标记为某个库（AArch64 上浮点会落到 FPR，即 `sb`/`fq` 系列），必要时插入 `COPY`。
   - InstructionSelect 日志会出现 `Select: %2:_(s32) = G_FADD ...`，随后 `Created:` 一条目标指令（AArch64 上如 `FADDSrr` 或 `FADD Sx, ...`）。
   - 最终 `fadd.s` 里只有目标浮点加法指令，无 `G_` 残留。
4. **预期结果**：你能把 `G_FADD` 这条通用指令，经「分到 FPR → 选定 `FADD`」两步，对应到 `fadd.s` 中的一条具体机器指令。
5. 想对比 `Fast` 与 `Greedy` 的差异，可加 `-regbankselect-greedy` 重新跑 RegBankSelect，观察是否插入了不同数量/位置的 `COPY`；具体差异「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 InstructionSelect 要「块按后序、块内指令逆序」地选择？
> **答**：这样选到某条指令时，它所有的**使用**都已被选过，操作数 vreg 已经被约束到具体寄存器类，选择器就能据此做操作数匹配与子寄存器折叠，得到更优的目标指令。

**练习 2**：RegBankSelect 的 `Fast` 与 `Greedy` 模式差别在哪？默认是哪个？
> **答**：`Fast` 只看当前一条指令、直接采用目标给的默认映射，编译快；`Greedy` 会枚举多种映射并按 `cost(I, RegBank) = 指令代价 + 跨库修复代价` 选局部最优，代码质量更好但编译慢。默认是 `Fast`（`optnone` 函数也强制 Fast）。

**练习 3**：InstructionSelect 跑完之后，gMIR 里的通用 vreg 的「低层类型 LLT」还存在吗？
> **答**：选择完成后所有 `G_` 指令都已变成目标指令，通用 vreg 也都已约束到寄存器类；后续不再需要 LLT，因此 `selectMachineFunction` 末尾会 `MRI.clearVirtRegTypes()` 把这些类型清除（见 InstructionSelect.cpp:342 附近）。

---

## 5. 综合实践

把四个阶段串起来，做一次「全流水线追踪」。

**任务**：对下面这段含整数运算、整数比较与分支的 IR，用 GlobalISel 跑完整流水线，画出它在每个阶段之后的 gMIR/MIR 形态。

```llvm
define i32 @max(i32 %a, i32 %b) {
entry:
  %cmp = icmp sgt i32 %a, %b
  br i1 %cmp, label %then, label %else
then:
  ret i32 %a
else:
  ret i32 %b
}
```

**操作步骤**：

1. 用 `-print-after-all` 一次性打印所有 pass 之后的 MIR，聚焦四个 GlobalISel pass：
   ```bash
   llc -mtriple=aarch64 -global-isel -print-after-all max.ll -o max.s 2> max.log
   ```
   在 `max.log` 中找到以 `IRTranslator`、`Legalizer`、`RegBankSelect`、`InstructionSelect` 为标题的几段 MIR 打印。

2. 也可以用更聚焦的调试开关分阶段看：
   ```bash
   llc -mtriple=aarch64 -global-isel -debug-only=irtranslator,legalizer,regbankselect,instruction-select max.ll -o /dev/null
   ```

3. 为每个阶段回答一个问题，形成一张「演变表」：
   - **IRTranslator 后**：`icmp` 变成了哪条 `G_` 指令？`br i1` 是如何翻译的？（提示：比较类会变成 `G_ICMP`，分支类变成 `G_BRCOND`/`G_BR`。）
   - **Legalizer 后**：比较与分支的类型/操作是否仍是 `Legal`，还是被改写了？
   - **RegBankSelect 后**：`%a`、`%b`、比较结果各被分到了哪个库（GPR 还是 FPR）？是否插入了 `COPY`？
   - **InstructionSelect 后**：`G_ICMP` + `G_BRCOND` 变成了哪些目标指令（AArch64 上通常是 `SUBS` + `B.LE`/`B.GT` 之类）？是否还有任何 `G_` 残留？

**预期结果**：你能用一张表把 `icmp`/`br` 这组语义，在四个阶段的形态逐一列出，并指出每一步「收紧了哪个约束」（操作码泛化→合法化→定库→定具体指令）。这就是 GlobalISel 「gMIR 逐步收紧成 MIR」的最直观演示。

> 若本地未构建带调试输出的 `llc`，可退而求其次：只看 `-print-after-all` 的 MIR 打印（Release 构建也支持），同样能辨认 `G_ICMP`/`G_BRCOND` 与最终目标指令；`-debug-only=...` 的详细日志记为「待本地验证」。

## 6. 本讲小结

- GlobalISel 是函数级、基于 gMIR 的新一代指令选择框架；gMIR 复用 MIR 数据结构，但用一整套目标无关的 `G_` 通用操作码、且虚拟寄存器只带低层类型 LLT。
- 核心流水线是四个 pass：**IRTranslator**（IR→gMIR，几乎逐条翻译，ABI 经 `CallLowering`）→ **Legalizer**（按目标 `LegalizerInfo` 与 `LegalizeAction` 把非法操作塑形成合法）→ **RegBankSelect**（给每个通用 vreg 选寄存器库，Fast/Greedy 两种模式）→ **InstructionSelect**（按目标 `InstructionSelector` 做最终选定，gMIR 变 MIR）。
- 阶段之间靠 **`MachineFunctionProperties`** 建立可校验的契约链：`IsSSA → +Legalized → +RegBankSelected → +Selected`；失败时由 `ResetMachineFunctionPass` 回退到 SelectionDAG。
- IRTranslator 用 `Instruction.def` 宏展开的大 `switch` 做翻译分发；Legalizer 用 InstList/ArtifactList 双工作表 + observer 自动维护，迭代到不动点；InstructionSelect 用「块后序 + 块内逆序」保证使用先于定义被选定。
- 相比 SelectionDAG，GlobalISel 以「更直接的 MIR 集成、更好的可观测性与编译速度」为目标，代价是多一步 RegBankSelect、优化深度仍在追赶，因此保留 fallback 机制。

## 7. 下一步学习建议

- **深入某一阶段的目标侧**：选一个目标（如 AArch64 或 AMDGPU），阅读其 `*LegalizerInfo.cpp`、`*RegisterBank.td`/`*RegisterBankInfo.cpp`、以及 TableGen 生成的 `*GenGlobalISel.inc` 匹配表，理解「目标如何描述自己」。文档对应 `llvm/docs/GlobalISel/Legalizer.rst`、`RegBankSelect.rst`、`InstructionSelect.rst`。
- **学习通用操作码语义**：通读 `llvm/docs/GlobalISel/GenericOpcode.rst`，对照 `GenericMachineInstrs.h` 的包装类，建立 `G_*` 操作码的全景认识。
- **本单元收尾**：下一讲 **u6-l4（MC 层：机器代码与目标文件）** 会接着 InstructionSelect 的产物往下走，看后端如何把选好的目标指令发射成汇编或 ELF/Mach-O/COFF 目标文件，补齐「IR → 机器码」的最后一公里。
- **进阶关联**：GlobalISel 的 Combiner（`llvm/lib/CodeGen/GlobalISel/Combiner*.cpp`）可在 Legalizer 前后插入做模式化简，与本单元 u4 的优化 pass、u6-l2 的 DAGCombiner 思路相通，值得对照阅读。
