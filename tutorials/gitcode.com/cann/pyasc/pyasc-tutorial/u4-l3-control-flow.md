# 控制流：for 循环、if 分支与 range/static_range

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `for i in range(...)` 与 `for i in asc.static_range(...)` 在 pyasc 里的两条完全不同的落地路径：前者生成 `scf.for` 循环 IR，后者在编译期被逐圈展开成顺序代码。
2. 根据循环次数是否编译期已知、循环体大小与性能诉求，为算子选择 `range` 或 `static_range`，并说清两者的代价（IR 体积、设备侧指令差异）。
3. 读懂 `BlockInOut` 与 `compute_inout`：块内改写外层变量时，pyasc 如何用「进块值（init_handles）+ 出块值（yield_values）」把 Python 的可变变量翻译成 MLIR 的 SSA 块进块出值。
4. 解释 `if` 分支的两种命运：编译期条件直接剪枝（不生成 IR 控制流），运行期条件生成 `scf.if` 并对两个分支改写的变量做类型一致的值合并。
5. 掌握 return 的规则：为什么运行时 if/for 里不能 return、为什么核函数不能返回对象、`ReturnTypesDict` 如何按函数名缓存返回类型。

本讲是第 4 单元的第三讲，承接 u4-l1（FunctionVisitor 架构）与 u4-l2（赋值、运算符、NameScope），把「一条条语句怎么变成 IR」推进到最复杂的部分——结构化控制流。

## 2. 前置知识

### 2.1 SSA：IR 里没有「变量」，只有「值」

MLIR 遵循 SSA（Static Single Assignment，静态单赋值）原则：每个 IR 值只被定义一次，之后不可修改。这和 Python 的习惯正相反：

```python
j = 0
for i in range(10):
    j = j + i   # Python：j 被覆盖；SSA：这里产生一个"新值" %j_1
```

在 SSA 世界里，「循环里改了 j」必须表达成：`scf.for` 带一个迭代参数 `%j_iter`（初值 `%c0`），循环体结尾 `scf.yield` 一个新值，下一轮的 `%j_iter` 就是这个新值；循环结束后 `%j_iter` 的最终结果作为 `scf.for` 的返回值继续被外层使用。**本讲一半的内容，都是在讲 pyasc 如何自动完成这套「可变变量 → 块进块出」的机械翻译。**

### 2.2 MLIR 的 scf 方言：结构化控制流

scf（Structured Control Flow）是 MLIR 内置的控制流方言，常见操作：

| IR 操作 | 含义 |
| --- | --- |
| `scf.for %i = %lb to %ub step %step iter_args(%a = %init) -> (T)` | 计数循环，`iter_args` 是循环携带值 |
| `scf.if %cond -> (T) { ... } else { ... }` | 分支，可带返回值（两个分支各自 `scf.yield`） |
| `scf.while` / `scf.yield` / `scf.condition` | 条件循环及其出口 |

在 u1-l5 你已经用 `PYASC_DUMP_PATH` 导出过 `codegen.mlir`，本讲会频繁回到这份文件里找 `scf.for` 和 `scf.if`。

### 2.3 编译期与运行期：本讲的隐形主角

- **编译期**：JIT 编译发生的那一刻（Host 上）。此时能确定的是 Python 字面量、`ConstExpr` 实参、模块级常量（经 NameScope 全局表快照）。
- **运行期**：Kernel 在设备上执行的时刻。此时才确定的是未标 `ConstExpr` 的标量参数（如 Add 示例的 `block_length`）、归纳变量 `i` 等，它们在前端的表现是 `PlainValue`（IR 值，见 u2-l3）。

一个条件/循环边界到底是编译期还是运行期，直接决定 pyasc 走哪条代码路径——这是本讲反复出现的判断依据。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/asc/language/core/range.py` | 定义 `range` 与 `static_range` 两个循环迭代器类，是本讲两种语义的源头 |
| `python/asc/codegen/function_visitor.py` | `visit_For`、`handle_static_range`、`compute_inout`、`BlockInOut`、`visit_If`、`visit_IfExp`、`visit_Return`、`visit_While` 全部在此 |
| `python/asc/codegen/name_scope.py` | `NameScope.save` 的 `defined`/`redefined` 记账，是 `compute_inout` 找出「循环携带变量」的依据 |
| `python/asc/language/core/ir_value.py` | `PlainValue` 的运算符落点（如 `i % 2` → `arith.remsi`），解释循环体内 IR 的来源 |
| `examples/01_add/add.py` | 综合实践对象：把它的 `for` 循环改写成两个版本对比 IR |
| `python/test/unit/codegen/test_function_visitor.py` | `scf.if` / `scf.for` 形态的回归断言，是实践依据 |
| `python/asc/runtime/compiler.py` | `codegen.mlir` 的 dump 位置（Pass 前 IR），综合实践要读它 |

## 4. 核心概念与源码讲解

### 4.1 两个 range 类：同一语法，两种落地

#### 4.1.1 概念说明

Python 的 `for i in range(N)` 依赖解释器逐圈调用 `__next__`。但 JIT 编译时没有解释器在场——`range` 在 pyasc 里只是一个**语法载体**：`FunctionVisitor` 拦截 `for` 语句的迭代器构造，读取它的 `start/stop/step` 三个属性，然后自己决定怎么「循环」。

pyasc 提供两个同构的类，语义截然不同：

- `range`（`asc.range`）：三个边界存为 `RuntimeInt`——**允许是运行期值**。前端一律把它翻译成 `scf.for` 循环 IR，循环真正发生在设备上。
- `static_range`（`asc.static_range`）：三个边界存为普通 `int`——**必须是编译期常量**。前端在编译期用普通 Python 循环把循环体逐圈「抄写」N 遍，完全不生成循环 IR。

注意：kernel 里直接写裸 `range(...)`（Python 内建，在 builtins 白名单里）与写 `asc.range(...)` 效果相同，都走 `scf.for` 路径。

#### 4.1.2 核心流程

```text
for i in <iterator>(...):
    body
        │
        ▼
visit_For 解析 iterator 的构造函数与实参
        │
        ├─ 构造函数是 static_range ──► handle_static_range：
        │       编译期 Python for 循环 N 圈，每圈把 i 存成普通 int，
        │       把 body 的 AST 重新 visit 一遍（完全展开，无循环 IR）
        │
        └─ 构造函数是 range / asc.range ──► 构造 _range 对象取三边界
                ──► materialize_ir_value 落成 i32 IR 值（常量或运行时值）
                ──► compute_inout 预建循环体块（含携带变量记账）
                ──► create_scf_ForOp 组装 scf.for + scf.yield
                ──► 循环结果重新绑定回外层作用域
```

#### 4.1.3 源码精读

先看两个类本身。`range` 的边界声明为 `RuntimeInt`（`int` 或 `PlainValue` 皆可），并明确禁止真正迭代：

[python/asc/language/core/range.py:L14-L46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/range.py#L14-L46)
上面定义了 `range` 类：三个 `@overload` 只是给类型检查器看的签名；真正的 `__init__` 只接收 1~3 个参数，按 Python `range` 的惯例填充 `start/stop/step`，字段的类型标注是 `RuntimeInt`——即允许传入 IR 值（如某个未标 ConstExpr 的 int 参数）。`__next__` 直接 `raise NotImplementedError("This function must not be called")`，说明它永远不会被真正迭代，只充当三边界的容器。

[python/asc/language/core/range.py:L49-L81](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/range.py#L49-L81)
这是 `static_range`：结构完全相同，唯一差别是字段类型标注从 `RuntimeInt` 换成了 `int`。这个「标注差异」就是两种语义的全部前置约定——后面 `handle_static_range` 会直接拿这三个属性去做普通 Python 运算，传 IR 值进来会在编译期立刻报错。

两者的分发点在 `visit_For` 里（详见 4.2），靠的是函数对象同一性比较：

[python/asc/codegen/function_visitor.py:L27](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L27)
`FunctionVisitor` 把 `range` 导入为别名 `_range`，与内建 `range` 在 `visit_For` 中一并识别。

[python/asc/codegen/function_visitor.py:L487-L497](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L487-L497)
这段是 `visit_For` 的分流逻辑：`func is static_range` 走编译期展开并直接 `return`；`func is range or func is _range` 构造 `_range` 对象取出三边界；其它迭代器（包括对 list、生成器的 for）一律抛「Only for-loops with range or asc.language.range or asc.language.static_range are supported」。注意这里用的是 `is`（同一性）而非 `==`，所以模块里自定义一个同名 `range` 类是骗不过去的。

`static_range` 的展开实现只有五行，却是本讲最重要的对照样本：

[python/asc/codegen/function_visitor.py:L473-L477](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L473-L477)
`handle_static_range` 用**真正的 Python for 循环**在编译期跑 N 圈：每圈先 `scope.save(target, i)` 把循环变量绑成一个普通 Python `int`，再 `visit_statements(node.body)` 把循环体语句从头访问一遍。也就是说，`for i in asc.static_range(16)` 会让循环体的 AST 被 visit 16 次，生成 16 份顺序排列的 IR——没有任何 `scf.for`。若三边界中混入了 `PlainValue`，Python 的内建 `range()` 会在编译期抛 `TypeError`，经 `visit` 的异常包装（u4-l1）转成带源码定位的 `CodegenError`。

两个类的导出链路（保证 kernel 里能写 `asc.static_range`）：`core/__init__.py` 把它们放进 `__all__`（[python/asc/language/core/__init__.py:L203-L204](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/__init__.py#L203-L204)），逐级汇入 `asc` 包，因此 `asc.range` / `asc.static_range` 与裸 `range` 在 kernel 内都可用。

#### 4.1.4 代码实践

1. **实践目标**：不运行任何 kernel，仅通过源码确认两个类的「一字之差」与 `__next__` 的防御式设计。
2. **操作步骤**：
   - 打开 `python/asc/language/core/range.py`，并排阅读 L28-40 与 L63-75 两个 `__init__`。
   - 用编辑器diff 或肉眼对比，记录所有不同点（提示：只有三个字段类型标注不同）。
   - 在安装好 pyasc 的环境里执行（待本地验证）：
     ```bash
     python3 -c "
     from asc.language.core.range import range as asc_range, static_range
     r = asc_range(4); s = static_range(2, 8, 2)
     print(r.start, r.stop, r.step, '|', s.start, s.stop, s.step)
     print(next(iter(r)))
     "
     ```
3. **需要观察的现象**：第一行应打印 `0 4 1 | 2 8 2`；第二行应抛出 `NotImplementedError: This function must not be called`。
4. **预期结果**：确认这两个类只是「三边界容器」，迭代协议被刻意禁用——真正决定循环形态的是 `visit_For` 里的分流，而不是这两个类自身。
5. 上述第二条命令的输出为**待本地验证**（需要已安装 pyasc；`range.py` 依赖链会引入 `_C` 扩展，无法脱离安装环境运行）。

#### 4.1.5 小练习与答案

**练习 1**：`for i in asc.range(n)` 中 `n` 是 kernel 的普通 int 参数（未标 ConstExpr），会发生什么？换成 `asc.static_range(n)` 呢？

**答案**：`asc.range(n)` 没问题——`n` 是 `PlainValue`（运行期值），作为 `RuntimeInt` 存入 `_range`，随后被 `materialize_ir_value` 落成 i32 的函数参数句柄，生成一条边界为运行时值的 `scf.for`。`asc.static_range(n)` 会在编译期失败：`handle_static_range` 用 `n`（`PlainValue`）去调 Python 内建 `range()`，抛 `TypeError`，最终被包装成 `CodegenError`，报错信息会带 kernel 源码定位。

**练习 2**：为什么 `visit_For` 用 `func is static_range` 而不是 `isinstance(fn, static_range)` 判断？

**答案**：因为比较对象是**类本身**而不是实例——`parse_iterator` 只 visit 了 `node.iter.func`（即名字 `static_range` 解析出的类对象），实例尚未构造。`is` 比较类对象的同一性，任何「长得像」的自定义类都无法冒充；若用 `isinstance` 比较类对象反而类型不合（类不是自身的实例），语义完全错误。

**练习 3**：`docs/python_syntax_support.md` 的支持列表里有 `continue` 的示例，但本讲说 break/continue 落到白名单拒绝。请设计一个一分钟的验证办法。

**答案**：在任一 kernel 的 `for i in range(4):` 循环体里写 `continue` 并触发编译，观察是否抛 `UnsupportedSyntaxError: Continue syntax is not supported in JIT function`。依据是 `function_visitor.py` 中不存在 `visit_Continue`/`visit_Break`（可先 `grep -n "visit_Continue\|visit_Break" python/asc/codegen/function_visitor.py` 确认为空），未覆写的节点统一落入 `generic_visit` 的白名单拒绝。文档示例与实现存在出入，以源码与实际报错为准（结果待本地验证）。

### 4.2 for 循环 IR 化：visit_For 与 scf.for

#### 4.2.1 概念说明

`range` 路径的目标是生成一条结构化循环 IR：

```mlir
%sum = scf.for %i = %c0 to %c16 step %c1 iter_args(%j = %c0_i32) -> (i32) {
  %j1 = arith.addi %j, %i : i32
  scf.yield %j1 : i32
}
```

三件事必须自动完成：

1. **边界物化**：`start/stop/step` 统一落成 int32 IR 值（常量就是 `arith.constant`）。
2. **循环携带变量的接线**：体内改写过的外层变量要接成 `iter_args`（进块）与 `scf.yield`（出块）。
3. **循环后重绑定**：循环结束后，这些变量在 Python 作用域里要指向 `scf.for` 的结果值，后续语句才能继续用。

其中 2 由 `compute_inout` 完成（4.3 详述），这里先看主流程。

#### 4.2.2 核心流程

```text
visit_For(node)
  ├─ 1. 拒绝 for...else；target 必须是单个标识符
  ├─ 2. parse_iterator：解析 range(...) 的类对象与实参（实参此时已 visit，
  │        常量折叠成 int，运行时值保持 PlainValue）
  ├─ 3. static_range 分流（见 4.1），此处继续 range 路径
  ├─ 4. materialize_ir_value(start/stop/step, int32)；
  │        三边界 DataType 不一致 → 报错
  ├─ 5. compute_inout(body, ind_var=(target, start 类型), make_args=True)
  │        在"游离块"里预visit 循环体，记账携带变量
  ├─ 6. create_scf_ForOp(start, stop, step, init_handles)
  │        target ← PlainValue(归纳变量)
  │        游离块内联进 scf.for 的 body，参数对齐
  │        create_scf_YieldOp(yield_handles)
  └─ 7. 循环结果 from_ir 后 scope.save 回外层作用域
```

#### 4.2.3 源码精读

[python/asc/codegen/function_visitor.py:L479-L501](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L479-L501)
`visit_For` 的前半段：L480-481 拒绝 `for...else`；L482-484 要求循环目标是普通标识符（不能 `for a, b in ...`）；L485 调 `parse_iterator` 拿到迭代器类与实参；L487-497 分流（4.1 已读）。L499 把三边界统一物化为 `KnownTypes.int_`（int32）的 `PlainValue`——纯 Python 常量在这里变成 `arith.constant`，运行时参数保持为参数句柄；L500-501 要求三边界 DataType 一致，否则报「Loop bounds must have the same DataType」。

[python/asc/codegen/function_visitor.py:L502-L516](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L502-L516)
后半段是 `scf.for` 的组装：L504 在 `visit_region` 上下文里调 `compute_inout` 预建循环体（传入归纳变量的名字与类型，`make_args=True` 表示携带变量初值也要变成块参数）；L505-506 创建 `scf.for`，`iter_args` 就是 `init_handles`；L507 把循环变量重新绑定为归纳变量的 `PlainValue`；L508-510 清空 `scf.for` 自带的空 body，把预建块内联进去并完成参数对接；L512 在 body 末尾补 `scf.yield`，把块末的携带值交还；L513-516 用 `from_ir` 把 `scf.for` 的每个结果包装回 `IRValue` 并 `scope.save`——**循环结束后 Python 名字指向的是循环结果**，这正是 SSA 与 Python 变量模型对接的收尾一步。

回到 Add 示例看真实效果：

[examples/01_add/add.py:L49-L54](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L49-L54)
`for i in range(TILE_NUM * BUFFER_NUM)`：`TILE_NUM`/`BUFFER_NUM` 是模块级普通 int（u3-l2 的全局快照），编译期 `8 * 2` 直接折叠成 `16`，因此生成的 `scf.for` 边界是三个常量（`0 to 16 step 1`）。循环体里的 `buf_id = i % BUFFER_NUM` 中 `i` 是归纳变量（`PlainValue`），`BUFFER_NUM` 是常量 2，于是生成一条**设备上每轮执行的取余指令**：

[python/asc/language/core/ir_value.py:L94-L96](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L94-L96)
`PlainValue.__mod__` 委托 `apply_binary_op(..., "RemSI", None)`，即 [python/asc/language/core/ir_value.py:L256-L264](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L256-L264) 里通过 `create_arith_RemSIOp` 生成的 `arith.remsi`。这就是「range 版循环的索引运算发生在设备上」的具体落点。

对照参考，`while` 循环也支持（生成 `scf.while`，before/after 两个子块 + `scf.condition`/`scf.yield`），实现见 [python/asc/codegen/function_visitor.py:L692-L710](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L692-L710)，同样复用 `compute_inout`，本讲不展开。

#### 4.2.4 代码实践

1. **实践目标**：在 dump 的 IR 中确认「一条 `scf.for` + 一份循环体 + 设备侧取余指令」。
2. **操作步骤**（延续 u1-l5 的方法，需已按 u1-l2 安装环境）：
   ```bash
   cd examples/01_add
   mkdir -p /tmp/ir_range
   PYASC_DUMP_PATH=/tmp/ir_range python3 add.py -r Model always_compile=True
   grep -n "scf.for" /tmp/ir_range/codegen.mlir
   grep -n "remsi" /tmp/ir_range/codegen.mlir
   ```
   说明：`always_compile=True` 绕过两级缓存（u3-l8），保证本次一定真编译并落盘 dump。
3. **需要观察的现象**：`scf.for` 恰好 1 处，形如 `scf.for %arg = %c0_i32 to %c16_i32 step %c1_i32`；`remsi` 出现在循环体内（`i % 2`）；循环体语句（data_copy/add/set_flag 等）只出现一份。
4. **预期结果**：range 版 Add 的 IR 中，循环体代码只编译一次、执行 16 次；索引计算（取余、乘 tile_length）都留在设备上每轮执行。
5. 本实践**待本地验证**（需要完整的 pyasc 安装与 Model 仿真环境；无 NPU 时 `-r Model` 即可）。

#### 4.2.5 小练习与答案

**练习 1**：把 `range(TILE_NUM * BUFFER_NUM)` 写成 `range(0, TILE_NUM * BUFFER_NUM, 2)`，功能上会有什么变化？IR 上呢？

**答案**：功能上循环圈数减半（步长 2，只处理偶数圈），Add 结果会错（一半数据没算）。IR 上只是 `scf.for` 的 `step` 边界从常量 1 变成常量 2，结构不变。这提示三边界与循环次数是解耦的——步长错了前端不会替你检查语义。

**练习 2**：循环体里 `i` 的 Python 类型是什么？`type(i)` 在 kernel 里合法吗？

**答案**：`i` 被绑定为 `PlainValue(op.get_induction_var())`（L507），是包装归纳变量的 IR 值。kernel 里没有 `type` 可用——builtins 白名单（[python/asc/codegen/name_scope.py:L13-L29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L13-L29)）虽然收录了 `type` 这个名字，但它拿到的是 `PlainValue` 对象，运行语义与 Python 直觉不同，调试请用 `PYASC_DUMP_PATH` 看 IR 而不是在 kernel 里内省。

### 4.3 BlockInOut：块进块出的记账与接线

#### 4.3.1 概念说明

`compute_inout` 是 for/if/while 共用的「块预构建器」。它回答一个问题：**把一段语句包进一个 IR 块（循环体、分支体）后，哪些外层变量被改写了？进块时用哪个值、出块时产出哪个值？**

答案装在 `BlockInOut` 里：

[python/asc/codegen/function_visitor.py:L60-L65](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L60-L65)
四个字段：`block` 是预建好的游离 IR 块（稍后被内联进真正的 `scf.for`/`scf.if`）；`init_handles` 是「进块前」各携带变量的 IR 句柄（成为 `iter_args` 初值）；`yield_values` 是「出块时」的 Python 级对象；`yield_handles` 是对应的 IR 句柄（成为 `scf.yield` 的操作数）。

而「哪些变量算携带变量」由 NameScope 的记账机制决定：

[python/asc/codegen/name_scope.py:L45-L50](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L45-L50)
`save` 的规则：块内**首次**保存一个**外层已有**的名字时记入 `redefined`（这些名字既在外层存在、又在块内被改写，正是携带变量候选）；块内新起的名字记入 `defined`（块内私有，不参与进出块）。`inherit()`（L41-43）复制 `local_vars` 但清空两个记账集合，所以每个块的记账都是从零开始、只反映「本块改写了哪些外层名字」。

#### 4.3.2 核心流程

`compute_inout(node, stmts, ind_var=None, make_args=False)` 的执行过程：

```text
1. 开 visit_region：作用域继承副本、保存插入点、return_allowed=False
2. new ir.Block()（游离块，尚未挂在任何 op 上）
3. 若 ind_var 给出（for 循环）：块头加一个参数，target 绑定为该参数的 PlainValue
4. 插入点移入游离块，逐条 visit stmts（IR 全部落在这个块里）
5. 对 scope.redefined 中每个名字：
     a. 与外层值做类型一致性检查（Python 类不同 → 报错）
     b. materialize 外层值 → init_handles（进块值）
     c. make_args=True 时：为每个 init 增加块参数，
        并把块内对该值的使用替换成块参数（SSA 的 phi 接线）
     d. materialize 块末值 → yield_values / yield_handles（出块值）
6. 打包成 BlockInOut 返回
```

`make_args` 的取值是 for 与 if 的关键差异：**循环体每圈要用「本轮参数」而不是「循环前初值」**，所以 for 传 `make_args=True`，把初值接成块参数；**if 的分支体只执行一次**，直接引用外部句柄即可，所以 if 传 `False`（缺的名字在合并时用初值透传，见 4.4）。

#### 4.3.3 源码精读

[python/asc/codegen/function_visitor.py:L213-L241](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L213-L241)
逐段看：L215 开 `visit_region`（作用域与插入点的保存/恢复、禁 return，见 [L328-L340](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L328-L340)）；L216-220 建游离块并（对 for）把归纳变量接成第一个块参数；L221-222 在块内 visit 循环体语句；L223-229 对每个 `redefined` 名字做类型检查——注意比较的是 **Python 对象类**（`type(old) is not type(new)`），拦截「int 值改绑成 Tensor」这类跳变，报错信息为 `'{name}' was re-assigned to an object with different type`；L230 物化外层值得到 `init_handles`；L231-234 在 `make_args` 时把初值升级为块参数并 `replace_uses_in_block`（块内引用改指向块参数，即 SSA 的 φ 接线）；L235-241 物化块末值，连同 `block`、`init_handles` 一起打包成 `BlockInOut`。

用一个最小例子（示例代码，非项目文件）手工推演：

```python
@asc.jit
def loop_kernel(n: int, acc: int):     # 示例代码
    j = 0
    for i in range(n):
        j = j + i
        acc = j
```

- `redefined = {j, acc}`：`j`、`acc` 在外层已存在（`acc` 是 kernel 参数，`j` 是前一行的赋值），块内被改写；`i` 是归纳变量，不算。
- `init_handles = {j → %c0_i32, acc → %arg1}`，即 `scf.for` 的两个 `iter_args` 初值。
- `yield_handles = {j → %addi 结果, acc → 同一 %addi 结果}`，即 `scf.yield` 的两个操作数。
- 生成的 IR 骨架：

```mlir
%2:%3 = scf.for %i = %c0 to %arg0 step %c1
    iter_args(%j = %c0_i32, %acc = %arg1) -> (i32, i32) {
  %addi = arith.addi %j, %i : i32
  scf.yield %addi, %addi : i32, i32
}
```

（骨架为示意，真实打印以 dump 为准。）

#### 4.3.4 代码实践

1. **实践目标**：不经运行，手工推演一段循环的 `redefined`/`init`/`yield` 三张表，再用 dump 验证。
2. **操作步骤**：
   - 把上面 `loop_kernel` 抄进一个测试脚本（或改写 01_add 副本：在循环里累计一个标量 `j`），按 u1-l2/u1-l4 的方式以 Model 模式运行并 dump。
   - 在纸上写出三张表：`redefined` 集合、每个名字的 `init` 来源、每个名字的 `yield` 来源。
   - 打开 `codegen.mlir`，找到 `scf.for`，核对 `iter_args` 的名字/顺序/初值，以及 `scf.yield` 的操作数。
3. **需要观察的现象**：`iter_args` 与你推演的 `init_handles` 一一对应；`scf.yield` 与 `yield_handles` 一一对应；两个同名赋值（`j`、`acc`）在块内只产生一条 `arith.addi`（赋值不产生 IR，u4-l2）。
4. **预期结果**：理解「块内改写的外层变量 = 循环携带变量」，以及 `compute_inout` 把它们接成 φ 的全过程。
5. **待本地验证**（需要可运行环境；纸面推演部分无需环境）。

#### 4.3.5 小练习与答案

**练习 1**：循环体内新定义的局部变量（如 `buf_id`）为什么不出现在 `iter_args` 里？

**答案**：`buf_id` 在块内首次保存，按 `NameScope.save` 规则记入 `defined` 而非 `redefined`——它在外层不存在，没有「进块初值」可言，每圈从头定义，属于块内私有值，不需要跨圈传递。

**练习 2**：把循环里的 `j = j + i` 改成 `j = x_local`（一个 LocalTensor），会发生什么？

**答案**：`compute_inout` 的类型一致性检查（L226-229）发现外层 `j` 是 `PlainValue`、块末是 `LocalTensor`（Python 类不同），抛 `UnsupportedSyntaxError`，报错信息为 `j was re-assigned to an object with different type: initial type is PlainValue, new type is LocalTensor`。这保证携带变量的 IR 形态在循环中保持稳定，否则 φ 接线无从谈起。

**练习 3**：为什么 for 要传 `make_args=True` 而 if 传 `False`？

**答案**：循环体是多圈复用的同一份 IR，体内对携带变量的读取必须指向「本轮的块参数」（SSA φ），否则每圈读到的都是循环前的初值，语义错误；if 的分支体只会顺序执行一次，直接引用外部句柄即可，合并结果时未改写的分支用初值透传（4.4 的 `select`），无需块参数。

### 4.4 分支：编译期剪枝与 scf.if 的值合并

#### 4.4.1 概念说明

`if` 的第一道关口是条件求值 `ensure_bool_value`，它把条件分成两类：

- **编译期条件**：条件表达式在编译期折叠成 Python `bool`（由字面量、常量全局、ConstExpr 参与的比较得到）。pyasc 直接**剪枝**：只 visit 命中的那个分支，不生成任何 IR 控制流——另一分支像不存在一样。
- **运行期条件**：条件是 `PlainValue`（设备上才知道真假）。生成 `scf.if ... { ... } else { ... }`，且**两个分支都被编译**，改写过的变量通过 yield 合并。

三元表达式 `a if cond else b` 是独立实现（`visit_IfExp`）：条件即使是编译期常量也会物化成 IR，永远生成 `scf.if`，且要求两侧数值类型一致。

#### 4.4.2 核心流程

```text
visit_If(node)
  ├─ ensure_bool_value(test)
  │     ├─ 编译期 bool ──► 只 visit 命中分支的语句（当前作用域顺序展开，
  │     │     不开 visit_region → 分支内允许 return）
  │     └─ PlainValue ──► cast 到 i1
  └─ 运行期路径：
        ├─ compute_inout(body) / compute_inout(orelse) 各预建一个游离块
        ├─ merge_sorted(两分支 yield_values) → 按名字排序的并集
        ├─ create_scf_IfOp(cond, ret_types, with_else=True)
        ├─ then 块：内联 body 块；yield = select(并集, then 自身, else 的 init)
        │        （只在 else 改写的名字，then 用进块初值透传）
        ├─ else 块：对称处理
        └─ 结果 from_ir 后 scope.save 回外层
```

`elif` 链无需特殊处理：Python 的 AST 本来就把 `elif` 表示为 `orelse` 里的嵌套 `If` 节点，于是运行期 `elif` 自然变成 else 块里的嵌套 `scf.if`，编译期 `elif` 逐层剪枝。

#### 4.4.3 源码精读

[python/asc/codegen/function_visitor.py:L254-L263](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L254-L263)
`ensure_bool_value`：先 `ConstExpr.unwrap` 解包，若得到 `int/float/None` 且不要求 IR，就 `bool(value)` 返回 Python 布尔（编译期路径的判据）；否则必须是 `PlainValue`，经 `cast(KnownTypes.bit)` 落成 i1。既不是编译期值也不是 `PlainValue`（比如 Tensor）则报「Condition expression must be evaluated as PlainValue」。

[python/asc/codegen/function_visitor.py:L554-L561](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L554-L561)
`visit_If` 的编译期剪枝路径：`isinstance(cond, bool)` 成立时只 visit 命中分支的语句序列。注意这里**没有** `visit_region`——不新建作用域、不禁 return、不记账进出块，效果等同于把分支代码平铺到当前位置。这就是「编译期 if 里可以 return」的根源（4.5 会用到）。

[python/asc/codegen/function_visitor.py:L562-L596](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L562-L596)
运行期路径：L564-565 对 body/orelse 分别 `compute_inout`（各自在自己的 `visit_region` 里预建游离块并记账）；L567-571 用 `merge_dict`（`dict1 | dict2`，见 [python/asc/common/compat.py:L48-L53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/common/compat.py#L48-L53)）取两分支改写变量的**并集**再按名字排序——排序保证 IR 生成的确定性；L572-574 按并集类型创建带 else 的 `scf.if`；L576-591 是核心的 `select` 回退：then 块要 yield 并集里**所有**名字，但有的名字只在 else 分支被改写，于是回退用 `else_inout.init_handles`（进块初值）透传——两分支产出值的外形（数量与顺序）完全一致；else 块对称处理；L593-596 把 `scf.if` 的结果逐个 `from_ir` 后写回外层作用域。

**类型一致性的实现思路**由此清晰：单个分支内，`compute_inout` 已对每个改写名做「与外层 Python 类一致」的检查；两个分支之间，合并要求同一名字在两边的 IR 类型构成同一个 `scf.if` 的返回类型列表，不一致会在 MLIR verifier 处暴露。实践中保持「同名变量在同族标量/同类对象上改写」即可远离这条边界。

[python/asc/codegen/function_visitor.py:L598-L623](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L598-L623)
`visit_IfExp`（三元表达式）：`require_ir=True` 强制条件物化为 i1，两个候选值分别物化后要求 `dtype` 一致且为数值类型，随后组装成返回单值的 `scf.if`。与语句级 `if` 的编译期剪枝不同，三元表达式**总是**生成 `scf.if`。

回归测试坐实了这些形态：

[python/test/unit/codegen/test_function_visitor.py:L107-L123](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L107-L123)
`test_func_visit_if` 用 FileCheck 断言：`if/elif/else` 链生成 `scf.if %1 -> (i32) { } else { }` 且 else 内嵌套第二个 `scf.if`（elif 的 AST 嵌套直接映射为 IR 嵌套）。[L95-L104](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/codegen/test_function_visitor.py#L95-L104) 的 `test_func_visit_if_exp` 断言三元表达式生成 `scf.if ... -> (i32)`。FileCheck 的机制见 [python/test/unit/conftest.py:L18-L32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/conftest.py#L18-L32)：以注释中的 `CHECK` 行匹配 dump 出的模块文本。

#### 4.4.4 代码实践

1. **实践目标**：亲眼区分「编译期 if 被剪枝」与「运行期 if 生成 scf.if」。
2. **操作步骤**：
   - 写两个对照 kernel（示例代码，可放进你自己的实验脚本）：
     ```python
     import asc

     @asc.jit
     def branch_runtime(x: int, ans: int):
         if x > 0:              # x 是运行期参数 → scf.if
             ans = ans + 1

     @asc.jit
     def branch_static(ans: int):
         if 2 > 1:              # 编译期常量条件 → 剪枝
             ans = ans + 1
     ```
   - 分别以 `always_compile=True` 触发编译并 dump（沿用 4.2.4 的 `PYASC_DUMP_PATH` 方法）。
   - `grep -c "scf.if" <dump>/codegen.mlir` 对比两份 IR。
3. **需要观察的现象**：`branch_runtime` 的 IR 含一个 `scf.if`（`%cmp` 条件、then/else 两块、yield 合并 `ans`）；`branch_static` 的 IR **没有** `scf.if`，只有一条 `arith.addi`——else 分支（空）和判断本身都消失了。
4. **预期结果**：编译期分支是零成本抽象，运行期分支才付 IR 与执行代价。
5. **待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`if x > 0:`（x 为运行期 int 参数）中，条件最终是什么 IR 类型？

**答案**：`x > 0` 经 `apply_compare_op` 得到 `int1` 的 `PlainValue`（[python/asc/language/core/ir_value.py:L273-L282](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L273-L282)），`ensure_bool_value` 再 `cast(KnownTypes.bit)`（已是 i1 则原样返回），即 `arith.cmpi` 产生的 i1 值直接作为 `scf.if` 的条件。

**练习 2**：运行期 if 只在 then 分支改写 `ans`，IR 里 else 块的 yield 是什么？

**答案**：`select` 回退规则使 else 块 yield `else_inout.init_handles['ans']`，即进入分支前的初值（此时 else 分支体为空，init 句柄就是外层的 `ans`）。这样两个分支都产出 `ans`，`scf.if` 的结果类型完整。

**练习 3**：为什么 `and`/`or` 在 pyasc 里没有短路语义？（承接 u4-l2）

**答案**：`visit_BoolOp` 把两侧都求值后调用 `logical_and`/`logical_or`（[python/asc/codegen/function_visitor.py:L430-L436](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L430-L436)），两侧统一物化为 i1 再做位运算（`apply_bool_op`，ir_value.py L267-271）。短路要求「先算左边、按需跳过右边」的控制流，而结构化 IR 中两侧已是并列表达式，故不短路；这也解释了为何要求成对加括号（链式布尔运算被拒）。

### 4.5 return 语句与 ReturnTypesDict

#### 4.5.1 概念说明

return 是控制流里限制最多的语句，三条规则层层设卡：

1. **位置限制**：return 只能出现在函数顶层，不能嵌套在运行期 `if`、`for`、`while` 块内（`visit_region` 会把 `return_allowed` 置 False）。原因正是 SSA：块内的 return 意味着控制流提前离开结构化区域，需要结果穿越块边界合并，pyasc 未提供该机制。编译期剪枝的 if 分支里**允许** return——那些语句是被平铺到顶层的。
2. **身份限制**：核函数（`is_kernel=True`）不能返回任何对象；只有 Device 侧执行函数（被其他 jit 函数调用的子函数）可以返回标量/张量值（u4-l4 讲内联时还会用到）。
3. **死代码剪除**：return 之后的语句全部丢弃（`discard_everything`）。

返回类型通过两个数据结构流转：`ReturnType`（Python 类 + IR 类型的二元组）记录单个返回值；`ReturnTypesDict`（`Dict[函数名, List[ReturnType]]`，定义在 [python/asc/codegen/function_visitor.py:L48](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L48)）按函数名缓存已访问子函数的返回类型。

#### 4.5.2 核心流程

```text
visit_Return
  ├─ return_allowed 为 False（在 for/if/while 块内）→ 报错
  ├─ 求值返回值；置 discard_everything=True（其后语句全部剪除）
  ├─ is_kernel=True 且带返回值 → 报错 "JIT kernel function cannot return objects"
  └─ 记录 state.return_types（整组赋值，一条编译路径只有第一个 return 生效）
       └─ visit_FunctionDef 收尾：用 return_types 修补函数签名

多分支/多次调用的类型一致性：
  ├─ 运行期分支内 return：直接禁止（第一道卡）
  ├─ 编译期分支剪枝后：每条编译路径只剩一个有效 return，
  │     其后的 return 被 discard_everything 剪掉 → 只记录一组类型
  └─ 同一子函数被多次调用（如被 static_range 展开 16 圈调用）：
        模块里已有同名函数 → 直接复用 visited_return_types[fn_name]，
        不再重复访问函数体 → 多次调用的签名天然一致
```

#### 4.5.3 源码精读

[python/asc/codegen/function_visitor.py:L644-L660](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L644-L660)
`visit_Return` 全貌：L645-646 位置检查（「Return statement is not allowed in nested blocks」）；L647-648 求值并打开 `discard_everything`；L649-650 无返回值的 `return` 直接放行；L651-652 核函数带返回值即报错；L653-660 把返回值逐个物化，**整组赋值**给 `state.return_types`，并生成 `func.ReturnOp`。由于 `visit` 入口在 `discard_everything` 为真时直接返回（u4-l1 的 L296-297），同一条编译路径上后续所有语句（包括更多 return）都不会再被处理——这就是「每个编译路径只记录一组返回类型」的实现。

[python/asc/codegen/function_visitor.py:L548-L550](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L548-L550)
`visit_FunctionDef` 的收尾：函数体访问完后，若 `state.return_types` 非空，就用它重设函数签名（`set_type(builder.get_function_type(输入, 结果类型))`）——因为 `FuncOp` 创建时还不知道返回类型，签名是事后修补的。

[python/asc/codegen/function_visitor.py:L193-L202](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L193-L202)
`ReturnTypesDict` 的用武之地：`call_jit_function` 调用一个被 `@asc.jit` 修饰的子函数时，若 IR 模块里已有同名函数（说明本编译过程中已访问过），直接取 `visited_return_types[fn_name]` 作为返回类型，**不再**重新访问函数体；否则才创建新的 `FunctionVisitor` 递归访问并登记。子函数再次被调用（典型场景：`static_range` 展开后同一 compute 函数被内联多次）时，签名复用第一次的记录，保证多次调用类型一致。该字典还会通过构造参数在 kernel visitor 与子函数 visitor 之间共享（[L77-L92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L77-L92)）。

文档口径可对照 [docs/python_syntax_support.md:L337](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/python_syntax_support.md#L337)：return 支持为顶层语句、不能嵌套在 if/for 等结构中、核函数不能返回对象、Device 侧执行函数可以返回。源码语义与其一致，且比文档更细：编译期剪枝的 if 分支平铺于顶层，故其中的 return 实际可行。

#### 4.5.4 代码实践

1. **实践目标**：用三个最小 kernel 验证三条 return 规则，练习「预测 → 编译 → 核对报错」。
2. **操作步骤**：依次编译以下三个 kernel（示例代码），记录每个的报错或产物：
   ```python
   import asc

   @asc.jit
   def ret_kernel(x: asc.GlobalAddress):      # 场景 1：核函数带返回
       return 1

   @asc.jit
   def ret_nested(x: int, y: int):            # 场景 2：运行期 if 内 return
       if x > y:
           return x

   @asc.jit
   def ret_static(x: int):                    # 场景 3：编译期 if 内 return
       if 1 > 0:
           return x
   ```
   注意场景 2/3 需以 Device 子函数形式被调用才能触发编译（核函数本身不能有返回值）。
3. **需要观察的现象**：
   - 场景 1 报 `JIT kernel function cannot return objects`；
   - 场景 2 报 `Return statement is not allowed in nested blocks`；
   - 场景 3 编译通过，函数签名带一个 i32 返回值，且 IR 中没有 `scf.if`。
4. **预期结果**：三条规则全部命中；报错均带 kernel 源码定位（u4-l1 的异常包装）。
5. **待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `state.return_types` 用赋值而不是 append？多次 return 会不会互相覆盖？

**答案**：赋值正合适。一条编译路径上第一个 return 之后 `discard_everything=True`，后续 return 根本不会被访问，因此不存在「第二个 return 覆盖第一个」的运行风险；而不同编译路径（编译期 if 的不同剪枝结果）各自只激活一个 return，记录的正是该路径的类型。若用 append，反而会把不同来源的类型混进同一列表。

**练习 2**：一个 Device 子函数先在 `x > 0` 分支 return `int`、后顶层 return `float`，能编译过吗？

**答案**：不能走到「合并」那一步。运行期 `if` 块内的 return 在 `visit_Return` 第一道检查就被拒（`return_allowed=False`）；只有编译期剪枝的分支才可能含 return，而剪枝后另一分支连同其 return 一起消失，不会出现两分支类型冲突的问题。这正是 pyasc 用「限制语句位置」替代「多分支返回类型合并」的设计取舍。

**练习 3**：`visited_return_types` 为什么以函数名为键、跨 visitor 共享？

**答案**：一次 JIT 编译会把 kernel 及其调用的所有 Device 子函数放进同一个 IR 模块（u4-l4 的内联机制）。以函数名为键查询「模块里是否已有该函数」，能在同一编译过程中第二次遇到同一子函数调用时（例如 `static_range` 展开导致重复调用）直接复用返回类型，既省去重复访问，也强制多次调用的签名一致；跨 visitor 共享则让 kernel visitor 拿到子函数 visitor 登记的类型。

## 5. 综合实践

**任务：把 Add 示例的 TILE_NUM 循环分别用 `range` 与 `asc.static_range` 各编译一版，对比 IR，量化两种循环的代价。**（本实践为规格指定的代码实践任务，需要已按 u1-l2 完成安装；全程可在 Model 仿真模式下完成。）

1. **准备两个版本**：
   ```bash
   mkdir -p ~/ctl_flow_lab && cd ~/ctl_flow_lab
   cp <仓库路径>/examples/01_add/add.py add_range.py
   cp add_range.py add_static.py
   ```
   `add_static.py` 只改一行：把 [examples/01_add/add.py:L49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L49) 的
   `for i in range(TILE_NUM * BUFFER_NUM):` 改为 `for i in asc.static_range(TILE_NUM * BUFFER_NUM):`。

2. **分别编译并 dump**：
   ```bash
   mkdir -p dump_range dump_static
   PYASC_DUMP_PATH=$PWD/dump_range  python3 add_range.py  -r Model always_compile=True
   PYASC_DUMP_PATH=$PWD/dump_static python3 add_static.py -r Model always_compile=True
   ```
   `always_compile=True` 绕过缓存（u3-l8），确保 dump 一定落盘（`codegen.mlir` 在 [python/asc/runtime/compiler.py:L162-L168](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L162-L168) 处写出，是 Pass 之前的 IR）。

3. **量化对比**：
   ```bash
   for d in dump_range dump_static; do
     echo "== $d =="
     wc -l $d/codegen.mlir
     grep -c "scf\.for" $d/codegen.mlir
     grep -c "remsi"    $d/codegen.mlir
     grep -c "asc\.Add" $d/codegen.mlir
   done
   ```

4. **预期观察**（待本地验证，具体数值以实测为准）：
   - `dump_range`：`scf.for` 1 处（`0 to 16 step 1`，边界为常量）；`asc.Add` 等循环体操作各 1 份；`remsi` 1 处（`i % BUFFER_NUM` 在设备上每轮计算）。
   - `dump_static`：`scf.for` 0 处；循环体操作各 16 份（16 = `TILE_NUM * BUFFER_NUM`）；`remsi` 0 处——`i` 是编译期 int，`i % 2` 折叠成常量 0/1，`buf_id * tile_length` 变成「常量 × tile_length」的乘法（`tile_length` 依赖运行时参数 `block_length`，无法折叠）。
   - IR 行数：设循环体 IR 规模为 \( S_{body} \)，则 range 版近似 \( S_{body} + O(1) \)，static 版近似 \( 16 \cdot S_{body} \)——线性放大。
   - 两版运行结果应一致（示例内 `torch.allclose` 断言都应通过）。

5. **分析并记录结论**，建议包含：
   - `range` 的优势：IR/指令体积小、边界可以是运行时值（本例恰好是常量，但换成 `ConstExpr` 形参或运行时参数也成立）；代价：每轮执行归纳变量的取余/乘法等索引运算，且有循环控制开销。
   - `static_range` 的优势：索引偏移全部或部分折叠成常量、无循环控制流、为后端进一步特化留出空间；代价：代码体积线性膨胀（本例 16 倍），边界必须编译期已知，过大或次数过多的展开可能挤占指令缓存。
   - 联系 u3-l2 的缓存陷阱：只改模块级常量（如 `TILE_NUM`）不改 kernel 源码不会使缓存失效，实验时请保持 `always_compile=True`。
   - 加分项：在 `add_static.py` 的循环体里加一个编译期 `if i == 0:`（`i` 是编译期 int，条件可折叠）对比「剪枝版 if」与运行期 `if buf_id == 0:`（生成 `scf.if`）的 IR 差异，把 4.4 的结论也串进来。

## 6. 本讲小结

- pyasc 的 `for` 只认 `range`/`asc.range`/`asc.static_range` 三种迭代器：`range` 一律生成 `scf.for`（边界物化为 int32，允许运行时值），`static_range` 在编译期由 `handle_static_range` 用普通 Python 循环把循环体逐圈展开，不生成循环 IR。
- `compute_inout` + `BlockInOut` 是控制流 IR 化的通用底座：在游离块里预 visit 语句，靠 `NameScope.redefined`（块内改写的外层名字）找出携带变量，产出「进块值 `init_handles` + 出块值 `yield_values/yield_handles`」，for 用 `make_args=True` 完成 SSA 的 φ 接线，if 用初值透传合并。
- `if` 有两种命运：编译期条件直接剪枝（不生成 IR、不开作用域、允许 return）；运行期条件生成带 else 的 `scf.if`，两分支改写的变量按名字排序取并集，单边改写的名字用对方分支的初值透传，类型一致性由 `compute_inout` 的 Python 类检查与 MLIR verifier 共同保证；`elif` 靠 AST 嵌套自然映射为嵌套 `scf.if`。
- return 只能在函数顶层：运行期块内被 `return_allowed` 拒绝、核函数不能返回对象、return 之后由 `discard_everything` 剪成死代码；返回类型经 `state.return_types`（整组赋值）修补函数签名，并由 `ReturnTypesDict` 按函数名跨调用缓存复用。
- 选择依据一句话：**循环次数编译期已知且体量小 → `static_range` 换取消除运行时索引计算；次数运行时可知或体量大 → `range` 保住 IR 体积**。

## 7. 下一步学习建议

- 下一讲 u4-l4《函数调用与 Device 子函数的内联》将讲 `visit_Call` 如何识别被 `@asc.jit` 修饰的调用目标并递归内联——本讲的 `ReturnTypesDict` 正是为它服务的，读完即闭环。
- 精读 `python/asc/codegen/function_visitor.py` 的 `visit_While`（L692-710），对照 `compute_inout` 在 `scf.while` 上 before/after 两块的用法，体会「一套底座、三种控制流」。
- 想看 `scf.for` 在后端被怎样进一步处理（如 UB 分配提升如何穿越循环），预习 u6-l2《张量物化与 UB 内存分配》，并可先浏览 `test/Dialect/AscendC/Transforms/hoist-ub-allocation.mlir` 里带 `scf.for` 的 lit 用例。
- 复习锚点：SSA/块进块出不理解时回看本讲 2.1；`PlainValue` 语义模糊回看 u2-l3；NameScope 查找规则回看 u4-l2。
