# 多轮 Rollout 与环境交互

## 1. 本讲目标

在 u7-l2 我们建立了 GRPO 的核心认知：对同一个 prompt 采样 K 条回答，用组内归一化的 advantage 更新策略。但那里假设的是**单轮**场景——模型一次性给出完整回答。现实里很多任务是多轮的：模型先输出一个动作（调用工具、下一步棋、提交答案），环境返回一个观测（工具结果、新棋盘、对错反馈），模型再基于观测继续推理。

学完本讲，你应当能够：

- 说清 ms-swift 中「多轮 Rollout」要解决什么问题，以及它与单轮 Rollout 的接口差异。
- 掌握 `RolloutScheduler` / `MultiTurnScheduler` 这套调度抽象，能区分「全定制（覆写 `run`）」与「部分定制（实现 `step` + `check_finished`）」两条路径。
- 理解 `Env`（gym 环境抽象）与 `GYMScheduler` 如何把「环境 reset/step」挂进调度器的通用钩子，以及 `--use_gym_env` 时奖励如何被直接消费。
- 读懂 `agent_loop.run_multi_turn` 这个与后端无关的同步驱动器，以及 `OpenEnvWrapper` 如何把外部 OpenEnv 服务接入多轮训练。
- 能够配置一个带环境交互的多轮 GRPO 实验，并解释 RolloutScheduler 在每一轮如何调用环境、把观测回填进对话。

## 2. 前置知识

本讲承接 u7-l1（RLHF 训练流程）、u7-l2（GRPO 算法核心）、u7-l3（奖励函数与 RM 插件），并复用 u6-l2 的 `GRPOVllmEngine`。在进入正文前，先用三段话把必要背景补齐。

**Rollout 是什么。** 在 GRPO 训练循环的「rollout → score → train」五阶段骨架里（见 u7-l2），rollout 阶段要用当前策略模型对一批 prompt 采样回答。ms-swift 把这个采样动作交给推理引擎完成，多轮场景下用的是 `GRPOVllmEngine`（vLLM 后端，见 u6-l2）。本讲讨论的不是「采样一次」，而是「采样→喂给环境→拿到新观测→再采样」的循环。

**多轮场景的奖励特征。** 单轮 GRPO 的奖励由奖励函数（u7-l3 的 orm/prm/RM 插件）对最终回答一次性打分。多轮场景里，环境每一步都可能给出一个 `reward`，整条轨迹（trajectory）的奖励是各步之和 `total_reward`。ms-swift 允许两种奖励来源：用 `--use_gym_env true` 让环境自身的 `total_reward` 直接当奖励；或继续用 `--reward_funcs`，环境只负责产出对话、奖励由函数算。

**异步推理的必要性。** 多轮调度需要「生成一段→等环境→再生成一段」的交错执行，并且要对一个 batch 里的多条轨迹并发推进。因此多轮 Rollout 强制要求 vLLM 的异步引擎（`--vllm_use_async_engine true`），调度器大量使用 `async/await`。这是后面所有代码是协程的根本原因。

## 3. 本讲源码地图

本讲涉及的核心文件集中在 `swift/rollout/`，外加参数定义与接线点：

| 文件 | 作用 |
| --- | --- |
| [swift/rollout/multi_turn.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py) | 调度器全部实现：`RolloutScheduler`（单轮基类）、`MultiTurnScheduler`（多轮抽象）、`MathTipsScheduler`/`GYMScheduler`/`OpenEnvScheduler` 等具体调度器，以及注册表 `multi_turns`。 |
| [swift/rollout/gym_env.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/gym_env.py) | 环境抽象 `Env`（reset/step/close 三方法）、示例 `SimpleMathEnv`，以及环境注册表 `envs`。 |
| [swift/rollout/agent_loop.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/agent_loop.py) | 与后端无关的多轮驱动器 `run_multi_turn`，把同一套调度器循环搬到 HF Accelerate / Megatron / Megatron-Ray 进程组里运行。 |
| [swift/rollout/openenv_wrapper.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/openenv_wrapper.py) | `OpenEnvWrapper`：对 OpenEnv 的 `GenericEnvClient` 做带重连/重试的薄封装，提供同步的 reset/step/close。 |
| [swift/arguments/deploy_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/deploy_args.py) | `RolloutArguments` 定义 `multi_turn_scheduler` / `max_turns` / `use_gym_env` / `gym_env` 等参数及其校验。 |
| [swift/pipelines/infer/rollout.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/rollout.py) | `swift rollout` 服务端，含 `get_rollout_engine_type`：把 `multi_turn_scheduler` 名字解析成调度器实例并包在引擎外层。 |
| examples/megatron/grpo/multi_turn/frozen_lake_plugin.py | 真实示例环境 `FrozenLakeEnv`，演示如何继承 `Env` 写一个文本游戏环境并注册。 |

记忆要点：`multi_turn.py` 是「大脑」（调度逻辑），`gym_env.py` 是「世界」（环境接口），`agent_loop.py` 是「手脚」（在不同进程组里跑大脑），`openenv_wrapper.py` 是连接外部世界的「网线」。

## 4. 核心概念与源码讲解

### 4.1 RolloutScheduler：多轮调度抽象

#### 4.1.1 概念说明

`RolloutScheduler` 是 ms-swift 多轮 Rollout 的根抽象。它的核心矛盾是：**推理引擎只会「给一段 prompt 生成一段回答」，而多轮训练需要「生成→环境反馈→再生成」的状态机**。调度器就是夹在训练器与推理引擎之间的这层状态机，负责管理「轮次（turn）」这一概念。

先看最朴素的单轮情形：基类 `RolloutScheduler.run()` 就是「调一次推理引擎，把 token 和全 1 的 loss mask 包成 `RolloutOutput` 返回」——这正是普通（非多轮）GRPO rollout 的行为。

```python
# swift/rollout/multi_turn.py:138-149 （单轮默认实现）
async def run(self, infer_request, request_config, **kwargs) -> 'RolloutOutput':
    response = await self.infer_engine.infer_async(infer_request, request_config, **kwargs)
    response_token_ids = response.choices[0].token_ids
    response_loss_mask = [1] * len(response_token_ids)
    return RolloutOutput(
        response=response,
        messages=infer_request.messages,
        response_token_ids=[response_token_ids],
        response_loss_mask=[response_loss_mask],
        rollout_infos={'num_turns': 1})
```

注意三个返回字段，它们是多轮训练的命脉：

- `response_token_ids`：每轮回答的 token id，按轮存成 `List[List[int]]`。如果调度器返回了它，训练器就**不再把回答文本重新编码成 token**，避免「编码/解码不对称」导致的训练偏差。
- `response_loss_mask`：与 `response_token_ids` 等长的 0/1 掩码，控制每个 token 是否参与 loss。多轮里环境观测、提示语往往不该算 loss，靠它屏蔽。
- `rollout_infos`：任意可 JSON 序列化的元信息，如 `num_turns`、`total_reward`，会一路带到训练器，既可写日志也可当奖励。

#### 4.1.2 核心流程

`RolloutScheduler` 定义了两个「通用钩子」，被两种执行模式共用：

```text
            ┌──────────── server 模式（swift rollout 服务，async 事件循环）───────────┐
训练器 ──→  │  scheduler.run()  内部直接 await 钩子                                    │
            └─────────────────────────────────────────────────────────────────────────┘
            ┌──────────── colocate 模式（训练与 vLLM 同进程，同步训练循环）─────────────┐
训练器 ──→  │  agent_loop.run_multi_turn()  用临时事件循环驱动钩子                      │
            └─────────────────────────────────────────────────────────────────────────┘
                                  │
         on_trajectory_start(requests)   ← 一条轨迹开始前调用一次（env.reset 放这里）
                                  │
         ┌──── while 每一轮 ─────┐
         │  infer_async（生成）  │
         │  on_turn_end(req,...) │  ← 一轮回答追加后调用（env.step 放这里，可返回 done）
         │  check_finished       │  ← 是否结束（可被 on_turn_end 的 done 覆盖）
         │  step(req, choice)    │  ← 构造下一轮的 request（把观测塞回 messages）
         └───────────────────────┘
```

钩子是 `async` 的，这样 gym 环境（其 reset/step 本身是协程）可以被直接 `await`。两种模式的差异仅在于「谁来跑这个事件循环」：server 模式天然在异步引擎的事件循环里，钩子被原生 await；colocate 模式是同步训练循环，由 `agent_loop.run_multi_turn` 临时建事件循环驱动（见 4.3）。

#### 4.1.3 源码精读

**通用钩子的声明与「双模式」注释。** 这段注释是理解整个设计的关键：钩子被 server 模式的 `run()` 与 colocate 模式的 `run_multi_turn()` 同时调用，因此覆写钩子就能在两种模式下都注入环境逻辑。

[swift/rollout/multi_turn.py:33-64](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L33-L64) — 通用钩子 `on_trajectory_start` / `on_turn_end` 的声明。`on_turn_end` 返回的 dict 可含 `'done'`（覆盖 `check_finished` 结论）与 `'rollout_infos'`（并入轨迹元信息）。

**属性代理 `__getattr__`。** 调度器把自身「伪装」成推理引擎：访问调度器上不存在的属性时，会转发到 `infer_engine` 甚至 `infer_engine.engine`。这样训练器拿到 `rollout_engine` 后，无论是裸引擎还是调度器包装，都能用同一套方法名调用。

[swift/rollout/multi_turn.py:151-169](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L151-L169) — `__getattr__` 与 `engine`/`tokenizer` 属性。注意 `tokenizer` 优先用构造时显式传入的（colocate 模式下 `infer_engine` 可能为 `None`）。

**多轮抽象 `MultiTurnScheduler`。** 它在 `RolloutScheduler` 之上提供了默认的多轮 `run()` 循环，并声明两个可定制点：`step()`（必须实现，处理轮间转移）与 `check_finished()`（有默认实现）。类文档明确给出「全定制 vs 部分定制」两条路径。

[swift/rollout/multi_turn.py:181-261](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L181-L261) — `MultiTurnScheduler` 类与 `run()` 方法签名。其中 L200-L221 还解释了 `response_token_ids` / `response_loss_mask` 的语义与两条 loss mask 策略（用内置 `loss_scale` 或返回逐 token 掩码）。

**默认 `run()` 的主循环骨架。** 这是 server 模式多轮的核心。它依次：移除占位回答 → 生成 → 追加 assistant → 调 `on_turn_end` → 判停（`done` 优先于 `check_finished`，再叠加 `max_turns` 兜底）→ 若停则组装 `RolloutOutput`，否则 `step()` 进入下一轮。

[swift/rollout/multi_turn.py:269-400](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L269-L400) — `run()` 的 `while True` 循环。重点看 L298-L307 的「判停优先级」与 L362-L386 的「step 返回值并入 token/mask/logprobs」。

判停的三级优先级值得单独记住：

```text
on_turn_end()['done']   （环境说了算，最高优先）
        ↓ 缺省时
check_finished(...)      （调度器自带的结束条件）
        ↓ 再叠加
current_turn >= max_turns （防忘判 max_turns 的兜底）
```

**`check_finished` 默认实现**只有两条：达到长度上限（`finish_reason == 'length'`）或撞到 `max_turns`。

[swift/rollout/multi_turn.py:426-453](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L426-L453) — 默认终止逻辑。

**两个「部分定制」示例调度器。**

- `MathTipsScheduler`：模型答错时追加一句「等一下，我好像算错了」的 tips，给模型一次自我修正机会。它的 `step()` 展示了如何**同时返回 `response_token_ids` 与 `response_loss_mask`**——把 tips 那几个 token 的 mask 置 0，使它们不参与 loss。

[swift/rollout/multi_turn.py:657-723](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L657-L723) — `MathTipsScheduler.step`：截断到 `<answer>`/`</think>`、追加 tips、构造 `[1]*n + [0]*m` 的 loss mask。

- `ThinkingModelTipsScheduler`：展示「全定制」路径——覆写整个 `run()`，返回 `List[RolloutOutput]`，即一条轨迹被拆成多条训练样本（每轮一条），用于「只在最后一轮算 loss」。这是「动态数量 rollout 输出」的典型用法。

[swift/rollout/multi_turn.py:495-546](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L495-L546) — `ThinkingModelTipsScheduler.run`：每轮产出独立 `RolloutOutput` 并收集成列表。

**注册表。** 所有内置调度器登记在模块级字典里，参数 `--multi_turn_scheduler` 用的就是这里的 key。

[swift/rollout/multi_turn.py:961-966](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L961-L966) — `multi_turns = {...}`：`math_tip_trick` / `gym_scheduler` / `openenv_scheduler` / `thinking_tips_scheduler`。

#### 4.1.4 代码实践（源码阅读型）

**实践目标：** 通过阅读 `MultiTurnScheduler.run()` 的循环，画出一条轨迹的状态流转，验证「环境观测如何变成下一轮的 user 消息」。

**操作步骤：**

1. 打开 [swift/rollout/multi_turn.py:262-400](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L262-L400)，在 L274、L289-L295、L298、L301、L309、L362 六处各画一个标记。
2. 回答下列追踪问题（答案见 4.1.5）：
   - 第一轮为什么要 `remove_response(messages)`（L274）？
   - L291-L295 的 `is_continuation` 分支何时触发？它和 L294 的 `append` 有何区别？
   - 一条 3 轮的轨迹，`total_response_ids` 最终是几个元素？每个元素对应什么？

**需要观察的现象：** `messages` 列表在每一轮之后变长（交替追加 assistant 与 user），而 `total_response_ids` / `total_response_loss_mask` 按「轮」追加（外层是轮，内层是该轮 token）。

**预期结果：** 3 轮轨迹 → `messages` 含 1 条 system（可选）+ 1 条初始 user + 3 条 assistant + 2 条环境回填的 user（最后一轮不再回填）；`response_token_ids` 长度为 3。本结论可在 4.2 的 `GYMScheduler` 运行时验证；若本地无 GPU，标记「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1.** 为什么 `RolloutScheduler` 的钩子要设计成 `async`，而不是普通同步方法？

> **答：** 因为多轮 Rollout 要对一个 batch 里的多条轨迹**并发**推进（`asyncio.gather` 同时跑多条轨迹的 reset/step/生成），且 gym 环境的 reset/step 本身可能涉及 I/O（HTTP、WebSocket），用协程才能在不阻塞事件循环的前提下交错执行。同步方法会让一条轨迹的环境调用卡住整个 batch。

**练习 2.** 假设你只想要「模型回答错时，追加一句提示让模型重答，最多重试一次」，应该用全定制还是部分定制？需要实现哪些方法？

> **答：** 用部分定制即可。继承 `MultiTurnScheduler`，实现 `step()`（追加提示 user 消息），覆写 `check_finished()`（已给过提示或答对则返回 True），并设 `max_turns=2` 兜底。这正是 `MathTipsScheduler` 的做法，不需要重写整个 `run()`。

**练习 3.** `on_turn_end` 返回 `{'done': True}` 与 `check_finished` 返回 `True` 效果一样吗？

> **答：** 在默认 `run()` 里效果一致（都会停），但语义来源不同：`on_turn_end['done']` 通常由**环境**判定（如游戏结束），`check_finished` 由**调度器**判定（如长度/轮数）。当两者冲突时，`on_turn_end['done']` 优先（L302-L303：`if 'done' in turn_result: should_stop = turn_result['done']`）。所以环境强制结束时，调度器的判停条件会被覆盖。

### 4.2 gym_env：环境抽象与 GYMScheduler

#### 4.2.1 概念说明

有了调度器骨架，下一个问题是：**「环境」在 ms-swift 里长什么样？** 答案是 `swift/rollout/gym_env.py` 里的 `Env` 抽象基类。它借鉴 OpenAI Gym 的 reset/step 范式，但做了两处适配 RL 训练的改造：

1. `reset` 返回三元组 `(observation, info, system_message)`——多了一个 `system_message`，因为每条轨迹的系统提示往往由环境决定（如游戏规则）。
2. `step` 接收的 `action` 不是张量，而是一段 `Messages` 对话——因为模型的「动作」就是它的自然语言回答。

`Env` 是纯接口；真正把环境接进调度器的是 `GYMScheduler`。它的妙处在于：**完全不需要覆写 `run()`**，只通过实现两个通用钩子 `on_trajectory_start`（调 `env.reset`）和 `on_turn_end`（调 `env.step`）就把环境挂进了 4.1 的主循环，并且 server / colocate 两种模式都能用。

#### 4.2.2 核心流程

一条 gym 多轮轨迹的生命周期如下：

```text
轨迹开始：
  on_trajectory_start(requests)
    └─ 对每条 req：
         env = envs[env_name](env_config)        # 按数据行的 env_config 建环境
         obs, info, sys = await env.reset(req)    # 拿初始观测
         req.messages = [{'role':'system',...}, {'role':'user','content':obs}]
         _envs[uuid] = env                        # 用 uuid 索引环境（一轨迹一环境）

每一轮：
  infer → assistant 回答
  on_turn_end(req, choice, turn)
    └─ env = _envs[req.uuid]
       next_obs, reward, done, info = await env.step(deepcopy(req.messages))
       _total_rewards[uuid] += reward             # 累计奖励
       _pending_obs[uuid] = None if done else next_obs
       return {'done': done, 'rollout_infos': {'total_reward':..., 'step_rewards':...}}
  若未停：
  step(req, choice, turn)
    └─ 把 _pending_obs[uuid] 作为新 user 消息 append 进 messages  ← 结果回填进对话
```

关键设计：**用 `req.uuid` 给每条轨迹一个独立的环境实例**，存在 `_envs: Dict[str, Env]` 里。因为一个 batch 同时跑 N 条轨迹，每条轨迹的游戏状态互不相同，必须靠 uuid 隔离。轨迹结束（`done`）时立即 `_close_and_remove(uuid)` 释放环境。

奖励聚合是简单的逐步求和：

\[
\text{total\_reward}^{(t)} = \sum_{i=1}^{t} r_i,\qquad r_i = \text{env.step}(\cdot).\text{reward}
\]

这个 `total_reward` 通过 `rollout_infos` 一路传到训练器；当 `--use_gym_env true` 时，它会被当作一列奖励直接并入 GRPO 的奖励矩阵（见 4.2.4）。

#### 4.2.3 源码精读

**`Env` 抽象基类。** 三个抽象方法 `reset` / `step` / `close`，签名清楚地体现了「对话即动作」。

[swift/rollout/gym_env.py:15-56](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/gym_env.py#L15-L56) — `Env` 基类。注意 `step` 的返回是 `(next_observation, reward, done, info)`，与 Gym 一致。

**`SimpleMathEnv` 示例与注册表。** 它从 `config.data_dict['problem']`/`['solution']` 取题与答案，用 `MathAccuracy` 判对错，对了 reward=1 并 `done=True`。最后 `envs = {'math_env': SimpleMathEnv}` 是环境注册表，对应调度器的 `--gym_env` 参数。

[swift/rollout/gym_env.py:84-127](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/gym_env.py#L84-L127) — `SimpleMathEnv` 与 `envs` 注册表。

**`GYMScheduler.on_trajectory_start`：一轨迹一环境。** 这是「环境如何接进调度器」的第一只手。它并行地为每条 request 建环境、reset、用初始观测替换 `req.messages`。

[swift/rollout/multi_turn.py:758-781](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L758-L781) — `GYMScheduler.on_trajectory_start`：从 `req.data_dict['env_config']` 取配置建环境，`env.reset(req)` 拿初始观测，重写 `req.messages`。

**`GYMScheduler.on_turn_end`：推进环境、累计奖励。** 第二只手。调 `env.step`、累加 `total_reward`、把 `next_obs` 暂存到 `_pending_obs`（done 则置 None），并返回 `done` 与 `rollout_infos`。

[swift/rollout/multi_turn.py:783-805](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L783-L805) — `GYMScheduler.on_turn_end`：注意 L791 传给 `env.step` 的是 `deepcopy(infer_request.messages)`，避免环境误改对话历史。

**`GYMScheduler.step`：把观测回填进对话。** 第三只手（也是最容易被忽略的一步）。它把暂存的 `_pending_obs` 作为一条新 user 消息追加进 `messages`，这样下一轮 `infer_async` 时模型就「看到」了环境的反馈。

[swift/rollout/multi_turn.py:810-817](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L810-L817) — `GYMScheduler.step`：环境观测 → user 消息的回填点。

**真实环境示例：`FrozenLakeEnv`。** 这是一个文本版「冰湖」游戏：模型每轮看到一张 ASCII 网格（S 起点/G 终点/H 洞/F 冰），要在 `<action>...</action>` 里输出 up/down/left/right，到达 G 得 1 分、掉洞得 0 分并结束。它的 `reset` 生成可解地图并渲染，`step` 解析动作、移动、判胜负。注册语句 `envs['frozen_lake'] = FrozenLakeEnv` 让它能被 `--gym_env frozen_lake` 选中。

[examples/megatron/grpo/multi_turn/frozen_lake_plugin.py:130-192](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/grpo/multi_turn/frozen_lake_plugin.py#L130-L192) — `FrozenLakeEnv` 的 `reset`/`step`：注意 `step` 里 `_parse_action` 从回答里抽动作（动作即「工具调用」），未解析出动作时返回「无效响应」观测而非结束。

**数据格式：每行的 `env_config`。** `FrozenLakeEnv` 的输入数据每行只放一个占位 user 消息和环境的初始化参数：

```json
{"messages":[{"role":"user","content":"<placeholder>"}],"env_config":{"seed":0}}
```

`<placeholder>` 会被 `on_trajectory_start` 里 `env.reset` 的初始观测覆盖；`seed` 让同一行的 `num_generations` 条 rollout 共享同一张地图，保证组内可比（GRPO 组内归一化要求）。

#### 4.2.4 代码实践（运行型，含源码阅读）

**实践目标：** 跑通 `examples/megatron/grpo/multi_turn/frozen_lake.sh`，并解释「模型动作 → 环境 step → 观测回填」的完整一轮。

**操作步骤：**

1. 阅读启动脚本的关键三行（完整脚本见 [examples/megatron/grpo/multi_turn/frozen_lake.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/grpo/multi_turn/frozen_lake.sh)）：

   ```bash
   --external_plugins examples/megatron/grpo/multi_turn/frozen_lake_plugin.py \
   --multi_turn_scheduler gym_scheduler \   # 选 GYMScheduler
   --gym_env frozen_lake \                  # 选 envs['frozen_lake']
   --use_gym_env true \                     # 用环境的 total_reward 当奖励
   --max_turns 10                           # 单条轨迹最多 10 步
   ```

   注意脚本里**没有 `--reward_funcs`**——因为 `--use_gym_env true` 时环境的 `total_reward` 直接作为奖励。

2. 在 8 卡环境执行该脚本（单机 8 张 GPU）。
3. 训练日志里观察 `reward` 指标——脚本注释提到 120 步内 reward 从 0.2 → 0.6。

**需要观察的现象：** 日志中的 `num_turns` 指标反映平均轨迹长度（成功到达 G 或掉洞会提前结束，`num_turns < max_turns`）；`reward` 随训练上升。`--use_gym_env true` 让环境的 `total_reward` 被直接消费，无需任何奖励函数。

**为什么不需要 `--reward_funcs`？** 看 GRPO 训练器的两处逻辑：① 校验放宽——`if not self.reward_funcs and not self.use_gym_env and not self._has_teacher` 才报错（[swift/rlhf_trainers/grpo_trainer.py:125-126](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L125-L126)）；② gym 奖励并入奖励矩阵——`if self.use_gym_env: self.reward_func_names.append('gym_reward')`（[swift/rlhf_trainers/grpo_trainer.py:2166-2170](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L2166-L2170)），把 `total_reward` 当成一列奖励函数，可与 `--reward_funcs` 通过 `--reward_weights` 混合。

**预期结果：** 若无 8 卡环境，标记「待本地验证」；但仍应能口述一轮：模型输出 `<action>down</action>` → `GYMScheduler.on_turn_end` 调 `FrozenLakeEnv.step` 移动 → `GYMScheduler.step` 把新网格渲染作为 user 消息追加 → 下一轮模型看到新网格。

#### 4.2.5 小练习与答案

**练习 1.** 为什么 `GYMScheduler` 要用 `req.uuid` 而不是数组下标来索引环境？

> **答：** 因为多轮驱动器（尤其 colocate 模式的 `run_multi_turn`）会在每一轮动态筛选「尚未结束的 request」继续推理，下标集合会变化且跨 rank 难以对齐；而 `uuid` 是每条 request 的稳定身份（见 `RolloutInferRequest.uuid`），无论 request 在哪一轮、被哪个进程处理，都能找回它对应的环境实例。

**练习 2.** `GYMScheduler` 没有覆写 `run()`，为什么它能实现多轮环境交互？

> **答：** 因为它把环境逻辑全部塞进了两个通用钩子：`on_trajectory_start`（= `env.reset`）和 `on_turn_end`（= `env.step` + 累计奖励 + 返回 done），再在 `step` 里把观测回填进 messages。基类 `MultiTurnScheduler.run()` 已经实现了「生成→钩子→判停→step→下一轮」的骨架，会自动调用这些钩子。这正是「部分定制」路径的价值。

**练习 3.** 如果想让 FrozenLake 在「到达终点」之外，再叠加一个「步数越少越好」的奖励，应该改哪里？

> **答：** 两种方式：① 直接在 `FrozenLakeEnv.step` 的返回 reward 里减去一个步数惩罚（环境自带奖励）；② 保留环境原奖励，额外加一个 `--reward_funcs`（如自定义步数奖励函数），用 `--reward_weights` 与 `gym_reward` 混合。方式 ② 更解耦，因为 gym 的 `total_reward` 已作为独立奖励列参与加权求和。

### 4.3 agent_loop 与 OpenEnv：工具循环驱动

#### 4.3.1 概念说明

前两节的 `run()` 是 **server 模式**（`swift rollout` 起一个 vLLM 服务，调度器在服务的异步事件循环里跑）。但 GRPO 训练还有一种主流部署：**colocate 模式**——vLLM 与训练器同进程，训练循环是同步的，每个 generation 步骤同步调一次 rollout。问题是：**colocate 模式没有现成的异步事件循环来跑那些 `async` 钩子**。

`swift/rollout/agent_loop.py` 就是为解决这个矛盾而生。它的 `run_multi_turn()` 函数把 server 模式 `run()` 的循环逻辑「搬」到同步上下文里：建一个临时事件循环，把每一轮的钩子 `gather` 起来跑完，再回到同步世界调 `rollout_fn`（一次推理）。它还参数化了两个后端相关的动作，使同一份循环能在 HF Accelerate、Megatron、Megatron-Ray 三种进程组里复用。

本节的另一条线是 **OpenEnv**：当环境不是 Python 类，而是一个**外部 HTTP/WebSocket 服务**（如 TextArena 的数独环境）时，ms-swift 用 `OpenEnvWrapper` 封装它的客户端，再用 `OpenEnvScheduler`（继承 `GYMScheduler`）把同步的 WebSocket 调用塞进异步钩子。

#### 4.3.2 核心流程

colocate 模式多轮的核心难点是**分布式终止一致性**：一个 generation batch 跨多个 rank，不同 rank 持有不同的 request 子集，必须让所有 rank 对「是否还有轨迹没跑完」达成一致，否则有的 rank 会提前退出、有的还在等。`run_multi_turn` 用 `gather_fn` 解决：

```text
每一轮：
  has_local_data  = 本 rank 还有未结束的 request？
  has_global_data = gather_fn([has_local_data])   ← 跨 rank 汇总（默认恒等，单进程用）
  if not any(has_global_data): break               ← 全员都没活儿了才退出

  对本 rank 仍存活的 request：
    on_turn_end(...)（用 loop.run_until_complete 跑协程）
    判停 → 存活的进 next_turn_index_to_infer
  rollout_fn(存活 requests or [], cfg)              ← 本 rank 没活儿也传空列表，但只要全局有活儿就继续
```

`rollout_fn` 是「跑一轮推理」的回调，由各后端注入（HF/Megatron/Ray 各自的实现）；`gather_fn` 是「跨 rank 汇总布尔列表」的回调，遵循 accelerate 的 `gather_object` 约定。这两个函数是 `run_multi_turn` 仅有的两个参数化点，其余循环逻辑与 server 模式的 `run()` 完全一致——文件头注释强调「循环体逐字搬自 trainer 的 colocate 实现」。

OpenEnv 这条线的流程：

```text
OpenEnvScheduler（继承 GYMScheduler）
  _create_env → OpenEnvWrapper(env_config)        # 不再是 Env 子类
  on_trajectory_start → wrapper.reset()（同步！）   # 用 asyncio.to_thread 包一层，避免阻塞事件循环
  on_turn_end：
    action_text = 模型回答
    action_dict = self.parse_action(action_text)   # LLM 文本 → dict（如 "[3 5 7]" → {'message':'[3 5 7]'}）
    obs, reward, done, meta = wrapper.step(action_dict)
    next_obs = self.format_observation(obs)        # dict → 字符串喂给 LLM
  format_observation / parse_action 可覆写，免去 openenv_* 命令行参数
```

#### 4.3.3 源码精读

**`run_multi_turn` 的函数签名与定位。** 文件头注释把它的设计意图说得非常清楚：循环体逐字来自 trainer，只参数化 `rollout_fn` 与 `gather_fn`。

[swift/rollout/agent_loop.py:1-27](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/agent_loop.py#L1-L27) — 模块文档与类型别名 `RolloutFn` / `GatherFn`。注意 `gather_fn` 必须传列表（每 rank 的贡献），传标量会崩 accelerate。

**`invoke_async_hook`：从同步上下文跑异步钩子。** colocate 模式下 `on_trajectory_start` 需要在第一轮生成前先调一次（让模型看到真实环境问题而非占位符），这个工具函数为此而生——新建临时事件循环跑完协程即销毁。

[swift/rollout/agent_loop.py:34-45](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/agent_loop.py#L34-L45) — `invoke_async_hook`。trainer 的 `rollout_mixin` 正是用它先调 `on_trajectory_start` 再开 rollout。

**`run_multi_turn` 的分布式终止判定。** 这是本节最值得读的片段：`has_local_data` 经 `gather_fn` 汇总成 `has_global_data`，只要全局还有任意 rank 有活儿，循环就不停；本 rank 没活儿时传空列表给 `rollout_fn` 但仍参与循环。

[swift/rollout/agent_loop.py:109-113](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/agent_loop.py#L109-L113) — 全局终止判定。

[swift/rollout/agent_loop.py:132-146](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/agent_loop.py#L132-L146) — 用 `loop.run_until_complete(_gather_turn_ends())` 并发跑本 rank 所有存活 request 的 `on_turn_end`，并合并 `rollout_infos`、计算各自的 `should_stop`。

**trainer 如何调用 `run_multi_turn`。** 这是把调度器接入 colocate 训练的接线点：先 `invoke_async_hook(scheduler.on_trajectory_start(requests))` 注入初始观测，再用 `run_multi_turn` 驱动多轮，`rollout_fn` 绑定到 trainer 自己的 `_rollout`。

[swift/rlhf_trainers/rollout_mixin.py:1090-1115](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout_mixin.py#L1090-L1115) — trainer 的多轮入口：先 `on_trajectory_start`（注释说明「让模型看到真实环境问题，而非占位符」），再 `run_multi_turn`。注：此处引用的是 `swift/rlhf_trainers/rollout_mixin.py`，行号基于当前 HEAD。

**`OpenEnvWrapper`：同步 WebSocket 客户端薄封装。** 它故意**不继承 `Env`**，只管连接管理与重试，把动作解析/观测格式化的职责留给调度器。`reset`/`step`/`close` 都是同步阻塞调用（`GenericEnvClient.sync()` 本就是阻塞的）。

[swift/rollout/openenv_wrapper.py:24-51](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/openenv_wrapper.py#L24-L51) — `OpenEnvWrapper` 与 `_ensure_client`：懒加载 `GenericEnvClient` 的同步版本。

[swift/rollout/openenv_wrapper.py:65-106](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/openenv_wrapper.py#L65-L106) — `_call_with_retry`（最多 3 次、指数退避重连）与 `reset`/`step` 的公开 API。

**`OpenEnvScheduler`：把同步 wrapper 塞进异步钩子。** 它覆写 `_create_env` 返回 `OpenEnvWrapper`，并在 `on_turn_end` 里用 `wrapper.step`（注意：基类 `GYMScheduler` 的 `on_turn_end` 是 `await env.step`，而 OpenEnv 的 step 是同步的）。关键差异是多了 `parse_action`（LLM 文本 → dict）与 `format_observation`（dict → 字符串）两个可覆写方法。

[swift/rollout/multi_turn.py:829-959](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L829-L959) — `OpenEnvScheduler`。重点看 L868-L900 的 `on_trajectory_start`（用 `asyncio.Semaphore` 限并发建连，避免压垮 OpenEnv 服务）、L902-L928 的 `on_turn_end`（`parse_action` → `wrapper.step`）、L930-L958 的 `parse_action`/`format_observation` 默认实现。

**真实示例：`SudokuScheduler`。** 它继承 `OpenEnvScheduler`，把数独环境接入多轮 GRPO，展示了如何覆写 `parse_action`（从模型输出抽 `[row col number]`）、`on_turn_end`（多奖励打分）与 `on_trajectory_start`（解析棋盘、生成提示）。注册语句 `multi_turns['sudoku_scheduler'] = SudokuScheduler` 让它能被 `--external_plugins` 加载。

[examples/train/grpo/plugin/openenv/sudoku_scheduler.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/grpo/plugin/openenv/sudoku_scheduler.py) — `SudokuScheduler` 全文（自定义 OpenEnv 调度器的范本）。

#### 4.3.4 代码实践（源码阅读型 + 可选运行）

**实践目标：** 理解 colocate 模式下多轮驱动如何在不阻塞训练循环的前提下跑异步钩子，并追踪一次「模型回答 → parse_action → wrapper.step → 观测回填」的工具调用循环。

**操作步骤：**

1. 在 [swift/rollout/agent_loop.py:109-249](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/agent_loop.py#L109-L249) 里追踪一次完整循环，标注：`has_global_data` 判定（L111-113）、`on_turn_end` 并发（L132-138）、判停与 `RolloutOutput` 组装（L149-206）、`step` 与存活集更新（L208-244）、`rollout_fn` 调用（L248）。
2. 在 [swift/rollout/multi_turn.py:902-928](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rollout/multi_turn.py#L902-L928) 追踪 `OpenEnvScheduler.on_turn_end`，画出「LLM 文本 → action_dict → wrapper.step → next_obs → format_observation → 回填 user 消息」的工具调用链。
3. （可选运行）若本地已起 OpenEnv 数独服务，参考 [examples/train/grpo/plugin/openenv/run_grpo_sudoku.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/grpo/plugin/openenv/run_grpo_sudoku.sh) 与 [examples/train/grpo/plugin/openenv/sudoku_scheduler.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/grpo/plugin/openenv/sudoku_scheduler.py) 跑一次小规模训练。

**需要观察的现象：** `parse_action` 解析失败时（`SudokuScheduler` 返回 `None`），`on_turn_end` 不是继续调用环境，而是直接给 -1 惩罚并 `done=True` 结束轨迹——避免污染环境状态。这是工具调用循环里一个重要的容错点。

**预期结果：** 能口述 colocate 模式下「同步训练循环 ↔ 临时事件循环 ↔ 异步钩子」的三层调用关系。若本地无 OpenEnv 服务，运行部分标记「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1.** `run_multi_turn` 为什么需要 `gather_fn`？直接用 `has_local_data` 判停会怎样？

> **答：** 多卡训练时一个 generation batch 分散在各 rank，不同 rank 手里的轨迹结束时间不同。若只用 `has_local_data`，会出现「rank 0 已全部结束、rank 1 还在跑」的情况：rank 0 退出循环后不再调 `rollout_fn`，而 rank 1 还在等 collective 通信，导致死锁或 NCCL 超时。`gather_fn` 把「是否有活儿」汇总到全局，保证所有 rank 同时退出。

**练习 2.** `OpenEnvWrapper.step` 是同步阻塞的，但 `OpenEnvScheduler.on_turn_end` 是 `async` 的，二者如何协作而不卡死事件循环？

> **答：** `OpenEnvWrapper` 基于 `GenericEnvClient.sync()`，本质是阻塞的 WebSocket I/O。`OpenEnvScheduler` 的 `on_turn_end` 在调用 `wrapper.step` 时（或子类如 `SudokuScheduler`）会用 `await asyncio.to_thread(wrapper.step, action_dict)` 把阻塞调用丢到线程池，从而不阻塞事件循环，其他轨迹的协程仍可并发推进。注意基类 `GYMScheduler` 用的是 `await env.step`（因为 gym 的 `Env.step` 本身是协程），`OpenEnvScheduler` 因 wrapper 是同步的才需要这一层转换。

**练习 3.** 为什么 `OpenEnvWrapper` 故意不继承 `Env`？

> **答：** 职责分离。`Env` 的 `reset`/`step` 接收/返回的是「业务语义」对象（observation 字符串、Messages 动作），而 `OpenEnvWrapper` 只负责「连接管理与原始收发」，返回的是未格式化的服务端原始对象。把「LLM 文本 ↔ dict」的解析留给 `OpenEnvScheduler` 的 `parse_action`/`format_observation`，使同一份 wrapper 能被不同环境复用，也让子类能按需定制解析逻辑（如数独的 `[row col number]` 解析）。

## 5. 综合实践

**任务：** 给 FrozenLake 写一个「带奖励整形」的多轮 GRPO 配置，并端到端解释一条轨迹里 RolloutScheduler 的行为。

要求：

1. **读懂现有配置。** 阅读 [examples/megatron/grpo/multi_turn/frozen_lake.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/grpo/multi_turn/frozen_lake.sh) 与 [examples/megatron/grpo/multi_turn/frozen_lake_plugin.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/megatron/grpo/multi_turn/frozen_lake_plugin.py)，回答：当前配置用的是哪个调度器？奖励从哪来？为什么 `max_completion_length=512` 而 `max_length=6120`、`vllm_max_model_len=6632`（提示：512 是每轮生成长度，6120 ≈ 前 9 轮历史 + prompt，6632 = 6120 + 最后一轮）。

2. **加一个奖励整形（二选一）。**
   - 方式 A（环境内整形）：修改 `FrozenLakeEnv.step`，在「到达终点」的 1.0 奖励基础上，减去 `0.05 * self.steps`，鼓励更短路径。重新跑训练，观察 reward 曲线是否更早收敛但峰值略降。
   - 方式 B（环境外混合）：不改环境，新增一个 `--reward_funcs steps_penalty`（参考 u7-l3 自定义奖励函数），用 `--reward_weights` 把 `gym_reward` 与 `steps_penalty` 按 1 : 0.05 混合。

3. **画一张轨迹时序图。** 针对 `seed=0` 的一条轨迹，画出从 `{"messages":[{"role":"user","content":"<placeholder>"}],"env_config":{"seed":0}}` 开始，到轨迹结束的完整时序，至少包含：`on_trajectory_start`（env.reset 覆盖 placeholder）、第 1/2/3 轮的 `infer → on_turn_end(env.step) → step(回填观测)`、以及结束条件（到达 G / 掉洞 / 撞 max_turns）。

4. **验证点。** 说明为什么 `--use_gym_env true` 时可以不传 `--reward_funcs`（引用 [swift/rlhf_trainers/grpo_trainer.py:125-126](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L125-L126) 与 [swift/rlhf_trainers/grpo_trainer.py:2166-2170](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L2166-L2170)）。

**说明：** 本任务把本讲三个最小模块串起来——4.1 的调度循环、4.2 的 gym 环境与奖励消费、4.3 的 colocate 驱动（frozen_lake.sh 用的是 `--vllm_mode colocate`，故走 `run_multi_turn`）。若本地无 8 卡环境，方式 A/B 的运行标记「待本地验证」，但时序图与验证点必须能完整口述。

## 6. 本讲小结

- **多轮 Rollout 的本质**是给推理引擎外面包一层「生成→环境反馈→再生成」的状态机，这层状态机就是 `RolloutScheduler`；它的根抽象把「单轮」实现为默认 `run()`，把「多轮」抽象为 `MultiTurnScheduler` 的「生成→钩子→判停→step」循环。
- **两条定制路径**：全定制（覆写 `run()`，如 `ThinkingModelTipsScheduler`，可返回动态数量的 `RolloutOutput`）与部分定制（实现 `step()` + 可选 `check_finished()`，如 `MathTipsScheduler`）。判停三级优先：`on_turn_end['done']` > `check_finished` > `max_turns` 兜底。
- **GYMScheduler 是「部分定制 + 通用钩子」的典范**：它不覆写 `run()`，只把 `env.reset` 挂到 `on_trajectory_start`、`env.step` 挂到 `on_turn_end`、观测回填挂到 `step`，用 `req.uuid` 给每条轨迹一个独立环境实例。
- **`Env` 抽象**借鉴 Gym 但适配 RL：`reset` 返回 `(obs, info, system_message)`，`step` 接收 `Messages`（对话即动作）；`--use_gym_env true` 时环境的 `total_reward` 作为一列奖励（`gym_reward`）直接并入奖励矩阵，可省去 `--reward_funcs`。
- **`agent_loop.run_multi_turn`** 是与后端无关的 colocate 驱动器：用临时事件循环跑异步钩子，用 `gather_fn` 解决多 rank 终止一致性，只参数化 `rollout_fn`/`gather_fn` 即可在 HF/Megatron/Ray 复用。
- **OpenEnv 接入**：`OpenEnvWrapper` 是同步 WebSocket 薄封装（不继承 `Env`），`OpenEnvScheduler` 用 `parse_action`/`format_observation` 做「LLM 文本 ↔ dict」转换，用 `asyncio.to_thread`/`Semaphore` 把阻塞 I/O 与并发建连管好。

## 7. 下一步学习建议

- **接 u7 后续 / u8 部署**：多轮 Rollout 依赖 `swift rollout` 服务，建议读 u8-l2（部署与服务化）理解 `SwiftRolloutDeploy` 的 FastAPI 服务端如何把 `infer` 端点接到调度器，以及权重热同步（`WeightSyncWorkerExtension`）如何让 vLLM 在每步 rollout 后拿到最新策略权重。
- **深入权重同步**：本讲刻意略过了 `swift/pipelines/infer/rollout.py` 里的 NCCL/CUDA-IPC 权重同步细节（`update_flattened_params` / `update_weights_from_ipc`），这是多轮 GRPO 能在线训练的关键。建议直接读 [swift/pipelines/infer/rollout.py:112-563](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/rollout.py#L112-L563)。
- **分布式训练背景**：`run_multi_turn` 的 `gather_fn` 假设你熟悉 `accelerate.gather_object`，若不熟，先补 u9-l1（分布式训练基础）。
- **动手扩展**：参考 `SudokuScheduler` 与 `frozen_lake_plugin.py`，为自己业务里的「工具/环境」写一个 `Env` 子类 + 注册到 `envs`，或写一个继承 `GYMScheduler`/`OpenEnvScheduler` 的调度器注册到 `multi_turns`，用 `--external_plugins` 加载。这是检验本讲是否吃透的最佳方式。
