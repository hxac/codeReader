# u4-l5 语法支持边界与错误诊断

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 pyasc 语法支持边界的**实现机制**：它不是一份逐条硬编码的「禁止清单」，而是「`FunctionVisitor` 实现了哪个 `visit_*` 方法，哪种 AST 节点就被支持；没实现的节点落进 `generic_visit` 统一报错」。
2. 掌握**内置函数白名单**：`NameScope.builtins` 里登记了哪 12 个名字、它们在编译期如何被求值，以及 `print` 为什么不在名单里。
3. 读懂 `CodegenError` / `UnsupportedSyntaxError` 的报错格式：`at <source>:行:列`、上下文摘录与 `^` 指示是如何拼出来的，报错行号与真实文件行号如何换算。
4. 区分**两层报错**：发生在 codegen 之前参数处理层的裸 `TypeError`（无源码摘录）与发生在语法重放层的 `CodegenError`（带源码定位），并能据此快速判断错误发生在哪一层。
5. 会用 `assert` 语句 / `static_assert` 做编译期检查，并理解为什么对运行时值做断言会被拒绝。

## 2. 前置知识

本讲是第 4 单元的收尾讲，建立在前几讲的认知之上：

- **编译期重放**（u4-l1）：`FunctionVisitor` 继承 `ast.NodeVisitor`，把 kernel 的 AST 逐节点「重放」成 ASC-IR。`visit` 方法负责分发：有 `visit_Xxx` 方法的节点被翻译，没有的节点会怎样——正是本讲的第一个主题。
- **NameScope 三级查找**（u4-l2）：`visit_Name` 解析名字时按 local → global（含 ConstExpr 实参）→ builtins 白名单的顺序查找，三级都 miss 时抛 `NameError`。本讲展开最后一级 builtins 的内容。
- **参数四分类与 get_arg_type**（u3-l3）：Host 传来的实参先经 `JITFunction.get_arg_type` 映射为 `PlainArgType` / `PointerArgType` 等类型；`str`/`list`/`dict` 这类值会在这一层被 `TypeError` 拒绝，**根本到不了语法重放**。这构成了本讲的「两层报错」。
- **源码捕获时机**（u3-l2）：`Function` 基类在**装饰时**就用 `inspect.getsourcelines` 抓取源码、用 `ast.parse` 得到 `FunctionDef`。这份源码行列表 `self.src` 正是后面错误信息里上下文摘录的来源。
- **AST 行号的两个基准**（u3-l2）：AST 是从「剥掉装饰器、从 `def` 行开始」的源码解析的，所以 `node.lineno` 以 `def` 行为第 1 行；而 `self.src` 含装饰器行。理解这个基准差是读懂报错行号的关键。

一个容易误解的事实先澄清：[docs/python_syntax_support.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md) 里那份「支持/不支持清单」是**结果**，不是**原因**。真正的因果链是：`function_visitor.py` 里实现了哪些 `visit_*` 方法 → 哪些语法可用。文档告诉你「什么不能用」，源码告诉你「为什么不能用、报错从哪一行代码抛出」。本讲把两边对齐。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py) | 本讲主角：`generic_visit`（不支持语法的统一落点）、`visit`（分发与异常包装）、各 `visit_*` 方法中的显式限制、`visit_Assert`（编译期断言） |
| [python/asc/codegen/errors.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/errors.py) | `CodegenError` 与 `UnsupportedSyntaxError`：报错消息的组装逻辑，全文仅 49 行 |
| [python/asc/codegen/name_scope.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py) | 内置函数白名单 `NameScope.builtins` 与三级 `lookup` |
| [python/asc/codegen/function.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py) | `Function` 基类：装饰期捕获 `self.src`（含装饰器）与 AST（不含装饰器）；`get_function_node` 在装饰期就拒绝非 `FunctionDef` 的定义 |
| [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py) | `get_arg_type`（参数层报错的来源）、`_run_codegen`（语法层报错的发生地） |
| [python/asc/language/core/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py) | `static_assert`：`assert` 语句背后的编译期断言实现 |
| [python/asc/language/basic/dump_tensor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/dump_tensor.py) | `asc.printf`：`print` 的合法替代品，向 IR 追加 `asc.PrintfOp` |
| [docs/python_syntax_support.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md) | 官方支持/不支持语法清单（本讲的对答案材料） |
| [python/test/unit/codegen/test_function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py) | 三个现成的报错用例：`assert`、`str` 实参、`print`，是本讲实践任务的模板 |

## 4. 核心概念与源码讲解

### 4.1 语法白名单机制：没有「禁止清单」，只有「支持清单」

#### 4.1.1 概念说明

Python 的 `ast.NodeVisitor` 有一条默认分发规则：访问 `ast.Xxx` 节点时，先找 `visit_Xxx` 方法；找不到就调用 `generic_visit`。标准库的 `generic_visit` 会递归访问子节点，而 **pyasc 把 `generic_visit` 改成了直接报错**：

[python/asc/codegen/function_visitor.py:290-291](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L290-L291)

```python
def generic_visit(self, node: ast.AST) -> NoReturn:
    self.raise_unsupported(node, f"{node.__class__.__name__} syntax is not supported in JIT function")
```

这一行就是整份「不支持清单」的引擎。任何没有对应 `visit_*` 方法的 AST 节点——`Import`、`Lambda`、`Dict`、`ListComp`、`Try`、`Yield`、`Global`、`Raise`、`Break`、`Continue`……——重放到它时都会在这里收到 `UnsupportedSyntaxError`，消息形如 `Import syntax is not supported in JIT function`（节点类名填入模板）。

这个设计有两层含义：

1. **默认拒绝（default-deny）**：新增一种不支持语法不需要改任何代码——只要不为它写 `visit_*` 方法即可。这与安全系统的白名单思想一致：宁可误杀（不支持某语法），不可漏放（把无法翻译成 IR 的语义静默放过）。
2. **错误必有定位**：因为报错发生在「重放到该节点」的瞬间，异常天然携带该节点的 `lineno` / `col_offset`（详见 4.3）。

除了 `generic_visit` 这个统一落点，还有第二类限制：**已实现的 `visit_*` 方法内部的显式检查**。比如 `for ... else` 的 `else` 分支、链式比较、多赋值目标——这些节点本身有 `visit_*` 方法（`ast.For`、`ast.Compare`、`ast.Assign` 都被支持），但方法体内对特定形态做了进一步拒绝。第三类限制发生在更早的**装饰期**：`@asc.jit` 修饰 `async def` 或类时，`Function.get_function_node` 直接抛 `TypeError`，连调用都不用发生。

#### 4.1.2 核心流程

一个 kernel 从装饰到触发语法报错的完整时间线：

```text
装饰期（import / def 定义时）
└── Function.__init__
    ├── inspect.getsourcelines(fn)      → self.src（含装饰器行的原始源码）
    ├── get_function_node(fn)           → ast.parse + 校验必须是单个 FunctionDef
    │     └── 不是 FunctionDef（如 AsyncFunctionDef/ClassDef）→ TypeError（装饰期即报错）
    └── 抓取完成，函数体尚未被访问

调用期（kernel[核数, 流](...) 触发）
└── JITFunction._run
    ├── 参数层：get_arg_type(每个实参)
    │     └── str/list/dict 等无法映射 → 裸 TypeError（无源码摘录，见 4.3）
    └── 语法层：_run_codegen → visitor.visit(FunctionDef)
          └── 逐节点重放
                ├── 节点有 visit_Xxx 方法 → 翻译成 IR
                │     └── 方法内部还可能显式拒绝特定形态（for-else、链式比较…）
                └── 节点没有 visit_Xxx 方法 → generic_visit
                      └── UnsupportedSyntaxError: "Xxx syntax is not supported in JIT function"
```

三类限制对照：

| 类别 | 检查位置 | 触发时机 | 报错类型 |
| --- | --- | --- | --- |
| 装饰期类型检查 | `function.py` `get_function_node` | `@asc.jit` 定义时 | `TypeError` |
| 方法内显式限制 | 各 `visit_*` 方法体 | 调用后重放到该节点 | `UnsupportedSyntaxError` |
| 未实现节点兜底 | `generic_visit` | 调用后重放到该节点 | `UnsupportedSyntaxError` |

#### 4.1.3 源码精读

**已实现的 `visit_*` 方法清单**（直接数一遍 [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py) 中的 `def visit_` 即可复现）：

| 语句类 | 表达式类 | 辅助节点 |
| --- | --- | --- |
| `Assign`、`AnnAssign`、`AugAssign` | `BinOp`、`UnaryOp`、`BoolOp`、`Compare` | `arguments`、`arg` |
| `If`、`IfExp`、`For`、`While` | `Call`、`Attribute`、`Subscript`、`Slice` | `keyword` |
| `Return`、`Pass`、`Assert`、`With` | `Constant`、`Name`、`List`、`Tuple` | `FunctionDef`（入口） |
| `Expr`（表达式语句） | `JoinedStr`、`FormattedValue`（f-string，编译期求值） | |

对照 [docs/python_syntax_support.md:3-267](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L3-L267) 的「支持的语法接口列表」——属性访问、while/for 循环、二元/一元运算符、tuple、pass、list、条件表达式、if、常量、比较、函数调用、布尔逻辑、增强赋值、赋值、带注解赋值、格式化占位符与 assert、下标、名称引用、切片——每一条都能在上表找到对应的 `visit_*` 方法。**文档与源码是一一对应的**。

**方法内显式限制的四个代表**：

1. `for` / `while` 不允许带 `else` 分支——`scf.for` / `scf.while` 没有承载 else 语义的地方：[python/asc/codegen/function_visitor.py:479-481](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L479-L481)（`visit_For`，`visit_While` 在 [692-694](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L692-L694) 同样检查）。

```python
def visit_For(self, node: ast.For) -> None:
    if len(node.orelse) != 0:
        self.raise_unsupported(node, "else statement is not allowed after for-loop")
```

2. 链式比较 `a < b < c` 被拒绝——IR 层一次只产生一个 `arith.cmpi`：[python/asc/codegen/function_visitor.py:448-450](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L448-L450)。同理，三目链式布尔 `a and b and c` 也被要求加括号分组（[430-432](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L430-L432)），因为 `PlainValue` 的 `logical_and` 只定义了双目。

3. 函数形参不允许默认值、仅位置参数 `/`、仅关键字参数 `*`——参数要一一映射到 kernel 的 ABI，无法表达「缺省」：[python/asc/codegen/function_visitor.py:314-320](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L314-L320)。

4. 赋值目标必须是名字或名字元组——`a = b = c` 链式赋值与 `arr[i], x = ...` 形态被拒绝：[python/asc/codegen/function_visitor.py:359-363](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L359-L363)。

**装饰期检查**：`@asc.jit` 修饰的必须是普通函数定义。`async def` 解析出 `AsyncFunctionDef`、类定义解析出 `ClassDef` 时，装饰那一刻就抛 `TypeError`：[python/asc/codegen/function.py:89-100](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L89-L100)

```python
@staticmethod
def get_function_node(fn: Callable) -> ast.FunctionDef:
    source = inspect.getsource(fn)
    ...
    def_node = node.body[0]
    if not isinstance(def_node, ast.FunctionDef):
        raise TypeError(f"JIT compilation is applicable to functions only, got {def_node.__class__.__name__}")
    return def_node
```

这解释了文档「不支持」清单里的 `async def` 条目（[docs/python_syntax_support.md:457-466](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L457-L466)）为什么报错形态和其他条目不同——它在 import 阶段就炸，报错栈里没有 `visit`。

**嵌套函数的双重防线**：`visit` 方法在分发前专门检查「已在函数内又遇到 `FunctionDef`」：[python/asc/codegen/function_visitor.py:298-299](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L298-L299)

```python
if self.state.inside_function and isinstance(node, ast.FunctionDef):
    self.raise_unsupported(node, "Nested functions are not supported")
```

注意 Device 子函数（u4-l4）不受影响：子函数是**模块级**定义、被 `call_jit_function` 递归 `visit(fn.node)` 的，每次递归创建的新 visitor 从函数定义重新开始（`inside_function` 初值 `False`），所以 02_add_framework 的 `copy_in/compute/copy_out` 是合法的——被禁止的是「函数体里面再写 `def`」。

**kernel 的 return 限制**：[python/asc/codegen/function_visitor.py:644-652](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L644-L652)

```python
def visit_Return(self, node: ast.Return) -> None:
    if not self.state.return_allowed:
        self.raise_unsupported(node, "Return statement is not allowed in nested blocks")
    value = self.visit(node.value)
    self.state.discard_everything = True
    if value is None:
        return
    if self.is_kernel:
        self.raise_unsupported(node, "JIT kernel function cannot return objects")
```

两条规则对应文档 [docs/python_syntax_support.md:308-337](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L308-L337) 的「return 语句」条目：return 只能作为顶层语句（嵌套在 if/for 里会被 `return_allowed` 状态拦截，u4-l3 讲过 `visit_region` 会把它置 `False`）；且 kernel 不能返回对象，只有 Device 侧执行函数可以。

#### 4.1.4 代码实践

**实践目标**：不依赖 NPU，用纯 Python 的 `ast` 模块验证「支持清单 = `visit_*` 方法表」，学会预测一段 kernel 会不会编译通过。

**操作步骤**（以下脚本为示例代码，仅需标准库）：

```python
# syntax_probe.py —— 预测 kernel 中每个 AST 节点是否被 FunctionVisitor 支持
import ast

SOURCE = '''\
def kernel(x, n):
    import math
    acc = 0
    for i in range(n):
        acc += i
    squared = [i * i for i in range(n)]
    if acc > 10:
        return acc
    print(acc)
'''

# 用正则从 function_visitor.py 中自动提取全部 visit_* 方法名（在仓库根目录运行）
import re
fv = open("python/asc/codegen/function_visitor.py", encoding="utf-8").read()
IMPLEMENTED = {name[len("visit_"):] for name in re.findall(r"def (visit_\w+)\(", fv)}

tree = ast.parse(SOURCE)
for node in ast.walk(tree):
    kind = type(node).__name__
    if kind.startswith("ctx") or kind in ("Load", "Store", "Del"):
        continue
    verdict = "OK" if kind in IMPLEMENTED else "REJECT (generic_visit)"
    print(f"{kind:20s} {verdict}")
```

**需要观察的现象**：输出里 `Import`、`ListComp`、`Return`（带值的 return 节点本身是支持的，是否报错取决于 `is_kernel` 与嵌套深度）各自的判定；`For`、`BinOp`、`Constant` 应为 OK。

**预期结果**（依据 4.1.3 的方法表推断）：

- `Import` → REJECT：无 `visit_Import` 方法；
- `ListComp` → REJECT：无 `visit_ListComp` 方法；
- `For` / `Name` / `BinOp` / `Constant` → OK；
- 注意 `print` 这种「名字」节点 `Name` 本身是 OK 的——它最终死在 NameScope 查找（4.2），不在本层。这说明**节点级白名单与名字级白名单是两道独立的闸门**。

若想进一步实测（需按 u1-l2 安装好 pyasc），把 `SOURCE` 变成一个真正的 `@asc.jit` 函数并调用，逐条注释掉非法行验证预测。实测部分待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`break`、`continue`、`global`、`nonlocal`、`lambda` 分别会因为哪条机制报错？报错消息长什么样？

**答案**：四者都没有对应的 `visit_*` 方法，全部落进 `generic_visit`（[function_visitor.py:290-291](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L290-L291)），消息分别为 `Break syntax is not supported in JIT function`、`Continue syntax is not supported in JIT function`、`Global syntax is not supported in JIT function`、`Nonlocal syntax is not supported in JIT function`、`Lambda syntax is not supported in JIT function`——节点类名填入同一模板。它们在文档不支持清单中对应 [docs/python_syntax_support.md:339-349](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L339-L349)、[423-435](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L423-L435)、[295-306](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L295-L306)、[498-512](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L498-L512)、[412-421](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L412-L421)。

**练习 2**：`x = a if a > 0 else 0` 与 `x = [a, b]` 都支持，但 `x = {a: b}` 不支持。请从「IR 侧有没有对应结构」的角度解释原因。

**答案**：条件表达式有 `visit_IfExp`（生成带 else 的 `scf.if`），list 与 tuple 有 `visit_List` / `visit_Tuple`（编译期 Python 容器，用于多返回值等场景）；而 dict 既没有 `visit_Dict` 方法（落到 `generic_visit` 报 `Dict syntax is not supported in JIT function`），IR 侧也没有「关联容器」对应物——设备侧代码的值要么是定宽标量、要么是 tensor/指针，没有哈希表语义。Host 侧把 dict 当实参传入同样不行（见 4.3.1 的参数层报错）。

**练习 3**：为什么 02_add_framework 里的 `compute` 函数能定义在 kernel 外被调用，而文档却说「嵌套函数不支持」？

**答案**：被禁止的是**语法上的嵌套**——在函数体字面里再写 `def`，由 `visit` 的 `inside_function` 检查拦截（[function_visitor.py:298-299](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L298-L299)）。模块级的 `@asc.jit` 子函数通过 `call_jit_function` 内联（u4-l4），每个子函数用独立的 visitor 实例递归访问，`inside_function` 从 `False` 起步，不触发该检查。

### 4.2 内置函数白名单：NameScope 的第三级

#### 4.2.1 概念说明

u4-l2 讲过 `NameScope` 按 local → global → builtins 三级查找名字。前两级是动态的（局部变量表、模块全局变量加 ConstExpr 实参），第三级 builtins 是**硬编码的白名单**——它不是 Python 内置作用域的完整拷贝，而是精心挑选的一小撮：

[python/asc/codegen/name_scope.py:13-29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L13-L29)

```python
class NameScope:
    builtins: Dict[str, Any] = {
        b.__name__: b
        for b in (
            dict, float, int, isinstance, issubclass, len, list,
            range, repr, str, tuple, type,
        )
    }
```

一共 12 个名字，可分三组理解：

| 组 | 名字 | 在编译期的用途 |
| --- | --- | --- |
| 类型/构造 | `dict` `float` `int` `list` `str` `tuple` | 类型标注（`int`）、编译期构造与转换（`float(c)`） |
| 反射 | `isinstance` `issubclass` `type` `repr` | 对编译期 Python 值做类型判断与打印（配合 f-string 的 assert 消息） |
| 计算 | `len` `range` | `len(nums)` 编译期求长度；`range` 供 `visit_For` 识别循环 |

关键认知：**白名单里的函数在编译期按普通 Python 语义立即执行**。`len(nums)` 中的 `nums` 若是编译期 list，`len` 就是 Python 内置 `len`，重放时直接算出数字。它们不会翻译成任何 IR。

反过来说：**不在名单里的 Python 内置名字一律查不到**。`print`、`open`、`input`、`abs`、`min`、`max`、`sum`、`enumerate`、`zip`……都不在。这不是疏忽，而是语义约束：

- `print` / `open` / `input` 是 **Host 侧 I/O**——kernel 在设备上执行时没有 stdout、没有文件系统、没有键盘，这些调用没有合法的翻译目标；
- `min` / `max` / `abs` 等数值聚合在设备侧有对应的 Ascend C API（高阶 API 或向量算子），应使用 `asc.` 前缀的版本，让前端生成正确的 IR，而不是编译期偷偷用 Python 算掉。

#### 4.2.2 核心流程

一个名字在 kernel 里被引用时的完整解析流程：

```text
visit_Name(node)                      # node.ctx 为 Load
└── dereference_name(node.id)
    └── NameScope.lookup(name)
        ├── ① local_vars（含 ConstExpr 实参，已并入 global_vars 构造参数）
        │      命中 → 返回值（可能是 PlainValue / Tensor / Python 立即数）
        ├── ② global_vars（模块级名字：asc、torch、math、其他 @asc.jit 函数……）
        │      命中 → 返回对象（JITFunction 会触发子函数内联，见 u4-l4）
        ├── ③ builtins 白名单（12 个名字）
        │      命中 → 返回 Python 内置对象，编译期直接执行
        └── 三级都 miss
               └── raise NameError(f"{name} is not defined")
                     ↓ 被外层 visit 的 capture_exceptions 捕获（见 4.3）
               CodegenError: "NameError: print is not defined"（带源码定位）
```

#### 4.2.3 源码精读

**查找与抛错**只有 6 行：[python/asc/codegen/name_scope.py:52-57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L52-L57)

```python
def lookup(self, name: str) -> Optional[Any]:
    for storage in self.local_vars, self.global_vars, self.builtins:
        val = storage.get(name, self.sentinel)
        if val is not self.sentinel:
            return val
    raise NameError(f"{name} is not defined")
```

注意它抛的是**裸 `NameError`**（不是 CodegenError）。真正把它包装成带定位的 `CodegenError` 的是外层 `visit` 的兜底逻辑（[function_visitor.py:305-312](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L305-L312)，4.3 详述）。`visit_Name` 与 `dereference_name` 本身不做任何特殊处理：[function_visitor.py:243-244](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L243-L244)、[635-639](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L635-L639)。

**`print` 的报错有官方测试背书**：[python/test/unit/codegen/test_function_visitor.py:263-271](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L263-L271)

```python
def test_error_test_print():

    @asc.jit
    def func_error_test_print(age):
        print(age)

    with pytest.raises(CodegenError) as e:
        func_error_test_print[1](100)
    assert "NameError: print is not defined" in str(e.value)
```

这条测试同时确认了两件事：`print` 报的是 `CodegenError`（说明 NameError 被包装了）；消息里保留了原始异常文本（说明包装时用了 `f"{e.__class__.__name__}: {e}"` 的格式）。

**`print` 的合法替代是 `asc.printf`**：[python/asc/language/basic/dump_tensor.py:44-63](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/dump_tensor.py#L44-L63)

```python
@require_jit
@set_common_docstring(api_name="printf")
def printf(desc: str, *params) -> None:
    ...
    global_builder.get_ir_builder().create_asc_PrintfOp(desc, var_ir_values)
```

与 `print` 的本质区别：`asc.printf` 是 language 层 API，`require_jit` 保证它只能在 JIT 编译期被调用（u2-l5 讲过这个守门装饰器），编译期执行的副作用是**向 IR 追加一个 `asc.PrintfOp`**——打印发生在设备侧执行时（需 debug 选项支持，详见 u7-l4），而不是在编译期。这正是「Host 语义 I/O 被拒绝、设备语义 I/O 有专门 API」的示范。

**第三方库调用的两种命运**。`import math` 写在 kernel 体内会直接被 `generic_visit` 拒绝；但把 `math` import 在**模块级**、在 kernel 里写 `math.sqrt(...)` 却能通过名字解析——`math` 在 global 一级被查到，`visit_Attribute` 用 `getattr` 取回 `math.sqrt`，随后 `visit_Call` 走「编译期直接执行」分支（u4-l4 的 `fn(*args, **kwargs)`）。此时结果取决于实参性质：

- 实参是**编译期值**（字面量、ConstExpr 值）：`math.sqrt(2)` 在编译期真算出 `1.414...`，以常量进入 IR——合法且有用；
- 实参是**运行时值**（`PlainValue` 等 IR 值）：`math.sqrt` 收到一个它不认识的 Python 对象，抛 `TypeError`，再被外层 `visit` 包装成 `CodegenError`——因为 CPython 的 `math.sqrt` 无法向 IR 里「发射」一次设备侧开方，设备侧开方应该用 `asc` 的对应算子 API。

所以准确的说法不是「第三方库一律禁用」，而是「**第三方函数只能服务于编译期计算，不能作用在运行时值上**」。这也是文档把 `import` 列入不支持清单（[docs/python_syntax_support.md:437-455](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L437-L455)）时只禁「kernel 体内 import」的原因——模块级 import 出来的对象能不能用，取决于你在编译期用它做什么。

#### 4.2.4 代码实践

**实践目标**：亲手验证白名单内外的分界，以及第三方函数在两类值上的不同命运。

**操作步骤**（需按 u1-l2 安装 pyasc；以下为示例代码，保存为 `builtin_probe.py`）：

```python
import math
import asc
import asc.runtime.config as config

config.set_platform(config.Backend.Model)  # 仿真模式即可，本实践只到 codegen 层

# 实验 A：白名单内的 len —— 编译期求值，应能通过
@asc.jit
def k_len(n: int):
    nums = [1, 2, 3]
    total = len(nums) + n   # len(nums) 编译期算出 3，与运行时参数 n 相加生成 IR

# 实验 B：白名单外的 print —— NameError 被包装为 CodegenError
@asc.jit
def k_print(n: int):
    print(n)

# 实验 C：第三方函数作用在编译期常量上 —— 编译期算出
@asc.jit
def k_math_const(n: int):
    root = math.sqrt(4) + n   # math.sqrt(4) 编译期 = 2.0

# 实验 D：第三方函数作用在运行时值上 —— 预期 TypeError 被包装
@asc.jit
def k_math_runtime(n: int):
    root = math.sqrt(n)       # n 是 PlainValue，math.sqrt 无法接受

for name, fn, arg in [("A: len", k_len, 1), ("B: print", k_print, 1),
                      ("C: math.sqrt(const)", k_math_const, 1), ("D: math.sqrt(runtime)", k_math_runtime, 4)]:
    try:
        fn[1](arg)
        print(f"[{name}] 编译通过")
    except Exception as e:
        print(f"[{name}] {type(e).__name__}: {str(e).splitlines()[-1]}")
```

**需要观察的现象**：四个实验各自的成败与报错的最后一行文本；特别对比 C 与 D——同一个 `math.sqrt`，一个常量一个运行时值，命运不同。

**预期结果**（依据 4.2.3 源码推断）：

- A 编译通过（`len` 在白名单，编译期返回 3）；
- B 抛 `CodegenError`，末行为 `NameError: print is not defined`（有单测背书）；
- C 编译通过（`math.sqrt(4)` 编译期求值为 2.0）；
- D 抛 `CodegenError`，末行为 `TypeError: ...`（`math.sqrt` 不接受 `PlainValue`）。

以上实测输出待本地验证。若 D 的具体错误文本与推断不符，请以实际输出为准并回读 [function_visitor.py:309-311](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L309-L311) 的包装格式核对。

#### 4.2.5 小练习与答案

**练习 1**：kernel 里写 `nums = [x, y, z]; n = len(nums)`，为什么 `len` 可用而 `sum(nums)` 不行？

**答案**：`len` 在 `NameScope.builtins` 白名单里（[name_scope.py:16-28](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L16-L28)），重放时按 Python 语义在编译期对 list 求长度；`sum` 不在名单里，三级查找全部 miss，抛 `NameError: sum is not defined`，被包装成 `CodegenError`。设计上，对 tensor 的求和应该用向量规约类 API（language/basic 下的 `vec_reduce` 系列，见 u5-l6 的实践），让它生成设备侧 IR 而不是 Host 侧算掉。

**练习 2**：`with open('file.txt') as f:` 为什么在文档里列为不支持（[docs/python_syntax_support.md:361-370](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L361-L370)）？报错发生在哪一层？

**答案**：两层都可能出问题，但先出问题的是**名字层**：`with` 语句本身其实有受限支持的 `visit_With`（[function_visitor.py:680-690](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L680-L690)，仅支持单项），重放进入 `open(...)` 调用时，`open` 不在白名单，`lookup` 抛 `NameError: open is not defined`，被包装为 `CodegenError`。根本原因与 `print` 相同：设备侧没有文件系统，Host I/O 没有合法的翻译目标。

**练习 3**：`range` 明明是 Python 内置函数，为什么 `visit_For` 还要专门写 `elif func is range or func is _range`（[function_visitor.py:490](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L490)）？

**答案**：pyasc 有两个「range」：Python 内置 `range`（经白名单解析回来）与 `asc.language.core.range`（导入为 `_range`，u4-l3 讲过它与 `static_range` 的区别）。`visit_For` 需要区分三种迭代器：`static_range`（编译期完全展开）、内置 `range`（生成 `scf.for`）、`asc` 的 `range`（同样生成 `scf.for`），其余迭代对象一律拒绝（[function_visitor.py:493-497](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L493-L497)：`Only for-loops with range or asc.language.range or asc.language.static_range are supported`）。所以对 `range` 的识别不是走通用调用路径，而是在 `visit_For` 里用 `is` 提前截获（u4-l4 也提到过这一点）。

### 4.3 CodegenError：报错如何携带源码位置

#### 4.3.1 概念说明

[python/asc/codegen/errors.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/errors.py) 只定义了两个类：

- `CodegenError`：语法生成阶段的错误基类，携带「AST 节点 + 源码行列表 + 原始消息」三件套，`format_message` 把它们渲染成带定位的友好报错；
- `UnsupportedSyntaxError(CodegenError)`：子类，目前**没有添加任何新逻辑**（[errors.py:48-49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/errors.py#L48-L49) 只有一句 `pass`）——它的价值是**语义分类**：调用方可以用 `except UnsupportedSyntaxError` 单独捕「语法不支持」，与「其他生成错误」区分开。

一个 `CodegenError` 有三条产生路径：

1. **显式拒绝**：`raise_unsupported`（[function_visitor.py:158-162](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L158-L162)）——各 `visit_*` 方法发现自己处理不了的形态，主动抛 `UnsupportedSyntaxError`，消息是人写的（如 `Loop bounds must have the same DataType`）；
2. **兜底拒绝**：`generic_visit`（4.1）——同样抛 `UnsupportedSyntaxError`，消息由节点类名模板生成；
3. **异常包装**：`visit` 的 try/except（[function_visitor.py:305-312](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L305-L312)）——重放过程中冒出的**任何**非 CodegenError 异常（NameError、language 层 API 的 TypeError、除零……）被包成 `CodegenError`，消息为 `f"{异常类名}: {原始消息}"`。受 `CodegenOptions.capture_exceptions`（默认 `True`，[function_visitor.py:36-39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L36-L39)）开关控制，关掉后裸异常直接穿出，方便调试 pyasc 自身。

还要加上一层**不经过 CodegenError 的报错**：参数层。`get_arg_type` 对 `str`/`list`/`dict` 等实参抛裸 `TypeError`（[python/asc/runtime/jit.py:66-94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L66-L94)），它发生在 `_cache_kernel`（[jit.py:156-158](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L156-L158)），**早于** `_run_codegen`——所以这类报错没有源码摘录。看到一个不含 `at <source>:...` 的报错，就说明错误发生在语法重放之前（或之后）的某个环节。

最后是 `assert` / `static_assert`：`visit_Assert` 把 kernel 里的 `assert cond, msg` 变成**编译期检查**——条件必须是编译期可判定为 bool 的值，否则明确拒绝；条件为假则抛带消息的 `AssertionError`（再由外层包装成 CodegenError）。它是「把配置校验前置到编译期」的正道工具。

#### 4.3.2 核心流程

`format_message` 的组装算法（[errors.py:26-45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/errors.py#L26-L45)）：

```text
输入：node（AST 节点，含 lineno/col_offset）、src（源码行列表）、error_message
├── src 为 None → 输出 " <source unavailable>"（装饰期抓不到源码时的兜底）
├── 头部：at <source>:{lineno}:{col_offset}:
├── 摘录：src[lineno-4 : lineno]        # 出错行之前 3 行上下文 + 1 行
├── 指示：' ' * col_offset + '^'        # ^ 打在摘录最后一行的 col_offset 列下
├── 摘录：src[lineno : lineno+3]        # 出错行之后 3 行上下文
└── 尾部：error_message                 # 人写消息或 "{异常类名}: {原始消息}"
```

读报错时需要知道的一个坑：**两个行号基准不一致**。

- `node.lineno` 来自的 AST 是从「剥掉装饰器、`def` 行起算」的源码解析的（[function.py:47-48](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L47-L48)、[137-145](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L137-L145)）——`def` 行是第 1 行；
- 摘录切自的 `self.src` 是 `inspect.getsourcelines` 抓的**原始行列表，含装饰器行**（[function.py:43-44](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L43-L44)）。

两者相差装饰器占的行数。因此 `^` 的对齐与头部行号到真实文件的换算，建议在实践 4.3.4 中实测确认，不要想当然。而写进 IR 定位信息的行号用的是另一套换算——`visit` 在每个节点前调用 `set_loc(filename, line_offset + node.lineno, col_offset)`（[function_visitor.py:302-304](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L302-L304)），其中 `line_offset` 来自 `fn.__code__.co_firstlineno`（[function.py:102-105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L102-L105)），即函数首行在真实文件中的行号——这才是「报错行号 → 真实文件行号」的官方换算通道。

#### 4.3.3 源码精读

**异常类的完整定义**：[python/asc/codegen/errors.py:13-45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/errors.py#L13-L45)

```python
class CodegenError(Exception):
    context_lines = 3

    def __init__(self, node: ast.AST, src: Optional[List[str]] = None, message: Optional[str] = None):
        super().__init__()
        self.src = src
        self.node = node
        self.error_message = message
        self.message = self.format_message()
```

三个字段值得注意：`node`（AST 节点本体，测试与上层可以继续取 `lineno`）、`src`（源码行列表）、`error_message`（原始消息）。`context_lines = 3` 是类属性——想调整摘录窗口大小，改这一个常量即可。

**包装逻辑**（本讲的枢纽代码）：[python/asc/codegen/function_visitor.py:293-312](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L293-L312)

```python
def visit(self, node: Optional[ast.AST]) -> Optional[Any]:
    ...
    try:
        return super().visit(node)
    except CodegenError:
        raise
    except Exception as e:
        if self.options.capture_exceptions:
            raise CodegenError(node, self.src, f"{e.__class__.__name__}: {e}") from e
        raise
```

三个细节：已有的 `CodegenError` 原样放行（避免双重包装）；包装用 `from e` 保留原始异常链（`__cause__`），traceback 里能看到根因；`capture_exceptions=False` 时直接 `raise` 裸异常——这是给 pyasc 开发者调试用的开关，通过 `@asc.jit(capture_exceptions=False)` 或调用时传参即可生效（`CodegenOptions` 是选项袋之一，u1-l5/u3-l1）。

**参数层的裸 TypeError**：[python/asc/runtime/jit.py:66-94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L66-L94) 的 `get_arg_type` 逐类型匹配（bool→int8、int→int32、float→float32、ndarray/torch.Tensor→指针、Struct→结构体），全不命中时：

[python/asc/runtime/jit.py:94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L94)

```python
raise TypeError(f"Argument type in JIT function is not supported: {value.__class__.__name__}")
```

官方测试（[python/test/unit/codegen/test_function_visitor.py:252-260](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L252-L260)）验证了 str 实参的报错：`Argument type in JIT function is not supported: str`——注意断言用的是 `pytest.raises(TypeError)` 而非 `CodegenError`，佐证了「参数层不经过 CodegenError」。

**编译期断言**：[python/asc/codegen/function_visitor.py:349-357](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L349-L357)

```python
def visit_Assert(self, node: ast.Assert) -> None:
    test = self.visit(node.test)
    try:
        test = ConstExpr(test)
        static_assert(test, self.visit(node.msg))
    except TypeError:
        self.raise_unsupported(
            node,
            f"An assertion turned out to test a runtime value {test!r}, only compile-time values are supported")
```

底层实现：[python/asc/language/core/utils.py:210-218](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L210-L218)

```python
def static_assert(test: bool, message: Optional[str] = None) -> None:
    test = ConstExpr.unwrap(test)
    if not isinstance(test, bool):
        raise TypeError(f"Assertion condition could not be determined, expected bool, got {test.__class__.__name__}")
    if not test:
        if message is not None:
            raise AssertionError(message)
        else:
            raise ValueError("miss message")
```

三个要点：

1. 条件会被 `ConstExpr.unwrap` 解包——`assert TILE_NUM > 0, "..."` 这类**编译期常量**检查合法；条件若是运行时值（`PlainValue`），`isinstance(test, bool)` 不成立抛 `TypeError`，被 `visit_Assert` 捕获后转成明确的 `UnsupportedSyntaxError`（`only compile-time values are supported`）——设备侧没有断言异常机制，运行时校验必须自己写 if + 处理逻辑；
2. 条件为假时抛 `AssertionError(message)`，再由外层 `visit` 包装成 `CodegenError`——官方测试（[test_function_visitor.py:241-249](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L241-L249)）断言 `"AssertionError" in str(e.value)`，用的正是文档「格式化占位符和assert语句」条目（[docs/python_syntax_support.md:225-233](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L225-L233)）里的 `assert 1 < 0, f"assert failed {num}"` 例子——f-string 在编译期求值（`visit_JoinedStr` / `visit_FormattedValue`，[function_visitor.py:518-527](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L518-L527)、[625-627](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L625-L627)），所以消息里的 `{num}` 会被填成实参的编译期可见值；
3. `message` 为 `None` 且条件为假时抛的是 `ValueError("miss message")`——写 `assert cond` 不带消息、且 cond 为假，会得到这个略显意外的错误。**养成总是给 assert 配消息的习惯**。

#### 4.3.4 代码实践

**实践目标**：并排观察「语法层 CodegenError」与「参数层 TypeError」的输出差异，并实测 `^` 指示与行号换算。

**操作步骤**（示例代码，需已安装 pyasc）：

```python
import asc
import asc.runtime.config as config

config.set_platform(config.Backend.Model)

@asc.jit
def k_syntax(n: int):
    acc = n + 1
    print(acc)          # 语法层错误：NameError 被包装为 CodegenError

@asc.jit
def k_arg(d):
    pass                # 函数体没问题，问题在实参类型

print("=" * 20, "语法层：CodegenError", "=" * 20)
try:
    k_syntax[1](1)
except Exception as e:
    print(type(e).__name__)
    print(e)            # 完整打印，观察 at <source>:行:列 / 摘录 / ^ / 消息

print("=" * 20, "参数层：TypeError", "=" * 20)
try:
    k_arg[1]({"k": 1})
except Exception as e:
    print(type(e).__name__)
    print(e)            # 应只有一行消息，无源码摘录
```

**需要观察的现象**：

1. 第一个报错是否包含 `at <source>:...` 头、上下文摘录、`^` 指示与 `NameError: print is not defined` 消息（`print` 不在白名单，异常发生在解析 `print` 这个名字时，而非调用它时）；
2. `^` 实际打在源码的哪一行——与报错头部的行号差几行？与 `print` 真实所在行差几行？（对照 4.3.2 的「两个基准」分析）
3. 第二个报错是否只有一行 `Argument type in JIT function is not supported: dict`，且异常类型是 `TypeError` 而非 `CodegenError`。

**预期结果**：语法层报 `CodegenError`（内嵌 NameError 文本，带源码摘录）；参数层报裸 `TypeError`（无摘录）。`^` 相对真实出错行的偏移量依装饰器行数而定，请以实测为准（待本地验证）——这正是 4.3.2 指出的两个行号基准问题。

#### 4.3.5 小练习与答案

**练习 1**：用户报来一个错误，输出里没有 `at <source>:行:列` 也没有源码摘录，只有一行 `Argument type in JIT function is not supported: list`。错误发生在哪个阶段？给出排查方向。

**答案**：发生在**参数层**——`JITFunction.get_arg_type`（[jit.py:94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L94)）在 `_cache_kernel` 里、`_run_codegen` 之前执行，抛的是裸 `TypeError`，不经过 `CodegenError.format_message`，所以没有源码定位。排查方向：检查 `kernel[核数, 流](...)` 的**实参**里有没有 list（本例）、str、dict 等无法映射的类型；合法做法是把列表拆成多个标量参数、改用 `ConstExpr` 传编译期常量、或用 tensor/Struct 承载数据。

**练习 2**：`assert n > 0, "n must be positive"`（`n` 是运行时 int 参数）会发生什么？想要等效的编译期检查怎么写？

**答案**：`n` 是运行时参数，`visit(node.test)` 得到 `PlainValue`；`static_assert` 里 `isinstance(test, bool)` 不成立抛 `TypeError`，被 `visit_Assert` 转成 `UnsupportedSyntaxError`，消息为 `An assertion turned out to test a runtime value ..., only compile-time values are supported`（[function_visitor.py:355-357](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L355-L357)）。若这个「必须大于 0」是编译期已知的约束，把参数改成 `n: asc.ConstExpr[int]` 再 assert（ConstExpr 值在编译期可判定，u2-l1/u3-l3）；若它依赖运行时数据，assert 无法胜任，需在 kernel 里写 `if n <= 0: ...` 的设备侧处理，或在 Host 侧 launch 之前校验。

**练习 3**：`@asc.jit(capture_exceptions=False)` 有什么用？普通算子开发者什么时候需要它？

**答案**：`capture_exceptions` 是 `CodegenOptions` 的字段（默认 `True`，[function_visitor.py:36-39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L36-L39)），控制 `visit` 的异常包装（[309-312](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L309-L312)）。置 `False` 后，language 层 API 内部的原始异常不再被包成 `CodegenError`，而是带着完整原始 traceback 直接穿出。普通开发者一般不需要；当你怀疑报错信息被包装层「吃掉」了根因（比如想看 `math.sqrt` 到底在哪一行抛的 TypeError），或为 pyasc 本身做贡献、调试 codegen 时，它是更直接的观察窗口。

## 5. 综合实践

把本讲三个模块串成一个「**报错 → 定位 → 改写**」排查手册的制作任务。这正是讲义规格里要求的实践：编写 5 个分别触发不同不支持语法/类型的 kernel，收集每个报错的输出格式，总结成排查条目。

**任务**：写一个脚本 `syntax_errors_tour.py`（示例代码），一次性触发 5 类典型报错，逐个记录「报错类型 / 消息 / 抛出位置 / 合法改写」。

```python
"""syntax_errors_tour.py —— pyasc 语法边界巡礼：5 类报错一次看全"""
import asc
import asc.runtime.config as config

config.set_platform(config.Backend.Model)  # 语法错误都在 codegen 层抛出，无需 NPU

def run_case(title, case):
    print("=" * 25, title, "=" * 25)
    try:
        kernel, args = case()
        kernel[1](*args)
        print("（未报错——与预期不符，请回读源码核对）")
    except Exception as e:
        print(f"异常类型: {type(e).__name__}")
        print(f"--- 报错全文 ---\n{e}\n----------------")

# 每个案例是一个工厂函数：返回（待调用的 @asc.jit 函数, 实参元组）
def case_import():
    @asc.jit
    def k(x):
        import math          # generic_visit: "Import syntax is not supported in JIT function"
    return k, (1,)

def case_print():
    @asc.jit
    def k(x):
        print(x)             # 白名单外: "NameError: print is not defined"
    return k, (1,)

def case_dict_arg():
    @asc.jit
    def k(d):
        pass                 # 函数体合法；dict 在参数层被拒
    return k, ({"a": 1},)    # get_arg_type: TypeError, 无源码摘录

def case_kernel_return():
    @asc.jit
    def k(x):
        return x * 2         # visit_Return: "JIT kernel function cannot return objects"
    return k, (1,)

def case_lambda():
    @asc.jit
    def k(x):
        f = lambda a: a * a  # generic_visit: "Lambda syntax is not supported in JIT function"
    return k, (4,)

for title, case in [("import 体内导入", case_import), ("print 未白名单", case_print),
                    ("dict 实参", case_dict_arg), ("kernel return", case_kernel_return),
                    ("lambda", case_lambda)]:
    run_case(title, case)
```

**执行**：`python3 syntax_errors_tour.py`。注意脚本能在**一个进程里连续跑完**——`_run_codegen` 用 try/finally 保证 `global_builder.teardown()` 必然执行（[jit.py:184-194](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L184-L194)），一次报错不会污染后续案例。

**预期结果**（依据本讲源码精读推断；实测待本地验证）：

| # | 触发写法 | 异常类型 | 报错来源（可点击核对） | 合法改写 |
| --- | --- | --- | --- | --- |
| 1 | kernel 体内 `import math` | `UnsupportedSyntaxError` | [function_visitor.py:290-291](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L290-L291) `generic_visit` | import 移到模块级，且只对编译期值使用；数值计算用 `asc` 算子 API |
| 2 | `print(x)` | `CodegenError`（内含 NameError） | [name_scope.py:52-57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L52-L57) `lookup` 三级 miss，经 [function_visitor.py:309-311](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L309-L311) 包装 | 设备侧打印用 `asc.printf("x=%d\n", x)`（[dump_tensor.py:44-63](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/dump_tensor.py#L44-L63)，生成 `asc.PrintfOp`）；Host 侧观察则在 launch 前后打印 |
| 3 | 实参传 `dict` | `TypeError`（裸异常，无摘录） | [jit.py:94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L94) `get_arg_type` | 拆成多个标量/ConstExpr 参数；或打包成 tensor / `Struct`（u3-l3） |
| 4 | kernel 内 `return x * 2` | `UnsupportedSyntaxError` | [function_visitor.py:651-652](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L651-L652) `visit_Return` | Kernel 不返回值，结果写回输出 tensor（01_add 的做法）；需要返回值的逻辑放进 Device 子函数（[docs/python_syntax_support.md:323-335](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L323-L335) 的支持写法） |
| 5 | `f = lambda a: a * a` | `UnsupportedSyntaxError` | [function_visitor.py:290-291](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L290-L291) `generic_visit` | 改写为模块级 `@asc.jit` Device 子函数，通过 `call_jit_function` 内联（u4-l4） |

**收尾动作**：

1. 核对每个案例报错全文里的 `at <source>:行:列`、上下文摘录与 `^`，验证 4.3.2 的「两个行号基准」分析；
2. 对照 [docs/python_syntax_support.md:269-580](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L269-L580) 的不支持清单，确认 5 个案例覆盖了「未实现节点（1、5）」「白名单外名字（2）」「参数层类型（3）」「方法内显式限制（4）」四种机制——加上装饰期 `TypeError`（`@asc.jit async def`，读者可自行补充第 6 案例）即为全部五道闸门；
3. 把表格保存下来，作为自己团队写 kernel 时的速查手册；遇到新报错时，先判断它属于哪一层（有无源码摘录是最快的判据），再按对应改写列处理。

## 6. 本讲小结

- pyasc 的语法支持边界由**默认拒绝**机制实现：`FunctionVisitor` 没实现 `visit_Xxx` 的 AST 节点统一落进 `generic_visit`，报 `Xxx syntax is not supported in JIT function`；文档 [docs/python_syntax_support.md](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md) 的清单是这个机制的**结果**，排查时应回到源码找**原因**。
- 限制共分五道闸门，报错形态各不相同：装饰期 `get_function_node` 的 `TypeError`（`async def`/类）、`generic_visit` 兜底（import/lambda/dict 字面量/break/continue…）、`visit_*` 方法内显式拒绝（for-else、链式比较、kernel return、参数默认值）、NameScope 白名单外名字（print/open → NameError）、参数层 `get_arg_type` 的裸 `TypeError`（str/list/dict 实参）。
- 内置函数白名单只有 12 个名字（[name_scope.py:13-29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L13-L29)），它们在编译期按 Python 语义立即执行；白名单外的 Host 语义 I/O 没有设备侧翻译目标，设备侧打印用 `asc.printf`。
- 第三方函数不是一刀切禁用：作用在**编译期值**上时合法（编译期求值），作用在**运行时值**（IRValue）上时因类型不匹配被包装成 `CodegenError`。
- `CodegenError` 通过「AST 节点 + 源码行列表 + 消息」渲染出 `at <source>:行:列` + 上下文 + `^` 的友好报错；注意 AST 行号（`def` 行为第 1 行）与摘录源码（含装饰器）基准不同，真实文件行号以 `set_loc` 的 `line_offset + node.lineno` 换算为准。
- `assert` 被编译为 `static_assert` 编译期检查：条件必须是编译期 bool；为假抛 `AssertionError`（务必带消息，否则得到 `ValueError: miss message`）；运行时校验不能用 assert，要写成 kernel 内的 if 或 Host 侧前置校验。

## 7. 下一步学习建议

第 4 单元（codegen 模块）到此完结。你已经完整看过「AST → ASC-IR」的翻译器：架构骨架（u4-l1）、语句与作用域（u4-l2）、控制流（u4-l3）、子函数内联（u4-l4）、语法边界与诊断（本讲）。接下来两条路：

1. **进入第 5 单元（推荐）**：`FunctionVisitor` 生成的 IR 长什么样？ASC-IR 这个 MLIR Dialect 是怎么用 TableGen 的 `.td` 文件定义出来的？从 [u5-l1 ASC Dialect 入门](./u5-l1-asc-dialect-intro.md) 开始，你会第一次打开 `include/ascir/Dialect/Asc/IR` 下的 `.td` 文件，把前端发出的 `create_asc_*` 调用与后端定义对上。
2. **巩固本讲**：把综合实践的排查手册扩充到文档不支持清单的全部条目；再阅读 [python/test/unit/codegen/test_function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py) 的其余用例，留意它如何用 `filecheck` 夹具断言生成的 IR 文本——这是从「读报错」进阶到「读测试断言」的练习，也为第 7 单元的贡献流程（u7-l6）热身。
