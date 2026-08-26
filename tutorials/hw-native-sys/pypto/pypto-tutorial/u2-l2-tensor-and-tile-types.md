# 讲义 u2-l2：Tensor 与 Tile 类型注解

## 1. 本讲目标

学完本讲，你应该能够：

1. 写出并读懂 PyPTO DSL 的全套参数注解：`pl.Tensor[[shape], dtype]`、`pl.Tile[[shape], dtype]`、`pl.Scalar[dtype]`、`pl.Array[extent, dtype]`，以及方向包装 `pl.Out[...]` / `pl.InOut[...]`。
2. 理解注解的「双重身份」：同一个 `pl.Tensor` 类，既是函数签名里的类型标注（注解模式），又是运行时包装 IR 表达式的对象（运行时模式）。
3. 掌握静态 shape 与动态 shape（`pl.dynamic()` / `DynVar`）两种写法，说清楚它们在特化缓存里的行为差异：静态维度以具体值进缓存键，动态维度折叠成 `None`。
4. 根据算子的实际用途（只读 / 只写 / 读写、固定形状 / 逐次变化的形状）选择正确的注解，并避开典型坑（字符串维度、`Scalar` + `InOut`、把 `Out` 错标成 `InOut` 导致依赖图错误）。

本讲承接 u2-l1：上一讲讲了特化缓存键 `CacheKey` 七元组与 `TensorMeta`，本讲把镜头推到「`TensorMeta` 里的 shape / dtype / 动态维度标记最初从哪里来」——答案就是函数签名上的类型注解。

## 2. 前置知识

- **类型注解（type annotation）**：Python 函数签名里冒号后面的部分（`a: pl.Tensor[...]`）。Python 本身运行时不强制检查注解，但 PyPTO 的解析器会把 DSL 函数的注解当成**编译期契约**读走，生成 IR 类型节点。
- **元类（metaclass）与 `__getitem__` 下标语法**：`pl.Tensor[[64, 128], pl.FP16]` 之所以合法，是因为 `Tensor` 的元类实现了 `__getitem__`，下标运算会**真的执行**并返回一个对象。这意味着注解不是"字符串"，而是求值产物。
- **dtype（DataType）**：元素宽度（`pl.FP16` = 16 位 IEEE 半精度等）。PyPTO 没有**隐式类型提升**，宽度不同的操作数必须显式 `pl.cast`（回顾 u1-l5 用 `mul/add/exp/recip` 拼 tanh 的做法）。
- **特化与缓存键**（u2-l1）：`@pl.jit` 首次调用按实参的 shape/dtype 特化并编译，产物存进 `JITFunction._cache`；缓存键里张量信息来自 `TensorMeta`。验证手法是 `kernel.compile()` 配合 `len(kernel._cache)`。
- **Tensor 与 Tile 的物理区别**（u1-l4）：`Tensor` 在 DDR 全局内存，`Tile` 是片上统一缓冲里的固定尺寸数据块。注解里写的是哪一类，就是在告诉编译器这个值住在哪里、允许哪些算子。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| `python/pypto/language/__init__.py` | DSL 门户：把 `Tensor` / `Tile` / `Scalar` / `Out` / `dynamic` 等 typing 名字与 `FP16` 等 dtype 常量统一挂到 `pl.*` 命名空间 |
| `python/pypto/language/typing/tensor.py` | `Tensor` 注解类与元类 `TensorMeta`（下标语法、注解/运行时双模式、`bind_dynamic`） |
| `python/pypto/language/typing/tile.py` | `Tile` 注解类与元类 `TileMeta`（下标语法、MemRef / MemorySpace / TileView 槽位） |
| `python/pypto/language/typing/scalar.py` | `Scalar[dtype]` 注解、算术运算符重载、`pl.RUNTIME` 标记 |
| `python/pypto/language/typing/dynamic.py` | `DynVar` 与 `pl.dynamic(name)`——动态维度注解的核心 |
| `python/pypto/language/typing/direction.py` | `Out[T]` / `InOut[T]` 方向包装（运行时恒等透传） |
| `python/pypto/language/typing/array.py` | `Array[extent, dtype]` 片上小数组注解 |
| `python/pypto/language/parser/type_resolver.py` | 解析器侧：把 AST 注解翻译成 IR 类型（`resolve_param_type` / `_resolve_subscript_type` / `_validate_dim_value`），是所有"注解写错在哪一步报错"的答案所在 |
| `python/pypto/jit/specializer.py` | `@pl.jit` 特化器：`DynDim` / `TensorMeta`、扫描注解里的动态维度、重生成 `@pl.program` 源码 |
| `python/pypto/jit/decorator.py` | `JITFunction`：torch 实参路径与签名路径两条绑定路线、`compile()` / `lower()` |
| `docs/en/dev/language/00-python_syntax.md` | 开发文档：Python IR 语法的类型系统一节（Tensor/Tile/布局/MemRef 槽位权威说明） |
| `docs/en/user/language/00-types.md` | 用户文档：类型速查表、动态形状、参数方向、边界案例 |
| `examples/beginner/02_elementwise.py` | 裸 `pl.Tensor` 注解（不带 shape）的入门示例 |

## 4. 核心概念与源码讲解

### 4.1 注解如何工作：元类下标与「双重身份」

#### 4.1.1 概念说明

`pl.Tensor[[64, 128], pl.FP16]` 不是 Python 原生类型语法，而是一次**真实的函数调用**。`Tensor` 的元类 `TensorMeta` 实现了 `__getitem__`，所以方括号下标会执行并返回一个 `Tensor` 实例，它携带 `shape` / `dtype` / `layout` / `memref` 四个字段。

这个 `Tensor` 类有两副面孔（回顾 u1-l4 讲过的"注解与运行时包装双重身份"，这里落到源码）：

- **注解模式**：写在函数签名里，只存 shape/dtype 元信息，`_expr` 为 `None`；
- **运行时模式**：包着一个 IR 表达式（如 `pl.load(...)` 的返回），`shape`/`dtype` 为 `None`。

区分两副面孔靠 `_annotation_only` 标志：注解实例调用 `unwrap()` 会直接报错。

#### 4.1.2 核心流程

```text
写下 a: pl.Tensor[[64, 128], pl.FP16]
   │
   ├─ Python 求值：TensorMeta.__getitem__(( [64,128], FP16 ))
   │     → Tensor(shape=[64,128], dtype=FP16, _annotation_only=True)   ← 注解对象，宽容，不校验
   │
   ├─ @pl.jit 特化 / @pl.function 解析时：
   │     TypeResolver._resolve_subscript_type() 读 AST 注解
   │     → ir.TensorType(shape=[64,128], dtype=FP32…)                   ← IR 类型，严格，逐槽校验
   │
   └─ 算子调用时（如 pl.load 返回值）：
        Tensor(expr=<ir.Call …>)                                        ← 运行时包装
```

要点：**注解对象本身不做多少校验，校验发生在解析器（`type_resolver.py`）和特化器里**。所以"写错注解"往往不在写下它的那一行报错，而在函数被 `@pl.function` / `@pl.jit` 处理时才报错。

#### 4.1.3 源码精读

`Tensor` 元类的下标入口，接受 2 / 3 / 4 个槽位（第三、四槽位后面讲）：

- [python/pypto/language/typing/tensor.py:41-77](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tensor.py#L41-L77) — `TensorMeta.__getitem__`：把 `pl.Tensor[[shape], dtype, …]` 的下标元组拆开，构造**注解模式**的 `Tensor` 实例（`_annotation_only=True`）。注意它只做"元组长度是否为 2/3/4"的结构检查，不检查 shape 内容——这就是字符串维度能一路活到解析器的原因（见 4.4）。
- [python/pypto/language/typing/tensor.py:171-206](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tensor.py#L171-L206) — `Tensor.__init__`：`_annotation_only=True` 时只存 shape/dtype/layout/memref；`expr` 非空时进入运行时包装模式；两者皆无则抛 `ValueError`。
- [python/pypto/language/typing/tensor.py:208-219](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tensor.py#L208-L219) — `unwrap()`：注解模式调用它抛 `ValueError`，说明注解对象不能混进表达式。
- [python/pypto/language/__init__.py:245-262](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L245-L262) — `pl` 命名空间从 `.typing` 一次性导入 `Tensor` / `Tile` / `Scalar` / `Array` / `InOut` / `Out` / `dynamic` 等；`docs/en/dev/language/00-python_syntax.md` 的类型系统一节（[docs/en/dev/language/00-python_syntax.md:33-64](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/dev/language/00-python_syntax.md#L33-L64)）就是这套语法的权威说明。

#### 4.1.4 代码实践

**实践目标**：亲眼确认"注解是求值产物"，并区分注解模式与运行时模式。

**操作步骤**（示例代码，可在装有 pypto 的 Python 会话中运行）：

```python
import pypto.language as pl

ann = pl.Tensor[[64, 128], pl.FP16]        # 下标真的执行了
print(repr(ann))                            # Tensor[[[64, 128]], <DataType.FP16: …>]
print(ann.shape, ann.dtype)                 # [64, 128] DataType.FP16

try:
    ann.unwrap()                            # 注解模式没有可解包的表达式
except ValueError as e:
    print("ValueError:", e)
```

**需要观察的现象**：

1. `repr(ann)` 打印出 shape 与 dtype，证明注解求值成了一个对象；
2. `unwrap()` 抛 `ValueError: Cannot unwrap annotation-only Tensor`。

**预期结果**：与上面注释一致。本实践只构造注解对象、不触发编译，任何装好 pypto 的环境都能跑。**待本地验证**（具体枚举打印格式可能因版本略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：`pl.Tensor[[64, 128], pl.FP16]` 里的双层方括号，外层和内层各是谁的语法？

**答案**：外层方括号是 `TensorMeta.__getitem__` 的下标调用（参数是一个元组 `([64, 128], FP16)`）；内层方括号是普通的 Python 列表字面量，构成 shape。两层缺一不可——`pl.Tensor[64, 128, pl.FP16]` 会把 shape 槽位错当成别的参数。

**练习 2**：为什么 `pl.load(a, [0, 0], [64, 64])` 的返回值也是一个 `Tensor`/`Tile` 对象，却不能再用作注解？

**答案**：因为它走的是运行时包装模式（`expr` 非空、`shape`/`dtype` 为 `None`），解析器从签名注解里要的是 shape/dtype 元信息；反过来，注解模式对象没有 `expr`，进表达式会 `unwrap()` 失败。同一个类靠 `_annotation_only` 标志区分两种构造路径。

---

### 4.2 容器类型注解：Tensor / Tile / Array / Tuple 的槽位语法

#### 4.2.1 概念说明

容器类型注解回答三个问题（与 `docs/en/user/language/00-types.md` 的三要素一一对应）：

1. **住在哪里**：`Tensor` → DDR；`Tile` → 片上统一缓冲（默认 Vec 空间）；`Array` → 片上标量寄存器堆 / C 栈。
2. **元素多宽**：dtype 常量（`pl.FP16` / `pl.INT32` / …）。
3. **怎么摆**：布局 / MemRef / TileView 等附加槽位（进阶内容，本讲只需认识形态）。

常用写法速查（依据 [docs/en/user/language/00-types.md:140-148](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/language/00-types.md#L140-L148)）：

| 类型 | 住在 | 写法 |
| ---- | ---- | ---- |
| `pl.Tensor[[shape], dtype]` | DDR | `x: pl.Tensor[[64, 128], pl.FP32]` |
| `pl.Tile[[shape], dtype]` | 片上缓冲（默认 Vec） | `t: pl.Tile[[64, 64], pl.FP32]` |
| `pl.Scalar[dtype]` | 寄存器宽度的单值 | `s: pl.Scalar[pl.FP32]` |
| `pl.Array[extent, dtype]` | 片上小数组 | `a: pl.Array[16, pl.INT32]` |
| `pl.Tuple[T1, T2]` | — | 多值返回注解 |

#### 4.2.2 核心流程

以 `TypeResolver` 解析 `t: pl.Tile[[16, 16], pl.FP16, pl.MemRef(...), pl.Mem.Left]` 为例：

```text
签名注解 AST
  → resolve_param_type()          # 先剥方向包装 Out/InOut（见 4.3）
  → _resolve_subscript_type()     # 识别 Tensor/Tile/Scalar/Array/Tuple 名字
      ├─ shape 槽  → _parse_shape → _validate_dim_value   # int 或 DynVar（见 4.4）
      ├─ dtype 槽  → resolve_dtype → ir.DataType
      └─ 附加槽    → MemRef / MemorySpace / TensorView / TileView / 布局
  → ir.TensorType / ir.TileType / ir.ScalarType / ir.ArrayType
```

Tensor 允许 2/3/4 个槽位，Tile 允许 2/3/4/5 个槽位；Tile 若带 `MemRef` 则**必须**同时写明 MemorySpace。

#### 4.2.3 源码精读

- [python/pypto/language/typing/tensor.py:41-56](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tensor.py#L41-L56) — `Tensor` 下标只接受元组长度 2、3、4，否则 `TypeError`，错误信息列出三种合法形态。
- [python/pypto/language/typing/tile.py:25-81](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tile.py#L25-L81) — `TileMeta.__getitem__`：尾部附加槽按**类型**逐个识别（`MemRef` / `MemorySpace` / `TileView`），每类最多一个；带 `MemRef` 却没写 `MemorySpace` 时在 [tile.py:71-72](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tile.py#L71-L72) 抛 `TypeError`。
- [python/pypto/language/typing/array.py:53-60](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/array.py#L53-L60) — `Array` 下标固定两槽 `[extent, dtype]`；extent 必须是编译期常量整数（解析器侧在 type_resolver 里再查一遍）。`Array` 通常**创建**而非注解——数组不跨函数边界（[array.py:81-95](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/array.py#L81-L95) 的类文档写明注解形式"rare"）。
- [python/pypto/language/parser/type_resolver.py:390-534](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/type_resolver.py#L390-L534) — `_resolve_subscript_type`：解析器侧的"注解语法总闸"。docstring 列出全部合法形态；[type_resolver.py:474-499](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/type_resolver.py#L474-L499) 对槽位数量不符给出带 hint 的 `ParserTypeError`。
- [python/pypto/language/parser/type_resolver.py:1321-1338](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/type_resolver.py#L1321-L1338) — `_to_ir_shape`：IR 构造器要求 shape 要么全是 `int`、要么全是 `ir.Expr`；只要有一个动态维度（Expr），所有 int 都被包成 `ConstInt(INDEX)`。这就是"混排 shape"在 IR 里的统一化规则。
- [python/pypto/language/__init__.py:274-296](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L274-L296) — `pl.FP16` 等 dtype 常量全部是 `DataType` 枚举成员的再导出，完整表见 [docs/en/user/language/00-types.md:96-112](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/language/00-types.md#L96-L112)（含 `INDEX`：循环变量与维度运算的 64 位索引类型；`TASK_ID`：任务句柄）。

#### 4.2.4 代码实践

**实践目标**：亲手触发一次"槽位数量错误"和一次"Tile 带 MemRef 缺 MemorySpace 错误"，熟悉报错样式，以后能秒懂。

**操作步骤**（示例代码）：

```python
import pypto.language as pl

# ① 槽位数错误：Tensor 只接受 2/3/4 槽
try:
    pl.Tensor[[64, 128]]                       # 只有 shape，没有 dtype
except TypeError as e:
    print("TypeError:", e)

# ② Tile 带 MemRef 必须同时给 MemorySpace
addr = pl.MemRef(0, 512, 0)                    # 三参数 MemRef：描述既有分配
try:
    pl.Tile[[16, 16], pl.FP16, addr]           # 缺 pl.Mem.Left/Vec
except TypeError as e:
    print("TypeError:", e)
```

**需要观察的现象**：两条 `TypeError`，错误信息分别枚举了合法的槽位形态 / 明说"Tile annotation with MemRef must also specify an explicit MemorySpace"。

**预期结果**：①② 都在**求值注解时**（还没进解析器）就抛错——这类结构性错误被元类前置拦截。**待本地验证**（本讲没有实际运行，报错文案以 [tile.py:71-72](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tile.py#L71-L72) 的源码为准）。

#### 4.2.5 小练习与答案

**练习 1**：`pl.Tensor[[32, 64], pl.FP32, pl.NZ]` 的第三个槽位是什么？`NZ` 能用在 `Tile` 注解上吗？

**答案**：第三槽位是布局（layout），`pl.NZ` 是一种 tile 化的硬件布局。用户文档明确：日常写 `pl.Tensor` 注解应当**只写 shape + dtype、不写布局标记**，布局由 Pass 依据产生/消费视角自动推导；`NZ` 是 tile-only 布局，不应作为 `Tensor` 注解（见 [docs/en/user/language/00-types.md:162-177](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/language/00-types.md#L162-L177)）。

**练习 2**：`pl.Array` 与 `pl.Tensor` 都是"一组同 dtype 元素"，为什么数组几乎不写注解？

**答案**：`Array` 是片上小数组（extent 编译期固定），v1 里**不跨函数边界**，所以它通常由 `pl.array.create(N, dtype)` 创建、用 `arr[i]` 下标读写（函数式更新），注解形式只是为清晰与向前兼容保留（[python/pypto/language/typing/array.py:81-95](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/array.py#L81-L95)）。

**练习 3**：一个 shape 混排注解 `pl.Tensor[[M, 128], pl.FP32]`（M 为动态）在 IR 里长什么样？

**答案**：`_to_ir_shape` 会把整个 shape 统一成 `list[Expr]`：`[<Var M>, ConstInt(128, INDEX)]`——静态的 128 也被包成 `ConstInt`，因为 `TensorType`/`TileType` 构造器不接受 int 与 Expr 混排（[type_resolver.py:1321-1338](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/type_resolver.py#L1321-L1338)）。

---

### 4.3 参数方向与标量：pl.Out / pl.InOut / pl.Scalar / pl.TaskId / pl.RUNTIME

#### 4.3.1 概念说明

- **方向（direction）是签名的一部分，不是约定**。默认 `In`（只读）；`pl.Out[T]` 只写；`pl.InOut[T]` 读写。编译器靠方向推导任务依赖：把一个实际要读写的缓冲错标成 `Out`，得到的不是编译错误，而是**错误的依赖图**（数据竞争）——这是用户文档点名的头号坑（[docs/en/user/language/00-types.md:252-262](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/language/00-types.md#L252-L262)）。
- **`pl.Scalar[dtype]`** 是按值传入的单个值（寄存器宽度），不是缓冲。
- **`pl.TaskId`** 是便捷别名，等价于 `pl.Scalar[pl.TASK_ID]`，用于 manual_scope 任务依赖。
- **`pl.RUNTIME`** 是编译期标记（不是值）：在 `compile()` / `lower()` 时传给某个标量参数，表示该参数**不特化**——产物里保留真实的 `pl.Scalar` 参数、调度时再给值，并且像动态维度一样**退出缓存键**。

#### 4.3.2 核心流程

```text
签名 def kernel(out: pl.Out[pl.Tensor[[…], FP32]], s: pl.Scalar[FP32]):
   │
   ├─ 求值 pl.Out[pl.Tensor[…]]：__class_getitem__ 原样返回内层 Tensor 注解对象
   │    （方向信息只存在于 AST 结构里，运行时不落地 —— u1-l5 讲过的"纯解析期标记"）
   │
   └─ 解析 resolve_param_type()：
        看到 Subscript 外层是 Out/InOut → 记 direction，剥掉包装
        再解析内层类型 → (ir.TensorType, ir.ParamDirection.Out)
        校验：Scalar 类型 + InOut → 直接 ParserTypeError
```

#### 4.3.3 源码精读

- [python/pypto/language/typing/direction.py:22-52](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/direction.py#L22-L52) — `Out` / `InOut` 的全部实现：`__class_getitem__` 把内层类型**原样返回**；方向信息由解析器从 AST 的 `Subscript` 结构上读走。类型检查器视角下它是 `Annotated[T, "Out"]` 别名。
- [python/pypto/language/parser/type_resolver.py:248-288](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/type_resolver.py#L248-L288) — `resolve_param_type`：剥方向包装、默认 `In`、并在 [type_resolver.py:281-286](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/type_resolver.py#L281-L286) 拒绝 `Scalar` + `InOut`（只有 Tensor/Tile 支持 InOut）。
- [python/pypto/language/typing/scalar.py:38-47](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/scalar.py#L38-L47) — `ScalarMeta.__getitem__`：`pl.Scalar[pl.FP32]` → 注解模式的 `Scalar` 实例。
- [python/pypto/language/typing/scalar.py:171-257](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/scalar.py#L171-L257) — `Scalar` 重载了全套算术/比较运算符，所以标量参数 `n` 在 DSL 里能直接写 `n * 2`、`n // 4`，生成对应 IR 表达式；[scalar.py:156-169](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/scalar.py#L156-L169) 的 `__bool__` 恒抛 `TypeError`，防止符号标量被 Python 隐式转布尔。
- [python/pypto/language/typing/scalar.py:265-322](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/scalar.py#L265-L322) — `RuntimeScalarMarker` 与 `RUNTIME` 单例：编译期标记，`unwrap()` 恒抛错，只能传给 `compile()`/`lower()` 表示"不特化"。
- [python/pypto/language/__init__.py:298-300](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L298-L300) — `TaskId = Scalar[TASK_ID]`：注意这一行在**模块导入时求值**，所以 `pl.TaskId` 已经是一个注解对象，直接写 `tid: pl.TaskId` 即可，不用再加方括号。

#### 4.3.4 代码实践

**实践目标**：验证「方向包装运行时恒等透传」与「Scalar + InOut 被解析器拒绝」。

**操作步骤**（示例代码）：

```python
import pypto.language as pl

# ① 运行时透传：Out[...] 返回的就是内层注解对象本身
inner = pl.Tensor[[4, 8], pl.FP32]
wrapped = pl.Out[inner]
print(wrapped is inner)                       # True —— 方向信息没有存进对象

# ② Scalar + InOut：解析器拒绝（需要真的声明一个 DSL 函数）
@pl.function
def bad(s: pl.InOut[pl.Scalar[pl.FP32]]):     # 解析签名时就会报错
    return s

# ③ TaskId 是现成的注解对象（导入时已求值 Scalar[TASK_ID]）
print(type(pl.TaskId))                        # <class 'pypto.language.typing.scalar.Scalar'>
```

**需要观察的现象**：① 打印 `True`；② 抛 `ParserTypeError`（信息为 "Scalar parameters cannot have InOut direction"，hint 说明只有 Tensor/Tile 支持）；③ `type(pl.TaskId)` 打印 `Scalar` 类本身，而 `pl.TaskId` 是它的一个注解实例。

**预期结果**：如上。② 的错误在 `@pl.function` 装饰器解析签名时触发（`resolve_param_type`）。**待本地验证**（本讲未实际运行；错误时机与文案以源码为准）。

#### 4.3.5 小练习与答案

**练习 1**：累加器 `acc` 既被读又被写，注解写成 `pl.Out[...]` 会怎样？

**答案**：能编译通过，但运行结果只在"两个任务重叠执行"时才出错——编译器认为 `Out` 参数无需等待此前的读者/写者，排出来的依赖图少边，产生数据竞争。正确写法是 `pl.InOut[...]`（依据 [docs/en/user/language/00-types.md:273-275](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/language/00-types.md#L273-L275) 的边界案例表）。

**练习 2**：`factor: pl.Scalar[pl.FP32]` 与 `factor: pl.FP32` 有何区别？

**答案**：后者只是 Python 层面把注解求值成一个 `DataType` 枚举成员，不是 PyPTO 的标量注解；合法的标量参数注解是 `pl.Scalar[pl.FP32]`（解析器在 `_resolve_subscript_type` 里按类型名 `Scalar` 分发）。特化时标量实参的**值**会进缓存键（u2-l1），除非用 `pl.RUNTIME` 显式声明不特化。

**练习 3**：`pl.TaskId` 为什么不需要写成 `pl.TaskId[...]`？

**答案**：`python/pypto/language/__init__.py:300` 在导入时就执行了 `TaskId = Scalar[TASK_ID]`，`Scalar[dtype]` 下标求值已经发生，`pl.TaskId` 本身就是注解对象。

---

### 4.4 静态与动态 shape：pl.dynamic() / DynVar 与特化行为

#### 4.4.1 概念说明

- **静态维度**：写在注解里的具体整数。编译器可以围绕它做规划（分块选择、展开因子、静态越界检查）。在 `@pl.jit` 的 torch 实参路径上，shape/dtype **取自传入的 torch 张量**，注解中的静态数值不参与逐维校验（见下方源码）；换一个 shape 就是一次新的特化（新缓存条目）。
- **动态维度**：`M = pl.dynamic("M")` 创建一个 `DynVar`，把它放进注解：`pl.Tensor[[M, 128], pl.FP32]`。该维度的具体数值推迟到每次启动时才确定，**一份编译产物服务该维度所有取值**——动态维度在 JIT 缓存键里折叠为 `None`，换 extent 不触发重编。
- **关键纠偏**：动态维度**不能**写成字符串。`pl.Tensor[["M", 128], pl.FP32]`（部分教程里的旧写法/直觉写法）会被解析器拒绝——合法形态只有整数和 `pl.dynamic()` 变量。另外，**同一个维度要复用同一个 `DynVar` 对象**，两次 `pl.dynamic("M")` 是两个独立维度（用户文档边界案例表点名此坑）。
- `DynVar` 继承自 `Scalar`，dtype 固定为 `INDEX`，所以它能出现在任何接受 `IntLike` 的槽位（如 `TensorView.valid_shape`）。

#### 4.4.2 核心流程

`@pl.jit` 下的动态维度全链路：

```text
模块级：M = pl.dynamic("M")                     ← 创建 DynVar（名字必须是合法标识符）
签名：a: pl.Tensor[[M, 128], pl.FP32]
   │
   ├─ ① decorator._extract_tensor_meta：
   │      从 torch 张量取 extents/dtype；
   │      _collect_annotation_dynamic_dims 扫描注解，发现 dim0 是 DynVar
   │      → TensorMeta.shape[0] = DynDim(name="M", static_bound=实际extent)
   │
   ├─ ② 缓存键：DynDim 维度折叠成 None        ← 换 extent 仍命中同一条缓存
   │
   ├─ ③ specializer 重生成 @pl.program 源码：
   │      M = pl.dynamic("M") 提升到模块级（共享 dynvar 缓存）
   │      注解保持 a: pl.Tensor[[M, 128], pl.FP32]（M 保持动态）
   │
   └─ ④ 解析：_validate_dim_value 看到 DynVar
          → 借出/登记 ir.Var("M", ScalarType(INDEX))（按名字缓存，跨函数共享同一实例）
          → _to_ir_shape 统一成 [Var M, ConstInt 128]
```

#### 4.4.3 源码精读

- [python/pypto/language/typing/dynamic.py:20-48](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/dynamic.py#L20-L48) — `DynVar`：名字必须是合法标识符（否则 `ValueError`）；dtype 固定 `INDEX`；`_ir_var` 惰性创建、首次使用后由 TypeResolver 填充——**同一个 DynVar 对象在多处注解里共享同一个 `ir.Var` 实例**，保证跨 `@pl.function` 边界的结构相等。
- [python/pypto/language/typing/dynamic.py:66-75](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/dynamic.py#L66-L75) — `dynamic(name)` 工厂函数本体。
- [python/pypto/language/parser/type_resolver.py:1146-1182](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/type_resolver.py#L1146-L1182) — `_validate_dim_value`：维度只接受 `int`、`DynVar`、或整数类型的标量表达式；**其他一切（包括字符串）抛 `ParserTypeError: "Shape variable … must be int or pl.dynamic()"`**——这就是字符串维度写法的"死刑判决书"。`DynVar` 在这里按名字登记进 `_dyn_var_cache`，同名复用同一个 `ir.Var`。
- [python/pypto/language/parser/type_resolver.py:1289-1319](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/type_resolver.py#L1289-L1319) — `_resolve_shape_dim`：shape 里写**变量名**（如 `pl.Tensor[[M, 128], …]` 的 `M`）时的解析顺序——先查闭包变量（int 常量或 DynVar），再查函数体内的 Scalar 变量，都找不到则报 "Unknown shape variable"。
- [python/pypto/jit/specializer.py:17-33](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L17-L33) — 特化器顶部的转换规则表：`a: pl.Tensor[[M, 128], pl.FP32]` → **M 保持动态**；`M = pl.dynamic("M")` 被提升到模块级；`bind_dynamic` 调用被删除（信息已并入注解）。
- [python/pypto/jit/specializer.py:57-77](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L57-L77) — `DynDim` 数据类：`static_bound` 记录本次特化的具体 extent，但在缓存键里被替换成 `None`——"不同 bound 复用同一次编译"的机关就在这个字段的设计上。
- [python/pypto/jit/specializer.py:356-386](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L356-L386) — `_collect_annotation_dynamic_dims`：让 `@pl.jit` 支持与 `@pl.program` 相同的动态形状契约——直接写在注解里的 DynVar 即标记该维动态，无需 `bind_dynamic`；返回 `(参数, 维号)` 集合与绑定表，与 `bind_dynamic` 来源**取并集**。
- [python/pypto/jit/decorator.py:188-216](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L188-L216) — `_build_tensor_meta`：extents 逐维装配 `TensorMeta.shape`——被标记动态的维写 `DynDim(static_bound=extent)`，静态维写整数。
- [python/pypto/jit/decorator.py:314-342](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L314-L342) — `_param_layouts` 的 docstring 一锤定音："**The torch-argument path derives shape and dtype from the passed tensors, so it never looks at annotations**"——torch 实参路径上 shape/dtype 来自张量，注解只贡献 layout 和动态维标记。
- [python/pypto/jit/decorator.py:2171-2215](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2171-L2215) — `JITFunction.compile()` 的**签名模式**：不传任何张量实参时，shape/dtype 契约直接从参数注解读取（每个张量参数必须有完整 `pl.Tensor[[...], dtype]` 注解；动态维无需给值，产物 extent 无关）。这是本讲综合实践的观察窗口。
- [python/pypto/jit/decorator.py:2263-2306](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L2263-L2306) — `lower()`：特化并返回**跑完 Pass 之后**的 IR `Program`，不执行代码生成、不上设备——用 `python_print` 打印它就能看到动态维在 IR 里的形态。
- 对照阅读：[docs/en/user/language/00-types.md:205-250](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/language/00-types.md#L205-L250) 动态形状一节（含"两种 extent 共用一份产物"的可运行示例），以及 [docs/en/user/language/00-types.md:273-277](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/language/00-types.md#L273-L277) 边界案例表（字符串维度、重复 `pl.dynamic` 都在表里）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：写两个算子——静态注解 `pl.Tensor[[128, 128], pl.FP16]` 与动态维度注解 `pl.Tensor[[M, 128], pl.FP32]`——对比两者在特化缓存上的行为差异，并在 IR 里看到动态维变成 `Var`。

**操作步骤**（示例代码，保存为 `spec_compare.py`）：

```python
import torch
import pypto.language as pl
from pypto.ir import python_print
from pypto.runtime import RunConfig

M = pl.dynamic("M")                              # ① 动态维度：先建 DynVar，再进注解


@pl.jit
def scale_static(a: pl.Tensor[[128, 128], pl.FP16], c: pl.Out[pl.Tensor[[128, 128], pl.FP16]]):
    with pl.at(level=pl.Level.CORE_GROUP):
        t = pl.load(a, [0, 0], [128, 128])
        pl.store(t, [0, 0], c)
    return c


@pl.jit
def scale_dynamic(a: pl.Tensor[[M, 128], pl.FP32], c: pl.Out[pl.Tensor[[M, 128], pl.FP32]]):
    with pl.at(level=pl.Level.CORE_GROUP):
        t = pl.load(a, [0, 0], [128, 128])
        pl.store(t, [0, 0], c)
    return c


cfg = RunConfig()

# ② 动态版：两种 extent 只编译一次
for extent in (128, 256):
    a = torch.ones(extent, 128, dtype=torch.float32)
    c = torch.zeros(extent, 128, dtype=torch.float32)
    scale_dynamic(a, c, config=cfg)
print("dynamic cache entries:", len(scale_dynamic._cache))      # 预期 1

# ③ 静态版：不同 shape 各自特化（shape 取自张量）
for extent in (128, 256):
    a = torch.ones(extent, extent, dtype=torch.float16)
    c = torch.zeros(extent, extent, dtype=torch.float16)
    scale_static(a, c, config=cfg)
print("static cache entries:", len(scale_static._cache))        # 预期 2

# ④ 签名模式编译 + IR 观察：不传张量，契约来自注解
prog = scale_dynamic.lower()                                     # 跑完 Pass 的 IR
print(python_print(prog))                                        # 找 a 的 shape：Var M + ConstInt 128
```

**需要观察的现象**：

1. ② 打印 `1`：extent 128 与 256 共用同一条缓存（`DynDim.static_bound` 在键里折成 `None`）。
2. ③ 打印 `2`：torch 路径 shape 取自张量，(128,128) 与 (256,256) 是两个不同的键。
3. ④ 打印的 IR 里，动态版的形状参数是名为 `M` 的变量（`Var M`），而不是写死的 128；静态版（若同样 `lower()`）形状是两个整数字面量。
4. 反例实验：把 ① 改成 `pl.Tensor[["M", 128], pl.FP32]`（字符串维度），重新运行——预期在解析阶段抛 `ParserTypeError`，报错信息为 "Shape variable … must be int or pl.dynamic()"。

**预期结果**：如上四条。其中 ③ 的"注解写 128×128 但传 256×256 不报错"依据 [decorator.py:317-320](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/decorator.py#L317-L320) 的路径说明（torch 路径不读注解 shape）；整段脚本涉及编译与设备执行，**待本地验证**——若 ③ 意外报错，请记录报错文案，那说明该路径新增了注解一致性校验，是需要回馈到本讲的事实。

**提示**：如果只想观察注解差异、不碰设备，可把 ②③ 换成 `scale_dynamic.compile()`（签名模式，不需要张量），再用 `len(._cache)` 观察；④ 的 `lower()` 本身不上设备。

#### 4.4.5 小练习与答案

**练习 1**：`rows = pl.dynamic("M")` 之后在两个函数里分别写 `pl.Tensor[[rows, 128], …]`，和各自 `pl.dynamic("M")` 再用，IR 上有何区别？

**答案**：前者两个注解引用**同一个 `DynVar` 对象**，首次解析时登记进 `_dyn_var_cache`，之后共享同一个 `ir.Var("M")` 实例——两个函数的该维是同一个维度（结构相等）；后者是两个对象、两次登记（同名会命中同一缓存条目，但语义上用户文档明确要求"复用对象，不要创建第二个同名变量"，因为靠名字缓存只是实现细节，对象同一性才是契约，见 [docs/en/user/language/00-types.md:245-247](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/language/00-types.md#L245-L247)）。

**练习 2**：什么时候**不该**用动态维度？

**答案**：维度真正固定时就保持静态。静态 extent 是编译器可以据以规划的数字（分块、展开、静态边界检查），动态维度等于把这个信息从编译器手里扣掉（[docs/en/user/language/00-types.md:248-250](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/user/language/00-types.md#L248-L250)）。另外：入口函数若给了具体 shape，程序就被钉死在一个 extent 上；在编排层对动态形状张量直接做编排级操作会更早失败（`InitMemRef` 需要常量维度），应把它交给 InCore 内核处理。

**练习 3**：`bind_dynamic` 和注解内 `pl.dynamic()` 是什么关系？

**答案**：两者是同一契约的两个入口，可叠加、结果取并集。`bind_dynamic` 用于**裸 `pl.Tensor`** 参数（注解没有 shape 可放 DynVar）：`a.bind_dynamic(0, M)` 把第 0 维标成动态，且它是运行时 no-op、由特化器从 AST 静态读走（[python/pypto/language/typing/tensor.py:226-267](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/typing/tensor.py#L226-L267)）；注解内写 `pl.Tensor[[M, 128], …]` 则无需 `bind_dynamic`，特化器重生成源码时还会把多余的 `bind_dynamic` 调用删掉（[specializer.py:26-27](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L26-L27)）。

## 5. 综合实践

**任务：给一个"带缩放因子的行变换"算子写完整的签名卡片，并验证每条注解在 IR 里的归宿。**

写一个 `@pl.jit` 算子，签名同时用上本讲全部注解要素：

```python
import torch
import pypto.language as pl
from pypto.ir import python_print

M = pl.dynamic("M")


@pl.jit
def row_scale(
    x: pl.Tensor[[M, 128], pl.FP16],                    # 动态行数 + 静态列数
    w: pl.Tensor[[128, 128], pl.FP32],                  # 全静态权重
    acc: pl.InOut[pl.Tensor[[M, 128], pl.FP32]],        # 读改写累加器
    out: pl.Out[pl.Tensor[[M, 128], pl.FP32]],          # 只写输出
    factor: pl.Scalar[pl.FP32],                         # 标量因子
):
    with pl.at(level=pl.Level.CORE_GROUP):
        t = pl.cast(pl.load(x, [0, 0], [128, 128]), pl.FP32)
        acc_t = pl.load(acc, [0, 0], [128, 128])
        out_t = pl.mul(pl.add(acc_t, t), factor)
        pl.store(out_t, [0, 0], out)
        pl.store(out_t, [0, 0], acc)
    return acc, out
```

然后完成三件事：

1. **签名模式编译**：`row_scale.compile()`（不传张量；标量 `factor` 通过关键字给一个字面量，如 `row_scale.compile(factor=2.0)`），确认它从注解读到了完整契约；再用 `row_scale.compile(factor=pl.RUNTIME)` 编译一份不特化标量的产物，比较 `len(row_scale._cache)` 的增量——两个特化（字面量 / RUNTIME）是不同的缓存条目。
2. **IR 归宿核对**：`prog = row_scale.lower()` 后 `python_print(prog)`，逐项核对：`x`/`acc`/`out` 的 dim0 是 `Var M`、dim1 是 128；`w` 两维都是整数；`factor` 是被特化进 IR 的常量（字面量路径）或仍是标量参数（RUNTIME 路径）。
3. **对照实验**：把 `factor` 的注解换成 `pl.InOut[pl.Scalar[pl.FP32]]` 再 `lower()`，确认被 `ParserTypeError` 拒绝；把 `M` 换成字符串 `"M"` 再 `lower()`，确认报错信息来自 `_validate_dim_value`。

**验收标准**：能说清楚——注解的每一槽分别变成了 IR 里的什么；动态维度与 `RUNTIME` 标量为什么"退出缓存键"；两个反例各自死在解析器的哪一行检查上。运行结果**待本地验证**。

## 6. 本讲小结

- 注解是**求值产物**：`pl.Tensor[[...], dtype]` 经由元类 `__getitem__` 变成一个携带 shape/dtype 的注解对象；同一个类还有"运行时包装 IR 表达式"的第二身份，靠 `_annotation_only` 区分，`unwrap()` 在注解模式恒失败。
- 槽位语法：`Tensor` 2/3/4 槽（shape、dtype、布局或 TensorView 或 MemRef），`Tile` 2/3/4/5 槽（额外可带 MemorySpace / TileView，且 MemRef 必须配 MemorySpace），`Scalar[dtype]`、`Array[extent, dtype]` 各自成体系；dtype 常量是 `DataType` 枚举的再导出，无隐式提升。
- 方向是签名的一部分：`pl.Out` / `pl.InOut` 在运行时恒等透传、只留 AST 标记，由 `resolve_param_type` 落成 `ir.ParamDirection`；`Scalar` 不允许 `InOut`。错标方向的代价是**错误的依赖图**而非编译错误。
- 静态与动态 shape：动态维度的唯一合法写法是 `M = pl.dynamic("M")` 后把 `M` 放进注解（字符串维度被 `_validate_dim_value` 拒绝）；动态维在缓存键里折成 `None`，一份产物服务所有 extent；同一维度必须复用同一个 `DynVar` 对象。
- `@pl.jit` 的 torch 实参路径上 shape/dtype **取自传入张量**，注解贡献的是布局与动态维标记；而 `compile()` / `lower()` 的**签名模式**（不传张量）直接从注解读契约，是观察注解效果最干净的窗口。

## 7. 下一步学习建议

- 下一讲 **u2-l3（Tensor 级算子）**：把本讲的注解放进真实算子调用里，看 `tensor.*` 命名空间如何用这些类型完成创建、切片与组装。
- 阅读 [examples/intermediate/06_dyn_valid_shape.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/intermediate/06_dyn_valid_shape.py) 与 [examples/models/06_paged_attention_dynamic.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/models/06_paged_attention_dynamic.py)：生产级算子如何成组使用 `pl.dynamic()`（一个文件里十来个 DynVar 协同）。
- 想深挖解析侧：通读 [python/pypto/language/parser/type_resolver.py](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/parser/type_resolver.py) 的 `resolve_param_type` → `_resolve_subscript_type` → `_validate_dim_value` 调用链，这是 u3-l1（DSL 解析）的预习材料。
- 想深挖特化侧：对照 [python/pypto/jit/specializer.py:17-33](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/jit/specializer.py#L17-L33) 的转换规则表逐条在 `lower()` 产物里找证据，衔接 u2-l1 的缓存键七元组。
