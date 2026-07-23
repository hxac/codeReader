# Prefill 调度与 Chunked Prefill

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 **两把「尺子」** 如何共同约束一个 prefill 批：`token_budget`（每批最多算多少新 token，由 `max_extend_tokens` 控制）限制的是「算多少」，而 `reserved_size` 对 `available_size` 的预算检查限制的是「放多少请求进来」，二者管的是两件不同的事。
- 读懂 `PrefillAdder` 如何**逐个请求**做准入：先 `_try_allocate_one` 做资源估计与加锁，再 `_add_one_req` 切片落池，并用 `try_add_one` 把「首次准入」与「分块续算」两条路径合流。
- 理解 **`ChunkedReq` 的切分与跨批续算**——这是本讲最微妙的一点：一个 5 万 token 的 prompt 不会一次性塞进 GPU，而是被切成 `token_budget` 大小的块，分多个 prefill 批逐块算完；而「上一块算到哪」这个进度，是靠 `complete_one()` 改写同一个 `ChunkedReq` 对象的 `cached_len`「穿」到下一批的。
- 解释 **prefill 与 in-flight decode 的资源争用**：为什么 `PrefillAdder` 一开始就把 `decode_manager.inflight_tokens` 当作 `reserved_size` 的初值，从而在给 prefill 预留页之前先扣除掉正在 decode 的请求占用的显存。

本讲紧承 u4-l1。u4-l1 讲了主循环「收消息 → 调度 → 前向 → 处理结果」的骨架，并把 `_schedule_next_batch()` 当成一个黑盒。本讲打开这个黑盒里**前半段**——`PrefillManager.schedule_next_batch()` 如何挑出下一个 prefill 批；后半段（decode 批怎么挑、`table_idx` 槽位怎么分）留到 u4-l4。本讲不涉及「前向里算什么」（u5）与「页怎么分配回收」（u6），只关注**调度侧**如何在 token 预算与显存预算内挑选请求、并决定是否分块。

## 2. 前置知识

阅读本讲前，请确保你已经建立以下认知（来自前置讲义）：

- **Req 的长度计数器**（u2-l1）：`Req` 的核心字段 `cached_len`（已缓存）、`device_len`（逻辑游标）、`max_device_len`（上限）恒满足不变量 `0 <= cached_len < device_len <= max_device_len`；派生量 `extend_len = device_len - cached_len`（本轮新算的 token 数）、`remain_len = max_device_len - device_len`（还能 decode 多少）。`complete_one()` 的语义是「游标先走一步」：`cached_len = device_len; device_len += 1`。本讲的分块续算完全建立在这套计数器之上。
- **Batch 的 phase 标签**（u2-l1）：`Batch` 带 `phase` 字段，取值 `"prefill"` 或 `"decode"`，区分两种计算形状。本讲产出的是 `phase="prefill"` 的批。
- **主循环调用点**（u4-l1）：`_schedule_next_batch` 每轮被调用一次，它 `self.prefill_manager.schedule_next_batch(self.prefill_budget) or self.decode_manager.schedule_next_batch()`——**prefill 优先，没有 prefill 才 decode**。本讲解释前半个分支。
- **进程身份与同一批次**（u1-l4、u4-l2）：多卡下各 rank 必须独立调度出**完全相同**的 batch。本讲的调度逻辑在每个 rank 上**确定性**地运行（按 `pending_list` 顺序处理），所以各 rank 选出的 prefill 批天然一致。
- **前缀缓存接口**（u2-l1 提过 `cache_handle`，详细在 u6）：`CacheManager.match_req` 返回 `MatchResult.cuda_handle`，其 `cached_len` 表示「这个 prompt 有多长的前缀已经在 KV cache 里了」。本讲把 `cached_len > 0`（命中前缀）的情况纳入考虑，但匹配树本身的实现（Radix Cache）留到 u6-l2。

> 名词解释：**Chunked Prefill（分块预填充）** —— 当一个 prompt 比单批 token 预算还长时，不一次性把它全部 prefill（那样会 OOM 或拖慢其他请求），而是切成若干个 `token_budget` 大小的块，每个 prefill 批只算其中一块，跨多个批把整个 prompt 算完。它和「Chunked Prefill 让 prefill 与 decode 同批」是两个不同概念——后者 Mini-SGLang 目前用「prefill 优先」的简单策略替代（见 4.4 的 TODO 注释）。

一个直觉问题先放在脑子里：**一个 5 万 token 的长 prompt 来了，但单批只允许算 8192 个 token，这 5 万 token 是怎么被「挤」进若干个 prefill 批、且每块之间不丢不重的？** 带着这个问题读下去。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [python/minisgl/scheduler/prefill.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py) | Prefill 调度的全部逻辑 | `ChunkedReq`、`PrefillAdder`（`_try_allocate_one`/`_add_one_req`/`try_add_one`）、`PrefillManager`（`schedule_next_batch`/`add_one_req`/`abort_req`） |
| [python/minisgl/scheduler/utils.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/utils.py) | 调度器公用数据结构 | `PendingReq`（待办请求，携带 `chunked_req` 续算句柄）及其 `input_len`/`output_len` 派生属性 |
| [python/minisgl/scheduler/scheduler.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py) | Scheduler 主类 | `_schedule_next_batch` 的 prefill-or-decode 选择、`_process_one_msg` 入队、`_process_last_data` 跳过 `ChunkedReq`、`_prepare_batch` 里 `allocate_paged` |
| [python/minisgl/scheduler/decode.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py) | Decode 调度 | `inflight_tokens`（prefill 预算的 `reserved_size` 初值来源）、`filter_reqs` |
| [python/minisgl/scheduler/cache.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py) | 页分配与前缀缓存 | `available_size`、`match_req`、`lock`/`unlock`、`allocate_paged`（被 `_prepare_batch` 调用） |
| [python/minisgl/engine/engine.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py) | Engine 前向 | `forward_batch` 里对**所有** req（含 `ChunkedReq`）调用 `complete_one()`——这是续算进度的来源 |
| [python/minisgl/core.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py) | 公共数据结构 | `Req.complete_one`、`extend_len`/`remain_len`、`__post_init__` 的不变量断言 |

## 4. 核心概念与源码讲解

本讲按「先建立两把预算尺子的直觉，再看 `PrefillAdder` 如何逐请求准入与切片，再揭示 `ChunkedReq` 跨批续算的对象共享机制，最后看 `PrefillManager` 如何组装批并重排待办队列」的顺序展开，对应四个最小模块：**token budget**、**PrefillAdder**、**ChunkedReq**、**PrefillManager**。

### 4.1 token_budget 与 reserved_size：批大小的两把尺子

#### 4.1.1 概念说明

Prefill 调度要同时回答两个问题，它们由**两把不同的尺子**把关，初学者最容易把它们混为一谈：

1. **这一批最多算多少 token？** —— 由 `token_budget` 控制。它等于 `SchedulerConfig.max_extend_tokens`，默认 `8192`（见 [config.py:16](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/config.py#L16)）。每往批里加一段新 token，就从 `token_budget` 里扣掉相应长度，扣到 0 就不再加。这把尺子管的是**单批的计算量 / 公平性**——避免一个超大 prompt 独占一整批、饿死别人，也避免单批 token 过多拖慢迭代。

2. **这个请求能不能放进 KV cache？** —— 由 `reserved_size` 对 `available_size` 的预算检查控制。一个请求不光要「算」，算完的 K/V 还要**长期驻留**在 KV cache 里供后续 decode 读取，因此准入它之前必须确认：把它整个 prompt + 整个输出都装下之后，剩余显存还够不够。这把尺子管的是**显存安全**——防止一次性放进太多请求导致 OOM。

关键直觉：**`token_budget` 限制「算多少」（分块），`reserved_size` 限制「放多少请求进来」（准入）**。一个 5 万 token 的 prompt，只要显存装得下整个 5 万 + 输出（第 2 把尺子通过），就可以被**准入**；但它不会被一次算完，而是受第 1 把尺子约束，每批只算 8192 个，分多批算完。

还有第三层考虑：**正在 decode 的请求也占着显存**。如果无视它们，给 prefill 预留的页就可能和 decode 抢同一块显存。所以 `reserved_size` 的**初值不是 0**，而是 `decode_manager.inflight_tokens`——先把 in-flight decode 的占用扣掉，再开始给 prefill 估预算。这就是「prefill 与 in-flight decode 的资源争用估计」。

#### 4.1.2 核心流程

两把尺子在一次 `schedule_next_batch` 里的协作（伪代码）：

```
schedule_next_batch(prefill_budget):
  adder = PrefillAdder(
      token_budget   = prefill_budget,            # 尺子1: 本批最多算这么多(8192)
      reserved_size  = decode_manager.inflight_tokens,  # 尺子2初值: 先扣除在途 decode
      cache_manager, table_manager,
  )
  for pending_req in pending_list:
      if adder.try_add_one(pending_req) 成功:
          把生成的 Req 加入本批
      else:
          break                                  # 任一把尺子不达标就停,不再加更多请求
```

`try_add_one` 内部，两把尺子的分工是：

- 尺子 1（`token_budget`）：先查 `if self.token_budget <= 0: return None`；切片时 `chunk_size = min(token_budget, remain_len)` 并 `token_budget -= chunk_size`。
- 尺子 2（`reserved_size` vs `available_size`）：只在**首次准入**时查（`_try_allocate_one`），估算 `estimated_len = 整段剩余 prompt + 输出`，要求 `estimated_len + reserved_size <= available_size`；通过后 `reserved_size += 整段剩余 prompt + 输出`，把这份占用「记上账」。

注意一个**不对称**：尺子 2 只在请求**首次**被准入时检查一次；之后的分块续算**不再查尺子 2**（见 4.2.1）。这是因为整段显存在首次准入时已经一次性「付清」，分块只是在已预留的空间里逐段写入，无需重复检查。

#### 4.1.3 源码精读

`token_budget` 的来源与默认值：

[python/minisgl/scheduler/config.py:16](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/config.py#L16) —— `max_extend_tokens: int = 8192`。它就是每个 prefill 批的 token 预算上限。[python/minisgl/scheduler/config.py:36-37](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/config.py#L36-L37) 还用 `@property` 把 `max_forward_len` 也指向它，说明「单批最大前向长度」与「分块大小」在 Mini-SGLang 里是同一个值。对应的 CLI 开关是 `--max-prefill-length`（或别名 `--max-extend-length`），其 help 文案直说 "Chunk Prefill maximum chunk size in tokens"（见 [args.py:164-171](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L164-L171)）。

`PrefillAdder` 的构造与 `reserved_size` 初值：

[python/minisgl/scheduler/prefill.py:131-136](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L131-L136) —— `schedule_next_batch` 里构造 `PrefillAdder`，把 `prefill_budget` 传给 `token_budget`，把 **`self.decode_manager.inflight_tokens`** 传给 `reserved_size`。这一行就是「先扣除在途 decode 占用」的体现。

`inflight_tokens` 的算法：

[python/minisgl/scheduler/decode.py:27-30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py#L27-L30) —— `inflight_tokens = sum(req.remain_len for req in running_reqs) + (page_size - 1) * len(running_reqs)`。前半段是所有正在 decode 的请求「还要生成多少 token」（未来要占的显存），后半段 `(page_size-1)*N` 是按页对齐的预留余量（每个请求留 1 页的空位，见 u4-l4）。把这个值作为 `reserved_size` 初值，意味着 prefill 准入检查**站在 decode 已经/即将占用显存之后**做估计。

`available_size` 的口径（尺子 2 的右侧）：

[python/minisgl/scheduler/cache.py:32-34](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L32-L34) —— `available_size = prefix_cache.size_info.evictable_size + len(free_slots) * page_size`，即「可被淘汰的前缀缓存」加「完全空闲的页」。注意它只数 `evictable_size`，不含被锁住（protected）的部分——这与下面 `lock` 会让 `available_size` 下降直接相关。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：验证「两把尺子管两件不同的事」。
2. **步骤**：
   - 在 [prefill.py:131-136](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L131-L136) 旁标注：`token_budget`（尺子 1）来自参数，`reserved_size`（尺子 2 初值）来自 `inflight_tokens`。
   - 跟着 [decode.py:27-30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py#L27-L30) 算一笔：假设 `page_size=1`、当前有 4 个 running 请求、`remain_len` 分别为 100/200/300/400，那么 `inflight_tokens = 1000 + 0 = 1000`。这就是本轮 prefill 的 `reserved_size` 起点值。
3. **需要观察的现象**：`reserved_size` 不是从 0 开始，而是「背负」着 decode 的在途占用。
4. **预期结果**：你能说清「为什么 prefill 准入要先看 decode 占了多少」——否则 prefill 与 decode 会争抢同一块显存。
5. 纯阅读即可，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `max_extend_tokens` 调得很大（比如 100000），分块行为会怎样？会有什么风险？
**答案**：`token_budget` 变大后，单个长 prompt 更可能在**一批内**算完（`chunk_size = min(token_budget, remain_len)` 更容易等于 `remain_len`，于是 `is_chunked` 为假，不分块）。好处是减少跨批开销；风险是单批 token 数暴涨，prefill 迭代变慢、显存峰值升高，且会**饿死**排在后面的请求（一批被一个长 prompt 占满）。这就是为什么要设一个合理上限（默认 8192）。

**练习 2**：为什么 `reserved_size` 的初值用 `inflight_tokens` 而不是 `len(running_reqs)`？
**答案**：`inflight_tokens` 估的是 decode **未来还要占用多少 token 位**（`remain_len` 之和加页对齐余量），这才是和 prefill 抢显存的真正量；`len(running_reqs)` 只是请求数，和显存占用没有直接换算关系，不能反映争用强度。

### 4.2 PrefillAdder：准入与切片的核心

#### 4.2.1 概念说明

`PrefillAdder` 是一个一次性的「累加器」：每个 prefill 批创建一个，持有一把会随添加递减的 `token_budget` 和一把会随添加递增的 `reserved_size`，逐个尝试把 `pending_list` 里的请求「加」进当前批。它对外只有一个入口 `try_add_one(pending_req)`，但内部区分两条路径：

- **首次准入路径**：请求第一次被处理（`pending_req.chunked_req` 为 `None`）。先调 `_try_allocate_one` 做资源估计与前缀匹配、加锁、分配 `table_idx`；成功后再调 `_add_one_req` 切片落池、构造 `Req`/`ChunkedReq`。
- **分块续算路径**：请求上一批没算完（`pending_req.chunked_req` 已指向一个 `ChunkedReq`）。**跳过** `_try_allocate_one`（资源已在首次准入时付清），直接用上一块留下的 `cached_len`/`table_idx`/`cache_handle` 调 `_add_one_req` 切下一块。

这条「续算跳过资源检查」的不对称是刻意设计：避免在 prefill 中途因为资源波动而拒绝续算（那会导致请求卡死、已分配资源泄漏）。资源安全在**准入那一刻**一次性把关，之后只受 `token_budget` 约束。

`try_add_one` 还有一个最早的短路：`if self.token_budget <= 0: return None`。一旦本批 token 预算耗尽，无论首准还是续算都立刻返回 `None`，由 `PrefillManager` 据此 `break` 停止再加请求。

#### 4.2.2 核心流程

`try_add_one` 的决策（伪代码）：

```
try_add_one(pending_req):
    if token_budget <= 0:              return None        # 尺子1耗尽,立刻停

    if pending_req.chunked_req 存在:                        # 续算路径
        return _add_one_req(pending_req,
                            cache_handle = chunked_req.cache_handle,
                            table_idx    = chunked_req.table_idx,
                            cached_len   = chunked_req.cached_len)   # 上一块留下的进度

    if 资源 = _try_allocate_one(pending_req):               # 首次准入路径
        cache_handle, table_idx = 资源
        return _add_one_req(pending_req, cache_handle, table_idx,
                            cached_len = cache_handle.cached_len)     # 命中前缀的长度

    return None                                              # 资源不足,放弃
```

`_try_allocate_one` 的准入检查（伪代码）：

```
_try_allocate_one(req):
    if table_manager.available_size == 0:   return None     # 连一个槽位都没了
    handle  = cache_manager.match_req(req).cuda_handle
    cached_len  = handle.cached_len                         # 前缀命中长度(0 表示没命中)
    extend_len  = req.input_len - cached_len                # 整段剩余 prompt
    estimated_len = extend_len + req.output_len             # 整段 prompt + 整段输出

    if estimated_len + reserved_size > available_size:  return None   # 尺子2(加锁前)
    cache_manager.lock(handle)                                         # 锁住命中前缀
    if estimated_len + reserved_size > available_size:                 # 尺子2(加锁后再查一次)
        return cache_manager.unlock(handle)                           #   锁完不够,解锁放弃
    table_idx = table_manager.allocate()                    # 分配一个 table 槽位
    if cached_len > 0:  把命中前缀的 device_ids / page_entry 拷进池   # 前缀命中才做
    return handle, table_idx
```

`_add_one_req` 的切片落池（伪代码）：

```
_add_one_req(pending_req, cache_handle, table_idx, cached_len):
    remain_len = pending_req.input_len - cached_len         # 还剩多少没算
    chunk_size = min(token_budget, remain_len)              # ★本块大小
    is_chunked = chunk_size < remain_len                    # ★是否还需要再分
    CLS = ChunkedReq if is_chunked else Req
    token_budget  -= chunk_size                             # 尺子1 扣减
    reserved_size += remain_len + pending_req.output_len    # 尺子2 记账(整段,非单块)
    把 input_ids[cached_len : cached_len+chunk_size] 拷进 token_pool[table_idx, 同切片]
    return CLS(input_ids[:cached_len+chunk_size], table_idx, cached_len, ...)
```

#### 4.2.3 源码精读

`_try_allocate_one` 全貌：

[python/minisgl/scheduler/prefill.py:39-63](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L39-L63) —— 逐段说明：

- **第 40-41 行（槽位短路）**：`table_manager.available_size == 0` 时直接返回 `None`。`available_size` 这里是 `TableManager` 的属性（见 [table.py:13-15](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/table.py#L13-L15)，即空闲的 `table_idx` 槽位数），与尺子 2 的 `cache_manager.available_size`（页/token 口径）是**两个不同东西**，别混淆。
- **第 44-48 行（前缀匹配 + 估算）**：`match_req` 返回命中前缀的 `handle`，`cached_len = handle.cached_len`；`extend_len = req.input_len - cached_len` 是**整段**剩余 prompt（不是单块），`estimated_len = extend_len + req.output_len` 把整段输出也算进去。注释里的两个 `TODO`（host cache、better estimate policy）说明这是保守上界估计。
- **第 50-54 行（加锁前后双重检查）**：这是本节的精妙处。`lock(handle)` 会把命中前缀的页从「可淘汰」挪到「受保护」，导致 `cache_manager.available_size`（只数 evictable）**下降**。所以先查一次（第 50 行），通过后 `lock`（第 52 行），再查一次（第 53 行）——锁完若超限，就用 `return self.cache_manager.unlock(handle)` 一行同时完成「解锁 + 返回 None 放弃」。`unlock` 本身返回 `None`，正好作为「放弃」信号。
- **第 56-61 行（落池命中前缀）**：仅当 `cached_len > 0`（命中前缀）时，把命中那段的 `device_ids` 与 `page_entry` 从请求和 handle 拷进 `token_pool`/`page_table` 的对应位置，让后续前向能读到这段已缓存的 K/V。

`lock`/`unlock` 的实现：

[python/minisgl/scheduler/cache.py:36-40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L36-L40) —— `lock`/`unlock` 都委托给 `prefix_cache.lock_handle(handle, unlock=...)`。正如 u6-l1 的接口契约所述：被锁的 handle 不会被 `evict` 淘汰，且锁定只改 `size_info`、不改缓存内容。

`_add_one_req` 全貌：

[python/minisgl/scheduler/prefill.py:65-90](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L65-L90) —— 逐段说明：

- **第 72-76 行（决定本块大小与是否分块）**：`chunk_size = min(self.token_budget, remain_len)`，`is_chunked = chunk_size < remain_len`。若预算够覆盖整段剩余，就不分块（`CLS = Req`）；否则分块（`CLS = ChunkedReq`）。随后 `token_budget -= chunk_size` 扣减尺子 1。
- **第 77 行（尺子 2 记账）**：`reserved_size += remain_len + pending_req.output_len`。注意加的是**整段** `remain_len`（不是 `chunk_size`），即「这个请求最终要占多少」一次性记满。这也是为何续算时无需再查尺子 2——账在首次准入时已结清。
- **第 78-81 行（切片拷贝）**：`_slice = slice(cached_len, cached_len + chunk_size)`，把这一块 `input_ids` 从 CPU（`.pin_memory()` 锁页内存）异步拷进 `token_pool[table_idx, _slice]`。注释说明只拷 token ids，新页的分配发生在调度器后续的 `_prepare_batch`（见 [scheduler.py:206](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L206) 的 `allocate_paged`）。
- **第 82-90 行（构造 Req/ChunkedReq）**：`input_ids=pending_req.input_ids[:cached_len+chunk_size]`——注意传入的是**从头到本块末尾**的连续切片（含已缓存段），这样 `Req.__post_init__` 算出的 `device_len = len(input_ids) = cached_len + chunk_size`，于是 `extend_len = device_len - cached_len = chunk_size`，正是本块要新算的量（见 [core.py:38-50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L38-L50)）。

`try_add_one` 的合流：

[python/minisgl/scheduler/prefill.py:92-113](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L92-L113) —— 第 93-94 行是尺子 1 短路；第 96-102 行是续算路径（直接读 `chunked_req` 的三个字段进 `_add_one_req`）；第 104-111 行是首次准入路径（`_try_allocate_one` 成功后用 `cache_handle.cached_len` 进 `_add_one_req`）；第 113 行两路都失败才返回 `None`。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：把「首次准入 vs 续算」两条路径在代码上对齐，看清续算为何跳过资源检查。
2. **步骤**：
   - 在 [prefill.py:96-102](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L96-L102) 标注「续算：直接 `_add_one_req`，无 `_try_allocate_one`」；在 [prefill.py:104-111](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L104-L111) 标注「首次：先 `_try_allocate_one` 再 `_add_one_req`」。
   - 追问：续算路径连 `table_manager.allocate()` 都不调，为什么 `table_idx` 还有效？答案：`table_idx` 是首次准入时分配的（[prefill.py:56](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L56)），续算复用同一个 `chunked_req.table_idx`，整段 prompt 的 K/V 都写进这一行的页表里（见 u4-l4 的二维 `page_table`）。
3. **需要观察的现象**：续算只消耗 `token_budget`，不再触碰尺子 2。
4. **预期结果**：你能解释「资源检查只做一次」既保证了显存安全，又避免了续算被拒导致的泄漏。
5. 纯阅读即可。

#### 4.2.5 小练习与答案

**练习 1**：`_try_allocate_one` 里为什么要「加锁前查一次、加锁后再查一次」？
**答案**：`lock(handle)` 会把命中前缀的页从 evictable 挪到 protected，使 `available_size`（只计 evictable）下降。加锁前的检查看不到这个下降，可能误判「够」；加锁后必须再查一次才能反映真实可用量。若锁完不够，就 `unlock(handle)` 解锁并返回 `None` 放弃，保持「没准入就不留锁」。

**练习 2**：`estimated_len` 为什么用 `extend_len + output_len`（整段），而不是用单块 `chunk_size + output_len`？
**答案**：因为准入决策要保证**整个请求**（整段 prompt + 整段输出）最终都装得下。如果按单块估，一个 5 万 token 的 prompt 会被误认为「只需装 8192」，于是放进来一堆长 prompt，等到后续块要写入时显存早已不够，引发 OOM。用整段估是保守上界，宁可少放不可超放。

### 4.3 ChunkedReq：分块续算的载体

#### 4.3.1 概念说明

`ChunkedReq` 是 `Req` 的子类，用来表示「这个 prefill 块不是请求的最后一块」。它只覆写两个成员：

- `append_host` 直接 `raise NotImplementedError("ChunkedReq should not be sampled")`——分块中间产生的「下一个 token」是无意义的（prompt 还没喂完），不能追加。
- `can_decode` 恒为 `False`——避免它被误加进 decode manager。

那「跨批续算」到底是怎么发生的？这是本讲最微妙、也最容易读漏的一点。答案藏在**对象共享**里：同一个 `ChunkedReq` 对象，同时被两个地方引用——

1. 它被放进 `batch.reqs`，于是 Engine 在 `forward_batch` 里会对它调用 `complete_one()`；
2. 它也被存回 `pending_req.chunked_req`（见 [prefill.py:141-144](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L141-L144)）。

由于 `complete_one()` 会改写 `self.cached_len = self.device_len`（见 [core.py:52-54](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L52-L54)），而这个对象又恰好是下一批 `try_add_one` 读的那个 `chunked_req`——于是「上一块算到哪」这个进度，就**通过同一个对象的 `cached_len` 字段，从 Engine 穿回了 Scheduler**。下一批据此切出新的一块。

换句话说：**续算进度不靠任何额外数据结构传递，而是靠 `complete_one()` 改写共享对象的 `cached_len` 这一副作用**。读到这一层，分块续算就通了。

> 为什么 `complete_one` 对 `ChunkedReq` 也照常调用？因为它在 [engine.py:199-200](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L199-L200) 是 `for req in batch.reqs: req.complete_one()`，**不区分** Req 还是 ChunkedReq。`device_len += 1` 那一步对 ChunkedReq 是无意义的（下一块会重新构造对象、重新由 `__post_init__` 算 `device_len`），但 `cached_len = device_len` 这一步恰好留下了我们需要的进度。

#### 4.3.2 核心流程

一个 5 万 token 的 prompt（无前缀命中、`cached_len=0`）在 `token_budget=8192` 下的续算时序（只画 req3，假设它独占后续批次）：

```
批1 (token_budget=8192，与 req1/req2 共享，这里只看 req3):
  首次准入: cached_len=0, remain=50000, chunk=min(2692,50000)=2692  # 预算被 req1/req2 用掉一些
           is_chunked=(2692<50000)=True → ChunkedReq_A(cached_len=0, device_len=2692)
           存回 pending_req3.chunked_req = ChunkedReq_A
  Engine forward: complete_one(ChunkedReq_A) → cached_len=2692   ★进度穿回

批2 (token_budget=8192,全新):
  续算: 读 chunked_req.cached_len=2692, remain=50000-2692=47308
        chunk=min(8192,47308)=8192, is_chunked=True → ChunkedReq_B(cached_len=2692, device_len=10884)
        存回 pending_req3.chunked_req = ChunkedReq_B
  Engine forward: complete_one(ChunkedReq_B) → cached_len=10884  ★进度穿回

批3..批6: 每批 chunk=8192，cached_len 依次 10884→19076→27268→35460→43652

批7 (token_budget=8192):
  续算: 读 cached_len=43652, remain=6348
        chunk=min(8192,6348)=6348, is_chunked=(6348<6348)=False → 普通 Req(最后一块!)
        pending_req3.chunked_req = None   # 不再续算
  Engine forward: complete_one → 产出第一个输出 token
  _process_last_data: 不是 ChunkedReq,走 cache_req(finished=False) → 整段前缀入缓存,转入 decode
```

要点：①每个块只算 `[cached_len, cached_len+chunk)` 这一段；之前各块算出的 K/V 已在 KV cache 里，靠 `page_table` 读回（前向的 attention kernel 负责，u7）；②`is_chunked` 为假的最后一块是普通 `Req`，它会产出首个输出 token、把整段 prompt 缓存进前缀树、并转入 decode。

#### 4.3.3 源码精读

`ChunkedReq` 类定义：

[python/minisgl/scheduler/prefill.py:23-29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L23-L29) —— `class ChunkedReq(Req)`。[第 24-25 行](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L24-L25) `append_host` 抛 `NotImplementedError`，[第 27-29 行](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L27-L29) `can_decode` 恒 `False`。这两个覆写正是 `_process_last_data` 必须「跳过 ChunkedReq」的原因——否则 `append_host` 会直接抛异常。

`is_chunked` 的判定与对象构造：

[python/minisgl/scheduler/prefill.py:72-75](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L72-L75) —— `is_chunked = chunk_size < remain_len`，据此 `CLS = ChunkedReq if is_chunked else Req`（[第 75 行](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L75)），随后 [第 82-90 行](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L82-L90) 用 `CLS(...)` 构造。注意每批都构造一个**新**对象——但新对象的 `cached_len` 是从上一批的对象（经 `complete_one` 改写后）读来的。

进度回传的源头——Engine 对所有 req 调 `complete_one`：

[python/minisgl/engine/engine.py:191-206](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L191-L206) —— `forward_batch`。[第 199-200 行](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L199-L200) `for req in batch.reqs: req.complete_one()` 在采样**之前**执行，且**不区分** Req/ChunkedReq。这就是「进度穿回」的物理动作。[core.py:52-54](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L52-L54) 的 `complete_one` 把 `cached_len` 置为 `device_len`。

调度侧把对象「同时」放进 batch 与 pending：

[python/minisgl/scheduler/prefill.py:139-147](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L139-L147) —— `if req := adder.try_add_one(pending_req):` 成功后：先 `pending_req.chunked_req = None`（清掉旧的），再 `if isinstance(req, ChunkedReq): pending_req.chunked_req = req`（把**同一个** `req` 存回），同时 `reqs.append(req)`（也把**同一个** `req` 放进批）。两条引用指向同一对象，是进度能穿回的前提。

结果处理时跳过 ChunkedReq：

[python/minisgl/scheduler/scheduler.py:138-167](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L138-L167) —— `_process_last_data`。[第 148-149 行](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L148-L149) `if isinstance(req, ChunkedReq): continue`，对中间块**不** `append_host`、**不** `cache_req`、**不**发 `DetokenizeMsg`——它们没有有意义的输出，也不该中途把半截 prompt 插进前缀缓存。只有最后一块（普通 `Req`）走到 [第 163-164 行](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L163-L164) 的 `cache_req(req, finished=False)`，把整段 prompt 一次性缓存并转入 decode。

#### 4.3.4 代码实践（重点：手动推演分块）

这是本讲核心实践之一，对应规格里的练习任务（完整版见第 5 节）。

1. **目标**：手算一个 50000 token 的请求在 `token_budget=8192` 下被切成几块、每块的 `ChunkedReq.cached_len` 是多少。
2. **步骤**：
   - 假设无前缀命中（`cached_len=0`），且后续批次它独占预算（每批 `chunk=8192`）。
   - 从 `cached_len=0` 开始：第 1 块因与别请求共享预算只拿到 2692（见第 5 节完整场景），后续每块 8192，直到剩余 ≤ 8192 时切最后一块。
   - 逐块记录 `(cached_len, chunk_size, 区间, 是否 ChunkedReq)`。
3. **需要观察的现象**：`cached_len` 每批累加 `chunk_size`；最后一块 `chunk_size < remain_len` 为假，变成普通 `Req`。
4. **预期结果**：req3 共被切 7 块——`2692, 8192, 8192, 8192, 8192, 8192, 6348`（前 6 块是 ChunkedReq，最后 6348 是普通 Req），总和 `2692 + 8192×5 + 6348 = 50000`。
5. 若本地有 GPU，可启动服务喂一个超长 prompt 并在 `_add_one_req` 临时加日志打印 `cached_len/chunk_size/is_chunked` 对照；无 GPU 则结论由阅读得出，数值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果 `complete_one()` 里只做 `device_len += 1`、**不**做 `cached_len = device_len`，分块续算会出什么问题？
**答案**：`cached_len` 永远停在 0，下一批 `try_add_one` 读到的还是 0，于是 `_add_one_req` 算出 `remain_len = 50000`、`chunk_size = 8192`、区间又是 `[0, 8192)`——**重复计算开头那一段**，且永远算不完。`cached_len = device_len` 这一句是把进度写回的关键。

**练习 2**：为什么 `ChunkedReq.can_decode` 要返回 `False`？
**答案**：分块中间的块不是请求的终点，不应该进入 decode manager 去逐 token 生成。返回 `False` 后，`DecodeManager.filter_reqs`（[decode.py:14-15](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py#L14-L15)，用 `if req.can_decode` 过滤）会把它排除在 running 集合外，确保只有最后一块（普通 `Req`，`can_decode=True`）才真正进入 decode。

### 4.4 PrefillManager：批次组装与待办队列重排

#### 4.4.1 概念说明

`PrefillManager` 是 prefill 调度的对外门面，持有三样东西：一个 `pending_list`（待办请求队列，元素是 `PendingReq`）、以及 `cache_manager`/`table_manager`/`decode_manager` 三个依赖。它对外暴露四个方法：

- `add_one_req(UserMsg)`：从一条入站消息造一个 `PendingReq` 压队尾——这是请求进入 prefill 视野的入口。
- `schedule_next_batch(prefill_budget)`：本讲的主角，按预算从 `pending_list` 里挑出一批，返回 `Batch(phase="prefill")`。
- `abort_req(uid)`：取消某请求，返回它的 `chunked_req`（若有）供上层释放资源。
- `runnable`（property）：`pending_list` 非空即真，被主循环用来判断「是否需要阻塞等待新消息」（u4-l1）。

`schedule_next_batch` 有一个关键的**队列重排**动作：处理完一轮后，`pending_list` 被重排为「仍需续算的 ChunkedReq 请求」+「本轮没轮到的请求」。前者被**挪到队头**，保证长 prompt 的下一块在下一批优先续算（续算只花 `token_budget`、几乎必定成功）。这意味着一个正在进行分块的长 prompt 会**连续占用**若干个 prefill 批，直到算完——这是当前简单策略的取舍（代码里也留了 `TODO: support other policies` 的注释）。

#### 4.4.2 核心流程

`schedule_next_batch` 的全流程（伪代码）：

```
schedule_next_batch(prefill_budget):
    if pending_list 为空: return None
    adder = PrefillAdder(token_budget=prefill_budget, reserved_size=inflight_tokens, ...)
    reqs, chunked_list = [], []
    for pending_req in pending_list:            # 按队列顺序逐个试
        if req := adder.try_add_one(pending_req):
            pending_req.chunked_req = None
            if req 是 ChunkedReq:
                pending_req.chunked_req = req    # 记下续算句柄
                chunked_list.append(pending_req)
            reqs.append(req)
        else:
            break                                # ★一旦加不进,立刻停止(不再试后面的)
    if reqs 为空: return None
    pending_list = chunked_list + pending_list[len(reqs):]   # 续算优先 + 未轮到的
    return Batch(reqs=reqs, phase="prefill")
```

注意三个要点：①遇到第一个「加不进」的请求就 `break`，**不**继续尝试后面的——即「队头阻塞」，前一个请求因为预算/资源不够加不进，后面也一律不加（保证批内请求在 `pending_list` 里连续，便于重排）；②`len(reqs)` 恰好等于「从队头连续成功消费的请求数」，所以 `pending_list[len(reqs):]` 正好是「没轮到的尾巴」；③续算请求被挪到新队头，但它们本来就在前 `len(reqs)` 个里，所以是「提取出来再前置」。

请求从入站到入队的路径：

```
UserMsg 到达 scheduler._process_one_msg
  → 校验长度(max_seq_len)、夹紧 max_tokens
  → prefill_manager.add_one_req(msg)
       → PendingReq(uid, input_ids, sampling_params) 压入 pending_list 尾部
```

#### 4.4.3 源码精读

`PendingReq` 数据结构：

[python/minisgl/scheduler/utils.py:14-27](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/utils.py#L14-L27) —— `PendingReq` 持有 `uid`/`input_ids`/`sampling_params` 和一个初值为 `None` 的 `chunked_req`（续算句柄）。`input_len` 是 `len(input_ids)`，`output_len` 是 `sampling_params.max_tokens`——这两个派生属性正是 4.1/4.2 里预算估算用到的。

`add_one_req` 入口与上游校验：

[python/minisgl/scheduler/prefill.py:123-124](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L123-L124) —— `add_one_req` 就是 `self.pending_list.append(PendingReq(...))`。调用点在 [scheduler.py:189](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L189)（`_process_one_msg` 里）。上游 [scheduler.py:177-188](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L177-L188) 先算 `max_output_len = max_seq_len - input_len`，超长直接丢弃、`max_tokens` 超过则夹紧——所以 `PendingReq.output_len` 不会把请求撑爆 `max_seq_len`。

`schedule_next_batch` 全貌：

[python/minisgl/scheduler/prefill.py:126-151](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L126-L151) —— 逐段说明：

- **第 127-128 行**：空队直接返回 `None`。
- **第 131-136 行**：构造 `PrefillAdder`（4.1 已展开）。
- **第 139-147 行**：主循环。`try_add_one` 成功就记录并按是否 ChunkedReq 分流（4.3 已展开）；失败就 `break`。
- **第 148-149 行**：一个都没加进就返回 `None`（比如第一个请求就因资源不足被拒）。
- **第 150 行（队列重排）**：`self.pending_list = chunked_list + self.pending_list[len(reqs):]`。续算请求前置；`pending_list[len(reqs):]` 是「从队头数 `len(reqs)` 个之后」的剩余请求，即「没被本轮消费的尾巴」。
- **第 151 行**：返回 `Batch(reqs=reqs, phase="prefill")`。

`_schedule_next_batch` 的 prefill-or-decode 选择：

[python/minisgl/scheduler/scheduler.py:219-225](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L219-L225) —— `batch = self.prefill_manager.schedule_next_batch(self.prefill_budget) or self.decode_manager.schedule_next_batch()`。注释 `# TODO: support other policies: e.g. DECODE first` 明确说明这是「prefill 优先」的简单策略。这意味着只要 `pending_list` 非空且资源允许，就**一直 prefill**，decode 会被推迟——这是 Mini-SGLang 有意简化的部分（生产版 SGLang 会更细粒度地交织 prefill 与 decode）。

`abort_req`：

[python/minisgl/scheduler/prefill.py:153-158](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L153-L158) —— 扫 `pending_list` 找 `uid` 匹配项，`pop` 掉并返回它的 `chunked_req`。注意：若该请求正在分块续算（`chunked_req` 非 None），返回的句柄带有 `table_idx`/`cache_handle`，调用方（[scheduler.py:192-195](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L192-L195)）据此 `_free_req_resources` 释放已分配的页与槽位，避免泄漏。

`runnable`：

[python/minisgl/scheduler/prefill.py:160-162](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L160-L162) —— `len(self.pending_list) > 0`。被 u4-l1 的 `overlap_loop`/`normal_loop` 用来决定 `receive_msg(blocking=...)`：有 prefill（或 decode）可跑时就不阻塞等消息。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：验证「队头阻塞 + 续算前置」的队列重排语义。
2. **步骤**：
   - 假设 `pending_list = [A, B, C, D]`，本轮 A、B 成功加入，C 因资源不足 `try_add_one` 返回 None 而 `break`，D 没被试到。
   - 推演：`reqs = [A的req, B的req]`，`len(reqs)=2`；若 A、B 都不是 ChunkedReq，`chunked_list=[]`；新 `pending_list = [] + pending_list[2:] = [C, D]`。
   - 再推演：若 A 是 ChunkedReq（长 prompt 第一块），B 是普通 Req，C 失败。则 `chunked_list=[A]`，`reqs=[A的req, B的req]`（`len=2`），新 `pending_list = [A] + pending_list[2:] = [A, C, D]`——A 被前置续算。
3. **需要观察的现象**：续算请求（A）被挪到新队头，下一批优先处理；失败请求（C）紧随其后。
4. **预期结果**：你能说清 `pending_list[len(reqs):]` 为何正好是「未轮到的尾巴」——因为 `len(reqs)` 就是连续成功消费的个数。
5. 纯阅读即可。

#### 4.4.5 小练习与答案

**练习 1**：`schedule_next_batch` 里为什么遇到第一个失败就 `break`，而不是跳过它继续试后面的请求？
**答案**：这是「队头阻塞」式的简单策略。原因有二：一是失败通常意味着「预算/资源不够」，后面的请求大概率也加不进；二是若跳过队头继续加后面的，会破坏 `pending_list` 的连续性，使第 150 行 `pending_list[len(reqs):]` 的「尾巴」语义失效（被跳过的请求既没进批、也不在尾巴里，会丢失）。`break` 保证了「进批的请求在队列里始终是连续前缀」。

**练习 2**：一个长 prompt 正在分块续算时，新来的短请求会被立刻 prefill 吗？
**答案**：不一定。续算请求被前置到队头，每批优先消耗 `token_budget` 续算；只要长 prompt 没算完（且预算被它占满），新短请求就会因 `token_budget <= 0` 被短路、轮不到。这是当前「prefill 优先 + 续算前置」策略的取舍，代码以 `TODO: support other policies` 标注了未来改进方向。

## 5. 综合实践

把本讲四个模块串起来，做一个 **「3 个请求在 `max_extend_tokens=8192` 下的分块全过程手算」** 任务，对应规格里的练习任务。

**场景**：`pending_list` 依次到来 3 个请求，`input_len` 分别为 **500 / 5000 / 50000**，均无前缀命中（`cached_len=0`），`output_len`（`max_tokens`）足够小、不影响资源准入（假设 `available_size` 充足，本任务只聚焦分块计数）。每批 `token_budget = 8192`。

**步骤**：

1. **第 1 批（`token_budget=8192`，`reserved_size=inflight_tokens=0`）**：按 `pending_list` 顺序逐个 `try_add_one`。
   - req1（500）：`remain=500`，`chunk=min(8192,500)=500`，`is_chunked=(500<500)=False` → 普通 Req，算 `[0,500)`。`token_budget: 8192→7692`。
   - req2（5000）：`remain=5000`，`chunk=min(7692,5000)=5000`，`is_chunked=False` → 普通 Req，算 `[0,5000)`。`token_budget: 7692→2692`。
   - req3（50000）：`remain=50000`，`chunk=min(2692,50000)=2692`，`is_chunked=(2692<50000)=True` → **ChunkedReq**，算 `[0,2692)`。`token_budget: 2692→0`。
   - 队列无更多请求，循环结束。`reqs=[req1, req2, req3块1]`，`chunked_list=[req3]`。
   - 重排：`pending_list = [req3] + [] = [req3]`（req1/req2 已 prefill 完，离开待办；req3 续算前置）。
   - **批 1 = {req1 全(500), req2 全(5000), req3 块1(2692)}**。本批 token 总数 `500+5000+2692=8192`，正好用满预算。

2. **第 2~6 批（每批 `token_budget=8192`，`pending_list=[req3]`）**：req3 续算，每批 `chunk=8192`。
   - 批 2：`cached_len`（经批 1 的 `complete_one`）= 2692，`remain=47308`，`chunk=8192`，算 `[2692,10884)`，ChunkedReq。
   - 批 3：`cached_len=10884`，`chunk=8192`，算 `[10884,19076)`。
   - 批 4：`cached_len=19076`，`chunk=8192`，算 `[19076,27268)`。
   - 批 5：`cached_len=27268`，`chunk=8192`，算 `[27268,35460)`。
   - 批 6：`cached_len=35460`，`chunk=8192`，算 `[35460,43652)`。
   - 每批都是 `is_chunked=True`（因为 `8192 < remain`），都是 ChunkedReq。

3. **第 7 批**：`cached_len=43652`，`remain=50000-43652=6348`，`chunk=min(8192,6348)=6348`，`is_chunked=(6348<6348)=False` → **普通 Req（最后一块）**，算 `[43652,50000)`。
   - 该批 forward 后 `_process_last_data` 不再跳过它：产出第一个输出 token、`cache_req(finished=False)` 把整段 `[0,50000)` 一次性写入前缀缓存、转入 decode。
   - `pending_req3.chunked_req = None`，req3 离开待办队列。

**需要观察的现象**：

- **每批的 ChunkedReq 标记**：批 1~批 6 里 req3 的块是 `ChunkedReq`（共 6 个 ChunkedReq 对象）；批 7 的最后一块是普通 `Req`。
- **进度如何传递**：批 N 的 `cached_len` = 批 N-1 的 `complete_one` 写入值（2692→10884→19076→27268→35460→43652）。
- **token 守恒**：req3 总长 `2692 + 8192×5 + 6348 = 50000`；三请求总 input `500+5000+50000 = 55500`，等于各批 token 之和 `8192(批1) + 8192×5(批2-6) + 6348(批7) = 55500`。
- **prefill 优先的副作用**：批 2~批 7 期间 req1/req2 虽已 prefill 完、在 decode，但每轮 `_schedule_next_batch` 都因 req3 还在 `pending_list` 而**优先 prefill**，decode 被推迟——这正是「prefill 优先 + 续算前置」策略的体现（见 4.4 的 TODO）。

**预期结果（汇总表）**：

| 批次 | token_budget 起点 | 本批 reqs | req3 的块 | req3 区间 | req3 是否 ChunkedReq |
| --- | --- | --- | --- | --- | --- |
| 1 | 8192 | req1, req2, req3块1 | 块1 | [0, 2692) | ✅ 是 |
| 2 | 8192 | req3块2 | 块2 | [2692, 10884) | ✅ 是 |
| 3 | 8192 | req3块3 | 块3 | [10884, 19076) | ✅ 是 |
| 4 | 8192 | req3块4 | 块4 | [19076, 27268) | ✅ 是 |
| 5 | 8192 | req3块5 | 块5 | [27268, 35460) | ✅ 是 |
| 6 | 8192 | req3块6 | 块6 | [35460, 43652) | ✅ 是 |
| 7 | 8192 | req3块7(最后) | 块7 | [43652, 50000) | ❌ 否(普通 Req,转 decode) |

**结论**：req3（50000）被切成 **7 块**，横跨 **7 个 prefill 批**（批 1 与 req1/req2 共享，批 2~7 独占）；前 6 块是 `ChunkedReq`（中间块，无输出、不入前缀缓存），第 7 块是普通 `Req`（产出首 token、缓存整段前缀、转入 decode）。整个过程中「算到哪了」的进度，全部靠 `complete_one()` 改写共享 `ChunkedReq.cached_len` 在 Engine 与 Scheduler 之间传递。

若本地有 GPU，可选验证：启动服务时加 `--max-prefill-length 8192`，构造一个约 5 万 token 的长 prompt（或在 [prefill.py:82](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L82) 构造处临时加 `logger.debug` 打印 `cached_len/chunk_size/is_chunked`），对照上表确认块数与区间；无 GPU 则结论由源码阅读得出，具体运行数值「待本地验证」。

## 6. 本讲小结

- Prefill 批受**两把尺子**约束：`token_budget`（= `max_extend_tokens`，默认 8192）管「每批算多少」，决定是否分块；`reserved_size` 对 `available_size` 的检查管「放多少请求进来」，决定准入。前者分块、后者防 OOM，是两件不同的事。
- `PrefillAdder.try_add_one` 合流两条路径：**首次准入**走 `_try_allocate_one`（前缀匹配 + 整段估算 + 加锁前后双重资源检查）再 `_add_one_req`；**分块续算**跳过资源检查、直接用上一块的 `cached_len` 进 `_add_one_req`——资源安全在准入那一刻一次性把关。
- `_try_allocate_one` 用 `estimated_len = 整段剩余 prompt + 输出` 做保守上界估计，并在 `lock` 前后各查一次 `available_size`，因为加锁会让 evictable 下降、可用量缩水。
- `ChunkedReq` 是中间块的载体，覆写 `append_host`（抛错）与 `can_decode`（恒 False）；**跨批续算的进度靠 `complete_one()` 改写同一个共享对象的 `cached_len`「穿」回 Scheduler**——这是本讲最关键、也最易读漏的机制。
- `PrefillManager.schedule_next_batch` 用「队头阻塞」逐个试加，遇到第一个失败就 `break`；之后把**续算请求前置**、未轮到的请求接后，重排 `pending_list`。最后一块（`is_chunked=False`）是普通 Req，负责产出首 token、缓存整段前缀并转入 decode。
- `_schedule_next_batch` 采用「**prefill 优先**」策略（`prefill or decode`），长 prompt 的分块续算会连续占用多个 prefill 批、推迟 decode——这是 Mini-SGLang 有意简化的部分，代码以 `TODO` 标注了未来更细粒度的策略。

## 7. 下一步学习建议

- 想知道 prefill 批产出后，**decode 批**怎么挑、`table_idx` 槽位与 `token_pool`/`page_table` 的二维结构如何运作，请读 **u4-l4 Decode 调度、TableManager 与 TokenPool**。
- 想知道本讲反复提到的 `cache_manager.match_req` / `lock` / `allocate_paged` / `cache_req` 背后的页分配、回收与淘汰细节，请读 **u6-l1 KV Cache 池与抽象** 与 **u6-l2 Radix Cache 实现** 与 **u6-l3 CacheManager 页分配、回收与淘汰**。
- 想知道分块续算时「之前各块的 K/V 如何被前向读回」——即 `cached_len` 之前那段在 attention kernel 里如何参与计算，请读 **u7 注意力后端**（尤其是 `prepare_metadata` 如何按 `cached_len`/`extend_len` 构造 `cu_seqlens`）。
- 想知道 `complete_one` 所在的 `forward_batch` 还做了哪些事（CUDA Graph 回放、采样、异步拷回），请读 **u5-l2 Engine forward 与采样** 与 **u5-l3 CUDA Graph 捕获与回放**。
- 建议顺带重读 **u2-l1 核心数据结构**，把本讲用到的 `cached_len`/`device_len`/`extend_len`/`remain_len` 不变量与 `complete_one` 语义对上，理解会更扎实。
