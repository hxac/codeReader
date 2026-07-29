# Actor 策略前向与更新

## 1. 本讲目标

本讲深入 PPO/GRPO 训练循环中最核心的一块「计算」——**策略网络（Actor）的前向与参数更新**。读完本讲你应当能够：

1. 说清 `DataParallelPPOActor` 的三个入口方法 `compute_log_prob` / `_forward_micro_batch` / `update_policy` 各自的职责与调用时机。
2. 画出 `update_policy` 中 **mini-batch → micro-batch → 梯度累积** 的三层循环结构，并解释 `gradient_accumulation = ppo_mini_batch_size // ppo_micro_batch_size` 为什么用于 loss 缩放。
3. 理解 `_forward_micro_batch` 里 logits 截取（`-response_length-1:-1`）、temperature 缩放、log_prob 与 entropy 的计算方式。
4. 看懂 `use_remove_padding` 配合 `flash_attn_varlen`、以及 Ulysses 序列并行（SP）如何减少 padding 浪费。
5. 独立追踪一条 `policy_loss = pg_loss - entropy_loss*entropy_coeff (- kl_loss*kl_loss_coef)` 的组合公式在源码中是如何被拼装出来的。

## 2. 前置知识

在进入本讲前，建议你已经建立以下认知（对应前置讲义）：

- **u6-l1 混合引擎**：`ActorRolloutRefWorker` 用 `role` 字符串派生 `_is_actor/_is_rollout/_is_ref` 三个标志。本讲的主角 `DataParallelPPOActor` 正是这个 worker 在 `_is_actor=True` 时实例化的子模块（[fsdp_workers.py:L327-L329](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L327-L329)），它持有 FSDP 包裹后的 `actor_module_fsdp` 和 `actor_optimizer`。
- **u5-l2 策略损失**：PPO 用 importance ratio \(r_t=\exp(\log\pi_\theta-\log\pi_{\theta_{old}})\) 做双侧裁剪，`compute_policy_loss` 返回 `pg_loss / pg_clipfrac / ppo_kl` 三项。本讲要回答：这个函数的输入 `log_prob` 是怎么从一次前向算出来的，算完之后 loss 又是怎么反传的。

补充几个初学者可能陌生的术语：

- **next-token 预测与 shift 对齐**：自回归语言模型在位置 \(t\) 产出的 logits 用来预测位置 \(t+1\) 的 token。因此要算「回答里第 \(k\) 个 token 的对数概率」，必须取「它前一个位置」的 logits。这就是源码里反复出现的切片 `-response_length-1:-1` 的来历。
- **梯度累积（gradient accumulation）**：显存放不下整个 mini-batch 的前向/反向时，把 mini-batch 切成若干 micro-batch，每个 micro-batch 算一次 loss、做一次 `backward()`，让梯度在 `.grad` 里累加；所有 micro-batch 跑完后再 `optimizer.step()` 一次。为保证「累积后的梯度」与「一次性算整 mini-batch 的梯度」期望一致，每个 micro-batch 的 loss 要除以 micro-batch 个数。
- **temperature（温度）**：在 softmax 前把 logits 除以 \(T\)。\(T>1\) 让分布更平坦、采样更多样；\(T=1\) 不改变分布。本讲里 temperature 只作用于 **log_prob 的数值尺度**，不改变采样（采样发生在 rollout 阶段）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [verl/workers/actor/dp_actor.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py) | 本讲主角。`DataParallelPPOActor` 实现，含三个核心方法。 |
| [verl/workers/actor/base.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/base.py) | 抽象基类 `BasePPOActor`，只规定 `compute_log_prob` / `update_policy` 两个接口签名。 |
| [verl/trainer/ppo/core_algos.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py) | 算法函数库。本讲复用其中的 `compute_policy_loss` 与 `kl_penalty`。 |
| [verl/utils/torch_functional.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py) | 底层张量工具：`logprobs_from_logits`、`entropy_from_logits`、`masked_mean`。 |
| [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml) | actor 分组的默认超参（批大小、clip、entropy 系数、KL loss 等）。 |
| [verl/workers/fsdp_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py) | `ActorRolloutRefWorker.update_actor` —— 把数据从 driver 传进来、调用 `update_policy`、收走 metrics。 |

## 4. 核心概念与源码讲解

### 4.1 DataParallelPPOActor 的整体职责与构造

#### 4.1.1 概念说明

`DataParallelPPOActor`（数据并行 PPO Actor）是单进程视角下的策略网络控制器。注意「数据并行」在这里指的是：每个 GPU（每个 worker）各自跑一份前向/反向，跨 GPU 的数据切分与拼回由更上层的 dispatch 机制（见 u3-l3）负责，本类**不感知**多卡。它只关心「我这一张卡上拿到的一批数据，怎么变成 loss、怎么更新参数」。

它有两个互斥的工作模式，由是否传入 `actor_optimizer` 区分：

- **传入 optimizer** → 是真正会被训练的 Actor（`_is_actor`），跑 `update_policy` 更新参数。
- **不传 optimizer（为 None）** → 退化成 **Reference Policy**（参考策略，冻结不训练），只用来算 `ref_log_prob` 给 KL 约束用（见 u6-l1 里 ref 模块独立构建的说明）。源码注释写得很直白：`"When optimizer is None, it is Reference Policy"`。

它的两个抽象方法接口由 [base.py:L26-L66](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/base.py#L26-L66) 的 `BasePPOActor` 规定：`compute_log_prob`（只读前向，返回对数概率）和 `update_policy`（训练，返回 metrics 字典）。

#### 4.1.2 核心流程

类构造时只做四件事：保存模块/优化器、读取两个并行相关开关、把 `entropy_from_logits` 用 `torch.compile` 加速。之后所有计算都在三个方法里发生：

```text
DataParallelPPOActor.__init__
  ├─ self.actor_module = actor_module          # FSDP 包裹的策略模型
  ├─ self.actor_optimizer = actor_optimizer    # None 时表示参考策略
  ├─ self.use_remove_padding = config 读取      # 是否去 padding
  ├─ self.ulysses_sequence_parallel_size        # SP 并行度
  ├─ self.use_ulysses_sp = sp_size > 1          # 是否启用 Ulysses SP
  └─ self.compute_entropy_from_logits = torch.compile(entropy_from_logits)
```

两个入口方法的调用时机：

- `compute_log_prob`：在 **rollout 生成之后、训练之前** 被调用一次（`generate_sequences` 里 `recompute_log_prob=True` 分支），用来重新算一遍 `old_log_probs`。它是 `eval()` + `no_grad()` 的只读前向。
- `update_policy`：在 fit() 的 `update_actor` 阶段被调用（见 [ray_trainer.py:L656-L657](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L656-L657)），是 `train()` 模式下真正反传更新参数的地方。

#### 4.1.3 源码精读

构造函数非常短，全部在 [dp_actor.py:L41-L56](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L41-L56)：

```python
def __init__(self, config, actor_module, actor_optimizer=None):
    """When optimizer is None, it is Reference Policy"""
    super().__init__(config)
    self.actor_module = actor_module
    self.actor_optimizer = actor_optimizer
    self.use_remove_padding = self.config.get('use_remove_padding', False)
    print(f'Actor use_remove_padding={self.use_remove_padding}')
    self.ulysses_sequence_parallel_size = self.config.ulysses_sequence_parallel_size
    self.use_ulysses_sp = self.ulysses_sequence_parallel_size > 1
    self.compute_entropy_from_logits = torch.compile(verl_F.entropy_from_logits, dynamic=True)
```

中文说明：

- `use_remove_padding` 从 config 取，默认 `False`；注意它默认不在 yaml 的 actor 分组里，而是由 `ActorRolloutRefWorker.init_model` 在运行时根据模型是否支持动态写入的（[fsdp_workers.py:L323-L326](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L323-L326) 用 `open_dict` 临时打开结构再 set）。
- `torch.compile(..., dynamic=True)` 把熵计算编译加速；`dynamic=True` 表示允许输入形状变化（不同 micro-batch 大小不同）。

#### 4.1.4 代码实践

**实践目标**：确认「optimizer 是否为 None」决定了这个类是 Actor 还是 Reference Policy，并追踪两条调用路径。

**操作步骤**：

1. 打开 [fsdp_workers.py:L327-L329](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L327-L329)，看到 actor 分支 `self.actor = DataParallelPPOActor(config=..., actor_module=self.actor_module_fsdp, actor_optimizer=self.actor_optimizer)` —— 传了 optimizer，是可训练 Actor。
2. 再看 ref 分支 [fsdp_workers.py:L348](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L348)：`self.ref_policy = DataParallelPPOActor(config=self.config.ref, actor_module=self.ref_module_fsdp)` —— 没有 `actor_optimizer` 参数，命中默认值 `None`，即冻结的参考策略。

**需要观察的现象**：同一个类被复用于两个角色，差异只在构造参数。

**预期结果**：ref_policy 实例的 `self.actor_optimizer is None`，因此它只应被调用 `compute_log_prob`，绝不会被调用 `update_policy`（调用会因 optimizer 为 None 而出错）。

#### 4.1.5 小练习与答案

**练习 1**：为什么参考策略复用 `DataParallelPPOActor` 而不是新写一个类？
**参考答案**：因为参考策略与 Actor 的前向计算（算 log_prob）完全相同，区别只在「不反传、不更新」。复用前向代码避免重复，靠「不传 optimizer + 只调 `compute_log_prob`」来表达「冻结」语义，是典型的用构造参数切换行为的做法。

**练习 2**：`torch.compile(entropy_from_logits, dynamic=True)` 里的 `dynamic=True` 为什么必要？
**参考答案**：actor 在不同 micro-batch 间 logits 形状（`token数 × vocab_size`）会变，尤其开启动态 batching 时 token 数不固定；`dynamic=True` 让编译图允许形状变化而不必每次重编译。

---

### 4.2 _forward_micro_batch：从 logits 到 log_prob / entropy 的前向核心

#### 4.2.1 概念说明

`_forward_micro_batch` 是整个 Actor 最底层的「一个 micro-batch 的前向」，`compute_log_prob` 和 `update_policy` 都调它。它接收一个 micro-batch 字典，返回两个形状均为 `(batch, response_length)` 的张量：

- `log_probs`：回答里每个 token 在**当前策略**下的对数概率 \(\log\pi_\theta(a_t|s_{<t})\)。
- `entropy`：回答里每个位置上策略分布的香农熵 \(H(\pi_\theta(\cdot|s_{<t}))\)，用于熵正则项。

它内部有两条分支，由 `use_remove_padding` 开关二选一：

| 分支 | 触发条件 | 核心做法 |
| --- | --- | --- |
| **naive**（不去 padding） | `use_remove_padding=False`（默认） | 直接把带 padding 的 `[bs, seqlen]` 喂模型，padding 位置照常参与 attention。 |
| **rmpad**（去 padding） | `use_remove_padding=True` | 用 `unpad_input` 把所有有效 token 压成一条长序列，靠 `flash_attn_varlen`（变长 attention）只算有效部分，再 `pad_input` 还原形状。 |

rmpad 分支还可叠加 **Ulysses 序列并行**（`use_ulysses_sp`）：把那条压扁的长序列在 SP 组上再切分，让多张卡合起来算一个超长序列。

#### 4.2.2 核心流程

naive 分支的流程（最常用，TinyZero 默认走这条）：

```text
_forward_micro_batch(micro_batch, temperature)  [naive 分支]
  ├─ input_ids/attention_mask/position_ids = micro_batch 取出
  ├─ output = actor_module(input_ids, attention_mask, position_ids, use_cache=False)
  │           # logits 形状 (bs, seqlen, vocab)
  ├─ logits.div_(temperature)                    # 原地除温度
  ├─ logits = logits[:, -response_length-1:-1]   # 截取「预测 response」的那一段
  ├─ log_probs = logprobs_from_logits(logits, responses)   # 取 response 对应位置
  └─ entropy   = entropy_from_logits(logits)
  # 二者形状都是 (bs, response_length)
```

关键是对 **shift 对齐**的理解。`input_ids = [prompt ; response]` 拼成 `seqlen = prompt_length + response_length`。要预测 response 的第 0 个 token，需要 prompt 最后一个位置（index = `prompt_length-1 = seqlen - response_length - 1`，即负索引 `-response_length-1`）的 logits；要预测 response 最后一个 token，需要 index `seqlen-2 = -2`。因此切片 `[-response_length-1 : -1]` 正好取出 `response_length` 个 logit 位置，与 `responses` 的 `response_length` 个 token 一一对应：

\[ \text{log\_prob}_t = \log\pi_\theta(\text{response}_t \mid \text{tokens}_{<\text{位置 }t}) \]

entropy 与 log_prob 共用同一批 logits，所以位置对齐天然一致。

`temperature` 在 softmax 之前以 `logits.div_(temperature)` **原地**除入，等价于把分布变成 \(\mathrm{softmax}(\text{logits}/T)\)。原地操作（带下划线的 `div_`）是为了省显存、避免 OOM（参见 [torch_functional.py:L359-L361](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L359-L361) 的 `post_process_logits` 同样注释 `inplace operation to avoid OOM`）。

#### 4.2.3 源码精读

整个方法在 [dp_actor.py:L58-L141](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L58-L141)。先看 naive 分支（最该读懂的一段），见 [dp_actor.py:L130-L141](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L130-L141)：

```python
else:  # not using rmpad and no ulysses sp
    output = self.actor_module(input_ids=input_ids,
                               attention_mask=attention_mask,
                               position_ids=position_ids,
                               use_cache=False)  # prevent model thinks we are generating
    logits = output.logits
    logits.div_(temperature)
    logits = logits[:, -response_length - 1:-1]  # (bsz, response_length)
    log_probs = logprobs_from_logits(logits, micro_batch['responses'])
    entropy = verl_F.entropy_from_logits(logits)  # (bsz, response_length)
```

中文说明：

- `use_cache=False` 很关键：模型只要看到 KV cache 相关线索就会以为在「生成」，这里我们只是做一次完整前向拿 logits，所以显式关掉。
- `logits.div_(temperature)` 在截取**之前**对整段 logits 做了温度缩放——因为截取只是切 view，缩放放在哪一步都等价，但放前面一次性做完更省事。
- 截取后再分别算 `log_probs`（用 `responses` 做 label）和 `entropy`。

底层 `logprobs_from_logits` 在 [torch_functional.py:L49-L62](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L49-L62)：

```python
def logprobs_from_logits(logits, labels):
    if FLAH_ATTN_CROSS_ENTROPY_LOSS_AVAILABLE:
        ...  # 走 flash_attn 的 triton cross_entropy，更快、更省显存
        output = logprobs_from_logits_flash_attn(logits, labels)
    else:
        output = logprobs_from_logits_naive(logits, labels)
    return output
```

naive 实现 [torch_functional.py:L70-L73](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L70-L73) 是 `log_softmax` 后按 label gather；flash_attn 实现则是把 `-(cross_entropy)` 取反（因为交叉熵 = -log_prob）。两条路数值等价，只是性能不同。

`entropy_from_logits` 在 [torch_functional.py:L95-L99](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/torch_functional.py#L95-L99)：

```python
def entropy_from_logits(logits):
    pd = torch.nn.functional.softmax(logits, dim=-1)
    entropy = torch.logsumexp(logits, dim=-1) - torch.sum(pd * logits, dim=-1)
    return entropy
```

这是分类分布熵的紧凑写法 \(\log Z - \sum_i p_i \cdot \text{logit}_i\)，其中 \(\log Z = \text{logsumexp}(\text{logits})\)。避免显式构造 `log_softmax` 再相乘，数值更稳。

rmpad 分支（[dp_actor.py:L71-L128](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L71-L128)）核心差别在于：先用 `unpad_input` 按 `attention_mask` 把所有 batch 的有效 token 压成一条 `(1, total_nnz)` 的长序列、记录 `indices` 以便还原；用 `torch.roll(..., shifts=-1, dims=1)` 把 input_ids 左移一位作为「下一个 token」的 label；传 `attention_mask=None` 给模型以触发 `flash_attn_varlen`（靠 `cu_seqlens` 区分各样本，不在 padding 上浪费算力）；算完用 `pad_input` 还原成 `(bs, seqlen)`，最后同样切 `[:, -response_length-1:-1]` 拿到 response 段。若再开 Ulysses SP（`use_ulysses_sp`），则在压扁后多一步 `ulysses_pad_and_slice_inputs` 切分、计算后 `gather_outpus_and_unpad` 聚合（[dp_actor.py:L84-L115](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L84-L115)）。

#### 4.2.4 代码实践

**实践目标**：用一个极小的 toy 前向，验证「shift 对齐 + temperature」与 `_forward_micro_batch` 数值一致。

**操作步骤**（待本地验证，需可跑 PyTorch 的环境）：

1. 构造一个 vocab_size=4、prompt_length=2、response_length=3 的 toy 模型（随便一个 `nn.Linear` 套 `nn.Embedding` 即可），手工算 `logits[:, -4:-1]`（即 `-response_length-1:-1`）的 log_softmax 并按 responses gather。
2. 用 `temperature=1.0` 调一次你的 toy 版 `_forward_micro_batch`，再改成 `temperature=2.0` 调一次，对比同一位置 log_prob 的差异。
3. 验证：\(T=2\) 时分布更平，原本高概率 token 的 log_prob 会下降、低概率 token 的 log_prob 会上升。

**预期结果**：\(T=2\) 的熵应明显大于 \(T=1\) 的熵；log_prob 随 \(T\) 增大而向均匀靠拢。

> 提示：不必非得跑通真实 3B 模型。本实践旨在用最小例子确认「logits 切片位置」与「温度作用点」这两件事。

#### 4.2.5 小练习与答案

**练习 1**：为什么切片是 `[:, -response_length-1:-1]` 而不是 `[:, -response_length:]`？
**参考答案**：因为 next-token 预测存在一步 shift——位置 \(t\) 的 logits 预测的是 \(t+1\) 的 token。response 共 `response_length` 个 token，预测它们所需的 logits 从「prompt 最后一位」（index `-response_length-1`）到「response 倒数第二位」（index `-2`，即 `-1` 前闭右开到的位置）。`-response_length-1:-1` 正好取到这 `response_length` 个位置；若取 `-response_length:` 会错位一位。

**练习 2**：`use_remove_padding=True` 时，为什么传给模型的 `attention_mask=None` 反而是对的？
**参考答案**：去 padding 后输入是一条变长打包序列，传统的 `[bs, seqlen]` attention_mask 已不存在；模型改用 `flash_attn_varlen`，靠 `cu_seqlens`（每条样本的长度累积数组）来区分样本边界，所以不再需要显式 attention_mask。传 `None` 正是触发这条 varlen 路径的信号。

---

### 4.3 compute_log_prob：只读地重算回答的对数概率

#### 4.3.1 概念说明

`compute_log_prob` 是一个**纯前向、不反传**的方法，目的只有一个：给定 `(input_ids, attention_mask, position_ids, responses)`，算出每个 response token 在**当前策略**下的 log_prob。

它最重要的使用场景是 u6-l1 提到的 **recompute old_log_probs**：rollout 用 vLLM 生成回答时，vLLM 与 FSDP 训练前向的数值并不完全一致（精度、实现差异），如果直接拿 vLLM 顺手产出的 log_prob 当 PPO 的 `old_log_prob`，会让 importance ratio \(r_t\) 的起点偏离 1，破坏 PPO 的更新前提。因此在 `generate_sequences` 末尾，会用 actor 自己的前向把 `old_log_probs` 重新算一遍，保证 \(r_t\) 初始接近 1（详见 u6-l1）。ref policy 的 `ref_log_prob` 也是用同一套逻辑算的（同一个类、不同实例）。

#### 4.3.2 核心流程

```text
compute_log_prob(data)                                # data: DataProto
  ├─ actor_module.eval()                              # 关闭 dropout 等
  ├─ micro_batch_size / temperature / use_dynamic_bsz = data.meta_info[...]
  ├─ batch = data.select([...]).batch                # 只挑需要的 4 个张量列
  ├─ micro_batches = 按 bsz 切分（固定条数 或 动态 token 预算）
  ├─ for mb in micro_batches:
  │     with torch.no_grad():
  │         _, log_probs = _forward_micro_batch(mb, temperature)   # 只要 log_prob，丢弃 entropy
  │     log_probs_lst.append(log_probs)
  ├─ log_probs = torch.concat(log_probs_lst, dim=0)
  └─ 若 use_dynamic_bsz: 按 reverse_indices 还原原始顺序
  return log_probs                                    # (batch, response_length)
```

两个细节值得注意：

1. **`temperature` 必须从 `meta_info` 显式取**，源码注释 `# temperature must be in the data.meta_info to avoid slient error`：如果不传，代码会 KeyError 直接报错，而不是默认 1.0 静默算错——这是一种「显式优于隐式」的防御式写法。temperature 由上层从 `rollout.temperature` 注入（见 [fsdp_workers.py:L430](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L430)），必须与 rollout 采样时的温度一致，否则重算的 old_log_prob 与采样分布不匹配。
2. **动态 batching 的乱序与还原**：固定 batching 时 `batch.split(micro_batch_size)` 保持原序，concat 回去顺序天然正确；动态 batching 时 `rearrange_micro_batches` 会按 token 均衡**打乱样本顺序**来装包，因此 concat 后需要用 `get_reverse_idx(indices)` 把顺序还原（[dp_actor.py:L195-L199](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L195-L199)）。

#### 4.3.3 源码精读

完整方法在 [dp_actor.py:L153-L201](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L153-L201)。关键片段：

```python
def compute_log_prob(self, data: DataProto) -> torch.Tensor:
    self.actor_module.eval()
    micro_batch_size = data.meta_info['micro_batch_size']
    temperature = data.meta_info['temperature']   # 必须显式传入，避免静默错误
    use_dynamic_bsz = data.meta_info['use_dynamic_bsz']
    select_keys = ['responses', 'input_ids', 'attention_mask', 'position_ids']
    batch = data.select(batch_keys=select_keys).batch

    if use_dynamic_bsz:
        max_token_len = data.meta_info['max_token_len'] * self.ulysses_sequence_parallel_size
        micro_batches, indices = rearrange_micro_batches(batch=batch, max_token_len=max_token_len)
    else:
        micro_batches = batch.split(micro_batch_size)

    log_probs_lst = []
    for micro_batch in micro_batches:
        with torch.no_grad():
            _, log_probs = self._forward_micro_batch(micro_batch, temperature=temperature)
        log_probs_lst.append(log_probs)
    log_probs = torch.concat(log_probs_lst, dim=0)

    if use_dynamic_bsz:
        ...  # 用 get_reverse_idx 还原顺序
    return log_probs
```

中文说明：

- `data.select(batch_keys=select_keys)` 只保留计算 log_prob 必需的 4 列，丢掉 `old_log_probs`、`advantages` 等无关字段，减少搬运。
- 注意这里 `temperature` 缩放的是「当前策略」的 logits，所以算出来的是 \(\log\pi_{\theta_{old}}\)（如果此刻 \(\theta\) 还没被这一步更新过）。这与 `update_policy` 里用**同一个** `_forward_micro_batch`、**同一个** temperature 算出的 `log_prob`（即 \(\log\pi_\theta\)）形成对照——PPO 的 ratio 正是这两者之比。
- 动态 batching 的 `rearrange_micro_batches` 在 [seqlen_balancing.py:L224-L256](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L224-L256)：按各样本有效 token 数（`attention_mask.sum(dim=1)`）做均衡分区装包，让每个 micro-batch 的总 token 数接近 `max_token_len`，从而提升 GPU 利用率（详见 u7-l2）。

#### 4.3.4 代码实践

**实践目标**：搞清 `old_log_prob` 与 `log_prob` 在数值上「应该几乎相等」这件事，以及 temperature 不一致会带来什么偏差。

**操作步骤**（源码阅读型）：

1. 在 [dp_actor.py:L189-L192](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L189-L192) 确认 `compute_log_prob` 复用的就是 `_forward_micro_batch`，与 `update_policy`（下一节）是**同一个前向函数**。
2. 追踪 `temperature` 的来源：`compute_log_prob` 从 `meta_info['temperature']` 取，而该值由 [fsdp_workers.py:L427-L430](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L427-L430) 写入 `rollout.temperature`；`update_policy` 同样从 `meta_info['temperature']` 取（[dp_actor.py:L209](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L209)）。
3. 回答：如果在 rollout 采样时 temperature=1.0，但 recompute 时因配置错误传成 0.5，`old_log_prob` 会偏大还是偏小？

**预期结果**：因为两次前向用同一个 `_forward_micro_batch`、同一次的 \(\theta\)，理论上 `old_log_prob`（recompute 得到）与稍后 `update_policy` 第一步算出的 `log_prob` 应几乎相等，使 ratio \(\approx 1\)。若 temperature 不一致，recompute 出的分布会偏尖（T 变小），高概率 token 的 log_prob 被高估，ratio 偏离 1，PPO 更新起点就被污染。

#### 4.3.5 小练习与答案

**练习 1**：`compute_log_prob` 为什么用 `eval()` 和 `torch.no_grad()`？
**参考答案**：它只是「读出」当前策略下回答的概率，不训练、不需要梯度；`eval()` 关掉 dropout/BN 随机性保证前向确定，`no_grad()` 不建计算图、省显存。

**练习 2**：动态 batching 下，为什么 concat 之后还要 `get_reverse_idx` 还原顺序，而固定 batching 不用？
**参考答案**：固定 batching 用 `batch.split(n)` 切分保持原序，concat 后顺序天然一致；动态 batching 的 `rearrange_micro_batches` 为了 token 均衡会打乱样本装入不同 micro-batch，concat 后顺序与输入不同，必须用记录的 indices 构造反向映射还原，否则 log_prob 会与 advantages 等字段错位。

---

### 4.4 update_policy：mini → micro 的梯度累积完整更新

#### 4.4.1 概念说明

`update_policy` 是 Actor 的「主战场」：它接收一个已经包含 `old_log_probs`、`advantages`（必要时还有 `ref_log_prob`）的 `DataProto`，完成一次完整的策略梯度更新，并返回训练指标（loss、clipfrac、grad_norm 等）。

它要解决的核心工程问题是：**一个 batch 太大，显存放不下整批的前向+反向**。于是采用两层切分：

- **mini-batch**：把整批数据切成若干份，每份 `ppo_mini_batch_size` 条。每个 mini-batch 对应一次 `optimizer.step()`。
- **micro-batch**：再把每个 mini-batch 切成更小的 `ppo_micro_batch_size` 条，每条做一次前向+反向，梯度累积。

> 名词辨析：很多人会把「mini-batch」和「micro-batch」混用。在 veRL 的命名约定里，**mini-batch 是「一次 optimizer.step 处理的单位」**，**micro-batch 是「一次前向+反向的单位」**。mini-batch 由若干 micro-batch 累积而成。

#### 4.4.2 核心流程

一次 `update_policy(data)` 的完整流程（伪代码）：

```text
update_policy(data):
  actor_module.train()
  gradient_accumulation = ppo_mini_batch_size // ppo_micro_batch_size   # 例如 256//64 = 4
  temperature = data.meta_info['temperature']
  batch = data.select([...old_log_probs, advantages, (ref_log_prob) if use_kl_loss])

  dataloader = batch.split(ppo_mini_batch_size)        # 第 1 层：mini-batch 迭代器
  for mini_batch in dataloader:
      micro_batches = mini_batch.split(ppo_micro_batch_size)   # 第 2 层：micro-batch
      actor_optimizer.zero_grad()

      for micro in micro_batches:
          micro = micro.cuda()
          response_mask = attention_mask[:, -response_length:]      # response 段的有效位
          entropy, log_prob = _forward_micro_batch(micro, temperature)   # 当前策略 π_θ

          # —— 拼装 loss ——
          pg_loss, pg_clipfrac, ppo_kl = compute_policy_loss(
              old_log_prob, log_prob, advantages, eos_mask=response_mask, cliprange=clip_ratio)
          entropy_loss = masked_mean(entropy, response_mask)
          policy_loss  = pg_loss - entropy_loss * entropy_coeff           # PPO 主项 + 熵正则
          if use_kl_loss:                                                 # GRPO 路线
              kld      = kl_penalty(log_prob, ref_log_prob, kl_loss_type)
              kl_loss  = masked_mean(kld, response_mask)
              policy_loss = policy_loss - kl_loss * kl_loss_coef

          loss = policy_loss / gradient_accumulation     # ★ 除以累积步数
          loss.backward()                                # 梯度累加进 .grad

      grad_norm = _optimizer_step()        # clip_grad_norm_ + optimizer.step()
  actor_optimizer.zero_grad()
  return metrics
```

**gradient_accumulation 用于 loss 缩放** 的原理：

设一个 mini-batch 有 \(G\) 个 micro-batch。我们希望「累积 \(G\) 次反向后的梯度」等价于「把整个 mini-batch 当一次平均 loss 算出的梯度」，即

\[ \text{目标梯度} = \nabla_\theta \left( \frac{1}{G}\sum_{i=1}^{G} \ell_i \right) = \frac{1}{G}\sum_{i=1}^{G}\nabla_\theta \ell_i \]

而梯度累积做的是 \(\sum_i \nabla_\theta (\ell_i / G) = \frac{1}{G}\sum_i \nabla_\theta \ell_i\)，二者相等。所以每个 micro-batch 的 loss 必须除以 \(G = \text{gradient\_accumulation}\)。源码里这一步就是 [dp_actor.py:L271](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L271) 的 `loss = policy_loss / self.gradient_accumulation`。

> 注意一个易错点：`zero_grad()` 是**每个 mini-batch 开头**调用一次（[dp_actor.py:L231](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L231)），而不是每个 micro-batch 调用——否则就失去累积意义了。

关于 **`ppo_epochs`**：你会在配置里看到 `ppo_epochs: 1`（[ppo_trainer.yaml:L33](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L33)）。需要澄清：在 **FSDP 后端**的 `update_policy` 里，**并没有外层 epochs 循环**——`dataloader = batch.split(ppo_mini_batch_size)` 只遍历一遍，即单 epoch。`ppo_epochs` 在 FSDP 路径下仅用于 MFU（算力利用率）估算（[fsdp_workers.py:L379](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L379)）。真正按 epoch 反复更新的是 Megatron 后端（`megatron_actor.py` 的 `epochs=self.config.ppo_epochs`），TinyZero 默认走 FSDP，所以实际是单 epoch。读源码时不要被配置项误导。

#### 4.4.3 源码精读

完整方法在 [dp_actor.py:L203-L287](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L203-L287)。逐段拆解。

**(1) 准备与梯度累积系数**（[dp_actor.py:L203-L218](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L203-L218)）：

```python
def update_policy(self, data: DataProto):
    self.actor_module.train()
    assert self.config.ppo_mini_batch_size % self.config.ppo_micro_batch_size == 0
    self.gradient_accumulation = self.config.ppo_mini_batch_size // self.config.ppo_micro_batch_size
    temperature = data.meta_info['temperature']
    select_keys = ['responses', 'input_ids', 'attention_mask', 'position_ids', 'old_log_probs', 'advantages']
    if self.config.use_kl_loss:
        select_keys.append('ref_log_prob')
    batch = data.select(batch_keys=select_keys).batch
    dataloader = batch.split(self.config.ppo_mini_batch_size)
```

中文说明：

- `assert mini % micro == 0` 保证 mini-batch 能被 micro-batch 整除，否则切分会有零头。
- `use_kl_loss=True`（GRPO 路线，见 u5-l5）时才把 `ref_log_prob` 选进来；否则不需要参考策略的 log_prob，少搬一列数据。
- `batch.split(ppo_mini_batch_size)` 得到 mini-batch 列表。

**(2) 双层循环 + loss 拼装**（[dp_actor.py:L220-L272](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L220-L272)）：

```python
for batch_idx, data in enumerate(dataloader):
    mini_batch = data
    if self.config.use_dynamic_bsz:
        max_token_len = self.config.ppo_max_token_len_per_gpu * self.ulysses_sequence_parallel_size
        micro_batches, _ = rearrange_micro_batches(batch=mini_batch, max_token_len=max_token_len)
    else:
        micro_batches = mini_batch.split(self.config.ppo_micro_batch_size)

    self.actor_optimizer.zero_grad()

    for data in micro_batches:
        data = data.cuda()  # actor device is cpu when using offload
        ...
        response_mask = attention_mask[:, -response_length:]
        ...
        entropy, log_prob = self._forward_micro_batch(micro_batch=data, temperature=temperature)

        pg_loss, pg_clipfrac, ppo_kl = core_algos.compute_policy_loss(
            old_log_prob=old_log_prob, log_prob=log_prob, advantages=advantages,
            eos_mask=response_mask, cliprange=clip_ratio)
        entropy_loss = verl_F.masked_mean(entropy, response_mask)

        policy_loss = pg_loss - entropy_loss * entropy_coeff

        if self.config.use_kl_loss:
            ref_log_prob = data['ref_log_prob']
            kld = core_algos.kl_penalty(logprob=log_prob, ref_logprob=ref_log_prob,
                                        kl_penalty=self.config.kl_loss_type)
            kl_loss = masked_mean(kld, response_mask)
            policy_loss = policy_loss - kl_loss * self.config.kl_loss_coef
            metrics['actor/kl_loss'] = kl_loss.detach().item()
            metrics['actor/kl_coef'] = self.config.kl_loss_coef

        loss = policy_loss / self.gradient_accumulation
        loss.backward()
```

中文说明：

- `response_mask = attention_mask[:, -response_length:]`：从整段 attention_mask 末尾切出 response 段，作为 `eos_mask` 传给 `compute_policy_loss`，只在有效 response token 上算 loss（pad 位置不计）。
- **policy_loss 的组合**正是本讲核心：

\[ \text{policy\_loss} = \underbrace{\text{pg\_loss}}_{\text{PPO 裁剪后策略损失}} - \underbrace{\text{entropy\_coeff} \cdot \text{entropy\_loss}}_{\text{熵正则（鼓励探索）}} \quad [ - \underbrace{\text{kl\_loss\_coef} \cdot \text{kl\_loss}}_{\text{仅 GRPO}} ] \]

  - `pg_loss` 来自 [core_algos.py:L163-L194](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/core_algos.py#L163-L194)（u5-l2 已详述）：`ratio = exp(log_prob - old_log_prob)`，双侧裁剪后取 `masked_mean(torch.max(pg_losses, pg_losses2))`。
  - `entropy_loss` 鼓励策略保持一定随机性，防止过早坍缩；默认 `entropy_coeff=0.001` 很小，是个温和的探索项。
  - `kl_loss` 仅 `use_kl_loss=True`（GRPO）时出现，默认 `kl_loss_type=low_var_kl`（低方差 KL 估计，见 u5-l4）、`kl_loss_coef=0.001`。注意它是 **从 loss 端加 KL**，与 GAE/PPO 路线「从 reward 端扣 KL」互斥（u5-l4 已说明）。
  - 三项相加后除以 `gradient_accumulation` 再 backward。

**(3) 优化器步进与指标收集**（[dp_actor.py:L282-L286](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L282-L286)）：

```python
grad_norm = self._optimizer_step()
data = {'actor/grad_norm': grad_norm.detach().item()}
append_to_dict(metrics, data)
```

`_optimizer_step` 在 [dp_actor.py:L143-L151](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L143-L151)：先按 `grad_clip`（默认 1.0）裁剪梯度（FSDP 用 `actor_module.clip_grad_norm_`，否则用普通 `clip_grad_norm_`），再 `optimizer.step()`，并返回裁剪前的 grad_norm 供监控。注意它在 mini-batch 循环**内**，即每个 mini-batch 更新一次参数。

**相关默认配置**（[ppo_trainer.yaml:L21-L35](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L21-L35)）：

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `ppo_mini_batch_size` | 256 | 一次 optimizer.step 处理的样本数 |
| `ppo_micro_batch_size` | 64 | 一次前向+反向的样本数 |
| `grad_clip` | 1.0 | 梯度范数裁剪上限 |
| `clip_ratio` | 0.2 | PPO ratio 双侧裁剪范围 ε |
| `entropy_coeff` | 0.001 | 熵正则系数 |
| `use_kl_loss` | False | True 表示 GRPO（loss 端加 KL） |
| `kl_loss_coef` | 0.001 | KL loss 系数 |
| `kl_loss_type` | low_var_kl | KL 估计方式 |
| `use_dynamic_bsz` | False | 是否按 token 预算动态切 micro-batch |
| `ppo_max_token_len_per_gpu` | 16384 | 动态 batching 时每 GPU 的 token 预算 |

#### 4.4.4 代码实践

**实践目标**：追踪 `policy_loss = pg_loss - entropy_loss*entropy_coeff (- kl_loss*kl_loss_coef)` 的拼装，并定量理解 `gradient_accumulation` 的缩放作用。这是本讲的核心实践。

**操作步骤**：

1. **定位公式**。在 [dp_actor.py:L248-L267](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L248-L267) 依次找到：
   - `pg_loss` 由 `compute_policy_loss` 算出（传 `old_log_prob`、`log_prob`、`advantages`、`eos_mask=response_mask`、`cliprange=clip_ratio`）；
   - `entropy_loss = masked_mean(entropy, response_mask)`；
   - `policy_loss = pg_loss - entropy_loss * entropy_coeff`；
   - `use_kl_loss` 分支里 `policy_loss = policy_loss - kl_loss * self.config.kl_loss_coef`。
2. **追踪 gradient_accumulation**。在 [dp_actor.py:L207-L208](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L207-L208) 读到 `gradient_accumulation = ppo_mini_batch_size // ppo_micro_batch_size`；默认配置下为 \(256 / 64 = 4\)。
3. **手算验证**。假设默认配置（mini=256, micro=64, dp=单卡），一个 rank-local batch 有 256 条样本：
   - mini-batch 个数 = \(256 / 256 = 1\)；
   - 该 mini-batch 内 micro-batch 个数 = \(256 / 64 = 4\)，即 `gradient_accumulation = 4`；
   - 4 个 micro-batch 各算一次 `loss = policy_loss / 4` 再 `backward()`，梯度累加；最后 `_optimizer_step()` 更新一次。
   - 等价于：\(\nabla \left(\frac{1}{4}\sum_{i=1}^{4}\ell_i\right)\)，即整个 mini-batch 的平均梯度。
4. **改参数观察**：把 `ppo_micro_batch_size` 从 64 调到 32（显存更省），则 `gradient_accumulation` 变成 8，micro-batch 个数翻倍，单步训练更慢但每步显存占用减半——这是 OOM 时常用的调参方向。

**需要观察的现象**：`gradient_accumulation` 随 `ppo_micro_batch_size` 反比例变化；无论怎么切，一次 `optimizer.step()` 对应的有效梯度（理论期望）不变。

**预期结果**：你能口头复述「mini-batch 决定 step 频率、micro-batch 决定单次显存、gradient_accumulation 是两者的比值并用于 loss 归一」这三句话。

> 待本地验证：若有 GPU，可在 e2e 测试 `tests/e2e/arithmetic_sequence` 里把 `ppo_micro_batch_size` 改小，观察训练是否仍能收敛（loss 曲线应与原配置基本一致，只是变慢）。

#### 4.4.5 小练习与答案

**练习 1**：默认配置 `ppo_mini_batch_size=256, ppo_micro_batch_size=64` 下，一次 `optimizer.step()` 实际更新了多少条样本的梯度？做了几次 `backward()`？
**参考答案**：更新了 256 条样本的平均梯度（一个 mini-batch）；期间做了 \(256/64=4\) 次 `backward()`（每个 micro-batch 一次），梯度累积，最后一次 `optimizer.step()`。

**练习 2**：如果忘了写 `loss = policy_loss / self.gradient_accumulation`（即不除以累积步数），训练会出什么问题？
**参考答案**：梯度会变成 \(\sum_i \nabla \ell_i\) 而非 \(\frac{1}{G}\sum_i \nabla \ell_i\)，等效学习率被放大了 \(G\) 倍（默认 4 倍），梯度幅度也随 micro-batch 数变化而不稳定，容易导致训练发散或对 `ppo_micro_batch_size` 过度敏感。

**练习 3**：为什么 `zero_grad()` 放在 mini-batch 循环开头，而不是放在最外层只调一次？
**参考答案**：因为每个 mini-batch 对应一次独立的 `optimizer.step()`，需要清掉上一轮的梯度再重新累积；若只调一次，第二个 mini-batch 的梯度会叠到第一个 mini-batch 残留的梯度上，更新就错了。

---

## 5. 综合实践

把本讲三块知识串起来，做一次「**给 `update_policy` 加一行调试日志，并解释你看到的数字**」的端到端源码阅读实践。

**任务**：假设训练出现异常（actor/pg_loss 不下降），你想确认前向与 loss 拼装是否正确。请完成：

1. **前向链路核对**（对应 4.2、4.3）：在 `_forward_micro_batch` 的 naive 分支返回前，临时打印一个 micro-batch 的 `log_probs[:, 0]`（response 第一个 token 的对数概率）与对应 `old_log_prob[:, 0]`。在 `update_policy` 第一步（策略尚未更新时），二者应几乎相等（ratio \(\approx 1\)）。若差距大，说明 temperature 或前向数值有问题。
   - 标注为「示例代码」，不要提交到真实训练：
     ```python
     # 示例代码：仅用于调试，验证 ratio 初始≈1
     print("log_prob[:3,0]", log_prob[:3, 0].detach().tolist())
     print("old_log_prob[:3,0]", old_log_prob[:3, 0].detach().tolist())
     print("ratio[:3,0]", torch.exp(log_prob[:3,0]-old_log_prob[:3,0]).detach().tolist())
     ```

2. **loss 组合核对**（对应 4.4）：在 [dp_actor.py:L257](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L257) 附近，打印 `pg_loss`、`entropy_loss`、`policy_loss` 三者的数值，确认符号关系：`policy_loss = pg_loss - entropy_loss*0.001`。注意 `entropy_loss ≥ 0`，所以熵正则在**减小** policy_loss（因为我们在最小化 loss，减小 loss 等价于增大熵，即鼓励探索）。

3. **梯度累积核对**（对应 4.4）：打印每个 micro-batch 的 `loss`（已除以 `gradient_accumulation`）和 `_optimizer_step` 返回的 `grad_norm`，确认每个 mini-batch 只 step 一次、grad_norm 在合理量级（被 `grad_clip=1.0` 裁剪前后的差异）。

4. **解释观察**：用一句话回答——为什么 `update_policy` 第一步 ratio 应接近 1，而随着 mini-batch 内多次 micro-batch 反传后 ratio 会逐渐偏离 1？

**预期结论**：第一步 ratio≈1 是因为 `old_log_prob` 由同一份 \(\theta\) 用同一个 `_forward_micro_batch` 重算（u6-l1 的 recompute）；随着参数被更新，当前 `log_prob` 偏离 `old_log_prob`，ratio 偏离 1，这正是 PPO clip 机制要约束的对象——当 ratio 超出 \([1-\epsilon, 1+\epsilon]\)（默认 \([0.8, 1.2]\)）时被裁剪，`pg_clipfrac` 随之上升。

> 本实践为「源码阅读 + 局部加日志」型，不需要跑完整 3B 训练；若要真跑，可用 `tests/e2e/arithmetic_sequence`（见 u7-l5）做最小化验证。

## 6. 本讲小结

- `DataParallelPPOActor` 有三个核心方法：只读的 `compute_log_prob`、底层前向 `_forward_micro_batch`、训练更新 `update_policy`；同一个类在「不传 optimizer」时复用为冻结的参考策略。
- `_forward_micro_batch` 用切片 `[:, -response_length-1:-1]` 实现 next-token 的 shift 对齐，`logits.div_(temperature)` 做温度缩放，返回 `(entropy, log_probs)`；`use_remove_padding` 配合 `flash_attn_varlen` 跳过 padding 计算，Ulysses SP 进一步在卡间切分长序列。
- `compute_log_prob` 是 `eval()+no_grad()` 的只读前向，专用于 recompute `old_log_probs`（保证 PPO ratio 起点≈1）；temperature 必须显式从 `meta_info` 取以防静默错误。
- `update_policy` 采用 **mini-batch（决定 step 频率）→ micro-batch（决定单次显存）→ 梯度累积** 的三层结构，`gradient_accumulation = ppo_mini_batch_size // ppo_micro_batch_size` 用于 loss 归一，使累积梯度等价于整 mini-batch 的平均梯度。
- 核心 loss 组合为 `policy_loss = pg_loss - entropy_loss*entropy_coeff`，GRPO 路线再 `- kl_loss*kl_loss_coef`（loss 端 KL，与 reward 端 KL penalty 互斥）。
- FSDP 后端的 `update_policy` 是单 epoch（`dataloader = batch.split(mini)` 只遍历一遍）；`ppo_epochs` 配置在此路径仅用于 MFU 估算，真正按 epoch 循环的是 Megatron 后端。

## 7. 下一步学习建议

- **横向对照 Critic**：本讲讲了 Actor 的前向与更新，下一步读 **u6-l3 Critic 价值估计与更新**，重点对比 `dp_critic._forward_micro_batch` 与本讲的异同——两者都用 `[-response_length-1:-1]` 切 response 段，但 critic 不需要 ratio/clip，loss 是 clipped MSE。
- **回到调度层**：若想搞清 `update_actor` 的数据是怎么从 driver 切到各 rank、metrics 怎么收回去，复习 **u3-l3 Dispatch 装饰器**（`update_actor` 标的是 `DP_COMPUTE_PROTO`）。
- **深入算法细节**：`pg_loss` 内部的 ratio、双侧裁剪、masked_mean 在 **u5-l2 策略损失** 里已详述；KL 估计的四种变体（kl/abs/mse/low_var_kl）在 **u5-l4** 详述，可对照本讲 `kl_penalty` 调用处理解 loss 端 KL 的来龙去脉。
- **显存与并行调优**：`use_remove_padding`、Ulysses SP、`use_dynamic_bsz`、梯度累积这些显存优化手段在 **u7-l1 FSDP 并行策略** 与 **u7-l2 序列长度均衡** 中有更系统的讨论。
