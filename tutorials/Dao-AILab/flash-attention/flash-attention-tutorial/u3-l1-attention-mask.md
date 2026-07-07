# AttentionMask：因果/滑窗/块稀疏掩码

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 FA4 把「因果 / 滑动窗口 / 块稀疏 / 用户自定义」这四类截然不同的掩码需求，统一抽象成什么数据结构（`AttentionMask`）。
- 理解掩码在 kernel 内部发生在**哪个时机**、对**哪个张量**生效，以及为什么掩码就是把无效位置的注意力分数 `S` 改写成 `-inf`。
- 独立写出一个 `mask_mod`（带正确签名的 `@cute.jit` 回调），把它注入 `flash_attn_func`，并能预测它会触发重新编译。
- 看懂 **R2P（register-to-predicate）32 列位图**掩码机制：为什么 FA4 宁可用一段看似啰嗦的位运算循环，也不直接写 `if col >= limit`。
- 区分 **块级（block-level）跳过** 与 **元素级（element-level）掩码** 两个层次，理解 `BlockInfo` 和 `AttentionMask` 各自负责哪一层。

## 2. 前置知识

在进入源码前，先用三段话把直觉建立起来。

**（a）注意力里「掩码」到底在掩什么。** 标准注意力是 `softmax(QKᵀ/√d) V`。中间矩阵 `S = QKᵀ/√d` 的形状是 `(seqlen_q, seqlen_k)`，每个元素 `S[q, k]` 表示「第 q 个 query 对第 k 个 key 的关注分」。但很多时候某些 `(q, k)` 对是**不该参与**的：因果模型里 query 不能看到未来的 key；滑窗模型里只能看到附近一段；文档级掩码里不同文档之间互不可见。掩码的做法不是删掉这些位置，而是把它们的分数设成 `-inf`，这样 `softmax` 之后这些位置的权重就精确地变成 `0`，等价于「没参与」。所以**掩码 = 在 softmax 之前把非法位置的 `S` 改成 `-inf`**。

**（b）分块下的两层掩码。** FA4 把 `S` 切成 `tile_m × tile_n` 的小块逐块计算（回顾 [u1-l1] 的 tiling 思想）。这就产生了两个优化层次：

- **块级跳过**：如果一个 `tile_n` 块**整块**都被掩掉（比如因果掩码下，对很靠后的 query 而言，靠前的 key 块虽然要看，但靠后的 key 块整块都超出了因果边界），那连读这块 K/V 都没必要——直接不进主循环。这是 [`BlockInfo`](#4-核心概念与源码讲解) 干的事。
- **元素级掩码**：对那些「部分有效、部分无效」的边界块，需要在算完 `S` 之后逐元素把无效位置改成 `-inf`。这是 [`AttentionMask.apply_mask`](#4-核心概念与源码讲解) 干的事。

两层配合：块级跳过负责省掉整块的访存与计算，元素级掩码只处理「边界」那一两块。

**（c）为什么掩码是个性能敏感操作。** `S` 的一个 tile 常常是 `128×128` 或 `128×64`，每个线程手里拿着一小撮元素。如果对每个元素都写一句 `if col >= limit: S = -inf`，会产生大量**发散的分支（warp divergence）**。FA4 的关键技巧是 R2P：把一整行 32 个列的「保留/丢弃」编码成一个 `uint32` 位图（bit=1 保留，bit=0 丢弃），再用一条 PTX 指令一次性把它们压进**谓词寄存器（predicate register）**，从而用向量化的「条件写」替代标量分支。这是本讲最值得理解的一处工程细节。

> 本讲会反复出现的术语：`S`（注意力分数 tile）、`tile_m/tile_n`（Q/KV 的分块尺寸）、`m_block/n_block`（当前处理的是第几个 Q/KV 块）、SSA（CuTeDSL 里把标量包成一维张量参与编译的中间表示）、`-inf`（屏蔽标记）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [flash_attn/cute/mask.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py) | 本讲主角。定义 `AttentionMask` 数据结构与 `apply_mask` / `apply_mask_sm100` 等掩码方法，以及 R2P 位图原语 `r2p_bitmask_below` / `mask_r2p_lambda`。 |
| [flash_attn/cute/block_info.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py) | 定义 `BlockInfo`，负责块级范围计算（`get_n_block_min_max` 等），即「这个 m_block 要遍历哪几个 n_block」。 |
| [flash_attn/cute/utils.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py) | 提供 `AuxData`（穿给 mask_mod 的辅助数据容器）、`scalar_to_ssa`、`shr_u32`/`shl_u32` 等 R2P 依赖的内联 PTX 工具。 |
| [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py) | 公共 API 入口。`_resolve_causal_local_window` 把 `causal / window_size / mask_mod` 归一化；`mask_mod` 在这里被装进 kernel 并进入 `compile_key`。 |
| [flash_attn/cute/flash_fwd.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py) | 前向 kernel。在这里能看到 `AttentionMask(...)` 被构造、`apply_mask` 被穿插进主循环的哪一个阶段。 |
| [tests/cute/mask_mod_definitions.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/mask_mod_definitions.py) | 一组现成的 `mask_mod` 范例（因果、文档、prefix-LM、滑窗等），是写自定义 mask_mod 最好的模板。 |

## 4. 核心概念与源码讲解

### 4.1 AttentionMask 抽象：把四种掩码塞进一个类

#### 4.1.1 概念说明

`AttentionMask` 要解决的问题是：**因果、滑动窗口、块稀疏、用户自定义 `mask_mod`，这四种语义上差别很大的掩码，能不能用同一套数据结构和同一个入口方法来处理？** FA4 的回答是「能」。它用一个 `@dataclass(frozen=True)` 把所有掩码需要的上下文（tile 尺寸、序列长度、窗口左右边界、是否 GQA 打包、是否转置）打包在一起，再用**一个** `apply_mask` 方法，靠 `const_expr` 在**编译期**把无关分支裁掉，特化出针对当前配置的精简代码。

这里有一个贯穿全讲的关键设计：`mask_causal`、`mask_local`、`mask_mod is None` 这些判断都包在 `const_expr(...)` 里。这意味着它们不是运行时 `if`，而是**编译期分支**——每一种组合都会编译出一份专属的、没有多余代码的 kernel。这也是为什么 `mask_mod` 改变会触发重新编译（详见 4.2.3）。

#### 4.1.2 核心流程

`AttentionMask.apply_mask` 在前向主循环里、**每次算完一个 tile 的 `S = QKᵀ` 之后、online softmax 把它累加之前**被调用。它的任务只有一句话：**把这个 tile 里所有无效位置的 `S` 改成 `-inf`**。决策树是：

```
apply_mask(acc_S, m_block, n_block, mask_causal, mask_local, mask_mod, ...)
├── 若 mask_causal 且 mask_local → assert 失败（二者互斥）
├── 分支 A：都不是，且无 mask_mod
│     └── 只需 seqlen 掩码：把 K 序列末尾 padding 的列置 -inf
├── 分支 B：都不是，但 mask_mod 不为 None   ← FlexAttention 风格
│     └── 对每个 (q, kv) 调一次 mask_mod，False → -inf
└── 分支 C：mask_causal 或 mask_local
      └── 按行算出「右边界 col_limit_right」（+局部再算左边界 col_limit_left），逐行掩
```

注意分支 A/B/C 是三选一，由 `mask_mod is None` 与两个布尔标志共同决定，编译期裁剪。

#### 4.1.3 源码精读

先看数据结构本身，字段都很朴素：

[flash_attn/cute/mask.py:158-166](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L158-L166) —— `AttentionMask` 把 tile 尺寸、序列长度信息、窗口边界、GQA 打包因子、转置标志全收在一个不可变 dataclass 里。`window_size_left/right` 为 `None` 表示「该侧不约束」，`swap_AB=True` 用于反向（K@Qᵀ）时行列含义互换。

接着看 `apply_mask` 是怎么把「逻辑坐标」还原出来的。一个 tile 内部，每个线程拿到 `S` 的一小片，但要掩码就得知道这片元素**全局**对应哪个 `(q_idx, kv_idx)`：

[flash_attn/cute/mask.py:193-210](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L193-L210) —— 这段构造一个「单位张量」`cS`（值等于自己的坐标），用 `thr_mma.partition_C(cS)` 把坐标按 MMA 的线程划分切分，于是每个线程能查到自己手里那片 `S` 对应的 `(row, col)`。`seqlenk_col_limit = seqlen_k - n_block * tile_n - thr_col_offset` 给出「当前块里 K 还剩多少有效列」，用于末尾 padding 掩码。

然后是**因果分支**（分支 C 的核心），这是初学者最该读懂的一段：

[flash_attn/cute/mask.py:300-330](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L300-L330) —— 重点看 `causal_row_offset = 1 + seqlen_k - n_block * tile_n - seqlen_q - thr_col_offset` 与 `col_limit_right = row_idx + causal_row_offset`。展开后等价于「当 `kv_idx > q_idx + (seqlen_k - seqlen_q)` 时掩掉」，也就是 **FA 的因果掩码把 Q 的末尾与 K 的末尾对齐**（end-aligned，与测试参考实现一致，见后文 `Sm100FusedMask.apply_mask` 的注释）。`r2p=True` 时走 R2P 快路径（4.3 详述），`r2p=False` 时退化成显式逐列 `for c` 循环——保留这条慢路径主要是为了对照与调试。

> 行号说明：上面引用的是非转置（`not swap_AB`）路径，最贴近前向主循环。转置路径（`swap_AB`，行 377-424）用于反向，逻辑对偶，本讲不展开。

最后，整棵决策树最顶上的互斥断言别忽略：

[flash_attn/cute/mask.py:192](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L192) —— `assert not (mask_causal and mask_local)`：FA4 把「纯因果」和「带窗口的局部」设计成两条互斥特化路径，不允许同时为真。`_resolve_causal_local_window`（见 4.2.3）在 Python 层就保证了这一点。

#### 4.1.4 代码实践（源码阅读型）

**目标**：验证你对「end-aligned 因果」的理解。

1. 打开 [flash_attn/cute/mask.py:300-302](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L300-L302)。
2. 设 `seqlen_q = 4`，`seqlen_k = 10`，`n_block = 0`，`tile_n = 8`，`thr_col_offset = 0`，手算 `causal_row_offset`。
3. 对 `row_idx = 0..3`（即 q_idx = 0..3），用 `col_limit_right = row_idx + causal_row_offset` 算出每个 query 能看到的最右列（kv_idx 上界）。
4. 预期结果：因为 `seqlen_k - seqlen_q = 6`，所以 `q_idx=0` 能看到 `kv_idx ≤ 6`，`q_idx=3` 能看到 `kv_idx ≤ 9`，即 **Q 的末尾对齐 K 的末尾**。如果你过去用过「左对齐」的因果掩码（`kv_idx ≤ q_idx`），这里会是个反直觉点——请把这个差异记在笔记里。
5. 「待本地验证」：你可以在一张纸上画出这个 `(4, 10)` 的因果掩码矩阵（保留=1，掩掉=0），确认它是右下对齐的下三角。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AttentionMask` 要做成 `frozen=True`（不可变）？

> 参考答案：因为它在 `@cute.jit` kernel 里被当作常量上下文使用，字段（尤其 `tile_m/tile_n`、`qhead_per_kvhead_packgqa`）会进入 `const_expr` 分支并参与编译期特化。可变实例容易在运行中被改动导致已编译 kernel 与实际语义不符，冻结它能在源码层杜绝这类误用。

**练习 2**：分支 A（无 causal/local/mask_mod）里，如果 `mask_seqlen=False`，`apply_mask` 实际上什么都不做。为什么还需要这条分支？

> 参考答案：为了在**编译期**把整段掩码代码裁掉。当 `BlockInfo` 已经保证当前 n_block 完全在有效范围内（无需末尾 padding 掩码）时，前向主循环会用 `mask_seqlen=False` 调用 `apply_mask`（见 4.2.3 的主循环片段）。`const_expr(not mask_causal and not mask_local and mask_mod is None)` 配合 `const_expr(mask_seqlen)` 为假，整条分支编译后是空操作，零开销。

---

### 4.2 mask_mod 用户回调：把任意掩码逻辑编译进 kernel

#### 4.2.1 概念说明

`mask_mod` 是 FA4 借鉴 FlexAttention 的设计：**用户写一个普通 Python 函数（用 `@cute.jit` 装饰），描述「给定 `(batch, head, q_idx, kv_idx)`，这个位置该不该保留」，FA4 在编译期把这个函数**内联**进 kernel。** 这比「外部传一个大掩码矩阵」高效得多——掩码矩阵是 `O(N²)` 显存，正是 FlashAttention 要消灭的东西；而 `mask_mod` 只携带「规则」，不携带「数据」（除非你显式用 `aux_tensors`）。

`mask_mod` 解决的问题：因果、滑窗这些「规则型」掩码可以用 `causal=True`/`window_size` 高效表达，但**文档分割、prefix-LM、膨胀滑窗、按 token 查表的 IMA 掩码**等任意结构，靠那几个布尔标志根本描述不了。`mask_mod` 给了一个万能逃生口。

#### 4.2.2 核心流程

一个 `mask_mod` 必须遵守固定签名（参数顺序固定，名字可自定义）：

```
mask_mod(batch, head, q_idx, kv_idx, seqlen_info, aux_tensors) -> Boolean
# 若还需要运行期标量捕获，再加第 7 个参数 aux_scalars（由 call_mask_mod 兼容垫片处理）
```

调用链与注入时机：

```
flash_attn_func(..., mask_mod=fn, aux_tensors=[...])
   └─ _flash_attn_fwd: 把 mask_mod 作为 cutlass.Constexpr 存进 kernel
        └─ 前向主循环算完一个 tile 的 S
             └─ AttentionMask.apply_mask(..., mask_mod=fn)
                  └─ 对该 tile 的每个 (q_idx, kv_idx)：
                       call_mask_mod(fn, batch, head, q, kv, seqlen_info, aux_data)
                       返回 False 的位置 → acc_S = -inf
```

关键点：`mask_mod` 对每个 `(q, kv)` 被**逐元素**调用一次（在分支 B）。这在 SM80/SM90 路径上是标量调用；在 SM100 上 FA4 提供了**向量化** `mask_mod`（一次处理 `vec_size` 个 kv，返回位打包的 `uint32`），把开销摊薄（见 [u8] Blackwell 讲义，本讲只点到为止）。

#### 4.2.3 源码精读

**（1）签名与兼容垫片。** `call_mask_mod` 是一个薄垫片，兼容「有/无 `aux_scalars`」两种历史签名：

[flash_attn/cute/mask.py:22-50](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L22-L50) —— 当 `aux_data.scalars is not None` 时多传一个参数给 `mask_mod`，否则按 6 参数调用。`aux_data` 是 `AuxData(tensors, scalars)`（[flash_attn/cute/utils.py:25-27](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L25-L27)），让 mask_mod 既能读辅助张量（如文档 ID 表），也能用运行期标量。

**（2）分支 B：逐元素调用 mask_mod。** 这是 `mask_mod` 真正被消费的地方：

[flash_attn/cute/mask.py:238-283](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L238-L283) —— 对 tile 里每个 `(r, col)`：先把线程局部坐标换成全局 `global_row_idx`/`global_col_idx`（行 241、255）；若开启了 PackGQA，把「打包进 seqlen 的头」还原回真正的 `(head_idx, q_idx)`（行 244-247）；再把标量包成 SSA（行 260-263）调 `call_mask_mod`；返回 `False` 的位置写 `-inf`（行 283）。注意 `mask_seqlen=True` 时还会额外把超出 `seqlen_q/seqlen_k` 的位置掩掉（行 274-279），这是 padding 保护。

**（3）`mask_mod` 的两个范例。** 看测试里的现成模板，最容易上手：

[tests/cute/mask_mod_definitions.py:24-36](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/mask_mod_definitions.py#L24-L36) —— `cute_causal_mask`：`offset = seqlen_k - seqlen_q`，返回 `n_idx <= m_idx + offset`。注意 `seqlen_info.seqlen_k` 是运行期 `Int32`，需要 `utils.scalar_to_ssa` 包一层再和 SSA 的 `n_idx/m_idx` 比较。这正是「end-aligned 因果」的 `mask_mod` 写法，与 4.1 里 `apply_mask` 内置因果分支的语义**完全一致**——内置路径只是把这条规则用 R2P 高速化了。

[tests/cute/mask_mod_definitions.py:146-159](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/mask_mod_definitions.py#L146-L159) —— `cute_document_mask`：从 `aux_tensors[0]` 里按 `(batch, head, idx)` 查文档 ID，两边 ID 相等才保留。这展示了 `mask_mod` + `aux_tensors` 的典型用法：掩码规则依赖一张外部查表。

**（4）Python 层的归一化与编译键。** 最后看 `mask_mod` 如何从公共 API 流到 kernel，以及它为何触发重编译：

[flash_attn/cute/interface.py:275-295](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L275-L295) —— `_resolve_causal_local_window`：**一旦传了 `mask_mod`，立即返回 `(False, False, ...)`**（行 280-281），即把 `causal`/`local` 都关掉。这是 FA4 的一条硬规则：`mask_mod` 与 `causal=True`/`window_size` **互斥**——`mask_mod` 完全接管掩码语义，你要因果就在 `mask_mod` 里自己写 `n <= m + offset`。

[flash_attn/cute/interface.py:614](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L614) —— `mask_mod_hash = utils.hash_callable(mask_mod)`：mask_mod 的可调用对象被哈希后**进入 `compile_key`**（回顾 [u2-l1] 的 compile_key 概念）。这意味着换一个 `mask_mod`（哪怕只改了里面一个常数）就会得到不同的哈希，从而**重新编译**一份新 kernel。这是 `mask_mod` 是「编译期内联」的直接证据。

**（5）主循环里的调用点。** 在前向 kernel 中确认掩码发生在 softmax 之前：

[flash_attn/cute/flash_fwd.py:999-1028](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L999-L1028) —— `AttentionMask(...)` 被构造，`mask_fn = partial(mask.apply_mask, ...)`，然后用 `mask_fn=partial(mask_fn, mask_mod=self.mask_mod, mask_seqlen=True)` 传进第一个 n_block 的计算。第一个块和因果/局部边界块用 `mask_seqlen=True`（要做末尾 padding 掩码），中间完全有效的块用 `mask_seqlen=False`（行 1053）以省掉掩码开销。

#### 4.2.4 代码实践（编写型，本讲核心实践）

**目标**：参考 `cute_causal_mask`，自定义一个 `mask_mod`，实现「每个 query 只能关注前一半的 key」，注入 `flash_attn_func` 并与 PyTorch 参考对比。

**操作步骤**（示例代码，本机无可用的 Blackwell/Hopper 时不一定能跑通，关键参数见下方「待本地验证」）：

```python
# 示例代码：自定义 mask_mod —— 只保留前一半 key
import torch
import cutlass
import cutlass.cute as cute
from flash_attn.cute import utils
from flash_attn.cute import flash_attn_func

@cute.jit
def first_half_key_mask(
    batch, head, m_idx, n_idx, seqlen_info, aux_tensors,
):  # 签名必须固定为这 6 个参数（顺序如此）
    # 只保留 n_idx <= seqlen_k // 2 的 key（前一半）
    half = seqlen_info.seqlen_k // 2
    half_ssa = utils.scalar_to_ssa(half, cutlass.Int32)
    return n_idx <= half_ssa   # 返回 Boolean: True=保留, False=掩成 -inf

# 构造输入: (batch, seqlen, num_heads, head_dim), 最后一维连续
torch.manual_seed(0)
b, sq, sk, h, d = 1, 256, 256, 4, 64
q = torch.randn(b, sq, h, d, dtype=torch.float16, device="cuda")
k = torch.randn(b, sk, h, d, dtype=torch.float16, device="cuda")
v = torch.randn(b, sk, h, d, dtype=torch.float16, device="cuda")

# 注入 mask_mod（注意: 不能再传 causal=True，二者互斥）
out_cute, lse_cute = flash_attn_func(q, k, v, mask_mod=first_half_key_mask)

# ---- PyTorch 参考实现 ----
def ref_first_half_key(q, k, v):
    qt, kt, vt = q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2)  # (b,h,sq,d)
    scores = torch.matmul(qt.float(), kt.float().transpose(-1, -1)) / (d ** 0.5)
    sk = scores.shape[-1]
    kv_idx = torch.arange(sk, device=scores.device)
    keep = kv_idx <= (sk // 2)            # (sk,) 与 query 无关
    scores = scores.masked_fill(~keep, float("-inf"))
    out = torch.softmax(scores, dim=-1) @ vt.float()
    return out.half().transpose(1, 2)

out_ref = ref_first_half_key(q, k, v)
print("max abs diff:", (out_cute - out_ref).abs().max().item())
```

**需要观察的现象**：

1. 首次调用会触发 JIT 编译，耗时明显较长（数十秒级）；第二次同样输入应命中缓存秒回（回顾 [u1-l3]）。
2. `out_cute` 与 `out_ref` 的最大误差应在 fp16 数量级（`1e-2 ~ 1e-1`），因为两者都是精确注意力，差异只来自 fp16 舍入。
3. 若误把 `mask_mod=first_half_key_mask` 与 `causal=True` 同时传入，会在 `_resolve_causal_local_window` 处把 causal 静默关掉（mask_mod 接管），输出仍以 mask_mod 为准——这是「互斥」的体现，不是报错。

**预期结果 / 待本地验证**：本讲义编写环境无可用 GPU，以上输出数值未实际运行，属「待本地验证」。请在 Hopper/Blackwell 机器上跑通后记录真实的 max abs diff。一个可确定的预期是：`lse_cute` 里被掩掉的 query 行（如果存在整行被掩的情况）会出现 `-inf`；本例中每个 query 都至少能看到前一半 key，故不会出现整行 `-inf`。

#### 4.2.5 小练习与答案

**练习 1**：把上面的 `first_half_key_mask` 改成「每个 query 只能看到自己**之前**的 key（标准左对齐因果）」，该怎么写？

> 参考答案：`return n_idx <= m_idx;`（注意是左对齐，没有 `offset`）。FA4 内置的 `causal=True` 是 end-aligned（`n_idx <= m_idx + (seqlen_k - seqlen_q)`），与「左对齐因果」**不同**——当 `seqlen_q != seqlen_k` 时差异明显。要左对齐因果，且 `seqlen_q != seqlen_k`，就得用 `mask_mod` 自己写。

**练习 2**：为什么 `mask_mod` 里要把 `seqlen_info.seqlen_k // 2` 用 `utils.scalar_to_ssa` 包一层，而不能直接和 `n_idx` 比较？

> 参考答案：`n_idx` 是 `cute.TensorSSA`（编译期 SSA 值），而 `seqlen_info.seqlen_k` 是运行期 `Int32` 标量。CuTeDSL 要求参与编译表达式构造的运算数同属 SSA 表示，`scalar_to_ssa` 把标量包成一维 SSA 张量（[flash_attn/cute/utils.py:931-935](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L931-L935)），两者才能生成合法的比较算子。

---

### 4.3 R2P 位图掩码：用一条指令掩掉 32 列

#### 4.3.1 概念说明

R2P 是本讲最「硬件味」的一节，但直觉很简单。回顾 4.1 的因果分支：对每一行，我们要把「列号 ≥ `col_limit_right`」的元素全置 `-inf`。最朴素的写法是：

```python
for c in range(ncol):
    acc_S[r, c] = -inf if col_idx[c] >= col_limit_right else acc_S[r, c]
```

问题在于：这是一个**每 32 列就重复一次的、有规律的条件写**，编译成标量分支会有可见的额外开销。R2P（register-to-predicate）的思路是：**把这 32 个位置该不该掩，预先编码成一个 32 位整数 `mask`（bit i = 1 保留，bit i = 0 丢弃），然后让编译器把下面的循环模式识别成一条「按谓词批量条件写」的 PTX 指令**：

```python
for i in range(32):
    in_bound = Boolean(mask & (1 << i))
    X[c] = X[c] if in_bound else -inf
```

关键不在代码「短了」，而在这段循环被 `range_constexpr` 完全展开后，编译器能把它**lower 成一条 R2P 指令**（把整数寄存器的内容重组成谓词寄存器），从而一次处理 32 列、没有分支。`mask.py` 里那条注释说得很直白：

> This needs to be `range_constexpr`, o/w the compiler can't generate the R2P instruction.

#### 4.3.2 核心流程

R2P 掩码由三个原语配合完成：

1. **算「右边界位图」`r2p_bitmask_below(limit, s)`**：在第 `s` 个 32 列块里，列号 `< limit` 的位置 bit=1。等价于「保留下三角」。
2. **算「左边界位图」`r2p_bitmask_above(limit, s)`**：列号 `≥ limit` 的位置 bit=1。等价于「保留上三角」。滑动窗口需要 `below & above` 同时取，得到 `[left, right)` 区间。
3. **`mask_r2p_lambda(X, mask_gen_fn)`**：把上面产出的位图按 32 列一块地「应用」到累加器片段 `X` 上，bit=0 的位置写 `-inf`。

位图的数学表达（`s` 是 32 列块的编号，块内列号 `i ∈ [0,31]` 对应全局列 `s*32 + i`）：

\[ \text{below}(\text{limit}, s)_i = \mathbb{1}[s\cdot 32 + i < \text{limit}] \]

\[ \text{above}(\text{limit}, s)_i = \mathbb{1}[s\cdot 32 + i \ge \text{limit}] \]

实现上不逐位判断，而是用一次移位：

- `below`：`m = max((s+1)*32 - limit, 0)`，返回 `0xFFFFFFFF >>u m`（右移 `m` 位，高位补 0，相当于「保留低 `32-m` 位」）。
- `above`：`n = max(limit - s*32, 0)`，返回 `0xFFFFFFFF << n`（左移 `n` 位，低位补 0，相当于「保留高 `32-n` 位」）。

#### 4.3.3 源码精读

**（1）常量与两个位图生成器。**

[flash_attn/cute/mask.py:19](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L19) —— `MASK_R2P_CHUNK_SIZE = 32`：R2P 以 32 列为基本块，正好对应一个 warp 里 32 个线程 / 一个 `uint32` 的位数。

[flash_attn/cute/mask.py:53-72](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L53-L72) —— `r2p_bitmask_below` / `r2p_bitmask_above`。注意它们用 `utils.shr_u32` / `utils.shl_u32`（[flash_attn/cute/utils.py:549](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L549)、[flash_attn/cute/utils.py:583](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L583)）走**内联 PTX** 而非 Python 的 `>>`/`<<`。原因是 LLVM 对「移位宽度等于类型宽度」（如 `x >> 32`）定义为未定义行为，而这里 `m` 恰好可能等于 32（整块全掩）；PTX 的 `shr.u32`/`shl.b32` 规定移位量钳制到类型宽度，是良定义的。

**（2）应用器 `mask_r2p_lambda`。**

[flash_attn/cute/mask.py:75-100](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L75-L100) —— 外层按 32 列块迭代（行 90），每个块向 `mask_gen_fn(s)` 要一个 `uint32` 位图；内层 `range_constexpr` 展开成 32 个固定 i（行 93），逐位测试 `mask & (1 << i)`，bit=0 写 `-inf`（行 100）。`rank1=True` 处理一维片段（单行），否则处理二维片段（多行同掩）。

**（3）SM90 累加器列号非线性，要先换算。** 这是 Hopper 路径独有的「坑」：

[flash_attn/cute/mask.py:103-111](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L103-L111) —— `sm90_col_to_r2p_idx`。SM90 的 MMA 累加器里，物理列排列是 `0, 1, 8, 9, 16, 17, ...`（非连续），所以一个「逻辑列阈值 `col_limit`」必须先换算成「物理元素索引」才能交给 `r2p_bitmask_below`。公式 `col_limit // 8 * 2 + min(col_limit % 8, 2)` 就是这个映射。

**（4）因果分支怎么把三个原语串起来。** 回看 4.1 引用过的因果分支，现在重点看 R2P 那条快路径：

[flash_attn/cute/mask.py:324-330](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L324-L330) —— `col_limit_r2p = sm90_col_to_r2p_idx(col_limit_right)` 先换算列号，再 `mask_r2p_lambda(acc_S_mn[r, None], lambda s: r2p_bitmask_below(col_limit_r2p, s), rank1=True)`，对单行片段做「保留下三角」。

滑动窗口（local）分支则在 `below` 的基础上再 `& above`，得到 `[left, right)`：

[flash_attn/cute/mask.py:367-376](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L367-L376) —— `mask_gen_fn(s) = r2p_bitmask_below(col_limit_right_r2p, s) & r2p_bitmask_above(col_limit_left_r2p, s)`。两个位图按位与，就是「既在右边界内、又在左边界内」的窗口。

**（5）块级跳过：`BlockInfo`。** R2P 解决的是「边界块的元素级掩码」；为了让**整块被掩的 tile 干脆不进主循环**，FA4 用 `BlockInfo.get_n_block_min_max` 算出每个 m_block 真正要遍历的 n_block 范围 `[n_block_min, n_block_max)`：

[flash_attn/cute/block_info.py:23-55](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L23-L55) —— 因果/有右窗时（行 32-38），用 `n_idx_right = m_idx_max + seqlen_k - seqlen_q (+ window_size_right)` 算出右边界，`n_block_max` 收紧到 `ceil(n_idx_right / tile_n)`，跳过右侧整块。有左窗时（行 40-46），`n_block_min` 抬到 `max((n_idx - window_size_left) // tile_n, 0)`，跳过左侧整块。`is_split_kv`（行 47-54）再把范围均分给多个 split（SplitKV 详见 [u7-l2]）。

把两层连起来：**`BlockInfo` 做粗筛（跳过整块），`AttentionMask.apply_mask`（含 R2P）做细修（处理边界块的逐元素掩码）**。比如因果掩码下，前向主循环只会进入 `[n_block_min, n_block_max)` 内的块，而其中只有最靠右那一两块（跨因果边界）需要 `apply_mask` 做元素级掩码，再往左的块 `mask_seqlen=False` 完全跳过掩码。

#### 4.3.4 代码实践（源码阅读型）

**目标**：亲手推一遍 `r2p_bitmask_below`，确认它真的等价于「保留下三角」。

1. 设 `limit = 5`（保留列 0,1,2,3,4），考虑第 0 个 32 列块 `s = 0`。
2. 用 `m = max((0+1)*32 - 5, 0) = 27`，得 `0xFFFFFFFF >>u 27 = 0x0000001F = 0b...0001_1111`。
3. 逐位读：bit0..bit4 = 1（列 0..4 保留），bit5..bit31 = 0（列 5..31 掩掉）—— 正是 `col < 5`。
4. 再验证 `s = 1`（列 32..63）：`m = max(2*32 - 5, 0) = 59`，PTX 中移位量钳制到 32，`0xFFFFFFFF >>u 32(clamped) = 0`，整块全掩——符合「列 32..63 都 ≥ 5」。
5. **预期结果**：`r2p_bitmask_below(5, 0) = 0x1F`，`r2p_bitmask_below(5, 1) = 0x0`。
6. 「待本地验证」：在 Python 里用 `int(0xFFFFFFFF) >> 27` 即可得 `31 = 0x1F`（注意 Python 整数无固定位宽，移位不会钳制，但本例移位量 27<32 所以一致；移位量 ≥32 时 Python 与 PTX 行为不同，这正是 FA4 用内联 PTX 的原因）。

#### 4.3.5 小练习与答案

**练习 1**：滑动窗口 `[left, right)` 用 `below(right) & above(left)` 实现。如果把 `&` 误写成 `|`，掩码行为会变成什么？

> 参考答案：`below(right)` 保留 `col < right`，`above(left)` 保留 `col ≥ left`，按位与才得到区间 `[left, right)`。改成按位或会变成「`col < right` 或 `col ≥ left`」的并集——只要 `right > left`（窗口非空），并集就是全部 32 列全 1，等于**完全不掩**。这是个隐蔽的退化 bug，但不会报错。

**练习 2**：为什么 SM100（Blackwell）路径 `apply_mask_sm100` 里几乎看不到 `sm90_col_to_r2p_idx`，反而有一个 `row_to_r2p_idx`？

> 参考答案：SM100 用 tcgen05/UMMA，累加器存在片上 `tmem`，行/列排列与 SM90 的寄存器累加器不同，且反向里两个 warp-group 共享 tmem、行是交错排布的（[flash_attn/cute/mask.py:114-134](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L114-L134)），所以需要把「行阈值」换算成「元素索引」`row_to_r2p_idx`，而不是 SM90 的列换算。这两套换算函数体现了「同一套 R2P 思想，不同硬件需要不同的坐标映射」。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「阅读 + 实验」综合任务：

**任务**：解释「为什么 `causal=True` 比自定义 `mask_mod`（实现同样因果语义）更快」。

**步骤**：

1. **阅读**：精读 [flash_attn/cute/interface.py:275-295](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L275-L295)（`_resolve_causal_local_window`），确认 `causal=True` 走的是「内置因果特化路径」（`mask_causal=True`），而传 `mask_mod=cute_causal_mask` 走的是「分支 B 逐元素调用」。
2. **对照**：把 [flash_attn/cute/mask.py:303-330](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L303-L330)（内置因果，用 R2P + BlockInfo 块级跳过）与 [flash_attn/cute/mask.py:238-283](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L238-L283)（mask_mod 分支，逐元素 `(r, col)` 调用）并排比较。
3. **分析**：列出内置因果路径相对 mask_mod 路径的三条性能优势。提示——(a) 块级跳过：内置因果让 `BlockInfo` 能用 `is_causal` 算紧的 `n_block_max`（[block_info.py:32-38](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L32-L38)），而 `mask_mod` 时 `is_causal=False`（[interface.py:280-281](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L280-L281)），块级跳过失效，必须遍历所有 n_block 再逐元素掩；(b) R2P vs 标量分支；(c) 谓词批量写 vs 逐元素条件写。
4. **实验（待本地验证）**：在同一组 `fp16 / hdim=64 / seqlen=2048 / causal` 输入上，分别用 `flash_attn_func(q,k,v,causal=True)` 与 `flash_attn_func(q,k,v,mask_mod=cute_causal_mask)` 各跑若干次（warmup 后计时），记录前向耗时，验证内置因果确实更快；同时打印两者输出的最大误差，确认数值一致（差异仅来自 fp16 舍入）。

**产出**：一张表，列出「内置 causal」与「mask_mod 模拟 causal」在「块级跳过是否生效 / 元素级掩码方式 / 是否重编译 / 实测前向耗时 / 与参考最大误差」五个维度上的对比。

## 6. 本讲小结

- **掩码的本质**：在 softmax 之前把非法位置的注意力分数 `S` 改成 `-inf`；FA4 用 `AttentionMask` 这一个数据结构 + 一个 `apply_mask` 方法统一表达因果、滑窗、块稀疏、用户 `mask_mod` 四类语义，靠 `const_expr` 在编译期裁剪出特化代码。
- **两层掩码**：`BlockInfo`（`get_n_block_min_max`）做**块级跳过**（整块被掩的 tile 不进主循环），`AttentionMask.apply_mask` 做**元素级掩码**（处理边界块的逐元素 `-inf`），二者协同。
- **mask_mod**：固定签名 `(batch, head, q_idx, kv_idx, seqlen_info, aux_tensors [, aux_scalars]) -> Boolean` 的 `@cute.jit` 回调；它**编译期内联**进 kernel，因此其哈希进入 `compile_key`，改变即重编译；它与 `causal=True`/`window_size` **互斥**（`_resolve_causal_local_window` 一旦见到 mask_mod 就把 causal/local 关掉）。
- **R2P 位图**：把 32 列的「保留/丢弃」编码成 `uint32` 位图，用 `r2p_bitmask_below`/`r2p_bitmask_above` 经内联 PTX 移位生成，再由 `mask_r2p_lambda` 的 `range_constexpr` 循环 lower 成一条谓词批量写指令；移位用 PTX 而非 Python 是为了规避 LLVM 的「移位等于位宽」UB。
- **因果对齐方式**：FA4 的内置因果是 **end-aligned**（Q 末尾对齐 K 末尾，`kv_idx ≤ q_idx + (seqlen_k − seqlen_q)`），`mask_mod` 里写 `n ≤ m + offset` 与之一致；若要左对齐因果需自行用 `mask_mod` 写 `n ≤ m`。
- **硬件差异**：SM90 累加器列号非线性，需 `sm90_col_to_r2p_idx` 换算；SM100 改用 `row_to_r2p_idx` 并提供向量化 mask_mod（`vec_size > 1` 返回位打包结果），两套换算体现「同一思想、不同坐标映射」。

## 7. 下一步学习建议

- 顺着「块级跳过」这条线，下一讲 [u3-l2 BlockInfo：分块与有效范围计算] 会把 `get_n_block_min_max` / `get_m_block_min_max` 彻底讲透，并手算因果下 `n_block` 的范围——本讲已铺垫，可直接进入。
- 想看 R2P 在更高性能路径上如何被复用，可跳读 [flash_attn/cute/mask.py:614-774](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/mask.py#L614-L774)（`apply_mask_sm100`，Blackwell 路径，含向量化 mask_mod 与 `rBitmask` 直接传入），等学完 [u8] Blackwell 专用 kernel 后回看会更通透。
- `mask_mod` 与 `score_mod` 是孪生设计（一个改 0/1 掩码，一个改分数本身），建议接着学 [u4-l2 score_mod：可编程打分修改]，对照两者的签名、注入机制与编译键处理。
- 块稀疏注意力会以 `block_sparse_tensors` 的形式再叠加一层「块级稀疏」语义，详见 [u10-l1 块稀疏注意力]；届时你会发现它与本讲的 `BlockInfo` 块级跳过是互补关系。
