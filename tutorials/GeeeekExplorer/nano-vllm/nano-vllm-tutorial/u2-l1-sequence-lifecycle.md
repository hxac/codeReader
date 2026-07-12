# Sequence：请求状态与生命周期

## 1. 本讲目标

在 [u1-l3](u1-l3-generate-flow.md) 里，我们把推理主循环拆成了 `schedule → run → postprocess` 的三段式 `step`，并提到调度器在 `waiting` / `running` 两个队列之间来回搬运的"东西"叫 **Sequence**。本讲就钻进这张数据结构。

学完本讲，你应当能够：

1. 说清 `Sequence` 上每一个 **token 计数字段**（`num_tokens` / `num_prompt_tokens` / `num_cached_tokens` / `num_scheduled_tokens`）的含义、初值与更新时机。
2. 画出 `WAITING` / `RUNNING` / `FINISHED` 三态状态机，并指出每条迁移线由谁、在什么条件下触发。
3. 解释 `block_table` 为什么是一个整数列表，以及 `num_blocks` / `block(i)` / `last_block_num_tokens` 这几个分块视图如何把一维 token 序列切成块。
4. 说清 `__getstate__` / `__setstate__` 为什么在 decode 阶段故意丢弃整张 prompt，只保留最后一个 token。

本讲是"调度与请求管理"单元（u2）的第一篇，是后续 [Scheduler 调度](u2-l2-scheduler-prefill-decode.md)、[Chunked prefill 与抢占](u2-l3-chunked-prefill-preemption.md) 以及 [BlockManager 块管理](u3-l1-block-manager.md) 的共同数据基础。

## 2. 前置知识

本讲默认你已经读过了 u1 系列，下面几条会直接用到：

- **三段式 step**（u1-l3）：一次 `step` 是 `schedule()`（决策跑哪些序列、prefill 还是 decode）→ `run()`（前向 + 采样）→ `postprocess()`（写回 token、判定是否结束）。
- **prefill 与 decode 的区别**（u1-l3）：prefill 一次性算完整条 prompt 并产出第一个补全 token；decode 每步只喂 1 个 token、产出 1 个新 token。`step` 用 `num_tokens` 的正负号区分这两个阶段。
- **KV Cache 与 PagedAttention**：每个算过的 token 都会在显存里留一份 K/V 供后续注意力复用；PagedAttention 把这块显存切成固定大小的**块（block）**，每条序列用一个 `block_table` 记录自己占用了哪几个块。块的分配、引用计数、哈希缓存是 u3 的事，本讲只把 `block_table` 当作"一个整数列表"。
- **Sequence 的诞生**（u1-l3）：`LLMEngine.add_request` 把 prompt 经 `tokenizer.encode` 编码成 `token_ids`，再 `Sequence(prompt, sampling_params)` 入队。

> 一个直觉比喻：你可以把 `Sequence` 想成"一张工单"。工单上写着：这条请求的原始 token、目前算到哪儿了、本步要算多少、占用了显存的哪些块、现在排队排到哪个窗口（状态）。调度器是窗口工作人员，每一步都在改写这张工单。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪些部分 |
|---|---|---|
| [`nanovllm/engine/sequence.py`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py) | 定义 `Sequence` 与 `SequenceStatus`，本讲的绝对主角 | 全文 84 行基本都要读 |
| [`nanovllm/engine/scheduler.py`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py) | 改写 `Sequence` 字段的"工作人员" | `schedule` / `preempt` / `postprocess` 中的字段读写与状态迁移 |
| [`nanovllm/sampling_params.py`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/sampling_params.py) | `Sequence` 构造时拆解采样参数的来源 | `temperature` / `max_tokens` / `ignore_eos` 三字段 |
| [`nanovllm/engine/llm_engine.py`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py) | 创建 `Sequence`、配置类级 `block_size` | `add_request` 与 `Sequence.block_size = ...` |

## 4. 核心概念与源码讲解

### 4.1 SequenceStatus：序列的三态状态机

#### 4.1.1 概念说明

一条请求从"用户敲下回车"到"生成结束"，在引擎里会经历几个阶段：先在等待区排队（还没轮到算 prefill），算完 prefill 后进入运行区（每步产一个 decode token），达到终止条件后退出。`SequenceStatus` 就是用来标记"现在在哪一阶段"的枚举。

nano-vllm 把这个生命周期压缩成了**三态**：

| 状态 | 含义 | 物理位置 |
|---|---|---|
| `WAITING` | 尚未完成 prefill（或被抢占回退），等待调度 | `scheduler.waiting` 队列 |
| `RUNNING` | prefill 已完成，每步参与 decode | `scheduler.running` 队列 |
| `FINISHED` | 遇到 eos 或达到 `max_tokens`，退出循环 | 从所有队列移除 |

#### 4.1.2 核心流程

三态之间的迁移由 `Scheduler` 在 `schedule` / `preempt` / `postprocess` 中驱动，迁移条件如下：

```
        add_request() 入队                       prefill 全部算完
WAITING ───────────────► WAITING ───────────────────────────► RUNNING
  ▲                       (waiting 队列)                          │
  │                                                              │
  │            preempt()（显存不足，回退一条 running 序列）         │
  └──────────────────────────────────────────────────────────────┘
                              ▲                                  │
                              │                   decode 每步产 1 token
                              └──────────────┐                   │
                                             │                   ▼
                                            (回到 waiting)    postprocess()
                                                              命中 eos 或 max_tokens
                                                                 │
                                                                 ▼
                                                             FINISHED
```

三条迁移线的精确触发点：

1. **`WAITING → RUNNING`**：`schedule()` 在 prefill 分支里，当本步累计算的 token 已经覆盖整条 prompt 时，把序列从 `waiting` 弹出、塞进 `running`，并置为 `RUNNING`。
2. **`RUNNING → WAITING`**：`preempt()` 在 decode 阶段显存不够时被调用，把一条 running 序列的块全部释放、塞回 `waiting` 队首，状态回退为 `WAITING`（且 `is_prefill` 复位为 `True`，因为下次得重做 prefill）。
3. **`RUNNING → FINISHED`**：`postprocess()` 检测到新 token 是 eos（且 `ignore_eos=False`）或补全长度已达 `max_tokens`，置为 `FINISHED` 并释放块、移出 `running`。

#### 4.1.3 源码精读

枚举本身极其简短，用 `auto()` 自动分配值（具体数值不重要，只关心身份比较）：

[`nanovllm/engine/sequence.py#L8-L11`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L8-L11) —— 定义三个状态：

```python
class SequenceStatus(Enum):
    WAITING = auto()
    RUNNING = auto()
    FINISHED = auto()
```

`Sequence` 构造时默认就是 `WAITING`：

[`nanovllm/engine/sequence.py#L20`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L20) —— `self.status = SequenceStatus.WAITING`。

三条迁移线落在 `scheduler.py`。**`WAITING → RUNNING`**：

[`nanovllm/engine/scheduler.py#L48-L51`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L48-L51) —— prefill 累计算完 prompt 全长时迁移：

```python
if seq.num_cached_tokens + seq.num_scheduled_tokens == seq.num_tokens:
    seq.status = SequenceStatus.RUNNING
    self.waiting.popleft()
    self.running.append(seq)
```

**`RUNNING → WAITING`**（抢占）：

[`nanovllm/engine/scheduler.py#L75-L79`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L75-L79) —— 显存不足时把 running 序列打回 waiting：

```python
def preempt(self, seq: Sequence):
    seq.status = SequenceStatus.WAITING
    seq.is_prefill = True
    self.block_manager.deallocate(seq)
    self.waiting.appendleft(seq)
```

**`RUNNING → FINISHED`**：

[`nanovllm/engine/scheduler.py#L89-L92`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L89-L92) —— 命中终止条件时迁移并清理：

```python
if (not seq.ignore_eos and token_id == self.eos) or seq.num_completion_tokens == seq.max_tokens:
    seq.status = SequenceStatus.FINISHED
    self.block_manager.deallocate(seq)
    self.running.remove(seq)
```

此外，`Sequence` 还提供了一个只读便利属性 `is_finished`，让外部不必直接拿枚举比较：

[`nanovllm/engine/sequence.py#L39-L41`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L39-L41) —— `step` 结束后用它在 `llm_engine.py` 里筛已完成的序列收集输出。

#### 4.1.4 代码实践

**目标**：亲手触发一次完整的状态迁移，确认状态只能由调度器的三个动作改变。

**操作步骤**：

```python
from nanovllm.engine.sequence import Sequence, SequenceStatus
from nanovllm.sampling_params import SamplingParams

seq = Sequence([1, 2, 3], SamplingParams(max_tokens=2))
print("构造后:", seq.status)              # 期望 WAITING

# 模拟 schedule() 把它推进 RUNNING
seq.status = SequenceStatus.RUNNING
print("设为 RUNNING:", seq.status, "is_finished:", seq.is_finished)  # 期望 RUNNING / False

# 模拟 postprocess() 命中 max_tokens（补全已达 2）
seq.append_token(10); seq.append_token(11)   # 现在补全 2 个，下面用属性判定
print("num_completion_tokens:", seq.num_completion_tokens)
seq.status = SequenceStatus.FINISHED
print("结束:", seq.status, "is_finished:", seq.is_finished)          # 期望 FINISHED / True
```

**需要观察的现象**：状态从 `WAITING` 走到 `RUNNING` 再到 `FINISHED`；`is_finished` 仅在 `FINISHED` 时为 `True`。

**预期结果**：依次打印 `WAITING`、`RUNNING False`、`2`、`FINISHED True`。（本脚本纯 Python，无需 GPU，可直接 `python` 运行。）

#### 4.1.5 小练习与答案

**练习 1**：为什么没有 `RUNNING → WAITING` 之外"从 `FINISHED` 回退"的迁移？
**答**：`FINISHED` 是终态，`postprocess` 在置为 `FINISHED` 的同时调用了 `block_manager.deallocate(seq)` 并 `running.remove(seq)`，序列已脱离所有队列，不再被调度器触碰。

**练习 2**：`preempt` 里为什么要把 `is_prefill` 重新设成 `True`？
**答**：抢占会释放该序列的全部 KV 块（`deallocate`），下次重新轮到它时必须从头重做 prefill，所以要把 `is_prefill` 复位，让调度器重新走 prefill 分支。

---

### 4.2 Sequence 的字段与 token 计数体系

#### 4.2.1 概念说明

如果说 `status` 回答"序列现在在哪个队列"，那么一堆 `num_*` 字段回答"序列算到哪了、本步要算多少"。这是 `Sequence` 最核心、也最容易混淆的部分。

`Sequence` 在构造时把这些计数全部初始化，整个推理过程中由调度器反复读写。它们之间的关系可以用一个简单的等式与一张进度图说清。

记 \(N\) 为 prompt 长度，则：

\[ \text{num\_tokens} = \text{num\_prompt\_tokens} + \text{num\_completion\_tokens} \]

而 `num_cached_tokens` 是"已经算过 KV 的进度水位"，`num_scheduled_tokens` 是"本步要新算多少"。它们随 `step` 的推进关系如下：

```
prompt token : [ t0 | t1 | t2 | t3 | t4 | ... ]          completion: [ c0 | c1 | ...
               <────────── num_tokens（已存在） ──────────>
               <────── num_prompt_tokens（定值） ──────>
               <──────── num_cached_tokens（已算 KV 的水位） ────────>
                              <─ num_scheduled_tokens（本步增量） ─>
```

#### 4.2.2 核心流程

四个计数器的**含义、初值、更新者**汇总如下：

| 字段 | 含义 | 初值 | 谁更新 / 何时 |
|---|---|---|---|
| `num_tokens` | 序列当前总 token 数（prompt + 已生成补全） | `len(prompt)` | `append_token` 每次 +1 |
| `num_prompt_tokens` | prompt 长度，构造后**定值** | `len(prompt)` | 不再变 |
| `num_cached_tokens` | 已算过 KV 的 token 数（"进度水位"） | `0`（无前缀缓存命中时） | `postprocess` 每步累加 `num_scheduled_tokens` |
| `num_scheduled_tokens` | 本步要新算的 token 数 | `0` | `schedule` 设置；`postprocess` 清零 |

派生的只读属性：

| 属性 | 计算 |
|---|---|
| `num_completion_tokens` | `num_tokens - num_prompt_tokens` |
| `prompt_token_ids` | `token_ids[:num_prompt_tokens]` |
| `completion_token_ids` | `token_ids[num_prompt_tokens:]` |

一个 `step` 内字段的演变（以一条 prompt 长度为 \(N\)、无缓存的序列为例）：

1. **`schedule`（prefill）**：`seq.num_scheduled_tokens = min(num_tokens - num_cached_tokens, remaining)` —— 决定本步算多少。
2. **`run`**：模型用 `num_scheduled_tokens` 个 token 做前向（这些字段被 `__getstate__` 打包发给 worker，见 4.4）。
3. **`postprocess`**：`seq.num_cached_tokens += seq.num_scheduled_tokens; seq.num_scheduled_tokens = 0` —— 推进水位、清空本步计数。若 prefill 还没算完（`num_cached_tokens < num_tokens`），`continue` 不写 token；否则 `append_token(token_id)` 并检查终止条件。

进入 decode 阶段后，每步 `num_scheduled_tokens` 恒为 1，`num_cached_tokens` 在 prefill 结束时已等于 `num_tokens`，之后随 `append_token` 让 `num_tokens` 不断 +1。

> 注意一个反直觉点：`num_cached_tokens` 的"已缓存"指**已经在本引擎里算过并写入 KV 的 token**，与 4.3 节的"前缀缓存命中"不同。前缀缓存命中会让 `BlockManager.allocate` 在构造期就把 `num_cached_tokens` 设成命中块数 \(\times\) block_size，从而跳过这些 token 的重算——那是 u3-l2 的内容。

#### 4.2.3 源码精读

类头部声明了两个**类级**属性 `block_size` 和 `counter`，所有实例共享：

[`nanovllm/engine/sequence.py#L14-L16`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L14-L16) —— `block_size` 默认 256，`counter` 是自增 id 生成器：

```python
class Sequence:
    block_size = 256
    counter = count()
```

`block_size` 虽有默认值，但引擎启动时会被 `Config.kvcache_block_size` 覆盖（见 4.3.3）。`counter` 保证每条序列拿到全局唯一、单调递增的 `seq_id`，这也是 `generate` 最后能按 `seq_id` 排序输出的依据（u1-l3）。

构造函数 `__init__` 是字段最密集的地方：

[`nanovllm/engine/sequence.py#L18-L31`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L18-L31) —— 初始化全部计数字段、状态与采样参数：

```python
def __init__(self, token_ids: list[int], sampling_params = SamplingParams()):
    self.seq_id = next(Sequence.counter)
    self.status = SequenceStatus.WAITING
    self.token_ids = copy(token_ids)
    self.last_token = token_ids[-1]
    self.num_tokens = len(self.token_ids)
    self.num_prompt_tokens = len(token_ids)
    self.num_cached_tokens = 0
    self.num_scheduled_tokens = 0
    self.is_prefill = True
    self.block_table = []
    self.temperature = sampling_params.temperature
    self.max_tokens = sampling_params.max_tokens
    self.ignore_eos = sampling_params.ignore_eos
```

几个值得注意的细节：

- `token_ids` 用 `copy(token_ids)` 防御性拷贝，避免外部列表被意外修改；`num_prompt_tokens` 用的是未拷贝前的 `len(token_ids)`，二者长度一致。
- `last_token` 独立维护，decode 时模型只喂这一个 token（见 4.4），无需翻整张表。
- `is_prefill` 初值为 `True`，在 `schedule` 的 decode 分支里被置为 `False`（[`scheduler.py#L67-L68`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L67-L68)）。
- `SamplingParams` 的三个字段被**拆解**后平铺进 `Sequence`，于是请求在引擎里流转时不必再携带一个 `SamplingParams` 对象。

派生属性集中在文件中段：

[`nanovllm/engine/sequence.py#L43-L53`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L43-L53) —— 三个只读切片属性，`postprocess` 判终止时直接用 `num_completion_tokens`：

```python
@property
def num_completion_tokens(self):
    return self.num_tokens - self.num_prompt_tokens
```

decode 写回新 token 的唯一入口是 `append_token`：

[`nanovllm/engine/sequence.py#L67-L70`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L67-L70) —— 只动三个字段：列表、`last_token`、`num_tokens`：

```python
def append_token(self, token_id: int):
    self.token_ids.append(token_id)
    self.last_token = token_id
    self.num_tokens += 1
```

而推进"进度水位"的逻辑不在 `Sequence` 内部，而在调度器后处理里：

[`nanovllm/engine/scheduler.py#L83-L88`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L83-L88) —— 先累加水位再清零，prefill 未完则跳过 append：

```python
self.block_manager.hash_blocks(seq)
seq.num_cached_tokens += seq.num_scheduled_tokens
seq.num_scheduled_tokens = 0
if is_prefill and seq.num_cached_tokens < seq.num_tokens:
    continue
seq.append_token(token_id)
```

#### 4.2.4 代码实践

**目标**：用一条短 prompt 手动重放"prefill 调度 → 后处理推进 → 多次 decode"的计数演变，肉眼看到水位上涨。

**操作步骤**：

```python
from nanovllm.engine.sequence import Sequence
from nanovllm.sampling_params import SamplingParams

seq = Sequence([10, 11, 12, 13, 14], SamplingParams(max_tokens=3))
print(f"初始   : num_tokens={seq.num_tokens} "
      f"prompt={seq.num_prompt_tokens} cached={seq.num_cached_tokens} "
      f"sched={seq.num_scheduled_tokens}")

# —— 模拟一次 prefill 的 schedule + postprocess ——
seq.num_scheduled_tokens = seq.num_tokens - seq.num_cached_tokens   # schedule 设值
print(f"schedule: sched={seq.num_scheduled_tokens}")
seq.num_cached_tokens += seq.num_scheduled_tokens                   # postprocess 累加
seq.num_scheduled_tokens = 0                                        # postprocess 清零
seq.append_token(100)                                               # 写回首个补全 token
print(f"prefill后: num_tokens={seq.num_tokens} cached={seq.num_cached_tokens} "
      f"completion={seq.num_completion_tokens}")

# —— 模拟 3 次 decode ——
for i, tok in enumerate([101, 102, 103], start=1):
    seq.num_scheduled_tokens = 1                                    # decode 每步 1
    seq.num_cached_tokens += seq.num_scheduled_tokens
    seq.num_scheduled_tokens = 0
    seq.append_token(tok)
    print(f"decode{i}: num_tokens={seq.num_tokens} cached={seq.num_cached_tokens} "
          f"completion={seq.num_completion_tokens}")
```

**需要观察的现象**：`num_cached_tokens` 在 prefill 后追上 `num_tokens`，之后每次 decode 仍 +1（因为 `append_token` 让 `num_tokens` 也 +1，水位与总长始终持平）；`num_completion_tokens` 从 1 涨到 4。

**预期结果**：

```
初始   : num_tokens=5 prompt=5 cached=0 sched=0
schedule: sched=5
prefill后: num_tokens=6 cached=6 completion=1
decode1: num_tokens=7 cached=7 completion=2
decode2: num_tokens=8 cached=8 completion=3
decode3: num_tokens=9 cached=9 completion=4
```

（待本地验证：脚本纯 Python 可直接运行。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `num_cached_tokens` 在 decode 阶段也持续 +1，而不是停在 `num_prompt_tokens`？
**答**：decode 每步产出的新 token 也要算它的 K/V 并写入 KV cache，所以"已算过 KV 的 token 数"会随 `append_token` 同步增长，始终等于 `num_tokens`。

**练习 2**：若一条 1000 token 的 prompt 因 `max_num_batched_tokens=512` 被切两块 prefill，第一步 `postprocess` 里 `num_cached_tokens` 会变成多少？会不会 `append_token`？
**答**：第一步 `num_scheduled_tokens=512`，故 `num_cached_tokens` 变为 512；由于 `is_prefill and num_cached_tokens(512) < num_tokens(1000)` 成立，`continue` 跳过 `append_token`，要等第二步算完剩余 488 个 token、`num_cached_tokens == num_tokens` 时才写回首个补全 token。

---

### 4.3 block_table 与分块视图

#### 4.3.1 概念说明

`block_table` 是 `Sequence` 唯一直接接触显存布局的字段：一个整数列表，每个元素是一个**块编号**，记录这条序列按顺序占用了 `BlockManager` 里的哪些 KV 块。块的内部细节（引用计数、空闲池、哈希）由 `BlockManager` 管（u3-l1），`Sequence` 只持有这张"块索引表"。

为了让外部按块视角访问 token，`Sequence` 提供了三个分块视图属性/方法：`num_blocks`、`last_block_num_tokens`、`block(i)`。它们把一维的 `token_ids` 切成一段段长度为 `block_size` 的片。

#### 4.3.2 核心流程

给定 `block_size = B`、`num_tokens = T`，块数（向上取整）：

\[
\text{num\_blocks} = \left\lceil \frac{T}{B} \right\rceil = \frac{T + B - 1}{B}
\]

最后一个块的占用 token 数：

\[
\text{last\_block\_num\_tokens} = T - (\text{num\_blocks} - 1) \cdot B
\]

第 \(i\) 块对应的 token 切片（从 0 计）：

\[
\text{block}(i) = \text{token\_ids}[\,i\cdot B : (i+1)\cdot B\,]
\]

举一个具体例子，设 \(B = 4\)、\(T = 10\)：

```
token_ids : [t0 t1 t2 t3 | t4 t5 t6 t7 | t8 t9]
block(i)  :   block 0       block 1       block 2
num_blocks = ceil(10/4) = 3
last_block_num_tokens = 10 - 2*4 = 2
block_table = [b0, b1, b2]   # 三个整数（块编号），由 BlockManager 分配
```

注意：`block_table` 的**长度等于 `num_blocks`**（在 prefill 一次性分配后），二者是"逻辑块"与"物理块编号"的对应关系。`block_table` 在 `__init__` 时是空列表，由 `BlockManager.allocate`（prefill）和 `BlockManager.may_append`（decode 跨块时）逐步填充。

#### 4.3.3 源码精读

`block_size` 是类级属性，默认 256，但被引擎在启动时覆盖：

[`nanovllm/engine/llm_engine.py#L21`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L21) —— 用 `Config.kvcache_block_size` 改写所有序列共享的块大小：

```python
Sequence.block_size = config.kvcache_block_size
```

`block_table` 初值为空：

[`nanovllm/engine/sequence.py#L28`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L28) —— `self.block_table = []`。

三个分块视图：

[`nanovllm/engine/sequence.py#L55-L65`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L55-L65) —— 向上取整算块数、算尾块占用、按块切片：

```python
@property
def num_blocks(self):
    return (self.num_tokens + self.block_size - 1) // self.block_size

@property
def last_block_num_tokens(self):
    return self.num_tokens - (self.num_blocks - 1) * self.block_size

def block(self, i):
    assert 0 <= i < self.num_blocks
    return self.token_ids[i*self.block_size: (i+1)*self.block_size]
```

谁在用这些视图？`BlockManager` 在做前缀缓存匹配时会逐块调用 `seq.block(i)` 算哈希、比对内容（[`block_manager.py#L62-L68`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L62-L68)）。也就是说，`Sequence` 负责回答"我的第 i 块装的是哪些 token"，`BlockManager` 负责回答"这块要不要复用缓存/新分配一块"，二者职责分离。

`__len__` 与 `__getitem__` 让 `Sequence` 用起来像序列：

[`nanovllm/engine/sequence.py#L33-L37`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L33-L37) —— `len(seq)` 等于 `num_tokens`，`seq[i]` 取 token：

```python
def __len__(self):
    return self.num_tokens

def __getitem__(self, key):
    return self.token_ids[key]
```

这也是 `BlockManager.can_append` 里 `len(seq) % block_size == 1` 这种写法能成立的原因——`len(seq)` 直接返回 `num_tokens`。

#### 4.3.4 代码实践

**目标**：把 `block_size` 调小，肉眼看清 token 如何被切成块、`block_table` 如何随块数增长。

**操作步骤**：

```python
from nanovllm.engine.sequence import Sequence
from nanovllm.sampling_params import SamplingParams

Sequence.block_size = 4                      # 调小便于观察（引擎里是 256）
seq = Sequence([10, 11, 12, 13, 14, 15, 16, 17, 18, 19], SamplingParams())

print("num_tokens  :", seq.num_tokens)
print("num_blocks  :", seq.num_blocks, "  (期望 ceil(10/4)=3)")
print("last_block  :", seq.last_block_num_tokens, "  (期望 10-2*4=2)")
for i in range(seq.num_blocks):
    print(f"block({i})     :", seq.block(i))

# 模拟 BlockManager.allocate 在 prefill 时填入块编号
seq.block_table = [100, 101, 102]
print("block_table :", seq.block_table)

# 模拟 decode 不断追加，看 block_table 何时该增长
for tok in [20, 21]:
    seq.append_token(tok)
    need_new = (len(seq) % seq.block_size == 1)
    print(f"append {tok}: num_tokens={seq.num_tokens} "
          f"num_blocks={seq.num_blocks} need_new_block={need_new}")
```

**需要观察的现象**：10 个 token 占 3 个块、尾块 2 个 token；`block(0..2)` 切片正确；追加到第 13、17 个 token（即 `num_tokens=13,17`，满足 `% 4 == 1`）时 `need_new_block` 为真——这正是 `BlockManager.may_append` 触发新分配的时机。

**预期结果**：

```
num_tokens  : 10
num_blocks  : 3   (期望 ceil(10/4)=3)
last_block  : 2   (期望 10-2*4=2)
block(0)     : [10, 11, 12, 13]
block(1)     : [14, 15, 16, 17]
block(2)     : [18, 19]
block_table : [100, 101, 102]
append 20: num_tokens=11 num_blocks=3 need_new_block=False
append 21: num_tokens=12 num_blocks=3 need_new_block=False
```

（追加示例里只到 12，`need_new_block` 仍为 `False`；若继续追加到 `num_tokens=13` 才会变 `True`——可自行加循环验证。待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`num_blocks` 用的是向上取整而不是向下取整，为什么？
**答**：尾块哪怕只装 1 个 token 也必须独占一个物理块来存它的 KV，所以必须向上取整，保证每个 token 都有所属块。

**练习 2**：`can_append` 里 `len(seq) % block_size == 1` 的判定，为什么是 `== 1` 而不是 `== 0`？
**答**：decode 在每步**开始**时调用 `may_append`，此时 `num_tokens` 已包含上一步 `append_token` 写回的新 token。当 `num_tokens = k*block_size + 1` 时，意味着上一个 token 落进了新块的第 0 个槽，必须先分配这块才能写它的 KV；所以判据是模 1。

---

### 4.4 \_\_getstate\_\_ / \_\_setstate\_\_：为多进程 IPC 精简状态

#### 4.4.1 概念说明

`__getstate__` / `__setstate__` 是 Python `pickle` 协议的两个钩子：前者决定"序列化时吐出什么"，后者决定"反序列化时怎么还原"。如果没定义它们，`pickle` 会默认序列化对象的全部 `__dict__`。

nano-vllm 为什么要费劲自定义这两个方法？因为它的张量并行（TP）实现是用 **`multiprocessing` 拉起多个 worker 进程**（u5-l3），rank 0 要把每条 `Sequence` **广播**给所有 worker，让它们各自跑前向。这条广播走的是共享内存 + `pickle`，**体积越小越好**。

关键观察：worker 只需要算前向，**不需要采样**（采样只在 rank 0 做，见 u5-l3）。而且：

- **prefill 阶段**：worker 需要整条 prompt 的 token_ids 才能算；
- **decode 阶段**：worker 只需要**最后一个 token**（`last_token`），因为它每步只喂 1 个 token。

于是一个朴素但有效的优化出现了：**prefill 时序列化整张 `token_ids`，decode 时只序列化一个 int（`last_token`），把可能成百上千的 prompt token 全部省掉。**

#### 4.4.2 核心流程

`__getstate__` 返回一个**元组**（不是字典），只挑了前向必需的 6 个字段：

```
(num_tokens, num_prompt_tokens, num_cached_tokens, num_scheduled_tokens, block_table, last_state)
```

其中 `last_state` 是个**变体**：

\[
\text{last\_state} =
\begin{cases}
\text{token\_ids} & \text{若 } \text{is\_prefill} = \text{True} \\
\text{last\_token} & \text{若 } \text{is\_prefill} = \text{False}
\end{cases}
\]

`__setstate__` 收到元组后，根据 `last_state` 的**类型**反推当前阶段：是 `list` 就是 prefill，恢复整张 `token_ids`；不是 list（是个 int）就是 decode，把 `token_ids` 置空、只留 `last_token`。

被**有意丢弃**的字段：`seq_id`、`status`、`is_prefill`、`temperature`、`max_tokens`、`ignore_eos`。

- `temperature / max_tokens / ignore_eos`：采样参数，worker 不采样，自然不需要。
- `status / is_prefill`：worker 从 `run(seqs, is_prefill)` 的调用参数知道阶段，不必从序列里读。
- `seq_id`：worker 不需要全局身份。

> 注意：`__setstate__` 不还原 `is_prefill`。worker 端反序列化得到的 `Sequence` 对象上**没有** `is_prefill` 这个属性——这没关系，因为 worker 不访问它，阶段信息来自调用入参。这是"按需精简"的必然代价：反序列化后的对象不是完整的，只为前向服务。

#### 4.4.3 源码精读

[`nanovllm/engine/sequence.py#L72-L74`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L72-L74) —— 序列化时按 `is_prefill` 决定带整表还是只带末 token：

```python
def __getstate__(self):
    last_state = self.last_token if not self.is_prefill else self.token_ids
    return (self.num_tokens, self.num_prompt_tokens, self.num_cached_tokens,
            self.num_scheduled_tokens, self.block_table, last_state)
```

[`nanovllm/engine/sequence.py#L76-L83`](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L76-L83) —— 反序列化时用 `isinstance(last_state, list)` 区分阶段：

```python
def __setstate__(self, state):
    self.num_tokens, self.num_prompt_tokens, self.num_cached_tokens, \
        self.num_scheduled_tokens, self.block_table, last_state = state
    if isinstance(last_state, list):
        self.token_ids = last_state
        self.last_token = self.token_ids[-1]
    else:
        self.token_ids = []
        self.last_token = last_state
```

这套机制能成立，前提是"prefill 时 `token_ids` 必非空、decode 时只看 `last_token`"，而这正是 prefill/decode 模型的输入约定。它把"阶段"这个语义编码进了**数据本身的形状**（list vs int），是一个很巧的精简。

#### 4.4.4 代码实践

**目标**：用标准库 `pickle` 模拟一次"rank 0 → worker"的广播，对比 prefill 与 decode 两种情况下序列化体积的差异。

**操作步骤**：

```python
import pickle
from nanovllm.engine.sequence import Sequence
from nanovllm.sampling_params import SamplingParams

# 一条 1000 token 的"长 prompt"
seq = Sequence(list(range(1000)), SamplingParams())

# —— 情形 A：prefill（is_prefill=True，默认）——
blob_a = pickle.dumps(seq)
seq2 = pickle.loads(blob_a)
print(f"[prefill ] 字节数={len(blob_a)}  反序列化 token_ids 长度={len(seq2.token_ids)}")

# —— 情形 B：decode（手动把 is_prefill 置 False，模拟进入 decode）——
seq.is_prefill = False
blob_b = pickle.dumps(seq)
seq3 = pickle.loads(blob_b)
print(f"[decode  ] 字节数={len(blob_b)}  反序列化 token_ids 长度={len(seq3.token_ids)} "
      f"last_token={seq3.last_token}")
print(f"体积比 decode/prefill ≈ {len(blob_b)/len(blob_a):.3f}")
```

**需要观察的现象**：prefill 的 blob 明显更大（携带 1000 个 int），decode 的 blob 小一两个数量级（只带 1 个 int）；decode 反序列化后 `token_ids` 为空、`last_token` 仍在。

**预期结果**（具体字节数取决于 pickle 实现，量级关系是重点）：

```
[prefill ] 字节数≈4300  反序列化 token_ids 长度=1000
[decode  ] 字节数≈70    反序列化 token_ids 长度=0 last_token=999
体积比 decode/prefill ≈ 0.016
```

（待本地验证：实际字节数可能略有出入，但 decode 体积应远小于 prefill。）

#### 4.4.5 小练习与答案

**练习 1**：如果 worker 端某段代码不小心访问了 `seq3.is_prefill`，会发生什么？为什么？
**答**：抛 `AttributeError`。因为 `__setstate__` 没有还原 `is_prefill`（也不还原 `status`、`temperature` 等），反序列化后的对象只为前向而生，不带这些字段。worker 若需要阶段信息，应从 `run(seqs, is_prefill)` 的入参取。

**练习 2**：为什么 `last_state` 用"是 list 还是 int"来隐式编码阶段，而不是直接在元组里多塞一个布尔？
**答**：用类型编码可以少传一个字段，且 `token_ids` 在 prefill 时本就必须传（worker 要算整条 prompt），它的存在/缺失天然对应 prefill/decode，无需额外标志位。这是"把语义折叠进数据形状"的极简做法。

---

## 5. 综合实践

把本讲的计数体系、状态机、`block_table` 与 pickle 精简串起来，手动**驱动一条 `Sequence` 走完完整生命周期**，全程只动 `Sequence` 和调度器对它的那几行操作，不碰真实模型。

**任务**：写一段脚本，构造一条 prompt（长度自定，建议 10 token），设小 `block_size`（如 4）与 `max_tokens=3`，按真实调度器的节奏，把它从 `WAITING` 推到 `RUNNING` 再到 `FINISHED`，并在每一步打印：

- 当前 `status`
- `num_tokens` / `num_cached_tokens` / `num_scheduled_tokens`
- `block_table`（用一个自增计数器模拟 `BlockManager` 分配的块编号）
- `num_completion_tokens` / `is_finished`

**参考骨架**：

```python
from nanovllm.engine.sequence import Sequence, SequenceStatus
from nanovllm.sampling_params import SamplingParams

Sequence.block_size = 4
seq = Sequence(list(range(10)), SamplingParams(max_tokens=3))
EOS = -1
fake_block_pool = iter(range(1000))

def alloc_blocks(n):
    return [next(fake_block_pool) for _ in range(n)]

def show(tag):
    print(f"{tag:>10} | status={seq.status.name:8} num_tokens={seq.num_tokens:2} "
          f"cached={seq.num_cached_tokens:2} sched={seq.num_scheduled_tokens} "
          f"completion={seq.num_completion_tokens} block_table={seq.block_table}")

show("init")

# 1) prefill：分配块、整段调度、推进水位、写首个补全 token
seq.block_table = alloc_blocks(seq.num_blocks)
seq.num_scheduled_tokens = seq.num_tokens - seq.num_cached_tokens
seq.num_cached_tokens += seq.num_scheduled_tokens
seq.num_scheduled_tokens = 0
if seq.num_cached_tokens == seq.num_tokens:
    seq.status = SequenceStatus.RUNNING
    seq.append_token(100)            # prefill 产出的首个补全 token
show("prefill")

# 2) decode 3 次（max_tokens=3，应在第 3 次命中终止）
for step, tok in enumerate([101, 102, EOS], start=1):
    seq.num_scheduled_tokens = 1
    if len(seq) % seq.block_size == 1:                 # 模拟 may_append
        seq.block_table += alloc_blocks(1)
    seq.num_cached_tokens += seq.num_scheduled_tokens
    seq.num_scheduled_tokens = 0
    seq.append_token(tok)
    if (not seq.ignore_eos and tok == EOS) or seq.num_completion_tokens == seq.max_tokens:
        seq.status = SequenceStatus.FINISHED
        seq.block_table = []
    show(f"decode{step}")
    if seq.is_finished:
        break

print("最终 is_finished:", seq.is_finished)
```

**验收点**：

1. prefill 后 `status` 变 `RUNNING`、`num_cached_tokens == num_tokens`、`block_table` 长度 = `num_blocks`。
2. 每步 decode `num_completion_tokens` +1，跨块时（`num_tokens` 满足 `% 4 == 1`）`block_table` 增长。
3. 第 3 次 decode 因 `num_completion_tokens == max_tokens`（或命中 EOS）置 `FINISHED`，`block_table` 被清空。

（待本地验证：本脚本纯 Python，可直接 `python` 运行；无需 GPU 与模型权重。）

## 6. 本讲小结

- `Sequence` 是一条请求在引擎里流转的"工单"，承载 token、计数、状态、块表与采样参数。
- 四个核心计数器：`num_tokens`（总长，随 `append_token` +1）、`num_prompt_tokens`（定值）、`num_cached_tokens`（已算 KV 的进度水位，`postprocess` 累加）、`num_scheduled_tokens`（本步增量，`schedule` 设值、`postprocess` 清零）。
- 三态状态机 `WAITING` / `RUNNING` / `FINISHED` 由调度器驱动：`schedule` 推进、`preempt` 回退、`postprocess` 终结。
- `block_table` 是物理块编号列表，长度等于 `num_blocks`；`num_blocks` 向上取整、`block(i)` 按块切片，供 `BlockManager` 做缓存匹配与分配。
- `__getstate__` / `__setstate__` 为多进程张量并行广播而精简：prefill 带整张 `token_ids`，decode 只带 `last_token`，并丢弃采样参数等 worker 不需要的字段。
- `block_size` 是类级属性，引擎启动时由 `Config.kvcache_block_size` 统一覆盖。

## 7. 下一步学习建议

本讲把"被搬运的数据结构"讲透了，下一步自然是看"搬运它的人"：

- [u2-l2 Scheduler：prefill 与 decode 调度](u2-l2-scheduler-prefill-decode.md) —— 精读 `Scheduler.schedule` 如何在 `waiting` / `running` 间决策、受 `max_num_seqs` 与 `max_num_batched_tokens` 约束，以及 `postprocess` 如何写回 token 并判定终止。
- [u2-l3 Chunked prefill 与抢占机制](u2-l3-chunked-prefill-preemption.md) —— 长 prompt 如何分块、显存不足时如何 `preempt` 回退。
- [u3-l1 PagedAttention 块管理 BlockManager](u3-l1-block-manager.md) —— `block_table` 背后的块分配、引用计数与 `may_append` 的全部细节。

建议在进入 u2-l2 之前，先把本讲综合实践跑一遍，确保你能凭空手动驱动一条 `Sequence` 的完整生命周期。
