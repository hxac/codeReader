# 块稀疏注意力

## 1. 本讲目标

本讲聚焦 FA4 的**块稀疏注意力（block-sparse attention）**机制。读完本讲，你应当能够：

- 说清「块稀疏」与「元素级掩码」的区别，以及块粒度上 full / partial（mask）/ skip 三态分块的含义。
- 看懂 FA4 用一组张量（`mask_block_cnt` / `mask_block_idx` / `full_block_cnt` / `full_block_idx` 等）描述稀疏块结构的 **ordered 稀疏表示**，并知道 `cnt + idx` 是怎样一种「左对齐压缩」格式。
- 理解 ordered 稀疏与稠密掩码之间的互转（`_ordered_to_dense_simple`），以及 `compute_block_sparsity` 如何用一个 `mask_mod` 把整张块掩码「分类」成三态。
- 掌握 `block_sparse_tensors` 这个公共参数如何接入 `_flash_attn_fwd` / `_flash_attn_bwd`：归一化、`q_subtile_factor`、进入 `compile_key`、并在 kernel 内被 producer / consumer 侧逐块消费。

本讲是「稀疏注意力与 MLA」单元的第一篇，承接 [u3-l1 AttentionMask](u3-l1-attention-mask.md) 里建立的 `mask_mod` 与元素级掩码抽象。

## 2. 前置知识

在进入块稀疏之前，请确认你理解以下几个概念（它们都来自前置讲义）：

- **tile（分块）**：FA4 把 Q 序列切成 `tile_m` 大小的 Q tile，把 K/V 序列切成 `tile_n` 大小的 KV tile，注意力被拆成「一个 Q tile 对一个 KV tile」的小矩阵乘。于是序列被映射到一个 `num_m_blocks × num_n_blocks` 的**块网格**，其中：

\[
\text{num\_m\_blocks} = \lceil \text{seqlen}_q / \text{tile\_m} \rceil,\qquad
\text{num\_n\_blocks} = \lceil \text{seqlen}_k / \text{tile\_n} \rceil
\]

- **mask_mod**：一个固定签名 `(batch, head, q_idx, kv_idx, seqlen_info, aux_tensors) -> bool` 的 `@cute.jit` 回调，在 softmax **之前**判定某对 token 是否可见（[u3-l1](u3-l1-attention-mask.md)）。可见返回 `True`，不可见返回 `False`。
- **元素级掩码的代价**：朴素做法是对每个 tile 里的每个 token 对都调一次 `mask_mod`、再把不可见位置改写成 `-inf`。即使一整块都被掩掉，kernel 仍要把这块 K/V 从显存搬进来、做一次 GEMM、再做一次全 `-inf` 的 softmax——纯属浪费带宽与算力。
- **在线 softmax**（[u4-l1](u4-l1-online-softmax.md)）：行最大值与行和逐块维护，这让「跳过某些块」变得安全——只要被跳过的块本该全被掩掉，跳过它们不改变最终结果。

> **关键直觉**：块稀疏是一种**加速手段，不是新算法**。它先在块粒度上判断每个块的状态，把整块可见的（full）和整块不可见的（skip）挑出来：full 块连 `mask_mod` 都不用算，skip 块连 K/V 都不必加载；只有「半遮半掩」的 partial 块才走元素级掩码。最终数学结果与「对所有 token 调 mask_mod」完全一致。

## 3. 本讲源码地图

本讲涉及的关键源码文件（都在 `flash_attn/cute/` 下）：

| 文件 | 作用 |
| --- | --- |
| [block_sparsity.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py) | 定义 `BlockSparseTensors` / `BlockSparseTensorsTorch` 数据结构、ordered↔dense 转换、形状归一化与校验、`dq_write_order` 计算。是「数据结构与工具层」。 |
| [compute_block_sparsity.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/compute_block_sparsity.py) | `BlockSparsityKernel`：把一个 `mask_mod` 在块粒度上**分类**成 full/partial/skip，并写出 `cnt`/`idx`；外层 `compute_block_sparsity(...)` 负责分配张量与 JIT 编译。是「稀疏结构的构建层」。 |
| [block_sparse_utils.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py) | kernel 运行时用的 producer/consumer 函数：`get_curr_blocksparse_tensors`、`produce_block_sparse_loads`、`consume_block_sparse_loads` 等，以及反向（转置索引）版本。是「kernel 内的执行层」。 |
| [interface.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py) | 公共参数 `block_sparse_tensors` 的接入点：校验、`normalize_block_sparse_config`、写进 `compile_key`、转成 cute 张量喂给 kernel。 |

三个文件恰好对应三层：**数据结构 → 结构构建 → kernel 执行**，外加 `interface.py` 把它们串到公共 API。

## 4. 核心概念与源码讲解

### 4.1 块稀疏动机：full / partial / skip 三态分块

#### 4.1.1 概念说明

给定一个 `mask_mod`，把它作用到某个块 `(m_block, n_block)` 覆盖的所有 token 对上，这块只有三种结局：

- **full block（全可见）**：块内每一对 token 都满足 `mask_mod == True`。既然整块都没有被掩的位置，softmax 时根本不需要元素级掩码——直接当普通 GEMM 算即可，省掉逐元素 `mask_mod` 调用。
- **partial block（部分可见，又称 mask block）**：块内既有可见也有不可见的 token。这种块**必须**加载 K/V、做 GEMM、并在 softmax 前对不可见位置打 `-inf`（即走元素级 `mask_mod`）。
- **skip block（全不可见）**：块内每一对 token 都 `mask_mod == False`。这种块对当前 Q tile 的贡献恒为 0，**整块跳过**——不加载 K/V、不做 GEMM。

三态分类只在「块粒度」上做一次判断，却能让 kernel 跳过大段无效工作。以因果掩码为例：下三角以外有大量 skip 块（直接不加载），下三角内部全是 full 块（省掉逐元素判断），只有主对角线附近那一块是 partial。

> 用在线 softmax 的语言：被跳过的 skip 块本就不贡献任何概率质量（它们在元素级掩码下会被改成 `-inf`，对 `row_max`/`row_sum` 无影响），所以「块级跳过」与「元素级 `-inf`」在数学上完全等价——这正是块稀疏「精确而非近似」的根源。

#### 4.1.2 核心流程

`compute_block_sparsity.py` 里的 `BlockSparsityKernel` 在每个 `(batch, head, m_block)` 上启动一个程序块，遍历该 Q tile 对应的所有 `n_block`，逐块判定三态并写回计数：

```text
for n_block in range(num_n_blocks):
    采样块 (m_block, n_block) 内的若干 (q_idx, kv_idx) 对
    has_unmasked = 块内是否存在 mask_mod == True 的 token
    has_masked   = 块内是否存在 mask_mod == False 的 token
    if has_masked and has_unmasked:   # partial
        mask_idx[cnt_mask] = n_block; cnt_mask += 1
    elif has_unmasked and not has_masked:  # full
        full_idx[cnt_full] = n_block; cnt_full += 1
    # else: skip，什么都不写
最后 thread 0 写回 mask_cnt[...] = cnt_mask, full_cnt[...] = cnt_full
```

注意 full 与 partial 用**两组独立的列表**存放，这是为了在 kernel 内消费时区分「要不要套元素级掩码」。

#### 4.1.3 源码精读

三态判定与写回的核心就在 kernel 的内层循环末尾，由 thread 0 统一更新两个计数器：

[compute_block_sparsity.py:306-319](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/compute_block_sparsity.py#L306-L319) —— `is_partial`（既有 masked 又有 unmasked）写入 `mask_idx`，`is_full`（只有 unmasked）写入 `full_idx`，全 masked 的块两边都不写（即 skip）。

[compute_block_sparsity.py:322-331](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/compute_block_sparsity.py#L322-L331) —— 循环结束后，thread 0 把累加出的 `num_mask_blocks` / `num_full_blocks` 写回 `mask_cnt` / `full_cnt`（定长走 `[batch, head, m]`，变长走 `[head, global_m_block]`）。

判定「块内有没有 unmasked / masked」有两种采样策略：精确路径让每个线程扫一列、用 `vote_any_sync` 做 warp 内归约、再用共享内存做跨 warp 归约；`use_fast_sampling=True` 时退化为只看 4 个角 + 中心共 5 个点（适用于掩码形状足够「规则」的场景，见 [compute_block_sparsity.py:189-242](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/compute_block_sparsity.py#L189-L242)）。注意一个细节：**越界（OOB）区域在 PyTorch 的 `BlockMask` 里被视为 masked，但在 CuTe kernel 里被视为「无需检查」**，所以边界块在两者间的 full/partial 归类可能不同（测试里专门处理了这个差异）。

#### 4.1.4 代码实践

**目标**：直观感受三态分类如何省工作量。

**步骤**：

1. 阅读上面的 [compute_block_sparsity.py:306-331](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/compute_block_sparsity.py#L306-L331) 两段代码。
2. 在脑中（或纸上）画一个 `seqlen=512`、`tile=128` 的因果掩码块网格（`4 × 4` 共 16 块）。
3. 标出每块的三态：下三角内部（full）、主对角线（partial）、上三角（skip）。

**预期结果**：16 块中约有 `1+2+3=6` 块是 full（下三角内部，含对角线下方），4 块是 partial（主对角线），6 块是 skip（上三角）。也就是说有接近一半的块根本不会被加载，对角线以下的内部块连 `mask_mod` 都不调——这正是块稀疏相对「逐元素 `mask_mod`」的加速来源。具体块数「待本地验证」（取决于边界归类约定）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `tile_n` 取得很大（比如 `tile_n = seqlen_k`，只有一列块），块稀疏还有意义吗？

> **答案**：几乎没有。此时每个 Q tile 只对应一个 KV 块，只要掩码里存在任何不可见 token，这块就是 partial，元素级掩码照样要跑；只有「整行全可见 / 整行全不可见」这种极端情况才能受益。块稀疏的收益来自「能在块粒度上挑出大段连续的 full / skip」，`tile_n` 过大就失去了这种粒度。

**练习 2**：为什么 full 块和 partial 块要存成两组独立的列表，而不是合并成一个列表？

> **答案**：因为消费方式不同。partial 块在 GEMM 后需要套元素级 `mask_mod`（把不可见位置打 `-inf`）；full 块不需要任何掩码，可以直接当无掩码 GEMM 算，省掉逐元素判断与谓词生成。分开存放让 kernel 内能用两段干净的循环分别处理，避免每个块都带一个「要不要掩码」的运行期分支。

### 4.2 BlockSparseTensors 数据结构：ordered 稀疏表示

#### 4.2.1 概念说明

三态分类的结果需要一种紧凑的数据结构存下来。FA4 用的是**类 CSR（压缩稀疏行）的 ordered 稀疏表示**——对每个 `(batch, head, m_block)`，存一个**计数**和一串**索引**：

- `mask_block_cnt[b, h, m]`：这个 Q tile 有多少个 partial KV 块。
- `mask_block_idx[b, h, m, :]`：这些 partial 块的 `n_block` 编号，**左对齐紧凑排列**，只有前 `mask_block_cnt[b,h,m]` 个有效。
- `full_block_cnt` / `full_block_idx`：同理，但存的是 full 块。

之所以叫「ordered」：每个 `m` 行的索引是按某种顺序（升序）紧凑排好的，长度由 `cnt` 决定。这比存一张稠密的 `M × N` 布尔掩码省显存得多——尤其当序列很长、稀疏度很高时，稠密掩码是 \(O(M\cdot N)\)，而 ordered 表示只占 \(O(\text{非零块数})\)。

数据结构用两个 NamedTuple 表达：kernel 内部用 `BlockSparseTensors`（cute 张量），宿主侧用 `BlockSparseTensorsTorch`（torch 张量）：

[block_sparsity.py:17-36](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L17-L36) —— kernel 侧的 `BlockSparseTensors`，前四项是必填的 `cnt`/`idx`，后面 `cu_total_m_blocks`、`cu_block_idx_offsets`、`dq_write_order` 等都是可选（变长 / 反向确定性才需要）。

[block_sparsity.py:39-49](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L39-L49) —— 宿主侧 `BlockSparseTensorsTorch`，多了 `block_size` 和 `spt` 两个纯 Python 元数据字段（不进 GPU）。

各字段含义一览：

| 字段 | 形状（定长） | 含义 |
| --- | --- | --- |
| `mask_block_cnt` | `[B, H, M]` | 每个 Q tile 的 partial 块数量 |
| `mask_block_idx` | `[B, H, M, N]` | partial 块的 `n_block` 编号（左对齐） |
| `full_block_cnt` | `[B, H, M]` | 每个 Q tile 的 full 块数量（可选） |
| `full_block_idx` | `[B, H, M, N]` | full 块的 `n_block` 编号（可选） |
| `cu_total_m_blocks` | `[B+1]` | 变长 Q 的累计 m_block 数（可选，变长才用） |
| `cu_block_idx_offsets` | `[B+1]` | 变长下每 batch 在打包 idx 里的偏移（可选） |
| `dq_write_order` | 同 `mask_block_idx` | 反向确定性用的锁值（可选） |
| `block_size` | `(tile_m, tile_n)` | 纯元数据，描述块大小 |

#### 4.2.2 核心流程

ordered 表示与稠密掩码的对应关系：

```text
对每个 m 行：
    dense[m, :] 中为 True 的列号  ——(排序、左对齐)——>  idx[m, 0..cnt-1]
    dense[m, :] 中 True 的个数                            cnt[m]
读取时：valid_cols = idx[m, 0 : cnt[m]]
```

变长（varlen）场景下，`mask_block_cnt` 退化成 `[H, total_m_blocks]`、`mask_block_idx` 退化成 `[H, total_n_blocks]`（把所有 batch 的行首尾拼起来），再用 `cu_total_m_blocks` 在 kernel 内把全局 `m_block` 反解回 `(batch, 局部 m_block)`。是否变长由 `cu_total_m_blocks is not None` 判定。

#### 4.2.3 源码精读

**判启用**：只要 `full_block_cnt` 或 `mask_block_cnt` 任一非空，就认为开了块稀疏：

[block_sparsity.py:481-482](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L481-L482) —— `is_block_sparsity_enabled` 的实现。

**紧凑索引（Compact indices）**：一个重要优化——`idx` 的最后一维 `N` 允许**小于**实际的 `num_n_blocks`，因为 FA4 每个 Q tile 只会读 `idx[m, 0..cnt-1]`，永远不会碰到尾部。这让我们能用「每行最大 cnt」而非 `num_n_blocks` 来分配 `idx`，避免长序列下 \(O(N^2)\) 的显存：

[block_sparsity.py:245-253](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L245-L253) —— 校验时若发现 `idx.shape[-1]` 比预期小，就把「期望形状」收缩到实际值，从而放行紧凑索引。

[block_sparsity.py:372-377](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L372-L377) —— 另一处对应的注释与上界检查（只拒绝「比 N 还大」的非法情形）。

**从 torch 到 cute**：进 kernel 前，宿主侧的 torch 张量被转成 cute 张量（统一 `assumed_align=4`、最后一维连续）：

[block_sparsity.py:632-670](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L632-L670) —— `to_cute_block_sparse_tensors` 把 6 个张量逐个 `to_cute_tensor`，跳过 `None` 的可选项，组装成 kernel 侧的 `BlockSparseTensors`。

#### 4.2.4 代码实践

**目标**：动手构造一个最小的 `BlockSparseTensorsTorch`，验证 `cnt`/`idx` 的左对齐语义。

**步骤**（纯 Python，无需 GPU）：

```python
# 示例代码：手搓一个 1×1×2 行（m=0,1）、每行最多 3 个 partial 块的稀疏结构
import torch
from flash_attn.cute.block_sparsity import BlockSparseTensorsTorch

# 假设 num_m_blocks=2, num_n_blocks=4
# m=0 行：partial 块是 n_block = {1, 3}（共 2 个）
# m=1 行：partial 块是 n_block = {0}    （共 1 个）
mask_block_cnt = torch.tensor([[[2, 1]]], dtype=torch.int32)        # [B=1,H=1,M=2]
mask_block_idx = torch.tensor([[[[1, 3, 0],   # m=0: 有效是前 2 个 -> {1,3}
                                  [0, 0, 0]]]], dtype=torch.int32)  # m=1: 有效是前 1 个 -> {0}
tensors = BlockSparseTensorsTorch(
    mask_block_cnt=mask_block_cnt,
    mask_block_idx=mask_block_idx,
    block_size=(128, 128),
)
for m in range(2):
    cnt = tensors.mask_block_cnt[0, 0, m].item()
    cols = tensors.mask_block_idx[0, 0, m, :cnt].tolist()
    print(f"m={m}: cnt={cnt}, partial n_blocks={cols}")
```

**预期结果**：

```text
m=0: cnt=2, partial n_blocks=[1, 3]
m=1: cnt=1, partial n_blocks=[0]
```

观察 `m=0` 行：虽然 `idx[0,0,0]` 长度是 3，但只有前 `cnt=2` 个（`[1,3]`）有效，第 3 个位置 `0` 是无意义的填充——这就是「左对齐压缩」。本段为示例代码，未在 GPU 上运行。

#### 4.2.5 小练习与答案

**练习 1**：`mask_block_idx` 的最后一维为什么可以小于 `num_n_blocks`？kernel 读取时如何保证不出错？

> **答案**：因为 kernel 对每个 `m` 只读取 `idx[m, 0 .. cnt[m]-1]` 这一段，`cnt[m]` 之外的尾部永远不会被访问。只要每行的 `cnt[m]` 都不超过实际分配的最后一维长度，就是安全的。这正是 [block_sparsity.py:245-253](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L245-L253) 放行紧凑索引的依据，它把 \(O(N^2)\) 的 idx 显存降到 \(O(\sum_m \text{cnt}_m)\)。

**练习 2**：变长场景下，`mask_block_cnt` 的形状从 `[B,H,M]` 退化成 `[H, total_m_blocks]`，那 kernel 怎么知道当前 `m_block` 属于哪个 batch？

> **答案**：靠 `cu_total_m_blocks`（前缀和，长度 `B+1`）。kernel 拿到全局 `global_m_block` 后，用 `get_batch_from_cu_tensor(global_m_block, cu_total_m_blocks)` 二分出 `batch_idx`，再减去前缀偏移得到「batch 内的局部 m_block」。详见 [compute_block_sparsity.py:167-172](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/compute_block_sparsity.py#L167-L172)（`BlockSparsityKernel.kernel` 内的变长分支）。

### 4.3 ordered↔dense 转换与稀疏掩码的构建

#### 4.3.1 概念说明

ordered 稀疏表示对 kernel 高效，但对人不够直观；稠密布尔掩码对人直观，但占显存。FA4 在 `block_sparsity.py` 里提供 `_ordered_to_dense_simple` 做正向转换（ordered→dense），它也是反向确定性算法 `compute_dq_write_order` 的基石。

构建 ordered 表示有两条路：

1. **FA4 原生**：调 `compute_block_sparsity(mask_mod, ...)`，它内部跑 `BlockSparsityKernel` 把 `mask_mod` 在块粒度上分类，直接产出 `BlockSparseTensorsTorch`。
2. **借 PyTorch flex_attention**：用 `torch.nn.attention.flex_attention.create_block_mask(mask_mod, ...)` 得到一个 `BlockMask`，再 `bm.as_tuple()` 解包出 `kv_mask_cnt/idx`、`full_kv_cnt/idx` 等字段，手动塞进 `BlockSparseTensorsTorch`。测试套件大量使用这条路径（因为 `BlockMask` 已帮你算好 full/partial）。

两条路殊途同归：最终都是填好 `cnt`/`idx` 四件套。

#### 4.3.2 核心流程

`_ordered_to_dense_simple` 的算法（纯 PyTorch 向量化）：

```text
输入: num_blocks[B,H,M]（每行有效列数）, indices[B,H,M,max_entries]（左对齐列号）, num_cols
1. 用 arange(max_entries) < num_blocks 造一个 valid 掩码，标记哪些列号有效
2. 把无效位置改成安全的 num_cols（一个越界列，写不到真实列里）
3. scatter 把 1 写到 dense[b,h,row,safe_col]
4. 切掉最后那列越界缓冲，得到 [B,H,M,num_cols] 的 0/1 矩阵
```

`compute_dq_write_order` 则在 ordered→dense 之后做 `cumsum`，得到每个 `(m, n)` 在「该 m 行的全部贡献者排序列表」中的**秩（rank）**，作为反向 dQ 累加的锁值，保证确定性且无死锁。

#### 4.3.3 源码精读

**ordered→dense**：

[block_sparsity.py:52-77](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L52-L77) —— `_ordered_to_dense_simple`：用 `valid = arange < num_blocks` 屏蔽无效索引，把无效列号替换成 `num_cols`（越界缓冲列），再 `dense[..., safe_indices] = 1`，最后切掉缓冲列。

**构建入口**：

[compute_block_sparsity.py:334-353](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/compute_block_sparsity.py#L334-L353) —— `compute_block_sparsity(...)` 的签名：接收 `tile_m/tile_n`、`mask_mod`、形状参数与变长可选张量，返回填好的 `BlockSparseTensorsTorch`。

[compute_block_sparsity.py:432-461](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/compute_block_sparsity.py#L432-L461) —— 分配四件套张量（定长 `[B,H,M]` / `[B,H,M,N]`，或变长 `[H,total_*]`），并打包成 `BlockSparseTensorsTorch(block_size=(tile_m,tile_n))`。

[compute_block_sparsity.py:470-483](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/compute_block_sparsity.py#L470-L483) —— 这个分类 kernel 自己的 `compile_key`：包含 `tile_m/tile_n`、`mask_mod_hash`、是否变长、是否 `use_fast_sampling` 等。换 `mask_mod` 或换 tile 都会触发重编译（缓存挂在 `compute_block_sparsity.compile_cache` 上）。

**反向锁值**：

[block_sparsity.py:80-148](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L80-L148) —— `compute_dq_write_order`：先把前向的 partial/full 两张 ordered 表合并成 dense，按列 `cumsum` 得到 rank 表，再按反向的 `(n_block, m_block)` 索引 gather 出每个贡献者的锁值；`spt=True` 时倒序赋值以匹配另一种 CTA 调度顺序，保证无死锁。

#### 4.3.4 代码实践

**目标**：用 `compute_block_sparsity` 从一个 `mask_mod` 构建 ordered 表示，再用 `_ordered_to_dense_simple` 转回稠密掩码，肉眼核对三态分类。

**步骤**：

```python
# 示例代码：需要一个 CUDA 设备（SM90+）才能真跑；下面给出可读脚本
import torch
from flash_attn.cute.compute_block_sparsity import compute_block_sparsity
from flash_attn.cute.block_sparsity import _ordered_to_dense_simple

B, H, Sq, Sk, tm, tn = 1, 1, 256, 256, 64, 64

@cute.jit  # 实际使用时请按 u3-l1 的 mask_mod 写法标注 @cute.jit
def causal_mask(b, h, q_idx, kv_idx, sl, aux):
    return q_idx >= kv_idx   # 因果：q 看得到 kv <= q

tensors = compute_block_sparsity(
    tile_m=tm, tile_n=tn, batch_size=B, num_heads=H,
    seqlen_q=Sq, seqlen_k=Sk, mask_mod=causal_mask,
    aux_tensors=None, device="cuda",
)
cnt, idx, fcnt, fidx = tensors.mask_block_cnt, tensors.mask_block_idx, \
                       tensors.full_block_cnt, tensors.full_block_idx

# 转成稠密 0/1 矩阵看一眼 partial 块的分布
dense_partial = _ordered_to_dense_simple(cnt, idx, Sk // tn)   # [B,H,M,N]
print("partial 块数（每行）:", cnt[0, 0].tolist())
print("full 块数（每行）:  ", fcnt[0, 0].tolist())
```

**需要观察的现象**：

- 因果掩码下，`m` 越大（Q 越靠后），partial 块数应该稳定在 1（只有对角那块），full 块数随 `m` 线性增长。
- `dense_partial` 在 `(m, n)` 平面上应只在主对角线附近为 1。

**预期结果**：`cnt[0,0]` 大致为 `[1,1,1,1]`（4 个 Q tile 各有 1 个 partial），`fcnt[0,0]` 大致为 `[0,1,2,3]`。具体数值「待本地验证」（依赖边界归类约定）。

#### 4.3.5 小练习与答案

**练习 1**：`_ordered_to_dense_simple` 里为什么要把无效索引替换成 `num_cols`（一个越界列），而不是直接 `dense[..., indices] = 1`？

> **答案**：因为无效位置上的 `indices` 值是未初始化的垃圾（可能是任意 `n_block`），直接 scatter 会把 1 错写到某个真实列，污染结果。替换成一个安全的越界列 `num_cols`（dense 多分配了一列缓冲），垃圾索引只会写进这个永不被读取的缓冲列，最后切掉即可——这是稀疏转稠密里常见的「pad 到安全列」技巧。

**练习 2**：`compute_block_sparsity` 的 `compile_key` 里包含 `cu_seqlens_q is None`、`cu_seqlens_k is None` 等布尔。为什么这些也要进 key？

> **答案**：是否变长决定了 kernel 内取 `m_block` / `batch_idx` 的代码路径（定长走 `block_idx()` 三元组，变长走 `get_batch_from_cu_tensor`），这是编译期分支（`const_expr`）。变长与否会编译出不同的 kernel 二进制，所以必须进 `compile_key` 才能正确命中/失效缓存。

### 4.4 block_sparse_tensors 如何接入前向 / 反向 kernel

#### 4.4.1 概念说明

有了数据结构和构建方法，最后一步是把 `block_sparse_tensors` 接到公共 API `_flash_attn_fwd` / `_flash_attn_bwd`。接入分四件事：

1. **校验与归一化**：`normalize_block_sparse_config` 检查形状、dtype、设备，把 batch/head 维可广播的张量 `expand` 到完整形状，并推断 `q_subtile_factor`。
2. **写进 `compile_key`**：块稀疏是否启用、广播模式、是否变长等都会改变编译出的 kernel，必须进 key。
3. **转成 cute 张量**喂给 kernel。
4. **kernel 内消费**：每个 Q tile 在进入主循环前，先用 `get_curr_blocksparse_tensors` 取出本 tile 的四件套（本 m 行的 partial/full 列表），再由 producer 侧 `produce_block_sparse_loads` 只加载这些 KV 块、consumer 侧 `consume_block_sparse_loads` 只对这些块做 MMA。

这里有个关键的「子分块」概念 **`q_subtile_factor`**：FA4 的前向在 SM100 上一次流水会同时载入 2 个 Q 子 tile（`q_stage=2`）。为了让稀疏块大小与流水对齐，稀疏的 `block_size_q` 必须是 `q_stage * tile_m` 的整数倍，这个倍数就是 `q_subtile_factor`。kernel 里要把逻辑 `m_block` 除以 `q_subtile_factor`（以及除以 `qhead_per_kvhead`，见 [u7-l1 pack_gqa](u7-l1-pack-gqa.md)）才能定位到稀疏张量的行。

#### 4.4.2 核心流程

前向接入链路：

```text
_flash_attn_fwd(block_sparse_tensors=...)
  └─ use_block_sparsity = block_sparse_tensors is not None
  └─ normalize_block_sparse_config(...) -> normalized_tensors, broadcast_pattern, q_subtile_factor
  └─ compile_key 里加入 use_block_sparsity / broadcast_pattern / 变长标志 / q_stage ...
  └─ to_cute_block_sparse_tensors(normalized_tensors) -> BlockSparseTensors（喂 kernel）
  └─ kernel 主循环每个 m_block:
        get_curr_blocksparse_tensors(...) -> (mask_cnt, mask_idx, full_cnt, full_idx) for THIS m
        produce_block_sparse_loads(...)  -> 只把 mask_idx ∪ full_idx 里的 KV 块搬进 smem
        consume_block_sparse_loads(...)  -> 只对这些块做 MMA（partial 套 mask_mod，full 不套）
```

反向链路是对偶的：反向外层循环是 `n_block`（KV tile），内层是 `m_block`（Q tile），所以反向用的是**转置后**的稀疏张量（「Q direction」索引：`q_mask_cnt[b,h,n]` = 这个 KV tile 要处理多少个 Q tile）。这就要求用户为反向单独提供一份 `block_sparse_tensors_bwd`，并在确定性模式下额外提供 `dq_write_order` 与 `spt`。

#### 4.4.3 源码精读

**公共参数**：`block_sparse_tensors` 作为 `_flash_attn_fwd` 的可选参数：

[interface.py:325](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L325) —— 参数声明；[interface.py:342](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L342) —— 文档说明「一组用于块稀疏的张量」。

**启用判定 + pack_gqa 互斥**：

[interface.py:511](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L511) —— `use_block_sparsity = block_sparse_tensors is not None`。

[interface.py:632-640](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L632-L640) —— 块稀疏 + pack_gqa 同用时，要求稀疏张量的 head 维必须是 1（广播），否则强制关掉 pack_gqa；变长 Q 还要求提供 `cu_total_m_blocks`。

**归一化与子分块推断**：

[interface.py:646-659](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L646-L659) —— 调 `normalize_block_sparse_config`，拿回归一化后的张量、广播模式、`q_subtile_factor`。

[block_sparsity.py:519-582](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L519-L582) —— `normalize_block_sparse_config`：区分定长 / 变长两条路，校验 `sparse_block_size_kv == tile_n`、`sparse_block_size_q % (q_stage*tile_m) == 0`，再委托 `normalize_block_sparse_tensors` 做 `expand` 与 dtype/device 检查。

[block_sparsity.py:294-384](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L294-L384) —— `infer_block_sparse_expected_shapes`：推断期望形状并算出 `q_subtile_factor = sparse_block_size_q // (q_stage * tile_m)`，把「用户的稀疏块大小」对齐到「kernel 的流水子分块」。

**写进 compile_key**：

[interface.py:728-729](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L728-L729) —— `use_block_sparsity` 与 `block_sparse_broadcast_pattern` 进 key（广播模式改变是因为 CuTe 把 stride=0 当静态，必须显式入键才能在模式变化时重编译）。

[interface.py:744-745](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L744-L745) —— 变长相关的两个布尔（`cu_total_m_blocks` / `cu_block_idx_offsets` 是否为 None）也进 key。

**kernel 内取本 tile 的四件套**：

[block_sparse_utils.py:68-82](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L68-L82) —— `get_curr_blocksparse_tensors`：按 `mask_block_cnt.shape` 的维数分发——2D 走变长路径、4D 走定长路径，返回当前 `(batch, head, m_block)` 的 `(mask_cnt, mask_idx, full_cnt, full_idx)`。

[block_sparse_utils.py:48-65](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L48-L65) —— 定长路径：直接 `mask_block_cnt[batch, head, m_block]` 取计数、`mask_block_idx[batch, head, m_block, None]` 取这一行的索引切片。

[block_sparse_utils.py:206-218](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L206-L218) —— `sparse_tensor_m_block`：把逻辑 `m_block` 除以 `qhead_per_kvhead` 与 `q_subtile_factor`，映射到稀疏张量的行号（编译期特化，因子为 1 时整段消失）。

**producer / consumer 侧**：

[block_sparse_utils.py:221-262](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L221-L262) —— `produce_block_sparse_loads`：先取本 tile 的四件套，再分四类情况（mask 空/非空 × full 空/非空）调用 `load_block_list` 把需要的 KV 块搬进流水线。`intra_wg_overlap` 时还会把 K 与 V 的载入交错重叠以藏延迟。

[block_sparse_utils.py:365-412](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L365-L412) —— `consume_block_sparse_loads`：consumer 侧镜像同样的索引逻辑，但只对取到的块做 `mma_one_n_block`；partial 列表传 `mask_mod`（套元素级掩码），full 列表传 `mask_mod=None`（不掩码）。`processed_any = mask_cnt + full_cnt > 0` 用来标记「这个 Q tile 是否完全没活干」（空 tile，见下）。

**空 tile 的处理（SM100）**：若某个 Q tile 的 `total_block_cnt == 0`（所有 KV 块都 skip），softmax warp-group 没有任何行统计可发布，但仍需与 correction warp-group 做一次 mbarrier 握手以保持相位对齐，并由 correction 侧把输出写成 0、LSE 写成 `-inf`：

[block_sparse_utils.py:84-130](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L84-L130) —— 详细注释了空 tile 的 mbarrier 契约。

[block_sparse_utils.py:740-855](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L740-L855) —— `handle_block_sparse_empty_tile_correction_sm100`：空 tile 时种入「全掩码行」统计（`row_sum=1`、`row_max=-inf`），并以 `scale=0` 跑 correction epilogue，把输出写成 0。

**SplitKV 配合**：块稀疏还能与 SplitKV 共存——每个 split 只处理自己分到的那段块列表：

[block_sparse_utils.py:554-560](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L554-L560) —— `split_block_range`：把 `[0, block_count)` 等分成 `num_splits` 段，返回本 split 的半开区间 `[block_begin, block_end)`。

**反向（转置索引）**：

[block_sparse_utils.py:1004-1018](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L1004-L1018) —— `get_total_q_block_count_bwd`：反向按 `n_block` 取 `q_block_cnt[b,h,n]`，得到这个 KV tile 要处理多少个 Q tile（再乘 `q_subtile_factor`）。

[interface.py:1664-1684](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1664-L1684) —— 反向归一化：确定性模式下强制要求 `dq_write_order`（及 full 对应的 `dq_write_order_full`）与 `spt`，缺则抛 `ValueError`。

[interface.py:1373](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1373) —— SM120（arch//10==12）显式拒绝块稀疏反向：`assert not (block_sparse_tensors is not None), "Block sparsity backward not supported on SM 12.0"`。

#### 4.4.4 代码实践

**目标**：跑通「构建块稀疏 → 喂给前向 → 与稠密掩码参考对比」的完整链路。

**步骤**（需要 SM90+ 的 CUDA 设备）：

```python
# 示例代码：带状掩码（保留对角线附近 window 个 token）的块稀疏前向
import torch, math
from flash_attn.cute import flash_attn_func
from flash_attn.cute.compute_block_sparsity import compute_block_sparsity
import cutlass.cute as cute

B, Hq, Hkv, D, Sq, Sk = 1, 8, 2, 64, 512, 512
tile_m = tile_n = 128
window = 64  # 块粒度的「带半宽」

q = torch.randn(B, Sq, Hq,  D, device="cuda", dtype=torch.float16)
k = torch.randn(B, Sk, Hkv, D, device="cuda", dtype=torch.float16)
v = torch.randn(B, Sk, Hkv, D, device="cuda", dtype=torch.float16)

@cute.jit
def band_mask(b, h, q_idx, kv_idx, sl, aux):
    return (q_idx - kv_idx) <= window   # 只看「不远在未来」的 kv（示例）

# 1) 从 mask_mod 构建 ordered 块稀疏四件套
sparse = compute_block_sparsity(
    tile_m=tile_m, tile_n=tile_n, batch_size=B, num_heads=Hq,
    seqlen_q=Sq, seqlen_k=Sk, mask_mod=band_mask,
    aux_tensors=None, device="cuda",
)

# 2) 同时传 mask_mod 和 block_sparse_tensors：partial 块套元素级掩码，full/skip 块加速
out_sparse, lse_sparse = flash_attn_func(
    q, k, v, mask_mod=band_mask, block_sparse_tensors=sparse, return_lse=True,
)

# 3) 稠密参考：对每个 token 对调 mask_mod，做 fp32 softmax(QK^T scale + mask) V
def dense_ref(q, k, v, mask_mod_fn, scale):
    # q,k,v: [B,S,H,D] -> 简化到单头对比
    ...  # 见下方说明
# 参考实现需手动展开 mask_mod 到 [B,H,Sq,Sk] 的 -inf 掩码后做标准注意力
```

**需要观察的现象**：

- `out_sparse` 与稠密参考的最大误差应在 fp16 舍入量级（块稀疏只改实现不改数学）。
- 把同一份输入再用 `flash_attn_func(q,k,v, mask_mod=band_mask)`（不传 `block_sparse_tensors`）跑一遍，输出应与 `out_sparse` 几乎一致——块稀疏相对它只是更快。

**预期结果**：两条路径输出最大误差在 `1e-2 ~ 1e-3` 量级（fp16）。具体数值与是否真正提速「待本地验证」（提速程度取决于掩码稀疏度与硬件）。

> 提示：完整可运行的稠密参考实现可参考 `tests/cute/testing.py` 里的 `attention_ref`，以及 `tests/cute/test_mask_mod.py` 里用 `flex_attention` 做参考的写法（[test_mask_mod.py:120-129](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_mask_mod.py#L120-L129)）。

#### 4.4.5 小练习与答案

**练习 1**：为什么传 `block_sparse_tensors` 时通常还要同时传 `mask_mod`？只传 `block_sparse_tensors` 行不行？

> **答案**：因为 partial 块内部仍需元素级掩码——块稀疏只知道「这块有掩有非掩」，并不知道块内具体哪些 token 该掩。`mask_mod` 正是提供「块内逐元素可见性」的回调。如果某个 mask 全是 full/skip 块（如纯因果且 tile 恰好整除），理论上可以不传 `mask_mod`；但只要存在 partial 块，就必须传 `mask_mod` 才能正确处理边界。两者协作：块稀疏负责「块级跳过/免掩码」，`mask_mod` 负责「partial 块内元素级掩码」。

**练习 2**：`q_subtile_factor` 是什么？为什么反向计算 `m_block` 时要除以它？

> **答案**：`q_subtile_factor = sparse_block_size_q // (q_stage * tile_m)`，表示「一个稀疏块覆盖了几个 kernel 流水子 tile」。SM100 前向 `q_stage=2`（一次载入 2 个 Q 子 tile），若稀疏块大小 `block_size_q = 2*tile_m`，则 `q_subtile_factor=1`；若 `block_size_q = 4*tile_m`，则 `q_subtile_factor=2`，即一个稀疏块对应 2 个 kernel 子 tile。kernel 里用 `sparse_tensor_m_block`（[block_sparse_utils.py:206-218](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparse_utils.py#L206-L218)）把逻辑 `m_block` 除以 `q_subtile_factor`（和 `qhead_per_kvhead`），才能定位到稀疏张量正确的行。

## 5. 综合实践

把本讲四个模块串起来，完成一个小任务：**用块稀疏实现一个「文档级」注意力并验证其正确性与加速**。

1. **定义掩码**：写一个 `mask_mod`，模拟「文档掩码」——同一个文档内的 token 互相可见，跨文档不可见（可借用 `tests/cute/mask_mod_definitions.py` 里的 `document_mask` 思路，或用 `aux_tensors` 传文档 id）。
2. **构建稀疏**：用 `compute_block_sparsity(...)` 或 `flex_attention.create_block_mask(...)` 生成 `BlockSparseTensorsTorch`，打印几个 `m` 行的 `mask_block_cnt` / `full_block_cnt`，确认文档边界处确实出现 partial 块、文档内部是 full 块、文档外是 skip 块。
3. **跑前向**：用 `flash_attn_func(q,k,v, mask_mod=..., block_sparse_tensors=...)` 跑一次，记录耗时。
4. **对比基线**：用相同输入跑两次基线——(a) 只传 `mask_mod` 不传块稀疏；(b) `flex_attention` 参考实现。比较三者的输出最大误差与耗时。
5. **写出结论**：块稀疏输出应与两个基线在 fp16 误差内一致；当文档数多、掩码稀疏度高时，块稀疏应明显快于「纯 mask_mod」基线。

进阶（可选）：若你在 SM100 上且有反向需求，用 `compute_dq_write_order_from_block_mask` 生成 `dq_write_order`，构造 `block_sparse_tensors_bwd`，验证反向梯度也一致（参考 `tests/cute/test_mask_mod.py` 的 `_build_block_sparse_masks_for_bwd`）。

> 本任务需要在 SM90 及以上的 CUDA 设备上运行；若手头只有 Ampere，前向块稀疏路径仍可尝试，但反向受限。数值与加速比「待本地验证」。

## 6. 本讲小结

- 块稀疏是**加速手段而非新算法**：在块粒度上把每个块分成 full（全可见，免掩码）/ partial（半掩，套元素级 `mask_mod`）/ skip（全掩，不加载），跳过大段无效工作，结果与逐元素掩码数学等价。
- FA4 用 **ordered 稀疏表示**（`cnt + 左对齐 idx`，类 CSR）描述结构：`BlockSparseTensors`（kernel 侧）/ `BlockSparseTensorsTorch`（宿主侧），支持定长 `[B,H,M]` 与变长 `[H,total_M]` 两种排布，且允许紧凑 `idx` 以避免 \(O(N^2)\) 显存。
- 三态分类由 `BlockSparsityKernel` 完成（`compute_block_sparsity.py`），ordered↔dense 互转由 `_ordered_to_dense_simple` 提供；构建 ordered 有「FA4 原生 `compute_block_sparsity`」和「PyTorch `create_block_mask` + `as_tuple`」两条路。
- 接入公共 API 时，`interface.py` 做**归一化与校验**（`normalize_block_sparse_config`，推断 `q_subtile_factor`）、把启用标志/广播模式/变长标志写进 `compile_key`、再转 cute 张量喂 kernel。
- kernel 内由 `get_curr_blocksparse_tensors` 取本 tile 四件套，`produce_block_sparse_loads` / `consume_block_sparse_loads` 只加载并计算有效块；空 tile（全 skip）在 SM100 上走专门的 mbarrier 握手与零输出路径；反向用转置索引，确定性模式还需 `dq_write_order` 与 `spt`。
- 块稀疏可与 SplitKV（`split_block_range` 切块列表）、pack_gqa（要求 head 维广播）共存，但 SM120 不支持块稀疏反向。

## 7. 下一步学习建议

- **继续稀疏主题**：阅读 [u10-l3 Top-k KV Gather 稀疏](u10-l3-topk-gather-kv.md)，对比「静态块稀疏」（本讲，掩码预知）与「动态 top-k 选择」（运行时按打分挑块）的异同。
- **深入反向确定性**：若你关心反向数值确定性，精读 `compute_dq_write_order`（[block_sparsity.py:80-148](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_sparsity.py#L80-L148)）与 `block_sparse_utils.py` 末尾的 SM90/SM100 反向 producer/consumer，理解锁值如何保证无死锁的 dQ 累加。
- **结合 Blackwell kernel**：阅读 `flash_fwd_sm100.py` 里 `produce_block_sparse_loads_sm100` / `softmax_block_sparse_sm100` 的调用点，看块稀疏如何嵌入 persistent kernel 与 UMMA 流水。
- **跑测试**：`pytest tests/cute/test_block_sparsity.py`（分类正确性）与 `pytest tests/cute/test_mask_mod.py -k block_sparse`（端到端正确性）是检验理解的最佳参照。
