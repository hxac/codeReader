# gather/scatter 与高级索引

## 1. 本讲目标

本讲是「高级数据访问」单元的第二讲，承接 u4-l1（TiledView 与 persistent 遍历）。到目前为止，你访问全局数组的手段只有两类：

- `ct.load(array, index, shape)` / `ct.store(array, index, tile)`——按**瓦片空间（tile space）**索引，取的是一块**规则、连续、网格对齐**的 tile（u3-l1）。
- `tv.load(tile_index)` / `tv.store(tile_index, tile)`——把切分方式固化成 `TiledView`，但仍然是**规则的瓦片索引**（u4-l1）。

这两类有一个共同点：访问的内存区域是**沿每个轴都连续、且按 tile 对齐的矩形块**，因此能高效地映射到 TMA（Tensor Memory Accelerator）/ 合并访存（coalesced load）。但很多真实算子并不满足这个约束——例如 embedding 查表（按下标抓若干**任意行**）、稀疏索引拷贝、直方图回写、按索引更新参数。这些场景需要**非常规索引**：要么每个元素各自指向一个任意位置，要么「沿一个轴是任意下标、沿其余轴仍是连续切片」。

本讲就教你这两套非常规索引 API：

- **`ct.gather` / `ct.scatter`**：最通用的**逐点（pointwise）**索引——每个维度都用一个**元素空间（element space）**的整数 tile 来逐点寻址，结果 tile 的形状由各索引 tile 广播决定。这是 cuTile 里最灵活、也最「散乱」的访问方式。
- **`ct.load_advanced_indexing` / `ct.store_advanced_indexing`**：**混合索引**——恰好有一个维度是「稀疏（sparse）」逐点下标（一个 1D 整数 tile），其余维度是「稠密（dense）」的连续 `ct.Slice(start, length)`。它相当于「抓若干整行/整列」，稠密维仍然连续、仍可用 TMA。

读完本讲，你应该能够：

- 理解 `gather`/`scatter` 的**元素空间逐点寻址**语义，以及 `indices` 元组、形状广播、`mask`、`padding_value`、`check_bounds` 的精确含义。
- 理解 `scatter` 写入**重复下标属未定义行为（UB）**这一关键陷阱。
- 理解 `load_advanced_indexing`/`store_advanced_indexing` 的「一维稀疏 + 多维连续切片」约定，以及 `ct.Slice`、稀疏维 / 稠密维的区分。
- 能判断三类访存 API（`load`/`gather`/`load_advanced_indexing`）的**性能与对齐**差异，并在正确场景选用正确的工具。

本讲覆盖三个最小模块：**`gather`、`scatter`、`load_advanced_indexing`**（`store_advanced_indexing` 与第三个模块成对出现，一并讲解）。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面这些来自前面讲义的概念（本讲直接使用，不再重复解释）：

- **Array 与 Tile**：`Array` 是 host 分配、全局显存、可读写、运行时 shape 的数组；`Tile` 是内核内部不可变、编译期 shape（每维为 2 的幂）的数据块（u2-l2、u2-l3）。
- **load–compute–store 与 tile space 索引**：`ct.load(array, index, shape)` 里的 `index` 是**瓦片下标**（不是元素下标），`shape` 是编译期 tile 大小；访问的区域是 `array[i*shape0 + x, j*shape1 + y]` 这种**规则矩形块**；部分越界按 `padding_mode` 填充、整体越界未定义（u3-l1）。
- **NumPy 式形状广播**：末尾对齐、对应维相等或其一为 1、维度少者左侧补 1；既不拷贝数据也不破坏「每维为 2 的幂」约束（u2-l3）。
- **类型提升与隐式 cast**：`store`/`scatter` 写入时若 tile dtype 与数组 dtype 不同会做隐式 cast，某些方向（如 float→int）会被拒绝并抛 `TileTypeError`（u2-l4）。
- **`@ct.kernel` 与 `ct.launch`**：kernel 不在定义时执行，由 host 端 `ct.launch(stream, grid, kernel, args)` 启动（u1-l2、u2-l1）。
- **`Constant[int]` 与 `ct.arange`**：编译期常量参数会嵌入 cubin；`ct.arange(size, dtype=...)` 生成 `[0,1,...,size-1]` 的 1D 整数 tile，是构造索引 tile 的常用工厂（u3-l5、u3-l2）。
- **stub 与后端实现**：`ct.gather` 等都是 `@stub`，只有签名与文档，真正的实现在后端 IR 注册系统（见 u5-l7）；本讲只讲**语义与用法**，不深入 IR 实现。

一句话回顾：`load`/`store` 用「瓦片下标」取「规则矩形块」。本讲要让你能取**任意位置的单个元素**（gather/scatter），以及**任意下标的整行/整列**（advanced indexing）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1571-L1663) | `gather`、`scatter` 的**权威签名与文档**（`indices` 约定、`mask`/`padding_value`/`check_bounds` 语义）。stub 只有签名，实现在后端。 |
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1463-L1568) | `load_advanced_indexing`、`store_advanced_indexing` 的签名与文档，定义「一维稀疏 + 稠密切片」约定。 |
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L948-L967) | `ct.Slice` 类：稠密维的 `(start, length)` 描述符。 |
| [test/test_gather_scatter.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_gather_scatter.py#L24-L56) | `gather`/`scatter` 的完整测试：1D/2D 拷贝、标量、自定义 `padding_value`、边界检查开关、自定义 `mask`。是理解语义最干净的范本。 |
| [test/test_load_store_advanced_indexing.py](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_load_store_advanced_indexing.py#L21-L72) | `load_advanced_indexing`/`store_advanced_indexing` 的测试：基本读写、稀疏 gather 行、动态/常量 `Slice.start`、越界零填充、重复稀疏下标 UB、各类错误用例。 |

## 4. 核心概念与源码讲解

### 4.1 gather：按元素索引读数组

#### 4.1.1 概念说明

回忆 `ct.load(array, index, shape)`：`index` 是**瓦片下标**，访问的是 `array[i*shape0 + x, ...]` 这种**网格对齐的矩形块**。如果你想取的不是矩形块，而是「**每个元素各自来自一个任意位置**」——比如按下标数组 `[3, 7, 0, 5]` 抓出第 3、7、0、5 个元素拼成一个新 tile——`load` 就无能为力了，因为它的 `index` 只能给一个矩形原点。

`ct.gather(array, indices)` 正是为这种**逐点（pointwise）寻址**设计的。它的关键差别是：

- `indices` 是**元素空间（element space）**的整数 tile，**每一个元素就是一个独立的下标**，而不是瓦片坐标。
- 结果 tile 的第 \(k\) 个元素，等于 `array` 在「由各索引 tile 在第 \(k\) 处取值组成的坐标」处的元素。

也就是说，`gather` 把「索引」从 `load` 的**瓦片坐标**提升为**逐元素坐标**，是 cuTile 里最通用的读操作。它解决三类问题：

1. **任意重排 / 抽取**：按下标数组抽取若干元素（embedding 查表、top-k 抽取）。
2. **非连续拷贝**：跨步、间隔地取元素。
3. **条件寻址**：配合 `mask`，按布尔掩码选择性加载。

代价是：因为每个元素都可能指向任意地址，访存模式**最不规整、最难合并**，通常是三类读操作里最慢的（见 4.4 的性能讨论）。

#### 4.1.2 核心流程

`gather` 的索引规则用一句话概括：**`indices` 是长度等于数组秩的元组，每个分量是一个整数 tile 或标量，所有分量的形状互相广播到一个公共形状，结果 tile 的形状就是这个公共形状。**

对一个 2D 数组，设两个索引 tile `ind0`（形状 (M,N,1)）和 `ind1`（形状 (M,1,K)），则：

```text
t = ct.gather(array, (ind0, ind1))   # t 的形状 = 广播(ind0, ind1) = (M, N, K)
```

结果 tile 的每个元素按下面的公式计算（广播后）：

\[
t[i, j, k] \;=\; \texttt{array}\big[\,\texttt{ind0}[i,j,0],\;\; \texttt{ind1}[i,0,k]\,\big]
\quad\text{对所有 } 0\le i<M,\;0\le j<N,\;0\le k<K
\]

几个要点：

- **元组长度 = 数组秩**：1D 数组传长度 1 的元组；2D 传长度 2；以此类推。
- **1D 数组的简写**：`ct.gather(array, ind0)` 严格等价于 `ct.gather(array, (ind0,))`，即单个 tile 自动包成长度 1 的元组。
- **广播**：各索引分量形状不必相同，只要能按 NumPy 规则广播到同一形状即可（u2-l3）。
- **逐点寻址**：与 `load` 的瓦片坐标完全不同——这里没有「瓦片大小」概念，索引直接是元素下标。
- **边界与填充**：默认 `check_bounds=True`，越界下标返回 `padding_value`（默认 0）；负下标**不**遵循 Python 的「负索引」约定，一律视为越界。

`gather` 还有三个控制选项，都通过「掩码」机制影响哪些元素真正被加载：

```text
mask (bool tile/scalar, 广播到公共形状):
    where mask == False  →  返回 padding_value，不真正读内存
padding_value (标量/tile, 广播到公共形状):
    越界 或 mask==False 时返回的值；默认 0
check_bounds (默认 True):
    True  → 自动生成「边界掩码」(0 <= idx < dim)，越界处返回 padding_value
    False → 关闭边界检查，越界访问是未定义行为 (UB)，由调用者保证下标合法

有效掩码 = mask AND (check_bounds ? 边界掩码 : 全 True)
```

当 `mask` 与 `check_bounds=True` 同时存在时，有效掩码是两者的**逻辑与**：一个元素只有在「自定义 mask 为 True」**且**「下标在界内」时才真正被加载。

#### 4.1.3 源码精读

`gather` 的权威签名与文档（语义全在 docstring 里，stub 没有函数体）：

[gather 签名与语义 — src/cuda/tile/_stub.py:L1571-L1618](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1571-L1618)

> 这段代码定义了 `gather(array, indices, /, *, mask=None, padding_value=0, check_bounds=True, latency=None)`，并逐条说明：`indices` 元组长度须等于数组秩、各分量可广播、结果形状为广播形状、1D 数组的单 tile 简写、`mask`/`padding_value`/`check_bounds` 的掩码语义，以及「负下标视为越界」。

最干净的语义范本是「按 `ct.arange` 构造索引做 1D 拷贝」的测试内核：

[1D gather/scatter 拷贝内核 — test/test_gather_scatter.py:L24-L30](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_gather_scatter.py#L24-L30)

```python
@ct.kernel
def array_copy_1d(x, y, TILE: ct.Constant[int]):
    bid = ct.bid(0)
    indices = ct.arange(TILE, dtype=np.int64)   # [0,1,...,TILE-1]
    indices += bid*TILE                           # 偏移到当前 block 的区间
    tx = ct.gather(x, indices)                    # 按元素下标读
    ct.scatter(y, indices, tx)                     # 按元素下标写
```

> 这段代码用 `ct.arange` 生成连续元素下标 `[bid*TILE, bid*TILE+1, ...]`，`gather` 把这些下标处的元素读成一个 tile，`scatter` 再写回 `y` 的相同下标处。注意 `indices` 直接作为元素下标传给 `gather`（1D 数组的单 tile 简写），而不是 `load` 那样的瓦片坐标。

2D 情形展示了「元组长度 = 秩」和「广播」：

[2D gather/scatter 与广播索引 — test/test_gather_scatter.py:L49-L56](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_gather_scatter.py#L49-L56)

```python
@ct.kernel
def array_copy_2d(x, y, TILE_X: ct.Constant[int], TILE_Y: ct.Constant[int]):
    bidx = ct.bid(0)
    bidy = ct.bid(1)
    ind_x = ct.arange(TILE_X, dtype=ct.int32) + bidx * TILE_X   # (TILE_X,)
    ind_y = ct.arange(TILE_Y, dtype=ct.int32) + bidy * TILE_Y   # (TILE_Y,)
    t = ct.gather(x, (ind_x[:, None], ind_y))   # 广播 (TILE_X,1) 与 (TILE_Y,) → (TILE_X, TILE_Y)
    ct.scatter(y, (ind_x[:, None], ind_y), t)
```

> 这段代码对 2D 数组传长度为 2 的元组：行下标 `ind_x[:, None]` 形状 (TILE_X,1)，列下标 `ind_y` 形状 (TILE_Y,)，二者广播成 (TILE_X, TILE_Y)，正好是结果 tile 的形状。这正是 4.1.2 公式中 `ind0`/`ind1` 广播的具体实例。

`mask` + `check_bounds` 的「逻辑与」语义，看带注释的测试最清楚：

[自定义 mask 与边界检查同时生效 — test/test_gather_scatter.py:L261-L285](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_gather_scatter.py#L261-L285)

> 这段代码用一组混合下标（含越界的 15、20）和混合 mask（含 False）验证：只有「mask=True **且** 下标在界内」的位置才真正加载 `x` 的值，其余位置（mask=False 或越界）都填 `padding_value=-1.0`。注释逐元素列出了期望结果，是理解有效掩码的最佳材料。

#### 4.1.4 代码实践

**实践目标**：亲手用 `gather` 实现一个「按下标数组抽取元素」的内核，观察 `padding_value` 与 `check_bounds` 的效果。

**操作步骤**（示例代码，需自行放入可运行环境）：

```python
# 示例代码：需在已安装 cuda-tile 的环境（含 GPU）中运行
import torch, math
import cuda.tile as ct

@ct.kernel
def gather_demo(x, y, N: ct.Constant[int]):
    # 构造下标 [0, 2, 4, 6, 8, 10, 12, 14]，对一个长度仅 10 的数组部分越界
    idx = ct.arange(N, dtype=ct.int32) * 2
    # 默认 check_bounds=True：越界处(下标 12,14 → 元素不存在)填 padding_value
    t = ct.gather(x, idx, padding_value=-1.0)
    ct.scatter(y, ct.arange(N, dtype=ct.int32), t)

x = torch.arange(10, dtype=torch.float32, device="cuda")   # [0..9]
y = torch.zeros(8, dtype=torch.float32, device="cuda")
ct.launch(torch.cuda.current_stream(), (1,), gather_demo, (x, y, 8))
print(y.cpu().tolist())
```

**需要观察的现象**：

- `idx = [0,2,4,6,8,10,12,14]`，其中 10、12、14 越界（数组长度仅 10，最大合法下标是 9）。
- 因此结果应是 `[x[0], x[2], x[4], x[6], x[8], -1.0, -1.0, -1.0]`，即 `[0,2,4,6,8,-1,-1,-1]`。

**预期结果**：`[0.0, 2.0, 4.0, 6.0, 8.0, -1.0, -1.0, -1.0]`。

**延伸观察（待本地验证）**：把 `padding_value=-1.0` 改成 `ct.gather(x, idx, check_bounds=False)` 并维持下标越界，行为是**未定义**的——可能读到随机值或崩溃。这正好对照出 `check_bounds` 的作用：它给 `gather` 注入了一个「边界掩码」，把越界访问变成「返回填充值」的安全行为。

#### 4.1.5 小练习与答案

**练习 1**：对一个 shape 为 (16,) 的 1D 数组，如何用一次 `gather` 调用取出**倒序**的 8 个元素（下标 `[15,14,...,8]`）？写出索引 tile 的构造代码。

**参考答案**：构造一个**递减**下标 tile 即可：`idx = ct.arange(8, dtype=ct.int32, start=15, step=-1)`（`ct.arange` 的 `step` 可以为负，见 u3-l2 工厂节）。然后 `t = ct.gather(x, idx)`。注意 `gather` 本身不要求下标有序或唯一，每个下标都是独立的逐点寻址。

**练习 2**：对一个 shape 为 (4, 4) 的 2D 数组，想取出主对角线 4 个元素 `[x[0,0], x[1,1], x[2,2], x[3,3]]` 成一个 (4,) tile。`indices` 该怎么写？

**参考答案**：行、列下标都用同一个 `arange`：`i = ct.arange(4, dtype=ct.int32)`，然后 `t = ct.gather(x, (i, i))`。两个分量形状都是 (4,)，广播后仍是 (4,)，每个位置 `t[k] = x[i[k], i[k]] = x[k, k]`。

---

### 4.2 scatter：按元素索引写数组

#### 4.2.1 概念说明

`ct.scatter(array, indices, value)` 是 `gather` 的逆操作——按逐点下标**写**而不是读。它和 `gather` 共享同一套 `indices` 约定（元组长度 = 秩、各分量可广播、1D 数组的单 tile 简写），也共享 `mask` 与 `check_bounds` 语义：

- `mask == False` 处**不发生写入**。
- `check_bounds=True`（默认）时，越界下标处**什么都不写**；`check_bounds=False` 时越界写入是 UB。

但 `scatter` 有一个 `gather` 没有的**关键陷阱**：**当多个位置指向同一个下标时（重复下标），写入是未定义行为（UB）。** 这不是 API 的疏忽，而是硬件并行的本质——一个 block 内的集体写入由许多线程并行完成，若多个线程写同一地址，最终留下哪一个是不确定的。

这一点在「直方图」这类需要「多对一累加」的场景尤为要命：直方图必须用 `ct.atomic_add`（u4-l3），**不能**用 `scatter`——`scatter` 是「覆盖写」而非「累加」。

#### 4.2.2 核心流程

`scatter` 的写入规则：

```text
对结果公共形状中的每个位置 (i,j,k,...)：
    若 有效掩码(i,j,k,...) == True 且 check_bounds 保证下标在界内：
        array[ ind0[i,j,k,...], ind1[...], ... ] = value[i,j,k,...]
    否则：
        不写入（越界或 mask=False）

⚠️ 若两个不同位置映射到同一个 (ind0, ind1, ...) 下标 → 未定义行为
    （最终值是其中「某一个」写入，但不指定是哪一个）
```

`value` 可以是标量或 tile，形状须能广播到 `indices` 的公共形状（与 `gather` 的 `padding_value` 同样的广播规则）。

一个常被忽略的安全特性：`scatter` 的越界处理是「**静默忽略**」，不会报错。配合数组的 `slice` 视图使用时，这意味着写到视图范围外的位置会被自动丢弃——这其实是安全的边界裁剪，下面的测试会演示。

#### 4.2.3 源码精读

`scatter` 的权威签名与文档：

[scatter 签名与语义 — src/cuda/tile/_stub.py:L1621-L1663](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1621-L1663)

> 这段代码定义 `scatter(array, indices, value, /, *, mask=None, check_bounds=True, latency=None)`，说明 `value` 须可广播到公共形状、`mask=False` 处不写、越界处不写，并明确「重复下标属 UB」。

`scatter` 静默忽略越界写入的安全特性，看「写到视图外被丢弃」的测试：

[写到切片视图之外被静默忽略 — test/test_gather_scatter.py:L172-L180](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_gather_scatter.py#L172-L180)

> 这段代码把长度 8 的 `y` 取前 5 个元素的视图 `y_slice = y[:5]` 作为写入目标，然后 `scatter` 8 个下标 `[0..7]`。下标 5、6、7 超出视图范围（视图只覆盖 `y[0:5]`），因此这三个写入被**静默忽略**，`y[5:8]` 保留原值 `[105,106,107]`，结果 `y = [10,11,12,13,14,105,106,107]`。这证明了越界写入不会越权写到视图之外的内存。

「重复稀疏下标是 UB」这一陷阱，测试用「只断言未重复位置」的方式规避：

[重复稀疏下标的 UB 行为 — test/test_load_store_advanced_indexing.py:L206-L222](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_load_store_advanced_indexing.py#L206-L222)

> 这段代码用下标 `[0, 0, 4, 6]`（前两个重复指向行 0）调用 `store_advanced_indexing`。注释明确：**行 0 是 UB**（被下标 0 和下标 1 两次写入，最终值未定义），因此测试**不**对行 0 做任何断言，只断言行 4 和行 6（下标唯一）被正确写为 99。这是 cuTile 文档化「重复下标 UB」语义的权威依据。（该 UB 规则对 `scatter` 同样成立。）

`mask` 选择性写入的范本：

[scatter 配合自定义 mask — test/test_gather_scatter.py:L288-L295](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_gather_scatter.py#L288-L295)

> 这段代码用 `[T,F,T,F,T,F,T,F]` 的布尔 mask 调用 `scatter(..., mask=mask_tile, check_bounds=False)`，结果只有偶数位置被写入、奇数位置保持 0。注意这里同时关掉了边界检查（`check_bounds=False`），因为下标都在界内、无需运行时掩码开销。

#### 4.2.4 代码实践

**实践目标**：用 `scatter` 实现「按下标数组**重排**写入」，并亲手验证「重复下标是 UB」。

**操作步骤**（示例代码）：

```python
# 示例代码
import torch
import cuda.tile as ct

@ct.kernel
def scatter_reorder(x, y, N: ct.Constant[int]):
    # 把 x[0..7] 按 [7,6,5,4,3,2,1,0] 的顺序写到 y 的 [0..7]
    src_idx = ct.arange(N, dtype=ct.int32)            # 读 x 的位置 [0..7]
    dst_idx = ct.arange(N, dtype=ct.int32, start=N-1, step=-1)  # 写 y 的位置 [7..0]
    t = ct.gather(x, src_idx)
    ct.scatter(y, dst_idx, t)

x = torch.arange(8, dtype=torch.float32, device="cuda")
y = torch.zeros(8, dtype=torch.float32, device="cuda")
ct.launch(torch.cuda.current_stream(), (1,), scatter_reorder, (x, y, 8))
print(y.cpu().tolist())   # 期望 [7,6,5,4,3,2,1,0]
```

**需要观察的现象**：`y` 被倒序填充，`[7,6,5,4,3,2,1,0]`。所有 `dst_idx` 互不重复，因此写入是良定义的。

**延伸观察（待本地验证）**：把 `dst_idx` 改成全部指向 0（如 `ct.full((N,), 0, dtype=ct.int32)`），即所有位置都写 `y[0]`。运行多次，观察 `y[0]` 的值是否稳定——由于重复下标是 UB，`y[0]` 可能是任意一个被写入的值，且不同运行可能不同。这正是直方图必须用 `atomic_add` 而非 `scatter` 的根本原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么「直方图统计」不能用 `scatter` 实现，而必须用 `ct.atomic_add`？

**参考答案**：直方图是「多对一」累加——多个输入元素可能落入同一个 bin，需要把计数**相加**。`scatter` 是「覆盖写」（最后写者胜），且重复下标是 UB（不保证顺序）。`ct.atomic_add` 对每个 bin 做**原子读-改-写**，保证累加正确且无 UB（u4-l3）。

**练习 2**：`scatter` 写入时若 `value` 的 dtype 与 `array` 的 dtype 不同会怎样？

**参考答案**：会做隐式 cast（与 `store` 一致，u3-l1）。若 cast 方向非法（例如把 float 值隐式写入 int 数组），编译期抛 `TileTypeError`（"cannot implicitly cast"）；若合法则静默转换。`gather`/`scatter` 测试套件用 `_is_implicit_cast_ok` 预判这一点（见 test_gather_scatter.py 的 `test_array_copy_1d`）。

---

### 4.3 load_advanced_indexing：一维稀疏 + 连续切片

#### 4.3.1 概念说明

`gather` 虽然通用，但有个性能隐患：**每个元素都是一次独立的随机访存**。而很多真实场景其实只需要「沿**一个**轴任意取，沿**其余**轴仍取连续块」——最典型的就是 **embedding 查表**：给定一批 token 下标 `[t0, t1, ...]`，从权重表 `W`（shape (词表大小, 向量维 D)）里取出 `W[t0, :]`、`W[t1, :]`……每行是一个**连续**的 D 维向量，只有「选哪一行」是任意的。

对这种「一维稀疏 + 多维连续」的模式，用 `gather` 需要给列维也造一个 `arange(D)` 索引 tile，相当于把「连续的 D 个元素」拆成 D 次独立寻址，浪费了连续性。`ct.load_advanced_indexing` 就是为了保留这种连续性而设计的：

> `indices` 是长度等于 `array.ndim` 的元组。**恰好一个**分量是 1D 整数 tile（「稀疏维 / sparse dim」），**其余**分量是 `ct.Slice(start, length)`（「稠密维 / dense dim」）。

这相当于 NumPy 的「混合高级索引」：一个 fancy-indexed 轴 + 若干切片轴。稠密维描述的是连续区间 `[start, start+length)`，因此后端仍可对这些连续段用 TMA / 合并访存，效率远高于纯 `gather`。

`ct.Slice(start, length)` 是稠密维的描述符：

- `start`：元素空间的**运行时**起始偏移（可以是标量、0D tile，或运行时计算的值）。
- `length`：tile 在该维的长度，必须是 **2 的幂的编译期常量**。

结果 tile 的形状是 `(len_0, ..., len_{n-1})`：稀疏维的长度 = 索引 tile 的长度；稠密维的长度 = 对应 `Slice.length`。

#### 4.3.2 核心流程

设 2D 数组 `x`，`row_indices` 是长度 R 的 1D 整数 tile，`col_slice = ct.Slice(col_start, C)`，则：

```text
tile = ct.load_advanced_indexing(x, (row_indices, col_slice))
# tile 形状 = (R, C)
# tile[r, c] = x[ row_indices[r], col_start + c ]   对 0<=r<R, 0<=c<C
```

用公式表达（2D 情形，稀疏维在第 0 轴）：

\[
\texttt{tile}[r, c] \;=\; \texttt{x}\big[\,\texttt{row\_indices}[r],\;\; \texttt{col\_start} + c\,\big]
\quad\text{对 } 0\le r<R,\;0\le c<C
\]

要点：

- **恰好一个稀疏维**：元组里必须有且仅有一个 1D 整数 tile。多了或少了都会在编译期抛 `TileTypeError`（"exactly one index must be a 1D integer Tile"）。
- **稀疏维必须是 1D**：传 2D 整数 tile 作稀疏维会报错（"1D"）。
- **元组长度 = 数组秩**：少了或多了都会报错（"does not match array rank"）。
- **稠密维 length 必须 2 的幂**：`ct.Slice(0, 3)` 会报错（"power of two"）。
- **越界填充**：稀疏维越界（如 `row_indices` 里某行号 ≥ 行数）与稠密维越界（`Slice` 超出数组）都按 `padding_mode` 填充，默认 `UNDETERMINED`，可设 `ct.PaddingMode.ZERO`（u2-l4）。
- **稀疏维可任意、可重复、可乱序**：与 `scatter` 不同，`load_advanced_indexing` 的稀疏维**重复下标是良定义的**——每个重复下标都独立读出同一行（读操作天然安全）。

`store_advanced_indexing` 用同一套 `indices` 约定，把一个形状须**精确匹配**索引所暗示形状的 tile 写回。和 `scatter` 一样，它的**稀疏维重复下标属 UB**（多写一地址）。

#### 4.3.3 源码精读

`ct.Slice` 是稠密维描述符：

[Slice 类 — src/cuda/tile/_stub.py:L948-L967](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L948-L967)

> 这段代码定义 `Slice(start, length)`：`start` 是元素空间起始偏移，`length` 是 tile 大小，必须是 2 的幂且为编译期常量。它专用于 `load_advanced_indexing`/`store_advanced_indexing` 的稠密维。

`load_advanced_indexing` 的权威签名与文档：

[load_advanced_indexing 签名与语义 — src/cuda/tile/_stub.py:L1463-L1523](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/src/cuda/tile/_stub.py#L1463-L1523)

> 这段代码定义「恰好一个稀疏 1D 整数 tile + 其余 Slice」的约定、结果形状 `(len_0,...,len_{n-1})`、`padding_mode` 对稀疏/稠密两维越界都生效，并给出一个 2D 示例（`ct.Slice(col_start, 4)` 取 4 列）。

「按行稀疏 gather + 列连续切片」的范本内核：

[稀疏行 gather 内核 — test/test_load_store_advanced_indexing.py:L41-L55](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_load_store_advanced_indexing.py#L41-L55)

```python
@ct.kernel
def gather_even_rows(x, y, ROWS: ct.Constant[int], COLS: ct.Constant[int]):
    indices = ct.arange(ROWS, dtype=ct.int32) * 2      # [0, 2, 4, 6] —— 稀疏维(行)
    tile = ct.load_advanced_indexing(x, (indices, ct.Slice(0, COLS)))  # 取每行的前 COLS 列
    ct.store(y, (0, 0), tile)
```

> 这段代码取出 `x` 的**偶数行**（行下标 `[0,2,4,6]`，稀疏维），每行取前 `COLS` 列（稠密维 `Slice(0, COLS)`）。结果 tile 形状 `(ROWS, COLS)`，相当于 `x[::2, :COLS]`。这正是「embedding 查表」的形态：`indices` 是 token 下标，`Slice(0, D)` 取整行向量。

稠密维 `start` 可以是运行时值（动态列起点）：

[动态 Slice.start — test/test_load_store_advanced_indexing.py:L80-L95](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_load_store_advanced_indexing.py#L80-L95)

> 这段代码把 `Slice(col_start, COLS)` 的 `col_start` 作为运行时参数传入，验证可取任意起点开始的连续 `COLS` 列。`start` 是运行时值、`length`（COLS）是编译期常量，体现了「稠密维 = 运行时起点 + 编译期长度」的设计。

稀疏维越界用 `padding_mode=ZERO` 填充：

[稀疏维部分越界零填充 — test/test_load_store_advanced_indexing.py:L166-L181](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_load_store_advanced_indexing.py#L166-L181)

> 这段代码对 8 行数组用下标 `[6,7,8,9]`（后两个越界），`padding_mode=ZERO` 时越界行被填 0。注释明确「6、7 在界内，8、9 越界」并给出期望结果，是理解稀疏维边界行为的最小例子。

各类约束错误的判定：

[错误用例集 — test/test_load_store_advanced_indexing.py:L304-L323](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_load_store_advanced_indexing.py#L304-L323)

> 这段代码集中展示两类错误：(1) 索引元组长度 ≠ 数组秩（报 "does not match array rank"）；(2) 稠密维 `Slice` 的 length 不是 2 的幂（报 "power of two"）。同文件 L243-L301 还覆盖了「无稀疏维」「多个稀疏维」「稀疏维是 2D tile」等错误，统一报 "exactly one index must be a 1D integer Tile" 或 "1D"。

#### 4.3.4 代码实践

**实践目标**：用 `load_advanced_indexing` 实现一个最小 embedding 查表，并对照 PyTorch 的 fancy indexing 验证数值。

**操作步骤**（示例代码）：

```python
# 示例代码
import torch
import cuda.tile as ct

@ct.kernel
def embed_lookup(W, idx, out, VOCAB: ct.Constant[int], D: ct.Constant[int]):
    # idx: 下标数组(稀疏维)  W: 权重表 (VOCAB, D)
    # 取出 idx 指定的若干整行，每行前 D 列
    rows = ct.load_advanced_indexing(W, (idx, ct.Slice(0, D)))
    ct.store(out, (0, 0), rows)

V, D, B = 16, 4, 4                    # 词表16, 向量维4, 查4个token
W = torch.arange(V * D, dtype=torch.float32, device="cuda").reshape(V, D)
idx = torch.tensor([3, 0, 7, 3], dtype=torch.int32, device="cuda")   # 含重复下标 3
out = torch.zeros(B, D, dtype=torch.float32, device="cuda")
ct.launch(torch.cuda.current_stream(), (1,), embed_lookup, (W, idx, out, V, D))
print(out.cpu().tolist())
# 对照
print(W[idx].cpu().tolist())
```

**需要观察的现象**：

- `out` 的每一行 = `W` 中对应下标的整行，形状 `(B, D)`。
- 注意 `idx` 里下标 3 出现了两次——对 `load_advanced_indexing`（读）这是**良定义**的：`out[0]` 和 `out[3]` 都等于 `W[3]`。

**预期结果**：`out` 与 `W[idx]`（PyTorch fancy indexing）逐元素相等。

**延伸观察（待本地验证）**：把上面的 `load_advanced_indexing` + `store` 换成等价的纯 `gather` 写法——给列维也造索引 `col = ct.arange(D, dtype=ct.int32)`，`rows = ct.gather(W, (idx[:, None], col[None, :]))`。功能相同，但语义上把连续的 D 列拆成了 D 次逐点寻址，性能通常更差。这正是 `load_advanced_indexing` 存在的价值（见 4.4）。

#### 4.3.5 小练习与答案

**练习 1**：对一个 shape (8, 8) 的 2D 数组，下面哪个调用合法？为什么？
(a) `ct.load_advanced_indexing(x, (ct.Slice(0,4), ct.Slice(0,4)))`
(b) `ct.load_advanced_indexing(x, (idx, ct.Slice(0,4)))`（`idx` 是 (4,) 整数 tile）
(c) `ct.load_advanced_indexing(x, (idx, ct.Slice(0,3)))`

**参考答案**：只有 **(b)** 合法。(a) 没有稀疏维（全是 Slice）→ 报 "exactly one index must be a 1D integer Tile"。(c) 稀疏维有了，但稠密维 `Slice(0,3)` 的 length 3 不是 2 的幂 → 报 "power of two"。(b) 恰好一个稀疏维 `idx`、一个稠密维 `Slice(0,4)`（length 4 是 2 的幂），合法，结果形状 (4, 4)。

**练习 2**：`load_advanced_indexing` 的稀疏维允许重复下标（良定义），但 `store_advanced_indexing` 的稀疏维重复下标却是 UB。为什么读和写的规则不同？

**参考答案**：读操作天然幂等——多个位置读同一地址得到相同值，无副作用，完全安全。写操作是「覆盖」——多个位置写同一地址时，硬件并行写入的顺序不确定，最终值未定义。因此读允许重复下标、写不允许。这与 `gather`（读，越界/重复都安全）和 `scatter`（写，重复下标 UB）的区分完全一致。

---

### 4.4 三类读操作的对齐与性能（综合对比）

在收尾前，把本讲的 `gather`、`load_advanced_indexing` 与前置的 `load`/`TiledView.load` 放在一起对比，帮你建立「该用哪个」的直觉。核心维度是**访存规整度**——越规整，越能合并（coalesce）或走 TMA，越快：

| 操作 | 索引方式 | 访问区域 | 规整度 | 典型用途 |
|------|----------|----------|--------|----------|
| `load` / `tv.load` | 瓦片坐标（tile space） | 网格对齐的**矩形连续块** | 最高（可 TMA / 合并） | GEMM、LayerNorm 等规整 tile 计算 |
| `load_advanced_indexing` | 一维**稀疏**下标 + 稠密连续 `Slice` | 若干**整行/整列**（稀疏散乱，稠密连续） | 中（稠密维仍可合并） | embedding 查表、按行索引抽取 |
| `gather` | **全逐点**元素下标 | 每个元素各自任意位置 | 最低（最散乱） | 任意重排、top-k、完全非连续抽取 |

经验法则：

- **能规整就规整**：如果你的访问模式是规则矩形块，永远优先 `load`/`TiledView`。
- **一维任意、其余连续**：用 `load_advanced_indexing`，保留稠密维的连续性，比 `gather` 快。
- **真的每个元素都任意**：才用 `gather`，接受它的随机访存代价。

一个常被忽略的对齐细节：`gather` 的 `check_bounds=True` 会给底层 `LoadPointer` 操作注入一个**运行时边界掩码**，使越界访问变成「返回填充值」的安全行为；`check_bounds=False` 则不生成掩码、访问更快但越界是 UB。这套机制在测试里通过检查 IR 中 `LoadPointer.mask` 是否为 `None` 来验证：

[checked vs unchecked 在 IR 上的体现 — test/test_gather_scatter.py:L197-L216](https://github.com/nvidia/cutile-python/blob/0c46a6222c61217a3fa740f01a1b14c9fef0ecec/test/test_gather_scatter.py#L197-L216)

> 这段代码编译同一个内核的两个版本（`check_bounds` 为 True/False），遍历最终 IR 断言：开启边界检查时 `LoadPointer` 的 `mask` 非 None，关闭时为 None。这把「安全但略慢」与「快但 UB」的取舍落实到了可见的 IR 层面。

## 5. 综合实践：embedding gather 内核

把本讲三个模块串起来，实现一个完整的 embedding 查表内核，并对比两种实现路径。

**任务**：给定权重表 `W`（shape (V, D)）和 token 下标数组 `idx`（shape (B,)），输出 `out`（shape (B, D)），其中 `out[b] = W[idx[b]]`。

**路径 A（推荐）——用 `load_advanced_indexing`**：稀疏维 = 行下标，稠密维 = `Slice(0, D)` 取整行。保留列方向的连续性，是 embedding 查表的惯用法。

```python
# 示例代码
import torch
import cuda.tile as ct

@ct.kernel
def embed_lookup_advanced(W, idx, out, D: ct.Constant[int]):
    rows = ct.load_advanced_indexing(W, (idx, ct.Slice(0, D)))
    ct.store(out, (0, 0), rows)
```

**路径 B（对比）——用纯 `gather`**：给行、列各造一个索引 tile 并广播。

```python
@ct.kernel
def embed_lookup_gather(W, idx, out, B: ct.Constant[int], D: ct.Constant[int]):
    col = ct.arange(D, dtype=ct.int32)              # (D,)
    rows = ct.gather(W, (idx[:, None], col[None, :]))  # 广播 (B,1)+(1,D) → (B,D)
    ct.store(out, (0, 0), rows)
```

**验证步骤**：

1. 用 `V=32, D=8, B=16`，随机初始化 `W` 和 `idx`（可含重复 token）。
2. 分别用两条路径各跑一次，得到 `out_A`、`out_B`。
3. 用 PyTorch 的 `W[idx]` 作为参考，断言三者逐元素相等。
4.（可选，待本地验证）用 Nsight Compute 或事件计时对比两条路径的耗时，预期路径 A 不慢于路径 B。

**思考题**：

- 为什么路径 A 在列方向更高效？（提示：稠密维 `Slice` 是连续区间，可合并访存 / TMA；路径 B 把每行的 D 个元素拆成 D 次逐点寻址。）
- 若 `idx` 含重复 token（如 `[3, 3, 7, ...]`），路径 A 和路径 B 都安全吗？（提示：读操作的重复下标永远良定义。）
- 若把任务反过来——「按 `idx` 把 `out` 的若干行**写回** `W`」（参数更新），该用哪个 API？重复 token 还安全吗？（提示：写操作的重复下标是 UB；多对一更新须用 `atomic_*`。）

## 6. 本讲小结

- **`gather`/`scatter` 是逐点（pointwise）元素空间寻址**：`indices` 是长度等于数组秩的元组，各分量是可广播的整数 tile，结果形状 = 广播形状；1D 数组可省略元组直接传单 tile。
- **`mask` + `padding_value` + `check_bounds` 三者通过「有效掩码」协同**：`gather` 越界或 `mask=False` 返回 `padding_value`；`scatter` 越界或 `mask=False` 不写入；`check_bounds=False` 关闭运行时边界掩码（更快但越界是 UB）。
- **`scatter` 的重复下标是未定义行为（UB）**——因为硬件并行写入顺序不确定；直方图等多对一累加必须改用 `ct.atomic_add`（u4-l3）。
- **`load_advanced_indexing`/`store_advanced_indexing` 是「一维稀疏 + 多维连续切片」**：元组里恰好一个 1D 整数 tile（稀疏维），其余是 `ct.Slice(start, length)`（稠密维，length 须为 2 的幂编译期常量）。
- **读的重复下标良定义、写的重复下标 UB**——这是 `load_advanced_indexing` 与 `store_advanced_indexing`、以及 `gather` 与 `scatter` 共同的区分。
- **性能直觉**：访存规整度 `load` > `load_advanced_indexing` > `gather`；能规整就规整，一维任意才用 advanced indexing，全逐点才用 gather。

## 7. 下一步学习建议

- **u4-l3（内存模型与原子操作）**：本讲反复提到「直方图/多对一累加要用 `atomic_add`」，下一讲会系统讲解 cuTile 的内存模型（`MemoryOrder`/`MemoryScope`）与原子操作族（`atomic_add/max/cas/...`）、`fence`，正是 `scatter` 无法覆盖的场景。建议紧接着学。
- **重读 u5-l7（stub 与实现注册）**：本讲的 `gather`/`scatter`/`load_advanced_indexing` 都是 `@stub`，若你想知道它们在后端如何被分派到具体的 IR 操作（如 `LoadPointer`/`StorePointer` 带掩码的版本），u5-l7 给出了从 `@stub` 到 `@impl` 的完整注册链路。
- **源码延伸**：想看 `check_bounds` 如何在 IR 层注入掩码，可结合 u6（优化 Pass）阅读 `LoadPointer`/`StorePointer` 的定义与数据流分析；想看字节码如何编码这些带掩码的访存，可参考 u7-l1（ir2bytecode）。
