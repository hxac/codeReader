# Chunked prefill 与抢占机制

## 1. 本讲目标

承接 u2-l2 对 Scheduler「prefill 优先、waiting/running 双队列调度」的理解，本讲聚焦 Scheduler 在两个极端场景下如何不让系统卡死：

- 一条 prompt 太长、单步算不完 → **chunked prefill**（分块预填充）。
- KV Cache 装不下所有正在 decode 的序列 → **preempt**（抢占回退）。

学完后你应能：

1. 说清 chunked prefill 的触发条件，以及「只允许首条序列分块」这条守卫背后的调度约束。
2. 描述 preempt 如何回收 decode 序列、序列如何从 WAITING 恢复、为什么 nano-vllm 选择「重算」而非「换出」。
3. 读懂 `BlockManager.can_append` 那一行布尔表达式，知道 decode 跨块时何时需要申请新块。
4. 用一个**不依赖 GPU** 的调度器仿真脚本，亲手触发并观察这两种机制。

## 2. 前置知识

- **Sequence 关键字段**：`num_tokens`（总长）、`num_cached_tokens`（已算 KV 的水位）、`num_scheduled_tokens`（本步增量）、`block_table`（占用的物理块编号列表）、`is_prefill`，以及 `WAITING/RUNNING/FINISHED` 三态。详见 u2-l1。
- **step 三段式与 prefill 优先**：一次 step 是 `schedule → run → postprocess`；只要 waiting 有可调度序列就做 prefill，否则才 decode。详见 u1-l3 与 u2-l2。
- **PagedAttention 块概念**：KV Cache 被切成固定大小 `block_size`（默认 256）的块，每块容纳 `block_size` 个 token，序列通过 `block_table` 记录自己占用的物理块编号。本讲只需这个直觉，细节见 u3-l1。
- **三个引擎级旋钮**：`max_num_batched_tokens`（单步 prefill 的 token 总预算，默认 16384）、`max_num_seqs`（单步最多调度多少条序列，默认 512）、`num_kvcache_blocks`（KV 块总数，由显存预算决定，见 u3-l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [nanovllm/engine/scheduler.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py) | `schedule()` 的 prefill/decode 两条分支、`preempt()` 抢占实现 |
| [nanovllm/engine/block_manager.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py) | `can_append()`/`may_append()` 跨块检查与分配、`deallocate()` 释放块 |
| [nanovllm/engine/sequence.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py) | `num_tokens`/`num_cached_tokens`/`num_scheduled_tokens`/`block_table` 等字段 |
| [nanovllm/engine/llm_engine.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py) | 第 21 行把 `Sequence.block_size` 同步为 `Config.kvcache_block_size` |

## 4. 核心概念与源码讲解

### 4.1 Scheduler.schedule：chunked prefill 的触发与「只切首条」

#### 4.1.1 概念说明

prefill 要把整条 prompt 的所有 token 算一遍 attention。当一条 prompt 很长（几千 token），而单步预算 `max_num_batched_tokens` 又装不下它时，nano-vllm 不会卡住等下一次完整调度，而是把这条 prompt **切成多段**，每个 step 只算一段（一个 chunk），分多个 step 把整条 prompt 算完。这就是 chunked prefill。

好处：超长 prompt 不会阻塞调度器，其它短请求可在长 prompt 的 chunk 之间插队，整体吞吐更平稳。代价：这条长 prompt 自己的 prefill 完成时间被拉长。

#### 4.1.2 核心流程

`schedule()` 的 prefill 分支伪代码：

```
当 waiting 非空 且 本步调度数 < max_num_seqs:
    seq = waiting 队首                          # 每轮都重新取队首
    remaining = max_num_batched_tokens - 本步已用 token
    若 remaining == 0: 跳出
    若 seq 全新(block_table 为空):
        num_cached_blocks = can_allocate(seq)   # 查前缀缓存命中块数
        若返回 -1(剩余块装不下): 跳出
        待算 = num_tokens - num_cached_blocks * block_size
    否则(seq 已被切过、正在续算):
        待算 = num_tokens - num_cached_tokens   # 减去已算水位
    若 remaining < 待算 且 本步已调度过别的序列: 跳出   # 只允许首条分块
    若 seq 全新: allocate(seq, num_cached_blocks)
    seq.num_scheduled_tokens = min(待算, remaining)
    若 已算水位 + 本段 == num_tokens: 晋升 RUNNING
    把 seq 加入本步调度列表
```

关键在 `seq = waiting 队首`：一条正在分块的序列在算完前**不会**被移出 waiting（只有完全算完才 `popleft` 晋升 RUNNING），所以它一直卡在队首，每个 step 续算一段。

#### 4.1.3 源码精读

prefill 分支主体见 [nanovllm/engine/scheduler.py:30-52](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L30-L52)，逐段说明：

- **L31** `seq = self.waiting[0]`：始终取队首，正被分块的序列自然被续算。
- **L32-L34**：`remaining` 是本步剩余 token 预算，耗尽即跳出整个 prefill 循环。
- **L35-L39**：全新序列走 `can_allocate` 查前缀缓存命中块数；返回 `-1` 表示剩余块不够装下这条序列，直接跳出（连头都开不了）。命中缓存块对应的 token 不用算，所以待算量要扣掉 `num_cached_blocks * block_size`。
- **L40-L41**：已在分块中的序列（`block_table` 非空）走 else，待算 = 总长 − 已算水位 `num_cached_tokens`。
- **L42-L43**：「只允许首条分块」守卫，见下文。
- **L44-L45**：全新序列第一次进来时调用 `allocate`，**一次性把整条序列需要的物理块全部分好**（块数由 `num_blocks` 决定）。分块只影响「算多少 token」，不影响「分配多少块」。
- **L46**：本步实际算的 token = `min(待算, remaining)`，`remaining` 不够时就只算 `remaining` 这么多 → 这就是「切片」。
- **L48-L51**：当累计已算水位等于总长时，整条 prefill 完成，序列晋升 `RUNNING`、移出 waiting、加入 running。

「只允许首条序列分块」的守卫见 [nanovllm/engine/scheduler.py:42-43](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L42-L43)：

```python
if remaining < num_tokens and scheduled_seqs:  # only allow chunked prefill for the first seq
    break
```

含义：如果当前队首序列本步放不下（`remaining < 待算`）**且**本步已经调度过别的序列（`scheduled_seqs` 非空），就整个跳出，把这条序列留到下一个 step 作为「第一条」再处理。这保证每个 step 里至多有一条「正在分块」的序列，且它一定是本步最先处理的那条——避免把一条整段 prefill 的序列和一条半截 prefill 的序列混进同一个 varlen 打包批次（那会让 `model_runner.prepare_prefill` 的偏移计算变得复杂）。

#### 4.1.4 代码实践

**目标**：用一个不依赖 GPU 的调度器仿真，亲眼看到一条长 prompt 被切成多段。

**操作步骤**：在仓库根目录新建 `observe_chunk.py`（这是示例脚本，可放在任意位置运行）：

```python
from nanovllm.config import Config
from nanovllm.engine.scheduler import Scheduler
from nanovllm.engine.sequence import Sequence
from nanovllm.sampling_params import SamplingParams

MODEL = "~/huggingface/Qwen3-0.6B/"          # u1-l1 已下载
cfg = Config(MODEL, num_kvcache_blocks=64,
             max_num_batched_tokens=32)        # 故意调到 32，强制切片
Sequence.block_size = cfg.kvcache_block_size   # 对齐 llm_engine.py:21 的同步
sched = Scheduler(cfg)

sp = SamplingParams(temperature=0.6, ignore_eos=True, max_tokens=3)
sched.add(Sequence(token_ids=[100] * 100, sampling_params=sp))  # 100 token 的 prompt

step = 0
while not sched.is_finished():
    seqs, is_prefill = sched.schedule()
    sched.postprocess(seqs, [1] * len(seqs), is_prefill)        # 喂假 token（id=1，非 eos）
    step += 1
    head = seqs[0]
    print(f"step{step} prefill={is_prefill} "
          f"sched={head.num_scheduled_tokens} "
          f"watermark={head.num_cached_tokens}/{head.num_tokens}")
```

**需要观察的现象**：前几个 prefill 步里，每步 `sched` 依次为 32、32、32、4，水位 `0→32→64→96→100`；第 4 步水位追平 `num_tokens` 后序列晋升 RUNNING，随后进入 decode。

**预期结果**：100 = 32 + 32 + 32 + 4，正好 4 段。本仿真纯 CPU、确定性可复现（待本地验证输出与上述一致）。

#### 4.1.5 小练习与答案

**练习 1**：把 `max_num_batched_tokens` 改成 70，100 token 的 prompt 会被切成几段、各多少？

**答案**：两段，70 + 30。第一段 `min(100, 70) = 70`；第二段待算 `100 − 70 = 30`。

**练习 2**：为什么正在分块的序列不会被其它 waiting 序列「插队」挤掉队首？

**答案**：序列在被完全算完前不会从 waiting 移除（L48-L51 的晋升条件未满足），`seq = self.waiting[0]` 每轮仍取到它；新请求是 `append` 到队尾。只有 `preempt` 用 `appendleft` 才会把恢复的序列插到队首（见 4.2）。

---

### 4.2 Scheduler.preempt：KV 不足时的抢占与恢复

#### 4.2.1 概念说明

decode 阶段每条序列每步产 1 个 token，token 越多，KV Cache 占的物理块越多。当所有 running 序列加起来把 KV 块用光、而某条序列下一步又要跨进一个新块时，调度器必须腾地方。nano-vllm 的做法是 **preempt（抢占）**：挑一条 running 序列，把它的物理块全部归还，让它退回 `WAITING`，等显存宽裕时重新 prefill。

注意 nano-vllm 是**重算式抢占**（recompute）：归还块时直接丢弃该序列已算的 KV，恢复后要把 prompt + 已生成 token 全部重算一遍。它没有像完整版 vLLM 那样把 KV 换出到 CPU 内存（swap），因为这会引入复杂的 CPU↔GPU 拷贝与额外状态机，与 nano-vllm「极简」的定位相悖。代价是被抢占序列的进度损失较大，好处是代码极简。

#### 4.2.2 核心流程

decode 分支里调用抢占的循环（`schedule` 的 decode 部分）：

```
当 running 非空 且 本步调度数 < max_num_seqs:
    seq = running.popleft()              # 取队首 running 序列
    只要 can_append(seq) 为假:           # 它需要新块却没有空闲块
        若 running 还有别的序列:
            preempt(running.pop())       # 抢占队尾(最后进来)的那条
        否则:
            preempt(seq); break          # 只剩自己，抢自己后退出
    否则(can_append 为真):
        调度 seq 本步 1 个 token, may_append 按需申请新块
把本步调度的序列按原序放回 running 队首
```

`preempt` 本身做四件事：状态置 `WAITING`、`is_prefill` 置 `True`（恢复时要重做 prefill）、`deallocate` 归还所有块、`appendleft` 插回 waiting 队首（高优先级，尽快恢复）。受害者选择是 `running.pop()`，即**队尾**——最后进入 running 的序列最先被抢，先来的「老」序列受到保护（它们通常更接近生成完毕，重算代价更高）。

#### 4.2.3 源码精读

decode 分支与抢占循环见 [nanovllm/engine/scheduler.py:57-73](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L57-L73)：

- **L59** `seq = self.running.popleft()`：从 running 队首取一条来 decode。
- **L60-L65**：`while not self.block_manager.can_append(seq)`——只要这条序列跨块需要新块却拿不到，就持续抢占。**L62** 抢队尾 `self.running.pop()`；若 running 空了（只剩 seq 自己），**L64** 抢 seq 自己并 `break`。每次 `preempt` 都会 `deallocate` 释放块，腾出空间后 `can_append` 转为真，循环退出。
- **L66-L70**（`while` 的 `else`）：循环条件为假（即 `can_append` 为真）时执行——本步调度 1 个 token，`may_append` 真正申请新块，`is_prefill=False`。
- **L71** `assert scheduled_seqs`：保证每个 decode step 至少调度一条，否则说明连唯一一条序列都腾不出空间（退化配置），程序主动报错。
- **L72** `self.running.extendleft(reversed(scheduled_seqs))`：把本步调度过的序列按原顺序重新放回 running 队首，保持 FIFO 顺序。

`preempt` 实现见 [nanovllm/engine/scheduler.py:75-79](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L75-L79)：

```python
def preempt(self, seq: Sequence):
    seq.status = SequenceStatus.WAITING
    seq.is_prefill = True
    self.block_manager.deallocate(seq)
    self.waiting.appendleft(seq)
```

注意 `deallocate` 只清 `block_table` 和 `num_cached_tokens`（见 [nanovllm/engine/block_manager.py:94-101](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L94-L101)），**不动** `token_ids` 与 `num_tokens`。所以恢复时待算量 `num_tokens - num_cached_tokens(0)` = 整段（prompt + 已生成），需要全部重算——这正是「重算式抢占」的语义。

#### 4.2.4 代码实践

**目标**：制造显存压力，亲眼看到 decode 步里 running 队列瞬间缩水（即抢占发生）。把上一节的脚本改成 `observe_preempt.py`：

```python
from nanovllm.config import Config
from nanovllm.engine.scheduler import Scheduler
from nanovllm.engine.sequence import Sequence
from nanovllm.sampling_params import SamplingParams

MODEL = "~/huggingface/Qwen3-0.6B/"
cfg = Config(MODEL, num_kvcache_blocks=4)        # 只有 4 个块 = 1024 token KV，极度紧张
Sequence.block_size = cfg.kvcache_block_size
sched = Scheduler(cfg)

sp = SamplingParams(temperature=0.6, ignore_eos=True, max_tokens=20)
for i in range(6):
    sched.add(Sequence(token_ids=[1000 + i] * 256, sampling_params=sp))  # 每条恰好占 1 个块

step = 0
while not sched.is_finished() and step < 40:
    step += 1
    running_before = len(sched.running)
    seqs, is_prefill = sched.schedule()
    sched.postprocess(seqs, [1] * len(seqs), is_prefill)
    tag = "PREFILL" if is_prefill else "DECODE "
    print(f"step{step:2d} {tag} waiting={len(sched.waiting)} "
          f"running={len(sched.running)} free={len(sched.block_manager.free_block_ids)} "
          f"sched={len(seqs)}")
```

**需要观察的现象**：

- **step 1 PREFILL**：4 个块被 0–3 号占满并晋升 RUNNING，4、5 号进不来 → `waiting=2, running=4`。
- **step 2 DECODE**：`running` 从 4 掉到 2、`waiting` 从 2 涨到 4，`free` 在该步内一度为 0——这就是抢占。被抢的是队尾的 3 号、2 号，它们被退回 waiting 队首。

**预期结果**：任意 DECODE 步里 `len(sched.running) < running_before`，就说明该步触发了 `preempt`。因为 decode 分支只消费 running（`popleft` 与 `pop`）、从不往 running 加新序列，所以 running 在 decode 步只可能因抢占而缩短——这是从外部判定抢占的可靠信号。本仿真确定性可复现（待本地验证）。

> 想在真实 GPU 上触发，等价做法是：用 `bench.py`，通过 `LLM(path, gpu_memory_utilization=0.3, max_num_seqs=512)` 把 KV 块预算压低，再在 `preempt` 里临时加一行 `print` 确认。是否触发取决于显卡显存。

#### 4.2.5 小练习与答案

**练习 1**：被抢占的序列恢复后，要从哪个 token 开始重算？

**答案**：从第 0 个 token 开始全部重算。因为 `deallocate` 把 `num_cached_tokens` 清零、`block_table` 清空，恢复时待算量 = `num_tokens - 0` = 整条序列长度（prompt + 已生成部分）。

**练习 2**：为什么抢占受害者用 `running.pop()`（队尾）而不是 `running.popleft()`（队首）？

**答案**：保护先来的序列。先进入 running 的序列通常已 decode 很多步、更接近 `max_tokens` 完成；抢队尾（最新进来的）能减小整体重算代价，避免「快完成的序列被反复打断」。

---

### 4.3 BlockManager.can_append：decode 跨块时的显存检查

#### 4.3.1 概念说明

decode 时序列每步只长 1 个 token，但这个 token 的 K/V 要写入 KV Cache 的具体「块内槽位」。当新 token 落在一个**尚未分配的新块的开头**时，就需要再申请一个物理块；否则塞进已分配块的空槽即可。`can_append` 就是回答「这一步要不要新块、要的话有没有空闲块」。

#### 4.3.2 核心流程

判定只需一行。是否需要新块由下式给出：

\[
\text{need\_new\_block} = \big(\text{len(seq)} \bmod \text{block\_size}\big) == 1
\]

即当序列当前长度对 `block_size` 取模等于 1 时，本步写入正好落到一个新块的起点，需要新块。`can_append` 返回 `len(free_block_ids) >= need_new_block`：要新块时必须有 ≥1 个空闲块，不要新块时恒为真。

#### 4.3.3 源码精读

```python
def can_append(self, seq: Sequence) -> bool:
    return len(self.free_block_ids) >= (len(seq) % self.block_size == 1)
```

见 [nanovllm/engine/block_manager.py:103-104](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L103-L104)。

- `len(seq)` 返回 `seq.num_tokens`（见 [nanovllm/engine/sequence.py:33-34](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L33-L34)），即当前 token 总数。
- `(len(seq) % self.block_size == 1)` 是一个 0/1 布尔值：为 1 表示本步 decode 要写入的槽位恰好跨进新块。

为什么是 `== 1` 而不是 `== 0`？因为 `len(seq)` 在 `can_append` 被调用时，已经包含了「上一步 postprocess 刚 append 的那个 token」，而本步要写入 KV 的正是这个最新 token 的 K/V。所以「长度对 block_size 取模为 1」等价于「刚跨过 block_size 的整数倍边界、最新 token 落在新块的第 0 槽」。`may_append`（[nanovllm/engine/block_manager.py:106-108](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L106-L108)）用**同一个条件**真正分配块，二者保持一致。

举几个数（`block_size = 256`）：

| num_tokens | num_tokens % 256 | 需要新块？ | 说明 |
|------------|------------------|-----------|------|
| 257 | 1 | 是 | 第 257 个 token 落在第 1 块第 0 槽，prefill 只分配了第 0 块 |
| 300 | 44 | 否 | 写进第 1 块第 43 槽，已分配 |
| 513 | 1 | 是 | 落在第 2 块第 0 槽 |

#### 4.3.4 代码实践

**目标**：直接实例化 `BlockManager`，验证 `can_append` 在不同序列长度下的真假，理解跨块时机。

```python
from nanovllm.engine.block_manager import BlockManager

bm = BlockManager(num_blocks=8, block_size=256)

class FakeSeq:                   # 只具备 can_append 所需的 __len__ 接口
    def __init__(self, n): self.num_tokens = n
    def __len__(self): return self.num_tokens

for n in [255, 256, 257, 300, 512, 513]:
    need = (n % 256 == 1)
    print(f"len={n:3d}  need_new_block={need}  can_append={bm.can_append(FakeSeq(n))}")
```

**需要观察的现象**：`need_new_block` 仅在 257、513 这类「长度 mod 256 == 1」的位置为 True，其余为 False；free 充足时 `can_append` 全为 True。

**预期结果**：`need` 列在 257、513 处为 True，其余 False。可顺手把 `num_blocks` 改成 0 复测——此时 257 处 `can_append` 变 False，即「要新块但没有空闲块」，正是 4.2 触发抢占的前提（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`block_size=256`，一条序列 decode 中 `num_tokens` 依次为 256、257、258。哪一步 `can_append` 会要求新块？

**答案**：`num_tokens=257` 这一步。257%256=1，最新 token 写入第 1 块第 0 槽，需要新块；256（0）和 258（2）都不需要。

**练习 2**：`can_append` 返回 False 时，`schedule` 的 decode 分支会做什么？

**答案**：进入 `while not can_append(seq)` 循环，`preempt` 一条队尾 running 序列释放块，再重测 `can_append`；若 running 已空则 `preempt(seq)` 自己并 `break`（见 4.2.3）。

## 5. 综合实践

把两种机制串起来：让一条长 prompt 被分块 prefill 的同时，让多条 decode 序列互相挤压触发抢占，绘制完整的 waiting/running 时序。

在仿真脚本里**同时**设两个旋钮：`max_num_batched_tokens=64`（强制 prefill 分块）+ `num_kvcache_blocks=4`（强制 decode 抢占）。加入 1 条 300 token 的长 prompt 和 4 条 256 token 的短 prompt，`ignore_eos=True`、`max_tokens=15`。逐步打印每步的 phase、`waiting/running/free`，以及每条调度序列的 `num_scheduled_tokens` 与 `num_cached_tokens`。

任务要求：

1. 指出哪些 step 是「长 prompt 在分块 prefill」（`num_scheduled_tokens < 待算`、`num_cached_tokens` 逐步追赶 `num_tokens`）。
2. 指出哪些 DECODE step 发生了抢占（`running` 计数下降）。
3. 解释被抢占的序列回到 waiting 后，下一个 step 它为何走 prefill 分支而不是 decode 分支（提示：`preempt` 把 `is_prefill` 置 True、且 waiting 非空时 `schedule` 优先 prefill）。

**预期**：你会看到 prefill 分块与 decode 抢占在同一份日志里交替出现，从而直观理解「prefill 优先 + 分块 + 抢占」三者如何协同。后续长期时序会因前缀缓存命中与反复抢占而变复杂，重点关注**首次**分块与**首次**抢占两个事件即可（待本地验证具体时序）。

## 6. 本讲小结

- **chunked prefill** 由 `max_num_batched_tokens` 不足触发；本步实际算的 token = `min(num_tokens − 已算水位, remaining)`，分块序列卡在 waiting 队首续算，算完才晋升 RUNNING。
- **「只允许首条分块」** 由 `if remaining < num_tokens and scheduled_seqs: break` 守卫，保证每个 step 至多一条半截 prefill 序列，简化 varlen 打包。
- **preempt 是重算式抢占**：归还物理块、置 `WAITING` 与 `is_prefill=True`、`appendleft` 插回 waiting 队首；受害者选队尾，保护老序列。
- **decode 跨块检查就一行**：`can_append` 在 `len(seq) % block_size == 1` 时要求空闲块，否则恒真；返回 False 即触发抢占。
- **恢复后的序列** 因 `is_prefill=True` 且 waiting 非空，会重新走 prefill 分支，把 prompt + 已生成部分整体重算。
- 这两段逻辑共同保证调度器在「请求很大」「显存很紧」两个极端下都不卡死。

## 7. 下一步学习建议

- **u3-l1（BlockManager 块管理）**：讲清 `allocate`/`deallocate`/引用计数，本讲的 `can_append`/`may_append`/`preempt` 都建立在它之上。
- **u3-l2（Prefix Caching）**：讲清 `can_allocate` 返回的命中块数如何得来，本讲 prefill 分支里「扣掉 `num_cached_blocks * block_size`」正依赖它。
- **u3-l3（KV Cache 显存预算）**：讲清 `num_kvcache_blocks` 这个本讲反复调小的旋钮，是怎么由 `gpu_memory_utilization` 算出来的。
- 若想看 `schedule` 产出的 seqs 如何被实际执行，进入 **u4-l1（ModelRunner 输入准备）**。
