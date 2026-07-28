# main_ppo 入口与组件拼装

## 1. 本讲目标

前面几讲我们已经走完了「数据→parquet→token 张量→DataProto→Ray 资源池」的整条管线，但在所有这些零件和真实的训练循环之间，还缺一个**总装车间**：它读入 Hydra 解析好的配置，把 tokenizer、各种 Worker、资源池、奖励函数一股脑拼起来，最后点火启动训练。这个总装车间就是 `verl/trainer/main_ppo.py`。

本讲学完后，你应该能够：

1. 说清 `main_task` 从「一份配置」到「`trainer.fit()` 跑起来」的完整装配步骤与顺序。
2. 解释 `role_worker_mapping` / `resource_pool_spec` / `mapping` 这三张表各自的作用，以及它们如何把抽象的「角色」绑定到具体的 GPU 资源。
3. 理解 `_select_rm_score_fn` 如何用 `data_source` 字符串路由到对应的规则奖励函数，并知道接入一个**新任务**需要改哪三处代码。
4. 看懂 `RewardManager` 在「model-based 奖励」与「rule-based 奖励」之间是如何切换的。

---

## 2. 前置知识

本讲需要你已经具备以下心智模型（来自前置讲义，这里只做一句回顾，不展开）：

- **Hydra 配置流动**（u1-l4）：`ppo_trainer.yaml` 默认值 + 命令行覆盖，在 `@hydra.main` 处拼合成一份 `config` 对象。本讲看到的 `config` 就是它解析后的结果。
- **规则奖励函数**（u2-l4）：每个任务有一个 `compute_score(solution_str, ground_truth)`，把模型回答判成 0/0.1/1.0 等标量分；`data_source` 字段决定用哪个 `compute_score`。
- **DataProto 数据协议**（u3-l1）：统一数据容器，含 `batch`（张量）与 `non_tensor_batch`（如 `data_source`、`ground_truth`）。本讲里 `RewardManager` 收到的就是它。
- **单控制器与 Ray 资源池**（u3-l2）：driver 进程编排、Ray worker 计算；`RayResourcePool` 划分 GPU、`max_colocate_count` 决定一张卡挤几个角色。本讲会看到 driver 侧如何把角色「映射」到资源池。

几个本讲会用到的术语：

| 术语 | 含义 |
| --- | --- |
| **strategy** | 训练后端，取值 `fsdp` 或 `megatron`，决定从哪个模块导入 Worker 实现。 |
| **Role** | 抽象角色枚举（Actor / Critic / RefPolicy / RewardModel 等），是「逻辑身份」。 |
| **colocate** | 把多个角色合体进同一进程共享显存，见 u3-l2。 |
| **reward_fn / val_reward_fn** | 训练用 / 验证用的奖励函数；二者都是 `RewardManager` 实例，仅 `num_examine` 不同。 |

---

## 3. 本讲源码地图

本讲只精读一个文件，但会引用两个相邻文件作为上下文：

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| [verl/trainer/main_ppo.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py) | **本讲主角**：训练入口，做组件拼装与奖励路由。 | 全文 193 行。 |
| [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) | `RayPPOTrainer` 训练主循环所在（下一讲 u4-l2 精读）。 | 仅引用其中的 `Role` 枚举与 `ResourcePoolManager`。 |
| [verl/utils/reward_score/countdown.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py) | countdown 任务的规则奖励函数。 | 仅引用 `compute_score` 的函数签名。 |

`main_ppo.py` 自上而下分四块：模块顶部 import（L18-L21）、奖励函数路由 `_select_rm_score_fn`（L24-L34）、奖励管理器 `RewardManager`（L37-L90）、以及真正的入口 `main` + `main_task`（L97-L193）。本讲按「先看全局装配，再钻进奖励」的顺序讲解。

---

## 4. 核心概念与源码讲解

### 4.1 main_task：从配置到训练的装配流水线

#### 4.1.1 概念说明

`main_task` 是一个被 `@ray.remote` 装饰的远程函数（在 Ray 集群里执行，而不是 driver 本地）。它本身**不含任何训练算法**，它的全部职责是「装配」：拿到一份已经解析好的 `config`，依次把 tokenizer、Worker 类、资源池、奖励函数这些零件拧在一起，最后交给 `RayPPOTrainer` 去跑。

可以把它想象成一条流水线：配置进来 → 选后端 → 列三张表 → （可选）挂奖励模型 → 装两个奖励函数 → 建资源池管理器 → 实例化 trainer → `init_workers()` 把模型建到各 GPU → `fit()` 开训。

#### 4.1.2 核心流程

`main_task` 的执行步骤（对应下方源码行号）：

```text
1. 打印并 resolve 配置              (L114-L115)
2. 从 HDFS/本地拿到模型权重路径      (L118)
3. 构造 tokenizer                   (L121-L122)
4. 按 strategy 选 Worker 实现与      (L125-L138)
   WorkerGroup 类
5. 组装三张表：                       (L142-L156)
   - role_worker_mapping  角色 → Worker 类
   - resource_pool_spec   池名 → 每节点进程数
   - mapping              角色 → 池名
6. 若启用 reward_model，追加          (L164-L172)
   RewardModel 角色到前两张表
7. 实例化 reward_fn / val_reward_fn  (L174-L177)
8. 构造 ResourcePoolManager          (L179)
9. 实例化 RayPPOTrainer，             (L181-L189)
   调 init_workers() → fit()
```

其中第 4、5 步是理解「组件如何绑定到硬件」的关键，我们重点讲。

#### 4.1.3 源码精读

**先看入口与点火（L97-L103）**：`main` 是 Hydra 入口，它只做两件事——初始化本地 Ray 集群，然后把真正的活儿交给远程的 `main_task`：

[verl/trainer/main_ppo.py:L97-L103](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L97-L103) —— `@hydra.main` 装饰的 `main` 负责起 Ray 并把 config 远程投递给 `main_task`。

注意 `ray.get(main_task.remote(config))`：`main_task.remote` 只是把任务**提交**到集群返回一个 future，`ray.get` 才会阻塞 driver 直到训练结束。这样 driver 进程与实际跑训练的进程就分离开了。

**第 4 步：按 strategy 选后端（L125-L138）**：

[verl/trainer/main_ppo.py:L125-L138](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L125-L138) —— 当 `actor.strategy == 'fsdp'` 时，从 `verl.workers.fsdp_workers` 导入 `ActorRolloutRefWorker`/`CriticWorker`，WorkerGroup 用普通的 `RayWorkerGroup`；选 `megatron` 时则换成 megatron 版实现与 `NVMegatronRayWorkerGroup`。

两个关键细节：

- 第 126、132 行各有一句 `assert config.actor_rollout_ref.actor.strategy == config.critic.strategy`——**actor 与 critic 必须用同一种后端**，不能一个 FSDP 一个 Megatron。这是因为二者要共享同一套权重分片/通信基础设施。
- 这里的 import 写在 `if` 分支**内部**是故意的：FSDP 与 Megatron 的依赖很重且互斥，按需导入可以避免在一台只装了 FSDP 的机器上因为 import Megatron 而报错。

**第 5 步：组装三张表（L142-L156）**——这是整段代码的「接线图」：

[verl/trainer/main_ppo.py:L142-L156](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L142-L156) —— 三张表把「逻辑角色」绑定到「物理资源」。

把三张表连起来看，就是一条两跳的链路：

```text
Role（角色）  ──role_worker_mapping──▶  Worker 类（怎么算）
Role（角色）  ──mapping──────────────▶  池名（global_pool）
池名          ──resource_pool_spec───▶  每节点 GPU 数列表（放哪）
```

具体到 TinyZero 的默认配置：

- `role_worker_mapping` 里 `Role.ActorRollout` 和 `Role.RefPolicy` **都指向同一个 `ActorRolloutRefWorker` 类**——这正是 u6-l1 要讲的「混合引擎」：一个 Worker 类同时扮演 actor/rollout/ref 三个角色。
- `resource_pool_spec` 把 `global_pool` 定义为 `[n_gpus_per_node] * nnodes`，即一个长度为节点数、每项是该节点 GPU 数的列表，它就是 u3-l2 里 `RayResourcePool` 期待的 `process_on_nodes`。
- `mapping` 里三个角色**全部指向 `global_pool`**：它们要被 colocate 到同一组 GPU 上共享显存。

> 关于 `Role` 枚举的真实定义，见 [verl/trainer/ppo/ray_trainer.py:L41-L51](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L41-L51)，其中 `ActorRolloutRef = 6` 正是混合引擎用的那个角色。

**第 6 步：可选的奖励模型（L164-L172）**：

[verl/trainer/main_ppo.py:L164-L172](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L164-L172) —— 只有 `config.reward_model.enable == True` 时，才会把 `RewardModelWorker` 加进两张表。

TinyZero 默认 `reward_model.enable = False`（见 [verl/trainer/config/ppo_trainer.yaml:L122](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L122)），所以这一段在 TinyZero 里**根本不执行**——奖励完全来自规则函数（4.3 节）。这段代码保留的是 veRL 框架的通用能力：当你想用神经网络打分（例如 RLHF 里的奖励模型）时才打开它。

**第 8-9 步：建管理器、点火（L179-L189）**：

[verl/trainer/main_ppo.py:L179-L189](https://github.com/Jiayi-Pan-TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L179-L189) —— `ResourcePoolManager` 持有上面的两张表，`RayPPOTrainer` 接收全部组件，`init_workers()` 才真正在 GPU 上建模型，`fit()` 进入训练主循环。

`ResourcePoolManager` 本身是个 dataclass，见 [verl/trainer/ppo/ray_trainer.py:L54-L76](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L54-L76)：它把 `resource_pool_spec` 翻译成一堆 `RayResourcePool`，并用 `mapping` 提供「按角色查池」的 `get_resource_pool(role)` 方法。注意第 71 行 `max_colocate_count=1` 是**硬编码**的——这正是 u3-l2 讲过的 FSDP 后端推荐设置：把所有角色合体进同一进程共享显存。

#### 4.1.4 代码实践

**实践目标**：通过追踪 `strategy` 分支与三张表，验证你对「角色→资源」绑定的理解。

**操作步骤**：

1. 打开 [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml)，确认 `actor_rollout_ref.actor.strategy`（约 L87）与 `critic.strategy`（约 L88）的默认值，并确认 `reward_model.enable`（L122）为 `False`。
2. 对照 [main_ppo.py:L142-L156](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L142-L156)，在纸上画一张表：列出 `ActorRollout` / `Critic` / `RefPolicy` 三个角色各自对应的 **Worker 类** 与 **池名**。
3. 思考：如果 `reward_model.enable` 改成 `True`，第 6 步会给 `role_worker_mapping` 和 `mapping` 各新增一条什么记录？

**需要观察的现象**：三个角色共享同一个 `global_pool`；`ActorRollout` 与 `RefPolicy` 共用同一个 Worker 类。

**预期结果**：你应当能画出「角色 → Worker 类 / 池名」的映射，并解释为什么 `ActorRollout` 和 `RefPolicy` 用同一个类（因为混合引擎复用同一份模型代码）。本实践为**源码阅读型**，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main` 函数里要用 `ray.get(main_task.remote(config))`，而不是直接调用 `main_task(config)`？

> **答案**：`main_task` 被 `@ray.remote` 装饰，直接调用拿不到结果（它变成了远程对象句柄）。`remote()` 提交任务到 Ray 集群、返回 future，`ray.get` 才阻塞 driver 等待训练真正结束。这样训练逻辑跑在 worker 进程，driver 只负责编排。

**练习 2**：如果把 `actor.strategy` 设为 `fsdp`、`critic.strategy` 设为 `megatron`，会发生什么？

> **答案**：第 126 行的 `assert config.actor_rollout_ref.actor.strategy == config.critic.strategy` 会抛 `AssertionError`，训练根本启动不了。框架强制两种角色共用同一后端。

**练习 3**：`resource_pool_spec` 里 `[n_gpus_per_node] * nnodes` 这个列表的语义是什么？

> **答案**：列表长度等于节点数 `nnodes`，每个元素是该节点的 GPU 进程数（这里等于 GPU 数）。它就是 u3-l2 中 `RayResourcePool` 的 `process_on_nodes` 参数，决定了 world_size = 列表所有元素之和。

---

### 4.2 RewardManager：把回答变成奖励张量

#### 4.2.1 概念说明

`main_task` 在第 7 步实例化了两个 `RewardManager`，分别作为训练奖励 `reward_fn` 和验证奖励 `val_reward_fn`，随后传给 `RayPPOTrainer`。`RewardManager` 是一个**可调用对象**（实现了 `__call__`）：吃进一个 `DataProto`（里面是 prompt+response+ground_truth），吐出一个形状为 `[batch, response_length]` 的奖励张量。

它有一个精巧的双重身份：

- **model-based 模式**：如果上游（神经网络奖励模型 `RewardModelWorker`）已经把分数算好放进 `data.batch['rm_scores']`，`RewardManager` 直接原样返回，不做任何计算。
- **rule-based 模式**：否则，它逐条解码回答，调用规则 `compute_score` 打分。TinyZero 走的就是这条。

#### 4.2.2 核心流程

`RewardManager.__call__` 的处理流程：

```text
1. 若 data.batch 已含 'rm_scores'：     (L49-L50)
     直接返回（model-based 短路）
2. 否则建一个全 0 的 reward_tensor，      (L52)
   形状同 responses
3. 对每条样本 i：                         (L56-L81)
   a. 从 prompts + attention_mask 切出
      有效 prompt（去左填充）
   b. 从 responses + attention_mask 切出
      有效 response 及其长度
   c. 拼接解码成字符串
   d. 取 ground_truth 与 data_source
   e. 按 data_source 路由 compute_score_fn
   f. 算出标量 score，放到 reward_tensor
      的「最后一个有效 response token」位
4. 每个 data_source 只打印前 num_examine 条 (L83-L88)
5. 返回 reward_tensor
```

#### 4.2.3 源码精读

**构造函数（L41-L43）**：

[verl/trainer/main_ppo.py:L41-L43](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L41-L43) —— `num_examine` 是「每个 data_source 往控制台打印几条解码结果」的数量。

回看 `main_task` 第 7 步：训练用的 `reward_fn` 传 `num_examine=0`（不打印，避免刷屏），验证用的 `val_reward_fn` 传 `num_examine=1`（每次验证只看一条样本长什么样）。这就是 `reward_fn` 与 `val_reward_fn` 的唯一区别。

**model-based 短路（L49-L50）**：

[verl/trainer/main_ppo.py:L45-L52](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L45-L52) —— 若 batch 里已有 `rm_scores`，直接返回；否则建全零奖励张量。

注意 `torch.zeros_like(data.batch['responses'])`：奖励张量与 `responses` **同形**，即 `[batch_size, response_length]`。奖励最终只会写在每行的一个位置上（末位有效 token），其余保持 0——这正是 RL 里常见的「**稀疏奖励**」：整段回答只有一个非零分。

**逐条解码与打分（L56-L81）**——这是核心：

[verl/trainer/main_ppo.py:L56-L81](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L56-L81) —— 切出有效 prompt/response、解码、路由奖励函数、把标量放到末位 token。

几个要点：

- **为什么要算 `valid_prompt_length`？** 因为 prompt 是**左填充**的（u2-l3 讲过，左填充是为了 rollout 能在右端续写）。真实的 prompt token 堆在序列右端，前面是一串 pad。`attention_mask[:prompt_length].sum()` 数出有效长度，再用 `prompt_ids[-valid_prompt_length:]` 切出真实内容，丢掉左边的 pad。
- **`reward_tensor[i, valid_response_length - 1] = score`**：把标量分数挂在**该回答最后一个有效 token**上。后续 PPO 计算优势函数时（u5-l1），这个分数会从这个位置反向传播回整条序列。如果挂在错的位置，梯度信号就错了。
- **`ground_truth` 与 `data_source` 都来自 `non_tensor_batch`**（L74、L77），印证 u3-l1 所讲：非张量字段（任务答案、任务类型）一直跟着样本走到这里。

#### 4.2.4 代码实践

**实践目标**：理解左填充结构如何影响「有效长度」与奖励落点。

**操作步骤**：

1. 阅读上面的 L56-L81，回答：如果一条样本的 prompt 长度是 10、其中真实 token 占 6（前 4 个是 pad），`valid_prompt_length` 等于几？`valid_prompt_ids` 取的是 `prompt_ids` 的哪几个位置？
2. 假设某条回答 response 长度为 20、有效 token 8（后 12 个是 pad），`score = 1.0` 会被写到 `reward_tensor[i, ?]` 的第几列？
3. 解释：为什么 `compute_score_fn(solution_str=sequences_str, ground_truth=ground_truth)` 用的是**关键字参数**而不是位置参数。

**需要观察的现象 / 预期结果**：

1. `valid_prompt_length = 6`，`valid_prompt_ids = prompt_ids[-6:]`（取最后 6 个，正是去掉了前 4 个 pad）。
2. 写到 `reward_tensor[i, 7]`（即 `valid_response_length - 1 = 8 - 1 = 7`）。
3. 用关键字参数是为了与 `compute_score` 的签名 `compute_score(solution_str, ground_truth, method='strict', format_score=0.1, score=1.)`（见 [verl/utils/reward_score/countdown.py:L59](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L59)）对齐，避免把 `ground_truth` 误传给 `method`。本实践为**源码阅读型**，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`reward_fn` 和 `val_reward_fn` 都是 `RewardManager` 实例，它们的区别是什么？为什么要分两个？

> **答案**：唯一区别是 `num_examine`：训练用 `0`（不打印），验证用 `1`（打印一条样本）。分开是为了在验证时能肉眼看到模型生成的回答长什么样，而训练时每步都打印会刷屏。逻辑（打分）完全一样。

**练习 2**：为什么奖励写在 `reward_tensor[i, valid_response_length - 1]` 而不是 `[i, -1]`？

> **答案**：response 是右填充的，序列最末尾可能是 pad 而非真实 token。`valid_response_length - 1` 才是「最后一个真实生成的 token」的位置。奖励必须挂在真实 token 上，后续 GAE/优势反传才有意义。

**练习 3**：在 model-based 模式下，`RewardManager` 自己会去调用神经网络奖励模型吗？

> **答案**：不会。`rm_scores` 是上游 `RewardModelWorker`（通过 `rm_wg`）**事先算好**放进 `data.batch` 的。`RewardManager` 只负责「如果有就直接返回」的短路，它本身不跑任何模型前向。

---

### 4.3 _select_rm_score_fn：按 data_source 路由奖励函数

#### 4.3.1 概念说明

`_select_rm_score_fn` 是一个**纯路由函数**：给它一个 `data_source` 字符串，返回对应的 `compute_score` 可调用对象。它是 TinyZero 接入新任务的**核心扩展点**——只要你的数据在 parquet 里写对了 `data_source`（u2-l1 讲过），再在这里加一行分支，新任务的奖励就接进训练了。

注意它是「子串包含」匹配而非精确相等（除了前两个），这给了数据源命名一些弹性：`'yolo/multiply-3_digit'`、`'yolo/arithmetic-3_digit'` 都能命中 `"multiply" in ...` / `"arithmetic" in ...` 分支。

#### 4.3.2 核心流程

路由逻辑是一个 if/elif 链：

```text
data_source == 'openai/gsm8k'      → gsm8k.compute_score
data_source == 'lighteval/MATH'    → math.compute_score
"multiply" in data_source          → multiply.compute_score
         或 "arithmetic" in data_source
"countdown" in data_source         → countdown.compute_score
其余                                → raise NotImplementedError
```

最后那个 `raise NotImplementedError` 很重要：如果你的数据里 `data_source` 拼错或没在这里登记，**训练一启动就会崩**——这比「默默给 0 分」安全得多，能在第一时间暴露配置错误。

#### 4.3.3 源码精读

[verl/trainer/main_ppo.py:L18-L34](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L18-L34) —— 顶部 import 把四个奖励模块拉进来，`_select_rm_score_fn` 按字符串路由。

值得注意的设计：

- **第 20 行一次性 import 四个模块**：`from verl.utils.reward_score import gsm8k, math, multiply, countdown`。要加新任务，这里就要多 import 一个模块（见下面的实践）。
- **函数返回的是「函数本身」而非「函数调用结果」**：`return countdown.compute_score`（没有括号）。调用时机交给 `RewardManager` 在 L80 执行 `compute_score_fn(solution_str=..., ground_truth=...)`。这种「注册函数、延迟调用」的模式让路由与打分彻底解耦。
- **匹配优先级**：if/elif 从上到下短路。当前顺序下 gsm8k 和 MATH 用精确匹配，其余用子串匹配。如果你新增的任务名里碰巧含 `"multiply"`，要小心被提前截胡——把更具体的分支放在前面。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：为一个新的 `'mytask'` 数据源接入你自己的 `compute_score`，把「数据—奖励—路由」三处改动跑通。

**背景**：现有的 `'countdown'`、`'yolo/multiply-3_digit'` 分支已经存在。现在假设你做了一个「两数求和」任务，`data_source` 命名为 `'mytask'`，要让它能被训练。需要改动 **三处代码**。

**操作步骤**（均为示例改动，需自行创建/编辑文件，**不要在阅读源码时实际修改**）：

1. **新建奖励函数文件**：在 `verl/utils/reward_score/` 下新建 `mytask.py`，实现一个最小 `compute_score`（签名要与现有约定一致）。示例代码：

   ```python
   # 示例代码：verl/utils/reward_score/mytask.py
   def compute_score(solution_str, ground_truth, method='strict', format_score=0.1, score=1.):
       # ground_truth 形如 {'target': 7}，期望模型在 <answer> 里写出两数之和
       answer = _extract_answer(solution_str)  # 你自己实现的提取逻辑
       if answer is None:
           return 0.0
       try:
           return score if abs(int(answer) - ground_truth['target']) < 1e-5 else format_score
       except Exception:
           return 0.0
   ```

   提取逻辑 `_extract_answer` 需复用 u2-l4 里 countdown 的 `<answer>` 标签正则思路。

2. **在 `main_ppo.py` 第 20 行的 import 中加入新模块**：

   ```python
   # verl/trainer/main_ppo.py 第 20 行，改为：
   from verl.utils.reward_score import gsm8k, math, multiply, countdown, mytask
   ```

3. **在 `_select_rm_score_fn` 里新增一个分支**（建议放在 `else` 之前）：

   ```python
   # verl/trainer/main_ppo.py _select_rm_score_fn 内，在 countdown 分支后：
   elif "mytask" in data_source:
       return mytask.compute_score
   ```

**第四处（数据侧，非 `main_ppo.py`）**：别忘了数据预处理脚本里 `make_map_fn` 要把 `data_source` 写成包含 `"mytask"` 的字符串（例如 `'mytask'`），否则 `RewardManager` 在 L77 取到的 `data_source` 进不了你的新分支。

**需要观察的现象**：

- 改完前：用 `data_source='mytask'` 的数据训练，会在 `RewardManager.__call__` 调用 `_select_rm_score_fn('mytask')` 时抛 `NotImplementedError`，训练立即崩溃。
- 改完后：`_select_rm_score_fn('mytask')` 返回 `mytask.compute_score`，每条样本被正常打分。

**预期结果**：三处代码改动 + 一处数据侧约定完成后，`'mytask'` 即可走通「打分」环节。注意：这只解决了**奖励路由**，要让训练真正跑起来还需正确生成 parquet 数据并通过 `train_tiny_zero.sh` 指向它——那部分在 u7-l3「自定义新任务」会端到端串起来。本实践为**源码阅读 + 示例编写型**，实际运行需本地 GPU 环境，故标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`_select_rm_score_fn` 返回的是 `countdown.compute_score` 而不是 `countdown.compute_score()`，这两种写法的区别是什么？

> **答案**：前者返回**函数对象本身**（不执行），由 `RewardManager` 在 L80 延迟调用；后者会**立刻执行**并返回结果，但此时还没有 `solution_str`/`ground_truth`，会报错。路由阶段只登记「用哪个函数」，不真正打分。

**练习 2**：为什么 `gsm8k`、`MATH` 用 `==` 精确匹配，而 `multiply`/`arithmetic`/`countdown` 用 `in` 子串匹配？

> **答案**：gsm8k/MATH 是单一固定数据源名（`'openai/gsm8k'`、`'lighteval/MATH'`），精确匹配最安全；而后者的数据源名带可变后缀（如 `'yolo/multiply-3_digit'`、`'yolo/arithmetic-3_digit'`），用子串匹配可以一次覆盖多个变体，省去为每个后缀各写一行。

**练习 3**：如果你新增任务的 `data_source` 取名叫 `'countdown-v2'`，会发生什么？

> **答案**：因为 `"countdown" in 'countdown-v2'` 为真，它会命中**已有的 countdown 分支**（L31-L32），返回 `countdown.compute_score`，而不是你为新任务写的函数。这说明命名要避免与现有子串冲突，或把更具体的分支放在 `elif` 链更前面。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，画出 `main_ppo` 从「配置」到「奖励打分」的完整数据与控制流图。

请完成以下内容：

1. **装配链路图**：以 `main(config)` 为起点，画出经过 `main_task` 的每一步（strategy 选择 → 三张表 → 可选 RewardModel → reward_fn/val_reward_fn → ResourcePoolManager → RayPPOTrainer → init_workers → fit），并在每个节点标注它**读 config 的哪个字段**、**产出什么对象**。
2. **奖励调用链追踪**：假设训练中 `RayPPOTrainer.fit()` 调用了 `reward_fn(data)`，请追踪：`RewardManager.__call__` → `_select_rm_score_fn(data_source)` → `countdown.compute_score(...)`，标出每一步的输入输出类型，并指出**奖励标量最终落在 reward_tensor 的哪个位置**。
3. **扩展设计**：基于 4.3.4 的实践，写出接入 `'mytask'` 任务所需改动的**全部位置清单**（含数据侧），并说明若忘记改 import 会报什么错（提示：`NameError` 还是 `NotImplementedError`？）。

**预期产出**：一张装配流程图 + 一段奖励调用链文字描述 + 一份改动清单。这张图会成为后续 u4-l2（RayPPOTrainer 初始化）、u4-l3（fit 主循环）、u4-l4（奖励计算细节）的「导航底图」——本讲只到「点火」，下一讲才钻进 `init_workers` 内部看 Worker 如何真正被创建。

---

## 6. 本讲小结

- `main_task` 是 `@ray.remote` 远程函数，职责是**装配**而非训练：它读 config、按 `strategy` 选 Worker 后端、组装三张表、建资源池管理器，最后交给 `RayPPOTrainer.init_workers().fit()`。
- 三张表是接线图：`role_worker_mapping`（角色→Worker 类）、`resource_pool_spec`（池名→每节点 GPU 数）、`mapping`（角色→池名）；TinyZero 默认三个角色全部 colocate 到同一个 `global_pool`。
- `RewardManager` 是可调用对象，有双重身份：batch 里已有 `rm_scores` 时直接返回（model-based），否则逐条解码并用规则函数打分（rule-based，TinyZero 走这条）。
- 奖励标量被挂在每条回答**最后一个有效 response token**上（`reward_tensor[i, valid_response_length-1]`），形成稀疏奖励，供后续优势函数反传。
- `_select_rm_score_fn` 是接入新任务的核心扩展点：新增一个任务需改三处代码（新建奖励文件、加 import、加路由分支）外加数据侧约定 `data_source` 字符串。

---

## 7. 下一步学习建议

本讲只到「点火」——`RayPPOTrainer` 被实例化、`init_workers()` 与 `fit()` 被调用，但没讲它们内部做了什么。建议按以下顺序继续：

1. **u4-l2 RayPPOTrainer 初始化与 Worker 编排**：精读 `ray_trainer.py` 的 `init_workers`，看资源池如何真正被消费、各 WorkerGroup 如何被 spawn、为什么 rollout 要最后创建。
2. **u4-l3 fit() 训练主循环全流程**：逐段拆解 `fit()`，把 generate → reward → advantage → update 这条主链路串起来，届时你会看到本讲的 `reward_fn` 正是在其中被调用。
3. **u4-l4 奖励计算与 RewardManager**：从 `fit()` 视角回看本讲的 `RewardManager`，看 model-based `rm_scores` 与 rule-based 分数是如何组合的。
4. 若你想立刻动手加任务，可直接跳到 **u7-l3 自定义新任务：端到端扩展**，那里把本讲的「三处改动」与数据生成、脚本启动完整串起来。
