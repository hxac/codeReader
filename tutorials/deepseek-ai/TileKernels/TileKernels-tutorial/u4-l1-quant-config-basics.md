# 量化基础：低比特格式与配置体系

## 1. 本讲目标

本讲是「量化模块（u4）」的第一篇，只打地基，**不读任何具体 cast kernel 的内部循环**。读完本讲你应当能够：

1. 说清楚 LLM 训练/推理里为什么要做低比特量化，以及「低比特浮点 + scaling factor」这套组合拳的基本原理。
2. 对比三种目标格式 **e4m3 / e2m1 / e5m6** 的位宽、指数、尾数与可表示范围。
3. 解释 **scaling factor（SF）** 的作用，以及 TileKernels 用 `QuantTensor = (tensor, sf)` 这个二元组来表达「带缩放因子的量化张量」的约定。
4. 读懂 `BaseCastConfig / CastInputConfig / CastOutputConfig` 这套 `frozen dataclass` 配置体系，理解 `dtype` / `sf_dtype` 等派生属性是怎么算出来的。
5. 手算 `get_sf_shape` 在 **row-major / col-major / packed ue8m0** 三种 SF 布局下的输出形状。

本讲涉及的全部源码只有两个文件：`tile_kernels/quant/types.py`（3 行）和 `tile_kernels/quant/common.py`（295 行，但本讲只精读其中与「格式定义 + 配置 + SF 形状」相关的部分；规约与位操作细节留到 u4-l2、u4-l3）。

## 2. 前置知识

- **u1-l3 目录结构与包入口**：你知道 `tile_kernels` 包分四层，量化算子在 `tile_kernels/quant/`，用户调用入口是 wrapper 函数而非 TileLang kernel 对象。
- **u2-l1 TileLang 算子解剖**：你知道一个算子是「`@tilelang.jit` 构造器 + `@T.prim_func` 内核」两层结构，编译期参数被烤进产物，运行时维度用 `T.dynamic(...)` 声明。本讲里你会看到配置对象（`CastInputConfig` 等）正是作为**编译期参数**传进 kernel 构造器的。
- **一点浮点表示常识**：IEEE 浮点数由「符号位 + 指数位 + 尾数位」组成。本讲会用到「eXmY」这种简写：`e` 表示指数位数，`m` 表示尾数位数。例如 `e4m3` = 4 位指数 + 3 位尾数（外加 1 位符号），共 8 位，也就是常见的 FP8。
- **一点 GPU 访存常识**：LLM 里激活值和权重的搬运量巨大，低比特表示能直接成倍降低显存读写量，这是量化的根本动机。

> 本讲不要求读者已经写过任何 TileLang kernel，但建议先扫一眼 u2-l1 中「编译期 vs 运行时」的概念。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的关键符号 |
| --- | --- | --- |
| [tile_kernels/quant/types.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/types.py) | 整个 quant 子包共享的类型别名，只有 3 行 | `QuantTensor` |
| [tile_kernels/quant/common.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py) | quant 子包的公共工具：配置体系、SF 形状/布局、SF 宏、解包调试工具 | `BaseCastConfig` / `CastInputConfig` / `CastOutputConfig`、`get_cast_input_and_config` / `get_cast_output_config`、`get_logical_hidden` / `get_physical_hidden`、`get_sf_shape` / `alloc_scaling_factors` / `cast_epilogue`、`get_sf_and_inv` / `load_sf` / `store_sf` / `transform_sf` |

辅助参考（非本讲主角，但会用来佐证格式定义）：

| 文件 | 用途 |
| --- | --- |
| [tile_kernels/quant/per_token_cast_to_e5m6_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py) | 佐证 e5m6 的最大值（65024）与 12 位打包方式 |
| [tile_kernels/quant/per_token_cast_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py) | 佐证配置对象如何被 wrapper 用来算 SF 形状、分配输出 |

---

## 4. 核心概念与源码讲解

### 4.1 低比特浮点格式：e4m3 / e2m1 / e5m6

#### 4.1.1 概念说明

LLM 推理与训练中，**显存带宽**往往比算力更先成为瓶颈。把激活值从 BF16/FP32（16/32 位）压到 8 位甚至 4 位，可以立刻把读写量砍掉 2~4 倍，相当于免费提速。

但「直接截断」会丢掉大量精度。低比特浮点的思路是：**保留浮点的「指数 + 尾数」结构**，让数值仍能覆盖很大的动态范围，只是每种量级能用的格子变少了。TileKernels 支持三种目标格式：

- **e4m3（FP8）**：8 位，动态范围适中，适合激活值。对应 PyTorch 的 `torch.float8_e4m3fn`。
- **e2m1（FP4）**：4 位，极度省带宽，但每个数只能取少数几个离散值，必须配合更细粒度的 scaling factor。TileKernels 内部用 `T.float4_e2m1fn`，但**在 PyTorch 侧以 `torch.int8` 打包存储**（一个 int8 装两个 fp4）。
- **e5m6**：12 位（1 符号 + 5 指数 + 6 尾数），动态范围大、精度较高，是项目自定义的格式，PyTorch 侧以 `torch.uint32` 作容器存储打包后的 12 位值。

一个 eXmY 浮点数的值（正常数，即指数非零）按下式计算：

\[
\text{value} = (-1)^{s} \cdot 2^{(e - \text{bias})} \cdot \left(1 + \frac{m}{2^{M}}\right)
\]

其中 \(s\) 是符号位，\(e\) 是指数值，\(m\) 是尾数值，\(M\) 是尾数位数，bias 是指数偏置。最大有限值出现在指数最大、尾数全 1 时。

#### 4.1.2 核心流程

把一个高精度数 cast 到低比特，关键是确定**该用哪个量级**来表达它。这由两个量决定：

1. **max_value**：该格式能表示的最大有限值（e4m3≈448，e2m1=6，e5m6=65024）。
2. **scaling factor（SF）**：一个缩放系数，把数据的实际范围「拉」到格式能覆盖的范围里。

确定 SF 的标准做法是 **per-block absmax**：在一个小块内取绝对值最大者 `amax`，令

\[
\text{sf} = \frac{\text{amax}}{\text{max\_value}}, \qquad \text{sf\_inv} = \frac{1}{\text{sf}} = \frac{\text{max\_value}}{\text{amax}}
\]

随后把块内每个元素乘以 `sf_inv` 再 cast 到低比特；反量化时再把低比特值乘回 `sf` 即近似还原。

> 「块」的大小由配置里的 `sf_block = (block_m, block_k)` 决定。块越小（粒度越细），精度越高、但 SF 占用的额外存储也越多。这就是 u4 整个模块反复在「精度 ↔ 带宽」之间权衡的核心矛盾。

#### 4.1.3 源码精读

**格式名 → torch dtype 的映射** 在 `get_cast_output_config` 里：

[tile_kernels/quant/common.py:99-113](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L99-L113) —— 注意 `'e2m1'` 映射到 `torch.int8`（两个 fp4 打包进一个字节），`'e5m6'` 映射到 `torch.uint32`（12 位值的容器）：

```python
mapping = {
    'e5m6': torch.uint32,
    'e4m3': torch.float8_e4m3fn,
    'e2m1': torch.int8,
}
```

**「int8 即打包的 FP4」的转换** 体现在 `BaseCastConfig.dtype` 派生属性里：

[tile_kernels/quant/common.py:27-29](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L27-L29) —— 当 `torch_dtype` 是 `torch.int8` 时，TileLang 侧的真实元素类型是 `T.float4_e2m1fn`：

```python
@property
def dtype(self) -> T.dtype:
    return T.dtype(self.torch_dtype) if self.torch_dtype != torch.int8 else T.float4_e2m1fn
```

**每种格式的下限（clamp_min_value）** 在 `CastOutputConfig` 里按 dtype 分别给出，用来防止 `amax` 过小导致 SF 下溢：

[tile_kernels/quant/common.py:50-59](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L50-L59) —— e4m3 用固定 `1e-4`，e2m1 用 `T.max_value(dtype) * 2**-126`：

```python
elif self.dtype == T.float8_e4m3fn:
    return 1e-4
elif self.dtype == T.float4_e2m1fn:
    return T.max_value(self.dtype) * (2**-126)
```

**e5m6 的最大值** 不在 `common.py`，而在专门的 e5m6 kernel 里硬编码为 65024，正好等于 \( (2 - 2^{-6}) \cdot 2^{15} = 65024 \)（5 位指数、bias=15）：

[tile_kernels/quant/per_token_cast_to_e5m6_kernel.py:15](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py#L15) —— `max_value = 65024`。

而 e5m6 的 12 位特性体现在打包函数 `float_to_e5m6`：它把 **8 个 float32 值打包成 3 个 uint32**（\(8 \times 12 = 96 = 3 \times 32\) 位），见 [tile_kernels/quant/per_token_cast_to_e5m6_kernel.py:53-64](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py#L53-L64)。

#### 4.1.4 代码实践：列一张格式对比表

**实践目标**：把三种格式的关键参数整理成一张表，并用公式验证最大值。

**操作步骤**：

1. 阅读 [common.py:99-113](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L99-L113) 的 `mapping`、[common.py:50-59](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L50-L59) 的 `clamp_min_value`，以及 e5m6 kernel 的 `max_value = 65024`。
2. 套用最大值公式 \( \text{max} = (2 - 2^{-M}) \cdot 2^{(2^{E}-1-\text{bias})} \)（对正常数最大指数），手算三种格式的最大有限值。
3. 把结果填入下表。

**预期结果**（参考答案）：

| 格式 | 总位宽 | 符号 | 指数 E | 尾数 M | bias | 最大有限值 | PyTorch 容器 dtype | TileLang 元素类型 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| e4m3 (FP8) | 8 | 1 | 4 | 3 | 7 | 448 | `torch.float8_e4m3fn` | `T.float8_e4m3fn` |
| e2m1 (FP4) | 4 | 1 | 2 | 1 | 1 | 6 | `torch.int8`（装 2 个 fp4） | `T.float4_e2m1fn` |
| e5m6 | 12 | 1 | 5 | 6 | 15 | 65024 | `torch.uint32`（装打包 12 位值） | （自定义，按 12 位打包） |

**需要观察的现象**：注意三种格式在 PyTorch 侧的「容器」都不一样——e4m3 是原生 1 字节，e2m1 借用 int8 打包两个值，e5m6 借用 uint32 打包。这正是后面 `get_logical_hidden` / `get_physical_hidden` 要处理的「逻辑维度 ↔ 物理维度」换算问题的根源。

> 说明：上表中 e4m3 / e2m1 / e5m6 的位宽与最大值是基于公开的浮点格式定义与本仓库源码（`max_value = 65024`、`bias = 1`）推得；e2m1 的可表示离散值集合还可由 [common.py:247-295](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L247-L295) 的 `unpack_from_e2m1fn_x2` 解码逻辑交叉验证（正数集合为 \(\{0, 0.5, 1, 1.5, 2, 3, 4, 6\}\)）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 e2m1 在 PyTorch 侧用 `torch.int8` 而不是某个「fp4」dtype？
**答案**：PyTorch 没有原生的 4 位浮点 dtype。最小可寻址单元是 8 位，所以把两个 fp4 值打包进一个 int8；相应地 `BaseCastConfig.dtype` 在 `torch_dtype == torch.int8` 时返回 `T.float4_e2m1fn`，让 TileLang kernel 按 fp4 语义处理。

**练习 2**：用最大值公式验证 e5m6 的 max = 65024。
**答案**：\(E=5, M=6, \text{bias}=15\)，最大正常数 \(= (2 - 2^{-6}) \cdot 2^{15} = \frac{127}{64} \cdot 32768 = 127 \cdot 512 = 65024\)，与源码一致。

---

### 4.2 Scaling Factor 与 QuantTensor 约定

#### 4.2.1 概念说明

低比特格式的动态范围很窄（e2m1 最大才 6）。如果直接把一个范围在 \([-100, 100]\) 的激活值 cast 成 fp4，几乎所有数都会被截成 \(\pm 6\)，信息全丢。

**Scaling factor（SF）** 就是用来「挪动量级」的：先在一个小块内统计 `amax`，算出一个缩放系数，把数据缩放到低比特格式能良好覆盖的区间；存储时既存低比特值，也存这个 SF，反量化时再乘回来。这样每个块都用满低比特格式的表示范围，精度大幅提升。

为了在 Python 侧统一表达「一个量化后的张量 = 数据 + 它的 SF」，TileKernels 定义了一个类型别名 `QuantTensor`。

#### 4.2.2 核心流程

一次「per-block absmax 量化」的数据流：

```
对每个 SF 块:
    amax = 块内所有元素绝对值的最大值        # 统计
    amax = max(amax, clamp_min_value)       # 防下溢（clamp）
    sf     = amax / max_value               # 缩放系数
    sf_inv = max_value / amax               # 反缩放系数（= 1/sf）
    块内每个元素 x:  q = cast(x * sf_inv)    # 量化写出
存储: (q 张量, sf 张量)  -> 反量化时 x ≈ q * sf
```

`sf` 与 `sf_inv` 是一对倒数。注意 `sf` 还可以被进一步「圆整到 2 的幂」（`round_sf`），或被打包成 UE8M0 格式——这些是 u4-l3 的主题，本讲只承认「SF 本身是 fp32 或 packed ue8m0」两种存储形式。

#### 4.2.3 源码精读

**QuantTensor 类型别名** —— 整个 quant 子包共用的「(数据张量, SF 张量)」二元组约定：

[tile_kernels/quant/types.py:1-3](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/types.py#L1-L3)

```python
import torch
QuantTensor = tuple[torch.Tensor, torch.Tensor]
```

**SF 与 sf_inv 的核心计算** 在 `get_sf_and_inv` 这个 `@T.macro` 里（它会被各个 cast kernel 内联调用）。先看它「不圆整」的简单分支：

[tile_kernels/quant/common.py:196-205](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L196-L205) —— `clamp` 后 `sf = amax / max_value`，`sf_inv = max_value / amax`：

```python
clamped_amax = T.max(amax, out_config.clamp_min_value)
max_value = T.max_value(out_config.dtype)
sf = clamped_amax / max_value
if not out_config.round_sf:
    return sf, max_value / clamped_amax
```

这里 `out_config.clamp_min_value` 就是 4.1 里按 dtype 给出的下限，`T.max_value(out_config.dtype)` 是该格式的最大有限值。`round_sf=True` 时会走位操作把 sf 圆整到 2 的幂，那部分留给 u4-l3。

**实际使用**：per_token_cast wrapper 把配置算好后，由 kernel 在每个块上调用这个宏产出 `(sf, sf_inv)`，再用 `store_sf` 写出 sf、用 `sf_inv` 缩放并 cast 数据，可对照 [per_token_cast_kernel.py:138-146](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L138-L146)。

#### 4.2.4 代码实践：跟踪一次 SF 计算

**实践目标**：用纯 Python 复现「不圆整」分支的 `get_sf_and_inv`，理解 sf 与 sf_inv 的关系。

**操作步骤**：

1. 假设目标格式是 e4m3，则 `max_value = 448`，`clamp_min_value = 1e-4`。
2. 取一个块，其 `amax = 100.0`。
3. 手算 `sf` 与 `sf_inv`。
4. 若块内某元素 `x = 50.0`，写出量化值 `q = x * sf_inv`（先不 cast）。

**预期结果**：

- `clamped_amax = max(100.0, 1e-4) = 100.0`
- `sf = 100.0 / 448 ≈ 0.2232`
- `sf_inv = 448 / 100.0 = 4.48`
- `q = 50.0 * 4.48 = 224.0`（在 e4m3 的 \([-448, 448]\) 范围内，未饱和）

**需要观察的现象**：`sf` 越大说明这个块的原始数据范围越大；反量化时 `q * sf ≈ 224.0 * 0.2232 ≈ 50.0` 还原回原值（在未做低比特 cast 前是精确的，cast 后才有误差）。

> 待本地验证：若想真正跑通，需要安装 torch + tilelang + SM90/SM100 GPU；本练习用纸笔即可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么需要 `clamp_min_value`？没有它会怎样？
**答案**：若某块全零或极小，`amax ≈ 0`，则 `sf_inv = max_value / amax` 会趋于无穷，导致量化值溢出。clamp 把 amax 抬到一个最小值，保证 SF 不会爆炸。

**练习 2**：`QuantTensor` 是 `(tensor, sf)` 还是 `(sf, tensor)`？
**答案**：是 `(tensor, sf)`，即数据张量在前、SF 张量在后，见 `types.py` 的定义与各 wrapper 的返回值（如 [per_token_cast_kernel.py:220](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L220) 的 `return out, out_sf`）。

---

### 4.3 配置体系：BaseCastConfig / CastInputConfig / CastOutputConfig

#### 4.3.1 概念说明

量化一个张量涉及一连串决策：目标格式是什么？SF 块多大？SF 存成 fp32 还是 packed ue8m0？SF 用行主序还是列主序？输入是高精度张量，还是「已经量化过、自带 SF」的张量？

TileKernels 把所有这些决策收进一个 **`frozen`（不可变）dataclass**，叫 `BaseCastConfig`，再用两个子类区分「输入侧」与「输出侧」的额外字段：

- `CastInputConfig`：描述**输入**——输入是否已经带 SF（`with_sf`）。
- `CastOutputConfig`：描述**输出**——SF 是否圆整到 2 的幂（`round_sf`）、是否自定义 clamp 下限等。

把配置做成不可变对象有两个好处：一是它可以安全地作为**编译期参数**传进 `@tilelang.jit` 的 kernel 构造器（同一个配置特化出一份编译产物，见 u2-l1）；二是配置的派生属性（如 `dtype`、`sf_dtype`）可以做成 `@property`，逻辑集中在一处。

#### 4.3.2 核心流程

用户调 wrapper（如 `per_token_cast`）时，配置是怎么被构造出来的？

```
用户输入 (x, fmt, num_per_channels, 各种开关)
        │
        ├─ 若 x 是 QuantTensor (已量化) ──► get_cast_input_and_config(x, sf_block)
        │                                      → 返回 (x, x_sf, CastInputConfig(with_sf=True))
        │                                         并根据 x_sf 的 stride/dtype 推断布局标志
        │
        └─ 若 x 是普通高精度张量 ────────► get_cast_input_and_config(x, None)
                                              → 返回 (x, None, CastInputConfig(with_sf=False))

        │
        ▼
get_cast_output_config(fmt, sf_block, 各开关)  → 返回 CastOutputConfig(...)
        │
        ▼
把 in_config / out_config 作为编译期参数传进 kernel 构造器
```

注意 `get_cast_input_and_config` 不只是查类型，它还**根据输入 SF 张量的物理布局（stride、dtype）自动设置 `use_tma_aligned_col_major_sf` 与 `use_packed_ue8m0`**——也就是说，输入侧的 SF 布局是由「别人怎么存的」决定的，输出侧的 SF 布局则由用户显式选择。

#### 4.3.3 源码精读

**BaseCastConfig 基类** 与三个派生属性：

[tile_kernels/quant/common.py:20-38](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L20-L38) —— 4 个直接字段 + 3 个派生 property：

```python
@dataclass(frozen=True)
class BaseCastConfig:
    torch_dtype: torch.dtype = torch.float8_e4m3fn
    sf_block: tuple[int, int] = (1, 1)
    use_tma_aligned_col_major_sf: bool = False
    use_packed_ue8m0: bool = False

    @property
    def dtype(self) -> T.dtype: ...          # int8 → float4_e2m1fn 的转换
    @property
    def sf_torch_dtype(self) -> torch.dtype: # packed → uint8，否则 float32
        return torch.uint8 if self.use_packed_ue8m0 else torch.float32
    @property
    def sf_dtype(self) -> T.dtype: ...       # TileLang 侧的 SF 类型
```

关键点：

- `sf_block = (block_m, block_k)`：每个 SF 覆盖 `block_m × block_k` 个元素。`(1, 128)` 表示「每行每 128 列一个 SF」（per-token）。
- `dtype` 属性把 `torch.int8` 翻译成 `T.float4_e2m1fn`（见 4.1）。
- `sf_torch_dtype`：默认 SF 用 `torch.float32`；当 `use_packed_ue8m0=True` 时改用 `torch.uint8`（即 UE8M0 打包格式，只存指数）。

**CastInputConfig / CastOutputConfig** 的额外字段：

[tile_kernels/quant/common.py:40-42](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L40-L42) —— 输入侧只多一个 `with_sf`；

[tile_kernels/quant/common.py:45-59](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L45-L59) —— 输出侧多 `round_sf`、`custom_clamp_min_value` 以及派生的 `clamp_min_value`。

**从输入张量构造 CastInputConfig**：

[tile_kernels/quant/common.py:62-88](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L62-L88) —— 重点看它如何据 `x_sf.stride(0)`、`x_sf.dtype` 自动推断布局：

```python
if x_sf.stride(0) == 1:                       # 列连续 → col-major
    config = replace(config, use_tma_aligned_col_major_sf=True)
    x_sf = x_sf.T
    if x_sf.dtype == torch.int32:             # int32 容器 → UE8M0 打包
        config = replace(config, use_packed_ue8m0=True)
        x_sf = x_sf.view(torch.uint8)
else:
    assert x_sf.stride(1) == 1                # 否则必须是行连续
    assert x_sf.dtype == torch.float32
```

注意这里用 `dataclasses.replace` 在不可变对象上「改一个字段生成新对象」，这正是 `frozen=True` 的配套用法。

**从格式名构造 CastOutputConfig**：

[tile_kernels/quant/common.py:91-113](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L91-L113) —— 把字符串 `'e5m6'/'e4m3'/'e2m1'` 翻成 `torch_dtype`（即 4.1 的 mapping），再连同各开关一起构造 `CastOutputConfig`。

#### 4.3.4 代码实践：源码阅读型——推断输入配置

**实践目标**：不跑代码，纯靠读 `get_cast_input_and_config` 推断：给定不同形态的输入，会得到什么样的 `CastInputConfig`。

**操作步骤**：对下面三种输入，写出返回的 `with_sf`、`use_tma_aligned_col_major_sf`、`use_packed_ue8m0` 三个标志。

1. `x` 是 `torch.float32` 普通张量，`sf_block=None`。
2. `x` 是 `QuantTensor`，其 `x_sf` 形状 `(M, K)`、`dtype=float32`、`stride=(K,1)`（行连续）。
3. `x` 是 `QuantTensor`，其 `x_sf` 转置后 `stride(0)==1`、`dtype=torch.int32`。

**预期结果**（参考答案）：

| 场景 | with_sf | use_tma_aligned_col_major_sf | use_packed_ue8m0 |
| --- | --- | --- | --- |
| 1. 高精度普通张量 | `False` | `False` | `False` |
| 2. QuantTensor，行连续 float32 SF | `True` | `False` | `False` |
| 3. QuantTensor，列连续 int32 SF | `True` | `True` | `True` |

**需要观察的现象**：场景 3 里两个列主序/打包标志被**同时**置位——这印证了 4.4 会看到的「packed ue8m0 必须配 col-major」的约定。

> 待本地验证：若要实跑确认，可在装好依赖后 `from tile_kernels.quant.common import get_cast_input_and_config` 并构造上述张量打印 config；但纸面推导已足够。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `BaseCastConfig` 要用 `frozen=True`？
**答案**：它要作为编译期参数传给 `@tilelang.jit` kernel 构造器；不可变保证它可哈希、可安全缓存，且作为「特化 key」的一部分不会被意外修改。改字段时用 `dataclasses.replace` 生成新对象。

**练习 2**：`CastOutputConfig.round_sf=True` 会改变什么？
**答案**：它让 `get_sf_and_inv` 走位操作分支，把 sf 圆整到最近的 2 的幂（并用 UE8M0 等格式存储），代价是精度略降、收益是 SF 可用更少字节存储且反量化可用纯移位实现。细节在 u4-l3。

---

### 4.4 SF 形状与布局：get_sf_shape 的三种布局

#### 4.4.1 概念说明

SF 是「每个块一个标量」，所以 SF 张量的形状取决于：(a) 块的划分，(b) SF 在内存里怎么摆。TileKernels 支持三种 SF 布局：

1. **row-major（默认）**：SF 形状 `(num_block_m, num_block_k)`，与数据张量的行列同序。SF 用 `float32`。
2. **col-major（TMA 对齐）**：SF 形状 `(num_block_k, num_block_m)`，行列转置，便于 TMA 硬件按列搬运。SF 仍用 `float32`。
3. **packed ue8m0**：每个 SF 只存指数（8 位无符号），4 个 SF 打包进内层维度，形状变为 `(ceil(num_block_k/4), num_block_m*4)`。SF 用 `uint8`（或 `int32` 容器）。

后两种通常成对出现（见 4.3 场景 3）：packed ue8m0 本质上要求 col-major 存储。

#### 4.4.2 核心流程

给定数据形状 `(num_tokens, hidden)` 与 `sf_block = (block_m, block_k)`，SF 块数为：

\[
\text{num\_block\_m} = \lceil \text{num\_tokens} / \text{block\_m} \rceil, \qquad
\text{num\_block\_k} = \lceil \text{hidden} / \text{block\_k} \rceil
\]

`get_sf_shape` 的判定逻辑（伪代码）：

```
if use_packed_ue8m0:
    num_block_m *= 4                      # 内层扩 4 倍
    num_block_k = ceildiv(num_block_k, 4) # 外层缩 4 倍（4 个 SF 打包）
if use_tma_aligned_col_major_sf:
    return (num_block_k, num_block_m)     # 列主序
else:
    return (num_block_m, num_block_k)     # 行主序
```

配套的还有三个工具：

- `alloc_scaling_factors`：按 `get_sf_shape` 分配 SF 张量，并在 col-major 时把行宽对齐到 16（ue8m0）或 4（float32）字节以满足 TMA 对齐，最后再切片回逻辑形状。
- `cast_epilogue`：kernel 跑完后把「带对齐 padding 的物理 SF」修正回「逻辑 SF」（含 ue8m0 的 int32 视图、转置、按 num_tokens 截断）。
- `load_sf` / `store_sf` / `transform_sf`：kernel 内部按当前布局读写 SF、并把 ue8m0 还原成 float32 的三个 `@T.macro`。

#### 4.4.3 源码精读

**get_sf_shape** —— 本讲的核心函数：

[tile_kernels/quant/common.py:130-138](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L130-L138)

```python
num_block_m = ceil_div(shape[0], config.sf_block[0])
num_block_k = ceil_div(shape[1], config.sf_block[1])
if config.use_packed_ue8m0:
    num_block_m = num_block_m * 4
    num_block_k = ceil_div(num_block_k, 4)
return (num_block_k, num_block_m) if config.use_tma_aligned_col_major_sf else (num_block_m, num_block_k)
```

注释明确指出：「For UE8M0, we must use col-major SF, and 4 UE8M0 are expanded into the inner dim (token)」。

**alloc_scaling_factors** —— 分配并对齐：

[tile_kernels/quant/common.py:141-165](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L141-L165) —— col-major 时把第二维（行宽）对齐到 16 或 4，分配后再 `[:, :sf_shape[1]]` 切回：

```python
if out_config.use_tma_aligned_col_major_sf:
    aligned_sf_shape = align(sf_shape[1], 16 if out_config.use_packed_ue8m0 else 4)
...
scaling_factor = torch.empty(size=(sf_shape[0], aligned_sf_shape), dtype=out_config.sf_torch_dtype, device=device)
if out_config.use_tma_aligned_col_major_sf:
    scaling_factor = scaling_factor[:, : sf_shape[1]]
```

**cast_epilogue** —— 把物理 SF 修回逻辑 SF：

[tile_kernels/quant/common.py:168-193](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L168-L193) —— ue8m0 时把 uint8 视图转回 int32，col-major 时转置，再按 `ceil_div(num_tokens, sf_block[0])` 截断行数。

**load_sf / store_sf / transform_sf** —— kernel 内部的多布局读写：

[tile_kernels/quant/common.py:219-226](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L219-L226) `load_sf`；[tile_kernels/quant/common.py:237-244](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L237-L244) `store_sf`；[tile_kernels/quant/common.py:229-234](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L229-L234) `transform_sf`。三者都用 `if config.use_packed_ue8m0 / elif use_tma_aligned_col_major_sf / else` 三分支处理三种布局，例如 `load_sf` 在 ue8m0 下用 `tensor[k_idx // 4, m_idx * 4 + k_idx % 4]` 寻址。

**实际调用点**：per_token_cast wrapper 用 `get_sf_shape` 在 kernel 内声明 SF 张量形状、用 `alloc_scaling_factors` 分配输出 SF、跑完用 `cast_epilogue` 修正，见 [per_token_cast_kernel.py:58-59](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L58-L59) 与 [per_token_cast_kernel.py:202-212](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L202-L212)。

#### 4.4.4 代码实践：手算三种布局下的 SF 形状（本讲核心实践）

**实践目标**：给定数据形状与 `sf_block`，手算 `get_sf_shape` 在三种布局下的输出，从而把本节公式彻底吃透。

**设定**：`shape = (num_tokens, hidden) = (64, 128)`，`sf_block = (1, 64)`（即每个 token、每 64 个 hidden 列共享一个 SF）。

**操作步骤**：

1. 先算基础块数：
   - `num_block_m = ceil_div(64, 1) = 64`
   - `num_block_k = ceil_div(128, 64) = 2`
2. 分别代入三种布局（注意布局 3 同时置 `use_packed_ue8m0=True` 与 `use_tma_aligned_col_major_sf=True`）。
3. 验证三者元素总数与「逻辑 SF 个数 = num_block_m × num_block_k = 128」的关系。

**预期结果**（参考答案）：

| 布局 | 标志 | 计算 | get_sf_shape 输出 |
| --- | --- | --- | --- |
| row-major | 都 False | 直接返回 `(num_block_m, num_block_k)` | **(64, 2)** |
| col-major | `use_tma_aligned_col_major_sf=True` | 返回转置 `(num_block_k, num_block_m)` | **(2, 64)** |
| packed ue8m0 | 两个标志都 True | `num_block_m=64*4=256`，`num_block_k=ceil_div(2,4)=1`，返回列主序 | **(1, 256)** |

**需要观察的现象**：

- row-major 与 col-major 只是转置关系，元素总数都是 \(64 \times 2 = 128\)。
- packed ue8m0 的元素总数是 \(1 \times 256 = 256\)，是 128 的 2 倍——**多出来的并不是更多 SF**，而是因为每个 SF 用 8 位（ue8m0）存，而 `(1, 256)` 的 `uint8` 张量共 256 字节，正好对应 128 个 SF 的指数（这里内层 `*4` 与外层 `/4` 的打包在「形状」上并不完全抵消，因为 `ceil_div(2,4)=1` 把外层压到了 1，内层则固定扩 4 倍）。也就是说，packed 布局的「形状」反映的是打包后的物理排布，不能直接当逻辑 SF 数读。

> 待本地验证：装好依赖后可执行下面这段（示例代码，非项目原有）来核对：
> ```python
> from tile_kernels.quant.common import get_sf_shape, CastOutputConfig
> cfg_row  = CastOutputConfig(sf_block=(1,64))
> cfg_col  = CastOutputConfig(sf_block=(1,64), use_tma_aligned_col_major_sf=True)
> cfg_pack = CastOutputConfig(sf_block=(1,64), use_tma_aligned_col_major_sf=True, use_packed_ue8m0=True)
> print(get_sf_shape((64,128), cfg_row))   # 预期 (64, 2)
> print(get_sf_shape((64,128), cfg_col))   # 预期 (2, 64)
> print(get_sf_shape((64,128), cfg_pack))  # 预期 (1, 256)
> ```
> 由于 `get_sf_shape` 是纯 Python（只依赖 `ceil_div`），这段在无 GPU 环境也能跑（只要能 import torch）；若 import 链路拉起 tilelang 失败，则以纸面结果为准。

#### 4.4.5 小练习与答案

**练习 1**：把 `sf_block` 改成 `(1, 128)`（即整行 hidden 共享一个 SF），对 `(64, 128)` 重算 row-major 形状。
**答案**：`num_block_m=64`，`num_block_k=ceil_div(128,128)=1`，row-major 输出 **(64, 1)**。每行一个 SF，共 64 个。

**练习 2**：为什么 packed ue8m0 要强制 col-major？
**答案**：ue8m0 把 4 个 SF 的指数打包进一个内层单元，这要求内层（token 维）连续可寻址且按 4 对齐，而 col-major 正是把 token 维放在内层（第二维）并做 TMA 对齐（`alloc_scaling_factors` 里 `align(..., 16)`）。行主序无法满足这种打包与对齐，所以源码注释明确「For UE8M0, we must use col-major SF」。

**练习 3**：`alloc_scaling_factors` 为什么分配后再 `[:, :sf_shape[1]]` 切一刀？
**答案**：为了 TMA 对齐，它把行宽向上 round 到 16/4 的倍数分配，但逻辑上只需要 `sf_shape[1]` 那么宽，所以切掉 padding；`cast_epilogue` 后续还会按真实的 `num_tokens` 再截断行数，保证最终 SF 张量形状与逻辑一致。

---

## 5. 综合实践

把本讲的「格式 + 配置 + SF 形状」串起来，做一个小设计任务。

**任务**：假设你有一块激活张量 `x`，形状 `(num_tokens=128, hidden=256)`，当前是 `torch.bfloat16`。你想用 `per_token_cast` 把它量化到 **e4m3**，每 32 个 hidden 列共享一个 SF（即 `sf_block=(1,32)`），并且希望 SF 用 **packed ue8m0** 存储（同时意味着 col-major + round 到 2 的幂）。

请完成：

1. **格式确认**：写出 e4m3 的位宽、最大值、PyTorch 容器 dtype（参考 4.1 表）。
2. **构造输出配置**：说明调用 `get_cast_output_config` 时应传哪些参数（`fmt`、`sf_block`、`use_tma_aligned_col_major_sf`、`round_sf`、`use_packed_ue8m0`），得到的 `CastOutputConfig` 的 `dtype`、`sf_torch_dtype` 分别是什么。
3. **构造输入配置**：因为输入是普通 BF16 张量，说明 `get_cast_input_and_config(x, None)` 返回的 `with_sf` 值。
4. **算 SF 形状**：手算 `get_sf_shape((128, 256), out_config)` 的输出形状。
5. **描述产物**：最终 `per_token_cast` 返回的是一个 `QuantTensor = (out, out_sf)`，说出 `out` 的 dtype 与 `out_sf` 的 dtype、形状。

**参考答案要点**：

1. e4m3：8 位、max=448、容器 `torch.float8_e4m3fn`。
2. `get_cast_output_config('e4m3', sf_block=(1,32), use_tma_aligned_col_major_sf=True, round_sf=True, use_packed_ue8m0=True)`；`dtype = T.float8_e4m3fn`；`sf_torch_dtype = torch.uint8`（因为 `use_packed_ue8m0=True`）。
3. 输入无 SF，`with_sf=False`。
4. `num_block_m=128`，`num_block_k=ceil_div(256,32)=8`；packed 下 `num_block_m=128*4=512`，`num_block_k=ceil_div(8,4)=2`；col-major 返回 **(2, 512)**。
5. `out.dtype = torch.float8_e4m3fn`，形状 `(128, 256)`；`out_sf.dtype = torch.uint8`（经 `cast_epilogue` 后以 int32/uint8 容器呈现），逻辑上对应 128×8=1024 个 SF。

> 待本地验证：第 4、5 步的形状可由 `get_sf_shape` 直接验证；端到端跑 `per_token_cast` 需要 SM90/SM100 GPU 与完整依赖。

## 6. 本讲小结

- 量化的根本动机是**省显存带宽**；低比特浮点保留「指数+尾数」结构，配合 **scaling factor（SF）** 用满表示范围。
- 三种目标格式：**e4m3**（8 位，max≈448，原生 FP8）、**e2m1**（4 位，max=6，两个打包进 int8）、**e5m6**（12 位，max=65024，打包进 uint32）。
- `QuantTensor = (tensor, sf)` 是整个 quant 子包对「带缩放因子的量化张量」的统一约定（[types.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/types.py)）。
- 配置体系用三个 `frozen dataclass`：`BaseCastConfig`（公共字段 + `dtype`/`sf_dtype` 派生属性）、`CastInputConfig`（输入是否带 SF）、`CastOutputConfig`（是否 round_sf、clamp 下限）。
- `get_cast_input_and_config` 会**根据输入 SF 的 stride/dtype 自动推断** col-major 与 ue8m0 标志；`get_cast_output_config` 把格式名翻成 torch dtype。
- SF 形状由 `get_sf_shape` 决定，三种布局：row-major `(num_block_m, num_block_k)`、col-major `(num_block_k, num_block_m)`、packed ue8m0 `(ceil(num_block_k/4), num_block_m*4)`；后两者通常成对出现。

## 7. 下一步学习建议

本讲只定义了「格式与配置」，还没有进入任何 cast kernel 的内部循环。建议按以下顺序继续：

- **u4-l2 per_token_cast kernel**：把本讲的 `CastInputConfig`/`CastOutputConfig` 与 `get_sf_and_inv` 放进真实的 per-token 量化主流程，看 `with_sf` 分支为何需要「两段规约（absmax→max）」。
- **u4-l3 SF 宏与幂次舍入**：精读 `get_sf_and_inv` 的 `round_sf` 位操作分支、UE8M0 打包格式，以及 `load_sf`/`store_sf`/`transform_sf` 的多布局分发——也就是本讲刻意略过的「圆整到 2 的幂」的底层实现。
- 之后再看 u4-l4（per-block / lossless / e5m6 变体与 cast_back 反量化）与 u4-l5（融合 SwiGLU + 量化），体会不同 SF 粒度与算子融合带来的精度/带宽取舍。
