# Sinkhorn 归一化（前向 + 自定义反向）

## 1. 本讲目标

学完本讲，读者应当能够：

1. 说清 Sinkhorn 归一化的数学含义：为什么要把一个矩阵反复按行、按列归一化，最终逼近「双随机矩阵」。
2. 读懂 `_mhc_sinkhorn_fwd` 前向 kernel：`softmax` + 行/列迭代归一化在 TileLang fragment 上的写法。
3. 解释为什么这个迭代算法**不能直接交给 PyTorch autograd**，必须手写一个独立的反向 kernel。
4. 精读 `_mhc_sinkhorn_bwd` 反向 kernel 的两大设计：前向重算（把所有中间快照存进 shared memory）+ 逆序回传（`for inv_step in T.serial(...)` 倒着走每一步）。
5. 看懂 `modeling/mhc/ops/sinkhorn.py` 如何用一个 `torch.autograd.Function` 把这两个 kernel 拼成可求导的对外 API `sinkhorn_normalize`，并理解它「只保存输入 `x`、不保存输出」的重计算（rematerialization）策略。

## 2. 前置知识

本讲承接 **u7-l1（MHC 概览）** 和 **u2-l3（循环、并行与规约原语）**。在进入正文前，先回忆几个关键概念：

- **mixes 混合系数**：在 Manifold HyperConnection（mhc）流水线里，`mhc_mult` 股残差流之间有一组可学习的混合系数。其中 `comb_mix` 是一个 \(M \times M\) 的小矩阵（\(M=\) `mhc_mult`，当前实现固定为 4），决定「四股残差流如何互相组合」。Sinkhorn 归一化作用在这个 \(M \times M\) 矩阵上，让它变成近似双随机矩阵，从而让每股残差流的「流入/流出」保持平衡。
- **fragment / shared**：来自 u2-l2。`T.alloc_fragment` 是协作布局寄存器，能做 `T.copy`、规约（`reduce_max/sum`）和 `T.Parallel` 元素循环；`T.alloc_shared` 是块内共享内存，容量大、可被动态下标索引，但写后通常要 `T.sync_threads`。
- **`T.reduce_sum(src, dst, dim=...)`**：来自 u2-l3。带 `dim` 的部分规约，沿指定维度求和并降一维。
- **`torch.autograd.Function`**：PyTorch 自定义可微算子的标准范式，需手写 `forward`/`backward`，用 `ctx.save_for_backward(...)` 显式声明反向需要的前向张量。u8-l1 会系统讲这个范式，本讲先用 Sinkhorn 当一个「复杂样板」。

> 数学记号约定：本讲用 \(x\) 表示前向输入矩阵，\(y\) 表示某一步归一化后的输出，\(\bar y\)（代码里的 `grad_frag`）表示损失对 \(y\) 的梯度（上游梯度），\(\bar x\) 表示对 \(x\) 的梯度（下游梯度）。行规约记「除以行和」，列规约记「除以列和」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/mhc/sinkhorn_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py) | 两个 TileLang kernel 构造器：`_mhc_sinkhorn_fwd`（前向）与 `_mhc_sinkhorn_bwd`（反向）。本讲的主要精读对象。 |
| [tile_kernels/modeling/mhc/ops/sinkhorn.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py) | `torch.autograd.Function` 封装 `_SinkhornNormalize` 与对外函数 `sinkhorn_normalize`，把 fwd/bwd 两个 kernel 缝合成一个可求导算子。 |
| [tile_kernels/torch/mhc.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/mhc.py) | 纯 PyTorch 参考实现 `sinkhorn_normalize_ref`，供测试对拍。 |
| [tests/mhc/test_sinkhorn.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_sinkhorn.py) | 正确性测试：用参考实现对拍前向输出与反向梯度。 |
| [tile_kernels/modeling/mhc/functional.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/functional.py) | 调用点：`mhc_pre` 在训练态把 `comb_mix` 喂给 `sinkhorn_normalize`。 |

调用关系一句话：`functional.mhc_pre`（训练分支）→ `sinkhorn_normalize`（ops 层 API）→ `_SinkhornNormalize.apply`（autograd.Function）→ `_mhc_sinkhorn_fwd` / `_mhc_sinkhorn_bwd`（TileLang kernel）。

## 4. 核心概念与源码讲解

### 4.1 Sinkhorn 归一化的数学：把矩阵推向双随机

#### 4.1.1 概念说明

一个 \(n \times n\) 矩阵 \(P\) 若满足「每个元素非负、每行元素之和为 1、每列元素之和为 1」，就叫**双随机矩阵（doubly stochastic matrix）**。双随机矩阵有一个美妙性质：用它去做线性组合 \(P z\)，既不会让某一维「吃掉」所有能量（行和为 1），也不会让某一维「泄漏」能量（列和为 1），整体是一个平衡的重分配。

**Sinkhorn 定理**告诉我们：给定任意一个元素为正的方阵 \(A\)，反复交替地「按行归一化、按列归一化」，序列会收敛到一个双随机矩阵 \(P = D_1 A D_2\)（\(D_1, D_2\) 是对角缩放阵）。这就是 **Sinkhorn 归一化（Sinkhorn normalization）**。

在 mhc 里，`comb_mix` 是那 \(M \times M\)（\(M=4\)）的混合系数矩阵。让它逼近双随机，相当于强制「四股残差流之间互相混合的总强度是平衡的」，避免某一流独大或被饿死。这节课只看「怎么归一化」，不证收敛性。

#### 4.1.2 核心流程

项目里的 Sinkhorn 归一化不是教科书最朴素版本，而是带 `softmax` 预处理和 `eps` 防除零的工程版本。前向一共这几步（设 \(X\) 为输入 \(M \times M\) 矩阵，\(\varepsilon=\) `eps`，\(R=\) `repeat`）：

1. **softmax 预处理 + \(\varepsilon\) 偏移**：先沿最后一维（列方向）做 softmax，保证非负且每行和为 1，再加一个小常数 \(\varepsilon\) 保证后续不会被零除：
   \[
   X \leftarrow \mathrm{softmax}(X,\,-1) + \varepsilon
   \]
2. **首次列归一化**：除以每列之和（沿倒数第二维求和）：
   \[
   X \leftarrow X \,/\, \bigl(\textstyle\sum_{-2} X + \varepsilon\bigr)
   \]
3. **迭代 \(R-1\) 次**，每次先按行、再按列归一化：
   \[
   X \leftarrow X \,/\, \bigl(\textstyle\sum_{-1} X + \varepsilon\bigr),\qquad
   X \leftarrow X \,/\, \bigl(\textstyle\sum_{-2} X + \varepsilon\bigr)
   \]

经过「1 次 softmax + 1 次列 + \((R-1)\) 次（行 + 列）」，矩阵近似双随机。注意：因为有 \(\varepsilon\) 偏移，严格说是「带正下界的近似双随机」，这正是测试对拍时参考实现要逐位复刻的细节。

#### 4.1.3 源码精读

纯 PyTorch 参考实现把上面的流程写得极简，是理解 kernel 的最好脚手架：

```python
def sinkhorn_normalize_ref(x: torch.Tensor, repeat: int = 10, eps: float = 1e-6) -> torch.Tensor:
    x = x.softmax(-1) + eps
    x = x / (x.sum(-2, keepdim=True) + eps)
    for _ in range(repeat - 1):
        x = x / (x.sum(-1, keepdim=True) + eps)
        x = x / (x.sum(-2, keepdim=True) + eps)
    return x
```

> 见 [tile_kernels/torch/mhc.py:8-14](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/mhc.py#L8-L14)：这就是前向 kernel 要在 GPU 上复刻的「黄金标准」。

记号提醒：`sum(-1)` 是沿最后一维（行内求和 → 行归一的除数），`sum(-2)` 是沿倒数第二维（列内求和 → 列归一的除数）。后面读 kernel 时，`dim=2` 对应 `sum(-1)`（行和），`dim=1` 对应 `sum(-2)`（列和）。

#### 4.1.4 代码实践

**实践目标**：用最小例子亲眼看到「交替行/列归一化让矩阵逼近双随机」。

**操作步骤**（纯 CPU PyTorch 即可，不需要 GPU）：

```python
import torch
torch.manual_seed(0)
A = torch.rand(4, 4) * 5 + 0.1        # 4x4 正矩阵（模拟 M=4 的 comb_mix）
X = A.clone()
for it in range(6):
    X = X / (X.sum(-1, keepdim=True))   # 行归一
    X = X / (X.sum(-2, keepdim=True))   # 列归一
    print(f"iter {it}: 行和偏差={ (X.sum(-1)-1).abs().max():.2e}  列和偏差={ (X.sum(-2)-1).abs().max():.2e}")
```

**需要观察的现象**：随着迭代次数增加，行和与列和都迅速逼近 1（偏差按指数下降）。

**预期结果**：到第 4~5 次迭代，行和/列和偏差通常已小于 \(10^{-5}\)。这解释了为什么默认 `repeat=10` 已经远超收敛所需——多出来的迭代主要是为了让「带 \(\varepsilon\)」的工程版本和朴素版本在数值上充分对齐。

> 如果无法在本地运行，明确标注「待本地验证」；上述脚本是标准 PyTorch，结果可直接复现。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `softmax(-1)` 这一步去掉，直接从原始 `comb_mix`（可能含负值）开始行/列归一化，会发生什么？

**参考答案**：行/列归一化是「除以和」，若和为 0 或负会导致除零、符号翻转或发散；softmax 既保证非负又保证每行和为 1，是让 Sinkhorn 迭代稳定的必要预处理。

**练习 2**：为什么每一步除法分母里都要单独加一个 \(\varepsilon\)，而不是只在第一步加一次？

**参考答案**：因为每一步的「和」都是对当前 \(X\) 重新求的，不同位置、不同步的和都可能接近 0；每步加 \(\varepsilon\) 是逐次防除零，保证整个迭代链都不会产生 inf/nan。

---

### 4.2 前向 kernel 精读：softmax + 行列迭代

#### 4.2.1 概念说明

前向 kernel `_mhc_sinkhorn_fwd` 把 4.1 的 PyTorch 参考搬到 GPU 上。它是一个标准的 TileLang 算子（骨架见 u2-l1）：最外层 `@tilelang.jit(pass_configs=...)`，构造器接收编译期参数 `hidden_size / token_block_size / repeat / eps`，内层 `@T.prim_func` 描述每个 block 的计算。

这里有个**命名陷阱**要预先点破：kernel 形参里的 `hidden_size`，在 mhc 实际调用中取的是 `x.shape[1]`，而 `x` 被 `view(-1, M, M)` 过（见 4.4 节），所以这个 `hidden_size` 其实等于 \(M=\)`mhc_mult`=4，也就是被归一化矩阵的边长，**不是** Transformer 的 hidden_size。读源码时把它当成「矩阵边长 H」即可。

#### 4.2.2 核心流程

每个 block 处理 `token_block_size` 个 token，每个 token 一个 \(H \times H\) 矩阵：

```
读输入 comb_res_mix[该批 token] → comb_frag (fragment, 形状 token_block×H×H)
# 第 0 步：softmax(dim=2) + eps
  row_max = reduce_max(comb_frag, dim=2)          # 数值稳定用
  comb_frag = exp(comb_frag - row_max)
  row_sum  = reduce_sum(comb_frag, dim=2)
  comb_frag = comb_frag / row_sum + eps
# 第 1 步：首次列归一化
  col_sum  = reduce_sum(comb_frag, dim=1)
  comb_frag = comb_frag / (col_sum + eps)
# 第 2 步起：repeat-1 轮，每轮 先行后列
  for _ in serial(repeat - 1):
      row_sum = reduce_sum(comb_frag, dim=2); comb_frag /= (row_sum + eps)
      col_sum = reduce_sum(comb_frag, dim=1); comb_frag /= (col_sum + eps)
写输出 comb_res_mix_out[该批 token] ← comb_frag
```

全程在一个 `comb_frag` fragment 上**原地（in-place）反复规约与除法**，没有开辟多个中间缓冲——这是前向省显存的关键，但也是它无法走 autograd 的根因之一（见 4.3）。

#### 4.2.3 源码精读

构造器与网格定义。注意 `num_tokens` 是运行时符号（`T.dynamic`），而 `hidden_size / token_block_size / repeat / eps` 是编译期常量，被烤进产物：

[til_kernels/mhc/sinkhorn_kernel.py:10-24](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L10-L24) —— `@tilelang.jit` 装饰的前向构造器，`num_tokens=T.dynamic(...)` 声明运行时维度，`with T.Kernel(T.ceildiv(num_tokens, token_block_size))` 定义一维网格（一个 block 处理一批 token）。

softmax 预处理 + eps（对应参考的第 1 行）：

[til_kernels/mhc/sinkhorn_kernel.py:31-38](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L31-L38) —— 先 `reduce_max` 拿到每行最大值做数值稳定，再 `exp(x - row_max)`、`reduce_sum` 求行和，最后 `comb_frag / row_sum + eps`。这里 `+ eps` 在除法**之后**，与参考 `x.softmax(-1) + eps` 完全一致。

首次列归一化（对应参考第 2 行）：

[til_kernels/mhc/sinkhorn_kernel.py:40-43](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L40-L43) —— `reduce_sum(..., dim=1)` 是沿倒数第二维（列和），除以 `col_sum[i,k] + eps`。

迭代体（对应参考的 for 循环）：

[til_kernels/mhc/sinkhorn_kernel.py:45-54](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L45-L54) —— `T.serial(repeat - 1)` 因为前后两轮之间有数据依赖（下一轮读这一轮写出的 `comb_frag`），不能用 `T.Parallel`；每轮内先 `dim=2` 行归一再 `dim=1` 列归一。

> 注意 `T.serial` 而非 `T.Parallel`：迭代轮次之间是串行依赖，这是 u2-l3 强调的「轮间有依赖选 serial」的典型场景。

#### 4.2.4 代码实践

**实践目标**：用项目自带测试验证前向 kernel 与参考实现数值一致。

**操作步骤**：

1. 打开 [tests/mhc/test_sinkhorn.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/mhc/test_sinkhorn.py)，找到 `test_sinkhorn_comprehensive`（L33-43）。
2. 阅读它的对拍逻辑：`_tester` 同时跑 `sinkhorn_normalize`（被测）和 `sinkhorn_normalize_ref`（参考），用 `torch.testing.assert_close` 比对前向输出 `out` 与反向梯度 `grad`。
3. 在有 SM90/SM100 GPU + 已 `pip install -e ".[dev]"` 的环境里运行单测：
   ```
   pytest tests/mhc/test_sinkhorn.py -v
   ```

**需要观察的现象**：不同 `n0`（批）、`n1`（token 数）、`mhc=4` 组合下，前向 `out_tl` 与 `out_ref` 都 `assert_close` 通过。

**预期结果**：全部用例通过。注意测试同时校验了**反向梯度**——这正是下一节要讲的复杂部分。

> 若本地无 GPU，这一步标注「待本地验证」；重点是理解对拍范式：参考实现 = `sinkhorn_normalize_ref`，被测 = 经 `autograd.Function` 封装的 kernel，断言用浮点 `assert_close`（不是位精确，因为浮点迭代有微小差异）。

#### 4.2.5 小练习与答案

**练习 1**：前向 kernel 里 `comb_frag` 是 `alloc_fragment`，而反向 kernel 里快照缓冲 `xs/sums` 用的是 `alloc_shared`。为什么快照必须放 shared 而不能放 fragment？

**参考答案**：`xs/sums` 要按运行时下标 `2*repeat-1-inv_step` 访问（虽然 `repeat` 是编译期常量、`inv_step` 会被 `T.serial` 展开，但快照总共有 `2*repeat` 份、每份是整片 `token_block×H×H`，体量远超寄存器预算）；shared memory 容量大且支持多份切片存放，适合做这种「多步快照」缓冲。fragment 留给「当前正在算的那一份」（如 `x_inter`、`grad_frag`）。

**练习 2**：把 `repeat` 从 10 改成 1，前向会退化成什么？

**参考答案**：`T.serial(repeat-1)` 变成 `serial(0)` 不执行，前向只做「softmax + eps + 首次列归一化」，即只保证行和≈1、做了一次列归一，远未到双随机。

---

### 4.3 为什么必须手写反向：迭代算法与 autograd 的冲突

#### 4.3.1 概念说明

这是本讲最关键的「为什么」。读者可能会问：PyTorch 不是能自动求导吗，为什么 Sinkhorn 要单独写一个 `_mhc_sinkhorn_bwd` kernel？有三个层层递进的原因：

1. **前向是编译后的 CUDA，autograd 看不见内部**。`_mhc_sinkhorn_fwd` 是一个 `@T.prim_func` 编译出的不透明 kernel。PyTorch autograd 只会对「用 PyTorch 算子组合、且 `requires_grad=True` 的张量操作」自动建图，对一个黑盒 CUDA kernel 没有任何求导规则。要让 `sinkhorn_normalize` 可微，要么用参考实现那种纯 PyTorch 写法（慢），要么手写反向 kernel（快）。

2. **即便强行用纯 PyTorch autograd，也会又慢又爆显存**。Sinkhorn 是迭代算法，每一步都依赖上一步的结果。autograd 为了能反传，需要把 `repeat` 轮里**每一步的中间张量和它的计算图**都保留下来，反向时再逆序遍历。这意味着显存随 `repeat` 线性增长，且每一步都是小算子、kernel launch 开销巨大。

3. **前向用了大量 fragment 上的原地更新**。如 4.2 所述，`comb_frag[i,j,k] = comb_frag[i,j,k] / (row_sum + eps)` 这种原地改写会破坏 autograd 的版本计数（version counter），autograd 根本无法正确追踪。

所以项目选择的方案是：**手写一个独立的反向 kernel，它只接收上游梯度 `grad_output` 和前向输入 `x`，在 kernel 内部把整条前向链「重算一遍」并逆序回传**。这就是经典的 **重计算 / 激活检查点（rematerialization / activation checkpointing）** 思想——用算力换显存：不保存任何中间激活，反向时重新算出来。

#### 4.3.2 核心流程

反向 kernel 的整体结构是「先重算前向、再逆序求导」两段：

```
# 输入：grad_output（上游梯度）、x（前向原始输入）；输出：grad_input
# ===== 第一段：前向重算，把每一步的 (矩阵快照 xs[t], 归一化和 sums[t]) 存进 shared =====
从 x 出发，按前向顺序重做：
  softmax（纯，不加 eps）          → xs[0]
  + eps                            → xs[1]
  首次列归一化（记录列和 sums[1]）
  对 repeat-1 轮：行归一（记录行和 sums[2r+2]、快照 xs[2r+2]）、列归一（记录列和 sums[2r+3]、快照 xs[2r+3]）
# ===== 第二段：逆序回传，for inv_step 倒着遍历每一步 =====
for inv_step in serial(2*repeat - 1):
    取出该步的快照 x_inter = xs[2*repeat-1-inv_step] 和归一化和
    按行/列归一化的反向公式更新 grad_frag
最后再过一遍 softmax 的反向（用 xs[0]）
写回 grad_input
```

关键设计点：**第一段把前向每一步的「除数 s」和「除之前的矩阵快照 x」都存下来**，这样第二段逆序时每一步的反向公式都能用上「当时的 \(x\)」和「当时的 \(s\)」，不需要重新求和。

#### 4.3.3 源码精读

先看反向 kernel 只保存输入 `x`、不保存前向输出这一事实——这是重计算的直接体现：

[til_kernels/modeling/mhc/ops/sinkhorn.py:18-21](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py#L18-L21) —— `ctx.save_for_backward(x)` 只存了原始输入 `x`；前向输出 `output` 没存，因为反向 kernel 会从 `x` 重新算出所有中间量。

反向 kernel 入参与快照缓冲声明：

[til_kernels/mhc/sinkhorn_kernel.py:71-94](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L71-L94) —— 三个张量形参 `grad_output / x / grad_input`；`xs = alloc_shared((repeat*2, token_block, H, H))` 存 \(2R\) 份矩阵快照，`sums = alloc_shared((repeat*2, token_block, H))` 存对应的归一化和。

> 为什么是 `repeat*2` 份？因为前向有「softmax（xs[0]）+ eps（xs[1]）+ 1 次列 + (R-1) 次（行+列）」，其中每一步归一化对应一个快照，总快照数恰为 \(2R\)（xs[0..2R-1]）。sums[0] 留空未用，因为 softmax 那步的「和」在反向里即时重算（见 4.4）。

#### 4.3.4 代码实践

**实践目标**：手算「行/列归一化」这一步的反向公式，理解逆序回传里那个 `(grad - sum2)/(s+eps)` 是怎么来的。

**操作步骤**（纸笔推导，对照 kernel 验证）：

1. 设某一步前向是 \(y_j = x_j / (s + \varepsilon)\)，其中 \(s = \sum_k x_k\)（沿归约维求和，**注意 \(s\) 本身依赖所有 \(x_i\)**）。
2. 求 \(\partial y_j / \partial x_i\)。提示：\(\partial s / \partial x_i = 1\)（\(s\) 里每个 \(x_i\) 出现一次）。
3. 由链式法则 \(\bar x_i = \sum_j \bar y_j \cdot \partial y_j / \partial x_i\)，推出 \(\bar x_i\) 用 \(\bar y_i, x_j, \bar y_j, s\) 表达的形式。

**需要观察的现象**：你会得到
\[
\bar x_i = \frac{1}{s+\varepsilon}\left(\bar y_i \;-\; \frac{\sum_j \bar y_j\, x_j}{s+\varepsilon}\right)
\]
令 \(\text{sum2} := \bigl(\sum_j \bar y_j x_j\bigr)/(s+\varepsilon)\)，则 \(\bar x_i = (\bar y_i - \text{sum2})/(s+\varepsilon)\)。

**预期结果**：这正是 kernel 里 `(grad_frag - col_sum2) / (col_sum + eps)` 的来源——其中 `temp = grad_frag * x_inter` 算 \(\bar y_j x_j\)，`reduce_sum` 算 \(\sum_j\)，再除以 \((s+\varepsilon)\) 得 `sum2`。

> 这一步的「坑」在于：分母 \(s\) 也依赖 \(x\)，所以反向**不是**简单地把梯度除以 \(s\)，而是多了一项 \(-\text{sum2}\) 的修正。这也是为什么反向 kernel 必须把每一步的 \(x\)（`xs[...]`）和 \(s\)（`sums[...]`）都存下来。

#### 4.3.5 小练习与答案

**练习 1**：如果某一步的归一化是「除以一个**与 \(x\) 无关**的常数 \(c\)」，即 \(y_j = x_j / c\)，它的反向会简化成什么？对比之下，Sinkhorn 的反向多了什么？

**参考答案**：\(\bar x_i = \bar y_i / c\)，只是一个逐元素除法，没有 \(-\text{sum2}\) 修正项。Sinkhorn 的分母 \(s=\sum x\) 依赖 \(x\)，所以多出了 \(-(\sum \bar y x)/s\) 这一项来补偿「分母也随 \(x\) 变化」的梯度。

**练习 2**：反向 kernel 选择「重算前向」而非「前向存中间、反向直接用」。这两种策略各自的代价是什么？

**参考答案**：「前向存中间」省反向算力，但显存随 `repeat` 线性增长，且要在 autograd 图里挂住每一步（对 TileLang fragment 原地更新尤其困难）；「重算前向」（本项目选择）多花一份前向算力，但反向只需保存原始输入 `x`，显存恒定。对 `repeat=10`、矩阵只有 \(4\times4\) 的小算子，算力很便宜，重算显然更划算。

---

### 4.4 反向 kernel 精读：逆序回传与 autograd 封装

#### 4.4.1 概念说明

本模块把 4.3 的两段流程在源码里落地，并把两个 kernel 缝进 `torch.autograd.Function`。重点有三：

- **第一段（前向重算）**怎么把快照写进 `xs/sums`，且下标编排要和第二段的逆序对齐。
- **第二段（逆序回传）**那个 `for inv_step in T.serial(2*repeat-1)` 怎么用 `inv_step % 2` 区分行/列步，怎么和 `xs/sums` 的下标 `2*repeat-1-inv_step` 配合。
- **autograd 封装**怎么把 fwd/bwd 两个 kernel 绑成一个可微算子，以及 `token_block_size` 前向取 1、反向取 32 这个有趣的差异。

#### 4.4.2 核心流程

**前向重算段**（在反向 kernel 内，从 `x` 出发）的下标编排：

```
softmax(纯) → xs[0]                 # 不加 eps，留给 softmax 反向用
+ eps       → xs[1]                 # 之后做首次列归一
首次列归一:  sums[1] = 列和(x_frag) ; x_frag /= (sums[1]+eps)
for step in serial(repeat-1):
    行归一: sums[step*2+2] = 行和 ; xs[step*2+2] = x_frag(归一前) ; x_frag /= (sums[...]+eps)
    列归一: sums[step*2+3] = 列和 ; xs[step*2+3] = x_frag(归一前) ; x_frag /= (sums[...]+eps)
```

规律：**奇数下标 = 列步（col），偶数下标 ≥2 = 行步（row）**，xs[0] 是 softmax、xs[1] 是 softmax+eps。最后写到 `xs[2R-1]` 为止。

**逆序回传段**：

```
for inv_step in serial(2*repeat - 1):      # 共 2R-1 步，倒着覆盖 xs[2R-1]→xs[1]
    x_inter = xs[2*repeat - 1 - inv_step]   # 该步归一化「之前」的矩阵
    if inv_step % 2 == 0:   # 列步反向（对应奇数下标 xs[2R-1], xs[2R-3], ...）
        s = sums[2*repeat - 1 - inv_step]
        sum2 = reduce_sum(grad_frag * x_inter, dim=1) / (s + eps)
        grad_frag = (grad_frag - sum2) / (s + eps)
    else:                   # 行步反向（对应偶数下标 xs[2R-2], xs[2R-4], ...）
        s = sums[2*repeat - 1 - inv_step]
        sum2 = reduce_sum(grad_frag * x_inter, dim=2) / (s + eps)
        grad_frag = (grad_frag - sum2) / (s + eps)
# 循环结束后，grad_frag 已是「对 softmax+eps 的梯度」；+eps 对梯度是恒等，故直接过 softmax 反向：
x_inter = xs[0]
grad_frag = (grad_frag - reduce_sum(grad_frag * x_inter, dim=2)) * x_inter
```

`inv_step % 2` 与下标奇偶性的对应：`2*repeat-1-inv_step` 当 `inv_step` 偶数时为奇数（列步），当 `inv_step` 奇数时为偶数（行步），正好和第一段的「奇=列、偶=row」对上。softmax 的反向用的是标准 softmax Jacobian-vector 公式 \(\bar x = (\bar y - \sum \bar y\cdot y)\cdot y\)，其中 \(y\) 就是 `xs[0]`（纯 softmax 输出）。

#### 4.4.3 源码精读

前向重算段（softmax→xs[0]、+eps→xs[1]、首次列归一、迭代行/列）：

[til_kernels/mhc/sinkhorn_kernel.py:96-127](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L96-L127) —— 注意 L104 `T.copy(x_frag, xs[0,...])` 存的是**未加 eps 的纯 softmax**，L107 存加了 eps 的版本；L111 `T.copy(col_sum, sums[1,...])` 把首次列和存好；L118-119、L124-125 在迭代里把行/列和与归一化前的快照成对存下。

逆序回传主循环：

[til_kernels/mhc/sinkhorn_kernel.py:131-151](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L131-L151) —— `for inv_step in T.serial(2 * repeat - 1)`，用 `T.copy(xs[...], x_inter)` 把 shared 里的快照拷回 fragment 再算（规约与元素循环都在 fragment 上做）；`inv_step % 2 == 0` 走列分支（`dim=1`）、否则走行分支（`dim=2`）。两分支结构完全对称，正是 4.3.4 推出的 \((\bar y - \text{sum2})/(s+\varepsilon)\)。

softmax 反向与写回：

[til_kernels/mhc/sinkhorn_kernel.py:153-163](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L153-L163) —— L154 取 `xs[0]`（纯 softmax），L156-160 实现 \(\bar x = (\bar y - \textstyle\sum \bar y y)\cdot y\)，L163 把最终 `grad_frag` 写回 `grad_input`。

最后看 autograd 封装与一个有趣的「前向 1、反向 32」差异：

[til_kernels/modeling/mhc/ops/sinkhorn.py:8-21](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py#L8-L21) —— `forward` 里 `fwd_kernel = _mhc_sinkhorn_fwd(hidden_size, 1, repeat, eps)`（`token_block_size=1`）、`bwd_kernel = _mhc_sinkhorn_bwd(hidden_size, 32, repeat, eps)`（`token_block_size=32`），并把编译好的 `bwd_kernel` 挂到 `ctx` 上（反向直接调用，不必重新编译）。反向 kernel 每 block 处理 32 个 token，是为了把昂贵的「前向重算 + 逆序回传」在更少的 block 里摊销、降低 launch 开销，同时 `repeat*2 × 32 × H × H` 的 shared 开销仍在 SMEM 预算内（H=4 时约几十 KB）。

[til_kernels/modeling/mhc/ops/sinkhorn.py:23-32](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py#L23-L32) —— `backward` 取回 `x`，分配 `grad_input`，调用 `ctx.bwd_kernel(grad_output, x, grad_input)`；返回 `(grad_input, None, None)` 与 `forward` 的三个输入 `(x, repeat, eps)` 逐位对应（`repeat`、`eps` 是非张量参数，梯度为 `None`）。对外函数 `sinkhorn_normalize` 把任意 `(..., M, M)` 输入 `view(-1, M, M)` 喂给 `apply`，再用 `view_as` 还原形状。

> `pass_configs` 里 `TL_DISABLE_WARP_SPECIALIZED=True`（见 [sinkhorn_kernel.py:5-7](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py#L5-L7)）关掉了 Hopper 的 warp 特化，因为这个 kernel 全是 fragment 上的规约与元素循环，没有典型 GEMM 那种「生产者-消费者」warp 分工，关掉反而更稳。这个开关会在 u10-l2 系统讲。

#### 4.4.4 代码实践

**实践目标**：跟读反向 kernel 中 `xs[step]/sums[step]` 的保存与 `for inv_step` 逆序循环，画出完整的反向计算图，并解释「为什么不能直接交给 autograd」。

**操作步骤**（源码阅读型实践，以 `repeat=3` 为例手算下标）：

1. 取 `repeat=3`，则 `2*repeat=6`，快照下标范围 xs[0..5]、sums[0..5]。
2. 按 4.4.2 的编排，**列出第一段每一步写下标**：
   - softmax → xs[0]
   - +eps → xs[1]
   - 首次列归一（sums[1]）
   - step=0：行（sums[2], xs[2]）→ 列（sums[3], xs[3]）
   - step=1：行（sums[4], xs[4]）→ 列（sums[5], xs[5]）
3. 再列第二段 `inv_step=0..4`（`2*repeat-1=5` 步），每步读的下标 `2*repeat-1-inv_step = 5,4,3,2,1`，标出是行还是列：
   - inv_step=0 → xs[5]/sums[5]，inv_step%2==0 → **列**
   - inv_step=1 → xs[4]/sums[4]，inv_step%2==1 → **行**
   - inv_step=2 → xs[3]/sums[3]，列
   - inv_step=3 → xs[2]/sums[2]，行
   - inv_step=4 → xs[1]/sums[1]，列
   - 循环外：softmax 反向用 xs[0]
4. 把第 2、3 步画成一张「前向重算（正序写）+ 逆序回传（倒序读）」的对偶图，箭头标出每步的 `s` 与 `x_inter` 来源。

**需要观察的现象**：逆序段的下标序列 `5,4,3,2,1` 恰好是「列、行、列、行、列」，与第一段的写入顺序「softmax(0), eps(1), 列(1), 行(2), 列(3), 行(4), 列(5)」完美对偶——最后写入的 xs[5]（最后一次列归一前）最先被反向消费。

**预期结果**：你得到的反向计算图应是一条从 `grad_output` 出发、依次经过「列→行→列→行→列→softmax」六站、最终落到 `grad_input` 的链，每一站都用 `(grad - sum2)/(s+eps)`（softmax 站用 `(grad - Σgrad·y)·y`）。**不能交给 autograd 的原因**：①前向是编译 CUDA 黑盒，无求导规则；②迭代每步在 fragment 上原地更新，破坏 autograd 版本计数；③若用纯 PyTorch autograd 要保留 `repeat` 份中间张量，显存与 launch 开销都不可接受——故采用「只存 `x`、反向重算」的 rematerialization 方案。

> 若想在本地进一步验证，可在 `repeat=3` 下对照运行 `sinkhorn_normalize` 与 `sinkhorn_normalize_ref` 的反向，用 `torch.autograd.gradcheck`（需 double 精度、小矩阵）确认梯度数值正确——标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：反向 kernel 第一段把 softmax 结果存了两份：xs[0]（纯 softmax）和 xs[1]（softmax+eps）。为什么 softmax 的反向（循环外那段）要用 xs[0] 而不是 xs[1]？

**参考答案**：softmax 反向公式 \(\bar x = (\bar y - \sum \bar y y)\cdot y\) 里的 \(y\) 必须是「和为 1 的纯概率」，即 xs[0]；xs[1] 多了 eps，每行之和不再是 1，代入公式会出错。两份快照分别服务「softmax 反向」和「首次列归一反向」，各取所需。

**练习 2**：封装层 `forward` 只 `save_for_backward(x)`，不存 `output`。如果改成「存 output、不存 x」，反向 kernel 还能工作吗？

**参考答案**：不能。反向 kernel 的第一段是从原始输入 `x` 出发重算所有中间快照的；若只存 output（已是归一化末态），无法还原出每一步的 `xs[t]/sums[t]`（信息已丢失）。这正是「重计算」策略要求必须保存输入 `x` 的原因。

**练习 3**：`backward` 返回三元组 `(grad_input, None, None)`，这三个位置分别对应 `forward` 的哪三个输入？为什么后两个是 `None`？

**参考答案**：分别对应 `forward(ctx, x, repeat, eps)` 的 `x`、`repeat`、`eps`。`repeat` 和 `eps` 是 Python 标量（非张量），没有梯度概念，故返回 `None`；只有 `x` 需要梯度，即 `grad_input`。

---

## 5. 综合实践

把本讲的知识串起来，完成一个「迷你 Sinkhorn + 自定义反向」的纸面工程演练：

**任务**：假设你要为一个**没有 eps** 的朴素 Sinkhorn（前向只有 softmax + 行列迭代，不加 eps）设计反向 kernel 的下标编排。

1. 写出前向步骤序列（softmax 后跟多少次行/列归一，取决于 `repeat`）。
2. 决定 `xs/sums` 需要多少份快照、下标如何编排（参考本讲的「奇=列、偶=row」规律）。
3. 写出逆序回传循环的上下界与 `inv_step % 2` 的行/列判定（注意：没有 eps 后，公式里所有 `+eps` 都去掉，但 `(grad - sum2)/s` 的结构不变）。
4. 讨论：去掉 eps 后，哪些地方可能出现数值不稳定？这解释了项目版本为什么坚持每步都加 eps。

**参考要点**：
- 无 eps 版前向 = softmax + 首次列 + (R-1)×(行+列)，快照数仍是 \(2R\)（softmax 一份 + 每次归一前一份），编排可完全照搬。
- 逆序循环仍是 `serial(2R-1)`，公式把 `/(s+eps)` 全换成 `/s`、softmax 反向不变。
- 去掉 eps 后，若某行/列和恰为 0（理论上有 softmax 预处理后不会，但浮点误差下边缘情况仍可能极小），会出现除零或极大梯度；项目坚持每步加 eps 正是为此兜底。

> 这是一个「源码阅读 + 设计推演」型综合任务，不需要 GPU；目的是让读者把「前向顺序写快照 ↔ 反向逆序读快照」这套对偶设计内化，并能迁移到同类迭代算法（如 u7-l4 的 multilayer_recompute）。

## 6. 本讲小结

- Sinkhorn 归一化通过「softmax + 交替行/列除法」把 \(M\times M\) 的 `comb_mix` 推向近似双随机矩阵，平衡 mhc 多股残差流的混合强度。
- 前向 kernel `_mhc_sinkhorn_fwd` 在单个 `comb_frag` fragment 上原地迭代（`T.serial` 串行，因轮间有依赖），`repeat/eps/hidden_size` 是编译期常量、`num_tokens` 是运行时符号。
- 反向**必须手写**：前向是编译 CUDA 黑盒（无 autograd 规则）、fragment 原地更新破坏版本计数、纯 PyTorch autograd 又会爆显存——故采用「只存输入 `x`、反向重算」的 rematerialization 策略。
- 反向 kernel `_mhc_sinkhorn_bwd` 两段式：先按前向顺序把每步的矩阵快照 `xs[t]` 与归一化和 `sums[t]` 写进 shared，再 `for inv_step in serial(2R-1)` 用 `inv_step%2` 区分行/列、按 \((\bar y-\text{sum2})/(s+\varepsilon)\) 逆序回传，最后用 `xs[0]` 过 softmax 反向。
- 行/列归一化的反向公式里，分母 \(s=\sum x\) 依赖 \(x\)，故比「除以常数」多出 \(-(\sum \bar y x)/s\) 修正项——这是必须保存每步 `x` 与 `s` 的根本原因。
- `torch.autograd.Function` 把两个 kernel 缝成 `sinkhorn_normalize`：前向 `token_block_size=1`、反向 `token_block_size=32`（摊销重算开销），`backward` 返回 `(grad_input, None, None)` 与三个前向输入逐位对应。

## 7. 下一步学习建议

- **横向对比另一种「迭代算法手写反向」**：阅读 [tile_kernels/mhc/multilayer_recompute_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/multilayer_recompute_kernel.py)（u7-l4），看它如何处理跨层重算，对比两者「保存什么、重算什么」的取舍。
- **系统化 autograd 封装范式**：本讲的 `_SinkhornNormalize` 是一个复杂样板；更简洁的标准范式见 u8-l1（以 `EngramGateFn` 为例的 `torch.autograd.Function` 教程），建议接着读。
- **推理态的融合替代**：u7-l2 提到推理（`no grad`）时 `mhc_pre` 走 `pre_big_fuse` 四合一融合 kernel，Sinkhorn 被吸收进大融合里。阅读 [tile_kernels/mhc/pre_big_fuse_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/pre_big_fuse_kernel.py) 看融合版如何省掉 Sinkhorn 的中间访存，并理解「融合换带宽、牺牲可反传」这一贯穿 mhc 模块的设计哲学。
