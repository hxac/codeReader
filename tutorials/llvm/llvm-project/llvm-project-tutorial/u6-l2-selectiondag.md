# SelectionDAG 指令选择与调度

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **SelectionDAG** 这一经典指令选择框架「为什么用 DAG」、它解决了什么问题；
- 描述一条 LLVM IR 指令是如何被 `SelectionDAGBuilder` **下沉（lower）** 成 DAG 节点的；
- 解释**合法化（legalization）**的两个层次——类型合法化与操作合法化，以及 `Legal / Promote / Expand / Custom` 四种处置动作；
- 理解指令选择本质上是一台**字节码模式匹配解释器**，知道 `OPC_Scope` 的回溯与 `OPC_MorphNodeTo` 的「就地变形」如何把通用节点变成目标机器节点；
- 认识指令调度（scheduling）与发射（emit）在流水线末尾的角色；
- 会用 `llc -debug-only=isel` 观察一段函数经过 SelectionDAG 各阶段的中间产物。

本讲承接 [u6-l1（后端总览）](u6-l1-codegen-overview.md)。u6-l1 已告诉我们：后端流水线里「指令选择」这一步有 SelectionDAG / FastISel / GlobalISel 三选一，由 `CodeGenPassBuilder::addCoreISelPasses` 决定走哪条路（详见 u6-l3 的 GlobalISel）。本讲专门拆开**最经典、最成熟**的 SelectionDAG 这条路。

## 2. 前置知识

在进入源码前，先用直觉建立两个观念。

**为什么指令选择要先把 IR 变成 DAG？** 前端 IR（`Module/Function/BasicBlock/Instruction`，见 u3）是线性的指令序列，适合「按顺序遍历做变换」。但「选出目标指令」这件事天然是一个**模式匹配**问题：编译器作者想表达的规则形如「只要看到 `(add (shl x, 1), y)` 这样的计算图，就生成一条 `LEA` 指令」。这种「计算子图 → 一条机器指令」的规则，用**有向无环图（DAG）**来描述最自然——每个节点是一次运算，边是数据依赖。TableGen（见 u6-l5）正是把这些 `.td` 里的模式规则编译成一张紧凑的**匹配表（MatcherTable）**，供选择器查表。所以「先变 DAG，再做选择」是为了让目标描述能用规则驱动。

**chain 与 glue（副作用怎么排序）**。DAG 理论上「无环」，但真实程序里有内存读写、调用等副作用，它们的顺序不能乱。SelectionDAG 用两类特殊边来固定顺序：

- **chain**（类型为 `MVT::Other`）：串起所有有副作用的节点（load / store / call …），形成一个偏序。入口节点 `EntryToken` 是链的起点，多个链可用 `TokenFactor` 合并。
- **glue**（类型为 `MVT::Glue`）：比 chain 更强的「紧邻」约束，常用于「这条指令的下一条必须紧跟着」（如 `cmp` 后接条件跳转），且**不参与 CSE**（公共子表达式消除），因为 glue 边是唯一的。

记住这两条边，后面看源码时就不会被 `op_values()` 里类型为 `Other` 的操作数搞糊涂。

> 关键术语：**SDNode**（DAG 节点）、**SDValue**（节点 + 结果编号）、**ISD 操作码**（目标无关的节点种类，如 `ISD::ADD`/`ISD::LOAD`）、**合法化（legalization）**、**匹配表（MatcherTable）**、**SUnit**（调度单元）、**MachineSDNode**（已选中、带目标操作码的节点）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp` | 整个 SelectionDAG 流水线的**驱动**：`runOnMachineFunction → SelectAllBasicBlocks → SelectBasicBlock → CodeGenAndEmitDAG`；以及模式匹配解释器 `SelectCodeCommon`。 |
| `llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp` | **DAG 构建**：把 LLVM IR 一条条翻译成 DAG 节点（`visit*` 系列、`getValue`/`setValue`）。 |
| `llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp` | **操作合法化**：`SelectionDAG::Legalize()` 驱动 `LegalizeOp`，按 `Legal/Promote/Expand/Custom` 处置每个节点。 |
| `llvm/lib/CodeGen/SelectionDAG/LegalizeTypes.cpp` | **类型合法化**：`SelectionDAG::LegalizeTypes()`，把目标不支持的大小/向量类型改写成合法类型。 |
| `llvm/lib/CodeGen/SelectionDAG/SelectionDAG.cpp` | DAG 容器与**节点工厂**：`getNode`（带 CSE）、`getMachineNode`、`Combine` 等。 |
| `llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.cpp` | **调度**：把选好的 MachineSDNode 组织成 `SUnit` 依赖图并排出指令顺序。 |
| `llvm/include/llvm/CodeGen/SelectionDAGNodes.h` | `SDValue`、`SDNode` 数据结构定义。 |

> 说明：SelectionDAG 是一个**按基本块（BasicBlock）**工作的框架——每处理完一个基本块就把 DAG 销毁重建，因此「DAG」是「基本块级」的临时数据结构，不是全函数的。

---

## 4. 核心概念与源码讲解

### 4.1 SelectionDAG 数据模型：SDNode、SDValue 与 ISD 节点

#### 4.1.1 概念说明

理解整条流水线之前，先认清它的「积木」。SelectionDAG 里的运算单位是 **SDNode**，它和前端 IR 的 `Instruction` 一样表示一次运算，但有两个关键不同：

1. **可以是多值的**：一个 SDNode 可以同时产出多个结果（例如 `LOAD` 既产出加载的数据，又产出更新后的 chain）。因此「使用一个值」不能用裸指针 `SDNode*`，必须再带上「第几个结果」，这就是 **SDValue**。
2. **操作码分两层**：选择前是目标无关的 **ISD 操作码**（`ISD::ADD`、`ISD::LOAD`、`ISD::BR`…），选择后变形为**目标操作码**（如 X86 的 `MOV32rr`），存进 `MachineSDNode`。

另一个贯穿全程的设计是 **CSE（公共子表达式消除）**：DAG 里的等价节点会被「折叠」成同一个对象。这与前端 IR 不同——前端 IR 靠单独的 pass（见 u4-l3 的 GVN）做去重，而 SelectionDAG 在**建节点时就顺手去重**，因为建图阶段保持 DAG 无环、等价即同对象最省事。

#### 4.1.2 核心流程

- `SDValue` = `(SDNode* Node, unsigned ResNo)`：一个值引用。
- `SDNode` 持有：操作码 `NodeType`、`SDNodeFlags`、操作数数组、若干结果类型（`EVT`）。
- 建节点走 `SelectionDAG::getNode(Opcode, DL, VT, operands...)`：先用 `(Opcode, 结果类型们, 操作数们)` 算一个 **FoldingSet 哈希键**，在 `CSEMap` 里查；查到就返回已有节点，查不到才新建并入表。这保证「结构相同的节点全局唯一」。
- 等价判定（CSE 键）可写成：

\[
\text{eq}(N_1, N_2) \iff \text{Op}(N_1){=}\text{Op}(N_2)\ \land\ \text{VTs}(N_1){=}\text{VTs}(N_2)\ \land\ \bigwedge_{i}\ \text{Opnd}_i(N_1){=}\text{Opnd}_i(N_2)
\]

产生 glue 结果的节点**不参与 CSE**（glue 边天然唯一，强行去重会破坏顺序约束）。

#### 4.1.3 源码精读

`SDValue` 极其精简，就是「节点指针 + 结果号」：

[llvm/include/llvm/CodeGen/SelectionDAGNodes.h:147-156](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/SelectionDAGNodes.h#L147-L156) —— `SDValue` 持有 `SDNode* Node` 与 `unsigned ResNo`，`ResNo` 指明用的是节点的第几个返回值。

[llvm/include/llvm/CodeGen/SelectionDAGNodes.h:510-516](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/SelectionDAGNodes.h#L510-L516) —— `SDNode` 的核心字段：`int32_t NodeType`（操作码）与 `SDNodeFlags Flags`（nuw/nsw/exact 等标志，与前端 IR 的 `OverflowingBinaryOperator` 对应）。

CSE 的键构造与去重逻辑：

[llvm/lib/CodeGen/SelectionDAG/SelectionDAG.cpp:737-773](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAG.cpp#L737-L773) —— `AddNodeIDOpcode/AddNodeIDValueTypes/AddNodeIDOperands/AddNodeIDNode` 把「操作码 + 类型 + 操作数」依次累加进 `FoldingSetNodeID`，这就是上面公式里那三项的代码实现。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAG.cpp:1034-1049](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAG.cpp#L1034-L1049) —— `doNotCSE` 决定哪些节点不去重：产出 glue 的、以及若干特殊节点一律跳过 CSE。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAG.cpp:7080-7090](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAG.cpp#L7080-L7090) —— `getNode` 的一个典型重载：算 ID → `CSEMap.InsertNode`，命中即复用、未命中才真正分配节点。`getNode` 在 `SelectionDAG.cpp` 里有上百个重载，但套路一致。

#### 4.1.4 代码实践

**目标**：用源码阅读确认「CSE 键 = 操作码 + 类型 + 操作数」三件套。

1. 打开 `SelectionDAG.cpp` 第 737 行起的四个 `AddNodeID*` 辅助函数，逐一确认它们分别往 `FoldingSetNodeID` 里加了什么。
2. 再看 `SDNode::Profile`（同文件约 14047 行）——`Profile` 是 `FoldingSetNode` 协议要求的方法，它内部正是调用 `AddNodeIDNode`。
3. **观察**：你会看到 `Profile` 没有把 `SDNodeFlags`（如 `nsw`）作为 CSE 键的一部分——想一想这意味着 `add nsw %a, %b` 与 `add %a, %b` 在 CSE 后是否可能合并？这会引出一个有趣的设计权衡。
4. **预期结果**：能口述出 CSE 键由哪三部分组成，以及 flags 不进键这一事实。**待本地验证** flags 是否真的不进键（不同 LLVM 版本可能调整）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SDValue` 要带 `ResNo`，而前端 IR 里 `Value*` 不用带？
**答案**：前端 IR 的每条 `Instruction` 只产出一个 `Value`（结构体返回值等少数例外用参数），一对一故裸指针够用；SDNode 可一次产出多个值（数据 + chain + glue），必须用结果号区分引用的是哪一个。

**练习 2**：产生 glue 结果的节点为何不参与 CSE？
**答案**：glue 表达的是「必须紧邻」的强顺序约束，是按出现**唯一**的。若把两个看似相同的 glue 产出节点合并，会让本不相干的指令被错误地串成紧邻关系，破坏语义。

---

### 4.2 指令选择总览：`CodeGenAndEmitDAG` 的流水线

#### 4.2.1 概念说明

每个基本块的处理都遵循同一条固定流水线，其「总调度」就是 `SelectionDAGISel::CodeGenAndEmitDAG`。理解了这个函数，就理解了 SelectionDAG 的全部阶段。流水线的本质是：**先把 IR 变成一张「什么都有」的通用 DAG，然后一步步把它「修剪」成只含目标合法操作、最后只剩目标机器指令的 DAG。**

#### 4.2.2 核心流程

`CodeGenAndEmitDAG` 对一个基本块顺序执行（括号内为源码里的计时区域名）：

```
0. (入口) DAG 已由 SelectBasicBlock 调用 SelectionDAGBuilder 建好
1. dag-combine1   : DAGCombine(BeforeLegalizeTypes)   通用化简（删冗余、强度削减）
2. legalize-types : LegalizeTypes()                    类型合法化
3. dag-combine-lt : DAGCombine(AfterLegalizeTypes)     类型合法化后再化简（若上步有改动）
4. legalize-vec   : LegalizeVectors()                  向量操作合法化
5. dag-combine-lv : DAGCombine(AfterLegalizeVectorOps) 向量合法化后再化简（若有改动）+ 再 LegalizeTypes
6. legalize       : Legalize()                         操作合法化（核心）
7. dag-combine2   : DAGCombine(AfterLegalizeDAG)       合法化后化简
8. isel           : DoInstructionSelection()           指令选择（模式匹配）
9. sched          : Scheduler->Run()                   指令调度
10. emit          : Scheduler->EmitSchedule()          发射成 MachineInstr 序列
11. cleanup       : CurDAG->clear()                    销毁本块的 DAG
```

可以把它归并为三大块：**① 建图与通用化简（1）→ ② 合法化到「只剩合法物」（2–7）→ ③ 选择、调度、发射（8–10）**。注意「combine」穿插出现三次（combine1 / combine-lt / combine2），因为每经过一道会改写 DAG 的阶段（合法化），都可能暴露新的化简机会，所以要重新跑一遍 `DAGCombiner`。

#### 4.2.3 源码精读

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:947-1000](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L947-L1000) —— `CodeGenAndEagDAG` 开头：先 `CurDAG->Combine(BeforeLegalizeTypes, ...)`（combine1，984–989 行），再进入类型合法化。注意第 955 行 `CurDAG->NewNodesMustHaveLegalTypes = false`——合法化前允许建任意类型的节点。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:1006-1046](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L1006-L1046) —— 类型合法化 `LegalizeTypes()`（1010 行）+ 仅当 `Changed` 时才跑 combine-lt（1034 行）。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:1102-1153](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L1102-L1153) —— 操作合法化 `Legalize()`（1108 行）→ combine2（1128 行）→ 指令选择 `DoInstructionSelection()`（1152 行）。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:1160-1200](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L1160-L1200) —— 调度 `Scheduler->Run(CurDAG, MBB)`（1168 行）→ 发射 `EmitSchedule`（1183 行）→ `CurDAG->clear()`（1199 行）销毁本块 DAG，为下一块腾位。

可视化与转储开关（实践会用到）：

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:148-189](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L148-L189) —— 一组 `cl::opt`：`-view-dag-combine1-dags`、`-view-legalize-types-dags`、`-view-isel-dags`、`-view-sched-dags` 等可在各阶段弹出 DAG 图；`-dump-sorted-dags` 让文本转储按拓扑排序。注意它们被 `#ifndef NDEBUG` 包裹——**只在断言版（assertions-enabled）构建里存在**。

#### 4.2.4 代码实践

**目标**：跑通一次 `llc -debug-only=isel`，把上面 11 步流水线的中间产物逐一对上号。

1. 准备一个极小 IR 文件 `t.ll`：

   ```llvm
   define i32 @f(i32 %a, i32 %b) {
     %s = add i32 %a, %b
     ret i32 %s
   }
   ```
2. 执行（要求你构建的 LLVM 带断言）：

   ```bash
   llc -debug-only=isel t.ll -o /dev/null
   ```
3. **观察**：标准错误里会按顺序出现形如 `Initial selection DAG` → `Optimized lowered selection DAG`（combine1 后）→ `Type-legalized selection DAG` → … → `Legalized selection DAG` → `Optimized legalized selection DAG`（combine2 后）→ `Selected selection DAG` 的转储，每个标题对应上面一个阶段。
4. **预期结果**：能在输出里数出至少「combine1 / legalize-types / legalize / combine2 / isel」几个标题，并理解它们正是流水线的检查点。若你的构建未开断言，`-debug-only` 不会有任何输出——这是预期行为，请用 `-DCMAKE_BUILD_TYPE=Debug` 或 `LLVM_ENABLE_ASSERTIONS=ON` 重建（见 u1-l3）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 combine 要在流水线里出现三次（combine1 / combine-lt 或 combine-lv / combine2），而不是只在最后跑一次？
**答案**：合法化会改写 DAG（把非法类型/操作替换成合法的等价物），改写后常暴露新的化简机会（如展开后出现可合并的指令）。在每个改写点之后重跑一次 combine，能更早消除冗余、减小后续阶段的图规模；只在最后跑一次会错过这些机会。

**练习 2**：流水线末尾 `CurDAG->clear()`（1199 行）说明了 SelectionDAG 的什么生命周期特征？
**答案**：DAG 是**基本块级**的临时数据结构——每处理完一个基本块就销毁重建，不会把全函数的 DAG 都留在内存里。

---

### 4.3 DAG 构建：IR → DAG（SelectionDAGBuilder）

#### 4.3.1 概念说明

流水线第 0 步「建图」由 `SelectionDAGBuilder`（简称 SDB）完成。它的职责是把一条条 LLVM IR 指令翻译成 SDNode。翻译遵循一个简单而强大的模式：**对 IR 指令的操作数递归求值（得到 SDValue），再用 `DAG.getNode` 造出本指令对应的节点，最后登记结果**。这和 Kaleidoscope 教程里「每个 AST 节点挂一个 `codegen()`」的思路完全同构（见 u2-l3）。

#### 4.3.2 核心流程

- 入口 `SelectBasicBlock` 对基本块里的每条 IR 指令调 `SDB->visit(*I)`，直到遇到尾调用为止；最后用 `SDB->getControlRoot()` 汇出 DAG 的根 chain，交给 `CodeGenAndEmitDAG`。
- `visit(I)` 内部转发到 `visit(Opcode, I)`，后者是一个用宏从 `Instruction.def` 展开出的**巨型 switch**：每个 IR 操作码分派到对应的 `visit##OPCODE`（如 `Instruction::Add → visitAdd`）。
- `visitBinary` 是绝大多数二元运算（add/sub/mul/and/or…）的统一落点：取操作数的 SDValue、提取 `SDNodeFlags`（nuw/nsw/exact/disjoint/FP 元数据）、`DAG.getNode`、`setValue`。
- `getValue(V)` 是「值缓存」：先查 `NodeMap`，命中直接返回已有 SDValue，避免对同一个 IR 值重复建子图（这也是 DAG 而非树的关键——共享子表达式天然形成 DAG）。

#### 4.3.3 源码精读

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:884-908](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L884-L908) —— `SelectBasicBlock`：循环对每条指令 `SDB->visit(*I)`（895 行），结束处 `CurDAG->setRoot(SDB->getControlRoot())` 设根（901 行），再调 `CodeGenAndEmitDAG()`（907 行）。注意它跳过 PHI（PHI 在基本块边界由寄存器处理）。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp:1424-1434](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp#L1424-L1434) —— `visit(Opcode, I)` 用 `HANDLE_INST` 宏（从 `llvm/IR/Instruction.def` 展开）生成 switch，把 `Instruction::Add` 之类映射到 `visitAdd((const BinaryOperator&)I)`。注释说明它没有用 `InstVisitor`，是因为还要兼容 `ConstantExpr`。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp:1798-1815](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp#L1798-L1815) —— `getValue(V)`：先查 `NodeMap` 缓存（1802–1803 行），再查已分配的虚拟寄存器（`getCopyFromRegs`），都没有才 `getValueImpl` 真正建节点并入缓存。这是「DAG 自然共享子表达式」的源头。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp:3735-3753](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp#L3735-L3753) —— `visitBinary` 的典型骨架（精简后）：

```cpp
// 示例代码（摘自源码，保留关键行）
SDValue Op1 = getValue(I.getOperand(0));      // 递归求值左操作数
SDValue Op2 = getValue(I.getOperand(1));      // 递归求值右操作数
SDValue BinNodeValue = DAG.getNode(Opcode, getCurSDLoc(),
                                   Op1.getValueType(), Op1, Op2, Flags);
setValue(&I, BinNodeValue);                    // 登记结果供后续指令 getValue
```

这就是「操作数 → getNode → setValue」的三段式，几乎所有 `visit*` 都是它的变体。

#### 4.3.4 代码实践

**目标**：跟踪一条 `add` 从 IR 到 DAG 节点的全过程。

1. 在 `t.ll` 里把函数体改成 `%s = add nsw i32 %a, %b; %d = mul i32 %s, %s; ret i32 %d`（引入共享子表达式 `%s` 和 `nsw` 标志）。
2. `llc -debug-only=isel t.ll -o /dev/null 2>&1 | head -60`。
3. **观察**：在 `Initial selection DAG` 转储里找到一个 `add` 节点和一个 `mul` 节点；`mul` 的两个操作数应指向**同一个** `add` 节点（因为 `%s` 被 `%d` 用了两次，`getValue` 缓存命中 → DAG 共享）。
4. **预期结果**：能指出 `mul` 的两个入边连到同一个 `add` 节点，从而直观看到「DAG ≠ 树」、CSE 在建图阶段就已生效。`nsw` 标志应出现在 `add` 节点的 flags 上。

#### 4.3.5 小练习与答案

**练习 1**：`visitBinary` 里为什么要先 `getValue(operand)` 再 `getNode`，而不是反过来？
**答案**：DAG 节点的操作数必须是已存在的 SDValue（边指向已建好的节点）。`getValue` 递归确保操作数对应的子图先建好，`getNode` 才能把它们连成本节点的入边——这对应了「先有子节点、后有父节点」的构造顺序。

**练习 2**：`NodeMap`（`getValue` 的缓存）与 `CSEMap`（`getNode` 的去重）是同一个东西吗？
**答案**：不是。`NodeMap` 是 **IR Value → SDValue** 的映射，避免对同一个 IR 值重复翻译；`CSEMap` 是 **(操作码,类型,操作数) → SDNode** 的映射，在 `getNode` 内部做结构去重。两者协同：`NodeMap` 在「翻译层」共享，`CSEMap` 在「建图层」共享。

---

### 4.4 合法化：让 DAG 适配目标（LegalizeTypes + Legalize）

#### 4.4.1 概念说明

建出来的 DAG 是「目标无关」的——它可能含有目标根本不支持的类型（如 64 位目标上的 `i128`、某目标不支持的 `i1`）和操作（如某目标没有硬件除法、没有某向量算术）。**合法化（legalization）** 就是把这些「非法」的节点，**改写成只由目标合法元素组成的等价 DAG**。

合法化分两个层次：

- **类型合法化（type legalization，`LegalizeTypes`）**：先解决「类型」问题。对每个不合法的类型，目标通过 `TargetLowering` 声明一种处置动作。
- **操作合法化（operation legalization，`Legalize`）**：类型都合法后，再解决「操作」问题——某操作在该类型上目标是否支持。

#### 4.4.2 核心流程

**类型合法化的动作**（在 `TargetLowering::LegalizeTypeAction` 里枚举）：

| 动作 | 含义 |
| --- | --- |
| `TypeLegal` | 类型本就合法，不动。 |
| `TypePromoteInteger` | 小整数提升到合法宽度（如 `i8→i32`）。 |
| `TypeExpandInteger` | 大整数拆成两个合法整数（如 `i64` 在 32 位目标上拆成两个 `i32`）。 |
| `TypeScalarizeVector` | 向量拆成标量逐元素处理。 |
| `TypeSplitVector` | 向量拆成两半窄向量。 |
| `TypeWidenVector` | 窄向量扩宽到合法宽度。 |

**操作合法化的动作**（`TargetLowering::LegalizeAction`）：`Legal`（合法，不动）、`Promote`（提升操作数类型）、`Expand`（展开成更基本的合法操作序列）、`Custom`（交给目标的 `LowerOperation` 自己处理）、`LibCall`（替换成运行库调用）。

两层合法化都遵循同一驱动套路：**按拓扑序遍历所有节点，对每个待合法化节点查目标动作并改写，改写产生的新节点可能仍需合法化，于是反复迭代直到不动点。**

#### 4.4.3 源码精读

[llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp:6315-6362](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp#L6315-L6362) —— `SelectionDAG::Legalize()`：先 `AssignTopologicalOrder`（6316 行），然后 `while(true)` 反复扫描（6333 行），用 `LegalizedNodes` 集合记录已处理节点，对每个新节点调 `Legalizer.LegalizeOp(N)`（6347 行），直到一轮下来没有新节点被合法化（`AnyLegalized` 为假）才退出。这是典型的「迭代到不动点」。

[llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp:985-1017](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp#L985-L1017) —— `LegalizeOp` 开头：先断言「进来的节点类型已经合法」（993–1004 行，这是**类型合法化必须先于操作合法化**的保证），再按操作码种类用 `TLI.getOperationAction` 查询动作。

[llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp:1344-1393](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp#L1344-L1393) —— 动作分派的精髓：

```cpp
// 示例代码（摘自源码 switch 的关键分支）
case TargetLowering::Legal:    return;                       // 合法，不动
case TargetLowering::Custom:                                   // 目标自定义
  if (SDValue Res = TLI.LowerOperation(SDValue(Node,0), DAG)) { /* 替换 */ return; }
  [[fallthrough]];                                             // 自定义失败则降级到 Expand
case TargetLowering::Expand:                                   // 展开
  if (ExpandNode(Node)) return;
  [[fallthrough]];
case TargetLowering::LibCall: ConvertNodeToLibcall(Node); return; // 库调用
case TargetLowering::Promote: PromoteNode(Node); return;          // 提升
```

注意 `Custom → Expand → LibCall` 的 `[[fallthrough]]` 链：自定义处理失败就退化为展开，展开仍不行就退化为库调用——逐级「兜底」。

[llvm/lib/CodeGen/SelectionDAG/LegalizeTypes.cpp:252-296](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/LegalizeTypes.cpp#L252-L296) —— 类型合法化的动作分派：`TypeLegal / TypePromoteInteger / TypeExpandInteger / TypeScalarizeVector / TypeSplitVector / TypeWidenVector` 各自走一条改写路径。

[llvm/lib/CodeGen/SelectionDAG/LegalizeTypes.cpp:1051-1051](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/LegalizeTypes.cpp#L1051-L1051) —— `SelectionDAG::LegalizeTypes()` 入口，驱动上述类型改写到不动点。

#### 4.4.4 代码实践

**目标**：观察「合法化前后 DAG 的差异」，理解 Expand 动作。

1. 改 `t.ll` 为一个目标不直接支持的操作，例如在 32 位目标上做 64 位除法：

   ```llvm
   define i64 @g(i64 %a, i64 %b) {
     %q = sdiv i64 %a, %b
     ret i64 %q
   }
   ```
2. `llc -mtriple=i386-linux-gnu -debug-only=isel t.ll -o /dev/null 2>&1 | grep -A3 -iE "legaliz|sdiv"`
3. **观察**：你会看到 `sdiv i64` 在合法化阶段被展开（Expand）为一系列 32 位操作（或库调用 `__divdi3`），`Legalized selection DAG` 里原来的单节点变成了多个节点。
4. **预期结果**：能描述「一条 `sdiv i64` 被合法化成什么」——这取决于目标（i386 上通常是 `LibCall` 调 `__divdi3`，或在有硬件除法时 Expand 成若干 `div`）。若看不清细节，标注「待本地验证」具体展开形式。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `LegalizeOp` 开头要 `assert` 所有进入的节点类型都已合法？
**答案**：合法化分两层：先类型、后操作。类型合法化（`LegalizeTypes`）已在流水线里先跑过（见 4.2 第 2 步），所以轮到操作合法化时，所有节点的类型必须已经合法；这个断言正是该顺序约束的运行时检查。

**练习 2**：`Custom` 动作失败时会发生什么？
**答案**：`[[fallthrough]]` 落到 `Expand`，尝试用目标无关的展开规则改写；若仍不行，再落到 `LibCall` 替换成运行库调用。即「自定义 → 展开 → 库调用」逐级兜底，保证任何操作最终都能被合法化。

---

### 4.5 指令选择、调度与发射

#### 4.5.1 概念说明

合法化后，DAG 里只剩下「目标支持的操作和类型」，但节点用的仍是**通用 ISD 操作码**（如 `ISD::ADD`）。**指令选择（instruction selection）** 就是把这些通用节点替换成**具体的目标机器指令**（如 X86 的 `ADD32rr`）。它靠 TableGen（u6-l5）把 `.td` 里写的一大堆「模式 → 指令」规则编译成一张**匹配表（MatcherTable）**，再用一个**字节码解释器**去查这张表——这就是 `SelectCodeCommon`。

选择完成后，节点都已变成 `MachineSDNode`（带目标操作码）。接下来**指令调度（scheduling）** 把这些节点排成一个线性指令序列：DAG 只表达「数据依赖」，但真实 CPU 有流水线、多发射、延迟槽，调度器要在依赖约束下重排指令以提升指令级并行。最后**发射（emit）** 把排好序的 `MachineSDNode` 转成 `MachineInstr`（u6-l1 里的后端 IR），写进 `MachineBasicBlock`。

#### 4.5.2 核心流程

- `DoInstructionSelection` 先 `AssignTopologicalOrder` 给节点编号，再**从 DAG 根（root）向前逆序遍历**到入口节点，对每个未处理节点调虚函数 `Select(Node)`。
- `Select` 是每个目标必须实现的纯虚方法；目标生成器（由 TableGen 产出）实现 `SelectCode`，它转调通用的 `SelectCodeCommon(Node, MatcherTable, ...)`。
- `SelectCodeCommon` 是一台**字节码机**：它按 `MatcherTable` 里的 `OPC_*` 字节码逐条执行——`OPC_Scope` 表示「尝试一组候选模式之一」（带回溯），`OPC_RecordNode/RecordChildN` 记录匹配到的节点备用，`OPC_CheckOpcode/CheckPredicate` 校验，`OPC_MorphNodeTo` 把匹配到的通用节点**就地变形**成目标 `MachineSDNode`，`OPC_CompleteMatch` 收尾。
- 调度由 `CreateScheduler()` 选一个调度器（默认启发式 `ISHeuristic`），`Scheduler->Run` 建 SUnit 依赖图并排序，`EmitSchedule` 落成 `MachineInstr`。

#### 4.5.3 源码精读

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:1287-1392](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L1287-L1392) —— `DoInstructionSelection`：1297 行 `AssignTopologicalOrder` 编号；1314–1384 行从根逆序遍历，对每个未死节点调 `Select(Node)`（1383 行）。注释（1310–1313 行）点明遍历方向：从链表尾（根）走向头（入口节点）。

[llvm/include/llvm/CodeGen/SelectionDAGISel.h:114-114](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/include/llvm/CodeGen/SelectionDAGISel.h#L114-L114) —— `virtual void Select(SDNode *N) = 0;`：每个目标的纯虚选择入口。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:3354-3443](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L3354-L3443) —— `SelectCodeCommon` 开头：先用一个大 switch 把一批「无需模式匹配」的特殊节点（`EntryToken`、`Register`、`CopyFromReg`、`INLINEASM`、`UNDEF`…）直接处理掉（3362–3442 行），剩下普通节点才进入字节码匹配主循环。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:3524-3581](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L3524-L3581) —— `OPC_Scope`：模式匹配的回溯核心。每个候选模式有自己的 `NumToSkip`（失败时跳到下一个候选的偏移）；解释器先用 `IsPredicateKnownToFail` 快速判断能否在不压栈的情况下直接判否（3551 行），若不能则 `MatchScopes.emplace_back()` 压一个回溯点（3572 行）——失败时回滚到 `FailIndex` 试下一个候选。这就是「多模式择优匹配」的实现。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:4395-4484](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L4395-L4484) —— `OPC_MorphNodeTo` 的关键：4402 行 `getMachineNode(...)` 或 4421 行 `MorphNode(...)` 把通用节点变形成目标 `MachineSDNode`；4478 行 `if (IsMorphNodeTo) { UpdateChains(...); return; }` 表示变形成功即完成本节点选择。对比 `OPC_EmitNode`（新建节点，可能继续匹配更大模式）与 `OPC_MorphNodeTo`（就地变形，立即收尾）。

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:4486-4505](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L4486-L4505) —— `OPC_CompleteMatch`：模式整体匹配成功后，把新节点结果「接」回原 `NodeToMatch` 的使用者。

调度与发射：

[llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp:2233-2235](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp#L2233-L2235) —— `CreateScheduler()` 默认返回 `ISHeuristic(this, OptLevel)`，即默认的启发式（列表）调度器；目标可覆盖此方法换调度策略。

[llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.cpp:55-65](https://github.com/llvm/llvm-project/blob/e096d2f60dbc6cab991d5c02a5f7125a6dc694dc/llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.cpp#L55-L65) —— `ScheduleDAGSDNodes::Run`：清空旧 SUnit 图（60 行）后调 `Schedule()`（64 行）开始建依赖图并排序。

#### 4.5.4 代码实践

**目标**：观察指令选择把通用 ISD 节点变成目标机器指令的全过程。

1. 用最开始的 `t.ll`（`add i32 %a, %b; ret`），针对 X86：

   ```bash
   llc -mtriple=x86_64-linux-gnu -debug-only=isel t.ll -o /dev/null 2>&1 \
     | grep -iE "Selected selection DAG|Morphed node|ISEL: Starting selection"
   ```
2. **观察**：你会看到 `ISEL: Starting selection on root node: ...` 行（1380 行的 `LLVM_DEBUG` 输出），随后是 `Selected selection DAG` 转储——其中原本的 `add`（ISD::ADD）节点已变成形如 `ch = ADD32rr ...` / `ADD64rr` 的目标节点。
3. **预期结果**：能在 `Selected selection DAG` 转储里指认出某个目标具体指令节点，并理解它是 `OPC_MorphNodeTo` 把 `ISD::ADD` 变形而来的。
4. 若想看调度顺序，可用 `-view-sched-dags`（断言版，需 graphviz）或在 `-debug-only=isel` 输出里寻找调度相关的 `LLVM_DEBUG` 行。**待本地验证**：具体指令名随目标与寄存器分配前后阶段而异。

#### 4.5.5 小练习与答案

**练习 1**：`DoInstructionSelection` 为什么从根**逆序**遍历到入口，而不是顺序？
**答案**：DAG 的根是「最终输出」（如 `CopyToReg` 把结果写回寄存器），入口是「源头」（参数、常量）。从根开始选，可以先匹配到「复合模式」（多条指令合成一条机器指令的模式）的根节点，从而把整棵子模式一次性变形为一条目标指令；若从入口顺序选，会过早把子节点单独选掉，错过更大模式的合并机会。

**练习 2**：`OPC_EmitNode` 与 `OPC_MorphNodeTo` 的区别是什么？
**答案**：`OPC_EmitNode` **新建**一个目标节点并记录，匹配过程可能继续（用于「一条模式展开成多条指令」或继续匹配更大模式）；`OPC_MorphNodeTo` 把当前匹配到的通用节点**就地变形**为目标节点并立即 `return`，表示本节点选择完成。后者是「一对一替换」的常见情形。

**练习 3**：指令调度发生在寄存器分配之前还是之后？
**答案**：SelectionDAG 自带的这次调度（`Scheduler->Run`）发生在**寄存器分配之前**（pre-RA），此时还是虚拟寄存器、无限多，调度只受数据依赖与延迟约束。寄存器分配之后还有一次 post-RA 调度（见 u6-l1 的 pre-RA / post-RA 划分）。

---

## 5. 综合实践

**任务**：把一个稍复杂的函数完整跑过 SelectionDAG 流水线，画出它从 IR 到「已选择 DAG」的演化。

准备如下 IR（含二元运算、内存访问、控制流，能触发多个合法化/选择阶段）：

```llvm
define i32 @h(i32* %p, i32 %n) {
entry:
  %c = icmp sgt i32 %n, 0
  br i1 %c, label %then, label %else
then:
  %v = load i32, i32* %p
  %v2 = mul nsw i32 %v, 3
  store i32 %v2, i32* %p
  br label %else
else:
  %phi = phi i32 [ %v2, %then ], [ 0, %entry ]
  ret i32 %phi
}
```

请完成：

1. 用 `llc -mtriple=x86_64-linux-gnu -debug-only=isel h.ll -o /dev/null 2>sel.log` 捕获全部转储到 `sel.log`。
2. 在 `sel.log` 中定位 **`then` 基本块**的各阶段标题（`Initial selection DAG` / `Optimized lowered selection DAG` / `Legalized selection DAG` / `Selected selection DAG`），按顺序记录每个阶段的关键节点。
3. 回答三个问题：
   - `mul ... 3` 在 combine1 阶段被改写成了什么？（提示：乘小常数常被替换为 `shl`+`add` 等强度削减。）
   - `load`/`store` 节点带了哪种特殊边来保证顺序？（应为 chain。）
   - `Selected selection DAG` 里，`mul`/`load`/`store` 分别变成了哪些 X86 目标节点？
4. 把你的发现整理成一张「阶段 → 节点变化」的表格。若某项无法确认，明确标注「待本地验证」。

这个任务串联了 4.2（流水线）、4.3（建图）、4.4（合法化）、4.5（选择）四个模块，完成后你就拥有了一条「IR → 已选择 DAG」的完整观察链路。

## 6. 本讲小结

- **SelectionDAG 是基本块级的指令选择框架**：每个基本块建一张临时 DAG，处理完销毁。积木是 `SDNode`（操作码+类型+操作数，建节点时即 CSE）与 `SDValue`（节点+结果号）；chain/glue 两类边固定副作用顺序。
- **总调度 `CodeGenAndEmitDAG` 是一条固定流水线**：combine1 → 类型合法化 → combine-lt → 向量合法化 → combine-lv → 操作合法化 → combine2 → 指令选择 → 调度 → 发射。combine 穿插三次，因为每次合法化改写后都可能暴露新的化简机会。
- **DAG 构建遵循「操作数 → getNode → setValue」三段式**：`SelectionDAGBuilder::visit*` 用 `Instruction.def` 展开的巨型 switch 分派，`getValue` 的 `NodeMap` 缓存让共享子表达式天然形成 DAG。
- **合法化分两层**：先类型（`LegalizeTypes`：Promote/Expand/Scalarize/Split/Widen），后操作（`Legalize`：Legal/Promote/Expand/Custom/LibCall，后者带 `Custom→Expand→LibCall` 的逐级兜底），都迭代到不动点。
- **指令选择是一台字节码模式匹配机**：`DoInstructionSelection` 从根逆序遍历调 `Select`；目标生成器把 `.td` 模式编译成 `MatcherTable`，`SelectCodeCommon` 用 `OPC_Scope` 回溯择优、`OPC_MorphNodeTo` 就地把通用节点变形为目标 `MachineSDNode`。
- **调度与发射收尾**：默认启发式调度器在 pre-RA 阶段重排指令以提升指令级并行，`EmitSchedule` 把 `MachineSDNode` 转成 `MachineInstr` 写入 `MachineBasicBlock`，交给 u6-l1 流水线后续阶段。
- **观测工具**：`llc -debug-only=isel`（需断言版构建）转储各阶段 DAG；`-view-*-dags` 系列（需断言版+graphviz）弹图。

## 7. 下一步学习建议

- **对比 GlobalISel（u6-l3）**：本讲的合法化/选择是「DAG + 模式匹配」思路；下一讲 u6-l3 讲新一代 GlobalISel 的 `IRTranslator→Legalizer→RegBankSelect→InstructionSelect` 四阶段，它是「MIR + 表/GISel 选择器」思路。读完两讲后，重点对比二者在**合法化粒度**与**选择方式**上的取舍，理解为什么 LLVM 要并行维护两套框架。
- **深入模式规则的来源（u6-l5 TableGen）**：本讲反复提到的 `MatcherTable` 由 TableGen 从 `.td` 编译而来。学完 u6-l5 后，可以挑一个目标（如 `llvm/lib/Target/X86/X86InstrArithmetic.td`）看一条 `(set dst (add src1 src2))` 模式是如何最终变成这里的 `OPC_MorphNodeTo` 字节码的。
- **读调度器源码**：本讲只点到 `ScheduleDAGSDNodes::Run`。对指令级并行感兴趣的话，可读 `llvm/lib/CodeGen/SelectionDAG/ScheduleDAGRRList.cpp`（列表调度）与 `ResourcePriorityQueue.cpp`，以及 post-RA 调度 `llvm/lib/CodeGen/MachineScheduler.cpp`。
- **看一个目标的 `LowerOperation`**：合法化的 `Custom` 动作最终落到目标的 `TargetLowering::LowerOperation`。可在 `llvm/lib/Target/X86/X86ISelLowering.cpp` 中挑一个简单 intrinsic 的 lowering 读一遍，体会「自定义合法化」如何手写 DAG 改写。
