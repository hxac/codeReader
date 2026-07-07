# Paged KV Cache

## 1. 本讲目标

本讲解决一个在大模型推理/服务（decode、KV cache）中非常现实的问题：**当 K/V 不再是连续张量，而是散落在显存池里的一堆「页（page）」时，FlashAttention 如何照样做精确注意力？**

读完后你应当掌握：

- 分页 KV cache 的**页表（page_table）映射模型**：逻辑 token 位置如何映射到物理页。
- `page_size` 的对齐约束，以及它如何决定两条截然不同的加载路径（TMA 直取 vs `PagedKVManager` 逐行 gather）。
- SM100（Blackwell）上 V 在 gmem 中**转置存储**的原因与实现。
- `PagedKVManager` 的三步加载流程（`load_page_table` → `compute_X_ptr` → `load_KV`），以及它如何复用 u5-l2 讲过的 cp.async 拷贝原子。
- 哪些架构支持分页 KV、哪些被显式拒绝。

本讲只读不改源码，所有命令的运行结果若未在你本机复现，均标注「待本地验证」。

## 2. 前置知识

- **u6-l1 前向主循环**：你已经知道前向是「Q 常驻、K/V 流水」，每个 `n_block` 把一块 K、一块 V 从 gmem 搬进 smem。本讲只改「这一块 K/V 从哪里来、地址怎么算」，主循环其余部分不变。
- **u5-l2 copy_utils 与 TMA / cp.async**：Ampere 走 `cp.async`（128-bit、靠 `commit_group`/`wait_group` 跟踪完成），Hopper+ 走 TMA（`cp.async.bulk`，按字节数经 mbarrier 通知完成）。分页 KV 同样在这两种搬运里二选一。
- **PagedAttention / vLLM 的分页思想**（非必须，但有助于直觉）：操作系统用页表把离散物理页映射成连续虚拟地址；分页 KV cache 借用同一思路，把「逻辑上的第 j 个 token」映射到「物理上的第 `page_table[j // page_size]` 页、页内偏移 `j % page_size`」。
- **gmem/smem/rmem 三级存储**：参见 u6-l1。分页的复杂性全在 gmem→smem 这一段。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [flash_attn/cute/paged_kv.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py) | `PagedKVManager`：分页 KV 的核心数据类，负责页表查询、地址计算与 cp.async 散列搬运。本讲主角。 |
| [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py) | 公共接口与架构分发：校验 `page_table` 形状、按 `page_size vs tile_n` 选 TMA 或非 TMA、在各架构 kernel 构造处传入 `paged_kv_non_tma` 标志。 |
| [flash_attn/cute/flash_fwd_sm100.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py) | Blackwell 前向 kernel：在主循环里 `create` 出 `PagedKVManager`，并在每个 `n_block` 调用 `load_page_table` + `load_KV`。 |
| [flash_attn/cute/flash_fwd_sm90.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm90.py) | Hopper 前向 kernel：同样接 `PagedKVManager`，是非 TMA 分页路径的另一处使用者。 |
| [tests/cute/test_flash_attn.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py) | `_generate_block_kvcache` 是构造分页 KV 的范本，也是本讲综合实践的依据。 |

## 4. 核心概念与源码讲解

### 4.1 页表映射模型：把散落的页看成连续序列

#### 4.1.1 概念说明

在训练里，K/V 是 `(batch, seqlen, num_heads, head_dim)` 的连续张量。但在**推理服务**里，KV cache 是按请求动态增长的：不同请求长度不同、显存碎片化、还要做 prefill/decode 混合。把每个请求的 KV 强行铺成连续大张量会浪费显存、难以扩缩容。

分页 KV cache 的做法是：维护一个**全局页池** `mK_paged` / `mV_paged`，形状是 `(num_pages, page_size, num_heads_kv, head_dim)`——第一维是「物理页号」，每页固定 `page_size` 个 token；再用一张**页表** `page_table`，形状 `(batch, max_num_pages_per_seq)`、`int32`，记录每条序列依次占用了哪些物理页。

于是「第 b 条序列的第 j 个 token」的物理位置是：

\[
\text{page} = \text{page\_table}\big[b,\ \lfloor j / \text{page\_size} \rfloor\big],\qquad
\text{offset} = j \bmod \text{page\_size}
\]

最终在页池里取 `mK_paged[page, offset, head, :]`。这样序列在逻辑上连续、在物理上可以任意散布（甚至多条序列共享/复用页），与 OS 的虚拟内存页表如出一辙。

#### 4.1.2 核心流程

把一条逻辑序列重排成物理页的过程（伪代码）：

```
输入: 逻辑 KV (batch, seqlen, h_kv, d), 页池布局 (num_pages, page_size, h_kv, d)
对每条序列 b:
    num_blocks_b = ceil(seqlen_b / page_size)
    for j in range(num_blocks_b):
        page = 从页池里分配一个空闲物理页号
        page_table[b, j] = page
        把逻辑 KV[b, j*page_size:(j+1)*page_size] 拷进 KV_paged[page]
kernel 内: 对逻辑 token 位置 t:
    page, offset = page_table[b, t // page_size], t % page_size
    从 KV_paged[page, offset] 读
```

kernel 侧只多了一步「`t → (page, offset)`」的查表，但它打破了「一个 tile 对应 gmem 中一段连续地址」的假设，这正是 `PagedKVManager` 要解决的问题。

#### 4.1.3 源码精读

公共接口只暴露在 **`flash_attn_varlen_func`**（变长/分页/打包的超集），标准 `flash_attn_func` **没有** `page_table` 参数：

- [flash_attn/cute/interface.py:2761-L2762](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L2761-L2762) —— `flash_attn_varlen_func`，分页 KV 的对外入口。
- [flash_attn/cute/interface.py:2774-L2801](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L2774-L2801) —— 形参 `page_table` 与文档 `page_table: (batch, max_num_pages_per_seq)`。

进入内部 `_flash_attn_fwd` 后，分页 KV 的形状约束集中在这一段，注意它和普通布局的差异：

[flash_attn/cute/interface.py:364-L383](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L364-L383) —— 当 `page_table is not None` 时：

- **禁用 `cu_seqlens_k`**（`page_table is not supported with cu_seqlens_k`）——分页与 K 侧变长打包互斥；
- `page_table` 必须是 `int32`、最后一维连续；
- `num_pages, page_size = v.shape[:2]`，且 **`seqlen_k = num_pages * page_size`**（见下一条 L370-L371）——逻辑 K 长度由页池规模决定；
- KV 张量形状不再是 `(batch, seqlen_k, ...)`，而是 **`(num_pages, page_size, num_head_kv, head_dim_v)`**（L382-L383）。

页表本身（`mPageTable`）与页池（`mK_paged`/`mV_paged`）被存进 `PagedKVManager` 这个数据类：

[flash_attn/cute/paged_kv.py:16-L43](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L16-L43) —— `PagedKVManager` 的字段：`mPageTable`、`mK_paged`、`mV_paged` 三个 gmem 张量，加上 `page_size_divmod`（用于查表的快速 divmod）、`seqlen_k`、`n_block_size` 等运行期与编译期参数。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在不跑 GPU 的前提下，亲手验证「页表映射」能把散页重排回连续序列。

**步骤**：

1. 打开 [tests/cute/test_flash_attn.py:1806-L1835](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L1806-L1835) 的 `_generate_block_kvcache`。
2. 重点看这两行：
   - `page_table = rearrange(torch.randperm(num_blocks, ...), "(b nblocks) -> b nblocks", b=batch_size)` —— 用一个随机置换把物理页号打乱后分给各序列；
   - `k_cache = rearrange(k_cache_paged[page_table.flatten()], "(b nblocks) block_size ... -> b (nblocks block_size) ...", b=batch_size)[:, :seqlen_k]` —— **这就是「把分页展开成连续」的参考实现**：先按 `page_table` 顺序 gather 物理页，再把 `(nblocks, page_size)` 折叠成连续 seqlen。
3. 手算一个最小例子：`page_size=2`，`page_table=[[3,1,0]]`，页池第 3、1、0 页分别是 `[[a,b],[c,d],[e,f],[g,h]]` 中对应页（注意页号从 0 起）。写出展开后的连续序列。

**预期**：gather 顺序是 page 3 → page 1 → page 0，即 `[g,h,c,d,a,b]`。这正说明 kernel 内部「逐 token 查 `page_table`」与外部「按 `page_table` gather 页」是同一映射。

**运行结果**：纯纸笔，无需 GPU。结论应与上述一致（待本地验证你自己的例子）。

#### 4.1.5 小练习与答案

**Q1**：为什么 `page_table` 必须是 `int32` 且最后一维连续？

**A1**：`int32` 是 kernel 里 `mPageTable[page_idx]` 索引的元素类型（见 4.3 的 `load_page_table`）；最后一维连续（`stride(-1)==1`）保证 `to_cute_tensor(..., leading_dim=1)` 能把它描述成一个可被 TMA/cp.async 高效索引的张量（interface.py:783-L784 以 `assumed_align=4`、`leading_dim=1` 转成 cute tensor）。

**Q2**：`page_table.shape = (batch, max_num_pages_per_seq)`，为何是 `max` 而不是每条序列的真实页数？

**A2**：一个 batch 要张量形状统一，所以按最长序列预留槽位；短序列的尾部页号是无效的，靠 `seqlen_k` 在运行期裁掉（kernel 里 `row_idx < self.seqlen_k` 的谓词，见 4.3）。

---

### 4.2 page_size、对齐与两条加载路径

#### 4.2.1 概念说明

回忆 u6-l1：前向主循环每次处理一个 `n_block_size`（即 `tile_n`）大小的 K/V 块。**非分页**时，这块在 gmem 里是一段连续地址，可以一条 TMA 或一轮 cp.async 搬完。

**分页**时问题来了：这块 `tile_n` 个 token 可能横跨多个物理页、地址完全不连续。能否高效搬运，取决于 `page_size` 与 `tile_n` 的关系——这是本讲最关键的二分：

- **`page_size == tile_n`**（如都是 128）：一个 tile 恰好是一整页，地址在页内连续 → **走 TMA 直取路径**，只需用 `page_table` 查出这一 tile 对应的物理页号 `page_idx`，然后像非分页一样一次 TMA 搬走。
- **`page_size != tile_n`**（如 `page_size=1/4/16/64/256`）：tile 与页边界不对齐，一个 tile 里的各行来自不同页 → **走非 TMA 的 `PagedKVManager` 路径**，逐行查页表、逐行 cp.async。

代码里这个二分由一个布尔标志表达：`paged_kv_non_tma = page_size not in [None, tile_n]`（`None` 表示非分页）。`use_tma_KV = not paged_kv_non_tma`。

#### 4.2.2 核心流程

两条路径的对照：

```
非分页 (page_size is None):
    use_tma_KV = True  (SM90+) 或 cp.async 连续 (SM80)
分页, page_size == tile_n:
    use_tma_KV = True  → 每个 n_block 查一次 page_table[b, n_block] 得 page_idx
                          → TMA 直接搬 gV[page_idx] 整页到 smem
分页, page_size != tile_n:
    use_tma_KV = False → PagedKVManager 路径
                          → load_page_table: 把这 tile 每行的 (page, offset) 算进寄存器
                          → compute_X_ptr:   拼成每行的 gmem 指针
                          → load_KV:          cp.async 逐行散列 gather 到 smem
```

注意 `page_size=1` 是解码（decode）场景的常见取值（每次只追加 1 个 token），它必然落入非 TMA 路径。

#### 4.2.3 源码精读

二分标志进入 kernel 构造参数，也进入 `compile_key`（改 `page_size` 会触发重编译）：

- [flash_attn/cute/interface.py:753](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L753) —— `page_size not in [None, tile_n]` 进 compile_key。
- [flash_attn/cute/interface.py:867](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L867) —— SM90 构造 `FlashAttentionForwardSm90(..., paged_kv_non_tma=page_size not in [None, tile_n])`。
- [flash_attn/cute/interface.py:936](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L936) —— SM100 同理。
- [flash_attn/cute/flash_fwd_sm100.py:144](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L144) —— kernel 内 `self.use_tma_KV = not paged_kv_non_tma`。

**TMA 路径**（page_size == tile_n）：主循环里直接用 `page_table` 查 `page_idx`：

[flash_attn/cute/flash_fwd_sm100.py:1478-L1486](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1478-L1486) —— `page_idx = mPageTable[batch_idx, n_block_first]`（仅当 `use_tma_KV`），随后 `load_K(..., page_idx=page_idx)`。注意 [flash_fwd_sm100.py:3043-L3051](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L3043-L3051) 的 TMA 分支里 `tXgX_cur = tXgX[None, block] if page_idx is None else tXgX[None, 0, page_idx]`——有 `page_idx` 时直接按物理页号索引页池。

**非 TMA 路径**（page_size != tile_n）：改用 `PagedKVManager`：

[flash_attn/cute/flash_fwd_sm100.py:1483-L1486](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1483-L1486) —— 首块加载前 `paged_kv_manager.load_page_table(n_block_first)`；循环体里 [flash_fwd_sm100.py:1505-L1506](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1505-L1506) 每个 `n_block` 都 `load_page_table`。`load_K`/`load_V` 最终落到 [flash_fwd_sm100.py:3052-L3060](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L3052-L3060) 的 else 分支：`paged_kv_manager.load_KV(block, sX_cur, K_or_V)` + `cp_async_commit_group()`。

**架构支持边界**（重要的「不支持」断言）：

- [flash_attn/cute/interface.py:826](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L826) —— `assert page_table is None, "paged KV not supported on SM 8.0"`（Ampere 不支持）。
- [flash_attn/cute/interface.py:945](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L945) —— `assert page_table is None, "Paged KV not supported on SM 12.0 in this PR"`（SM120 本版本不支持）。
- 即：**分页 KV 仅在 SM90（Hopper）与 SM100/SM110（Blackwell 数据中心）前向可用**。
- 额外约束：2CTA 指令要求 `page_size in [None, 128]`（[interface.py:596](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L596)）；SM100 hd256 2CTA 还要求 `max_seqlen_k % page_size == 0` 且 `page_table.shape[1] == max_seqlen_k // page_size`（[interface.py:896-L907](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L896-L907)）。

#### 4.2.4 代码实践（阅读 + 推理型）

**目标**：理解 `page_size` 取值如何切换路径，并能预测一次调用走哪条路。

**步骤**：

1. 假设你在 SM100（Blackwell）上，默认 `tile_n=128`。
2. 对下表每一行，写下 `paged_kv_non_tma` 的值、`use_tma_KV` 的值、走哪条路径：

   | `page_size` | `paged_kv_non_tma` | `use_tma_KV` | 路径 |
   |---|---|---|---|
   | `None`（非分页） | ? | ? | ? |
   | `128` | ? | ? | ? |
   | `1`（解码） | ? | ? | ? |
   | `64` | ? | ? | ? |
   | `256` | ? | ? | ? |

3. 再读 [flash_fwd_sm100.py:1414-L1449](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1414-L1449)，确认 `use_tma_KV` 为 True 时 `paged_kv_manager=None`（走 TMA 分支），为 False 时才 `PagedKVManager.create(...)`。

**预期结果**：`None`→非分页 TMA；`128`→分页 TMA（`page_idx` 直查）；`1/64/256`→`paged_kv_non_tma=True`，走 `PagedKVManager`。`256` 还要注意是否触发 2CTA 的 `page_size in [None,128]` 约束（会关闭 2CTA）。

**运行结果**：纯推理，无需 GPU。

#### 4.2.5 小练习与答案

**Q1**：为什么 `page_size=1` 几乎一定走非 TMA 路径？

**A1**：`tile_n` 通常是 64 或 128，`page_size=1 != tile_n`，所以 `paged_kv_non_tma=True`、`use_tma_KV=False`。`page_size=1` 意味着每个 token 一页，tile 内每个 token 都来自不同页，只能逐行 gather。

**Q2**：把 `page_size` 从 128 改成 64，会触发重编译吗？

**A2**：会。`page_size not in [None, tile_n]` 是 compile_key 的一部分（interface.py:753）；128 时该布尔值为 False、64 时为 True，编译出的 kernel 不同（一个用 TMA 路径、一个用 `PagedKVManager`），故重编译。

---

### 4.3 PagedKVManager 三步加载：load_page_table → compute_X_ptr → load_KV

#### 4.3.1 概念说明

非 TMA 路径下，`PagedKVManager` 要完成一件麻烦事：把一个 `n_block_size` 大小的 tile（其各行散落在不同物理页）**scatter-gather** 进 smem 里一段连续区域。它分三步：

1. **`load_page_table(n_block)`**：把这个 tile 里每个 token 行的 `(page, offset)` 查出来，存进寄存器 `tPrPage` / `tPrPageOffset`。
2. **`compute_X_ptr(K_or_V)`**：把 `(page, offset)` 加上头维偏移 `d_offset`，拼成每行的 gmem 元素指针 `tPrXPtr`。
3. **`load_KV(n_block, sX, K_or_V)`**：用 cp.async 把这些指针指向的数据搬进 smem 的对应行。

这套机制复用了 u5-l2 讲过的 cp.async 拷贝原子（`CopyG2SOp`、128-bit 单次、`commit_group`/`wait_group`），只是「源地址」从连续基址换成了页表查出的散列指针。

#### 4.3.2 核心流程

```
# 1) 查页表 → 寄存器（每个线程负责若干行）
for i in range(page_entry_per_thread):
    row = (本线程负责的行号)
    row_idx = n_block * n_block_size + row        # 逻辑 token 行号
    page, offset = divmod(row_idx + leftpad_k, page_size)
    if 行有效 and row_idx < seqlen_k:
        tPrPage[i]    = mPageTable[page]
        tPrPageOffset[i] = offset
    else:
        tPrPage[i] = 0                            # 无效行指向页 0，靠谓词屏蔽

# 2) 拼指针
for i in range(page_entry_per_thread):
    if V 且 SM100(转置): tPrXPtr[i] = &mX[d_offset, offset, page]
    else:                tPrXPtr[i] = &mX[offset, d_offset, page]

# 3) cp.async 散列搬运
for m in tile 内每一行:
    ptr = warp 内 shuffle 取出本行指针
    cute.copy(cp.async 原子, 从 ptr 搬, 到 sX[m], pred=行有效)
cp_async_commit_group()
```

关键技巧：**指针计算只在每个 warp 里做一份，再用 `shuffle_sync` 在 warp 内广播**（见 4.3.3），避免每个线程都查一遍页表。

#### 4.3.3 源码精读

**第一步 `load_page_table`**：

[flash_attn/cute/paged_kv.py:136-L155](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L136-L155) —— 重点三行：

- L147：`page_idx, page_offset = divmod(row_idx + self.leftpad_k, self.page_size_divmod)` —— 用编译期注入的 `FastDivmodDivisor` 做 `t // page_size` 与 `t % page_size`，`leftpad_k` 是左侧填充（本讲场景为 0）。
- L149-L151：`is_valid = (...) and row_idx < self.seqlen_k` —— 越界行（超出真实序列长度）谓词为假。
- L152：`page = self.mPageTable[page_idx] if is_valid else 0` —— 无效行指向页 0（数据无意义，但靠谓词保证不会被真正写入）。

**第二步 `compute_X_ptr`**（含 SM100 V 转置分支，与 4.4 呼应）：

[flash_attn/cute/paged_kv.py:157-L171](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L157-L171) —— 用 `utils.elem_pointer` 把 `(d_offset, page_offset, page)` 或 `(page_offset, d_offset, page)` 折算成 gmem 元素指针。注意坐标顺序随 V 是否转置而变（L167-L170）。

**第三步 `load_KV`** 与逐行搬运 `_copy_row_async`：

[flash_attn/cute/paged_kv.py:209-L247](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L209-L247) —— `load_KV` 先 `compute_X_ptr`，再按行调度。L237-L241 的 `shuffle_sync(tPrXPtr[m // gmem_threads_per_row], m % gmem_threads_per_row, ...)` 就是「warp 内广播指针」；L242-L246 用广播出的指针构造一个 `(head_dim,)` 的临时 gmem 视图 `mX_paged_cur`，交给 [flash_attn/cute/paged_kv.py:187-L207](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L187-L207) 的 `_copy_row_async` 按 `k` 维分多次 128-bit `cute.copy`。

`PagedKVManager.create` 在工厂方法里准备好这一切（tiled copy、谓词 `tKpK`/`tVpV`、页表寄存器）：[flash_attn/cute/paged_kv.py:45-L134](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L45-L134)。其中 L77-L87 构造 `gmem_tiled_copy_KV`（cp.async 原子 + 线程布局），L100/L109 用 `utils.predicate_k` 生成头维越界谓词。

#### 4.3.4 代码实践（阅读型）

**目标**：把三步调用链在主循环里串起来。

**步骤**：

1. 在 [flash_fwd_sm100.py:1432-L1447](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1432-L1447) 找到 `PagedKVManager.create(...)`，注意 `page_size = mK.shape[0]`（L1431，页池第一维大小，注意这里是 `mK.shape[0]` 即 `page_size`，因为 mK 已是 `(page_size, d, num_pages)` 的切片视图）。
2. 跟着 `load_K`/`load_V`（L1451-L1470 的 `partial`）进入 [flash_fwd_sm100.py:3014-L3060](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L3014-L3060)，确认非 TMA 分支调用 `paged_kv_manager.load_KV(block, sX_cur, K_or_V)`。
3. 画出三步时序：`load_page_table(n_block)` →（`load_K` 内）`compute_X_ptr("K")` → `_copy_row_async` ×N → `cp_async_commit_group()` → mbarrier 通知完成。K 和 V 各走一遍。

**需要观察的现象**：每个 `n_block` 恰好触发一次 `load_page_table`（首块在 L1484、循环块在 L1506），随后 K、V 各一次 `load_KV`。

**预期结果**：三步调用链与 4.3.2 的伪代码一一对应。

**运行结果**：源码阅读型，无需 GPU；如要观察真实执行，可用 `cute.printf` 加线程守卫在 `load_page_table` 末尾打印 `page`/`page_offset`（参考 AI/DEBUG_2CTA.md 的 printf bisection 思路）。

#### 4.3.5 小练习与答案

**Q1**：为什么无效行（`row_idx >= seqlen_k`）要把 `page` 设成 0 而不是干脆不写？

**A1**：因为 `compute_X_ptr` 必须给每个寄存器槽产出一个合法指针（后续 `shuffle_sync` 与 `cute.copy` 是向量化的、不带条件）。设成页 0 得到一个合法地址，再靠 `should_load`/`row_valid` 谓词（`load_KV` 里 L233-L235 的 `row_valid`）让这次 `cute.copy` 实际不写入 smem。这是 GPU kernel 常见的「指针有效 + 谓词屏蔽」模式。

**Q2**：`load_page_table` 里每个线程负责多少行？

**A2**：`page_entry_per_thread = n_block_size // num_threads`（paged_kv.py:89，见 create 的 L89）。即一个 tile 的 `n_block_size` 行均分给 `num_threads` 个线程，每线程查 `page_entry_per_thread` 个页表项。

---

### 4.4 SM100 上 V 的 gmem 转置与 smem 转置

#### 4.4.1 概念说明

K 与 V 在 attention 里的访问模式不同：算 `QK^T` 时沿 K 的 `head_dim` 维做点积；算 `PV` 时沿 V 的 `page_size`（序列）维做加权求和。SM100（Blackwell）据此做了一个布局优化：**把 V 在 gmem 页池里转置存储**——从与 K 相同的 `(page_size, dv, num_pages)` 变成 `(dv, page_size, num_pages)`，让 V 的序列维（`page_size`）变成最内层之外的低 stride 维，更利于后续 MMA 读取。

SM90（Hopper）则保持 V 与 K 同构，转置推迟到 MMA 之前用 `utils.transpose_view` 在 smem 层完成。这个差异由一个编译期布尔量表达：

\[
\text{v\_gmem\_transposed} = (\text{arch} \neq 90)
\]

即 SM100/SM110 转置、SM90 不转置。

#### 4.4.2 核心流程

```
arch == 90  (Hopper):   V gmem = (page_size, dv, num_pages)  ← 与 K 同构
                         compute_X_ptr 坐标 = (offset, d_offset, page)
arch != 90  (Blackwell): V gmem = (dv, page_size, num_pages)  ← 转置
                         compute_X_ptr 坐标 = (d_offset, offset, page)
                         smem 还要再做一次 (page_size, dv) → (dv, page_size) 转置
```

注意 gmem 转置只针对 **V**，K 始终是 `(page_size, d, num_pages)`。

#### 4.4.3 源码精读

- [flash_attn/cute/paged_kv.py:63-L65](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L63-L65) —— 注释与判定：`# SM100 transposes V in gmem to (dv, page_size, num_pages); SM90 keeps V as (page_size, dv, num_pages)`，`v_gmem_transposed = arch != 90`。
- [flash_attn/cute/paged_kv.py:157-L171](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L157-L171) `compute_X_ptr`：L163 `transposed = (K_or_V == "V" and self.v_gmem_transposed)`；L167-L168 转置分支 `utils.elem_pointer(mX, (d_offset, page_offset, page))`，L170 非转置分支 `(page_offset, d_offset, page)`。
- [flash_attn/cute/paged_kv.py:173-L185](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L173-L185) `_flatten_smem_sm100`：把搬进 smem 的 V 再转置一次——L183-L184 `if K_or_V == "V": sX_pi = make_tensor(sX_pi.iterator, select(sX_pi.layout, mode=[1,0]))` 即交换 smem 的前两个模式，得到 `(dv, page_size)` 布局。注意它在 `load_KV` 里仅对 `arch != 90` 调用（[paged_kv.py:215-L221](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L215-L221)，SM90 走 `group_modes` 且注释明说不在此时转置 V）。
- V 的越界谓词也随之适配：[flash_attn/cute/paged_kv.py:102-L109](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L102-L109)，转置时 `dv` 是 `shape[0]`、否则是 `shape[1]`。

#### 4.4.4 代码实践（阅读型）

**目标**：理解「gmem 转置 + smem 转置」两步如何配合，以及为何 SM90 不在 `PagedKVManager` 里转置 V。

**步骤**：

1. 读 [paged_kv.py:215-L221](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py#L215-L221)：SM90 走 `sX_pi = group_modes(sX, 0, 1)`，注释 `SM90 does NOT transpose V here (it's transposed via utils.transpose_view before MMA)`。
2. 对比 SM100 的 `_flatten_smem_sm100`：搬进 smem 后立刻把 V 交换成 `(dv, page_size)`。
3. 写一句话解释：为什么 gmem 已经转置了，smem 还要再转一次？

**预期**：gmem 转置是为了让**从页池读出**的字节流更连续（V 的序列维相邻）；smem 转置是为了让 **MMA 读 V** 时命中 Swizzle 布局、bank-conflict 最小。两者服务不同阶段的访存模式（gmem→smem 的 cp.async vs smem→rmem 的 MMA 取数）。

**运行结果**：源码阅读型，无需 GPU。

#### 4.4.5 小练习与答案

**Q1**：`v_gmem_transposed = arch != 90`，那 SM80 呢？

**A1**：理论上 `arch=80 != 90` 会得到 `True`，但**无意义**——因为 SM80 在更早的 [interface.py:826](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L826) 就 `assert page_table is None` 直接拒绝了分页 KV，根本不会构造 `PagedKVManager`。这个表达式只在真正会用它的 SM90/SM100/SM110 路径里有意义。

**Q2**：为什么 K 不需要转置？

**A2**：K 在 `QK^T` 中按 `head_dim` 维点积，其 `page_size`（序列）维是 M/N 维、`head_dim` 是 K 维，与 smem 里 MMA 想要的布局天然吻合；V 的 `page_size` 维在 `PV` 里作为 K 维参与、访问模式不同，故单独转置优化。

---

## 5. 综合实践

把本讲串起来：构造一个 **`block_table` 风格的分页 KV cache**，调用 `flash_attn_varlen_func` 的分页路径，再与「把同一份 KV 连续展开后」的非分页结果对比，验证数值一致。范本直接取自测试 [tests/cute/test_flash_attn.py:1806-L1835](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L1806-L1835) 的 `_generate_block_kvcache` 与 L1527 的调用。

**目标**：亲手跑通「分页 = 连续」的等价性，并感受 `page_size` 取值对路径的影响。

**操作步骤**（以下为示例代码，标注「示例代码」；尚未在本机验证运行结果）：

```python
# 示例代码：分页 KV vs 连续 KV 等价性验证
# 需要在 Hopper(SM90) 或 Blackwell(SM100/SM110) GPU 上运行
import math, torch
import flash_attn.cute as fa_cute   # FA4

device = "cuda"; dtype = torch.float16
batch, seqlen_q, seqlen_k = 2, 64, 512
nheads, nheads_kv, head_dim = 8, 2, 64          # GQA
page_size = 128                                   # 本讲指定

# ---- 1) 构造 q ----
q = torch.randn(batch, seqlen_q, nheads, head_dim, device=device, dtype=dtype)

# ---- 2) 构造分页 KV 页池 + page_table（仿 _generate_block_kvcache）----
num_pages_per_seq = math.ceil(seqlen_k / page_size)
num_pages = num_pages_per_seq * batch * 3         # 池子开大些，制造“散落”
k_paged = torch.randn(num_pages, page_size, nheads_kv, head_dim, device=device, dtype=dtype)
v_paged = torch.randn(num_pages, page_size, nheads_kv, head_dim, device=device, dtype=dtype)

# 随机给每条序列分配 num_pages_per_seq 个物理页（int32!）
perm = torch.randperm(num_pages, device=device)[: batch * num_pages_per_seq]
page_table = perm.reshape(batch, num_pages_per_seq).to(torch.int32)   # (batch, max_num_pages_per_seq)

# ---- 3) 分页前向（注意：走 flash_attn_varlen_func，且 q 仍按 batch/seqlen 给）----
# 分页路径走 flash_attn_varlen_func：q 用 (batch, seqlen_q, ...) 形式，KV 给页池，附 page_table
out_paged, lse_paged = fa_cute.flash_attn_varlen_func(
    q, k_paged, v_paged, page_table=page_table,
    max_seqlen_q=seqlen_q, max_seqlen_k=seqlen_k,
    softmax_scale=1.0 / math.sqrt(head_dim), causal=False,
)

# ---- 4) 把分页展开成连续 KV 作为参考 ----
def expand_paged(kv_paged, page_table, seqlen_k):
    # 按每条序列的 page_table 顺序 gather 物理页，再折叠成连续 seqlen
    gathered = kv_paged[page_table.reshape(-1)]            # (b*nblocks, page_size, h, d)
    b, nblocks = page_table.shape
    contig = gathered.reshape(b, nblocks * page_size, *gathered.shape[2:])[:, :seqlen_k]
    return contig

k_contig = expand_paged(k_paged, page_table, seqlen_k)
v_contig = expand_paged(v_paged, page_table, seqlen_k)

out_ref, lse_ref = fa_cute.flash_attn_func(
    q, k_contig, v_contig, softmax_scale=1.0 / math.sqrt(head_dim), causal=False,
)

print("out max abs diff:", (out_paged - out_ref).abs().max().item())
print("lse max abs diff:", (lse_paged - lse_ref).abs().max().item())
```

**需要观察的现象与预期结果**：

1. `out_paged` 与 `out_ref` 的最大绝对误差应为 fp16 量级（约 1e-2 ~ 1e-3，取决于硬件与 hdim）；`lse_paged` 与 `lse_ref` 几乎相同。这证明**分页只是布局变换，不改数学结果**。
2. 把 `page_size` 改成 `64` 或 `1`：结果仍应一致，但（若你能测耗时）会发现走的是 `PagedKVManager` 非 TMA 路径、相对更慢；`page_size=128`（== 默认 `tile_n`）走 TMA 直取路径。
3. 若强行在 Ampere/SM120 上跑，会触发 [interface.py:826](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L826) / [interface.py:945](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L945) 的 `AssertionError`。

**运行结果**：待本地验证（本环境无 GPU，未实跑）。可参考 `pytest tests/cute/test_flash_attn.py -k paged -x` 的既有用例（如 `test_flash_attn_paged_deepseek`，[test_flash_attn.py:1841](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L1841)）确认该等价性是被 CI 持续守护的。

> 提示：测试里 `page_size` 的参数化是 `[None] + ([1, 4, 128])`（[test_flash_attn.py:1091](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L1091)），正好覆盖「非分页 / 解码 page=1 / 小页 4 / 对齐页 128」四种典型情形，可作为你自测的取值表。

## 6. 本讲小结

- 分页 KV cache 用**页表** `page_table (batch, max_num_pages_per_seq)` 把逻辑 token 映射到物理页池 `(num_pages, page_size, h_kv, d)`，KV 张量形状从 `(batch, seqlen, ...)` 变成 `(num_pages, page_size, ...)`，`seqlen_k = num_pages * page_size`。
- 入口是 **`flash_attn_varlen_func`**（`flash_attn_func` 不支持 `page_table`）；与 `cu_seqlens_k` 互斥；`page_table` 必须 `int32` 且最后一维连续。
- **`page_size` 决定路径**：`page_size == tile_n` 走 TMA 直取（每块查一个 `page_idx`）；`page_size != tile_n` 走 `PagedKVManager` 的 cp.async 散列 gather。该布尔进入 `compile_key`，改 `page_size` 会重编译。
- `PagedKVManager` 三步走：`load_page_table`（divmod 查 `(page,offset)` 进寄存器）→ `compute_X_ptr`（拼 gmem 指针）→ `load_KV`（cp.async + warp 内 `shuffle_sync` 广播指针 + 谓词屏蔽无效行）。
- **SM100 把 V 在 gmem 转置**为 `(dv, page_size, num_pages)`（`v_gmem_transposed = arch != 90`），并在 smem 里再做一次 `(dv, page_size)` 转置；SM90 保持 V 与 K 同构、推迟到 MMA 前转置。
- 架构边界：分页 KV 仅 SM90/SM100/SM110 前向支持；SM80、SM120 被显式 `assert` 拒绝；2CTA 还要求 `page_size in [None, 128]`。

## 7. 下一步学习建议

- **u8-l1 Blackwell 前向 Kernel 全景**：本讲的 SM100 分页路径活在 `FlashAttentionForwardSm100` 的 persistent / 2CTA 框架里，下一步应整体读这个 kernel，把分页加载与 UMMA、tmem 累加、persistent 调度拼起来。
- **u7-l2 SplitKV 与 Combine**：分页 KV 常与长上下文解码共存，理解 SplitKV 如何切分 `n_block` 区间（u3-l2 的 `get_n_block_min_max` + `is_split_kv`）后，可尝试分析「分页 + SplitKV」的兼容性（注意 2CTA 与 SplitKV 互斥）。
- **继续读源码**：通读 [paged_kv.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/paged_kv.py) 全文与 [flash_fwd_sm90.py:692-L819](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm90.py#L692-L819) 的 Hopper 版 `PagedKVManager` 使用，对比 SM90 与 SM100 两套实现的细微差异。
