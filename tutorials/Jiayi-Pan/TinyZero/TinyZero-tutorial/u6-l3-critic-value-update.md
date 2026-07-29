# Critic 价值估计与更新

## 1. 本讲目标

在前面的讲义里，我们已经讲过两件事：u5-l3 里 GAE 如何把末端稀疏奖励摊还成每个 token 的优势 `advantages`，并把 `returns = advantages + values` 当作价值网络的监督目标；u6-l1 里 `ActorRolloutRefWorker` 这个混合引擎如何用一份 FSDP 权重同时承担 actor 与 rollout。

本讲把视角转向 **Critic（价值网络）**：它不在混合引擎里，而是作为独立的 `CriticWorker` 存在。读完本讲，你应该能够：

1. 说清 Critic 在 PPO 里扮演什么角色，以及它的「value head」是怎么用 `AutoModelForTokenClassification` 造出来的。
2. 看懂 `DataParallelPPOCritic.compute_values` 如何做一次只读前向、截取 response 段、再乘 mask 得到逐 token 的价值估计。
3. 看懂 `update_critic` 如何用 `returns` 监督价值网络、调用 `compute_value_loss`、并用梯度累积完成一次反向更新。
4. 解释一个关键对照：为什么 `dp_critic._forward_micro_batch` 和 `dp_actor._forward_micro_batch` 取 response 段的切片范围 `[:, -response_length - 1:-1]` 完全一致，而 critic 却**不需要 ratio / clip**。

> 本讲对应 GAE 路线（`adv_estimator=gae`，`use_critic=True`）。GRPO 路线（u5-l5）不需要 critic，本讲的代码在那条路线下根本不会被创建。

## 2. 前置知识

在进入源码前，先用最朴素的语言把几个概念讲清楚。

**Critic 是什么？** 在 PPO 这类 actor-critic 算法里，有两个网络协同工作：

- **Actor（策略网络）**：给定状态，输出「下一步该做什么」的概率分布。在语言模型里就是「每个位置下一个 token 的概率」。它管的是 *行动*。
- **Critic（价值网络）**：给定状态，输出一个标量，表示「从当前状态出发，预期能拿到多少累计奖励」。它管的是 *估值*，用来给 actor 的行动打分（好于预期还是差于预期），这个打分就是 **advantage（优势）**。

用一句话区分：actor 输出的是 *动作的概率*，critic 输出的是 *状态的价值*。

**为什么需要 critic？** 早期的策略梯度方法用一个完整回合的真实回报来估计优势，方差很大。Critic 给出一个「基准线」$V(s)$，我们用实际回报减掉这个基准线得到优势 $A_t$，能大幅降低方差。在 GAE（Generalized Advantage Estimation）里，critic 的逐 token 估值 $V(s_t)$ 是反向递推优势的核心原料（见 u5-l3）。

**value head 是什么？** Critic 不能直接复用 actor 的语言模型头（那个头输出词表大小的 logits）。它需要一个「每个 token 输出一个标量」的头。TinyZero/veRL 的做法很巧妙：直接借用 HuggingFace 的 `AutoModelForTokenClassification`（原本用于 token 分类任务，比如命名实体识别），把分类类别数设成 1（`num_labels=1`），于是每个 token 位置就输出一个标量——正好就是我们要的价值估计。这样不需要手写新的网络结构，复用现成的 transformer backbone。

**关键张量形状约定**（贯穿本讲）：

| 张量 | 形状 | 含义 |
|------|------|------|
| `input_ids` | `(batch, seqlen)` | prompt + response 拼接，prompt 左填充、response 右填充 |
| `responses` | `(batch, response_length)` | 仅 response 段的 token id |
| `attention_mask` | `(batch, seqlen)` | 1 表示真实 token，0 表示 pad |
| `values` / `vpreds` | `(batch, response_length)` | 每个 response token 对应一个价值标量 |
| `returns` | `(batch, response_length)` | critic 的监督目标（来自 GAE） |

其中 `seqlen = prompt_length + response_length`（经过填充后两者都是固定长度）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [verl/workers/critic/dp_critic.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py) | 本讲主角。`DataParallelPPOCritic` 实现 critic 的前向估值与反向更新 |
| [verl/workers/critic/base.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/base.py) | 抽象基类 `BasePPOCritic`，只定义 `compute_values` / `update_critic` 两个接口 |
| [verl/trainer/ppo/core_algos.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py) | 算法库。本讲用到 `compute_value_loss`，并回顾 `compute_gae_advantage_return` |
| [verl/utils/torch_functional.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py) | 小工具：`masked_mean`、`clip_by_value` |
| [verl/workers/fsdp_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py) | `CriticWorker`：构建 critic 模型（value head）、FSDP 包装、再实例化 `DataParallelPPOCritic` |
| [verl/workers/actor/dp_actor.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py) | 对照阅读：actor 的 `_forward_micro_batch`，与本讲 critic 做切片对照 |
| [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) | `fit()` 主循环里调用 `compute_values` 与 `update_critic` 的位置 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先看 critic 的模型本体（value head 是怎么来的），再依次精读 `_forward_micro_batch`、`compute_values`、`update_critic`，最后与 actor 做关键对照。

### 4.1 Critic 的角色与 value head 的构造

#### 4.1.1 概念说明

PPO 训练循环里 critic 要做两件事（见 u4-l3 的 `fit()` 流程）：

1. **估值（compute_values）**：在每一步训练开始、reward 算出来之前，对当前 batch 做一次只读前向，得到逐 token 的 `values`。这些 `values` 会喂给 GAE 算优势（u5-l3）。
2. **更新（update_critic）**：等 GAE 产出 `returns` 后，critic 用 `returns` 当监督目标做一次回归更新，让自己下次估得更准。

也就是说，critic 是一个 **回归器**：输入一条序列，输出每个 response 位置的一个标量价值，监督信号是 GAE 算出来的「真实」累计回报 `returns`。

#### 4.1.2 核心流程

value head 的构造流程在 `CriticWorker._build_critic_model_optimizer` 里：

1. 读基座模型配置，把 `num_labels` 强行设成 1。
2. 用 `AutoModelForTokenClassification.from_pretrained` 加载——这一步会把基座 transformer 的最后一层 hidden state 接到一个「输出维度 = num_labels = 1」的线性分类头上。
3. 用 FSDP 包装（`FULL_SHARD`），并构建 AdamW 优化器与 warmup 学习率调度器。
4. 把（模型, 优化器）交给 `DataParallelPPOCritic`。

关键直觉：critic 和 actor 共享**同一种 transformer backbone**（TinyZero 里都是 Qwen2.5），区别只在最后一个头——actor 的头是 `hidden→vocab`（输出 token 概率），critic 的头是 `hidden→1`（输出价值标量）。

#### 4.1.3 源码精读

把 `num_labels` 设为 1，是 value head 的全部秘密：

```python
critic_model_config.num_labels = 1
```

这行在 [verl/workers/fsdp_workers.py:573](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L573) —— 注释说明「可以用任意架构随机初始化，未必和 actor 相同」，但 TinyZero 实际用的就是同一基座。

随后加载模型（带 flash attention）：

```python
critic_module = AutoModelForTokenClassification.from_pretrained(
    pretrained_model_name_or_path=local_path,
    torch_dtype=torch_dtype,
    config=critic_model_config,
    attn_implementation='flash_attention_2',
    trust_remote_code=trust_remote_code)
```

见 [verl/workers/fsdp_workers.py:589-L593](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L589-L593)。注意它给 critic 关掉了 dropout（`classifier_dropout=0.`、`hidden_dropout='0'`，见 587–588 行），因为价值估计需要稳定的前向输出。

FSDP 包装与优化器构建在 [verl/workers/fsdp_workers.py:622-L637](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L622-L637)：critic 用 `FULL_SHARD` 策略、AdamW（默认 lr=1e-5，见 `ppo_trainer.yaml`）。

最后在 `init_model` 里实例化本讲的主角：

```python
self.critic = DataParallelPPOCritic(config=self.config,
                                    critic_module=self.critic_module,
                                    critic_optimizer=self.critic_optimizer)
```

见 [verl/workers/fsdp_workers.py:665-L667](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L665-L667)。从这里开始，所有估值与更新逻辑都进入 `dp_critic.py`。

> 接口层面，`DataParallelPPOCritic` 继承自抽象基类 `BasePPOCritic`（[verl/workers/critic/base.py:26-L40](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/base.py#L26-L40)），后者用 `@abstractmethod` 强制子类必须实现 `compute_values` 与 `update_critic`。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认 critic 与 actor 的「头」不同。

**操作步骤**：

1. 打开 [verl/workers/fsdp_workers.py:573](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L573)，确认 `num_labels = 1`。
2. 在同一文件里搜索 actor 的模型构建（`_build_model_optimizer`，位于 `ActorRolloutRefWorker` 内），对比它用的是哪个 HF 类（actor 走的是 `AutoModelForCausalLM`，输出词表 logits）。

**需要观察的现象**：actor 的最后输出维度是 `vocab_size`，critic 的最后输出维度是 `1`。

**预期结果**：你能用一句话说出——actor 头输出「下一个 token 的概率分布」，critic 头输出「当前 token 位置的状态价值标量」。

**待本地验证**：如果你本地装好了环境，可以写两行 `AutoConfig` + `AutoModelForTokenClassification` 打印 `model.classifier.out_features`，确认它等于 1。

#### 4.1.5 小练习与答案

**练习 1**：为什么 critic 用 `AutoModelForTokenClassification` 而不是 `AutoModelForCausalLM`？

**参考答案**：因为 critic 需要的是「每个 token 位置一个标量」，这是 token 级回归/分类任务的结构；`AutoModelForCausalLM` 的头输出 `vocab_size` 维（语言建模头），维度不匹配。设 `num_labels=1` 后，`TokenClassification` 的头正好是 `hidden→1`。

**练习 2**：critic 的 backbone 可以和 actor 不同吗？

**参考答案**：源码注释（549 行）明确说「未必和 actor 相同」，接口允许任意架构。但实践中为了表征对齐，TinyZero 让二者共用同一 Qwen2.5 基座。

---

### 4.2 `_forward_micro_batch`：逐 token 价值前向

#### 4.2.1 概念说明

`_forward_micro_batch` 是 critic 最底层的前向函数：输入一个 micro batch，输出每个 response token 位置的价值标量。它是 `compute_values`（只读）和 `update_critic`（带梯度）共用的前向逻辑。

这里最关键、也最容易困惑的一点是 **response 段的切片**：模型对整条 `input_ids`（prompt+response）前向，得到每个位置的输出，但我们要的是 response 段对应的那些位置。代码用的是 `[:, -response_length - 1:-1]` 这个看起来奇怪的切片——它正好取出 `response_length` 个元素。我们会在 4.4 节专门解释它为什么和 actor 完全一致。

#### 4.2.2 核心流程

`_forward_micro_batch` 的执行过程（非 remove_padding 分支，最常用）：

1. 取 `response_length = micro_batch['responses'].size(-1)`。
2. 在 `torch.autocast(bf16)` 下对整条序列前向：`output = self.critic_module(input_ids, attention_mask, position_ids, use_cache=False)`。
3. `values = output.logits`，形状 `(batch, seqlen, 1)`。
4. 切 response 段并 squeeze：`values = values[:, -response_length - 1:-1].squeeze(-1)`，得到 `(batch, response_length)`。
5. （remove_padding 分支）先用 `unpad_input` 去掉 pad、可选叠加 Ulysses 序列并行，再 `pad_input` 还原后切同样的 response 段。

`use_cache=False` 是个细节：明确告诉模型「我们在做前向不是在生成」，避免触发 KV cache 逻辑。

#### 4.2.3 源码精读

非 remove_padding 分支（默认）只有三行核心：

```python
output = self.critic_module(input_ids=input_ids,
                            attention_mask=attention_mask,
                            position_ids=position_ids,
                            use_cache=False)  # prevent model thinks we are generating
values = output.logits
values = values[:, -response_length - 1:-1].squeeze(-1)
```

见 [verl/workers/critic/dp_critic.py:95-L100](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L95-L100)。`output.logits` 因为 `num_labels=1` 而是 `(batch, seqlen, 1)`，`.squeeze(-1)` 后变 `(batch, seqlen)`，再切 response 段。

remove_padding 分支（[verl/workers/critic/dp_critic.py:61-L93](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L61-L93)）是显存优化路径：当 batch 里序列长短不齐时，先用 `unpad_input` 把所有 pad 压掉、拼成一条长序列喂给 `flash_attn_varlen`，算完再用 `pad_input` 还原成 `(batch, seqlen)`，最后切同样的 `[:, -response_length - 1:-1]`（93 行）。可选的 Ulysses 序列并行（`ulysses_sequence_parallel_size > 1`）会把这条长序列再按 sp 维切给多张卡。

函数签名和返回值见 [verl/workers/critic/dp_critic.py:53-L101](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L53-L101)。

#### 4.2.4 代码实践

**实践目标**：手算 `[:, -response_length - 1:-1]` 切出的元素个数，建立对切片的直觉。

**操作步骤**：

1. 假设 `seqlen = 8`，`response_length = 3`（即 prompt 占 5 个位置，response 占 3 个位置，位置下标 0..7）。
2. Python 里 `list(range(8))[-3-1:-1]` 等价于 `list(range(8))[-4:-1]`，也就是下标 `[4, 5, 6]`。

**需要观察的现象**：切出的下标是 `4,5,6`，正好 3 个元素 = `response_length`。

**预期结果**：切片 `[-response_length-1:-1]` 永远取出恰好 `response_length` 个元素，对应位置从 `prompt_length - 1` 到 `seqlen - 2`。

> 直觉解释：response 的第一个 token 在位置 `prompt_length`，它是被位置 `prompt_length - 1` 的输出「预测/估计」出来的。所以与 response token t 对齐的价值，取自位置 `prompt_length - 1 + t`。这一串位置正好是 `[-response_length-1:-1]`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 critic 前向时显式传 `use_cache=False`？

**参考答案**：训练阶段的 critic 做的是一次性整序列前向（不是逐 token 自回归生成），不需要也不应该构建 KV cache；传 `use_cache=False` 既省显存又避免模型误判进入生成模式（代码注释原文："prevent model thinks we are generating"）。

**练习 2**：remove_padding 分支最后为什么还要再切一次 `[:, -response_length - 1:-1]`？

**参考答案**：`pad_input` 还原出来的是完整的 `(batch, seqlen)` 张量，和非 remove_padding 分支的形状一致；为了拿到 response 段、保持两条路径输出形状统一，必须在最后切同样的 response 段。

---

### 4.3 `compute_values`：推理态估值

#### 4.3.1 概念说明

`compute_values` 是 critic 在 `fit()` 里被调的第一个方法（见 u4-l3）。它的职责很纯粹：**做一次只读前向，产出 `values` 张量**。这步发生在 reward 与 advantage 计算之前——因为 GAE 需要 `values` 作为输入。

它和 `update_critic` 的本质区别：`compute_values` 在 `eval()` + `no_grad()` 下运行，**不更新参数**；`update_critic` 在 `train()` 下运行，**反向传播更新参数**。

#### 4.3.2 核心流程

1. `self.critic_module.eval()`：切到推理态（关掉 dropout 等，尽管构造时已关）。
2. 只挑前向需要的 4 个 key：`['responses', 'input_ids', 'attention_mask', 'position_ids']`。
3. 按 `use_dynamic_bsz` 决定怎么切 micro batch：动态切按总 token 数（`rearrange_micro_batches`），否则按固定条数（`batch.split(micro_batch_size)`）。
4. 逐 micro batch 在 `torch.no_grad()` 下调用 `_forward_micro_batch`，收集结果。
5. `torch.concat` 拼回 `(batch, response_length)`。
6. **乘 mask 清零 pad 位置**：`values = values * attention_mask[:, -response_length - 1:-1]`。
7. 若用了动态 bsz，因为 `rearrange_micro_batches` 打乱了顺序，需要用 `get_reverse_idx` 把结果还原回原始顺序。

第 6 步是关键：response 段里也有右填充的 pad，这些位置的「价值」是无意义的，必须用 mask 清零，否则会污染后续 GAE 的反向递推。

#### 4.3.3 源码精读

eval 与 key 选择：

```python
self.critic_module.eval()
micro_batch_size = data.meta_info['micro_batch_size']
select_keys = ['responses', 'input_ids', 'attention_mask', 'position_ids']
batch = data.select(batch_keys=select_keys).batch
```

见 [verl/workers/critic/dp_critic.py:114-L117](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L114-L117)。注意 `compute_values` 的 batch_size 来自 `data.meta_info['micro_batch_size']`（推理态的 micro batch size），与 `update_critic` 用的 `ppo_micro_batch_size`（训练态）是两个不同的配置项。

只读前向循环：

```python
values_lst = []
for micro_batch in micro_batches:
    with torch.no_grad():
        values = self._forward_micro_batch(micro_batch)
    values_lst.append(values)
values = torch.concat(values_lst, dim=0)
```

见 [verl/workers/critic/dp_critic.py:127-L132](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L127-L132)。`no_grad()` 保证不建计算图、不耗额外显存。

乘 mask 清零 pad：

```python
responses = data.batch['responses']
attention_mask = data.batch['attention_mask']
response_length = responses.size(1)
values = values * attention_mask[:, -response_length - 1:-1]
```

见 [verl/workers/critic/dp_critic.py:133-L136](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L133-L136)。这里的 mask 切片 `[-response_length-1:-1]` 与 `_forward_micro_batch` 内部取 value 的切片**完全对齐**——value[t] 对应位置 `prompt_length-1+t`，mask 也取同一位置，保证「pad 位置的 value 被准确清零」。

动态 bsz 还原顺序（[verl/workers/critic/dp_critic.py:138-L142](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L138-L142)）：

```python
if use_dynamic_bsz:
    indices = list(itertools.chain.from_iterable(indices))
    revert_indices = torch.tensor(get_reverse_idx(indices), dtype=torch.long)
    values = values[revert_indices]
```

完整方法见 [verl/workers/critic/dp_critic.py:113-L144](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L113-L144)。

产出后，`fit()` 用 `batch.union(values)` 把它挂回 DataProto（[verl/trainer/ppo/ray_trainer.py:612-L615](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L612-L615)），随后 `compute_advantage` 在 gae 分支读 `data.batch['values']` 喂给 GAE（[verl/trainer/ppo/ray_trainer.py:119-L132](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L119-L132)）。

#### 4.3.4 代码实践

**实践目标**：理解 `compute_values` 为何必须乘 mask，以及它和 `update_critic` 用了不同的 batch size 来源。

**操作步骤**：

1. 在 [verl/workers/critic/dp_critic.py:115](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L115) 看到 `compute_values` 读的是 `data.meta_info['micro_batch_size']`。
2. 对比 [verl/workers/critic/dp_critic.py:164](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L164)，`update_critic` 读的是 `self.config.ppo_micro_batch_size`。
3. 在 [verl/workers/critic/dp_critic.py:136](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L136) 确认乘 mask 这一行。

**需要观察的现象**：估值与更新用的是两个独立的 micro batch size 配置；估值的输出在 pad 位置被强制清零。

**预期结果**：你能解释——估值用更大的 `micro_batch_size`（因为 `no_grad` 不存激活，显存占用小，可以开大）；更新用较小的 `ppo_micro_batch_size`（因为要存反向所需的激活，显存压力大）。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉第 136 行的 `values = values * attention_mask[:, -response_length - 1:-1]`，会对训练造成什么影响？

**参考答案**：response 段右填充的 pad 位置会保留前向算出的随机价值（非零），这些虚假价值会进入 GAE 的反向递推 `delta = r + gamma*V_{t+1} - V_t`，污染整条序列的优势估计，导致 advantage 失真、训练不稳定甚至发散。

**练习 2**：为什么 `compute_values` 用 `eval()` 而 `update_critic` 用 `train()`？

**参考答案**：`compute_values` 只是读取当前 critic 的估值供 GAE 使用，不应改变模型状态、也不需要 dropout/BN 的训练态行为；`update_critic` 要真正更新参数，必须处于 `train()` 模式。

---

### 4.4 `update_critic`：用 returns 拟合并更新

#### 4.4.1 概念说明

`update_critic` 是 critic 的「学习」步骤。GAE 算出 `returns`（每个 token 位置的「真实」累计回报，`returns = advantages + values`，见 u5-l3）后，critic 用它当监督目标，让自己对每个状态的估值 $V_\phi(s_t)$ 尽量逼近 `returns`。

这是一个标准的 **回归问题**：最小化预测价值 `vpreds` 与目标 `returns` 之间的误差。损失函数用的是 PPO 风格的 **clipped MSE**（`compute_value_loss`），加了一个 clip 来防止单步更新过大。

> 为什么用 `returns` 而不是 `advantages` 做监督？因为 `returns` 是未归一化的「绝对」回报尺度，和 value 本身的尺度一致；而 `advantages` 经过 `masked_whiten` 归一化（均值 0、方差 1），尺度对不上。详见 u5-l3。

#### 4.4.2 核心流程

`update_critic` 的更新结构（mini → micro → 梯度累积），和 `update_policy`（u6-l2）几乎对称：

1. `self.critic_module.train()`：切训练态。
2. 选 key：这次多了 `'values'`（旧估值）和 `'returns'`（监督目标）。
3. 按 `ppo_mini_batch_size` 切成若干 mini batch（决定一个 step 内更新几次）。
4. 对每个 mini batch：
   - `critic_optimizer.zero_grad()`。
   - 再按 `ppo_micro_batch_size`（或动态 bsz）切成若干 micro batch。
   - 对每个 micro batch：
     - 算 `eos_mask = attention_mask[:, -response_length - 1:-1]`（与 value 切片对齐的有效位置 mask）。
     - 前向得 `vpreds = self._forward_micro_batch(data)`（**这次带梯度**）。
     - 调 `compute_value_loss(vpreds, values, returns, eos_mask, cliprange_value)` 得 `vf_loss` 与 `vf_clipfrac`。
     - `loss = vf_loss / gradient_accumulation`，`loss.backward()` 累积梯度。
     - 记录指标 `critic/vf_loss`、`critic/vf_clipfrac`、`critic/vpred_mean`。
   - 一个 mini batch 的所有 micro batch 反向完后，调 `_optimizer_step()` 做一次梯度裁剪 + 优化器 step。

#### 4.4.3 源码精读

训练态与监督字段选择：

```python
self.critic_module.train()
select_keys = ['input_ids', 'responses', 'attention_mask', 'position_ids', 'values', 'returns']
batch = data.select(batch_keys=select_keys).batch
```

见 [verl/workers/critic/dp_critic.py:146-L152](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L146-L152)。注意这里同时带了 `values`（旧估值）与 `returns`（监督目标）；`values` 在 `compute_value_loss` 里用作 clip 的中心。

micro batch 内的核心计算：

```python
eos_mask = attention_mask[:, -response_length - 1:-1]
vpreds = self._forward_micro_batch(data)
vf_loss, vf_clipfrac = core_algos.compute_value_loss(vpreds=vpreds,
                                                     values=values,
                                                     returns=returns,
                                                     eos_mask=eos_mask,
                                                     cliprange_value=self.config.cliprange_value)
loss = vf_loss / self.gradient_accumulation
loss.backward()
```

见 [verl/workers/critic/dp_critic.py:178-L190](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L178-L190)。`eos_mask` 的切片 `[-response_length-1:-1]` 与 vpreds 的切片严格对齐——这是 masked 损失只在有效位置计算的前提。

`compute_value_loss` 是 clipped MSE：

```python
vpredclipped = verl_F.clip_by_value(vpreds, values - cliprange_value, values + cliprange_value)
vf_losses1 = (vpreds - returns)**2
vf_losses2 = (vpredclipped - returns)**2
vf_loss = 0.5 * verl_F.masked_mean(torch.max(vf_losses1, vf_losses2), eos_mask)
vf_clipfrac = verl_F.masked_mean(torch.gt(vf_losses2, vf_losses1).float(), eos_mask)
```

见 [verl/trainer/ppo/core_algos.py:234-L238](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L234-L238)。完整函数见 [core_algos.py:216-L239](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L216-L239)。

损失公式可写成：

\[
\mathcal{L}_{\text{vf}} = 0.5 \cdot \frac{1}{|M|}\sum_{t \in M} \max\!\left( (V_\phi(s_t) - R_t)^2,\; (\mathrm{clip}(V_\phi(s_t),\, V_{\text{old}}(s_t)\pm \epsilon_v) - R_t)^2 \right)
\]

其中 $M$ 是有效 token 集合（`eos_mask=1`），$\epsilon_v$ 是 `cliprange_value`（默认 0.5）。取 `max` 的用意：如果新预测已经比 clip 后的更远离 `returns`，就用 clip 后的（更保守）；反之用原始的。这样能防止 critic 在复用旧 rollout 数据时单步更新过猛。

`gradient_accumulation` 的含义（构造时算好，[dp_critic.py:48-L49](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L48-L49)）：

```python
assert self.config.ppo_mini_batch_size % self.config.ppo_micro_batch_size == 0
self.gradient_accumulation = self.config.ppo_mini_batch_size // self.config.ppo_micro_batch_size
```

每个 micro batch 的 loss 除以 `gradient_accumulation` 再 backward，等价于把整个 mini batch 的梯度做了平均——这和 actor 端（u6-l2）是同一套梯度累积逻辑。

梯度裁剪与优化器 step 在 `_optimizer_step`（[dp_critic.py:103-L111](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L103-L111)）：FSDP 下用 `critic_module.clip_grad_norm_`，否则用 `torch.nn.utils.clip_grad_norm_`，裁剪阈值 `grad_clip`（默认 1.0）。

完整的 `update_critic` 见 [verl/workers/critic/dp_critic.py:146-L204](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L146-L204)。`fit()` 里的调用点在 [ray_trainer.py:647-L649](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L647-L649)，注意它在 `update_actor` **之前**（critic 先于 actor 更新，且受 `critic_warmup` 门控，654 行）。

#### 4.4.4 代码实践

**实践目标**：用一组极小张量手算 clipped value loss，确认你理解了 `compute_value_loss` 的 `max` 逻辑。

**操作步骤**（纯手算 / 待本地验证）：

1. 取单样本单 token：`vpreds = [2.0]`，`values = [1.0]`（旧估值），`returns = [1.5]`，`cliprange_value = 0.5`，`eos_mask = [1]`。
2. 算 `vpredclipped = clip(2.0, 1.0-0.5, 1.0+0.5) = clip(2.0, 0.5, 1.5) = 1.5`。
3. 算 `vf_losses1 = (2.0 - 1.5)^2 = 0.25`，`vf_losses2 = (1.5 - 1.5)^2 = 0`。
4. `max(0.25, 0) = 0.25`，`vf_loss = 0.5 * 0.25 = 0.125`。

**需要观察的现象**：因为新预测 `2.0` 偏离旧估值 `1.0` 超过了 `cliprange_value=0.5`，被 clip 到 `1.5`，而 clip 后恰好等于 `returns`，所以 `vf_losses2=0`；但 `max` 选了更大的 `vf_losses1=0.25`。

**预期结果**：`vf_loss = 0.125`，`vf_clipfrac = mean(vf_losses2 > vf_losses1) = mean(0 > 0.25) = 0`（本例未触发 clip 主导）。

**待本地验证**：在本地用 `torch.tensor` 复现上述数值，对照 [core_algos.py:216-L239](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L216-L239) 的输出。

#### 4.4.5 小练习与答案

**练习 1**：`compute_value_loss` 里为什么用 `torch.max(vf_losses1, vf_losses2)` 而不是 `min`？

**参考答案**：取 `max` 等于在两个损失里选「更悲观（更大）」的那个。当新预测比 clip 后还偏离目标时（`vf_losses1` 更大），用真实损失，保证梯度不被 clip 削弱；当 clip 后的损失更大时，用 clip 后的，限制单步更新幅度。这是 PPO 价值损失的标准做法，防止 critic 用陈旧 rollout 数据时更新过猛。

**练习 2**：`update_critic` 为什么先于 `update_actor`？

**参考答案**：critic 用当前 batch 的 `returns` 更新估值；actor 的策略梯度依赖 advantage，而 advantage 的尺度与 critic 的估值质量相关。先更新 critic 能让随后 actor 用的估值基准更新鲜（虽然 advantage 已在本 step 算好，但顺序上 critic 优先是惯例，且 `critic_warmup` 门控允许只训 critic 不训 actor 来预热估值）。

---

## 5. 综合实践

本节把本讲两件最值得想清楚的事串起来：**切片对齐** 与 **critic 为何不需要 ratio/clip**。这是本讲规格里指定的核心实践任务。

### 任务一：对照 actor 与 critic 的 `_forward_micro_batch`

打开两个文件并排阅读：

- critic：[verl/workers/critic/dp_critic.py:53-L101](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L53-L101)
- actor：[verl/workers/actor/dp_actor.py:58-L141](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L58-L141)

**要回答的问题**：

1. 两者取 response 段都用 `[:, -response_length - 1:-1]`（critic 在 [100 行](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L100)，actor 在 [137 行](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L137)）。为什么切片必须一致？

   **参考答案**：因为 PPO 要求 advantage $A_t$、log-prob $\log\pi(a_t|s_t)$、value $V(s_t)$ 三者在**同一个时间步 $t$** 上对齐。actor 的 `log_prob[t]` 是「在位置 `prompt_length-1+t` 的输出下，生成 response 第 t 个 token 的对数概率」；critic 的 `value[t]` 取自同一位置 `prompt_length-1+t`，表示「生成 response 第 t 个 token 之前那个状态的价值」。两者用同一切片，才能保证 `advantage[t]`（由 `value[t]`、`value[t+1]`、`reward[t]` 算出）和 `log_prob[t]`（策略梯度的乘法因子）逐元素对齐，策略损失 `ratio[t] * advantage[t]` 才有意义。

2. 切片 `[-response_length-1:-1]` 取的是「预测 response token 的那个位置」而非 response token 本身所在位置——请用 `seqlen=8, response_length=3` 验证它取出下标 `[4,5,6]`（即 `prompt_length-1` 到 `seqlen-2`），并解释为什么 response 第 0 个 token（位置 5）的价值取自位置 4。

   **参考答案**：位置 4 是 prompt 的最后一个 token，它的前向输出对应「读完整个 prompt、即将生成第一个 response token」的状态——这正是 response 第 0 步的状态价值 $V(s_0)$。所以 value[t] 取自位置 `prompt_length-1+t`，整体切片是 `[-response_length-1:-1]`。

### 任务二：解释 critic 为何不需要 ratio / clip（策略损失那种）

**要回答的问题**：actor 的策略损失里有 `ratio = exp(log_prob - old_log_prob)` 和 `torch.clamp(ratio, 1-ε, 1+ε)`（见 u5-l2 的 `compute_policy_loss`），而 critic 的损失（4.4 节）完全没有 ratio，只有 clipped MSE。为什么？

**参考答案**（分三点）：

1. **ratio 是为随机策略的重要性采样而生**。actor 是随机策略，训练时策略分布会变化（$\pi_\theta$ ≠ 收集数据时的 $\pi_{\text{old}}$），复用旧样本估计新策略梯度时必须用 `ratio = π_new/π_old` 做偏差校正。critic 是**确定性**回归器，输出一个标量价值，不存在「概率分布」，自然没有概率比 `ratio` 可言。

2. **critic 是回归，不是策略梯度**。critic 最小化的是预测价值与目标 `returns` 的 MSE，目标是固定的（一个 step 内 `returns` 不变），不存在「用旧分布的样本估新分布期望」的问题。回归问题里没有 importance sampling 的需要。

3. **但 critic 仍然有「自己的 clip」**：`compute_value_loss` 里的 `vpredclipped = clip(vpreds, values ± cliprange_value)`（[core_algos.py:234](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L234)）。这里的 `values` 是**旧估值**（rollout 时的 critic 输出），clip 的目的和策略损失的 clip 类似——防止 critic 复用陈旧数据时单步更新过大。所以准确说法是：**critic 没有「概率比 ratio」，但有「价值 clip」**；两者的数学形式不同，但「限制单步更新幅度」的工程动机一致。

### 任务三（源码阅读型）：画一张 critic 数据流图

对照 [ray_trainer.py:611-L649](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L611-L649)，画出 critic 在一个训练 step 里的数据流：

```
batch ──compute_values──> values ──union──> batch
                                        │
              (reward_fn / apply_kl_penalty / compute_advantage)
                                        │
                                  values + returns
                                        │
                                 ──update_critic──> metrics
```

标注清楚：`compute_values` 发生在 advantage 之前（给 GAE 喂 values），`update_critic` 发生在 advantage 之后（用 returns 监督），二者用的是同一个 `_forward_micro_batch`，但前者 `eval()+no_grad()`，后者 `train()+backward()`。

---

## 6. 本讲小结

- Critic 是 PPO 里的价值网络，用 `AutoModelForTokenClassification` + `num_labels=1` 造出「每 token 一个标量」的 value head，和 actor 共享 backbone、只差最后一个头（[fsdp_workers.py:573](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L573)）。
- `_forward_micro_batch` 是 critic 的底层前向，对整条序列算 logits 后用 `[:, -response_length-1:-1]` 切出恰好 `response_length` 个价值标量（[dp_critic.py:95-L100](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L95-L100)）。
- `compute_values` 是只读估值（`eval()+no_grad()`），产出 `values` 供 GAE 使用，并乘 mask 清零 response 段的 pad 位置（[dp_critic.py:113-L144](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L113-L144)）。
- `update_critic` 是带梯度的回归更新，用 `returns` 监督、调用 clipped MSE 的 `compute_value_loss`，并走 mini→micro→梯度累积结构（[dp_critic.py:146-L204](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L146-L204)）。
- actor 与 critic 的 response 切片完全一致（`[-response_length-1:-1]`），是为了让 advantage、log-prob、value 在同一时间步 $t$ 对齐。
- critic 没有 ratio（它是确定性回归，不需要重要性采样），但有「价值 clip」（`cliprange_value`，默认 0.5）来限制单步更新幅度。

## 7. 下一步学习建议

- **横向读完三个 Worker 的前向**：本讲讲完 critic，建议回头对照 u6-l2（actor 的 `update_policy`）和 u6-l4（vLLM rollout）。三者共用 `_forward_micro_batch` 的骨架，但 actor 多了 temperature 缩放与 log-prob、rollout 走的是 vLLM 推理引擎，对比阅读能让你彻底看清「同一个 backbone 三种用法」。
- **纵向追一条数据**：从 u4-l3 的 `fit()` 主循环出发，把 `values → GAE advantages/returns → update_critic` 这条链路在源码里走一遍，确认你理解 `returns = advantages + values` 里 `values` 的尺度为何必须未归一化（u5-l3）。
- **进入扩展主题**：下一讲 u6-l4 会讲 vLLM rollout 生成，之后 u7 单元进入 FSDP 并行、序列长度均衡、自定义新任务等高级主题。如果你想动手，可以试着把 `adv_estimator` 从 `gae` 切到 `grpo`（u5-l5），观察 critic 相关代码（本讲整篇）如何被完全旁路——这是检验你是否真懂 critic 何时存在的最好练习。
