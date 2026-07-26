# Python AST 解析器与 overrides

## 1. 本讲目标

本讲承接 u5-l1（eager builder 与 prim_func 转换），回答一个更底层的问题：

> 用户写下的那段 Python 函数体，到底是怎么一行一行变成 TIR IR 的？

读完本讲，你应当能够：

1. 说清楚 tilelang 前端「两条 AST 通路」的分工：TVMScript 风格的 `language/parser`（基于 dispatch 表的访问者）与 eager 的 `DSLMutator`（基于 `__tb` 钩子的 AST 改写器）。
2. 画出一条 `T.copy(A, B)` 语句从 Python AST 节点，到 builder 调用，再到 `tl.tileop.copy` intrinsic 的完整映射。
3. 理解 `language/overrides` 如何用「同键后注册覆盖」的方式，在不动 TVMScript 源码的前提下改写 `Assign`/`AugAssign`/`AnnAssign` 的语义（支持 `local.var` 标量缓冲与链式赋值）。
4. 知道 `language/ast/ir.py` 提供的是哪一层 API，以及它如何经 `_ffi_api` 落到 C++ 的 IRBuilder frame。

---

## 2. 前置知识

在进入源码前，先建立四个直觉。

### 2.1 Python 代码也是一棵树（AST）

Python 解释器在执行函数前，会先把源码解析成一棵「抽象语法树」（Abstract Syntax Tree，AST）。例如：

```python
@T.prim_func
def add(A: T.Tensor((128,), "float32"), B: T.Tensor((128,), "float32")):
    for i in T.serial(128):
        B[i] = A[i] + 1.0
```

它的函数体在 AST 层面大致是：

```
FunctionDef(add)
└── For(target=i, iter=Call(T.serial, 128))
    └── Assign(target=Subscript(B, i),
               value=BinOp(Subscript(A, i), Add, Constant(1.0)))
```

Python 标准库 `ast` 模块给了我们「拿到这棵树、遍历它、改写它」的能力。tilelang 的前端，本质上就是「在函数体被 Python 真正执行之前，拦截这棵 AST，把它翻译成 IR」。

### 2.2 两种「翻译 AST」的经典套路

业界有两种把 AST 翻译成 IR 的常见设计，tilelang 两种都有：

- **dispatch 表 + `visit_*` 方法**（TVMScript 路线）：维护一张「AST 节点类型 → 处理函数」的注册表。遇到 `For` 节点就查表调用 `visit_for`，遇到 `Assign` 就调用 `visit_assign`，每个 `visit_*` 内部调用 IRBuilder 拼 IR。`language/parser` 走的就是这条路。
- **AST 改写器 + 钩子对象**（eager 路线）：用一个 `NodeTransformer` 把用户的 AST 直接改写成「对一个魔法对象 `__tb` 的方法调用」，再让 Python 执行改写后的代码。`language/eager/ast.py` 的 `DSLMutator` 走的是这条路（u5-l1 已讲）。

二者殊途同归：**都是把 Python AST 节点映射成「向 IR builder 追加节点」的动作**。本讲会把这两张映射表都列出来。

### 2.3 IRBuilder 与 frame

TVM 的 IRBuilder 用「frame（帧）」表达嵌套作用域：`T.prim_func()` 返回一个 `PrimFuncFrame`，`T.serial()` 返回一个 `ForFrame`，`T.If()` 返回 `IfFrame`……这些 frame 都是上下文管理器，`__enter__` 进入作用域、`__exit__` 退出并把内部收集的语句挂回外层。`language/ast/ir.py` 提供的就是「创建这些 frame 的 Python 函数集合」。

### 2.4 dispatch 注册表与「覆盖」

TVMScript 用一个全局 dispatch 注册表，键是 `(token, type_name)`，例如 `("tir", "Assign")`。`@dispatch.register(token="tir", type_name="Assign")` 会把一个处理函数登记到这个键下。**当多个模块用同一个键注册时，后注册的覆盖先注册的**——这正是 `language/overrides` 改写语义的手段：它用与上游 TVMScript 完全相同的键重新注册一遍，从而「悄悄换掉」处理逻辑。

> 小提示：本讲提到「上游 TVMScript」时，指的是 `tvm.tirx.script`（tilelang 维护的 TVM `tir` 镜像，名为 `tirx`）。tilelang 的 `language/parser` 是它的一个 fork，`language/overrides` 则是对它的就地补丁。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|---|---|---|
| [tilelang/language/parser/entry.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/entry.py) | TVMScript 风格的入口：`prim_func`/`macro`/`Buffer`/`Ptr` | 4.1 解析器入口 |
| [tilelang/language/parser/parser.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py) | 各类 AST 节点的 `visit_*` 处理函数与 `bind_*` 绑定助手 | 4.1 解析器主体 |
| [tilelang/language/parser/operation.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/operation.py) | 把算术/比较/布尔运算符重载注册到 TIR 表达式 | 4.1 表达式层 |
| [tilelang/language/overrides/parser.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/parser.py) | 用同键覆盖 `Assign`/`AugAssign`/`AnnAssign`，支持 `local.var` 与链式赋值 | 4.2 语义改写 |
| [tilelang/language/overrides/buffer.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/buffer.py) | 给 `tirx.Buffer.__getitem__` 打补丁，提供更友好的越界报错 | 4.2 语义改写 |
| [tilelang/language/ast/ir.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/ast/ir.py) | TIR IR builder 函数库（`buffer`/`prim_func`/`arg`/`serial`/`If`…） | 4.3 IR 构造层 |
| [tilelang/language/ast/_ffi_api.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/ast/_ffi_api.py) | 经 `tvm.ffi` 加载 C++ 侧 `script.ir_builder.tirx` 函数 | 4.3 IR 构造层 |
| [tilelang/language/eager/ast.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py) | `DSLMutator`：把用户 AST 改写为 `__tb` 钩子调用 | 4.3 第二条通路 |
| [tilelang/language/common.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py) | 装配 `T` 命名空间，决定 `overrides` 与 `eager` 的导入顺序 | 4.2/4.3 装配 |

---

## 4. 核心概念与源码讲解

### 4.1 parser 模块：TVMScript 风格的 AST 解析器

#### 4.1.1 概念说明

`tilelang/language/parser/` 是 TVMScript TIR 解析器的一个 fork。它示范了「dispatch 表驱动的 AST 访问者」这一经典编译前端设计：

- **入口**（`entry.py`）：`prim_func` 装饰器把 Python 函数交给 TVMScript 的 `parse()`。
- **驱动**（TVMScript 的 `Parser` 类）：把函数体切成 AST 节点，对每个节点按 `(token, type_name)` 查 dispatch 表，调用对应 `visit_*`。
- **处理函数**（`parser.py`）：每个 `visit_*` 读取子表达式的求值结果，调用 `language/ast/ir.py` 的 IR builder 函数，把 IR 节点追加到当前 frame。
- **表达式层**（`operation.py`）：把 `+`、`<`、`and` 等运算符重载注册到 TIR 的 `PrimExpr`，使得 `A[i] + 1.0` 在求值时自然变成 `tirx.Add(...)`。

> 需要明确的一点：今天默认的 `T.prim_func` 实际指向 eager builder（见 4.3 与 u5-l1）。`language/parser` 是 TVMScript 风格的另一条通路，它把 dispatch 表架构体现得最清楚，也是 `overrides` 改写的对象，因此值得先把它读懂。

#### 4.1.2 核心流程

一条 `@prim_func` 函数从 Python 到 PrimFunc 的流程如下：

```
@prim_func def kernel(...)        # entry.py: prim_func 装饰器
        │
        ▼
parse(func, capture, ...)          # TVMScript Parser 接管
        │
        ▼  按 (token="tir", type_name=AST节点类型) 查 dispatch 表
visit_function_def  ──►  T.prim_func() / T.func_name() / T.arg()   开 PrimFuncFrame
        │
        ▼  逐条访问函数体
visit_for          ──►  for_frame = T.serial(...)  ;  with for_frame as iters
visit_assign       ──►  T.buffer_store(buf, value, indices)        （切片赋值）
visit_expr_stmt    ──►  T.evaluate(...) / T.buffer_store(...)      （表达式语句）
visit_if           ──►  T.If(...) / T.Then() / T.Else()
        │
        ▼  frame 退出时把收集到的语句打包
PrimFunc 对象
```

关键在于：**AST 节点类型 ↔ `visit_*` ↔ IR builder 调用**这三者是一一对应的。本节最后会给出完整对照表。

#### 4.1.3 源码精读

**(a) 入口 `prim_func`**

`prim_func` 装饰器捕获调用栈，把函数交给 TVMScript 的 `parse`，并保留原函数名。它同时通过 `dispatch_token = "tir"` 告诉 TVMScript「这是一个 TIR 方言函数」：

- [tilelang/language/parser/entry.py:37-58](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/entry.py#L37-L58) 定义 `prim_func`，支持 `@prim_func` 与 `@prim_func(private=True)` 两种写法（靠 `func is not None` 区分）。
- [tilelang/language/parser/entry.py:66-73](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/entry.py#L66-L73) `decorator_wrapper` 的核心一行：`f = parse(func, utils.inspect_function_capture(func), check_well_formed=check_well_formed)`——把函数与其闭包变量交给 `parse`，得到 `PrimFunc`。
- [tilelang/language/parser/entry.py:85](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/entry.py#L85) `setattr(prim_func, "dispatch_token", "tir")`：声明方言 token，`Parser` 内部据此选择 `tir` 这套 `visit_*`。

`BufferProxy` 与 `PtrProxy` 则是 `T.Buffer(...)`/`T.Ptr(...)` 的代理，最终都落到 `language/ast/ir.py` 的 `buffer`/`ptr`：

- [tilelang/language/parser/entry.py:158-185](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/entry.py#L158-L185) `BufferProxy.__call__` 直接 `return buffer(shape, dtype=..., scope=..., ...)`。

**(b) `visit_for`：循环如何变成 ForFrame**

- [tilelang/language/parser/parser.py:177-198](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L177-L198) `visit_for` 先 `self.eval_expr(node.iter)` 求值 `T.serial(...)` 得到一个 `ForFrame`，再用 `with for_frame as iters` 进入循环作用域，把循环变量绑定后递归访问循环体。

注意它要求 `iter` 必须是 `T.frame.ForFrame`，否则报错——这就是为什么 tilelang 里 `for` 的可迭代对象只能是 `range`/`T.serial`/`T.grid`/`T.parallel` 等返回 frame 的原语。

**(c) `visit_assign`：赋值如何分发到 `buffer_store`**

这是理解整条链路最关键的一段。tilelang 把赋值分成「切片赋值（写缓冲）」和「名字绑定」两类：

- [tilelang/language/parser/parser.py:219-266](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L219-L266) `visit_assign`。当左侧是 `Subscript`（如 `C[i, j] = ...`）时，求值下标并调用 `T.buffer_store(buf, rhs, indices)` 生成缓冲写语句；否则走 `eval_assign` + `bind_assign_value` 做名字绑定。
- [tilelang/language/parser/parser.py:114-159](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L114-L159) `bind_assign_value` 处理 `vi, vj = T.axis.remap("SSR", ...)` 这类绑定：遇到 `Frame` 就 `__enter__`，遇到 `Buffer/IterVar/Var` 就 `IRBuilder.name(...)` 起名。

**(d) `visit_function_def`：函数体的总装**

- [tilelang/language/parser/parser.py:369-416](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L369-L416) 开 `T.prim_func()` frame，设置函数名、返回类型，逐个参数调用 `T.arg(name, ann)`，再 `visit_body`。
- 第 [386](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L386) 行有个有趣细节：`self.var_table.add("range", T.serial)`——在函数作用域里把内置 `range` 直接重映射成 `T.serial`，所以用户写 `for i in range(128)` 也能被解析成串行循环。

**(e) `visit_expr_stmt`：表达式语句如何落地**

像 `T.copy(A, B)` 这种「单独成句的表达式」会走到这里。它根据求值结果的类型分发：

- [tilelang/language/parser/parser.py:437-471](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L437-L471)：结果是 `Frame` 就进入作用域；是 `PrimExpr` 就 `T.evaluate(res)`；是 `BufferStore` 就 `T.buffer_store(...)`；是 `int/bool` 就包成常量 evaluate；字符串（docstring）忽略。

**(f) 表达式层：运算符重载**

`A[i] + 1.0` 之所以求值后是 `tirx.Add`，是因为 `operation.py` 把每个运算符注册成了 TIR 表达式的方法：

- [tilelang/language/parser/operation.py:29-114](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/operation.py#L29-L114) `_register_expr_op` 内部定义 `_and/_or/_eq/_lt/...`，并用 `register_op(ty, op, i)(m)` 把它们挂到 `doc.Add`/`doc.Eq`/`doc.And` 等 AST 运算符节点上。其中 `_auto_broadcast` 负责把 Python 的 `int/float` 自动提升成与对面操作数同 dtype 的 `IntImm/FloatImm`，并在 lanes（向量宽度）不一致时插 `tirx.Broadcast`。
- [tilelang/language/parser/operation.py:153-154](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/operation.py#L153-L154) 对 `PrimExpr` 与 `IterVar` 两类各注册一次。

#### 4.1.4 代码实践

**实践目标**：亲手验证「AST 节点类型 → `visit_*`」的对应关系，并观察「内置 `range` 被重映射为 `T.serial`」这一行为。

**操作步骤**：

1. 阅读 [tilelang/language/parser/parser.py:369-416](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L369-L416) 的 `visit_function_def`，确认 `range` 被加入 `var_table` 指向 `T.serial`。
2. 写一个最小 TIR kernel（注意：这里用 tilelang 自己的 `language.parser` 入口，而非默认的 eager `T.prim_func`），分别用 `for i in range(8)` 与 `for i in T.serial(8)`：

   ```python
   # 示例代码：仅用于阅读 parser 行为，依赖 tilelang 内部模块
   from tilelang.language.parser import prim_func
   import tilelang.language.ast.ir as T

   @prim_func
   def kernel(A: T.Buffer((8,), "float32")):
       for i in range(8):       # range 在函数作用域内被重映射为 T.serial
           A[i] = A[i] + 1.0
   ```

3. 打印 `kernel`（得到的是 `PrimFunc`），观察其 script 形式里循环是否被规范化为 `T.serial`。

**需要观察的现象**：两种写法生成的 PrimFunc 循环结构应一致；`range(8)` 被当作 `T.serial(0, 8)` 解析。

**预期结果**：函数体落成 TIR 的 `For` 节点，`kind=serial`，`A[i] = A[i] + 1.0` 落成 `BufferStore`。

**若无法本地运行**：标注「待本地验证」。即便不运行，对照 `visit_for`（[parser.py:177-198](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L177-L198)）与 `visit_assign`（[parser.py:219-266](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L219-L266)）即可推断出上述结论。

#### 4.1.5 小练习与答案

**练习 1**：为什么 tilelang 里 `for i in range(128)` 能合法，而 `for i in [0, 1, 2]` 不行？

> **参考答案**：`visit_for` 要求 `node.iter` 求值结果是 `T.frame.ForFrame`。`range` 在 `visit_function_def` 里被重映射为 `T.serial`，调用 `T.serial(128)` 返回 `ForFrame`；而 `[0,1,2]` 是普通 Python list，求值后不是 `ForFrame`，触发 [parser.py:190-194](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L190-L194) 的报错。

**练习 2**：`C[i, j] = A[i, j] * 2.0` 在 `visit_assign` 里走的是哪条分支？最终调用哪个 IR builder 函数？

> **参考答案**：左侧 `C[i, j]` 是 `Subscript`，走 [parser.py:257-264](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L257-L264) 的分支，求值下标 `[i, j]` 与右值后调用 `T.buffer_store(C, rhs, [i, j])`。

**练习 3**：`A[i] + 1.0` 中的 `+` 是怎么变成 TIR 的 `Add` 的？

> **参考答案**：`operation.py` 用 `register_op` 把 `doc.Add` 节点重载注册到 `PrimExpr`；`Parser` 求值 `BinOp` 时调用该重载，内部经 `_auto_broadcast` 把 `1.0` 提升为 `FloatImm`，再返回 `tirx.Add(A[i], FloatImm(1.0))`（见 [operation.py:58-93](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/operation.py#L58-L93)）。

---

### 4.2 overrides 模块：不动上游源码的语义改写

#### 4.2.1 概念说明

`language/overrides/` 解决的问题是：**tilelang 需要给 TVMScript 的 TIR 解析器加一点自己的语义，但又不想（也不能）去改 3rdparty 里 TVM 的源码。**

它的做法非常轻量——「同键后注册覆盖」：上游 TVMScript 已经用 `@dispatch.register(token="tir", type_name="Assign")` 注册了一个 `visit_assign`；tilelang 在自己的 `overrides/parser.py` 里用**完全相同的键**再注册一次 `tilelang_visit_assign`。由于 tilelang 的 `overrides` 模块是在上游解析器之后被导入的（见 4.2.2），后注册者胜出，于是 tilelang 的逻辑就「替换」了上游逻辑。

具体改写了两类语义：

1. **链式赋值**：支持 `a = b = c = 1` 这种连续赋值（上游只支持单目标）。
2. **`local.var` 标量缓冲的写回**：tilelang 的 `T.alloc_var` 产生的是 scope 为 `local.var` 的零维缓冲，对它的赋值要翻译成 `buffer_store(buf, value, [0])`，而不是普通变量绑定。

此外，`overrides/buffer.py` 用经典的猴子补丁替换 `tirx.Buffer.__getitem__`，把「下标个数不匹配」的错误信息改得更友好。

#### 4.2.2 核心流程

```
tilelang/language/common.py 装配 T 命名空间
        │
        ▼  from .tir.common import *     # ① 触发上游 tvm.tirx.script.parser 导入
        │                                  #    上游用 (tir, Assign) 注册 visit_assign
        ▼  from . import overrides        # ② 导入 overrides 包
        │                                  #    overrides/__init__.py 导入 parser、buffer
        ▼  overrides/parser.py 模块级执行 @dispatch.register(token="tir", type_name="Assign")
        │                                  # ③ 用同键重新注册 tilelang_visit_assign → 覆盖
        ▼  overrides/buffer.py 模块级执行 tirx.Buffer.__getitem__ = _patched_buffer_getitem
                                       # ④ 替换 Buffer 下标方法
```

关键证据是 `common.py` 的导入顺序：

- [tilelang/language/common.py:14-17](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py#L14-L17) 先 `from .tir.common import *`（拉起上游解析器），紧接着 `from . import overrides as _overrides`（触发覆盖），最后 `from .eager import *`。
- [tilelang/language/overrides/__init__.py:7-9](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/__init__.py#L7-L9) 「Register parser overrides upon import」——导入即注册。

#### 4.2.3 源码精读

**(a) 覆盖 `Assign`：链式赋值 + `local.var` 写回**

- [tilelang/language/overrides/parser.py:19-21](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/parser.py#L19-L21) 用同键注册 `tilelang_visit_assign`，docstring 直言「Override `Assign` to support chained writes and `local.var` buffers」。
- [tilelang/language/overrides/parser.py:46-54](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/parser.py#L46-L54) `for lhs in node.targets:` 遍历**所有**赋值目标，逐个处理——这就是链式赋值的实现（上游只取 `targets[0]`）。对每个 `Subscript` 左侧都 `T.buffer_store(...)`。
- [tilelang/language/overrides/parser.py:56-70](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/parser.py#L56-L70) 关键的 `local.var` 分支：若左侧名字解析出的 `lhs_value` 是一个 `BufferLoad`，且其 buffer 的 `scope() == "local.var"`、单下标 `0`，则把赋值翻译成 `T.buffer_store(lhs_value.buffer, rhs, indices=[0])`——把「向标量变量赋值」改写成「向零维缓冲写一个元素」。
- [tilelang/language/overrides/parser.py:71](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/parser.py#L71) 其余情况回退到上游行为：`self.eval_assign(target=lhs, source=rhs, bind_value=tvm_tir_parser.bind_assign_value)`（注意这里复用的是从 `tvm.tirx.script.parser` 导入的上游 `bind_assign_value`）。

> 设计要点：`overrides` 不是「全盘重写」，而是「特判 tilelang 新增的两种情况，其余委托回上游」。这是覆盖式补丁最稳的写法——尽量复用上游逻辑，只补差异。

**(b) 覆盖 `AugAssign` 与 `AnnAssign`**

两者思路相同，都是「在原有逻辑前插入一段 `local.var` 特判」：

- [tilelang/language/overrides/parser.py:76-123](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/parser.py#L76-L123) `tilelang_visit_aug_assign`：`acc += x` 先求出 `lhs_expr`/`rhs_expr`，合成 `BinOp` 求值，再对 `local.var` 做 `buffer_store(..., [0])`，否则回退上游。
- [tilelang/language/overrides/parser.py:128-155](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/parser.py#L128-L155) `tilelang_visit_ann_assign`：带类型注解的赋值（如 `acc: T.float32 = 0.0`）同样加 `local.var` 特判。

**(c) `Buffer.__getitem__` 补丁**

- [tilelang/language/overrides/buffer.py:10-26](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/buffer.py#L10-L26) 先保存原始 `_original_buffer_getitem`，定义 `_patched_buffer_getitem` 检查下标个数是否等于 buffer 维度，不匹配时抛出带 shape 信息的 `IndexError`，匹配则调原始实现。最后 `tirx.Buffer.__getitem__ = _patched_buffer_getitem` 完成替换。这是纯用户体验改进，不影响正确性。

#### 4.2.4 代码实践

**实践目标**：验证「同键覆盖」的导入顺序依赖，以及 `local.var` 写回语义。

**操作步骤**：

1. 确认导入顺序：阅读 [common.py:14-17](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py#L14-L17)，回答：如果把 `from . import overrides` 移到 `from .tir.common import *` **之前**，会发生什么？
2. 在 Python 中检查 dispatch 表里 `(tir, Assign)` 指向哪个函数（只读观察）：

   ```python
   # 示例代码：观察覆盖结果（依赖 tvm.script.parser._core.dispatch 内部结构）
   import tilelang.language  # 触发 common.py 装配 → overrides 注册
   from tvm.script.parser._core import dispatch
   # dispatch 的内部注册表形如 {(token, type_name): func}
   print(dispatch._registry[("tir", "Assign")])  # 应为 tilelang_visit_assign
   ```

3. 观察链式赋值语义：写一个 `a = b = c = T.alloc_local(...)` 风格的片段，对比上游（只赋 `targets[0]`）与 tilelang（遍历全部 `targets`）的差异。

**需要观察的现象**：步骤 2 应打印出 `tilelang.language.overrides.parser` 里的函数；步骤 1 的反例会导致覆盖失效（`a = b = c` 报错或只赋值第一个目标）。

**预期结果**：tilelang 的 `tilelang_visit_assign` 确实占据了 `(tir, Assign)` 这个键。

**若无法本地运行**：标注「待本地验证」。`dispatch._registry` 的具体字段名以本地安装的 TVM 版本为准，可能需要调整；核心结论（同键后注册覆盖）由 [overrides/parser.py:19](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/parser.py#L19) 与 [common.py:14-17](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/common.py#L14-L17) 的导入顺序共同保证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tilelang_visit_assign` 在特判之后还要调 `tvm_tir_parser.bind_assign_value`，而不是自己重新实现一遍绑定逻辑？

> **参考答案**：为了最小化与上游的差异、降低维护成本。tilelang 只关心「链式赋值」和「`local.var`」两点新语义，其余（普通变量绑定、`T.axis.remap` 等）与上游完全一致，直接复用上游 `bind_assign_value` 即可，上游逻辑变动时 tilelang 自动跟进。

**练习 2**：`acc: T.float32 = 0.0` 中 `acc` 是 `T.alloc_var` 产生的标量。这条赋值最终生成什么 TIR？

> **参考答案**：`acc` 对应一个 scope 为 `local.var` 的零维 buffer；`tilelang_visit_ann_assign` 的 `local.var` 分支（[overrides/parser.py:143-150](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/parser.py#L143-L150)）把它翻译成 `T.buffer_store(acc_buffer, 0.0, indices=[0])`，即向该 buffer 的第 0 个元素写入常量。

**练习 3**：`overrides/buffer.py` 的补丁若不加，对功能有无影响？

> **参考答案**：无正确性影响，仅影响错误信息可读性。原始 `__getitem__` 在下标个数不匹配时报错信息不直观；补丁加上后会提示 buffer 的真实 shape 与期望下标个数（见 [buffer.py:18-22](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/overrides/buffer.py#L18-L22)）。

---

### 4.3 ast 模块与两条前端的对照

#### 4.3.1 概念说明

`language/ast/` 提供的是「IR 构造函数库」。它**不解析 AST**，而是把「我已经决定要建一个 buffer / 开一个 for / 写一次 buffer」这类意图，翻译成对 C++ IRBuilder 的调用。可以把它理解成 `visit_*` 与 `__tb` 钩子共同依赖的「积木箱」。

`language/ast/ir.py` 里的每个函数都很薄：参数校验 + 调一次 `_ffi_api.XXX(...)`，把活儿交给 C++ 侧注册在 `script.ir_builder.tirx` 命名空间下的函数，返回一个 frame：

- [tilelang/language/ast/_ffi_api.py:23](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/ast/_ffi_api.py#L23) `tvm.ffi._init_api("script.ir_builder.tirx", __name__)` 把 C++ 函数挂到 `_ffi_api` 模块对象上。
- [tilelang/language/ast/ir.py:95-163](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/ast/ir.py#L95-L163) `buffer(...)` 规整 shape/strides 后 `return _ffi_api.Buffer(...)`。
- [tilelang/language/ast/ir.py:171-186](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/ast/ir.py#L171-L186) `prim_func(...)` 直接 `return _ffi_api.PrimFunc(is_private)`，返回 `PrimFuncFrame`。
- [tilelang/language/ast/ir.py:189-205](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/ast/ir.py#L189-L205) `arg(name, obj)` → `_ffi_api.Arg(name, obj)`。

#### 4.3.2 核心流程：两条通路共用「AST → builder」的思想

理解了 4.1 的 dispatch 通路后，再看 eager 通路（u5-l1 已介绍 `Builder`），会发现二者**共享同一套思想，只是 visitor 的实现不同**：

| 维度 | TVMScript 通路（`language/parser`） | eager 通路（`language/eager`） |
|---|---|---|
| AST 来源 | TVMScript 自带的 `doc` 节点 | Python 标准库 `ast` 节点 |
| 访问者 | `Parser` + dispatch 表 `visit_*` | `DSLMutator(ast.NodeTransformer)` |
| 翻译方式 | 在 `visit_*` 内**直接**调 IR builder | 先把 AST **改写**成 `__tb.xxx(...)`，再让 Python 执行 |
| builder 对象 | `IRBuilder` + frame | `Builder`（`BaseBuilder` 的 TIR 子类） |
| 是否当前默认 | 否（`parser/` 为 TVMScript fork） | **是**（`T.prim_func` 指向 eager） |
| 入口证据 | [entry.py:71](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/entry.py#L71) `parse(func, ...)` | [eager/builder.py:1505-1508](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1505-L1508) `prim_func` → `mutate(func)` |

eager 通路里，`DSLMutator` 把每种 AST 节点改写成对一个魔法对象 `__tb`（即 `Builder`）的方法调用，这就是 u5-l1 所说的「钩子」。两张映射表如下。

**TVMScript 通路的节点 → IR builder 映射**（来自 `parser.py`）：

| AST 节点 | 处理函数 | 发出的 builder 调用 |
|---|---|---|
| `For` | `visit_for` ([parser.py:177](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L177)) | `with T.serial(...) as iters` |
| `Assign`（切片） | `visit_assign` ([parser.py:219](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L219)) | `T.buffer_store(buf, rhs, indices)` |
| `Assign`（名字） | `visit_assign` + `bind_assign_value` ([parser.py:114](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L114)) | `IRBuilder.name(...)` / `T.Let(...)` |
| `With` | `visit_with` ([parser.py:345](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L345)) | `stack.enter_context(frame)` |
| `If` | `visit_if` ([parser.py:474](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L474)) | `T.If/Then/Else` |
| `Expr`（表达式语句） | `visit_expr_stmt` ([parser.py:437](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L437)) | `T.evaluate(...)` / `T.buffer_store(...)` |
| `FunctionDef` | `visit_function_def` ([parser.py:369](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L369)) | `T.prim_func/T.func_name/T.arg` |

**eager 通路的节点 → `__tb` 钩子映射**（来自 `eager/ast.py` 的 `DSLMutator`）：

| AST 节点 | DSLMutator 改写为 | 源码位置 |
|---|---|---|
| `Expr`（如 `T.copy(A,B)`） | `__tb.eval(value)` | [eager/ast.py:296-298](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L296-L298) |
| `For` | `for _ in __tb.ctx_for(range):` | [eager/ast.py:309-317](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L309-L317) |
| `If` | `__tb.ctx_if(cond)` / `__tb.ctx_then/ctx_else` | [eager/ast.py:279-294](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L279-L294) |
| `Assign`（标量） | `__tb.bind('name', value)` | [eager/ast.py:335-337](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L335-L337) |
| `Assign`（切片） | `__tb.assign_slice(lval, slice, value)` | [eager/ast.py:344-352](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L344-L352) |
| `AugAssign` | `__tb.aug_assign('+', target, value)` | [eager/ast.py:440-459](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L440-L459) |
| `While` | `__tb.ctx_while(lambda: cond)` | [eager/ast.py:473-475](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L473-L475) |
| `Break`/`Continue` | `__tb.ctx_break()` / `__tb.ctx_continue()` | [eager/ast.py:324-330](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L324-L330) |

> 这两张表就是「AST 节点到 builder 调用的映射」，也是本讲实践任务要画的图。

#### 4.3.3 源码精读：一条 `T.copy(A, B)` 的完整旅程（eager 默认通路）

把上面的映射表串起来，跟踪默认 eager 通路下 `T.copy(A, B)` 这一行：

1. `@T.prim_func` 触发 [eager/builder.py:1505-1508](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1505-L1508) 的 `prim_func`，调用 `mutate(func)` 得到 `IRGenerator`。
2. `DSLMutator` 把函数体里 `T.copy(A, B)` 这一 `ast.Expr` 改写成 `__tb.eval(T.copy(A, B))`（[eager/ast.py:296-298](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L296-L298)）。
3. 改写后的代码在 `Builder` 上下文里执行：`T.copy` 即 [tilelang/language/copy_op.py:54](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py#L54) 的 `copy`，它返回一个 intrinsic 调用 `tirx.call_intrin("handle", tirx.op.Op.get("tl.tileop.copy"), src, dst, ...)`（[copy_op.py:134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py#L134)）。
4. `__tb.eval(...)` 拿到这个 `call_intrin` 表达式后，由 `Builder` 把它作为一条求值语句追加进当前 frame（与 4.1 里 `visit_expr_stmt` 对 `PrimExpr` 调 `T.evaluate` 的处理对应）。
5. 至此，源码层只留下一个语义占位 `tl.tileop.copy`——具体展开成 TMA/cp.async/普通循环，要等到 u6 的 `lower_tile_op` Pass（与 u3-l1、u2-l2 的描述一致）。

把这条链路画成图：

```
ast.Expr: T.copy(A, B)
      │  DSLMutator.visit_Expr
      ▼
__tb.eval( T.copy(A, B) )
      │  T.copy = copy_op.copy
      ▼
tirx.call_intrin("tl.tileop.copy", src, dst)
      │  __tb.eval (Builder)
      ▼
TIR 语句: Evaluate(Call tl.tileop.copy)
      │  （后续 Pass lower_tile_op 展开）
      ▼
TMA / cp.async / 普通循环
```

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：阅读 `parser/entry.py` 与 `eager/ast.py`，亲手画出「一个 `T.copy` 调用如何被解析并最终调用 eager builder」的 AST 节点到 builder 调用映射图。

**操作步骤**：

1. 打开 [tilelang/language/parser/entry.py:66-73](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/entry.py#L66-L73)，确认 TVMScript 通路的入口是 `parse(func, ...)`。
2. 打开 [tilelang/language/eager/ast.py:266-330](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/ast.py#L266-L330)，找到 `class DSLMutator(ast.NodeTransformer)` 与 `visit_Expr`，确认 `T.copy(A, B)` 被改写成 `__tb.eval(value)`。
3. 打开 [tilelang/language/copy_op.py:54-134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py#L54-L134)，确认 `T.copy` 返回 `tirx.call_intrin(... "tl.tileop.copy" ...)`。
4. 用 4.3.3 末尾的流程图作为模板，自己重画一遍，并在每个箭头旁标注**对应的源码文件与行号**。

**需要观察的现象**：你能为图中每一步找到确切的源码出处；`T.copy` 在前端**只**产生一个 intrinsic 占位，不产生任何搬运指令。

**预期结果**：得到一张三段式映射图 `ast.Expr → __tb.eval → tl.tileop.copy intrinsic`，且每段都能点到真实代码行。

**若无法本地运行**：本任务为「源码阅读型实践」，无需运行即可完成；如需验证，可在 eager builder 里给 `__tb.eval` 对应的 `Builder` 方法临时加一行日志（只读分析，不改源码逻辑），观察 `T.copy` 是否确实经过该路径——标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`language/ast/ir.py` 里的 `prim_func` 与 `language/parser/entry.py` 里的 `prim_func` 是同一个东西吗？

> **参考答案**：不是。`ast/ir.py:171` 的 `prim_func(is_private)` 是一个 **IR builder 函数**，返回 `PrimFuncFrame`（开作用域用）；`parser/entry.py:37` 的 `prim_func` 是一个**装饰器**，负责把 Python 函数交给 TVMScript 解析。前者是「积木」，后者是「用积木搭房子的人」。eager 通路里的第三个 `prim_func`（[eager/builder.py:1505](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py#L1505)）才是今天 `T.prim_func` 实际指向的入口。

**练习 2**：为什么 `DSLMutator` 要先把 AST 改写成 `__tb.xxx(...)`，而不是像 TVMScript 那样在 `visit_*` 里直接建 IR？

> **参考答案**：改写成普通 Python 调用后，可以让 **Python 解释器自己**去处理表达式求值、运算符重载、闭包捕获等复杂语义，前端只需提供 `__tb` 这一组钩子。这样既能复用 Python 的求值能力（如 `A[i] + B[i]` 的运算符重载），又能让 eager 模式在运行时用真实参数替换 `T.const`（u5-l1 的 phase2 机制），实现「一份模板通吃多种 shape」。

**练习 3**：在 4.3.3 的旅程里，`T.copy` 前端产生的 intrinsic 名字是什么？它在哪里被展开成真实指令？

> **参考答案**：intrinsic 名字是 `tl.tileop.copy`（[copy_op.py:134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/copy_op.py#L134)）。它在编译流水线后期的 `lower_tile_op` Pass 中展开（详见 u6-l2「关键 lowering Pass 解读」与 u2-l2 的搬运原语说明），前端不负责选指令。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「端到端追踪」小任务。

**任务**：给下面这段默认（eager）通路的 kernel 画一张完整的「Python 源码 → AST 节点 → builder 调用 → TIR 语句」映射表。

```python
import tilelang.language as T

@T.prim_func
def scaled_copy(A: T.Tensor((128,), "float32"),
                B: T.Tensor((128,), "float32")):
    with T.Kernel(128) as i:
        acc = T.alloc_local((1,), "float32")
        acc[0] = A[i]
        B[i] = acc[0] * 2.0
```

**要求**：

1. 对 `with T.Kernel`、`acc = T.alloc_local(...)`、`acc[0] = A[i]`、`B[i] = acc[0] * 2.0` 这四行，分别指出：
   - 它对应哪种 Python AST 节点（`With`/`Assign`/`Assign+Subscript`/…）；
   - 在 **eager 通路** 里被 `DSLMutator` 改写成哪个 `__tb.xxx(...)` 钩子（查 4.3.2 第二张表）；
   - 最终落到哪一类 TIR 语句（`Allocate`/`BufferStore`/`Evaluate`…）。
2. 解释：为什么 `acc[0] = A[i]` 这一行，如果走的是 TVMScript 通路且 `acc` 是 `local.var` 标量缓冲，会被 `tilelang_visit_assign` 特判为 `buffer_store(acc, A[i], [0])`？（参考 4.2.3）
3. 写下你追踪过程中用到的所有源码行号（形如 `eager/ast.py:296`）作为依据。

**参考思路**：

- `with T.Kernel(128) as i` → `ast.With` →（eager）`__tb` 的 kernel 上下文钩子；（TVMScript）`visit_with` ([parser.py:345](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/parser/parser.py#L345))。
- `acc = T.alloc_local(...)` → `ast.Assign`（名字绑定）→ `__tb.bind('acc', ...)` → 一个 `local` scope buffer。
- `acc[0] = A[i]` → `ast.Assign`（左侧 Subscript）→ eager 走 `__tb.assign_slice`；TVMScript 走 `tilelang_visit_assign` 的 `local.var` 特判或 `T.buffer_store`。
- `B[i] = acc[0] * 2.0` → `ast.Assign`（左侧 Subscript，右侧 `BinOp`）→ 运算符经 `operation.py` 的 `_register_expr_op` 重载成 `tirx.Mul`，最终 `buffer_store(B, Mul(BufferLoad(acc), 2.0), [i])`。

完成本任务后，你就把「Python 写法 → AST → builder 钩子 → TIR」这条链路彻底打通了。

---

## 6. 本讲小结

- tilelang 前端有**两条**把 Python AST 翻译成 IR 的通路：TVMScript 风格的 `language/parser`（dispatch 表 + `visit_*`），与 eager 的 `DSLMutator`（把 AST 改写成 `__tb` 钩子调用）；今天默认的 `T.prim_func` 走 eager。
- `language/parser/parser.py` 把每种 AST 节点（`For`/`Assign`/`With`/`If`/`Expr`/`FunctionDef`）映射到一次 IR builder 调用（`T.serial`/`T.buffer_store`/`T.evaluate`/`T.prim_func`…），`operation.py` 负责把运算符重载到 TIR 表达式。
- `language/overrides` 用「同键后注册覆盖」改写上游 TVMScript 的 `Assign`/`AugAssign`/`AnnAssign`，新增了链式赋值与 `local.var` 标量缓冲写回两类语义，并用猴子补丁改善 `Buffer.__getitem__` 的报错信息；覆盖生效靠 `common.py` 里 `tir.common → overrides` 的导入顺序。
- `language/ast/ir.py` 是「IR 构造积木箱」，每个函数薄薄一层、经 `_ffi_api` 调 C++ 侧 `script.ir_builder.tirx` 返回 frame；它是两条通路共同的底层。
- 一条 `T.copy(A, B)` 在前端只产生 `tl.tileop.copy` intrinsic 占位（`__tb.eval` → `call_intrin`），真实指令要等后端 Pass 展开——这呼应了 u3/u6 的 tile op 模型。

---

## 7. 下一步学习建议

- **顺着 intrinsic 往下走**：本讲停在前端「留占位」。建议接着读 u5-l3「语义检查与参数抽取」，看 `PreLowerSemanticCheck` 如何校验这些占位与 buffer，再到 u6-l2「关键 lowering Pass 解读」看 `lower_tile_op` 如何把 `tl.tileop.copy` 展开成真实搬运指令。
- **想更懂 eager builder**：回头精读 u5-l1 提到的 [tilelang/language/eager/builder.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/eager/builder.py) 中 `Builder` 对 `__tb` 各钩子（`eval`/`bind`/`ctx_for`/`assign_slice`…）的具体实现，把本讲的「钩子名」与「TIR 落点」一一对应起来。
- **想更懂 dispatch 机制**：可对照 3rdparty/tvm 中 `tvm/script/parser/_core/dispatch.py`（若已检出子模块）阅读 `register`/`get` 的实现，验证「同键后注册覆盖」的注册表语义。
- **延伸阅读**：`language/parser/entry.py` 里的 `TIRMacro`/`macro` 展示了 TVMScript 的宏（hygienic vs non-hygienic）机制，是理解「代码片段替换」如何在前端完成的好材料。
