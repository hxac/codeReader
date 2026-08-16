# 语句与表达式：赋值、运算符重载与作用域

## 1. 本讲目标

上一讲（u4-l1）我们搭好了 FunctionVisitor 的骨架：它继承 `ast.NodeVisitor`，把 kernel 的 AST 逐节点「重放」成 ASC-IR。本讲下钻到最基础的两类节点——**赋值语句**与**算术/比较表达式**——并拆解支撑它们的变量表 `NameScope`。学完本讲，你应该能够：

1. 说清 JIT 下一条赋值语句的两种命运：什么时候只是「Python 名字绑定」（不产生任何 IR），什么时候会级联生成 `arith.*` 操作。
2. 手工推演 `a = a + 1` 这样的连续赋值每一步生成了哪些 IR 操作、变量 `a` 先后指向哪些 IR 句柄。
3. 看懂 `get_binary_method_name` / `get_bool_method_name` 等四张「运算符翻译表」，以及 `apply_binary_method` 的正向/反向双派发逻辑。
4. 解释 NameScope 的三级查找（local → global → builtins）、`defined`/`redefined` 两个集合的真实含义，以及它们如何支撑「同名变量在循环体/分支块内外指向不同 IR 句柄」。
5. 根据 `CodegenError` / `UnsupportedSyntaxError` 的报错格式（文件、行号、源码摘录、`^` 指示）快速定位到出错的 AST 节点。

## 2. 前置知识

### 2.1 Python 的 AST 与表达式上下文（ctx）

Python 解释器执行前会把源码解析成抽象语法树（AST）。本讲会反复遇到三类节点：

- `ast.Assign`：赋值语句，如 `a = a + 1`。它有 `targets`（左边，可能是名字、元组、下标）和 `value`（右边表达式）。
- `ast.BinOp`：二元运算，如 `a + 1`。由 `left`、`op`（如 `ast.Add`）、`right` 三部分组成。
- `ast.Name`：变量名引用。它带一个 `ctx` 属性——`ast.Load` 表示「读取这个名字」，`ast.Store` 表示「写入这个名字」。同一个 `a`，出现在 `a = 1` 左边时 ctx 是 Store，出现在 `a + 1` 里时 ctx 是 Load。**这是 Python 自己的机制，不是 pyasc 发明的**，pyasc 靠它区分「查名字」和「登记名字」。

用标准库 `ast` 可以直接看到这些节点，本讲多个实践就基于它，不需要安装 pyasc。

### 2.2 Python 运算符协议：`__add__` 与 `__radd__`

`a + b` 在 CPython 里等价于先尝试 `a.__add__(b)`，若返回 `NotImplemented` 再尝试 `b.__radd__(a)`。任何类只要实现这些「魔法方法」，就能用自己的语义接管 `+`、`-`、`==` 等运算符。pyasc 正是利用这一点：`PlainValue.__add__` 不做数值计算，而是**在编译期创建一条 MLIR `arith.addi` 操作并返回新的 PlainValue**。理解了「运算符被劫持成建 IR」，本讲就懂了一半。

### 2.3 与前几讲的衔接

- u2-l3 讲过 `IRValue` 体系：`PlainValue` 是设备侧标量的延迟求值包装，`GlobalAddress` 是 Host 传入的设备指针包装，`materialize_ir_value` 是把 Python 立即数落成 IR 常量的漏斗。本讲看它们的运算符方法如何被 visitor 调用。
- u4-l1 讲过 `global_builder` 单例与 `_run_codegen` 的生命周期：所有 `create_*` 调用都写进 builder 持有的 `ir.ModuleOp`；`VisitorState` 管理遍历状态。本讲聚焦其中与语句、名字有关的部分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py) | 本讲主角：`visit_Assign`、四张运算符翻译表、`apply_binary_method`、`compute_inout`（NameScope 的消费方） |
| [python/asc/codegen/name_scope.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py) | 变量表：三级查找、`defined`/`redefined` 集合、`inherit` 作用域复制 |
| [python/asc/language/core/ir_value.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py) | 运算符重载的落点：`PlainValue.apply_binary_op`、`GlobalAddress.__add__`、`materialize_ir_value` |
| [python/asc/codegen/errors.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/errors.py) | `CodegenError` 报错格式化（源码摘录 + `^` 指示）、`UnsupportedSyntaxError` 子类 |
| [python/test/unit/codegen/test_function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py) | 本讲的「标准答案」：FileCheck 注释里写死了预期生成的 arith 节点 |
| [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) | 真实 kernel 中的赋值与运算实例 |

## 4. 核心概念与源码讲解

### 4.1 visit_Assign：赋值语句的两种命运

#### 4.1.1 概念说明

在普通 Python 里，`a = a + 1` 是「取 a 的值、加 1、把名字 a 重绑到新对象」。在 pyasc 的 JIT 编译期，这条语句被 FunctionVisitor「重放」，但语义有一个关键分裂：

- **赋值本身永远不产生 IR**。`scope.save(name, value)` 只是往变量表里登记「名字 → Python 对象」。
- **产生 IR 的是右边的表达式求值**。`a + 1` 会真的创建一条 `arith.addi` 操作，并返回一个新的 `PlainValue` 句柄；随后 `a` 这个名字被重绑到这个新句柄。

于是 RHS 的求值结果决定了赋值的「命运」：

| RHS 求值结果 | 例子 | 赋值后名字绑定到 | 是否新增 IR 操作 |
| --- | --- | --- | --- |
| Python 立即数 | `a = 5`、`n = TILE_NUM` | Python int | 否（延迟物化，用到才落 IR） |
| 参数包装对象 | `a = block_length`（int 运行时参数） | PlainValue（参数句柄的别名） | 否 |
| 表达式结果 | `a = a + 1` | 新的 PlainValue | 是（`arith.constant` + `arith.addi`） |
| API 返回对象 | `x_gm = asc.GlobalTensor()` | Tensor 包装对象 | 构造本身不产生，调方法才产生 |

这个「绑定 vs 生成」的二分法，是读懂一切 pyasc kernel 的钥匙：**语句是编译期重放的脚本，赋值只是改名字表，运算符和函数调用才往 IR 里添东西。**

#### 4.1.2 核心流程

`visit_Assign` 的分发逻辑可用伪代码描述：

```
visit_Assign(node):
    targets = node.target（AnnAssign 单目标）或 node.targets（Assign 目标列表）
    若 len(targets) != 1:            # a = b = c 不支持
        raise UnsupportedSyntaxError
    rhs = visit(node.value)          # ★ 先求右边：可能触发一串 IR 构建

    若 lhs 是 Subscript 且 Store:     # x[i] = rhs
        base = visit(lhs.value); sub = visit(lhs.slice)
        base.__setitem__(sub, rhs); return
    若 lhs 是 Attribute 且 Store:     # obj.field = rhs
        优先 base.__setattrjit__(attr, rhs)，否则 setattr
        return
    若 lhs 是 Name:                  # a = rhs
        names = [lhs 的名字字符串]     # visit 对 Store 语境的 Name 直接返回名字
    若 lhs 是全 Name 的 Tuple:       # a, b = rhs
        names = [每个名字字符串]
    否则:
        raise UnsupportedSyntaxError("Assignment target must be name or tuple of names")

    若 rhs 可迭代 且 目标不止一个:    # a, b = 1, 2
        逐个解包，个数必须相等
    否则:
        rhs_values = [rhs]           # a = [1,2,3] 时整个 list 绑给一个名字
    for name, value in zip(names, rhs_values):
        scope.save(name, value)      # ★ 只登记，不产生 IR
```

两个易错点提前点出：

1. **链式赋值 `a = b = c` 被禁止**（targets 长度必须为 1）。
2. **`a += 1` 不是独立路径**，而是先被改写成 `a = a + 1` 再重放（见 4.1.3）。

#### 4.1.3 源码精读

赋值主入口，先求 RHS、再按 target 类型分流、最后统一 `scope.save`：

[python/asc/codegen/function_visitor.py:L359-L396](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L359-L396)

> 这段代码做三件事：限定单目标；把 `x[i] = v`、`obj.f = v` 两类复合目标转交给 `__setitem__` / `__setattrjit__`；剩下的名字目标（单个或全名字元组）逐个 `self.scope.save(name, value)` 完成绑定。注意 L364 **先** `self.visit(node.value)` 求右边——所有 IR 都在这一步产生。

带类型标注的赋值 `a: asc.int_ = ...` 直接委托给同一逻辑：

[python/asc/codegen/function_visitor.py:L346-L347](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L346-L347)

> `visit_AnnAssign` 整体转调 `visit_Assign`，标注本身不参与绑定（类型由 RHS 推导）。

复合赋值 `+=` 的实现是「AST 改写再重放」：

[python/asc/codegen/function_visitor.py:L398-L408](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L398-L408)

> 它把目标 `lhs` 复制成 **Load 语境**的新节点（`ast.Name(lhs.id, ctx=ast.Load())`），手工拼出 `ast.BinOp(lhs, node.op, node.value)` 与 `ast.Assign(targets=[原目标], value=该BinOp)`，再 `self.visit(assign)`。也就是说 `cnt += step` 与 `cnt = cnt + step` 在 pyasc 里走完全相同的路径。

`visit_Name` 是名字与 ctx 分流的枢纽：

[python/asc/codegen/function_visitor.py:L635-L639](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L635-L639)

> Store 语境返回**名字字符串本身**（供 `visit_Assign` 收集目标名）；Load 语境去 `self.scope.lookup` 查值并做 `ConstExpr.unwrap` 解包。这就是为什么 `visit_Assign` 里 `self.visit(lhs)` 拿到的是字符串而不是对象。

真实 kernel 中的例子——[examples/01_add/add.py:L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L31) 的 `offset = asc.get_block_idx() * block_length`：

- RHS 是 `ast.BinOp`：`get_block_idx()` 调用先建一条 IR 操作返回 PlainValue，乘法再建一条（见 4.2）；
- 赋值只是把名字 `offset` 绑到乘法结果的 PlainValue 上。

而 [examples/01_add/add.py:L39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L39) 的 `tile_length = block_length // TILE_NUM // BUFFER_NUM` 则展示「立即数混入」：`block_length` 是 PlainValue，`TILE_NUM`/`BUFFER_NUM` 是模块级 Python int，整型 `//` 被翻译为 `DivSI`（见 4.2.3 的对照表）。

#### 4.1.4 代码实践（纯标准库，任何机器可运行）

**实践目标**：不安装 pyasc，直接用 Python 标准库观察赋值语句的 AST 结构，验证 4.1.2 的分流依据。

**操作步骤**：

1. 新建 `/tmp/ast_probe.py`（**示例代码**，非项目文件）：

```python
import ast

src = "a = a + 1\na, b = 1, 2\nx_local[i:] = y\n"
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            print(f"Assign target={ast.dump(t)[:60]}")
    if isinstance(node, ast.Name):
        print(f"  Name id={node.id!r} ctx={type(node.ctx).__name__}")
```

2. 运行 `python3 /tmp/ast_probe.py`。
3. 再对真实 kernel 做一次：把 `examples/01_add/add.py` 中 `vadd_kernel` 函数体复制进 `ast.parse`，统计 `ast.Assign` 与 `ast.Store` 的出现位置。

**需要观察的现象**：

- `a = a + 1` 中左侧 `a` 的 ctx 是 `Store`，右侧 `a` 的 ctx 是 `Load`——同名不同 ctx；
- `a, b = 1, 2` 的 target 是 `ast.Tuple`，两个元素都是 `Name`（Store），对应 `visit_Assign` 的元组分支；
- `x_local[i:] = y` 的 target 是 `ast.Subscript`（Store），对应 `__setitem__` 分支。

**预期结果**：输出中每个赋值目标都能对应到 4.1.2 伪代码的一条分支；`vadd_kernel` 里 `offset = ...`、`tile_length = ...`、`buf_id = ...` 等行的 target 均为单个 `Name`（Store）。

#### 4.1.5 小练习与答案

**练习 1**：`a = b = 1` 在 pyasc kernel 里会发生什么？
**答案**：`visit_Assign` 检查 `len(targets) != 1`，链式赋值有两个 target，抛出 `UnsupportedSyntaxError`，报错信息为 "Assignment operator must have exactly one target"，并附源码摘录与 `^` 指示（见 4.2.4 报错格式）。

**练习 2**：kernel 中写 `nums = [1, 2, 3]`，`nums` 绑定到什么？会产生 IR 吗？
**答案**：`visit_List` 返回 Python list（三个 int 元素），单个名字目标走 `rhs_values = [rhs]` 整体绑定；**不产生任何 IR**。只有当 list 里的值被用于 IR 上下文（如作下标）时才会经 `materialize_ir_value` 物化成常量。单元测试 `func_visit_list` 正是返回一个 list 再整体使用的例子。

**练习 3**：`cnt += step`（`step` 是 int 运行时参数）会生成哪些 IR 操作？
**答案**：`visit_AugAssign` 改写出 `cnt = cnt + step`，等价于 `visit_BinOp(ast.Add)` → `PlainValue.__add__` → `apply_binary_op("AddI", "AddF")`：物化 `step` 的句柄本身就是函数参数（无需常量），所以新增一条 `arith.addi %cnt, %step`。若右侧是立即数（如 `cnt += 1`）则会先多出一条 `%c1_i32 = arith.constant 1 : i32`。

---

### 4.2 二元运算映射：从 AST 运算符到魔法方法再到 IR

#### 4.2.1 概念说明

`visit_BinOp` 自己不做任何算术，它只做一次「查表翻译」：把 AST 的运算符节点类（`ast.Add`、`ast.Sub`……）映射成 Python 魔法方法名（`__add__`、`__sub__`……），然后把实际计算交给操作数对象的方法。这样一套翻译逻辑同时服务三类操作数：

1. **两个纯 Python 值**：`getattr(lhs, '__add__')(rhs)` 就是普通 Python 加法，结果是 Python 值——**不产生 IR**。这解释了 `TILE_NUM * 2` 这类编译期常量折叠。
2. **至少一侧是 IR 包装对象**（PlainValue/GlobalAddress/Tensor）：运算符方法内部调用 builder 创建 IR 操作，返回新的 IR 包装对象——**产生 IR**。
3. **左侧是立即数、右侧是 IR 值**（如 `1 + x`）：走反向方法 `__radd__`，同样产生 IR。

比较运算（`==`、`<`……）与逻辑运算（`and`、`or`）有各自独立的翻译表；一元运算（`-x`、`not x`）还有第四张表。四张表合起来就是 pyasc 的「运算符方言边界」：表里没有的运算符（比如 `@`、`:=`）会在查表时直接 `NotImplementedError`。

#### 4.2.2 核心流程

`a + 1`（设 `a` 是 PlainValue，dtype 为 int32）的完整链路：

```
visit_BinOp
 ├─ lhs = visit(a)  → PlainValue(H0)          # 查 NameScope
 ├─ rhs = visit(1)  → Python int 1
 ├─ get_binary_method_name(ast.Add) → '__add__'
 └─ apply_binary_method('__add__', lhs, rhs)
      ├─ lhs 有 builder 支持 → 不走反向分支
      ├─ lhs.__add__(1)
      │    └─ PlainValue.apply_binary_op(self, 1, "AddI", "AddF")
      │         ├─ infer_common_type → 结果类型 = PlainValue 一侧的 dtype（int32）
      │         ├─ lhs 已是 PlainValue，原样保留
      │         ├─ rhs = materialize_ir_value(1, int32)
      │         │      └─ %c1_i32 = arith.constant 1 : i32     ← 新 IR 操作 ①
      │         └─ builder.create_arith_AddIOp(H0, %c1_i32)
      │                └─ %1 = arith.addi %H0, %c1_i32          ← 新 IR 操作 ②
      └─ 返回 PlainValue(%1, int32)                              # a 随后被重绑到它
```

类型推导的一条硬规则来自 `infer_common_type`：**运算结果类型永远沿用 PlainValue 一侧的 dtype**；若两侧都是 PlainValue，用**左侧**的。因此 `int32参数 + int64常量` 不会自动升位——常量会被 cast 到左侧类型（物化时直接按该类型生成常量）。整型与浮点分别选用不同 builder 方法（`AddI`/`AddF`），组合不合法时（如浮点取模 `RemSI` 无浮点变体）`builder_attr` 为 `None`，抛 `ValueError`。

比较运算的结果是 **int1（一比特整数）** 而不是 Python bool：`__eq__` 等方法调用 `apply_compare_op`，按整/浮选择 `create_arith_CmpIOp`/`CmpFOp` 与对应谓词（eq/ne/sge/sgt/sle/slt 或 OEQ/ONE/…），返回 `PlainValue(handle, dtype=int1)`。它可以直接喂给 `if`/`while` 的条件（经 `ensure_bool_value` cast 成 bit），但**不能**当普通 int 参与算术。

逻辑运算有一个重要语义差异：`and`/`or` 被映射为 `logical_and`/`logical_or`，两侧**都会**被求值（先物化成 i1，再 `AndI`/`OrI`）——没有 Python 的短路求值。所以 `x != 0 and 10 // x > 1` 这种依赖短路的写法在 kernel 里两支都会执行，务必拆成嵌套 `if`。

#### 4.2.3 源码精读

四张翻译表（二元、布尔、一元、比较）：

[python/asc/codegen/function_visitor.py:L94-L113](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L94-L113)

> 二元表：`ast.Add→__add__`、`ast.Sub→__sub__`、`ast.Mult→__mul__`、`ast.Div→__truediv__`、`ast.FloorDiv→__floordiv__`、`ast.Mod→__mod__`、`ast.Pow→__pow__`、移位与位运算映射 `__lshift__`/`__rshift__`/`__and__`/`__or__`/`__xor__`。查不到即抛 `NotImplementedError`。

[python/asc/codegen/function_visitor.py:L115-L124](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L115-L124)

> 布尔表：`ast.And→logical_and`、`ast.Or→logical_or`。注意映射到的不是 Python 魔法方法名，而是 PlainValue 上的具名方法（见 ir_value.py L327-L332）。

[python/asc/codegen/function_visitor.py:L126-L152](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L126-L152)

> 一元表（`__neg__`/`__pos__`/`__not__`/`__invert__`）与比较表（六个有序/相等比较到 `__eq__`…`__le__`）。`is`/`is not` 不在表里，而是 `visit_Compare` 里直接做 Python 身份比较（编译期判定）。

正向/反向双派发：

[python/asc/codegen/function_visitor.py:L164-L171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L164-L171)

> `apply_binary_method` 的规则：若**左**操作数没有 builder 支持而**右**操作数有（如 `1 + x`），直接调反向方法 `rhs.__radd__(lhs)`——反向方法名用正则 `__(.*)__ → __r\1__` 从正向名机械生成；否则先 `lhs.__add__(rhs)`，返回 `NotImplemented` 再回退反向。若两侧都是纯 Python 值，这条路径退化为普通 Python 运算（不产生 IR）。「builder 支持」的判定见 `has_builder_support`（[L154-L156](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L154-L156)）：BaseTensor、GlobalAddress、PlainValue 三类。

表达式入口只有三行：

[python/asc/codegen/function_visitor.py:L424-L428](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L424-L428)

> `visit_BinOp`：递归求两个子表达式 → 查表 → `apply_binary_method`。`visit_Compare`（[L448-L459](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L448-L459)）与之同构，但额外限定「单操作符单比较数」（`1 < x < 10` 链式比较不支持），`is`/`is not` 直接返回 Python bool。`visit_BoolOp`（[L430-L436](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L430-L436)）限定恰好两个值，`a and b and c` 必须加括号分组。

运算的真正落点在 PlainValue：

[python/asc/language/core/ir_value.py:L255-L264](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L255-L264)

> `apply_binary_op` 是所有二元算术的统一实现：`infer_common_type` 定结果类型 → 两侧 `materialize_ir_value` 到该类型 → 按整/浮选拼出 builder 方法名 `create_arith_{AddI|AddF|...}Op` → 调用并包成新 PlainValue。每个魔法方法（如 [`__add__`，L74-L76](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L74-L76)）只是传入整型/浮点两个变体名的薄封装。

类型推导与比较：

[python/asc/language/core/ir_value.py:L243-L253](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L243-L253)

> `infer_common_type`：只认 PlainValue 一侧（优先左侧）的 dtype，两侧都不是 PlainValue 则抛 `ValueError`。这就是「结果类型跟随 IR 值一侧」规则的出处。

[python/asc/language/core/ir_value.py:L273-L282](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L273-L282)

> `apply_compare_op`：按公共类型选 `create_arith_CmpIOp`（eq/ne/sge/sgt/sle/slt，**带符号**比较）或 `create_arith_CmpFOp`（OEQ/ONE/OGE/…），结果 dtype 固定为 `int1`。

常量物化漏斗：

[python/asc/language/core/ir_value.py:L344-L363](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L344-L363)

> `materialize_ir_value`：PlainValue 直接（必要时 cast）；ConstExpr 先解包再递归；Python int/float 按目标类型规整后由 `convert_value`（[L366-L396](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L366-L396)）生成 `builder.get_i32(1)` 之类的常量句柄。这正是 `a + 1` 里那个 `1` 变成 `%c1_i32 = arith.constant 1 : i32` 的地方。

指针加法是「同一运算符、不同接收者、不同 IR」的最佳例证：

[python/asc/language/core/ir_value.py:L45-L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L45-L51)

> `GlobalAddress.__add__` 生成的是 `emitasc.PtrOffsetOp`（先把偏移 IndexCast 成 index 类型），语义是**指针偏移**而非算术加。[examples/01_add/add.py:L35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L35) 的 `x + offset` 走的就是这条路。

为方便查阅，整型运算符与 IR 操作对照如下（浮点侧把 `I` 换成 `F`，无浮点变体的项标 ✗）：

| Python 运算符 | 魔法方法 | 整型 IR 操作 | 浮点变体 |
| --- | --- | --- | --- |
| `+` | `__add__` | `arith.addi` | `arith.addf` |
| `-` | `__sub__` | `arith.subi` | `arith.subf` |
| `*` | `__mul__` | `arith.muli` | `arith.mulf` |
| `/`、`//` | `__truediv__`/`__floordiv__` | `arith.divsi`（向零截断） | `arith.divf` |
| `%` | `__mod__` | `arith.remsi` | ✗（ValueError） |
| `**` | `__pow__` | ✗（NotImplementedError） | ✗ |
| `<<`/`>>` | `__lshift__`/`__rshift__` | `arith.shli`/`arith.shrsi` | ✗ |
| `&`/`\|`/`^` | `__and__`/`__or__`/`__xor__` | `arith.andi`/`arith.ori`/`arith.xori` | ✗ |
| `==` 等六个比较 | `__eq__`…`__le__` | `arith.cmpi`（结果 i1） | `arith.cmpf` |
| `and`/`or` | `logical_and`/`logical_or` | `arith.andi`/`arith.ori`（两侧 i1） | — |

注意两个语义陷阱：整型 `//` 与 `%` 映射的是**带符号截断/取余**（C 语义），与 Python 对负数的 floor 语义不同；`**` 直接不支持。

#### 4.2.4 代码实践

**实践目标**：用单元测试里的 FileCheck 注释验证 4.2.3 的对照表——测试注释就是「标准答案」。

**操作步骤**：

1. 打开 [python/test/unit/codegen/test_function_visitor.py:L41-L74](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L41-L74)，阅读 `test_func_visit_bool_op` 与 `test_func_visit_compare` 两个用例的 `# CHECK:` 注释。
2. 对 `test_func_visit_compare_kernel` 里的每条 `if a > b:` / `ans += 1`，按 4.2.2 的链路手工推演应生成的 arith 节点序列（提示：`ans = 0` 先物化 `%c0_i32 = arith.constant 0 : i32`，可对照 [`test_func_visit_constant`，L77-L90](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L77-L90) 的两条 CHECK）。
3. 若本地已构建 pyasc（`pip install -e .` 完成），运行：

```bash
cd python/test/unit
python3 -m pytest codegen/test_function_visitor.py -k "bool_op or compare or constant" --skip-filecheck -v
```

（`--skip-filecheck` 跳过对 FileCheck 外部工具的依赖；`mock_jit` fixture 已把 launcher/compiler 打桩，只走 codegen。）

4. 观察报错格式：同文件 [L263-L271](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L263-L271) 的 `test_error_test_print` 断言报错串包含 `"NameError: print is not defined"`——`print` 不在 NameScope 的 builtins 白名单里，`NameError` 被包装成 `CodegenError`（见 4.3.3）。

**需要观察的现象**：`test_func_visit_bool_op_kernel` 中 `value >= min_threshold and value <= max_threshold` 对应两条 `arith.cmpi` 加一条 `arith.andi`；`cnt += step` 对应 `arith.addi`；`ret = cnt == step` 对应一条结果为 i1 的 `arith.cmpi`。

**预期结果**：手工推演的节点序列与 CHECK 注释一致；pytest 通过（**待本地验证**——需要已构建的 pyasc 环境；无环境时完成步骤 1、2 的纸面推演即可）。

#### 4.2.5 小练习与答案

**练习 1**：`offset = asc.get_block_idx() * block_length` 中，如果写成 `block_length * asc.get_block_idx()`（交换左右），生成的 IR 有何不同？
**答案**：基本等价。左侧 `block_length` 是 PlainValue、有 builder 支持，仍走正向 `__mul__`；结果类型取左侧（block_length 的 int 类型）。只有当**左侧是无 builder 支持的立即数**（如 `8 * block_length`）时才会改走 `__rmul__`，生成的 `arith.muli` 操作数顺序对调、结果类型改由右侧 PlainValue 决定。

**练习 2**：kernel 里写 `p = x % 3`（`x` 是 float32 参数）会发生什么？
**答案**：`__mod__` 只注册了整型变体 `"RemSI"`（浮点变体为 `None`）。`x` 是浮点 PlainValue，`infer_common_type` 取 float32，`apply_binary_op` 中 `builder_attr` 为 `None`，抛出 `ValueError: Binary operation is not supported between ...`，该异常在 `visit` 的包装层变成 `CodegenError`。

**练习 3**：为什么 `if a and b:` 里 `a`、`b` 都会被求值？这和 Python 有何不同？
**答案**：`visit_BoolOp` 把 `and` 翻译成 `logical_and` 方法，`apply_bool_op`（ir_value.py L266-L271）先把**两侧**都物化成 i1 再生成 `arith.andi`——两侧表达式都已执行完 IR 构建，不存在「左边为假就不算右边」的短路。Python 的 `and` 是短路语法糖；pyasc kernel 里若需短路语义，应写成嵌套 `if`。

---

### 4.3 NameScope：作用域、变量遮蔽与跨块重绑定

#### 4.3.1 概念说明

NameScope 是 FunctionVisitor 的「变量表」，回答一个问题：**遇到一个名字（Load 语境），当前应该拿到哪个 Python 对象？** 它用三级存储按序查找：

1. `local_vars`：函数内绑定（含形参、赋值、循环变量）；
2. `global_vars`：模块级全局变量 **与 ConstExpr 实参合并而成**（构造时 `merge_dict(global_vars, spec.constexprs)`，所以 constexpr 参数像全局变量一样可查）；
3. `builtins`：白名单内置（dict/float/int/isinstance/issubclass/len/list/range/repr/str/tuple/type）——**注意没有 `print`、`abs`、`min`、`max`**，用了就以 `NameError` 落幕。

除了三级查找，NameScope 还维护两个集合，这是本模块最精妙也最容易被误读的部分：

- `defined`：**本层**第一次绑定的名字；
- `redefined`：**改写了「进入本层之前就存在」的名字**。

关键在于 `inherit()` 的行为：进入循环体/分支块时，新 NameScope **复制** `local_vars`（名字都能查到）但**清空** `defined`/`redefined`（重新记账）。于是在块内给外层已有的名字赋值时：名字在 `local_vars` 里（继承来的）但不在本层 `defined` 里 → 记入 `redefined`。而同一层内的重复赋值（`a = x; a = a + 1` 都在函数顶层）只会进一次 `defined`、**不会**进 `redefined`。

`redefined` 不是给用户看的——它是 `compute_inout` 的输入：块内改写过的外层变量，就是需要跨块传递的「块进块出」变量（循环携带依赖），会被接成 `scf.for` 的 iter_args/yield 或 `scf.if` 的 results。这直接回答了本讲标题里的问题——**同名变量如何在不同块中指向不同 IR 句柄**。

#### 4.3.2 核心流程

`save` 的记账状态机（一段伪代码 + 一张三幕剧）：

```
save(name, value):
    若 name 不在 local_vars:      defined.add(name)      # 本层首次定义
    否则若 name 不在本层 defined:  redefined.add(name)    # 改写了外层/更早存在的名字
    local_vars[name] = value                             # 无论哪种，都重绑
```

同名变量跨块重绑定的「三幕剧」（以 `a = 0` 后接 `for i in range(n): a = a + 1` 为例）：

```
第一幕（函数顶层）:
    a ──► PlainValue(H0)            # %c0_i32 = arith.constant 0；a 记入 defined

第二幕（进入 for 体，visit_region → scope = 外层.inherit()）:
    副本能查到 a ──► H0（继承）      # defined/redefined 已清空
    a = a + 1 → 生成 H1 = arith.addi H0, %c1
    a ──► PlainValue(H1)            # a 在 local_vars 但不在本层 defined → redefined.add(a)
    compute_inout 检测到 redefined={a}：
      - init_handles : a → H0      # 接到 scf.for 的 iter_arg
      - yield_handles: a → H1      # 接到 scf.yield
    （若内外类型不同，如 int→Tensor，直接 UnsupportedSyntaxError）

第三幕（离开 for 体，回到外层 scope）:
    scf.for 的结果句柄 H2 = ...
    scope.save('a', PlainValue(H2))  # a ──► H2，后续引用 a 都用循环结果
```

三幕之间，名字 `a` 先后指向 H0、H1（块内副本）、H2（块外恢复后）——**外层与内层互不污染**：块内改的只是 `local_vars` 的副本，外层在第二幕期间始终指向 H0；这正是 `inherit()` 复制而非共享的目的。`if/elif` 分支同理：`compute_inout` 分别对 then/else 两块记账，`redefined` 的并集经 `scf.if` 的 results 合并，块结束后统一重绑。

支撑这套机制的还有两个上下文管理器：`nest_scope`（只切换 scope，`with` 语句用）和 `visit_region`（切 scope + 保存/恢复 IR 插入点 + 关闭 return，for/if/while/子函数都用）。块级作用域的生命周期完全由它们的一进一出决定。

#### 4.3.3 源码精读

NameScope 全文很短，值得整读：

[python/asc/codegen/name_scope.py:L12-L29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L12-L29)

> 类级 `builtins` 白名单：十二个内置。`print` 不在其中，这解释了单元测试里 `print(age)` 报 `NameError: print is not defined`。

[python/asc/codegen/name_scope.py:L31-L43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L31-L43)

> 构造与 `inherit`：`inherit` 复制 `local_vars`（副本！），`defined`/`redefined` 因新建对象而天然为空；`copy_globals=True` 时连 global_vars 也复制（用于子函数内联场景隔离全局）。

[python/asc/codegen/name_scope.py:L45-L57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L45-L57)

> `save` 的记账逻辑与 `lookup` 的三级查找。`lookup` 用 `sentinel` 对象区分「存的是 None」与「不存在」，找不到抛 `NameError`——这个异常随后会被 visitor 包装（见下）。[`reset_def`，L59-L61](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L59-L61) 清空两个集合，当前仓库内暂无调用方，属预留工具方法。

visitor 侧的接线：

[python/asc/codegen/function_visitor.py:L84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L84)

> FunctionVisitor 构造时创建初始 NameScope：`merge_dict(global_vars, spec.constexprs)` 把模块全局与本次调用的 ConstExpr 实参并成 global 层——ConstExpr 参数因此能像全局常量一样被 `visit_Name` 查到并解包。

[python/asc/codegen/function_visitor.py:L281-L288](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L281-L288)

> `nest_scope`：进入时 `self.scope = outer_scope.inherit()`，退出时**无条件还原**外层对象（try/finally）。块内一切绑定随副本一起消失。

[python/asc/codegen/function_visitor.py:L328-L340](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L328-L340)

> `visit_region`：在 `nest_scope` 的基础上再保存/恢复 IR 插入点与 `return_allowed` 状态。for/if/while 的「块」都由它界定——scope 的进出与 IR 块的进出同步。

`redefined` 的消费方——`compute_inout`：

[python/asc/codegen/function_visitor.py:L213-L241](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L213-L241)

> 在继承出的作用域里重放块内语句后，对 `self.scope.redefined` 中每个名字：① 用外层 scope 查旧值、本层查新值，`type(old) is not type(new)` 即报 UnsupportedSyntaxError（同名变量跨块改类型被禁止，如 int 改成 Tensor）；② 旧值物化为 `init_handles`（块入口）、新值物化为 `yield_handles`（块出口），打包进 `BlockInOut`。for 循环把它们接到 iter_args/yield，if 接到 results（细节在 u4-l3 展开）。

块结束后的「重绑定」：

[python/asc/codegen/function_visitor.py:L513-L516](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L513-L516)

> for 循环：`block_inout.yield_values` 中的每个名字，用 `value.from_ir(op.get_result(i))` 从 `scf.for` 的结果句柄重建 PlainValue，`scope.save` 回外层——即「第三幕」的 `a ──► H2`。[visit_If 的对应代码，L593-L596](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L593-L596) 同构，只是结果来自 `scf.if`。

未定义名字如何变成友好报错：

[python/asc/codegen/function_visitor.py:L293-L312](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L293-L312)

> `visit` 包装层：每个节点进入前用 `node.lineno/col_offset` 刷新 IR 位置信息（`set_loc`），再委托给 `ast.NodeVisitor.visit`；捕获到的任何非 CodegenError 异常（如 NameScope 抛的 `NameError`、PlainValue 抛的 `ValueError`）在 `capture_exceptions=True`（默认）时统一包成 `CodegenError`，消息形如 `"NameError: print is not defined"`；`CodegenError` 及其子类则直接透传。

[python/asc/codegen/errors.py:L26-L49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/errors.py#L26-L49)

> `format_message` 的输出格式：`at <source>:行:列:` + 出错行**前** 3 行源码摘录 + 按列偏移缩进的 `^` 指示 + 出错行**后** 3 行 + 具体错误消息。`UnsupportedSyntaxError` 是 `CodegenError` 的空子类——「语法不支持」与「编译过程出错」共享同一报错版式，靠消息内容区分。

#### 4.3.4 代码实践（纯标准库，可真实运行）

**实践目标**：用最小复刻验证 4.3.2 的 `save` 记账状态机——特别是「同层重复赋值不进 `redefined`、块内改写外层名字才进」。

**操作步骤**：

1. 新建 `/tmp/scope_probe.py`，内容为 **示例代码**（照抄 name_scope.py 的核心逻辑，去掉项目依赖）：

```python
class NameScope:
    def __init__(self, local_vars=None):
        self.local_vars = {} if local_vars is None else local_vars
        self.sentinel = object()
        self.defined = set()
        self.redefined = set()

    def inherit(self):
        return NameScope(self.local_vars.copy())   # 副本 + 空账本

    def save(self, name, value):
        if name not in self.local_vars:
            self.defined.add(name)
        elif name not in self.defined:
            self.redefined.add(name)
        self.local_vars[name] = value

    def lookup(self, name):
        val = self.local_vars.get(name, self.sentinel)
        if val is not self.sentinel:
            return val
        raise NameError(f"{name} is not defined")

outer = NameScope()
outer.save("a", "H0")            # 第一幕：a = 0
outer.save("a", "H1")            # 同层重复赋值！
print("outer  :", outer.local_vars, "defined =", outer.defined, "redefined =", outer.redefined)

inner = outer.inherit()          # 第二幕：进入 for 体
inner.save("a", "H2")            # 块内改写外层名字
print("inner  :", inner.local_vars, "defined =", inner.defined, "redefined =", inner.redefined)
print("outer.a:", outer.lookup("a"))   # 外层未被污染
try:
    outer.lookup("nosuch")
except NameError as e:
    print("lookup :", e)
```

2. 运行 `python3 /tmp/scope_probe.py`。

**需要观察的现象**：

- outer 层两次 `save("a", ...)` 后 `defined = {'a'}`、`redefined = set()`——同层重复赋值**不**进 redefined；
- inner 层一次 `save("a", ...)` 后 `redefined = {'a'}`——因为它在 local_vars 里但不在本层 defined 里；
- `outer.lookup("a")` 仍返回 `'H0'`（块内只改副本）；查不到的名字抛 `NameError: nosuch is not defined`。

**预期结果**：输出与本讲 4.3.2 的三幕剧完全吻合；这正是 `compute_inout` 能精确找出循环携带变量的原因。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `print(x)` 在 kernel 里报的是 `CodegenError`，消息却是 `NameError: print is not defined`？
**答案**：`print` 不在 NameScope 的 builtins 白名单（只有 dict/float/int/isinstance/issubclass/len/list/range/repr/str/tuple/type）。`visit_Name` → `lookup` 抛出标准 `NameError`；`visit` 包装层捕获所有非 CodegenError 异常，按 `f"{类名}: {消息}"` 包成 `CodegenError` 并保留源码摘录。

**练习 2**：kernel 顶层写 `a = 1`，随后在 for 循环体内写 `a = x_gm`（一个 GlobalTensor），会发生什么？
**答案**：块内 `a` 的类型从 PlainValue 变成 Tensor。`compute_inout` 对 `redefined` 中的 `a` 做类型一致性检查（`type(old) is not type(new)`），抛 `UnsupportedSyntaxError`，消息说明初始类型与新类型。同名变量跨块**改类型**被禁止，但跨块**同类型重赋值**（int→int）是允许的。

**练习 3**：ConstExpr 参数为什么能直接在 kernel 体内当常量用（如 `for i in range(TILE_NUM)`）？
**答案**：FunctionVisitor 构造 NameScope 时执行 `merge_dict(global_vars, spec.constexprs)`，ConstExpr 实参与模块全局变量合并进同一查找层；`visit_Name` 查到后经 `ConstExpr.unwrap` 解包出 Python 值，参与编译期运算，全程不产生 IR。

---

## 5. 综合实践

把本讲三个模块串成一次完整的「赋值追踪 + 报错观察」实验（对应规格中的实践任务）。以下需要已按 u1-l2 构建好的 pyasc 环境；无环境时完成步骤 1 的纸面推演与第 3 节的纯标准库实践。

**任务**：在 kernel 中写三行连续赋值，找出每次赋值生成的 IR 操作与 `a` 的指向变化；再故意触发未定义变量，收集报错格式。

**步骤**：

1. 基于 `examples/01_add/add.py` 复制出 `/tmp/add_trace.py`，在 `vadd_kernel` 开头（`offset` 赋值之后）插入三行（**示例代码**）：

```python
    a = block_length          # 绑定：a ──► 参数句柄（int32），无新 IR
    a = a + 1                 # 生成 %c1_i32 = arith.constant 1 与 %add = arith.addi，a ──► 新句柄
    a = a * 2                 # 生成 %c2_i32 = arith.constant 2 与 %mul = arith.muli，a ──► 新句柄
    offset = a // USE_TILE    # 让 a 真正被使用（除法生成 arith.divsi）
```

   并在文件顶部补一个常量 `USE_TILE = 1`。
2. 设置 dump 环境变量后运行（Model 仿真模式，无需 NPU）：

```bash
export PYASC_DUMP_PATH=/tmp/pyasc_dump
python3 /tmp/add_trace.py -r Model
```

3. 打开 `/tmp/pyasc_dump/` 下的 `codegen.mlir`（**Pass 之前**的前端产物，保证逐语句急切发射的运算都在），在 `vadd_kernel` 函数体开头找到：
   - `%c1_i32 = arith.constant 1 : i32` 与 `arith.addi`；
   - `%c2_i32 = arith.constant 2 : i32` 与 `arith.muli`；
   - （若保留第 4 行）`arith.constant 1` 与 `arith.divsi`。
   记录三行赋值各自「新增的 IR 操作 + `a` 先后绑定的三个句柄」，与 4.1.2/4.2.2 的推演对照。
4. 复制一份改名为 `/tmp/add_err.py`，把 `a = a + 1` 改成 `a = b + 1`（`b` 未定义），运行 `python3 /tmp/add_err.py -r Model`，记录完整报错：`at <source>:行:列:`、前 3 行源码摘录、`^` 指示的位置、消息 `NameError: b is not defined`。
5. （可选）把 `a = a + 1` 挪进 `for i in range(TILE_NUM):` 循环体内并 `print` 前后两份 `codegen.mlir` 中与 `a` 有关的行，观察 `scf.for` 的 iter_args/yield 如何把 `a` 接成循环携带变量——为 u4-l3 热身。

**预期结果**（**待本地验证**，需构建好的环境）：

- `codegen.mlir` 中三行赋值按顺序产生 `constant 1`、`addi`、`constant 2`、`muli`；`a` 依次指向参数句柄、addi 结果、muli 结果；
- 未定义变量版本抛出的 `CodegenError` 报错包含源码定位与 `^` 指示，能直接指到 `a = b + 1` 那一行；
- 若无环境，用 4.1.4 的 `ast` 探针 + 4.3.4 的 scope 探针完成同等深度的纸面推演，并用 `python/test/unit/codegen/test_function_visitor.py` 的 CHECK 注释做交叉验证。

## 6. 本讲小结

- **赋值不产生 IR**：`visit_Assign` 先求 RHS（这一步才级联建 IR），再把名字经 `scope.save` 绑到结果对象；`x[i]=v`、`obj.f=v` 分别转交 `__setitem__` 与 `__setattrjit__`；链式赋值被禁止；`+=` 是「AST 改写成 `a = a + b` 再重放」。
- **运算符走查表翻译**：`get_binary_method_name` 等四张表把 AST 运算符映射到魔法方法或具名方法；`apply_binary_method` 按「左侧是否有 builder 支持」决定正向/反向（`__radd__`）派发；两个纯 Python 值相运算退化为普通 Python 计算，不产生 IR。
- **PlainValue 是运算的落点**：`apply_binary_op` 以 `infer_common_type`（结果类型跟随 IR 值一侧、优先左侧）选整型/浮点 builder 方法；比较产生 `int1`；`GlobalAddress.__add__` 生成的是指针偏移而非算术加；`//`、`%` 是 C 截断语义，`**` 不支持。
- **NameScope 三级查找**：local（含形参与块内绑定）→ global（模块全局 + ConstExpr 实参合并）→ builtins 白名单（无 `print`）；查不到抛 `NameError`，被 `visit` 包装层转成带源码定位的 `CodegenError`。
- **defined/redefined 是跨块传递的记账**：`inherit()` 复制 local_vars 但清空账本；块内改写外层名字才进 `redefined`；`compute_inout` 据此把这类名字接成 `scf.for`/`scf.if` 的块进块出值，块结束后用 `from_ir(op.get_result(i))` 把名字重绑到结果句柄——同名变量由此在不同块指向不同 IR 句柄，且外层不被块内污染。

## 7. 下一步学习建议

本讲只处理了「直线代码」的语句与表达式。下一讲 **u4-l3 控制流：for 循环、if 分支与 range/static_range** 将把 4.3 的 `compute_inout` 讲透：`BlockInOut.init_handles/yield_values` 如何变成 `scf.for` 的 iter_args 与 `scf.yield`、`if` 分支的返回类型如何经 `ReturnTypesDict` 合并、`static_range` 如何把循环在编译期完全展开。建议提前阅读 [python/asc/language/core/range.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/range.py)，并重看 `visit_For`（[function_visitor.py:L479-L516](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L479-L516)）与本讲 4.3.3 的衔接处。若想先横向巩固，可重跑 4.1.4 的 AST 探针去解析一个带 `for`/`if` 的 kernel，观察 `ast.For`/`ast.If` 节点结构。
