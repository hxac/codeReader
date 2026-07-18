# StatementExecutor：遍历过程块语句

## 1. 本讲目标

本讲承接 u5-l1（`ProceduralContext` 与 `VariableState`），回答一个具体问题：**slang 给我们的过程块是一棵「语句 AST」，sv-elab 怎么把这棵 AST 长成一棵 `Case`/`Switch` 意图树、并同步维护变量状态？**

学完后你应该能够：

- 说清 `StatementExecutor` 是什么、由谁构造、按什么规则把 slang 语句分派到各个 `handle(...)`。
- 对 `if`/`case`/`for`/`while`/`foreach`、`break`/`continue`/`return`、赋值与断言等常见构造，分别指出对应的 `handle` 方法与它对 case 树做了什么。
- 用 `SwitchHelper` 的 `enter_branch` / `exit_branch` / `branch` / `finish` 四个动作，解释「分支进入 → 保存变量状态 → 分支体内赋值 → 还原变量状态 → 合并出汇合线」的完整生命周期。
- 理解死分支检测、循环展开栈 `sw_stack`、分支末尾「空 Switch 下钻」等优化与排序技巧。

本讲只覆盖 **语句级** 的翻译；左值位掩码的细节在 u5-l3，逃逸构造（`break`/`continue`/`return` 的标志变量建模）的全貌在 u5-l4，时序块（`always_ff`）的分类在 u6。

## 2. 前置知识

在进入源码前，请确认你已经掌握 u5-l1 的几个关键点（本讲直接复用，不再重述）：

- **过程块 = 一棵 case 树 + 一份变量状态**。每翻译一个 `always`/`initial`，sv-elab 就 new 一个 `ProceduralContext`，它持有 `root_case`/`current_case`（HDL 意图 case 树的根与当前游标）和 `vstate`（`VariableState`，一张「可回滚的按位赋值账本」）。
- **`VariableState` 的四件套**：`set(lhs, value)` 写入当前值、`evaluate(vbits)` 读出当前值、`save(map)` 把改动日志 `revert` 换出、`restore(map)` 按日志回滚并返回「本作用域内被改过的位及其新值」。底层用 `visible_assignments` 记当前值、`revert` 记改动历史，`RTLIL::Sm` 哨兵表示「原本不存在」。
- **`Case::Action` 是 HDL 意图，`Case::aux_actions` 是已物化连线**。赋值会被压成 `Action{lvalue, mask, unmasked_rvalue}` 存进 `current_case->actions`，而真正会被降级（lower）进 RTLIL 的是 `aux_actions`（见 u3-l4）。

还需了解两个来自 slang 的外部概念：

- **ASTVisitor**：slang 提供的访问者框架。`StatementExecutor` 继承 `ast::ASTVisitor<StatementExecutor, ast::VisitFlags::Statements>`，对每一种语句类型 `T` 实现一个 `void handle(const ast::T &)`；slang 在 `stmt.visit(visitor)` 时按 AST 节点的真实 `kind` 自动调用匹配的 `handle`。
- **bitstream（位流）**：sv-elab 全程统一的位宽度量单位，一个变量的「位流」就是它从最低位起排开的若干 bit。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/statements.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h) | 本讲主文件。定义 `SwitchHelper` 与 `StatementExecutor`，包含所有语句的 `handle`。 |
| [src/cases.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h) | `Case`/`Switch` 意图树结构，`add_switch`/`add_case`/`copy_into`。 |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | `ProceduralContext`、内嵌 `VariableState`、`UnrollLimitTracking`、逃逸帧 `EscapeFrame`。 |
| [src/procedural.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc) | `do_simple_assign`/`update_variable_state`/`signal_escape` 等被语句翻译调用的方法。 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | `VariableState::set/evaluate/save/restore` 的实现，以及把过程块喂给 `StatementExecutor` 的入口 `handle_comb_like_process`。 |

## 4. 核心概念与源码讲解

### 4.1 StatementExecutor：slang 语句的访问者总机

#### 4.1.1 概念说明

`StatementExecutor` 是一个「函数对象式」的 AST 访问者：它本身不带什么状态，只是把 `ProceduralContext`（工作台）、`EvalContext`（表达式求值器）、`UnrollLimitTracking`（展开限额）拿引用握在手里，然后对每一种 slang 语句节点实现一个 `handle`。

要理解它的定位，抓住一句话：**`StatementExecutor` 几乎不直接发出 RTLIL 单元来表达控制流**。遇到 `if`/`case`/循环时，它的活儿是「在 `current_case` 下面长出新的 `Switch`/`Case` 节点，并同步更新 `vstate`」。真正把控制流物化成 `$mux` 这类单元，是后续 `Switch::lower` + `proc_*` 的事（见 u3-l4、u8-l1）。赋值语句是个例外——它会通过 `assign_rvalue` 直接落到 `Case::Action` 与 `vstate.set`。

每个过程块翻译时都「现 new 一个」`StatementExecutor`，把 body 语句交给它访问。入口在 [src/slang_frontend.cc:1784-1785](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1784-L1785)（组合型 `always`）与 [src/slang_frontend.cc:2057-2058](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2057-L2058)（`initial`），形式都是：

```cpp
ProceduralContext procedure(netlist, ProcessTiming::implicit);
body.visit(StatementExecutor(procedure));
```

#### 4.1.2 核心流程

`StatementExecutor` 的工作就是「按 AST 节点 kind 分派」。下表把所有 `handle` 归类（行号均对应 src/statements.h）：

| 语句构造 | handle 方法 | 行号 | 对 case 树 / vstate 的作用 |
|---|---|---|---|
| `a = x;` / `a <= x;` | `handle(ExpressionStatement)` | [L320](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L320) | `eval` 表达式 → 分派到 `AssignmentExpression` → `procedural->assign_rvalue` |
| `begin ... end` | `handle(BlockStatement)` | [L322-L336](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L322-L336) | 要求 Sequential 块，进入 automatic 作用域，访问 body |
| 语句列表 | `handle(StatementList)` | [L338-L369](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L338-L369) | 逐条访问；配合逃逸标志做「提前退出」 |
| `if` / `if-else` | `handle(ConditionalStatement)` | [L371-L408](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L371-L408) | 常量短路；否则 `SwitchHelper` 建分支 |
| `case/casez/casex/inside` | `handle(CaseStatement)` | [L410-L478](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L410-L478) | `SwitchHelper` + 通配符 / `full_case` / `parallel_case` |
| `while` | `handle(WhileLoopStatement)` | [L480-L522](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L480-L522) | 循环展开，动态条件则建分支 |
| `for` | `handle(ForLoopStatement)` | [L524-L584](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L524-L584) | 循环展开 |
| `foreach` | `handle(ForeachLoopStatement)` | [L586-L682](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L586-L682) | 多维迭代展开 |
| `break` / `continue` | `handle(Break/ContinueStatement)` | [L717-L725](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L717-L725) | `signal_escape` 置逃逸标志 |
| `return` | `handle(ReturnStatement)` | [L727-L740](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L727-L740) | 写返回值 + `signal_escape` |
| 声明带初值的变量 | `handle(VariableSymbol)` | [L709-L714](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L709-L714) | `init_nonstatic_variable` |
| `#delay` / `@event` | `handle(TimedStatement)` | [L742-L748](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L742-L748) | 报「不可综合」诊断后继续访问内部语句 |
| `wait(...)` | `handle(WaitStatement)` | [L750-L753](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L750-L753) | 报不支持诊断 |
| 立即断言 `assert` | `handle(ImmediateAssertionStatement)` | [L204-L238](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L204-L238) | 发 `$check` 单元 |
| 并发断言 | `handle(ConcurrentAssertionStatement)` | [L240-L249](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L240-L249) | 委托 `process_sva_property`（见 u7-l4） |
| 空语句 `;` | `handle(EmptyStatement)` | [L684](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L684) | 什么也不做 |
| 兜底 | `handle(Statement)` / `handle(Expression)` | [L755-L760](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L755-L760) | 报 `LangFeatureUnsupported` / `unimplemented` |

赋值这条路径值得单独记住，因为绝大多数过程块代码最终都落在这里：

```
handle(ExpressionStatement)          // statements.h:320
  └─ eval(stmt.expr)                 // 调 EvalContext::operator()
       └─ case AssignmentExpression  // slang_frontend.cc:1243-1253
            └─ procedural->assign_rvalue(assign, rhs)   // procedural.cc:439
                 └─ update_variable_state(...)          // procedural.cc:202
                      ├─ current_case->actions.push_back(Action{...})  // :293
                      └─ vstate.set(lvalue, rvalue)                    // :299
```

即一条赋值同时做两件事：往 `current_case` 压一个 HDL 意图 `Action`，并把 `vstate` 的当前值更新为右值。这正是 blocking 语义在 case 树里的体现——同一条路径上后面的语句读到的就是新值。

#### 4.1.3 源码精读

先看类骨架与成员。[src/statements.h:190-202](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L190-L202) 声明 `StatementExecutor` 继承 ASTVisitor，四个引用成员 `netlist`/`context`/`eval`/`unroll_limit` 全部来自传入的 `ProceduralContext`：

```cpp
struct StatementExecutor : public ast::ASTVisitor<StatementExecutor, ast::VisitFlags::Statements>
{
    NetlistContext &netlist;
    ProceduralContext &context;
    EvalContext &eval;
    UnrollLimitTracking &unroll_limit;
    const ast::StatementBlockSymbol *containing_block = nullptr;

    StatementExecutor(ProceduralContext &context)
        : netlist(context.netlist), context(context), eval(context.eval),
          unroll_limit(context.unroll_limit) {}
```

注意 `containing_block` 这个成员：它在进入 `BlockStatement` 时被保存/恢复（[L329-L332](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L329-L332)），目的是让立即断言能借用所在命名块的标签做单元名（[L219-L223](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L219-L223)）。

兜底 `handle` 体现了 sv-elab 的「防御式编程」风格——遇到没专门处理的语句就走诊断或硬停，绝不悄悄生成错误网表：

```cpp
void handle(const ast::Statement &stmt) {
    netlist.add_diag(diag::LangFeatureUnsupported, stmt.sourceRange.start());
}
void handle(const ast::Expression &expr) { unimplemented(expr); }
```

（[src/statements.h:755-760](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L755-L760)）`unimplemented` 是 u8-l4 会讲到的硬停宏；`LangFeatureUnsupported` 则是「先记一笔诊断、继续翻译」的软失败。两者的区别决定了某种构造是「能用但部分受限」还是「直接挡掉」。

`StatementList` 的处理里藏着一个与逃逸构造协作的精巧设计（[L338-L369](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L338-L369)）：每访问下一条语句前，先读 `context.get_disable_flag()`（当前最内层逃逸构造的标志变量）。若它是一个非常量信号，说明运行时可能已经 `break`/`continue`/`return` 了，于是用一个 `SwitchHelper(signal, ...)` 把「剩余语句」包进 `signal==0` 分支，让它们只在「未逃逸」时执行；若是常量真则直接 `break`（剩余全是死代码）。这个机制是 u5-l4 的伏笔，这里只需知道：**顺序语句列表会自动被逃逸标志切成条件片段**。

#### 4.1.4 代码实践

**实践目标**：在源码里「对号入座」，确认 slang 语句到 `handle` 的映射，并跑一个最小例子看 `case` 语句最终被 `proc` 物化成什么。

**操作步骤**：

1. 打开 [src/statements.h:410](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L410)，阅读 `handle(CaseStatement)`，找到它如何用 `SwitchHelper b(context, ...)` 建分支、如何处理 `Inside`/`WildcardJustZ`/`WildcardXOrZ`。
2. 准备一段最小 SV（参考 [tests/unit/case.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/case.sv) 的写法），用一个 `casez`：

   ```bash
   yosys -p "read_slang <<EOF
   module top(input logic [3:0] w, output logic [1:0] m);
       always_comb
       casez (w)
           4'b?00?: m = 2'd0;
           4'b??10: m = 2'd1;
           default: m = 2'd3;
       endcase
   endmodule
   EOF
   proc; show"
   ```

   （`read_slang <<EOF ... EOF` 是本项目测试里反复使用的 heredoc 调用形式，见 [tests/unit/dff.ys:1](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L1)。）

**需要观察的现象**：`proc` 之后，`casez` 的多分支汇合应被物化成若干 `$mux`/`$bmux`（或等价的多路选择逻辑），输出 `m` 由条件选择。

**预期结果**：`show` 输出里能看到选择单元而非 RTLIL `process`/`switch` 节点——这印证了「`StatementExecutor` 只长 case 意图树，物化交给 `proc`」的分工。

**待本地验证**：具体单元类型与个数取决于本地 Yosys 版本的 `proc` 优化策略，请以实际 `show` 输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `StatementExecutor` 遇到 `if` 时不直接发一个 `$mux` 单元，而是先建 `Switch`/`Case` 节点？

**参考答案**：因为分支里可能含 blocking 赋值、嵌套控制流、部分位赋值，且还要供后续锁存器分析（u6-l3）和 `proc` 统一优化消费。先保留 HDL 意图（左值用 `VariableBits`、保留 slang 源码信息），等整棵树成型再统一降级，比边走边发单元更易维护、更易优化。

**练习 2**：`handle(TimedStatement)`（[#L742](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L742)）报了诊断后为什么还要 `stmt.stmt.visit(*this)`？

**参考答案**：它把 `#delay`/`@event` 这类不可综合的时序控制「剥离并警告」，但仍继续翻译被它包裹的内部语句，尽量给出可用网表而不是整块丢弃。

---

### 4.2 SwitchHelper：分支的进入、退出与合并

#### 4.2.1 概念说明

`SwitchHelper` 是一个**栈上的辅助对象**（RAII），专门用来在当前 case 树位置上「开一个 `Switch`、挂几个分支 `Case`、最后合并」。它把 `if`/`case`/循环这些「分叉—汇合」结构公共的逻辑抽出来：你只要告诉它「分支条件」和「分支体」，它负责变量状态的保存/还原以及汇合线的生成。

可以把它的角色理解成：**一次 `SwitchHelper` 的生命周期 = 在 case 树上长出一个 `Switch` 节点的完整过程**。`if`、`case`、`while`、`for`、`foreach` 内部都用它，区别只在「挂哪些 compare 值」和「循环展开时积攒多少层」。

#### 4.2.2 核心流程

一个 `SwitchHelper` 的典型用法（以 `if-else` 为例）遵循下面这个时序：

```
构造 SwitchHelper b(context, signal)
   └─ sw = parent->add_switch(signal)          // 在当前 case 下挂一个 Switch

b.branch({S1}, [&]{ ifTrue 体 })                // 「条件为真」分支
   ├─ enter_branch({S1})
   │    ├─ vstate.save(save_map)               // 把改动日志换出（快照）
   │    └─ current_case = sw->add_case({S1})   // 新建分支 Case，游标下移
   ├─ f()                                       // 执行分支体（其中赋值会改 vstate）
   └─ exit_branch()
        ├─ current_case = parent               // 游标回到分叉前
        └─ updates = vstate.restore(save_map)  // 回滚 vstate，并取回本分支算出的新值
              └─ branch_updates.push_back((this_case, 改过的位, 新值))

b.branch({}, [&]{ ifFalse 体 })                 // else 分支，compare 为空 = 默认分支
   └─ （同上）

b.finish(netlist)                               // 合并：为「任一分支动过的位」建汇合线
```

关键不变量：**`enter_branch` 时保存的 vstate 快照，在 `exit_branch` 时被完整回滚**。所以分支体内对变量的修改不会泄漏到兄弟分支或分支之后——这正是「不同分支互不干扰」的实现方式。每个分支算出的「新值」则被收进 `branch_updates`，留给 `finish` 合并。

#### 4.2.3 源码精读

`SwitchHelper` 的成员与构造见 [src/statements.h:34-51](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L34-L51)：

```cpp
struct SwitchHelper {
    Case *parent;
    Case *&current_case;          // 引用 ProceduralContext::current_case，移动游标
    Switch *sw;
    using VariableState = ProceduralContext::VariableState;
    VariableState &vstate;
    VariableState::Map save_map;
    std::vector<std::tuple<Case *, VariableBits, RTLIL::SigSpec>> branch_updates;
    bool entered = false, finished = false;

    SwitchHelper(ProceduralContext &context, RTLIL::SigSpec signal)
        : parent(context.current_case), current_case(context.current_case), vstate(context.vstate)
    {
        sw = parent->add_switch(signal);
    }
```

注意 `current_case` 是**引用**，直接绑到 `ProceduralContext::current_case`。所以 `SwitchHelper` 移动游标就是移动整个翻译器的「当前插入点」；这也是为什么分支体里递归 `visit` 出来的 `if`/赋值会自动落到正确的子 `Case` 里。

`enter_branch` 做两件事——快照 + 建分支（[src/statements.h:71-79](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L71-L79)）：

```cpp
void enter_branch(std::vector<RTLIL::SigSpec> compare) {
    save_map.clear();
    vstate.save(save_map);
    log_assert(!entered);
    log_assert(current_case == parent);
    current_case = sw->add_case(compare);
    entered = true;
}
```

`vstate.save(save_map)` 的实现极其简洁——只是把改动日志 `revert` 整表 `swap` 出来（[src/slang_frontend.cc:559-562](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L559-L562)）：

```cpp
void VariableState::save(Map &save) {
    revert.swap(save);
}
```

也就是说，「保存」=「把空白的 save_map 换进 `revert`，分支体里的新改动从此记进这张新表」。这是零拷贝快照。

`exit_branch` 是逆操作——游标回退 + 回滚 + 取回本分支新值（[src/statements.h:81-90](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L81-L90)）：

```cpp
void exit_branch() {
    log_assert(entered);
    log_assert(current_case != parent);
    Case *this_case = current_case;
    current_case = parent;
    entered = false;
    auto updates = vstate.restore(save_map);
    branch_updates.push_back(std::make_tuple(this_case, updates.first, updates.second));
}
```

`restore`（[src/slang_frontend.cc:564-586](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L564-L586)）做三件事：先把 `revert` 里记过的位收集成 `lreverted`、读出它们在 `visible_assignments` 里的当前（分支内）值 `rreverted`；再按 `revert` 把 `visible_assignments` 逐位还原（`Sm` 哨兵表示擦除、否则恢复旧值）；最后把 `save_map` 换回 `revert`。返回的 `{lreverted, rreverted}` 就是「本分支动过哪些位 / 它们算成了什么」，正好喂给 `branch_updates`。

#### 4.2.4 代码实践

**实践目标**：用纸笔（或注释）还原一次 `enter_branch → 分支体 → exit_branch` 期间 `vstate` 内部的变化。

**操作步骤**：

1. 假设进入分支前 `visible_assignments = {}`、`revert = {}`。
2. 模拟 `enter_branch`：调用 `save(save_map)` 后，`revert` 与 `save_map` 各是什么？  
   **答**：`revert = {}`（新空表），`save_map` 被换走（原本也空）。
3. 模拟分支体里执行 `a = x`（`a` 是某变量的 1 位）：根据 [src/slang_frontend.cc:511-527](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L511-L527) 的 `VariableState::set`，写出 `revert` 与 `visible_assignments` 的变化。  
   **答**：因为 `revert` 此前不含 `a` 且 `visible_assignments` 也不含 `a`，所以 `revert[a] = RTLIL::Sm`（记下「原本不存在」），然后 `visible_assignments[a] = x`。
4. 模拟 `exit_branch`：`restore` 会怎么还原？返回值是什么？  
   **答**：`lreverted = {a}`，`rreverted = {x}`；由于 `revert[a] == Sm`，`visible_assignments.erase(a)`，即彻底擦除这次修改；`branch_updates` 多一项 `(this_case, {a}, {x})`。

**预期结果**：分支结束后 `visible_assignments` 回到 `{}`，但 `branch_updates` 记下了「这个分支把 `a` 算成了 `x`」，供 `finish` 合并。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `save`/`restore` 用 `swap` 而不是逐项复制？

**参考答案**：O(1) 的整表交换实现了零拷贝快照；分支体里无论改多少位，都只动一张增量日志表 `revert`，保存与还原本身不随位数增长。

**练习 2**：`entered`/`finished` 两个布尔标志配合析构函数里的 `log_assert`（[src/statements.h:53-57](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L53-L57)）在防什么？

**参考答案**：防止「进了分支没出」（`entered` 必须在析构时为假）和「攒了 branch_updates 却没调 `finish`」。这是用断言把「分支必须配对、必须收尾」的不变量写死。

---

### 4.3 branch/finish：分支生命周期与变量状态保存

#### 4.3.1 概念说明

本模块把 `SwitchHelper` 的两个最关键方法 `branch` 与 `finish` 单独放大。`branch` 是「死分支检测 + 进入/执行/退出」的语法糖；`finish` 是整个分支机制的收尾——它把「任一分支动过的位」收齐，为它们各自建一根**汇合线（merge wire）**，并在每个分支 `Case` 里写一条「条件性地把本分支的值驱动到汇合线」的 `aux_action`。`finish` 之后，`vstate` 里这些位的当前值就变成了那根汇合线，于是「`if` 之后的语句」读到的正是按条件选择后的结果。

`branch` 与 `finish` 之所以重要，是因为它们共同回答了：**分支体里用 `VariableBits`（HDL 意图）记录的赋值，到底怎么变成真实的、带条件选择的网表信号？** 桥梁就是 `finish` 创建的汇合线。

#### 4.3.2 核心流程

`branch(compare, f)` 把「三段式」打包，并在最前面做一次死分支剪除：

```
branch(compare, f):
    if (compare 单值且全定值 且 sw->signal 全定值 且 signal != compare[0]):
        return                         // 死分支，直接丢弃，不进树
    enter_branch(compare)
    f()                                // 用户给的分支体
    exit_branch()
```

`finish(netlist)` 的合并逻辑可分为两趟：

1. **求并集 + 建汇合线**：把所有 `branch_updates` 里动过的位并起来（`updated_anybranch`）。对其中每一个「非作用域末尾」的位块 `chunk`：
   - 读背景值 `w_default = vstate.evaluate(chunk)`（通常是该静态变量的原始线）；
   - 建一根占位汇合线 `w`（名字取自变量名 + 切片文本）；
   - 在 `parent` 上写「汇合线默认接背景值」：`parent->aux_actions += {w, w_default}`；
   - 把 `vstate` 里该位的当前值改成汇合线：`vstate.set(chunk, w)`。
2. **逐分支写条件驱动**：对每个分支 `(rule, target, source)`，把 `target` 的每一位映射到它现在的汇合线（`vstate.visible_assignments`），再写 `rule->aux_actions += {汇合线, source 片段}`。于是「条件为真」的 `Case` 会用本分支算出的值覆盖默认值。

「作用域末尾变量」（end-of-scope variables，`eos_variables`）是个边界情况：若某位只出现在部分分支、且它根本不在 `visible_assignments` 里（典型是 `if` 内声明的局部变量，出了 `if` 就不可见），则不为它建汇合线，直接跳过。

#### 4.3.3 源码精读

`branch` 方法本体很短，重点是开头的死分支检测（[src/statements.h:92-104](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L92-L104)）：

```cpp
void branch(std::vector<RTLIL::SigSpec> compare, std::function<void()> f)
{
    // TODO: extend detection
    if (compare.size() == 1 && compare[0].is_fully_def() && sw->signal.is_fully_def() &&
            sw->signal != compare[0]) {
        // dead branch
        return;
    }
    enter_branch(compare);
    f();
    exit_branch();
}
```

这是 sv-elab 里为数不多的「小优化」：当开关信号和比较值都是编译期常量、且明显不相等时，这个分支永远不会命中，于是连 `Case` 节点都不建。注释 `TODO: extend detection` 说明作者知道这还很保守（只覆盖最简单情形）。注意 `handle(ConditionalStatement)` 自己还有一套更强的常量短路（见下一节），二者互补。

`handle(ConditionalStatement)` 是 `branch` 最直观的用例（[src/statements.h:371-408](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L371-L408)），核心片段：

```cpp
RTLIL::SigSpec condition = netlist.ReduceBool(eval(*cond.conditions[0].expr));

if (condition.is_fully_def()) {
    if (condition[0] == RTLIL::S1) { cond.ifTrue.visit(*this); return; }
    else if (condition[0] == RTLIL::S0) { if (cond.ifFalse) cond.ifFalse->visit(*this); return; }
    // fall through on Sx
}

SwitchHelper b(context, condition);
b.sw->statement = &cond;
b.branch({RTLIL::S1}, [&]() { /* 访问 ifTrue */ });
if (cond.ifFalse)
    b.branch({}, [&]() { /* 访问 ifFalse，compare 空 = 默认分支 */ });
b.finish(netlist);

// descend into an empty switch so we force action priority for follow-up statements
context.current_case = context.current_case->add_switch({})->add_case({});
```

两点要读懂：

1. **常量短路**：条件是确定常量时，直接只翻译命中的一支、`return`，根本不建 `Switch`。`Sx`（未知）时落入正常建分支路径——这避免了「条件为 x 时整段消失」的错误。
2. **末尾「空 Switch 下钻」**（[L407](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L407)）：建一个 `signal={}` 的平凡 `Switch` 并把游标下移到它唯一的空 `Case`。原因是 RTLIL `CaseRule` 的语义是「先执行自身 actions，再顺序执行子 switches」——把 `if` 之后的语句塞进更深的层级，能强制它们排在 `if` 的汇合之后执行，从而保证「动作优先级」正确。`handle(CaseStatement)`、各循环 handler 末尾都有同样一行（[L477](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L477)、[L521](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L521)、[L583](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L583)、[L681](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L681)）。

`finish` 方法是分支机制的「汇合点」（[src/statements.h:106-170](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L106-L170)）。先求并集、筛掉作用域末尾变量（[L108-L127](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L108-L127)）：

```cpp
VariableBits updated_anybranch;
for (auto &branch : branch_updates)
    updated_anybranch.append(std::get<1>(branch));
updated_anybranch.sort_and_unify();

Yosys::pool<Variable> eos_variables;
auto &va = vstate.visible_assignments;
for (auto bit : updated_anybranch)
    if (bit.variable.kind != Variable::Static && !va.count(bit))
        eos_variables.insert(bit.variable);
```

然后为每个需要合并的块建汇合线、改写 `vstate`（[L129-L141](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L129-L141)）：

```cpp
RTLIL::SigSpec w_default = vstate.evaluate(netlist, chunk);
RTLIL::SigSpec w = netlist.add_placeholder_signal(chunk.bitwidth(), name_suggestion);
parent->aux_actions.push_back(RTLIL::SigSig(w, w_default));
vstate.set(chunk, w);
```

最后逐分支把「本分支的值」条件性地驱动到汇合线（[L143-L167](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L143-L167)），核心是把 `target` 的各位经 `va`（现已指向汇合线）取回，再写 `rule->aux_actions += {target_w, source}`。降级时（[src/cases.h:84-L113](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h#L84-L113) 的 `copy_into`）只有这些 `aux_actions` 会被搬进真正的 `RTLIL::CaseRule`，而 `Case::Action`（HDL 意图）会被锁存器分析等后续阶段消费——这与 u3-l4 的结论一致。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：追踪一个 `if-else` 经过 `SwitchHelper` 后生成的 `Case`/`Switch` 结构与 `VariableState` 变化。这是本讲规格指定的实践任务。

**操作步骤**：

1. 考虑最小模块：

   ```systemverilog
   module top(input logic sel, input logic [3:0] x, y, output logic [3:0] a);
       always_comb begin
           if (sel) a = x;
           else     a = y;
       end
   endmodule
   ```

2. 在源码上逐步标注。进入 `always_comb` 时，`handle_comb_like_process` 先建 `procedure`，其构造函数把 case 树初始化为 `root_case → 平凡 Switch(signal={}) → 空 Case`（[src/procedural.cc:116-L118](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L116-L118)），`current_case` 指向那个空 `Case`。
3. body 是 `begin...end` → `handle(BlockStatement)` → 访问到 `ConditionalStatement` → `handle(ConditionalStatement)`（[L371](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L371)）。`sel` 非常量，于是走 `SwitchHelper b(context, sel)`：在 `current_case` 下挂 `Switch(signal=sel)`。
4. `b.branch({S1}, …)`：
   - `enter_branch` 保存 vstate，建 `Case(compare={S1})`，游标下移；
   - 分支体 `a = x`：经 `ExpressionStatement → eval → assign_rvalue → update_variable_state`，往这个 `Case` 压 `Action{a, 全1掩码, x}`，并 `vstate.set(a, x)`；
   - `exit_branch` 回滚 vstate，记 `branch_updates += (if_case, {a}, {x})`。
5. `b.branch({}, …)`（else，compare 空 = 默认分支）：同理得到 `Case(compare={})`、`Action{a, 全1掩码, y}`、`branch_updates += (else_case, {a}, {y})`。
6. `b.finish`：
   - `updated_anybranch = {a 的 4 位}`；
   - `w_default = vstate.evaluate(a) = a 的原始静态线`（因为 `a` 此前没被赋值过）；
   - 建汇合线 `w`（命名提示来自 `a`），`parent->aux_actions += {w, a_原始线}`，`vstate.set(a, w)`；
   - if_case：`aux_actions += {w, x}`；else_case：`aux_actions += {w, y}`。
7. 末尾空 Switch 下钻，游标进入更深层空 `Case`（`if` 之后没有语句了，此处无影响）。

**需要观察的现象（生成的意图树）**：

```
root_case
└─ Switch(signal={})              // 构造时建的平凡外壳
   └─ Case(compare={})            // current_case 起点
      └─ Switch(signal=sel)       // handle(ConditionalStatement) 建的 if
         ├─ Case(compare={S1}):   actions=[Action{a,1,x}]   aux_actions=[{w, x}]
         └─ Case(compare={}):     actions=[Action{a,1,y}]   aux_actions=[{w, y}]
      └─ Switch(signal={})        // 末尾空 Switch（强制优先级）
         └─ Case(compare={})
```

`vstate` 在 `finish` 后：`a → 汇合线 w`；汇合线 `w` 默认接 `a` 的原始线，并被两个分支条件性覆盖为 `x` / `y`。

**预期结果**：`w` 的语义就是 `sel ? x : y`。运行 `proc` 后，这棵树会物化成一个 4 位 `$mux`（`Y = S ? B : A`，见 u3-l2）。可用下面命令本地确认（**待本地验证**具体单元形式）：

```bash
yosys -p "read_slang <<EOF
module top(input logic sel, input logic [3:0] x, y, output logic [3:0] a);
    always_comb begin
        if (sel) a = x; else a = y;
    end
endmodule
EOF
proc; show"
```

**预期结果**：`show` 中应能看到一个 `$mux` 单元，其 `S=sel`、`A=y`、`B=x`、`Y=a`（或等价的选择逻辑），印证汇合线合并的语义。

#### 4.3.5 小练习与答案

**练习 1**：把上面例子里的 `else a = y;` 删掉（变成不完整 `if`），`finish` 还会为 `a` 建汇合线吗？`a` 会变成什么？

**参考答案**：仍会建汇合线 `w`，但只有一个分支 `(if_case, {a}, {x})`。`w` 默认接 `a` 的原始线，仅在 `sel` 时被覆盖为 `x`——这正是锁存器的雏形。后续 `detect_possibly_unassigned_subset`（u6-l3）会发现 `a` 在 `sel==0` 时悬空，从而插入 `$dlatch`/staging 信号。

**练习 2**：`branch` 里的死分支检测为什么要求 `sw->signal.is_fully_def()`？如果不要求会怎样？

**参考答案**：因为比较的前提是「信号确定取某个常量值」。若信号本身含 `x`/`z`，它在仿真上可能命中也可能不命中，不能判定为死分支。放宽这一条件会误删在 `x` 态下本应保留的分支，导致网表语义与仿真不一致。

**练习 3**：循环（`for`/`while`）里用了一个 `std::vector<SwitchHelper> sw_stack`（[L536](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L536)、[L483](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L483)），为什么需要「栈」而不是单个 `SwitchHelper`？

**参考答案**：循环是「编译期展开」的，每次迭代若循环条件仍非常量，就新开一层 `SwitchHelper` 包住这一轮的 body。展开 N 轮就可能堆叠 N 层 `Switch`，所以需要 `sw_stack` 积攒；退出循环时逆序 `exit_branch + finish` 把它们逐层合并。展开限额由 `unroll_limit.unroll_tick` 控制（见 u5-l4）。

## 5. 综合实践

把本讲三块知识（语句分派、`SwitchHelper` 分支生命周期、`branch`/`finish` 合并）串起来，做一个「读 + 跑 + 改」的小任务。

**任务**：解释下面这段含 `if-else` 与 `case` 的组合逻辑，如何被 `StatementExecutor` 长成 case 树、再被 `proc` 物化。

```systemverilog
module top(input logic [1:0] op,
           input logic [3:0] a, b,
           output logic [3:0] y);
    always_comb begin
        if (a == b)
            y = 4'd0;
        else begin
            case (op)
                2'd0: y = a + b;
                2'd1: y = a - b;
                default: y = a;
            endcase
        end
    end
endmodule
```

**建议步骤**：

1. **画意图树**。参照 4.3.4 的方法，画出两层嵌套：外层 `if (a==b)` 的 `Switch(signal = (a==b))`，其 else 分支 `Case` 内嵌一个 `case(op)` 的 `Switch(signal=op)`，后者挂三个 `Case`（`{0}`、`{1}`、默认 `{}`）。标出每个 `Case` 的 `actions`（HDL 意图）与 `finish` 注入的 `aux_actions`（对汇合线 `y` 的条件驱动）。
2. **跑出来对照**。用 heredoc 读入并 `proc; show`（参考 [tests/unit/dff.ys:1](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L1) 的形式）：

   ```bash
   yosys -p "read_slang <<EOF
   module top(input logic [1:0] op, input logic [3:0] a, b, output logic [3:0] y);
       always_comb begin
           if (a == b) y = 4'd0;
           else begin
               case (op)
                   2'd0: y = a + b;
                   2'd1: y = a - b;
                   default: y = a;
               endcase
           end
       end
   endmodule
   EOF
   proc; show"
   ```

3. **观察并解释**：`show` 里应能看到 `$eq`（`a==b`）、`$add`、`$sub`，以及把它们按条件选出的 `$mux` 链。解释每条 `aux_action` 如何变成一个 `$mux` 的输入。
4. **构造一个死分支**：把 `2'd0: y = a + b;` 临时改成 `3'd0:` 之外的某个在 2 位 `op` 下不可能的常量比较（或在 `case` 上方加一个 `if (1'b0) y = 4'd15;`），重新跑，观察死分支是否被 `branch` 的检测剪掉（少一个分支/少一个 `$mux` 输入）。**待本地验证**：实际是否被剪除取决于 `proc` 的优化力度；若仍可见，结合本讲 4.3.3 解释 `branch` 的检测条件为何可能没覆盖到这种情形。

**预期结果**：你能用本讲的术语（`Switch`/`Case`、`enter_branch`/`exit_branch`、`branch_updates`、`finish` 汇合线、末尾空 Switch）完整说清楚从 SV 源码到 `$mux` 链的每一步。

## 6. 本讲小结

- `StatementExecutor` 是一个 slang AST 访问者，按语句 `kind` 分派到各个 `handle`；它几乎不直接发控制流单元，而是在 `current_case` 下长 `Case`/`Switch` 意图树并同步更新 `vstate`。
- 赋值走 `ExpressionStatement → eval → assign_rvalue → update_variable_state`，同时压一个 HDL 意图 `Case::Action` 并 `vstate.set`；这是 blocking 语义在 case 树里的实现。
- `SwitchHelper` 把「分叉—汇合」公共逻辑抽成栈上 RAII 对象：`enter_branch` 零拷贝快照 vstate、`exit_branch` 回滚并取回本分支新值，二者靠 `VariableState::save/restore` 的 `swap` 完成。
- `branch` 在进入前做保守的死分支检测（开关信号与比较值均为常量且不等即丢弃）；`finish` 为「任一分支动过的位」建汇合线、把 `vstate` 改写为汇合线，并在每个分支 `Case` 写条件驱动。
- `if`/`case`/循环 handler 末尾都会「下钻一个空 Switch」以强制后续语句的动作优先级；循环用 `sw_stack` 积攒展开出来的多层 `SwitchHelper`。
- 兜底 `handle(Statement)` 报 `LangFeatureUnsupported`（软失败）、`handle(Expression)` 走 `unimplemented`（硬停），体现了防御式编程风格。

## 7. 下一步学习建议

- **u5-l3 赋值处理与位掩码**：本讲把赋值简化为「压 `Action` + `vstate.set`」，但部分位赋值（如 `a[1] = x`）如何与 `mask`/`unmasked_rvalue` 配合、`assign_to_lvalue_with_masking` 如何递归摊平动态左值，留待 u5-l3。
- **u5-l4 逃逸构造与循环展开**：本讲多次提到 `signal_escape`、`get_disable_flag`、`UnrollLimitTracking`，它们的完整建模（逃逸标志变量、`EscapeFrame`、展开限额耗尽诊断）在 u5-l4。
- **u6-l1/l3 时序与锁存器**：`finish` 建好的汇合线在「不完整赋值」下会触发锁存器推断；想看 `Case::Action` 如何被 `detect_possibly_unassigned_subset` 与 `insert_latch_signaling` 消费，直接读 [src/cases.h:122-L169](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h#L122-L169) 并配合 u6-l3。
- **源码延伸阅读**：想看 `SwitchHelper` 在更复杂场景的用法，可对比 `handle(CaseStatement)`（[L410](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L410)）的通配符处理与 `handle(ForeachLoopStatement)`（[L586](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L586)）的多维迭代展开。
