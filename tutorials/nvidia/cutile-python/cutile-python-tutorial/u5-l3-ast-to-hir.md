# AST 到 HIR：ast2hir

## 1. 本讲目标

上一讲（u5-l2）我们俯瞰了 `compile_tile` 的整条流水线，看到 Python 内核函数会先被 `get_function_hir` 翻译成 **HIR（High-level Intermediate Representation，高层中间表示）**，再交给后续的 `hir2ir`、IR 优化 pass、字节码与 cubin 生成。本讲就钻进这条流水线的「第一段」，搞清楚这个黑盒。

学完本讲，你应该能够：

- 说清 **HIR 是什么、为什么需要它**，以及它和 Python AST、和后续 Tile IR 的关系。
- 读懂 `hir.Function / Block / Call / Value` 这一组核心数据结构，理解 HIR「用通用 `Call` 表达一切操作（包括结构化控制流）」的设计。
- 复述 `get_function_hir` 把一个 Python 函数对象变成 `hir.Function` 的完整步骤（取源码、`if True:` 缩进技巧、构造全局表、建 `_Context`、收尾）。
- 理解 `ast_get_all_local_names` 的作用域分析作用：为什么必须在 lowering 之前就把「本函数有哪些局部变量」算清楚。
- 对一个含 `for` 与 `if` 的小内核，手工画出它的 HIR `Block/Call` 草图，标注每个块的终止 `jump` 与 `stored_indices`。

## 2. 前置知识

本讲默认你已掌握（前置讲义已建立）：

- **Python AST（抽象语法树）**：标准库 `ast` 把源码解析成由 `ast.FunctionDef`、`ast.For`、`ast.If`、`ast.BinOp`、`ast.Name` 等节点组成的树。本讲大量出现这些节点类型。
- **load–compute–store 范式与控制流子集**（u3-l1、u3-l3）：tile code 是「被翻译的 Python」，`if/for/while` 会被翻译成结构化 IR；本讲正是这一翻译的**最前端**实现。
- **`compile_tile` 流水线**（u5-l2）：`get_function_hir` 产出的 `hir.Function` 被 `_IrKeeper` 持有，随后由 `hir2ir` 拉动；本讲是这条链路的源头。

进入源码前，先用通俗语言建立两个关键直觉。

### 2.1 为什么不直接把 AST 翻译成 Tile IR？

Python 的 AST 是一棵「面向人类语法」的树：`a + b` 是 `ast.BinOp`，`if ...:` 是 `ast.If`，`for ...:` 是 `ast.For`，节点种类上百种，每种语法都有一套自己的字段。如果直接把 AST 翻成最终的 Tile IR，翻译器要同时处理两件复杂的事：

1. **Python 语义的归一化**：把五花八门的语法节点归一成少数几种统一形式。
2. **Tile IR 的具体形状**：Tile IR 是 SSA（静态单赋值）风格，有具体的 `Operation` 类型、操作数、嵌套 region 等。

把这两件事混在一起会让代码极难维护。cuTile 的做法是**插入一层 HIR**：先把 AST「拍平+归一」成一种极简的、统一的中间形态，再由 `hir2ir`（下一讲 u5-l4）把这层中间形态翻译成具体的 Tile IR。HIR 只关心「归一」，不关心「具体 IR 形状」。

### 2.2 HIR 的核心抽象：万物皆「函数调用」，可变变量靠 load/store_var 桥接

HIR 最关键的设计选择是：**它没有定义一堆具体的 Operation 类型，而是用「函数调用」这一个概念建模所有操作**。这是 hir.py 顶部注释明确写出的设计意图：

[src/cuda/tile/_ir/hir.py:L6-L14](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L6-L14) — 注释说明 HIR 用「function call」统一建模所有操作，连结构化控制流也不例外。

也就是说：

- `a + b` 在 HIR 里就是一个 `Call`，它的 `callee`（被调用者）是 Python 的 `operator.add`。
- `x.attr` 是 `Call(callee=getattr, args=(x, "attr"))`。
- `a < b < c`（链式比较）被拆成一串嵌套的 `if_else` 调用。
- `if/for/while` 控制流也是 `Call`：`callee` 是 HIR 专用 stub（`hir_stubs.if_else`、`hir_stubs.loop`），而它的某个 `args` 是一个**嵌套的 `Block`**（代码块作为「一等值」传给控制流操作）。

而 Python「可反复赋值的局部变量」如何塞进「单赋值」IR？答案是一座很干净的桥：

- 翻译**开始前**，先静态算清「这个函数有哪些局部变量名」，每个名字分配一个**稳定的整数下标（local index）**。
- 读变量 → 生成 `load_var(ResolvedName)` 调用；写变量 → 生成 `store_var(ResolvedName, value)` 调用。`ResolvedName` 里就装着那个稳定下标。
- 每个 `Block` 用 `stored_indices` 集合记录「这个块（含嵌套）里被赋值过哪些局部」。后续 `hir2ir` 正是靠它知道「循环跨迭代要为哪些变量插入合并（carried values / phi）」。

所以「先算清局部变量名」是整条 lowering 的地基——这就是 `ast_get_all_local_names` 存在的意义。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/cuda/tile/_passes/ast2hir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py) | 本讲主角。把 Python AST 翻译成 HIR。含入口 `get_function_hir`、翻译上下文 `_Context`、表达式/语句分派表 `_expr_handlers`/`_stmt_handlers`，以及 if/for/while 的具体 lowering。 |
| [src/cuda/tile/_ir/hir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py) | HIR 的数据结构定义：`Value`、`Call`、`Block`、`Jump`、`ResolvedName`、`Function`、`Operand`。 |
| [src/cuda/tile/_passes/ast_util.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py) | 作用域分析工具 `ast_get_all_local_names`：遍历 AST，收集函数的全部局部变量名。 |
| [src/cuda/tile/_ir/hir_stubs.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir_stubs.py) | HIR 专用的「伪函数」（stub）：`if_else`、`loop`、`static_foreach`、`build_tuple`、`store_var`、`load_var`、`make_closure` 等。它们只是占位符号，真正的实现在 `hir2ir` 阶段才落地。 |

---

## 4. 核心概念与源码讲解

### 4.1 模块一：`get_function_hir` —— 从 Python 函数到 HIR 的入口

#### 4.1.1 概念说明

`get_function_hir` 是 ast2hir 模块对外的唯一入口。它接收一个普通的 Python 可调用对象（即被 `@ct.kernel` 装饰前/后的那个函数），返回一个 `hir.Function`。它在 `compile_tile` 流水线里只被调用一次（u5-l2 讲过：HIR 与具体 signature 无关，所以**只翻译一遍、按函数缓存**）。

调用点在 `_compile.py`：

[src/cuda/tile/_compile.py:L472-L473](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L472-L473) — `compile_tile` 调用 `get_function_hir(ann_func.pyfunc, entry_point=True)`，把得到的 `func_hir` 交给 `_IrKeeper` 持有，后续按需拉动生成 final IR。

#### 4.1.2 核心流程

`get_function_hir` 的大步骤是「取源码 → 解析 AST → 构造全局表与作用域 → 建翻译上下文 → lowering → 收尾」：

```
get_function_hir(pyfunc, entry_point):
  1. 解包 @function 装饰器，拿到原始 pyfunc
  2. inspect.findsource + getblock 取出函数源码文本
  3. 用 "if True:" 包一层再 ast.parse，绕过缩进问题；修正行号
  4. 构造 func_globals：__builtins__ + __globals__ + 闭包变量(__closure__/co_freevars)
  5. 建 FunctionDesc（文件名/行号/列号/是否入口）
  6. ast_get_all_local_names(func_def)  → 拿到局部变量名集合
  7. 构造 _Context（持有全局表、局部名、序列发生器、当前块等）
  8. _get_function_hir_inner(func_def, signature, ctx) → 递归 lowering，产出 hir.Function
  9. _finalize_func(ret, 0, ()) → 计算嵌套函数的捕获关系 captures_by_depth
  return ret
```

整个函数被 `@lru_cache` 装饰（按 `(pyfunc, entry_point)` 缓存），所以同一个函数对象不会被重复翻译。

#### 4.1.3 源码精读

入口与缓存、解包装饰器：

[src/cuda/tile/_passes/ast2hir.py:L24-L28](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L24-L28) — `@lru_cache` 缓存；用 `is_function_wrapper` 判断并剥掉 `@ct.function` 装饰器，拿到最里层的原始函数。

取源码（特意用 `findsource` 而非 `getsourcelines`，以保留装饰器所在行）：

[src/cuda/tile/_passes/ast2hir.py:L32-L34](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L32-L34) — `findsource` 取整个文件源，`getblock` 从函数首行切出「这一整个代码块」的文本。

接下来是一个值得细品的「缩进技巧」。函数源码可能嵌套在类、`if` 块里，带额外缩进，直接喂给 `ast.parse` 会因缩进不一致而报错。常见的 `textwrap.dedent` 方案并不正确——它对未缩进的续行（如单独一行的 `200)`）会失败。cuTile 用了一个巧办法：**给源码前面加一行 `if True:` 并多缩进一格**，让原缩进变成 `if` 块内的合法缩进：

[src/cuda/tile/_passes/ast2hir.py:L36-L62](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L36-L62) — 「if True:」包裹技巧：把函数源拼到一个 `if True:` 块内再 parse，规避缩进问题；parse 后剥掉外层 `If` 拿到真正的 `FunctionDef`。

由于多塞了一行 `if True:` 和一格缩进，AST 节点的行号、列号都要回偏修正：

[src/cuda/tile/_passes/ast2hir.py:L86-L98](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L86-L98) — `_fix_line_and_column_numbers` 把每个节点的行号回偏 `first_line - 2`、列号减 1，还原成源文件里的真实位置，保证后续报错定位准确。

构造全局表（关键：把闭包变量也塞进 `func_globals`，这样内核里引用的外层变量在 lowering 时能被当作「冻结全局」解析）：

[src/cuda/tile/_passes/ast2hir.py:L64-L69](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L64-L69) — `func_globals` 由 `__builtins__`、`__globals__` 与闭包单元 `__closure__`（按 `co_freevars` 对应）拼成；闭包变量随后被当作「冻结全局」处理。

收尾：建 `FunctionDesc`、跑作用域分析、构造 `_Context`、调用内部 lowering、最后 `_finalize_func`：

[src/cuda/tile/_passes/ast2hir.py:L71-L81](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L71-L81) — 建 `FunctionDesc`、调 `ast_get_all_local_names` 拿局部名、构造 `_Context`、`_get_function_hir_inner` 产出 `hir.Function`、`_finalize_func` 补全嵌套函数捕获关系。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「HIR 只翻译一次、按函数缓存」这件事，并理解 `entry_point` 的含义。

**操作步骤**：

1. 读 [_passes/ast2hir.py:L24-L25](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L24-L25)，确认 `@lru_cache` 与签名 `(pyfunc, entry_point)`。
2. 读 [_compile.py:L472](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L472)，确认内核入口调用时传的是 `entry_point=True`。
3. 用 Grep 搜索整个仓库里 `get_function_hir` 的全部调用点，确认除 `_compile.py` 这一处入口调用外，其余都是内部递归（`_get_function_hir_inner`）或定义/导入。

**需要观察的现象 / 预期结果**：你会看到 `get_function_hir` 只在 `compile_tile` 路径上被以 `entry_point=True` 调用一次；嵌套函数（lambda、内部 `def`）则通过 `_make_closure` → `_get_function_hir_inner` 以 `entry_point=False` 递归翻译。`entry_point` 的差别会在 4.4 节看到：只有入口块会在结尾自动补一个 `RETURN`。

> 待本地验证：是否真的命中 `lru_cache`（可临时在 `get_function_hir` 体内加一行 `print`，连续两次 `ct.launch` 同一内核，预期只打印一次）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 cuTile 用 `inspect.findsource` 而不是 `getsourcelines` 来取源码？
**参考答案**：因为 `getsourcelines` 会自动解包装饰器，可能丢失装饰器所在行；而 cuTile 需要拿到「从装饰器或 `def` 行开始的完整代码块」，再自己用 `getblock` 切块，以精确控制行号与定位。

**练习 2**：如果把 `@lru_cache` 去掉，会在什么时候出现重复翻译？是否影响正确性？
**参考答案**：同一个内核函数被多次 `ct.launch`（或不同 signature 触发同一函数的多次编译）时会被重复翻译成 HIR。不影响正确性，但会重复做无谓的 AST 解析与 lowering，浪费编译时间——这正是用 `lru_cache` 缓存 HIR 的动机。

---

### 4.2 模块二：`hir.Function / Block / Call / Value` —— HIR 的核心数据结构

#### 4.2.1 概念说明

HIR 只有很少几种数据结构，全部定义在 [src/cuda/tile/_ir/hir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py)。理解这一节，你就理解了 HIR 的全部「形状」。最核心的四个是：

- **`Value`**：一个 SSA 值，本质就是一个整数 `id`，打印为 `%id`。它是 `Call` 的产物，也是函数参数。
- **`Call`**：一次函数调用，HIR 里**唯一的「操作」载体**。形如 `result = callee(args, **kwargs)`。
- **`Block`**：一段线性 `Call` 序列 + 一个终止 `Jump`，是控制流的「代码块」，可作为参数传给控制流 stub。
- **`Function`**：一个完整函数的 HIR 容器，持有一个根 `Block`、局部变量名表、参数下标、嵌套函数、捕获信息等。

此外还有两个辅助概念：`Operand`（「可以是 `Value`，也可以是任意 Python 立即常量」的联合类型）与 `Jump`（块的终止符枚举）。

#### 4.2.2 核心流程

一个 `hir.Function` 在内存里大致长这样（伪代码）：

```
Function
├── desc: FunctionDesc           # 名字/文件/行号/是否入口
├── body: Block                  # 根块（入口）
├── signature: inspect.Signature # 参数签名
├── local_names: tuple[str]      # 本函数全部局部变量名（按下标排列）
├── param_local_indices: tuple   # 每个参数在 local_names 里的下标
├── frozen_global_names/values   # 冻结全局的名字与值
├── nested_functions: tuple      # 直接嵌套的子函数 HIR
├── captures_by_depth            # 各外层作用域被捕获的局部下标
└── enclosing_funcs              # 外层函数链

Block
├── params: tuple[Value]         # 入口参数（如循环体的归纳变量）
├── calls: list[Call]            # 线性调用序列
├── jump: Jump | None            # 终止符（END_BRANCH/CONTINUE/BREAK/RETURN）
├── result / have_result         # 块的「产出值」（用于 if_else 各分支回送结果）
└── stored_indices: set[int]     # 本块（含嵌套）被赋值的局部下标集合

Call
├── result: Value | None         # 调用结果（None 表示无返回值，如 store）
├── callee: Operand              # 被调用者：Python callable 或 hir_stubs.xxx
├── args: tuple                  # 操作数（Value 或常量或嵌套 Block）
├── kwargs: tuple                # 关键字参数
└── loc: Loc                     # 源码位置（报错/调试用）
```

要点：

- **一切操作都是 `Call`**。算术 `a+b` 的 `callee` 是 `operator.add`；读属性 `x.y` 的 `callee` 是 `getattr`；下标 `x[i]` 的 `callee` 是 `operator.getitem`。
- **控制流也是 `Call`，其参数里夹着嵌套 `Block`**。例如 `if_else(cond, then_block, else_block)`、`loop(body_block, iterable)`。`Block` 因此是一种「一等操作数」。
- **每个 `Block` 恰有一个终止 `Jump`**（或为 `None`，由上层 lowering 补齐）。四种 `Jump`：`END_BRANCH`（if 分支结束）、`CONTINUE`（进入下一轮循环）、`BREAK`（跳出循环）、`RETURN`（函数返回）。

#### 4.2.3 源码精读

**`Value` 与 `make_value` 缓存**：`Value` 是 frozen dataclass，只有 `id`；`make_value` 用一个全局数组缓存前 2000 个 `Value` 实例以减少对象分配：

[src/cuda/tile/_ir/hir.py:L27-L54](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L27-L54) — `Value` 打印为 `%id`；`make_value` 命中缓存快速路径，否则批量扩容 100 个。

**`Operand` 类型别名**：这是理解 HIR「常量如何表达」的钥匙。一个操作数要么是 `Value`（某次调用的结果或参数），要么是任意 Python 对象（立即常量）：

[src/cuda/tile/_ir/hir.py:L57-L62](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L57-L62) — `Operand = Value | Any`：`Value` 表示「先前调用的结果/参数」，其他类型即「立即常量」。

**`Call`**：HIR 唯一的操作载体。注意它的 `__str__` 对特殊 callee `identity` 做了特判（直接打印成 `%r = %arg`），因为常量会被包成 `identity(value)` 调用以保留位置信息：

[src/cuda/tile/_ir/hir.py:L73-L93](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L73-L93) — `Call` 字段与打印；`identity` 被特判为直接赋值形式。

**`Jump` 枚举**：块的四种终止方式：

[src/cuda/tile/_ir/hir.py:L96-L100](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L96-L100) — `END_BRANCH/CONTINUE/BREAK/RETURN`。

**`Block`**：注意 `params`（循环体接收归纳变量）、`stored_indices`（被赋值的局部下标集合，会沿嵌套向上传播）：

[src/cuda/tile/_ir/hir.py:L103-L126](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L103-L126) — `Block` 结构与打印（`^block_id(params): ...`）。

**`ResolvedName`**：用 `(depth, index)` 二元组定位一个名字。`depth` 标识它在哪一层作用域（`-1` 是全局，等于当前函数深度是本函数局部，介于其间是捕获的外层局部）：

[src/cuda/tile/_ir/hir.py:L129-L149](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L129-L149) — `ResolvedName` 注释详述四种情况；`UNKNOWN_NAME = ResolvedName(-1, -1)` 表示未找到。

**`Function`**：HIR 的顶层容器。注意 `local_names`（按下标排列的全部局部名）、`param_local_indices`（参数→局部下标）、`nested_functions`（嵌套函数 HIR）、`captures_by_depth`/`enclosing_funcs`（闭包捕获）：

[src/cuda/tile/_ir/hir.py:L152-L195](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L152-L195) — `Function` 的全部字段。

最后，本节依赖的一组「HIR stub」定义在 `hir_stubs.py`，它们只是带 `@stub` 标记的占位函数，描述了控制流与变量读写在 HIR 层的「调用接口」，真正实现要等到 `hir2ir`：

[src/cuda/tile/_ir/hir_stubs.py:L14-L79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir_stubs.py#L14-L79) — `if_else`/`tuple_comp_if`/`loop`/`static_foreach`/`build_tuple`/`unpack`/`identity`/`store_var`/`load_var`/`make_closure`/`do_static_eval`/`do_static_assert`/`enter_context`/`pop_context`/`is_contained_in` 等 HIR stub。

#### 4.2.4 代码实践

**实践目标**：用最小例子验证「算术与下标都是 `Call`」。

**操作步骤**：

1. 阅读 [_passes/ast2hir.py:L482-L489](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L482-L489)（`_binop_expr`：`a + b` → `ctx.call(operator.add, (lhs, rhs))`）。
2. 阅读 [_passes/ast2hir.py:L591-L595](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L591-L595)（`_subscript_expr`：`x[i]` → `ctx.call(operator.getitem, ...)`）。
3. 阅读 [_passes/ast2hir.py:L545-L549](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L545-L549)（`_constant_expr`：字面量被包成 `identity(value)` 以保留位置）。

**预期结果**：你能用自己的话复述——在 HIR 层，`a + b`、`x[i]`、字面量 `3` 这三者都被归一为「一次 `Call`」，只是 `callee` 分别是 `operator.add`、`operator.getitem`、`hir_stubs.identity`。这就是「万物皆 Call」的具体含义。

#### 4.2.5 小练习与答案

**练习 1**：`Call.result` 为 `None` 意味着什么？举一个会产生这种 `Call` 的 Python 语句。
**参考答案**：表示这次调用没有返回值（void）。典型例子是赋值语句翻译出的 `store_var(rn, value)` 调用，以及任何作为表达式语句的纯调用（如 `ct.store(...)`）——它们通过 `ctx.call_void`（见 [_passes/ast2hir.py:L241-L242](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L241-L242)）追加到块里。

**练习 2**：`Block.params` 什么时候非空？
**参考答案**：当块是某个控制流操作的「子块」、且需要接收外层传入的值时。最典型是 `for` 循环体块，它的 `params` 装着归纳变量（见 4.4 节 `_for_stmt`），循环每轮把当前迭代值经该参数送入体块。

---

### 4.3 模块三：`ast_get_all_local_names` —— 作用域分析与变量定位

#### 4.3.1 概念说明

如前置知识 2.2 所述，HIR 要把「可变局部变量」架到「SSA」上，必须**提前**知道一个函数有哪些局部变量。`ast_get_all_local_names` 就是干这件事的：它静态遍历 AST，把「所有会被绑定到本地作用域的名字」收集成一个集合。

它解决的核心问题是：**Python 的局部变量集合是静态可知的**——一个名字是不是局部，由函数里是否出现过对它的赋值（含 `def`/`class`/`import`/`as`/`except ... as`/`global`/`nonlocal` 等）决定，而不是运行时才知道。cuTile 借此在 lowering 前给每个局部名分配稳定下标。

#### 4.3.2 核心流程

```
ast_get_all_local_names(func):
  stored_names       = ∅    # 所有「绑定到本作用域」的名字
  explicit_globals   = ∅    # 显式 global 声明的名字
  explicit_nonlocals = ∅    # 显式 nonlocal 声明的名字

  walk(func.body):                          # 不下钻到嵌套 def/class 体
    Name(Store|Del ctx)        → stored
    FunctionDef/ClassDef name  → stored
    Global(names)              → explicit_globals
    Nonlocal(names)            → explicit_nonlocals
    ExceptHandler name         → stored
    MatchMapping/MatchAs/...   → stored
    Import/ImportFrom alias    → stored (alias 或 asname)

  # 函数形参本身也是局部变量
  for arg in (posonlyargs + args + kwonlyargs + vararg + kwarg):
      stored.add(arg)

  local_names = stored - (explicit_globals | explicit_nonlocals)
  return VariableNames(local_names, explicit_globals, explicit_nonlocals)
```

两条关键边界规则（由 `_should_skip_field` 实现）：

1. **不下钻到嵌套 `def`/`async def`/`class` 的 body**——嵌套函数有自己的作用域，它的局部不属于外层。
2. **不下钻到推导式的 `target`**——推导式的归纳变量不泄漏到外层作用域（与 Python 语义一致）。

#### 4.3.3 源码精读

返回类型 `VariableNames` 是个 NamedTuple，三元组分别对应「真局部」「显式 global」「显式 nonlocal」：

[src/cuda/tile/_passes/ast_util.py:L10-L13](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L10-L13) — `VariableNames` 三元组。

主函数 `ast_get_all_local_names`：内部 `walk` 用 `match type(node)` 分派各种绑定来源，再递归遍历子字段：

[src/cuda/tile/_passes/ast_util.py:L16-L67](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L16-L67) — 收集 Store/Del 名字、函数/类名、global/nonlocal、except/as、import 别名；先把形参加入；最后 `stored - (globals|nonlocals)` 得到 local_names。

边界控制 `_should_skip_field`：跳过嵌套定义的 body 与推导式的 target：

[src/cuda/tile/_passes/ast_util.py:L70-L79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast_util.py#L70-L79) — 两条「不下钻」规则，保证只统计本函数自己的局部。

这个结果如何被消费？回到 `get_function_hir`，它把 `local_names` 传给 `_Context`，后者在 `__init__` 里排序成 `_own_local_names` 列表、并建 `name_to_local_idx` 映射——这就是「名字 → 稳定下标」表的来源：

[src/cuda/tile/_passes/ast2hir.py:L182-L185](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L182-L185) — `_Context.__init__` 把局部名排序成列表、建 `name → local index` 表。

随后 `_Context.load` / `_Context.store` 用这张表把变量读写翻译成 `load_var(ResolvedName)` / `store_var(ResolvedName, value)` 调用，并把下标记进 `stored_indices`：

[src/cuda/tile/_passes/ast2hir.py:L254-L274](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L254-L274) — `store` 生成 `store_var(ResolvedName, value)` 并把下标加入 `stored_indices`；`load` 按「本函数局部 → 外层捕获 → 冻结全局 → UNKNOWN」四级解析 `ResolvedName`。

#### 4.3.4 代码实践

**实践目标**：手工跑一遍作用域分析，体会「局部变量集合静态可知」。

**操作步骤**：对下面这段（仅用于示意作用域，非真实可运行内核），按 `ast_get_all_local_names` 的规则推断 `local_names`：

```python
# 示例代码（仅演示作用域分析，不是可运行内核）
def f(a, b):              # a, b 是形参 → 局部
    x = 1                 # Name(Store) → x 局部
    y = [i for i in range(3)]  # 推导式归纳变量 i 不泄漏
    import numpy as np    # asname np → 局部
    def helper():         # 函数名 helper → 局部（但其 body 不下钻）
        z = 2             # 属于 helper 的作用域，不计入 f
    return x
```

**预期结果**：`f` 的 `local_names` 应为 `{"a", "b", "x", "y", "np", "helper"}`——注意 `i`（推导式归纳变量）和 `z`（嵌套函数局部）都**不**在内。

**待本地验证**：可在 Python 里 `import ast; ast.parse(...)` 后调用本仓库的 `ast_get_all_local_names` 直接核对（需先把 `src/` 加入路径并 import）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ast_get_all_local_names` 要返回 `explicit_globals` 与 `explicit_nonlocals`，而不是只返回 `local_names`？
**参考答案**：因为在 lowering 时，`_Context.load`/`store` 需要区分一个名字是「本函数局部」「外层捕获」还是「冻结全局」。把显式 `global`/`nonlocal` 名字从 `local_names` 里剔除（`stored - (globals|nonlocals)`）只是第一步；下游遇到这些名字会走不同分支（例如对 global/nonlocal 赋值会直接报错，见 `store` 里的 `Cannot assign to ...` 检查）。

**练习 2**：`_should_skip_field` 跳过推导式的 `target`，对应 Python 哪条作用域规则？
**参考答案**：Python 3 里列表/集合/字典推导式与生成器表达式有自己的作用域，其归纳变量（如 `[i for i in ...]` 里的 `i`）不会泄漏到外层。cuTile 复刻了这条规则，避免把推导式临时变量误算成外层局部。

---

### 4.4 模块四：控制流如何落到 HIR（if / for / while → Block + Call）

> 这个模块是把前三个模块串起来的「应用篇」，也是本讲综合实践（画 HIR 草图）所必须的。

#### 4.4.1 概念说明

cuTile 的 tile code 是「被翻译的 Python」——没有 Python 运行时。`if/for/while` 不是「在运行时跳转」，而是被 ast2hir **翻译成结构化的 HIR 控制流调用**，其形状最终会落到下游 Tile IR 的 `IfElse`/`Loop` 操作（详见下一讲 u5-l4）。本模块看 ast2hir 这一段是怎么搭积木的。

核心思想：**每个控制流结构 = 一个对 stub 的 `Call` + 若干个嵌套 `Block`**。每个嵌套块结尾会有一个 `Jump`：

| Python 语法 | HIR 形态 | 各子块的 `jump` |
| --- | --- | --- |
| `if c: T else: E` | `if_else(c, then, else)` | 两分支均 `END_BRANCH` |
| `for i in it: body` | `loop(body_block, it)`，`body_block.params=(i,)` | 体块 `CONTINUE` |
| `while c: body` | 体块开头插 `if_else(c, then=end, else=break)`；外层 `loop(body, None)` | 体块 `CONTINUE` |
| `a < b < c`（链式比较） | 嵌套 `if_else` | 各分支 `END_BRANCH` |
| `x and y` / `x or y` | 短路 `if_else` | 各分支 `END_BRANCH` |

> 几条限制（与 u3-l3 呼应）：`for` 仅支持 `range`/`ct.static_iter`；`break` 仅在 `while` 中允许（`for` 编成定数循环，无 break 语义）；`for-else`/`while-else` 直接报错。

#### 4.4.2 核心流程（以 `for` 与 `if` 为例）

`for` 的 lowering（`_for_stmt`）：

```
_for_stmt(stmt):
  若有 orelse → 报错 'for-else' 不支持
  区分普通 for（hir_stubs.loop + range）与 static for（hir_stubs.static_foreach + static_iter）
  parent_loops.append(kind)            # 记录所在循环类型（供 break/continue 校验）
  induction_var = make_value()
  with new_block(params=(induction_var,)) as body_block:
      _do_assign(induction_var, stmt.target)   # i = <归纳值>，写入 stored_indices
      _stmt_list(stmt.body)                    # 翻译循环体
      若体块无 jump 且为普通 for → 设 CONTINUE
  parent_loops.pop()
  call_void(op, (body_block, iterable))        # loop(body, iterable)
```

`if` 的 lowering（`_if_stmt`）：

```
_if_stmt(stmt):
  cond = bool(stmt.test)
  with new_block() as then_block:
      _stmt_list(stmt.body)
      若无 jump → 设 END_BRANCH
  with new_block() as else_block:
      _stmt_list(stmt.orelse)
      若无 jump → 设 END_BRANCH
  call_void(if_else, (cond, then_block, else_block))
```

#### 4.4.3 源码精读

`_for_stmt`：注意它对 `static_iter` 的特殊分派、归纳变量作为体块参数、以及结尾自动补 `CONTINUE`：

[src/cuda/tile/_passes/ast2hir.py:L707-L733](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L707-L733) — `for` lowering：区分 `loop`/`static_foreach`，体块以归纳变量为参数，自动补 `CONTINUE`。

`_if_stmt`：两个分支各开一个块、结尾 `END_BRANCH`，最后发 `if_else` 调用：

[src/cuda/tile/_passes/ast2hir.py:L1012-L1026](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1012-L1026) — `if` lowering。

`_while_stmt`：体块开头先插「`if cond: pass else: break`」（用 `if_else` + `BREAK` 实现），再翻循环体，外层包一个 `loop(body, None)`（`iterable=None` 表示无限循环，靠内部 break 退出）：

[src/cuda/tile/_passes/ast2hir.py:L913-L937](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L913-L937) — `while` lowering：靠 `if_else` 在体首实现条件、`loop(body, None)` 实现循环。

链式比较 `a < b < c`：拆成嵌套 `if_else`，对应注释里那段展开伪代码：

[src/cuda/tile/_passes/ast2hir.py:L499-L536](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L499-L536) — 链式比较展开为短路嵌套 `if_else`。

辅助：`_Context.new_block` 是「开块」的统一入口，**退出时把子块的 `stored_indices` 并入父块**——这就是 `stored_indices` 沿嵌套向上传播的机制：

[src/cuda/tile/_passes/ast2hir.py:L220-L234](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L220-L234) — `new_block` 上下文管理器；退出时 `old.stored_indices.update(new_block.stored_indices)`。

最后，入口块与非入口块的差别在 `_ast2hir`：入口（kernel）直接翻 body 并在结尾自动补 `RETURN`；非入口（helper 函数）则把 body 包进一个 `loop`（用 `break` 实现提前 return），靠 `$returning`/`$retval` 两个特殊局部变量传递返回意图：

[src/cuda/tile/_passes/ast2hir.py:L1203-L1233](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1203-L1233) — `_ast2hir`：入口自动补 `RETURN`；非入口用 loop+break 支持 early return。

#### 4.4.4 代码实践

**实践目标**：把一个含 `for` + `if` 的小内核，按本模块规则**手工**画出 HIR 的 `Block/Call` 草图。

**操作步骤**：

1. 准备一个最简示意内核（为聚焦控制流 HIR，使用标量运算；它足以体现 for/if 的翻译规则）：

```python
# 示例代码（用于演示 HIR 控制流结构）
@ct.kernel
def example(a: ct.Array, out: ct.Array):
    bid = ct.bid(0)        # ① 标量赋值
    acc = 0                # ② 标量赋值
    for i in range(3):     # ③ for 循环
        n = bid + i        # ④ 循环体内赋值
        if n < 5:          # ⑤ if
            acc = acc + n  # ⑥ then 分支赋值
    ct.store(out, (bid,), acc)  # ⑦ 循环外
```

2. 按 4.4.2 的规则逐行翻译，得到如下草图（`^N` 为块号，`%v` 为 Value，`<idx name>` 表示该局部变量在 `local_names` 里的下标所对应的 `ResolvedName`）：

```
^0():                                       # 根块（入口），stored_indices ⊇ {bid, acc}
    %bid = <ct.bid>(0)                      # ① bid = ct.bid(0)
    store_var(<bid>, %bid)
    %zero = identity(0)
    store_var(<acc>, %zero)                 # ② acc = 0
    loop(^1, <range(3)>)                    # ③ for i in range(3)
    <ct.store>(<out>, <(bid,)>, <load acc>) # ⑦ store
    return                                  # jump = RETURN（入口自动补）

^1(%i):                                     # for 体块，params=(%i,)；stored_indices ⊇ {i, n, acc}
    store_var(<i>, %i)                      # i = 归纳值
    %n = add(load <bid>, %i)
    store_var(<n>, %n)                      # ④ n = bid + i
    %cond = lt(load <n>, 5)                 # ⑤ n < 5
    if_else(%cond, ^2, ^3)                  #    if (n<5) ^2 else ^3
    continue                                # jump = CONTINUE（普通 for 自动补）

^2():                                       # if-then 块；stored_indices ⊇ {acc}
    %new = add(load <acc>, load <n>)
    store_var(<acc>, %new)                  # ⑥ acc = acc + n
    end_branch                              # jump = END_BRANCH

^3():                                       # if-else 块；stored_indices = ∅
    end_branch                              # jump = END_BRANCH
```

**需要观察的现象 / 预期结果**：

- 每个块**有且仅有一个**终止 `jump`：根块 `RETURN`、for 体块 `CONTINUE`、两个 if 分支 `END_BRANCH`。
- 嵌套块的 `stored_indices` 向上传播：`^2` 写了 `acc` → `^1`（for 体）也含 `acc` → 最终根块 `^0` 的 `stored_indices` 含 `{bid, acc}`（外加循环携带的 `i`/`n`）。`acc` 跨循环迭代被改写，正是下游 `hir2ir` 要为它插入 carried value / phi 的信号。
- for 体块的 `params=(%i,)`：归纳变量作为参数送入体块。

**待本地验证**：cuTile 当前没有「直接 dump HIR」的环境变量（`CUDA_TILE_DUMP_TILEIR`/`CUDA_TILE_DUMP_BYTECODE` dump 的是下游 final IR 与字节码，不是 HIR）。如要核对草图，可在 `get_function_hir` 返回前临时 `print(func_hir.body)`（`Block.__str__` 会按上面那种 `^N(...):` 格式打印），运行一次 `ct.launch` 观察输出。

#### 4.4.5 小练习与答案

**练习 1**：把上面内核里的 `if n < 5:` 改成 `if 0 < n < 5:`（链式比较），HIR 会多出什么结构？
**参考答案**：依据 [_passes/ast2hir.py:L499-L536](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L499-L536)，链式比较会展开为短路嵌套 `if_else`：先算 `c0 = (0 < n)`，在 `then` 块里再算 `c1 = (n < 5)` 并以 `END_BRANCH` 回送 `c1`，在 `else` 块里回送 `c0`（假）。所以会多出两个内部块和一次外层 `if_else` 调用。

**练习 2**：为什么 `for` 循环体里写 `break` 会被拒绝，而 `while` 里可以？
**参考答案**：见 [_passes/ast2hir.py:L1036-L1040](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L1036-L1040)：`for` 在底层编成定数计数循环（下游 `ForOp`，迭代次数编译期已知），没有「跳出」语义；而 `while` 编成通用 `LoopOp`，本身就是靠体首的 `if cond ... else: break` 退出的，所以支持 `break`（u3-l3 已建立这条结论）。

---

## 5. 综合实践

把本讲四个模块串成一个任务：**给一个稍微复杂一点的内核，产出它的完整 HIR 草图，并解释它如何对接下游。**

**任务内核**（同时含 `for`、`if`、链式比较、嵌套 `def`）：

```python
# 示例代码（用于综合实践：手画 HIR）
@ct.kernel
def k(a: ct.Array, out: ct.Array):
    s = 0
    for i in range(4):
        t = ct.load(a, (i,))           # t 是 tile
        if 0 <= i <= 2:                # 链式比较 → 嵌套 if_else
            s = s + i
    # 一个嵌套函数（仅演示作用域与 nested_functions）
    def helper(j):
        return j + 1
    ct.store(out, (0,), s)
```

**要求**：

1. 用 `ast_get_all_local_names` 的规则，列出 `k` 的 `local_names`（注意：`helper` 是 `k` 的局部名；`j` 属于 `helper`，不计入 `k`；`t`/`s`/`i` 计入 `k`）。
2. 画出 `k` 的 HIR 块树：标出根块、for 体块、两个 if 分支块、链式比较引入的内部块；为每个块标注 `jump` 与 `stored_indices`。
3. 指出哪些变量会进入下游 `hir2ir` 的「循环 carried value」处理（提示：看 for 体块及嵌套块里被 `store` 的、且在循环外仍被引用的变量——本题是 `s`）。
4. 说明 `helper` 这个嵌套 `def` 在 HIR 里如何体现（提示：它会经 `_make_closure` 翻译成一个 `entry_point=False` 的子 `hir.Function`，挂在父函数的 `nested_functions` 上；其捕获关系由 `_finalize_func` 计算进 `captures_by_depth`，见 [_passes/ast2hir.py:L113-L125](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L113-L125)）。

**自检方法**：在 `get_function_hir` 返回前临时 `print(ret)` / `print(ret.body)`，跑一次 `ct.launch`，对照你画的草图与实际打印的 `^N(...):` 结构（格式见 [hir.py:L103-L126](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py#L103-L126)）。注意：本任务内核仅用于演示 HIR 结构，部分 tile 语义（如对 tile 做整数比较）可能不被下游接受；聚焦点放在 ast2hir 产出的 HIR 形状上即可。**待本地验证。**

---

## 6. 本讲小结

- **HIR 是 AST 与 Tile IR 之间的归一层**：先把 Python 五花八门的语法节点拍平、归一成极简形态，再由 `hir2ir` 翻成具体 Tile IR，从而解耦「Python 语义归一」与「具体 IR 形状」。
- **万物皆 `Call`**：HIR 不定义一堆 Operation，而是用「函数调用」统一建模——算术是 `operator.add`、属性是 `getattr`、下标是 `operator.getitem`、字面量被包成 `identity`，连 `if/for/while` 控制流也是「对 stub 的 `Call` + 嵌套 `Block`」。
- **核心四结构**：`Value`（SSA 值 `%id`）、`Call`（唯一操作载体）、`Block`（线性 `Call` 序列 + 一个 `Jump` + `stored_indices`）、`Function`（顶层容器，持局部名表/参数下标/嵌套函数/捕获信息）。
- **`get_function_hir` 是唯一入口**：`@lru_cache` 缓存；经「取源码 → `if True:` 缩进技巧 → 建 `func_globals`（含闭包）→ 作用域分析 → 建 `_Context` → lowering → `_finalize_func`」产出 `hir.Function`，且在 `compile_tile` 中以 `entry_point=True` 只调用一次。
- **`ast_get_all_local_names` 是 lowering 的地基**：静态算清「本函数有哪些局部变量」，给每个名字分配稳定下标；变量读写据此翻译成 `load_var`/`store_var(ResolvedName)`，并用 `stored_indices` 标记「块内被改写的局部」，供下游做循环 carried value 合并。
- **控制流即结构化 `Call`**：`if`→`if_else` + 两个 `END_BRANCH` 块；`for`→`loop(body, iterable)`，体块以归纳变量为参数、结尾 `CONTINUE`；`while`→体首插 `if_else(...,else:break)` + 外层 `loop(body, None)`；链式比较与 `and/or` 展开为短路嵌套 `if_else`。

## 7. 下一步学习建议

本讲止步于「HIR 长什么样」。下一篇 **u5-l4「HIR 到 IR：hir2ir」** 会回答「这堆 `Call` + `Block` 怎么变成具体的 Tile IR Operation」：

- 重点读 [src/cuda/tile/_passes/hir2ir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py)，看它如何把 HIR 的 `callee`（`operator.add`、`hir_stubs.if_else`/`loop`、`load_var`/`store_var` 等）分派到具体的 IR Op。
- 关注它如何用 `stored_indices` 为循环构造 carried values / phi（与本讲的 `Block.stored_indices` 直接对接）。
- 配合 [src/cuda/tile/_ir/control_flow_ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py) 与 [src/cuda/tile/_ir/scope.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/scope.py) 体会 `IfElse`/`Loop` 与作用域管理。

建议在进入 u5-l4 前，先把本讲综合实践的 HIR 草图亲手画一遍——hir2ir 正是建立在这张图之上。
