# 奖励计算与 RewardManager

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚在 `fit()` 训练主循环里，一个 batch 的「分数」是从哪里来的、经过哪两条来路（model-based 与 rule-based）。
- 解释为什么当 `rm_scores` 存在时，`RewardManager` 会直接短路返回，而不再调用规则奖励函数；以及这种「优先级」和代码注释里「combine」一词之间的微妙出入。
- 读懂 `RewardManager.__call__` 如何用 `attention_mask` 切开 prompt / response、解码出可读文本、按 `data_source` 路由到对应的 `compute_score`。
- 看清一个标量分数（0 / 0.1 / 1.0）是如何被「放到回答最后一个有效 token」上，变成一个和 `responses` 同形状的稀疏张量 `reward_tensor`。
- 区分训练阶段 `reward_fn` 与验证阶段 `val_reward_fn` 的差别。

本讲承接 u2-l4（规则奖励函数的内部逻辑）与 u4-l1（`main_ppo` 装配出 `RewardManager`），把视角从「单个奖励函数怎么算分」抬升到「整个训练循环怎么把分数组装起来」。

## 2. 前置知识

- **稀疏奖励（sparse reward）**：模型生成一整段几百上千 token 的回答，但真正能用来判分的信号往往只在最后（答案对不对）。所以奖励张量绝大多数位置是 0，只有少数位置（通常是最后一个有效 token）是非零的分数。这种「信号很稀」的形式就是稀疏奖励。
- **token 级分数 vs 标量分数**：规则函数 `compute_score` 返回的是一个标量（如 `1.0`），但 PPO 算法在序列上工作，需要每个 token 都有一个分数。于是训练系统要把这个标量「铺」到 token 维度上——本讲的核心就是把标量放到末位 token，其余位置补 0。
- **prompt / response 的拼接与填充**：回顾 u2-l3，prompt 采用**左填充**（pad 在左、真实内容在右），生成完成后 response 采用**右填充**（真实 token 在左、pad 在右）。两者拼成完整序列。`attention_mask` 是用来区分「真实 token（1）」与「pad（0）」的关键。
- **`data_source` 奖励路由**：回顾 u4-l1，每条样本都带一个 `data_source` 字符串（如 `countdown`），`_select_rm_score_fn` 据此选用对应的 `compute_score`。
- **DataProto**：回顾 u3-l1，数据协议分 `batch`（张量列）与 `non_tensor_batch`（非张量列，如 `data_source`、`reward_model`）。本讲里 `RewardManager` 会逐条从 `non_tensor_batch` 取出任务类型与正确答案。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `verl/trainer/main_ppo.py` | 定义 `RewardManager`（可调用对象）与 `_select_rm_score_fn`（按 `data_source` 路由奖励函数）。 |
| `verl/trainer/ppo/ray_trainer.py` | `fit()` 里调用 `reward_fn` 组装 `token_level_scores`；`_validate()` 里调用 `val_reward_fn` 做验证打分。 |
| `verl/utils/reward_score/countdown.py` | countdown 任务的规则奖励函数 `compute_score`，返回标量 0 / 0.1 / 1.0。 |
| `verl/workers/fsdp_workers.py` | `RewardModelWorker.compute_rm_score` 与 `_expand_to_token_level`，演示 model-based 路径如何把标量铺到 token 级（与本讲的末位放置是同一思想）。 |

## 4. 核心概念与源码讲解

### 4.1 奖励的两条来路与优先级

#### 4.1.1 概念说明

在 `fit()` 单步训练里，「给这一批生成回答打分」这件事发生在 `adv`（advantage）计时块内。分数有两条来路：

1. **model-based（模型奖励）**：用一个神经网络奖励模型（RewardModelWorker）对生成回答前向打分。输出键名为 `rm_scores`，且**已经是 token 级张量**。
2. **rule-based（规则奖励）**：用确定性 Python 函数（如 `countdown.compute_score`）判分。输出一个标量，再由 `RewardManager` 放到末位 token。

TinyZero 默认关闭 model-based（`config.reward_model.enable=False`），所以你跑 countdown 时走的是**纯 rule-based**。但代码同时支持两条路并存，理解优先级很重要。

#### 4.1.2 核心流程

`fit()` 里相关片段（精简后）：

```
with _timer('adv', timing_raw):
    if self.use_rm:                                   # ① 若启用模型奖励
        reward_tensor = self.rm_wg.compute_rm_score(batch)
        batch = batch.union(reward_tensor)            #    把 rm_scores 并进 batch

    reward_tensor = self.reward_fn(batch)             # ② 统一入口：调用 RewardManager
    batch.batch['token_level_scores'] = reward_tensor # ③ 记为 token 级分数

    # 之后：apply_kl_penalty 得 token_level_rewards；compute_advantage 得 advantage
```

`reward_fn` 就是 `RewardManager` 实例。它内部第一件事是检查 batch 里有没有 `rm_scores`：

```python
# 如果存在 rm_scores（model-based），直接返回；否则才走规则函数
if 'rm_scores' in data.batch.keys():
    return data.batch['rm_scores']
```

这就是「优先级」：**只要 model-based 的 `rm_scores` 在，`RewardManager` 就原样返回它，完全跳过规则函数**。

> ⚠️ 一个值得留意的出入：`fit()` 上方的注释写的是「先算 model 分数，再调 reward_fn **combine** 两者」，但 `RewardManager.__call__` 的实现其实是**短路（short-circuit）而非合并**——有 `rm_scores` 就直接 return，规则分根本没参与。也就是说当前代码下「同时启用 model RM 与规则函数」时，规则函数的分数会被丢弃。注释描述的是一种设计意图，实现尚未做到真正的加权合并。读源码时要相信代码、对注释保持警惕。

#### 4.1.3 源码精读

`fit()` 中组装分数的核心几行：

[verl/trainer/ppo/ray_trainer.py:617-628](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L617-L628) — 先可选地算 model-based 分并 `union` 进 batch，再统一调用 `self.reward_fn(batch)` 得到 `reward_tensor`，赋给 `token_level_scores`。

`use_rm` 的来源在构造函数里：

[verl/trainer/ppo/ray_trainer.py:322-323](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L322-L323) — `self.use_rm = Role.RewardModel in role_worker_mapping`。而 `main_ppo` 只有在 `config.reward_model.enable` 为真时才把 `Role.RewardModel` 注册进 `role_worker_mapping`，所以 TinyZero 默认 `use_rm=False`。

短路返回那一行：

[verl/trainer/main_ppo.py:49-50](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L49-L50) — `'rm_scores' in data.batch.keys(): return data.batch['rm_scores']`，这就是 model-based 优先于 rule-based 的实现。

model-based 路径的产出（与本讲主题呼应）：

[verl/workers/fsdp_workers.py:1016-1018](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L1016-L1018) — `_expand_to_token_level` 把标量分数铺到 token 级后，包装成 `{'rm_scores': token_level_scores}`。它的铺法（见 4.2.3）与规则路径**完全一致**：都把分数放在最后一个有效 token。

#### 4.1.4 代码实践

**实践目标**：在不实际跑训练的前提下，通过读源码确认「TinyZero 默认走哪条路」。

**操作步骤**：

1. 打开 `verl/trainer/main_ppo.py`，找到 [RewardManager 实例化处](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L174-L177)，确认 `reward_fn` 与 `val_reward_fn` 都是 `RewardManager`。
2. 打开 `ppo_trainer.yaml`（u1-l4 讲过），找到 `reward_model.enable` 的默认值。
3. 追踪：`reward_model.enable=False` → `main_ppo` 不注册 `Role.RewardModel` → `RayPPOTrainer.__init__` 里 `use_rm=False` → `fit()` 里 `if self.use_rm` 分支不进入 → batch 里永远没有 `rm_scores` → `RewardManager.__call__` 永远走到规则函数分支。

**需要观察的现象 / 预期结果**：你会确认 TinyZero 训练时 `token_level_scores` 完全由规则函数产生；model-based 那条路虽存在于代码中，但在默认配置下是「死代码」。把这条推理链用一两句话写下来。

> 待本地验证：若你本地把 `reward_model.enable=True` 并提供一个 RM checkpoint，再观察训练日志，应能看到 `rm_scores` 生效、规则函数被短路（这是推断，未在本地实跑）。

#### 4.1.5 小练习与答案

**练习 1**：如果同时启用了 model-based RM 与规则函数，最终 `token_level_scores` 取决于谁？为什么？

**参考答案**：取决于 model-based。因为 `RewardManager.__call__` 第一行就检查 `'rm_scores' in data.batch.keys()`，存在则直接返回，规则函数不会被调用。

**练习 2**：`fit()` 注释说「combine」，实际代码是 combine 吗？

**参考答案**：不是。当前实现是短路（short-circuit）而非加权合并；注释反映的是设计意图，与实现不符。这是一个真实的源码-注释出入。

---

### 4.2 RewardManager.__call__：解码与「末位 token」放置

#### 4.2.1 概念说明

`RewardManager.__call__` 是整个奖励系统的「装配车间」：它接收一个 `DataProto`（里面已有生成好的 `prompts` / `responses`），逐条做四件事——

1. 用 `attention_mask` 把左填充的 prompt 和右填充的 response 各自还原成「真实 token」。
2. 把 prompt+response 解码成可读字符串。
3. 按 `data_source` 路由到对应的 `compute_score`，得到一个标量分数。
4. 把这个标量放到 `reward_tensor[i, valid_response_length - 1]`——也就是该条回答的**最后一个有效 response token** 上。

这是本讲最关键的一段代码，也是练习题针对的目标。

#### 4.2.2 核心流程

对每一条样本 `i`：

```
prompt_ids        = batch['prompts']                    # 左填充
response_ids      = batch['responses']                  # 右填充
whole_attn        = batch['attention_mask']             # 全序列 mask

prompt_length     = prompt_ids.shape[-1]                # prompt 段长度
valid_prompt_len  = whole_attn[:prompt_length].sum()    # prompt 段里真实 token 数
valid_prompt_ids  = prompt_ids[-valid_prompt_len:]      # 砍掉左侧 pad

valid_resp_len    = whole_attn[prompt_length:].sum()    # response 段里真实 token 数
valid_resp_ids    = response_ids[:valid_resp_len]       # 砍掉右侧 pad

sequences_str     = decode(cat(valid_prompt_ids, valid_resp_ids))
score             = compute_score_fn(sequences_str, ground_truth)

reward_tensor[i, valid_resp_len - 1] = score            # ★ 放在最后一个有效 token
```

为什么是「最后一个有效 token」？直觉有三层：

- **稀疏性**：答案对错只有在回答结束时才知道，所以信号天然落在末尾。
- **形状对齐**：`reward_tensor` 必须和 `responses` 同形状（`[batch, response_length]`），才能和 log-prob、value 等逐 token 对齐做后续 PPO 计算。
- **可反传**：后续 `compute_advantage`（GAE 反向递推）会把这个末位信号沿时间步往回传播，给每个 token 分配优势值。所以放在末位 = 放在因果链的「出口」，便于信用分配（credit assignment）。

用公式表达这条放置规则（设该样本真实 response 长度为 \(L\)，分数为 \(s\)）：

\[
\text{reward\_tensor}[i,\; t] =
\begin{cases}
s, & t = L - 1 \\
0, & \text{其他}
\end{cases}
\]

`num_examine` 控制的是另一件事：**每种 `data_source` 各打印多少条解码结果到控制台**，用于训练时人眼检查模型生成质量。它与打分无关，纯粹是调试观测。详见 4.2.4。

#### 4.2.3 源码精读

整段 `__call__`：

[verl/trainer/main_ppo.py:45-90](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L45-L90) — 短路判断 → 初始化全零 `reward_tensor` → 逐条解码打分 → 末位放置 → 限量打印。

几个关键点逐行说明：

[verl/trainer/main_ppo.py:52](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L52) — `reward_tensor = torch.zeros_like(data.batch['responses'], ...)`：奖励张量形状与 `responses` 完全一致，初始全零，对应稀疏奖励。

[verl/trainer/main_ppo.py:63-68](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L63-L68) — 用 `attention_mask` 切出 prompt 真实段（`[-valid_prompt_length:]` 砍左 pad）与 response 真实段（`[:valid_response_length]` 砍右 pad）。

[verl/trainer/main_ppo.py:74-80](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L74-L80) — 从 `non_tensor_batch` 取 `ground_truth` 与 `data_source`，经 `_select_rm_score_fn` 路由后调用 `compute_score_fn`，得到标量 `score`。

[verl/trainer/main_ppo.py:81](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L81) — `reward_tensor[i, valid_response_length - 1] = score`：★ 本讲的题眼——把分数放在最后一个有效 response token。

> 对照 model-based 路径，`_expand_to_token_level` 用 `argmax(position_ids * attention_mask)` 定位「最后一个有效 token」后放分数，思想与此完全一致：
> [verl/workers/fsdp_workers.py:911-924](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L911-L924) — model-based 把标量分数铺到 token 级，同样落在末位。可见「末位放置」是两条来路共享的不变量。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：亲手验证「为什么用 `attention_mask` 算 `valid_response_length`，以及为什么放在 `valid_response_length - 1`」，并搞清 `num_examine` 的作用。

**操作步骤**：

1. 打开 [verl/trainer/main_ppo.py:56-81](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L56-L81)，准备一张示意图（纸笔即可）。假设某条样本：prompt 段长 6（其中左侧 2 个 pad），response 段长 5（其中右侧 3 个 pad，真实 token 占 2 位）。
2. 写出整条 `attention_mask`：prompt 段 `[0,0,1,1,1,1]`，response 段 `[1,1,0,0,0]`，拼起来 `[0,0,1,1,1,1,1,1,0,0,0]`。
3. 计算：`prompt_length = 6`；`valid_prompt_length = attn[:6].sum() = 4`；`valid_response_length = attn[6:].sum() = 2`。
4. 那么 `reward_tensor[i, valid_response_length - 1] = reward_tensor[i, 1]`，即 response 段索引 1（第 2 个真实 token，也就是最后一个有效 token）被赋分。

**需要观察的现象 / 预期结果**：

- 分数落在 response 段的**最后一个真实 token**，而不是整个 response 段的最后一个位置（那个位置是 pad，放进去会被 mask 掉、信号丢失）。
- 这正解释了「为什么必须用 `attention_mask` 算长度」：因为 response 是右填充，序列末尾是 pad，直接放 `[-1]` 会把分数放到 pad 上、被 `attention_mask` 清零，奖励信号就丢了。
- `num_examine` 的作用：控制每种 `data_source` 在控制台打印几条解码样本。在 `main_ppo` 里训练用 `num_examine=0`（不打），验证用 `num_examine=1`（每种任务打 1 条供人眼检查）。见 [main_ppo.py:86-88](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L86-L88) 的 `already_print_data_sources` 计数逻辑。

5.（可选，本地有 GPU 时）写一个 5 行的最小脚本：手工构造一个 `DataProto`（`prompts`/`responses`/`attention_mask` + `non_tensor_batch` 里放 `data_source='countdown'` 和一个 `ground_truth`），调用 `RewardManager(tokenizer, num_examine=1)(data)`，打印返回的 `reward_tensor`，确认非零元素确实位于各样本的 `valid_response_length - 1`。

> 待本地验证：步骤 5 的运行结果取决于你是否能搭起最小 verl 环境；若不具备，完成步骤 1–4 的纸笔推导即可达到本实践目标。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `reward_tensor[i, valid_response_length - 1] = score` 误写成 `reward_tensor[i, -1] = score`，会发生什么？

**参考答案**：分数会被放到 response 段的最末位，而该位置是 pad token（右填充）。后续计算（如 `apply_kl_penalty`、advantage）会在 `attention_mask` 上做 `masked` 操作，pad 位的分数会被忽略/清零，导致奖励信号丢失，模型学不到东西。

**练习 2**：`valid_prompt_length` 在打分逻辑里其实没有参与赋分（赋分只用了 `valid_response_length`），那它存在的意义是什么？

**参考答案**：它用于**解码**——把左填充的 prompt 砍掉 pad 后，与真实 response 拼成一个干净的字符串再 `tokenizer.decode`，这样 `compute_score` 拿到的是可读的自然语言（含 `<answer>` 等标签），而不是带 pad 的乱码。它服务的是「正确解码」，不是「赋分位置」。

**练习 3**：`num_examine=0` 和 `num_examine=1` 对训练的数值结果有影响吗？

**参考答案**：没有。`num_examine` 只控制 `print`（调试输出），不进入 `reward_tensor`，因此对梯度、loss、任何数值都无影响。它纯粹是人眼观测模型生成质量的开关。

---

### 4.3 compute_score：标量分数的产出契约

#### 4.3.1 概念说明

`RewardManager` 只负责「解码 + 路由 + 末位放置」，真正的判分逻辑在每个任务的 `compute_score` 里。u2-l4 已经深入讲过 countdown 的「提取 → 校验 → 求值 → 分级打分」四步，本讲只强调它对上层（`RewardManager`）的**契约**：输入一个解码字符串和 `ground_truth`，输出一个**标量**（0 / 0.1 / 1.0）。`RewardManager` 不关心你内部怎么算，只要返回一个浮点数。

#### 4.3.2 核心流程

countdown 的分级（回顾）：

| 情况 | 返回值 | 含义 |
| --- | --- | --- |
| 提取不到 `<answer>` 等式 | `0` | 完全无格式，零分 |
| 等式用到题目没给的数字（偷数字） | `format_score = 0.1` | 格式对但违规，给小分作塑形 |
| 等式合法但结果 ≠ target | `format_score = 0.1` | 格式对但算错，给小分 |
| 结果与 target 误差 < 1e-5 | `score = 1.0` | 完全正确 |

这三档分数正是 u2-l4 提到的「奖励塑形（reward shaping）」：用 0.1 的 `format_score` 给「至少格式对」的模型一点起步信号，避免初期全是 0 导致梯度消失、训不动。但 0.1 不能太高，否则模型会「只刷格式、不求解」（reward hacking）。

#### 4.3.3 源码精读

[verl/utils/reward_score/countdown.py:59-111](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L59-L111) — `compute_score` 的完整分级打分逻辑。

两处关键返回点：

[verl/utils/reward_score/countdown.py:81-84](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L81-L84) — 无等式返回 `0`（注意是整型 0，会被赋给 float 张量，无碍）。

[verl/utils/reward_score/countdown.py:100-107](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L100-L107) — 结果正确返回 `score=1.0`，否则返回 `format_score=0.1`。

此外注意 [countdown.py:73](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/countdown.py#L73) 的 `do_print = random.randint(1, 64) == 1`：以 1/64 概率随机抽样打印一条样本到控制台。这是除 `num_examine` 之外**另一处**调试打印入口——它在 `compute_score` 内部、按概率抽样；而 `num_examine` 在 `RewardManager` 里、按 `data_source` 配额。两者互补，共同构成训练时的样本观测手段。

> 对照 multiply 任务的 `compute_score`：[verl/utils/reward_score/multiply.py:27-58](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/reward_score/multiply.py#L27-L58) — 契约完全一致（返回标量），只是内部只比较 `<answer>` 内整数是否等于 `ground_truth`，不校验运算过程。可见「返回标量」是 `RewardManager` 对所有规则函数的统一要求。

#### 4.3.4 代码实践

**实践目标**：把 `compute_score` 的三档输出与 `RewardManager` 的末位放置串起来，验证端到端契约。

**操作步骤**：

1. 准备 3 条「解码字符串」输入（示例代码，非项目原有；可直接在 Python 里调 `compute_score`）：

```python
# 示例代码：手动验证 countdown.compute_score 的三档输出
from verl.utils.reward_score.countdown import compute_score

gt = {"target": 24, "numbers": [4, 6]}

# ① 完全正确：4*6=24
s1 = "Assistant:\n<think>4 times 6</think><answer>4*6</answer>"
# ② 格式对但结果错：4+6=10 ≠ 24
s2 = "Assistant:\n<think>4 plus 6</think><answer>4+6</answer>"
# ③ 无 <answer> 标签
s3 = "Assistant:\nI think the answer is 24."

print(compute_score(s1, gt))  # 预期 1.0
print(compute_score(s2, gt))  # 预期 0.1
print(compute_score(s3, gt))  # 预期 0
```

2. 想象这 3 个标量被 `RewardManager` 分别放到 3 条样本的 `valid_response_length - 1` 位置，其余位置为 0。

**需要观察的现象 / 预期结果**：① 得 `1.0`、② 得 `0.1`、③ 得 `0`。这印证了「规则函数返回标量 → RewardManager 末位放置」的契约。

> 待本地验证：若不装 verl，可直接把 `countdown.py` 的 `compute_score` 函数体复制到一个独立 `.py` 文件里单独运行（它只依赖 `re`、`random`、标准库），即可复现上述结果。

#### 4.3.5 小练习与答案

**练习 1**：`compute_score` 返回 `0`（整型）而 `reward_tensor` 是 `float32`，会有问题吗？

**参考答案**：不会。Python 整型赋值给 `torch.zeros(..., dtype=torch.float32)` 的元素时，PyTorch 会自动转成 `0.0`。

**练习 2**：为什么 countdown 给「偷数字」也只返回 `format_score` 而不是 `0`？

**参考答案**：因为格式本身（有合法 `<answer>` 等式）是对的，给小分作为塑形信号，帮助训练初期梯度不至全零；但通过 `validate_equation` 拦截了「数字不对」的情况，确保只有真正用了题目给定数字且结果正确才能拿满分 1.0，避免奖励黑客。

---

### 4.4 _validate：验证阶段的奖励路径

#### 4.4.1 概念说明

训练用 `reward_fn`，验证用 `val_reward_fn`。两者**都是 `RewardManager`**，区别仅在构造参数 `num_examine`（训练=0，验证=1）以及调用上下文。验证阶段还有一个关键设定：**只走 rule-based**，并且用**贪心解码**。

#### 4.4.2 核心流程

`_validate` 对验证集每个 batch：

1. 跳过 model-based 风格的样本（验证只用规则奖励）。
2. 设置 `meta_info`：`do_sample=False`（贪心，不采样）、`validate=True`、`recompute_log_prob=False`。
3. 用 `actor_rollout_wg.generate_sequences` 生成（贪心），pad/unpad 对齐 dp_size。
4. `test_batch.union(test_output_gen_batch)` 把生成结果并回。
5. 调 `self.val_reward_fn(test_batch)` 得到 `reward_tensor`。
6. 把 token 级 `reward_tensor` 沿最后一维 `.sum(-1)` 压成每条样本一个标量（因为只有末位有非零分数，求和 = 取出那个分数）。
7. 按 `data_source` 分桶求均值，得到 `val/test_score/<data_source>` 指标。

#### 4.4.3 源码精读

[verl/trainer/ppo/ray_trainer.py:392-442](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L392-L442) — `_validate` 全流程。

几个关键点：

[verl/trainer/ppo/ray_trainer.py:399-401](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L399-L401) — 注释明说「we only do validation on rule-based rm」：即便启用了 model RM，验证也只用规则函数（且当样本 `style=='model'` 时直接返回空字典）。

[verl/trainer/ppo/ray_trainer.py:404-410](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L404-L410) — `do_sample=False` 即贪心解码，保证验证结果可复现、可对比。

[verl/trainer/ppo/ray_trainer.py:423](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L423) — `reward_tensor = self.val_reward_fn(test_batch)`，与训练同款 `RewardManager`，只是 `num_examine=1` 会多打几条样本。

[verl/trainer/ppo/ray_trainer.py:428](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L428) — `torch.cat(...).sum(-1)` 把 token 级压成样本级。这一步能成立，**恰恰因为** `RewardManager` 把分数放在唯一的末位 token、其余为 0，所以沿序列求和等价于「取出那个分数」。

#### 4.4.4 代码实践

**实践目标**：理解「token 级 → 样本级」的压缩为什么用 `.sum(-1)`。

**操作步骤**：

1. 假设某条样本 `reward_tensor[i] = [0, 0, 0, 1.0, 0]`（真实长度 4，分数放在索引 3，末位是 pad 为 0）。
2. 手算 `.sum(-1)`：`0+0+0+1.0+0 = 1.0`。

**需要观察的现象 / 预期结果**：求和结果就是该样本的分数。这反过来印证了 4.2 的放置规则——如果分数被错误地放到多个 token 或 pad 位，`.sum(-1)` 就不再是「该样本的真实分数」，`val/test_score` 指标会失真。

> 待本地验证：若本地能跑一次验证，观察 wandb/console 里 `val/test_score/countdown` 是否在 0~1 之间、随训练上升。

#### 4.4.5 小练习与答案

**练习 1**：为什么验证用贪心（`do_sample=False`）而训练用采样？

**参考答案**：验证要可复现、可对比不同 step 的真实能力，贪心解码确定性最强；训练时采样（`n>1`）是为了产生多样化的回答供 PPO/GRPO 计算优势，需要探索。

**练习 2**：`val_reward_fn` 和 `reward_fn` 是同一个类吗？差别在哪？

**参考答案**：都是 `RewardManager` 实例。差别仅在 `num_examine`：训练用 `0`（不打印），验证用 `1`（每种 `data_source` 打印 1 条解码样本供人眼检查）。打分逻辑完全相同。

---

## 5. 综合实践

**综合任务：画一张「分数从生成到 advantage」的完整数据流图，并标注奖励相关环节。**

把本讲与 u4-l3（fit 主循环）串起来：

1. 在一张大图上画出 `fit()` 单步训练的以下阶段，并把「奖励相关」的环节用红笔圈出：
   - `generate_sequences`（生成 n 条回答）
   - `repeat / union`（对齐 prompt 与 response）
   - `_balance_batch`
   - （可选）`rm_wg.compute_rm_score` → `rm_scores`
   - **`reward_fn(batch)` → `token_level_scores`** ← 本讲核心
   - `apply_kl_penalty` → `token_level_rewards`
   - `compute_advantage` → `advantages`
   - `update_critic / update_actor`
2. 在「`reward_fn`」这一格内部，再画一张小图：`DataProto` → `RewardManager.__call__` → 检查 `rm_scores`（短路？）→ 逐条解码 → `_select_rm_score_fn` 路由 → `compute_score` 返回标量 → 末位放置 → 返回 `reward_tensor`。
3. 在图上用箭头标出：标量分数如何从 `compute_score` 流到 `reward_tensor[i, valid_response_length-1]`，再经 `.sum(-1)`（验证时）或 `apply_kl_penalty`+`compute_advantage`（训练时）被消费。

**验收标准**：你能指着图讲清楚「为什么分数必须放在最后一个有效 token」「为什么 `attention_mask` 不可省」「model-based 与 rule-based 谁优先」三件事。如果某处讲不清，回到对应小节重读源码。

## 6. 本讲小结

- 奖励有两条来路：model-based（`rm_scores`，已 token 级）与 rule-based（`compute_score` 标量）。`RewardManager` 是统一入口。
- **优先级**：`RewardManager.__call__` 第一行检查 `rm_scores`，存在则短路返回，规则函数被跳过；TinyZero 默认 `reward_model.enable=False`，所以实际只走 rule-based。
- 代码注释里的「combine」与实现的「short-circuit」存在出入——读源码要相信代码。
- `RewardManager` 用 `attention_mask` 切出 prompt/response 真实段、解码、按 `data_source` 路由到 `compute_score`，再把标量放到 `reward_tensor[i, valid_response_length - 1]`（最后一个有效 response token）。
- **为什么末位 + 为什么用 mask**：response 是右填充，末尾是 pad；不用 mask 直接放 `[-1]` 会把分数丢到 pad 上被清零。末位放置是稀疏奖励 + 信用分配的要求，也是 model-based `_expand_to_token_level` 共享的不变量。
- `num_examine` 只控制调试打印（每种 `data_source` 打几条），不影响任何数值；`compute_score` 内部还有 1/64 概率的 `do_print` 作为另一处抽样观测。
- 验证用 `val_reward_fn`（同 `RewardManager`，`num_examine=1`）、贪心解码、只走规则奖励，最后 `.sum(-1)` 把 token 级压成样本级分数。

## 7. 下一步学习建议

- 本讲得到的 `token_level_scores`（放在末位 token 的稀疏奖励）是 PPO 一切计算的起点。下一单元 u5（核心算法 core_algos）会从这里接手：u5-l1 讲 `apply_kl_penalty` 如何从 `token_level_scores` 扣 KL 得到 `token_level_rewards`，以及 `compute_advantage` 如何把末位信号反向传播成每个 token 的优势。建议直接进入 u5-l1。
- 若你想先把「奖励如何被消费」看全，可先读 `verl/trainer/ppo/ray_trainer.py` 里 `apply_kl_penalty` 与 `compute_advantage` 两个函数（u5-l1 的主角），带着「末位 token 上那个标量是怎么被铺开成逐 token 优势」的问题去读。
- 若你更关心「如何接入自己的任务奖励」，可跳到 u7-l3（自定义新任务），那里会回到 `_select_rm_score_fn` 与 `compute_score` 的扩展实践。
