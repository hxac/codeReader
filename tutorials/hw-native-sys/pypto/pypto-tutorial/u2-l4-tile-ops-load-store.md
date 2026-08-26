# Tile 级算子：load/store 与片上计算

## 1. 本讲目标

学完本讲，你应该能够：

1. 准确说出 `pl.load` / `pl.store` 每个参数的含义，特别是 **offsets（在源张量坐标系中移动）** 与 **shapes（恒为 Tile 窗口尺寸）** 的分工。
2. 理解 Tile 级算子为什么「直接映射片上指令」——一条 `tile.add` 对应一条向量指令，一条 `tile.matmul` 对应一次 Cube 单元乘法，中间没有隐藏的切分、暂存和调度。
3. 掌握 Cube 矩阵乘的多级存储链 `GM → Mat(L1) → Left/Right(L0A/L0B) → Acc(L0C)`，以及 `pl.move` 在链路中的角色。
4. 能用「分块循环 + K 维累加」的完整三段式，把一个大于单个 Tile 的矩阵乘手写出来，并用 torch 对照验证。

本讲是 u2-l3（Tensor 级算子）的镜像篇：上一讲你看到 Tensor 级只声明「算什么」，本讲你看 Tile 级如何把「怎么算」完全交给开发者自己。

## 2. 前置知识

### 2.1 Tensor 与 Tile：两类数据的回顾

- **Tensor**：活在全局内存（GM，即设备 DRAM）里的整块数组，尺寸可以很大，是算法视角的数据。
- **Tile**：活在片上存储里的**固定尺寸窗口**，是硬件视角的数据。一个 Tile 同时带有物理 shape 与 valid_shape（有效区域）两层信息。

Tile 级编程的全部内容，就是把数据在「GM ↔ 片上」之间搬进搬出，并在片上用最少的指令完成计算。

### 2.2 片上存储层级（读懂本讲必备）

以 Ascend 类硬件为例，从远到近：

| 层级 | 别名 / MemorySpace | 服务对象 | 典型容量级 |
| --- | --- | --- | --- |
| 全局内存 GM | — | 主机与设备共享 | GB 级 |
| 统一缓冲 UB | `MemorySpace.Vec` | 向量单元（AIV） | 百 KB 级 |
| L1 / Mat | `MemorySpace.Mat` | Cube 单元的数据 staging 区 | 百 KB 级 |
| L0A / L0B | `MemorySpace.Left` / `Right` | Cube 单元的左/右操作数专用缓冲 | 几十 KB |
| L0C / Acc | `MemorySpace.Acc`（由 matmul 结果隐式使用） | Cube 累加器 | 几十 KB |

离计算单元越近，容量越小、带宽越高。`pl.load` 默认把数据搬进 Vec/UB；要让 Cube 做矩阵乘，还得显式走 `Mat → Left/Right` 这条链（4.3 节展开）。

### 2.3 一个坐标系约定

`pl.load(a, offsets, shapes)` 读到的窗口满足：

\[ W[i,j] = T[o_r+i,\ o_c+j]\quad (0 \le i < h,\ 0 \le j < w) \]

其中 \((o_r, o_c)\) 是 offsets，\((h, w)\) 是 shapes。**窗口形状不变，让原点在源张量坐标系里滑动**——这就是分块循环的全部秘密。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/pypto/language/op/tile_ops.py` | Tile 级算子的 DSL 包装层：`load`/`store`/`move`/`create` 与全部 `tile.*` 计算算子，全部收进 `__all__`（约 150 个名字） |
| `python/pypto/language/__init__.py` | 把 `load`/`store`/`move` 等 Tile 专属算子直接提升为 `pl.*`；把 `add`/`mul`/`matmul` 等从统一分发层引入 |
| `python/pypto/language/op/unified_ops.py` | `pl.add`/`pl.matmul` 这类「同名双态」算子的按类型分发器：Tensor 走 `tensor.*`，Tile 走 `tile.*` |
| `python/pypto/language/dsl_api.py` | `pl.range` 循环迭代器的定义，分块循环的载体 |
| `examples/beginner/02_elementwise.py` | 本讲主线示例：`load → add/mul → store` 三段式 + 分块循环 `chunked_add` |
| `examples/beginner/05_matmul.py` | Cube 矩阵乘的最小示例：完整多级存储链 |
| `examples/intermediate/04_matmul_acc.py` | K 维分块累加的标准范式：首块 `matmul` 初始化，后续 `matmul_acc` 累加 |
| `docs/en/api/tile.md` | `pl.tile` 命名空间的 API 文档入口（mkdocstrings 自动生成） |

## 4. 核心概念与源码讲解

### 4.1 pl.load / pl.store：全局内存与片上世界的大门

#### 4.1.1 概念说明

`pl.load` 把 GM 中 Tensor 的一个矩形窗口**复制**到片上，返回一个 Tile；`pl.store` 反向把 Tile 写回 GM 的指定位置。它们是 Tile 级编程中仅有的两条 GM 数据通路，也是「显式数据搬运」这一性能代价的显性化——你在 Tensor 级看不到搬运，是因为编译器（`ConvertTensorToTileOps` 等 Pass）替你插入了这两条指令。

两个函数都是 **Tile 专属算子**：在语言层被直接从 `tile_ops` 提升为 `pl.load` / `pl.store`，没有 Tensor 态的歧义。

#### 4.1.2 核心流程

```text
pl.load(tensor, offsets, shapes, valid_shape?, target_memory?, clamp?)
  1. valid_shape 缺省时取 shapes（整窗都有效）
  2. 把 offsets/shapes/valid_shape 中的 Scalar 解包为 IR 表达式
  3. 构造 tile.load IR Call，返回包装该 Call 的 DSL Tile

pl.store(tile, offsets, output_tensor, shapes?, atomic?)
  1. 解包坐标与张量
  2. 构造 tile.store IR Call（atomic 选择覆写还是原子累加）
  3. 返回以该 Call 为表达式的 Tensor（即输出张量的新 SSA 值）
```

参数语义（务必记住）：

| 参数 | 含义 |
| --- | --- |
| `offsets` | 窗口**左上角**在**源张量坐标系**中的位置，长度等于维度数 |
| `shapes` | 要搬运的区域的**窗口尺寸**——循环分块时它恒等于 Tile 尺寸，只有 offsets 在动 |
| `valid_shape` | 窗口内的有效区域，缺省等于 shapes；用于源尾部不足一整块时「声明实际有多少真数据」 |
| `target_memory` | 落到哪个片上级：`Vec`（默认留给编译器）/ `Mat`（喂 Cube） |
| `clamp` | 读越界时的处理：默认拒绝；`clamp=True` 截回源边界 |

#### 4.1.3 源码精读

**load 的签名与文档**——[python/pypto/language/op/tile_ops.py:374-L424](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L374-L424)：`load(tensor, offsets, shapes, valid_shape=None, target_memory=None, clamp=False)`。docstring 明确写下坐标系约定：`offsets: Offsets in each dimension. Always in the source tensor's coordinate system.`（offsets 永远在源张量坐标系中），以及「只读有效范围，Tile 可以比源里真实存在的区域大」——valid_shape 的存在让一个物理 Tile 能服务不满块的尾部。

**valid_shape 缺省逻辑**——[python/pypto/language/op/tile_ops.py:414-L423](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L414-L423)：`if valid_shape is None: valid_shape = shapes`，随后把三个坐标序列经 `_normalize_intlike` 解包（其中 Scalar 元素被展开为 IR 表达式，[L273-L275](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L273-L275)）后构造 `tile.load` Call。这就是「循环变量是 Scalar 也能当偏移用」的实现基础。

**store 的签名与 atomic**——[python/pypto/language/op/tile_ops.py:427-L470](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L427-L470)：`store(tile, offsets, output_tensor, shapes=None, *, atomic=...)`。默认 `AtomicType.None_` 直接覆写；`AtomicType.Add` 把 Tile 原子累加进 GM 已有内容——文档点明这是 **split-K 多核累加** 的用法，并警告跨核浮点累加顺序不固定、目标须先清零。返回值是 `output_tensor.__class__(expr=call_expr)`：按调用方传入的具体张量类（含 DistributedTensor 子类）重建一个包装对象，这正是文件头 [L193-L199](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L193-L199) 用 bound TypeVar 解释的设计。

**提升到 pl.* 命名空间**——[python/pypto/language/__init__.py:120-L154](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/__init__.py#L120-L154)：`from .op.tile_ops import (..., load, ..., move, ..., store, ...)`。所以 `pl.load` 与 `pl.tile.load` 是同一个函数。

**三段式的最小完整例子**——[examples/beginner/02_elementwise.py:39-L46](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/02_elementwise.py#L39-L46)：

```python
@pl.jit
def tile_add_128(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        tile_a = pl.load(a, [0, 0], [128, 128])
        tile_b = pl.load(b, [0, 0], [128, 128])
        tile_c = pl.add(tile_a, tile_b)
        pl.store(tile_c, [0, 0], c)
    return c
```

三行分别对应搬运进、片上算、搬运出。注意 `pl.store` 的返回值在这里被丢弃——写回的副作用发生在设备侧，`return c` 返回的是 `pl.Out` 参数。

**分块循环：offsets 滑动、shapes 不动**——[examples/beginner/02_elementwise.py:81-L95](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/02_elementwise.py#L81-L95)：

```python
@pl.jit
def chunked_add(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        for i in pl.range(ROWS // TILE_ROWS):
            tile_a = pl.load(a, [i * TILE_ROWS, 0], [TILE_ROWS, COLS])
            tile_b = pl.load(b, [i * TILE_ROWS, 0], [TILE_ROWS, COLS])
            pl.store(pl.add(tile_a, tile_b), [i * TILE_ROWS, 0], c)
    return c
```

512×128 的张量装不进一个 128×128 的 Tile，于是按行切成 4 块。循环体里 `shapes` 恒为 `[TILE_ROWS, COLS]`，只有 `offsets` 的行分量 `i * TILE_ROWS` 在动（`i` 是 Scalar，`i * TILE_ROWS` 是标量表达式——load 内部会把它解包成 IR 表达式）。

#### 4.1.4 代码实践

**实践目标**：体会「窗口尺寸固定、原点滑动」的坐标系约定。

1. 运行基线：`python examples/beginner/02_elementwise.py`，确认输出 `OK`。
2. 把文件顶部的 `TILE_ROWS` 从 `128` 改为 `64`（`ROWS = 512` 不变）。
3. 重新运行。

**需要观察的现象**：程序仍然输出 `OK`——`chunked_add` 对分块尺寸完全无关紧要，因为循环次数 `ROWS // TILE_ROWS` 自动从 4 变成 8，每块窗口 `[64, 128]` 仍在源坐标系内无缝滑动。

**预期结果**：4 次 128×128 搬运变成 8 次 64×128 搬运，正确性不变。（运行结果待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`pl.load(a, [32, 16], [64, 64])` 读到的窗口里，`W[0, 0]` 和 `W[63, 63]` 分别对应源张量 `a` 的哪个元素？

答案：`W[0,0] = a[32, 16]`，`W[63,63] = a[32+63, 16+63] = a[95, 79]`。窗口元素与源的映射是 \( W[i,j] = T[o_r+i,\ o_c+j] \)。

**练习 2**：为什么 `chunked_add` 里 `pl.load` 的第三个参数始终写 `[TILE_ROWS, COLS]` 而不是写源张量的完整形状 `[512, 128]`？

答案：第三个参数是 shapes（窗口尺寸），描述「这一次搬多大」，必须等于片上 Tile 的尺寸；源张量总形状由 offsets 的滑动范围（循环边界 `ROWS // TILE_ROWS`）隐式覆盖。写成 `[512, 128]` 意味着要求一个 512×128 的片上 Tile，超出硬件容量。

**练习 3**：`store` 的 `atomic=pl.AtomicType.Add` 用在什么场景？文档提示了哪两个附带义务？

答案：split-K——多个核把各自的部分积累加进同一块 GM 输出。义务：跨核浮点累加顺序不定导致结果不确定；目标区域必须先清零。

### 4.2 tile.* 计算算子：片上计算的完整武器库

#### 4.2.1 概念说明

数据进了片上，接下来全部计算都由 `tile.*` 算子完成。这一层的特点是**一一映射硬件指令**：`tile.add` 对应一条向量加法指令，`tile.matmul` 对应一次 Cube 乘法，`tile.ci` 直接标注了它映射到 `pto.tci`。没有自动广播推导、没有隐藏的临时变量分配——需要暂存区的算子（如按行归约）会**显式要求你传入 `tmp_tile`**。

这种「显式」正是 Tile 级的性能价值：你在源码里看到的每一条调用，就是设备上将要执行的每一条指令，寄存器与片上缓冲的占用完全可预测。

#### 4.2.2 核心流程

`tile.*` 算子按功能分组（对应 `__all__` 中的清单）：

| 分组 | 代表算子 | 备注 |
| --- | --- | --- |
| 搬运/生命周期 | `load` / `store` / `move` / `create` / `full` | 4.1 与 4.3 节 |
| 片上生成 | `ci`(=arange) / `tri` / `random` / `full` | 不读 GM，直接在片上产生数据 |
| 逐元素算术 | `add` / `sub` / `mul` / `div` / `neg` / `exp` / `sqrt` / `cast` … | 标量右操作数走 `adds`/`muls`/… 变体 |
| 比较与位运算 | `cmp` / `and_` / `or_` / `xor` / `shl` … | 部分需要 `tmp` |
| Cube 乘法 | `matmul` / `matmul_acc` / `matmul_bias` / `gemv` / `matmul_mx` | 见 4.3 节 |
| 归约 | `row_sum` / `row_max` / `col_sum` / `row_argmax` … | row_* 沿最后一维、col_* 沿第 0 维；多数需 `tmp_tile` |
| 广播扩展 | `row_expand_*` / `col_expand_*` / `expands` | 向量沿行/列铺开参与运算 |
| 视图与形状 | `slice` / `reshape` / `transpose` / `transpose_view` / `set_validshape` | 零拷贝或带拷贝各异 |
| 散聚 | `gather*` / `scatter*` / `assemble` / `concat` | 按索引搬数据 |

调用链（以 `pl.add(tile_a, tile_b)` 为例）：

```text
pl.add                     （unified_ops 分发器）
  ├─ isinstance 检查: 两个操作数都是 Tile
  └─ _tile.add(lhs, rhs)   （tile_ops 包装层）
       ├─ _unwrap_rhs: Tile/Scalar → IR Expr
       └─ _ir_ops.add(...)  （IR 层构造 tile.add Call）
            └─ 返回 Tile(expr=call_expr)
```

#### 4.2.3 源码精读

**算子清单**——[python/pypto/language/op/tile_ops.py:22-L165](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L22-L165)：`__all__` 列出全部 Tile 算子。数一遍你会发现这是 DSL 中最大的一张算子表——因为硬件指令有多少种能力，这一层就该有多少个入口。

**标量右操作数规范化**——[python/pypto/language/op/tile_ops.py:957-L971](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L957-L971)：`add` 的签名是 `rhs: Tile | int | float | Scalar | Expr`，docstring 写明 `A scalar rhs canonicalizes to tile.adds`。实现上 `_unwrap_rhs`（[L268-L270](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L268-L270)）只是把包装类型解开，真正的 adds/muls 分流发生在更底层的 IR 构造里——这就是 u1-l5 里「Tile 乘标量路由到 `tile.muls` 立即数内嵌」的源头。

**显式暂存区：归约算子**——[python/pypto/language/op/tile_ops.py:1489-L1504](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1489-L1504)：`row_sum(tile, tmp_tile)` **必须**传一个同 dtype、同秩且各维不小于输入的暂存 Tile——硬件做行归约需要一块已知的工作区，DSL 不替你偷偷分配。对比 Tensor 级 `tensor.row_sum` 只需要一个参数，两者的抽象高度差一目了然。

**dtype 从严：超越函数**——[python/pypto/language/op/tile_ops.py:1114-L1127](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1114-L1127)：`sin` 标注 `FP32 only`，非 FP32 输入**直接拒绝而不是自动提升**，docstring 指路 `pl.cast(tile, pl.FP32)`。Tile 级没有隐式类型提升——每一次 dtype 变化都必须是你亲手写的一条 `tile.cast`（[L1229-L1249](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1229-L1249)）。

**双态分发的 Tile 侧**——[python/pypto/language/op/unified_ops.py:992-L1036](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L992-L1036)：`pl.matmul` 按操作数类型分流；Tile 分支（[L1026-L1035](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L1026-L1035)）里，`a_trans`/`b_trans`/`c_matrix_nz` 这三个 Tensor 专属旗标会被**显式拒绝**——docstring（[L1002-L1005](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L1002-L1005)）给出原因：**在 Tile 级，转置是类型属性而不是算子旗标**（转置后的 Tile 有自己的布局类型，用 `pl.tile.transpose` / `transpose_view` 显式构造）。

**API 文档入口**——[docs/en/api/tile.md:1-L11](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/docs/en/api/tile.md#L1-L11)：`pl.tile` 命名空间的文档由 mkdocstrings 从 `pypto.language.tile` 的 docstring 自动生成，并链接到「如何选择命名空间」的用户文档。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：建立对算子表的「分类直觉」，并验证 s 后缀规则。

1. 打开 [python/pypto/language/op/tile_ops.py:22-L165](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L22-L165)，把 `__all__` 里的算子按上面 4.2.2 的九个分组数一数，记下每组的数量。
2. 找出所有**必须传 `tmp_tile` / `tmp`** 的算子（提示：搜 `tmp_tile: Tile`、`tmp: Tile`），数出个数。
3. 写一个 5 行以内的小内核验证 s 后缀：`pl.mul(tile_a, 2.0)`（tile_a 为 `pl.load` 得到的 Tile），用 `kernel.compile()` 或 dump IR 的方式查看生成的算子名。

**需要观察的现象**：第 3 步生成的 IR 里算子名是 `tile.muls` 而不是 `tile.mul`——标量右操作数被规范化到了立即数内嵌的变体。

**预期结果**：约 150 个算子中，归约、位运算一族（`xor`/`rem` 等）和 `prelu` 等显式要求暂存区；乘标量路由到 `tile.muls`。（第 1、2 步数量待你本地数出；第 3 步现象待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：Tensor 级 `tensor.row_sum(x)` 和 Tile 级 `tile.row_sum(x, tmp)` 差了一个参数，这个参数为什么在 Tile 级必须由用户提供？

答案：Tile 级算子一一映射硬件指令，硬件行归约需要一块显式工作区；片上缓冲是稀缺资源，DSL 不做隐式分配，让开发者对占用完全可控。Tensor 级由后续降级 Pass（如内存规划三部曲）自动插入暂存。

**练习 2**：`pl.sin(tile_fp16)` 会发生什么？正确的写法是什么？

答案：被拒绝（非 FP32 输入不自动提升）。正确写法：`pl.sin(pl.cast(tile_fp16, pl.FP32))`。Tile 级没有隐式 dtype 提升。

**练习 3**：`pl.matmul(tile_a, tile_b, a_trans=True)` 为什么报错？转置在 Tile 级如何表达？

答案：Tile 级的转置是类型属性（转置 Tile 有自己的布局），不是算子旗标；要用 `pl.tile.transpose`（数据搬运式）或 `pl.tile.transpose_view`（NZ↔ZN 零拷贝重解释）先构造转置 Tile，再喂给 `pl.matmul`。

### 4.3 pl.move 与多级存储链：把矩阵乘喂给 Cube 单元

#### 4.3.1 概念说明

向量算术（`add`/`exp`/…）在 UB 里就能做，但**矩阵乘不归向量管**——它由独立的 Cube（矩阵）单元执行，操作数必须位于 L0A/L0B 专用缓冲，结果落在 L0C 累加器。`pl.load` 的 `target_memory` 只能把数据送到 `Mat`（L1），从 L1 到 L0A/L0B 的最后一公里要靠 **`pl.move`** 显式搬运。

于是 Cube matmul 的标准前置是一条四级链：

```text
GM --pl.load(target_memory=Mat)--> L1 --pl.move(Left/Right)--> L0A/L0B --matmul--> L0C --pl.store--> GM
```

为什么要分两跳而不是 load 直达 L0？因为 L0A/L0B 是 Cube 私有的窄缓冲，硬件只接受从 L1 出发的搬运；而且 L1 这一级还承担「GM 形状 → Cube 分形（fractal）布局」的整形职责。

另一个关键规则：**`tile.matmul` 的结果 dtype 由 Cube 累加器决定**——浮点操作数恒为 FP32，整型操作数恒为 INT32。FP16 输入、FP32 累加不是可选项，而是硬件固有行为。

#### 4.3.2 核心流程

以 `05_matmul.py` 的 64×64 乘法为例：

```text
1. pl.load(a, [0,0], [64,64], target_memory=Mat)   → A 的 L1 副本
2. pl.load(b, [0,0], [64,64], target_memory=Mat)   → B 的 L1 副本
3. pl.move(a_l1, Left)                              → 搬进 L0A
4. pl.move(b_l1, Right)                             → 搬进 L0B
5. pl.matmul(a_l0a, b_l0b)                          → L0C 上的 FP32 结果 Tile
6. pl.store(tile_c, [0,0], c)                       → 写回 GM
```

对应的数值过程：

\[ C = A \cdot B,\qquad C \in \mathbb{R}^{M\times N},\ \ A \in \mathbb{R}^{M\times K},\ B \in \mathbb{R}^{K\times N} \]

其中间累加发生在 FP32（L0C 累加器精度），与输入自身的 FP16/FP32 无关。

#### 4.3.3 源码精读

**示例的内存层级自述**——[examples/beginner/05_matmul.py:10-L22](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/05_matmul.py#L10-L22)：模块 docstring 把层级写成一行 `GM -> Mat (L1) -> Left/Right (L0A/L0B) -> matmul -> Acc (L0C)`，并注明「cube basics」。

**完整链条的代码**——[examples/beginner/05_matmul.py:29-L38](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/beginner/05_matmul.py#L29-L38)：

```python
@pl.jit
def matmul_64(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        tile_a_l1 = pl.load(a, [0, 0], [64, 64], target_memory=pl.MemorySpace.Mat)
        tile_b_l1 = pl.load(b, [0, 0], [64, 64], target_memory=pl.MemorySpace.Mat)
        tile_a_l0a = pl.move(tile_a_l1, target_memory=pl.MemorySpace.Left)
        tile_b_l0b = pl.move(tile_b_l1, target_memory=pl.MemorySpace.Right)
        tile_c_l0c = pl.matmul(tile_a_l0a, tile_b_l0b)
        pl.store(tile_c_l0c, [0, 0], c)
    return c
```

六行代码与 4.3.2 的六步一一对应。变量名 `_l1` / `_l0a` / `_l0b` / `_l0c` 是项目示例自觉遵守的命名约定，值得模仿。

**pl.move 的签名**——[python/pypto/language/op/tile_ops.py:615-L639](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L615-L639)：`move(tile, target_memory, blayout=None, slayout=None)`，目标空间枚举包含 `Vec / Mat / Left / Right / LeftScale / RightScale`（后两个服务 MX 缩放数据）。可选的 block/scatter 布局参数留待高级用法。

**matmul 的包装**——[python/pypto/language/op/tile_ops.py:1252-L1263](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1252-L1263)：`matmul(lhs, rhs)` 只是薄薄一层——unwrap 两个操作数、构造 IR Call、重新包装成 Tile。性能语义全在「操作数必须在 Left/Right」这一前置条件里。

**结果 dtype 规则的文档**——[python/pypto/language/op/unified_ops.py:1013-L1016](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/unified_ops.py#L1013-L1016)：`tile.matmul` 的结果 dtype 由 Cube 累加器固定（浮点 FP32、整数 INT32），Tensor 专属的 `out_dtype` 在 Tile 路径上只有与该推导一致时才被接受，否则报错。**FP16 进、FP32 出是写死的行为。**

**matmul_acc：带累加的矩阵乘**——[python/pypto/language/op/tile_ops.py:1283-L1310](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1283-L1310)：`matmul_acc(acc, lhs, rhs, init_cond=None)` 计算 `acc += lhs @ rhs`，直接累加在 L0C 上，省掉一次「读出-相加-写回」的往返。`init_cond` 参数（[L1288-L1294](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/op/tile_ops.py#L1288-L1294)）在条件为真的步上改为覆写而不是累加——这是 split-K 惯用法的官方形态，可以省掉累加器清零或首轮剥离。

#### 4.3.4 代码实践

**实践目标**：验证「target_memory 缺省留给编译器」与「Mat 显式指定」两条路径都可行，并画出内存链。

1. 运行基线：`python examples/beginner/05_matmul.py`，确认 `OK`。
2. 复制 `matmul_64` 为 `matmul_64_auto`，删去两个 `pl.load` 的 `target_memory=pl.MemorySpace.Mat` 参数（保留 `pl.move` 不变）。
3. 用两个随机 FP32 64×64 张量调用它并 `torch.allclose` 对照。

**需要观察的现象**：`target_memory=None` 时 load 落点未指定（文档说明默认留给编译器放置），`pl.move(..., Left/Right)` 仍然把操作数送到 Cube 缓冲。

**预期结果**：两种写法数值结果一致；链路图上唯一的差别是第一跳的目的地由「显式 Mat」变成「编译器决定」。（运行结果待本地验证——若编译器拒绝从非 Mat 空间 move 到 Left/Right，把观察到的报错记下来，这本身就是一个有价值的实验数据。）

#### 4.3.5 小练习与答案

**练习 1**：为什么示例不写成 `pl.load(a, [0,0], [64,64], target_memory=pl.MemorySpace.Left)` 一步到位？

答案：`load` 的 `target_memory` 只支持 `Vec` / `Mat` 两类落点（见 load 文档的 Args 说明）；L0A/L0B 只接受从 L1 出发的 `pl.move` 搬运，且这一跳同时承担布局整形（GM 行主序 → Cube 分形布局）。

**练习 2**：FP16 的 A、B 做一次 `pl.matmul`，结果 Tile 的 dtype 是什么？如果想输出 FP16 该怎么办？

答案：FP32——Cube 累加器 dtype 固定，浮点输入恒 FP32。输出 FP16 需要显式 `pl.cast(acc, pl.FP16)` 后再 `pl.store` 到 FP16 张量。

**练习 3**：`pl.move(tile, pl.MemorySpace.Vec)` 在什么场景下有用？

答案：把 Cube 产出的数据（或 L1 staging 数据）送到统一缓冲，交给向量单元做后处理（如激活函数）——融合算子里「matmul → 向量激活」的衔接指令。

### 4.4 分块循环与 K 维累加：Tile 三段式的完整形态

#### 4.4.1 概念说明

真实的矩阵远大于一个 Tile。把 4.1 的分块循环和 4.3 的存储链合起来，就得到 Tile 级编程最经典的形态：**三重分块 + K 维累加**。

把矩阵乘按 Tile 尺寸 \(M_T \times K_T\)、\(K_T \times N_T\) 切开后，求和式被拆成两层：

\[ C[m,n] \;=\; \sum_{k=0}^{K-1} A[m,k]\,B[k,n] \;=\; \sum_{b=0}^{K/K_T-1}\ \underbrace{\sum_{j=0}^{K_T-1} A[m,\,bK_T{+}j]\,B[bK_T{+}j,\,n]}_{\text{第 } b \text{块的部分积 } P_b[m,n]} \]

每个 \(P_b\) 恰是一次 `tile.matmul`（在 L0C 上产生），跨块求和用 `tile.matmul_acc` 在 L0C 内原地累加：

\[ \text{acc}_0 = P_0,\qquad \text{acc}_b = \text{acc}_{b-1} + P_b \]

这样 K 维无论多长，片上只需要 \(M_T \times K_T\)、\(K_T \times N_T\)、\(M_T \times N_T\) 三块缓冲。

#### 4.4.2 核心流程

```text
for m0 in 行分块:                     # offsets 行滑动
  (for n0 in 列分块:)                 # N 不超过 N_TILE 时可省
    k = 0 块:  load A[m0:m0+M_T, 0:K_T] / B[0:K_T, n0:n0+N_T]
                move → Left/Right → matmul          # acc 初始化（进 L0C）
    k = 1..K/K_T-1 块:
                load → move → matmul_acc             # 原地累加
    store(acc, [m0, n0], c)                          # 最后一次性写回 GM
```

要点：**累加发生在片上（L0C），store 只在整列 K 走完后发生一次**。如果每个 k 块都 store 一次再从 GM 读回相加，性能会差一个数量级。

#### 4.4.3 源码精读

**K 分块累加的教学版**——[examples/intermediate/04_matmul_acc.py:29-L52](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/intermediate/04_matmul_acc.py#L29-L52)：64×64 的乘法被拆成两个 K=32 块。

首块初始化（[L37-L42](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/intermediate/04_matmul_acc.py#L37-L42)）：

```python
tile_a0_l1 = pl.load(a, [0, 0], [64, 32], target_memory=pl.MemorySpace.Mat)
tile_b0_l1 = pl.load(b, [0, 0], [32, 64], target_memory=pl.MemorySpace.Mat)
tile_a0_l0a = pl.move(tile_a0_l1, target_memory=pl.MemorySpace.Left)
tile_b0_l0b = pl.move(tile_b0_l1, target_memory=pl.MemorySpace.Right)
acc: pl.Tile[[64, 64], pl.FP32] = pl.matmul(tile_a0_l0a, tile_b0_l0b)
```

注意两个细节：A 取 `A[:, 0:32]`（offsets `[0,0]`、shapes `[64,32]`），B 取 `B[0:32, :]`（offsets `[0,0]`、shapes `[32,64]`）——K 维在 A 的列、B 的行上同时被切。以及局部变量的**类型注解** `acc: pl.Tile[[64, 64], pl.FP32]`，显式声明累加器是 FP32（与 Cube 累加器规则呼应）。

第二块累加（[L44-L49](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/intermediate/04_matmul_acc.py#L44-L49)）：

```python
tile_a1_l1 = pl.load(a, [0, 32], [64, 32], target_memory=pl.MemorySpace.Mat)
tile_b1_l1 = pl.load(b, [32, 0], [32, 64], target_memory=pl.MemorySpace.Mat)
...
acc = pl.matmul_acc(acc, tile_a1_l0a, tile_b1_l0b)
```

A 的列偏移走到 `[0, 32]`，B 的行偏移走到 `[32, 0]`——**K 轴在两个操作数上同步滑动**。`pl.matmul_acc` 把部分积累进已有的 acc。

收尾（[L51](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/intermediate/04_matmul_acc.py#L51)）：`pl.store(acc, [0, 0], c)`——两个 K 块全部累加完才写回 GM 一次。

**循环内累加的生产版**——[examples/models/qwen3_jit/kernels/projection.py:42-L54](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/models/qwen3_jit/kernels/projection.py#L42-L54)：Qwen3 投影内核把同样的「首块初始化 + 后续累加」放进 `pl.pipeline` 循环，用 `if k0 == 0: matmul else: matmul_acc` 在循环体内区分首轮——这是本讲综合实践可选的另一种写法。

**pl.range 支持起点**——[python/pypto/language/dsl_api.py:196-L222](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/python/pypto/language/dsl_api.py#L196-L222)：`pl.range` 接受 `(stop)` / `(start, stop)` / `(start, stop, step)` 三种形态，参数可以是 int 字面量或 Scalar——所以「剥离首轮 + `pl.range(1, K // K_TILE)` 循环其余轮」是合法写法。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：手工执行一遍 K 分块累加，确认理解坐标滑动。

1. 读取 [examples/intermediate/04_matmul_acc.py:29-L52](https://github.com/hw-native-sys/pypto/blob/c7ba9fb0cbabdcad347cf5f2c91f2c710b96981a/examples/intermediate/04_matmul_acc.py#L29-L52)。
2. 在纸上写出 `matmul_acc_64` 中 4 个 `pl.load` 各自读到的窗口范围（用 `a[r1:r2, c1:c2]` 的切片记法）。
3. 运行 `python examples/intermediate/04_matmul_acc.py` 确认 `OK`。

**需要观察的现象 / 预期答案**：

- `tile_a0_l1 = a[0:64, 0:32]`，`tile_b0_l1 = b[0:32, 0:64]`
- `tile_a1_l1 = a[0:64, 32:64]`，`tile_b1_l1 = b[32:64, 0:64]`
- 两块的 K 区间 `[0,32)` 与 `[32,64)` 首尾相接、不重不漏，正是 \(\sum_b P_b\) 的分块方式。

**预期结果**：程序输出 `OK`（`torch.allclose(c, torch.matmul(a, b))` 通过），说明「matmul 初始化 + matmul_acc 累加」在数学上等价于整块乘法。

#### 4.4.5 小练习与答案

**练习 1**：`matmul_acc_64` 里如果把第二块的两个 load 偏移写成 `[0, 0]` 和 `[0, 0]`（忘了滑动），程序还会报错吗？结果会怎样？

答案：大概率不报错——所有窗口都合法（都在源内），但算的是 \(A[:,0:32]B[0:32,:] + A[:,0:32]B[0:32,:]\)，即第一块的部分积乘 2。这是**正确性 bug 而不是编译错误**，Tile 级不检查你的分块是否覆盖了完整的 K——这也是必须用 torch 对照验证的原因。

**练习 2**：为什么累加用 `pl.matmul_acc` 而不是 `acc = pl.add(acc, pl.matmul(...))`？

答案：`matmul_acc` 直接在 L0C 累加器上原地累加；`pl.add` 方案要把每个部分积从 L0C 读到 UB、做向量加法、再写回，多两次数据搬运和一层向量指令。语义相同，性能差一个量级。

**练习 3**：`matmul_acc` 的 `init_cond=(k0 == 0)` 参数解决什么问题？

答案：让首轮在条件为真时覆写而不是累加，从而免去「累加器预先清零」或「首轮剥离」——官方文档称之为 split-K 惯用法；运行时谓词则编译成两分支，不需要在累加器上引入 phi。

## 5. 综合实践

**任务**：用 Tile 级算子实现 \(128 \times 256\) 乘 \(256 \times 64\) 的 FP16 矩阵乘（FP32 累加），手动控制全部分块尺寸，并与 `torch.matmul` 对照验证。

这一任务串联本讲全部四个模块：`pl.load`/`pl.store` 的坐标约定（4.1）、Tile 算子与 dtype 规则（4.2）、Cube 存储链（4.3）、分块循环与 K 维累加（4.4）。

**分块方案**：\(M_T = K_T = N_T = 64\)。于是 M 方向 2 块、N 方向恰好 1 块（64 = N_TILE，列循环可省）、K 方向 4 块（首块初始化 + 3 次累加）。

**参考实现**（示例代码，建议新建文件后运行）：

```python
import pypto.language as pl
import torch
from pypto.runtime import RunConfig

M, K, N = 128, 256, 64
M_TILE, K_TILE, N_TILE = 64, 64, 64


@pl.jit
def matmul_tiled(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        for m in pl.range(M // M_TILE):
            # ---- K 首块：初始化累加器（GM → Mat → L0A/L0B → matmul → L0C）----
            a_l1 = pl.load(a, [m * M_TILE, 0], [M_TILE, K_TILE],
                           target_memory=pl.MemorySpace.Mat)
            b_l1 = pl.load(b, [0, 0], [K_TILE, N_TILE],
                           target_memory=pl.MemorySpace.Mat)
            acc = pl.matmul(
                pl.move(a_l1, target_memory=pl.MemorySpace.Left),
                pl.move(b_l1, target_memory=pl.MemorySpace.Right),
            )
            # ---- K 其余块：load 偏移沿 A 的列 / B 的行同步滑动，原地累加 ----
            for k in pl.range(1, K // K_TILE):
                a_l1 = pl.load(a, [m * M_TILE, k * K_TILE], [M_TILE, K_TILE],
                               target_memory=pl.MemorySpace.Mat)
                b_l1 = pl.load(b, [k * K_TILE, 0], [K_TILE, N_TILE],
                               target_memory=pl.MemorySpace.Mat)
                acc = pl.matmul_acc(
                    acc,
                    pl.move(a_l1, target_memory=pl.MemorySpace.Left),
                    pl.move(b_l1, target_memory=pl.MemorySpace.Right),
                )
            # ---- 全部 K 块累加完毕，一次性写回 GM ----
            pl.store(acc, [m * M_TILE, 0], c)
    return c


if __name__ == "__main__":
    cfg = RunConfig()
    torch.manual_seed(0)
    a = torch.randn(M, K, dtype=torch.float16)
    b = torch.randn(K, N, dtype=torch.float16)
    c = torch.zeros(M, N, dtype=torch.float32)          # Cube 累加器恒 FP32
    matmul_tiled(a, b, c, config=cfg)
    ref = torch.matmul(a.float(), b.float())            # FP16 值提升到 FP32 后相乘
    assert torch.allclose(c, ref, rtol=1e-3, atol=1e-3)
    print("OK")
```

**逐行核对清单**（做完后自查）：

1. `shapes` 恒为 `[M_TILE, K_TILE]` / `[K_TILE, N_TILE]`；只有 `offsets` 在动——A 的行偏移 `m * M_TILE`、A 的列偏移 `k * K_TILE`、B 的行偏移 `k * K_TILE`。（4.1 的口诀）
2. `pl.range(1, K // K_TILE)` 从 1 起循环，首轮已剥离——`pl.range` 支持 `(start, stop)` 形态。
3. `acc` 无需注解也能推出 FP32；想显式声明可写 `acc: pl.Tile[[64, 64], pl.FP32] = ...`（`04_matmul_acc.py` 的写法）。
4. 输出张量 `c` 是 FP32——`tile.matmul` 浮点结果恒 FP32。若要 FP16 输出，须在 store 前 `pl.cast(acc, pl.FP16)` 并把 `c` 换成 FP16 张量。
5. 对照基准用 `torch.matmul(a.float(), b.float())`：FP16 输入提升到 FP32 后相乘，与 Cube「FP16 输入、FP32 累加」语义一致；容差放 1e-3。

**观察点与预期结果**：断言通过、打印 `OK`。（运行结果待本地验证。）验证通过后可以做的两个变体实验：

- 把 `M_TILE` 改成 128（M 方向只剩 1 块），确认结果不变；
- 用 `if k == 0: acc = pl.matmul(...) else: acc = pl.matmul_acc(...)` 的循环内分支（qwen3 `projection.py` 的写法）替换剥离首轮的结构，对比两种写法。

## 6. 本讲小结

- `pl.load` / `pl.store` 是 Tile 级仅有的 GM 数据通路；坐标口诀是 **shapes 恒为 Tile 窗口尺寸、offsets 在源张量坐标系中滑动**，`valid_shape` 声明窗口内真实有效的区域。
- `tile.*` 算子一一映射片上指令：约 150 个算子按搬运/生成/算术/Cube 乘法/归约/广播/视图/散聚分组；需要暂存区的算子（行归约等）显式要求 `tmp_tile`，没有隐式 dtype 提升，标量右操作数规范化到 `s` 后缀变体。
- Cube 矩阵乘走四级存储链 `GM → Mat(L1) → Left/Right(L0A/L0B) → Acc(L0C)`，`pl.load(target_memory=Mat)` 管第一跳、`pl.move` 管最后一公里；浮点结果 dtype 由累加器固定为 FP32。
- K 维累加的标准范式：首块 `pl.matmul` 初始化 L0C，其余块 `pl.matmul_acc` 原地累加，**全部 K 块完成后才 `pl.store` 一次**；等价公式是 \( \text{acc}_b = \text{acc}_{b-1} + A_b B_b \)。
- Tile 级的正确性完全由分块坐标决定——偏移写错通常不报错而是算错，torch 对照验证是标配手段。

## 7. 下一步学习建议

- **下一讲（u2-l5）**：标量计算与控制流——`pl.range` 更完整的形态、条件语句、标量表达式如何与 Tile 算子协作（比如用 `pl.tile.get_block_idx()` 做按块分支）。
- **延伸阅读源码**：`examples/intermediate/04_matmul_acc.py` 的邻居 `05_assemble.py`（片上拼装）；`examples/models/08_llama_mini.py` 第 175-215 行（真实模型里 K 分块 + `transpose_view` 的组合用法）。
- **向后衔接**：本讲手工写的分块，正是 u5-l5 将要剖析的 `ConvertTensorToTileOps` Pass 自动做的事情——学完那一讲再回头看 Tensor 级算子，你会知道编译器替你插的每一条 load/move/store 长什么样。
