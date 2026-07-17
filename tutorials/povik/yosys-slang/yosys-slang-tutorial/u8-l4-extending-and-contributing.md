# 扩展开发：添加对新 SV 构造的支持

## 1. 本讲目标

sv-elab（原名 yosys-slang）只支持 SystemVerilog 的「可综合子集」，并非全部合法 SV。当你喂给它一个尚未支持的 SV 构造时，它要么优雅地报一条诊断，要么直接中止并打印一堆调试信息。本讲要回答三个问题：

1. 那个「直接中止 + 打印调试信息」的机制是如何实现的？我写代码时该如何复用它？
2. 当我想为一个新的 SV 构造（一种表达式、一条语句、一类符号）添加翻译支持时，应该改在哪里？
3. 把改动贡献回上游时，仓库对提交内容（尤其是 AI 生成内容）有什么规定？

学完本讲，你应该能够：

- 说出 `require` / `unimplemented` / `ast_invariant` / `ast_unreachable` 四个宏的区别与各自适用场景。
- 区分「软不支持的诊断路径」与「硬中止的 `unimplemented` 路径」，并为新构造选择正确的处理方式。
- 在 `EvalContext::operator()`（手写 `switch`）、`StatementExecutor`（slang ASTVisitor）、`PopulateNetlist`（slang ASTVisitor）三处扩展点中，准确判断该改哪一处、怎么改。
- 读懂 `docs/MAINTAINERS.md` 与 `README.md` 里的贡献约定与 AI 政策。

本讲是整个学习手册的收尾篇，承接 u5-l4（逃逸构造与循环展开），把前面所有讲义里出现的 `unimplemented(...)`、`require(...)` 调用统一起来讲解。

## 2. 前置知识

在动手前，请确认你已经理解以下概念（前面讲义已建立）：

- **slang AST 与 ASTVisitor**：slang 把 SystemVerilog 源码解析成一棵抽象语法树（AST），节点类型分属 `ast::Expression`、`ast::Statement`、`ast::Symbol`、`ast::TimingControl` 等大类，每大类下又有具体 kind（如 `ExpressionKind::BinaryOp`、`StatementKind::Conditional`）。slang 提供 CRTP（奇异递归模板）基类 `ast::ASTVisitor<Derived, Flags>`，会按节点 kind 自动分派到派生类的 `handle(const ast::具体类型 &)` 方法。这一点是本讲「扩展点」的基础，不熟悉的读者建议先回看 u3-l1、u5-l2。
- **RTLIL**：Yosys 的中间表示，sv-elab 的最终产物。翻译一个 SV 构造，本质就是「读 AST 节点 → 调 `RTLILBuilder` 在画布上建线/建单元」（见 u3-l2）。
- **诊断系统**：`NetlistContext::add_diag(...)` 会把诊断「先攒起来，报告期再报」，不立即中止（见 u2-l4）。这与本讲的 `unimplemented`「立即中止」形成对照。
- **等价性测试范式**：`read_slang` 造 gate 网表 + `read_rtlil`/`read_verilog` 造 gold 网表 + `equiv_make`/`equiv_induct`/`equiv_status -assert` 做证明（见 u8-l1）。

一个贯穿全讲的关键认知：**sv-elab 的翻译器是「防御式」的**——它在大量内部位置假设「当前遇到的 AST 一定属于已支持的形态」，一旦假设被打破就用 `require`/`unimplemented` 当场炸开并把现场dump 出来。这种风格决定了「加新构造」的工作流。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/abort_helpers.cc` | 实现 `unimplemented_`（AST 序列化 + 源码行打印 + `log_error` 中止）与 `wire_missing_`。 |
| `src/slang_frontend.h` | 声明 `unimplemented_`/`wire_missing_` 四个重载，并定义 `require`/`unimplemented`/`ast_invariant`/`ast_unreachable`/`wire_missing` 五个宏；同时声明 `EvalContext`、`ProceduralContext`、`NetlistContext` 等中枢类型。 |
| `src/slang_frontend.cc` | 三大扩展点中的两处：`EvalContext::operator()`（手写 `switch` 分派表达式）与 `PopulateNetlist`（ASTVisitor 分派符号）。 |
| `src/statements.h` | 第三处扩展点：`StatementExecutor`（ASTVisitor 分派过程块语句）。 |
| `docs/MAINTAINERS.md` | 维护者备忘：合并提交信息的书写模板。 |
| `README.md` | 项目说明，其中「Contributing」与「AI policy」小节规定了贡献流程与 AI 政策（注意：AI 政策在这里，不在 MAINTAINERS.md）。 |
| `tests/unit/dff.ys`、`tests/run.sh` | 等价性测试的最小范例与红绿跑测脚本，给「补一个测试」这一步提供模板。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先讲防御式编程三件套（abort_helpers），再讲三大扩展点的分派结构（扩展点），最后把两者串成「从触发到可合并改动」的完整工作流（贡献流程）。

### 4.1 防御式编程三件套：require / unimplemented / ast_invariant

#### 4.1.1 概念说明

sv-elab 在遍历 slang AST 时，会在很多内部位置做出「这里的 AST 一定长成某样」的假设。比如「这个转换一定是从整数类型到整数类型」「这个 case 语句的条件一定是单个表达式」。这些假设如果被打破，说明遇到了一个尚未实现（或根本不可综合）的 SV 构造。

项目提供一组统一的宏来表达这类假设，它们一旦失败就**立即中止整个 Yosys 进程**，并在中止前把出错 AST 节点序列化成 JSON、把对应的源码行打印出来。这种「快失败 + 现场丰富」的设计，让你在开发期能立刻定位是哪个 SV 构造没被支持、它的 AST 长什么样。

四个宏的区别：

| 宏 | 含义 | 典型用途 |
| --- | --- | --- |
| `require(obj, property)` | 断言 `property` 为真，否则中止并把 `property` 字符串化后带进错误信息 | 「这个节点应当满足某个条件，否则就是尚未支持的形态」 |
| `ast_invariant(obj, property)` | `require` 的别名，语义同上 | 强调这是「AST 理应恒成立的不变量」（slang 已经保证），被打脸通常是 sv-elab 的 bug |
| `unimplemented(obj)` | 无条件中止 | 「这种 kind 我压根没处理，直接炸」 |
| `ast_unreachable(obj)` | `unimplemented` 的别名 | 强调「按不变量这里不该走到」 |

此外还有一个相关的 `wire_missing(netlist, symbol)`，用于「翻译到一半发现某个符号对应的线没建出来」这种内部错误，它走单独的 `wire_missing_` 实现，打印出 realm、参数、重映射作用域等更偏「网表构建现场」的信息。

> 注意区分：`require`/`unimplemented` 是**硬中止**（`log_error`，整个 Yosys 退出）；而 u2-l4 讲的 `netlist.add_diag(diag::XXX)` 是**软诊断**（记进队列，继续翻译，最后统一上报）。给新构造加支持时，要选对路径——后面 4.2 会专门讲。

#### 4.1.2 核心流程

调用任一宏时，控制流最终落到 `abort_helpers.cc` 里的 `unimplemented__` 模板函数，它做四件事：

```text
unimplemented__(obj, file, line, condition)
  ├── 用 slang 的 ASTSerializer 把 obj 序列化成 JSON，打到 stdout
  ├── 取出 obj 的 SourceRange，定位到源码文件名、行号、列号
  ├── 把出错那一整行源码文本也打到 stdout
  └── log_error("Feature unimplemented at <file>:<line> ... (failed condition \"<condition>\")")
        → [[noreturn]]，Yosys 进程退出
```

关键点：

- `condition` 参数：`require` 把断言表达式用 `#property` 字符串化传入；`unimplemented` 传 `NULL`。所以错误信息里你能直接看到「是哪个条件挂了」。
- `[[noreturn]]` 标注：编译器知道这些函数不返回，因此调用点之后的代码不需要写 `else`，也不会收到「控制流穿过非 void 函数」之类的警告。这正是源码里大量 `require(...); return xxx;` 紧挨着写却安全的原因。
- 四个重载（`Symbol`/`Expression`/`Statement`/`TimingControl`）只是为了让不同 AST 大类都能取到各自的 `sourceRange`，实现都转调同一个模板。

#### 4.1.3 源码精读

先看宏定义与函数声明，全部集中在 `slang_frontend.h` 的 `// abort_helpers.cc` 注释块下：

[src/slang_frontend.h:640-651](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L640-L651) —— 声明四个 `unimplemented_` 重载与 `wire_missing_`，并定义五个宏。要点：`require` 用 `#property` 字符串化断言、`unimplemented` 传 `NULL`、`ast_invariant` 与 `ast_unreachable` 只是别名。

```c
[[noreturn]] void unimplemented_(const ast::Symbol &obj, const char *file, int line, const char *condition);
[[noreturn]] void unimplemented_(const ast::Expression &obj, const char *file, int line, const char *condition);
[[noreturn]] void unimplemented_(const ast::Statement &obj, const char *file, int line, const char *condition);
[[noreturn]] void unimplemented_(const ast::TimingControl &obj, const char *file, int line, const char *condition);
#define require(obj, property) { if (!(property)) unimplemented_(obj, __FILE__, __LINE__, #property); }
#define unimplemented(obj) { slang_frontend::unimplemented_(obj, __FILE__, __LINE__, NULL); }
#define ast_invariant(obj, property) require(obj, property)
#define ast_unreachable(obj) unimplemented(obj)
```

再看实现。`unimplemented__` 是真正干活的模板，四个公开重载都转调它：

[src/abort_helpers.cc:46-74](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/abort_helpers.cc#L46-L74) —— 用 `ASTSerializer` 把出错节点序列化成 JSON 打到 `std::cout`，再从 `SourceManager` 取出源码文件名、行号、列号和那一行原文打印，最后 `log_error` 中止。注意它依赖外部全局 `global_compilation`（声明见 `extern ast::Compilation *global_compilation;`）来拿到 ASTSerializer 与 SourceManager。

```c
template <typename T>
[[noreturn]] void unimplemented__(const T &obj, const char *file, int line, const char *condition)
{
    slang::JsonWriter writer;
    writer.setPrettyPrint(true);
    ast::ASTSerializer serializer(*global_compilation, writer);
    serializer.serialize(obj);
    std::cout << writer.view() << std::endl;
    // ... 取出 obj 的 SourceRange，打印 "Source line <file>:<line>:<col>: <源码原文>" ...
    log_error("Feature unimplemented at %s:%d, see AST and code line dump above%s%s%s\n", file,
            line, condition ? " (failed condition \"" : "", condition ? condition : "",
            condition ? "\")" : "");
}
```

`wire_missing_` 走的是另一套现场：它打印的是「网表构建」现场而非 AST 现场——HDL 实例层次路径、模块名、参数取值、缺失线的符号层次路径、`scopes_remap` 表，最后 `log_error("Internal frontend error ...")`：

[src/abort_helpers.cc:100-133](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/abort_helpers.cc#L100-L133) —— 翻译过程中某符号的线缺失时调用，打印 realm、参数、`scopes_remap`，提示「这是 sv-elab 内部错误，不是用户 SV 不可综合」。

最后看几个真实的、贯穿前面讲义的用法，体会它们的语义差异：

- `require(call, !call.isSystemCall())`（[src/statements.h:255](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L255)）：过程块里的函数调用只处理用户函数，遇到系统调用 `$xxx` 就炸，错误信息会带 `(failed condition "!call.isSystemCall()")`。
- `ast_invariant(expr, expr.kind != ast::ExpressionKind::Streaming)`（[src/slang_frontend.cc:1217](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1217)）：求值器主入口断言「流拼接不会走到这里」（流拼接由 `EvalContext::streaming()` 单独处理，见 u4-l1），用 `ast_invariant` 强调这是「本不该发生」。
- `unimplemented(conv)`（[src/slang_frontend.cc:799](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L799)）：类型转换既非「整数→整数」也非「位流→位流」时，直接 `unimplemented`，错误信息里没有 condition 字段。

#### 4.1.4 代码实践

**实践目标**：亲眼看到一次 `unimplemented` 触发时的完整输出，建立「这个机制到底吐什么」的直觉。

**操作步骤**（源码阅读 + 可选运行）：

1. 打开 [src/abort_helpers.cc:46-74](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/abort_helpers.cc#L46-L74)，确认它先打 JSON、再打源码行、最后 `log_error`。
2. 在 [src/slang_frontend.cc:785-800](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L785-L800) 的 `apply_conversion` 里，确认只有当转换「既非 integral→integral、也非 bitstream→bitstream」时才会落到 `unimplemented(conv)`（第 799 行）。
3. （可选，待本地验证）如果你已按 u8-l3 构建 `build/slang.so`，试着写一段会触发该分支的 SV（例如涉及非 integral、非 bitstream 的类型转换），用 `yosys -m build/slang.so -p "read_slang <<'EOF' ... EOF"` 跑一下，观察 stdout 上的 AST JSON、源码行，以及最后的 `Feature unimplemented at ...` 错误行。

**需要观察的现象**：进程非零退出；stdout 出现一段以 `"kind"` 开头的 JSON（出错节点的 AST）；紧接着一行 `Source line <file>:<line>:<col>: <源码原文>`；最后 Yosys 日志里有一条 `Feature unimplemented at src/slang_frontend.cc:799`。

**预期结果**：能从输出里同时读出「用户 SV 的哪一行」「它对应的 slang AST 节点长什么样」「sv-elab 哪个源文件的哪一行炸了」三件事——这正是 4.3 节「定位触发点」要用的全部信息。

> 若你无法本地构建或构造不出触发用例，可只完成步骤 1–2 的源码阅读，不影响后续理解。本实践不强求运行成功。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `require(obj, property)` 后面经常紧跟 `return xxx;` 却不需要包在 `else` 里？
**答案**：因为 `unimplemented_` 被标注为 `[[noreturn]]`，编译器知道 `require` 失败时控制流不会继续，因此后面的 `return` 只在 `require` 成功时执行，逻辑上等价于包在 `else` 中，且不会有「控制流穿过非 void 函数」警告。

**练习 2**：`ast_invariant` 与 `require` 在源码层面完全相同，为什么还要起两个名字？
**答案**：纯语义提示。`ast_invariant` 告诉读者「这条是 slang 语义本应保证的不变量，若失败多半是 sv-elab 自己的 bug」；`require` 更偏「这是 sv-elab 对可综合性做的限制，失败代表遇到了尚未支持的构造」。两者实现一致，但读代码时能快速判断该去查 slang 还是查 sv-elab。

**练习 3**：`wire_missing` 和 `unimplemented` 打印的「现场」有何不同？
**答案**：`unimplemented` 打 AST 现场（节点 JSON + 源码行），面向「这个 SV 构造没实现」；`wire_missing` 打网表现场（realm 层次路径、参数取值、`scopes_remap` 表），面向「翻译到一半某根线没建出来」的 sv-elab 内部错误。

### 4.2 扩展点：三大 ASTVisitor 分派器

#### 4.2.1 概念说明

sv-elab 把 slang AST 翻译成 RTLIL 的全部工作，分布在三个「分派器」里，它们就是本讲的三大扩展点：

1. **`EvalContext::operator()`**——把表达式（`ast::Expression`）求值成 `RTLIL::SigSpec`（u4-l1）。
2. **`StatementExecutor`**——遍历过程块语句（`ast::Statement`），长成 HDL 意图 case 树并更新变量状态（u5-l2）。
3. **`PopulateNetlist`**——遍历模块里的符号（`ast::Symbol`），建模块、端口、线、实例、过程块等（u3-l1）。

这三者的分派机制有一个**关键差异**，决定了你「加新构造」时要改的语法：

- `EvalContext::operator()` 用的是**手写 `switch (expr.kind)`**。加一种新表达式 → 加一个 `case`。它的兜底 `default` 是**软诊断**（`add_diag(diag::LangFeatureUnsupported)`，返回 `Sx`），不是硬中止。
- `StatementExecutor` 与 `PopulateNetlist` 用的是 **slang 的 CRTP `ASTVisitor`**。加一种新语句/符号 → 给派生类新增一个 `void handle(const ast::具体类型 &)` 方法，slang 会自动按 kind 分派过来。它们的兜底分两种：基类大类的兜底（如 `handle(const ast::Statement &)`、`handle(const ast::Symbol &)`）。

记住这条判断规则：**表达式走 `EvalContext` 的 `switch`；语句走 `StatementExecutor` 的 `handle`；符号走 `PopulateNetlist` 的 `handle`。**

#### 4.2.2 核心流程

两个 ASTVisitor 的分派由 slang 的 `ast::ASTVisitor<Derived, Flags>` 模板自动生成：当你调用 `node.visit(visitor)` 时，它按节点的具体 kind，尽量调用派生类里**最具体**的 `handle(const ast::具体类型 &)` 重载；如果派生类没有对应具体类型的重载，就回退到大类的 `handle(const ast::Expression&)` / `handle(const ast::Statement&)` / `handle(const ast::Symbol&)`。

```text
新增对「新语句种类 FooStatement」的支持：
  └── 在 StatementExecutor 里新增 void handle(const ast::FooStatement &stmt) { ... }
        slang ASTVisitor 自动把 FooStatement 节点分派到这里，不再落到兜底

新增对「新表达式 kind Foo」的支持：
  └── 在 EvalContext::operator() 的 switch(expr.kind) 里新增
        case ast::ExpressionKind::Foo: { ... ret = ...; } break;
        若不加，会落到 default → 软诊断 LangFeatureUnsupported（返回 Sx）
```

三个兜底（fallback）的处理策略各不相同，需要分清：

| 分派器 | 兜底（base）handle | 兜底行为 | 含义 |
| --- | --- | --- | --- |
| `EvalContext::operator()` | `switch` 的 `default` | `add_diag(LangFeatureUnsupported)` 后返回 `Sx` | 软诊断，不中止 |
| `StatementExecutor` | `handle(const ast::Statement&)` | `add_diag(LangFeatureUnsupported)` | 软诊断，不中止 |
| `StatementExecutor` | `handle(const ast::Expression&)` | `unimplemented(expr)` | 硬中止（表达式不是语句） |
| `PopulateNetlist` | `handle(const ast::Symbol&)` | `unimplemented(sym)` | 硬中止 |

可以看到 `PopulateNetlist` 对「未知符号」的态度最严厉（直接炸），而表达式/语句的未知 kind 则是「软」的——这是因为模块体里出现一个未知符号通常意味着 sv-elab 漏了一整类成员，宁可炸开让开发者立刻看见；而表达式/语句的种类太多，软诊断能让其余部分的设计继续翻译完。

> 给新构造加支持时的第一个决策：**你要的是「软诊断」还是「真正实现」？** 若该构造不可综合，应新增一个诊断码（u2-l4，五处对齐）并在兜底前显式 `add_diag`；若该构造可综合，应在对应分派器里新增 `case`/`handle`。

#### 4.2.3 源码精读

**扩展点一：`EvalContext::operator()`**（手写 switch）。

[src/slang_frontend.cc:1205-1217](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1205-L1217) —— 表达式求值主入口：先用 `AttributeGuard` 绑定源码属性，对 untyped 类型直接 `unimplemented(expr)`（硬中止），再用 `ast_invariant` 排除 `Streaming`，然后进入下面的 `switch`。

[src/slang_frontend.cc:1242-1243](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1242-L1243) —— 大分派 `switch (expr.kind)` 的起点。每个 `case ast::ExpressionKind::XXX` 处理一类表达式（如 `BinaryOp`、`ElementSelect`、`Conversion`、`IntegerLiteral` 等）。

[src/slang_frontend.cc:1659-1661](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1659-L1661) —— `switch` 的 `default`，注意这里是**软诊断**而非硬中止：记一条 `diag::LangFeatureUnsupported` 后 `goto error`，最终返回 `Sx`（宽度与表达式位宽相同）。

```c
default:
    netlist.add_diag(diag::LangFeatureUnsupported, expr.sourceRange);
    goto error;
```

**扩展点二：`StatementExecutor`**（ASTVisitor）。

[src/statements.h:190](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L190) —— 类声明，继承 slang 的 CRTP 访问器 `ast::ASTVisitor<StatementExecutor, ast::VisitFlags::Statements>`。第一个模板参数是派生类自身（CRTP），slang 据此生成按 kind 分派到 `handle(...)` 的代码。

[src/statements.h:755-760](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L755-L760) —— 两个兜底：对未知语句种类 `handle(const ast::Statement&)` 走软诊断 `LangFeatureUnsupported`；对「不是语句的表达式」`handle(const ast::Expression&)` 走硬中止 `unimplemented(expr)`。加新语句支持 = 在这两行之前新增 `void handle(const ast::具体语句类型 &stmt)`。

```c
void handle(const ast::Statement &stmt)
{
    netlist.add_diag(diag::LangFeatureUnsupported, stmt.sourceRange.start());
}

void handle(const ast::Expression &expr) { unimplemented(expr); }
```

**扩展点三：`PopulateNetlist`**（ASTVisitor + TimingPatternInterpretor）。

[src/slang_frontend.cc:1766](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1766) —— 类声明，多重继承自 `TimingPatternInterpretor`（时序模式识别，u6-l1）与 `ast::ASTVisitor<PopulateNetlist, ast::VisitFlags::Statements>`。它有几十个 `handle(const ast::具体符号类型 &)` 方法（端口、实例、连续赋值、generate 块、过程块……）。

[src/slang_frontend.cc:2934-2937](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2934-L2937) —— 兜底 `handle(const ast::Symbol &sym)`，注意是**硬中止** `unimplemented(sym)`。加新符号支持 = 在此之前新增 `void handle(const ast::具体符号类型 &sym)`。注意上方一大批 `handle(...){}` 空实现（如 `Type`、`ParameterSymbol`、`SubroutineSymbol` 等），它们是「故意忽略」的符号种类，新增空 handle 也是「声明不支持但不报错」的一种合法手段。

```c
void handle(const ast::Symbol &sym)
{
    unimplemented(sym);
}
```

#### 4.2.4 代码实践

**实践目标**：在三个扩展点里，分别能说出「我要加一个新构造，改哪一行、加什么代码」。

**操作步骤**（纯源码阅读）：

1. 打开 [src/slang_frontend.cc:1242](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1242)，确认 `EvalContext` 是 `switch (expr.kind)`。假如要支持一种新的表达式 kind `Foo`，你会在这里加 `case ast::ExpressionKind::Foo:`，参考相邻 `case`（如第 1489 行的 `RangeSelect`）的写法：把具体类型 `expr.as<...>()` 取出、调 `RTLILBuilder` 造电路、把结果赋给 `ret`。
2. 打开 [src/statements.h:190](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L190)，确认 `StatementExecutor` 是 ASTVisitor。假如要支持一种新语句 `FooStatement`，你会新增 `void handle(const ast::FooStatement &stmt) { ... }`，参考第 371 行的 `handle(const ast::ConditionalStatement &)` 的写法：用 `SwitchHelper` 把分支挂进 case 树并维护 `vstate`。
3. 打开 [src/slang_frontend.cc:2934](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2934)，确认 `PopulateNetlist` 的兜底是硬中止。假如要支持一种新符号 `FooSymbol`，你会新增 `void handle(const ast::FooSymbol &sym) { ... }`，参考第 2098 行 `handle(const ast::PortSymbol &)` 的写法。

**需要观察的现象**：你能指出三个扩展点「分派机制不同」（switch vs ASTVisitor）、「兜底策略不同」（软诊断 vs 硬中止）、「新增语法不同」（加 case vs 加 handle 方法）。

**预期结果**：面对任意一个「未支持的 SV 构造」，你能先判断它属于表达式、语句还是符号，从而直接定位到该改哪一处。

#### 4.2.5 小练习与答案

**练习 1**：用户写了一个 sv-elab 不认识的语句种类，运行时没有崩溃，只是最后报告里多了一条诊断。为什么？
**答案**：因为 `StatementExecutor` 的兜底 `handle(const ast::Statement&)`（[src/statements.h:755](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L755)）走的是软诊断 `add_diag(LangFeatureUnsupported)`，不会中止；翻译继续，诊断在报告期统一上报。

**练习 2**：同样是不认识的种类，为什么 `PopulateNetlist` 遇到未知符号会直接崩溃，而 `EvalContext` 遇到未知表达式不会？
**答案**：`PopulateNetlist` 的兜底 `handle(const ast::Symbol&)` 是硬中止 `unimplemented(sym)`，因为模块体里出现未知符号通常意味着漏掉一整类成员，开发者应立刻看见；`EvalContext` 的 `switch ... default` 是软诊断，让表达式种类过多时其余设计仍能翻译完。这是项目有意的取舍。

**练习 3**：`PopulateNetlist` 里有大量 `handle(const ast::Type&) {}` 这样的空实现，它们存在的意义是什么？
**答案**：它们是「故意忽略」的符号种类——声明 sv-elab 已知这类符号存在、但不需要为它在网表里生成任何东西，从而避免落到兜底 `handle(const ast::Symbol&)` 的硬中止。新增一个空 handle 也是「声明不支持但静默跳过」的合法手段（当你确认某符号可安全忽略时）。

### 4.3 从 unimplemented 到一个可合并的改动（贡献流程）

#### 4.3.1 概念说明

前两节给出了「工具」（防御式宏）和「位置」（三大扩展点）。本节把它们串成一条完整的贡献工作流：当你在实际使用中撞到一个 `unimplemented`，从「定位触发点」到「新增 handle」再到「补一个等价性测试」，最后「提交」。

这条工作流是：

```text
1. 复现：用最小 SV 用例触发 unimplemented，读 dump 拿到「用户源码行 + AST JSON + 触发点源文件:行」
2. 定位：根据「触发点」落到哪个文件，判断属于表达式/语句/符号 → 确定该改哪个扩展点
3. 判定可综合性：该构造可综合吗？
     ├─ 不可综合 → 新增诊断码（u2-l4 五处对齐），在兜底前 add_diag（软路径）
     └─ 可综合   → 在对应扩展点新增 case / handle（硬实现）
4. 实现：取出 slang AST 节点 → 调 RTLILBuilder 造线/单元 → 把结果塞进 ret（表达式）/ case 树（语句）/ 画布（符号）
5. 测试：在 tests/ 下补一个等价性用例（gold vs gate + equiv_induct）
6. 提交：遵循 docs/MAINTAINERS.md 的提交信息习惯，并遵守 README.md 的 AI 政策
```

关于贡献约定，要分清两个文件的真实内容（**注意：AI 政策在 README.md，不在 MAINTAINERS.md**）：

- `docs/MAINTAINERS.md` 只记录了**合并提交信息的书写习惯**（merge commit 用 `Merge "<标题>" (#<PR号>)` + 一段贡献者来信摘录；squash commit 则把正文里的 commit 列表替换成来信摘录）。它面向项目维护者，不涉及 AI 政策。
- `README.md` 的「Contributing」与「AI policy」小节才是**贡献流程与 AI 政策**的权威来源：欢迎贡献，鼓励先沟通再动手；对 AI 生成内容有明确限制（见 4.3.3）。

#### 4.3.2 核心流程

实现一个新构造时，无论改哪个扩展点，都遵循同一「读 AST → 造 RTLIL」的微观模式：

```text
handle / case 内部：
  ├── (可选) require/ast_invariant 把已支持子形态外的部分挡回 unimplemented
  ├── 取出 slang 具体节点：expr.as<ast::具体类型>() / 直接用参数
  ├── 递归求值子节点：(*this)(sub_expr) 或 sub_stmt.visit(*this)
  ├── 调 RTLILBuilder 在 netlist.canvas 上建线/单元（u3-l2 五步模式）
  └── 把产物交回去：表达式赋给 ret；语句挂进 current_case / 更新 vstate；符号直接落到画布
```

「先把能处理的子形态挡在外圈」是这个代码库的典型防御风格——里层代码可以放心假设节点已经满足某些条件。4.1 里 `apply_conversion` 就是范例：它先用 `if (from.isIntegral() && to.isIntegral())` 与 `else if (...isBitstreamType())` 把已支持形态处理掉，剩下的兜底 `unimplemented(conv)`。

测试侧，sv-elab 用**等价性测试**证明新构造翻译正确（u8-l1）：写一个只含新构造的小模块作 gate（`read_slang`），手写或用 `read_verilog` 造一个语义等价的 gold 网表，再 `equiv_make`/`equiv_induct`/`equiv_status -assert`。`tests/run.sh` 会遍历所有 `tests/*/*.ys` 与 `*.tcl`，用 `slang.so` 插件跑、按退出码判红绿。

#### 4.3.3 源码精读

**实现范例：`apply_conversion` 的「分层兜底」写法。**

[src/slang_frontend.cc:785-800](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L785-L800) —— 类型转换的典型实现：先用两个 `if` 分支把「integral↔integral」「bitstream↔bitstream」两种已支持形态处理掉（中间还用 `require(conv, ...)` 把 bitstream 转换的宽度必须相等这一假设挡住），剩下的兜底 `unimplemented(conv)`。给新构造加支持时，应模仿这种「逐层窄化、最后兜底」的结构，而不是一开始就写一个能吃所有形态的大函数。

```c
if (from.isIntegral() && to.isIntegral()) {
    // ... 整数间转换：符号/零扩展 ...
} else if (from.isBitstreamType() && to.isBitstreamType()) {
    require(conv, from.getBitstreamWidth() == to.getBitstreamWidth());
    return op;
} else {
    unimplemented(conv);
}
```

**测试范例：`tests/unit/dff.ys` 的第一个用例。**

[src/tests/unit/dff.ys:1-44](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L1-L44) —— 等价性测试的标准骨架：`read_slang <<EOF ... EOF` 造 gate（含 `iff` 的 `always_ff`），`read_rtlil <<EOF ... EOF` 手写 gold 网表（`$add` + `$dffe`），`async2sync` → `equiv_make gold gate equiv` → `equiv_induct equiv` → `equiv_status -assert`。给新构造补测试时，照此骨架替换 SV 与 gold 即可。

**测试运行：`tests/run.sh`。**

[src/tests/run.sh:11-27](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/run.sh#L11-27) —— 遍历 `tests/*/*.ys` 与 `*.tcl`，用 `yosys -m build/slang.so` 跑每个脚本，按退出码判 OK/FAIL；不依赖 CMake，是 u8-l1 里 CTest 注册路径的互补红绿脚本。新测试只要按命名放到 `tests/unit/` 或 `tests/various/` 下就会被这个脚本自动拾取（CTest 侧则需在 `tests/CMakeLists.txt` 的 `ALL_TESTS` 里登记，见 u8-l1）。

**贡献约定与 AI 政策：`README.md`。**

[src/README.md:82-94](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L82-L94) —— 「Contributing」欢迎贡献并建议开发前先与维护者沟通确定方案；「AI policy」明确：**禁止向 Issues/PR/Discussions 提交 LLM 生成的代码与文本**（包括 PR 里的代码）；唯一例外是配合 bug 报告的、已最小化且去除多余 LLM 注释的复现用例；把 LLM 当作工作流中的工具（搜索/调试/测试/机械修改）是允许的，但**提交的代码应由人类基于对代码库的理解来编写，而非由 LLM 生成**；维护者本人保留绕过 PR 直接推送的选项。

[src/docs/MAINTAINERS.md:1-9](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/docs/MAINTAINERS.md#L1-L9) —— 维护者备忘，仅规定合并提交信息的书写模板（merge 用 `Merge "<标题>" (#<号>)` + 来信摘录；squash 用 GitHub 默认模板但把正文 commit 列表换成来信摘录）。**不包含 AI 政策**——AI 政策在上面的 README.md。

> 一个对学习者的现实提醒：本讲义由 AI 生成，正是 README「AI policy」所规制的对象。你若要把本讲提到的任何代码改动真正贡献回 sv-elab，需由你本人基于对源码的理解重写实现与测试，而不是直接复制本讲义里的示例片段去提 PR。

#### 4.3.4 代码实践

**实践目标**：把 4.1、4.2 串起来，完整走一遍「定位触发点 → 设计 handle → 补等价性测试」的设计流程（设计为主，不强求实现到能跑通）。

**操作步骤**：

1. **定位触发点**。考察 [src/slang_frontend.cc:799](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L799) 的 `unimplemented(conv)`：它在 `EvalContext::apply_conversion` 里，触发条件是「转换的源/目标类型既非 integral、也非 bitstream」。读 [src/slang_frontend.cc:785-800](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L785-L800) 确认这一点。注意它属于「表达式求值」范畴，所以该改的扩展点是 `EvalContext`（不是新增 handle，而是丰富这个函数的分支）。
2. **判定可综合性 + 设计改动**。设想你要放宽这个 `unimplemented`：先想清楚「源/目标都不是 integral 也不是 bitstream」还可能是哪些合法 SV 类型转换（例如某些涉及 `real` 或联合体的情况），判断它们是否可综合。若不可综合 → 新增一个诊断码并在 `else` 分支 `add_diag` 后返回 `Sx`（参考 4.2.3 里 `EvalContext` 的 `default` 软诊断写法）；若可综合 → 在 `else` 里新增一段「读 AST → 调 RTLILBuilder」的实现。写出这段设计（伪代码即可），模仿 `apply_conversion` 现有分支的结构。
3. **补等价性测试**。照 [src/tests/unit/dff.ys:1-44](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L1-L44) 的骨架，起草一个 `tests/unit/my_feature.ys`：`read_slang` 一个只含该新构造的 gate 模块，配一个语义等价的 gold 网表，再 `async2sync`/`equiv_make`/`equiv_induct`/`equiv_status -assert`。如果该构造当前还没有 gold 对照（因为正是要新加支持），可暂时只写 `read_slang` 的 gate 部分并在注释里说明「gold 待补」——这正是 `tests/unit/function_call.ys` 这类「先保证能翻译不报错」用例的形态。
4. （可选，待本地验证）若你已构建 `build/slang.so`，把改动应用后重建，运行 `bash tests/run.sh` 或单独 `yosys -m build/slang.so tests/unit/my_feature.ys`，观察是否从「崩溃/报错」变为「OK」。

**需要观察的现象**：你能产出三份产物——(a) 一句话说明触发点在哪、属于哪类扩展点；(b) 一段模仿现有风格的实现/诊断设计；(c) 一个符合等价性测试骨架的新 `.ys` 文件草稿。

**预期结果**：即便不实际合并代码，你已掌握 sv-elab「加新 SV 构造支持」的标准动作链。运行结果若无法本地复现，请标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：你在 `read_slang` 时撞到 `Feature unimplemented at src/slang_frontend.cc:2936`。这是哪个扩展点？你该新增什么？
**答案**：第 2936 行是 `PopulateNetlist::handle(const ast::Symbol&)` 的 `unimplemented(sym)`（[src/slang_frontend.cc:2934-2937](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2934-L2937)）。属于「符号」扩展点。你应根据 dump 里的 AST JSON 确定未知符号的具体类型，然后在 `PopulateNetlist` 里新增 `void handle(const ast::该符号类型 &sym)`（或在确认可忽略时新增一个空 handle）。

**练习 2**：为什么给新构造补测试要用「等价性测试」（gold vs gate），而不是像普通软件那样写断言？
**答案**：因为 sv-elab 的产物是 RTLIL 网表，「翻译正确」的最佳判据是「与一个已知正确的参考实现行为等价」。等价性测试用 `equiv_make`/`equiv_induct` 做 k-归纳证明，能自动覆盖所有输入取值，比手写若干组断言更可靠（详见 u8-l1）。

**练习 3**：一个贡献者用 LLM 辅助搜索定位了 bug、并在本地用 LLM 做了几处机械重命名，然后由自己手写实现并提交 PR。这违反 README 的 AI 政策吗？
**答案**：不违反。README 的 AI policy 明确「把 LLM 当工作流工具（搜索/调试/测试/机械修改）是允许的」，禁止的是「提交 LLM 生成的代码与文本」。只要 PR 里的实现与文字由人类基于自身理解编写即可（见 [src/README.md:82-94](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L82-L94)）。

## 5. 综合实践

**任务**：完整走一遍「为某个当前会触发 `unimplemented` 的 SV 构造，设计一个最小改动方案」。

1. **选目标**：从本讲出现的真实 `unimplemented` 站点中任选一个作为目标，例如：
   - [src/slang_frontend.cc:799](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L799) 的类型转换 `unimplemented(conv)`；
   - [src/slang_frontend.cc:2936](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2936) 的未知符号 `unimplemented(sym)`；
   - [src/statements.h:760](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L760) 的非语句表达式 `unimplemented(expr)`。
2. **定位**：写出它的触发条件、所属扩展点（表达式/语句/符号）、以及「该改 switch 还是新增 handle 方法」。
3. **设计实现**：模仿同文件里相邻分支/相邻 handle 的风格，写出一段「读 AST → 调 RTLILBuilder → 回交产物」的实现草稿；若你判定该构造不可综合，则改为「新增诊断码 + 在兜底前 `add_diag`」的设计（并按 u2-l4 列出五处对齐清单）。
4. **补测试**：照 [tests/unit/dff.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L1-L44) 的骨架，起草 `tests/unit/<your_feature>.ys`（gate + 可选 gold + `equiv_*` 三连）。
5. **自查提交合规**：确认你的实现与 PR 描述由你本人基于对源码的理解编写，符合 [README.md 的 AI 政策](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L82-L94)。

**验收标准**：能交出「触发点说明 + 扩展点判断 + 实现/诊断设计 + 测试草稿 + 合规自查」五份材料；运行结果若无法本地复现，明确标注「待本地验证」。

## 6. 本讲小结

- sv-elab 用四个宏做防御式编程：`require`/`ast_invariant`（断言某条件，失败带字符串化条件中止）、`unimplemented`/`ast_unreachable`（无条件中止），实现都在 [src/abort_helpers.cc:46-74](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/abort_helpers.cc#L46-L74)，中止前会 dump 出错节点的 AST JSON 与源码行；另有 `wire_missing` 处理「网表构建现场」类内部错误。
- 三个扩展点对应三类 AST 节点：`EvalContext::operator()` 的手写 `switch (expr.kind)` 管表达式（兜底软诊断）、`StatementExecutor`（ASTVisitor）管语句（语句兜底软诊断、表达式兜底硬中止）、`PopulateNetlist`（ASTVisitor）管符号（兜底硬中止）。加支持的语法分别是「加 case」与「加 `handle(const ast::具体类型&)` 方法」。
- 软诊断（`add_diag`）与硬中止（`unimplemented`）是两条不同路径：不可综合构造走软诊断（按 u2-l4 五处对齐新增诊断码），可综合构造走硬实现（在对应扩展点新增分支）。
- 实现新构造遵循「读 slang AST → 调 RTLILBuilder 造线/单元 → 回交产物」的微观模式，并模仿 `apply_conversion` 的「逐层窄化、最后兜底」防御写法。
- 正确性用等价性测试证明（gold vs gate + `equiv_induct`），骨架见 [tests/unit/dff.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L1-L44)，红绿脚本见 [tests/run.sh](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/run.sh#L11-L27)。
- 贡献约定：`docs/MAINTAINERS.md` 只管合并提交信息模板；**AI 政策在 README.md**——禁止提交 LLM 生成的代码与文本，允许把 LLM 当工作流工具。

## 7. 下一步学习建议

- **回到前面讲义对照**：挑一个你最感兴趣的核心机制（如 u6 时序识别、u7 存储器推断），在其源码里搜 `require(`/`unimplemented(`，用本讲的视角重新审视「它假设了什么、留了哪些扩展口」。
- **动手实战**：在 sv-elab 的 issue 跟踪器里找一个标记为「构造不支持」的小 issue，按本讲的工作流设计一个 patch（即使不提交，也是绝佳的练习）。
- **深入 slang**：三大扩展点的分派能力来自 slang 的 `ASTVisitor`，阅读 `third_party/slang/` 里 `slang/ast/ASTVisitor.h` 的 CRTP 实现，能让你更准确地知道「加一个 `handle` 会被怎样分派」。
- **测试体系全景**：若你还没读过 u8-l1（等价性测试与自测）与 u8-l2（croc_boot 端到端集成测试），建议补上，把「如何验证我的新改动是正确的」补成完整闭环。
