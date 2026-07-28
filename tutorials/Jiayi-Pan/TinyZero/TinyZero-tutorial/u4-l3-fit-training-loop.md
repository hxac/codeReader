# fit() 训练主循环全流程

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `fit()` 单步训练（一个 step）的完整阶段顺序，以及每一步在做什么。
- 把每个阶段对应到正确的 worker group（`actor_rollout_wg` / `ref_policy_wg` / `critic_wg` / `rm_wg`），并分清哪些是 driver 本地计算。
- 理解 `batch.repeat(n)` 如何与 rollout 的 n 次采样对齐，以及 `uid` 为什么要在 repeat 之前赋值。
- 看懂 `_balance_batch` 为什么要在分片前重排数据、`global_token_num` 起什么作用。
- 读懂 `_timer` / `compute_timing_metrics` 的计时与「每 token 折算」逻辑。
- 区分 `reward_fn` 与 `val_reward_fn` 的差异。

## 2. 前置知识

本讲承接 u4-l2（RayPPOTrainer 初始化与 Worker 编排）与 u3-l1（DataProto 数据传输协议），以下结论默认你已经掌握：

- **driver 进程跑主循环**，握有完整 `DataProto`，通过 worker group 的方法（RPC）编排；各 GPU 上是被动的 worker。fit 的注释明确写着「The driver process only need to call the compute functions of the worker group through RPC」。
- **四个 worker group**：`actor_rollout_wg`（rollout 生成 + actor 更新）、`ref_policy_wg`（参考策略）、`critic_wg`（价值网络）、`rm_wg`（奖励模型，TinyZero 默认关）。
- **`use_critic` 由 `adv_estimator` 决定**：`gae` 需要 critic，`grpo` 借组内归一化省掉价值网络（见 u4-l2）。
- **DataProto 三段结构**：`batch`（张量）、`non_tensor_batch`（任务类型、正确答案等）、`meta_info`（整批共享的全局元信息）。
- **DataProto 的 `chunk`/`concat`/`union`/`repeat`/`reorder`** 是 driver 与 worker 之间搬运数据的工具（详见 u3-l1）。

一句话的 RL 直觉：PPO/GRPO 的一步训练 = 「采样回答 → 评估每条回答的好坏 → 把好坏变成优势（advantage）→ 用优势更新策略」。fit 就是把这个流程串起来的指挥。

关键术语：

| 术语 | 含义 |
|---|---|
| `token_level_scores` | 奖励函数给出的稀疏分数（只在回答末位 token 非零） |
| `token_level_rewards` | scores 扣除 KL 惩罚后的「真正用于学习」的奖励 |
| `advantage` | 每个动作相对平均水平的好坏，直接驱动策略更新 |
| DP（data parallel） | 同一个 batch 切成 `world_size` 份分到各 GPU |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) | `fit()` 主循环、`_balance_batch`、`_validate`、`apply_kl_penalty`、`compute_advantage`、`compute_timing_metrics`、`_timer` 全部在本讲范围 |
| [verl/protocol.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py) | DataProto 的 `repeat`/`union`/`reorder` 等，fit 里反复用到的数据操作 |
| [verl/utils/seqlen_balancing.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py) | Karmarkar-Karp 分区算法，`_balance_batch` 调用它均衡各 rank token 数 |
| [verl/trainer/main_ppo.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py) | `RewardManager` 与 `reward_fn`/`val_reward_fn` 的构造，理解两者区别 |

## 4. 核心概念与源码讲解

### 4.1 fit() 全景：driver 进程的编排循环骨架

#### 4.1.1 概念说明

`fit()` 是整个训练的「指挥」。它运行在 driver 进程（单个 CPU 节点，类注释写明 `runs on the driver process on a single CPU/GPU node`），自己几乎不算任何梯度，只做两件事：

1. 按顺序调用各 worker group 的方法，让远端 GPU 干活；
2. 在本地做轻量计算（奖励拼装、KL 惩罚、优势估计、指标统计）。

这正是 u4-l2 讲的「单控制器」架构：driver 握有完整 `DataProto`，决定数据往哪送、什么时候送、怎么拼回来。

#### 4.1.2 核心流程

```
fit()
 ├─ 创建 logger（Tracking）
 ├─ 训练前验证（val_before_train；若 val_only 为真直接退出）
 ├─ global_steps = 1
 └─ for epoch in total_epochs:
      for batch_dict in train_dataloader:
         ── 一个 step 开始 ──
         ├─ 1. 生成与对齐        (gen)
         ├─ 2. 序列长度均衡      (_balance_batch)
         ├─ 3. 前向估计          (ref → values)
         ├─ 4. 奖励 → 优势       (adv)
         ├─ 5. 反向更新          (update_critic → update_actor)
         ├─ 6. 验证/存盘         (testing → save_checkpoint)
         ├─ 统计指标并 log
         ├─ global_steps += 1
         └─ 达到 total_training_steps 则验证后 return
```

每个 step 的所有计算都包在一个 `with _timer('step', ...)` 里，内部各阶段再用各自的 `_timer`，最终由 `compute_timing_metrics` 折算成「每 token 耗时」。

注意早停逻辑：训练步数由 `total_training_steps` 封顶（= dataloader 长度 × `total_epochs`，或被配置显式覆盖），达到后做一次最终验证并 `return`，不一定跑满所有 epoch。

#### 4.1.3 源码精读

fit 的整体骨架与训练前验证：

[verl/trainer/ppo/ray_trainer.py:547-573](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L547-L573) —— fit 定义、创建 logger、`val_before_train` 训练前验证（若 `val_only` 为真则直接 return）。

主循环与 step 计时框架：

[verl/trainer/ppo/ray_trainer.py:575-600](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L575-L600) —— 双层 `for` 循环，每步新建空的 `metrics`/`timing_raw`，把 `batch_dict` 包成 `DataProto`，并 `pop` 出生成所需的张量字段；最外层 `with _timer('step', timing_raw)`。

指标收集与早停：

[verl/trainer/ppo/ray_trainer.py:673-689](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L673-L689) —— 每步末尾用 `compute_data_metrics` / `compute_timing_metrics` 汇总并 `logger.log`，`global_steps += 1`，达到 `total_training_steps` 做最终验证后 return。

其中把 `batch_dict` 转成 `DataProto`、并 pop 出生成字段的关键两行：

```python
batch: DataProto = DataProto.from_single_dict(batch_dict)
gen_batch = batch.pop(batch_keys=['input_ids', 'attention_mask', 'position_ids'])
```

`pop` 的作用是把「生成需要的 `input_ids`/`attention_mask`/`position_ids`」单独拎出来给 rollout 引擎，剩余的 `non_tensor_batch`（`data_source`、`ground_truth` 等）留在 `batch` 里，等生成结果回来再 `union` 合并。

#### 4.1.4 代码实践

**目标**：建立 fit 的整体骨架心智模型。

**步骤**：

1. 打开 [ray_trainer.py:547](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L547)，从 `fit` 往下读到第一个 `with _timer('step', ...)`。
2. 用纸画出「双层 for 循环 + 6 个阶段 + 末尾 log + 早停 return」的骨架。
3. 找到 `total_training_steps` 的来源（提示：在 [`_create_dataloader`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L379-L385) 里 = `len(train_dataloader) * total_epochs`）。

**需要观察的现象**：训练前验证只发生在 `val_reward_fn is not None` 与 `val_before_train` 同时成立时。

**预期结果**：能说清「一个 step = 一个 batch 的一次完整 PPO 更新」。

#### 4.1.5 小练习与答案

**Q1**：为什么 fit 把每个 step 的计算都包进 `with _timer('step')`？

**A**：为了测量单步总耗时，并把它和内部各阶段（gen/ref/values/adv/update_*）的耗时一起记录，方便定位性能瓶颈（见 4.5 的 `compute_timing_metrics`）。

**Q2**：如果配置了 `trainer.val_only=True`，fit 会做什么？

**A**：在训练前验证之后直接 return，不进入训练循环（见 [569-570](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L569-L570)）。

---

### 4.2 生成与数据对齐：generate → repeat(n) → union

#### 4.2.1 概念说明

rollout 阶段（vLLM 生成）对一个 prompt 可以采样 `n` 次（`actor_rollout_ref.rollout.n`，这是 GRPO 的核心入口参数）。这意味着「1 个 prompt → n 条回答」。但 prompt 侧的非张量信息（`data_source`、`ground_truth`）只有 1 份，回答侧却产出 n 份。

要对齐这两侧，fit 用 `batch.repeat(repeat_times=n, interleave=True)` 把 prompt 侧复制 n 份，再用 `union` 把生成结果（含 `responses`、`old_log_probs` 等）横向合并进来。

`interleave=True` 的语义：样本顺序变成 `[p0,p1,p2, p0,p1,p2]`，即「同一 prompt 的 n 份紧挨在一起」。这正是 GRPO 组内归一化（u5-l5）所需要的「同组相邻」排列。

#### 4.2.2 核心流程

```
gen_batch = batch.pop([input_ids, attention_mask, position_ids])   # 仅张量给生成
gen_batch_output = actor_rollout_wg.generate_sequences(gen_batch)  # vLLM 生成 n 条/每 prompt
                                                                   # 该输出 batch_size = 原 prompt 数 × n
batch[uid] = 随机 uuid（给每条原 prompt 一个唯一 id，供 GRPO 分组）
batch = batch.repeat(n, interleave=True)                           # prompt 侧复制 n 份，对齐
batch = batch.union(gen_batch_output)                              # 横向合并生成结果
```

生成结果的 batch_size 已经是「原 prompt 数 × n」，所以 prompt 侧必须 `repeat` 到同样大小才能 `union`（union 要求两边 batch_size 相等，见 u3-l1）。

`uid` 字段：每条原 prompt 一个唯一字符串，在 repeat **之前**赋值。repeat 会把同一个 uid 复制 n 份，正好让「同一 prompt 的 n 条回答」共享一个 uid——GRPO 在 `compute_advantage` 里用 `non_tensor_batch['uid']` 把它们归为一组算均值/标准差。

#### 4.2.3 源码精读

生成、赋 uid、repeat、union：

[verl/trainer/ppo/ray_trainer.py:586-595](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L586-L595) —— 这是 fit 里最关键的数据对齐片段。注释「repeat to align with repeated responses in rollout」点明了 repeat 的目的。

`DataProto.repeat` 的 interleave 实现：

[verl/protocol.py:547-589](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L547-L589) —— `interleave=True` 时张量用 `repeat_interleave`（同 prompt 的 n 份相邻），非张量用 `np.repeat`（行为一致）；`interleave=False` 时则用 `expand/reshape`（同一份连续重复 n 次）。

`DataProto.union`：

[verl/protocol.py:423-439](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L423-L439) —— 横向合并两个 batch_size 相同的 DataProto，重名字段必须相等否则报错。

interleave 分支的关键代码：

```python
repeated_tensors = {key: tensor.repeat_interleave(repeat_times, dim=0)
                    for key, tensor in self.batch.items()}
```

即在第 0 维做 `repeat_interleave`：`[A,B]` × 2 → `[A,A,B,B]`。

#### 4.2.4 代码实践

**目标**：用一个迷你 DataProto 验证 `repeat + union` 的对齐行为（不依赖 GPU）。

**步骤**：

1. 写一个小脚本（示例代码，非项目原有代码）构造 prompt 侧 batch（含一个标记 `prompt_id` 的张量字段）和一个「伪生成结果」batch（batch_size = 原 × n，含 `responses` 占位）：

   ```python
   # 示例代码：需本地装有 torch + tensordict
   from verl import DataProto
   import torch
   prompt = DataProto.from_dict(tensors={'prompt_id': torch.tensor([0, 1])})
   fake_gen = DataProto.from_dict(tensors={'responses': torch.zeros(4, 8)})  # n=2 → 4 条
   ```

2. 对 prompt 侧 `repeat(n=2, interleave=True)`，再 `union` 伪结果，打印 batch_size 与每条样本的 `prompt_id` 顺序。
3. 对比 `interleave=True` 与 `interleave=False` 的样本排列差异。

**需要观察的现象**：`interleave=True` 时 `prompt_id` 序列是 `[0,0,1,1,...]`，`interleave=False` 时是 `[0,1,...,0,1,...]`。

**预期结果**：理解为什么 GRPO 必须用 `interleave=True`（组内相邻），并明白 fit 注释为何提醒「`_balance_batch` 会打乱顺序要小心」（见 4.3）。

> 如本地未装 verl 依赖，具体数值「待本地验证」，但 `[A,A,B,B]` 与 `[A,B,A,B]` 的逻辑可手推。

#### 4.2.5 小练习与答案

**Q1**：为什么 `uid` 要在 repeat 之前赋值，而不是之后？

**A**：repeat 之前赋，每条 prompt 一个 uid，repeat 会把它复制 n 份；这样同 prompt 的 n 条回答共享同一 uid，GRPO 才能按 uid 分组。若之后赋，每条回答都会有不同 uid，分组就失效了。

**Q2**：如果 `rollout.n=1`（PPO 单次采样），`repeat(1)` 会不会改变数据？

**A**：不会，`repeat_times=1` 时数据不变（batch_size 不变），逻辑等价于直接 union。

---

### 4.3 序列长度均衡：_balance_batch 与 global_token_num（核心模块）

#### 4.3.1 概念说明

数据并行（DP）下，一个 batch 会被 `chunk(world_size)` 切成 `world_size` 份，每份送一张 GPU（见 u3-l1 的 chunk）。如果各份的「有效 token 总数」差异很大（有的 rank 全是长序列、有的全是短序列），那么最慢的那张卡决定整体速度（木桶效应），快的卡空等。

`_balance_batch` 在 driver 端、分发之前重排 batch 顺序，使切分后每个 rank 拿到的有效 token 总数尽量接近。它用的是 **Karmarkar-Karp 最大差分法**（一种近似均衡分区的贪心 + 堆算法）。

随后 fit 把每条样本的有效 token 数写进 `meta_info['global_token_num']`，供下游 worker（actor/critic 的动态 micro batch 切分）参考。

#### 4.3.2 核心流程

```
global_seqlen_lst = 每条样本 attention_mask 求和（= 该样本有效 token 数）
world_size = actor_rollout_wg.world_size（GPU 数）
partitions = get_seqlen_balanced_partitions(global_seqlen_lst, k=world_size, equal_size=True)
            ↑ Karmarkar-Karp：把样本分成 world_size 组，每组 token 总和尽量相等、且样本数相等
global_idx = 把 k 组下标展平成一个重排索引
batch.reorder(global_idx)   # 原地重排
batch.meta_info['global_token_num'] = 每条样本 token 数（tolist）
```

`equal_size=True` 要求 batch_size 能被 `world_size` 整除（每组样本数相等），这样 dispatch 的等分 chunk 才能正常工作。注意 reorder 会打乱原本「同 prompt 相邻」的顺序——fit 的注释专门提醒 GRPO/rloo 这类组方法要当心（好在 GRPO 用 uid 分组而非物理相邻，所以不受影响）。

Karmarkar-Karp 的直觉（以 k=2 为例）：把数字排序后，反复取「当前差最大的两个集合」做差分合并（大的配小的），逐步逼近两组和相等。扩展到 k 组就是 k 路最大差分。

#### 4.3.3 源码精读

`_balance_batch` 全文：

[verl/trainer/ppo/ray_trainer.py:530-545](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L530-L545) —— 统计每条样本 token 数、调用平衡分区、reorder、记录均衡度指标。

`global_token_num` 写入：

[verl/trainer/ppo/ray_trainer.py:603](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L603) —— `batch.meta_info['global_token_num'] = torch.sum(batch.batch['attention_mask'], dim=-1).tolist()`。

Karmarkar-Karp 分区算法：

[verl/utils/seqlen_balancing.py:152-183](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L152-L183) —— `get_seqlen_balanced_partitions` 入口，内部调用 [karmarkar_karp](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L25-L130)。

均衡度统计：

[verl/utils/seqlen_balancing.py:186-217](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L186-L217) —— `log_seqlen_unbalance` 输出 `min/max/minmax_diff/balanced_min/balanced_max/mean`，可在日志里看到均衡前后各 rank token 总和的差距。

`DataProto.reorder`（原地）：

[verl/protocol.py:539-545](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L539-L545) —— 同时按索引重排 batch 张量与 `non_tensor_batch`。

#### 4.3.4 代码实践

**目标**：手算一次平衡分区，理解它如何减少各 rank 的 token 差距。

**步骤**：

1. 设 `seqlen_list = [10, 20, 30, 40, 50, 60]`，`world_size=2`（k=2，`equal_size=True`，每组 3 个）。
2. 朴素顺序切分会得到 `[10,20,30]`（和 60）vs `[40,50,60]`（和 150），差 90。
3. 按 [karmarkar_karp](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L103-L130) 的逻辑：排序后配对初始 State，再反序合并，手推两组划分（理想情况下两组和都接近 105）。
4. 对比两种切分的 `minmax_diff`。

**需要观察的现象**：平衡后两组 token 总和的差显著小于朴素切分。

**预期结果**：理解 `_balance_batch` 的收益是「让各 GPU 工作量均衡，消除木桶效应」。具体分组「待本地验证」（可写小脚本直接调用 `get_seqlen_balanced_partitions([10,20,30,40,50,60], 2, True)` 打印结果）。

#### 4.3.5 小练习与答案

**Q1**：为什么 `_balance_batch` 必须在 dispatch 分片之前、且在 driver 端做？

**A**：因为分片（`chunk(world_size)`）一旦发生，数据已散到各 worker，driver 无法再全局重排。只有 driver 握有完整 batch，才能做跨 rank 的全局均衡决策。

**Q2**：reorder 打乱了顺序，对 GRPO 有什么风险？

**A**：GRPO 依赖「同 prompt 的 n 条回答」相邻（4.2 的 interleave），reorder 理论上可能把它们打散。fit 的注释明确提醒 GRPO/rloo 实现者要注意——实际 GRPO 用 `uid` 分组而非依赖物理相邻，所以不受影响。

---

### 4.4 奖励链路：ref → values → reward → KL penalty → advantage

#### 4.4.1 概念说明

这一段把「生成出来的回答」转换成「可以用来更新策略的 advantage」。分四步：

1. **ref_log_prob**：用冻结的参考策略（基座模型）算每个 token 的对数概率，作为 KL 的参考基准（仅当 `use_reference_policy`）。
2. **values**：用 critic 给每个 response token 估一个价值 V(s)（仅 `gae`，GRPO 跳过）。
3. **reward**：调 `reward_fn` 给每条回答打分（rule-based 或叠加 model-based），分数挂在回答末位 token。
4. **KL penalty → advantage**：从 score 扣掉 KL 惩罚得到 `token_level_rewards`，再算 GAE/GRPO 优势。

#### 4.4.2 核心流程

```
if use_reference_policy:                          # ref
    ref_log_prob = ref_policy_wg.compute_ref_log_prob(batch)
    batch.union(ref_log_prob)
if use_critic:                                    # values
    values = critic_wg.compute_values(batch)
    batch.union(values)
# —— adv（driver 侧）——
if use_rm:                                        # 可选 model-based
    reward_tensor = rm_wg.compute_rm_score(batch)
    batch.union(reward_tensor)
reward_tensor = reward_fn(batch)                  # rule-based 规则奖励（TinyZero 走这条）
batch['token_level_scores'] = reward_tensor
if not use_kl_loss:
    batch = apply_kl_penalty(batch, kl_ctrl)      # token_level_rewards = scores - beta*KL
else:
    batch['token_level_rewards'] = scores         # KL 放进 actor loss 里，不在这里扣
batch = compute_advantage(batch, adv_estimator)   # gae 或 grpo
```

`reward_fn` 的双重身份（u4-l1 讲过）：若 batch 里已有 `rm_scores`（model-based 已算）则直接返回，否则逐条解码、按 `data_source` 路由规则函数打分。

`apply_kl_penalty` 的核心公式（token 级）：

\[
r_t = s_t - \beta \cdot \mathrm{KL}_t
\]

其中 \(\mathrm{KL}_t\) 是 `old_log_probs` 与 `ref_log_prob` 之间的 token 级 KL（详见 u5-l1），按序列用 `response_mask` 求平均得到 `current_kl`，再喂给 KL 控制器自适应调 \(\beta\)。

#### 4.4.3 源码精读

ref 与 values 阶段：

[verl/trainer/ppo/ray_trainer.py:605-615](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L605-L615) —— 条件调用 `ref_policy_wg` / `critic_wg` 并 union 结果。

adv 阶段（reward + KL + advantage）：

[verl/trainer/ppo/ray_trainer.py:617-644](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L617-L644) —— 先可选 model-based，再 rule-based `reward_fn`，再 `apply_kl_penalty`（或直接复制 scores），最后 `compute_advantage`。

`apply_kl_penalty`：

[verl/trainer/ppo/ray_trainer.py:84-113](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L84-L113) —— 计算 `kld`、`token_level_rewards = token_level_scores - beta*kld`、序列平均 `current_kl` 并更新 `kl_ctrl`。

`compute_advantage`（gae/grpo 分支）：

[verl/trainer/ppo/ray_trainer.py:116-147](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L116-L147) —— `gae` 用 `values` 走 `compute_gae_advantage_return`；`grpo` 用 `uid` 分组走 `compute_grpo_outcome_advantage`。

KL 扣减的关键片段：

```python
token_level_rewards = token_level_scores - beta * kld
```

（[ray_trainer.py:102](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L102)）。若 `use_kl_loss=True`（GRPO 常用），则跳过此扣减，KL 直接进 actor loss（见 [631-637](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L631-L637) 的 else 分支）。

#### 4.4.4 代码实践

**目标**：跟踪一条数据从「生成分数」到「token_level_rewards」的变换。

**步骤**：

1. 在 [`RewardManager.__call__`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L45-L90) 找到 `reward_tensor[i, valid_response_length - 1] = score`，确认分数只挂在末位 token。
2. 跟到 [`apply_kl_penalty:102`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L102)，写出：若某条回答 `score=1.0`、`beta=0.001`、末位 token 的 `KL=2.0`，则该 token 的 `reward = 1.0 - 0.001×2.0 = 0.998`，其余 token 的 reward 来自 scores（0）减 `beta×KL`。
3. 说明 `use_kl_loss=True` 时为何走 else 分支（reward = scores，KL 留给 actor loss）。

**需要观察的现象**：除末位外，scores 几乎全 0（稀疏奖励），KL 项在每个有效 token 上都有值。

**预期结果**：理解「稀疏 score + 稠密 KL 惩罚」如何组合成 `token_level_rewards`。

#### 4.4.5 小练习与答案

**Q1**：`reward_fn` 在什么情况下会直接返回而不调用规则函数？

**A**：当 batch 里存在 `rm_scores` 字段（model-based RM 已算过）时，`RewardManager` 直接返回它（[main_ppo.py:49-50](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L49-L50)），短路掉 rule-based。

**Q2**：gae 和 grpo 在 `compute_advantage` 里对 critic 的依赖有何不同？

**A**：gae 分支需要 `values`（critic 前向结果）算 GAE；grpo 分支不用 values，改用 `uid` 分组的组内归一化，所以 `use_critic=False`（u4-l2）。

---

### 4.5 反向更新、评估存盘与计时打点：compute_timing_metrics（核心模块）

#### 4.5.1 概念说明

拿到 advantage 后，进入真正的梯度更新：

- **update_critic**：用 `returns` 拟合价值网络（仅 `gae`）。
- **update_actor**：用 advantage 更新策略。注意有一道「critic warmup」门控——前 `critic_warmup` 步只更新 critic、冻住 actor，让价值网络先学几步再开始动策略，稳定早期训练。
- **testing**：定期（`test_freq`）在验证集上贪心生成并打分。
- **save_checkpoint**：定期（`save_freq`）存 actor/critic 权重。
- **计时**：`_timer` 给每段计时，`compute_timing_metrics` 把绝对秒数折算成「每 token 毫秒」，便于跨配置比较吞吐。

#### 4.5.2 核心流程

```
if use_critic:                          # update_critic
    critic_output = critic_wg.update_critic(batch)
    metrics.update(reduce(critic_output.metrics))
if global_steps > critic_warmup:        # critic 预热门控
    actor_output = actor_rollout_wg.update_actor(batch)   # update_actor
    metrics.update(reduce(actor_output.metrics))
if test_freq 命中:                      # testing
    val_metrics = self._validate()
if save_freq 命中:                      # save_checkpoint
    self._save_checkpoint()
# —— 末尾 ——
metrics.update(compute_data_metrics(...))     # 数据侧指标
metrics.update(compute_timing_metrics(...))   # 计时指标
logger.log(metrics, step)
```

`_validate` 内部：对验证集每条 prompt 贪心生成（`do_sample=False`）、调 `val_reward_fn` 打分、按 `data_source` 汇总 mean reward。验证集 batch 可能不被 `world_size` 整除，故用 `pad_dataproto_to_divisor` 补齐再 `unpad`。

`compute_timing_metrics` 的折算逻辑：每类阶段对应一个 token 计数（`gen` 用 response token 数，`ref`/`values`/`adv`/`update_*` 用 prompt + response 总 token 数），把 `timing_raw[name]`（秒）换算成 ms/token：

\[
\text{ms/token} = \frac{\text{timing\_raw}[name] \times 1000}{\text{num\_tokens\_of\_section}[name]}
\]

`_timer` 是个 contextmanager，用 `codetiming.Timer` 测量进入/退出区间，把 `timer.last`（秒）写进 `timing_raw`。

#### 4.5.3 源码精读

update_critic / critic_warmup 门控 / update_actor：

[verl/trainer/ppo/ray_trainer.py:646-659](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L646-L659) —— 注意 `critic_warmup <= global_steps` 才更新 actor，这是预热条件。

testing / save_checkpoint：

[verl/trainer/ppo/ray_trainer.py:662-671](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L662-L671) —— 按 `test_freq` / `save_freq` 周期触发。

`_validate`（验证集生成 + 打分）：

[verl/trainer/ppo/ray_trainer.py:392-442](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L392-L442) —— 关键点：`do_sample=False`（贪心）、`validate=True`、pad/unpad、`val_reward_fn(test_batch)`、按 `data_source` 汇总 `val/test_score/{data_source}`。

`_save_checkpoint`：

[verl/trainer/ppo/ray_trainer.py:516-528](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L516-L528) —— 存 actor（`actor_rollout_wg`）与 critic（`critic_wg`）到 `default_local_dir`。

`_timer`：

[verl/trainer/ppo/ray_trainer.py:284-288](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L284-L288) —— contextmanager + `codetiming.Timer`，`timing_raw[name] = timer.last`。

`compute_timing_metrics`：

[verl/trainer/ppo/ray_trainer.py:260-281](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L260-L281) —— 定义 `num_tokens_of_section` 映射，输出 `timing_s/{name}`（秒）与 `timing_per_token_ms/{name}`（ms/token）。

每步末尾指标汇总：

[verl/trainer/ppo/ray_trainer.py:673-678](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L673-L678) —— `compute_data_metrics` + `compute_timing_metrics` + `logger.log`。

**worker group 归属速查**（对应实践任务）：

| 阶段（_timer 名） | 调用的 worker group | 干什么 |
|---|---|---|
| `gen` | `actor_rollout_wg` | rollout 引擎（vLLM）生成回答 |
| `ref` | `ref_policy_wg` | 参考策略算 log_prob |
| `values` | `critic_wg` | critic 算价值 |
| `adv` | driver 本地（+ 可选 `rm_wg`） | `reward_fn` / `apply_kl_penalty` / `compute_advantage`；可选 `rm_wg.compute_rm_score` |
| `update_critic` | `critic_wg` | 更新价值网络 |
| `update_actor` | `actor_rollout_wg` | 更新策略（actor） |
| `testing` | `actor_rollout_wg` + driver | `_validate` 贪心生成 + `val_reward_fn` |
| `save_checkpoint` | `actor_rollout_wg` + `critic_wg` | 存权重 |

#### 4.5.4 代码实践（本讲主实践）

**目标**：把 fit 一个 step 的阶段画成时序流程图，标注每个阶段调的 worker group，并说清 `reward_fn` 与 `val_reward_fn` 的区别。

**步骤**：

1. 读 [`fit:586-680`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L586-L680)，把 8 个 `_timer` 区间（`gen`/`ref`/`values`/`adv`/`update_critic`/`update_actor`/`testing`/`save_checkpoint`）按时间轴自上而下排列。
2. 在每个阶段右侧标注它调用的 worker group（用上面的速查表）。
3. 标出哪些阶段是 driver 本地计算（`adv` 里的 `reward_fn`/`apply_kl_penalty`/`compute_advantage`）。
4. 对比 [`reward_fn` vs `val_reward_fn` 的构造](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L174-L177)，写出两者区别。

**reward_fn 与 val_reward_fn 的区别**：

- 两者都是同一个 `RewardManager` 类，唯一构造差异是 `num_examine`：`reward_fn=0`（训练时不打印样本），`val_reward_fn=1`（验证时每种 `data_source` 打印 1 条解码样本到控制台供人工观察）。
- 使用位置不同：`reward_fn` 在 fit 训练步的 adv 阶段用；`val_reward_fn` 在 `_validate`（训练前 / 周期性 / 训练后）用。
- 生成方式不同：训练用采样（`do_sample` 由 config 决定），验证用贪心（`do_sample=False`）。
- 注释强调「validation always uses function-based RM」：验证永远走规则奖励，即使开了 `reward_model`；且若验证样本是 model 风格，`_validate` 直接返回空（[392-401](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L400-L401)）。

**预期结果**：画出一张「step 时序图」，能一眼看出每段在哪个 worker group 上跑、driver 做了哪些本地轻量计算。

#### 4.5.5 小练习与答案

**Q1**：`critic_warmup` 起什么作用？

**A**：前 `critic_warmup` 步只更新 critic、跳过 `update_actor`（[654](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L654)），让价值网络先学到合理估值再开始动策略，避免早期 advantage 估计太差导致策略崩溃。

**Q2**：为什么 `compute_timing_metrics` 要把秒折算成 ms/token？

**A**：绝对秒数受 batch 大小、序列长度影响，难以跨配置比较；折算成「每 token 毫秒」后能直接比较不同配置的吞吐效率，定位哪个阶段是瓶颈。

---

## 5. 综合实践

**任务：画一张完整的「一个 PPO step 的数据流时序图」并自检。**

1. 画出 driver 进程上 batch 的字段演化时间线，标注每个节点 batch 里新增/变化了哪些字段：
   - 初始：`input_ids`/`attention_mask`/`position_ids` + `non_tensor_batch`(`data_source`/`ground_truth`)
   - 赋 uid + repeat 后：batch_size × n，`uid` 被复制
   - gen 后 union：+ `prompts`/`responses`/`old_log_probs`
   - ref 后：+ `ref_log_prob`
   - values 后：+ `values`
   - adv 后：+ `token_level_scores` / `token_level_rewards` / `advantages` / `returns`
   - `update_critic`/`update_actor`：消费上述字段产出 metrics
2. 在时间线左侧标 worker group（`actor_rollout_wg` / `ref_policy_wg` / `critic_wg` / `rm_wg` / driver）。
3. 标出 `with _timer` 的嵌套结构（`step` 包住所有，内部各阶段各一个）。
4. 自检：把 GRPO 场景（`use_critic=False`、`use_kl_loss=True`）画一遍，对比哪些阶段消失（`values`、`update_critic`）、KL 处理如何变化（走 actor loss 而非 `apply_kl_penalty`）。

完成这张图后，你就能在阅读任何 PPO/GRPO 训练日志时，把 `timing_s/gen`、`critic/score/mean`、`response_length/mean` 等指标对号入座到 fit 的具体阶段。

## 6. 本讲小结

- `fit()` 是 driver 进程的编排循环：自己不算梯度，只调 worker group 方法 + 本地轻量计算（奖励/KL/优势/指标）。
- 一个 step = `generate → repeat(n)/union → _balance_batch → ref → values → reward → KL penalty → advantage → update_critic → update_actor → validate/checkpoint`。
- `repeat(n, interleave=True)` 把 prompt 侧复制 n 份以对齐 rollout 的 n 次采样；`uid` 在 repeat 前赋值供 GRPO 分组。
- `_balance_batch` 用 Karmarkar-Karp 在分片前重排 batch，让各 DP rank 的有效 token 总数均衡，消除木桶效应；`global_token_num` 写进 `meta_info` 供下游动态切分。
- 奖励链路：稀疏 score（末位 token）+ KL 惩罚 = `token_level_rewards`，再经 gae/grpo 算 advantage。
- `reward_fn`（训练、`num_examine=0`、采样）与 `val_reward_fn`（验证、`num_examine=1`、贪心、永远规则奖励）是同一个 `RewardManager` 的两种用法。
- `_timer` + `compute_timing_metrics` 把每段耗时折算成 ms/token，用于定位瓶颈。

## 7. 下一步学习建议

- 下一讲 u4-l4 会更细地拆 `RewardManager` 与 reward 如何被组合（model-based vs rule-based 的优先级、分数如何挂到末位 token）。
- 之后进入 u5 单元深入 core_algos 的数学：u5-l1（KL 惩罚与优势）、u5-l2（PPO 策略损失与 clip）、u5-l3（GAE 与价值损失）。
- 想理解 `_balance_batch` 背后的 Karmarkar-Karp 细节，可先读 [seqlen_balancing.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py) 的 `karmarkar_karp`，对应 u7-l2 的序列长度均衡主题。
- 建议结合 u7-l5 的 `tests/e2e` 最小训练，实际跑一个 step，对照本讲的时序图观察日志输出。
