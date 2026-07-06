# Stub 与实现注册：从 builtin 到 IR

## 1. 本讲目标

本讲是「编译前端」单元里把**用户 API** 与**底层 Tile IR** 缝合起来的那一讲。读完本讲，你应当能够：

1. 说清 `@stub` 装饰器到底给一个函数贴了什么标记，后端凭什么认出它。
2. 读懂 `ImplRegistry` 的三张表（`op_implementations` / `_overloaded_implementations` / `unflatten_aggregate_implementations`），并知道 `@impl` 装饰器把一个 IR 实现挂到哪个键上。
3. 解释 `tile_impl_registry` 如何把 core / arithmetic / control_flow / static_eval 四个子注册表合并成一张总表。
4. 掌握 `overload_dispatcher` + `WILDCARD` 的「按操作数类型分派」机制，并用 `operator.add`（tile 相加 vs tuple 拼接）说清多态是怎么实现的。
5. 手动追踪 `ct.add(a, b)` 与 `a + b` 两条路径，一直追到 `RawBinaryArithmeticOperation` 这条 IR Operation。
6. 理解 `build_tuple` / `loosely_typed_const` 里的 `result_var` 复用入口变量（entry Var）的写法。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **stub / function / kernel 三类装饰器**（u1-l4、u5-l1）：`stub` 标记「签名在前端、实现在后端」的内置操作；它不进顶层 `__all__`，靠一个标志位被后端识别。
- **Tile IR 的核心对象**（u5-l5）：`IRContext` / `Builder` / `Block` / `Var` / `Operation`。本讲里 `@impl` 注册的「实现」就是「用 `Builder` 发射若干 `Operation`、返回一个结果 `Var`」的 Python 函数。
- **hir2ir 是一个 HIR 解释器**（u5-l4）：它遍历 HIR 的 `Call`，遇到 stub/内置就走注册表查实现，遇到用户函数就内联。本讲讲的就是「查实现」这一步用的数据结构。
- **load–compute–store 范式**（u3-l1）：你会写 `ct.load` / `ct.add` / `ct.store`，本讲回答「这些 `ct.xxx` 在编译期是怎么变成 IR 的」。

两个术语先约定好：

- **stub（桩）**：用 `@stub` 装饰的函数，只有签名和文档，函数体是 `pass` 或省略；它不是真的实现，而是「占位符 + 类型签名」。
- **impl（实现）**：一个普通的 Python（通常是 `async`）函数，接收 `Var` 参数、用 `Builder` 发射 IR、返回结果 `Var`；通过 `@impl(some_stub)` 挂到某个 stub 上。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `src/cuda/tile/_execution.py` | 定义 `stub` / `function` / `kernel` 三类装饰器，以及 `is_stub` 判定。 |
| `src/cuda/tile/_stub.py` | 全部用户可见的 `@stub` 操作（`add` / `load` / `store` / `bid` …）的签名与文档。 |
| `src/cuda/tile/_ir/op_impl.py` | **本讲主战场**：`ImplRegistry` 类、`@impl` 装饰器、`overload_dispatcher`、`WILDCARD`，以及一堆参数校验辅助函数。 |
| `src/cuda/tile/_ir/ops.py` | 创建并聚合 `tile_impl_registry`；注册 `ct.add` / `ct.sub` 等 stub 的实现、`operator.add` 的 tuple 重载。 |
| `src/cuda/tile/_ir/core_ops.py` | `core_impl_registry()`、`build_tuple`、`loosely_typed_const`、`binop_overload_dispatcher`。 |
| `src/cuda/tile/_ir/arithmetic_ops.py` | `arithmetic_impl_registry()`、`RawBinaryArithmeticOperation`、`operator.add` 的 `(TensorLikeTy, TensorLikeTy)` 重载。 |
| `src/cuda/tile/_ir/control_flow_ops.py` / `static_eval_ops.py` | 分别提供 `control_flow_impl_registry()` 与 `static_eval_impl_registry()` 子表。 |
| `src/cuda/tile/_passes/hir2ir.py` | `_call_function` / `_call_builtin`：在 IR 生成阶段查注册表并调用实现。 |
| `src/cuda/tile/_passes/ast2hir.py` | 把 `a + b` 翻成 `operator.add(a, b)`、把 `ct.add(a,b)` 翻成对 stub 的 `Call`。 |

## 4. 核心概念与源码讲解

### 4.1 `@stub`：用户 API 的签名占位

#### 4.1.1 概念说明

cuTile 的用户 API（`ct.add`、`ct.load`、`ct.bid`……）有一个共同特点：它们的函数体在源码里几乎是空的（常常就是 `pass`）。这是因为 **tile code 不是被 Python 解释执行的**，而是被 ast2hir 翻译成 IR。所以这些函数真正的作用只有两个：

1. 提供一个**类型签名**（供 `inspect.signature` 读取），让前端知道参数个数、名字、默认值。
2. 提供一份**文档字符串**。

`@stub` 装饰器的工作就是：先复用 `function` 的执行空间逻辑，再给返回对象贴一个「我是桩」的标志 `_cutile_python_stub = True`。后端的注册系统凭这个标志把 stub 和 IR 实现对接起来。

#### 4.1.2 核心流程

```text
@stub 装饰 func
   ├── function(func, host=host)      # 处理执行空间（host/tile）
   └── func._cutile_python_stub = True # 贴桩标志
```

判定时，`is_stub` 会沿着 `__wrapped__` 链向上找这个标志（因为 `function` 用了 `functools.wraps`，真实函数可能被包了好几层）。

#### 4.1.3 源码精读

`stub` 装饰器本身非常短，核心就是「先 `function`，再贴标志」：

[`_execution.py:172-181` —— `stub` 装饰器](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L172-L181) 先调 `function` 处理执行空间，再设 `_cutile_python_stub = True`。

判定函数沿 `__wrapped__` 链查找标志，避免被 `functools.wraps` 的层层包装骗过：

[`_execution.py:184-190` —— `is_stub` 沿包装链判定](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_execution.py#L184-L190)

一个真实的 stub 例子——`ct.add`，函数体只有 `pass`，价值全在签名与文档：

[`_stub.py:3148-3165` —— `ct.add` 是一个 `@stub`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L3148-L3165)

> 注意：`Tile.__add__` 里写的 `return add(self, other)`（`_stub.py:616-617`）在 tile code 编译期**不会**被真正调用——tile code 是翻译出来的，不是执行出来的。它只在「假设有人真在 host 端把 Tile 当对象操作」时才有意义。`a + b` 在编译期是被 ast2hir 直接当成 `ast.BinOp` 翻成 `operator.add` 的（见 4.5）。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认「stub 的函数体从不被运行」这一论断。
2. **步骤**：在 `_stub.py` 里挑三个 stub（如 `bid`、`load`、`add`），观察它们的函数体；再用编辑器全局搜索 `_cutile_python_stub`，看除了 `stub()` 和 `is_stub()` 之外，还有谁在读这个标志。
3. **观察**：`hir2ir.py` 的 `_call_function` 正是凭 `is_stub(callee)` 决定走 `_call_builtin`（查注册表）而不是内联用户函数。
4. **预期**：你会得出结论——stub 函数体里写什么都不重要，真正起作用的是它的**签名**和**标志位**。

#### 4.1.5 小练习与答案

- **练习 1**：`@function`（不加 `@stub`）装饰的函数，`is_stub` 返回什么？为什么？
- **练习 2**：如果给 `ct.add` 的函数体里写 `return x + y`，会改变编译结果吗？
- **答案**：
  1. 返回 `False`。`function` 只设了 `_cutile_function_wrapper`，没有设 `_cutile_python_stub`；`is_stub` 找不到标志即返回 `False`。`function` 表达的是「执行空间」，`stub` 表达的是「需要对接 IR 实现」，两者是正交概念。
  2. 不会。tile code 不被执行，函数体在编译期被忽略；起作用的仍是签名（参数 `x, y, /, *, rounding_mode, flush_to_zero`）。

---

### 4.2 `ImplRegistry` 与 `@impl`：把签名对接到 IR 实现

#### 4.2.1 概念说明

光有 stub 还不够——每个 stub 都得有一个真正的 IR 实现。`ImplRegistry` 就是存放这层映射的容器。它本质上是「stub → 实现」的字典，但额外支持两种增强：

- **重载（overload）**：同一个 stub 可以按操作数类型挂多个实现（如 `operator.add` 对 tile 做算术、对 tuple 做拼接）。
- **线程局部「当前注册表」**：IR 生成是并发安全的（外层有锁），实现内部通过 `ImplRegistry.get_current()` 拿到当前注册表，避免把注册表当参数到处传。

`@impl(some_stub)` 是注册的语法糖：它把被装饰的 Python 函数包一层（加版本检查、记录错误上下文），然后存进注册表。

#### 4.2.2 核心流程

```text
@impl(stub, fixed_args=..., overload=..., min_version=...) 装饰 func
   ├── 若 fixed_args 非空：func = functools.partial(func, *fixed_args)   # 预绑前几个参数
   ├── func_sig = inspect.signature(func)
   ├── _verify_params_match(stub_sig, func_sig)                          # 签名必须对齐
   ├── 包一层 wrapper：_check_version() + 记录 _current_stub.stub_and_args
   └── 存表：
        ├── overload == () → op_implementations[stub] = wrapper          # 唯一实现
        └── overload != () → _overloaded_implementations[stub][overload]  # 重载实现
                             = (priority, predicates, wrapper)
```

`priority`（优先级）= 重载模式里**非 WILDCARD** 的元素个数，用来在多个重载都匹配时选最具体的那一个。

#### 4.2.3 源码精读

`ImplRegistry` 持有三张表，注意 `op_implementations`（唯一实现）与 `_overloaded_implementations`（重载实现，嵌套 dict）的区别：

[`op_impl.py:58-83` —— `ImplRegistry` 的三张表与 `update`/`as_current`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/op_impl.py#L58-L83) `update` 把另一个注册表的全部条目合并进来，`as_current` 是线程局部的上下文管理器。

`@impl` 装饰器是本节核心。重点看三件事：(1) `fixed_args` 用 `functools.partial` 预绑参数，让一个 Python 函数能服务多个 stub；(2) `_verify_params_match` 强制 stub 与 impl 形参个数、名字一一对应；(3) `overload` 是否为空决定存进哪张表：

[`op_impl.py:155-213` —— `impl` 装饰器](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/op_impl.py#L155-L213)

`wrapper` 里有一段「记录当前 stub 与实参」的逻辑，它把 `(stub, stub_sig, func_sig, args, kwargs)` 暂存到线程局部的 `_current_stub`。这样当实现内部对某个 `Var` 抛类型错误时，错误信息能自动补上「这是 `ct.add()` 的第几个参数错了」：

[`op_impl.py:189-200` —— 记录错误上下文](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/op_impl.py#L189-L200)（配合 `_recover_error_context` / `_make_type_error` 在 `op_impl.py:789-819` 一起读）。

签名校验函数：

[`op_impl.py:29-37` —— `_verify_params_match`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/op_impl.py#L29-L37) 形参个数与名字都必须一致。

一个用 `fixed_args` 把同一个实现挂到四个 stub 的典型写法（注意 `fn` 参数被预绑为 `"add"`/`"sub"`/`"mul"`/`"truediv"`）：

[`ops.py:175-183` —— `@impl(ct.add, fixed_args=["add"])` 一组](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L175-L183)

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解 `fixed_args` 如何让「一份实现服务多个 stub」。
2. **步骤**：读 `ops.py:175-183` 的 `binary_arithmetic_impl_with_rd_and_ftz`，它有 5 个形参 `(fn, x, y, rounding_mode, flush_to_zero)`；而 `ct.add` 的签名是 `(x, y, /, *, rounding_mode, flush_to_zero)` 共 4 个形参。
3. **观察**：`fixed_args=["add"]` 用 `functools.partial` 预绑了 `fn="add"`，`inspect.signature(partial)` 会自动扣掉这个已绑参数，于是 impl 的有效签名变成 4 个，与 stub 对齐，通过 `_verify_params_match`。
4. **预期**：你能在脑海里画出来「堆叠四个 `@impl` 装饰器 → 同一个 impl 函数被注册四次，分别绑定不同的 `fn` 字符串」。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 impl 的形参名字必须和 stub 完全一致（`_verify_params_match` 的第二条 assert）？
- **练习 2**：`min_version` 参数什么时候会真正报错？
- **答案**：
  1. 因为错误上下文还原（`_recover_error_context`）需要用 `func_sig.bind(*args)` 把实参绑定到 impl 的形参名，再去 stub 侧查同名参数，从而打印「第几个参数错了」。名字不一致就会对不上。
  2. 在 `wrapper` 执行时，`_check_version` 读 `Builder.get_current().ir_ctx.tileiras_version`；若当前字节码版本 < `min_version`，抛 `TileUnsupportedFeatureError`，提示「需要某版本以上的 tileiras」。它把「特性不可用」从模糊的运行期错误前移成清晰的编译期错误。

---

### 4.3 `tile_impl_registry`：聚合四个子注册表

#### 4.3.1 概念说明

如果只有一个全局 dict，cuTile 上百个 stub 的实现会塞在一个文件里，无法维护。cuTile 的做法是**分而治之**：按主题拆成四个子注册表，每个子模块各自建一个 `ImplRegistry()`、各自在 import 时用 `impl = _registry.impl` 注册，最后在 `ops.py` 里聚合成一张总表 `tile_impl_registry`。

四个子表大致分工：

| 子注册表 | 来源文件 | 内容 |
| --- | --- | --- |
| `core_impl_registry()` | `core_ops.py` | 常量、tuple、`operator.add` 的 tuple 重载、赋值、上下文管理、`binop_overload_dispatcher` 等「核心」语义。 |
| `static_eval_impl_registry()` | `static_eval_ops.py` | `static_eval` / `static_assert` 的编译期实现。 |
| `arithmetic_impl_registry()` | `arithmetic_ops.py` | `operator.add/sub/mul/...` 在 `TensorLikeTy` 上的算术重载、一元算子、比较、where、reshape/broadcast/astype。 |
| `control_flow_impl_registry()` | `control_flow_ops.py` | `Loop` / `IfElse` 等控制流降级实现。 |

#### 4.3.2 核心流程

```text
每个子模块（以 arithmetic_ops.py 为例）：
   _registry = ImplRegistry()
   impl = _registry.impl
   @impl(...)           # import 时即注册到 _registry
   def ...: ...

ops.py 聚合：
   tile_impl_registry = ImplRegistry()
   tile_impl_registry.update(core_impl_registry())
   tile_impl_registry.update(static_eval_impl_registry())
   tile_impl_registry.update(arithmetic_impl_registry())
   tile_impl_registry.update(control_flow_impl_registry())
   impl = tile_impl_registry.impl          # 给 ops.py 自己注册用
```

`update` 是浅合并：同键后者覆盖前者；重载表是「按 stub 再按 overload 键」二级合并。

#### 4.3.3 源码精读

聚合点只有寥寥几行，却是整个前端 IR 生成的「注册中心」：

[`ops.py:78-83` —— `tile_impl_registry` 聚合四个子表](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L78-L83)

子模块的「私有注册表 + 暴露 getter」模式，以算术为例：

[`arithmetic_ops.py:34-39` —— 私有 `_registry` 与 `arithmetic_impl_registry()`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/arithmetic_ops.py#L34-L39) 模块级 `_registry = ImplRegistry()` 在 import 时就被各 `@impl` 填充。

core 子表同理：

[`core_ops.py:34-44` —— `core_impl_registry()`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py#L34-L44)

谁会把 `tile_impl_registry` 设为「当前」？是编译流水线。`compile_tile` 在生成 IR 前，会用 `with tile_impl_registry.as_current():` 进入上下文，让 hir2ir 内部的 `ImplRegistry.get_current()` 能拿到它：

[`_compile.py:394` —— `with tile_impl_registry.as_current():`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L394)（建议结合 u5-l2 的 `_IrKeeper` 阶段一起读）。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：验证「聚合」确实是浅合并、且子表之间没有同键冲突。
2. **步骤**：在 `control_flow_ops.py` / `static_eval_ops.py` 里找到与 `core_ops.py` / `arithmetic_ops.py` 同名的 `*_impl_registry()` 函数；再确认它们各自注册的 stub 集合互不重叠（控制流用 `hir_stubs.loop` 之类，算术用 `operator.*`）。
3. **观察**：四个子表注册的「键空间」天然正交，所以聚合时不会互相覆盖。
4. **预期**：你会理解为什么 cuTile 能安全地拆分——因为每个主题领域各自认领了一组互不冲突的 stub。

#### 4.3.5 小练习与答案

- **练习 1**：如果 `arithmetic_ops.py` 和 `core_ops.py` 都给 `operator.add` 注册了一个 `(TensorLikeTy, TensorLikeTy)` 重载，聚合后会发生什么？
- **练习 2**：为什么每个子模块要自己 `impl = _registry.impl`，而不是直接用全局的 `tile_impl_registry.impl`？
- **答案**：
  1. `update` 对重载表是按 `(stub, overload_key)` 二级合并，若两边的 `overload` 键完全相同，后合并的会覆盖先合并的；若只是「类型重叠但键不同」，则两张重载项都会保留，`_find_overload` 在分派时若发现两个同优先级都命中，会抛 `Multiple matching overloads`。
  2. 为了**解耦导入顺序**。子模块在 import 时就立刻注册，此刻 `ops.py` 里的 `tile_impl_registry` 可能还没创建（循环导入风险）。各模块先写进自己的私有 `_registry`，再由 `ops.py` 主动 `update` 聚合，避免了「注册时总表必须已存在」的强耦合。

---

### 4.4 `overload_dispatcher`：基于类型的重载分派

#### 4.4.1 概念说明

很多 stub 是**多态**的：同一个 `operator.add`，对两个 tile 要做算术，对两个 tuple 要做拼接，对 tile + 标量要先广播。`@impl(stub, overload=...)` 负责声明「我处理这一类操作数类型组合」，而 `overload_dispatcher` 负责在调用时**根据实际操作数类型挑出唯一一个重载**。

它的工作方式很有趣：被 `overload_dispatcher` 装饰的函数本身是一个**生成器**，它先 `yield` 一个「重载键」（通常是 `(type(x_ty), type(y_ty))`），框架拿到键后去 `_overloaded_implementations[stub]` 里 `_find_overload`，找到匹配的实现再去执行。这相当于把「运行期分派」也表达成了一个可注册的 stub 实现。

`WILDCARD` 是「通配符」，在重载模式里表示「这一位匹配任何值」，用来写「兜底」重载。

#### 4.4.2 核心流程

```text
overload_dispatcher(stub) 装饰 key_func
   └── 注册一个 async implementation 到 op_implementations[stub]
        该 implementation：
          key = next(key_func(*args))                  # 让 key_func yield 重载键
          overload_impl = _find_overload(stub, key)    # 按优先级选最优重载
          return overload_impl(*args)

_find_overload(stub, overload_key):
   for (priority, predicates, impl) in candidates:
       若 predicates 全部满足(arg) 且 priority 更高 → 记为最佳
   priority = 重载模式里非 WILDCARD 的个数            # 越具体优先级越高
```

优先级用一个小公式表达：设重载模式为 \(p = (p_1, \dots, p_n)\)，则

\[
\text{priority}(p) = \sum_{i=1}^{n} \mathbb{1}[p_i \neq \texttt{WILDCARD}]
\]

匹配谓词由 `_predicate_from_overload_pattern` 生成：`WILDCARD` → 恒真；`type` → `issubclass`；其它值 → 相等。

#### 4.4.3 源码精读

`overload_dispatcher` 把生成器协作 + `_find_overload` 包装成一个 async impl，注册到 `op_implementations[stub]`（注意：是唯一实现那张表，不是重载表）：

[`op_impl.py:85-127` —— `overload_dispatcher`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/op_impl.py#L85-L127)

`_find_overload` 按 priority 选最具体的匹配；多个同级匹配则报错，无匹配返回 `None`：

[`op_impl.py:129-153` —— `_find_overload`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/op_impl.py#L129-L153)

谓词生成：

[`op_impl.py:227-233` —— `_predicate_from_overload_pattern`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/op_impl.py#L227-L233) 与 [`op_impl.py:40-50` 的 `WILDCARD`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/op_impl.py#L40-L50)。

最漂亮的多态例子：`operator.add` 的分派器 yield 出 `(type(x_ty), type(y_ty))`，由具体重载接手：

[`core_ops.py:52-80` —— `binop_overload_dispatcher`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py#L52-L80) yield 操作数类型对，找不到重载时翻成清晰的 `TileTypeError`。

两个并列的重载——同样挂在 `operator.add` 下，却干完全不同的事：

- 两 tile 相加（算术）：

[`arithmetic_ops.py:487-496` —— `operator.add` 的 `(TensorLikeTy, TensorLikeTy)` 重载](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/arithmetic_ops.py#L487-L496)

- 两 tuple 拼接（`+` 对 tuple 的语义）：

[`ops.py:186-190` —— `operator.add` 的 `(TupleTy, TupleTy)` 重载](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L186-L190) 调 `build_tuple` 拼接两个 tuple 的 items。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：体会「同一个 `+` 走两条完全不同的 IR 路径」。
2. **步骤**：设想内核里有 `t1 + t2`（两 tile）和 `idx + idx`（两整数 tuple）两处加法。两者都被 ast2hir 翻成 `operator.add`（见 4.5）。在 `_overloaded_implementations[operator.add]` 里，前者命中 `(TensorLikeTy, TensorLikeTy)`、后者命中 `(TupleTy, TupleTy)`。
3. **观察**：`_find_overload` 用 `issubclass` 判定——`TileTy` 是 `TensorLikeTy` 子类故命中算术重载；`TupleTy` 命中 tuple 重载。两条路径分别产出 `RawBinaryArithmeticOperation` 与 `build_tuple`。
4. **预期**：你能向别人解释「为什么 cuTile 不需要为 tile 和 tuple 分别定义两个加法操作符」——因为多态在 IR 注册层用重载解决了。

#### 4.4.5 小练习与答案

- **练习 1**：若把 `(TensorLikeTy, TensorLikeTy)` 重载改成 `(WILDCARD, WILDCARD)`，会对 tuple 相加产生什么影响？
- **练习 2**：`_find_overload` 在「两个重载都匹配且优先级相同」时为什么直接报错而不是任选一个？
- **答案**：
  1. priority 从 2 降到 0。这样 tile 相加和 tuple 相加都只有 priority=0 的重载可命中（tuple 重载也是 priority=2），但 tile 仍匹配 `issubclass(TileTy, WILDCARD)` 为真。问题在于：当传入两个 tuple 时，`(WILDCARD, WILDCARD)` 与 `(TupleTy, TupleTy)` 都匹配，而后者 priority 更高（2 > 0），所以 tuple 仍正确走拼接；但传入 tile 时只剩 `(WILDCARD, WILDCARD)` 命中，算术仍能走通。真正的风险是**新增**别的 priority=0 重载时会与之冲突。结论：WILDCARD 应只用于「兜底」，不要随便降低具体重载的优先级。
  2. 因为「两个同级都匹配」通常意味着重载集合设计上有歧义（类型空间相交），静默任选一个会让编译结果不可预测、且难以调试。直接抛 `ValueError` 把问题暴露给开发者，是更安全的工程选择。

---

### 4.5 端到端追踪：从 `ct.add` 到 `RawBinaryArithmeticOperation`

#### 4.5.1 概念说明

把前四节串起来。本节追踪两条真实路径，它们最终都汇流到同一条 IR Operation `RawBinaryArithmeticOperation`：

- **路径 A：`a + b`**（运算符形式）—— 经 ast2hir 的 `_binop_map` 变成 `operator.add`，走重载分派。
- **路径 B：`ct.add(a, b)`**（函数形式）—— 直接是对 `ct.add` stub 的调用，走 `op_implementations[ct.add]`。

两条路径在 `binary_arithmetic_tensorlike` 处汇合，再经 `binary_arithmetic_tensorlike_raw` 发射 `RawBinaryArithmeticOperation`。

#### 4.5.2 核心流程

```text
路径 A: a + b
  ast.BinOp(Add) ──ast2hir._binop_map──> hir.Call(operator.add, [a,b])
        │
        └─> hir2ir: is_supported_builtin_func(operator.add) → _call_builtin
                   → op_implementations[operator.add] = binop_overload_dispatcher
                   → yield (TileTy, TileTy) → _find_overload
                   → _binary_arithmetic_tensorlike_impl("add", x, y)

路径 B: ct.add(a, b)
  ast.Call(ct.add) ──ast2hir._call_expr──> hir.Call(ct.add, [a,b,rm,ftz])
        │
        └─> hir2ir: is_stub(ct.add) → _call_builtin
                   → op_implementations[ct.add] = binary_arithmetic_impl_with_rd_and_ftz
                   → binary_arithmetic_tensorlike("add", x, y, rm, ftz)

两路汇合:
  binary_arithmetic_tensorlike ──(promote+broadcast)──> binary_arithmetic_tensorlike_raw
        └─> add_operation(RawBinaryArithmeticOperation, fn="add", lhs, rhs, ...)
```

#### 4.5.3 源码精读

**路径 A 的起点**——ast2hir 把 `ast.Add` 映射到 `operator.add`：

[`ast2hir.py:472-488` —— `_binop_map` 与 `_binop_expr`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L472-L488)

**路径 B 的起点**——ast2hir 把 `ct.add(a,b)` 当成普通 Call，callee 解析为 stub 对象：

[`ast2hir.py:342-356` —— `_call_expr` 末段：`ctx.call(callee, args, kwargs)`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/ast2hir.py#L342-L356)

**两路共同的分发入口**——hir2ir 的 `_call_function` 区分 stub/内置 vs 用户函数，`_call_builtin` 查注册表：

[`hir2ir.py:230-240` —— `_call_function`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L230-L240) 与 [`hir2ir.py:243-266` —— `_call_builtin`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L243-L266) 注意最后把 `None` 结果兜底成 `loosely_typed_const(None)`，并把实现返回的 `Var` 作为调用结果。

**汇合点 1**——算术实现（做类型提升、广播、常量折叠，然后下沉到 raw）：

[`arithmetic_ops.py:456-484` —— `binary_arithmetic_tensorlike`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/arithmetic_ops.py#L456-L484) 两个操作数都是 loosely typed 常量时走 `binop_propagate_constant` 编译期求值，否则下沉。

**汇合点 2**——发射最终 IR Operation：

[`arithmetic_ops.py:445-453` —— `binary_arithmetic_tensorlike_raw` 调 `add_operation(RawBinaryArithmeticOperation, ...)`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/arithmetic_ops.py#L445-L453)

**终点**——IR Operation 类，它的 `generate_bytecode` 在后端阶段把 `fn="add"` 与 `int/float` 组合翻译成 `encode_AddIOp` / `encode_AddFOp`：

[`arithmetic_ops.py:357-442` —— `RawBinaryArithmeticOperation` 及其 `generate_bytecode`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/arithmetic_ops.py#L357-L442)

#### 4.5.4 代码实践（源码阅读型 · 本讲主实践）

1. **目标**：亲手把 `ct.add(a, b)` 从用户调用一直追到 `RawBinaryArithmeticOperation`，画出注册与分派路径图。
2. **操作步骤**：
   - 在 `_stub.py:3148-3165` 确认 `ct.add` 是 `@stub`，记下其签名 `(x, y, /, *, rounding_mode, flush_to_zero)`。
   - 在 `ops.py:175-183` 找到 `@impl(ct.add, fixed_args=["add"])` 指向的 `binary_arithmetic_impl_with_rd_and_ftz`，确认它把 `rounding_mode`/`flush_to_zero` 解析为常量后调 `binary_arithmetic_tensorlike`。
   - 在 `arithmetic_ops.py:456-484` 跟到 `binary_arithmetic_tensorlike_raw`（445-453），看到 `add_operation(RawBinaryArithmeticOperation, ...)`。
   - 在 `arithmetic_ops.py:357-442` 读 `RawBinaryArithmeticOperation.generate_bytecode`，确认 `("add", "int")` → `encode_AddIOp`、`("add", "float")` → `encode_AddFOp`。
   - 再对照「路径 A」：`ast2hir.py:472-488` → `core_ops.py:52-80`（dispatcher）→ `arithmetic_ops.py:487-496`（重载）→ 同一汇合点。
3. **需要观察的现象**：两条路径在 `binary_arithmetic_tensorlike` 汇合；区别只在前半段——路径 A 多了「运算符→`operator.add`→重载分派」一跳，路径 B 多了「`fixed_args` 绑定 `fn`」一步。
4. **预期结果**：你画出一张图，左路 `a + b`、右路 `ct.add(...)`，在 `binary_arithmetic_tensorlike` 处合并，终点是 `RawBinaryArithmeticOperation`。这张图就是本讲交出的「注册与分派路径」答案。
5. 若想进一步验证：开启 `CUDA_TILE_DUMP_TILEIR`（见 u8-l5），对一个只含 `c = a + b` 的内核 dump IR，应能看到一条 `raw_binary_arith` 操作（opcode 由 `@dataclass(..., opcode="raw_binary_arith")` 决定）。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `ct.add(a, b)` 的实现 `binary_arithmetic_impl_with_rd_and_ftz` 比 `a + b` 的 `_binary_arithmetic_tensorlike_impl` 多了 `rounding_mode` / `flush_to_zero` 两个参数？
- **练习 2**：若用户写 `a + b`，但 `a`、`b` 都是编译期常量，会走到 `RawBinaryArithmeticOperation` 吗？
- **答案**：
  1. 因为 `ct.add` 这个 stub 的签名里**显式**带了 `rounding_mode` 与 `flush_to_zero` 关键字参数（`_stub.py:3150-3151`），impl 必须接收并解析它们；而 `operator.add` 是 Python 内置二元运算符，签名只有 `(x, y)`，没有这两个旋钮，所以它的重载 impl 不需要这两个参数。这正是 `fixed_args` 之外，stub 与 impl 签名必须严格对齐的体现。
  2. 不会。`binary_arithmetic_tensorlike` 在 `arithmetic_ops.py:467-468` / `481-482` 检查到两个操作数都是常量时，直接走 `binop_propagate_constant` 在编译期算出结果（常量折叠），返回一个 `loosely_typed_const` / `strictly_typed_const`，根本不会发射 `RawBinaryArithmeticOperation`。只有至少一个非常量时才下沉到 raw 操作。

---

### 4.6 `build_tuple` 与 `result_var` 复用入口变量的写法

#### 4.6.1 概念说明

最后讲两个「写 impl 时很常用」的模式。

**`build_tuple`** 是把一组 `Var` 打包成 tuple 聚合值的 IR 构造助手。它对应两种触发场景：

- 用户在 tile code 里写 `(a, b)` 这种 tuple 字面量——ast2hir 识别出 `tuple(...)` 内置调用后，发射 `hir_stubs.build_tuple`，其 impl 是 `build_tuple_impl`。
- impl 内部需要构造 tuple 结果（如 `operator.add` 的 tuple 重载做拼接、或 reduce 返回「值 + 下标」）——直接调用 Python 函数 `build_tuple(items)`。

注意区分：`hir_stubs.build_tuple` 是 HIR 层的「桩」（代表「构造一个 tuple」这个语法动作），`ct` 里并没有 `ct.build_tuple` 这个用户 API；`build_tuple`（`core_ops.py:291`）是 IR 构造函数；`build_tuple_impl`（`core_ops.py:302`）是 `hir_stubs.build_tuple` 的注册实现，它转调 `build_tuple`。

**`result_var` 模式**：`Builder.add_operation` / `make_aggregate` / `loosely_typed_const` / `build_tuple` 都接受一个可选的 `result` / `result_var` 参数。默认情况下 Builder 会新建一个临时 `Var` 作为操作结果；但若传入 `result_var=existing_var`，则**复用这个已存在的入口变量**作为结果，而不是新建。这在 tuple 解包、phi 合并等场景里很有用——可以让「某个 block 入口参数」直接成为某条操作的结果，省掉一次赋值。

#### 4.6.2 核心流程

```text
build_tuple(items, result_var=None):
   ty       = TupleTy(每个 item 的 type)
   loose_ty = TupleTy(每个 item 的 loose type)
   res = Builder.make_aggregate(TupleValue(items), ty, loose_ty, result_var=result_var)
   if 全是常量: res.set_constant(tuple(...))
   return res

make_aggregate(value, ty, loose_ty, result_var=None):
   if result_var is None: result_var = ir_ctx.make_temp()   # 默认新建临时
   result_var.set_type(ty); result_var.set_loose_type(...); result_var.set_aggregate(value)
   return result_var                                        # 复用入口变量时直接返回它
```

#### 4.6.3 源码精读

`build_tuple` 构造 tuple 类型与聚合值，常量情况下顺带标记为常量：

[`core_ops.py:291-299` —— `build_tuple` 接受 `result_var`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py#L291-L299)

`build_tuple_impl` 注册到 HIR 桩 `hir_stubs.build_tuple`（即 tuple 字面量的 impl），转调上面的 `build_tuple`：

[`core_ops.py:302-304` —— `@impl(hir_stubs.build_tuple)`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py#L302-L304)

`loosely_typed_const` 同样接受 `result_var`，把它一路传到 `add_operation(..., result=result_var)`：

[`core_ops.py:207-224` —— `loosely_typed_const` 的 `result_var` 透传](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py#L207-L224) 与 [`core_ops.py:236-270` —— `_strictly_typed_const_inner` 末尾 `add_operation(..., result=result_var)`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/core_ops.py#L236-L270)

底层 `Builder._add_operation` 与 `make_aggregate` 对 `result` 的处理——`None` 则新建临时 `make_temp`，否则复用传入变量：

[`ir.py:370-407` —— `_add_operation` 区分新建/复用](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L370-L407) 与 [`ir.py:409-424` —— `make_aggregate(result_var=...)`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L409-L424)

#### 4.6.4 代码实践（源码阅读型）

1. **目标**：看清 tuple 字面量是如何变成 IR 的，并理解 `result_var` 的复用语义。
2. **步骤**：设想内核里写 `p = (a, b)`。ast2hir 在 `_call_expr` 里识别到 `_is_builtin_tuple(call.func)`（`ast2hir.py:343-351`），发射 `ctx.call(hir_stubs.build_tuple, (a, b))`。hir2ir 走 `_call_builtin(hir_stubs.build_tuple, ...)` → `op_implementations[hir_stubs.build_tuple]` = `build_tuple_impl` → `build_tuple((a, b))`。
3. **观察**：在 `build_tuple` 里若所有 items 都是常量，结果 `Var` 会被 `set_constant`，下游就能做常量折叠。再读 `make_aggregate` 的 `result_var` 分支：传 `None` 时 `ir_ctx.make_temp()` 造一个全新临时 Var；传入已有 Var 时直接 `set_type`/`set_aggregate` 到它上面。
4. **预期**：你能解释「为什么 impl 偶尔要主动传 `result_var`」——为了让某个 block 入口参数（phi 用）或已存在的变量直接充当结果，避免多一条赋值/移动。

#### 4.6.5 小练习与答案

- **练习 1**：`hir_stubs.build_tuple` 和 `ct.add` 都是「桩」，但前者不出现在用户文档里。为什么？
- **练习 2**：`build_tuple` 在什么情况下会顺带 `set_constant`？这对后续优化有什么好处？
- **答案**：
  1. `hir_stubs.build_tuple` 是 **HIR 层的桩**，代表「构造 tuple」这一语法动作，由 ast2hir 在翻译 `(a, b)` 字面量或 `tuple(...)` 时自动发射，用户无法、也不需要直接「调用」它；而 `ct.add` 是**用户层桩**，是公开 API。两者都靠 `@impl` 对接 IR 实现，只是面向的「调用者」不同——一个是 ast2hir，一个是写内核的人。
  2. 当 `all(x.is_constant() for x in items)` 为真时（`core_ops.py:297`）。好处是：得到一个常量 tuple 后，`binop_propagate_constant`、`require_constant_int_tuple`、`require_constant_scalar_tuple` 等都能在编译期直接读出它的值，触发常量折叠与静态校验（如 `static_assert`、shape 的 2 的幂检查），省掉运行期计算。

---

## 5. 综合实践

把本讲全部知识串成一个「自造 stub 并观察分派」的源码阅读任务（**纯阅读，不改源码、不运行**）：

**背景**：假设你想给 cuTile 新增一个 stub `ct.fma(x, y, z)`（融合乘加，`x*y+z`），但不真去实现它，只是规划。

**任务**：

1. **写签名**：参照 `ct.add`（`_stub.py:3148-3165`），写出 `ct.fma` 的 `@stub` 签名，决定要不要 `rounding_mode` / `flush_to_zero`。
2. **选注册表**：判断 `ct.fma` 的实现应注册到哪个子表（大概率是 `arithmetic_ops.py`），并用 `@impl(ct.fma, ...)` 写出 impl 的形参（记得 stub 与 impl 签名要过 `_verify_params_match`）。
3. **设计重载（可选）**：若希望 `ct.fma` 同时支持 tile 与标量常量，参照 `operator.add` 的重载写法，思考要不要用 `overload=` 还是直接在 impl 里用 `is_constant()` 分支（参考 `binary_arithmetic_tensorlike` 的常量折叠分支）。
4. **追踪到 IR**：设想 impl 内部最终调用一个虚构的 `add_operation(FmaOperation, ...)`，对照 `RawBinaryArithmeticOperation`（`arithmetic_ops.py:357-442`）写出 `FmaOperation` 应有的 `operand()` / `attribute()` 字段与 `generate_bytecode` 分支。
5. **画总图**：把「用户调用 `ct.fma(...)` → ast2hir → hir2ir `_call_builtin` → `op_implementations[ct.fma]` → impl → `add_operation(FmaOperation)` → 字节码」画成一张序列图，标注每一步对应的源码行号。

**交付物**：一张序列图 + 一段说明「`ct.fma` 与 `a*b+c`（假如后者也被支持）会在哪里汇合、在哪里分叉」。这个练习综合了 `@stub`、`@impl`、签名校验、重载、`add_operation` 与字节码生成全部六个最小模块。

> 提示：本练习是「设计型源码阅读」，重在把注册与分派机制讲清楚，**不要真的去改 `cext` 或源码**。若想验证思路，可去 `test/` 目录找已有 stub 的测试，对照看它的 impl 是怎么注册的。

## 6. 本讲小结

- `@stub` 给函数贴 `_cutile_python_stub = True`，本质是「签名 + 文档」的占位；`is_stub` 沿 `__wrapped__` 链判定。stub 函数体在编译期不被执行。
- `ImplRegistry` 用 `op_implementations`（唯一实现）与 `_overloaded_implementations`（重载实现）两张表存「stub → impl」映射，外加 `unflatten_aggregate_implementations` 处理聚合类型；通过线程局部的 `get_current()` / `as_current()` 提供「当前注册表」。
- `@impl(stub, fixed_args=, overload=, min_version=)` 是注册语法糖：`fixed_args` 用 `partial` 预绑参数让一份 impl 服务多个 stub；`overload` 决定存哪张表；`_verify_params_match` 强制签名对齐；wrapper 还记录错误上下文与做版本门控。
- `tile_impl_registry` 在 `ops.py` 聚合 core / static_eval / arithmetic / control_flow 四个子表，子表键空间正交、import 时各自注册、最后浅合并。
- `overload_dispatcher` + `WILDCARD` 实现「按操作数类型分派」：被装饰的生成器 yield 重载键，`_find_overload` 按优先级（非 WILDCARD 个数）选最具体的重载；`operator.add` 对 tile 做算术、对 tuple 做拼接是多态典范。
- `ct.add(a,b)` 与 `a+b` 两条路径在 `binary_arithmetic_tensorlike` 汇合，终点是 `RawBinaryArithmeticOperation`；常量操作数会在编译期折叠，不发射 raw 操作。
- `build_tuple` / `loosely_typed_const` 的 `result_var` 参数可复用入口变量充当结果，`Builder._add_operation` / `make_aggregate` 在 `result is None` 时才新建临时 Var。

## 7. 下一步学习建议

- **向下游走（后端）**：本讲止步于「发射 `Operation`」。这些 Operation 如何被编码成字节码，见 **u7-l1（IR 到字节码：ir2bytecode）**——`RawBinaryArithmeticOperation.generate_bytecode` 里调用的 `encode_AddIOp` 就在那里被消费。
- **向优化走**：发射出来的 IR 会进优化 pass 流水线。算术相关的典型优化是 FMA 融合，见 **u6-l4（代码外提、循环分裂与模式重写）** 里的 `rewrite_patterns`。
- **向前端上游走**：本讲频繁引用的「ast2hir 如何把 `a+b` 翻成 `operator.add`」「tuple 字面量如何翻成 `hir_stubs.build_tuple`」，系统讲解在 **u5-l3（AST 到 HIR）**；hir2ir 的分派与内联全貌在 **u5-l4（HIR 到 IR）**。
- **类型侧补完**：重载分派依赖 `TensorLikeTy` / `TupleTy` 等类型层级，以及 `Var.get_type()` / `get_loose_type()`，见 **u5-l6（类型系统）**。
- **动手验证**：想亲眼看到 `raw_binary_arith` 这条 IR，配合 **u8-l5（调试与性能工具）** 的 `CUDA_TILE_DUMP_TILEIR` 一起做。
