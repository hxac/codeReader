# Tile scheduler metadata 分配

## 1. 本讲目标

本讲是「Split-KV、Combine 与 Tile Scheduler」单元的第三篇，承接 [u4-l1](u4-l1-splitkv-buffers.md) 的 split-KV 缓冲思想与 [u4-l2](u4-l2-combine-kernel.md) 的 combine 归并，专讲 decode 三段式中的第一段——**调度元数据是如何被算出来的**。

学完后你应当掌握：

1. 理解 tile scheduler 的**负载均衡目标**：为什么要把 batch 里的请求/块尽量均匀地切给 `num_sm_parts` 个 SM partition。
2. 理解 `DecodingSchedMeta` 的每个字段：`begin_req_idx`/`end_req_idx`、`begin_block_idx`/`end_block_idx`、`begin_split_idx`、以及 `is_first_req_splitted`/`is_last_req_splitted` 两个 split 标志。
3. 理解 `num_splits` 前缀和数组的生成方式、它与 accumulate 缓冲的对应关系，以及空序列/边界序列的修正逻辑。
4. 能对照 `get_decoding_sched_meta.cu` 手算一个小例子，推出若干份 `DecodingSchedMeta` 与 `num_splits`。

---

## 2. 前置知识

在进入源码前，先用通俗语言回顾几个本讲要用到的基础概念。

### 2.1 split-KV 与「主 kernel → combine」之间的契约

decode 阶段 query 极少（\(s_q=1\)）而 KV 很长，单条序列无法喂饱整张 GPU 的 SM，于是把长 KV **横向切成若干 split**，交给多个 SM partition 并行处理（详见 [u4-l1](u4-l1-splitkv-buffers.md)）。每个 split 产出一组「段内已归一化的局部输出」和「局部 lse」，写入两块 float32 累加缓冲 `o_accum` / `lse_accum`，最后由 combine kernel 跨 split rescale 归并（详见 [u4-l2](u4-l2-combine-kernel.md)）。

这就引出一个**编排问题**：

- 每条 batch 请求的 KV 长度不同，到底切几份 split？哪几个 SM partition 负责哪条请求的哪一段 block？
- 主 kernel 怎么知道「我这个 partition 该把结果写到 accumulate 缓冲的第几个 split 槽位」？
- combine kernel 怎么知道「这条请求一共产生了几个 split，从哪个槽位开始读」？

这三个问题，全部由一个轻量级的「元数据 kernel」一次性算好，写成两张表：

- `tile_scheduler_metadata[num_sm_parts]`：给**主 kernel** 看，每张表描述一个 SM partition 要处理的请求范围、block 范围、split 起始槽位、首尾是否为不完整分块。
- `num_splits[batch_size+1]`：给 **combine kernel** 看的前缀和，描述每条请求占了 accumulate 缓冲里的哪一段 split 区间。

本讲就是讲这张「元数据 kernel」如何填充这两张表。

### 2.2 分页 KV cache 与 block 的概念

FlashMLA 的 KV cache 是**分页（paged）**的：所有 token 被切成固定大小的 page block（`page_block_size=64` 个 token 为一块），散落在 `blocked_k` 池子里，由 `block_table` 记录每条请求依次用到了哪些块（见 [u1-l4](u1-l4-python-api-quickstart.md)）。

因此一条请求「要 attend 的 token 范围」总是可以被换算成「block 范围」：从 token 索引 `t` 到 block 索引 `t / 64`。本讲里你会反复看到 `/ block_size_n`（`block_size_n` 即 64）这种换算。

### 2.3 单 warp 元数据 kernel

这个元数据 kernel 只用 **1 个 block、32 个线程（1 个 warp）** 启动（见后文 `run_get_decoding_sched_meta_kernel`）。原因很简单：它处理的不是庞大的注意力计算，而是 batch 级别的「切分计划」，数据量很小（batch 通常几十到上百），用一个 warp 串行扫一遍就够，不需要也不应该占用大量 SM。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu` | 元数据 kernel 主体：单 warp 扫 batch，产出 `DecodingSchedMeta` 与 `num_splits` |
| `csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.h` | 对外声明 `run_get_decoding_sched_meta_kernel` |
| `csrc/params.h` | 定义 `DecodingSchedMeta`、`GetDecodeSchedMetaParams`（输入契约） |
| `csrc/api/dense_decode.h` | dense 解码接口：首次调用时构造 `GetDecodeSchedMetaParams` 并启动本 kernel |
| `csrc/api/sparse_decode.h` | sparse 解码接口：同理，且 `fixed_overhead_num_blocks`/`num_sm_parts` 由各 Impl 的 `get_meta` 决定 |
| `csrc/smxx/decode/combine/combine.cu` | combine kernel：消费 `num_splits` 前缀和来定位每条请求的 split 区间 |

注意一个关键的**分工**：`DecodingSchedMeta` 表是给**主 kernel** 读的；`num_splits` 前缀和是给 **combine kernel** 读的。两者由同一个元数据 kernel 同时写出，保证一致。

---

## 4. 核心概念与源码讲解

### 4.1 负载均衡切分逻辑

#### 4.1.1 概念说明

decode 的 SM 资源被切成 `num_sm_parts` 份 partition，每份 partition 负责一批「(请求, block)」工作量。如果某些 partition 分到的 KV block 特别多、另一些特别少，就会出现「快的等慢的」的拖尾，整体吞吐被最慢的 partition 拖垮。

因此元数据 kernel 的核心目标是**负载均衡**：把所有请求的 block 总量尽量平均地摊到 `num_sm_parts` 份上。但这里有一个微妙的约束——**切分只能发生在 block 边界上**（因为 KV 是按 page block 加载的），而且**同一条请求被切到不同 partition 时，必须用独立的 split 槽位**（否则两个 partition 写同一块 accumulate 缓冲会冲突）。

为此 kernel 引入两个量：

- **payload**：每个 partition 预期承担的「工作量配额」。把总工作量除以 `num_sm_parts` 向上取整，再补一个固定开销。
- **fixed_overhead_num_blocks**：每条请求的固定开销（dense 与多数 sparse 实现为 5，head128 small_topk 变体为 3）。它代表每处理一条请求都要付出的「非 KV block」成本（如越界掩码、边界处理等），加进配额里能让切分更贴近真实耗时。

#### 4.1.2 核心流程

整个 kernel 分三步，伪代码如下：

```
# 第 0 步：每个 lane 各算若干条请求的 num_blocks，并累加 total_num_blocks
for i in lane覆盖的请求:
    cur_s_k = dense ? seqlens_k[i] : effective_topk(i)   # sparse 时含 topk/extra_topk
    num_blocks[i] = last_block(cur_s_k) - first_block(0) + 1
    total_num_blocks += num_blocks[i] + fixed_overhead

# 第 1 步：warp 内蝶形归约，让所有 lane 拿到一致的 total_num_blocks
total = warp_reduce_sum(total_num_blocks)
payload = ceil_div(total, num_sm_parts) + fixed_overhead

# 第 2 步：由 lane 0 串行地把 [0, total) 按 payload 配额切成 num_sm_parts 份
write DecodingSchedMeta[num_sm_parts] 与 num_splits[batch_size+1]
assert(所有请求都被分完，且没有半截请求悬空)
```

关键设计点：

1. **第一步用整个 warp 并行**（每 lane 处理 `batch_size/32` 条请求），**第二步只由 lane 0 串行**。因为第二步是有状态的「游标推进」（`now_req_idx`/`now_block`/`now_n_split_idx` 三个游标），天然串行，但数据量小、无所谓。
2. **payload 是「向上取整 + 余量」**：`ceil_div` 保证配额之和 ≥ 总量，于是至多最后一个 partition 会有空余，不会出现「分不完」。

#### 4.1.3 源码精读

第一步：并行计算每条请求的 block 数与总量。

[get_decoding_sched_meta.cu:28-55](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L28-L55) —— 这段循环对每条请求算出 `cur_s_k`（dense 取真实 `seqlens_k`，sparse 取 `topk` 并按需对齐到 block 边界再加 `extra_topk`），再换算成 `first/last_block_idx` 与 `num_blocks`，并累加进 `total_num_blocks`。注意第 51 行 `total_num_blocks += num_blocks + fixed_overhead_num_blocks`，把固定开销也计入总量。

warp 蝶形归约让 32 个 lane 拿到一致的 `total_num_blocks`：

[get_decoding_sched_meta.cu:56-59](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L56-L59) —— 标准的 `__shfl_xor_sync` 蝶形求和（offset 从 16 折半到 1），把每个 lane 的局部和归约成 warp 全局和。

计算 payload 并进入主分配循环：

[get_decoding_sched_meta.cu:61-62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L61-L62) —— `payload = ceil_div(total_num_blocks, num_sm_parts) + fixed_overhead_num_blocks`。`cutlass::ceil_div(a,b)` 即 \((a+b-1)/b\)（见 `host.h:72`）。

#### 4.1.4 代码实践

**实践目标**：在源码里定位 payload 的计算，理解 `fixed_overhead` 的作用。

**操作步骤**：

1. 打开 [get_decoding_sched_meta.cu:62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L62)，找到 `payload = cutlass::ceil_div(total_num_blocks, num_sm_parts) + fixed_overhead_num_blocks;`。
2. 回到调用方 [dense_decode.h:101-112](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L101-L112)，确认 dense 路径传入 `fixed_overhead_num_blocks = 5`、`block_size_n = 64`。
3. 再看 [sparse_decode.h:59-66](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L59-L66)（`Decode_Sm90_Impl::get_meta`）与 [sparse_decode.h:168-175](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L168-L175)（`Decode_Sm100_Head128_Impl::get_meta`），对比两者的 `fixed_overhead_num_blocks`（5 vs 3）。

**需要观察的现象**：dense 与多数 sparse 实现都用 `fixed_overhead=5`，唯独 head128 的 small_topk 变体用 `3`。

**预期结果**：能口述「payload = 总工作量平均到每份 partition 后，再补上每条请求的固定开销」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 payload 用 `ceil_div`（向上取整）而不是普通除法？

**答案**：向上取整保证 \(\text{payload} \times \text{num\_sm\_parts} \ge \text{total}\)，即配额总和不小于真实总量。若用普通除法向下取整，最后一个 partition 可能「装不下」剩余工作量，导致切分失败或分配不均。

**练习 2**：如果把 `fixed_overhead_num_blocks` 设成 0，对切分结果会有什么影响？

**答案**：每条请求少了 5 的固定开销计入，`payload` 变小、`total` 也变小。极端情况下短请求更容易被同一个 partition 连续吞下，但更关键的是 `else` 分支里 `remain_payload - fixed_overhead_num_blocks > 0` 这个判定会变成 `remain_payload > 0`，可能让一个 partition 在剩余配额极少时仍硬塞一个 block 给当前请求、产生不必要的 split。`fixed_overhead` 起到了「预留切换成本」的作用。

---

### 4.2 DecodingSchedMeta 字段

#### 4.2.1 概念说明

`DecodingSchedMeta` 是写给主 kernel 的「一张 partition 工单」。每个 SM partition 拿到自己的那张工单，就知道：

- 我要处理哪几条请求（`begin_req_idx` ~ `end_req_idx`）；
- 这些请求里，第一条和最后一条是不是「只处理了一部分 block」（split 标志）；
- 我产出的局部结果该写进 accumulate 缓冲的哪个 split 槽位起（`begin_split_idx`）。

#### 4.2.2 核心流程：七个字段一览

结构定义在 [params.h:10-16](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L10-L16)，逐字段含义如下表：

| 字段 | 含义 | 备注 |
|---|---|---|
| `begin_req_idx` | 本 partition 负责的**起始**请求下标 | 闭区间 |
| `end_req_idx` | 本 partition 负责的**结束**请求下标 | 闭区间 |
| `begin_block_idx` | 第一条请求的起始 block（全局 block 下标） | 闭 |
| `end_block_idx` | 最后一条请求的结束 block（全局 block 下标） | **开**（exclusive） |
| `begin_split_idx` | 本 partition 写 accumulate 缓冲的起始 split 槽位 | 相对当前请求的 split 基址 |
| `is_first_req_splitted` | 第一条请求是否为**不完整分块**（与其他 partition 共享同一条请求） | 0/1 |
| `is_last_req_splitted` | 最后一条请求是否为**不完整分块** | 0/1 |

两个要点：

1. **block 区间是「左闭右开」**（注释 `Inclusive, exclusive`），即 `[begin_block_idx, end_block_idx)`。所以一条完整请求的 block 数 = `end_block_idx - begin_block_idx`。
2. **`begin_split_idx` 的基准是「当前请求」**。accumulate 缓冲里一条请求的全局基址由 `num_splits[req]` 给出，本 partition 写的槽位 = `num_splits[req] + begin_split_idx`（详见 4.3）。

另外，结构用 `__align__(4*8)`（即 32 字节对齐），末尾补了 `_pad[1]` 凑成 8 个 int = 32 字节。这主要是为了对齐访问与和后续字段排布友好。

#### 4.2.3 源码精读：字段如何被赋值

字段全部在主分配循环里由 lane 0 写入。[get_decoding_sched_meta.cu:66-99](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L66-L99) 是整个循环，关键赋值点：

- `begin_req_idx = now_req_idx`、`begin_block_idx = now_block + first_block_idx_shared[now_req_idx]`、`begin_split_idx = now_n_split_idx`、`is_first_req_splitted = (now_block != 0)` —— 在循环开头记录进入本 partition 时的游标（[第 68-71 行](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L68-L71)）。
- `end_req_idx` 与 `end_block_idx` —— 在内层 while 结束后，依据「当前请求是否被切到一半（`now_block > 0`）」二分计算（[第 92-93 行](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L92-L93)）。
- `is_last_req_splitted` —— 判定 `end_block_idx` 是否还没到最后一个 block（[第 94 行](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L94)）。
- 当本 partition 只覆盖**一条**请求时（`begin_req_idx == end_req_idx`），首尾两个 split 标志被合并成它们的「或」（[第 95-97 行](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L95-L97)）。

其中内层 while 循环是切分的核心，逻辑可概括为：

```
while 还有剩余请求:
    本请求剩余 block = num_blocks[req] - now_block
    if 配额 >= 本请求剩余 block + fixed_overhead:
        # 整条请求吃下，推进到下一条请求
        记 num_splits[req+1]，归零 now_block / now_n_split_idx
    else:
        # 配额不够吃下整条请求 → 在本请求内部切一刀
        if 配额 - fixed_overhead > 0:
            now_block += (配额 - fixed_overhead)   # 在当前请求里前进若干 block
            now_n_split_idx += 1                    # 多产生一个 split
        break
```

也就是说，每条请求要么被某个 partition「整条吞下」，要么在某个 partition 内部「被切一刀」——切的位置由「剩余配额减去固定开销」决定，保证不会切出 0 block 的空 split。

#### 4.2.4 代码实践

**实践目标**：把字段语义和源码赋值点一一对应。

**操作步骤**：

1. 打开 [params.h:10-16](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/params.h#L10-L16)，在每行注释旁用自己的话补一句中文（例如 `end_block_idx` 旁注「开区间，等于最后一条请求 last_block+1 或被切到的 now_block」）。
2. 打开 [get_decoding_sched_meta.cu:68-71](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L68-L71)，确认 `is_first_req_splitted = (now_block != 0)`：只有当进入本 partition 时游标 `now_block` 不为 0（即上一 partition 把同一条请求切到了一半），首请求才算 split。

**预期结果**：能解释为何 `now_block != 0` 恰好等价于「本 partition 的第一条请求是上一 partition 的延续」。

#### 4.2.5 小练习与答案

**练习 1**：`begin_block_idx` 为什么写成 `now_block + first_block_idx_shared[now_req_idx]` 而不是直接 `now_block`？

**答案**：`now_block` 是「相对当前请求起点的 block 偏移」，而 `first_block_idx_shared[req]` 是该请求在全局 block 池里的起始 block。两者相加才是「全局 block 下标」。当前实现里 `first_token_idx` 恒为 0，所以 `first_block_idx_shared` 总是 0，二者等价；保留相加是为了支持未来「请求不从 token 0 开始」的一般情形。

**练习 2**：`begin_req_idx == end_req_idx` 时为什么要把首尾 split 标志合并成「或」？

**答案**：当一个 partition 只覆盖一条请求时，这条请求既充当「首请求」又充当「尾请求」。若它被切分了，那么从该 partition 的角度看，首尾都是不完整分块，主 kernel 需要知道这条请求整体是被 split 的；取「或」能确保只要首或尾任一为 split，两个标志都置 1，避免主 kernel 误判某端是完整请求。

---

### 4.3 num_splits 前缀和

#### 4.3.1 概念说明

`num_splits` 是一个长度为 `batch_size + 1` 的 int 数组，本质是**前缀和**（prefix sum）：`num_splits[i]` 表示「前 i 条请求累计产生了多少个 split」。于是第 i 条请求的 split 区间就是：

\[
[\,\text{num\_splits}[i],\ \text{num\_splits}[i+1]\,)
\]

它的消费者是 combine kernel：combine 对每条请求 `batch_idx`，读 `start_split_idx = num_splits[batch_idx]`、`end_split_idx = num_splits[batch_idx+1]`，从而知道该从 accumulate 缓冲的哪个槽位范围把局部结果捞出来归并。

#### 4.3.2 核心流程

前缀和的更新只发生在「某条请求被某个 partition **完整吞下**」的时刻——因为只有此时才能确定这条请求一共产生了多少个 split。伪代码：

```
cum_num_splits = 0
num_splits[0] = 0
每当一条请求被完整处理完:
    cum_num_splits += (now_n_split_idx + 1)   # +1 是最后那个收尾 chunk
    num_splits[req + 1] = cum_num_splits
```

注意 `+1`：一条请求如果之前被切了 `now_n_split_idx` 刀（产生 `now_n_split_idx` 个 partial chunk），加上最后收尾的那一个 chunk，总共 `now_n_split_idx + 1` 个 split。

最后 kernel 还会把 `num_splits_shared` 拷出全局内存供 combine 读取（[第 104-106 行](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L104-L106)）。

#### 4.3.3 源码精读

前缀和更新点：

[get_decoding_sched_meta.cu:77-78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L77-L78) —— `cum_num_splits += now_n_split_idx + 1; num_splits_shared[now_req_idx + 1] = cum_num_splits;`。这正是「请求被完整吞下时，把累计 split 数写到下一条请求的起点」。

combine kernel 如何消费这张前缀和表：

[combine.cu:36-41](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L36-L41) —— `start_split_idx = num_splits_ptr[batch_idx]`、`end_split_idx = num_splits_ptr[batch_idx+1]`、`my_num_splits = end_split_idx - start_split_idx`，并据此从 `lse_accum` / `o_accum` 的对应槽位 gather。当 `my_num_splits == 1` 时直接 return（早退，见 [u4-l2](u4-l2-combine-kernel.md)）。

> 关键一致性：主 kernel 写 accumulate 缓冲的槽位 = `num_splits[req] + begin_split_idx`；combine 读的区间 = `[num_splits[req], num_splits[req+1])`。两者通过同一张前缀和表对齐，保证「写」和「读」的槽位严丝合缝。

#### 4.3.4 代码实践

**实践目标**：验证「写端」与「读端」通过前缀和表对齐。

**操作步骤**：

1. 在 [get_decoding_sched_meta.cu:77](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L77) 旁注：「主 kernel 把第 req 条请求的 split 写到 `num_splits[req] + begin_split_idx`」。
2. 在 [combine.cu:36-38](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/combine/combine.cu#L36-L38) 旁注：「combine 从 `[num_splits[req], num_splits[req+1])` 读」。
3. 思考：若某条请求只产生 1 个 split，主 kernel 走的是 [u4-l1](u4-l1-splitkv-buffers.md) 讲过的 `is_no_split` 直写路径（直接写最终 out/lse），combine 则早退——两端都不碰 accumulate 缓冲。

**预期结果**：能口述「前缀和表是主 kernel 与 combine 之间唯一的 split 槽位契约」。

#### 4.3.5 小练习与答案

**练习 1**：`num_splits` 数组为什么长度是 `batch_size + 1` 而不是 `batch_size`？

**答案**：因为它是前缀和，第 0 项固定为 0（基址），第 i+1 项才是「前 i 条请求的累计 split 数」。这样第 i 条请求的区间可直接写成 `[num_splits[i], num_splits[i+1])`，无需特判 i=0。

**练习 2**：调用方在分配 accumulate 缓冲时用的上界是 `total_num_splits = batch_size + num_sm_parts`（见 [dense_decode.h:164](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/dense_decode.h#L164)）。为什么这个上界一定够用？

**答案**：每条请求至多被切成「(它被多少个 partition 触及)」个 split。最坏情况下，`num_sm_parts` 个 partition 每个都横跨一条新请求的边界，且每条请求至少占 1 个 split，故 split 总数上界 ≈ `batch_size + num_sm_parts`（每条请求的「基准 1 个 split」加上「因被切分而额外产生的 split」）。这个宽松上界保证 accumulate 缓冲永不越界。

---

### 4.4 空/边界序列处理

#### 4.4.1 概念说明

真实 batch 里常出现两类边界情形，元数据 kernel 必须妥善处理，否则会让主 kernel 崩溃或读到越界 block：

1. **空序列（`seqlens_k == 0`）**：某条请求没有 KV。但 block 换算会把它算成「1 个 block」（block 0），需要事后修正。
2. **sparse 下的空 topk（`cur_s_k == 0`）**：某条请求一个 token 都没选到。kernel 把它强制改成 1，避免主循环空转。

#### 4.4.2 核心流程

边界处理散落在两处：

```
# sparse 路径里避免空 topk
if cur_s_k == 0: cur_s_k = 1   # "Ensure the main loop will never be empty"

# block 换算后，空序列的修正
if seqlens_k[i] == 0:
    # first/last_token_idx 都=0 → num_blocks = 0-0+1 = 1（多算了一个 block）
    # 后续 end_block_idx / is_last_req_splitted 的判定里用 "seqlens_k_shared[x] == 0" 把它纠正回 0
```

关键在于：**空序列的 `num_blocks` 仍按 1 计入 `total_num_blocks`**（参与 payload 配额），但在写 `end_block_idx` 与 `is_last_req_splitted` 时，用 `seqlens_k_shared[x] == 0` 条件把 block 范围纠正为 0，保证主 kernel 不会真的去访问 block 0。

#### 4.4.3 源码精读

sparse 空 topk 修正：

[get_decoding_sched_meta.cu:36-37](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L36-L37) —— `if (cur_s_k == 0) cur_s_k = 1;`，注释明确写着 "Ensure the main loop will never be empty"。

空序列 block 换算的注释说明：

[get_decoding_sched_meta.cu:48-50](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L48-L50) —— 注释解释：若 `seqlens_k == 0`，则 `first_token_idx == last_token_idx == 0`，于是 `num_blocks = 1`（多算了），将在本 kernel 后面修正。

`end_block_idx` 与 `is_last_req_splitted` 里对空序列的纠正：

[get_decoding_sched_meta.cu:93-94](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L93-L94) —— 第 93 行三元式里有 `seqlens_k_shared[now_req_idx-1] == 0 ? 0 : last_block_idx_shared[now_req_idx-1] + 1`，把空序列的 `end_block_idx` 纠正为 0；第 94 行 `&& seqlens_k_shared[cur_meta.end_req_idx] != 0` 确保空序列不会被误判为 split。

此外，循环末尾的断言是一道全局安全网：

[get_decoding_sched_meta.cu:100](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L100) —— `FLASH_DEVICE_ASSERT(now_req_idx == batch_size && now_block == 0 && now_n_split_idx == 0);`，断言「所有请求都分完了、且没有请求停在半截 block 上」。`FLASH_DEVICE_ASSERT` 是 device 端断言（见 `utils.h:26`），失败时 `printf` 后执行 `asm("trap;")` 让 GPU 停下。

#### 4.4.4 代码实践

**实践目标**：理解空序列如何被「先算成 1 个 block、再纠正回 0」。

**操作步骤**：

1. 打开 [get_decoding_sched_meta.cu:44-54](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L44-L54)。假设 `seqlens_k[i] == 0`，手动推出 `first_token_idx=0`、`last_token_idx=max(0-1,0)=0`、`cur_first_block_idx=0`、`cur_last_block_idx=0`、`num_blocks=1`。
2. 再看 [第 93 行](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/smxx/decode/get_decoding_sched_meta/get_decoding_sched_meta.cu#L93)：当 `now_block == 0` 且 `seqlens_k_shared[now_req_idx-1] == 0` 时，`end_block_idx` 取 0，于是 block 区间 `[0, 0)` 为空，主 kernel 对该请求不访问任何 block。

**预期结果**：能解释「为什么 `num_blocks` 算成 1 但实际 block 区间是空」不矛盾——前者只用于算 payload 配额，后者才决定主 kernel 真正读哪些 block。

#### 4.4.5 小练习与答案

**练习 1**：为什么空序列的 `num_blocks` 仍按 1 计入 `total_num_blocks`，而不是按 0？

**答案**：因为即便空序列没有真实 block，主 kernel 处理它仍要付出 `fixed_overhead` 那份固定开销（边界判定、写空输出等）。把 `num_blocks` 记为 1 后，`num_blocks + fixed_overhead` 这一项更能反映真实耗时，使负载均衡更准。而真正决定「读哪些 block」的 `end_block_idx` 已被纠正为 0，不会引发越界访问。

**练习 2**：末尾的 `FLASH_DEVICE_ASSERT(now_req_idx == batch_size && ...)` 如果失败，通常意味着什么？

**答案**：意味着 payload 配额没能把全部请求分完，或某条请求被切到一半就耗尽了 `num_sm_parts` 个 partition。正常情况下 `ceil_div` 保证了配额总和 ≥ 总量，不该出现分不完；若触发，说明 `num_sm_parts`、`fixed_overhead` 或 batch 数据存在异常组合，需要排查。断言把这种「静默错误」变成显式的 GPU trap。

---

## 5. 综合实践

本节用一个完整的**手算例子**把四个最小模块串起来。这是本讲的主实践任务。

### 5.1 例子 A（任务指定）：b=3，块数 [4,1,5]，num_sm_parts=2

**设定**：dense 解码，三条请求各自占用的 page block 数为 `[4, 1, 5]`（即 `num_blocks_shared = [4,1,5]`，对应 `first_block_idx_shared=[0,0,0]`、`last_block_idx_shared=[3,0,4]`、`seqlens_k_shared` 全非零）。`block_size_n=64`、`fixed_overhead_num_blocks=5`、`num_sm_parts=2`。

> 说明：这些 block 数可由 `seqlens_k = [256, 64, 320]` 推出（`last_token_idx / 64` 分别得 block 3/0/4，`num_blocks` 即 4/1/5）。本例直接采用 block 数以聚焦切分逻辑。

**第 1 步：算 total_num_blocks**

\[
\text{total} = (4+5) + (1+5) + (5+5) = 9 + 6 + 10 = 25
\]

经 warp 蝶形归约后，所有 lane 都拿到 25。

**第 2 步：算 payload**

\[
\text{payload} = \lceil 25 / 2 \rceil + 5 = 13 + 5 = 18
\]

**第 3 步：主分配循环（lane 0 串行）**

初始游标 `now_req_idx=0, now_block=0, now_n_split_idx=0, cum_num_splits=0`，`num_splits[0]=0`。

**i=0（partition 0）**：进入时 `begin_req_idx=0, begin_block_idx=0, begin_split_idx=0, is_first_req_splitted=(0!=0)=0`，`remain_payload=18`。

- `now_req_idx=0`：`now_remain_blocks=4-0=4`，`remain(18) >= 4+5=9`？是 → `cum_num_splits += 0+1=1`，`num_splits[1]=1`，`remain=18-9=9`，推进 `now_req_idx=1, now_block=0, now_n_split_idx=0`。
- `now_req_idx=1`：`now_remain_blocks=1-0=1`，`remain(9) >= 1+5=6`？是 → `cum_num_splits += 0+1=1`（得 2），`num_splits[2]=2`，`remain=9-6=3`，推进 `now_req_idx=2, now_block=0, now_n_split_idx=0`。
- `now_req_idx=2`：`now_remain_blocks=5-0=5`，`remain(3) >= 5+5=10`？否 → `remain-5 = 3-5 = -2 > 0`？否 → `break`。
- 收尾：`now_block=0`，故 `end_req_idx = now_req_idx-1 = 1`；`end_block_idx = last_block_idx_shared[1]+1 = 0+1 = 1`；`is_last_req_splitted = (1 != last_block[1]+1=1) && ... = 0`；`begin_req(0) != end_req(1)`，不合并。

→ **meta[0]** = `{begin_req=0, end_req=1, begin_block=0, end_block=1, begin_split_idx=0, is_first=0, is_last=0}`。

**i=1（partition 1）**：进入时 `now_req_idx=2, now_block=0`，`begin_req_idx=2, begin_block_idx=0, begin_split_idx=0, is_first_req_splitted=0`，`remain_payload=18`。

- `now_req_idx=2`：`now_remain_blocks=5-0=5`，`remain(18) >= 5+5=10`？是 → `cum_num_splits += 0+1=1`（得 3），`num_splits[3]=3`，推进 `now_req_idx=3, now_block=0`。
- `now_req_idx=3 >= batch_size(3)`，退出 while。
- 收尾：`now_block=0`，故 `end_req_idx=3-1=2`；`end_block_idx=last_block_idx_shared[2]+1=4+1=5`；`is_last_req_splitted=(5 != 5) && ...=0`；`begin_req(2)==end_req(2)`，合并 → `is_first=is_last=0||0=0`。

→ **meta[1]** = `{begin_req=2, end_req=2, begin_block=0, end_block=5, begin_split_idx=0, is_first=0, is_last=0}`。

**结果汇总**：

| partition | begin_req | end_req | begin_block | end_block | begin_split_idx | is_first | is_last | 负责的 block |
|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 1 | 0 | 1 | 0 | 0 | 0 | req0 的 block[0,4) + req1 的 block[0,1) |
| 1 | 2 | 2 | 0 | 5 | 0 | 0 | 0 | req2 的 block[0,5) |

```
num_splits = [0, 1, 2, 3]
```

校验每条请求的 split 数：req0 = `1-0=1`、req1 = `2-1=1`、req2 = `3-2=1`，**全部为 1**。这意味着三条请求都未被切分，主 kernel 对它们走 `is_no_split` 直写路径，combine 全部早退。本例展示的是**纯请求级负载均衡**（把 req0/1 给 partition 0、req2 给 partition 1），没有发生 split-KV。

末尾断言：`now_req_idx(3)==batch_size(3) && now_block==0 && now_n_split_idx==0` ✓ 通过。

### 5.2 例子 B（补充，演示真实 split）：b=1，块数 [100]，num_sm_parts=2

为演示 `begin_split_idx` 与 split 标志，再算一个长序列被切的例子。`fixed_overhead=5`。

- `total = (100+5) = 105`
- `payload = ceil(105/2) + 5 = 53 + 5 = 58`

**i=0**：`begin_req=0, begin_block=0, begin_split_idx=0, is_first=0`，`remain=58`。
- `now_req_idx=0`：`now_remain=100-0=100`，`remain(58) >= 100+5=105`？否 → `remain-5=53 > 0`？是 → `now_block += 53`（=53），`now_n_split_idx=1`，`remain=0`，`break`。
- 收尾：`now_block=53>0` → `end_req_idx=now_req_idx=0`，`end_block_idx=now_block+first_block[0]=53`；`is_last_req_splitted=(53 != last_block[0]+1=100) && (seqlens!=0)=1`；`begin_req(0)==end_req(0)` → 合并 `is_first=is_last=0||1=1`。

→ **meta[0]** = `{begin_req=0, end_req=0, begin_block=0, end_block=53, begin_split_idx=0, is_first=1, is_last=1}`。

**i=1**：`now_req_idx=0, now_block=53`，`begin_req=0, begin_block=0+53=53, begin_split_idx=now_n_split_idx=1, is_first=(53!=0)=1`，`remain=58`。
- `now_req_idx=0`：`now_remain=100-53=47`，`remain(58) >= 47+5=52`？是 → `cum_num_splits += now_n_split_idx+1 = 1+1 = 2`（`cum=2`），`num_splits[1]=2`，`remain=58-52=6`，推进 `now_req_idx=1, now_block=0, now_n_split_idx=0`。
- `now_req_idx=1 >= 1`，退出。
- 收尾：`now_block=0` → `end_req_idx=1-1=0`；`end_block_idx=last_block[0]+1=100`；`is_last_req_splitted=(100 != 100)&&...=0`；`begin_req(0)==end_req(0)` → 合并 `is_first=is_last=1||0=1`。

→ **meta[1]** = `{begin_req=0, end_req=0, begin_block=53, end_block=100, begin_split_idx=1, is_first=1, is_last=1}`。

```
num_splits = [0, 2]
```

**解读**：req0 被切成 2 个 split（`num_splits[1]-num_splits[0]=2`）。partition 0 写槽位 `num_splits[0]+begin_split_idx = 0+0 = 0`，partition 1 写槽位 `0+1 = 1`；combine 从 `[0, 2)` 把两个 split 归并。两个 partition 的 `begin_block` 分别是 0 和 53，block 区间 `[0,53)` 与 `[53,100)` 恰好首尾相接覆盖整条请求。这就是 split-KV 在元数据层的完整体现。

> 若无法在 GPU 上运行，本实践为纯手算型，已给出完整推导与预期结果，无需「待本地验证」。

---

## 6. 本讲小结

- 元数据 kernel `get_mla_metadata_kernel` 是一个**单 warp** kernel，串行扫一遍 batch，产出两张表：给主 kernel 的 `DecodingSchedMeta[num_sm_parts]` 与给 combine 的 `num_splits[batch_size+1]`。
- **负载均衡**靠 `payload = ceil_div(total, num_sm_parts) + fixed_overhead` 这个配额：把总工作量（含每条请求的固定开销）平均到每个 partition，配额不够整条吞下时就在 block 边界切一刀。
- `DecodingSchedMeta` 用 `begin/end_req_idx`（请求范围，闭）、`begin/end_block_idx`（block 范围，左闭右开）、`begin_split_idx`（accumulate 槽位起点）与首尾 split 标志，完整描述一个 partition 的工单；单请求 part 会合并首尾标志。
- `num_splits` 是**前缀和**，第 i 条请求的 split 区间为 `[num_splits[i], num_splits[i+1])`；它在「请求被完整吞下」时累加 `now_n_split_idx+1`。主 kernel 的写槽位与 combine 的读区间通过这张表对齐。
- **空/边界序列**先被算成 1 个 block 计入 payload，再在 `end_block_idx`/`is_last_req_splitted` 处用 `seqlens_k==0` 纠正回 0；sparse 空 topk 被强制改成 1 避免空循环；末尾 `FLASH_DEVICE_ASSERT` 兜底保证「分完且无半截请求」。
- dense 与 sparse 两条解码路径**共用**这个 kernel，差异仅体现在 `cur_s_k` 的来源（真实 `seqlens_k` vs `topk`/`extra_topk`）与 `fixed_overhead`/`num_sm_parts` 的取值上。

---

## 7. 下一步学习建议

- **横向**：回到 [u4-l2](u4-l2-combine-kernel.md) 结合本讲的 `num_splits` 前缀和，重读 combine 的 rescale 归并循环，验证「写端槽位 ↔ 读端区间」的一致性。
- **纵向（进入 sparse）**：本讲已铺好 sparse 解码的调度基础，下一单元 [u5-l1](u5-l1-fp8-kvcache-format.md) 起进入 FP8 sparse decoding kernel，届时你会看到主 kernel 如何消费 `DecodingSchedMeta` 的 split 标志去决定写 accumulate 缓冲还是直写输出。
- **源码延伸**：阅读 `csrc/sm90/decode/dense/splitkv_mla.cuh` 与 `csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh` 里读取 `tile_scheduler_metadata_ptr` 的代码，看主 kernel 如何把本讲的「工单」翻译成实际的 block 遍历与 accumulate 写入。
