# Megatron 后端与奖励模型 Worker

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `strategy` 这个配置开关在 `main_ppo` 入口处如何决定「导入哪一批 Worker 类」「用哪种 WorkerGroup」，从而把 FSDP 与 Megatron 两条后端切换开来。
- 理解 Megatron 后端的 `ActorRolloutRefWorker` 与 FSDP 后端的同名类在并行模型（TP/PP/DP）上的本质差异，以及它为什么仍然能扮演同一个「混合引擎」角色。
- 掌握 `RewardModelWorker`（model-based 奖励）如何把一个 `AutoModelForTokenClassification` 的标量输出展开成 token 级分数，并区分 FSDP 与 Megatron 两套实现。
- 看清 model-based 奖励与 rule-based 奖励在训练循环里如何并存、谁优先谁兜底，以及为什么 TinyZero 默认关掉奖励模型照样能训练。

本讲是「Worker 与混合引擎实现」单元的延伸，承接 u6-l1（ActorRolloutRefWorker 混合引擎）与 u4-l4（奖励计算与 RewardManager）的结论，把视野从「TinyZero 默认走的 FSDP + 规则奖励」拓宽到「verl 还内置了 Megatron 后端与神经网络奖励模型」。

## 2. 前置知识

- **后端（backend / strategy）**：指训练时模型参数被「切成什么样」放到多张 GPU 上。veRL 内置两条后端：FSDP（PyTorch 原生的 Fully Sharded Data Parallel，类似 ZeRO-3）与 Megatron（NVIDIA 的张量/流水线并行）。
- **数据并行（DP）、张量并行（TP）、流水线并行（PP）**：
  - DP：每张卡拿完整模型，处理不同样本。
  - TP：把每一层的权重矩阵「竖着切」分到多卡，同一 token 的计算跨卡协作。
  - PP：把模型的不同层「横着切」分到不同卡，数据像流水线一样依次经过各段。
  - 三者满足 `world_size = DP × TP × PP`。
- **Megatron 的 `mpu`（model parallel util）**：Megatron-LM 维护的全局并行状态机，`mpu.initialize_model_parallel(...)` 一次初始化后，全进程都能查到「我在哪个 TP 组、哪个 PP 组、DP 世界大小是多少」。
- **规则奖励 vs 模型奖励**：见 u2-l4、u4-l4。规则奖励是确定性 Python 函数 `compute_score`；模型奖励是一个训练好的神经网络（reward model, RM）给每条回答打分。
- **`@register` 与 Dispatch**：见 u3-l3。带 `DataProto` 入参、要数据并行处理的方法标 `DP_COMPUTE_PROTO`；无数据入参、全员同构的方法标 `ONE_TO_ALL`。Megatron 后端额外用了两个变体（见 4.2）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [verl/trainer/main_ppo.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py) | 训练总装入口，按 `strategy` 选 Worker 类与 WorkerGroup，按 `reward_model.enable` 决定是否挂载 RM。 |
| [verl/workers/megatron_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py) | Megatron 后端：`ActorRolloutRefWorker`、`CriticWorker`、`RewardModelWorker` 三类。 |
| [verl/workers/fsdp_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py) | FSDP 后端：与本讲对照的 `RewardModelWorker`（含 `compute_rm_score`）。 |
| [verl/workers/reward_model/base.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/reward_model/base.py) | 奖励模型抽象基类 `BasePPORewardModel`，规定 `compute_reward` 接口契约。 |
| [verl/single_controller/ray/megatron.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/megatron.py) | Megatron 专用的 Ray WorkerGroup（`NVMegatronRayWorkerGroup`）。 |
| [verl/single_controller/base/decorator.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py) | `Dispatch` 枚举与预定义分发/收集函数表。 |
| [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) | `fit()` 中 `compute_rm_score` 与 `reward_fn` 的组合点。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块推进：先讲后端切换（strategy），再讲 Megatron Worker 的并行差异，再讲 RewardModelWorker 的前向，最后讲 model-based 与 rule-based 如何并存。

### 4.1 strategy：在 FSDP 与 Megatron 两条后端间切换

#### 4.1.1 概念说明

`ActorRolloutRefWorker` 这个名字在 FSDP 和 Megatron 后端里**各有一个**，它们同名、同职责（都当混合引擎），但「模型怎么切」完全不同。`main_ppo` 用一个 `strategy` 字符串决定到底 import 哪一份。这样新增后端时，训练主循环 `fit()` 一行都不用改——它只调 `actor_rollout_wg.generate_sequences(...)` 这类方法，由 dispatch 层去翻译。

关键是：**切换后端换的是「类」与「WorkerGroup」，不是「流程」**。

#### 4.1.2 核心流程

`main_task` 在拼装阶段做一次二选一：

1. 读 `config.actor_rollout_ref.actor.strategy`。
2. fsdp → import `fsdp_workers` 的类 + 普通 `RayWorkerGroup`。
3. megatron → import `megatron_workers` 的类 + `NVMegatronRayWorkerGroup`。
4. 断言 `actor.strategy == critic.strategy`（actor 和 critic 必须同后端）。
5. 把选好的 `ray_worker_group_cls` 一路传给 `RayPPOTrainer`。

奖励模型是**独立**的第二条切换线：`config.reward_model.strategy` 单独决定 RM 用哪个后端的 `RewardModelWorker`，与 actor/critic 的 strategy 解耦。

#### 4.1.3 源码精读

后端二选一，定义 worker 类与 worker group 类：

[verl/trainer/main_ppo.py:124-138](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L124-L138) —— 这段做后端分发：fsdp 分支导入 `fsdp_workers` 的 `ActorRolloutRefWorker, CriticWorker` 并用 `RayWorkerGroup`；megatron 分支导入 `megatron_workers` 的同名类并用 `NVMegatronRayWorkerGroup`。两处都 `assert actor.strategy == critic.strategy`，强制两个被训练/被估计的模型用同一种切分方式。

注意 `Role.RefPolicy` 也复用 `ActorRolloutRefWorker`（只是构造时 role 不同）：

[verl/trainer/main_ppo.py:142-146](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L142-L146) —— 三个角色到 Worker 类的映射表。ActorRollout 与 RefPolicy 指向**同一个类**，靠 u6-l1 讲过的 `role` 字符串区分 `_is_actor`/`_is_ref` 等标志。

奖励模型这条独立切换线：

[verl/trainer/main_ppo.py:164-172](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L164-L172) —— 只有 `reward_model.enable=True` 时才往 `role_worker_mapping` 里挂 `Role.RewardModel`，并按 `reward_model.strategy` 选 FSDP 或 Megatron 的 `RewardModelWorker`。注意它也放进同一个 `global_pool`，即 RM 与 actor/critic 物理共置。

而**规则奖励函数恒定创建**，与 enable 无关：

[verl/trainer/main_ppo.py:174-177](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L174-L177) —— 无论是否启用奖励模型，`reward_fn` 与 `val_reward_fn`（都是 `RewardManager`）都会被实例化并传给 trainer。这是「关闭 RM 仍能训练」的直接证据。

默认配置里 RM 是关的：

[verl/trainer/config/ppo_trainer.yaml:121-126](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L121-L126) —— `enable: False`、`strategy: fsdp`、`model.path` 默认指向一个 LLaMA3 RM；`model.input_tokenizer` 用变量插值取 actor 的模型路径（chat template 一致时设为 null，详见 4.3）。

#### 4.1.4 代码实践

**实践目标**：亲手确认「换 strategy 换的是 import 和 worker_group_cls，不动 fit()」。

**操作步骤**：

1. 打开 [main_ppo.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py)，定位 124–138 行的两个分支。
2. 用 `grep` 统计 `fit()` 与 `init_workers()` 内部是否出现 `strategy` 或 `fsdp_workers`/`megatron_workers` 字样（应当几乎没有，因为它们只依赖注入进来的 `ray_worker_group_cls`）。
3. 在 124 行临时把 `'fsdp'` 改成 `'megatron'`（仅阅读，不运行），顺着分支读下去，列出三处会随之改变的对象：Worker 类来源、WorkerGroup 类、（RM 线无关）。

**需要观察的现象 / 预期结果**：`fit()` 体对后端无感知；改变 strategy 只影响 `main_task` 这一处的装配。**待本地验证**：若你装好了 Megatron-LM 依赖，可尝试用 `+actor_rollout_ref.actor.megatron.tensor_model_parallel_size=2` 之类覆盖跑通（本仓库未提供现成 Megatron 训练脚本，默认走 FSDP）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main_ppo` 要断言 `actor.strategy == critic.strategy`？
**参考答案**：actor 与 critic 都是要反向更新、且在 GAE 路线里逐 token 对齐（见 u6-l3）的模型，若两者用不同切分（比如 actor 走 TP=2、critic 走 TP=1），它们的 `world_size` 与 DP 划分不一致，dispatch 等分竖切与 `chunk(world_size)` 会对不上，数据无法正确分发；强制同后端才能保证它们在同一套进程组拓扑里协作。

**练习 2**：`reward_model.strategy` 与 `actor.strategy` 互相独立，有什么好处？
**参考答案**：RM 只做只读前向打分、不反传，其切分策略可与训练模型解耦；你可以让 actor 走 FSDP、RM 也走 FSDP，也可以按需各自选最合适的后端，而不必把它们绑死。

### 4.2 Megatron Worker：3D 并行与角色复用

#### 4.2.1 概念说明

FSDP 后端只有一个并行维度（数据并行 + 可选 Ulysses 序列并行，见 u7-l1）。Megatron 后端引入了 **3D 并行**：`world_size = DP × TP × PP`。它的 `ActorRolloutRefWorker`（注意与 u6-l1 的 FSDP 版同名！）继承自 `MegatronWorker`，在构造时调用 Megatron 的 `mpu.initialize_model_parallel` 把全局拓扑建好，之后整个 Worker 内的方法都默认「我已经知道自己在哪个 TP/PP 组」。

它仍然复用 u6-l1 的混合引擎思想：一个类 + `role` 字符串派生 `_is_actor`/`_is_rollout`/`_is_ref` 三标志，五种角色组合共用代码。差异只在「模型如何被切」和「数据如何沿 PP 维度流动」。

#### 4.2.2 核心流程

构造期一次性初始化并行状态：

1. `torch.distributed.init_process_group(backend="nccl")`（若未初始化）。
2. `mpu.initialize_model_parallel(tensor_model_parallel_size=..., pipeline_model_parallel_size=...)` 建立 TP/PP 组。
3. 若开 `sequence_parallel`，设 `CUDA_DEVICE_MAX_CONNECTIONS=1`（序列并行对通信重叠敏感，需禁用默认的流水线重叠）。
4. 派生 `_is_actor`/`_is_rollout`/`_is_ref` 标志。
5. 把 `ppo_mini_batch_size`、`ppo_micro_batch_size` 等**按 DP 世界大小整除**——因为每个 DP rank 只处理整批的 `1/DP`。

PP 维度的特殊性：模型被切成多段，一段在一个 PP rank 上。当 actor 还要兼做 rollout（`_is_actor and _is_rollout`）时，rollout 需要完整模型，于是用一个 `AllGatherPPModel`（hybrid engine）把各 PP 段的参数 allgather 收拢到本 rank。

#### 4.2.3 源码精读

并行初始化与角色标志：

[verl/workers/megatron_workers.py:79-104](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L79-L104) —— `__init__` 里 `init_process_group` 后立即 `mpu.initialize_model_parallel(...)`，参数取自 `self.config.actor.megatron.tensor_model_parallel_size` / `pipeline_model_parallel_size`；随后派生 `_is_actor/_is_rollout/_is_ref`。这与 FSDP 版混合引擎的「role 标志」机制完全一致，但多了 `mpu` 这一层并行拓扑。

序列并行的环境约束：

[verl/workers/megatron_workers.py:84-85](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L84-L85) —— 开 `sequence_parallel` 时设 `CUDA_DEVICE_MAX_CONNECTIONS=1`。

3D HybridEngine 的构建（actor+rollout 分支）：

[verl/workers/megatron_workers.py:169-185](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L169-L185) —— `if self._is_actor and self._is_rollout` 时用 `AllGatherPPModel(...)` 包装模型提供器，`this_rank_models` 取本 PP rank 的模型段。FSDP 版没有这一层，因为 FSDP 每卡都有完整模型（仅参数分片），不需要 allgather 整模型。

Megatron 专用 Dispatch 模式（与 u3-l3 对照）：

- `init_model`：[megatron_workers.py:260-260](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L260) 标 `Dispatch.ONE_TO_ALL`（无数据入参，全员同构初始化）。
- `update_actor`：[megatron_workers.py:325-325](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L325) 标 `Dispatch.MEGATRON_COMPUTE_PROTO`（带 DataProto，按 DP 切）。
- `compute_ref_log_prob`：[megatron_workers.py:375-375](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L375) 同样 `MEGATRON_COMPUTE_PROTO`。
- `generate_sequences`：[megatron_workers.py:344-344](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L344) 标 `Dispatch.MEGATRON_PP_AS_DP_PROTO`——这是 Megatron 特有的：把 PP 组当成一个 DP 单元来分发（同一条样本要流经所有 PP 段，所以不能在 PP 维度上切样本）。

Dispatch 枚举里这几个 Megatron 变体的定义：

[verl/single_controller/base/decorator.py:25-37](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L25-L37) —— `ONE_TO_ALL=1`、`MEGATRON_COMPUTE_PROTO=6`、`MEGATRON_PP_AS_DP_PROTO=7`、`DP_COMPUTE_PROTO=9` 并列存在，`get_predefined_dispatch_fn`（300 行起）会查表翻译成对应的 `dispatch_fn`/`collect_fn`。

`generate_sequences` 末尾的重算（与 u6-l1 一致的 hybrid 不变式）：

[verl/workers/megatron_workers.py:361-367](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L361-L367) —— `_is_actor and not validate` 时用 actor 自身前向重算 `old_log_probs`，保证 PPO 的 importance ratio 起点为 1（vLLM 与 Megatron actor 前向数值不一致，见 u6-l4）。

WorkerGroup 侧的差异：

[verl/single_controller/ray/megatron.py:25-25](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/ray/megatron.py#L25) —— `NVMegatronRayWorkerGroup(RayWorkerGroup, MegatronWorkerGroup)`，多继承 `MegatronWorkerGroup`，在 Ray actor 里额外处理 Megatron 的 TP/PP 进程组拓扑（普通 `RayWorkerGroup` 不懂 PP）。

#### 4.2.4 代码实践

**实践目标**：对比 FSDP 与 Megatron 两个 `ActorRolloutRefWorker` 在「初始化并行拓扑」上的差异。

**操作步骤**：

1. 并排打开 [megatron_workers.py:63-104](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L63-L104) 与 [fsdp_workers.py:53-](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L53) 的 `__init__`。
2. 在一张纸上分两列写：FSDP 版初始化了什么（device mesh、Ulysses 可选），Megatron 版初始化了什么（`mpu.initialize_model_parallel` + TP/PP）。
3. 找出三个方法在两个后端里用的 Dispatch 差异：Megatron 版用 `MEGATRON_COMPUTE_PROTO` / `MEGATRON_PP_AS_DP_PROTO`，FSDP 版（u6-l1）用 `DP_COMPUTE_PROTO`。

**需要观察的现象 / 预期结果**：两者角色标志机制相同，但「并行拓扑」一列几乎全不同——这就是 strategy 切换的本质。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `generate_sequences` 用 `MEGATRON_PP_AS_DP_PROTO` 而不是普通的 `MEGATRON_COMPUTE_PROTO`？
**参考答案**：生成（rollout）需要完整模型，rollout 引擎被放在一个 PP 组上；一条 prompt 必须流经该 PP 组的所有 rank，所以在 PP 维度上不能切样本，只能把「一个 PP 组」当成一个 DP 单元来分发数据。`update_actor` 不涉及跨 PP 段的整模型推理，所以可以用普通的 `MEGATRON_COMPUTE_PROTO` 按 DP 切。

**练习 2**：为什么构造里要 `ppo_mini_batch_size //= mpu.get_data_parallel_world_size()`？
**参考答案**：配置里的 batch size 是全局的；每个 DP rank 只处理 `1/DP`，故每 rank 看到的 mini/micro batch size 也要除以 DP 世界大小，保证整体 batch 不变。

### 4.3 RewardModelWorker：model-based 奖励的前向

#### 4.3.1 概念说明

`RewardModelWorker` 是 model-based 奖励的载体：它加载一个**预训练好的奖励模型**（不是规则函数），对每条 `prompt + response` 前向一次，输出一个标量分数。verL 的实现约定 RM 必须是 `AutoModelForTokenClassification` 的子类——即把 LM 的输出头从 `hidden→vocab` 换成 `hidden→1`，让每个 token 吐一个标量「奖励值」，再取回答末位有效 token 的值作为该样本的分数。

与 u6-l3 的 critic 形似：critic 也是 `hidden→1` 的 value head。区别在于：critic 是**在线训练**的（用 GAE returns 监督），RM 是**冻结的预训练模型**（只前向打分，不更新）。

它同样有 FSDP 与 Megatron 两套实现：FSDP 版继承 `Worker`、用 `FSDP(...)` 包装；Megatron 版继承 `MegatronWorker`、用 `get_parallel_model_from_config(value=True)` 构造。本节以 FSDP 版为主精读（`compute_rm_score` 是本讲指定的最小模块），Megatron 版见 4.3.3 末尾对照。

#### 4.3.2 核心流程

`compute_rm_score(data)` 的流程：

1. （可选）若 RM 与 actor 用不同 tokenizer/chat template，先 `_switch_chat_template` 把 prompt+response 用 RM 自己的 tokenizer 重新编码。
2. 进入 Ulysses sharding manager，处理序列并行。
3. 按固定 `micro_batch_size` 或动态 `max_token_len` 切 micro batch，逐个 `_forward_micro_batch`。
4. 每个 micro batch 得到一个 `(batch_size,)` 的标量分数向量，`torch.cat` 起来。
5. `_expand_to_token_level` 把标量分数放到对应样本末位有效 token 上，并裁出 response 段，得到与 rule-based 同形状的 `token_level_scores`（名为 `rm_scores`）。

取末位有效 token 的公式（与 rule-based 奖励的末位放置一致，见 u4-l4）：

\[
\text{eos\_idx}_i = \arg\max_j \big(\text{position\_ids}_{i,j} \cdot \text{attention\_mask}_{i,j}\big)
\]

即「attention_mask 为 1 的位置里 position_ids 最大的那个」，就是回答最后一个有效 token。

#### 4.3.3 源码精读

FSDP 版 RM 的构建（注意 `num_labels=1`）：

[verl/workers/fsdp_workers.py:793-848](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L793-L848) —— `model_config.num_labels = 1` 把分类头压成单标量；用 `AutoModelForTokenClassification.from_pretrained(...)` 加载 RM；再用 `FSDP(..., sharding_strategy=ShardingStrategy.FULL_SHARD, sync_module_states=True, ...)` 包装（zero3）。注释明确「必须在 fp32 创建，否则 optimizer 会是 bf16」。

chat template 切换的开关：

[verl/workers/fsdp_workers.py:801-808](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L801-L808) —— `input_tokenizer` 为 None 时 `_do_switch_chat_template=False`（RM 与 actor 用同一套 tokenizer），否则为 True，说明 RM 有自己的 chat 模板，需要重新编码输入。这就是 ppo_trainer.yaml 里 `input_tokenizer` 注释「set this to null if the chat template is identical」的含义。

单 micro batch 前向与末位 token 提取：

[verl/workers/fsdp_workers.py:857-909](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L857-L909) —— `_forward_micro_batch` 在 `torch.no_grad()` 下前向得到逐 token 的 logits（`(bsz, seqlen)`，squeeze 掉最后一维的 1），再用 `eos_mask_idx = torch.argmax(position_ids * attention_mask, dim=-1)` 取末位有效 token（907 行），`rm_score[arange(bsz), eos_mask_idx]` 得到每样本一个标量（908 行）。支持 `use_remove_padding`（变长去填充）与 Ulysses 序列并行。

标量展开到 token 级：

[verl/workers/fsdp_workers.py:911-924](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L911-L924) —— `_expand_to_token_level`：建一个与 attention_mask 同形的零张量，把每样本标量 `scores[i]` 放到 `eos_mask_idx` 位，再 `[:, -response_length:]` 裁出 response 段。这与 rule-based 奖励把分数挂到末位 token 的方式完全同构（见 u4-l4），使得两条来路的输出可互换。

`compute_rm_score` 主流程：

[verl/workers/fsdp_workers.py:983-1023](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L983-L1023) —— `@register(Dispatch.DP_COMPUTE_PROTO)`（注意 FSDP 版用普通 `DP_COMPUTE_PROTO`，不是 Megatron 变体）；可选 `_switch_chat_template` 后，按 `use_dynamic_bsz` 切 micro batch，逐个前向，cat 出 `(batch_size,)` 的 scores；动态 bsz 时用 `get_reverse_idx` 还原顺序；最后 `_expand_to_token_level` 得 `rm_scores`。注释（1017 行）特意提醒：**这只是分数，未必是最终 RL 奖励**——还要经 `reward_fn` 组合（见 4.4）。

抽象基类规定的接口契约：

[verl/workers/reward_model/base.py:23-45](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/reward_model/base.py#L23-L45) —— `BasePPORewardModel.compute_reward` 规定输入含 `input_ids/attention_mask/position_ids`，输出 `reward` 形状 `[batch, seqlen]`，只在 EOS 位放分数、其余为 0。`fsdp_workers.RewardModelWorker` 的 `_expand_to_token_level` 正好兑现这个契约。

Megatron 版对照（更薄）：

[verl/workers/megatron_workers.py:722-727](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L722-L727) —— Megatron 版 `compute_rm_score` 只把数据上 cuda，然后 `self.rm.compute_reward(data)`（`self.rm` 是 `MegatronRewardModel`），token 级展开的细节藏在 `MegatronRewardModel` 内（本仓库未展开实现）。它的 Dispatch 标的是 `MEGATRON_COMPUTE_PROTO`（[722 行](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L722)），模型构建见 [megatron_workers.py:638-655](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/megatron_workers.py#L638-L655)（`value=True` 即 value/reward head）。

#### 4.3.4 代码实践

**实践目标**：理解「RM 输出逐 token 标量 → 取末位 → 展开回 token 级」这条链路。

**操作步骤**：

1. 读 [fsdp_workers.py:906-908](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L906-L908)，确认 `eos_mask_idx` 的计算用的是 `position_ids * attention_mask` 而非单独用 attention_mask。
2. 读 [fsdp_workers.py:911-924](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L911-L924)，画出：一个 `(1, seqlen)` 的零张量，在 eos 位填入 score，再 `[:, -response_length:]` 裁剪。
3. 构造一个示例（**示例代码**，非项目原有）：假设 `position_ids = [[0,1,2,3,4]]`、`attention_mask = [[1,1,1,1,0]]`、`response_length=2`、score=0.7，手算 `eos_mask_idx=3`，展开后 token_level_scores 形如 `[0,0,0,0.7,0]`，裁出 response 段得 `[0.7,0]`（注意左填充 prompt + 右填充 response 的全局约定，见 u6-l4）。

**需要观察的现象 / 预期结果**：分数只出现在回答最后一个有效 token 上，其余位为 0——稀疏奖励的末位放置。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 RM 与 critic 都把输出头设成 `num_labels=1`/`value=True`，但一个是冻结的、一个是在线训练的？
**参考答案**：RM 是「外挂的、已训练好的偏好模型」，只前向提供监督信号（打分），不参与 RL 梯度更新；critic 是 RL 流程内部为了估计 advantage（GAE）而**在线学习**的价值函数，用 returns 监督更新。两者结构相似（hidden→1）、用途相反。

**练习 2**：`_do_switch_chat_template` 何时为 True？切模板时为什么用「右填充」？
**参考答案**：当 RM 与 actor 用不同 tokenizer（`input_tokenizer` 不为 None）时为 True。右填充（`left_pad=False`）是因为这是纯前向打分、不涉及从序列右端续写生成，左/右填充不影响分类结果，按 RM 训练时的惯例右填充即可；而 actor rollout 必须**左填充**（见 u2-l3）是为了能从右端追加生成 token。

### 4.4 model-based 与 rule-based 奖励如何并存

#### 4.4.1 概念说明

这是本讲最关键的结论之一：model-based 奖励（`compute_rm_score`）和 rule-based 奖励（`compute_score`）**不是二选一，而是串接**。流程是「先算 RM 分数（可选），再用 `reward_fn` 组合」。`reward_fn` 即 u4-l4 的 `RewardManager`，它检测到 batch 里已有 `rm_scores` 就短路返回 RM 分数，否则逐条走规则函数。

因此：

- 开启 RM（`reward_model.enable=True`）：`rm_wg.compute_rm_score(...)` 先把 `rm_scores` 注入 batch，`RewardManager` 检测到后短路，token_level_scores = rm_scores。
- 关闭 RM（TinyZero 默认）：`rm_wg` 根本不创建，batch 里没有 `rm_scores`，`RewardManager` 逐条解码、按 `data_source` 调规则 `compute_score` 打分。

**「关闭奖励模型仍能训练」的根因就是后者**：TinyZero 选 countdown/multiply 这类有精确规则奖励的任务，`reward_fn` 恒定存在且足够提供训练信号，RM 可有可无。

#### 4.4.2 核心流程

`fit()` 的奖励阶段（`adv` 计时块内）：

1. 若 `self.use_rm`：`reward_tensor = self.rm_wg.compute_rm_score(batch)`，`batch.union(reward_tensor)`（注入 `rm_scores`）。
2. `reward_tensor = self.reward_fn(batch)`——`RewardManager.__call__` 内部决定走 RM 分数还是规则分数。
3. `batch.batch['token_level_scores'] = reward_tensor`。
4. 之后 `apply_kl_penalty`（或 GRPO 的 `use_kl_loss` 跳过）得到 `token_level_rewards`，进入优势计算（u5-l1）。

`use_rm` 与 `rm_wg` 的存在性由 `reward_model.enable` 决定。

#### 4.4.3 源码精读

`fit()` 中 RM 与 rule 的组合点：

[verl/trainer/ppo/ray_trainer.py:621-628](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L621-L628) —— `if self.use_rm:` 先 `self.rm_wg.compute_rm_score(batch)` 并 `batch.union(...)`；随后**无条件** `reward_tensor = self.reward_fn(batch)` 写入 `token_level_scores`。注释（619–620 行）描述的「combine」与 u4-l4 指出的 short-circuit 实现一致：以代码为准，`RewardManager` 见 `rm_scores` 即返回它，并不真正与规则分数相加。

RM WorkerGroup 的创建门控：

[verl/trainer/ppo/ray_trainer.py:509-510](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L509-L510) —— `self.rm_wg = all_wg['rm']` 与 `self.rm_wg.init_model()` 只在 `reward_model.enable=True` 时才发生（`all_wg` 里才有 `'rm'` 这个键）；否则 `use_rm=False`，621 行的 if 被跳过，`reward_fn` 独立完成打分。

验证侧恒走规则奖励：

[verl/trainer/ppo/ray_trainer.py:400-400](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L400-L400) —— 验证时也要 `reward_model.enable and style=='model'` 才走模型分数；TinyZero 关掉 RM，验证自然只用 `val_reward_fn`（规则奖励，见 u4-l3）。

#### 4.4.4 代码实践

**实践目标**：亲手回答讲义指定的实践问题——「reward_model.enable=False 时为何仍能训练」。

**操作步骤**：

1. 在 [ppo_trainer.yaml:122](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L122) 确认默认 `enable: False`。
2. 在 [main_ppo.py:174-177](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L174-L177) 确认 `reward_fn`/`val_reward_fn` 无条件创建。
3. 在 [ray_trainer.py:621-627](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L621-L627) 追踪：`use_rm=False` 时跳过 `compute_rm_score`，直接 `reward_fn(batch)`，而 `batch` 里没有 `rm_scores`，于是 `RewardManager`（u4-l4）逐条调规则 `compute_score`。
4. 写一句话结论：因为 countdown/multiply 的规则奖励是自足的，RM 不是训练的必要条件。

**需要观察的现象 / 预期结果**：enable=False 时 `role_worker_mapping` 里没有 `Role.RewardModel`、`rm_wg` 不创建、fit 里 `use_rm` 分支被跳过，但 `reward_fn` 仍能产出 `token_level_scores`，训练正常推进。**待本地验证**：可在训练日志里确认没有「building reward model」相关输出。

#### 4.4.5 小练习与答案

**练习 1**：若同时开启 RM（model-based）和规则奖励（rule-based），最终 `token_level_scores` 是两者之和吗？
**参考答案**：不是。源码注释说「combine」，但 `RewardManager.__call__` 检测到 `rm_scores` 就**短路返回** RM 分数（见 u4-l4），并不与规则分数相加。若确实想要「两者融合」，需要自行修改 `RewardManager` 的合并逻辑，当前实现以 RM 分数为准。

**练习 2**：为什么 `compute_rm_score` 与规则奖励的输出都做成「只在末位 token 有值」的稀疏形式？
**参考答案**：下游 `apply_kl_penalty` 和 `compute_advantage`（u5-l1）都在 token 级张量上操作，两条来路统一成同形状的 `token_level_scores`（`[bsz, response_length]`）才能无缝替换；末位放置对应「回答作为一个整体得到一个分数」的语义，即 outcome reward。

## 5. 综合实践

**任务**：为 TinyZero 设计一个「同时挂上神经网络奖励模型」的配置方案，并解释每个开关的作用。

请完成：

1. **后端选择**：在 [main_ppo.py:124-138](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L124-L138) 处说明，若你希望 actor 走 Megatron（TP=2, PP=2）后端，需要把 `actor.strategy` 改成什么、会连带把 `ray_worker_group_cls` 换成哪个类、为何 `critic.strategy` 必须同步改。
2. **开启 RM**：参考 [ppo_trainer.yaml:121-126](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L121-L126)，写出最小改动清单：`reward_model.enable=True`、`reward_model.strategy`（fsdp 或 megatron）、`model.path` 指向一个真实的 RM 权重、`input_tokenizer` 在 chat template 一致时设 null 否则指向 actor 的 tokenizer。
3. **数据流追踪**：画出从 `generate_sequences` 产出 batch → `compute_rm_score` 注入 `rm_scores` → `reward_fn` 短路返回 → `token_level_scores` → `apply_kl_penalty` → `token_level_rewards` → `compute_advantage` 的完整链路，标注每一步落在 [ray_trainer.py:608-637](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L608-L637) 的哪一段。
4. **回退验证**：把 `reward_model.enable` 改回 False，说明训练为何不受影响（`reward_fn` 自足、countdown 规则奖励足够）。

预期产出：一份配置清单 + 一张数据流图 + 一段「关闭 RM 仍可训练」的论证。**待本地验证**（本仓库未提供现成 RM 训练脚本与 Megatron 脚本，默认走 FSDP + 规则奖励）。

## 6. 本讲小结

- `strategy`（fsdp / megatron）是后端切换开关：在 `main_ppo` 里决定 import 哪份 `ActorRolloutRefWorker`/`CriticWorker`、用哪种 `RayWorkerGroup`；`fit()` 本身对后端无感知。
- Megatron 后端的 `ActorRolloutRefWorker` 同样用 role 标志做混合引擎，但多了 `mpu` 建立的 3D 并行（TP/PP/DP），并用 `AllGatherPPModel` 在 actor+rollout 时收拢 PP 段；Dispatch 用 Megatron 专用变体（`MEGATRON_COMPUTE_PROTO`、`MEGATRON_PP_AS_DP_PROTO`）。
- `RewardModelWorker` 把 `AutoModelForTokenClassification`（`num_labels=1`）前向得到的逐 token 标量，取末位有效 token、再展开回 token 级，产出与规则奖励同形状的 `rm_scores`；有 FSDP 与 Megatron 两套实现。
- model-based 与 rule-based 奖励串接而非互斥：`use_rm` 时先注入 `rm_scores`，再由 `reward_fn`（`RewardManager`）统一收口（见 `rm_scores` 即短路）。
- TinyZero 默认 `reward_model.enable=False`，但 `reward_fn` 恒定创建且 countdown/multiply 规则奖励自足，所以关闭 RM 仍能正常训练。

## 7. 下一步学习建议

- 若想真正跑通 Megatron 后端，建议阅读 [verl/utils/megatron_utils.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/megatron_utils.py)（`init_model_parallel_config`、`get_model`）与 `AllGatherPPModel` 所在的 sharding manager，理解 PP 段参数如何 allgather 与 reshard。
- 若要接入 model-based 奖励，可对照 [verl/workers/reward_model/megatron/reward_model.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/reward_model/megatron/reward_model.py) 阅读 `MegatronRewardModel.compute_reward` 的 token 级展开实现。
- 下一讲 u7-l5 将转向测试、调试与实验跟踪，教你用 `tests/e2e` 做最小化训练验证、用 `compute_data_metrics` 读指标。
