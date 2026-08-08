# Proc 常量性检查：用 Z3 发现可证明常量的节点

## 1. 本讲目标

本讲讲解 XLS 在 HEAD `ccd2a0636` 新引入的开发者工具 `proc_constancy_checker_main`。它针对 **Proc（进程）**，用一个完整的 SMT 求解器（Z3）去证明某些 IR 节点的取值「在所有激活（activation）中都恒定不变」——即**可证明常量（provably constant）**节点，哪怕这些节点在语法上根本不是一个字面量（literal）。

学完本讲你应当能够：

1. 说清「字面量常量」与「可证明常量」的区别，理解后者为何对优化和硬件面积有意义（「Potential Static Flops」）。
2. 看懂 `proc_testutils::UnrollProc` 如何把一个有时序状态的 Proc 按激活展开成一个无状态的纯函数，并保留「原节点 → 各激活副本」的映射。
3. 理解 `NonSynthRemovalPass` 如何剥离 `assert`/`trace`/`cover` 等不可综合操作及其专属中间依赖。
4. 读懂 `proc_constancy_checker_main` 的核心流程：剥离 → 筛选候选 → 展开 → 翻译成 Z3 → 逐节点/逐位证明恒定，以及 `--mode`、`--unroll_count`、`--fail_on_constants`、`--node_filter` 等选项与最终报告。

---

## 2. 前置知识

本讲建立在两篇前置讲义之上，这里只做最小回顾：

- **u3-l5 Proc、Channel 与状态化通信**：Proc 由 `config` / `init` / `next` 三件套组成，本质是「随时间归纳」。`next(st)` 每被调用一次就是一次**激活（activation）**；状态元素（StateElement）像寄存器，初值由 `init` 给出，每激活更新一次。Proc 与无状态的 Function 同属 `FunctionBase`，靠 `Kind::kProc` 区分。
- **u6-l4 形式化验证：Solver 与等价性检查**：XLS 用 `solvers::z3::IrTranslator` 把 IR 数据流图翻译成 Z3 的位向量（bit-vector）AST，再用「把待证命题取反、若 UNSAT 则原命题成立」的模式做证明。本讲的「常量性」证明正是这个 UNSAT 模式的直接应用。

还需要一个本讲会反复用到的关键区分：

| 概念 | 含义 | 谁负责发现 |
|---|---|---|
| **字面量常量（literal / trivial constant）** | 节点本身是 `Literal`，或操作数全是 `Literal` 的聚合 | 优化器 `opt` 的常量折叠（`const_fold`）、算术化简（`arith_simp`） |
| **可证明常量（provably constant）** | 取值在所有运行情形下恒定，但语法上不是字面量 | **本工具**（用 Z3 完整求解） |

优化器的折叠只看「局部、按构造就是常量」的节点；而本工具能跨多个激活做完整推理，发现优化器漏掉的「实际恒定但长得像真计算」的节点。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `xls/dev_tools/proc_constancy_checker_main.cc` | 命令行入口：解析标志、编排整个检查流程、打印进度与报告 |
| `xls/dev_tools/proc_constancy_checker.h` / `.cc` | 核心库函数：剥离、候选筛选、展开、Z3 辅助（`FlattenBitsOnly`） |
| `xls/passes/non_synth_removal_pass.h` / `.cc` | `NonSynthRemovalPass`：剥离不可综合操作的复合 Pass |
| `xls/ir/proc_testutils.h` | `UnrollProc`：把 Proc 按激活展开成函数，保留节点映射 |
| `xls/solvers/z3_ir_translator.h` | `IrTranslator`：IR → Z3 AST 翻译器 |
| `xls/dev_tools/testdata/constancy_test.x` | 配套示例 DSLX，精心构造了「节点级常量」和「位级常量」两种情况 |

---

## 4. 核心概念与源码讲解

### 4.1 什么是「可证明常量」节点，以及该检查哪些节点

#### 4.1.1 概念说明

设想硬件里有一个寄存器，它每一拍都重新计算自己的值，但你用数学可以证明：**无论输入和状态如何演化，它的值永远不变**。这就是一个「可证明常量」节点。在最终电路上，它对应一个**永远不会翻转的触发器（static flop）**——纯属浪费面积和功耗，本应被优化掉。

优化器 `opt` 之所以可能漏掉它，有两个根本原因：

1. **优化器是局部的、近似的**。它依赖查询引擎（BDD、区间分析等）做「过近似（over-approximation）」判断，对复杂表达式可能直接放弃，返回「不确定」而非「可折叠」。
2. **优化器不跨激活推理**。Proc 的常量性往往来自「状态如何随时间演化」这一时序性质，而 `opt` 主要在单个函数体的静态数据流图上做局部模式匹配。

本工具用 Z3（一个**完备**的位级 SMT 求解器）一次性把 Proc 展开成多个激活、再证明某节点在所有激活中取值相等——这是优化器做不到的「跨时序、完整」推理。

#### 4.1.2 核心流程

为了不浪费 Z3 的算力、也避免误报，工具要先筛掉**没必要检查**的节点。筛选逻辑由 `GetNodesFilteringNonSynthAndTrivialConstants` 实现，按拓扑序逐节点判定：

```text
对 Proc 中每个节点 n（按拓扑序）：
  若 n「按构造就是常量」(IsConstantByConstruction) → 跳过（这是该常量的）
  若 n 是 token 类型 或 0 位宽 → 跳过（无值）
  若 n 是 Param/StateRead/Receive/Send/Next/Assert/Trace/Cover → 跳过
  否则 → 加入「待检查候选」
```

其中「按构造就是常量」的判定很巧妙：一个节点是 `Literal`，**或者**它的所有操作数都已被判定为按构造常量。这要求**必须按拓扑序**处理，边判边把常量塞进集合，后续节点才能查到操作数的状态——否则会把「本该是常量」的聚合节点误判为「非常量」而 spuriously 报告。

#### 4.1.3 源码精读

`IsConstantByConstruction` 的判定规则：

[proc_constancy_checker.cc:60-72](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker.cc#L60-L72) — 中文说明：`Literal` 直接算常量；凡是 `Receive`/`StateRead`/`Param`/`Send`/`Next`/`Assert`/`Trace`/`Cover` 这些「动态输入或副作用」源头一律不算常量；其余节点当且仅当**所有操作数**都已在 `constant_nodes` 集合中才算常量。

候选筛选主循环：

[proc_constancy_checker.cc:85-105](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker.cc#L85-L105) — 中文说明：先 `TopoSort` 拓扑排序，再逐节点用上面的规则过滤，把 token、零位宽、以及各类 IO/状态/副作用节点剔除，剩下的才是值得用 Z3 证明的候选。

配套的 C++ 单元测试印证了筛选结果——对一个「`add1 = state + 1` + 一条 `assert`」的 Proc，剥离后候选只剩 `add1` 一个：

[proc_constancy_checker_test.cc:65-76](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker_test.cc#L65-L76) — 中文说明：`EXPECT_THAT(names, UnorderedElementsAre("add1"))`，字面量 `lit1`、`cond` 与 token 都被过滤掉了。

#### 4.1.4 代码实践

1. **实践目标**：直观体会「字面量被过滤、真计算被保留」。
2. **操作步骤**：阅读 [constancy_test.x:22-29](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/testdata/constancy_test.x#L22-L29)。其中 `const_zero = st & u32:0`、`trailing_zero = st << u32:2`、`add1 = st + u32:1 + const_zero + (trailing_zero & u32:0)`。请预测：`u32:0`、`u32:1`、`u32:2` 这些字面量会不会出现在候选里？`const_zero`、`trailing_zero`、`add1` 会不会？
3. **需要观察的现象**：字面量本身（`Literal`）被 `IsConstantByConstruction` 直接判为常量而过滤；`const_zero`/`trailing_zero`/`add1` 都有 `StateRead`（`st`）这个非常量操作数，故都不算「按构造常量」，都会进入候选。
4. **预期结果**：三个计算节点都在候选名单中，留待 4.4 用 Z3 区分它们的常量性。
5. 若想确认运行结果，可在 4.4 的端到端实践中加 `--node_filter` 观察候选是否被纳入。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `IsConstantByConstruction` 必须按拓扑序调用、边判边把常量写入集合？

> **答**：因为判定 `n` 是否「按构造常量」依赖它的**操作数**是否已被判为常量。若先判 `n` 再判其操作数，`n` 会因操作数还不在集合里而被误判为非常量。拓扑序保证「操作数先于使用者」被处理。

**练习 2**：`StateRead` 节点本身会不会被当作候选去检查常量性？为什么？

> **答**：不会。`StateRead` 是状态/动态输入的源头，被显式列入跳过名单（既在 `IsConstantByConstruction` 中返回 `false`，又在 `GetNodesFilteringNonSynthAndTrivialConstants` 中被过滤）。它代表「每激活可能变化的寄存器值」，正是常量性证明的**输入变量**，而非被证明对象。

---

### 4.2 NonSynthRemovalPass：剥离不可综合节点

#### 4.2.1 概念说明

`assert`、`trace`、`cover` 这些操作是**不可综合（non-synthesizable）**的——它们是给仿真/验证用的，不会变成真实电路。在证明常量性前，必须先把它们剥离掉，原因有二：

- 它们携带 **token**（ sequencing 用），若留着会让 Z3 翻译器去处理无实际数据含义的 token 流；
- 它们可能引入「仅被 assert/trace 消费的中间节点」，这些中间节点对常量性结论毫无贡献，徒增求解规模。

#### 4.2.2 核心流程

`NonSynthRemovalPass` 是一个**复合 Pass**，内部串接四步：

```text
1. NonSynthSeparationPass：把不可综合操作「克隆」进一个单独的 non-synth 函数，
   原函数里用一个 invoke 去调用它。
2. NonSynthInvokeRemovalPass：删掉那些调用 non-synth 函数的 invoke 节点。
3. DeadCodeEliminationPass (DCE)：删除「只被已删除 invoke 消费」的中间节点。
4. DeadFunctionEliminationPass (DFE)：删除克隆出来的、现已无人调用的 non-synth 函数。
```

这套「先分离 → 删调用 → 清死码 → 删死函数」的组合，保证剥离后 IR 里**一个不可综合节点都不剩**，且没有遗留的孤立中间计算。

> 补充一个事实：`NonSynthRemovalPass` 虽然通过 `REGISTER_OPT_PASS` 注册进了优化 Pass 注册表，但它**并不在标准优化管线**（`optimization_pass_pipeline.txtpb` 的 `default_pipeline`）里——标准管线只用到它的前半段 `non_synth_separation`。所以这个「彻底删除」的 Pass 通常只被本工具等开发者工具显式调用。

#### 4.2.3 源码精读

复合 Pass 的四步编排写在构造函数里：

[non_synth_removal_pass.cc:64-71](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/passes/non_synth_removal_pass.cc#L64-L71) — 中文说明：`Add<NonSynthSeparationPass>()` → `Add<NonSynthInvokeRemovalPass>()` → `Add<DeadCodeEliminationPass>()` → `Add<DeadFunctionEliminationPass>()`，正是上面四步。

其中第 2 步 `NonSynthInvokeRemovalPass` 是本 Pass 文件里就地定义的，逻辑很直白——遍历节点，删掉所有 `to_apply()->non_synth()` 为真的 `invoke`：

[non_synth_removal_pass.cc:34-60](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/passes/non_synth_removal_pass.cc#L34-L60) — 中文说明：注意迭代时用 `std::next` 预存下一个位置再 `RemoveNode`，这是边遍历边删除节点的标准写法，避免迭代器失效。

头部文档对四步意图的权威说明：

[non_synth_removal_pass.h:24-42](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/passes/non_synth_removal_pass.h#L24-L42) — 中文说明：类注释逐条列出 1～4 步及其作用，`kName = "non_synth_removal"`。

库函数 `StripNonSynthNodes` 就是「构造一个 `NonSynthRemovalPass` 并在 package 上 `Run` 一次」的薄封装：

[proc_constancy_checker.cc:76-83](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker.cc#L76-L83) — 中文说明：`OptimizationContext` + `PassResults` 是跑 Pass 的标配上下文与结果收集器，`pass.Run(package, ...)` 在整个 package 上执行四步剥离。

#### 4.2.4 代码实践

1. **实践目标**：验证 `assert` 等节点确实被剥离。
2. **操作步骤**：在 `constancy_test.x` 的 `next` 里有一条 `assert!(st == st, "test assert")`（[constancy_test.x:26](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/testdata/constancy_test.x#L26)）。把它转成 IR 后，`assert` 会变成 `assert` 节点。运行本工具时观察开头日志。
3. **需要观察的现象**：`RealMain` 在第 [proc_constancy_checker_main.cc:257](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker_main.cc#L257) 行调用 `StripNonSynthNodes` 之后，再枚举候选节点时，`assert`/`trace`/`cover` 及其专属 token 都不应再出现。
4. **预期结果**：候选名单（4.1）中不含任何 `assert`/`trace`/`cover`/`send` 的 token，与 C++ 测试 `GetNonConstantNodes` 的结论一致。
5. 运行结果：待本地验证（需先构建工具，见 4.4）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接用标准优化管线里的 `non_synth_separation`（分离）就够了，还要再做「删 invoke + DCE + DFE」？

> **答**：分离（separation）只是把不可综合操作**搬进**一个单独函数、用 invoke 调用，它们**仍然存在于 IR 中**（只是位置变了）。而常量性证明需要的是「IR 里完全没有这些节点」，所以必须继续删掉 invoke、再用 DCE 清掉因此失去用途的中间节点、用 DFE 删掉孤立的 non-synth 函数，才能得到一个纯净的可综合子图交给 Z3。

**练习 2**：`NonSynthRemovalPass` 的第 2 步 `NonSynthInvokeRemovalPass` 是 `OptimizationFunctionBasePass`（作用域 Pass），它如何被应用到 package 里的每个函数？

> **答**：作用域基类（参见 u4-l1）会自动把 `RunOnFunctionBaseInternal` 对 package 内每个 `FunctionBase`（Function/Proc/Block）各执行一次，无需复合 Pass 手写遍历。所以删 invoke 的逻辑只写一份，框架负责套用到所有函数。

---

### 4.3 Proc 激活展开：UnrollProc 与节点映射

#### 4.3.1 概念说明

Proc 是有时序的：`next(st)` 每调用一次产出一个新状态。Z3 擅长证明**无状态函数**的等价性（u6-l4）。要把「跨激活的常量性」变成 Z3 能处理的问题，关键是**把时序的 Proc 展开成一个无状态的纯函数**：让这个函数的输入是若干激活的「外部输入」，函数体内顺序模拟 `activation_count` 次 `next`，第 k 次的 `StateRead` 接第 k-1 次算出的状态。

更妙的是：展开时为**每个原始节点**记录它在**每一个激活副本里**对应的克隆节点。于是「证明节点 `n` 跨激活恒定」就变成「证明 `n` 的 k 个克隆在展开函数里取值相等」——一个纯粹的函数内等价性问题。

#### 4.3.2 核心流程

`UnrollProcForConstancy` 调用 `proc_testutils::UnrollProc`，后者返回一个 `UnrolledProc` 结构：

```text
UnrollProc(proc, activation_count, include_state=true, token=0xdeadbeef, cleanup=false)
  → UnrolledProc {
       function: 展开后的无状态函数 Function*
       activations: 每个激活一个 ActivationAction
         每个 ActivationAction.node_values: { 原始 Node* → 该激活里的 BValue }
       initial_state: 各 StateElement 的初值
    }

再把 activations[*].node_values 转置，得到：
  NodeActivationMap = { 原始 Node* → [激活0的克隆, 激活1的克隆, ..., 激活k-1的克隆] }
```

两个细节值得注意：

- **`cleanup=false`**：通常展开后会跑 DCE/内联清理中间节点，但这里**故意不清理**——因为我们要检查的正是那些「中间节点」是否恒定，若被 DCE 删掉就没法检查了。
- **token 用非零字面量** `0xdeadbeef` 替换：Z3 不喜欢零长度值还有使用者，故用一个 32 位非零值占位（见 `proc_testutils.h` 注释）。

#### 4.3.3 源码精读

`UnrollProc` 的签名与关键约束（每激活每通道最多收/发一次、状态从初值开始、专为 z3 等测试工具设计）：

[proc_testutils.h:109-126](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/ir/proc_testutils.h#L109-L126) — 中文说明：`UnrollProc` 的 `cleanup` 参数文档明确写道「检查中间非 IO 节点常量性时应设 `cleanup=false`，以免中间节点被 DCE 消除」——这正是本工具的用法。

承载节点映射的两个数据结构：

[proc_testutils.h:69-81](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/ir/proc_testutils.h#L69-L81) — 中文说明：`ActivationAction.node_values` 是「原始 Node* → 该激活里的 BValue」；`UnrolledProc` 汇总了展开函数、各激活动作与初值。

把 `UnrolledProc` 转置成 `NodeActivationMap` 的薄封装：

[proc_constancy_checker.cc:107-124](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker.cc#L107-L124) — 中文说明：双重循环遍历每个激活的 `node_values`，把 `orig_node → val.node()` 收集进 `node_activations[orig_node]`，最终每个原始节点拿到一个「跨激活克隆列表」。注意 `cleanup=false` 传参。

C++ 测试验证展开确实产出了非空函数与非空映射：

[proc_constancy_checker_test.cc:78-84](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker_test.cc#L78-L84) — 中文说明：`UnrollProcForConstancy(proc, 3)` 后 `EXPECT_NE(func, nullptr)` 且 `EXPECT_FALSE(map.empty())`。

#### 4.3.4 代码实践

1. **实践目标**：理解「同一个原始节点在不同激活里是不同的克隆」。
2. **操作步骤**：以 `constancy_test.x` 为例，`st` 是状态。展开 3 次后，`add1` 这个原始节点会对应 3 个克隆 `add1_act0`、`add1_act1`、`add1_act2`，分别用「初值 0」「0+1=1」「1+1=2」作为各自的 `st` 输入计算。请画出这三条数据流如何首尾相接（激活 i 的 `next` 状态 → 激活 i+1 的 `StateRead`）。
3. **需要观察的现象**：`NodeActivationMap[add1]` 是一个长度为 3 的列表。
4. **预期结果**：每个候选节点的克隆数应等于 `unroll_count`；主程序里 `FilterTargetsForChecking` 正是据此过滤——只保留克隆数恰为 `unroll_count` 的节点（某些位于阻塞收发之后的节点可能并非每激活都出现）。
5. 运行结果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么本工具要把 `cleanup` 设为 `false`？若设为 `true` 会怎样？

> **答**：`cleanup=true` 会在展开后跑 DCE 和内联，删掉「无人消费的中间节点」。但本工具的目的恰恰是检查这些中间节点是否恒定，若被删掉就无法检查了，故必须 `cleanup=false` 把它们全部保留。

**练习 2**：`UnrollProc` 要求「每激活每通道最多收/发一次」。如果一个 Proc 在单次 `next` 里对同一 `out` 通道 `send` 了两次，会怎样？

> **答**：这是 `UnrollProc` 当前不支持的场景（`proc_testutils.h` 里 `TODO(allight): Support sending on a single channel multiple times`）。本工具假定输入 Proc 满足该约束，否则展开结果可能不正确。

---

### 4.4 Z3 常量性证明：node 模式与 bit 模式

#### 4.4.1 概念说明

有了展开函数和「原节点 → k 个克隆」的映射，常量性证明化为一个标准的 **UNSAT 等价性检查**（承接 u6-l4）：

> 节点 `n` 跨所有激活恒定 ⟺ 「存在某两个激活使 `n` 取值不同」**不可满足（UNSAT）**。

工具提供两种粒度：

- **node 模式**：把 `n` 的 k 个克隆当整体比较，证明「整体值恒定」。
- **bit 模式**：把值拆成逐位，独立证明**每一位**是否恒定。这样能发现「值会变、但某些位永远不变」的节点——例如 `x << 2` 的最低 2 位恒为 0。

#### 4.4.2 核心流程

整个证明在 `RealMain` 里编排，对每个候选节点构造一个「是否会变化」的布尔公式，交给 Z3 用 `Z3_solver_check_assumptions` 判定：

```text
# node 模式（核心，对节点 n，克隆 c_0..c_{k-1}）：
对 i = 1..k-1：  d_i = NOT( eq(z3(c_0), z3(c_i)) )
can_change = OR(d_1, ..., d_{k-1})        # 「存在某激活与激活0不同」
res = Z3_solver_check_assumptions(can_change)
  res == TRUE (SAT)   → 找到了会变的赋值 → n 非常量
  res == FALSE (UNSAT) → 没有任何赋值会让它变 → n 是常量 ★
  res == UNKNOWN      → 超时/超资源 → 标记 timeout

# bit 模式：先把每个克隆 FlattenBitsOnly 拆成位级 AST，
#           再对每一位索引 b 重复上面的 SAT/UNSAT 判定。
```

判定的三值逻辑：

- `Z3_L_TRUE`（SAT）：`can_change` 可满足 → **非常量**；
- `Z3_L_FALSE`（UNSAT）：`can_change` 不可满足 → **常量**（报告 `CONSTANT NODE/BIT DETECTED`）；
- 其他（UNKNOWN）：超时或触达 rlimit → 报告 `UNKNOWN (原因)`。

一个聪明的性能优化：节点按拓扑序检查，一旦某节点被证明恒定，就把 `NOT(can_change)` **永久 assert 进求解器**（决策层 0）。后续检查它的使用者时，Z3 的同余闭包（congruence closure）能立刻把恒定操作数的 AST 合并进同一等价类，无需重新探索证明树。

#### 4.4.3 源码精读

标志定义（全部选项一目了然）：

[proc_constancy_checker_main.cc:69-87](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker_main.cc#L69-L87) — 中文说明：`--ir_path`（必填）、`--top_proc`（空则用 package 的 top）、`--unroll_count`（默认 4）、`--mode`（`node`/`bit`，默认 `node`）、`--z3_rlimit`/`--z3_timeout_ms`（资源/时间限制）、`--node_filter`（按名字子串过滤候选）、`--fail_on_constants`（发现常量则返回非零退出码）。

`RealMain` 的编排主干（剥离 → 筛选 → 展开 → 翻译 Z3 → 循环检查）：

[proc_constancy_checker_main.cc:257-283](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker_main.cc#L257-L283) — 中文说明：先 `StripNonSynthNodes`，再 `GetNodesFilteringNonSynthAndTrivialConstants` 取候选，按 `--node_filter` 过滤，`UnrollProcForConstancy` 展开，最后 `IrTranslator::CreateAndTranslate(unrolled_func, allow_unsupported=true)` 把展开函数翻译成 Z3 AST。

Z3 求解器与参数设置（rlimit / timeout / 关闭 ctrl_c）：

[proc_constancy_checker_main.cc:285-304](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker_main.cc#L285-L304) — 中文说明：注释解释为何把 `ctrl_c` 设为 `false`——Z3 默认会把 Ctrl+C 当成「取消当前求解」而非「终止整个程序」，这里关掉该行为。

**node 模式**的核心判定循环（构造 `can_change` 并三分支解释结果）：

[proc_constancy_checker_main.cc:321-352](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker_main.cc#L321-L352) — 中文说明：`Z3_mk_eq` 比较激活 0 与激活 i，`Z3_mk_not` 取反得「不相等」，`Z3_mk_or` 合成 `can_change`；SAT 走 `RecordNonConstantCheck`，UNSAT 走 `RecordConstantNode` 并把 `NOT(can_change)` assert 进求解器加速后续，UNKNOWN 走 `RecordTimeoutNode`。

**bit 模式**的逐位判定：

[proc_constancy_checker_main.cc:353-406](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker_main.cc#L353-L406) — 中文说明：先用 `FlattenBitsOnly` 把每个激活克隆的 Z3 值拆成位级 AST，再对每一位索引 `b` 独立做与 node 模式相同的 SAT/UNSAT 判定。

把元组/数组类型的 Z3 值递归拍平到位级的辅助函数：

[proc_constancy_checker.cc:126-161](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker.cc#L126-L161) — 中文说明：`bits` 类型直接调 translator 的 `FlattenValue`；元组用 `Z3_get_tuple_sort_field_decl` 取各字段投影；数组用 `Z3_mk_select` 逐元素取，递归收集所有位。

报告与 `--fail_on_constants` 语义：

[proc_constancy_checker_main.cc:412-426](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker_main.cc#L412-L426) — 中文说明：汇总 Non-Constant / Constant（标注 `Potential Static Flops`）/ Timeout 三类计数；若 `fail_on_constants && constant_checks > 0` 则返回 `FailedPreconditionError`，经 `ExitStatus` 变成非零退出码。

底层翻译器接口（承接 u6-l4）：

[z3_ir_translator.h:62-63](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/solvers/z3_ir_translator.h#L62-L63) 与 [z3_ir_translator.h:95](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/solvers/z3_ir_translator.h#L95) 与 [z3_ir_translator.h:119-120](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/solvers/z3_ir_translator.h#L119-L120) — 中文说明：`CreateAndTranslate` 把整个 `FunctionBase` 翻成 Z3 AST；`GetTranslation(Node*)` 取某节点对应的 AST（这就是「原节点克隆 → Z3 AST」的桥梁）；`FlattenValue` 把一个值拆成位级 AST 数组。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：亲手跑通端到端检查，对照 `constancy_test.x` 理解 node/bit 两模式的差异输出。
2. **操作步骤**：

   ```bash
   # (1) 构建工具与 DSLX→IR 转换器
   bazel build -c opt //xls/dev_tools:proc_constancy_checker_main
   bazel build -c opt //xls/dslx:ir_converter_main

   # (2) 把示例 .x 转成 .ir（IR 打印到 stdout，重定向到文件）
   ./bazel-bin/xls/dslx/ir_converter_main \
       xls/dev_tools/testdata/constancy_test.x > /tmp/constancy_test.ir

   # (3) node 模式：展开 3 次检查
   ./bazel-bin/xls/dev_tools/proc_constancy_checker_main \
       --ir_path=/tmp/constancy_test.ir --unroll_count=3 --mode=node

   # (4) bit 模式：只看 trailing_zero 的逐位常量性
   ./bazel-bin/xls/dev_tools/proc_constancy_checker_main \
       --ir_path=/tmp/constancy_test.ir --unroll_count=3 \
       --mode=bit --node_filter=trailing_zero

   # (5) 让工具在发现常量时返回非零退出码
   ./bazel-bin/xls/dev_tools/proc_constancy_checker_main \
       --ir_path=/tmp/constancy_test.ir --unroll_count=3 \
       --mode=node --fail_on_constants ; echo "exit=$?"
   ```

   > 注：`constancy_test.ir` 不是仓库里 checked-in 的文件，而是由 Bazel 规则 `xls_dslx_ir`（见 [xls/dev_tools/BUILD:1087-1092](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/BUILD#L1087-L1092)）从 `.x` 生成的产物，所以这里用 `ir_converter_main` 手动生成等价物。

3. **需要观察的现象与预期结果**（这些断言直接来自配套 Python 测试 [proc_constancy_checker_main_test.py](https://github.com/google/xls/blob/ccd2a0636fc675febe35822d09dc7baf28ab1527/xls/dev_tools/proc_constancy_checker_main_test.py#L29-L126)，可放心对照）：
   - **(3) node 模式**：输出含 `CONSTANT NODE DETECTED: 'const_zero'`。`const_zero = st & 0` 整体恒为 0，故是「节点级常量」。注意 `trailing_zero` **不会**作为节点级常量被报告（因为 `st << 2` 的整体值随 `st` 变化）。
   - **(4) bit 模式**：输出含 `CONSTANT BIT DETECTED: 'trailing_zero' bit [0]` 与 `bit [1]`。左移 2 位清掉了最低 2 位，故这两位恒为 0，是「位级常量」——这正是 node 模式抓不到、bit 模式才能抓到的精细常量。
   - **(5) fail_on_constants**：退出码 `exit=` 非 0，且 stdout 仍含 `CONSTANT NODE DETECTED: 'const_zero'`。
4. **若无法本地构建**：可直接阅读 `proc_constancy_checker_main_test.py` 中 `test_constancy_detection_node_mode` / `test_constancy_detection_bit_mode` / `test_fail_on_constants_flag` 三个用例的断言，它们就是上面预期结果的权威来源（属于「源码阅读型实践」）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `const_zero` 在 node 模式被判为常量，而 `trailing_zero` 不会，但 `trailing_zero` 的 bit [0]、bit [1] 在 bit 模式却被判为常量？

> **答**：`const_zero = st & u32:0` 对任意 `st` 整体都是 0，所有激活的克隆整体相等 → node 模式 UNSAT → 节点级常量。`trailing_zero = st << u32:2` 的整体值随 `st` 变（如 `st=1` 得 4，`st=2` 得 8），故 node 模式 SAT → 非常量。但左移 2 位保证最低 2 位恒为 0，所以这两位跨激活恒等 → bit 模式对该位 UNSAT → 位级常量。这正体现了 bit 模式比 node 模式更精细。

**练习 2**：判定结果为 `UNKNOWN` 时意味着什么？工具如何处理？

> **答**：`UNKNOWN` 表示 Z3 在给定 `--z3_rlimit` / `--z3_timeout_ms` 内无法给出确定结论（既不能证明会变，也不能证明恒定）。工具调用 `Z3_solver_get_reason_unknown` 取原因，按 `RecordTimeoutNode/Bit` 计入 `Timeout Checks`，并在报告里单列一行，**不**把它当作常量。用户可调大 rlimit/timeout 后重试。

**练习 3**：请用一句话说明本工具为何能发现「普通 opt 难以识别为字面量的常量节点」。

> **答**：因为本工具用完备的 Z3 在「跨多个激活的展开函数」上做完整位级推理，而 opt 的常量折叠/查询引擎是局部、近似、且不跨激活时序推理的，因此对「语法上非字面量、但跨激活可证明恒定」的节点往往无能为力。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次完整的「常量审计」：

1. **改造样例**：复制 `constancy_test.x` 为 `/tmp/my_test.x`，在 `next` 里再加一个节点 `let half_const = (st * u32:4) | u32:3;`（先猜测：它的整体值会变吗？低 2 位呢？）。
2. **生成 IR 并检查**：用 4.4 的命令把它转成 IR，分别用 `--mode=node` 和 `--mode=bit --node_filter=half_const` 检查。
3. **解读报告**：
   - `half_const` 在 node 模式应**不**被报告为常量节点（因为 `st*4` 高位随 `st` 变）；
   - 在 bit 模式，低 2 位因 `| u32:3` 恒被置 1，应被报告为 `CONSTANT BIT DETECTED: 'half_const' bit [0]` 与 `bit [1]`。
4. **追问优化器**：把同一 IR 跑一遍 `opt_main`（标准优化管线），观察 `half_const` 是否被化简、低 2 位的常量性是否被 opt 的区间/BDD 分析识别。对比 Z3 的结论与 opt 的结论，体会「可证明常量」相对「字面量折叠」的更强能力。
5. 若本地无法运行，把上述预测写成断言（参照 `proc_constancy_checker_main_test.py` 的写法），作为「源码阅读型实践」的产出。

> 这个任务覆盖了：剥离不可综合节点（4.2）→ 激活展开与节点映射（4.3）→ Z3 逐节点/逐位证明（4.4）→ 与标准优化管线的对比（4.1 概念）。

---

## 6. 本讲小结

- **可证明常量 ≠ 字面量常量**：前者是「跨所有运行情形恒定」，需完备求解器证明；后者是「按构造就是常量」，opt 能折叠。本工具专找前者。
- **三步流水线**：`StripNonSynthNodes`（剥离 assert/trace/cover 及专属中间依赖）→ `UnrollProcForConstancy`（把 Proc 展开成无状态函数并保留「原节点→各激活克隆」映射，`cleanup=false` 保住中间节点）→ Z3 逐节点/逐位证明。
- **证明即 UNSAT**：构造 `can_change = 存在某激活与激活0不同`，SAT 则非常量、UNSAT 则常量、UNKNOWN 则超时；被证明恒定的节点会 assert 进求解器加速后续检查。
- **node vs bit**：node 模式判整体恒定，bit 模式把元组/数组拍平后逐位判定，能发现「值会变但某些位恒定」的精细常量（如左移、位或产生的常数位）。
- **选项与报告**：`--mode`、`--unroll_count`（须 >1）、`--node_filter`、`--z3_rlimit`/`--z3_timeout_ms`、`--fail_on_constants`（发现常量则非零退出码）；报告把常量计为 `Potential Static Flops`。
- **工程定位**：`NonSynthRemovalPass` 虽已注册但不在标准优化管线，只被本类开发者工具显式调用；本工具承接 u6-l4 的 UNSAT 等价性检查思路、依赖 u3-l5 的 Proc 模型。

---

## 7. 下一步学习建议

- **回看 u6-l4**：本讲的 `can_change` UNSAT 判定与 `IrTranslator` 翻译，正是 u6-l4「IR 等价性检查」的同款机制，可对照体会「等价性证明」与「常量性证明」如何共用一套 Z3 基础设施。
- **深入 u7-l1**：`UnrollProc` 与 `proc_runtime`、`proc_state_legalization_pass` 同属 Proc 的执行/分析侧设施，学完本讲后阅读 u7-l1 能更全面理解 Proc 的时序语义。
- **扩展阅读源码**：`xls/passes/non_synth_separation_pass.h`（分离的详细文档与示例 IR）、`xls/ir/proc_testutils.cc`（`UnrollProc` 的展开实现，含收发阻塞与状态串接细节）、`xls/solvers/z3_ir_translator.cc`（IR→Z3 的逐 Op 翻译），可把本讲的「薄封装」逐层追到底。
