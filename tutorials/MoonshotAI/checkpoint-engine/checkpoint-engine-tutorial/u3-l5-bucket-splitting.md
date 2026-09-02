# bucket 切分与 bucket size 自动探测

## 1. 本讲目标

上一讲（u3-l4）我们读完了 `update` 广播主流程的四拍循环，但当时有两个输入是"从天上掉下来的"：

- `bucket_size` 到底是多少？谁决定的？
- 那一份 `buckets` 列表（每个元素是 `(receiver_rank, owner_rank, bucket)`）是怎么从全局参数元数据里切出来的？

学完本讲，你应该能够：

1. 读懂 `_gen_h2d_buckets` 的切桶循环：它如何按 owner 分组、按 `bucket_size` 软上限把连续张量聚成 `H2DBucket`，并保证**永远不把一个张量切成两半**。
2. 读懂 `_detect_bucket_size` 的预算逻辑：为什么启用 `h2d_buffer` 时预算是全集群最小空闲显存的 1/3、禁用时是 1/2；`PS_MAX_BUCKET_SIZE_GB`（默认 8 GiB）和最大单张量如何参与最终的 `min/max` 收敛。
3. 会用环境变量 `PS_MAX_BUCKET_SIZE_GB`（以及构造参数 `mem_fraction` / 环境变量 `PS_MEM_FRACTION`）调整桶大小，并能从日志里确认自己的调整生效了。

本讲所有实践均可在纯 CPU 环境完成。

## 2. 前置知识

承接前几讲已建立的概念，这里只做简要回顾：

- **对齐槽位（aligned slot）**：每个张量在锁页缓冲区里占据的字节数不是它的原始大小，而是向上取整到 256 字节倍数的 `aligned_size`（见 [pin_memory.py:23](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L23) 的 `_ALIGN_SIZE = 256`，公式回顾见 u2-l2）：

  \[ \text{aligned\_size} = \left\lceil \frac{\text{dtype.itemsize} \times \text{numel}}{256} \right\rceil \times 256 \]

- **ParameterMeta**：`(name, dtype, shape, aligned_size)` 四字段模型，是切桶循环里最基本的"货物单元"（u2-l1）。
- **全局参数表 `_current_global_parameter_metas`**：`gather_metas` 之后，每个持有权重的 rank（owner）对应一个 `MemoryBufferMetaList`，里面有若干块锁页 buffer 的元数据（u3-l3）。
- **三阶段流水线与显存代价**：Broadcast 更新消耗的设备显存约为 `3 × bucket_size`（1 份 `h2d_buffer` + 2 份双缓冲 `buffer`）；禁用 `h2d_buffer` 后降为 `2 × bucket_size`（u3-l4）。
- **all_reduce**：对一个分布在所有 rank 上的张量做约减（求 min/sum 等），结果所有 rank 都能拿到。本讲会看到一个"一次约减同时取两个统计量"的小技巧。

一句话概括本讲要解决的问题：**把"哪些字节、从哪个 owner 的哪块锁页内存、按什么顺序搬进多大显存缓冲"变成一份可执行的清单，并且清单的粒度（桶大小）要根据当前集群最紧张的那张卡自动定。**

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| [checkpoint_engine/ps.py:68-105](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L68-L105) | `_gen_h2d_buckets` | 切桶主循环：全局元数据 → `H2DBucket` 列表 |
| [checkpoint_engine/ps.py:632-682](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L632-L682) | `ParameterServer._detect_bucket_size` | 桶大小自动探测与 `disable_h2d_buffer` 回退 |
| [checkpoint_engine/ps.py:108-163](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L108-L163) | `_assign_receiver_ranks` | P2P 模式下为桶挑接收端（本讲只提一句，详见 u5-l6） |
| [checkpoint_engine/data_types.py:78-87](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L78-L87) | `BucketRange`、`H2DBucket` | 桶与桶内字节区间的数据结构 |
| [checkpoint_engine/ps.py:804-826](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L804-L826) | `_update_per_bucket` 中的调用点 | 探测 → 切桶 → 分配 `h2d_buffer` 与双缓冲 |
| [checkpoint_engine/ps.py:684-714](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L684-L714) | `_copy_to_buffer` | 桶的消费方：按 `BucketRange` 装填缓冲 |
| [README.md:179-182](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L179-L182) | Environment Variables | `PS_MAX_BUCKET_SIZE_GB` 官方文档 |

## 4. 核心概念与源码讲解

### 4.1 H2DBucket：桶的载体数据结构

#### 4.1.1 概念说明

一次 Broadcast 更新不可能把 1 TB 权重一口气塞进显存，必须分成若干批，每一批就是一个 **bucket（桶）**。桶是流水线的基本调度单位：u3-l4 的四拍循环每轮处理一个桶，双缓冲按 `gidx % 2` 在两个半区之间交替。

`H2DBucket` 就是桶的 Python 表示。它回答三个问题：

- `size`：这个桶总共有多少字节（是各张量 `aligned_size` 之和，不是简单张量字节数之和）；
- `ranges`：这些字节分别要从 owner 的**哪块**锁页 buffer 的**哪个偏移**取多少长度——这是"取货清单"；
- `items`：桶里装了哪些 `ParameterMeta`，最终会被 `_to_named_tensor` 转成发给 worker 的张量清单。

#### 4.1.2 核心流程

一个桶的生命周期：

```text
_gen_h2d_buckets 产出
    → _update_per_bucket: buffer_b = buffer[gidx%2*bucket_size : +bucket.size]
    → _copy_to_buffer 按 ranges 把字节装进 buffer_b（本地 H2D 或远端 RDMA 读）
    → dist.broadcast(buffer_b, src=receiver_rank)
    → _to_named_tensor(bucket.items, gidx%2*bucket_size) 告诉 worker 张量在哪
```

关键不变量：`bucket.size == sum(r.size for r in bucket.ranges)`，即"取货清单"的长度必须恰好等于桶声明的字节数（`_copy_to_buffer` 末尾的 `assert offset == bucket.size` 检查的就是它）。

#### 4.1.3 源码精读

[H2DBucket 的定义在 data_types.py:84-87](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L84-L87)：

```python
class H2DBucket(BaseModel):
    size: int
    ranges: list[BucketRange]
    items: list[ParameterMeta]
```

三个字段正好对应上面说的三个问题。注意它是 pydantic `BaseModel` 而不是 `NamedTuple`——因为桶的描述需要跨进程/JSON 边界（与 u2-l1 讲过的设计取舍一致：跨边界用 BaseModel，纯进程内用 NamedTuple）。

桶被装填和消费的地方在 [ps.py:876-890](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L876-L890)：

```python
start = gidx % 2 * bucket_size
buffer_b: torch.Tensor = buffer[start : start + bucket.size]
if receiver_rank == self._rank:
    if disable_h2d_buffer:
        ...
    else:
        buffer_b.data.copy_(h2d_buffer[: bucket.size])
dist.broadcast(buffer_b, src=receiver_rank, group=ranks_group)
```

这段代码说明：桶在显存里的落点由 `gidx % 2`（双缓冲半区）决定，而**桶的真实长度是 `bucket.size`，不是 `bucket_size`**——最后一个桶往往装不满，切片 `buffer[start : start + bucket.size]` 会自动缩短。发给 worker 的张量偏移同理基于半区起点，见 [ps.py:35-48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L35-L48) 的 `_to_named_tensor`（`offset` 参数传入的就是 `gidx % 2 * bucket_size`，调用点在 [ps.py:904](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L904)）。

#### 4.1.4 代码实践

**实践目标**：确认 `H2DBucket` 是可 JSON 化的普通 pydantic 模型，并验证 `size` 与 `ranges` 的不变量可以在构造侧手工维护。

**操作步骤**（纯 CPU，项目根目录下运行）：

```python
# practice_h2dbucket.py（示例代码）
from torch import Size, float16
from checkpoint_engine.data_types import BucketRange, H2DBucket, ParameterMeta

m0 = ParameterMeta(name="w0", dtype=float16, shape=Size([512]), aligned_size=1024)
m1 = ParameterMeta(name="w1", dtype=float16, shape=Size([256]), aligned_size=512)

bucket = H2DBucket(
    size=m0.aligned_size + m1.aligned_size,
    ranges=[BucketRange(0, 0, m0.aligned_size), BucketRange(0, 1024, m1.aligned_size)],
    items=[m0, m1],
)
print(bucket.model_dump_json(indent=2))
assert bucket.size == sum(r.size for r in bucket.ranges)
```

**需要观察的现象**：JSON 输出里 `dtype` 变成了字符串 `"torch.float16"`、`shape` 变成了数组——这是 u2-l1 讲过的 `_TorchDtype`/`_TorchSize` 自定义序列化在工作；`ranges` 里每个元素是三元数组（NamedTuple 的 JSON 形态）。

**预期结果**：断言通过；`bucket.model_dump()` 返回纯 Python 类型的 dict。这正是 metas 导出（u6-l3）能携带桶信息的基础。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `H2DBucket.size` 用"aligned_size 之和"而不是"张量字节数之和"？

**答案**：因为字节搬运的最小单位是对齐槽位。`BucketRange` 描述的是锁页 buffer 里的字节区间，而张量在锁页 buffer 里就是按 `aligned_size` 紧挨着排布的（offset 由 `aligned_size` 逐个累加，见 u2-l3/u2-l4）。用原始字节数求和会导致区间和实际内存布局对不上。

**练习 2**：桶在显存里的位置由哪个表达式决定？为什么最后一个桶不需要特殊处理？

**答案**：由 `start = gidx % 2 * bucket_size` 决定（双缓冲半区选择）。最后一个桶通常 `bucket.size < bucket_size`，Python 切片 `buffer[start : start + bucket.size]` 自动以 `bucket.size` 为实际长度，广播和 `_to_named_tensor` 都基于这个切片与 `bucket.items`，因此无需特殊处理。

---

### 4.2 BucketRange：桶内字节的"取货清单"

#### 4.2.1 概念说明

一个 owner 的权重分散在若干块锁页 buffer（`MemoryBuffer`，normal pin 按约 4 GiB 切的内存桶，inplace pin 则一个文件一块）里。切桶时，一个 `H2DBucket` 的字节可以来自**不同锁页 buffer 的不同片段**——只要它们在参数排序上是连续的。

`BucketRange` 用三元组 `(idx, offset, size)` 描述其中一段：

- `idx`：owner 的 `memory_pool` 里第几块锁页 buffer（源码注释称之为 "bucket_idx of MemoryBucket in memory_pool"）；
- `offset`：这块 buffer 内的起始字节偏移；
- `size`：这段有多少字节。

它是纯 `NamedTuple`（[data_types.py:78-82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L78-L82)），因为它只在 PS 进程内使用，从不需要序列化出境：

```python
class BucketRange(NamedTuple):
    idx: int  # bucket_idx of MemoryBucket in memory_pool
    offset: int
    size: int
```

#### 4.2.2 核心流程

`BucketRange` 的两个消费方向（都在 `_copy_to_buffer`，[ps.py:684-714](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L684-L714)）：

```text
owner 在本地（Broadcast 且我就是 owner）:
    for b in bucket.ranges:
        buffer[offset : offset+b.size].copy_(本地pool[b.idx].buffer[b.offset : b.offset+b.size])
        # 即：从第 b.idx 块锁页 buffer 的 b.offset 处取 b.size 字节，追加到目标缓冲

owner 在远端（P2P）:
    for b in bucket.ranges:
        remote_ptrs.append(远端第 b.idx 块 buffer 的指针 + b.offset); lens.append(b.size)
    最后聚成一次 batch_transfer_sync_read 批量 RDMA 读
```

也就是说，`BucketRange` 是同一份"字节配方"的两种用法：本地当**切片下标**用，远端当**绝对地址**用。

#### 4.2.3 源码精读

[ps.py:696-711](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L696-L711) 是本地装填路径，可以清楚看到 `b.idx` 如何索引内存池、`b.offset/b.size` 如何切片：

```python
for b in bucket.ranges:
    assert offset + b.size <= bucket.size, ...
    if owner_rank is not None:
        buf_ptrs.append(ptr_base + offset)
        remote_ptrs.append(ptrs[b.idx][0] + b.offset)
        lens.append(b.size)
    else:
        pool = self._get_memory_pool(checkpoint_name)[b.idx]
        buffer[offset : offset + b.size].data.copy_(
            pool.buffer[b.offset : b.offset + b.size],
            non_blocking=True,
        )
    offset += b.size
assert offset == bucket.size, f"offset {offset} != bucket_size {bucket.size}"
```

两处防御：每个 range 装填前断言不越过 `bucket.size`；装填完断言总字节数恰好等于 `bucket.size`。这两条断言就是 4.1 节不变量的运行时把关。

#### 4.2.4 代码实践

**实践目标**：手工推演一个小桶的 `BucketRange`，体会"idx/offset/size 三个数就是一段字节"。

**操作步骤**：设 owner 的内存池有两块 buffer：`pool[0]` 依次放着 `aligned_size` 为 600 KiB、600 KiB、600 KiB 的三个张量，`pool[1]` 放着 1024 KiB 的一个张量。若一个桶要装"pool[0] 的第三个张量 + pool[1] 的第一个张量"，写出它的 `ranges` 和 `size`（1 KiB = 1024 字节）。

**需要观察的现象**：先在纸上写，再用下面三行验证（示例代码）：

```python
ranges = [(0, 2 * 600 * 1024, 600 * 1024), (1, 0, 1024 * 1024)]
size = sum(r[2] for r in ranges)
print(size / 1024, "KiB")  # 期望 1624.0
```

**预期结果**：`ranges = [BucketRange(0, 1228800, 614400), BucketRange(1, 0, 1048576)]`，`size = 1662976` 字节（1624 KiB）。注意 pool[0] 内第三个张量的偏移是前两个槽位之和 `2 × 600 KiB`，而不是"前两个张量的原始字节数之和"——这里恰好相等是因为 600 KiB 本身就是 256 的倍数。

#### 4.2.5 小练习与答案

**练习 1**：一个 `H2DBucket` 的 `ranges` 里能出现两个 `idx` 相同的区间吗？

**答案**：能。切桶循环按参数顺序推进，如果在同一块锁页 buffer 内部发生了切桶（当前桶放不下下一个张量），当前 buffer 会被分成多个区间，分属相邻的桶；但**同一个桶内**同一 `idx` 只会出现一个连续区间（循环对每段连续字节只 append 一次）。跨桶才会看到同 `idx` 的多个区间。

**练习 2**：`BucketRange` 为什么不做乘法校验（比如检查 `offset+size` 不超过该 buffer 的总长）？

**答案**：切桶循环从 `MemoryBufferMetas.metas`（每块 buffer 的参数清单）出发累加 `aligned_size` 生成区间，区间天然落在 buffer 范围内；`_copy_to_buffer` 再用 `bucket.size` 一层的断言兜底。这是"生成时保证、消费时抽查"的分层防御。

---

### 4.3 `_gen_h2d_buckets`：按 owner 与软上限切桶

#### 4.3.1 概念说明

`_gen_h2d_buckets` 是一个**模块级纯函数**（不在 `ParameterServer` 类里），输入是 gather 来的全局元数据和一个目标桶大小，输出桶列表。它解决的问题是：

1. **按 owner 分组**：一个桶的字节永远来自同一个 owner。这样 P2P 模式下每次 RDMA 读只对接一个远端，Broadcast 模式下每个桶有明确的 `owner_rank` 可查（`_get_addr_ptrs`）。
2. **软上限**：`bucket_size` 是"尽量不超过"的软约束。切桶的原子是一个张量的**整个对齐槽位**——判断条件是"加上这个张量会不会超"，而不是"超了就切一刀"。因此单个超大张量永远不会被腰斩。
3. **决定 receiver**：`ranks` 为空（Broadcast）时 owner 即 receiver，返回 `[(owner, owner, bucket)]`；`ranks` 非空（P2P）时交给 `_assign_receiver_ranks` 按 RDMA 拓扑挑接收端（u5-l6 专门讲）。

#### 4.3.2 核心流程

伪代码（保留关键变量名）：

```text
for owner_rank, items in global_metas.items():          # 逐 owner，绝不混桶
    新建空桶
    for idx, metas in enumerate(items.memory_buffer_metas_list):   # 逐锁页 buffer
        start_offset = offset = 0                        # 偏移在每块 buffer 内重新从 0 计
        for meta in metas.metas:                         # 逐张量（按排布顺序）
            s = meta.aligned_size
            if 当前桶.size + s > bucket_size:            # 软上限检查（严格大于）
                if offset - start_offset > 0:            # 当前 buffer 已有累计字节
                    当前桶.ranges.append(BucketRange(idx, start_offset, offset - start_offset))
                start_offset = offset
                新建空桶
            offset += s
            当前桶.size += s; 当前桶.items.append(meta)   # 张量整体进入当前桶
        当前桶.ranges.append(BucketRange(idx, start_offset, offset - start_offset))  # 收尾区间
    assert 最后一个桶.size > 0                            # 每个 owner 至少产出一个非空桶

if ranks 为空:  return [(owner, owner, bucket) ...]      # Broadcast：自己广播自己的桶
else:           return _assign_receiver_ranks(...)       # P2P：按拓扑挑 receiver
```

三个值得注意的细节：

- 判断用的是 `>` 不是 `>=`，所以桶**恰好装到 `bucket_size`** 是允许的；
- 收尾的 `ranges.append` 在每块 buffer 的循环结束后执行一次，把"自上次切桶以来的尾巴"闭合进当前桶；因此一个桶的 `ranges` 可以横跨多块锁页 buffer（多个 `idx`）；
- 切桶绝不切开张量：这是"软"上限的含义。生产路径中 `_detect_bucket_size` 保证 `bucket_size ≥ max_tensor_bytes`（见 4.4），软上限实际上不会被突破；这个防御性写法只在离线误用时才暴露（见下面实践的第 3 步）。

#### 4.3.3 源码精读

完整实现在 [ps.py:68-105](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L68-L105)，核心内循环（[ps.py:80-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L80-L93)）：

```python
start_offset, offset = 0, 0
for meta in metas.metas:
    s = meta.aligned_size
    if buckets[-1][1].size + s > bucket_size:
        if offset - start_offset > 0:
            buckets[-1][1].ranges.append(
                BucketRange(idx, start_offset, offset - start_offset)
            )
        start_offset = offset
        buckets.append((owner_rank, H2DBucket(size=0, ranges=[], items=[])))
    offset += s
    buckets[-1][1].size += s
    buckets[-1][1].items.append(meta)
buckets[-1][1].ranges.append(BucketRange(idx, start_offset, offset - start_offset))
```

`buckets` 的元素是 `(owner_rank, H2DBucket)`，切桶时 `buckets[-1]` 永远指向"当前正在填充的桶"。注意切桶发生时**先闭合区间、再开新桶、然后才把当前张量放入新桶**——顺序保证了区间不重不漏。

owner 到 receiver 的分岔在 [ps.py:97-105](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L97-L105)：

```python
ranks_set = set(ranks) if ranks else set()
actual_local_topo = (
    {k: v & ranks_set for k, v in local_topo.items() if v & ranks_set} if ranks else local_topo
)
# if ranks is empty, assign the owner_rank as receiver_rank, this is used for colocate architecture
if not ranks:
    return [(owner_rank, owner_rank, bucket) for owner_rank, bucket in buckets]
else:
    return _assign_receiver_ranks(buckets, actual_local_topo, remote_topo)
```

Broadcast 模式（colocate 部署）里 owner 就是 receiver：这正是 u3-l4 讲过的"倒置广播源"——数据持有者直接当 `dist.broadcast` 的 `src`。

#### 4.3.4 代码实践

**实践目标**：在纯 CPU 环境驱动 `_gen_h2d_buckets`，验证切桶结果与手推一致，并观察"软上限"的两个边界行为。

**操作步骤**：

1. 编写脚本（示例代码）：

```python
# practice_gen_buckets.py
from torch import Size, bfloat16
from checkpoint_engine.data_types import (
    MemoryBufferMetaList, MemoryBufferMetas, ParameterMeta,
)
from checkpoint_engine.ps import _gen_h2d_buckets

def mk(name, kib):  # 构造 aligned_size 恰为 kib KiB 的元数据
    nbytes = kib * 1024
    assert nbytes % 256 == 0
    return ParameterMeta(name=name, dtype=bfloat16,
                         shape=Size([nbytes // 2]), aligned_size=nbytes)

def owner_metas(rank, per_file):  # per_file: 该 owner 每块 buffer 里各张量的 KiB 数
    return MemoryBufferMetaList(
        p2p_store_addr=None, rdma_device="",
        memory_buffer_metas_list=[
            MemoryBufferMetas(
                metas=[mk(f"rank{rank}_f{fi}_w{ti}", k) for ti, k in enumerate(file)],
                ptr=0, size=sum(file) * 1024)
            for fi, file in enumerate(per_file)
        ],
    )

global_metas = {0: owner_metas(0, [[600, 600, 600, 600], [1024, 1024]]),  # owner0: 两块 buffer
                1: owner_metas(1, [[1400, 1400, 1400]])}                  # owner1: 一块 buffer

BUCKET = 2000 * 1024
result = _gen_h2d_buckets(global_metas, BUCKET, {}, {}, ranks=None)

for receiver, owner, b in result:
    assert receiver == owner                       # Broadcast: owner 即 receiver
    assert b.size == sum(r.size for r in b.ranges) # 不变量: 清单长度 == 桶大小
    print(f"owner={owner} size={b.size/1024:.0f}KiB ranges={b.ranges} items={[m.name for m in b.items]}")
print("buckets per owner:", {o: sum(1 for _, ow, _ in result if ow == o) for o in (0, 1)})
```

2. 运行 `python practice_gen_buckets.py`。

**需要观察的现象与预期结果**（可先手推再对照）：

| 桶序 | owner | size (KiB) | ranges（idx, 起, 长，单位 KiB） | items |
| --- | --- | --- | --- | --- |
| 0 | 0 | 1800 | (0, 0, 1800) | 3 个 600KiB 张量 |
| 1 | 0 | 1624 | (0, 1800, 600) **和** (1, 0, 1024) | 1 个 600KiB + 1 个 1024KiB |
| 2 | 0 | 1024 | (1, 1024, 1024) | 1 个 1024KiB |
| 3 | 1 | 1400 | (0, 0, 1400) | 1 个 1400KiB |
| 4 | 1 | 1400 | (0, 1400, 1400) | 1 个 1400KiB |
| 5 | 1 | 1400 | (0, 2800, 1400) | 1 个 1400KiB |

重点看**桶 1**：它的 `ranges` 同时含 `idx=0` 和 `idx=1`——桶跨锁页 buffer 边界，这正是 4.2 节"一个桶、多段字节"的实例。owner0 产出 3 个桶、owner1 产出 3 个桶，对应广播主循环的 `max_len = 3` 轮。

3. 再把 `BUCKET` 改成 `1200 * 1024` 重跑（小于 owner1 第一个张量的 1400 KiB）。观察：owner1 的结果里**第一个桶是空桶**（`size=0`、`ranges=[]`、`items=[]`）。原因是循环为每个 owner 先无条件创建一个空桶，而"放不下就开新桶"的判断在放入第一个张量前就触发了；末尾的 `assert buckets[-1][1].size > 0` 只检查每个 owner 的**最后一个**桶，拦不住这个开头的空桶。生产路径中 `_detect_bucket_size` 保证 `bucket_size ≥ max_tensor_bytes`，这一分支不会触发，但离线实验时需要知道它的存在。

若你的运行结果与上表不符，请回查 4.3.2 的伪代码逐行核对（本表由源码逻辑手推得出，判定条件是严格大于 `>`；本讲写作环境未能实际执行该脚本，**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：把判断条件 `buckets[-1][1].size + s > bucket_size` 改成 `>=` 会怎样？

**答案**：恰好装满 `bucket_size` 的桶会被强制提前收尾，产出更多更小的桶（例如 4.3.4 实践里 owner0 的前三个 600 KiB 张量在 2000 KiB 上限下，`>` 时能同桶共处 1800 KiB，`>=` 时第三个张量就会触发 `1200+600 ≥ 1800`？不会——注意比较对象是 `bucket_size` 而不是当前累计值；真正的差别出现在累计值恰好等于 `bucket_size` 时：`>` 允许"装满即止"，`>=` 会在已装满后再来一个张量时多切一次，两者只在边界相等时不同）。核心结论：`>` 允许桶恰好等于 `bucket_size`，语义上是"不超过"而非"严格小于"。

**练习 2**：为什么每个 owner 强制另起一个新桶，而不是接着上一个 owner 的桶继续装？

**答案**：桶是 `dist.broadcast` 的 `src` 选择和 P2P RDMA 读的目标单位的依据（`(receiver_rank, owner_rank, bucket)` 三元组里 owner 是桶的属性）。若一个桶混装两个 owner 的字节，广播时无法用单一的 `owner_rank` 定位数据来源，`_copy_to_buffer` 的远端批量读也无法把一段字节对应到单一远端地址空间。

**练习 3**：如果某个 rank 注册了空 checkpoint（空注册，u3-l3 讲过它会缺席 `_current_global_parameter_metas`），对切桶有什么影响？

**答案**：没有影响——`_gen_h2d_buckets` 只遍历 `global_metas.items()`，空注册的 rank 根本不在字典里，不产出桶。它仍可作为 receiver 参与 P2P 接收，但不会成为 owner。

---

### 4.4 `_detect_bucket_size`：显存预算与 h2d_buffer 开关

#### 4.4.1 概念说明

桶大小是一个**全局共识**：所有 rank 必须用同一个 `bucket_size`，否则双缓冲半区的切分（`gidx % 2 * bucket_size`）就对不齐了。同时它受制于**全集群最紧张的那张卡**——木桶效应：任何一张 rank 的显存装不下 `3 × bucket_size`，流水线就会 OOM。

`_detect_bucket_size` 一次做完三件事：

1. 用一次 `all_reduce(MIN)` 拿到全集群最小的"空闲显存 × mem_fraction"，同时（用负数技巧）拿到全集群最大的 ZMQ 计数器；
2. 决定是否禁用 `h2d_buffer`：如果最大单张量连"空闲显存 / 3"都放不下，就放弃 `h2d_buffer` 省下一份桶显存，预算放宽到 /2；
3. 结合用户上限 `PS_MAX_BUCKET_SIZE_GB`（默认 8 GiB）与最大张量，收敛出最终 `bucket_size`。

#### 4.4.2 核心流程

```text
打包一个 2 元 int64 张量上设备:
    [ 本机空闲显存 × mem_fraction,  -本机 zmq_addr_counter ]
一次 all_reduce(MIN):
    free_bytes      = 所有 rank 中的最小值
    zmq_addr_counter = -min(-counter_i) = 所有 rank 中的最大值   ← 负数技巧

扫描全局元数据求 max_tensor_bytes（最大的 aligned_size）

k = 3（启用 h2d_buffer 时的份数）
若 max_tensor_bytes ≤ ⌊free/(3×256)⌋×256 且未强制禁用:
    预算 free = ⌊free/(3×256)⌋×256                # 3×bucket ≤ free ⇒ 不会 OOM
否则:
    预算 free = ⌊free/(2×256)⌋×256；断言 max_tensor_bytes ≤ 预算
    disable_h2d_buffer = True                     # 只剩 2×bucket ≤ free

bucket_size = min( max(B_max, max_tensor_bytes), free )
    其中 B_max = PS_MAX_BUCKET_SIZE_GB × 2^30（默认 8 GiB）
返回 (bucket_size, disable_h2d_buffer)
```

写成公式（\(F\) 为 mem_fraction，默认 0.9；\(A = 256\)）：

\[ \text{bucket\_size} = \min\left(\max\left(B_{\max},\ s_{\max}\right),\ \left\lfloor \frac{f \cdot F}{k \cdot A} \right\rfloor \cdot A \right), \qquad k = \begin{cases} 3, & \text{启用 } \texttt{h2d\_buffer} \\ 2, & \text{禁用 } \texttt{h2d\_buffer} \end{cases} \]

其中 \(f\) 是全集群最小空闲显存，\(s_{\max}\) 是最大单张量的 `aligned_size`，\(B_{\max}\) 是用户上限。

三层语义：

- **\(\max(B_{\max}, s_{\max})\)（下限）**：桶必须装得下最大单张量（切桶不切开张量，4.3 节的软上限因此实际不会破）；8 GiB 只是"偏好值"，超大张量可以顶破它。
- **\(\lfloor f \cdot F / (kA) \rfloor \cdot A\)（上限/预算）**：保证 \(k \times \text{bucket\_size} \le f \cdot F\)，即流水线的全部临时显存（h2d_buffer 1 份 + 双缓冲 2 份，或仅双缓冲 2 份）在最小的那张卡上也放得下。向下对齐到 256 的倍数，推测是为了与张量槽位的对齐粒度（`_ALIGN_SIZE`）保持一致（源码未注释，此为一处理解）。
- **回退条件**：\(s_{\max} > f \cdot F / 3\) 时放弃 `h2d_buffer`。代价是各 rank 的 H2D 无法在广播链之外并行（带宽受限于单机 H2D），收益是省下一整份桶显存；若连 \(f \cdot F / 2\) 都装不下 \(s_{\max}\)，直接 `assert` 失败报错——没有任何桶大小能同时满足约束。

#### 4.4.3 源码精读

打包与约减在 [ps.py:638-655](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L638-L655)：

```python
GiB = 1 << 30  # noqa: N806
tensor = torch.tensor(
    [
        # proportion of current device free memory bytes
        int(float(self.device_manager.device_module.mem_get_info()[0]) * self._mem_fraction),
        # we use negative value to reuse allreduce min operation
        # for getting the max value of zmq_addr_counter in all ranks
        -self._zmq_addr_counter,
    ],
    dtype=torch.int64,
    device=self.device_manager.device_type,
)
dist.all_reduce(tensor, op=torch.distributed.ReduceOp.MIN, group=ranks_group)
tensor = tensor.cpu()
free_bytes, self._zmq_addr_counter = tensor[0].item(), -tensor[1].item()
```

两个细节：

- `mem_get_info()[0]` 是设备**空闲**字节数（`[1]` 才是总量），乘上 `self._mem_fraction`——它来自构造参数 `mem_fraction` 或环境变量 `PS_MEM_FRACTION`，默认 0.9（[ps.py:208](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L208)）。
- 负数技巧：\(\min_i(-c_i) = -\max_i(c_i)\)，于是**一次** MIN 约减同时拿到"最小空闲显存"和"最大 ZMQ 计数器"。后者写回 `self._zmq_addr_counter`，让所有 rank 之后在 [`_bind_zmq_socket`（ps.py:622-630）](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L622-L630) 里生成**完全相同**的抽象 UDS 地址列表（`ipc://@checkpoint-engine-{uuid}-{counter}.sock`）——每个 rank 只 bind 自己 uuid 的地址，但要把全量地址表交给 `req_func` 去通知 worker 连接，计数器不同步就会连错。这也解释了为什么 `_update_per_bucket` 里必须**先**调用 `_detect_bucket_size`（[ps.py:804](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L804)）再 `_bind_zmq_socket`（[ps.py:842](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L842)）。ZMQ 协议本身是 u3-l6 的主题。

分支与收敛在 [ps.py:656-682](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L656-L682)：

```python
max_tensor_bytes = 0
for items in self._current_global_parameter_metas.values():
    for metas_list in items.memory_buffer_metas_list:
        for meta in metas_list.metas:
            max_tensor_bytes = max(max_tensor_bytes, meta.aligned_size)
free_bytes_divided_3 = free_bytes // (3 * _ALIGN_SIZE) * _ALIGN_SIZE
if max_tensor_bytes <= free_bytes_divided_3 and not disable_h2d_buffer:
    self._logger_rank0(f"[rank{self._rank}] use h2d buffer")
    free_bytes = free_bytes_divided_3
else:
    self._logger_rank0(
        f"[rank{self._rank}] disable h2d buffer when max_tensor_bytes {max_tensor_bytes} is larger than free_bytes {free_bytes} // 3"
    )
    free_bytes = free_bytes // (2 * _ALIGN_SIZE) * _ALIGN_SIZE
    assert max_tensor_bytes <= free_bytes, ...
    disable_h2d_buffer = True
max_bytes = int(float(os.getenv("PS_MAX_BUCKET_SIZE_GB", "8")) * GiB)
bucket_size = min(max(max_bytes, max_tensor_bytes), free_bytes)
logger.info(f"[rank{self._rank}] auto detect bucket size {bucket_size / GiB:.2f} GiB")
return bucket_size, disable_h2d_buffer
```

注意 `max_tensor_bytes` 扫的是**全局**元数据（包括别的 rank 的张量），因为每个 receiver 桶都要装得下任意 owner 的大张量。`disable_h2d_buffer` 形参默认 `False`，当前仓库里唯一调用点 [ps.py:804](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L804) 并不覆盖它——它是留给外部强制禁用的口子。

`_mem_fraction` 的来源在 [ps.py:208](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L208)：

```python
self._mem_fraction = mem_fraction or float(os.getenv("PS_MEM_FRACTION", "0.9"))
```

`PS_MAX_BUCKET_SIZE_GB` 的官方文档在 [README.md:179-182](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L179-L182)（"An integer is used to set the maximum bucket size... If not set, 8GB is used as default"）；`PS_MEM_FRACTION` 未列入 README 的环境变量表，只能从源码看到。

桶大小确定后，两块设备缓冲在 [ps.py:813-826](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L813-L826) 分配——这就是预算公式的落点：

```python
h2d_buffer: torch.Tensor | None = (
    None
    if disable_h2d_buffer
    else torch.empty(bucket_size, dtype=torch.uint8, device=self.device_manager.device_type)
)
...
buffer = torch.empty(
    bucket_size * 2, dtype=torch.uint8, device=self.device_manager.device_type
)
```

`h2d_buffer` 1 份 + 双缓冲 2 份 = 3 × `bucket_size`；禁用时只剩 `bucket_size * 2`。这正是 /3 与 /2 的由来。

#### 4.4.4 代码实践

**实践目标**：在不依赖 GPU/分布式的前提下复刻 `_detect_bucket_size` 的算术，观察三种典型场景下分支与结果的变化，掌握 `PS_MAX_BUCKET_SIZE_GB` 的调节效果。

**操作步骤**：

1. 保存以下脚本（**示例代码**：逻辑逐行复刻自 ps.py:632-682，把"显存探测 + all_reduce"换成了直接传参，便于 CPU 演算）：

```python
# practice_detect_bucket.py
import os

ALIGN, GiB = 256, 1 << 30

def detect(free_bytes, max_tensor_bytes, mem_fraction=0.9, disable_h2d_buffer=False):
    free_bytes = int(free_bytes * mem_fraction)          # 对应打包进张量前的缩放
    d3 = free_bytes // (3 * ALIGN) * ALIGN
    if max_tensor_bytes <= d3 and not disable_h2d_buffer:
        free_bytes = d3                                  # 启用 h2d_buffer，预算 free/3
        tag = "use h2d buffer"
    else:
        free_bytes = free_bytes // (2 * ALIGN) * ALIGN   # 禁用，预算 free/2
        assert max_tensor_bytes <= free_bytes
        disable_h2d_buffer, tag = True, "disable h2d buffer"
    max_bytes = int(float(os.getenv("PS_MAX_BUCKET_SIZE_GB", "8")) * GiB)
    bucket_size = min(max(max_bytes, max_tensor_bytes), free_bytes)
    print(f"{tag:20s} bucket_size = {bucket_size / GiB:.2f} GiB")
    return bucket_size, disable_h2d_buffer

G = 1024**3
detect(20 * G, 0.5 * G)        # A: 显存充裕
detect(20 * G, 7 * G)          # B: 大张量触发回退
detect(6 * G, 0.5 * G)         # C: 显存紧张
os.environ["PS_MAX_BUCKET_SIZE_GB"] = "2"
detect(20 * G, 0.5 * G)        # D: 用户上限 2 GiB
detect(6 * G, 3.5 * G)         # E: 连 free/2 都装不下最大张量
```

2. 运行 `python practice_detect_bucket.py`。

**需要观察的现象与预期结果**：

| 场景 | min free（×0.9 后） | 最大张量 | 分支 | bucket_size | 设备临时显存 |
| --- | --- | --- | --- | --- | --- |
| A | 20 GiB | 0.5 GiB | use h2d buffer | ≈6.67 GiB | 3×≈6.67 ≈ 20 GiB |
| B | 20 GiB | 7 GiB | disable h2d buffer | 8.00 GiB | 2×8 = 16 GiB ≤ 20 GiB |
| C | 6 GiB | 0.5 GiB | use h2d buffer | 2.00 GiB | 3×2 = 6 GiB |
| D（上限 2 GiB） | 20 GiB | 0.5 GiB | use h2d buffer | 2.00 GiB | 3×2 = 6 GiB |
| E | 6 GiB | 3.5 GiB | assert 失败 | —（AssertionError） | — |

解读：A 中预算 `20/3 ≈ 6.67` 小于默认上限 8 GiB，**预算成了瓶颈**；B 中 7 GiB > 20/3，回退后预算 10 GiB，上限 8 GiB 反过来成为 `bucket_size`；D 演示了 `PS_MAX_BUCKET_SIZE_GB` 把桶压小（轮数变多、显存变省）；E 复现"连一半空闲显存都装不下最大张量则报错"。场景 A 的精确值：`20 GiB × 0.9 = 19327352832`，`//768×256 = 7158278656 ≈ 6.67 GiB`。以上预期值由公式手算得出（确定性算术，可直接复算），脚本本身的运行输出**待本地验证**。

3.（可选，需真实 GPU 集群，**待本地验证**）在 examples/update.py 的运行环境中 `export PS_MAX_BUCKET_SIZE_GB=2` 后发起一次更新，观察日志中的 `auto detect bucket size 2.00 GiB` 与 `use h2d buffer` 字样（分别来自 [ps.py:681](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L681) 与 [ps.py:663](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L663)；前两条 `use/disable` 日志经 `_logger_rank0` 只在本机 local_rank=0 打印，见 [ps.py:288-290](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L288-L290)）。

#### 4.4.5 小练习与答案

**练习 1**：为什么预算除以 3 或 2，而不是让用户直接指定"桶的个数"？

**答案**：因为约束的真正来源是**显存总量**而非桶个数：启用 `h2d_buffer` 时设备上同时存在 1 份 `h2d_buffer` + 2 份双缓冲 = 3 份桶大小；禁用时 2 份。让用户指定桶个数无法自动适配"全集群最小空闲显存"这个随负载浮动的值，而除法把安全性（不 OOM）交给系统、把偏好（`PS_MAX_BUCKET_SIZE_GB`）留给用户。

**练习 2**：`min(max(max_bytes, max_tensor_bytes), free_bytes)` 里两层 `min/max` 各自防御什么？

**答案**：内层 `max(max_bytes, max_tensor_bytes)` 是**下限**——桶必须装得下最大单张量（切桶不切张量），所以用户上限不能把它压到 `max_tensor_bytes` 之下；外层 `min(..., free_bytes)` 是**上限**——不能超过显存预算。由于分支条件（`s_max ≤ free/3`）或断言（`s_max ≤ free/2`）已保证 `max_tensor_bytes ≤ free_bytes`，两个约束不会冲突，恒有 `bucket_size ≥ max_tensor_bytes`，4.3 节的软上限因此在生产路径上等价于硬上限。

**练习 3**：如果删除负数计数器技巧，改为第二次 `all_reduce(MAX)` 单独取 `zmq_addr_counter` 的最大值，功能上等价吗？有什么代价？

**答案**：功能等价（都能让所有 rank 拿到全局最大计数器），代价是多一次集合通信。NCCL/HCCL 的 all_reduce 是同步屏障，每次都有微秒到毫秒级的固定开销；把两个统计量打包进同一个张量、用一次 MIN 完成，是分布式代码里常见的"捎带"优化。

## 5. 综合实践

**任务：写一个"桶规划器"（bucket planner），把本讲四个模块串起来。**

给定一个模拟集群（各 rank 的空闲显存、各 owner 的权重布局）和用户上限 `PS_MAX_BUCKET_SIZE_GB`，输出一份更新计划表。全部在 CPU 上运行（示例代码）：

```python
# bucket_planner.py
import os
from torch import Size, bfloat16
from checkpoint_engine.data_types import (
    MemoryBufferMetaList, MemoryBufferMetas, ParameterMeta,
)
from checkpoint_engine.ps import _gen_h2d_buckets

ALIGN, GiB = 256, 1 << 30

# ---- 第 1 步：模拟 _detect_bucket_size（无分布式版，复刻 ps.py:632-682 的算术）----
def detect(free_per_rank, max_tensor_bytes, mem_fraction=0.9):
    free = min(int(f * mem_fraction) for f in free_per_rank)   # all_reduce(MIN)
    d3 = free // (3 * ALIGN) * ALIGN
    disable = not (max_tensor_bytes <= d3)
    free = (d3 if not disable else free // (2 * ALIGN) * ALIGN)
    assert max_tensor_bytes <= free
    cap = int(float(os.getenv("PS_MAX_BUCKET_SIZE_GB", "8")) * GiB)
    return min(max(cap, max_tensor_bytes), free), disable

# ---- 第 2 步：搭 2 个 owner 的假元数据 ----
def mk(name, kib):
    return ParameterMeta(name=name, dtype=bfloat16,
                         shape=Size([kib * 1024 // 2]), aligned_size=kib * 1024)

def owner_metas(rank, per_file):
    return MemoryBufferMetaList(
        p2p_store_addr=None, rdma_device="",
        memory_buffer_metas_list=[
            MemoryBufferMetas(metas=[mk(f"rank{rank}_f{fi}_w{ti}", k) for ti, k in enumerate(file)],
                              ptr=0, size=sum(file) * 1024)
            for fi, file in enumerate(per_file)
        ],
    )

global_metas = {0: owner_metas(0, [[600]*4, [1024]*2]),      # ≈3.4 GiB 权重
                1: owner_metas(1, [[1400]*3])}                # ≈4.1 GiB 权重

max_tensor = max(m.aligned_size for v in global_metas.values()
                 for mb in v.memory_buffer_metas_list for m in mb.metas)

# ---- 第 3 步：探测 + 切桶 + 汇总 ----
bucket_size, disable = detect([24 * GiB, 24 * GiB, 16 * GiB], max_tensor)  # 最紧的卡是 16G
buckets = _gen_h2d_buckets(global_metas, bucket_size, {}, {}, ranks=None)

per_owner, total_items = {}, 0
for _, owner, b in buckets:
    per_owner.setdefault(owner, []).append(b.size)
    total_items += len(b.items)
    assert b.size == sum(r.size for r in b.ranges)

rounds = max(len(v) for v in per_owner.values())
print(f"bucket_size = {bucket_size/GiB:.3f} GiB, disable_h2d_buffer = {disable}")
print(f"buckets per owner (GiB): { {o: [round(s/GiB,3) for s in v] for o, v in per_owner.items()} }")
print(f"broadcast rounds (max_len) = {rounds}, total tensors = {total_items}, "
      f"peak device mem = {2 + (0 if disable else 1)} x bucket_size ≈ "
      f"{(2 + (0 if disable else 1)) * bucket_size / GiB:.2f} GiB")
```

**操作步骤**：运行 `python bucket_planner.py`；然后做两组对照实验——(a) 把最紧的卡从 16 GiB 改成 6 GiB；(b) `export PS_MAX_BUCKET_SIZE_GB=2` 后重跑。

**需要观察的现象**：

1. 基线：`max_tensor = 1400 KiB`，min free（×0.9）= 14.4 GiB，预算 /3 ≈ 4.8 GiB，`bucket_size = min(max(8G, 1400K), 4.8G) = 4.8 GiB`，启用 h2d_buffer，峰值临时显存 ≈ 14.4 GiB。
2. 实验 (a)：预算降到 1.8 GiB 仍是 1400 KiB 张量的 /3 分支，`bucket_size = 1.8 GiB`，桶数变多、轮数变多——这就是"显存越紧、流水线越碎"。
3. 实验 (b)：`bucket_size` 被压到 2 GiB，轮数进一步增多——这就是运维上"用轮数换显存"的旋钮。

**预期结果**：三组运行都满足不变量 `bucket.size == sum(ranges)`、每个桶的 items 都是完整张量；计划表里的"broadcast rounds"正是 `_update_per_bucket` 中 `max_len` 循环的迭代次数（[ps.py:855](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L855)）。预期数值由公式手算得出，脚本运行输出**待本地验证**。

## 6. 本讲小结

- **桶是流水线的调度单位**：`H2DBucket(size, ranges, items)` 描述"多少字节、从哪几段取、装了哪些张量"；`BucketRange(idx, offset, size)` 是桶内字节的取货清单，本地装填时当切片下标用，P2P 时当绝对地址用。
- **切桶三原则**：按 owner 分组绝不混桶；`bucket_size` 是软上限、切桶的原子是整个对齐槽位（绝不腰斩张量）；一个桶的 `ranges` 可跨多块锁页 buffer。`ranks` 为空时 owner 即 receiver（colocate 的倒置广播源）。
- **切桶有一个边界行为**：owner 的第一个张量就超过 `bucket_size` 时会留下一个开头的空桶（末尾断言只查最后一个桶）；生产路径的预算保证使其不可达，离线实验可见。
- **预算公式**：\(\text{bucket\_size} = \min(\max(B_{\max}, s_{\max}), \lfloor f F / (kA) \rfloor A)\)，\(k=3\)（启用 `h2d_buffer`）或 \(2\)（禁用）；\(k\) 的来源是设备上同时存在几份桶大小的临时缓冲。
- **回退与报错**：最大张量放不进 free/3 就禁用 `h2d_buffer` 省显存（代价是 H2D 串行化）；连 free/2 都放不下就 `assert` 失败。
- **一次 MIN 约减捎带两个统计量**：最小空闲显存定预算，负数技巧取到的最大 ZMQ 计数器保证各 rank 生成一致的抽象 socket 地址——`_detect_bucket_size` 必须先于 `_bind_zmq_socket` 调用。

## 7. 下一步学习建议

- **下一讲 u3-l6（ZMQ 协议）**：本讲反复出现的 `_zmq_addr_counter`、`_bind_zmq_socket` 和 `ipc://@` 抽象地址将在那里展开为完整的 REQ/REP 消息状态机，建议接着读。
- **u5-l6（P2P bucket 分配算法）**：`_gen_h2d_buckets` 在 `ranks` 非空时把最后一步交给 `_assign_receiver_ranks`（[ps.py:108-163](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L108-L163)），那是一个按 RDMA 网卡拓扑最大化带宽的贪心算法，配套测试 [tests/test_assign_receiver_ranks.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_assign_receiver_ranks.py) 可以纯 CPU 运行。
- **回看 u3-l4**：带着本讲的 `bucket_size`/`disable_h2d_buffer` 语义重读 `_update_per_bucket` 的四拍循环，检查你是否能解释每一处 `gidx % 2 * bucket_size` 和 `h2d_buffer` 的引用。
- 若想动手实验：把综合实践的 planner 接上真实显存探测（`torch.cuda.mem_get_info()`），对比不同 `PS_MAX_BUCKET_SIZE_GB` 下的计划表与实际更新日志中的 `auto detect bucket size ... GiB` 行。
