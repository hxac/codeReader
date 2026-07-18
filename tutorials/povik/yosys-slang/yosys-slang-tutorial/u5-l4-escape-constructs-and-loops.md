# 逃逸构造：break/continue/return 与循环展开

## 1. 本讲目标

本讲承接 u5-l2（StatementExecutor 遍历过程块语句），专门解决一个被前面刻意略过的问题：sv-elab 怎么处理 `break`、`continue`、`return`，以及 `for` / `while` / `foreach` 这些循环。

学完本讲，你应当能够：

- 说清楚为什么 sv-elab 把 `break`/`continue`/`return` 统称为「逃逸构造（escape construct）」，以及它如何用一个「逃逸标志变量」来建模它们。
- 读懂 `EscapeFrame`、`escape_stack`、`RegisterEscapeConstructGuard` 三者的协作，并能解释 `signal_escape` 为什么会从内向外逐层「点亮」标志。
- 解释 `for` 循环是怎么被「展开」成一棵嵌套 case 树的，以及 `UnrollLimitTracking` 在展开次数超限时如何报错。
- 说出函数（`SubroutineSymbol`）调用时 `EnterAutomaticScopeGuard` 与函数体逃逸栈帧是如何配合，从而支持 `return` 与可重入的。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**直觉一：sv-elab 没有真正的「跳转」。**
硬件里没有 `goto`，只有组合逻辑与时序逻辑。sv-elab 把 `always`/`initial` 翻译成一棵 case 树（参见 u3-l4、u5-l1），所有的控制流最终都要落到「在某些条件下做某些赋值」。所以 `break`、`continue`、`return` 这种「中途跳出」的语义，必须被改写成「从这一刻起，后面所有语句都不要执行」的条件控制。

sv-elab 的做法是给每一个可被逃逸的构造（一个循环、一次循环体、一个函数体）配一个**虚拟的 1 位标志变量**，初值为 0；一旦发生逃逸，就把对应标志置 1；标志为 1 之后，同一作用域内排在后面的语句都会被一个「标志==0 才执行」的 switch 分支挡掉。这就用纯数据流的方式模拟了「跳过后续语句」。

**直觉二：循环 = 编译期重复展开。**
综合器不能生成「循环执行」的硬件（那是仿真器的概念）。`for` 循环在 sv-elab 里被处理成：在编译期把循环体复制若干次，每次用不同的循环变量值，展开成一棵层层嵌套的 case 树。如果循环次数是常量且不大，这棵树就有限；如果循环条件依赖运行时信号，sv-elab 也会尽力展开（每一轮复制一个带条件的分支），但展开次数必须有上限，否则编译会失控——这个上限就是 `unroll_limit`（默认 4000，见 u2-l3）。

> 术语提示：本讲反复出现「标志（flag）」「帧（frame）」「展开（unroll）」三个词。标志是一个 1 位 `Variable`；帧是逃逸栈上的一项，记录「这是哪种逃逸构造 + 它的标志是谁」；展开指编译期把循环体复制多次。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | 声明 `EscapeFrame`、`ProceduralContext::escape_stack`、`RegisterEscapeConstructGuard`、`UnrollLimitTracking`、`EnterAutomaticScopeGuard` 等核心抽象。 |
| [src/procedural.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc) | 实现逃逸栈的压栈/弹栈、`signal_escape`、`get_disable_flag`，以及 `UnrollLimitTracking` 的计数与报错。 |
| [src/statements.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h) | `StatementExecutor` 里对 `For/While/Foreach` 循环、`break/continue/return`、函数调用的 `handle`，是把直觉落成代码的地方。 |
| [src/variables.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.cc) | `Variable::escape_flag`——逃逸标志变量的工厂方法。 |
| [src/diag.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) | 展开超限、缺停止条件等诊断码的文案与严重级别登记。 |

## 4. 核心概念与源码讲解

### 4.1 逃逸构造与逃逸标志变量

#### 4.1.1 概念说明

SystemVerilog 里有三类「中途退出」的语句：

- `break`：退出**当前整个循环**；
- `continue`：跳过**当前这一轮循环体**的剩余部分，进入下一轮；
- `return`：退出**当前整个函数**。

这三者看似不同，但本质都是「从这里开始，后面别执行了，直接跳到某个外层构造的末尾」。sv-elab 把承载这种跳转的外层构造统称为**逃逸构造**，分成三种 `EscapeConstructKind`：

```cpp
enum EscapeConstructKind {
    Loop,          // 我们通过它逃逸以实现 break
    LoopBody,      // 通过它逃逸以实现 continue
    FunctionBody,  // 通过它逃逸以实现 return
};
```

每个逃逸构造在创建时配一个 1 位标志变量。这个标志**不是源码里写的变量**，而是 sv-elab 自己造的「虚拟变量」，用 `Variable::escape_flag(id)` 工厂方法生成（u3-l3 讲过 `Variable` 的 `Kind` 枚举里有专门的 `EscapeFlag` 类别）。

#### 4.1.2 核心流程

整个逃逸机制围绕一个**逃逸栈** `escape_stack` 运转。栈里每一项是一个 `EscapeFrame`，记录「这是哪种逃逸构造」和「它的标志是谁」。规则是：

1. 进入一个循环/函数体时，压入一个新帧（`RegisterEscapeConstructGuard` 构造函数做这件事），并给它的标志赋初值 0。
2. 遇到 `break`/`continue`/`return` 时，调用 `signal_escape`：从栈顶（最内层）往栈底（最外层）扫描，**把沿途每个帧的标志都置 1**，直到遇到与请求种类匹配的那一帧为止。
3. 一个语句序列在执行每条语句前，先读「最内层帧的标志」当前值；若为 1，说明已经逃逸过，跳过本条及后续语句。
4. 离开循环/函数体时，弹出栈顶帧（`RegisterEscapeConstructGuard` 析构函数）。

关键点在第 2 步的「从内向外逐层点亮」。为什么 `break` 要把内层的 `LoopBody` 标志也置 1？因为 `break` 不仅退出整个循环，也等于结束了当前这一轮循环体；把内层标志也点亮，就能让当前循环体里排在 `break` 之后的语句也立刻被跳过。`return` 更极端，它要点亮从最内层一直到 `FunctionBody` 的**所有**帧——因为函数返回意味着退出一切嵌套结构。

用一个简单的状态来描述某个标志 \( f \) 的取值：

\[
f = \begin{cases} 1 & \text{已发生对应逃逸} \\ 0 & \text{尚未逃逸} \end{cases}
\]

语句序列中第 \( k \) 条语句被执行的充要条件是：执行到它之前，最内层标志仍为 0。

#### 4.1.3 源码精读

先看 `EscapeFrame` 的结构定义，它就在 `ProceduralContext` 内部：

[src/slang_frontend.h:295-310](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L295-L310) —— 声明逃逸帧：`flag` 是 1 位标志变量，`subroutine` 仅对函数体非空，`kind` 区分三种逃逸构造。

逃逸栈本身是 `ProceduralContext` 的私有成员：

[src/slang_frontend.h:317](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L317) —— `std::vector<EscapeFrame> escape_stack;` 一切逃逸状态的根。

逃逸标志变量由工厂方法产生，`kind` 设为 `EscapeFlag`：

[src/variables.cc:39-45](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/variables.cc#L39-L45) —— `Variable::escape_flag(int id)` 造一个仅由整数 `id` 标识的虚拟变量；不同逃逸构造拿不同 `id`，互不混淆。

最核心的 `signal_escape`，体现「从内向外逐层点亮」：

[src/procedural.cc:86-96](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L86-L96) —— 用反向迭代器 `rbegin()` 从栈顶扫向栈底，对每个帧 `do_simple_assign(loc, flag, S1)` 把标志置 1；遇到 `kind` 匹配的帧就 `break` 停下。最后的 `log_assert(it != rend())` 是一道防线：如果请求 `break` 却在栈里找不到任何 `Loop` 帧，说明源码把 `break` 写在了循环外面，直接 abort。

读取「当前是否已逃逸」的入口是 `get_disable_flag`：

[src/procedural.cc:488-494](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L488-L494) —— 返回栈顶帧的标志；栈为空（不在任何逃逸构造内）时返回一个「空」`Variable`，调用方据此判断「没有逃逸可能，无需 gating」。

#### 4.1.4 代码实践

**实践目标**：用一个带 `break` 的 `for` 循环，看清「逃逸标志如何让后续语句被跳过」。

**操作步骤**（源码阅读型实践）：

1. 打开 [tests/unit/break.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/break.sv) 的第一个模块 `priority_encoder`：它遍历 `bits`，找到第一个为 1 的位就 `encoded = i; break;`。
2. 对照 [src/statements.h:717-720](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L717-L720) 的 `handle(BreakStatement)`，确认 `break` 只是调用 `signal_escape(..., Loop)`。
3. 跟踪 `signal_escape`（[src/procedural.cc:86-96](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L86-L96)）：此时栈顶是循环体的 `LoopBody` 帧（更内），下一层是循环的 `Loop` 帧；`break` 把两个标志都置 1，在 `Loop` 帧处停下。
4. 回到 `for` 循环的展开循环（[src/statements.h:557-568](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L557-L568)）：`break_rv = substitute_rvalue(guard1.flag)` 读到的是「循环标志」当前值；非全常量时开一个 `switch(break_rv)`，只在 `break_rv==0` 分支里继续，`break_rv==1`（已 break）就跳出展开循环。

**需要观察的现象**：`break` 没有产生任何「跳转」单元，而是让「循环标志」变成 1，于是同一次展开里 `break` 之后的语句（在 `StatementList` 的 gating 下）和「是否进入下一轮展开」都由这个 1 位标志决定。

**预期结果**：最终网表里，`encoded` 会被一连串「如果第 i 位为 1 且更高位都为 0」的条件选择驱动——这正是 `for + break` 展开后的优先编码器逻辑。具体网表细节**待本地验证**（可用 `read_slang` 后 `show` 或 `dump` 观察）。

#### 4.1.5 小练习与答案

**练习 1**：在一个嵌套两层 `for` 循环的内层写 `break`，`signal_escape` 会点亮哪些帧的标志？

**参考答案**：从内层栈顶的 `LoopBody`（内层循环体）开始，点亮它，再到内层的 `Loop`（内层循环）帧——因为 `break` 请求的是最近的 `Loop`，匹配即停。外层循环的帧不受影响，所以外层循环会正常进入下一轮。

**练习 2**：`continue` 请求 `LoopBody`，为什么通常只点亮一个帧？

**参考答案**：`continue` 只想结束当前这一轮循环体，栈顶通常就是当前循环体的 `LoopBody` 帧，`kind` 立刻匹配，所以只置 1 这一个标志就停下，循环本身（`Loop` 帧）不退出，下一轮照常展开。

---

### 4.2 RegisterEscapeConstructGuard：逃逸构造的作用域守卫

#### 4.2.1 概念说明

`signal_escape` 依赖逃逸栈上有正确的帧。谁来负责「进入时压栈、离开时弹栈」？答案是 RAII 风格的守卫类 `RegisterEscapeConstructGuard`。它在构造时压入一个 `EscapeFrame` 并给标志赋初值 0，在析构时弹出该帧——和 u3-l2 讲过的 `AttributeGuard` 是同一种「构造-析构成对」的套路，保证即使中途 `return`/异常也不会泄漏栈帧。

守卫把构造时生成的标志变量作为公有成员 `flag` 暴露出来，这样循环展开代码既能「读到本循环是否被 break」（`guard1.flag`），也能在内层嵌套时引用它。

#### 4.2.2 核心流程

```
{
    RegisterEscapeConstructGuard guard1(ctx, Loop, &stmt);  // 压栈，flag=0
    // ... 循环体或子构造，可能 signal_escape 把 guard1.flag 置 1 ...
    // 读 guard1.flag 决定是否还要展开下一轮
}   // ~RegisterEscapeConstructGuard 弹栈
```

构造函数有两个重载：一个给函数体（带 `SubroutineSymbol*`），一个给循环（带 `Statement*` 作为定位用）。两者都断言 `kind` 必须与重载匹配，防止误用。

#### 4.2.3 源码精读

类声明与暴露的 `flag` 成员：

[src/slang_frontend.h:340-357](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L340-L357) —— 注意 `const Variable flag;` 在构造函数初始化列表里由 `Variable::escape_flag(context.flag_counter++))` 生成，每个守卫拿一个全局递增的 `id`。

函数体重载的构造函数：

[src/procedural.cc:55-66](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L55-L66) —— `assert(kind == FunctionBody)`，压栈，记录 `subroutine`，然后 `do_simple_assign(..., flag, RTLIL::S0, true)` 给标志赋初值 0（true 表示 blocking 语义）。

循环重载的构造函数：

[src/procedural.cc:68-78](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L68-L78) —— `log_assert(kind == Loop || kind == LoopBody)`，同样压栈并赋初值 0。

析构函数极其简单：

[src/procedural.cc:107-110](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L107-L110) —— 仅 `escape_stack.pop_back()`。栈的 LIFO 特性保证了嵌套构造的正确嵌套。

#### 4.2.4 代码实践

**实践目标**：看清一个 `for` 循环里同时存在哪几个逃逸帧。

**操作步骤**：

1. 阅读 [src/statements.h:534-555](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L534-L555)（`handle(ForLoopStatement)` 开头）：`guard1` 用 `Loop` 注册，代表「整个循环」，`break` 通过它退出。
2. 紧接着循环体被包在 `guard2`（`LoopBody`）里（[src/statements.h:552-555](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L552-L555)），代表「当前这一轮」，`continue` 通过它退出。
3. 想象此刻 `escape_stack` 自底向上是 `[...外层..., Loop(guard1), LoopBody(guard2)]`。

**需要观察的现象**：循环每展开一轮，`guard2` 都构造-析构一次（每轮一个新的 `LoopBody` 帧，但用同一个 `id`？不——`flag_counter++` 每次构造都自增，所以每轮的 `LoopBody` 标志是**不同**的虚拟变量）。`guard1` 在整个循环期间只构造一次。

**预期结果**：你能在脑中画出「外层帧不动，内层帧随展开轮次进出」的动态画面。这一点对理解展开后的网表规模很重要：每一轮展开都会引入新的标志变量与新的条件分支。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `guard2`（`LoopBody`）放在 `while (true)` 展开循环的**内部**，而 `guard1`（`Loop`）放在**外部**？

**参考答案**：`guard1` 代表整个循环，整个循环只「注册」一次；`guard2` 代表「当前这一轮循环体」，每一轮展开都是一个全新的作用域，`continue` 只应影响本轮，所以必须每轮重新压栈/弹栈。若 `guard2` 也放在外部，`continue` 点亮的就会是同一帧，无法区分不同轮次。

**练习 2**：守卫的析构只做 `pop_back()`，会不会把标志变量「泄漏」到网表里？

**参考答案**：标志是虚拟 `Variable`，弹出栈只是不再让它参与后续逃逸判定；它在展开过程中已经被 `do_simple_assign` / `Case::Action` 物化进 case 树，这部分逻辑是正确硬件行为，不算泄漏。没有被任何逃逸路径点亮的标志（恒为 0）会在下游 `proc` 优化中被常量折叠消掉。

---

### 4.3 循环展开与 UnrollLimitTracking

#### 4.3.1 概念说明

`for`/`while`/`foreach` 在 sv-elab 里没有「运行时循环」，只有「编译期展开」：用一个 `while (true)` 的 C++ 循环，每次复制一份循环体，直到循环条件不再成立或被 `break`。问题是，如果循环条件依赖运行时信号（比如 `while (!sample && idx != N)` 里 `sample` 来自输入），每一轮复制都是一个带条件的分支，理论上可能要展开无数轮。sv-elab 必须设一个上限，超过就报错放弃——这就是 `UnrollLimitTracking`。

它属于 `ProceduralContext`（[src/slang_frontend.h:249](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L249)），限额取自 `SynthesisSettings::unroll_limit()`，默认 4000（u2-l3）。

#### 4.3.2 核心流程

`UnrollLimitTracking` 维护一个**全局计数器** `unroll_counter`，统计本次过程块内所有循环**合计**展开了多少轮；以及一个 `loops` 集合，记录哪些循环语句参与过展开（用于在报错时附注）。它有三个关键操作：

- `enter_unrolling()` / `exit_unrolling()`：成对调用，标记「现在进入/离开一个循环的展开区域」。`unrolling` 是个深度计数器，只有从 0 变 1（最外层循环进入）时才重置计数器与 `loops`，保证**整个过程块共享一个计数池**而非每个循环独立 4000。
- `unroll_tick(stmt)`：每展开一轮调用一次。`++unroll_counter`，若超过 `limit` 就发 `diag::UnrollLimitExhausted` 并把 `error_issued` 置真，此后 `unroll_tick` 立即返回 `false` 让调用方停止展开。

展开轮次上限的判定可写成：

\[
\text{继续展开} \iff \text{unroll\_counter} < \text{limit}
\]

一旦 \(\text{unroll\_counter} \geq \text{limit}\)，报错并停止。

#### 4.3.3 源码精读

类声明与成员：

[src/slang_frontend.h:207-222](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L207-L222) —— `unrolling` 深度计数器、`unroll_counter` 全局计数、`loops` 集合、`error_issued` 一次性报错标志。

构造时从 settings 取限额：

[src/procedural.cc:496-498](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L496-L498) 以及 [src/procedural.cc:112-118](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L112-L118) —— `ProceduralContext` 构造时 `unroll_limit(netlist, netlist.settings.unroll_limit())`，把限额固化下来。

进入/退出展开区域：

[src/procedural.cc:505-518](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L505-L518) —— `enter_unrolling` 用 `if (!unrolling++)` 判断「是否首次进入」，是则重置 `unroll_counter=0`、`error_issued=false`、清空 `loops`。这意味着嵌套循环共享外层启动的计数会话。

核心计数与报错：

[src/procedural.cc:520-540](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L520-L540) —— `unroll_tick`：若已报错直接返回 `false`；否则把当前循环语句加入 `loops`，`++unroll_counter`，超限时发 `diag::UnrollLimitExhausted`（附 `limit` 值），并为 `loops` 里其它循环各附一条 `diag::NoteLoopContributes` 注解，告诉用户「还有这些循环一起吃掉了配额」，最后 `error_issued=true` 返回 `false`。

诊断文案：

[src/diag.cc:193-197](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L193-L197) —— `UnrollLimitExhausted` 文案 `"unroll limit of {} exhausted [--unroll-limit=]"`，`NoteLoopContributes` 文案 `"loop contributes to unroll tally"`，二者分别定为 Error 与 Note。

调用点（以 `for` 为例）：

[src/statements.h:537-576](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L537-L576) —— `unroll_limit.enter_unrolling()` 包住整个 `while(true)`；每轮末尾 `if (!unroll_limit.unroll_tick(&stmt)) break;` 既计数又在超限时停止展开；最后 `unroll_limit.exit_unrolling()`。

另外，`for` 循环若根本**没有停止条件**，根本无从展开，sv-elab 提前发另一个诊断：

[src/statements.h:529-532](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L529-L532) 配合 [src/diag.cc:177-178](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L177-L178) —— `MissingStopCondition`：`"stop condition is missing; loop cannot be unrolled"`。

#### 4.3.4 代码实践

**实践目标**：复现「展开限额耗尽」的诊断，并理解其附注。

**操作步骤**：

1. 准备一个 `always_comb` 里的 `for` 循环，循环次数由一个**运行时输入**控制且没有静态上界，例如 `for (int i = 0; i < span; i++) mask[i] = 1;`，其中 `span` 是 3 位输入（参考 [tests/unit/break.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/break.sv) 末尾 `test_break10` 的写法）。
2. 用一个很小的限额运行：`read_slang -unroll-limit 4 your_file.sv`（`--unroll-limit` 是 u2-l3 讲过的 `SynthesisSettings` 选项）。
3. 观察输出。

**需要观察的现象**：sv-elab 会展开若干轮（每一轮带一个 `i < span` 的条件分支），当展开轮数达到 4 时，输出 `unroll limit of 4 exhausted [--unroll-limit=]` 的 Error，并停止继续展开。若文件里有多个循环，还会看到 `loop contributes to unroll tally` 的 Note 指向其它循环。

**预期结果**：错误信息里 `{}` 被填成你传入的限额值 `4`，提示用户用 `--unroll-limit=` 提高上限。若把限额调高到能覆盖 `span` 的最大值（如 8），则展开成功且无诊断。**待本地验证**（取决于本地是否已编译好 `read_slang` 插件）。

#### 4.3.5 小练习与答案

**练习 1**：两个嵌套的 `for` 循环，外层 10 次、内层 10 次，默认限额 4000，会触发 `UnrollLimitExhausted` 吗？

**参考答案**：不会。合计展开 \(10 \times 10 = 100\) 轮，远小于 4000。注意是「合计」计数，因为 `enter_unrolling` 只在最外层进入时重置计数器，嵌套循环共享同一个 `unroll_counter`。

**练习 2**：为什么 `unroll_tick` 要用 `error_issued` 保证只报一次错？

**参考答案**：超限后展开循环不会立刻退出，还要把已压栈的守卫、`SwitchHelper` 等收尾；若每轮都报一次，同一个循环会刷出几十条相同错误，淹没真正的诊断。`error_issued` 让后续 `unroll_tick` 静默返回 `false`，既停止展开又只报一条。

---

### 4.4 函数调用与 EnterAutomaticScopeGuard

#### 4.4.1 概念说明

函数（`SubroutineSymbol`）是带「自动（automatic）」局部变量的逃逸构造。它有两点比普通循环更复杂：

1. **可重入**：一个函数可能被递归调用，或在一个 `always` 里被多次调用；每次调用都有自己的局部变量实例。sv-elab 用 `EvalContext::scope_nest_level` 给每个函数作用域记一个「嵌套层级」，配合 `Variable::from_symbol` 里的 `depth` 字段（u3-l3），让不同层级的同名局部变量映射到不同的 `Variable`。
2. **返回值**：函数体里遇到 `return expr`，要把 `expr` 赋给返回值变量，再逃逸出整个函数体。

`EnterAutomaticScopeGuard` 就是用来在进入一个「含自动变量的作用域」时把 `scope_nest_level` 加一、离开时减一的 RAII 守卫。它和 `RegisterEscapeConstructGuard(FunctionBody)` 一起，构成了函数调用的完整脚手架。

#### 4.4.2 核心流程

函数调用的大致流程（在 `StatementExecutor::handle_call` 里）：

```
1. 求值输入实参 arg_in
2. EnterAutomaticScopeGuard(eval, subroutine)     // scope_nest_level++，隔离本次调用的自动变量
3. 访问形参/局部变量声明，给它们赋初值
4. 把 arg_in 赋给输入形参
5. RegisterEscapeConstructGuard(FunctionBody)      // 压逃逸栈，return 通过它退出
6. 访问函数体（body）—— body 里的 return 会 signal_escape(FunctionBody)
7. 读返回值变量 returnValVar 的值作为 ret
8. （守卫析构：弹逃逸栈、scope_nest_level--）
9. 把输出形参的最终值写回调用处的实参
```

`return` 的处理见 [src/statements.h:727-740](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L727-L740)：先 `get_current_subroutine()` 找到最近的函数体帧，若有返回表达式就赋给 `returnValVar`，再 `signal_escape(FunctionBody)` 点亮从栈顶到函数体帧的所有标志。

#### 4.4.3 源码精读

`EnterAutomaticScopeGuard` 类声明：

[src/slang_frontend.h:198-205](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L198-L205) —— 持有 `EvalContext` 与 `ast::Scope*`。

构造与析构：

[src/procedural.cc:37-53](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L37-L53) —— 构造时 `++context.scope_nest_level[scope]`；析构时减一，归零则 `erase` 掉该项保持字典干净。`scope` 为空时是 no-op。

`scope_nest_level` 的用途在 `EvalContext` 里：

[src/slang_frontend.h:152-155](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L152-L155) —— 注释写明「隔离可重入作用域（即函数）的自动变量」。

函数调用的完整实现：

[src/statements.h:251-318](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L251-L318) —— `handle_call`。注意第 [274](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L274) 行的 `EnterAutomaticScopeGuard scope_guard(eval, subroutine)` 与第 [292-295](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L292-L295) 行的 `RegisterEscapeConstructGuard escape_guard(context, FunctionBody, subroutine)` 把函数体访问包起来；返回值在第 [305-306](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L305-L306) 行通过 `substitute_rvalue(returnValVar)` 取出。

`return` 语句本身：

[src/statements.h:727-740](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L727-L740) —— 取当前函数，把返回表达式赋给 `returnValVar`，然后 `signal_escape(FunctionBody)`。注意它会点亮函数体内所有更内层的帧（比如函数里嵌套的循环），从而让 `return` 真正「跳出一切」。

辅助查询 `get_current_subroutine`：

[src/procedural.cc:98-105](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L98-L105) —— 从栈顶向下找第一个 `FunctionBody` 帧，返回其 `subroutine`；找不到则 `log_abort()`（即 `return` 写在了函数外）。

#### 4.4.4 代码实践

**实践目标**：理解函数体的逃逸栈帧与 `return` 的配合。

**操作步骤**：

1. 阅读 [tests/unit/break_return.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/break_return.sv) 的 `test_return_from_loop`：函数里有个 `for` 循环，循环里 `if (i > 5) return i * 3;`。
2. 想象执行到 `return` 时的逃逸栈（自底向上）：`[FunctionBody, Loop(guard1), LoopBody(guard2)]`。
3. `return` 调用 `signal_escape(FunctionBody)`：从 `LoopBody` 开始，点亮 `LoopBody`、`Loop`、`FunctionBody` 三个标志（一路点到匹配的 `FunctionBody`）。
4. 于是本次展开里 `return` 之后的语句被跳过（`LoopBody` 标志已亮），`for` 不再展开下一轮（`Loop` 标志已亮），函数体后续语句也被跳过（`FunctionBody` 标志已亮）——完全符合「函数返回」语义。

**需要观察的现象**：`return` 一次性点亮三层标志，把循环展开和函数体剩余语句统统短路。

**预期结果**：函数等价于「找第一个大于 5 的 `i`，返回 `3*i`」，即 \(i=6\) 时返回 18（与 [break_return.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/break_return.sv) 第 31 行的断言 `f() == 18` 一致）。

#### 4.4.5 小练习与答案

**练习 1**：函数 `f` 调用函数 `g`，`g` 里 `return` 时，`f` 的语句会被跳过吗？

**参考答案**：不会。`g` 的 `return` 只点亮从栈顶到 `g` 的 `FunctionBody` 帧之间的标志；`f` 的 `FunctionBody` 帧在更下方，不在扫描范围内。`handle_call` 在 `g` 返回后（守卫析构弹掉 `g` 的帧）继续在 `f` 的作用域里执行，`f` 的逃逸标志未被动过。

**练习 2**：为什么 `handle_call` 里要先 `EnterAutomaticScopeGuard` 再 `RegisterEscapeConstructGuard`，而不是反过来？

**参考答案**：作用域层级（`scope_nest_level`）要在访问形参/局部变量声明**之前**就建立好，这样声明出的自动变量才带正确的 `depth`，能和同函数其它层级实例区分；而逃逸帧只要在访问函数体（可能含 `return`）之前建立即可。两者的析构顺序由 C++ 栈上对象自动保证（后构造先析构），无需手动管理。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一个完整的「源码追踪」任务。

**任务**：给定下面这段 SystemVerilog（综合自本讲各处例子），手动推演 sv-elab 的处理过程，并对照源码验证每一步。

```systemverilog
module m #(parameter integer W = 8) (
    input  logic [W-1:0] bits,
    output logic [$clog2(W)-1:0] idx
);
    function automatic logic [$clog2(W)-1:0] first_one(logic [W-1:0] v);
        for (int i = 0; i < W; i++) begin
            if (v[i]) return i;
        end
        return '0;
    endfunction

    always_comb idx = first_one(bits);
endmodule
```

请完成：

1. **逃逸栈演变**：从 `always_comb` 进入、调用 `first_one`、进入函数体、展开 `for` 循环、遇到 `return`，逐步画出 `escape_stack` 的内容（每一步栈里有哪些帧、种类是什么）。对照 [src/procedural.cc:55-110](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L55-L110)。
2. **标志点亮**：`return i` 触发 `signal_escape(FunctionBody)` 时，点亮了哪几个标志？参考 4.4.4 的分析。
3. **展开计数**：`for` 循环最多展开 8 轮（`W=8`），整个过程 `unroll_counter` 增加多少？是否触及默认 4000 上限？对照 [src/procedural.cc:520-540](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L520-L540)。
4. **作用域隔离**：若把 `first_one` 改成递归调用，`EnterAutomaticScopeGuard` 如何保证每层的 `i` 互不干扰？对照 [src/procedural.cc:37-53](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L37-L53) 与 u3-l3 的 `depth` 字段。
5. **网表预期**：最终 `idx` 应当是 `bits` 的优先编码器（最低位 1 的下标）。可用 `read_slang` 加载后 `proc; opt; show` 观察，**待本地验证**。

这个任务把「逃逸标志变量」「作用域守卫」「循环展开计数」「函数返回」四件事在一小段代码里全部触发，做完即可确认你已掌握本讲。

## 6. 本讲小结

- sv-elab 没有真正的跳转指令；`break`/`continue`/`return` 统一被建模成「点亮一个 1 位逃逸标志变量」，由 `Variable::escape_flag` 生成、`EscapeFrame` 在 `escape_stack` 上记录。
- `signal_escape` 从栈顶向栈底**逐层点亮**标志直到匹配的 `EscapeConstructKind`：`break` 点亮内层 `LoopBody` + `Loop`，`continue` 只点亮 `LoopBody`，`return` 一路点亮到 `FunctionBody`。
- `RegisterEscapeConstructGuard` 是 RAII 守卫，构造压栈、赋标志初值 0，析构弹栈；`get_disable_flag` 取栈顶标志，供 `StatementList` 在每条语句前 gating「是否已逃逸」。
- 循环靠编译期 `while(true)` 展开，每轮复制一份带条件分支的循环体；`UnrollLimitTracking` 用一个**过程块共享**的全局计数器限额（默认 4000），超限发 `UnrollLimitExhausted` 并附 `NoteLoopContributes` 注解，且只报一次。
- `for` 无停止条件时发 `MissingStopCondition`；函数调用用 `EnterAutomaticScopeGuard` 维护 `scope_nest_level` 以隔离可重入自动变量，用 `RegisterEscapeConstructGuard(FunctionBody)` 支持 `return`，返回值经 `returnValVar` 传出。

## 7. 下一步学习建议

本讲把过程块内的控制流（循环与逃逸）讲完了。接下来可以：

- 进入 **u6-l1（TimingPatternInterpretor）** 与 **u6-l2（触发器发射）**：看 `always` 块在「组合 / 触发器 / initial」三种时序下如何被分类，本讲的 `for` 展开、`Case`/`Switch` 树都会在那里被消费成最终寄存器电路。
- 阅读 **u7-l1（存储器推断）**：循环里对数组下标的动态写入（如 `for + mem[i]=...`）如何触发存储器推断，与本讲的循环展开形成对照。
- 若想验证本讲理解，可回到 **u8-l1（测试体系）**，仿照 [tests/unit/break.sv](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/break.sv) 的等价性测试范式，自己加一个含 `continue` 与嵌套循环的最小用例。
