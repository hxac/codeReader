# Decode 调度、TableManager 与 TokenPool

## 1. 本讲目标

上一篇（u4-l3）讲了 **Prefill** 如何在 token budget 内挑请求、如何把长 prompt 切块续算。本讲打开 `_schedule_next_batch` 的另一半：**Decode 批怎么挑、谁来跑**，以及支撑 prefill 与 decode 共同运行的两个底层管家——`TableManager` 与 `token_pool`。

读完本讲，你应当能够：

1. 说清 `DecodeManager` 如何用 `running_reqs` 这个集合维护「正在生成中的请求」，并在请求结束时干净退出。
2. 解释为什么 decode 批要**按 `uid` 排序**（这正是当前 HEAD 提交 `#113` 修复的问题），以及它与张量并行（TP）正确性的关系。
3. 理解 `inflight_tokens` 如何**为正在 decode 的请求向未来预留 KV 空间**，防止 prefill 把显存吃光。
4. 画出 `table_idx`、`page_table`、`token_pool` 三者的二维结构，说清「一行 = 一个请求、一列 = 一个序列位置」。
5. 解释 `dummy_req` 为什么复用 `token_pool`，以及 `max_running_req` 如何同时约束 table 槽位数和 `page_table` 的行数。

---

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**Decode 是什么阶段。** LLM 推理分两阶段：prefill 一次性算完整段 prompt，产出第一个 token；之后每一步只喂入「上一步刚生成的 1 个 token」，算出下一个 token，循环往复，这一循环就是 decode。一个请求 prefill 完成后，就「转正」成为 decode 请求，在后续每一轮调度里被反复前向，直到生成结束（遇到 EOS 或达到 `max_tokens`）。

**为什么需要一个 manager 来管 decode 请求。** 系统里同时有很多请求在 decode，它们的生命周期互不相同：有的刚 prefill 完加入，有的还在生成，有的这一步刚结束要退出。需要一个数据结构集中维护「当前这一轮要参与 decode 的请求集合」，并能高效地增、删、查。这就是 `DecodeManager.running_reqs`。

**什么是 `table_idx`、`page_table`、`token_pool`。** 在 mini-sglang 里，每个正在跑的请求会被分配一个**行号** `table_idx`。这个行号同时索引两张二维表：

- `page_table[table_idx, pos]`：存的是「这个请求在序列位置 `pos` 的 KV，落在 KV cache 池的哪个槽」。它是 paged attention 的**寻址表**。
- `token_pool[table_idx, pos]`：存的是「这个请求在序列位置 `pos` 的 token id」。它是**输入 token 的来源**，也是**生成 token 的去处**。

如果把每个请求想象成表格里的一行，那么行号是 `table_idx`，列号是序列位置。`TableManager` 就是「行号分配器」。

> 关键术语回顾：`Req`（一条请求的运行时分身）、`uid`（请求全局唯一编号）、`complete_one()`（推进游标）、`can_decode`（是否还能继续生成）。这些都在 u2-l1 讲过，本讲直接使用。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [python/minisgl/scheduler/decode.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py) | `DecodeManager`：维护 decode 请求集合、估算在途 token、产出 decode 批。本讲主角之一。 |
| [python/minisgl/scheduler/table.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/table.py) | `TableManager`：`table_idx` 槽位的分配/回收，持有 `token_pool`。本讲主角之二。 |
| [python/minisgl/scheduler/scheduler.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py) | `Scheduler` 把两个 manager 装配起来，并在 `_forward` 里真正读写 `token_pool`。 |
| [python/minisgl/engine/engine.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py) | 构造 `page_table`、`dummy_req`，决定了 `max_running_req + 1` 这个维度。 |
| [python/minisgl/core.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py) | `Req` 定义 `table_idx`、`can_decode`、`remain_len` 等字段。 |
| [python/minisgl/scheduler/prefill.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py) | prefill 一侧消费 `table_idx`、`token_pool`、`inflight_tokens`，与本讲互为镜像。 |

---

## 4. 核心概念与源码讲解

### 4.1 DecodeManager：decode 队列的总管与 inflight_tokens 估计

#### 4.1.1 概念说明

`DecodeManager` 是一个极简的 dataclass，只有两个字段：`page_size` 和 `running_reqs`（一个 `Req` 的集合）。它的全部职责是：

1. 维护「正在 decode 的请求」集合 `running_reqs`；
2. 估算这些请求**未来还要消耗多少 KV token**（`inflight_tokens`），供 prefill 调度时预留；
3. 产出一个 decode `Batch`（或返回 `None` 表示没有可跑的 decode）。

它本身不做任何前向计算，只是「记账 + 产出批次」。

#### 4.1.2 核心流程

每轮主循环里，`DecodeManager` 被 `Scheduler` 以两种方式触碰：

- **前向后**：`filter_reqs(batch.reqs)` 用刚前向完的那批请求去**重建** `running_reqs`（详见 4.2）。
- **调度时**：`schedule_next_batch()` 把 `running_reqs` 排序后打包成 `Batch(phase="decode")`。

而 `inflight_tokens` 是一个只读属性，被 `PrefillManager` 在构造 `PrefillAdder` 时读取，作为 `reserved_size` 的初值（详见 4.1.3 与 u4-l3）。

伪代码：

```
每轮:
    先调度 prefill（若 pending 非空）
    若 prefill 没产出 batch，再 schedule_next_batch() 跑 decode
前向完成后:
    filter_reqs(本批 reqs)   # 新 prefill 进来的加入，已结束的剔除
```

#### 4.1.3 源码精读

类的定义与字段（[python/minisgl/scheduler/decode.py:L9-L12](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py#L9-L12)）：

```python
@dataclass
class DecodeManager:
    page_size: int
    running_reqs: Set[Req] = field(default_factory=set)
```

`inflight_tokens` 是本模块最巧妙的一笔（[python/minisgl/scheduler/decode.py:L27-L30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py#L27-L30)）：

```python
@property
def inflight_tokens(self) -> int:
    tokens_reserved = (self.page_size - 1) * len(self.running_reqs)  # 1 page reserved
    return sum(req.remain_len for req in self.running_reqs) + tokens_reserved
```

它由两部分相加：

- `sum(req.remain_len for req in self.running_reqs)`：每个在跑请求还剩多少 token 没生成（`remain_len = max_device_len - device_len`，见 [python/minisgl/core.py:L44-L46](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L44-L46)）。这些 token 将来 decode 时**每一个都要写一格 KV**，必须提前占住。
- `(page_size - 1) * len(self.running_reqs)`：页对齐的「零头」。因为 KV 以页（`page_size` 个 token）为单位分配，每个请求当前页可能没填满，最坏每个请求浪费 `page_size - 1` 个 token 位置。注释里的「1 page reserved」指的就是这份最坏零头。

它的唯一消费者是 prefill：[python/minisgl/scheduler/prefill.py:L131-L136](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L131-L136) 把它作为 `PrefillAdder.reserved_size` 的初值：

```python
adder = PrefillAdder(
    token_budget=prefill_budget,
    reserved_size=self.decode_manager.inflight_tokens,  # 先扣除在途 decode
    ...
)
```

于是 `PrefillAdder` 在判断「还能不能塞一个新 prefill 请求」时（[python/minisgl/scheduler/prefill.py:L50-L54](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L50-L54)），用的 `available_size` 已经预先扣掉了 decode 的未来占用，避免 prefill 把显存吃光、导致 decode 下一步 OOM。

`runnable` 与 `schedule_next_batch`（[python/minisgl/scheduler/decode.py:L32-L39](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py#L32-L39)）：

```python
def schedule_next_batch(self) -> Batch | None:
    if not self.runnable:
        return None
    return Batch(reqs=sorted(self.running_reqs, key=lambda req: req.uid), phase="decode")

@property
def runnable(self) -> bool:
    return len(self.running_reqs) > 0
```

注意 `sorted(..., key=lambda req: req.uid)`——这条排序是 TP 正确性的命门，4.2 节专门讲。

#### 4.1.4 代码实践

**目标**：手算 `inflight_tokens`，理解它如何随请求状态变化。

**步骤**：

1. 假设 `page_size=1`，`running_reqs` 里有 2 个请求：A 的 `remain_len=20`、B 的 `remain_len=5`。
2. 按 [decode.py:L27-L30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py#L27-L30) 的公式计算 `inflight_tokens`。
3. 再假设 `page_size=32`，重算一遍，观察页对齐零头的影响。

**需要观察的现象 / 预期结果**：

- `page_size=1`：零头项 `(1-1)*2=0`，结果 `= 20+5 = 25`。
- `page_size=32`：零头项 `(32-1)*2=62`，结果 `= 25+62 = 87`。

可见页越大，为在途 decode 预留的「保险」越保守——这是用空间换安全。

> 本实践为纯算术推导，无需 GPU，可直接手算验证。

#### 4.1.5 小练习与答案

**练习 1**：`inflight_tokens` 为什么不能用 `sum(req.extend_len ...)` 之类的「本批要算的量」来代替？

**答案**：`inflight_tokens` 度量的是「这些请求**未来还会**往 KV cache 写多少」，是给 prefill 做**容量预留**用的；`extend_len` 只是「本批当前要算的量」。预留要看未来的最坏占用，而非本批工作量，否则 prefill 会把 decode 未来需要的页抢走。

**练习 2**：`DecodeManager` 的字段里没有任何 tensor，这是巧合吗？

**答案**：不是巧合。`DecodeManager` 只做集合记账与预算估算，是**纯 CPU 逻辑**；真正的张量（KV、token）都在 engine / table_manager 侧。这种「调度逻辑与张量数据分离」正是 overlap scheduling 能把 CPU 调度与 GPU 计算重叠的前提（见 u4-l1）。

---

### 4.2 running_reqs：decode 请求的准入、退出与确定性排序

#### 4.2.1 概念说明

`running_reqs` 是 `DecodeManager` 唯一的可变状态，是一个 `Set[Req]`。它的生命周期由三个操作刻画：

- **准入（加入）**：一个请求 prefill 完成首 token 后，经 `filter_reqs` 加入集合，开始 decode。
- **退出（移除）**：请求生成结束（`can_decode` 为假，或命中 EOS），由 `remove_req` 移除；若客户端主动 abort，由 `abort_req` 移除。
- **产出批次**：`schedule_next_batch` 把集合排序后打包。

`Set` 是无序的，但 TP 要求所有 rank 产出**同一顺序**的批次——这就引出了「按 uid 排序」这条命门。

#### 4.2.2 核心流程

准入与退出的核心是 `filter_reqs`，它在每次前向后被调用（[python/minisgl/scheduler/scheduler.py:L232](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L232)）。它用一种「全量重建」的写法同时完成「加入新请求」和「剔除已结束请求」：

```
running_reqs = { req ∈ (旧集合 ∪ 本批reqs) 满足 req.can_decode }
```

退出有两个入口：

- 自然结束：在 `_process_last_data` 里检测到 `finished`，调用 `remove_req`（[scheduler.py:L160-L161](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L160-L161)）。
- 客户端 abort：`_process_one_msg` 收到 `AbortBackendMsg`，调用 `abort_req(uid)`（[scheduler.py:L192-L195](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L192-L195)）。

#### 4.2.3 源码精读

`filter_reqs`（[python/minisgl/scheduler/decode.py:L14-L15](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py#L14-L15)）：

```python
def filter_reqs(self, reqs: Iterable[Req]) -> None:
    self.running_reqs = {req for req in self.running_reqs.union(reqs) if req.can_decode}
```

一行做了三件事：先 `union` 把本批新请求并进来，再用 `can_decode` 过滤。`can_decode` 定义在 [python/minisgl/core.py:L59-L61](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L59-L61)：

```python
@property
def can_decode(self) -> bool:
    return self.remain_len > 0
```

即「还能不能继续生成」。这条过滤天然把两类对象挡在 decode 集合之外：

- **已结束的请求**：`remain_len == 0`，被剔除。
- **`ChunkedReq`（prefill 中间块）**：它覆写 `can_decode` 恒为 `False`（[python/minisgl/scheduler/prefill.py:L27-L29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L27-L29)），所以即便混进本批也不会污染 decode 集合。

`remove_req` 与 `abort_req`（[python/minisgl/scheduler/decode.py:L17-L25](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/decode.py#L17-L25)）：

```python
def remove_req(self, req: Req) -> None:
    self.running_reqs.discard(req)

def abort_req(self, uid: int) -> Req | None:
    for req in self.running_reqs:
        if req.uid == uid:
            self.running_reqs.remove(req)
            return req
    return None
```

`abort_req` 按 `uid` 查找并移除，返回被移除的 `Req`，以便 scheduler 回收它的 `table_idx` 等资源（见 [scheduler.py:L200-L202](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L200-L202) 的 `_free_req_resources`）。

**为什么必须按 uid 排序——HEAD 提交 #113 的来龙去脉。** `running_reqs` 是 `set`，Python 集合的迭代顺序受元素哈希与插入历史影响，**不同进程、不同运行之间不保证一致**。在单卡（`tp=1`）下这无所谓；但在张量并行（`tp>1`）下，每张卡是一个独立 scheduler 进程，各自构造 decode batch。如果 rank0 把请求排成 `[A, B, C]`、rank1 排成 `[B, A, C]`，那么前向时各卡按不同顺序切分同一批数据，后续的 `all_reduce` 会对不齐维度，直接出错。

当前 HEAD 的提交 `#113`「Stabilize decode batch request order across TP ranks」正是修这个问题——把：

```python
return Batch(reqs=list(self.running_reqs), phase="decode")            # 旧：集合顺序，不确定
```

改成：

```python
return Batch(reqs=sorted(self.running_reqs, key=lambda req: req.uid), phase="decode")  # 新：uid 确定序
```

`uid` 是请求的全局唯一编号（由前端 `FrontendManager` 分配，见 u3-l1），所有 rank 收到的 `uid` 集合相同，因此按 `uid` 排序后各卡得到**完全一致的请求顺序**。这就是 decode 批「确定性」的来源。

#### 4.2.4 代码实践

**目标**：用 git 复现 #113 这次修复，体会「确定性排序」对 TP 的必要性。

**步骤**：

1. 查看 HEAD 的这次提交：
   ```bash
   git show 9a91cfafe754aa85daee49998176275667eb58f2 -- python/minisgl/scheduler/decode.py
   ```
2. 阅读改动，确认它只动了 `schedule_next_batch` 一行。
3. 在 [scheduler.py:L219-L225](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L219-L225) 的 `_schedule_next_batch` 里确认：prefill 与 decode 二选一产出 batch，且 decode 走的就是这条已修复的路径。

**需要观察的现象 / 预期结果**：diff 应显示 `-return Batch(reqs=list(self.running_reqs)...)` / `+return Batch(reqs=sorted(self.running_reqs, key=lambda req: req.uid)...)`。这是一行修复，但消除了 TP 下 batch 分叉的隐患。

> 本实践为源码阅读型，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：`filter_reqs` 为什么用「重建整个集合」而不是「逐个 add / remove」？

**答案**：因为一次前向的 batch 里可能**同时**包含新 prefill 完成的请求（要加入）和已结束的请求（要剔除）。用「`union` 再按 `can_decode` 过滤」一次性完成双向更新，逻辑最简且不会漏；逐个操作则需要分别判断每个请求该加还是该删，更易出错。

**练习 2**：如果把 `sorted(...)` 改回 `list(...)`，单卡（`tp=1`）服务会立刻崩溃吗？

**答案**：不会立刻崩溃。`tp=1` 时只有一个 rank，batch 顺序随意都不影响正确性，只是顺序不稳定。问题只在 `tp>1` 时才暴露——不同 rank 顺序不一致会导致 `all_reduce` 维度错位。这也是该 bug 能潜伏一段时间、直到多人多卡场景才被发现的原因。

---

### 4.3 TableManager：table_idx 槽位的分配与回收

#### 4.3.1 概念说明

每个正在运行的请求都需要一个 `table_idx`——它是请求在 `page_table` 和 `token_pool` 里的**行号**。`TableManager` 就是这个行号的「发号器」：请求 prefill 准入时 `allocate()` 领一个号，请求结束时 `free()` 归还。

它的实现极其精简：一个 `_free_slots` 列表，初始为 `[0, 1, ..., max_running_reqs-1]`，分配就 `pop`、归还就 `append`。**槽位编号 == `table_idx`**。

#### 4.3.2 核心流程

```
初始化: _free_slots = [0, 1, 2, ..., max_running_reqs-1]
prefill 准入一个请求: allocate() -> pop 出一个号（如 max-1）赋给 req.table_idx
请求结束: free(req.table_idx) -> append 回列表，可被后续请求复用
任意时刻: available_size = len(_free_slots)  # 还能再容纳多少请求
```

注意两点：

- 分配是 **LIFO**（`pop()` 取末尾），所以相邻两个请求的 `table_idx` 往往相邻。
- `table_idx` 是**请求级**资源，与序列长度无关——无论 prompt 多长，一个请求始终只占一行；序列长度体现在**列**上。

#### 4.3.3 源码精读

整个类只有 22 行（[python/minisgl/scheduler/table.py:L4-L21](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/table.py#L4-L21)）：

```python
class TableManager:
    def __init__(self, max_running_reqs: int, page_table: torch.Tensor) -> None:
        self._max_running_reqs = max_running_reqs
        self._free_slots = list(range(max_running_reqs))
        self.page_table = page_table
        # NOTE: dummy request also use this pool to get the input ids, ...
        self.token_pool = torch.zeros_like(page_table, dtype=torch.int32)

    @property
    def available_size(self) -> int:
        return len(self._free_slots)

    def allocate(self) -> int:
        return self._free_slots.pop()

    def free(self, slot: int) -> None:
        self._free_slots.append(slot)
```

关键设计点：

1. **`token_pool` 与 `page_table` 同形**：`torch.zeros_like(page_table)`，即 shape `(max_running_req + 1, aligned_max_seq_len)`、`int32`。`page_table` 由 engine 传入（[scheduler.py:L58](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L58)），scheduler 又把它别名成 `self.token_pool`（[scheduler.py:L71](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L71)）。

2. **零初始化**：`torch.zeros_like`。注释（[table.py:L9-L10](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/table.py#L9-L10)）解释了原因——`dummy_req` 也要从这里取输入 token，所以必须预填合法 token id（`0`）。这一点在 4.4 详细展开。

3. **`allocate` 在 prefill 准入时被调用**：[prefill.py:L56](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L56) 的 `table_idx = self.table_manager.allocate()`，随后把请求的 prompt token 拷进 `token_pool[table_idx]` 对应区域（[prefill.py:L57-L61](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L57-L61) 与 [prefill.py:L79-L81](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L79-L81)）。

4. **`free` 在请求退出时被调用**：[scheduler.py:L200-L202](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L200-L202)：

```python
def _free_req_resources(self, req: Req) -> None:
    self.table_manager.free(req.table_idx)      # 归还行号
    self.cache_manager.cache_req(req, finished=True)  # 归还/缓存 KV 页
```

行号归还后可立即被新请求复用——这就是 `table_idx` 与「请求」而非「token」一一对应的体现。

#### 4.3.4 代码实践

**目标**：用一段最小 Python（无需 GPU）模拟 `TableManager` 的分配/回收，直观感受 LIFO 与并发上限。

**操作步骤**（把下面的「示例代码」存为 `table_sim.py` 直接 `python table_sim.py` 运行）：

```python
# 示例代码：TableManager 的纯 Python简化模拟（不含 torch）
class TableManagerSim:
    def __init__(self, max_running_reqs):
        self._free_slots = list(range(max_running_reqs))
    @property
    def available_size(self):
        return len(self._free_slots)
    def allocate(self):
        return self._free_slots.pop()
    def free(self, slot):
        self._free_slots.append(slot)

tm = TableManagerSim(max_running_reqs=4)
a, b, c = tm.allocate(), tm.allocate(), tm.allocate()
print("after 3 alloc:", a, b, c, "available =", tm.available_size)  # 3,2,1 available=1
tm.free(b)
print("after free b, next alloc =", tm.allocate(), "available =", tm.available_size)  # 复用 2
```

**需要观察的现象 / 预期结果**：三次分配得到 `3, 2, 1`（LIFO，从末尾取）；`available_size` 从 4 降到 1；释放 `b=2` 后下一次分配复用的正是 `2`。这对应真实代码里「请求结束→行号立刻可被新请求复用」。

#### 4.3.5 小练习与答案

**练习 1**：`allocate` 用 `pop()`（取末尾）、`free` 用 `append()`（放末尾），为什么不用 `pop(0)`（取头部）？

**答案**：`pop(0)` 是 O(n)（要把后续元素整体前移），而 `pop()`/`append()` 都是 O(1)。行号分配在每次 prefill 准入、回收在每次请求结束，都是高频路径，必须用 O(1) 操作。至于分配顺序是 LIFO 还是 FIFO，对正确性没有影响。

**练习 2**：一个 prompt 长 10000 token 的请求，和一个 prompt 长 5 token 的请求，谁占的 `table_idx` 多？

**答案**：一样多——都只占**一个** `table_idx`（一行）。`table_idx` 是请求级行号，序列长度体现在列（序列位置）上，不影响行号占用。但长请求会占用更多**列**，从而通过 `page_table` 占用更多 KV 页（见 4.4 与 u6）。

---

### 4.4 token_pool 与 page_table：请求的二维存储视图

#### 4.4.1 概念说明

`page_table` 和 `token_pool` 是两张同形的二维表，行号都是 `table_idx`，列号都是序列位置。它们的区别在于**存什么**：

| 表 | `table[idx, pos]` 存的是 | 用途 |
| --- | --- | --- |
| `page_table` | KV cache 池里的**槽位号**（page 内偏移） | 给 attention 后端寻址 K/V（paged KV） |
| `token_pool` | 该位置的 **token id** | 喂给 embedding 取输入；存采样出的新 token |

可以这样理解：`page_table` 是「**K/V 在哪**」的地图，`token_pool` 是「**输入是什么**」的草稿纸。

两者的第一维都是 `max_running_req + 1`（行数），第二维是 `aligned_max_seq_len`（列数）。`+1` 那一行是留给 `dummy_req` 的——这是 CUDA graph padding 的关键，也是本节实践的重点。

#### 4.4.2 核心流程

`token_pool` 在一轮前向里的双向使用（见 [scheduler.py:L227-L233](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L227-L233)）：

```
1. 聚合输入: batch.input_ids = token_pool[token_mapping, positions]
   # token_mapping[i] = 第 i 个输入 token 所属请求的 table_idx
   # positions[i]     = 该 token 的序列位置
2. 前向 + 采样 -> next_tokens_gpu
3. 散回输出: token_pool[req.table_idx, device_len] = next_token
   # 把新 token 写到「下一次 decode 要读的位置」
```

- **prefill 前**：调度器把 prompt token 拷进 `token_pool[table_idx, cached_len:device_len]`（[prefill.py:L79-L81](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/prefill.py#L79-L81)）。
- **decode 时**：上一步把新 token 写在 `token_pool[table_idx, device_len]`，`complete_one()` 推进游标后，下一步的 `positions` 恰好读到这一格——形成「写→推进→读」的接力。

`page_table` 则由 `CacheManager.allocate_paged` 在前向前写入（[cache.py:L42-L53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L42-L53)），把新分配的 KV 页号填进 `page_table[table_idx, page_pos]`；attention 后端通过 `batch.out_loc = page_table[token_mapping]`（[scheduler.py:L210](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L210)）读取这些位置去访问 KV。

#### 4.4.3 源码精读

**两张表的构造在 engine 里**（[python/minisgl/engine/engine.py:L65-L73](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L65-L73)）：

```python
self.max_seq_len = min(config.max_seq_len, num_tokens)
aligned_max_seq_len = _align_up_32(self.max_seq_len)
self.ctx.page_table = self.page_table = torch.zeros(  # + 1 for dummy request
    (config.max_running_req + 1, aligned_max_seq_len),
    dtype=torch.int32,
    device=self.device,
)
```

注意三个细节：

- 第一维是 `max_running_req + 1`，注释「+ 1 for dummy request」。
- 列数 `aligned_max_seq_len` 按 32 对齐（`_align_up_32`，[engine.py:L214-L215](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L214-L215)），是对齐到 128 字节的配套处理（见注释 [engine.py:L66](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L66)）。
- `token_pool` 不是在这里建，而是在 `TableManager.__init__` 里用 `torch.zeros_like(page_table)` 建成同形（[table.py:L11](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/table.py#L11)）。

**`dummy_req` 占用第 `+1` 行**（[engine.py:L89-L98](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L89-L98)）：

```python
self.dummy_req = Req(
    input_ids=torch.tensor([0], dtype=torch.int32, device="cpu"),
    table_idx=config.max_running_req,   # 末行：max_running_req
    cached_len=0, output_len=1, uid=-1, ...
)
self.page_table[self.dummy_req.table_idx].fill_(num_tokens)  # 指向 dummy page
```

`dummy_req.table_idx = config.max_running_req`，正好是 `page_table`/`token_pool` 的最后一行（因为合法槽位是 `0..max_running_req-1`，见 [table.py:L7](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/table.py#L7) 的 `list(range(max_running_reqs))`）。它的 `page_table` 整行被填成 `num_tokens`——即 KV 池里那个专门的「dummy page」（KV 池本身也 `+1` 了，见 [engine.py:L57-L63](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L57-L63) 的 `num_pages + 1`），这样 dummy 读写不会污染真实请求的 KV。

**`token_pool` 的双向读写**（[scheduler.py:L227-L233](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L227-L233)）：

```python
def _forward(self, forward_input: ForwardInput) -> ForwardOutput:
    batch, sample_args, input_mapping, output_mapping = forward_input
    batch.input_ids = self.token_pool[input_mapping]               # 聚合输入
    forward_output = self.engine.forward_batch(batch, sample_args)
    self.token_pool[output_mapping] = forward_output.next_tokens_gpu  # 散回输出
    self.decode_manager.filter_reqs(forward_input.batch.reqs)
    return forward_output
```

这里 `input_mapping`、`output_mapping` 都是「二元组张量」，做的是高级索引（fancy indexing）：`token_pool[token_mapping, positions]` 一次取出整批所有输入 token；`token_pool[mapping_list, write_list] = next_tokens` 一次写回每个请求的新 token。这两个二元组由 [scheduler.py:L252-L259](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L252-L259) 的 `_make_input_tuple` 与 [scheduler.py:L262-L267](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L262-L267) 的 `_make_write_tuple` 构造——前者把每个输入 token 映射到其 `table_idx` 与 `position`，后者对每个请求写到 `device_len`（不能 decode 的请求写 `-1` 作占位，见 [scheduler.py:L265](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L265)）。

#### 4.4.4 代码实践（本讲指定实践）

**目标**：解释 `dummy_req` 为何复用 `token_pool`，并说明 `max_running_req` 如何同时约束 table 槽位与 `page_table` 行数。

**步骤 1——找到 dummy 复用的原因。** 阅读 [table.py:L9-L11](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/table.py#L9-L11) 的注释：

```python
# NOTE: dummy request also use this pool to get the input ids, so we need to
# make sure the token pool is initialized with valid values (token_id = 0).
self.token_pool = torch.zeros_like(page_table, dtype=torch.int32)
```

再对照 `pad_batch`（[graph.py:L160-L166](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L160-L166)）：

```python
def pad_batch(self, batch: Batch) -> None:
    padded_size = (next(bs for bs in self.graph_bs_list if bs >= batch.size)
                   if self.can_use_cuda_graph(batch) else batch.size)
    batch.padded_reqs = batch.reqs + [self.dummy_req] * (padded_size - batch.size)
```

CUDA graph 只能回放**固定 batch size** 的计算图。真实 decode 批大小不固定，所以用 `dummy_req` 把它**补齐**（pad）到某个捕获尺寸。补齐后的 `padded_reqs` 会一起进入 `_make_input_tuple`，进而执行 `batch.input_ids = token_pool[input_mapping]`（[scheduler.py:L229](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L229)）。这意味着 dummy 那些行也会被 gather 出「输入 token id」送去 embedding 查表——所以 `token_pool` 的 dummy 行必须存放**合法的词表 id**，否则会越界或查到垃圾。`torch.zeros_like` 把整池预填 `0`，正好保证 dummy 行读到合法 id `0`。这就是「dummy 复用 token_pool」必须零初始化的原因。

**步骤 2——说明 max_running_req 的双重约束。** 对照三处：

- 槽位上限：[table.py:L7](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/table.py#L7) `_free_slots = list(range(max_running_reqs))`——初始恰好 `max_running_req` 个槽，即最多同时跑这么多请求。
- `page_table`/`token_pool` 行数：[engine.py:L70](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L70) `(config.max_running_req + 1, aligned_max_seq_len)`——第一维 `max_running_req + 1`，前 `max_running_req` 行给真实请求，最后一行给 dummy。

**需要观察的现象 / 预期结果（结论）**：

1. `max_running_req` 同时是「table 槽位数」和「`page_table`/`token_pool` 的真实请求行数」。二者必然相等：每个活跃请求领一个 `table_idx`（占一个槽、占一行），请求结束归还后槽与行同时空出。`+1` 那一行永远不参与分配（它超出 `range(max_running_req)`），专供 dummy。
2. `max_running_req` **不**约束序列长度——长度由列数 `aligned_max_seq_len` 与 KV 页总数共同决定（见 u5-l1、u6）。它只约束「并发请求数」。
3. `--shell-mode` 下 `max_running_req` 被强制为 1（[args.py:L233](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/args.py#L233)），于是 `page_table` 只有 2 行（1 真 + 1 dummy），呼应 u1-l2 讲过的「shell 模式强制单请求」。

> 本实践为源码阅读型，无需 GPU。若想亲眼验证，可在 [scheduler.py:L58](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L58) 与 [engine.py:L70](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L70) 处加日志打印 `table_manager._free_slots` 长度与 `page_table.shape`，确认二者关系（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：dummy_req 的 `page_table` 整行被填成 `num_tokens`（[engine.py:L98](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L98)），指向「dummy page」。如果让 dummy 复用某个真实请求的页，会发生什么？

**答案**：dummy 在 CUDA graph 回放时会对其「输入位置」做 KV 写入（store_kv）。若它指向真实请求的页，就会把真实请求的 KV 覆盖污染，导致该请求后续 decode 结果错乱。所以专门给 KV 池 `+1` 一个 dummy 页、并让 dummy 行整行指向它，把 dummy 的副作用隔离起来。

**练习 2**：`token_pool` 的写出用了「不能 decode 的请求写 `-1`」（[scheduler.py:L265](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L265)）。这是 `token_pool` 的第几列？为什么安全？

**答案**：`-1` 在 Python/PyTorch 索引里指「最后一列」（`aligned_max_seq_len - 1`）。把已结束请求的新 token 写到这一列是安全的，因为该列对应一个几乎不会被任何活跃请求读到的末端位置（真实请求的有效位置在 `[0, device_len)` 内，通常远小于列数上界）。这是一种「用一次无副作用写入代替条件分支」的技巧，让整批写入可以用一条高级索引语句完成。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一个**手动模拟**：追踪 3 个请求从 prefill 准入到 decode 结束的全过程，重点观察 `table_idx`、`token_pool`、`running_reqs` 的变化。

**场景设定**（`max_running_req=4`，`page_size=1`）：

- 时刻 t1：请求 uid=10、uid=20 先后 prefill 完成，加入 decode。
- 时刻 t2：请求 uid=5 prefill 完成，加入 decode。
- 时刻 t3：uid=20 生成结束退出。
- 时刻 t4：新的 uid=7 prefill 完成，加入 decode。

**任务**：

1. **TableManager 视角**：写出 t1~t4 各时刻 `_free_slots` 的内容与 `available_size`。（提示：LIFO 分配。）
2. **token_pool / page_table 视角**：画出 t3 时刻 4 个请求各自占用的「行（table_idx）」，标注哪一行被释放、哪一行被 uid=7 在 t4 复用。
3. **DecodeManager 视角**：写出 t4 时刻 `running_reqs` 的内容，以及 `schedule_next_batch()` 产出的 `Batch.reqs` 顺序。验证它是按 `uid` 升序排列（应是 `[uid=5, uid=7, uid=10]`），并解释若不排序在 `tp=2` 下为何出错。
4. **inflight_tokens 视角**：若 t4 时三个请求的 `remain_len` 分别为 30/10/50，`page_size=1`，算出此刻 `inflight_tokens`，说明它如何影响下一个 prefill 请求能否被准入。

**参考答案要点**：

1. 初始 `_free_slots=[0,1,2,3]`。t1 分配两次（LIFO 取 3、2）→ `[0,1]`，`available=2`。t2 分配一次取 1 → `[0]`，`available=1`。t3 释放 uid=20 所占行（=2）→ `[0,2]`，`available=2`。t4 分配一次取 2（复用刚释放的行）→ `[0]`，`available=1`。
2. 行占用：uid=10→3、uid=20→2（t3 释放）、uid=5→1、uid=7→2（t4 复用 uid=20 的旧行）。
3. t4 时 `running_reqs={uid=5, uid=7, uid=10}`；排序后 `Batch.reqs=[uid=5, uid=7, uid=10]`。若不排序，rank0/rank1 的 `set` 迭代顺序可能不同，导致两卡 batch 顺序不一致，`all_reduce` 维度错位（正是 #113 修复的问题）。
4. `inflight_tokens = (30+10+50) + (1-1)*3 = 90`。prefill 调度时这 90 个 token 会被预先从 `available_size` 扣除，作为在途 decode 的预留。

> 这是一张「状态推演」练习，全程手算即可，无需 GPU。

---

## 6. 本讲小结

- `DecodeManager` 是个极简 dataclass，靠 `running_reqs: Set[Req]` 维护「正在 decode 的请求」，自身不含任何 tensor——调度逻辑与张量数据分离，是 overlap scheduling 的前提。
- `filter_reqs` 用「`union` 再按 `can_decode` 过滤」一行完成「加入新请求 + 剔除已结束请求」；`ChunkedReq` 因 `can_decode` 恒 `False` 而天然被挡在 decode 集合外。
- **decode 批按 `uid` 排序**是 TP 正确性的命门：`set` 迭代顺序不确定，不同 rank 会产出顺序不同的 batch，导致 `all_reduce` 维度错位。当前 HEAD 的提交 `#113` 正是把 `list(...)` 改成 `sorted(..., key=uid)` 修复了它。
- `inflight_tokens = Σ remain_len + (page_size-1)×请求数`，为在途 decode 向未来预留 KV 空间，被 prefill 作为 `reserved_size` 初值，防止 prefill 把显存吃光。
- `TableManager` 是 `table_idx` 行号发号器：`allocate()` 即 `pop()`、`free()` 即 `append()`，LIFO、O(1)；行号是请求级资源，与序列长度无关。
- `page_table`（K/V 寻址）与 `token_pool`（token id 草稿）同形 `(max_running_req+1, aligned_max_seq_len)`，行=请求、列=序列位置；`dummy_req` 复用 `token_pool`，故整池必须零初始化，使 dummy 行读到合法 token id `0`。
- `max_running_req` 同时约束「table 槽位数」与「真实请求行数」，二者恒等；`+1` 行专供 dummy，永不参与分配。`--shell-mode` 下它被强制为 1。

---

## 7. 下一步学习建议

本讲把 `_schedule_next_batch` 的 decode 一半和底层 `table_idx`/`token_pool` 讲清了。接下来：

- **横向补全调度**：阅读 [scheduler/cache.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py)（`CacheManager`），看 `allocate_paged` 如何把 KV 页号写进 `page_table`、`cache_req` 如何在请求结束时回收页——它和 `TableManager.free` 是配对的「行回收 + 页回收」。
- **纵向进入执行**：进入 u5-l1（Engine 初始化与显存管理），看 `page_table` 的列数 `aligned_max_seq_len` 与 KV 页总数如何由显存反推出来；这会把本讲的「列维度」补全。
- **看 token_pool 的真正消费者**：进入 u5-l2（Engine forward 与采样），看 `batch.input_ids` 如何进入 embedding、采样出的 `next_tokens_gpu` 如何被 `token_pool[output_mapping]` 接收，形成「写→推进→读」的 decode 接力。
- **建议动手**：在 [scheduler.py:L229](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L229) 与 [scheduler.py:L231](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L231) 加调试日志，打印 `token_pool` 的 gather/scatter，亲眼确认 decode 的「写→读」时序。
