# DSpark 核心机制：锚点采样、噪声块与非因果掩码

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `build_anchor_candidate_mask` 对「锚点自身」与「首个监督目标」的双约束从何而来。
2. 描述 `create_noise_embed` 如何在一条全 mask token 的流里放回锚点 token，以及 `create_position_ids` 为什么让块内第 0 个槽位保持锚点的原始位置。
3. 读懂 `dspark_mask_mod` 中「上下文可见 ∪ 本块内可见」的或逻辑，并能手算任意一个 query 能看到哪些 key。
4. 理解 DSpark「一次前向并行产出 num_anchors × block_size 个监督位置」的训练构图为什么是块式半自回归（block-wise semi-autoregressive）的。

本讲只聚焦 `deepspec/modeling/dspark/common.py` 中「构图」相关的三个函数；草稿模型的本体结构（注意力如何同时吃目标隐状态与噪声 embedding）留给下一讲 u4-l2。

## 2. 前置知识

### 2.1 从 u2-l2 / u3-l1 承接的两个概念

- **loss_mask**：在 u2-l2 中，`preprocess_record` 把对话渲染成 token 序列后，只有 assistant 回复（含结束符）对应的位置 loss_mask 为 1，其余（system/user/prompt 部分）为 0。它随 target cache 一起落盘（u2-l4），训练时从缓存读出（u2-l6）。
- **run_batch 的输入**：u3-l1 讲过，`BaseTrainer` 用模板方法模式把前向下放给子类的 `run_batch`，喂给草稿模型 `forward` 的三样东西正是 `input_ids`、`target_hidden_states`（K 层拼接的目标中间隐状态）、`loss_mask`。

### 2.2 DSpark 的训练范式：不是「从左到右」，而是「锚点 + 填空块」

普通语言模型训练是严格自回归的：位置 \( i \) 的隐状态预测 token \( i+1 \)，一个样本一次前向只能得到 seq_len 个监督位置，且必须串行。

DSpark 的做法不同。想象序列中被监督（assistant 生成）的某个位置 \( p \)：

```
...  c_{p-3}  c_{p-2}  c_{p-1} [p] t_{p+1} t_{p+2} ... t_{p+B} ...
                            ↑锚点   └────── B 个待预测 token ──────┘
```

- 把位置 \( p \) 的真实 token 叫**锚点（anchor）**。
- 草稿模型的输入不是「上文 + 已生成的草稿」，而是「上文 + 一个以锚点开头、后面全是 mask token 的**噪声块**」。
- 一次前向之后，块内第 \( t \) 个槽位输出对 \( t_{p+t+1} \) 的预测——也就是说，**块内 B 个预测是并行的**，每个槽位只靠位置编码区分自己该预测哪个 token。
- 每个样本不止放一个块：`config/dspark/dspark_qwen3_4b.py` 里 `num_anchors=512`、`block_size=7`，即一条样本一次前向并行产出 512 × 7 = 3584 个监督位置。

这与推理时的行为严格一致：部署时 DSpark 也是拿「已接受的上下文 + 最后一个被接受的 token 作为锚点」拼一个噪声块，一次 forward 吐出整块候选 token（评估侧细节见 u6-l4）。**训练构图 = 推理构图的复现**，这是理解本讲所有代码的主线。

### 2.3 flex_attention 与 mask_mod

PyTorch 的 `torch.nn.attention.flex_attention` 允许用一个纯函数描述任意注意力可见性：

```python
def mask_mod(b, h, q_idx, kv_idx) -> bool:  # batch、head、query 下标、key 下标
    ...
```

`create_block_mask(mask_mod, ...)` 会把它编译成**块稀疏**的 `BlockMask`，而不是物化一个 \([B, H, Q, KV]\) 的全量 float 掩码。DSpark 的 KV 长度是 `seq_len + num_anchors × block_size`（下文详述），全量掩码在这个尺度下不可行，块稀疏表示是必需品。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `deepspec/modeling/dspark/common.py` | DSpark 模型无关的共享工具：构图、损失输入构造、指标 | 全部三个最小模块都在这里 |
| `deepspec/modeling/dspark/qwen3/modeling.py` | Qwen3 族草稿模型本体 | 只看 `forward` 中调用构图函数的顺序（L398-421），证明三块拼图如何咬合 |
| `config/dspark/dspark_qwen3_4b.py` | DSpark + Qwen3-4B 的训练配置 | `block_size=7`、`num_anchors=512`、`mask_token_id=151669` 的取值 |

Gemma4 族（`deepspec/modeling/dspark/gemma4/modeling.py` L460-480）以完全相同的顺序调用同一组函数，本讲不区分模型族。

## 4. 核心概念与源码讲解

先给出全局图。`Qwen3DSparkModel.forward`（[deepspec/modeling/dspark/qwen3/modeling.py:388-427](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L388-L427)）的构图阶段只有四步：

```text
① sample_anchor_positions(loss_mask)      → anchor_positions [B, num_anchors], block_keep_mask [B, num_anchors]
② create_noise_embed(...)                 → noise_embedding   [B, num_anchors×block_size, H]
③ create_position_ids(...)                → 块内位置 = 锚点位置 + 0..block_size-1
④ create_dspark_attention_mask(...)       → BlockMask（Q=num_blocks×block_size, KV=seq_len+num_blocks×block_size）
```

喂给草稿主干的最终序列布局是（K/V 的拼接发生在注意力层内部，见 [deepspec/modeling/dspark/qwen3/modeling.py:107-112](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L107-L112) 的 `k = torch.cat([k_ctx, k_noise], dim=1)`）：

```text
KV 序列（长度 seq_len + num_blocks × block_size）
┌────────────────────────────┬───────────────────────────────────────────┐
│ 上下文：目标模型隐状态投影    │ 噪声块：num_blocks × block_size            │
│ 0, 1, ..., seq_len-1       │ [blk0: a₀ m m m m m m][blk1: a₁ m m ...]… │
└────────────────────────────┴───────────────────────────────────────────┘
query 流 = 噪声块自身（只有草稿槽位发出 query）
aᵢ = 第 i 块的锚点 token，m = mask token
```

下面逐模块拆解。

### 4.1 锚点采样：build_anchor_candidate_mask 与 sample_anchor_positions

#### 4.1.1 概念说明

锚点采样回答的问题是：**在哪些位置放块？**

不是随便放。约束来自 `DSparkForwardOutput` 的文档字符串（[deepspec/modeling/dspark/common.py:22-27](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L22-L27)）：「采样器只保留**首个草稿目标**被 loss_mask 启用的锚点」。展开成代码就是双约束：

- 位置 \( i \) 可以当锚点，需要 `loss_mask[i] == 1`（锚点自身落在被监督区间内，即 assistant 生成的 token——与推理时「锚点必是目标模型已接受的真实 token」一致）；
- 同时 `loss_mask[i+1] == 1`（块内第 0 个槽位预测 token \( i+1 \)，这个预测必须有监督才有损失可算）。

此外一个样本未必凑得出 `num_anchors` 个合法锚点（assistant 片段太短），所以采样器还要输出 `block_keep_mask` 标记哪些块是真实的，**dummy 块**由 `block_keep_mask` 和 `eval_mask` 在下游统一屏蔽。

#### 4.1.2 核心流程

```text
build_anchor_candidate_mask(seq_len, loss_mask):
    num_candidates = seq_len - 1                    # 锚点最多放到倒数第 2 个位置
    valid[i] = loss_mask[i] > 0.5  AND  loss_mask[i+1] > 0.5

sample_anchor_positions(seq_len, loss_mask, num_anchors):
    valid ← 上式
    1. 无效候选位填哨兵 index=seq_len+1、随机数=2.0（排序时必然沉底）
    2. 对随机数排序 → gather 哨兵化的下标 → 等价于「有效位中均匀无放回抽 max_n 个」
    3. 不足 max_n 时补 seq_len+1 哨兵
    4. 升序排序（哨兵天然沉到尾部）
    5. keep_mask = 前 min(有效数, num_anchors) 个为 True 的前缀掩码
    6. 被丢弃槽位的锚点位置清零（配合 4.2 里的 where 再抹回 mask token）
```

注意第 4、5 步的配合：升序排序后真实锚点必然是**前缀**，所以 `keep_mask` 才能用一个 `arange < clamp(count, max_n)` 的前缀掩码表达。

#### 4.1.3 源码精读

双约束的核心三行（[deepspec/modeling/dspark/common.py:109-120](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L109-L120)）：`anchor_valid` 取 `loss_mask[:, :num_candidates]`，`first_target_valid` 取错开一位的 `loss_mask[:, 1:num_candidates+1]`，两者按位与：

```python
anchor_valid = loss_mask[:, :num_candidates] > 0.5
first_target_valid = loss_mask[:, 1 : num_candidates + 1] > 0.5
return anchor_valid & first_target_valid
```

采样主体（[deepspec/modeling/dspark/common.py:123-169](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L123-L169)）。哨兵排序这一段是全函数的灵魂（L143-164）：

```python
masked_indices = torch.where(valid, indices, torch.full_like(indices, seq_len + 1))
random_vals = torch.rand(bsz, num_candidates, device=device)
random_vals = torch.where(valid, random_vals, torch.full_like(random_vals, 2.0))
_, sorted_idx = random_vals.sort(dim=1)
gathered = torch.gather(masked_indices, 1, sorted_idx)
...
anchors = gathered[:, :max_n].sort(dim=1).values
keep_mask = torch.arange(max_n, device=device).unsqueeze(0) < (
    valid_counts.unsqueeze(1).clamp(max=max_n)
)
anchors = torch.where(keep_mask, anchors, torch.zeros_like(anchors))
```

无效位的随机数被固定为 2.0（大于任何 `rand` 值），排序后必然沉底，于是「取排序后前 max_n 个」=「只在有效位里无放回均匀抽样」。最后被丢弃槽位清零（L168），这个零值会在 4.2 的 `create_noise_embed` 里被 `where(block_keep_mask, ...)` 抹回 mask token，两级防御。

采样质量有配套观测：`log_sampler_stats`（[deepspec/modeling/dspark/common.py:191-248](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L191-L248)）把 `valid_anchors_ratio`、`sampled_anchors_ratio`、`block_supervised_tokens_ratio` 写进 u3-l6 讲过的 `add_metric` 比值型指标，可在 TensorBoard 里确认「锚点不够抽」的样本占比。

#### 4.1.4 代码实践

**实践目标**：用玩具样本验证双约束与 dummy 块行为。

**操作步骤**（示例代码，在仓库根目录运行，仅需安装 torch）：

```python
import torch
from deepspec.modeling.dspark.common import build_anchor_candidate_mask, sample_anchor_positions

seq_len = 16
loss_mask = torch.zeros(1, seq_len)
loss_mask[0, 6:13] = 1.0  # 只有 6..12 被监督

valid = build_anchor_candidate_mask(seq_len=seq_len, loss_mask=loss_mask)
print("candidate mask:", valid[0].tolist())

torch.manual_seed(0)
anchors, keep = sample_anchor_positions(
    seq_len=seq_len, loss_mask=loss_mask, num_anchors=4, device=torch.device("cpu")
)
print("anchors:", anchors[0].tolist(), "keep:", keep[0].tolist())

anchors2, keep2 = sample_anchor_positions(
    seq_len=seq_len, loss_mask=loss_mask, num_anchors=10, device=torch.device("cpu")
)
print("anchors2:", anchors2[0].tolist(), "keep2:", keep2[0].tolist())
```

**需要观察的现象**：

- `candidate mask` 是长度 15 的列表，True 恰好出现在下标 6..11（共 6 个）。
- `anchors` 是从 {6,...,11} 中无放回抽出、**升序排列**的 4 个互异值；`keep` 全 True。
- `anchors2`：前 6 个是升序真实锚点，后 4 个是 0；`keep2` 为 `[True]*6 + [False]*4`。

**预期结果**：candidate mask 下标 5 处为 False（虽然 `loss_mask[6]=1` 使其作为「首个目标」合格，但 `loss_mask[5]=0` 使其作为锚点自身不合格）；下标 12 处也为 False（锚点自身合格，但首个目标 `loss_mask[13]=0`）。两种失败模式正好各占一端，印证双约束。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：若 `seq_len=16` 且 loss_mask 只在位置 0 为 1，`sample_anchor_positions` 返回什么？
**答案**：candidate mask 全 False（位置 0 不满足 `loss_mask[1]=1`），`valid_counts=0`，`keep_mask` 全 False、`anchors` 全 0。注意此时 `num_candidates=15 ≠ 0`，不会走 L138-141 的早退分支（那个分支只在 `seq_len=1` 时触发），而是走正常路径后被 keep_mask 全部丢弃。

**练习 2**：为什么不直接用 `loss_mask` 当 candidate mask，非要错开一位？
**答案**：块内第 0 个槽位预测 `anchor+1` 处的 token，若 `loss_mask[anchor+1]=0`，这个槽位的交叉熵目标不存在（是 prompt 或 padding），整块的监督从第一个位置就是空的。错位与保证「第一个预测目标一定有监督」。

**练习 3**：`anchors` 为什么要在最后升序排序？打乱顺序行不行？
**答案**：功能上行——下游只用 `anchor_positions` 做 gather 和掩码，与块顺序无关。但升序 + 哨兵沉底让「真实锚点占据前缀、dummy 占据尾部」成为不变量，`keep_mask` 才能简化成一个前缀掩码，`eval_mask`、注意力掩码里的 `q_block_id` 语义也更稳定。

### 4.2 噪声块构造：create_noise_embed 与 create_position_ids

#### 4.2.1 概念说明

锚点定了之后，草稿模型的 query 流要长什么样？

- **create_noise_embed**：先铺一条长度为 `num_blocks × block_size` 的 id 流，全部填 `mask_token_id`（Qwen3 族配置为 151669，Gemma4 族为 4，见 [config/dspark/dspark_qwen3_4b.py:15](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L15)）；再把每个块的第 0 个槽位（flat 下标 `b × block_size`）替换成该块锚点处的**真实 token**（从 `input_ids` gather 出来）；dummy 块保持 mask token。最后过 `embed_tokens` 得到 embedding。
- **create_position_ids**：块内第 \( t \) 个槽位的位置 id 取 \( p_b + t \)，其中 \( p_b \) 是块 \( b \) 的锚点位置。

位置公式的妙处：锚点槽位的位置 id 就是它原本在上下文中的位置 \( p_b \)，于是草稿槽位 \( t \) 对上下文 key \( j \) 的 RoPE 相对距离恰为 \( (p_b + t) - j \)——**与这些 token 在真实序列里的距离完全一致**。模型不需要重新学习「块内位置到真实位置的换算」。

#### 4.2.2 核心流程

```text
create_noise_embed(embed_tokens, input_ids, anchor_positions, block_keep_mask, mask_token_id, block_size):
    noise_ids ← 全 mask_token_id 的 [B, num_blocks×block_size]
    anchor_tokens ← gather(input_ids, anchor_positions)      # 每块锚点处的真实 token
    noise_ids[batch, b×block_size] ← where(block_keep_mask, anchor_tokens, mask_token_id)
    return embed_tokens(noise_ids)

create_position_ids(anchor_positions, block_size):
    offsets = [0, 1, ..., block_size-1]
    return (anchor_positions[:, :, None] + offsets).reshape(B, num_blocks×block_size)
```

注意上下文那段的位置 id 不由这两个函数负责——`forward` 里直接用 `arange(seq_len)` 拼接（[deepspec/modeling/dspark/qwen3/modeling.py:412-414](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L412-L414)）。

#### 4.2.3 源码精读

`create_position_ids`（[deepspec/modeling/dspark/common.py:251-261](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L251-L261)）就是一行广播加法：

```python
offsets = torch.arange(block_size, device=device).view(1, 1, -1)
return (anchor_positions.unsqueeze(-1) + offsets).view(bsz, num_blocks * block_size)
```

`create_noise_embed` 的关键在散射回填（[deepspec/modeling/dspark/common.py:264-294](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L264-L294)），L282-293：

```python
block_starts = torch.arange(num_blocks, device=device) * block_size
anchor_tokens = torch.gather(input_ids, 1, anchor_positions)
...
noise_ids[flat_batch_idx, block_starts] = torch.where(
    block_keep_mask,
    anchor_tokens,
    torch.tensor(mask_token_id, dtype=torch.long, device=device),
)
return embed_tokens(noise_ids)
```

`torch.where(block_keep_mask, ...)` 是对 4.1 末尾「被丢弃锚点清零」的第二级防御：dummy 块的 `anchor_positions` 已经是 0，若没有这个 where，会把序列第 0 个 token（通常是 BOS/prompt 开头）错误地放进 dummy 块头部。

#### 4.2.4 代码实践

**实践目标**：验证「锚点 token 落在块头、其余槽位是 mask token、位置 id 与真实位置对齐」。

**操作步骤**（示例代码）：

```python
import torch
from torch import nn
from deepspec.modeling.dspark.common import create_noise_embed, create_position_ids

vocab, hidden, block_size = 100, 8, 3
torch.manual_seed(0)
embed = nn.Embedding(vocab, hidden)   # 权重确定，embedding(id) == weight[id]
input_ids = torch.randint(0, vocab, (1, 16))
loss_mask = torch.zeros(1, 16); loss_mask[0, 6:13] = 1.0

# 人为指定 2 个块：锚点 6 和 10，全部保留
anchor_positions = torch.tensor([[6, 10]])
block_keep_mask = torch.tensor([[True, True]])

noise = create_noise_embed(embed, input_ids, anchor_positions, block_keep_mask,
                           mask_token_id=99, block_size=block_size)
pos = create_position_ids(anchor_positions, block_size)

# 反查每个槽位的 token id：找 weight 中与 noise 逐行相同的行
w = embed.weight
ids = [int((w == row).all(dim=-1).nonzero()[0]) for row in noise[0]]
print("noise ids :", ids)          # 期望 [input_ids[6], 99, 99, input_ids[10], 99, 99]
print("position  :", pos[0].tolist())  # 期望 [6, 7, 8, 10, 11, 12]
```

**需要观察的现象**：反查得到的 id 列表长度为 6；每个块的第 0 个槽位等于对应锚点处的 `input_ids` 值，其余槽位为 99。

**预期结果**：`position` 为 `[6, 7, 8, 10, 11, 12]`——块 0 从锚点位置 6 顺延，块 1 从 10 顺延，两块互不干扰。运行结果待本地验证（`vocab=100` 时随机权重行可能碰巧重复导致反查歧义，若遇到可增大 `vocab`）。

#### 4.2.5 小练习与答案

**练习 1**：块内第 0 个槽位的位置 id 是多少？为什么必须等于 \( p_b \) 而不是 0？
**答案**：等于锚点位置 \( p_b \)（offsets 从 0 开始）。若从 0 开始，锚点槽位与上下文 key 的 RoPE 相对距离会被压缩，模型看到的几何关系与真实序列不一致，训练信号与推理时的分布脱节。

**练习 2**：把 `create_noise_embed` 里的 `torch.where(block_keep_mask, ...)` 去掉会有什么后果？
**答案**：dummy 块的 `anchor_positions` 为 0，会错误地把 `input_ids[0]`（prompt/BOS 类 token）的 embedding 放进 dummy 块头部。由于 dummy 块的输出随后被 `eval_mask` 与注意力掩码屏蔽，损失不受影响，但 forward 的输入被污染，属于「侥幸正确」的隐患写法。

**练习 3**：为什么块内非锚点槽位统一用 mask token，而不是像朴素 AR 那样填「前一个真实 token」？
**答案**：因为 DSpark 的块内预测本来就是并行的——训练时块内槽位互相看到的只是 mask embedding 加不同位置编码（见 4.3），token 级的自回归条件不靠主干、而靠 markov head 在 logits 层用真实前一 token 补上（[deepspec/modeling/dspark/qwen3/modeling.py:473-481](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L473-L481) 构造的 `prev_token_ids` 正是 `[锚点, 真实目标 token 前移一位]`；详见 u4-l3）。若在输入里就喂真实前 token，推理时（还没有这些 token）就无解了。

### 4.3 非因果注意力：create_dspark_attention_mask 与 dspark_mask_mod

#### 4.3.1 概念说明

现在回答最后一个问题：**块内槽位和上下文之间，谁看得到谁？**

规则一共三条：

1. **上下文可见**：块 \( b \)（锚点 \( p_b \)）的所有槽位都能看到上下文 key \( j < p_b \)——注意是**严格小于**，锚点自身的 token 不出现在上下文里，因为它已经作为块头活在噪声流中（4.2），出现两次会造成同一 token 被两套投影各算一次 K/V。
2. **块内可见**：块内的所有槽位互相可见（包括锚点槽位），**块间不可见**——每个块是一个独立的提议单元，与推理时「一块一块独立提议」一致。
3. **dummy 块整体不可见**：`is_valid_block` 把被丢弃块的所有 query 行整个置 False，其输出由 `eval_mask` 屏蔽，不进损失。

注意这个注意力是**非因果**的：块内后面的槽位能看到前面的槽位，前面的也能看到后面的（双向）。这之所以可行，是因为块内非锚点槽位的输入全是同一个 mask embedding，块内可见性实际提供的信息只有「块内相对位置」（经 RoPE 编码）；真正的 token 级自回归性由 markov head 在输出层补齐（练习 3 已述）。这就是「块式半自回归」一词的准确含义：**块间串行语义、块内并行构图**。

#### 4.3.2 核心流程

用可见性谓词表述（对 batch \( b \)、query 全局下标 \( q \)、key 全局下标 \( k \)）：

\[
\text{visible}(b, q, k) = \Big( \underbrace{(k < L) \land (k < p_{b'})}_{\text{上下文}} \lor\ \underbrace{(k \ge L) \land (b' = \lfloor (k - L)/B \rfloor)}_{\text{本块内}} \Big) \land \text{keep}(b, b')
\]

其中 \( L \) 是 seq_len，\( B \) 是 block_size，\( b' = \lfloor q / B \rfloor \) 是 query 所属块。query 全局下标只在噪声流内取值（\( 0 \le q < N \times B \)），key 全局下标覆盖上下文 + 噪声流。

单个 query 的可见 key 集合可以手算为：

\[
\mathcal{K}(q) = \{0, 1, \dots, p_{b'} - 1\} \cup \{L + b'B,\ \dots,\ L + (b'+1)B - 1\}
\]

#### 4.3.3 源码精读

掩码谓词本体（[deepspec/modeling/dspark/common.py:86-96](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L86-L96)）：

```python
def dspark_mask_mod(b, h, q_idx, kv_idx):
    del h
    q_block_id = q_idx // block_size
    anchor_pos = anchor_positions[b, q_block_id]
    is_context = kv_idx < seq_len
    mask_context = is_context & (kv_idx < anchor_pos)
    is_draft = kv_idx >= seq_len
    kv_block_id = (kv_idx - seq_len) // block_size
    mask_draft = is_draft & (q_block_id == kv_block_id)
    is_valid_block = block_keep_mask[b, q_block_id]
    return (mask_context | mask_draft) & is_valid_block
```

四行各自对应一条规则：`mask_context` 是「上下文且在锚点左侧」（严格 `<`），`mask_draft` 是「噪声流且同块」（块间由 `q_block_id == kv_block_id` 排除），二者取或，最后与 `is_valid_block` 相与。

外层包装（[deepspec/modeling/dspark/common.py:98-106](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L98-L106)）：

```python
return create_block_mask(
    dspark_mask_mod, B=bsz, H=None,
    Q_LEN=num_blocks * block_size,
    KV_LEN=seq_len + num_blocks * block_size,
    device=device,
)
```

两个长度参数揭示了序列布局：query 只有噪声块（`num_blocks × block_size`），KV 是上下文加噪声流（`seq_len + num_blocks × block_size`）。这个布局在注意力层内部由 `k = torch.cat([k_ctx, k_noise], dim=1)` 落实（[deepspec/modeling/dspark/qwen3/modeling.py:107-112](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L107-L112)）：上下文段的 K/V 由目标隐状态投影得到，噪声段的 K/V 由 `noise_embedding` 投影得到——投影细节属 u4-l2 范围。

与监督的衔接：`forward` 里 `label_indices = anchor_positions + [1..block_size]`（[deepspec/modeling/dspark/qwen3/modeling.py:432-446](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L432-L446)），即槽位 \( t \) 预测 token \( p_b + t + 1 \)；`build_eval_mask`（[deepspec/modeling/dspark/common.py:172-188](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L172-L188)）再把逐位有效性做 `cumprod`，保证监督只在「连续有效前缀」上生效——锚点离序列监督区间末尾太近时，块尾几个槽位自动退出损失。

#### 4.3.4 代码实践

**实践目标**：手算并用纯 Python 复核掩码谓词，不依赖 flex_attention 编译。

**操作步骤**（示例代码）：

```python
# 接 4.1.4 的玩具设定：seq_len=16, block_size=3
# 假设 sample_anchor_positions(num_anchors=4) 抽到 anchors = [6, 8, 10, 11]，全保留
seq_len, block_size = 16, 3
anchors = [6, 8, 10, 11]
num_blocks = len(anchors)

def visible(q):  # 复刻 dspark_mask_mod 的逻辑，逐 key 判定
    b = q // block_size
    p = anchors[b]
    keys = []
    for k in range(seq_len + num_blocks * block_size):
        mask_context = (k < seq_len) and (k < p)
        mask_draft = (k >= seq_len) and ((k - seq_len) // block_size == b)
        if mask_context or mask_draft:
            keys.append(k)
    return keys

for q in [0, 1, 5, 6]:  # 块0的三个槽位 + 块1的第一个槽位
    print(f"q={q:2d} (block {q//block_size}) -> {visible(q)}")
```

**需要观察的现象**：四行输出的可见 key 集合。

**预期结果**（先手算再对照）：

- `q=0/1/2`（块 0，锚点 6）：`{0,1,2,3,4,5}` + `{16,17,18}`。上下文严格停在 5（锚点 6 不含），块 0 的噪声 key 是全局下标 16-18。
- `q=5`（块 1，锚点 8）：`{0..7}` + `{19,20,21}`。
- `q=6`（块 2，锚点 10）：`{0..9}` + `{22,23,24}`。

同一块的三个槽位可见集合完全相同——「上下文 ∪ 本块」与槽位在块内的位置无关；块与块之间只通过各自的锚点位置差区分可见的上下文范围。若想进一步验证 `create_block_mask` 本体，可在有 torch ≥ 2.5 的环境里直接调用 `create_dspark_attention_mask(...)` 后把 `BlockMask` 转成稠密布尔矩阵对照（编译耗时较长，结果待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：把 `kv_idx < anchor_pos` 改成 `<=` 会发生什么？
**答案**：锚点 token 会在上下文段（目标隐状态投影的 K/V）和噪声流块头（噪声 embedding 投影的 K/V）各出现一次，注意力会双重计数同一 token；且这与推理时的构图不一致（推理时锚点只作为草稿块头存在），训练/推理分布出现偏移。

**练习 2**：为什么块间必须不可见（去掉 `q_block_id == kv_block_id` 这个条件）？
**答案**：块是独立的提议单元。推理时 DSpark 每轮只构造**一个**块做提议，不存在「其他块」；若训练时块 \( b \) 能看到块 \( b' \) 的噪声表示，模型学到的是推理时不存在的依赖。另外块顺序在样本内本是随机采样的副产品，块间可见会引入无意义的顺序信息。

**练习 3**：dummy 块的 query 行全为 False，softmax 会不会出 NaN？
**答案**：代码不需要为此担心——flex_attention 的块稀疏实现对全遮蔽行有专门的空注意力处理（输出零），且更重要的是下游语义：dummy 块的输出被 `eval_mask`（`block_keep_mask` 相与）彻底排除在损失之外（[deepspec/modeling/dspark/common.py:187](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L187)），其具体数值不参与任何梯度路径。若你对空注意力行为存疑，可在本地用 `create_block_mask` 的稠密化输出验证（待本地验证）。

## 5. 综合实践

把三个模块串成一个「一次前向的纸上推演」：

1. **构造**：`seq_len=16`，`loss_mask` 在 6..12 为 1，`block_size=3`，`num_anchors=4`。
2. **采样**（4.1.4 脚本）：记录抽到的 `anchors` 与 `keep`（应为 {6..11} 中升序 4 个、全 True）。
3. **构图**（4.2.4 脚本）：反查 noise id 流（每块头是锚点 token、其余是 mask token），打印 `create_position_ids` 的输出。
4. **掩码**（4.3.4 脚本）：对每个块打印可见 key 集合，填出下面这张表（示例答案以 anchors=[6,8,10,11] 为准）：

| 块号 | 锚点 | 可见上下文 key | 可见噪声 key |
|---|---|---|---|
| 0 | 6 | 0..5 | 16..18 |
| 1 | 8 | 0..7 | 19..21 |
| 2 | 10 | 0..9 | 22..24 |
| 3 | 11 | 0..10 | 25..27 |

5. **监督对齐**：手写每个块槽位 \( t \) 的预测目标下标（应为 \( p_b + t + 1 \)，例如块 0 的三个槽位分别预测 token 7、8、9），并与 `label_indices = anchor_positions + [1..block_size]` 的定义（[deepspec/modeling/dspark/qwen3/modeling.py:432-435](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L432-L435)）核对；标出哪些槽位会因越过监督区间末尾（12）被 `build_eval_mask` 的 cumprod 剔除（块 2 预测 13 的槽位、块 3 预测 13 与 14 的槽位）。

完成后，你应当能不看资料画出「KV 序列布局 + 任一块的可见范围 + 槽位到目标的错位对齐」三合一示意图。

## 6. 本讲小结

- **锚点采样是双约束的**：位置 \( i \) 当锚点需要 `loss_mask[i]` 与 `loss_mask[i+1]` 同时为 1——锚点自身须是 assistant token，且块内第一个预测目标必须有监督；凑不满 `num_anchors` 时用 dummy 块 + `block_keep_mask` 前缀掩码兜底。
- **噪声块 = mask token 流 + 块头放回锚点**：`create_noise_embed` 用 `where(keep_mask, ...)` 抹掉 dummy 块的零值锚点，与采样端的清零构成两级防御。
- **位置 id 从锚点顺延**：槽位 \( t \) 的位置是 \( p_b + t \)，锚点槽位保持其在真实序列中的位置，RoPE 相对距离与真实序列一致。
- **注意力是「上下文严格左侧 ∪ 本块内」**：上下文可见到 \( p_b - 1 \) 为止（锚点不重复出现），块内双向可见但块间完全隔离，dummy 块整体屏蔽——由一个 11 行的 `dspark_mask_mod` 谓词经 `create_block_mask` 编译成块稀疏掩码。
- **块式半自回归的分工**：主干只负责「给定上下文与位置，并行填出 B 个槽位」，token 级自回归条件由 markov head 在 logits 层用真实前一 token 补上——这正是训练构图能平行于推理构图的原因。
- 一次前向的监督规模是 `num_anchors × block_size`（Qwen3-4B 配置下 512 × 7 = 3584 个位置），这是 DSpark 训练效率的来源。

## 7. 下一步学习建议

本讲只讲了「构图」，三块拼图如何被草稿模型消费还没展开。下一讲 **u4-l2（Qwen3DSparkModel：复用 HF 组件的草稿模型结构）** 将精读 `Qwen3DSparkAttention`：上下文段的 K/V 如何由 `target_hidden_states` 投影、噪声段如何由 `noise_embedding` 投影、两段如何拼接后与本讲的 BlockMask 对齐，以及 `build_draft_config` 如何从目标 config 派生 `block_size`、`num_draft_layers` 等字段。之后 u4-l3（markov head）会接住本讲反复预告的「logits 层自回归补偿」，u4-l4 则讲这些监督位置上的损失如何计算。阅读源码时建议从 [deepspec/modeling/dspark/qwen3/modeling.py:388](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L388) 的 `forward` 入手向下追。
