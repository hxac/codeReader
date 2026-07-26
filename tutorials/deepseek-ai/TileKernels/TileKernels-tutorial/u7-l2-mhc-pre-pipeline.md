# MHC 前处理流水线与融合

## 1. 本讲目标

本讲聚焦 Manifold HyperConnection（mhc，流形超连接）流水线的 **pre（前处理）** 段。上一讲（u7-l1）我们建立了整条流水线 `expand → pre → sublayer → post` 的心智模型，并精读了 `expand / norm_fn / post` 三个基础算子。本讲要回答：**pre 段如何把多股残差压成单股喂给子层（attention/FFN），以及为什么在推理时它会被「融合」成一个 kernel。**

学完后你应该能够：

1. 画出 `mhc_pre` 的三段组合顺序：`pre_norm_fn → pre_split_mixes → sinkhorn → pre_apply_mix`，并说明每一步的输入输出。
2. 说清 `pre_split_mixes` 把 `mixes` 切成 pre / post / comb 三段的逻辑与数学定义。
3. 解释 `pre_apply_mix` 如何用一组门控权重把 `mhc_mult` 股残差加权压缩成单股。
4. 读懂 `pre_big_fuse` 如何用「warp 分工」把四个阶段融进一个 kernel。
5. 说透 **`pre_big_fuse` 为什么只在 `torch.is_grad_enabled()` 为 False（推理态）时启用**——这是本讲的核心思辨点。
6. 理解 `mhc_head` / `head_compute_mix` 对 lm_head 的精简处理。

## 2. 前置知识

阅读本讲前，建议你已经掌握（这些都在前置讲义中讲过）：

- **mhc 的残差扩展思想**（u7-l1）：标准 Transformer 的单股残差 `(..., H)` 被升级为 `(..., mhc_mult, H)` 的多股并行残差，子层对多股无感知；`mhc_mult=4` 是当前唯一保证可用的取值。
- **TileLang 算子骨架**（u2-l1）：`@tilelang.jit` 构造器 + `@T.prim_func` 内核 + `T.dynamic` 运行时符号 + `with T.Kernel(..., threads=N)` 网格。
- **存储层级与搬运**（u2-l2）：`alloc_fragment`（寄存器）/ `alloc_shared`（共享内存）/ `T.copy` 搬运。
- **循环与规约原语**（u2-l3）：`T.Parallel`、`T.serial`、`T.Pipelined`、`reduce_sum/max`、`alloc_reducer`。
- **`norm_fn` 的两步结构**（u7-l1）：`fwd_mul`（tensor core GEMM 算未归一化的 mixes 与平方和）+ `fwd_norm`（RMSNorm 归一化），产出 `mhc_mult3 = mhc_mult*(mhc_mult+2)` 个通道。

几个本讲会用到的术语，先用一句话定型：

- **mixes（混合系数）**：`pre_norm_fn` 的输出，形状 `(..., mhc_mult3)`。对 `mhc_mult=4` 即 24 个通道，是后续一切门控的「原料」。
- **pre / post / comb 三段**：把这 24 个通道按用途切成三组——pre 控制如何把多股压成单股、post 与 comb 控制子层输出如何混回多股。post 与 comb 不在 pre 里消费，而是打包成不透明元组 `ctx=(post_mix, comb_mix)` 交给 `mhc_post`。
- **fusion（融合）**：把原本串行、各自读写一次显存的多个 kernel 合成一个，让中间结果留在片上（寄存器/共享内存），省下 HBM 往返。

## 3. 本讲源码地图

本讲涉及的关键文件分两层：**functional 编排层**（决定调用顺序与是否融合）与 **kernel 实现层**（每个阶段的具体计算）。

| 文件 | 作用 | 所属层 |
| --- | --- | --- |
| `tile_kernels/modeling/mhc/functional.py` | `mhc_pre` 与 `mhc_head` 的顶层编排，含 `is_grad_enabled` 分流 | 编排层 |
| `tile_kernels/modeling/mhc/ops/pre_split_mixes.py` | `MHCPreSplitMixes`（autograd.Function）封装 split kernel | 桥接层 |
| `tile_kernels/modeling/mhc/ops/pre_apply_mix.py` | `MHCPreApplyMix`（autograd.Function）封装 apply kernel | 桥接层 |
| `tile_kernels/modeling/mhc/ops/pre_big_fuse.py` | `mhc_pre_big_fuse`（**普通函数，非 autograd**）封装融合 kernel | 桥接层 |
| `tile_kernels/modeling/mhc/ops/head_compute_mix.py` | `MHCHeadComputeMix`（autograd.Function）封装 head mix | 桥接层 |
| `tile_kernels/mhc/pre_split_mixes_kernel.py` | split_mixes 的 TileLang 前向/反向 kernel | 实现层 |
| `tile_kernels/mhc/pre_apply_mix_kernel.py` | apply_mix 的 TileLang 前向/反向 kernel | 实现层 |
| `tile_kernels/mhc/pre_big_fuse_kernel.py` | 四合一融合 kernel（仅前向） | 实现层 |
| `tile_kernels/mhc/head_compute_mix_kernel.py` | lm_head 专用精简 mix kernel | 实现层 |

读者调用入口是 `mhc_pre` / `mhc_head`（functional 层），它们再调 ops 层的 `autograd.Function.apply` 或普通函数，ops 层再启动 TileLang kernel。这一「编排 → 桥接 → 实现」三层结构是 modeling 层的通用范式（见 u8-l1）。

## 4. 核心概念与源码讲解

### 4.1 functional.mhc_pre：三段组合与 is_grad_enabled 分流

#### 4.1.1 概念说明

`mhc_pre` 是 pre 段的**唯一用户入口**。它接收多股残差 `residual`、投影矩阵 `fn`、缩放 `scale`、偏置 `base`，产出两样东西：

- `layer_input`：形状 `(..., hidden_size)` 的单股张量，直接喂给子层（attention 或 FFN）。
- `ctx`：不透明元组 `(post_mix, comb_mix)`，记录「子层输出之后该如何混回多股」，原样转交给 `mhc_post`。

pre 段的语义可以概括为一句：**用一组可学习的门控，把 `mhc_mult` 股残差加权求和成一股，同时为 post 段准备好两张「回混配方」。**

#### 4.1.2 核心流程

`mhc_pre` 内部有一个关键分支：**是否处于推理态（无梯度）**。这决定了走「四合一融合」还是「四步拆分」。

```
mhc_pre(residual, fn, scale, base, ...):
    if not torch.is_grad_enabled():        # 推理态
        post_mix, comb_mix, layer_input = mhc_pre_big_fuse(...)   # 一个融合 kernel
        return layer_input, (post_mix, comb_mix)

    # 训练态：四步拆分，每步都是独立 autograd.Function
    mixes      = mhc_pre_norm_fn(residual, fn, norm_weight, ...)       # GEMM + RMSNorm
    pre_mix, post_mix, comb_mix = mhc_pre_split_mixes(mixes, ...)      # 切三段
    comb_mix   = sinkhorn_normalize(comb_mix, ...)                     # 行列归一化
    layer_input = mhc_pre_apply_mix(residual, pre_mix)                 # 加权压缩成单股
    return layer_input, (post_mix, comb_mix)
```

两条路径在**前向数值上等价**（融合版只是把拆分版的中间张量留在片上），区别只在「能否反传」。

#### 4.1.3 源码精读

分流判据就一行——`torch.is_grad_enabled()`：

[functional.py:69](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L69) 判定当前是否在梯度计算上下文中。`torch.no_grad()` / `torch.inference_mode()` 会让它返回 False，从而走融合路径。

推理态走融合（返回值里 `layer_input` 喂子层，`(post_mix, comb_mix)` 当 ctx）：

[functional.py:70-82](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L70-L82) 一次性调用 `mhc_pre_big_fuse` 拿到三个输出。

训练态走四步拆分，顺序就是上面流程图：

[functional.py:84-105](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L84-L105) 依次调用 `mhc_pre_norm_fn` → `mhc_pre_split_mixes` → `sinkhorn_normalize` → `mhc_pre_apply_mix`。

注意 `mhc_pre` 的返回签名（[functional.py:44](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L44)）声明返回 `tuple[torch.Tensor, tuple[torch.Tensor, torch.Tensor]]`——即 `(layer_input, (post_mix, comb_mix))`。第二个元素是「不透明」元组：调用方不需要理解其内部结构，只需原样传给 `mhc_post`。这种用元组打包上下文、跨函数传递的写法，是把多个 op 串成一条流水线的常见手法。

> 关键结论：**分流不是性能优化可选项，而是正确性必需。** 融合 kernel 没有反向实现，只能在不需要梯度的推理态使用。具体原因见 4.4.5。

#### 4.1.4 代码实践

**实践目标**：确认两条路径的前向等价性，并定位融合路径没有反向的证据。

**操作步骤（源码阅读型 + 可选运行）**：

1. 打开 `tile_kernels/modeling/mhc/ops/pre_big_fuse.py`，确认 `mhc_pre_big_fuse` 是一个**普通 `def` 函数**，不是 `torch.autograd.Function` 的子类，函数体内也没有 `save_for_backward`。证据见 [pre_big_fuse.py:7-18](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/pre_big_fuse.py#L7-L18)。
2. 对比 `tile_kernels/modeling/mhc/ops/pre_split_mixes.py` 中的 `MHCPreSplitMixes(torch.autograd.Function)`，它有完整的 `forward` / `backward`，见 [pre_split_mixes.py:7](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/pre_split_mixes.py#L7) 与 [pre_split_mixes.py:58](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/pre_split_mixes.py#L58)。
3. **直接复用项目自带的对拍测试**：`tests/mhc/test_pre_big_fuse.py` 已经把「融合路径」与「四步拆分参考」跑在同一组输入上比对，断言用的是位精确的 `torch.equal`（而非浮点容差），见 [test_pre_big_fuse.py:110-138](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_pre_big_fuse.py#L110-L138)。其中的参考函数 `big_fuse_reference`（[test_pre_big_fuse.py:57-93](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_pre_big_fuse.py#L57-L93)）内部就是 `mhc_pre_norm_fn → mhc_pre_split_mixes → sinkhorn_normalize → mhc_pre_apply_mix` 四步。有 GPU 时执行 `pytest tests/mhc/test_pre_big_fuse.py -v`（**待本地验证**），无 GPU 时纯阅读这两段代码即可确认等价性已被项目固化。

**需要观察的现象**：测试里三个 `torch.equal` 全部通过，说明两条路径连浮点累加顺序都做到位一致；融合路径返回的 `layer_input` 没有 `grad_fn`（不连计算图），拆分路径的有 `grad_fn`。

**预期结果**：前向数值一致；只有拆分路径能 `.backward()`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `mhc_pre` 里的判据从 `not torch.is_grad_enabled()` 改成 `True`（永远走融合），在训练时会发生什么？

**参考答案**：前向仍能得到正确的 `layer_input`，但因为它来自一个无 `backward` 的普通函数，`layer_input` 不连计算图，随后的 `loss.backward()` 无法把梯度传回 `fn / scale / base / residual`，参数不会被更新。这正是判据必须区分训练/推理的原因。

**练习 2**：`ctx=(post_mix, comb_mix)` 为什么要打包成「不透明元组」而不是让调用方自己拼？

**参考答案**：post 段（`mhc_post`）需要这两张配方表才能把子层输出混回多股残差；它们的内部结构（一张是 `(..., mhc_mult, 1)` 的逐股权重，一张是 `(..., mhc_mult, mhc_mult)` 的股间耦合矩阵）是 pre/post 两段共同约定的实现细节。打包成元组并称为「opaque」，是为了让流水线的中间环节（子层）不需要关心这些细节，只负责原样转发。

---

### 4.2 pre_split_mixes_kernel：把 mixes 切成 pre / post / comb

#### 4.2.1 概念说明

`pre_norm_fn` 产出的 `mixes` 形状是 `(..., mhc_mult3)`，其中 `mhc_mult3 = mhc_mult*(mhc_mult+2)`。对 `mhc_mult=4` 即 24 个通道。这 24 个通道在语义上分三组：

| 段名 | 通道区间（mhc_mult=4） | 通道数 | 用途 |
| --- | --- | --- | --- |
| pre | `[0, mhc_mult)` = `[0,4)` | 4 | 压缩多股→单股的门控，**在 pre 段立即消费** |
| post | `[mhc_mult, 2*mhc_mult)` = `[4,8)` | 4 | 子层输出的逐股回混权重，交 `mhc_post` |
| comb | `[2*mhc_mult, mhc_mult3)` = `[8,24)` | 16 = 4×4 | 股间耦合矩阵，交 `mhc_post`（先做 sinkhorn） |

`pre_split_mixes` 就是把连续的 `mixes` 切成这三段，并对每段套不同的非线性。

#### 4.2.2 核心流程

对每个 token `i`、每个通道下标 `j`：

\[
\begin{aligned}
\text{pre\_mix}[i,j] &= \sigma\!\left(\text{mixes}[i,j]\cdot s_0 + b_j\right) + \varepsilon_{\text{pre}}, \quad j\in[0,M) \\
\text{post\_mix}[i,j] &= \sigma\!\left(\text{mixes}[i,j+M]\cdot s_1 + b_{j+M}\right)\cdot c_{\text{post}}, \quad j\in[0,M) \\
\text{comb\_mix}[i,j\cdot M+k] &= \text{mixes}[i,j\cdot M+k+2M]\cdot s_2 + b_{j\cdot M+k+2M}, \quad j,k\in[0,M)
\end{aligned}
\]

其中 \(M=\text{mhc\_mult}\)，\(s_0,s_1,s_2\) 是 `scale` 的三个分量，\(\sigma\) 是 sigmoid，\(\varepsilon_{\text{pre}}\) 是防零下溢的小常数，\(c_{\text{post}}\) 是 `mhc_post_mult_value`。注意三段的非线性不同：

- **pre** 用 `sigmoid + eps`：得到一个严格正、有下界 \(\varepsilon_{\text{pre}}\) 的门控（保证每股至少贡献一点，避免某股被完全关闭）。
- **post** 用 `sigmoid * post_mult_value`：同样是正门控，但乘一个幅度因子。
- **comb** 是**线性**的（无 sigmoid），因为它要表示一张「耦合矩阵」，随后由 sinkhorn 归一化压到合理范围。

反向传播需要 sigmoid 的导数 \(\sigma'(z)=\sigma(z)(1-\sigma(z))\)，所以反向 kernel 要**重算一次** sigmoid（见 4.2.3）。

#### 4.2.3 源码精读

前向 kernel 的三段切分是三个 `T.Parallel` 循环。先看 pre 段：

[pre_split_mixes_kernel.py:51-57](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_split_mixes_kernel.py#L51-L57) 对前 `mhc_mult` 个通道做 `sigmoid(scale[0]·mixes + base) + eps`。

post 段（注意偏移 `+mhc_mult`、用 `scale[1]`、乘 `post_mult_value`）：

[pre_split_mixes_kernel.py:58-59](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_split_mixes_kernel.py#L58-L59) post 段计算。

comb 段（线性，偏移 `+2*mhc_mult`，共 `mhc_mult*mhc_mult` 个通道）：

[pre_split_mixes_kernel.py:60-61](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_split_mixes_kernel.py#L60-L61) comb 段计算。

通道常数在构造期就算好了：`mhc_mult2 = mhc_mult*mhc_mult`、`mhc_mult3 = mhc_mult*2 + mhc_mult2`，见 [pre_split_mixes_kernel.py:20-21](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_split_mixes_kernel.py#L20-L21)。

反向 kernel 用 **persistent kernel**（grid 绑定 SM 数而非 token 数，详见 u6-l1）逐块扫描所有 token。它需要 `scale` 和 `base` 的梯度，但这两个是「跨 token 共享的标量/小向量参数」，多个 block 会同时累加它们——因此采用 **split-K 风格的部分梯度**：每个 block 写一行到 `mhc_scale_grad_partial[num_sms, 3]` / `mhc_base_grad_partial[num_sms, mhc_mult3]`，再在 ops 层 `.sum(0)` 聚合。

反向里 pre 段重算 sigmoid 并套导数：

[pre_split_mixes_kernel.py:138-142](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_split_mixes_kernel.py#L138-L142) `input_mixes_grad = pre_grad · σ·(1−σ)`，正是 sigmoid 导数链。

`base` 梯度对每个 token 求和（`reduce_sum(..., dim=0, clear=False)` 用 `clear=False` 做跨块累加）：

[pre_split_mixes_kernel.py:150](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_split_mixes_kernel.py#L150) base 梯度跨 token 归约。

`scale` 梯度用 `alloc_reducer(3)`（replication='all'）跨线程累加，最后 `finalize_reducer`：

[pre_split_mixes_kernel.py:152-154](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_split_mixes_kernel.py#L152-L154) 同时算 `scale_grad`（reducer 累加）与给 `input_mixes_grad` 乘回 `scale`（链式法则另一半）。这里用 `j // mhc_mult` 把 24 个通道映射回 3 个 scale 分量，`T.min(2, ...)` 是为了把最后一组（comb，`j//mhc_mult==2`）也钳到下标 2。

ops 层把 partial 梯度聚合：

[pre_split_mixes.py:100-102](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/pre_split_mixes.py#L100-L102) `.sum(0)` 把 `num_sms` 个分块梯度求和，得到最终 `scale_grad / base_grad`。这是「每个 SM 写一行、外层求和」的 split-K 归约模式，u8-l3 会集中讨论。

#### 4.2.4 代码实践

**实践目标**：用 PyTorch 复现 split_mixes 的前向，理解三段切分。

**操作步骤（源码阅读 + 可选对拍）**：

1. 读懂 [pre_split_mixes_kernel.py:51-61](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_split_mixes_kernel.py#L51-L61) 的三个循环。
2. 写一个 torch 参考函数（**示例代码，非项目原有**）：

   ```python
   # 示例代码：split_mixes 的 torch 参考
   def split_mixes_ref(mixes, scale, base, mhc_mult, post_mult_value, pre_eps):
       M = mhc_mult
       pre  = torch.sigmoid(mixes[..., :M] * scale[0] + base[:M]) + pre_eps
       post = torch.sigmoid(mixes[..., M:2*M] * scale[1] + base[M:2*M]) * post_mult_value
       comb = mixes[..., 2*M:] * scale[2] + base[2*M:]
       return pre, post, comb
   ```
3. （**待本地验证**，需 GPU）造随机 `mixes`、`scale`、`base`，对拍 `mhc_pre_split_mixes` 与上面的参考。

**需要观察的现象**：pre 段恒 \(>\varepsilon_{\text{pre}}\)；post 段恒 \(\in[0, c_{\text{post}}]\)；comb 段可正可负（未归一化）。

**预期结果**：前向逐元素一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 pre 段要 `+ pre_eps`，而 post 段不要？

**参考答案**：pre 段的值会作为「权重」去乘残差并求和（见 4.3）。如果某股的门控恰好 sigmoid 输出极接近 0，该股信息会被完全丢弃；`+ eps` 给每股一个最小贡献下界，保证信息不丢。post 段的值是交给 `mhc_post` 做回混的幅度因子，没有「不能为零」的约束，故不加。

**练习 2**：反向 kernel 里 `T.min(2, j // mhc_mult)` 的 `min` 是为了处理哪种情况？

**参考答案**：`scale` 只有 3 个分量（对应 pre/post/comb 三段），但通道数 `mhc_mult3 = mhc_mult*(mhc_mult+2)`。`j // mhc_mult` 在 comb 段会等于 2（恰好是合法下标），看似不需要 `min`；但写 `min(2, ...)` 是一种防御性写法，确保即便通道数与三段划分不完全对齐，下标也不会越界，恒映射到合法的 `scale[0..2]`。

---

### 4.3 pre_apply_mix_kernel：把 pre_mix 加权压缩回单股

#### 4.3.1 概念说明

split 出来的 `pre_mix` 形状是 `(..., mhc_mult, 1)`——对每个 token、每股残差一个门控标量。`pre_apply_mix` 的工作就是：**用这组门控把 `mhc_mult` 股残差加权求和，压缩成单股 `(..., hidden_size)`**，作为子层的输入。

数学上：

\[
\text{layer\_input}[i, h] = \sum_{m=0}^{M-1} \text{pre\_mix}[i, m]\cdot \text{residual}[i, m, h]
\]

这是一个纯**带宽受限**（bandwidth-bound）算子：计算量只是乘加，但要在显存里读 `mhc_mult` 份残差、写一份输出。所以 kernel 的设计目标是把残差**一次性搬进共享内存**、在寄存器里做累加，最大化复用。

#### 4.3.2 核心流程

```
对每个 token（一个 block）:
    把本 token 的 mhc 个门控标量 load 进 mixl 寄存器
    对 hidden 维按 h_blk 分块（Pipelined 双缓冲）:
        把 residual[token, :, h_blk] 搬进 shared (mhc × h_blk)
        再搬进 fragment
        清零累加器 ol[h_blk]
        for m in 0..mhc:                # serial（轮间有依赖：都往 ol 累加）
            ol += mixl[m] * xl[m, :]    # parallel（hidden 维向量加）
        把 ol 写回 layer_input[token, h_blk]
```

关键点：**`mhc` 维用 `T.serial`（因为有累加依赖），`hidden` 维用 `T.Parallel`（向量内独立）**。这正是 u2-l3 讲过的「有依赖用 serial、独立用 Parallel」的选择。

#### 4.3.3 源码精读

前向 kernel 用一个 block 处理一个 token，`threads=128`：

[pre_apply_mix_kernel.py:33-35](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_apply_mix_kernel.py#L33-L35) 网格按 token 一维展开，先 load 门控。

hidden 维分块 + 双缓冲流水线：

[pre_apply_mix_kernel.py:37-41](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_apply_mix_kernel.py#L37-L41) `T.Pipelined(..., num_stages=2)` 让「下一块的 load」与「当前块的算」重叠，掩盖 HBM 延迟。`disable_tma=True` 走向量化 load-store（见 u2-l2）。

核心累加（serial over mhc，parallel over hidden）：

[pre_apply_mix_kernel.py:47-49](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_apply_mix_kernel.py#L47-L49) 即上面的求和公式。

反向需要 `residual` 和 `pre_mix` 两路梯度：\(\frac{\partial o}{\partial x}\) 是 `mix`（乘回门控），\(\frac{\partial o}{\partial \text{mix}}\) 是 `x`（乘回残差）。`mix_grad` 是跨 hidden 的归约，用 `alloc_reducer`：

[pre_apply_mix_kernel.py:101-103](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_apply_mix_kernel.py#L101-L103) 同时更新 `mix_grad`（累加 `o_grad·x`）与 `x_grad`（`mix·o_grad`）。

反向对 mix 梯度的跨线程归约：

[pre_apply_mix_kernel.py:107-108](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_apply_mix_kernel.py#L107-L108) `finalize_reducer(mgl)` 把各线程累加的 `mix_grad` 合并，再写回。

ops 层有一个巧妙的「就地累加」优化：如果 `x` 的存储上挂着 `grad_from_mhc_post` 属性（说明这是 post 段反向写出的梯度缓冲），就直接往里累加 `x_grad` 并对该参数返回 `None`，避免分配临时张量：

[pre_apply_mix.py:30-38](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/pre_apply_mix.py#L30-L38) 检测 `grad_from_mhc_post` 并就地写入。这与 engram 模块的 `main_grad` 思路一致（u6-l2）：把跨算子的梯度累加进同一个 fp32 缓冲，省一次临时张量与一次显式加法。

#### 4.3.4 代码实践

**实践目标**：理解「mhc 维 serial、hidden 维 parallel」的选择，以及 Pipelined 的作用。

**操作步骤（源码阅读型）**：

1. 读 [pre_apply_mix_kernel.py:47-49](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_apply_mix_kernel.py#L47-L49)，把两层循环的语义用一句话说清。
2. 思考：如果把外层 `T.serial(mhc)` 换成 `T.Parallel(mhc)`，会发生什么？

**需要观察的现象**（推理）：`Parallel(mhc)` 会让多个线程同时写 `ol[i1_h] += ...`，产生写冲突（race），结果未定义；所以累加维度必须 serial。

**预期结果**：能解释「累加维度必须 serial，向量内维度可以 parallel」这条 u2-l3 的规则在此处的具体体现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `mix_grad` 要用 `alloc_reducer`，而 `x_grad` 不用？

**参考答案**：`x_grad[m,h] = mix[m]·o_grad[h]` 对每个 `(m,h)` 都是独立的，可以直接并行写。而 `mix_grad[m] = Σ_h o_grad[h]·x[m,h]` 需要把整个 hidden 维的乘积求和到一个标量，是跨 hidden 的归约，必须用 reducer 把各线程的部分和合并。

**练习 2**：`h_blk = math.gcd(h_blk, hidden)`（[pre_apply_mix_kernel.py:25](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_apply_mix_kernel.py#L25)）这一步在做什么？

**参考答案**：把期望的块大小（默认 1024）与实际 `hidden_size` 取最大公约数，保证 `hidden_size % h_blk == 0`，使 hidden 维能被整分成若干块、循环边界不用处理尾部。这是 u2-l1 / u2-l3 讲过的「用 gcd/ceildiv 处理分块边界」的典型用法。

---

### 4.4 pre_big_fuse_kernel：推理态的四合一融合与 warp 分工

#### 4.4.1 概念说明

`pre_big_fuse` 是 pre 段的**推理专用融合 kernel**。它把训练态拆分路径里的四个阶段——「RMSNorm 归一（norm_fn 的 norm 半步）+ split_mixes + sinkhorn + apply_mix」——融进**一个 kernel**。注意：`norm_fn` 的另一半（`fwd_mul`，即 GEMM）并没有融进来——GEMM 是 tensor core 密集型算子，仍由单独的 `_mhc_pre_norm_fn_fwd_mul` 完成；融合 kernel 的输入正是该 GEMM 的产物 `gemm_out_mul` / `gemm_out_sqrsum`。

融合的收益：训练态四步路径里，`mixes`、`pre_mix`、`comb_mix`（归一化前）等中间张量都要写回 HBM 再被下一步读回；融合后它们全留在片上（寄存器/共享内存），每个 token 只读写显存一次。对一个带宽受限的 pre 段，这能显著提升有效带宽。

#### 4.4.2 核心流程

融合 kernel 采用 **「一个 block 处理一个 token，block 内 warp 分工」** 的结构，`threads=96`（3 个 warp）：

```
with T.Kernel(num_tokens, threads=96) as pid:   # 每 token 一个 block
    if thread < 32:          # warp 0：做「小维度」标量/通道计算
        # (A) norm：从 gemm_out 算出 mixes[24]，写进 mixes_shared
    if thread < 32:          # warp 0 继续：做 split(post/comb) + sinkhorn
        # (B) post_mix、comb_mix(4x4)、行列归一化 → 写 global
    else:                    # warp 1-2：做「大维度」hidden 计算
        # (C) 从 mixes_shared 算 pre_mix → apply_mix 加权压缩 → 写 layer_input
```

warp 0（threads 0-31）负责**每个 token 的标量级**工作：24 个通道的归一化、4 个 post 值、4×4 的 comb 矩阵及其 sinkhorn 行列归一化——这些数据量小（几十个标量），一个 warp 绰绰有余。

warps 1-2（threads 32-95）负责**hidden 维**工作：apply_mix 要在 `hidden_size` 上做加权求和，数据量大，需要更多线程并行。它们从 warp 0 写入的共享内存读取 mixes，再做压缩。

两路通过**共享内存 `mixes_shared`** 通信：warp 0 生产 mixes，warps 1-2 消费它做 pre_mix。生产与消费之间需要块级同步，框架在此处负责插入（kernel 源码里没有显式 `T.sync_threads`，但若不同步，warps 1-2 会读到未就绪的 mixes，结果将未定义）。

#### 4.4.3 源码精读

网格与线程数：

[pre_big_fuse_kernel.py:41](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_big_fuse_kernel.py#L41) `T.Kernel(num_tokens, threads=96)`，每 token 一个 block、96 线程。

**warp 0 第一段：RMSNorm 归一（norm_fn 的 norm 半步）**。它把 `n_splits` 个 split 的 `gemm_out_mul` 求和、`gemm_out_sqrsum` 求和算 rms，再归一化得到 `mixes`，写入共享内存：

[pre_big_fuse_kernel.py:45-58](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_big_fuse_kernel.py#L45-L58) 这里 `rms = rsqrt(Σ sqrsum / (M·H) + rms_eps)`、`mixes[j] = (Σ gemm_out_mul[j]) · rms`，正是 RMSNorm。注意它**只由前 32 个线程执行**（`T.get_thread_binding() < 32`），其余线程在此段空转。

**warp 0 第二段：split(post/comb) + sinkhorn**。从 `mixes_shared` 取值，算 post_mix（写 global）、comb 矩阵，再做 softmax + 行列迭代归一化，最后写 `comb_mix` 到 global：

[pre_big_fuse_kernel.py:60-101](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_big_fuse_kernel.py#L60-L101) sinkhorn 的实现是：先 row softmax、再交替「按行除 / 按列除」共 `sinkhorn_repeat` 次（见 u7-l3 对 sinkhorn 的专题讲解）。这里它被**内联**进同一个 kernel，而非调用独立的 sinkhorn kernel——这正是「融合」的核心。

**warps 1-2：pre_mix + apply_mix**。这是 `if thread<32` 的 `else` 分支，由 threads 32-95 执行：

[pre_big_fuse_kernel.py:102-129](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_big_fuse_kernel.py#L102-L129) 先从 `mixes_shared` 算 `pre_mix_shared`（sigmoid+eps），再在 hidden 维分块（`T.Pipelined` 双缓冲）做 `ol += pre·residual` 的加权压缩，写 `layer_input`。这与 4.3 的 apply_mix kernel 逻辑完全一致，只是内联了进来。

ops 层封装确认融合 kernel 的输入是 GEMM 产物，且 GEMM 仍是独立调用：

[pre_big_fuse.py:58-85](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/pre_big_fuse.py#L58-L85) 先调 `_mhc_pre_norm_fn_fwd_mul`（GEMM）产出 `gemm_out_mul/sqrsum`，再喂给 `_mhc_pre_big_fuse`。

注意 ops 层有一处实现现状注释：TileLang 实现不支持 split-K，所以 `n_splits` 被强制设为 1（建议用 DeepGEMM 路径获得更好性能）：

[pre_big_fuse.py:50-54](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/pre_big_fuse.py#L50-L54) 注释说明 split-K 的现状与改进方向。

#### 4.4.4 代码实践

**实践目标**：理解 warp 分工如何让「小维度」与「大维度」计算在同一 block 内并行。

**操作步骤（源码阅读型）**：

1. 读 [pre_big_fuse_kernel.py:41-129](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_big_fuse_kernel.py#L41-L129)，用两种颜色标注 warp 0 与 warps 1-2 各自执行的语句。
2. 回答：warp 0 在做 sinkhorn（[L88-97](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_big_fuse_kernel.py#L88-L97)）的同时，warps 1-2 在做什么？

**需要观察的现象**（推理）：sinkhorn 是 `mhc_mult×mhc_mult`（4×4=16）个小标量的迭代，warp 0 独立完成；与此同时 warps 1-2 在 hidden 维（成百上千个元素）上并行做 apply_mix 累加。两者**指令互不重叠**，掩盖了彼此的延迟——这就是 warp 分工（warp specialization）的收益。源码顶部的 `TL_DISABLE_WARP_SPECIALIZED` 关掉的是编译器自动的 warp-specialized 调度，这里的分工是**手写**的 `if/else` 划分，二者不冲突。

**预期结果**：能说清「warp 0 负责标量级小维度，warps 1-2 负责 hidden 大维度，通过共享内存 mixes_shared 通信」。

#### 4.4.5 为什么融合只在推理态安全（核心思辨）

这是本讲最重要的结论，单独成节。

**根本原因：融合 kernel 没有反向实现。**

证据链：

1. `mhc_pre_big_fuse` 是 ops 层的一个**普通函数**，不是 `torch.autograd.Function` 的子类，函数体内没有 `backward`、没有 `save_for_backward`：[pre_big_fuse.py:7](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/pre_big_fuse.py#L7)。
2. 因此它的输出张量**不连计算图**，无法 `backward`。
3. functional 层只在 `not torch.is_grad_enabled()` 时调用它：[functional.py:69](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L69)。

**更深层的原因：融合把中间张量留在了片上，无东西可存给反向。**

要支持 autograd 反向，每一步要么 (a) 把前向中间量 `save_for_backward` 存下来，要么 (b) 在反向里重算。但融合的设计目标恰恰是**不把 `mixes`、归一化前的 `comb` 等中间量写回 HBM**——它们只活在寄存器/共享内存里，kernel 结束就没了。于是：

- 没有中间量可 save → 路径 (a) 失败。
- 重算则需要把融合的四个阶段重新跑一遍 → 这正是训练态的另一种做法：`multilayer_recompute`（见 u7-l4），它通过跨层重算来换取显存，而非用融合 kernel。

所以两条路径各有归宿：

| 场景 | 路径 | 反向 | 中间张量 |
| --- | --- | --- | --- |
| 推理（`no_grad`） | `pre_big_fuse` 融合 | 不需要 | 全留片上，省带宽 |
| 训练（`grad`） | 四步拆分，每步独立 autograd.Function | 每步自带 backward | 中间量写 HBM 供反向使用 |

一句话总结：**融合是用「牺牲可反传性」换「带宽」，所以只能用在不需要反传的推理态；训练态必须保留可反传的拆分结构。**

#### 4.4.6 小练习与答案

**练习 1**：融合 kernel 为什么不把 `fwd_mul`（GEMM）也并进来？

**参考答案**：GEMM 是 tensor core 密集型计算，其调度、分块、流水线策略与后面的标量归一化/归约完全不同；混在一起反而难以为两类计算同时取到最优。当前设计让 GEMM 单独跑（用专门的 matmul kernel），融合 kernel 只接手其后的「归一化+切分+sinkhorn+应用」这一连串带宽/标量型工作，各取所长。

**练习 2**：如果要在训练态也用融合，需要付出什么代价？

**参考答案**：要么写一个与融合前向配套的、覆盖全部四阶段的反向 kernel（工程量大，且要妥善保存或重算 mixes 等中间量）；要么用重算策略（`multilayer_recompute`，u7-l4），在反向时重跑融合前向以重建中间量。两条路都比「四步拆分」复杂，所以训练态默认走拆分。

---

### 4.5 mhc_head 与 head_compute_mix：lm_head 的精简前处理

#### 4.5.1 概念说明

`mhc_head` 是给**最后一个语言模型头（lm_head）**准备的 pre 段变体。普通子层（attention/FFN）后面还有 post 段把输出混回多股残差；但 lm_head 是网络末端，**不需要 post/comb 回混，只需要把多股残差压成单股再做最终投影**。因此 lm_head 只用到 pre 段三段里的「pre 那一段」。

`head_compute_mix` 就是「只算 pre_mix」的精简版 split：它只做 `sigmoid(scale·mix + base) + eps`，用单个 `scale`（形状 `[1]`），输出 `mhc_mult` 个通道。

#### 4.5.2 核心流程

```
mhc_head(residual, fn[mhc_mult, mhc_mult*H], scale[1], base[mhc_mult], ...):
    mhc_mult3 = mhc_mult*(mhc_mult+2)
    if fn.shape[0] < mhc_mult3:                 # fn 只有 mhc_mult 行，不够 mhc_mult3
        fn = F.pad(fn, (0,0, 0, mhc_mult3 - fn.shape[0]))   # 补零行到 mhc_mult3
    mixes = mhc_pre_norm_fn(residual, fn, ...)  # 复用 norm_fn 的 GEMM
    mixes = mixes[..., :mhc_mult]               # 只取前 mhc_mult 个通道
    mix   = mhc_head_compute_mix(mixes, scale, base, pre_eps)   # 精简 split
    return mhc_pre_apply_mix(residual, mix.unsqueeze(-1))       # 复用 apply_mix
```

精妙之处在 **pad + 切片**：lm_head 的 `fn` 只有 `[mhc_mult, mhc_mult*H]`（行数 = `mhc_mult`），但 `mhc_pre_norm_fn` 的 GEMM kernel 期望 `fn` 有 `mhc_mult3` 行。与其另写一个小 GEMM，不如把 `fn` 用零行 pad 到 `mhc_mult3` 行，复用现有 kernel，算完再切片丢掉多余通道——多算的 `mhc_mult3 - mhc_mult` 个通道是常数零贡献，不影响正确性。

#### 4.5.3 源码精读

pad 使 fn 形状匹配 norm_fn kernel：

[functional.py:141-144](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L141-L144) `F.pad(fn, (0, 0, 0, mhc_mult3 - fn.shape[0]))` 在行方向补零。

切片取前 mhc_mult 个通道：

[functional.py:154](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L154) `mixes[..., :mhc_mult]`。

调用精简版 split（head_compute_mix）与复用 apply_mix：

[functional.py:158-160](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L158-L160) `mhc_head_compute_mix` 产出 `mhc_mult` 个门控，再 `mhc_pre_apply_mix` 压成单股。

head_compute_mix 的前向 kernel 极简——只是 sigmoid + eps：

[head_compute_mix_kernel.py:31](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/head_compute_mix_kernel.py#L31) `output = sigmoid(input*scale[0] + base) + pre_eps`。与 split_mixes 的 pre 段公式完全相同，只是 scale 维度从 3 退化到 1。

它的反向同样用 persistent kernel + split-K partial 梯度（`mhc_scale_grad_partial[num_sms,1]`、`mhc_base_grad_partial[num_sms, mhc_mult]`），再在 ops 层 `.sum(0)`：

[head_compute_mix.py:63-66](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/head_compute_mix.py#L63-L66) partial 梯度聚合。这一模式与 4.2 的 split_mixes 反向、u8-l3 的 ops 层归约一脉相承。

注意 head 路径**没有融合版本**——lm_head 通常只在最后调用一次，且其前处理就是「norm + 单段 split + apply」，开销占比小，没必要单独写融合 kernel；它走的是带 autograd 的训练态拆分逻辑。

#### 4.5.4 代码实践

**实践目标**：验证 pad+切片 的等价性。

**操作步骤（源码阅读型）**：

1. 读 [functional.py:141-160](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py#L141-L160)，确认 lm_head 只用了 pre 段。
2. 思考：pad 进去的零行，在 GEMM `fn @ residual` 中贡献为零，所以切片丢弃它们不改变 `mixes[..., :mhc_mult]` 的值。

**预期结果**：能解释「为什么 pad 零行 + 切片」与「直接用一个 `[mhc_mult, ...]` 的小 GEMM」数值等价，且前者复用了已特化的 norm_fn kernel。

#### 4.5.5 小练习与答案

**练习 1**：`mhc_head` 为什么不返回 `ctx` 元组？

**参考答案**：`mhc_head` 后面没有子层、没有 post 段，输出直接交给 lm_head 投影。post/comb 是为「子层输出回混多股」准备的，lm_head 不需要回混，所以不产出也不返回 ctx。

**练习 2**：`head_compute_mix` 的 `scale` 形状是 `[1]`，而 `split_mixes` 的是 `[3]`，为什么？

**参考答案**：split_mixes 要同时算 pre/post/comb 三段，每段一个 scale 分量，共 3 个；head_compute_mix 只算 pre 这一段，只需 1 个 scale。这是「精简版」与「完整版」在参数上的直接体现。

---

## 5. 综合实践

**任务**：画出 pre 段在训练态与推理态下的**完整数据流图**，并标注每条 HBM 读写。

**要求**：

1. **训练态**（四步拆分）：从 `residual` 开始，依次画出 `pre_norm_fn`（GEMM→RMSNorm 产出 mixes）→ `pre_split_mixes`（产出 pre/post/comb）→ `sinkhorn`（产出归一化 comb）→ `pre_apply_mix`（产出 layer_input）。在每个箭头上标注「写 HBM / 读 HBM」以及张量形状。找出至少 3 个「写出去又马上读回来」的中间张量（如 mixes、pre_mix、comb_mix）。
2. **推理态**（融合）：画出 `fwd_mul`（GEMM，独立）→ `pre_big_fuse`（融合其余全部）。标注哪些中间张量**不再经过 HBM**（留在片上）。标出 warp 0 与 warps 1-2 的分工，以及它们通过 `mixes_shared` 的通信点。
3. **思辨**：在你的图上圈出「融合省下的 HBM 往返」，并解释为什么这些节省在训练态无法直接获得（提示：反向需要中间量）。
4. （**待本地验证**，需 GPU）若有硬件，对同一组输入分别用 `torch.no_grad()` 与默认模式跑 `mhc_pre`，用 `torch.cuda.memory` 或 nsys 观察 HBM 流量差异。

**预期产出**：一张双栏对比图（手绘或工具绘制均可）+ 一段 200 字以内的说明，指出融合省下的最关键的 2-3 次中间张量往返，并点明「无 backward」是融合仅限推理的根本约束。

## 6. 本讲小结

- `mhc_pre` 是 pre 段入口，按 `torch.is_grad_enabled()` 分流：**推理走融合，训练走四步拆分**。
- 四步拆分顺序是 `pre_norm_fn → pre_split_mixes → sinkhorn → pre_apply_mix`，每步都是独立 `autograd.Function`，自带 forward/backward。
- `pre_split_mixes` 把 `mixes`（`mhc_mult3 = mhc_mult*(mhc_mult+2)` 通道）切成 pre（sigmoid+eps）、post（sigmoid·post_mult）、comb（线性，待 sinkhorn）三段；反向用 persistent kernel + split-K partial 梯度。
- `pre_apply_mix` 用 pre 段门控把多股残差加权压缩成单股；mhc 维 serial（累加依赖）、hidden 维 parallel，hidden 分块用 `Pipelined` 双缓冲。
- `pre_big_fuse` 把「RMSNorm 归一 + split + sinkhorn + apply」融进一个 kernel，用 warp 分工（warp 0 做小维度标量、warps 1-2 做 hidden 大维度），中间量留片上省带宽；**它没有反向，所以只能用于推理**。
- `mhc_head` / `head_compute_mix` 是 lm_head 的精简版：只算 pre 段，靠 pad+切片复用 norm_fn kernel。

## 7. 下一步学习建议

- **u7-l3 Sinkhorn 归一化（前向 + 自定义反向）**：本讲把 sinkhorn 当作 pre 流水线的一环一笔带过，下一讲会专题拆解它的迭代行列归一化前向，以及为什么反向需要手写（保存全部中间 xs/sums 并逆序回传）。
- **u7-l4 多层重计算 kernel**：本讲指出训练态若想省显存就得「重算」，下一讲讲 `multilayer_recompute` 如何在 mhc 训练流水线中跨层重算，与推理态的 `pre_big_fuse` 形成训练/推理两条对照路径。
- **u8-l1 autograd.Function 封装范式**：本讲的 `MHCPreSplitMixes` / `MHCPreApplyMix` 是 autograd.Function 的典型用法，u8-l1 会以 EngramGate 为例系统讲解这套封装范式（含 `save_for_backward`、`main_grad` 就地累加、返回 None）。
- 继续阅读建议：`tile_kernels/modeling/mhc/ops/` 目录下其余 op（`post.py`、`expand.py`）以及 `tile_kernels/mhc/norm_fn_kernel.py`，把整条 mhc 流水线的 forward/backward 全部串起来。
