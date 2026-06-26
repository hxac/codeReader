# 高效多头注意力实现对比

## 1. 本讲目标

在 u3-l3 里，我们已经从零写出了 `MultiHeadAttention`（第 3 章的「教学版」），它能正确计算多头因果注意力。但在真实工程中，**同一个数学公式有多种写法**，它们的运行速度和内存占用可能差好几倍。

本讲不增加任何新「模型能力」，而是带你看懂仓库里那本「注意力实现大比拼」的笔记本，学完后你应当能够：

- 说出朴素注意力实现为什么在**内存**上吃亏，以及 FlashAttention 解决了什么问题；
- 掌握 PyTorch 的 `torch.nn.functional.scaled_dot_product_attention`（简称 **SDPA**），并区分 `is_causal` / `attn_mask` / `need_weights` 这几个开关各自的作用；
- 用 `torch.allclose` 验证「不同写法算出来的是同一个东西」，并用计时、内存测量来**评估**而非空谈「谁更快」。

一句话：本讲把「能跑的注意力」升级成「能在 GPU 上又快又省地跑的注意力」，为 u9-l3（GQA / MLA / SWA）和 u10 的现代 LLM 架构打地基。

## 2. 前置知识

阅读本讲前，请确保你已掌握以下概念（不熟悉可回看对应讲义）：

- **缩放点积注意力**与**因果掩码**（见 u3-l1、u3-l2）：注意力公式 \(\text{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}\right)V\)，以及用上三角掩码把未来 token 的注意力分数置为 \(-\infty\)。
- **多头注意力**（见 u3-l3）：把特征维 `d_out` 均分成 `num_heads` 份，每头独立做缩放点积注意力再拼接，最后过 `out_proj`。本讲所有实现都假设你已经懂 `view` / `transpose` 怎么切头。
- **PyTorch 训练范式**（见 u8-l1）：`nn.Module`、前向 `forward`、`.to(device)`、`model.eval()` 关闭 dropout、以及「前向 + 反向」两个阶段。

几个本讲会反复用到的术语，先做个通俗解释：

| 术语 | 通俗解释 |
| --- | --- |
| **朴素注意力（naive）** | 自己手写 `Q @ K^T`、`softmax`、`@ V` 三步，中间结果（注意力矩阵）完整存在显存里。 |
| **FlashAttention** | 一种**内存优化的注意力算法**：分块（tiling）计算、用「在线 softmax」避免把整张 N×N 注意力矩阵写回显存，省内存又省搬运时间。 |
| **SDPA** | PyTorch 内置的 `scaled_dot_product_attention` 函数，底层会**自动调用** FlashAttention 等高效 kernel。 |
| **Fused kernel（融合算子）** | 把多个小算子合并成一个大算子，减少中间结果落盘和 kernel 启动开销。 |
| **在线 softmax（online softmax）** | 分块扫描 K/V 时，边算边维护当前块的 max 与求和，最后再合并，避免一次性看到整行。 |

## 3. 本讲源码地图

本讲几乎只围绕**一个文件**展开，外加它的测试文件作为「正确性验证」的范本：

| 文件 | 作用 |
| --- | --- |
| [ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb) | 核心笔记本：并列给出 **9 种**因果多头注意力实现，并在 CPU 与 A100 GPU 上做了计时对比，是本讲的主体。 |
| [ch03/02_bonus_efficient-multihead-attention/README.md](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/README.md) | bonus 目录的说明，内嵌了三张基准对比图（仅前向、前向+反向、编译后）。 |
| [ch03/02_bonus_efficient-multihead-attention/tests/test_mha_implementations.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/tests/test_mha_implementations.py) | 用 `torch.allclose` 验证「einsum 版」与「第 3 章 Linear 版」输出一致的测试，是本讲正确性实践的出处。 |

> 提示：该笔记本第一段就把设备选成 `mps`/`cuda`/`cpu` 之一，并构造了固定的测试输入 `embeddings`（`batch_size=8, context_len=1024, embed_dim=768`）。后面所有实现都用这同一个输入对比，保证可比性。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先总览 9 种实现（4.1），再讲朴素实现的内存瓶颈与 FlashAttention 的解法（4.2，对应 *FlashAttention/SDPA 对比* 与 *内存权衡*），然后逐开关拆解 SDPA（4.3，对应 *scaled_dot_product_attention*），最后落地到正确性与性能评估（4.4）。

### 4.1 九种实现全景：功能相同、工程取舍不同

#### 4.1.1 概念说明

这 9 种实现**数学上完全等价**——都算的是因果多头缩放点积注意力，输出形状都是 `(batch, num_tokens, embed_dim)`。它们的差别只在「怎么把数学写成代码」：用几个权重矩阵、用 `Linear` 还是 `einsum`、是不是调 PyTorch 的融合算子。我们可以按「优化轴」把它们分三组：

- **A 组：手写派（朴素，N×N 矩阵全量物化）**
  - 实现 1 `Ch03_MHA_Wrapper`：第 3 章教学版的「逐头循环」写法，把多个单头 `CausalAttention` 放进 `nn.ModuleList` 串行跑（对应 u3-l3 里讲的 Variant A）。
  - 实现 2 `Ch03_MHA`：第 3 章最终版，用一次大投影 + `view/transpose` 切头，避免逐头循环（对应 u3-l3 的 Variant B）。
  - 实现 3 `MultiHeadAttentionCombinedQKV`：把 Q/K/V 三个 `Linear` 合并成一个 `3*d_out` 的大 `Linear`，一次投影出三者。
  - 实现 4 `MHAEinsum`：用 `torch.einsum`（爱因斯坦求和）写线性变换和点积，不依赖 `nn.Linear`。
- **B 组：调融合算子派（不再手写 softmax）**
  - 实现 5 `MHAPyTorchScaledDotProduct`：用 SDPA 且 `is_causal=True`，**自动走 FlashAttention**。
  - 实现 6 `MHAPyTorchSDPAWithoutFlash`：用 SDPA 但传显式 `attn_mask`，**关闭** FlashAttention 快速路径。
  - 实现 7 `MHAPyTorchClass`：直接用 `torch.nn.MultiheadAttention`，默认 `need_weights=True`。
  - 实现 8：同上但 `need_weights=False`，内部改走 SDPA。
  - 实现 9 `MHAPyTorchFlexAttention`：用 PyTorch 2.5+ 的 FlexAttention（仅在 CUDA 上可用）。

为什么要在乎「怎么写」？因为**同一个公式，朴素写法会把一张 N×N 的注意力矩阵完整存进显存**，序列一长就成了瓶颈；而 B 组的融合算子从根上回避了这张大矩阵。

#### 4.1.2 核心流程

九种实现共用同一条数据流骨架，区别只在中间「注意力核心」那一步：

```text
输入 x: (b, n, d)
   │
   ├── A 组：W_q/W_k/W_v（或合并的 qkv）投影出 Q,K,V: (b, h, n, hd)
   │
   ├── 【注意力核心，分歧点】
   │     • A 组：attn = softmax( (Q@K^T)/√hd  被 mask 置 -inf ) @ V   ← 物化整张 N×N
   │     • B 组：context = SDPA(Q, K, V, is_causal=True/False)         ← 不物化 N×N
   │
   └── 拼回头 + out_proj → 输出: (b, n, d)
```

实现 3/5/6/9 还共用一个漂亮的「单次大投影 + 切三份」技巧（下一节源码精读会展开）。

#### 4.1.3 源码精读

笔记本一开头就把设备和输入定死，保证所有实现同台竞技：

[mha-implementations.ipynb:L70](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L70) —— 设备自动选择：优先 Apple 的 `mps`，其次 NVIDIA `cuda`，最后 `cpu`；这是全书通用的设备约定。

A 组代表——第 3 章最终版 `Ch03_MHA`，用 `view/transpose` 无拷贝切头，然后**手动三步**算注意力：

[mha-implementations.ipynb:L235-L330](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L235-L330) —— `Ch03_MHA` 完整类：投影 → `view` 把 `(b,n,d_out)` 展成 `(b,n,h,hd)` → `transpose(1,2)` 把头提到前面 → `queries @ keys.transpose(2,3)` 算分数 → `masked_fill_` 套因果掩码 → `softmax(../√hd)` → `@ values` → 转回去 `.contiguous().view` 拼头 → `out_proj`。注意 `attn_scores`/`attn_weights` 这两个 `(b,h,n,n)` 张量都会被**完整物化**到显存，这正是朴素实现的内存代价。

对比逐头循环的「笨办法」`Ch03_MHA_Wrapper`：

[mha-implementations.ipynb:L173-L186](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L173-L186) —— `Ch03_MHA_Wrapper`：用 `nn.ModuleList` 装若干单头 `CausalAttention`，`forward` 里 `[head(x) for head in self.heads]` 串行跑 12 次，再 `torch.cat`。功能等价但 Python 循环开销大、无法融合，是基准里最慢的实现之一（这正是 u3-l3 选择 Variant B 的原因）。

「合并 QKV」的写法，是后面 B 组也反复用的技巧：

[mha-implementations.ipynb:L369-L390](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L369-L390) —— `MultiHeadAttentionCombinedQKV` 用单个 `self.qkv = nn.Linear(d_in, 3*d_out)` 一次算出 Q/K/V，再 `view(b,n,3,h,hd)` → `permute(2,0,3,1,4)` 重排成 `(3,b,h,n,hd)`，最后 `qkv.unbind(0)` 拆成三个 `(b,h,n,hd)`。一次大 GEMM 比三次小 GEMM 在 GPU 上更高效。

#### 4.1.4 代码实践

**实践目标**：确认九种实现输出形状一致，建立「它们算的是同一件事」的直觉。

**操作步骤**（以下为示例代码，在已按 README 装好 PyTorch 的环境中运行）：

```python
# 示例代码：通跑 9 种实现，检查输出形状
import torch
b, n, d, h = 8, 1024, 768, 12
x = torch.randn((b, n, d))

# 假设已从笔记本复制 9 个类：Ch03_MHA_Wrapper, Ch03_MHA, ... MHAPyTorchFlexAttention
impls = {
    "wrapper":      Ch03_MHA_Wrapper(d, d//h, n, 0.0, h),
    "ch03":         Ch03_MHA(d, d, n, 0.0, h),
    "combined_qkv": MultiHeadAttentionCombinedQKV(d, d, h, n, 0.0),
    "einsum":       MHAEinsum(d, d, n, 0.0, h),
    "sdpa_flash":   MHAPyTorchScaledDotProduct(d, d, h, n, 0.0),
    "sdpa_noflash": MHAPyTorchSDPAWithoutFlash(d, d, h, n, 0.0),
    "mha_default":  MHAPyTorchClass(d, d, h, n, 0.0),
    "mha_noweights":MHAPyTorchClass(d, d, h, n, 0.0, need_weights=False),
    # flex 需要 CUDA + PyTorch>=2.5，按条件加入
}
for name, m in impls.items():
    out = m(x)
    print(f"{name:14s} -> {tuple(out.shape)}")
```

**需要观察的现象**：每一行都打印 `(8, 1024, 768)`，即 `batch × num_tokens × embed_dim`。

**预期结果**：所有实现的输出**形状**完全相同；但**数值**不同（因为各自权重是独立随机初始化的，这一点 4.4 会专门用「复制权重 + allclose」来处理）。FlexAttention 若不在 CUDA 上会跳过或报错，属正常。

#### 4.1.5 小练习与答案

**练习 1**：`Ch03_MHA_Wrapper` 和 `Ch03_MHA` 算的是同一个东西，为什么前者更慢？
**答案**：前者用 Python 的 `for head in self.heads` 串行跑 12 次，每次都触发一次单头的前向与 kernel 启动；后者用张量 `view/transpose` 一次性把所有头并到 batch 维，让底层一次算完，省掉了循环与多次 kernel 启动开销。

**练习 2**：`MultiHeadAttentionCombinedQKV` 用 `qkv.unbind(0)` 拆三份，等价于分别写三个 `Linear` 吗？
**答案**：数学上等价（都是对 `d_in→d_out` 的三个投影），但工程上不同：合并版只做一次 `(d_in, 3·d_out)` 的大矩阵乘法，GPU 上大 GEMM 的吞吐通常高于多次小 GEMM；权重数量也完全一样。

---

### 4.2 朴素注意力的内存瓶颈与 FlashAttention 思想

#### 4.2.1 概念说明

这是本讲最核心的一节，对应「**FlashAttention/SDPA 对比**」与「**内存权衡**」两个最小模块。

朴素注意力的麻烦出在中间那张 **N×N 注意力矩阵**上。回顾公式：

\[
\text{Attn}(Q,K,V)=\operatorname{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}\right)V
\]

其中 \(QK^{\top}\) 是一个 \(N\times N\) 的矩阵（N 是序列长度），`softmax` 又会物化出同样大小的 `attn_weights`。于是朴素实现的**峰值显存**里至少躺着这两张 \(N\times N\) 大矩阵。这跟模型参数量无关，纯粹是「序列长度的平方」在烧显存——这就是所谓的 **O(N²) 内存**。

举个本笔记本的真实数字：`batch=8, heads=12, seq=1024, float32`，一张注意力矩阵占

\[
\text{内存}_{\text{一张 attn}} = 8 \times 12 \times 1024 \times 1024 \times 4\ \text{字节} \approx 384\ \text{MiB}
\]

而朴素路径前向就要物化「分数 + 权重」两张，反向还得为它们留梯度，轻松破 1 GiB——还只是**一层**、**1024 长度**。一旦上下文拉到几万 token（如长文档），显存就会爆。

**FlashAttention** 的洞察是：你其实**不需要**把整张 \(N\times N\) 矩阵完整存在显存里。它把 Q/K/V 切成小块（tiling），在片上高速 SRAM 里分块算注意力，用**在线 softmax** 边扫描边归一化，从而：

- **峰值显存**从 O(N²) 降到接近 O(N)（只存分块所需的少量中间量）；
- **速度**也变快——因为 GPU 的 HBM（大显存）与 SRAM（片上）之间搬运这张大矩阵本身就耗时，不物化它就少了一大笔搬运费。

PyTorch 的 SDPA 在底层会**自动选择** FlashAttention（或 memory-efficient attention）等高效 kernel——你只要换一行调用，就白拿这些收益。

#### 4.2.2 核心流程

朴素注意力（A 组）的显存流：

```text
Q, K, V: (b,h,n,hd)
   │  Q @ K^T        ← 物化 attn_scores: (b,h,n,n)   ← O(N²) 显存点 ①
   │  masked_fill(-inf)
   │  softmax        ← 物化 attn_weights: (b,h,n,n)   ← O(N²) 显存点 ②
   │  @ V
   └─ context: (b,h,n,hd)
```

FlashAttention（SDPA 底层）的显存流：

```text
Q, K, V: (b,h,n,hd)
   │  分块（tile）载入 SRAM
   │  块内 Q@K^T + 在线 softmax（维护 running max / running sum）
   │  块内累加 @ V
   └─ context: (b,h,n,hd)        ← 从不物化整张 (b,h,n,n)
```

> 注意：FlashAttention **没有改变数学结果**（在 float 数值精度内），它改变的是**计算顺序与显存足迹**。所以 4.4 可以用 `torch.allclose` 把它和朴素版对齐。

#### 4.2.3 源码精读

B 组的 SDPA 实现里，朴素那段 `Q@K^T → softmax → @V` 被折叠成**一次函数调用**，注意力矩阵根本不在 Python 层露面：

[mha-implementations.ipynb:L624-L625](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L624-L625) —— `MHAPyTorchScaledDotProduct` 的核心：`nn.functional.scaled_dot_product_attention(queries, keys, values, attn_mask=None, dropout_p=use_dropout, is_causal=True)`。这一行内部自动启用 FlashAttention；`is_causal=True` 告诉它「我要因果掩码」，由融合 kernel 直接处理，无需你传任何 `mask` 张量，也就不会物化 `attn_scores`/`attn_weights`。

对照实现 6——刻意传显式 `attn_mask` 来**关掉** FlashAttention 快速路径：

[mha-implementations.ipynb:L733-L734](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L733-L734) —— `MHAPyTorchSDPAWithoutFlash` 用 `attn_mask=attn_mask, ..., is_causal=False`。笔记本明确注释「disable FlashAttention by passing an explicit causal mask」。把实现 5 和 6 放在一起，正好说明：**同样的 SDPA，传不传 `attn_mask` 决定走不走融合 kernel**——这是后面性能差距的根因。

#### 4.2.4 代码实践

**实践目标**：亲手感受「O(N²) 内存」长什么样，并对比朴素与 SDPA 的峰值显存。

**操作步骤**（示例代码，**需要 CUDA GPU** 才能量到真实显存）：

```python
# 示例代码：对比朴素 Ch03_MHA 与 SDPA 的峰值显存（仅 GPU 可测）
import torch
from ch03_module import Ch03_MHA, MHAPyTorchScaledDotProduct  # 假设已导入

for N in [512, 1024, 2048, 4096]:
    x = torch.randn(8, N, 768, device="cuda")
    naive = Ch03_MHA(768, 768, N, 0.0, 12).cuda().eval()
    sdpa  = MHAPyTorchScaledDotProduct(768, 768, 12, N, 0.0).cuda().eval()

    for name, m in [("naive", naive), ("sdpa", sdpa)]:
        torch.cuda.reset_peak_memory_stats()
        with torch.no_grad():
            m(x)
        peak = torch.cuda.max_memory_allocated() / 1024**2  # MiB
        print(f"N={N:5d}  {name:6s}  peak={peak:8.1f} MiB")
```

**需要观察的现象**：随着 N 翻倍，`naive` 的峰值显存大约**按 4 倍**（即 \(N^2\)）增长；`sdpa` 的增长平缓得多。

**预期结果**：朴素版峰值显存曲线是二次的，SDPA 接近线性。**待本地验证**：在无 GPU 环境下无法用 `torch.cuda.max_memory_allocated` 测显存，可退而用 CPU 上的 `tracemalloc` 测 Python 堆（但精度和代表性都差很多），或直接参照下一节 4.4 的**计时**对比。

#### 4.2.5 小练习与答案

**练习 1**：为什么说「FlashAttention 不改变数学，只改变计算顺序」？
**答案**：它算的仍是 \(\operatorname{softmax}(QK^\top/\sqrt{d_k})V\)，只是把 Q/K/V 分块、用在线 softmax 分批归一化再合并。分块求和与整体求和在数学上等价，浮点上仅在舍入误差级别有差异，故可用 `torch.allclose` 对齐。

**练习 2**：把上下文长度从 1024 翻到 4096，朴素注意力的「分数矩阵」显存涨到原来的几倍？
**答案**：\(4^2=16\) 倍。因为分数矩阵大小是 \(N\times N\)，N 翻 4 倍则元素数变 16 倍。这正是长序列下朴素实现很快爆显存的原因。

---

### 4.3 scaled_dot_product_attention：is_causal / attn_mask / need_weights 三把钥匙

#### 4.3.1 概念说明

`scaled_dot_product_attention`（SDPA）是 PyTorch 2.0 起官方推荐的注意力函数，对应「**scaled_dot_product_attention**」最小模块。它的签名关键参数有三个，正好对应本笔记本里 4 个实现的「开关组合」：

\[
\text{SDPA}(Q, K, V;\ \text{is\_causal},\ \text{attn\_mask})=\operatorname{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}+M\right)V
\]

其中 \(M\) 是掩码项（被屏蔽位置为 \(-\infty\)）。三把「钥匙」：

| 开关 | 取值 | 含义 | 笔记本里的实现 |
| --- | --- | --- | --- |
| `is_causal` | `True` | 让融合 kernel 自动生成下三角因果掩码，**走 FlashAttention**，最快也最省 | 实现 5 `MHAPyTorchScaledDotProduct` |
| `attn_mask` | 传显式张量 | 你自己提供掩码矩阵；这会**禁用** `is_causal=True` 的快速路径 | 实现 6 `MHAPyTorchSDPAWithoutFlash` |
| `need_weights` | `False`（`nn.MultiheadAttention` 的参数） | 不返回注意力权重矩阵，内部转而用 SDPA | 实现 8 `mha_pytorch_class_noweights` |

核心规律：**只要你还想要那张 N×N 的注意力权重矩阵（`need_weights=True` 或自己传 `attn_mask`），就得多花成本把它物化出来**；反之让 kernel 自己隐式处理掩码，就能拿满 FlashAttention 的收益。

还有第 9 个实现 `MHAPyTorchFlexAttention` 用的是 `flex_attention`——PyTorch 2.5+ 的新接口，能把「因果」这类约束写成纯 Python 函数 `causal(b,h,q_idx,kv_idx)= q_idx>=kv_idx`，再编译成 block mask。它灵活（能表达任意稀疏模式），但本笔记本的基准里它在编译/调度上反而较慢（见 4.4），适合**非标准注意力模式**而非纯因果场景。

#### 4.3.2 核心流程

实现 5（最推荐写法）的 SDPA 前向：

```text
x: (b, n, d)
  qkv = self.qkv(x)                    # (b, n, 3d)
  qkv = qkv.view(b, n, 3, h, hd)       # (b, n, 3, h, hd)
  qkv = qkv.permute(2,0,3,1,4)         # (3, b, h, n, hd)
  queries, keys, values = qkv          # 各 (b, h, n, hd)
  ctx = SDPA(q, k, v, attn_mask=None, is_causal=True)   # ← 一行融合算子
  ctx = ctx.transpose(1,2).reshape(b, n, d)  # 拼头
  return self.proj(ctx)
```

注意它**完全不构造 `attn_scores`、不调用 `softmax`、不持有 `mask` buffer**——这些都被藏进 SDPA 内部。

#### 4.3.3 源码精读

实现 5 的整体结构（没有 `mask` buffer、没有 `masked_fill`、没有显式 `softmax`）：

[mha-implementations.ipynb:L592-L630](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L592-L630) —— `MHAPyTorchScaledDotProduct`：`__init__` 只有 `qkv`、`proj`、`dropout`，**没有 `register_buffer("mask", ...)`**（对比实现 2/3/6/7 都有）。这是 SDPA `is_causal=True` 的标志——掩码交给 kernel 隐式生成。`dropout` 直接存成 float，运行时用 `self.training` 决定 `dropout_p`，因为 SDPA 的 dropout 是它内部参数而非一个 `nn.Dropout` 模块。

实现 8 用 `nn.MultiheadAttention` 时，靠 `need_weights=False` 一行就能从「慢的默认路径」切到「快的 SDPA 路径」：

[mha-implementations.ipynb:L934](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L934) —— `need_weights=False # NEW!`。笔记本引用的 PyTorch 文档原话：「Set `need_weights=False` to use the optimized `scaled_dot_product_attention` and achieve the best performance for MHA.」——同一个 `nn.MultiheadAttention` 类，改一个布尔值，性能差出一档（见 4.4 基准）。

FlexAttention 把因果写成函数，再编译成 `block_mask`：

[mha-implementations.ipynb:L1005-L1055](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L1005-L1055) —— `MHAPyTorchFlexAttention`：定义 `def causal(b,h,q_idx,kv_idx): return q_idx >= kv_idx`，用 `create_block_mask(causal, ...)` 把它编译成稀疏块掩码，再喂给 `flex_attention(...)`。注意它受 `torch.cuda.is_available()` 与 PyTorch≥2.5 限制，且笔记本注明「FlexAttention caveat: It currently doesn't support dropout」。

#### 4.3.4 代码实践

**实践目标**：亲手验证「三把钥匙」带来的差异——同样的 SDPA，开关不同速度不同、但结果一致。

**操作步骤**（示例代码）：

```python
# 示例代码：is_causal=True vs attn_mask=False 两种 SDPA 调用
import torch, torch.nn as nn, math
torch.manual_seed(0)
b, h, n, hd = 2, 12, 64, 64
q = torch.randn(b, h, n, hd); k = torch.randn(b, h, n, hd); v = torch.randn(b, h, n, hd)

# 方式 A：is_causal=True（走 FlashAttention）
out_a = nn.functional.scaled_dot_product_attention(q, k, v, is_causal=True)

# 方式 B：传显式因果 attn_mask（不走 FlashAttention）
mask = torch.triu(torch.ones(n, n, dtype=torch.bool), diagonal=1)
out_b = nn.functional.scaled_dot_product_attention(q, k, v, attn_mask=mask, is_causal=False)

print("max abs diff:", (out_a - out_b).abs().max().item())
print("allclose:", torch.allclose(out_a, out_b, atol=1e-6))
```

**需要观察的现象**：两种方式的**输出几乎逐位相等**（浮点误差级别），证明它们算的是同一个因果注意力。

**预期结果**：`max abs diff` 是 1e-6 量级，`allclose` 为 `True`。这正说明 `is_causal=True` 和「自己传因果 `attn_mask`」**数学等价**，差别只在性能（前者走融合 kernel）。FlexAttention 因仅在 CUDA + PyTorch≥2.5 可用，本步骤未含它，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：实现 5 的 `__init__` 里为什么没有 `register_buffer("mask", ...)`？
**答案**：因为它用 `is_causal=True`，因果掩码由 SDPA 的融合 kernel 在内部隐式生成，不需要把掩码张量作为模型状态存下来；这也省掉了「掩码随 `.to(device)` 迁移」的麻烦（呼应 u3-l2 讲过的 `register_buffer` 用途）。

**练习 2**：什么场景下你会放弃 `is_causal=True`、改用 `attn_mask` 或 FlexAttention？
**答案**：当掩码不是标准下三角因果时，例如滑动窗口（SWA，见 u9-l3）、文档间分隔（packing）、或任意稀疏注意力模式——这些无法用单一 `is_causal` 表达，必须显式描述掩码，此时用 `attn_mask` 或更灵活的 FlexAttention。

---

### 4.4 正确性验证与性能基准

#### 4.4.1 概念说明

「谁更快」不能靠拍脑袋，要靠**两件事**：一是证明它们**算的是同一个东西**（正确性），二是**公平地量**耗时与显存（性能）。这对应「**内存与速度权衡**」的落地评估。

**正确性**：不同实现参数结构不同（有的三个 `Linear`、有的一个合并 `qkv`、有的是裸 `Parameter`），不能直接比输出。办法是**人为把权重复制成一致**，再跑同一输入，用 `torch.allclose(a, b, atol=...)` 判定——容忍浮点舍入误差。仓库的测试文件正是这么做的：把 `Ch03_MHA` 的 `W_query/W_key/W_value/out_proj` 拷给 `MHAEinsum`，证明「einsum 写法 ≡ Linear 写法」。

**性能**：在 GPU 上计时有个大坑——**CUDA 是异步的**，用 Python 的 `time.time()` 测不准。笔记本用了 `torch.cuda.Event` 的 record/synchronize 来精确计时；此外还做了 **warmup**（先空跑几轮预热 kernel 缓存/JIT）、对比「仅前向 / 前向+反向 / 编译后」三档，避免把编译开销算进结果。

#### 4.4.2 核心流程

正确的基准测试流程（GPU）：

```text
1. warmup：空跑 5 次，让 kernel 选定、缓存热起来
2. torch.cuda.synchronize()      ← 等 GPU 真正算完
3. 重复 N 次：
     start.record(); fn(x); end.record()
     torch.cuda.synchronize()    ← 每次都同步
     记录 start.elapsed_time(end)
4. 对 N 次取 mean/std
```

正确性验证流程：

```text
1. 以实现 X 为基准，构造与之参数结构兼容的实现 Y
2. 用 torch.no_grad() 把 X 的权重逐个 .copy_() 到 Y
3. 同输入前向，得 out_x / out_y
4. assert torch.allclose(out_x, out_y, atol=1e-5)
```

#### 4.4.3 源码精读

笔记本的 GPU 计时器（含 warmup 与 synchronize）：

[mha-implementations.ipynb:L1796-L1816](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L1796-L1816) —— `time_pytorch_function`：先用 5 次 warmup + `torch.cuda.synchronize()`，再用 `start.record()`/`end.record()` 配合 `synchronize()` 测 N 次，返回 `(mean, std)`。这正是规避「CUDA 异步导致测不准」的标准写法。

前向+反向的计时包装（注意它会清梯度、对 `output.sum()` 反向）：

[mha-implementations.ipynb:L1872-L1900](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L1872-L1900) —— `forward_backward` 与 `time_pytorch_function_forward_backward`：先 `embeddings.grad.zero_()`，再 `output=func(embeddings); loss=output.sum(); loss.backward()`，让反向也参与计时。这一档比「仅前向」更能反映真实训练成本。

编译后再测（`torch.compile`）：

[mha-implementations.ipynb:L1962](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/mha-implementations.ipynb#L1962) —— `fn = torch.compile(fn)`：把每个实现编译后再跑前向+反向。`torch.compile` 能进一步融合算子、折叠 kernel，是「榨干性能」的最后一招。

正确性范本——仓库测试如何证明 einsum 版 ≡ 第 3 章版：

[test_mha_implementations.py:L34-L63](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch03/02_bonus_efficient-multihead-attention/tests/test_mha_implementations.py#L34-L63) —— `test_mha_einsum_matches_ch03`：用同一 `seed` 构造 `Ch03_MHA` 与 `MHAEinsum`，经 `copy_weights`（`to_mha.W_query.copy_(from_mha.W_query.weight.T)` 等，注意 `.weight.T` 转置——`nn.Linear` 权重是 `(out,in)`，einsum 版 `W_query` 是 `(in,out)`）把权重对齐，再断言 `torch.allclose(out_linear, out_einsum, atol=1e-5)`。它还参数化了三种 `d_in/d_out` 组合（相等、不等、`d_in>d_out`）覆盖边界。

#### 4.4.4 代码实践

**实践目标**：复刻仓库测试的做法，验证 einsum 版与第 3 章版输出一致；再用 `timeit` 量出本地耗时对比表。

**操作步骤**（示例代码）：

```python
# 示例代码：正确性 + 计时
import torch, timeit
torch.manual_seed(123)
b, n, d, h = 2, 4, 768, 12
x = torch.randn(b, n, d)

m_linear = Ch03_MHA(d, d, n, 0.0, h, qkv_bias=False).eval()
m_einsum = MHAEinsum(d, d, n, 0.0, h, qkv_bias=False).eval()

# ① 复制权重（参考 tests/test_mha_implementations.py 的 copy_weights）
with torch.no_grad():
    m_einsum.W_query.copy_(m_linear.W_query.weight.T)
    m_einsum.W_key.copy_(m_linear.W_key.weight.T)
    m_einsum.W_value.copy_(m_linear.W_value.weight.T)
    m_einsum.out_proj.weight.copy_(m_linear.out_proj.weight)
    m_einsum.out_proj.bias.copy_(m_linear.out_proj.bias)

# ② 正确性：应逐位相等
out_l, out_e = m_linear(x), m_einsum(x)
print("allclose:", torch.allclose(out_l, out_e, atol=1e-5))

# ③ 计时（CPU 上可用 timeit；GPU 上请改用 torch.cuda.Event，见 4.4.3）
t1 = timeit.timeit(lambda: m_linear(x), number=20) / 20
t2 = timeit.timeit(lambda: m_einsum(x), number=20) / 20
print(f"linear={t1*1e3:.2f} ms  einsum={t2*1e3:.2f} ms")
```

**需要观察的现象**：`allclose` 为 `True`（证明两实现数学等价）；两实现的耗时在同一量级（都是朴素派，未走融合算子）。

**预期结果**：`allclose=True`。绝对耗时随机器而异——笔记本在 **M3 Mac CPU（PyTorch 2.4）** 上的 `%timeit` 实测记录（见笔记本输出，**数值随机器变化，仅供量级参考**）：

| 实现 | CPU 耗时 | A100 GPU 耗时 |
| --- | --- | --- |
| 1) wrapper（逐头循环） | 179 ms | 4.68 ms |
| 2) Ch03 MHA（张量切头） | 166 ms | 3.08 ms |
| 3) combined QKV | 190 ms | 3.81 ms |
| 4) einsum | 196 ms | 4.11 ms |
| **5) SDPA + is_causal=True（FlashAttention）** | **110 ms** | **1.1 ms** |
| 6) SDPA + 显式 attn_mask（无 Flash） | 99.5 ms | 1.8 ms |
| 7) nn.MHA 默认 | 198 ms | 3.04 ms |
| 8) nn.MHA `need_weights=False` | 168 ms | 2.13 ms |
| 9) FlexAttention（仅 CUDA） | — | 13.9 ms |

读表要点（也是本讲最重要的结论之一）：

- **A100 上**，实现 5（SDPA + FlashAttention）≈ 1.1 ms，是最快的朴素版（实现 2，3.08 ms）的近 **3 倍**快；比逐头循环（实现 1）快约 4 倍。融合算子在 GPU 上收益巨大。
- **CPU 上**差异变小，甚至实现 6（无 Flash）反超实现 5——因为 FlashAttention 的优势主要来自 GPU 的 HBM↔SRAM 带宽优化，CPU 上没有这套硬件红利。
- **`need_weights` 的威力**：实现 7→8 仅改一个布尔值，GPU 上 3.04 ms → 2.13 ms。
- **FlexAttention 反而慢**（13.9 ms）：它在编译 block mask 上有开销，纯因果这种简单模式「杀鸡用牛刀」，更适合复杂稀疏模式。

> **待本地验证**：上表是笔记本在特定硬件/版本下的实测记录，你本地的绝对数字会不同（尤其 CPU 与 GPU 的排名可能颠倒），但「GPU 上 SDPA + FlashAttention 最快、朴素逐头循环最慢」的**相对结论**通常稳定。

#### 4.4.5 小练习与答案

**练习 1**：为什么 GPU 计时必须用 `torch.cuda.Event` + `synchronize`，而不能用 `time.time()`？
**答案**：CUDA kernel 是**异步**下发的——Python 的调用在 kernel 真正执行完前就返回了。用 `time.time()` 会测到「下发耗时」而非「计算耗时」，结果严重偏小且不稳。`Event.record()` 配合 `synchronize()` 能精确卡到 kernel 真正完成的时刻。

**练习 2**：测试里 `to_mha.W_query.copy_(from_mha.W_query.weight.T)` 为什么要转置（`.T`）？
**答案**：`nn.Linear` 内部权重形状是 `(out_features, in_features)`，而 `MHAEinsum` 里 `W_query` 是用 `einsum("bnd,do->bno", x, W)` 直接参与计算，形状是 `(in, out)`，两者互为转置。对齐权重时必须 `.T`，否则形状不匹配或语义错位。

---

## 5. 综合实践

**任务**：写一个「注意力实现对比器」，把本讲三件事（形状校验、正确性对齐、性能基准）串起来，最终给出一张「该选哪种实现」的决策建议。

**建议步骤**：

1. 从笔记本把 9 个类导入（或直接在笔记本里追加一个 cell），用统一输入 `x = torch.randn(8, 1024, 768)` 跑一遍，打印每个输出的 `shape`，确认全部为 `(8, 1024, 768)`。
2. 仿照 `test_mha_einsum_matches_ch03`，选 `Ch03_MHA` 作为「基准真值」，分别与 `MHAEinsum`、`MultiHeadAttentionCombinedQKV` 做权重对齐 + `torch.allclose`（注意合并 QKV 版的 `self.qkv` 要拆成三份再 `.T` 复制）。记录哪些能对上、哪些因为结构差异难以直接对齐。
3. 用笔记本的 `time_pytorch_function`（GPU）或 `timeit`（CPU）量出耗时均值；若有 GPU，再用 `torch.cuda.max_memory_allocated` 量出峰值显存，整理成一张三列表：`实现 | 耗时(ms) | 峰值显存(MiB)`。
4. 把 SDPA `is_causal=True` 与 `is_causal=False, attn_mask=...` 的耗时差换算成「FlashAttention 在你机器上的加速比」，写一句结论。
5. 给出选型建议（示例口径）：
   - 标准因果注意力、要最快 → **实现 5（SDPA + `is_causal=True`）**；
   - 复用现成 `nn.MultiheadAttention` → 记得 `need_weights=False`（实现 8）；
   - 非标准稀疏掩码 → FlexAttention 或显式 `attn_mask`；
   - 教学/调试要看注意力权重 → 才退回朴素实现 2。

**验收标准**：能说出「同一数学公式，换 SDPA + FlashAttention 在 GPU 上快了约几倍、显存从 O(N²) 降到接近 O(N)」，并能用 `torch.allclose` 证明它们数值一致。

## 6. 本讲小结

- 本笔记本把**同一个因果多头注意力**写了 **9 种**实现，它们数学等价、输出形状同为 `(b, n, d)`，差别只在「怎么写」带来的速度与内存差异。
- 朴素实现的内存瓶颈是那张 **N×N 注意力矩阵**，峰值显存是 **O(N²)**；序列一长就爆显存，与参数量无关。
- **FlashAttention** 用分块 + 在线 softmax，让显存降到接近 **O(N)** 且更快；它**不改变数学**，故可用 `torch.allclose` 与朴素版对齐。
- `scaled_dot_product_attention` 是 PyTorch 官方推荐入口：`is_causal=True` 走 FlashAttention（最快最省）；传显式 `attn_mask` 则关闭快速路径；`nn.MultiheadAttention` 设 `need_weights=False` 同样切到 SDPA。
- 基准要公平：GPU 计时必须用 `torch.cuda.Event` + `synchronize` 并做 warmup；A100 上 SDPA+Flash 比朴素实现快约 2–4 倍，CPU 上差距缩小；FlexAttention 在纯因果场景反而偏慢，强项是非标准稀疏模式。
- 选型口诀：**生产用 SDPA + `is_causal=True`，教学/调试要看权重才用朴素版**。

## 7. 下一步学习建议

- **纵向深入 KV 内存**：本讲解决的是「注意力计算」本身的 O(N²) 内存；而推理时的 **KV cache** 会随序列线性吃显存，那是另一类问题，请接着学 **u9-l1（KV Cache）** 和 **u9-l3（GQA / MLA / SWA）**——GQA/MLA 正是为了削减 KV 缓存、SWA 则是 SDPA 之外另一种降低注意力量级的掩码策略。
- **横向迁移到真实模型**：本讲的 SDPA 写法会原样出现在 u10 的现代架构里。建议阅读 [ch05/07_gpt_to_llama/previous_chapters.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/07_gpt_to_llama/previous_chapters.py)，看 Llama 的注意力如何用 RoPE + SDPA 组合。
- **扩展练习**：把本讲的 `time_pytorch_function` 套到你 u4-l3 的 `GPTModel` 上，对比「整模型用 Ch03_MHA」与「整模型用 SDPA 版」在单步训练上的耗时差异，体会融合算子在堆叠多层后的累积收益（**待本地 GPU 验证**）。
