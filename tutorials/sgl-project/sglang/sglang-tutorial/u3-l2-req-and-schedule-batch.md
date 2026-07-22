# 请求与批数据模型：Req 与 ScheduleBatch

## 1. 本讲目标

本讲打开 u3-l1 里 `get_new_batch_prefill` / `update_running_batch` 操作的「数据」。学完后你应当能够：

- 说清一个 `Req` 对象在请求从到达到结束的整个生命周期里，有哪些关键字段被读/写，以及它们各自的含义。
- 说清 `ScheduleBatch` 这个「批容器」内部装了哪三类东西（请求列表、共享资源引用、跨给 GPU 的张量），以及它在 prefill 与 decode 两个阶段分别保存什么。
- 看懂一次调度迭代产出的 `NextBatchPlan` 如何把「本轮要跑的批」和「更新后的常驻 decode 批」打包回调度器。
- 理解为什么 `ScheduleBatch` 的字段要「原地重建而非就地修改」。

本讲只聚焦 **数据结构本身**，不展开调度算法（u3-l3）与 CPU/GPU 重叠（u3-l4）。

## 2. 前置知识

在进入字段之前，先建立两个直觉。

**第一，区分「请求级状态」和「批次级张量」。** 一条用户请求在调度器里是「活的」——它有输入 token、已经生成的输出 token、前缀缓存命中信息、是否结束等不断变化的状态；这些是 **请求级** 的，每条请求一份，由 `Req` 承载。但 GPU 一次前向要处理的是「一整批请求」，需要把若干条请求的 token 拍平、对齐成几个大张量（`input_ids`、`seq_lens`、`out_cache_loc` 等）一次性喂给模型；这些是 **批次级** 的，由 `ScheduleBatch` 承载。

**第二，prefill 和 decode 是两套完全不同的张量形状。**

- **prefill（SGLang 内部叫 extend）**：处理 prompt 里「还没算过」的那段 token，每条请求长度不同，所以 `input_ids` 是 **变长拼接** 的一维张量。
- **decode**：每条请求只往前走一个 token，所以 `input_ids` 形状固定是 `[batch_size]`，每个请求恰好一个 token。

正因为两种阶段形状差很大，`ScheduleBatch` 提供了 `prepare_for_extend()` 和 `prepare_for_decode()` 两套组装逻辑。

**术语准备：** `array("q")` 是 Python 标准库 `array` 模块里「8 字节有符号整数」的紧凑数组（比普通 `list[int]` 省内存）；`Range` 是一个具名元组 `(start, end)`，带一个 `length = end - start` 属性，定义在 [utils/common.py:1182-1188](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/utils/common.py#L1182-L1188)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/sglang/srt/managers/schedule_batch.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py) | 本讲主战场：定义 `Req`、`ScheduleBatch`、`NextBatchPlan`，以及 prefill/decode/合并/过滤等所有批操作 |
| [python/sglang/srt/managers/scheduler.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py) | `handle_generate_request` 里把进来的请求构造成 `Req` 对象 |
| [python/sglang/srt/managers/scheduler_components/batch_result_processor.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/batch_result_processor.py) | 前向结束后把采样出的 token 写回 `req.output_ids`，并判定是否结束 |
| [python/sglang/srt/utils/common.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/utils/common.py) | `Range` 具名元组定义 |

## 4. 核心概念与源码讲解

### 4.1 Req 类：单请求的全生命周期状态

#### 4.1.1 概念说明

`Req` 是 **一条用户请求在调度器内部的完整状态载体**，定义在 [schedule_batch.py:713](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L713)。你可以把它理解成一张「会随时间填写的工单」：

- 一开始工单上只有 **输入**（prompt 文本和 token id）。
- 经过前缀缓存匹配后，工单上记下 **命中了哪些已有 KV**（`prefix_indices`、`last_node`）。
- 每轮 decode 结束，工单上追加 **新生成的 token**（`output_ids`）。
- 当达到停止条件，工单盖上 **结束章**（`finished_reason`）。

`Req` 在 u3-l1 提到的进程拓扑里是「贯穿三进程的汇合键」的载体之一：它的 `rid`（request id）就是 u2-l3 讲的那把唯一汇合键。注意 `Req` 本身只在 Scheduler 进程内活动，**不会上 ZMQ 跨进程传输**——真正上线的是 `TokenizedGenerateReqInput`，到了 Scheduler 才被「翻译」成 `Req`（见 4.1.3）。

#### 4.1.2 核心流程：一条 Req 的一生

```text
请求到达 Scheduler
   │  handle_generate_request 构造 Req（origin_input_ids 已分好词，output_ids 为空）
   ▼
进入 waiting_queue（等待 prefill）
   │  init_next_round_input：
   │    - _refresh_fill_ids 把 origin+output 拼成 full_untruncated_fill_ids
   │    - tree_cache.match_prefix 写回 prefix_indices / last_node / host_hit_length
   │    - set_extend_range 记下「本轮要算 [start, end) 这段」
   ▼
被选进 prefill 批（ScheduleBatch.prepare_for_extend 读取上面这些字段）
   │  GPU 前向 → 采样出第一个 token
   ▼
batch_result_processor：req.output_ids.append(next_token_id)
   │  update_finish_state 判定是否结束
   ├── 未结束 → merge 进 running_batch（decode 批）
   │     每轮 prepare_for_decode 再前向 1 token，再 append，循环
   └── 结束（finished_reason 非空）
         │  release_kv_cache 释放 KV、evict 树节点
         ▼
       filter_batch 把它从 running_batch 剔除
```

四个最常被读写的字段贯穿全程：

| 字段 | 含义 | 何时写 |
| --- | --- | --- |
| `origin_input_ids` | prompt 的 token id 数组（含图像 padding） | 构造时一次写定 |
| `output_ids` | 已生成的输出 token，**只追加** | 每轮 decode 后 `append` |
| `full_untruncated_fill_ids` | origin + output 的完整序列 | `_refresh_fill_ids` 维护 |
| `prefix_indices` | 前缀缓存命中的 KV 下标 | `init_next_round_input` 里 match_prefix 写回 |

#### 4.1.3 源码精读

**(1) 构造：从上线消息翻译成 Req。** Scheduler 的 `handle_generate_request` 接到 `TokenizedGenerateReqInput` 后，构造 `Req`：

```python
# python/sglang/srt/managers/scheduler.py:2112
req = Req(
    recv_req.rid,            # 唯一 id
    recv_req.input_text,
    recv_req.input_ids,      # 已分好词的 origin_input_ids
    recv_req.sampling_params,
    return_logprob=recv_req.return_logprob,
    ...
)
```

永久链接：[scheduler.py:2112-2153](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L2112-L2153)。这一步把跨进程消息「落地」成 Scheduler 私有的、可变的状态对象。

**(2) 输入输出与 fill_ids 三件套。** `Req.__init__` 里最核心的几行：

```python
# schedule_batch.py:763-778
self.origin_input_ids = origin_input_ids
self.origin_input_ids_unpadded = ...   # 图像 padding 之前的原始长度
self.output_ids = array("q")           # 输出 token，只追加
self.full_untruncated_fill_ids = array("q")  # origin + output 完整序列
self.extend_range: Optional[Range] = None     # 本轮要算 [start, end)
```

注意 `output_ids` 的注释强调它是「按约定只追加（append-only）」的——因为下游 `_refresh_fill_ids` 只靠长度去推断已经同步了多少，如果中途原地改写却保持长度不变，会悄悄把 `fill_ids` 写坏。永久链接：[schedule_batch.py:761-778](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L761-L778)。

`_refresh_fill_ids` 用「已同步输出数 = 完整长度 - origin 长度」来增量追加，只在别名或长度对不上时才全量重建：

```python
# schedule_batch.py:1170-1177
n_have_output = len(self.full_untruncated_fill_ids) - len(self.origin_input_ids)
if (self.full_untruncated_fill_ids is not self.origin_input_ids
        and 0 <= n_have_output <= len(self.output_ids)):
    self.full_untruncated_fill_ids.extend(self.output_ids[n_have_output:])
else:
    self.full_untruncated_fill_ids = self.origin_input_ids + self.output_ids
```

这是一种常见的 **增量同步优化**：每轮 decode 只追加新 token，避免 O(上下文长度) 的全量拷贝。

**(3) 前缀缓存匹配：init_next_round_input。** 这是 Req 在 prefill 前的「装备整备」函数，负责把 origin+output 拼好，然后向 `tree_cache` 询问「我这段 token 有多少已经缓存了」：

```python
# schedule_batch.py:1240-1271（节选）
match_result = tree_cache.match_prefix(
    MatchPrefixParams(key=RadixKey(token_ids=token_ids_to_match,
                                   extra_key=self.extra_key, limit=key_limit),
                      req=self, cow_mamba=cow_mamba))
(self.prefix_indices, self.last_node, self.last_host_node,
 self.best_match_node, self.host_hit_length,
 self.swa_host_hit_length, self.mamba_host_hit_length,
 self.mamba_branching_seqlen) = (match_result.device_indices, ...)
```

匹配结果被「拆箱」写回 Req 的多个字段：`prefix_indices` 是命中的 KV 下标张量、`last_node` 是命中的基数树节点（后续 insert KV 要挂在它下面）、`host_hit_length` 是命中在主机内存（HiCache）上的长度。这些字段随后被 `ScheduleBatch.prepare_for_extend` 读走，用来决定「实际还要算多少 token」。永久链接：[schedule_batch.py:1179-1300](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L1179-L1300)。

**(4) 长度与结束判定。** 一个很有用的只读属性 `seqlen`：

```python
# schedule_batch.py:1087-1090
@property
def seqlen(self) -> int:
    return len(self.origin_input_ids) + len(self.output_ids)
```

以及结束判定就是看 `finished_reason` 是否被设上：

```python
# schedule_batch.py:1148-1150
def finished(self) -> bool:
    return self.finished_reason is not None
```

**注意一个易错点**：调度循环中途想中止一条请求时，不能直接写 `finished_reason`（否则它会被 `filter_batch` 立刻过滤掉、再也没机会回响应），而是写 `to_finish`，等结果处理阶段再正式收尾（见 [schedule_batch.py:858-861](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L858-L861)）。

**(5) 输出 token 真正被写回的位置。** 这不在 `schedule_batch.py`，而在 `batch_result_processor`——每轮前向采样出 token 后：

```python
# python/sglang/srt/managers/scheduler_components/batch_result_processor.py:233-238
# req output_ids are set here
req.output_ids.append(next_token_id)
self._maybe_update_reasoning_tokens(req, next_token_id)
req.update_finish_state()
```

永久链接：[batch_result_processor.py:230-243](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/batch_result_processor.py#L230-L243)。这就是 `output_ids` 在 decode 阶段不断增长的源头。

**(6) 增量解码的偏移量。** `surr_offset` / `read_offset` 是给 DetokenizerManager 做增量解码用的（u2-l3 提过的 `surr_offset`/`read_offset`），在 `init_incremental_detokenize` 里初始化，避免每出一个 token 就把整段重解码一遍：[schedule_batch.py:1303-1321](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L1303-L1321)。

#### 4.1.4 代码实践

**实践类型：源码阅读型（无需 GPU）。**

1. **实践目标**：为一条请求画出它「字段读写时间线」，把零散的字段和调度阶段对应起来。
2. **操作步骤**：
   - 打开 [schedule_batch.py:761-912](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L761-L912)，列出「输入输出」「内存池」「前缀信息」「结束判定」四个分组的字段。
   - 用 Grep 在 `python/sglang/srt/managers/` 下搜索 `req.output_ids`、`req.prefix_indices`、`req.extend_range`，记录每个字段分别在哪些函数被 **读**、哪些被 **写**。
3. **需要观察的现象**：你会发现 `output_ids` 几乎只在 `batch_result_processor` 被写、在各处被读；`prefix_indices` 只在 `init_next_round_input` 被写、在 `prepare_for_extend` 被读。
4. **预期结果**：得到一张「阶段 → 字段 → 读/写」的表格，例如：

   | 阶段 | 字段 | 操作 |
   | --- | --- | --- |
   | 构造 | `origin_input_ids` | 写（一次） |
   | prefill 装备 | `prefix_indices`/`last_node` | 写（match_prefix） |
   | prefill 装备 | `extend_range` | 写（set_extend_range） |
   | 结果处理 | `output_ids` | 写（append） |
   | 结果处理 | `finished_reason` | 写（结束时） |

5. 如有 GPU 环境，可用 `sglang.Engine` 加载一个小模型，发一条 `stream=True` 请求，对照日志确认每个 token 对应一次 `output_ids.append`（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `output_ids` 被设计成 append-only？如果某段代码原地 `output_ids[i] = x` 改了中间一个 token 但长度不变，会发生什么？

> **参考答案**：因为 `_refresh_fill_ids` 用「完整长度 − origin 长度」来推断已经同步了多少个输出 token，从而只追加新增部分。原地改写若保持长度不变，这个推断感知不到变化，`full_untruncated_fill_ids` 就不会被修正，导致喂给模型的序列与实际输出不一致。

**练习 2**：`seqlen` 属性等于什么？为什么不用一个单独的字段存？

> **参考答案**：`seqlen = len(origin_input_ids) + len(output_ids)`。用属性按需计算，可以保证它永远和两个源数组同步，避免维护一个可能失步的冗余字段。

**练习 3**：调度循环中途想取消一条请求，为什么不能直接 `req.finished_reason = FINISH_ABORT(...)`？

> **参考答案**：因为 `filter_batch` 依据 `finished()` 过滤请求，一旦 `finished_reason` 被设上，请求会立刻被剔除出批，可能来不及把已生成的部分流式回写给调用方。正确做法是先设 `to_finish`，由结果处理阶段在合适时机正式收尾。

---

### 4.2 ScheduleBatch 类：批容器与 GPU 张量组装

#### 4.2.1 概念说明

`ScheduleBatch` 是 **一次调度迭代里「要交给 GPU 的那一批」的容器**，定义在 [schedule_batch.py:1821](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L1821)。如果说 `Req` 是单条工单，`ScheduleBatch` 就是「一沓工单 + 一张给 GPU 看的汇总表」。它内部装了三类东西：

1. **请求列表** `reqs: List[Req]`——这批包含哪些请求（核心字段，`ForwardBatch` 会从它派生 `lora_ids`/`rids`/`grammars`/位置等）。
2. **共享资源引用** `req_to_token_pool`、`token_to_kv_pool_allocator`、`tree_cache`、`model_config`——这些在整个引擎生命周期内不变，所有批共用同一份。
3. **跨给 GPU 的张量与元数据** `input_ids`、`seq_lens`、`out_cache_loc`、`forward_mode` 等——这些是 prefill/decode 阶段才填的、按批次变化的张量。

一个关键设计（见仓库代码规范 `schedule-batch-out-of-place-mutation`）：**第三类张量字段必须「重建后整体重新绑定」，不能就地修改**。比如长度加 1 要写 `self.seq_lens = self.seq_lens + 1`，而不是 `self.seq_lens += 1`。原因是 overlap 调度器会把「上一轮的旧批对象」暂存起来延后处理，旧对象必须保持冻结不变。

#### 4.2.2 核心流程：prefill 批 vs decode 批

```text
ScheduleBatch.init_new(reqs, ...)        # 只装 reqs + 共享资源，张量字段先留空
        │
        ├── prefill 路径：prepare_for_extend()
        │     - 对每个 req 取 get_fill_ids()，再去掉已缓存前缀 prefix_indices
        │       → 得到「真正要算的 token」input_ids（变长拼接）
        │     - seq_lens = req.extend_range.end（每条不同）
        │     - prefix_lens / extend_lens / extend_num_tokens
        │     - alloc_for_extend 分配 KV 槽 → out_cache_loc
        │
        └── decode 路径：prepare_for_decode()
              - forward_mode = DECODE
              - input_ids 形状固定 [b]（每请求 1 个 token）
              - alloc_for_decode(token_per_req=1) → out_cache_loc
              - seq_lens = seq_lens + 1，kv_committed_len += 1
```

.prefill 批和 decode 批的「形状」差异就体现在上面：prefill 的 `input_ids` 是所有请求变长 token 拍平的一维张量（长度 = `extend_num_tokens`），decode 的 `input_ids` 长度恰好等于 `batch_size`。

#### 4.2.3 源码精读

**(1) 字段分三大组。** 类定义开头用注释把字段分了组，最具代表性的是这几行：

```python
# schedule_batch.py:1826-1832（核心 + 共享资源）
reqs: List[Req]
req_to_token_pool: ReqToTokenPool = None
token_to_kv_pool_allocator: BaseTokenToKVPoolAllocator = None
tree_cache: BasePrefixCache = None
```

跨给 GPU 的张量字段集中在后面，例如：

```python
# schedule_batch.py:1883-1906（节选）
input_ids: torch.Tensor = None        # shape: [b] 或变长
req_pool_indices: torch.Tensor = None # shape: [b]
seq_lens: torch.Tensor = None         # shape: [b]
out_cache_loc: torch.Tensor = None    # KV 输出位置
```

永久链接：[schedule_batch.py:1821-1997](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L1821-L1997)。注意很多字段默认是 `None`——它们要等到 `prepare_for_*` 才被填上。

**(2) 工厂 init_new：只装 reqs 和共享资源。** 它并不组装张量，只是把请求列表、内存池、树缓存、模型配置等放进去，并顺带从 reqs 聚合几个布尔标志：

```python
# schedule_batch.py:2011-2025（节选）
batch = cls(
    reqs=reqs,
    req_to_token_pool=req_to_token_pool,
    token_to_kv_pool_allocator=token_to_kv_pool_allocator,
    tree_cache=tree_cache,
    model_config=model_config,
    ...
    return_logprob=any(req.return_logprob for req in reqs),
    has_grammar=any(req.grammar for req in reqs),
    is_prefill_only=all(req.is_prefill_only for req in reqs),
)
```

永久链接：[schedule_batch.py:1998-2033](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L1998-L2033)。

**(3) prefill 组装：prepare_for_extend。** 这是把 Req 的请求级字段「拍平」成批次张量的关键：

```python
# schedule_batch.py:2180-2185
input_ids = [r.get_fill_ids()[len(r.prefix_indices) :] for r in reqs]  # 去掉已缓存前缀
extend_num_tokens = sum(len(ids) for ids in input_ids)
seq_lens = [r.extend_range.end for r in reqs]
prefix_lens = [len(r.prefix_indices) for r in reqs]
extend_lens = [r.extend_range.length for r in reqs]
```

注意第一行 `get_fill_ids()[len(prefix_indices):]`——它把「完整序列」切掉「已命中缓存的 prefix」部分，剩下的才是这一轮真正要送进 GPU 算的 token。这正是 RadixAttention 省算力的体现：命中越多，`input_ids` 越短。随后 `alloc_for_extend` 给这批分配 KV 槽，写回 `out_cache_loc`。永久链接：[schedule_batch.py:2170-2218](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L2170-L2218)。

**(4) decode 组装：prepare_for_decode。** decode 要简单得多：每请求只算 1 个 token，所以只分配一个槽、序列长度整体加 1：

```python
# schedule_batch.py:2876-2889（节选）
self.out_cache_loc = alloc_for_decode(self, token_per_req=1)
for req in self.reqs:
    req.decode_batch_idx += 1
    req.kv_committed_len += 1
self.seq_lens = self.seq_lens + 1            # 重建而非 += ，见 4.2.1
self.seq_lens_cpu = self.seq_lens_cpu + 1
self.seq_lens_sum = None                      # 惰性重算
```

永久链接：[schedule_batch.py:2847-2917](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L2847-L2917)。注意这里对 `seq_lens` 用的是「`+ 1` 重新绑定」而不是 `+= 1`，正是 4.2.1 说的不可变约定。

**(5) filter_batch：剔除已结束的请求。** 每轮 decode 后会有请求达到停止条件，需要从批里清掉：

```python
# schedule_batch.py:2929-2956（节选）
keep_indices = [i for i in range(len(self.reqs))
                if not self.reqs[i].finished()
                and self.reqs[i] not in chunked_req_to_exclude]
...
self.reqs = [self.reqs[i] for i in keep_indices]
self.req_pool_indices = self.req_pool_indices[keep_indices_device]
self.seq_lens = self.seq_lens[keep_indices_device]
self.out_cache_loc = None   # 留给下一轮 prepare 重算
```

永久链接：[schedule_batch.py:2919-2965](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L2919-L2965)。它会同步裁剪所有「按请求维度对齐」的张量，保证 `reqs` 和各张量第一维始终一致。

**(6) merge_batch：把 prefill 批并入 running decode 批。** u3-l1 讲的 `get_next_batch_to_run` 里有一步「先 prefill 新请求、再把它们 merge 进正在 decode 的批」，就是它：

```python
# schedule_batch.py:3008-3045（节选）
self.req_pool_indices = torch.cat([self.req_pool_indices, other.req_pool_indices])
self.seq_lens = torch.cat([self.seq_lens, other.seq_lens])
self.out_cache_loc = None
self.reqs = self.reqs + other.reqs
```

永久链接：[schedule_batch.py:2998-3057](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L2998-L3057)。同样遵守「重建而非就地 extend」的约定（`self.reqs + other.reqs`，不是 `self.reqs.extend(...)`）。

**(7) copy：给 overlap 模式做快照。** overlap 调度器要把「上一轮的批」暂存起来延后处理，所以需要一个浅拷贝。`copy()` 只复制 `process_batch_result` 真正会用到的字段，并对 `reqs` 列表做切片防御性快照：

```python
# schedule_batch.py:3065-3066
return ScheduleBatch(
    reqs=self.reqs[:],   # 切片快照，绝不与原列表别名
    ...
)
```

永久链接：[schedule_batch.py:3059-3078](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L3059-L3078)。

#### 4.2.4 代码实践

**实践类型：源码阅读 + 形状推导（无需 GPU）。**

1. **实践目标**：亲手推出一个 prefill 批和一个 decode 批的 `ScheduleBatch` 关键张量形状。
2. **操作步骤**：
   - 假设有 2 条请求，A 的 prompt 5 token、命中前缀 2 token；B 的 prompt 3 token、命中前缀 0 token。
   - 在 `prepare_for_extend` 里手工算 `input_ids`、`extend_num_tokens`、`prefix_lens`、`extend_lens`、`seq_lens` 各是多少。
   - 再假设这 2 条都进入 decode，在 `prepare_for_decode` 里算 `input_ids` 长度、`seq_lens` 加 1 后的值。
3. **需要观察的现象**：prefill 批的 `input_ids` 是变长拍平的（A 贡献 3 个、B 贡献 3 个 → 共 6 个），而 decode 批的 `input_ids` 恰好等于批大小 2。
4. **预期结果**：

   | 字段 | prefill 批 | decode 批 |
   | --- | --- | --- |
   | `input_ids` 长度 | (5−2)+(3−0)=6 | 2（= batch_size） |
   | `prefix_lens` | [2, 0] | （decode 不用） |
   | `extend_lens` | [3, 3] | （decode 不用） |
   | `seq_lens` | [5, 3]（=extend_range.end） | 上一轮 +1 |
   | `out_cache_loc` 长度 | 6（每 token 一个槽） | 2（每请求一个槽） |

5. 代码层面可用 Grep 确认 `alloc_for_extend` 按 `extend_num_tokens` 分配、`alloc_for_decode` 按 `token_per_req=1` 分配（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `merge_batch` 里写 `self.reqs = self.reqs + other.reqs` 而不是 `self.reqs.extend(other.reqs)`？

> **参考答案**：`ScheduleBatch` 的张量/列表字段遵循「原地重建而非就地修改」的约定。overlap 调度器会把旧批对象暂存延后处理，旧对象必须保持冻结；`extend` 会就地改动被暂存引用的同一个列表，破坏这一前提。`+` 生成新列表，旧引用不受影响。

**练习 2**：`out_cache_loc` 在 `filter_batch` 和 `merge_batch` 里都被置为 `None`，为什么？

> **参考答案**：这两个操作改变了批的成员（剔除或并入请求），旧的 KV 输出位置不再对应新的请求布局。置 `None` 后，由下一轮 `prepare_for_*` 重新分配，避免用到错位的旧值。

**练习 3**：`init_new` 聚合了 `return_logprob`、`has_grammar`、`is_prefill_only` 三个批级布尔标志，分别用 `any` 还是 `all`？为什么 `is_prefill_only` 用 `all`？

> **参考答案**：前两者用 `any(...)`（只要有一条请求需要，整批就得按「需要」处理），`is_prefill_only` 用 `all(...)`（只有批里 **所有** 请求都是 prefill-only，整批才能走 prefill-only 优化路径；混入一条要生成的请求就不成立）。

---

### 4.3 NextBatchPlan：调度器迭代结果打包

#### 4.3.1 概念说明

u3-l1 讲过，调度器每轮迭代的核心是 `get_next_batch_to_run`，它要返回 **两样东西**：本轮真正要送给 GPU 跑的批，以及「更新之后」的常驻 decode 批（`running_batch`）。这两者被一起打包成 `NextBatchPlan`。它非常小，却把「调度决策」和「状态更新」这两件事干净地分开了。

#### 4.3.2 核心流程

```text
get_next_batch_to_runs():
   merge_batch(...)                    # prefill 批并入 running
   分流：
     有新请求 → prepare_for_extend → batch_to_run = prefill 批
     仅 decode  → prepare_for_decode → batch_to_run = running_batch
     空闲       → batch_to_run = None
   return NextBatchPlan(batch_to_run, running_batch)
```

- `batch_to_run` 可能为 `None`（没有活儿干时，走 idle）。
- `running_batch` 永远是「更新后」的那一份，调度器用它覆盖自己的 `self.running_batch`。

#### 4.3.3 源码精读

整个类只有两个字段，用的是 `msgspec.Struct`（仓库新代码统一用 msgspec 而非 dataclass）：

```python
# schedule_batch.py:3171-3173
class NextBatchPlan(msgspec.Struct):
    batch_to_run: Optional[ScheduleBatch]
    running_batch: ScheduleBatch
```

永久链接：[schedule_batch.py:3171-3173](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L3171-L3173)。

把决策结果显式分成「跑什么」和「新的常驻态」两份，好处是：调用方（事件循环）拿到 `batch_to_run` 就能直接交给 `TpModelWorker.forward_batch_generation` 去做 GPU 前向（u5-l1），同时用 `running_batch` 覆盖调度器的常驻批，二者互不干扰——这正是 u3-l1 overlap 模式能让「CPU 处理上一轮结果」与「GPU 跑当前批」并行的基础。

#### 4.3.4 代码实践

**实践类型：源码阅读型。**

1. **实践目标**：确认 `NextBatchPlan` 的两个槽位在事件循环里如何被消费。
2. **操作步骤**：在 [scheduler.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py) 里用 Grep 搜索 `NextBatchPlan(` 和 `get_next_batch_to_run`，找到它构造的位置，再搜索 `.batch_to_run` / `.running_batch` 的读取点。
3. **需要观察的现象**：`batch_to_run` 被传给前向（非空时），`running_batch` 被赋值回 `self.running_batch`。
4. **预期结果**：能看到「构造 plan → 取 batch_to_run 跑前向 → 取 running_batch 更新常驻态」这条三步行。
5. 若 `batch_to_run` 为 `None`，应观察到事件循环走 idle 分支而不调用前向（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`batch_to_run` 为什么是 `Optional`？

> **参考答案**：当既没有新请求待 prefill、`running_batch` 也为空（或全部 idle）时，这一轮没有批要跑，`batch_to_run` 就是 `None`，事件循环据此走空闲分支，避免空跑 GPU。

**练习 2**：为什么把 `running_batch` 也放进 `NextBatchPlan` 一起返回，而不是让 `get_next_batch_to_run` 直接就地修改 `self.running_batch`？

> **参考答案**：把「决策结果」作为返回值显式产出，而不是在函数内部悄悄改 self 状态，能让 `get_next_batch_to_run` 更接近纯函数、便于推理与测试；调用方拿到 plan 后再统一应用 `running_batch`，也方便 overlap 模式对旧/新状态做隔离管理。

## 5. 综合实践

把本讲三个最小模块串起来，完成一张「一条请求 × 数据结构」的全景追踪表。

**任务**：选定一条最简单的请求（prompt = "你好"，`max_new_tokens = 3`），按下表逐阶段填写它对应的 `Req` 字段值与所在 `ScheduleBatch` 的状态。请基于本讲引用的真实源码推导，而非猜测。

| 调度阶段 | Req 关键字段快照 | 所在 ScheduleBatch | batch 的 `forward_mode` | batch 关键张量 |
| --- | --- | --- | --- | --- |
| ① 到达，构造 Req | `origin_input_ids`=?，`output_ids`=? | （未入批） | — | — |
| ② `init_next_round_input` 后 | `prefix_indices`=?，`extend_range`=? | waiting→被选入 prefill 批 | EXTEND | `input_ids`=?，`extend_num_tokens`=? |
| ③ prefill 前向后 | `output_ids`=?（append 第 1 个） | running_batch（刚 merge） | — | — |
| ④ 第 1 次 decode 前 | `output_ids` 长度=1 | running_batch | DECODE | `input_ids` 长度=?，`seq_lens`=? |
| ⑤ 第 3 次 decode 后结束 | `finished_reason`=? | 被 `filter_batch` 剔除 | — | — |

**要求**：

1. 假设无前缀缓存命中（`prefix_indices` 为空），填出 ② 中 `extend_range` 的 `start`/`end`、`extend_num_tokens`。
2. 指出 ④ 里 `seq_lens` 相比 ③ 增加了多少，对应 `prepare_for_decode` 的哪一行。
3. 指出 ⑤ 里 `filter_batch` 用 `Req` 的哪个方法判定「该剔除」。
4. 标注整条流程中 `output_ids` 被写入的唯一代码位置（文件 + 行号）。

**参考思路（请先自己填再对照）**：① `origin_input_ids` 是 "你好" 的 token（假设 2 个），`output_ids` 为空 `array("q")`；② 无命中时 `prefix_indices` 空、`extend_range ≈ (0, 2)`、`extend_num_tokens = 2`；③ `output_ids` 长度变 1，写入点为 [batch_result_processor.py:234](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/batch_result_processor.py#L234)；④ `seq_lens` 每轮 +1（[schedule_batch.py:2885](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L2885)），`input_ids` 长度 = batch_size；⑤ 用 `req.finished()`（[schedule_batch.py:1148](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L1148)）判定。

## 6. 本讲小结

- `Req` 是单条请求在调度器内的全生命周期状态：输入靠 `origin_input_ids`、输出靠 append-only 的 `output_ids`、前缀命中靠 `prefix_indices`/`last_node`、本轮算哪段靠 `extend_range`、结束靠 `finished_reason`。
- `output_ids` 的真正写入点不在 `schedule_batch.py`，而在 `batch_result_processor.py:234` 的 `append`；`_refresh_fill_ids` 靠长度增量同步，因此 `output_ids` 必须只追加。
- `ScheduleBatch` 装三类东西：请求列表 `reqs`、共享资源引用（内存池/树缓存/模型配置）、跨给 GPU 的批次张量；`init_new` 只装前两类，张量由 `prepare_for_extend`/`prepare_for_decode` 按阶段填充。
- prefill 批 `input_ids` 是变长拍平（长度 = `extend_num_tokens`，已扣除命中前缀），decode 批 `input_ids` 固定为 `[batch_size]`，每请求 1 个 token。
- `ScheduleBatch` 的张量/列表字段遵循「原地重建而非就地修改」约定（`+` 而非 `extend`/`+=`），这是 overlap 调度器暂存旧批对象的前提。
- `NextBatchPlan(batch_to_run, running_batch)` 把「本轮跑什么」与「更新后的常驻 decode 批」显式分开，是事件循环与 overlap 并行的基础。

## 7. 下一步学习建议

- 本讲只讲了批 **数据结构**，但「哪些请求被选进 prefill 批、按什么顺序」是调度策略问题，正是 **u3-l3 调度策略：LPM/FCFS/LOF 与 PrefillAdder** 的主题，它会用到本讲的 `prefix_indices`、`extend_num_tokens` 等字段做预算评估。
- 想看 `ScheduleBatch` 的张量如何被进一步打包成模型直接消费的 `ForwardBatch`，进入 **u5-l2 ForwardBatch：单次前向的数据载体**。
- 想理解 overlap 模式为何需要 `copy()` 快照与「不可变」约定，进入 **u3-l4 调度组件与 CPU-GPU 重叠**。
- 想理解 `tree_cache.match_prefix` 如何填出 `prefix_indices`/`last_node`，进入 **u4-l1 RadixAttention 与基数树缓存**。
