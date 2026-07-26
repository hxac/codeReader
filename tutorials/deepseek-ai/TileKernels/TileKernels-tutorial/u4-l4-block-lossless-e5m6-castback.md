# per-block / lossless / E5M6 变体与 cast_back 反量化

## 1. 本讲目标

本讲是量化模块的「变体收尾篇」。前面 u4-l2 已经把行级量化（per-token）的主流程讲透了：load → reduce 算 absmax → 定标 → scale → store。本讲不再重复这条主线，而是围绕「主流程之外的四个变体」展开：

1. **per_block_cast**：把 SF（scaling factor）从「每行一段」推广到「二维块」，理解 per-token 与 per-block 的 SF 粒度差异。
2. **per_block_cast_lossless**：把已经量化好的 FP4（E2M1）张量**无损地**再分块成 FP8（E4M3），理解为什么「低比特→高比特」可以做到无损，以及 SF 在指数字段上的运算。
3. **per_token_cast_to_e5m6**：量化到一个自定义的 12 位格式 E5M6，理解「位打包（bit packing）」和浮点特殊值处理。
4. **cast_back**：把量化张量**反量化**回高精度（BF16/FP32），理解反量化的流程与精度损失来源。

学完后你应该能够：

- 说清 per-token 与 per-block 在 SF 粒度、SF 数量、与硬件 GEMM 分块匹配度上的差异。
- 推导出 FP4→FP8 无损再分块的充要条件（2⁶ 余量与 Max/Min ≤ 2¹¹ 约束）。
- 解释 E5M6 的 12 位布局与 `8 个值 → 3 个 uint32` 的打包过程。
- 画出 cast_back 的数据流，并指出一次「量化→反量化」往返的精度损失来源。

## 2. 前置知识

本讲默认你已经掌握 u4-l1（低比特格式与配置体系）、u4-l2（per_token_cast 五段骨架与单段/两段规约）、u4-l3（SF 宏与幂次舍入）。这里只做最简回顾，并补三个本讲要用到的新概念。

### 2.1 回顾：量化主流程与 QuantTensor 约定

一次量化（cast）走五步：

\[
\text{load} \to \text{reduce(absmax)} \to \text{get\_sf\_and\_inv} \to \text{scale} \to \text{store}
\]

其中定标公式（u4-l3 的 `get_sf_and_inv`）为：

\[
\text{sf} = \frac{\text{clamp}(\text{amax})}{\text{max\_value}}, \qquad \text{sf\_inv} = \frac{1}{\text{sf}}
\]

量化值 = 原值 × sf_inv；反量化值 = 量化值 × sf。整个库用 `QuantTensor = (tensor, sf)` 二元组统一表达「带缩放因子的量化张量」，`sf` 在不同布局下可能是 float32 或打包的 UE8M0（u4-l3 已讲）。

### 2.2 回顾：三种低比特格式的「天花板」

| 格式 | 位宽 | 指数/尾数 | max_value | 容器 dtype |
|------|------|-----------|-----------|-----------|
| e4m3（FP8） | 8 | 3 / 3 | 448 | `float8_e4m3fn` |
| e2m1（FP4） | 4 | 2 / 1 | 6 | 两个打包进 `int8` |
| e5m6（自定义） | 12 | 5 / 6 | 65024 | 打包进 `uint32` |

记住这条规律：**指数位越多 → 表示范围越大；尾数位越多 → 表示精度越高**。这是本讲「FP4 能无损变 FP8」的根本原因。

### 2.3 新概念：SF 在「指数字段」上的运算

本讲的 lossless kernel 几乎所有 SF 运算都不在浮点域做，而是在 float32 的**原始指数字段**（8 bit）上做。一个正的 float32 可以写成：

\[
x = 2^{\,E - 127} \times (1.\text{mantissa})_2
\]

其中 \(E\) 是 8 位指数字段（存在 float32 的第 23–30 位）。当 SF 恰好是 2 的幂（u4-l3 的 `round_sf` 保证）时，它的尾数为 0，**整个 SF 就由指数字段 \(E\) 唯一决定**。于是：

- 「取 SF 的指数」：`reinterpret(sf, uint32) >> 23`，得到 \(E \in [0,255]\)。
- 「把指数 \(E\) 还原成 float32」：`reinterpret(uint32(E) << 23, float32)`，得到 \(2^{E-127}\)。
- 「两个 SF 相除」：等价于两个 \(E\) **相减**。

这两个位操作（`>> 23` 与 `<< 23`）会反复出现在 lossless 和 E5M6 的代码里，是本讲最重要的「速记符号」。

### 2.4 新概念：TileLang 的 `replicate` 布局注解

u2-l3 讲过 `alloc_reducer` 做跨线程归约。本讲 per_block_cast 用的是它的「近亲」：用 `alloc_fragment` + `T.annotate_layout(..., replicate=N, forward_fn=...)` 来表达「同一个逻辑张量被复制成 N 份，分散到线程上做部分累加，再用 `T.reduce_max(dim=...)` 合并」。看到 `replicate` 就想到「跨线程的部分归约缓冲」。

## 3. 本讲源码地图

本讲涉及的关键文件（都在 `tile_kernels/quant/` 下）：

| 文件 | 作用 |
|------|------|
| [per_block_cast_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py) | 二维分块量化 kernel + 三个 wrapper（默认 / 只算 SF / 用预算 SF 只 cast） |
| [per_block_cast_lossless_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_lossless_kernel.py) | FP4(E2M1) → FP8(E4M3) 无损再分块 kernel |
| [per_token_cast_to_e5m6_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py) | 量化到 12 位 E5M6 + 位打包 |
| [cast_back_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/cast_back_kernel.py) | 反量化（FP8/FP4 → BF16/FP32）kernel + 派发 wrapper |
| [cast_back_e5m6_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/cast_back_e5m6_kernel.py) | E5M6 的反量化 kernel（cast_back 经 `x_special_fmt='e5m6'` 派发到此） |
| [common.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py) | 共享基础设施：`get_sf_and_inv`、`load_sf`/`store_sf`/`transform_sf`、`get_sf_shape`、各种 Config（u4-l1/u4-l3 已讲） |
| [torch/cast.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/cast.py) | 纯 PyTorch 参考实现（`cast`、`cast_back`），测试时对拍用 |
| tests/quant/test_per_block_cast.py、test_per_block_cast_lossless.py、test_per_token_cast_to_e5m6.py、test_cast_back.py、test_cast_back_e5m6.py | 五个对应的测试文件，含正确性对拍与 benchmark |

> 提示：E5M6 的正向 kernel `per_token_cast_to_e5m6` 并没有在 [quant/__init__.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/__init__.py) 里单独导出，用户统一通过 `per_token_cast(x, 'e5m6', ...)` 进入（见 4.3）。这是本讲唯一一个「文件名不等于入口名」的算子，值得留意。

---

## 4. 核心概念与源码讲解

### 4.1 per_block_cast：二维分块的 per-block 量化

#### 4.1.1 概念说明

per_token_cast 的 SF 块形状是 `(1, num_per_channels)`：**一行一段**，每个 SF 只覆盖「1 个 token × num_per_channels 列」。SF 网格形状是 `(num_tokens, hidden/num_per_channels)`，沿 token 轴最细（每个 token 自己一组）。

per_block_cast 把 SF 块推广成真正的二维块 `(num_per_tokens, num_per_channels)`，其中 `num_per_tokens ∈ {32, 128}`。也就是说，**连续 num_per_tokens 行、num_per_channels 列共享一个 SF**。SF 网格形状变成 `(num_tokens/num_per_tokens, hidden/num_per_channels)`。

为什么要二维分块？

- **匹配硬件的分块缩放 GEMM**。Hopper/Blackwell 的 block-scaled 矩阵乘（如 MXFP、FP8 GEMM）按 2D 块（典型 128×128、32×32）读取缩放因子。per-block 的 SF 布局天然对齐这些 tile，省去运行时重组。
- **少存 SF**。同样的矩阵，per-block 的 SF 数量比 per-token 少 num_per_tokens 倍，访存与存储开销更低。
- **代价是精度**。一个 SF 覆盖更多元素，amax 被更大的离群点拉高，小值精度变差。所以 per-block 适合权重（分布平稳），per-token 更适合激活（每行差异大）。

一句话总结粒度差异：

| 维度 | per-token `(1, C)` | per-block `(M, C)`，M∈{32,128} |
|------|--------------------|--------------------------------|
| token 轴粒度 | 每 1 行一段 | 每 M 行一段（更粗） |
| channel 轴粒度 | 每 C 列一段 | 每 C 列一段（相同） |
| SF 总数 | `num_tokens × hidden/C` | `(num_tokens/M) × hidden/C` |
| 典型用途 | 激活 | 权重 / block-scaled GEMM |

#### 4.1.2 核心流程

per_block_cast 的 kernel 主流程与 per_token_cast 同构，仍是「load → reduce absmax → get_sf_and_inv → scale → store」。区别全在「怎么把一个 2D 瓦片里的 absmax 归约到一个 SF」。

```
对每个瓦片 (block_m × block_k)：
  1. T.copy 把全局 X 的一个 (block_m, block_k) 块读进 fragment
  2. amax 归约（这是 per-block 的关键）：
       a. 分配 amax_reducer：形状 (sf_rows, sf_cols, num_threads//num_sf_per_block)
          —— 第 3 维把「同一个 SF 块」拆给多个线程并行做部分 absmax
       b. T.clear(amax_reducer)
       c. for i,j in Parallel(block_m, block_k):
            amax_reducer[sf_row, sf_col, block_offset] = max(.., abs(x[i,j]))
          （block_offset 由布局函数 amax_forward_fn 决定，决定每个元素归哪个线程）
       d. T.reduce_max(amax_reducer, amax_fragment, dim=2)
          —— 把第 3 维（线程维）合并，得到每个 SF 块的最终 amax
  3. 对每个 SF 块：get_sf_and_inv(amax) → 算出 sf / sf_inv；store_sf 写回全局；缓存 sf 到 sf_fragment
  4. for i,j in Parallel(block_m, block_k):
       out[i,j] = x[i,j] * sf_fragment[i//num_per_tokens, j//num_per_channels]
```

这里的 `block_offset` 思路就是 u2-l3 讲过的「replicate + reduce」：把归约工作切成 num_threads//num_sf_per_block 份并行做，最后合并。一个瓦片里有几个 SF 块（`num_sf_per_block`），每个 SF 块就分到几个线程。

#### 4.1.3 源码精读

**kernel 构造与分块参数。** `get_per_block_cast_kernel` 用 `@tilelang.jit` 装饰，关掉 warp-specialized 与 data-race-check（因为这里手写了跨线程归约，竞态由设计保证）。分块由「256 线程 × 8192 元素/块」的硬预算驱动（[per_block_cast_kernel.py:11-41](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L11-L41)）：

```python
num_threads = 256
num_elements_per_block = 8192
num_vectorize = get_best_vectorize_size(in_config.dtype)
num_per_tokens, num_per_channels = out_config.sf_block
assert num_per_tokens in (32, 128) and num_per_channels in (32, 128)

block_k = max(num_per_channels, num_elements_per_block // num_per_tokens)
block_m = num_per_tokens
```

注意 `block_m = num_per_tokens`：一个瓦片恰好覆盖一个 SF 块的高，channel 方向则可能覆盖多个 SF 块（`num_sf_cols_per_block = block_k // num_per_channels`）。例如 `sf_block=(32,32)` 时 `block_k = max(32, 8192//32) = 256`，一个瓦片有 `32×8=8` 个 SF 块；`sf_block=(128,128)` 时 `block_k = max(128, 64) = 128`，一个瓦片只有 `1` 个 SF 块。

**absmax 归约（per-block 的核心）。** 这段用 `T.annotate_layout` 的 `replicate` 把瓦片元素分散到线程上做部分 absmax，再用 `reduce_max` 合并（[per_block_cast_kernel.py:60-84](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L60-L84)）：

```python
amax_reducer = T.alloc_fragment((num_sf_rows_per_block, num_sf_cols_per_block,
                                 num_threads // num_sf_per_block), in_config.dtype)
amax_fragment = T.alloc_fragment((num_sf_rows_per_block, num_sf_cols_per_block), T.float32)
T.annotate_layout({
    amax_reducer:  T.Fragment(..., replicate=1,            forward_fn=amax_forward_fn),
    amax_fragment: T.Fragment(..., replicate=num_threads // num_sf_per_block, forward_fn=amax_forward_fn),
})
T.clear(amax_reducer)
for i, j in T.Parallel(block_m, block_k):
    block_offset = (i % (num_threads * num_vectorize // block_k) * num_per_channels
                    + j % num_per_channels) // num_vectorize
    amax_reducer[i // num_per_tokens, j // num_per_channels, block_offset] = T.max(
        amax_reducer[i // num_per_tokens, j // num_per_channels, block_offset],
        T.abs(x_fragment[i, j]))
T.reduce_max(amax_reducer, amax_fragment, dim=2)
```

读法：`amax_reducer` 的第 3 维大小是 `num_threads // num_sf_per_block`，即「每个 SF 块分到多少线程」；`amax_fragment` 的 `replicate` 取同一个值，表示它被复制这么多份（每个线程持有一份部分结果），最后由 `reduce_max(..., dim=2)` 把这一维合并。`amax_forward_fn`（[per_block_cast_kernel.py:48-50](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L48-L50)）负责把瓦片内的逻辑坐标映射成「(线程号, 线程内槽位)」，使并行的 `T.max` 写入不撞车。

**定标与写出 SF。** 归约出 amax 后，对每个 SF 块调 `get_sf_and_inv`（u4-l3 的共享宏）算出 `sf`/`sf_inv`，`store_sf` 写回全局，并把 `sf` 缓存进 `sf_fragment` 供 scale 步使用（[per_block_cast_kernel.py:85-88](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L85-L88)）：

```python
for i, j in T.Parallel(num_sf_rows_per_block, num_sf_cols_per_block):
    sf_inv, sf = get_sf_and_inv(amax_fragment[i, j], out_config)
    store_sf(out_sf, sf_inv, pid_x * num_sf_rows_per_block + i,
             pid_y * num_sf_cols_per_block + j, out_config)
    sf_fragment[i, j] = sf
```

注意 `get_sf_and_inv` 返回的是 `(sf_inv, sf)`（顺序与 per_token_cast 一致），写全局的是 `sf_inv`，scale 用的是 `sf`——这和 u4-l3 的约定一致。

**cast_only 分支。** 当外部传入预算好的 SF 时（`sf_only=False, cast_only=True`），跳过 absmax 归约，直接 `load_sf` 读回并求倒数（[per_block_cast_kernel.py:89-95](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L89-L95)）：

```python
else:  # cast_only
    for i, j in T.Parallel(num_sf_rows_per_block, num_sf_cols_per_block):
        sf = load_sf(out_sf, pid_x * num_sf_rows_per_block + i,
                     pid_y * num_sf_cols_per_block + j, out_config)
        sf_fragment[i, j] = 1 / sf
if sf_only:
    T.thread_return()   # 只算 SF 的模式：算完 SF 就提前退出，不做 scale
```

**scale 与写出。** 与 per_token 完全同构，只是 SF 索引变成二维（[per_block_cast_kernel.py:129-130](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L129-L130)）：

```python
for i, j in T.Parallel(block_m, block_k):
    out[pid_x * block_m + i, pid_y * block_k + j] = (
        x_fragment[i, j] * sf_fragment[i // num_per_tokens, j // num_per_channels])
```

> 代码里 `if pid_x < ... - 1 and pid_y < ... - 1:` 与 `else:` 两个分支体完全相同（[per_block_cast_kernel.py:125-136](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L125-L136)）。注释说明这是有意为之：把边界判断提到顶层有利于 SASS 代码生成（非边界瓦片走无谓词的快路径）。这是「为编译器写源码」的典型技巧。

**wrapper：三种模式。** 与 per_token_cast 一样，per_block 也暴露三个入口，全部委托给同一个 `per_block_cast_impl`（[per_block_cast_kernel.py:141-202](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L141-L202)）。impl 的四步是 u2-l1 讲过的标准 wrapper 骨架：校验 → 配置 → 编译/缓存 kernel → 分配输出并启动。关键在它据参数切出三种行为：

| 入口 | sf_only | cast_only | 返回 |
|------|---------|-----------|------|
| [per_block_cast](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L205-L226) (默认) | False | False | `QuantTensor = (out, out_sf)` |
| [per_block_cast_with_sf_only](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L229-L250) | True | False | 只返回 `out_sf` |
| [per_block_cast_with_precomputed_sf](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L253-L276) | False | True（传 sf） | 只返回 cast 后的 `out` |

注意分配输出时 FP4 要除以 2（两个值打包进一个 int8）：`out = torch.empty((num_tokens, hidden if fmt=='e4m3' else hidden//2), ...)`（[per_block_cast_kernel.py:180](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_kernel.py#L180)）。

#### 4.1.4 代码实践

**实践目标**：亲手算出 per-block 的 SF 数量，对比 per-token，理解粒度差异。

**操作步骤**（源码阅读型，无需 GPU）：

1. 假设输入 `x.shape = (4096, 8192)`，目标格式 e4m3。
2. 对 per-token（`sf_block=(1,128)`）：用 [common.py 的 get_sf_shape](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L130-L138) 手算 SF 张量形状：`num_block_m = ceil(4096/1)=4096`，`num_block_k = ceil(8192/128)=64`，row-major 下为 `(4096, 64)`。
3. 对 per-block（`sf_block=(128,128)`）：`num_block_m = ceil(4096/128)=32`，`num_block_k = ceil(8192/128)=64`，形状 `(32, 64)`。
4. 计算两者 SF 元素数之比：`4096×64` vs `32×64`，per-token 是 per-block 的 128 倍。

**需要观察的现象**：per-block 把 token 轴的 SF 数量砍掉了 `num_per_tokens`（=128）倍，channel 轴不变。这正是「per-block 省 SF 存储」的量化体现。

**预期结果**：per-token SF 数 = 262144；per-block（128,128）SF 数 = 2048；比值 = 128。

> 如有 GPU，可进一步运行 [tests/quant/test_per_block_cast.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_per_block_cast.py) 的正确性用例，里面会用 `tile_kernels.torch.cast` 对拍、并用 `cast_back` 做往返、最后 `check_bias` 检查统计无偏。运行命令与结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：给定 `sf_block=(32,32)`、`hidden=8192`，求 per_block_cast kernel 的 `block_k`、`block_m`、`num_sf_per_block`。

**答案**：`block_k = max(32, 8192//32) = 256`；`block_m = 32`；`num_sf_rows_per_block = 32//32 = 1`；`num_sf_cols_per_block = 256//32 = 8`；`num_sf_per_block = 1×8 = 8`。所以一个瓦片有 8 个 SF 块，每个 SF 块分到 `256//8 = 32` 个线程做部分 absmax。

**练习 2**：为什么 per_block_cast 在 `if pid_x < ...-1 and pid_y < ...-1` 边界分支里写了一段与主体完全相同的代码？

**答案**：这是为 SASS 代码生成做的优化——把边界判断提到顶层，让非边界的绝大多数瓦片走「无谓词 load/store」的快路径，避免每个元素都带边界谓词。逻辑上两段等价，收益在编译产物层面。

---

### 4.2 per_block_cast_lossless：FP4→FP8 的无损再分块

#### 4.2.1 概念说明

这个 kernel 解决一个很特别的需求：我手上已经有一个**量化好的 FP4（E2M1）张量**（带 SF），现在想把它**重新分块**成更大块（如 `1×32 → 128×128`）的 **FP8（E4M3）**，而且要求**信息一点不丢**（lossless）。

为什么可能无损？因为 E4M3 在「范围」和「精度」两维都**严格优于** E2M1：

\[
\text{E2M1 max} = 2^2 \times 1.5 = 6, \quad \text{E4M3 max} = 448; \qquad
\text{E2M1 尾数 1 位} < \text{E4M3 尾数 3 位}
\]

任何 E2M1 能表示的值（乘上它的 SF 后），E4M3 都能**精确**表示。难点不在数据本身，而在 **SF 的合并**：把多个细粒度的输入 SF 合并成一个粗粒度的输出 SF 时，要保证每个元素相对新 SF 的 rescale 因子仍落在 E4M3 的正常表示范围内。

kernel 顶部的注释把这条数学约束写得很清楚（[per_block_cast_lossless_kernel.py:21-31](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_lossless_kernel.py#L21-L31)）。下面我们把它推导清楚。

#### 4.2.2 核心流程

设输入 SF（forward scale，2 的幂）的真指数为 \(e_\text{in}\)，合并后的输出 SF 真指数为 \(e_\text{out}\)。每个元素的 E4M3 量化值需要是：

\[
q_{e4m3} = v_{e2m1} \times \frac{\text{sf}_\text{in}}{\text{sf}_\text{out}} = v_{e2m1} \times 2^{\,e_\text{in} - e_\text{out}}
\]

要让结果无损，这个 \(q_{e4m3}\) 必须落在 E4M3 的正常范围内（min 正常数 \(2^{-6}\)，max 448），于是得到两条约束：

**不溢出（上界）**：最坏情况是最大元素 \(v_{e2m1}=6\) 配上块内最大 SF \(e_\text{in}=\max e_\text{in}\)。若取 \(e_\text{out} = \max e_\text{in} - 6\)，则

\[
q_{e4m3}^{\max} = 6 \times 2^{6} = 384 \le 448 \;\checkmark
\]

之所以除以 \(2^6\) 而不是更多，是因为 \(6 \times 2^6 = 384 < 448 < 6 \times 2^7 = 768\)——2⁶ 是不溢出前提下能保留的最大动态范围。

**不损失精度（下界）**：最小有效元素 \(v_{e2m1}=2^{-1}\) 配上块内最小 SF \(e_\text{in}=\min e_\text{in}\)，要求 rescale 后 ≥ E4M3 min 正常 \(2^{-6}\)：

\[
2^{-1} \times 2^{\,\min e_\text{in} - e_\text{out}} \ge 2^{-6}
\;\Longrightarrow\;
e_\text{out} \le \min e_\text{in} + 5
\]

代入 \(e_\text{out} = \max e_\text{in} - 6\)，得到 kernel 那条断言的来源：

\[
\max e_\text{in} - 6 \le \min e_\text{in} + 5
\;\Longleftrightarrow\;
\frac{\text{sf}_\text{in,max}}{\text{sf}_\text{in,min}} \le 2^{11}
\]

也就是说：**只要块内最大/最小输入 SF 之比不超过 2¹¹，FP4→FP8 再分块就严格无损**。这就是 kernel 里 `T.device_assert(x_sf_uint32 + 5 >= out_sf_uint32)`（[per_block_cast_lossless_kernel.py:128](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_lossless_kernel.py#L128)）的含义。

有了数学，流程就很直观了。关键：**所有 SF 运算都在原始指数字段（uint32）上做**，因为 SF 都是 2 的幂，指数加减就等于 SF 乘除：

```
对每个输出瓦片 (block_m × block_k)：
  1. 读输入 FP4 到 shared，读输入 SF 到 fragment
  2. transform_sf_to_uint32：把每个输入 SF 转成原始指数 E（fp32: >>23；ue8m0: 直接用）
  3. 把输入 SF 指数 reshape 到「每个输出 SF 块内含若干输入 SF」的三维布局
  4. reduce_max(dim=输入SF维)：输出 SF 指数 = 块内输入 SF 指数的最大值
  5. 输出 SF 指数 -= 6（饱和减，≥0）—— 即 e_out = max e_in − 6
  6. 对每个输入 SF：调整指数 = e_in − e_out + 127；device_assert(e_in + 5 ≥ e_out)
  7. 对每个元素：q_e4m3 = fp4_value × 2^(e_in − e_out)，cast 写出
  8. 把输出 SF（指数形式）store_sf 回全局
```

#### 4.2.3 源码精读

**前置约束。** kernel 一开头用一连串 `assert` 钉死使用条件（[per_block_cast_lossless_kernel.py:35-40](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_lossless_kernel.py#L35-L40)）：只支持 E2M1→E4M3、输入必须带 SF、块宽必须是 2 的幂、输出块必须是输入块的整数倍。这些正是上面数学推导的前提。

**两个 SF 位操作宏。** 这是 2.3 节那两个「速记符号」的具象化（[per_block_cast_lossless_kernel.py:62-71](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_lossless_kernel.py#L62-L71)）：

```python
@T.macro
def transform_sf_to_uint32(sf, sf_dtype):
    if sf_dtype == T.float32:
        return T.reinterpret(sf, T.uint32) >> 23   # 取原始指数 E
    return T.uint32(sf)                             # ue8m0 本身就是 E

@T.macro
def transform_sf_to_fp32(sf):
    return T.reinterpret(T.uint32(sf) << 23, T.float32)  # E 还原成 2^(E-127)
```

**第 3–5 步：合并输入 SF 得到输出 SF 指数。** 先把输入 SF 指数按「输出块布局」reshape 成三维 `(num_out_sf_m, num_out_sf_k, num_in_sf_per_out_sf)`，第 3 维是「落在这个输出 SF 块里的那些输入 SF」；然后 `reduce_max(dim=2)` 取它们指数的最大值（[per_block_cast_lossless_kernel.py:96-114](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_lossless_kernel.py#L96-L114)）：

```python
T.reduce_max(x_sf_uint32_fragment_reshaped, out_sf_uint32_fragment, dim=2)
```

接着做「除以 2⁶」——在指数域就是减 6，并用饱和减防止下溢（[per_block_cast_lossless_kernel.py:117-119](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_lossless_kernel.py#L117-L119)）：

```python
out_sf_uint32_fragment[i, j] = T.if_then_else(
    out_sf_uint32_fragment[i, j] >= 6,
    out_sf_uint32_fragment[i, j] - 6, 0)
```

**第 6–7 步：算每个元素的 rescale 指数并应用。** 调整指数 `e_in − e_out + 127`（+127 是为后续 `<<23` 还原准备的偏置），并用 `device_assert` 守住无损下界；随后每个元素乘上还原出的 rescale 因子并 cast 成 E4M3（[per_block_cast_lossless_kernel.py:122-137](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_lossless_kernel.py#L122-L137)）：

```python
T.device_assert(x_sf_uint32_fragment[i, j] + 5 >= out_sf_uint32_fragment[out_m, out_k])
x_sf_uint32_fragment[i, j] = (x_sf_uint32_fragment[i, j]
                              - out_sf_uint32_fragment[out_m, out_k] + 127)
...
sf = transform_sf_to_fp32(x_sf_uint32_fragment[m_idx_2, k_idx_2])   # = 2^(e_in − e_out)
x_out_fragment[i, j] = T.cast(T.float32(x_in_shared[i, j]) * sf, out_config.dtype)
```

注意这里的 `+127` 与 `transform_sf_to_fp32` 里的 `<<23 → 2^(E-127)` 恰好抵消，净效果就是乘以 \(2^{e_\text{in}-e_\text{out}}\)——和我们 4.2.2 的推导完全一致。

**第 8 步与边界保护。** 写出输出 SF 时同样按布局分发（ue8m0 存 uint8，否则还原 float32）。整个过程里反复出现的 `if i*sf_block + pid*block < num_tokens and ... < hidden` 守卫（如 [L109](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_lossless_kernel.py#L109)、[L126](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_lossless_kernel.py#L126)）是为了在张量尺寸不是块整数倍时，不让越界的「幽灵 SF」污染结果。

**wrapper。** [per_block_cast_lossless](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_block_cast_lossless_kernel.py#L154-L209) 接收一个 `QuantTensor`（E2M1 数据 + SF）、输入块大小、输出块大小，返回新的 `(out, out_sf)`。它用 `get_logical_hidden` 把打包的 int8 物理形状还原成逻辑 hidden（FP4 两个值打包成一个 int8，逻辑维度是物理的 2 倍）。

#### 4.2.4 代码实践

**实践目标**：验证「块内 SF 比值 ≤ 2¹¹」这条无损前提，理解测试为什么故意构造数据。

**操作步骤**（源码阅读型）：

1. 阅读 [tests/quant/test_per_block_cast_lossless.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_per_block_cast_lossless.py) 的 `clamp_abs_ratio`（[L15-20](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_per_block_cast_lossless.py#L15-L20)）：它把输入张量的绝对值限制在 `max_abs / 2^9` 以上。
2. 思考：输入被 `cast` 成 E2M1 后，SF 由 absmax 决定。`clamp_abs_ratio` 把 absmax 的比值限制在 2⁹ 以内，远小于 2¹¹，于是无损前提必然成立。
3. 阅读断言：测试把 FP4 和「再分块后的 FP8」都 `cast_back` 到 fp32，用 `assert_equal`（位精确）比较（[L94-96](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_per_block_cast_lossless.py#L94-L96)）——注意这里用的是 **`assert_equal` 而不是 `calc_diff`**，正说明「无损」是位级别的精确相等。

**需要观察的现象**：测试用位精确断言验证无损；它必须靠 `clamp_abs_ratio` 主动压窄动态范围来满足 2¹¹ 约束，否则随机数据的离群点会打破无损条件、触发 kernel 内的 `device_assert`。

**预期结果**：在 `clamp_abs_ratio(max_ratio=2**9)` 下，`cast_back(fp4) == cast_back(lossless_reblock(fp4))` 逐位成立。「待本地验证」实际运行结果。

#### 4.2.5 小练习与答案

**练习 1**：如果把「除以 2⁶」改成「除以 2⁷」，会发生什么？

**答案**：上界会更安全（\(6 \times 2^{5}=192 \ll 448\)），但下界变紧：要求 \(\max e_\text{in} - 7 \le \min e_\text{in} + 5\)，即 SF 比值 ≤ 2¹² 才不进 subnormal——等一下，这样下界反而更宽松了吗？不：除得越多，\(e_\text{out}\) 越小，下界 \(e_\text{out} \le \min e_\text{in}+5\) 越容易满足，但上界余量被浪费，整体动态范围利用率下降。kernel 选 2⁶ 是「不溢出前提下余量最小、动态范围最大」的临界点。

**练习 2**：为什么这个 kernel 全程在 uint32 指数字段上做 SF 运算，而不是直接用浮点乘除？

**答案**：因为 SF 都是 2 的幂（u4-l3 的 round_sf 保证），其浮点值的尾数全 0，全部信息集中在 8 位指数上。在指数字段上做整数加减，等于精确的 SF 乘除，既无舍入误差（保证无损），又比浮点运算便宜。这也是 UE8M0 只存 1 字节指数的底层逻辑。

---

### 4.3 per_token_cast_to_e5m6：12 位 E5M6 与位打包

#### 4.3.1 概念说明

E5M6 是一个**自定义的 12 位浮点格式**：1 位符号 + 5 位指数 + 6 位尾数。它其实就是 **IEEE fp16（E5M10）的高 12 位**——直接砍掉 fp16 尾数的低 4 位。所以：

- 表示范围与 fp16 相同：max = \(2^{15}\times(1+63/64) = 65024\)，min 正常 = \(2^{-14}\)。
- 精度介于 fp16（10 位尾数）和 bf16（7 位尾数）之间：6 位尾数。
- 物理存储：**8 个 E5M6 值 = 8 × 12 = 96 bit = 3 × uint32**，紧凑打包。

为什么需要 E5M6？它是一种「比 FP8 精、比 fp16 省」的中间表示，适合对精度敏感但又想省带宽的环节。注意它是「截断的 fp16」，所以特殊值（subnormal、max、min）处理必须和 fp16 的位定义严格对齐。

入口方面，如源码地图所述，E5M6 正向 kernel 不在 `__init__.py` 单独导出，而是由 [per_token_cast 在 `fmt=='e5m6'` 时派发](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L170-L175)过来：

```python
assert fmt in ('e5m6', 'e4m3', 'e2m1')
if fmt == 'e5m6':
    assert x_block_size is None
    assert not sf_only
    assert sf is None
    return per_token_cast_to_e5m6(x, num_per_channels, ...)
```

所以用户调用 `tile_kernels.quant.per_token_cast(x, 'e5m6', num_per_channels=hidden)` 即可。它的 SF 是真正的 per-token（`sf_block=(1, hidden)`，`num_per_channels` 必须等于 `hidden`）。

#### 4.3.2 核心流程

E5M6 的量化主流程和 per_token_cast 几乎一样（load → reduce absmax → 定标 → scale → store），只有两处特殊：

1. **定标用专属宏 `get_sf_and_inv_e5m6`**：因为 E5M6 不是 TileLang 内建 dtype，没有 `T.max_value(dtype)` 可用，所以 max_value 硬编码为 65024。
2. **store 之前多了一步位打包 `float_to_e5m6`**：把每 8 个 float32 值打包成 3 个 uint32。

```
对每个 token（block_m × block_k 瓦片，block_k = align(hidden, 128*8)）：
  1. 读输入到 fragment
  2. reshape 成 [block_m, num_groups, num_per_channels]，reduce_absmax(dim=2) 得每组 amax
  3. get_sf_and_inv_e5m6(amax)：sf = amax/65024，算 sf_inv；store_sf 写回
  4. scale：out_fragment[i,j] = x[i,j] * sf_inv[i, j//num_per_channels]
  5. 位打包：每 8 个相邻元素一组 → float_to_e5m6 → 3 个 uint32 → 写回 out
```

#### 4.3.3 源码精读

**定标宏 `get_sf_and_inv_e5m6`。** 它是 u4-l3 `get_sf_and_inv` 的 E5M6 专版，唯一差别是 `max_value = 65024` 写死，`clamp_min_value` 由 wrapper 传入 1e-4（[per_token_cast_to_e5m6_kernel.py:10-30](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py#L10-L30)）：

```python
@T.macro
def get_sf_and_inv_e5m6(amax, out_config):
    clamped_amax = T.max(amax, out_config.clamp_min_value)
    max_value = 65024
    sf = clamped_amax / max_value
    if not out_config.round_sf:
        return sf, max_value / clamped_amax
    # round_sf 分支：同样的 (bits-1)>>23+1 位操作把 sf 舍入到 2 的幂
    ...
```

> 小注：源码里有一行 `sf = T.alloc_var(T.float32)` 紧接着被 `sf = clamped_amax / max_value` 覆盖，前一行实际是死代码，功能上等价于直接 `sf = clamped_amax / max_value`。阅读时以赋值结果为准。

**位打包宏 `float_to_e5m6`。** 这是本模块最精巧的部分：输入 8 个 float32，输出 3 个 uint32（[per_token_cast_to_e5m6_kernel.py:33-64](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py#L33-L64)）。每个值的打包分三步：

```python
for i in T.unroll(8):
    value_half = T.call_extern(T.float16, '__float2half_rz', x[i])  # 先转 fp16（向零舍入）
    half_u16[i] = T.reinterpret(value_half, T.uint16)
    value_u32 = T.reinterpret(x[i], T.uint32)
    remain_bits = value_u32 & kCutBits           # 被砍掉的低位（用于舍入判定）
    half_u16[i] = half_u16[i] >> 4               # 砍掉 fp16 尾数低 4 位 → 12 位
    cond = ((half_u16[i] & 1) + remain_bits > kThreshold)  # RTNE：四舍五入到偶数
    half_u16[i] = half_u16[i] + T.cast(cond, T.uint16)
```

读法：先调设备函数 `__float2half_rz` 把 float32 转 fp16（向零舍入），再 `>> 4` 砍掉尾数低 4 位得到 12 位 E5M6。砍之前用 `kCutBits`（低 17 位掩码）和 `kThreshold`（2¹⁶）做一次 RTNE（round-to-nearest-even）修正，保证截断误差无偏。最后 8 个 12 位值用移位与或拼进 3 个 uint32（[L53-64](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py#L53-L64)），拼接顺序与反量化 kernel `e5m6_to_float` 严格互逆。

**kernel 主体。** [per_token_cast_to_e5m6_kernel.py:108-154](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py#L108-L154) 是定标 + scale + 打包的完整循环。注意分块约束 `block_k = align(hidden, num_threads * 8)`——必须按 8 对齐，因为打包以 8 个值为单位；scale 后的 `out_fragment` 用 `out_forward_fn` 注解布局，保证连续 8 个元素落在同一线程便于打包（[L103-105](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py#L103-L105)、[L149-154](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py#L149-L154)）：

```python
for x, y in T.Parallel(block_m, block_k // 8):
    for j in T.serial(8):
        in_local[j] = out_fragment[x, y * 8 + j]
    float_to_e5m6(in_local, out_local)
    for j in T.serial(3):
        out[pid_token*block_m + x, pid_hidden*(block_k//8*3) + y*3 + j] = out_local[j]
```

**wrapper 与输出形状。** [per_token_cast_to_e5m6](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py#L159-L215) 分配 `(num_tokens, hidden//8*3)` 的 uint32 输出，kernel 跑完后再 `out.view(torch.uint8)`——所以最终物理形状是 `(num_tokens, hidden*3//2)` 的 uint8（每个值 12 bit = 1.5 字节）。`get_cast_output_config('e5m6', (1, num_per_channels), ..., 1e-4)` 把 clamp 下限显式设为 1e-4（[L189](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py#L189)）。

#### 4.3.4 代码实践

**实践目标**：理解 E5M6 对浮点特殊值的处理，看懂测试为何单独构造 subnormal。

**操作步骤**（源码阅读型）：

1. 阅读 [tile_kernels/testing/generator.py 的 `_E5M6_SPECIAL_VALUES`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L76-L80)，三个特殊值是：`2^-20`（min subnormal）、`2^-14 × 63/64`（max subnormal）、`2^-14`（min normal）。
2. 阅读 [tests/quant/test_per_token_cast_to_e5m6.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_per_token_cast_to_e5m6.py#L65-L83)：它对每个特殊值张量（最后一列还塞了 `65024.0` 即 max）跑 kernel，与 `tile_kernels.torch.cast_to_e5m6` 参考用 `assert_equal` 位精确对拍。
3. 思考：为什么 subnormal 必须单独测？因为 `__float2half_rz` 在指数 < fp16 正常下限时走 subnormal 路径，`>> 4` 截断 + RTNE 的行为在 subnormal 区最容易出错。

**需要观察的现象**：特殊值用例的存在说明打包宏对 fp16 的正常/subnormal 边界很敏感；`float_to_e5m6` 的 RTNE 修正（`remain_bits`、`kThreshold`）正是为了在这些边界上保持精度。

**预期结果**：对三个特殊值，kernel 输出与 torch 参考位精确一致。「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：E5M6 的 max 为什么是 65024 而不是 fp16 的 65504？

**答案**：E5M6 只有 6 位尾数，max normal = \(2^{15}\times(1+63/64) = 32768 \times 1.984375 = 65024\)。fp16 有 10 位尾数，max = \(2^{15}\times(1+1023/1024)=65504\)。砍掉 4 位尾数让最大可表示值略降。

**练习 2**：为什么 `block_k` 必须按 8 对齐（`align(hidden, num_threads*8)`）？

**答案**：因为位打包以「8 个值 → 3 个 uint32」为最小单位。若一个线程组分到的元素不是 8 的倍数，`float_to_e5m6` 的 `T.unroll(8)` 循环就会越界或打包错位。按 8 对齐保证每组恰好 8 个连续元素。

---

### 4.4 cast_back：反量化的流程与精度损失

#### 4.4.1 概念说明

cast_back 是量化的逆操作：把 `QuantTensor = (data, sf)` **反量化**回高精度（BF16 或 FP32）。数学上极其简单——把每个量化值乘回它所属块的 SF：

\[
\hat{x}_{i,j} = q_{i,j} \times \text{sf}[\,i // M,\; j // C\,]
\]

但「反量化值 \(\hat{x}\)」并不等于原始值 \(x\)——**精度损失发生在正向量化那一刻**（把 \(x\) 舍入到 e4m3/e2m1/E5M6 的离散网格），cast_back 只是忠实地把舍入后的值放大回来。如果正向还开了 `round_sf`（SF 舍入到 2 的幂），SF 本身也带来额外误差。

cast_back 的工程价值不在数学，而在**高效的分块访存**：它要同时读「紧凑的量化数据」和「按块共享的 SF」，把 SF 广播到每个元素上相乘，再写出高精度结果——典型的带宽受限 kernel。

#### 4.4.2 核心流程

```
对每个瓦片 (TILE_M × TILE_K)：
  1. 读量化数据 x 到 shared（disable_tma，走向量化 load-store）
  2. 读 SF：对每个 SF 块 load_sf + transform_sf（ue8m0 还原成 float32）→ sf_shared
  3. 广播相乘：out_fragment[i,j] = x_shared[i,j] * sf_shared[i//M, j//C]
  4. 写出 out_fragment 到全局（高精度输出）
```

关键在分块参数 `TILE_M/TILE_K` 的选择，它按「per-token 还是 per-block」分两条路径，目的是让 SF 广播的访存模式尽量连续。

#### 4.4.3 源码精读

**分块策略：两条路径。** [get_cast_back_kernel](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/cast_back_kernel.py#L19-L49) 据 `num_per_tokens` 选瓦片（[L28-43](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/cast_back_kernel.py#L28-L43)）：

```python
if num_per_tokens == 1:                       # per-token：每行一段
    TILE_K = math.gcd(hidden, num_elems_per_block)
    if hidden <= num_elems_per_block:         # 小 hidden：用 replication 优化
        TILE_K = align(hidden, num_threads * (2 if in_config.dtype==T.float4_e2m1fn else 1))
    TILE_M = num_elems_per_block // TILE_K
    if TILE_M <= 3:                           # TILE_M 太小时强制 1，避免谓词 load
        TILE_M = 1
else:                                          # per-block：固定 128×64
    TILE_M, TILE_K = 128, 64
```

读法：per-token 时 SF 沿行变化，所以瓦片尽量「宽而扁」（大 TILE_K、适当 TILE_M），让一行内的 SF 复用最大化；FP4 还要按 2 对齐（两个值打包进一个字节）。per-block 直接用 128×64 匹配常见 block-scaled GEMM tile。

**kernel 主体。** [cast_back_kernel.py:51-72](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/cast_back_kernel.py#L51-L72) 是「读数据 → 读 SF → 广播乘 → 写出」四步。SF 的读取与还原是重点（[L62-67](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/cast_back_kernel.py#L62-L67)）：

```python
T.copy(x[pid_token*TILE_M, pid_hidden*TILE_K], x_shared, disable_tma=True)
for i, j in T.Parallel(T.ceildiv(TILE_M, num_per_tokens), T.ceildiv(TILE_K, num_per_channels)):
    token_index   = pid_token*TILE_M // num_per_tokens + i
    channel_index = pid_hidden*TILE_K // num_per_channels + j
    sf = load_sf(x_sf, token_index, channel_index, in_config)   # 按布局分发读取
    sf_shared[i, j] = transform_sf(sf, in_config)               # ue8m0 → float32
for i, j in T.Parallel(TILE_M, TILE_K):
    out_fragment[i, j] = x_shared[i, j] * sf_shared[i // num_per_tokens, j // num_per_channels]
```

`load_sf` 和 `transform_sf` 都是 u4-l3 讲过的共享宏：前者按 row-major / col-major / packed-ue8m0 三分支寻址，后者把 UE8M0 的 1 字节指数还原成 float32（`uint32(sf)<<23` reinterpret）。

**wrapper 与派发。** [cast_back](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/cast_back_kernel.py#L77-L123) 是总入口，它还承担一个派发职责——遇到 E5M6 打包格式时转交给专用 kernel（[L99-103](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/cast_back_kernel.py#L99-L103)）：

```python
if x_special_fmt == 'e5m6':
    assert x.dtype == torch.uint8
    return cast_back_e5m6((x, x_sf), fmt, x_block_size)
```

为什么 E5M6 要单独一个 kernel？因为它的数据是「3 个 uint32 解包成 8 个 float32」（`e5m6_to_float`，[cast_back_e5m6_kernel.py:12-40](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/cast_back_e5m6_kernel.py#L12-L40)），与 FP8/FP4 的「直接 cast」完全不同，必须走 `float_to_e5m6` 的严格逆运算。其余流程（读 SF、广播乘、写出）与主 kernel 一致。

便利入口 [per_token_cast_back](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/cast_back_kernel.py#L126-L143) 只是 `cast_back(x, fmt, (1, num_per_channels), ...)` 的语法糖。

#### 4.4.4 代码实践

**实践目标**：亲手做一次「量化 → 反量化」往返，计算相对误差并定位损失来源。

**操作步骤**（需 GPU；若无 GPU 则按「源码阅读型」跟踪数据流）：

1. 构造输入 `x = torch.randn((512, 4096), dtype=torch.bfloat16, device='cuda')`。
2. 正向：`x_q, sf = tile_kernels.quant.per_token_cast(x, 'e4m3', num_per_channels=128, round_sf=True)`。
3. 反向：`x_hat = tile_kernels.quant.per_token_cast_back((x_q, sf), 'bf16', num_per_channels=128)`。
4. 计算相对误差：用 [tile_kernels/testing/numeric.py 的 `calc_diff`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/numeric.py#L26-L30)，`diff = calc_diff(x, x_hat)`。也可直接看 `(x - x_hat).abs().max()`。
5. 改参数对比：分别试 `round_sf=True/False`、`num_per_channels=128` vs `4096`（整行一个 SF）、`fmt='e2m1'` vs `'e4m3'`，观察 `diff` 变化。

**需要观察的现象**（参考 [tests/quant/test_cast_back.py:109](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_cast_back.py#L109) 的阈值）：

- e4m3 的 `calc_diff` 应 < 1e-3；e2m1 应 < 2e-2（FP4 精度更差）。
- `round_sf=True` 比 `False` 误差略大（SF 被舍入到 2 的幂）。
- `num_per_channels` 越小（SF 越细），误差越小。

**精度损失来源分析**（这是本实践的要点）：

1. **量化网格舍入（主因）**：正向把 x 舍入到 e4m3/e2m1 的离散值，尾数位越少误差越大（e2m1 > e4m3）。这一步不可逆，cast_back 无法恢复。
2. **SF 舍入（次因）**：`round_sf=True` 时 SF 被舍入到 2 的幂，定标不精确。
3. **SF 粒度**：块越大（num_per_channels 越大），一个 SF 要兼顾的动态范围越大，amax 被离群点拉高，小值相对误差变大。
4. **cast_back 本身**：乘法是精确的，不引入新误差（除非输出 dtype 再截断，如回到 bf16）。

**预期结果**：e4m3 + round_sf + num_per_channels=128 时 `diff` 约 1e-4 量级；e2m1 约 1e-2 量级。具体数值「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：cast_back 的 per-token 分支里，为什么 `TILE_M <= 3` 时要强制 `TILE_M = 1`？

**答案**：TILE_M 太小意味着每个瓦片要处理多行但每行元素少，边界谓词 load 的开销占比过高。强制 `TILE_M=1`（一行一个瓦片、TILE_K 取满）让 load 都是整行连续读，避免谓词化，对带宽受限的反量化更划算。注释原文：「avoid predicated loads for better performance」。

**练习 2**：为什么 E5M6 的反量化不能复用主 `cast_back_kernel`，而要单独的 `cast_back_e5m6_kernel`？

**答案**：FP8/FP4 的反量化是「直接把量化值 cast 成 float32 再乘 SF」；E5M6 的数据是 3 个 uint32 打包 8 个值，必须先用 `e5m6_to_float`（`float_to_e5m6` 的逆运算）解包成 8 个 float32，才能乘 SF。解包逻辑与普通 cast 不兼容，所以单独成 kernel，由 `cast_back` 经 `x_special_fmt='e5m6'` 派发。

---

## 5. 综合实践

把本讲四个变体串起来，做一个「**量化格式转换链 + 往返误差分析**」的小任务。

**场景**：你有一份 BF16 激活 `x`，要比较三种存储方案的往返精度与 SF 开销。

**任务**：

1. 对同一份 `x`（如 `(1024, 4096)` bf16），分别用三种方式量化并反量化：
   - **A. per-token e4m3**：`per_token_cast(x,'e4m3',128)` → `per_token_cast_back(...,'bf16',128)`。
   - **B. per-block e4m3**：`per_block_cast(x,'e4m3',(128,128))` → `cast_back((q,sf),'bf16',(128,128))`。
   - **C. per-token E5M6**：`per_token_cast(x,'e5m6',4096)` → `cast_back((q,sf),'bf16',(1,4096),x_special_fmt='e5m6')`。
2. 对每种方案，用 `calc_diff(x, x_hat)` 算往返误差，并统计各自的 SF 张量元素数（用 `get_sf_shape` 或直接看 `.shape`）。
3. 把结果填入下表，分析「精度 vs 带宽（数据大小 + SF 大小）」的权衡：

| 方案 | 数据位/元素 | SF 元素数 | calc_diff | 备注 |
|------|-------------|-----------|-----------|------|
| A: per-token e4m3 | 8 | ? | ? | 基线 |
| B: per-block e4m3 | 8 | ? | ? | SF 更省，精度略降 |
| C: per-token E5M6 | 12 | ? | ? | 精度最高，带宽居中 |

**进阶思考**：

- 哪种方案最适合「权重存储」？哪种最适合「激活」？结合 per-token/per-block 的粒度差异说明。
- 如果你拿到的是一份已经量化好的 FP4 权重，想换成更大块的 FP8 喂给 GEMM，应该用本讲的哪个算子？它的无损前提是什么？如何用 `clamp_abs_ratio` 保证前提成立？

> 这个综合任务把「per-block 粒度」「E5M6 打包」「cast_back 往返」「lossless 再分块」四个模块串成一条真实的数据管线。运行结果「待本地验证」，但表格的结构与权衡分析不依赖具体数值。

## 6. 本讲小结

- **per_block_cast** 把 SF 从「每行一段」推广到「二维 (M,C) 块」（M∈{32,128}），用 `replicate + reduce_max` 的跨线程 absmax 归约适配 2D 分块；SF 数量比 per-token 少 M 倍，更贴合 block-scaled GEMM，但精度略降。
- **per_block_cast_lossless** 利用「E4M3 严格优于 E2M1」做到 FP4→FP8 无损再分块；全部 SF 运算在原始指数字段上做（`>>23`/`<<23`），无损前提是「块内 SF 比值 ≤ 2¹¹」与「输出 SF = max(输入 SF)/2⁶」。
- **per_token_cast_to_e5m6** 量化到自定义 12 位格式（fp16 高 12 位），用 `float_to_e5m6` 把 8 个值打包成 3 个 uint32，并靠 RTNE 截断处理 fp16 subnormal 等特殊值；入口经 `per_token_cast(x,'e5m6',...)` 派发。
- **cast_back** 反量化 = 「按块广播 SF 相乘」，按 per-token/per-block 选不同瓦片，E5M6 经 `x_special_fmt='e5m6'` 派发到专用解包 kernel。
- **精度损失**主要来自正向量化（网格舍入 + SF 舍入 + SF 粒度），cast_back 本身是精确的；e4m3 往返误差约 1e-3，e2m1 约 2e-2。
- 贯穿全讲的两条线索：**SF 都是 2 的幂 → 在指数字段上做整数运算**；**粒度（per-token vs per-block）是精度与带宽/SF 开销的主要旋钮**。

## 7. 下一步学习建议

- **横向打通「融合量化」**：本讲都是独立的 cast 算子。u4-l5 会讲 `swiglu_forward_and_per_token_cast` 等**融合算子**——把 SwiGLU 激活与量化缝在一个 kernel 里，省掉一次全局存储往返。学完本讲再去看融合版，会发现五段骨架完全一致，只是 load 的输入变成了 SwiGLU 的中间结果。
- **深入建模层**：本讲的 kernel 都是「无梯度的纯前向」。如果你关心训练，跳到 u8-l1 看 `torch.autograd.Function` 如何把底层 kernel 封装成可求导层（以 EngramGate 为例）。
- **硬件感知调优**：本讲多次出现 `get_best_vectorize_size`、`disable_tma`、`replicate` 布局。u10-l1/u10-l2 会集中讲 SM 数、共享内存、TMA、向量化等硬件感知调优手段，届时可以回头重新审视 per_block_cast 的 `replicate` 归约和 E5M6 的 `out_forward_fn` 布局为何这样设计。
- **补全量化家族**：本讲没展开 `per_channel_cast`（沿 channel 维 per-token 的变体）和 `per_channel_cast_and_transpose`（转置+量化融合），它们在 u4-l5 与融合算子一起讲。如果你在做权重量化，值得顺带读 [per_channel_cast_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_channel_cast_kernel.py)。
