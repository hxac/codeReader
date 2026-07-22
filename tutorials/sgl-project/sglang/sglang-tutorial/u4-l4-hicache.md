# HiCache：KV 分层卸载到主机/磁盘

## 1. 本讲目标

本讲承接 u4-l1（RadixAttention 与基数树缓存）、u4-l2（KV 内存池）与 u4-l3（前缀缓存接口与淘汰策略）。前几讲讲的 KV 缓存都住在 GPU 显存里（L1），本讲要把这层缓存「向上」延伸出主机内存（L2）和磁盘/共享存储（L3）两层。

学完后你应该能够：

1. 说清 HiCache 的三层结构（GPU / Host / Storage）解决了什么问题。
2. 看懂 `HiRadixCache` 在 `evict`（淘汰到下层）、`load_back`（从下层捞回）、`prefetch_from_storage`（提前预热）这几条主线上怎么编排。
3. 理解 `HiCacheStorage` 这层抽象，以及运行期 `attach_storage_backend` / detach 的挂载机制。
4. 读懂主机侧 MHA 池（`pool_host/mha.py`）的**分阶段写回（staged write-back）**机制，并能说清为什么**非对称 MHA（K 与 V 的 head_dim 不相等）必须用两套独立 staging buffer**。

> 本讲对应的本次代码变化：`pool_host/mha.py` 引入路径由旧的 `sglang.jit_kernel.hicache` 迁移到新的 `sglang.kernels.ops.kvcache.hicache`（RFC #29630 算子统一迁移），并为 `AsymmetricMHATokenToKVPoolHost` 新增了分阶段写回支持。其余 HiCache 文件（`hiradix_cache.py`、`hicache_storage.py`）主体未变。

## 2. 前置知识

- **KV 缓存是什么**：Transformer 每一层对历史 token 计算 K/V，存在显存里避免重复计算。上下文越长，KV 越大。
- **Paged / 基数树缓存**：u4-l1、u4-l2 讲过，SGLang 用一棵 RadixTree + 两级内存池（`ReqToTokenPool` / `TokenToKVPool`）管理显存里的 KV，并以 page 为单位做前缀复用与淘汰。
- **延迟 vs 吞吐的取舍**：GPU 显存最快但最贵也最小；主机内存大但慢一个数量级；磁盘/网络共享存储最大但最慢。HiCache 就是用「快慢结合的分层存储」换取「能放下更长的上下文 + 更高的显存利用率」。
- **DMA / kernel 拷贝**：把显存数据搬到主机内存，既可以用 PyTorch 张量索引（`direct` 后端），也可以用专门的 CUDA kernel（`kernel` 后端）做更高带宽的搬运。本讲的「分阶段写回」就是 `kernel` 后端的一种实现。
- **MHA 与「非对称」**：标准多头注意力里 K 和 V 的 head_dim 相同；少数模型（如 MiMo-V2）的 K 与 V 维度不同（`head_dim != v_head_dim`），这会影响拷贝 kernel 的步长（stride）计算，是本讲的一个重点。

如果你对 RadixCache 的 `TreeNode`、`match_prefix`、`evict`、`inc_lock_ref`/`dec_lock_ref` 还不熟，建议先读 u4-l1 与 u4-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/sglang/srt/mem_cache/hiradix_cache.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py) | `HiRadixCache`，继承 `RadixCache`，是三层缓存的「总指挥」，串联 evict / write_backup / load_back / prefetch。 |
| [python/sglang/srt/mem_cache/hicache_storage.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hicache_storage.py) | `HiCacheStorage` 抽象基类与 `HiCacheFile` 实现，定义 L3（磁盘/共享存储）的键值接口。 |
| [python/sglang/srt/mem_cache/pool_host/mha.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py) | 主机侧（L2）MHA 池：`MHATokenToKVPoolHost` 与 `AsymmetricMHATokenToKVPoolHost`，包含本讲重点「分阶段写回」。 |
| [python/sglang/srt/managers/cache_controller.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/cache_controller.py) | `HiCacheController`，负责 GPU↔Host↔Storage 的搬运编排与后台线程，含 `attach_storage_backend`。 |
| [python/sglang/srt/server_args.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py) | `hicache_*` 一组 CLI 参数，全部归入 `NS("memory")` 命名空间。 |
| [python/sglang/kernels/ops/kvcache/hicache.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/kernels/ops/kvcache/hicache.py) | HiCache 搬运算子的新统一位置（原 `sglang.jit_kernel.hicache`），含 `can_use_write_back_jit_kernel` 等能力探测与 JIT 算子。 |

## 4. 核心概念与源码讲解

### 4.1 三层 KV 缓存：动机与整体架构

#### 4.1.1 概念说明

GPU 显存是有限的。当请求很多、上下文很长时，RadixCache 会按 LRU/优先级淘汰节点（见 u4-l3）。**被淘汰的 KV 一旦再次被命中，就要重新做一遍 prefill 计算**——这在多轮对话、Agent、长文档问答里会造成大量重复计算。

HiCache 的思路是**给 KV 缓存加「下层」**：

- **L1 = GPU 显存**：`TokenToKVPool`（u4-l2），最快，由 RadixCache 直接管理。
- **L2 = 主机内存（Host）**：`pool_host/mha.py` 里的 `MHATokenToKVPoolHost`。容量通常是 L1 的若干倍（由 `hicache_ratio` 控制），慢于显存但远快于磁盘。
- **L3 = 磁盘 / 共享存储（Storage）**：`hicache_storage.py` 里的 `HiCacheStorage`，可选。跨进程、跨机器复用 KV，容量最大但最慢。

这样，GPU 放不下的 KV 不是直接丢弃，而是**降级（demote）到 Host**；Host 也放不下时再降到 Storage；下次命中时再**升级（promote）回来**（load_back / prefetch）。代价是搬运带宽，换的是「省掉重算」和「支撑更长上下文」。

#### 4.1.2 核心流程

整体可以画成一条「漏斗式」的数据流：

```text
请求命中前缀
      │
      ▼
[L1 GPU] ──evict──▶ [L2 Host] ──evict_host──▶ [L3 Storage]
   ▲                    │                          │
   │ load_back           │ write_backup_storage     │
   │ (H2D)               │ (H2L3)                   │ prefetch_from_storage (L3→L2)
   └────────────────────┴──────────────────────────┘
```

两条贯穿三层的「动作」要区分清楚：

- **backup（写回）方向是 L1→L2→L3**：把还在 GPU 的 KV 备份到下层，方便日后淘汰时「安全丢弃」。
- **load / prefetch（取回）方向是 L3→L2→L1**：命中了下层才有的前缀，把它搬回 GPU。

SGLang 在 `ServerArgs` 里用一组参数控制这条流水线，全部标注 `NS("memory")`：

- `hicache_ratio`：L2 与 L1 的容量比，默认 `2.0`。
- `hicache_size`：直接指定 L2 的 GB 数，会覆盖 ratio。
- `hicache_write_policy`：`write_back` / `write_through` / `write_through_selective`，默认 `write_through`。
- `hicache_io_backend`：GPU↔Host 搬运方式，`direct` / `kernel` / `kernel_ascend`，默认 `kernel`。
- `hicache_mem_layout`：L2 张量布局，默认 `page_first`。
- `hicache_storage_backend`：L3 后端，`file` / `mooncake` / `hf3fs` / `nixl` / `eic` / `simm` / `mori` / `aibrix` / `dynamic`，默认 `None`（即不启用 L3）。
- `hicache_storage_prefetch_policy`：`best_effort` / `wait_complete` / `timeout`，默认 `timeout`。

这些字段定义在 [server_args.py:L2479-L2549](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L2479-L2549)。

启用方式是在启动命令加 `--enable-hierarchical-cache`，可参考 CI 测试 [test/registered/models_e2e/test_mimo_v2.py:L24-L30](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/test/registered/models_e2e/test_mimo_v2.py#L24-L30) 的写法。

#### 4.1.3 源码精读：HiRadixCache 是怎么被造出来的

`HiRadixCache` 在缓存注册表里被选中并构造：

[python/sglang/srt/mem_cache/registry.py:L125](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/registry.py#L125) —— 当 `--enable-hierarchical-cache` 打开时，缓存工厂返回 `HiRadixCache(params=params, server_args=server_args)`。

它的 `__init__` 做三件事：建 L2 池、建 cache_controller（含可选 L3）、登记若干「在途操作」账本：

[python/sglang/srt/mem_cache/hiradix_cache.py:L77-L184](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L77-L184)。关键点：

- L83-L91：根据 GPU 池类型挑 L2 池。MHA 模型走 `get_mha_host_pool_cls(self.kv_cache)(...)`（本讲 4.4 重点）。
- L117：`enable_storage = server_args.hicache_storage_backend is not None`，决定 L3 是否启用。
- L159-L175：构造 `HiCacheController`，把 L3 backend、prefetch 阈值、写策略都交给它。

注意：`HiRadixCache` **继承自 `RadixCache`**，所以前几讲讲过的 `match_prefix`、`insert`、`inc_lock_ref` 等接口它都有，只是把「淘汰=丢弃」改成了「淘汰=降级到下层」。

#### 4.1.4 代码实践

**实践目标**：在不启动服务的前提下，确认 HiCache 的三层结构与对应的开关。

**操作步骤**：

1. 在 `server_args.py` 中定位 7 个 `hicache_*` 字段，记录每个字段的默认值与 `NS(...)` 标注（应当都是 `NS("memory")`）。
2. 在 `registry.py` 中找到 `HiRadixCache(...)` 构造点，确认它的上一行是判断「是否启用分层缓存」。
3. 在 `hiradix_cache.py` 的 `__init__` 中找到 L2 池类型分派（`get_mha_host_pool_cls` / `MLATokenToKVPoolHost`）与 L3 开关（`enable_storage`）。

**需要观察的现象**：`hicache_storage_backend` 默认是 `None`，意味着**只开 `--enable-hierarchical-cache` 而不指定 storage backend 时，只有 L1+L2 两层，没有 L3**。L3 必须显式指定 backend 才会出现。

**预期结果**：你能用一句话回答「SGLang 的 HiCache 有几层、分别由哪些参数控制」。

> 本步骤为源码阅读型实践，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：如果不加任何 `--hicache-*` 参数，HiCache 会启用吗？
**答案**：不会。必须显式 `--enable-hierarchical-cache`，才会让 `registry.py` 选 `HiRadixCache`。

**练习 2**：`hicache_ratio=2.0` 意味着什么？
**答案**：L2（主机内存池）的容量是 L1（GPU 显存池）的 2 倍。

---

### 4.2 HiRadixCache：分层缓存的总指挥

#### 4.2.1 概念说明

`HiRadixCache` 的核心职责是在 RadixTree 的节点状态之上，多维护一组「这个节点的 KV 现在住在哪一层」的信息：

- `node.value`：KV 在 **GPU** 的物理槽索引（L1）。`None` 表示已被 evict 出 GPU。
- `node.host_value`：KV 在 **主机** 的索引（L2）。
- `node.backuped`：是否已经成功备份到下层（决定淘汰时能否「安全丢弃」）。
- `node.evicted`：是否已从 GPU 淘汰（命中时需要 load_back）。
- `node.lock_ref` / `node.host_ref_counter`：引用计数，保护在用 KV 不被淘汰（u4-l1、u4-l3 讲过）。

围绕这些状态，`HiRadixCache` 提供五条主要动作：`write_backup`（写回 L2/L3）、`evict`（淘汰出 GPU）、`load_back`（从 L2 捞回 GPU）、`prefetch_from_storage`（L3 预热到 L2）、`match_prefix`（查询命中）。

#### 4.2.2 核心流程

**淘汰（evict）** 是调度器在显存吃紧时调用的。根据写策略分两条路：

```text
evict(num_tokens)
  ├─ write_back 模式 → _evict_write_back
  │     对每个待淘汰叶节点：
  │       已 backuped  → _detach_backuped（保留 host，释放 device 槽）
  │       未 backuped  → write_backup(write_back=True) 搬到 host，再 detach
  │       搬不动       → _drop_subtree_no_host（主机也满，直接丢弃并告警）
  └─ 其他模式（write_through*） → _evict_write_through
        已 backuped  → _evict_backuped
        未 backuped  → _evict_regular（直接丢，发 BlockRemoved 事件）
```

**取回（load_back）** 发生在命中了一个已被 evict 的节点时。它会沿着父链收集所有需要捞回的节点，一次性从 host 读回 device：

```text
load_back(last_hit_node)
  1. while node.evicted: nodes_to_load 收集祖先链
  2. inc_lock_ref(祖先) 防止又被淘汰
  3. host_indices = cat(各节点 host_value)
  4. cache_controller.load(host_indices) → device_indices（H2D 搬运）
     失败则先 evict 腾地方再重试一次
  5. 把 device_indices 切回各节点，node.value 复活
```

**预热（prefetch_from_storage）** 是 L3 专属优化：当一个请求的前缀大概率在 L3 命中时，提前把它从 L3 拉到 L2，等真正 `load_back` 时就直接命中 L2，省掉 L3 的慢速读取。

#### 4.2.3 源码精读

**淘汰分派** [hiradix_cache.py:L1111-L1119](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1111-L1119)：按 `write_policy` 选择 `_evict_write_back` 或 `_evict_write_through`。

**write_through 淘汰** [hiradix_cache.py:L1135-L1151](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1135-L1151)：用一个按优先级排序的小顶堆（`_make_eviction_heap`）逐个弹出叶节点；已备份的走 `_evict_backuped`，未备份的走 `_evict_regular`。

**write_back 淘汰** [hiradix_cache.py:L1153-L1185](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1153-L1185)：相比 write_through，多了一个「先 `write_backup` 把 KV 搬到 host、再 `_detach_backuped`」的分支；搬不动时 `_drop_subtree_no_host` 并打印告警。注意方法注释里写了 **this path will be deprecated in the future**——write_back 是较老的模式。

**写回（D2H 备份）** [hiradix_cache.py:L829-L859](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L829-L859)：核心是调 `self.cache_controller.write(device_indices=node.value, ...)` 把 device 槽内容搬到 host，拿到 `host_indices` 存进 `node.host_value`。搬不动时会先 `evict_host` 腾地方再重试一次（L843-L849）。这里有个**不变式**：write-through 模式下，已备份节点必须从根开始连续成前缀，不能有空洞，所以 L833-L836 会检查父节点是否已备份。

**写回 L3** [hiradix_cache.py:L905-L918](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L905-L918)：`write_backup_storage` 在 D2H 完成后（`_finish_write_through_ack` 里调用），把 host 内容再持久化到 L3，处理了「节点可能被 split」的情况（`_concat_split_chain`）。

**取回（H2D）** [hiradix_cache.py:L1290-L1361](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1290-L1361)：按上面流程图的 5 步走。注意 L1311 的 `load_back_threshold`——太小的命中不值得搬；L1322 `cache_controller.load` 真正做 H2D，失败会 `evict` 后重试（L1328-L1333），仍失败则放弃并告警（L1335-L1346）。

**预热** [hiradix_cache.py:L1672-L1711](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1672-L1711)：构造对齐到 page 的 `prefetch_key`，交给 `cache_controller.prefetch`，登记进 `self.ongoing_prefetch`。注意 L1696-L1698 的注释：**host 索引不再在这里预分配**，而是等 L3 命中数确定后在 `_drain_and_alloc_storage_hit` 里按实际命中页数懒分配，避免为主机预留用不上的内存。

**命中查询** [hiradix_cache.py:L1639-L1670](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1639-L1670)：`match_prefix` 返回 `MatchResult`，里面同时给出 `device_indices`（GPU 上有的）和 `host_hit_length`（在 host 上、需 load_back 的长度），让调度器知道「命中了多少、其中多少在 GPU、多少在下层」。

#### 4.2.4 代码实践

**实践目标**：手工跟踪一条长上下文请求的 KV 在「显存不足 → evict 到 host → 再次命中 → load_back 回 GPU」全过程中的方法调用链。

**操作步骤**：

1. 假设服务用 `--enable-hierarchical-cache --hicache-write-policy write_through` 启动。
2. 设想一条长上下文请求 R1 完成 prefill，其 KV 写入 RadixCache（`insert`）。
3. 显存压力上升，调度器调用 `evict(EvictParams(num_tokens=N))`：
   - 读 [hiradix_cache.py:L1114-L1117](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1114-L1117)，确认走 `_evict_write_through`。
   - 对 R1 对应的叶节点：若已 `backuped`，走 `_evict_backuped`（[L1200-L1204](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1200-L1204)），释放 device 槽但保留 `host_value`；否则 `_evict_regular`（[L1206-L1214](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1206-L1214)）直接丢弃。
4. 一条新请求 R2 与 R1 共享前缀，调度器调 `match_prefix`，发现 `device_indices` 较短但 `host_hit_length > 0`（节点 `evicted=True`）。
5. 调度器对 `best_match_node` 调 `init_load_back`（[L1363-L1383](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1363-L1383)）→ `load_back`（[L1290](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1290)），经 `cache_controller.load` 把 host 的 KV 搬回 GPU，`node.value` 复活。

**需要观察的现象**：load_back 内部有「先尝试 → 失败则 evict 腾地 → 再试一次 → 还失败则放弃」的重试逻辑（L1322-L1346）。

**预期结果**：你能画出 R1 的 KV 在三层之间的迁移轨迹，并标注每一步调用的方法名。

> 待本地验证：在真实 GPU 上用足够长的上下文压满显存，才能稳定触发 evict/load_back；小上下文不会触发。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `write_backup` 在 write-through 模式下要求「父节点已 backuped」（L833-L836）？
**答案**：保证已备份节点从根开始是连续前缀，避免出现「中间断了」的空洞，否则淘汰与 load_back 的祖先链收集会出错。

**练习 2**：`load_back` 里 `inc_lock_ref(祖先)` 的作用是什么？
**答案**：在 H2D 搬运期间防止祖先节点又被别的淘汰逻辑踢出 GPU，保证搬运的目标链稳定。

---

### 4.3 HiCacheStorage 抽象与 attach/detach storage_backend

#### 4.3.1 概念说明

L3（磁盘/共享存储）需要适配多种后端：本地文件（`file`）、3FS（`hf3fs`）、Mooncake、NIXL、EIC、MoRI、SIMM 等。SGLang 用一个抽象基类 `HiCacheStorage` 统一它们的接口，再用具体子类（如 `HiCacheFile`）实现。

L3 的另一个特点是：它**可以在运行期挂载/卸载**，而不必在启动时就决定。这通过 `HiCacheController.attach_storage_backend` 实现，对应一个管理员 HTTP 端点（`/clear_hicache_storage_backend` 等，需要 admin key）。

#### 4.3.2 核心流程

L3 后端的核心接口是「按页（page）的键值存取」：

- **key**：通常是某段前缀 token 序列的哈希字符串（`get_hash_str`），加上 rank/model 后缀避免多卡写冲突。
- **exists / batch_exists_v2**：查这些页在不在 L3（最长前缀语义）。
- **get / batch_get_v2**：把页从 L3 读到 host buffer。
- **set / batch_set_v2**：把页从 host buffer 写到 L3。

`batch_exists_v2` 还支持**多池（multi-pool）命中策略**：除了 KV 池，还可以有 Mamba SSM 状态池、SWA（滑窗）池等「辅助池」，用 `PoolTransfer` 描述，用 `PoolHitPolicy` 决定怎么取交集（`ALL_PAGES` 要求前缀每页都在；`TRAILING_PAGES` 只要求尾部若干页在）。

**挂载流程**：

```text
attach_storage_backend(storage_backend, ...)
  1. 检查 enable_storage 未开启 & 旧后台线程已停止（_stop_storage_threads）
  2. _generate_storage_config 生成 storage_config（含 tp/pp/cp rank、是否 MLA 等）
  3. StorageBackendFactory.create_backend(...) 造后端实例
  4. backend.register_mem_pool_host(self.mem_pool_host) 把 L2 池交给它
  5. enable_storage = True，按后端类型选 page_get/page_set 函数（零拷贝 or 通用）
```

#### 4.3.3 源码精读

**抽象基类** [hicache_storage.py:L148-L324](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hicache_storage.py#L148-L324)：`HiCacheStorage(ABC)` 定义 `get`/`set`/`exists`/`batch_get`/`batch_set`/`batch_exists_v2`/`batch_get_v2`/`batch_set_v2` 等抽象方法。`batch_exists_v2` 的文档（[L163-L194](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hicache_storage.py#L163-L194)）清楚说明了多池命中策略。

**配置与传输描述符**：

- `HiCacheStorageConfig` [hicache_storage.py:L26-L40](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hicache_storage.py#L26-L40)：携带 tp/pp/cp rank、是否 MLA、布局等。
- `PoolTransfer` [hicache_storage.py:L92-L107](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hicache_storage.py#L92-L107)：统一描述一次「按池」的传输，含 device/host 索引、keys、命中策略。
- `PrefetchTimeoutConfig` [hicache_storage.py:L49-L55](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hicache_storage.py#L49-L55)：线性 prefetch 超时策略的三个旋钮（固定开销 `base`、每 1024 token 的 `per_ki_token`、上限 `max`）。

**文件后端实现** [hicache_storage.py:L359](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hicache_storage.py#L359)：`HiCacheFile` 把每页存成一个 `.bin` 文件。它的 `set`（[L498-L545](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hicache_storage.py#L498-L545)）走「先写临时文件再 `os.replace` 原子替换」的套路，并把 LRU/容量/磁盘淘汰逻辑委托给 `LRUFileEvictor`，自己只做「原始字节存储」。`get`（[L461-L483](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hicache_storage.py#L461-L483)）用 `f.readinto(memoryview(...))` 直接读进目标张量，避免拷贝。

**运行期挂载** [cache_controller.py:L431-L504](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/cache_controller.py#L431-L504)：`HiCacheController.attach_storage_backend`。关键约束在 [L443-L444](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/cache_controller.py#L443-L444)：**要求此刻没有在途请求**，且预期在调度线程（控制路径）上执行，不能和 prefetch/backup 并发。卸载则由 `_stop_storage_threads` 把后台线程收掉再清状态（attach 时也会先调它兜底，见 L448-L453）。

#### 4.3.4 代码实践

**实践目标**：理解 L3 后端的「按页键值」模型与多池命中策略。

**操作步骤**：

1. 在 `hicache_storage.py` 的 `HiCacheFile.batch_exists_v2`（[L600-L648](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hicache_storage.py#L600-L648)）里，找出「最长连续 KV 前缀」是如何计算的（L614-L621 用 `next(...)` 找第一个不存在的页）。
2. 对比 `PoolHitPolicy.ALL_PAGES` 与 `TRAILING_PAGES` 两个分支（L630-L643），说明它们各自适用于什么辅助池（KV/DSA 用 ALL_PAGES；Mamba/SWA 状态用 TRAILING_PAGES）。
3. 在 `cache_controller.py` 的 `attach_storage_backend` 里，确认 `backup_skip` 的含义（[L465-L469](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/managers/cache_controller.py#L465-L469)：MLA 模型只需一个 rank 备份）。

**需要观察的现象**：`HiCacheFile` 的 key 会被加上 `config_suffix`（含 model 名、tp rank、pp、cp rank），保证多卡/多模型不串。

**预期结果**：你能解释「为什么 attach_storage_backend 要求没有在途请求」。

> 本步骤为源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：`HiCacheFile.set` 为什么先写 `.tmp` 文件再 `os.replace`？
**答案**：原子性——写到一半崩溃不会留下半个坏文件覆盖已有的好数据；`os.replace` 在同一文件系统上是原子的。

**练习 2**：MLA 模型为什么只需 `tp_rank == 0` 备份（`backup_skip`）？
**答案**：MLA 把 KV 压成低秩，多张卡上共享同一份低秩表示，没必要每张卡都存一份，省 L3 空间与写入带宽。

---

### 4.4 主机侧 MHA 池与分阶段写回（staged write-back）

> 这是本讲的重点模块，也是本次代码更新（`update`）的核心改动所在。

#### 4.4.1 概念说明

L2（主机内存）池的职责是：在 GPU 和主机之间搬运 KV。搬运发生在两个方向：

- **backup（D2H）**：`backup_from_device_all_layer` —— 把 GPU 上所有层的 K/V 搬到 host buffer。这是「写回」。
- **load（H2D）**：`load_to_device_per_layer` —— 把 host buffer 的某层搬回 GPU。

当 `hicache_io_backend=kernel` 时，搬运不走 PyTorch 张量索引（慢），而是走专门的 CUDA/HIP kernel（高带宽）。**问题在于**：直接 device→host 大块拷贝会占用过多显存带宽、和正常前向抢资源。**分阶段写回（staged write-back）** 的解法是：先在 GPU 上开一块较小的 **staging buffer**（中转缓冲），把要搬的页分批拷进 staging，再从 staging 异步搬到 host。这样每次只占一小块显存，且能与前向重叠。

本讲的代码更新做了两件事：

1. **算子迁移**：`from sglang.jit_kernel.hicache import ...` → `from sglang.kernels.ops.kvcache.hicache import ...`（RFC #29630，详见 u11-l2）。算子功能和签名没变，只是搬家了。
2. **新增非对称 MHA 的分阶段写回**：之前只有「对称 MHA」（K、V head_dim 相同）有分阶段写回；现在 `AsymmetricMHATokenToKVPoolHost`（K、V head_dim 不同，如 MiMo-V2）也支持了。

#### 4.4.2 核心流程：对称 vs 非对称

**对称 MHA**（`MHATokenToKVPoolHost`）：K 和 V 的 `head_dim` 相同，所以一个 token 里 K 的字节数 = V 的字节数 = `head_num * head_dim * itemsize`，称为 `element_dim`。一个分阶段 kernel 可以**同时**搬 K 和 V，用两块形状相同的 `staging_k_buffer` / `staging_v_buffer`：

```text
_init_write_back_staging_buffers()（对称）
  条件：layout=="page_first" 且 CUDA/HIP 且 element_size % 16 == 0
  分配 staging_k_buffer / staging_v_buffer，容量 = min(page_num, STAGING_PAGE_CHUNK) * page_size
  can_use_write_back_jit = True

backup_from_device_all_layer()（对称，page_first 分支）
  if can_use_write_back_jit:
      jit_transfer_hicache_all_layer_staged_lf_pf(
          k/v_ptr_src, src/dst_indices, staging_k/v, dst_k/v, page_size)   # K、V 一次搞定
  else:
      transfer_kv_all_layer_lf_pf(...)   # 回退到非分阶段路径
```

**非对称 MHA**（`AsymmetricMHATokenToKVPoolHost`）：K 的 head_dim ≠ V 的 head_dim，于是 K、V 的 token 步长不同：

\[
\text{stride}_K = \text{head\_num} \times \text{head\_dim} \times \text{itemsize},\quad
\text{stride}_V = \text{head\_num} \times \text{v\_head\_dim} \times \text{itemsize}
\]

一个 kernel 没法同时按两种步长搬，所以**必须把 K、V 拆成两次独立的单缓冲（single-buffer）分阶段拷贝**，各用自己步长专用的 staging buffer：

```text
_init_write_back_staging_buffers()（非对称，新）
  条件同上，但 K、V 分别探测：
      can_use_write_back_jit_kernel(element_size=stride_K) 且
      can_use_write_back_jit_kernel(element_size=stride_V)
  分配 staging_k_buffer[tokens, layer, head, head_dim]
  分配 staging_v_buffer[tokens, layer, head, v_head_dim]   ← 最后一维不同！

backup_from_device_all_layer()（非对称，新）
  if can_use_write_back_jit:
      jit_transfer_hicache_all_layer_mla_staged_lf_pf(K 侧)   # 用 staging_k_buffer
      jit_transfer_hicache_all_layer_mla_staged_lf_pf(V 侧)   # 用 staging_v_buffer
  else:
      transfer_kv_all_layer_mla_lf_pf(K 侧)                    # 回退
      transfer_kv_all_layer_mla_lf_pf(V 侧)
```

> 注意类注释（[mha.py:L935-L945](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L935-L945)）：非对称池用两个独立 buffer（`k_buffer`/`v_buffer`）而不是一个 `(2, ...)` 张量，正是为了让每侧保留自己的原生步长。

#### 4.4.3 源码精读

**基类构造** [pool_host/mha.py:L69-L122](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L69-L122)：`MHATokenToKVPoolHost.__init__` 在末尾（L122）调 `self._init_write_back_staging_buffers()`。L98-L100 探测普通 JIT 搬运能力 `can_use_jit`（注释说明 ROCm/HIP 也有路径）。

**对称分阶段缓冲初始化** [pool_host/mha.py:L171-L204](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L171-L204)：

- L177：要求 `layout == "page_first"` 且非 NPU/XPU/MPS。
- L182-L186：`can_use_write_back_jit = (CUDA 或 HIP) and can_use_write_back_jit_kernel(element_size=element_dim * itemsize)`。
- L192-L204：先 `torch.empty(...)` 分配 `staging_k_buffer`，再用 `torch.empty_like(staging_k_buffer)` 分配形状完全相同的 `staging_v_buffer`（对称模型 K、V 同形状）。

**对称写回路径** [pool_host/mha.py:L357-L381](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L357-L381)：`backup_from_device_all_layer` 的 `page_first` 分支。L358-L369 走分阶段 kernel `jit_transfer_hicache_all_layer_staged_lf_pf`，**一次调用同时传 K 和 V**；否则回退到 `transfer_kv_all_layer_lf_pf`。

**非对称类** [pool_host/mha.py:L935-L945](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L935-L945)：`AsymmetricMHATokenToKVPoolHost(MHATokenToKVPoolHost)`，docstring 点明 K、V 独立存储、独立步长。

**非对称分阶段缓冲初始化（本次新增）** [pool_host/mha.py:L947-L990](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L947-L990)：

- L958-L964：**K、V 分别探测能力**，用 `_k_token_stride_size()` 和 `_v_token_stride_size()`（L1054-L1058）算各自的 element_size，要求两者都能用分阶段 kernel。
- L971-L990：分配 `staging_k_buffer`（最后一维 `head_dim`）与 `staging_v_buffer`（最后一维 `v_head_dim`）——**两块形状不同**。

**非对称写回路径（本次新增 staged 分支）** [pool_host/mha.py:L1143-L1159](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L1143-L1159)：当 `can_use_write_back_jit` 为真，**分两次**调 `jit_transfer_hicache_all_layer_mla_staged_lf_pf`，分别搬 K 和 V；否则回退到 `transfer_kv_all_layer_mla_lf_pf`（L1161-L1178）。

**步长计算** [pool_host/mha.py:L1054-L1064](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L1054-L1064)：`_k_token_stride_size` / `_v_token_stride_size` / `_k_layout_dim` / `_v_layout_dim`。注意 `init_kv_buffer`（[L1007-L1052](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L1007-L1052)）的注释：**故意不设置** `token_stride_size` / `layout_dim`，因为 K、V 步长不同，任何想用单一共享步长的调用都是 bug，应当 `AttributeError` 大声报错。

**工厂函数** [pool_host/mha.py:L1299-L1307](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L1299-L1307)：`get_mha_host_pool_cls(device_pool)` 按 `device_pool.head_dim != device_pool.v_head_dim` 选 `AsymmetricMHATokenToKVPoolHost`，否则选 `MHATokenToKVPoolHost`。

**能力探测算子** [python/sglang/kernels/ops/kvcache/hicache.py:L92-L113](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/kernels/ops/kvcache/hicache.py#L92-L113)：`can_use_write_back_jit_kernel` 要求 `element_size % 16 == 0`（L99-L101），否则 warning 并返回 False；能编出 staged JIT 模块才返回 True。

#### 4.4.4 代码实践

**实践目标**：理解非对称 MHA 场景下「分阶段写回为何必须拆成 K、V 两次」，并定位触发该路径的条件。

**操作步骤**：

1. **追踪工厂分派**：读 [get_mha_host_pool_cls](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L1299-L1307)，确认「head_dim != v_head_dim」时返回 `AsymmetricMHATokenToKVPoolHost`。
2. **对比两个 `_init_write_back_staging_buffers`**：
   - 对称版（[L171](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L171)）：用单一 `element_dim` 探测，staging_k 与 staging_v **最后一维相同**。
   - 非对称版（[L947](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L947)）：K、V **分别探测**，staging_v 最后一维是 `v_head_dim`。
3. **对比写回路径**：对称版一次调用搬完 K+V（[L359](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L359)）；非对称版两次调用（[L1144](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L1144) 与 [L1152](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L1152)）。
4. **可选运行验证（需要 8 卡 H200 与 MiMo-V2.5 权重，较重）**：参考 [test/registered/models_e2e/test_mimo_v2.py:L11-L31](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/test/registered/models_e2e/test_mimo_v2.py#L11-L31) 的启动参数启动服务。

**需要观察的现象 / 重要提示**：

> 该 CI 测试用的是 `--hicache-mem-layout page_first_direct --hicache-io-backend direct`，走的是 `direct` 直传路径，**并不会**触发新增的 staged JIT kernel。要真正命中本次新增的代码，需用 `--hicache-mem-layout page_first --hicache-io-backend kernel`，且在 CUDA/HIP 上、`element_size % 16 == 0`。换用 `direct` 是因为分阶段 kernel 对某些 head_dim 组合暂不支持（`can_use_write_back_jit` 为 False 时自动回退到直传）。

**预期结果**：你能说清——「对称 MHA 一次 kernel 搬 K+V；非对称 MHA 必须拆两次，因为 K、V 步长不同，共享步长会让某一侧拷贝错位」。

> 待本地验证：在带 GPU 的机器上用 MiMo-V2.5 + `page_first`/`kernel` 启动，并在 `_init_write_back_staging_buffers` 加日志确认 `can_use_write_back_jit=True`、`staging_v_buffer` 最后一维等于 `v_head_dim`。

#### 4.4.5 小练习与答案

**练习 1**：为什么对称 MHA 的 `backup_from_device_all_layer` 一次 kernel 调用就能搬完 K 和 V，而非对称不行？
**答案**：对称时 K、V 的 token 字节数相同（`element_dim` 一样），可共用步长；非对称时 `stride_K ≠ stride_V`，一个 kernel 无法同时按两种步长寻址，所以拆成 K、V 两次单缓冲拷贝。

**练习 2**：`AsymmetricMHATokenToKVPoolHost.init_kv_buffer` 为什么「故意不设置 `token_stride_size`」？
**答案**：K、V 步长不同，不存在「单一共享步长」。不设置它，可以让任何误用单一步长的调用以 `AttributeError` 显式失败，而不是悄悄拿 K 的步长去拷 V（会拷错位）。

**练习 3**：什么条件下 `can_use_write_back_jit` 才为 True？
**答案**：`layout == "page_first"`、设备是 CUDA 或 HIP（非 NPU/XPU/MPS）、且对应侧的 `element_size % 16 == 0`（对称只测一次，非对称 K、V 各测一次），三者同时满足。

---

## 5. 综合实践

把本讲的四条主线串起来，完成一次「源码走查 + 配置推演」：

**任务**：为一个假设的「非对称 MHA 长上下文服务」写出 HiCache 的完整配置与数据流说明。

1. **选配置**：模型是 MiMo-V2.5（K、V head_dim 不同）。从 [server_args.py:L2479-L2549](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L2479-L2549) 选出：`--enable-hierarchical-cache`、`--hicache-ratio 1.5`、`--hicache-mem-layout page_first`、`--hicache-io-backend kernel`，并可选加 `--hicache-storage-backend file` 启用 L3。
2. **画三层**：标出 L1（`MHATokenToKVPool`）、L2（`AsymmetricMHATokenToKVPoolHost`）、L3（`HiCacheFile`）三类对象，并注明 L2 是由 `get_mha_host_pool_cls` 在 [mha.py:L1305-L1306](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L1305-L1306) 选出来的。
3. **写回链路**：当 GPU 显存吃紧，调度器调 `evict` →（write_back 模式）`write_backup`（[hiradix_cache.py:L829](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L829)）→ `cache_controller.write` → 最终落到非对称池的 `backup_from_device_all_layer`（[mha.py:L1143-L1159](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/pool_host/mha.py#L1143-L1159)），**分两次**把 K、V 经各自的 staging buffer 搬到 host。
4. **取回链路**：下次命中已淘汰前缀，`match_prefix`（[L1639](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1639)）报告 `host_hit_length` → `load_back`（[L1290](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1290)）→ `cache_controller.load` 把 host 的 KV 搬回 GPU。
5. **L3 预热**（若启用）：`prefetch_from_storage`（[L1672](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/mem_cache/hiradix_cache.py#L1672)）在请求到来前把 L3 的前缀预热到 L2。

**交付物**：一张标注了「方法名 + 文件:行号」的三层数据流图，以及一段说明「为什么这个非对称模型在写回时要走两次 kernel」。

## 6. 本讲小结

- HiCache 给 KV 缓存加了 **L1(GPU) → L2(Host) → L3(Storage)** 三层，把「淘汰即丢弃」改成「淘汰即降级」，换来更长上下文与更高显存利用率。
- `HiRadixCache` 继承 `RadixCache`，在节点状态上多了 `host_value`/`backuped`/`evicted` 等，通过 `evict` / `write_backup` / `load_back` / `prefetch_from_storage` / `match_prefix` 编排三层之间的搬运。
- `HiCacheStorage` 是 L3 的抽象基类，`HiCacheFile` 是文件实现；L3 可经 `HiCacheController.attach_storage_backend` 在运行期挂载（要求无在途请求）。
- 主机侧 MHA 池在 `pool_host/mha.py`，**分阶段写回**用 GPU staging buffer 分批搬运，降低显存带宽占用。
- **本次更新**：算子从 `sglang.jit_kernel.hicache` 迁到 `sglang.kernels.ops.kvcache.hicache`；并为 `AsymmetricMHATokenToKVPoolHost` 新增分阶段写回——因 K、V 步长不同，必须拆成两次独立单缓冲拷贝，用形状不同的 `staging_k_buffer` / `staging_v_buffer`。
- 想真正命中新增的 staged kernel，需 `page_first` 布局 + `kernel` io 后端 + CUDA/HIP + `element_size % 16 == 0`；否则自动回退到直传或非分阶段路径。

## 7. 下一步学习建议

- **u4 系列收尾后**：回到 u5-l1（ModelRunner 与前向执行路径），看 `out_cache_loc` 如何把 HiCache 的命中索引接到注意力后端。
- **想深入 L3 后端**：阅读 `python/sglang/srt/mem_cache/storage/` 下各后端实现（mooncake/hf3fs/nixl/eic 等），以及 `cache_controller.py` 的后台线程与 `kv_events`（u9-l2 会讲分离部署下的 KV 传输）。
- **想理解算子迁移全貌**：本讲提到的 `jit_kernel → kernels.ops` 迁移属于 RFC #29630，完整机制见 u11-l2（统一算子体系：kernels 注册/选择与 sgl-kernel/JIT）。
- **想看非对称 MHA 的上游**：读 `python/sglang/srt/mem_cache/memory_pool.py` 里 `MHATokenToKVPool` 的 `v_head_dim` 字段，以及 `models/` 下用到它的模型实现。
