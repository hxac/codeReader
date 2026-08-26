# Tensor 级算子：在整块张量上写计算

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `pl.*`、`pl.tensor.*`、`pl.tile.*` 三个命名空间的分工，并理解 `pl.*` 是「按参数类型分发」的调度器而非第三层抽象。
2. 追踪一条 `pl.add(a, b)` 调用从语言层包装到 IR 层 `tensor.add` / `tensor.adds` 算子调用的完整路径。
3. 按功能分类掌握 `tensor.*` 命名空间下的常用算子（创建、切片、逐元素、归约、广播、矩阵乘）。
4. 理解 Tensor 级编程「由编译器决定放置与调度」的含义——编译流水线中的 `ConvertTensorToTileOps` 等 Pass 会把 Tensor 级算子自动降级成 Tile 级的 load/compute/store 链路。
5. 能判断一个计算该写在 Tensor 级还是 Tile 级，并动手对比同一功能的两种写法。

## 2. 前置知识

本讲建立在前面几讲的基础上，先用两段话把关键前置概念补齐。

**Tensor 与 Tile 是两台「不同的机器」。** 回顾 u1-l4：`Tensor` 是全局内存（DDR）里的整块数组，shape 可以很大、由运行时分配；`Tile` 是片上统一缓冲里的固定尺寸数据块，是硬件视角的数据单元。同一个 `add`，作用在 Tensor 上是「对整块 DDR 数组做加法，编译器负责把它切小、搬到片上、调度执行」；作用在 Tile 上是「对一块你已经放好位置的片上缓冲做加法」。所以 PyPTO 把同一套算术在两个层级各实现了一遍。

**函数体有两个层级。** `@pl.jit` 函数的函数体顶层是「编排级」（orchestration）——在这里可以写 Tensor 级算子、创建张量、调用别的内核；`with pl.at(level=...)` 作用域内（或 `@pl.jit.incore` 函数体）是「设备内核级」——在这里才能做 Tile 级的 load/store。这个区分在 4.4 节会展开。

**注解即求值产物。** 回顾 u2-l2：`pl.Tensor[[64, 64], pl.FP32]` 这种写法经元类下标生成注解对象，`Tensor` 类有「注解模式」和「运行时模式」双重身份。本讲看到的 `Tensor` 值（算子的返回值）都是运行时模式——内部包着一个 IR 表达式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/pypto/language/op/unified_ops.py` | `pl.*` 统一分发层：按参数类型把调用转发给 tensor 层或 tile 层 |
| `python/pypto/language/op/tensor_ops.py` | `pl.tensor.*` 语言层包装：接受/返回 DSL `Tensor`，转发到 IR 构建层 |
| `python/pypto/ir/op/tensor_ops.py` | IR 构建层：真正构造 `tensor.*` 算子的 `Call` 表达式 |
| `python/pypto/language/__init__.py` | 把上述三层按命名空间组装成 `pl.tensor` / `pl.*` |
| `tests/ut/language/test_unified_ops.py` | 验证「`pl.X` 与显式 `pl.tensor.X` 生成结构相等 IR」的测试 |
| `docs/en/user/ops/00-dispatch.md` | 官方「如何选命名空间」指南 |
| `docs/en/api/tensor.md` | `pl.tensor` 的 API 文档入口（mkdocstrings 自动生成） |
| `examples/models/02_vector_dag.py` | 编排级 DAG 示例：Tensor 级 `create_tensor` + InCore 内核组合 |
| `examples/beginner/02_elementwise.py`、`examples/beginner/06_concat.py` | Tile 级三段式对照示例 |

## 4. 核心概念与源码讲解

### 4.1 三个命名空间与 `pl.*` 统一分发

#### 4.1.1 概念说明

用户面对三个命名空间：`pl.tensor.*`、`pl.tile.*` 和不带限定的 `pl.*`。关键认知是：**`pl.*` 不是第三层抽象，它是一个调度器（dispatcher）**——它检查实参类型，把调用转发给 tensor 实现或 tile 实现，自己不添加任何语义。

官方文档用一句话点破三者的区别：

> `pl.tensor.add` names an operation the compiler will place and schedule for you. `pl.tile.add` names an operation on a buffer you already placed.（`pl.tensor.add` 命名一个由编译器替你放置和调度的操作；`pl.tile.add` 命名一个作用在你已放置好的缓冲上的操作。）

选名规则很简单：

```text
写一个算子？
├─ 存在不限定的 pl.* 版本？ → 用它
└─ 不存在 → 该算子是层级专属的，写出层级名（pl.tensor.xxx / pl.tile.xxx）
```

#### 4.1.2 核心流程

`pl.add(a, b)` 的分发决策树：

```text
pl.add(lhs, rhs)
├─ lhs 是 Tensor，rhs 是 Tensor / int / float / Scalar / Expr
│    → pl.tensor.add(lhs, rhs)          # Tensor 级
├─ lhs 是 Tile，rhs 是 Tile
│    → pl.tile.add(lhs, rhs)            # Tile 级二元
├─ lhs 是 Tile，rhs 是标量
│    → pl.tile.adds(lhs, rhs)           # Tile 级标量形式（尾缀 s）
├─ lhs、rhs 都是标量
│    → 标量算术（生成 Scalar 表达式）
└─ 其他（如 Tensor 与 Tile 混用）→ TypeError
```

注意「混用禁止」：Tensor 和 Tile 不能出现在同一次二元运算里，否则报错。

#### 4.1.3 源码精读

先看分发层的模块自述——它明确声明了自己的角色（[python/pypto/language/op/unified_ops.py:L10-L16](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L10-L16)）：

> Provides type-dispatched wrappers that auto-select between tensor and tile operations based on the input type (Tensor vs Tile). Users can write `pl.add(a, b)` instead of explicitly choosing `pl.tensor.add` or `pl.tile.add`.

`add` 的完整实现就是决策树的直译（[python/pypto/language/op/unified_ops.py:L317-L327](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L317-L327)）：

```python
def add(lhs, rhs):
    """Element-wise addition, dispatched by input type."""
    if isinstance(lhs, Tensor) and isinstance(rhs, (Tensor, int, float, Scalar, _ir_core.Expr)):
        return _tensor.add(lhs, rhs)
    if isinstance(lhs, Tile) and isinstance(rhs, Tile):
        return _tile.add(lhs, rhs)
    if isinstance(lhs, Tile) and isinstance(rhs, (int, float, Scalar, _ir_core.Expr)):
        return _tile.adds(lhs, rhs)
    if _is_scalar_like(lhs) and _is_scalar_like(rhs):
        return Scalar(expr=_to_scalar_expr(lhs) + _to_scalar_expr(rhs))
    _raise_type_dispatch_error("add", lhs, rhs)
```

最后的兜底报错函数会把错误统一格式化成 `pl.add: ...`，并对「混用」给出专门提示（[python/pypto/language/op/unified_ops.py:L127-L144](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L127-L144)）。

那么 `pl.*` 到底暴露了哪些名字？答案在语言包的组装处（[python/pypto/language/__init__.py:L77-L81](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L77-L81)）：

```python
from .op import tensor_ops as tensor      # pl.tensor 命名空间
from .op import tile_ops as tile          # pl.tile 命名空间
```

同时，`pl.*` 顶层名按三种来源分组导入：

- **只有 Tensor 层才定义的名字**（或双子在 tile 层签名无法调和的名字）直接从 `tensor_ops` 导入——如 `create_tensor`、`full`、`dim`、`gather`、`scatter`（[python/pypto/language/__init__.py:L99-L119](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L99-L119)，L99-L103 的注释解释了分组原则）。
- **两层都有的名字**从 `unified_ops` 导入，让 Tensor/Tile 分发生效（[python/pypto/language/__init__.py:L158-L240](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L158-L240)）。
- **Tile 专属名字**从 `tile_ops` 导入，如 `load`、`store`、`relu`（[python/pypto/language/__init__.py:L120-L154](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L120-L154)）。

「哪些算子只有单层版本」官方文档给了速查表（[docs/en/user/ops/00-dispatch.md:L66-L79](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/ops/00-dispatch.md#L66-L79)）：

| 只有 Tensor 级 | 原因 |
| --- | --- |
| `pl.create_tensor`、`pl.full`、`pl.assemble` | 整块数组的创建与放置 |
| `pl.dim` | 张量的运行时维度 |

| 只有 Tile 级 | 原因 |
| --- | --- |
| `pl.tile.load` / `store` / `move` | 内存空间之间的搬运 |
| `pl.tile.transpose_view` | 已放置缓冲上的视图 |
| `pl.tile.get_block_idx` 等 | 当前执行 block 的身份 |

（注意：`pl.load` / `pl.store` 是 tile 层函数的顶层短名再导出，**不是**分发器——见 [docs/en/user/ops/00-dispatch.md:L81-L84](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/ops/00-dispatch.md#L81-L84)。）

#### 4.1.4 代码实践

**实践目标**：用仓库自带测试验证「`pl.add` 与 `pl.tensor.add` 生成完全相同的 IR」。

**操作步骤**：

1. 确认已完成 u1-l2 的开发模式安装（`pip install -e ".[dev]"`）。
2. 在仓库根目录运行（先 `source .claude/skills/testing/load-env.sh` 加载机器限额）：

   ```bash
   python -m pytest tests/ut/language/test_unified_ops.py -v
   ```

3. 打开测试文件，重点读 `test_add`（[tests/ut/language/test_unified_ops.py:L86-L97](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/language/test_unified_ops.py#L86-L97)）：它定义两个函数——一个用 `pl.add(a, b)`，一个用 `pl.tensor.add(a, b)`——然后断言两者 IR 结构相等。
4. 模仿它写一个自己的对照（示例代码，可直接粘进 Python 交互环境）：

   ```python
   import pypto.language as pl
   from pypto import ir
   from pypto.ir.printer import python_print

   @pl.function
   def unified(a: pl.Tensor[[64], pl.FP32], b: pl.Tensor[[64], pl.FP32]) -> pl.Tensor[[64], pl.FP32]:
       c: pl.Tensor[[64], pl.FP32] = pl.add(a, b)
       return c

   @pl.function
   def explicit(a: pl.Tensor[[64], pl.FP32], b: pl.Tensor[[64], pl.FP32]) -> pl.Tensor[[64], pl.FP32]:
       c: pl.Tensor[[64], pl.FP32] = pl.tensor.add(a, b)
       return c

   ir.assert_structural_equal(unified, explicit)
   print(python_print(unified))
   ```

**需要观察的现象**：`assert_structural_equal` 不抛异常；打印出的 IR 里出现的是限定的 `pl.tensor.add(...)` 调用——说明分发层只是转发，最终 IR 里记录的是 tensor 层算子。

**预期结果**：断言通过；IR 中可以看到 `pl.tensor.add`（参数是 Tensor 时）。若把 `b` 换成标量（对照 `test_add_scalar`，[tests/ut/language/test_unified_ops.py:L205-L216](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/language/test_unified_ops.py#L205-L216)），IR 会变成 `pl.tensor.adds`——标量形式的自动选择发生在更下层（见 4.2）。

#### 4.1.5 小练习与答案

**练习 1**：`pl.matmul(a, b)` 和 `pl.tensor.matmul(a, b)`（`a`、`b` 都是 `Tensor`）生成的 IR 一样吗？依据是什么？

答案：一样。`tests/ut/language/test_unified_ops.py` 的 `test_matmul`（[L226-L241](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/language/test_unified_ops.py#L226-L241)）对两者做了 `assert_structural_equal` 断言。

**练习 2**：写 `pl.add(tensor_a, tile_b)` 会发生什么？为什么这样设计？

答案：抛出 `TypeError`。`unified_ops.add` 的所有分支都不匹配，落到 `_raise_type_dispatch_error`，它会检测到实参同时含 Tensor 和 Tile 并报「cannot mix Tensor and Tile arguments」。设计原因是两级数据的存储位置和生命周期完全不同（DDR vs 片上缓冲），混用的语义无法定义。

**练习 3**：`pl.load` 是分发器吗？

答案：不是。`load` / `store` / `move` 等是 tile 层函数的顶层短名再导出（`python/pypto/language/__init__.py:L120-L154` 从 `tile_ops` 直接导入），官方文档明确说明它们「不是分发器，只是 `pl.tile.load` 的短名」。

### 4.2 语言层包装与 IR 构建：一条 `pl.add` 的三层旅程

#### 4.2.1 概念说明

`pl.tensor.add` 背后其实还有两层：

1. **语言层包装**（`python/pypto/language/op/tensor_ops.py`）：类型安全的薄壳——接受和返回 DSL 的 `Tensor` / `Scalar` 对象，把参数「解包」成裸 IR 表达式后转发。
2. **IR 构建层**（`python/pypto/ir/op/tensor_ops.py`）：真正构造 IR——调用 `create_op_call("tensor.add", ...)` 生成一个 `Call` 表达式，并在这里完成 `tensor.add`（张量×张量）与 `tensor.adds`（张量×标量）的自动选择。

语言层模块的 docstring 自我定位很准确（[python/pypto/language/op/tensor_ops.py:L10-L14](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L10-L14)）：

> This module provides type-safe wrappers around pypto.ir.op.tensor operations that accept and return Tensor types instead of raw Expr/Call objects.

#### 4.2.2 核心流程

以 `pl.tensor.add(t, 5)` 为例（`t` 是 DSL `Tensor`）：

```text
pl.tensor.add(t, 5)                      # 语言层入口
  │ t.unwrap() → 裸 Expr；5 原样传递
  ▼
pypto.ir.op.tensor_ops.add(lhs_expr, 5)  # IR 构建层
  │ _normalize_scalar_operand(...) 把 5 变成带 ScalarType 的常量表达式
  │ 看 rhs 类型：ScalarType → "tensor.adds"；否则 → "tensor.add"
  ▼
_ir_core.create_op_call("tensor.adds", [lhs, rhs], ...)   # 生成 Call 表达式
  ▼
Tensor(expr=call_expr)                   # 语言层把结果重新包成 DSL Tensor
```

#### 4.2.3 源码精读

语言层的 `add` 只做三件事：解包左操作数、解包右操作数、转发（[python/pypto/language/op/tensor_ops.py:L755-L770](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L755-L770)）：

```python
def add(lhs: Tensor, rhs: int | float | Tensor | Scalar | Expr) -> Tensor:
    lhs_expr = lhs.unwrap()
    call_expr = _ir_ops.add(lhs_expr, _unwrap_rhs(rhs))
    return Tensor(expr=call_expr)
```

右操作数的解包规则由 `_unwrap_rhs` 统一处理：DSL 的 `Tensor` / `Scalar` 调 `unwrap()` 取出内部表达式，Python 数字原样放行（[python/pypto/language/op/tensor_ops.py:L145-L156](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L145-L156)）。

真正的「add 还是 adds」分岔在 IR 构建层（[python/pypto/ir/op/tensor_ops.py:L537-L558](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/ir/op/tensor_ops.py#L537-L558)）：

```python
def add(lhs: Expr, rhs: int | float | Expr, span: Span | None = None) -> Call:
    actual_span = _get_span_or_capture(span)
    rhs_expr = _normalize_scalar_operand(lhs, rhs, actual_span, fallback_int_dtype=DataType.FP32)

    rhs_type = rhs_expr.type
    if isinstance(rhs_type, ScalarType):
        return _ir_core.create_op_call("tensor.adds", [lhs, rhs_expr], {}, actual_span)
    else:
        return _ir_core.create_op_call("tensor.add", [lhs, rhs_expr], {}, actual_span)
```

`mul` 与之完全同构，分岔为 `tensor.muls` / `tensor.mul`（[python/pypto/ir/op/tensor_ops.py:L497-L518](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/ir/op/tensor_ops.py#L497-L518)）。这就是文档说的「你几乎不用手写 `adds` / `muls`——给统一形式传一个 Python 数字就会自动选中」的机制落点。

再看一个「只有 Tensor 级才有」的算子——`create`（[python/pypto/ir/op/tensor_ops.py:L42-L112](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/ir/op/tensor_ops.py#L42-L112)）。它把 shape 列表打包成 `MakeTuple`、校验 `init_value` 的可表示范围，最终生成 `tensor.create` 算子调用（L112）：

```python
return _ir_core.create_op_call("tensor.create", args, kwargs, actual_span)
```

语言层对应的 `create` 则补全了面向用户的文档与默认参数，并把返回的 `Call` 包成 `Tensor`（[python/pypto/language/op/tensor_ops.py:L164-L215](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L164-L215)）；L218 的 `create_tensor = create` 说明两个名字是同一个函数。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「张量×张量 → `tensor.add`；张量×标量 → `tensor.adds`」的自动选择。

**操作步骤**：

1. 运行以下脚本（示例代码）：

   ```python
   import pypto.language as pl
   from pypto.ir.printer import python_print

   @pl.function
   def tt(a: pl.Tensor[[64], pl.FP32], b: pl.Tensor[[64], pl.FP32]) -> pl.Tensor[[64], pl.FP32]:
       return pl.tensor.add(a, b)          # 张量 × 张量

   @pl.function
   def ts(a: pl.Tensor[[64], pl.FP32]) -> pl.Tensor[[64], pl.FP32]:
       return pl.tensor.add(a, 5)          # 张量 × 标量

   print(python_print(tt))
   print(python_print(ts))
   ```

2. 对照测试 `_assert_explicit_tensor_scalar_sugar`（[tests/ut/language/test_unified_ops.py:L35-L84](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/language/test_unified_ops.py#L35-L84)）——它断言 `pl.tensor.add(a, 5)` 与显式的 `pl.tensor.adds(a, 5)` 结构相等。

**需要观察的现象**：两个函数打印出的算子名不同——前者 `pl.tensor.add`，后者 `pl.tensor.adds`。

**预期结果**：与测试断言一致：`add(a, 5)` 就是 `adds(a, 5)`。打印 API 的定义见 [python/pypto/ir/printer.py:L15-L21](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/ir/printer.py#L15-L21)。

#### 4.2.5 小练习与答案

**练习 1**：语言层 `tensor_ops.add` 自己做「add/ads 分岔」吗？

答案：不做。它只解包转发；分岔发生在 IR 构建层 `pypto/ir/op/tensor_ops.py` 的 `add` 里，依据是右操作数归一化后的类型是否为 `ScalarType`。

**练习 2**：为什么语言层包装要「接受/返回 `Tensor`，内部却转发裸 `Expr`」？

答案：DSL 层的类型安全与易用性（用户拿到的始终是可继续链式调用的 `Tensor` 对象）需要一层包装；而 IR 构建层面向的是算子调用本身（`Call` 表达式 + 操作数 `Expr`），两层职责分离后，测试和 Pass 可以直接在 IR 层构造算子而不经过 DSL。

**练习 3**：`pl.tensor.create` 与 `pl.create_tensor` 是两个函数吗？

答案：不是。语言层 L218 `create_tensor = create` 是同一函数的两个名字；`pl.create_tensor` 是它在顶层的再导出（`python/pypto/language/__init__.py:L104-L119`）。

### 4.3 常用 Tensor 级算子分类速查

#### 4.3.1 概念说明

`tensor.*` 命名空间的算子约有一百个（语言层 `__all__` 列出了全部名字，见 [python/pypto/language/op/tensor_ops.py:L20-L124](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L20-L124)）。死记没有意义，按「这个算子在数据流里扮演什么角色」分类即可。完整 API 由 [docs/en/api/tensor.md](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/api/tensor.md) 自动生成文档（该文件只有 11 行，是 mkdocstrings 的挂载点，正文来自源码 docstring——这也是「读 docstring 就是读文档」的项目惯例）。

#### 4.3.2 分类总表

| 类别 | 代表算子 | 一句话语义 |
| --- | --- | --- |
| 创建与初始化 | `create` / `full` / `ci`(=arange) / `random` / `create_l1` | 分配一块新张量；`init_value=0` 可让运行时预清零，比 `full` 便宜（`full` 要靠一个内核物化常量张量） |
| 视图与形状 | `slice` / `view` / `reshape` / `reinterpret_view` / `transpose` / `concat` / `fillpad` / `set_validshape` | 零拷贝地换一个「看法」或在边界补值；`concat` 只沿列拼接 |
| 逐元素算术 | `add/adds`、`sub/subs`、`mul/muls`、`div/divs`、`fmod`、`neg`、`abs`、`exp`、`log`、`sin`、`cos`、`sqrt`、`rsqrt`、`recip`、`cast`、`maximum`、`minimum`、`cmp` | 形状不变逐元素计算；尾缀 `s` 是标量右操作数形式 |
| 位运算与移位 | `and_`、`or_`、`xor`、`not_`、`shl`、`shr` 及其 `s` 形式 | 整数 dtype 专用 |
| 归约 | `row_sum/max/min/prod/argmax/argmin`、`col_*` 同族 | 沿最后一维（row）或倒数第二维（col）归约，**保留维度** |
| 广播 | `expands`、`expand_clone`、`row_expand_*`、`col_expand_*` | 把 `[M,1]` 行向量或 `[1,N]` 列向量按行/列扩展成 `[M,N]` 参与运算 |
| 矩阵乘 | `matmul`、`matmul_acc` | 带转置标志与累加器；秩 > 2 时自动走 batch 路径 |
| 读写与散聚 | `read` / `write`（标量级）、`assemble`（子区域写入）、`gather` / `scatter` / `scatter_update` / `sort32` / `mrgsort` / `paged_gather` / `gather_row` | 编排级读配置、写局部、按索引散聚 |
| 元信息与设备 | `dim`、`get_block_idx`、`get_block_num`、`get_subblock_idx` | 运行时维度与 SPMD block 身份 |
| 解析期标记 | `no_dep`、`dump_tag` | 运行时恒等透传，由解析器在解析期消费（回顾 u1-l5 的 `pl.Out` 同类机制） |

#### 4.3.3 源码精读

**`slice`——最常用的视图算子。** 签名（[python/pypto/language/op/tensor_ops.py:L415-L423](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L415-L423)）：

```python
def slice(
    tensor: _TensorT,
    shape: Sequence[IntLike],
    offset: Sequence[IntLike],
    valid_shape: Sequence[IntLike] | None = None,
    drop_dims: Sequence[int | Expr] | None = None,
    pad_value: PadValue | int | float | None = None,
    clamp: bool = False,
) -> _TensorT:
```

三个核心参数：`shape` 是窗口尺寸、`offset` 是窗口在源张量坐标系中的起点、`valid_shape` 收窄「窗口里真正有效的区域」（边界不对齐时的关键参数——u2-l2 讲过的动态维度经常配合它）。函数体在 L481-L492 完成解包并转发，最后用 `tensor.__class__(expr=call_expr)` 重包装——这个写法配合 L134-L138 的 `TypeVar`，让 `DistributedTensor` 子类切片后仍返回子类（[python/pypto/language/op/tensor_ops.py:L481-L492](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L481-L492)）。

**`matmul`——带转置标志的矩阵乘。**（[python/pypto/language/op/tensor_ops.py:L644-L674](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L644-L674)）`a_trans` / `b_trans` 是 **Tensor 级专属**参数：张量不携带布局信息，所以转置只能用标志表达；到了 Tile 级，转置是类型属性（`pl.tile.transpose_view`），统一分发层对 Tile 操作数传 `a_trans=True` 会直接 `TypeError` 而不是悄悄丢弃（见 [python/pypto/language/op/unified_ops.py:L1024-L1036](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L1024-L1036) 的守卫逻辑）。它的 docstring 还揭示了隐式降级的去处：秩 > 2 的张量矩阵乘由 `ConvertTensorToTileOps` 降级为 `tile.batch_matmul`，再由 `FlattenTileNdTo2D` 展开成逐批次 `tile.matmul`（[python/pypto/language/op/unified_ops.py:L1019-L1022](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L1019-L1022)）。

**`row_sum`——保留维度的行归约。**（[python/pypto/language/op/tensor_ops.py:L1202-L1213](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L1202-L1213)）「reduces along last axis, keeps dim」——`[64, 128]` 进、`[64, 1]` 出。测试 `test_row_max` 用显式注解确认了这一形状约定（[tests/ut/language/test_unified_ops.py:L243-L249](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/tests/ut/language/test_unified_ops.py#L243-L249)）。对应地，`col_*` 族沿倒数第二维归约，`[M, N]` 进、`[1, N]` 出。Tensor 级调用 `row_sum` 不需要传暂存 Tile——统一分发层会在 Tile 路径强制要求 `tmp_tile`、在 Tensor 路径拒绝它（[python/pypto/language/op/unified_ops.py:L1139-L1153](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L1139-L1153)），因为 Tensor 路径的暂存由降级 Pass 自动分配。

**`cast`——带舍入模式的类型转换。**（[python/pypto/language/op/tensor_ops.py:L1826-L1844](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L1826-L1844)）`mode` 支持 `"none"` / `"rint"` / `"round"`（默认）/ `"floor"` / `"ceil"` / `"trunc"` / `"odd"` 七种——高精度算子链里控制中间精度的常用旋钮（`log` / `recip` / `rsqrt` / `div` 还有 `high_precision=` 开关，选择 PTOAS 高精度路径）。

#### 4.3.4 代码实践

**实践目标**：用归约 + 广播两族算子，在编排级拼出 softmax 的核心三步（行最大值、行移位指数、行求和），体会「Tensor 级算子像搭积木」。

**操作步骤**（示例代码，逐段粘贴运行）：

```python
import torch
import pypto.language as pl
from pypto.runtime import RunConfig

@pl.jit
def softmax_parts(x: pl.Tensor, rowmax_out: pl.Out[pl.Tensor],
                  expdiff_out: pl.Out[pl.Tensor], rowsum_out: pl.Out[pl.Tensor]):
    mi = pl.row_max(x)                    # [M, N] → [M, 1]
    e = pl.row_expand_expdif(x, mi)       # exp(x[i,:] - mi[i,0]) → [M, N]
    s = pl.row_sum(e)                     # [M, N] → [M, 1]
    pl.assemble(rowmax_out, mi, [0, 0])
    pl.assemble(expdiff_out, e, [0, 0])
    pl.assemble(rowsum_out, s, [0, 0])
    return rowmax_out

torch.manual_seed(0)
x = torch.randn(64, 128, dtype=torch.float32)
rm = torch.zeros(64, 1, dtype=torch.float32)
ed = torch.zeros(64, 128, dtype=torch.float32)
rs = torch.zeros(64, 1, dtype=torch.float32)
softmax_parts(x, rm, ed, rs, config=RunConfig())

mi_ref = x.max(dim=-1, keepdim=True).values
assert torch.allclose(rm, mi_ref, rtol=1e-5, atol=1e-5)
assert torch.allclose(ed, torch.exp(x - mi_ref), rtol=1e-4, atol=1e-4)
assert torch.allclose(rs, torch.exp(x - mi_ref).sum(dim=-1, keepdim=True), rtol=1e-4, atol=1e-4)
print("OK")
```

**需要观察的现象**：三行算子调用就完成了「归约 → 广播 → 归约」的组合；不需要任何 `pl.load` / `pl.store`，也不需要分块循环。

**预期结果**：断言通过（默认平台为 `a2a3sim` 模拟器，见 u1-l4）。`row_expand_expdif` 的语义（`exp(tensor[i,:] - row_vec[i,0])`）以 [python/pypto/language/op/tensor_ops.py:L1500-L1515](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L1500-L1515) 的 docstring 为准。若在本地设备/模拟器上运行报错，请记录报错信息——此组合未经本讲环境实测，**待本地验证**；退路是只用 `@pl.function` 定义并用 `python_print` 检查生成的 `tensor.row_max` / `tensor.row_expand_expdif` / `tensor.row_sum` 调用链。

#### 4.3.5 小练习与答案

**练习 1**：`pl.tensor.concat` 能沿行拼接两个 `[32,16]` 张量得到 `[64,16]` 吗？

答案：不能。`concat` 只沿列维度拼接（docstring：'Concatenate two tensors along the column dimension'，[python/pypto/language/op/tensor_ops.py:L1889-L1902](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L1889-L1902)）。`[32,16]` 拼 `[32,16]` 得 `[32,32]`——这正是 `examples/beginner/06_concat.py` 演示的计算。

**练习 2**：`pl.full([M,N], pl.FP32, 0)` 和 `pl.create_tensor([M,N], pl.FP32, init_value=0)` 都能得到全零张量，选哪个？

答案：优先 `create_tensor(init_value=0)`。`create` 的 docstring 写明：`init_value` 由运行时在 AICPU 上预填充、对任何 dtype 都合法；而 `full`「物化一个常量张量」需要走一个内核，更贵（[python/pypto/language/op/tensor_ops.py:L176-L183](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tensor_ops.py#L176-L183)）。

**练习 3**：`pl.row_sum` 为什么不像 `pl.tile.row_sum` 那样要求传暂存缓冲？

答案：Tile 级的缓冲生命周期由用户管理，所以暂存必须显式传入；Tensor 级的暂存在 `ConvertTensorToTileOps` 降级时由编译器自动分配。统一分发层用 `_reject_tmp_for_tensor` / `_require_tmp_for_tile` 两个守卫把这个不对称性变成显式报错（[python/pypto/language/op/unified_ops.py:L189-L213](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L189-L213)）。

### 4.4 Tensor 级与 Tile 级的分工：谁决定数据放在哪、怎么切

#### 4.4.1 概念说明

「Tensor 级编程隐式完成切分与调度」的含义：你在编排级写 `pl.add(a, b)`，只声明了**要算什么**；**怎么算**——数据怎么切块、每块放到哪个核、中间量驻留哪级存储、任务之间怎么排依赖——全部交给编译流水线。具体落点是第 10 号 Pass `ConvertTensorToTileOps`：它把 `tensor.*` 算子降级成 `tile.load` → 片上计算 → `tile.store` 的链路（官方指南在文末直接指向该 Pass 文档，[docs/en/user/ops/00-dispatch.md:L115](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/ops/00-dispatch.md#L115)）。Tile 级则相反：你亲手 load、亲手 store、亲手分块循环（回顾 u1-l5 的三段式与口诀）。

两级的合法位置也不同（[docs/en/user/ops/00-dispatch.md:L100-L108](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/ops/00-dispatch.md#L100-L108)）：Tile 算子被拒于 `@pl.jit` 函数体顶层——要移进 `pl.at(...)` 作用域；Tensor 创建类算子被拒于 InCore 函数内——分配是「控制面」的工作，要在编排级做完再通过参数传进内核。

#### 4.4.2 核心流程

| 维度 | Tensor 级（`pl.tensor.*` / `pl.*` 传 Tensor） | Tile 级（`pl.tile.*` / `pl.at` 作用域内） |
| --- | --- | --- |
| 数据单位 | 整块 DDR 数组 | 片上固定尺寸 Tile |
| 放置与调度 | 编译器（Pass 自动降级、自动切分） | 用户（手写 load/store/分块循环） |
| 典型位置 | `@pl.jit` 编排级函数体、Orchestration 函数 | `pl.at(...)` 作用域、`@pl.jit.incore` 函数体 |
| 暂存缓冲 | 编译器分配 | 用户传入 |
| 表达力 | 一行一个整块操作，接近 NumPy | 完全控制数据搬运与驻留 |
| 适用人群 | 算法开发者快速表达 | 性能专家精细优化 |

生产代码里两级是**协作**关系：编排级用 Tensor 级算子组织数据流与中间缓冲，计算热点下沉到 InCore 内核做 Tile 级优化。

#### 4.4.3 源码精读

**例一：`examples/models/02_vector_dag.py`——两级协作的标准范本。** 编排入口 `vector_dag` 计算 `f = (a + b + 1)(a + b + 2) + (a + b)`：它在编排级用 `pl.create_tensor` 分配中间量（`create` 只能在这里用），然后逐个调用 InCore 内核（[examples/models/02_vector_dag.py:L76-L96](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/models/02_vector_dag.py#L76-L96)）：

```python
@pl.jit
def vector_dag(a: pl.Tensor, b: pl.Tensor, f: pl.Out[pl.Tensor]):
    c = pl.create_tensor([128, 128], dtype=pl.FP32)   # Tensor 级：分配中间缓冲
    c = kernel_add_128(a, b, c)                       # InCore 内核做 Tile 级工作
    d = pl.create_tensor([128, 128], dtype=pl.FP32)
    d = kernel_add_scalar_128(c, 1.0, d)
    ...
```

被调用的 `kernel_add_128` 则是纯粹的 Tile 级三段式（[examples/models/02_vector_dag.py:L43-L50](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/models/02_vector_dag.py#L43-L50)）：

```python
@pl.jit.incore
def kernel_add_128(a: pl.Tensor, b: pl.Tensor, output: pl.Out[pl.Tensor]):
    a_tile = pl.load(a, [0, 0], [128, 128])    # Tile 级：亲手搬运
    b_tile = pl.load(b, [0, 0], [128, 128])
    result = pl.add(a_tile, b_tile)            # 同名算子，Tile 分支
    pl.store(result, [0, 0], output)
    return output
```

注意同一个 `pl.add`：编排级写就是 `tensor.add`，InCore 里作用于 Tile 就是 `tile.add`——分发层让两级代码共享同一拼写。该示例有代码生成测试守护（`tests/st/codegen/dsl/test_add_mul_orch_codegen.py` 断言其降级后是 1 个 Orchestration 函数 + 3 个 AIV 内核）。

**例二：`examples/models/04_paged_attention.py`——Tensor 级算子在真实模型里的用法。** 编排函数 `paged_attention` 用三件套组织数据流（[examples/models/04_paged_attention.py:L289-L331](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/models/04_paged_attention.py#L289-L331)）：

```python
batch_cfg = pl.tensor.read(config, [0])                       # 标量读：取运行时配置
...
oi_buf = pl.create_tensor([q_tile, head_dim_cfg], dtype=pl.FP32)   # 循环内分配累加缓冲
...
qi = pl.slice(query, [q_tile, head_dim_cfg], [cur_offset, 0])      # 零拷贝视图
kj = pl.slice(key_cache, [block_size_cfg, head_dim_cfg], [kv_block_row, 0])
```

读配置、按块表换算物理行号、切出本批次的查询/键/值视图——这些「调度逻辑」用 Tensor 级算子表达；随后的 QK 矩阵乘、softmax、PV 矩阵乘都交给 InCore 内核在 Tile 级完成。这是「Tensor 级写数据流、Tile 级写算子」的典范。

**例三：切分要不要手写？** 对比同一个 512×128 加法的两种写法。Tensor 级一行 `pl.add(a, b)` 完事；Tile 级必须手写分块循环（[examples/beginner/02_elementwise.py:L81-L95](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/02_elementwise.py#L81-L95)）——Tile 是固定尺寸窗口，512 行装不下一个 128 行的 Tile，就得循环四次、每次移动行偏移。这份「手写切分」的工作，在 Tensor 级正是由降级 Pass 代劳的。

#### 4.4.4 代码实践

**实践目标**：量化「Tensor 级一行 vs Tile 级分块循环」的代码量差，并确认两者数值一致。

**操作步骤**：

1. 阅读并运行 `python examples/beginner/02_elementwise.py`（Tile 级，含 `chunked_add`）。
2. 写一个 Tensor 级版本（示例代码）：

   ```python
   import torch
   import pypto.language as pl
   from pypto.runtime import RunConfig

   @pl.jit
   def tensor_add_512x128(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
       return pl.assemble(c, pl.add(a, b), [0, 0])
   ```

3. 分别统计两个版本的算子调用行数与循环行数；用 `torch.allclose(a_big + b_big, c)` 验证。

**需要观察的现象**：Tile 版需要 `pl.range` 循环 + 偏移计算（约 4 行核心逻辑），Tensor 版核心逻辑 1 行；两者输出一致。

**预期结果**：数值一致。`@pl.jit` + `pl.Out` + `assemble` 写回的组合**待本地验证**；若不被接受，改用 `02_vector_dag.py` 的模式（`create_tensor` 分配输出 + 调用 InCore 内核写回），该模式有示例与测试双重守护。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `pl.create_tensor` 在 InCore 函数里会被拒绝？

答案：张量分配是控制面（编排级）的工作——分配发生在 DDR，由运行时管理；InCore 内核只应操作传进来的片上数据。官方排错表把它列为「Tensor creation is control-plane work」，建议在控制面分配后经 `pl.Out[...]` 参数传入（[docs/en/user/ops/00-dispatch.md:L105-L107](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/ops/00-dispatch.md#L105-L107)）。

**练习 2**：判断以下计算各适合写在哪一级，为什么：(a) `c = a * b + bias`（三个 4096×4096 张量）；(b) 一个需要 L0Cube 累加器驻留的 FP16 matmul 热点内核。

答案：(a) 先写 Tensor 级——纯逐元素、无特殊驻留需求，让编译器切分调度；确认正确后再按性能剖析决定是否下沉。(b) 写 Tile 级（InCore 函数）——需要精确控制操作数从 UB 到 L0A/L0B 的搬运与累加器驻留（回顾 u1-l5 的 matmul 内存链），这是 Tensor 级表达不了的。

**练习 3**：`pl.matmul` 的 `a_trans` 参数为什么传给 Tile 操作数会报错而不是被忽略？

答案：因为「静默丢弃」会编译出错误的数学。张量不携带布局，转置只能靠标志；Tile 的转置是类型属性（`transpose_view`），没有等价标志。分发层的守卫 `_reject_tile_unsupported` 对非默认值直接 `TypeError` 并给出正确写法（[python/pypto/language/op/unified_ops.py:L1024-L1036](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L1024-L1036)；守卫设计原则见 [L147-L167](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L147-L167) 的注释块）。

## 5. 综合实践

**任务**：把同一个融合算子 `c = a * b + bias`（输入输出均为 128×128 FP32）写两遍——版本 A 仅用 Tensor 级算子，版本 B 用 Tile 级 load/store 三段式——运行两者，对比代码量与结果。

**完整代码**（示例代码，保存为 `tensor_vs_tile.py`）：

```python
import torch
import pypto.language as pl
from pypto.runtime import RunConfig

# ── 版本 A：仅 Tensor 级算子（编排级函数体，无 pl.at、无循环）──
@pl.jit
def fused_tensor_level(a: pl.Tensor, b: pl.Tensor, bias: pl.Tensor, c: pl.Out[pl.Tensor]):
    t = pl.mul(a, b)                  # → tensor.mul：整块相乘，切分交给编译器
    r = pl.add(t, bias)               # → tensor.add：整块加偏置
    return pl.assemble(c, r, [0, 0])  # 把结果写入输出张量


# ── 版本 B：Tile 级三段式（对照 02_elementwise.py 的模式）──
@pl.jit
def fused_tile_level(a: pl.Tensor, b: pl.Tensor, bias: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        ta = pl.load(a, [0, 0], [128, 128])     # Tile 级：亲手搬运每一块
        tb = pl.load(b, [0, 0], [128, 128])
        t = pl.mul(ta, tb)                      # → tile.mul
        tbias = pl.load(bias, [0, 0], [128, 128])
        r = pl.add(t, tbias)                    # → tile.add
        pl.store(r, [0, 0], c)
    return c


if __name__ == "__main__":
    cfg = RunConfig()
    torch.manual_seed(0)
    a = torch.randn(128, 128, dtype=torch.float32)
    b = torch.randn(128, 128, dtype=torch.float32)
    bias = torch.randn(128, 128, dtype=torch.float32)
    expected = a * b + bias

    c1 = torch.zeros(128, 128, dtype=torch.float32)
    fused_tensor_level(a, b, bias, c1, config=cfg)      # 首次调用：编译 + 执行
    assert torch.allclose(c1, expected, rtol=1e-5, atol=1e-5)

    c2 = torch.zeros(128, 128, dtype=torch.float32)
    fused_tile_level(a, b, bias, c2, config=cfg)
    assert torch.allclose(c2, expected, rtol=1e-5, atol=1e-5)

    print("OK — 两个版本结果一致")
```

**执行步骤**：

1. 版本 B 的模式与 `examples/beginner/02_elementwise.py` / `06_concat.py` 完全一致，可直接运行（默认平台 `a2a3sim`）。
2. 版本 A 中 `pl.mul` / `pl.add` 作用于 Tensor 的解析行为有单元测试守护（4.1 的 `test_unified_ops.py`），编排级算子的运行时路径也有系统测试覆盖（如 `tests/st/runtime/control_flow/test_dyn_orch_shape.py` 的 `test_dyn_orch_add`）；但「`@pl.jit` 编排体 + 纯 Tensor 表达式 + `pl.assemble` 写回 `pl.Out`」这一完整组合**待本地验证**。若报错，两个退路：
   - 只验证编译：把执行换成 `fused_tensor_level.lower(a, b, bias, c1)`（`lower` 在 `tests/st/codegen/dsl/test_add_mul_orch_codegen.py:L47` 有使用先例），再用 `python_print` 检查降级产物；
   - 改用 `02_vector_dag.py` 的守护模式：`pl.create_tensor` 分配输出 + InCore 内核写回。
3. 对比指标：核心逻辑行数（预期 A 约 3 行、B 约 7 行）；把张量放大到 512×128 再试——B 需要补分块循环（参照 `chunked_add`），A 一行都不用改。
4. 用 `python_print` 打印两个版本降级后的 IR，找一找版本 A 的 `tensor.mul` / `tensor.add` 去了哪里（应能看到 Tile 级链路——这正是第 10 号 Pass 的效果；系统验证方法见 u3-l5 与 u7-l5）。

**预期结果**：两版本 `torch.allclose` 均通过；A 代码量约为 B 的一半以下，且不随张量变大而增长；IR 层面两版本殊途同归于 Tile 级 load/compute/store。

## 6. 本讲小结

- `pl.*` 是按实参类型分发的调度器，不是第三层抽象；存在不限定形式就优先用它，不存在说明该算子层级专属（`create_tensor`/`full`/`load`/`store` 等）。
- 一条 `pl.add` 走三层：`unified_ops` 分发 → `language/op/tensor_ops` 解包/重包装 → `ir/op/tensor_ops` 构造 `tensor.add` 或 `tensor.adds` 的 `Call`（标量右操作数自动选 `s` 形式）。
- `tensor.*` 算子按角色分类记忆：创建/视图/逐元素/归约（保维度）/广播/矩阵乘/散聚/元信息；归约与广播配合可拼出 softmax 类计算。
- Tensor 级「由编译器放置与调度」的落点是 `ConvertTensorToTileOps` 等 Pass 的自动降级——切分、搬运、暂存分配都不用用户操心；Tile 级则全部手工。
- 生产代码两级协作：编排级用 `create_tensor`/`slice`/`read` 组织数据流（paged attention 范式），计算热点下沉到 InCore 内核做 Tile 级优化（vector_dag 范式）。
- 分发层的守卫哲学：无法被某一路径兑现的关键字参数（`a_trans`、`tmp_tile` 等）一律报 `TypeError` 而非静默丢弃。

## 7. 下一步学习建议

- **下一讲 u2-l4「Tile 级算子：load/store 与片上计算」**：从 Tensor 级转到 Tile 级，学 `pl.load` / `pl.store` 的坐标与尺寸参数、Tile 级算子清单，以及本讲提到的 `tmp_tile` 暂存约定的另一面。
- 精读 `docs/en/user/ops/01-catalog.md`（目录清单）与 `docs/en/api/tile.md`，把两级算子目录对齐着看。
- 通读 `examples/models/02_vector_dag.py` 与 `examples/models/04_paged_attention.py` 的编排函数，体会「Tensor 级数据流 + Tile 级内核」的分层节奏。
- 预习第 3 单元：u3-l3 会从 `compile()` 入口讲编译全流程，届时可以用 `dump_passes` 亲眼看到本讲的 `tensor.add` 在 `convert_tensor_to_tile_ops` 前后变成什么样（对应 Pass 文档 `docs/en/dev/passes/10-convert_tensor_to_tile_ops.md`）。
