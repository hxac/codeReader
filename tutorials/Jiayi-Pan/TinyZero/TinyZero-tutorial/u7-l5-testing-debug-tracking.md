# 测试、调试与实验跟踪

## 1. 本讲目标

读到这里，你已经走完了 PPO/GRPO 的数学推导、Worker 的混合引擎实现。本讲换一个视角，回答一个工程问题：**「我写完/改完代码后，怎么知道它真的能跑、跑得对、跑得好？」**

具体来说，学完本讲你应该能够：

- 用仓库自带的 `tests/e2e/arithmetic_sequence` 在单卡上跑一次**最小化训练**，作为「环境是否健康、代码是否跑得通」的烟雾测试（smoke test）。
- 读懂 veRL 的**指标体系**：`compute_data_metrics` / `compute_timing_metrics` 产出了哪些 key，actor/critic 各自汇报了哪些训练量，它们如何被 `reduce_metrics` 汇聚。
- 读懂 `Tracking` 这个**统一日志后端**如何把同一份指标同时落到 console / wandb / mlflow，以及它在 `fit()` 里的三个打点时机。
- 掌握现成的调试手段：`log_gpu_memory_usage` 显存打点、`_timer` 阶段计时、reward 函数里基于 `num_examine` 的样本打印。
- 把 `response_length/mean` 随训练**变长**这一现象，与 u1-l1 讲过的 R1 Zero「Aha moment」联系起来。

> 关于讲义主题里提到的 `trajectory_tracker`：经全仓库检索，**TinyZero 代码中没有这个对象**。本讲只讲解真实存在的调试工具，不编造接口。

## 2. 前置知识

- **端到端测试（e2e test）**：用一个极小模型、极小数据、极少步数跑通完整训练链路，目的不是「训出好模型」，而是「验证流水线没坏」。它相当于 RL 训练的「Hello World」。
- **指标（metric）**：训练过程中每个 step 产出的若干标量（如 `actor/pg_loss`、`critic/score/mean`），用来观察训练是否健康。本讲会频繁用到 u4-l3、u5-l1~u5-l5 里的概念：`token_level_scores`（任务分）、`token_level_rewards`（含 KL 罚的奖励）、`advantages`、`returns`、`values`。
- **稀疏奖励 vs 稠密奖励**：u2-l4 的 countdown 是稀疏奖励（只有回答末尾一个 token 有分）；本讲的 e2e 测试用的是**稠密奖励**（每个正确 token 都给一点分），这是为了让极小模型「无需 SFT 也能学起来」。
- **wandb**：Weights & Biases，一个流行的实验跟踪 SaaS，把指标画成曲线图。veRL 把它作为可选后端之一。
- **codetiming**：一个轻量计时库，veRL 用它的 `Timer` 上下文管理器给每个阶段掐秒表。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tests/e2e/run_ray_trainer.sh](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/run_ray_trainer.sh) | e2e 测试的入口脚本：跑训练、`tee` 到日志、再跑 `check_results.py` 断言奖励是否达标 |
| [tests/e2e/arithmetic_sequence/rl/main_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/arithmetic_sequence/rl/main_trainer.py) | e2e 训练的总装：Hydra 配置、`ray.init`、手写稠密 reward_fn、装配 `RayPPOTrainer` |
| [tests/e2e/arithmetic_sequence/rl/config/ray_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/arithmetic_sequence/rl/config/ray_trainer.yaml) | e2e 专用的极小配置（200 epoch、console 日志、HF rollout、`n=1`） |
| [tests/e2e/envs/digit_completion/task.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/envs/digit_completion/task.py) | 任务定义：等差数列续写 + `compute_reward` 稠密奖励 |
| [tests/e2e/check_results.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/check_results.py) | 解析训练日志，断言 `critic/rewards/mean` 是否超过 0.2 |
| [verl/utils/tracking.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/tracking.py) | `Tracking` 类：console/wandb/mlflow 三后端统一 `log` 接口 |
| [verl/utils/logger/aggregate_logger.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/logger/aggregate_logger.py) | `LocalLogger`：把指标 dict 拼成一行 `step:N - k:v` 打到控制台 |
| [verl/utils/debug/performance.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/debug/performance.py) | `log_gpu_memory_usage`：打印某 rank 的已分配/已预留显存（GB） |
| [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) | `compute_data_metrics` / `compute_timing_metrics` / `fit()` 的日志打点 |

---

## 4. 核心概念与源码讲解

### 4.1 端到端测试：用 arithmetic_sequence 跑最小训练

#### 4.1.1 概念说明

当你升级了 torch、换了 vllm 版本、或者改了 `core_algos.py` 里某个公式，怎么最快确认「整套 RL 流水线没被改坏」？答案不是去跑 TinyZero 的 3B countdown（那是真训练，要几小时），而是跑一个**玩具任务**：让一个极小的随机初始化模型，在「等差数列续写」任务上学几百步，看奖励能不能涨起来。

这就是 `tests/e2e/arithmetic_sequence` 的作用——它是 veRL 自带的**烟雾测试**：模型小、数据小、步数少、不依赖 vLLM（用 HF rollout），几分钟内能在单卡上跑完。如果它跑通了且奖励上涨，说明从数据加载、rollout、advantage、actor/critic 更新到日志输出的**整条链路都是通的**。

> 任务本身（digit completion）非常简单：prompt 形如 `7,8:20,2`，表示「以公差续写，最大数取模 20，续写 2 个数」，正确答案是 `11,12`。详见 [tests/e2e/envs/digit_completion/task.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/envs/digit_completion/task.py)。

#### 4.1.2 核心流程

e2e 测试由三段串联：

```text
run_ray_trainer.sh
   │
   ├─ python3 .../main_trainer.py ... | tee /tmp/output_ray_trainer.txt
   │        └─ RayPPOTrainer.fit()  → 每个 step 打印一行 "step:N - critic/rewards/mean:... - ..."
   │
   └─ python3 .../check_results.py --output_file=/tmp/output_ray_trainer.txt
            └─ 提取所有 step 行里的 critic/rewards/mean，断言 best > 0.2
```

其中 `main_trainer.py` 的装配与 u4-l1 的 `main_ppo.py` 几乎同构，但有三点刻意简化：

1. **不接规则奖励路由**：reward 函数直接手写在 `make_reward_function` 里，而不是走 `_select_rm_score_fn`。
2. **不创建参考策略（RefPolicy）**：`role_worker_mapping` 只有 `ActorRollout` 和 `Critic` 两个角色（对比 u4-l1 的三个）。
3. **稠密奖励**：每个正确 token 都给分，而非只在末尾给。

#### 4.1.3 源码精读

先看入口脚本 [tests/e2e/run_ray_trainer.sh:L5-L17](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/run_ray_trainer.sh#L5-L17)：它把训练的标准输出用 `tee` 同时打到屏幕和 `/tmp/output_ray_trainer.txt`，然后把这个文件喂给 `check_results.py`。注意它用 Hydra 覆盖把数据/模型路径指到仓库内的本地文件（`data.train_files=...`、`actor_rollout_ref.model.path=...`），所以无需联网下载。

再看 `main_trainer.py` 的装配 [tests/e2e/arithmetic_sequence/rl/main_trainer.py:L91-L157](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/arithmetic_sequence/rl/main_trainer.py#L91-L157)：`@hydra.main` 装在本地 config 上，`ray.init` 注入一组环境变量（关闭 Megatron 计时器等），然后按 u4-l1 学过的三张表套路组装 `role_worker_mapping` / `resource_pool_spec` / `mapping`，两个 Role 都 colocate 到 `global_pool`：

```python
role_worker_mapping = {
    Role.ActorRollout: ray.remote(ActorRolloutRefWorker),
    Role.Critic:        ray.remote(CriticWorker),
}
mapping = {Role.ActorRollout: global_pool_id, Role.Critic: global_pool_id}
```

注意这里**没有 `Role.RefPolicy`**，所以 u4-l2 讲过的 `use_reference_policy` 为 `False`，KL 控制器会被设成系数为 0 的 `FixedKLController`（详见 4.4）。

最值得读的是 reward 函数 [tests/e2e/arithmetic_sequence/rl/main_trainer.py:L35-L88](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/arithmetic_sequence/rl/main_trainer.py#L35-L88)。它和 u4-l4 的 `RewardManager` 思路一致——都是解码出 prompt/response、调用打分函数、把分数放进 `reward_tensor`——但**奖励是稠密的**：

```python
reward_tensor = torch.zeros_like(data.batch['responses'], dtype=torch.float32)
...
reward_output = compute_reward(prompt, response)   # 返回逐 token 奖励数组
dense_reward = reward_output[0].tolist()
...
# pad to response_length：用最后一份非零奖励填满剩余位置
for _ in range(reward_tensor.shape[-1] - len(dense_reward)):
    dense_reward.append(last_reward)
reward_tensor[i] = dense_reward * response_mask
```

对照 u2-l4：countdown 的 `RewardManager` 把标量分数**只放在最后一个有效 token**（稀疏），而这里每个正确 token 都有 `per_token_reward`（见 `compute_reward` 的注释「We compute dense reward here so that we can directly train RL without SFT」[tests/e2e/envs/digit_completion/task.py:L137-L160](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/envs/digit_completion/task.py#L137-L160)）。这是玩具任务能学起来的关键：稠密信号让随机初始化的小模型也有梯度可走。函数里还保留了一句样本打印 `if i < num_examine: print(prompt, response)`，这是最朴素的「看模型在说什么」的调试手段（4.2 再展开）。

最后看断言 [tests/e2e/check_results.py:L20-L52](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/check_results.py#L20-L52)：它逐行扫描日志，只挑以 `step` 开头的行，按 ` - ` 切分后在键值对里找 `critic/rewards/mean`（注意它校验的是 **rewards** 而非 score，即含 KL 罚的奖励），记录最大值，最后 `assert best_reward > 0.2`。这条 0.2 的阈值就是「流水线健康」的判据——学不会说明哪里坏了。

#### 4.1.4 代码实践

1. **实践目标**：在本地单卡上跑通 e2e 烟雾测试，并从日志里提取关键指标。
2. **操作步骤**：
   ```bash
   cd /path/to/TinyZero
   bash tests/e2e/run_ray_trainer.sh 2>&1 | tee /tmp/e2e_full.log
   ```
   若无 GPU，则跳过实际运行，做下面的「源码阅读型」分析。
3. **需要观察的现象**：
   - 屏幕先打印两遍 `pprint(config)`（归一化 batch 前后各一次）。
   - 每个 step 打印一行 `step:N - critic/rewards/mean:... - actor/pg_loss:... - response_length/mean:... - ...`。
   - 最后 `check_results.py` 打印 `Best reward is ...` 和 `Check passes`。
4. **预期结果**：`best_reward > 0.2`，断言通过。若报错或奖励停在 0，说明环境/代码有问题。
5. **待本地验证**：实际奖励数值与运行时长依赖本地 GPU，无法在此给出确定值。

> **源码阅读型替代实践**（无 GPU 时）：打开 [ray_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/arithmetic_sequence/rl/config/ray_trainer.yaml)，找出三项：`rollout.name`（应为 `hf`，故不需要 vLLM）、`rollout.n`（应为 1，故不是 GRPO）、`algorithm.adv_estimator`（应为 `gae`，故需要 critic）。解释这三者为什么让 e2e 测试「最小化」。

#### 4.1.5 小练习与答案

**练习 1**：e2e 测试为什么把 `num_examine` 设为 1 而不是 0 或很大？
> 答案：设为 1 是为了每个 step 只打印 1 条样本供人眼检查「模型在说什么」，既提供了调试可见性，又不至于把日志刷爆；设为 0 则完全没有样本可见，设为大会淹没真正的指标行。

**练习 2**：`check_results.py` 为什么校验 `critic/rewards/mean` 而不是 `critic/score/mean`？
> 答案：rewards 是扣过 KL 罚后真正驱动优势计算的信号（见 u5-l1），score 只是任务原始分。不过在本 e2e 里没有 RefPolicy、KL 系数为 0，二者数值上几乎相等；用 rewards 更贴近「训练真正优化的目标」。

**练习 3**：e2e 测试创建了一个极小模型（仓库内自带 `model.safetensors`）。请说明它为什么不直接复用 Qwen2.5-0.5B？
> 答案：e2e 的目的是「验证流水线」，不是「训出好模型」。自带的随机/极小模型体积小、加载快、不依赖外网下载，最适合做分钟级的回归测试；任务也特意设计成「稠密奖励 + 简单规则」让随机权重也能学。

---

### 4.2 调试工具：显存打点、阶段计时与样本打印

#### 4.2.1 概念说明

分布式 RL 训练最难调的两类问题：**显存爆炸（OOM）** 和 **不知道时间花在哪**。veRL 没有花哨的可视化调试器，而是用三个朴素工具应对：

- `log_gpu_memory_usage`：在关键节点打印显存，定位是哪一步把显存吃光的。
- `_timer`（基于 `codetiming.Timer`）：给 `fit()` 的每个阶段掐秒表，折算成「每 token 毫秒」定位瓶颈。
- reward 函数里的 `num_examine` 打印：直接把 prompt/response 文本打出来，人眼判断模型行为。

这三个工具的共同特点是**侵入式但有成本极低**：只是一行 `print`/计时调用，不影响数值正确性。

#### 4.2.2 核心流程

显存打点的使用模式是「在某个大操作前后各打一点」，通过差值看这一步吃了多少显存：

```text
log_gpu_memory_usage('Before init from HF AutoModel', ...)   # 基线
   <加载模型 / FSDP 包装 / 建优化器>
log_gpu_memory_usage('After Actor FSDP init', ...)           # 增量 = FSDP 的开销
```

阶段计时则由 `fit()` 里的 `with _timer('gen', timing_raw):` 上下文管理器自动记录，最后由 `compute_timing_metrics` 折算。

#### 4.2.3 源码精读

显存打点的实现极简 [verl/utils/debug/performance.py:L20-L30](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/debug/performance.py#L20-L30)：

```python
def log_gpu_memory_usage(head, logger=None, level=logging.DEBUG, rank=0):
    if (not dist.is_initialized()) or (rank is None) or (dist.get_rank() == rank):
        memory_allocated = torch.cuda.memory_allocated() / 1024**3
        memory_reserved = torch.cuda.memory_reserved() / 1024**3
        message = f'{head}, memory allocated (GB): {memory_allocated}, memory reserved (GB): {memory_reserved}'
        ...
```

两个关键点：一是它区分 `memory_allocated`（PyTorch 实际占用的）和 `memory_reserved`（CUDA 缓存池预留的，通常更大）；二是 `rank=0` 默认只在 0 号卡打印，避免多卡重复刷屏。这个函数在 `fsdp_workers.py` 里被密集调用——模型初始化、FSDP 包装、优化器创建、rollout 引擎构建、权重同步前后都有打点，例如 [verl/workers/fsdp_workers.py:L267-L280](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L267-L280) 围着 vLLM rollout 与 sharding manager 的构建各打了一点。读这些打点的差值，就能回答「OOM 到底发生在建 actor 还是建 rollout」。

阶段计时器定义在 [verl/trainer/ppo/ray_trainer.py:L284-L288](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L284-L288)：

```python
@contextmanager
def _timer(name: str, timing_raw: Dict[str, float]):
    with Timer(name=name, logger=None) as timer:   # codetiming.Timer，见 import 第 28 行
        yield
    timing_raw[name] = timer.last
```

它把每个阶段的耗时（秒）写进 `timing_raw` 这个普通 dict。`fit()` 里用 `with _timer('gen', timing_raw):`、`with _timer('update_actor', timing_raw):` 等包住各阶段（见 4.4 的 fit 时序）。`Timer` 来自第三方库 `codetiming`（[ray_trainer.py:L28](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L28)），`logger=None` 表示它自己不打印，只把 `timer.last`（最近一次耗时）交给 `_timer` 收集。

第三个工具是样本打印，已在 4.1.3 见过：`if i < num_examine: print(prompt, response)`。在 u4-l4 的 `RewardManager` 里也有同款机制——`num_examine` 只控制「打印几条样本供调试」，不影响奖励数值。

#### 4.2.4 代码实践

1. **实践目标**：用显存打点定位一次前向/反向的显存增量。
2. **操作步骤**：仓库里有现成的演示 `tests/gpu_utility/test_ops.py`，它在一次 cross-entropy 的前向/反向前后各打了点 [tests/gpu_utility/test_ops.py:L26-L43](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/gpu_utility/test_ops.py#L26-L43)。在装了 flash-attn 的机器上跑：
   ```bash
   pytest tests/gpu_utility/test_ops.py -s
   ```
3. **需要观察的现象**：`At start` → `before computation` → `After forward` → `After backward` 四个点的 `memory allocated` 递增，反向后达到峰值。
4. **预期结果**：`After forward` 与 `At start` 的差 ≈ 激活与 logits 占用；`After backward` 的峰值反映反向所需的梯度缓存。
5. **待本地验证**：具体数值取决于 GPU 与 dtype，需本地实测。

> **源码阅读型替代实践**：在 [fsdp_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py) 中统计 `log_gpu_memory_usage` 的全部调用点，按 `init_model` 的执行顺序排成一列，推断「actor FSDP 初始化」「vLLM rollout 构建」「offload 优化器」三步各自的显存影响。

#### 4.2.5 小练习与答案

**练习 1**：`memory_allocated` 和 `memory_reserved` 哪个通常更大？为什么 OOM 时要看前者？
> 答案：`memory_reserved`（CUDA 缓存池）通常 ≥ `memory_allocated`（实际 tensor 占用）。OOM 是因为「想再分配却拿不到空闲显存」，而缓存池里可能有未被 tensor 占用的空闲块可复用，所以判断「是否真的用满」应看 `memory_allocated` 是否逼近总量，同时结合 `empty_cache()` 后的变化。

**练习 2**：`_timer` 为什么用 `logger=None`？
> 答案：因为它不需要 `codetiming.Timer` 自己去打印/记录日志，只需要在退出上下文时把 `timer.last` 取走、塞进 `timing_raw` dict，统一交给 `compute_timing_metrics` 处理。这样所有计时都汇聚到同一份指标里，而不是散落在各处日志。

**练习 3**：为什么 `log_gpu_memory_usage` 默认 `rank=0`？
> 答案：多卡训练时每张卡的显存占用通常对称（数据并行），全部打印会重复 `world_size` 份刷屏；只看 rank 0 即可代表整体趋势。需要排查某张卡异常时再显式传别的 rank。

---

### 4.3 实验跟踪：Tracking 统一日志后端

#### 4.3.1 概念说明

训练会产出几十个指标，光靠 `print` 到终端很难观察「随 step 变化的曲线」。业界做法是把指标送给一个**实验跟踪后端**（如 wandb）自动画图。veRL 的 `Tracking` 类把这个差异屏蔽掉：业务代码只管调用 `logger.log(data=metrics, step=step)`，至于落到 console、wandb 还是 mlflow，由构造时传入的 `default_backend` 决定。这是一个典型的「**统一接口、多后端**」设计。

#### 4.3.2 核心流程

```text
fit() 启动时：
   logger = Tracking(project_name, experiment_name,
                     default_backend=config.trainer.logger,   # 如 ['console'] 或 ['console','wandb']
                     config=<整份配置>)
        │
        ├─ 'console' → LocalLogger（每步拼成一行打印）
        ├─ 'wandb'   → wandb.init(...)（需要 WANDB_API_KEY）
        └─ 'mlflow'  → mlflow.start_run(...)（把配置展平成 params）

每个 step：
   logger.log(data=metrics_dict, step=global_steps)
        └─ 遍历所有已启用后端，逐个调用 .log(data, step)
```

#### 4.3.3 源码精读

`Tracking` 的定义见 [verl/utils/tracking.py:L24-L62](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/tracking.py#L24-L62)。构造函数先做后端校验与归一化：

```python
class Tracking(object):
    supported_backend = ['wandb', 'mlflow', 'console']
    def __init__(self, project_name, experiment_name, default_backend='console', config=None):
        if isinstance(default_backend, str):
            default_backend = [default_backend]
        ...
        if 'tracking' in default_backend or 'wandb' in default_backend:
            ... wandb.init(project=..., name=..., config=config)
            self.logger['wandb'] = wandb
        if 'console' in default_backend:
            self.console_logger = LocalLogger(print_to_console=True)
            self.logger['console'] = self.console_logger
```

注意 `'tracking'` 是 `'wandb'` 的**废弃别名**（会发 `DeprecationWarning`），二者走同一分支。`log` 方法是统一入口：

```python
def log(self, data, step, backend=None):
    for default_backend, logger_instance in self.logger.items():
        if backend is None or default_backend in backend:
            logger_instance.log(data=data, step=step)
```

`backend=None` 时广播到所有后端；传具体名字则只落指定后端。

console 后端的真实面目在 [verl/utils/logger/aggregate_logger.py:L21-L42](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/logger/aggregate_logger.py#L21-L42)，`concat_dict_to_str` 把 dict 拼成 `step:N - k1:v1.000 - k2:v2.000` 这样一行——这正是 4.1 里 `check_results.py` 按 ` - ` 和 `:` 切分解析的格式来源。两个系统（打印方与解析方）靠这一行文本约定耦合在一起。

那么 `Tracking` 在哪里被创建？在 `fit()` 开头 [verl/trainer/ppo/ray_trainer.py:L553-L559](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L553-L559)：

```python
from verl.utils.tracking import Tracking
logger = Tracking(project_name=self.config.trainer.project_name,
                  experiment_name=self.config.trainer.experiment_name,
                  default_backend=self.config.trainer.logger,
                  config=OmegaConf.to_container(self.config, resolve=True))
```

后端由配置项 `trainer.logger` 决定。e2e 的 yaml 里是 `logger: ['console']`（[ray_trainer.yaml:L146](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/e2e/arithmetic_sequence/rl/config/ray_trainer.yaml#L146)）；TinyZero 真实训练脚本里可改成 `['console','wandb']` 同时画图。`config=OmegaConf.to_container(..., resolve=True)` 把整份（已插值解析的）配置也送进后端，这样 wandb 里能看到每次实验用的全部超参。

`fit()` 里有**三个** `logger.log` 打点时机：训练前验证（[L568](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L568)）、每个 step（[L678](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L678)）、训练结束后最终验证（[L688](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L688)），都以 `self.global_steps` 为横轴，保证曲线连续。

#### 4.3.4 代码实践

1. **实践目标**：把 e2e 测试的日志后端从 console 切到 console+wandb，看指标如何变成曲线。
2. **操作步骤**：
   - 设置环境变量并登录一次：`export WANDB_API_KEY=...`（首次需 `wandb login`，匿名可用 `WANDB_MODE=offline` 离线模式）。
   - 编辑 `tests/e2e/arithmetic_sequence/rl/config/ray_trainer.yaml`，把 `logger: ['console']` 改为 `logger: ['console','wandb']`。
   - 重新跑 `bash tests/e2e/run_ray_trainer.sh`。
3. **需要观察的现象**：终端仍打印每步指标行；同时 wandb 会启动一个 run，网页上能看到 `critic/rewards/mean`、`actor/pg_loss`、`response_length/mean` 等曲线。
4. **预期结果**：`critic/rewards/mean` 曲线整体上升趋势，`actor/pg_loss` 在 0 附近波动。
5. **待本地验证**：wandb 联网与曲线形态需本地实测。
6. **提示**：因 `check_results.py` 只读 `/tmp/output_ray_trainer.txt`（终端 `tee` 的文本），切到 wandb 不影响断言。

> **源码阅读型替代实践**：阅读 [tracking.py:L48-L52](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/tracking.py#L48-L52) 与 `_compute_mlflow_params_from_objects`，说明 mlflow 后端为什么要把 list 转成 `{'list_len': N, '0': ..., '1': ...}` 的 dict（提示：mlflow 的 params 是扁平键值对，不支持嵌套/列表）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Tracking.log` 用 `for ... in self.logger.items()` 而不是固定调用某个后端？
> 答案：因为 `Tracking` 支持同时启用多个后端（如 `['console','wandb']`）。把启用后端存进 dict 并遍历，能让一次 `log` 调用广播到所有后端，业务代码无需感知后端数量。`backend` 参数只是用来「只往部分后端写」的可选过滤。

**练习 2**：`Tracking` 把整份 `config` 也传给后端（`config=...`），有什么用？
> 答案：让每次实验的超参（学习率、kl_coef、batch_size 等）和指标一起被记录，方便事后对比「不同配置下的曲线差异」，是实验可复现与可比较的基础。

**练习 3**：`LocalLogger` 的 `flush` 方法是空的（`pass`），这说明什么？
> 答案：console 后端是无状态、即时打印的（每次 `log` 直接 `print(..., flush=True)`），没有需要缓冲累积再刷出的状态，所以 `flush` 无事可做。它主要是为了和「有缓冲语义」的 logger 保持接口一致而保留的空实现。

---

### 4.4 指标体系：compute_data_metrics 与 compute_timing_metrics

#### 4.4.1 概念说明

`fit()` 每个 step 会汇聚出一个**大指标 dict**，它由四股来源合并而成：

1. **actor 汇报**（`update_actor` 返回）：`actor/pg_loss`、`actor/pg_clipfrac`、`actor/ppo_kl`、`actor/entropy_loss`、`actor/grad_norm`（GRPO 还有 `actor/kl_loss`）。
2. **critic 汇报**（`update_critic` 返回）：`critic/vf_loss`、`critic/vf_clipfrac`、`critic/vpred_mean`、`critic/grad_norm`。
3. **数据侧**（`compute_data_metrics`）：score/reward/advantages/returns/values/response_length/prompt_length。
4. **计时侧**（`compute_timing_metrics`）：各阶段秒数与「每 token 毫秒」。

理解这些 key 的含义，是判断「训练是否健康」的前提——比如 `actor/pg_clipfrac` 飙高说明策略更新过于激进，`critic/vf_explained_var` 接近 1 说明价值函数拟合得好，`response_length/clip_ratio` 高说明回答频繁撞到长度上限。

#### 4.4.2 核心流程

`fit()` 末尾的汇聚逻辑 [verl/trainer/ppo/ray_trainer.py:L646-L678](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L646-L678)：

```text
update_critic  → critic_output.meta_info['metrics']  ──┐
update_actor   → actor_output.meta_info['metrics']   ──┤
compute_data_metrics(batch, use_critic)               ──┼─→ metrics ─→ logger.log
compute_timing_metrics(batch, timing_raw)             ──┘
```

其中 actor/critic 的 metrics 是「list 套标量」（因为 `append_to_dict` 把每个 micro-batch 的值都 append 进 list），需经 `reduce_metrics` 取 `np.mean` 压成单值 [verl/trainer/ppo/ray_trainer.py:L150-L153](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L150-L153)。而 `compute_data_metrics` 直接算 `.item()` 返回标量，无需 reduce。

#### 4.4.3 源码精读

**（a）数据指标** [verl/trainer/ppo/ray_trainer.py:L172-L257](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L172-L257)。它先把序列级的 score/reward 算出来（对 token 维求和），再用 `response_mask` 把 advantage/returns/values 限定在有效 token 上：

```python
sequence_score  = batch.batch['token_level_scores'].sum(-1)   # 每条样本的任务总分
sequence_reward = batch.batch['token_level_rewards'].sum(-1)  # 每条样本的奖励（含 KL 罚）
...
valid_adv    = torch.masked_select(advantages, response_mask) # 只取有效 response token
```

产出的指标可按下表理解（结合 u5-l1~u5-l3 的概念）：

| 指标 | 含义 | 健康信号 |
| --- | --- | --- |
| `critic/score/mean` | 平均任务分（原始） | 随训练上升 |
| `critic/rewards/mean` | 平均奖励（扣 KL 罚后） | 随训练上升 |
| `critic/advantages/mean` | 有效 token 上的平均优势 | 围绕 0 波动 |
| `critic/returns/mean` | 平均回报（advantage+value） | 与 rewards 尺度相近 |
| `critic/vf_explained_var` | 价值函数解释的方差比 | 越接近 1 越好 |
| `response_length/mean` | 平均回答长度 | R1 Zero 下会变长 |
| `response_length/clip_ratio` | 回答撞到长度上限的比例 | 过高需调大 `max_response_length` |

其中 `vf_explained_var` 的公式为：

\[ \text{vf\_explained\_var} = 1 - \frac{\mathrm{Var}(\text{returns} - \text{values})}{\mathrm{Var}(\text{returns}) + \epsilon} \]

分子是「残差的方差」，分母是「目标的方差」，比值越小（解释方差越接近 1）说明 critic 对 returns 拟合得越好；对应源码 [ray_trainer.py:L197-L198,L235](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L197-L198)。`response_length` 等长度信息来自辅助函数 `_compute_response_info` [ray_trainer.py:L156-L169](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L156-L169)，它用 `attention_mask` 切分 prompt 段与 response 段并各自求和。

**（b）actor/critic 汇报的指标**。actor 侧在 `update_policy` 里逐 micro-batch 累积 [verl/workers/actor/dp_actor.py:L274-L284](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L274-L284)：

```python
data = {
    'actor/entropy_loss': entropy_loss.detach().item(),
    'actor/pg_loss':      pg_loss.detach().item(),
    'actor/pg_clipfrac':  pg_clipfrac.detach().item(),   # 被 clip 的 token 比例
    'actor/ppo_kl':       ppo_kl.detach().item(),        # 新旧策略近似 KL
}
append_to_dict(metrics, data)
...
data = {'actor/grad_norm': grad_norm.detach().item()}
```

`append_to_dict` 把每个 micro-batch 的标量追加进 list [verl/utils/py_functional.py:L41-L45](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/py_functional.py#L41-L45)，最后由 `reduce_metrics` 取均值。critic 侧同构，产出 `critic/vf_loss`、`critic/vf_clipfrac`、`critic/vpred_mean`、`critic/grad_norm` [verl/workers/critic/dp_critic.py:L193-L202](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L193-L202)。这些 `actor/*`、`critic/*` 前缀与 `compute_data_metrics` 里的 `critic/score` 等**共享 `critic/` 前缀但来源不同**——一个来自价值网络的训练损失，一个来自数据的统计，阅读时要注意区分。

**（c）计时指标** [verl/trainer/ppo/ray_trainer.py:L260-L281](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L260-L281)。它把 `timing_raw`（4.2 的 `_timer` 收集的秒数）原样输出为 `timing_s/{name}`，并折算「每 token 毫秒」：

\[ \text{timing\_per\_token\_ms}[name] = \frac{\text{timing\_raw}[name] \times 1000}{\text{num\_tokens\_of\_section}[name]} \]

其中 `gen` 段按 response token 数折算，`ref/values/adv/update_critic/update_actor` 段按总 token 数折算（见 `num_tokens_of_section` 字典）。这样不同 batch 大小的实验也能横向比较「生成 vs 训练」谁更慢。

#### 4.4.4 代码实践

1. **实践目标**：从一次 e2e 运行的 console 日志中提取并解读关键指标。
2. **操作步骤**：跑完 4.1 的 `bash tests/e2e/run_ray_trainer.sh 2>&1 | tee /tmp/e2e_full.log` 后，取最后一个 step 的那一行，按 ` - ` 切分，挑出以下 key 的值：`critic/score/mean`、`critic/rewards/mean`、`actor/pg_loss`、`actor/pg_clipfrac`、`actor/ppo_kl`、`response_length/mean`、`response_length/clip_ratio`、`timing_s/gen`、`timing_s/update_actor`。
3. **需要观察的现象**：
   - 比较**最早**与**最后**若干 step：`critic/score/mean` 是否上升？`response_length/mean` 是否变长？
   - `actor/pg_clipfrac` 是否在合理范围（通常远小于 1）；`actor/ppo_kl` 是否较小（说明策略没跑飞）。
4. **预期结果**：score 上升、response_length 趋稳或略升、pg_clipfrac 较小。把这几列抄成一张小表。
5. **待本地验证**：具体数值依赖本地运行结果。
6. **与 R1 Zero 现象的联系**：u1-l1 讲过，R1 Zero 的标志是模型「自发涌现自我验证与搜索」，外在表现为**回答变长且奖励上升**。在指标上，这正是 `response_length/mean` 与 `critic/score/mean` **同时上升**的形态——模型学会了「多想几步再作答」。TinyZero 的 countdown 任务上同样能观察到 response_length 在训练中逐步增长（伴随 score 上升），这就是小尺度下对「Aha moment」的可观测信号。若 `response_length/clip_ratio` 持续走高（大量回答撞到 `max_response_length` 上限），则要考虑调大上限，否则会人为截断模型的推理过程。

> **源码阅读型替代实践**：对照 `compute_data_metrics` 的返回 dict，把每个 key 标注「来自哪个张量字段」（如 `critic/score/* ← token_level_scores`、`critic/rewards/* ← token_level_rewards`、`critic/advantages/* ← advantages`），画出「字段 → 指标」的映射表，复习 u5-l1 的数据流。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `compute_data_metrics` 里 advantage/returns/values 要先 `torch.masked_select(..., response_mask)` 再统计，而 score/reward 直接 `.sum(-1)`？
> 答案：score/reward 本身只在有效 token 上非零（reward 由 u4-l4 的末位放置、稀疏/稠密设计保证），对整行求和等价于对有效 token 求和；而 advantage/returns/values 在 pad 位置可能非零（如 whitening 前的原始值或 value 前向的逐 token 输出），必须用 mask 过滤掉 pad，否则统计被填充位污染。

**练习 2**：`actor/ppo_kl` 和 `actor/pg_clipfrac` 分别监控什么？哪个飙高更危险？
> 答案：`ppo_kl` 是新策略相对旧策略的近似 KL（见 u5-l2），衡量一步更新后策略漂移多大；`pg_clipfrac` 是被 clip 的 token 比例，衡量有多少更新被 PPO 的双侧裁剪封住。`ppo_kl` 飙高更危险——它意味着策略在一步内跑得太远，可能训练崩溃；`pg_clipfrac` 偏高通常只是提示 ε 偏小或学习率偏大，相对可控。

**练习 3**：`compute_timing_metrics` 为什么对 `gen` 段用 response token 数折算，而对 `update_actor` 段用总 token 数折算？
> 答案：生成的计算量正比于产出的 response token 数（逐 token 自回归），所以按 response token 数归一；而 actor 的前向/反向要处理整条序列（prompt+response），计算量正比于总 token 数，故按总 token 数归一。这样得到的「每 token 毫秒」才在各自阶段内可比、且能跨实验横向比较。

---

## 5. 综合实践

把本讲四块知识串起来，做一次「**给 e2e 测试加一个自定义诊断指标**」的小任务：

**任务**：在 `compute_data_metrics` 之外，仿照它的写法，给 e2e 训练新增一个诊断量——「回答里首个 eos 之前的平均长度占 `max_response_length` 的比例」，并让它通过 `Tracking` 落到 console。

**步骤**：

1. **理解数据**：response 段的有效长度由 `attention_mask` 末尾切片决定（见 `_compute_response_info`），`response_length/mean` 已经给了「有效长度均值」，`response_length/clip_ratio` 给了「撞上限的比例」。你要新增的是一个介于二者之间的诊断。
2. **定位接入点**：指标最终都汇入 `fit()` 的 `metrics` dict 并经 `logger.log` 输出（[ray_trainer.py:L674-L678](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L674-L678)）。最省事的做法是把新指标加进 `compute_data_metrics` 的返回 dict（它本就有 `response_length/*` 这一族）。
3. **写代码（示例代码，非项目原有）**：在 `compute_data_metrics` 里仿照现有 `response_length/mean` 的写法，加一行例如：
   ```python
   'response_length/valid_ratio':
       (torch.mean(response_length) / max_response_length).detach().item(),
   ```
   这会复用已有的 `response_length`（每条样本的有效 response 长度）和 `max_response_length`。
4. **验证**：重跑 e2e，确认日志行里出现 `response_length/valid_ratio:...`；随训练观察它是否随 `critic/score/mean` 一起上升——若上升，说明模型倾向于生成更长的有效回答，呼应 R1 Zero 的「回答变长」现象。
5. **反思**：思考这个指标和 `clip_ratio` 的区别——`valid_ratio` 反映「平均用了多长」，`clip_ratio` 反映「有多少被截断」，二者结合才能完整刻画生成长度行为。

> 提示：本任务只改 `compute_data_metrics` 一处、加一行，不触碰任何 Worker 或算法代码——这正是 veRL「指标在 driver 侧集中计算」设计带来的便利（见 u4-l3）。

## 6. 本讲小结

- `tests/e2e/arithmetic_sequence` 是 veRL 的**烟雾测试**：用极小模型 + 稠密奖励 + HF rollout + `n=1`，几分钟单卡跑通整条 RL 链路；`run_ray_trainer.sh` 跑完再用 `check_results.py` 断言 `critic/rewards/mean > 0.2`。
- 调试三件套真实存在且朴素：`log_gpu_memory_usage` 打显存（区分 allocated/reserved、默认 rank 0）、`_timer`(codetiming) 给 `fit()` 各阶段掐秒表、reward 函数里 `num_examine` 控制样本打印；**仓库中没有 `trajectory_tracker`**。
- `Tracking` 是「统一接口、多后端」的日志器：`console`/`wandb`/`mlflow` 三选多，由 `trainer.logger` 配置决定；在 `fit()` 里有训练前、每 step、训练后三个 `logger.log` 打点。
- 每个 step 的大指标 dict 由四股合并：actor/critic 各自汇报（经 `reduce_metrics` 取均值）+ `compute_data_metrics`（数据统计，直接 `.item()`）+ `compute_timing_metrics`（秒数与每 token 毫秒）。
- 关键判读：`critic/score|rewards/mean` 看任务学没学会、`actor/pg_clipfrac|ppo_kl` 看策略更新是否激进、`critic/vf_explained_var` 看 critic 拟合好坏、`response_length/*` 看生成长度行为。
- **R1 Zero 联系**：`response_length/mean` 与 `critic/score/mean` 同时上升，就是小尺度下「模型学会多想几步」的可观测信号，对应 u1-l1 的 Aha moment。

## 7. 下一步学习建议

- 下一讲 **u7-l6「R1 Zero 'Aha' 现象与调参解读」** 会把本讲的指标判读上升到「调参」层面：结合 `format_score`、`kl_coef`、reward shaping 讨论「只刷格式」等 reward hacking 风险，以及 3B vs 0.5B 的涌现差异。建议先掌握本讲的 `pg_clipfrac`、`vf_explained_var`、`response_length` 三个指标。
- 想深入指标产出的算法侧，回看 **u5-l2（compute_policy_loss 返回 pg_loss/pg_clipfrac/ppo_kl）** 与 **u5-l3（compute_value_loss 返回 vf_loss/vf_clipfrac）**，理解这些监控量如何在损失函数里被算出。
- 想理解 e2e 测试里「为什么能不接规则奖励路由」而 main_ppo 要接，回看 **u4-l1（RewardManager 与 `_select_rm_score_fn`）** 与 **u4-l4（奖励计算与末位放置）**。
- 若你打算给自己的任务写 e2e 测试，可参考 `tests/e2e/arithmetic_sequence/data/create_dataset.py` 与 `model/create_model_tokenizer.py`，把数据和极小模型一起 vendored 进仓库，保证测试离线可跑。
