# 控制流：if / for / while 与 range 限制

## 1. 本讲目标

前两讲（u3-l1、u3-l2）我们写出的内核都是「直来直去」的：load 一块 tile、做一段算术、store 回去，中间没有任何分支或循环。但真实的内核经常需要**条件判断**（「只对某些 block 处理」「只在迭代到某一步时累加」）和**循环**（分块 GEMM 沿 K 维一圈圈累加、pooling 在窗口上滑动）。本讲就来回答：**tile 代码里能写哪些 Python 控制流？有什么限制？它们在底层变成了什么？**

读完本讲，你应当能够：

- 在 tile 代码里正确使用 `if/else`、`for ... in range(...)`、`while`，并知道它们**可以任意嵌套**。
- 牢记 `range` 的 **`step` 必须严格大于 0** 这一硬性限制，并能解释为什么负步长会被拒绝。
- 区分 `break` / `continue` 在 `for` 循环与 `while` 循环里的**支持差异**（for 不支持 break，while 两者都支持）。
- 理解控制流在底层会映射成两种**结构化 IR 操作**：分支映射为 `IfElse`，循环映射为携带循环变量（carried values）的 `Loop`；并理解为什么「tile 不可变」会让循环必须用「携带值」的方式来表达。

本讲覆盖三个最小模块：**if/for/while（tile 代码支持的 Python 控制流子集及其限制）**、**range step 限制（step > 0）**、**Loop / IfElse（控制流的 IR 映射）**。

## 2. 前置知识

本讲默认你已经掌握：

- **执行空间与 tile code 的定位**（u2-l1）：cuTile 定义 host / SIMT / tile 三种执行空间，控制流只能写在 **tile code**（即 `@ct.kernel` 装饰的函数体）里；tile code 里**没有 Python 运行时**，所有 Python 语法都会被编译器「翻译」成 Tile IR，而不是真的由 Python 解释器执行。
- **load–compute–store 范式与 `ct.bid`**（u3-l1）：`ct.bid(axis)` 返回当前 block 在 grid 第 `axis` 轴的坐标，是内核里「定位自己」的手段，本讲的实践会大量用到它。
- **tile 不可变（immutable）**（u2-l3、u3-l2）：tile 一旦创建就不能修改，任何「修改」操作都返回一个**新 tile**。这一点在讲循环时是关键——循环里反复「`xi += 1`」看似在改 `xi`，底层其实是每轮产生一个新值，再用「携带值」串起来。

一个贯穿全讲的核心直觉是：**tile code 是「被翻译的 Python」，不是「被执行的 Python」**。文档明确写道：「There is no Python runtime within tile code」（见 [execution.rst:L80-L86](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L80-L86)）。所以你写的 `for` / `while` / `if` 只是**语法糖**，编译器会把它们解析成结构化的 IR 操作（`Loop` / `IfElse`）。这也解释了为什么只有「一部分」Python 语法被支持——编译器只实现了对其中一部分的翻译。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/source/execution.rst](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst) | 权威文档。声明 tile code 支持的 Python 子集、控制流可任意嵌套，以及「`step` 必须严格为正」这一限制。 |
| [src/cuda/tile/_ir/control_flow_ops.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/control_flow_ops.py) | 控制流的**IR 实现核心**。定义 `Loop`、`IfElse`、`Continue`、`Break`、`EndBranch`、`Return` 等操作，以及把 HIR 的 `loop` / `if_else` 降级为这些 IR 操作的 `loop_impl` / `if_else_impl`。本讲最重要的一份源码。 |
| [src/cuda/tile/_ir/core_ops.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/core_ops.py) | 内置 `range` 的实现 `range_`，**step > 0 的校验就在这里**。 |
| [src/cuda/tile/_passes/ast2hir.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_passes/ast2hir.py) | 前端：把 Python 的 `ast.For` / `ast.While` / `ast.If` / `ast.Break` / `ast.Continue` 翻译成 HIR 的 `loop` / `if_else` 调用。从这里能看到 for 与 while 的不同降级方式，以及 break/continue 的限制来源。 |
| [src/cuda/tile/_ir/type.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/type.py) | `RangeIterType` 与 `RangeValue`，表示一个 `range(...)` 的「区间值」（start/stop/step 三元组），是 for 循环与底层 `Loop` 之间的桥梁。 |
| [test/test_control_flow.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_control_flow.py) | 控制流的完整测试集：for / while / 嵌套 / break / continue / if-else / 三元表达式 `a if c else b`。是「语法到底支不支持」的最可靠参考。 |
| [samples/MatMul.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/MatMul.py) | 经典的「K 维 for 循环累加」GEMM 内核，是循环 + 携带值（accumulator）最现实的例子。 |

> 提示：本讲偏「机制」。`@ct.kernel` 装饰器、compile 总流程、HIR/IR 的完整数据结构分别在 u5-l1、u5-l2、u5-l3、u5-l5 讲。本讲只聚焦「控制流这一类语句是怎么走过来的」。

---

## 4. 核心概念与源码讲解

### 4.1 tile 代码支持的 Python 控制流子集（if / for / while）

#### 4.1.1 概念说明

cuTile 允许你在 tile code 里直接写 Python 的 `if`、`for`、`while`，语义和普通 Python 基本一致：

- **`if / else`**：根据一个**标量布尔条件**选择执行哪一个分支。条件必须是标量（零维）布尔值，不能是 tile——因为「整个 block 集体走同一条路」，不存在「一半线程走 then、一半走 else」。
- **`for ... in range(...)`**：计数循环。迭代对象目前只能是 `range(...)`（或编译期 `ct.static_iter(...)`，属于进阶用法，本讲不展开）。
- **`while cond:`**：条件循环，只要 `cond` 为真就继续。

文档明确列出这些语句「可用且可任意嵌套」（见 [execution.rst:L103-L107](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L103-L107)）。但有若干**限制**，初学者最容易踩坑：

1. **`range` 的 `step` 必须严格为正**（详见 4.2）。`range(10, 0, -1)` 这种倒序循环不支持。
2. **`break` 在 `for` 循环里不支持**，但在 `while` 循环里支持；`continue` 在两者里都支持（见 4.1.3）。
3. **不支持 `for-else` / `while-else`**（带 `else` 的循环）。
4. **不支持 `match-case`**（测试里直接标记为 `xfail`，见 [test_control_flow.py:L844-L845](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_control_flow.py#L844-L845)）。
5. **不支持异常（`try/except`）、协程**等（见 [execution.rst:L84-L86](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L84-L86)）。

一个常被忽略的点是「**循环体内对象不可变**」。文档在对象模型里写道：「All objects created within tile code are immutable」（见 [execution.rst:L88-L95](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L88-L95)）。所以循环里写 `xi += 1`，并不是「把 tile `xi` 原地加 1」，而是「算出一个新 tile，再把这个名字 `xi` 重新绑定到新 tile」。这一点会在 4.3 讲「携带值」时再次用到。

#### 4.1.2 核心流程

一段 tile 代码里的控制流，从源码到执行大致经过：

1. **前端 ast2hir**：把 `ast.If` / `ast.For` / `ast.While` 翻译成对 HIR 内置函数 `hir_stubs.if_else` / `hir_stubs.loop` 的调用，每个分支/循环体变成一个 HIR `Block`。
2. **HIR → IR（hir2ir）**：`if_else_impl` / `loop_impl` 把这些 HIR 调用降级为具体的 IR 操作 `IfElse` / `Loop`，并处理跨分支、跨迭代的类型合并与常量传播。
3. **IR → 字节码**：`IfElse.generate_bytecode` / `Loop.generate_bytecode` 把结构化 IR 编码成 TileIR 字节码里的 `IfOp` / `ForOp` / `LoopOp`。
4. **tileiras → cubin**：后端把字节码编译成 GPU 机器码，`if` 变成 block 级的条件分支，`for` 变成计数循环。

其中第 1 步是 for 与 while **分道扬镳**的地方，值得记住：

- `for ... in range(...)` → HIR `loop(body, iterable=range值)`，其中 `iterable` 非 None。
- `while cond:` → HIR `loop(body, iterable=None)`，编译器在循环体**最前面**自动插入一句「`if cond: pass; else: break`」来实现「条件不满足就跳出」。

#### 4.1.3 源码精读

**（a）`if` 语句的降级** —— 前端把 `ast.If` 翻译成一次 `if_else` 调用，then/else 各成一个 HIR Block（[ast2hir.py:L1012-L1026](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_passes/ast2hir.py#L1012-L1026)）：

```python
@_register(_stmt_handlers, ast.If)
def _if_stmt(stmt: ast.If, ctx: _Context) -> None:
    cond = _bool_expr(stmt.test, ctx)
    with ctx.new_block() as then_block:
        _stmt_list(stmt.body, ctx)
        ...
    with ctx.new_block() as else_block:
        _stmt_list(stmt.orelse, ctx)
        ...
    ctx.call_void(hir_stubs.if_else, (cond, then_block, else_block))
```

注意 `_bool_expr`——条件会被强制要求是布尔标量（底层 `require_bool_scalar_type` 校验，见 [control_flow_ops.py:L305](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/control_flow_ops.py#L305)），写 `if some_tile:` 是不合法的。

**（b）`for` 与 `while` 的不同降级** —— 这是本小节最关键的一段（[ast2hir.py:L707-L733](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_passes/ast2hir.py#L707-L733)）：

```python
@_register(_stmt_handlers, ast.For)
def _for_stmt(stmt: ast.For, ctx: _Context):
    ...
    static_iter_expr = _get_static_iter_expr(stmt.iter, ctx)
    if static_iter_expr is None:
        kind = LoopKind.FOR
        op = hir_stubs.loop
        iterable = _expr(stmt.iter, ctx)        # range(...) → 一个 RangeValue
    ...
    induction_var = ctx.make_value()
    with ctx.new_block(params=(induction_var,)) as body_block:
        _do_assign(induction_var, stmt.target, ctx)   # 把归纳变量绑定到循环变量名
        _stmt_list(stmt.body, ctx)
        ...
    ctx.call_void(op, (body_block, iterable))          # iterable 非 None
```

`for` 把 `range(...)` 求值成一个 `iterable`（非 None）传给 `loop`。而 `while` 完全不同（[ast2hir.py:L913-L937](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_passes/ast2hir.py#L913-L937)）：

```python
@_register(_stmt_handlers, ast.While)
def _while_stmt(stmt: ast.While, ctx: _Context):
    ...
    with ctx.new_block() as body_block:
        # Add "if cond: pass; else: break"
        cond = _bool_expr(stmt.test, ctx)
        with ctx.new_block() as then_block:
            ctx.set_block_jump(hir.Jump.END_BRANCH)    # then: 什么都不做
        with ctx.new_block() as else_block:
            ctx.set_block_jump(hir.Jump.BREAK)         # else: 跳出循环
        ctx.call_void(hir_stubs.if_else, (cond, then_block, else_block))
        ...
    ctx.call_void(hir_stubs.loop, (body_block, None))  # iterable = None
```

`while` 的精髓是那句注释 `Add "if cond: pass; else: break"`：**while 循环没有「计数」概念，它的「条件」是靠每轮开头插一句「条件假就 break」来实现的**。这也解释了下面 break/continue 的支持差异。

**（c）break / continue 的限制来源** —— 限制在前端就拦下来了（[ast2hir.py:L1029-L1040](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_passes/ast2hir.py#L1029-L1040)）：

```python
@_register(_stmt_handlers, ast.Continue)
def _continue_stmt(stmt: ast.Continue, ctx: _Context) -> None:
    if ctx.parent_loops and ctx.parent_loops[-1] is LoopKind.STATIC_FOR:
        raise ctx.syntax_error("Continue in a for loop with static_iter() is not supported")
    ctx.set_block_jump(hir.Jump.CONTINUE)        # 普通 for / while 都允许 continue

@_register(_stmt_handlers, ast.Break)
def _break_stmt(stmt: ast.Break, ctx: _Context) -> None:
    if ctx.parent_loops and ctx.parent_loops[-1] in (LoopKind.FOR, LoopKind.STATIC_FOR):
        raise ctx.syntax_error("Break in a for loop is not supported")
    ctx.set_block_jump(hir.Jump.BREAK)           # 只有 while 允许 break
```

读这段代码就能精确得出结论：

| 语句 | 普通 `for` | `while` | `static_iter` for |
|------|-----------|---------|-------------------|
| `continue` | ✅ 支持 | ✅ 支持 | ❌ 不支持 |
| `break` | ❌ 不支持 | ✅ 支持 | ❌ 不支持 |

为什么 `for` 不支持 `break`？因为 `for` 在底层会被编成 TileIR 的 **`ForOp`**——一个有固定起止计数的结构化循环，协议里没有「中途跳出」的口子；而 `while` 被编成更通用的 **`LoopOp`**，它本来就靠 `break` 来结束（参见 4.3.3 的字节码编码差异，以及测试里的 TODO 注释 [test_control_flow.py:L183-L184](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_control_flow.py#L183-L184)）。测试 `test_break_in_for_loop` 明确断言它会抛 `TileSyntaxError`（[test_control_flow.py:L177-L185](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_control_flow.py#L177-L185)）。

一个合法的 while + break 例子（[test_control_flow.py:L224-L234](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_control_flow.py#L224-L234)）：

```python
@ct.kernel
def break_in_while(x, n, tile: ct.Constant[int]):
    i = ct.bid(0)
    xi = ct.load(x, index=(i,), shape=(tile,))
    count = 0
    while count < n:
        if count == 2:
            break              # while 里 break 合法
        xi += 1
        count += 1
    ct.store(x, index=(i,), tile=xi)
```

#### 4.1.4 代码实践

**实践目标**：写一个内核，对每个 block，**仅当 `bid(0)` 为偶数时**才把对应 tile 加 1 并 store；奇数 block 保持原值。用它体会 `if` 在 tile code 里的写法。

**操作步骤**：

1. 新建脚本 `parity_store.py`，写入下面的「示例代码」（这是为本讲新写的示例，非项目原有文件）：

```python
# 示例代码
import cuda.tile as ct
import torch

@ct.kernel
def even_blocks_add_one(x, tile: ct.Constant[int]):
    i = ct.bid(0)
    xi = ct.load(x, index=(i,), shape=(tile,))
    if i % 2 == 0:          # 仅偶数 block 进入 then 分支
        xi = xi + 1
    ct.store(x, index=(i,), tile=xi)

N, TILE = 256, 128
x = torch.zeros(N, dtype=torch.float32, device="cuda")
grid = (N // TILE, 1, 1)
ct.launch(torch.cuda.current_stream(), grid, even_blocks_add_one, (x, TILE))

print(x.cpu().reshape(-1, TILE).sum(dim=1))   # 每个 block 的求和
```

2. 确认本机已按 u1-l2 安装好 `cuda-tile[tileiras]` 与 PyTorch 后运行：`python parity_store.py`。

**需要观察的现象**：

- 程序应输出形如 `[128., 0., 128., 0.]` 的张量——偶数 block（第 0、2 个）每个元素被加 1，共 128；奇数 block 全为 0。
- 把 `i % 2 == 0` 改成 `i % 2`（漏写 `== 0`），观察是否仍能编译（`i % 2` 的结果是 int32 而非 bool，应触发条件须为布尔标量的类型错误）。

**预期结果**：偶数 block 的 tile 被加 1，奇数 block 不变。底层每个 block 都会生成一个 `IfElse` 操作：`then` 块里是 `xi = xi + 1`，`else` 块为空（仅一条 `EndBranch`）。**待本地验证**：确切的 dump 文本以你本地 `CUDA_TILE_DUMP_TILEIR=1`（详见 u8-l5）的输出为准。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `even_blocks_add_one` 改成「`if/else` 两边都给 `xi` 赋值」的等价写法（偶数 +1，奇数 -1）。两个分支里 `xi` 的类型必须满足什么约束？

> **答案**：两个分支对同一个名字 `xi` 赋值时，两侧类型必须**一致**，否则底层 `PhiState` 会报「Type of `xi` depends on path taken」。这里两边都是「原 tile `+/-` 整数常量」，类型一致（同为 `xi` 的 dtype），合法。测试 [test_control_flow.py:L641-L657](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_control_flow.py#L641-L657) 演示了一边是 bool、一边是 tile 导致的 `TileTypeError`。

**练习 2**：下面这段能在 cuTile 里编译吗？为什么。

```python
for k in range(10):
    if k == 5:
        break
```

> **答案**：**不能**。`break` 在普通 `for` 循环里不支持，前端 `_break_stmt` 会抛 `TileSyntaxError: Break in a for loop is not supported`（[ast2hir.py:L1036-L1040](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_passes/ast2hir.py#L1036-L1040)）。若确需「计数到一半跳出」，应改写为 `while` 循环。

---

### 4.2 range 与 step > 0 限制

#### 4.2.1 概念说明

`for` 循环的迭代对象目前实际只支持 `range(...)`。Python 的 `range` 有三种调用形式：

- `range(stop)`
- `range(start, stop)`
- `range(start, stop, step)`

cuTile 全部支持，但有一条**硬性限制**：**`step` 必须严格大于 0**。文档把它列为「Current limitations」的唯一一条（见 [execution.rst:L109-L118](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/execution.rst#L109-L118)）：负步长（如 `range(10, 0, -1)`）不支持；甚至「通过变量间接传入负步长」可能产生**未定义行为**——也就是说编译器不一定能在编译期发现，可能编出错误结果。所以**永远不要依赖负步长**。

为什么有这个限制？因为 `for` 循环在底层被编成 TileIR 的 `ForOp`，它是一个「从 start 每次 +step 直到达到 stop」的计数循环，后端实现目前只处理正向步长（源码里直接留了 `FIXME(Issue 314): Support negative step.`，见下文）。

`range(...)` 在 cuTile 里并不是 Python 内建，而是被注册成了 IR 实现，返回一个特殊的**区间值** `RangeValue`，它就是 `(start, stop, step)` 三元组。for 循环拿到这个三元组，才知道「从哪开始、到哪结束、步长多少」。

#### 4.2.2 核心流程

`range_` 实现的关键步骤（[core_ops.py:L717-L742](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/core_ops.py#L717-L742)）：

1. **参数个数校验**：必须是 1~3 个参数，且每个都是**有符号整数标量**（`require_signed_integer_scalar_type`）。
2. **补全缺省值**：
   - 1 个参数：`start=0, stop=arg, step=1`
   - 2 个参数：`start=arg0, stop=arg1, step=1`
   - 3 个参数：`start, stop, step` 全用传入值。
3. **step 校验**（仅 3 参数形式时）：若 `step` 是编译期常量且 `<= 0`，立即抛 `TileTypeError`。
4. **打包成区间值**：用 `(start, stop, step)` 构造 `RangeValue`，类型标记为 `RangeIterType`。

注意第 3 步只在「step 是常量」时才校验。如果 step 是运行时变量且恰好为负，编译器**发现不了**，行为未定义——这正是文档警告「Passing a negative step indirectly via a variable may cause undefined behavior」的来源。

#### 4.2.3 源码精读

`range` 的 IR 实现（[core_ops.py:L717-L742](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/core_ops.py#L717-L742)）：

```python
@impl(range)
def range_(args: tuple[Var, ...]) -> Var:
    if not 1 <= len(args) <= 3:
        raise TileTypeError(f"Invalid number of arguments: {len(args)}")
    for arg in args:
        require_signed_integer_scalar_type(arg)
    ...
    if len(args) == 1:
        start = strictly_typed_const(0, ...)
        stop = args[0]
        step = strictly_typed_const(1, ...)
    elif len(args) == 2:
        start, stop = args[0], args[1]
        step = strictly_typed_const(1, ...)
    else:
        start, stop, step = args[0], args[1], args[2]
        # FIXME(Issue 314): Support negative step.
        # Error out if step is constant and not positive.
        if step.is_constant() and step.get_constant() <= 0:
            raise TileTypeError(f"Step must be positive, got {step.get_constant()}")

    agg_value = RangeValue(start, stop, step)
    ty = RangeIterType(datatype.default_int_type)
    return make_aggregate(agg_value, ty)
```

`FIXME(Issue 314)` 这行注释把「为什么不支持负步长」交代得很清楚：**这是一个尚未实现的功能**，而非永久性的设计禁令。校验只拦「常量且 `<= 0`」的情况。

`RangeValue` 只是一个朴素的三元组（[type.py:L875-L882](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/type.py#L875-L882)）：

```python
@dataclass
class RangeValue(AggregateValue):
    start: "Var"
    stop: "Var"
    step: "Var"
    def as_tuple(self) -> tuple["Var", ...]:
        return self.start, self.stop, self.step
```

它的类型 `RangeIterType` 是一个**聚合类型**，由三个相同 dtype 的标量组成（[type.py:L853-L866](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/type.py#L853-L866)）。这个 `(start, stop, step)` 三元组会原样传给 4.3 里的 `Loop` 操作，成为它计数循环的参数。

#### 4.2.4 代码实践

**实践目标**：亲手触发 step 校验，观察错误信息，建立「负步长不可用」的肌肉记忆。

**操作步骤**：

1. 写一个最小内核，刻意用负步长（示例代码）：

```python
# 示例代码
import cuda.tile as ct
import torch

@ct.kernel
def bad_countdown(x, tile: ct.Constant[int]):
    i = ct.bid(0)
    xi = ct.load(x, index=(i,), shape=(tile,))
    for k in range(8, 0, -1):      # 负步长
        xi = xi + 1
    ct.store(x, index=(i,), tile=xi)

x = torch.zeros(128, dtype=torch.float32, device="cuda")
ct.launch(torch.cuda.current_stream(), (1, 1, 1), bad_countdown, (x, 128))
```

2. 运行它。

**需要观察的现象**：

- 程序应在 `ct.launch` 阶段抛出 `cuda.tile._exception.TileTypeError: Step must be positive, got -1`，且**不会**执行任何 GPU 计算。

**预期结果**：抛出上述类型错误。这正是 [core_ops.py:L737-L738](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/core_ops.py#L737-L738) 那行校验的直接表现。把 `-1` 改回 `1` 或 `2` 即可正常通过。

#### 4.2.5 小练习与答案

**练习 1**：`range(n)`、`range(0, n)`、`range(0, n, 1)` 在 cuTile 里循环次数相同吗？底层生成的 `RangeValue` 是否一致？

> **答案**：循环次数相同，都是 `n` 次（`n>0` 时）。底层三者都会被补全成 `start=0, stop=n, step=1` 的 `RangeValue`（缺省值由 `range_` 填入，见 [core_ops.py:L726-L732](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/core_ops.py#L726-L732)）。测试 `test_basic_for_loop` 正是用这三种等价写法验证同一行为（[test_control_flow.py:L78-L98](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_control_flow.py#L78-L98)）。

**练习 2**：如果 `step` 是一个**运行时传入的变量**（如 `ct.Constant[int]` 参数）且值为负，会发生什么？

> **答案**：因为校验只覆盖「`step.is_constant()` 且 `<= 0`」，编译期无法判定运行时符号的正负，**校验不会触发**，程序会「成功」编译，但运行结果未定义。这正是文档强调「不要通过变量间接传负步长」的原因。安全做法：只在源码里写字面正步长。

---

### 4.3 控制流的 IR 映射：IfElse 与携带值的 Loop

#### 4.3.1 概念说明

前面两节讲的是「语法层面能用什么」。这一节往下看一层：`if` 和 `for/while` 在 Tile IR 里到底长什么样。理解这一层，你才能看懂 dump 出来的 IR，也才能理解为什么 cuTile 的循环必须用「携带值（carried values）」来表达。

cuTile 的控制流在 IR 层是**结构化的**——不是一堆带标签的 goto，而是两种嵌套的「块操作」：

- **`IfElse`**：带一个布尔条件 `cond`、一个 `then_block`、一个 `else_block`。两个分支各自是一个嵌套的 IR `Block`。
- **`Loop`**：带可选的 `start/stop/step`（for 循环有，while 循环没有）、一组 `initial_values`（循环开始前的初值）、一个 `body`（循环体嵌套 Block）。

**为什么循环需要「携带值」？** 这是本节的灵魂。回到「tile 不可变」：循环体里写 `xi += 1`，并不是修改 `xi`，而是产生新值并重新绑定名字。那么「下一轮迭代要用上一轮的结果」这件事怎么表达？答案是**函数式 / SSA 风格**：把循环里被重新赋值的变量变成循环的**迭代参数（iter args）**——

- 进入循环时，传入它的**初值**（`initial_values`）。
- 每轮迭代，这个变量作为循环体 Block 的**入参**（`body_vars`）。
- 每轮迭代结束时，算出的新值通过 `continue`（普通结束一轮）回传，成为下一轮的入参。
- 循环结束时，最后一轮的值成为循环的**结果**，绑定回外层的名字。

这和 MLIR 的 `scf.for`（带 `iter_args`）或函数式语言里的 `fold` 是同一个思路。形式化地，一段

```python
xi = xi_init
for k in range(start, stop, step):
    xi = f(xi, k)
# 此后使用 xi
```

会被映射成形如下的结构（伪 IR，仅示意结构）：

\[
\xi_{\text{out}} = \mathrm{Loop}(\textit{start}, \textit{stop}, \textit{step},\ \textit{init}=\xi_{\text{init}},\ \textit{body}=\lambda k, \xi_{\text{in}}.\ \texttt{continue}\ f(\xi_{\text{in}}, k))
\]

即每一轮 \(\xi\) 沿着 \(\xi_{\text{init}} \to f(\xi_{\text{init}}, 0) \to f(\cdot, 1) \to \dots\) 这条链流动，最后得到 \(\xi_{\text{out}}\)。

#### 4.3.2 核心流程

**`IfElse` 的构造**（`if_else_impl`，[control_flow_ops.py:L301-L402](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/control_flow_ops.py#L301-L402)）：

1. 校验 `cond` 是布尔标量；若 `cond` 是编译期常量，直接**常量折叠**——只编译会执行的那个分支（[control_flow_ops.py:L305-L308](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/control_flow_ops.py#L305-L308)）。
2. 收集两个分支里**被重新赋值**的变量名集合 `stored_locals`（并集，排序）。
3. 分别把 then / else 的 HIR Block 降级为 IR Block；每个分支末尾插一条 `EndBranch`（yield）操作，把本分支赋予这些变量的值「上交」。
4. 用 `PhiState` 合并两分支的类型与常量性（两分支类型必须一致）。
5. 生成 `IfElse` 操作，其结果是「按条件从 then/else 中选出的那些值」，再绑定回外层名字。

**`Loop` 的构造**（`loop_impl`，[control_flow_ops.py:L106-L261](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/control_flow_ops.py#L106-L261)）：

1. 判断是 for 还是 while：`require_optional_range_type(iterable)` 返回非 None 即 for。
2. 收集循环体里被赋值的变量 `stored_locals`，为每个建立一个 `LoopVarState`（含两个 phi：`body_phi` 管理循环体内的类型，`result_phi` 管理循环出口的类型，见 [ir.py:L315-L341](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ir.py#L315-L341)）。
3. 取这些变量的**当前值**作为 `initial_values`（循环入口的初值）。
4. 若是 for，额外创建**归纳变量**（induction variable，即 `k`），作为循环体 Block 的第一个参数。
5. 把每个被赋值变量在循环体内**重定义**为新的 SSA 变量（`body_vars`），作为循环体 Block 的后续参数。
6. 降级循环体；体末若无显式跳转，自动补 `continue`。
7. 处理 `continue` / `break`：把它们的「下一轮值 / 出口值」回填进对应的 phi，并更新 `Continue` / `Break` 操作的操作数。
8. 生成 `Loop` 操作：for 时带 `start/stop/step`（编成 `ForOp`），while 时不带（编成 `LoopOp`）。
9. 把循环结果绑定回外层名字。

一条贯穿全程的规则：**只有「在循环体内被重新赋值」的变量才需要走携带值机制**。只读不写的变量（如 load 一次就不变的 tile）不会进入 `stored_locals`，开销更小。

#### 4.3.3 源码精读

**（a）`Loop` 操作的字段** —— 它清楚地展示了「携带值」如何落在数据结构上（[control_flow_ops.py:L33-L52](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/control_flow_ops.py#L33-L52)）：

```python
@dataclass(eq=False)
class Loop(Operation, opcode="loop"):
    start: Var | None = operand()        # for 有，while 为 None
    stop: Var | None = operand()
    step: Var | None = operand()
    initial_values: tuple[Var, ...] = operand()   # 携带变量的初值
    body: Block = nested_block()                  # 循环体

    @property
    def is_for_loop(self) -> bool:
        return self.start is not None             # 靠 start 是否为 None 区分 for/while

    @property
    def induction_var(self):
        ... return self.nested_blocks[0].params[0]   # 循环体第 0 个参数 = 归纳变量 k

    @property
    def body_vars(self) -> tuple[Var, ...]:
        return self.body.params[1:] if self.is_for_loop else self.body.params  # 携带变量
```

`initial_values` 就是 4.3.1 里说的 \(\xi_{\text{init}}\)；`body_vars` 是每轮迭代里这些变量的「入口形态」。for 循环体 Block 的参数序列是 `(归纳变量 k, 携带变量1, 携带变量2, ...)`，while 则没有归纳变量，直接是 `(携带变量1, ...)`。

**（b）for 与 while 的字节码分叉** —— 这是 for 不支持 break 的根因（[control_flow_ops.py:L54-L79](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/control_flow_ops.py#L54-L79)）：

```python
if self.is_for_loop:
    start, stop, step = (ctx.get_value(x) for x in (self.start, self.stop, self.step))
    nested_builder = bc.encode_ForOp(ctx.builder, result_type_ids, start, stop, step,
                                     initial_values, unsignedCmp=False)   # 结构化计数循环
    ...
else:
    nested_builder = bc.encode_LoopOp(ctx.builder, result_type_ids, initial_values)  # 通用循环
```

`ForOp` 是「定数计数循环」，没有 break 口子；`LoopOp` 是「靠 break 结束的通用循环」。前端为了不破坏 `ForOp` 的语义，干脆在 `_break_stmt` 里禁掉 for+break。

**（c）`Loop` 的可读形态** —— `_to_string_rhs` 告诉我们 dump 出来时长什么样（[control_flow_ops.py:L86-L103](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/control_flow_ops.py#L86-L103)）：

```python
if self.is_for_loop:
    header_str = (f"for {self.body.params[0].name}"
                  f" in range({self.start.name}, {self.stop.name}, {self.step.name})")
else:
    header_str = "loop"
carried_vars_str = ", ".join(f"{format_var(b)} = {i.name}"
                             for b, i in zip(body_vars, self.initial_values))
return f"{header_str} (with {carried_vars_str})"
```

也就是说，一个带累加器 `xi` 的 for 循环，dump 出来大致形如（示例，具体名字以本地 dump 为准）：

```
%xi_out = loop  for %k in range(%start, %stop, %step) (with %xi_in: tile<...> = %xi_init)
  do(%k, %xi_in):
    %xi_new = ...        # xi + 1 之类
    continue %xi_new
```

`(with %xi_in = %xi_init)` 这一段正是「携带值」的可视化：把外层的初值 `%xi_init` 接到循环体入参 `%xi_in` 上，循环结束时再产出 `%xi_out`。

**（d）一个真实的携带值例子** —— 测试 `plus_n_one_arg` 把循环计数 n 重复累加到 tile 上（[test_control_flow.py:L18-L24](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_control_flow.py#L18-L24)）：

```python
@ct.kernel
def plus_n_one_arg(x, n, tile: ct.Constant[int]):
    i = ct.bid(0)
    xi = ct.load(x, index=(i,), shape=(tile,))   # xi_init
    for _ in range(n):
        xi += 1                                   # 每轮产生新 xi，作为携带值
    ct.store(x, index=(i,), tile=xi)              # 用循环结果的 xi
```

这里 `xi` 就是被携带的变量：`initial_values=(xi_init,)`，每轮 `continue xi_new`，循环结果回填给外层 `xi`。注意循环变量名是 `_`（在 HIR 里仍是一个归纳变量，只是用户没用到）。

而 GEMM 里的累加器是更现实的携带值例子（[samples/MatMul.py:L71-L93](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/MatMul.py#L71-L93)）：

```python
accumulator = ct.full((tm, tn), 0, dtype=ct.float32)   # initial_value
for k in range(num_tiles_k):
    a = ct.load(A, index=(bidx, k), shape=(tm, tk), ...).astype(dtype)
    b = ct.load(B, index=(k, bidy), shape=(tk, tn), ...).astype(dtype)
    accumulator = ct.mma(a, b, accumulator)             # 每轮更新携带值
...
ct.store(C, index=(bidx, bidy), tile=accumulator)       # 用最终累加结果
```

`accumulator` 沿 K 维一圈圈流动，正是 `Loop (with accumulator = 0)` + 每轮 `continue %new_acc` 的典型形态。

#### 4.3.4 代码实践

**实践目标**：通过 dump 出的 IR，亲眼看到「携带值」是如何把循环串起来的。这是一次**源码阅读 + IR 观察型实践**。

**操作步骤**：

1. 准备一个最小携带值内核（示例代码）：

```python
# 示例代码
import cuda.tile as ct
import torch

@ct.kernel
def accumulate(x, n, tile: ct.Constant[int]):
    i = ct.bid(0)
    xi = ct.load(x, index=(i,), shape=(tile,))
    for _ in range(n):
        xi = xi + 1
    ct.store(x, index=(i,), tile=xi)
```

2. 设置环境变量开启 TileIR 中间产物 dump（具体变量名与用法见 u8-l5，此处为 `CUDA_TILE_DUMP_TILEIR`）：在启动前 `export CUDA_TILE_DUMP_TILEIR=1`，再运行一次只触发编译的小脚本（不必真跑 GPU，编译阶段就会 dump）。
3. 在 dump 文本里定位 `loop` 操作，找到 `(with ... = ...)` 这一段。

**需要观察的现象**：

- dump 中应出现一个 `for ... in range(...)` 形态的 `loop` 操作，其 `with` 子句里有一个携带变量，初值就是 load 出来的那个 tile。
- 循环体 `do(...)` 的参数里，第一个是归纳变量（对应源码里的 `_`），其后是携带变量的「入口形态」。
- 循环体末尾应有一条 `continue`，带一个操作数——即每轮算出的新 tile。

**预期结果**：你能用 4.3.3 里 `_to_string_rhs` 的格式（`for %k in range(...) (with %xi_in: ... = %xi_init)`）逐字段对上 dump 的内容。**待本地验证**：确切的变量名、行数与缩进以你本地的 dump 输出为准；若 dump 默认未开启，请按 u8-l5 的环境变量说明打开。

#### 4.3.5 小练习与答案

**练习 1**：如果一个循环体里**只读不写**任何外层变量（比如只 `ct.store` 到全局数组，不更新任何 tile），它还会有 `initial_values` 和携带值吗？

> **答案**：不会。`loop_impl` 只为「循环体内被赋值的变量」（`body.stored_indices`）建立携带值（[control_flow_ops.py:L122-L126](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/control_flow_ops.py#L122-L126)）。只写全局数组、不重新绑定任何名字时，`initial_values` 为空，循环不携带任何值，结构更简单。

**练习 2**：`while True:` 加 `break` 是 cuTile 里常见的「至少执行一次再判断」写法（见 [test_control_flow.py:L305-L322](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_control_flow.py#L305-L322)）。它在底层是 `ForOp` 还是 `LoopOp`？为什么可以 break？

> **答案**：是 `LoopOp`。因为 `while` 在前端降级时 `iterable=None`（[ast2hir.py:L936](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_passes/ast2hir.py#L936)），`loop_impl` 里 `range_ty` 为 None，走 `encode_LoopOp` 分支（[control_flow_ops.py:L67-L68](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/control_flow_ops.py#L67-L68)）。`LoopOp` 本来就靠 `BreakOp` 结束，所以 while 里能用 break，而 for（`ForOp`）不能。

---

## 5. 综合实践

把本讲的三块内容（`if`、`for`+`range`、携带值循环）串起来，完成下面这个小内核。

**任务**：实现一个「**带条件守卫的逐块累加**」内核 `guarded_accumulate`：

- 输入数组 `x`（1D，float32）；参数 `n`（每个 block 要累加的次数）、`tile: ct.Constant[int]`。
- 对**每一个** block：load 出自己的 tile `xi`；用 `for _ in range(n): xi = xi + 1` 累加 `n` 次。
- 但只有当 `ct.bid(0)` 为**偶数**时才把结果 store 回 `x`；奇数 block 不写（保持原值）。
- host 端用 `ct.cdiv` 计算 grid，用 `torch` 张量验证：偶数 block 的元素应为 `n`，奇数 block 应为 0。

**参考骨架**（示例代码，需你补全 host 端）：

```python
# 示例代码
import cuda.tile as ct
import torch

@ct.kernel
def guarded_accumulate(x, n, tile: ct.Constant[int]):
    i = ct.bid(0)
    xi = ct.load(x, index=(i,), shape=(tile,))
    for _ in range(n):           # 4.2: range，step 默认 1（>0，合法）
        xi = xi + 1              # 4.3: xi 是携带值
    if i % 2 == 0:               # 4.1: 条件守卫
        ct.store(x, index=(i,), tile=xi)

N, TILE, n = 256, 128, 5
x = torch.zeros(N, dtype=torch.float32, device="cuda")
grid = (ct.cdiv(N, TILE), 1, 1)
ct.launch(torch.cuda.current_stream(), grid, guarded_accumulate, (x, n, TILE))

block_sums = x.cpu().reshape(-1, TILE).sum(dim=1)
print(block_sums)   # 期望：[640., 0., 640., 0.]  （偶数 block 5*128=640）

# 思考：把这个内核改成 while 循环版本，并把 n 换成「累加到等于阈值为止」，
# 体会 while + break 与 for 的差异，以及为何 while 版能 break 而 for 版不能。
```

**验收点**：

1. 输出偶数 block 求和为 `n * TILE`，奇数 block 为 0。
2. 能解释：dump 出来的 IR 里应当同时出现一个 `Loop`（携带 `xi`）和一个 `IfElse`（包住 `store`）。
3. 把 `for` 版改写成等价的 `while` 版（用计数器 + 条件），并验证结果一致；再尝试在 `for` 版里加 `break`，确认会收到 `TileSyntaxError`。

**待本地验证**：上述打印数值与 dump 形态以你本机的 GPU、驱动与 tileiras 版本的实际输出为准。

## 6. 本讲小结

- tile code 支持 `if/else`、`for ... in range(...)`、`while`，且可**任意嵌套**；但它是「被翻译的 Python」，没有 Python 运行时。
- `range` 的 **`step` 必须严格 > 0**：常量负步长会在 `range_` 里被直接拒绝（`FIXME Issue 314`），通过变量间接传负步长属**未定义行为**。
- `break` **只在 `while` 里支持**，`for` 里不支持；`continue` 在 for/while 都支持；不支持 `for-else`、`while-else`、`match-case`、异常。
- for 与 while 在前端就分道扬镳：for 带 `iterable=RangeValue`，while 的 `iterable=None` 且靠「体首插 `if cond: pass; else: break`」实现条件。
- 控制流在 IR 层是结构化的：`if` → `IfElse`（then/else 两个嵌套 Block），循环 → `Loop`（for 带 `start/stop/step` 编成 `ForOp`，while 不带、编成 `LoopOp`）。
- 因为 tile 不可变，循环里被重新赋值的变量会变成**携带值（carried values）**：初值经 `initial_values` 入循环体，每轮由 `continue` 回传，结束时产出循环结果——本质是 SSA / 函数式 fold。

## 7. 下一步学习建议

- **想写出真正的循环内核**：下一讲 u3-l4（归约与扫描）会讲 `ct.sum/max/...` 与 `cumsum/scan`，它们常和本讲的 `for` 循环搭配（例如在循环里做局部归约）。随后 u3-l6 的分块 GEMM 是「携带值累加循环」最经典的实战。
- **想搞懂本讲那些 IR 操作的「上层」**：本讲反复出现的 `Loop` / `IfElse` / `PhiState` / `LoopVarState` 属于 Tile IR 核心，u5-l5 会系统讲 `IRContext/Builder/Block/Var/Operation`；`if_else_impl` / `loop_impl` 的完整降级逻辑在 u5-l4（hir2ir）深入展开。
- **想看控制流如何变成字节码**：`Loop.generate_bytecode` / `IfElse.generate_bytecode` 里的 `encode_ForOp` / `encode_IfOp` 等属于后端，在 u7-l1（ir2bytecode）与 u7-l2（字节码格式）详述。
- **想调试本讲的内核**：综合实践里用到的 `CUDA_TILE_DUMP_TILEIR` 等环境变量在 u8-l5（调试与性能）有完整清单。
