# DSL 解析：Python AST 如何变成 IR

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 PyPTO 把一段 Python DSL 源码变成 IR 的完整路径：装饰器抓源码 → `ast.parse` → `ASTParser` 逐语句翻译。
2. 理解两条解析入口的分工：`@pl.function` / `@pl.program` 装饰器解析（作者写代码时触发）与 `pl.parse` / `pl.parse_program` 文本解析（打印-再解析、JIT 特化重解析时触发），以及它们最终汇入同一个 `ASTParser`。
3. 掌握 Python 语法到 IR 节点的映射表：`for` → `ForStmt`、`if` → `IfStmt`、赋值 → `AssignStmt`/`EvalStmt`、`pl.xxx(...)` 调用 → `Call` 表达式。
4. 读懂 `parse_op_call` 的命名空间路由（`pl.` / `pl.tensor.` / `pl.tile.` / `pld.` / `pl.builtin.`），并理解 `invoke_dsl` 如何把「解析器的 ir.Expr」与「DSL 包装函数的 Tensor/Tile/Scalar」衔接起来。
5. 理解本次更新的重点：打印器输出的 `pl.builtin.<ns>.<op>` 形式如何被解析器读回，保证「打印 → 再解析」往返一致。

## 2. 前置知识

在阅读本讲之前，你应该已经：

- 写过几个 `@pl.jit` 算子（单元 1、单元 2 的内容），知道 `pl.load` / `pl.store` / `pl.range` / `pl.at` 是什么。
- 了解 IR 的基本概念：PyPTO 的中间表示由 Program / Function / Stmt / Expr / Type 组成（u1-l1、u1-l3 讲过分层；u4 会系统精读）。

本讲还需要两个背景概念：

**AST（Abstract Syntax Tree，抽象语法树）**。Python 解释器在执行代码前，会先把源码文本解析成一棵语法树。标准库的 `ast` 模块可以把这棵树暴露出来：`ast.parse("x = a + b")` 会得到一个 `Assign` 节点，它的 `value` 是一个 `BinOp` 节点。PyPTO 不「执行」DSL 函数，而是**解读这棵树**——遇到 `ast.For` 就造一个 IR 的 `ForStmt`，遇到 `ast.Call` 就造一个 IR 的 `Call` 表达式。

**span（源位置）**。IR 里每个节点都带一个 `Span`，记录它来自哪个文件的第几行第几列。这是报错时能画出 `-->` 箭头的根基。解析器用 `SpanTracker` 在翻译的同时把 AST 的行号抄给 IR 节点。

**internal_only 算子**。算子注册表（u4-l6 会精读）里有一部分算子标记为「仅编译器内部使用」——用户不能直接调用，它们由 Pass 在降级过程中合成。用户写的是复合形式 `pld.tensor.allreduce`，Pass 42（LowerHostTensorCollectives）把它降级成内部的 `builtin.tensor.allreduce`。这类算子没有 DSL 包装函数，打印器却会把它们打印出来——于是解析器必须能读回，否则打印出的 IR 就是「死文本」。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| `python/pypto/language/parser/decorator.py` | `@pl.function` / `@pl.program` 装饰器：抓取函数源码、`ast.parse`、创建 `ASTParser` |
| `python/pypto/language/parser/ast_parser.py` | 核心解析器（约 9000 行）：语句分发、表达式翻译、算子调用路由 |
| `python/pypto/language/parser/_dsl_invoker.py` | 解析期调用 DSL 包装函数的桥梁：包装参数、钉住 span、解包返回值 |
| `python/pypto/language/parser/text_parser.py` | 文本解析入口 `parse` / `parse_program`：`exec` 代码串后走同一条装饰器链 |
| `tests/ut/ir/parser/test_builtin_namespace.py` | `pl.builtin.*` 命名空间的解析测试（本次新增） |
| `docs/en/dev/language/00-python_syntax.md` | Python IR 语法规范：类型系统、表达式、语句的完整文法 |

辅助模块（本讲只提名字，不深入）：`span_tracker.py`（源位置追踪）、`scope_manager.py`（作用域与 SSA 校验）、`type_resolver.py`（类型注解解析）、`diagnostics/`（报错渲染）。

## 4. 核心概念与源码讲解

### 4.1 两条解析入口，一个解析器

#### 4.1.1 概念说明

PyPTO 的 DSL 就是 Python。让 Python 代码变成 IR，第一步是拿到**源码文本**，第二步才是解析。拿源码有两种途径：

1. **装饰器入口（作者视角）**：`@pl.function` 装饰一个真实函数时，装饰器用 `inspect` 抓出函数源码，去缩进后交给 `ast.parse`。你在 u2 写的每个算子都走的这条路。
2. **文本入口（机器视角）**：`pl.parse(code_str)` 接收一段字符串。它先把字符串 `exec` 出来——字符串里的 `@pl.function` 装饰器随之真实执行，于是又回到了装饰器入口。这条路径服务于两个场景：**打印-再解析**（把 `ir.python_print` 的输出读回 IR，测试与调试的基石）和 **JIT 特化重解析**（u2-l1 讲过，特化器把 jit 函数重写成 `@pl.program` 源码再解析，`source_map` 参数用来把生成代码的行号映射回用户原始文件）。

无论哪条路，最终都汇入同一个 `ASTParser` 类。这就是「分工」的本质：**入口负责拿源码，解析器负责翻译**。

#### 4.1.2 核心流程

```text
装饰器入口:
  @pl.function 装饰
    → _get_source_info(): inspect 抓源码 + 起始行号
    → textwrap.dedent(): 去缩进（ast.parse 要求顶层缩进为 0）
    → ast.parse(): 得到 Python AST
    → ASTParser(source_file, source_lines, line_offset, ...)
    → parser.parse_function(func_def, ...)  →  ir.Function

文本入口:
  pl.parse(code_str)
    → compile(code_str) + exec(): 让字符串里的装饰器真实执行
    → （回到装饰器入口）
    → ir.Function / ir.Program
```

#### 4.1.3 源码精读

装饰器如何抓源码并创建解析器：

- [decorator.py:993-1022](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/decorator.py#L993-L1022)：`@pl.function` 的核心段落——`_get_source_info` 取源码文件与行列表，`textwrap.dedent` 去缩进并据此计算列偏移，`line_offset` 记录「函数在原文件的第几行」以映射回真实行号；随后 `ast.parse` 出语法树、定位 `FunctionDef` 节点，构造 `ASTParser`。
- [decorator.py:1014-1022](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/decorator.py#L1014-L1022)：`ASTParser` 的构造参数——`strict_ssa`、闭包变量 `closure_vars`（外层 Python 作用域里的常量从这里进入解析）、`pending_comments`（行注释，最终挂到 IR 语句的 `leading_comments` 元数据上）。`@pl.program` 在装饰类时走同样的构造（见 decorator.py:1313 附近）。

解析器主类的字段勾勒出它的四大协作模块：

- [ast_parser.py:629-646](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L629-L646)：`ASTParser` 类声明与构造签名。注意 `global_vars` / `gvar_to_func` 两个参数——`@pl.program` 装饰多个函数时共享它们，跨函数调用（`self.helper(x)`）才能解析成对其他 `ir.Function` 的 `Call`。
- [ast_parser.py:671-684](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L671-L684)：构造函数体——组装 `SpanTracker`（源位置）、`ScopeManager`（作用域/SSA）、`ExprEvaluator`（闭包求值）、`TypeResolver`（类型注解）、`IRBuilder`（真正造 IR 节点的 Builder API，u4-l7 精读）。解析器自己不直接 `new` IR 节点，一切都通过 `self.builder`。

文本入口如何汇入同一条链：

- [text_parser.py:162-221](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/text_parser.py#L162-L221)：`parse` 的文档字符串讲明了机制——「代码被动态执行，自动 import pypto.language as pl」，并明确警告使用了 `exec`（只应喂可信输入）。`source_map` 参数把生成代码行号重映射回用户原始文件，服务 JIT 特化链路。
- [text_parser.py:367-394](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/text_parser.py#L367-L394)：`parse_program` 现在是 `parse` 的别名，仅校验结果确实是 `Program`。测试里大量使用它做往返验证（本讲 4.4 与实践任务都会用到）。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「装饰器入口」抓的是源码文本而非函数对象。
2. **操作步骤**：
   - 在 Python 里定义一个普通函数 `def probe(): return pl.const(1, pl.INT32)`，用 `inspect.getsource(probe)` 打印它，观察拿到的是带缩进的源码文本。
   - 对照阅读 decorator.py:1000-1001 的 `textwrap.dedent` 调用，理解为什么要去缩进（类方法体天然带一层缩进，`ast.parse` 只接受顶层无缩进代码）。
3. **观察现象**：`inspect.getsource` 返回的字符串保留了方法在类里的缩进。
4. **预期结果**：理解「装饰器解析 = 源码文本 → AST → IR」，与函数是否被 Python 调用过无关——`@pl.function` 装饰完成时 IR 就已经存在了。
5. 待本地验证（无需编译环境，纯 Python 层即可运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pl.parse` 用 `exec` 执行代码串，而不是直接 `ast.parse` 后手工找装饰器？

**答案**：因为 `exec` 让字符串里的 `@pl.function` 装饰器真实执行，装饰器内部已经实现了完整的抓源码 → AST → 翻译流程（包括类体的 `@pl.program` 处理、闭包变量收集、跨函数引用解析）。直接 `ast.parse` 就要重复实现这一整套；复用装饰器链路只需保证 `exec` 后能从临时模块的命名空间里取回产物（`parse` 在 text_parser.py:222-246 里正是这么做的：注册 linecache、建临时模块）。

**练习 2**：`ASTParser` 构造参数里的 `dyn_var_cache` 起什么作用？

**答案**（见 [ast_parser.py:661-663](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L661-L663) 的文档）：它是「动态维度变量名 → ir.Var」的共享缓存。`@pl.program` 的多个函数共享同一个 cache 时，同一个 `DynVar`（比如动态维度 `M`）在不同函数里解析出**同一个** `ir.Var` 对象——这呼应 u2-l2 讲过的规则「同一维度须复用同一 DynVar」，缓存就是在解析层落实这条规则。

---

### 4.2 语句层：parse_statement 的分发与 for/if 的映射

#### 4.2.1 概念说明

函数体是一条条 Python 语句。`ASTParser.parse_statement` 是语句层的总分发器：它拿着一个 `ast.stmt` 节点，按类型路由到对应的处理方法，每个处理方法再通过 `IRBuilder` 造出 IR 语句。**这张分发表就是「Python 语法 → IR 节点」映射的核心**：

| Python 语法 | 处理方法 | IR 产物 |
| ---- | ---- | ---- |
| `x = ...` / `x: T = ...` | `parse_assignment` / `parse_annotated_assignment` | `AssignStmt`（或 `EvalStmt`） |
| `for i in pl.range(...)` | `parse_for_loop` | `ForStmt`（kind 由迭代器决定） |
| `while ...` | `parse_while_loop` | `WhileStmt` |
| `if ... / else` | `parse_if_statement` | `IfStmt`（配 `pl.yield_` 时带 return_vars phi） |
| `with pl.at(...)` 等 | `parse_with_statement` | `ScopeStmt`（InCore 等作用域） |
| `return ...` | `parse_return` | `ReturnStmt` |
| 表达式语句（裸调用） | `parse_evaluation_statement` | `EvalStmt` |
| `break` / `continue` / `pass` | `parse_break` / `parse_continue` / 无 | `BreakStmt` / `ContinueStmt` / 无 |

不在表里的语句一律拒绝，报 `UnsupportedFeatureError` 并附 hint——这是「通过报错定位处理分支」的反向用法：报错信息里出现的语句类型名，就是你应该去找的 `isinstance` 分支。

#### 4.2.2 核心流程

以 `for i, (acc,) in pl.range(n, init_values=(x,))` 为例：

```text
ast.For
  → parse_statement 分发到 parse_for_loop (ast_parser.py:2686)
  → _validate_for_loop_iterator: 迭代器必须是 pl.range/parallel/unroll/pipeline/while_/spmd/split_aiv
  → while_ / spmd / split_aiv 各有专门子路径
  → _parse_for_loop_target: 拆出循环变量名与 iter_args 元组目标
  → _parse_range_call: 解析 start/stop/step/init_values/attrs
  → _ITERATOR_TO_KIND[iterator_type] 查表得 ForKind
  → 从边界常量 dtype 推断循环变量 dtype（INDEX 或 INT64）
  → builder.for_loop(...) 进入循环体作用域，递归解析 body
```

`pl.range` / `pl.parallel` / `pl.unroll` / `pl.pipeline` 四种迭代器共享同一条 `ForStmt` 骨架，仅 `ForKind` 枚举不同；而 `pl.while_` 被建模成「for + 条件」的形式，`pl.spmd` / `pl.split_aiv` 则各自展开成专门的 scope 结构。

#### 4.2.3 源码精读

- [ast_parser.py:1271-1335](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L1271-L1335)：`parse_statement` 的完整分发表。开头的 `_drain_pending_comments`（1283）把行注释排空挂到即将生成的语句上；1285-1295 的特判把裸字符串（docstring）改造成下一条语句的注释；1307-1328 的 `isinstance` 链就是上表的落地；1329-1335 对未知语句抛 `UnsupportedFeatureError`，报错文案列出全部支持的语句类型。
- [ast_parser.py:2686-2717](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L2686-L2717)：`parse_for_loop` 开头——先校验迭代器类型，再按 `while_` / `spmd` / `split_aiv` 三个特例分流。docstring（2687-2700）写清了两种目标写法：Pattern A（带 `init_values` 的元组目标，产生 iter_args/yield）与 Pattern B（简单 `for i in pl.range(n)`，无 iter_args，SSA 化交给后续的 C++ ConvertToSSA Pass）。
- [ast_parser.py:8823-8828](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L8823-L8828)：`_ITERATOR_TO_KIND` 查表——`range→Sequential`、`parallel→Parallel`、`unroll→Unroll`、`pipeline→Pipeline`。四行字典决定了四种循环的 IR 身份。
- [ast_parser.py:2790-2808](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L2790-L2808)：循环变量 dtype 的推断逻辑——裸 `int` 字面量映射 `INDEX`，显式 `INT64` 边界则重建 `INT64` 循环变量。这段注释点明动机是「保持往返保真」：打印器若以 INT64 边界打印，重新解析必须还原出 INT64 循环变量，打印与解析才能闭环。
- [ast_parser.py:3495-3557](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L3495-L3557)：`parse_if_statement` 全貌。关键设计：**先扫描后解析**——用 `_scan_for_yields`（3520、3524）预扫两个分支里的 `pl.yield_` 变量名，据此决定 `should_leak`（无显式 yield 时变量泄漏到外层作用域，phi 节点交给 C++ ConvertToSSA）；有 yield 时在 3548-3557 声明 `return_vars`（phi 的 IR 形态），并在 3559-3568 把结果变量注册回外层作用域。这就是 README 里「if + pl.yield_ = phi 节点」机制的实现处。
- [ast_parser.py:1345-1357](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L1345-L1357)：`_parse_body_siblings`——块内语句逐条交给 `parse_statement`，兄弟语句之间做「过期注释清扫」。所有复合语句（for/if/with）的 body 都经它递归下降。

#### 4.2.4 代码实践

1. **实践目标**：亲手把一条 `for` 与一条 `if` 映射到 `ast_parser.py` 的具体处理函数。
2. **操作步骤**：
   - 写一个最小算子（示例代码）：

     ```python
     import pypto.language as pl

     @pl.program
     class P:
         @pl.function
         def main(self, x: pl.Tensor[[8], pl.FP32]) -> pl.Tensor[[8], pl.FP32]:
             out = pl.tensor.create([8], pl.FP32)
             for i in pl.range(2):
                 t = pl.load(x, shapes=[8], offsets=[0])
                 if i == 0:
                     t2 = pl.mul(t, pl.const(2.0, pl.FP32))
                 else:
                     t2 = t
                 pl.store(out, t2, offsets=[0])
             return out
     ```

   - 在 `parse_statement` 的 `isinstance(stmt, ast.For)` 分支（ast_parser.py:1311-1312）与 `ast.If` 分支（1315-1316）处各加一行心算注释：「这里把 ast.For 交给 parse_for_loop → ForStmt」。
   - 打印该程序 IR 验证：`from pypto.ir import python_print; print(python_print(P))`（与 `pypto.pypto_core.ir.python_print` 是同一能力的 Python 包装，见 `python/pypto/ir/printer.py:15`），在输出里找到 `for` 与 `if` 对应的语句。
3. **观察现象**：打印出的 IR 里出现形如 `for i in pl.range(...)` 与 `if ...` 的行——注意打印器输出的是**合法 DSL 源码**，与解析器输入同形。
4. **预期结果**：源码里的循环对应 IR 的 `ForStmt`，分支对应 `IfStmt`；本例 `if` 无 yield，故无 return_vars，变量直接泄漏到外层。
5. 若本地未配置运行环境，步骤中的打印可改为纯阅读：对照 docs/en/dev/language/01-statements.md 的文法阅读。

#### 4.2.5 小练习与答案

**练习 1**：`for i in range(2)`（Python 内建 `range`，没有 `pl.` 前缀）会发生什么？

**答案**：`_validate_for_loop_iterator`（parse_for_loop 第一步，ast_parser.py:2702）要求迭代器调用必须是 `pl.range` / `pl.parallel` 等白名单形式，内建 `range` 不在其中，解析器会抛出带 hint 的解析错误。这就是 u1-l5 反复强调「循环必须用 `pl.range`」的执行点。

**练习 2**：为什么 `parse_if_statement` 要「先扫描 yield 再解析分支」，而不是解析完了再收集？

**答案**：`IfStmt` 的 phi（`return_vars`）需要**在构造 IfStmt 时**就知道两个分支各产出哪些变量及其类型（3548-3557 的注释：分支解析完后再声明 return_vars，用的是扫描阶段捕获的 yield 类型）。而作用域管理要求 `enter_scope` / `exit_scope(leak_vars=...)` 在解析分支**之前**就确定 `should_leak`（3532）——是否有显式 yield 决定了变量是留在分支作用域内还是泄漏给外层让 C++ Pass 处理。两件事都只能在解析前回答，所以必须先扫描。

---

### 4.3 表达式与算子调用：parse_expression 与 parse_op_call 的命名空间路由

#### 4.3.1 概念说明

语句的骨架之下是表达式。`parse_expression` 处理 Python 的字面量、变量、二元运算、比较、下标等；其中最重量级的是**函数调用** `ast.Call`——所有 `pl.*` / `pld.*` 算子调用都从这里进入。

`parse_op_call` 解决的问题是：`pl.add(x, y)`、`pl.tensor.add(x, y)`、`pld.tensor.barrier(s)`、`pl.builtin.tensor.barrier(...)` 这些不同**拼写**该路由到哪里？它的答案是一张按「属性链段数 + 命名空间」组织的路由表：先把 `pl.tensor.add` 这样的属性访问拆成 `["pl", "tensor", "add"]`，再逐条匹配。

路由到具体算子后，解析器**不自己造 `Call` 节点**，而是调用 Python 层的 DSL 包装函数（`pypto.language.op.add` 等），让包装函数完成类型检查与分发（比如 Tile+标量自动路由到 `tile.adds`——u2-l3 讲过的行为就实现在包装层）。`invoke_dsl` 是这座桥：把解析器手里的 `ir.Expr` 包装成 DSL 的 `Tensor` / `Tile` / `Scalar` 对象，调用包装函数，再把返回值解包回 `ir.Expr`。

#### 4.3.2 核心流程

```text
ast.Call (例如 pl.tile.add(a, b))
  → parse_expression → parse_call (ast_parser.py:6079)
  → 属性链拆解: pl.tile.add → attrs = ["pl", "tile", "add"]
  → parse_op_call (ast_parser.py:6183) 依次匹配:
      1. pl.aiv_shard / pl.aic_gather 特例        （split 作用域继承）
      2. pld.<op>           2 段统一形式           → _parse_pld_op
      3. pld.<cat>.<op>     3 段规范形式           → _parse_pld_category_op
      4. pl.builtin.<cat>.<op>  打印器专用回读      → _parse_builtin_op   ← 本次新增
      5. pl.tensor.<op> / pl.tile.<op> / pl.system.<op> / pl.array.<op> / pl.prefetch.<op>
      6. pl.const / pl.FP32.get_byte 等常量特例
      7. pl.<op>            2 段统一形式           → _parse_unified_op
      8. 都不匹配 → UnsupportedFeatureError
  → _dispatch_op(module, ...):
      解析位置参数 / kwargs / attrs
      → invoke_dsl(包装函数, args, kwargs, span)
          → _wrap_arg: ir.Expr → DSL Tensor/Tile/Scalar
          → use_parser_span(span) 钉住调用点源位置
          → 调用包装函数（其内部 builder 造 Call 节点）
          → _unwrap_result: DSL 对象 → ir.Expr
      → _attach_op_attrs: 挂上 attrs={...} 机器属性
```

#### 4.3.3 源码精读

**表达式分发**：

- [ast_parser.py:5794-5830](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L5794-L5830)：`parse_expression` 的 isinstance 链——`Name→parse_name`、`Constant→parse_constant`、`BinOp→parse_binop`、`Call→parse_call`、`Subscript→parse_subscript` 等；不支持的表达式抛 `UnsupportedFeatureError`。这张表是表达式层的映射总纲。
- [ast_parser.py:5861-5878](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L5861-L5878)：`parse_constant`——Python `bool` → `ir.ConstBool`，`int` → `ir.ConstInt(value, INDEX)`，`float` → `ir.ConstFloat(value, DEFAULT_CONST_FLOAT)`。注意裸 `int` 被钉成 `INDEX` dtype；想指定 dtype 必须用 `pl.const(value, dtype)`（u2-l4 讲过的惯用法）。5880-5883 还揭示了 `None` 的特殊身份：TaskId 的「无效」哨兵，最终降级为 `system.task_invalid`。

**算子调用路由**：

- [ast_parser.py:6183-6204](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L6183-L6204)：`parse_op_call` 开头——while 循环沿 `ast.Attribute` 链向左收集属性名，把 `pl.tensor.add` 拆成 `["pl", "tensor", "add"]`。
- [ast_parser.py:6222-6236](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L6222-L6236)：`pld` 两个分支（6222-6228）与**本次新增的 `pl.builtin` 分支**（6230-6236）。builtin 分支只按 `attrs[1] == "builtin"` 匹配而不校验段数——段数校验推迟到 `_parse_builtin_op` 内部，这样拼错拼写（如 `pl.builtin.barrier` 少了一段）会得到「命名空间自己的诊断 + 正确拼写提示」，而不是掉进 2 段统一路径报出无用的 `Unknown operation 'pl.builtin'`。
- [ast_parser.py:6238-6288](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L6238-L6288)：其余命名空间分支——`pl.tensor.*` / `pl.tile.*` / `pl.system.*` / `pl.array.*` / `pl.prefetch.*` 五个 3 段命名空间、`pl.const` 与 `pl.<dtype>.get_byte()` 常量特例、最后的 2 段统一形式 `pl.<op>`（6275-6282），以及兜底报错（6284-6288，hint 列出全部合法前缀）。
- [ast_parser.py:8830-8846](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L8830-L8846)：`_parse_unified_op`——2 段形式只在 `pypto.language.op` 包里查可调用对象。docstring 明确：`pl.range`、`pl.dynamic`、`pl.const` 这类非算子符号不会被当成算子调用（它们在上游各自的人口被拦截）。
- [ast_parser.py:8313-8343](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L8313-L8343)：`_dispatch_op` 主体——模块上不存在该算子名则报 `InvalidOperationError`；否则解析参数后调 `invoke_dsl(op_func, args, kwargs, span)` 并挂 attrs。docstring 点明设计原则：**包装函数拥有类型检查与分发权，解析器只负责解析参数、桥接类型、钉 span**。

**桥接层**（本讲第二个精读文件）：

- [_dsl_invoker.py:49-105](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/_dsl_invoker.py#L49-L105)：`_wrap_arg` 的包装规则——`MakeTuple`（源码里列表字面量的解析期表示）拆回 Python list 让 shape/offset 类参数接受；`ConstInt` / `ConstFloat` **保持**为裸 Expr 以保住解析器选定的 dtype（若提前抽成 Python int，下游 builder 默认值会悄悄改掉结果 dtype，58-65 行注释举了 `tensor.muls` 的例子）；其余 Expr 按 `type` 字段查表包装成 Tensor / Tile / Scalar / Array / Ptr / CommCtx 等 DSL 类。85-86 行有个细节：`DistributedTensorType` 必须先于 `TensorType` 判断，因为 pybind 把它注册成了 `TensorType` 的 Python 子类——顺序错了分布式张量会被降级成普通 `Tensor` 包装。
- [_dsl_invoker.py:108-137](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/_dsl_invoker.py#L108-L137)：`_unwrap_result`——单对象直接 `.unwrap()`；多输出包装函数返回的 tuple 被识别并**还原出公共的 Call**（各元素是引用同一 tuple 的 `TupleGetItemExpr(call, i)`），供解析器的元组解包路径重绑临时变量。
- [_dsl_invoker.py:140-151](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/_dsl_invoker.py#L140-L151)：`invoke_dsl` 三步曲——包装参数、`use_parser_span(span)` 上下文里调用包装函数（IR builder 经此 contextvar 拿到调用点 span，u4-l7 的 Builder API 会再遇到它）、解包结果。模块 docstring（10-28 行）讲了这次设计的历史：过去解析器维护手写的分发表（`_TILE_SCALAR_OPS` / `_SCALAR_BINARY_OPS`），如今 `pl.add` 就是普通的 Python 属性查找 `pypto.language.op.add`，类型分发逻辑回到包装函数本体。

#### 4.3.4 代码实践

1. **实践目标**：跟随一次 `pl.add` 调用走完「拼写 → 路由 → 桥接」全链。
2. **操作步骤**：
   - 写一行 `c = pl.add(a, b)`（a、b 为 Tile），按 4.3.2 的流程图在代码里标注路径：`parse_expression`（5794）→ `parse_call`（6079）→ `parse_op_call`（6183）→ 命中 6275-6282 的 2 段统一分支 → `_parse_unified_op`（8830）→ `_dispatch_op`（8313）→ `invoke_dsl`（_dsl_invoker.py:140）。
   - 在 `_dsl_invoker.py` 的 `_wrap_arg` 里找到 Tile 参数命中哪一行（`isinstance(t, ir.TileType)` → 90 行），确认它被包装成 DSL `Tile` 后才进入 `unified_ops.add`。
   - 打印该函数 IR，确认 `pl.add(tile, tile)` 在 IR 里是 `Call(op="tile.add")`——分发发生在包装函数内部，解析器无从知晓也不需要知晓。
3. **观察现象**：IR 中算子名是 `tile.add`，与源码里的统一拼写 `pl.add` 不同。
4. **预期结果**：理解「源码拼写 ≠ IR 算子名」——统一拼写只是语法糖，真正的算子身份由包装函数按操作数类型决定（Tile+Tile → `tile.add`，Tile+标量 → `tile.adds`）。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`_wrap_arg` 为什么不把 `ConstInt` 抽成 Python `int` 再传给包装函数？什么时候又必须抽？

**答案**（_dsl_invoker.py:58-65 注释原文的复述）：`ConstInt` 携带解析器选定的 dtype（裸 `int` → `INDEX`、`pl.const` 指定的 dtype 原样保留），抽成 Python `int` 会丢掉这个信息，让下游 IR builder 的默认值接管（例如 `tensor.muls` 的 rhs 默认 `FP32`），悄悄改变结果 dtype。而「axis 类」参数（语义上是坐标而非数值）确实需要 Python `int`，此时包装函数自己通过 IR 层的 `ConstInt.value` 字段显式抽取——需要时取值，不需要时保型。

**练习 2**：如果把 `pl.builtin` 分支放在路由表的最后（2 段统一分支之后），会发生什么？

**答案**：`pl.builtin.tensor.barrier(...)` 的属性链是 `["pl", "builtin", "tensor", "barrier"]`，段数 ≥ 2 且 `attrs[1] == "builtin"` 不在 6279 行的排除名单（`tensor/tile/system/array/prefetch`）里，会先命中 2 段统一分支，把 `builtin` 当作算子名去 `pypto.language.op` 里查——查不到，报 `Unknown operation`，且报错不含任何 builtin 命名空间的拼写提示。这正是 commit 信息里描述的修复前症状（`Unknown operation 'pl.builtin'`），也是分支必须放在统一分支**之前**、且匹配 `attrs[1]` 即进入的原因。

---

### 4.4 pl.builtin.<ns>.<op> 的回读：打印-再解析往返一致

#### 4.4.1 概念说明

这是本次增量更新的核心（commit `78993964`，PR #2526）。

**问题**：Python 打印器把所有已注册的非 `pld` 算子都冠以 `pl.` 前缀输出。Pass 42（LowerHostTensorCollectives）把宿主侧集合通信 `pld.tensor.allreduce` 降级成内部的 `builtin.tensor.allreduce` 后，打印器输出的就是 `pl.builtin.tensor.allreduce(...)`。而解析器此前没有 `pl.builtin` 命名空间——这个调用掉进 2 段统一路径，报 `Unknown operation 'pl.builtin'`。也就是说：**打印器有一个「写者」，解析器却没有对应的「读者」**，Pass 42 之后的 IR 打印出来就再也读不回了。

**修复形态**：仿照代码库里已有的「仅打印器产生」形态的处理先例（`_parse_printed_alloc_call`、`pld.tile._remote_load_with_physical_tail_padding`），新增一条机器专用解析路径 `_parse_builtin_op`：

- 通过 `ir._create_internal_op_call` 构建（走 `OpRegistry::CreateInternal`，绕开 `internal_only` 的用户面创建守卫——守卫本身不动，用户仍不能凭 `create_op_call` 造内部算子）。
- **只**接受 `builtin.` 命名空间下真实注册的名字——`pl.builtin.tensor.write` 会被拒绝，因为 `tensor.write` 虽是真实算子但不在 `builtin.` 下注册。命名空间不会变成通往任意内部算子的后门。
- **只**接受打印器可能写出的形态：`device` attr 与覆盖每个位置参数的 `arg_directions` attr 是必填的。这两个不变量由编排代码gen在内部检查中读取；若解析器放行缺 attr 的手写调用，错误会推迟到代码gen才炸成「internal error」——把坏用户输入伪装成编译器 bug。前置校验让它在解析期就以用户错误报出。

**边界**（commit 的「reviewer note」如实记录）：修复后 Pass 42 之后的 IR **能解析**了，但整程序 `assert_structural_equal` 仍不成立——Pass 41（MaterializeCommDomainScopes）合成的 `CommDomainScopeStmt` 被打印器故意打成注释（`# pld.comm_domain: ...`），再解析回来会退化为普通 `SeqStmts`；`DistributedTensorType` 的 `window_buffer` 回引则完全不打印。所以相关测试固定用 `BEFORE_AND_AFTER` 验证模式而非默认的往返模式。给作用域一个可解析的表面是个悬而未决的设计题。

#### 4.4.2 核心流程

```text
打印 → 再解析 往返:
  P (含 pld.tensor.allreduce)
    → passes 41/42 降级
    → ir.python_print(P)   →  文本 "... pl.builtin.tensor.allreduce(x, attrs={...}) ..."
    → pl.parse_program(文本)
        → exec → @pl.program → ASTParser
        → parse_op_call 命中 pl.builtin 分支 (ast_parser.py:6235)
        → _parse_builtin_op(["tensor", "allreduce"], call)   (ast_parser.py:8713)
            ① 段数必须恰为 2 → 否则报「命名空间自己的」拼写诊断
            ② ir.is_op_registered("builtin.tensor.allreduce") → 否则 Unknown builtin operation
            ③ 解析 args / kwargs / attrs
            ④ _check_builtin_op_printer_invariants:
                 device attr 必在; arg_directions 必在; 两者长度一致
            ⑤ ir._create_internal_op_call(full_name, args, kwargs, span)
            ⑥ _attach_op_attrs 挂回 attrs
    → 得到与原 IR 结构等价的 Program
```

#### 4.4.3 源码精读

- [ast_parser.py:6230-6236](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L6230-L6236)：`parse_op_call` 里的 builtin 分支——注释写明动机：「按 `attrs[1]` 单独匹配，让畸形拼写得到命名空间自身的诊断，而不是掉进 2 段统一路径报出无用的 `Unknown operation 'pl.builtin'`」。
- [ast_parser.py:8713-8742](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L8713-L8742)：`_parse_builtin_op` 的 docstring——交代了 builtin 算子的身世（`internal_only`、由 `LowerHostTensorCollectives` 合成）、这条路存在的原因（「print → parse 往返必须对流水线能产生的每一种 IR 成立」）、以及「它是打印器的机器专用读者」这一定位。8733-8742 一段值得整段细读：*因为它是打印器的读者，所以它只接受打印器能写出的东西*——这是整条路径的设计纲领。
- [ast_parser.py:8744-8779](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L8744-L8779)：方法主体——①段数校验（8745-8752，hint 给出正确拼写与「用 `pld.tensor.*` 代替」的建议）；②注册表查询（8755-8762）；③参数解析与不变量校验（8764-8767）；④构建（8769，`ir._create_internal_op_call`）；⑤错误包装（8770-8778，`BUG_CLASS_EXCEPTIONS` 原样上抛——编译器 bug 不该被改写成用户错误；其余异常包上 `InvalidOperationError` 并带 span）。
- [ast_parser.py:8781-8821](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/parser/ast_parser.py#L8781-L8821)：`_check_builtin_op_printer_invariants`——三条前置校验：`device` 必在（编排代码gen靠它解析发射 rank）、`arg_directions` 必在、且条数等于位置参数个数。docstring 引用了代码gen侧的依据（`EmitBuiltinWindowCollectiveDispatch` 的 INTERNAL_CHECK）：接受一个打印器写不出的调用，等于把坏输入推迟成编译器 bug 诊断。
- [tests/ut/ir/parser/test_builtin_namespace.py:55-76](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/parser/test_builtin_namespace.py#L55-L76)：测试的素材——宿主编排函数模板与打印器实际输出的 barrier 形态（第 76 行）：`pl.builtin.tensor.barrier(signal, attrs={"device": r, "arg_directions": [pl.adir.inout]})`。这一行就是「写者写什么」的权威样例。
- [tests/ut/ir/parser/test_builtin_namespace.py:79-111](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/parser/test_builtin_namespace.py#L79-L111)：前两个用例——①解析产物确实是注册表里的内部算子（`call.op.name == ir.get_op("builtin.tensor.barrier").name`，注意这里按名字比较，正是 u1-l3 提过的「按名不按指针」不变量）；②机器属性 `attrs={...}` 完整往返：`device` 是名为 `r` 的 `ir.Var`，`arg_directions` 是 `[ArgDirection.InOut]`。
- [tests/ut/ir/parser/test_builtin_namespace.py:114-135](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/parser/test_builtin_namespace.py#L114-L135)：命名空间封闭性——未注册的 `builtin.not_a_collective` 与「真实存在但不在 builtin 下」的 `builtin.write`（即 `tensor.write`）都被拒绝；段数错误（`pl.builtin.barrier`）的报错里含完整拼写与 hint。
- [tests/ut/ir/parser/test_builtin_namespace.py:148-175](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/parser/test_builtin_namespace.py#L148-L175)：三条不变量的负向用例——缺 `device`、缺 `arg_directions`、条数不匹配，各自被解析期用户错误拦截；148-156 的 docstring 把「为什么必须前置」讲得最清楚：代码gen在 `INTERNAL_CHECK` 后面读 `device`，放行缺 attr 的调用会把坏输入变成编译器 bug 诊断。
- [tests/ut/ir/parser/test_builtin_namespace.py:178-198](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/parser/test_builtin_namespace.py#L178-L198)：公共表面不变——用户写 `pld.tensor.barrier`，解析出的 IR 在 Pass 跑之前仍是 `pld.tensor.barrier`，内部名不提前出现。

#### 4.4.4 代码实践

1. **实践目标**：完成一次真实的「打印 → 再解析」往返，并理解它在哪里会破、为什么。
2. **操作步骤**：
   - 运行现成测试感受绿灯：`python -m pytest tests/ut/ir/parser/test_builtin_namespace.py -v`（需先按 u1-l2 构建好环境并 `source .claude/skills/testing/load-env.sh`，测试并行度用 `PYPTO_TEST_JOBS`，不要裸用 `-n auto`）。
   - 把测试第 76 行的打印形态抄进自己的脚本，手动构造往返（示例代码）：

     ```python
     import pypto.language as pl
     from pypto.pypto_core import ir

     src = open("printed_barrier.py").read()   # 内容为 test 里 _barrier_program(...) 产生的程序文本
     program = pl.parse_program(src)
     print(ir.python_print(program))
     ```

   - 对照输出与输入：逐行确认 `pl.builtin.tensor.barrier(signal, attrs={"device": r, "arg_directions": [pl.adir.inout]})` 原样再现。
   - 再做一个破坏实验：把文本里的 `pl.adir.inout` 列表改成空表 `[]`，重新 `pl.parse_program`，观察报错。
3. **观察现象**：改空表后解析期立即抛 `InvalidOperationError`，报错提到 `arg_directions entries` 与位置参数个数不符，且 hint 指向 `pld.tensor.barrier` 公共形式。
4. **预期结果**：往返一致成立；违反「打印器不变量」的输入在解析期（而非代码gen期）被拒绝为用户错误。
5. 待本地验证（需要已构建的 pypto_core 扩展）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_parse_builtin_op` 用 `ir._create_internal_op_call` 而不是普通的 `create_op_call`？

**答案**：`builtin.*` 算子在注册表里标记为 `internal_only`。`create_op_call` 走 `CreateUserFacing` 创建路径，会撞上 internal_only 守卫而拒绝——这是保护用户面 API 不被内部算子穿透的既有防线。`_create_internal_op_call` 走 `CreateInternal`，是编译器自用的创建通道。守卫本身未被触碰：用户无论通过哪条 DSL 路径仍然造不出 `builtin.*` 算子，只有「读回打印器输出」这一条机器通道可以。

**练习 2**：修复之后，Pass 42 输出的 IR 整程序往返相等了吗？为什么？

**答案**：没有。能**解析**不等于**结构相等**：上游 Pass 41 合成的 `CommDomainScopeStmt` 被打印器有意打成 `# pld.comm_domain:` 注释（再解析退化为 `SeqStmts`），`DistributedTensorType` 的 `window_buffer` 回引不打印（再解析回来是 `window_buffer is None`）。因此 `test_lower_host_tensor_collectives.py` 的断言对再解析一侧放宽了 `window_bound_args=False`，且该测试模块固定用 `BEFORE_AND_AFTER` 验证模式。这属于「打印器有写者、解析器缺读者」问题在上一个 Pass 的残留，留待后续设计。

**练习 3**：如果一个新 Pass 也合成 internal_only 算子（比如注册在 `builtin.system.*` 下），解析器需要改什么？

**答案**：什么都不用改——只要算子真的注册在 `builtin.` 命名空间下，`_parse_builtin_op` 的注册表查询（`ir.is_op_registered("builtin.system.<op>")`）就能找到它，打印器输出的 `pl.builtin.system.<op>(...)` 自动可读回。需要检查的只有一点：新 Pass 合成调用时是否也无条件盖上了 `device` 与逐参数的 `arg_directions`（即是否同样经由「单一构造点」如 `MakeBuiltinCallWithAttrs`）；如果新算子的 attrs 集合不同，`_check_builtin_op_printer_invariants` 的三条校验才需要对应调整。

---

## 5. 综合实践

把本讲四个模块串成一个完整的「解析器导览」实验。

**任务**：制作你自己的「Python 语法 → IR 节点」映射注释版。

1. 写一个综合算子，同时包含：`for i in pl.range(...)` 循环、`if/else` 分支（一个分支带 `pl.yield_`、一个不带）、至少两次不同拼写的算子调用（`pl.add` 与 `pl.tile.add` 各一次）、一次标量二元运算（如 `i * 2 + 1`）。
2. **语句层**：在 `ast_parser.py` 的 `parse_statement` 分发表（1307-1328）旁列出你的每条语句命中哪个分支、进入哪个处理函数、产出哪种 IR 语句。
3. **表达式层**：为每个算子调用标注 `parse_op_call` 命中的分支号（按 4.3.2 流程图的编号），并写出它的最终 IR 算子名（`tile.add`？`tile.adds`？）。
4. **往返验证**：`ir.python_print` 打印该程序，把输出喂给 `pl.parse_program`，再打印一次，两次输出逐行 diff。
5. **builtin 对照**：阅读 `tests/ut/ir/parser/test_builtin_namespace.py` 的第 76 行，把它与你第 4 步的输出对比——你的算子输出里没有 `pl.builtin.*`（因为没跑 Pass 42），这正是「打印形式随流水线阶段演化、解析器必须全都能读」的含义：同一个解析器，既读你手写的 DSL，也读流水线中段机器产出的 DSL。
6. **预期结果**：一份映射注释表 + 一份零 diff 的往返记录。若 `pl.yield_` 分支的往返出现差异（如类型注解重排），记录差异并到 `docs/en/dev/language/00-python_syntax.md` 里找打印规范解释它。

## 6. 本讲小结

- PyPTO DSL 的解析有两条入口——装饰器入口（`@pl.function`/`@pl.program` 用 inspect 抓源码）与文本入口（`pl.parse`/`pl.parse_program` 用 exec 驱动装饰器）——但只有**一个**解析器 `ASTParser`：入口负责拿源码，解析器负责翻译。
- `parse_statement` 的 isinstance 分发表就是「Python 语法 → IR 节点」的映射总纲：`for`→`ForStmt`（`_ITERATOR_TO_KIND` 决定 ForKind）、`if`→`IfStmt`（有无 `pl.yield_` 决定 phi 还是变量泄漏给 ConvertToSSA）、不认识的语句直接报错并列出支持清单。
- `parse_op_call` 按属性链段数与命名空间路由一切算子调用；路由到目标后，解析器不造 `Call` 节点，而是经 `invoke_dsl` 把 `ir.Expr` 包装成 DSL 类型、调用包装函数、再解包——类型检查与 Tile/标量分发权都住在包装层。
- 本次更新（PR #2526）补上了打印器的「读者」：`pl.builtin.<ns>.<op>` 分支 + `_parse_builtin_op`，经 `ir._create_internal_op_call` 构建、封闭在 `builtin.` 注册命名空间内、前置校验 `device`/`arg_directions` 不变量，使 Pass 42 之后的 IR 重新可解析。
- 「打印 → 再解析」不是全有或全无：`CommDomainScopeStmt` 打成注释、`window_buffer` 不打印，这两处上游缺口让整程序结构相等仍不成立——测试因此固定在 `BEFORE_AND_AFTER` 模式。
- 排查解析问题的方法：拿报错里的语句/表达式类型名或算子拼写，到 `parse_statement` / `parse_expression` / `parse_op_call` 三张表里反查命中（或未命中）的分支。

## 7. 下一步学习建议

- **下一讲（u3-l2）**将讲函数种类与调用约定——`@pl.function` 的 `type=`/`level=`/`role=` 参数如何决定 Inline / InCore / Opaque 身份，正是本讲 `parse_function`（ast_parser.py:980）里那几个形参的去向。
- 想系统了解 IR 打印（写者一侧）与 Builder API，提前翻阅 `docs/en/dev/ir/06-builder.md` 与 `docs/en/dev/ir/07-parser.md`；u4-l7 会把「构造 → 打印 → 解析」三角完整讲一遍。
- 语法规范的权威文本是 `docs/en/dev/language/00-python_syntax.md`（表达式与类型）与 `01-statements.md`（语句与控制流），本讲遇到的所有映射在那里都有正式文法。
- Pass 42 的降级细节（`pld.tensor.*` 如何变成 `builtin.tensor.*`）见 `docs/en/dev/passes/42-lower_host_tensor_collectives.md` 新增的 "Printed form" 一节；分布式编程将在 u7-l2 展开。
