# Top-k KV Gather 稀疏

## 1. 本讲目标

本讲聚焦 FA4 的 **Top-k KV Gather 稀疏注意力**机制。它由 `flash_attn/cute/topk_gather_kv.py` 中的 `CpasyncGatherKVManager` 实现，目前只接在 MLA 吸收（absorption）前向 kernel 上。读完本讲，你应当能够：

- 说清「Top-k gather」的动机：对每个 query，先用一个**外部打分器**选出最相关的 k 个 KV token（下标存进 `gather_kv_indices`），再只对这 k 个 token 做精确注意力，从而把注意力的算力与带宽按 k / seqlen_k 的比例降下来。
- 看懂 gather 的「三步法」：`load_index_topk`（取下标）→ `compute_X_ptr`（按下标算 gmem 指针）→ `load_X`（用 `cp.async` 把散落的行 gather 成一块连续 smem tile），以及为什么这里**必须用 cp.async 而非 TMA**。
- 理解 gather 之后仍是「精确注意力」——非法（越界 / 占位）行通过 `compute_bitmask` 生成一个 32 列位图，由消费侧 `apply_mask_sm100` 把对应分数置 `-inf`，所以结果精确而非近似。
- 能把 Top-k gather 与 [u10-l1 块稀疏](u10-l1-block-sparsity.md) 做对比：前者在**token 粒度**做内容相关、形状任意的动态选取；后者在**块粒度**做由 `mask_mod` 导出的静态选取。

本讲是「稀疏注意力与 MLA」单元的第三篇，承接 [u10-l2 MLA](u10-l2-mla.md) 里建立的 MLA 吸收公式与 [u5-l2 拷贝原子](u5-l2-copy-utils-and-tma.md) 里的 cp.async 搬运，并与 [u7-l2 SplitKV](u7-l2-splitkv-and-combine.md) 中「长 KV」这一主题形成呼应（两者都是为长上下文 / 解码减负，但思路不同）。

## 2. 前置知识

进入本讲前，请确认你理解以下几个概念（都来自前置讲义）：

- **tile 与块网格**：FA4 把 Q 切成 `tile_m` 的 Q tile、KV 切成 `tile_n` 的 KV tile，注意力被拆成「一个 Q tile 对一个 KV tile」的小矩阵乘。常规前向里每个 n_block 对应 KV cache 中**连续**的 `tile_n` 行（[u3-l2 BlockInfo](u3-l2-block-info.md)）。
- **MLA 吸收公式**（[u10-l2](u10-l2-mla.md)）：\( O=\text{softmax}(\text{scale}\cdot(QK^{\mathsf T}+Q_v V^{\mathsf T}))\,V \)，其中 Q 携带位置编码、\(Q_v\) 携带潜在分量、V 是被压缩的潜在 KV。MLA 的 KV cache 极长，正是 Top-k gather 的主要落地场景。
- **cp.async 拷贝原子**（[u5-l2](u5-l2-copy-utils-and-tma.md)）：Ampere/Blackwell 上单线程发起的 128-bit 异步全局→共享搬运，靠 `commit_group`/`wait_group` 或 mbarrier 跟踪完成；TMA（`cp.async.bulk`）虽然搬整块更快，但要求**规整、连续**的访存，无法做逐 token 的散列 gather。
- **在线 softmax**（[u4-l1](u4-l1-online-softmax.md)）：行最大值/行和逐块维护，把某些分数置 `-inf` 等价于让这些位置不贡献概率质量——这是 Top-k「只算一部分 KV 仍精确」的数学根基。

> **关键直觉**：Top-k gather 是一种**加速手段，不是新算法**。它先把「该看哪些 KV」这件事交给一个外部打分器（产物是 `gather_kv_indices`），kernel 内部再把这些被选中的 token gather 到一起、照常跑在线 softmax。如果选中的恰好是「正确」的那些 token，结果就是精确的；即使选错或越界，位图掩码也能保证不出现非法数值。它的代价是 gather 本身（按下标逐行搬运）和外部打分。

## 3. 本讲源码地图

本讲涉及的关键源码文件（都在 `flash_attn/cute/` 下）：

| 文件 | 作用 |
| --- | --- |
| [topk_gather_kv.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/topk_gather_kv.py) | 本讲主角。`CpasyncGatherKVManager` 数据类 + 四个 `@cute.jit` 方法：取下标 `load_index_topk`、算指针 `compute_X_ptr`、搬运 `load_X`、生成有效位图 `compute_bitmask`。是「token 级散列 gather 的执行核心」。 |
| [flash_fwd_mla_sm100.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py) | MLA 吸收前向 kernel `FlashAttentionMLAForwardSm100`。在 cpasync load warpgroup 里调用 gather manager 把 K/V gather 进流水线，并在消费侧把位图交给 `apply_mask_sm100`。是「gather 的调用方」。 |
| [interface.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py) | 公共参数 `gather_kv_indices` 的接入点：形状校验、与 `qv` 绑定、与分页 KV 互斥、`is_topk_gather` 进 kernel 构造与 `compile_key`。 |
| [mask.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py) | `AttentionMask.apply_mask_sm100` 的 `rBitmask` 分支：读取 32 列位图、把非法列分数置 `-inf`。是「gather 之后精确化的最后一道关」。 |
| [testing.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/testing.py) | 参考实现 `attention_ref` 里对 `gather_kv_indices` 的处理：把未选中的 KV 位置在 `scores` 上填 `-inf`，是 kernel 行为的「金标准」。 |

整条链路是：**接口约定 → gather manager 执行搬运 → 前向 kernel 调用 → 掩码精确化 → 参考实现对照**。

## 4. 核心概念与源码讲解

### 4.1 Top-k 块选择策略：动机与接口

#### 4.1.1 概念说明

普通注意力里，一个 query 要对所有 `seqlen_k` 个 key 算分数。但实践中（尤其长上下文、解码、MLA），真正对某个 query 有贡献的 KV token 往往只是少数——大部分 token 的注意力权重接近 0。如果能**先挑出最有用的 k 个 KV token，再只对它们做精确注意力**，就能把算力与带宽从 \(O(\text{seqlen}_k)\) 降到 \(O(k)\)。

「挑选」这件事由一个**外部打分器**完成（不在本 kernel 内），产物是 `gather_kv_indices`——一个记录「每个 query 该看哪些 KV 行号」的整型张量。kernel 只负责按这些行号把 KV gather 进来、照常做在线 softmax。

接口上（仅 MLA / `qv` 路径支持，[interface.py:714](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L714) 显式断言 `gather_kv_indices is only supported with qv`）：

```text
gather_kv_indices: (total_q, gather_kv_length)        # varlen
                  或 (batch, seqlen_q, gather_kv_length)  # 定长
```

其中 `gather_kv_length` 就是这里的「k」。两条硬约束（[interface.py:693-696](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L693-L696)）：

- `gather_kv_indices.shape[:-1] == qv.shape[:-2]`（每个 query 都有一组自己的 top-k 行号）；
- `gather_kv_length % 128 == 0`（必须对齐 `tile_n=128`，因为 gather 是按整块 `tile_n` 行进行的）。

还与分页 KV 互斥（[interface.py:685](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L685)）：`paged KV + topk sparsity not yet supported together`。

#### 4.1.2 核心流程

前向 kernel 拿到 `gather_kv_indices` 后，n_block 的范围从「覆盖整条 KV」变成「覆盖 k 个被选 token」：

```text
n_block_max = gather_kv_length // tile_n      # 例如 k=2048, tile_n=128 → 16 个 n_block
for n_block in range(n_block_max-1, -1, -1):  # 仍是倒序遍历
    取出本 n_block 对应的 tile_n 个 KV 行号  mIndexTopk[n_block*tile_n : ...]
    用 cp.async 把这 tile_n 个散落在 gmem 的行 gather 进一块连续 smem
    (可选) 计算本 tile 的有效位图，存进 sBitmask
    做正常的 QK / PV GEMM + 在线 softmax（消费侧用位图把非法行置 -inf）
```

注意一个关键点：**被 gather 的 `tile_n` 行在原 KV cache 里不一定连续**，它们是任意散布的；但 gather 进 smem 之后，它们被重排成一块规整的 `(tile_n, hdim)`，后续 GEMM / softmax 与稠密路径完全一样。这就是「gather 之后再做精确注意力」的含义。

#### 4.1.3 源码精读

入口在 `FlashAttentionMLAForwardSm100` 的 cpasync load warpgroup 里。当 `is_topk_gather` 为真时，n_block 范围改用 `topk_length // tile_n`，绕过了常规的因果/局部 `BlockInfo.get_n_block_min_max`：

[flash_fwd_mla_sm100.py:1424-1432](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L1424-L1432) —— topk 路径下 `n_block_min=0`、`n_block_max = topk_length // tile_n`；否则回退到 `block_info.get_n_block_min_max`。注释里还保留了一个 `topk_length_dynamic` 的备选（按每个 query 的真实 k 动态取上界），目前用的是编译期常量 `self.topk_length`。

随后为当前 query tile 取出对应的下标切片 `mIndexTopk_cur`（定长 `[None, m_idx, batch_idx]`、varlen `[None, m_idx + offset_q]`），再据此创建 gather manager：

[flash_fwd_mla_sm100.py:1452-1470](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L1452-L1470) —— `CpasyncGatherKVManager.create(...)` 把下标张量、CTA 内坐标、`topk_length`、`seqlen_k_limit`、tile/hdim 配置与位图相关的 smem / pipeline 全部打包成一个管理器实例。

`seqlen_k_limit` 是另一处与因果相关的细节：非因果时 `= seqlen_k`；因果时 `= m_local_idx + 1 + seqlen_k - seqlen_q`（[flash_fwd_mla_sm100.py:1445-1451](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L1445-L1451)），它界定了「哪些被 gather 的行号是合法的」，供位图判断越界。

#### 4.1.4 代码实践

> **实践目标**：从接口层确认 Top-k gather 的输入约束与触发条件。
>
> **操作步骤**：
> 1. 打开 `flash_attn/cute/interface.py`，定位 `gather_kv_indices is not None` 的分支（约 690 行）。
> 2. 阅读三条断言：与 `qv` 绑定、形状匹配、`gather_kv_length % 128 == 0`、与分页 KV 互斥。
> 3. 在约 873-884 行确认 `is_topk_gather=sparse_kv` 与 `topk_length=gather_kv_length` 如何作为构造参数传给 `FlashAttentionMLAForwardSm100`。
>
> **需要观察的现象**：`gather_kv_length` 既是 kernel 内 `n_block_max` 的分子，也作为下标张量最后一维长度出现。
>
> **预期结果**：你会看到 `topk_length` 被强制为 128 的倍数，且 `is_topk_gather`（一个布尔）会进入 kernel 的编译期开关——**改它（即从无 gather 切到有 gather）会触发重编译**。
>
> **待本地验证**：若手头有 Blackwell 卡，可用 `gather_kv_indices=None` 与一份合法的 `gather_kv_indices` 各跑一次 MLA 前向，观察第二次是否发生 JIT 编译。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `gather_kv_length` 必须是 128 的倍数？

**参考答案**：因为 gather 是以 `tile_n=128` 为单位逐块进行的——每次 `load_index_topk` + `load_X` 处理整整 `tile_n` 行并搬进一块 `(tile_n, hdim)` 的 smem tile。若 k 不是 128 的倍数，最后一块会是不完整的 tile，需要额外的边界处理；当前实现用「位图掩码」来吸收越界行（见 4.3），但前提是 tile 本身仍是完整的 128 行，故要求 `gather_kv_length % 128 == 0`。

---

### 4.2 Gather 后的精确注意力：cp.async 三步法

#### 4.2.1 概念说明

「gather」指把**散落在 gmem 各处的 `tile_n` 行 K/V** 搬成**一块连续的 smem tile**。难点在于：这 `tile_n` 行的下标来自 `mIndexTopk`，彼此毫无规律，无法用 TMA（TMA 需要一个预编译的描述符描述一块规整区域）。因此 gather 走的是 **cp.async**——每个线程拿一个「自己负责的行」的下标、算出该行在 gmem 的指针、发起一次 128-bit 异步拷贝。

`CpasyncGatherKVManager` 把这件事拆成清晰的三步，外加一步位图：

| 方法 | 职责 | 产出 |
| --- | --- | --- |
| `load_index_topk` | 从 `mIndexTopk` 把本 n_block 的 `tile_n` 个行号读进寄存器（每线程若干个） | `rTopk` / `rTopkHalf` |
| `compute_X_ptr` | 按行号算出每个线程负责行的 gmem 指针（`utils.elem_pointer`）与「该行是否合法」标志 | `tPrXPtr` / `tPrRowValid` |
| `load_X` | 用 cp.async 把指针指向的行搬进 smem tile，非法行用谓词屏蔽 | 写满 `sX[stage]` |
| `compute_bitmask` | 汇总「哪些行合法」成一个 32 列位图，存进 `sBitmask` | `bitmask`（uint32） |

gather 完成后，K/V 在 smem 里就是规整的 `(tile_n, hdim)`，后续 GEMM（`Q@K^T`、`P@V`）与在线 softmax 与稠密路径**逐字相同**——这正是「gather 之后再做精确注意力」的工程含义。

#### 4.2.2 核心流程

```text
# producer（cpasync load warpgroup）每个 n_block 做的事
load_index_topk(n_block):           # 每 thread 读 entries_per_thread = tile_n/num_threads 个行号
    rTopk[i] = mIndexTopk[n_block*tile_n + row(i)]

compute_X_ptr(mX):                  # 把行号翻译成 gmem 行指针 + 合法性
    topk_idx = rTopk[i]
    tPrRowValid[i] = (0 <= topk_idx < seqlen_k_limit)
    tPrXPtr[i]     = elem_pointer(mX, (topk_idx, d_offset)).toint()

load_X(mX, sX):                     # cp.async gather
    每个线程负责若干 (m, k) 元素：
      通过 shuffle_sync 把「行指针」广播给同行的线程
      cute.copy(cp.async_atom, mX_cur_copy_ki, tXsX_k, pred=row_valid)

# 可选：构造有效位图交给消费侧
compute_bitmask():                  # 见 4.3
```

关键技巧是「**线程按列分、行号靠 shuffle 广播**」。下标张量被设计成 `topk_indices_per_thread = tile_n // num_threads` 个/线程（[topk_gather_kv.py:102](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/topk_gather_kv.py#L102)），`load_index_topk` 用一段交织的行号映射（[topk_gather_kv.py:144-156](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/topk_gather_kv.py#L144-L156)）让 128 个线程均匀覆盖 128 行；而 `load_X` 里搬运一行 `hdim` 元素需要多个线程协作（`gmem_threads_per_row` 个线程负责一行），于是用 `utils.shuffle_sync` 把「持有行指针的那个线程」的指针广播给同行的伙伴（[topk_gather_kv.py:257-261](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/topk_gather_kv.py#L257-L261)）。这与 [u7-l3 分页 KV](u7-l3-paged-kv.md) 里 `PagedKVManager` 的散列 gather 是同一套思路。

#### 4.2.3 源码精读

`create()` 在编译期确定拷贝原子与线程布局。注意几条 `assert` 锁定了当前实现的能力边界：

[topk_gather_kv.py:73-89](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/topk_gather_kv.py#L73-L89) —— `num_threads == 128`、`hdim % 64 == 0`、`(hdim_v // num_hdimv_splits // cta_group_size) % 64 == 0`，并按 `gcd(hdim, hdim_v/..., 128//dtype_bytes)` 算出 `gmem_k_block_size` 与每行线程数 `gmem_threads_per_row`。拷贝原子是单次 128-bit 的 `CopyG2SOp`（[topk_gather_kv.py:90-94](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/topk_gather_kv.py#L90-L94)）。

`compute_X_ptr` 把行号翻译成指针与合法性，`transpose` 分支对应 V 在 gmem 中转置（`(d, topk_idx)`）的布局：

[topk_gather_kv.py:206-216](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/topk_gather_kv.py#L206-L216) —— `tPrXPtr[i] = elem_pointer(mX, (topk_idx, d_offset))`（非转置）或 `(d_offset, topk_idx)`（转置）；`disable_bitmask` 关闭时同时算 `row_valid`。

`load_X` 是真正的 gather 主体，两层循环：外层按行 `m`、内层按 `hdim` 方向的 `k` 段搬运：

[topk_gather_kv.py:257-278](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/topk_gather_kv.py#L257-L278) —— `shuffle_sync` 把行指针广播给同行线程；为每个线程构造一个一维 `mX_cur` 视图（起始指针 = 广播来的行指针），再 `tiled_divide` 成 128-bit 段；最后 `cute.copy(gmem_tiled_copy_KV, src, dst, pred=should_load)` 完成异步搬运，`pred` 用 `should_load`（来自 `row_valid`）屏蔽非法行。

搬运的发起方在 `cpasync_gather_load_KV` 里，它把「获取流水线 stage → gather manager.load_X → commit_group → 通知 mbarrier」串起来：

[flash_fwd_mla_sm100.py:1709-1729](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L1709-L1729) —— 注意 `load_X` 写入的是 `sX[None, None, None, stage]`（多级环形缓冲里的某个 stage），随后 `cp_async_commit_group()` 与 `sync_object_full.arrive_cp_async_mbarrier(stage)` 把「这一 stage 搬完了」通知给 mbarrier，消费侧才能安全读。

producer 侧的主循环按 prologue / mainloop / epilogue 三段排布，K/V 与转置 Vt（`transpose=True`）交错搬运：

[flash_fwd_mla_sm100.py:1524-1566](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L1524-L1566) —— 每次迭代先 `load_index_topk(n_block-1)` 取下一块的行号、再 `load_K/load_V` gather、再 `compute_bitmask`；转置的 Vt 用 `load_index_topk(n_block, transpose=True)` 取另一份行号。这与 [u5-l1 流水线](u5-l1-pipeline-state.md) 里「producer 抢占 stage → 搬运 → 释放」的模型一致。

#### 4.2.4 代码实践

> **实践目标**：在源码里跟踪一次 KV 行的 gather 数据路径。
>
> **操作步骤**：
> 1. 打开 `topk_gather_kv.py` 的 `load_index_topk`（135-161 行），看清 `rTopk[i]` 是如何按下标张量取值的。
> 2. 进入 `compute_X_ptr`（194-216 行），找到 `elem_pointer(mX, (topk_idx, d_offset))`——这是把「第 `topk_idx` 行」翻译成 gmem 指针的一行。
> 3. 进入 `load_X`（218-278 行），定位 `shuffle_sync(tPrXPtr[...], ...)` 与最终的 `cute.copy(..., pred=should_load)`。
>
> **需要观察的现象**：指针是「每线程算一次、再 warp 内广播」；搬运本身带 `pred`，非法行不产生访存。
>
> **预期结果**：你能画出「`mIndexTopk` 行号 → 行指针 → cp.async → smem tile stage」的完整数据路径，并解释为何此处无法用 TMA（下标不连续、形状不规整）。

#### 4.2.5 小练习与答案

**练习 1**：`load_index_topk` 里 `transpose` 参数为 `True` 与 `False` 时分别写到哪个寄存器张量？为什么需要两份？

**参考答案**：`transpose=True` 写 `rTopk`，`False` 写 `rTopkHalf`（[topk_gather_kv.py:142](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/topk_gather_kv.py#L142)）。需要两份是因为同一批 `tile_n` 行号在「非转置 K/V」与「转置 Vt」两条搬运流水里被**错位一拍**地复用（producer 主循环里先 `load_index_topk(n_block-1, transpose=False)`、再 `load_index_topk(n_block, transpose=True)`，见 [flash_fwd_mla_sm100.py:1543-1555](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L1543-L1555)），用两个寄存器张量避免互相覆盖。

---

### 4.3 有效位图（bitmask）：让 gather 结果精确

#### 4.3.1 概念说明

gather 进来的 `tile_n` 行里，**未必每一行都合法**。两种情况会产生非法行：

- **越界行号**：外部打分器可能用 `-1`（占位）或超出 `[0, seqlen_k_limit)` 的值填充未用满的位置；
- **因果边界**：因果模式下 `seqlen_k_limit = m_local_idx + 1 + seqlen_k - seqlen_q`，比 `seqlen_k` 小，于是某些被 gather 的行号会落在合法窗口之外。

如果不对这些行处理，它们仍会被算进 `QK^T` 与 softmax，污染结果。`compute_bitmask` 的任务就是给每个 32 列块打一个「1=合法、0=非法」的位图，让消费侧在 softmax 前把非法列的分数置 `-inf`。这等价于「这些位置本就不参与注意力」，所以最终结果**精确**——与参考实现里 `masked_fill_(..., -inf)` 一致。

#### 4.3.2 核心流程

位图构造用了一个精巧的「**lane 即列**」编码：每个 warp 有 32 个 lane，恰好对应一个 32 列块。每个 lane 持有自己那一行的 `row_valid`，置位 `1 << lane_idx`，再做一次 warp 内按位 OR 归约（因为各 lane 的行号互斥，OR 与加等价），就得到一个 uint32 位图：

```text
# compute_bitmask（producer 侧，每 warp 产出一个 uint32）
lane_idx = thread_idx % 32
topk_idx = rTopk_NonInterleaved[0]            # 本 lane 负责的行号
bitmask  = (1 << lane_idx)  if (0 <= topk_idx < seqlen_k_limit) else 0
bitmask  = warp_reduce(bitmask, op=+)         # warp 内 OR（互斥故加法等价）
# 经 pipeline_bitmask 存进 sBitmask[warp_idx, stage]
```

消费侧 `apply_mask_sm100` 拿到 `rBitmask`（一组 uint32，每 32 列一个），逐位解包、把 0 位对应的分数列改成 `-inf`：

```text
# mask.py apply_mask_sm100 的 rBitmask 分支
for i in range(ncol_packed):        # 每 i 覆盖 32 列
    val = rBitmask[i]
    for j in range(32):
        col = 32*i + j
        acc_S[col] = acc_S[col]  if ((val >> j) & 1)  else -inf
```

这与 [u3-l1](u3-l1-attention-mask.md) 中因果掩码的 R2P 位图是同一种「32 列压一个 uint32」的思路，区别在于这里的位**不是由 `q_idx/kv_idx` 公式生成，而是由 gather 时逐行查 `seqlen_k_limit` 得到**。

#### 4.3.3 源码精读

`compute_bitmask` 全程（注意它对 `rTopk_NonInterleaved` 的依赖——`load_index_topk` 在非转置且未禁用位图时，会额外按「不交织」的顺序读一个行号进 `rTopk_NonInterleaved[0]`，供本方法逐 lane 判合法性）：

[topk_gather_kv.py:163-192](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/topk_gather_kv.py#L163-L192) —— 逐 lane 置位 `1 << lane_idx`、`warp_reduce(bitmask, operator.add)` 做按位 OR，再经 `pipeline_bitmask.producer_acquire/commit` 与 `cpasync_barrier.arrive_and_wait` 把位图写进 `sBitmask[warp_idx, stage]` 并推进流水。`warp_reduce` 的加法在「各 lane 的行号互斥」前提下与按位 OR 等价（见 [utils.py:319-333](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L319-L333) 的蝴蝶归约实现）。

消费侧读取位图并交给掩码函数：

[flash_fwd_mla_sm100.py:2801-2812](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_mla_sm100.py#L2801-L2812) —— `pipeline_bitmask.consumer_wait` 等到位图就绪，按 `warp_idx` 偏移读 `rBitmask`，随后 `mask_fn(tSrS_t2r, n_block=n_block, rBitmask=rBitmask)`。`mask_fn` 即 `apply_mask_sm100` 的 partial（`mask_seqlen=False`，因为 seqlen 边界已由位图负责）。

位图真正起作用的位运算在 `apply_mask_sm100`：

[mask.py:649-657](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L649-L657) —— `rBitmask is not None` 分支：每 32 列一个 uint32，逐位 `(curr_mask_val >> j) & 1` 判保留，否则置 `-Float32.inf`。注意这是 gather 路径的**独立分支**，与下面的因果 / 局部 `r2p_bitmask_*` 分支互斥（`elif`）。

有一个优化开关 `disable_bitmask`：当外部保证所有 gather 行号都合法（如非因果且 `seqlen_k >= gather_kv_length`）时，可以关掉位图、连 `pipeline_bitmask` 都不建（[interface.py:692](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L692) 起默认 `False`，附带一段被注释掉的自动判断逻辑）。

#### 4.3.4 代码实践

> **实践目标**：对照参考实现，验证「位图置 `-inf`」与「未选中 KV 置 `-inf`」等价。
>
> **操作步骤**：
> 1. 打开 `flash_attn/cute/testing.py` 的 `attention_ref`，定位 `if gather_kv_indices is not None:` 分支（约 418-425 行）。
> 2. 阅读它如何构造 `topk_index_mask` 并对未选中位置 `masked_fill_(-inf)`。
> 3. 回到 [mask.py:649-657](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mask.py#L649-L657)，对照位图解包逻辑。
>
> **需要观察的现象**：参考实现是在「完整 `seqlen_k` 列」上把未选中列置 `-inf`；kernel 则是先 gather 成 `gather_kv_length` 列、再把其中非法列置 `-inf`。两者都使「未被选中的 KV 不贡献概率」。
>
> **预期结果**：你会确认两者数学等价——kernel 输出与参考实现只差 fp16 舍入。
>
> **待本地验证**：构造一组带 `-1` 占位行的 `gather_kv_indices`，确认输出不含 NaN（即非法行确被屏蔽）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `compute_bitmask` 用 `warp_reduce(bitmask, operator.add)` 而不是显式的按位 OR？

**参考答案**：因为每个 lane 只把自己那一位（`1 << lane_idx`）置 1，各 lane 的置位互不重叠，此时「按位 OR」与「整数加法」结果完全相同。`warp_reduce` 的蝴蝶归约（[utils.py:331-332](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L331-L332)）用 `op=` 参数即可复用，不必为按位 OR 另写一条归约路径。

---

### 4.4 与静态块稀疏的对比

#### 4.4.1 概念说明

Top-k gather 与 [u10-l1 块稀疏](u10-l1-block-sparsity.md) 都属于「只算一部分 KV」的稀疏注意力，但两者的**选取粒度、选取来源与实现手段**截然不同：

| 维度 | Top-k gather（本讲） | 块稀疏（u10-l1） |
| --- | --- | --- |
| 选取粒度 | **token 级**（`tile_n` 个任意行号 gather 成一块） | **块级**（整块 `tile_n` 对齐的 full/partial/skip） |
| 选取来源 | **内容相关 / 动态**：外部打分器给出 `gather_kv_indices`，可任意散布 | **结构相关 / 静态**：由 `mask_mod` 经 `compute_block_sparsity` 预计算 |
| 选取可否任意 | 可以，行号完全任意（散落、不连续） | 受限，必须落在 `tile_n` 对齐的块网格上 |
| 搬运方式 | cp.async 逐行 gather（TMA 做不到） | 整块 TMA / cp.async（块本身连续） |
| 精确性保证 | 位图把非法 gather 行置 `-inf` | full 块免掩码、partial 块套 `mask_mod`、skip 块跳过 |
| 接入 | 仅 MLA 吸收 kernel（`qv` 路径，SM100/SM110） | 通用前向/反向（经 `block_sparse_tensors`） |
| 是否进 compile_key | `is_topk_gather`（布尔）进 | `use_block_sparsity` 等进 |

一句话总结：**块稀疏是「按规则跳过整块」，Top-k gather 是「按打分挑出任意 token」**。前者适合掩码形状规则（因果、局部、结构化稀疏）的场景；后者适合「哪些 token 重要」只能由内容动态决定的场景（如 MLA 的检索式注意力、学习到的稀疏）。

#### 4.4.2 核心流程：理论 FLOPs 下降的估算

注意力的算力主体是两次 GEMM（以 MLA 吸收公式为例，省略 scale）：

\[
\text{FLOPs} \;\approx\; \underbrace{2\cdot \text{sq}\cdot \text{sk}\cdot d}_{QK^{\mathsf T}}
\;+\; \underbrace{2\cdot \text{sq}\cdot \text{sk}\cdot d_v}_{Q_v V^{\mathsf T}}
\;+\; \underbrace{2\cdot \text{sq}\cdot \text{sk}\cdot d_v}_{PV}
\]

其中 sq、sk 分别是 query / key 序列长，d、\(d_v\) 是头维。三项都**线性依赖 sk**。Top-k gather 把「真正参与运算的 key 数」从 sk 降到 k，于是：

\[
\frac{\text{FLOPs}_{\text{topk}}}{\text{FLOPs}_{\text{dense}}} \;\approx\; \frac{k}{\text{sk}}
\]

保留 top-25%（即 \(k/\text{sk}=0.25\)）时，注意力主干的算力降到约 **25%**，即理论 **下降约 75%**、约 **4× 加速**。KV 带宽同样按比例下降（只搬 k 个 token 而非 sk 个）。需要注意两点修正：

- **额外开销**：gather 本身要按下标逐行搬运（比连续 TMA 慢），还要读 `gather_kv_indices`、算位图；以及外部打分器的开销不在本 kernel 内。
- **近似性**：这是「如果选中的就是最重要的 token」的上界；若打分器选错，省下的算力换来的是精度损失——但只要选对，结果是**精确**的（区别于低秩近似等真正有损的方法）。

#### 4.4.3 源码精读

两者在接口层的并存与互斥关系：Top-k gather 走 `gather_kv_indices`（仅 `qv`），块稀疏走 `block_sparse_tensors`（通用）。同一个 kernel 不会同时启用两者；MLA 吸收 kernel 也明确不接受块稀疏（`score_mod`/`mask_mod`/`softcap` 等在 `qv` 路径下被断言为 None，见 [interface.py:680-682](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L680-L682)）。

值得对比的是搬运实现：块稀疏因为块本身连续，仍可用 TMA 整块搬；而 Top-k gather 因为行号散乱，**只能**用 cp.async（[topk_gather_kv.py:90-100](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/topk_gather_kv.py#L90-L100) 的 `cpasync.CopyG2SOp` 原子），这也是它性能上限低于块稀疏、却更灵活的根本原因。

#### 4.4.4 代码实践（本讲主实践）

> **实践目标**：量化 Top-k gather 的理论算力收益，并界定其适用场景。
>
> **操作步骤**：
> 1. 在 `topk_gather_kv.py` 顶部确认 `tile_n=128`、`num_threads=128`（即每线程 gather 一行），理解「k 个 token = k/128 个 n_block」。
> 2. 用本节公式手算：sk=8192、k=2048（top-25%）、\(d=64\)、\(d_v=512\)（典型 MLA 形状），写出 dense 与 topk 的 FLOPs 比值。
> 3. 列出 gather 的三项额外开销（按下标搬运、读 `gather_kv_indices`、算位图），说明为何实际加速会低于 4×。
> 4. 给出适用场景：长 KV、内容相关检索（如 MLA）、打分器廉价且准确；以及不适用场景：短序列、掩码规则可静态确定（此时块稀疏更快）。
>
> **需要观察的现象**：主干 GEMM 的 FLOPs 随 k 线性下降；gather 开销随 k 增加但占比小。
>
> **预期结果**：top-25% 时主干算力降至约 25%（下降约 75%）；考虑 gather 开销后实际端到端加速通常在 2×–4× 之间（**待本地验证**，依赖打分器质量与硬件）。
>
> **待本地验证**：若手头有 Blackwell 卡且实现了打分器，可用 k=2048 vs sk=8192 各跑一次 MLA 前向，记录耗时比与最大输出误差。

#### 4.4.5 小练习与答案

**练习 1**：给定 sk=8192、k=1024（top-12.5%），主干注意力 FLOPs 降到原来的多少？

**参考答案**：约 1024/8192 = 12.5%，即降到约 1/8、下降约 87.5%。但 gather 与打分开销的相对占比会随 k 变小而上升，k 太小时边际收益递减。

**练习 2**：如果一个掩码是「每个 query 只看固定窗口内的 KV」，应该用 Top-k gather 还是块稀疏？

**参考答案**：用块稀疏（或直接用 `window_size` 局部掩码）。因为窗口是结构化、可静态确定的，块稀疏能用 TMA 整块搬运、性能更优；Top-k gather 的散列 gather 在这里只会徒增开销而无额外灵活性。

---

## 5. 综合实践

把本讲三块内容（接口约定 → gather 三步法 → 位图精确化）串成一个端到端的「源码阅读 + 行为验证」任务：

1. **接口层**：在 `interface.py` 找到 `gather_kv_indices` 的校验（[interface.py:693-696](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L693-L696)），记录四条约束（与 `qv` 绑定、形状匹配、`%128==0`、与分页互斥）。
2. **调用层**：在 `flash_fwd_mla_sm100.py` 跟踪一次 gather：`n_block_max` 的来源（1424-1427）→ `CpasyncGatherKVManager.create`（1452-1470）→ producer 主循环里的 `load_index_topk`/`load_X`/`compute_bitmask`（1524-1566）→ `cpasync_gather_load_KV`（1709-1729）。
3. **执行层**：在 `topk_gather_kv.py` 画出一条 KV 行的路径：`mIndexTopk[row]` → `elem_pointer` → `shuffle_sync` 广播 → `cute.copy(pred=row_valid)`。
4. **精确化层**：在 `mask.py` 的 `apply_mask_sm100` rBitmask 分支（649-657）确认非法列被置 `-inf`，并在 `testing.py` 的参考实现（418-425）找到对应金标准。
5. **产出**：写一段说明，回答三个问题——(a) 为什么 gather 必须用 cp.async 而非 TMA；(b) 位图如何保证结果精确；(c) top-25% 时理论 FLOPs 下降比例与实际加速为何不同。

**预期结果**：你应能用一张图（下标张量 → 行指针 → cp.async → smem → 位图 → GEMM/softmax）把整条 Top-k gather 路径讲清楚，并能说出它与块稀疏的三处本质差异（粒度、来源、搬运方式）。

## 6. 本讲小结

- Top-k gather 是一种**加速手段而非新算法**：外部打分器给出 `gather_kv_indices`，kernel 把被选中的 `k` 个 KV token gather 成连续 tile 后照常做在线 softmax，结果精确。
- `CpasyncGatherKVManager` 用三步法完成 token 级散列 gather：`load_index_topk` 取行号 → `compute_X_ptr` 算 gmem 指针 → `load_X` 用 cp.async 逐行搬运；因为行号任意散布，**只能用 cp.async，不能用 TMA**。
- gather 之后的注意力数学与稠密路径**逐字相同**（QK/PV GEMM + 在线 softmax），这正是「gather 之后再精确注意力」的工程含义。
- 非法行（越界 / 占位 / 因果边界）由 `compute_bitmask` 生成 32 列 uint32 位图，消费侧 `apply_mask_sm100` 把对应分数置 `-inf`，使结果精确、不产生 NaN。
- 与块稀疏对比：Top-k gather 是**token 级、内容相关、动态**选取（任意散布行号）；块稀疏是**块级、结构相关、静态**选取（`tile_n` 对齐、由 `mask_mod` 导出）。前者更灵活但搬运更贵。
- 主干注意力 FLOPs 与 KV 带宽随 k 线性下降，top-25% 时理论降至约 25%（约 75% 节省）；实际端到端加速受 gather 开销与打分器质量影响，且 `is_topk_gather` 进 `compile_key` 会触发重编译。

## 7. 下一步学习建议

- **回到 MLA 全景**：本讲的 gather 只在 MLA 吸收前向落地。建议重读 [u10-l2 MLA](u10-l2-mla.md)，把 gather 路径放回 MLA 的三段 MMA（`S=Q@K^T`、`S+=Q_v@V^T`、`O=P@V`）里理解其位置。
- **对比两种长 KV 方案**：把本讲与 [u7-l2 SplitKV](u7-l2-splitkv-and-combine.md) 对照——SplitKV 是「把 KV 切给多个 SM 并行」，Top-k gather 是「只算一部分 KV」，两者可视为长上下文减负的互补思路。
- **深入散列搬运**：`topk_gather_kv.py` 的 `load_X` 与 [u7-l3 分页 KV](u7-l3-paged-kv.md) 的 `PagedKVManager` 是同一类「不连续→连续」gather 的两种实例，对比阅读能巩固 cp.async 散列 gather 的通用写法。
- **掩码体系收尾**：位图（本讲）、R2P 位图（[u3-l1](u3-l1-attention-mask.md)）、块稀疏三态（[u10-l1](u10-l1-block-sparsity.md)）共同构成 FA4 的掩码家族，建议画一张总图归纳它们各自的粒度与适用场景。
