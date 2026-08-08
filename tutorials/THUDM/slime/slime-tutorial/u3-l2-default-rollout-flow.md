# 默认 rollout 函数 generate_rollout 全流程

## 1. 本讲目标

本讲拆解 slime 默认 rollout 函数 `generate_rollout`（位于 `slime/rollout/sglang_rollout.py`）的完整执行流程。读完本讲，你应当能够：

1. 说清楚「取 prompt → 调 SGLang 生成 → 算 logprob → 算奖励 → 返回训练样本」这条主链路在代码里对应哪些函数、按什么顺序执行。
2. 理解 `generate_rollout_async` 的主循环如何用「过采样 + 动态过滤」凑齐 `rollout_batch_size` 组样本。
3. 掌握三层生成函数 `generate` / `generate_and_rm` / `generate_and_rm_group` 的分工，以及 `group_rm` 与「逐样本奖励」两条路径的差异。
4. 认识 `abort` 机制如何优雅终止多余请求，以及它如何与 partial rollout 衔接、把半成品样本回收到数据缓冲区。

---

## 2. 前置知识

本讲建立在前面几讲已建立的概念之上，回顾三个关键点：

### 2.1 Sample 是流动的数据载体（u3-l1）

`Sample` 是贯穿 rollout 与 training 的核心数据结构。在 rollout 阶段，它经历这样的演化：

- 数据源取出时：只有 `prompt`、`tokens`（prompt 的 token）、`label`、`index` 等，`status=PENDING`。
- 生成阶段：通过 `Sample.append_response_tokens` 增量写入 `tokens`（拼上模型生成的新 token）、`response`（文本）、`loss_mask`（哪些 token 参与训练）、`rollout_log_probs`（行为策略对数概率，用于 off-policy 修正），并把 `status` 推进为 `COMPLETED`/`TRUNCATED`。
- 奖励阶段：写入 `reward` 字段。

理解这点很关键——本讲的几乎所有函数都在**修改 `Sample` 的这几个字段**。

### 2.2 rollout_manager 是调用方（u2-l3）

`generate_rollout` 不是被入口脚本 `train.py` 直接调用的，而是被 `RolloutManager.generate` 间接调用。编排层在初始化时把字符串参数解析成可调用对象：

[slime/ray/rollout.py:447-448](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L447-L448) —— 把 `--rollout-function-path`（默认值就是本讲的主角）通过 `load_function` 解析成 `self.generate_rollout`。

而该参数的默认值定义在参数中枢：

[slime/utils/arguments.py:318-320](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L318-L320) —— `default="slime.rollout.sglang_rollout.generate_rollout"`，即用户不显式指定时，slime 用的就是本讲要精读的这个函数。

真正的调用发生在 `_get_rollout_data`：

[slime/ray/rollout.py:650-652](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L650-L652) —— 通过 `call_rollout_fn` 调用，取回 `RolloutFnTrainOutput`，再 `.samples` 拿到 `list[list[Sample]]`。

### 2.3 同步外壳 + 异步内核的桥接

slime 的默认 rollout 是「同步外壳 + asyncio 异步内核」结构。`generate_rollout` 本身是普通同步函数，但内部用 `run(coro)` 把一个协程丢到后台事件循环线程里执行并阻塞等待结果：

[slime/utils/async_utils.py:34-36](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/async_utils.py#L34-L36) —— `run` 把协程调度到一个常驻后台线程的事件循环上，`run_forever` 永不退出，靠 `run_coroutine_threadsafe(...).result()` 阻塞取值。

为什么要异步？因为一次 rollout 要同时给成百上千个 prompt 并发地请求 SGLang 引擎，用 `asyncio.gather` 并发 + 信号量限流是最自然的写法。这一讲你会反复看到 `async/await`、`asyncio.create_task`、`asyncio.wait`、`asyncio.gather` 这些原语。

> 名词解释：**信号量（Semaphore）** 是一种并发限流器，构造时传入一个整数 N，表示「最多允许 N 个协程同时进入临界区」。每个协程进入前 `acquire`（计数 -1）、离开时 `release`（计数 +1），第 N+1 个会被挂起等待。本讲用 `async with semaphore:` 这个上下文管理器来自动 acquire/release。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `slime/rollout/sglang_rollout.py` | **本讲主角**。默认 rollout 函数及其全部辅助函数都在这里。 |
| `slime/rollout/base_types.py` | 定义 `RolloutFnTrainOutput` / `RolloutFnEvalOutput` 两个输出包装类与 `call_rollout_fn` 兼容层。 |
| `slime/rollout/rm_hub/__init__.py` | 奖励计算入口：`async_rm`（逐样本）、`batched_async_rm`（整组）。 |
| `slime/backends/sglang_utils/server_control.py` | abort 的底层实现：向 SGLang server 发 `/abort_request` 直到其请求队列清空。 |
| `slime/rollout/sample_hooks.py` | 生成后、奖励前的「样本钩子」机制（`apply_rollout_sample_hooks`）。 |
| `slime/utils/async_utils.py` | `run` 函数：同步↔异步的桥。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：① `generate_rollout` 同步入口与 `generate_rollout_async` 异步主循环；② 三层生成函数 `generate` / `generate_and_rm` / `generate_and_rm_group`；③ `abort` 机制与 partial rollout 衔接。

### 4.1 generate_rollout：同步入口与异步主循环

#### 4.1.1 概念说明

`generate_rollout` 是 slime 的「默认数据生成策略」：给定参数 `args`、本轮编号 `rollout_id`、数据源 `data_source`，它要产出**正好 `rollout_batch_size` 组样本**（每组含 `n_samples_per_prompt` 条 `Sample`），每组都已完成生成、奖励计算、状态标记。

它要解决两个现实问题：

1. **凑数问题**：RL 训练每轮需要固定批量的数据，但单次采样可能因为「动态过滤」（如 DAPO 丢弃全对/全错组）而不足，需要反复补采。
2. **并发问题**：成百上千个 prompt 必须并发请求 SGLang，又要给 SGLang 限流防止过载。

它的设计是「同步外壳 + 异步内核」：同步外壳 `generate_rollout` 负责按 `evaluation` 分流，异步内核 `generate_rollout_async` 负责真正干活。

#### 4.1.2 核心流程

`generate_rollout` 的整体走向（伪代码）：

```
def generate_rollout(args, rollout_id, data_source, evaluation=False):
    assert args.rollout_global_dataset
    if evaluation:
        return run(eval_rollout(args, rollout_id))          # 评估路径，本讲不展开
    output, aborted_samples = run(generate_rollout_async(args, rollout_id, data_source.get_samples))
    if aborted_samples:                                      # 有半成品（partial rollout 才会非空）
        data_source.add_samples(aborted_samples)             # 回收到数据缓冲区，下轮续训
    return output                                            # RolloutFnTrainOutput
```

`generate_rollout_async` 的核心是**一个双层 while 循环**：

```
target_data_size = rollout_batch_size
data = []
while len(data) < target_data_size:                          # 外层：还没凑够
    while remaining_batch_size < target_data_size:           # 内层：在途任务太少，再补
        samples = data_source(over_sampling_batch_size)      # 从缓冲区取若干 prompt 组
        submit_generate_tasks(samples)                       # 为每组创建 generate_and_rm_group 任务
    done, pendings = asyncio.wait(pendings, FIRST_COMPLETED) # 等任意一组完成
    for task in done:
        group = task.result()
        if dynamic_filter 判定丢弃: continue                 # 动态过滤（如丢全对组）
        if len(data) < target_data_size:
            data.append(group)                               # 收编一组
aborted_samples = abort(args, rollout_id)                    # 凑够了，终止多余的在途请求
return RolloutFnTrainOutput(samples=data, ...), aborted_samples
```

几个要点：

- **`data_source` 其实是 `data_source.get_samples` 方法**（一个 `Callable`），所以函数体内 `data_source(over_sampling_batch_size)` 就是在取数据。这点会在 4.1.3 结合源码确认。
- **过采样粒度**由 `over_sampling_batch_size` 控制：它决定每次「补采」取多少组 prompt。`target_data_size` 则是「凑够多少组」。后者对应 u1-l4 讲过的供需公式：\[ \text{组数} = \text{rollout\_batch\_size}, \quad \text{总样本数} = \text{rollout\_batch\_size} \times \text{n\_samples\_per\_prompt} \] 必须等于训练侧的 `global_batch_size × num_steps_per_rollout`。
- **`asyncio.wait(..., FIRST_COMPLETED)`** 而不是 `gather`：因为需要「边产出边收编边补采」，一组完成就立刻检查是否够数，不够就继续补采——这是流水线式调度，不能用一次性等全部完成的 `gather`。

#### 4.1.3 源码精读

**同步入口** `generate_rollout`：

[slime/rollout/sglang_rollout.py:618-641](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L618-L641) —— 这是函数定义本身。注意 L637 把 `data_source.get_samples` 作为第三个参数传进异步函数；L638-639 把 abort 收集到的 `aborted_samples` 通过 `data_source.add_samples` 回填给数据缓冲区。这条 `add_samples` 是 partial rollout 跨轮续传的关键（详见 4.3）。

**异步内核签名** `generate_rollout_async`：

[slime/rollout/sglang_rollout.py:372-386](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L372-L386) —— 注意第三个参数类型标注 `data_source: Callable[[int], list[list[Sample]]]`，证实「传进来的是 `get_samples` 可调用对象，而非数据源对象本身」；返回类型是 `tuple[RolloutFnTrainOutput, list[list[Sample]]]`，第二个元素是 abort 收集的半成品。

**目标批量与主循环**：

[slime/rollout/sglang_rollout.py:399-410](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L399-L410) —— `target_data_size = args.rollout_batch_size`（L399）；外层 while 控制总凑数，内层 while 在 `remaining_batch_size` 不足时调用 `data_source(args.over_sampling_batch_size)` 取数据并 `submit_generate_tasks` 提交。`remaining_batch_size` 是「在途未收回的任务组数」，由 `GenerateState` 维护。

> `over_sampling_batch_size` 的默认值见 [slime/utils/arguments.py:418-429](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L418-L429) 的 help：若为 `None` 则用 `rollout_batch_size` 作为默认，即「一次性取够」、不分批补采。

**收编与动态过滤**：

[slime/rollout/sglang_rollout.py:412-440](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L412-L440) —— L412 `asyncio.wait(..., FIRST_COMPLETED)` 流水线式回收；L423 `assert len(group) == args.n_samples_per_prompt` 校验每组样本数；L426-434 调用动态过滤器，若判定丢弃则 `remaining_batch_size -= 1` 并 `continue`（不收编这组）；L438-440 仅在还没凑够时 `data.append(group)`（防止凑够后多余完成的组被重复计入）。

> 名词解释：**动态过滤（dynamic filter）** 是 DAPO 风格采样策略。例如 `check_reward_nonzero_std` 会丢弃「一组里所有样本奖励相同」的组（全对或全错，组内方差为 0，无法提供 GRPO 学习信号）。本讲只关注它与主循环的交互（丢一组→补采一组），过滤器本身的判据见 u3-l5。

**收尾与返回**：

[slime/rollout/sglang_rollout.py:449-468](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L449-L468) —— L449 调 `abort` 终止多余请求；L451 断言最终凑够了；L452-455 按 `index` 排序保持稳定顺序；L458 `state.reset()` 清空状态防污染下一轮；L459-466 是可选的样本过滤器与「全样本处理器」钩子；L468 返回 `RolloutFnTrainOutput(samples=data, metrics=metric_gatherer.collect())`。

**`GenerateState` 单例**：主循环依赖的全局状态都收在一个单例里。

[slime/rollout/sglang_rollout.py:83-117](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L83-L117) —— 用 `metaclass=SingletonMeta` 保证整个进程只有一个实例（因为 tokenizer、processor、信号量都要复用）。L94 信号量 `sglang_server_concurrency × 引擎数` 限流并发请求数；L95-105 把 `--rollout-temperature/top_p/top_k/max_response_len/stop` 等参数打包成 `sampling_params` 字典（注意 L103 `no_stop_trim=True`、L104 `spaces_between_special_tokens=False` 两个非默认值）。

**`submit_generate_tasks`**：

[slime/rollout/sglang_rollout.py:136-149](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L136-L149) —— 遍历每个 prompt 组，为每组创建一个 `generate_and_rm_group` 异步任务塞进 `self.pendings`，并把组数累加到 `remaining_batch_size`。注意「一个 prompt 组 = 一个 asyncio task」，组内多个样本的并发交给 `generate_and_rm_group` 内部的 `gather`。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：理解过采样如何「补数」，弄清 `target_data_size`、`remaining_batch_size`、`over_sampling_batch_size` 三者的关系。

**操作步骤**：

1. 打开 `slime/rollout/sglang_rollout.py`，定位 `generate_rollout_async`（L372）。
2. 在 L399、L406、L149、L433 这几行分别打上断点（或在脑中标注），跟踪以下三个变量的变化：`target_data_size`、`state.remaining_batch_size`、`len(data)`。
3. 假设 `rollout_batch_size=8`、`over_sampling_batch_size=4`、`n_samples_per_prompt=4`，且每次取 4 组回来后总有 1 组被动态过滤丢弃，手工模拟前 3 轮外层循环。

**需要观察的现象**：

- 第 1 次内层循环：`remaining_batch_size` 从 0 → 提交 4 组 → 4。
- 随着任务完成，`remaining_batch_size` 递减；只要它 `< 8`，内层 while 就会再取 `(4)` 组补上。
- `data` 增长到 8 时外层 while 退出。

**预期结果**：你能口算出「最终 `len(data) == 8 == rollout_batch_size`，且由于过滤丢弃，实际请求 SGLang 的 prompt 组数大于 8」。这正是「过采样」名称的由来——采得多、用得少。

**运行结果**：待本地验证（完整运行需多卡 GPU 集群）；本实践为静态阅读与推演。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `over_sampling_batch_size` 设成远大于 `rollout_batch_size`（比如 10 倍），主循环会怎样？

> **答案**：内层 while 会在第一轮就一次性提交远超目标的任务（`remaining_batch_size` 直接超过 `target_data_size`，内层条件不再成立）。之后这些多余任务会在外层循环中陆续完成，但 L438 `if len(data) < target_data_size` 会阻止它们被重复收编，最终在 L449 被 `abort` 终止。结果是：内存占用峰值高、abort 浪费算力多，但功能上仍能凑够 `rollout_batch_size` 组。

**练习 2**：为什么主循环用 `asyncio.wait(..., FIRST_COMPLETED)` 而不是 `asyncio.gather(*pendings)`？

> **答案**：`gather` 要等**全部**任务完成才返回，无法「一组完成就立刻检查是否够数、不够就补采」。`FIRST_COMPLETED` 允许流水线式调度：任意一组完成即回收，按需补采，凑够即停，从而尽早触发 `abort` 节省算力。

---

### 4.2 三层生成函数 generate / generate_and_rm / generate_and_rm_group

#### 4.2.1 概念说明

主循环只负责调度与凑数，真正「让一个 prompt 生成回答并打分」的工作由三个函数协作完成，构成自底向上的三层：

| 层 | 函数 | 职责 | 输入 → 输出 |
|----|------|------|-------------|
| 第 1 层（最底层） | `generate` | 只管「调 SGLang 生成、把新 token 与 logprob 写进 Sample」 | 一个 PENDING 的 Sample → 一个生成了 response 的 Sample（未算奖励） |
| 第 2 层 | `generate_and_rm` | 在 `generate` 外面包「限流 + custom_generate 分流 + 样本钩子 + 逐样本奖励」 | 一个 Sample → 一个带 reward 的 Sample |
| 第 3 层（最顶层） | `generate_and_rm_group` | 对一组（同 prompt 多采样）样本并发跑 `generate_and_rm`，可选地做「整组奖励」 | `list[Sample]`（一组）→ `list[Sample]`（带 reward） |

为什么分三层？因为 GRPO 这类算法对**同一个 prompt 采样多个回答**（`n_samples_per_prompt`），这些回答需要：

- **并发**生成（否则太慢）——第 3 层用 `gather` 并发。
- **逐样本**或**整组**计算奖励——有的奖励（如「组内排序」）需要看到整组才能算，所以奖励计算的位置要在第 3 层（`group_rm`）和第 2 层（逐样本）间二选一。

#### 4.2.2 核心流程

**第 1 层 `generate`**（默认单轮生成）：

```
async def generate(args, sample, sampling_params) -> Sample:
    assert sample.status in (PENDING, ABORTED)
    prompt_ids = _prepare_prompt_ids(sample, tokenizer, processor)   # prompt → token ids
    if max_new_tokens == 0:
        sample.status = TRUNCATED; return sample                     # 退化情形
    payload = {sampling_params, return_logprob=True, input_ids/text, image_data?}
    output = await post(router_url + "/generate", payload)           # 调 SGLang
    从 output["meta_info"]["output_token_logprobs"] 取 (logp, token) 对
    sample.append_response_tokens(tokens, log_probs, trainable=True) # 写回 Sample
    return sample
```

**第 2 层 `generate_and_rm`**（带限流/钩子/奖励）：

```
async def generate_and_rm(args, sample, sampling_params, evaluation=False):
    if sample 已是 COMPLETED/TRUNCATED: 直接 return（断言已有 reward，除非 group_rm）  # partial rollout 复用
    async with semaphore:                       # 限流
        if aborted: sample.status=ABORTED; return
        with dp_rank_context():                 # 数据并行均衡
            if custom_generate_function_path:   # 自定义生成（多轮 agent 等）
                sample = await custom_func(args, sample, sampling_params, evaluation?)
            else:
                sample = await generate(args, sample, sampling_params)
    sample = await apply_rollout_sample_hooks(args, sample)  # 生成后钩子
    if group_rm: return sample                  # 整组奖励留给第 3 层
    if isinstance(sample, list): 批量算奖励      # custom_func 可能 fan-out 出 list
    else: sample.reward = await async_rm(args, sample)
    return sample
```

**第 3 层 `generate_and_rm_group`**（一组并发 + 整组奖励）：

```
async def generate_and_rm_group(args, group, sampling_params, evaluation=False):
    if aborted: return group
    为组内每个 sample 生成 session_id（一致哈希路由用）
    tasks = [generate_and_rm(args, sample, params_i) for sample in group]
    group = await asyncio.gather(*tasks)        # 并发跑完整组
    if not aborted and group_rm:                # 整组奖励
        rewards = await batched_async_rm(args, group)
        把 reward 写回每个 sample
    return group
```

两条奖励路径的区别：

- **逐样本奖励**（`group_rm=False`，默认）：每个 sample 在第 2 层 `generate_and_rm` 内独立算奖励（L283-285），奖励函数只看自己。
- **整组奖励**（`group_rm=True`）：第 2 层在 `group_rm` 处提前 return 不算奖励（L265-266），由第 3 层把整组喂给 `batched_async_rm`（L328-332），奖励函数能看到同 prompt 的所有回答（如做组内归一化）。

#### 4.2.3 源码精读

**第 1 层 `generate`**：

[slime/rollout/sglang_rollout.py:152-219](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L152-L219) —— 这是「调 SGLang」的最薄封装。关键点：

- L160-162 断言 sample 状态必须是 `PENDING` 或 `ABORTED`（被 abort 后重试也允许）。
- L169-171 处理 `max_new_tokens==0` 的退化情形，直接标 `TRUNCATED` 返回。
- L174-189 构造 payload：`return_logprob=True` 是 slime 必须——它要 SGLang 返回每个生成 token 的对数概率（用于 off-policy 修正）。多模态时走 `text`+`image_data`，文本走 `input_ids`。
- L196-198 若 `sample.session_id` 存在且路由策略是一致哈希，则设 `X-SMG-Routing-Key` 请求头——这是前缀缓存复用的路由依据（多轮 agent 场景关键，见 u7-l2）。
- L200-202 `await post(url, payload)` 真正发 HTTP 请求；并用 `trace_span` 包裹以便追踪。
- L204-208 从返回的 `meta_info` 抽取 `output_token_logprobs`，每个元素是 `[logp, token_id]`，拆成两个列表。
- L210-217 `sample.append_response_tokens(..., trainable=True, ...)` 把生成 token、logprob、meta_info 写进 Sample——这是 Sample 从「只有 prompt」变成「有 response」的关键一步。

**第 2 层 `generate_and_rm`**：

[slime/rollout/sglang_rollout.py:223-287](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L223-L287) —— 在生成之外包了限流、分流、钩子、奖励。关键点：

- L230-231 partial rollout 下，把已有的（上一轮）off-policy 生成用 `loss_mask=0` 屏蔽——这是 partial rollout 与 off-policy 修正的衔接点。
- L234-238 已完成/截断的 sample 直接返回（partial rollout 续传时复用已有结果），断言 `sample.reward is not None`（除非 `group_rm`）。
- L243 `async with state.semaphore` 限流，L244-246 若已 abort 则标 `ABORTED` 返回。
- L250-258 **custom_generate 分流**：优先用 `sample.generate_function_path`（per-sample，eval 配置可设），否则用 `args.custom_generate_function_path`。自定义函数可能多轮调用工具、fan-out 出多个 Sample（见 u6-l2、u7）。注意 L255-258 按签名是否含 `evaluation` 决定怎么传参——这是 slime 对自定义函数签名兼容性的处理。
- L260 默认路径调 `generate`。
- L262 `apply_rollout_sample_hooks`：生成后、奖励前跑样本钩子（详见 [slime/rollout/sample_hooks.py:39-50](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sample_hooks.py#L39-L50)，它递归地保留 list 形状、对每个 Sample 叶子跑钩子）。
- L265-266 `group_rm` 时提前 return，奖励留给第 3 层。
- L268-278 custom_generate 返回 `list[Sample]`（fan-out）时批量算奖励。
- L279-286 单样本路径：`sample.reward = await async_rm(args, sample)`。

**奖励分发 `async_rm`**：

[slime/rollout/rm_hub/__init__.py:55-96](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L55-L96) —— 优先级：per-sample `custom_rm_path` > `args.custom_rm_path` > 按 `rm_type` 分发到内置奖励（`deepscaler`/`math`/`f1`/`gpqa`/`remote_rm` 等）。L69-71 处理 `boxed_` 前缀：先抽 `\boxed{}` 答案再判类型。本讲只需知道「它返回一个 float 奖励」，各类奖励细节见 u3-l4。

**第 3 层 `generate_and_rm_group`**：

[slime/rollout/sglang_rollout.py:295-334](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L295-L334) —— 关键点：

- L307-308 已 abort 直接返回整组。
- L311-313 为组内每个 sample 生成唯一 `session_id`（若未设），供一致哈希路由。
- L318-320 确定性推理模式下，用预生成的 `group_sampling_seeds` 给每个 sample 不同的采样种子（保证同 prompt 多采样可复现）。
- L321-323 为每个 sample 创建一个 `generate_and_rm` 任务。
- L325 `asyncio.gather(*tasks)` **并发**跑完整组——这是「同 prompt 多采样」并发的核心。
- L328-332 `group_rm` 模式下，把整组喂给 `batched_async_rm`（[rm_hub/__init__.py:99-110](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L99-L110)），写回每个 reward。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：看清一个 prompt 组内「同 prompt、N 个采样」是如何并发、如何各自算奖励的。

**操作步骤**：

1. 打开 `sglang_rollout.py`，在 `generate_and_rm_group`（L295）设阅读锚点。
2. 假设 `n_samples_per_prompt=4`、`group_rm=False`，回答：第 3 层会创建几个 `generate_and_rm` 任务？它们何时并发、何时汇合？
3. 把 `group_rm` 切成 `True`，对比奖励计算的位置发生了什么变化。
4. 阅读 `generate_and_rm` 的 L250-260，回答：当同时设置了 `--custom-generate-function-path` 和默认 `generate`，会走哪条？依据是哪一行代码？

**需要观察的现象**：

- `n_samples_per_prompt=4` → `gather` 里有 4 个任务并发；汇合点是 L325。
- `group_rm=True` 时，逐样本奖励分支（L279-286）被 L265-266 的 early return 跳过，整组奖励在 L328-332 统一算。
- 自定义生成优先：依据 L250 的 `getattr(sample, "generate_function_path", None) or args.custom_generate_function_path`，非空才走自定义，否则走 L260 的默认 `generate`。

**预期结果**：你能画出「一个 group → 4 个并发的 generate_and_rm → 4 个并发的 generate（各发一次 HTTP）→ 各算一次 reward → gather 汇合」的时序。

**运行结果**：待本地验证（需 SGLang 服务）；本实践为静态阅读。

#### 4.2.5 小练习与答案

**练习 1**：custom_generate 函数返回了 `list[Sample]`（fan-out），后续奖励如何处理？

> **答案**：`generate_and_rm` 在 L268 判断 `isinstance(sample, list)`，进入批量分支：L270 先看有没有 `ABORTED`，L273 筛出 `reward is None` 的样本，L275 用 `batched_async_rm` 一次性算多个奖励并写回。这种 fan-out 是多轮 agent 把一条轨迹拆成多段可训练 Sample的基础（见 u7-l3）。

**练习 2**：为什么 `group_rm=True` 时评估（eval）不支持？

> **答案**：`eval_rollout` 在 [sglang_rollout.py:475](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L475) 开头 `assert not args.group_rm`。因为评估只关心每条回答的绝对奖励（算 pass rate），不需要组内相对信息；且 eval 的并发结构与训练的 group 结构不同。

---

### 4.3 abort 机制与 partial rollout 衔接

#### 4.3.1 概念说明

主循环一旦凑够 `rollout_batch_size` 组，就会调 `abort(args, rollout_id)` 来终止**仍在 SGLang 队列里排队的多余请求**。这有两个目的：

1. **省算力**：过采样产生的多余请求没必要继续生成完。
2. **回收半成品（partial rollout）**：如果开启了 `--partial-rollout`，那些被 abort 时**已经生成了一部分**的样本不是丢弃，而是被打包成「半成品」回收到数据缓冲区，下一轮接着生成——这就是 partial rollout 的跨轮续传。

abort 涉及两个层面：

- **应用层**：`abort` 函数设置全局 `aborted=True` 标志、回收 asyncio 任务。
- **引擎层**：`abort_servers_until_idle` 向每个 SGLang server 发 `/abort_request`，轮询直到其请求队列清空。

#### 4.3.2 核心流程

```
async def abort(args, rollout_id):
    state.aborted = True                                    # 全局标志，正在跑的任务会自检并标 ABORTED
    urls = (await get(router + "/workers"))["workers"]      # 拿到所有 SGLang server 的 URL
    await abort_servers_until_idle(urls)                    # 引擎层：逐个 server abort 直到空闲
    while state.pendings:                                   # 应用层：等所有在途 asyncio 任务结束
        done, pendings = asyncio.wait(pendings, FIRST_COMPLETED)
        if not partial_rollout: continue                    # 普通模式：直接丢弃
        for task in done:                                   # partial 模式：回收半成品
            group = task.result()
            给已有 response 的 sample 标 start_rollout_id
            aborted_samples.append(group)
    return aborted_samples                                  # 回到 generate_rollout，被 add_samples 回填
```

`aborted=True` 标志的作用链：`generate_and_rm` 在 `async with semaphore` 进入后会检查 `if state.aborted`（L244-246），把尚未开始的 sample 标 `ABORTED` 提前返回；`generate_and_rm_group` 开头也检查（L307-308）。

#### 4.3.3 源码精读

**应用层 `abort`**：

[slime/rollout/sglang_rollout.py:337-369](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L337-L369) —— 关键点：

- L342 `state.aborted = True`：设置全局标志。注意 L341 `assert not state.aborted`，保证一次 rollout 只 abort 一次。
- L344-345 向 router 的 `/workers` 端点要所有 server 的 URL。
- L347 `abort_servers_until_idle(urls)`：引擎层终止。
- L351-364 `while state.pendings` 循环等待在途任务结束。L354 `if not args.partial_rollout: continue`——普通模式直接丢弃结果。L358-364 partial 模式：对每个完成的 group，若 sample 已有 `response`，则给它打上 `start_rollout_id` 元数据，收集进 `aborted_samples`。
- L366-367 日志记录回收了多少半成品。
- L369 返回 `aborted_samples`，它在 `generate_rollout`（L638-639）里被 `data_source.add_samples` 回填给缓冲区。

**引擎层 `abort_servers_until_idle`**：

[slime/backends/sglang_utils/server_control.py:66-67](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/server_control.py#L66-L67) —— 用 `gather` 对所有 server 并发执行 `abort_server_until_idle`。

[slime/backends/sglang_utils/server_control.py:43-63](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/server_control.py#L43-L63) —— 单个 server 的终止循环：L47 发 `/abort_request`，L50 查 `/v1/loads` 看还有几个在跑的请求，L55 若已清空则返回，否则 L62 `sleep(retry_interval)` 后重试。这里的关键是「发 abort 后不是立刻返回，而是轮询确认队列真的空了」，否则下一轮 rollout 会和残余请求抢资源。

> 名词解释：**partial rollout** 解决长尾样本问题。当某些 prompt 生成极慢（长尾），普通模式要么等它们（拖慢整轮）、要么 abort 丢弃（浪费已生成部分）。partial rollout 允许「这轮没生成完的，下轮接着生成」：半成品带 `response_length>0` 被 `add_samples` 回收，下轮取出时 `generate_and_rm` 看到 `status=PENDING` 但已有部分 response，会续生成（L230-231 还会把上一轮的 off-policy 部分用 `loss_mask=0` 屏蔽）。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：理解 abort 如何「既终止多余请求、又保住 partial rollout 的半成品」。

**操作步骤**：

1. 打开 `server_control.py` 的 `abort_server_until_idle`（L43），跟随一次「发 abort → 查 load → 重试」循环。
2. 回到 `sglang_rollout.py` 的 `abort`（L337），对比 `partial_rollout=True/False` 两个分支在 `while state.pendings` 循环里的行为差异。
3. 追踪一个「已生成 50 个 token、但本轮被 abort」的 sample：它如何进入 `aborted_samples`、如何经 `add_samples` 回到缓冲区、下轮如何被 `generate_and_rm` 续上。
4. 阅读 `generate_and_rm` L230-231，回答：下轮续生成时，上一轮的 50 个 token 的 `loss_mask` 会被怎样处理？为什么？

**需要观察的现象**：

- 普通模式：`continue` 直接丢，`aborted_samples` 为空，`generate_rollout` 的 `if aborted_samples` 不成立，不回填。
- partial 模式：每个有 `response` 的 sample 被收集，带 `start_rollout_id`，最终 `add_samples` 回填。

**预期结果**：你能讲清「半成品跨轮续传」的完整闭环：abort 回收 → `add_samples` → 下轮 `get_samples` 取出 → `generate_and_rm` 续生成（旧部分 `loss_mask=0`）。

**运行结果**：待本地验证（需集群）；本实践为静态阅读。

#### 4.3.5 小练习与答案

**练习 1**：`abort` 里为什么要 `while state.pendings` 等所有任务结束，而不是直接返回？

> **答案**：因为发完 `/abort_request` 后，正在 SGLang 里跑的请求不会瞬间消失，对应的 asyncio 任务也不会瞬间完成。必须等它们全部结束（标 ABORTED 或带 partial 结果返回），否则：(a) `state.reset()` 后这些游离任务可能访问已清空状态；(b) partial 模式下会漏收半成品。`while state.pendings` 保证「应收尽收」。

**练习 2**：如果不开 `--partial-rollout`，被 abort 的半成品的计算资源是否完全浪费？

> **答案**：是的。普通模式下 `continue` 直接丢弃这些 group 的结果，已生成的 token 不被回收、不进训练。这正是 partial rollout 存在的意义——把长尾样本的已生成部分 salvage 回来，避免浪费。

---

## 5. 综合实践

**任务**：在 `slime/rollout/sglang_rollout.py` 中，追踪**一个 prompt 组**（同 prompt、`n_samples_per_prompt` 个采样）从「数据源取出」到「进入返回的 `list[list[Sample]]`」的完整调用链，并画出函数调用图。

**步骤**：

1. 从 `generate_rollout`（L618）出发，按以下顺序阅读并记录每个函数的行号与一句话职责：
   - `generate_rollout` → `run` → `generate_rollout_async`
   - `generate_rollout_async` 内：`data_source(...)`（取数）→ `submit_generate_tasks` → `asyncio.wait`
   - `submit_generate_tasks` → 为每组创建 `generate_and_rm_group` 任务
   - `generate_and_rm_group` → `asyncio.gather` → 每个样本一个 `generate_and_rm`
   - `generate_and_rm` → `generate`（默认）或 custom_generate → `apply_rollout_sample_hooks` → `async_rm`（逐样本）或 early return（group_rm）
   - `generate` → `post("/generate")` → `append_response_tokens`
   - 回到 `generate_rollout_async`：`call_dynamic_filter` → `data.append(group)` → 循环结束 → `abort` → `RolloutFnTrainOutput`
2. 用一张树状调用图把上述链路画出来，标注：
   - 哪些是 `async`、哪些是同步。
   - 哪一层做了**并发**（`gather`/`create_task`）、哪一层做了**限流**（`semaphore`）。
   - 奖励计算在 `group_rm=False/True` 时分别落在哪一层。
3. 在调用图上标出 `Sample` 字段的写入时机：`tokens`/`response`/`loss_mask`/`rollout_log_probs` 在哪一步被写、`reward` 在哪一步被写、`status` 在哪几步被推进。

**验收标准**：你的调用图应当能让一个没读过源码的人，仅凭图就能回答「一个 prompt 的 4 个采样回答是如何并发生成、各自打分、最终汇成一组返回的」。

**运行结果**：待本地验证（完整链路需 SGLang + 多卡）；本实践为源码阅读与画图。

---

## 6. 本讲小结

- `generate_rollout` 是「同步外壳 + 异步内核」结构：同步入口按 `evaluation` 分流，训练路径把 `data_source.get_samples` 交给 `generate_rollout_async` 执行。
- `generate_rollout_async` 用双层 while 循环 + `asyncio.wait(FIRST_COMPLETED)` 实现「过采样补数 + 动态过滤凑够 `rollout_batch_size` 组」的流水线调度。
- 数据生成分三层：`generate`（最薄，只调 SGLang）/ `generate_and_rm`（限流+分流+钩子+逐样本奖励）/ `generate_and_rm_group`（一组并发 + 可选整组奖励）。
- 奖励有两条路径：`group_rm=False` 在第 2 层逐样本算（`async_rm`），`group_rm=True` 在第 3 层整组算（`batched_async_rm`）。
- `abort` 在凑够后终止多余请求：引擎层 `abort_servers_until_idle` 轮询清空 SGLang 队列，应用层等所有在途任务结束；partial rollout 下回收半成品并打 `start_rollout_id`，经 `add_samples` 跨轮续传。

---

## 7. 下一步学习建议

- **u3-l3 数据源 DataSource 与缓冲区**：本讲把 `data_source` 当作「能 `get_samples(n)` / `add_samples(...)` 的黑盒」，下一讲拆开看它内部如何管理 prompt 池与 partial rollout 缓冲。
- **u3-l4 奖励模型 rm_hub**：本讲只用了 `async_rm` 返回一个 float，下一讲展开 `rm_type` 分发与各类内置奖励（deepscaler/math/f1/gpqa）的判分逻辑。
- **u3-l5 动态采样与过滤**：本讲把动态过滤当作主循环里的一个「丢一组」开关，下一讲详解 `DynamicFilterOutput`、`keep_when_insufficient` 等判据与触发再采样的阈值。
- **u7-l4 流式、全异步与部分回滚 rollout**：本讲讲的是默认同步内核，高级篇会对比 `sglang_streaming_rollout`、`fully_async_rollout` 与 partial rollout 三种高级数据流。
