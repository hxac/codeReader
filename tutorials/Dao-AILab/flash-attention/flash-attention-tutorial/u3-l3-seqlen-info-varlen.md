# SeqlenInfo：变长序列与偏移

## 1. 本讲目标

本讲紧接 [u3-l2 BlockInfo](u3-l2-block-info.md)。在上一讲里，`BlockInfo.get_n_block_min_max` 依赖两个关键数值 `seqlen_q` 与 `seqlen_k` 来裁剪每个 Q tile 需要遍历的 K/V 块范围。那么这两个数值从哪里来？如果一个 batch 里的序列长度各不相同，kernel 又是怎么知道「现在正在处理第几条序列、它从打包张量的哪里开始」？

学完本讲，你应当能够：

1. 说清 **varlen（variable-length，变长）注意力** 为什么要把不等长序列紧凑打包成一条 1D 张量，并理解 `cu_seqlens`（cumulative seqlens，累积序列长度数组）的打包与解包方式。
2. 区分 `offset`（精确起点偏移）与 `offset_padded`（tile 对齐偏移），理解二者为何并存、各自用在哪里。
3. 读懂 `SeqlenInfoQK` 这个数据类如何把「Q 与 K 的长度、起点、块数」等信息在每个 tile 开头**一次性**从显存读入寄存器，避免主循环里反复读显存。

本讲只聚焦 `seqlen_info.py` 的设计与 `interface.py` 中 varlen 入口对 `cu_seqlens` 的处理，不进入 kernel 主循环内部。

## 2. 前置知识

### 2.1 为什么需要变长注意力

Transformer 训练时经常碰到一个 batch 里各样本长度不一的情况。最朴素的做法是把所有序列 **pad（补零）** 到 batch 内最长的那条，凑成 `(batch, max_seqlen, num_heads, head_dim)` 的整齐张量。但注意力是 \(O(N^2)\) 的算子：补零补出来的位置仍然会参与 `QK^T` 与 softmax 计算，白白浪费算力与显存。

varlen 的思路是：**干脆把补零去掉，把所有序列首尾相接拼成一条长 1D 张量**，再用一个很小的「边界数组」`cu_seqlens` 标出每条序列的起止位置。这样每条序列只对自己的 token 做注意力，没有任何浪费。

### 2.2 cu_seqlens 是前缀和

设 batch 里有 \(B\) 条序列，第 \(b\) 条长度为 \(\ell_b\)。定义累积长度：

\[
\text{cu\_seqlens}[b] \;=\; \sum_{i=0}^{b-1} \ell_i, \qquad b = 0, 1, \dots, B
\]

其中 \(\text{cu\_seqlens}[0] = 0\)，\(\text{cu\_seqlens}[B] = \sum_i \ell_i\) 就是所有序列拼起来的**总长度** `total_q`。于是第 \(b\) 条序列在打包张量里占据半开区间：

\[
[\,\text{cu\_seqlens}[b],\; \text{cu\_seqlens}[b+1]\,), \qquad
\ell_b = \text{cu\_seqlens}[b+1] - \text{cu\_seqlens}[b]
\]

例如长度分别为 \(128, 256, 64\) 的三条序列，`cu_seqlens = [0, 128, 384, 448]`，总长度 `448`。

### 2.3 与上一讲的衔接

[BlockInfo](u3-l2-block-info.md) 的 `get_n_block_min_max` 形参第一个就是 `seqlen_info: SeqlenInfoQK`，里面真正被用到的字段是 `seqlen_q`、`seqlen_k`（推导因果/滑窗对角线 `n_idx = m_idx + seqlen_k - seqlen_q`）。本讲就解释 `SeqlenInfoQK` 是如何被构造出来、又是如何把长度与偏移信息高效送达 `BlockInfo` 的。

> 关键术语速查：varlen（变长）、cu_seqlens（累积长度前缀和）、offset（序列起点偏移）、offset_padded（tile 对齐偏移）、`assume(..., divby=...)`（向编译器声明对齐不变量）、tile（分块）、Constexpr（编译期常量）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [flash_attn/cute/seqlen_info.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py) | 本讲主角。定义 `SeqlenInfo`、`SeqlenInfoQK`、`SeqlenInfoQKNewK`，把所有与序列长度相关的信息在每个 tile 开头一次性算好。 |
| [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py) | 公共入口 `flash_attn_varlen_func` 与内部 `_flash_attn_fwd`，负责校验 `cu_seqlens` 形状、按 varlen 与否推断输出与 LSE 的形状。 |
| [flash_attn/cute/flash_fwd.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py) | 前向 kernel。在主循环开始前调用 `SeqlenInfoQK.create(...)` 构造长度信息，再用它对 Q/K/V 做 `domain_offset` 偏移。 |
| [flash_attn/cute/block_info.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py) | 消费者。`BlockInfo.get_n_block_min_max` 接收 `SeqlenInfoQK`，读其 `seqlen_q/seqlen_k` 推导 n-block 范围。 |

## 4. 核心概念与源码讲解

### 4.1 varlen 与 cu_seqlens：把不等长序列紧凑打包

#### 4.1.1 概念说明

如果坚持用定长布局 `(batch, max_seqlen, num_heads, head_dim)` 处理变长输入，补零不仅浪费，还会让注意力把零向量当成真实 token 去算分数（虽然可以通过掩码挡掉，但算力仍被消耗）。varlen 的解决方案是**把 batch 维度折叠进 seqlen 维**：

- 定长布局：`(batch, seqlen, num_heads, head_dim)`
- varlen 布局：`(total, num_heads, head_dim)`，其中 `total = sum(各序列长度)`

折叠后「第几条序列」不再由张量的某一维直接索引，而是改由 `cu_seqlens` 这个长度为 `batch+1` 的小数组来定位。FA4 里 `cu_seqlens` 是 `int32`、连续、形状恒为 `(batch_size + 1,)`，由 [interface.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py) 严格校验。

注意 Q 和 K 各有一个 `cu_seqlens`（`cu_seqlens_q`、`cu_seqlens_k`），因为 Q 与 K 的长度可以不同（典型如交叉注意力或解码场景）。

#### 4.1.2 核心流程

把变长 batch 喂给 FA4 的流程：

1. 用户把 \(B\) 条不等长序列首尾拼接成 1D 张量 `q/k/v`，并准备前缀和数组 `cu_seqlens_q`、`cu_seqlens_k`。
2. 调用 `flash_attn_varlen_func(q, k, v, cu_seqlens_q, cu_seqlens_k, ...)`。
3. `_flash_attn_fwd` 从 `cu_seqlens_q.shape[0] - 1` 推出 `batch_size`，校验形状与 dtype。
4. kernel 网格为「每条序列分配若干 tile」，每个 tile 拿到自己的 `batch_idx` 后，用 `cu_seqlens[batch_idx]` 算出该序列在打包张量里的起点偏移，再做注意力。
5. 输出 `out` 与定长情形同形（`(total_q, num_heads, head_dim_v)`），LSE 则因 varlen 改为 `(num_heads, total_q)`。

#### 4.1.3 源码精读

**入口如何从 `cu_seqlens` 推出 batch_size 与总长度。** 这是 varlen 与定长分流的第一个分叉点：

[flash_attn/cute/interface.py:357-363](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L357-L363) —— 当 `cu_seqlens_q` 非空时，`batch_size` 来自 `cu_seqlens_q.shape[0] - 1`，`seqlen_q` 置为 `None`（不再有统一长度），`total_q` 取自 Q 张量第 0 维。

紧接着是形状硬约束：

[flash_attn/cute/interface.py:391-394](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L391-L394) —— 要求 `cu_seqlens_q.shape == (batch_size + 1,)`。这正对应前缀和定义里数组长度为 \(B+1\)。

dtype 与连续性约束（`cu_seqlens` 必须是 int32、连续）在 [interface.py:415-421](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L415-L421) 统一校验。

输出 `out` 与 `lse` 的形状会因 varlen 改变，体现在同一函数里：

[flash_attn/cute/interface.py:469-475](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L469-L475) —— `q_batch_seqlen_shape` 在 varlen 下变成 `(total_q,)`；LSE 形状由定长的 `(batch, num_head, seqlen_q)` 变成 varlen 的 `(num_head, total_q)`。**注意 LSE 的维度顺序也变了**（num_head 提到最前），这对后续 split/合并逻辑很关键。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个 3 条不等长序列的 varlen 输入，看清 `cu_seqlens` 的结构。

**操作步骤**（纯 CPU 也能跑，仅用于理解打包方式，不调用 GPU kernel）：

```python
# 示例代码：演示 cu_seqlens 的构造与解包，CPU 即可运行
import torch

lens = [128, 256, 64]                      # 三条序列的长度
B = len(lens)
H, D = 8, 64

# 1) 构造前缀和：cu_seqlens[b] = sum(lens[:b])
cu_seqlens = torch.tensor(
    [0] + [sum(lens[: i + 1]) for i in range(B)],
    dtype=torch.int32,
)
print("cu_seqlens =", cu_seqlens.tolist())  # [0, 128, 384, 448]

total_q = int(cu_seqlens[-1])               # 448

# 2) 模拟打包张量（首尾相接，batch 维折叠进 seqlen 维）
q_packed = torch.randn(total_q, H, D)

# 3) 用 cu_seqlens 解包出第 b 条序列
def get_seq(b):
    sl, sr = int(cu_seqlens[b]), int(cu_seqlens[b + 1])
    return q_packed[sl:sr]                  # 形状 (lens[b], H, D)

for b in range(B):
    print(f"seq {b}: shape = {tuple(get_seq(b).shape)}")
```

**需要观察的现象**：`cu_seqlens` 第一个元素恒为 0，最后一个元素等于总长度；切片 `[cu_seqlens[b]:cu_seqlens[b+1]]` 恰好还原第 b 条序列。

**预期结果**：打印出 `[0, 128, 384, 448]`，以及三条序列的形状 `(128, 8, 64)`、`(256, 8, 64)`、`(64, 8, 64)`。

> 完整 GPU 对比脚本见本讲第 5 节综合实践。

#### 4.1.5 小练习与答案

**练习 1**：若 batch 内 4 条序列长度为 `[32, 64, 96, 128]`，写出 `cu_seqlens` 与 `total_q`。

**答**：`cu_seqlens = [0, 32, 96, 192, 320]`，`total_q = 320`。

**练习 2**：为什么 `cu_seqlens` 的长度是 `batch_size + 1` 而不是 `batch_size`？

**答**：因为要同时表达「第 b 条的起点」(`cu_seqlens[b]`) 和「最后一条的终点」(`cu_seqlens[B]`)。若只有 B 个元素，就无法表达总长度，也无法用 `cu_seqlens[b+1] - cu_seqlens[b]` 计算最后一条的长度。

---

### 4.2 offset 与 offset_padded：tile 对齐偏移

#### 4.2.1 概念说明

kernel 知道了 `batch_idx` 后，要解决的下一个问题是：**这条序列在打包张量里从哪里开始？** 这个起点就是 `offset = cu_seqlens[batch_idx]`。

但仅有一个精确的 `offset` 还不够。回想上一讲，注意力是按 **tile（分块，例如 128×128）** 处理的；很多硬件加载指令（尤其是 Hopper/Blackwell 的 TMA）要求被访问的地址**对齐到 tile 粒度**。而 `cu_seqlens[batch_idx]` 是任意整数，未必是 tile 的倍数。因此 FA4 同时维护两个偏移：

- `offset`：精确起点，可能不对齐。
- `offset_padded`：在 `offset` 基础上向下取整到 tile 倍数，并用 `assume(..., divby=tile)` 把「结果能被 tile 整除」这一不变量告诉编译器，便于生成对齐访存指令。

`offset` 用于普通的 `domain_offset`（视图平移，逻辑寻址），`offset_padded` 用于需要严格对齐的代码路径（如 TMA 描述符构造）。二者并存，各司其职。

#### 4.2.2 核心流程

`SeqlenInfo.create`（定长/单向版本）里两个偏移的计算：

\[
\text{offset} = \text{cu\_seqlens}[b]
\]

\[
\text{offset\_padded} = \left\lfloor \frac{\text{offset} + b \cdot \text{tile}}{\text{tile}} \right\rfloor \cdot \text{tile}
\]

然后对 `offset_padded` 调用 `cute.assume(x, divby=tile)`，向 CuTeDSL 编译器声明 `x % tile == 0`。`+ b * tile` 是一个布局修正项，使向下取整后的基址在 batch-strided 布局下保持一致的对齐语义；其精确取值属于实现细节，**关键点是最终结果一定是 tile 的整数倍**。

消费侧 `offset_batch(padded=...)` 按需选用其一：`padded=False` 用 `offset`，`padded=True` 用 `offset_padded`。

#### 4.2.3 源码精读

`SeqlenInfo` 是定长/单向场景下的精简版数据类，先看它如何同时算出两个偏移：

[flash_attn/cute/seqlen_info.py:32-38](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py#L32-L38) —— `offset` 直接取 `cu_seqlens[batch_idx]`；`offset_padded` 用 `(offset + batch_idx * tile) // tile * tile` 取整，并在注释里点明「Add divby so that the compiler knows the alignment when moving by offset_padded」，再用 `cute.assume(..., divby=tile)` 把对齐不变量固化下来。

> 旁注：`const_expr(cu_seqlens is None)` 是 CuTeDSL 的**编译期分支**。当某 kernel 特化不使用 varlen 时，`cu_seqlens` 恒为 `None`，整段 varlen 代码会在编译期被裁掉，生成的 PTX 里完全没有相关指令。这正是 [u3-l1](u3-l1-attention-mask.md) 讲过的「编译期特化」思想。

消费侧 `offset_batch` 根据 `padded` 形参选用对应偏移：

[flash_attn/cute/seqlen_info.py:47-63](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py#L47-L63) —— 没有 `cu_seqlens` 时直接按 `batch_idx` 索引 batch 维（定长路径）；有 `cu_seqlens` 时改用 `cute.domain_offset` 把第 0 维平移 `off`（其中 `off = multiple * (offset or offset_padded)`），把视图对齐到当前序列起点。

`SeqlenInfoQK`（双向版）里同样的两个偏移分别为 Q、K 各算一份：

[flash_attn/cute/seqlen_info.py:96-107](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py#L96-L107) —— `offset_q/offset_k` 各取自 `mCuSeqlensQ/K[batch_idx]`；`padded_offset_q/k` 用相同的 `//tile*tile` 取整 + `assume(divby=tile)`，分别按 `tile_m`、`tile_n` 对齐（Q 按 tile_m，K 按 tile_n，因为二者 tile 尺寸可以不同）。

#### 4.2.4 代码实践

**实践目标**：用 Python 复现 `offset` 与 `offset_padded` 的计算，直观感受「对齐」的效果。

**操作步骤**：

```python
# 示例代码：CPU 运行，复现 offset / offset_padded
def offset_padded(offset, batch_idx, tile):
    return (offset + batch_idx * tile) // tile * tile

cu_seqlens = [0, 128, 384, 448]   # 长度 128/256/64
tile = 128

for b in range(3):
    off = cu_seqlens[b]
    off_p = offset_padded(off, b, tile)
    print(f"b={b}  offset={off:4d}  offset_padded={off_p:4d}  "
          f"aligned? {off_p % tile == 0}")
```

**需要观察的现象**：`offset` 未必是 tile 倍数（这里恰好都是，但把序列长度改成 `100, 200, 50` 就会看到不对齐的 `offset`）；`offset_padded` 一定是 tile 倍数。

**预期结果**（用 `[100, 200, 50]`，tile=128）：每行 `aligned? True`，且可观察到 `offset_padded <= offset`（向下取整）。

**待本地验证**：把 `tile` 改成 64，观察 `offset_padded` 如何随之变化。

#### 4.2.5 小练习与答案

**练习 1**：`assume(x, divby=tile)` 在编译流程中起什么作用？去掉它会怎样？

**答**：它向 CuTeDSL 编译器声明运行期不变量 `x % tile == 0`，让编译器据此生成对齐访存指令（如 TMA）。去掉后，编译器无法确信地址对齐，可能退回到更慢的通用加载路径，或无法构造合法的 TMA 描述符。

**练习 2**：为什么 `SeqlenInfoQK` 里 Q 用 `tile_m` 对齐、K 用 `tile_n` 对齐，而不是统一用一个 tile？

**答**：因为前向 kernel 中 Q 的分块尺寸是 `tile_m`、K/V 的分块尺寸是 `tile_n`，二者可以不同。TMA 描述符的对齐粒度取决于对应张量实际的拷贝块大小，所以 Q、K 必须各自按自己的 tile 对齐。

---

### 4.3 SeqlenInfoQK：Q/K 长度一次性读取与跟踪

#### 4.3.1 概念说明

`SeqlenInfoQK` 是本讲真正的核心。它解决一个工程问题：**主循环里很多地方都需要 `seqlen_q`、`seqlen_k`、`offset_q`、`offset_k`、`num_n_blocks` 这些值**——`BlockInfo.get_n_block_min_max` 要用、元素级掩码 `apply_mask` 要用、Q/K/V 的 `domain_offset` 也要用。如果每个用途都各自去显存读一遍 `cu_seqlens`，会造成大量重复的全局内存访问（HBM 读写昂贵，参见 [u1-l1](u1-l1-what-is-flashattention.md) 讲过的 IO 感知思想）。

`seqlen_info.py` 顶部注释把这一设计意图说得很清楚：

> This consolidates all the info related to sequence length. This is so that we can do all the gmem reads once at the beginning of each tile, rather than having to repeat these reads to compute various things like n_block_min, n_block_max, etc.

「把所有与序列长度相关的信息集中起来，让我们能在每个 tile 开头把 gmem 读取**做一次**就够了，而不是在算 `n_block_min`、`n_block_max` 等时反复读。」

所以 `SeqlenInfoQK` 的本质是：**在每个 tile 开头一次性读完所有需要的长度信息，缓存进寄存器（Int32 字段），后续全程复用。**

#### 4.3.2 核心流程

`SeqlenInfoQK.create(batch_idx, ...)` 在 tile 开始时执行，按以下优先级解析长度（三种来源，互斥）：

1. **`seqused` 给定**（「定长布局 + 真实长度」场景，张量仍带 batch 维但序列被补零，`seqused[b]` 给出真实长度）：

   \[
   \text{seqlen\_q} = \text{seqused\_q}[b]
   \]

2. **`cu_seqlens` 给定**（varlen 打包场景）：

   \[
   \text{seqlen\_q} = \text{cu\_seqlens\_q}[b+1] - \text{cu\_seqlens\_q}[b]
   \]

3. **二者都没有**（纯定长场景）：

   \[
   \text{seqlen\_q} = \text{seqlen\_q\_static}
   \]

K 的长度同理。此外它还顺带预算：

\[
\text{num\_n\_blocks} = \left\lceil \frac{\text{seqlen\_k}}{\text{tile\_n}} \right\rceil
= \frac{\text{seqlen\_k} + \text{tile\_n} - 1}{\text{tile\_n}}
\]

以及块稀疏路径需要的 `m_block_offset`、`block_idx_offset`（默认 `block_idx_offset = m_block_offset * num_n_blocks`）。四个布尔标志 `has_cu_seqlens_q/k`、`has_seqused_q/k` 是 `Constexpr[bool]`，编译期决定，从而把上面的三选一分支在编译期彻底消除——每种特化只包含自己那一条路径的 PTX。

#### 4.3.3 源码精读

**核心数据类定义**——注意字段类型，区分编译期常量与运行期寄存器值：

[flash_attn/cute/seqlen_info.py:66-80](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py#L66-L80) —— `offset_q/offset_k/seqlen_q/seqlen_k/num_n_blocks` 等是运行期 `Int32`（每个 tile 不同，存寄存器）；`has_cu_seqlens_q/k`、`has_seqused_q/k` 是 `Constexpr[bool]`（编译期固定，决定特化）。这种「运行期数值 + 编译期标志」的混合，是 FA4 kernel 特化的典型手法。

**工厂方法 `SeqlenInfoQK.create`**——集中读取、集中计算：

[flash_attn/cute/seqlen_info.py:82-145](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py#L82-L145) 把所有 gmem 读 + 算术压缩在一个静态方法里。

其中长度解析的三选一逻辑：

[flash_attn/cute/seqlen_info.py:108-123](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py#L108-L123) —— Q 优先用 `mSeqUsedQ`，其次 `mCuSeqlensQ[b+1] - offset_q`，最后退到 `seqlen_q_static`；K 同理。注意 varlen 分支用 `cu_seqlens[b+1] - offset_q`，而 `offset_q` 已经在 [L96](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py#L96) 读过一次了——复用已读到的寄存器值，不重复访存。

`num_n_blocks` 与块稀疏偏移的预算：

[flash_attn/cute/seqlen_info.py:124-130](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py#L124-L130) —— `num_n_blocks = (seqlen_k + tile_n - 1) // tile_n` 即向上取整；`block_idx_offset` 在块稀疏索引张量存在时取 `mCuBlockIdxOffsets[b]`，否则退化为 `m_block_offset * num_n_blocks`（块稀疏是 [u10-l1](u10-l1-block-sparsity.md) 的主题，这里只需知道它被顺带算好）。

**消费者：前向 kernel 怎么用它。**

[flash_attn/cute/flash_fwd.py:795-803](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L795-L803) —— 在主循环开始前一次性 `SeqlenInfoQK.create(...)`，传入 `batch_idx`、静态长度、四个可选张量。注意 `seqlen_q_static/seqlen_k_static` 取的是 `mQ.shape[0]/mK.shape[0]`——在 varlen 下这只是「兜底静态值」，实际会被 `cu_seqlens` 覆盖。

构造完立刻喂给 `BlockInfo`：

[flash_attn/cute/flash_fwd.py:804-809](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L804-L809) —— `block_info.get_n_block_min_max(seqlen, m_block)` 返回当前 Q tile 要遍历的 K/V 块范围；注释还点出一个 varlen 陷阱：网格里「多余的 tile」（`batch_idx >= num_batch`）会得到 `seqlen_q=seqlen_k=0`、`n_block_max=0`，于是用 `cutlass.max(n_block_max - 1, 0)` 把起始 n_block 钳到 0，避免负索引。

随后用 `offset_q/offset_k` 把 Q/K/V 视图平移到当前序列：

[flash_attn/cute/flash_fwd.py:818-827](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L818-L827) —— 定长路径用 `mQ[None, None, num_head, batch_size]` 直接按 batch 索引；varlen 路径用 `cute.domain_offset((seqlen.offset_q, 0), mQ[None, None, num_head])` 平移第 0 维。这就是 `SeqlenInfoQK` 缓存的 `offset_q` 真正被消费的地方。

**消费者：`BlockInfo` 取用长度字段。**

[flash_attn/cute/block_info.py:24-38](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L24-L38) —— `n_block_max = ceil_div(seqlen_info.seqlen_k, tile_n)`、对角索引 `n_idx = m_idx_max + seqlen_info.seqlen_k - seqlen_info.seqlen_q`，正是 [u3-l2](u3-l2-block-info.md) 讲过的因果/滑窗裁剪。两个长度值都直接读自 `SeqlenInfoQK` 的寄存器字段，**不再触碰显存**——这就是「一次性读取」的收益。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读，跟踪 `SeqlenInfoQK` 在一个 tile 生命周期里的「一次构造、多处消费」。

**操作步骤**：

1. 打开 [seqlen_info.py:82-145](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/seqlen_info.py#L82-L145)，列出 `create` 里所有从显存读出的量（提示：`mCuSeqlensQ[batch_idx]`、`mCuSeqlensQ[batch_idx+1]`、`mSeqUsedQ[batch_idx]` 及 K 侧对应项、`mCuTotalMBlocks[batch_idx]`、`mCuBlockIdxOffsets[batch_idx]`）。
2. 打开 [flash_fwd.py:795-827](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L795-L827)，找出 `seqlen`（即 `SeqlenInfoQK` 实例）被消费的全部位置：`get_n_block_min_max(seqlen, ...)`、`seqlen.has_cu_seqlens_q`、`seqlen.offset_q`、`seqlen.offset_k`。
3. 打开 [block_info.py:24-55](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L24-L55)，确认 `seqlen_info.seqlen_q/seqlen_k` 在裁剪逻辑里被引用了几次。

**需要观察的现象**：显存读取**只发生在 `create` 里**；之后所有用途（`BlockInfo` 裁剪、`domain_offset`、掩码）读的都是已缓存的寄存器字段，不再有 `cu_seqlens[...]` 的访存。

**预期结果**：你能画出一张「`create` 一次性读 gmem → 缓存为 Int32 字段 → 被 BlockInfo/掩码/视图偏移多次复用」的流向图，从而说明为何这种集中读取能省下大量重复 HBM 访问。

**待本地验证**：若想验证字段确实进了寄存器，可结合 [u11-l5](u11-l5-debugging-ptx-sass.md) 的方法导出 PTX，检查 `create` 之后是否没有重复的 `ld.global`（加载全局内存）指令。

#### 4.3.5 小练习与答案

**练习 1**：`has_cu_seqlens_q` 为什么用 `Constexpr[bool]` 而不是普通 `bool`？

**答**：因为「是否 varlen」在**编译期**就确定了（一个 kernel 特化要么总是处理 varlen，要么总不处理）。用 `Constexpr[bool]` 配合 `const_expr(...)` 分支，能让编译器在生成 PTX 时直接裁掉不走的分支（如定长特化里完全没有 `cu_seqlens` 相关指令），既减少寄存器占用也避免无用的分支判断。普通 `bool` 会保留运行期分支。

**练习 2**：`seqused_q` 与 `cu_seqlens_q` 都能告诉 kernel 序列长度，它们分别对应什么输入场景？

**答**：`cu_seqlens_q` 对应**紧凑打包**（batch 维折叠进 seqlen，张量是 `(total_q, ...)`，靠前缀和定位边界）；`seqused_q` 对应**定长补零布局**（张量仍是 `(batch, max_seqlen, ...)`，但每条序列真实长度由 `seqused_q[b]` 给出，多余位置是补零）。二者互斥，且 `seqused` 优先级高于 `cu_seqlens`。

**练习 3**：网格里出现 `batch_idx >= num_batch` 的「多余 tile」时，`SeqlenInfoQK` 会给出什么值？kernel 如何自保？

**答**：对 varlen，网格通常按最大可能的 batch 数铺满，于是某些 tile 的 `batch_idx` 越界。读 `cu_seqlens[batch_idx]` 会得到末尾的累积值，进而 `seqlen_q = cu_seqlens[batch_idx+1] - cu_seqlens[batch_idx]` 退化为 0（或被防护逻辑置 0），`n_block_max = ceil(0/tile_n) = 0`。kernel 用 `cutlass.max(n_block_max - 1, 0)` 钳住起始 n_block，且加载/存储谓词在 `seqlen==0` 时全部为假，从而该 tile 不做任何有效计算也不越界访存（见 [flash_fwd.py:805-809](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L805-L809)）。

---

## 5. 综合实践

把本讲三个最小模块串起来：构造 3 条长度为 `128/256/64` 的序列，分别用 **varlen 打包** 与 **逐条定长** 两种方式跑 FA4 前向，验证二者数值一致。

```python
# 示例代码：需 CUDA + flash-attn-4 安装；无 GPU 时可只阅读，理解数据流
import torch
from flash_attn.cute import flash_attn_func, flash_attn_varlen_func

torch.manual_seed(0)
device, dtype = "cuda", torch.float16
H, D = 8, 64
lens = [128, 256, 64]                         # 三条不等长序列
B = len(lens)

# ---- 模块 4.1：构造 cu_seqlens 与打包张量 ----
cu = [0]
for L in lens:
    cu.append(cu[-1] + L)
cu_seqlens_q = torch.tensor(cu, dtype=torch.int32, device=device)  # [0,128,384,448]
cu_seqlens_k = cu_seqlens_q.clone()
total_q = cu[-1]

q = torch.randn(total_q, H, D, device=device, dtype=dtype)        # 自注意力：q=k
v = torch.randn(total_q, H, D, device=device, dtype=dtype)

# ---- varlen 一次调用 ----
out_varlen, lse_varlen = flash_attn_varlen_func(
    q, q, v,
    cu_seqlens_q=cu_seqlens_q,
    cu_seqlens_k=cu_seqlens_k,
    max_seqlen_q=max(lens),
    max_seqlen_k=max(lens),
    causal=True,
)
# varlen 的 LSE 形状是 (num_head, total_q)，与定长不同
print("out_varlen:", tuple(out_varlen.shape), "lse_varlen:", tuple(lse_varlen.shape))

# ---- 模块 4.3 思路：逐条定长调用，再按 cu_seqlens 拼回 ----
outs_ref = []
for b, L in enumerate(lens):
    sl, sr = int(cu_seqlens_q[b]), int(cu_seqlens_q[b + 1])
    qb = q[sl:sr].unsqueeze(0)                # (1, L, H, D) 定长布局
    vb = v[sl:sr].unsqueeze(0)
    out_b, _ = flash_attn_func(qb, qb, vb, causal=True)
    outs_ref.append(out_b.squeeze(0))
out_ref = torch.cat(outs_ref, dim=0)          # 拼回 (total_q, H, D)

# ---- 对比 ----
print("max abs diff:", (out_varlen - out_ref).abs().max().item())
```

**需要观察的现象与预期结果**：

1. `out_varlen` 形状为 `(448, 8, 64)`，与拼接得到的 `out_ref` 完全一致；`lse_varlen` 形状为 `(8, 448)`（num_head 在前，印证 [interface.py:472](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L472) 的 varlen LSE 布局）。
2. `max abs diff` 应在 fp16 舍入误差量级（典型 \(10^{-3}\) 级别），说明 varlen 与逐条定长在数学上等价，差异仅来自浮点舍入。
3. 改变 `lens`（如 `[100, 200, 50]`，含非 tile 倍数长度）应仍得到一致结果——这正验证了 `offset`（精确起点）与元素级掩码协同处理了不对齐边界。

**待本地验证**：本实践需要 Blackwell 或 Hopper GPU 及 `flash-attn-4`；若无 GPU，请改做 4.3.4 的源码阅读型实践（跟踪 `SeqlenInfoQK.create` 的一次构造、多处消费）。

## 6. 本讲小结

- varlen 通过把 batch 维折叠进 seqlen 维、用前缀和数组 `cu_seqlens`（长度 `batch+1`）定位每条序列边界，彻底消除补零浪费；FA4 在 [interface.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py) 严格校验其形状为 `(batch+1,)`、int32、连续。
- `offset = cu_seqlens[batch_idx]` 是精确起点；`offset_padded` 把它向下取整到 tile 倍数并用 `assume(divby=tile)` 告知编译器对齐不变量，二者分别服务于逻辑寻址与对齐访存（TMA）。
- `SeqlenInfoQK` 的设计精髓是「**每个 tile 开头一次性 gmem 读取、之后全程寄存器复用**」，把 `seqlen_q/seqlen_k/offset_q/offset_k/num_n_blocks` 等集中算好，供 `BlockInfo`、掩码、视图偏移多处消费，避免重复 HBM 访问。
- 长度解析有三来源、按优先级互斥：`seqused`（定长补零布局）> `cu_seqlens`（varlen 打包）> `seqlen_static`（纯定长）；由 `Constexpr[bool]` 标志在编译期裁剪分支。
- varlen 改变了输出形状：`out` 退去 batch 维成 `(total_q, ...)`，LSE 由 `(batch, num_head, seqlen_q)` 变为 `(num_head, total_q)`。
- 越界的「多余 tile」会得到 `seqlen=0` 与 `n_block_max=0`，靠 `cutlass.max(...,0)` 钳位与加载谓词自保，不越界访存。

## 7. 下一步学习建议

本讲把「长度与偏移」这一维度讲完了，`BlockInfo`（[u3-l2](u3-l2-block-info.md)）+ `SeqlenInfoQK`（本讲）合起来，已经能算出每个 Q tile 的合法 K/V 遍历范围。接下来：

- **第 4 单元** 进入在线 softmax 与 `score_mod`，看 `n_block_min/max` 算出的范围里 softmax 如何分块累加、rescale。
- **第 5 单元** 讲流水线与 TMA 拷贝，那时你会更明白 `offset_padded` 与 `assume(divby=tile)` 为何对 TMA 描述符必不可少。
- **第 6 单元** 进入前向 kernel 主循环（`flash_fwd.py`），把本讲的 `SeqlenInfoQK.create` 与 `domain_offset` 放回完整上下文中。
- 若你对块稀疏感兴趣，可预习 [u10-l1 块稀疏注意力](u10-l1-block-sparsity.md)，届时会看到 `SeqlenInfoQK` 里 `m_block_offset`、`block_idx_offset`、`num_n_blocks` 三个字段的真正用武之地。
