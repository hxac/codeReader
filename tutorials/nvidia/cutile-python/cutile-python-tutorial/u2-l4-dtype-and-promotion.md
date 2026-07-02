# 数据类型 DType 与类型提升

## 1. 本讲目标

上一讲（u2-l3）我们弄清了 tile 的形状约束、scalar 是零维 tile，以及「Python 字面量在 tile code 里自动成为 loosely typed（松散类型）常量标量」这条伏笔。本讲就把这条伏笔彻底展开，回答三个问题：

1. cuTile 里到底有哪些数据类型？它们如何被定义、如何分类？
2. 当两个不同类型的 tile/scalar 做运算（如 `a + b`）时，结果类型由谁决定？
3. 在 `load`/`store` 以及浮点运算中，`RoundingMode`（舍入模式）和 `PaddingMode`（填充模式）各扮演什么角色？

学完后你应当能够：

- 说出 `int8..int64`、`float16/32/64`、`bfloat16`、`tfloat32`、`float8_e4m3fn` 等类型的用途与归属。
- 区分 **loosely typed（松散类型）常量** 与 **strictly typed（严格类型）常量**，并预测二者参与运算后的提升结果。
- 读懂 `_datatype.py` 里的类型提升表（promotion table），并能手动查表推断任意两个数值类型运算的结果。
- 理解 `RoundingMode` 与 `PaddingMode` 在 `load`/`store` 与算术运算中的作用。

## 2. 前置知识

在进入源码前，先用通俗语言铺几个概念。

**数据类型（dtype）** 描述一个值的「存储格式」：用多少比特、是否有符号、整数还是浮点、浮点的指数/尾数位如何分配。同一个数学值 `5`，存成 `int16` 和存成 `float32` 在内存里的二进制完全不同。cuTile 里的 array、tile、scalar 都有一个确定的 dtype。

**类型提升（type promotion）** 是「两个不同类型的操作数一起运算时，先统一到一个公共类型再计算」的规则。例如 `int16 + int32` 在大多数语言里会提升到 `int32`。NumPy、C++、cuTile 都有各自的提升规则，cuTile 的规则与 NumPy 高度一致，但对受限浮点（restricted float）和有符号/无符号混合做了更严格的限制。

**算术类型（arithmetic dtype）** 指能参与通用四则运算（加减乘除）的数值类型。注意：cuTile 里有些「数值类型」并不算术，例如 `tfloat32`、`float8_*`，它们只能用于特定硬件指令（如张量核 `mma`），不能直接做 `a + b`，需要先显式转换（cast）。

**舍入（rounding）与填充（padding）** 是数值计算里两类「边界行为」：
- 当浮点精度不够（如把 `float32` 转成 `float16`）时，多余的位如何取舍，由 **RoundingMode** 决定。
- 当 `load` 一个 tile，而该 tile 部分超出数组边界时，越界元素填什么值，由 **PaddingMode** 决定。

> 名词约定：本讲出现的 `|dtype|`、`|tile code|`、`|loosely typed|` 等带竖线的写法是 cuTile 官方文档的术语替换标记，含义等同普通名词。

## 3. 本讲源码地图

本讲涉及的关键文件，按职责分为三类：

| 文件 | 作用 |
| --- | --- |
| [_datatype.py]([L1-L720](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_datatype.py)) | cuTile 全部 dtype 的定义、分类、查询函数（`is_integral`/`is_float`/`is_arithmetic` 等），以及核心的**类型提升表** `_DTypePromotionImpl`。 |
| [_numeric_semantics.py]([L1-L57](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_numeric_semantics.py)) | 定义两个枚举：`RoundingMode`（舍入模式）与 `PaddingMode`（填充模式）。 |
| [docs/source/data.rst]([L202-L274](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst)) | 官方文档对 Data Types、Arithmetic Promotion、Rounding/Padding Modes 的文字说明与提升规则描述。 |

此外，本讲会引用三处「实现层」源码来把规则说透（这些文件在 U5/U6 会深入讲解，本讲只取关键片段）：

| 文件 | 作用 |
| --- | --- |
| [_ir/typing_support.py]([L147-L161](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/typing_support.py)) | `dtype_of_constant_scalar`：把一个 Python 字面量归类成具体 dtype（loosely typed 常量的「隐式类型」来源）。 |
| [_ir/ops_utils.py]([L243-L294](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops_utils.py)) | `promote_dtypes`/`promote_types`：在运算时把两个操作数（含 loosely typed 常量）提升到公共类型。 |
| [test/test_loose_typing.py]([L1-L188](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_loose_typing.py)) | 真实测试，断言了 loosely/strictly 常量混合运算的结果类型，是本讲最好的「可运行依据」。 |

---

## 4. 核心概念与源码讲解

### 4.1 DType：类型对象与类型族

#### 4.1.1 概念说明

在 cuTile 里，**DType 不是一个 Python `type`，而是一个不可变（immutable）的对象实例**。也就是说，`ct.int32`、`ct.float16` 本身就是对象，你可以把它当参数传递、当值比较（`x.dtype == ct.float32`），甚至可以作为 kernel 参数。

这一点和 NumPy（`np.dtype('int32')`）、PyTorch（`torch.int32` 是个枚举值）都不同——cuTile 的 dtype 是「单例对象」，全局唯一。源码里用 `_dtype_by_name` 字典保证同名 dtype 只创建一次。

cuTile 的数值 dtype 可分为三大族（外加一个受限子族）：

| 族（category） | 包含的类型 | 是否算术（可四则运算） |
| --- | --- | --- |
| Boolean | `bool_` | 是 |
| Integral（整数） | `uint8/16/32/64`、`int8/16/32/64` | 是 |
| Float（普通浮点） | `float16/32/64`、`bfloat16` | 是 |
| RestrictedFloat（受限浮点） | `tfloat32`、`float8_e4m3fn/e5m2/e8m0fnu`、`float4_e2m1fn` | **否**（只能用于特定指令，需显式 cast） |

**RestrictedFloat 是本讲的一个关键陷阱**：`tfloat32`、各种 `float8_*` 看起来像浮点，但它们不能直接做 `a + b`，因为它们的精度/范围太特殊，隐式提升会带来意外结果。cuTile 干脆禁止隐式提升，强制你用 `astype` 显式转换。这正是张量核（tensor core）专用类型的典型用法（见 u3-l6）。

#### 4.1.2 核心流程

一个 dtype 的「定义」由一个不可变数据类 `_DTypeDefinition` 承载，关键字段是：

- `bitwidth`：位宽（如 `int32` 是 32）。
- `numeric_category`：所属族（`Boolean`/`Integral`/`Float`/`RestrictedFloat`），`None` 表示非数值类型（如指针 dtype）。
- `simple_bytecode_type`：对应到字节码层的 `bc.SimpleType`（这是后端序列化用的，U7 会讲）。
- 整数还多一个 `signed` 字段，并提供 `get_min_value`/`get_max_value`。

dtype 的创建流程是：

```text
_numeric_dtype / _integer_dtype(name, bitwidth, ...)
        │  构造 _DTypeDefinition（或 _IntegerDTypeDefinition）
        ▼
_define_dtype(name, definition)
        │  查 _dtype_by_name，若已存在则直接返回旧对象（保证单例）
        │  否则 object.__new__(DType)，登记进 _dtype_defs / _dtype_by_name
        ▼
   返回唯一的 DType 单例（如 int32、float16）
```

族之间的有序关系 `Boolean < Integral < Float`（代码里用 `IntEnum` 的 `Boolean=0, Integral=1, Float=2, RestrictedFloat=3` 表达）是后面类型提升「谁升级谁」的基础。

#### 4.1.3 源码精读

DType 类本身禁止用户直接构造（`__new__` 抛错），只能用预定义的单例；它的 `bitwidth`/`name` 属性用 `@function(host=True, tile=False)` 标注为「仅 host code 可用」：

[DType 类与禁止构造]([L26-L66](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_datatype.py#L26-L66))：定义 `DType`，`__new__` 抛 `TypeError`，`bitwidth`/`name` 是只读属性，`__call__` 是 stub（用于 `ct.int16(5)` 这种「用 dtype 当构造器生成 scalar」的用法）。

族的有序定义在 `NumericDTypeCategory`，其中 `arithmetic` 属性显式声明 RestrictedFloat **不算术**：

[族枚举与 arithmetic 标志]([L69-L91](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_datatype.py#L69-L91))：`Boolean=0, Integral=1, Float=2, RestrictedFloat=3`；`arithmetic` 对前三个返回 `True`，对 `RestrictedFloat` 返回 `False`。

具体的类型定义集中在一块，可以一眼看清全部类型及其族归属。注意受限浮点用 `NumericDTypeCategory.RestrictedFloat`：

[全部数值类型的定义]([L196-L246](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_datatype.py#L196-L246))：`bool_`、`uint8..uint64`、`int8..int64`、`float16/32/64`、`bfloat16`（均为 Float 族），以及 `tfloat32`、`float8_e4m3fn/e5m2/e8m0fnu`、`float4_e2m1fn`（均为 RestrictedFloat 族）。

> 各受限浮点的位域含义在 docstring 里有说明，例如 `tfloat32` 是「1 符号位 + 8 指数位 + 10 尾数位，19 位表示存于 32 位容器」，`float8_e4m3fn` 是「1 符号位 + 4 指数位 + 3 尾数位」。这些是 NVIDIA 硬件（张量核、微缩放格式 MX）专用的低精度格式。

「默认类型」在源码里被显式写出，理解它对预测 loosely typed 常量行为很重要：

[默认整数/浮点类型]([L249-L250](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_datatype.py#L249-L250))：`default_int_type = int32`、`default_float_type = float32`。这就是为什么一个普通整数字面量默认被当作 `int32`、浮点字面量默认被当作 `float32`。

一组查询函数（`is_integral`/`is_float`/`is_arithmetic` 等）统一通过 `_dtype_defs[t].numeric_category` 判断：

[is_arithmetic 等查询函数]([L260-L338](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_datatype.py#L260-L338))：`is_numeric`、`is_boolean`、`is_integral`、`is_float`、`is_restricted_float`、`is_arithmetic` 等，是后续提升逻辑反复调用的「分类原语」。

#### 4.1.4 代码实践

**实践目标**：亲手感受「dtype 是单例对象」以及族分类。

**操作步骤**（在装好 cuTile 的环境里运行）：

```python
import cuda.tile as ct

# 1) dtype 是对象，可比较、可传递
print(ct.int32 is ct.int32)          # True（单例）
print(ct.int32 == ct.int32)          # True
print(ct.int32.bitwidth)             # 32
print(ct.int32.name)                 # int32

# 2) 用 dtype 当构造器生成一个 strictly typed scalar
s = ct.int16(5)                      # 一个 dtype 为 int16 的标量

# 3) 族分类（这些查询函数在 host 也可用）
from cuda.tile._datatype import is_integral, is_float, is_restricted_float, is_arithmetic
print(is_integral(ct.int16))         # True
print(is_float(ct.bfloat16))         # True（bfloat16 属 Float 族）
print(is_restricted_float(ct.tfloat32))   # True
print(is_arithmetic(ct.tfloat32))    # False（受限浮点不算术！）
```

**需要观察的现象**：`ct.int32 is ct.int32` 为 `True` 说明是同一对象；`is_arithmetic(ct.tfloat32)` 为 `False` 印证了「受限浮点不能直接做四则运算」。

**预期结果**：如上注释所示。若你试图写 `ct.tfloat32(...) + ct.tfloat32(...)`，会在编译期报「隐式提升涉及 restricted float」的错误（详见 4.2）。

> 若未安装 cuTile 或无 GPU，以上为「待本地验证」；类型对象本身的属性（`bitwidth`/`name`/单例性）不依赖 GPU 运行，可在纯 host 侧验证。

#### 4.1.5 小练习与答案

**练习 1**：`bfloat16` 和 `float16` 都是 16 位浮点，为什么 `bfloat16 + float16` 在 cuTile 里需要显式 cast？

**参考答案**：二者虽同属 Float 族，但位域分配不同（`bfloat16` 是 1+8+7，`float16` 是 1+5+10），隐式合并没有唯一合理结果。查看 4.2 的提升表可知 `f16` 行与 `bf` 列的交叉格是 `na`（不允许），源码注释「Float16 and BFloat16 requires explicit type cast」。

**练习 2**：`ct.uint8` 和 `ct.int8` 都是 8 位整数，`uint8 + int8` 会得到什么？

**参考答案**：需要显式 cast。提升表里 `u8` 行与 `i8` 列交叉格是 `na`，源码注释「Signed and unsigned requires explicit type cast」——有符号与无符号混合必须显式转换。

---

### 4.2 promote_types：类型提升规则

#### 4.2.1 概念说明

**类型提升**回答的是：`x op y`（op 为 `+ - * /` 等）时，当 `x` 和 `y` 类型不同，结果用什么类型？

cuTile 的提升规则有一个核心区分：**loosely typed（松散类型）常量** vs **strictly typed（严格类型）操作数**。

- **loosely typed 常量**：Python 字面量（`2`、`3.0`）、`Constant[int]` 参数值、以及 `Tile.ndim`/`Tile.shape` 这类「编译期常量属性」。它们没有固定的 dtype，只在一个宽松的「值」上携带，运算时会「尽量迁就」对方。
- **strictly typed 操作数**：通过 `ct.int16(5)`、`ct.arange(...)`、`ct.load(...)` 得到的 tile/scalar，有明确的 dtype，不会随意改变。

提升规则可以浓缩成四条（与官方文档 [data.rst 的 Arithmetic Promotion 段]([L221-L249](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L221-L249)) 一致）：

1. **两个操作数都是 loosely typed 常量** → 结果仍是 loosely typed 常量（值在编译期算出，类型暂不固定）。例如 `5 + 7` 是 loosely typed 整数常量 `12`，`5 + 3.0` 是 loosely typed 浮点常量 `8.0`。
2. **至少一个不是 loosely typed 常量** → 先给 loosely typed 那个挑一个具体 dtype（整数常量按值大小挑 `int32`/`int64`/`uint64`，浮点常量挑 `float32`），然后做正式提升：
   - 跨族：`Boolean < Integral < Float`，**强族胜出**。
   - 同族且其中一个是 loosely typed 常量：**采用另一个（具体类型）操作数的 dtype**。
   - 同族且都不是 loosely typed：查提升表。

简而言之：**loosely typed 常量是「软」的，会迁就对方；strictly typed 是「硬」的，要按表合并。** 但有一道安全护栏：当 loosely typed 整数常量被吸收进一个更窄的具体整数类型时，会做**范围检查**，越界直接报错。

#### 4.2.2 核心流程

提升在运算（如 `add`）发生时执行，整体流程：

```text
            x op y
              │
   ┌──────────┴──────────┐
   │ 两者都是 loosely typed?│
   └──────────┬──────────┘
        是 ↓            否 ↓
  编译期直接算出值        promote_types(x_ty, y_ty)
  返回 loosely typed        │
  常量（无具体 dtype）   ┌───┴────────────────┐
                         │ 先把 loosely typed  │
                         │ 归类成具体 dtype    │ ← dtype_of_constant_scalar
                         │ (int32/int64/uint64/float32)
                         └───┬────────────────┘
                             │
                      promote_dtypes(d1, d2)
                             │
            ┌────────────────┼─────────────────┐
            │                 │                 │
       含受限浮点?        走 _DTypePromotionImpl     有符号/无符号混合、
       → 报错要求         ._common_dtype_table       f16/bf 混合 → 表里 na
       显式 cast          查表得到公共 dtype         → 报错要求显式 cast
```

loosely typed 常量被「归类成具体 dtype」的规则来自 `dtype_of_constant_scalar`：

\[ \text{dtype}(v) = \begin{cases} \text{bool\_} & v \text{ 是 bool} \\ \text{int32} & -2^{31} \le v < 2^{31} \\ \text{int64} & -2^{63} \le v < 2^{63} \\ \text{uint64} & 0 \le v < 2^{64} \\ \text{float32} & v \text{ 是 float} \end{cases} \]

也就是说，一个整数字面量会根据值的大小落到 `int32`/`int64`/`uint64` 之一，浮点字面量一律 `float32`。

#### 4.2.3 源码精读

`dtype_of_constant_scalar` 把 Python 字面量归类成具体 dtype，这是 loosely typed 常量的「隐式类型」来源：

[dtype_of_constant_scalar]([L147-L161](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/typing_support.py#L147-L161))：bool→`bool_`；int 按值大小落 `int32`/`int64`/`uint64`（超大则报错）；float→`default_float_type`（即 `float32`）。

`promote_types` 是运算时调用的入口，它同时处理「dtype 合并」和「shape 广播」：

[promote_types / promote_dtypes]([L270-L294](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops_utils.py#L270-L294))：用 `match` 分四种情形——两个 `LooselyTypedScalar`、左松右严、左严右松、两个具体 dtype，最终都汇入 `_DTypePromotionImpl.promote_dtypes`，并叠加 shape 广播。

「一个具体 dtype 与一个 loosely typed 常量」合并的核心逻辑，带有范围检查：

[_promote_dtype_and_loosely_typed_constant]([L243-L267](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops_utils.py#L243-L267))：同族时取具体操作数的 dtype，并对整数做 `IntegerInfo(dtype)` 范围校验（越界抛 `TileValueError`）；跨族时强族胜出。`force_float=True` 用于除法 `/`。

真正的「两个具体 dtype 查表」核心是 `_DTypePromotionImpl`：

[提升表 _common_dtype_table 与 promote_dtypes]([L397-L440](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_datatype.py#L397-L440))：`_order` 是行列索引顺序（`b1, u8, u16, ... f4e2m1fn`）；`_common_dtype_table[i][j]` 给出第 i 与第 j 个 dtype 的公共类型，`None`（`na`）表示禁止隐式提升。`promote_dtypes` 先特判「相同类型」「含受限浮点」「含指针」，再查表，`None` 则抛 `TileTypeError` 要求显式 cast。

为了便于阅读，下表摘录了**常用类型**之间的提升结果（行 ∩ 列，`ERR` = 需显式 cast；完整表见源码 [_common_dtype_table]([L400-L419](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_datatype.py#L400-L419))）：

| | **u8** | **u16** | **i8** | **i16** | **i32** | **i64** | **f16** | **f32** | **bf** |
|---|---|---|---|---|---|---|---|---|---|
| **u8** | u8 | u16 | ERR | ERR | ERR | ERR | f16 | f32 | bf |
| **i8** | ERR | ERR | i8 | i16 | i32 | i64 | f16 | f32 | bf |
| **i16** | ERR | ERR | i16 | i16 | **i32** | i64 | f16 | f32 | bf |
| **i32** | ERR | ERR | i32 | **i32** | i32 | i64 | f16 | f32 | bf |
| **f16** | f16 | f16 | f16 | f16 | f16 | f16 | f16 | f32 | ERR |
| **f32** | f32 | f32 | f32 | f32 | f32 | f32 | f32 | f32 | f32 |
| **bf** | bf | bf | bf | bf | bf | bf | ERR | f32 | bf |

从表里能直观看到三条规则：跨族整数→浮点统一成浮点；同族向更宽的提升（`i16+i32=i32`）；`f16` 与 `bf` 交叉为 ERR。

最后看「运算实现」如何把上述逻辑串起来。二元算术在提升前先短路「两个 loosely typed」情形：

[二元算术的 loosely typed 短路与提升]([L466-L482](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/arithmetic_ops.py#L466-L482))：若两操作数都是 `LooselyTypedScalar`，直接 `binop_propagate_constant` 在编译期算值并返回 loosely typed 常量（类型 `None`）；否则用 `promote_types(..., force_float=(fn=="truediv"))` 求公共类型再运算。除法 `truediv` 强制 `force_float=True`，所以 `int / int` 结果是浮点。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：推断三个表达式的结果类型，并用源码/文档验证。

请先**自己推断**以下三个表达式在 cuTile tile code 里的结果类型（不要急着看答案）：

```python
import cuda.tile as ct

e1 = ct.int16(5) + 2          # ?
e2 = ct.int16(5) + ct.int32(7)  # ?
e3 = 5 + 3.0                  # ?
```

**操作步骤**：

1. 对每个表达式，判断两个操作数是 loosely typed 还是 strictly typed。
2. 对 loosely typed 的，用 `dtype_of_constant_scalar` 推它的隐式 dtype。
3. 套用 4.2.1 的四条规则或查 4.2.3 的提升表。
4. 对照官方文档 [Arithmetic Promotion 段]([L221-L249](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L221-L249)) 验证。
5. 用真实测试 `test_loose_typing.py` 里 `combine_loose_and_strict_int` 加以佐证。

**推导与预期结果**：

- `e1 = ct.int16(5) + 2`：`ct.int16(5)` 是 **strictly typed int16**；`2` 是 loosely typed 常量，`dtype_of_constant_scalar(2)` = `int32`。两者同属 Integral 族、其一为 loosely typed → **采用另一个操作数的 dtype**，结果为 **`int16`**。（且 `2` 在 int16 范围内，范围检查通过。）
- `e2 = ct.int16(5) + ct.int32(7)`：两个都 strictly typed（`int16`、`int32`），同族 Integral、非 loosely typed → 查表，`i16` 行 `i32` 列 = **`int32`**。
- `e3 = 5 + 3.0`：两个都 loosely typed → **结果仍是 loosely typed 常量**，值在编译期算出为 `8.0`，因结果为浮点，故是 **loosely typed 浮点常量 `8.0`**（暂不绑定具体 dtype）。

**用测试佐证**：真实测试 [combine_loose_and_strict_int]([L157-L170](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_loose_typing.py#L157-L170)) 中 `t = n + ct.int16(2)`（`n` 为 loosely typed 的 `Constant[int]`）结果被断言为 int16；`b = a + t`（`a` 为 int8，`t` 为 strict int16）经 `ct.static_eval(b.dtype == ct.int16)` 断言为 int16——这与我们对 `e1`/`e2` 的推断逻辑完全一致。

**需要观察的现象**：如果把 `e1` 里的 `2` 换成 `100000`（超出 int16 范围 ±32767），会在编译期抛 `TileValueError: Integer constant 100000 is out of range of int16`（参见测试 [test_propagate_constant_int_then_promote_out_of_range]([L37-L40](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_loose_typing.py#L37-L40))）。这印证了范围护栏。

> 上述类型推断可在装好 cuTile 的环境里，把表达式写进一个 `@ct.kernel` 并用 `ct.static_assert(... .dtype == ct.xxx)` 编译期断言来验证；无 GPU/未安装时为「待本地验证」，但推断逻辑可纯靠源码完成。

#### 4.2.5 小练习与答案

**练习 1**：`ct.arange(4, dtype=ct.int8) + 0.5` 的结果类型是什么？

**参考答案**：`ct.arange(...)` 是 strictly typed `int8`；`0.5` 是 loosely typed 浮点常量（隐式 `float32`）。跨族 Integral < Float，强族胜出 → **`float32`**。这正是测试 [propagate_constant_float_then_promote]([L43-L63](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_loose_typing.py#L43-L63)) 里 `a - t` 被断言为 `ct.float32` 的原因。

**练习 2**：为什么 `ct.tfloat32(1.0) + ct.float32(2.0)` 会编译失败？

**参考答案**：`tfloat32` 是 RestrictedFloat。`promote_dtypes` 在 [_DTypePromotionImpl.promote_dtypes]([L421-L440](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_datatype.py#L421-L440)) 开头就检测到受限浮点并抛 `TileTypeError`，要求显式 cast（如 `ct.astype(x, ct.float32)`）。

**练习 3**：`7 / 2` 的结果类型是什么？为什么不是整数？

**参考答案**：两个 loosely typed 整数常量做 `/`（`truediv`），实现层 [arithmetic_ops.py]([L470-L471](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/arithmetic_ops.py#L470-L471)) 传 `force_float=True`，编译期算出 `3.5`，结果是 **loosely typed 浮点常量 `3.5`**。

---

### 4.3 RoundingMode 与 PaddingMode

#### 4.3.1 概念说明

前面两节讲的是「类型本身」，这一节讲「类型转换与边界时的行为」。两类模式都定义在 [_numeric_semantics.py]([L8-L56](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_numeric_semantics.py#L8-L56))，是两个独立的枚举。

**RoundingMode（舍入模式）** 用于浮点运算与类型转换：当目标精度无法精确表示结果时（如 `float32 → float16`，或 `int / int` 截断），按哪种策略取近似值。它出现在算术运算（`add`/`sum`/`exp` 等）和 `astype` 的参数里。

**PaddingMode（填充模式）** 专用于 `load`：当一个 tile 部分落在数组边界之外时，越界元素填什么值。它出现在 `ct.load`、`Array.tiled_view`、`load_advanced_indexing` 的参数里。

二者最大的区别：**RoundingMode 影响「数值精度」，PaddingMode 影响「越界元素的值」**。一个是算出来的值怎么舍，一个是读不到的元素填什么。

#### 4.3.2 核心流程

**RoundingMode 的典型链路**（以 `sum` 为例）：

```text
ct.sum(x, axis=..., rounding_mode=ct.RoundingMode.RZ)
        │  rounding_mode 透传到 IR 的 reduce 操作
        ▼
   生成带 rounding_mode 属性的 Reduce Op
        │  ir2bytecode 时 rounding_mode_to_bytecode() 编码
        ▼
   最终影响硬件累加/截断行为
```

**PaddingMode 的典型链路**（以 `load` 为例）：

```text
ct.load(array, index=(i,), shape=(4,), padding_mode=ct.PaddingMode.ZERO)
        │  若 tile 越界，越界元素按 ZERO 填充
        ▼
   生成带 padding_mode 的 Load Op
        ▼
   后端可据此选用带边界处理的访存指令（或 TMA）
```

舍入模式的数学含义（IEEE 754 视角）：把一个实数 \(r\) 映射到目标类型可表示的最近值。四种主要舍入：

\[ \text{RN: 就近向偶} \quad \text{RZ: 向零} \quad \text{RM: 向} -\infty \quad \text{RP: 向} +\infty \]

例如把 `1.5` 转成整数：RN 得 2（偶）、RZ 得 1、RM 得 1、RP 得 2。

#### 4.3.3 源码精读

`RoundingMode` 定义了 7 个值，前四个是标准 IEEE 754 舍入，后三个是 cuTile/硬件特有：

[RoundingMode 枚举]([L8-L32](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_numeric_semantics.py#L8-L32))：`RN`（就近向偶，默认）、`RZ`（向零/截断）、`RM`（向负无穷）、`RP`（向正无穷）、`FULL`（全精度，仅 f32）、`APPROX`（近似，仅 f32）、`RZI`（就近整数再向零）。

`PaddingMode` 定义了 6 个值，对应越界元素的不同填充值：

[PaddingMode 枚举]([L35-L56](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_numeric_semantics.py#L35-L56))：`UNDETERMINED`（未定，默认）、`ZERO`（填 0）、`NEG_ZERO`（填 -0.0）、`NAN`（填 NaN）、`POS_INF`（填 +∞）、`NEG_INF`（填 −∞）。

`PaddingMode` 真正被使用的位置——`ct.load` 的签名与文档：

[load 的 padding_mode 参数]([L1218-L1245](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L1218-L1245))：`padding_mode` 默认 `UNDETERMINED`；对部分越界的 tile，按该模式填充越界元素；若 tile 整体在数组外则行为未定义。文档示例 `ct.load(x, (2,), shape=4, padding_mode=zero_pad)`。

`RoundingMode` 在算术运算上的用法——`add`/`sum` 等都接受可选的 `rounding_mode`：

[add 的 rounding_mode 参数]([L3122-L3126](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L3122-L3126))：`add(x, y, /, *, rounding_mode=None, flush_to_zero=False)`，默认 `None`（即适用的 RN）。`flush_to_zero` 是另一个相关的数值选项（FTZ，把非规格化数刷成零）。

官方文档对这两个枚举的说明（自动从 docstring 生成）：

[文档：Rounding Modes / Padding Modes]([L256-L274](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L256-L274))：用 `autoclass` 直接展示 `RoundingMode` 与 `PaddingMode` 的成员说明。

#### 4.3.4 代码实践

**实践目标**：体会 PaddingMode 在「tile 越界」时的作用，并理解默认 `UNDETERMINED` 的含义。

**操作步骤**（改编自 [_stub.py 中 load 的官方示例]([L1286-L1297](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L1286-L1297))）：

```python
import torch
import cuda.tile as ct

@ct.kernel
def k(x, out):
    # x 长度 10；从 index 2 起 load 一个长度 4 的 tile 不会越界
    t1 = ct.load(x, (2,), shape=4)                       # [2,3,4,5]
    # 从 index 8 起 load 长度 4 的 tile 会越界 2 个元素
    t2 = ct.load(x, (8,), shape=4, padding_mode=ct.PaddingMode.ZERO)  # [8,9,0,0]
    ct.store(out, (0,), t1)
    ct.store(out, (4,), t2)

x = torch.arange(10, device="cuda", dtype=torch.int32)
out = torch.zeros((8,), device="cuda", dtype=torch.int32)
ct.launch(torch.cuda.current_stream(), (1,), k, (x, out))
print(out.tolist())   # 预期 [2,3,4,5, 8,9,0,0]
```

**需要观察的现象**：
- `t2` 从下标 8 开始取 4 个元素，但数组只有 10 个（下标 0..9），下标 10、11 越界。设 `padding_mode=ZERO` 后，这两个位置填 `0`。
- 若不传 `padding_mode`（默认 `UNDETERMINED`），越界元素的值是不确定的（编译器不保证），所以**只要 tile 可能越界，就应显式指定 PaddingMode**。

**预期结果**：`[2, 3, 4, 5, 8, 9, 0, 0]`。

> 本实践需要 GPU 与 cuTile 运行环境；无环境时为「待本地验证」，但 PaddingMode 的语义可纯靠文档与源码理解。

#### 4.3.5 小练习与答案

**练习 1**：`PaddingMode.UNDETERMINED` 是「填未确定值」，为什么默认值偏偏选它而不是 `ZERO`？

**参考答案**：性能。当编译器能证明 tile 不越界时（常见于对齐的 persistent 内核），无需任何边界处理，访存可以降级到最快的硬件路径（如 TMA）。`UNDETERMINED` 把「是否需要边界处理」的决定权交给编译器；只有真的可能越界时用户才显式指定 `ZERO` 等。`ZERO` 会强制生成边界填充逻辑，可能更慢。

**练习 2**：`RoundingMode.RZ` 和 `RoundingMode.RN` 在把 `0.5`、`1.5`、`2.5` 转成整数时分别得多少？

**参考答案**：
- `RZ`（向零）：三者都得 `0`、`1`、`2`（直接截断小数部分）。
- `RN`（就近向偶）：`0.5→0`、`1.5→2`、`2.5→2`（.5 时取偶数）。

**练习 3**：`astype(x, ct.float16)` 这种把高精度转低精度的操作，是否受 `RoundingMode` 影响？

**参考答案**：是。`astype` 内部会走 [_ir/arithmetic_ops.py 的 astype]([L160-L161](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/arithmetic_ops.py#L160-L161))，默认按 RN 就近向偶舍入；如需截断可显式指定 `rounding_mode`。这正是 `float32 → float16` 时精度损失的处理方式。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**写一个「带边界安全 + 类型混用」的 row-wise 缩放内核**，并预测每一处的类型与边界行为。

要求：
1. 输入 `x` 是 `float32` 的 `(M, N)` 矩阵，输出 `y` 同形。
2. 内核对每个 tile 做 `y = x * alpha + beta`，其中 `alpha` 是 loosely typed 浮点常量（如 `0.5`），`beta` 是 strictly typed 的 `ct.float16(0.1)`。
3. 当 `N` 不是 tile 宽度的整数倍时，用 `PaddingMode.ZERO` 处理越界。
4. 用 `static_assert` 断言中间结果的 dtype 符合你的预测。

**思考要点**（先自己回答，再写代码验证）：
- `x`（strict float32）`* alpha`（loose float）→ 结果 dtype？（答：float32，强族/同族吸收 loosely typed）
- 上一结果 `+ beta`（strict float16）→ 结果 dtype？（答：查表 `f32` 行 `f16`... 注意 `f16` 与 `f32` 提升结果是 `f32`，见 4.2.3 表格；但 `beta` 是 float16，`f32 + f16 = f32`）
- 越界 tile 需要哪种 `padding_mode`？（答：`ZERO`，且应明确指定）

参考骨架（**示例代码**，非项目原有文件）：

```python
import torch
import cuda.tile as ct

TM, TN = 4, 8   # tile 形状（每维须为 2 的幂，见 u2-l3）

@ct.kernel
def scale_kernel(x, y, M: ct.Constant[int], N: ct.Constant[int]):
    i = ct.bid(0)
    base = ct.load(x, (i * TM, 0), shape=(TM, TN),
                   padding_mode=ct.PaddingMode.ZERO)   # 越界填 0
    alpha = 0.5                                        # loosely typed 浮点常量
    beta = ct.float16(0.1)                             # strictly typed float16
    r = base * alpha                                   # float32（loose 被吸收）
    ct.static_assert(r.dtype == ct.float32)            # 编译期断言
    out = r + beta                                     # float32（f32 + f16 → f32）
    ct.static_assert(out.dtype == ct.float32)
    ct.store(y, (i * TM, 0), out)

# host 侧启动（M、N 自行选，故意让 N 不是 TN 的整数倍以触发 padding）
# 待本地验证：构造 x、y 张量，计算 grid = (ct.cdiv(M, TM),)，launch 后与参考值比对。
```

> 这个综合实践把「DType 分类（float16/float32）」「loosely vs strictly 提升」「PaddingMode 边界处理」三者用到同一内核里。若 `N` 不是 `TN` 整数倍而又不指定 `padding_mode`，越界元素为 `UNDETERMINED`，结果会出错——这正是 PaddingMode 存在的意义。

## 6. 本讲小结

- cuTile 的 **DType 是不可变单例对象**，数值类型分四族：Boolean、Integral、Float（含 `bfloat16`）、**RestrictedFloat**（`tfloat32`/`float8_*`），其中 RestrictedFloat **不算术**，禁止隐式提升、必须显式 cast。
- 类型提升的核心区分是 **loosely typed 常量**（字面量、`Constant` 值、常量属性，会迁就对方）与 **strictly typed 操作数**（有确定 dtype）。两个 loose 常量运算结果仍是 loose 常量；否则按「跨族强族胜、同族查表」求公共类型。
- 整数 loosely typed 常量按值大小隐式归为 `int32`/`int64`/`uint64`，浮点归为 `float32`；被吸收进更窄的整数类型时有**范围检查**，越界报 `TileValueError`。
- 提升表 [_common_dtype_table]([L400-L419](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_datatype.py#L400-L419)) 里 `na`/`ERR` 的格子（如 `f16`∩`bf`、有符号∩无符号）都要求显式 cast。
- `RoundingMode` 控制**浮点运算/类型转换的舍入**（默认 `RN` 就近向偶），`PaddingMode` 控制 **`load` 越界元素的填充**（默认 `UNDETERMINED`，越界场景应显式指定 `ZERO` 等）。
- 推断类型时，真实测试 [test_loose_typing.py]([L1-L188](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_loose_typing.py)) 是最好的「可运行依据」，里面的 `static_assert`/`static_eval` 直接断言了结果 dtype。

## 7. 下一步学习建议

至此，U2「执行模型与数据模型」全部完成——你已经掌握了 grid/block、array、tile/scalar/广播、dtype 与提升这套**用户侧数据语义**。接下来两条路：

- **想先会「写内核」**：进入 U3《编写内核：用户 API 实战》。本讲的类型提升知识会在 u3-l1（load/store）、u3-l2（tile 算术与 `astype`）、u3-l4（归约，用到 `RoundingMode`）里被反复用到；u3-l6（matmul/张量核）会大量使用本讲的 **RestrictedFloat**（`tfloat32`/`float8_*`）与 `_mma_supported_dtypes`。
- **想深入「类型如何在 IR 里表示」**：可在进入 U5 后阅读 [_ir/type.py]([L87-L94](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/type.py#L87-L94)) 的 `LooselyTypedScalar`/`TileTy`/`ArrayTy`，看 loosely typed 常量与具体 dtype 在 IR 层是如何统一表达的。

建议优先走 U3，把「写什么」彻底熟练后，再回头攻 U5 的「怎么编译」。
