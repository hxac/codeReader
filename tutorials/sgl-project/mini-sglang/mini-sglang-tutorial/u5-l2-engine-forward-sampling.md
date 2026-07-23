# Engine forward 与采样

## 1. 本讲目标

上一篇（u5-l1）我们把 `Engine.__init__` 这条「显存装配流水线」走完了：模型建好、KV cache 池分好、page_table 摆好、CUDA Graph 捕获完。本讲紧接着回答下一个问题：

> 当 Scheduler 把一个装配好的 `Batch` 交给 Engine 后，Engine 是如何把它变成「下一个 token」并返回的？

读完本讲，你应当能够：

1. 说清 [`forward_batch`](#) 的四个职责：选前向路径（CUDA Graph replay 还是普通 `model.forward()`）、推进 `Req` 状态、采样、异步拷回 CPU。
2. 解释 `Sampler.prepare` 如何把「逐请求」的 `SamplingParams` 打包成「整批」的 `BatchSamplingArgs`，并理解全贪婪时 `temperatures=None` 走 `torch.argmax` 快路径的原因。
3. 读懂 `sample_impl` 对 top-k / top-p / 纯温度采样四种组合的分派逻辑。
4. 理解 `ForwardOutput` 三件套（`next_tokens_gpu` / `next_tokens_cpu` / `copy_done_event`）在 Overlap Scheduling 下的分工：GPU 侧 token 立刻喂给下一步，CPU 侧 token 延迟交给 detokenizer，event 用来给延迟消费「把关」。

---

## 2. 前置知识

本讲默认你已经建立以下认知（来自前置讲义）：

- **`Req` 的长度计数器模型**（u2-l1）：核心字段 `cached_len`（已缓存）、`device_len`（逻辑游标）、`max_device_len`（上限），恒满足 \( 0 \le \text{cached\_len} < \text{device\_len} \le \text{max\_device\_len} \)。`complete_one()` 的语义是「游标先走一步」：`cached_len = device_len; device_len += 1`。
- **`Batch` 与 `Context.forward_batch`**（u2-l1）：`Batch` 是多个 `Req` 的打包，带 `phase`（`"prefill"` / `"decode"`）；`Context.forward_batch(batch)` 是一个上下文管理器，进入时把 batch 挂到全局 Context 的 `_batch`，退出时清空，让模型层/注意力后端能在前向中读到「当前 batch」。
- **Overlap Scheduling 的双 stream 模型**（u4-l1）：Engine 在自己的 `self.stream`（engine stream）上跑前向，Scheduler 在另一条 stream 上做调度；上一批结果的处理被推迟到「下一轮」的 `_process_last_data`。
- **CUDA Graph 的适用条件**（u5-l1 / 下一篇 u5-l3 预告）：只有 decode 且 `batch.size` 不超过 `max_graph_bs` 时才走 graph replay，否则走普通前向。
- **采样的数学直觉**：贪婪采样 = 永远选 logits 最大的词 = `argmax`；温度采样用 softmax 概率随机抽；top-k 只在概率最大的 k 个里抽；top-p（nucleus）只在累计概率达到 p 的最小集合里抽。

如果你对 LLM 推理里「prefill 算一段 prompt 出首 token、decode 每步出一个 token」这个两阶段节奏还不熟悉，建议先回顾 u2-l1。

---

## 3. 本讲源码地图

本讲围绕两个核心文件展开，并引用两个支撑文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `python/minisgl/engine/engine.py` | Engine 主体 | `forward_batch` 方法、`ForwardOutput` 定义 |
| `python/minisgl/engine/sample.py` | 采样器 | `Sampler.prepare`、`Sampler.sample`、`sample_impl`、`BatchSamplingArgs` |
| `python/minisgl/core.py` | 公共数据结构 | `SamplingParams.is_greedy`、`Req.complete_one`、`Context.forward_batch` |
| `python/minisgl/engine/graph.py` | CUDA Graph | `can_use_cuda_graph`、`replay`（graph 路径的另一半） |

调用关系一句话概括：

```
Scheduler._forward
  └─ Engine.forward_batch(batch, sample_args)        ← 本讲主角
       ├─ ctx.forward_batch(batch)  + graph.replay / model.forward   （算 logits）
       ├─ req.complete_one()         （推进游标，CPU 记账）
       ├─ Sampler.sample(logits, args)                （出 token）
       └─ next_tokens_gpu → token_pool；next_tokens_cpu + event → 返回
```

其中 `sample_args` 并不是 Scheduler 自己拼的，而是 Scheduler 调用 `Engine.sampler.prepare(batch)` 得到的——这条线索在 4.3 节展开。

---

## 4. 核心概念与源码讲解

### 4.1 forward_batch：前向的编排者

#### 4.1.1 概念说明

[`Engine.forward_batch`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L191-L206) 是 Engine 暴露给 Scheduler 的**唯一前向入口**。它接收两样东西：

- 一个已经装配好的 `Batch`（`input_ids` / `positions` / `out_loc` / `attn_metadata` 都已由 Scheduler 和注意力后端填好）；
- 一组整批采样参数 `BatchSamplingArgs`（由 `Sampler.prepare` 预先生成）。

它返回一个 `ForwardOutput`（下一个 token 的 GPU/CPU 双副本 + 一个 event）。整个方法只做四件事：**选前向路径 → 推进请求状态 → 采样 → 异步拷回**。它本身不碰 ZMQ、不碰 NCCL（NCCL 的 all_reduce 藏在 `model.forward()` 内部的 TP Linear 里），是纯粹的「单卡、单步」计算。

#### 4.1.2 核心流程

用伪代码描述 `forward_batch` 的执行过程：

```
forward_batch(batch, args):
    1. 断言 current_stream == engine stream     # overlap scheduling 的前提
    2. 进入 ctx.forward_batch(batch):           # 把 batch 挂到全局 Context
         if can_use_cuda_graph(batch):          # decode 且 size <= max_graph_bs
             logits = graph_runner.replay(batch) # 只拷输入缓冲，回放图
         else:
             logits = model.forward()            # 正常前向（prefill 或大 decode）
       退出 ctx.forward_batch()                  # 清空 _batch
    3. 对每个 req 调 complete_one()              # 推进长度游标（纯 CPU 记账）
    4. next_tokens_gpu = sampler.sample(logits[:size], args).to(int32)
    5. next_tokens_cpu = next_tokens_gpu.to("cpu", non_blocking=True)  # 异步 D2H
    6. copy_done_event.record(stream)            # 在 engine stream 上记录事件
    7. return ForwardOutput(gpu, cpu, event)
```

注意第 3 步 `complete_one()` 只推进「游标」，**并不**把新 token 写进 `input_ids`——真正把 token 值追加到 `Req.input_ids` 的 `append_host` 发生在下一轮 Scheduler 的 `_process_last_data` 里（用 `next_tokens_cpu`）。这正是 u2-l1 讲过的「游标先走一步、数据随后补齐」的节奏，也是 Overlap Scheduling 能成立的微观基础。

#### 4.1.3 源码精读

方法本体很短，逐行看：

[python/minisgl/engine/engine.py:191-206 — forward_batch 全貌](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L191-L206) ：编排「算 logits → 推进游标 → 采样 → 异步拷回」四步。

关键点拆解：

- **第 192 行 `assert torch.cuda.current_stream() == self.stream`**：强制本方法必须在 engine stream 上执行。这是 Overlap Scheduling 的硬前提——Scheduler stream 与 engine stream 必须严格分离，否则两条 stream 的交错假设全部失效。这个断言把「调用者必须切到 engine stream」这一隐式契约变成了显式检查。
- **第 193 行 `with self.ctx.forward_batch(batch):`**：进入上下文，把 `batch` 安装到全局 Context 的 `_batch` 字段。模型前向时，注意力后端、MoE 后端等组件会通过 `get_global_ctx().batch` 读到「当前正在算的这一批」。退出 `with` 块时 `_batch` 被清空（见 [core.py 的 forward_batch 上下文管理器](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L115-L122)），它还附带一个「禁止嵌套」的断言，防止两个 batch 互相覆盖。
- **第 194-197 行 二选一算 logits**：判定函数 [graph.py 的 can_use_cuda_graph](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L149-L150) 的逻辑是 `batch.is_decode and batch.size <= self.max_graph_bs`。命中则走 [graph_runner.replay(batch)](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L152-L158)（只把真实输入拷进捕获时的缓冲区，然后 `g.replay()`，返回 `buffer.logits[: batch.size]`）；否则走 `self.model.forward()`。也就是说：**prefill 永远走普通前向，decode 在 batch 不大时走 graph 回放**。graph 的捕获与回放细节是下一篇 u5-l3 的主题，本讲只需把它当作「一条更快的等价路径」。
- **第 199-200 行 `for req in batch.reqs: req.complete_one()`**：在退出上下文之后、采样之前，推进每个请求的游标。`complete_one` 的实现见 [core.py:52-54](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L52-L54)：`self.cached_len = self.device_len; self.device_len += 1`。这是纯 Python 赋值，不触发任何 GPU 操作，所以放在采样前/后都「免费」，放这里是为了让「本步已经算完」的状态尽快可见。
- **第 202 行采样**：`self.sampler.sample(logits[: batch.size], args).to(torch.int32)`。切片 `[: batch.size]` 是为了**丢弃 padding 行**——graph 路径会把真实请求 pad 到捕获尺寸（用 `dummy_req` 补齐，见 [graph.py 的 pad_batch](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L160-L166)），replay 返回的 logits 虽然已经切到 `batch.size`，这里再切一次既是防御也明示意图；`.to(torch.int32)` 是因为下游 `token_pool` 用 int32 存 token id。
- **第 203-205 行 异步拷回 + 记录事件**：这一段是 4.2 节的主角，这里先记住结论——GPU token 留在卡上立刻可用，CPU token 通过一次异步 D2H 拷贝获得，event 用来在「真正需要 CPU token 时」确认拷贝已完成。

#### 4.1.4 代码实践

**实践目标**：亲手验证「prefill 走 `model.forward()`、小 decode 走 graph replay」这条分支。

**操作步骤**（源码阅读 + 局部加日志，**待本地验证**运行结果）：

1. 在 `forward_batch` 的第 194 行前临时加一行日志：
   ```python
   # 示例代码（仅用于观察，验证后请删除）
   logger.info_rank0(
       f"forward_batch: phase={batch.phase} size={batch.size} "
       f"use_graph={self.graph_runner.can_use_cuda_graph(batch)}"
   )
   ```
2. 启动服务（参考 u1-l2），发一条较长 prompt（例如 2000 token）请求一次性生成几十个 token。
3. 观察日志：首步（prefill）应打印 `phase=prefill use_graph=False`；之后每步 decode 应打印 `phase=decode use_graph=True`。

**需要观察的现象**：

- prefill 步只有一次，且 `use_graph=False`；
- 后续 decode 步 `use_graph=True`（前提是 `batch.size <= max_graph_bs`）；
- 如果并发请求数超过 `max_graph_bs`，会看到 decode 步也退回 `use_graph=False`。

**预期结果**：分支选择完全由 `batch.is_decode and batch.size <= max_graph_bs` 决定，与请求内容无关。若你的环境无 GPU，则跳到「源码阅读型」：直接对照 [graph.py:149-150](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/graph.py#L149-L150) 与 [engine.py:194-197](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L194-L197) 复述判定条件即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `complete_one()` 放在 `with self.ctx.forward_batch(batch)` 退出**之后**，而不是退出之前？

> **答案**：`complete_one` 只改 `Req` 的长度游标，不依赖、也不需要全局 Context 里的 `_batch`；而 `model.forward()` 在 `with` 块**内部**执行时会读取 `ctx.batch`。把 `complete_one` 放在退出之后，可以保证前向期间 `ctx.batch` 始终指向「正在算的这一批」，不会被提前清空，逻辑更清晰、更安全。

**练习 2**：如果调用者忘了切到 engine stream 就直接调 `forward_batch`，会发生什么？

> **答案**：第 192 行的 `assert torch.cuda.current_stream() == self.stream` 会直接抛 `AssertionError`。这是好事——它把「必须在 engine stream 上调用」这一隐式契约显式化，避免在默认 stream 上执行导致 Overlap Scheduling 的双 stream 假设被悄悄破坏。

---

### 4.2 ForwardOutput：一份 token，两份副本，一个事件

#### 4.2.1 概念说明

[`ForwardOutput`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L23-L26) 是一个 `NamedTuple`，只有三个字段：

```python
class ForwardOutput(NamedTuple):
    next_tokens_gpu: torch.Tensor      # 留在 GPU 上的 token
    next_tokens_cpu: torch.Tensor      # 异步拷回 CPU 的 token
    copy_done_event: torch.cuda.Event  # 标记 D2H 拷贝完成的事件
```

为什么不直接返回一份 token？因为同一步产生的 token，有**两个消费者、两个时限**：

1. **下一步的模型前向**（GPU 侧）：需要立刻拿到这个 token 作为下一步 decode 的输入。它必须在 GPU 上、必须马上可用。时限：**立刻**。
2. **detokenizer / 用户回复**（CPU 侧）：需要把这个 token 转成文字送回前端。它可以晚一点拿到，且要跨进程（走 ZMQ），必须在 CPU 上。时限：**可以延迟到下一轮**。

于是 Engine 做了「一份 token，两份副本」：GPU 副本喂第一个消费者（同一条 stream，天然有序，无需同步）；CPU 副本通过异步 D2H 拷贝得到，喂第二个消费者；`copy_done_event` 专门用来给那个「延迟消费者」把关——等它真正要读 CPU token 时，再 `synchronize()` 这个 event。

#### 4.2.2 核心流程

把 `forward_batch` 的返回值放进 Overlap Scheduling 的整体时序里看：

```
engine stream (本轮):   算 logits ── 采样 ──┬─ token_pool[output_mapping] = next_tokens_gpu  (立刻，同 stream)
                                            └─ D2H async copy → next_tokens_cpu
                                               record(copy_done_event)        ← 本轮结束
scheduler stream (下一轮):   _process_last_data:
                                 copy_done_event.synchronize()   ← 此刻才等 D2H 完成
                                 用 next_tokens_cpu 做 append_host / DetokenizeMsg
```

要点：

- **GPU 副本不需要 event**：写 `token_pool` 和下一步读 `token_pool` 都在 engine stream 上，CUDA stream 的顺序保证天然成立。
- **CPU 副本需要 event**：D2H 拷贝是异步排入 engine stream 的，而消费者在 scheduler stream（另一条 stream）上、且晚一轮才读。跨 stream、跨轮次的「数据就绪」只能靠 event 来同步。
- **延迟同步 = 隐藏延迟**：拷贝与本轮其它工作重叠；等到下一轮 `_process_last_data` 才 `synchronize()`，此时拷贝大概率早已完成，`synchronize()` 几乎不阻塞。

#### 4.2.3 源码精读

返回结构定义与生成代码：

[python/minisgl/engine/engine.py:23-26 — ForwardOutput 三字段](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L23-L26) ：声明 GPU token / CPU token / 完成事件三件套。

[python/minisgl/engine/engine.py:202-206 — 采样 + 异步拷回 + 记录事件](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L202-L206) ：`to("cpu", non_blocking=True)` 请求一次异步 D2H 拷贝；`torch.cuda.Event()` + `record(self.stream)` 在 engine stream 上打一个时间戳，标记「到此刻为止提交的工作（含那次 D2H）都已入队」。

两个消费者分别在 Scheduler 里：

[python/minisgl/scheduler/scheduler.py:231 — GPU 副本立刻写回 token_pool](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L231) ：`self.token_pool[output_mapping] = forward_output.next_tokens_gpu`。注意这一行紧跟在 `forward_batch` 之后、**没有**任何 `synchronize()`，因为它和前向在同一条 engine stream 上，顺序天然保证。下一步 `batch.input_ids = self.token_pool[input_mapping]`（[scheduler.py:229](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L229)）就会把这个刚写入的 token 读出来当输入——一个 token 的「GPU 内闭环」，全程不过 CPU。

[python/minisgl/scheduler/scheduler.py:142-151 — CPU 副本延迟消费](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L142-L151) ：在下一轮的 `_process_last_data` 里，先 `copy_done.synchronize()` 确认 D2H 已完成，再用 `next_tokens_cpu[i]` 调 `req.append_host(...)` 把 token 追加到 CPU 侧的 `input_ids`，并构造 `DetokenizeMsg` 送往 detokenizer。

> 这两段 Scheduler 代码不属于本讲的源码范围，但它们是理解 `ForwardOutput` 设计意图的关键证据——读到这里你就明白「为什么要三件套」了。

#### 4.2.4 代码实践

**实践目标**：用「破坏性思维」理解 event 的必要性。

**操作步骤**（源码阅读型 + 思想实验）：

1. 假设把 [engine.py:204-205](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L204-L205) 的 `copy_done_event` 录制删掉，并把 [scheduler.py:143](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L143) 的 `copy_done.synchronize()` 也删掉。
2. 推理：下一轮 `_process_last_data` 读 `next_tokens_cpu` 时，那次异步 D2H 拷贝可能还没完成（尤其在 batch 大、token 多时）。
3. 结论：你会读到**未初始化或上一步残留**的脏数据，表现为输出乱码或错位。这就是 event 的价值——它把「拷贝完成」这一物理事实显式化。

**预期结果**（思想实验，**待本地验证**）：删除 event 后，在高负载、大 batch 下更容易复现乱码；保留 event 则任何情况下都正确。请不要真的修改源码运行——这是「读懂为什么需要它」的练习。

#### 4.2.5 小练习与答案

**练习 1**：`next_tokens_gpu` 为什么不需要配一个 event？

> **答案**：它的生产者（采样）和消费者（写 `token_pool`、下一步前向读 `token_pool`）都在**同一条 engine stream** 上。CUDA 规定同一条 stream 内的操作按提交顺序执行，因此顺序天然成立，不需要跨 stream 的同步原语。只有跨 stream（engine stream 的 D2H → scheduler stream 的读取）才需要 event。

**练习 2**：如果把 `next_tokens_gpu.to("cpu", non_blocking=True)` 改成阻塞拷贝（去掉 `non_blocking`），Overlap Scheduling 会受什么影响？

> **答案**：阻塞拷贝会强制当前 stream 等待 D2H 完成，相当于在 `forward_batch` 末尾插了一次同步。这会把「拷贝」从「能与下一轮调度重叠」变成「串行占用 engine stream 时间」，部分抵消 Overlap Scheduling 隐藏 CPU/拷贝延迟的收益（回顾 u4-l1：每轮耗时从约 `max(T_cpu, T_gpu)` 退化向 `T_cpu + T_gpu`）。

---

### 4.3 Sampler.prepare：把逐请求参数压平成整批张量

#### 4.3.1 概念说明

一个 batch 里可能有几十上百个请求，每个请求自带一份 `SamplingParams`（temperature / top_k / top_p）。但 GPU 采样 kernel 不会「逐请求」调用几十上百次——它需要**整批**的、对齐成张量的参数。

[`Sampler.prepare`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L53-L68) 就是这个「逐请求 → 整批」的压平器：它读 `batch.reqs` 里每个 `Req.sampling_params`，输出一个 [`BatchSamplingArgs`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L13-L17)（`temperatures` / `top_k` / `top_p` 三个可选张量）。

它有两个值得一提的设计：

1. **贪婪快路径**：如果整批全是贪婪采样，直接返回 `temperatures=None`，后续 `sample` 走 `torch.argmax`，完全绕开 softmax 和 flashinfer 采样 kernel。
2. **按需物化**：只有当 batch 里**存在**某个请求真的用了 top-k（或 top-p）时，才把 `top_k`（或 `top_p`）物化成张量上传；否则保持 `None`，让下游走更省的分派分支。

注意：`prepare` 由 Scheduler 在 `_prepare_batch` 阶段调用（见 [scheduler.py:214](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L214)），产出的 `BatchSamplingArgs` 跟着 `ForwardInput` 一起送进 `forward_batch`。所以采样参数的上传发生在调度阶段，不占用 engine stream 的时间。

#### 4.3.2 核心流程

`prepare` 的决策树：

```
prepare(batch):
    params = [r.sampling_params for r in batch.reqs]
    if 所有 params 都 is_greedy:
        return BatchSamplingArgs(temperatures=None)        # ← 快路径，仅此一项
    否则:
        ts     = [max(贪婪?0 : temperature, 1e-6) for p]    # 温度，下限 1e-6
        top_ks = [top_k if top_k>=1 else vocab_size for p]  # top_k=-1 → vocab_size
        top_ps = [clamp(top_p, 1e-6, 1.0) for p]            # top_p 夹到 [1e-6, 1]
        temperatures = 上传(ts, float32)                     # 一定物化
        top_k  = any(k != vocab_size) ? 上传(top_ks,int32) : None
        top_p  = any(p < 1.0)       ? 上传(top_ps,float32) : None
        return BatchSamplingArgs(temperatures, top_k, top_p)
```

理解这张决策树的关键是两个「归一化」：

- **温度归一化**：贪婪请求（`is_greedy=True`）在混合批里被赋温度 `0.0`，再被 `max(., 1e-6)` 抬到 `1e-6`。这避免了 softmax 里除以零，同时 `1e-6` 的极小温度会让概率分布极度尖锐，近似 argmax——于是贪婪与非贪婪请求能共用同一个 softmax kernel。
- **top_k 归一化**：`top_k=-1`（`SamplingParams` 的默认值，含义是「不过滤」）被映射成 `vocab_size`（保留全部词）。含义等价，但统一成「正整数」后才能塞进 int32 张量。

#### 4.3.3 源码精读

[python/minisgl/engine/sample.py:13-17 — BatchSamplingArgs](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L13-L17) ：三个字段都是 `Tensor | None`，`None` 即「该参数整批都不启用」。

[python/minisgl/engine/sample.py:20-21 — make_device_tensor](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L20-L21) ：先用 `pin_memory=True` 建一个锁页 CPU 张量，再 `to(device, non_blocking=True)` 异步上传。锁页内存是 H2D 真异步的前提（与 4.2 节 D2H 那次不同，这里是 H2D，方向相反）。

[python/minisgl/engine/sample.py:53-68 — prepare 主体](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L53-L68) ：逐行对应上面的决策树。注意第 55-56 行的全贪婪短路，以及第 64-67 行的两个 `any(...)` 守卫——它们决定 `top_k` / `top_p` 是否物化。

`is_greedy` 的判定在 [core.py:23-25](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/core.py#L23-L25) ：

```python
@property
def is_greedy(self) -> bool:
    return (self.temperature <= 0.0 or self.top_k == 1) and self.top_p == 1.0
```

即「温度 ≤ 0、或 top_k=1（只取最大那个）」且「top_p=1.0（不裁剪）」就是贪婪。默认 `SamplingParams(temperature=0.0, top_k=-1, top_p=1.0)` 满足 `temperature<=0`，所以默认即贪婪。

#### 4.3.4 代码实践（本讲指定任务）

**实践目标**：解释两个问题——(a) 为什么全贪婪时 `temperatures=None` 能走 `torch.argmax` 快路径？(b) `top_k=-1` 是如何被归一化的？以下是带答案的逐步分析。

**问题 (a)：全贪婪 → `temperatures=None` → `torch.argmax` 的快路径**

追踪链路：

1. [prepare 第 55-56 行](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L55-L56)：`if all(p.is_greedy for p in params): return BatchSamplingArgs(temperatures=None)`。整批都贪婪时，**不**上传任何采样参数张量，`temperatures` 直接为 `None`。
2. [sample 第 73-74 行](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L73-L74)：`if args.temperatures is None: return torch.argmax(logits, dim=-1)`。

**为什么这是「快路径」**？贪婪采样的数学定义就是「永远选 logits 最大的词」：

\[ \text{next\_token} = \arg\max_{i}\, \text{logit}_i \]

这正好就是 `torch.argmax(logits, dim=-1)`，**不需要**计算 softmax 概率、**不需要**调用 flashinfer 的采样 kernel、**不需要**上传温度/top_k/top_p 张量。一次 `argmax` 单 kernel 搞定，省去了 softmax 的指数运算和采样的随机数生成。所以全贪婪批走这条路既正确又显著更快。

**问题 (b)：`top_k=-1` 的归一化**

[prepare 第 60 行](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L60)：

```python
top_ks = [p.top_k if p.top_k >= 1 else self.vocab_size for p in params]
```

`top_k` 的语义是「只在概率最大的 k 个词里采样」。`-1`（以及任何 `< 1` 的值）表示「不限制」，即保留全部 `vocab_size` 个词。归一化分两步发挥作用：

1. **语义等价转换**：`-1 → vocab_size`。保留全部词 = 不过滤，二者在采样上完全等价，但 `vocab_size` 是个合法的正整数，能塞进 int32 张量喂给 kernel。
2. **驱动「按需物化」**：紧接着第 64 行 `if any(k != self.vocab_size for k in top_ks): top_k = ...`。如果整批每个请求的 `top_k` 都被归一化成了 `vocab_size`（即没人真的要用 top-k），那么 `any(...)` 为假，`top_k` 保持 `None`，于是 [sample_impl](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L24-L45) 会跳过 top-k 分支，走更轻的 `top_p_sampling_from_probs` 或纯 `sampling_from_probs`。

**结论**：`top_k=-1` 并不是一个会被 kernel 直接读懂的「魔法值」，而是先被 `prepare` 翻译成 `vocab_size`，再借助「全等于 vocab_size 即等于没用 top-k」这一等价关系，让整批都没用 top-k 时干脆不传 `top_k` 张量。`-1` 在这里只是 `SamplingParams` 暴露给用户的「未设置」哨兵值。

> 旁证：commit `5ae38fc`（见仓库 git log）专门为 `ForwardInput.write_tuple` 里的 `-1` 哨兵值补充了文档，说明项目里 `-1` 作为「未设置」是一个有意的、统一的设计约定。

#### 4.3.5 小练习与答案

**练习 1**：一个 batch 里有 3 个请求，采样参数分别是 `A(temperature=0)`、`B(temperature=0.7, top_k=40)`、`C(temperature=0)`。`prepare` 走快路径吗？`top_k` 张量会被物化吗？

> **答案**：不走快路径——`B` 不是贪婪（`temperature=0.7 > 0` 且 `top_k=40 != 1`），`all(is_greedy)` 为假。`top_ks` 归一化为 `[vocab_size, 40, vocab_size]`，因为 `any(k != vocab_size)` 为真（40），所以 `top_k` 张量**会**被物化上传。`A`、`C` 的温度被设为 `0.0` 再抬到 `1e-6`。

**练习 2**：为什么 `make_device_tensor` 要用 `pin_memory=True`，而 `forward_batch` 里的 D2H 拷贝（4.2 节）却没显式建锁页目标？

> **答案**：`make_device_tensor` 做的是 **H2D**（CPU→GPU）上传，锁页内存是 H2D 真异步、不阻塞 engine stream 的前提，所以显式 `pin_memory=True`。4.2 节的 D2H（GPU→CPU）拷贝用 `to("cpu", non_blocking=True)` 请求异步，其正确性由 `copy_done_event` 把关——即便目标不是锁页内存，event 的 `synchronize()` 也能保证读到完整数据。两处方向相反，权衡也不同。

---

### 4.4 sample_impl：分派到 flashinfer 采样 kernel

#### 4.4.1 概念说明

当 `prepare` 没走快路径（`temperatures` 非 `None`）时，`sample` 会调用 [`sample_impl`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L24-L45)，把真正采样数学交给 **flashinfer** 的采样 kernel。`sample_impl` 本身不做数学，只做**分派**：先统一做一次带温度的 softmax，再根据 `top_k` / `top_p` 是否为 `None`，选四个 flashinfer 函数之一。

#### 4.4.2 核心流程

采样的数学是：先用温度 \( T \) 把 logits 转成概率：

\[ p_i = \frac{\exp(\text{logit}_i / T)}{\sum_{j} \exp(\text{logit}_j / T)} \]

- **纯温度采样**：直接按概率 \( p \) 做多项式抽样（`sampling_from_probs`）。
- **top-k**：只保留概率最大的 k 个，重新归一化后再抽（`top_k_sampling_from_probs`）。
- **top-p（nucleus）**：把词按概率从大到小累加，取累计概率首次达到 p 的最小集合，重新归一化后抽（`top_p_sampling_from_probs`）。
- **top-k + top-p**：先 top-k 再 top-p（`top_k_top_p_sampling_from_probs`）。

分派逻辑（对应 [sample.py:33-45](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L33-L45)）：

```
probs = flashinfer.softmax(logits, temperatures, enable_pdl=is_sm90_supported())
if top_k is None and top_p is None:   → sampling_from_probs(probs)
elif top_p is None:                    → top_k_sampling_from_probs(probs, top_k)
elif top_k is None:                    → top_p_sampling_from_probs(probs, top_p)
else:                                  → top_k_top_p_sampling_from_probs(probs, top_k, top_p)
```

注意 `top_k` / `top_p` 是否为 `None` 完全由 `prepare` 的「按需物化」决定（4.3 节）。所以 `prepare` 与 `sample_impl` 是一对紧密耦合的设计：前者负责「能省则省」，后者负责「按剩余参数挑最省的 kernel」。

补充两个细节：

- 第 32 行 `enable_pdl=is_sm90_supported()`：PDL（Programmatic Dependent Launch，程序化依赖启动）是 Hopper（SM90）及更新架构的特性，能让一个 kernel 在等待依赖时提前启动下一段，flashinfer 在支持的卡上开启它以进一步降延迟。
- `sample` 方法带 `@nvtx_annotate("Sampler")` 装饰器并内嵌 `torch.cuda.nvtx.range("Sampler")`（[sample.py:70-75](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L70-L75)），方便用 Nsight Systems 这类工具把采样段在时间线上标出来做性能分析。

#### 4.4.3 源码精读

[python/minisgl/engine/sample.py:24-45 — sample_impl 四分支分派](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L24-L45) ：先 softmax，再按 `top_k`/`top_p` 的 `None` 组合选 kernel。函数签名里 `top_k` / `top_p` 的类型是 `Tensor | int | float | None`，但实际从 `BatchSamplingArgs` 传进来时只会是「张量或 None」（int/float 分支是为兼容其它调用路径保留的）。

[python/minisgl/engine/sample.py:70-75 — Sampler.sample 入口](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L70-L75) ：`temperatures is None` 走 `argmax` 快路径，否则 `sample_impl(logits.float(), ...)`——注意 `.float()`，flashinfer 的采样 kernel 要求 float32 概率输入，这里把可能的 bfloat16/half logits显式升精度。

#### 4.4.4 代码实践

**实践目标**：把 `prepare` 的输出与 `sample_impl` 的四个分支对应起来。

**操作步骤**（源码阅读型表格练习）：

在下表中，根据「batch 里请求的参数特征」推断 `prepare` 产出什么、`sample_impl` 走哪个分支，然后把空格填满：

| batch 参数特征 | `prepare` 产出 | `sample_impl` 分支 |
| --- | --- | --- |
| 全部 `is_greedy` | `temperatures=None` | （不走 sample_impl，直接 `argmax`） |
| 有非贪婪，但 `top_k` 全归一化为 vocab_size、`top_p` 全为 1.0 | `temperatures=tensor`, `top_k=None`, `top_p=None` | ?（填空 1） |
| 存在 `top_k < vocab_size`，无 `top_p < 1.0` | `temperatures=tensor`, `top_k=tensor`, `top_p=None` | ?（填空 2） |
| 无 `top_k < vocab_size`，存在 `top_p < 1.0` | `temperatures=tensor`, `top_k=None`, `top_p=tensor` | ?（填空 3） |
| 同时存在有效的 `top_k` 和 `top_p` | 三者都物化 | ?（填空 4） |

**预期结果（答案）**：

- 填空 1 → `sampling_from_probs(probs)`（纯温度采样）；
- 填空 2 → `top_k_sampling_from_probs(probs, top_k)`；
- 填空 3 → `top_p_sampling_from_probs(probs, top_p)`；
- 填空 4 → `top_k_top_p_sampling_from_probs(probs, top_k, top_p)`。

逐一对照 [sample.py:33-45](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L33-L45) 验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `sample_impl` 里要先做一次 `softmax`，再根据 top-k/top-p 分派，而不是每个分支各自从 logits 算起？

> **答案**：因为无论走哪个分支，第一步都是「把 logits 变成概率分布」，这部分计算（含温度缩放和指数归一化）对所有分支都一样。先统一算一次 `probs`，既避免重复实现 softmax，也让四个分支的 kernel 接口统一为「输入概率、输出 token」，flashinfer 的四个 `*_from_probs` 函数正是这样命名的。

**练习 2**：`sample` 里对非快路径调用了 `logits.float()`，但快路径（`argmax`）没有。为什么快路径不需要？

> **答案**：`torch.argmax` 只比较大小、不计算指数，对 bfloat16/half 等 logits 同样正确，且省一次类型转换。而 flashinfer 的 softmax/采样 kernel 要求 float32 概率输入，所以非快路径必须 `.float()` 升精度。这也是快路径更快的一个细节：它连这次类型转换都省了。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端跟踪一个 decode token 的诞生」。

**任务**：以一个 `batch.size=2` 的 decode 批为例，按时间顺序写出从 `Scheduler._forward` 调用 `Engine.forward_batch` 起、到下一轮 `_process_last_data` 消费完为止的完整事件链，并标注每一步发生在哪条 stream、用到了本讲的哪个模块。

**参考作答模板**（请先自己填，再对照）：

1. **调度阶段（scheduler stream）**：`_prepare_batch` 调 [`Sampler.prepare`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/sample.py#L53-L68) 生成 `BatchSamplingArgs`，连同装配好的 batch 组成 `ForwardInput`。（模块：Sampler.prepare）
2. **engine stream 入口**：[`forward_batch`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L191-L206) 断言在 engine stream 上，进入 `ctx.forward_batch(batch)`。（模块：forward_batch）
3. **算 logits（engine stream）**：decode 且 `size=2 ≤ max_graph_bs`，走 `graph_runner.replay(batch)` 得到 logits。（模块：forward_batch 的分支）
4. **推进游标（CPU，瞬时）**：对两个 req 各调一次 `complete_one()`，`device_len` 各 +1。（模块：forward_batch）
5. **采样（engine stream）**：若两 req 都贪婪 → `temperatures=None` → `argmax`；否则 → `sample_impl` 分派到 flashinfer。得到 `next_tokens_gpu`，`.to(int32)`。（模块：Sampler.prepare 的产出 + sample_impl）
6. **双副本 + 事件（engine stream）**：`next_tokens_gpu` 异步 D2H 得 `next_tokens_cpu`，`record(copy_done_event)`，封装 [`ForwardOutput`](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L23-L26) 返回。（模块：ForwardOutput）
7. **GPU 闭环（engine stream，立刻）**：Scheduler 把 `next_tokens_gpu` 写进 `token_pool[output_mapping]`，下一步前向再读出来——token 全程没离开 GPU。（模块：ForwardOutput 的 GPU 副本）
8. **延迟消费（scheduler stream，下一轮）**：`_process_last_data` 先 `copy_done.synchronize()`，再用 `next_tokens_cpu` 做 `append_host`、构造 `DetokenizeMsg` 送往 detokenizer。（模块：ForwardOutput 的 CPU 副本 + event）

完成后再回答一个问题：如果第 5 步两个请求一个贪婪、一个 `temperature=0.8, top_p=0.9`，`prepare` 会走快路径吗？`top_p` 张量会物化吗？

> **参考答案**：不走快路径（有一个非贪婪）；`top_p` 会物化（存在 `0.9 < 1.0`）；`top_k` 不物化（二者 `top_k` 都归一化为 vocab_size）。`sample_impl` 走 `top_p_sampling_from_probs` 分支。

---

## 6. 本讲小结

- `forward_batch` 是 Engine 的唯一前向入口，职责只有四件：**选前向路径 → `complete_one` 推进游标 → 采样 → 异步拷回**；它必须在 engine stream 上调用，并用 `ctx.forward_batch(batch)` 上下文把 batch 挂进全局 Context。
- 前向路径二选一：`can_use_cuda_graph(batch)`（decode 且 `size ≤ max_graph_bs`）走 graph replay，否则走 `model.forward()`；prefill 永远走普通前向。
- `Sampler.prepare` 把逐请求的 `SamplingParams` 压平成整批的 `BatchSamplingArgs`；全贪婪时短路返回 `temperatures=None`，让 `sample` 走 `torch.argmax` 快路径，完全绕开 softmax 与 flashinfer。
- `top_k=-1` 是「未设置」哨兵值，被 `prepare` 归一化为 `vocab_size`，并借助「全等于 vocab_size 即未用 top-k」驱动 `top_k` 张量的按需物化；同理 `top_p` 用 `any(p < 1.0)` 守卫。
- `sample_impl` 先做一次带温度 softmax，再按 `top_k`/`top_p` 的 `None` 组合分派到 flashinfer 的四个采样函数；它只分派、不做新数学。
- `ForwardOutput` 三件套体现「一份 token、两份副本、一个事件」：GPU 副本同 stream 立刻喂下一步（无需同步），CPU 副本异步 D2H 供下一轮 detokenizer 使用，`copy_done_event` 给延迟消费把关——这是 Overlap Scheduling 在 Engine 侧的具体落地。

---

## 7. 下一步学习建议

- **下一篇 u5-l3（CUDA Graph 捕获与回放）**：本讲把 `graph_runner.replay` 当作黑盒，下一篇会打开它，讲清 `GraphRunner` 如何按一组 batch size 捕获 decode 图、用 `dummy_req` 把真实 batch pad 到捕获尺寸、回放时为何只拷输入缓冲。读完你就能完整解释 4.1 节那条 graph 分支。
- **u6（KV Cache 管理）**：`forward_batch` 里 `model.forward()` 内部会调用 `store_kv` 把新 K/V 写进 KV 池——这条写回路径的存储布局与 `out_loc` 的来历，要到 u6-l1 才讲透。
- **u7（注意力后端）**：`ctx.forward_batch(batch)` 安装的 batch，最终被注意力后端的 `prepare_metadata` / `forward` 读取。u7-l2 会讲 FlashInfer 后端如何把 `cu_seqlens` / `indices` / `page_table` 喂给 paged attention。
- **回看 u4-l1（Overlap Scheduling）**：本讲的 `ForwardOutput` + event 是 Overlap 的「产物契约」，建议带着本讲的理解重读 u4-l1 的 `overlap_loop` 与 `_process_last_data`，体会「算当前批」与「处理上一批」如何在两条 stream 上交错。
