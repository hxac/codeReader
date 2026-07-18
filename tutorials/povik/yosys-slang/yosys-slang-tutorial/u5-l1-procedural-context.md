# ProceduralContext 与 VariableState

> 单元 5 ·第 1 讲 · 过程块建模：always/initial 到 RTLIL Process

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `ProceduralContext` 在翻译一个 `always`/`initial` 过程块时扮演的「工作台」角色：它同时持有「HDL 意图的 case 树」「变量当前值」「逃逸栈」「时序信息」。
- 解释 `VariableState` 为什么用一张「可回滚账本」(`visible_assignments` + `revert`)来记录变量的当前值，并掌握 `set` / `evaluate` / `save` / `restore` 四个操作。
- 跟踪一条 `if` 语句：进入分支时 `save`、退出分支时 `restore`，最后由 `SwitchHelper::finish` 把各分支的取值合并成一组「分支选择线」，从而把整段过程块收进**一个** `RTLIL::Process`。
- 理解收尾两步 `all_driven` 与 `copy_case_tree_into` 的作用：前者汇总「本过程驱动了哪些静态变量」，后者把 HDL 意图树降级成 `RTLIL::CaseRule`。
- 了解 `effects_priority`（副作用单元的顺序戳）与 `initial_locals_state`（initial 过程里局部变量的初值表）这两个辅助成员的用途。

## 2. 前置知识

本讲假设你已经读过：

- **u3-l3 Variable 与 VariableBits**：知道 `VariableBit`/`VariableBits` 是「描述某变量某些位」的轻量键，不是真实线网。
- **u3-l4 Case 与 Switch**：知道过程块里的赋值不直接生 RTLIL，而是先压成一棵仿 `CaseRule`/`SwitchRule` 的 `Case`/`Switch` 树，`Case::Action` 带着左值 `VariableBits` + `mask` + `unmasked_rvalue`。
- **u4-l2 LValue**：知道静态左值会被折叠成 `VariableBits`，交给 `update_variable_state`。

几个本讲会反复用到、但值得先点一下的概念：

- **过程块（procedural block）**：SystemVerilog 里 `always_comb` / `always_latch` / `always_ff` / `always @(...)` / `initial` 这些块。它们体内是一段「顺序执行」的语句，而不是连续赋值。
- **RTLIL::Process**：Yosys 网表里表示「行为级过程」的对象，核心是一棵 `root_case`（`RTLIL::CaseRule`），其下挂 `SwitchRule`/`CaseRule` 子树与一组 `actions`（连线 `SigSig`）。Yosys 后续的 `proc_*` 一族 pass 会把 Process 展平成纯门级。sv-elab 的任务就是把 SV 的过程块翻译成这样一棵 Process 树。
- **blocking / nonblocking 赋值**：`=`（阻塞）与 `<=`（非阻塞）。综合时两者语义不同，sv-elab 在同一过程块里禁止对同一变量混用（会报诊断）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | `ProceduralContext` 类与内嵌 `VariableState` 结构体的声明，以及 `ProcessTiming`、`EscapeFrame`。 |
| [src/procedural.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc) | `ProceduralContext` 的构造、`update_variable_state`、`do_simple_assign`、`substitute_rvalue`、`copy_case_tree_into`、`all_driven`、`case_enable`、`inherit_state` 的实现。 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | `VariableState::set/evaluate/save/restore` 的实现，以及 `handle_comb_like_process` 等消费 `ProceduralContext` 的入口。 |
| [src/statements.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h) | `SwitchHelper`——本讲的「分支合并器」，调用 `vstate.save/restore`，在 `finish()` 里把分支结果合并成选择线。 |
| [src/cases.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h) | `Case`/`Switch` 树节点，`copy_into` 把它降级为 `RTLIL::CaseRule`。 |
| [tests/unit/latch.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/latch.ys) | 含不完整 `if` 的组合块用例，会触发锁存器推断，是观察分支行为的好样本。 |

---

## 4. 核心概念与源码讲解

本讲按四个最小模块推进：

- **4.1 ProceduralContext**——一个过程块的「工作台」。
- **4.2 VariableState**——变量当前值的「可回滚账本」。
- **4.3 分支建模与合并**——`if` 如何靠 save/restore + finish 收进一个 Process（本讲核心）。
- **4.4 收尾：all_driven 与 copy_case_tree_into**——汇总驱动集合、降级成 RTLIL Process。

### 4.1 ProceduralContext：一个过程块的「工作台」

#### 4.1.1 概念说明

每翻译**一个**过程块（一个 `always_comb`、一个 `always_ff`、一个 `initial`……），sv-elab 就 new 一个 `ProceduralContext` 对象。它是这个块在翻译期间的「工作台」：所有语句遍历、变量赋值、case 树构造都在它之上进行，块翻译完它就析构（或把状态过继给下一个上下文）。

把它想象成一张摆满工具的桌子，桌上同时摊着四样东西：

1. **case 树（`root_case` / `current_case`）**：仿 RTLIL 的 `Case`/`Switch` 树，记录「在当前控制流位置，发生了哪些赋值」。这是 u3-l4 讲过的 HDL 意图树。
2. **变量当前值（`vstate`）**：一个 `VariableState`，记录每个变量位「到目前为止」被算成了什么信号。它使后续语句读到这个变量时能拿到「过程块内最近一次赋值」的结果（blocking 语义）。
3. **时序信息（`timing`）**：一个 `ProcessTiming&` 引用，说明本块是组合（`Implicit`）、initial（`Initial`）还是边沿触发（`EdgeTriggered`），以及触发信号列表。
4. **逃逸栈（`escape_stack`）与副作用计数（`effects_priority`）**：处理 `break/continue/return` 与 `$print/$check` 等副作用单元的顺序。

此外还有两个专用于 initial 过程的成员：`initial_locals_state`（局部变量/逃逸标志的逐位初值）和 `preceding_memwr`（前面已发出的 `$memwr` 写端口，用于计算优先级掩码）。

#### 4.1.2 核心流程

一个过程块从无到有变成 RTLIL，大体经历：

```
addProcess() 建空 RTLIL::Process
   │
   ├── new ProceduralContext(netlist, timing)
   │        └── 建空 root_case；current_case 指向一个 trivial 外壳
   │
   ├── body.visit(StatementExecutor(procedure))   ← 遍历语句，填 case 树 + 改 vstate
   │
   ├── （可选）all_driven() / 锁存器检测 / 连续驱动
   │
   └── context.copy_case_tree_into(proc->root_case)  ← 降级成 RTLIL::CaseRule
```

构造函数建好「空树 + 空状态」，语句遍历不断往里填，最后 `copy_case_tree_into` 把整棵树搬到 `RTLIL::Process` 上。注意：**一个过程块对应一个 `ProceduralContext`，也对应一个 `RTLIL::Process`**——这正是「合并回单一 Process」的物质基础。

#### 4.1.3 源码精读

先看 `ProceduralContext` 的关键成员声明（只列与本讲强相关的部分）：

> [src/slang_frontend.h:L247-L272](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L247-L272) —— `ProceduralContext` 的成员：`unroll_limit`、`netlist`、`timing`、`eval`、`effects_priority`、`root_case`、`current_case`、`initial_locals_state`、`preceding_memwr`，以及私有的 `flag_counter` 与 `seen_blocking_assignment`/`seen_nonblocking_assignment` 两张「已见过某变量被哪种赋值」的表。

几个要点逐条点出：

- `root_case` 是 `std::unique_ptr<Case>`，整棵 HDL 意图树的根；`current_case` 是「当前控制流位置」对应的 `Case*`，赋值会压进它的 `actions`。
- `eval` 是一个 `EvalContext`，且 `EvalContext::procedural` 回指 `this`——所以过程块内求值表达式时，能拿到 `vstate` 提供的「过程块局部当前值」。
- `effects_priority` 是 `int`，初值 0；每发出一个有副作用的单元就**前缀自减**赋给它的 `PRIORITY` 参数：

> [src/slang_frontend.cc:L1154-L1155](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1154-L1155) —— `handle_display` 里 `cell->parameters[ID::PRIORITY] = --context.effects_priority;`。`$print`/`$check`/`$memwr` 这类「不写变量、但有副作用」的单元无法靠变量状态的 merge 自动排序，于是用一个严格递减的整数戳标记源码顺序，交给下游 `proc` pass 排序。

- `initial_locals_state` 只在 `timing.kind == ProcessTiming::Initial` 时使用，按位记录自动变量与逃逸标志的初值（见 4.2.3 的 initial 分支）。

再看构造函数，它把工作台「搭」起来：

> [src/procedural.cc:L112-L118](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L112-L118) —— `ProceduralContext` 构造：用 `netlist.settings.unroll_limit()` 初始化展开限额；`eval(*this)` 让求值上下文回指自己；`root_case = make_unique<Case>()` 建空树；`current_case = root_case->add_switch({})->add_case({})` 在根上挂一个 **signal 为空的平凡 Switch + 一个空 Case** 作为初始外壳。

那个空 Switch 外壳不是摆设：`copy_into` 降级时会把这种「平凡 Switch」拍平（见 u3-l4），所以它不会污染最终 RTLIL，但让「无条件赋值」和「`if` 里的赋值」能走同一套 `current_case->actions.push_back(...)` 代码路径。

`ProcessTiming` 决定了工作台的「时序性格」，它的三种 `Kind` 与触发信号在：

> [src/slang_frontend.h:L224-L245](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L224-L245) —— `ProcessTiming`：`Initial`/`Implicit`/`EdgeTriggered` 三类，`background_enable`（背景使能，默认 `S1`）与 `triggers`（触发信号 + 边沿极性）。`implicit`/`initial` 两个静态实例分别代表组合与初始化时序。

最后，`inherit_state` 提供了一种「把另一个上下文的变量状态和副作用顺序过继过来，但不借它的 timing」的能力——它被异步复位拆分（u6）等场景用来在同一个 Process 里拼接多个 `ProceduralContext`：

> [src/procedural.cc:L123-L134](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L123-L134) —— `inherit_state`：把 `seen_blocking/nonblocking_assignment`、`preceding_memwr`、`vstate`、`flag_counter` 整体复制过来。注释点明这是「借状态与副作用顺序，不借 ProcessTiming」。

#### 4.1.4 代码实践

**实践目标**：确认「一个过程块 = 一个 `ProceduralContext` = 一个 `RTLIL::Process`」这条对应关系，并找到构造函数建出的初始外壳。

**操作步骤**：

1. 打开 [src/slang_frontend.cc:L1779-L1785](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1779-L1785)，这是 `handle_comb_like_process` 的开头。确认它先 `addProcess()` 建 RTLIL Process，紧接着 `new ProceduralContext(netlist, ProcessTiming::implicit)` 建工作台。
2. 在 [src/procedural.cc:L112-L118](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L112-L118) 的构造函数里，注意 `root_case->add_switch({})` 传入的是空 `SigSpec`——这就是「平凡 Switch」的来源。

**需要观察的现象**：`handle_comb_like_process` 里 `ProceduralContext procedure(...)` 是栈上局部对象，函数返回前一定会在某处调用 `procedure.copy_case_tree_into(proc->root_case)`（见 [src/slang_frontend.cc:L1846](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1846)）。

**预期结果**：你会看到 `addProcess` 与 `copy_case_tree_into` 在同一个函数里成对出现，且 `ProceduralContext` 的生命周期恰好夹在二者之间——这就坐实了「一块一 Process」。

**待本地验证**：若你想动态确认，可在构造函数里加一行 `log("ProceduralContext created\n");`（本讲是源码阅读型实践，修改仅为观察，不要提交），跑 `tests/unit/latch.ys`，数一数打印次数是否等于源码里 `always` 块的个数。

#### 4.1.5 小练习与答案

**练习 1**：`eval` 成员的类型是 `EvalContext`，而 `EvalContext` 里又有一个 `ProceduralContext *procedural` 指针。这个环引用有什么用？

**参考答案**：过程块内求值右值表达式时，若读到某个被本块先前 blocking 赋值改过的变量，需要走 `vstate`（而非静态线）拿「当前值」。`EvalContext::procedural` 让求值入口能拿到 `procedural->vstate` 与 `procedural->substitute_rvalue`，从而实现 blocking 语义下的「读到最近一次赋值」。

**练习 2**：`effects_priority` 为什么用 `--context.effects_priority`（前缀自减）而不是 `context.effects_priority++`？

**参考答案**：它是严格递减的整数戳，先发出的副作用拿到更大的值。这只是一种约定：只要任意两个副作用单元的 `PRIORITY` 互不相同且与源码顺序单调对应即可，下游 `proc` pass 据此排序。用递减还是递增是实现选择，递减让它从 0 开始不必预知总数。

---

### 4.2 VariableState：变量当前值的「可回滚账本」

#### 4.2.1 概念说明

`VariableState` 是 `ProceduralContext` 内嵌的一个小结构体，回答一个问题：**「到目前为止，过程块里每个变量位被算成了什么信号？」**

为什么不直接读 RTLIL 线网？因为过程块翻译期间，赋值的左值是 `VariableBits`（HDL 意图），尚未物化成真实线；而且 blocking 赋值要求「后面的语句读到的是前面刚赋的值」。所以 sv-elab 用一张「逐位的当前值表」`visible_assignments` 来暂存答案。

它的妙处在于**可回滚**：进入一个 `if` 分支前，把账本「拍照」；分支里改的值先记在一本「撤销日志」`revert` 上；退出分支时，根据撤销日志把账本还原成分支前的样子（这样兄弟分支不会互相污染），同时把「本分支算出的新值」交给合并器。这就是 `save` / `restore` 的由来。

数据结构只有两张 map：

> [src/slang_frontend.h:L320-L331](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L320-L331) —— `VariableState`：`Map visible_assignments`（`VariableBit → RTLIL::SigBit`，当前值表）与 `Map revert`（撤销日志）；方法 `set` / `evaluate` / `save` / `restore`。

#### 4.2.2 核心流程

四个操作的语义（`Map = dict<VariableBit, RTLIL::SigBit>`）：

```
set(lhs, value):
    对 lhs 的每一位 bit:
        若 revert 还没记过 bit:
            revert[bit] = visible_assignments 里 bit 的旧值;  （没有旧值则记 RTLIL::Sm 哨兵）
        visible_assignments[bit] = value 的对应位

evaluate(netlist, vbits):
    对 vbits 的每一位:
        若在 visible_assignments 里 → 返回它（过程块局部当前值）
        否则（必为 Static 变量）→ 返回真实线网位 wire(symbol)[offset]

save(save_map):
    revert.swap(save_map)        # 把撤销日志整体交给调用方，自己换上空日志

restore(save_map):
    收集 revert 里的所有 bit → lreverted，及它们在 visible_assignments 里的「分支新值」→ rreverted
    把 visible_assignments 按 revert 还原（Sm 哨兵表示「原本没有」，删掉）
    save_map.swap(revert)        # 把保存的外层日志换回 revert
    return (lreverted, rreverted) # 交给合并器：哪些位、在本分支被算成了什么
```

关键直觉：**`visible_assignments` 永远反映「当前控制流位置的变量取值」**；`revert` 是「为了能回退而记的改动日志」。`Sm`（mark）作为哨兵表示「这个位在本次 set 之前根本不在表里」。

`set` 之所以在更新前把旧值塞进 `revert`，是为了让单次赋值天然可撤销；而 `save` 用 `swap` 把整段 `revert` 「截」出来，是为了把「一个分支的全部改动」打包成一个可还原、可汇报的单位。

#### 4.2.3 源码精读

先看 `set`——注意它在写入新值前，先把旧值（或 `Sm` 哨兵）存进 `revert`：

> [src/slang_frontend.cc:L511-L527](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L511-L527) —— `VariableState::set`：逐位写入 `visible_assignments`，同时为每位在 `revert` 里留底（已记过则不覆盖，未记过则用 `RTLIL::Sm` 标记「原本不存在」）。

`evaluate` 是「读变量当前值」的入口，优先查局部表，查不到才回退到静态线网：

> [src/slang_frontend.cc:L529-L543](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L529-L543) —— `VariableState::evaluate(netlist, vbits)`：`Dummy` 变量给 `Sx`；在表里则取表值；否则断言是 `Static` 并取真实线网位。这正是 blocking 语义下「读到最近赋值」的实现。

`save` / `restore` 是一对，靠 `swap` 移交撤销日志：

> [src/slang_frontend.cc:L559-L586](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L559-L586) —— `save` 把 `revert` 整体 `swap` 给调用方传入的 map（开启一段新的撤销日志）；`restore` 先把 `revert` 里的位排序、收集它们的「分支新值」`rreverted`，再按 `revert` 把 `visible_assignments` 还原（`Sm` 表示原本没有，`erase` 掉），最后把外层日志 `swap` 回 `revert`，并返回 `(lreverted, rreverted)`。

`restore` 返回的二元组很关键：`lreverted` 是「本分支动过的位」，`rreverted` 是「这些位在本分支被算成的值」。合并器（`SwitchHelper::finish`）正是靠它知道「每个分支给每个变量算出了什么」。

`VariableState` 的两个直接消费者在 `ProceduralContext` 内：

- `update_variable_state`：赋值的统一入口。它先做 blocking/nonblocking 诊断、initial 特判，最后在 `mask.is_fully_ones()` 时直接 `vstate.set`，否则用 `$bwmux` 把「背景值（`vstate.evaluate`）」与「新值」按掩码混合后再 `set`：

> [src/procedural.cc:L293-L304](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L293-L304) —— `current_case->actions.push_back(...)` 记录 HDL 意图；随后若 mask 全 1 走 `vstate.set` 直通，否则 `rvalue = Bwmux(vstate.evaluate(...), unmasked_rvalue, mask)` 再 `set`。这部分 u3-l4 已细讲，这里强调它对 `vstate` 的读写。

- `substitute_rvalue`：当过程块内某处要「读一个左值的当前值」（典型如函数返回值、`break` 条件）时，调用 `vstate.evaluate`：

> [src/procedural.cc:L338-L349](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L338-L349) —— 非 initial 分支里，对每个 chunk：若变量非 Static、或曾被 blocking 赋值，则 `vstate.evaluate`（走局部当前值表）；否则 `netlist.convert_static`（直接取静态线）。

initial 过程则完全绕开 `vstate`，改写 `initial_locals_state` / `netlist.initial_state`（因为 initial 里不允许非阻塞赋值，且要求常量求值）：

> [src/procedural.cc:L250-L291](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L250-L291) —— `timing.kind == Initial` 时：先 `crop_undef_mask`，断言右值全常量，再按位写入 `initial_locals_state`（局部/逃逸标志）或 `netlist.initial_state`（静态变量），存储器则调 `add_memory_init`。这就是 `initial_locals_state` 成员的用途。

#### 4.2.4 代码实践

**实践目标**：用一个最小例子跟踪一次 `set`，看清 `visible_assignments` 与 `revert` 的变化。

**操作步骤**：

1. 准备一段组合块（**示例代码**，非项目原有）：

   ```systemverilog
   module demo(input logic s, input logic [1:0] a, b, output logic [1:0] y);
       always_comb begin
           y = a;       // 第一次赋值 y
           if (s)
               y = b;   // 分支里再赋值 y
       end
   endmodule
   ```

2. 在 [src/slang_frontend.cc:L511-L527](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L511-L527) 的 `set` 里，对 `y` 的每一位推演：第一次 `y = a` 时，`revert[y[0]]`、`revert[y[1]]` 会被写成 `RTLIL::Sm`（因为此前表里没有 y），`visible_assignments[y[*]] = a[*]`。

**需要观察的现象**：跟踪到 `if (s)` 分支里的 `y = b` 时，进入分支前会先 `save`（把含 `y→Sm` 的 `revert` 截走），分支内第二次 `set(y, b)` 时 `revert` 是空的、会重新记 `revert[y[*]] = a[*]`（即分支前的值）。

**预期结果**：`set` 每次都「先留底、再覆盖」，因此任意时刻都能凭 `revert` 回退到上一个快照——这是分支互不污染的根。

**待本地验证**：可在 `set` 里临时加日志打印 `bit` 与写入前后的 `visible_assignments[bit]`，编译后用上面 `demo` 跑 `read_slang`，核对打印顺序与你的推演一致。

#### 4.2.5 小练习与答案

**练习 1**：`evaluate` 对「表里没有的位」断言 `vbit.variable.kind == Variable::Static` 并取真实线网。为什么 `Local`/`EscapeFlag` 变量不允许走这条回退路径？

**参考答案**：局部变量与逃逸标志没有「对应的静态线网」（它们是过程块内的虚拟变量，物化要靠 `SwitchHelper::finish` 建占位线）。读到它们时必须已经在 `visible_assignments` 里（即已被赋值或被 finish 建出的合并线覆盖），否则就是「使用未赋值的自动变量」，属于不该出现的状态，故用断言拦截。

**练习 2**：`restore` 里 `if (pair.second == RTLIL::Sm) visible_assignments.erase(pair.first);` 这行的 `Sm` 是什么意思？若改成 `visible_assignments[pair.first] = pair.second;` 会怎样？

**参考答案**：`Sm` 是「本次 set 之前该位根本不在表里」的哨兵。还原时应当 `erase` 掉它（恢复「不存在」），而不是把字面值 `Sm` 当信号写进去；若直接赋值，会把一个不存在的位强行设成 mark 状态，污染后续 `evaluate`。

---

### 4.3 分支建模与合并：`if` 如何变成一个 Process

> 这是本讲的核心，也是实践任务聚焦之处。

#### 4.3.1 概念说明

过程块里有 `if`/`case` 时，难点在于：**不同分支给同一变量算出了不同的值，怎么把它们合并？** 而且 blocking 语义要求「`if` 之后的语句读到的是按条件选择后的结果」。

sv-elab 的解法是把「分支」与「合并」拆成三步，全部围绕 `VariableState` 的可回滚性：

1. **进分支（`enter_branch`）**：`vstate.save(save_map)`——把账本「拍照」，开启一段干净的撤销日志。
2. **跑分支体**：语句照常赋值，`vstate` 记录本分支算出的新值，同时 `revert` 累积本分支的改动。
3. **出分支（`exit_branch`）**：`vstate.restore(save_map)`——把账本还原成分支前（兄弟分支互不污染），同时拿到 `(本分支动过的位, 本分支算出的值)` 二元组存进 `branch_updates`。
4. **合并（`finish`）**：对所有「任一分支动过的变量」建一根占位「合并线」`w`，其默认值是分支前的背景值；在每个分支的 `Case` 里加一条 `aux_action` 把 `w` 连到该分支的值。再把 `vstate` 里该变量的当前值改成 `w`——于是 `if` 之后的语句读到的就是「按条件选择后的合并线」。

这套机制由 `SwitchHelper`（[src/statements.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h)）封装。`StatementExecutor` 遇到 `if`/`case` 时构造一个 `SwitchHelper`，逐分支 `branch(...)`，最后 `finish(...)`。

#### 4.3.2 核心流程

以 `if (s) x = a; else x = b;`（其后还有 `y = x;`）为例，`vstate` 与 case 树的演化：

```
进 if 前： visible_assignments: {} （x 尚未被赋值，x 是 Static 变量）
            current_case = C0（平凡外壳下的当前 Case）

SwitchHelper sw(signal = s);
  sw.branch({1'b1}):           ── 进入 then 分支
    save(save_map):            revert 被截走（此刻含先前改动，本例为空）
    current_case = C_then
    x = a  → set(x, a):        visible_assignments: {x:a}
                               revert: {x: Sm}     （x 此前不在表）
    restore(save_map):         还原 visible_assignments: {} （x 删掉）
                               branch_updates += (C_then, x, a)
                               save_map 换回外层 revert
  sw.branch({default}):        ── 进入 else 分支
    save → set(x, b) → restore：branch_updates += (C_else, x, b)

sw.finish(netlist):
    updated_anybranch = {x}
    为 x 建合并线 w，默认值 = vstate.evaluate(x) = 背景值（x 的静态线）
    parent(C0).aux_actions += {w, 背景值}
    C_then.aux_actions += {w, a}
    C_else.aux_actions += {w, b}
    vstate.set(x, w):          visible_assignments: {x: w}   ← 之后读 x 拿到 w

其后 y = x; → 读 x 走 evaluate → 拿到 w（合并线）
```

数学上，合并线 `w` 实现的就是一个按 `s` 选择的 mux：

\[
w = \begin{cases} a & \text{if } s = 1 \\ b & \text{if } s = 0 \end{cases}
\]

但因为 `x` 可能在 `if` 之前已有值（背景值 `x₀`），且分支可能不完整（如只有 `if (s) x = a;`，无 else），默认值取背景值 `x₀` 就自然表达了「未赋值则保持」。这也正是**不完整赋值 → 锁存器**的来源（u6-l3）。

降级时，`SwitchHelper` 构造的 `Switch(signal=s)` 与其下两个 `Case` 经 `copy_into` 变成 `RTLIL::SwitchRule(signal=s)` + 两个 `RTLIL::CaseRule`，每个 `CaseRule` 的 `actions` 里有 `w = a` / `w = b`。整棵树挂在**同一个** `RTLIL::Process` 的 `root_case` 下。

#### 4.3.3 源码精读

`SwitchHelper` 的进/出分支是 save/restore 的直接调用点：

> [src/statements.h:L71-L90](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L71-L90) —— `enter_branch`：`save_map.clear(); vstate.save(save_map);` 把当前撤销日志截走，然后把 `current_case` 切到新分支 `sw->add_case(compare)`。`exit_branch`：`vstate.restore(save_map)` 还原账本并取回 `(lreverted, rreverted)`，连同当前分支 `Case*` 一起存入 `branch_updates`，再把 `current_case` 切回父节点。

注意 `branch(compare, f)` 还做了一个小优化——死分支检测：

> [src/statements.h:L92-L104](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L92-L104) —— 若 `compare` 与 `signal` 都是编译期常量且不相等，直接 `return`，连分支都不进。

合并逻辑全部在 `finish`：

> [src/statements.h:L106-L141](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L106-L141) —— `finish`：先把各分支 `branch_updates` 汇总成 `updated_anybranch`（任一分支动过的位）；对其中每个**非 Static 且在父作用域已无可见赋值**的变量计入 `eos_variables`（end-of-scope，超生命周期不再合并）；对其余每个 chunk 建占位合并线 `w`，默认值 `w_default = vstate.evaluate(netlist, chunk)`（背景值），写入 `parent->aux_actions`，并 `vstate.set(chunk, w)` 让父作用域之后读到 `w`。

随后 `finish` 的第二段循环把每个分支算出的值接回各自的 `w`：

> [src/statements.h:L143-L167](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L143-L167) —— 遍历 `branch_updates`，对每个分支 `(rule, target, source)`：从 `vstate.visible_assignments`（此刻已是合并线 `w`）取出 `target_w`，在分支 `rule` 里加 `aux_action {target_w, source 的对应段}`——也就是「本分支把合并线连成本分支的值」。`eos_variables` 跳过。

把三段串起来读：`enter_branch`→分支体→`exit_branch` 改的是 `vstate` 与 `branch_updates`；`finish` 把 `branch_updates` 翻译成「父作用域的合并线 + 各分支的条件连线」，并更新 `vstate` 让后续语句读到合并结果。整个过程**没有新建 `RTLIL::Process`**，只是在同一棵 case 树上长出 `Switch`/`Case` 与 `aux_actions`，最后由 `copy_case_tree_into` 一次性降级——这就是「合并回单一 Process」。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：跟踪 `tests/unit/latch.ys` 里 `latch01_gate` 的不完整 `if`，解释 `VariableState` 如何在分支里 save/restore，以及为何最终落在**一个** Process 上（并触发锁存器）。

**操作步骤**：

1. 阅读 [tests/unit/latch.ys:L1-L30](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/latch.ys#L1-L30)。被测设计是：

   ```systemverilog
   always @(*) begin
       if (en) q = d;     // 只有 then 分支，没有 else
   end
   ```

2. 推演 `SwitchHelper` 的执行（`signal = en`）：
   - **then 分支**（`compare = {1'b1}`）：`save` → 体里 `q = d` 使 `set(q, d)`，`revert = {q[*]: Sm}` → `restore` 还原 `q`（删掉），`branch_updates += (C_then, q[3:0], d[3:0])`。
   - **else/默认分支**：`en=0` 时没有对 `q` 的赋值，`branch_updates += (C_default, ∅, ∅)`（`q` 未动）。
   - **finish**：`updated_anybranch = {q[3:0]}`；为 `q` 建合并线 `w`，默认值 `vstate.evaluate(q)` = `q` 的静态背景线（这里 `q` 是输出，背景即其本身）；`C_then.aux_actions += {w, d}`；`vstate.set(q, w)`。

3. **为什么是锁存器**：`q` 在 `en=0` 分支未被赋值，合并线 `w` 的默认值取的是「`q` 的背景值」——也就是 `q` 自己。这构成一个保持回路（`q` 在 `en=0` 时保持原值），`detect_possibly_unassigned_subset` 据此判定 `q` 为悬空位，最终由 `handle_comb_like_process` 调 `addDlatch` 发出 `$dlatch`（见 [src/slang_frontend.cc:L1816-L1843](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1816-L1843)）。对照 `latch.ys` 里的 gold 网表：正是一个 `cell $dlatch`，`EN=\en, D=\d, Q=\q`。

**需要观察的现象**：整段 `always @(*)` 只产生**一个** `RTLIL::Process`（一次 `addProcess` + 一次 `copy_case_tree_into`）；`if` 没有另开 Process，而是变成同一 Process 内的一棵 `Switch`/`Case`，最终被锁存器信号机制改写。

**预期结果**：`equiv_induct` 通过（gate 的 `$dlatch` 与 gold 的 `$dlatch` 等价）。这反向印证了「分支靠 save/restore+finish 合并、整块落一个 Process」是正确的。

**待本地验证**：按 [tests/run.sh](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/run.sh) 的方式 `yosys -m build/slang.so tests/unit/latch.ys`，在 `read_slang` 之后加一句 `show -format dot` 或 `dump` 查看生成的 Process/cell，确认只有一个 `$dlatch`。

#### 4.3.5 小练习与答案

**练习 1**：若把 `latch01` 改成完整赋值 `if (en) q = d; else q = 0;`，`finish` 里 `w` 的默认值还会被用到吗？还会推断出锁存器吗？

**参考答案**：不会用到，也不会有锁存器。因为 `q` 在两个分支都被完整赋值，`detect_possibly_unassigned_subset` 找不到悬空位；合并线 `w` 的默认值（背景值）虽仍被算出并写进 `parent->aux_actions`，但被两个分支都覆盖，等价于纯组合 mux，`q` 经 `add_continuous_driver` 直接连到 `w`。

**练习 2**：`enter_branch` 里 `save_map.clear()` 之后才 `vstate.save(save_map)`。为什么要先 clear？

**参考答案**：`save_map` 是 `SwitchHelper` 的成员，可能在上一条 `if`（外层或兄弟）复用同一 helper 时残留旧内容。`save` 用 `swap` 接管 `revert`，若 `save_map` 非空会把旧日志误当作「外层要还原的目标」。先 `clear` 保证本次 save 拿到的是干净的容器。

**练习 3**：`finish` 里为何要区分 `eos_variables`（end-of-scope）？

**参考答案**：局部变量（`Variable::Local`，如函数内自动变量、命名块内变量）若仅在分支内被赋值且在父作用域的 `visible_assignments` 里已不可见，说明它「随分支结束而消亡」，不需要在父作用域合并、也不该建合并线泄漏到外面。`eos_variables` 把这类变量挑出来跳过，避免为短生命周期变量生成无意义的连线。

---

### 4.4 收尾：all_driven 与 copy_case_tree_into

#### 4.4.1 概念说明

语句遍历完后，`ProceduralContext` 还要做两件收尾的事，对应两个最小模块：

- **`all_driven()`**：回答「这个过程块到底驱动了哪些**静态**变量位？」它扫一遍 `vstate.visible_assignments`，挑出 `Variable::Static` 的位。组合块据此决定把哪些信号连成连续驱动、哪些是悬空（锁存器）。
- **`copy_case_tree_into(RTLIL::CaseRule&)`**：把 HDL 意图树（`root_case`）降级成 `RTLIL::CaseRule`，挂到 `RTLIL::Process::root_case` 上。降级只搬运 `aux_actions`（已物化的连线）与 `Switch`/`Case` 结构；`actions`（带 `VariableBits` 左值的 HDL 意图）的语义已被变量状态、`SwitchHelper::finish`、`add_continuous_driver` 等编译掉（u3-l4 已述）。

#### 4.4.2 核心流程

```
all_driven():
    收集 visible_assignments 的所有 key → all_driven
    sort_and_unify
    只保留 Variable::Static 的 chunk
    return all_driven_filtered

copy_case_tree_into(rule):
    root_case->copy_into(netlist, &rule)   ← 见 cases.h 的 Case::copy_into
```

#### 4.4.3 源码精读

`all_driven` 把「过程块局部可见的赋值」过滤成「对静态外信号的驱动」：

> [src/procedural.cc:L141-L156](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L141-L156) —— `all_driven`：遍历 `vstate.visible_assignments` 收集所有位，排序合并，再过滤掉非 `Static` 的 chunk（局部变量、逃逸标志不计入「对外驱动」）。

`copy_case_tree_into` 只是一行转发，真正活儿在 `Case::copy_into`：

> [src/procedural.cc:L136-L139](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L136-L139) —— `copy_case_tree_into`：`root_case->copy_into(netlist, &rule)`。

> [src/cases.h:L84-L113](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h#L84-L113) —— `Case::copy_into`：把 `compare` 与 `aux_actions` 复制进 `RTLIL::CaseRule`，然后逐个子 `Switch` 调 `lower`（其中 `trivial()` 平凡外壳被拍平以降低树深，加速下游 `proc_prune`）。注意它只搬 `aux_actions`，不搬 `actions`。

`all_driven` 与 `copy_case_tree_into` 在 `handle_comb_like_process` 里如何配合，是理解「合并回单一 Process」的最后一块拼图：

> [src/slang_frontend.cc:L1787-L1807](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1787-L1807) —— `all_driven()` 取出被驱动位；`detect_possibly_unassigned_subset` 找悬空位；未悬空的位经 `vstate.visible_assignments.at(bit)` 取到合并后的值（4.3 里 finish 建出的合并线 `w`），凑成连续驱动 `cl/cr`，悬空的进 `latch_driven` 走锁存器路径。

> [src/slang_frontend.cc:L1846](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1846) —— `procedure.copy_case_tree_into(proc->root_case)`：把这唯一一棵树挂到先前 `addProcess` 建出的 Process 上。

另外，`case_enable()` 为副作用单元（`$check`/`$print`）提供「当前 case 节点是否激活」的使能信号，是 `set_effects_trigger` 的依赖：

> [src/procedural.cc:L158-L168](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L158-L168) —— `case_enable`：initial 过程恒为 `S1`；否则建一根占位使能位，在 `root_case` 置 0、在 `current_case` 置 1，从而表达「当前分支激活」。

#### 4.4.4 代码实践

**实践目标**：确认「整个组合块只产出零个或一个 Process 之外的连续驱动，case 树被降级到唯一 Process」。

**操作步骤**：

1. 在 [src/slang_frontend.cc:L1779-L1846](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1779-L1846) 通读 `handle_comb_like_process`，数 `addProcess` 与 `copy_case_tree_into` 各出现几次（应各 1 次）。
2. 注意组合块里完整赋值的变量**不进 Process**：它们经 `add_continuous_driver`（u3-l1）变成模块顶层的连续连线，Process 里只剩锁存器/触发器相关结构。

**需要观察的现象**：`all_driven()` 返回的位会被一分为二——非悬空位走连续驱动，悬空位走 `addDlatch`；两类结果最终都挂在同一模块、同一（或零个）Process 体系下。

**预期结果**：对一个纯组合的完整 `always_comb`，`copy_case_tree_into` 后的 Process 在 `proc` pass 处理后会被完全展平成门，不留 Process 残骸。

**待本地验证**：对 4.2.4 的 `demo` 跑 `read_slang` 后用 `dump` 查看，确认完整赋值的 `y` 走的是连续驱动而非 Process。

#### 4.4.5 小练习与答案

**练习 1**：`all_driven` 为什么要过滤掉非 `Static` 的 chunk？

**参考答案**：`Local`/`EscapeFlag`/`Dummy` 变量是过程块内部的虚拟变量，没有对外可见的信号；只有 `Static` 变量（模块级 `reg`/`wire`/输出等）才构成「本过程对外部网表的驱动」。过滤掉内部变量避免把合并线误当作对外驱动。

**练习 2**：`copy_into` 降级时为什么不搬运 `Case::actions`（带 `VariableBits` 左值的那些）？

**参考答案**：`actions` 是 HDL 意图，左值是 `VariableBits` 而非真实信号，无法直接写进 `RTLIL::CaseRule`（后者只接受 `SigSig` 连线）。它们的语义已经在翻译期被「消费掉」：完整赋值经 `SwitchHelper::finish` 变成合并线、再经 `add_continuous_driver` 物化；锁存器位经 `insert_latch_signaling` 改写成对 staging/en 信号的 `aux_actions`。所以降级时只剩 `aux_actions` 需要搬。

---

## 5. 综合实践

把本讲四块串起来，做一次完整的「过程块翻译」推演。

**任务**：对下面这段（**示例代码**，非项目原有），手工模拟 `ProceduralContext` 的翻译全过程，画出最终的 case 树与 `vstate` 终态。

```systemverilog
module top(input logic clk, input logic [7:0] a, input logic sel,
           output logic [7:0] y);
    logic [7:0] r;            // 静态变量
    always_comb begin
        r = a;                // (1) blocking 赋值
        if (sel)              // (2) if 分支
            r = a + 8'd1;     //     then: r = a+1
        // 无 else
        y = r;                // (3) 读 r，应拿到合并线
    end
endmodule
```

要求：

1. **指出**会构造几个 `SwitchHelper`、几个 `RTLIL::Process`。
2. **推演**语句 (1) 后 `visible_assignments` 与 `revert` 的内容；进入 then 分支时 `save` 截走了什么；then 分支里 (2) 的赋值后 `revert` 记了什么；`exit_branch` 后 `branch_updates` 是什么。
3. **说明**`finish` 为 `r` 建的合并线 `w_r` 的默认值是什么（提示：背景值 = `r` 的静态线，但 `r` 在语句 (1) 已被赋成 `a`，注意 `finish` 在 `if` 这层只看 `if` 之内的改动）。
4. **判断**`y` 最终是连续驱动还是进 Process？`r` 是否会推断锁存器？为什么？
5. **验证**：把这段写进一个 `.sv`，参考 `tests/unit/latch.ys` 的等价性测试范式（配一个行为等价的 gold），用 `yosys -m build/slang.so` 跑 `equiv_induct` 验证你的推演（**待本地验证**）。

**参考要点**：

1. 一个 `SwitchHelper`（对应 `if (sel)`），一个 `RTLIL::Process`（一个 `always_comb`）。
2. (1) 后：`visible_assignments = {r[*]: a[*]}`，`revert = {r[*]: Sm}`。进 then 前 `save` 截走含 `r[*]:Sm` 的日志；then 内 `r=a+1` 使 `revert = {r[*]: a[*]}`（旧值即 (1) 赋的 a）；`exit_branch` 后 `branch_updates = (C_then, r[7:0], (a+1)[7:0])`，`visible_assignments` 被还原回 `{r[*]: a[*]}`。
3. `w_r` 默认值 = `vstate.evaluate(r)` = `a`（被 (1) 设进表的当前值）；`C_then.aux_actions += {w_r, a+1}`；`vstate.set(r, w_r)`。故 (3) `y = r` 读到 `w_r`。
4. `y`、`r` 都**不会**推断锁存器。关键在 (3) 点里 `w_r` 的默认值取的是「`r` 的当前值 = `a`」——这个 `a` 正是语句 (1) `r = a` 留下的背景值。于是 `w_r = sel ? (a+1) : a`，对 `sel` 的所有取值都有定义；`r` 和 `y` 都是完整赋值 → 都走连续驱动（连到 `w_r`），Process 在 `proc` pass 后被完全展平。**这正是「背景值合并」机制的价值**：`if` 之前的一次无条件赋值会自动填充未被选中的分支，从而避免锁存器。对照 4.3.4 的 `latch01`——那里 `q` 没有「前置无条件赋值」作背景，`en=0` 时只能保持自身，才推断出 `$dlatch`。两例一对照，就能看清「悬空与否」的本质。

> 这道综合题把 `ProceduralContext` 的构造、`VariableState` 的 set/save/restore、`SwitchHelper::finish` 的合并、以及 `all_driven`/`copy_case_tree_into` 的收尾全部串到了一条真实翻译路径上。

## 6. 本讲小结

- `ProceduralContext` 是翻译**一个**过程块的工作台：持有 case 树（`root_case`/`current_case`）、变量当前值（`vstate`）、时序（`timing`）、逃逸栈与副作用计数（`effects_priority`）。**一块对应一个 `RTLIL::Process`**。
- `VariableState` 是一张「可回滚账本」：`visible_assignments` 记当前值，`revert` 记本次改动日志（`Sm` 哨兵表示「原本不存在」）。`set` 先留底再覆盖，`evaluate` 优先查表否则回退静态线，`save`/`restore` 靠 `swap` 移交撤销日志。
- 分支合并靠 `SwitchHelper`：`enter_branch`→`save`、分支体→`set`、`exit_branch`→`restore`（还原 + 取回分支新值）、`finish`（建合并线、各分支条件连线、更新 `vstate`）。整段 `if`/`case` 不新开 Process，而是在同一棵 case 树上生长。
- `all_driven` 把过程块对**静态**变量的驱动汇总出来（供连续驱动 / 锁存器判定）；`copy_case_tree_into` 经 `Case::copy_into` 把 HDL 意图树降级成 `RTLIL::CaseRule`（只搬 `aux_actions`，`actions` 的语义已被编译掉）。
- `effects_priority` 给 `$print`/`$check`/`$memwr` 等副作用单元盖严格递减的顺序戳；`initial_locals_state` 在 initial 过程里按位存局部变量/逃逸标志的初值（绕开 `vstate`）。

## 7. 下一步学习建议

- **u5-l2 StatementExecutor 与 SwitchHelper**：本讲只用了 `SwitchHelper` 的 save/restore/finish；下一讲会展开 `StatementExecutor` 如何对 `if`/`for`/`case` 各类语句分派，并更完整地讲 `SwitchHelper` 的生命周期与死分支检测。
- **u5-l3 赋值处理与位掩码**：深入 `assign_rvalue` / `assign_to_lvalue_with_masking` / `update_variable_state` 的掩码混合（`$bwmux`），以及 blocking/nonblocking 混用诊断（可先看 `tests/various/assign_mixing.ys`）。
- **u5-l4 逃逸构造与循环展开**：本讲提到的 `escape_stack`、`EscapeFrame`、`RegisterEscapeConstructGuard`、`UnrollLimitTracking` 如何实现 `break/continue/return` 与循环展开限额。
- 若想先看时序，可跳到 **u6-l1 TimingPatternInterpretor**：看 `ProcessTiming::EdgeTriggered` 是怎么从 always 块里识别出来的，以及 `inherit_state` 在异步复位拆分里的实际用法。
