# 类型系统：TileTy、ArrayTy、ListTy 与 TypingHooks

## 1. 本讲目标

本讲聚焦 Tile IR 的**类型系统**（`src/cuda/tile/_ir/type.py`）。学完后你应当能够：

- 说清 `Type` 这个根类如何用一套统一的「聚合契约」（`is_aggregate` / `aggregate_item_types` / `flatten_aggregate` / `make_aggregate_value`）表达从标量到嵌套元组的所有类型。
- 掌握 `TileTy`（flyweight 缓存）、`ArrayTy`（`base_ptr + shape + strides`，shape 现可携带编译期常量维度）、`ListTy`、`TupleTy` 各自的结构与展开规则。
- 理解一个 `List[Array]` 这样的聚合参数如何被 `flatten_block_parameters` 展开成一串扁平的、可序列化的 `Var`，作为内核 Block 的入口参数。
- 理解 `TypingHooks.get_tensor_like_type` 为何是前端的扩展点，以及它如何让 IR 与具体的「张量表示」解耦。

本讲是 u5-l5（IR 核心：IRContext/Builder/Block/Var/Operation）的直接续篇——上一讲讲了「值与操作怎么组装」，本讲回答「这些值到底带什么类型」。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**第一，Tile IR 是 SSA 风格的。** 每个 `Var` 只被赋值一次，它的类型由 `IRContext.typemap` 集中记录（见 u5-l5）。类型不是 Python 的 `type`，而是本讲的 `Type` 对象——一个自成体系的、不可变的类型层级。

**第二，内核参数最终必须是「可序列化的扁平值」。** Tile IR 会被编码成字节码（u7-l2），再由 `tileiras` 编译。字节码的函数签名只能接受「朴素的标量/指针」参数。但用户在 Python 里写的内核参数可能是 `Array`、`tuple[Tensor, Tensor]`、`List[Array]` 这样的**聚合对象**。于是存在一个根本矛盾：

> 用户的聚合参数 ⟷ 字节码的扁平签名

本讲的核心任务就是讲解 IR 类型系统如何用「聚合 + 展平」机制化解这个矛盾。`Array` 会被拆成 `(base_ptr, *shape, *strides)`，`tuple` 会被拆成它的各个元素，`List` 会被拆成 `(base_ptr, length)`——全部落到标量/指针 `Var`，再把原聚合对象「按需重建」回来。

下面两个数学事实会反复出现。一个秩为 \(n\) 的数组展开后的元素个数为：

\[
\text{flat\_count}(\text{Array}) = 1 + 2n \quad (\text{一个 base\_ptr} + n\text{ 个 shape} + n\text{ 个 strides})
\]

一个聚合类型 `T` 递归展开到叶子的总数，由 `flatten_aggregate` 给出：若 `T` 非聚合则为 1，否则为各子类型计数之和。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/cuda/tile/_ir/type.py` | 类型层级的主战场：`Type` 根类、`TensorLikeTy`、`TileTy`、`ArrayTy`、`ListTy`、`TupleTy` 及各种 View 类型。 |
| `src/cuda/tile/_ir/aggregate_support.py` | 聚合展开/重建的工具集：`flatten_block_parameters`、`expand_aggregate_var`、`unflatten_aggregates` 等。 |
| `src/cuda/tile/_ir/ir.py` | 提供 `TypingHooks` 抽象、`IRContext`（持有 `typing_hooks`）、`Var.flatten_aggregate`、`Builder.make_aggregate`、`Block.params`。 |
| `src/cuda/tile/_compile.py` | 类型系统的「主顾」：`_get_array_ty`（构造 `ArrayTy`，含静态形状）、`_TileTypingHooks`（`TypingHooks` 的标准实现）、`_create_parameter`（调用 `flatten_block_parameters`）。 |

阅读建议：先看 `type.py` 里 `Type` 根类的四个聚合方法，那是全篇的「语法规则」；再看 `aggregate_support.py` 的 `flatten_block_parameters` 看「规则如何被驱动」；最后回 `ir.py` 看 `TypingHooks` 如何把这套系统与前端解耦。

## 4. 核心概念与源码讲解

### 4.1 Type 类型层级与聚合契约

#### 4.1.1 概念说明

Tile IR 的所有类型都继承自一个根类 `Type`。它不是 Python 内置的 `type`，而是一个独立、不可变、自描述的层级。整个层级里最关键的不是某个具体类型，而是 `Type` 定义的**聚合契约（aggregate contract）**——一套四个方法，用统一的方式描述「一个类型是不是由多个子值组成、怎么拆、怎么装回去」。

这套契约之所以重要，是因为它把「数组、元组、列表」这些概念上千差万别的聚合，归一成同一种可递归处理的结构。无论你是把一个 `Array` 拆成指针和尺寸，还是把一个 `tuple[A, B]` 拆成两个元素，调用的是同一组方法。

层级里还有一个中间基类 `TensorLikeTy`，它代表「长得像张量、有 dtype 和 shape」的类型（tile、loosely typed scalar 等），提供 `tensor_dtype()` 和 `tensor_shape()` 两个查询接口。

#### 4.1.2 核心流程

聚合契约的四个方法构成一个递归协议：

```text
is_aggregate()            → 这个类型是不是由多个子值组成？（默认 False）
aggregate_item_types()    → 若是聚合，它的「直接子类型」有哪些？
flatten_aggregate()       → 递归展平到全部叶子（非聚合）类型
make_aggregate_value()    → 给定一组建好的子 Var，把它们「装回」一个聚合值
```

`flatten_aggregate` 的递归逻辑很简洁：聚合则对各子类型继续递归，非聚合则 yield 自己。

#### 4.1.3 源码精读

[`Type` 根类定义了聚合契约的默认行为](src/cuda/tile/_ir/type.py#L35-L62)：`is_aggregate` 默认返回 `False`，`flatten_aggregate` 在非聚合时 yield 自身、聚合时递归各子类型。

```python
# src/cuda/tile/_ir/type.py
class Type:
    def is_aggregate(self) -> bool:
        return False
    def aggregate_item_types(self) -> tuple["Type", ...]:
        raise NotImplementedError()
    def flatten_aggregate(self) -> Iterator["Type"]:
        if self.is_aggregate():
            for ty in self.aggregate_item_types():
                yield from ty.flatten_aggregate()
        else:
            yield self
    def make_aggregate_value(self, items: tuple["Var", ...]) -> "AggregateValue":
        raise NotImplementedError()
```

[`TensorLikeTy` 是所有「张量样」类型的基类](src/cuda/tile/_ir/type.py#L76-L84)，只承诺两个查询接口，具体类型（如 `TileTy`）再去实现：

```python
class TensorLikeTy(Type):
    """Base class for all tensor-like types, e.g. tiles, loosely typed scalars, etc."""
    def tensor_dtype(self) -> "DType": raise NotImplementedError()
    def tensor_shape(self) -> tuple[int, ...]: raise NotImplementedError()
```

> 这四个方法 + `TensorLikeTy` 就是本讲全部具体类型的「宪法」。后面每个类型（`TileTy`/`ArrayTy`/`ListTy`/`TupleTy`）都只是在回答「我是否聚合、我的子类型是谁、怎么装回来」。

#### 4.1.4 代码实践

**实践目标**：用源码确认「叶子类型走 `flatten_aggregate` 的 yield 自身分支」。

**操作步骤**：

1. 打开 [`src/cuda/tile/_ir/type.py`](src/cuda/tile/_ir/type.py#L45-L50)，定位 `flatten_aggregate`。
2. 心智模拟：对一个非聚合类型（如 `NoneType`），`is_aggregate()` 返回 `False`，于是进入 `else` 分支 yield 自己，迭代器只产出一个元素。
3. 再对一个聚合类型（如 `TupleTy([NoneType, NoneType])`）：`is_aggregate()` 为 `True`，遍历两个 `NoneType` 子类型，各自再 yield 自己，总共产出两个元素。

**预期结果**：`list(TupleTy([NONE, NONE]).flatten_aggregate())` 长度为 2，且元素都是 `NONE`。

**待本地验证**：可在一个内核里用 dump IR（`CUDA_TILE_DUMP_TILEIR`）观察实际类型字符串，确认叶子类型确实不被进一步拆分。

#### 4.1.5 小练习与答案

**练习 1**：`Type.__eq__` 和 `__hash__` 在基类里都 `raise NotImplementedError()`，为什么不是默认按对象身份比较？

**答案**：因为类型系统要求「结构相等」——两个 `TileTy(float32, (16,16))` 应当被视为同一个类型（甚至通过 flyweight 复用同一对象）。把比较下放到具体子类，是为了让每个类型用自己的结构字段定义相等性（见 4.2 的 `TileTy.__eq__`）。

**练习 2**：`make_aggregate_value` 接收的是 `tuple[Var, ...]`（值），而 `aggregate_item_types` 返回的是 `tuple[Type, ...]`（类型）。这二者长度应当满足什么关系？

**答案**：严格相等。`aggregate_item_types()` 给出直接子类型，`make_aggregate_value(items)` 用同等数量的子 `Var` 装回聚合值——这是 `_unflatten_proper_aggregate` 里 `zip(..., strict=True)` 能成立的前提。

---

### 4.2 TileTy：flyweight 张量类型

#### 4.2.1 概念说明

`TileTy` 描述内核内部一个 **tile**（瓦片）的类型——即 `ct.load` 搬进来的那块不可变数据。它由 `dtype`（元素类型）和 `shape`（编译期常量形状，每维为 2 的幂）两个属性唯一决定。

`TileTy` 最值得关注的设计是 **flyweight（享元）模式**：相同的 `(dtype, shape)` 组合永远返回同一个对象。这不仅省内存，更让「类型相等」可以直接退化成对象身份比较之外的稳妥结构比较，也让 `TileTy` 可以放心地作为字典 key、用作缓存键。

#### 4.2.2 核心流程

`TileTy` 不是用 `__init__` 构造，而是用 `__new__` 拦截创建：

```text
TileTy(dtype, shape)
  → 查 _tile_ty_cache[(dtype, shape)]
  → 命中：直接返回缓存对象（不新建）
  → 未命中：object.__new__ 新建，填字段，存入缓存，返回
```

于是全进程范围内，`TileTy(float32, (16,16)) is TileTy(float32, (16,16))` 成立（同一对象）。

#### 4.2.3 源码精读

[`TileTy` 通过 `__new__` + 模块级字典 `_tile_ty_cache` 实现 flyweight](src/cuda/tile/_ir/type.py#L427-L440)：

```python
# src/cuda/tile/_ir/type.py
class TileTy(TensorLikeTy):
    def __new__(cls, dtype: "DType", shape: Sequence[int] = ()) -> "TileTy":
        shape = tuple(shape)
        try:
            return _tile_ty_cache[(dtype, shape)]
        except KeyError:
            pass
        assert isinstance(dtype, DType)
        ret = object.__new__(cls)
        ret.dtype = dtype
        ret.shape = shape
        _tile_ty_cache[(dtype, shape)] = ret
        return ret
```

缓存表本身是一个模块级字典 [`_tile_ty_cache`](src/cuda/tile/_ir/type.py#L478)，以 `(dtype, shape)` 为键。

`TileTy` 还实现了自己的结构相等与哈希 [`__eq__` / `__hash__`](src/cuda/tile/_ir/type.py#L462-L468)，比较 `dtype` 与 `shape`——即使 flyweight 因某种原因失效（例如不同进程），结构比较仍能给出正确结果。

注意：`TileTy` **不是聚合**（没有覆盖 `is_aggregate`，继承默认的 `False`）。它本身就是叶子——一个 tile 是一个完整值，不再拆分。这与 `ArrayTy` 形成鲜明对比（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：体会 flyweight 带来的对象复用。

**操作步骤**：这是「源码阅读型实践」，不需要真跑 GPU。

1. 在 `_ir/type.py` 的 `TileTy.__new__` 处确认：第一次构造 `(float32, (16,16))` 会 miss、新建并存入缓存；第二次同样的参数会 hit、直接返回。
2. 对比 `ArrayTy`：它没有 flyweight，每次 `ArrayTy(...)` 都新建对象，靠 `__eq__` 做结构比较。思考为何 `ArrayTy` 不适合 flyweight（提示：它的 shape/strides 含运行时 `None`，组合空间太大）。

**预期结果**：理解「形状空间小且编译期完全已知」的类型（`TileTy`）适合 flyweight，而「含运行时维度」的类型（`ArrayTy`）不适合。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TileTy` 用 `__new__` 而不是 `__init__` 来实现缓存？

**答案**：`__init__` 在 `__new__` 之后总会被 Python 自动调用，无法阻止重复初始化。用 `__new__` 拦截，可以在缓存命中时直接返回已有对象、根本不进入新建流程，从而彻底避免重复构造。

**练习 2**：`TileTy` 没有覆盖 `is_aggregate`，那 `TileTy(...).flatten_aggregate()` 返回什么？

**答案**：返回只有一个元素（它自己）的迭代器。因为继承的 `is_aggregate()` 为 `False`，`flatten_aggregate` 走 `else` 分支 `yield self`。这正是「叶子类型」的标志。

---

### 4.3 ArrayTy：base_ptr + shape + strides（含静态形状）

#### 4.3.1 概念说明

`ArrayTy` 描述用户传入内核的**全局数组**（即 `ct.Array`）在 IR 中的类型。它和 `TileTy` 有一个根本区别：**`ArrayTy` 是聚合类型**。源码注释解释了原因——数组虽然在底层用 TensorViews 表示，但**不能跨控制流传递**，因此必须能被拆解成 `(base_ptr, *shape, *strides)` 一串基本值，像普通标量一样参与循环携带、phi 合并。

`ArrayTy` 的关键字段：

- `dtype`：元素类型（编译期常量）。
- `shape`：`Tuple[Optional[int], ...]`，**每一维要么是 `None`（运行时动态），要么是一个 `int`（编译期静态常量）**。这正是 u3-l7 引入的「静态形状特化」落在类型系统里的样子。
- `strides`：同样允许 `None`（动态）或 `int`（已特化的常量步长）。
- `index_dtype`：shape/strides 值的整数类型，默认 `int32`。
- `memory_space`：显存空间，默认 `GENERIC`。
- `typing_hooks`：持有的钩子，用于决定子值的张量类型（见 4.6）。

#### 4.3.2 核心流程

`ArrayTy` 的展开规则：一个秩为 \(n\) 的数组，按 `(base_ptr, dim0_size, dim1_size, ..., dim0_stride, dim1_stride, ...)` 的顺序展开为 \(1 + 2n\) 个叶子值：

\[
\text{aggregate\_item\_types}(\text{ArrayTy}) = (\text{base\_ptr\_ty},\ \underbrace{\text{size\_ty}, \ldots, \text{size\_ty}}_{n},\ \underbrace{\text{size\_ty}, \ldots, \text{size\_ty}}_{n})
\]

其中 `base_ptr_ty` 是「指向 `dtype` 的指针」对应的张量类型，`size_ty` 是 `index_dtype`（默认 int32）的零维张量类型。注意这些子类型本身都通过 `typing_hooks.get_tensor_like_type` 获得——这是 `TypingHooks` 最密集的使用点。

**静态形状如何进入 `ArrayTy.shape`**：在 `_compile.py` 的 `_get_array_ty` 里，先用 `_resolve_static_shape_axes` 把用户写的 `ArrayAnnotation(static_shape_dims=(0,-1))`（支持负索引）归一化成一个长度为 `ndim` 的布尔掩码，再逐维度决定：被标注为静态的维度，从 `ArrayConstraint.shape_constant` 取出编译期常量填入；其余维度填 `None`。

#### 4.3.3 源码精读

[`ArrayTy.__init__`](src/cuda/tile/_ir/type.py#L524-L540) 接收 `dtype`、`shape`（含 `None` 表示动态）、`strides`、`typing_hooks` 等，`shape` 字段允许 `Optional[int]`：

```python
# src/cuda/tile/_ir/type.py
class ArrayTy(Type):
    def __init__(self, dtype, /, shape, strides, typing_hooks,
                 index_dtype=None, memory_space=MemorySpace.GENERIC):
        ...
        self.dtype = dtype
        self.shape = shape              # Tuple[Optional[int], ...]
        self.strides = strides          # Tuple[Optional[int], ...]
        self.index_dtype = int32 if index_dtype is None else index_dtype
        self.memory_space = memory_space
        self.typing_hooks = typing_hooks
```

[`ArrayTy.is_aggregate`](src/cuda/tile/_ir/type.py#L545-L549) 返回 `True`，注释点明了「数组不能跨控制流传递，必须能拆成 base_ptr+shape+strides」的根本原因：

```python
    def is_aggregate(self) -> bool:
        # Even though arrays are actually represented with TensorViews, they can't be
        # propagated through control flow. So we need to be able to unpack the array
        # into its individual (base_ptr, *shape, *strides) values.
        return True
```

[`ArrayTy.aggregate_item_types`](src/cuda/tile/_ir/type.py#L551-L556) 用 `typing_hooks` 决定 base_ptr 与各 size 的张量类型，产出 \(1+2n\) 个子类型：

```python
    def aggregate_item_types(self) -> tuple["Type", ...]:
        from .._datatype import pointer_dtype
        base_ptr_ty = pointer_dtype(self.dtype, self.memory_space)
        base_ptr_tile_ty = self.typing_hooks.get_tensor_like_type(base_ptr_ty, ())
        size_ty = self.typing_hooks.get_tensor_like_type(self.index_dtype, ())
        return (base_ptr_tile_ty,) + (size_ty,) * (self.ndim * 2)
```

静态形状的真正「入口」在 `_compile.py`：[`_resolve_static_shape_axes`](src/cuda/tile/_compile.py#L244-L257) 把 `static_shape_dims`（含负索引）归一为布尔掩码并校验越界/重复：

```python
# src/cuda/tile/_compile.py
def _resolve_static_shape_axes(array_ann, ndim, path) -> list[bool]:
    static_shape_mask = [False] * ndim
    for axis in array_ann.static_shape_dims:
        if not -ndim <= axis < ndim:
            raise _make_constraint_error(...)            # 越界
        normalized = axis + ndim if axis < 0 else axis   # 负索引归一
        if static_shape_mask[normalized]:
            raise _make_constraint_error(...)            # 重复
        static_shape_mask[normalized] = True
    return static_shape_mask
```

随后 [`_get_array_ty`](src/cuda/tile/_compile.py#L260-L294) 用这个掩码逐维决定 `shape[i]`：静态维度填入 `param.shape_constant` 的常量值，其余填 `None`：

```python
    static_shape_mask = _resolve_static_shape_axes(array_ann, param.ndim, path)
    array_ty_shape = []
    for axis, (annotated_as_static, constraint_size) in enumerate(
            zip(static_shape_mask, param.shape_constant, strict=True)):
        if annotated_as_static:
            if constraint_size is None:
                raise _make_constraint_error(...)        # 标注静态却无常量值
            array_ty_shape.append(constraint_size)       # 编译期常量进入 shape
        else:
            array_ty_shape.append(None)                  # 动态维度
    return ArrayTy(param.dtype, shape=tuple(array_ty_shape), ...)
```

> 关键洞见：静态形状不是新增字段，而是**复用了 `shape` 字段的「`None` vs `int`」二义性**。一个维度要么 `None`（运行时由 launch 参数填），要么是一个具体的 `int`（编译期烘焙死，进入 IR 类型本身，因而能被 divby/对齐优化使用，见 u6-l2）。`ArrayTy.__str__` 也据此把 `None` 渲染成 `?`、把常量渲染成数字（[`type.py:578-586`](src/cuda/tile/_ir/type.py#L578-L586)）。

#### 4.3.4 代码实践

**实践目标**：亲手算出一个二维静态形状数组的展开数与 shape 字段。

**操作步骤**：

1. 假设用户写 `Annotated[ct.Array, ct.ArrayAnnotation(static_shape_dims=(0,))]`，运行时该维取值 4、另一维运行时为 8。
2. 推断 `_resolve_static_shape_axes` 产出 `mask = [True, False]`。
3. 推断 `_get_array_ty` 产出 `shape = (4, None)`。
4. 算展开数：`1 + 2*ndim = 1 + 2*2 = 5`，即 `(base_ptr, size0, size1, stride0, stride1)`。

**需要观察的现象**：dump 出的 IR 里，该数组参数的类型字符串应形如 `Array[<dtype>,(4,?):(?,?)...]`——第 0 维是数字 `4`（静态），第 1 维是 `?`（动态）。

**预期结果**：理解「静态形状 = `shape` 字段里的 `int`」，且静态维度会进入 JIT 缓存键（不同取值编译不同 cubin）。

**待本地验证**：用 `CUDA_TILE_DUMP_TILEIR` 跑一个带 `static_shape_dims` 的内核，对照类型字符串确认。

#### 4.3.5 小练习与答案

**练习 1**：一个三维数组，其中第 0 维和第 2 维被特化为静态，展开后有几个叶子值？

**答案**：`1 + 2*3 = 7` 个。静态形状只改变 `shape` 字段里某些维度从 `None` 变成 `int`，**不改变展开数**（展开数只取决于 `ndim`）。

**练习 2**：为什么 `static_shape_dims=(0, -3)` 在三维数组里会被判为重复？

**答案**：`0` 归一化为 `0`，`-3` 归一化为 `-3 + 3 = 0`，两者落到同一位置，触发 `static_shape_mask[normalized]` 为真的重复检查，报 "Axis appears more than once"。

---

### 4.4 ListTy 与 TupleTy：两类聚合参数

#### 4.4.1 概念说明

除了数组，cuTile 还支持两类聚合参数：**元组**（`tuple`）和**列表**（`List[Array]`）。它们在 IR 类型层分别由 `TupleTy` 和 `ListTy` 表达，但二者的「聚合形态」很不一样。

- **`TupleTy`：异构定长元组。** 它持有一个 `value_types` 序列，每个位置可以是不同类型（如 `[ArrayTy, TileTy(int32)]`）。`is_aggregate` 为真，直接子类型就是它的各元素类型。无论用户写的是定长 `tuple[A, B]` 还是变长 `tuple[T, ...]`，物化到 IR 后都是一个 `TupleTy`（变长 vs 定长的区分留在注解/约束层，见 u3-l7、u5-l1）。

- **`ListTy`：同质变长列表。** 它的 `item_type` 字段记录元素类型（如 `ArrayTy`），但**运行时表示不是「一串元素」，而是 `(base_ptr, length)`**——`base_ptr` 指向一段连续的 `int64` 缓冲（每个 int64 是一个元素的指针/句柄），`length` 是元素个数。换言之，List 在 IR 里被编译成一个「指针 + 长度」的对，元素本身并不内联。

#### 4.4.2 核心流程

二者的展开规则：

- `TupleTy([t0, t1, ...])`：`aggregate_item_types()` 直接返回 `(t0, t1, ...)`，再由 `flatten_aggregate` 递归展平。
- `ListTy(item_type)`：`aggregate_item_types()` 返回 `(TileTy(ptr<int64>), TileTy(int32))`——只有两项，与 `item_type` 是什么无关。`item_type` 仅用于类型检查与元素访问的语义，不参与运行时展开。

```text
TupleTy(t0, t1)  →  flatten →  leaves of t0 ++ leaves of t1
ListTy(item_ty)  →  flatten →  (ptr<int64>_tile, int32_tile)   # 永远 2 个叶子
```

#### 4.4.3 源码精读

[`TupleTy`](src/cuda/tile/_ir/type.py#L217-L233) 持有 `_value_types`，是聚合类型，子类型即各元素类型，`make_aggregate_value` 装回 `TupleValue`：

```python
# src/cuda/tile/_ir/type.py
class TupleTy(Type):
    def __init__(self, value_types: Sequence[Type]):
        self._value_types = tuple(value_types)
    def is_aggregate(self) -> bool:
        return True
    def aggregate_item_types(self) -> tuple["Type", ...]:
        return self._value_types
    def make_aggregate_value(self, items) -> "AggregateValue":
        return TupleValue(items)
```

[`ListTy`](src/cuda/tile/_ir/type.py#L822-L837) 的展开与元素类型无关——永远产出「指向 int64 的指针 tile + int32 长度 tile」两项：

```python
# src/cuda/tile/_ir/type.py
@dataclass(frozen=True)
class ListTy(Type):
    item_type: Type
    def is_aggregate(self) -> bool:
        return True
    def aggregate_item_types(self) -> tuple["Type", ...]:
        from .._datatype import int32, int64, pointer_dtype
        ptr_dtype = pointer_dtype(int64)
        ptr_tile_ty = TileTy(ptr_dtype)
        len_ty = TileTy(int32)
        return ptr_tile_ty, len_ty
    def make_aggregate_value(self, items) -> "AggregateValue":
        base, length = items
        return ListValue(base, length)
```

`List[Array]` 在 `_compile.py` 里被构造为 `ListTy(array_ty)`：[`_create_parameter` 处理 `ListConstraint`](src/cuda/tile/_compile.py#L223-L227) 时，先为元素数组构造 `array_ty`，再包成 `ListTy`：

```python
# src/cuda/tile/_compile.py
    elif isinstance(constraint, ListConstraint):
        assert isinstance(constraint.element, ArrayConstraint)
        array_ann = None if annotation.list is None else annotation.list.element
        array_ty = _get_array_ty(constraint.element, array_ann, path, var.ctx.typing_hooks)
        ty = ListTy(array_ty)
```

> 注意 `ListTy.item_type` 存的是 `array_ty`（一个 `ArrayTy`），但 `aggregate_item_types` 完全无视它、固定返回两项。这说明 `item_type` 是给「元素访问语义」用的，运行时序列化只关心「指针 + 长度」。

#### 4.4.4 代码实践

**实践目标**：对比 `TupleTy` 与 `ListTy` 的展开差异。

**操作步骤**：

1. 心智模拟 `TupleTy([ArrayTy(2D), ArrayTy(2D)])` 的 `flatten_aggregate`：先取两个 `ArrayTy` 子类型，各自再展开成 5 个叶子，共 10 个叶子。
2. 心智模拟 `ListTy(ArrayTy(2D))` 的 `flatten_aggregate`：直接返回 `(ptr<int64>, int32)` 共 2 个叶子——**与元素是几维数组无关**。
3. 思考：为何 `List` 的元素个数不进入类型？因为 List 是变长的，元素个数是运行时 `length` 值，不是编译期类型的一部分。

**预期结果**：理解「定长元组的展开数随元素类型变化，变长列表的展开数恒为 2」。

#### 4.4.5 小练习与答案

**练习 1**：`TupleTy([TileTy(int32,(16,)), TileTy(int32,(16,))])` 展开后有几个叶子？

**答案**：2 个。每个 `TileTy` 是叶子（非聚合），各 yield 自己，总共 2 个。

**练习 2**：`ListTy(ArrayTy(3D))` 展开后有几个叶子？为什么不是 `1 + 2*3`？

**答案**：2 个（指针 + 长度）。`1 + 2*3` 是「单个 ArrayTy」的展开数，但 List 不内联其元素数组——它只持有一个指向元素缓冲的指针和长度。元素数组的展开发生在元素被实际访问时，而非 List 类型本身。

---

### 4.5 flatten_block_parameters：聚合展开为可序列化参数

#### 4.5.1 概念说明

现在来到本讲的「主轴」：聚合参数如何变成扁平的 Block 入口参数。这一步由 `flatten_block_parameters` 完成，它是连接「用户聚合对象」与「字节码扁平签名」的桥梁。

关键设计：**展开不是「销毁」聚合，而是「双向建立映射」**。对每个聚合参数 `v`，`flatten_block_parameters` 会：

1. 新建一组扁平的子 `Var`（`v_0, v_1, ...`），类型是 `v` 的各叶子类型——这些扁平 Var 成为 Block 的真正入口参数。
2. 把原聚合 `Var` `v` **重建**为一个 `make_aggregate` 操作，其输入正是这些扁平子 Var。

这样，函数体里凡是用到 `v` 的地方，都能拿到一个完整的聚合值；而对外（字节码签名）暴露的只是扁平子 Var。这就是「先拆后装」的模式（与 u3-l7 描述的 tuple 处理一致）。

#### 4.5.2 核心流程

```text
对每个参数 var v:
  if v.get_type().is_aggregate():
      item_types = v.get_type().flatten_aggregate()          # 递归取叶子类型
      flat_vars = expand_aggregate_var(v)                    # 建 v_0,v_1,... 并设类型
      _unflatten_proper_aggregate(iter(flat_vars), ty, ty, v)# 用 flat_vars 重建 v
      记录 flat_vars 为一个分组
  else:
      记录 (v,) 为一个分组
返回 list[tuple[Var,...]]（每个原参数对应一个分组）
```

`expand_aggregate_var` 负责造子 Var：按叶子类型数量，创建名为 `v_0, v_1, ...` 的 Var 并逐个 `set_type`。`_unflatten_proper_aggregate` 负责装回：递归地把叶子 Var 组装回嵌套的聚合值，最终用 `Builder.make_aggregate` 绑定到原 `v`。

#### 4.5.3 源码精读

[`flatten_block_parameters`](src/cuda/tile/_ir/aggregate_support.py#L59-L71) 是入口，对每个聚合参数走「展开 + 重建」、对叶子参数原样保留：

```python
# src/cuda/tile/_ir/aggregate_support.py
def flatten_block_parameters(vars: Sequence[Var]) -> list[tuple[Var, ...]]:
    ret = []
    for v in vars:
        ty = v.get_type_allow_invalid()
        if ty.is_aggregate():
            flattened_vars = expand_aggregate_var(v)
            ret.append(flattened_vars)
            it = iter(flattened_vars)
            _unflatten_proper_aggregate(it, ty, ty, v)      # 用扁平 Var 重建 v
            assert next(it, None) is None                    # 恰好用完
        else:
            ret.append((v,))
    return ret
```

[`expand_aggregate_var`](src/cuda/tile/_ir/aggregate_support.py#L50-L56) 按叶子类型造子 Var 并设类型：

```python
def expand_aggregate_var(var: Var) -> tuple[Var, ...]:
    item_types = tuple(var.get_type().flatten_aggregate())
    ret = tuple(var.ctx.make_var(f"{var.get_original_name()}_{i}", var.loc)
                for i in range(len(item_types)))
    for item, item_ty in zip(ret, item_types, strict=True):
        item.set_type(item_ty)
    return ret
```

[`_unflatten_proper_aggregate`](src/cuda/tile/_ir/aggregate_support.py#L74-L95) 递归装回：先递归处理各直接子类型（`_maybe_unflatten_aggregate`），再用 `make_aggregate_value` 组装，最后通过 `Builder.make_aggregate`（或注册的实现）绑定到目标 `result_var`：

```python
def _unflatten_proper_aggregate(flattened_iter, nominal, actual, result_var) -> Var:
    nominal_item_types = nominal.aggregate_item_types()
    ...
    items = tuple(_maybe_unflatten_aggregate(flattened_iter, item_nominal, item_actual)
                  for ... in zip(nominal_item_types, actual.aggregate_item_types(), strict=True))
    val = nominal.make_aggregate_value(items)
    impl = ImplRegistry.get_current().unflatten_aggregate_implementations.get(type(nominal))
    if impl is None:
        return Builder.get_current().make_aggregate(val, nominal, result_var=result_var)
    else:
        return impl(val, nominal, result_var)
```

调用方在 [`_create_parameter`](src/cuda/tile/_compile.py#L232-L234) 里就是用它把刚设好类型的参数立即展开：

```python
# src/cuda/tile/_compile.py
    var.set_type(ty)
    [flat_vars] = flatten_block_parameters([var])            # 立即展开为扁平 Var
    nonconstant_flat_vars.append((flat_vars, constraint))
```

最终在 `_IrKeeper.get_final_ir` 里，内核 Block 的入口参数正是这些扁平 Var 的总和（`func_body.params = sum((vars for vars, _ in params.nonconstant_flat_vars), ())`，见 [`_compile.py:402-404`](src/cuda/tile/_compile.py#L402-L404)）。`Var.flatten_aggregate`（[`ir.py:238-243`](src/cuda/tile/_ir/ir.py#L238-L243)）则提供「值层面」的递归展开，与「类型层面」的 `Type.flatten_aggregate` 对偶。

> 把整条链串起来：用户参数 → `_create_parameter` 设类型 → `flatten_block_parameters` 展开成扁平 Var 并重建聚合 → 扁平 Var 成为 `Block.params` → 字节码序列化时只看到扁平签名。聚合对象始终在 IR 内部「按需重建」，对外只暴露可序列化的扁平值。

#### 4.5.4 代码实践

**实践目标**：跟踪一个 `List[Array]` 参数被展开成扁平 Var 序列的全过程。

**操作步骤**：

1. 设想内核签名 `def k(weights: List[Array]):`，运行时传入一个长度为 3 的 List。
2. 在 `_create_parameter` 里，`ListConstraint` 分支构造 `ty = ListTy(array_ty)`（其中 `array_ty` 是元素数组的 `ArrayTy`），`var.set_type(ty)`。
3. 调用 `flatten_block_parameters([var])`：
   - `ty.is_aggregate()` 为真 → `expand_aggregate_var(var)` 按 `ListTy.flatten_aggregate()` 造 2 个子 Var：`var_0`（类型 `TileTy(ptr<int64>)`）、`var_1`（类型 `TileTy(int32)`）。
   - `_unflatten_proper_aggregate` 把 `(var_0, var_1)` 装回 `ListValue(base=var_0, length=var_1)`，并用 `make_aggregate` 绑定到原 `var`。
4. 于是 `Block.params` 多了 `var_0, var_1` 两个扁平入口参数；内核体内访问 `weights` 时拿到的是重建好的 List 聚合值。

**需要观察的现象**：dump 出的内核签名里，`List[Array]` 参数对应**两个**标量参数（一个指针、一个 int32 长度），而不是「多个数组」。

**预期结果**：能画出 `List[Array] → (ptr<int64>, int32)` 的展开图，并指出 `array_ty`（元素类型）不参与展开数。

#### 4.5.5 小练习与答案

**练习 1**：`flatten_block_parameters` 对一个非聚合的 `TileTy` 参数返回什么？

**答案**：返回 `[(v,)]`——单元素元组，原 Var 不变。因为 `TileTy.is_aggregate()` 为 `False`，走 `else` 分支。

**练习 2**：为什么 `_unflatten_proper_aggregate` 里要 `assert next(it, None) is None`？

**答案**：这是「恰好用完」校验。展开时按 `flatten_aggregate` 的叶子数造了 N 个 Var，重建时也应恰好消费 N 个。若迭代器还有剩余，说明「类型声明的叶子数」与「实际提供的 Var 数」不一致——这是类型不一致的 bug 信号，必须断言失败。

---

### 4.6 TypingHooks：前端扩展点

#### 4.6.1 概念说明

最后看一个「横切」的设计：`TypingHooks`。前面多处出现 `typing_hooks.get_tensor_like_type(dtype, shape)`——构造 base_ptr 的类型、构造 size 的类型、构造常量的类型时，编译器都不是直接 `TileTy(...)`，而是问 `typing_hooks`。

为什么？因为 Tile IR 的类型系统被设计成**与「具体的张量表示」解耦**。`get_tensor_like_type(dtype, shape)` 是一个抽象：「给我一个 dtype 和 shape，告诉我用哪种 TensorLikeTy 表达它」。标准 cuTile 前端用 `TileTy` 来回答，但这个抽象留出了换实现的口子——例如实验性前端或不同的张量后端，可以提供自己的 `TypingHooks`，让 IR 里所有「张量样」的值用另一套类型表达，而不必改动 `ArrayTy`、各种 op 等大量代码。

#### 4.6.2 核心流程

```text
IRContext(typing_hooks)          # 每个 IR 上下文持有一个 hooks
  ↓
ArrayTy.aggregate_item_types()   # 需要 base_ptr/size 的类型
  → self.typing_hooks.get_tensor_like_type(...)
  ↓
_TileTypingHooks.get_tensor_like_type(dtype, shape)
  → return TileTy(dtype, shape)  # 标准前端：直接返回 TileTy
```

`TypingHooks` 被 `IRContext` 在构造时持有（[`ir.py:40-51`](src/cuda/tile/_ir/ir.py#L40-L51)），并被 `ArrayTy` 等类型对象反过来持有（`self.typing_hooks`），从而任何需要「张量类型」的地方都能就近问到。

#### 4.6.3 源码精读

[`TypingHooks` 抽象基类](src/cuda/tile/_ir/ir.py#L33-L35) 只定义一个方法：

```python
# src/cuda/tile/_ir/ir.py
class TypingHooks:
    def get_tensor_like_type(self, dtype: DType, shape: Sequence[int]) -> TensorLikeTy:
        raise NotImplementedError()
```

[`IRContext` 在构造时接收并保存 `typing_hooks`](src/cuda/tile/_ir/ir.py#L38-L51)，使整个内核的 IR 共用同一套钩子：

```python
class IRContext:
    def __init__(self, log_ir_on_error, tileiras_version, typing_hooks: TypingHooks):
        ...
        self.typing_hooks = typing_hooks
```

标准前端提供 [`_TileTypingHooks`](src/cuda/tile/_compile.py#L360-L362)——把抽象落到 `TileTy`：

```python
# src/cuda/tile/_compile.py
class _TileTypingHooks(TypingHooks):
    def get_tensor_like_type(self, dtype: DType, shape: Sequence[int]) -> TileTy:
        return TileTy(dtype, shape)
```

它的使用面非常广。除了 4.3 里 `ArrayTy.aggregate_item_types` 通过 `self.typing_hooks` 调用，编译器在构造标量常量、指针、reshape 结果等场景也统一走 `var.ctx.typing_hooks.get_tensor_like_type(...)`（例如 [`_compile.py:220`](src/cuda/tile/_compile.py#L220) 构造标量类型、arithmetic_ops 里大量 reshape/cast 的结果类型）。这种集中化让「换一种张量表示」成为可能：只需提供新的 `TypingHooks` 子类。

> 一句话：`TypingHooks` 是类型系统为「可替换前端」预留的接缝。`TileTy` 是这条接缝上今天的标准实现。

#### 4.6.4 代码实践

**实践目标**：统计 `get_tensor_like_type` 的调用密度，体会「接缝」的覆盖面。

**操作步骤**：

1. 在仓库内搜索 `get_tensor_like_type`（参考本讲源码地图列出的命中点：`_ir/type.py`、`_ir/arithmetic_ops.py`、`_ir/core_ops.py`、`_ir/cast_ops.py`、`_ir/control_flow_ops.py`、`_ir/ops_utils.py`、`_compile.py`、`_ir/typing_support.py`）。
2. 选一处（如 `arithmetic_ops.py` 的 reshape）阅读：结果类型为何不直接 `TileTy(...)` 而要问 hooks。
3. 思考：如果要让 IR 用一种「假想的 MyTileTy」替代 `TileTy`，需要改哪些点？

**预期结果**：发现只需实现一个新的 `TypingHooks` 子类并把它传给 `IRContext`，所有走 hooks 的调用点自动切换——这就是「扩展点」的价值。

#### 4.6.5 小练习与答案

**练习 1**：`ArrayTy` 为什么要持有一个 `typing_hooks` 引用，而不是每次需要时去问 `IRContext`？

**答案**：因为 `ArrayTy` 是不可变的数据类式对象，可能在多处被构造/复用，且它的 `aggregate_item_types` 必须知道「用哪种张量类型表达 base_ptr/size」。把 hooks 直接存进 `ArrayTy`，让类型对象自包含，调用 `aggregate_item_types` 时无需额外上下文。

**练习 2**：如果某前端想用 `MyTileTy` 替代 `TileTy`，`get_tensor_like_type` 应返回什么？`ArrayTy.aggregate_item_types` 的返回会变吗？

**答案**：应返回 `MyTileTy(dtype, shape)`。`aggregate_item_types` 的**结构不变**（仍是 base_ptr 类型 + 各 size 类型），但其中每个张量类型的**具体类**从 `TileTy` 变成 `MyTileTy`——这正是 hooks 抽象的解耦点。

## 5. 综合实践

把本讲所有概念串起来，做一个完整的「参数展开推演」。

**任务**：给定一个内核签名

```python
@ct.kernel
def k(
    a: Annotated[ct.Array, ct.ArrayAnnotation(static_shape_dims=(0,))],
    pair: tuple[ct.Tensor, ct.Tensor],
    weights: List[ct.Array],
):
    ...
```

假设 `a` 是秩 2 数组（第 0 维静态取值 16，第 1 维动态），`pair` 是两个标量张量，`weights` 是长度为 L 的数组列表。要求：

1. **写出每个参数的 IR 类型**：`a` → `ArrayTy(shape=(16, None), ...)`；`pair` → `TupleTy([<tensor_ty>, <tensor_ty>])`；`weights` → `ListTy(ArrayTy(...))`。
2. **算出每个参数经 `flatten_block_parameters` 后的扁平 Var 数**：
   - `a`：`1 + 2*2 = 5`（base_ptr + 2 shape + 2 strides）。
   - `pair`：2（两个叶子张量）。
   - `weights`：2（ptr<int64> + int32 长度）。
3. **画出 Block 入口参数序列**：共 `5 + 2 + 2 = 9` 个扁平 Var。
4. **标注哪些维度是编译期常量**：只有 `a.shape[0] == 16` 是常量（进入 IR 类型，参与 divby/对齐优化，见 u6-l2）；其余 size/length 都是运行时值。
5. **说明重建**：`a`、`pair`、`weights` 三个原聚合 Var 都会被 `make_aggregate` 重建，函数体内访问它们时拿到完整聚合值。

**验证方式**：把这段签名写进一个最小内核，设置 `CUDA_TILE_DUMP_TILEIR=1` 运行，对照 dump 出的函数签名（`(...):` 行，参数由 [`Block.to_string`](src/cuda/tile/_ir/ir.py#L772-L784) 打印，聚合 Var 会显示为 `name{flat0, flat1, ...}`，见 [`var_aggregate_name`](src/cuda/tile/_ir/ir.py#L699-L703)），确认扁平参数的数量与静态维度的位置。**待本地验证**（需要 CUDA 环境）。

## 6. 本讲小结

- Tile IR 的所有类型继承自 `Type`，其核心是**聚合契约**四方法：`is_aggregate` / `aggregate_item_types` / `flatten_aggregate` / `make_aggregate_value`，用统一方式描述「拆与装」。`TensorLikeTy` 是「有 dtype 和 shape」的中间基类。
- `TileTy` 用 `__new__` + `_tile_ty_cache` 实现 **flyweight**，相同 `(dtype, shape)` 复用同一对象；它是叶子（非聚合）。
- `ArrayTy` 是聚合类型，展开为 `1 + 2*ndim` 个叶子 `(base_ptr, *shape, *strides)`；`shape` 字段的 `None`/`int` 二义性承载了**静态形状特化**——静态维度从 `_get_array_ty` 的 `shape_constant` 烘焙进类型。
- `TupleTy`（异构定长，子类型即各元素）与 `ListTy`（同质变长，运行时只是 `ptr<int64> + int32` 长度）是两类聚合参数，展开规则截然不同。
- `flatten_block_parameters` 是「先拆后装」的主轴：把聚合参数展开成可序列化的扁平 Var 作为 `Block.params`，同时用 `make_aggregate` 重建原聚合对象供函数体使用。
- `TypingHooks.get_tensor_like_type` 是前端的**扩展接缝**，标准实现 `_TileTypingHooks` 返回 `TileTy`；所有需要「张量类型」的地方都走它，使 IR 与具体张量表示解耦。

## 7. 下一步学习建议

- **下一讲 u5-l7（Stub 与实现注册）**：本讲只讲了「类型怎么表达」，但 `make_aggregate`、`build_tuple` 这些操作的具体 IR 实现是怎么注册和分派的？下一讲讲 `@stub` / `@impl` / `tile_impl_registry`，补上「类型→操作」的最后一环。
- **u6-l2（数据流分析与整除性传播）**：本讲提到静态形状进入 `ArrayTy.shape` 后能成为「整除性事实」——下一单元会展开 `dataflow_analysis` 如何把这些常量维度折叠成对齐假设，优化内存访问。
- **u7-l2（字节码格式与版本）**：本讲强调参数最终要「可序列化」，u7-l2 会展示扁平 Var 如何被 `encode_*Op` 写进字节码的函数签名 section，与本讲的展开结果首尾相接。
- **延伸阅读**：直接打开 [`src/cuda/tile/_ir/type.py`](src/cuda/tile/_ir/type.py) 通读各类 View 类型（`TiledViewTy` / `GatherScatterViewTy` / `PartitionViewTy`），它们都复用本讲的聚合契约，是巩固「四方法」直觉的最佳练习。
