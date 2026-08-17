# IRValue 体系：Python 对象如何代表 IR 值

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 **IRHandle 与 IRValue 的双层设计**：底层是 pybind11 暴露的 `ir.Value` 句柄，上层是带 `dtype` 和运算符重载的 Python 包装对象，两者靠 `from_ir` / `to_ir` 协议互转。
2. 解释 **RuntimeInt / RuntimeNumeric 类型别名** 如何让同一个 API 既接受 Python 常量、又接受 IR 值，并理解 PlainValue 算术运算的「延迟求值」语义。
3. 说明 **GlobalAddress** 如何表示 Host 传入的设备指针参数，以及它的 `+` 运算为什么生成「指针偏移」而不是普通加法。
4. 手动推演 **materialize_ir_value** 对 PlainValue / IRValue / ConstExpr / int / float 各类输入的处理路径。
5. 拿一行 kernel 代码（如 `offset = asc.get_block_idx() * block_length`），画出每个子表达式的「类型标注图」。

## 2. 前置知识

本讲建立在前面几讲的概念之上，先用通俗语言补齐三个背景。

**（1）JIT 下的「求值」发生在编译期。** 回顾 u1-l5：`@asc.jit` 函数体并不按 Python 语义执行，而是由 `FunctionVisitor` 遍历 AST。visitor 的 `visit` 方法对每个表达式节点返回一个 Python 对象——如果这个对象是 IRValue（本讲主角），那么后续对它做 `+`、`*`、`>` 等运算时，Python 的运算符重载会在**编译期的此刻**被触发，向 IR 追加节点。也就是说，你写下的每个中缀运算符，最终都变成一条设备侧指令的「占位符」。

**（2）MLIR 的 Value 是什么。** ASC-IR 建立在 MLIR 之上。MLIR 里的一个 `Value` 是一个 SSA（静态单赋值）值：它有类型、由某个 Operation 产生、之后不再改变。C++ 侧的 `mlir::Value` 通过 pybind11 暴露给 Python，就是 `asc._C.ir.Value`。它是一个「哑」对象——能被传给 builder，但没有 `__add__`、不知道自己的 dtype 是 `asc.float32` 还是 `asc.int32`。

**（3）Python 运算符重载协议。** 对 `a + b`，Python 先尝试 `a.__add__(b)`；若返回 `NotImplemented`，再尝试 `b.__radd__(a)`。pyasc 前端正是利用这个协议「拦截」kernel 里的算术表达式，把它改写为 IR 构建。后面会看到 `FunctionVisitor.apply_binary_method` 还额外处理了「左边是普通 int、右边是 IR 值」的路由。

另外两个本讲直接依赖的概念：**DataType / KnownTypes**（u2-l1：类型系统的原子单元，`KnownTypes.int_` 就是 `int32`），**ConstExpr**（u2-l1：编译期常量的标记包装）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/asc/language/core/ir_value.py` | 本讲主战场：`IRHandle`、`IRValue`、`GlobalAddress`、`PlainValue`、`RuntimeInt` 系列别名、`materialize_ir_value`、`convert_value`、`cast_to_index` 全部在此 |
| `python/asc/language/core/utils.py` | `global_builder` 全局单例与 `require_jit` 保护，决定「什么时候允许创建 IR」 |
| `python/asc/language/core/ops.py` | `asc.number()` 与 `asc.inline()` 两个用户接口，是 materialize 机制的上层应用 |
| `python/asc/language/basic/sys_var.py` | `get_block_idx()`：RuntimeInt 的典型「生产者」 |
| `python/asc/codegen/function_visitor.py` | `visit_BinOp` / `get_binary_method_name` / `get_arg_value`：AST 运算如何路由到运算符重载、参数句柄如何变成 IRValue |
| `python/asc/runtime/jit.py` | `get_arg_type`：Host 侧实参（torch.Tensor 等）如何映射为参数类型 |
| `python/asc/language/core/tensor.py` | `set_global_buffer`：GlobalAddress 的典型「消费者」 |
| `python/asc/language/core/array.py` | `Array`：IRValue 协议的第三个实现，`cast_to_index` 的使用现场 |
| `examples/01_add/add.py` | 本讲实践的分析对象 |

## 4. 核心概念与源码讲解

### 4.1 IRHandle 与 IRValue：双层设计

#### 4.1.1 概念说明

问题：kernel 代码里写的是 Python 表达式，而 MLIR 需要的是 `ir.Value`。`ir.Value` 是 C++ 对象的 Python 投影，没有 dtype、没有运算符重载，无法直接参与 `a * b` 这样的表达式。

pyasc 的解法是**双层包装**：

- **下层 `IRHandle`**：就是 `ir.Value` 的类型别名，纯粹的代表「一个 IR 节点的句柄」，在各个 builder 调用之间传递。
- **上层 `IRValue`**：抽象基类，只约定一个双向协议——`from_ir(handle)` 把裸句柄包装成 Python 对象，`to_ir()` 把 Python 对象还原成裸句柄。`PlainValue`（标量）、`GlobalAddress`（设备指针）、`Array`（数组）、`BaseTensor`（u2-l2 的 Tensor 家族）都实现这个协议。

一句话：**IRHandle 负责「能被 builder 使用」，IRValue 负责「能被 Python 表达式使用」**。

#### 4.1.2 核心流程

一个 IR 值的典型生命周期：

```text
builder.create_xxxOp(...)          # ① 用 builder 创建 Operation，得到 IRHandle
        │
        ▼
PlainValue(handle, dtype)          # ② 构造（或 from_ir）成 Python 包装对象
        │
        ▼
参与 Python 表达式（a * b + 1）    # ③ 运算符重载触发，生成更多 IR 节点
        │
        ▼
value.to_ir()                      # ④ 还原为裸句柄，传给下一个 create_xxxOp
```

这个循环贯穿整个前端：所有 language 层 API 的函数体本质上都是「② → ③ → ④」的组合。

#### 4.1.3 源码精读

先看两个核心定义——`IRHandle` 只是一行别名，`IRValue` 是只有两个抽象方法的协议：

[python/asc/language/core/ir_value.py:L20-L32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L20-L32) —— 第 20 行把 `ir.Value` 起别名为 `IRHandle`；第 23-32 行定义 `IRValue` 抽象基类，`from_ir` 是类方法（从裸句柄重建包装对象），`to_ir` 是实例方法（交出裸句柄）。

`PlainValue` 的构造函数体现了「句柄 + 类型」的组合，且明确标注不应被用户直接调用：

[python/asc/language/core/ir_value.py:L61-L66](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L61-L66) —— `PlainValue` 持有 `handle` 与 `dtype`；若构造时不给 dtype，则调用 `DataType.from_ir(handle.get_type())` 从 IR 类型反查。

`from_ir` 的实现非常薄，恰好演示了协议的「重建」方向：

[python/asc/language/core/ir_value.py:L284-L286](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L284-L286) —— `PlainValue.from_ir` 从句柄的 IR 类型反推出 DataType，再构造自身。

[python/asc/language/core/array.py:L41-L49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/array.py#L41-L49) —— `Array.from_ir` 是更完整的示例：从 memref 类型同时反查元素 dtype 和长度。这说明 `from_ir` 的入参虽然只是个句柄，但类型信息都藏在 IR 类型里。

另一个关键问题：**什么时候允许创建 IRValue？** 答案由 `global_builder` 和 `require_jit` 把守：

[python/asc/language/core/utils.py:L136-L151](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L136-L151) —— `GlobalBuilder` 持有当前编译的 `ir.Builder` 与 `ir.ModuleOp`；`set_ir_builder` 在每次 codegen 开始时创建它们（何时被调用见 u1-l5 的 `_run_codegen` 主链路）。

[python/asc/language/core/utils.py:L196-L207](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L196-L207) —— `require_jit` 装饰器：若全局 builder 尚未初始化（即不在 JIT 编译过程中），直接抛 `RuntimeError`。`ir_value.py` 里几乎所有运算符方法都带这个装饰器。

#### 4.1.4 代码实践

**实践目标**：亲眼看一次「IRValue 的创建被 JIT 环境把守」的效果，理解这些类不能在普通 Python 代码里使用。

**操作步骤**：

1. 在仓库根目录新建临时脚本 `check_require_jit.py`（示例代码，分析完可删除）：

```python
# 示例代码：验证 require_jit 的保护作用
import asc

idx = asc.get_block_idx()   # 预期在这里抛出 RuntimeError
print(idx)
```

2. 执行 `python3 check_require_jit.py`。

**需要观察的现象**：脚本不会打印任何数字，而是在 `asc.get_block_idx()` 一行抛出异常。

**预期结果**：异常信息形如 `'get_block_idx' cannot be called without initialization of global builder`——因为此刻 `global_builder` 里还没有 `ir.Builder`（没有正在进行的 JIT 编译）。对比之下，同一行写在 `@asc.jit` 函数体里就能正常工作（4.2 节会看到它的实现）。具体报错文本以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`IRHandle` 和 `IRValue` 分别解决什么问题？

**答案**：`IRHandle`（即 `ir.Value`）是 MLIR C++ 对象的 Python 投影，解决「与 builder 交互」的问题；`IRValue` 是 Python 包装层，解决「参与 Python 表达式、携带 dtype、提供运算符重载」的问题。两者通过 `from_ir` / `to_ir` 互转。

**练习 2**：为什么 `from_ir` 是类方法（classmethod），而 `to_ir` 是实例方法？

**答案**：`from_ir` 的输入只有一个裸句柄，需要先知道「要包装成哪个类」才能构造实例，所以必须是类方法（`cls(handle)`）；`to_ir` 只依赖实例自身持有的 `handle`，自然是个实例方法。

**练习 3**：在本仓库中找出 `IRValue` 的至少三个实现类。

**答案**：`PlainValue`、`GlobalAddress`（均在 `ir_value.py`）、`Array`（`array.py`）；此外 `tensor.py` 中的 Tensor 家族（`BaseTensor` 及其子类）也实现了该协议（u2-l2 已讲）。

### 4.2 PlainValue 与 RuntimeInt：标量的延迟求值

#### 4.2.1 概念说明

`PlainValue` 表示「一个设备侧标量」。它的关键特性是：**编译期只知道类型和产生它的 IR 节点，具体的值要等 Kernel 在核上运行时才存在**。例如 `asc.get_block_idx()` 的值，第 0 号核运行时是 0、第 1 号核是 1——在 Host 端编译时它根本没有值，只有一个 `asc.GetBlockIdxOp` 占位。

为了让 API 签名同时容纳「IR 值」和「Python 立即数」，`ir_value.py` 末尾定义了一组类型别名：

```python
RuntimeBool:   TypeAlias = Union[PlainValue, bool]
RuntimeInt:    TypeAlias = Union[PlainValue, int]
RuntimeFloat:  TypeAlias = Union[PlainValue, float]
RuntimeNumeric: TypeAlias = Union[RuntimeInt, RuntimeFloat]
```

读作「运行期才知道的整数 / 浮点 / 数值」。任何标注为 `RuntimeInt` 的参数位，既可以传 `block_length`（PlainValue），也可以传 `8`（Python int），内部统一交给 `materialize_ir_value` 处理（4.4 节）。

#### 4.2.2 核心流程

以 Add 示例中的这一行为例（[examples/01_add/add.py:L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L31)）：

```python
offset = asc.get_block_idx() * block_length
```

求值过程：

1. `FunctionVisitor.visit_BinOp` 先递归 `visit` 左右子节点，得到两个 Python 对象；
2. 左边 `asc.get_block_idx()` 返回 `PlainValue(dtype=int32)`；
3. 右边 `block_length` 是 kernel 的运行时参数（标注 `int`），在函数入口已被包装成 `PlainValue(dtype=int32)`；
4. 查运算符映射表：`ast.Mult` → `__mul__`；
5. 调用 `PlainValue.__mul__` → `apply_binary_op(self, other, "MulI", "MulF")`：
   - `infer_common_type` 推导结果类型（两侧都是 int32 → int32）；
   - `materialize_ir_value` 把两侧统一成 PlainValue；
   - 按类型选择整数实现 `MulI`，调用 `builder.create_arith_MulIOp` 生成 IR；
6. 返回**新的** `PlainValue(int32)`，由 NameScope 绑定到名字 `offset`。

注意一个重要的对照：**纯 Python 常量之间的运算不会走这条路**。`add.py` 里的 `TILE_NUM * BUFFER_NUM`（两个模块级 int）在 visitor 求值时就是普通 Python 整数乘法，直接得到 16，不产生任何 IR。只有当至少一侧是 IRValue 时，运算才会「落」到 IR 上。这个分岔由 `apply_binary_method` 的路由逻辑决定（见下面源码精读）。

类型推导与溢出语义也值得注意：`infer_common_type` 规则是「优先取 PlainValue 一侧的 dtype」，因此 `2 * x`（int × PlainValue）的结果类型跟随 `x`；同时 PlainValue 的整数运算是固定位宽的（如 int32），语义上等价于模 \( 2^{32} \) 的回绕运算，而不是 Python int 的无限精度——这是「不能用普通 Python int 语义理解它」的核心原因之一。

#### 4.2.3 源码精读

先看「生产者」——`get_block_idx` 如何造出第一个 PlainValue：

[python/asc/language/basic/sys_var.py:L33-L36](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/sys_var.py#L33-L36) —— 调用 `create_asc_GetBlockIdxOp` 创建 ASC Dialect 操作，以 `KnownTypes.int_`（即 int32，见 [python/asc/language/core/dtype.py:L118-L123](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/dtype.py#L118-L123) 的别名表）作为结果类型，包成 PlainValue 返回。这就是 4.1.4 实践中被 `require_jit` 拦下的那个函数。

RuntimeInt 系列别名的定义：

[python/asc/language/core/ir_value.py:L338-L341](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L338-L341) —— 四个 Union 别名，是全前端 API 签名的通用「数值参数」标注。

运算符重载的实现模式（以加、乘为例，其余同构）：

[python/asc/language/core/ir_value.py:L74-L96](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L74-L96) —— 每个二元运算符都委托给 `apply_binary_op`，传入整数版和浮点版两个 builder 名（如 `"AddI"` / `"AddF"`）；`//` 和 `/` 都映射到 `DivSI`/`DivF`。

[python/asc/language/core/ir_value.py:L99-L101](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L99-L101) —— 边界示例：`**`（幂）直接抛 `NotImplementedError`，说明「能写哪些 Python 运算」是显式白名单，不是天然支持。

三个 classmethod 组成了运算的核心机制：

[python/asc/language/core/ir_value.py:L243-L253](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L243-L253) —— `infer_common_type`：只要有一侧是 PlainValue 就取它的 dtype（lhs 优先）；两侧都不是则报错（实际调用路径保证了至少一侧是 PlainValue）。

[python/asc/language/core/ir_value.py:L255-L264](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L255-L264) —— `apply_binary_op`：推导类型 → 两侧 materialize → 按整数/浮点选择 builder 属性名 → `getattr(builder, f"create_arith_{名字}Op")` 动态调用 → 包装新的 PlainValue 返回。这段是「Python 运算符 → arith 方言 Operation」的完整翻译。

比较运算走另一条路，产出 1 位整数（布尔）：

[python/asc/language/core/ir_value.py:L173-L195](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L173-L195) —— 六个比较运算符，整数用 `CmpIPredicate`、浮点用 `CmpFPredicate`（注意浮点区分有序/无序，如 `OEQ`/`ONE`）。

[python/asc/language/core/ir_value.py:L273-L282](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L273-L282) —— `apply_compare_op` 生成 `CmpIOp`/`CmpFOp`，结果 dtype 固定为 `KT.int1`。这解释了为什么 `if offset > 0:` 不能按 Python 的 truthiness 处理——条件是一个 IR 值，需要 FunctionVisitor 特殊处理成 IR 级分支（u4-l3 会展开）。

最后是 visitor 侧的路由：

[python/asc/codegen/function_visitor.py:L424-L428](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L424-L428) —— `visit_BinOp`：递归求值左右子树，查映射表得到魔法方法名，交给 `apply_binary_method`。

[python/asc/codegen/function_visitor.py:L94-L113](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L94-L113) —— `get_binary_method_name`：`ast.Add` → `__add__`、`ast.Mult` → `__mul__` 等 12 项映射表。

[python/asc/codegen/function_visitor.py:L164-L171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L164-L171) —— `apply_binary_method` 的三分支路由：左侧是普通 Python 值而右侧有 builder 支持（`has_builder_support` 检查 `BaseTensor`/`GlobalAddress`/`PlainValue`，见 [L156](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L156)）时直接调右值的反身方法（`8 * x` → `x.__rmul__(8)`）；否则先调左值方法，返回 `NotImplemented` 再回退右值反身方法；两侧都是普通 Python 值时就退化成纯 Python 求值（不产生 IR）。

#### 4.2.4 代码实践

**实践目标**：把 `offset = asc.get_block_idx() * block_length` 的 AST 结构和 visitor 路由亲手对上号。

**操作步骤**：

1. 在仓库根目录执行（示例代码，直接在命令行跑即可）：

```bash
python3 - <<'EOF'
import ast, inspect
src = "offset = asc.get_block_idx() * block_length"
print(ast.dump(ast.parse(src), indent=2))
EOF
```

2. 观察 dump 出的树：`Assign` → `BinOp(op=Mult)` → 左 `Call(func=Attribute(GetBlockIdx))`、右 `Name(block_length)`。
3. 对照 [function_visitor.py:L94-L113](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L94-L113) 确认 `Mult` 映射到 `__mul__`；对照 [function_visitor.py:L164-L171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L164-L171) 确认两侧都是 PlainValue 时走 `lhs.__mul__(rhs)` 分支。
4. 把 `block_length` 换成常量 `2048` 再 dump 一次——AST 结构不变（仍是 `BinOp(Mult)`），推演一下此时求值结果有何不同（提示：常量会被 materialize 成 IR 常量，运算仍生成 `arith.muli`）。

**需要观察的现象**：AST 只有结构信息，完全不含类型；类型是 visitor 求值时由返回的 Python 对象携带的。

**预期结果**：你能写出一张「AST 节点 → visitor 方法 → 产出的 Python 对象类型」的三列表（参考答案见本讲第 5 节综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：`RuntimeInt` 和 `int` 有什么区别？为什么 `set_flag` 的参数位适合用前者？

**答案**：`int` 是编译期就确定的 Python 整数；`RuntimeInt = Union[PlainValue, int]` 表示「运行期才可能有值的整数」，既容纳 IR 值也容纳常量。kernel 里的数值多数依赖 `get_block_idx()`、运行时参数等，编译期没有值，所以 API 统一用 RuntimeInt 标注。

**练习 2**：`a = a + 1` 在 kernel 里连续执行三次，IR 里是「一个变量被加三次」吗？

**答案**：不是。每次 `+` 都生成一个新的 `arith.addi` Operation 和新的 PlainValue（SSA 语义），NameScope 里名字 `a` 的指向被三次重新绑定。最终 IR 是四节点的值链，而不是对同一存储单元的三次自增。

**练习 3**：为什么 `x ** 2` 会报错，而 `x * x` 可以？

**答案**：[ir_value.py:L99-L101](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L99-L101) 中 `__pow__` 显式抛出 `NotImplementedError`——运算符支持是逐个手写的白名单，幂运算没有对应的 arith builder 映射，故不支持。

### 4.3 GlobalAddress：Host 设备指针的 IR 代表

#### 4.3.1 概念说明

Add 示例的 kernel 签名是：

```python
def vadd_kernel(x: asc.GlobalAddress, y: asc.GlobalAddress, z: asc.GlobalAddress, block_length: int):
```

Host 侧调用时传的是三个 `torch.Tensor`。tensor 的本质是「一段设备内存 + dtype」，kernel 内部需要的正是「带类型的设备地址」——这就是 `GlobalAddress`：一个指向 Global Memory 的指针值，携带元素 dtype。

它与 PlainValue 的关键差异在**运算语义**：两个 PlainValue 相加是算术，`GlobalAddress + RuntimeInt` 是**指针偏移**——生成的是 `emitasc.PtrOffsetOp`（贴近 C 的指针运算），而不是 `arith.addi`。这正对应 Ascend C 里 `x_gm + offset` 的写法。

#### 4.3.2 核心流程

从 Host 实参到 IR 的完整链路：

```text
Host 侧: vadd_kernel[8, stream](x, y, z, block_length)
   │  x 是 torch.Tensor
   ▼
JITFunction.get_arg_type(x)          → PointerArgType(float32)   ① 按实参类型分类
   ▼
codegen 生成 kernel 函数签名          → x 在 IR 里是 memref<...> 参数
   ▼
FunctionVisitor.get_arg_value(...)   → GlobalAddress(handle, float32)  ② 包装
   ▼
kernel 体内: x + offset              → arith.indexcast + emitasc.PtrOffset  ③ 偏移
   ▼
x_gm.set_global_buffer(x + offset, block_length)                     ④ 消费
```

#### 4.3.3 源码精读

第 ① 步，Host 实参分类：

[python/asc/runtime/jit.py:L74-L88](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L74-L88) —— `get_arg_type`：`np.ndarray` 和 `torch.Tensor`（去掉 `torch.` 前缀取 dtype 名）都映射为 `PointerArgType(dtype)`；Python `int`/`float` 映射为 `PlainArgType`。这决定了「张量进 kernel 后是指针、标量是值」。

第 ② 步，参数句柄包装成 IRValue：

[python/asc/codegen/function_visitor.py:L265-L274](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L265-L274) —— `get_arg_value`：`PointerArgType` → `GlobalAddress(handle, dtype)`；`PlainArgType` → `PlainValue(handle, dtype)`。kernel 体里名字 `x`、`block_length` 绑定的 Python 对象就是在这里诞生的。

GlobalAddress 自身的定义与指针加法：

[python/asc/language/core/ir_value.py:L35-L43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L35-L43) —— 构造函数同样标注「不应被用户直接调用」，`__repr__` 故意不打印句柄（句柄是 C++ 对象，没有可读的 repr）。

[python/asc/language/core/ir_value.py:L45-L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L45-L51) —— `__add__` 的三步：把偏移量 materialize 成 int32 → `IndexCastOp` 转成 index 类型 → `create_emitasc_PtrOffsetOp` 生成「指针 + 偏移」的新地址。与 `PlainValue.__add__`（生成 `arith.addi`）对比，可以清楚看到「同名的 `+`，不同的 IR」。

[python/asc/language/core/ir_value.py:L53-L58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L53-L58) —— `from_ir` 从句柄类型中取出元素类型反查 dtype：`ir.get_element_type(handle.get_type())`。这说明 kernel 参数在 IR 层是「元素类型的 memref」，dtype 信息一直藏在 IR 类型里。

第 ④ 步，消费者：

[python/asc/language/core/tensor.py:L149-L167](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L149-L167) —— `set_global_buffer(buffer: GlobalAddress, buffer_size: RuntimeInt)`：从 `buffer.dtype` 取得元素类型构造 GlobalTensor，再生成 `GlobalTensorSetGlobalBufferOp`，其中 `buffer.to_ir()` 交出裸句柄、`_mat(buffer_size)` 把长度物化成 PlainValue。Add 示例第 35-37 行的三个 `set_global_buffer` 调用全部经过这里（[examples/01_add/add.py:L35-L37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L35-L37)）。

#### 4.3.4 代码实践

**实践目标**：在真实导出的 IR 中看到 `GlobalAddress.__add__` 生成的指针偏移节点。

**操作步骤**：

1. 设置 dump 目录并运行 Add 示例（Model 仿真模式即可，无需 NPU）：

```bash
mkdir -p /tmp/pyasc_dump
PYASC_DUMP_PATH=/tmp/pyasc_dump python3 examples/01_add/add.py -r Model
```

2. 打开 `/tmp/pyasc_dump/codegen.mlir`（Pass 前的 IR），在 `vadd_kernel` 的函数体开头找三组形如 `indexcast` 与 `emitasc.ptr_offset` 的操作（对应 `x + offset`、`y + offset`、`z + offset`）。
3. 再打开同目录的 `ascendc.cpp`，找到 `SetGlobalBuffer` 调用，观察第一个实参正是指针偏移表达式。
4. 数一数 `ptr_offset` 的数量——应与 kernel 里 `指针 + offset` 出现的次数一致（本例 3 次）。

**需要观察的现象**：`emitasc.ptr_offset` 的第二个操作数来自一个 `arith.indexcast`，而 `indexcast` 的输入又是 `arith.muli`（4.2 节的 `offset`）——两级链路在 IR 里肉眼可见。

**预期结果**：IR 中能找到 3 处指针偏移；`ascendc.cpp` 中 `SetGlobalBuffer` 的实参形态与之一一对应。具体打印文本以本地 dump 为准（待本地验证；若未配置好仿真环境，可改为源码阅读型实践：沿 4.3.2 的链路图逐文件核对上述五个代码位置）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `GlobalAddress.__add__` 生成 `PtrOffsetOp` 而不是 `arith.addi`？

**答案**：GlobalAddress 不是数值而是地址，`地址 + 整数` 的语义是指针偏移（C 语义），生成通用算术加法在类型和含义上都是错的。EmitAsc 方言的 `PtrOffsetOp` 专门表达这一语义，后端发射时映射为 C 的指针运算。

**练习 2**：`GlobalAddress.from_ir` 如何在只有裸句柄的情况下恢复 dtype？

**答案**：`ir.get_element_type(handle.get_type())` 从 IR 类型（memref 的元素类型）反查出 MLIR 类型，再交给 `DataType.from_ir` 得到 pyasc 的 DataType——dtype 从未丢失，只是存在 IR 类型里。

**练习 3**：如果把 kernel 签名里的 `x: asc.GlobalAddress` 误写成普通参数（比如 Host 传 int），链路会在哪一步走岔？

**答案**：在 `JITFunction.get_arg_type`（jit.py L67 起）就会分类成 `PlainArgType` 而非 `PointerArgType`，`get_arg_value` 随之包装成 `PlainValue`；后续 `x + offset` 变成算术加法而非指针偏移，传给 `set_global_buffer` 时会因类型不符而报错。

### 4.4 materialize_ir_value：统一落地协议

#### 4.4.1 概念说明

前端 API 的数值参数位统一标注为 `RuntimeInt`/`RuntimeNumeric`，因此 API 实现里拿到的可能是：PlainValue、别的 IRValue、ConstExpr 包装、裸的 int/float——四种形态。而 builder 只认 `to_ir()` 出来的裸句柄。**`materialize_ir_value` 就是这个漏斗**：无论进来什么，出去都是一个（类型正确的）PlainValue。名字里的 materialize（物化）意为「把尚不是 IR 的值落成 IR」。

#### 4.4.2 核心流程

`materialize_ir_value(value, required_type)` 的分支决策：

| 输入形态 | 处理 | 产物 |
| --- | --- | --- |
| `PlainValue` | 已物化；给了 required_type 就再 `cast` 一次 | 原（或转型后的）PlainValue |
| 其他 `IRValue`（如 GlobalAddress） | 原样返回；此时不允许指定 required_type | 原对象 |
| `ConstExpr` | 解包出 `.value` 后递归物化 | PlainValue |
| `int` / `float` | 按 required_type 归一（`int()`/`float()`）后调 `convert_value` 生成 IR 常量 | PlainValue（常量） |
| 其他类型 | 抛 `TypeError` | —— |

其中「裸数值 → IR 常量」由 `convert_value` 完成：它维护一张「Python 类型 × DataType 名 → builder 工厂」的查找表（如 `int` × `"int32"` → `builder.get_i32`），调用工厂把立即数写成 IR 常量并包装成 PlainValue。

#### 4.4.3 源码精读

漏斗本体：

[python/asc/language/core/ir_value.py:L344-L363](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L344-L363) —— 依次判断 PlainValue / IRValue / ConstExpr / int/float；对裸数值先按 required_type 做 `bool()`/`int()`/`float()` 归一，再交 `convert_value`。

常量工厂：

[python/asc/language/core/ir_value.py:L366-L396](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L366-L396) —— `convert_value`：`type_to_builder` 表覆盖 bool（i1）、int（int1~uint64 共 9 档）、float（f16/f32/f64）；未指定 required_type 时 int 默认 int32、float 默认 float32——与 u2-l1 讲过的 KnownTypes 映射一致。不支持的组合会抛 `ValueError`。

index 类型的专用通道：

[python/asc/language/core/ir_value.py:L399-L407](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L399-L407) —— `cast_to_index`：裸 int 直接 `builder.get_index`；PlainValue 先 `to_ir` 再 `IndexCastOp`。MLIR 的 index 是专门给下标用的整数类型，所以它需要单独的物化入口。

[python/asc/language/core/array.py:L31-L39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/array.py#L31-L39) —— `cast_to_index` 的使用现场：`Array.__getitem__`/`__setitem__` 把 RuntimeInt 下标物化成 index 类型后传给 `memref.LoadOp`/`StoreOp`；`__setitem__` 同时用 `_mat`（materialize 的别名导入）把右侧值统一成数组 dtype。

两个上层用户接口，可以看到 materialize 直接暴露给用户面的形态：

[python/asc/language/core/ops.py:L32-L34](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ops.py#L32-L34) —— `asc.number(value, dtype)`：materialize 的直接封装，把 Python 数值（或 ConstExpr）落成指定 dtype 的 PlainValue。当你需要「把一个 Python 立即数显式变成某个 dtype 的 IR 值」时用它。

[python/asc/language/core/ops.py:L17-L29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ops.py#L17-L29) —— `asc.inline(code, args)`：把参数逐个 `_mat(...).to_ir()` 后塞进 `emitasc_VerbatimOp`，实现「直接内嵌一段 Ascend C 代码」的逃生舱（u4 讲错误处理时会再遇到它）。

#### 4.4.4 代码实践

**实践目标**：手动推演一行混合了 PlainValue 与 Python 常量的表达式如何逐级物化。

**操作步骤**：

1. 阅读并分析 Add 示例的这两行（[examples/01_add/add.py:L39-L42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L39-L42)）：

```python
tile_length = block_length // TILE_NUM // BUFFER_NUM
data_type = x.dtype
buffer_size = tile_length * BUFFER_NUM * data_type.sizeof()
```

2. 为每个子表达式写下一行记录：`(表达式, Python 对象类型, dtype, 生成的 IR 操作)`。
3. 对照 [ir_value.py:L344-L363](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L344-L363) 与 [L255-L264](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L255-L264) 验证你的每一步。
4. 可选验证：设 `PYASC_DUMP_PATH` 重新运行示例，在 `codegen.mlir` 里数一数 `arith.divsi`、`arith.muli`、`arith.constant` 的出现次数是否与推演一致。

**需要观察的现象**：`TILE_NUM`、`BUFFER_NUM`、`sizeof()` 的返回值都是编译期 Python int，它们不会「凭空」出现在 IR 里，而是每次参与运算时被 materialize 成 `arith.constant`。

**预期结果**（参考推演）：

| 表达式 | 类型 | dtype | IR |
| --- | --- | --- | --- |
| `block_length // TILE_NUM` | PlainValue | int32 | `arith.divsi`（右侧 8 物化为 constant） |
| `… // BUFFER_NUM` | PlainValue | int32 | 再一个 `arith.divsi` |
| `tile_length * BUFFER_NUM` | PlainValue | int32 | `arith.muli` |
| `… * sizeof()`（=4） | PlainValue | int32 | `arith.muli` |
| `buffer_size` | PlainValue | int32 | NameScope 绑定，无新 IR |

若做第 4 步，`divsi` 应出现 2 次、`muli` 至少 3 次（buffer_size 两次 + offset 一次）；具体以本地 dump 为准（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`materialize_ir_value(3)` 和 `materialize_ir_value(3, asc.float32)` 结果有何不同？

**答案**：前者未指定 required_type，int 默认按 `KT.int_`（int32）物化，生成 i32 常量；后者先被 `float(3)` 归一再按 float32 生成 f32 常量。

**练习 2**：为什么对「非 PlainValue 的 IRValue」指定 required_type 会报错？

**答案**：materialize 的类型转换能力来自 `PlainValue.cast`，GlobalAddress 等对象没有算术 cast 语义（指针不能随便转成数值），所以这条路径直接 `raise ValueError` 拒绝。

**练习 3**：`asc.number` 和直接写 Python 字面量有什么区别？什么时候必须用前者？

**答案**：字面量在参与运算时也会被自动 materialize，多数场景等价；但当需要**显式控制 dtype**（例如把 `3` 落成 float16 而不是默认 int32）或把 ConstExpr 值传给只收 PlainValue 的内部接口时，需要 `asc.number(value, dtype)`。

## 5. 综合实践

**任务**：对 Add 示例中的 `offset = asc.get_block_idx() * block_length`（[examples/01_add/add.py:L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L31)）及紧随其后的 `x_gm.set_global_buffer(x + offset, block_length)`（[L35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L35)），画出每个子表达式的类型标注图，并回答：为什么不能用普通 Python int 语义理解 `offset`？

**操作步骤**：

1. 复习 4.2.4 的 AST dump 方法，确认两行的 AST 结构。
2. 逐个子表达式填写下表（这就是「类型标注图」的表格形态）。
3. 为「为什么不是 Python int」至少写出四条理由。
4. 用 `PYASC_DUMP_PATH` 导出 `codegen.mlir`，逐行核对你推演的 IR 操作是否真实出现。

**参考答案（类型标注图）**：

```text
asc.get_block_idx()
    └─ PlainValue(dtype=int32)                  ← asc.GetBlockIdxOp
block_length
    └─ PlainValue(dtype=int32)                  ← kernel 运行时参数（PlainArgType）
get_block_idx() * block_length
    └─ PlainValue(dtype=int32)                  ← arith.muli（MulI 路径）
offset
    └─ PlainValue(dtype=int32)                  ← NameScope 名字绑定，无新 IR
x
    └─ GlobalAddress(dtype=float32)             ← torch.Tensor 实参 → PointerArgType
x + offset
    └─ GlobalAddress(dtype=float32)             ← arith.indexcast + emitasc.ptr_offset
block_length（作为 set_global_buffer 第二参）
    └─ PlainValue(dtype=int32)                  ← 直接复用，无需物化
```

**「为什么不能用普通 Python int 语义理解」的四条理由**：

1. **值在编译期不存在**：`get_block_idx()` 的值每个核各不相同（0、1、2…），Host 端编译时它只是 `asc.GetBlockIdxOp` 占位符；Python int 则在求值那一刻就有确定值。
2. **定长回绕而非无限精度**：PlainValue 整数运算是 int32 的 `arith.muli`，语义上模 \( 2^{32} \) 回绕；Python int 是任意精度，永不满出。
3. **不可用于 Python 控制流**：`if offset > 0:` 中的 `>` 产出 `int1` 的 IR 比较操作（[ir_value.py:L273-L282](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L273-L282)），不能按 Python truthiness 分支，必须由 FunctionVisitor 降级为 IR 级分支；Python int 条件则直接走解释器分支。
4. **每次运算是新 SSA 值而非原地修改**：`offset = offset + 1` 生成新的 `arith.addi` 与新 PlainValue，名字只是 NameScope 里的重新绑定；Python int 变量是可重复赋值的存储概念。

## 6. 本讲小结

- **双层设计**：`IRHandle`（`ir.Value` 别名）负责与 builder 交互，`IRValue` 抽象类通过 `from_ir`/`to_ir` 协议提供 Python 包装；PlainValue、GlobalAddress、Array、Tensor 都实现该协议。
- **PlainValue = 设备侧标量的延迟求值**：编译期只有类型与 IR 节点，值在核运行时才确定；二元/比较运算经 `apply_binary_op`/`apply_compare_op` 翻译为 arith 方言 Operation，比较结果固定为 `int1`。
- **RuntimeInt/RuntimeNumeric** 是 `Union[PlainValue, int/float]` 系别名，让 API 参数位同时容纳 IR 值与立即数；纯 Python 常量之间的运算不产生 IR。
- **GlobalAddress** 表示 Host 传入的设备指针（torch.Tensor → `PointerArgType` → `get_arg_value` 包装），其 `+` 生成 `indexcast + emitasc.ptr_offset` 而非算术加法，最终被 `set_global_buffer` 消费。
- **materialize_ir_value 是统一漏斗**：PlainValue 直通（可再 cast）、ConstExpr 解包递归、裸 int/float 经 `convert_value` 落成指定 dtype 的 IR 常量；index 类型走 `cast_to_index` 专用通道。
- **一切创建都被 `require_jit` 把守**：没有 `global_builder` 初始化就没有 IRValue，这是区分「kernel 代码」与「普通 Python 代码」的第一道闸门。

## 7. 下一步学习建议

- 下一讲 **u2-l4（枚举与硬件位置）** 将把视角从「值的表示」转向「值放在哪个硬件位置」：TPosition、HardEvent 与双缓冲流水，Add 示例的同步指令会得到完整解释。
- 若想提前看清「名字 → PlainValue/GlobalAddress」的绑定机制，可阅读 `python/asc/codegen/name_scope.py`，并留意 `FunctionVisitor` 中 `visit_Assign`/`visit_Name` 如何使用它（u4-l2 会系统讲解）。
- 对 IR 侧好奇的读者，可以用 `PYASC_DUMP_PATH` 导出的 `codegen.mlir` 对照本讲的每一条「生成的 IR 操作」，提前熟悉 MLIR 文本格式——这正好是 u5-l1（ASC Dialect 入门）的预习材料。
