# AST 到 HIR：ast2hir

## 1. 本讲目标

本讲是「编译前端」单元的第三讲，承接 u5-l2 的 `compile_tile` 流水线——在那里我们知道整条流水线的第二步就是 `get_function_hir`，它「只做一次、与 signature 无关」。本讲要拆开这个黑盒，看清 cuTile 是如何把一段 Python 源码变成它自己的高层中间表示（HIR）的。

学完后你应该能够：

- 说清 **HIR 是什么**：它是一种「一切皆函数调用」的中间表示，没有专门的 Operation 定义，用通用的 `Call` 表达算术、控制流、变量读写等一切操作。
- 掌握 HIR 的核心数据结构 **`hir.Value` / `Operand` / `Call` / `Block` / `Function`**，以及它们各自的字段含义。
- 理解 **`get_function_hir`** 的入口流程：取源码、`if True:` 缩进小技巧、构造 `_Context`、按 AST 节点类型分派翻译。
- 理解 **`ast_get_all_local_names`** 这一步作用域预扫描为什么必须在翻译之前完成，以及名字是如何被编码成 `ResolvedName(depth, index)` 的。
- 能手算/打印一个含 `for` + `if` 的内核的 HIR Block/Call 结构，标出每个块的 `jump` 与 `stored` 变量。

## 2. 前置知识

本讲默认你已经掌握以下概念（在前置讲义中已建立）：

- **Python AST（抽象语法树）**：Python 标准库 `ast` 把源码解析成一棵由 `ast.FunctionDef`、`ast.For`、`ast.If`、`ast.BinOp`、`ast.Name` 等节点组成的树。本讲大量出现这些节点类型。
- **load–compute–store 范式与控制流子集**（u3-l1、u3-l3）：tile code 是「被翻译的 Python」，`if/for/while` 会被翻译成结构化 IR；本讲正是这一翻译的**最前端**实现。
- **`static_eval` / `static_assert`**（u3-l5）：它们在 ast2hir 阶段被特殊分派（不走普通 tile 翻译），本讲会看到这个分派入口。
- **`compile_tile` 流水线**（u5-l2）：`get_function_hir` 产出的 `hir.Function` 被 `_IrKeeper` 持有，随后由 `hir2ir` 拉动；本讲是这条链路的源头。
- **SSA 风格的值**（u5-l5 预告）：HIR 里的每个计算结果都用一个带 id 的 `Value`（写作 `%id`）表示，一次定义、按引用使用，这是后续 Tile IR SSA 风格的前身。

一个关键直觉：**HIR 不是 Tile IR**。HIR 更接近「结构化的 Python」，只做了一层薄薄的抽象——把 Python 的运算符、控制流关键字统一成「调用某个 Python 可调用对象」。真正的、带具体 Operation 定义的 Tile IR 是下一阶段 `hir2ir`（u5-l4）才产生的。所以 HIR 的 `Call` 里会出现 `operator.add`、`getattr`、`slice` 这些**普通的 Python 函数对象**作为被调用者（callee），这一点请先记住，后面会反复用到。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/cuda/tile/_ir/hir.py` | HIR 的数据结构定义：`Value`、`Operand`、`Call`、`Jump`、`Block`、`ResolvedName`、`Function`。本讲的「词汇表」。 |
| `src/cuda/tile/_passes/ast2hir.py` | 翻译器本体：入口 `get_function_hir`、上下文 `_Context`、表达式/语句分派表 `_expr_handlers`/`_stmt_handlers`，以及 `if/for/while` 等控制流的翻译函数。本讲的「主角」。 |
| `src/cuda/tile/_passes/ast_util.py` | 作用域预扫描 `ast_get_all_local_names`，在翻译前一次性收集函数里所有局部变量名。本讲的「先行侦察兵」。 |
| `src/cuda/tile/_ir/hir_stubs.py` | HIR 里出现的「内置被调用者」的占位声明：`if_else`、`loop`、`static_foreach`、`build_tuple`、`load_var`、`store_var`、`identity` 等。理解 HIR 文本的钥匙。 |
| `src/cuda/tile/_compile.py` | 调用方：`compile_tile` 在第 472 行调用 `get_function_hir`，把结果交给 `_IrKeeper`。 |
| `experimental/cuda-lang/test/test_hir.py` | 一个可参考的「打印 HIR」范例：`str(func_hir.body)` 把 Block 打印成文本，本讲代码实践借鉴它。 |

> 说明：`experimental/cuda-lang` 是另一个实验语言包，**不是本 `cuda.tile` 手册的范围**，但它的 `test_hir.py` 恰好示范了如何把 HIR 打印出来做 FileCheck，我们只借用这一个用法。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

1. **HIR 设计哲学：一切皆 `Call`**——先建立 HIR 的直觉。
2. **`get_function_hir`：翻译入口与分派机制**——看清翻译从哪里开始、怎么把节点路由到处理器。
3. **`Function` / `Block` / `Call` 数据结构与 `ResolvedName`**——把词汇表逐字段讲透。
4. **控制流的 HIR 翻译：`if` / `for` / `while`**——这是综合实践要画的图的核心。
5. **`ast_get_all_local_names`：作用域预扫描**——为什么翻译前要先扫一遍名字。

### 4.1 HIR 设计哲学：一切皆 `Call`

#### 4.1.1 概念说明

HIR（High-level Intermediate Representation，高层中间表示）是 cuTile 从 Python AST 构造出来的**第一层**中间表示。它的设计哲学写在 [hir.py:L6-L14](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L6-L14) 的模块注释里，核心有两点：

- **没有专门的 Operation 定义**。不同于后面的 Tile IR（每个操作是一个具体的 Op 类），HIR 用「函数调用」这一个概念建模所有操作。比如 `a + b` 在 HIR 里就是「调用 `operator.add(a, b)`」。
- **常量的表示更简单**：常量既可以作为参数直接出现，也可以作为被调用函数直接出现。

这种设计的好处是：**翻译器不需要为每种 Python 语法发明一个 IR 节点**。绝大多数表达式只需查一张「AST 运算符 → Python 运算符函数」的映射表（如 `_binop_map`），然后发出一个 `Call` 即可。语义的真正落地（`operator.add` 到底对应哪条 Tile IR 指令）推迟到 `hir2ir` 阶段。

#### 4.1.2 核心流程

HIR 的最小积木是 `Value` 和 `Call`：

- 每个计算产出一个新的 `Value`（带一个全局递增的 id，打印为 `%id`）。
- 一个 `Call` 描述「用哪些参数调用谁、结果存到哪个 `Value`」。
- 一串 `Call` 按顺序装进一个 `Block`；`Block` 末尾挂一个 `Jump` 表示控制流走向（继续 / 跳出循环 / 返回 / 分支结束）。

至于「参数」和「被调用者」到底能装什么，由 `Operand` 类型规定（见 4.1.3）。

#### 4.1.3 源码精读

**`Value`** 是一个冻结的 dataclass，仅持有一个 id，`__str__` 把它打印成 `%id`：

[hir.py:L27-L32](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L27-L32) —— `Value` 定义。id 小于 2000 的 `Value` 会被 `make_value` 缓存复用（[hir.py:L40-L54](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L40-L54)），避免无谓的对象创建。

**`Operand`** 是整个 HIR 最关键的类型别名：

[hir.py:L57-L62](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L57-L62) —— `Operand = Value | Any`。

这段注释说清了 HIR 的「两种操作数」约定，务必记住：

- 如果一个操作数是 `Value` 实例 → 它代表**之前某次 `Call` 的结果，或一个内核参数**（即「引用」）。
- 如果是任何其它类型的对象 → 它是一个**立即常量（immediate constant）**，被原样嵌入 HIR。

这就解释了为什么 HIR 的 `Call` 里能直接出现 `operator.add`（一个 Python 函数对象，作为 callee）、字符串属性名、`ResolvedName`、`StaticEvalExpression` 等「奇奇怪怪」的东西——它们都是立即常量，不需要预先声明。

**`Call`** 把上面三者合在一起：

[hir.py:L73-L79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L73-L79) —— `Call` 字段：`result`（结果 `Value`，可为 `None` 表示无返回值）、`callee`（被调用者，`Operand`）、`args`（位置参数元组，元素可为 `Operand` 或 `Starred`）、`kwargs`（关键字参数元组）、`loc`（源码位置）。`Call.__str__`（[hir.py:L81-L93](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L81-L93)）负责把一条 Call 打印成 `%3 = <fn:add>(%1, %2)  # Line 7` 这样的文本；它对 `identity` 这个 callee 做了特判（直接打印成 `%3 = %2`），所以字面量常量的 HIR 看起来很干净。

**`Jump`** 用一个枚举表达块尾的控制流走向：

[hir.py:L96-L100](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L96-L100) —— `END_BRANCH`（if/else 分支结束）、`CONTINUE`（循环继续下一轮）、`BREAK`（跳出循环）、`RETURN`（函数返回）。

最后看一个体现「一切皆 Call」的典型例子——**字面量常量**怎么翻译。在 [ast2hir.py:L545-L549](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L545-L549) 里，`ast.Constant` 不是直接返回 `node.value`，而是包了一层 `identity` 调用，**目的是保留源码位置信息**（`loc`）。这正是「常量也是 Call」的体现。

#### 4.1.4 代码实践

**目标**：直观感受「一切皆 Call」。

**步骤**：

1. 想象内核里有一行 `x = a + b * 2`。
2. 不运行，先在纸上按「运算符 → Python 函数」的思路把它拆成 HIR 的 `Call` 序列（提示：`b * 2` 先算，`2` 是常量需要 `identity`，`a`/`b` 是变量需要 `load_var`，乘/加分别是 `operator.mul` / `operator.add`，赋值是 `store_var`）。
3. 然后运行下面这段「观察型」脚本（纯 Python，不需要 GPU）来对照你的猜测：

```python
# 示例代码：打印一段普通 Python 函数的 HIR
from cuda.tile._passes.ast2hir import get_function_hir

def demo(a, b):
    x = a + b * 2
    return x

func_hir = get_function_hir(demo, entry_point=True)
print(func_hir.body)
```

**需要观察的现象**：输出里每个变量读取都是一次 `load_var(...)` 调用，每个赋值都是一次 `store_var(...)` 调用，`+` / `*` 分别显示为对加/乘函数的调用，常量 `2` 通过 `identity` 出现。

**预期结果**：你会看到类似下面结构的文本（`%N` 的具体编号待本地验证）：

```
^0():
    %a = load_var(...)        # 读 a
    %b = load_var(...)        # 读 b
    %two = <fn:identity>(2)   # 常量 2
    %mul = <fn:mul>(%b, %two) # b * 2
    %sum = <fn:add>(%a, %mul) # a + (b*2)
    store_var(..., %sum)      # x = ...
    return
```

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ast.Constant` 要包一层 `identity` 调用，而不是直接把 `node.value` 当操作数返回？

**答案**：为了给这个常量挂上源码位置（`loc`）。直接返回 `node.value` 会丢失位置信息，而后续报错、调试信息都依赖 `loc`。包成 `Call` 就能携带 `ctx.current_loc`。参见 [ast2hir.py:L545-L549](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L545-L549) 的注释。

**练习 2**：HIR 的 `Call.callee` 是 `Operand` 类型，意味着它可以是一个 `Value`，也可以是任意 Python 对象。各举一个实际出现的例子。

**答案**：callee 是 `Value` 的例子：`x = f()` 中 `f` 是局部变量，先 `load_var` 得到 `%f`，再 `%r = %f()`。callee 是 Python 对象的例子：`a + b` 中 callee 直接是 `operator.add` 这个函数对象（立即常量）。

---

### 4.2 `get_function_hir`：翻译入口与分派机制

#### 4.2.1 概念说明

`get_function_hir` 是 ast2hir 模块的唯一对外入口（被 `_compile.py` 和 `hir2ir.py` 调用）。它接收一个 Python 函数对象，返回一棵 `hir.Function`。它的职责可以概括为三步：

1. **取源码并解析成 AST**——这里有一个处理缩进的经典小技巧。
2. **建立翻译上下文 `_Context`**——收集全局变量、局部变量名、id 分配器等。
3. **按节点类型分派翻译**——用两张注册表把每种 AST 节点路由到对应的处理函数。

它用 `@lru_cache` 装饰（[ast2hir.py:L24](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L24)），所以同一个函数的 HIR 只计算一次——这正是 u5-l2 所说的「`get_function_hir` 只做一次、与 signature 无关」的实现原因。

#### 4.2.2 核心流程

```
get_function_hir(pyfunc, entry_point)
  ├── 解包 @function 装饰器（is_function_wrapper）
  ├── inspect.findsource + getblock 取出函数源码行
  ├── 用 "if True:\n " 包一层再 ast.parse（修正缩进）
  ├── _fix_line_and_column_numbers 修正行号/列号
  ├── 收集 func_globals（含 builtins、闭包变量）
  ├── ast_get_all_local_names 预扫描局部变量名      ← 模块 5 详讲
  ├── 构造 _Context（持有局部名、id 分配器、当前块等）
  └── _get_function_hir_inner
        ├── _ast2hir 递归翻译函数体 → 根 Block
        └── 组装 hir.Function
```

翻译过程的「路由」靠两张表：`_expr_handlers`（表达式）和 `_stmt_handlers`（语句），用 `_register` 装饰器填充（[ast2hir.py:L291-L295](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L291-L295)）。`_expr` / `_stmt` 这两个总分派函数（[ast2hir.py:L615-L619](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L615-L619) 与 [ast2hir.py:L1162-L1165](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1162-L1165)）按 `type(node)` 查表，找不到就调用 `_unsupported_*` 抛 `TileSyntaxError`。

#### 4.2.3 源码精读

**入口取源码**（[ast2hir.py:L24-L62](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L24-L62)）：先用 `inspect.findsource`（而不是 `getsourcelines`，因为后者会展开装饰器）取源码行，再 `inspect.getblock` 截出整个函数。

**`if True:` 缩进小技巧**（[ast2hir.py:L36-L62](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L36-L62)）：内核函数可能写在类体、`if` 块里，带有额外缩进；如果直接喂给 `ast.parse` 会因 `textwrap.dedent` 处理不了续行（如未缩进的 `200)`）而失败。cuTile 的办法是**主动加一层缩进**，再用 `if True:` 包起来解析，最后用 `_fix_line_and_column_numbers`（[ast2hir.py:L86-L98](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L86-L98)）把行号/列号还原回原文件。

**收集全局与闭包变量**（[ast2hir.py:L64-L69](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L64-L69)）：`func_globals` 由 `pyfunc.__builtins__` 与 `pyfunc.__globals__` 合成，再加上 `pyfunc.__closure__` 里的自由变量。这些构成了「冻结全局（frozen globals）」——翻译期可见但假定编译后不变的宿主对象（如 `operator`、`range`、`ct`、用户在内核外 import 的东西）。

**构造 `_Context` 并翻译**（[ast2hir.py:L71-L81](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L71-L81)）：先用 `ast_get_all_local_names` 拿到局部名集合，构造 `_Context`，调用 `_get_function_hir_inner` 完成翻译，最后 `_finalize_func` 计算闭包捕获关系。

**`_Context`** 是贯穿整个翻译的「工作台」（[ast2hir.py:L159-L193](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L159-L193)）。它持有：

- `value_id_sequence` / `block_id_sequence`：`Value` 与 `Block` 的 id 分配器；
- `current_block`：当前正在填充的 `Block`；
- `name_to_local_idx`：局部名 → 局部索引的映射；
- `parent_loops`：当前所在的循环类型栈（用于校验 `break`/`continue`/`return` 是否合法）；
- `frozen_globals`、`local_names`、`_outer_rns`（外层捕获）等作用域信息。

它提供的核心动作有：`call` / `call_void`（发出一条 `Call`，[ast2hir.py:L236-L242](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L236-L242)）、`store` / `load`（变量读写，详见 4.3）、`new_block`（开一个新块，[ast2hir.py:L220-L234](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L220-L234)）。

**分派翻译的典型例子——二元运算**（[ast2hir.py:L482-L489](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L482-L489)）：`a + b` 命中 `ast.BinOp` 处理器，查 `_binop_map`（[ast2hir.py:L472-L479](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L472-L479)）把 `ast.Add` 映射成 `operator.add`，递归翻译左右操作数，发出 `ctx.call(operator.add, (lhs, rhs))`。一行 `a + b` 就变成了一条 HIR Call。

#### 4.2.4 代码实践

**目标**：体会分派表的工作方式。

**步骤**：

1. 打开 [ast2hir.py:L301](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L301) 与 [ast2hir.py:L632](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L632)，数一下 `@_register(_expr_handlers, ...)` 和 `@_register(_stmt_handlers, ...)` 各有多少个，列出它们支持的 AST 节点类型。
2. 思考：如果用户在内核里写了一个 `try/except`，会走到哪条路径？

**需要观察的现象**：`_expr_handlers` 覆盖 `Call/Name/UnaryOp/BinOp/Compare/Attribute/Constant/JoinedStr/Tuple/Subscript/Slice/Lambda/BoolOp/IfExp` 等；`_stmt_handlers` 覆盖 `Assign/AnnAssign/AugAssign/Expr/For/While/If/Continue/Break/Return/With/Pass/FunctionDef`。

**预期结果**：`try/except`（`ast.Try`）不在表里，会落到 `_unsupported_stmt`（[ast2hir.py:L1158-L1159](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1158-L1159)），抛出 `Unsupported syntax` 错误——这对应 u3-l3 所说的「tile code 只支持 Python 控制流子集」。

#### 4.2.5 小练习与答案

**练习 1**：`get_function_hir` 为什么用 `inspect.findsource` 而不是 `inspect.getsourcelines`？

**答案**：因为 `getsourcelines` 会自动展开装饰器，而 cuTile 需要原始的、包含 `@ct.kernel` 等装饰器的源码片段来做行号定位。注释见 [ast2hir.py:L30-L32](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L30-L32)。

**练习 2**：`_expr` 函数（[ast2hir.py:L615-L619](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L615-L619)）除了查表分派，还做了一件什么重要的事？

**答案**：它用 `with ctx.change_loc(expr)` 把「当前源码位置」切换到该表达式节点，这样发出去的 `Call.loc` 才能正确指向源码。位置信息会一路传到 Tile IR 与调试信息。

---

### 4.3 `Function` / `Block` / `Call` 数据结构与 `ResolvedName`

#### 4.3.1 概念说明

4.1 讲了 `Value` / `Operand` / `Call` / `Jump` 这几个「原子」，本模块把它们装进容器：`Block` 装一串 `Call` 加一个 `Jump`；`Function` 装函数体 `Block` 加各种元数据。此外还有一个关键概念 `ResolvedName`——它是 HIR 用来**精确定位一个名字到底指代哪个变量**的编码，是理解 `load_var` / `store_var` 的钥匙。

#### 4.3.2 核心流程

一个 `hir.Function` 的内部结构如下：

```
hir.Function
  ├── desc: FunctionDesc          # 函数名、文件、行号、是否入口
  ├── body: Block                 # 函数体根块
  ├── signature: inspect.Signature
  ├── local_names: tuple[str,...] # 本函数所有局部变量名（按 local index 排序）
  ├── param_local_indices         # 每个参数在 local_names 中的下标
  ├── frozen_global_names/values  # 冻结全局的名字与值
  ├── nested_functions            # 直接嵌套的子函数（闭包）
  ├── captures_by_depth           # 捕获了哪些外层局部变量
  └── enclosing_funcs             # 外层函数链
```

`Block` 内部：

```
hir.Block
  ├── block_id: int
  ├── params: tuple[Value,...]    # 块参数（如循环体的归纳变量）
  ├── calls: list[Call]           # 块内顺序执行的调用
  ├── jump: Jump | None           # 块尾跳转
  ├── result: Operand             # 块的产出值（分支/返回时用）
  └── stored_indices: set[int]    # 本块（及子块）写过的局部变量下标
```

#### 4.3.3 源码精读

**`Block`** 定义见 [hir.py:L103-L126](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L103-L126)。注意 `stored_indices` 这个字段——它记录「这个块里有哪些局部变量被赋值过」。当 `ctx.store(...)` 被调用时（[ast2hir.py:L254-L263](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L254-L263)），除了发出 `store_var` 调用，还会把该变量的 local index 加入 `current_block.stored_indices`（除非是列表推导的归纳变量）。更巧妙的是，`new_block` 退出时会**把子块的 `stored_indices` 并入父块**（[ast2hir.py:L233-L234](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L233-L234)），所以根块的 `stored_indices` 实际上是整棵函数所有赋值的并集。这个集合后续会被 `hir2ir` 用来决定哪些变量需要做 phi 合并（循环携带值）。

**`ResolvedName`** 定义与注释见 [hir.py:L129-L149](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L129-L149)。它用 `(depth, index)` 二元组编码一个名字的归属：

| `depth` | 含义 | `index` 指向 |
| --- | --- | --- |
| `-1` | 全局（冻结）变量 | `Function.frozen_global_names` |
| `(-1, -1)` | 没找到（`UNKNOWN_NAME`） | — |
| `0 ≤ depth < 本函数 depth` | 外层函数的捕获局部 | `enclosing_funcs[depth].local_names` |
| `depth == 本函数 depth` | 本函数的局部 | `Function.local_names` |

这个编码正是 `ctx.load` / `ctx.store` 的核心。看 [ast2hir.py:L265-L274](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L265-L274) 的 `load`：依次在「本函数局部 → 外层捕获 → 冻结全局」里查名字，都没命中就用 `UNKNOWN_NAME`，然后发出 `load_var(resolved_name, name)`——注意它**同时把 `ResolvedName` 和原始字符串名都传进去**，字符串名主要用于报错信息。`store`（[ast2hir.py:L254-L263](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L254-L263)）类似，但额外禁止给 global/nonlocal 赋值。

`load_var` / `store_var` / `if_else` / `loop` 等 callee 都在 [hir_stubs.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir_stubs.py) 里用 `@stub` 占位声明（如 [hir_stubs.py:L46-L51](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir_stubs.py#L46-L51)），它们只是「占位符」，真正的语义在 hir2ir 阶段实现。

**`Function`** 定义见 [hir.py:L152-L195](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L152-L195)，字段含义如上面流程图所示。其中 `captures_by_depth` 是 `_finalize_func` 算出来的（[ast2hir.py:L113-L125](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L113-L125)）：它遍历函数体里所有 `load_var` 调用，收集其中指向「本函数之外」的 `ResolvedName`（[ast2hir.py:L101-L110](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L101-L110)），从而知道嵌套函数捕获了哪些外层变量——这是实现闭包的基础。

#### 4.3.4 代码实践

**目标**：观察 `ResolvedName` 与 `stored_indices`。

**步骤**：

```python
# 示例代码：观察一个含闭包的函数的 HIR 字段
from cuda.tile._passes.ast2hir import get_function_hir

def outer(scale):
    def helper(x):
        return x * scale      # scale 是从 outer 捕获的
    return helper(2)

func_hir = get_function_hir(outer, entry_point=True)
print("== body ==")
print(func_hir.body)
print("== nested funcs ==", len(func_hir.nested_functions))
if func_hir.nested_functions:
    nf = func_hir.nested_functions[0]
    print("nested local_names:", nf.local_names)
    print("nested captures_by_depth:", nf.captures_by_depth)
```

**需要观察的现象**：`outer` 里出现的 `scale`，在内层 `helper` 的 HIR 中应表现为一个 `depth < helper 自身 depth` 的 `ResolvedName`，并出现在 `captures_by_depth` 里。

**预期结果**：`captures_by_depth` 是按外层 depth 分组的「捕获局部 index 列表」，非空即说明闭包捕获发生。具体编号待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`ctx.store` 为什么要把变量 index 加入 `stored_indices`，而这个集合又为什么要在 `new_block` 退出时并入父块？

**答案**：`stored_indices` 记录「哪些局部变量在这个块及其子块里被改写」，供 hir2ir 判断哪些变量在控制流汇合点（如循环回边、if 合流）需要 phi 合并。子块是父块的一部分，所以子块的写自然要冒泡到父块；这样根块的 `stored_indices` 就是整函数的写并集。参见 [ast2hir.py:L233-L234](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L233-L234) 与 [ast2hir.py:L262-L263](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L262-L263)。

**练习 2**：`load_var` 调用里同时传了 `ResolvedName` 和原始名字字符串，二者各起什么作用？

**答案**：`ResolvedName` 是程序化定位（hir2ir 据此取到正确的值/槽位），名字字符串是给人看的（用于报错、调试信息）。参见 [hir_stubs.py:L50-L51](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir_stubs.py#L50-L51) 与 [ast2hir.py:L274](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L274)。

---

### 4.4 控制流的 HIR 翻译：`if` / `for` / `while`

#### 4.4.1 概念说明

本模块是综合实践的核心。控制流（`if/for/while`）无法用单条 `Call` 表达，HIR 的做法是：**把分支/循环体建成独立的 `Block`，再用一个高阶调用把它们组织起来**——`if_else(cond, then_block, else_block)`、`loop(body_block, iterable)`。这里的 `then_block`、`else_block`、`body_block` 都是 `hir.Block` 对象，作为**立即常量操作数**直接塞进 `Call.args`（回忆 4.1 的 `Operand` 约定）。

这正是 HIR「用调用建模一切」的威力：控制流也被统一成了「调用一个接受块参数的内置函数」。

#### 4.4.2 核心流程

三种控制流的翻译模式：

```
if cond:                  →  cond = bool(test)
    A                          ^then:  A 的语句;  end_branch
else:                         ^else:  else 的语句; end_branch
    B                          if_else(cond, ^then, ^else)

for i in it:             →  ^body(%ind):
    A                           store(i, %ind);  A 的语句;  continue
                          loop(^body, <it 的 HIR>)

while cond:              →  ^body():
    A                           cond=bool(test)
                                if_else(cond, ^pass{end_branch}, ^break{break})
                                A 的语句;  continue
                          loop(^body, None)        # None = 无穷循环，靠 break 退出
```

注意几个关键点：

- `for` 的循环体块**带一个参数** `%ind`（归纳变量），由 `loop` 在每轮注入；翻译时立刻 `store(i, %ind)` 把它赋给循环目标。
- `while` 没有迭代器，它用 `loop(body, None)`（[hir_stubs.py:L22-L23](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir_stubs.py#L22-L23) 的注释说「`iterable` 为 None 即无穷循环」），靠在体首插入「`if cond: pass; else: break`」来退出。
- 三者的合法 `Jump`：分支块尾 `END_BRANCH`，循环体尾 `CONTINUE`，`break` 是 `BREAK`，返回是 `RETURN`。

#### 4.4.3 源码精读

**`if` 语句**（[ast2hir.py:L1012-L1026](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1012-L1026)）：先把 `test` 包成布尔（`_bool_expr`），再用两个 `with ctx.new_block()` 开 then/else 块，分别 `_stmt_list` 翻译体与 orelse；若块尾没有显式 jump，补一个 `END_BRANCH`（[L1018-L1019](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1018-L1019)、[L1023-L1024](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1023-L1024)）；最后发出 `if_else(cond, then_block, else_block)`（[L1026](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1026)）。两个 `Block` 对象作为操作数直接传入。

**`for` 语句**（[ast2hir.py:L707-L733](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L707-L733)）：先判断是不是 `ct.static_iter(...)`（静态展开循环，对应 `_get_static_iter_expr`，[L736-L745](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L736-L745)）；普通 `for` 选 `op = hir_stubs.loop`，翻译 `iterable`；接着开一个**带归纳变量参数**的体块 `with ctx.new_block(params=(induction_var,))`（[L726](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L726)），先 `_do_assign(induction_var, stmt.target)`（[L727](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L727)）把归纳变量赋给 `i`，再翻译体；体尾若无 jump 则补 `CONTINUE`（[L729-L730](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L729-L730)）；最后 `loop(body_block, iterable)`（[L733](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L733)）。`parent_loops` 栈（[L724/L731](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L724)）用于校验内层的 `break/continue/return` 合法性。

**`while` 语句**（[ast2hir.py:L913-L937](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L913-L937)）：开体块（无参数），在体首插入条件判断——`if_else(cond, then=END_BRANCH, else=BREAK)`（[L922-L928](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L922-L928)），即「真则继续、假则跳出」；然后翻译体，补 `CONTINUE`；最后 `loop(body_block, None)`（[L936](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L936)）。

**`break` / `continue` / `return`**（[ast2hir.py:L1029-L1054](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1029-L1054)）：它们不发出 Call，而是直接设置当前块的 `jump`。其中 `break` 在 `for`/`static_for` 里被拒（[L1038-L1039](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1038-L1039)），`return` 在 `for` 里被拒（[L1045-L1046](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1045-L1046)）——这正是 u3-l3 所说的「`for` 编成定数计数循环，无 break 语义」。`return` 还区分入口与非入口：入口函数直接 `Jump.RETURN`，非入口（helper）则把返回值存到 `$retval`、置 `$returning=True`、再 `BREAK` 跳出包装循环（[L1048-L1054](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1048-L1054)）。

**入口函数与 helper 的差异**（[ast2hir.py:L1203-L1233](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1203-L1233)）：入口 kernel 的根块体尾若无 jump，补 `RETURN`（[L1209-L1211](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1209-L1211)）；helper 函数则被包进一个 `loop(body, None)` 以支持「用 break 模拟 early return」（[L1212-L1226](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1212-L1226)）。

#### 4.4.4 代码实践

**目标**：手动跟踪 `if` 的翻译，画出 Block 拓扑。

**步骤**：

1. 阅读一个最简内核片段 `if i > 3: acc = acc + i`。
2. 对照 [ast2hir.py:L1012-L1026](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1012-L1026) 与 `_compare_expr`（[ast2hir.py:L499-L536](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L499-L536)），在纸上列出会创建几个 `Block`、每个块的 `jump` 是什么。
3. 用 4.4.5 的运行脚本对照。

**需要观察的现象**：`if` 创建 then、else 两个子块，各以 `END_BRANCH` 结尾；条件 `i > 3` 先产生一条 `gt` 调用，再被 `bool_` 包成布尔；最后一条 `if_else(cond, ^then, ^else)` 把两个块串起来。

**预期结果**：3 个 Block（当前块 + then + else），then 块的 `stored_indices` 含 `acc` 的 index，jump 全为 `END_BRANCH`（else 块空体）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `for` 循环体的 `Block` 要带一个参数 `params=(induction_var,)`，而 `while` 的体块不带参数？

**答案**：`for` 每轮由 `loop` 把「当前元素」注入为归纳变量，块参数就是接收这个值的入口；翻译时紧接着 `store(i, %ind)` 赋给循环目标。`while` 没有迭代元素，靠 `loop(body, None)` 反复执行体块，退出条件在体首用 `if_else(..., BREAK)` 表达，故不需要参数。参见 [ast2hir.py:L726-L727](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L726-L727) 与 [ast2hir.py:L918-L936](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L918-L936)。

**练习 2**：在内核里写 `for i in range(8): break` 会发生什么？为什么？

**答案**：编译期抛 `TileSyntaxError: Break in a for loop is not supported`。因为 `for` 被编成定数计数循环，HIR 层就在 [ast2hir.py:L1038-L1039](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1038-L1039) 拒绝了 `break`。`break` 只在 `while` 里允许。

---

### 4.5 `ast_get_all_local_names`：作用域预扫描

#### 4.5.1 概念说明

在翻译开始**之前**，cuTile 先对函数 AST 做一次完整扫描，把「这个函数里到底有哪些局部变量名」一次性收集起来。这件事看似多余——翻译时遇到 `x = ...` 不就知道 `x` 是局部了吗？其实不然。考虑 `load(x)`：当翻译器在表达式里遇到 `ast.Name('x')` 时，它必须**立刻**判断 `x` 是局部、捕获自外层、还是全局，才能发出正确的 `ResolvedName`。而 Python 的作用域规则是「整个函数级」的——一个名字是否为局部，取决于它**在整个函数里**有没有被赋值，而不是看翻译到了哪一行。所以必须先扫一遍，拿到完整的局部名集合，翻译时才能正确分类。

这就是 `ast_get_all_local_names` 的使命。

#### 4.5.2 核心流程

```
ast_get_all_local_names(func_def)
  ├── 先把所有形参加入 stored_names
  ├── walk 每条语句：
  │     ├── ast.Name (Store/Del)          → 加入 stored_names
  │     ├── ast.FunctionDef/ClassDef      → 函数/类名加入 stored_names
  │     ├── ast.Global / ast.Nonlocal     → 分别记录到 explicit_globals/nonlocals
  │     ├── ast.Import / ImportFrom       → 导入的别名加入 stored_names
  │     └── 不下钻到嵌套函数体、不下钻到推导式的 target
  └── return local_names = stored_names - (globals | nonlocals)
```

返回的 `local_names` 集合会被排序成 `_own_local_names` 列表，名字在列表里的下标就是它的 **local index**，也就是 `ResolvedName.index`（当 `depth == 本函数 depth` 时）。

#### 4.5.3 源码精读

**`ast_get_all_local_names`** 定义在 [ast_util.py:L16-L67](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L16-L67)。它用 `match type(node)` 逐类收集：

- `ast.Name` 且 `ctx` 为 `Store`/`Del`（[L22-L24](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L22-L24)）：所有赋值/删除目标。
- `ast.FunctionDef`/`AsyncFunctionDef`/`ClassDef`（[L25-L26](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L25-L26)）：嵌套定义本身会绑定一个名字。
- `ast.Global`/`ast.Nonlocal`（[L27-L30](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L27-L30)）：声明为 global/nonlocal 的名字要从 local 里剔除。
- `ast.Import`/`ImportFrom`（[L37-L42](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L37-L42)）：导入绑定的别名。

两个关键的「不下钻」规则在 `_should_skip_field`（[ast_util.py:L73-L79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L73-L79)）：

- **不下钻到嵌套函数/类的 `body`**——因为嵌套函数有自己的作用域，它的局部变量不该泄漏到外层。
- **不下钻到推导式的 `target`**——推导式（如 `[x for x in ...]`）的归纳变量在 Python 里是推导式局部作用域，不泄漏。

最后返回 `stored_names - (explicit_globals | explicit_nonlocals)`（[ast_util.py:L66-L67](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L66-L67)），保证 global/nonlocal 声明生效。

**它在入口被调用**：[ast2hir.py:L74](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L74) `local_names, _, _ = ast_get_all_local_names(func_def)`，结果传给 `_Context`。`_Context.__init__` 把它排序成 `_own_local_names` 并建立 `name_to_local_idx` 映射（[ast2hir.py:L182-L185](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L182-L185)）。此后 `ctx.load`/`ctx.store` 就靠这张表把名字分类成 `ResolvedName`。

同一个函数还被 `_make_closure`（处理嵌套函数/lambda，[ast2hir.py:L1076-L1105](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1076-L1105)）和 `_call_static_eval`（处理 `static_eval`，[ast2hir.py:L375](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L375)）复用，用来分析子作用域的名字。

#### 4.5.4 代码实践

**目标**：验证「名字的局部性是函数级、整段决定的」。

**步骤**：

```python
# 示例代码：对比两种写法的 local_names
import ast
from cuda.tile._passes.ast_util import ast_get_all_local_names

src1 = "def f(x):\n    a = 1\n    return a + x\n"
src2 = "def f(x):\n    return a + x\n    a = 1\n"   # a 的赋值在 return 之后（死代码）
t1 = ast.parse(src1).body[0]
t2 = ast.parse(src2).body[0]
print("src1 locals:", ast_get_all_local_names(t1).local_names)
print("src2 locals:", ast_get_all_local_names(t2).local_names)
```

**需要观察的现象**：尽管 `src2` 里 `a = 1` 在 `return` 之后永远执行不到，`a` 仍被算作局部变量。两个例子的 `local_names` 都应包含 `a`（以及参数 `x`）。

**预期结果**：两份 `local_names` 相同，都是 `{'a', 'x'}`。这印证了 Python 的「整段函数决定局部性」规则——也是为什么必须预扫描，而不能边翻译边判定。

#### 4.5.5 小练习与答案

**练习 1**：`_should_skip_field` 为什么要跳过嵌套函数定义的 `body`？

**答案**：嵌套函数有自己独立的作用域，它的局部变量不属于外层函数。若下钻进去，就会把内层函数的局部名误算成外层局部，导致 `ResolvedName` 错乱、闭包捕获分析失真。参见 [ast_util.py:L73-L79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L73-L79)。

**练习 2**：内核里写 `global g; g = 1`，`g` 会出现在 `local_names` 里吗？

**答案**：不会。`ast.Global` 把 `g` 记入 `explicit_globals`，最终 `local_names = stored_names - (globals | nonlocals)` 会把它剔除（[ast_util.py:L66-L67](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L66-L67)）。这也对应 `ctx.store` 对「给 global 赋值」会抛错（[ast2hir.py:L256-L259](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L256-L259)）。

---

## 5. 综合实践

把本讲五个模块串起来：用 `get_function_hir` 打印一个含 **`for` + `if`** 的内核的 HIR，然后**手画 Block/Call 结构图**，标注每个块的 `jump` 与 `stored_indices`，并解释每个模块在产出这份 HIR 时各自扮演的角色。

### 5.1 实践目标

- 看清 `for` 与 `if` 嵌套时 HIR 的块拓扑。
- 把 `ast_get_all_local_names`（预扫描）、`ResolvedName`（名字编码）、`stored_indices`（写跟踪）、`Jump`（控制流）、`if_else`/`loop`（高阶调用）这些概念在一棵真实的 HIR 上对上号。

### 5.2 操作步骤

**第 1 步**：准备一个含 `for` + `if` 的普通 Python 函数（无需 GPU，`get_function_hir` 是纯前端）：

```python
# 示例代码：综合实践——打印并分析 for+if 的 HIR
from cuda.tile._passes.ast2hir import get_function_hir

def kernel():
    acc = 0
    for i in range(8):
        if i > 3:
            acc = acc + i
    return acc

func_hir = get_function_hir(kernel, entry_point=True)
print(func_hir.body)
print("--- root stored_indices ---", func_hir.body.stored_indices)
```

**第 2 步**：根据打印结果，在纸上画出块拓扑图。建议这样画：每个 `^N` 画成一个方框，框内列出关键 `Call`（不必抄全部 `%N` 编号，写语义即可，如 `load_var(i)`、`gt`、`add`、`store_var(acc)`），框底写 `jump`，框侧写 `stored_indices`（哪些变量在这个块里被赋值）。用箭头表示 `if_else`/`loop` 调用把哪些块组织起来。

**第 3 步**：对照源码解释你看到的现象，逐项回答：

1. `acc` 和 `i` 是怎么进入 `local_names` 的？（提示：`acc` 来自 `ast.Assign` 的 Store，`i` 来自 `for` 的 target——回顾 4.5。）
2. `for` 的体块为什么带一个参数？那个参数最后被 `store` 给了谁？（回顾 4.4。）
3. `if i > 3` 产生了哪几个块？它们的 `jump` 分别是什么？
4. `acc = acc + i` 这一行在 then 块里展开成哪几条 `Call`（读 `acc`、读 `i`、`add`、写 `acc`）？
5. 根块的 `stored_indices` 为什么同时含 `acc` 和 `i`？（提示：`new_block` 的冒泡合并——回顾 4.3。）

### 5.3 需要观察的现象

- 一个根块 `^0`（函数体），末尾是 `return`。
- 一个 `for` 体块 `^1(%ind)`：开头 `store_var(i, %ind)`，里面嵌着 `if_else(...)`，末尾 `continue`。
- `if` 衍生出 then 块 `^2`（含 `load_var(acc)`、`load_var(i)`、`add`、`store_var(acc)`、`end_branch`）与 else 块 `^3`（基本为空、`end_branch`）。
- 根块发出一条 `loop(^1, <range 的 HIR>)` 调用。

### 5.4 预期结果（结构草图）

下面的块编号与 `%N` 仅作示意，**具体编号待本地验证**——以你实际打印的输出为准：

```
^0():                                  jump=RETURN       stored={acc, i}
    %c0 = identity(0)
    store_var(rn_acc, %c0)             # acc = 0
    ^1(%ind):                          jump=CONTINUE     stored={i, acc}
        store_var(rn_i, %ind)          # i = 归纳变量
        %li = load_var(rn_i)
        %three = identity(3)
        %cmp = <fn:gt>(%li, %three)    # i > 3
        %cond = <fn:bool_>(%cmp)
        ^2():                          jump=END_BRANCH   stored={acc}
            %la = load_var(rn_acc)
            %li2 = load_var(rn_i)
            %sum = <fn:add>(%la, %li2) # acc + i
            store_var(rn_acc, %sum)
        ^3():                          jump=END_BRANCH   stored={}
        if_else(%cond, ^2, ^3)
    loop(^1, <range(8) 的 HIR>)
    return
```

> 你画完图后，应当能指出：`acc` 的多次赋值跨越了 `^0` 与 `^2`，这正是 hir2ir 在循环回边处要为 `acc` 建立 phi（携带值）的依据——也就是 u3-l3 提到的「循环体内被重新赋值的变量成为 carried values」在最前端的体现。

### 5.5 进阶（可选）

把 `for` 换成 `while`：

```python
def kernel_w():
    acc = 0
    i = 0
    while i < 8:
        if i > 3:
            acc = acc + i
        i = i + 1
    return acc
```

对比两者的 HIR：`while` 的体块**不带参数**，且块首多了一组「`if_else(cond, pass, break)`」。理解这一点后，你就完整掌握了 `for` 与 `while` 在 HIR 层的本质差异（见 4.4.3 的 `_for_stmt` vs `_while_stmt`）。

## 6. 本讲小结

- **HIR 是「一切皆 `Call`」的中间表示**：没有专门 Operation，算术是 `operator.add` 调用、控制流是 `if_else`/`loop` 调用、变量读写是 `load_var`/`store_var` 调用；操作数 `Operand = Value | Any`，`Value` 表示引用、其它对象表示立即常量（[hir.py:L6-L62](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L6-L62)）。
- **`get_function_hir` 是翻译入口**：用 `inspect.findsource` 取源码、`if True:` 缩进技巧解析 AST、构造 `_Context`、按 `_expr_handlers`/`_stmt_handlers` 分派；`@lru_cache` 保证每个函数只译一次（[ast2hir.py:L24-L81](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L24-L81)）。
- **数据结构层级是 `Function` → `Block` → `Call` → `Value`**：`Block` 装一串 `Call` 加一个 `Jump`；`stored_indices` 跟踪块的写并向上冒泡，是后续 phi 合并的依据（[hir.py:L103-L195](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L103-L195)）。
- **名字被编码成 `ResolvedName(depth, index)`**：`-1` 表全局、`(-1,-1)` 表未找到、`depth == 本函数` 表局部、其它表捕获；`load`/`store` 据此分类（[hir.py:L129-L149](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L129-L149)、[ast2hir.py:L254-L274](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L254-L274)）。
- **控制流被翻译成「带块参数的高阶调用」**：`if` → 两个 `END_BRANCH` 子块 + `if_else`；`for` → 带归纳变量参数的体块（`CONTINUE`）+ `loop(body, iterable)`；`while` → 无参体块首插「`if cond: pass; else: break`」+ `loop(body, None)`（[ast2hir.py:L707-L937](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L707-L937)、[L1012-L1026](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1012-L1026)）。
- **`ast_get_all_local_names` 是必要的前置扫描**：Python 的局部性由整段函数决定，必须先于翻译拿到完整局部名集合，`load` 才能正确分类（[ast_util.py:L16-L67](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L16-L67)）。

## 7. 下一步学习建议

HIR 只是前端的中间产物，它的 `Call` 里还满是 `operator.add`、`if_else`、`loop` 这样的「占位调用」。下一步要把它们**降级**成真正带 Operation 定义的 Tile IR：

- **u5-l4（HIR 到 IR：hir2ir）**：本讲的直接后续。`hir2ir` 会消费这里产出的 `hir.Function`，把 `operator.add` 分派到具体的算术 Op、把 `if_else`/`loop` 落地成 `IfElse`/`Loop` IR 操作，并利用本讲的 `stored_indices` 建立循环携带变量的 phi 状态。建议带着本讲综合实践画出的 HIR 草图去读 u5-l4，对照同一个内核在两个阶段的形态。
- **u5-l5（IR 核心：IRContext/Builder/Block/Var/Operation）**：理解 hir2ir 产物的目标数据结构——Tile IR 的 `Var`/`Operation`/`Block` 与本讲的 `Value`/`Call`/`Block` 的对应与差异（HIR 的 `Value` 是轻量 id，IR 的 `Var` 是带类型的 SSA 值）。
- **回头深读**：若想看 `static_eval`/`static_assert` 是如何在前端被特殊处理的（u3-l5 的实现层），可精读 [ast2hir.py:L304-L392](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L304-L392) 的 `_parse_keyword_like_func` 与 `_call_static_eval`——它们走的是与普通翻译完全不同的「编译期求值」路径，是 ast2hir 里最精巧的一段。
