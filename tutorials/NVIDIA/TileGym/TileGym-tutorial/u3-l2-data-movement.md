# 数据搬运原语：load/store 与 gather/scatter

## 1. 本讲目标

上一讲（u3-l1）我们看懂了一个 cuTile 内核的「骨架」——装饰器、签名、`ConstInt`、`ct.bid`、grid-stride 循环。但当时我们刻意跳过了一个最关键的问题：**内核里的数据是怎么从显存搬到片上、又怎么写回去的？** 本讲就把这块补上。学完后你应该能够：

- 区分 cuTile 的两套数据搬运原语：`load`/`store` 与 `gather`/`scatter`，并能说出它们各自「描述要访问的数据」的方式有何不同。
- 理解 `ct.arange` 如何生成偏移索引，以及它为什么是 gather/scatter 的好搭档。
- 看懂 `check_bounds`、`padding_value`、`padding_mode` 这三组边界/填充参数，并能在不同算子里解释它们的选择。
- 掌握 `ct.astype` 在「加载即转 fp32、计算完转回原类型」这条流水线里的位置与作用。

本讲只讲**数据搬运**，不展开启动细节（`ct.launch` 与 grid 计算是 u3-l3 的内容），也不展开 softmax 四种变体的完整算法（那是 u3-l4）。

## 2. 前置知识

在进源码前，先用三段话建立直觉。

**为什么数据搬运值得单独讲一讲。** GPU 内核的计算速度通常远快于显存带宽——也就是说，算力不是瓶颈，「把数据喂给算力」才是。tile 编程模型的全部意义就在于：用一小块一小块的「瓦片（tile）」在快速的片上存储（寄存器 / 共享内存）里反复计算，尽量少碰慢吞吞的全局显存。所以「怎么描述一块瓦片、怎么把它搬进来、怎么搬出去」是写内核时最高频、也最影响性能的动作。cuTile 把这些动作封装成了几个原语，本讲就是讲它们。

**两种「描述一块数据」的视角。** 同样是要从一张大表 `(N_ROWS, N_COLS)` 里取出「第 `row_idx` 行、前 `TILE_SIZE` 个元素」这一块瓦片，cuTile 给了你两种描述方式：

- **「锚点 + 形状」视角**（`load`/`store`）：我告诉它「从位置 `(row_idx, 0)` 开始，取一个形状为 `(1, TILE_SIZE)` 的矩形块」。这就像在二维表格上画一个矩形选区，起点加上宽高。这种矩形、连续的访问最适合硬件的 TMA（Tensor Memory Accelerator，张量内存加速器）单元，也方便编译器判断是否越界。
- **「索引数组」视角**（`gather`/`scatter`）：我直接给它一串下标 `(row_idx, [0,1,2,...,TILE_SIZE-1])`，说「按这串下标去取」。这更像 NumPy 的 fancy indexing——你可以取连续的一段，也可以取任意稀疏的位置（比如「第 5、第 9、第 100 列」）。它更灵活，但通常走更通用的「按地址取数（LDG）」路径。

**一个贯穿全讲的例子。** TileGym 的 softmax 正好同时给出了这两种写法：它的 **basic / multi-wave / chunked** 变体用 `gather`/`scatter`，而 **TMA** 变体用 `load`/`store`。我们对照这两个内核，就能看清两套原语在「同一道题」下的差别。`silu_and_mul` 则进一步展示了 gather/scatter 如何用 `ct.arange` + 加法拼出「第二段数据」的索引。

> 本讲承接 u3-l1：你已经认识 `@ct.kernel`、`ConstInt`、`ct.bid`。本讲假设你能看懂内核骨架，我们把注意力完全放在函数体里那几行 `ct.load` / `ct.gather` / `ct.scatter` 上。

## 3. 本讲源码地图

本讲涉及两个核心源文件和一个辅助文件：

| 文件 | 作用 |
| --- | --- |
| `src/tilegym/ops/cutile/softmax.py` | cuTile 版 softmax，含 4 个内核变体。本讲重点对照其中的 **basic 内核**（`gather`/`scatter`）与 **TMA 内核**（`load`/`store`），并顺带看 **chunked 内核**如何用 `arange`+加法拼索引。 |
| `src/tilegym/ops/cutile/silu_and_mul.py` | cuTile 版 `silu_and_mul`，用 `gather`/`scatter` 同时加载「前半段」和「后半段」两块瓦片，是 arange + 偏移的经典例子。 |
| `src/tilegym/ops/cutile/utils.py` | 提供 `next_power_of_2`，launch 时把列数向上取整到 2 的幂，这决定了 `TILE_SIZE`，从而决定 `ct.arange(TILE_SIZE)` 的长度。 |

## 4. 核心概念与源码讲解

### 4.1 load/store：基于「锚点 + 形状」的瓦片访问

#### 4.1.1 概念说明

`ct.load` 和 `ct.store` 是一对**面向矩形瓦片**的搬运原语。它们用两个关键参数描述要访问的数据：

- `index`：一个**锚点**（anchor），即这块瓦片在大张量里的「左上角」坐标，例如 `(row_idx, 0)`。锚点的每个分量可以是标量，也可以是瓦片（用于批量取多块）。
- `shape`：这块瓦片的**形状**，例如 `(1, TILE_SIZE)`，表示「1 行、`TILE_SIZE` 列」。

`load` 据此从大张量里「画」出一个 `shape` 大小的矩形块，搬成一片上瓦片；`store` 则把一片算好的瓦片按同样的「锚点 + 形状」写回大张量。因为是规则的矩形访问，它天然贴合硬件的 TMA 单元（Hopper 即 sm90+ 才有真正的 TMA 硬件，更早的架构会做软件仿真）。

#### 4.1.2 核心流程

`load` 的语义可以近似理解为：

```
tile = ct.load(tensor, index=anchor, shape=tile_shape, padding_mode=...)
# 等价于：在大张量上，以 anchor 为左上角，切出一个 tile_shape 的矩形，
#        搬到片上返回一个形状 == tile_shape 的瓦片。
```

注意：返回瓦片的**形状由 `shape` 参数决定**，而不是由 `index` 决定。这一点和 gather 不同（gather 的结果形状由索引数组的广播决定），是两套原语最容易混淆的地方，4.2 节会对照说明。

`store` 是反过来的动作，多一个 `tile=` 参数表示「要写回去的这片瓦片」：

```
ct.store(tensor, index=anchor, tile=computed_tile)
# 等价于：把 computed_tile 这片瓦片，写回大张量中以 anchor 为左上角的矩形位置。
```

#### 4.1.3 源码精读

TMA 版 softmax 内核用 `load` 一次取整行，算完用 `store` 写回：

[softmax.py:80-94 —— `_softmax_kernel_tma` 的开头与 load 调用](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L80-L94) —— 注意第 94 行 `ct.load(input, index=(row_idx, 0), shape=(1, TILE_SIZE), padding_mode=ct.PaddingMode.NEG_INF)`：锚点是 `(row_idx, 0)`，形状是 `(1, TILE_SIZE)`，即「从第 `row_idx` 行第 0 列起，取 1 行 × `TILE_SIZE` 列」。返回的是一个**二维**瓦片 `(1, TILE_SIZE)`。

[softmax.py:100](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L100) —— `row_max = ct.max(row, 1, keepdims=True)`：因为 `load` 返回的是二维瓦片，所以这里的归约轴是 `1`（沿列方向归约）。请记住这个 `1`，4.2 节会看到 gather 版用的是 `0`。

[softmax.py:113-114](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L113-L114) —— `softmax_output = ct.astype(softmax_output, input.dtype)` 把结果转回原始类型，随后 `ct.store(output, index=(row_idx, 0), tile=softmax_output)` 用同样的「锚点 + 形状」写回。读和写用了**对称**的 `index=(row_idx, 0)`，形状则由 `tile` 本身 `(1, TILE_SIZE)` 隐式给出。

> 说明：TMA 版要求 `TILE_SIZE >= n_cols`，即一行能被一片瓦片装下（见 [softmax.py:93](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L93) 的注释）。所以它适合「列数适中、一次装下一整行」的场景；列数太大装不下时就得改用 chunked 版本（4.3 节）。

#### 4.1.4 代码实践

**实践目标**：确认 `load` 返回瓦片的形状与归约轴的关系。

1. 打开 `src/tilegym/ops/cutile/softmax.py`，对比第 94 行（TMA 版 `load`）与第 100 行（`ct.max(row, 1, ...)`）。
2. 把第 94 行的 `shape=(1, TILE_SIZE)` 想象成改成 `shape=(TILE_SIZE,)`（一维），思考：如果瓦片变成一维，第 100 行的归约轴应该相应改成几？
3. 在你本地的 H100 / Blackwell 机器上（sm ≥ 9），运行：

```bash
pytest tests/ops/test_softmax.py -k "use_tma" -x
```

**需要观察的现象**：测试应全部通过（TMA 路径生效）；若你在 sm < 9 的卡上跑，前向逻辑里 `use_tma` 会被自动关掉并告警（见 [softmax.py:323-332](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L323-L332)），那时走的就不是 `load` 路径了。

**预期结果**：sm ≥ 9 时 TMA 测试通过；sm < 9 时该变体回退到 basic 路径。**待本地验证**（取决于你的卡型）。

#### 4.1.5 小练习与答案

- **练习 1**：TMA 版 `store` 为什么没有显式传 `shape` 参数？
  - **答**：因为 `store` 要写回的形状已经由 `tile=softmax_output` 这片瓦片本身的形状 `(1, TILE_SIZE)` 隐式给出，只需 `index` 锚点定位「写到哪里」即可。
- **练习 2**：如果把 `ct.load(input, index=(row_idx, 0), shape=(1, TILE_SIZE))` 的 `index` 改成 `(row_idx, 5)`，取到的是哪一块？
  - **答**：从第 `row_idx` 行第 5 列起、形状仍为 `(1, TILE_SIZE)` 的矩形块，即第 `row_idx` 行的第 `5..5+TILE_SIZE-1` 列。

---

### 4.2 gather/scatter：基于「索引数组」的访问

#### 4.2.1 概念说明

`ct.gather` 和 `ct.scatter` 是一对**面向索引数组**的搬运原语。它们不再用「锚点 + 矩形形状」，而是直接给每一维一个**索引（下标）**，按这串下标去取/放元素：

- `gather(tensor, (idx_dim0, idx_dim1, ...))`：`idx_dimK` 可以是标量，也可以是瓦片。各维索引会**广播**到同一形状，然后逐元素地「按下标取数」，返回一个形状等于广播结果的瓦片。
- `scatter(tensor, (idx_dim0, idx_dim1, ...), value)`：把 `value` 这片瓦片按下标逐元素写回去。

它的表达力比 `load`/`store` 强：你可以取连续的一段（下标是 `arange`），也可以取任意稀疏的位置（下标是任意计算的瓦片）。代价是它通常走更通用的「按地址取数 / 存数」路径，访问模式不如矩形 TMA 那么规整。

#### 4.2.2 核心流程

最典型的用法——取一行的前 `TILE_SIZE` 列：

```
offsets = ct.arange(TILE_SIZE, dtype=ct.int32)      # [0,1,2,...,TILE_SIZE-1]
row = ct.gather(input, (row_idx, offsets), check_bounds=True, padding_value=-math.inf)
```

这里 `row_idx` 是标量，`offsets` 是长度为 `TILE_SIZE` 的瓦片。二者广播，等价于按下标 `(row_idx, 0), (row_idx, 1), ..., (row_idx, TILE_SIZE-1)` 取数，返回**一维**瓦片 `(TILE_SIZE,)`。

`scatter` 的写回与之对称：

```
ct.scatter(output, (row_idx, offsets), softmax_output, check_bounds=True)
```

#### 4.2.3 源码精读

basic softmax 内核正是这个写法：

[softmax.py:29-33](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L29-L33) —— 第 29 行 `offsets = ct.arange(TILE_SIZE, dtype=ct.int32)` 生成列下标；第 33 行 `row = ct.gather(input, (row_idx, offsets), check_bounds=True, padding_value=-math.inf)` 按下标取出一行。注意返回的是**一维**瓦片。

[softmax.py:38](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L38) —— `row_max = ct.max(row, 0, keepdims=True)`：因为 gather 返回一维瓦片，归约轴是 `0`。**和 4.1 节 TMA 版的 `ct.max(row, 1, ...)` 对照**——同一个 softmax 算法，仅仅因为换了加载原语，瓦片维度不同，归约轴就要跟着变。这是 load/gather 最容易踩的坑。

[softmax.py:49-50](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L49-L50) —— 转回原类型后 `ct.scatter(output, (row_idx, offsets), softmax_output, check_bounds=True)` 写回，索引与 gather 时完全对称。

`silu_and_mul` 进一步展示了「索引加偏移」取第二段数据的技巧：

[silu_and_mul.py:29-40](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L29-L40) —— 第 34-35 行 `a_col_idx = offsets`（前半段 `[0, hidden)`），`b_col_idx = offsets + TOTAL_HIDDEN_SIZE`（后半段 `[hidden, 2*hidden)`）。同一组 `offsets` 加不同的常量偏移，得到两段不同的列下标。第 39-40 行两次 `ct.gather` 分别取出 `a`、`b` 两片瓦片。这就是 gather 的灵活之处——用算术拼出任意下标，而 load 的「矩形」做不到「取两段不连续的数据」。

> 第 38 行注释点明了广播规则：「gather broadcasts `(scalar, tile)` to `(tile,)`」——标量行号与瓦片列号广播成一维结果。

#### 4.2.4 代码实践

**实践目标**：体会「索引加偏移」的表达力。

1. 阅读 [silu_and_mul.py:34-40](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L34-L40)，确认 `a_col_idx` 与 `b_col_idx` 是同一份 `offsets` 加不同常量。
2. 思考：如果要用 `load`（矩形）取 `silu_and_mul` 需要的 `a`、`b` 两段，需要几次 `load`？分别取什么矩形？
3. **预期结果**：gather 只需构造两个下标瓦片，一次取一段；load 则要发两次独立的矩形 `load`（一个锚点在 `0`，一个在 `TOTAL_HIDDEN_SIZE`），且每次都取 `(1, TILE_SIZE)`。这说明 gather 在「取若干不连续段」时书写更紧凑。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 basic softmax 用 `ct.max(row, 0, ...)`，而 TMA 版用 `ct.max(row, 1, ...)`？
  - **答**：gather 返回一维瓦片 `(TILE_SIZE,)`，沿唯一的轴 `0` 归约；load 返回二维瓦片 `(1, TILE_SIZE)`，沿列方向即轴 `1` 归约。加载原语决定了瓦片的维度布局。
- **练习 2**：用 gather 取「第 `r` 行的第 3、7、11 列」该怎么写索引？
  - **答**：把列下标构造成一个瓦片 `[3,7,11]`（例如 `ct.full((3,), 3, ...) + ct.arange(3, ...)*...` 或预构造），然后 `ct.gather(input, (r, col_idx), ...)`，标量 `r` 与瓦片 `col_idx` 广播，返回长度为 3 的一维瓦片。

---

### 4.3 arange、check_bounds 与 padding：索引生成与边界处理

#### 4.3.1 概念说明

这一节把三个紧密相关的概念放一起：**怎么生成索引**、**要不要查越界**、**越界处填什么**。

- **`ct.arange(N, dtype=...)`**：生成一个长度为 `N` 的瓦片，内容是 `[0, 1, 2, ..., N-1]`。它是 gather/scatter 最常用的「列下标发生器」。因为 `N` 必须在编译期已知，所以 `N` 一般是 `ConstInt`（如 `TILE_SIZE`）——这也是 u3-l1 讲过的「编译期常量」的典型用途。
- **`check_bounds`（gather/scatter 用）**：一个布尔值，决定是否对索引做越界检查。
  - `True`：越界的读取位置返回 `padding_value`，越界的写入位置被丢弃（不写）。
  - `False`：不做检查，性能更好，但要求调用方保证索引绝不越界（否则是非法访问）。
  - 特别地，它可以是一个**编译期常量表达式**，如 `TILE_SIZE != N`：当 `TILE_SIZE` 恰好等于实际列数（列数本身是 2 的幂）时，整个检查在编译期就被消去。
- **`padding_value`（gather 用）**：一个**标量**，越界读取处填这个值。
- **`padding_mode`（load 用）**：一个**枚举**，如 `ct.PaddingMode.NEG_INF`（负无穷）或 `ct.PaddingMode.ZERO`（零），越界处填该模式对应的值。

注意这两套填充参数的分属：`gather` 配 `padding_value`（标量），`load` 配 `padding_mode`（枚举），不要混用。

#### 4.3.2 核心流程

softmax 选 `-inf` 作填充不是随手的——它让填充元素在数学上「消失」。softmax 的分母是 \(\sum_j \exp(x_j - \max)\)，填充处取 \(-\infty\) 时：

\[
\exp(-\infty - \max) = \exp(-\infty) = 0
\]

于是填充元素对分子、分母都贡献 0，等价于「这一行根本没有这些列」。这是处理「列数不是 `TILE_SIZE` 的整数倍、末尾有填充」的正确姿势。

当列数很大、一片瓦片装不下整行时，chunked softmax 用「`arange` 基底 + 每块偏移」生成不同列段的下标：

```
col_offsets_base = ct.arange(TILE_SIZE)              # [0,1,...,TILE_SIZE-1]  固定基底
for chunk_idx in range(num_chunks):
    chunk_start = chunk_idx * TILE_SIZE
    col_indices = ct.full((TILE_SIZE,), chunk_start) + col_offsets_base
    chunk = ct.gather(input, (row_idx, col_indices), check_bounds=True, padding_value=-inf)
```

即第 `c` 块的列下标是 `[c*TILE_SIZE, c*TILE_SIZE+1, ..., c*TILE_SIZE+TILE_SIZE-1]`——基底 `arange` 复用，每块只换一个起始偏移。

#### 4.3.3 源码精读

basic softmax 的 arange + gather + 负无穷填充三件套：

[softmax.py:29](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L29) —— `offsets = ct.arange(TILE_SIZE, dtype=ct.int32)`，生成列下标基底。

[softmax.py:33](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L33) —— `ct.gather(input, (row_idx, offsets), check_bounds=True, padding_value=-math.inf)`：开启越界检查，越界处填 \(-\infty\)，保证填充列在 softmax 里贡献为 0。

multi-wave 版展示了「把 `check_bounds` 设成编译期常量」来省掉检查：

[softmax.py:64-68](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L64-L68) —— 第 66 行 `check_bound = TILE_SIZE != N`，第 68 行把它传给 gather。结合第 60-63 行的注释：当 `TILE_SIZE == N`（列数恰为 2 的幂、瓦片正好装满、无填充）时，`check_bound` 在编译期为 `False`，越界检查整段被消去，省下运行时开销。

chunked softmax 的「基底 + 偏移」拼索引：

[softmax.py:135-141](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L135-L141) —— 第 135 行先算出固定的 `col_offsets_base`；第 140 行 `col_indices = ct.full((TILE_SIZE,), chunk_start, dtype=ct.int32) + col_offsets_base` 给每块换一个起始；第 141 行 gather 按新下标取这一段。

load 的填充用枚举 `padding_mode`：

[softmax.py:94](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L94) —— `padding_mode=ct.PaddingMode.NEG_INF`：load 不吃标量 `padding_value`，而是吃这个枚举（同样取负无穷，和 gather 的 `-math.inf` 语义一致，只是参数形式不同）。仓库里还有 `ct.PaddingMode.ZERO` 的用例，见 [flashinfer/cutile/fmha_prefill_bsr.py:82](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/suites/flashinfer/cutile/fmha_prefill_bsr.py#L82)。

`TILE_SIZE` 来自哪里：launch 时把列数向上取 2 的幂，决定 arange 长度：

[softmax.py:333-334](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L333-L334) —— `TILE_SIZE = next_power_of_2(n_cols)`，所以 `ct.arange(TILE_SIZE)` 的长度永远 ≥ 实际列数，多出来的位置靠 `padding_value=-inf` 兜住。

#### 4.3.4 代码实践

**实践目标**：验证「`TILE_SIZE` 是 2 的幂时检查可省」这一设计。

1. 读 [softmax.py:64-68](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L64-L68)，解释 `check_bound = TILE_SIZE != N` 在两种情形下的取值。
2. 跑测试，分别覆盖「列数是 2 的幂」和「列数不是 2 的幂」两类用例：

```bash
pytest "tests/ops/test_softmax.py::Test_Softmax::test_op" \
  -k "use_multi_wave and (256-2048 or 256-1009)"
```

**需要观察的现象**：列数 2048（2 的幂，`TILE_SIZE==N`）时检查被编译期消去；列数 1009（非 2 的幂，`TILE_SIZE=1024≠N`）时检查保留，末尾 15 个填充位置填 `-inf` 后被 softmax 正确忽略。两类结果都应与 torch 参考在容差内一致。

**预期结果**：两类用例都通过，最大绝对误差满足 `rtol=1e-5, atol=1e-7`（fp32）。**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 softmax 用 `-inf` 填充，而很多 GEMM 内核用 `0` 填充（`PaddingMode.ZERO`）？
  - **答**：softmax 里填充元素参与 `exp` 求和，需 `exp(-inf)=0` 使其贡献为 0；GEMM 里填充元素参与乘加，填 0 等于「该位置不贡献」，正好对齐「缺失数据当 0」的语义。选择取决于「这个值在后续运算里取什么才安全」。
- **练习 2**：`check_bounds=False` 何时才安全？
  - **答**：当且仅当你能保证所有索引都落在张量范围内。multi-wave 版用 `TILE_SIZE != N` 在编译期判断：`TILE_SIZE == N`（无填充）时索引必不越界，故可关掉检查；否则必须开启。

---

### 4.4 astype：类型转换的时机与意义

#### 4.4.1 概念说明

`ct.astype(tile, dtype)` 把一片瓦片转成指定 `dtype`，等价于把片上瓦片重新解释/舍入成另一种数值类型。它在 cuTile 内核里几乎总是出现在两个固定位置，构成一条「加载即转 fp32、计算完转回原类型」的流水线：

1. **加载后**：`row = ct.astype(row, ct.float32)`——把可能是 fp16/bf16 的输入升到 fp32，再做归约、指数等对精度敏感的计算。
2. **写回前**：`out = ct.astype(out, input.dtype)`——把 fp32 结果降回输入的原始类型，再 store/scatter。

为什么要这样安排？因为 fp16/bf16 的有效位数少，直接在低精度下做 `exp`、求和、相除这类运算，误差会迅速放大甚至溢出。统一升到 fp32 算完，再降回去，能在「存储紧凑」和「计算精确」之间取得平衡。这是几乎所有数值内核的通用做法。

`dtype` 既可以用 `ct.float32`（来自 `cuda.tile`），也可以直接用 `torch.float32`——cuTile 接受 torch 的 dtype 常量，两份内核正好各用了一种写法（见源码精读）。

#### 4.4.2 核心流程

完整的类型流水线（以 basic softmax 为例）：

```
row = ct.gather(...)                      # 原类型（可能是 fp16）
row = ct.astype(row, ct.float32)          # ① 升精度
... max / exp / sum / div 都在 fp32 下做 ...
softmax_output = ct.astype(..., input.dtype)  # ② 降回原类型
ct.scatter(output, ..., softmax_output)   # 写回
```

#### 4.4.3 源码精读

softmax 里两次 astype 构成这条流水线：

[softmax.py:35](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L35) —— `row = ct.astype(row, ct.float32)`：加载后立刻升到 fp32，用 `ct.float32` 写法。

[softmax.py:49](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L49) —— `softmax_output = ct.astype(softmax_output, input.dtype)`：写回前降回「输入的原始类型」`input.dtype`，保证输出与输入同类型。

`silu_and_mul` 用的是 torch dtype 写法，且对 a、b 两片分别升精度：

[silu_and_mul.py:41-42](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L41-L42) —— `a_tile = ct.astype(a_tile, torch.float32)`、`b_tile = ct.astype(b_tile, torch.float32)`：用 `torch.float32`，语义与 `ct.float32` 相同。

[silu_and_mul.py:51](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L51) —— `result = ct.astype(result, output.dtype)`：算完降回输出类型再 scatter。

> 顺带一提：`silu_and_mul` 里 `ct.truediv(..., flush_to_zero=True, rounding_mode=RMd.APPROX)` 是**逐元素计算**阶段的近似开关（属于 u4-l1 的内容），不属于数据搬运，这里不展开，只需知道它发生在「升精度之后、降精度之前」。

#### 4.4.4 代码实践

**实践目标**：体会「升精度计算」对结果的影响。

1. 打开 `tests/ops/test_softmax.py`，看 [test_softmax.py:64-67](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L64-L67) 的容差设定：fp16 用 `rtol=1e-3, atol=1e-5`，fp32 用 `rtol=1e-5, atol=1e-7`。
2. 跑一组 fp16 用例：

```bash
pytest "tests/ops/test_softmax.py::Test_Softmax::test_op" -k "baseline and float16"
```

**需要观察的现象**：即便输入是 fp16，内核内部也是先 `astype` 到 fp32 再算，所以误差被压在 fp16 容差（`1e-3/1e-5`）以内，而不是因低精度 `exp` 而发散。

**预期结果**：fp16 用例通过，误差在容差内。**待本地验证**。

#### 4.4.5 小练习与答案

- **练习 1**：为什么是「加载后升、写回前降」，而不是反过来？
  - **答**：升精度是为了让**中间计算**（exp、求和、相除）有足够有效位，避免误差放大或溢出；所以必须在计算前升。写回前降是为了让**存储**回到紧凑的低精度、并与输入/输出类型对齐；所以必须在计算后、写回前降。
- **练习 2**：写回前为什么用 `input.dtype` / `output.dtype`，而不是写死 `ct.float32`？
  - **答**：因为输入可能是 fp16/bf16/fp32，输出要与输入同类型；写死 fp32 会导致输出类型与输入不一致，破坏算子契约。用 `input.dtype` 让内核对多种精度自适应。

---

## 5. 综合实践

把本讲的四块知识串起来，完成下面这个「两版加载对照」的源码阅读 + 实测任务。

**任务**：对照 softmax 的 **gather/scatter 版**（basic 内核）与 **TMA / load/store 版**（tma 内核），写一段说明，讲清它们各自适用的**列宽**与**硬件**场景。

建议步骤：

1. **定位两份内核**：
   - gather/scatter 版：[`_softmax_kernel`，softmax.py:18-50](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L18-L50)。
   - load/store 版：[`_softmax_kernel_tma`，softmax.py:80-114](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L80-L114)。

2. **逐项填表**（在你的笔记里完成）：

   | 对照项 | gather/scatter 版 | load/store（TMA）版 |
   | --- | --- | --- |
   | 描述数据的方式 | 索引数组 `(row_idx, offsets)` | 锚点 + 形状 `index=(row_idx,0), shape=(1,TILE_SIZE)` |
   | 返回瓦片维度 | 一维 `(TILE_SIZE,)` | 二维 `(1, TILE_SIZE)` |
   | 归约轴 | `0` | `1` |
   | 边界/填充参数 | `check_bounds` + `padding_value`（标量） | `padding_mode`（枚举） |
   | 对列宽的要求 | 任意；列宽很大时配 chunked 多块 | 一片瓦片装下整行（`TILE_SIZE >= n_cols`） |
   | 硬件亲和性 | 通用 LDG 路径，全架构可用 | 矩形 TMA，sm≥9 才有硬件加速，更低架构被仿真/回退 |

3. **实测两条路径**（需 sm≥9 的卡才能真正走 TMA）：

```bash
# basic 路径
python -c "
import torch, tilegym
x = torch.rand(256, 2048, device='cuda', dtype=torch.float32)
ref = torch.nn.functional.softmax(x, dim=-1)
y = tilegym.ops.softmax(x)                       # 默认 basic（gather/scatter）
print('basic  max_abs_err', (y-ref).abs().max().item())
yt = tilegym.ops.softmax(x, use_tma=True)        # TMA（load/store）
print('tma    max_abs_err', (yt-ref).abs().max().item())
"
```

4. **要写出的说明**应至少覆盖：①gather 版因返回一维、归约轴为 0，load 版返回二维、归约轴为 1，二者只是同一算法的两种瓦片布局；②gather 版「索引可任意拼」适合列宽不规则或需取多段（如 silu_and_mul），load 版矩形规整、贴合 TMA、适合「一片装一行」的适中列宽；③load/TMA 真正发挥威力需 sm≥9（见 [softmax.py:323-332](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L323-L332) 的回退保护），低架构应选 gather 版。

**预期结果**：两条路径的最大绝对误差都应在 fp32 容差（`1e-5/1e-7` 量级）内一致。若在 sm<9 的卡上跑，`use_tma=True` 会被自动回退并打印告警，此时 `yt` 实际走的是 basic 路径。**待本地验证**（取决于卡型）。

## 6. 本讲小结

- cuTile 有两套数据搬运原语：`load`/`store` 用「锚点 + 矩形形状」，返回多维瓦片，贴合 TMA；`gather`/`scatter` 用「索引数组」，按下标广播取/放，返回形状等于广播结果的瓦片，更灵活、走通用 LDG 路径。
- 同一个 softmax，basic 版（gather，返回一维）归约轴是 `0`，TMA 版（load，返回二维 `(1,TILE_SIZE)`）归约轴是 `1`——换原语就要换轴，这是最常见的坑。
- `ct.arange(N)` 生成 `[0..N-1]` 列下标基底，`N` 须为编译期常量（`TILE_SIZE`）；chunked softmax 用「`arange` 基底 + 每块偏移」拼出不同列段，`silu_and_mul` 用「`offsets` + 常量」取前后两段。
- 边界与填充分两套：gather/scatter 用 `check_bounds`（可为编译期常量 `TILE_SIZE != N`）+ `padding_value`（标量）；load 用 `padding_mode`（枚举 `NEG_INF`/`ZERO`）。softmax 选 `-inf` 是为了让填充元素在 `exp` 下贡献为 0。
- `ct.astype` 固定出现在「加载后升 fp32」与「写回前降回 `input.dtype`」两处，兼顾计算精度与存储紧凑；`ct.float32` 与 `torch.float32` 等价通用。

## 7. 下一步学习建议

- 数据搬进来之后，下一步自然是「怎么把它启动起来」。u3-l3（内核启动模式：`ct.launch` 与 grid 计算）会讲主机侧 `_launch_softmax_*` 函数如何算 grid、取 `NUM_SM`、并调用 `ct.launch(stream, grid, kernel, args)`。
- 想看四种 softmax 变体的完整算法（basic / TMA / chunked 三遍 / multi-wave）如何根据 `use_tma`/`use_chunked`/`use_multi_wave` 选择，并如何用 `torch.autograd.Function` 封装，请读 u3-l4（softmax 内核全解）。
- 想看 gather/scatter + arange 在「逐元素 + autograd」场景下的更完整应用（含反向重计算），可预习 u4-l1（逐元素内核 silu_and_mul）与 u4-l2（autograd 集成与反向内核）。
