# Transpiler：从 Python AST 到 Tilus IR

## 1. 本讲目标

本讲深入 Tilus 编译流水线的「第一公里」——转译器（Transpiler）。读者在 [u3-l1](u3-l1-compilation-pipeline-overview.md) 已经知道 `build_program` 把一个 `Program` 经过多趟 Pass 与代码生成变成 `.so`，但那个 `Program` 从哪里来？答案就是 Transpiler：它把用户写的 `__call__` 方法体（一段普通 Python 代码）转译成一棵 Tilus IR 语句树（`Function.body`）。

学完本讲，你应当能够：

- 说清 Transpiler 如何遍历 Python AST 并生成 Tilus IR（AST 访问器机制）。
- 掌握作用域（Scope）与张量/变量的绑定规则，理解 `self`、参数、自由变量如何进入 IR。
- 理解 `self.global_view(...)` 这类指令调用最终如何变成一条 `InstStmt`，以及 `LambdaProxy` 如何把 lambda 表达式延迟到转译期执行。

## 2. 前置知识

在阅读本讲前，建议你已建立以下认知（见入门层与 [u3-l1](u3-l1-compilation-pipeline-overview.md)）：

- **Tilus Script 骨架**：一个内核是继承 `tilus.Script` 的类，`__init__` 设编译期超参，`__call__` 描述算子逻辑（见 [u1-l3](u1-l3-first-kernel-vector-add.md)）。
- **Tilus IR 的基本单位**：`Program / Function / Stmt / Instruction / Tensor`（见 [u3-l1](u3-l1-compilation-pipeline-overview.md)，细节在 [u3-l3](u3-l3-tilus-ir-program-function-stmt.md) 展开）。本讲只需知道：一条 `InstStmt` 包裹一条 `Instruction`，是 IR 语句树里的叶节点。
- **Python `ast` 模块**：Python 源码会被解析成抽象语法树（AST），每种语法结构对应一个 `ast.*` 节点类，例如 `ast.Assign`、`ast.For`、`ast.BinOp`、`ast.Call`。Transpiler 就是一个遍历这些节点的访问器（visitor）。

一个关键直觉：**Transpiler 不是「静态分析」源码，而是「边执行边记录」**。它真的会调用 `self.global_view(...)` 这样的方法，只不过这些方法的副作用不是搬数据，而是往一个语句栈里追加 `InstStmt`。这种模式可称为 *transpile-run*（转译即运行），理解这一点，本讲的很多设计就顺理成章了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/lang/transpiler/transpiler.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py) | `Transpiler` 主类：继承 `ScopedProgramBuilder` 与 `PythonAstFunctor`，实现所有 `visit_*` 方法与 `transpile`/`transpile_call` 入口。 |
| [python/tilus/lang/transpiler/builder.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/builder.py) | `Scope` 与 `ScopedProgramBuilder`：管理名字→变量/张量的作用域链。 |
| [python/tilus/lang/transpiler/common.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/common.py) | `TilusProgramError`：把转译错误映射回用户源码的行号与列号。 |
| [python/tilus/lang/instructions/base.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/base.py) | `builder_context` 与 `InstructionGroup._builder`：把指令组与当前 Transpiler 桥接起来。 |
| [python/tilus/ir/builders/stmt_builder.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/builders/stmt_builder.py) | `StmtBuilder`：`global_view`/`load_global` 等方法的真正实现，`append` 把 `Instruction` 包成 `InstStmt`。 |

辅助阅读：`examples/vector_add/vector_add.py`（最小范例）、`python/tilus/lang/instantiated_script.py`（调用 Transpiler 的上层）。

## 4. 核心概念与源码讲解

### 4.1 Transpiler 的入口：把 `__call__` 变成 `Function`

#### 4.1.1 概念说明

`Transpiler` 的职责只有一个：给定一个已实例化的 `Script` 对象（`__init__` 已跑完、超参已填好），把它 `__call__` 方法体里的 Python 代码，转译成一棵 Tilus IR 语句树，再连同参数与元数据组装成一个 `Function`。

它的类定义同时继承两个基类，这是理解它的钥匙：

```python
class Transpiler(ScopedProgramBuilder, PythonAstFunctor):
```

- `PythonAstFunctor`（来自内嵌的 hidet 子包）提供「遍历 AST 节点」的访问器骨架——一组 `visit_*` 方法。
- `ScopedProgramBuilder`（继承自 `ir.builders.StmtBuilder`）提供「收集语句」的能力——一个语句栈 `_stack`，以及作用域链。

所以 Transpiler 既是 *AST 访问器*（读），又是 *语句构造器*（写）：每访问到一个 AST 节点，就往语句栈里追加对应的 IR 语句。

#### 4.1.2 核心流程

转译从 `transpile()` 开始，整体流程如下：

```text
InstantiatedScript._instantiate_schedule
        │  创建 Transpiler()，调用 transpile(script, name2consts, name2divisibility)
        ▼
transpile()
        │  1. create_script_call_args：区分常量参数与内核参数，构造 params
        │  2. with builder_context(self):     # 让 self.global_view 能找到当前 builder
        │  3.   transpile_call(script.__call__, params, {})
        │  4. body = self.flush_stmts()        # 把语句栈拍平成 SeqStmt
        │  5. 由 script.attrs.blocks/warps 构造 Metadata
        │  6. 返回 Function(name, params=kernel_params, body, metadata)
```

其中第 3 步 `transpile_call` 是真正的「读源码、走 AST」环节：它用 `inspect.getsourcelines` 取出 `__call__` 的源码文本，去掉装饰器和缩进，`ast.parse` 成 AST，然后逐条 `visit` 函数体里的语句。

#### 4.1.3 源码精读

入口 `transpile()` 负责参数处理与最终 `Function` 的组装：[python/tilus/lang/transpiler/transpiler.py:158-213](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L158-L213)。关键几行：

```python
# 用 builder_context 把自己注册为「当前 builder」，
# 这样 __call__ 里的 self.global_view(...) 才能找到语句栈
with builder_context(self):
    self.transpile_call(script.__call__, params.values(), {})
body: Stmt = self.flush_stmts()            # 语句栈 → SeqStmt
...
func = Function(name=script.__class__.__name__, params=kernel_params, body=body, metadata=metadata)
```

`create_script_call_args` 区分两类参数（承接 [u1-l4](u1-l4-datatypes-and-pointer-types.md) 的 const vs 运行时参数）：常量参数（`int/float/str`）直接用 `name2consts` 里的具体值填入（不进 IR）；其余参数必须有 `BaseType` 标注，被创建成 `Var` 作为内核形参：[python/tilus/lang/transpiler/transpiler.py:116-156](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L116-L156)。

真正读源码、走 AST 的是 `transpile_call`：[python/tilus/lang/transpiler/transpiler.py:229-380](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L229-L380)。它先打开三层嵌套作用域（外部环境 → 参数 → 函数体），再用 `inspect` 取源码并解析：

```python
lines, start_line = inspect.getsourcelines(method)
source = "".join(lines)
source, col_offset = eliminate_indent(source)        # 去掉公共缩进
source, inc_lineno = eliminate_decorators(source)     # 去掉装饰器
parsed: ast.Module = ast.parse(source=source)
func_def: ast.FunctionDef = parsed.body[0]
...
with self.scope():  # body scope
    for stmt in func_def.body:
        self.visit(stmt)          # 逐条访问语句 → 往语句栈追加 IR
```

注意一个细节：`transpile_call` 还负责更新 `self.file/start_lineno/start_column`（[transpiler.py:360-372](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L360-L372)）。这些字段记录「当前在用户源码的哪个位置」，当转译出错时，`TilusProgramError` 能把错误指回用户的原始文件与行列，而不是 tilus 内部代码（见 [common.py:25-61](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/common.py#L25-L61)）。

#### 4.1.4 代码实践

**实践目标**：确认「`__call__` 源码 → AST → IR」这条链路确实存在，并定位源码读取发生在哪里。

**操作步骤**：

1. 打开 [python/tilus/lang/transpiler/transpiler.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py)，定位 `transpile_call` 中的 `inspect.getsourcelines(method)` 一行（约 344 行）。
2. 对照 `examples/vector_add/vector_add.py` 的 `VectorAdd.__call__`（[第 27-46 行](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py#L27-L46)）。
3. 想象 `inspect.getsourcelines` 取出的就是这 20 行文本，`ast.parse` 后 `func_def.body` 大约有 8 条语句（赋值、`self.attrs.blocks = ...`、若干 `self.global_view(...)` 等）。

**预期结果**：你能说清「用户写的 `__call__` 文本是被 `inspect` 动态取出、再 `ast.parse` 成节点树」这一事实，而不是某种静态的字节码处理。

> 待本地验证：若想亲眼看到 AST，可在 `transpile_call` 的 `ast.parse` 之后临时打印 `ast.dump(parsed)`（仅用于学习，不要提交）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `transpile` 要先 `with builder_context(self)`，再 `transpile_call`？顺序能反过来吗？

**参考答案**：不能反过来。`builder_context(self)` 的作用是把当前 Transpiler 注册为全局 `_current_builder`（见 4.4）。而 `transpile_call` 在访问 `__call__` 体内时会真实调用 `self.global_view(...)` 等方法，这些方法依赖 `_current_builder` 才能把生成的 `Instruction` 追加到语句栈。若先 `transpile_call`，指令方法会因 `_current_builder is None` 而报错。

---

### 4.2 PythonAstFunctor 访问机制：visit 分派

#### 4.2.1 概念说明

`PythonAstFunctor`（[python/tilus/hidet/lang/transpiler.py:163-317](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/lang/transpiler.py#L163-L317)）定义了一个标准的访问者模式骨架：它声明了一堆 `visit_*` 方法（默认 `raise NotImplementedError`），并约定 `visit(node)` 根据 `node` 的类型分派到对应的 `visit_类名`。

`Transpiler` 重写了其中需要的方法（`visit_Assign`、`visit_For`、`visit_BinOp`、`visit_Call` 等），让每种 Python 语法结构都映射到一段「生成 IR」的逻辑。`visit_*` 的返回值通常是「该表达式求值后的对象」——可能是一个 `Var`（标量）、一个 `Tensor`（张量），或一个 Python 值。

#### 4.2.2 核心流程

`visit` 的分派逻辑很简洁：[python/tilus/lang/transpiler/transpiler.py:90-108](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L90-L108)。

```text
visit(node)
  │  method = "visit_" + node.__class__.__name__   # 例如 ast.For → "visit_For"
  │  若存在该方法 → 调用它
  │  否则 → 抛 TilusProgramError：该 AST 节点不支持
  └ 任何异常都被包成 TilusProgramError，附带出错的 node
```

两个本讲重点关注的 `visit` 方法：

- **`visit_For`**（[transpiler.py:1090-1137](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L1090-L1137)）：处理 `for ... in range(...)`。
- **`visit_BinOp`**（[transpiler.py:735-787](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L735-L787)）：处理 `a + b`、`a * b` 等二元运算。

#### 4.2.3 源码精读

先看 **`visit_For`** 是如何生成循环 IR 的：[python/tilus/lang/transpiler/transpiler.py:1090-1137](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L1090-L1137)。

```python
def visit_For(self, stmt: ast.For) -> None:
    iter_targets: list[ast.Name] = [...]      # 收集循环变量名
    stmt_iter = self.visit(stmt.iter)          # 求值 in 后面的表达式
    ...
    if isinstance(stmt_iter, TilusLoopIterable):
        loop_vars: list[Var] = [...]           # 创建 int32 循环变量
        ...
        with self.block(), self.scope() as for_scope:   # 新开一个语句块 + 作用域
            for var in loop_vars:
                for_scope.bind(name=var.name, var_or_value=var)
            for s in stmt.body:
                self.visit(s)                  # 递归访问循环体 → 追加到块内
        body = self.pop_innermost_last()       # 取出刚才块内的语句
        self.append(stmt_iter.generate_loop_statement(loop_vars=loop_vars, body=body))
```

这里有两点值得注意：

1. `self.visit(stmt.iter)` 求值 `range(...)` 时，得到的是一个 `RangeLoop` 对象（来自 [loops.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/constructs/loops.py)），它是 `TilusLoopIterable` 的子类，**不是** Python 内置 `range`。`RangeLoop.generate_loop_statement` 负责产出真正的 `ForStmt`（含 `unroll_factor`，承接 [u2-l3](u2-l3-control-flow-and-thread-groups.md) 讲的 `unroll` 提示）：[loops.py:53-92](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/constructs/loops.py#L53-L92)。
2. 循环体被放进一个 `self.block()`（新的语句栈帧）里递归访问，访问完用 `pop_innermost_last` 取出整段体，再交给 `generate_loop_statement` 包成 `ForStmt`。这是「先收集子语句、再包一层」的典型手法。

再看 **`visit_BinOp`**，它是 *transpile-run* 思想的最佳体现：[python/tilus/lang/transpiler/transpiler.py:735-787](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L735-L787)。

```python
lhs = self.visit(expr.left)
rhs = self.visit(expr.right)
...
elif isinstance(lhs, RegisterTensor) or isinstance(rhs, RegisterTensor):
    # 把标量也包成 RegisterTensor，保证两边类型一致
    if not isinstance(lhs, RegisterTensor):
        lhs = self.allocate_register(dtype=rhs.dtype, shape=rhs.shape, f_init=lambda _: rhs.dtype(lhs))
    if not isinstance(rhs, RegisterTensor):
        rhs = self.allocate_register(...)
    # 包成 *WithMethods 对象，让 operator.add 触发指令记录
    if isinstance(lhs, RegisterTensor):
        lhs = RegisterTensorWithMethods(lhs, self)
    ...
    return op_dict[type(expr.op)](lhs, rhs)   # 例如 operator.add(lhs, rhs)
```

当 `ra + rb` 的两边都是 `RegisterTensor` 时，`operator.add(lhs, rhs)` 并不是在做数值加法，而是触发 `RegisterTensorWithMethods.__add__`，后者调用 builder 的 `add`，从而生成一条 `AddInst` 并追加为 `InstStmt`（详见 4.4）。这就是「执行即记录」。

而如果两边都是纯标量（`hidet_ir.Expr`/`int`/`float`），则直接用 Python 的 `operator.add` 构造一个 hidet 表达式节点返回——这是「标量在编译期算符号表达式，张量走指令」的分流。

#### 4.2.4 代码实践

**实践目标**：对照一个简单 Script，画出 `for` 循环与二元运算的 AST→IR 映射。

**操作步骤**：

1. 以 `examples/vector_add/vector_add.py` 的 `rc = ra + rb`（[第 45 行](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py#L45)）为例。它在 AST 里是一个 `ast.Assign`，其 `value` 是 `ast.BinOp(op=ast.Add, left=Name('ra'), right=Name('rb'))`。
2. 在纸上画出映射：
   - `ast.Assign` → `visit_Assign`（[transpiler.py:798](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L798)）→ 调 `process_name_assign('rc', <BinOp结果>)`。
   - `ast.BinOp` → `visit_BinOp` → 两边都是 `RegisterTensor` → `operator.add` → 一条 `AddInst`（输出为新 `RegisterTensor`）。
   - 最终 `rc` 这个名字被绑定到 `AddInst` 产生的张量。
3. 再以一个带循环的例子（如 `examples/matmul/matmul_v0.py` 的 K 维循环）对照 `visit_For`：`for k_start in self.range(0, K, block_k)` → `RangeLoop` → `ForStmt`。

**需要观察的现象**：张量运算（`+`）走「指令记录」分支，产生 IR 节点；而像 `offset = self.block_elems * self.blockIdx.x`（[vector_add.py:37](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py#L37)）这种标量 `*` 走「hidet 表达式」分支，只产生一个标量 `Var`，不产生 `InstStmt`。

**预期结果**：你能区分「张量二元运算 → 指令」与「标量二元运算 → 表达式」两条路径，并指出分流点在 `visit_BinOp` 的 `isinstance(..., RegisterTensor)` 判断。

#### 4.2.5 小练习与答案

**练习 1**：`visit_BinOp` 里为什么要把 `RegisterTensor` 再包一层 `RegisterTensorWithMethods`？

**参考答案**：因为 `operator.add(lhs, rhs)` 会调用对象的 `__add__`。裸 `RegisterTensor` 是不可变的数据类、没有重载 `__add__`（它的运算符重载只在转译器内由 `WithMethods` 提供）；包成 `RegisterTensorWithMethods` 后，`operator.add` 才会触发 `__add__`，进而在 builder 上生成 `AddInst` 并返回结果张量。

**练习 2**：如果用户写 `for i in [0, 1, 2]:`（用一个列表当可迭代对象），转译器会怎样？

**参考答案**：`self.visit(stmt.iter)` 会得到一个 Python `list`，它不是 `TilusLoopIterable`，于是 `visit_For` 走到最后的 `else` 分支并抛出 `TilusProgramError`，提示「For loop iterable must be ... range(...)」。Tilus 只接受 `self.range(...)` 这类受控的可迭代对象。

---

### 4.3 ScopedProgramBuilder：作用域与张量/变量绑定

#### 4.3.1 概念说明

转译器在「执行」`__call__` 时，需要一套名字解析机制：当遇到 `ra`，它要知道 `ra` 绑定的是哪个 `RegisterTensor`；遇到 `n`，要知道是哪个内核形参 `Var`。这套机制就是作用域（Scope）。

`ScopedProgramBuilder`（[builder.py:73-117](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/builder.py#L73-L117)）维护一条**作用域链**：每个 `Scope` 节点保存三类绑定，并通过 `parent` 指针串成链；查找名字时从当前作用域逐级向上找。

#### 4.3.2 核心流程

一个 `Scope` 同时承载三种「名字→对象」映射，对应三类被绑定的对象：

```text
Scope
├── name2var:    dict[str, Var]       # 标量变量（hidet Var，会进 IR）
├── name2value:  dict[str, Tensor]    # 张量（RegisterTensor/SharedTensor/...）
└── name2host_var: dict[str, Any]     # 宿主对象（如 range 函数、外部导入的常量）
```

绑定与查找的规则：

- `bind(name, x)`：按 `x` 的类型分到上述三个字典之一；**同一作用域内不允许重名**（除了 `_`，用于丢弃赋值）。
- `lookup(name)`：先查当前作用域的三个字典，未命中则递归查 `parent`，直到链顶。

作用域的进出由 `with self.scope():` 管理（`Scope.__enter__/__exit__` 会切换模块级 `_current_scope`），这正好对应 Python 的词法作用域：进入函数体、进入 `for`/`if`/`with` 块都开新作用域，退出时还原。

#### 4.3.3 源码精读

`Scope` 的核心实现：[python/tilus/lang/transpiler/builder.py:28-70](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/builder.py#L28-L70)。

```python
def bind(self, name, var_or_value):
    if name in self.name2var or name in self.name2value or name in self.name2host_var:
        if name == "_":
            return                       # 允许反复给 '_' 赋值（丢弃）
        raise RuntimeError(f'Variable "{name}" has already been defined in the current scope.')
    if isinstance(var_or_value, Var):
        self.name2var[name] = var_or_value
    elif isinstance(var_or_value, Tensor):
        self.name2value[name] = var_or_value
    else:
        self.name2host_var[name] = var_or_value

def lookup(self, name):
    if name in self.name2var:   return self.name2var[name]
    if name in self.name2value: return self.name2value[name]
    if name in self.name2host_var: return self.name2host_var[name]
    if self.parent:
        return self.parent.lookup(name)   # 沿父链向上
    return None
```

`ScopedProgramBuilder` 在构造时种下一个内置作用域，把 `range` 绑成 tilus 的 `range`（而非 Python 内置）：[builder.py:73-83](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/builder.py#L73-L83)。

```python
self.builtin_scope.bind("range", tilus.lang.constructs.loops.range)
```

`transpile_call` 在转译一个方法时，会构造三层作用域（外部环境 → 参数 → 函数体）：[transpiler.py:253-262](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L253-L262)。

```python
self.dump_and_push_scopes()                 # 保存当前作用域链，切回 builtin scope
with self.scope():                          # 外部环境作用域
    external_env = self.get_external_env(method)   # 取 __globals__ 与自由变量
    for name, value in external_env.items():
        self.bind(name, value)              # 用户在模块顶部 import 的东西在这里生效
    with self.scope():                      # 参数作用域
        self.bind("self", method.__self__)  # 绑定 self（Script/Class 实例）
        for ...:                            # 逐个绑定形参（见下）
        with self.scope():                  # 函数体作用域
            for stmt in func_def.body:
                self.visit(stmt)
```

参数绑定按标注分三种处理：[transpiler.py:288-340](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L288-L340)：

- 标注为 `RegisterTensor/SharedTensor/GlobalTensor`：按引用绑定（张量对象本身）。
- 标注为 `DataType/PointerType`（如 `int32`、`~float32`）：声明一个 `Var` 并绑定（标量按值）。
- 标注为 `bool/int/float/str`：编译期常量，直接绑定 Python 值，不进 IR。

还有一个容易忽略的细节——`dump_and_push_scopes`/`pop_and_restore_scopes`：[builder.py:104-116](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/builder.py#L104-L116)。当转译一个嵌套调用的方法（比如 `__call__` 里又调用了用户定义的 Class 方法），它会把当前作用域链压栈、切回 builtin 作用域再开始，结束后还原。这保证每次 `transpile_call` 都从一个干净的作用域起点开始，方法之间不会串名字。

#### 4.3.4 代码实践

**实践目标**：通过一个故意触发「重复定义」的例子，验证作用域规则。

**操作步骤**：

1. 复制 `examples/vector_add/vector_add.py` 为一个临时脚本（不要改原文件）。
2. 在 `__call__` 里写两行：
   ```python
   ra = self.load_global(ga, offsets=[offset], shape=[self.block_elems])
   ra = self.load_global(gb, offsets=[offset], shape=[self.block_elems])  # 同作用域重名
   ```
3. 实例化并调用内核，观察抛出的错误。

**预期结果**：转译器抛出 `RuntimeError: Variable "ra" has already been defined in the current scope.`——这正是 `Scope.bind` 的重名检查（[builder.py:49-53](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/builder.py#L49-L53)）。对比之下，若把第二行写成 `_ = ...` 则不会报错。

> 待本地验证：具体报错堆栈是否一定来自 `Scope.bind`，可在本机跑一次确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `ra`（一个 `RegisterTensor`）和 `offset`（一个标量 `Var`）放在同一个 `Scope` 的不同字典里，而不是合用一个字典？

**参考答案**：因为它们在 IR 里的角色不同——`Var` 是标量表达式里的变量，`Tensor` 是指令的输入/输出。分开存放既能在 `lookup` 时直接返回正确类型的对象（供后续 `visit_*` 分流处理），也避免了类型判断的歧义。同时这天然实现了「同名张量与同名标量互不冲突」在结构上的隔离（虽然 `bind` 仍统一查重）。

**练习 2**：用户在 `__call__` 里 `import` 了 `math` 并写 `math.sqrt(x)`，转译器怎么找到 `math`？

**参考答案**：`transpile_call` 用 `get_external_env`（[transpiler.py:215-227](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L215-L227)）取出方法的 `__globals__` 与自由变量，把它们绑定在外部环境作用域。于是 `visit_Name('math')` 能 `lookup` 到模块对象，`visit_Attribute` 取到 `math.sqrt`，最终在 `visit_Call` 里被映射成 hidet 的 `primitives.sqrt`（见 4.4 的内置函数分支）。

---

### 4.4 从指令调用到 InstStmt：builder_context 与 LambdaProxy

#### 4.4.1 概念说明

本模块回答本讲最核心的问题：用户在 `__call__` 里写 `self.global_view(...)`，这行代码是怎么变成 IR 里的一条 `InstStmt` 的？

答案是一根三段式的桥：

1. **`builder_context`** 把当前 Transpiler 注册为全局 `_current_builder`。
2. **指令组**（如 `RootInstructionGroup`）上的 `self.global_view` 只是一个薄包装，它通过 `self._builder` 取到当前 builder，再委托给 builder 的同名方法。
3. **`StmtBuilder.append`** 把生成的 `Instruction` 包成 `InstStmt` 追加到语句栈。

此外，本模块还讲一个「延迟执行」机制——**`LambdaProxy`**。当用户在 `__call__` 里写一个 `lambda`（例如 `allocate_register` 的 `f_init=lambda axes: ...`），它不会立刻被求值，而是被包成 `LambdaProxy`，等到 builder 真正需要构造指令的初始化表达式时才在转译期执行。

#### 4.4.2 核心流程

以 `ga = self.global_view(a_ptr, dtype=float32, shape=[n])` 为例，整条链路：

```text
visit_Call(self.global_view(...))            # AST: ast.Call
  │  func = visit_Attribute(self.global_view) → 绑定方法（MethodType）
  │  命中 visit_Call 的「直接调用」分支：method(*args, **kwargs)
  ▼
RootInstructionGroup.global_view(...)        # root.py:薄包装
  │  return self._builder.global_view(...)   # _builder 即全局 _current_builder
  ▼
StmtBuilder.global_view(...)                 # stmt_builder.py
  │  inst = GlobalViewInst.create(output=..., ptr=ptr)
  │  self.append(inst)
  ▼
StmtBuilder.append(inst)                     # stmt_builder.py:408
  │  stmt = InstStmt(inst_or_stmt)   # Instruction → InstStmt
  │  self._stack[-1].append(stmt)
```

关键在于：`visit_Call` 对这类「指令方法」**不是**递归 `transpile_call`（那是给用户自定义方法用的），而是**直接调用**它——这正是 *transpile-run* 的体现。

#### 4.4.3 源码精读

**第一段：`builder_context` 桥接全局 builder。** [python/tilus/lang/instructions/base.py:19-44](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/base.py#L19-L44)。

```python
_current_builder: Optional[StmtBuilder] = None

class InstructionBuilderContext:
    def __enter__(self) -> None:
        global _current_builder
        _current_builder = self.builder       # 进入时设为当前 Transpiler
    def __exit__(self, *args):
        global _current_builder
        _current_builder = None

class InstructionGroup:
    @property
    def _builder(self) -> StmtBuilder:
        global _current_builder
        assert _current_builder is not None
        return _current_builder               # 指令组从这里取回 builder

def builder_context(builder): return InstructionBuilderContext(builder)
```

`transpile` 里那句 `with builder_context(self)` 就是把 Transpiler 自己注册进去（4.1 已见）。于是 `RootInstructionGroup` 上的 `self.global_view` 能取到它：

[python/tilus/lang/instructions/root.py:460-470](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L460-L470)

```python
def global_view(self, ptr, *, dtype, shape, strides=None):
    ...
    return self._builder.global_view(ptr=ptr, dtype=dtype, layout=layout)   # 委托
```

**第二段：`visit_Call` 如何决定「直接调用」还是「递归转译」。** [python/tilus/lang/transpiler/transpiler.py:555-704](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L555-L704)。它把调用分成若干情形：

```python
if isinstance(func, types.MethodType):
    method = func; f_self = method.__self__; f_func = method.__func__
    if isinstance(f_self, Script) and getattr(Script, f_func.__name__, None) is not f_func:
        ret = self.transpile_call(method, args, kwargs)          # 情形1：用户自定义方法 → 递归转译
    elif isinstance(f_self, Class) and getattr(Class, f_func.__name__, None) is not f_func:
        ret = self.transpile_call(method, args, kwargs)          # 情形1：用户 Class 方法
    elif isinstance(f_self, (GlobalTensor, SharedTensor, RegisterTensor, TMemoryTensor)):
        ...                                                      # 情形2：张量方法（如 tensor.to(...)）
    else:
        ret = method(*args, **kwargs)                           # 情形4：直接调用 ← global_view 走这里
elif isinstance(func, (types.BuiltinMethodType, types.BuiltinFunctionType)):
    ...                                                          # 情形3：内置函数（max/min/sqrt...）
```

判定的关键 trick 是 `getattr(Script, f_func.__name__, None) is not f_func`：`global_view` 是从 `RootInstructionGroup` **继承**来的，`getattr(Script, 'global_view')` 与 `f_func` 是同一个函数，于是 `is not` 为假，情形 1 不成立，落到情形 4 **直接调用**。而用户在子类里**自己定义**的方法，`getattr(Script, name)` 取到的是基类版本、与子类的 `f_func` 不同，于是 `is not` 为真，走情形 1 递归转译（即 *inlined kernel procedure*）。

**第三段：`append` 把 Instruction 包成 InstStmt。** [python/tilus/ir/builders/stmt_builder.py:408-413](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/builders/stmt_builder.py#L408-L413)。

```python
def append(self, inst_or_stmt):
    if isinstance(inst_or_stmt, Instruction):
        stmt: Stmt = InstStmt(inst_or_stmt)   # ← 这一行是「指令 → IR 语句」的关口
    else:
        stmt = inst_or_stmt
    self._stack[-1].append(stmt)
```

`StmtBuilder.global_view`（[stmt_builder.py:1235-1238](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/builders/stmt_builder.py#L1235-L1238)）调用 `GlobalViewInst.create(...)` 后 `self.append(inst)`，于是 `GlobalViewInst` 经此关口变成 `InstStmt`，挂到语句栈。最终 `flush_stmts` 把整栈拍成 `SeqStmt`，成为 `Function.body`。

**第四段：`LambdaProxy`——把 lambda 延迟到转译期执行。** [python/tilus/lang/transpiler/transpiler.py:56-79](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L56-L79)。

```python
class LambdaProxy:
    def __init__(self, lambda_expr: ast.Lambda, translator: Transpiler):
        self.lambda_expr = lambda_expr
        self.translator = translator

    def __call__(self, *args, **kwargs):
        with self.translator.scope() as lambda_params_scope:    # 新开作用域绑定 lambda 形参
            for arg, arg_expr in zip(self.lambda_expr.args.args, args):
                lambda_params_scope.bind(arg.arg, arg_expr)
            return self.translator.visit(self.lambda_expr.body) # 在转译期访问 lambda 体
```

当 `visit_Lambda`（[transpiler.py:839-840](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L839-L840)）遇到一个 lambda 时，并不立刻执行它，而是返回 `LambdaProxy`。这个代理被当作普通 Python 可调用对象传给 builder（例如 `allocate_register(..., f_init=lambda axes: axes[0] + 1)`）。等到 builder 真正构造指令、需要那个初始化表达式时才调用它——此时 `LambdaProxy.__call__` 在一个新作用域里绑定 lambda 形参，再 `visit` lambda 体，产出一个 hidet `Expr`。这样 lambda 体里的运算也被纳入转译，而非在宿主 Python 里算掉。

#### 4.4.4 代码实践

**实践目标**：跟踪一条 `self.global_view(...)` 从 AST 到 `InstStmt` 的完整路径，并验证 `LambdaProxy` 的延迟执行。

**操作步骤**：

1. **跟踪指令调用**：在 [stmt_builder.py:408](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/builders/stmt_builder.py#L408) 的 `append` 方法处设断点（或临时加一行 `print(type(inst_or_stmt))`，仅供学习），运行 `vector_add` 的 `main()`。
2. 观察调用栈：`append` 的调用者应是 `global_view`/`load_global` 等 builder 方法，而被追加的对象是 `GlobalViewInst`/`LoadGlobalInst` 等 `Instruction` 子类，包成的 `InstStmt` 被压入 `_stack[-1]`。
3. **验证 LambdaProxy**：在 `visit_Lambda`（[transpiler.py:839](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/transpiler/transpiler.py#L839)）处观察：vector_add 本身没有 lambda，可改用任意一个带 `f_init=lambda ...` 的内核（例如 `allocate_register(dtype=..., shape=[n], f_init=lambda axes: 0.0)`）。你会看到 lambda 体并非立即执行，而是在 builder 构造 `AllocateRegisterInst` 时由 `LambdaProxy.__call__` 触发。

**需要观察的现象**：

- `append` 被调用多次，对应 `__call__` 里每条指令各一次（`global_view` ×3、`load_global` ×2、`store_global` ×1，以及 `+` 产生的 `AddInst`）。
- 这些 `InstStmt` 最终在 `flush_stmts` 处合成一个 `SeqStmt`，即 `Function.body`。

**预期结果**：你能完整复述「`visit_Call` → 直接调用指令方法 → `_builder` 委托 → `Instruction.create` → `append` → `InstStmt`」这条链，并解释 lambda 为何要延迟到 builder 内才执行（因为它的形参是 builder 提供的坐标 `Var`，只有构造指令时才存在）。

> 待本地验证：步骤 1、3 的断点/打印行为需在带 GPU 的环境运行；无 GPU 时可只做源码阅读与栈推演。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `visit_Call` 里 `getattr(Script, f_func.__name__, None) is not f_func` 的判断去掉，会发生什么？

**参考答案**：那么 `self.global_view(...)` 会被误判为情形 1（用户自定义方法），进入 `transpile_call` 递归转译 `global_view` 的「源码」。但 `global_view` 是 tilus 内部方法，其源码里充满 `self._builder` 之类的转译期对象，并非为递归转译而写，会立即出错或产生错误的 IR。这个判断正是用来区分「继承下来的指令方法（直接调用）」与「用户自定义方法（递归转译）」。

**练习 2**：`LambdaProxy` 为什么不直接在 `visit_Lambda` 里把 lambda 体求值掉？

**参考答案**：因为 lambda 的形参（如 `f_init=lambda axes: ...` 中的 `axes`）只有在 builder 构造指令时才被赋值（它们是表示坐标的 `Var`）。在 `visit_Lambda` 时刻这些形参还没有值，必须把「lambda 体」作为一个可延迟执行的对象保存下来，等到 builder 调用它时再在新作用域里绑定形参并转译体。这正是「表达式代理」的意义——把求值推迟到它真正有上下文的时刻。

---

## 5. 综合实践

把本讲四个模块串起来：用源码阅读 + IR 导出，亲手验证「一段 `__call__` 如何变成一棵 IR 树」。

1. **准备**：开启 IR 导出，参考 [u3-l1](u3-l1-compilation-pipeline-overview.md) 与项目 `CLAUDE.md` 的 Cache 小节：
   ```python
   import tilus
   tilus.option.cache_dir("/tmp/tilus-vadd-cache")
   tilus.option.debug.dump_ir()
   ```
2. **运行** `examples/vector_add/vector_add.py` 的 `main()`（若无 GPU，可只阅读缓存目录中已生成的产物，或用 compile-only 模式，见 [u8-l4](u8-l4-debugging-testing-profiling.md)）。
3. **定位产物**：在 `/tmp/tilus-vadd-cache` 下找到对应 `programs/<hash>/` 目录，查看 `program.txt`（转译后、优化前的 Tilus IR）与 `ir/` 下各 Pass 后的 IR。
4. **对照源码逐行解释**，针对 `VectorAdd.__call__` 的每一行（[vector_add.py:34-46](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py#L34-L46)），在 `program.txt` 里找到它生成的 IR，并填入下表：

   | `__call__` 源码行 | 负责的 visit 方法 | 生成的 IR 节点 |
   | --- | --- | --- |
   | `self.attrs.blocks = (cdiv(n, ...),)` | `visit_Assign`→`process_attribute_assign` | （写进 Metadata，非语句） |
   | `offset: int32 = ...` | `visit_AnnAssign`→`process_name_assign` | `DeclareStmt` |
   | `ga = self.global_view(...)` | `visit_Call`(情形4)→`global_view` | `InstStmt(GlobalViewInst)` |
   | `ra = self.load_global(...)` | 同上→`load_global` | `InstStmt(LoadGlobalInst)` |
   | `rc = ra + rb` | `visit_BinOp`→`add` | `InstStmt(AddInst)` |
   | `self.store_global(...)` | `visit_Expr`→`visit_Call` | `InstStmt(StoreGlobalInst)` |

5. **反思**：确认你看到了「张量运算→指令、标量运算→表达式」的分流，以及 `self`/参数/`offset` 如何通过作用域绑定进入 IR。

> 待本地验证：表中「生成的 IR 节点」一列的具体形态以本地 `program.txt` 实际内容为准。

## 6. 本讲小结

- `Transpiler` 同时是 AST 访问器（`PythonAstFunctor`）和语句构造器（`ScopedProgramBuilder`），它用 `inspect` 取出 `__call__` 源码、`ast.parse` 成节点树，再逐条 `visit` 生成 IR，最后 `flush_stmts` 拍成 `Function.body`。
- 转译采用 **transpile-run（转译即运行）** 模式：`visit_*` 真实执行用户代码，但张量/指令运算被拦截、记录为 IR，而非数值计算；标量运算则走 hidet 表达式分支。
- 作用域（`Scope`）用三个字典分别承载 `Var`/`Tensor`/宿主对象，沿父链查找；`transpile_call` 用三层作用域（外部环境→参数→函数体）绑定 `self`、形参与自由变量，并用 `dump_and_push_scopes` 保证嵌套调用互不串扰。
- 指令调用经 `builder_context`→`InstructionGroup._builder`→`StmtBuilder.append` 三段桥，把 `Instruction` 包成 `InstStmt` 追加到语句栈；`visit_Call` 用 `getattr(Script, name) is not f_func` 区分「继承的指令方法（直接调用）」与「用户自定义方法（递归转译）」。
- `LambdaProxy` 把 lambda 体延迟到 builder 构造指令时才在新作用域里转译，使 lambda 形参（坐标 `Var`）能正确绑定。

## 7. 下一步学习建议

- 接下来读 [u3-l3：Tilus IR 结构](u3-l3-tilus-ir-program-function-stmt.md)，正式认识 `Program/Function/Stmt` 的不可变数据类设计，理解本讲产出的 `Function.body` 到底是什么结构。
- 再读 [u3-l4：Instruction 与 Tensor](u3-l4-instruction-and-tensor.md)，看清本讲里反复出现的 `GlobalViewInst`/`AddInst` 与 `RegisterTensor`/`GlobalTensor` 的内部字段。
- 想自己写一个 Pass 处理转译产物时，参考 [u5-l1：Pass 框架与 IRRewriter](u5-l1-pass-framework-irrewriter.md)，那里会讲如何遍历本讲生成的 `InstStmt` 树。
