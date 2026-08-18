# Eagle3 损失与训练器：逐步 CE 与位置错位约定

## 1. 本讲目标

本讲是 Eagle3 系列的第二讲，承接 u5-l1 对 Eagle3 草稿模型结构（5 层拼接特征、单层草稿、TTT 链式展开）的讲解，进入「怎么算损失、怎么接进训练框架」。

学完本讲，你应该能够：

1. 解释 Eagle3 训练时「隐状态 \(i\) 配 token \(i+1\)、RoPE 位置保持 \(i\)」的错位约定，并说出每一步 TTT 中 query 位置看到的是哪个 token、预测的是哪个 token。
2. 描述 `compute_eagle3_loss` 的完整流程：软目标构造、TTT 循环、`step_loss_decay` 逐步衰减加权、`local_mean` 归一化。
3. 对比 `Qwen3Eagle3Trainer.run_batch` 与 DSpark 版本的输入差异：一次前向 + 三项加权损失 vs 七次链式前向 + 单一软交叉熵。

## 2. 前置知识

本讲默认你已读过 u5-l1（Eagle3 模型结构）和 u3-l1/u3-l2（BaseTrainer 装配与主循环）。这里补充三个概念：

- **软交叉熵（soft cross-entropy）**：普通交叉熵的目标是 one-hot 标签；软交叉熵的目标是一个完整概率分布（这里是目标模型输出的分布）。它等价于知识蒸馏损失，梯度方向是「把草稿分布拉向目标分布」。u4-l4 里 DSpark 用 L1 距离做蒸馏，Eagle3 则直接用软交叉熵。
- **teacher forcing（教师强制）**：训练时每一步喂给模型的是真实 token，而不是模型自己上一步采样的 token。Eagle3 的 TTT 循环里，`current_input_ids` 每步左移的都是真实 `input_ids`，属于 teacher forcing。
- **Triton**：一个在 Python 里写 GPU kernel 的编译器。本讲的 `FusedLogSoftmaxLoss` 用 Triton 把软交叉熵的前向和反向融合成两个 kernel，核心目的是省显存。看不懂 kernel 细节不影响理解主流程。

复习两个 u5-l1 已建立的结论，本讲直接使用：

- 草稿注意力输入是 `fc(5 层拼接隐状态)` 与 embedding 各过 LN 后的 2H 拼接；`draft_num_hidden_layers=1`。
- TTT（train-time test）：训练时把推理期的链式展开「预演」`ttt_length=7` 步，每步吃自己上一步的隐状态。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/modeling/eagle3/loss.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py) | 本讲主战场：`compute_eagle3_loss`、错位工具函数、Triton 融合软交叉熵 `FusedLogSoftmaxLoss`、逐步与前缀指标 |
| [deepspec/trainer/eagle3_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/eagle3_trainer.py) | `Qwen3Eagle3Trainer` / `Gemma4Eagle3Trainer`：复用 BaseTrainer，只覆写少量钩子 |
| [deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py) | 训练骨架：`build_models` 默认实现、`run_batch` 抽象钩子、主循环对 `run_batch` 的调用点 |
| [deepspec/trainer/dspark_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py) | 对照组：DSpark 版 `run_batch` |
| [deepspec/modeling/eagle3/qwen3/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py) | 草稿模型 `forward` 的 RoPE 偏移分支、`ttt_length`/`step_loss_decay` 属性来源 |
| [deepspec/modeling/eagle3/common.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py) | `Eagle3ForwardOutput` 数据合同 |
| [config/eagle3/eagle3_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py) | 训练配置：`ttt_length=7`、`step_loss_decay=0.8` |

batch 的五个字段来自 `CacheCollator`（u2-l6）：`input_ids`、`loss_mask`、`attention_mask`、`target_hidden_states`（K×H 拼接特征）、`target_last_hidden_states`（目标最终层隐状态），见 [deepspec/data/target_cache_dataset.py:859-870](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L859-L870)。

## 4. 核心概念与源码讲解

本讲的最小模块：**位置错位约定**、**compute_eagle3_loss**、**FusedLogSoftmaxLoss 与训练指标**、**eagle3_trainer**。

### 4.1 位置错位约定：隐状态 i 配 token i+1

#### 4.1.1 概念说明

回想 u1-l1 的核心命题：投机解码的加速比取决于草稿分布是否逼近目标分布。EAGLE 系列的关键洞察是：目标模型在位置 \(i\) 的隐状态 \(h_i\) 已经「看过」token \(t_i\)，它蕴含的是「接下来会生成什么」的信息。因此草稿模型的每一步不吃 \((h_i, t_i)\)（信息冗余），而是吃 \((h_i, t_{i+1})\)——用目标已经给出的下一个真实 token 配目标的隐状态，去预测**再下一个** token \(t_{i+2}\)。这就是「隐状态 \(i\) 配 token \(i+1\)」的错位输入约定。

这个错位贯穿三条线：

1. **输入侧**：第 0 步 TTT 中，query 位置 \(i\) 的注意力输入是 `concat(LN(embed(t_{i+1})), LN(fc(h_i)))`。
2. **目标侧**：位置 \(i\) 的监督目标是教师模型对 token \(t_{i+2}\) 的分布（教师 logits 位置 \(i+1\) 做过 softmax）。
3. **RoPE 侧**：位置 \(i\) 的旋转位置编号保持 \(i\)（不跟着 token \(i+1\) 错开），每个 TTT 步整体 +1。

为什么 RoPE 可以「保持 \(i\)」？因为旋转位置编码只通过**相对距离**起作用，而草稿侧的 Q 和全部 KV（都在草稿自己的 DynamicCache 里）使用同一套编号体系，整体平移不改变任何一对 (query, key) 的相对距离。真实 token 下它对应 \(t_{i+1+k}\)，编号却是 \(i+k\)，差 1 是自洽且无害的。

#### 4.1.2 核心流程

设序列长度 \(T\)，TTT 步编号 \(k = 0, 1, \dots, K-1\)（\(K\) = `ttt_length`，默认 7）。用 `←` 表示「左移一位」：

```
索引:            0     1     2     3     4    ...
input_ids:       t0    t1    t2    t3    t4
target_hidden:   h0    h1    h2    h3    h4         ← 第 0 步的 hidden_states 输入

第 k=0 步:
  输入 token:     t1    t2    t3    t4    [pad0]    ← current_input_ids = input_ids ←
  预测目标:       p(t2) p(t3) p(t4) p(t5)  —        ← teacher 分布，错位 +2
  RoPE 位置:       0     1     2     3     4

第 k=1 步:
  hidden_states = 第 0 步输出的残差流（不再是 target_hidden）
  输入 token:     t2    t3    t4   [pad0] [pad0]    ← 再左移一位
  预测目标:       p(t3) p(t4) p(t5)  —      —
  RoPE 位置:       1     2     3     4     5        ← 每步整体 +1

第 k 步（一般式）:
  输入:  (draft_hidden_i^{k-1}, embed(t_{i+1+k}))   ← 链式：吃自己上一步的隐状态
  目标:  teacher 对 t_{i+2+k} 的分布
  RoPE:  位置 i 的编号 = i + k
```

三条同步滑动的线：`current_input_ids` 每步左移、`position_mask`（监督掩码）每步左移、目标分布切片起点每步 +1。序列末尾的位置随滑动逐渐失去监督目标，掩码左移补进的 0 恰好把它们关掉。

#### 4.1.3 源码精读

**左移工具**。[deepspec/modeling/eagle3/loss.py:45-54](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L45-L54) 定义了 `_shift_with_zero_padding`：`left=False` 分支返回 `cat((tensor[:, 1:], zeros), dim=1)`，即输出位置 \(i\) 取输入位置 \(i+1\)（左移），末尾补零。`compute_eagle3_loss` 第 371 行用它做第一次移位：

```python
current_input_ids = _shift_with_zero_padding(input_ids, left=False)
```

见 [deepspec/modeling/eagle3/loss.py:361-371](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L361-L371)——把 batch 的 `input_ids`/`attention_mask` 转成 long、构造 `base_position_ids = arange(T)`，然后左移出 `current_input_ids`。

**监督掩码的初值**。[deepspec/modeling/eagle3/loss.py:76-88](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L76-L88) 的 `_build_next_token_position_mask` 把 `loss_mask` 复制为 float，再把每个序列**最后一个有效 token** 位置清零——注释写明原因：「Last valid tokens have no next-token target inside the cached sequence」，即末 token 的下一个 token 不在缓存里，无从监督。

**软目标的错位与均匀尾巴**。[deepspec/modeling/eagle3/loss.py:91-103](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L91-L103) 的 `_build_padded_next_token_target_probs`：对教师 logits 做 softmax（教师位置 \(j\) 的分布预测 token \(j+1\)），然后取 `target_probs[:, 1:, :]` 左移一位并拼上均匀分布尾巴：

```python
uniform_tail = target_probs.new_full((B, ttt_length + 1, V), 1.0 / V)
return torch.cat((target_probs[:, 1:, :], uniform_tail), dim=1).detach()
```

左移后位置 \(i\) 是教师对 token \(i+2\) 的分布；尾巴保证后续每个 TTT 步的切片 `[:, k:k+T,:]` 长度恒为 \(T\) 不越界。`detach()` 确保教师分布只是常量目标，不回传梯度。

**输入侧的拼接**。错位输入的「token \(i+1\)」与「隐状态 \(i\)」在 decoder 层里拼成 2H，见 [deepspec/modeling/eagle3/qwen3/modeling.py:183-187](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L183-L187)：残差流先过 `hidden_norm`，embedding 过 `input_layernorm`，再 `cat` 到一起喂给注意力。注意 `hidden_states` 是**未过最终 norm 的残差流**（norm 只发生在 `compute_logits` 里，见 [deepspec/modeling/eagle3/qwen3/modeling.py:268-269](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L268-L269)），所以 TTT 链下一步直接把它作为 `hidden_states` 输入是自洽的。

**fc 投影只发生在第一步**。第一轮的 `hidden_states` 是 5H 拼接特征，后续轮是 H 维残差流。[deepspec/modeling/eagle3/qwen3/modeling.py:344-346](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L344-L346) 用宽度判断自动区分：末维等于 `5 × hidden_size` 时才过 `fc` 投影。

**RoPE 位置保持 i、每步 +1**。[deepspec/modeling/eagle3/qwen3/modeling.py:374-380](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L374-L380)：损失函数每轮都传同一个 `base_position_ids`（arange），`rope_cache_step_offset=True` 时加上 `past_seen_tokens // q_len`，即步 \(k\) 的 RoPE 编号为 \(i + k\)。断言 `past_seen_tokens % q_len == 0` 要求 TTT 块定长（每步 q_len 都是 \(T\)）。

#### 4.1.4 代码实践

**实践目标**：用纯 PyTorch（CPU 即可）验证三条滑动线的错位关系，把 4.1.2 的示意图落到代码上。

**操作步骤**（示例代码，非项目自带脚本；三个函数逐字取自 loss.py，避免 import 整个模块时连带加载 triton）：

```python
# shift_check.py（示例代码）
import torch

def shift(tensor, left=False):                      # 来自 loss.py:45-54
    zero_padding = torch.zeros_like(tensor[:, -1:])
    if left:
        return torch.cat((zero_padding, tensor[:, :-1]), dim=1)
    return torch.cat((tensor[:, 1:], zero_padding), dim=1)

def next_token_position_mask(loss_mask, attention_mask):  # 来自 loss.py:76-88
    position_mask = loss_mask.to(torch.float32).clone()
    seq_lengths = attention_mask.to(torch.long).sum(dim=-1)
    idx = torch.arange(position_mask.shape[0])
    position_mask[idx, (seq_lengths - 1).clamp_min(0)] = 0
    return position_mask.unsqueeze(-1)

T = 8
input_ids = torch.arange(100, 100 + T).unsqueeze(0)      # [t0..t7]
loss_mask = torch.tensor([[0, 0, 1, 1, 1, 1, 1, 0]])     # 位置 2..6 是 assistant 区间
attention_mask = torch.tensor([[1, 1, 1, 1, 1, 1, 1, 0]]) # 真实长度 7

cur = shift(input_ids, left=False)
print("step0 输入 token:", cur.tolist())
mask = next_token_position_mask(loss_mask, attention_mask)
print("step0 掩码:", mask.squeeze(-1).tolist())

for k in range(1, 4):
    cur = shift(cur, left=False)
    mask = shift(mask.squeeze(-1), left=False).unsqueeze(-1)
    print(f"step{k} 输入 token:", cur.tolist())
    print(f"step{k} 掩码:      ", mask.squeeze(-1).tolist())
```

**需要观察的现象**：

- `step0 输入 token` 是 `[t1, t2, ..., t7, 0]`——位置 \(i\) 放的是 token \(i+1\)。
- `step0 掩码` 在位置 6 被清零（最后一个有效 token，虽然 `loss_mask[6]=1`），有效监督位是 2..5。
- 每多一步，掩码整体左移，右侧补 0：序列末尾滑出的位置自动失去监督。

**预期结果**（待本地验证）：step3 时掩码只剩 `[0,0,0,0,0,1,0,0]`——同一个序列能提供的监督位置随 TTT 步数递减。

#### 4.1.5 小练习与答案

**练习 1**：TTT 第 3 步（`step_idx=3`），query 位置 5 的注意力输入中，配对进来的 token 是哪个？监督目标分布对应哪个 token？

**答案**：输入 token 是 \(t_{5+1+3} = t_9\)（`current_input_ids` 左移了 4 次）；监督目标是教师对 token \(t_{5+2+3} = t_{10}\) 的分布（目标切片起点 3 + 位置 5，再错位 +2）。

**练习 2**：为什么 RoPE 编号可以比真实 token 位置统一少 1 而不影响正确性？

**答案**：RoPE 只通过 query 与 key 的相对位置差起作用。草稿侧所有 Q/KV（包括 DynamicCache 里的历史步）共用同一套「\(i+k\)」编号，整体平移不改变任何相对距离；差 1 是常数偏移，自洽。

**练习 3**：`_build_next_token_position_mask` 为什么要对每个序列把最后一个有效 token 清零？

**答案**：该位置的「下一个 token」不在缓存序列内，没有监督目标；不清零会拿均匀尾巴或越界数据当目标。

### 4.2 compute_eagle3_loss：TTT 循环与逐步衰减软 CE

#### 4.2.1 概念说明

`compute_eagle3_loss` 是 Eagle3 的损失总入口，由 trainer 的 `run_batch` 调用。它把「一条缓存样本」变成「一个标量损失」，中间执行完整的 TTT 链。与 DSpark 的三项加权损失（CE + L1 蒸馏 + 置信度 BCE，u4-l4）不同，Eagle3 只有一种损失——**对教师分布的软交叉熵**——但它在 \(K\) 个 TTT 步上各算一次，再用几何衰减加权求和：

\[
\mathcal{L}_{\text{step}}^{(k)} = -\frac{1}{BT}\sum_{i \in \text{valid}_k} \sum_{v} p_{\text{target}}^{(k)}(i,v)\,\log \operatorname{softmax}\!\left(z^{(k)}(i,\cdot)\right)_v
\]

\[
\mathcal{L} = \sum_{k=0}^{K-1} \gamma^k \,\mathcal{L}_{\text{step}}^{(k)}, \qquad \gamma = \texttt{step\_loss\_decay} = 0.8,\ K = 7
\]

直觉：推理时草稿链的第 1 个 token 最容易被目标接受、越往后越容易跑偏，所以早期步的监督要重、晚期步要轻。\(0.8^k\) 让第 6 步的权重降到约 0.26，与「链式提议的边际价值递减」匹配。这和 DSpark 里槽位衰减 \(e^{-t/\gamma}\)（u4-l4）是同一思想在不同算法形态下的实现——DSpark 衰减的是**块内槽位**，Eagle3 衰减的是**TTT 链步数**。

归一化用 `local_mean`：每步除以固定的 \(B \times T\)（本地 batch × 序列长），而不是除以有效 token 数。注释里明确说明 `valid_token_mean` 被禁用的原因：全局 token 计数会让长序列主导梯度，评测接受长度会随训练退化（[deepspec/modeling/eagle3/loss.py:57-73](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L57-L73)）。配置里 `local_batch_size=1`，所以每条序列权重相等。

#### 4.2.2 核心流程

```
compute_eagle3_loss(model, batch, ttt_length=K, step_loss_decay=γ):
    # ── 准备阶段 ──
    current_input_ids = input_ids 左移一位                 # 错位输入
    position_mask_0   = loss_mask 去掉每序列最后有效 token
    target_probs      = softmax(lm_head(target_last_hidden))
                        左移一位 + 均匀尾巴, detach          # 错位目标
    position_masks[k] = position_mask_0 左移 k 次           # 掩码同步滑动
    normalizers[k]    = B * T                               # local_mean
    cache = DynamicCache()                                  # 草稿自己的 KV
    total_loss = 0

    # ── TTT 主循环（K 次 forward）──
    for k in 0 .. K-1:
        out = model(hidden_states, current_input_ids,
                    position_ids=arange(T), past_key_values=cache,
                    use_cache=True, return_logits=True,
                    rope_cache_step_offset=True)
        step_loss = FusedLogSoftmaxLoss(out.draft_logits,
                                        target_probs[:, k:k+T, :],
                                        position_masks[k],
                                        normalizers[k])
        total_loss += step_loss * γ**k                      # 衰减加权
        记录 ploss_k / accuracy@k / accept_rate@k 指标
        hidden_states      = out.hidden_states              # 链式：吃自己上一步
        current_input_ids  = 再左移一位
    记录 tau_greedy / tau_probabilistic / loss
    return total_loss
```

注意力的可见性（u5-l1 已讲，此处只回顾结论）：query 位置 \(i\) 在第 \(k\) 步能看到第 0 步中 \(j \le i\) 的位置（首块因果）以及第 1..k-1 步中同索引 \(i\) 的位置（同索引对角线），由 [deepspec/modeling/eagle3/common.py:116-126](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L116-L126) 的 `eagle3_mask_mod` 编译成块稀疏掩码。

#### 4.2.3 源码精读

**准备阶段**。[deepspec/modeling/eagle3/loss.py:354-397](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L354-L397)：先在 `torch.no_grad()` 下用 `target_logits_only=True` 走一条「只过 lm_head」的捷径拿教师 logits（对应 [deepspec/modeling/eagle3/qwen3/modeling.py:339-342](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L339-L342)，`lm_head(target_last_hidden_states)`），转成软目标后立刻 `del target_logits` 释放显存。随后循环构造 K 个逐步左移的 `position_masks`，并算出每步的 normalizer：

```python
loss_normalizers = _compute_loss_normalizers(position_masks=position_masks)
```

**TTT 主循环**。[deepspec/modeling/eagle3/loss.py:402-443](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L402-L443)。每轮四个动作：

1. 目标切片对齐（404-408 行）：`target_probs[:, step_idx : step_idx + seq_len, :]`，注释提醒与 Eagle3 参考实现保持切片对齐。
2. 草稿前向（410-419 行）：传入当前的 `hidden_states`、`current_input_ids`、不变的 `base_position_ids`、累积的 `past_key_values`，并开 `rope_cache_step_offset=True`。
3. 软交叉熵（430-435 行）：`FusedLogSoftmaxLoss.apply(draft_logits, target_step_probs, position_mask_step, loss_normalizers[step_idx])`。
4. 衰减加权与状态推进（436-444 行）：

```python
add_metric(f"ploss_{step_idx}", step_loss.detach(), reduction="dp_mean", tag="train")
step_weight = float(step_loss_decay) ** step_idx
total_loss = total_loss + step_loss * step_weight
current_input_ids = _shift_with_zero_padding(current_input_ids, left=False)
```

`hidden_states = output.hidden_states`（420 行）把残差流喂给下一轮，`DynamicCache` 在前向内部逐层 `update`，每步追加 \(T\) 个 KV。

**超参从哪来**。`ttt_length` 与 `step_loss_decay` 是 model config 的必填字段：[deepspec/modeling/eagle3/qwen3/modeling.py:213-222](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/modeling.py#L213-L222) 在 `__init__` 里断言存在并存成实例属性；[deepspec/modeling/eagle3/qwen3/config.py:14-20](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/qwen3/config.py#L14-L20) 的 `build_draft_config` 校验 `ttt_length >= 1`、`step_loss_decay > 0`。训练配置的实值见 [config/eagle3/eagle3_qwen3_4b.py:10-16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py#L10-L16)：`ttt_length=7`、`step_loss_decay=0.8`、`draft_num_hidden_layers=1`。

**返回值与指标**。循环结束后调用 `_log_eagle3_prefix_metrics` 记录前缀指标，把 `total_loss` 记为 `loss` 指标后返回（[deepspec/modeling/eagle3/loss.py:446-452](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L446-L452)）。

#### 4.2.4 代码实践

**实践目标**：验证 `ttt_length=7` 时损失确实是 7 个带衰减权重的软 CE 项，并观察监督位置随步数滑出的定量规律。

**操作步骤**（示例代码，非项目自带脚本；用假 forward 替代真实模型，CPU 可跑）：

```python
# ttt_weight_check.py（示例代码）
import torch

def shift(t):                                          # 左移一位
    return torch.cat((t[:, 1:], torch.zeros_like(t[:, -1:])), dim=1)

T, K, gamma = 8, 7, 0.8
input_ids  = torch.arange(100, 100 + T).unsqueeze(0)
loss_mask  = torch.tensor([[0, 0, 1, 1, 1, 1, 1, 0]])
attn       = torch.tensor([[1, 1, 1, 1, 1, 1, 1, 0]])

mask0 = loss_mask.float().clone()
mask0[0, attn.sum(-1).item() - 1] = 0                   # 末有效 token 清零

cur, mask, weights, valid_counts = shift(input_ids), mask0, [], []
for k in range(K):
    weights.append(gamma ** k)
    valid_counts.append(int(mask.sum()))
    # 这里用假 forward：真实代码换成 model(hidden_states, cur, ...)
    cur, mask = shift(cur), shift(mask)
    mask = mask * attn                                   # 滑出真实长度的位置也无效

print("步权重:", [round(w, 6) for w in weights])
print("权重和:", round(sum(weights), 6))                # Σ 0.8^k, k=0..6
print("每步有效监督位置数:", valid_counts)
```

**需要观察的现象**：

- `weights` 恰有 7 项：`[1.0, 0.8, 0.64, 0.512, 0.4096, 0.32768, 0.262144]`。
- `valid_counts` 逐步递减，体现「TTT 越深，能监督的位置越少」。
- 把 `loss_mask` 换成全 1（假设整条都是 assistant），观察递减斜率的变化。

**预期结果**（待本地验证）：权重和 ≈ 3.951424，等于等比级数 \((1-0.8^7)/(1-0.8)\)。

#### 4.2.5 小练习与答案

**练习 1**：手算 `step_loss_decay=0.8`、`ttt_length=7` 时的总权重 \(\sum_{k=0}^{6} 0.8^k\)；若把 decay 改成 1.0 呢？

**答案**：\((1-0.8^7)/(1-0.8) = (1-0.2097152)/0.2 = 3.951424\)。decay=1.0 时各步等权，总权重为 7，即不再区分「早期步重要、晚期步次要」。

**练习 2**：为什么 `_compute_loss_normalizers` 里断言禁用 `valid_token_mean`？

**答案**：`valid_token_mean` 用全局有效 token 数做分母，长序列贡献的 token 多、梯度权重大，会主导优化方向；实测会使评测接受长度随训练退化。`local_mean` 除以固定的 \(B \times T\)（`local_batch_size=1` 时即序列数），每条序列权重相等（[deepspec/modeling/eagle3/loss.py:62-69](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L62-L69) 的注释）。

**练习 3**：序列足够长时，位置 0 最多参与几步损失？位置 \(i\) 呢？

**答案**：`position_masks[k][i] = mask0[i+k]`，所以位置 \(i\) 在第 \(k\) 步是否被监督取决于 `mask0[i+k]`。位置 0 参与 K=7 步（若 `mask0[0..6]` 均为 1）；越靠右的位置参与步数越少，序列末端的位置一步都不参与。

### 4.3 FusedLogSoftmaxLoss 与训练指标：省显存的软交叉熵

#### 4.3.1 概念说明

软交叉熵的朴素 PyTorch 实现（`log_softmax` 后与目标分布点积）会在 autograd 计算图里保留 \([B, T, V]\) 的 fp32 log-probs 张量。Qwen3 词表约 15 万，\(T=4096\) 时单个张量就是 \(B \times 4096 \times 151936 \times 4\) 字节；Eagle3 的 TTT 要连做 7 步，显存直接爆掉。

`FusedLogSoftmaxLoss` 用 Triton 把前向和反向各写成一个融合 kernel，autograd 跨步只保留 `logits.detach()` 和每行两个 fp32 标量（在线 log-sum-exp 的最大值 \(m\) 与指数和 \(d\)），**从不物化** \([B,T,V]\) 的 fp32 log-probs（[deepspec/modeling/eagle3/loss.py:1-13](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L1-L13) 的模块 docstring 写明了这一点，并注明改编自 SpecForge / Liger-Kernel / Unsloth）。文件头还交代了来源：这是 SpecForge 的 `LogSoftmaxLoss` 移植（Apache-2.0）。

配套的指标函数让训练日志能直接回答「草稿链每一步质量如何」：

- 逐步指标：`accuracy@k`（贪心 argmax 是否与教师一致）、`accept_rate@k`、`valid_tokens@k`。
- 前缀指标：`tau_greedy` / `tau_probabilistic`——链式贪edy/概率意义下的期望接受长度。

#### 4.3.2 核心流程

前向 kernel 对每一行（共 \(B \times T\) 行）做两轮分块扫描：

1. **第一轮（在线 log-sum-exp）**：按 `BLOCK_SIZE` 分块扫词表，维护运行最大值 \(m\) 与重整化的指数和 \(d\)：

\[
d \leftarrow d \cdot e^{m - m_{\text{new}}} + \sum_{\text{block}} e^{z - m_{\text{new}}}, \qquad m \leftarrow m_{\text{new}}
\]

2. **第二轮**：用 \(m, d\) 重构 `log_softmax`，累加 \(-\sum_v p_{\text{target}}(v)\,\log\operatorname{softmax}(z)_v\) 写入该行 loss。

被 `position_mask` 为 0 的行直接 `return`，loss 保持初始化的 0——这就是「masked 位置零损失」。最终标量 = 所有行 loss 之和除以 normalizer。

反向梯度（对 logits，含缩放 \(1/\text{normalizer}\) 与上游 \(\lambda\)）：

\[
\frac{\partial (\lambda \cdot \ell)}{\partial z_v} = -\lambda\, p_{\text{target}}(v) + \lambda \Big(\sum_{v'} p_{\text{target}}(v')\Big) \operatorname{softmax}(z)_v
\]

由于 \(\sum_{v'} p_{\text{target}}(v') = 1\)，第二项就是 \(\lambda \cdot \operatorname{softmax}(z)_v\)；kernel 里先累出 `target_grad_sum` 再减，写成通用形式。

accept_rate 的定义与 DSpark 完全同源（u4-l4）：

\[
a = 1 - \tfrac{1}{2}\|p_{\text{draft}} - p_{\text{target}}\|_1 = 1 - \mathrm{TV}(p, q)
\]

前缀指标用 cumprod 表达「链式全部被接受」：

\[
\tau_{\text{greedy}} = 1 + \sum_{k} \prod_{j \le k} \mathbb{1}[\text{step } j \text{ 贪心正确}], \qquad
\tau_{\text{prob}} = 1 + \sum_{k} \prod_{j \le k} a_j
\]

（+1 是目标模型每轮兜底提交的那个 token。）

#### 4.3.3 源码精读

**autograd Function 的合同**。[deepspec/modeling/eagle3/loss.py:282-292](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L282-L292) 的 docstring 说清了三件事：返回 \(\sum_i(-\sum_v p \cdot \log\operatorname{softmax})/\text{normalizer}\)、masked 行贡献零、**反向把梯度原地写进 logits 存储并把它当梯度返回**——这是 Liger/Unsloth 的省显存模式，调用方在 backward 之后不能再读 logits。

**forward**。[deepspec/modeling/eagle3/loss.py:294-325](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L294-L325)：展平成 \(B \times T\) 行、按词表算 `BLOCK_SIZE` 与 `num_warps`（[deepspec/modeling/eagle3/loss.py:23-42](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L23-L42)，大词表分块循环，不必 `BLOCK_SIZE == next_pow2(V)`），launch 前向 kernel，`ctx.save_for_backward(logits.detach(), target_p, position_mask, m, d)`，返回 `loss.sum() / normalizer`。

**前向 kernel 的在线 log-sum-exp**。[deepspec/modeling/eagle3/loss.py:187-221](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L187-L221)：第一轮循环更新 \(m, d\)（195-197 行的重整化更新），第二轮重构 `log_softmax` 并累加目标加权的对数概率，最后存 `-loss`、\(m\)、\(d\)。

**backward kernel**。[deepspec/modeling/eagle3/loss.py:224-279](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L224-L279)：masked 行直接把梯度清零写回；有效行先累 `target_grad_sum`（258-265 行），再算 `grad = -(target * grad_output - softmax * target_grad_sum)` 原地写入 logits（276-279 行）。Python 侧 [deepspec/modeling/eagle3/loss.py:327-351](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L327-L351) 把这个被改写的张量作为 logits 的梯度返回，其余输入的梯度为 `None`。

**逐步指标**。[deepspec/modeling/eagle3/loss.py:106-134](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L106-L134)：贪心正确掩码、`accept_rate = 1 - 0.5*|p-q|_1`（clamp 到 [0,1] 再乘有效掩码），以 `accuracy@k` / `accept_rate@k`（ratio 型，u3-l6 的分子分母累积）与 `valid_tokens@k`（`dp_sum`）上报。

**前缀指标**。[deepspec/modeling/eagle3/loss.py:137-158](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L137-L158)：把 K 步的掩码 stack 起来，`cumprod(dim=0)` 得到「链式前缀全对/全接受」，求和后 +1 上报 `tau_greedy` / `tau_probabilistic`。训练时盯着 `tau_probabilistic` 就能预判评测端的接受长度走势。

#### 4.3.4 代码实践

**实践目标**：用朴素的 PyTorch 表达式验证融合 kernel 的数学等价性（不碰 Triton）。

**操作步骤**（示例代码，需 CUDA 环境；若无 GPU 则先在纸上推演并标注「待本地验证」）：

```python
# fused_loss_check.py（示例代码，需 CUDA）
import torch
from deepspec.modeling.eagle3.loss import FusedLogSoftmaxLoss

torch.manual_seed(0)
B, T, V = 1, 4, 50
logits = torch.randn(B, T, V, device="cuda", requires_grad=True)
target_probs = torch.softmax(torch.randn(B, T, V, device="cuda"), dim=-1)
position_mask = torch.tensor([[[1], [1], [0], [1]]], device="cuda", dtype=torch.float32)

fused = FusedLogSoftmaxLoss.apply(logits, target_probs, position_mask, normalizer=B * T)

ref_masked = (target_probs * torch.log_softmax(logits.float(), dim=-1)).sum(-1)
ref = -(ref_masked * position_mask.squeeze(-1)).sum() / (B * T)
print(fused.item(), ref.item(), (fused - ref).abs().item())
```

**需要观察的现象**：`fused` 与 `ref` 的差在 1e-6 量级（数值路径不同导致的浮点误差）；被 mask 的位置 2 改成 1 时 loss 变大。

**预期结果**（待本地验证）：两者一致；backward 后再读 `logits` 会得到被梯度覆盖的值——这正是 docstring 警告的行为，值得顺手观察一次。

#### 4.3.5 小练习与答案

**练习 1**：为什么反向只需要 `logits.detach()` 加每行两个标量 \(m, d\)？

**答案**：softmax 概率可以由 \(\exp(z - m)/d\) 从原始 logits 重构，不需要保存任何 \([B,T,V]\) 中间张量；\(m\)、\(d\) 就是前向在线 log-sum-exp 的副产品。

**练习 2**：`FusedLogSoftmaxLoss` 反向后为什么不能读 `logits`？

**答案**：backward 把梯度**原地写进** logits 的存储并把该张量作为梯度返回（Liger/Unsloth 模式），原值已被覆盖（[deepspec/modeling/eagle3/loss.py:287-291](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L287-L291)）。

**练习 3**：`tau_probabilistic` 与投机解码加速比是什么关系？

**答案**：它是「每轮验证平均提交的 token 数」的训练期估计——链上各步接受概率的连乘之和再 +1（目标模型兜底 token）。接受长度越长，目标模型调用次数越少，加速比越高（u1-l1）。

### 4.4 eagle3_trainer：复用 BaseTrainer 的训练器

#### 4.4.1 概念说明

u3-l1 介绍过 BaseTrainer 的模板方法模式：骨架负责分布式初始化、FSDP 包装、梯度累积、检查点与日志，算法差异全部下放到两个钩子——`_build_draft_model`（造什么模型）和 `run_batch`（一个 batch 怎么变成 loss）。`Qwen3Eagle3Trainer` 就是这个模式的又一个用户：

- **`_build_draft_model`**：用 `build_qwen3_eagle3_config` 从目标 config 派生草稿 config，实例化 `Qwen3Eagle3Model`。
- **`run_batch`**：把 batch 整个交给 `compute_eagle3_loss`，超参（`ttt_length`、`step_loss_decay`）从 `self.draft_model` 的实例属性读取。

它还额外覆写了 `build_models`。值得如实指出：**这个覆写与 BaseTrainer 的默认实现逐行等价**（都是「建草稿 → 上 GPU → 从 CPU 上的目标模型拷贝并冻结 embedding/lm_head → 释放目标模型」），差异只在注释——Eagle3 版注明「draft head 与 norm 保持冻结/与目标无关，以对齐 DSpark 的设定：head 不训练、norm 不继承」。可把它理解为承载说明文档与未来扩展点的显式覆写；真正必须实现的仍只有两个抽象钩子。

`Gemma4Eagle3Trainer` 进一步只换 `_build_draft_model`（改用 Gemma4 的 config 构建器与模型类），与 DSpark 侧的 `Gemma4DSparkTrainer` 手法完全一致（u4-l5 的「算法超参原样跨族迁移」）。

#### 4.4.2 核心流程

```
train.py 解析配置（config/eagle3/eagle3_qwen3_4b.py）
  └─ trainer_cls = Qwen3Eagle3Trainer（配置里直接存放类，u1-l4）
       └─ BaseTrainer.__init__
            ├─ self.draft_model, self.tokenizer = self.build_models()   # 多态：Eagle3 覆写版
            │    ├─ _build_draft_model → build_qwen3_eagle3_config + Qwen3Eagle3Model
            │    └─ initialize_embeddings_and_head(freeze=True)          # 冻结 embed/lm_head
            ├─ torch.compile（本配置 torch_compile=False，跳过）
            ├─ FSDP 包装 → self.model
            └─ CacheDataset + validate_train_cache + 日程 + BF16Optimizer
       └─ train() 主循环（u3-l2）
            └─ loss = self.run_batch(batch) / gradient_accumulation_steps
                 └─ compute_eagle3_loss(model=self.model, batch, ttt_length, step_loss_decay)
```

注意 `run_batch` 里 forward 走 `self.model`（可能被 compile/FSDP 包装），而 `ttt_length` 等纯 Python 属性从 `self.draft_model`（裸模型）读取——避免经过包装层取属性。

#### 4.4.3 源码精读

**类骨架**。[deepspec/trainer/eagle3_trainer.py:16-17](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/eagle3_trainer.py#L16-L17)：`class Qwen3Eagle3Trainer(BaseTrainer)`，类属性 `data_collator_cls = CacheCollator`——Eagle3 与 DSpark 消费同一种目标缓存格式，数据侧零改动。

**build_models 覆写**。[deepspec/trainer/eagle3_trainer.py:19-52](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/eagle3_trainer.py#L19-L52)：加载 tokenizer 与目标 config → `self._build_draft_model(...)` → 草稿模型搬上 GPU（`dtype=self.precision_dtype`）→ 从 CPU 上 `.eval()` 的目标模型取 `get_input_embeddings()`/`get_output_embeddings()` → `initialize_embeddings_and_head(..., freeze=True)` → `del target_model`。与基类默认版（[deepspec/trainer/base_trainer.py:251-282](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L251-L282)）逐行对照，仅注释不同。`__init__` 里的调用点在 [deepspec/trainer/base_trainer.py:175-188](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L175-L188)（随后可选 compile、FSDP 包装）。

**run_batch**。[deepspec/trainer/eagle3_trainer.py:61-67](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/eagle3_trainer.py#L61-L67)：

```python
def run_batch(self, batch):
    return compute_eagle3_loss(
        model=self.model,
        batch=batch,
        ttt_length=int(self.draft_model.ttt_length),
        step_loss_decay=float(self.draft_model.step_loss_decay),
    )
```

主循环对它的调用在 [deepspec/trainer/base_trainer.py:373-380](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L373-L380)：`loss = self.run_batch(batch) / self.gradient_accumulation_steps` 后 `loss.backward()`，梯度累积与 `no_sync` 的语义 u3-l2 已讲，这里不重复。

**与 DSpark run_batch 的输入差异**。对照组 [deepspec/trainer/dspark_trainer.py:25-39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L25-L39)：

| 维度 | DSpark `run_batch` | Eagle3 `run_batch` |
| --- | --- | --- |
| 前向次数 | 1 次（模型内部采样锚点、整块构图） | 7 次（`compute_eagle3_loss` 内 TTT 循环） |
| 喂给模型的输入 | `input_ids`、`target_hidden_states`、`loss_mask`、`target_last_hidden_states` 一次给足 | 每步给逐步左移的 `current_input_ids`、固定 `position_ids`、递增的 `DynamicCache`；`loss_mask` 不进模型，只用来造损失掩码 |
| 教师信号 | `aligned_target_logits`（模型输出的一部分） | `target_logits_only=True` 单独走一次 lm_head，转软目标后 `detach` |
| 损失组成 | 三项 alpha 加权（CE + L1 + 置信度 BCE） | 单一软交叉熵，靠 `step_loss_decay^k` 加权 |
| 权重衰减对象 | 块内槽位 \(e^{-t/\gamma}\) | TTT 链步数 \(0.8^k\) |
| 归一化 | 分母跨 rank all_reduce | `local_mean`（本地 \(B \times T\)），指标用 `dp_mean`/`dp_sum` 归约 |

**Gemma4 变体**。[deepspec/trainer/eagle3_trainer.py:70-76](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/eagle3_trainer.py#L70-L76)：`Gemma4Eagle3Trainer(Qwen3Eagle3Trainer)` 只覆写 `_build_draft_model`，换成 `build_gemma4_eagle3_config` 与 `Gemma4Eagle3Model`。`build_models` 与 `run_batch` 原样继承——Eagle3 的算法逻辑天然模型无关。

**输出合同**。循环里模型返回的 `Eagle3ForwardOutput` 只有三个字段（[deepspec/modeling/eagle3/common.py:13-17](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/common.py#L13-L17)）：`hidden_states`（链式传递）、`draft_logits`（进损失）、`target_logits`（可选）。与 DSpark 的 `DSparkForwardOutput` 相比字段少得多——没有 markov、置信度相关输出，损失侧也因此简单。

#### 4.4.4 代码实践

**实践目标**：通过对照阅读量化两个训练器在 `run_batch` 上的差异，检验自己对「同一骨架、两种算法」的理解。

**操作步骤**（源码阅读型实践，不需要 GPU）：

1. 打开 [deepspec/trainer/dspark_trainer.py:14-39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L14-L39) 与 [deepspec/trainer/eagle3_trainer.py:16-67](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/eagle3_trainer.py#L16-L67) 并排对照。
2. 逐项填写 4.4.3 的差异表，并给每一项找到行号证据。
3. 用 Grep 在仓库里搜 `compute_eagle3_loss` 与 `compute_dspark_loss` 的全部调用点，确认它们各自只被本算法的 trainer 调用。
4. 检查 [config/eagle3/eagle3_qwen3_4b.py:18-31](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/eagle3/eagle3_qwen3_4b.py#L18-L31) 的 `train` 段：找出 `trainer_cls=Qwen3Eagle3Trainer` 与 `torch_compile=False`，并对比 [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) 同段的设置。

**需要观察的现象**：两个 trainer 的公共样板（import、类声明、`data_collator_cls`）高度一致；差异集中在「调哪个损失函数、给它什么超参」。

**预期结果**（待本地验证）：能说出 Eagle3 版 `run_batch` 的 4 个独有事实——不直接调 `self.model` 而是交给损失函数、`ttt_length`/`step_loss_decay` 取自裸模型实例、batch 的 `loss_mask` 不进模型 forward、教师 logits 在损失函数内单独计算。

#### 4.4.5 小练习与答案

**练习 1**：`Qwen3Eagle3Trainer` 必须实现 BaseTrainer 的哪些抽象钩子？`build_models` 是必须的吗？

**答案**：必须实现 `_build_draft_model` 和 `run_batch`（基类分别 `raise NotImplementedError`，[deepspec/trainer/base_trainer.py:284-285](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L284-L285) 与 [316-317](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L316-L317)）。`build_models` 有默认实现，不是必须；Eagle3 的覆写当前与默认版逻辑等价，只差注释。

**练习 2**：DSpark 的 `run_batch` 把 `loss_mask` 传进模型，Eagle3 却不传。为什么？

**答案**：DSpark 在模型 forward 内部用 `loss_mask` 采样锚点、构造训练构图（u4-l1/u4-l2）；Eagle3 的构图由 TTT 循环驱动，`loss_mask` 只在损失侧用于构建 `position_mask`，模型 forward 不需要它。

**练习 3**：`Gemma4Eagle3Trainer` 为什么只需 7 行？

**答案**：它继承 `Qwen3Eagle3Trainer`，`build_models`、`run_batch`、`compute_eagle3_loss` 全部模型无关；唯一的族差异（config 派生规则与模型类）收拢在 `_build_draft_model` 一个钩子里。

## 5. 综合实践

把本讲内容串成一张图加一段伪代码（对应本讲规格的实践任务）。

**任务 A：画 `compute_eagle3_loss` 张量流图**。对照 [deepspec/modeling/eagle3/loss.py:354-452](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py#L354-L452) 手绘或用 Mermaid 画出张量流，节点必须包含以下检查点（画完后逐项核对）：

```
batch 五字段
 ├─ target_last_hidden_states ──lm_head(无梯度)──► target_logits ──softmax+左移+均匀尾──► target_probs
 ├─ loss_mask + attention_mask ──去末token──► position_mask_0 ──逐步左移──► position_masks[k]
 ├─ input_ids ──左移──► current_input_ids（每步再左移）
 ├─ target_hidden_states ─┐
 └─ attention_mask ───────┤
                          ▼
   ┌──────────── TTT 循环 k = 0..6 ────────────┐
   │ fc 投影(仅 k=0) → decoder 层 → draft hidden ─┬─► lm_head(norm) ──► draft_logits
   │ DynamicCache(k×T 个 KV)                     │      + position_masks[k] + target_probs[k:k+T]
   │ RoPE 编号 = i + k                           └─► FusedLogSoftmaxLoss → step_loss
   └────────────────── × 0.8^k 累加 ──────────────────────► total_loss
                                        同时: accuracy@k / accept_rate@k / ploss_k
   循环后: cumprod → tau_greedy / tau_probabilistic
```

**任务 B：伪代码验证 7 个衰减 CE 项**。把 4.2.4 的脚本扩展成「假模型」版本：用一个返回固定随机 logits 的函数代替 `model(...)`，完整跑通 7 步循环，打印每步的 `step_weight`、有效位置数与加权 loss，最后核对 `sum(step_loss_k × 0.8^k) == total_loss`。

**验收标准**：不看讲义能向别人解释三个问题——(1) 第 k 步位置 i 的输入 token 与目标 token 各是哪个；(2) 为什么掩码要跟着左移；(3) DSpark 与 Eagle3 的 `run_batch` 差在哪。结果待本地验证。

## 6. 本讲小结

- **位置错位约定**：Eagle3 每步吃 \((h_i, t_{i+1})\) 预测 \(t_{i+2}\)；`current_input_ids`、`position_mask`、目标分布切片三条线随 TTT 步同步左移，序列末尾滑出的位置自动失去监督；RoPE 编号保持 \(i\)（每步整体 +1），靠相对距离自洽。
- **compute_eagle3_loss**：准备错位输入与软目标 → 7 次链式前向（DynamicCache 递增、吃自己上一步的残差流）→ 每步对教师分布做软交叉熵、乘 \(0.8^k\) 加权求和；`local_mean` 归一化（每序列等权）。
- **FusedLogSoftmaxLoss**：Triton 融合前反向，跨步只存 `logits.detach()` 与每行 \(m, d\) 两个标量，不物化 \([B,T,V]\) fp32 log-probs；backward 原地写梯度，之后不可再读 logits。
- **训练指标**：`accuracy@k`、`accept_rate@k`（= \(1-\mathrm{TV}(p,q)\)）逐步监控；`tau_greedy`/`tau_probabilistic` 用 cumprod 估计链式期望接受长度，是评测端加速比的训练期先行指标。
- **eagle3_trainer**：模板方法模式的又一次落地——`_build_draft_model` 与 `run_batch` 两个钩子承载全部算法差异，`build_models` 覆写与基类等价（只差注释）；Gemma4 版仅换一个钩子。

## 7. 下一步学习建议

- 下一讲 u5-l3（DFlash 配置化实现与三种算法对比）会把 DSpark/DFlash/Eagle3 三种算法放进同一张表：建议先自己整理「损失组成 × 提议形态 × 头结构」三维对比，再读该讲校对。
- 评估侧预告：本讲的 `tau_probabilistic` 将在 u6-l6（Eagle3 评估器）里变成真实的链式逐 token 提议——推理时草稿吃的是**自己采样**的 token（不再是 teacher forcing），这是训练与评估最关键的分布差异，值得带着这个问题去读。
- 源码延伸阅读：[deepspec/modeling/eagle3/loss.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/eagle3/loss.py) 的两个 Triton kernel 是学习「在线 log-sum-exp + 原地梯度」显存优化模式的好样本；配合 [deepspec/utils/metrics.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/metrics.py) 回顾 u3-l6 的指标归约语义。
