# 流式、全异步与部分回滚 rollout

## 1. 本讲目标

本讲面向「长尾样本」这一 RL rollout 的核心痛点：当同一批 prompt 的生成耗时差异巨大时，默认 rollout 循环会被最慢的样本拖垮。学完本讲你应当能够：

1. 说清 slime 默认 rollout 循环在「长尾」场景下的两个结构性瓶颈。
2. 区分三种高级数据流——**流式生成 `generate_streaming`**、**全异步 `generate_rollout_fully_async`**、**部分回滚 `partial_rollout`**——各自解决什么问题、改动哪一层、能否叠加。
3. 看懂 `AsyncRolloutWorker` 后台线程池与「跨 rollout 续传缓冲」的实现细节，并能据此为真实负载选型。

## 2. 前置知识

本讲承接 **u3-l2（默认 rollout 函数 `generate_rollout` 全流程）**，假定你已掌握：

- 默认 rollout 的「同步外壳 + 异步内核」结构，以及 `generate / generate_and_rm / generate_and_rm_group` 三层生成函数的分工。
- **过采样补数**：`target_data_size`、`remaining_batch_size`、`over_sampling_batch_size` 三个量如何协同凑齐 `rollout_batch_size` 组。
- **abort 机制**：凑够目标后调用 `abort()` 终止多余在途请求，引擎层 `abort_servers_until_idle` 排空 SGLang 队列。
- `Sample` 数据结构（尤其 `tokens` / `response_length` / `loss_mask` / `rollout_log_probs` / `Sample.Status` 状态机），以及 `RolloutDataSourceWithBuffer` 的「缓冲区 + `pop_first`」机制（见 u3-l3）。

补充两个本讲要反复用到的关键事实：

- **`Sample.append_response_tokens`** 是「增量写回」的唯一入口。它会同步维护 `tokens`、`response_length`、`loss_mask`、`rollout_log_probs` 的一致性；`trainable=True` 的 token 必须带 `log_probs`，`trainable=False` 的 token（工具/环境注入）自动补 0。它是理解「半成品如何被安全续写」的基础。
- **`GenerateState` 是单例**（`metaclass=SingletonMeta`）。`aborted` 标志、信号量 `semaphore`、采样参数都在这个全局单例里，跨函数共享。这一条决定了「流式/全异步」如何感知 abort。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [slime/rollout/sglang_streaming_rollout.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_streaming_rollout.py) | 流式生成函数 `generate_streaming`：用 SSE 流逐块写回 `Sample`，保证 abort 时半成品已落盘。 |
| [slime/rollout/fully_async_rollout.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/fully_async_rollout.py) | 全异步 rollout：进程级后台 worker 跨 rollout 边界维持固定并发池，解耦并发度与批量。 |
| [slime/rollout/data_source.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py) | `RolloutDataSourceWithBuffer`：partial rollout 跨轮续传的载体，`pop_first` 缓冲过滤器。 |
| [slime/rollout/sglang_rollout.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py) | 默认 rollout 主体：`abort()` 收集半成品、`generate_and_rm` 的 off-policy 掩码、`generate_rollout` 的回写胶水。 |

> 三者的层次关系一句话：**流式**是「单样本生成工位」的替换（最内层），**partial rollout** 是默认循环上的一个「开关 + 缓冲」增强（不动循环骨架），**全异步**是「整条 rollout 函数」的替换（最外层）。三者只在「流式 + partial」这一组合上有协同，全异步与另两者互斥。

## 4. 核心概念与源码讲解

### 4.1 流式生成 generate_streaming

#### 4.1.1 概念说明

默认 `generate` 向 SGLang `/generate` 发一次请求，**等待一个完整的 JSON 响应**。这在 abort 场景下有个隐患：如果一个权重更新或 partial-rollout 回收的 abort 在生成途中触发，请求被切断，要拿回已生成的文本只能依赖 SGLang 的 `/abort_request` 返回它已收集的内容——这条路径脆弱、不可控。

`generate_streaming` 把这一次 HTTP 调用改成 **SSE（Server-Sent Events）流**：服务器边生成边推 chunk，slime 每收到一个 chunk 就**立即把累计状态写回 `sample`**。于是当 abort 切断流时，已生成的半成品**已经稳稳落在 `sample` 上了**，不需要 `/abort_request` 往返。

它只是「最内层单样本生成工位」的替换，通过 `--custom-generate-function-path` 注入；外层的信号量、dp_rank 均衡、abort 编排、partial-rollout 缓冲交接**仍由 `sglang_rollout` 负责**。

> 一个易错点：SGLang 默认的流式输出是**累加式（cumulative）**的——服务端 `state.output_token_logprobs` 不断累积，每个 chunk 都引用「到目前为止的完整列表」。这一点直接决定了下面的实现写法。

#### 4.1.2 核心流程

1. 进入函数前先**快照调用前的 sample 基线状态**（`base_tokens`、`base_response`、`base_log_probs`、`base_loss_mask` 等）。
2. 向 `/generate` 发 POST，payload 带 `stream=True`、`return_logprob=True`。
3. 对每一行 `data: {...}`：
   - 解析 JSON chunk，取 `meta_info` 与累计的 `output_token_logprobs`（`item[0]` 是 logprob，`item[1]` 是 token id）。
   - **先把 sample 重置回基线**，再用 `append_response_tokens` 追加本次调用**全量**的 `call_tokens`。
   - 检查 `state.aborted`，若已 abort 则 `break`。
4. 流结束后：若是 abort 切断且没有 `finish_reason`，则 `sample.status = ABORTED`。

由于流是「调用内累加」，每次都「重置到基线 + 追加全量」，这样一次中断恰好让 sample 停在「最后观测到的那个 chunk 的边界」上，不多不少。

#### 4.1.3 源码精读

模块顶部 docstring 把「为什么这么做」讲得很清楚——核心收益在 abort 时半成品已就位：

[slime/rollout/sglang_streaming_rollout.py:L1-L25](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_streaming_rollout.py#L1-L25) —— 说明这是默认 `generate` 的 drop-in 替换，关键收益在 abort 时已把 tokens/响应文本/log-prob 落到 `sample` 上，不依赖 `/abort_request` 返回文本。

进入函数后，先把「调用前状态」逐一快照下来，作为每次重置的锚点：

[slime/rollout/sglang_streaming_rollout.py:L91-L101](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_streaming_rollout.py#L91-L101) —— 快照基线状态，注释点明「每次按 基线 + chunk 增量 重建」，保证中途断流时 sample 恰好停在最后观测的边界。

SSE 主循环里，关键三步是「解析累加 chunk → 重置到基线 → 追加全量 + 探测 abort」：

[slime/rollout/sglang_streaming_rollout.py:L116-L157](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_streaming_rollout.py#L116-L157) —— 逐行读取 `data:` 前缀的 SSE，解析累加式 `output_token_logprobs`；每个 chunk 都把 sample 重置回 base 再 `append_response_tokens` 追加全量 `call_tokens`，注释明说「外层 abort 一旦切断，留下来的就是目前已写入的状态」；末尾 `if state.aborted: break`。

最后判定终态——abort 切断且无 `finish_reason` 时标记为 `ABORTED`：

[slime/rollout/sglang_streaming_rollout.py:L162-L163](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_streaming_rollout.py#L162-L163) —— abort 且未正常结束则置 `Sample.Status.ABORTED`，供外层 partial-rollout 回收或全异步重排。

#### 4.1.4 代码实践

**目标**：通过阅读源码理解「重置到基线 + 追加全量」这一写法的必要性，而不是断章取义地「追加增量」。

**操作步骤**：

1. 假想 SGLang 服务端推送了三个累加 chunk，token 序列依次为 `[A]`、`[A,B]`、`[A,B,C]`。
2. 在纸上分别模拟两种写法在每个 chunk 后 `sample.tokens` 的值：
   - 写法甲（实际代码）：每个 chunk 先重置到 `base_tokens`，再 `append_response_tokens(call_tokens=[A,B,C], ...)`。
   - 写法乙（错误）：每个 chunk 直接 `append` 本 chunk 解析出的「新增」token。
3. 假设在第二个 chunk（`[A,B]`）之后 `state.aborted` 变真、第三 chunk 还没到，记录两种写法下 `sample.tokens` 最终值。

**需要观察的现象**：写法甲在 abort 时得到 `[base..., A, B]`（恰好在最后观测边界）；写法乙若误把累加 chunk 当增量处理，会得到 `[base..., A, A, B]`（重复）或漏 token。

**预期结果**：累加式流必须「重置 + 全量追加」才能保证边界正确性。

**待本地验证**：在带 SGLang 的环境跑 `examples/` 下任一示例并改用 `--custom-generate-function-path slime.rollout.sglang_streaming_rollout.generate_streaming`，对比日志中 abort 时 `sample.response` 是否为截断处的真实前缀。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接在 `generate_streaming` 里 `await post(...)` 一次性拿结果，而要开 SSE 流？

**参考答案**：一次性请求在 abort 时只能靠 `/abort_request` 返回已收集文本，路径脆弱；SSE 流每 chunk 都把累计状态写回 sample，abort 切断时半成品已就位，对 partial-rollout 回收至关重要。

**练习 2**：若某 chunk 的 `meta_info` 里没有 `output_token_logprobs` 字段，`call_tokens`/`call_log_probs` 会怎样？

**参考答案**：见 L132-L134，只有 `"output_token_logprobs" in meta` 时才覆盖 `call_tokens`/`call_log_probs`；否则保留上一 chunk 的值，避免空 chunk 冲掉已有累加状态。

---

### 4.2 全异步 generate_rollout_fully_async

#### 4.2.1 概念说明

默认 rollout 有两个结构性瓶颈：

1. **批量耦合并发**：一个 rollout 步骤内的在途任务数被「凑齐 `rollout_batch_size` 组」这一目标驱动，步与步之间队列冷启动。
2. **木桶效应**：`generate_rollout_async` 必须等凑够 `rollout_batch_size` 组**完整**结果才返回，最慢的那条样本卡住整个步骤边界——训练空等。

全异步 rollout 把并发度与批量**解耦**：一个**进程级后台 worker**（一个线程 + 一个 asyncio 事件循环）持续从 `data_buffer` 拉取 group、对每个 group 跑 `generate_and_rm_group`，维持一个**跨 rollout 边界的固定并发池**。每次 `generate_rollout` 调用只是去 worker 的输出队列里「取够 `rollout_batch_size` 组」就返回。于是下一轮训练不必等最慢的在途样本——它们在后台继续跑，产物落入输出队列，供**未来**某个 rollout 取用。

关键设计取舍（来自模块 docstring）：

- 它是 `--rollout-function-path` 级别的**整条函数替换**，但单样本逻辑仍可插拔——因为 worker 调的就是 slime 自带的 `generate_and_rm_group`，`--custom-generate-function-path` / `--custom-rm-path` 照常生效。
- 并发度取自 `args.sglang_server_concurrency * get_rollout_num_engines(args)`，与默认循环里每样本信号量上限对齐。
- worker **不感知** slime 高层的 pause/权重更新信号（如 `GenerateState.aborted`）；每条在途生成**自己**短路并把状态置为 `Sample.Status.ABORTED`，worker 唯一专属职责是**把含 ABORTED 的 group 重定向回 `data_buffer`**（而非送给训练），让下一轮（刷新过权重后）重新拾起。

> 重要区别（与 4.3 对比）：全异步对 ABORTED 轨迹**不保留**已生成进度，而是**整组重新入队、从头来过**（见 `examples/fully_async/README.md` 的 Limitations）。这与 partial-rollout「保留半成品续写」截然不同。

此外，全异步要求 `train_async.py` 驱动（预取下一轮 rollout）、**不支持 colocate**、**不支持 evaluation 模式**。

#### 4.2.2 核心流程

1. **首次调用**：`_get_global_worker` 创建进程级单例 `AsyncRolloutWorker`（线程 + asyncio 循环），跨多次 `generate_rollout` 调用共享，使输出队列保持「热」。
2. **后台 `_loop`**：`while running`——先回收已完成 task；只要 `len(active_tasks) < max_concurrent`，就从 `data_buffer.get_samples(1)` 拉 group 并 `asyncio.create_task(generate_and_rm_group(...))`；然后 `await asyncio.sleep(1)`。
3. **完成回调 `_make_done_cb(gid)`**：task 完成后——若结果里任一 sample 是 `ABORTED` → `data_buffer.add_samples([result])` 重排入缓冲；否则 `output_queue.put((gid, result))`。
4. **取数 `_generate_rollout_async`**：拿到全局 worker，`while len(collected) < target` 反复 `get_completed_groups()` 取已完成组；无产出则 `sleep(0.05)`；凑够后按 `sample.index` 排序、取前 `target` 返回。
5. **入口 `generate_rollout_fully_async`**：`evaluation` 为真则直接抛错，否则 `run(coro)`。

并发模型可用一个不等式概括：worker 让「在途任务池」始终维持在 `max_concurrent` 附近，而每次 `generate_rollout` 只取走 `target` 个成品——**生产（采样）与消费（训练）被输出队列解耦**。

#### 4.2.3 源码精读

模块 docstring 点明核心动机与「重排 ABORTED 而非送训练」这一专属职责：

[slime/rollout/fully_async_rollout.py:L1-L24](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/fully_async_rollout.py#L1-L24) —— 解耦 `max_concurrent_tasks` 与 `rollout_batch_size`；worker 不感知高层信号，唯一职责是把 ABORTED group 重定向回 `data_buffer`。

全局 worker 用线程锁保证只建一次，进程退出时 `atexit` 收尾：

[slime/rollout/fully_async_rollout.py:L53-L73](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/fully_async_rollout.py#L53-L73) —— `_get_global_worker` 单例化 worker（并发度 = `sglang_server_concurrency * 引擎数`），`atexit.register(_stop_global_worker)` 保证退出时停掉线程。

后台循环「回收 → 补满 → 睡 1 秒」三段式：

[slime/rollout/fully_async_rollout.py:L123-L154](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/fully_async_rollout.py#L123-L154) —— 先把已完成 task 从 `active_tasks` 移除并 `t.result()`（结果已在回调处理），再 `while len(active_tasks) < max_concurrent` 从 `data_buffer.get_samples(1)` 拉 group、`create_task(generate_and_rm_group(...))` 并挂回调，最后 `await asyncio.sleep(1)`。

完成回调是「ABORTED 重排 vs 正常入队」的分叉点：

[slime/rollout/fully_async_rollout.py:L169-L191](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/fully_async_rollout.py#L169-L191) —— 取 `done_task.result()`；若任一 sample 状态为 `ABORTED`，调 `data_buffer.add_samples([result])` 重排（注释明说「不送训练」），否则 `output_queue.put((gid, result))`。

每次 rollout 调用「排空输出队列直到凑够目标」：

[slime/rollout/fully_async_rollout.py:L211-L241](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/fully_async_rollout.py#L211-L241) —— `while len(collected) < target` 反复 `get_completed_groups()`；无产出 `sleep(0.05)`；凑够后按 `sample.index` 排序取前 `target` 返回（保证确定性与 slime 惯例一致）。

入口与 eval 守卫：

[slime/rollout/fully_async_rollout.py:L251-L256](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/fully_async_rollout.py#L251-L256) —— `generate_rollout_fully_async` 是 `--rollout-function-path` 入口；evaluation 模式直接抛错（与连续运行模型冲突）。

#### 4.2.4 代码实践

**目标**：理解「输出队列跨 rollout 保持热度」如何消除木桶效应。

**操作步骤**：

1. 阅读 `examples/fully_async/README.md` 的「Worker Internals」与「Limitations」两节。
2. 阅读 `run-qwen2.5-0.5B-fully_async.sh`，找出把默认管线切到全异步的两个关键开关（提示：驱动脚本与 `--rollout-function-path`）。
3. 假设 `rollout_batch_size=8`、`n_samples_per_prompt=4`、`sglang_server_concurrency=512`、引擎数=1。在手算「在途池上限」与「每轮取走数量」后，描述：若某轮有 1 条样本特别慢（远超其他），默认循环与全异步分别在「该轮训练开始时刻」上表现如何。

**需要观察的现象**：默认循环必须等那条慢样本完成（凑够 8 组）才能进训练；全异步则在该轮立刻取走已完成的 8 组进训练，慢样本留在后台池继续跑，其产物落入下一轮。

**预期结果**：全异步把「慢样本延迟」从「卡住当前步」摊到「顺延到后续步」，训练步间隔更平稳。

**待本地验证**：在 4×GPU 节点跑 `bash examples/fully_async/run-qwen2.5-0.5B-fully_async.sh`，观察日志中 `fully-async rollout N: collected X/Y, queue=Z` 的 `queue` 在 rollout 之间是否非零（证明队列跨轮保持热度）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 worker 用 `data_buffer.get_samples(1)`（每次只拉 1 组）而不是像默认循环那样一次拉 `over_sampling_batch_size` 组？

**参考答案**：worker 的目标是维持一个**固定大小的并发池**（`max_concurrent`），它按需「拉一组、补一个 task」即可，不需要一次性大批量拉取；批量拉取反而会让缓冲区瞬时被抽空、与「持续平滑生产」的初衷相悖。

**练习 2**：README 说「partial-rollout 式的 ABORTED 续写尚未接入」。结合源码，被重排的 group 下一次被取出时会发生什么？

**参考答案**：见 `_make_done_cb`，ABORTED group 经 `data_buffer.add_samples` 进入缓冲；下一轮 `get_samples` 经 `pop_first` 取出后，因 `Sample.status` 不是 `COMPLETED/TRUNCATED` 且 `response_length` 通常为 0（全异步未保留半成品），会**从头重新生成**，而非续写。

---

### 4.3 partial rollout buffer（跨轮续传）

#### 4.3.1 概念说明

对于**超长响应**（如长链推理、长代码），一条样本可能在一轮 rollout 的 abort 预算内**根本生成不完**。默认行为是 abort 时丢弃半成品——浪费已算的前缀。partial rollout 反其道而行：把**半成品样本回收进数据缓冲**，**下一轮 rollout 从断点处续写**，经过若干轮把一条长响应「拼接」完成。

它与 4.1、4.2 的层次关系：

- partial rollout **不替换** rollout 函数，只是默认循环上的一个开关（`--partial-rollout`）+ 缓冲机制。
- 它与 **流式（4.1）天然协同**：流式保证 abort 时半成品被可靠捕获到 `sample`，而这正是 partial rollout 回收「连贯半成品」的前提；二者常组合使用。
- 它与 **全异步（4.2）互斥**：全异步对 ABORTED 是「重排从头来」，不续写。

核心要处理的问题是**off-policy 失配**：被回收的前缀是「上一轮旧权重」下生成的，到了下一轮（权重已更新）它已偏离当前策略。slime 提供两种处理：

1. `--mask-offpolicy-in-partial-rollout`：把**已有前缀**的 `loss_mask` 全置 0，只让本轮新生成的 on-policy token 参与梯度。
2. 用 `--buffer-filter-path` 按 `rollout_id`（样本起始轮）对缓冲里的半成品排序/丢弃，避免半成品「陈酿」过久、失配过深。

#### 4.3.2 核心流程（跨 rollout 视角）

- **第 N 轮**：生成中途触发 abort（如时间/步数预算）。`abort()` 把已有一段 response 的样本打上 `metadata["start_rollout_id"] = N`（记录该半成品**起始**于哪一轮，供缓冲过滤器按「年龄」决策），收集进 `aborted_samples`。
- **回写**：`generate_rollout` 调 `data_source.add_samples(aborted_samples)`，把半成品组压入缓冲。
- **第 N+1 轮取数**：`RolloutDataSourceWithBuffer.get_samples` 是「**缓冲优先**」——`_get_samples_from_buffer` 经 `pop_first` 先弹出半成品组，**再**向数据集要新 prompt。于是半成品优先被续写。
- **续写**：`generate_and_rm` 发现 `response_length > 0`——若开了 `mask-offpolicy-in-partial-rollout`，先把已有 response 的 `loss_mask` 清零（标记为 off-policy 前缀），再继续 `append_response_tokens` 追加本轮新 token（`loss_mask=1`）。若仍未完成 → 再次 abort → 再次回收，如此反复。
- **完成**：状态变为 `COMPLETED` 后，该样本（完整 response + 混合新旧 `loss_mask`）流向训练；掩码开启时只有 on-policy 新 token 贡献梯度。

#### 4.3.3 源码精读

`abort()` 在排空在途任务时，按 `partial_rollout` 开关决定是否收集半成品并打标：

[slime/rollout/sglang_rollout.py:L350-L369](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L350-L369) —— `while state.pendings` 等所有在途任务结束；`if not args.partial_rollout: continue` 跳过收集；否则对每个已 done 的 group，凡 `sample.response` 非空且未打标的，写入 `metadata["start_rollout_id"] = rollout_id`，append 进 `aborted_samples`。

`generate_and_rm` 入口处的 off-policy 前缀掩码：

[slime/rollout/sglang_rollout.py:L229-L231](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L229-L231) —— 注释「mask previous off-policy generation for partial rollout」；若 `partial_rollout` 且 `mask_offpolicy_in_partial_rollout` 且已有 `response_length > 0`，把已有 response 段的 `loss_mask` 全置 0，确保只训练本轮 on-policy 新 token。

`generate_rollout` 把半成品回写进缓冲的胶水：

[slime/rollout/sglang_rollout.py:L637-L640](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L637-L640) —— `generate_rollout_async` 返回 `(output, aborted_samples)`；若 `aborted_samples` 非空，调 `data_source.add_samples(...)` 入缓冲，供下一轮续写。

缓冲的「先取缓冲、不足再取数据集」两段式：

[slime/rollout/data_source.py:L177-L196](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L177-L196) —— `get_samples` 先 `_get_samples_from_buffer(num_samples)`，扣减还需数量，不足再 `super().get_samples(...)` 向数据集要；`_get_samples_from_buffer` 调 `self.buffer_filter(args, None, self.buffer, num_samples)`。

默认缓冲过滤器 `pop_first`（FIFO，并就地删除已取）：

[slime/rollout/data_source.py:L225-L229](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L225-L229) —— `pop_first` 取 `min(len(buffer), num_samples)` 个、`del buffer[:num_to_pop]` 就地移除；它是 partial rollout 跨轮续传「先进先出」的默认策略，可用 `--buffer-filter-path` 替换。

`add_samples` 把半成品组压入缓冲（带 `n_samples_per_prompt` 长度断言）：

[slime/rollout/data_source.py:L198-L211](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L198-L211) —— 校验入参为 `list[list[Sample]]` 且每组长度等于 `n_samples_per_prompt`，逐组 `self.buffer.append(group)`。

> 注意：`metadata["start_rollout_id"]`（本模块，per-sample，记录半成品起始轮，供 buffer filter 用）与 `actor.py` / `placement_group.py` 里的 `start_rollout_id`（全局「从第几轮续训」的恢复点）**只是同名、语义不同**，不要混淆。

#### 4.3.4 代码实践

**目标**：在纸上追踪一条慢样本在 3 轮内的 `loss_mask` 演化，验证 off-policy 掩码的正确性。

**操作步骤**：

1. 设 `--partial-rollout --mask-offpolicy-in-partial-rollout`，单条样本需要 3 轮才能生成完。
2. 用一张表记录每轮结束时该样本的 `tokens`（response 部分）与对应 `loss_mask`：
   - 第 1 轮：生成 50 token 后 abort，全部为新 → `loss_mask = [1]*50`；回收入缓冲。
   - 第 2 轮：取出，先掩码旧 50 token（→ `[0]*50`），续写 30 token 新 → 末段 `[1]*30`；仍 abort，再回收。
   - 第 3 轮：取出，旧 80 token 全掩码（→ `[0]*80`），续写 20 token 新 → 末段 `[1]*20`；完成。
3. 检查最终送训练时，只有第 3 轮那 20 个 on-policy token 带 `loss_mask=1`。

**需要观察的现象**：每轮续写前，**已有** response 段被整体清零；只有「当前轮新增」段保留 `1`。

**预期结果**：跨 3 轮累计 100 token，但仅最后 20 token 参与梯度——off-policy 前缀被正确隔离。

**待本地验证**：真实运行需多卡 + 长响应配置；若无条件，可改为「源码阅读型实践」——在 `generate_and_rm`（L229-L231）与 `abort`（L354-L367）两处各加一行 `logger.info`，断点确认 `response_length` 与 `loss_mask` 的演化符合上表。

#### 4.3.5 小练习与答案

**练习 1**：若**不开** `--mask-offpolicy-in-partial-rollout`，跨轮续传的样本在训练时会有什么隐患？

**参考答案**：旧前缀是上一轮旧权重下生成的（off-policy），若不掩码，训练会对这些「行为策略」产出的 token 计算 policy gradient，引入 off-policy 偏差，削弱 on-policy RL 的正确性；掩码把这些 token 的梯度贡献清零。

**练习 2**：为什么 `buffer_filter` 的签名里要传入 `rollout_id`（即便 `pop_first` 没用到）？

**参考答案**：见 u3-l3——`rollout_id` 让自定义过滤器能按「半成品起始轮 vs 当前轮」的差距做策略（如优先消费 `start_rollout_id` 最老的、或丢弃陈酿过久的半成品），以控制 off-policy 失配深度；`pop_first` 只是默认的 FIFO 实现，不需要它。

**练习 3**：partial rollout 与全异步都把「未完成工作」导向缓冲，本质区别是什么？

**参考答案**：partial rollout**保留半成品已生成 token**并在下一轮**续写**（`response_length>0` 时追加）；全异步对 ABORTED group**不保留进度**、整组重排后**从头生成**。前者省算力但需处理 off-policy，后者实现简单但重算开销大。

---

## 5. 综合实践

**场景**：你正在训练一个会写长代码的 agent，同一批 prompt 的生成耗时差异极大——多数样本 30 秒完成，但有 5% 的「长尾」样本要 5 分钟。

**任务**：写一段选型说明（300 字左右），回答以下两个问题，并给出推荐配置。

1. **何时选全异步（`fully_async`）？** 结合 4.2 的「跨 rollout 并发池 + 输出队列解耦」，说明它如何把长尾延迟摊到后续步，并指出它的代价（不支持 colocate、不支持 eval、ABORTED 从头来、需 `train_async.py`）。
2. **何时选 partial rollout？** 结合 4.3 的「半成品回收续写」，说明它适合「单条样本本身就很长、一轮生成不完」的情况，并指出它与流式（4.1）的协同、以及 `--mask-offpolicy-in-partial-rollout` 的必要性。

**判断框架（填表后据此下结论）**：

| 维度 | 全异步 `fully_async` | partial rollout |
| --- | --- | --- |
| 痛点定位 | 长尾拖慢**步边界**（木桶效应） | 单条样本**一轮生成不完** |
| 改动层次 | 整条 rollout 函数（最外层） | 默认循环上的开关 + 缓冲 |
| 对半成品 | 重排、**从头来过** | 回收、**断点续写** |
| 驱动脚本 | 必须 `train_async.py`、禁 colocate | 用 `train.py` 即可 |
| off-policy 处理 | 靠刷新权重后重算 | 靠 `mask-offpolicy` 掩码旧前缀 |
| 与流式关系 | 互斥（自身不依赖半成品捕获） | **协同**（流式保证半成品可靠回收） |

**参考结论**：若长尾源于「任务难度差异导致少数样本生成特别久，但每条最终都能在一轮内完成」→ 选 **全异步**，让慢样本在后台池顺延、不卡训练步。若长尾源于「响应本身极长，单条一轮 abort 预算内生成不完」→ 选 **partial rollout**（并组合 `generate_streaming` + `--mask-offpolicy-in-partial-rollout`），把长响应跨轮拼接、只训练每轮新增的 on-policy 段。

> 提示：可阅读 `examples/fully_async/README.md` 的 Limitations 一节，确认全异步「ABORTED 不续写」的限制是否与你的负载兼容。

## 6. 本讲小结

- **流式 `generate_streaming`** 把单样本生成改成 SSE 流，每个累加 chunk 都「重置到基线 + 追加全量」写回 `sample`，使 abort 时半成品已可靠就位——它是 partial rollout 回收连贯半成品的前提。
- **全异步 `generate_rollout_fully_async`** 用进程级后台 worker 维持跨 rollout 边界的固定并发池，把并发度与批量解耦，靠输出队列让训练不必等最慢样本；ABORTED group 被重排、从头来过，且不支持 colocate/eval。
- **partial rollout** 是默认循环上的开关 + 缓冲机制：`abort()` 回收半成品并打 `start_rollout_id`，`add_samples` 入缓冲，下轮 `pop_first` 优先取出续写；`--mask-offpolicy-in-partial-rollout` 把旧前缀 `loss_mask` 清零以隔离 off-policy。
- 三者层次不同：流式最内（工位）、partial 居中（开关）、全异步最外（整函数）；只有「流式 + partial」协同，全异步与另两者互斥。
- 选型看痛点：长尾拖慢步边界 → 全异步；单条样本一轮生成不完 → partial rollout。

## 7. 下一步学习建议

- 若你想把「半成品续写」做得更智能（按年龄丢弃、按 rollout_id 衰减），复习 **u3-l3** 的 `buffer_filter` 契约，尝试写一个自定义 `--buffer-filter-path`。
- 全异步的「单样本生成」仍走 `generate_and_rm_group`，其奖励与生成分支见 **u3-l2**、**u3-l4**；若要在全异步下接入多轮 agent，参考 **u7-l2/u7-l3** 的 adapter 与 fan-out。
- 权重更新如何触发 `GenerateState.aborted`（全异步里每条生成自我短路的信号源），见 **u5-l1/u5-l3** 的 `update_weights` 与 SGLang 引擎封装。
