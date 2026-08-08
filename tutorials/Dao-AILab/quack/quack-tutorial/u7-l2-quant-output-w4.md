# 量化 GEMM 输出与 W4 权重

## 1. 本讲目标

本讲解决两个互相关联的问题：**「GEMM 怎么在出口直接产出量化张量」**，以及 **「怎么把 4-bit 量化权重喂进 GEMM」**。

读完本讲你应当能够：

- 说清 `BlockScaleFactorStore` 这个 EpiOp 如何在 epilogue 的 store 阶段「就地」生成 scale factor（SF）并改写累加器，让普通 `f32→d_dtype` 转换自动写出量化值。
- 区分两种输出量化方向：`SFD`（SF 向量沿 N，行方向）与 `SFDCol`（SF 向量沿 M，列方向），并理解 `out_quant_dim` 的含义。
- 解释 `out_transposed=True` 如何用一个**转置恒等式** \(D^\top = B^\top A^\top\) 把「列方向量化」转化为「行方向量化」，从而避开特殊的内核 swap 管道，并且对反向消费者更友好。
- 理解 `gemm_w4.py` 的 4-bit 权重 GEMM（W4A16 / W4A8）路径：权重作为 WGMMA A 操作数在寄存器里反量化、输出按转置写出。

## 2. 前置知识

本讲默认你已经学过：

- **u6-l1 / u6-l2**：可组合 epilogue 与 EpiOp 生命周期（`begin`/`begin_loop`/`quantize`/`end` 钩子）。
- **u6-l5**：领域 epilogue，尤其是 `BlockScaleFactorStore` 在 store 前对 fragment 做量化的总体思路。
- **u7-l1**：块缩放量化（Microscaling, MX）的主机侧抽象，`BlockScaledFormat`（格式的「唯一真相源」）与 `BlockScaledOperand`（把 qdata + blocked scale + format 绑成原子的非 Tensor 容器），以及 SF 的 `(rm, rk, 32, 4, 4)` blocked swizzle 布局。

几个本讲反复用到的术语，先给出直觉定义：

- **scale factor（SF）**：块缩放量化中，把每 `sf_vec_size` 个连续元素打包共用一个缩放字节。e8m0 格式 SF 向量长 32，e4m3（NVFP4）长 16。
- **SFD**：output 端的 scale factor（D 的 SF），与输入端的 SFA/SFB 对应。它的硬件存储布局是 `(L, rm, rk, 32, 4, 4)` 的 128×4 blocked atom。
- **量化输出（quantized output）**：GEMM 不再返回 bf16 结果，而是返回一个 `BlockScaledOperand`，其 `qdata` 是 fp8/fp4 量化值、`scale` 是 blocked SF。
- **W4 / W4A16 / W4A8**：weight-only 量化。权重 W 是 4-bit（或 8-bit），激活是高精度。W4A16 = 4-bit 权重 × bf16 激活；W4A8 = 4-bit 权重 × e4m3 激活。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `quack/epilogue/quantize_out.py` | `BlockScaleFactorStore` 输出量化 EpiOp 与设备侧量化核心 `sfd_quantize_subtile` / `sfd_quantize_subtile_col`。 |
| `quack/gemm_interface.py` | 公共 `gemm` API：`out_dtype`/`out_quant_dim`/`out_transposed` 参数、`gemm_quant_out` 自定义算子、配置剪枝 `_sfd_ok`、SF 缓冲分配。 |
| `quack/gemm_default_epi.py` | `GemmDefaultEpiMixin._epi_ops` 中声明 `mSFD`（行）与 `mSFDCol`（列）两个 `BlockScaleFactorStore`。 |
| `quack/gemm_w4.py` | W4 权重 GEMM 入口 `gemm_w4a16` / `gemm_w4a8`，以及离线权重重排 `prepare_w4_weight`。 |
| `quack/operand_transform/transform.py` | `TransformAW4`：把打包权重作为 A 操作数、在寄存器里反量化喂给 WGMMA。 |
| `quack/operand_transform/host.py` | `pick_w4_cfg`：W4 配置（tile/split-k）的经验选择规则。 |
| `quack/blockscaled/quantize_utils.py` | 量化语义契约（与 cuBLAS/CUTLASS 位精确）与共享核心 `quantize_sf_slots`。 |
| `tests/test_gemm_quant_out.py` | 量化输出的数值正确性测试，含行/列/转置三种路径。 |

## 4. 核心概念与源码讲解

本讲覆盖三个最小模块：**BlockScaleFactorStore 输出量化**、**SFD/SFDCol 与 out_transposed**、**W4 权重 GEMM**。其中第二个模块拆成 4.2（方向选择）和 4.3（转置技巧）两节，因为转置技巧是本讲实践任务的重点。

### 4.1 BlockScaleFactorStore：在出口生成 SF 并就地缩放累加器

#### 4.1.1 概念说明

普通的 GEMM 出口把 fp32 累加器转换成 bf16 写回显存。如果下游消费者（例如下一层 GEMM）想以 fp8/fp4 块缩放格式直接读取这个结果，我们就必须额外做一次「量化」：算出每 `sf_vec_size` 个连续元素的绝对最大值 `amax`，编码成一个 SF 字节，再把每个元素按 SF 反缩放成落在值域网格上的量化值。

关键设计取舍是：**不单独发一个量化 kernel**，而是把这个量化融进 GEMM 的 epilogue。这样省掉一次「读回 D → 量化 → 写出」的显存往返。`BlockScaleFactorStore` 就是承担这件事的 EpiOp（参见 [quack/epilogue/quantize_out.py:135-168](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L135-L168) 的类 docstring）。

它的核心思想用一个公式串起来：

\[
\text{sf} = \text{cvt}\!\left(\frac{\text{amax}}{\text{value\_dtype\_max}} \cdot \text{norm\_const}\right)
\qquad
\text{rescale} = \min\!\left(\text{rcp}(\text{dequant}(\text{sf})) \cdot \text{norm\_const},\; \text{FLT\_MAX}\right)
\]

\[
q = \text{cvt\_value\_dtype}(y \cdot \text{rescale})
\]

其中 `cvt` 是 f32→SF dtype 的转换（f32→e8m0 向 +inf 取整，f32→e4m3 就近偶），`y` 是 epilogue 处理后的 fp32 累加器值。要点是：**rescale 用的是「量化后的 SF」的反量化值**，而不是原始的 amax/max 比值——这样才能保证写出的量化值与写出的 SF 字节自洽。这套语义与 cuBLAS / CUTLASS C++ 位精确，契约写在 [quack/blockscaled/quantize_utils.py:9-22](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize_utils.py#L9-L22)。

#### 4.1.2 核心流程

`BlockScaleFactorStore` 的执行嵌入在 epilogue 驱动循环（见 u5-l1 / u6-l1）里，按 EpiOp 生命周期推进：

1. **`begin`（每 CTA tile 一次）**：分区 SF 的 gmem 视图、分配寄存器里的 SF-slot / amax / rescale 暂存张量，并从 tiled copy 的线程布局推导出「一个 SF 向量跨越多少 lane、多少 warp」的几何。
2. **`begin_loop`（每 epilogue 子 tile 一次）**：切出本子 tile 的状态；若向量跨 warp（SM120），准备跨 warp 的 smem 交换缓冲。
3. **`quantize`（驱动循环在 store_convert 之前调用）**：对一个子 tile 的 fragment 计算 amax → 量化成 SF 字节 → **就地**用 rescale 缩放 fragment。注意调用时机：在所有逐元素 epilogue op 之后、store op 的 dtype 转换之前，所以 SF 反映的是最终输出值（见 [quack/epilogue/quantize_out.py:681-716](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L681-L716)）。
4. **`end`（每 tile 一次）**：把整 tile 的 SF 字节用向量化 store 写回 gmem。

设备侧核心 `sfd_quantize_subtile` 的处理逻辑（伪代码）：

```
# frag 是子 tile 的累加器寄存器片段，sf-slot 张量以 zero-stride 广播布局与之对齐
for 每个元素 i:
    tDrAmax[slot(i)] = fmax(tDrAmax[slot(i)], |frag[i]|)   # 累加到对应 SF slot
if 向量跨 lane:  蝶形 shuffle 合并 lane 间 amax
if 向量跨 warp:  经 smem sExch 合并 warp 间 amax（SM120）
amax = |amax|                                              # 清掉累积的符号位
quantize_sf_slots(amax → SF字节 + rescale因子)              # 共享量化核心
for 每个元素 i:
    frag[i] = frag[i] * rescale[slot(i)]                    # 就地缩放
```

这里有两个工程细节值得注意，都写在 [quack/epilogue/quantize_out.py:834-941](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L834-L941)：

- **SF slot 张量用 zero-stride 广播布局**（u3-l2 讲过的技巧）：让 `vec` 个元素别名到同一个 slot，于是「按元素累加 amax」与 fragment 的寄存器顺序无关。
- **amax 用 `fmax(.., abs=True)`（xorsign）合并**（非 SM100）：保留最大幅度同时异或符号，省掉逐元素 `absf`，每个 slot 只在最后清一次符号。SM100 则保留普通 `fmax(acc, |x|)`，因为 ptxas 能把它融合成 3 输入 `FMNMX3.ABS`（SM100 专属），xorsign 反而会破坏这个融合。

共享核心 `quantize_sf_slots` 在 [quack/blockscaled/quantize_utils.py:49-87](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/quantize_utils.py#L49-L87)。e8m0 的倒数无需 `MUFU.RCP`：把 biased exponent 字节取反就得到精确倒数（`254 - byte`），NaN（0xFF）回绕到 0xFF 自然传播。

#### 4.1.3 源码精读

`BlockScaleFactorStore` 是 `EpiOp` 的子类，构造时由 `direction`（`"row"`/`"col"`）和 `output`（量化哪个存储输出，默认 `"D"`）两个参数刻画（[quack/epilogue/quantize_out.py:170-204](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L170-L204)）。这两个参数也构成编译键 `config_key()`，所以不同方向、不同量化目标会特化出不同 cubin。

主机侧 `to_params`（[quack/epilogue/quantize_out.py:237-323](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L237-L323)）做两件事：① 把硬件 blocked 张量 `(L, rm, rk, 32, 4, 4)` **重新视图**成逻辑 `(M_pad, N_pad, L)`，其 intra-vector 模式步长为 0，使它能像 D 一样被 tile 与分区；② 把可选的 fp32 `norm_const`（NVFP4 的 per-tensor scale 的倒数）配对进参数。它还断言了几条结构性约束，例如：

- 当前仅支持 SM100/SM120（[L265-L267](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L265-L267)）。
- SF dtype 只能是 e8m0（mx）或 e4m3（nvfp4）；e4m3 SF 必须配 fp4 值。
- D 布局必须是 row-major（n-major）。
- 不允许 64 行的 2-CTA tile——会切坏 SF 向量。

它在哪里被声明为 epilogue 的一员？在 `GemmDefaultEpiMixin._epi_ops` 里同时挂了两个，**至多一个激活**（[quack/gemm_default_epi.py:71-83](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L71-L83)）：

```python
BlockScaleFactorStore("mSFD"),                       # 行方向：SF 沿 N
BlockScaleFactorStore("mSFDCol", direction="col"),   # 列方向：SF 沿 M
```

对应的 `EpilogueArguments` 字段 `mSFD` / `mSFDCol` / `sfd_norm_const` 见 [quack/gemm_default_epi.py:106-113](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L106-L113)。回忆 u6-l1 的「声明是全集、执行是子集」：运行期未传 `mSFD` 的调用，这个 op 会被 `_filter_epi_ops` 过滤掉，既不进编译键，也不产生实例属性，于是普通非量化输出路径完全不受影响（这正是 `test_quant_out_regression_no_sfd` 守护的不变量）。

#### 4.1.4 代码实践

**实践目标**：用一个简单的 `quant_ref` 函数复现内核的位精确量化，理解 SF 字节与 rescale 的来历。

**操作步骤**：

1. 打开 `tests/test_gemm_quant_out.py`，阅读 `quant_ref`（[tests/test_gemm_quant_out.py:57-101](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_quant_out.py#L57-L101)）。这是纯 PyTorch 的参考实现，与 `quantize_sf_slots` 的契约一一对应。
2. 关注其中 e8m0 分支：用 `torch.frexp` 拆出指数，再 `(e + 127)` 得到字节，这正是「f32→e8m0 向 +inf 取整」的软件实现。
3. 关注 `rcp = norm_const / scale_q`：rescale 用的是量化后的 scale `scale_q`，而不是原始 amax/max。

**需要观察的现象**：当 `scale_q == 0`（全零块）时，`rcp` 取 `FLT_MAX`，量化值为 0——这与设备侧 `fmin(acc_scale, FLT_MAX)` 的饱和保护一致。

**预期结果**：`quant_ref` 给出的 `(q_float, sf_bytes, scale_float)` 与内核产物逐位相同（`test_quant_out_exact` 用 `B = identity` 保证了 GEMM 结果位精确，从而 amax 也位精确）。若你手上没有 SM100/SM120 GPU，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 rescale 用「量化后 SF 的反量化值」，而不是直接用 `dmax / amax`？

**答案**：因为写出的量化值最终要和写出的 SF 字节一起被下游反量化（`dequant = q * sf * ...`）。如果 rescale 用的是原始 `dmax/amax`，而 SF 字节是 `cvt(amax/dmax)`（经过了取整），两者不自洽，反量化就会有系统偏差。用同一个量化后 SF 的倒数做 rescale，误差被吸收进 SF 的取整里，保证 `q * dequant(sf)` 一致。参见 `quantize_utils.py` 契约注释。

**练习 2**：`BlockScaleFactorStore` 在驱动循环里被调用的时机为什么必须在所有逐元素 epilogue op **之后**？

**答案**：SF 反映的是「最终要写出的值」的 amax。若在 `apply_linear_epilogue`（加 alpha/beta/bias/rowvec/colvec）之前算 amax，就会漏掉这些线性项对幅度的贡献，导致 SF 偏小、量化值溢出。所以 `quantize` 钩子安排在 `epi_visit_subtile` 之后、`store_convert` 之前。

### 4.2 SFD/SFDCol：行方向与列方向的选择

#### 4.2.1 概念说明

SF 向量可以沿输出的两个轴排列，对应下游消费者不同的收缩方向：

- **行方向 `SFD`（沿 N）**：每 `sf_vec_size` 个**连续 N 元素**共用一个 SF 字节。这是默认方向（`out_quant_dim=-1`）。它的动机是：输出 D 是下一层 GEMM 的 **A 操作数**，下一层沿它的 N（= 这层的输出 N）做 K 收缩，所以 SF 沿 N 正好匹配输入端 SFA 的向量方向，可以直接当成 `BlockScaledOperand` 喂给下一层。
- **列方向 `SFDCol`（沿 M）**：每 `sf_vec_size` 个**连续 M 元素**共用一个 SF 字节（`out_quant_dim=-2`）。动机是 **反向消费者**：如果某个下游 GEMM 要沿这个输出的 M 维做收缩（例如反向传播里对权重的梯度），它需要 SF 沿 M。

为什么方向不是「免费的」？因为 SF 向量的几何必须落在硬件友好的 lane/warp 几何上：

- 行方向在 SM100 上：epilogue 的 (4,1) warp 形状 + 32dp tmem load，让一个 32 元素 SF 向量正好落在一个 warp 内，warp-local 归约，无需 smem。
- 列方向在 SM100 上：`lane == row`（每 lane 一个行），所以一列的 amax 是**一次** `redux.sync.max.abs.NaN.f32`（SM100 专属指令），SF 字节按字节由指定 lane 写出——见 [quack/epilogue/quantize_out.py:944-995](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L944-L995)。SM120 没有这条 redux，就走通用 slot 机制 + smem 交换。

#### 4.2.2 核心流程

主机侧 `gemm()` 把用户的 `out_quant_dim` 翻译成「调哪个 op」：

```
out_quant_dim == -1  →  SFD（行方向，SF 沿 N）
out_quant_dim == -2  →  SFDCol（列方向，SF 沿 M）
```

两者互斥（`EpilogueArguments` 注释明确「至多一个」）。最终都汇入 `gemm_quant_out` 自定义算子，由 `sfd_dim` 参数二选一：`"n"` 把张量传给 `SFD`，`"m"` 传给 `SFDCol`（见 [quack/gemm_interface.py:1446-1448](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L1446-L1448)）。

两种方向的 SF 缓冲形状不同，所以分配函数也不同：

- 行方向 `_alloc_blockscaled_out`：SF 是 `(…, rm, rk, 32, 4, 4)`，沿 N 分块（[quack/gemm_interface.py:272-300](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L272-L300)）。
- 列方向 `_alloc_blockscaled_out_col`：SF 是 `(…, rn, rm_k, 32, 4, 4)`，沿 M 分块，**且只支持 fp8**（fp4 沿 N 打包，没有沿 M 收缩的消费者能用，见 [quack/gemm_interface.py:303-319](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L303-L319)）。

设备侧，列方向走的是 `_to_params_col`（[quack/epilogue/quantize_out.py:325-354](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L325-L354)）：构建一个 `(M_pad, N_pad, L)` 视图，M 模式在 `vec` 行上广播（步长 0），4 个连续 M slot 排在连续字节。

#### 4.2.3 源码精读

`gemm()` 的参数文档把两种方向讲得很清楚（[quack/gemm_interface.py:989-1002](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L989-L1002)）：

> `out_quant_dim`: which logical dim the SF vectors run along — -1 (default: along N, the next GEMM's contraction dim) or -2 (along M, for backward consumers contracting over this output's M).

配置剪枝 `_sfd_ok`（[quack/gemm_interface.py:421-450](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L421-L450)）会剔除 SFD 跑不了的形状。注意第一条：**`swap_ab` 的配置一律被拒**——SFD 假定 D 是 n-major 且 N 未被 swap。这条约束是下一节 `out_transposed` 设计的直接动因。

#### 4.2.4 代码实践

**实践目标**：验证 `out_quant_dim=-2`（列方向）产物的 SF 沿 M 排列。

**操作步骤**：阅读 `test_quant_out_col_exact`（[tests/test_gemm_quant_out.py:380-399](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_quant_out.py#L380-L399)）。它用 `col_quant_ref`（[L363-L377](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_quant_out.py#L363-L377)）算参考 SF，注意 `amax = x.reshape(m // vec, vec, n).abs().amax(1)`——沿 M 分组。

**预期结果**：`res.quant_dim == -2`，且 SF 字节与 `col_quant_ref` 逐位相等。待本地验证（需 SM100/SM120）。

#### 4.2.5 小练习与答案

**练习**：为什么 `_alloc_blockscaled_out_col` 断言「fp4 值只能走行方向」？

**答案**：fp4（`float4_e2m1fn_x2`）两个元素打包成一个字节，且是沿 **N** 打包的（连续 N 元素凑半个字节）。一个沿 M 收缩的消费者没法用沿 N 打包的值，所以列方向只支持 fp8（每个值 1 字节，沿 N 存储但可沿 M 取）。注释原文见 [quack/gemm_interface.py:309-312](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L309-L312)。

### 4.3 out_transposed：用 swapped GEMM 得到行量化的 D^T

#### 4.3.1 概念说明

这是本讲最巧妙的设计，也是实践任务的核心。问题：**我想要列方向量化（SF 沿 M）的输出，但又想用 fp4、又想在 SM120 上省掉跨 warp 的 smem 交换，怎么办？**

答案是一个纯数学恒等式。设 \(D = AB\)（\(D\) 是 \(M\times N\)），则

\[
D^\top = B^\top A^\top
\]

\(D^\top\) 是 \(N\times M\)。**\(D^\top\) 的行方向（沿它的 N，也就是 \(D\) 的 M）量化**，等价于 **\(D\) 的列方向（沿 M）量化**！

所以只要计算 \(D^\top = B^\top A^\top\)，并对这个转置结果走**普通行方向 SFD** 路径，就得到了「沿原 M 的 SF」。这带来三个好处（写在 [quack/gemm_interface.py:992-998](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L992-L998)）：

1. **复用普通行方向路径**：内核看到的是它原生的 n-major D，无需任何 kernel 侧的 swap 管道。
2. **fp4 也能用**：fp4 沿（转置后的）N = 原 M 打包，而 NVFP4/MXFP4 本来就是沿 M 打包的——天然契合。
3. **SM120 上无跨 warp 交换**：行方向在 SM120 上可以把 warp 的 N run 拓宽到 32 列（`mma_n_warp_run`），让整个 SF 向量 warp-local；列方向做不到。

#### 4.3.2 核心流程

`out_transposed=True` 的实现极其简洁——它就是一次**递归调用**，把操作数转置后重入 `gemm()`（[quack/gemm_interface.py:1018-1045](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L1018-L1045)）：

```python
if fmt_d is not None and out_transposed:
    assert out_quant_dim == -2, "out_transposed requires out_quant_dim=-2"
    assert bias is None, "out_transposed quantized output does not support bias yet"
    assert cu_seqlens_m is None and cu_seqlens_k is None and A_idx is None, (...)
    ...
    return gemm(
        B.mT,              # 新的 A = B^T  （N,K）
        A.mT,              # 新的 B = A^T  （K,M）
        out=out,
        alpha=alpha,
        out_dtype=out_dtype,
        out_quant_dim=-1,  # 在转置帧里走行方向
        ...
    )
```

`.mT` 是矩阵转置的**视图**（对 `BlockScaledOperand` 也只交换 qdata 步长、scale 原样携带，见 u7-l1），**不搬数据**。所以递归调用计算的正是 \(B^\top A^\top = D^\top\)，输出形状是 \((N, M)\)，沿其最后一维（= 原 M）做行方向量化。

这个路径之所以能绕开 4.2 提到的「SFD 拒绝 swap_ab」约束，是因为它**根本没有用 swap_ab**：它把转置吸收进了操作数本身，内核跑的是一个普通的、未交换的 n-major GEMM。注释把这称为「唯一一种 SFD under swap 相干的方向」——意指得到转置形状的同时不需要任何 swap 管道。

#### 4.3.3 源码精读

关键代码段（[quack/gemm_interface.py:1018-1045](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L1018-L1045)）注释写明：「run the swapped GEMM (operand `.mT` views, no data movement) through the ordinary row-direction path. The kernel sees its native n-major D」。断言它不支持 bias / varlen / gather / concat_layout——这些都是转置后语义不直接对应原问题的特性。

测试 `test_quant_out_transposed`（[tests/test_gemm_quant_out.py:420-445](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_quant_out.py#L420-L445)）用 `B = identity` 让 GEMM 结果位精确，断言两件事：① 转置路径的 SF 字节与「对 \(D^\top\) 行量化」的参考逐位相等；② 对 fp8，转置路径与列方向路径（`out_quant_dim=-2` 不转置）产出的 SF 字节与值**逐位相同**——因为它们量化的是同一组 amax、用同一套 cvt。这证明两条路径数学等价，只是实现策略不同。

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：亲手验证 `out_transposed + out_quant_dim=-2` 通过 swapped GEMM 得到行量化输出，并解释它为何对反向消费者更友好。

**操作步骤**：

1. **读源码画路径**。从 `gemm(A, B, out_dtype=fmt, out_quant_dim=-2, out_transposed=True)` 进入 [gemm_interface.py:1018](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L1018)，确认它递归调用 `gemm(B.mT, A.mT, ..., out_quant_dim=-1)`。写出递归调用计算的是 \(B^\top A^\top\)，形状 \((N, M)\)。

2. **写一个最小验证脚本**（示例代码，需 SM100/SM120）：

   ```python
   import torch
   from quack.gemm_interface import gemm

   torch.manual_seed(0)
   m, n, k = 256, 320, 256
   A = torch.randn(m, k, dtype=torch.bfloat16, device="cuda")
   B = torch.eye(k, n, dtype=torch.bfloat16, device="cuda")  # identity => D==A 位精确

   # 路径1：列方向量化（不转置）
   res_col = gemm(A, B, out_dtype="mxfp8_e4m3", out_quant_dim=-2, tuned=False)
   # 路径2：转置 + 行方向量化
   res_t   = gemm(A, B, out_dtype="mxfp8_e4m3", out_quant_dim=-2,
                  out_transposed=True, tuned=False)

   print(res_t.qdata.shape)              # 预期 (n, m) = (320, 256)：转置过的输出
   # 两条路径的 SF 字节应逐位相同
   assert torch.equal(res_t.scale.view(torch.uint8),
                      res_col.scale.view(torch.uint8))
   ```

3. **追踪为何「对反向消费者更友好」**。考虑一个线性层 \(Y = X W^\top\) 的反向：权重的梯度是 \(dW = X^\top \, dY\)（或 \(gY^\top X\)），它沿 **M 维（batch/序列维）** 收缩。若前向 GEMM 的输出（这里 \(Y\) 或某个中间激活）要被反向 GEMM 当成块缩放操作数读，反向 GEMM 的 K = 前向输出的 M。`out_transposed` 产出的 \(D^\top\) 形状 \((N, M)\) 中，**值沿 M 连续**（即沿反向消费者的 K 连续），是一个**可直接加载的 k-major 操作数**，SF 沿同一方向——无需再做一次转置 + 重量化。

**需要观察的现象**：
- `res_t.qdata.shape[0] == n`（输出被转置）。
- 两条路径 SF 字节完全一致（同一组 amax、同一套 cvt）。
- 若改用 `out_dtype="nvfp4"`，列方向路径（`out_quant_dim=-2` 不转置）会被 `_alloc_blockscaled_out_col` 拒绝（fp4 只能沿 N 打包），但 `out_transposed=True` 仍可用——这正是转置路径「fp4-capable」的价值。

**预期结果**：断言通过。若你手上是 SM90 或无 GPU，则步骤 2 的运行标注「待本地验证」，但步骤 1、3 的源码追踪与数学推导可以离线完成。

#### 4.3.5 小练习与答案

**练习 1**：递归调用里为什么 `out_quant_dim` 从 `-2` 改成了 `-1`？

**答案**：因为在转置帧里，输出的最后一维（`-1`）对应原来的 M 维（`-2`）。我们想要 SF 沿原 M，而在 \((N, M)\) 输出里「沿最后一维」=「沿 M」，所以在转置帧里用行方向 `-1` 即可。

**练习 2**：为什么转置路径在 SM120 上比列方向路径更省？

**答案**：SM120 是 warp-MMA，列方向时一个 32 行的 SF 向量会跨越 16 行 stripe 的 warp 邻居，必须经 smem `sExch` 合并 amax（见 4.1.2 的 `xwarp` 分支）。转置路径走行方向，可以把 warp 的 N run 拓宽到 32 列（`GemmSm120.mma_n_warp_run`），让整个 SF 向量 warp-local，省掉 smem 交换与一次 barrier。

### 4.4 W4 权重 GEMM：4-bit 权重的反量化路径

#### 4.4.1 概念说明

W4（weight-only 量化）是大模型推理的经典场景：权重体积大、要省显存与带宽，所以压成 4-bit；激活是动态的、保持 bf16 高精度。数学上（见 [quack/gemm_w4.py:1-9](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L1-L9)）：

\[
\text{out}[M, N] = \text{act}[M, K] \;@\; \text{dequant}(W)[N, K]^\top
\]

注意权重是 \([N, K]\) 且转置参与。QuACK 的设计取舍是：**把打包权重作为 WGMMA 的 A 操作数**，由 `TransformAW4` 在寄存器里反量化成 bf16，直接喂给矩阵乘——这避免了「先反量化成一整张 bf16 权重矩阵」的显存爆炸。模块 docstring 把这一点说得很直白（[quack/gemm_w4.py:1-23](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L1-L23)）。

W4 有两个子家族：

- **W4A16**（`gemm_w4a16`）：4-bit（或 8-bit）权重 × bf16 激活。per-tensor 权重缩放走 epilogue 的 alpha。
- **W4A8**（`gemm_w4a8`）：sign-magnitude int4-g128 权重 × e4m3 per-token 激活。激活先被 `quantize_act_per_token_fp8` 量化成 fp8 + 每行一个 fp32 scale。

#### 4.4.2 核心流程

W4 的典型使用是**两步**：

1. **离线 `prepare_w4_weight`**（一次性，[quack/gemm_w4.py:46-51](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L46-L51)）：把量化权重（+ scale、按格式而定）**重排**成一对 blob。N 补齐到 128 的倍数（tile 粒度），字节按 WGMMA A-fragment 顺序洗牌，使设备侧反量化**免 shuffle**。格式由 `wformat` 字符串选（`qtip2s`、`int4sm`、`nvfp4`、`mxfp4`、`mxfp8` 等）。

2. **在线 `gemm_w4a16` / `gemm_w4a8`**（每次推理）：吃激活 + 重排好的 blob，吐 bf16 输出。

设备侧反量化由 `TransformAW4` 负责（[quack/operand_transform/transform.py:187-209](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L187-L209)）。`owns_a_layout = True` 表示这个 transform 自己拥有 A 的布局——A 不再是普通张量，而是离线重排的 blob。它的核心契约：每线程 LDS 16B（8-bit 权重或 tile_k=128 格式是 32B）后，值直接落在 WGMMA A-fragment 顺序里，所以 `decode_k16` 反量化是 shuffle-free 的。SF（若有）走 aux 操作数槽，与 A 在同一 mbarrier 下 TMA 进来。

**输出是转置写出的**：D 在内核里是 \((N, M)\) m-major，等价于调用者视角的 \((M, N)\) row-major 输出。这个「swap-at-trace」约定由 `cd_transposed` 标志触发（[quack/gemm_runtime/host.py:260](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/host.py#L260)：`cd_transposed=swap_ab or owned_fmt is not None`）。注释在 [quack/gemm_w4.py:122-124](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L122-L124)。

#### 4.4.3 源码精读

`gemm_w4a16` 的签名与流程（[quack/gemm_w4.py:61-143](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L61-L143)）关键点：

- **配置选择**：调 `pick_w4_cfg`（[quack/operand_transform/host.py:160](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L160)）拿到 `(tile_m, tile_n, split_k)`。其经验不变量（注释 [L169-L182](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L169-L182)）：赢家配置把 grid 放在 ~112–128 CTA、用能达到该数的最大 tile（tile_m=128 比 64 快 10–25%），grid 不够时用 serial split-k 补足。
- **per-tensor 权重缩放 = alpha**：用一个极简的 `@gemm_epilogue` 函数 `_w4a16_alpha`（[quack/gemm_w4.py:56-58](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L56-L58)）把 alpha 乘到累加器上。回忆 u6-l4：`alpha` 形参会自动推断成一个 `Scalar` EpiOp，`alpha==1.0` 是位等价的恒等（不进 cubin）。所以 per-tensor scale 是「免费的」epilogue 标量乘。
- **split-k 缓冲复用**：serial split-k 的 partials 工作区按形状缓存复用（[quack/gemm_w4.py:111-121](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L111-L121)），因为内核会把信号量复位。

`gemm_w4a8`（[quack/gemm_w4.py:169-250](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L169-L250)）多了一层「per-token 激活缩放」。`quantize_act_per_token_fp8`（[quack/gemm_w4.py:161-166](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L161-L166)）把 \((M,K)\) 浮点激活量化成 e4m3 + 每行 fp32 scale（`amax/448`）。注释 [L146-L148](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L146-L148) 点出关键洞察：per-token scale 是「k-不变、per-output-row」的因子，它可以从 GEMM 求和里提出来，作为一次**精确的 fp32 colvec 乘**在 epilogue 应用。又因为 D 是转置的，这个 colvec 在内核里翻转成 rowvec。`_w4a8_token_scale`（[quack/gemm_w4.py:149-151](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L149-L151)）正是 `{"D": acc * v}`，`v` 由 `ColVecLoad` 装载。

整层抽象的妙处在于（docstring [L16-L23](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L16-L23)）：`gemm_w4a16`/`gemm_w4a8` 只是「fn epilogue 前端 + `transform_a`」的薄糖，两个入口都是 `EpiMod.gemm(..., transform_a=...)` 调用。所以 W4 内核与所有 epilogue 变体**共享** plan 缓存、jit/磁盘缓存、异步编译池、EpiOp 参数机制。留在 `gemm_w4.py` 里的只是 W4 自己的主机表面：离线 `prepare`、校验、显式 tile 处理、split-k 缓冲复用。

#### 4.4.4 代码实践

**实践目标**：跑通一次 W4A16 的「prepare → gemm」两步流程，并理解为何输出形状是 \((M, N)\)。

**操作步骤**：

1. 读 `tests/test_gemm_w4.py`（本讲的 W4 数值测试，配套 `DecodeFormat.quantize_reference`/`dequant_reference` 做 roundtrip 校验）。`DecodeFormat` 的 host 契约见 [quack/operand_transform/formats/__init__.py:57-88](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/formats/__init__.py#L57-L88)：`prepare(q, sf) -> (blob, sf_blob)` 是离线重排，`decode_k16` 是设备侧反量化。
2. 写一个最小调用（示例代码，需 SM90+）：

   ```python
   import torch
   from quack.gemm_w4 import prepare_w4_weight, gemm_w4a16

   m, n, k = 128, 256, 512
   act = torch.randn(m, k, dtype=torch.bfloat16, device="cuda")
   W   = torch.randn(n, k, dtype=torch.bfloat16, device="cuda")  # (N,K)
   q, sf = ...                       # 按 wformat 把 W 量化（见 formats 的 quantize_reference）
   blob, sf_blob = prepare_w4_weight(q, sf, wformat="qtip2s")    # 离线重排
   out = gemm_w4a16(act, blob, sf=sf_blob, wformat="qtip2s")
   print(out.shape)                  # 预期 (m, n) = (128, 256)
   ```

**需要观察的现象**：
- `blob.shape[0] * 64 == n_full`（N 补齐到 128 倍数，每 64 行一组）。
- 输出 `out.shape == (m, n)`，即转置写在内部完成、对调用者透明。
- 若 `n` 不是 128 倍数，`gemm_w4a16` 内部分配 padded `out` 再切片返回（[quack/gemm_w4.py:100-104,141-143](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L100-L104)）。

**预期结果**：`out` 与 bf16 参考 `act.float() @ W.float().T` 在量化误差内一致。具体量化步长取决于 `wformat`。待本地验证（需 GPU）。

#### 4.4.5 小练习与答案

**练习 1**：W4A16 里 per-tensor 权重缩放为什么不需要单独的 epilogue op，直接乘 alpha 就行？

**答案**：\(D = \text{act} @ (s_w \cdot \text{dequant}(W))^\top = s_w \cdot (\text{act} @ \text{dequant}(W)^\top)\)。标量 \(s_w\) 可以提到 GEMM 外，变成对累加器的一次标量乘，即 epilogue 的 alpha。`_w4a16_alpha` 的 `acc * alpha` 精确表达了这一点，alpha=1.0 时退化为恒等。

**练习 2**：W4A8 的 per-token 激活 scale 为何能「从 GEMM 求和里提出来」？

**答案**：per-token scale \(s_m\) 只依赖行 \(m\)（与 k 无关）。\(D_{m,n} = \sum_k (\text{act}_{m,k} \cdot W_{n,k}) = s_m \sum_k (\text{act}^{q}_{m,k} \cdot W_{n,k})\)。求和号对 k 展开，\(s_m\) 是公因子，提到求和号外，变成对结果行 \(m\) 的一次逐行乘（colvec）。注释见 [quack/gemm_w4.py:146-148](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L146-L148)。

## 5. 综合实践

把本讲三块内容串起来：构建一个 **「bf16 GEMM → 量化输出 → 当作下一层 blockscaled 输入」** 的小链，并对比两种「沿 M 量化」路径。

**任务**：

1. **正向链（行方向 SFD）**。模拟 `fc1 → fc2`：第一层 `gemm(A, W1, out_dtype="nvfp4")` 得到一个 `BlockScaledOperand`（行方向，SF 沿 N）；把它**直接**作为第二层 `gemm(x1_op, W2_op.mT, out_dtype=torch.bfloat16)` 的 A 操作数。这正是 `test_quant_out_varlen_m_chain`（[tests/test_gemm_quant_out.py:327-360](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_quant_out.py#L327-L360)）展示的 MoE 风格链。观察：第一层量化输出的 SF 布局天然匹配第二层 SFA 输入，无需任何重排。

2. **反向友好的量化（对比两条路径）**。对同一个 GEMM，分别调用：
   - `gemm(A, B, out_dtype="mxfp8_e4m3", out_quant_dim=-2)`（列方向，SFDCol）
   - `gemm(A, B, out_dtype="mxfp8_e4m3", out_quant_dim=-2, out_transposed=True)`（转置 + 行方向）
   
   验证两者 SF 字节逐位相同（参考 4.3.4 的脚本）。再说明：若下游是一个「沿此输出 M 收缩」的反向 GEMM，转置路径产出的 \((N,M)\) k-major 张量可以直接当操作数加载；而列方向路径产出的是 \((M,N)\)、值沿 N 连续，反向消费者要先转置。

3. **写一段总结**：用一句话说清「行方向 SFD 服务前向链、列方向 SFDCol / out_transposed 服务反向消费者」这一分工，以及 `out_transposed` 为何在 fp4 + SM120 上更优。

**验收标准**：能画出三种产物的形状与 SF 排列方向；能解释 `out_transposed` 的转置恒等式；能指出 W4 把权重放在 A 操作数的动机（省显存、寄存器内反量化）。

## 6. 本讲小结

- `BlockScaleFactorStore` 是输出量化 EpiOp：在 store_convert 之前对子 tile fragment 算 amax、量化成 SF 字节、就地 rescale，使普通 `f32→d_dtype` 转换自动写出量化值；语义与 cuBLAS/CUTLASS 位精确。
- 两个方向：`SFD`（行，SF 沿 N，喂下一层前向 GEMM）与 `SFDCol`（列，SF 沿 M，喂反向消费者）；由 `out_quant_dim`（-1/-2）选择，至多激活一个。
- `out_transposed=True` 用恒等式 \(D^\top=B^\top A^\top\) 把「列方向量化」转成「转置结果的行方向量化」，复用普通行路径、无 swap 管道、fp4 可用、SM120 省跨 warp smem 交换。
- 量化输出整条链：`gemm()` → `gemm_quant_out` 自定义算子 → `gemm_tuned` 的 `SFD`/`SFDCol` 参数 → `BlockScaleFactorStore`；配置由 `_sfd_ok` 剪枝（拒绝 swap_ab 等）。
- W4 权重 GEMM 把打包权重作为 WGMMA A 操作数，由 `TransformAW4` 在寄存器里 shuffle-free 反量化；输出按 `cd_transposed` 转置写出；per-tensor/per-token 缩放分别折叠成 epilogue 的 alpha / colvec 乘。
- W4 入口是「fn epilogue 前端 + `transform_a`」的薄糖，与所有 epilogue 变体共享 plan/jit/异步编译缓存。

## 7. 下一步学习建议

- **u7-l3（A 算子变换）**：本讲只点了 `TransformAW4` 与 `formats/`，下一讲会系统讲 `operand_transform/` 的 transform/kinds 内核侧、`@a_transform` 前端与 host bundle（含 W4 配置规则与 packed-weight 解码格式 `qtip` 等）。
- **u7-l4（AllGather + GEMM）**：W4 常与张量并行同用，下一讲讲分布式 AllGather 与 GEMM 的融合。
- **继续阅读源码**：想深挖量化数学，读 `quack/blockscaled/quantize_utils.py` 的完整契约；想看 W4 各格式的离线重排与设备反量化，读 `quack/operand_transform/formats/__init__.py` 与 `quack/operand_transform/transform.py` 的 `TransformAW4.decode_k16`。
