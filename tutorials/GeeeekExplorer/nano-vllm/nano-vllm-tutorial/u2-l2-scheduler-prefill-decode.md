# Scheduler：prefill 与 decode 调度

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `Scheduler.schedule()` 返回值的含义，以及「prefill 优先于 decode」这条策略是如何用代码体现的。
- 解释 `max_num_seqs` 与 `max_num_batched_tokens` 这两个约束分别在 prefill 和 decode 阶段起什么作用。
- 读懂分块 prefill（chunked prefill）「只对首条序列切片」的设计，并能手算出每一步调度了哪些序列、各调度了多少 token。
- 理解 `postprocess()` 如何把采样到的 token 写回序列、推进「已计算」水位，并在合适的时候把序列标记为 `FINISHED`。

本讲是上一讲「Sequence 生命周期」的延续：上一讲讲了序列「长什么样」，本讲讲序列「被谁、按什么规则推着走」。

## 2. 前置知识

在进入源码前，先用大白话把几个关键概念对齐。

- **prefill 与 decode**：大模型生成文本分两个阶段。prefill 阶段把整条 prompt（用户输入）一次性喂给模型，算出每一个位置的 KV（Key/Value），并预测第一个新 token；decode 阶段则是每次只把上一步生成的 1 个 token 喂进去，再预测下一个 token，如此循环。prefill 是「算一大批」，decode 是「一次一个」。
- **token 与 batch**：这里把「一个序列里的一个位置」叫一个 token。一个 step（一步）里可以同时处理多条序列，本步所有序列的 token 数加起来叫「本步的 batched tokens」。
- **KV Cache 与 block**：模型每算一个 token 都会产生 K/V，存起来给后续位置复用，这就是 KV Cache。nano-vllm 把 KV Cache 切成固定大小的「块（block）」来管理（详见 u3 单元）。调度器在调度时需要关心「块够不够分」。
- **两队列模型**：调度器内部维护两个队列。`waiting` 存「还没开始 prefill 或没 prefill 完」的序列；`running` 存「已经 prefill 完、正在 decode」的序列。
- **Sequence 的计数**（上一讲）：`num_tokens`（当前总长度）、`num_cached_tokens`（已经算过 KV 的进度水位）、`num_scheduled_tokens`（本步要新算多少 token）。本讲会反复用到这三个字段。

## 3. 本讲源码地图

本讲主要围绕一个文件展开，并少量引用它的协作者：

| 文件 | 作用 |
| --- | --- |
| [nanovllm/engine/scheduler.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py) | 调度器本体。`schedule()` 决策、`postprocess()` 写回、`preempt()` 抢占都在这里。 |
| [nanovllm/engine/llm_engine.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py) | 引擎主循环 `step()`，串起 `schedule → run → postprocess`。 |
| [nanovllm/config.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py) | 提供 `max_num_seqs`、`max_num_batched_tokens` 等约束参数。 |
| [nanovllm/engine/block_manager.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py) | 块管理器，提供 `can_allocate / allocate / can_append / may_append / hash_blocks`，被调度器调用。 |
| [nanovllm/engine/sequence.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py) | `Sequence` 数据结构，提供 `append_token`、`num_completion_tokens` 等。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先看调度器的「两队列模型与构造」，再分别精读 `schedule()` 的 prefill 分支、decode 分支（含抢占），最后看 `postprocess()` 的写回与终止判定。

### 4.1 调度器的两队列模型与构造

#### 4.1.1 概念说明

调度器是引擎的「决策大脑」。模型前向计算很贵，所以每一步必须先决定：

1. 这一步跑哪些序列？
2. 这一步是 prefill 还是 decode？
3. 每条序列本步算多少 token？

nano-vllm 用一个非常直观的模型来回答这些问题：**两个队列 + 一条优先级规则**。

- `waiting` 队列：装着等待 prefill 的序列（刚进来的新请求，或被抢占退回来的请求）。
- `running` 队列：装着已经 prefill 完、正在逐 token decode 的序列。
- 优先级规则：**只要 waiting 里还有能调度的序列，就优先做 prefill**；只有 waiting 空了，才去处理 running 做 decode。

这套规则的好处是：新请求不会被正在 decode 的长请求饿死——只要有新请求，引擎会立刻先把它的 prompt 算完。

调度器还受两个容量上限约束（来自 `Config`）：

- `max_num_seqs`：一步里最多同时处理多少条序列。
- `max_num_batched_tokens`：一步里所有序列的 token 数之和不能超过这个值（主要约束 prefill，因为 prefill 一次吃很多 token）。

#### 4.1.2 核心流程

调度器对外暴露的调用节奏是这样的（被 `LLMEngine.step()` 驱动）：

```text
add(seq)            # 新请求入队，进 waiting 尾部
  ↓
schedule()          # 决策：返回 (本步要跑的序列列表, 是否是 prefill)
  ↓
model_runner.run()  # 真正算（不在本讲范围）
  ↓
postprocess()       # 把算出的 token 写回序列，判定是否结束
```

辅助方法：

- `is_finished()`：waiting 和 running 都空了，引擎主循环就停止。
- `add(seq)`：把序列追加到 waiting 尾部（FIFO）。

#### 4.1.3 源码精读

构造函数把 Config 里的约束读进来，并初始化两个队列与块管理器：

[Scheduler.__init__:L10-L17](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L10-L17) —— 读取 `max_num_seqs`、`max_num_batched_tokens`、`eos`、`block_size`，新建 `BlockManager`，并创建 `waiting` / `running` 两个空 `deque`。

两个辅助方法很短：

[is_finished 与 add:L19-L23](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L19-L23) —— `is_finished` 判据是「两个队列都空」；`add` 把序列 `append` 到 `waiting` 尾部。

约束参数的默认值在 Config 里：

[max_num_batched_tokens / max_num_seqs:L9-L10](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L9-L10) —— 默认每步最多 16384 个 token、最多 512 条序列。这两个值会在「综合实践」里被我们改小来放大调度细节。

> 小提示：`waiting` 和 `running` 都是 `collections.deque`，因为它两端进出都是 O(1)。调度器会频繁从队首取、队首放回，`deque` 比 `list` 合适。

#### 4.1.4 代码实践

**实践目标**：在不跑模型的前提下，建立「调度器 = 两队列 + 约束参数」的直觉。

**操作步骤**：

1. 打开 [config.py:L9-L18](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L9-L18)，记下 `max_num_batched_tokens=16384`、`max_num_seqs=512`、`kvcache_block_size=256`。
2. 打开 [scheduler.py:L10-L23](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L10-L23)，对照确认 `__init__` 把这几个值存成了实例属性。

**需要观察的现象**：调度器自己「不持有任何模型对象」，它只持有队列和块管理器——也就是说，调度决策与模型计算是完全解耦的。

**预期结果**：你能用一句话向别人解释「调度器构造时准备了哪两个队列、受哪两个数约束」。

#### 4.1.5 小练习与答案

**练习 1**：如果一个请求刚通过 `add()` 进入调度器，它会被放进哪个队列？状态是什么？
**答案**：进入 `waiting` 队列尾部；状态是 `WAITING`（在 `Sequence.__init__` 中设定）。

**练习 2**：`is_finished()` 何时返回 `True`？只用 `running` 空来判断行不行？
**答案**：当 `waiting` 和 `running` 都为空时返回 `True`。不行——如果有请求还在 `waiting`（比如显存不足一直没调度上），仅看 `running` 空会误判为结束。

---

### 4.2 schedule() 的 prefill 分支：prefill 优先与 token 预算

#### 4.2.1 概念说明

`schedule()` 是本讲的主角。它返回一个元组 `(scheduled_seqs, is_prefill)`：

- `scheduled_seqs`：本步要送进模型的序列列表。
- `is_prefill`：布尔值，标记本步是 prefill 还是 decode。

prefill 分支的核心是一条「**装填循环**」：从 `waiting` 队首开始，把序列一条条拿出来，尝试塞进本步的「token 预算筐」里，直到装不下或队列空了。

这里有两个精妙的设计要先点出来，否则代码会看不懂：

1. **前缀缓存复用**：如果一条序列的 prompt 前缀已经在缓存里（上一讲提到的 `num_cached_tokens`，本讲的 `can_allocate` 会返回命中块数），那么这些已缓存的 token **不需要再算一遍**，本步真正要算的 token 数会减去这部分。
2. **分块只给首条序列**：当 token 预算装不下当前序列时，如果它是本步的第一条（`scheduled_seqs` 为空），就允许「切片」——只算它前 `remaining` 个 token，剩下的留到后续 step；如果不是第一条，就直接停，把预算留给下次。

为什么要「只给首条切片」？为了避免「一条超长 prompt 把整步预算吃光、同时让其它短请求干等」的反向极端——只要还有别的序列能整条塞进去，就先塞整条的。

#### 4.2.2 核心流程

prefill 分支的伪代码：

```text
num_batched_tokens = 0
while waiting 非空 and 已调度数 < max_num_seqs:
    seq = waiting 队首
    remaining = max_num_batched_tokens - num_batched_tokens   # 剩余预算
    若 remaining == 0: break
    if seq 还没分配过块:                       # 全新序列
        num_cached_blocks = can_allocate(seq)  # 命中缓存的块数，-1 表示块不够
        若 num_cached_blocks == -1: break
        num_tokens = seq.num_tokens - num_cached_blocks * block_size   # 扣掉缓存
    else:                                       # 之前分块过、续算的序列
        num_tokens = seq.num_tokens - seq.num_cached_tokens
    若 remaining < num_tokens 且 scheduled_seqs 非空: break   # 非首条不切片
    若 seq 是全新的: allocate(seq, num_cached_blocks)         # 一次性分配所有块
    seq.num_scheduled_tokens = min(num_tokens, remaining)     # 本步实际算多少
    num_batched_tokens += seq.num_scheduled_tokens
    若 全部算完: 状态置 RUNNING，从 waiting 移到 running
    把 seq 加入 scheduled_seqs
若 scheduled_seqs 非空: return (scheduled_seqs, True)        # 本步是 prefill
# ……否则进入 decode 分支
```

注意一个关键事实：`allocate()` 会**一次性为整条序列分配所有需要的块**（从 `num_cached_blocks` 到 `seq.num_blocks`），见 [BlockManager.allocate:L75-L92](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L75-L92)。也就是说，「分块」分的是**计算量**（一次算多少 token），不是显存块——块在第一次 prefill 时就全分好了。这也是为什么续算分支（`else`）里不再调用 `allocate`。

#### 4.2.3 源码精读

[ schedule() 的 prefill 循环:L29-L52](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L29-L52) —— 这是本模块最核心的一段，逐行解读如下：

- L30：循环条件 `self.waiting and len(scheduled_seqs) < self.max_num_seqs`——队列非空且没超序列数上限。
- L32：`remaining` 是本步剩余 token 预算。
- L35-L39：全新序列（`not seq.block_table`）走 `can_allocate`。返回 `-1` 表示空闲块不够（L37-L38 直接 `break`）；否则用命中块数把要算的 token 数减下来。
- L40-L41：续算序列（已有 `block_table` 但没算完），要算的是「总长 − 已算水位」。
- L42-L43：**「非首条不切片」**规则。注意条件是 `remaining < num_tokens and scheduled_seqs`——只有当预算装不下 **且** 已经有别的序列在本步时才停。
- L46：`seq.num_scheduled_tokens = min(num_tokens, remaining)`——本步真正要算的 token 数，受预算封顶。这就是「切片」发生的地方。
- L48-L51：如果「已算水位 + 本步算的 == 总长」，说明 prefill 完成，状态转 `RUNNING`，从 `waiting` 弹出塞进 `running`。

[ prefill 完成则返回:L54-L55](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L54-L55) —— 只要本步调度到了至少一条 prefill 序列，就立刻返回 `is_prefill=True`，**不会**再去碰 decode。这就是「prefill 优先」的代码体现。

> 前缀缓存的细节（`can_allocate` 怎么算命中块数、`compute_hash` 怎么链式哈希）属于 u3-l2 的内容，本讲只需把它当成「返回命中块数或 -1」的黑盒。

#### 4.2.4 代码实践

**实践目标**：用一组小数字手算 prefill 调度，验证你对 token 预算与「首条切片」规则的理解。这一步纯纸笔即可，不需要 GPU。

**场景设定**：`max_num_batched_tokens = 8`（故意极小），`max_num_seqs = 512`，`block_size = 256`。三条请求同时进来（假设没有前缀缓存命中，空闲块充足）：

- A：prompt 长度 5
- B：prompt 长度 10
- C：prompt 长度 3

`waiting = [A, B, C]`（按进入顺序）。

**操作步骤**：按 L29-L52 的逻辑，一步步推演每个 step 调度了谁。

**需要观察的现象 / 预期结果**（待本地验证，以下为按源码推演的结果）：

| step | 类型 | 调度序列 | 各序列 `num_scheduled_tokens` | 说明 |
| --- | --- | --- | --- | --- |
| 1 | prefill | `[A]` | A=5 | A 用掉 5 预算；B 需要 10 但剩余 3 且 A 已在列 → 停 |
| 2 | prefill | `[B]` | B=8 | B 是首条，允许切片，只算 8/10；B 未算完，留在 waiting |
| 3 | prefill | `[B, C]` | B=2, C=3 | B 续算 2（剩 10−8=2），完成；剩余 6 够 C 整条算完 |
| 4 | decode | `[A, B, C]` | 各=1 | waiting 空，转 decode，三条各算 1 个 token |

**关键观察**：step 2 里 B 被切成了两段（8 + 2）才算完；这正是「首条序列允许分块」的体现。如果你把 `max_num_batched_tokens` 调到 10 以上，B 就能一步算完，表格会少一行。

> 注意：`num_scheduled_tokens` 是「本步新算的量」。step 2 里 B 算了 8，但 B 的 `num_tokens` 还是 10（prompt 没变），变的是 `num_cached_tokens` 从 0 涨到 8（在 `postprocess` 里推进，见 4.4）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `max_num_batched_tokens` 设得比最长 prompt 还大，分块 prefill 还会发生吗？
**答案**：不会。分块只在 `remaining < num_tokens` 时才有意义；预算够装下整条 prompt 时，`min(num_tokens, remaining) == num_tokens`，一步算完。

**练习 2**：`schedule()` 在 prefill 分支里，最多会返回多少条序列？受什么约束？
**答案**：受 `max_num_seqs` 与 `max_num_batched_tokens` 双重约束——既不能超过 `max_num_seqs` 条，所有序列的 `num_scheduled_tokens` 之和也不能超过 `max_num_batched_tokens`。

**练习 3**：为什么续算分支（`else`）里不调用 `allocate`？
**答案**：因为第一次 prefill 时 `allocate` 已经一次性为整条序列分好了全部块，续算只是补计算量，不需要再分块。

---

### 4.3 schedule() 的 decode 分支：显存检查与 preempt 抢占

#### 4.3.1 概念说明

只有当 prefill 分支一条序列都没调度到（`scheduled_seqs` 为空）时，才会进入 decode 分支。此时 `waiting` 已空，引擎专心为 `running` 里的序列逐 token 续命。

decode 的逻辑比 prefill 简单：每条 running 序列本步算 1 个 token。但它多了一个**显存检查**环节——因为 decode 每生成一个 token 就要往 KV Cache 写一份 K/V，**当某条序列正好填满一个 block、需要开新 block 时**，必须确保还有空闲块可用。

如果空闲块不够，引擎就要**抢占（preempt）**：把某些 running 序列「打回原形」——释放它的块、退回 waiting 队列、重置成 prefill 状态，腾出块给当前序列继续 decode。被抢占的序列之后要重新 prefill（代价较高，但总比崩掉好）。

抢占的顺序很关键：调度器从 `running` 的**队尾**（最新加入的序列）开始牺牲，而被抢占的序列放到 `waiting` 的**队首**（下一次 prefill 优先处理），尽量保护那些「已经 decode 了很久、快要结束」的老序列。

#### 4.3.2 核心流程

decode 分支伪代码：

```text
while running 非空 and 已调度数 < max_num_seqs:
    seq = running.popleft()        # 从队首取
    while not can_append(seq):     # 需要新块但没有空闲块
        if running 还有别的序列:
            preempt(running.pop()) # 牺牲队尾序列，腾块
        else:
            preempt(seq); break    # 只能牺牲自己，本步不 decode 它
    else:                          # while 正常结束（能 append）
        seq.num_scheduled_tokens = 1
        seq.is_prefill = False
        may_append(seq)            # 真正申请新块（如果需要）
        scheduled_seqs.append(seq)
running.extendleft(reversed(scheduled_seqs))   # 把这批按原序插回队首
return (scheduled_seqs, False)
```

这里有两个 Python 易错点要先讲清，否则代码会读不懂：

1. **`while ... else`**：Python 的 while 循环可以接 `else`，**当循环条件变为假而退出时**执行 `else`；如果是被 `break` 打断的，则**不执行** `else`。所以上面的 `else` 分支表示「`can_append(seq)` 终于成功为 True」。
2. **`can_append` 的返回值**：它是 `len(free_block_ids) >= (len(seq) % block_size == 1)`。右边的 `(len(seq) % block_size == 1)` 是个布尔值（True=1/False=0）。含义是：只有当序列长度正好「跨进新 block 的第一个 token」时才需要 1 个空闲块，否则不需要（返回恒真）。

#### 4.3.3 源码精读

[ decode 分支主体:L57-L73](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L57-L73) —— 解读：

- L58：循环条件同样是 `max_num_seqs` 封顶。
- L60-L65：`while not can_append(seq)` 处理块不足。L61-L62：还有别的 running 序列可牺牲时，`self.running.pop()` 从**队尾**取一条来 preempt；L63-L65：否则只能 preempt 自己并 `break`（此时 `else` 不执行，本条本步不 decode）。
- L67-L70：`else` 分支——成功拿到块（或本来就不需要），设 `num_scheduled_tokens=1`、`is_prefill=False`，调用 `may_append` 真正落账新块，加入本步列表。
- L71：`assert scheduled_seqs`——只要进了 decode 分支（说明 running 非空），至少能调度一条。
- L72：`self.running.extendleft(reversed(scheduled_seqs))` 把这批序列**按原顺序**插回队首。`extendleft` 本身会逆序插入，所以先 `reversed` 一次抵消，保证顺序稳定。

[preempt:L75-L79](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L75-L79) —— 抢占做了四件事：状态置回 `WAITING`、`is_prefill=True`（这样它会被当成 prefill 重算）、`deallocate` 释放所有块、`appendleft` 放到 waiting 队首。

配合看块管理器的两个方法：

[can_append:L103-L104](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L103-L104) —— 「是否需要新块」取决于 `len(seq) % block_size == 1`（序列长度刚跨进新块的第一个位置）。

[may_append:L106-L108](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L106-L108) —— 真正申请新块并追加到 `block_table`。

> 关于 `len(seq)`：`Sequence.__len__` 返回 `num_tokens`（见 u2-l1）。decode 时每步 `append_token` 后 `num_tokens` 会 +1，所以「跨进新块」的判断会随生成长度推进而周期性触发。

#### 4.3.4 代码实践

**实践目标**：把 `while...else` 与 `extendleft(reversed(...))` 这两个 Python 易错点彻底搞懂。

**操作步骤**：

1. 在 Python 里跑一段最小复现，理解 while-else：

   ```python
   # 示例代码（非项目源码）
   n = 0
   while n < 3:
       n += 1
   else:
       print("正常结束，执行 else")   # 会打印
   ```

   再把循环改成 `while True: ... break`，观察 `else` 是否还执行（不会）。

2. 在 Python 里验证 `extendleft` 的逆序行为：

   ```python
   # 示例代码（非项目源码）
   from collections import deque
   d = deque(["x"])
   batch = ["A", "B", "C"]
   d.extendleft(reversed(batch))
   print(list(d))   # ['A', 'B', 'C', 'x']
   ```

   试试不加 `reversed` 会得到什么（`['C','B','A','x']`），从而理解 L72 为什么必须 `reversed`。

**需要观察的现象**：确认你对「正常退出走 else、break 不走 else」「extendleft 逆序」的记忆。

**预期结果**：你能向别人解释 L72 那行若去掉 `reversed`，running 队列的顺序会怎样被打乱。

#### 4.3.5 小练习与答案

**练习 1**：`can_append` 里 `(len(seq) % block_size == 1)` 这个 `== 1` 是什么意思？为什么是 1 而不是 0？
**答案**：它判断序列长度是否「刚好跨进一个新 block 的第一个 token」。因为 decode 是先检查再 `append_token`，当 `len(seq)` 对 block_size 取模等于 1 时，意味着即将写入的位置落在一个全新的 block 上，所以需要提前申请一个空闲块。

**练习 2**：被 `preempt` 的序列，下次被调度时是 prefill 还是 decode？为什么？
**答案**：是 prefill。因为 `preempt` 把 `is_prefill` 置回 `True`、释放了块、状态置回 `WAITING` 并放进 `waiting` 队首，所以它要重新走 prefill 流程（好在有前缀缓存，重算成本可能比想象低，详见 u3-l2）。

**练习 3**：L72 的 `assert scheduled_seqs` 在什么情况下会失败？
**答案**：理论上当进入 decode 分支时 `running` 非空，至少能调度一条，所以断言应当成立。如果它失败，说明 running 里的所有序列（含自己）都被 preempt 了——这只有在显存极度紧张、连一条序列的块都凑不齐时才会发生，属于异常状态。

---

### 4.4 postprocess()：写回 token、推进水位与终止判定

#### 4.4.1 概念说明

`schedule()` 决策完、模型前向算完、采样拿到新 token 后，就轮到 `postprocess()` 收尾。它对每条本步参与计算的序列做三件事：

1. **登记哈希块**：把本步新填满的 block 登记进哈希表，供后续请求做前缀缓存命中（`hash_blocks`）。
2. **推进「已算」水位**：`num_cached_tokens += num_scheduled_tokens`，并清零 `num_scheduled_tokens`。
3. **判定终止并写回 token**：决定是否把采样到的 token 追加进序列、是否结束这条序列。

这里有个反直觉但很重要的分支：**在 prefill 阶段，如果序列还没 prefill 完，采到的 token 会被丢弃**。道理是：分块 prefill 时，本步算的「最后一个位置」并不是 prompt 真正的最后一个位置，它预测出的 token 没有意义。只有当 `num_cached_tokens` 追平 `num_tokens`（整条 prompt 都算完了），才把这一个 token 真正接上去，作为第一个生成 token。

终止条件有两个，满足任一即结束：

- 采到了 eos（结束符），且该请求没有设置 `ignore_eos`；
- 生成的 token 数达到了 `max_tokens` 上限。

#### 4.4.2 核心流程

`postprocess` 伪代码：

```text
for (seq, token_id) in zip(seqs, token_ids):
    block_manager.hash_blocks(seq)                       # 登记新块进哈希表
    seq.num_cached_tokens += seq.num_scheduled_tokens    # 推进已算水位
    seq.num_scheduled_tokens = 0
    if 是 prefill 且 还没算完 (num_cached_tokens < num_tokens):
        continue                                         # 丢弃这个无意义的 token
    seq.append_token(token_id)                           # 真正追加 token
    if (未忽略 eos 且 token_id == eos) 或 已达 max_tokens:
        状态置 FINISHED
        deallocate(seq)                                  # 释放块
        running.remove(seq)
```

需要说明 `is_prefill` 这个入参：它是 `schedule()` 返回的那个「本步是否 prefill」的布尔值，对所有序列一致。

#### 4.4.3 源码精读

[postprocess:L81-L92](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L81-L92) —— 逐行：

- L83：`hash_blocks` 把本步新写满的 block 登记进哈希表（前缀缓存的基础）。
- L84-L85：推进水位 `num_cached_tokens`、清零 `num_scheduled_tokens`。这两个字段是调度与写回之间的「接力棒」。
- L86-L87：**prefill 未完成则丢弃 token**。注意判断用 `seq.num_cached_tokens < seq.num_tokens`——刚刚 L84 已经把水位推进了，所以这里是在问「推进之后是否已经算完整条 prompt」。
- L88：`append_token` 真正把 token 追加进 `token_ids`，并更新 `last_token` 和 `num_tokens`，见 [Sequence.append_token:L67-L70](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L67-L70)。
- L89-L92：终止判定。`num_completion_tokens` 是「已生成 token 数」（`num_tokens - num_prompt_tokens`，见 [Sequence.num_completion_tokens:L43-L45](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L43-L45)）。命中则置 `FINISHED`、释放块、从 running 移除。

再回头看一下 `postprocess` 的调用处，理解它和 `schedule` 的配合：

[step() 三段式:L49-L55](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L49-L55) —— `step()` 严格按 `schedule() → model_runner.call("run", ...) → postprocess(...)` 三段执行。`num_tokens` 这一行（L51）用正负号区分阶段：prefill 取各序列 `num_scheduled_tokens` 之和（正），decode 取 `-len(seqs)`（负），供 `generate` 统计两类吞吐。

> 设计代价：分块 prefill 时，模型其实为「本步最后一个位置」算了 logits 并采了样，但 `postprocess` 把这个 token 丢了。这是一种以少量冗余计算换取代码简洁的取舍。

#### 4.4.4 代码实践

**实践目标**：理解 prefill 未完成时 `continue` 分支的作用，以及它如何与「分块 prefill」配合。

**操作步骤**：

1. 回顾 4.2 的手算表格。在 step 2，B 的 `num_scheduled_tokens=8`，但 `num_tokens=10`。
2. 推演 step 2 的 `postprocess` 对 B 做了什么：
   - `hash_blocks(B)`：B 还没填满任何 block（block_size=256，B 才 10 个 token），所以不登记（`hash_blocks` 内部 `start == end` 时直接 return，见 [hash_blocks:L110-L113](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L110-L113)）。
   - `num_cached_tokens`：0 → 8；`num_scheduled_tokens`：8 → 0。
   - `is_prefill` 为 True 且 `num_cached_tokens(8) < num_tokens(10)` → `continue`，B 的 token_ids 仍然是 10 个，**不追加**。
3. 再推演 step 3 的 `postprocess` 对 B：`num_cached_tokens` 8 → 10；`10 < 10` 为假，不再 continue，执行 `append_token`，B 终于得到第一个生成 token。

**需要观察的现象**：B 在 step 2 被采样出一个 token 但被丢弃，直到 step 3 prefill 完成才真正写入。

**预期结果**：你能解释「为什么 B 的 `num_completion_tokens` 在 step 2 之后仍然是 0」。

#### 4.4.5 小练习与答案

**练习 1**：一条序列在 prefill 完成的那一步，`postprocess` 会给它追加几个 token？
**答案**：追加 1 个。prefill 完成意味着 `num_cached_tokens == num_tokens`，`continue` 条件不成立，于是执行一次 `append_token`，写入本步采样到的第一个生成 token。

**练习 2**：如果一条请求设置了 `ignore_eos=True`，它什么时候结束？
**答案**：只有当 `num_completion_tokens == max_tokens` 时才结束（eos 被忽略，见 L89 的 `not seq.ignore_eos` 条件）。

**练习 3**：序列被标记 `FINISHED` 后，它的块会怎样？
**答案**：L91 调用 `deallocate(seq)` 释放所有块（引用计数减到 0 的块回归 free 池），并从 `running` 移除。这也是前缀缓存能复用这些块的前提（块内容仍可能留在哈希表里，详见 u3）。

---

## 5. 综合实践

**任务**：把 `max_num_batched_tokens` 设成一个很小的值，构造多个长短不一的请求，给 `schedule()` 加日志，画出每一步返回的序列列表与各自的 `num_scheduled_tokens`，亲眼看到 prefill 优先、分块 prefill 与 decode 切换的全过程。

**实践目标**：把本讲四个模块串起来——你会同时看到「两队列流转」「token 预算约束」「首条分块」「decode 阶段切换」。

**操作步骤**：

1. **给调度器加日志**。在 [scheduler.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py) 的 `schedule()` 两个 `return` 之前各加一行打印（这只是教学用的临时日志，不要提交）：

   ```python
   # 在 L55 之前（prefill return）
   print(f"[PREFILL ] step seqs={[(s.seq_id, s.num_scheduled_tokens) for s in scheduled_seqs]}")
   return scheduled_seqs, True

   # 在 L73 之前（decode return）
   print(f"[DECODE  ] step seqs={[(s.seq_id, s.num_scheduled_tokens) for s in scheduled_seqs]}")
   return scheduled_seqs, False
   ```

2. **改小预算**。在调用处把 `max_num_batched_tokens` 调小。最简单的办法是在 `example.py` 里构造引擎时传入：

   ```python
   # 示例代码：修改 example.py 的 LLM(...) 调用
   llm = LLM(model, max_num_batched_tokens=16, enforce_eager=True)
   ```

   `enforce_eager=True` 顺手关掉 CUDA Graph（u5-l1），避免 batch size 变化干扰观察。

3. **构造长短不一的请求**。把 example.py 的 prompts 改成几条长度差异明显的输入，并把 `max_tokens` 设小（比如 3），以便快速看到 decode 阶段：

   ```python
   # 示例代码
   prompts = ["你好", "请用三句话介绍一下中国历史上的唐朝", "一" * 200]   # 短 / 中 / 长
   sampling_params = SamplingParams(temperature=0.8, max_tokens=3)
   ```

4. **运行并记录**：`python example.py`，把每行 `[PREFILL]` / `[DECODE]` 日志按顺序抄下来。

**需要观察的现象**：

- 第一阶段全部是 `[PREFILL]`，且最长的 prompt 会因为 `max_num_batched_tokens=16` 被切成多步（同一 `seq_id` 连续出现多次，`num_scheduled_tokens` 逐步累加直到算完）。
- waiting 清空后，日志切换成 `[DECODE]`，每步每条序列 `num_scheduled_tokens=1`。
- 短请求先 decode 完（先从 `[DECODE]` 列表里消失），长请求继续。

**预期结果**：你会得到一张类似 4.2.4 手算表格、但来自真实运行的时序表。把它和你手算的对照，验证理解。

> 说明：本实践需要 GPU 与已下载的模型权重（沿用 u1-l1 的环境）。具体的 token 切分边界取决于你实际的 prompt 分词长度，所以数值结果标注为「待本地验证」——重点是观察「prefill 优先 → 长请求被分块 → 切换到 decode」这个节奏，而不是某个具体数字。

## 6. 本讲小结

- 调度器用 **waiting / running 两个队列** 组织所有请求，遵循「**prefill 优先于 decode**」——只要 waiting 有可调度序列就先做 prefill，否则才 decode。
- prefill 受 **`max_num_seqs`（条数）** 与 **`max_num_batched_tokens`（token 总量）** 双重约束；当预算装不下某条序列时，**只允许本步第一条序列分块**（`remaining < num_tokens and scheduled_seqs` 这个判断）。
- decode 每条 running 序列每步算 1 个 token；写 KV 前用 `can_append` 检查是否需要新块，块不足时通过 **`preempt`** 牺牲队尾序列、退回 waiting 重做 prefill。
- `postprocess` 三件事：登记哈希块、推进 `num_cached_tokens` 水位、判定终止；**prefill 未完成时采到的 token 会被丢弃**，只有整条 prompt 算完才写入第一个生成 token。
- 序列在 `WAITING → RUNNING → FINISHED` 之间的迁移，全部由 `schedule()` 与 `postprocess()` 这两个方法驱动。

## 7. 下一步学习建议

本讲把「调度决策」讲透了，但故意把两个东西当黑盒用了：

1. **块是怎么分配、回收、命中缓存的**——`can_allocate / allocate / can_append / hash_blocks` 的内部机制。这是 u3 单元的核心，建议接着读 **u3-l1（BlockManager 块管理）** 和 **u3-l2（Prefix Caching 哈希匹配）**，搞清调度器反复调用的那些块管理方法到底在做什么。
2. **分块 prefill 与抢占的更极端场景**——当显存极度紧张时抢占会如何连锁触发。可以读 **u2-l3（Chunked prefill 与抢占机制）**，它专门讨论 `preempt` 与 `can_append` 在压力下的行为。

读完 u3 之后，再回到 u4 单元看「调度好的张量是怎么送进模型算的」，就能把从请求到输出的整条链路彻底打通。
