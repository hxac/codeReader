# Tile Scheduler 与 CLC 动态持久化调度

## 1. 本讲目标

本讲接续 u8-l1（Blackwell 前向 Kernel 全景）。在 u8-l1 中我们看到 Blackwell 前向 kernel 是一个 **persistent kernel**：每个 CTA 不会「算完一块就退出」，而是在一个 `while work_tile.is_valid_tile` 的循环里不断向调度器「领活」，直到所有工作块算完。

那么——**「下一个工作块到底是谁」由谁决定？** 这正是 `tile_scheduler.py` 要回答的问题。学完本讲，你应当能够：

- 说清 `WorkTileInfo` 四元组 `(block, head, batch, split)` 的含义，以及所有调度器共用的 `TileSchedulerProtocol` 契约；
- 区分 `SchedulingMode` 的四种取值，并指出当前真正落地的只有 `STATIC` 与 `CLC` 两种；
- 讲明白三种静态调度策略：**单 tile**（每 CTA 一块）、**persistent**（grid-stride 循环）、**varlen**（变长序列的 warp 前缀和坐标映射），以及 `SingleTileLPTScheduler` 里的 L2 swizzle + LPT（Longest-Processing-Time-first）块重排；
- 理解 Blackwell 的 **CLC（Cluster Launch Control）硬件动态调度** 如何由 `ClcState` 承载，以及 `clc_work_to_coords` 如何把硬件返回的 work tile 映射成逻辑坐标；
- 在负载不均的场景下，推理为何 CLC 动态调度能比静态调度降低尾延迟。

## 2. 前置知识

阅读本讲前，请确保已理解以下概念（均在前面讲义中建立）：

- **persistent kernel**：CTA 不随 grid 退出，而是循环领活直到工作耗尽（u8-l1）。
- **work tile / 块坐标**：注意力被切成 `(m_block, head, batch, split)` 四个轴上的工作块，`m_block` 是 Q 序列方向的下标（u3-l2 的 BlockInfo）。
- **causal / local 掩码带来的工作不均**：因果掩码下，靠近对角线（`q_idx` 大）的 Q tile 要遍历更多 KV 块，远端（`q_idx` 小）的 Q tile 只遍历很少 KV 块——不同 work tile 的实际计算量天差地别（u3-l1、u6-l1）。
- **varlen / cu_seqlens**：一个 batch 内序列不等长，每条序列的 `m_block` 数也不同（u3-l3）。
- **warp / warp-group / cluster**：GPU 的线程组织层级，cluster 是 Blackwell 上多个 CTA 的协作组（u6-l2、u8-l1）。
- **producer / consumer 流水**：一方负责「领活/搬运」，一方负责「计算」，靠 mbarrier 握手（u5-l1、u5-l3）。

一个贯穿全讲的直觉：**调度器就是 persistent kernel 里的「任务队列接口」**。静态调度器的「队列」在 launch 时刻就用公式算死了（每个 CTA 领哪些块是确定的）；CLC 调度器的「队列」则在运行时由一块专用硬件动态分发（谁先干完谁就去领下一块）。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| `flash_attn/cute/tile_scheduler.py` | 全部调度器的定义：`SchedulingMode` 枚举、`WorkTileInfo`、`TileSchedulerProtocol` 协议、`ClcState`，以及单 tile / persistent / LPT / varlen / FMHA-CLC 五类调度器。 |
| `flash_attn/cute/flash_fwd_sm100.py` | Blackwell 前向 kernel，**消费**调度器：在 `__init__` 里选调度器类、构造 `ClcState`，在主循环里调用 `initial_work_tile_info / advance_to_next_work`。 |
| `flash_attn/cute/interface.py` | 公共 API 层，决定是否请求 CLC（`use_clc_scheduler` 的门控逻辑）。 |
| `flash_attn/cute/utils.py` | `FA_CLC` 环境变量读取，提供 `_get_use_clc_scheduler_default()`。 |
| `AI/CLC_TRACE_DEBUG.md` | 如何用 `FA_LOG_LEVEL=3 FA_CLC=1` 抓取 CLC 调度器逐次查询的 trace。 |

## 4. 核心概念与源码讲解

### 4.1 调度器统一契约：WorkTileInfo、TileSchedulerProtocol 与 SchedulingMode 四种模式

#### 4.1.1 概念说明

不管底层是静态公式还是 CLC 硬件，persistent kernel 的主循环长得都一样：

```python
work_tile = tile_scheduler.initial_work_tile_info()
while work_tile.is_valid_tile:
    m_block, head_idx, batch_idx, split_idx = work_tile.tile_idx
    # ... 算这一块 ...
    work_tile = tile_scheduler.advance_to_next_work()
```

为了让这一段循环对**任何**调度器都能原样复用，FA4 抽象出一个统一契约，由三件套构成：

1. **`WorkTileInfo`**——一块「工作」的载体。FA4 把它扩成四个轴 `(block, head, batch, split)`，比上游 cutlass 的版本多一维，用以统一表达 SplitKV 的 split 轴。
2. **`TileSchedulerProtocol`**——所有调度器必须实现的五个方法，规定了「领第一块 / 取当前块 / 前进到下一块 / 预取下一块 / 收尾」的接口。
3. **`SchedulingMode`**——一个枚举，标注「这块调度器实例用哪种分发策略」，驱动 kernel 内部 `const_expr` 分支的编译期裁剪。

#### 4.1.2 核心流程

调度器在每个 CTA 上的生命周期分为**两套动作**，对应流水的两端：

- **consumer 侧（真正算活的那批 warp，如 MMA warp）**：
  1. `initial_work_tile_info()` 取第一块；
  2. `while is_valid_tile`：算当前块 → `advance_to_next_work()` 取下一块。
- **producer 侧（专门领活的那一个 warp，仅 CLC 需要）**：
  1. `initial_work_tile_info()`；
  2. `while is_valid_tile`：`prefetch_next_work()` 向硬件发查询 → `advance_to_next_work()` 等结果；
  3. `producer_tail()` 收尾。

对静态调度器，producer 侧的 `prefetch_next_work` / `producer_tail` 是**空操作**（no-op）——因为没有「预取」这种事，下一块坐标是个纯算术公式。这也是协议把 producer 方法设计成「可选」的原因。

四种 `SchedulingMode` 的名义含义：

| 取值 | 名义含义 | 当前是否落地 |
| --- | --- | --- |
| `NONE` | 不调度 | ❌ 全代码无引用，预留 |
| `STATIC` | 启动时刻公式静态分配 | ✅ 所有调度器都支持 |
| `DYNAMIC` | 软件 work-stealing 动态分配 | ❌ 全代码无引用，预留 |
| `CLC` | 硬件（Blackwell CLC 单元）动态分配 | ✅ 仅 `SingleTileLPTScheduler` / `SingleTileVarlenScheduler` 支持 |

⚠️ **重要事实**：`DYNAMIC` 与 `NONE` 在枚举里存在，但全仓库**没有任何调度器实现它们**——每个调度器的 `to_underlying_arguments` 里的断言只允许 `STATIC`，或「只允许 `STATIC` 与 `CLC`」。所以 `SchedulingMode` 实际只有 **STATIC** 与 **CLC** 两条活路径，`DYNAMIC` 可视为「软件动态调度」的占位。这一点务必记牢，不要误以为存在第三种运行模式。

#### 4.1.3 源码精读

**`SchedulingMode` 枚举**——四种模式的定义：

[flash_attn/cute/tile_scheduler.py:33-37](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L33-L37) — 用 `IntEnum + auto()` 定义 `NONE/STATIC/DYNAMIC/CLC`。它是 `cutlass.Constexpr[SchedulingMode]` 的取值，进 `compile_key`，换模式即重编译。

**`WorkTileInfo`**——四轴工作块，重写了 MLIR 序列化以承接 5 个值（4 坐标 + 1 有效位）：

[flash_attn/cute/tile_scheduler.py:94-102](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L94-L102) — 继承上游 `cutlass.utils.WorkTileInfo`，把 tile_idx 扩成 `(block, head, batch, split)` 四元组。`__new_from_mlir_values__` 里 `assert len(values) == 5` 正是对应「4 坐标 + 1 有效标志」。

**`TileSchedulerProtocol`**——统一契约，文档串讲了调度器的两大职责：

[flash_attn/cute/tile_scheduler.py:105-143](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L105-L143) — 协议列出五个方法。文档字符串点明两件事：①坐标映射（linear tile index → `(m_block, head, batch, split)`）；②工作分发方式（静态 grid-stride vs CLC 动态）。注意 `advance_to_next_work` 的 docstring 区分了两条路径：静态走「grid-stride + get_current_work」，CLC 走「consumer_wait + get_current_work + consumer_release」。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「同一段主循环」对静态与 CLC 两种调度器都成立。

**步骤**：
1. 打开 [flash_attn/cute/flash_fwd_sm100.py:1361-1362](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1361-L1362)，这是 consumer（MMA）侧的主循环起点：`work_tile = tile_scheduler.initial_work_tile_info()` → `while work_tile.is_valid_tile`。
2. 跳到 [flash_attn/cute/flash_fwd_sm100.py:1535](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1535) 看循环尾：`work_tile = tile_scheduler.advance_to_next_work()`。
3. 再看 producer 侧 [flash_attn/cute/flash_fwd_sm100.py:2942-2958](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2942-L2958)：`clc_scheduler_warp` 里多了 `prefetch_next_work()` 与收尾的 `producer_tail()`。

**观察现象**：consumer 侧循环完全不关心是静态还是 CLC——差异都被封装进 `advance_to_next_work` 内部的 `const_expr(scheduling_mode == CLC)` 分支。

**预期结果**：你会确认两套循环骨架一致，只是 producer 侧多了预取与收尾。**待本地验证**：在真实 Blackwell 卡上设 `FA_LOG_LEVEL=1`，分别用 `FA_CLC=0` 与 `FA_CLC=1` 跑同一个小形状，观察 host 日志里 `TileScheduler=...` 与 `scheduling_mode=...` 的变化。

#### 4.1.5 小练习与答案

**Q1**：为什么 `WorkTileInfo` 要把坐标扩成四元组，而不是上游 cutlass 默认的三元组？

**参考答案**：因为 FA4 的前向/反向要把 **SplitKV 的 split 轴** 也当作一个独立的工作维度（见 u7-l2）。一个 `(block, head, batch)` 还要再切 `num_splits` 份分别交给不同 CTA，所以需要第四个坐标 `split_idx` 来定位。`SingleTileScheduler.get_current_work` 里 `head_idx, split_idx = divmod(head_idx, num_splits)` 正是把 split 折叠进 head 维后再拆回来。

**Q2**：如果我把 `scheduling_mode` 从 `STATIC` 改成 `DYNAMIC`，会发生什么？

**参考答案**：直接断言失败。没有任何调度器的 `to_underlying_arguments` 接受 `DYNAMIC`——例如 `SingleTileScheduler` 断言 `scheduling_mode == STATIC`、`SingleTileLPTScheduler` 断言 `scheduling_mode in (STATIC, CLC)`。`DYNAMIC` 当前只是枚举里的占位，没有实现。

---

### 4.2 静态调度三态：单 tile / persistent / varlen（含 LPT 与 L2 swizzle）

#### 4.2.1 概念说明

静态调度，意思是「**每个 CTA 该算哪些块，在 kernel 启动那一刻就由一个确定性公式算死了**」，运行时不再变动。FA4 里有四类静态调度器，差别在于「**怎么把一个线性编号 `tile_idx` 映射成 `(block, head, batch, split)`，以及 grid 怎么开**」：

1. **`SingleTileScheduler`**——非持久。grid 直接开成 `(num_block, num_head*num_splits, num_batch)`，**一个 CTA 恰好算一块**，算完即退。最简单。
2. **`StaticPersistentTileScheduler`**——持久。grid 只开到 SM 数量级，每个 CTA 用 **grid-stride 循环** 连续领多块。
3. **`SingleTileLPTScheduler`（STATIC 路径）**——生产环境前向主力。持久 + **L2 swizzle**（让连续若干块共享同一批 KV，命中 L2）+ **LPT**（把「重」的块排在前面以均衡负载）。用于 causal / local 场景。
4. **`SingleTileVarlenScheduler`（STATIC 路径）**——变长。因为每条序列的 `m_block` 数不同，没法用一个简单的 divmod 公式，得用 **warp 内前缀和** 在运行时把线性编号解开成 `(block, head, batch)`。

#### 4.2.2 核心流程

**单 tile（`SingleTileScheduler`）**：grid 维度直接编码坐标，`get_current_work` 从 `block_idx` 反解：

```
grid = (num_block, num_head * num_splits, num_batch)
block_idx, head_idx, batch_idx = blockIdx.xyz        # 直接就是坐标
if is_split_kv: head_idx, split_idx = divmod(head_idx, num_splits)
advance_to_next_work → 置 _is_first_block=False → is_valid_tile 变 False（一块就停）
```

**持久 grid-stride（`StaticPersistentTileScheduler`）**：grid 只开到 SM 数，循环跨步：

```
grid = min(SM_count, total_blocks_cluster)
tile_idx = blockIdx.x
loop:
    hn_idx, block_idx = divmod(tile_idx, num_block_cluster_divmod)
    batch_idx, head_idx = divmod(hn_idx, num_head_divmod)
    is_valid = tile_idx < total_blocks_cluster
    tile_idx += grid_dim.x        # grid-stride 跨步领下一块
```

**LPT + L2 swizzle（`SingleTileLPTScheduler` STATIC 路径）**：核心是「**把 `swizzle` 个 head 绑成一组**，让连续若干工作块都访问同一批 KV，从而把 KV 稳稳留在 L2」：

\[
\text{size\_one\_head} = \text{seqlen\_k} \times (\text{headdim}+\text{headdim\_v}) \times \text{element\_size}
\]

\[
\text{swizzle} = 2^{\lfloor \log_2(\text{L2\_budget} / \text{size\_one\_head}) \rfloor}
\]

其中 `L2_budget` 取 `50 * 1024 * 1024`（约 50 MiB，预留给 K/V 的 L2 容量）。`swizzle` 就是「L2 里能同时放下几个 head 的 KV」，取 2 的幂以便用快速 divmod。然后线性编号被切成若干「段（section）」，每段含 `swizzle * num_block` 个块，段内才遍历 block；最后一段（residual）可能不满，用单独的 divmod 处理。LPT 则把 block 下标**翻转**（`block = num_block - 1 - block`），让靠近对角线的「重块」先算，使各 CTA 负载更均衡。

**变长（`SingleTileVarlenScheduler` STATIC 路径）**：序列不等长，每条 batch 的 `m_block` 数 `ceil_div(seqlen_b, tile_m)` 不同，无法用闭式 divmod。解法是 warp 协作：32 个 lane 各自算出一个 batch 的 `num_m_blocks`，做 **warp 内前缀和**，再二分定位 `tile_idx` 落在哪条 batch、哪个 block、哪个 head。这部分逻辑在 `_varlen_coord_map` 里，是本文件最精巧的一段。

#### 4.2.3 源码精读

**`SingleTileScheduler`——非持久，一 CTA 一块**：

[flash_attn/cute/tile_scheduler.py:227-245](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L227-L245) — `get_grid_shape` 把 grid 直接开成 `(num_block, num_head*num_splits, num_batch)`，即「grid 维度本身就是坐标」。

[flash_attn/cute/tile_scheduler.py:247-256](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L247-L256) — `get_current_work` 把 `block_idx/head_idx/batch_idx` 直接打包进 `WorkTileInfo`；若 `is_split_kv`，用 `divmod` 从 head 维拆出 `split_idx`。`_is_first_block` 初值 `True`，所以第一块必然有效。

[flash_attn/cute/tile_scheduler.py:264-266](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L264-L266) — `advance_to_next_work` 只是把 `_is_first_block=False`，于是 `is_valid_tile` 变假——**一个 CTA 只算一块**。

**`StaticPersistentTileScheduler`——持久，grid-stride**：

[flash_attn/cute/tile_scheduler.py:337-348](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L337-L348) — `get_grid_shape` 查询硬件 SM 数，grid 开成 `min(SM_count // cluster_m * cluster_m, total_blocks_cluster * cluster_m)`，即「CTA 数被 SM 数封顶」——这是 persistent 的本质。

[flash_attn/cute/tile_scheduler.py:350-356](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L350-L356) — `get_current_work`：两次 `divmod` 把线性 `tile_idx` 解成 `(block, head, batch)`，`is_valid = tile_idx < total_blocks_cluster`。

[flash_attn/cute/tile_scheduler.py:364-369](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L364-L369) — `advance_to_next_work`：`tile_idx += grid_dim`（或 `cluster_dim`），这就是 **grid-stride**——每个 CTA 跨步领下一块，直到 `tile_idx` 越界。

**`SingleTileLPTScheduler`——L2 swizzle + LPT 的参数推导**：

[flash_attn/cute/tile_scheduler.py:428-455](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L428-L455) — `Params.create` 推导 swizzle：`size_one_kv_head = seqlen_k*(headdim+headdim_v)*element_size`（注意这里用 `Int64` 防止长序列下溢出），`swizzle = 1 << log2_floor(L2_budget // size_one_head)` 取 2 的幂；再算 `num_hb_quotient`（完整段数）与 `num_hb_remainder`（尾段残余），后者单独构造一个 divmod 以免除零。

[flash_attn/cute/tile_scheduler.py:573-598](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L573-L598) — STATIC 路径的 `get_current_work`：先用 `l2_major_divmod` 把 `tile_idx` 切成 `(bidhb, l2_mod)`，再按「是否进入尾段」选择 `l2_minor_divmod` 或 `l2_minor_residual_divmod`，重组出真实的 `bidhb_actual`，最后 `divmod(num_head)` 得 `(batch, head)`。LPT 翻转体现在 `block = num_block - 1 - block`。

**`SingleTileVarlenScheduler`——变长的 warp 前缀和坐标映射**：

[flash_attn/cute/tile_scheduler.py:909-923](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L909-L923) — `get_grid_shape`：因为序列不等长，grid 只能按「最坏情况」开——`total_blocks_max` 用 `total_q`（所有序列 Q token 总数）除以 `tile_m` 估算，再 round down 到 cluster 倍数。

[flash_attn/cute/tile_scheduler.py:925-946](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L925-L946) — `_get_num_m_blocks`：每个 lane 负责一个 batch，读出该序列长度（从 `mSeqUsedQ` 或 `mCuSeqlensQ`），算出它的 `m_block` 数。这是前缀和的「每格初值」。

[flash_attn/cute/tile_scheduler.py:948-1046](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L948-L1046) — `_varlen_coord_map`：用 `utils.warp_prefix_sum` 累加 32 个 batch 的 `num_m_blocks`，配合 `cute.arch.shuffle_sync` 在 warp 内广播、`vote_ballot_sync + popc` 做并行二分，最终把线性 `tile_idx` 落到正确的 `(block, head, batch)`。

#### 4.2.4 代码实践（手算 + 阅读型）

**目标**：手算 `SingleTileLPTScheduler` 在小例子下的 LPT 翻转效果，并对照源码。

**步骤**：
1. 设 `num_block = 4`，`num_head = 2`，`num_batch = 1`，causal（`lpt = True`）。设 L2 足够大使得 `swizzle = num_head = 2`（一段装下所有 head）。
2. 在纸上写出 `total_blocks = 4 * 2 * 1 = 8`。STATIC 路径下 `get_grid_shape = (8, num_splits=1, 1)`，即 8 个 CTA，`tile_idx = 0..7`。
3. 对每个 `tile_idx`，按 [flash_attn/cute/tile_scheduler.py:573-598](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L573-L598) 的公式推 `block / head / batch`，注意 LPT 翻转 `block = 4 - 1 - block`。

**预期结果**：你会看到 `tile_idx` 递增时，`block` 在每个 head 内部是从大到小（3,2,1,0）排列的——这就是「重块（大 block，靠近因果对角线）先算」。**待本地验证**：可选地写一段纯 Python 复现这段 divmod 链，打印 `(tile_idx → block, head, batch)` 表与手算对照。

#### 4.2.5 小练习与答案

**Q1**：`StaticPersistentTileScheduler` 与 `SingleTileScheduler` 都不涉及 CLC，为什么 Blackwell 前向还要同时保留两个？

**参考答案**：二者 grid 开法不同，适配不同场景。`SingleTileScheduler` 非持久，grid 等于总块数，CTA 算一块就退——适合总块数不多、不值得持久化的情况；`StaticPersistentTileScheduler` 持久，grid 被 SM 数封顶，CTA 用 grid-stride 连续领多块——适合总块数远大于 SM 数、需要减少 launch/退出开销的情况。选择逻辑见 [flash_attn/cute/flash_fwd_sm100.py:243-250](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L243-L250)。

**Q2**：L2 swizzle 里为什么 `swizzle` 要取 2 的幂？

**参考答案**：两个原因：①取 2 的幂后 `l2_minor_divmod` 等可退化成位运算（`FastDivmodDivisor` 对 2 的幂有快路径），省掉除法；②注释里写「Seems faster if swizzle is a power of 2」——经验上 2 的幂分段对 L2 命中与调度对齐更友好。

---

### 4.3 CLC 动态调度与 ClcState：硬件 work tile 到逻辑坐标的映射

#### 4.3.1 概念说明

**CLC（Cluster Launch Control）** 是 Blackwell 引入的一块**硬件任务分发单元**。与静态调度「启动时算死谁算哪块」不同，CLC 在运行时维护一个全局原子计数器：每个 CTA 干完当前块后，向 CLC 硬件「查询」下一块，硬件把一个尚未被领取的 work tile 编号原子地返回给它。

这带来的本质变化是 **工作分发从「预先分配」变成「按需领取」**——类似于一个硬件实现的 work-stealing 队列。`ClcState` 就是承载这套硬件交互状态的对象：它包住了上游 cutlass 的 `ClcDynamicPersistentTileScheduler`（硬件调度器）、`PipelineClcFetchAsync`（异步流水）、以及 producer/consumer 的 pipeline state。

FA4 里支持 CLC 的是两个调度器：`SingleTileLPTScheduler` 与 `SingleTileVarlenScheduler`（以及 FMHA 专用的 `Sm100FmhaClcDynamicTileScheduler`）。它们的共同模式是：**静态路径用公式算坐标，CLC 路径用 `clc_work_to_coords` 把硬件返回的原始坐标翻译成逻辑坐标**。

#### 4.3.2 核心流程

CLC 是一个 producer/consumer 流水，关键在于「**硬件返回的坐标**」与「**FA4 主循环期望的逻辑坐标 `(block, head, batch, split)`**」之间的翻译：

```
# producer warp（clc_scheduler_warp）：
work = tile_scheduler.initial_work_tile_info()
while work.is_valid_tile:
    tile_scheduler.prefetch_next_work()   # 向 CLC 硬件发下一次查询
    work = tile_scheduler.advance_to_next_work()  # consumer_wait 等结果
tile_scheduler.producer_tail()

# consumer warp（MMA）：
work = tile_scheduler.initial_work_tile_info()
while work.is_valid_tile:
    (block, head, batch, split) = work.tile_idx   # 已经是逻辑坐标
    ... 算 ...
    work = tile_scheduler.advance_to_next_work()   # consumer_wait + release
```

`ClcState.prefetch_next_work` 的内部动作（[tile_scheduler.py:77-81](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L77-L81)）：

1. `producer_acquire` 取得一个流水槽；
2. `producer_get_barrier` 拿到对应的 mbarrier 地址；
3. `hw_scheduler.advance_to_next_work(mbarrier_addr)` 让硬件把下一个 work tile 写进响应缓冲，完成时通过该 mbarrier 通知；
4. `producer_state.advance` 推进流水相位。

`SingleTileLPTScheduler.clc_work_to_coords` 的翻译规则（[tile_scheduler.py:543-571](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L543-L571)）：CLC 返回的是**原始 grid 坐标（无 L2 swizzle，顺序由硬件决定）**，FA4 只需做三件事——①按 `cluster_shape_m` 除出块号；②可选的 LPT 块翻转；③若 `is_split_kv`，把 batch 维上的 `split` 拆回来。注意它**不**重复静态路径那套 L2 swizzle divmod——因为硬件已经替你决定遍历顺序了。

#### 4.3.3 源码精读

**`ClcState` 数据类——承载 CLC 运行时状态**：

[flash_attn/cute/tile_scheduler.py:40-92](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L40-L92) — 类文档说得很清楚：`FlashAttentionForwardSm100` 负责构造它（因为它拥有 CLC 响应缓冲、mbarrier 存储、launch 几何），各 tile scheduler 再消费它、把硬件 work tile 映射成自己的逻辑 `WorkTileInfo`。字段四个：`_hw_scheduler`（硬件调度器）、`_pipeline`（异步流水）、`_consumer_state` / `_producer_state`（流水相位）。

[flash_attn/cute/tile_scheduler.py:61-91](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L61-L91) — `create` 工厂与五个方法。`prefetch_next_work`（producer 侧预取）、`consumer_wait/consumer_release`（consumer 侧等/释放）、`producer_tail`（收尾）。这些都只是转调上游 cutlass 的 pipeline / hw_scheduler。

**`SingleTileLPTScheduler` 的 CLC 路径**：

[flash_attn/cute/tile_scheduler.py:494-518](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L494-L518) — `_clc_grid_shape` 与 `clc_problem_shape`：CLC 模式下 grid 不再是 `(total_blocks, num_splits, 1)`，而是 `(num_block*cluster_m, num_head, num_batch_splits)`；`clc_problem_shape` 把它打包成上游 `ClcDynamicPersistentTileSchedulerParams`，用来初始化硬件调度器。

[flash_attn/cute/tile_scheduler.py:543-571](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L543-L571) — `clc_work_to_coords`：硬件→逻辑的翻译。注意注释「CLC returns raw grid coordinates — no L2 swizzle (hardware decides order). We only apply cluster division, optional LPT block reversal, and split_kv unpacking.」——这是 CLC 与静态路径最关键的差异：**遍历顺序交给硬件**。

[flash_attn/cute/tile_scheduler.py:573-598](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L573-L598) — `get_current_work` 顶部的 `const_expr(scheduling_mode == CLC)` 分支：CLC 时调 `self.clc.get_current_work()` 拿硬件结果，再 `clc_work_to_coords` 翻译；否则走静态 L2-swizzle 公式。

[flash_attn/cute/tile_scheduler.py:612-624](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L612-L624) — `advance_to_next_work` 与 `producer_tail`：CLC 时分别是 `consumer_wait + get_current_work + consumer_release` 与 `clc.producer_tail`；静态时分别是「置 `tile_idx = total_blocks` 使其失效」与空操作。

**`SingleTileVarlenScheduler` 的 CLC 路径**：

[flash_attn/cute/tile_scheduler.py:1048-1064](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L1048-L1064) — CLC 时先读硬件返回的 `tile_idx`，**无效则置成 `grid_dim`**（让随后的 `_varlen_coord_map` 判 `is_valid=False`），因为「CLC 的 `tile_idx` 在无效时是垃圾值，不能信」——注释点明这是个必须用 local-then-assign 规避的 CuTe DSL 结构性陷阱。然后仍交 `_varlen_coord_map` 解码，复用同一套变长坐标映射。

**FMHA 专用的 `Sm100FmhaClcDynamicTileScheduler`**（独立于上面的通用路径）：

[flash_attn/cute/tile_scheduler.py:1573-1590](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/tile_scheduler.py#L1573-L1590) — `work_tile_info_from_clc_response`：直接调 `cute.arch.clc_response(result_addr)` 读硬件返回的 `(m_idx, n_idx, l_idx, vld)`，再把 L 维（`l_idx`）解成 `(bid, hid)`。这是给 FMHA `(M,B,H)` 问题形状用的较早期 CLC 实现，与 `SingleTileLPTScheduler` 的 CLC 路径并存。

**CLC 的接线（在 kernel 里）**：

[flash_attn/cute/flash_fwd_sm100.py:1092-1131](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1092-L1131) — 用 `const_expr(self.use_clc_scheduler)` 包起整段 CLC 构造：建立 `ClcDynamicPersistentTileScheduler`、`PipelineClcFetchAsync`（`tx_count=16`、producer/consumer group 按 warp 数配）、producer/consumer state，组装成 `ClcState`，再 `tile_scheduler_cls.create(tile_sched_params, clc=clc)`。非 CLC 分支则不带 `clc` 参数创建。

[flash_attn/cute/flash_fwd_sm100.py:1137-1147](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1137-L1147) — CLC 时把一个专门的 warp（`clc_scheduler_warp_id`）派去当 producer 跑 `clc_scheduler_warp`，其余空 warp 跑 `empty_warp`；非 CLC 时空 warp 只调寄存器配置、不做调度活。

**CLC 的启用门控（在公共 API 层）**：

[flash_attn/cute/interface.py:625-630](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L625-L630) — 即使 `FA_CLC=1` 请求了 CLC，仍会在两类场景**强制回退**：varlen MHA（负载不均使更多 KV 块在飞，反而伤 L2）、dense noncausal（基本只付出 work-stealing 开销却没收益）。注释原文：「CLC regressed for varlen MHA and dense noncausal.」

[flash_attn/cute/utils.py:66](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L66) 与 [flash_attn/cute/utils.py:91-92](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/utils.py#L91-L92) — `FA_CLC` 环境变量默认 `"0"`（关），`_get_use_clc_scheduler_default()` 直接返回它。

#### 4.3.4 代码实践（推理 + trace 对照）

**目标**：解释「变长序列 + 因果掩码」下 CLC 动态调度为何能减少尾延迟，并对照 `AI/CLC_TRACE_DEBUG.md`。

**推理过程**（请按这个思路自己写一遍）：

1. **静态调度的尾延迟来源**：在 `StaticPersistentTileScheduler` 或 `SingleTileLPTScheduler`(STATIC) 下，每个 CTA 在启动时就被公式分配了一组 work tile（grid-stride 或 L2-swizzle 序列）。但因果掩码下，不同 `block` 的计算量差异巨大——靠近对角线的 block 要遍历 ~全部 KV，远端 block 只遍历很少 KV。即使 LPT 用块翻转做了一定均衡，**每个 CTA 拿到的总工作量仍不可能完全相等**。于是总耗时由「最忙的那个 CTA」决定（短板效应），先干完的 CTA 空转等它——这就是**尾延迟（tail latency）**。形式地，设 CTA \(i\) 分到的总工作量为 \(W_i\)，则墙钟时间 \(\approx \max_i W_i / \text{吞吐}\)，而非理想的 \((\sum_i W_i)/(\text{N\_CTA}\cdot\text{吞吐})\)。

2. **CLC 为何更优**：CLC 把分发变成**运行时按需领取**。CTA 干完一块立刻向硬件要下一块，硬件用一个全局原子计数器保证每块恰好被领一次。于是没有任何 CTA 被「预先钉死」在一组重块上——重块会被先空出来的 CTA 及时消化。理想情况下墙钟时间趋近 \((\sum_i W_i)/(\text{N\_CTA}\cdot\text{吞吐})\)，即**均值而非最大值**，尾延迟被显著削平。这正是 work-stealing 类算法相对静态分配的核心收益。

3. **为何并非万能**：CLC 的硬件查询本身有开销，且动态顺序会打破静态 L2 swizzle 精心安排的「连续块共享 KV」——所以 interface.py 才会在 varlen MHA / dense noncausal 下回退（上面已引用）。

**对照 trace**：按 [AI/CLC_TRACE_DEBUG.md](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/CLC_TRACE_DEBUG.md) 抓一段 trace：

```bash
FA_LOG_LEVEL=3 FA_CLC=1 CUDA_VISIBLE_DEVICES=0 python your_repro.py > clc_trace.log 2>&1
python AI/parse_clc_log.py clc_trace.log
```

**需要观察的现象**：
- host 日志出现 `scheduling_mode=CLC`、`TileScheduler=SingleTileLPTScheduler`（确认真的走了 CLC 路径）；
- 大量 `[CLC] query sm=<smid> cta=<blockIdx.x> (m_blk=...,h=...,b=...,s=...) valid=1` 行——你会看到**同一个 `cta`（物理 blockIdx）在不同时刻领到了不同的 `m_blk`**，这正是「按需领取」的直接证据；
- 工作算完后出现连续的 `valid=0`（调度器耗尽）。

**预期结果**：trace 直观证明「物理 CTA 与逻辑 work tile 的绑定是运行时动态变化的」，与静态调度「blockIdx 即坐标」形成鲜明对比。**待本地验证**：本环境若无 Blackwell GPU，则只能阅读源码与文档完成推理，无法实跑 trace。

#### 4.3.5 小练习与答案

**Q1**：`SingleTileLPTScheduler` 的 CLC 路径为何不再做 L2 swizzle，而 STATIC 路径要做？

**参考答案**：STATIC 路径需要靠公式主动安排「连续若干块访问同一批 KV」来命中 L2，因为没人帮你决定顺序；CLC 路径下，遍历顺序由硬件调度器动态决定（`clc_work_to_coords` 的注释明说「hardware decides order」），软件再叠一层 swizzle 既无意义也无法保证生效。所以 CLC 路径只做「cluster 除法 + LPT 翻转 + split 拆包」三件最小的事。

**Q2**：`SingleTileVarlenScheduler` 在 CLC 失效时，为什么要把 `tile_idx` 置成 `grid_dim` 而不是直接信硬件返回值？

**参考答案**：因为硬件在 work tile 无效时返回的 `tile_idx` 是垃圾值，若直接拿去喂 `_varlen_coord_map`，可能解出一个看似合法的假坐标。置成 `grid_dim`（一个必然越界的值）能保证 `_varlen_coord_map` 走 `batch_idx >= num_batch` 分支返回 `is_valid=False`，安全地结束循环。源码注释还指出这是为了规避 CuTe DSL 在 `self` 上做 runtime `if` 的结构性陷阱，故采用 local-then-assign 写法。

**Q3**：为什么 `FA_CLC=1` 不保证一定走 CLC？

**参考答案**：`FA_CLC` 只是「请求」。interface.py 还有多重门控：varlen MHA 与 dense noncausal 强制回退（[interface.py:625-630](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L625-L630)）；kernel 内部还要求 `use_tma_KV` 且不与 `overlap_sO_sQ` 冲突（[flash_fwd_sm100.py:228-232](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L228-L232)）；且 CLC 需要 Blackwell 硬件。任一不满足都会静默退回 STATIC。这也是 `CLC_TRACE_DEBUG.md` 强调「keep the host log」来确认 `scheduling_mode=CLC` 的原因。

## 5. 综合实践

**任务**：为 Blackwell 前向 kernel 的调度器选择画一张「决策树 + 数据流」综合图，把本讲三块内容串起来。

要求：
1. **决策树**：从「是否 varlen？是否 causal/local？是否 persistent？是否请求 CLC 且通过门控？」出发，对照 [flash_attn/cute/flash_fwd_sm100.py:243-252](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L243-L252) 的选择逻辑，画出落到 `SingleTileVarlenScheduler / SingleTileLPTScheduler / StaticPersistentTileScheduler / SingleTileScheduler` 中的哪一个，并标注对应的 `scheduling_mode`（STATIC 或 CLC）。
2. **数据流**：选定一个调度器后，画出 producer warp 与 consumer warp 各自调用协议五方法（`initial_work_tile_info / prefetch_next_work / advance_to_next_work / producer_tail`）的时序，并标出 `WorkTileInfo (block, head, batch, split)` 在两者之间的传递路径（注意 CLC 下 producer 预取的结果经 mbarrier 传给 consumer）。
3. **门控标注**：在图上用虚线框标出 interface.py 与 kernel 里所有可能让 CLC 回退到 STATIC 的条件。

**验收**：能用这张图向别人讲清楚「同一个 `flash_attn_func` 调用，为什么换一个形状就可能从 CLC 静悄悄退回 STATIC」。**待本地验证**：若手头有 Blackwell 卡，用 `FA_LOG_LEVEL=1` 实际跑几个形状（dense noncausal、causal、varlen），核对 host 日志里的 `TileScheduler=...` 与 `scheduling_mode=...` 是否与你的决策树一致。

## 6. 本讲小结

- 所有调度器实现同一个 `TileSchedulerProtocol` 契约（五方法），使 persistent 主循环 `while work_tile.is_valid_tile` 对静态与 CLC 都通用；工作块统一用四轴 `WorkTileInfo (block, head, batch, split)`。
- `SchedulingMode` 有四种取值，但**当前只有 STATIC 与 CLC 真正落地**；`DYNAMIC` / `NONE` 仅为预留枚举位，无任何调度器实现它们。
- 静态调度有三态：`SingleTileScheduler`（一 CTA 一块，非持久）、`StaticPersistentTileScheduler`（持久 grid-stride）、`SingleTileVarlenScheduler`（变长，靠 warp 前缀和解坐标）；生产主力 `SingleTileLPTScheduler` 在 STATIC 路径上叠加 **L2 swizzle**（连续块共享 KV 命中 L2）与 **LPT 块翻转**（重块先算均衡负载）。
- CLC 是 Blackwell 硬件动态分发单元，由 `ClcState` 承载（包住硬件调度器 + 异步流水 + producer/consumer state）；`clc_work_to_coords` 把硬件返回的原始 grid 坐标翻译成逻辑 `(block, head, batch, split)`，且**不再做 L2 swizzle**（顺序交硬件决定）。
- CLC 把工作分发从「预先分配」变成「按需领取」，在因果/变长等负载不均场景下把墙钟时间从 \(\max_i W_i\) 拉向均值，从而削平尾延迟；但 varlen MHA 与 dense noncausal 会因伤 L2 或徒增开销而被 interface.py 强制回退 STATIC。

## 7. 下一步学习建议

- **继续读调度器的调用方**：回到 [flash_attn/cute/flash_fwd_sm100.py:1344-1535](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1344-L1535) 的 `load` 与 `mma` 主循环，看 consumer 拿到 `work_tile.tile_idx` 后如何驱动 BlockInfo、SeqlenInfo、SplitKV，把本讲的「坐标」与 u3（分块/变长）、u7-l2（SplitKV）打通。
- **深入 Blackwell 硬件原语**：本讲提到的 `cute.arch.clc_response`、`PipelineClcFetchAsync`、cluster 等都依赖 u8-l3（UMMA descriptor 与 blackwell_helpers）的 PTX 层知识，建议接着读 u8-l3。
- **2CTA 与 CLC 的互斥**：[flash_attn/cute/flash_fwd_sm100.py:228-239](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L228-L239) 显示 CLC 与 2CTA 指令有耦合（`cluster_shape_m == cta_group_size`），可在 u8-l4（hd256 2CTA kernel）里看到这套协作与潜在的死锁陷阱。
- **调试实战**：按 `AI/CLC_TRACE_DEBUG.md` 用 `FA_LOG_LEVEL=3 FA_CLC=1` 抓一段 trace，用 `AI/parse_clc_log.py` 解析，亲眼验证「物理 CTA 与逻辑 work tile 的动态绑定」，这会顺带为 u11-l5（GPU kernel 调试）做铺垫。
