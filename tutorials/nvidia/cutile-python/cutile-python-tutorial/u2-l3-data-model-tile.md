# 数据模型：Tile、Scalar 与形状广播

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 **tile（瓦片）** 是什么：它是 kernel 内部 **不可变**、**形状在编译期已知**、**每一维都必须是 2 的幂** 的多维数据集合，并解释这三条约束各自的来源。
- 解释 **scalar（标量）就是零维 tile**：它的 `shape` 是空元组 `()`，字面量（如 `7`、`3.14`）默认是 *loosely typed*（松散类型）常量标量，因此 `a = 0; a.dtype` 在 cuTile 里能返回 `int32`。
- 用 NumPy 广播规则推导两个 tile 做算术运算后的结果 shape，并说明 cuTile 的广播语义 **和 NumPy 完全一致**。
- 说清 **element space（元素空间）** 与 **tile space（瓦片空间）** 的区别，以及 `ct.load(array, index, shape)` 是如何用 tile 索引定位元素的。

本讲承接 [u2-l2 全局数组 Array](u2-l2-data-model-array.md)。上一讲我们弄明白了「host 分配的全局数组」是 block 算的数据来源；本讲就回答下一层问题：**数组被 `ct.load` 取进 kernel 之后，变成了一个什么样的对象？**——答案是 tile。理解了 tile 的不可变性与「2 的幂」约束，你才能真正看懂后续所有的算术、归约、matmul 内核。

## 2. 前置知识

阅读本讲前，请先具备以下认知（来自前面几讲）：

- **全局数组（global array）** 放在 GPU 全局显存、由 host 分配、可读写；`shape`/`strides` 是运行时 `int32`，`dtype`/`ndim` 是编译期常量（详见 u2-l2）。
- cuTile 的执行单元是 **block**：tile 运算由整个 block 集体并行完成，不暴露单个线程（详见 [u2-l1 执行模型](u2-l1-execution-model.md)）。
- 一个内核由「`@ct.kernel` 装饰 + 函数体 + host 端 `ct.launch`」组成，函数体在 `launch` 时才 JIT 执行（详见 [u1-l2](u2-l1-execution-model.md)）。

下面几个名词是本讲的新术语，先给一句话直觉，后面会精读源码：

- **tile（瓦片）**：从全局数组里「切」出来的一小块，在 kernel 内部被集体计算；它不可变、形状编译期已知。
- **scalar（标量）**：零维 tile，只有一个元素；Python 字面量在 tile code 里自动被当成 scalar。
- **broadcasting（广播）**：让形状不同的两个 tile 一起做算术的规则，和 NumPy 一致。
- **element space / tile space**：前者是「数组所有元素」构成的空间，后者是「把数组切成等大 tile 后，所有 tile」构成的空间。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [docs/source/data.rst](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst) | 数据模型官方说明。本讲的概念基础几乎都在它的 "Tiles and Scalars"、"Element & Tile Space"、"Shape Broadcasting" 三节里。 |
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py) | `Tile` 类与 `ScalarProtocol` 的 Python 类型存根（type stub），定义了 tile 的 `dtype`/`shape`/`ndim` 属性与全部算术运算符重载。 |
| [src/cuda/tile/_ir/op_impl.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/op_impl.py) | 后端实现里的 `require_constant_shape`——真正用 `x & (x-1) != 0` 校验「每维为 2 的幂」的地方。 |

> 说明：和 [u2-l2](u2-l2-data-model-array.md) 一样，`_stub.py` 里的 `class Tile` 只是「签名壳」，负责前端类型检查与文档生成；真正的形状校验、算术降级发生在后端 IR（`_ir/`）。本讲为了讲清「2 的幂」约束，会越界引用一处后端实现 `require_constant_shape`，为这条规则提供源码证据。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Tile**——不可变、编译期形状、每维为 2 的幂；以及它从哪儿来（element/tile space）。
2. **Scalar**——零维 tile 与 loosely typed 常量。
3. **Broadcasting**——NumPy 式形状广播规则。

### 4.1 Tile：不可变的编译期形状值

#### 4.1.1 概念说明

一个 **tile** 是「某种 `dtype` 元素在多维空间里排布的 **不可变** 集合」。文档用一句话点明了它的三个本质属性：

> A *tile* is an **immutable** multidimensional collection of elements of a specific |dtype|. … The shape of a tile must be **known at compile time**. Each dimension of a tile must be a **power of 2**.

把这三条拆开看：

- **不可变（immutable）**：tile 一旦产生，它的元素值就不能被修改。所有「形状操作」（`reshape`/`permute`/`transpose`/`astype`）和算术运算（`+`/`*`/…）都 **返回一个新 tile**，绝不改原 tile。这和全局数组（可写、host 持有）形成鲜明对照。
- **形状编译期已知（compile-time shape）**：tile 的每一维长度必须是编译期常量。这就是为什么 `ct.load(array, index, shape=(tm,tn))` 的 `shape` 参数标着 `Constant[Shape]`——它要在编译时就确定。
- **每维为 2 的幂（power of 2）**：`1, 2, 4, 8, 16, …` 都合法；`3, 5, 6, 7, …` 都非法。这条约束来自底层硬件：GPU 的张量核、TMA、wipp 级别的集体运算都按 2 的幂对齐工作，把它作为语言级约束能让编译器直接映射到最快的硬件路径。

> 「不可变」是 tile 与全局数组最关键的区别，请牢记这张对照表：

| | 全局数组 Array | Tile |
| --- | --- | --- |
| 谁持有 | host 分配并传入 | kernel 内部产生 |
| 可变性 | **可读写** | **不可变** |
| shape | 运行时 `int32` | **编译期常量** |
| 每维约束 | 任意正整数 | **必须为 2 的幂** |
| 物理表示 | 真实显存 | 不一定有物理表示 |

最后一点「不一定有物理表示」很重要：tile 可以来自 `ct.load`（对应一次真实的显存读取），也可以来自 `ct.zeros`/`ct.full` 这类 **factory（工厂）函数**（编译期合成、可能只存在于寄存器里），还可以是某个算术运算的中间结果。

#### 4.1.2 核心流程

tile 的产生与消费，可以画成下面这条流：

```text
   全局数组 (host 分配, shape 运行时)                kernel 内部
┌──────────────────────────┐        ct.load        ┌──────────────────────┐
│  array: (M, N)           │  ───────────────────► │  tile: (tm, tn)       │  ← 不可变、
│  element space           │   按 tile 索引切一块   │  编译期 shape、2 的幂  │     编译期已知
└──────────────────────────┘                       └─────────┬────────────┘
                                                              │ 算术/形状操作
                                                              ▼ (返回新 tile, 不改原值)
                                                       ┌──────────────┐
                                                       │  new tile    │
                                                       └──────┬───────┘
                                              ct.store   写回  │
                                       ◄────────────────────────┘
   全局数组 (被 tile 覆盖写)
```

要理解「`ct.load` 怎么用 tile 索引切出一块」，必须先分清两个空间：

- **element space（元素空间）**：数组所有元素按某种布局（行优先/列优先）排成的多维空间。
- **tile space（瓦片空间）**：把数组按某个 `tile_shape` 切成等大瓦片后，所有瓦片排成的空间。

文档用一个例子说清二者的换算：对一个 `(M, N)` 的二维数组，用瓦片形状 `(tm, tn)` 切分，得到一个 `(cdiv(M, tm), cdiv(N, tn))` 的二维瓦片空间；瓦片索引 `(i, j)` 取出的是第 `i` 行、第 `j` 列那块 `(tm, tn)` 的元素，即：

\[
t[x, y] = \text{array}[i \cdot tm + x,\; j \cdot tn + y], \quad 0 \le x < tm,\; 0 \le y < tn
\]

其中 `cdiv` 是向上取整除法（cuTile 里的 `ct.cdiv`）。这也解释了为什么 `tm`/`tn` 必须是 2 的幂：它们既是瓦片形状，也是切分单元，硬件按这个粒度集体搬运数据。

> 这个「按瓦片索引定位」的模型还会在 [u4-l1 TiledView 与 persistent 遍历](u4-l1-tiled-view-and-persistent.md) 里以 `TiledView` 的形式更系统地展开；本讲只需建立直觉即可。

#### 4.1.3 源码精读

文档对 tile 三条属性的权威定义：

[docs/source/data.rst:82](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L82) — 定义 tile 是 **immutable**（不可变）的多维元素集合。

[docs/source/data.rst:88](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L88) — 明确写出「形状必须编译期已知，且每一维必须是 2 的幂」。

[docs/source/data.rst:108-112](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L108-L112) — tile 只能在 tile code 里使用；它的内容 **不一定有物理内存表示**；它由 `ct.load`/`ct.gather` 这类加载函数或 `ct.zeros` 这类 factory 函数产生。

`Tile` 类的存根定义在 `_stub.py`，它的核心属性与运算符如下：

[src/cuda/tile/_stub.py:532-560](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L532-L560) — `class Tile` 声明了三个只读属性 `dtype` / `shape` / `ndim`。注意 `shape` 的返回标注是 `tuple[const int, ...]`（**编译期常量**），这与全局数组 `Array.shape` 返回运行时 `int32` 形成对照——这正是「tile 形状编译期已知」在类型签名上的体现。

[src/cuda/tile/_stub.py:592-606](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L592-L606) — `reshape`/`permute`/`transpose`/`astype` 这些形状与类型操作 **都返回新的 `Tile`**，印证了「不可变」——它们从不就地修改 `self`。

[src/cuda/tile/_stub.py:616-704](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L616-L704) — 全部算术与比较运算符（`__add__`/`__mul__`/`__ge__`/…）也都返回新 `Tile`，例如 `def __add__(self, other) -> "Tile": return add(self, other)`。这些重载是 4.3 广播规则的入口。

「每一维必须是 2 的幂」这条约束，真正的校验代码在后端 `require_constant_shape`：

[src/cuda/tile/_ir/op_impl.py:412-433](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/op_impl.py#L412-L433) — 这个函数遍历 shape 的每一维：先保证为正（`x <= 0` 报 "is not positive"），再用经典的位运算技巧 `x & (x - 1) != 0` 判断是否为 2 的幂（只有 2 的幂才满足 `x & (x-1) == 0`），否则抛出 "is not a power of two"。

> 顺带一提，`cat`（拼接）的 docstring 里专门有一条 Notes：「Due to power-of-two assumption on all tile shapes, the two input tiles must have the same shape」——因为拼接后的维度仍必须是 2 的幂，所以 cuTile 的 `cat` 要求两个输入 tile 形状相同（见 [src/cuda/tile/_stub.py:2353-2356](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2353-L2356)）。这是「2 的幂」约束派生出来的一个实际限制。

#### 4.1.4 代码实践

**实践目标**：亲手触发一次「维度非 2 的幂」的编译失败，对照源码确认报错来自 `require_constant_shape`。

**操作步骤**：

1. 写一个最小内核，故意把 tile 形状设成 `3`（不是 2 的幂）：

```python
# 示例代码：故意制造「非 2 的幂」的 tile 形状
import cuda.tile as ct
import torch

@ct.kernel
def bad_kernel(x):
    t = ct.load(x, index=(0,), shape=(3,))   # shape=3 不是 2 的幂
    print(t)

x = torch.arange(8, device='cuda')
ct.launch(torch.cuda.current_stream(), (1,), bad_kernel, (x,))
```

2. 运行它，阅读报错信息。
3. 把 `shape=(3,)` 改成 `shape=(4,)`，确认能正常运行。

**需要观察的现象**：

- 第 1 步应在编译期（JIT 阶段）抛出类型错误，信息里应包含 "is not a power of two"。
- 第 3 步改回 `4` 后，内核正常打印 `[0, 1, 2, 3]`。

**预期结果**（待本地验证）：报错文本与 [src/cuda/tile/_ir/op_impl.py:430-431](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/op_impl.py#L430-L431) 的 `f"Dimension #{i} of {var_name} {tuple(shape)} is not a power of two"` 一致；改成 2 的幂后通过。

#### 4.1.5 小练习与答案

**练习 1**：tile 的三条核心约束是哪三条？分别带来什么后果？

> **参考答案**：（1）**不可变**——所有算术/形状操作都返回新 tile，不改原值（[src/cuda/tile/_stub.py:616-704](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L616-L704)）；（2）**形状编译期已知**——`ct.load` 的 `shape` 必须是常量，`Tile.shape` 返回 `const int`（[src/cuda/tile/_stub.py:544-551](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L544-L551)）；（3）**每维为 2 的幂**——`3` 这类维度会被 `require_constant_shape` 拒绝（[src/cuda/tile/_ir/op_impl.py:429-431](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/op_impl.py#L429-L431)）。

**练习 2**：对一个 `(M, N)` 数组用瓦片形状 `(tm, tn)` 切分，瓦片空间是多少维、每维多大？

> **参考答案**：瓦片空间是二维，形状为 `(cdiv(M, tm), cdiv(N, tn))`，即每个方向上「元素数除以瓦片大小」向上取整。瓦片索引 `(i, j)` 对应元素 `array[i*tm + x, j*tn + y]`（公式见 4.1.2）。

**练习 3**：为什么 `ct.cat` 要求两个输入 tile 形状必须相同，而不像 NumPy 那样允许不同长度拼接？

> **参考答案**：因为拼接后产生的维度仍必须是 2 的幂。若允许任意长度拼接，结果维度很容易不是 2 的幂（例如 `2 + 1 = 3`），违反 tile 形状约束。docstring 在 [src/cuda/tile/_stub.py:2353-2356](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2353-L2356) 明确说明了这一点。

---

### 4.2 Scalar：零维 Tile 与 loosely typed 常量

#### 4.2.1 概念说明

**scalar（标量）就是零维 tile**。它只有一个元素，`shape` 是空元组 `()`。文档写道：

> A zero-dimensional tile is called a *scalar*. … Numeric literals like `7` or `3.14` are treated as constant scalars, i.e. zero-dimensional tiles.

这意味着在 tile code 里，**Python 字面量（`7`、`3.14`、`True`）自动就是 scalar**，可以直接参与 tile 运算而不需要显式转换。例如 `t + 1` 里的 `1` 就是一个标量 tile，会按广播规则（4.3）自动扩展到 `t` 的形状。

scalar 和普通 Python `int`/`float` 有一个微妙但重要的区别：**scalar 是 tile，所以它有 `dtype` 和 `shape` 属性**。文档给了一个反直觉的例子——在 cuTile 里这段代码能跑：

```python
a = 0
a.dtype   # 在 cuTile 里返回 cuda.tile.int32；而在原生 Python 里会抛 AttributeError
```

这里的关键是 **loosely typed（松散类型）**：字面量 `0`、`2`、`3.14` 默认是「松散类型常量」——它们没有固定的具体 dtype，而是等参与运算时再按上下文决定（详见 [u2-l4 数据类型与类型提升](u2-l4-dtype-and-promotion.md)）。同样 loosely typed 的还有像 `Tile.ndim`、`Tile.shape`、`Array.ndim` 这类「常量属性」。

> 一句话区分：scalar = 零维 tile（有 shape `()`、有 dtype）；字面量 = loosely typed 的常量 scalar（dtype 待定）。它们都属于 tile 家族，所以能和任意 tile 一起做算术。

#### 4.2.2 核心流程

字面量/标量参与运算的判定流程：

```text
   字面量 7 / 3.14 / True
            │  (在 tile code 中)
            ▼
   当作 loosely typed 常量 scalar（零维 tile, shape=()）
            │
            │  与某个 tile t 做运算，例如 t + 7
            ▼
   把 scalar 按「2 的幂/广播」规则扩展到 t 的 shape
            │
            ▼
   得到与 t 同 shape 的结果 tile
```

要点：

- scalar 因为是零维（`shape == ()`），在任何广播场景里都 **永远可广播**（见 4.3 的规则：维度少则在左侧补 1，而零维补完仍是全 1），所以 `t + 7` 总是合法的。
- 若两个操作数 **都是** loosely typed 常量（如 `5 + 7`、`5 + 3.0`），结果是 **仍为 loosely typed 的常量**（分别是整型常量 `12`、浮点常量 `8.0`），不会被强制成具体 dtype；这一点会在 u2-l4 的「算术提升」里详细展开。

#### 4.2.3 源码精读

文档对 scalar 的定义与「字面量即常量标量」：

[docs/source/data.rst:94-96](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L94-L96) — 零维 tile 称为 scalar，只有一个元素、shape 为 `()`；字面量 `7`/`3.14` 被当作常量标量（零维 tile）。

[docs/source/data.rst:98-106](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L98-L106) — 说明 scalar 与 Python `int`/`float` 的区别：scalar 有 `dtype`/`shape` 属性，并给出 `a = 0; a.dtype` 返回 `cuda.tile.int32` 的例子。

[docs/source/data.rst:117-118](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L117-L118) — 标量常量默认是 loosely typed，例如字面量 `2`，以及 `Tile.ndim`、`Tile.shape`、`Array.ndim` 这类常量属性。

「两个 loosely typed 常量相加仍为 loosely typed 常量」的规则原文：

[docs/source/data.rst:226-230](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L226-L230) — `5 + 7` 是 loosely typed 整型常量 `12`，`5 + 3.0` 是 loosely typed 浮点常量 `8.0`。

在源码层面，`Tile` 与 `Scalar` 用一个联合类型 `TileOrScalar` 统一表达，几乎所有算术运算都接受它：

[src/cuda/tile/_stub.py:132](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L132) — `Scalar = int | float | ScalarProtocol`，把 Python 原生 `int`/`float` 与实现了 `ScalarProtocol`（含 `__add__`/`__mul__` 等运算符与 `__index__`）的对象都算作 scalar。

[src/cuda/tile/_stub.py:707](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L707) — `TileOrScalar = Union[Tile, Scalar]`。这就是为什么 `t + 1`（tile + 字面量）、`ct.store(arr, idx, tile=0)`（存标量）都合法——它们的参数类型就是 `TileOrScalar`。

[src/cuda/tile/_stub.py:22-130](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L22-L130) — `ScalarProtocol` 定义了 scalar 能做的全部运算（算术、比较、`__index__` 可当 range 索引）。注意 `__index__` 的 docstring「Scalar can be used as index in range」——这正是标量能做循环上界/下标的依据。

#### 4.2.4 代码实践

**实践目标**：在内核里验证「字面量是带 dtype 的 scalar」，并观察 scalar 与 tile 相加时的广播。

**操作步骤**：

1. 写一个内核，把一个字面量当作 scalar 来查询 `dtype`，再让它与一个 tile 相加：

```python
# 示例代码：观察字面量 scalar 的属性与广播
import cuda.tile as ct
import torch

@ct.kernel
def scalar_kernel(x):
    t = ct.load(x, index=(0,), shape=(4,))   # t: shape (4,) 的 int tile
    s = 7                                      # 字面量 -> loosely typed 常量 scalar (shape ())
    print("scalar dtype:", s)                  # 观察它被当成 tile 而非报错
    print("t + s:", t + s)                     # scalar 广播到 (4,) 后逐元素相加

x = torch.arange(8, dtype=torch.int32, device='cuda')
ct.launch(torch.cuda.current_stream(), (1,), scalar_kernel, (x,))
```

2. 运行，观察 `t + s` 的输出。

**需要观察的现象**：

- `s = 7` 不会报错（在原生 Python 里 `s.dtype` 会报 `AttributeError`，但 cuTile 里 `s` 是 scalar tile）。
- `t + s` 打印 `[7, 8, 9, 10]`——`s` 被广播到 `(4,)` 后逐元素加到 `[0,1,2,3]` 上。

**预期结果**（待本地验证）：`t + s` 输出 `[7, 8, 9, 10]`，证明标量字面量被当作零维 tile 并广播参与了运算。

#### 4.2.5 小练习与答案

**练习 1**：scalar 的 `shape` 是什么？它是几维 tile？

> **参考答案**：scalar 的 `shape` 是空元组 `()`，它是 **零维** tile，恰好只有一个元素（见 [docs/source/data.rst:94-96](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L94-L96)）。

**练习 2**：为什么在 cuTile 里 `a = 0; a.dtype` 能返回 `int32`，而在原生 Python 里会抛 `AttributeError`？

> **参考答案**：因为在 tile code 中字面量 `0` 被当作 loosely typed 常量 scalar（零维 tile），而 tile 都有 `dtype` 属性；原生 Python 的 `int` 对象没有 `dtype` 属性，故报错。原文见 [docs/source/data.rst:98-106](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L98-L106)。

**练习 3**：`5 + 3.0` 在 cuTile 里结果是什么类型？为什么不是 `float32`？

> **参考答案**：结果是 **loosely typed 浮点常量** `8.0`，而不是具体的 `float32`。因为两个操作数都是 loosely typed 常量时，结果仍是 loosely typed 常量，要等到与一个具体 dtype 的 tile 运算时才会被确定成具体类型（见 [docs/source/data.rst:226-230](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L226-L230)，细节留到 u2-l4）。

---

### 4.3 Broadcasting：NumPy 式形状广播

#### 4.3.1 概念说明

**形状广播（shape broadcasting）** 让两个形状不同的 tile 也能一起做算术：较小的 tile 会自动「扩展」到较大 tile 的形状。cuTile 的广播规则和 NumPy **完全一致**，文档列了三条：

1. **按末尾维度对齐（aligned by trailing dimensions）**：从最右边的维度开始一一对应。
2. **对应维度「相等或有一个是 1」才算兼容**：否则报错。
3. **维度数少的那方在左侧补 1**：例如 `(8,)` 与 `(4,8)` 运算时，`(8,)` 被当成 `(1,8)`。

广播 **不真的拷贝数据**——它只是告诉编译器「在运算时把那一维当成重复了若干次」，因此既简洁又高效。

> 因为广播只会把大小为 `1` 的维度「扩展」，而 `1` 本身就是 2 的幂，所以广播 **永远不会破坏** 4.1 的「每维为 2 的幂」约束——结果 shape 仍由各维中较大的（非 1）值决定，而那些值原本就都是 2 的幂。

#### 4.3.2 核心流程

给定两个 tile 形状 \(S_a\) 与 \(S_b\)，广播结果形状 \(S_r\) 的逐维计算规则：

\[
S_r[\,-k\,] = \begin{cases}
S_a[\,-k\,] & \text{若 } S_b[\,-k\,] = 1 \\
S_b[\,-k\,] & \text{若 } S_a[\,-k\,] = 1 \\
S_a[\,-k\,] & \text{若 } S_a[\,-k\,] = S_b[\,-k\,] \\
\text{报错} & \text{否则（如 } 4 \text{ vs } 8\text{）}
\end{cases}
\]

其中 \(-k\) 表示从末尾往前数第 \(k\) 维。先把较短的那个 shape 在 **左侧补 1** 到等长，再逐维套用上式。

用两个具体例子走一遍（这也是综合实践会用的例子）：

| 运算 | 左 shape | 右 shape | 对齐后 | 逐维取大 | 结果 shape |
| --- | --- | --- | --- | --- | --- |
| `(4,1) + (1,8)` | `(4,1)` | `(1,8)` | 已等长 | `max(4,1)=4`, `max(1,8)=8` | **`(4,8)`** |
| `(8,) + (4,8)` | `(8,)` | `(4,8)` | 左补成 `(1,8)` | `max(1,4)=4`, `max(8,8)=8` | **`(4,8)`** |
| `(4,) + (8,)` | `(4,)` | `(8,)` | 已等长 | `4 vs 8` 都非 1 且不等 | **报错** |

除了隐式广播，cuTile 还提供显式广播 `ct.broadcast_to(tile, shape)`，以及配合广播用的形状操作 `ct.expand_dims`（插入大小为 1 的新轴），它们都遵循同一套 NumPy 规则。

#### 4.3.3 源码精读

广播规则的三条权威定义：

[docs/source/data.rst:188-200](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L188-L200) — "Shape Broadcasting" 一节：按末尾维度对齐；对应维度相等或有一个为 1 才兼容；维度少的在左侧补 1；语义与 NumPy 一致。

显式广播与插轴的存根签名：

[src/cuda/tile/_stub.py:2375-2399](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2375-L2399) — `broadcast_to(x, shape)` 按 NumPy 广播规则把 tile 显式扩展到目标 shape；docstring 给的例子 `ct.broadcast_to(ct.arange(4), (2,4))` 把 `(4,)` 扩展成 `(2,4)`。

[src/cuda/tile/_stub.py:2314-2340](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2314-L2340) — `expand_dims(x, axis)` 在指定位置插入一个大小为 1 的新轴，正是为广播「对齐维度数」准备的工具；还支持 NumPy 风格的 `x[:, None]` 语法糖（见 `Tile.__getitem__` 的 [src/cuda/tile/_stub.py:612-614](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L612-L614)）。

隐式广播的入口是各算术运算符。例如 `gather`/`scatter` 的 docstring 直接用广播描述结果形状：

[src/cuda/tile/_stub.py:1583-1592](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L1583-L1592) — `gather` 的例子：索引 `ind0`/`ind1` 形状分别为 `(M,N,1)` 与 `(M,1,K)`，广播后结果 tile 形状为 `(M,N,K)`，正是按 4.3.2 规则算出来的。

#### 4.3.4 代码实践

**实践目标**：验证 `(4,1)` tile 与 `(1,8)` tile 相加的结果 shape 为 `(4,8)`，并理解广播如何省去显式扩展。

**操作步骤**：

1. 用 factory 函数构造两个不同形状的 tile 并相加：

```python
# 示例代码：验证 (4,1) + (1,8) -> (4,8)
import cuda.tile as ct

@ct.kernel
def bcast_kernel():
    a = ct.full((4, 1), 1, dtype=ct.int32)   # shape (4,1), 全 1
    b = ct.full((1, 8), 10, dtype=ct.int32)  # shape (1,8), 全 10
    c = a + b                                 # 广播 -> shape (4,8)
    print(c)

ct.launch(... , (1,), bcast_kernel, ())      # stream/grid 按你的环境填
```

2. 运行，观察 `c` 的形状与数值。
3. 把 `b` 改成 `ct.full((8,), 10, ...)`（一维 `(8,)`），确认仍得到 `(4,8)`（左侧补 1）。
4. 试着把 `b` 改成 `ct.full((3,), 10, ...)`，观察报错。

**需要观察的现象**：

- 第 2 步 `c` 是 `(4,8)` 的矩阵：每行都是 `[11, 11, …, 11]`（`a` 的 `1` 广播到 8 列，`b` 的 `10` 广播到 4 行，相加得 11）。
- 第 3 步 `(8,)` 自动左侧补 1 成 `(1,8)`，结果仍是 `(4,8)`。
- 第 4 步 `(3,)` 与 `(4,1)` 末尾维 `3 vs 1`→`3`、前一维 `（无） vs 4`→`4` 得 `(4,3)`，但 **`3` 不是 2 的幂**，应触发 4.1 的 power-of-two 报错。

**预期结果**（待本地验证）：第 2、3 步输出 `(4,8)` 全 11 矩阵；第 4 步报 "is not a power of two"（因为结果维度 `3` 违反 4.1 约束）。

#### 4.3.5 小练习与答案

**练习 1**：预测 `(4,1)` tile 与 `(1,8)` tile 相加后的结果 shape，并说明推导过程。

> **参考答案**：结果 shape 是 **`(4,8)`**。两 shape 等长，逐维取较大值：末尾维 `max(1,8)=8`，前一维 `max(4,1)=4`，得 `(4,8)`。数值上 `a`（全 1）沿列广播、`b`（全 10）沿行广播，相加为全 11。规则见 [docs/source/data.rst:195-197](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/docs/source/data.rst#L195-L197)。

**练习 2**：为什么把某个 tile 维度设成 `3` 会编译失败？失败发生在哪一步？

> **参考答案**：因为 tile 的每一维必须是 2 的幂，而 `3 & (3-1) = 3 & 2 = 2 != 0`，所以后端 `require_constant_shape` 在编译期校验形状时抛出 "is not a power of two"（[src/cuda/tile/_ir/op_impl.py:429-431](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/op_impl.py#L429-L431)）。这是 **编译期**（JIT）错误，不是运行期错误。

**练习 3**：`(4,)` tile 与 `(8,)` tile 直接相加会怎样？如何用 `expand_dims` 让它们能广播？

> **参考答案**：直接相加会 **报错**——末尾维 `4 vs 8` 既不相等也没有一个是 1。可以用 `expand_dims` 把 `(4,)` 变成 `(4,1)`、把 `(8,)` 变成 `(1,8)`（或 `(1,4)` 与 `(8,1)`），再相加就能广播成 `(4,8)`。`expand_dims` 定义见 [src/cuda/tile/_stub.py:2314-2340](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2314-L2340)。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「形状预测 + 约束验证」的小任务。

**任务**：在一个内核里依次做四件事，并在每一步 **先手算预测、再运行核对**：

1. 用 `ct.full` 构造 `a: (4,1)`（值 2）、`b: (1,8)`（值 3），相加得 `c`，预测 `c.shape` 与数值，再 `print(c)` 核对。
2. 构造标量 `s = 5`，计算 `c + s`，预测结果 shape 与数值（scalar 如何广播？）。
3. 用 `ct.expand_dims` 把一个 `(4,)` tile 变成 `(4,1)`，再与 `(1,8)` 相加，验证它等价于第 1 步。
4. **故意**把某次 `ct.full` 的 shape 写成 `(3,)`，确认它在编译期因「非 2 的幂」被拒绝，并核对你能从报错里找到 "power of two" 字样。

**提示**：

- 第 1 步：广播结果 `(4,8)`，数值 `2+3=5`（`a` 沿列、`b` 沿行广播）。
- 第 2 步：scalar 是零维 tile，广播到 `(4,8)`，结果全 `10`。
- 第 3 步：`expand_dims((4,), axis=1)` → `(4,1)`，与 `(1,8)` 广播仍得 `(4,8)`，与第 1 步一致——说明显式插轴和直接写 `(4,1)` 等价。
- 第 4 步：报错来自 [src/cuda/tile/_ir/op_impl.py:429-431](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/op_impl.py#L429-L431)，文本含 "is not a power of two"。

**预期结果**（待本地验证）：前三步的输出 shape/数值与你的手算预测完全一致；第四步在 JIT 阶段抛出含 "power of two" 的类型错误。完成本任务后，你应该能闭着眼睛回答：「tile 是什么、scalar 是什么、两个 tile 相加 shape 怎么算、为什么维度不能是 3」。

## 6. 本讲小结

- **tile 是 kernel 内部不可变的编译期形状值**：它由 `ct.load`/factory 函数产生，所有算术与形状操作都返回新 tile、不改原值；`Tile.shape` 返回 `const int`（与 `Array.shape` 的运行时 `int32` 对照）。
- **每一维必须是 2 的幂**：由后端 `require_constant_shape` 用 `x & (x-1) != 0` 在编译期校验，`3` 这类维度会被拒；连 `ct.cat` 都因此要求两输入形状相同。
- **scalar 就是零维 tile**：`shape` 为 `()`；Python 字面量在 tile code 里自动成为 loosely typed 常量标量，故 `a = 0; a.dtype` 返回 `int32`。
- **广播与 NumPy 完全一致**：末尾对齐、对应维「相等或有一个为 1」、维度少的左侧补 1；`(4,1)+(1,8)` 结果为 `(4,8)`。
- **广播不破坏 2 的幂约束**：广播只扩展大小为 1 的维度，而 1 本身是 2 的幂，结果各维仍是合法值。
- **element space vs tile space**：前者是数组元素空间，后者是「按瓦片形状切分后的瓦片」空间；瓦片索引 `(i,j)` 对应元素 `array[i*tm+x, j*tn+y]`。

## 7. 下一步学习建议

- 下一讲 [u2-l4 数据类型 DType 与类型提升](u2-l4-dtype-and-promotion.md) 会把本讲埋下的「loosely typed 常量」这条线讲透：`int8`/`float16`/`bfloat16`/`tfloat32`/`float8` 等类型族，以及 `5 + 3.0` 到底何时被确定成具体 dtype。建议和本讲 4.2 对照阅读。
- 想动手写第一个真正的 load–compute–store 内核，可进入 [u3-l1 load/store 与 load-compute-store 范式](u3-l1-load-store-pattern.md)——那里会把本讲的 tile/array/广播用到真实计算里。
- 想深入了解「按瓦片索引定位」的 `TiledView` 抽象（含重叠/间隔瓦片、persistent 遍历），可跳到 [u4-l1 TiledView 与 persistent 遍历](u4-l1-tiled-view-and-persistent.md)，它是本讲 4.1.2 element/tile space 的进阶版。
- 对「2 的幂」校验的完整调用链感兴趣的同学，可顺带阅读 [src/cuda/tile/_ir/op_impl.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/op_impl.py) 中 `require_constant_shape` 及其调用方，那里是 tile 形状约束的全部源头。
