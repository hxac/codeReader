# CodeGen 总览与后端流水线

## 1. 本讲目标

本讲是「目标代码生成与后端」单元（u6）的第一篇，目标是从高空俯瞰整个 LLVM 后端：从一份优化后的 LLVM IR，到最终的目标汇编或目标文件，中间到底经历了哪些阶段、由谁编排、用什么数据结构承载。

学完后你应当能够：

- 说清后端的七大约略阶段（指令选择、调度与成型、SSA 层机器码优化、寄存器分配、prolog/epilog 插入、晚期机器码优化、代码发射）的顺序与各自职责。
- 理解 `CodeGenPassBuilder` 这个模板类如何像「装配车间」一样把后端流水线拼接起来，以及它在新 Pass 管理器（New PM）下与 `llc` 的关系。
- 认识后端自己的 IR——`MachineFunction` / `MachineBasicBlock` / `MachineInstr`，并理解它和前端 IR（`Function`/`BasicBlock`/`Instruction`）是两套不同的数据结构。
- 会用 `llc -print-after-all` / `-debug` 等开关，亲眼观察一段 IR 经过后端各阶段后的中间 MachineIR。

本讲只做「总览」，不深入 SelectionDAG / GlobalISel / TableGen 的实现细节——它们是后续 u6-l2、u6-l3、u6-l5 各篇的主题。

## 2. 前置知识

在进入后端之前，请确认你已经理解以下概念（它们分别来自前置讲义）：

- **三段式架构与 IR 的桥梁作用（u2-l1）**：前端 `clang` 产出 LLVM IR，中端 `opt` 优化 IR，后端 `llc` 把 IR 变成机器码。本讲正式打开「后端」这个黑盒。
- **LLVM IR 的层次结构（u3-l1）**：内存中的 IR 是一棵树 `Module ⊃ Function ⊃ BasicBlock ⊃ Instruction`。后端会把这些「高级」对象翻译成另一套「机器级」对象，层次结构惊人地相似。
- **新 Pass 管理器架构（u4-l1）**：`PassManager` 顺序执行变换 pass，`AnalysisManager` 惰性缓存分析结果，`PassBuilder` 注册并装配流水线。后端流水线本质上也是一条 pass 流水线，只是单位从「IR 函数」变成了「MachineFunction」。

下面要频繁出现两个新术语，先在这里建立直觉：

- **目标（Target）**：指一套具体的 CPU 架构后端，如 X86、AArch64、RISCV、MIPS。LLVM 把所有目标无关的后端算法（寄存器分配、调度等）实现在 `llvm/lib/CodeGen/`，而把每个目标特有的描述（指令、寄存器、调用约定）放在 `llvm/lib/Target/<架构>/`。这种分离让新增一个目标只需写「描述」而无需重写算法。
- **MachineIR（MIR）**：后端的中间表示。它长得很像 IR，但操作的是「虚拟寄存器 / 物理寄存器 / 目标指令」而非「SSA 值」。后端流水线的大部分阶段都在改写 MIR。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [llvm/docs/CodeGenerator.md](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/docs/CodeGenerator.md) | 官方「目标无关代码生成器」文档，给出后端的六大组成与七阶段总览，是本讲概念的权威来源。 |
| [llvm/include/llvm/Passes/CodeGenPassBuilder.h](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h) | 新 PM 下的后端流水线「装配车间」。用一个 CRTP 模板类把 IR→MIR→机器码的所有 pass 串起来，是本讲精读的核心。 |
| [llvm/tools/llc/llc.cpp](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/tools/llc/llc.cpp) | `llc` 代码生成驱动的全部源码（一个薄壳）。`main` 解析参数后调用 `compileModule`，后者调用目标的 `addPassesToEmitFile` 装配后端流水线。 |
| [llvm/docs/WritingAnLLVMBackend.md](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/docs/WritingAnLLVMBackend.md) | 「如何编写一个 LLVM 后端」指南，列出了新增后端必须实现的组件与基本步骤，帮助理解 Target 在流水线中的位置。 |
| [llvm/include/llvm/Target/TargetMachine.h](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Target/TargetMachine.h) | `TargetMachine` 抽象基类，定义了 `addPassesToEmitFile` 等后端入口虚函数。 |
| [llvm/include/llvm/CodeGen/MachineFunction.h](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/CodeGen/MachineFunction.h) | 后端 IR 的「函数」容器，持有一组 `MachineBasicBlock`。 |
| [llvm/include/llvm/CodeGen/MachineInstr.h](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/CodeGen/MachineInstr.h) | 后端 IR 的「指令」类，是后端流水线最频繁操作的对象。 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 后端流水线阶段**（回答「后端做了哪些事」）和 **4.2 CodeGenPassBuilder**（回答「这些事由谁、按什么机制编排」）。

### 4.1 后端流水线阶段

#### 4.1.1 概念说明

前端和中端结束时，程序是一棵优化后的 LLVM IR 树（`Module ⊃ Function ⊃ BasicBlock ⊃ Instruction`），指令还是「与具体 CPU 无关」的抽象运算（`add`、`load`、`call`……）。但真实 CPU 只认它自己的指令（如 X86 的 `addl`、`movl`）。后端的任务，就是把这棵抽象 IR 树翻译成一串具体目标指令，并解决「真实硬件才有的麻烦」：寄存器数量有限、调用约定要遵守、栈帧要布置、指令要排好顺序。

官方文档把这套翻译过程归纳为**七个约略阶段**，它们是理解整个后端的骨架：

1. **指令选择（Instruction Selection）**：决定用哪些目标指令来表达 IR。把抽象运算变成目标指令，使用（近乎）无限的虚拟寄存器，保持 SSA 形式。
2. **调度与成型（Scheduling and Formation）**：对选出的指令确定一个执行顺序，并把它们「成型」为真正的 `MachineInstr` 序列。
3. **SSA 层机器码优化（SSA-based Machine Code Optimizations）**：在 SSA 形式的机器码上做可选优化，如机器级窥孔优化、if-conversion。
4. **寄存器分配（Register Allocation）**：把无限的虚拟寄存器映射到目标有限的物理寄存器，必要时插入溢出（spill）代码，消除所有虚拟寄存器引用。
5. **Prolog/Epilog 插入**：栈空间需求确定后，插入函数序言/结语，消除抽象栈帧索引引用，做栈帧指针消除等优化。
6. **晚期机器码优化（Late Machine Code Optimizations）**：在「接近最终」的机器码上做收尾优化，如溢出代码调度、窥孔。
7. **代码发射（Code Emission）**：把最终机器码以目标汇编或二进制机器码形式输出。

> 「约略阶段」（approximate）这个词很重要：真实流水线比这复杂得多，每个阶段都拆成多个 pass，而且目标还可以在任意位置插入自己的私有 pass。这七个阶段是**思维模型**，不是严格的一对一映射。

#### 4.1.2 核心流程

七个阶段可以进一步归并为三大块，形成后端的主干：

```
LLVM IR（机器无关）
   │
   ▼  ── 第一大块：IR → MIR 的「下沉」（addISelPasses）
   │     · IR 上的最后准备：循环强度削弱、常量提升、异常处理 lowering、栈保护
   │     · 指令选择：SelectionDAG / GlobalISel / FastISel 三选一
   │     产物：SSA 形式的 MachineFunction（虚拟寄存器）
   ▼
Machine IR（SSA）
   │
   ▼  ── 第二大块：MIR 优化 + 寄存器分配（addMachinePasses 前半 + addOptimizedRegAlloc）
   │     · SSA 层优化：MachineLICM、MachineCSE、MachineSinking、窥孔
   │     · 寄存器分配：PHI 消除 → 虚拟→物理寄存器 → 重写
   │     · Prolog/Epilog 插入
   │     产物：使用物理寄存器的 MachineFunction
   ▼
Machine IR（物理寄存器）
   │
   ▼  ── 第三大块：晚期优化 + 代码发射（addMachinePasses 后半 + AsmPrinter）
   │     · 块布局（MachineBlockPlacement）、分支折叠、第二次调度
   │     · MC 层发射：目标汇编 .s 或目标文件 .o
   ▼
目标汇编 / 目标文件
```

注意一个关键节点：**寄存器分配之前**，机器码处于「SSA + 无限虚拟寄存器」状态，很多优化此时最方便做（如 `MachineLICM`）；**寄存器分配之后**，虚拟寄存器被替换为物理寄存器、插入了溢出代码，此后的优化要面对真实硬件约束。所以后端流水线经常以「pre-RA / post-RA」来划分阶段。

#### 4.1.3 源码精读

这七个阶段的权威文字定义在官方文档里：

[llvm/docs/CodeGenerator.md:L104-L142](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/docs/CodeGenerator.md#L104-L142) —— 这段把后端「高层设计」明确划分为指令选择、调度与成型、SSA 层机器码优化、寄存器分配、prolog/epilog 插入、晚期机器码优化、代码发射七个阶段，并强调「指令选择器用最优模式匹配来产生高质量本地指令序列」是整套设计的基石。

文档还交代了后端框架的**六大组成**，帮助你区分「谁负责什么」：

[llvm/docs/CodeGenerator.md:L28-L59](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/docs/CodeGenerator.md#L28-L59) —— 六大组成分别是：抽象目标描述接口（`include/llvm/Target/`）、机器码表示类（`include/llvm/CodeGen/`）、MC 层（汇编级构造）、目标无关算法（`lib/CodeGen/`）、具体目标的描述实现（`lib/Target/`）、目标无关 JIT 组件。这正是「算法在 `lib/CodeGen`、描述在 `lib/Target`」这一分工的来源。

`WritingAnLLVMBackend.md` 在「前置阅读」里也复述了同样的阶段清单，并把它和编写新后端的工作联系起来：

[llvm/docs/WritingAnLLVMBackend.md:L42-L46](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/docs/WritingAnLLVMBackend.md#L42-L46) —— 提示读者重点关注指令选择、调度与成型、SSA 层优化、寄存器分配、prolog/epilog 插入、晚期机器码优化、代码发射这些阶段。

接下来看后端 IR 的承载类。后端流水线操作的核心对象是 `MachineInstr`：

[llvm/include/llvm/CodeGen/MachineInstr.h:L65-L74](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/CodeGen/MachineInstr.h#L65-L74) —— `MachineInstr` 是「每条机器指令」的表示，它必须是平凡析构（trivial destructor）的——`MachineFunction` 被销毁时，其内部 `MachineInstr` 直接释放、不调用析构函数。这是为了性能：后端会创建和销毁海量指令。

而 `MachineFunction` 则是这些指令的容器，对应前端 IR 里的 `Function`：

[llvm/include/llvm/CodeGen/MachineFunction.h:L8-L15](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/CodeGen/MachineFunction.h#L8-L15) —— 注释写明：`MachineFunction` 收集一个函数的本地机器码，内含一串 `MachineBasicBlock`，并持有各类目标相关信息。

于是后端也有一棵层次树：`MachineFunction ⊃ MachineBasicBlock ⊃ MachineInstr`，与前端 `Function ⊃ BasicBlock ⊃ Instruction` 几乎一一对应——只是「值」变成了「寄存器/指令操作数」。

#### 4.1.4 代码实践

实践目标：亲眼看到一段 IR 经过后端若干阶段后的中间 MachineIR，验证「七阶段」确实存在。

操作步骤：

1. 准备一段最简单的 IR（示例代码，保存为 `add.ll`）：

   ```llvm
   ; 示例代码：一个返回两数之和的函数
   define i32 @add(i32 %a, i32 %b) {
     %r = add i32 %a, %b
     ret i32 %r
   }
   ```

2. 执行（待本地验证，需要已构建好的 `llc`）：

   ```bash
   llc add.ll -print-after-all -o add.s 2> add.dump
   ```

   `-print-after-all` 会让后端在**每一个 pass 之后**把当时的 `MachineFunction` 打印到 stderr。`2>` 把 stderr 重定向到文件以便翻阅。

3. 在 `add.dump` 里搜索关键字，定位阶段边界：
   - 搜 `IR Translation` / `Generic` —— GlobalISel 的产物（若启用）。
   - 搜 `Register Coalescer`、`Virtual Register Map`、`VirtRegRewriter` —— 寄存器分配相关 pass。注意观察「分配前全是 `%0 %1` 虚拟寄存器，分配后变成 `%eax %ecx` 物理寄存器」。
   - 搜 `Prologue/Epilogue` —— prolog/epilog 插入，你会看到函数头尾多出 `pushq %rbp` 之类。
   - 搜 `Block Placement`、`AsmPrinter` —— 晚期块布局与代码发射。

需要观察的现象：

- 每个 pass 之前有一行 `*** IR Dump After <pass名> ***`（对 IR pass）或对应的 MIR dump banner（对机器 pass），把整条流水线「工序」逐一列出。
- 寄存器分配前后，寄存器命名发生质变（虚拟 → 物理）。

预期结果：你能从 dump 中按顺序数出「指令选择 → SSA 优化 → 寄存器分配 → prolog/epilog → 晚期优化 → 发射」几大段落，与 4.1.2 的流程图对得上。

> 如果只想看阶段名而不想看海量 MIR，可用 `llc -debug-pass=Structure add.ll -o /dev/null`（旧版）或在 `-print-after-all` 的 banner 行里只看 pass 名。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SSA 层机器码优化（如 `MachineLICM`）通常放在寄存器分配**之前**，而不是之后？

参考答案：寄存器分配前机器码还是 SSA 形式、使用无限虚拟寄存器，此时循环不变量外提等优化只需搬运指令、不用担心物理寄存器数量与溢出代码；分配后虚拟寄存器被替换成物理寄存器并插入溢出 `load/store`，再做这些优化既要处理寄存器压力又会打乱溢出代码，难度和正确性风险都高得多。

**练习 2**：后端 IR 的层次树 `MachineFunction ⊃ MachineBasicBlock ⊃ MachineInstr` 和前端 IR 的 `Function ⊃ BasicBlock ⊃ Instruction` 几乎一一对应。既然结构这么像，为什么后端不直接复用前端的 IR 类？

参考答案：两者关注点不同。前端 IR 的 `Instruction` 操作的是「与目标无关的 SSA 值」（类型、`Use` 链、可被任意 `Value` 引用）；而后端 `MachineInstr` 操作的是「寄存器与指令操作数」，要表达物理寄存器约束、调用约定、栈帧索引、指令编码标志等机器级细节。复用会导致前端类背上沉重的目标相关字段，也会让 SSA 优化算法和机器码优化算法纠缠不清，所以后端另起一套机器级 IR。

### 4.2 CodeGenPassBuilder

#### 4.2.1 概念说明

知道了后端「该做哪些事」，下一个问题是：**谁把这些 pass 按正确顺序拼起来？** 答案在新 Pass 管理器下是 `CodeGenPassBuilder`。

回顾 u4-l1：中端用 `PassBuilder` 解析 `-passes=...` 文本、装配优化流水线。后端类似，但更「固定」——后端流水线不是用户随意用文本拼出来的，而是由 `CodeGenPassBuilder` 这个模板类按既定顺序装配，目标（Target）只能通过若干「钩子」在指定位置插私货。

`CodeGenPassBuilder` 的关键设计有三点：

1. **CRTP 模板 + 目标覆写**：它是一个 `template <typename DerivedT, typename TargetMachineT>` 的基类，目标通过 `DerivedT`（自己的具体子类）覆写 `addInstSelector`、`addIRTranslator` 等方法来注入目标特有的 pass，骨架则共享。
2. **三层流水线**：后端 pass 分属 Module / Function / MachineFunction 三个层级，`CodeGenPassBuilder` 内部用一个 `PassManagerWrapper` 同时持有三层管理器，并在合适时机「冲刷」（flush）合并。
3. **大量 Pre/Post 钩子**：基类提供 `addPreRegAlloc`、`addPostRegAlloc`、`addPreEmitPass` 等空实现，目标覆写即可在主阶段前后插入私有 pass，无需重写整条流水线。

#### 4.2.2 核心流程

`CodeGenPassBuilder::buildPipeline` 是总入口，它的执行顺序就是后端流水线的「目录」：

```
buildPipeline(MPM, ...)            # 总入口，产出一条 ModulePassManager 流水线
  │
  ├─ 先 require 若干模块级分析       # MachineModuleAnalysis 等
  ├─ addISelPasses(PMW)            # 第一大块：IR → MIR
  │     ├─ addGlobalMergePass
  │     ├─ addIRPasses             # IR 层最后优化（LSR、常量提升…）
  │     ├─ addCodeGenPrepare       # 为代码生成做准备的 IR→IR 变换
  │     ├─ addPassesToHandleExceptions
  │     └─ addISelPrepare          # 选指前的最后整理 + verify
  ├─ flushFPMsToMPM                # 把累积的 Function pass 冲刷进 Module 流水线
  │
  ├─ addCoreISelPasses(PMW)        # 指令选择（SelectionDAG / FastISel / GlobalISel 三选一）
  │                                 #   产物：SSA 形式的 MachineFunction
  ├─ addMachinePasses(PMW)         # 第二、三大块：MIR 优化 + 寄存器分配 + 晚期优化
  │     ├─ addMachineSSAOptimization
  │     ├─ addPreRegAlloc → addOptimizedRegAlloc → addPostRegAlloc
  │     ├─ PrologEpilogInserter
  │     ├─ addMachineLateOptimization
  │     ├─ 第二次调度 (PostRAScheduler)
  │     ├─ addBlockPlacement
  │     └─ addPreEmitPass2
  │
  └─ addAsmPrinter(PMW)            # 代码发射：AsmPrinter 把 MachineInstr 写成 .s
```

其中指令选择器在 `addCoreISelPasses` 里三选一，逻辑很直白：

```
若用户 -fast-isel                → FastISel
否则若 -global-isel 或目标默认   → GlobalISel
否则若 -O0 且目标想要 fast-isel  → FastISel
否则                             → SelectionDAG（默认）
```

寄存器分配有「快」「优化」两条路径，由优化等级决定：`-O0` 走 `addFastRegAlloc`（无合并、无调度的最小集合），否则走 `addOptimizedRegAlloc`（含存活区间、寄存器合并、pre-RA 调度）。

#### 4.2.3 源码精读

先看这个类的「身份」与构造：

[llvm/include/llvm/Passes/CodeGenPassBuilder.h:L176-L201](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L176-L201) —— `CodeGenPassBuilder` 是 CRTP 模板类，构造时持有 `TargetMachine`、`CGPassBuilderOption`（优化等级、是否用 GlobalISel 等）和回调指针。注释点明：`MachinePassRegistry.def` 描述了所有内置 pass 如何构造，它们在构造时可引用这些成员。

总入口 `buildPipeline`：

[llvm/include/llvm/Passes/CodeGenPassBuilder.h:L572-L636](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L572-L636) —— 这是后端流水线的「目录」。它先 require 一批模块级分析，再依次调 `addISelPasses`、`addCoreISelPasses`、`addMachinePasses`，最后按输出类型决定走 AsmPrinter（打印汇编）还是 PrintMIR（打印 MIR 文本）。

第一大块（IR → MIR 的下沉）由 `addISelPasses` 组织：

[llvm/include/llvm/Passes/CodeGenPassBuilder.h:L698-L718](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L698-L718) —— 依次加入全局合并、（可选）ObjCARC 契约、PreISel 内 intrinsic lowering、`ExpandIRInsts`、`addIRPasses`、`addCodeGenPrepare`、异常处理准备、`addISelPrepare`。注意这些大部分还是**在 IR 上**工作，是「选指之前对 IR 的最后一次整理」。

指令选择三选一的判定逻辑：

[llvm/include/llvm/Passes/CodeGenPassBuilder.h:L882-L895](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L882-L895) —— 用一个 `SelectorType` 枚举在 `SelectionDAG / FastISel / GlobalISel` 之间抉择。这正是后续 u6-l2（SelectionDAG）与 u6-l3（GlobalISel）两讲的分水岭。

后端机器码阶段的总装，注释自称「可读作主要 CodeGen 阶段的标准顺序」：

[llvm/include/llvm/Passes/CodeGenPassBuilder.h:L969-L1092](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L969-L1092) —— `addMachinePasses` 把 SSA 优化、pre-RA、寄存器分配、post-RA、prolog/epilog 插入、晚期优化、第二次调度、块布局、XRay/Patchable、`addPreEmitPass2` 串成一条线。开头 L973 的 `if (getOptLevel() != None)` 决定走优化版还是 `-O0` 版。

SSA 层机器码优化的具体内容：

[llvm/include/llvm/Passes/CodeGenPassBuilder.h:L1094-L1133](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L1094-L1133) —— `addMachineSSAOptimization` 依次加入早期尾复制、PHI 优化、栈着色、死机器指令消除、ILP 优化钩子、`EarlyMachineLICM`、`MachineCSE`、`MachineSinking`、窥孔优化。这些正是 4.1 七阶段里的「SSA 层机器码优化」。

寄存器分配的两条路径：

[llvm/include/llvm/Passes/CodeGenPassBuilder.h:L1213-L1218](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L1213-L1218) —— `addFastRegAlloc` 只加 PHI 消除 + TwoAddress + 快速分配，是最小集合。

[llvm/include/llvm/Passes/CodeGenPassBuilder.h:L1223-L1289](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L1223-L1289) —— `addOptimizedRegAlloc` 则包含死 lane 检测、存活变量/区间分析、寄存器合并、子寄存器重命名、pre-RA 调度（`MachineScheduler`），再调分配与重写，最后还有 `StackSlotColoring`、`MachineCopyPropagation`、post-RA `MachineLICM`。`RAGreedy`（贪心）与 `RegAllocFast` 的抉择在 `addTargetRegisterAllocator` / `addRegAllocPass`：

[llvm/include/llvm/Passes/CodeGenPassBuilder.h:L1149-L1184](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L1149-L1184) —— 按优化等级（或 `-regalloc-npm=`）选择快速还是贪心分配器。

目标插私货的 Pre/Post 钩子，全部是空实现的虚方法：

[llvm/include/llvm/Passes/CodeGenPassBuilder.h:L340-L377](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L340-L377) —— `addPreRegAlloc`、`addPreRewrite`、`addPostRegAlloc`、`addPreSched2`、`addPreEmitPass`、`addPreEmitPass2` 等。注释说明它们是「常见代码流水线里目标可插入 pass 的便利点」。

三层流水线如何合并——这是理解 `CodeGenPassBuilder` 内部机制的关键：

[llvm/include/llvm/Passes/CodeGenPassBuilder.h:L263-L281](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L263-L281) —— `flushFPMsToMPM` 把累积的 `MachineFunctionPassManager` 包进 `createFunctionToMachineFunctionPassAdaptor`，再包进 `createModuleToFunctionPassAdaptor`，挂到 `ModulePassManager` 上。这就把三层单位（Module/Function/MachineFunction）嵌套成了新 PM 能执行的一条流水线。

那么 `llc` 是怎么调到 `CodeGenPassBuilder` 的？默认情况下 `llc` 走的是**旧（legacy）Pass 管理器**路径：

[llvm/tools/llc/llc.cpp:L838-L844](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/tools/llc/llc.cpp#L838-L844) —— `compileModule` 里构建 `legacy::PassManager`，调用 `Target->addPassesToEmitFile(PM, ...)` 装配后端流水线。这条路径的概念阶段与 `CodeGenPassBuilder` 完全一致，只是实现用的是 legacy PM + `TargetPassConfig`。

[llvm/tools/llc/llc.cpp:L752-L757](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/tools/llc/llc.cpp#L752-L757) —— 当传 `-enable-new-pm` 或 `-passes=` 时，`llc` 改走 `compileModuleWithNewPM`，那才是真正用 `CodeGenPassBuilder::buildPipeline` 的新 PM 路径。换句话说，`CodeGenPassBuilder` 是后端流水线的「现代描述」，与 legacy `TargetPassConfig` 描述的是同一件事。

`addPassesToEmitFile` 的真实实现（不在抽象基类，而在 `CodeGenTargetMachineImpl`）：

[llvm/lib/CodeGen/CodeGenTargetMachineImpl.cpp:L232-L255](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/lib/CodeGen/CodeGenTargetMachineImpl.cpp#L232-L255) —— 标准目标的 `addPassesToEmitFile` 调 `addPassesToGenerateCode`（装配选指+机器码流水线），再视情况 `addAsmPrinter` 或 `createPrintMIRPass`，最后加一个 `FreeMachineFunctionPass` 释放内存。

抽象基类里的虚函数声明（默认返回 `true` 表示「不支持」）：

[llvm/include/llvm/Target/TargetMachine.h:L433-L439](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Target/TargetMachine.h#L433-L439) —— `TargetMachine::addPassesToEmitFile` 的默认实现返回 `true`，意为「该目标不支持此文件类型 emission」；标准后端在 `CodeGenTargetMachineImpl` 中覆写它。

最后，`llc` 如何决定优化等级：

[llvm/tools/llc/llc.cpp:L126-L130](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/tools/llc/llc.cpp#L126-L130) —— `-O` 选项默认 `-O2`，这个等级会传给 `TargetMachine`，进而影响 `addMachinePasses` 里 `if (getOptLevel() != None)` 的分支与寄存器分配路径选择。

#### 4.2.4 代码实践

实践目标：用 `llc` 的内置开关「列出」整条后端流水线的工序，再对照 `CodeGenPassBuilder` 的源码确认它们就是 `buildPipeline` 装出来的那些阶段。

操作步骤：

1. 对 4.1.4 的 `add.ll`，运行（待本地验证）：

   ```bash
   # 方式 A：用新 PM 路径打印流水线结构
   llc -enable-new-pm add.ll -o add.s -print-after-all 2> add.newpm.dump

   # 方式 B：切换指令选择器，对比流水线差异
   llc -global-isel add.ll -o add_gisel.s -print-after-all 2> add.gisel.dump
   ```

2. 在两个 dump 里分别搜索：
   - `IRTranslator` / `Legalizer` / `RegBankSelect` / `InstructionSelect` —— 这四个 GlobalISel 阶段（u6-l3 主题）。
   - `SelectionDAG` / `DAG->DAG` —— SelectionDAG 路径的选指（u6-l2 主题）。
   - `Greedy Register Allocator` —— `addOptimizedRegAlloc` 装入的 `RAGreedy`。

3. 对照源码核对：打开 `CodeGenPassBuilder.h` 的 `addMachinePasses`（L969-L1092），把 dump 里依次出现的机器 pass 名，逐个在这段源码里找到对应的 `addMachineFunctionPass(XxxPass(), PMW)` 调用。

需要观察的现象：

- 方式 A（默认 SelectionDAG）与 `-global-isel` 的 dump，在「指令选择」那一段工序完全不同——前者是 DAG 系列，后者是 IRTranslator/Legalizer/RegBankSelect/InstructionSelect 四件套。
- 优化等级会影响流水线长度：试试 `llc -O0 ... -print-after-all`，会发现 `addOptimizedRegAlloc` 里那一堆合并/调度 pass 消失，只剩 `addFastRegAlloc` 的最小集合。

预期结果：你能把 `-print-after-all` 输出的工序序列，与 `CodeGenPassBuilder::buildPipeline` → `addMachinePasses` 的源码调用顺序一一对应，确认「文档里的七阶段」就是「源码里的 pass 序列」。

#### 4.2.5 小练习与答案

**练习 1**：`CodeGenPassBuilder` 为什么要用 CRTP（`template <typename DerivedT, typename TargetMachineT>`）而不是普通虚函数多态？

参考答案：后端 pass 流水线对编译速度极敏感，普通虚函数派发在每个 pass、每条指令上都有开销。CRTP 让基类通过 `derived()` 在编译期静态解析到具体目标子类，把「目标覆写钩子」的调用编译成直接调用甚至内联，既保留了「目标可定制」的灵活性，又消除了运行时虚表开销。

**练习 2**：假设你要给某目标在「寄存器分配之后、prolog/epilog 插入之前」插一个私有 pass，应该覆写 `CodeGenPassBuilder` 的哪个钩子？

参考答案：覆写 `addPostRegAlloc`（[CodeGenPassBuilder.h:L362](https://github.com/llvm/llvm-project/blob/11038cc1618ac1f801e4029b7149f68f3ad949f5/llvm/include/llvm/Passes/CodeGenPassBuilder.h#L362)）。由 `addMachinePasses`（L998）可见，调用顺序是 `addPreRegAlloc → addOptimizedRegAlloc → addPostRegAlloc → ... → PrologEpilogInserter`，正好满足「分配后、prolog/epilog 前」的位置要求。

**练习 3**：`llc` 默认走 legacy PM，而 `CodeGenPassBuilder` 是新 PM 的产物。本讲为什么仍以 `CodeGenPassBuilder.h` 作为精读主文件？

参考答案：两者描述的是**同一套概念流水线**（同样的七阶段、同样的 pass 顺序），而 `CodeGenPassBuilder.h` 是单一头文件、结构清晰、注释完整地呈现了整条流水线的装配逻辑，非常适合作为「后端流水线地图」来读；legacy 路径的实现分散在 `TargetPassConfig` 与各 `addPassesToX` 方法中，更难一览全貌。理解了 `CodeGenPassBuilder`，再读 legacy 路径只是「换了个 PM 容器」。

## 5. 综合实践

把本讲两个模块串起来的小任务：**手绘一张属于你机器的后端流水线图，并用 `llc` 验证它**。

1. **画图（纸笔或文本）**：以本讲 4.1.2 的三大块流程图为底，针对你的本机架构（如 X86-64），在 `addMachinePasses` 的每个主阶段旁标注：它会调用哪些具体 pass、产物是 SSA-MIR 还是物理寄存器 MIR。

2. **生成验证素材**：写一段稍复杂的 IR（含一个循环），例如（示例代码）：

   ```llvm
   ; 示例代码：循环累加，便于观察 LICM、寄存器分配、块布局
   define i32 @sum(i32 %n) {
   entry:
     br label %loop
   loop:
     %i = phi i32 [0, %entry], [%next, %loop]
     %s = phi i32 [0, %entry], [%snext, %loop]
     %snext = add i32 %s, %i
     %next = add i32 %i, 1
     %cmp = icmp slt i32 %next, %n
     br i1 %cmp, label %loop, label %exit
   exit:
     ret i32 %snext
   }
   ```

3. **运行并核对**：`llc -print-after-all sum.ll -o sum.s 2> sum.dump`，在 `sum.dump` 中：
   - 找到 `Machine LICM` / `Early Machine LICM`，确认循环不变量被外提（对应 SSA 层优化）。
   - 找到 `Greedy Register Allocator` 前后，确认 `%0/%1` 虚拟寄存器变成 `%eax/%ecx` 等物理寄存器。
   - 找到 `Machine Block Placement`，确认基本块顺序可能被重排以减少跳转。
   - 找到 `AsmPrinter`，确认最终的 `.s` 汇编就此生成。

4. **反思**：把你画图时标注的 pass，与 dump 实际出现的 pass 比对，找出 1～2 个你画漏的 pass，回到 `CodeGenPassBuilder.h:L969-L1092` 看它属于哪个钩子或主阶段。

这个任务让你把「文档七阶段 → 源码装配 → 实际 dump」三者对齐，是后续深入 SelectionDAG（u6-l2）、GlobalISel（u6-l3）、MC 层（u6-l4）、TableGen（u6-l5）的地基。

## 6. 本讲小结

- 后端把机器无关的 LLVM IR 翻译成具体目标机器码，官方归纳为七大约略阶段：指令选择、调度与成型、SSA 层机器码优化、寄存器分配、prolog/epilog 插入、晚期机器码优化、代码发射。
- 后端有自己的 IR：`MachineFunction ⊃ MachineBasicBlock ⊃ MachineInstr`，与前端 IR 层次结构相似但关注「寄存器与机器指令操作数」，不复用前端类。
- `CodeGenPassBuilder` 是新 PM 下后端流水线的「装配车间」，用 CRTP 模板让目标共享骨架、通过钩子定制；其 `buildPipeline` → `addISelPasses` / `addCoreISelPasses` / `addMachinePasses` 就是流水线的目录。
- 流水线常以「pre-RA / post-RA」划分：分配前是 SSA + 无限虚拟寄存器（做 LICM/CSE/Sinking），分配后是物理寄存器 + 溢出代码（做块布局、第二次调度）。
- 指令选择在 `addCoreISelPasses` 三选一（SelectionDAG / FastISel / GlobalISel），这是后续两讲的分水岭。
- `llc` 默认走 legacy PM（`Target->addPassesToEmitFile`），但描述的是同一套概念流水线；`-enable-new-pm` 才直接用 `CodeGenPassBuilder`。`-print-after-all` 是观察流水线的最佳工具。

## 7. 下一步学习建议

本讲只给了后端的「骨架」，每个阶段内部都有大量精妙设计。建议按以下顺序深入：

- **u6-l2 SelectionDAG 指令选择与调度**：打开 `addCoreISelPasses` 里 `SelectionDAG` 这一支的黑盒，看 IR 如何下沉为 DAG、如何合法化、如何模式匹配选指。
- **u6-l3 GlobalISel 新一代指令选择**：对应 `addIRTranslator` / `addLegalizeMachineIR` / `addRegBankSelect` / `addGlobalInstructionSelect` 四阶段，理解它为何要取代 SelectionDAG。
- **u6-l4 MC 层：机器代码与目标文件**：打开 `addAsmPrinter` 与代码发射阶段，看 `MachineInstr` 如何经 MC 层变成 `.s` / `.o`。
- **u6-l5 TableGen 与目标描述**：理解 `lib/Target/<架构>/*.td` 如何描述指令/寄存器/调用约定，并被选指器消费——这是 `WritingAnLLVMBackend.md` 列出的「基本步骤」背后的引擎。

在进入下一篇之前，建议先用本讲的 `-print-after-all` 实践，把你机器上某段简单 IR 的后端 dump 通读一遍，建立「眼见为实」的直觉，再去读各阶段的源码会事半功倍。
