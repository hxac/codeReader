# FunctionVisitor 总览：AST 遍历器架构

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `FunctionVisitor` 的输入（源码行、`Specialization`、全局变量、位置信息、选项）与输出（填好的 `ir.ModuleOp`）分别是什么、由谁准备、交给谁。
2. 理解 `VisitorState` 中 `inside_function`、`return_allowed`、`discard_everything`、`visited_return_types` 等状态字段分别在守卫什么规则。
3. 理解 `global_builder` 单例如何承载 `ir.Builder` 与 `ir.ModuleOp`，以及它的生命周期为什么必须由调用方（`jit.py`）管理。
4. 掌握 `ast.NodeVisitor` 的 `visit_*` 分发机制，并能独立定位任意 AST 节点类型在 `function_visitor.py` 中的处理方法。

本讲是第 4 单元「AST 到 ASC-IR」的第一讲，只讲**架构骨架**：构造、状态、builder、分发。赋值/运算符细节在 u4-l2，控制流在 u4-l3，函数调用内联在 u4-l4 展开。

## 2. 前置知识

- **AST（抽象语法树）**：Python 解释器在执行前会把源码解析成一棵树，树上每个节点是 `ast.FunctionDef`、`ast.Assign`、`ast.Call` 这样的对象。`import ast; ast.parse(source)` 就能得到它。pyasc 不解释执行你的 kernel，而是「读」这棵树。
- **`ast.NodeVisitor`**：Python 标准库提供的访问者模式基类。调用 `visitor.visit(node)` 时，基类会自动寻找并调用 `visit_节点类名` 方法，例如遇到 `ast.Assign` 就调 `visit_Assign`；找不到对应方法时落到 `generic_visit`。pyasc 重写了 `generic_visit`，让它直接报「语法不支持」——这是 pyasc 语法边界的第一道防线。
- **`Specialization`（参数类型表）**：u3-l3 讲过的参数特化结果——一个装着 `args`（运行时参数的 IR 类型）与 `constexprs`（编译期常量值）的对象。codegen 阶段要靠它来生成 IR 函数的形参列表。
- **`global_builder`**：u2-l5 提过的全局单例，持有 pybind11 暴露的 `ir.Builder`。所有 language 层 API（如 `asc.add`）都通过它向 IR 追加操作。本讲会看清它的完整生命周期。
- **`IRValue` 双层设计**：u2-l3 讲过 `IRHandle`（裸 IR 句柄）与 `IRValue`（带 dtype 的 Python 包装）。本讲会看到 visitor 如何把 `PlainValue`/`GlobalAddress` 存进作用域。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py) | 本讲主角。`FunctionVisitor` 继承 `ast.NodeVisitor`，把 kernel 的 AST 翻译成 ASC-IR；同时定义 `CodegenOptions`、`VisitorState`、`BlockInOut` 等数据类 |
| [python/asc/codegen/name_scope.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py) | `NameScope`：编译期的变量表，按「局部 → 全局 → 内置白名单」三级查找名字 |
| [python/asc/language/core/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py) | `GlobalBuilder` 类与 `global_builder` 单例（另含 `OverloadDispatcher`、`require_jit`，u2-l5 已讲） |
| [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py) | 调用方。`_run_codegen` 负责「建 builder → 建 visitor → visit → 收模块 → teardown」 |
| [python/asc/codegen/function.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py) | `Function` 基类：装饰时抓好的源码、AST 节点、`FunctionLocation` 都从这里来 |
| [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) | 实践对象：`vadd_kernel` 的 AST 是本讲动手分析的目标 |

## 4. 核心概念与源码讲解

### 4.1 FunctionVisitor 构造

#### 4.1.1 概念说明

`FunctionVisitor` 是一台「翻译机」：一端吃进 Python kernel 的 AST，另一端借助 `global_builder` 向 MLIR 模块里写操作。它不自己创建 `ir.Builder`，也不自己创建 IR 上下文——这些外部资源在它诞生之前就已就绪。构造函数只做四件事：

1. 记录输入（源码行、参数类型表、位置、选项）；
2. 用「全局变量 + ConstExpr 实参」初始化 `NameScope`；
3. 把 builder 的插入点与位置信息设置到正确位置；
4. 初始化 `VisitorState`。

理解「输入从哪来」是本节的关键：这些参数全部由 u3-l2 讲过的 `Function` 基类在**装饰时**一次性捕获，调用时只是取用。

#### 4.1.2 核心流程

```text
@asc.jit 装饰阶段（u3-l2）
    inspect.getsource → fn.src（源码行）、fn.node（FunctionDef AST）、fn.location（文件名+行偏移）
调用阶段 jit.py:_run_codegen
    global_builder.set_ir_builder(context)          # 建 Builder + ModuleOp
    FunctionVisitor(
        source_lines = fn.src,                       # 报错时还原源码上下文
        spec         = Specialization(arg_types, constexprs),  # u3-l3 的参数类型表
        global_vars  = fn.fn.__globals__,            # kernel 所在模块的全局变量字典
        location     = fn.location,
        options      = CodegenOptions(...),
        is_kernel    = True,
    )
    visitor.visit(fn.node)                           # 从 FunctionDef 开始遍历
    return global_builder.get_ir_module()            # 输出：填好的 ir.ModuleOp
```

#### 4.1.3 源码精读

先看构造函数本体（[python/asc/codegen/function_visitor.py:L70-L92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L70-L92)）：

```python
class FunctionVisitor(ast.NodeVisitor):

    def __init__(
        self,
        source_lines: Optional[List[str]],
        spec: Specialization,
        global_vars: Dict[str, Any],
        location: FunctionLocation,
        options: CodegenOptions,
        visited_return_types: Optional[ReturnTypesDict] = None,
        is_kernel: bool = True,
    ):
        super().__init__()
        self.src = source_lines
        self.ir_function: Optional[ir.FuncOp] = None
        self.spec = spec
        self.scope = NameScope(merge_dict(global_vars, spec.constexprs))
        self.location = location
        global_builder.get_ir_builder().set_insertion_point_to_start(global_builder.get_ir_module().get_body())
        global_builder.get_ir_builder().set_loc(self.location.filename, self.location.line_offset, 0)
        self.state = VisitorState()
        self.options = options
        self.is_kernel = is_kernel
        if visited_return_types:
            self.state.visited_return_types = visited_return_types
```

逐行拆解：

- `self.scope = NameScope(merge_dict(global_vars, spec.constexprs))`：把 kernel 模块的全局变量和本次调用传入的 ConstExpr 实参**合并成一张初始变量表**。之后 kernel 里的自由变量（如 `TILE_NUM`）和 ConstExpr 形参都能在 `visit_Name` 时查到值——这正是 u3-l2 讲的「ConstExpr 两种来源」在 codegen 侧的汇合点。
- 两行 `global_builder.get_ir_builder()...`：把 IR 插入点设到模块体开头（此时模块还是空的），并把位置信息设为「kernel 文件名 + 起始行偏移」。之后每条 IR 操作都会带上源码位置，报错时能指回 Python 行号。
- `is_kernel`：区分 Kernel 函数与 Device 侧执行函数（u1-l4 的概念）。同一个类两种用法：Kernel 不允许 `return` 值、要 `make_global`；Device 函数允许返回、被内联调用（详见 u4-l4）。
- `visited_return_types`：递归访问 Device 子函数时，父 visitor 会把自己的这张表传给子 visitor，避免同一子函数被重复展开（见 4.2 节）。

输入的来源在调用方 [python/asc/runtime/jit.py:L184-L194](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L184-L194)：

```python
    def _run_codegen(self, spec: Specialization, options: CodegenOptions) -> ir.ModuleOp:
        self.context = self.create_context()
        if not options.ir_multithreading:
            self.context.disable_multithreading()
        try:
            global_builder.set_ir_builder(self.context)
            visitor = self.codegen(self.src, spec, self.fn.__globals__, self.location, options, is_kernel=True)
            visitor.visit(self.node)
            return global_builder.get_ir_module()
        finally:
            global_builder.teardown()
```

注意三个细节：

1. `global_builder.set_ir_builder(self.context)` 在构造 visitor **之前**执行——所以构造函数里才能直接 `get_ir_builder()`。顺序不能颠倒。
2. visitor 的输出不通过返回值传递，而是「顺手」写进 `global_builder` 持有的模块，`_run_codegen` 最后用 `get_ir_module()` 收走。
3. `self.codegen` 是类属性（[python/asc/runtime/jit.py:L30-L33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L30-L33) 中 `codegen: Type[FunctionVisitor] = FunctionVisitor`），默认就是本讲的 `FunctionVisitor`，子类可替换——u3-l1 讲过的组合式扩展点。

`FunctionLocation` 的定义很轻（[python/asc/codegen/function.py:L24-L27](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L24-L27)）：

```python
@dataclass
class FunctionLocation:
    filename: str = "<source>"
    line_offset: int = 0
```

只有文件名和行偏移两个字段。AST 节点自己的 `lineno` 是相对函数体的行号，IR 里的真实行号 = `location.line_offset + node.lineno`（这个加法发生在 4.4 节的 `visit` 里）。

而 `fn.node` 这棵 AST 是装饰时由 `get_function_node` 抓取的（[python/asc/codegen/function.py:L89-L98](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L89-L98)）：剥掉装饰器、`textwrap.dedent` 去缩进、`ast.parse` 解析，并校验「模块必须恰好含一个 FunctionDef」。

#### 4.1.4 代码实践

**实践目标**：亲手扮演一次 `_run_codegen`，体会「builder 先就位、visitor 后构造」的顺序约束。

**操作步骤**：

1. 阅读 [python/asc/runtime/jit.py:L184-L194](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L184-L194)，把五步（建 context → set_ir_builder → 构造 visitor → visit → get_ir_module/teardown）抄成一张时序小抄。
2. 打开 [python/asc/codegen/function_visitor.py:L70-L92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L70-L92)，逐个构造参数标注来源：哪个来自 `Function` 基类装饰时的捕获？哪个来自 `_run` 调用时的实参？
3. 思考验证：如果把 `_run_codegen` 中的 `set_ir_builder` 一行移动到 `visitor = ...` 之后，会发生什么？

**需要观察的现象**：构造参数可以全部在 `Function`（装饰期产物）与 `Specialization`（调用期产物）两条线上归位，没有一个是凭空出现的。

**预期结果**：第 3 步的结论是——构造函数里 `global_builder.get_ir_builder()` 会返回 `None`，随后 `.set_insertion_point_to_start(...)` 抛 `AttributeError`。这就是「builder 必须先就位」的硬约束。（待本地验证：可在已装好 pyasc 的环境里用 Python 直接调用这两行复现。）

#### 4.1.5 小练习与答案

**练习 1**：`global_vars` 参数传的是 `self.fn.__globals__`，为什么不能用 `globals()`？

**答案**：`globals()` 是**调用方模块**（`jit.py`）的全局变量表，而 `self.fn.__globals__` 是**被装饰 kernel 所在模块**的全局表。kernel 里引用的自由变量（如 `add.py` 里的 `TILE_NUM`、`BUFFER_NUM`）必须去 kernel 自己的模块里找；用 `globals()` 会让所有 `Name` 查找都落空。

**练习 2**：`spec.constexprs` 为什么要和 `global_vars` 合并进同一个 `NameScope`，而不是分开存？

**答案**：从 `visit_Name` 的视角看，「模块级全局常量」和「Host 传进来的 ConstExpr 实参」在 kernel 体内出现的形式完全一样——都是一个裸名字。两者都必须在编译期解出具体的 Python 值，合并成一张表后 `dereference_name` 就不需要区分来源，查找逻辑统一为「局部 → 全局+constexpr → 内置」。

**练习 3**：`ir_function` 为什么在构造函数里初始化为 `None`，而不是立刻创建？

**答案**：`FuncOp` 要在遍历到 `FunctionDef` 节点时才能创建——函数名来自 AST 节点，形参类型来自 `spec.args`，且创建后还要马上建入口块、绑定形参（见 4.4 节 `visit_FunctionDef`）。构造 visitor 时 AST 还没开始走，只能先占位为 `None`。

### 4.2 VisitorState

#### 4.2.1 概念说明

`VisitorState` 是一个 dataclass，记录「遍历进行到什么阶段、什么允许、什么禁止」。AST 遍历是递归下降的过程，同一套 `visit_*` 方法会在不同的语法位置被复用（函数顶层、循环体内、if 分支内、被内联的子函数内），很多合法性规则是**位置相关**的：`return` 在函数顶层合法、在循环体内非法；函数定义只能出现在遍历的起点；`return` 之后的代码是死代码。这些规则没有写死在每个方法里，而是集中由状态字段表达。

#### 4.2.2 核心流程

五个字段的分工：

| 字段 | 初值 | 谁修改它 | 守卫的规则 |
| --- | --- | --- | --- |
| `discard_everything` | `False` | `visit_Return` 置 `True`；`visit_FunctionDef` 结束时复位 | `return` 后面的语句是死代码，`visit` 直接短路返回 `None`，不再生成任何 IR |
| `inside_function` | `False` | `visit_FunctionDef` 进入时置 `True`、退出时复位 | 遍历起点必须是 `FunctionDef`；`FunctionDef` 内不允许再嵌 `FunctionDef`（不支持嵌套函数） |
| `return_allowed` | `True` | `visit_region` 进入嵌套块时置 `False`、退出时恢复 | `return` 不允许出现在嵌套块（循环体/if 分支）里 |
| `return_types` | `[]` | `visit_Return` 记录返回值类型 | Device 函数的返回类型要回填到 `FuncOp` 的函数签名上 |
| `visited_return_types` | `{}` | `call_jit_function` 写入 | 跨函数共享「已访问子函数的返回类型表」，防止同一子函数被重复展开 |

配合的状态流（以一次 Kernel 编译为例）：

```text
visit(FunctionDef)          inside_function: False → True
  ├─ visit(Assign/For/...)  return_allowed: True（顶层）
  │   └─ visit_region()     return_allowed: True → False（进入嵌套块）→ True（退出恢复）
  ├─ visit(Return)          discard_everything → True（kernel 里直接报错，见 visit_Return）
  └─ FunctionDef 收尾       inside_function、discard_everything 复位
```

#### 4.2.3 源码精读

定义在 [python/asc/codegen/function_visitor.py:L51-L57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L51-L57)：

```python
@dataclass
class VisitorState:
    discard_everything: bool = False
    inside_function: bool = False
    return_allowed: bool = True
    return_types: List[ReturnType] = field(default_factory=list)
    visited_return_types: ReturnTypesDict = field(default_factory=dict)
```

三个字段的消费点。第一，`discard_everything` 在总入口 `visit` 里被消费（[python/asc/codegen/function_visitor.py:L293-L297](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L293-L297)）：

```python
    def visit(self, node: Optional[ast.AST]) -> Optional[Any]:
        if node is None:
            return None
        if self.state.discard_everything:
            return None
```

第二，`inside_function` 的两道守卫紧接着生效（[python/asc/codegen/function_visitor.py:L298-L301](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L298-L301)）：

```python
        if self.state.inside_function and isinstance(node, ast.FunctionDef):
            self.raise_unsupported(node, "Nested functions are not supported")
        if not self.state.inside_function and not isinstance(node, ast.FunctionDef):
            raise RuntimeError(f"JIT compilation is applicable to functions only, got {node.__class__.__name__} node")
```

两条规则合起来划定了遍历的形状：**必须以 `FunctionDef` 开头，且全程只有一个 `FunctionDef`**。

第三，`return_allowed` 由 `visit_region` 维护、由 `visit_Return` 消费。`visit_region` 是所有嵌套块（循环体、if 分支、子函数体）共用的上下文管理器（[python/asc/codegen/function_visitor.py:L328-L340](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L328-L340)）：

```python
    @contextmanager
    def visit_region(self) -> Generator[Tuple[NameScope, ir.InsertPoint], Any, None]:
        outer_scope = self.scope
        self.scope = outer_scope.inherit()
        insert_point = global_builder.get_ir_builder().save_insertion_point()
        return_allowed = self.state.return_allowed
        self.state.return_allowed = False
        try:
            yield outer_scope.inherit(), insert_point
        finally:
            self.scope = outer_scope
            global_builder.get_ir_builder().restore_insertion_point(insert_point)
            self.state.return_allowed = return_allowed
```

它一次做了三件事：继承出子作用域（`NameScope.inherit`）、保存/恢复 IR 插入点（先在一个临时 `ir.Block` 里构建，稍后整体搬进目标区域——u4-l3 会用到这个技巧）、把 `return_allowed` 压为 `False`。于是 `visit_Return` 的检查（[python/asc/codegen/function_visitor.py:L644-L652](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L644-L652)）：

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

最后看 `visited_return_types` 的跨函数共享。`call_jit_function` 处理对另一个 `@asc.jit` 函数的调用时（[python/asc/codegen/function_visitor.py:L193-L202](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L193-L202)）：

```python
        if global_builder.get_ir_module().has_function(fn_name):
            ret_types = self.state.visited_return_types[fn_name]
        else:
            spec = Specialization(arg_types, constexprs)
            with self.visit_region():
                visitor = FunctionVisitor(fn.src, spec, base_fn.__globals__, fn.location, self.options,
                                          self.state.visited_return_types, is_kernel=False)
                visitor.visit(fn.node)
                ret_types = visitor.state.return_types
                self.state.visited_return_types[fn_name] = ret_types
```

同一个 visitor 实例（`self`）保持对 kernel 顶层的遍历，同时**另起一个 `FunctionVisitor`** 去访问子函数 AST（`is_kernel=False`），并把 `self.state.visited_return_types` 传进去——两个 visitor 共享一张表，第二次调用同名子函数时走 `has_function` 快速路径，不再重复展开。这就是 u4-l4「内联」的机制雏形。

#### 4.2.4 代码实践

**实践目标**：验证状态守卫规则，把「语法规则」与「状态字段」一一对应。

**操作步骤**（源码阅读型实践，无需运行环境）：

1. 在 [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py) 中用编辑器搜索 `state.`，统计每个字段的所有读写点。
2. 对每个字段填一张三列表：**写入点（行号）/ 读取点（行号）/ 守卫的规则**。
3. 针对 kernel 分别写出会触发三条报错的 Python 代码片段（仅写在纸面上）：
   - `def f(): def g(): pass` → 嵌套函数；
   - `for i in range(8): return` → 嵌套块 return；
   - `return 1` → kernel 返回对象。

**需要观察的现象**：三条报错的文案分别出现在 L299、L646、L652，且都经由 `raise_unsupported`/`raise`，携带 AST 节点与源码行。

**预期结果**：每条规则都能落到唯一一个状态字段的唯一一个检查点上，没有散落的重复检查——这是「状态集中管理」的直接体现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `discard_everything` 在 `visit_FunctionDef` 末尾要复位，而 `inside_function` 也在同一处复位？

**答案**：同一个 `FunctionVisitor` 实例在一次 `visit` 里只处理一个函数，本可以不复位；但复位的意义在于健壮性与复用语义——状态描述「当前这次遍历」，遍历结束后回到初始状态，外部（如 `call_jit_function`）读取 `visitor.state.return_types` 时不会误以为还在函数内部。更重要的是，如果未来同一实例被复用（或单元测试直接构造 visitor 连续 visit 多个函数），残留的 `discard_everything=True` 会让第二个函数的整段体被静默丢弃。

**练习 2**：`return_types` 和 `visited_return_types` 一个是列表一个是字典，为什么结构不同？

**答案**：`return_types` 描述**本函数**的返回值列表（可能多个返回值，顺序有意义），`visit_FunctionDef` 收尾时按顺序回填函数签名；`visited_return_types` 是**按子函数名索引**的缓存表（函数名 → 该函数的返回类型列表），用于跨函数共享、避免重复展开子函数。一个是「我的输出」，一个是「我调过的所有函数的输出备忘」。

**练习 3**：`visit_region` 用 `try/finally` 恢复三个东西（scope、插入点、return_allowed）。如果嵌套块里抛了 `UnsupportedSyntaxError`，会发生什么？

**答案**：`finally` 仍会执行，三个状态被完整恢复，然后异常继续向上传播；外层 `visit` 的 `except` 把它包装成带源码位置的 `CodegenError`（若 `capture_exceptions` 开启），最终 `_run_codegen` 的 `finally` 触发 `global_builder.teardown()` 清理 builder。资源不会泄漏，状态不会串味。

### 4.3 global_builder 与插入点

#### 4.3.1 概念说明

`GlobalBuilder` 是一个极简的容器：持有当前生效的 `ir.Builder` 和它创建的 `ir.ModuleOp`，外加一串 teardown 回调。它解决的问题是**作用域割裂**：IR 操作的创建发生在两个互不相识的地方——visitor 在 `visit_BinOp` 里要建算术操作，language 层的 `asc.add` 在自己的模块里也要建操作——它们需要同一个「当前 builder」。pyasc 的选择是模块级单例 `global_builder`，而不是把 builder 一路作为参数传递。

代价是生命周期必须有人管：单例是全局的，一次编译结束必须清空，否则下一次编译会往旧模块里续写。这个「谁建谁拆」的约定由 `jit.py` 承担。

#### 4.3.2 核心流程

```text
set_ir_builder(context)                 # _run_codegen 调用
    builder = ir.Builder(context)
    ir_module = builder.create_ModuleOp()
    builder.set_insertion_point_to_start(ir_module.get_body())
    注册 reset 回调（把 builder 置 None）
        │
        │  ← 编译期间：visitor 与 language API 随时取用
        │     get_ir_builder() / get_ir_module()
        │
teardown()                              # _run_codegen 的 finally 调用
    逆序执行回调：builder = None（ir_module 引用随实例一起被丢弃）
```

「插入点（insertion point）」是 MLIR builder 的游标：新操作总是插在游标处。整个 codegen 阶段，这个游标被反复移动——进函数入口块、进循环体、进临时块、再恢复——4.2 节的 `visit_region` 就是移动它的一种封装。

#### 4.3.3 源码精读

`GlobalBuilder` 全类 + 单例（[python/asc/language/core/utils.py:L136-L170](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L136-L170)）：

```python
class GlobalBuilder:

    def __init__(self):
        self.builder: Optional[ir.Builder] = None
        self.ir_module: Optional[ir.ModuleOp] = None
        self.teardown_callbacks: List[Callable[[], None]] = []

    def set_ir_builder(self, context: ir.Context) -> None:
        self.builder = ir.Builder(context)
        self.ir_module = self.builder.create_ModuleOp()
        self.builder.set_insertion_point_to_start(self.ir_module.get_body())

        def reset():
            self.builder = None

        self.on_teardown(reset)

    def get_ir_builder(self) -> ir.Builder:
        return self.builder

    def get_ir_module(self) -> ir.ModuleOp:
        return self.ir_module

    def on_teardown(self, callback: Callable[[], None]) -> None:
        if not callable(callback):
            raise TypeError("GlobalBuilder teardown callback must be callable")
        self.teardown_callbacks.append(callback)

    def teardown(self) -> None:
        for callback in reversed(self.teardown_callbacks):
            callback()
        self.teardown_callbacks.clear()


global_builder = GlobalBuilder()
```

值得注意的三点：

- `set_ir_builder` 一步到位建好 Builder 和 ModuleOp 并把插入点放到模块体开头，随后**注册 `reset` 回调**。teardown 采用回调列表而非硬编码清理，是为了让其他组件（如 TPipeManager，u2-l6 提过「teardown 自动复位」）也能挂接清理逻辑，且按注册的逆序执行。
- `get_ir_builder` 没有任何判空保护，返回 `None` 就让调用方自己崩——配合 `require_jit`（[python/asc/language/core/utils.py:L196-L207](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L196-L207)）在 JIT 之外调用 `asc.add` 等接口时给出友好报错：`'xxx' cannot be called without initialization of global builder`。
- 单例在模块导入时创建（`global_builder = GlobalBuilder()`），但内容为空；它「有没有货」完全取决于是否处于一次 `_run_codegen` 的 try 块中。

visitor 侧对插入点的首次设置在构造函数（已在 4.1.3 看到）：

```python
global_builder.get_ir_builder().set_insertion_point_to_start(global_builder.get_ir_module().get_body())
global_builder.get_ir_builder().set_loc(self.location.filename, self.location.line_offset, 0)
```

`set_loc` 设置的是 IR 位置的「基线」（文件名 + 起始行偏移 + 列 0）；每个节点被访问时会用 `line_offset + node.lineno` 覆盖为精确行号（见 4.4.3 的 `visit`）。

#### 4.3.4 代码实践

**实践目标**：理清 `global_builder` 的完整生命周期，画出状态变迁图。

**操作步骤**：

1. 通读 [python/asc/language/core/utils.py:L136-L170](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L136-L170) 与 [python/asc/runtime/jit.py:L184-L194](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L184-L194)。
2. 画一条时间轴，标出五个时刻 `global_builder.builder` 的值：模块导入时、`set_ir_builder` 后、`visitor.visit` 期间、`get_ir_module()` 收模块时、`teardown()` 后。
3. 追加思考：`_run_codegen` 收走 `ir.ModuleOp` 引用后执行 `teardown`，模块会被销毁吗？

**需要观察的现象**：`teardown` 的 `reset` 回调只把 `builder` 置 `None`，并未动 `ir_module` 字段。

**预期结果**：时间轴上 builder 经历 `None → Builder → None` 三态；第 3 步结论——`CompiledKernel` 流水线后续（`_run_compiler`）仍要用这个模块对象跑 Pass，Python 侧 `global_builder.ir_module` 虽仍指着它，但只要 `_run_codegen` 的返回值被 `_cache_kernel` 持有，模块就不会被回收；真正防止「下次编译续写旧模块」的是下一次 `set_ir_builder` 会创建**全新的** Builder 和 ModuleOp 覆盖旧引用。（待本地验证：可在装好 pyasc 的环境连续触发两次编译并 `id()` 两次的模块对象对比。）

#### 4.3.5 小练习与答案

**练习 1**：`teardown` 为什么按注册顺序的**逆序**执行回调？

**答案**：与编译器/解释器销毁栈帧同理——后注册的回调通常依赖先注册者建立的资源（例如某组件注册的清理需要 builder 还在，而 `reset` 恰好是最先注册的、负责消灭 builder 的回调，必须最后执行）。逆序保证依赖方先清理、被依赖方后清理。

**练习 2**：如果两次 JIT 编译之间忘了 teardown，会发生什么？

**答案**：以当前实现看，危害被「下一次 `set_ir_builder` 整体覆盖 builder/ir_module 引用」挡住了大半；但已注册的 teardown 回调会一直堆积（列表只增不减），且注册方（如 TPipeManager 这类有状态的组件）在本该复位的时机没有复位，可能把上一个 kernel 的残留状态带进下一个。`_run_codegen` 用 `try/finally` 保证 teardown 必然执行，正是杜绝这条路径。

**练习 3**：为什么 visitor 不把 `ir.Builder` 存成自己的属性 `self.builder`，而要每次经过 `global_builder` 中转？

**答案**：因为建 IR 的不止 visitor 自己。`visit_Call` 最终会调到 language 层 API（如 `asc.data_copy`），这些 API 在自己的模块里也要拿 builder 建 `create_asc_DataCopyOp` 之类的操作（u2-l5 的三段式）。若 builder 藏在 visitor 实例里，language API 就无法访问。全局单例是让「visitor 世界」和「language API 世界」共享同一游标的最简单方案——代价是 `require_jit` 必须把守「编译期之外不许调用」。

### 4.4 visit 分发：从 AST 节点到 IR 构建

#### 4.4.1 概念说明

`ast.NodeVisitor` 的分发约定是：`visit(node)` 会调用 `self.visit_<节点类名>(node)`。pyasc 覆写了总入口 `visit`，在标准分发之前加了一层「门卫 + 定位 + 异常包装」；并把 `generic_visit`（处理所有**没有**对应 `visit_*` 方法的节点）改成直接报不支持。于是整套分发的骨架是：

1. 门卫检查（`discard_everything` 短路、`FunctionDef` 位置约束）；
2. `set_loc` 更新 IR 源码位置；
3. 委托给标准库分发 `super().visit(node)`；
4. `visit_XXX` 方法体执行：先递归 `visit` 子节点拿到 Python 值或 `IRValue`，再调 builder 建 IR、或调 Python 协议方法（如 `__add__`）；
5. 意外异常包装成带源码位置的 `CodegenError`。

而「建 IR」这条腿大多数时候并不直接发生在 `visit_*` 里：二元运算走 `__add__` 等魔法方法（`PlainValue` 的运算符重载最终调 builder），函数调用走「真的用 Python 调用那个函数对象」——`visit_Call` 对非 `Function` 目标直接 `fn(*args, **kwargs)`，让 `asc.data_copy` 这类 API 在编译期被执行，其内部再用 `global_builder` 建 IR。**「JIT 编译的过程，本质是让 kernel 里的 Python 代码以 AST 驱动的方式在编译期重放一遍，重放时所有值都换成 IR 句柄」**——这是理解整个 codegen 的钥匙。

#### 4.4.2 核心流程

总入口（[python/asc/codegen/function_visitor.py:L293-L312](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L293-L312)）的执行序列：

```text
visit(node)
    ├─ node 为 None 或 discard_everything？ → 返回 None（死代码剪除）
    ├─ inside_function 且节点是 FunctionDef？ → 报错：不支持嵌套函数
    ├─ 不在函数内且节点不是 FunctionDef？    → RuntimeError：只能编译函数
    ├─ 节点带 lineno/col_offset？ → set_loc(filename, line_offset+lineno, col_offset)
    ├─ super().visit(node)        → 标准 NodeVisitor 分发到 visit_XXX
    │       └─ 没有 visit_XXX？   → generic_visit → UnsupportedSyntaxError
    └─ 异常处理：CodegenError 原样上抛；其余异常按 capture_exceptions 包装后上抛
```

`visit_XXX` 方法的两种典型形态：

- **表达式节点**（`visit_BinOp`、`visit_Call`、`visit_Name`...）：返回一个值——Python 立即数（`int`/`float`）或 `IRValue`（`PlainValue`/`GlobalAddress`/Tensor），供父节点继续组合。
- **语句节点**（`visit_Assign`、`visit_For`、`visit_FunctionDef`...）：返回 `None`，副作用是写 IR 与写 `NameScope`。

文件内 `visit_*` 方法一览（按行号排序，供检索）：

| AST 节点 | 方法 | 行号 | 一句话职责 |
| --- | --- | --- | --- |
| `FunctionDef` | `visit_FunctionDef` | [L529](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L529-L552) | 建 `FuncOp`、入口块、绑定形参、遍历函数体、补终结器 |
| `arguments` / `arg` | `visit_arguments` / `visit_arg` | [L314](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L314-L326) / [L325](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L325-L326) | 检查形参形态（无默认值/仅位置），收集形参名 |
| `Assign` / `AnnAssign` / `AugAssign` | `visit_Assign` 等 | [L359](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L359-L408) | 求值右侧，按目标类型（名字/下标/属性）落到作用域或对象 |
| `AugAssign` | `visit_AugAssign` | [L398](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L398-L408) | 改写成 `Assign(BinOp(...))` 复用 `visit_Assign` |
| `Expr` | `visit_Expr` | [L464](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L464-L465) | 表达式语句透传 |
| `Return` | `visit_Return` | [L644](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L644-L660) | 状态检查、记录返回类型、建 `func.ReturnOp` |
| `For` / `While` | `visit_For` / `visit_While` | [L479](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L479-L516) / [L692](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L692-L710) | 区分 static_range（编译期展开）与 range（建 `scf.ForOp`）；`scf.WhileOp` |
| `If` / `IfExp` | `visit_If` / `visit_IfExp` | [L554](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L554-L623) | 编译期条件直接剪枝；运行时条件建 `scf.IfOp` |
| `With` | `visit_With` | [L680](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L680-L690) | 真的调用 `__enter__`/`__exit__`，配 `nest_scope` |
| `Assert` / `Pass` | `visit_Assert` / `visit_Pass` | [L349](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L349-L357) / [L641](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L641-L642) | 断言仅限编译期值（`static_assert`）；`pass` 为空操作 |
| `Name` | `visit_Name` | [L635](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L635-L639) | Store 语境返回名字字符串，Load 语境查 `NameScope` 并 `ConstExpr.unwrap` |
| `Attribute` | `visit_Attribute` | [L410](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L410-L422) | 先 `getattr`，失败再走 `__getattrjit__` 协议 |
| `Call` | `visit_Call` | [L438](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L438-L446) | 目标是 `Function` 则内联调用；否则**真的调用**该 Python 对象 |
| `BinOp` / `UnaryOp` / `BoolOp` / `Compare` | 对应方法 | [L424](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L424-L459) 等 | 运算符节点翻译成魔法方法名后调 `apply_binary_method` |
| `Constant` | `visit_Constant` | [L461](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L461-L462) | 直接返回字面值 |
| `Subscript` / `Slice` | `visit_Subscript` / `visit_Slice` | [L665](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L665-L670) / [L662](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L662-L663) | 还原成 Python `slice`，交给对象的 `__getitem__` |
| `List` / `Tuple` | `visit_List` / `visit_Tuple` | [L632](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L632-L633) / [L672](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L672-L673) | 递归元素，组 Python 容器 |
| `FormattedValue` / `JoinedStr` | 对应方法 | [L518](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L518-L527) / [L625](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L625-L627) | f-string 仅用于编译期字符串（如报错信息） |
| `keyword` | `visit_keyword` | [L629](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L629-L630) | 返回 `(参数名, 值)` 对 |
| （无对应方法的一切节点） | `generic_visit` | [L290](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L290-L291) | 抛 `UnsupportedSyntaxError` |

注意一个容易踩的坑：**运算符节点本身（`ast.Mult`、`ast.FloorDiv`、`ast.Eq` 等）没有 `visit_*` 方法**，它们作为 `BinOp.op` / `Compare.ops` 的子节点存在，由 `visit_BinOp` 等通过查表翻译（[python/asc/codegen/function_visitor.py:L94-L113](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L94-L113) 的 `get_binary_method_name` 把 `ast.Mult` 映射为 `__mul__` 等）。用 `ast.walk` 遍历时它们会出现在节点列表里，但不会触发分发。

#### 4.4.3 源码精读

总入口 `visit`（[python/asc/codegen/function_visitor.py:L293-L312](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L293-L312)）：

```python
    def visit(self, node: Optional[ast.AST]) -> Optional[Any]:
        ...
        if hasattr(node, "lineno") and hasattr(node, "col_offset"):
            global_builder.get_ir_builder().set_loc(self.location.filename, self.location.line_offset + node.lineno,
                                                    node.col_offset)
        try:
            return super().visit(node)
        except CodegenError:
            raise
        except Exception as e:
            if self.options.capture_exceptions:
                raise CodegenError(node, self.src, f"{e.__class__.__name__}: {e}") from e
            raise
```

这段代码做了两件「基建」：**位置传播**（每个节点访问前把 IR 位置校到该节点的真实行号，注释、空行都不影响）与**异常归一**（任何底层异常——哪怕是 `AttributeError`、`TypeError` 这种纯 Python 错误——都被包成携带源码上下文的 `CodegenError`，报错信息因此能精确指到 kernel 的某一行）。

`generic_visit` 一行流（[python/asc/codegen/function_visitor.py:L290-L291](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L290-L291)）：

```python
    def generic_visit(self, node: ast.AST) -> NoReturn:
        self.raise_unsupported(node, f"{node.__class__.__name__} syntax is not supported in JIT function")
```

标准库的 `generic_visit` 本会「继续遍历子节点」，这里改成必抛错——**白名单机制**：没被显式实现的语法一律不支持，而不是默默跳过。这就是 docs/python_syntax_support.md（u4-l5 详述）背后清单的执行点。

看两个代表性方法。表达式侧 `visit_BinOp`（[python/asc/codegen/function_visitor.py:L424-L428](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L424-L428)）：

```python
    def visit_BinOp(self, node: ast.BinOp) -> Any:
        lhs = self.visit(node.left)
        rhs = self.visit(node.right)
        method_name = self.get_binary_method_name(type(node.op))
        return self.apply_binary_method(method_name, lhs, rhs)
```

它自己不建任何 IR，只把 `ast.Add` 查表成 `'__add__'`，然后调 `apply_binary_method`（[L164-L171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L164-L171)）：优先调 `lhs.__add__(rhs)`，若左侧是不支持 IR 的普通 Python 值而右侧支持，则改调右侧的反向方法 `__radd__`。真正建 IR 的代码在 `PlainValue.__add__` 里（u2-l3 讲过）。**`a + b` 在 JIT 下的语义 = 「在编译期对两个 IR 值对象做一次 Python 加法，其副作用是生成一条算术 IR」**。

语句侧的集大成者是 `visit_FunctionDef`（[python/asc/codegen/function_visitor.py:L529-L552](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L529-L552)）：

```python
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.state.inside_function = True
        arg_types = self.spec.args.values()
        builder = global_builder.get_ir_builder()
        input_ir_types = [arg_type.to_ir() for arg_type in arg_types]
        self.ir_function = builder.create_func_FuncOp(node.name, builder.get_function_type(input_ir_types))
        self.ir_function.make_aicore()
        if self.is_kernel:
            self.ir_function.make_global()
        entry = self.ir_function.add_entry_block()
        arg_names = list(self.spec.args.keys())
        self.ir_function.set_arg_names(arg_names)
        builder.set_insertion_point_to_start(entry)
        for i, (name, arg) in enumerate(self.spec.args.items()):
            value = self.get_arg_value(arg, self.ir_function.get_arg(i))
            self.scope.save(name, value)
        self.visit_statements(node.body)
        if not entry.has_terminator():
            builder.create_func_ReturnOp()
        if self.state.return_types:
            result_ir_types = [ret_type.ir_type for ret_type in self.state.return_types]
            self.ir_function.set_type(builder.get_function_type(input_ir_types, result_ir_types))
        self.state.inside_function = False
        self.state.discard_everything = False
```

七步：建 `FuncOp`（函数名来自 AST，形参类型来自 `spec`——两路输入在此汇合）→ 标记 `aicore`（Kernel 再加 `global`）→ 建入口块并设形参名 → 把每个形参按 `get_arg_value` 包装成 `GlobalAddress`/`PlainValue`/Struct 本地副本（[L265-L274](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L265-L274)，u3-l3 的四类 `ArgType` 在此兑现）存进作用域 → 逐条访问函数体 → 无终结器则补 `func.ReturnOp` → Device 函数有返回值则重设函数签名。到此，`ir.ModuleOp` 里有了第一个（Kernel 场景下唯一一个）函数，输出侧的准备完成。

配合 `NameScope`（[python/asc/codegen/name_scope.py:L45-L57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L45-L57)）：

```python
    def save(self, name: str, value: Any) -> None:
        if name not in self.local_vars:
            self.defined.add(name)
        elif name not in self.defined:
            self.redefined.add(name)
        self.local_vars[name] = value

    def lookup(self, name: str) -> Optional[Any]:
        for storage in self.local_vars, self.global_vars, self.builtins:
            val = storage.get(name, self.sentinel)
            if val is not self.sentinel:
                return val
        raise NameError(f"{name} is not defined")
```

`save` 同时维护 `defined`/`redefined` 两个集合——「同名变量在嵌套块中被重新赋值」正是 u4-l3 `compute_inout` 判定循环携带依赖的依据；`lookup` 的三级查找顺序（局部 → 全局+constexpr → 内置白名单）决定了一个名字能否被解析。内置白名单（[python/asc/codegen/name_scope.py:L13-L29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L13-L29)）只有 `dict/float/int/isinstance/issubclass/len/list/range/repr/str/tuple/type` 等 12 个名字——`print`、`open` 都不在其中，kernel 里写了就报 `NameError`。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：用 Python 标准库 `ast` 模块 dump 出 Add 示例核函数的 AST 树，自动生成「AST 节点类型 → visitor 方法」对照表，验证每个节点都有（或明确没有）归宿。

**操作步骤**：

1. 在仓库根目录新建脚本 `ast_map.py`（**示例代码**，仅用标准库，不需要安装 pyasc、不需要 NPU）：

```python
import ast
from collections import Counter

# 1. 解析 add.py，定位核函数 vadd_kernel
source = open("examples/01_add/add.py").read()
tree = ast.parse(source)
kernel = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "vadd_kernel")

# 2. 从 function_visitor.py 源码收集全部 visit_* 方法名（避免导入 asc 包）
fv_src = open("python/asc/codegen/function_visitor.py").read()
fv_tree = ast.parse(fv_src)
visitor_cls = next(n for n in fv_tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "FunctionVisitor")
methods = {m.name: m.lineno for m in visitor_cls.body
           if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
           and (m.name.startswith("visit_") or m.name == "generic_visit")}

# 3. 遍历核函数 AST，建立对照表
rows = Counter()
for node in ast.walk(kernel):
    kind = type(node).__name__
    rows[kind] += 1
print(f"{'AST 节点类型':<18}{'数量':>4}  {'visitor 方法':<24}{'定义行号':>6}  支持?")
for kind, count in sorted(rows.items(), key=lambda kv: -kv[1]):
    m = f"visit_{kind}"
    if m in methods:
        print(f"{kind:<18}{count:>4}  {m:<24}{methods[m]:>6}  是")
    elif kind in ("Load", "Store"):  # 语境标记，不是独立分发节点
        print(f"{kind:<18}{count:>4}  {'(ctx, 不单独分发)':<24}{'-':>6}  -")
    else:
        print(f"{kind:<18}{count:>4}  {'generic_visit':<24}{methods['generic_visit']:>6}  否!")
```

2. 运行 `python3 ast_map.py`。
3. 对表中每个「是」的节点，打开 [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py) 跳到打印出的行号，读方法体，确认它如何处理该节点。
4. 挑一个 kernel 里的真实语句，例如 [examples/01_add/add.py:L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L31) 的 `offset = asc.get_block_idx() * block_length`，写出它的分发链。

**需要观察的现象**：所有节点要么命中某个 `visit_*`，要么属于语境标记（`Load`/`Store`）、运算符子节点（`Mult`/`FloorDiv`），**不应**出现落到 `generic_visit` 的类型——因为 add.py 的 kernel 用的都是受支持语法。

**预期结果**：对照表大致如下（节点集合以实际运行为准，此处为按源码推得的参考答案）：

| AST 节点类型 | visitor 方法 | 行号 | 备注 |
| --- | --- | --- | --- |
| `FunctionDef` | `visit_FunctionDef` | L529 | 遍历起点，建 `FuncOp` |
| `arguments` / `arg` | `visit_arguments` / `visit_arg` | L314 / L325 | 形参检查与收集 |
| `Assign` | `visit_Assign` | L359 | `offset = ...` 等全部赋值 |
| `AnnAssign`（本例无） | `visit_AnnAssign` | L346 | 转发 `visit_Assign` |
| `Expr` | `visit_Expr` | L464 | `asc.data_copy(...)` 这类表达式语句 |
| `For` | `visit_For` | L479 | L49 的 `for i in range(...)` |
| `Name` | `visit_Name` | L635 | 变量读写（`ctx` 分 Load/Store） |
| `Attribute` | `visit_Attribute` | L410 | `asc.get_block_idx`、`asc.TPosition.VECIN` |
| `Call` | `visit_Call` | L438 | 一切函数/方法调用 |
| `BinOp` | `visit_BinOp` | L424 | L31 的 `*`、L39 的 `//` |
| `Constant` | `visit_Constant` | L461 | 整数字面量（0、2 等） |
| `Subscript` | `visit_Subscript` | L665 | `x_local[buf_id * tile_length:]` |
| `Slice` | `visit_Slice` | L662 | 上例的下标切片 |
| `Mult` / `FloorDiv` / `Mod` | （无 visit_*，查表翻译） | L94-L113 | 运算符节点，由 `get_binary_method_name` 处理 |
| `Load` / `Store` | （不分发） | — | 语境标记，`visit_Name` 内部判断 |

第 4 步 `offset = asc.get_block_idx() * block_length` 的分发链参考：

```text
Assign
 ├─ value: BinOp(Mult)
 │    ├─ left: Call(Attribute(Name'asc'.get_block_idx))
 │    │    └─ visit_Call → fn 是 asc.get_block_idx 对象 → 真调用
 │    │        → 返回 PlainValue（IR 值）
 │    └─ right: Name'block_length' (Load)
 │         └─ visit_Name → lookup → PlainValue（形参，IR 值）
 │    → get_binary_method_name(ast.Mult) = '__add__' 的同族 '__mul__'
 │    → apply_binary_method('__mul__', lhs, rhs) → PlainValue.__mul__ → 生成乘法 IR
 └─ target: Name'offset' (Store)
      └─ visit_Name 返回字符串 'offset' → scope.save('offset', 乘积 IRValue)
```

#### 4.4.5 小练习与答案

**练习 1**：`visit_Call` 里 `fn = self.visit(node.func)` 拿到的可能是什么？`visit_Name` 在这里返回的为什么不是 IR 值？

**答案**：`node.func` 通常是 `Attribute`（如 `asc.data_copy`）或裸 `Name`（如直接导入的 `data_copy`）。`visit_Attribute` 对 `asc.data_copy` 先 `visit(Name'asc')`——`asc` 是 kernel 的全局变量，`lookup` 返回 **`asc` 模块对象本身**（不是 IR 值），再 `getattr` 拿到 API 函数对象。于是 `visit_Call` 的 `fn` 是真正的 Python 函数，`fn(*args, **kwargs)` 让它在编译期被真实执行、内部经 `global_builder` 建 IR。名字查到什么取决于作用域里存的是什么——模块、函数、`IRValue` 都可能。

**练习 2**：为什么 `generic_visit` 直接抛错是「白名单」，而标准库默认行为是「继续遍历子节点」？这对写 kernel 的人意味着什么？

**答案**：标准库默认允许未知节点静默通过，对「只读分析」是合理的；但 codegen 必须保证**每条语句都有确定的 IR 对应**，静默跳过会导致生成缺操作的 kernel、在设备上产生错误结果。改成必抛错后，未支持语法在编译期即刻暴露（带行号），而不是运行期算错。对写 kernel 的人意味着：语法边界就是 `visit_*` 方法的集合，遇到报错时去 function_visitor.py 搜 `visit_节点名` 即可判断是「不支持」还是「实现有 bug」。

**练习 3**：`visit_Assign` 里为什么先 `rhs = self.visit(node.value)` 再处理 target？如果把顺序反过来会怎样？

**答案**：先求右值是因为处理目标可能用到目标的**旧值**或对象本身（下标赋值 `base[...] = v` 要先 `visit` 出 `base` 对象，属性赋值同理），而右值求值不应受目标处理影响；更重要的是 Python 语义就是「先求值右侧」。顺序反过来的典型问题：`x = x + 1` 中若先按 Store 处理 `x`（返回名字字符串并存表），再求右值时 `x` 查到的可能已是未完成的绑定状态。当前实现里 Store 语境的 `visit_Name` 只返回名字字符串不查表（[L635-L639](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L635-L639)），配合「先右后左」保证了正确性。

## 5. 综合实践

**任务：给「Add 核函数的一次编译」写出完整的分发审计报告。**

1. **准备**：完成 4.4.4 的 `ast_map.py`，拿到 `vadd_kernel`（[examples/01_add/add.py:L28-L69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L28-L69)）的节点对照表。
2. **补链路**：在对照表基础上，为下面三行各写一条「AST → visit 方法 → IR/作用域副作用」的三栏记录：
   - L35 `x_gm.set_global_buffer(x + offset, block_length)`（提示：`x + offset` 中 `x` 是 `GlobalAddress` 形参，其 `__add__` 是指针偏移语义——u2-l3）；
   - L49 `for i in range(TILE_NUM * BUFFER_NUM):`（提示：走 `visit_For` 的哪条分支？`TILE_NUM * BUFFER_NUM` 是编译期常量，`_range` 构造时是否物化成 IR 由 `visit_For` 决定——预告 u4-l3）；
   - L53 `asc.data_copy(x_local[buf_id * tile_length:], x_gm[i * tile_length:], tile_length)`（提示：两个切片各自触发 `Subscript`→`Slice`→`__getitem__`，u2-l2 讲过生成 subindex 节点）。
3. **验证状态机**：在纸上对 L49-L69 的循环体标出 `visit_region` 进入/退出时 `return_allowed` 的值变化，并回答：循环体里写 `return` 为什么必错？写出对应报错文案。
4. **复盘输入输出**：对照 [python/asc/runtime/jit.py:L184-L194](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L184-L194)，写一段话说明：`vadd_kernel` 的 4 个形参如何经 `Specialization` → `visit_FunctionDef` 的 `get_arg_value` → `scope.save` 变成函数体内可用的 `GlobalAddress`×3 + `PlainValue`×1（对应 u3-l3 的 `PointerArgType`/`PlainArgType`）。

**验收标准**：报告能仅凭源码回答「kernel 里任意一行 Python 在编译期发生了什么」，且每条结论都带 `文件:行号` 引用。有环境的读者可加做实证：安装 pyasc 后设置 `PYASC_DUMP_PATH` 运行 `python3 examples/01_add/add.py -r Model`，对照导出的 `codegen.mlir` 检查你推演的 IR 操作是否真实出现（无环境则标注「待本地验证」）。

## 6. 本讲小结

- `FunctionVisitor` 的输入全部有出处：源码行/AST/位置来自装饰期捕获的 `Function` 基类，参数类型表来自调用期构造的 `Specialization`；输出不靠返回值，而是写进 `global_builder` 持有的 `ir.ModuleOp`，由 `_run_codegen` 收走。
- `VisitorState` 用五个字段集中管理位置相关规则：`inside_function` 限定「有且仅有一个 FunctionDef」，`return_allowed`（由 `visit_region` 维护）禁止嵌套块 return，`discard_everything` 剪除 return 后的死代码，`return_types`/`visited_return_types` 服务返回值签名与子函数缓存。
- `global_builder` 是 visitor 世界与 language API 世界共享同一 `ir.Builder` 游标的单例桥；生命周期严格由 `_run_codegen` 的 `set_ir_builder`/`teardown` 夹住，`require_jit` 负责把编译期外的调用挡在门外。
- 分发骨架 = 覆写的总入口 `visit`（门卫 + set_loc + 异常包装）+ 标准 `NodeVisitor` 分发 + 改为必抛错的 `generic_visit`（语法白名单）；运算符节点不参与分发，由 `get_binary_method_name` 等查表翻译成魔法方法。
- codegen 的本质是「AST 驱动的编译期重放」：`visit_Call` 真调用 language API、`visit_BinOp` 真调用 `IRValue.__add__`，重放中所有值都是 IR 句柄或编译期常量，副作用即 IR 的生成。
- `NameScope` 以「局部 → 全局+ConstExpr → 内置白名单」三级查找解析名字，`defined`/`redefined` 集合为下一讲的循环携带依赖分析埋下伏笔。

## 7. 下一步学习建议

下一讲 **u4-l2「语句与表达式：赋值、运算符重载与作用域」**深入本讲划出的两块骨架：`visit_Assign` 的目标三形态（名字/下标/属性）与 `apply_binary_method` 的双向方法协商、`NameScope.inherit` 支撑的变量遮蔽。建议先自己重读 [python/asc/codegen/function_visitor.py:L359-L428](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L359-L428) 与 [python/asc/codegen/name_scope.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py)。若想先夯实 IR 值一侧，可回看 u2-l3；想看 `scf.ForOp`、`scf.IfOp` 如何拼装，则预习 u4-l3 并对照本讲 `visit_region`/`compute_inout` 的「临时块构建再搬移」手法。
