# KV 缓存配置与分配策略

> 本讲属于第 6 单元「KV 缓存与 RadixAttention」，承接 [u6-l1 KV 缓存内存池](u6-l1-memory-pool.md)。
> u6-l1 讲的是「池长什么样」（`req_to_token` / `token_to_kv` 两层映射、KV 张量、dtype）；
> 本讲回答两个更上层的问题：**这个池要做多大？运行时每个 token 的 slot 怎么分出去、怎么收回来？**

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 SGLang 是如何「从一块可用显存」推导出「KV 池能容纳多少 token」的整条流水线（预算 → coeff/bias 模型 → 容量 → 外部约束）。
2. 理解混合 Mamba/线性注意力模型的「状态池」预算为何要单独联合求解，并能解释 `max_mamba_cache_size`（记作 K）的内存预算方程里那个 `(K+1)` 的 **+1 padding 修正**修正了什么 off-by-one。
3. 描述一次 prefill/extend 与一次 decode 分别走哪条 slot 分配与回收路径，paged 与非 paged 的差异在哪。
4. 区分三类分配器（非分页 `TokenToKVPoolAllocator`、分页 `PagedTokenToKVPoolAllocator`、统一内存池的 `MultiEndedAllocator`）的记账方式，并知道 RadixCache 实现是怎么被「注册 + 选择」的。

## 2. 前置知识

- **KV 缓存（KV cache）**：Transformer 每一层对历史 token 的 Key/Value 缓存。每多缓存一个 token，每层都要存一份 K 和 V。所以「每 token 的内存成本」与层数、KV head 数、head 维度、dtype 强相关——这正是后面 `cell_size` 的来源。
- **page / page_size**：把连续的 token slot 按固定大小（`page_size`，SGLang 默认 1）打包成一个「页」。`page_size=1` 时退化为逐 token 分配；`page_size>1`（如分页 attention）时按页分配，减少元数据开销。
- **prefill / extend / decode**：prefill 是首段长上下文一次性算；decode 是每次只多一个 token；extend 是「已有前缀 KV 命中缓存，再续写一段」。slot 分配在 extend 和 decode 两条路径上形态不同。
- **混合 Mamba / 线性注意力模型**：这类模型（如 Jamba、Mamba2、GDN 系列）除了普通注意力层，还有状态空间模型（SSM/Mamba）层。后者不存「每个 token 的 KV」，而是存一个「按请求维度」的 **conv state + temporal state**（一个请求一份，而非一个 token 一份）。所以它和 KV 池是两种不同的存储，要分开预算。
- **off-by-one（差一错误）**：预算里少算了 1 个 slot 的容量，导致实际分配的池子比预算认为的大一点点，极端情况下会超卖显存。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [srt/mem_cache/kv_cache_configurator.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py) | 配置中枢。profile 显存 → 算预算 → 调 sizing → 派生池尺寸 → 初始化各类池与分配器。**本讲的主角。** |
| [srt/model_executor/pool_configurator.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/model_executor/pool_configurator.py) | 把「字节数预算」换算成「token 数」的纯数学层。`coeff + bias` 模型，按架构分 Default / HybridSWA / DSV4 等子类。 |
| [srt/mem_cache/allocation.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocation.py) | 运行时 slot 分配/回收的入口函数（`alloc_for_extend` / `alloc_for_decode` / `alloc_token_slots` / `alloc_req_slots`）。 |
| [srt/mem_cache/allocator/base.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocator/base.py) | 分配器抽象基类 `BaseTokenToKVPoolAllocator`：定义 `alloc` / `free` / `backup_state` 等契约。 |
| [srt/mem_cache/allocator/paged.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocator/paged.py) | 分页分配器 `PagedTokenToKVPoolAllocator`：页对齐的 `alloc` / `alloc_extend` / `alloc_decode` / `free`。 |
| [srt/mem_cache/multi_ended_allocator.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/multi_ended_allocator.py) | 统一内存池分配器 `MultiEndedAllocator`：一个字节 buffer 上挂多个子池，靠 virtual→physical 映射 + compaction 工作。 |
| [srt/mem_cache/registry.py](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/registry.py) | RadixCache 实现的可插拔工厂注册表（`register_radix_cache_backend` / `create_tree_cache`）。 |

> 说明：`srt/mem_cache/allocator/` 是一个子包（`base.py` / `paged.py` / `swa.py` / `hisparse.py` / `mamba.py` / `token.py`）。本讲聚焦 `base.py`、`paged.py` 与统一池的 `multi_ended_allocator.py`，其余是针对 SWA / HiSparse / NPU 的特化分配器，原理同构。

## 4. 核心概念与源码讲解

### 4.1 KV 池规格推算：从「可用显存」到「能放多少 token」

#### 4.1.1 概念说明

启动服务时，SGLang 要回答一个关键问题：**给定这张卡的剩余显存，KV 池最多能放多少 token？** 这个数字（`max_total_num_tokens`）决定了能同时服务多少请求、多长的上下文。

整体思路是一个「预算 → 容量」的反推：

- 先量出「扣掉模型权重和运行时 slack 之后，还剩多少字节」可用给 KV 池。
- 再知道「每缓存一个 token，全层一共要花多少字节」（记作 `cell_size`，即 coeff）。
- 相除就得到 token 数，最后按 page 对齐。

`pool_configurator.py` 把这件事抽象成一个统一的 **`coeff + bias` 模型**：

\[
\text{available\_bytes} = \text{max\_tokens} \times \text{coeff} + \text{bias}
\quad\Rightarrow\quad
\text{max\_tokens} = \frac{\text{available\_bytes} - \text{bias}}{\text{coeff}}
\]

对最普通的 MHA/MLA 模型，`bias = 0`、`coeff = cell_size`，于是 `max_tokens = available_bytes // cell_size`。

#### 4.1.2 核心流程

推算由 `KVCacheConfigurator.configure` 主导，分层清晰：

```text
configure(pre_model_load_memory)
  └─ _resolve_memory_pool_config(pre_model_load_memory)
       ├─ _profile_available_bytes()          # ① 量可用字节（含 mamba 预留）
       │      └─ _handle_max_mamba_cache()     #    混合 Mamba: 从预算里先扣状态池（见 4.2）
       ├─ config_from_budget(available_bytes)  # ② 字节 → token 数
       │      └─ MemoryPoolConfigurator.calculate_pool_sizes(budget, page_size)
       │             = available_bytes // cell_size，再 page 对齐
       ├─ _apply_token_constraints(capacity)   # ③ 用户上限 + PP 多卡取 min 同步
       ├─ resolve_max_num_reqs(capacity)        # ④ 由容量反推 max_running_requests
       └─ finalize_with_max_running_requests() # ⑤ 填充依赖请求数的派生池（如 DSV4 state）
  └─ _derive_pool_sizes(config)                # ⑥ 拆成 full/swa/c4/c128 等各池尺寸
  └─ _init_pools(sizes, ...)                   # ⑦ 真正 new 出池对象 + 分配器
```

- **第①步量预算**：`_profile_available_bytes` 拿到当前可用显存，减去 `pre_model_load_memory * (1 - mem_fraction_static)` 作为「非静态运行时 slack」（这部分要留给激活、临时 buffer，不能给 KV）。对混合 Mamba 模型，再调 `_handle_max_mamba_cache` 把状态池的份额从总预算里切走。
- **第②步换算**：`config_from_budget` 把字节交给 `calculate_pool_sizes`。
- **第③步外部约束**：`_apply_token_constraints` 叠加两类约束——用户 `--max-total-tokens` 上限，以及流水线并行（PP）多卡间用 `all_reduce(MIN)` 取最小值（因为各 PP stage 层数不同，能放的 token 数不同，必须对齐到最少的那个）。
- **第④步反推请求数**：`resolve_max_num_reqs` 由 token 容量估算能同时跑多少请求，并受 mamba 状态池大小约束（见 4.2）。

#### 4.1.3 源码精读

`configure` 是整条链的入口，先解析配置再派生尺寸再建池：

[kv_cache_configurator.py:219-252](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L219-L252) — 主入口 `configure`：解析 `MemoryPoolConfig` → `_derive_pool_sizes` → `_init_pools`，最后把所有池打包成 `KVCacheConfigResult` 返回。

字节数→token 数的换算核心在 `DefaultPoolConfigurator`。对普通 MHA 模型，每 token 的字节数就是「KV head 数 × (head_dim + v_head_dim) × 层数 × dtype 字节数」：

[pool_configurator.py:261-266](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/model_executor/pool_configurator.py#L261-L266) — 默认 MHA 的 `cell_size` 公式（KV head × 头维 × 层数 × dtype 字节）。

[pool_configurator.py:287-292](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/model_executor/pool_configurator.py#L287-L292) — `DefaultPoolConfigurator.calculate_pool_sizes`：`max_tokens = available_bytes // cell_size`，再 `// page_size * page_size` 做页对齐。

预算与外部约束的衔接：

[kv_cache_configurator.py:1527-1570](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1527-L1570) — `_profile_available_bytes`：计算 KV 预算（可用显存 − slack），混合 Mamba 时交给 `_handle_max_mamba_cache` 切走状态池份额；若剩余 ≤ 0 则给出「提高 `--mem-fraction-static`」的可操作报错。

[kv_cache_configurator.py:1593-1620](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1593-L1620) — `_apply_token_constraints`：叠加用户 `--max-total-tokens` 上限，并用 `torch.distributed.all_reduce(MIN)` 在 PP rank 间对齐。

[kv_cache_configurator.py:1679-1701](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1679-L1701) — `config_from_budget`：先 `calculate_pool_sizes(budget)`，再 `_apply_token_constraints`；若约束改变了容量，就用 `calculate_pool_sizes_from_max_tokens` 重算（保证 page 对齐）。

#### 4.1.4 代码实践

1. **实践目标**：手算一个具体模型的 KV 池容量，并与启动日志对比。
2. **操作步骤**：
   - 选一个普通 MHA 模型（如 Qwen2.5-0.5B，假设 `num_kv_heads=4`、`head_dim=64`、`v_head_dim=64`、`hidden_layers=24`、KV dtype=bf16 即 2 字节）。
   - 套用 cell_size 公式：`cell_size = 4 × (64+64) × 24 × 2 = 49152` 字节/token ≈ 48 KB/token。
   - 启动时设 `--mem-fraction-static 0.85`，观察启动日志里 `Memory pool end. avail mem=... GB` 和 KV 池打印的 `max_total_num_tokens`。
   - 用日志里的可用字节数 `÷ cell_size` 反推，应与打印的 token 数量级一致（会因 slack、page 对齐略有出入）。
3. **需要观察的现象**：改 `--mem-fraction-static`（如 0.8 → 0.9），`max_total_num_tokens` 应近似线性上升；改 `--context-length` 不直接影响池大小，但影响 `resolve_max_num_reqs` 的估算。
4. **预期结果**：手算值与日志值偏差在「slack + page 对齐」量级内（通常 <10%）。若偏差巨大，多半是选错了层数/dtype，或模型走了 MLA/DSA 分支（cell_size 公式不同，见 `pool_configurator.py` 的 `use_mla_backend` 分支）。
5. 若手边无 GPU：可只做「阅读型实践」——在 [pool_configurator.py:261-266](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/model_executor/pool_configurator.py#L261-L266) 用模型的 `config.json` 代入算出 cell_size，标注「待本地验证」。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `DefaultPoolConfigurator` 的 `bias` 是 0？什么情况下 `bias` 会非零？
  - **答案**：默认 MHA/MLA 池里每个 token 的开销只与层数、KV head、维度、dtype 有关，是一个「纯线性、无固定开销」的模型，所以 `bias=0`。当池里存在与 token 数无关的固定开销（如 DSV4 的 c128 压缩状态池按请求数固定分配、或 FP4 的共享 dequant workspace）时，就需要把这些固定项放进 `bias` 或单独扣除（DSV4 走的是 `_get_c128_state_fixed_bytes` 单独扣，见 `pool_configurator.py`）。
- **练习 2**：`_apply_token_constraints` 里为什么要做 `all_reduce(MIN)`？
  - **答案**：流水线并行下，不同 PP stage 持有的层数不同（模型被竖着切了），所以每段能放的 token 数不同；只有对齐到「最少的那个 stage」的容量，所有 stage 才能在同一 batch 上协同，否则会出现某些 stage 提前 OOM。

---

### 4.2 Mamba 状态池预算与 +1 padding 修正

> 这是本次更新的**重点**。HEAD `59ef3b1` 的提交 *\[Fix\] Reserve the mamba pool's +1 padding slot in the memory budget solve (#32184)* 修正了这里的 off-by-one。

#### 4.2.1 概念说明

混合 Mamba/线性注意力模型有两类需要常驻显存的缓存：

- **普通注意力层的 KV 池**：按 token 维度，`max_total_num_tokens` 个 slot。
- **Mamba/SSM 层的状态池**：按**请求**维度，每请求一份 `conv_state + temporal_state`。它的容量是 `max_mamba_cache_size`（我们记作 **K**）个 slot。

这两类共享同一块「KV 预算」，所以不能各自独立算，而要**联合求解**：在给定预算下，K 取多大才不会超卖？这就是 `_handle_max_mamba_cache` 的工作。

**关键陷阱（+1 padding slot）**：Mamba 状态池和请求映射表（`ReqToTokenPool`）在物理上都会**多分配一个 slot**（即实际张量是 `size + 1` 而不是 `size`）。多出来的那一个叫 **padding slot**。它的作用是给 **CUDA Graph 的 dummy 读写**兜底——CUDA Graph 捕获的静态 batch 会把空位上的 `req_pool_indices` 默认填成 0，于是这些 dummy 读写会落到 index 0 这个「无害的」padding slot 上，而不是越界。

问题在于：**预算求解方程如果假设池占 `K` 份内存，但实际池占 `K+1` 份**，就会差 1 个 slot 的容量。在「显存刚好吃满」的边界配置下，这 1 份就足以导致实际分配超出预算、引发 OOM 或越界。修正就是把方程里所有用到池容量的地方都改成 `K+1`。

#### 4.2.2 核心流程

`_handle_max_mamba_cache` 有三条分支，取决于用户给了哪些参数：

1. **显式 `--max-mamba-cache-size`**：直接用用户给的 K，并按「capped 请求数 +1」预留投机解码的中间状态内存。
2. **`--disable-radix-cache` 且显式 `--max-running-requests`**：由请求数推 K，中间状态按「`K + 1`」预留。
3. **自动拟合（默认）**：从预算里按比例切出 mamba 预算，**联立方程求解 K**。

第 3 条的联立方程（带投机解码 `D = speculative_num_draft_tokens`、`ratio` 为 mamba 状态放大比）：

\[
\underbrace{(K+1)\cdot \text{per\_req}}_{\text{main state 池}}
\;+\;
\underbrace{\left(\frac{K}{\text{ratio}}+1\right)\cdot D \cdot \text{per\_req}}_{\text{spec 中间状态池}}
\;=\; \text{mamba\_budget\_bytes}
\]

其中两个 `+1` 分别对应「main state 池的 padding slot」和「spec 中间状态池的 padding slot」。解出：

\[
K = \left\lfloor\frac{\text{budget} - \text{per\_req}\cdot(1+D)}{\text{per\_req}\cdot(1 + D/\text{ratio})}\right\rfloor
\]

（分子里的 `per_req·(1+D)` 就是两个 padding slot 的成本。）不带投机解码时退化为：

\[
K = \left\lfloor\frac{\text{budget} - \text{per\_req}}{\text{per\_req}}\right\rfloor
\]

最后，函数返回「总预算 − main state 池实际占用」。注意这里实际占用也必须是 `(K+1)·per_req`：

```text
return total_rest_memory − (K + 1) * per_req   # main state 池实际占用，含 padding slot
```

#### 4.2.3 源码精读

先看 padding slot 的**物理来源**——这才是 off-by-one 的根。`ReqToTokenPool` 和 Mamba 状态池都多分 1 个 slot：

[memory_pool.py:267-278](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/memory_pool.py#L267-L278) — `ReqToTokenPool.__init__`：`self._alloc_size = size + 1`，注释说明「+1 padding row at index 0：cuda-graph padded batches 默认 `req_pool_indices=0`，dummy 读写落在这里无害」。注意 `free_slots` 从 1 开始（`range(1, ...)`），即 slot 0 永不分配给真实请求。

[memory_pool.py:503-511](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/memory_pool.py#L503-L511) — Mamba `conv_state` 张量：`size=(num_mamba_layers, size + 1) + conv_shape`，`temporal_state` 同样是 `size + 1`（见 [memory_pool.py:528-532](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/memory_pool.py#L528-L532)）。`envelope_layout` 分支用 `max_slots = size + 1`（[memory_pool.py:483](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/memory_pool.py#L483)）。**物理池是 `size+1`，所以预算必须按 `K+1` 算。**

再看预算求解的修正点（全部已带上 `+1`）：

[kv_cache_configurator.py:1703-1813](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1703-L1813) — `_handle_max_mamba_cache` 全函数。逐段对应：

[kv_cache_configurator.py:1713-1732](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1713-L1732) — 分支①（显式 `max_mamba_cache_size`）：投机解码时预留中间状态用 `(capped_reqs + 1)`——那个 `+1` 是中间状态池的 padding slot。

[kv_cache_configurator.py:1733-1750](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1733-L1750) — 分支②（`disable_radix_cache` + 显式请求数）：中间状态按 `(server_args.max_mamba_cache_size + 1)` 预留。

[kv_cache_configurator.py:1751-1789](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1751-L1789) — 分支③（自动拟合）：

[kv_cache_configurator.py:1756-1764](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1756-L1764) — 注释直接写出联立方程 `(K + 1) * per_req + (K / ratio + 1) * D * per_req = mamba_budget_bytes`，并指向 `memory_pool.py` 解释 +1 的来源。

[kv_cache_configurator.py:1770-1776](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1770-L1776) — 投机解码分支求解：`max_mamba_cache_size = (budget − per_req·(1+D)) // (per_req·(1+D/ratio))`，分子减去的 `per_req·(1+D)` 正是两个 padding slot 的成本。

[kv_cache_configurator.py:1786-1789](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1786-L1789) — 非投机分支求解：`max_mamba_cache_size = (budget − per_req) // per_req`，分子减去的 `per_req` 是 main state 池那一个 padding slot。

[kv_cache_configurator.py:1807-1813](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1807-L1813) — 最后扣 main state 实际占用：`mamba_state_memory = (max_mamba_cache_size + 1) * mamba_cache_per_req`，注释「+1: the pool's padding slot」。**修正前这里很可能是 `max_mamba_cache_size * per_req`（漏了 +1），导致返回的 `total_rest_memory` 比真实多了 `per_req` 字节，下游 KV 池就多拿了这一份。**

辅助：`max_mamba_cache_size` 还会反向约束 `max_running_requests`：

[kv_cache_configurator.py:1637-1651](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1637-L1651) — `resolve_max_num_reqs` 里的 mamba 约束：`max_num_reqs = min(..., max_mamba_cache_size // ratio)`，若 ≤0 给出可操作报错。

[kv_cache_configurator.py:1572-1591](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1572-L1591) — `_calculate_mamba_ratio`：`ratio = 3 + additional_ratio`，`additional_ratio` 来自 overlap/lazy 的 ping-pong buffer（overlap 时 2、lazy 时 1、非 overlap 时 1）。

#### 4.2.4 代码实践

1. **实践目标**：定位 `(K+1)` 形式的 +1 修正，并解释它修正了什么 off-by-one。
2. **操作步骤**：
   - 在 [kv_cache_configurator.py:1786-1789](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1786-L1789) 找到非投机分支的求解 `int((mamba_budget_bytes - per_req) // per_req)`。
   - 把分子改回 `mamba_budget_bytes`（即「假装漏掉 +1」），对照 [memory_pool.py:506](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/memory_pool.py#L506) 的 `size + 1`，推演：求解出的 K 会偏大 1，main state 池实际分配 `(K+1)·per_req`，而预算只扣了 `K·per_req`，于是多占了 `per_req` 字节。
   - （**只读分析，不要真改源码**）写一段说明：在「`--mem-fraction-static` 设到几乎吃满显存」的边界配置下，这 `per_req` 字节就是压垮骆驼的最后一根稻草。
3. **需要观察的现象**：对比修正前后，启动一个混合 Mamba 模型（如 `--mem-fraction-static 0.9` 高压配置），修正后日志里的 `max_total_num_tokens` 会略小（因为 mamba 占用算得更准），但不再出现偶发 OOM。
4. **预期结果**：能口头复述「物理池是 `size+1`（CUDA Graph dummy 兜底），所以预算方程与最终扣减都必须用 `K+1`；修正前用 `K`，差 1 个 slot」。
5. 若无混合 Mamba 模型/GPU：标注「待本地验证」，只做源码阅读分析即可。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 padding slot 是「1 个」而不是更多？slot 0 为什么不会被真实请求占用？
  - **答案**：CUDA Graph 的静态 batch 只会把空位的 `req_pool_indices` 填成单个固定值 0，所以只需要 **1 个**兜底 slot（index 0）。`ReqToTokenPool` 的 `free_slots = range(1, alloc_size)` 从 1 开始，真实请求永远拿不到 slot 0，于是 0 专门留给 dummy 读写，互不干扰。
- **练习 2**：投机解码分支的方程里有两个 `+1`，分别是什么？
  - **答案**：一个是 **main state 池**（按请求维度，`K` 份 + 1 padding）；另一个是 **spec 中间状态池**（按「capped 请求数 `K/ratio`」维度，`K/ratio` 份 + 1 padding）。分子 `−per_req·(1+D)` 里，`per_req·1` 是 main state 的 padding，`per_req·D` 是中间状态 padding（中间状态每份含 `D` 个 draft token）。

---

### 4.3 运行时 slot 分配与回收：allocation.py

#### 4.3.1 概念说明

池建好后，每次 forward 之前，调度器要为当前 batch 里的每个 token 分配一个 KV slot（拿到一串 `out_cache_loc`），forward 之后请求结束时要回收 slot。这一层逻辑集中在 `allocation.py`，它不直接操作张量，而是**调用分配器**（`allocator.alloc*`）拿到 slot 编号，再把这些编号写进 `req_to_token` 映射表。

两条主路径：

- **extend（prefill/续写）**：一批请求各自要分配 `extend_len` 个新 token 的 slot，还要把命中的前缀 slot 续接上。
- **decode（逐 token）**：每个请求只多 1 个 token 的 slot。

每条路径都分 **paged**（`page_size > 1`）与 **非 paged**（`page_size == 1`）两个子路径。paged 路径要用到 `last_loc`（请求当前最后一页的位置）来决定是「续用尾页」还是「开新页」。

#### 4.3.2 核心流程

**extend 路径**（`alloc_for_extend`）：

```text
alloc_for_extend(batch)
  ├─ batch.maybe_evict_swa()                 # 先清掉滑窗外的旧 token
  ├─ alloc_req_slots(...)                     # ① 分配「请求槽」(req_pool_idx)，失败则 RuntimeError
  ├─ if page_size == 1:
  │      alloc_token_slots(tree_cache, extend_num_tokens)   # ②a 非分页: 直接要 N 个 slot
  │    else:
  │      alloc_paged_token_slots_extend(...)  # ②b 分页: 算 last_loc + alloc_extend kernel
  └─ write_cache_indices(...)                 # ③ 把 out_cache_loc 写进 req_to_token 映射表
```

**decode 路径**（`alloc_for_decode`）类似，但每个请求只要 1 个 token，调 `alloc_token_slots`（非分页）或 `alloc_paged_token_slots_decode`（分页）。

**回收**：slot 的回收不是在这些函数里做，而是在请求结束/RadixCache 淘汰时，由 RadixCache 调 `allocator.free(...)`（见 4.4）。`alloc_req_slots` 里对混合 Mamba 池还会做「byte-coordinated」的可用量预检与必要时的 mamba 状态淘汰。

#### 4.3.3 源码精读

[allocation.py:303-403](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocation.py#L303-L403) — `alloc_for_extend`：extend 主路径。先 `alloc_req_slots` 分请求槽，再按 `page_size` 分 token slot，最后 `write_cache_indices` 写表。

[allocation.py:146-171](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocation.py#L146-L171) — `alloc_token_slots`：非分页快速路径。先 `evict_from_tree_cache` 淘汰够多的条目，再 `allocator.alloc(num_tokens)`；若返回 None 则抛「Out of memory, try to lower your batch size」。

[allocation.py:188-249](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocation.py#L188-L249) — `alloc_paged_token_slots_extend`：分页 extend。会「高估」需求（假设每请求都要开新页：`num_tokens = extend_num_tokens + len(seq_lens) * page_size`），先淘汰，再调 `allocator.alloc_extend(...)`（内部跑 Triton `alloc_extend_kernel`），返回页对齐的 slot。

[allocation.py:539-593](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocation.py#L539-L593) — `alloc_for_decode`：decode 主路径，每请求 1 token，写表时 `locs = seq_lens`（decoder-decoder 模型会偏移 `encoder_lens`）。

[allocation.py:252-291](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocation.py#L252-L291) — `alloc_req_slots`：分配请求槽。对 `HybridReqToTokenPool`（混合 Mamba）会按 byte-coordinated 可用量预检，并在 mamba 状态不够时触发 `tree_cache.evict(mamba_num=...)`；普通池拿不到槽则抛 RuntimeError 并提示调小 `--max-running-requests`。

[allocation.py:55-101](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocation.py#L55-L101) — `write_cache_indices`：把分配到的 `out_cache_loc` 按前缀/续写两段写进 `req_to_token` 表（有 Triton kernel 快路径和逐请求 fallback）。

#### 4.3.4 代码实践

1. **实践目标**：跟踪一次 extend 的 slot 分配调用链。
2. **操作步骤**：
   - 在 [allocation.py:303](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocation.py#L303) 的 `alloc_for_extend` 头部加一行 `print(f"[trace] extend num_tokens={batch.extend_num_tokens}, page_size={_alloc_page_size(batch)}")`（只读分析用，不要提交）。
   - 启动一个 `--page-size 1` 的模型发一条长 prompt，观察日志里每步分配的 token 数；再换 `--page-size 16` 重试，对比走的是 `alloc_token_slots` 还是 `alloc_paged_token_slots_extend`。
3. **需要观察的现象**：`page_size=1` 时每次 extend 分配的 slot 数等于 `extend_num_tokens`；`page_size>1` 时会略多（高估的「每请求一页」余量），且 `alloc_extend_kernel` 被调用。
4. **预期结果**：能用一句话总结「extend 走 extend 路径、decode 走 decode 路径，二者都先淘汰再分配、失败 fail-loud」。**待本地验证。**
5. 若不便改源码：纯阅读 [allocation.py:303-403](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocation.py#L303-L403)，画出三步（请求槽 → token slot → 写表）的调用图。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `alloc_paged_token_slots_extend` 要「高估」一页的需求（`extend_num_tokens + len(seq_lens) * page_size`）？
  - **答案**：分页分配时，最坏情况下每个请求的续写都会跨页、各开一个新页。先把最坏情况的页数都先淘汰出来（`evict_from_tree_cache`），能保证随后的 `alloc_extend` 不会因为 free list 不足而返回 None。实际 `alloc_extend` 用完后只会真正消耗 `num_new_pages` 页，高估部分并不会被扣走。
- **练习 2**：`alloc_token_slots` 在 `allocator.alloc` 返回 `None` 时抛 RuntimeError 而不是静默重试，这种「fail-loud」设计的好处是什么？
  - **答案**：分配失败意味着 admission（准入预算）算错了或池子真满了。fail-loud 把错误立刻暴露在调度层并给出可操作建议（调小 batch / `--max-running-requests`），而不是静默吞掉导致后续 forward 读到未初始化/错误的 KV slot，那会是更难定位的数值错误。

---

### 4.4 分配器策略与 RadixCache 注册

#### 4.4.1 概念说明

**分配器（allocator）** 是 slot 的「记账员」：它维护一张「哪些 slot 空闲、哪些在用」的表，对外只暴露 `alloc(n)` / `free(ids)` / `available_size()`。`allocation.py` 的函数只跟分配器打交道，不关心 slot 背后是哪种存储。

SGLang 有三类典型分配器，区别在「记账方式」：

| 分配器 | 适用场景 | 记账方式 | 关键方法 |
| --- | --- | --- | --- |
| `TokenToKVPoolAllocator` | `page_size==1` 且无 DCP 的普通模型 | 一个**空闲 slot 列表**（free list） | `alloc(n)` / `free(ids)` |
| `PagedTokenToKVPoolAllocator` | `page_size>1` 或 DCP 分页模型 | **按页**管理：`free_pages` 列表，slot = page×page_size+offset | `alloc` / `alloc_extend` / `alloc_decode`（Triton kernel） |
| `MultiEndedAllocator` | 统一内存池（`--enable-unified-memory`，hybrid Mamba/SWA） | **virtual→physical 页映射表 + watermark + compaction** | `alloc*` + `translate_kv_loc` + `flush_opportunistic` |

**RadixCache 注册（registry）** 是另一回事：它注册的不是 slot 分配器，而是「前缀树缓存实现」的工厂。`RadixCache`、`UnifiedRadixCache`、`HiRadixCache`、`ChunkCache` 等是不同实现，靠 `--radix-cache-backend` 选择，外部实现可在 import 时调 `register_radix_cache_backend(name, factory)` 注册进来。

#### 4.4.2 核心流程

**分配器的选择**发生在 `KVCacheConfigurator._build_token_to_kv_pool_allocator`：按平台/模型/page_size 选具体类——out-of-tree 平台用 `current_platform.get_paged_allocator_cls()`；NPU/Ascend 有专用子类；普通路径下 `page_size==1 && dcp_size==1` 用非分页 `TokenToKVPoolAllocator`，否则用 `PagedTokenToKVPoolAllocator`。统一内存池（`--enable-unified-memory`）走单独的 fast path，构造 `MultiEndedAllocator`。

**分配器的 alloc/free 协议**（基类 `BaseTokenToKVPoolAllocator` 定义）：

```text
alloc(need_size) -> Tensor | None      # 要 n 个 slot，返回 slot id 列表；不够返回 None
free(free_index)                         # 归还 slot
free_group_begin / free_group_end        # 批量 free：攒一批再一次 free
backup_state / restore_state             # 给投机解码用的快照/回滚
available_size()                         # 当前可分配量
```

**RadixCache 的注册与选择**：

```text
create_tree_cache(ctx)
  ├─ name = server_args.radix_cache_backend
  ├─ if name: factory = get_radix_cache_factory(name)   # 外部注册的后端
  │           cache = factory(ctx)
  └─ else:    cache = default_radix_cache_factory(ctx)   # 内置选择链
                 ├─ disable_radix_cache → ChunkCache
                 ├─ hybrid_swa / hybrid_ssm → UnifiedRadixCache
                 ├─ enable_hierarchical_cache → HiRadixCache / Unified+HiCache
                 └─ 默认 → RadixCache
```

外部后端通过 `register_radix_cache_backend("mybackend", factory)` 在 import 时把自己塞进全局字典 `_RADIX_CACHE_REGISTRY`，然后用户用 `--radix-cache-backend mybackend` 选中。

#### 4.4.3 源码精读

[allocator/base.py:27-117](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocator/base.py#L27-L117) — `BaseTokenToKVPoolAllocator` 抽象基类：定义 `alloc` / `free` 抽象方法，以及 `backup_state`/`restore_state`/`free_group_begin`/`free_group_end`/`merge_and_sort_free`/`resize` 等通用协议。注意 `alloc_extend`/`alloc_decode` 基类默认抛 `NotImplementedError`（只有分页分配器实现）。

[allocator/paged.py:105-170](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocator/paged.py#L105-L170) — `PagedTokenToKVPoolAllocator`：`num_pages = size // page_size`；`alloc` 从 `free_pages` 头部取页，把页展开成 slot（`page * page_size + offset`）。

[allocator/paged.py:277-284](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocator/paged.py#L277-L284) — `clear`：`free_pages = range(1, num_pages+1)`——**页 0 是 padding slot，永不分配**（与 4.2 的 `size+1` 同源）。

[allocator/paged.py:261-272](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocator/paged.py#L261-L272) — `free`：`free_index // page_size` 还原成页号，去重后回收到 `free_pages`（`need_sort` 时先进 `release_pages`，合并时排序）。

[multi_ended_allocator.py:99-124](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/multi_ended_allocator.py#L99-L124) — `MultiEndedAllocator`：在一个共享 `UnifiedKVPool` 字节 buffer 上为一个子池做分配。核心是 `virtual_to_physical` / `physical_to_virtual` 两张页映射表 + `watermark_physical` 水位线。`alloc*` 先在**虚拟空间**跑一次 kernel，再把消耗的虚拟页 **bind** 到物理页；`translate_kv_loc` 把虚拟 slot 翻译成物理 slot。

[multi_ended_allocator.py:652-709](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/multi_ended_allocator.py#L652-L709) — `translate_kv_loc`：虚拟→物理翻译，并对 tombstone（`v2p == -1`）做 `clamp_min(0)`——把非法读路由到物理 slot 0（又是那个 padding sink），避免 CUDA Graph 重放时越界。

[multi_ended_allocator.py:1364-1567](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/multi_ended_allocator.py#L1364-L1567) — `_flush`：**compaction（紧凑化）**。free 会在物理空间里留下「洞」，`_flush` 把高位的存活页搬到洞里、回退水位线，从而在共享 buffer 里为 peer 子池腾出连续空间。这是统一内存池能在一个 buffer 上跑 full+swa / full+mamba 两个子池的关键。

[registry.py:54-72](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/registry.py#L54-L72) — `register_radix_cache_backend` / `get_radix_cache_factory`：全局字典 `_RADIX_CACHE_REGISTRY`，名字重复或为空会报错。

[registry.py:79-160](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/registry.py#L79-L160) — `default_radix_cache_factory`：内置选择链（ChunkCache / UnifiedRadixCache / HiRadixCache / RadixCache 等）。

[registry.py:196-233](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/registry.py#L196-L233) — `create_tree_cache`：按 `--radix-cache-backend` 优先选外部注册的后端，否则走内置链；最后可包一层 `StreamingSession`。

#### 4.4.4 代码实践

1. **实践目标**：对比三类分配器，并理解 RadixCache 的可插拔注册。
2. **操作步骤**：
   - 阅读 [allocator/base.py:27-117](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocator/base.py#L27-L117)、[allocator/paged.py:105-170](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocator/paged.py#L105-L170)、[multi_ended_allocator.py:99-124](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/multi_ended_allocator.py#L99-L124)，填一张对比表（见 4.4.1）。
   - 在 [kv_cache_configurator.py:1464-1484](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1464-L1484) 找到「`page_size==1 && dcp_size==1` 用非分页，否则用分页」的分支，确认选择逻辑。
   - （阅读型）在 [registry.py:54-68](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/registry.py#L54-L68) 模拟注册一个假后端：写一段伪代码 `register_radix_cache_backend("mybench", lambda ctx: RadixCache(ctx.params))`，说明它如何被 `--radix-cache-backend mybench` 选中（不要真往源码里加）。
3. **需要观察的现象**：分页分配器的 `clear` 让页 0 不可分配；统一内存池分配器有 `watermark` 和 compaction，另两类没有。
4. **预期结果**：能说清「非分页=slot free list、分页=页 free list + kernel、统一池=v2p 映射 + compaction」，以及「registry 注册的是 RadixCache 工厂而非 slot 分配器」。
5. 无 GPU 时标注「待本地验证」，纯源码阅读即可完成本实践。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `MultiEndedAllocator` 需要 compaction（`_flush`），而另外两类不需要？
  - **答案**：前两类（非分页 / 分页）的池是**独占**的，free 出来的 slot 直接回到自己的 free list 即可复用，不需要搬动数据。`MultiEndedAllocator` 跑在**一个共享字节 buffer** 上，full 和 swa（或 full 和 mamba）两个子池从两端相向生长，free 会在中间留下「洞」；不 compaction 的话水位线无法回退，peer 子池就没法扩展。compaction 把存活页搬到洞里、回退水位线，才能在共享空间里动态调配两个子池的边界。
- **练习 2**：`registry.py` 的 `_RADIX_CACHE_REGISTRY` 和 `attention_registry`（注意力后端注册表）是不是一回事？
  - **答案**：不是。`registry.py` 注册的是**前缀树缓存实现**（RadixCache 家族），决定「KV 的前缀复用怎么组织」；`attention_registry` 注册的是**注意力计算后端**（FlashInfer / Triton / fa3 等），决定「注意力算子怎么算」。两者正交：一个管存储复用，一个管计算。

## 5. 综合实践

把本讲四条主线串起来，完成一个「**从预算到 slot 的全程推演**」小任务：

1. **预算**：选一个普通 MHA 模型，套 [pool_configurator.py:261-266](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/model_executor/pool_configurator.py#L261-L266) 的公式算出 `cell_size`，假设某张卡有 `B` 字节可用，算出 `max_total_num_tokens = B // cell_size`（再 page 对齐）。
2. **池**：对照 [kv_cache_configurator.py:1368-1484](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1368-L1484) 说明：`page_size=1` 时选 `TokenToKVPoolAllocator`，`page_size>1` 时选 `PagedTokenToKVPoolAllocator`，并指出页 0 是 padding slot（[paged.py:277-284](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocator/paged.py#L277-L284)）。
3. **运行时分配**：画一条请求从 `alloc_for_extend` → `alloc_token_slots`/`alloc_paged_token_slots_extend` → `write_cache_indices` 的时序（[allocation.py:303-403](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocation.py#L303-L403)），标注 `out_cache_loc` 在哪里产生、写进哪张表。
4. **回收与复用**：说明请求结束后 RadixCache 调 `allocator.free` 回收 slot（[paged.py:261-272](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/allocator/paged.py#L261-L272)），而 RadixCache 实现本身由 [registry.py:196-233](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/registry.py#L196-L233) 选中。
5. **进阶（如有混合 Mamba 模型）**：把 [kv_cache_configurator.py:1786-1813](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/kv_cache_configurator.py#L1786-L1813) 的 `(K+1)` 修正代入，手算一份 mamba 预算，说明漏掉 +1 会怎样。

产出：一张「预算 → 池 → 分配 → 回收」的四段流程图 + 一段关于 +1 padding 修正的文字说明。

## 6. 本讲小结

- KV 池大小由一条「**预算 → 容量**」流水线决定：`_profile_available_bytes` 量预算 → `calculate_pool_sizes` 用 `coeff+bias` 模型（默认 `cell_size` 除法）换 token 数 → `_apply_token_constraints` 叠加用户上限与 PP `all_reduce(MIN)` → `resolve_max_num_reqs` 反推请求数。
- 混合 Mamba 模型的状态池与 KV 池共享预算，要联立方程求解 `max_mamba_cache_size (K)`；**HEAD `59ef3b1` 修正了 +1 padding slot 的 off-by-one**——物理池是 `size+1`（CUDA Graph dummy 兜底），所以求解方程、中间状态预留、最终扣减三处都必须用 `K+1`。
- 运行时 slot 分发集中在 `allocation.py`：extend 走 `alloc_for_extend`、decode 走 `alloc_for_decode`，都先淘汰再分配、失败 fail-loud；`page_size>1` 走带 `alloc_extend/decode` kernel 的分页路径。
- 分配器有三种记账方式：非分页（slot free list）、分页（页 free list + Triton kernel）、统一内存池 `MultiEndedAllocator`（v2p 映射 + watermark + compaction）；它们都遵守 `BaseTokenToKVPoolAllocator` 的 `alloc/free/backup_state` 协议。
- `registry.py` 注册的是 **RadixCache 实现**的可插拔工厂（`--radix-cache-backend`），与注意力后端注册表正交；padding slot（页/slot 0）是分配器与池的共同约定。

## 7. 下一步学习建议

- 本讲聚焦「池多大、slot 怎么分」。slot 分配完后，**前缀复用与淘汰**由 RadixCache 负责，下一讲直接读 [u6-l2 RadixAttention 与 RadixCache](u6-l2-radix-cache.md)，看 `alloc_token_slots` 里调的 `evict_from_tree_cache` 与 `tree_cache` 到底怎么组织。
- 想深入统一内存池的 compaction 与 lazy 路径，精读 [multi_ended_allocator.py:1364-1567](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/mem_cache/multi_ended_allocator.py#L1364-L1567) 的 `_flush`，并对照 [u6-l3 缓存变体](u6-l3-cache-variants.md) 的 UnifiedRadixCache。
- 想理解 KV 迁移（PD 分离）时 slot 怎么跨节点搬运，可先记住本讲的 `req_to_token` / `out_cache_loc` 两个数据结构，再去读第 8 单元的 PD 分离讲义。
- 若对 DSV4 压缩注意力池（c4/c128/state 多池）感兴趣，精读 [pool_configurator.py:574-835](https://github.com/sgl-project/sglang/blob/59ef3b15cc86eb64c48cd5e687a95dbefb872a29/python/sglang/srt/model_executor/pool_configurator.py#L574-L835) 的 `DSV4PoolConfigurator`，那是 `coeff+bias` 模型在多池场景下的推广。
