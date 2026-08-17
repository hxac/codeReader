# u4-l4 函数调用与 Device 子函数的内联

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `visit_Call` 的分岔逻辑：为什么 `asc.add(...)` 这类调用是「编译期直接执行」，而 `copy_in(...)` 这类调用走 `call_jit_function` 的内联路径。
2. 逐步描述一次子函数调用的完整处理流程：实参绑定 → ConstExpr 分流 → `IRArgType` 包装 → 递归访问子函数 AST → 生成 `func.CallOp`。
3. 解释「内联」在 pyasc 中的两层含义：IR 层的「同模块共处」与 Ascend C 发射层的 `always_inline`。
4. 列出 `is_kernel` 标志带来的三种差异（符号可见性、return 规则、编译选项作用域），并能解释为什么给 Device 函数传 jit 编译参数是无效的。

## 2. 前置知识

本讲建立在前几讲的认知之上，先快速回顾：

- **编译期重放**（u4-l1）：JIT 下 kernel 函数体不是被 Python 执行，而是被 `FunctionVisitor` 逐个 AST 节点「重放」，重放过程中向 `global_builder` 持有的 `ir.ModuleOp` 追加 IR。
- **NameScope 三级查找**（u4-l2）：`visit_Name` 解析一个名字时，按 local → global（含 ConstExpr 实参）→ builtins 白名单的顺序查找。`copy_in` 这类模块级函数名会在 global 一级被找到，取回 `@asc.jit` 包装后的 `JITFunction` 对象。
- **参数四分类**（u3-l3）：`Specialization` 的参数类型表里有 `PlainArgType`（定宽标量）、`PointerArgType`（设备指针）、`StructArgType`（结构体）和 `IRArgType`（设备侧 IR 值透传）。本讲会看到 `IRArgType` 唯一的用武之地——子函数互调。
- **Kernel 与 Device 侧执行函数**（u1-l4）：用 `kernel[核数, 流](...)` 中括号启动的是 Kernel；被其他 jit 函数用小括号调用的是 Device 侧执行函数。02_add_framework 里 `vadd_kernel` 是前者，`copy_in/compute/copy_out` 是后者。
- **TQue 不记录 dtype**（u2-l6）：这正是 `compute` 函数要多收一个 `z_gm` 参数的真实原因（示例源码里有注释点明）。

一个容易混淆的词先说清楚：本讲标题里的「内联」**不是** MLIR 的 inlining Pass。pyasc 前端没有做任何 IR 级函数内联；「内联」发生在最后两步——Ascend C 发射层给 Device 函数打上 `always_inline` 前缀，由毕昇编译器完成真正的内联。细节见 4.2。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py) | 本讲主角：`visit_Call`（调用分岔口）、`call_jit_function`（子函数内联核心）、`visit_FunctionDef`（is_kernel 差异落点）、`visit_Return`（返回值规则） |
| [examples/02_add_framework/add_framework.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py) | 分析样本：一个 Kernel 加三个 Device 子函数的标准结构 |
| [python/asc/codegen/function.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py) | `Function` 基类：`split_args` 在子函数调用处被复用，按**子函数自己的标注**分流参数 |
| [python/asc/codegen/specialization.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py) | `IRArgType`：设备侧 IR 值跨越函数边界的类型包装 |
| [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py) | `JITFunction`：装饰器选项存进 `default_options`，只在 Kernel 启动路径被读取；`_run_codegen` 以 `is_kernel=True` 创建 visitor |
| [lib/Target/AscendC/External/Func.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp) | Ascend C 发射层：Kernel 发射为 `extern "C" __global__`，Device 函数发射为 `__inline__ always_inline` |
| [lib/Dialect/Asc/Transforms/PrivatizeFunc.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/PrivatizeFunc.cpp) | 模块级 Pass：没有 `asc.global` 属性的函数统一设为 private |
| [include/ascir/Dialect/Asc/Transforms/Passes.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td) | Pass 声明：确认 `InsertSync` 是函数级 Pass、`PrivatizeFunc` 是模块级 Pass |
| [python/src/IR.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp) | pybind 绑定：`ModuleOp.has_function`（按名查重）的实现 |

## 4. 核心概念与源码讲解

### 4.1 visit_Call：编译期函数调用的分岔口

#### 4.1.1 概念说明

JIT 重放过程中遇到的每一个 `f(x)` 形态的 AST 节点（`ast.Call`）都会进入 `visit_Call`。此时「调用」这个词有两层完全不同的含义：

1. **编译期直接执行**：被调对象是普通 Python 函数或方法时，`visit_Call` 在**编译期**真的调用它（`fn(*args, **kwargs)`）。`asc.add`、`asc.data_copy`、`in_queue_x.alloc_tensor(...)`、`pipe.init_buffer(...)` 全部走这条路——它们是 language 层的普通 Python 函数/方法，编译期执行它们，副作用就是向 IR 追加操作。这是 pyasc「用 Python 函数包装 IR 构建」思想的具体落点。
2. **子函数内联**：被调对象是 `Function` 实例（即被 `@asc.jit` 修饰后得到的 `JITFunction`）时，转入 `call_jit_function`，把对方的函数体也编译进当前模块。`copy_in/compute/copy_out` 的调用走这条路。

为什么 `fn` 会是 `Function` 实例？因为 `copy_in` 是模块级名字，`visit(node.func)` 解析 `ast.Name` 时经 NameScope 的 global 一级查到 `JITFunction` 对象；`JITFunction` 继承自 `Function`（[python/asc/runtime/jit.py:30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L30)），所以 `isinstance(fn, Function)` 成立。

还有一类调用根本不进 `visit_Call`：`for i in range(...)` 中的 `range(...)` 被 `visit_For` 提前截获，由 `parse_iterator` 手工拆解（[python/asc/codegen/function_visitor.py:467-471](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L467-L471)），目的是区分 `range` 与 `static_range` 两种循环语义（u4-l3）。最后，Host 侧的 `vadd_kernel[8, stream](...)` 是普通 Python 语法执行，与 FunctionVisitor 无关。

#### 4.1.2 核心流程

```text
visit_Call(node)
├── fn = visit(node.func)          # 解析出被调对象（经 NameScope）
├── 可调用检查：not callable(fn) → 报不支持
├── args = [visit(a) for a in node.args]        # 先求值所有实参（产生 IR 或立即数）
├── kwargs = {visit(k) ...}
├── isinstance(fn, Function)?
│     ├── 是  → call_jit_function(fn, args, kwargs)   # 子函数内联（4.2）
│     └── 否  → fn(*args, **kwargs)                   # 编译期直接执行
└── 返回调用结果（IRValue 或 Python 值）
```

注意实参求值顺序：先递归 `visit` 每个实参表达式（这一步可能已经生成 IR），再判断分岔。所以 `copy_in(i, x_gm, y_gm, ...)` 里的 `i`（循环归纳变量）在进入 `call_jit_function` 之前就已经是一个 `PlainValue`。

#### 4.1.3 源码精读

分岔口的实现只有 9 行：

[python/asc/codegen/function_visitor.py:438-446](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L438-L446)

```python
def visit_Call(self, node: ast.Call) -> Optional[Any]:
    fn = self.visit(node.func)
    if not callable(fn):
        self.raise_unsupported(node, f"{fn.__class__.__name__} instance is not callable")
    args = [self.visit(arg) for arg in node.args]
    kwargs = dict(self.visit(keyword) for keyword in node.keywords)
    if isinstance(fn, Function):
        return self.call_jit_function(fn, args, kwargs)
    return fn(*args, **kwargs)
```

- 第 2 行解析被调对象；第 3-4 行做可调用检查（比如对一个小括号跟在 `int` 字面量后面这类语法报错）。
- 第 5-6 行求值实参与关键字实参。
- 第 7-9 行就是分岔口：`Function` 实例 → 内联路径；否则编译期直接执行。

对照示例中的三个调用点（[examples/02_add_framework/add_framework.py:45-48](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L45-L48)）：

```python
for i in range(TILE_NUM * BUFFER_NUM):
    copy_in(i, x_gm, y_gm, in_queue_x, in_queue_y, tile_length)
    compute(z_gm, in_queue_x, in_queue_y, out_queue_z, tile_length)
    copy_out(i, z_gm, out_queue_z, tile_length)
```

- `range(...)`：被 `visit_For` 截获，不进 `visit_Call`。
- `copy_in/compute/copy_out`：`fn` 是 `JITFunction`（`Function` 子类），走 `call_jit_function`。
- 进入子函数体内后，`alloc_tensor/enque/asc.data_copy/asc.add` 等：`fn` 是普通函数/绑定方法，编译期直接执行。

#### 4.1.4 代码实践

**实践目标**：用纯 Python 的 `ast` 模块枚举示例中的所有调用点，并按 `visit_Call` 的分岔规则给它们分类——不安装 pyasc 也能完成。

**操作步骤**：

1. 在仓库根目录新建脚本（示例代码，可放任意位置）：

```python
# classify_calls.py（示例代码）
import ast

src = open("examples/02_add_framework/add_framework.py").read()
tree = ast.parse(src)

for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        kind = "JIT 子函数调用" if name in ("copy_in", "compute", "copy_out") \
            else "for-range（被 visit_For 截获）" if name == "range" \
            else "编译期直接执行"
        print(f"L{node.lineno}: {name}(...)  ->  {kind}")
```

2. 运行 `python3 classify_calls.py`。

**需要观察的现象**：输出里三类调用各有哪些；特别注意 `vadd_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, ...)` 这一行不会被 `ast.walk` 报告为 `vadd_kernel` 的调用——它的 `node.func` 是一个 `ast.Subscript`（中括号语法），`ast.unparse` 会打印成 `vadd_kernel[...](...)`。

**预期结果**：`copy_in/compute/copy_out` 各出现一次（都在 for 循环体内）；`range` 出现在 For 语句的 iter 里；`asc.get_block_idx`、`asc.GlobalTensor`（构造调用）、`x_gm.set_global_buffer`、`pipe.init_buffer`、`alloc_tensor/enque/deque/free_tensor`、`asc.data_copy`、`asc.add` 等大量调用归入「编译期直接执行」；`vadd_kernel[...]` 的调用属于 Host 侧执行，与本 visitor 无关。

#### 4.1.5 小练习与答案

**练习 1**：如果把 kernel 里的 `compute(...)` 换成一个未被 `@asc.jit` 修饰的普通 Python 函数 `my_compute(...)`，会发生什么？

**答案**：`visit_Call` 走 `fn(*args, **kwargs)` 分支，在**编译期**直接执行该函数。函数体里的 `in_queue_x.deque(...)` 等调用仍会正常向 IR 追加操作（它们自身是编译期执行的普通调用），效果上等价于把函数体展开进调用处；但函数体内出现的 Python 语句（如 `for`、`if`）不会走 visitor 的控制流翻译，而是按普通 Python 语义执行——只有当它们恰好能在编译期求值时才碰巧可用。pyasc 用 `@asc.jit` 标记子函数，正是为了让其函数体被完整地按 JIT 语义（控制流 IR 化、ConstExpr 解包、作用域管理）处理。

**练习 2**：`asc.add(z_local, x_local, y_local, tile_length)` 里的 `asc.add` 是什么时候被真正执行的？它和 `compute` 函数的执行时机有何区别？

**答案**：`asc.add` 在**编译期**被立即执行（`visit_Call` 的直接执行分支），执行结果是向 IR 追加一个 Add 操作；`compute` 则不被执行，它的 AST 被递归访问、编译成模块内一个新的 `func.FuncOp`，调用点只留下一条 `func.call`。一句话：`asc.add` 是「盖图的章」，`compute` 是「另画一张图再连一条边」。

**练习 3**：为什么 `for i in range(...)` 的 `range(...)` 不需要经过 `visit_Call`？

**答案**：因为 `range` 与 `static_range` 决定的是循环的两种完全不同的编译策略（生成 `scf.for` 还是编译期展开），`visit_For` 必须先于求值拿到迭代器函数对象本身做身份比较（`func is static_range` / `func is range`），所以用 `parse_iterator` 手工拆解 `node.iter`，绕过了通用的 `visit_Call` 分发（[python/asc/codegen/function_visitor.py:467-497](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L467-L497)）。

### 4.2 子函数内联：从递归访问到 always_inline

#### 4.2.1 概念说明

「Device 子函数被内联」在 pyasc 里分两层，理解这两层是本讲的核心：

1. **IR 生成层——同模块共处**：`call_jit_function` 为子函数新建一个 `FunctionVisitor(is_kernel=False)`，递归访问子函数的 AST。子函数成为一个普通的 `func.FuncOp`，与 Kernel 函数位于**同一个** `ir.ModuleOp` 里；调用点生成一条真正的 `func.CallOp`。注意：这一步没有把函数体复制进调用处——IR 里函数边界仍然存在。
2. **代码生成层——always_inline**：翻译成 Ascend C 时，发射层检查函数有没有 `asc.global` 属性：没有（即 Device 函数）就给函数打上 `__inline__ __attribute__((always_inline))` 前缀；最终的真内联由毕昇编译器在编译 `.cce` 源文件时完成。

这个设计的取舍在于：

- **前端不做 MLIR 内联**。跨函数的 SSA 改写、循环携带依赖的合并都非常复杂；交给编译器后端做，前端只需保证语义正确。
- **Pass 可以按函数为粒度工作**。`InsertSync` 等同步相关 Pass 声明为 `func::FuncOp` 级 Pass（见 4.2.3），Device 函数保持独立 FuncOp，使每个函数体内的同步分析有清晰边界。
- **代价**：Device 函数不能被单独编译和缓存——它随 Kernel 所在模块整体走一次编译；此外存在「同名函数只按名字去重」的陷阱（见下文）。

#### 4.2.2 核心流程

`call_jit_function` 的完整流程（七步）：

```text
call_jit_function(fn, args, kwargs)
├── 1. base_fn = fn.fn                       # 取被包装的原生 Python 函数
├── 2. get_call_args：inspect.signature 绑定实参（apply_defaults）
├── 3. split_args：按【子函数自己的标注】分流
│        ConstExpr 标注 → constexprs（编译期）
│        其余           → runtime_args（进 IR 签名）
├── 4. runtime_args 的值逐个检查：是 IRValue 则直通，
│        否则 materialize_ir_value 物化成 IR；再包装为 IRArgType
├── 5. 模块里已有同名函数？
│        是 → 复用 visited_return_types 缓存的返回类型（跳过重新访问）
│        否 → 在 visit_region 保护下新建 FunctionVisitor(is_kernel=False)
│              递归 visit(fn.node)，函数体落在模块体开头
├── 6. 在调用点创建 func.CallOp（operands = 各 runtime 实参的 IR 值）
└── 7. 包装返回值：0 个 → None；1 个 → 单个 IRValue；多个 → 列表
```

结合 02_add_framework 的 `copy_in(i, x_gm, y_gm, in_queue_x, in_queue_y, tile_length)` 看参数如何跨界：

| 子函数形参 | 标注 | 分流结果 | 跨界方式 |
| --- | --- | --- | --- |
| `i` | `int` | 运行时 | 循环归纳变量，调用点已是 `PlainValue`（IRValue 直通）→ 成为 IR 参数 |
| `x_gm` / `y_gm` | `asc.GlobalAddress` | 运行时 | `GlobalAddress` 对象（IRValue 子类）→ 成为 IR 参数 |
| `in_queue_x` / `in_queue_y` | `asc.TQue` | 运行时 | `TQue` 对象（`TQueBind` → `IRValue`）→ 成为 IR 参数 |
| `tile_length` | `asc.ConstExpr[int]` | 编译期 | 值烘进子函数的 `Specialization.constexprs`，进入子 visitor 的 NameScope，**不进 IR 签名** |

运行时参数的类型统一包装成 `IRArgType(py_type, ir_type)`：py_type 记住「这个 IR 值该还原成哪个 language 层类」，ir_type 直接取实参的实际 IR 类型。在子函数一侧，`get_arg_value` 对 `IRArgType` 执行 `py_type.from_ir(handle)`，把 IR 句柄重新还原成 `TQue`/`GlobalTensor`/`PlainValue` 对象——**调用方把对象拆成 IR，被调方由 IR 重组对象**。

ConstExpr 的传递路径也值得注意：kernel 作用域里的 `tile_length` 本身是 ConstExpr 形参，`visit_Name` 访问时已 `ConstExpr.unwrap` 成 Python int（u4-l2）；进入 `split_args` 后又按子函数标注重新包成 `ConstExpr`。值在两层函数间以「纯 Python 值」的形态穿梭，全程不产生 IR。

#### 4.2.3 源码精读

**核心函数 `call_jit_function`**：

[python/asc/codegen/function_visitor.py:179-211](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L179-L211)

```python
def call_jit_function(self, fn: Function, args: Tuple[Any], kwargs: Dict[str, Any]) -> Optional[Any]:
    base_fn = fn.fn
    call_args = self.get_call_args(base_fn, *args, **kwargs)
    annotations = get_annotations(base_fn)
    runtime_args, constexprs = Function.split_args(call_args, annotations)
    arg_values: Dict[str, IRValue] = {}
    for name, value in runtime_args.items():
        if isinstance(value, IRValue):
            arg_values[name] = value
        else:
            arg_values[name] = materialize_ir_value(value)
    arg_types = {name: IRArgType(type(value), value.to_ir().get_type()) for name, value in arg_values.items()}
    fn_name = fn.node.name
    ret_types = []
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
    ir_operands = [value.to_ir() for value in arg_values.values()]
    op = global_builder.get_ir_builder().create_func_CallOp(fn.node.name, ir_operands,
                                                            [ret_type.ir_type for ret_type in ret_types])
    ...
```

逐段说明：

- 第 2-4 行：拿原生函数、绑定实参、按**子函数自己的标注**（不是 Kernel 的标注）做 `split_args` 分流——`split_args` 的实现见 [python/asc/codegen/function.py:119-132](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L119-L132)。
- 第 5-9 行：运行时实参必须是（或被物化为）IRValue；随后全部包成 `IRArgType`。`IRArgType` 的构造函数里有一道硬校验：

[python/asc/codegen/specialization.py:44-53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py#L44-L53)

```python
class IRArgType(BaseArgType):
    def __init__(self, py_type: Type[IRValue], ir_type: ir.Type):
        if not issubclass(py_type, IRValue):
            raise TypeError("Only IRValue can be passed between JIT functions")
```

  错误信息说得直白：**只有 IRValue 能在 JIT 函数之间传递**。你不能把一个 Python list 或 str 传给子函数（它们根本不是 IRValue，物化也会失败）；想传「编译期已知的值」请走 ConstExpr 形参。

- 第 12-19 行：**按名去重**。`has_function(fn_name)` 的实现在 pybind 绑定层，按符号名在模块里查找（[python/src/IR.cpp:560-568](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L560-L568)）。命中则跳过重新访问，返回类型从 `visited_return_types`（一个跨 visitor 共享的字典，经构造函数传入）读取。由此得到一个重要事实：**同一 kernel 内，同名子函数的函数体只生成一次**——即使第二次调用传了不同的 ConstExpr 实参，也只会复用第一次生成的函数体，因为 ConstExpr 根本不随 `CallOp` 传递。实践中应避免对同名子函数做不同的 ConstExpr 特化。
- 第 15-20 行：`visit_region()` 上下文管理器先保存当前 NameScope 与 IR 插入点（[python/asc/codegen/function_visitor.py:328-340](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L328-L340)），保证子 visitor 干完活后，父 visitor 能原样回到调用点继续。子 visitor 的构造函数会把插入点设到**模块体开头**（[python/asc/codegen/function_visitor.py:86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L86)），所以 dump 出的 `codegen.mlir` 中 Device 函数通常排在 Kernel 函数之前。
- 第 21-23 行：回到调用点创建 `func.CallOp`——IR 中函数边界依然存在。

**子函数如何接收参数**：子 visitor 的 `visit_FunctionDef` 用 `spec.args.values()` 的 `to_ir()`（对 `IRArgType` 就是实参的实际 IR 类型）拼出函数签名，再用 `get_arg_value` 把每个 IR 参数还原成 language 层对象存入 NameScope：

[python/asc/codegen/function_visitor.py:265-274](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L265-L274)

```python
def get_arg_value(self, arg_type: BaseArgType, handle: IRHandle) -> IRValue:
    if isinstance(arg_type, PointerArgType):
        return GlobalAddress(handle=handle, dtype=arg_type.dtype)
    if isinstance(arg_type, PlainArgType):
        return PlainValue(handle=handle, dtype=arg_type.dtype)
    if isinstance(arg_type, IRArgType):
        return arg_type.py_type.from_ir(handle)      # 子函数互调：IR → 还原为 TQue/GlobalTensor 等
    ...
```

第 6-7 行就是 `IRArgType` 的专属通道——`TQue.from_ir(handle)` 重新给出一个绑定同一 IR 值的 `TQue` 对象，子函数体内就能继续写 `in_queue_x.deque(...)`。这也解释了示例里 `compute` 为什么要收 `z_gm`（[examples/02_add_framework/add_framework.py:62-65](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L62-L65)）：`TQue` 不记录 dtype，`deque` 需要调用方显式给出 dtype，于是把 `z_gm` 传进来「借」它的 dtype（源码注释原文：`"z_gm" is passed here to obtain dtype`）。

**发射层的 always_inline**：翻译成 Ascend C 时，每个 `func.FuncOp` 走同一段发射代码，关键分岔在有没有 `asc.global` 属性：

[lib/Target/AscendC/External/Func.cpp:63-77](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L63-L77)

```cpp
bool isMainFunction = functionOp->hasAttr(ascendc::attr::global);
...
os << (isMainFunction ? "extern \"C\"  __global__ " : "__inline__ __attribute__((always_inline)) ");
os << "__aicore__ ";
```

- Kernel（有 `asc.global`）→ `extern "C" __global__ __aicore__ ...`：全局符号，能被 aclrt 运行时按名字加载启动。
- Device 函数（无 `asc.global`）→ `__inline__ __attribute__((always_inline)) __aicore__ ...`：强制内联提示，毕昇编译器会把它内联进 Kernel，函数边界在最终二进制中消失，不产生真实调用开销。

而调用点的 `func.CallOp` 发射成普通的 C 函数调用（[lib/Target/AscendC/External/Func.cpp:23-36](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L23-L36)）：`os << callOp.getCallee() << "(" ... << ")"`。

**符号私有化**：Pass 流水线里的 `PrivatizeFunc`（模块级 Pass，[include/ascir/Dialect/Asc/Transforms/Passes.td:85-87](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L85-L87)）把所有没有 `asc.global` 属性的函数设为 private：

[lib/Dialect/Asc/Transforms/PrivatizeFunc.cpp:31-37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/PrivatizeFunc.cpp#L31-L37)

```cpp
getOperation().walk([](func::FuncOp op) {
    if (!op->hasAttrOfType<UnitAttr>(ascendc::attr::global)) {
        op.setPrivate();          // Device 函数：模块内私有
    } else if (!op.isDeclaration()) {
        op.setPublic();           // Kernel：公开符号，供运行时加载
    }
});
```

**同步 Pass 与函数边界**：`InsertSync` 声明为函数级 Pass（`Pass<"ascendc-insert-sync", "func::FuncOp">`，[include/ascir/Dialect/Asc/Transforms/Passes.td:59-61](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L59-L61)），`runOnOperation` 拿到的操作数就是单个 `func::FuncOp`（[lib/Dialect/Asc/Transforms/InsertSync.cpp:174-190](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/InsertSync.cpp#L174-L190)）。Device 函数作为独立 FuncOp，会被这个 Pass 与 Kernel 一视同仁地逐函数处理——这是「不在前端做内联」的第二个好处：同步分析的自然边界恰好就是用户写的函数边界。

#### 4.2.4 代码实践

**实践目标**：用 `PYASC_DUMP_PATH` 导出 02_add_framework 的中间产物，亲眼确认「四个函数同住一个模块、调用点是 func.call、Ascend C 里是 always_inline」。

**操作步骤**：

1. 按 u1-l2 完成安装后，在仓库根目录执行：

```bash
mkdir -p /tmp/pyasc_dump_u4l4
PYASC_DUMP_PATH=/tmp/pyasc_dump_u4l4 python3 examples/02_add_framework/add_framework.py -r Model
```

2. 打开 `/tmp/pyasc_dump_u4l4/codegen.mlir`（Pass 前的 IR），搜索 `copy_in`、`compute`、`copy_out`、`vadd_kernel` 四个名字。
3. 在 Kernel 函数体内找到对 `copy_in` 等的调用行（`call @copy_in(...)` 形态）。
4. 打开 `ascendc.cpp`，搜索 `always_inline` 与 `__global__`，对比两类函数的前缀。

**需要观察的现象**：

- `codegen.mlir` 中模块里有 4 个函数定义；Device 函数排在 Kernel 之前（子 visitor 把插入点设在模块体开头）；只有 `vadd_kernel` 带全局属性（Pass 后的 `ascir.mlir` 中 Device 函数标记为 `private`）。
- `ascendc.cpp` 中 `copy_in/compute/copy_out` 三个函数都以 `__inline__ __attribute__((always_inline)) __aicore__` 开头，只有 `vadd_kernel` 入口是 `extern "C" __global__`；Kernel 循环体内对它们的调用是普通 C 函数调用写法。

**预期结果**：三个 Device 函数的函数体完整保留在 `ascendc.cpp` 中（带 always_inline 前缀），函数边界直到毕昇编译阶段才被消除；最终 `binary.o` 中只有 Kernel 入口符号。Device 函数与 Kernel 的具体排列顺序、「private」标记的出现位置，**待本地验证**（以实际 dump 内容为准）。

#### 4.2.5 小练习与答案

**练习 1**：同一个 kernel 里先写 `copy_in(..., tile_length=8)` 再写 `copy_in(..., tile_length=16)`（假设 `tile_length` 是 `copy_in` 的 ConstExpr 形参），第二次调用会生成一个新的函数特化吗？

**答案**：不会。`has_function(fn_name)` 只按函数名查重（[python/asc/codegen/function_visitor.py:193](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L193)），第二次调用直接复用第一次生成的函数体与缓存返回类型；而 ConstExpr 值只进 `Specialization.constexprs`（影响函数体的生成），不随 `func.CallOp` 传递。因此第二次调用的 `tile_length=16` 完全被忽略，函数行为以第一次的 `8` 为准。若确需两种特化，应写出两个不同名的子函数。

**练习 2**：为什么 `TQue`、`GlobalTensor` 可以直接传给子函数，而一个 Python 字符串不行？

**答案**：`TQue`（经 `TQueBind`）、`GlobalTensor`（经 `BaseTensor`）都是 `IRValue` 子类，`call_jit_function` 把它们包装成 `IRArgType` 后以 IR 值形态进入子函数签名，再由 `from_ir` 还原；`IRArgType` 构造时校验 `issubclass(py_type, IRValue)`，字符串不满足，直接抛 `TypeError: Only IRValue can be passed between JIT functions`。若要传「编译期已知」的配置信息，正确做法是声明为 `ConstExpr[str]` 之类的常量形参——但注意 ConstExpr 值同样受练习 1 的同名去重约束。

**练习 3**：既然 IR 里函数边界还在（`func.CallOp` 是真实调用），为什么说 Device 函数是「内联」的？这对性能意味着什么？

**答案**：内联发生在两级之后：发射层给非 `asc.global` 函数打 `__inline__ __attribute__((always_inline))` 前缀（Func.cpp 第 66 行），毕昇编译器编译 `.cce` 时执行强制内联。对性能而言，Kernel 循环里调用 `copy_in` 不会产生真实的函数调用开销，分层组织代码是「零成本抽象」；代价是若子函数很大且被多处调用，二进制体积与编译时间会随调用点展开而增长。

### 4.3 Kernel 与 Device 函数：一枚 is_kernel 标志决定的三种差异

#### 4.3.1 概念说明

`@asc.jit` 修饰的函数有两种身份，判据**不是**装饰器怎么写，而是**如何被使用**：

- 用中括号语法 `kernel[核数, 流](...)` 启动 → **Kernel**：编译成独立的 Kernel 二进制入口，被 aclrt 下发到 NPU 执行。
- 被另一个 jit 函数用小括号调用 → **Device 侧执行函数**：作为子函数内联进调用方的 IR 模块。

代码层面，这个身份由 `FunctionVisitor` 的构造参数 `is_kernel` 决定，且只有一个赋值点差异：Kernel 的 visitor 由 `_run_codegen` 创建时传 `is_kernel=True`（[python/asc/runtime/jit.py:184-194](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L184-L194)），子函数的 visitor 由 `call_jit_function` 创建时传 `is_kernel=False`。这枚标志在三个地方产生可见差异：

| 差异点 | Kernel（is_kernel=True） | Device 函数（is_kernel=False） |
| --- | --- | --- |
| 符号可见性 | `make_global()` 打上 `asc.global` 属性 → public → 发射为 `extern "C" __global__` | 无该属性 → PrivatizeFunc 设为 private → 发射为 `__inline__ always_inline` |
| return 规则 | 不能 return 任何对象（报错） | 可以 return，返回类型写入函数签名，调用方拿回 IRValue |
| 编译选项 | 装饰器/调用处选项经 `_run` 合并生效 | 自己的装饰器选项完全不参与；Pass 选项随整个模块生效 |

#### 4.3.2 核心流程

Device 函数返回值的流动路径（Kernel 不具备的能力）：

```text
子函数体内：return a, b
  └── visit_Return：is_kernel=False → 不走报错分支
        ├── materialize_ir_value 物化返回值
        ├── return_types 记录 [ReturnType(py_type, ir_type), ...]
        └── create_func_ReturnOp([...])            # 带值的 IR return

回到 visit_FunctionDef 收尾：
  └── 若有返回值：ir_function.set_type(补上结果类型的函数类型)

回到 call_jit_function（调用方）：
  └── create_func_CallOp(..., 结果类型列表)
        └── 0 个 → None；1 个 → py_type.from_ir(result)；多个 → 列表
```

选项的生效路径（解释「Device 函数传 jit 编译参数无效」）：

```text
@asc.jit(insert_sync=True)          # 装饰器选项 → JITFunction.default_options
def copy_out(...): ...
        │
        ├── 以 Kernel 身份启动：copy_out[8, stream](...)
        │     └── _run 合并 default_options → 生成 CompileOptions → 作用于整个模块
        │
        └── 以 Device 函数身份被调用：copy_out(...)   ← vadd_kernel 内部
              └── visit_Call → call_jit_function
                    └── 只读 fn.fn / fn.node / fn.src / fn.location
                        default_options 从未被读取 → 选项无效
```

#### 4.3.3 源码精读

**差异点一：符号可见性**。`visit_FunctionDef` 中：

[python/asc/codegen/function_visitor.py:529-544](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L529-L544)

```python
def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
    self.state.inside_function = True
    arg_types = self.spec.args.values()
    builder = global_builder.get_ir_builder()
    input_ir_types = [arg_type.to_ir() for arg_type in arg_types]
    self.ir_function = builder.create_func_FuncOp(node.name, builder.get_function_type(input_ir_types))
    self.ir_function.make_aicore()
    if self.is_kernel:
        self.ir_function.make_global()          # 只有 Kernel 打 asc.global
    entry = self.ir_function.add_entry_block()
    ...
```

第 11-12 行是分水岭：`make_global()` 只在 `is_kernel` 为真时执行。这个属性随后被 PrivatizeFunc（定 public）与发射层（`extern "C" __global__`）消费，串起 4.2 讲的整条链。另外注意第 9 行 `make_aicore()` 对两类函数都执行——它标记「这是设备侧函数」，与是否为 Kernel 入口无关。

**差异点二：return 规则**：

[python/asc/codegen/function_visitor.py:644-660](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L644-L660)

```python
def visit_Return(self, node: ast.Return) -> None:
    ...
    self.state.discard_everything = True
    if value is None:
        return
    if self.is_kernel:
        self.raise_unsupported(node, "JIT kernel function cannot return objects")
    values = []
    ...
    self.state.return_types = [ReturnType(type(value), value.to_ir().get_type()) for value in ir_values]
    global_builder.get_ir_builder().create_func_ReturnOp([value.to_ir() for value in ir_values])
```

第 6-7 行：Kernel 试图 return 对象时直接抛 `UnsupportedSyntaxError`——Kernel 的输出只能通过指针参数（如 `z`）写回，没有返回值通道。Device 函数则记录返回类型并生成带值的 return；函数类型在收尾时补上结果（[python/asc/codegen/function_visitor.py:548-550](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L548-L550)）。

**差异点三：编译选项作用域**。装饰器上的选项在 `JITFunction.__init__` 里只被**存放**：

[python/asc/runtime/jit.py:35-46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L35-L46)

```python
class JITFunction(Function[P, T]):
    ...
    def __init__(self, fn: Callable[P, T], **options):
        super().__init__(fn)
        ...
        self.default_options: Dict[str, Any] = options
```

`default_options` 唯一的消费时机是 Kernel 启动路径 `_run`（`kernel[n, stream]` 触发，u3-l1 讲过「合并默认选项 → 调用时覆盖装饰器」）。对照 `call_jit_function`（4.2.3 摘录）：它从头到尾只访问 `fn.fn`、`fn.node`、`fn.src`、`fn.location` 四个成员，**从不读取 `default_options`**；子 visitor 继承的是 Kernel 的 `CodegenOptions`（只有 `capture_exceptions`、`ir_multithreading` 两个字段，用于异常包装与 IR 多线程开关）。

至于 `CompileOptions`（`insert_sync`、`kernel_type` 等），它的作用对象本来就是**整个 IR 模块**——`run_passes(mod)` 把 PassManager 跑在模块上（[python/asc/runtime/compiler.py:175-183](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L175-L183)），天然同时覆盖 Kernel 与所有 Device 函数。所以给某个 Device 函数单独声明 `@asc.jit(insert_sync=True)` 没有意义：要么整个模块触发同步重建链，要么都不触发。`LaunchOptions`（核数、流）更是只有 Kernel 才有「下发」概念，与 Device 函数无关。

还有一条隐含规则：**不能在调用点给子函数传编译选项**。`copy_in(i, x_gm, ...)` 的小括号里只能放函数形参——编译选项只存在于 Kernel 启动语法 `kernel[...]({...选项...})` 的两个位置（中括号与关键字实参），而这正是 u3-l1 讲过的「形参名不得与配置关键字撞名」检查存在的原因。

#### 4.3.4 代码实践

**实践目标**：验证「给 Device 函数声明的 jit 编译选项不生效」。

**操作步骤**：

1. 复制 `add_framework.py` 为 `add_framework_opt.py`（示例代码，读者自行创建），把 `copy_out` 的装饰器改为 `@asc.jit(insert_sync=True)`：

```python
@asc.jit(insert_sync=True)                      # 故意给 Device 函数加编译选项
def copy_out(i: int, z_gm: asc.GlobalTensor, out_queue_z: asc.TQue,
             tile_length: asc.ConstExpr[int]):
    ...
```

2. 分别用原版和修改版运行，均设置 `PYASC_DUMP_PATH`：

```bash
PYASC_DUMP_PATH=/tmp/dump_a python3 examples/02_add_framework/add_framework.py -r Model
PYASC_DUMP_PATH=/tmp/dump_b python3 add_framework_opt.py -r Model
```

3. 对比两份 `ascir.mlir` 与 `ascendc.cpp`：`diff /tmp/dump_a/ascendc.cpp /tmp/dump_b/ascendc.cpp`。

**需要观察的现象**：diff 结果为空（或仅剩文件名等无关差异）；两份 IR 中同步相关操作完全一致。

**预期结果**：`insert_sync=True` 对 `copy_out` 不产生任何影响——`call_jit_function` 不读 `default_options`，且本示例是显式 TQue 队列风格，IR 中没有 `LocalTensorAutoOp`，`need_insert_sync()` 本来就返回 False（[python/src/IR.cpp:569-574](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L569-L574)），同步重建链不会触发。注意：本实践需要完整的 pyasc 运行环境，**待本地验证**。作为不需要环境的替代，可以直接阅读 [python/asc/codegen/function_visitor.py:179-199](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L179-L199)，确认 `call_jit_function` 的代码路径中确实不存在对 `default_options` 的任何引用。

#### 4.3.5 小练习与答案

**练习 1**：同一个 `@asc.jit` 函数能否既是 Kernel 又是 Device 函数？

**答案**：能。身份由使用方式决定：`f[8, stream](...)` 以 Kernel 身份编译（独立入口、参与两级缓存、走 `_run` 全流程）；在另一个 jit 函数内写 `f(...)` 则以 Device 函数身份内联进对方模块（`is_kernel=False`）。两种身份互不影响，编译产物也各自独立。

**练习 2**：Kernel 为什么不允许 return 对象，而 Device 函数可以？

**答案**：Kernel 是 aclrt 下发的执行入口，Host 与设备之间只有「参数 blob + 指针写回」这一条数据通道（u3-l6 的参数 ABI），没有接收返回值的机制，所以 `visit_Return` 对 `is_kernel=True` 直接报 `JIT kernel function cannot return objects`；Device 函数的调用点与被调点同在一个 IR 模块内，返回值就是 `func.CallOp` 的结果 SSA 值，走寄存器/栈即可，天然支持。

**练习 3**：如果不小心写了 `@asc.jit(insrt_sync=True)`（拼写错误），错误在什么时候、由哪段代码报出？

**答案**：在**装饰时**（模块导入时）就报错，与是否被调用无关。`JITFunction.__init__` 用 `get_config_keywords()`（拼合 `CodegenOptions/CompileOptions/LaunchOptions` 三个选项袋的全部字段名，[python/asc/runtime/jit.py:125-135](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L125-L135)）检查选项名集合，未知名字抛 `RuntimeError: The following option names are unknown: insrt_sync`（[python/asc/runtime/jit.py:41-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L41-L43)）。这是 u3-l1 讲过的两道守门检查之一。

## 5. 综合实践

**任务**：把 02_add_framework 的 `compute` 再拆出一个子函数 `add_and_enque`，验证「多级子函数调用全部内联进同一个 Kernel」，并解释内联与同步插入 Pass 的关系。

**步骤**：

1. 复制示例为 `add_framework_split.py`（示例代码，读者自行创建），把 `compute` 拆成两级：

```python
@asc.jit
def add_and_enque(z_gm: asc.GlobalTensor, in_queue_x: asc.TQue, in_queue_y: asc.TQue,
                  out_queue_z: asc.TQue, tile_length: asc.ConstExpr[int]):
    x_local = in_queue_x.deque(z_gm.dtype)      # TQue 不记录 dtype，沿用原示例「借 z_gm.dtype」的写法
    y_local = in_queue_y.deque(z_gm.dtype)
    z_local = out_queue_z.alloc_tensor(z_gm.dtype)
    asc.add(z_local, x_local, y_local, tile_length)
    out_queue_z.enque(z_local)
    ...
```

   实现提示：`add_and_enque` 承接原 `compute` 中「deque 两个输入 → add → enque 输出」的部分；形参照抄原函数的标注风格（`asc.GlobalTensor`、`asc.TQue`、`asc.ConstExpr[int]`）。`compute` 保留 `free_tensor` 收尾或一并下沉到新函数，自行取舍并保证 alloc/free 配对。

2. 运行并导出中间产物：

```bash
mkdir -p /tmp/dump_split
PYASC_DUMP_PATH=/tmp/dump_split python3 add_framework_split.py -r Model
```

3. 验证结果一致：脚本内的 `assert torch.allclose(z, x + y)` 通过。
4. 检查 `/tmp/dump_split/codegen.mlir`：模块内应有 5 个函数（`copy_in`、`compute`、`add_and_enque`、`copy_out`、`vadd_kernel`），`compute` 函数体内有一条对 `add_and_enque` 的 `call`，`vadd_kernel` 体内有三条对一级子函数的 `call`——调用链两级，全部共处一个模块。
5. 检查 `/tmp/dump_split/ascendc.cpp`：`add_and_enque` 与其他子函数一样带 `__inline__ __attribute__((always_inline))` 前缀；`compute` 内对它的调用是普通 C 调用。
6. 回答思考题：**这次拆分对插入同步 Pass 有什么影响？** 参考要点：
   - `InsertSync` 是 `func::FuncOp` 级 Pass，`add_and_enque` 作为独立 FuncOp 会被逐函数处理，Pass 的分析边界与新的函数边界自动对齐；
   - 本示例是显式 TQue 队列风格，模块中没有 `LocalTensorAutoOp`，`need_insert_sync()` 返回 False，`EraseSync → HoistQueBind → InsertSync → UnifyPipe` 链（[python/asc/runtime/compiler.py:137-141](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L137-L141)）不会触发，两份 dump 的同步结构应无差异；
   - 即便触发了同步插入，插入的 set_flag/wait_flag 调用最终随 `always_inline` 进入 Kernel 体，不会因函数分层而产生调用开销。
7. （可选）对照实验：把 `add_and_enque` 的 `@asc.jit` 去掉再跑一次，观察结果是否仍正确，并用 4.1 的知识解释——去掉装饰器后它变成编译期直接执行的普通函数，`deque` 等调用仍会生成 IR，但 ConstExpr 形参、作用域管理等 JIT 语义不再适用。

**预期结果**：数值结果与原示例一致；IR 与 Ascend C 中均能看到「两级子函数、单模块、always_inline」的结构。整个实践需要可运行 pyasc 环境，**待本地验证**。

## 6. 本讲小结

- `visit_Call` 是编译期函数调用的分岔口：被调对象是 `Function` 实例走 `call_jit_function` 内联路径，否则在编译期直接执行——`asc.add` 等基础 API 正是靠后者向 IR 追加操作；`for-range` 调用被 `visit_For` 截获，不进 `visit_Call`。
- 子函数内联分两层：IR 层由 `call_jit_function` 递归访问子函数 AST，生成同模块的独立 `func.FuncOp` 与调用点的 `func.CallOp`；Ascend C 发射层给非 Kernel 函数打 `__inline__ __attribute__((always_inline))` 前缀，真正的内联由毕昇编译器完成。
- 子函数参数按**子函数自己的标注**分流：IRValue 直通并包装为 `IRArgType`（唯一用途就是子函数互调），在子函数侧经 `from_ir` 还原为 language 层对象；ConstExpr 值烘进子函数的 NameScope，不进 IR 签名。
- `has_function` 按名去重：同名子函数只生成一次函数体，第二次调用的 ConstExpr 实参会被静默忽略——需要多份特化时应改名。
- Kernel 与 Device 函数的差异全部由 `is_kernel` 标志驱动：`make_global` 决定 public/`extern "C" __global__` 还是 private/`always_inline`；只有 Device 函数能 return 对象；装饰器选项只存于 `default_options` 且仅在 Kernel 启动路径生效，`call_jit_function` 从不读它，CompileOptions 天然作用于整个模块。
- 分层 Device 函数是零成本抽象：Pass 以函数为粒度工作、边界清晰，最终内联消除调用开销；代价是子函数随 Kernel 模块整体编译、不能独立缓存。

## 7. 下一步学习建议

- **u4-l5（语法支持边界与错误诊断）**：本讲多处出现「不支持」类报错（不可调用对象、嵌套函数、Kernel return 对象），下一讲系统梳理支持/不支持语法清单与 `CodegenError` 的定位方法。
- **u6-l2 / u6-l3（Transforms 精读）**：本讲看到 `InsertSync` 是函数级 Pass、`PrivatizeFunc` 是模块级 Pass；到第 6 单元深入 `MaterializeTensor`、`HoistUBAllocation` 等如何改写本讲生成的 IR。
- **延伸阅读**：[lib/Target/AscendC/External/Func.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp) 的 `func::FuncOp` 发射逻辑（多基本块函数为何要求变量前置声明），以及 [examples/02_add_framework/add_framework.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py) 的三个子函数如何对应「搬入-计算-搬出」三段流水线。
