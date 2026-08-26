# Tile 级算子：load/store 与片上计算

## 1. 本讲目标

学完本讲，你应该能够：

1. 准确说出 `pl.load` / `pl.store` 每个参数的含义，特别是 **offsets（在源张量坐标系中移动）** 与 **shapes（恒为 Tile 窗口尺寸）** 的分工。
2. 理解 Tile 级算子为什么「直接映射片上指令」——一条 `tile.add` 对应一条向量指令，一条 `tile.matmul` 对应一次 Cube 单元乘法，中间没有隐藏的切分、暂存和调度。
3. 掌握 Cube 矩阵乘的多级存储链 `GM → Mat(L1) → Left/Right(L0A/L0B) → Acc(L0C)`，以及 `pl.move` 在链路中的角色。
4. 能用「分块循环 + K 维累加」的完整三段式，把一个大于单个 Tile 的矩阵乘手写出来，并用 torch 对照验证。
5. 了解累加算子族 `matmul_acc` / `gemv_acc` / `batch_matmul_acc` 共有的 `init_cond` 谓词——split-K 的 `k == 0` 惯用法，它如何用「覆写 vs 累加」的二选一取代首轮剥离。

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
| L0C / Acc | `MemorySpace.Acc`（由 matmul 结果隐式使用，也可显式 create） | Cube 累加器 | 几十 KB |

离计算单元越近，容量越小、带宽越高。`pl.load` 默认把数据搬进 Vec/UB；要让 Cube 做矩阵乘，还得显式走 `Mat → Left/Right` 这条链（4.3 节展开）。

### 2.3 一个坐标系约定

`pl.load(a, offsets, shapes)` 读到的窗口满足：

\[ W[i,j] = T[o_r+i,\ o_c+j]\quad (0 \le i < h,\ 0 \le j < w) \]

其中 \((o_r, o_c)\) 是 offsets，\((h, w)\) 是 shapes。**窗口形状不变，让原点在源张量坐标系里滑动**——这就是分块循环的全部秘密。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/pypto/language/op/tile_ops.py` | Tile 级算子的 DSL 包装层：`load`/`store`/`move`/`create` 与全部 `tile.*` 计算算子，全部收进 `__all__`（约 150 个名字）；累加算子族在此暴露 `init_cond` |
| `python/pypto/language/typing/scalar.py` | `BoolLike` 别名与 `predicate_to_expr`：把 `bool` / `Scalar` 谓词统一收敛成 IR 表达式，`init_cond` 参数的公共入口 |
| `python/pypto/language/__init__.py` | 把 `load`/`store`/`move` 等 Tile 专属算子直接提升为 `pl.*`；把 `add`/`mul`/`matmul` 等从统一分发层引入 |
| `python/pypto/language/op/unified_ops.py` | `pl.add`/`pl.matmul` 这类「同名双态」算子的按类型分发器：Tensor 走 `tensor.*`，Tile 走 `tile.*` |
| `python/pypto/language/dsl_api.py` | `pl.range` 循环迭代器的定义，分块循环的载体 |
| `examples/beginner/02_elementwise.py` | 本讲主线示例：`load → add/mul → store` 三段式 + 分块循环 `chunked_add` |
| `examples/beginner/05_matmul.py` | Cube 矩阵乘的最小示例：完整多级存储链 |
| `examples/intermediate/04_matmul_acc.py` | K 维分块累加的标准范式：首块 `matmul` 初始化，后续 `matmul_acc` 累加 |
| `tests/ut/codegen/test_matmul_init_cond.py` | `init_cond` 家族的行为断言：字面量折叠、运行时谓词、分组（rank-3）操作数透传 |
| `tests/st/runtime/ops/test_gemv.py` | `gemv_acc` 带 `init_cond` 的 on-device 用例：累加器 create-then-narrow 的标准拼写 |
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

**load 的签名与文档**——[python/pypto/language/op/tile_ops.py:383-L433](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L383-L433)：`load(tensor, offsets, shapes, valid_shape=None, target_memory=None, clamp=False)`。docstring 明确写下坐标系约定：`offsets: Offsets in each dimension. Always in the source tensor's coordinate system.`（offsets 永远在源张量坐标系中），以及「只读有效范围，Tile 可以比源里真实存在的区域大」——valid_shape 的存在让一个物理 Tile 能服务不满块的尾部。

**valid_shape 缺省逻辑**——[python/pypto/language/op/tile_ops.py:423-L432](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L423-L432)：`if valid_shape is None: valid_shape = shapes`，随后把三个坐标序列经 `_normalize_intlike` 解包（其中 Scalar 元素被展开为 IR 表达式，[L273-L275](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L273-L275)）后构造 `tile.load` Call。这就是「循环变量是 Scalar 也能当偏移用」的实现基础。

**store 的签名与 atomic**——[python/pypto/language/op/tile_ops.py:436-L479](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L436-L479)：`store(tile, offsets, output_tensor, shapes=None, *, atomic=...)`。默认 `AtomicType.None_` 直接覆写；`AtomicType.Add` 把 Tile 原子累加进 GM 已有内容——文档点明这是 **split-K 多核累加** 的用法，并警告跨核浮点累加顺序不固定、目标须先清零。返回值是 `output_tensor.__class__(expr=call_expr)`：按调用方传入的具体张量类（含 DistributedTensor 子类）重建一个包装对象，这正是文件头 [L193-L199](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L193-L199) 用 bound TypeVar 解释的设计。

**提升到 pl.* 命名空间**——[python/pypto/language/__init__.py:120-L154](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/__init__.py#L120-L154)：`from .op.tile_ops import (..., load, ..., move, ..., store, ...)`。所以 `pl.load` 与 `pl.tile.load` 是同一个函数。

**三段式的最小完整例子**——[examples/beginner/02_elementwise.py:39-L46](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/beginner/02_elementwise.py#L39-L46)：

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

**分块循环：offsets 滑动、shapes 不动**——[examples/beginner/02_elementwise.py:81-L95](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/beginner/02_elementwise.py#L81-L95)：

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
| 搬运/生命周期 | `load` / `store` / `move` / `create` / `full` | 4.1、4.3 与 4.5 节 |
| 片上生成 | `ci`(=arange) / `tri` / `random` / `full` | 不读 GM，直接在片上产生数据 |
| 逐元素算术 | `add` / `sub` / `mul` / `div` / `neg` / `exp` / `sqrt` / `cast` … | 标量右操作数走 `adds`/`muls`/… 变体 |
| 比较与位运算 | `cmp` / `and_` / `or_` / `xor` / `shl` … | 部分需要 `tmp` |
| Cube 乘法 | `matmul` / `matmul_acc` / `batch_matmul_acc` / `matmul_bias` / `gemv` / `gemv_acc` / `matmul_mx` | 见 4.3 与 4.5 节 |
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

**算子清单**——[python/pypto/language/op/tile_ops.py:22-L165](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L22-L165)：`__all__` 列出全部 Tile 算子。数一遍你会发现这是 DSL 中最大的一张算子表——因为硬件指令有多少种能力，这一层就该有多少个入口。

**标量右操作数规范化**——[python/pypto/language/op/tile_ops.py:966-L980](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L966-L980)：`add` 的签名是 `rhs: Tile | int | float | Scalar | Expr`，docstring 写明 `A scalar rhs canonicalizes to tile.adds`。实现上 `_unwrap_rhs`（[L268-L270](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L268-L270)）只是把包装类型解开，真正的 adds/muls 分流发生在更底层的 IR 构造里——这就是 u1-l5 里「Tile 乘标量路由到 `tile.muls` 立即数内嵌」的源头。

**显式暂存区：归约算子**——[python/pypto/language/op/tile_ops.py:1538-L1553](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1538-L1553)：`row_sum(tile, tmp_tile)` **必须**传一个同 dtype、同秩且各维不小于输入的暂存 Tile——硬件做行归约需要一块已知的工作区，DSL 不替你偷偷分配。对比 Tensor 级 `tensor.row_sum` 只需要一个参数，两者的抽象高度差一目了然。

**dtype 从严：超越函数**——[python/pypto/language/op/tile_ops.py:1123-L1136](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1123-L1136)：`sin` 标注 `FP32 only`，非 FP32 输入**直接拒绝而不是自动提升**，docstring 指路 `pl.cast(tile, pl.FP32)`。Tile 级没有隐式类型提升——每一次 dtype 变化都必须是你亲手写的一条 `tile.cast`（[L1238-L1258](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1238-L1258)）。

**双态分发的 Tile 侧**——[python/pypto/language/op/unified_ops.py:992-L1036](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/unified_ops.py#L992-L1036)：`pl.matmul` 按操作数类型分流；Tile 分支（[L1026-L1035](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/unified_ops.py#L1026-L1035)）里，`a_trans`/`b_trans`/`c_matrix_nz` 这三个 Tensor 专属旗标会被**显式拒绝**——docstring（[L1002-L1005](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/unified_ops.py#L1002-L1005)）给出原因：**在 Tile 级，转置是类型属性而不是算子旗标**（转置后的 Tile 有自己的布局类型，用 `pl.tile.transpose` / `transpose_view` 显式构造）。

**API 文档入口**——[docs/en/api/tile.md:1-L11](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/api/tile.md#L1-L11)：`pl.tile` 命名空间的文档由 mkdocstrings 从 `pypto.language.tile` 的 docstring 自动生成，并链接到「如何选择命名空间」的用户文档。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：建立对算子表的「分类直觉」，并验证 s 后缀规则。

1. 打开 [python/pypto/language/op/tile_ops.py:22-L165](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L22-L165)，把 `__all__` 里的算子按上面 4.2.2 的九个分组数一数，记下每组的数量。
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

**示例的内存层级自述**——[examples/beginner/05_matmul.py:10-L22](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/beginner/05_matmul.py#L10-L22)：模块 docstring 把层级写成一行 `GM -> Mat (L1) -> Left/Right (L0A/L0B) -> matmul -> Acc (L0C)`，并注明「cube basics」。

**完整链条的代码**——[examples/beginner/05_matmul.py:29-L38](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/beginner/05_matmul.py#L29-L38)：

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

**pl.move 的签名**——[python/pypto/language/op/tile_ops.py:624-L648](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L624-L648)：`move(tile, target_memory, blayout=None, slayout=None)`，目标空间枚举包含 `Vec / Mat / Left / Right / LeftScale / RightScale`（后两个服务 MX 缩放数据）。可选的 block/scatter 布局参数留待高级用法。

**matmul 的包装**——[python/pypto/language/op/tile_ops.py:1261-L1272](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1261-L1272)：`matmul(lhs, rhs)` 只是薄薄一层——unwrap 两个操作数、构造 IR Call、重新包装成 Tile。性能语义全在「操作数必须在 Left/Right」这一前置条件里。

**结果 dtype 规则的文档**——[python/pypto/language/op/unified_ops.py:1013-L1016](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/unified_ops.py#L1013-L1016)：`tile.matmul` 的结果 dtype 由 Cube 累加器固定（浮点 FP32、整数 INT32），Tensor 专属的 `out_dtype` 在 Tile 路径上只有与该推导一致时才被接受，否则报错。**FP16 进、FP32 出是写死的行为。**

**matmul_acc：带累加的矩阵乘**——[python/pypto/language/op/tile_ops.py:1292-L1319](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1292-L1319)：`matmul_acc(acc, lhs, rhs, init_cond=None)` 计算 `acc += lhs @ rhs`，直接累加在 L0C 上，省掉一次「读出-相加-写回」的往返。`init_cond` 参数（[L1295-L1305](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1295-L1305)）在条件为真的步上改为覆写而不是累加——这是 split-K 惯用法的官方形态，4.5 节专门展开（现在 `gemv_acc` 与 `batch_matmul_acc` 也加入了这个家族）。

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

上面这个「首块初始化 + 其余块累加」的写法叫**剥离首轮（peel）**；4.5 节将展示用 `init_cond` 谓词把两步合并成一个统一循环的替代写法。

#### 4.4.3 源码精读

**K 分块累加的教学版**——[examples/intermediate/04_matmul_acc.py:29-L52](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/intermediate/04_matmul_acc.py#L29-L52)：64×64 的乘法被拆成两个 K=32 块。

首块初始化（[L37-L42](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/intermediate/04_matmul_acc.py#L37-L42)）：

```python
tile_a0_l1 = pl.load(a, [0, 0], [64, 32], target_memory=pl.MemorySpace.Mat)
tile_b0_l1 = pl.load(b, [0, 0], [32, 64], target_memory=pl.MemorySpace.Mat)
tile_a0_l0a = pl.move(tile_a0_l1, target_memory=pl.MemorySpace.Left)
tile_b0_l0b = pl.move(tile_b0_l1, target_memory=pl.MemorySpace.Right)
acc: pl.Tile[[64, 64], pl.FP32] = pl.matmul(tile_a0_l0a, tile_b0_l0b)
```

注意两个细节：A 取 `A[:, 0:32]`（offsets `[0,0]`、shapes `[64,32]`），B 取 `B[0:32, :]`（offsets `[0,0]`、shapes `[32,64]`）——K 维在 A 的列、B 的行上同时被切。以及局部变量的**类型注解** `acc: pl.Tile[[64, 64], pl.FP32]`，显式声明累加器是 FP32（与 Cube 累加器规则呼应）。

第二块累加（[L44-L49](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/intermediate/04_matmul_acc.py#L44-L49)）：

```python
tile_a1_l1 = pl.load(a, [0, 32], [64, 32], target_memory=pl.MemorySpace.Mat)
tile_b1_l1 = pl.load(b, [32, 0], [32, 64], target_memory=pl.MemorySpace.Mat)
...
acc = pl.matmul_acc(acc, tile_a1_l0a, tile_b1_l0b)
```

A 的列偏移走到 `[0, 32]`，B 的行偏移走到 `[32, 0]`——**K 轴在两个操作数上同步滑动**。`pl.matmul_acc` 把部分积累进已有的 acc。

收尾（[L51](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/intermediate/04_matmul_acc.py#L51)）：`pl.store(acc, [0, 0], c)`——两个 K 块全部累加完才写回 GM 一次。

**循环内累加的生产版**——[examples/models/qwen3_jit/kernels/projection.py:42-L54](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/models/qwen3_jit/kernels/projection.py#L42-L54)：Qwen3 投影内核把同样的「首块初始化 + 后续累加」放进 `pl.pipeline` 循环，用 `if k0 == 0: matmul else: matmul_acc` 在循环体内区分首轮——这是 4.5 节 `init_cond` 要消灭的「循环内 if 分支」写法的活例。

**pl.range 支持起点**——[python/pypto/language/dsl_api.py:196-L222](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/dsl_api.py#L196-L222)：`pl.range` 接受 `(stop)` / `(start, stop)` / `(start, stop, step)` 三种形态，参数可以是 int 字面量或 Scalar——所以「剥离首轮 + `pl.range(1, K // K_TILE)` 循环其余轮」是合法写法。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：手工执行一遍 K 分块累加，确认理解坐标滑动。

1. 读取 [examples/intermediate/04_matmul_acc.py:29-L52](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/intermediate/04_matmul_acc.py#L29-L52)。
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

**练习 3**：`04_matmul_acc.py` 的「首块 matmul 初始化」与 qwen3 内核的「循环内 if k0 == 0 分支」各有什么代价？有没有第三种写法？

答案：前者要求把首轮从循环中剥出来，循环体写两遍；后者保持单一循环但引入两分支——累加器在每个分支被不同算子定义，IR 层需要一个 phi（或结构不同的两条路径）汇合。第三种写法就是 4.5 节的 `init_cond=(k0 == 0)` 谓词：单一循环、单一算子、累加器单一定义。

### 4.5 init_cond 谓词：累加算子族的 split-K 惯用法

#### 4.5.1 概念说明

`tile.matmul_acc` 有一个可选谓词 `init_cond`：在谓词为**真**的步上，算子改为**覆写**累加器（行为等同 `matmul`）而不是累加。于是 split-K 循环不再需要「首轮剥皮」或「预先清零」——首轮的 `k == 0` 直接触发覆写，把累加器「铸造」出来；后续步谓词为假，正常累加：

\[ \text{acc} \;\leftarrow\; \begin{cases} \text{lhs}\cdot\text{rhs}, & \text{init\_cond 成立（覆写，即 cmatrixInit）} \\ \text{acc} + \text{lhs}\cdot\text{rhs}, & \text{否则（原地累加）} \end{cases} \]

这个谓词最初只在 `matmul_acc` 上，现在**整个 Cube 累加算子族**都支持它：

| 算子 | init_cond 位置 | 说明 |
| --- | --- | --- |
| `tile.matmul_acc` | 第 4 个**位置参数**（也可关键字传） | 最早的形态；2D 矩阵乘累加 |
| `tile.gemv_acc` | **仅关键字**（`init_cond=...`） | GEMV 是 M=1 的 matmul，跑在同一个 Cube MAD 上，硬件支持完全相同（MAD 的 `cmatrixInit` 位并非 matmul 专用）。仅关键字是因为 `acc_phase` 已占第 4 个位置槽，换成位置参数会改变既有调用的绑定 |
| `tile.batch_matmul_acc` | 第 4 个**位置参数**（也可关键字传） | 新近加入；`FlattenTileNdTo2D` 会把谓词**透传**给它展开出的每一个 2D `tile.matmul_acc` |

为什么这个谓词重要？对比三种 split-K 写法：

```text
写法 A（剥皮）:   k=0 块在循环外用 matmul 初始化，循环只跑 k≥1
                  → 循环体写两遍，代码重复
写法 B（分支）:   循环内 if k0 == 0: matmul else: matmul_acc
                  → 累加器在两个分支被不同算子定义，需要 phi 汇合
写法 C（谓词）:   循环内统一 matmul_acc(acc, a, b, init_cond=(k0 == 0))
                  → 单一循环、单一算子、累加器单一定义（single-def）
```

另外注意：`matmul_bias` / `gemv_bias` / `matmul_mx_bias` 这类**带 bias 的变体刻意不带** `init_cond`——它们本来就用 bias 铸造累加器，没有「初始值可否条件化」的问题。

#### 4.5.2 核心流程

`init_cond` 的类型是 `BoolLike = bool | Scalar | Expr`，三族算子统一经 `predicate_to_expr` 收敛成 IR 表达式。两种谓词的编译行为不同：

```text
init_cond=True / False（Python 字面量）
  → ConstInt(.., BOOL)，编译期常量
  → 降低时直接选定一种形态（覆写或累加），不留下任何分支

init_cond=(k0 == 0)（k0 是循环变量，Scalar 比较表达式）
  → 符号表达式，运行期值
  → 降低成对两种形态的分支（branch over the two），
    但累加器上没有 phi —— 两条路径都落到同一个 L0C 缓冲

init_cond=非 BOOL 类型（如 pl.read(acc, [0,0]) 的 INDEX 标量）
  → 直接拒绝，不会被悄悄当作真值
```

配套的累加器声明方式也变了：写法 C 中累加器不再由首轮 `matmul` 隐式产生，而是循环前**显式 create** 一块 L0C：

```text
acc = pl.tile.create([M, N], pl.FP32, target_memory=pl.MemorySpace.Acc)   # 只分配，不初始化
for k0 in pl.range(0, K, K_TILE):
    a = load(...); b = load(...)                                          # offsets 沿 K 滑动
    acc = pl.matmul_acc(acc, a, b, init_cond=(k0 == 0))                   # k0==0 覆写，其余累加
pl.store(acc, [0, 0], c)
```

关键性质是**数值性**的：这块 L0C 从不清零，`k0 == 0` 步若没有正确选中覆写形态，结果会带上 L0C 里的陈旧内容——错误直接反映在数值对照上，而不是结构差异上。

#### 4.5.3 源码精读

**matmul_acc：家族的原型**——[python/pypto/language/op/tile_ops.py:1292-L1319](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1292-L1319)：签名 `matmul_acc(acc, lhs, rhs, init_cond: BoolLike | None = None)`。docstring 给出 split-K 惯用法的官方示例（`pl.pipeline` 循环内 `pl.tile.matmul_acc(acc_t, a, b, init_cond=(k0 == 0))`），并写明两条降低规则：字面量 `True`/`False` 编译期选定形态；运行期谓词降低为两形态分支、累加器无 phi。

**gemv_acc：同一谓词，仅关键字**——[python/pypto/language/op/tile_ops.py:1449-L1496](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1449-L1496)：签名在 `acc_phase` 之后加了 `*, init_cond: BoolLike | None = None`（[L1454-L1455](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1454-L1455) 的星号把它标成 keyword-only）。docstring（[L1462-L1477](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1462-L1477)）说清了来龙去脉：GEMV 是 M=1 的 matmul、跑在同一个 Cube MAD 上，所以携带同一个谓词位；`init_cond` 仅关键字是因为 `acc_phase` 已经拥有第 4 个位置槽——改成位置参数会让既有的 `gemv_acc(acc, a, b, "partial")` 调用被重新绑定。

**gemv 的累加器要「先建后收窄」**——[tests/st/runtime/ops/test_gemv.py:669-L674](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/st/runtime/ops/test_gemv.py#L669-L674)：GEMV 的 `[1, N]` 结果在物理上占据 16 行 L0C，所以累加器不能直接 `create([1, N])`，标准拼写是先建物理形状再收窄有效区：

```python
acc_raw = pl.tile.create([16, n], pl.FP32, target_memory=pl.MemorySpace.Acc)
acc = pl.tile.set_validshape(acc_raw, 1, n)
for k0 in pl.range(0, k_total, k_chunk):
    a_l1 = pl.load(a, [0, k0], [1, k_chunk], target_memory=pl.MemorySpace.Mat)
    b_l1 = pl.load(b, [k0, 0], [k_chunk, n], target_memory=pl.MemorySpace.Mat)
    acc = pl.tile.gemv_acc(acc, a_l1, b_l1, init_cond=(k0 == 0))
```

被替换的旧写法（直线 `tile.gemv` 首块）会隐式产生正确类型的累加器；改成谓词写法后这一步要显式做——这是从「剥皮」迁移到 `init_cond` 时最容易踩的坑。

**batch_matmul_acc：谓词随降级透传**——[python/pypto/language/op/tile_ops.py:1322-L1347](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L1322-L1347)：签名与 `matmul_acc` 对齐（`init_cond` 为第 4 参数），docstring 明确「`init_cond` 行为与 `matmul_acc` 完全一致；`FlattenTileNdTo2D` 会把谓词转发给它展开出的每一个 2D `tile.matmul_acc`」。这一点至关重要：3D Tile 最终会被摊平成若干 2D 调用，如果降级时悄悄丢掉谓词，`k == 0` 步就会向一块**从未初始化的累加器**累加——错误被静默吞掉。典型受益场景是 MoE：专家权重切片 `w[e:e+1, :, :]` 是 rank-3、batch=1 的操作数，见 [tests/ut/codegen/test_matmul_init_cond.py:277-L305](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L277-L305) 的分组 split-K 用例（`pl.matmul_acc(acc, x_k, w_k, b_trans=True, init_cond=(kb == 0))`，断言 `pto.tmatmul ` 恰好一次（覆写）且无 `scf.if`）。

**运行期谓词的标准循环**——[tests/ut/codegen/test_matmul_init_cond.py:146-L153](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L146-L153)：`create` 一块 `[16, 16]` 的 Acc 累加器（带 `pl.Tile[[16, 16], pl.FP32, pl.MemorySpace.Acc]` 注解），然后 `for k0 in pl.range(0, 64, 16)` 内统一 `pl.matmul_acc(acc_tile, a, b, init_cond=(k0 == 0))`——这就是本讲综合实践版本 B 的模板。字面量形态的对照用例在 [L96-L101](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L96-L101)（`init_cond=True`）。

**BoolLike 与 predicate_to_expr**——[python/pypto/language/typing/scalar.py:325-L350](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/typing/scalar.py#L325-L350)：`BoolLike = bool | Scalar | Expr`；`predicate_to_expr` 把 Python `bool` 变成 `ConstInt(.., BOOL)`（编译期常量，降低时可折叠），把 `Scalar`（典型如 `k == 0` 的比较结果）解包成它携带的符号表达式（保持为运行期值），`None` 原样通过。三族算子的 `init_cond` 都走这一个入口。

**create 的 compact 旗标（了解即可）**——[python/pypto/language/op/tile_ops.py:296-L341](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L296-L341)：`create` 新增了 keyword-only 的 `compact` 参数（[L320-L326](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/op/tile_ops.py#L320-L326)），声明 L0C 缓冲持有「按有效区打包」的乘积（N 分形 pitch 为 `ceil(validRow/16)*16`）。docstring 明说这是**编译器内部标志**：内核开发者不设置它，是 `AutoTileMatmulL0` 这个 Pass 在为自己合成的 split-K 累加器种子打标记。它与 `init_cond` 是一对——自动分块 Pass 生成 split-K 时，正是用 `tile.create(compact=True)` 声明种子、用 `init_cond=(ko == 0)` 驱动首轮覆写（见 [tests/ut/codegen/test_matmul_init_cond.py:228-L263](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L228-L263) 对该 Pass 产物的折叠断言）。手写内核只需关心 `init_cond`。

#### 4.5.4 代码实践

**实践目标**：亲手把 4.4 的「剥皮版」K 循环改写成「谓词版」，并从测试断言学会验证谓词真的被折叠/分支。

1. 以综合实践的版本 A（剥皮：`pl.range(1, K // K_TILE)`）为基线，确认 `torch.allclose` 通过。
2. 按版本 B 改写：循环前 `pl.tile.create` 累加器，`pl.range(K // K_TILE)` 单循环内统一 `matmul_acc(..., init_cond=(k == 0))`。
3. 用 `--dump-passes` 或 `kernel.compile()` 导出两版的降级 IR / 产物，对比 K 循环体的形态：版本 A 循环外有一条独立的初始化 matmul；版本 B 循环体内只有一条 `matmul_acc`。
4. 阅读断言范式：[tests/ut/codegen/test_matmul_init_cond.py:262-L263](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L262-L263) 用 `mlir.count("pto.tmatmul.acc") == 1` 且 `"scf.if" not in mlir` 验证「一次覆写 + 其余累加、无分支残留」。

**需要观察的现象**：两版数值完全一致；版本 B 的循环体比版本 A 少一种算子形态（不再有裸 `matmul` 与 `matmul_acc` 两种路径并存）；若把 `init_cond=(k == 0)` 误删，`k == 0` 步向未初始化累加器累加，数值对照大概率失败——这正是 4.5.2 说的「数值性而非结构性」的验证。

**预期结果**：版本 A、版本 B 的输出都通过 `torch.allclose(c, torch.matmul(a.float(), b.float()), rtol=1e-3, atol=1e-3)`。（运行结果待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：为什么 `gemv_acc` 的 `init_cond` 是 keyword-only，而 `matmul_acc` / `batch_matmul_acc` 可以按位置传？

答案：`gemv_acc` 的第 4 个位置槽已被 `acc_phase` 占据（`gemv_acc(acc, lhs, rhs, "partial")` 是既有合法调用）；若把 `init_cond` 插成第 4 个位置参数，这些调用的实参会被重新绑定到谓词上。改成 keyword-only 是零破坏的扩展方式。

**练习 2**：如果 `FlattenTileNdTo2D` 把 `batch_matmul_acc` 的谓词在降级时**丢掉**而不是透传，会发生什么？为什么说这是危险的静默错误？

答案：3D 调用被摊平成多个 2D `matmul_acc` 后，`k == 0` 步的那一个会向一块从未初始化的 L0C 累加——结果带上陈旧缓冲内容，数值错误但编译完全通过、IR 结构也看不出异常。所以源码文档专门强调「forwards the predicate to every 2D tile.matmul_acc it unrolls this op into」，且有专门测试钉住这一行为。

**练习 3**：`init_cond=True`（字面量）和 `init_cond=(k0 == 0)`（运行期比较）降低后的产物有何不同？

答案：字面量经 `predicate_to_expr` 变成 `ConstInt(.., BOOL)` 编译期常量，降低时直接选定覆写形态，产物里没有分支（测试断言 `scf.if` 不出现）；运行期比较保持为符号表达式，降低成对「覆写 / 累加」两种形态的分支，但累加器本身无 phi——分支的两臂落到同一块 L0C 缓冲。当外层循环被展开、谓词在每个副本里成为常量时，后者也会被折叠成单一 MAD。

## 5. 综合实践

**任务**：用 Tile 级算子实现 \(128 \times 256\) 乘 \(256 \times 64\) 的 FP16 矩阵乘（FP32 累加），手动控制全部分块尺寸，与 `torch.matmul` 对照验证；再改写成 K 维单循环并用 `matmul_acc` 的 `init_cond=(k == 0)` 消除首步剥皮，验证两版结果一致。

这一任务串联本讲全部五个模块：`pl.load`/`pl.store` 的坐标约定（4.1）、Tile 算子与 dtype 规则（4.2）、Cube 存储链（4.3）、分块循环与 K 维累加（4.4）、`init_cond` 谓词（4.5）。

**分块方案**：\(M_T = K_T = N_T = 64\)。于是 M 方向 2 块、N 方向恰好 1 块（64 = N_TILE，列循环可省）、K 方向 4 块。

**版本 A：剥皮式（首块 matmul 初始化 + 其余 matmul_acc）**（示例代码，建议新建文件后运行）：

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
```

**版本 B：谓词式（单循环 + init_cond，消除剥皮）**（示例代码；模板来自 [tests/ut/codegen/test_matmul_init_cond.py:146-L153](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L146-L153)）：

```python
@pl.jit
def matmul_tiled_pred(a: pl.Tensor, b: pl.Tensor, c: pl.Out[pl.Tensor]):
    with pl.at(level=pl.Level.CORE_GROUP):
        for m in pl.range(M // M_TILE):
            # ---- 累加器只分配、不初始化；由 k == 0 步的覆写铸造 ----
            acc: pl.Tile[[64, 64], pl.FP32, pl.MemorySpace.Acc] = pl.tile.create(
                [M_TILE, N_TILE], pl.FP32, target_memory=pl.MemorySpace.Acc
            )
            # ---- K 全部 4 块走同一个循环：k==0 覆写，其余累加 ----
            for k in pl.range(K // K_TILE):
                a_l1 = pl.load(a, [m * M_TILE, k * K_TILE], [M_TILE, K_TILE],
                               target_memory=pl.MemorySpace.Mat)
                b_l1 = pl.load(b, [k * K_TILE, 0], [K_TILE, N_TILE],
                               target_memory=pl.MemorySpace.Mat)
                acc = pl.matmul_acc(
                    acc,
                    pl.move(a_l1, target_memory=pl.MemorySpace.Left),
                    pl.move(b_l1, target_memory=pl.MemorySpace.Right),
                    init_cond=(k == 0),
                )
            pl.store(acc, [m * M_TILE, 0], c)
    return c
```

**运行与对照**（示例代码）：

```python
if __name__ == "__main__":
    cfg = RunConfig()
    torch.manual_seed(0)
    a = torch.randn(M, K, dtype=torch.float16)
    b = torch.randn(K, N, dtype=torch.float16)
    ref = torch.matmul(a.float(), b.float())            # FP16 值提升到 FP32 后相乘

    c_a = torch.zeros(M, N, dtype=torch.float32)        # Cube 累加器恒 FP32
    matmul_tiled(a, b, c_a, config=cfg)
    assert torch.allclose(c_a, ref, rtol=1e-3, atol=1e-3)

    c_b = torch.zeros(M, N, dtype=torch.float32)
    matmul_tiled_pred(a, b, c_b, config=cfg)
    assert torch.allclose(c_b, ref, rtol=1e-3, atol=1e-3)
    assert torch.allclose(c_a, c_b, rtol=1e-3, atol=1e-3)   # 两版彼此一致
    print("OK")
```

**逐行核对清单**（做完后自查）：

1. `shapes` 恒为 `[M_TILE, K_TILE]` / `[K_TILE, N_TILE]`；只有 `offsets` 在动——A 的行偏移 `m * M_TILE`、A 的列偏移 `k * K_TILE`、B 的行偏移 `k * K_TILE`。（4.1 的口诀）
2. 版本 A 用 `pl.range(1, K // K_TILE)` 从 1 起循环剥离首轮；版本 B 用 `pl.range(K // K_TILE)` 单循环，首轮交给 `init_cond=(k == 0)` 覆写。
3. 版本 B 的累加器由 `pl.tile.create(..., target_memory=pl.MemorySpace.Acc)` 显式分配且**从不清零**——这是谓词写法的数值契约：`k == 0` 步必须覆写。
4. `acc` 的类型注解镜像测试写法 `pl.Tile[[64, 64], pl.FP32, pl.MemorySpace.Acc]`（`04_matmul_acc.py` 的两槽注解 `pl.Tile[[64, 64], pl.FP32]` 也合法）。
5. 输出张量 `c` 是 FP32——`tile.matmul` / `tile.matmul_acc` 浮点结果恒 FP32。若要 FP16 输出，须在 store 前 `pl.cast(acc, pl.FP16)` 并把 `c` 换成 FP16 张量。
6. 对照基准用 `torch.matmul(a.float(), b.float())`：FP16 输入提升到 FP32 后相乘，与 Cube「FP16 输入、FP32 累加」语义一致；容差放 1e-3。

**观察点与预期结果**：三条断言全部通过、打印 `OK`。（运行结果待本地验证。）验证通过后可以做的三个变体实验：

- 把 `M_TILE` 改成 128（M 方向只剩 1 块），确认结果不变；
- 用 `if k == 0: acc = pl.matmul(...) else: acc = pl.matmul_acc(...)` 的循环内分支（qwen3 `projection.py` 的写法）替换版本 B，对比三版的降级 IR；
- dump 版本 B 的产物，仿照 [tests/ut/codegen/test_matmul_init_cond.py:262-L263](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py#L262-L263) 数一数覆写形态（`pto.tmatmul`）与累加形态（`pto.tmatmul.acc`）各出现几次、有没有 `scf.if` 残留。

## 6. 本讲小结

- `pl.load` / `pl.store` 是 Tile 级仅有的 GM 数据通路；坐标口诀是 **shapes 恒为 Tile 窗口尺寸、offsets 在源张量坐标系中滑动**，`valid_shape` 声明窗口内真实有效的区域。
- `tile.*` 算子一一映射片上指令：约 150 个算子按搬运/生成/算术/Cube 乘法/归约/广播/视图/散聚分组；需要暂存区的算子（行归约等）显式要求 `tmp_tile`，没有隐式 dtype 提升，标量右操作数规范化到 `s` 后缀变体。
- Cube 矩阵乘走四级存储链 `GM → Mat(L1) → Left/Right(L0A/L0B) → Acc(L0C)`，`pl.load(target_memory=Mat)` 管第一跳、`pl.move` 管最后一公里；浮点结果 dtype 由累加器固定为 FP32。
- K 维累加的标准范式：首块 `pl.matmul` 初始化 L0C，其余块 `pl.matmul_acc` 原地累加，**全部 K 块完成后才 `pl.store` 一次**；等价公式是 \( \text{acc}_b = \text{acc}_{b-1} + A_b B_b \)。
- 累加算子族 `matmul_acc` / `gemv_acc`（keyword-only）/ `batch_matmul_acc` 共享 `init_cond` 谓词：条件成立时覆写而非累加，是 split-K 的 `k == 0` 惯用法——单一循环、单一算子、累加器单一定义；字面量谓词编译期折叠，运行期谓词降低为无 phi 的两形态分支；`batch_matmul_acc` 的谓词会被 `FlattenTileNdTo2D` 透传到每个展开出的 2D 调用。
- Tile 级的正确性完全由分块坐标决定——偏移写错通常不报错而是算错，torch 对照验证是标配手段；谓词误删同样是数值错误而非编译错误。

## 7. 下一步学习建议

- **下一讲（u2-l5）**：标量计算与控制流——`pl.range` 更完整的形态、条件语句、标量表达式如何与 Tile 算子协作（比如用 `pl.tile.get_block_idx()` 做按块分支）。
- **延伸阅读源码**：`examples/intermediate/04_matmul_acc.py` 的邻居 `05_assemble.py`（片上拼装）；`examples/models/08_llama_mini.py` 第 175-215 行（真实模型里 K 分块 + `transpose_view` 的组合用法）；[tests/ut/codegen/test_matmul_init_cond.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/codegen/test_matmul_init_cond.py) 全文（`init_cond` 家族的完整行为清单）。
- **向后衔接**：本讲手工写的分块，正是 u5-l5 将要剖析的 `ConvertTensorToTileOps` Pass 自动做的事情；本讲 4.5 的 `init_cond=(ko == 0)` 与 `tile.create(compact=True)`，正是 u5-l6 的 `AutoTileMatmulL0` Pass 在自动 split-K 时合成的东西——学完那两讲再回头读本讲的循环，你会看到编译器生成的代码与你手写的谓词版几乎逐行对应。
