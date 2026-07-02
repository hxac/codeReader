# tile 算术运算与形状操作

## 1. 本讲目标

上一讲（u3-l1）我们建立了 cuTile 内核的「主循环骨架」：用 `ct.load` 把全局数组的一块搬成 tile，再用 `ct.store` 写回。但骨架中间的「计算」部分还是空的——load 出来的 tile 到底能做哪些运算？形状不匹配时怎么办？想换一种数值类型呢？

本讲就来填上这块「compute」。读完本讲，你应当能够：

- 用运算符（`+ - * / ** %`、比较、`@`）或等价的 `ct.add/ct.mul/...` 函数对 tile 做**逐元素算术**，并理解它的形状广播与类型提升规则。
- 用 `astype` 在 tile 之间做**显式类型转换**，并分清它与 `bitcast` 的区别。
- 用 `reshape`、`permute`、`transpose`、`broadcast_to`、`expand_dims`、`cat` 对 tile 做**形状变换**，并牢记这些操作都**产生新 tile 而不修改原 tile**。
- 独立写出一个**矩阵转置内核**，把 \((M,N)\) 矩阵转成 \((N,M)\)。

本讲覆盖三个最小模块：**add/mul/…arithmetic（逐元素算术）**、**astype（类型转换）**、**reshape/permute（形状变换）**，并顺带把 transpose/broadcast_to/cat 串起来。

## 2. 前置知识

本讲默认你已经掌握：

- **tile 与 array 的区别**（u2-l3）：tile 是内核内部**不可变**、形状**编译期已知**、每维必须为 **2 的幂**的多维集合；array 是 host 分配、可读写、运行时 shape 的全局显存。
- **load–compute–store 范式**（u3-l1）：`ct.load(array, index, shape)` 返回 tile，`ct.store(array, index, tile)` 写回，`index` 是 **tile space（瓦片）索引**而非元素下标。
- **DType 与类型提升**（u2-l4）：算术运算会做类型提升，loosely typed 常量（字面量、`Constant`）会迁就对方，strictly typed 操作数按提升表求公共类型；`RoundingMode` 控制舍入。

一个反复出现的关键直觉是：**tile 是不可变的（immutable）**。文档把 tile 定义为「an immutable multidimensional collection of elements」（见 [data.rst:L82](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L82)）。这意味着本讲所有算术与形状操作都**不会就地修改输入 tile，而是返回一个新的 tile**。在底层 Tile IR 里，这对应 SSA（单赋值）风格——每个运算产生一个新的值，旧值仍然存在。这是 cuTile「函数式」计算风格的核心，也是它容易并行、容易优化的原因。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py) | 所有用户操作的**签名桩（stub）**。本讲的算术、`astype`、`reshape/permute/transpose/broadcast_to/cat` 全部在这里以 `@stub` 声明，并附带 `testcode/testoutput` 行为示例。`Tile` 类里还有运算符重载与同名方法。 |
| [samples/Transpose.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/Transpose.py) | 一个完整的矩阵转置示例：`@ct.kernel` 内用 `ct.transpose` 做 tile 内转置，host 端算 grid 并 `ct.launch`。是本讲综合实践的范本。 |
| [test/test_transpose.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_transpose.py) | 转置/置换的单元测试，演示了「函数式」与「方法式」两种等价写法，可作为行为参考。 |
| [docs/source/data.rst](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst) | tile 不可变性、2 的幂约束、形状广播规则的权威文字说明。 |

> 说明：`_stub.py` 里这些函数只有签名与文档（标注为 `@stub`），真正的实现由后端 IR 注册系统提供（详见 u5-l7）。本讲关注「怎么用、语义是什么」，实现机制留给后续讲义。

---

## 4. 核心概念与源码讲解

### 4.1 逐元素算术运算与运算符重载

#### 4.1.1 概念说明

cuTile 的算术运算是**逐元素（elementwise）**的：两个 tile 做加法，就是对应位置的元素两两相加，得到一个新 tile。这一点和 NumPy 的 ndarray 运算完全一致，也和 PyTorch 张量运算一致。

cuTile 提供两种等价的写法：

1. **运算符**：直接写 `a + b`、`a * b`、`a @ b`、`a > b`。
2. **函数**：写 `ct.add(a, b)`、`ct.mul(a, b)`、`ct.matmul(a, b)`、`ct.greater(a, b)`。

运算符只是函数的语法糖：`Tile` 类重载了 Python 的双下划线方法（`__add__`、`__mul__` 等），内部转调对应的 `ct.*` 函数。

两个关键语义（与 u2-l4 衔接）：

- **形状广播**：两 tile 形状不同时，按 NumPy 规则自动广播到公共形状。
- **类型提升**：两 tile dtype 不同时，提升到公共 dtype；loosely typed 常量会迁就对方。

算术返回 `TileOrScalar`——结果是 tile；若两个操作数都是标量（零维 tile），结果也是标量。

#### 4.1.2 核心流程

一次逐元素二元运算的执行过程可概括为：

1. **形状广播**：把 `x`、`y` 的形状按 NumPy 规则求公共形状 `S`。
2. **类型提升**：把 `x`、`y` 的 dtype 按提升表求公共 dtype `T`（详见 u2-l4）。
3. **逐元素求值**：对公共形状 `S` 中的每个位置，取 `x`、`y` 对应元素（必要时按广播规则复制），做该运算。
4. **产生新 tile**：返回一个 dtype 为 `T`、shape 为 `S` 的**新** tile；`x`、`y` 不被修改。

形状广播的规则（见 [data.rst:L188-L200](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L188-L200)）：

- 末尾对齐（trailing dimensions align）；
- 对应维相等，或其中一个为 1，则兼容；
- 维度较少的一方在左侧补 1。

用集合记号，两个形状 \(s_x\)、\(s_y\) 能广播，当且仅当对每一维 \(i\) 满足

\[
s_x^{(i)} = s_y^{(i)} \;\lor\; s_x^{(i)} = 1 \;\lor\; s_y^{(i)} = 1
\]

结果维取 \(\max(s_x^{(i)}, s_y^{(i)})\)。例如 \((4,1)\) 与 \((1,8)\) 广播得到 \((4,8)\)。

#### 4.1.3 源码精读

**（1）运算符重载转调函数**——`Tile` 类把每个运算符都委托给同名 `ct.*` 函数。例如加法和乘法：

[_stub.py:L616-L623](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L616-L623) —— `Tile.__add__` 调 `add`，`Tile.__mul__` 调 `mul`：

```python
def __add__(self, other) -> "Tile":
    return add(self, other)
...
def __mul__(self, other) -> "Tile":
    return mul(self, other)
```

这一族重载覆盖了 `+ - * / // % **`、位运算 `& | ^ << >>`、比较 `> >= < <= == !=`、一元 `-x`/`~x`、以及矩阵乘 `@`（见 [_stub.py:L700-L701](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L700-L701) 的 `__matmul__ → matmul`）。

> ⚠️ **重要陷阱**：`Tile.__eq__` 被重载成 `equal`（见 [_stub.py:L688-L689](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L688-L689)）。所以 `a == b` **不是** Python 的对象相等判断，而是返回一个逐元素比较的布尔 tile。在 tile code 里这是你想要的；但在 host 代码里把 tile 当 key 或做身份判断会出问题——tile 本就不能在 host 代码里使用。

**（2）函数式签名与广播/提升说明**——加法的自由函数签名（带可选的 `rounding_mode` 与 `flush_to_zero`）：

[_stub.py:L3122-L3139](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L3122-L3139) —— `add`，文档里嵌入了 `tx + 10 → [10,11,12,13]` 的行为示例：

```python
@_doc_binary_op('+')
@stub
def add(x, y, /, *, rounding_mode=None, flush_to_zero=False) -> TileOrScalar:
    ...
```

所有二元算术（`add/sub/mul/truediv/pow/mod/bitwise_*/minimum/maximum`）都套了同一个文档装饰器 `_doc_binary_op`，它统一生成这句语义说明（见 [_stub.py:L3107-L3117](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L3107-L3117)）：

> The `shape` of `x` and `y` will be broadcasted and `dtype` promoted to common dtype.

即「形状广播、dtype 提升到公共类型」。`sub`/`mul` 同构（见 [_stub.py:L3142-L3179](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L3142-L3179)）。`minimum/maximum` 用的是 Python 内置 `min/max` 风格（见 [_stub.py:L3437-L3474](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L3437-L3474)），注意它俩和归约 `ct.max/ct.min`（下一讲 u3-l4）不同：这里是逐元素取大/取小，不消除维度。

**（3）配套的逐元素选择 `where`**——算术常和条件选择搭配：

[_stub.py:L4031-L4057](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L4031-L4057) —— `where(cond, x, y)`：cond 为 True 取 x，False 取 y，得到逐元素选择的新 tile。

#### 4.1.4 代码实践

**实践目标**：亲手写一个逐元素「缩放并偏移」内核，观察运算符、广播与类型提升。

**操作步骤**：

1. 阅读上面的文档示例，确认 `tx + 10`、`tx * 3` 的预期输出。
2. 编写如下内核（示例代码，基于 u3-l1 的 load/store 范式扩展而来）：

```python
# 示例代码
import cuda.tile as ct

@ct.kernel
def scale_shift(x, y, alpha):
    t = ct.load(x, index=(ct.bid(0),), shape=(32,))   # shape (32,) int32
    # alpha 是 Python 字面量传入，作为 loosely typed 常量；int32 tile 与之运算仍为 int32
    out = t * alpha + 100                                # 逐元素: 先 mul 后 add
    ct.store(y, index=(ct.bid(0),), tile=out)
```

3. host 端用 `torch` 造一个长度可被 32 整除的 `int32` 张量 `x`、同形 `y`，按 u3-l1 的方式算 grid 并 `ct.launch`，最后 `torch.testing.assert_close(y, x * alpha + 100)`。

**需要观察的现象**：

- `t * alpha + 100` 这一行里，`alpha` 与 `100` 都是 loosely typed 常量，结果 dtype 跟随 `t`（int32）。
- 把 `t` 改成先 `ct.astype(t, ct.float32)` 再运算，观察结果 tile 变成 float（`flush_to_zero` 等浮点选项才开始生效）。

**预期结果**：`y == x * alpha + 100`，逐元素成立。若你尚未搭好运行环境，可先标注为「待本地验证」，并把断言写好备用。

#### 4.1.5 小练习与答案

**练习 1**：下列表达式的结果 shape 各是多少？
(a) `ct.arange(4) + 10`；(b) shape `(4,1)` 的 tile 与 shape `(1,8)` 的 tile 相加；(c) shape `(4,8)` 与 shape `(8,)` 相加。

**答案**：
- (a) `(4,)`——标量 `10` 广播到 `(4,)`。
- (b) `(4,8)`——按 NumPy 广播。
- (c) `(4,8)`——`(8,)` 左侧补 1 成 `(1,8)`，再广播。

**练习 2**：为什么在 tile code 里写 `if a == b: ...`（`a`、`b` 是非零维 tile）会有问题？

**答案**：`a == b` 被重载成 `equal`，返回的是一个**布尔 tile**，而不是 Python `bool`。`if` 需要的是一个确定的真值；对一个含多个元素的 tile 做布尔判断语义不明确，cuTile 不会隐式把 tile 折叠成标量。若要判断，应先用归约（如 `ct.all`/下一讲的归约）或对零维 tile 操作。

---

### 4.2 类型转换 astype

#### 4.2.1 概念说明

`astype` 把一个 tile 的元素**显式转换**成目标 `DType`，返回一个**新 tile**，原 tile 不变。它和类型提升的区别在于：提升是隐式的、由运算规则驱动；`astype` 是你主动指定的、可控的转换。

典型用途：

- load 出 `float32` 数据，计算中想用 `tfloat32` 走张量核（u3-l6 会展开）。
- 把 `int32` 索引 tile 转成 `float32` 参与浮点运算。
- 把高精度累加结果降回 `float16` 写回显存。

需要区分两个相近操作：

| 操作 | 语义 | 例子 |
|------|------|------|
| `astype(x, dtype)` | **数值转换**：按目标类型重新表示数值（可能舍入/截断） | `1.9 → int32` 得 `1` 或 `2`（按舍入模式） |
| `bitcast(x, dtype)` | **位重解释**：保持底层比特不变，换个类型「看」 | `1.0(f32)` 的比特 `0x3f800000` → `uint32` 得 `0x3f800000` |

#### 4.2.2 核心流程

`astype` 的执行：

1. 校验目标 `dtype` 合法（受限浮点如 `tfloat32/float8_*` 不能算术，但可以作为转换目标/来源，详见 u2-l4）。
2. 对 tile 每个元素，按目标 dtype 做数值转换；浮点转换受 `RoundingMode`（默认 RN，最近偶数）影响（见 u2-l4）。
3. 返回同 shape、新 dtype 的 tile。

注意 `astype` **不改变 shape**，只改 dtype。

#### 4.2.3 源码精读

`astype` 既是自由函数也是 `Tile` 方法：

[_stub.py:L2492-L2515](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2492-L2515) —— 自由函数 `astype`，文档示例 `ct.arange(4, int32)` → `astype(..., float32)` 得 `[0.0,1.0,2.0,3.0]`：

```python
@stub
def astype(x, dtype, /) -> Tile:
    """Converts a tile to the specified data type."""
```

[_stub.py:L604-L606](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L604-L606) —— `Tile.astype` 方法只是转调自由函数：

```python
def astype(self, dtype) -> "Tile":
    return astype(self, dtype)
```

对照 `bitcast`（位重解释，不改比特）：见 [_stub.py:L2518-L2540](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2518-L2540)，其文档示例 `bitcast(1.0, uint32)` 得 `0x3f800000`。

#### 4.2.4 代码实践

**实践目标**：用 `astype` 把整数 tile 转成浮点，并验证 `astype` 不改 shape、不改原 tile。

**操作步骤**：

```python
# 示例代码
@ct.kernel
def to_float(x, y):
    t = ct.load(x, index=(0,), shape=(4,))   # 假设 x 是 int32
    f = ct.astype(t, ct.float32)             # 新 tile，dtype=float32，shape 仍是 (4,)
    ct.store(y, index=(0,), tile=f * 2.5)    # 之后才能用浮点乘
```

**需要观察的现象**：

- 不写 `astype` 直接 `t * 2.5`：`2.5` 是 loosely typed 浮点常量，按 u2-l4 会先归为 float32，结果其实也是 float32——但显式 `astype` 更清晰、更可控。
- `t` 本身仍是 int32（不可变，未被修改），`f` 才是 float32。

**预期结果**：`y == x * 2.5`（数值上）。环境未就绪时标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`astype(ct.full((2,), 1.0, ct.float32), ct.int32)` 与 `bitcast(ct.full((2,), 1.0, ct.float32), ct.int32)` 结果分别是什么？

**答案**：`astype` 数值转换：`1.0 → 1`（int32）。`bitcast` 位重解释：`1.0` 的 IEEE-754 比特是 `0x3f800000`，所以得到整数 `1065353216`（即 `0x3f800000`）。

**练习 2**：想把 `float32` 累加器降回 `float16` 写回显存以省带宽，应该用 `astype` 还是 `bitcast`？为什么？

**答案**：用 `astype`。`bitcast` 只重解释比特，`float32` 的 32 比特无法塞进 `float16` 的 16 比特（位数不匹配会报错）；`astype` 才是真正的数值精度转换。

---

### 4.3 形状重组：reshape 与 permute/transpose

#### 4.3.1 概念说明

「形状操作」改变 tile 的**逻辑形状或轴顺序，但不改变（或只重排）底层元素**，且都返回新 tile。本节讲三个最常用的：`reshape`、`permute`、`transpose`。

- **reshape(shape)**：在元素总数不变的前提下，把 tile 重新组织成新形状。常用于把一维 tile 看成二维、或把多维 tile 摊平。
- **permute(axes)**：对 tile 的所有轴做任意重排（置换），是 NumPy `np.transpose` 的等价物。
- **transpose(axis0, axis1)**：只交换两个轴。对二维 tile 不传参时，默认交换两个轴（即矩阵转置）。

三者都**不拷贝/不修改元素**——它们产生的是原 tile 的一个新视图（逻辑层面），原 tile 依然不可变且原样存在。

两个约束（来自 u2-l3，由后端在编译期校验）：

1. tile 每维必须是 **2 的幂**；`reshape` 后的各维同样要满足（见 [data.rst:L88](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L88)）。
2. `reshape` 元素总数守恒；`permute`/`transpose` 元素总数与各维大小都不变，只是轴顺序变了。

#### 4.3.2 核心流程

**reshape**：

1. 校验 `shape` 各维为 2 的幂。
2. 计算总元素数 \(N = \prod s_i\)，要求等于原 tile 元素数（允许一个维度写成 `-1` 自动推断：\(s_k = N / \prod_{i\ne k} s_i\)）。
3. 按行优先重排逻辑形状，返回新 tile。

**permute(axes)**：

1. `axes` 是 `0..ndim-1` 的一个置换。
2. 新 tile 的第 `i` 轴 = 原 tile 的第 `axes[i]` 轴；即 `out.shape[i] = in.shape[axes[i]]`。
3. 元素按新轴顺序重新索引：`out[i_0, ...] = in[i_{axes^{-1}(0)}, ...]`，返回新 tile。

**transpose(axis0, axis1)**：

1. 二维 tile 且不传参：等价于 `permute((1,0))`。
2. 否则必须显式给出要交换的两个轴 `axis0`、`axis1`，只交换这两个轴，其余不动。

#### 4.3.3 源码精读

**（1）reshape**——支持 `-1` 自动推断：

[_stub.py:L2402-L2432](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2402-L2432) —— 文档示例 `reshape(arange(8),(2,4))` 得 `[[0,1,2,3],[4,5,6,7]]`：

```python
@stub
def reshape(x, /, shape) -> Tile:
    """Reshapes a tile to the specified shape.
    One of the shape elements may be specified as -1 to indicate that the
    corresponding dimension is to be inferred automatically."""
```

方法形式 `Tile.reshape` 转调它（见 [_stub.py:L592-L594](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L592-L594)）。

**（2）permute**——全轴置换：

[_stub.py:L2435-L2458](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2435-L2458) —— 文档示例 `permute(arange(8).reshape(2,2,2), (2,0,1))` 重排三个轴。

**（3）transpose**——二维默认转置，高维需显式指定两轴：

[_stub.py:L2461-L2489](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2461-L2489)：

```python
@stub
def transpose(x, /, axis0=None, axis1=None) -> Tile:
    """Transposes two axes of the input tile with at least 2 dimensions.
    For a 2-dimensional tile, the two axes are transposed if axis0 and axis1 are not specified.
    For tiles with more than 2 dimensions, axis0 and axis1 must be explicitly specified."""
```

测试文件同时验证了「函数式」与「方法式」两种写法等价，例如默认轴转置：

[test_transpose.py:L46-L52](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_transpose.py#L46-L52)：

```python
@ct.kernel
def transpose_tile_2d_default_axes(x, y, use_method: ct.Constant[bool]):
    tx = ct.load(x, index=(0, 0), shape=(128, 64))
    if use_method:
        ty = tx.transpose()          # 方法式
    else:
        ty = ct.transpose(tx)        # 函数式
    ct.store(y, index=(0, 0), tile=ty)
```

> 这段测试还顺带告诉你一个事实：transpose 不改变元素，只是把 `(128,64)` 的 tile 变成 `(64,128)`——所以 store 的目标数组 `y` 必须是 `(64,128)` 形状，store 的 `index=(0,0)` 仍指向 y 的 tile space 原点。

#### 4.3.4 代码实践

**实践目标**：在内核里把一维 tile reshape 成二维、转置后再观察形状变化（源码阅读型实践）。

**操作步骤**：

1. 阅读 [_stub.py:L2479-L2489](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2479-L2489) 的 transpose 示例：`arange(8).reshape(2,4)` 再 `transpose()` 得到 `[[0,4],[1,5],[2,6],[3,7]]`，即 `(4,2)`。
2. 在脑海（或纸面）上画一遍：`(2,4)` 的元素布局是行优先 `[[0,1,2,3],[4,5,6,7]]`，转置后行列互换。
3. 把上面的 `transpose_tile_2d_default_axes` 内核拿来，把 `shape=(128,64)` 改成 `shape=(64,128)`，预测 `tx.transpose()` 后的形状。

**需要观察的现象**：transpose 只换轴顺序，元素值不变；输出 tile 的形状是输入形状按交换轴后的结果。

**预期结果**：`shape=(64,128)` 的 tile 经 `transpose()` 变为 `(128,64)`。可对照测试断言验证（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：一个 `(16,2)` 的 tile，`reshape` 到 `(8,-1)` 后形状是什么？为什么？

**答案**：`(8,4)`。总元素数 32，第一个维指定为 8，则第二维 `-1` 自动推断为 \(32/8=4\)。注意 4 和 8 都是 2 的幂，校验通过。

**练习 2**：对三维 tile `(2,2,2)`，`permute(t,(2,0,1))` 与 `transpose(t,0,2)` 的结果形状分别是什么？

**答案**：`permute(t,(2,0,1))` 形状为 `(2,2,2)`（把原轴 2→新轴0、0→1、1→2；这里三维都是 2，形状数值不变但元素布局变了）。`transpose(t,0,2)` 只交换轴 0 和轴 2，形状也是 `(2,2,2)`，但元素布局不同于上面的 permute（permute 还动了中间轴）。形状数值相同不代表元素排列相同。

---

### 4.4 维度增减与拼接：broadcast_to、expand_dims、cat

#### 4.4.1 概念说明

除了 reshape/permute，还有一组「细粒度」形状操作：

- **broadcast_to(shape)**：把 tile 按 NumPy 广播规则**显式**扩到目标形状（不改数据，只加广播语义）。和算术里的隐式广播是同一套规则，但这里你主动声明。
- **expand_dims(axis)**：在指定位置插入一个大小为 1 的新轴。也支持 NumPy 风格语法 `x[:, None]`。
- **cat((t0, t1), axis)**：沿指定轴把两个 tile **拼接**成一个。

#### 4.4.2 核心流程

- `broadcast_to`：校验目标形状可由原形状广播得到（每维相等或原为 1），返回目标形状的新 tile，不拷贝实际数据。
- `expand_dims`：在 `axis` 处插入大小 1 的轴，`ndim` 加 1。
- `cat`：要求两个 tile 在**非拼接轴**上形状相同；拼接轴上的尺寸相加。**特别注意**：由于 cuTile 强制每维为 2 的幂，文档指出 `cat` 的两个输入必须形状完全相同（见下方源码），拼接后某维翻倍（仍为 2 的幂）。

#### 4.4.3 源码精读

**broadcast_to**：

[_stub.py:L2375-L2399](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2375-L2399) —— `broadcast_to(arange(4),(2,4))` 把 `(4,)` 显式扩成 `(2,4)`，两行相同。

**expand_dims（含 `x[:, None]` 语法糖）**：

[_stub.py:L2314-L2340](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2314-L2340) —— 同时演示 `expand_dims(tx,0)` 得 `[[0,1,2,3]]`、`tx[:,None]` 得 `[[0],[1],[2],[3]]`。

它通过 `Tile.__getitem__` 接入语法糖（见 [_stub.py:L612-L614](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L612-L614)）：`x[...]` 里的 `None` 即新增轴。

**cat（注意 2 的幂约束）**：

[_stub.py:L2343-L2372](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2343-L2372)：

```python
@stub
def cat(tiles, /, axis) -> Tile:
    """Concatenates two tiles along the `axis`.
    ...
    Notes:
        Due to power-of-two assumption on all tile shapes,
        the two input tiles must have the same shape."""
```

文档明确：因 2 的幂假设，两个输入 tile **必须形状相同**；沿 `axis` 拼接后该维加倍。

#### 4.4.4 代码实践

**实践目标**：用 `expand_dims` 把行/列向量对齐，再用算术广播完成一个「广播加法」，体会显式扩维与隐式广播的等价性。

**操作步骤**：

```python
# 示例代码
@ct.kernel
def broadcast_add(x_row, x_col, y):
    row = ct.load(x_row, index=(0,), shape=(1, 8))            # (1,8)
    col = ct.load(x_col, index=(0,), shape=(8,))              # (8,)
    col2d = col[:, None]                                      # expand_dims -> (8,1)
    ct.store(y, index=(0, 0), tile=row + col2d)               # 广播加 -> (8,8)
```

**需要观察的现象**：

- 不写 `col2d = col[:, None]`，直接 `row + col` 也能广播（`(1,8)` 与 `(8,)` 广播成 `(8,8)`）；`expand_dims` 只是让你把「想广播的轴」表达得更显式。
- `cat` 两输入必须同形：若想拼两个 `(4,)` 得 `(8,)`，两个输入都必须是 `(4,)`。

**预期结果**：`y[i,j] = row[0,j] + col[i]`（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`cat((t, t), axis=0)` 其中 `t` 形状 `(2,2)`，结果形状是什么？换成 `axis=1` 呢？

**答案**：`axis=0` 沿行拼接得 `(4,2)`；`axis=1` 沿列拼接得 `(2,4)`。两种结果各维仍是 2 的幂。

**练习 2**：想把一个 `(8,)` 的 tile 变成 `(1,8)`，有哪几种写法？

**答案**：至少三种：`ct.reshape(t,(1,8))`、`ct.expand_dims(t,0)`、`t[None, :]`（等价于 `t[:, None]` 的轴位置变体）。它们都只是形状视图变化，元素不变。

---

## 5. 综合实践：实现一个矩阵转置内核

把本讲三块内容（算术、astype、形状操作）与 u3-l1 的 load/store 串起来，完成本讲指定的实践任务：**把 \((M,N)\) 矩阵转置为 \((N,M)\)**。我们参考官方示例 [samples/Transpose.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/Transpose.py)。

### 5.1 思路

转置有两种 tile 内实现路径（任务要求二选一或都试）：

- **路径 A（permute/transpose）**：`ct.load` 出 \((t_m,t_n)\) 的 tile，用 `ct.transpose` 在 tile 内转置成 \((t_n,t_m)\)，再 `ct.store` 到输出数组的「交换后」tile 索引。
- **路径 B（load 的 order 参数）**：直接用 `ct.load(x, index=(j,i), shape=(t_n,t_m), order=(1,0))`，让 load 时就把数组轴置换，一步得到转置后的 tile（`order` 语义见 u3-l1 的 `load` 文档）。

下面给出路径 A 的内核（与官方示例一致）。

### 5.2 内核（基于 samples/Transpose.py）

核心三行——load、transpose、store：

[samples/Transpose.py:L46-L57](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/Transpose.py#L46-L57)：

```python
input_tile = ct.load(x, index=(bidx, bidy), shape=(tm, tn))   # (tm, tn)
transposed_tile = ct.transpose(input_tile)                     # -> (tn, tm)
ct.store(y, index=(bidy, bidx), tile=transposed_tile)          # 写到 y 的 (bidy,bidx)
```

其中 `bidx = ct.bid(0)`、`bidy = ct.bid(1)`（见 [samples/Transpose.py:L39-L40](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/Transpose.py#L39-L40)）。

**关键细节**：

1. 输入 `x` 是 \((M,N)\)，每个 block 取 \((t_m,t_n)\) 的 tile，所以 grid 在 M 方向有 \(\lceil M/t_m\rceil\) 个 block、N 方向有 \(\lceil N/t_n\rceil\) 个。
2. 输出 `y` 是 \((N,M)\)。转置后 tile 形状变成 \((t_n,t_m)\)，写入 `y` 时 tile 索引要**交换**成 `(bidy, bidx)`——因为 `bidy` 原本索引 N 方向，在 `y` 里成了行方向。

### 5.3 host 端启动

host 端算 grid 并启动（见 [samples/Transpose.py:L108-L120](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/Transpose.py#L108-L120)）：

```python
grid_x = ceil(m / tm)         # M 方向 block 数
grid_y = ceil(n / tn)         # N 方向 block 数
grid = (grid_x, grid_y, 1)
y = torch.empty((n, m), device=x.device, dtype=x.dtype)   # 输出 (N,M)
ct.launch(torch.cuda.current_stream(), grid, transpose_kernel, (x, y, tm, tn))
```

完整 wrapper（含按 dtype 选 tile 大小、输入校验）见 [samples/Transpose.py:L60-L122](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/Transpose.py#L60-L122)；示例的 `__main__` 还提供了 float16 / float32 / 非整除维度三种测试用例（见 [samples/Transpose.py:L125-L187](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/Transpose.py#L125-L187)）。

### 5.4 你的任务

1. 直接运行官方示例：`python samples/Transpose.py --correctness-check`，确认三种用例都打印 `Correctness check passed`。
2. **改用路径 B**：把内核里的 `ct.transpose(input_tile)` 换成一次带 `order=(1,0)` 的 `ct.load`（参考 u3-l1 `load` 文档里的转置示例），重新算 load 的 `shape` 与 `index`，使结果一致。
3. **加上 astype**：把内核改成读 `float32` 的 `x`、按 `float16` 写回 `y`（即 `ct.store(y, ..., tile=ct.astype(transposed_tile, ct.float16))`），host 端把 `y` 声明为 `float16`，验证数值在 half 精度范围内一致。
4. **挑战**：解释为什么部分越界的 tile（M 或 N 不是 \(t_m\)、\(t_n\) 的整数倍时）仍然能得到正确结果。（提示：回顾 u3-l1 的「部分越界 store 被忽略」。）

**预期结果**：路径 A 与路径 B 数值一致，均等于 `x.T`；astype 版本在 float16 精度内一致。若暂无 GPU 环境，路径 1–3 标注「待本地验证」并把断言代码写好。

---

## 6. 本讲小结

- cuTile 的算术是**逐元素**的，运算符（`+ * @ >` …）与函数（`ct.add/ct.mul/...`）等价——运算符只是 `Tile` 类双下划线方法的语法糖，转调同名函数。
- 二元算术统一做两件事：**形状广播**（NumPy 规则）与**类型提升**（u2-l4 的提升表），返回 `TileOrScalar`；结果都是**新 tile**，原 tile 不变。
- `astype` 做**数值类型转换**（受 `RoundingMode` 影响），要和**位重解释** `bitcast` 区分；`astype` 不改 shape。
- `reshape`（支持 `-1` 推断）、`permute`（全轴置换）、`transpose`（交换两轴，二维默认转置）改变形状/轴序但不改元素，且都返回新 tile；都受「每维 2 的幂」约束。
- `broadcast_to`/`expand_dims`（含 `x[:,None]`）/`cat` 是更细粒度的形状工具；`cat` 因 2 的幂约束要求两输入同形。
- **tile 不可变**是贯穿本讲的根本性质：所有算术与形状操作都是「产生新 tile」，对应底层 SSA 风格的 Tile IR（实现机制见 u5-l5/u5-l7）。

## 7. 下一步学习建议

- **u3-l3 控制流**：本讲的算术产生的是单条直线计算；下一讲讲 `if/for/while`，你会看到循环里如何用「携带值」把多步算术串起来，并理解为什么 tile 不可变性让循环分析变得简单。
- **u3-l4 归约与扫描**：本讲的 `minimum/maximum` 是逐元素的；归约 `ct.sum/max/min/argmax` 才会**消除维度**，是 LayerNorm、softmax 类内核的基础。
- **u3-l6 matmul 与张量核**：本讲的 `@`/`ct.matmul` 是高层矩阵乘；要榨干硬件，需要 `ct.mma` 与 `tfloat32`/`float16`，那时 `astype` 会再次登场。
- **若想深挖实现**：本讲所有 `@stub` 函数的后端 IR 实现注册机制在 u5-l7（`@stub` 与 `@impl`、`tile_impl_registry`）；算术对应的 IR Op 在 u5-l5。
