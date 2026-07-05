# HIR 到 IR：hir2ir

## 1. 本讲目标

上一讲（u5-l3）我们看到，ast2hir 把 Python 内核翻译成了 HIR——一种「万物皆 Call」的归一化中间表示。HIR 离真正的 GPU 代码还很远：它没有具体的操作定义，连 `a + b` 都只是对 `operator.add` 的一次调用。

本讲要回答的问题是：**这些「通用 Call」是怎么变成具体的 Tile IR Operation 的？**

学完本讲，你应该能够：

1. 理解 `hir2ir` 是一个「HIR 解释器」：它遍历 HIR 的 Block/Call，按被调用对象的类型把 Call 分派（dispatch）成具体的 IR Operation，写进 `Builder`。
2. 理解用户自定义函数（`function` 装饰的、或闭包）是如何被**内联（inline）**进调用者的，以及为何用协程（coroutine）来实现以绕开 Python 递归上限。
3. 理解 `for`/`while` 循环如何被降级（lower）成一个带 body 的 `Loop` Operation，其中循环体内被改写的局部变量如何变成「携带值（carried values）」。
4. 理解 `if/else` 如何降级成 `IfElse` Operation，以及编译期常量条件如何被直接展平（flatten）。
5. 理解 `PhiState` 如何在控制流汇合点（循环回边、if-else 汇合）做类型统一与常量传播。

本讲覆盖四个最小模块：`hir2ir`（主分派器）、`loop_impl`（循环降级）、`if_else_impl`（分支降级）、`PhiState`（汇合点的 phi 状态）。

## 2. 前置知识

### 2.1 HIR 的结构（来自 u5-l3）

简要回顾 HIR 的核心构件（定义在 [src/cuda/tile/_ir/hir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py)）：

- `hir.Value`：一个带 `id` 的值引用，形如 `%3`，代表「某次 Call 的结果」或「一个内核参数」。
- `hir.Call`：一次函数调用，含 `callee`（被调用对象）、`args`/`kwargs`、`result`（结果 Value 或 None）。
- `hir.Block`：一个线性指令序列，由一串 `Call` + 一个终止 `Jump`（`END_BRANCH`/`CONTINUE`/`BREAK`/`RETURN`）组成；它还记录 `params`（块参数）和 `stored_indices`（块内被赋值的局部变量下标集合）。
- `hir.Function`：一个函数定义，含 `body: Block`、`local_names`（所有局部变量名）、`param_local_indices`（每个参数对应 `local_names` 的下标）。

控制流在 HIR 里也是 Call：`if/else` 是对 `hir_stubs.if_else` 的 Call（两个参数是 then/else 的 Block），`for/while` 是对 `hir_stubs.loop` 的 Call（参数是 body Block 和 iterable）。这些「控制流 stub」定义在 [src/cuda/tile/_ir/hir_stubs.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir_stubs.py)。

### 2.2 IR 的核心构件（预告 u5-l5）

IR（定义在 [src/cuda/tile/_ir/ir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py)）比 HIR 具体：

- `Var`：一个 SSA 风格的值，有名字、位置、类型（存在 `IRContext.typemap` 里），还可以是「常量」「聚合值」。
- `Operation`：所有具体 IR 操作的基类，用 `operand()`/`attribute()`/`nested_block()` 三种字段声明输入，子类用 `opcode="..."` 注册自己的助记符。
- `Block`：IR 指令序列的容器，有 `params`（块入口参数）和 `operations`。
- `Builder`：线程局部（`threading.local`）的「当前构建器」，`add_operation(...)` 把一个 Operation 追加到当前 Block。hir2ir 期间始终有一个 active Builder。
- `IRContext`：跨整个内核的上下文，保管所有 `Var` 的类型/常量/聚合值，并提供 `make_var`/`make_temp`。

### 2.3 SSA 与 phi 节点

静态单赋值（SSA）要求每个变量只被赋值一次。当控制流汇合时（循环回到开头、if-else 两支合并），同一个「逻辑变量」可能有多条不同的到达定义。phi 节点就是在汇合点「按来路选值」的虚拟操作。cuTile 没有显式的 phi 指令，而是把汇合语义编码进「块入口参数 + PhiState 类型/常量统一」，效果等价于函数式 fold——这一点是理解循环降级的关键。

### 2.4 协程与软件栈

`hir2ir` 用 Python 的 `async`/`await` 实现了一个**软件调用栈**：每内联一层用户函数，不是真的递归调用 Python 函数（那会很快撞上 Python 默认的 ~1000 层递归上限），而是 `await resume_after(...)` 把继续工作挂起、让外层协程驱动器循环推进。你只需记住：源码里到处是 `async def` 和 `await`，但它们在这里不是为了并发，而是为了**用协程模拟一个可中断的递归栈**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/cuda/tile/_passes/hir2ir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py) | hir2ir 主入口与核心：Block 遍历、Call 分派、用户函数内联、被调用对象类型分派。 |
| [src/cuda/tile/_ir/control_flow_ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py) | 控制流相关的 IR Operation（`Loop`/`IfElse`/`Continue`/`Break`/`EndBranch`/`Return`）及其 `@impl` 实现（`loop_impl`/`if_else_impl`）。 |
| [src/cuda/tile/_ir/scope.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/scope.py) | 名称作用域：`Scope`（当前函数的编译状态）、`LocalScope`（局部变量槽）、`ControlFlowInfo`/`JumpInfo`（收集循环/分支内的跳转）。 |
| [src/cuda/tile/_ir/ir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py) | IR 核心：`Var`/`Operation`/`Block`/`Builder`，以及本讲重点的 `PhiState` 与 `LoopVarState`。 |
| [src/cuda/tile/_ir/hir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/hir.py) | HIR 数据结构（u5-l3 已详解，本讲作为输入引用）。 |

## 4. 核心概念与源码讲解

### 4.1 hir2ir：把 HIR「解释」成 IR 的主分派器

#### 4.1.1 概念说明

`hir2ir` 本质上是一个**遍历 HIR、发射 IR 的解释器**。它没有「编译」的复杂感，更像在按顺序「执行」HIR：

- 遇到一个 `hir.Block`，就逐条遍历它的 `Call`，再把末尾的 `Jump` 翻成对应的终止操作。
- 遇到一个 `hir.Call`，就解析出被调用对象和实参，然后根据被调用对象的**类型**决定怎么处理：是查 `ImplRegistry` 调内置实现？还是内联一个用户函数？还是构造一个 dtype？
- 内置实现（`@impl` 注册的函数，如 `loop_impl`、`if_else_impl`、各种算术 op）会调用 `Builder.add_operation(...)`，把具体 Operation 写进当前 Block。

这套机制把「HIR 的通用 Call」和「IR 的具体 Operation」解耦：HIR 只管「谁调用谁」，具体「翻译成什么 Operation」由 `@impl` 注册表决定（u5-l7 会专门讲注册表，本讲只用到结论）。

#### 4.1.2 核心流程

整个 hir2ir 的执行流程可以画成：

```
hir2ir(func_hir, param_vars, ir_ctx)
  └─ run_coroutine(_hir2ir_coroutine(...))        # 用协程驱动，绕开 Python 递归上限
       └─ _create_scope(...)                       # 为本函数建一个 Scope（局部变量槽）
       └─ 把内核参数 Var 绑定到 local_names 的下标
       └─ _dispatch_hir_block_inner(func_hir.body) # 遍历函数体 Block
            ├─ for call in block.calls:
            │     _dispatch_call(call)             # 解析实参 → call() 分派
            │     if builder.is_terminated: return # 遇到展平的常量分支可提前结束
            └─ _dispatch_hir_jump(block)           # 处理末尾的 END_BRANCH/CONTINUE/BREAK/RETURN
```

`_dispatch_call` 的核心是 [`call(callee_var, args, kwargs)`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L273-L320)，它按 `callee_var.get_type()` 的类型分派：

| 被调用对象类型 | 处理方式 |
|----------------|----------|
| `FunctionTy`（普通函数引用） | 走 `_call_function`：stub/内置 → `_call_builtin`；否则 → 内联用户函数 |
| `BoundMethodTy`（绑定方法） | 把 `bound_self` 拼到实参最前面，再当普通函数调用 |
| `DTypeConstructor`（如 `ct.float32(...)`） | 生成 `dtype_constructor` 操作 |
| `ClosureTy`（闭包/嵌套函数） | 内联，并按需冻结捕获的外层变量 |
| `TypeTy` + dataclass | 构造一个 dataclass 实例（`build_dataclass_instance`） |
| `TypeTy` + Enum | 编译期求值枚举值 |

`_call_function` 内部的分流很关键：[src/cuda/tile/_passes/hir2ir.py:L236-L240](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L236-L240)——如果被调用对象是 stub（用户 API，如 `ct.add`）或受支持的内置函数，走 `_call_builtin` 查注册表；否则取它的 HIR 并内联。

#### 4.1.3 源码精读

**入口与协程驱动**。`hir2ir` 只是个薄壳，真正的工作在协程里。注意它先建 `Scope`、再把参数 Var 绑定到局部变量槽，然后分派函数体：

[src/cuda/tile/_passes/hir2ir.py:L38-L63](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L38-L63) —— `hir2ir` 用 `run_coroutine` 驱动 `_hir2ir_coroutine`；协程内创建 scope、用 `zip(..., strict=True)` 把每个参数 Var 写进 `scope.local[local_idx]`，再分派 `func_hir.body`。出错时若开启了 `log_ir_on_error`，会把已生成的部分 IR 打到 stderr，方便调试。

**Block 遍历**。逐条分派 Call，并注意「提前终止」：当一个 Call 的实现把当前 Block 终结了（典型：`if True: break` 这种常量条件被展平），就不再处理后面的 Call：

[src/cuda/tile/_passes/hir2ir.py:L100-L127](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L100-L127) —— `_dispatch_hir_block_inner` 用 `cursor` 记录当前 Call 下标（仅为错误打印定位），每条 Call 都用 `_wrap_exceptions(loc)` + `builder.change_loc(loc)` 包裹，保证报错信息和 IR 里的行号正确。

**Jump 翻译**。Block 末尾的 `Jump` 映射到对应的终止操作函数：

[src/cuda/tile/_passes/hir2ir.py:L130-L143](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L130-L143) —— `END_BRANCH`→`end_branch(...)`、`CONTINUE`→`continue_()`、`BREAK`→`break_()`、`RETURN`→`return_(...)`。这些函数（见 4.2/4.3）并不立即「跳走」，而是把信息记录进当前 `ControlFlowInfo`，留给外层 `loop_impl`/`if_else_impl` 收尾。

**Call 分派与实参解析**。`_dispatch_call` 解析实参（含 `*args`/`**kwargs` 的展开），再调 `call()`：

[src/cuda/tile/_passes/hir2ir.py:L157-L188](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L157-L188) —— 注意 `Starred`（`*x`）要求展开对象是 `TupleTy`，`**x` 要求是 `DictTy`；解析后调用 `call(callee_var, args, kwargs)`，结果回填到 `scope.hir2ir_varmap[hir_call.result.id]`，这样后续引用该 Value 时就能查到对应的 IR `Var`。

**类型分派总入口**。[src/cuda/tile/_passes/hir2ir.py:L273-L320](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L273-L320) 是 `call()` 的完整逻辑，对应 4.1.2 表格中的全部情况。

#### 4.1.4 代码实践

**实践目标**：在不看 IR 的情况下，仅凭源码预测一个简单表达式语句的分派路径。

**操作步骤**：

1. 阅读内置调用入口 [src/cuda/tile/_passes/hir2ir.py:L243-L266](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L243-L266)（`_call_builtin`），看清它如何从 `ImplRegistry.op_implementations[callee]` 取实现并调用。
2. 假设有内核 `y = ct.load(...); z = ct.add(y, 1); ct.store(...)`，跟踪 `ct.add(y, 1)` 这条 Call：
   - `_dispatch_call` 解析出 `callee_var` 的类型是 `FunctionTy`（因为 `ct.add` 是 stub）；
   - `call()` → `_call_function` → `is_stub(callee)` 为真 → `_call_builtin`；
   - `_call_builtin` 在注册表里查到 `add` 的实现并调用，实现内部 `add_operation(...)` 发射一条算术 IR Operation。
3. **需要观察的现象**：被调用对象是 stub 时走的是「查表发射单条 Operation」，而不是内联。

**预期结果**：能在脑中画出 `ct.add(a, b)` 的路径为 `_dispatch_call → call → _call_function → _call_builtin → ImplRegistry 查表 → add_operation`。这是一个纯源码阅读型实践，无需运行 GPU（待本地验证：可选地开启 `CUDA_TILE_LOGS=log_cutile_ir` 观察实际生成的 IR 里是否出现对应的算术 op）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hir2ir` 要用协程（`async`/`await`）而不是普通递归？

**参考答案**：因为用户函数会被**内联**进调用者（见 4.2 与下面的 `_call_user_defined`），深嵌套调用会很快撞上 Python 默认的递归上限。用协程实现的「软件栈」（`run_coroutine`/`resume_after`）把每一层内联变成一次可挂起、可恢复的步进，从而绕开 Python 栈深度限制。源码注释也写明：*“Run as a coroutine using a software stack, so that we don't exceed Python's recursion limit.”*（[src/cuda/tile/_passes/hir2ir.py:L41-L42](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/hir2ir.py#L41-L42)）。

**练习 2**：`_dispatch_hir_block_inner` 里 `builder.is_terminated` 为真时为什么可以直接 `return`？

**参考答案**：某些 `@impl` 实现会终结当前 IR Block——最典型的是 `if_else_impl` 在条件为编译期常量时直接展平被选中的分支（见 4.3.3），如果该分支以 `break` 结尾，就会把 Block 标记为 terminated。此时 HIR 里该 Block 后续的 Call 已经不可能执行，继续分派没有意义，故提前返回。

---

### 4.2 loop_impl：把 for/while 循环降级成 Loop Operation

#### 4.2.1 概念说明

在 HIR 里，`for _ in range(n): ...` 被表达成一次对 `hir_stubs.loop(body, iterable)` 的 Call。`loop_impl` 就是这个 stub 的 `@impl` 实现，负责把循环降级成一条具体的 [`Loop` IR Operation](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L33-L103)。

`Loop` Operation 的字段是理解一切的钥匙：

```
Loop:
    start, stop, step        # for 循环的 range 三元组（while 循环时为 None）
    initial_values: (Var,..) # 进入循环前，每个「携带值」的初值
    body: Block              # 循环体，参数为 (归纳变量, *携带值)
```

为什么需要 `initial_values` 和「携带值」？因为 **tile 是不可变的**（u2-l3），循环体内对一个名字的「重新赋值」`xi += 1` 不能真的改 `xi`，而是产生了一个新值。这个新值要带到「下一轮循环」和「循环结束后」，就必须作为循环的「携带值（carried value）」流动——这本质上把循环编译成了一个**函数式 fold**：

\[
\text{acc}_0 = \text{initial\_value},\qquad
\text{acc}_{i+1} = \text{body}(\text{induction}_i,\ \text{acc}_i),\qquad
\text{result} = \text{acc}_N
\]

哪些局部变量会成为携带值？正是 HIR `Block.stored_indices`——即「循环体内被赋值过的局部变量下标集合」。`loop_impl` 用它来确定携带值的个数与顺序。

#### 4.2.2 核心流程

`loop_impl` 的工作可分为「准备 → 分派 body → 收尾」三段：

```
loop_impl(body, iterable):
  1. 特例：若 iterable 为 None（while）且 body 末尾只有一个 break、无嵌套跳转
        → 这是 ast2hir 为「支持提前 return」而自动套上的外层循环，可整体展平删除。
  2. 找出携带值：stored_locals = sorted(body.stored_indices)
     为每个携带值建 LoopVarState(body_phi, result_phi) 和初值 initial_values。
  3. for 循环特有：建归纳变量 induction_var；把初值传播给 result_phi（应对 0 次迭代）。
  4. 进入嵌套 Block（enter_nested_block），在其中：
       - 为每个携带值在 body 作用域里 redefine 出「本轮的 body_var」；
       - 分派 body Block → 把 IR Operation 写进 new_body；
       - body 里的 continue/break 会被记录进 loop_info.jumps。
  5. 收尾：把 continue/break 携带的输出传播回 body_phi/result_phi；
       用 mask 过滤掉类型非法的变量；展平聚合；回填 Continue/Break 操作的实参。
  6. add_operation(Loop, ...) 生成最终的 Loop Operation；
       把结果 unflatten 后 store_var 回外层作用域的局部槽。
```

注意第 1 步那个「特例」很关键：ast2hir 会给每个 helper 函数体外套一个 `while True: ... break` 形式的循环，目的是支持函数体里出现 `return`（把 return 编译成 break）。如果函数体实际没有提前 return，`loop_impl` 就把这个无意义的循环展平掉，避免多套一层 `Loop`。

#### 4.2.3 源码精读

**Loop Operation 的形状**。先看数据结构和它的可读打印，建立直观印象：

[src/cuda/tile/_ir/control_flow_ops.py:L33-L52](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L33-L52) —— `Loop` 用 `operand()` 声明 `start/stop/step/initial_values`，用 `nested_block()` 声明 `body`；`is_for_loop` 由 `start is not None` 判定；`induction_var` 是 body 的第 0 个参数，`body_vars` 是其余参数（携带值）。

[src/cuda/tile/_ir/control_flow_ops.py:L85-L103](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L85-L103) —— `_to_string_rhs` 决定了 dump 出来的样子：for 循环打印成 `for <ind> in range(start, stop, step) (with <body_var> = <init_var>, ...)`，while 循环打印成 `loop (with ...)`。这条信息在 4.2.4 的实践中会直接用到。

**携带值的准备与归纳变量**：

[src/cuda/tile/_ir/control_flow_ops.py:L121-L139](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L121-L139) —— `stored_locals = tuple(sorted(body.stored_indices))` 确定携带值；为每个建 `LoopVarState(PhiState(NONCONSTANT), PhiState())`；`initial_values` 从外层作用域取这些名字的当前 Var；for 循环额外建一个 `induction_var`（类型取自 `range_ty.dtype`）并映射回 `body.params[0]`。注意 for 循环把初值传播给 `result_phi`——这是因为 for 循环可能 0 次迭代，此时结果就等于初值。

**分派循环体**。在嵌套 Block 里，每个携带值被 `redefine` 成「本轮的新 Var」（SSA！），然后才分派 body：

[src/cuda/tile/_ir/control_flow_ops.py:L141-L159](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L141-L159) —— `enter_nested_block(body_loc)` 开一个新 Builder/Block；`scope.change_loop_info(loop_info)` 让 body 内的 `continue_()`/`break_()` 知道往哪个 `ControlFlowInfo` 记录；`scope.local.enter_branch()` 保护外层局部槽（分支内对局部的改写不泄漏到外层，详见 4.4.3）。`state.body_phi.propagate(initial_var, allow_loose_typing=False)` 把初值类型喂给 body_phi，`redefine` 产出本轮 body_var。

**收尾：传播、过滤、回填、建 Operation**。这是最长也最精细的一段，核心是「让携带值在 continue/break/初值/结果之间类型一致」：

[src/cuda/tile/_ir/control_flow_ops.py:L161-L210](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L161-L210) —— 遍历 `loop_info.jumps`（每个 continue/break），把其输出传播给 `body_phi`（仅 continue）/`result_phi`；用 `mask` 标记类型仍合法的变量，对聚合类型做展平（`flatten_block_parameters`/`flatten_aggregates`），再回填到各 Continue/Break Operation 的 `values`。类型非法的「未定义」初值/输出用 `MakeDummy` 占位（见 [src/cuda/tile/_ir/control_flow_ops.py:L546-L573](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L546-L573)）。

[src/cuda/tile/_ir/control_flow_ops.py:L212-L247](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L212-L247) —— 真正 `add_operation_variadic(Loop, ...)` 生成 Operation，结果经 `unflatten_aggregates` 还原成携带值形状，最后 `store_var(local_idx, res, ...)` 把循环结果写回外层作用域——这一步等价于「循环结束后，名字 `xi` 指向最终累加结果」。

**continue/break 的实现**。它们看似简单，实则是「登记跳转信息」而非「直接跳」：

[src/cuda/tile/_ir/control_flow_ops.py:L446-L459](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L446-L459)（`continue_`）与 [src/cuda/tile/_ir/control_flow_ops.py:L478-L493](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L478-L493)（`break_`）—— 两者都先调 `exit_callback` 收尾上下文管理器，再 `add_operation_variadic(Continue/Break, ...)` 发射终止 Operation，最后把「当前各携带值的 Var」打包成 `JumpInfo` 追加进 `loop_info.jumps`。这些 `JumpInfo.outputs` 正是上面收尾阶段被传播和回填的对象。

#### 4.2.4 代码实践

**实践目标**：阅读一个真实循环内核，**仅凭源码**预测它降级出的 `Loop` Operation 形状（归纳变量、携带值、初值）。

**操作步骤**：

1. 阅读 [test/test_control_flow.py:L17-L24](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_control_flow.py#L17-L24) 的内核 `plus_n_one_arg`：
   ```python
   @ct.kernel
   def plus_n_one_arg(x, n, tile: ct.Constant[int]):
       i = ct.bid(0)
       xi = ct.load(x, index=(i,), shape=(tile,))
       for _ in range(n):
           xi += 1
       ct.store(x, index=(i,), tile=xi)
   ```
2. 分析循环体 `xi += 1`：`xi` 是循环体内唯一被重新赋值的局部 → `body.stored_indices = {xi 的下标}` → 携带值有 **1 个**，初值是 `ct.load` 的结果 tile。
3. 这是 `for _ in range(n)`，所以是 for 循环：有一个归纳变量（即 `_`，但内核里没用到，仅作计数），`start=0`、`stop=n`、`step=1`。
4. 对照 [src/cuda/tile/_ir/control_flow_ops.py:L85-L103](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L85-L103) 的打印格式，预测 dump 里会出现类似：
   ```
   <result> = for <ind> in range(<0>, <n>, <1>) (with <xi_body> = <xi_load>)
   do (<ind>, <xi_body>):
       <xi_body_new> = add <xi_body>, 1
       continue <xi_body_new>
   ```
   其中 `xi_body`（body 入口参数）= 携带值；`xi_load`（来自 `ct.load`）= 初值；`add` 的结果经 `continue` 回传成下一轮的 `xi_body`。

**需要观察的现象**：循环被编译成一个 fold——携带值 `xi` 作为 body 的块参数流入，每轮由 `add` 产生新值再 `continue` 回去。

**预期结果**：能指出「归纳变量 = range 计数器（内核里是 `_`）」「携带值 = `xi`，初值 = `ct.load(x, ...)` 的结果」。**待本地验证**：设置 `CUDA_TILE_DUMP_TILEIR=1` 运行该内核（参见 [src/cuda/tile/_debug.py:L10](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_debug.py#L10)），在 dump 出的 IR 里定位 `for ... in range(...)` 行，确认携带值与初值与预测一致。

#### 4.2.5 小练习与答案

**练习 1**：for 循环为什么要在第 3 步把「初值传播给 `result_phi`」（[src/cuda/tile/_ir/control_flow_ops.py:L132-L134](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L132-L134)）？

**参考答案**：因为 for 循环可能 0 次迭代（`range(0, 0)`）。此时循环体一次都不执行，没有任何 continue/break 输出能传播给 `result_phi`，循环结果就必须回退到初值。提前把初值传播给 `result_phi`，保证 0 次迭代时携带值的类型/常量信息仍然有来源，结果就是初值本身。

**练习 2**：`break` 在 for 循环里为什么不生效（u3-l3 提到 for 不支持 break）？从源码看 `loop_impl` 是否天然支持 break？

**参考答案**：从 `loop_impl` 本身看，它**完全支持** break——`break_()` 会发射 `Break` Operation 并登记 `JumpInfo`，收尾阶段会把 break 的输出传播给 `result_phi`。for 循环「不支持 break」是**前端 ast2hir 层面**的限制（for 被编成定数计数 ForOp 语义），而非 `loop_impl` 的能力缺失。换言之，IR 层的 `Loop` Operation 语义足以表达 break，只是 for 循环这条入口不会生成它。

---

### 4.3 if_else_impl：把 if/else 降级成 IfElse Operation

#### 4.3.1 概念说明

`if cond: ... else: ...` 在 HIR 里是对 `hir_stubs.if_else(cond, then_block, else_block)` 的 Call。`if_else_impl` 把它降级成一条 [`IfElse` IR Operation](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L273-L298)，结构为：

```
IfElse:
    cond: Var              # 布尔标量条件
    then_block: Block      # then 分支
    else_block: Block      # else 分支
    → 结果: 各分支 yield 的值的合并
```

和循环一样，if/else 也涉及「携带值」——但这里是指**在 then/else 内被赋值、且在 if 之后还要用的局部变量**。两支对同一个名字的赋值要在汇合点合并（phi），合并的前提是两支给出的类型一致。

`if_else_impl` 有一个重要优化：**编译期常量条件直接展平**。如果 `cond.is_constant()`，就根本不生成 `IfElse` Operation，而是只分派被选中的那一支（`if True` 只编 then，`if False` 只编 else）。这正是 4.1 里 `builder.is_terminated` 提前返回的来源之一。

#### 4.3.2 核心流程

```
if_else_impl(cond, then_block, else_block):
  1. require_bool_scalar_type(cond)；若 cond.is_constant() → 只展平被选中分支，返回。
  2. stored_locals = sorted(then_block.stored_indices | else_block.stored_indices)
  3. 分派 then_block（进嵌套 Block + change_if_else_info + local.enter_branch）。
  4. 若 then 完全没 yield（info.jumps 为空）→ 把结构改写成
        if cond: then; else: EndBranch(None); <else_block>
     再展平 else_block。（避免两支都不 yield）
  5. 分派 else_block。
  6. 用 PhiState 把两支（及各 EndBranch 的输出）的类型/常量做统一。
  7. 用 mask 过滤非法结果，回填各 EndBranch 的 outputs。
  8. add_operation_variadic(IfElse, ...) 生成 Operation；
     把「显式结果」（如三元表达式 x if cond else y 的值）与「存储回局部槽的结果」分别处理。
```

第 4 步的改写值得注意：如果一个 `if` 没有 `else`（HIR 里 else_block 是空的 EndBranch），且 then 分支不产出任何值，就直接把 else 内容接到 IfElse 之后，避免出现「两支都不 yield」的非法结构。

#### 4.3.3 源码精读

**IfElse Operation 与字节码生成**：

[src/cuda/tile/_ir/control_flow_ops.py:L273-L298](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L273-L298) —— `IfElse` 有 `cond` 操作数和 `then_block`/`else_block` 两个嵌套 Block；打印时前缀是 `then`/`else`，行首是 `if(cond=...)`。

**常量条件展平**：

[src/cuda/tile/_ir/control_flow_ops.py:L301-L308](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L301-L308) —— `require_bool_scalar_type(cond)` 确保条件是布尔标量；若 `cond.is_constant()`，直接 `_flatten_branch(被选中的分支)`，完全不发射 `IfElse`。这是 cuTile「编译期 if」的底层来源（结合 u3-l5 的 `static_eval`，很多 `if` 能在编译期定值从而被消去）。

**两支分派与 phi 统一**：

[src/cuda/tile/_ir/control_flow_ops.py:L319-L370](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L319-L370) —— `stored_locals` 取两支的并集；分派 then（必要时先做第 4 步的结构改写）；分派 else；用 `result_phis = tuple(PhiState() for ...)` 对每个结果做 `propagate`，统一类型；`mask` 过滤掉 `InvalidType`；`flatten_aggregates` 展平后回填各 `EndBranch.outputs`；最后 `add_operation_variadic(IfElse, ...)`。

**结果分流**：

[src/cuda/tile/_ir/control_flow_ops.py:L377-L402](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L377-L402) —— 把 `IfElse` 的结果分成「显式结果」（如 `ct.static_eval` 风格的表达式值，最多 1 个，作为返回值）和「存储结果」（写回 `stored_locals` 对应的局部槽）。前者是 `ret`，后者逐个 `store_var`。

**EndBranch 与 _flatten_branch**。[src/cuda/tile/_ir/control_flow_ops.py:L512-L524](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L512-L524) 的 `end_branch` 把「显式 yield 值 + 当前各存储局部」打包成 `JumpInfo` 记入 `if_else_info`；[src/cuda/tile/_ir/control_flow_ops.py:L416-L427](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L416-L427) 的 `_flatten_branch` 在「展平模式」下分派一个分支并取出它唯一的 yield 值（常量条件路径用它）。

#### 4.3.4 代码实践

**实践目标**：观察常量条件展平如何改变生成的 IR。

**操作步骤**：

1. 设想两个内核片段：
   - 动态条件：`if ct.bid(0) > 0: ct.store(...)` —— `bid(0)` 是运行时值，`cond` 非常量。
   - 常量条件：`if True: ct.store(...)`（或经 `static_eval` 得到的编译期布尔）—— `cond.is_constant()` 为真。
2. 对照 [src/cuda/tile/_ir/control_flow_ops.py:L305-L308](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L305-L308) 预测：动态条件会生成一条 `IfElse` Operation（带 then/else 两个嵌套 Block）；常量条件不会出现 `IfElse`，只看到被选中分支里的 `store` 等操作「直接铺」在外层 Block 里。

**需要观察的现象**：常量分支被「溶解」进外层，IR 更扁平、更短；动态分支保留显式的 `if(cond=...)` 结构。

**预期结果**：能说出「常量 if 在 hir2ir 阶段就被消除，不会进入后续 IR 优化 pass」。**待本地验证**：用 `CUDA_TILE_DUMP_TILEIR=1` 对比两个内核的 dump，确认常量条件无 `IfElse`、动态条件有。

#### 4.3.5 小练习与答案

**练习 1**：`if_else_impl` 里 `stored_locals` 为什么取 then/else 的**并集**而不是交集？

**参考答案**：因为汇合点之后，无论实际走了哪一支，外层代码都期望这些局部变量「有一个确定的值」。只要任一支改写了某个局部，合并后就必须为它产生一个 phi 结果（即便另一支没改写，也要用该支入口处的旧值作为「另一支的输出」参与合并，这部分由 `ControlFlowInfo.stored_locals` 与各 `EndBranch` 记录的 outputs 配合完成）。取并集保证所有「可能被改写」的名字都被纳入合并，不遗漏。

**练习 2**：`if_else_impl` 第 4 步（then 不 yield 时把 else 接到 IfElse 之后）解决了什么问题？

**参考答案**：`IfElse` Operation 的两支必须各自以 `EndBranch`（yield）终止，类型检查要求两支 yield 的类型一致。如果一个 `if` 没有 `else`、then 分支又不产出值，直接构造 `IfElse` 会出现「两支都不 yield」的非法结构。把 else 内容移到 IfElse 之外，相当于「if 只负责有条件的 then，其余顺序执行」，回避了非法结构。这是源码注释里 *“avoid the situation where none of the branches yield”* 的含义。

---

### 4.4 PhiState：控制流汇合点的类型与常量统一

#### 4.4.1 概念说明

`loop_impl` 和 `if_else_impl` 都反复用到一个对象：`PhiState`（[src/cuda/tile/_ir/ir.py:L93-L164](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L93-L164)）。它就是 cuTile 版的「phi 节点状态」：在一个控制流汇合点，把多条到达定义（初值、各 continue/break/EndBranch 的输出）的信息**合并**成一份，用于决定汇合后变量的类型与常量性。

`PhiState` 主要做两件事：

1. **类型统一**：所有到达定义的类型必须一致（或在容错情况下退化为 `InvalidType`），否则报 `TileTypeError`——「变量类型依赖于路径」。
2. **常量传播**：如果某个变量在所有到达路径上都是同一个常量值，汇合后它仍是常量；只要有一条路径是非常量，或两条路径常量值不同，就降级为非常量。

对循环，每个携带值需要**两个** `PhiState`：`body_phi`（流入下一轮 body 的类型）和 `result_phi`（循环结束后的结果类型），它们被一起装进 `LoopVarState`（[src/cuda/tile/_ir/ir.py:L314-L317](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L314-L317)）。

#### 4.4.2 核心流程

`PhiState` 的核心方法 `propagate(src)` 用一条到达定义 `src` 来更新自己。设汇合点有 N 条到达定义 \(v_0, v_1, \dots, v_{N-1}\)，则：

\[
\text{ty}_{\text{merge}} = v_0.\text{ty} \;\;\text{要求}\;\; v_i.\text{ty} = v_0.\text{ty}\ \forall i
\]

类型不一致时：若 `fail_eagerly=False`（默认），合并类型退化为 `InvalidType`（稍后由 mask 过滤，常用于「变量在某些路径未定义」）；若 `fail_eagerly=True`（如循环的 continue 路径），立即抛 `TileTypeError`。

常量传播按**聚合元素的粒度**逐项判断（因为一个 tuple/数组可能在某些维度是常量、某些不是）。对第 i 个聚合元素：

\[
\text{const}_i =
\begin{cases}
\text{MAY\_BE\_CONSTANT}(v_i) & \text{若所有路径上该元素都等于同一个常量} \\
\text{NONCONSTANT} & \text{否则}
\end{cases}
\]

`finalize_constant_and_loose_type(dst)` 在最后把存活下来的常量写回结果 Var，并设置 loose type。

#### 4.4.3 源码精读

**propagate：类型与常量的增量合并**：

[src/cuda/tile/_ir/ir.py:L105-L156](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L105-L156) —— 第一条到达定义直接初始化 `self.ty`/`self.loose_ty`；后续若类型不符，按 `fail_eagerly` 决定报错或退化为 `InvalidType`；loose type 不完全一致时「统一到具体类型」。常量部分逐聚合元素判断：`item.is_constant()` 为真且与已记录值相等 → 保持 `MAY_BE_CONSTANT`，否则 → `NONCONSTANT`。

**finalize：把结论写回 Var**：

[src/cuda/tile/_ir/ir.py:L158-L164](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L158-L164) —— 对每个仍是 `MAY_BE_CONSTANT` 的聚合元素调用 `item.set_constant(val)`，并 `dst.set_loose_type(self.loose_ty)`。

**LoopVarState：循环携带值的双 phi**：

[src/cuda/tile/_ir/ir.py:L314-L341](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L314-L341) —— `finalize_loopvar_type` 协调 `body_phi` 与 `result_phi`：若两者类型都有效却不一致，直接报 `TileTypeError`（“Variable ... has changed its type inside a loop”）。这正是「循环携带值在每轮和结束后必须同型」的校验。

**Scope：承载 phi 所需的「当前控制流上下文」**。phi 合并依赖「当前在哪个循环/分支里」，这些由 `Scope` 提供：

[src/cuda/tile/_ir/scope.py:L136-L190](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/scope.py#L136-L190) —— `Scope` 持有 `loop_info`/`if_else_info`（当前控制流的 `ControlFlowInfo`）、`local_scopes`（局部变量槽栈）、`hir2ir_varmap`（HIR Value.id → IR Var 的映射）。`change_loop_info`/`change_if_else_info` 是上下文管理器，进入循环/分支时换上对应的 `ControlFlowInfo`，出来时还原——这让 `continue_()`/`break_()`/`end_branch()` 能找到正确的收集容器。

[src/cuda/tile/_ir/scope.py:L18-L28](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/scope.py#L18-L28) —— `JumpInfo`（一次跳转及其输出）和 `ControlFlowInfo`（一个控制流构造内所有跳转的收集器，外加 `flatten` 标志）。

**LocalScope.enter_branch：分支内的局部改写隔离**：

[src/cuda/tile/_ir/scope.py:L84-L92](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/scope.py#L84-L92) —— `enter_branch()` 复制一份局部槽快照供分支内使用，退出时还原。这样 then/else 各自的 `redefine` 不会互相污染，汇合点的 phi 才能基于「分支内最终值」正确合并。

#### 4.4.4 代码实践

**实践目标**：用一个「循环携带值在两轮间类型不一致」的反例，触发 `PhiState` 的类型校验。

**操作步骤**：

1. 设想（或写出）一个**非法**内核：在循环里让同一个名字在不同轮次变成不同类型，例如：
   ```python
   @ct.kernel
   def bad(x, n, tile: ct.Constant[int]):
       i = ct.bid(0)
       acc = ct.load(x, index=(i,), shape=(tile,))   # acc: float32 tile
       for _ in range(n):
           acc = 0                                       # 第 1 轮起 acc 变成 loosely-typed int 常量
       ct.store(x, index=(i,), tile=acc)
   ```
   （注意：这只是用来理解校验逻辑的示意；实际能否触发取决于类型提升细节。）
2. 对照 [src/cuda/tile/_ir/ir.py:L336-L341](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L336-L341)，预测：`body_phi.ty`（来自初值 load，float32 tile）与 `result_phi.ty`（来自循环体内 acc 的新类型）若不一致，`finalize_loopvar_type` 会抛 `TileTypeError`，信息形如 *“Variable acc has changed its type inside a loop from ... to ...”*。

**需要观察的现象**：编译阶段（而非运行阶段）报类型错误，且错误信息明确指向「循环内类型变化」。

**预期结果**：能解释「循环携带值的类型必须在所有轮次一致」这一约束由 `LoopVarState.finalize_loopvar_type` 强制。**待本地验证**：实际构造一个能稳定触发的内核并编译，确认错误信息（该实践为源码阅读型，重在理解校验逻辑而非具体取值）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `PhiState` 的常量传播要按「聚合元素粒度」逐项判断，而不是整Var一刀切？

**参考答案**：因为一个聚合值（如 tuple、带静态形状的数组）可能「部分维度是常量、部分不是」。例如一个 tuple 参数里某个元素是 `Constant`、另一个不是（u3-l7 的「部分常量」场景），整Var一刀切会丢掉「部分常量」的信息，导致后续优化（常量折叠、对齐推理）失效。逐聚合元素判断能保留每个元素独立的常量性，这正是 `propagate` 里 `for i, item in enumerate(src.flatten_aggregate())` 循环的存在意义。

**练习 2**：`loop_impl` 里每个携带值用两个 `PhiState`（`body_phi` 和 `result_phi`），能否合并成一个？

**参考答案**：不能。`body_phi` 描述「流入下一轮 body 的类型」（来自初值 + 各 continue），`result_phi` 描述「循环结束后的结果类型」（来自初值 + 各 break + for 的 0 次迭代回退）。两者收集的到达定义集合不同：continue 只影响 body_phi（下一轮），break 只影响 result_phi（退出）。`finalize_loopvar_type` 正是靠比较这两个独立 phi 的类型来校验「循环内类型不变」。合并会丢失这层区分。

## 5. 综合实践

把本讲四个模块串起来，完成 spec 要求的端到端追踪任务：**跟踪一个简单循环内核经 hir2ir 后生成的 `Loop` IR 操作，标出归纳变量与携带值**。

### 5.1 选定内核

使用 [test/test_control_flow.py:L17-L24](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_control_flow.py#L17-L24) 的 `plus_n_one_arg`（一个最简单的「循环累加」内核）。

### 5.2 操作步骤

1. **画出 HIR**（基于 u5-l3 的知识）：函数体是一个 Block，含 `ct.bid`、`ct.load`、`ct.loop(body=循环体Block, iterable=range(n))`、`ct.store` 四个 Call；循环体 Block 内只有一个 `xi = add(xi, 1)` 的 Call，末尾 Jump 是隐式的 `CONTINUE`。
2. **跟踪 hir2ir 主分派**：`hir2ir` → `_hir2ir_coroutine` 建作用域、绑定参数 → `_dispatch_hir_block_inner` 遍历函数体的 Call。前两个 Call（`bid`/`load`）是 stub，走 `_call_builtin` 各发一条 IR Operation。第三个 Call 的 callee 是 `hir_stubs.loop`，也是 stub，于是 `_call_builtin` 查到 `loop_impl` 并调用它。
3. **进入 loop_impl**（4.2.3）：
   - `stored_locals = {xi}` → 携带值 1 个；
   - 初值 `initial_values = (load 的结果 Var,)`；
   - for 循环 → 建 `induction_var`，`start=0/stop=n/step=1`；
   - `enter_nested_block` 开新 Block，在其中 `redefine` 出本轮 `xi` 的 body_var，`body_phi.propagate(初值)`；
   - 分派循环体 Block → 发射 `add` 操作；末尾 CONTINUE 被翻译成 `continue_()`，它发射 `Continue` Operation 并把 `add` 的结果登记为 `JumpInfo.outputs`；
   - 收尾：把 continue 的输出传播给 `body_phi`/`result_phi`，`finalize_loopvar_type` 校验类型一致；
   - `add_operation_variadic(Loop, ...)` 生成 `Loop` Operation；`store_var` 把结果写回 `xi` 的局部槽。
4. **回到主 Block**：最后一个 Call `ct.store` 同样走 stub → `_call_builtin`，发 `store` 操作。

### 5.3 标注归纳变量与携带值

在你的追踪笔记里明确写出：

- **归纳变量（induction variable）**：`range(n)` 的计数器，对应内核里的 `_`。它不参与计算，仅作 for 循环的计数；在 IR 里是 `Loop.body.params[0]`（见 [src/cuda/tile/_ir/control_flow_ops.py:L45-L48](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L45-L48)）。
- **携带值（carried value）**：`xi`，初值 = `ct.load(x, index=(i,), shape=(tile,))` 的结果；每轮由 `xi + 1` 更新；循环结束后 `xi` 指向累加结果，被 `ct.store` 用到。在 IR 里是 `Loop.body.params[1]`，初值挂在 `Loop.initial_values`（见 [src/cuda/tile/_ir/control_flow_ops.py:L50-L52](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/control_flow_ops.py#L50-L52)）。

### 5.4 预期结果与验证

预期 dump（`CUDA_TILE_DUMP_TILEIR=1`，结构示意，具体 Var 名以待本地验证为准）：

```
...
%xi_load = load ...
%loop_result = for %ind in range %0, %n, %1 (with %xi_body = %xi_load)
do (%ind, %xi_body):
    %xi_new = add %xi_body, %1
    continue %xi_new
store ..., %loop_result
```

**待本地验证**：在有 GPU 的环境运行 `plus_n_one_arg` 并开启 `CUDA_TILE_DUMP_TILEIR`，对照上面的结构确认：(a) 出现 `for ... in range(...)` 行；(b) `with` 后面列出且仅列出 `xi` 一个携带值；(c) body 内有 `add` 与 `continue`，且 `continue` 的实参是 `add` 的结果。如果 dump 里携带值多于一个，说明 HIR 判定还有其他局部在循环内被改写——回到源码核对 `body.stored_indices`。

## 6. 本讲小结

- `hir2ir` 是一个「HIR 解释器」：遍历 HIR Block/Call，按被调用对象类型把通用 Call 分派成具体 IR Operation，写进当前 `Builder`；用协程实现软件栈以绕开 Python 递归上限。
- Call 的分派由 `call()` 按 callee 类型决定：stub/内置走 `_call_builtin`（查 `ImplRegistry`），用户函数/闭包走 `_call_user_defined`（内联），还有 dtype 构造、enum、dataclass 等特例。
- `loop_impl` 把 `for`/`while` 降级成一个带 body 的 `Loop` Operation；循环体内被改写的局部变量（`body.stored_indices`）变成「携带值」，以 `initial_values` 流入、每轮由 `continue` 回传、结束后由 `break`/for 回退产出结果——本质是函数式 fold。
- `if_else_impl` 把 `if/else` 降级成 `IfElse` Operation；编译期常量条件直接展平被选中分支（不生成 `IfElse`）；两支对同一局部的赋值在汇合点合并。
- `continue_`/`break_`/`end_branch` 不直接「跳」，而是把跳转输出登记进当前 `ControlFlowInfo.jumps`，由外层 `loop_impl`/`if_else_impl` 收尾时传播与回填。
- `PhiState` 在汇合点做类型统一（不一致则 `InvalidType` 或报错）与按聚合元素粒度的常量传播；循环携带值用 `LoopVarState`（`body_phi` + `result_phi`）双 phi，`finalize_loopvar_type` 强制「循环内类型不变」。

## 7. 下一步学习建议

- **u5-l5（IR 核心：IRContext、Builder、Block、Var、Operation）**：本讲把 `Var`/`Operation`/`Block`/`Builder` 当作已知概念使用了，下一讲会正式拆解它们的数据结构与声明式字段机制，补全「IR 长什么样」的细节。
- **u5-l7（Stub 与实现注册）**：本讲多次提到「`ImplRegistry` 查表」和 `@impl`，但没讲注册表本身怎么工作、重载怎么分派。学完 u5-l7 能彻底打通「用户 API → stub → @impl → IR Operation」的完整链路。
- **u6-l1（Pass 流水线总览）**：hir2ir 产出的 IR 还要经过一系列优化 pass（DCE、整除性传播、token 排序等）。建议接着看 `_transform_ir` 如何在 hir2ir 之后再加工这些 `Loop`/`IfElse`。
- **拓展阅读**：想看更复杂的循环形态（嵌套、多重循环、标量累加器），可直接阅读 [test/test_control_flow.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_control_flow.py) 里 `TestForLoop` 的其余用例，并用本讲的方法逐一预测它们的 `Loop` 结构。
