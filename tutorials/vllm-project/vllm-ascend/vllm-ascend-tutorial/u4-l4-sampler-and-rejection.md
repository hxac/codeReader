# 采样器与拒绝采样

## 1. 本讲目标

上一讲（u4-l2）我们走完了 `NPUModelRunner` 的一次前向主链路，终点停在 `_sample`：模型吐出 logits 之后，到底哪个 token 会被「选中」并送回调度器？这一讲就专门回答这个问题。

读完本讲，你应当能够：

- 说清 `AscendSampler` 是如何继承上游 `Sampler` 并在 NPU 上完成贪心 / 随机 / top-k-top-p 采样的，以及它为什么要避开 `torch.multinomial`。
- 解释惩罚项（penalties：repetition / frequency / presence）在 NPU 上如何用 Triton-Ascend 内核批量改写 logits。
- 理解投机解码（speculative decoding）验证阶段的核心算法——拒绝采样（rejection sampling），以及 `AscendRejectionSampler` 如何把 draft（草稿）token 与 target（目标）token 对齐验证、并产出最终输出序列。
- 看懂三种增强验证策略：逐 token 验证、Block Verify（联合验证）、Entropy Verify（基于熵的后验放宽）。
- 学会通过环境变量 / `additional_config` 切换采样路径，并通过 UT 测试验证理解。

本讲涉及的关键词：logits、采样、贪心 / 随机采样、top-k / top-p、惩罚项、投机解码、draft / target、拒绝采样、bonus token、recovered token、reduce-sample、Block Verify、Entropy Verify。

## 2. 前置知识

### 2.1 从 logits 到 token

语言模型最后一层输出的是一个长度等于词表大小（vocabulary size，简称 `V`）的向量，叫做 **logits**。它还不是概率，需要经过 `softmax` 才能变成概率分布。采样的任务就是：给定这 `V` 个分数，挑出一个 token id。

- **贪心采样（greedy）**：直接取 `argmax`，永远选分数最高的那个，确定性强、但容易重复乏味。
- **随机采样（random）**：按概率分布抽签。为了让生成可控，常用 **top-k**（只在概率最高的 k 个里抽）和 **top-p（nucleus）**（只在累积概率达到 p 的最小集合里抽）来截断长尾。
- **温度（temperature）**：`logits / temperature`，温度越低分布越尖（更趋近贪心），温度越高越随机。温度为 0 即等价于贪心。

### 2.2 投机解码与拒绝采样：为什么需要它

普通采样里，每生成一个 token 都要走一次完整的大模型前向（target model），这是吞吐瓶颈。**投机解码（speculative decoding）** 的思路是：用一个很小很快的 **draft model（草稿模型）** 一次性「猜」出接下来的 k 个 token（draft tokens），然后让大模型（target model）**一次前向** 同时验证这 k 个草稿。如果草稿猜对了，就等于一次前向拿到了 k+1 个 token，大幅提速。

关键问题：如何验证？答案就是 **拒绝采样（rejection sampling）**。它保证最终输出的分布与「只用 target model 采样」**完全一致**，从而既加速又不损失质量。其核心规则（推导自「Speculative Decoding」论文）如下：

设草稿模型给出的 token 为 \(d\)，它在草稿分布下的概率为 \(q(d)\)；目标模型在该位置的概率为 \(p(d)\)。对每个草稿位置独立抽一个均匀随机数 \(u \sim \text{Uniform}(0,1)\)：

\[ \text{accept}(d) \iff u < \min\!\left(1,\ \frac{p(d)}{q(d)}\right) \]

- 一旦某个位置被 **拒绝**，该位置及之后所有草稿全部作废。
- 被拒绝的位置需要从「残差分布」中 **恢复（recover）** 一个 token：残差分布正比于 \(\max(0,\ p - q)\)（把草稿模型多给的概率扣掉，再归一化）。
- 如果所有草稿都被接受，则额外奖励一个 **bonus token**（target model 在最后一个位置直接采样得到的 token）。

这就是本讲要讲清的算法，`AscendRejectionSampler` 正是它在 NPU 上的实现。

### 2.3 张量并行（TP）下的采样特殊性

在 u4-l1 我们提过 TP：词表维度的 logits 会被切成多份，每张卡只拿到 `V/tp_size` 列。这给采样带来麻烦——`argmax` 和 `top-k` 都需要在 **全局词表** 上比较。vllm-ascend 为此提供了 `enable_reduce_sample`（降采样）路径：先在本地做 top-k，再用 HCCL `all_gather` 把各卡候选拼起来，最后在拼接后的小集合上完成采样。这条路径贯穿本讲两个采样器，请记住它。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [vllm_ascend/sample/sampler.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/sampler.py) | 普通采样器 `AscendSampler` 与 top-k/top-p 实现，是非投机解码路径的采样入口。 |
| [vllm_ascend/sample/penalties.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/penalties.py) | 惩罚项封装 `apply_all_penalties`，把 logits 喂给 Triton-Ascend 内核。 |
| [vllm_ascend/sample/rejection_sampler.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py) | 投机解码验证器 `AscendRejectionSampler`，本讲重头戏。 |
| [vllm_ascend/ops/triton/penalty.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/penalty.py) | 惩罚项的 Triton-Ascend 内核 `apply_all_penalties_kernel`。 |
| [vllm_ascend/ops/triton/reject_sample.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/reject_sample.py) | 拒绝采样的高性能 Triton 内核（greedy / random / block-verify 三套）。 |
| [vllm_ascend/worker/model_runner_v1.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/model_runner_v1.py) | `_sample` 方法：根据是否开启投机解码，分流到 `AscendSampler` 或 `AscendRejectionSampler`。 |
| [vllm_ascend/ascend_config.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py) | `enable_reduce_sample` 与 `RejectionSamplerConfig`（block_verify / entropy_verify 等）的配置入口。 |

> 提示：三个核心文件都在 `vllm_ascend/sample/` 目录下，该目录只有一个空 `__init__.py` 和这三个 `.py` 文件，职责非常内聚。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 采样器**、**4.2 惩罚项**、**4.3 拒绝采样**。前两个服务于普通（非投机）采样，第三个服务于投机解码验证。三者共享同一条「TP 降采样」骨架。

### 4.1 采样器：AscendSampler

#### 4.1.1 概念说明

`AscendSampler` 是 vllm-ascend 对上游 `vllm.v1.sample.sampler.Sampler` 的继承重写（参见 u3 讲过的「继承 + 最小改写」模式）。它的职责很单一：吃进 logits 与 `SamplingMetadata`，吐出 `SamplerOutput`（含 `sampled_token_ids` 和可选 logprobs）。

它之所以要重写而非直接复用，原因有三：

1. **避免 `torch.multinomial`**：上游随机采样用的是 `torch.multinomial`，但它在 NPU 上会触发 CPU-NPU 同步（device→host 阻塞），严重拖慢流水。`AscendSampler` 改用指数分布 + `argmax` 的等价做法（下文详解）。
2. **TP 降采样**：在 `enable_reduce_sample` 下，贪心与 top-k/top-p 都要走 all-gather。
3. **NPU 原生 top-k-top-p 算子**：A2/A3 卡上有 `torch_npu.npu_top_k_top_p`，比纯 PyTorch 实现快。

#### 4.1.2 核心流程

普通采样的主流程（`_sample` 非投机分支）：

```
model_runner._sample(logits)
  └─ self.sampler.prepare_sampling(max_topk)   # 仅 reduce_sample 时，缓存全局 top_k
  └─ self.sampler(logits, sampling_metadata)    # 上游 Sampler.__call__
        ├─ apply_penalties(logits, ...)          # 惩罚项（4.2）
        ├─ forward_native / forward             # top-k-top-p + softmax + 随机采样
        └─ return SamplerOutput
```

`Sampler.__call__`（上游）内部会依次调用 `apply_penalties`、`apply_top_k_top_p`、`random_sample`，而这三步都被 `AscendSampler` 通过重写 / 注入替换成了 NPU 版本。

随机采样的关键技巧——**用指数分布替代 multinomial**：

```python
# sampler.py:20-43
def random_sample(probs, generators):
    """Randomly sample from the probabilities.
    We use this function instead of torch.multinomial because torch.multinomial
    causes CPU-NPU synchronization.
    """
    with npu_stream_switch(global_stream()):
        q = torch.empty_like(probs)
        if len(generators) != probs.shape[0]:
            q.exponential_()
        if generators:
            for i, generator in generators.items():
                q[i].exponential_(generator=generator)
    torch.npu.current_stream().wait_stream(global_stream())
    return probs.div_(q).argmax(dim=-1).view(-1)
```

> 这段代码做了什么：抽样 \(q \sim \text{Exp}(1)\)（指数分布），然后返回 `argmax(probs / q)`。数学上，`argmax(p_i / q_i)` 等价于按概率 `p` 做分类抽样（Gumbel-max 技巧的指数版）。这样既全程在 NPU 上算、又避开了 `multinomial` 的同步。代码还把随机数生成放到独立的 `global_stream()`（全局辅助流）上，再让默认流 `wait_stream`，实现异步。

#### 4.1.3 源码精读

`AscendSampler` 的类定义与构造：

```python
# sampler.py:46-81
class AscendSampler(Sampler):
    @staticmethod
    def apply_penalties(logits, sampling_metadata, output_token_ids):
        """Use Triton-Ascend penalties on NPU when Triton is available; else vLLM default."""
        if not HAS_TRITON:
            ...  # 回退到上游 Sampler.apply_penalties
            return Sampler.apply_penalties(logits, sampling_metadata, output_token_ids)
        if sampling_metadata.no_penalties:
            return logits
        return apply_all_penalties(...)  # 见 4.2

    def __init__(self, logprobs_mode=DEFAULT_LOGPROBS_MODE):
        super().__init__(logprobs_mode=logprobs_mode)
        self.topk_topp_sampler = AscendTopKTopPSampler(logprobs_mode=logprobs_mode)
        ...
```

> 这段代码做了什么：`AscendSampler` 只重写了两个点——`apply_penalties`（路由到 Triton-Ascend 惩罚内核）和 `topk_topp_sampler`（换成 `AscendTopKTopPSampler`），其余流程全部沿用父类 `Sampler`。这是典型的「最小改写」。

贪心采样在 TP 降采样下的特殊处理：

```python
# sampler.py:86-106
@staticmethod
def greedy_sample(logits):
    if get_ascend_config().enable_reduce_sample:
        tp_group = get_tp_group()
        B, V_local = logits.shape
        rank = tp_group.rank_in_group
        local_max_logits, local_max_indices = logits.max(dim=-1)
        local_global_idx = local_max_indices + rank * V_local        # 本地索引 → 全局索引
        gathered_logits = tp_group.all_gather(local_max_logits.unsqueeze(-1), dim=-1)
        gathered_global_idx = tp_group.all_gather(local_global_idx.unsqueeze(-1), dim=-1)
        global_max_rank = gathered_logits.argmax(dim=-1)
        target_argmax = gathered_global_idx.gather(dim=-1, index=global_max_rank.unsqueeze(-1)).squeeze(-1)
        return target_argmax
    else:
        return logits.argmax(dim=-1).view(-1)
```

> 这段代码做了什么：开启降采样时，每张卡先在本地 `V_local` 列上找最大值，把局部索引加上 `rank * V_local` 还原成 **全局词表索引**，再 all-gather 到 `[B, world_size]`，最后在所有卡的最大值里挑最大的并取出对应全局索引。这就是「分布式 argmax」。没开降采样时直接本地 argmax。

top-k/top-p 的实现有两套，按芯片型号二选一：

```python
# sampler.py:266-270
apply_top_k_top_p = (
    _apply_top_k_top_p_torch_npu
    if get_ascend_device_type() in [AscendDeviceType.A2, AscendDeviceType.A3]
    else _apply_top_k_top_p_pytorch
)
```

> 这段代码做了什么：模块加载时根据设备类型决定 `apply_top_k_top_p` 指向哪个函数。A2/A3 用 `torch_npu.npu_top_k_top_p` 原生算子（见 [sampler.py:236-263](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/sampler.py#L236-L263)），其他芯片（如 310P）走纯 PyTorch 实现（见 [sampler.py:161-233](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/sampler.py#L161-L233)）。后者在降采样路径里同样用 all-gather 拼接各卡 top-k 候选。

#### 4.1.4 代码实践

**实践目标**：理解普通采样路径在 `model_runner_v1.py` 里的接入点，以及 `enable_reduce_sample` 开关如何改变 top-k 行为。

**操作步骤**：

1. 打开 [vllm_ascend/worker/model_runner_v1.py:2357-2388](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/model_runner_v1.py#L2357-L2388) 的 `_sample` 方法，找到非投机分支：

   ```python
   if self.input_batch.sampling_metadata.top_k is not None and get_ascend_config().enable_reduce_sample:
       max_topk = self.input_batch.top_k_cpu[...].max()
       self.sampler.prepare_sampling(max_topk)
   return self.sampler(logits=logits, sampling_metadata=sampling_metadata)
   ```

2. 追踪 `prepare_sampling` 到 [sampler.py:115-119](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/sampler.py#L115-L119)，它把「全 batch 最大的 top_k」缓存到 `self.top_k`，供降采样路径决定本地取多少候选。

3. 在配置里加上 `enable_reduce_sample: true`（见 [ascend_config.py:285](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ascend_config.py#L285)），观察 `AscendTopKTopPSampler.forward_native` 走 [sampler.py:132-148](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/sampler.py#L132-L148) 的降采样分支（返回 `(next_token, logits_to_return)`），而非默认分支。

**需要观察的现象**：开启后采样返回的 token id 是「全局词表索引」（可能大于 `V_local`）；未开启时只可能是本地分片内的索引，需要后续 all-gather 才能拼成全局 id。

**预期结果**：能说清「`prepare_sampling` 把整批最大的 top_k 提前算好，避免每个 token 重复 all-gather 全词表」这一优化动机。

> 说明：本实践为源码阅读型，无需 NPU 即可完成；如需实际运行，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `random_sample` 用 `probs.div_(q).argmax()` 而不是 `torch.multinomial`？

> **答案**：`torch.multinomial` 在 NPU 上会触发 device→host 同步（CPU-NPU synchronization），阻塞流水线。用指数分布抽样 \(q\) 后取 `argmax(probs/q)` 是数学等价的分类抽样（Gumbel-max 技巧的指数形式），全程在 NPU 上完成，且可放在独立流上异步执行。

**练习 2**：`greedy_sample` 在降采样路径里，`local_global_idx = local_max_indices + rank * V_local` 这一步的 `rank` 和 `V_local` 分别是什么？

> **答案**：`rank` 是当前 worker 在 TP 组内的卡号（`tp_group.rank_in_group`），`V_local` 是本卡分到的词表列数（`V / tp_size`）。把本地相对索引加上 `rank * V_local` 是为了把「分片内的索引」还原成「全局词表索引」，这样 all-gather 后才能在全局空间上比较。

---

### 4.2 惩罚项：Penalties（Triton-Ascend 实现）

#### 4.2.1 概念说明

采样前，用户往往希望对生成内容施加约束，vLLM 提供三种惩罚：

- **repetition penalty（重复惩罚）**：对已经出现过的 token，根据 logits 正负缩放，抑制重复。
- **frequency penalty（频率惩罚）**：按 token 在输出中出现的次数，线性降低其 logits（出现越多越被压）。
- **presence penalty（存在惩罚）**：只要 token 出现过（不论几次），固定降低其 logits。

这三项都是在 logits 上做就地（in-place）修改。上游 vLLM 的实现是为 GPU / CPU 写的，vllm-ascend 在有 Triton-Ascend 时改用自己的内核以获得更好的 NPU 性能；没有 Triton 时回退到上游实现（带性能告警）。

#### 4.2.2 核心流程

```
AscendSampler.apply_penalties(logits, ...)
  └─ apply_all_penalties(...)                    # penalties.py
        ├─ _convert_to_tensors(output_token_ids) # list[list[int]] → pad 成张量
        └─ apply_penalties_triton(...)           # triton/penalty.py
              ├─ get_token_bin_counts_and_mask_triton(prompt)   # prompt 的 one-hot 计数
              ├─ get_token_bin_counts_and_mask_triton(output)   # output 的计数
              └─ apply_all_penalties_kernel[grid](...)          # 就地改写 logits
```

惩罚的核心数学（逐 token、逐词位）：

\[ \text{logit}'_i = \text{logit}_i \times \begin{cases} 1/r & \text{logit}_i > 0 \\ r & \text{logit}_i \le 0 \end{cases} - f \cdot c_i - p \cdot \mathbb{1}[c_i > 0] \]

其中 \(r/f/p\) 是 repetition / frequency / presence 惩罚系数，\(c_i\) 是 token \(i\) 在输出里出现的次数；repetition 项的判断条件还包含 prompt 中是否出现过。

#### 4.2.3 源码精读

Python 侧封装非常薄，主要做张量转换：

```python
# penalties.py:25-45
def apply_all_penalties(logits, prompt_token_ids, presence_penalties,
                        frequency_penalties, repetition_penalties, output_token_ids):
    """Apply penalties to logits via Triton-Ascend."""
    _, vocab_size = logits.shape
    output_tokens_t = _convert_to_tensors(output_token_ids, vocab_size, logits.device)
    output_tokens_t.masked_fill_(output_tokens_t == -1, vocab_size)
    return apply_penalties_triton(
        logits, prompt_token_ids, output_tokens_t,
        presence_penalties, frequency_penalties, repetition_penalties,
    )
```

> 这段代码做了什么：把 `output_token_ids`（变长 list）用 `make_tensor_with_pad` 补齐成 `[num_seqs, max_len]` 张量（pad 值为 `vocab_size`，越界即「不是真实 token」），再交给 Triton 内核。

Triton 内核的关键片段（每个 program 处理若干条序列，每条序列内分块遍历词表）：

```python
# ops/triton/penalty.py:59-93
for seq_idx in range(start_seq, end_seq):
    repetition_penalty = tl.load(repetition_penalties_ptr + seq_idx)
    frequency_penalty  = tl.load(frequency_penalties_ptr + seq_idx)
    presence_penalty   = tl.load(presence_penalties_ptr + seq_idx)
    for vocab_start in range(0, vocab_size, BLOCK_SIZE):
        ...
        need_repetition_penalty = (prompt_mask_val | output_mask_val).to(tl.int1)
        penalty_factor = tl.where(need_repetition_penalty, repetition_penalty, 1.0)
        scaling = tl.where((logits > 0.0).to(tl.int1), 1.0 / penalty_factor, penalty_factor)
        updated = logits * scaling
        updated -= frequency_penalty * output_bin_counts
        updated -= presence_penalty * output_mask_val.to(tl.float32)
        tl.store(logits_ptr + logits_offset, updated, mask=mask)
```

> 这段代码做了什么：对每个词位，先用 `prompt_mask | output_mask` 判断该 token 是否在 prompt 或输出中出现过，据此决定是否施加 repetition 缩放（正 logits 除以因子、负 logits 乘以因子，从而压低已出现 token）；再减去 frequency（按出现次数 `output_bin_counts` 线性）和 presence（出现即减）两项。结果就地写回 logits。`grid = (min(num_seqs, get_vectorcore_num()), 1, 1)`，按 NPU 向量核数切分批次。

`AscendSampler.apply_penalties` 的回退逻辑（[sampler.py:53-59](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/sampler.py#L53-L59)）：无 Triton 时打 `warning_once` 并调 `Sampler.apply_penalties`（上游默认实现）。这是个常见的「有能力就走快路径，否则安全回退」模式。

#### 4.2.4 代码实践

**实践目标**：理解三种惩罚对 logits 的不同作用方式。

**操作步骤**：

1. 阅读 [ops/triton/penalty.py:96-123](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/penalty.py#L96-L123) 的 `apply_penalties_triton`，注意它先调两次 `get_token_bin_counts_and_mask_triton` 分别对 prompt 和 output 计数。
2. 在内核 `apply_all_penalties_kernel` 中定位三处 `tl.store` 前的计算（[penalty.py:82-93](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/penalty.py#L82-L93)），分别对应 repetition、frequency、presence。
3. 设想一个 token 在输出里出现了 3 次，回答：repetition 会因为它出现 3 次而多扣吗？frequency 会吗？presence 会吗？

**需要观察的现象**：repetition 只看「是否出现过」（mask），与次数无关；frequency 与次数成正比；presence 也只看「是否出现过」，但是固定值而非比例缩放。

**预期结果**：能复述三者差异——repetition 是比例缩放（按 logits 正负），frequency 是按次数线性扣减，presence 是出现即固定扣减。

> 说明：源码阅读型实践，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `apply_all_penalties` 里要把 pad 值设成 `vocab_size`？

> **答案**：变长序列补齐时，pad 位不是真实 token。把 pad 值设为 `vocab_size`（一个合法词表之外的越界值），后续 `get_token_bin_counts_and_mask_triton` 在统计计数时就会忽略这些位置，不会把 pad 误计入某个真实 token 的频率。

**练习 2**：`AscendSampler.apply_penalties` 在 `sampling_metadata.no_penalties` 为真时直接返回 logits，这一短路有什么好处？

> **答案**：绝大多数请求其实没开任何惩罚，提前短路可以完全跳过 Triton 内核启动与计数开销，避免无谓的 NPU 计算。这是「快速路径」优化。

---

### 4.3 拒绝采样：AscendRejectionSampler

这是本讲的重头戏，也是任务要求讲透的部分。

#### 4.3.1 概念说明

`AscendRejectionSampler` 继承上游 `vllm.v1.sample.rejection_sampler.RejectionSampler`，专门用于投机解码的 **验证（verify）阶段**。它的输入有四块：

- `draft_token_ids`：draft model 猜出的草稿 token，形状 `[num_tokens]`（按请求拍平）。
- `draft_probs`：草稿模型在每个位置给出的概率分布，`[num_tokens, V]`（ngram 投机时为 `None`）。
- `logits`：target model 的输出 logits，`[num_tokens + batch_size, V]`（含 bonus 位）。
- `sampling_metadata`：每个请求的温度、top-k、top-p、是否贪心等。

它的输出是 `[batch_size, max_spec_len + 1]` 的 `output_token_ids`：每一行是被验证接受的 token 序列（含可能的 bonus），未填满的位置用 `PLACEHOLDER_TOKEN_ID`（-1）占位。

核心难点有三个：

1. **对齐**：draft 的 token 与 target 的 logits 必须按位置一一对应，且要在 TP 分片下还原成全局概率。
2. **变长批次**：不同请求的草稿长度不同（`num_draft_tokens`），需要用累积偏移 `cu_num_draft_tokens` 把一维 token 数组映射回二维 `[batch, max_spec_len]` 网格。
3. **拒绝采样的向量化**：朴素实现要逐请求、逐 token 循环；这里要全程向量化（用 mask + argmin 模拟「首个拒绝位」）。

此外，vllm-ascend 还提供两种 **提升接受率** 的增强策略（可配置）：

- **Block Verify**：把整段草稿当作一个整体做联合验证（基于累积乘积），来自 MagicMTP 思路，提升长草稿的接受率。
- **Entropy Verify**：用 target 分布的熵动态放宽接受阈值（基于后验），进一步提升接受率。

#### 4.3.2 核心流程

`AscendRejectionSampler.forward` 的整体编排：

```
forward(metadata, draft_probs, logits, sampling_metadata)
  ├─ 1. 取出 bonus_logits 并采样得到 bonus_token_ids      # 每个请求的「奖励 token」
  ├─ 2. 取出 target_logits，转 fp32，施加 logits_processor # 惩罚项 / allowed_ids / bad_words
  ├─ 3. apply_sampling_constraints(target_logits, ...)     # 温度 + top-k + (可选 all-gather) + top-p
  └─ 4. rejection_sample(...)                              # 真正的拒绝采样（greedy / random / block / entropy）
        ├─ 分配 output_token_ids，填 PLACEHOLDER
        ├─ 若有非随机（贪心）请求：
        │     ├─ greedy_sample(target_logits)              # 分布式 argmax
        │     └─ rejection_greedy_sample_*                 # 草稿 == argmax 则接受
        ├─ 若有随机请求：
        │     ├─ target_probs = softmax(target_logits)
        │     ├─ generate_uniform_probs(...)               # 抽 u ~ Uniform(0,1)
        │     ├─ sample_recovered_tokens(...)              # 预算残差恢复 token
        │     └─ rejection_random_sample_*                 # 按 u < p/q 判接受，否则取恢复 token
        └─ 填 bonus token（全部接受时）
```

拒绝采样的核心判定（随机路径，[rejection_sampler.py:1248-1251](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L1248-L1251)）：

```python
acceptance_condition = (draft_token_probs > zero_threshold) & (
    target_token_probs / draft_token_probs >= uniform_token_probs
)
```

即 2.2 节的公式 \(\;u < p(d)/q(d)\;\) 的代码形态（`>=` 与 `<` 等价翻转）。一旦某位置不满足，之后的位置全部 `should_skip`。

被拒绝位从残差分布恢复（[rejection_sampler.py:1450-1473](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L1450-L1473)）：

\[ \text{residual}_i = \max(0,\ p_i - q_i), \qquad \text{recover} = \arg\max_i \frac{\text{residual}_i}{s_i} \]

其中 \(s\)（代码里的 `q_values`）是按请求预计算的归一化因子（每个请求 `q.exponential_()` 得到，用于把残差转成可比较的分布）。

Block Verify 的联合判定（[rejection_sampler.py:1552-1567](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L1552-L1567)）：

\[ \pi_k = \prod_{j \le k} \min\!\left(1,\ \frac{p(d_j)}{q(d_j)}\right), \qquad \text{legal}_k \iff \pi_k \ge \prod_{j \le k} u_j \]

即把逐位的接受比累积起来，与累积均匀随机数比较，整段一起判。

Entropy Verify 在普通阈值上叠加熵因子（[rejection_sampler.py:1237-1247](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L1237-L1247)）：

\[ h = -\sum_i p_i \log p_i,\quad \tau = \min\!\left(e^{-\alpha h},\ \text{posterior\_threshold}\right),\quad \text{accept} \iff \frac{p(d)}{q(d)} \ge \tau \cdot u \]

熵越低（分布越确定）接受阈值越高、越严格；熵越高越放宽，从而提升接受率。

#### 4.3.3 源码精读

**入口 `forward`——四步编排**（[rejection_sampler.py:150-260](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L150-L260)）。先校验 `max_spec_len <= MAX_SPEC_LEN`（[rejection_sampler.py:181-185](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L181-L185)）。然后用 `bonus_logits_indices` 取出每个请求最后一位的 logits，调内部 `self.sampler` 得到 bonus token：

```python
# rejection_sampler.py:196-208
bonus_logits = logits[bonus_logits_indices]
bonus_sampler_output = self.sampler(
    logits=bonus_logits,
    sampling_metadata=replace(sampling_metadata, max_num_logprobs=-1),
    predict_bonus_token=True,
    logprobs_mode_override="processed_logits" if self.is_processed_logprobs_mode else "raw_logits",
)
bonus_token_ids = bonus_sampler_output.sampled_token_ids
```

> 这段代码做了什么：target model 的 logits 里，每个请求最后多算了一个「bonus 位」（用于全部接受时奖励的 token）。这里把它单独取出来采样，得到 `bonus_token_ids`。注意它复用了 `self.sampler`（即 `AscendSampler`），所以 bonus 也走 NPU 采样路径。

接着取 target logits、施加处理器与采样约束：

```python
# rejection_sampler.py:213-228
raw_target_logits = logits[target_logits_indices].to(torch.float32)
target_logits = raw_target_logits
if not self.is_processed_logprobs_mode:
    target_logits = target_logits.clone()           # 保留原始 logits 供 logprobs 计算
target_logits = self.apply_logits_processors(target_logits, sampling_metadata, metadata)
target_logits = apply_sampling_constraints(
    target_logits, metadata.cu_num_draft_tokens, sampling_metadata, self.top_k)
```

> 这段代码做了什么：`target_logits_indices` 把 target logits 中对应草稿验证位的那些行抽出来（去掉 bonus 行），转 fp32 提精度，施加惩罚 / bad_words / allowed_ids 等 logits 处理器，再做温度缩放与 top-k/top-p。`apply_sampling_constraints` 返回的可能是 `(logits, indices)` 二元组（降采样时带全局候选索引），也可能是单张量（贪心时）。

**核心 `rejection_sample` 函数**（[rejection_sampler.py:416-899](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L416-L899)）。先读配置决定走哪条增强路径：

```python
# rejection_sampler.py:486-500
using_block_verify = max_spec_len >= 3 and bool(
    get_ascend_config().rejection_sampler_config.enable_block_verify)
using_entropy_verify = bool(
    get_ascend_config().rejection_sampler_config.enable_entropy_verify)
...
posterior_threshold = float(get_ascend_config().rejection_sampler_config.posterior_threshold)
posterior_alpha = float(get_ascend_config().rejection_sampler_config.posterior_alpha)
```

> 这段代码做了什么：从 `AscendConfig.rejection_sampler_config` 读四个开关。`block_verify` 额外要求 `max_spec_len >= 3`（短草稿没必要联合验证）。这两个开关决定了下文随机路径走哪个内核。

分配输出并填充占位符：

```python
# rejection_sampler.py:519-524
output_token_ids = torch.empty((batch_size, max_spec_len + 1), dtype=torch.int32, device=device)
output_token_ids.fill_(PLACEHOLDER_TOKEN_ID)
```

**贪心路径**（[rejection_sampler.py:562-610](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L562-L610)）：贪心请求的验证很简单——草稿 token 与 target 的 argmax 相等就接受，否则该位取 target argmax 并停止：

```python
# rejection_sampler.py:562-583
if not sampling_metadata.all_random:
    if get_ascend_config().enable_reduce_sample:
        target_argmax = greedy_sample(target_logits)    # 分布式 argmax（见 4.1）
    else:
        target_argmax = target_logits.argmax(dim=-1).view(-1)
    if HAS_TRITON:
        rejection_greedy_sample_with_triton(...)
    else:
        ...  # PyTorch 回退
```

> 这段代码做了什么：对含贪心请求的批次，先算 target 的 argmax（降采样走 `greedy_sample` 做 all-gather），再调内核逐位比对 `draft == argmax`。全部贪心时（`all_greedy`）验完即返回。

**随机路径——草稿与目标概率的对齐**（[rejection_sampler.py:614-645](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L614-L645)）。这是回答「target 与 draft 如何对齐」的关键：

```python
# rejection_sampler.py:614-621
if target_indices is not None:                          # 降采样：logits 是候选分片
    selected_vocab_size = target_logits.shape[-1]
    global_vocab_size = draft_probs.shape[-1] if draft_probs is not None else selected_vocab_size
    target_probs = target_logits.softmax(dim=-1, dtype=torch.float32)
```

```python
# rejection_sampler.py:625-645
uniform_probs = generate_uniform_probs(num_tokens, num_draft_tokens,
                                       sampling_metadata.generators, device)
recovered_token_ids = sample_recovered_tokens(         # 预算残差恢复 token
    max_spec_len, num_draft_tokens, cu_num_draft_tokens,
    draft_token_ids, draft_probs, target_probs, sampling_metadata, device,
    target_indices=target_indices, global_vocab_size=global_vocab_size,
    enable_reduce_sampling=True)
```

> 这段代码做了什么：
> 1. **对齐**：`draft_token_ids[num_tokens]`、`draft_probs[num_tokens, V]`、`target_logits[num_tokens, ...]` 三个一维拍平的数组共享同一个 `num_tokens` 维度——第 `i` 行就是第 `i` 个草稿位置的草稿 token、草稿概率、目标 logits。它们靠 `SpecDecodeMetadata` 在 `model_runner` 里就构造好了一一对应。
> 2. **降采样下的概率对齐**：开启降采样时，`target_logits` 是 `[num_tokens, top_k*tp_size]` 的候选分片，配 `target_indices`（候选在全局词表中的索引）。验证时需要「在候选里查找草稿 token」才能拿到 `target_prob(draft)`——这正是 `rejection_random_sample_pytorch` 里 `is_in_candidates = target_indices == draft` 的作用。
> 3. **抽均匀随机数**：`generate_uniform_probs` 为每个草稿位置生成 `u`。
> 4. **预算恢复 token**：`sample_recovered_tokens` 先算好每个位置一旦被拒绝要用的恢复 token，存进 `recovered_token_ids`。

随后按是否开 block_verify 选择内核（[rejection_sampler.py:647-753](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L647-L753)）：

```python
# rejection_sampler.py:649-677（节选）
if not using_block_verify:
    if HAS_TRITON:
        rejection_random_sample_kernel[(grid,)](
            output_token_ids, cu_num_draft_tokens, draft_token_ids, draft_probs,
            target_probs, target_indices, bonus_token_ids, recovered_token_ids,
            uniform_probs.to(torch.float32), is_greedy, max_spec_len,
            selected_vocab_size, global_vocab_size, batch_size, ori_target_probs,
            ..., ENABLE_REDUCE_SAMPLING=True, SYNTHETIC_MODE=synthetic_mode,
            ENTROPY_VERIFY=using_entropy_verify, BLOCK_SIZE=block_size,
            POSTERIOR_THRESHOLD=posterior_threshold, POSTERIOR_ALPHA=posterior_alpha, ...)
    else:
        rejection_random_sample_pytorch(...)
else:
    if HAS_TRITON:
        rejection_random_sample_block_verify_kernel[(grid,)](...)
```

> 这段代码做了什么：随机路径分四个内核变体——`rejection_random_sample_kernel`（逐位验证）、`rejection_random_sample_block_verify_kernel`（联合验证）、以及对应的 `*_pytorch` 回退。`grid` 与 `block_size` 由 [reject_sample.py:23-31](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/reject_sample.py#L23-L31) 的 `cal_grid_and_block_size` 按 NPU 向量核数与 batch 大小决定。

**向量化「首个拒绝位」**（PyTorch 回退 [rejection_sampler.py:1254-1279](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L1254-L1279)，Triton 内核同理）：

```python
# rejection_sampler.py:1254-1265
first_rejection = (~acceptance_condition) & valid_mask
first_reject_pos = torch.where(
    first_rejection.any(dim=1, keepdim=True),
    first_rejection.float().argmax(dim=1, keepdim=True),   # 首个拒绝位
    default_pos)                                            # 全接受则填到末尾
pos_mask = pos_indices >= first_reject_pos
should_skip = pos_mask & valid_mask                         # 首个拒绝之后全部跳过
```

> 这段代码做了什么：拒绝采样的语义是「一旦拒绝，后续全部作废」。向量化实现里，先用 `argmax` 找到每行第一个拒绝位置 `first_reject_pos`，再用 `pos_indices >= first_reject_pos` 造出 `should_skip` 掩码，把首个拒绝位之后的所有位置标记为跳过，从而在没有 Python 循环的情况下模拟「短路」语义。

#### 4.3.4 代码实践

**实践目标**：跑通拒绝采样的 PyTorch 回退 UT，亲眼看一遍 draft 与 target 如何对齐验证、bonus 如何填入。这是任务要求的核心实践。

**操作步骤**：

1. 打开 [tests/ut/sample/test_rejection_sampler.py:36-64](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/ut/sample/test_rejection_sampler.py#L36-L64) 的 `test_rejection_greedy_sample_pytorch`。它的输入是：

   ```
   cu_num_draft_tokens = [2, 4]      # 两个请求，累积偏移
   num_draft_tokens   = [2, 2]       # 各 2 个草稿
   draft_token_ids    = [10, 11, 20, 21]
   target_argmax      = [10, 99, 20, 22]   # 注意第二位 11→99 不匹配
   bonus_token_ids    = [[100], [200]]
   ```

2. 在本地运行该 UT（无 NPU 也可，因为测的是 `_pytorch` 回退）：

   ```bash
   pytest tests/ut/sample/test_rejection_sampler.py::TestAscendRejectionSampler::test_rejection_greedy_sample_pytorch -v
   ```

   > 若环境缺少依赖无法运行，标注「待本地验证」，改为手工推演。

3. 手工推演对齐过程（这正是「target 与 draft 如何对齐验证」）：
   - 请求 0：draft = `[10, 11]`，target argmax = `[10, 99]`。第 0 位 `10==10` 接受；第 1 位 `11 != 99` 拒绝，取 target `99`，停止。
   - 请求 1：draft = `[20, 21]`，target argmax = `[20, 22]`。第 0 位 `20==20` 接受；第 1 位 `21 != 22` 拒绝，取 target `22`，停止。

4. 对照断言（[test_rejection_sampler.py:61-64](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/ut/sample/test_rejection_sampler.py#L61-L64)）：

   ```python
   assert output_token_ids[0, 0].item() == 10   # 请求0 第0位：接受 draft 10
   assert output_token_ids[0, 1].item() == 99   # 请求0 第1位：拒绝，取 target 99
   assert output_token_ids[1, 0].item() == 20   # 请求1 第0位：接受 draft 20
   assert output_token_ids[1, 2].item() == PLACEHOLDER_TOKEN_ID  # 请求1 没走到 bonus
   ```

**需要观察的现象**：

- `output_token_ids` 的形状是 `[batch_size, max_spec_len + 1] = [2, 3]`，多出的一列留给 bonus。
- 请求 0 在第 1 位就拒绝了，所以不会填 bonus（第 2 列保持 `PLACEHOLDER`）。
- 接受的位填 draft token，首个拒绝位填 target argmax，之后全填 `PLACEHOLDER`。

**预期结果**：能用自己的话复述——「draft 与 target 按位置（`cu_num_draft_tokens` 编排）逐位比对；贪心下相等即接受，首个不等的位改填 target argmax 并停止；全接受才在末尾填 bonus」。

> 说明：本实践首选运行 UT；若无 NPU/依赖，手工推演断言即可，结论一致。

#### 4.3.5 小练习与答案

**练习 1**：开启 `enable_block_verify` 后，验证逻辑从「逐位独立判」变成「整段联合判」，为什么这通常能 **提升接受率**？

> **答案**：逐位判时，任何一个位置的均匀随机数 `u` 偏大就会触发拒绝并截断后续所有位（哪怕后续本可接受）。Block Verify 用累积乘积 \(\pi_k = \prod_{j\le k}\min(1, p/q)\) 与累积均匀数比较，是把「这一段草稿整体作为一次抽样」来判定，等价于把原本会被「首位拒绝」连带丢弃的后续 token 用联合概率重新衡量，从而减少误丢弃、提升接受率。

**练习 2**：`rejection_sample` 里 `synthetic_mode`（合成模式）与普通模式的最大区别是什么？为什么它和 `block_verify` 互斥（[rejection_sampler.py:493-498](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/sample/rejection_sampler.py#L493-L498)）？

> **答案**：普通模式按 `u < p(d)/q(d)` 判接受（依赖 draft/target 概率比较）；合成模式则用预设的「条件接受率 `conditional_rates`」直接判接受（`u < rate`），与 target 匹配无关，用于在没有真实 draft 概率时模拟接受率。它采用的是「逐位、首次拒绝」语义，与 block_verify 的「联合（累积乘积）验证」语义冲突，所以代码里显式抛 `ValueError` 禁止同时开启。

**练习 3**：降采样路径下，`rejection_random_sample_pytorch` 如何拿到草稿 token 在 target 分布下的概率 `target_token_probs`？

> **答案**：降采样时 `target_probs` 是 `[num_tokens, top_k*tp_size]` 的候选分片，配 `target_indices`（候选的全局词表索引）。代码用 `is_in_candidates = (target_indices == draft_expanded)` 在候选里查找草稿 token 是否出现，再用 `torch.where(is_in_candidates, flat_target_probs, 0).sum(dim=1)` 取出它对应的 target 概率；若草稿不在候选里则概率为 0（必然被拒绝）。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**手工模拟一次完整的拒绝采样，并指出每一步对应哪个函数**。

**场景**：单请求（batch_size=1），`max_spec_len=2`，随机采样（非贪心），开启降采样。

**草稿与目标信息**（数值为示意，便于推演）：

- `draft_token_ids = [A, B]`，对应 `draft_probs` 里 `q(A)=0.4, q(B)=0.3`。
- target 在这两位的候选分片（`target_indices` / `target_probs`，已 softmax）里，`p(A)=0.5, p(B)=0.1`。
- 抽到的均匀随机数 `u = [0.6, 0.2]`。

**任务**：

1. **对齐**：指出 `[A, B]`、`q`、`p`、`u` 都共享长度 2 的 token 维度，靠 `cu_num_draft_tokens` 把它们绑定到同一请求的位 0、位 1。（对应 `rejection_sample` 的输入契约）
2. **逐位判接受**（对应 `rejection_random_sample_pytorch` 的 `acceptance_condition`）：
   - 位 0：`p(A)/q(A) = 0.5/0.4 = 1.25 ≥ 1 > u(0.6)` → 接受 A。
   - 位 1：`p(B)/q(B) = 0.1/0.3 ≈ 0.33`，与 `u(0.2)` 比：`0.33 ≥ 0.2` → 接受 B。
3. **bonus**：两位都被接受 → 在末尾填 bonus token（对应 `should_add_bonus` 分支）。
4. **若把 `u[1]` 改成 0.5**：位 1 `0.33 < 0.5` → 拒绝，位 1 取 `recovered_token_ids[1]`（由 `sample_recovered_tokens` 从残差 `max(0, p - q)` 算出），且不填 bonus。请推演此时 `output_token_ids` 的形状与占位情况。

**交付物**：一张表，列出「步骤 / 输入 / 输出 / 对应源码函数」四列，把上面 4 步填进去。再写一句话：为什么最终输出分布与「只用 target 采样」一致？

> 参考答案要点：拒绝采样通过「接受概率 = min(1, p/q)」+「拒绝时从残差 `max(0,p-q)` 恢复」两步，恰好让最终每个 token 的输出概率等于 target 分布 \(p\)（草稿分布 \(q\) 在数学上被抵消），这就是它「无损加速」的理论保证。

## 6. 本讲小结

- `AscendSampler` 继承上游 `Sampler`，只重写 `apply_penalties` 与 `topk_topp_sampler` 两点；随机采样用「指数分布 + argmax」替代 `torch.multinomial`，避免 CPU-NPU 同步。
- TP 降采样路径（`enable_reduce_sample`）下，贪心与 top-k 都用 all-gather 拼接各卡候选，贪心 argmax 还要把本地索引加 `rank*V_local` 还原成全局索引。
- 惩罚项（repetition / frequency / presence）由 `apply_all_penalties` 经 Triton-Ascend 内核 `apply_all_penalties_kernel` 就地改写 logits，无 Triton 时安全回退到上游。
- `AscendRejectionSampler` 负责投机解码验证：draft 与 target 按 token 维度（`cu_num_draft_tokens` 编排）逐位对齐，随机路径按 `u < p(d)/q(d)` 判接受，拒绝位从残差分布恢复，全接受则补 bonus。
- 拒绝采样的「首个拒绝即截断」语义用 `argmax` 找首个拒绝位 + `should_skip` 掩码向量化实现，全程无 Python 循环。
- 两种增强策略可配：Block Verify（累积乘积联合验证，需 `max_spec_len>=3`）与 Entropy Verify（基于熵的后验阈值放宽），二者都能提升接受率；synthetic_mode 与 block_verify 互斥。

## 7. 下一步学习建议

- 本讲聚焦 v1 model runner 的采样链路。若你对投机解码的 **草稿生成侧** 感兴趣，建议接着读 u10-l4（投机解码），那里讲 `eagle_proposer` / `ngram_proposer` / `mtp` 等 drafter 是如何产出 `draft_token_ids` 与 `draft_probs` 喂给本讲的 `AscendRejectionSampler` 的。
- 想了解 v2 架构下采样的差异，可读 u4-l3（NPUModelRunner v2），对比 v1/v2 在采样衔接上的不同。
- Triton 内核细节（grid/block 划分、向量核适配）属于 u6-l2（Triton 算子集）的范围；本讲提到的 `reject_sample.py` 与 `penalty.py` 都可作为该讲的实例。
- 建议动手扩展：参照 [tests/ut/sample/test_rejection_sampler.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/ut/sample/test_rejection_sampler.py) 的模式，为 Block Verify 或 Entropy Verify 的 PyTorch 回退补一个最小用例，以加深对联合验证与熵阈值的理解。
