# 扩展开发：添加对新 SV 构造的支持

## 1. 本讲目标

sv-elab（原 yosys-slang）只支持 SystemVerilog 的「可综合子集」，不可能、也不打算覆盖 IEEE 1800 的全部语法。当源码里出现一个尚未翻译的 SV 构造时，翻译器要么**优雅地降级**（报一条诊断、用 `x` 顶上、继续综合），要么**硬中止**（打印诊断 dump 后 `log_error` 退出）。理解这两条路的区别，是给本项目「加一种新构造支持」的起点。

学完本讲，你应当能够：

- 区分 `unimplemented`/`require`/`ast_invariant`（硬中止）与 `add_diag`（软诊断），并知道何时该用哪一种；
- 在三大 AST 访问器（`EvalContext::operator()`、`StatementExecutor`、`PopulateNetlist`）里定位「新构造应该挂在哪」；
- 描述一次合格贡献的完整流程：定位触发点 → 加 handle（或加诊断）→ 补等价性测试 → 提 PR，并清楚本项目的 AI 政策边界。

本讲是整个学习手册的收官篇，承接 u5-l4（逃逸构造与循环展开）里对过程块翻译的理解，把视角从「读源码」切换到「改源码」。

## 2. 前置知识

本讲假设你已经读过：

- **u2-l1 / u2-l2**：`SlangFrontend::execute` 的四段骨架，以及 slang driver 如何产出 `ast::Compilation` 交给 sv-elab 翻译。
- **u2-l4**：诊断系统——`DiagnosticIssuer::add_diag`「先攒后报」、`diag::setup_messages` 集中登记文案与严重级别。
- **u3-l1**：`NetlistContext` 是翻译中枢，同时是「画笔」(`RTLILBuilder`) 与诊断容器。
- **u4-l1**：`EvalContext::operator()` 把 slang 表达式求值成 `RTLIL::SigSpec` 的大 switch。
- **u5-l2**：`StatementExecutor` 把过程块语句长成 HDL 意图 case 树。

两个背景概念先说清楚：

- **slang AST**：slang 把 SV 源码解析、语义分析后产出一棵类型化的 AST，节点按类别（`ExpressionKind`/`StatementKind`/`SymbolKind`）区分。sv-elab 的工作就是遍历这棵树，把每个节点翻译成 RTLIL。
- **slang ASTVisitor**：slang 提供的 CRTP 访问器框架。你写若干 `void handle(const ast::XxxNode &)` 重载，框架按节点 `kind` 自动分派到匹配的重载；没有匹配重载的节点会落到通用回退（或框架默认行为）。sv-elab 的三大翻译入口都建立在这个框架上。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/abort_helpers.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/abort_helpers.cc) | 实现 `unimplemented_`/`wire_missing_`，即硬中止时打印 AST + 源码行 dump 并 `log_error` 的逻辑 |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | 定义 `require`/`unimplemented`/`ast_invariant`/`ast_unreachable` 宏，声明三大翻译器类 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | `EvalContext::operator()`（表达式分派）与 `PopulateNetlist`（符号分派）的所在 |
| [src/statements.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h) | `StatementExecutor`：过程块语句分派，含 `WaitStatement` 与通用回退 |
| [src/diag.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) | 诊断码登记：`WaitStatementUnsupported`、`LangFeatureUnsupported` 等的文案与严重级别 |
| [README.md](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md) | 「Contributing」与「AI policy」段，规定贡献与提交方式 |
| [docs/MAINTAINERS.md](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/docs/MAINTAINERS.md) | 维护者视角：合并 commit 信息模板 |
| [tests/various/unimplemented.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/unimplemented.ys) | 硬中止路径的真实回归测试（covergroup） |
| [tests/various/wait_test.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/wait_test.ys) | 软诊断路径的真实回归测试（`wait`） |

## 4. 核心概念与源码讲解

### 4.1 abort_helpers：防御式编程的断言工具箱

#### 4.1.1 概念说明

翻译 SV 到 RTLIL 是一件「假设密集」的工作：你会频繁写下「走到这里时，这个表达式的子属性 X 必然成立」「这个分支不可能被走到」这类断言。sv-elab 没有用裸 `assert()`，而是提供了一组带丰富上下文的宏：

- `require(obj, property)`：期望 `property` 为真，否则当作「这个构造还没实现」硬中止；
- `unimplemented(obj)`：无条件硬中止，表示「这条路径还没写」；
- `ast_invariant(obj, property)`：`require` 的语义化别名，强调「这是对 AST 形状的不变量」；
- `ast_unreachable(obj)`：`unimplemented` 的语义化别名，强调「这条路径本不该被走到」。

它们和「软诊断」`add_diag`（u2-l4）的根本区别在于**是否致命**：宏一律走 `log_error`，**进程当场退出**；`add_diag` 只是把诊断攒进队列，综合继续往下跑。换句话说：

- 你**确信**某种情况不该发生、或暂时不想处理 → 用宏，让它「响亮地失败」，并自动打印调试 dump；
- 你**预期**某种构造会出现、但有意不支持 → 用 `add_diag` 报一条诊断，让设计的其余部分仍能综合。

#### 4.1.2 核心流程

当某个宏被触发时，执行流程是：

1. 宏在调用点把 `__FILE__`、`__LINE__`（以及 `require` 还会把条件字符串化 `#property`）连同 AST 节点对象一起塞进 `unimplemented_` 的某个重载。
2. `unimplemented_` 按节点类型（`Symbol`/`Expression`/`Statement`/`TimingControl`）转发到同一个模板函数 `unimplemented__`。
3. `unimplemented__` 用 slang 的 `ASTSerializer` 把节点序列化成 JSON 打到 stdout，再打印对应的源码行，最后调用 Yosys 的 `log_error`，进程退出。

用伪代码表示：

```
触发点 require(expr, cond)
   └─> 若 !(cond): unimplemented_(expr, __FILE__, __LINE__, "cond")
         └─> unimplemented__(obj, file, line, condition)
               ├─ 序列化 obj 为 JSON 并打印
               ├─ 打印 obj 对应的源码行 (文件:行:列: 内容)
               └─ log_error("Feature unimplemented at %s:%d ...")  // 进程退出
```

这套机制的关键好处：**硬中止自带调试现场**。你不需要再额外加日志去猜「到底是哪个节点、长什么样、在源码哪儿」——dump 已经把这些全给你了。

#### 4.1.3 源码精读

宏的定义集中在 [src/slang_frontend.h:641-651](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L641-L651)。先看四个核心宏：

```cpp
#define require(obj, property) { if (!(property)) unimplemented_(obj, __FILE__, __LINE__, #property); }
#define unimplemented(obj) { slang_frontend::unimplemented_(obj, __FILE__, __LINE__, NULL); }
#define ast_invariant(obj, property) require(obj, property)
#define ast_unreachable(obj) unimplemented(obj)
```

四个宏的关键差异：

- `require`/`ast_invariant` 是**条件断言**，把条件表达式字符串化（`#property`）传下去，错误信息里会显示「failed condition "xxx"」，方便定位；
- `unimplemented`/`ast_unreachable` 是**无条件断言**，条件参数传 `NULL`。

`unimplemented_` 有四个重载（[src/slang_frontend.h:641-644](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L641-L644)），按 slang AST 节点的四大基类分别接受 `Symbol`/`Expression`/`Statement`/`TimingControl`，转发到同一个模板实现（[src/abort_helpers.cc:76-98](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/abort_helpers.cc#L76-L98)）。

真正的「打印 dump 并退出」逻辑在模板函数 `unimplemented__`（[src/abort_helpers.cc:46-74](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/abort_helpers.cc#L46-L74)）。关键两段，先看 JSON + 源码行 dump：

```cpp
slang::JsonWriter writer;
writer.setPrettyPrint(true);
ast::ASTSerializer serializer(*global_compilation, writer);
serializer.serialize(obj);                       // 把 AST 节点序列化成 JSON
std::cout << writer.view() << std::endl;
// ... 计算并打印源码行：文件:行:列: 原始文本 ...
```

再看最终致命一击（[src/abort_helpers.cc:71-73](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/abort_helpers.cc#L71-L73)）：

```cpp
log_error("Feature unimplemented at %s:%d, see AST and code line dump above%s%s%s\n", file,
        line, condition ? " (failed condition \"" : "", condition ? condition : "",
        condition ? "\")" : "");
```

注意错误信息里的 `at %s:%d` 就是**触发点的源文件与行号**——这一点直接决定了 4.2 的实践方式：跑一次就能从错误信息里读出触发位置。

文件里还有一个相关函数 `wire_missing_`（[src/abort_helpers.cc:100-133](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/abort_helpers.cc#L100-L133)），由 `wire_missing` 宏调用，专门用于「该符号对应的线找不到」这类内部错误，同样以 `log_error` 收尾。它和 `unimplemented` 的区别是语义：`wire_missing` 是「内部前端错误」（网表构建期的状态不一致），而 `unimplemented` 是「功能未实现」。

举两个真实使用点，体会 `require` 与 `ast_invariant` 的差别。在 `EvalContext::operator()` 入口附近，对流式表达式做了硬断言（[src/slang_frontend.cc:1217](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1217)）：

```cpp
ast_invariant(expr, expr.kind != ast::ExpressionKind::Streaming);
```

含义是：流拼接 `{<<n{…}}` 应当由专门的 `streaming()` 入口处理（见 u4-l1），如果它居然走进了通用的 `operator()`，说明上游分流逻辑出了问题，是**不变量被破坏**，必须响亮失败。

再看三元运算符处理里的条件断言（[src/slang_frontend.cc:1576-1577](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1576-L1577)）：

```cpp
require(expr, ternary.conditions.size() == 1);
require(expr, !ternary.conditions[0].pattern);
```

含义是：sv-elab 目前只支持「单条件、无模式匹配」的三元运算符；遇到多条件或带 `pattern` 的形式就视为「未实现」硬中止。这正是「子属性不满足 → 当未实现处理」的典型用法。

#### 4.1.4 代码实践

**实践目标**：亲眼看到硬中止 dump 长什么样，并从错误信息里读出触发点的文件与行号。

**操作步骤**：

1. 本仓库已有一个现成的「硬中止回归测试」[tests/various/unimplemented.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/unimplemented.ys)。它用 `covergroup`（功能覆盖率构造，不可综合）来触发 `unimplemented`，并断言恰好出现一条 `Feature unimplemented` 错误：

   ```tcl
   logger -expect error ".*Feature unimplemented.*" 1
   read_slang <<EOF
   module covergroup_unsupported(input clk);
      ...
      covergroup gc @(posedge clk);
         a : coverpoint a_var;
      ...
      gc g1 = new;
   endmodule
   EOF
   ```

2. 在已构建好的环境里直接跑这一条脚本（构建方式见 u8-l3）：

   ```bash
   yosys tests/various/unimplemented.ys
   ```

3. 也可以去掉 `logger -expect` 那行，单独 `read_slang` 一段含 `covergroup` 的 SV，观察完整输出。

**需要观察的现象**：

- 进程会打印一大段 **JSON 格式的 AST**（`ASTSerializer` 序列化的结果）；
- 紧接着打印一行 **源码行**，形如 `Source line <文件>:<行>:<列>: <源码文本>`；
- 最后以 `Feature unimplemented at <文件>:<行>, see AST and code line dump above` 结束并退出。

**预期结果**：你能从最后这行错误信息里**直接读出触发 `unimplemented` 的源代码文件与行号**。把这个文件:行号记下来——4.2 的扩展实践会用到它。

**如果无法本地运行**：明确标注「待本地验证」。即便不运行，你也可以从 [src/abort_helpers.cc:71-73](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/abort_helpers.cc#L71-L73) 的 `log_error` 格式串推断出输出形态。

#### 4.1.5 小练习与答案

**练习 1**：`require(obj, property)` 和 `unimplemented(obj)` 在生成的错误信息上有什么差别？为什么？

**答案**：`require` 会把条件表达式字符串化（`#property`）传给 `unimplemented_`，错误信息里多出 `(failed condition "xxx")`；`unimplemented` 传 `NULL`，没有这部分。差别在于 `require` 表达「我预期某个子属性成立，否则视为未实现」，把那个属性的名字直接报出来更有助于排查。

**练习 2**：什么情况下应该用 `add_diag`（软诊断）而不是 `unimplemented`（硬中止）？

**答案**：当你**预期**该构造会出现在合法 SV 设计里、且**有意不综合它**（设计其余部分仍应产出网表）时，用 `add_diag` 报一条诊断并继续；当你**确信**该情况不该发生（不变量被破坏），或这条路径**尚未编写、暂时无法处理**时，用 `unimplemented` 让它响亮失败、并靠 dump 暴露问题。一句话：软诊断面向「用户的设计里有不支持的东西」，硬中止面向「翻译器自身还没覆盖到 / 出了内部错」。

### 4.2 扩展点：三大 AST 访问器的分派与回退

#### 4.2.1 概念说明

sv-elab 的翻译逻辑挂在 slang ASTVisitor 上的**三个**类，分别对应 AST 的三大节点类别。想给一种新 SV 构造加支持，第一件事就是判断它落在哪一类、对应的访问器在哪：

| AST 节点类别 | 翻译器 | 产出 | 典型新构造 |
|---|---|---|---|
| `ast::Expression` | `EvalContext::operator()` | `RTLIL::SigSpec` | 新的运算符、字面量、调用形式 |
| `ast::Statement` | `StatementExecutor`（`statements.h`） | HDL 意图 case 树节点 | 新的循环/分支/跳转语句 |
| `ast::Symbol` | `PopulateNetlist`（`slang_frontend.cc`） | RTLIL 模块内容（线、单元、子模块） | 新的声明类型（如 `covergroup`） |

每个访问器都是「按节点 `kind` 分派」的大 switch（或一组 `handle` 重载）。关键设计：**未识别的节点会被显式拦截**，要么走软诊断回退、要么走硬中止回退。这就给了贡献者一个清晰的插入点——新加一个 `case` 或一个 `handle` 重载即可。

#### 4.2.2 核心流程

以表达式求值为例，`EvalContext::operator()` 的分派结构是：

```
operator()(expr)
  ├─ AttributeGuard 绑定源码属性
  ├─ expr.eval(const_) 常量折叠捷径（命中则直接返回）
  └─ switch (expr.kind)
       ├─ case Assignment / BinaryOp / Concatenation / ...  ← 已支持
       ├─ ...
       ├─ default: add_diag(LangFeatureUnsupported) → 返回 x   ← 软诊断回退
       └─ 出口不变量: ast_invariant(expr, ret.size() == 位宽)
```

注意出口处那条不变量（[src/slang_frontend.cc:1670](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1670)）：无论走哪个分支，求值结果的位宽必须等于表达式类型的 bitstream 宽度。这意味着**新加一个 `case` 时，必须保证产出正确位宽的 `SigSpec`**，否则会触发硬中止。

`StatementExecutor` 与 `PopulateNetlist` 结构类似，只是回退策略不同（见 4.2.3）。给新构造加支持的通用套路是：

1. 跑一次让构造触发回退，读 dump / 诊断信息定位是哪个访问器、哪个 `kind`；
2. 在对应 switch 里加一个 `case`（或加一个 `handle(const ast::XxxNode&)` 重载）；
3. 复用已有的 `RTLILBuilder` 方法（u3-l2）发出对应的 RTLIL 单元/线；
4. 保证位宽/状态一致，别破坏出口不变量；
5. 补一个等价性测试（见 4.3）。

#### 4.2.3 源码精读

**表达式分派** `EvalContext::operator()` 起于 [src/slang_frontend.cc:1205](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1205)。它的 `default` 分支是**软诊断**回退（[src/slang_frontend.cc:1659-1661](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1659-L1661)）：

```cpp
default:
    netlist.add_diag(diag::LangFeatureUnsupported, expr.sourceRange);
    goto error;
```

`error` 标签会把返回值置成全 `x`（[src/slang_frontend.cc:1664-1667](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1664-L1667)）。`LangFeatureUnsupported` 在 [src/diag.cc:205-206](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L205-L206) 登记为 `Error` 级别、文案 `"unsupported language feature"`：

```cpp
engine.setMessage(LangFeatureUnsupported, "unsupported language feature");
engine.setSeverity(LangFeatureUnsupported, DiagnosticSeverity::Error);
```

也就是说：**遇到没 `case` 的表达式 `kind`，综合不会崩，只会报一条 `unsupported language feature` 并把那段求值成 `x`。**

**语句分派** `StatementExecutor` 定义于 [src/statements.h:190](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L190)。它对每一种已知语句写一个 `handle(const ast::XxxStatement&)` 重载，没有对应重载的语句落到通用回退（[src/statements.h:755-758](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L755-L758)），同样是**软诊断**：

```cpp
void handle(const ast::Statement &stmt)
{
    netlist.add_diag(diag::LangFeatureUnsupported, stmt.sourceRange.start());
}
```

而「语句里嵌着一个不该出现的裸 `Expression`」则用**硬中止**（[src/statements.h:760](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L760)）：

```cpp
void handle(const ast::Expression &expr) { unimplemented(expr); }
```

`wait` 语句是一个绝佳的「软诊断」范例——它有专门的 `handle`，但实现就是报一条诊断（[src/statements.h:750-753](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L750-L753)）：

```cpp
void handle(const ast::WaitStatement &stmt)
{
    netlist.add_diag(diag::WaitStatementUnsupported, stmt.sourceRange);
}
```

`WaitStatementUnsupported` 在 [src/diag.cc:154-155](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L154-L155) 登记为 **`Warning`**（注意：比 `LangFeatureUnsupported` 的 `Error` 低一级）、文案 `"wait statement will not be synthesized"`。这里的设计意图很清楚：`wait` 是事件驱动的，本就不可综合，但它常出现在测试性代码里，所以只**警告**而不当作错误阻断综合。

**符号分派** `PopulateNetlist` 定义于 [src/slang_frontend.cc:1766](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1766)，继承自 `TimingPatternInterpretor` 与 `ast::ASTVisitor`，用一长串 `handle(const ast::XxxSymbol&)` 处理各类符号（端口、例化、连续赋值、generate 块……）。没有专用 `handle` 的符号（例如 `covergroup`）不会被软诊断优雅接住，而是会在后续翻译中触发 `unimplemented` 硬中止——这正是 [tests/various/unimplemented.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/unimplemented.ys) 所覆盖的场景。

把三条回退路径放在一起对比：

| 访问器 | 未识别节点的回退 | 性质 |
|---|---|---|
| `EvalContext::operator()` | `add_diag(LangFeatureUnsupported)` → 返回 `x` | 软诊断（Error） |
| `StatementExecutor`（语句） | `add_diag(LangFeatureUnsupported)` | 软诊断（Error） |
| `StatementExecutor`（裸 Expression） | `unimplemented(expr)` | 硬中止 |
| `PopulateNetlist`（未覆盖符号） | 后续触达 `unimplemented` | 硬中止 |

结论：表达式和语句层面已经铺好了「软诊断兜底」，贡献者通常是在已有 `case` 旁边**新增一个 `case`**；而符号层面若新增一种声明类型，则需要**主动决定**是加 `handle` 生成硬件、还是加一条软诊断跳过——否则默认就是硬中止。

#### 4.2.4 代码实践

**实践目标**：把一个当前「硬中止」的构造，改成「软诊断」式处理，并定位到具体插入点。本实践是「源码阅读 + 设计」型，不要求真的提交改动。

**操作步骤**：

1. **触发并定位**。按 4.1.4 跑 `tests/various/unimplemented.ys`，从错误信息 `Feature unimplemented at <文件>:<行>` 读出触发点，确认它属于 `PopulateNetlist`（符号层）还是其调用的某个子翻译函数。

2. **判断该不该生成硬件**。`covergroup` 是 IEEE 1800 的功能覆盖率构造（见测试文件顶部注释引用的 19.7.1 节），**本质上不对应任何电路**。所以正确的设计决策**不是**给它加一个生成 RTLIL 的 `handle`，而是像 `wait` 那样**加一条软诊断**，让含 `covergroup` 的设计其余部分仍能综合。

3. **设计最小改动**（在纸上完成）：
   - 在 `PopulateNetlist` 里加一个 `handle(const ast::CovergroupSymbol&)`（或在触发 `unimplemented` 的实际位置），参考 `handle(const ast::WaitStatement&)`（[src/statements.h:750-753](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L750-L753)）的写法，调用 `netlist.add_diag(...)` 后直接 `return`，不生成任何单元；
   - 按 u2-l4 的「五处对齐」新增一个诊断码（例如 `CovergroupUnsupported`）：声明（diag.h）、定义（diag.cc）、登记文案与严重级别（`setup_messages`）、触发点（此处）、测试断言；
   - 严重级别选 `Warning` 还是 `Error` 需要权衡：参考 `WaitStatementUnsupported` 用了 `Warning`（[src/diag.cc:154-155](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L154-L155)），因为这类构造常出现在非综合语境。

4. **设计测试**。把现有的 `unimplemented.ys` 改写成「软诊断」版，参考 [tests/various/wait_test.ys:17](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/wait_test.ys#L17) 的写法：

   ```tcl
   test_slangdiag -expect "covergroup will not be synthesized"
   ```

   替换掉原来的 `logger -expect error ".*Feature unimplemented.*" 1`。

**需要观察的现象**：

- 改动前：跑该用例，进程打印 AST/源码 dump 后以 `Feature unimplemented` 退出；
- 改动后（设想）：进程**不再退出**，只输出一条诊断，综合继续完成。

**预期结果**：你得到一份清晰的「触发点 → 新 handle → 新诊断码 → 新测试」改动清单。

**待本地验证**：本实践未实际运行；触发点的确切文件:行号需以 4.1.4 的 dump 输出为准。同时注意：`covergroup` 的 AST 形态、是否需要连同其成员符号一起跳过，需在读 dump 后确认，本讲标注为「待确认」。

#### 4.2.5 小练习与答案

**练习 1**：`EvalContext::operator()` 的 `default` 分支用软诊断，而 `StatementExecutor::handle(const ast::Expression&)` 用硬中止。为什么后者不能用软诊断？

**答案**：语句访问器收到一个裸 `Expression` 节点，意味着出现了「语句位置上却是个表达式」这种**结构性异常**，是上游分流出错，不是「用户写了不支持的东西」。对结构性异常应当硬中止、暴露 bug；而 `operator()` 的 `default` 面对的是「一个合法但未实现的表达式 `kind`」，属于可预见的用户输入，适合软诊断降级。

**练习 2**：在 `EvalContext::operator()` 末尾有一条 `ast_invariant(expr, ret.size() == (int) expr.type->getBitstreamWidth())`。你新加一个 `case` 处理某种表达式时，这条不变量对你意味着什么约束？

**答案**：意味着你产出的 `RTLIL::SigSpec ret` 的位宽**必须**等于该表达式类型的 bitstream 宽度。常见做法是让组合单元的输出宽度参数对齐，或在末尾用 `extend_u0`/截断补足。若位宽不符，会直接触发 `unimplemented` 硬中止——这其实是一道保护下游（RTLIL 校验、`proc`）的安全阀。

### 4.3 贡献流程：从诊断到等价性测试到 PR

#### 4.3.1 概念说明

前两节讲的是「技术上怎么改」。本节讲「流程上怎么提交」。sv-elab 是一个有明确维护者（@povik）的开源项目，对贡献有一套约定，尤其是一条**很严格的 AI 政策**——即便你只是用 AI 辅助，也必须先读清楚。

一条合格的功能贡献通常由四部分组成：

1. **改动本体**：在合适的访问器里加 `handle`/`case`；
2. **诊断（若涉及）**：按 u2-l4 的五处对齐新增/调整诊断码；
3. **测试**：在 `tests/` 下加一个等价性测试（u8-l1）或诊断测试；
4. **PR**：写清楚动机，遵循 AI 政策。

#### 4.3.2 核心流程

```
发现未支持的构造
   └─> 复现 + 读 dump，定位访问器与 kind        (4.1 / 4.2)
        └─> 决策：生成硬件 or 软诊断跳过？
             ├─ 生成硬件 → 加 case/handle，发 RTLIL 单元
             └─ 软诊断  → 加诊断码（五处对齐）+ handle 里 add_diag 后 return
                  └─> 写测试
                       ├─ 生成硬件 → 等价性测试（gold vs gate，equiv_induct）
                       └─ 软诊断  → test_slangdiag -expect "..."
                            └─> 在 tests/CMakeLists.txt 注册（若需要）
                                 └─> 本地跑 tests/run.sh 或 ctest 全绿
                                      └─> 提 PR（遵守 AI 政策）
```

#### 4.3.3 源码精读

**等价性测试范式**见 [tests/unit/dff.ys:41-44](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L41-L44)——这是「生成硬件」类贡献的测试样板：

```tcl
async2sync
equiv_make dff_iff01_gold dff_iff01_gate dff_iff01_equiv
equiv_induct dff_iff01_equiv
equiv_status -assert
```

套路是：`read_slang` 产 gate 网表，`read_rtlil`/`read_verilog` 产 gold 网表，`equiv_make` 建比较器，`equiv_induct` 做 k-归纳证明，`equiv_status -assert` 断言全证毕（u8-l1）。你新加的构造若生成硬件，就照这个范式写一对 gold/gate。

**诊断测试范式**见 [tests/various/wait_test.ys:1-17](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/wait_test.ys#L1-L17)：先 `read_slang` 一段会触发诊断的 SV，再用 `test_slangdiag -expect "wait statement will not be synthesized"` 断言文案命中（配合 u2-l4 的 `check_diagnostics` 与 `in_succesful_failtest`，使带错也算通过）。你若新增软诊断，就照这个范式断言你的文案。

**贡献与 AI 政策**在 [README.md:82-94](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L82-L94)。先看欢迎语与沟通约定（[README.md:82-84](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L82-L84)）：

> Contributions are welcome! If you intent to develop a particular feature, feel free to get in touch and consult on the appropriate approach.

即：动手开发某个特性前，**先和维护者沟通**确认方向。

AI 政策（[README.md:86-94](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L86-L94)）有三条要点，必须牢记：

1. **禁止向项目公开论坛（Issues / PR / Discussions）提交 LLM 生成的内容**（代码或文字），PR 里的代码也算；
2. **例外**：为 bug report 准备的「复现用例（reproducer）」可以含 LLM 产物，前提是已最小化、并清掉多余的 LLM 注释；
3. **允许**：把 LLM 当工具用于搜索、调试、测试或做机械编辑；但最终提交的代码必须是**人**基于自己对代码库的理解写出来的，而非 LLM 直接生成。维护者本人保留绕过此规则直接 push 的权利。

这条政策对本讲的直接含义：你可以用 AI 辅助**理解**源码、**定位**触发点、**生成测试输入**，但**不要把 AI 生成的 handler/patch 直接放进 PR**。4.2.4 的「设计最小改动」应理解为学习练习；若要真正上游，代码需由你自己人工编写。

**维护者侧的合并约定**见 [docs/MAINTAINERS.md:1-9](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/docs/MAINTAINERS.md#L1-L9)：合并 commit 用 `Merge "<PR title>" (#<PR number>)` 模板，正文引用贡献者说明中的精炼片段；squash 合并则用 GitHub 默认模板、同样替换为精炼引述。这主要是维护者视角，但贡献者写好 PR 标题与说明会直接进入 commit 信息。

#### 4.3.4 代码实践

**实践目标**：为 4.2.4 中设计的「covergroup 软诊断」改动，配齐一份完整、可提交的测试与文档清单（不实际提交）。

**操作步骤**：

1. **写诊断测试**。仿照 [tests/various/wait_test.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/wait_test.ys)，新建 `tests/various/covergroup_test.ys`：

   ```tcl
   read_slang <<EOF
   module covergroup_test(input clk);
      int a_var;
      covergroup gc @(posedge clk);
         a : coverpoint a_var;
      endgroup
   endmodule
   EOF

   test_slangdiag -expect "covergroup will not be synthesized"
   ```

2. **注册测试**。查阅 `tests/CMakeLists.txt`（u8-l1）里的 `ALL_TESTS` 列表，把新文件名加进去；若用 `tests/run.sh`（u8-l1）跑测，确认它会被自动扫到。

3. **本地验证**。

   ```bash
   ./tests/run.sh            # 不依赖 CMake 的红绿脚本（u8-l1）
   # 或
   ctest                     # 走 CMake/CTest
   ```

4. **核对 AI 政策合规**。对照 [README.md:86-94](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L86-L94) 自检：提交的 handler 与测试代码是你自己写的吗？没有残留的 LLM 注释吗？

5. **提 PR 前**。按 [README.md:84](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L84) 先开 issue 或讨论与维护者对齐方向，PR 标题写清「Add soft diagnostic for covergroup」之类。

**需要观察的现象**：改动前该输入会硬中止退出；改动后只产一条 `Warning`/`Error` 诊断，综合正常完成，`test_slangdiag -expect` 命中。

**预期结果**：一份「1 个新诊断码 + 1 个新 handle + 1 个新测试 + tests/CMakeLists.txt 一行注册」的完整改动包，且通过 AI 政策自检。

**待本地验证**：测试是否被 `run.sh` 自动发现、诊断文案是否与 `-expect` 完全一致（u2-l4 强调比对的是格式化文案），均需实跑确认。

#### 4.3.5 小练习与答案

**练习 1**：你给一种新运算符加了 `EvalContext` 的 `case` 并生成硬件。该配哪种测试——等价性测试还是 `test_slangdiag`？为什么？

**答案**：配**等价性测试**。因为新运算符产出真实 RTLIL，需要证明 gate 网表与参考 gold 网表行为等价，正用 `equiv_make`/`equiv_induct`/`equiv_status -assert` 这套范式（[tests/unit/dff.ys:41-44](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L41-L44)）。`test_slangdiag` 只用于断言诊断文案，适合「软诊断跳过」类改动。

**练习 2**：本项目 AI 政策允许用 LLM 做哪些事？禁止做什么？

**答案**：允许把 LLM 当工具用于搜索、调试、测试、机械编辑；允许在 bug report 的最小化复现用例里含 LLM 产物。禁止向 Issues/PR/Discussions 提交 LLM 生成的代码或文字（含 PR 中的代码）；提交的代码必须是人基于自身理解写出来的（[README.md:86-94](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L86-L94)）。

## 5. 综合实践

把本讲三节串起来，完成一次「从触发到设计到测试」的完整推演（源码阅读 + 设计型，无需提交）：

1. **触发**。挑一个当前未支持的 SV 构造。推荐两个现成样本：硬中止侧用 [tests/various/unimplemented.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/unimplemented.ys) 的 `covergroup`，软诊断侧用 [tests/various/wait_test.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/wait_test.ys) 的 `wait`。

2. **分类**。对照 4.2.3 的回退对比表，判断它落在表达式 / 语句 / 符号哪一层、当前是硬中止还是软诊断，并用 dump（4.1.4）确认触发点的文件:行号。

3. **决策**。问自己：这个构造**对应电路吗**？
   - 对应（如某个新运算符）→ 走「生成硬件」路线：加 `case`/`handle`，复用 `RTLILBuilder`（u3-l2）发单元，保证出口位宽不变量；
   - 不对应（如 `covergroup`、`wait`）→ 走「软诊断」路线：加诊断码（u2-l4 五处对齐），`handle` 里 `add_diag` 后 `return`。

4. **测试**。生成硬件 → 写等价性测试（gold vs gate + `equiv_induct`）；软诊断 → 写 `test_slangdiag -expect`；按需在 `tests/CMakeLists.txt` 注册。

5. **合规**。对照 [README.md:86-94](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L86-L94) 做 AI 政策自检，确认代码是你自己写的。

产出物：一张包含「触发点文件:行号、所属访问器、决策（硬件/软诊断）、要改的文件清单、要加的测试、预期现象」的表格。这就是一份合格的贡献设计文档。

## 6. 本讲小结

- sv-elab 对未支持构造有两条路：**软诊断**（`add_diag`，综合继续，常见于「有意不支持」）与**硬中止**（`unimplemented`/`require`/`ast_invariant`/`ast_unreachable`，`log_error` 退出，常见于「不变量被破坏 / 尚未编写」）。
- 四个宏都汇集到 [src/abort_helpers.cc:46-74](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/abort_helpers.cc#L46-L74) 的 `unimplemented__`，它会自动打印 AST 的 JSON 与源码行 dump，错误信息里直接带触发点的文件:行号——这是定位问题的利器。
- 三大 AST 访问器是扩展点：`EvalContext::operator()`（表达式，[src/slang_frontend.cc:1205](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1205)）、`StatementExecutor`（语句，[src/statements.h:190](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L190)）、`PopulateNetlist`（符号，[src/slang_frontend.cc:1766](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1766)）。前两者已有软诊断兜底，符号层未覆盖则会硬中止。
- 加支持的通用套路：跑一次读 dump 定位 → 决策（生成硬件 or 软诊断）→ 加 `case`/`handle` → 必要时五处对齐加诊断码 → 补等价性或诊断测试。
- 贡献前先与维护者沟通；本项目 AI 政策严格：禁止提交 LLM 生成的代码/文字到 PR，但允许用 LLM 辅助搜索、调试、测试与机械编辑（[README.md:86-94](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/README.md#L86-L94)）。
- `wait` 与 `covergroup` 是一对绝佳对照：前者是「软诊断优雅跳过」的范本（[src/statements.h:750-753](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L750-L753)），后者是「未覆盖即硬中止」的实例（[tests/various/unimplemented.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/unimplemented.ys)）。

## 7. 下一步学习建议

- **动手读一个真实 handler**：挑 [src/statements.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h) 里任意一个非平凡的 `handle`（如 `handle(const ast::ForLoopStatement&)`，u5-l4），看它如何把语句翻译进 case 树，体会「新增一个 handle」的真实工作量。
- **跟踪一次诊断的一生**：从 `add_diag`（u2-l4）到 `setup_messages` 登记文案、再到 `check_diagnostics` 比对，完整走一遍五处对齐，为将来新增诊断码做准备。
- **跑一遍测试体系**：按 u8-l1 用 `tests/run.sh` 或 `ctest` 跑全量等价性测试，感受 gold/gate 范式，并尝试为本讲 4.2.4 的设计真正补一个用例。
- **回顾整条翻译流水线**：从 u2-l1 的 `execute` 四段、u3 的数据模型、u4 的表达式、u5 的过程块、u6 的时序、u7 的高级主题一路回看，建立「一个 SV 构造从源码到 RTLIL 经历了哪些层」的全景图——这是判断「新构造该挂在哪一层」的终极依据。
