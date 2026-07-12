# Scheduler：prefill 与 decode 调度

## 1. 本讲目标

本讲聚焦 nano-vllm 推理引擎的「决策大脑」——`Scheduler`。学完后你应该掌握：

1. `Scheduler.schedule()` 的返回值结构，以及「prefill 优先于 decode」这一核心策略是如何用代码体现的。
2. 两个调度约束 `max_num_seqs`（批次内最多多少条序列）与 `max_num_batched_tokens`（单步最多算多少个 token）分别作用在哪一层，二者如何共同决定一个 step 里调度哪些序列。
3. `postprocess()` 如何把模型产出的 token 写回序列、推进 KV 计算水位、登记前缀缓存，并在命中 EOS 或达到 `max_tokens` 时判定序列终止。

本讲只讲 `Scheduler` 本身的调度与后处理逻辑；它调用的 `BlockManager`（块分配、缓存命中）的内部细节留给第 3 单元，分块 prefill 与抢占（preempt）的深入机制留给下一讲（u2-l3）。

## 2. 前置知识

在进入 `Scheduler` 之前，先回顾两个来自前序讲义的关键认知。

**两个阶段：prefill 与 decode。** 大模型推理分两段。prefill 阶段一次性吃下整条 prompt，算出所有位置的 KV Cache，并在最后产出一个 token；decode 阶段每次只吃上一个 token，逐步续写。prefill 是算力密集型（并行算一长串），decode 是访存密集型（每步只算一个）。引擎必须显式区分这两个阶段来调度。

**Sequence 的计数字段。** 这是上一讲（u2-l1）的主角，本讲会反复用到其中三个：

- `num_tokens`：当前序列的总 token 数（prompt 长度 + 已生成的 completion）。
- `num_cached_tokens`：已经算过 KV 的 token 水位，即「计算进度」。
- `num_scheduled_tokens`：本次 step 被调度去算的 token 增量，由 `schedule` 写入、`postprocess` 清零。

字段定义见 [nanovllm/engine/sequence.py:25-26](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L25-L26)（`num_cached_tokens` 与 `num_scheduled_tokens` 初值均为 0）。

**三态状态机。** 序列有三种状态（[nanovllm/engine/sequence.py:8-11](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L8-L11)）：`WAITING`（排队等 prefill）、`RUNNING`（prefill 完毕、正在 decode）、`FINISHED`（生成结束）。`Scheduler` 正是驱动这些状态迁移的角色。

**step 的三段式。** 来自 u1-l3：`LLMEngine.step()` 严格按 `schedule`（决策）→ `run`（前向采样）→ `postprocess`（写回）执行（[nanovllm/engine/llm_engine.py:49-55](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L49-L55)）。本讲就是要把 `schedule` 和 `postprocess` 这一头一尾彻底讲透。

## 3. 本讲源码地图

本讲主要涉及以下文件：

| 文件 | 作用 |
| --- | --- |
| [nanovllm/engine/scheduler.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py) | **本讲主角**。`Scheduler` 类，含 `schedule`/`preempt`/`postprocess`。 |
| [nanovllm/engine/llm_engine.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py) | `step()` 调用 `schedule`/`postprocess`，展示返回值如何被消费。 |
| [nanovllm/engine/block_manager.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py) | `schedule`/`postprocess` 调用的块管理方法（`can_allocate`/`allocate`/`can_append`/`may_append`/`hash_blocks`/`deallocate`）。本讲只看接口语义，内部留到第 3 单元。 |
| [nanovllm/engine/sequence.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py) | `Sequence` 的计数字段与 `append_token`。 |
| [nanovllm/config.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py) | `max_num_seqs`、`max_num_batched_tokens` 的默认值。 |

## 4. 核心概念与源码讲解

本讲把 `Scheduler` 拆成四个最小模块：双队列与 prefill 优先策略、prefill 调度的双约束、decode 调度与显存检查、postprocess 写回与终止判定。

### 4.1 双队列与 prefill 优先策略

#### 4.1.1 概念说明

`Scheduler` 内部维护两个双端队列（`deque`）：

- `waiting`：等待 prefill 的序列。新请求经 `add()` 进来时一律入此队（[nanovllm/engine/scheduler.py:22-23](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L22-L23)）。
- `running`：已经完成 prefill、正在 decode 的序列。

引擎每次 `step` 都要回答一个问题：**这一步该 prefill 还是 decode？** nano-vllm 的回答是「prefill 优先」——只要 `waiting` 里有可以调度的序列，就优先做 prefill；只有当 `waiting` 空（或里面的序列暂时不可调度）时，才对 `running` 做 decode。这样做的好处是让新请求尽快进入生成态，降低首 token 延迟。

`schedule()` 的返回值是一个二元组 `(scheduled_seqs, is_prefill)`：

- `scheduled_seqs`：本次 step 真正要送进模型前向的序列列表。
- `is_prefill`：布尔标志，告诉调用方这批序列处于哪个阶段。

这个布尔标志非常关键，它一路影响后续：`step()` 用它决定吞吐量统计的正负号（[nanovllm/engine/llm_engine.py:51](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L51)），`postprocess` 用它决定是否要把采样 token 追加进序列（见 4.4）。

#### 4.1.2 核心流程

`schedule` 的顶层结构可以用下面这段伪代码概括：

```
function schedule():
    scheduled_seqs = []
    # 第一阶段：尝试 prefill
    while waiting 非空 且 未超 max_num_seqs:
        ... 取 waiting[0]，扣 token 预算，写 num_scheduled_tokens ...
        scheduled_seqs.append(seq)
    if scheduled_seqs 非空:
        return scheduled_seqs, True        # ← prefill 优先：有就立刻返回

    # 第二阶段：decode（只有 prefill 没产出时才走到这里）
    while running 非空 且 未超 max_num_seqs:
        ... 取 running 序列，每条调度 1 个 token ...
    return scheduled_seqs, False
```

注意中间那句 `if scheduled_seqs: return ..., True`——它是「prefill 优先」的字面体现：只要 prefill 循环放过哪怕一条序列，就立刻返回，绝不让 decode 抢这一步。

#### 4.1.3 源码精读

队列与约束参数在构造函数里初始化（[nanovllm/engine/scheduler.py:10-17](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L10-L17)），其中 `max_num_seqs`、`max_num_batched_tokens`、`eos`、`block_size` 都直接来自 `Config`，并构造一个 `BlockManager`：

```python
self.max_num_seqs = config.max_num_seqs
self.max_num_batched_tokens = config.max_num_batched_tokens
self.eos = config.eos
self.block_size = config.kvcache_block_size
self.block_manager = BlockManager(config.num_kvcache_blocks, config.kvcache_block_size)
self.waiting: deque[Sequence] = deque()
self.running: deque[Sequence] = deque()
```

`schedule` 的签名与「prefill 优先」的提前返回见 [nanovllm/engine/scheduler.py:25-55](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L25-L55)，关键就是这一句：

```python
if scheduled_seqs:
    return scheduled_seqs, True
```

只要 prefill 阶段攒到了序列，就立即带 `True` 返回；否则才继续往下走 decode 逻辑。

#### 4.1.4 代码实践

**实践目标**：直观感受「prefill 优先」。

**操作步骤**：

1. 打开 [nanovllm/engine/llm_engine.py:49-55](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L49-L55) 的 `step()`。
2. 在 `seqs, is_prefill = self.scheduler.schedule()` 之后加一行日志：`print(f"[step] is_prefill={is_prefill}, num_seqs={len(seqs)}")`。
3. 运行 `example.py`（见 u1-l1），传入多条 prompt。

**需要观察的现象**：日志会先连续打印若干行 `is_prefill=True`（把所有 prompt 依次 prefill 完），之后才进入大量 `is_prefill=False`（decode 阶段）。**不会出现 prefill 与 decode 在同一个 step 内混跑的情况**——这正是 prefill 优先策略的直接证据。

**预期结果**：待本地验证（需要 GPU 环境跑通推理）。若暂无环境，也可纯靠阅读 [nanovllm/engine/scheduler.py:54-55](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L54-L55) 的提前返回逻辑推出同一结论。

#### 4.1.5 小练习与答案

**练习 1**：如果 `waiting` 里有一条 100 万 token 的超长 prompt，且 `max_num_batched_tokens` 很小，引擎会不会一直卡在 prefill、永远不 decode 其它已就绪序列？

**答案**：会持续做该长 prompt 的分块 prefill，但不一定「永远不 decode」。因为分块 prefill 只对 `waiting` 的**首条**序列切片（见 4.2），且每个 step 只要 `scheduled_seqs` 非空就以 prefill 返回。在长 prompt 仍在 `waiting` 首部且未完成期间，引擎确实会一直返回 prefill；只有当它被切完、晋升 `RUNNING` 后，后续新请求才可能被 prefill。这是 nano-vllm 极简实现的取舍。

**练习 2**：`schedule` 的返回元组里第二个值为什么必须是布尔而不是枚举？它被哪两处消费？

**答案**：因为只有 prefill/decode 两态，布尔足够。它被 `step()` 的吞吐统计（`num_tokens` 正负号）和 `postprocess`（是否追加 token）消费。

### 4.2 prefill 调度：token 预算与序列数双约束

#### 4.2.1 概念说明

prefill 调度同时受两个上限约束，二者分别限制「宽度」和「总量」：

- `max_num_seqs`：一个 step 内最多调度多少条序列（宽度上限，默认 512，见 [nanovllm/config.py:10](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L10)）。
- `max_num_batched_tokens`：一个 step 内所有序列加起来最多算多少个 token（总量上限，默认 16384，见 [nanovllm/config.py:9](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L9)）。

为什么需要两个？因为 prefill 时每条序列贡献的 token 数等于其 prompt 长度，长短不一。光限制序列条数，可能让单步 token 总量爆炸（算力超载）；光限制 token 总量，又可能塞进极多短序列（调度开销大）。两个约束一起作用，才能把每个 step 的计算量稳定在一个可控区间。

此外，prefill 还要处理两个细节：**前缀缓存命中**（部分 prompt 的 KV 已经算过、可直接复用，不应再算）与**分块 prefill**（一条 prompt 太长、一次算不完，需切片）。

#### 4.2.2 核心流程

prefill 循环对 `waiting` 队首序列逐一处理，流程如下：

```
while waiting 非空 且 len(scheduled_seqs) < max_num_seqs:
    seq = waiting[0]
    remaining = max_num_batched_tokens - 已累计 token 数
    if remaining == 0: break                       # 预算耗尽

    if seq 是新序列（block_table 为空）:
        num_cached_blocks = block_manager.can_allocate(seq)   # 缓存命中块数
        if num_cached_blocks == -1: break          # 显存不够
        num_tokens = seq.num_tokens - num_cached_blocks * block_size   # 扣掉命中部分
    else:  # 分块 prefill 的续算
        num_tokens = seq.num_tokens - seq.num_cached_tokens

    if remaining < num_tokens 且 scheduled_seqs 非空:
        break          # 只允许首条序列被分块；非首条放不下就让步

    给 seq 分配块；写 num_scheduled_tokens = min(num_tokens, remaining)
    if 全部 prompt 都已覆盖:
        seq 状态 → RUNNING，从 waiting 移到 running
    scheduled_seqs.append(seq)
```

这里有两个关键公式。每条序列本次实际算的 token 数：

\[
\text{num\_scheduled\_tokens} = \min(\text{num\_tokens},\ \text{remaining})
\]

而序列晋升为 `RUNNING` 的判据是「已算部分恰好覆盖整条 prompt」：

\[
\text{num\_cached\_tokens} + \text{num\_scheduled\_tokens} = \text{num\_tokens}
\]

注意这里的 `num_cached_tokens` 在 `allocate` 阶段被设成「命中的缓存块 × 块大小」（见 [nanovllm/engine/block_manager.py:92](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L92)），所以缓存命中的 token 既不算进 `num_tokens`（被减掉了），又通过 `num_cached_tokens` 计入了「覆盖量」，逻辑自洽。

#### 4.2.3 源码精读

prefill 循环主体见 [nanovllm/engine/scheduler.py:29-52](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L29-L52)。几个要点逐段看。

**双约束的 while 条件与预算检查**（[nanovllm/engine/scheduler.py:30-34](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L30-L34)）：

```python
while self.waiting and len(scheduled_seqs) < self.max_num_seqs:
    seq = self.waiting[0]
    remaining = self.max_num_batched_tokens - num_batched_tokens
    if remaining == 0:
        break
```

`len(scheduled_seqs) < self.max_num_seqs` 是宽度约束；`remaining == 0` 是总量约束（预算耗尽立即停）。

**新序列 vs 续算的两分支**（[nanovllm/engine/scheduler.py:35-41](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L35-L41)）：

```python
if not seq.block_table:
    num_cached_blocks = self.block_manager.can_allocate(seq)
    if num_cached_blocks == -1:
        break
    num_tokens = seq.num_tokens - num_cached_blocks * self.block_size
else:
    num_tokens = seq.num_tokens - seq.num_cached_tokens
```

`block_table` 是否为空是判别「首次 prefill」与「分块续算」的开关。`can_allocate` 返回 -1 表示显存不足以放下这条新序列（连扣掉缓存后所需的新块都凑不齐），此时直接 break，本步 prefill 终止（其内部判定逻辑留到 u3-l2）。

**「只允许首条分块」的开关**（[nanovllm/engine/scheduler.py:42-43](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L42-L43)）：

```python
if remaining < num_tokens and scheduled_seqs:  # only allow chunked prefill for the first seq
    break
```

注意 `and scheduled_seqs`——只有当「已经调度过至少一条序列」时，才不允许当前序列被切。换言之，**第一条序列允许 `num_scheduled_tokens < num_tokens`（被切），后续序列要么整条放下，要么让步**。这个设计避免了「一堆序列都被切一半」的混乱，把分块仅限于队首那条最长的。

**分配、写调度量、晋升**（[nanovllm/engine/scheduler.py:44-52](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L44-L52)）：

```python
if not seq.block_table:
    self.block_manager.allocate(seq, num_cached_blocks)
seq.num_scheduled_tokens = min(num_tokens, remaining)
num_batched_tokens += seq.num_scheduled_tokens
if seq.num_cached_tokens + seq.num_scheduled_tokens == seq.num_tokens:
    seq.status = SequenceStatus.RUNNING
    self.waiting.popleft()
    self.running.append(seq)
scheduled_seqs.append(seq)
```

注意分块未完成的序列**不会被 popleft**，仍留在 `waiting[0]`，下次 `schedule` 会从 `else` 分支续算；只有整条 prompt 覆盖完毕才晋升 `RUNNING` 并搬到 `running` 队列。

#### 4.2.4 代码实践

这是本讲的**核心实践**（对应大纲指定的练习任务）：把 `max_num_batched_tokens` 设得很小，观察分块 prefill 与多序列调度。

**实践目标**：亲手验证 token 预算如何切分长 prompt、如何放行/挡住后续序列。

**操作步骤**：

1. 在 [nanovllm/engine/scheduler.py:46-52](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L46-L52) 的 `scheduled_seqs.append(seq)` 之前加日志：
   ```python
   print(f"[prefill] seq_id={seq.seq_id} num_tokens={num_tokens} "
         f"num_scheduled={seq.num_scheduled_tokens} remaining={remaining} "
         f"promoted={seq.status == SequenceStatus.RUNNING}")
   ```
2. 用很小的预算构造引擎（**示例代码，非项目原有**）：
   ```python
   from nanovllm import LLM, SamplingParams
   llm = LLM("Qwen/Qwen3-0.6B", max_num_batched_tokens=10)
   sp = SamplingParams(temperature=0.5, max_tokens=3)
   llm.generate(["短句", "另一句稍长一点的输入", "第三句"], sp)
   ```
3. 把每一步 `schedule` 返回的 `(seq_id, num_scheduled_tokens)` 抄下来，画成表格。

**需要观察的现象（手工推演，假设三条 prompt 长度分别为 12、5、8 token，无缓存命中，`max_num_batched_tokens=10`）**：

| step | 返回 seq（num_scheduled） | 说明 |
| --- | --- | --- |
| 1 | seq0(10) | 预算 10 < 12，首条被切；seq0 仍 waiting |
| 2 | seq0(2)→晋升, seq1(5)→晋升 | seq0 续算 2 个补满，再放 seq1(5)，此时累计 7；seq2 需 8 但 remaining=3，非首条→让步 |
| 3 | seq2(8)→晋升 | seq2 整条放下 |
| 4+ | seq0,seq1,seq2 各 1 | waiting 已空，进入 decode |

**预期结果**：日志中能清楚看到 seq0 被切成两段、seq2 在 step 2 被「让步」推迟、每个 step 的 `num_scheduled_tokens` 之和不超过 10。待本地验证（需 GPU）。

> 如果没有 GPU，可把上表当作「源码阅读型实践」的答案——它完全由 [nanovllm/engine/scheduler.py:30-52](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L30-L52) 的逻辑推出，不必真跑。

#### 4.2.5 小练习与答案

**练习 1**：为什么分块 prefill 只允许作用于首条序列，而不是所有序列都切？

**答案**：若所有序列都被切，`waiting` 里会堆积大量「半成品」序列，每条都占着已分配的块、状态又不明确，调度与显存管理会变复杂。限定只切队首一条，既解决了「单条过长」的痛点，又把分块状态局限在一条序列上，实现极简。

**练习 2**：`can_allocate` 返回的 `num_cached_blocks` 同时影响了 `num_tokens` 和晋升判据中的 `num_cached_tokens`，二者如何配合？

**答案**：`num_tokens` 减掉 `num_cached_blocks * block_size`，使本步只需算「未命中」的 token；而 `allocate` 把 `num_cached_tokens` 设为 `num_cached_blocks * block_size`（[block_manager.py:92](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L92)），于是晋升判据 `num_cached_tokens + num_scheduled_tokens == num_tokens` 中，命中部分被计入「已覆盖」，未命中部分由本步 `num_scheduled_tokens` 补齐，正好凑满。

### 4.3 decode 调度：显存检查与队列回填

#### 4.3.1 概念说明

decode 阶段每条序列每步只产 1 个 token，调度逻辑相对简单，但要处理一个显存问题：每生成一个新 token，就要往 KV Cache 里写一个新槽位；当 token 数跨越块边界时，需要新分配一个物理块。如果空闲块不够（显存紧张），就得**抢占**（preempt）一些正在 decode 的序列——把它们退回 `waiting`、释放块，腾出空间。

这里用到 `BlockManager` 的两个接口（本讲只讲语义，内部留到 u3-l1）：

- `can_append(seq)`：判断序列追加一个 token 时是否还能拿到所需块（[block_manager.py:103-104](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L103-L104)）。
- `may_append(seq)`：真正执行追加，必要时分配新块（[block_manager.py:106-108](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L106-L108)）。

#### 4.3.2 核心流程

decode 循环从 `running` 队首取序列，每条调度 1 个 token：

```
while running 非空 且 len(scheduled_seqs) < max_num_seqs:
    seq = running.popleft()
    while not can_append(seq):          # 显存不够
        if running 里还有别的序列:
            preempt(running.pop())      # 抢占尾部序列，释放块
        else:
            preempt(seq); break         # 没别人可抢，抢自己并放弃本步
    else:                               # can_append 通过（while 正常结束）
        seq.num_scheduled_tokens = 1
        seq.is_prefill = False
        may_append(seq)
        scheduled_seqs.append(seq)
# 把这批序列按原顺序放回 running 队首
running.extendleft(reversed(scheduled_seqs))
```

这里有一个 Python 易错点：**`while ... else`** 中的 `else` 仅在循环**未被 `break` 打断**时执行。也就是说：

- `can_append` 一次通过 → 循环体不执行 → `else` 执行 → 序列正常调度。
- `can_append` 失败 → 抢占别人释放块 → 重试通过 → `else` 执行 → 序列调度成功。
- `can_append` 失败且无别人可抢 → `preempt(seq)` 后 `break` → **`else` 不执行** → 该序列被踢回 `waiting`，本步不参与。

`preempt` 的动作见 [nanovllm/engine/scheduler.py:75-79](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L75-L79)：状态退回 `WAITING`、`is_prefill` 复位为 `True`（意味着要重做 prefill）、释放块、`appendleft` 回 `waiting` 队首。其恢复与重算的完整时序是下一讲 u2-l3 的主题。

#### 4.3.3 源码精读

decode 循环见 [nanovllm/engine/scheduler.py:57-73](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L57-L73)：

```python
while self.running and len(scheduled_seqs) < self.max_num_seqs:
    seq = self.running.popleft()
    while not self.block_manager.can_append(seq):
        if self.running:
            self.preempt(self.running.pop())
        else:
            self.preempt(seq)
            break
    else:
        seq.num_scheduled_tokens = 1
        seq.is_prefill = False
        self.block_manager.may_append(seq)
        scheduled_seqs.append(seq)
assert scheduled_seqs
self.running.extendleft(reversed(scheduled_seqs))
return scheduled_seqs, False
```

几个细节：

- decode 每条序列固定 `num_scheduled_tokens = 1`，并显式置 `is_prefill = False`（供 `__getstate__` 序列化时只传 `last_token`，详见 u2-l1）。
- `assert scheduled_seqs` 是一道安全检查：decode 必须至少调度出一条序列。极端显存压力下（连一条序列的单个新块都凑不齐）会触发断言——这是 nano-vllm 极简实现未优雅处理的边界。
- 末尾 `self.running.extendleft(reversed(scheduled_seqs))` 把刚取出的序列**按原顺序**放回 `running` 队首。因为 `extendleft` 会把列表倒着插，所以先 `reversed` 再插，正负抵消，保序。

#### 4.3.4 代码实践

**实践目标**：观察 decode 阶段的稳定节奏与 `running` 队列保序回填。

**操作步骤**：

1. 在 [nanovllm/engine/scheduler.py:72](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L72) 的 `extendleft` 之前加日志：
   ```python
   print(f"[decode] seq_ids={[s.seq_id for s in scheduled_seqs]} "
         f"running_size={len(self.running)} free_blocks={len(self.block_manager.free_block_ids)}")
   ```
2. 跑 `example.py`，待进入 decode 阶段（日志 `is_prefill=False` 后）观察输出。

**需要观察的现象**：每步 decode 的 `seq_ids` 列表长度等于当前 `running` 序列数；随序列陆续 `FINISHED`，列表逐渐变短；`free_blocks` 在抢占发生时会有跳变。

**预期结果**：待本地验证。无 GPU 时，可通过阅读 `extendleft(reversed(...))` 推断「每步 decode 处理全部 running 序列、且顺序稳定」这一结论。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接 `running.extend(scheduled_seqs)` 把序列放回队尾，而要用 `extendleft(reversed(...))` 放回队首？

**答案**：decode 的语义是「每步推进所有 running 序列一格」，序列相对顺序应保持不变、且下一步仍从同一批开始。放回队尾会改变轮转顺序；`extendleft(reversed(...))` 利用两次反转互相抵消，把这批序列原序插回队首，保证下一步 `popleft` 取出的还是它们。

**练习 2**：`while not can_append(seq)` 循环里，抢占的是 `running.pop()`（队尾）而不是 `running.popleft()`（队首），为什么？

**答案**：队尾是最近才加入 running 的序列，优先抢占它们可以让「资格最老、可能快结束」的队首序列尽量保留，减少重算浪费。这与 vLLM 的「recompute 式抢占」思路一致。

### 4.4 postprocess：写回 token、推进水位与终止判定

#### 4.4.1 概念说明

`run`（模型前向 + 采样）之后，每条序列都会得到**恰好一个** token。`postprocess` 负责把这些 token 落袋为安，做四件事：

1. **登记前缀缓存**：把本步新写满的整块 KV 注册进哈希表，供将来的请求命中。
2. **推进计算水位**：`num_cached_tokens += num_scheduled_tokens`，把「本步算的」并入「已算的」。
3. **（视情况）追加 token**：把采样结果接到序列末尾。
4. **判定终止**：命中 EOS 或达到 `max_tokens` 则置 `FINISHED` 并释放块。

#### 4.4.2 核心流程

```
for seq, token_id in zip(seqs, token_ids):       # 每序列一个 token
    block_manager.hash_blocks(seq)               # 登记新写满的块
    seq.num_cached_tokens += seq.num_scheduled_tokens   # 推进水位
    seq.num_scheduled_tokens = 0
    if is_prefill 且 num_cached_tokens < num_tokens:
        continue                                 # 分块 prefill 未到末尾，丢弃本 token
    seq.append_token(token_id)                   # 追加 token
    if (未忽略 eos 且 token == eos) 或 num_completion_tokens == max_tokens:
        seq.status = FINISHED
        block_manager.deallocate(seq)            # 释放块
        running.remove(seq)
```

其中最易错的是那句 `if is_prefill and num_cached_tokens < num_tokens: continue`。它的含义是：**如果是 prefill 步骤、但 prompt 还没被全覆盖（即分块 prefill 的中间几步），就丢弃这次采样 token，不追加。** 因为只有整条 prompt 算完后的那个 token 才是真正有意义的「第一个生成 token」，中间步骤的采样结果不该混入序列。

终止判据里 `num_completion_tokens` 的定义见 [nanovllm/engine/sequence.py:43-45](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L43-L45)，即 `num_tokens - num_prompt_tokens`，也就是已生成的 completion 数；`append_token` 把 `num_tokens` 加 1（[sequence.py:67-70](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L67-L70)），所以追加后立即检查它是否追平 `max_tokens`。

#### 4.4.3 源码精读

`postprocess` 全文见 [nanovllm/engine/scheduler.py:81-92](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L81-L92)：

```python
def postprocess(self, seqs: list[Sequence], token_ids: list[int], is_prefill: bool):
    for seq, token_id in zip(seqs, token_ids):
        self.block_manager.hash_blocks(seq)
        seq.num_cached_tokens += seq.num_scheduled_tokens
        seq.num_scheduled_tokens = 0
        if is_prefill and seq.num_cached_tokens < seq.num_tokens:
            continue
        seq.append_token(token_id)
        if (not seq.ignore_eos and token_id == self.eos) or seq.num_completion_tokens == seq.max_tokens:
            seq.status = SequenceStatus.FINISHED
            self.block_manager.deallocate(seq)
            self.running.remove(seq)
```

几个要点：

- `zip(seqs, token_ids)` 隐含一个不变量：**模型每步对每条序列只产 1 个 token**，无论 prefill 还是 decode。prefill 产出的是「prompt 末位的预测」，decode 产出的是「续写的下一个」。
- `hash_blocks`（[block_manager.py:110-120](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L110-L120)）只在「整块恰好写满」时才登记，所以多数 decode 步（块未满）它是空操作。
- 终止分支里的 `self.running.remove(seq)` 能安全执行，是因为：能走到这里的 prefill 序列都已在 `schedule` 中晋升 `RUNNING`（在 `running` 里）；而 `continue` 跳过的分块 prefill 序列还在 `waiting`，不会走到 `remove`，故不会误删。

#### 4.4.4 代码实践

**实践目标**：验证终止判定的两条触发路径。

**操作步骤**：

1. 在 [nanovllm/engine/scheduler.py:89-92](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L89-L92) 的终止分支里加日志：
   ```python
   print(f"[FINISHED] seq_id={seq.seq_id} reason="
         f"{'eos' if (not seq.ignore_eos and token_id==self.eos) else 'max_tokens'} "
         f"completion={seq.num_completion_tokens}")
   ```
2. 用两组参数对比（**示例代码**）：
   ```python
   # 路径 A：靠 max_tokens 终止
   SamplingParams(temperature=0.5, max_tokens=2)
   # 路径 B：靠 eos 终止（设 ignore_eos=False，让其自然遇到 eos）
   SamplingParams(temperature=0.5, max_tokens=1000)
   ```
3. 分别跑推理，观察日志里的 `reason` 字段。

**需要观察的现象**：路径 A 下序列在生成 2 个 token 后以 `reason=max_tokens` 结束；路径 B 下多数序列会在遇到句末符时以 `reason=eos` 提前结束（completion 远小于 1000）。

**预期结果**：待本地验证。无 GPU 时，可直接由 [nanovllm/engine/scheduler.py:89](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L89) 的或表达式推出这两条触发路径。

#### 4.4.5 小练习与答案

**练习 1**：为什么分块 prefill 的中间步要 `continue` 丢弃 token？如果不丢，会发生什么？

**答案**：分块 prefill 的中间步只算了 prompt 的一部分，此时对「部分 prompt」做采样得到的 token 没有意义，不是真正的下一个词。若不丢而追加，会把垃圾 token 混进序列、污染后续 decode。只有覆盖完整条 prompt（`num_cached_tokens == num_tokens`）后的那次采样才值得保留。

**练习 2**：`ignore_eos=True` 时，序列还能怎样终止？

**答案**：只能靠 `num_completion_tokens == max_tokens` 终止（[scheduler.py:89](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L89)）。此时 `not seq.ignore_eos` 为假，左半短路失效，全靠右半的长度上限。

## 5. 综合实践

**任务**：用一张表完整刻画「3 条长短不一的请求、在很小的 token 预算下、从 prefill 到全部 FINISHED」的全过程，把本讲四个模块串起来。

设定：`max_num_batched_tokens = 10`，`max_num_seqs = 512`，`block_size = 256`，无前缀缓存命中，三条 prompt 长度分别为 12、5、8 token，`max_tokens = 2`，`ignore_eos = True`（避免提前结束，便于观察）。

请按下面要求填写（答案用「源码阅读型」推演，即直接依据 [scheduler.py:25-92](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L25-L92) 推出）：

| step | 阶段 | 调度的 seq（num_scheduled） | 关键事件 |
| --- | --- | --- | --- |
| 1 | prefill | seq0(10) | seq0 被切，仍 waiting |
| 2 | prefill | seq0(2)→RUNNING, seq1(5)→RUNNING | seq0 补满；seq2 因 remaining=3<8 且非首条被让步 |
| 3 | prefill | seq2(8)→RUNNING | seq2 整条放下，waiting 空 |
| 4 | decode | seq0(1), seq1(1), seq2(1) | 各自第 1 个 completion token |
| 5 | decode | seq0(1), seq1(1), seq2(1) | 各自第 2 个 completion token；3 条全部 `num_completion_tokens==2==max_tokens` → FINISHED |

**核查点**：

1. step 2 为什么 seq2 不出现？→ 因为 [scheduler.py:42-43](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L42-L43) 的「非首条放不下就 break」。
2. step 4-5 的 `num_scheduled_tokens` 为什么都是 1？→ decode 固定写 1（[scheduler.py:67](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L67)）。
3. step 5 后为什么三条都结束？→ [scheduler.py:89](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L89) 右半 `num_completion_tokens == max_tokens` 命中。

> 若有 GPU，可把 4.2.4 的日志加上，跑一遍验证上表；若无，上表即为基于源码的可靠推演结果。

## 6. 本讲小结

- `Scheduler` 用 `waiting`/`running` 两个双端队列组织请求，新请求入 `waiting`，prefill 完成后晋升 `running`。
- `schedule()` 遵循 **prefill 优先**：只要 prefill 循环产出序列就立即带 `is_prefill=True` 返回，否则才 decode，返回 `False`。
- prefill 同时受 `max_num_seqs`（宽度）与 `max_num_batched_tokens`（token 总预算）双重约束；首条序列允许分块，其余序列要么整条放下要么让步。
- 缓存命中由 `can_allocate` 返回的块数同时扣减 `num_tokens`、计入 `num_cached_tokens`，使晋升判据 `num_cached_tokens + num_scheduled_tokens == num_tokens` 自洽。
- decode 每条序列每步调度 1 个 token，跨块时由 `can_append`/`may_append` 管理新块，显存不足时 `preempt` 退回尾部序列。
- `postprocess` 完成四件事：`hash_blocks` 登记缓存、推进 `num_cached_tokens` 水位、按需 `append_token`（分块 prefill 中间步丢弃 token）、命中 EOS 或 `max_tokens` 时置 `FINISHED` 并释放块。

## 7. 下一步学习建议

本讲把 `schedule`/`postprocess` 的主干讲清了，但刻意留下了两块「深水区」：

1. **下一讲 u2-l3《Chunked prefill 与抢占机制》**：专门深挖分块 prefill 的完整触发与续算时序，以及 `preempt` 把序列退回 `waiting` 后如何重做 prefill、`can_append` 的显存检查内部逻辑。如果你觉得 4.2 的「首条切片」和 4.3 的 `while...else` 还没看透，u2-l3 会把它们彻底拆开。
2. **第 3 单元《显存与 KV Cache》**：`can_allocate`/`allocate`/`can_append`/`may_append`/`hash_blocks`/`deallocate` 的内部实现——块池、引用计数、基于 xxhash 的链式前缀哈希。建议按 u3-l1（BlockManager）→ u3-l2（Prefix Caching）→ u3-l3（KV Cache 显存预算）的顺序阅读。

建议在进入下一讲前，先回头把本讲「综合实践」的表格自己推一遍——能独立推出每一步的 `num_scheduled_tokens` 与状态迁移，就说明你真正掌握了 `Scheduler` 的调度逻辑。
