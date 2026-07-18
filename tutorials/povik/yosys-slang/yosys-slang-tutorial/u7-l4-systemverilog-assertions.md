# 讲义：SystemVerilog 断言（SVA）

## 1. 本讲目标

本讲讲解 sv-elab 如何把 SystemVerilog 断言（SystemVerilog Assertions，简称 SVA）翻译成「可综合」的 RTLIL 检查单元。学完后你应该能够：

- 说清楚一条并发断言（`assert property (...)`）从 slang AST 到 RTLIL `$check` 单元的完整路径；
- 区分两个翻译入口 `process_sva_property`（核心翻译）与 `process_freestanding_sva_property`（模块级自由站立断言的时钟提取）的职责与调用关系；
- 理解 `EvalContext::sva` 引入的「SVA 特殊求值规则」（`in_sva_expression` 标志）为什么不同于普通过程块求值；
- 掌握 `$past` 系统函数如何用一串触发器实现「上一拍的值」；
- 了解 `--ignore-assertions` 选项的作用，以及哪些 SVA 特性会被 sv-elab 拒绝并触发诊断。

本讲承接 u5-l4（逃逸构造与循环展开），属于单元 7（高级主题）。读者应已经理解 u5 的 `ProceduralContext`/`StatementExecutor` 与 u6 的 `ProcessTiming`（组合 / Initial / 边沿触发）。

## 2. 前置知识

如果你没用过 SVA，下面几个概念够用了：

- **断言（assertion）**：在代码里声明「某个布尔条件应当成立」的语句。它本身不是数据通路，而是给仿真器、形式化工具或综合工具的「提示 / 检查」。SV 有三种风味：
  - `assert`：断言条件必须成立，违反即报错；
  - `assume`：假定条件成立（约束输入，常用于形式化）；
  - `cover`：覆盖目标，统计该条件被命中的次数。
- **立即断言（immediate assertion）**：写在过程块里、形式为 `assert(expr);` 的语句，求值的是「当下」的表达式值。
- **并发断言（concurrent assertion）**：形式为 `assert property (@(posedge clk) expr);`，它描述的是「在时钟边沿采样到的值」之间的关系，带有时序语义。本讲主角就是它。
- **时钟事件控制 `@(posedge clk)`**：规定断言在 `clk` 上升沿采样。
- **`disable iff (cond)`**：当 `cond` 为真时「暂停」该断言的检查（典型用于复位期间）。
- **`$check` 单元**：Yosys RTLIL 里表示一条断言的单元，带 `FLAVOR`（assert/assume/cover）、触发参数（`TRG_*`）、使能 `EN` 和被检查条件 `A` 等端口。sv-elab 只负责「发出」这个字级单元，真正消费它（例如用于形式化证明或丢弃）的是下游 pass。
- **`$past(expr)`**：SVA 系统函数，返回 `expr` 在「上一个时钟沿」的值。

一句话定位：slang 已经把 SV 断言解析成了一棵 `AssertionExpr` AST；sv-elab 的工作是把这棵树里「可综合的子集」翻译成一个挂在某个 `ProcessTiming` 上的 `$check` 单元，剩下的（带时序算子、重复等纯仿真特性）则发诊断拒绝。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/sva.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc) | SVA 翻译的全部逻辑：`process_sva_property` 与 `process_freestanding_sva_property` 两个函数。 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | `EvalContext::sva`、`handle_past`、`ProcessTiming::extract_trigger`、自由站立断言的调用点、`--ignore-assertions` 选项注册。 |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | `EvalContext::sva` 与 `in_sva_expression` 字段声明、`ProcessTiming` 结构体、`SynthesisSettings::ignore_assertions`。 |
| [src/statements.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h) | 过程块内的 `handle(ImmediateAssertionStatement)` 与 `handle(ConcurrentAssertionStatement)`。 |
| [src/diag.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) / [src/diag.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h) | SVA 相关诊断码的定义、文案与严重级别。 |
| [tests/various/concurrent_assert.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/concurrent_assert.sv) / [tests/various/concurrent_assert.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/concurrent_assert.ys) | 并发断言等价性 / 计数测试与本讲综合实践的活样本。 |

---

## 4. 核心概念与源码讲解

### 4.1 process_sva_property：并发断言的核心翻译

#### 4.1.1 概念说明

`process_sva_property` 是所有「可综合并发断言」最终都会流经的核心函数。它的契约很清晰：

- **输入**：一条 `ConcurrentAssertionStatement`（slang 对 `assert property (...)` 的 AST 表示）、它所在的语句块符号 `block`、一个**已经配好时序的** `ProceduralContext &procedural`、以及「剥掉顶层时钟后的」属性表达式 `top_expr`。
- **输出**：在该过程块上挂一个 `$check` 单元，并把被检查的条件连到它的 `A` 端口。

注意它**不创建 `RTLIL::Process`**——`Process` 由调用方（过程块语句执行器或自由站立入口）负责创建和最终 `copy_case_tree_into`。`process_sva_property` 只负责往「当前 case 树」上追加一个副作用单元。这与 u5 讲过的「过程块翻译成一棵 case 树、副作用单元（`$check`/`$print`）挂在 case 节点上」是一致的。

为什么断言被当作「副作用单元」？因为断言不产生值、不驱动线网，它只是在某个时序点「检查一个条件」，与 `$display` 这类打印同属一类——都由 `set_effects_trigger` 绑定触发时机与使能。

#### 4.1.2 核心流程

`process_sva_property` 的执行可以拆成五步：

1. **剥离 `disable iff`**：若顶层是 `DisableIffAssertionExpr`，用一个 `SwitchHelper` 把断言体放进「disable 条件为假」的分支里（即只有未 disable 时才检查）。
2. **形态校验**：只接受 `SimpleAssertionExpr`；带重复的简单断言也拒绝。
3. **确定 flavor**：按 `assertionKind` 映射成 `"assert"`/`"assume"`/`"cover"` 字符串。
4. **取单元名**：若断言是某具名块的唯一语句，用块名作单元名（保留 label），否则生成新名。
5. **求值并发射 `$check`**：用 `eval.sva(...)` 把布尔条件求值成 1 位信号，建 `$check` 单元，设 `FLAVOR`/`PRIORITY` 等参数，连 `A` 端口，调 `set_effects_trigger` 绑定时序。

`disable iff` 的处理用到了 u5-l2 的 `SwitchHelper`——它本是为 `if`/`case` 分支汇合设计的，这里被「借用」来表达「条件性检查」：进入「条件为 0」的分支执行检查体，`ScopeGuard` 在函数末尾自动 `exit_branch` + `finish`。

伪代码：

```
function process_sva_property(stmt, block, procedural, top_expr):
    expr = top_expr
    if expr is DisableIff:
        switch = SwitchHelper(procedural, ReduceBool(sva(disable.condition)))
        switch.enter_branch({S0})        # 进入「条件==0（未 disable）」分支
        expr = disable.expr
    # guard: 函数返回时 switch.exit_branch() + switch.finish()

    if expr not SimpleAssertionExpr:
        add_diag(UnsupportedSVAFeature); return
    if simple_assertion.repetition:
        add_diag(RepetitionsUnsupported); return

    flavor = {Assert:"assert", Assume:"assume", Cover:"cover"}[stmt.kind]
    cell_name = block 名（若是具名块唯一语句）否则 new_id()

    A = ReduceBool(sva(simple_assertion.expr))     # 1 位布尔条件
    cell = canvas.addCell(cell_name, $check)
    set_effects_trigger(cell)                       # 绑定 TRG/EN
    cell.FLAVOR = flavor; cell.PRIORITY = --effects_priority
    cell.A = A
```

#### 4.1.3 源码精读

函数签名与「时钟已剥离、时序在 procedural 里」的契约：

> [src/sva.cc:L28-L34](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L28-L34) —— 入参 `top_expr` 即「已经剥掉顶层时钟」的属性表达式；注释明说「Any top level clocking expressions have been stripped. Clocking is part of the created procedural context.」

`disable iff` 的剥离与 `ScopeGuard` 自动收尾：

> [src/sva.cc:L40-L53](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L40-L53) —— 用 `std::optional<SwitchHelper>` 延迟构造，`ScopeGuard` 在函数末尾（包括提前 `return` 的路径）统一 `exit_branch` + `finish`，保证「条件为 0（`RTLIL::S0`）」分支被正确汇合。这正是 `disable iff (cond)` 的语义：只在 `cond` 为假时检查。

形态校验——只放行「简单断言、无重复」：

> [src/sva.cc:L55-L66](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L55-L66) —— 非 `SimpleAssertionExpr` 发 `UnsupportedSVAFeature`；带 `repetition`（如 `a [*2]`）发 `RepetitionsUnsupported`。这两类是 SVA 的时序算子，不可综合。

flavor 映射与 label 命名：

> [src/sva.cc:L68-L83](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L68-L83) —— `assertionKind` 三选一得 flavor 字符串；具名块的唯一断言用块名作 `$check` 单元名（这就是 `tests/various/concurrent_assert.ys` 里 `n:*my_concurrent_assert` 能匹配到的原因）。

`$check` 单元的发射：

> [src/sva.cc:L85-L95](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L85-L95) —— 关键三步：① `ReduceBool(eval.sva(...))` 把属性表达式求成 1 位；② `canvas->addCell(cell_name, ID($check))` 建单元；③ `set_effects_trigger(cell)` 把当前过程块的时序（触发沿、使能）写进单元的 `TRG_*` 参数与 `EN` 端口。`PRIORITY` 取自递减的 `effects_priority`，用于在同一拍多条断言 / 多次副作用间排序。

`set_effects_trigger` 如何把时序写进 `$check`：

> [src/slang_frontend.cc:L447-L450](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L447-L450) 与 [src/slang_frontend.cc:L407-L444](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L407-L444) —— `set_effects_trigger` 调 `timing.extract_trigger(...)`。对边沿触发（`EdgeTriggered`），设 `TRG_ENABLE=true`、`TRG_WIDTH=triggers.size()`、`TRG_POLARITY` 为各触发沿极性的常量向量、`TRG` 端口接触发信号，`EN = LogicAnd(background_enable, case_enable())`；对组合（`Implicit`），`TRG_ENABLE=false`。

#### 4.1.4 代码实践

**目标**：对照 `tests/various/concurrent_assert.ys`，验证 `m_concurrent_assert`（`assert property (@(posedge clk) data != 8'hFF);`）确实被翻译成一个带正确触发参数的 `$check` 单元。

**步骤**（源码阅读 + 本地可选运行）：

1. 打开 [tests/various/concurrent_assert.sv:L1-L5](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/concurrent_assert.sv#L1-L5)，确认输入设计就是一条 posedge 并发断言。
2. 阅读 [tests/various/concurrent_assert.ys:L6-L10](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/concurrent_assert.ys#L6-L10)，注意三条断言：
   - `select -assert-count 1 m_concurrent_assert/t:$check` —— 恰好 1 个 `$check`；
   - `... r:FLAVOR=assert %i` —— flavor 是 assert；
   - `... r:TRG_ENABLE=1 %i` 与 `... r:TRG_POLARITY=1'1 %i` —— 触发使能开、单沿且极性为 1（posedge）。
3. 回到源码：`posedge clk` 是怎么变成 `TRG_POLARITY=1'1` 的？答案在 `process_freestanding_sva_property`（4.2 节）建 `ProcessTiming` 时把 `signal_event.edge == PosEdge` 映射成 `edge_polarity = true`（[src/sva.cc:L122-L129](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L122-L129)），再由 `extract_trigger` 把 `true` 写成 `RTLIL::S1`（[src/slang_frontend.cc:L426-L430](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L426-L430)）。
4. （本地可选）若已按 u8-l3 构建 `slang.so`，在仓库根目录跑：
   ```
   yosys -m slang.so -p "read_slang tests/various/concurrent_assert.sv; select m_concurrent_assert/t:\$check; show -format dot"
   ```
   预期能看到 1 个 `$check` 单元，其 `A` 端口连着 `data != 8'hFF` 的比较输出，`TRG` 端口连 `clk`。**待本地验证**（取决于环境是否已装好 yosys + 插件）。

#### 4.1.5 小练习与答案

**练习 1**：`process_sva_property` 为什么不自己 `addProcess`，而要把这件事留给调用方？

**参考答案**：因为它要服务于两种调用场景——过程块内的并发断言（`Process` 已由外层 `StatementExecutor` 所在的过程块拥有）和自由站立的并发断言（`Process` 由 `process_freestanding_sva_property` 新建）。把 `addProcess`/`copy_case_tree_into` 留给调用方，核心函数就能保持「只往当前 case 树追加副作用单元」的单一职责。

**练习 2**：`disable iff (rst)` 在电路上如何体现？

**参考答案**：它不产生专门的「disable」单元，而是把检查体放进一个以 `ReduceBool(rst)` 为条件的 `SwitchHelper` 的「条件 == 0」分支。等价于「当 `rst` 为 1 时整个 `$check` 不被该 case 节点激活」，其效果最终通过 `case_enable()` 进入 `$check` 的 `EN` 端口（[src/slang_frontend.cc:L411](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L411)）。

---

### 4.2 process_freestanding_sva_property：自由站立断言与时钟提取

#### 4.2.1 概念说明

并发断言可以写在两个地方：

- **过程块内**：例如 `always_ff @(posedge clk) begin assert property (...); end`——此时时钟已经在过程块的敏感列表里，断言的 `propertySpec` 不再带 `@(posedge clk)`。这种由 `StatementExecutor::handle(ConcurrentAssertionStatement)` 直接调 `process_sva_property`（见 4.5 节）。
- **模块级（自由站立）**：例如顶层直接写 `assert property (@(posedge clk) data != 8'hFF);`。slang 把这种「裸」断言包成一个 `isFromAssertion` 的 `ProceduralBlockSymbol`（`Always`）。sv-elab 在 `handle(ProceduralBlockSymbol)` 里识别这个标记，调 `process_freestanding_sva_property`。

`process_freestanding_sva_property` 多做的活就是**把 `@(posedge clk)` 这层时钟从属性规范里剥出来，变成一个 `ProcessTiming`**，然后新建 `ProceduralContext` 调核心函数，最后自己 `addProcess` + `copy_case_tree_into`。

#### 4.2.2 核心流程

```
function process_freestanding_sva_property(netlist, stmt, block):
    spec = stmt.propertySpec
    if spec is ClockingAssertionExpr:          # 带 @(event)
        clocking = spec.clocking
        if clocking not SignalEventControl:
            add_diag(UnsupportedSVAFeature); return
        signal_event = clocking
        timing = ProcessTiming(EdgeTriggered)
        switch signal_event.edge:
            None:     add_diag(SVAClockingRequiresEdge); return
            Both:     add_diag(BothEdgesUnsupported); return
            Pos/Neg:  timing.triggers += {signal, polarity, ast_node}
        if signal_event.iff: add_diag(IffUnsupported)   # TODO，暂不支持
        procedure = ProceduralContext(netlist, timing)
        process_sva_property(stmt, block, procedure, clocking_expr.expr)  # 剥掉时钟后的体
    else:                                       # 不带时钟：组合语义
        procedure = ProceduralContext(netlist, ProcessTiming::implicit)
        process_sva_property(stmt, block, procedure, spec)
    # 两条路径共同收尾
    rtlil_proc = canvas.addProcess(new_id())
    procedure.copy_case_tree_into(rtlil_proc.root_case)
```

#### 4.2.3 源码精读

自由站立入口与「剥时钟」：

> [src/sva.cc:L98-L116](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L98-L116) —— 取 `statement.propertySpec`；若带 `ClockingAssertionExpr`，要求其 clocking 是 `SignalEventControl`（否则 `UnsupportedSVAFeature`），并准备一个 `EdgeTriggered` 的 `ProcessTiming`。

边沿种类分派：

> [src/sva.cc:L117-L139](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L117-L139) —— `None` 发 `SVAClockingRequiresEdge`（自由站立断言必须有边沿）；`BothEdges` 发 `BothEdgesUnsupported`；`PosEdge`/`NegEdge` 才填一条 `Sensitivity` 进 `timing.triggers`，`edge_polarity` 由 `edge == PosEdge` 决定。注意 iff 在这里直接发 `IffUnsupported`（带 `// TODO`，即未实现）。

建过程块并灌入 case 树：

> [src/sva.cc:L141-L155](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L141-L155) —— 两条路径（带时钟 / 不带时钟）都新建 `ProceduralContext`，调 `process_sva_property`，然后 `canvas->addProcess` + `copy_case_tree_into`——这就是 4.1 练习 1 里「调用方负责建 Process」的那个调用方。

自由站立断言的「识别点」——slang 把裸断言包成 `isFromAssertion` 的 `Always` 过程：

> [src/slang_frontend.cc:L2062-L2085](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2062-L2085) —— `handle(ProceduralBlockSymbol)`：若 `symbol.isFromAssertion && procedureKind == Always`，剥一层可能的 `BlockStatement` 后，`ConcurrentAssertionStatement` 走 `process_freestanding_sva_property`，`ImmediateAssertionStatement` 走「建 implicit `ProceduralContext` + `StatementExecutor`」；否则（普通 always）才走 `interpret(symbol)`（即 u6 的时序分类器）。

#### 4.2.4 代码实践

**目标**：对比「带时钟」与「不带时钟」两种自由站立断言的网表差异。

**步骤**：

1. 阅读上面的 [src/sva.cc:L141-L155](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L141-L155)。两条分支的唯一区别是 `ProcessTiming`：带时钟用 `EdgeTriggered`（带一条 trigger），不带时钟用静态的 `ProcessTiming::implicit`（组合）。
2. 预测：对一个 `assert property (cond);`（不带 `@`），生成的 `$check` 的 `TRG_ENABLE` 会是什么？根据 [src/slang_frontend.cc:L413-L419](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L413-L419)（`Implicit` 分支设 `TRG_ENABLE=false`），应为 `TRG_ENABLE=0`。
3. [tests/unit/sva.sv:L19](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/sva.sv#L19) 里恰好有一条不带时钟、带 `disable iff` 的自由站立断言 `assert property (disable iff (!c) c);`，可作为验证样本（**待本地验证**其 `TRG_ENABLE`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `process_freestanding_sva_property` 拒绝 `EdgeKind::None`（电平敏感）的时钟？

**参考答案**：并发断言的语义是「在时钟**边沿**采样」，电平敏感没有「采样时刻」的概念，无法映射成触发器型的 `ProcessTiming`。源码据此发 `SVAClockingRequiresEdge`（[src/sva.cc:L118-L120](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L118-L120)）。

**练习 2**：`process_freestanding_sva_property` 与 `process_sva_property` 是「重载」还是「包装」关系？

**参考答案**：包装。前者多负责「剥时钟 + 建 ProcessTiming + 建 Process」三件事，核心翻译仍复用后者；故两者签名里前者不需要 `ProceduralContext &`，而后者需要。

---

### 4.3 EvalContext::sva：SVA 特殊求值规则

#### 4.3.1 概念说明

断言体里的表达式（如 `data != 8'hFF`）需要被求值成 RTLIL 信号。sv-elab 没有另写一套求值器，而是复用 u4-l1 的 `EvalContext::operator()`，只是用一个标志位 `in_sva_expression` 告诉它「现在是在 SVA 上下文里求值」。入口就是 `EvalContext::sva`。

为什么需要这个标志？因为 SVA 的求值语义与普通过程块**不完全相同**。最关键的差别在「如何读一个静态变量」：

- 普通过程块里读静态变量，要走 `substitute_rvalue`（取它在 case 树里「当前算出的值」）；
- SVA 里读静态变量，要读「采样到的原始线」（`convert_static`，即模块里那根静态线本身），因为断言关心的是「信号在采样点的物理值」，不是过程块里被改写过的中间值。

这个差别来自 IEEE Std 1800-2017 第 16.14 节对断言表达式求值的规定，源码头注释也直接引用了 §16.14.6。

#### 4.3.2 核心流程

`sva` 本身极简——保存-置位-调用-恢复：

```
function EvalContext::sva(expr):
    save = in_sva_expression
    in_sva_expression = true
    ret = (*this)(expr)            # 复用普通 operator()
    in_sva_expression = save
    return ret
```

`in_sva_expression` 在 `operator()` 内部被三处消费：

1. **`Value`（读变量）**：`if (procedural && (!in_sva_expression || variable.kind != Static))` 走 `substitute_rvalue`，否则（SVA 内的静态变量）走 `convert_static`。
2. **`Conversion`（const' 转换）**：`const'(...)` 内部把标志**临时清零**，因为按 §16.14.6，const cast 内部回到普通求值规则。
3. **`ElementSelect`（数组下标）**：对推断存储器的元素读，只有 `!in_sva_expression` 才发 `$memrd_v2`；SVA 内则走普通（展平）寻址。

#### 4.3.3 源码精读

`sva` 的实现与字段声明：

> [src/slang_frontend.cc:L1196-L1203](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1196-L1203) —— 经典的 save-set-restore 三步，保证嵌套调用与异常路径下标志都能还原。

> [src/slang_frontend.h:L165-L167](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L165-L167) 与 [src/slang_frontend.h:L190-L192](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L190-L192) —— 注释点明「Variable reading within SVA is special depending on whether the variable is static, automatic, or is part of an expression casted to `const`」，并引用 IEEE 1800-2017 §16.14.6。

消费点 1——读静态变量时的分叉：

> [src/slang_frontend.cc:L1313-L1322](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1313-L1322) —— 普通过程块读变量走 `procedural->substitute_rvalue`（取 case 树里的当前值）；但若 `in_sva_expression && variable.kind == Static`，则改走 `netlist.convert_static`（直接读模块里那根静态线）。这就是 SVA「读采样值」的落点。

消费点 2——`const'` cast 把标志清零：

> [src/slang_frontend.cc:L1460-L1466](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1460-L1466) —— `conv.isConstCast` 时，进 `operand` 求值前把 `in_sva_expression` 临时置 `false`，求值完恢复。注释引用 §16.14.6：const cast 内部遵循普通过程求值规则。[tests/unit/sva.sv:L14](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/sva.sv#L14) 的 `const'(i)` 正是为此而设的测试样本。

消费点 3——存储器元素读在 SVA 内不发读端口：

> [src/slang_frontend.cc:L1500-L1504](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1500-L1504) —— 条件 `is_inferred_memory(...) && !in_sva_expression`：只有**不在** SVA 上下文时，数组元素读才落成 `$memrd_v2`（衔接 u7-l1 存储器推断）；SVA 内则交给 `AddressingResolver` 按普通（展平）寻址处理。

#### 4.3.4 代码实践

**目标**：通过 `tests/unit/sva.sv` 的一个用例，看清 `in_sva_expression` 对静态变量读取的影响。

**步骤**：

1. 阅读 [tests/unit/sva.sv:L10-L17](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/sva.sv#L10-L17)。第二个 `always_comb` 块里先 `i = 0;` 再写断言 `bar1: assert property (i == 1 && const'(i) == 0 && j == 0);`。
2. 分析：`i` 是块内声明的局部变量。断言里直接读 `i` 时，由于 `in_sva_expression` 生效，按 §16.14 语义读到的是「采样值」；而 `const'(i)` 内部 `in_sva_expression` 被清零，回到普通求值读到 `i` 当前的过程值。两者用 `&&` 拼在一起，正好是边界用例。
3. （源码阅读型）回到 [src/slang_frontend.cc:L1313-L1322](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1313-L1322)，确认 `j`（automatic 局部变量）因为 `kind != Static`，无论是否在 SVA 内都走 `substitute_rvalue`——这就是为什么 `j == 0` 总读到过程值。

#### 4.3.5 小练习与答案

**练习 1**：若 `EvalContext::sva` 不设置 `in_sva_expression`，断言里读一个模块级 `wire` 会读到什么？

**参考答案**：会走到 `substitute_rvalue`（过程块当前值分支），而不是 `convert_static`（静态线）。对模块级 `wire`/`logic` 这类静态信号，这会得到「过程块里被改写的中间值」而非「采样点的物理值」，偏离 SVA 语义。

**练习 2**：为什么 `const'(...)` 要把标志清零，而普通类型转换不用？

**参考答案**：const cast 是 SVA 特意提供的「逃逸口」，按 §16.14.6 它的内部表达式遵循**普通**过程求值规则（允许读到过程当前值，例如用于采样后已在块内变化的变量）。普通转换没有这个语义特例，故沿用外层 SVA 上下文。

---

### 4.4 handle_past：用触发器链实现 $past

#### 4.4.1 概念说明

`$past(expr)` 是 SVA 里最常用的「时序」系统函数：返回 `expr` 在上一个时钟沿的值；`$past(expr, n)` 返回 `n` 拍前的值。sv-elab 把它实现成**一串触发器**——每个时钟沿把当前值往后挪一格，链长即延迟拍数。这与 u6-l2 的触发器发射完全复用（`add_dffe`）。

支持范围有限：只认前两个参数（表达式、拍数）；第 3 个 gating、第 4 个 clocking 不支持。

#### 4.4.2 核心流程

```
function handle_past(eval, call):
    if call.arguments().size() > 2:
        add_diag(PastGatingClockingUnsupported); return Sx
    if procedural == null or timing is Implicit
       or triggers.size() != 1 or not timing_matches_process:
        add_diag(SystemFunctionRequireClockedBlock, name); return Sx

    num_cycles = arg[1] if present else 1     # 编译期求值
    current_val = eval(call.arguments()[0])
    trigger = procedural.timing.triggers[0]   # 唯一时钟

    prev_val = current_val
    for i in 0 .. num_cycles-1:
        past_wire = add_placeholder_signal(width, "$past")
        add_dffe(clock=trigger.signal, en=background_enable,
                 d=prev_val, q=past_wire, polarity=trigger.edge_polarity, en_polarity=true)
        prev_val = past_wire
    return prev_val                            # 末级 DFF 的输出
```

`num_cycles` 级 DFF 串联：第 1 级输出是「1 拍前」，第 `n` 级输出是「n 拍前」。返回末级。

#### 4.4.3 源码精读

函数体与参数限制：

> [src/slang_frontend.cc:L816-L832](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L816-L832) —— 注释列出完整签名 `$past(expr, num_ticks, gating_expr, clocking_event)` 但声明「we only support first 2 args」；超过 2 参数发 `PastGatingClockingUnsupported`。随后要求「时钟块」：`procedural != nullptr`、`timing.kind != Implicit`、恰好 1 个 trigger、且 `timing_matches_process`（即该过程块的时序未被异步复位重解释），否则发 `SystemFunctionRequireClockedBlock` 并返回 `x`。

拍数求值与 DFF 链：

> [src/slang_frontend.cc:L835-L866](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L835-L866) —— 第 2 参数用 slang 的编译期求值（`call.arguments()[1]->eval(eval.const_)`）拿到 `num_cycles`（默认 1）；然后循环 `num_cycles` 次，每次 `add_placeholder_signal` 建一根命名带 `$past` 的线，调 `netlist.add_dffe(...)` 把 `prev_val` 打一拍进 `past_wire`，链式推进。时钟与极性取自唯一的 `triggers[0]`，使能取 `timing.background_enable`。

`$past` 在 `operator()` 的 Call 分派里被路由进来：

> [src/slang_frontend.cc:L1622-L1623](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1622-L1623) —— `name == "$past"` 分支直接调 `handle_past(*this, call)`，与 `$countones`/`$clog2`/`$signed` 等并列。注意它是「普通求值」路径，因此 `$past` 的实参 `expr` 会按调用处的上下文求值（断言里则会被外层 `sva` 包裹）。

#### 4.4.4 代码实践

**目标**：理解 `$past` 在网表里长什么样。

**步骤**：

1. 假设有一条 `assert property (@(posedge clk) data == $past(data));`。预测路径：`process_freestanding_sva_property` 建一个 `EdgeTriggered`(posedge clk) 的 `ProceduralContext`；`process_sva_property` 求值 `data == $past(data)` 时，`$past(data)` 经 `handle_past` 生成 1 个 `$dffe`（因为 `num_cycles=1`），其 `D=data`、`Q=$past 线`、时钟 `clk`；相等比较的输出进 `$check.A`。
2. 把它扩成 `$past(data, 3)`，预测会生成 **3 个**级联 `$dffe`。
3. 若把 `$past` 写在一个不带时钟的 `always_comb` 里，预测会触发 `SystemFunctionRequireClockedBlock`（[src/slang_frontend.cc:L828-L832](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L828-L832)）。**待本地验证**（仓库未直接提供此用例的 .ys）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `handle_past` 要求 `timing.triggers.size() == 1`？

**参考答案**：`$past` 的「上一拍」是相对**单一时钟**定义的；多触发沿（如双沿或异步复位拆出的多 trigger）下「上一拍」无定义。源码据此拒绝，并配合 `timing_matches_process` 排除被异步复位重解释过的情况。

**练习 2**：`$past(x)` 的返回值在第一个时钟沿之前是什么？

**参考答案**：sv-elab 用 `add_dffe` 发射普通 `$dffe`，其上电初值由下游（RTLIL 的默认 / `init` 属性）决定，未在 `handle_past` 内显式指定；仿真上等价于未定义/初值。这符合 SV 对「采样前 `$past`」返回未定义值的语义。

---

### 4.5 支持边界与 ignore_assertions

#### 4.5.1 概念说明

SVA 是一个非常大的特性集（序列算子 `[*]`/`[->]`、蕴含 `|->`/`|=>`、`within`/`throughout`、`if-else` 属性、递归属性等），绝大多数只在仿真 / 形式化时有意义、不可综合。sv-elab 的策略是：**只支持「单拍布尔检查」这一可综合子集**，其余发诊断拒绝。同时提供一个总开关 `--ignore-assertions`，让用户在不想处理任何断言时把它们全部丢弃（例如只想要数据通路网表）。

#### 4.5.2 核心流程

`--ignore-assertions` 在四个入口被检查，任一为真即跳过该断言的翻译：

| 位置 | 覆盖场景 |
|---|---|
| `statements.h:206` | 过程块内**立即断言** |
| `statements.h:242` | 过程块内**并发断言** |
| `slang_frontend.cc:2074` | **自由站立并发 / 立即断言**（slang 包成的 `isFromAssertion` Always） |
| `slang_frontend.cc:2929` | `PropertySymbol`（property/sequence 声明）——即使不忽略也会发 `SVAUnsupported` |

SVA 支持边界一览（诊断码见 diag.cc）：

| 触发条件 | 诊断码 | 严重级别 |
|---|---|---|
| 非 `SimpleAssertionExpr`、非 `SignalEventControl` 时钟 | `UnsupportedSVAFeature` (1075) | Error |
| 简单断言带重复（`[*n]`） | `RepetitionsUnsupported` (1076) | Error |
| 自由站立断言时钟为电平（`None`） | `SVAClockingRequiresEdge` (1077) | Error |
| 自由站立断言时钟为双沿 | `BothEdgesUnsupported` (1004) | Error |
| 自由站立断言带 `iff` | `IffUnsupported` (1000) | Error |
| 未知 assertionKind / 过程块内 `expect` | `AssertionUnsupported` (1025) | Error |
| 过程块内并发断言用 `expect` | `ExpectStatementUnsupported` (1045) | Warning |
| `property`/`sequence` 声明 | `SVAUnsupported` (1044) | Error |
| `$past` 给了第 3/4 参数 | `PastGatingClockingUnsupported` (1063) | Error |
| `$past` 不在单时钟块内 | `SystemFunctionRequireClockedBlock` (1064) | Error |

#### 4.5.3 源码精读

选项注册：

> [src/slang_frontend.cc:L100-L101](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L100-L101) —— `--ignore-assertions` 绑定到 `SynthesisSettings::ignore_assertions`，help 文案「Ignore assertions and formal statements in input」。字段类型是 `std::optional<bool>`（[src/slang_frontend.h:L500](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L500)），各处用 `value_or(false)` 读取（见 u2-l3 关于「保留用户未指定态」的设计）。

四个跳过点：

> [src/statements.h:L204-L207](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L204-L207) 与 [src/statements.h:L240-L249](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L240-L249) —— 立即断言与过程块内并发断言的入口，开头 `if (ignore_assertions) return`/跳过。注意过程块内并发断言额外拒绝 `expect`（发 `ExpectStatementUnsupported` 警告）。

> [src/slang_frontend.cc:L2074-L2085](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2074-L2085) —— 自由站立断言入口的 `ignore_assertions` 守卫。

> [src/slang_frontend.cc:L2928-L2932](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2928-L2932) —— `handle(PropertySymbol)`：即使用户没开 `--ignore-assertions`，遇到 `property ... endproperty` 这类声明也直接发 `SVAUnsupported`（提示「ignore all assertions with '--ignore-assertions'」）。

诊断码定义与文案：

> [src/diag.cc:L99-L100](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L99-L100)（`SVAUnsupported` 1044、`ExpectStatementUnsupported` 1045）、[src/diag.cc:L117-L118](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L117-L118)（`PastGatingClockingUnsupported` 1063、`SystemFunctionRequireClockedBlock` 1064）、[src/diag.cc:L128-L130](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L128-L130)（`UnsupportedSVAFeature` 1075、`RepetitionsUnsupported` 1076、`SVAClockingRequiresEdge` 1077）。文案与严重级别在 [src/diag.cc:L252-L256](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L252-L256)、[src/diag.cc:L299-L303](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L299-L303)、[src/diag.cc:L332-L339](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L332-L339) 集中登记（详见 u2-l4 的诊断系统）。`AssertionUnsupported` 与 `BothEdgesUnsupported`/`IffUnsupported` 见 [src/diag.cc:L82](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L82)、[src/diag.cc:L55-L59](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L55-L59)。

#### 4.5.4 代码实践

**目标**：亲手触发一条 SVA 诊断，验证支持边界表。

**步骤**：

1. 构造一个最小 SV 文件 `tmp_repeat.sv`（**示例代码**，非仓库原有）：
   ```systemverilog
   module tmp_repeat(input clk, input a);
       assert property (@(posedge clk) a [*2]);
   endmodule
   ```
2. 预测：`a [*2]` 是带重复的简单断言，会在 [src/sva.cc:L63-L66](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L63-L66) 命中 `RepetitionsUnsupported`。
3. （本地可选）`yosys -m slang.so -p "read_slang tmp_repeat.sv"`，预期 stderr 出现 `repetitions unsupported`。
4. 把断言换成 `assert property (@(clk) a);`（电平敏感），预期命中 `SVAClockingRequiresEdge`（[src/sva.cc:L118-L120](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L118-L120)）。
5. 加 `--ignore-assertions`，预期所有断言被静默丢弃、无 `$check` 生成。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`expect property (...)` 与 `assert property (...)` 在 sv-elab 里待遇有何不同？

**参考答案**：`assert/assume/cover` 都会落成 `$check`（flavor 不同）；但 `expect`（过程块内的「阻塞式」断言）在过程块内并发断言入口被专门拦下，发 `ExpectStatementUnsupported`（Warning，不综合，[src/statements.h:L243-L244](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L243-L244)），在 `assertionKind` switch 的 default 里则发 `AssertionUnsupported`。

**练习 2**：用户既没写 `property` 声明、也没用算子，为什么还会偶尔看到 `SVAUnsupported`？

**参考答案**：因为源码里写了 `property ... endproperty` 声明（或 sequence 声明），sv-elab 不支持把声明展开成可综合逻辑，于是在 `handle(PropertySymbol)` 直接发 `SVAUnsupported`（[src/slang_frontend.cc:L2928-L2932](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2928-L2932)），并提示用 `--ignore-assertions` 跳过。

---

## 5. 综合实践

**任务**：给 `tests/various/concurrent_assert.sv` 增加一个含 `$past` 的断言模块，并把整条「断言 → RTLIL」链路走通。

**操作步骤**：

1. 在 `concurrent_assert.sv` 末尾追加一个新模块（**示例代码**）：
   ```systemverilog
   module m_past_assert(input clk, input [7:0] data);
       assert property (@(posedge clk) data == $past(data));
   endmodule
   ```
2. **画调用链**（源码阅读，不需运行）：`handle(ProceduralBlockSymbol)` 识别 `isFromAssertion`（[src/slang_frontend.cc:L2062-L2085](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2062-L2085)）→ `process_freestanding_sva_property` 剥出 `posedge clk` 建 `EdgeTriggered` 时序（[src/sva.cc:L116-L142](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L116-L142)）→ `process_sva_property` 求值条件（[src/sva.cc:L85-L95](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L85-L95)），其中 `$past(data)` 经 `handle_past` 生成 1 个 `$dffe`（[src/slang_frontend.cc:L854-L864](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L854-L864)）→ 等值比较结果进 `$check.A`。
3. **预测网表**：`m_past_assert` 应有 1 个 `$check`（`FLAVOR=assert`、`TRG_ENABLE=1`、`TRG_POLARITY=1'1`）、1 个 `$dffe`（时钟 `clk`、位宽 8）、1 个比较单元（`$eq`）。
4. **（本地可选）验证**：参照 `concurrent_assert.ys` 的写法，新增几条 `select -assert-count` 断言，例如：
   ```
   select -assert-count 1 m_past_assert/t:$check r:FLAVOR=assert %i
   select -assert-count 1 m_past_assert/t:$dffe r:WIDTH=8 %i
   ```
   跑测试脚本验证计数。**待本地验证**（需要构建环境，构建方式见 u8-l3）。
5. **进阶**：把 `$past(data)` 改成 `$past(data, 3)`，预期 `$dffe` 数量从 1 变 3，验证你对 `handle_past` 循环次数的理解。

**需要观察的现象**：`$past` 每多一拍延迟就多一个级联 `$dffe`；`$check` 的触发参数与同模块的 posedge 并发断言一致；开 `--ignore-assertions` 后这些单元全部消失。

---

## 6. 本讲小结

- 并发断言在 sv-elab 里被翻译成一个挂在 `ProcessTiming` 上的 `$check` 副作用单元；`process_sva_property` 是核心翻译函数，只往当前 case 树追加单元，不建 `Process`。
- 自由站立（模块级）断言由 `process_freestanding_sva_property` 处理：它把 `@(posedge clk)` 剥成一个 `EdgeTriggered` 的 `ProcessTiming`，建 `ProceduralContext` 调核心函数，再自己 `addProcess` + `copy_case_tree_into`。
- `disable iff` 用一个 `SwitchHelper` 的「条件==0」分支实现，效果最终经 `case_enable()` 进入 `$check.EN`；`assert/assume/cover` 三种 flavor 落到 `FLAVOR` 参数。
- `EvalContext::sva` 通过 `in_sva_expression` 标志切换求值语义：SVA 内读静态变量走 `convert_static`（采样值），`const'(...)` 内部临时回到普通求值；存储器元素读在 SVA 内不发 `$memrd_v2`。
- `$past(expr[, n])` 用一串 `add_dffe` 实现「n 拍前」，要求单时钟块、最多 2 个参数。
- sv-elab 只支持「单拍布尔检查」子集；序列算子、重复、双沿、iff、`expect`、property 声明等会触发 `UnsupportedSVAFeature`/`RepetitionsUnsupported`/`SVAClockingRequiresEdge`/`BothEdgesUnsupported`/`IffUnsupported`/`ExpectStatementUnsupported`/`SVAUnsupported` 等诊断；`--ignore-assertions` 可一键丢弃所有断言。

## 7. 下一步学习建议

- 若想看 `$check` 单元下游如何被消费（形式化证明流程 / `formal` pass），可阅读 Yosys 上游的 `formal` 相关 pass 与 `CHERI`/`smt2` 后端；sv-elab 的职责到「发出 `$check`」为止。
- 想加深对「副作用单元触发」机制的理解，可回到 u5-l1 复习 `case_enable()` 与 `set_effects_trigger`，并对比 `$print`/`$check` 的异同。
- 想验证本讲预测，建议接着学 **u8-l1 测试体系**：了解 `tests/various/*.ys` 的 `select -assert-count` 范式与 CTest 集成，自己新增一个 SVA 等价性用例。
- 若你对「扩展 sv-elab 支持更多 SV 构造」感兴趣，可跳到 **u8-l4 扩展开发**：`process_sva_property` 里发 `UnsupportedSVAFeature` 的几处（[src/sva.cc:L55-L66](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L55-L66)、[src/sva.cc:L109-L112](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/sva.cc#L109-L112)）正是可下手新增 handle 的扩展点。
