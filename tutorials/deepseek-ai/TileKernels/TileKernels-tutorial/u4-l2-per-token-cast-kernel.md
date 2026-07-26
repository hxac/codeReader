# per_token_cast kernel：absmax + 两段规约 + 向量化

## 1. 本讲目标

本讲是量化模块（第 4 单元）从「地基」走向「真实 kernel 内部」的第一篇。在 [u4-l1](u4-l1-quant-config-basics.md) 里我们只建立了「低比特格式 + scaling factor（SF）+ 配置体系」的心智模型，本讲带你真正走进最常用的行级量化 kernel——`per_token_cast`，看它如何在一个 GPU thread block 内完成「读入 → 算定标因子 → 缩放 → 写出」。

读完本讲，你应该能够：

1. 画出 `per_token_cast_kernel` 的完整数据流图（load → reduce_absmax → get_sf_and_inv → scale → store）。
2. 说清楚为什么当输入**自带 SF**（`with_sf=True`）时必须用**两段规约**（absmax → 乘 SF → max），而输入是高精度（`with_sf=False`）时只需要**单段 absmax**。
3. 掌握 `block_m / block_k / num_per_channels / num_groups` 这套分块参数的推导逻辑——它们如何由 `num_threads`、`num_per_channels`、`hidden` 共同决定。

本讲只覆盖两个最小模块：**per_token_cast_kernel** 与 **quant/common**（聚焦其中本 kernel 真正调用的部分，如 `get_sf_and_inv`、`load_sf/store_sf/transform_sf`、`get_best_vectorize_size`）。SF 宏的位运算细节（`round_sf`、UE8M0）已在 u4-l1 概览，完整的位操作精读留到 [u4-l3](u4-l3-sf-macros-and-rounding.md)。

## 2. 前置知识

本讲默认你已经读过以下内容，不会重复讲解：

- **u2-l1 / u2-l2 / u2-l3**：TileLang 的 `@tilelang.jit` + `@T.prim_func` 骨架、`alloc_fragment/alloc_shared` 三级存储、`T.copy`、`T.Parallel` 循环、`T.reduce_absmax/reduce_max` 与 `T.reshape`。如果对「编译期 vs 运行时」「fragment 是协作布局寄存器」没有印象，请先回看。
- **u4-l1**：低比特格式 e4m3/e2m1/e5m6、`QuantTensor=(tensor, sf)` 约定、`BaseCastConfig/CastInputConfig/CastOutputConfig` 三个 frozen dataclass、`sf = amax / max_value`、`sf_inv` 互为倒数、`clamp_min_value` 防下溢、`get_sf_shape` 的三种 SF 布局。

为方便承接，这里复述两个本讲会反复用到的关键事实：

- **per-token（行级）量化**：每行（每个 token）在 hidden 维度上按 `num_per_channels` 个元素切分成若干 SF 块，每块算一个 absmax，得到一个 SF。输出 SF 的块形状是 `(1, num_per_channels)`——行方向粒度为 1，列方向粒度为 `num_per_channels`。
- **absmax 定标**：`amax = max(|x|)`，`sf = amax / max_value`（`max_value` 是目标格式能表示的最大正数，如 e4m3 是 448），把数据压进 `[-max_value, max_value]`；反量化时乘回 `sf`。

### 一个贯穿全讲的直觉：为什么要「两段」

假设你有一批**已经量化过的 FP8 数据**，它自带一个较细的输入 SF（比如 `(128,128)` 块）。现在你想把它**重新量化**成另一种粒度（比如 `(1,32)`）。每个元素的真实值是：

\[
\text{real}[i,j] = x[i,j] \times \text{sf\_in}[\text{块}(i,j)]
\]

新粒度下的定标因子需要的是 `max(|real[i,j]|)`。如果你对每个元素都先乘 `sf_in` 再比大小，就是 `block_k` 次乘法 + `num_per_channels` 路 max。

但有个可利用的结构：**向量化加载（vectorize）的一组连续元素一定落在同一个输入 SF 块里**（代码里用断言保证了这一点）。于是同一组里 `sf_in` 是常数，可以提到 absmax 外面：

\[
\max_{k\in\text{组}} |x[i,k] \cdot \text{sf\_in}| = \text{sf\_in} \cdot \max_{k\in\text{组}} |x[i,k]|
\]

这就把工作分成了两段：先在每组内做一次**便宜的 absmax**（fp16，元素少），再给每组的结果**乘上它自己的 sf_in**，最后跨组做一次 **fp32 的 max**。这正是 `with_sf=True` 分支的核心。先记住这个直觉，下面的源码就是在精确实现它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/quant/per_token_cast_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py) | 本讲主角。定义 `get_per_token_cast_kernel`（TileLang kernel 构造器）与三个 wrapper：`per_token_cast`、`per_token_cast_with_sf_only`、`per_token_cast_with_precomputed_sf`。 |
| [tile_kernels/quant/common.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py) | 量化模块的公共基础设施：配置 dataclass、`get_best_vectorize_size`、SF 宏（`get_sf_and_inv` / `load_sf` / `store_sf` / `transform_sf`）、`get_sf_shape`、`alloc_scaling_factors`、`cast_epilogue`。 |
| [tile_kernels/utils.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/utils.py) | `align` / `ceil_div` / `is_power_of_two` 三个无副作用小工具，服务于分块与对齐。 |
| [tests/quant/test_per_token_cast.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_per_token_cast.py) | 正确性与 benchmark 测试，用 `tile_kernels.torch.cast` 作参考对拍。 |
| [tile_kernels/torch/cast.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/cast.py) | 纯 PyTorch 参考实现，本讲的「标准答案」。 |

调用入口（用户侧）：`tile_kernels.quant.per_token_cast(x, fmt, num_per_channels, ...)`，它最终触发 `get_per_token_cast_kernel` 的 JIT 编译并启动。

---

## 4. 核心概念与源码讲解

### 4.1 common.py 中的量化基础设施（本讲用到的那部分）

#### 4.1.1 概念说明

`per_token_cast_kernel` 内部调用了 `common.py` 里的四个「SF 宏」和一个向量化工具函数。它们是 kernel 与配置体系之间的桥梁，理解它们的作用是读懂 kernel 的前提：

- `get_best_vectorize_size(dtype)`：根据当前 GPU 的 SM major（SM<10 还是 ≥10）和 dtype 字节数，返回一次内存事务能搬运多少个元素（16 或 32 除以字节数）。
- `get_sf_and_inv(amax, out_config)`：给定一个 amax，算出 `(sf, sf_inv)`。这是定标的「出口」。
- `load_sf / store_sf`：按配置的 SF 布局（row-major / col-major / packed UE8M0）读写一个 SF 元素。
- `transform_sf`：把读出来的 SF（可能是 UE8M0 字节）还原成 float32，便于 kernel 内部参与运算。

#### 4.1.2 核心流程

向量化大小的决策逻辑：

```
get_best_vectorize_size(dtype):
    ver = 当前 GPU 的 compute capability（如 "9.0"）
    major = ver 的主版本号
    base = (16 if major < 10 else 32)        # SM90→16, SM100→32
    return base // dtype.bytes               # FP8(1B)→16或32, FP4打包int8(1B)→16或32
```

`get_sf_and_inv` 的定标流程（本讲只看数学，位运算在 u4-l3）：

```
clamped_amax = max(amax, clamp_min_value)   # 防下溢
sf = clamped_amax / max_value                # 把数据压进 [-max_value, max_value]
if not round_sf:
    return sf, max_value / clamped_amax       # sf_inv = 1/sf
else:
    # 把 sf 舍入到最近的 2 的幂（位操作，u4-l3 详解）
    ...
```

#### 4.1.3 源码精读

**向量化大小探测**——按 SM major 选 16 或 32，再除以 dtype 字节数：

[common.py:L13-L17](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L13-L17) 读取目标 GPU 的 compute version，按 major 决定基础宽度。这决定了后续 fragment 的布局粒度。

**定标出口 `get_sf_and_inv`**——clamp 防下溢后算 sf 与 sf_inv：

[common.py:L196-L216](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L196-L216) 里 `clamp_min_value` 来自 `CastOutputConfig`：

[common.py:L50-L59](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L50-L59) e4m3 的下限是 `1e-4`，e2m1 的下限是 `max_value * 2**-126`。`clamp` 保证 amax 不会小到让 sf 下溢成非规格数。

> 注意：`get_sf_and_inv` 是 `@T.macro`（[common.py:L196](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L196)），意味着它**内联**进调用点的 kernel 代码，而不是一次函数调用——这样 `out_config` 这些编译期常量能在内联后被折叠优化。

**SF 读写的三种布局分发**——`load_sf` / `store_sf` / `transform_sf` 都用同样的三分支结构：

[common.py:L219-L226](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L219-L226) 是 `load_sf`：packed UE8M0 把 4 个 SF 打包成一个 int32（下标换算成 `[k//4, m*4 + k%4]`）；col-major 直接转置存取；row-major 是普通二维索引。`store_sf`（[common.py:L237-L244](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L237-L244)）和 `transform_sf`（[common.py:L229-L234](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L229-L234)）结构相同。这套宏让 kernel 主体无需关心 SF 的物理布局，只管逻辑下标 `(m_idx, k_idx)`。

#### 4.1.4 代码实践

**实践目标**：确认你对 `get_sf_and_inv` 数学部分的理解，并看清 `clamp_min_value` 的作用。

**操作步骤**：

1. 打开 [common.py:L196-L216](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L196-L216)，忽略 `round_sf` 分支，只读前两行 `clamped_amax = T.max(amax, clamp_min_value)` 与 `sf = clamped_amax / max_value`。
2. 手算一个例子：输入是 e4m3（`max_value=448`），某块 `amax=2.0`，则 `sf = 2.0/448 ≈ 0.00446`，`sf_inv = 448/2.0 = 224`。验证 `sf * sf_inv == 1`（在精度范围内）。
3. 再算一个极端例子：`amax=0`（全零块），由于 `clamp_min_value=1e-4`，`clamped_amax=1e-4`，于是 `sf=1e-4/448`，避免了除零与下溢。

**需要观察的现象 / 预期结果**：手算的 `sf` 与 `sf_inv` 应严格互为倒数；`clamp` 让全零块也能产生合法的、非零的 sf。若想用代码验证，可用纯 Python（`struct` 打包 float32）复现，但位运算部分（`round_sf`）留到 u4-l3，本讲不展开。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_sf_and_inv` 要先 `clamp` 再除，而不是先除再 clamp？
**答案**：因为 `sf = amax / max_value`，若 `amax=0` 会得到 `sf=0`，进而 `sf_inv = 1/0` 是 inf。clamp 在除法之前抬高了分子，保证 sf 与 sf_inv 都是有限的正常浮点数。

**练习 2**：`load_sf` 里 packed UE8M0 分支的下标为什么是 `(k_idx // 4, m_idx * 4 + k_idx % 4)`？
**答案**：UE8M0 把 4 个连续 k 方向的 SF 压进一个 int32 的 4 个字节，所以 k 方向要除 4 选 int32、再不关心字节内偏移（整体读写）；而 4 个 token 的 SF 被并排放在内维，故 m 方向乘 4。详见 u4-l1 的 `get_sf_shape` 与 u4-l3。

---

### 4.2 分块与线程划分：block_m / block_k / num_per_channels / num_groups

#### 4.2.1 概念说明

GPU kernel 不能一次处理整张矩阵，必须把 `(num_tokens, hidden)` 切成一块块 tile，每个 thread block 处理一个 `(block_m, block_k)` 的 tile。`per_token_cast_kernel` 的分块有三个约束：

1. **线程预算**：每个 block 用 `num_threads=128` 个线程，每线程固定处理 32 个元素，所以一个 tile 最多 `128*32=4096` 个元素。
2. **SF 对齐**：tile 的列宽 `block_k` 必须是输出 SF 块宽 `num_per_channels` 的整数倍，否则一个 SF 块会跨越 tile 边界，跨 block 归约代价大。
3. **向量化对齐**：tile 内连续元素的分组宽度 `num_vectorize` 还要能整除 `block_k`，保证每次内存事务都对齐。

四个参数的含义：

- `num_per_channels`：输出 SF 块的列宽（来自 `out_config.sf_block[1]`），即「多少个相邻元素共享一个 SF」。这是用户通过 `num_per_channels` 传入的。
- `block_k`：tile 的列宽。
- `block_m`：tile 的行高。
- `num_groups`：tile 每行里有几个 SF 块，`num_groups = block_k // num_per_channels`。

#### 4.2.2 核心流程

```
num_threads        = 128
num_elems_per_thread = 32
num_elems_per_block  = num_threads * num_elems_per_thread   # 4096
num_per_channels     = out_config.sf_block[1]               # 用户给的，如 32/64/128

if hidden == num_per_channels:           # 退化：整行一个 SF（per-channel）
    block_k          = align(hidden, num_threads * (2 if FP4 else 1))
    num_per_channels = block_k           # 此时 num_groups 必为 1
else:
    block_k = max(num_per_channels, gcd(num_elems_per_block, hidden))

assert block_k % num_per_channels == 0
block_m    = 1 if (4096 % block_k != 0) else 4096 // block_k
num_groups = block_k // num_per_channels
```

直觉上：`block_k` 取「SF 块宽」与「4096 和 hidden 的最大公因数」中的较大者——前者保证 SF 对齐，后者尽量让 tile 在 hidden 方向整除、减少边界碎片。`block_m` 再用剩余预算（`4096 // block_k`）决定能塞几行。

#### 4.2.3 源码精读

**线程与元素预算**——固定的 128 线程 × 32 元素：

[per_token_cast_kernel.py:L27-L30](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L27-L30) 确立了「每 block 4096 元素」的硬预算，`num_per_channels` 从输出配置取出。

**block_k / block_m / num_groups 的推导**：

[per_token_cast_kernel.py:L32-L42](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L32-L42) 包含两个分支：

- `hidden == num_per_channels`（[L32-L36](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L32-L36)）：退化成 per-channel（整行一个 SF），并把 `num_per_channels` 重置为对齐后的 `block_k`。FP4 时要按 2 对齐，因为两个 FP4 元素打包进一个 int8。
- 一般分支（[L37-L38](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L37-L38)）：`block_k = max(num_per_channels, gcd(4096, hidden))`。

[L40-L42](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L40-L42) 三条断言/计算：保证 `block_k` 整除 `num_per_channels`；算 `block_m`（不能整除时退化成 1 行）；算 `num_groups`。

**向量化宽度与 with_sf 的一组对齐断言**：

[per_token_cast_kernel.py:L44-L52](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L44-L52) 里 `num_vectorize = min(get_best_vectorize_size(dtype), gcd(block_m*block_k/num_threads, 32))`。后半段 `with_sf` 的断言非常关键，它就是 4.4 节两段规约能成立的前提：`num_per_channels >= num_vectorize` 保证了「一个向量化组不会跨 SF 块」，`block_{m,k}` 与 `sf_block` 的整除关系保证了 SF 块在 tile 内规则排布。

**网格定义**——每个 thread block 处理一个 `(block_m, block_k)` tile：

[per_token_cast_kernel.py:L72](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L72) 用 `T.ceildiv(num_tokens, block_m)` × `T.ceildiv(hidden, block_k)` 的二维网格覆盖整张矩阵，`threads=num_threads=128`。

#### 4.2.4 代码实践

**实践目标**：手算一组真实分块参数，验证它们满足所有约束。

**操作步骤**：

1. 取 `hidden=4096`、`num_per_channels=128`、`dtype=float8_e4m3fn`（FP8）。
2. 走一般分支：`gcd(4096, 4096)=4096`，`block_k = max(128, 4096) = 4096`。
3. `block_m = 4096 // 4096 = 1`，`num_groups = 4096 // 128 = 32`。
4. 再取 `hidden=4096`、`num_per_channels=32`：`block_k = max(32, 4096) = 4096`，`block_m=1`，`num_groups=128`。
5. 取一个不整除的例子：`hidden=2048`、`num_per_channels=128`，`block_k=max(128, gcd(4096,2048)=2048)=2048`，`block_m=4096//2048=2`，`num_groups=2048//128=16`。

**需要观察的现象 / 预期结果**：每种组合下，`block_k % num_per_channels == 0` 与 `block_m * block_k == 4096`（除非退化到 `block_m=1`）都成立；`num_groups` 恰好等于「tile 每行的 SF 块数」。若某组参数让 `block_m*block_k != 4096` 且 `block_m != 1`，说明你算错了。

**待本地验证**：以上是纯算术推导，可在 Python 里 `import math` 后用 `math.gcd` 复算确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `hidden == num_per_channels` 时要 `assert not in_config.with_sf`？
**答案**：per-channel 退化分支假设输入是高精度（bf16/fp32）且整行共享一个 SF；若输入自带 SF 又走 per-channel，块划分逻辑（`num_per_channels` 被重置为 `block_k`）与 with_sf 的两段规约假设冲突，故直接禁止。

**练习 2**：`block_m = 1` 会在什么情况下出现，有什么代价？
**答案**：当 `4096 % block_k != 0`（即 `block_k` 不整除 4096）时退化为 `block_m=1`，一个 thread block 只处理 1 行。代价是每行的 tile 数变多、grid 变大、线程并行度可能下降（但 `block_k` 较大时每线程仍处理 32 元素，只是 M 维并行性变弱）。

---

### 4.3 数据流总览与单段规约路径（with_sf=False）

#### 4.3.1 概念说明

有了分块，现在看 kernel 主体做什么。无论哪条路径，每个 thread block 都执行同一个五段骨架：

```
load      : x[pid_token*block_m, pid_hidden*block_k]  →  x_fragment (寄存器)
reduce    : 在 x_fragment 上算定标所需的 amax           →  sf_inv_fragment
get_sf    : amax → (sf, sf_inv)                        →  写出 out_sf
scale     : x_fragment * sf_inv                        →  out_shared (共享内存)
store     : out_shared  →  out[...] (全局)
```

当输入是高精度（`bf16/fp32`，`with_sf=False`）时，元素的真实值就是它本身，amax 可以一次性算完——这就是**单段规约路径**。它还有一个子分支 `cast_only`：当外部已经预算好了 SF（`per_token_cast_with_precomputed_sf`），连 absmax 都不用算，直接读 SF 做 scale。

#### 4.3.2 核心流程

**单段路径（with_sf=False，非 cast_only）**：

```
1. T.copy 输入到 x_fragment
2. reshape x_fragment → [block_m, num_groups, num_per_channels]
3. reduce_absmax(dim=2) → amax_fragment [block_m, num_groups]   # 每个SF块的absmax
4. 对每个 (i,j): amax → get_sf_and_inv → (sf, sf_inv)
   - store_sf(out_sf, sf, ...)    # 写出 SF
   - sf_inv_fragment[i,j] = sf_inv
5. out_shared[i,j] = x_fragment[i,j] * sf_inv_fragment[i, j // num_per_channels]
6. T.copy out_shared → out
```

`cast_only` 子路径跳过 2-4 的 absmax，直接 `sf_inv = 1 / load_sf(out_sf)`。

#### 4.3.3 源码精读

**五段骨架的存储分配**——fragment 装输入/规约结果，shared 装输出：

[per_token_cast_kernel.py:L73-L82](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L73-L82) 分配三个 buffer：`x_fragment`（输入）、`sf_inv_fragment`（每块一个 sf_inv，形状 `(block_m, num_groups)`）、`out_shared`（输出中转）。`T.annotate_layout` 用 `x_layout_fn` 给 fragment 指定线程-元素映射，让每个线程拿到连续 `num_vectorize` 个元素以支持合并读。

**输入搬运**——`disable_tma=True` 走向量化 load：

[per_token_cast_kernel.py:L85](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L85) 用 `T.copy(..., disable_tma=True)` 把全局内存搬进 fragment（u2-l2 讲过：目标是 fragment 时关 TMA、走向量化 load-store）。

**单段 absmax + 定标 + 写出**（else 分支的非 cast_only 部分）：

[per_token_cast_kernel.py:L133-L146](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L133-L146) 是单段路径的核心：

- [L135](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L135) 把 fragment reshape 成 `[block_m, num_groups, num_per_channels]`——把「同一 SF 块的元素」聚到最后一维。
- [L137](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L137) `T.reduce_absmax(..., dim=2)` 在最后一维上做 absmax，压成 `(block_m, num_groups)`——每个 SF 块一个 amax。
- [L138-L146](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L138-L146) 对每个块调 `get_sf_and_inv` 得到 `(sf, sf_inv)`，`store_sf` 写出 SF，并把 `sf_inv` 存进 `sf_inv_fragment` 供 scale 用。

**cast_only 子分支**——预算 SF 时只做 scale：

[per_token_cast_kernel.py:L129-L132](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L129-L132) 直接 `load_sf` 读外部预算的 SF，`sf_inv = 1/sf`，跳过整个 absmax。

**scale + 写出**（两条路径共用）：

[per_token_cast_kernel.py:L148-L154](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L148-L154) 用 `sf_inv_fragment[i, j // num_per_channels]` 把每块的 sf_inv 广播到块内每个元素（`j // num_per_channels` 把元素列号映射回 SF 块号），乘到 `x_fragment` 写进 `out_shared`，最后 `T.copy` 到全局。`sf_only` 模式下跳过这段（只算 SF 不算 cast 值）。

#### 4.3.4 代码实践

**实践目标**：用 PyTorch 参考对拍单段路径，理解「reshape → reduce_absmax(dim=2)」等价于按块取 absmax。

**操作步骤**：

1. 读参考实现 [torch/cast.py:L138-L164](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/cast.py#L138-L164)：它把 padded 源 reshape 成 `(Hb, bh, Wb, bw)` 后 permute 成 `(Hb, Wb, bh*bw)`，对最后一维取 `abs().max(dim=-1)`。这与 kernel 的 `[block_m, num_groups, num_per_channels]` + `reduce_absmax(dim=2)` 在数学上完全一致。
2. 用一段最小 PyTorch 代码复现单段路径（**示例代码，非项目原有**）：

   ```python
   import torch
   x = torch.randn(2, 128, device='cuda', dtype=torch.bfloat16)  # block_m=2, hidden=128
   num_per_channels = 32
   num_groups = 128 // num_per_channels                          # = 4
   xr = x.reshape(2, num_groups, num_per_channels)
   amax = xr.abs().max(dim=2).values                             # (2, 4)
   max_value = 448.0
   sf = torch.clamp(amax, min=1e-4).float() / max_value          # clamp 防下溢
   sf_inv = max_value / torch.clamp(amax, min=1e-4).float()
   out = x.float() * sf_inv.repeat_interleave(num_per_channels, dim=1)
   ```

3. 把这段的 `out`、`sf` 与 `tile_kernels.quant.per_token_cast(x, 'e4m3', 32)` 的输出用 `tile_kernels.testing.numeric.assert_equal` 对拍。

**需要观察的现象 / 预期结果**：cast 值应位精确相等（量化是确定性映射），SF 也应相等。`amax` 的形状 `(block_m, num_groups)` 与 kernel 里 `sf_inv_fragment` 一致。

**待本地验证**：若手头没有 SM90/SM100 GPU，第 3 步无法运行；可只做第 1-2 步的算术与形状验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 fragment reshape 成 `[block_m, num_groups, num_per_channels]` 而不是 `[block_m, num_per_channels, num_groups]`？
**答案**：要把「同一 SF 块的 `num_per_channels` 个元素」放到**连续的最后一维**，`reduce_absmax(dim=2)` 才能在正确的维度上塌缩。后者会把不同块的元素混到同一规约维度，得到错误的 amax。

**练习 2**：`cast_only` 分支里 `sf_inv = 1 / sf`，为什么不用 `get_sf_and_inv`？
**答案**：`cast_only` 时 SF 已经由外部预算好（可能还做过 `round_sf`），kernel 只需反量化系数 `sf_inv`；`get_sf_and_inv` 会重新算 sf 并可能再次 round，会破坏已确定的 SF。

---

### 4.4 两段规约路径（with_sf=True）：absmax → 乘 SF → max

#### 4.4.1 概念说明

这是本讲最核心、也最容易卡住的部分。当输入**本身已经是量化数据**（FP8/FP4）且自带一个较细的输入 SF 时（`with_sf=True`），元素的真实值 = `x * sf_in`。要算新粒度下的 amax，必须对「真实值」取 absmax，而不是对原始 `x`。

朴素做法是先反量化所有元素再 absmax，代价是 `block_k` 次乘法。但如 4.1 节直觉所述，向量化组内的 `sf_in` 是常数，可以提出来。于是 kernel 把规约拆成两段：

- **第一段（stage1，fp16）**：在每个向量化组（`num_vectorize` 个元素）内做 absmax，得到「组内原始 absmax」。因为组内 sf_in 常数，组内 absmax 等价于对真实值取 absmax 后再除以 sf_in——只需最后统一乘回。
- **乘 SF**：把每组 absmax 乘上它所在输入 SF 块的 sf_in，得到「组内真实 absmax」。
- **第二段（stage2，fp32）**：跨组（在同一输出 SF 块内的若干组）取 max，得到「输出块的真实 absmax」。

收益：乘法次数从 `block_k` 降到 `block_k / num_vectorize`；fp16 的组内规约便宜，fp32 的跨组 max 元素少（每块只有 `num_per_channels / num_vectorize` 个）因而精度和性能都好。

#### 4.4.2 核心流程

```
A. 读输入 SF：load_sf + transform_sf → x_sf_fragment [num_sf_rows, num_sf_cols]
B. stage1: reshape x_fragment → [block_m, block_k//num_vectorize, num_vectorize]
           reduce_absmax(dim=-1) → stage1_amax [block_m, block_k//num_vectorize]   (fp16)
C. 乘 SF：stage2_amax[i,j] = fp32(stage1_amax[i,j]) * x_sf_fragment[i//bh, j*num_vectorize//bw]
D. stage2: reshape stage2_amax → [block_m, num_groups, (block_k//num_vectorize)//num_groups]
           reduce_max(dim=-1) → sf_inv_fragment [block_m, num_groups]              (fp32)
E. 对每块: get_sf_and_inv → store_sf(out_sf) ; 记录 sf_inv
F. scale + store: factor = x_sf[块] * sf_inv[块]; out_shared = x * factor
```

注意 F 步用了一个巧妙的合并：真实值反量化再量化是 `x * sf_in * sf_inv`，两个乘法合成一个 `factor = sf_in * sf_inv`。

#### 4.4.3 源码精读

**A. 读输入 SF**——按 in_config 布局逐元素加载并还原成 float32：

[per_token_cast_kernel.py:L87-L94](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L87-L94) 算出 tile 内的 SF 块数 `(num_sf_rows_per_block, num_sf_cols_per_block)`，`transform_sf(load_sf(...))` 把 UE8M0 或 float32 的 SF 统一成 float32 存进 `x_sf_fragment`。

**B. stage1——组内 absmax（fp16）**：

[per_token_cast_kernel.py:L96-L99](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L96-L99) 把 fragment reshape 成 `[block_m, block_k // num_vectorize, num_vectorize]`，在最后一维 `reduce_absmax` 得到 `(block_m, block_k // num_vectorize)` 的 fp16 部分结果。注释「use half for reduction」点明了用 fp16 是为了省寄存器与算力。

**C. 乘输入 SF——把原始 absmax 还原成真实 absmax**：

[per_token_cast_kernel.py:L101-L107](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L101-L107) 对每个组 `(i,j)`，找到它所属的输入 SF 块：行方向 `i // in_config.sf_block[0]`，列方向 `j * num_vectorize // in_config.sf_block[1]`（`j*num_vectorize` 把组号还原成元素列号），乘上 `x_sf_fragment` 对应项，结果升回 fp32 存进 `stage2_amax_fragment`。这一步正是「把常数 sf_in 提出来后乘回」的实现。

**D. stage2——跨组 max（fp32）**：

[per_token_cast_kernel.py:L109-L111](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L109-L111) 把 stage2 结果 reshape 成 `[block_m, num_groups, block_k // num_vectorize // num_groups]`，在最后一维 `reduce_max` 得到 `(block_m, num_groups)`——每个**输出 SF 块**的真实 amax，即 `sf_inv_fragment`。注释「using float for reduction」说明跨组 max 用 fp32 保精度。

**E. 定标并写出 SF**：

[per_token_cast_kernel.py:L113-L119](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L113-L119) 对每块调 `get_sf_and_inv` 得 `(sf, sf_inv)`，`store_sf` 写出 SF，并把 `sf_inv` 留在 fragment。

**F. 合并乘法 scale + 写出**：

[per_token_cast_kernel.py:L121-L126](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L121-L126) 注释「Apply two multiplication at once」点明：`factor = x_sf[输入块] * sf_inv[输出块]`，一次乘法同时完成「反量化（乘 sf_in）」和「再量化（乘 sf_inv）」。

#### 4.4.4 代码实践

**实践目标**：验证两段规约与「朴素逐元素反量化再 absmax」在数学上等价，并体会它省下的乘法。

**操作步骤**：

1. 构造一个最小例子（**示例代码，非项目原有**）：

   ```python
   import torch
   torch.manual_seed(0)
   block_m, block_k, nv, npc = 1, 128, 32, 128   # num_vectorize=32, num_per_channels=128
   num_groups = block_k // npc                    # = 1
   x = torch.randn(block_m, block_k, device='cuda')
   x_sf = torch.rand(block_m, 1, device='cuda') + 0.5   # 假设整行一个输入SF（简化）
   # 朴素：逐元素反量化再按输出块取absmax
   real = x * x_sf
   naive_amax = real.abs().reshape(block_m, num_groups, npc).amax(dim=2)  # (block_m, num_groups)
   # 两段：
   stage1 = x.reshape(block_m, block_k//nv, nv).abs().amax(dim=2)         # (block_m, block_k//nv)
   scaled = stage1 * x_sf                                                  # 乘 sf_in
   stage2 = scaled.reshape(block_m, num_groups, -1).amax(dim=2)            # (block_m, num_groups)
   print(torch.allclose(naive_amax, stage2))   # 预期 True
   ```

2. 数一数乘法：朴素路径有 `block_m*block_k = 128` 次逐元素乘法；两段路径只有 `block_m * (block_k//nv) = 4` 次（stage C）。
3. 读 [test_per_token_cast.py:L92-L109](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_per_token_cast.py#L92-L109)：测试用 `tile_kernels.torch.cast`（参考实现，走朴素路径）与 kernel（两段）对拍 `x_casted` 与 `x_sf`，正是用「数学等价的另一条路」来验证两段实现的正确性。

**需要观察的现象 / 预期结果**：`allclose` 为 True；乘法次数从 128 降到 4（本例 `nv=32`）。若改 `npc < nv` 会触发 [L48-L50](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L48-L50) 的断言失败——因为此时一个向量化组会跨输入 SF 块，「组内 sf_in 常数」的前提被打破。

**待本地验证**：CUDA torch 仍需 GPU；无 GPU 时可把 `device='cuda'` 改成 `'cpu'` 仅做逻辑验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 stage1 用 fp16、stage2 用 fp32？
**答案**：stage1 在组内取 absmax，元素多（`num_vectorize` 个）、数值范围由输入决定，用 fp16 省寄存器与算力且 absmax 对精度不敏感；stage2 元素少（每输出块 `num_per_channels/num_vectorize` 个）但要决定最终 SF，用 fp32 保精度，避免跨组 max 的舍入误差放大。

**练习 2**：若 `num_per_channels < num_vectorize`（如 SM100 上 FP8、`num_per_channels=16`）会怎样？
**答案**：会命中 [L48-L50](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L48-L50) 断言。因为向量化组宽于输出 SF 块，一个组跨多个输出块，两段规约的分组前提不成立；代码用断言而非静默处理，避免错误结果。

**练习 3**：F 步的 `factor = x_sf * sf_inv` 把两次乘法合成一次，为什么这在数学上正确？
**答案**：反量化是 `x * sf_in`（还原真实值），再量化是 `* sf_inv`（压回目标范围），两次乘法可结合：`x * sf_in * sf_inv = x * (sf_in * sf_inv)`。合并后每个元素只做一次乘法。

---

## 5. 综合实践

把本讲三块知识（分块、单段、两段）串起来，完成一个**数据流图 + 路径对比**的小任务。

**任务**：为 `per_token_cast_kernel` 画一张完整的数据流图，并在图上标出 `with_sf=False` 与 `with_sf=True` 两条路径的分叉点与汇合点。

**建议步骤**：

1. 以 [per_token_cast_kernel.py:L72-L154](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L72-L154) 为准，画出公共主干：
   - `T.Kernel` 网格 → `alloc_fragment/alloc_shared` → `T.annotate_layout` → `T.copy(x → x_fragment)`。
2. 在「`if in_config.with_sf:`」处画一个分叉：
   - **上路（with_sf=True）**：`load x_sf` → stage1 `reduce_absmax` → `乘 x_sf` → stage2 `reduce_max` → `get_sf_and_inv/store_sf`。
   - **下路（with_sf=False）**：若 `cast_only` 读预算 SF；否则 `reduce_absmax(dim=2)` → `get_sf_and_inv/store_sf`。
3. 在「`if not sf_only:`」处画汇合点：scale（上路用 `factor=x_sf*sf_inv`，下路用 `sf_inv`）→ `out_shared` → `T.copy → out`。
4. 在图旁用一句话写清两条路径的「amax 维度数」差异：上路先按 `num_vectorize` 塌缩再按输出块 max（两段），下路直接按 `num_per_channels` 塌缩（一段）。
5. （可选）运行测试验证你的理解：

   ```bash
   # 只读环境若无 GPU 则跳过
   pytest tests/quant/test_per_token_cast.py -k "e4m3" -n 4
   ```

**预期结果**：图能清晰体现「公共主干 + with_sf 分叉 + sf_only/cast_only 旁路 + scale 汇合」的结构；能解释两段 vs 一段的本质是「输入是否自带 SF、是否可利用组内 sf_in 常数」。若运行测试，FP8 输入（`in_dtype=float8_e4m3fn`）的用例走两段路径，bf16/fp32 输入走单段路径。

## 6. 本讲小结

- `per_token_cast_kernel` 的五段骨架是 **load → reduce(算 amax) → get_sf_and_inv(定标) → scale → store**，输入输出都走 `disable_tma=True` 的向量化路径。
- 分块由 `num_threads=128`、每线程 32 元素（共 4096）的硬预算驱动；`block_k` 取 `max(num_per_channels, gcd(4096, hidden))`，`block_m = 4096 // block_k`，`num_groups = block_k // num_per_channels`。
- **单段路径（with_sf=False）**：输入高精度，直接 reshape 成 `[block_m, num_groups, num_per_channels]` 后 `reduce_absmax(dim=2)` 一步得到每块 amax；`cast_only` 子分支连 absmax 都跳过。
- **两段路径（with_sf=True）**：输入已量化带 SF，先按向量化组做 fp16 absmax（组内 sf_in 常数可提出），乘回 sf_in 还原真实值，再跨组做 fp32 max。乘法次数从 `block_k` 降到 `block_k/num_vectorize`。
- 两条路径的 SF 写出与 scale 共用同一套宏（`store_sf`、`get_sf_and_inv`），其中 with_sf 路径把反量化与再量化合成一次 `factor = x_sf * sf_inv`。
- kernel 通过三个 wrapper 暴露三种模式：`per_token_cast`（默认算 SF+cast）、`per_token_cast_with_sf_only`（只算 SF）、`per_token_cast_with_precomputed_sf`（用预算 SF 只做 cast，即 `cast_only`）。

## 7. 下一步学习建议

- 想搞清 `get_sf_and_inv` 里 `round_sf` 把 SF 舍入到 2 的幂的位操作、以及 UE8M0 打包的细节，请继续 [u4-l3：SF 宏与幂次舍入](u4-l3-sf-macros-and-rounding.md)。
- 想对比「per-token（行级）」与「per-block（二维块）」两种 SF 粒度的差异、以及 lossless / e5m6 变体和反量化，请读 [u4-l4：per-block / lossless / E5M6 变体与 cast_back 反量化](u4-l4-block-lossless-e5m6-castback.md)。
- 想看量化如何与 SwiGLU / 转置**融合**以省一次全局存储读写，请读 [u4-l5：融合 SwiGLU + 量化 与 per-channel transpose](u4-l5-fused-swiglu-quant.md)。
- 建议同时打开 [tests/quant/test_per_token_cast.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_per_token_cast.py) 与 [torch/cast.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/cast.py)，用参考实现的朴素路径反推 kernel 两段路径的正确性，这是理解量化 kernel 最稳的练习方式。
