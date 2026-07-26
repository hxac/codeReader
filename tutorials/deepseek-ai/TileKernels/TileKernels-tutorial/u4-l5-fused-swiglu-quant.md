# 融合 SwiGLU + 量化 与 per-channel transpose

## 1. 本讲目标

本讲是量化模块（第 4 单元）的收尾篇。前面 u4-l1 到 u4-l4 讲的都是「单一职责」的 cast kernel：输入一个张量，输出一个量化张量。本讲往上走一步，回答一个工程问题：

> 当量化算子的输入本身是另一个逐元素算子（SwiGLU 激活）的计算结果时，我们能不能把「先算激活、再单独量化」两步合成一个 kernel？

答案是能，而且收益巨大——因为这类算子是**访存受限（bandwidth-bound）**的，省下一次全局存储（HBM）的读写往返，就能让有效带宽接近线性提升。

学完本讲，你应当能够：

1. 说出 SwiGLU 的前向数学定义，并对照 `torch/swiglu.py` 的纯 PyTorch 参考实现。
2. 用字节开销（bytes）定量解释「融合」相比「两步走」省下了哪些全局存储读写。
3. 读懂三个融合 kernel 的内部结构：
   - `swiglu_forward_and_per_token_cast`（SwiGLU + per-token 量化）
   - `per_channel_cast_and_transpose`（纯量化 + 转置，不含 SwiGLU）
   - `swiglu_forward_and_per_channel_cast_and_transpose`（SwiGLU + per-channel 量化 + 转置，三合一）
4. 区分 per-token 与 per-channel 两种 SF 粒度，以及融合 kernel 内部两种不同的实现风格（fragment 集体式 vs local+shared 直索引式）。

## 2. 前置知识

本讲默认你已经学过：

- **u2-l1 / u2-l2 / u2-l3**：TileLang 的 `@tilelang.jit` + `@T.prim_func` 骨架、`alloc_fragment`/`alloc_local`/`alloc_shared` 三级存储、`T.Parallel`/`T.unroll`/`T.vectorized` 循环与 `reduce_absmax` 规约原语。
- **u3-l1**：批量转置 kernel 用「寄存器 4×4 翻转 + 共享内存 swizzle + padding」消除 bank conflict 的套路——本讲的 per-channel transpose kernel 就是它的近亲。
- **u4-l1 / u4-l2 / u4-l3**：低比特格式（e4m3/e2m1/e5m6）、`QuantTensor=(tensor, sf)` 约定、`CastOutputConfig` 配置体系、`get_sf_and_inv`/`store_sf`/`get_best_vectorize_size` 等 SF 宏，以及 per-token cast 的「load→absmax→定标→scale→store」五段骨架。

两个本讲要用到的核心术语回顾：

- **SwiGLU**：一种门控激活，定义为 \(\text{silu}(x_l)\cdot x_r\)，其中 \(\text{silu}(x)=x\cdot\sigma(x)=x/(1+e^{-x})\)。输入张量在最后一维拆成左右两半 \(x_l\) 和 \(x_r\)。
- **SF（scaling factor，缩放因子）**：低比特量化的定标系数。per-token 的 SF 块形如 \((1,\text{num\_per\_channels})\)（每行沿 hidden 切段）；per-channel 的 SF 块形如 \((\text{num\_per\_tokens},1)\)（多个 token 共享同一列的 SF）。详见 4.3 与 4.4 的对比。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/torch/swiglu.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/swiglu.py) | 纯 PyTorch 参考实现：`swiglu_forward`（前向）与 `swiglu_backward`（反向），供测试对拍验证数值等价。 |
| [tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py) | SwiGLU + per-token FP8 量化的融合 kernel，带 MoE 路由上下文（topk 权重、专家掩码）与可选 clamp 计数。 |
| [tile_kernels/quant/per_channel_cast_and_transpose_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_channel_cast_and_transpose_kernel.py) | 纯 BF16→FP8 量化 + 转置的融合 kernel（不含 SwiGLU），用寄存器转置 + swizzle 风格。 |
| [tile_kernels/quant/swiglu_forward_and_per_channel_cast_and_transpose_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_channel_cast_and_transpose_kernel.py) | SwiGLU + per-channel FP8 量化 + 转置的三合一融合 kernel，含「转置 / 不转置」两个分支。 |
| [tile_kernels/quant/common.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py) | 共享基础设施：`get_cast_output_config`、`get_sf_and_inv`、`store_sf`、`alloc_scaling_factors`、`cast_epilogue`、`get_best_vectorize_size` 等。 |

测试与基准见：

- [tests/quant/test_swiglu_forward_and_per_token_cast.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_swiglu_forward_and_per_token_cast.py)
- [tests/quant/test_swiglu_forward_and_per_channel_cast_and_transpose.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_swiglu_forward_and_per_channel_cast_and_transpose.py)

## 4. 核心概念与源码讲解

### 4.1 SwiGLU 前向数学定义与 PyTorch 参考

#### 4.1.1 概念说明

SwiGLU（Swish-Gated Linear Unit）是 Transformer FFN 里常用的门控激活。给定输入 \(x\)，形状为 \((N, 2H)\)，把它沿最后一维拆成左右两半：

\[
x_l = x[:, :H], \quad x_r = x[:, H:]
\]

SwiGLU 前向定义为：

\[
\text{out} = \text{silu}(x_l)\cdot x_r = \frac{x_l}{1+e^{-x_l}}\cdot x_r
\]

注意输出形状从 \((N, 2H)\) 变成 \((N, H)\)——「宽度减半」是 SwiGLU 的固有行为，所以本讲所有 kernel 的输入 `hidden*2`、输出 `hidden` 都是来自这里。

在 MoE（专家混合）场景下，SwiGLU 的输出还要按 token 的路由权重 `topk_weights` 做逐行缩放：

\[
\text{out}_i \leftarrow \text{out}_i \cdot w_i
\]

其中 \(w_i\) 是第 \(i\) 个 expanded token 对应的 top-k 路由权重。此外，为了数值稳定，常在激活前对 \(x_l\) 和 \(x_r\) 做 clamp（截断）。

#### 4.1.2 核心流程

`swiglu_forward` 参考实现的步骤：

1. 把输入 `x` 升精度到 fp32，拆成 `x_left` / `x_right` 两半。
2. 可选 clamp：`x_left` 截到 \((-\infty, v]\)，`x_right` 截到 \([-v, v]\)。
3. 计算 `out = x_left / (1 + exp(-x_left)) * x_right`。
4. 可选：按 `pos_to_token_topk` 把每个 expanded token 映射到路由权重，逐行相乘。
5. 返回 fp32 输出。

#### 4.1.3 源码精读

拆半与升精度：

[swiglu.py:L60-L63](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/swiglu.py#L60-L63) —— `x.float()` 升到 fp32，再切成左右两半。参考实现全程 fp32，是为了给「融合 kernel 在 fragment 里也用 fp32 中间计算」提供数值基准。

clamp 逻辑：

[swiglu.py:L66-L72](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/swiglu.py#L66-L72) —— 当传入 `swiglu_clamp_value` 时，`x_left` 只截上限、`x_right` 同时截上下限；若还传了 `clamped_count`，则先把被截断的元素个数累加进长度为 3 的计数张量（index 0 统计 \(x_l>v\)，1 统计 \(x_r>v\)，2 统计 \(x_r<-v\)）。这个计数在 4.3 会看到 kernel 侧对应实现。

SwiGLU 主公式：

[swiglu.py:L75](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/swiglu.py#L75) —— `out = x_left / (1.0 + torch.exp(-x_left)) * x_right`。这正是融合 kernel 里要逐元素复现的核心式子。

逐行路由权重缩放：

[swiglu.py:L78-L86](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/swiglu.py#L78-L86) —— `pos_to_token_topk` 把每个 expanded token 映射到一个扁平的 `(token, topk)` 下标；下标 `< 0` 表示 padding（对应输出行置零）。`w_expanded` 是按 expanded token 展开后的权重向量，最后 `out * w_expanded.unsqueeze(1)` 完成逐行缩放。

> 旁注：本文件还有一个 [swiglu_backward](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/swiglu.py#L98-L227) 与 [elementwise_fma](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/swiglu.py#L93-L95)（用 `@torch.compile` 显式捕获乘加 pattern 以规避精度问题）。它们服务于 modeling 层的反向传播，本讲聚焦前向融合，反向留到第 8 单元（u8-l1 autograd 封装）。

#### 4.1.4 代码实践

**目标**：在不依赖 GPU 的前提下，用纯 PyTorch 复现 SwiGLU 前向，确认你对公式的理解与 `swiglu_forward` 一致。

**操作步骤**（示例代码，可直接在任意带 torch 的环境运行）：

```python
# 示例代码：纯 torch 复现 SwiGLU 前向，对照 tile_kernels.torch.swiglu_forward
import torch

N, H = 4, 128
x = torch.randn(N, 2 * H, dtype=torch.bfloat16, device='cpu')

# 手写参考
x_fp32 = x.float()
x_left, x_right = x_fp32[:, :H], x_fp32[:, H:]
out_manual = x_left / (1.0 + torch.exp(-x_left)) * x_right

# 项目参考实现（cpu 也能跑，swiglu_forward 只用 torch 原语）
from tile_kernels.torch import swiglu_forward
out_ref = swiglu_forward(x)

print(torch.allclose(out_manual, out_ref))  # 预期 True（位精确，同一组运算）
```

**需要观察的现象**：`out_manual` 与 `out_ref` 完全相等（`allclose` 返回 `True`）。

**预期结果**：两者逐元素一致，证明 SwiGLU 公式就是 `silu(x_left) * x_right`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `x_left` 换成 ReLU，SwiGLU 会退化成什么？
**答**：变成 \(\text{relu}(x_l)\cdot x_r\)，即 ReLU-Gated Linear Unit（ReGLU 的近亲）。SwiGLU 的「Swish」部分特指用 silu 做门控。

**练习 2**：为什么参考实现要先把 `x` 升到 fp32 再算 `exp`？
**答**：`exp(-x)` 在 \(x\) 较负时会很大，bf16 的动态范围（约 ±65504）与精度都不足以稳定表示；fp32 提供更大指数域和更高精度，作为融合 kernel 内部计算的「黄金参考」。

---

### 4.2 为什么要融合：访存代价分析

#### 4.2.1 概念说明

承接 u3-l1 的结论：转置、量化这类「每元素只算一两次、但要搬动整个张量」的算子是**访存受限**的——性能瓶颈不在算力（FLOPs），而在 HBM 带宽。对这类算子，衡量好坏的指标是**有效带宽（GB/s）**：

\[
\text{bandwidth} = \frac{\text{实际搬运的字节数}}{\text{耗时}}
\]

「融合」的核心思想：如果两个相邻算子的中间结果不需要被别的算子复用，就别把它写回 HBM 再读回来——让它在片上（寄存器 / 共享内存）流动一次即可。每省下一次「写中间结果 + 读中间结果」的 HBM 往返，有效带宽就近乎成比例提升。

#### 4.2.2 核心流程（字节开销对账）

设输入 `x` 形状 \((N, 2H)\)，bf16（2 字节），输出 FP8 e4m3（1 字节）。先看 **per-token 融合** 的两条路径：

| 步骤 | 两步走（先 SwiGLU 再单独量化） | 融合 |
| --- | --- | --- |
| 读输入 `x` | \(2\cdot N\cdot 2H = 4NH\) 字节 | \(4NH\) 字节 |
| 写 SwiGLU 中间结果（bf16, \((N,H)\)） | \(2NH\) 字节 | 0 |
| 读 SwiGLU 中间结果 | \(2NH\) 字节 | 0 |
| 写 FP8 输出 \((N,H)\) | \(NH\) 字节 | \(NH\) 字节 |
| 写 SF（小，略） | 略 | 略 |
| **HBM 总流量** | \(\approx 9NH\) | \(\approx 5NH\) |

融合省下的正是中间结果的一次往返：\(2NH + 2NH = 4NH\) 字节，约占总流量的 \(4/9\approx 44\%\)。对一个纯访存受限的算子，这意味着有效带宽可提升约 \(9/5=1.8\) 倍。

测试里 `count_bytes` 正是按**融合**口径统计的——只数输入 `x`、输出 `x_fp8`、`x_sf` 三项：

[test_swiglu_forward_and_per_token_cast.py:L170](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_swiglu_forward_and_per_token_cast.py#L170) —— `num_bytes = count_bytes(x, x_fp8, x_sf)`，对应上表「融合」列。

对于 **per-channel + transpose** 融合，收益更进一步：单独做一次转置意味着对 FP8 输出再「读 \(NH\) + 写 \(NH\)」一次（额外 \(2NH\)），融合后这部分也省掉了。

#### 4.2.3 源码精读

wrapper 分配输出时，只看到一次输入、两类输出（FP8 张量 + SF），没有任何中间 bf16 张量被分配：

[swiglu_forward_and_per_token_cast_kernel.py:L252-L256](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py#L252-L256) —— 只 `torch.empty` 了 FP8 的 `out` 和 SF 的 `out_sf`，SwiGLU 的中间激活全程活在 kernel 内部的 fragment 里，不落 HBM。

#### 4.2.4 代码实践

**目标**：亲手把 4.2.2 的字节账算一遍，体会「44% 流量节省」怎么来的。

**操作步骤**：取一组真实参数（如 `N=4096, H=7168`，bf16），用 Python 算出两步走与融合两条路径的 HBM 字节数与比值。

```python
# 示例代码：字节开销对账
N, H = 4096, 7168
bytes_bf16, bytes_fp8 = 2, 1

read_x = bytes_bf16 * N * 2 * H          # 读 x
mid_roundtrip = 2 * (bytes_bf16 * N * H) # 中间结果写+读（两步走独有）
write_fp8 = bytes_fp8 * N * H            # 写 FP8 输出

two_step = read_x + mid_roundtrip + write_fp8
fused = read_x + write_fp8
print(f"two_step = {two_step/1e9:.2f} GB")
print(f"fused    = {fused/1e9:.2f} GB")
print(f"saved    = {(two_step-fused)/two_step*100:.1f}%")
print(f"speedup ceiling = {two_step/fused:.2f}x")
```

**需要观察的现象**：省下比例约 44%，理论带宽上限提升约 1.8 倍。

**预期结果**：`saved ≈ 44.4%`，`speedup ceiling ≈ 1.80x`。这正是融合的核心动机。

#### 4.2.5 小练习与答案

**练习 1**：如果中间结果用 fp32 而非 bf16 暂存，融合的收益会变大还是变小？
**答**：变大。fp32 中间结果往返要 \(4NH+4NH=8NH\) 字节（而非 \(4NH\)），两步走总流量涨到 \(13NH\)，融合省下的比例更高（约 62%）。

**练习 2**：为什么对「计算受限」的算子（如大 GEMM）融合的收益不如这里明显？
**答**：计算受限算子的瓶颈是 FLOPs 而非带宽，省下的 HBM 流量不在瓶颈上，故提升有限。融合最契合的是本讲这类逐元素、低算术强度（arithmetic intensity）的算子。

---

### 4.3 swiglu_forward_and_per_token_cast：per-token 量化融合 kernel

#### 4.3.1 概念说明

这是本讲最复杂的一个 kernel。它在 per-token cast（u4-l2）的五段骨架上，前面「嫁接」了 SwiGLU 前向，并额外支持 MoE 路由上下文：

- **per-token 量化**：SF 块为 \((1, \text{num\_per\_channels})\)，每个 expanded token 沿 hidden 维按 `num_per_channels`（128 或整个 hidden）切段，每段一个 SF。
- **MoE 上下文**：通过 `pos_to_token_topk`（查路由权重）、`pos_to_expert`（专家掩码，`-1` 表 padding）把 SwiGLU 输出按路由权重缩放，并跳过 padding 行。
- **clamp 计数**：可选地统计被截断的元素个数，此时 kernel 切换为 **persistent kernel** 模式。

#### 4.3.2 核心流程

整个 kernel 用 **fragment 集体式风格**（与 u4-l2 的 per_token_cast 同源），网格是一维的 `num_blocks`，每块处理一个 `(TILE_X 行 × TILE_Y 列)` 的瓦片：

1. **load**：把 `x` 的左半 `xl`、右半 `xr` 读进 fragment（fp32）。
2. **SwiGLU + clamp + 权重**：逐元素算 `val = val_l / (1+exp(-val_l)) * val_r`（可选 clamp、可选 `* topk_weight`），结果写回 `x_fragment`。
3. **reduce_absmax**：对 reshape 成 `[TILE_X, num_groups, num_per_channels]` 的 fragment 沿最后一维做 absmax，得到每段的 `sf_inv_fragment`。
4. **get_sf_and_inv + store_sf**：定标、写出 SF。
5. **scale + store**：`out = x_fragment * sf_inv`，cast 成 e4m3 后 `T.copy` 写回全局内存。

padding 守卫：当 `pos_to_expert[i] == -1` 时，跳过该行（不读不写）。

persistent 模式：当 `count_clamp=True` 时，网格块数固定为 `num_sms * 4`，每个块用一个 `T.serial` 循环轮流处理多个逻辑瓦片，最后用 `finalize_reducer` + `atomic_add` 把三路 clamp 计数汇总。

#### 4.3.3 源码精读

JIT 构造器与编译开关：

[swiglu_forward_and_per_token_cast_kernel.py:L11-L15](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py#L11-L15) —— `@tilelang.jit` 关掉 warp specialized（`TL_DISABLE_WARP_SPECIALIZED=True`），因为这种逐元素 kernel 不需要 warp specialization 的流水线收益。

瓦片大小启发式（4096 元素硬预算，与 u4-l2 一致）：

[swiglu_forward_and_per_token_cast_kernel.py:L26-L46](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py#L26-L46) —— `TILE_Y` 从 `num_per_channels` 起步不断翻倍直到填满 4096 元素预算；`TILE_X` 调整到能被线程数整除。

运行时符号与 persistent 切换：

[swiglu_forward_and_per_token_cast_kernel.py:L48-L57](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py#L48-L57) —— `num_expanded_tokens` 等用 `T.dynamic` 声明（承接 u2-l1）；`count_clamp` 为真时 `num_blocks = num_sms * 4`，进入 persistent 模式。

SwiGLU 主计算（含 clamp 两分支与权重缩放）：

[swiglu_forward_and_per_token_cast_kernel.py:L120-L144](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py#L120-L144) —— 这段是本 kernel 的「融合心脏」。注意 clamp 分两种：`count_clamp` 为真时用 `T.Select` 同时把截断计数累加进 reducer（L127-L136）；否则用更便宜的 `T.min`/`T.max`（L138-L139）。最终公式在 L141-L143：`val = val_l / (1 + T.exp(-val_l)) * val_r [* topk_weights]`，与 4.1 的参考完全一致。

定标与写出 SF：

[swiglu_forward_and_per_token_cast_kernel.py:L147-L154](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py#L147-L154) —— `reduce_absmax(..., dim=2)` 做部分规约（承接 u2-l3）；随后调用 u4-l3 讲过的 `get_sf_and_inv` 与 `store_sf`（后者按 row-major / col-major / packed ue8m0 自动分发）。

scale + cast store：

[swiglu_forward_and_per_token_cast_kernel.py:L157-L160](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py#L157-L160) —— `out_fragment = x_fragment * sf_inv`，再 `T.copy` 写回。这一步把 fp32 的 SwiGLU 结果一次性 cast 成 e4m3，中间结果从未落 HBM。

persistent 收尾（计数汇总）：

[swiglu_forward_and_per_token_cast_kernel.py:L162-L170](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py#L162-L170) —— `finalize_reducer` 合并跨线程累加器，`tid==0` 的线程用 `atomic_add` 把三路 clamp 计数写到长度为 3 的全局张量。这正是 4.1 参考里 `clamped_count` 的 kernel 侧对应。

wrapper：校验 → 建 `out_config` → 取/编译 kernel → 分配输出 → 启动 → `cast_epilogue`：

[swiglu_forward_and_per_token_cast_kernel.py:L234-L260](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py#L234-L260) —— 注意 `out_config = get_cast_output_config('e4m3', (1, num_per_channels), ...)`，SF 块是 \((1, \text{num\_per\_channels})\)（per-token）；`cast_epilogue` 在尾部把 SF 张量按布局（col-major / packed ue8m0）整形成最终形状。

#### 4.3.4 代码实践

**目标**：对照测试里的 `func_ref`，理解「融合 kernel ≡ 先 `swiglu_forward` 再 `cast`」的数值等价。

**操作步骤**：阅读 [test_swiglu_forward_and_per_token_cast.py:L91-L110](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_swiglu_forward_and_per_token_cast.py#L91-L110)。参考路径分两步：先调 `swiglu_forward(x, ...)` 得 fp32 激活，再 `cast(out, 'e4m3', (1, num_per_channels), ...)` 量化；被测路径是一步 `tile_kernels.quant.swiglu_forward_and_per_token_cast(...)`。

**需要观察的现象**：两条路径的 FP8 输出与 SF 都通过 `assert_equal`（位精确）判等（见 [L130-L131](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_swiglu_forward_and_per_token_cast.py#L130-L131)）；当开启 clamp 计数时，三路计数也位精确相等（[L132-L133](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_swiglu_forward_and_per_token_cast.py#L132-L133)）。

**预期结果**：融合前后位精确一致——这证明融合只是「把两步合进一个 kernel」，没有改变数学语义。

**GPU 验证（待本地验证）**：在带 SM90/SM100 GPU 的环境执行：

```bash
pytest tests/quant/test_swiglu_forward_and_per_token_cast.py -n 4
```

应全部通过；设置 `TK_PRINT_KERNEL_SOURCE=1` 再跑一次，可看到融合后生成的单一 CUDA 源码（没有独立的 SwiGLU kernel）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `count_clamp=True` 时要切到 persistent kernel（`num_blocks = num_sms * 4`）？
**答**：clamp 计数要把「所有瓦片各自的截断个数」汇总到一个长度为 3 的全局计数器。若每个逻辑瓦片一个块，就需要大量跨块原子；persistent 模式让每个 SM 长驻、在块内用 reducer 先把多个逻辑瓦片的计数加总，最后每块只发一次 `atomic_add`，大幅减少原子竞争。

**练习 2**：`pos_to_expert[i] == -1` 的行在 kernel 里如何被处理？
**答**：见 [L114-L118](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_token_cast_kernel.py#L114-L118) 与各处的 `if (not with_pos_to_expert) or pos_to_expert_fragment[i] >= 0` 守卫：padding 行既不读输入、也不写输出/SF，等价于在参考侧 `masked_fill(mask, 0)`。

---

### 4.4 per_channel_cast_and_transpose：量化 + 转置融合

#### 4.4.1 概念说明

这个 kernel **不含 SwiGLU**，只做一件事：把 BF16 矩阵 \((N, H)\) 量化成 FP8 并**同时转置**成 \((H, N)\)。它是 u3-l1 批量转置 kernel 的「量化加强版」。

关键差异在 **SF 粒度**：这里用 **per-channel**，即 SF 块为 \((\text{num\_per\_tokens}, 1)\)，`num_per_tokens ∈ {32, 128}` 表示「把连续 `num_per_tokens` 个 token 分一组，每组每个 channel（hidden 维的每一列）各一个 SF」。SF 张量形状为 \((N/\text{num\_per\_tokens}, H)\)。这与 4.3 的 per-token（每行分段）是两个正交方向。

> 命名提示：「per-channel」强调「每个 channel 一个 SF」，代价是 token 维要按 `num_per_tokens` 分组。它本质是 u4-l4 讲过的 per-block 量化（二维块），只是 block 的形状是 \((\text{num\_per\_tokens}, 1)\)。

#### 4.4.2 核心流程

实现用 **local + shared 直索引风格**（与 u3-l1 转置同源，区别于 4.3 的 fragment 集体式）。网格是二维 `(hidden//TILE_Y, num_tokens//TILE_X)`，`TILE_X=128, TILE_Y=64, TILE_K=4`：

1. **读 + 寄存器转置**：每个线程读一个 \(4\times4\) 小块进 `tmp` 寄存器，原地翻转行列（`tmp[k,j]=tmp_row[k]`）。
2. **swizzle 写共享内存**：用 `swizzle_j = (j + tid//4) % TILE_K` 打散线程规律，配合共享内存 `+TILE_K` padding 消除 bank conflict（承接 u3-l1）。
3. **分阶段规约定标**：把共享内存按 4 个 stage 分批 `T.copy` 进 fragment，每批 `reduce_absmax` 算 per-channel amax，`get_sf_and_inv` 定标写 SF，并用 `sf_inv` 把 fragment 缩放。
4. **转置写出**：`out[pid_y*TILE_Y+i, pid_x*TILE_X+j] = fragment[i,j] * sf_inv`——下标互换正是转置。

分阶段（`num_stages=4`）的目的是**降低寄存器压力**：一次只处理 `TILE_Y/4` 行，复用同一组 fragment 寄存器。

#### 4.4.3 源码精读

瓦片常量与线程映射：

[per_channel_cast_and_transpose_kernel.py:L22-L25](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_channel_cast_and_transpose_kernel.py#L22-L25) —— `TILE_X, TILE_Y, TILE_K = 128, 64, 4`，`num_threads_per_token = TILE_Y // TILE_K`，即每个线程负责共享内存 TILE_Y 维上的 `TILE_K` 个元素。

共享内存 padding：

[per_channel_cast_and_transpose_kernel.py:L39](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_channel_cast_and_transpose_kernel.py#L39) —— `out_shared` 形状 `(TILE_Y, TILE_X + TILE_K)`，`+TILE_K` 是静态 padding，让行宽不再是 32 的倍数，降低 bank conflict（u3-l1 讲过的套路）。

寄存器转置 + swizzle 写共享：

[per_channel_cast_and_transpose_kernel.py:L46-L61](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_channel_cast_and_transpose_kernel.py#L46-L61) —— 读入 `tmp_row` 后写进 `tmp[k,j]`（行列翻转）；写共享时用 `swizzle_j = (j + tid // 4) % TILE_K` 改写线程-元素映射，进一步打散 bank 访问规律。

分阶段规约定标 + 转置写出：

[per_channel_cast_and_transpose_kernel.py:L65-L80](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_channel_cast_and_transpose_kernel.py#L65-L80) —— 4 个 stage 逐批处理；`reduce_absmax` 沿 `num_per_tokens` 维（reshape 后的最后一维）规约；写出时下标 `[pid_y*TILE_Y+..., pid_x*TILE_X+...]` 把行列互换，完成转置。这一段同时完成了量化与转置，省下了对 FP8 结果的单独转置 pass。

wrapper：

[per_channel_cast_and_transpose_kernel.py:L97-L119](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_channel_cast_and_transpose_kernel.py#L97-L119) —— 强制 `x.dtype == torch.bfloat16`、`fmt == 'e4m3'`、`num_per_tokens ∈ {32,128}`；SF 块 `(num_per_tokens, 1)`；输出分配成转置形状 `(hidden, num_tokens)`。

#### 4.4.4 代码实践

**目标**：理解「量化 + 转置」融合相比「先量化再转置」省了什么。

**操作步骤**：阅读测试 [test_per_channel_cast_and_transpose.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_per_channel_cast_and_transpose.py)（若存在）。参考路径通常是 `cast` 后再 `.T.contiguous()`，被测路径一步完成。用 4.2 的字节账思路手算：单独转置要对 FP8 数据再「读 \(NH\) + 写 \(NH\)」，约 \(2NH\) 字节，融合后省掉。

**需要观察的现象**：融合 kernel 的 `count_bytes` 只统计输入 bf16 与输出 FP8/SF，不含额外的转置 pass。

**预期结果**：相比「量化 + 单独转置」，融合省下约 \(2NH\) 字节的 HBM 流量（FP8 数据的一次读 + 一次写）。

#### 4.4.5 小练习与答案

**练习 1**：per-channel 的 SF 形状是 \((N/\text{num\_per\_tokens}, H)\)，per-token 的 SF 形状是 \((N, H/\text{num\_per\_channels})\)。谁的 SF 更多？
**答**：取决于 `num_per_tokens` 与 `num_per_channels` 的相对大小。当二者都是 128 且 \(H=7168\) 时，per-token 有 \(N\times56\) 个 SF，per-channel 有 \(N/128\times7168=N\times56\) 个——数量相同，但物理布局（哪个维度被分组）不同，适配不同的下游 GEMM。

**练习 2**：为什么用 `num_stages=4` 分阶段，而不是一次性处理整个 `TILE_Y`？
**答**：一次性处理需要 `(TILE_Y, TILE_X)` 的 fragment，寄存器占用过大；分阶段把 fragment 高度降到 `TILE_Y/4`，复用同一组寄存器 4 次，降低寄存器压力、提高占用率（occupancy）。

---

### 4.5 swiglu_forward_and_per_channel_cast_and_transpose：三合一融合

#### 4.5.1 概念说明

这是本讲的「终极融合」：把 **SwiGLU 前向 + per-channel 量化 + 转置** 三件事合进一个 kernel。它有一个 `without_transpose` 开关：

- `without_transpose=True`：输出保持 \((N, H)\) 行主序（不转置），只融合 SwiGLU + per-channel 量化。
- `without_transpose=False`：输出转置成 \((H, N)\)，三件事全融合。

输入必须是 BF16，输出 e4m3，`num_per_tokens ∈ {32, 128}`。它不带 MoE 路由上下文（与 4.3 不同），适用于非 MoE 的 FFN 场景。

#### 4.5.2 核心流程

两个分支共用同一套瓦片 `TILE_X=128, TILE_Y=64, TILE_K=4`，但实现风格不同：

**`without_transpose=True` 分支**（local + shared 直索引）：

1. 向量化读 `x` 的左右半，逐元素算 SwiGLU，结果写进 `act_shared`（形状 `(TILE_X, TILE_Y)`，与输入同行序）。
2. 二次读 `act_shared`，用 `bfloat16x2` 打包的 `abs2` 做组内 absmax，写进 `amax_shared`。
3. 跨 token-block 维规约 `amax_shared` 得 per-channel amax，定标写 SF。
4. `out = act_shared * sf_inv`，向量化写回（不转置）。

**`without_transpose=False` 分支**（转置 + fragment 分阶段）：

1. 读 `x` 左右半，算 SwiGLU，结果**直接转置着**写进 `act_shared`（形状 `(TILE_Y, TILE_X+TILE_K)`，下标 `[col*..., i*...]` 互换）。
2. 与 4.4 一样，分 4 个 stage 把 `act_shared` 拷进 fragment，`reduce_absmax` 定标。
3. 转置写出 `out[pid_y*TILE_Y+i, pid_x*TILE_X+j]`。

#### 4.5.3 源码精读

构造器与瓦片调整：

[swiglu_forward_and_per_channel_cast_and_transpose_kernel.py:L22-L35](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_channel_cast_and_transpose_kernel.py#L22-L35) —— `num_per_tokens, _ = out_config.sf_block`（per-channel）；`TILE_X, TILE_Y` 按 `num_per_tokens` 与 `hidden` 可整除性微调；`num_split_blocks = TILE_X // num_per_tokens` 是 token 维的分组数。

`without_transpose` 分支的 SwiGLU 主公式：

[swiglu_forward_and_per_channel_cast_and_transpose_kernel.py:L77](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_channel_cast_and_transpose_kernel.py#L77) —— `val = val_l / (1 + T.exp(-val_l)) * val_r`，与 4.1 参考、4.3 kernel 完全一致。

转置分支的 SwiGLU + 转置着写共享：

[swiglu_forward_and_per_channel_cast_and_transpose_kernel.py:L128-L152](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_channel_cast_and_transpose_kernel.py#L128-L152) —— 注意 L146 的同一公式，以及 L152 `act_shared[col*TILE_K+j, i*TILE_K+k] = x_act_local[j,k]`——下标互换实现了「边算 SwiGLU 边转置」。注释（[L151](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_channel_cast_and_transpose_kernel.py#L151)）明确说「接受 4 路 bank conflict，因为 swizzle 的开销大于收益」——这是相对 4.4 的一个工程取舍。

转置分支的分阶段定标 + 转置写出：

[swiglu_forward_and_per_channel_cast_and_transpose_kernel.py:L155-L172](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_channel_cast_and_transpose_kernel.py#L155-L172) —— 与 4.4 的 [L65-L80](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_channel_cast_and_transpose_kernel.py#L65-L80) 几乎逐行对应——分 4 stage、`reduce_absmax`、`get_sf_and_inv`、转置写出。这说明 4.4 是 4.5 转置分支的「无 SwiGLU 子集」。

wrapper：

[swiglu_forward_and_per_channel_cast_and_transpose_kernel.py:L199-L229](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/swiglu_forward_and_per_channel_cast_and_transpose_kernel.py#L199-L229) —— 强制 bf16 输入、e4m3 输出、`num_per_tokens ∈ {32,128}`、`num_tokens % 128 == 0`；按 `without_transpose` 决定输出形状；SF 块 `(num_per_tokens, 1)`。

#### 4.5.4 代码实践

**目标**：对照参考「`swiglu_forward` → `cast` → 可选 `.T.contiguous()`」验证三合一 kernel 的数值等价。

**操作步骤**：阅读 [test_swiglu_forward_and_per_channel_cast_and_transpose.py:L62-L81](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_swiglu_forward_and_per_channel_cast_and_transpose.py#L62-L81)。参考路径：`swiglu_forward(x).bfloat16()` → `cast(..., block_size=(num_per_tokens,1))` → 不转置或 `.T.contiguous()`。被测路径一步 `swiglu_forward_and_per_channel_cast_and_transpose(...)`。

**需要观察的现象**：FP8 输出与 SF 都通过 `assert_equal` 位精确判等（[L80-L81](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_swiglu_forward_and_per_channel_cast_and_transpose.py#L80-L81)）。注意参考里 `swiglu_forward` 返回 fp32，这里先 `.bfloat16()` 再 cast——这是 kernel 用 bf16 中间存储（`act_shared` 是 `in_dtype`）的对应。

**预期结果**：位精确等价。

**GPU 验证（待本地验证）**：

```bash
pytest tests/quant/test_swiglu_forward_and_per_channel_cast_and_transpose.py -n 4
```

应全部通过。

#### 4.5.5 小练习与答案

**练习 1**：转置分支注释说「接受 4 路 bank conflict」，而 4.4 的 per_channel_cast_and_transpose 却用了 swizzle。为什么这里反而放弃 swizzle？
**答**：这里在读 `x` 算 SwiGLU 时就已经用 `x_act_local[j,k]` 做了一次寄存器内的 4×4 翻转（L147-L152），写共享的 pattern 已经被打散过；再加一层 swizzle 的指令开销超过了它省下的少量 bank conflict，故作者选择接受 4 路 conflict。这是「swizzle 收益 vs 开销」的典型权衡（承接 u3-l1）。

**练习 2**：`without_transpose=True` 分支为什么需要 `amax_shared` 这个中间共享缓冲，而转置分支不需要？
**答**：per-channel 的 SF 要把 `num_per_tokens` 个 token 的 amax 合到一起——不转置分支里这些 token 在 TILE_X 维上，需要跨线程归约，故先用 `bfloat16x2` 在线程内部分组求 abs2、再写 `amax_shared` 做跨线程 `T.max`。转置分支因为已经把数据转进了 fragment，可直接用集体的 `reduce_absmax(dim=2)` 一次完成，不需要手动中间缓冲。

---

## 5. 综合实践

把本讲的三件事（SwiGLU 公式、融合的访存收益、per-token vs per-channel 粒度）串起来，完成下面这个**对比型实践**。

**任务**：给定一组形状参数，定量比较「两步走」与「融合」两条路径的 HBM 流量，并用纯 torch 写出等价参考，确认语义一致。

**输入参数**：`N=2048` 个 expanded token，`H=7168`（单头隐藏），bf16 输入，e4m3 输出，`num_per_channels=128`。

**步骤 1：字节对账（无需 GPU）**

```python
# 示例代码
N, H = 2048, 7168
bf16, fp8 = 2, 1
read_x = bf16 * N * 2 * H
mid = 2 * (bf16 * N * H)        # SwiGLU 中间结果往返
write_fp8 = fp8 * N * H
print("two-step GB =", (read_x + mid + write_fp8) / 1e9)
print("fused    GB =", (read_x + write_fp8) / 1e9)
```

**步骤 2：参考语义（无需 GPU，对照 `swiglu_forward`）**

```python
# 示例代码：手写 SwiGLU + per-token absmax 定标，对照项目参考
import torch
x = torch.randn(N, 2 * H, dtype=torch.bfloat16, device='cpu')
xl = x.float()[:, :H]; xr = x.float()[:, H:]
act = xl / (1.0 + torch.exp(-xl)) * xr              # SwiGLU
# per-token absmax 定标（每行按 128 一段）
g = act.reshape(N, H // 128, 128)
amax = g.abs().amax(dim=2, keepdim=True)            # 每段 amax
sf = amax / 448.0                                    # e4m3 max_value≈448
print("SF shape (per-token) =", sf.shape)            # (N, H//128)
```

**步骤 3：GPU 验证（待本地验证）**

在 SM90/SM100 环境跑两个 benchmark，对比有效带宽：

```bash
pytest tests/quant/test_swiglu_forward_and_per_token_cast.py \
  -k benchmark --run-benchmark -k "benchmark"  # 读 benchmark_record 输出的 bandwidth_gbs
```

**需要观察的现象与预期结果**：

- 步骤 1：两步走 ≈ \( (4+4+1)NH = 9NH \)，融合 ≈ \(5NH\)，省约 44%。
- 步骤 2：`sf.shape == (N, H//128)`，与 kernel 的 per-token SF 形状一致；手写 `act` 与 `swiglu_forward(x)` 位精确相等。
- 步骤 3：融合 kernel 的实测 `bandwidth_gbs` 应明显反映「省下一次中间往返」的收益，且更接近硬件显存带宽极限。

> 若无 GPU，步骤 1、2 已足够建立「融合省字节、语义不变」的认知；步骤 3 标注为「待本地验证」。

## 6. 本讲小结

- **SwiGLU 前向**就是 \(\text{silu}(x_l)\cdot x_r = x_l/(1+e^{-x_l})\cdot x_r\)，输入 \((N,2H)\)、输出 \((N,H)\)；`torch/swiglu.py` 的 `swiglu_forward` 是它的纯 PyTorch 黄金参考。
- **融合的动机是省访存**：SwiGLU + 量化是访存受限算子，融合后 SwiGLU 的中间激活只在片上流动，省下一次「写 + 读」的 HBM 往返（约 \(4NH\) 字节，约占总流量 44%）。
- **`swiglu_forward_and_per_token_cast`** 用 fragment 集体式风格，把 SwiGLU（含 MoE 权重缩放、专家掩码、clamp 计数）与 per-token 量化合进一个 kernel；clamp 计数时切到 persistent kernel 模式汇总计数。
- **`per_channel_cast_and_transpose`** 用 local+shared 直索引风格（寄存器 4×4 翻转 + swizzle + padding），把量化与转置合一，per-channel 的 SF 块是 \((\text{num\_per\_tokens},1)\)。
- **`swiglu_forward_and_per_channel_cast_and_transpose`** 是三合一，转置分支与 `per_channel_cast_and_transpose` 几乎逐行同构，只是前置了 SwiGLU 计算；`without_transpose` 开关控制是否转置。
- **数值等价性**：三个融合 kernel 都与「先激活再量化（再转置）」的参考路径位精确一致，融合只改性能、不改语义。

## 7. 下一步学习建议

- **进入 modeling 层**：本讲的融合 kernel 是「前向」算子。要把它变成可训练的 PyTorch 层，需要 `torch.autograd.Function` 封装 + 反向——这正是 `torch/swiglu.py` 里 `swiglu_backward` 的用途。建议接着学 **u8-l1（autograd.Function 封装范式）**，那里会讲反向如何把梯度接回 SwiGLU 的两半输入与路由权重。
- **深化访存分析**：想更系统地理解「为什么融合能提速」，可回顾 u3-l1 的 bandwidth-bound 概念，并在 u9-l2（benchmark 插件）学会用 `count_bytes` + `benchmark_timer` 量化任意算子的有效带宽。
- **动手扩展**：若想练习新增融合算子，参考 u10-l3 的「四件套」流程——本讲的 kernel + wrapper + torch 参考 + 测试正是四件套的标准范本。
