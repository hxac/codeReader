# NetlistContext：网表构建的中枢

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `NetlistContext` 为什么用「多重继承」把 `RTLILBuilder` 和 `DiagnosticIssuer` 拼到一起，以及这种聚合带来的设计好处。
- 指出 `canvas`、`wire_cache`、`driven_variables`、`register_driven_variables`、`initial_state` 等关键成员分别记录什么、在翻译流程里何时被读写。
- 解释 `realm`（实例体）与一个 `NetlistContext` 实例的「一一对应」关系，以及 `HierarchyQueue` 如何围绕这条对应关系管理整个设计的模块集合。

本讲是单元 3（核心数据模型）的总纲。它先把上一单元（u2）讲到的「PopulateNetlist 遍历 slang AST 产出 RTLIL」这步放大，让你看清：翻译过程中的所有状态——画布、线网、被驱动的位、诊断——都集中挂在一个叫 `NetlistContext` 的对象上。后续 u3-l2、u3-l3、u3-l4 会分别拆解它内部更细的抽象。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自 u1、u2）：

- **sv-elab 的职责边界**：slang 负责把 SystemVerilog 源码解析成 AST 并做语义分析；本仓库负责遍历那棵 AST，把它翻译成 Yosys 的字级 RTLIL 网表。
- **read_slang 的四阶段骨架**：参数装配 → slang 解析源码 → 创建 `Compilation` 并校验 → `PopulateNetlist` 遍历产出 RTLIL 模块。
- **RTLIL 基本概念**：Yosys 用 `RTLIL::Design` 包含若干 `RTLIL::Module`，每个模块里有 `RTLIL::Wire`（线网）、`RTLIL::Cell`（单元，如 `$add`、`$dff`）、`RTLIL::Process`（过程块）。`RTLIL::SigSpec` 是「一段信号的描述」，是 RTLIL 里最常用的值载体。
- **slang 的精化产物**：`ast::Compilation` 持有整棵精化后的设计树，顶层实例是 `ast::InstanceSymbol`，实例体是 `ast::InstanceBodySymbol`。

如果你对「HDL 意图」这个词感到陌生，可以这样理解：在翻译过程中，sv-elab 常常需要先记录「设计者想表达什么」（比如某个变量在某个分支里被赋值），等收集完上下文后再决定「最终网表里该长成什么样」（比如要不要插入一个锁存器）。本讲关注的 `NetlistContext`，就是同时承载这两类信息的容器。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | 声明 `NetlistContext`、`RTLILBuilder`、`DiagnosticIssuer`、`SynthesisSettings`、`EvalContext`、`HierarchyQueue`（在 .cc 内）等几乎所有核心数据结构。 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | 实现 `NetlistContext` 的构造/析构、`id`/`add_wire`/`wire`/`convert_static`、`register_driven`/`add_continuous_driver`、`find_symbol_realm` 等，以及 `HierarchyQueue` 与顶层实例的创建流程。 |
| [src/builder.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc) | 实现 `RTLILBuilder` 的全部方法：`new_id`、`bless_cell`、组合/时序单元发射、`add_placeholder_signal`、`connect` 等。 |
| [src/diag.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) | 实现 `DiagnosticIssuer` 的 `add_diag`/`report_into`，并集中登记所有诊断码的文案与严重级别。 |

> 提示：`RTLILBuilder` 与 `DiagnosticIssuer` 虽然在 `slang_frontend.h` 里声明、在各自 `.cc` 里实现，但它们都是为 `NetlistContext` 服务的「能力 mixin」，因此本讲会把三者合在一起看。

## 4. 核心概念与源码讲解

### 4.1 NetlistContext：多重继承与整体职责

#### 4.1.1 概念说明

把一段 SystemVerilog 翻译成 RTLIL，本质上是「一边走 AST，一边往一块画布上摆线网和单元」。这件事听起来简单，难在翻译过程不是一遍完成的：

- 表达式求值要立刻产出组合单元（如 `$add`）；
- 过程块里的赋值要先记成「意图」，等整个块走完再决定是组合逻辑还是触发器/锁存器；
- 任何一步发现不可综合的写法，都要能报错，但报错不能打断整个设计的翻译（要让用户一次看到尽量多错误）。

`NetlistContext` 就是把这些互相耦合的能力集中到一个对象上。它的核心设计是**多重继承**：同时继承 `RTLILBuilder`（负责「生成 RTLIL」）和 `DiagnosticIssuer`（负责「记录诊断」），再加上一组自有成员来记录「HDL 意图」与设计上下文。

用一个生活类比：`NetlistContext` 像是翻译官的办公桌——桌上同时放着「RTLIL 画图纸」（`canvas`）、「便签本」（记录意图的 `wire_cache`/`driven_variables` 等）和「问题清单」（`issued_diagnostics`）。翻译官随手就能在三者之间切换，而不需要到处传递一堆零散对象。

#### 4.1.2 核心流程

`NetlistContext` 在一次综合中的生命周期：

1. **创建**：从顶层实例（或层次展开时遇到的子实例）构造，立刻在 `RTLIL::Design` 上 `addModule` 建出画布 `canvas`。
2. **填充**：`PopulateNetlist` 以 `netlist.realm.visit(populate)` 为入口遍历实例体，过程中持续调用继承来的 RTLILBuilder 方法摆单元、写自有成员记录意图。
3. **收尾**：析构时对画布做 `fixup_ports()` 与 `check()`，确保产出的 `RTLIL::Module` 是良构的。

整个流程的关键是：所有状态都挂在 `netlist` 这一个引用上，`EvalContext`、`ProceduralContext`、`PopulateNetlist` 等都通过持有 `NetlistContext &` 来共享同一份上下文。

#### 4.1.3 源码精读

先看类的声明与它的「身份」字段：

[src/slang_frontend.h:537-560](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L537-L560) —— `NetlistContext` 多重继承自 `RTLILBuilder` 与 `DiagnosticIssuer`，并持有 `settings`、`compilation`、`realm`、`eval` 四个引用/对象。

这段代码做了什么：

- `struct NetlistContext : RTLILBuilder, public DiagnosticIssuer` —— 一句多重继承，把「画 RTLIL」和「攒诊断」两套能力并进来。
- `SynthesisSettings &settings` —— 指向命令行选项的内存表示（见 u2-l3），翻译过程中随时查询开关。
- `ast::Compilation &compilation` —— 指向 slang 的精化结果，用来查属性、查类型。
- `const ast::InstanceBodySymbol &realm` —— 本网表对应的实例体（见 4.4）。
- `EvalContext eval` —— 「背景」求值上下文，用于不在过程块里的表达式求值。

接着看构造函数，它把上面这些字段串起来，并立刻建出画布：

[src/slang_frontend.cc:3420-3429](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3420-L3429) —— 主构造函数：用 `instance.body` 初始化 `realm`，用 `*this` 初始化 `eval`，并通过 `design->addModule(...)` 创建 `canvas`，再把模块级属性从 slang 定义搬过来。

析构函数做收尾校验：

[src/slang_frontend.cc:3438-3445](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3438-L3445) —— 析构时若 `canvas` 仍有效，调用 `fixup_ports()` 补齐端口、`check()` 做合法性校验。注释提到「移动构造可能清空 canvas 指针」，所以这里加了空指针保护。

> 注意：类显式 `delete` 了拷贝构造与拷贝赋值（[src/slang_frontend.h:599-600](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L599-L600)），说明 `NetlistContext` 是「按引用传递、堆上创建」的对象，不希望被意外复制——它持有太多带指针成员的状态。

#### 4.1.4 代码实践

**实践目标**：从源码层面确认「多重继承」这一结构，并感受「一个对象聚合多职责」带来的调用便利。

**操作步骤**：

1. 打开 [src/slang_frontend.h:537](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L537)，确认 `NetlistContext` 的两个基类。
2. 在 `slang_frontend.cc` 中搜索 `netlist.add_diag(`（例如 [src/slang_frontend.cc:1811](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1811)）与 `netlist.canvas->`（例如 [src/slang_frontend.cc:1781](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1781)）。

**需要观察的现象**：调用方写的是 `netlist.add_diag(...)` 和 `netlist.canvas->addProcess(...)`——前者来自 `DiagnosticIssuer`，后者来自 `RTLILBuilder` 的成员。调用者完全不需要关心这两个能力分别来自哪个基类。

**预期结果**：你会看到同一个 `netlist` 对象在几行之内既能报错、又能往画布上摆东西，这正是多重继承带来的「写起来像同一个东西」的效果。

#### 4.1.5 小练习与答案

**练习 1**：`NetlistContext` 为什么要 `delete` 拷贝构造？如果允许拷贝会发生什么？

**参考答案**：因为它持有 `canvas`（裸指针）、`wire_cache`/`driven_variables`（带键的字典）、`realm`/`eval`（引用与回指自己的对象）。默认浅拷贝会导致两个 `NetlistContext` 共享同一块画布和同一份缓存，析构时还会 double-free。禁止拷贝强制大家用引用传递唯一的 `NetlistContext`。

**练习 2**：`EvalContext eval` 是 `NetlistContext` 的成员，而 `EvalContext` 内部又持有 `NetlistContext &netlist`（[src/slang_frontend.h:144](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L144)）。这种「互指」是否构成问题？

**参考答案**：不构成问题。`eval` 是 `NetlistContext` 的成员对象，构造时用 `*this` 把自己的引用交给 `eval.netlist`（见构造函数 `eval(*this)`）。两者生命周期一致（同生共死），只是互指，没有所有权冲突。

---

### 4.2 RTLILBuilder：生成 RTLIL 的「画笔」

#### 4.2.1 概念说明

`RTLILBuilder` 是一组「如何把高层运算翻译成 RTLIL 单元」的工具集。它把 Yosys 的 `RTLIL::Module` API 包了一层，提供更贴近 sv-elab 需求的便捷方法，比如：

- 直接发射组合单元：`Mux`、`Bwmux`、`Eq`、`Lt`、`Shift`、`ReduceBool`……
- 直接发射时序单元：`add_dff`、`add_dffe`、`add_aldff`、`add_aldffe`、`add_dual_edge_aldff`；
- 管理命名：`new_id` 生成唯一内部名；
- 管理属性：`staged_attributes` + `AttributeGuard` + `bless_cell` 实现「先暂存、后盖印」。

它最重要的成员是 `canvas`——一个指向当前正在构建的 `RTLIL::Module` 的指针。所有「生成 RTLIL」的动作最终都落到 `canvas` 上。

#### 4.2.2 核心流程

RTLILBuilder 发射一个组合单元的典型流程（以 `ReduceBool` 为例）：

1. 先做常量折叠捷径：若操作数全常量，直接调 `RTLIL::const_*` 算出结果，不建单元。
2. 否则用 `add_y_wire` 建一根结果线 `y`。
3. 调 `canvas->addReduceBool(...)` 建单元。
4. 调 `bless_cell(cell)` 把当前暂存的属性（`staged_attributes`）和源码范围盖到单元上。

属性暂存机制的设计动机：sv-elab 常常在求值表达式树时才知道某个单元该挂哪些 `(*属性*)`，但属性是在外层通过 `AttributeGuard` 设置的。`bless_cell` 在单元真正诞生时统一「盖印」，避免每个发射方法都重复处理属性。

#### 4.2.3 源码精读

先看 `RTLILBuilder` 的状态成员：

[src/slang_frontend.h:359-370](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L359-L370) —— `canvas` 指向当前模块；`staged_attributes` 是待盖印的属性表；`staged_source_range` 记录源码位置；`next_id`/`new_id` 生成内部唯一名。

`new_id` 的实现很简单但很关键——所有匿名线网/单元的名字都靠它：

[src/builder.cc:38-44](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L38-L44) —— 用一个自增计数器 `next_id` 拼 `$` 前缀名，可带 `base` 提示（如 `$add$3`）。

`bless_cell` 实现「盖印」：

[src/builder.cc:52-60](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L52-L60) —— 把 `staged_attributes` 整体赋给单元，并在没有显式 `src` 时用暂存的源码范围补一个 `src` 属性。

再看一个组合单元的完整发射：

[src/builder.cc:62-72](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L62-L72) —— `ReduceBool`：先尝试常量折叠与 1 位特例，否则建结果线、建 `$reduce_bool` 单元、盖印。

RTLILBuilder 提供的组合与时序单元方法清单（声明）：

[src/slang_frontend.h:372-413](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L372-L413) —— 一长串 `Mux`/`Bwmux`/`Shift`/`Eq`…… 与 `add_dff`/`add_dffe`/`add_aldff`/`add_aldffe`/`add_dual_edge_aldff`。

其中 `add_placeholder_signal` 与 `connect` 是「先建占位信号、后接驱动」这对核心操作：

[src/builder.cc:704-717](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L704-L717) —— `add_placeholder_signal`：在 `canvas` 上建一根给定宽度的线，`public_name` 决定用用户给的名字还是匿名 `$...` 名。

[src/builder.cc:462-469](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L462-L469) —— `connect`：用 `$buf` 单元把 `rhs` 驱动到 `target`（占位信号）上。

> 说明：关于 `$dffe`/`$aldff` 等单元的具体选择策略属于 u6（时序逻辑）的内容，本讲只确认「RTLILBuilder 提供了这些发射能力」。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `canvas` 上长出来的线网与单元，建立「RTLILBuilder 方法 ↔ RTLIL 单元」的直觉。

**操作步骤**（需要已按 README 构建好 `slang.so` 并安装支持的 Yosys；若环境不具备，可作为源码阅读型实践）：

1. 新建 `tiny.sv`：

```systemverilog
module top(input logic [3:0] a, input logic [3:0] b, output logic [3:0] y);
    assign y = (a > b) ? a + b : a - b;
endmodule
```

2. 用 heredoc 方式（与仓库测试一致）跑 `read_slang`，再 dump 出 RTLIL：

```bash
yosys -p "read_slang tiny.sv --top top; prep; write_rtlil top_rtlil.txt"
```

3. 在 `top_rtlil.txt` 里查找 `$gt`、`$mux`、`$add`、`$sub` 这些单元。

**需要观察的现象**：RTLIL 文本里会出现若干 `cell $add $...`、`cell $mux $...` 等，它们的 `connect` 行把 `a`、`b`、中间线连起来；每个单元大概率带一个 `attribute \src` 指向源码位置（这正是 `bless_cell` 盖的印）。

**预期结果**：你能在 dump 里找到与表达式一一对应的单元。具体的命名（`$add$0` 还是 `$add$3`）取决于遍历顺序，**待本地验证**；但单元类型与连接关系应与上面分析一致。

> 若无法运行：改为阅读 [src/builder.cc:62-72](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L62-L72)，对着 `ReduceBool` 的「常量折叠捷径 → 建结果线 → 建单元 → 盖印」四步，自己推演 `a + b` 会走哪条路（非常量 → 建 `$add`）。

#### 4.2.5 小练习与答案

**练习 1**：`ReduceBool` 为什么要在最前面判断 `a.is_fully_const()`？

**参考答案**：常量折叠捷径。如果操作数在翻译期就是常量，直接调 `RTLIL::const_reduce_bool` 算出 1 位常量返回，不必在网表里留下一个永远输入固定的 `$reduce_bool` 单元，能减小网表体积。

**练习 2**：`bless_cell` 为什么要读 `staged_source_range_valid` 而不是每次都算 `src`？

**参考答案**：因为很多表达式叶子（如纯常量）根本不需要 `src` 字符串；源码范围被「暂存」起来，只有真正需要时（valid 为 true 且单元还没有 `src`）才格式化并写入，避免无谓的字符串构造开销（参见头文件注释 [src/slang_frontend.h:364-367](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L364-L367)）。

---

### 4.3 DiagnosticIssuer：诊断的延迟上报

#### 4.3.1 概念说明

`DiagnosticIssuer` 是一个「混入（mixin）」类，提供「把诊断先攒起来、之后再统一上报」的能力。它复用 slang 的 `slang::Diagnostic` / `slang::DiagCode` 体系（不另造轮子），但改变了上报时机：

- 翻译期：`add_diag(code, location)` 把一条诊断塞进 `issued_diagnostics` 向量，并返回引用以便链式填 `{}` 占位符。
- 报告期：`report_into(engine)` 把攒下的诊断逐条交给 slang 的 `DiagnosticEngine` 真正格式化、输出。

为什么要「先攒后报」？因为 sv-elab 希望一次翻译尽量多地把错误都找出来给用户，而不是遇到第一个错误就抛异常中断。攒在一个向量里，最后还能统一排序、去重。`NetlistContext` 和 `InferredMemoryDetector` 都继承了它，所以「网表构建」和「存储器推断」这两件事都能各自攒自己的诊断。

#### 4.3.2 核心流程

一条诊断从产生到进入 Yosys 日志：

1. 翻译中某处发现不可综合写法，调用 `netlist.add_diag(diag::XXX, loc)`，返回 `Diagnostic&`。
2. 链式 `<< "..."` 填充文案占位符。
3. 翻译结束后，`execute()` 把 `netlist.issued_diagnostics` 与 `populate.mem_detect.issued_diagnostics` 合并、排序、去重。
4. 调 `report_into` 或等价的 `engine.issue` 逐条输出，slang 的输出再被劫持转发进 Yosys 的 `log()`。

#### 4.3.3 源码精读

`DiagnosticIssuer` 的全部接口非常小：

[src/slang_frontend.h:473-486](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L473-L486) —— 三个 `add_diag` 重载 + `add_diagnostics` + `report_into` + 公开成员 `issued_diagnostics`。

实现同样精简：

[src/diag.cc:24-52](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L24-L52) —— `add_diag` 往 `issued_diagnostics` 末尾 `emplace_back` 一条并返回引用（支持链式 `<<`）；`report_into` 遍历向量逐条 `engine.issue`。

诊断码本身集中定义在 `diag` 命名空间，全部挂在 `DiagSubsystem::Netlist` 下，编号从 1000 起：

[src/diag.cc:55-66](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L55-L66) —— 例如 `LatchNotInferred` 是 `(Netlist, 1010)`，与 slang 原生诊断区分开。

最后看一个真实触发点，体会「链式填占位符」：

[src/slang_frontend.cc:3370-3371](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3370-L3371) —— 在 `check_hier_ref` 里，当跨保留层次边界引用时，`add_diag(diag::ReferenceAcrossKeptHierBoundary, range)` 产生一条诊断。

而这条诊断的文案「hierarchical reference across preserved module boundary」登记在：

[src/diag.cc:270-271](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L270-L271) —— `setup_messages` 里 `engine.setMessage(ReferenceAcrossKeptHierBoundary, ...)` 并设为 `Error`。

> 关于诊断系统的完整链路（包括负向测试 `check_diagnostics`、`expected_diagnostic`、`captureOutput` 等）已在 u2-l4 讲过，本讲只关注 `DiagnosticIssuer` 这个「容器」角色。

#### 4.3.4 代码实践

**实践目标**：跟踪一条诊断从「触发」到「文案登记」的两个落点。

**操作步骤**：

1. 在 [src/diag.cc:166-167](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L166-L167) 读到 `LatchNotInferred` 的文案 `"latch not inferred for variable '{}' driven from always_latch procedure"`，严重级别为 `Warning`。
2. 在 `slang_frontend.cc` 中搜索 `diag::LatchNotInferred`，找到触发点 [src/slang_frontend.cc:1811-1812](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1811-L1812)。
3. 观察它的写法：`auto &diag = netlist.add_diag(diag::LatchNotInferred, symbol.location); diag << chunk.text();`。

**需要观察的现象**：`add_diag` 返回的 `diag` 引用被 `<<` 填入 `chunk.text()`，正好对应文案里的 `{}` 占位符。

**预期结果**：当某个 `always_latch` 块里有一个变量被判定为「没有推断出锁存器」时，用户看到的告警里 `{}` 就会被替换成那个变量的名字。具体运行输出**待本地验证**，但文案拼装逻辑可从源码直接确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `add_diag` 要返回 `Diagnostic&` 而不是 `void`？

**参考答案**：为了支持链式填充占位符。调用方写成 `add_diag(code, loc) << "..."`，返回引用正好接上 `operator<<`，省去先取出末尾元素再填的麻烦（见 [src/diag.cc:24-28](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L24-L28)）。

**练习 2**：`NetlistContext` 和 `InferredMemoryDetector` 都继承 `DiagnosticIssuer`，这会不会导致同一类诊断被重复上报？

**参考答案**：不会，因为它们各自维护独立的 `issued_diagnostics`，记录的是各自翻译阶段产生的问题；最终在 `execute()` 里会被合并进同一个 `Diagnostics` 向量再排序去重（见 [src/slang_frontend.cc:3782-3785](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3782-L3785)）。

---

### 4.4 关键状态成员与 realm 的对应关系

#### 4.4.1 概念说明

除了从两个基类继承来的能力，`NetlistContext` 自身还携带一大批「记录 HDL 意图」的状态成员。可以大致把它们分成三类：

| 类别 | 代表成员 | 作用 |
| --- | --- | --- |
| 命名/线网缓存 | `wire_cache`、`id()`/`hdlname()`、`scopes_remap` | 把 slang 符号稳定映射到 RTLIL 名字与 `SigSpec`，避免重复建线 |
| 被驱动记录 | `driven_variables`、`register_driven_variables`、`special_net_drivers`、`initial_state` | 记录「哪些位被赋值了、被什么赋值」，供锁存器/触发器/特殊网收尾用 |
| 层次/黑盒判定 | `realm`、`find_symbol_realm`、`should_dissolve`、`is_blackbox`、`detected_memories` | 决定模块边界、存储器推断 |

本节聚焦三个最常被读写的成员——`wire_cache`、`driven_variables`、`realm`，并把 `realm` 与「一个 NetlistContext 实例」的对应关系讲透。

#### 4.4.2 核心流程

**线网缓存流程**：当翻译第一次遇到某个变量符号时，调 `add_wire(symbol)` 在 `canvas` 上建线、设置 `upto`/`start_offset`、把结果存进 `wire_cache[&symbol]`；之后所有地方都用 `wire(symbol)` 从缓存取，避免重复建线。

**被驱动记录流程**：每当一个变量（或一段 `VariableBits`）出现在赋值左值，就调 `register_driven(vbits)` 把它的每一位塞进 `driven_variables`；连续赋值则进一步用 `add_continuous_driver` 把右值 `connect` 到对应信号。这些记录是后续「这个变量是组合驱动还是需要触发器/锁存器」判断的输入。

**realm 对应关系**：一个 `NetlistContext` 实例 ↔ 一个不会被展平（dissolve）的实例体 `realm`。当设计完全展平时，`realm` 就是顶层模块体；当保留层次时，每个保留边界对应一个 `NetlistContext`。`find_symbol_realm(symbol)` 用于回答「这个符号最终属于哪个 realm 的网表」。

#### 4.4.3 源码精读

先看这批状态成员的声明：

[src/slang_frontend.h:562-587](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L562-L587) —— `emitted_mems`、`scopes_remap`、`wire_cache`、`driven_variables`、`register_driven_variables`、`special_net_drivers`/`special_net_symbols`、`initial_state`、`disabled`。

`add_wire` 是线网缓存的核心：

[src/slang_frontend.cc:3141-3168](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3141-L3168) —— 根据符号类型算出位宽，用 `AttributeGuard` 暂存属性，调继承来的 `add_placeholder_signal` 建公开名线网，处理打包数组的 `upto`/`start_offset`，最后写入 `wire_cache`，并对特殊网类型（wand/wor）登记到 `special_net_symbols`。

`wire` 则是缓存的读取端，找不到就直接报错中断：

[src/slang_frontend.cc:3386-3392](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3386-L3392) —— 在 `wire_cache` 里查，未命中则 `wire_missing`（一个 `[[noreturn]]` 的助手）。

被驱动记录的两个重载：

[src/slang_frontend.cc:643-652](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L643-L652) —— `register_driven` 遍历 `VariableBits` 的每一位插入 `driven_variables`；符号重载先转成 `Variable` 再调用前者。

连续驱动则把「记录意图」和「生成 RTLIL」结合起来：

[src/slang_frontend.cc:654-672](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L654-L672) —— `add_continuous_driver` 按 chunk 遍历左值：特殊网位记进 `special_net_drivers`（留待 `finalize_special_nets` 做 reduce-and/or），普通位则 `register_driven` 后用 `connect` 接上右值。

接下来是 `realm` 的对应关系。先看 `find_symbol_realm` 如何沿作用域链向上找到「不会被展平的实例体」：

[src/slang_frontend.cc:3304-3329](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3304-L3329) —— 从符号的父作用域逐层上溯，遇到 `InstanceBody` 就看它的父实例是否会被 `should_dissolve`：若会则继续上溯，若不会（或已到 Root）就把这个实例体作为 realm 返回。

而「一个 realm ↔ 一个 NetlistContext」由 `HierarchyQueue` 维护：

[src/slang_frontend.cc:1696-1718](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1696-L1718) —— `HierarchyQueue` 用 `netlists`（以 `InstanceBodySymbol*` 为键的映射）保证每个 realm 最多建一个 `NetlistContext`，`get_or_emplace` 在命中时返回已有引用、未命中时 `new` 一个并加入 `queue`。

最后看顶层流程如何把这两者串起来：

[src/slang_frontend.cc:3764-3780](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3764-L3780) —— 对每个顶层实例取 `ref_body`，`get_or_emplace` 建/取对应 `NetlistContext`，标记 `canvas` 的 `\top` 属性；随后遍历 `hqueue.queue`，对每个 `netlist` 构造 `PopulateNetlist` 并以 `netlist.realm.visit(populate)` 进入翻译。

> 名字小知识：`realm` 直译「领域」，在这里指「这个网表所管辖的实例体范围」。`module_type_id`（[src/slang_frontend.cc:210-218](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L210-L218)）会把实例的层次路径拼进模块名，所以保留层次时你会看到形如 `\modname$inst.path` 的模块名。

#### 4.4.4 代码实践

**实践目标**：用源码阅读的方式，理清「符号 → realm → NetlistContext」的映射，体会层次保留与完全展平的差别。

**操作步骤**：

1. 准备一个两层层次的设计（示例代码，仅用于阅读对照，不必运行）：

```systemverilog
// 示例代码
module sub(input logic a, output logic y);
    assign y = ~a;
endmodule
module top(input logic a, output logic y);
    logic w;
    sub u_sub(.a(a), .y(w));
    assign y = w & a;
endmodule
```

2. 跟踪 `top` 的实例体：在完全展平（默认）模式下，`should_dissolve(sub 的实例)` 返回 true，于是 `find_symbol_realm(sub 内部的符号)` 会一路回到 `top` 的实例体——全设计只有一个 realm、一个 `NetlistContext`。
3. 若改用 `--keep-hierarchy`（u2-l3 讲过的 `hierarchy_mode()==ALL`），`should_dissolve` 对 `sub` 返回 false，于是 `sub` 的实例体成为一个新 realm，`HierarchyQueue::get_or_emplace` 会为它新建第二个 `NetlistContext`。

**需要观察的现象**：`find_symbol_realm` 的返回值取决于 `should_dissolve` 的判定，而后者又受 `settings.hierarchy_mode()` 影响——这正是「选项 → 层次策略 → realm 划分 → NetlistContext 数量」的因果链。

**预期结果**：默认模式 dump 出 1 个模块（`top`，内部已展平 `sub`）；`--keep-hierarchy` 模式 dump 出 2 个模块（`top` 与 `sub`，`top` 里有一个 `\sub` 类型的 cell）。具体命名与连接**待本地验证**。

> 进阶：在 [src/slang_frontend.cc:3226](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3226) 起的 `should_dissolve` 里，逐条核对黑盒、interface、inout 端口等「不展平」的判定条件，能进一步解释为什么某些模块必然成为独立 realm。

#### 4.4.5 小练习与答案

**练习 1**：`wire_cache` 的键为什么是 `const ast::Symbol*`（符号指针）而不是符号名字符串？

**参考答案**：因为同一个名字在不同作用域/实例里可能指不同符号（比如两个模块都有叫 `a` 的变量）。用符号指针作键能精确区分「这个具体的变量声明」，且符号在 slang 精化后地址稳定。头文件里还为 `const slang::ast::Symbol*` 特化了 `hash_ops`（[src/slang_frontend.h:35](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L35)）以支持作为字典键。

**练习 2**：`register_driven` 记录的是「位」（`VariableBit`）而非「整个变量」，这样做有什么好处？

**参考答案**：因为部分位赋值（如 `a[1] = x`、`{x, y} = expr`）只驱动变量的某些位。按位记录能让后续判断精确到「这一位是否在所有路径上都被赋值」，这正是锁存器推断（u6-l3 的 `detect_possibly_unassigned_subset`）所需要的粒度。

**练习 3**：如果两个不同的 slang 符号恰好映射到同一个 realm，它们会被放进同一个 `NetlistContext` 吗？

**参考答案**：会。realm 与 `NetlistContext` 是一一对应的（由 `HierarchyQueue::netlists` 映射保证）。同一 realm 下所有符号的线网、单元、诊断都落在同一个 `canvas` 和同一个 `issued_diagnostics` 里——这也是为什么「跨 realm 引用」需要 `check_hier_ref` 特别处理（[src/slang_frontend.cc:3361-3384](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3361-L3384)）。

## 5. 综合实践

把本讲的知识串起来，完成一张 **`NetlistContext` 成员关系图**。这是本讲的核心实践任务。

**任务**：画一张图（手绘或用任意工具），中央是 `NetlistContext`，它向上连出两个基类 `RTLILBuilder`、`DiagnosticIssuer`，向下连出四个引用成员 `settings`/`compilation`/`realm`/`eval`，再展开 4.4 表格里的状态成员。然后用两种颜色（或标记）区分：

- 🟦 **生成 RTLIL**：`canvas`（来自 RTLILBuilder）、`staged_attributes`/`bless_cell`、`new_id`、`add_placeholder_signal`/`connect`、各种 `add_*` 单元发射方法、`add_wire`（建线并写缓存）、`convert_static`（把意图位转成 SigSpec）。
- 🟧 **记录 HDL 意图**：`wire_cache`（符号→已建信号的缓存）、`driven_variables`/`register_driven`（哪些位被驱动）、`register_driven_variables`（寄存器/锁存器驱动位）、`special_net_drivers`/`special_net_symbols`（wand/wor）、`initial_state`（初值）、`scopes_remap`、`detected_memories`。
- ⬜ **上下文/边界**：`settings`、`compilation`、`realm`、`find_symbol_realm`/`should_dissolve`/`is_blackbox`、`disabled`。
- 🟥 **诊断**：`issued_diagnostics`（来自 DiagnosticIssuer）、`add_diag`/`report_into`。

**验收要点**（自检）：

1. 你能说出 `canvas` 是「画图纸」、`wire_cache` 是「便签」、`issued_diagnostics` 是「问题清单」这三者的分工吗？
2. 你能解释为什么 `add_continuous_driver` 同时碰到两类颜色（先 `register_driven` 记意图，再 `connect` 生成 RTLIL）吗？这正是 `NetlistContext` 把两类职责聚合在一起的典型场景。
3. 你能指出 `realm` 是连接「上下文」与「层次边界」的枢纽，并由 `HierarchyQueue` 维护一一对应吗？

> 想进一步加深印象：在图上标出 u2-l2 讲过的「握手点」——`compilation->getRoot().topInstances` → `get_instance_body` → `get_or_emplace` → `netlist.realm.visit(populate)`，确认整条数据流都流经你画的这个 `NetlistContext`。

## 6. 本讲小结

- `NetlistContext` 通过多重继承 `RTLILBuilder` + `DiagnosticIssuer`，把「生成 RTLIL」「记录诊断」「记录 HDL 意图」三类职责聚合到一个对象上，调用方写 `netlist.xxx()` 即可，无需关心能力来自哪个基类。
- `RTLILBuilder` 是生成 RTLIL 的「画笔」，核心成员 `canvas` 指向当前 `RTLIL::Module`；它用 `new_id` 管命名、`staged_attributes`+`bless_cell` 管属性、`add_placeholder_signal`+`connect` 管「先占位后接驱动」。
- `DiagnosticIssuer` 是「先攒后报」的诊断容器，`add_diag` 返回引用以支持链式填占位符，`report_into` 在报告期统一交给 slang 的 `DiagnosticEngine`。
- 关键状态成员分工明确：`wire_cache` 缓存符号→信号，`driven_variables`/`register_driven_variables` 按位记录被驱动情况，`initial_state`/`special_net_drivers` 服务于初值与特殊网收尾。
- `realm`（实例体）与一个 `NetlistContext` 实例一一对应，由 `HierarchyQueue` 维护；`find_symbol_realm` 沿作用域链、结合 `should_dissolve` 判定符号归属哪个 realm。
- `NetlistContext` 在构造时 `addModule` 建画布、在析构时 `fixup_ports`+`check` 收尾，且禁止拷贝，强调「唯一、堆上、按引用传递」。

## 7. 下一步学习建议

- **u3-l2 RTLILBuilder：RTLIL 单元动物园**：本讲只把 `RTLILBuilder` 当「画笔」概览，下一讲会逐个拆解 `Mux`/`Bwmux`/`add_dffe`/`add_aldff` 等方法到底建了什么单元、端口怎么连，并细讲 `AttributeGuard` 的暂存/恢复机制。
- **u3-l3 Variable 与 VariableBits**：本讲多次提到 `VariableBits`/`VariableBit`（被驱动记录、`convert_static`、`add_continuous_driver`），下一讲会讲清这套「HDL 意图位级抽象」与 `RTLIL::SigSpec` 的区别。
- **u3-l4 Case 与 Switch**：`ProceduralContext`（本讲提到的、持有 `NetlistContext &` 的过程块上下文）内部用 `Case`/`Switch` 树记录过程块意图，下一讲展开。
- **延伸阅读**：想提前看层次处理全貌，可读 [src/slang_frontend.cc:3226](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3226) 起的 `should_dissolve` 与 u7-l2。
