# u4-l3 Markov 头：基于前一 token 的逐步 logits 修正

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `VanillaMarkov` 中 `markov_w1` / `markov_w2` 构成的**低秩双线性 logits 偏置**，以及为什么不用全词表矩阵。
2. 说出 `apply_step_logits` 与 `apply_block_logits` 的分工：推理单步修正 vs 训练整块 teacher-forced 修正。
3. 手推 `sample_block_tokens` 的逐 token 迭代循环：`prev_token_ids` 如何从「锚点 token」变成「上一步刚采样的 token」。
4. 对比 `markov_rank=0`（DFlash 配置）关闭 markov 头后，训练与推理两条路径分别退化成什么。

## 2. 前置知识

本讲建立在前几讲的概念之上，先快速回顾：

- **块式半自回归（u4-l1、u4-l2）**：DSpark 草稿模型一次 forward 并行产出一个块内 `block_size=7` 个位置的 logits。块内所有槽位的输入都是 mask token，**主干隐状态在产出时并不知道同块前面槽位最终会放什么 token**——这是并行的代价：块内缺少 token 级的自回归依赖。
- **Markov 头就是要补上这块缺失**：在 logits 上加一个只依赖「前一个 token」的偏置项。它不能让块内完全自回归（那要串行 7 次 forward），但用极便宜的查表加一次线性投影，近似补回最邻近的一阶依赖。
- **teacher forcing（教师强制）**：训练时「前一个 token」直接用监督数据里的真实 token（`prev_token_ids` 由锚点 token 与右移的 `target_ids` 拼成）；推理时没有真值，只能用「上一步实际采出来的 token」，于是必须逐 token 迭代——这正是训练（并行、整块）与推理（串行、逐槽位）两种 API 并存的原因。
- **`sample_tokens`（u6 会再次用到）**：`temperature < 1e-5` 时取 `argmax`（贪心），否则按 softmax 概率 `multinomial` 采样。

一个术语提醒：**Markov（马尔可夫）** 指的是「下一状态只依赖前一状态」的一阶依赖假设。`VanillaMarkov` 是严格一阶、无记忆的；仓库里还提供了带门控的 `GatedMarkovHead` 和带循环状态的 `RNNHead` 两个扩展变体，本讲以 `VanillaMarkov` 为主，变体在 4.1.3 简述。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `deepspec/modeling/dspark/markov_head.py` | 本讲主角：`VanillaMarkov` / `GatedMarkovHead` / `RNNHead` 三个头与工厂函数 `build_markov_head` |
| `deepspec/utils/sampling.py` | 底层采样工具：`sample_tokens`（贪心 / 多项式采样）等 |
| `deepspec/modeling/dspark/qwen3/modeling.py` | 接线层：草稿模型 `__init__` 里构建 markov 头；训练 forward 里调用 `apply_block_logits`；推理侧封装 `sample_draft_tokens` |
| `deepspec/eval/dspark/draft_ops.py` | 评估侧调用点：`build_dspark_proposal` 里以锚点 token 作为 `first_prev_token_ids` 发起逐 token 采样 |
| `config/dspark/dspark_qwen3_4b.py` | DSpark 配置：`markov_rank=256`、`markov_head_type='vanilla'` |
| `config/dflash/dflash_qwen3_4b.py` | DFlash 配置：`markov_rank=0` 关闭 markov 头 |

## 4. 核心概念与源码讲解

### 4.1 VanillaMarkov：低秩双线性偏置

#### 4.1.1 概念说明

我们想让槽位 \( t \) 的 logits 依赖前一个 token \( x_{t-1} \)。最彻底的做法是维护一张 \( |V| \times |V| \) 的偏置表 \( B \)，直接加 \( B[x_{t-1}] \)。但 Qwen3 词表 \( |V| = 151936 \)，一张满表约有

\[
151936^2 \approx 2.3 \times 10^{10}
\]

即 230 亿个参数，比整个草稿模型还大几个数量级，不可行。

`VanillaMarkov` 的解法是**低秩分解**：把偏置表分解成「查表 + 投影」两步，

\[
\mathrm{bias}(x_{\mathrm{prev}}) = W_2 \, W_1[x_{\mathrm{prev}}], \qquad W_1 \in \mathbb{R}^{|V| \times r},\ W_2 \in \mathbb{R}^{r \times |V|}
\]

即先取前一个 token 的 rank-\(r\) 嵌入 \( W_1[x_{\mathrm{prev}}] \in \mathbb{R}^r \)，再线性投影回词表维度。参数量降为 \( 2|V|r \)。取默认配置 \( r = 256 \)：

\[
2 \times 151936 \times 256 \approx 7.8 \times 10^7
\]

约 7800 万参数，比满表缩小约 300 倍。直觉上这是一个**双线性形式**：修正后 logits 的第 \( v \) 维是 \( w_{2,v} \cdot w_1[x_{\mathrm{prev}}] \)（词 \(v\) 的投影向量与前一词嵌入的内积），本质上是把「大词表 × 大词表」的共现偏置压进一个 256 维的隐空间。

修正后的最终 logits 为：

\[
\mathrm{logits}' = \mathrm{logits}_{\mathrm{base}} + \mathrm{bias}(x_{\mathrm{prev}})
\]

其中 `logits_base` 是冻结的 `lm_head` 作用于草稿主干隐状态的结果（见 u4-l2 的 `compute_logits`）。注意 `lm_head` 冻结、markov 头可训练——markov 头是「在不动输出头的前提下给 logits 加可学习偏置」的轻量适配器。

#### 4.1.2 核心流程

`VanillaMarkov` 的调用层次（自底向上）：

```
markov_w1: nn.Embedding(V, r)      # 查表：前一个 token -> r 维嵌入
markov_w2: nn.Linear(r, V, bias=False)  # 投影：r 维嵌入 -> 词表维偏置

get_prev_embeddings(token_ids)  ->  W1[token_ids]          # [.., r]
project_bias(latent)            ->  W2(latent)             # [.., V]
compute_step_bias(token_ids, hidden_states)
        -> project_bias(get_prev_embeddings(token_ids))     # [.., V]（无视 hidden_states）
apply_step_logits(logits, token_ids=..)  -> logits + bias   # 单步修正
apply_block_logits(base_logits, token_ids=..)               # 整块修正（逐位置同一公式）
sample_block_tokens(base_logits, first_prev_token_ids=..)   # 推理：逐 token 采样
```

`compute_step_bias` 是唯一定义「偏置怎么算」的地方，三个方法都复用它；`VanillaMarkov` 里 `hidden_states` 参数被 `del` 掉——它是严格无记忆（memoryless）的，偏置只看前一个 token id，不看主干隐状态。

#### 4.1.3 源码精读

**权重定义**——两个矩阵构成低秩分解：

- [deepspec/modeling/dspark/markov_head.py:8-18](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L8-L18)：`VanillaMarkov.__init__` 校验 `markov_rank > 0`，创建 `markov_w1 = nn.Embedding(vocab_size, markov_rank)` 与 `markov_w2 = nn.Linear(markov_rank, vocab_size, bias=False)`。注意 `w2` 无偏置项——它输出的是加在 logits 上的偏置，本身不需要额外自由度。

**偏置计算**——查表加投影：

- [deepspec/modeling/dspark/markov_head.py:20-32](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L20-L32)：`get_prev_embeddings` 把 token id 查成 \(r\) 维嵌入；`project_bias` 投影回词表维；`compute_step_bias` 串联两者，且第一行就 `del hidden_states`——vanilla 头是纯 token 条件的。

**两个扩展变体**（了解即可，DSpark 默认不用）：

- [deepspec/modeling/dspark/markov_head.py:93-122](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L93-L122)：`GatedMarkovHead` 在偏置前加一个由 `[hidden_states; W1[x]]` 经 sigmoid 门控，用隐状态调节前 token 嵌入的强度。
- [deepspec/modeling/dspark/markov_head.py:125-173](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L125-L173)：`RNNHead` 维护 GRU 式循环状态 \( s_k = g \odot s_{k-1} + (1-g) \odot \tilde{s} \)，让块内第 \(k\) 位能看到完整前缀历史 \(x_{<k}\)，突破一阶 Markov 假设。

**工厂函数与 `markov_rank=0` 退化**：

- [deepspec/modeling/dspark/markov_head.py:287-311](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L287-L311)：`build_markov_head` 读取 `config.markov_rank`；**等于 0 时直接返回 `None`**（即"没有 markov 头"），大于 0 时按 `config.markov_head_type` 派发到 vanilla / gated / rnn 三种实现。

**配置对比**——DSpark 开、DFlash 关：

- [config/dspark/dspark_qwen3_4b.py:18-24](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L18-L24)：DSpark 用 `markov_rank=256`、`markov_head_type='vanilla'`，且置信度头也拼接 markov 嵌入（`confidence_head_with_markov=True`，见 4.2.3）。
- [config/dflash/dflash_qwen3_4b.py:18-19](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_qwen3_4b.py#L18-L19)：DFlash 显式写 `markov_rank=0` 注释 "Disable markov head"——这就是 u5-l3 将展开的「DFlash = 同一套 DSpark 代码关掉若干开关」的第一处开关。

**接线点**——草稿模型在哪里持有它：

- [deepspec/modeling/dspark/qwen3/modeling.py:251-252](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L251-L252)：`Qwen3DSparkModel.__init__` 里 `self.markov_head = build_markov_head(config)`，markov 头是草稿模型的一个子模块，随 FSDP 一起训练、随 checkpoint 一起保存。Gemma4 侧对应实现在 `deepspec/modeling/dspark/gemma4/modeling.py`，逻辑对称。

#### 4.1.4 代码实践

**实践目标**：亲手验证低秩偏置的数值行为，并体会参数量的节省。

**操作步骤**（以下为示例代码，可在仓库根目录用 `python -c` 或临时脚本运行，仅依赖 torch）：

```python
# 示例代码：验证 VanillaMarkov 的低秩偏置
import torch
from deepspec.modeling.dspark.markov_head import VanillaMarkov

torch.manual_seed(0)
V, r = 151936, 256
head = VanillaMarkov(vocab_size=V, markov_rank=r)

n_params = sum(p.numel() for p in head.parameters())
print(f"markov 参数量 = {n_params:,}")        # 预期 2*V*r = 77,791,232
print(f"满词表矩阵参数量 = {V * V:,}")        # 约 23,084,540,000

prev = torch.tensor([[123]])                  # [1, 1] 前一个 token
bias = head.compute_step_bias(prev, None)     # [1, 1, V]
print(bias.shape)                             # torch.Size([1, 1, 151936])
# 手工复算：W2 @ W1[123]
manual = head.markov_w2(head.markov_w1(prev.long()))
print(torch.allclose(bias, manual))           # True
```

**需要观察的现象**：`bias` 形状是 `[1, 1, V]`（与输入 token id 的前缀维度保持一致，便于广播）；`compute_step_bias` 与手工 `w2(w1(x))` 逐位一致。

**预期结果**：参数量恰为 `2*V*r`，比满表少约 297 倍；`allclose` 为 `True`。（具体数值待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `markov_w2` 用 `bias=False`？

**答案**：`w2` 的输出是加在已有 logits 上的**偏置增量**，`logits_base` 本身已由冻结的 `lm_head` 产生；再加一个自由偏置向量会与 `logits_base` 的整体平移自由度冗余，训练上无益还多 \( |V| \) 个参数。

**练习 2**：把 `markov_rank` 从 256 调到 151936，参数量变成多少？这和满词表矩阵是什么关系？

**答案**：变成 \( 2|V|^2 \)，反而是满表的两倍（满表是 \( |V|^2 \)）。低秩分解只有在 \( r \ll |V| \) 时才省参数；\( r = |V| \) 时退化为一个秩至多为 \(|V|\) 的满秩分解，还多花一倍存储。这说明 \(r\) 是「偏置表有效秩」的先验假设。

**练习 3**：`VanillaMarkov.compute_step_bias` 为什么可以忽略 `hidden_states`？

**答案**：因为 vanilla 头的建模假设是一阶 Markov：块内槽位 \(t\) 的修正只条件于 \(x_{t-1}\) 这个 token id，主干隐状态的信息已经体现在 `logits_base` 里。`GatedMarkovHead` 与 `RNNHead` 则推翻这一假设，显式用上了 `hidden_states`。

---

### 4.2 apply_step_logits / apply_block_logits：训练与单步推理的修正

#### 4.2.1 概念说明

同一个「logits + bias(x_prev)」公式，在两个场景下有两个封装：

- **`apply_block_logits`（训练用）**：输入整块 `base_logits [B, num_blocks, block_size, V]` 与每个槽位的「前一个 token」`token_ids [B, num_blocks, block_size]`，一次加完所有偏置。关键在于训练时**前一个 token 是已知的真值**（teacher forcing）：监督信号的右移即可得到，所以整块可以完全并行修正，与块式并行 forward 天然匹配。
- **`apply_step_logits`（单步用）**：输入 `[batch, V]` 的单步 logits 与 `[batch]` 的前一个 token，修正一步。它是 `sample_block_tokens` 循环体的核心，也被暴露成模型级 API `sample_draft_token_step` 供外部做链式单步采样。

对 `VanillaMarkov` 而言两者在数学上完全等价（无记忆，逐位运算）；但对 `RNNHead`，`apply_block_logits` 会带着循环状态在块内展开（teacher-forced unroll），与独立调用 `compute_step_bias`（状态清零）**不等价**——这是「无记忆 vs 有状态」头的本质分界。

#### 4.2.2 核心流程

训练侧（teacher forcing，完全并行）：

```
forward() 内：
  anchor_token_ids = gather(input_ids, anchor_positions)        # 每块第 0 槽位的"前 token"= 锚点处真实 token
  prev_token_ids   = cat([anchor_token_ids, target_ids[:, :, :-1]])   # 块内槽位 t 的前 token = 真值 t-1
  draft_logits     = lm_head(output_hidden)                     # [B, num_blocks, block_size, V]
  draft_logits     = markov_head.apply_block_logits(draft_logits,
                          token_ids=prev_token_ids, ...)        # 一次加完整块偏置
```

推理侧单步（串行）：

```
step_logits = base_logits + compute_step_bias(prev_token_ids, hidden)
```

伪代码对比：

```
训练（并行）:   logits'[b, i, t, :] = logits[b, i, t, :] + B[ prev_true[b, i, t] ]     # t 一次算完
推理（串行）:   for t: logits_t = logits_t + B[prev]; x_t = sample(logits_t); prev = x_t
```

#### 4.2.3 源码精读

**两个修正方法**：

- [deepspec/modeling/dspark/markov_head.py:34-41](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L34-L41)：`apply_step_logits` 就是 `logits + compute_step_bias(token_ids, hidden_states)`，单步、逐 batch。
- [deepspec/modeling/dspark/markov_head.py:43-53](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L43-L53)：`apply_block_logits` 先处理空块短路（`base_logits.size(2) == 0` 直接返回），再对整块调用同一个 `compute_step_bias` 后相加——由于 vanilla 偏置逐位置独立，无需循环。

**训练侧接线（prev token 从哪来）**：

- [deepspec/modeling/dspark/qwen3/modeling.py:473-481](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L473-L481)：`prev_token_ids = cat([anchor_token_ids.unsqueeze(-1), target_ids[:, :, :-1]], dim=-1)`——块内第 0 槽位的「前一个 token」是锚点处的真实 token；第 \(t\) 槽位用的是真值标签 \(target[t-1]\)。这就是 teacher forcing。
- [deepspec/modeling/dspark/qwen3/modeling.py:482-493](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L482-L493)：先 `compute_logits` 得到原始 `draft_logits`，`markov_head is not None` 时调用 `apply_block_logits` 覆盖它。修正后的 `draft_logits` 进入 `DSparkForwardOutput`，交给 u4-l4 的 `compute_dspark_loss`——所以 **CE 与 L1 蒸馏作用的都是修正后的 logits**。

**置信度头对 markov 嵌入的复用**：

- [deepspec/modeling/dspark/qwen3/modeling.py:299-307](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L299-L307) 与 [deepspec/modeling/dspark/qwen3/modeling.py:504-516](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L504-L516)：当 `confidence_head_with_markov=True`（DSpark 默认配置）时，置信度头的输入是 `[主干隐状态; markov_w1[x_prev]]` 的拼接——markov 嵌入不只修正 logits，还为「这个位置会被目标模型接受吗」的预测提供前 token 信息。

**单步 API 的模型级封装**：

- [deepspec/modeling/dspark/qwen3/modeling.py:335-359](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L335-L359)：`sample_draft_token_step` 接收 `[batch, vocab]` 的 logits，`markov_head is None` 时原样返回（不修正），否则 `apply_step_logits` 后再采样。这给了评估侧做逐 token 链式提议的原子操作（Eagle3 式用法可对照 u6-l6）。

#### 4.2.4 代码实践

**实践目标**：验证「`apply_block_logits` ≡ 逐位置 `apply_step_logits`」这一无记忆性等价（本讲规格中的验证任务前半部分）。

**操作步骤**（示例代码）：

```python
# 示例代码：apply_block_logits 与逐条 apply_step_logits 一致性
import torch
from deepspec.modeling.dspark.markov_head import VanillaMarkov

torch.manual_seed(42)
head = VanillaMarkov(vocab_size=100, markov_rank=16)   # 小词表便于观察

B, num_blocks, block_size, V = 2, 3, 4, 100
base = torch.randn(B, num_blocks, block_size, V)
prev = torch.randint(0, V, (B, num_blocks, block_size))  # 模拟 teacher-forced 真值前 token

# 方式一：整块修正
out_block = head.apply_block_logits(base, token_ids=prev, hidden_states=None)

# 方式二：逐块逐位置用 apply_step_logits，再拼回去
outs = []
for i in range(num_blocks):
    blk = []
    for t in range(block_size):
        blk.append(head.apply_step_logits(
            base[:, i, t, :], token_ids=prev[:, i, t], hidden_states=None))
    outs.append(torch.stack(blk, dim=1))
out_step = torch.stack(outs, dim=1)

print(torch.equal(out_block, out_step))   # 预期 True（同一组逐位运算）
print(out_block.shape)                    # torch.Size([2, 3, 4, 100])
```

**需要观察的现象**：两种方式输出逐位相等；修正量 `out_block - base` 在每个位置只依赖对应 `prev` 位置的 token id——把 `prev[0,0,0]` 换成别的 id，只有 `[0,0,0,:]` 这一行的修正量变化。

**预期结果**：`torch.equal` 返回 `True`。（待本地验证；若因浮点路径差异报 False，可退用 `torch.allclose`，但按源码两者应是同一组查表加线性运算，应严格相等。）

**附加观察（选做）**：把 `VanillaMarkov` 换成 `RNNHead(vocab_size=100, markov_rank=16, hidden_size=8)`（需喂非 None 的 `hidden_states`，形状 `[B, num_blocks, block_size, 8]`），重复上述对比——此时**不再相等**，因为 `RNNHead.apply_block_logits` 带状态展开（见 [deepspec/modeling/dspark/markov_head.py:191-225](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L191-L225)），而独立单步的状态被清零（[deepspec/modeling/dspark/markov_head.py:175-189](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L175-L189)）。

#### 4.2.5 小练习与答案

**练习 1**：训练时块内第 0 槽位的 `prev_token_ids` 是什么？为什么它不用 `target_ids` 右移得到？

**答案**：是锚点位置的真实 token（`anchor_token_ids`）。块内槽位 \(t\) 预测的是锚点后第 \(t+1\) 个 token（u4-l1 的错位约定），所以第 0 槽位的「前一个 token」就是锚点 token 本身，而第 \(t \ge 1\) 槽位的前一个 token 是 `target_ids[t-1]`——`cat([anchor, target[:, :-1]])` 一次拼出整块的前 token 序列。

**练习 2**：训练用 teacher forcing、推理用自回归迭代，两者会不会「训练-推理不一致」？

**答案**：形式上公式完全一致（都是 `logits + B[x_prev]`），区别只在 \(x_{prev}\) 的来源：训练用真值，推理用自己采的 token。这与普通 LM 训练（next-token prediction 的 teacher forcing）与自回归解码的关系相同，存在 exposure bias，但一阶偏置的误差传播有限；`sample_block_tokens` 逐 token 迭代正是为了让推理时每个槽位的偏置都用**真实采出的**前 token 计算，而非假设块内并行结果。

**练习 3**：如果删掉 `apply_block_logits` 里的空块短路（`base_logits.size(2) == 0`）会怎样？

**答案**：功能上仍正确（对大小为 0 的维度做查表加投影得到空张量），短路是一次廉价的防御性提前返回，避免空块进入无谓的计算图。类似的空序列守卫在 `sample_block_tokens`、`sample_draft_tokens` 里也各有一份（见 4.3.3）。

---

### 4.3 sample_block_tokens：推理时的逐 token 迭代采样

#### 4.3.1 概念说明

推理时（评估侧提议），块内前一个 token 不再已知，必须**边采样边推进**：先修正第 0 槽位 logits（用锚点 token 作为前驱），采样出 \(x_0\)；再用 \(x_0\) 修正第 1 槽位，采样 \(x_1\)……共迭代 `block_size` 次。`sample_block_tokens` 就是这个循环的容器，它同时返回两个东西：

- `sampled_tokens [B, proposal_len]`：逐槽位采出的草稿 token（拼进 `verify_input_ids` 交给目标模型验证）；
- `corrected_logits [B, proposal_len, V]`：每个槽位**采样时实际使用的**修正后 logits——这是投机解码验证阶段计算草稿分布 \(q\) 的依据（u6-l3 的拒绝采样需要它）。

底层的采样原子是 `sample_tokens`：`temperature < 1e-5` 走 `argmax` 贪心，否则 softmax 加 `multinomial`。仓库默认 `temperature=0.0`，即贪心。

与 4.2 的关系：`sample_block_tokens` 的循环体 = `apply_step_logits` + `sample_tokens` + 状态推进（`prev_token_ids = next_token_ids`）。

#### 4.3.2 核心流程

```
输入: base_logits [B, L, V]          # L = proposal_len（默认 block_size=7）
      first_prev_token_ids [B]       # 每块第一个槽位的前驱 = 锚点 token
      hidden_states (可选)            # vanilla 头忽略；gated/rnn 头按槽位切片使用

prev = first_prev_token_ids
for t in 0 .. L-1:
    step_logits = base_logits[:, t] + compute_step_bias(prev, hidden[:, t])
    x_t         = sample_tokens(step_logits, temperature)     # 贪心或多项式
    prev        = x_t                                          # 关键：状态推进
返回 (stack(x_t), cat(step_logits))
```

复杂度：\(L\) 次小算子调用（查表 + \(r \times V\) 矩阵乘），无任何 transformer 前向——相对一次草稿主干 forward 可忽略，这就是「用极便宜的串行修正换块内一阶依赖」的性价比所在。

#### 4.3.3 源码精读

**循环本体**：

- [deepspec/modeling/dspark/markov_head.py:55-90](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L55-L90)：`sample_block_tokens`。L63-71 处理 `proposal_len == 0` 的空提议短路；L75 `prev_token_ids = first_prev_token_ids.long()` 初始化前驱；L76-89 循环——每个 `step_idx` 取出该槽位 `base_logits[:, step_idx, :]` 与可选的 `hidden_states[:, step_idx, ...]` 切片，`apply_step_logits` 修正，`sample_tokens` 采样，然后 L89 `prev_token_ids = next_token_ids` 完成状态推进；最后 stack/cat 拼回 `[B, L]` 与 `[B, L, V]`。
- [deepspec/modeling/dspark/markov_head.py:76-82](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L76-L82)：注意 `step_hidden` 的按槽位切片——`gated`/`rnn` 头在这里拿到逐位置隐状态，vanilla 头则照样丢弃。

**采样原子**：

- [deepspec/utils/sampling.py:20-27](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L20-L27)：`sample_tokens`——`temperature < 1e-5` 直接 `argmax`（贪心），否则展平后除温度、softmax、`multinomial`。这就是循环里「采样」一步的全部逻辑。
- [deepspec/utils/sampling.py:6-11](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/sampling.py#L6-L11)：`logits_to_probs` 用同一套温度约定把 logits 变分布——评估侧后续用它把 `corrected_logits` 转成草稿概率 \(q\) 供拒绝采样（u6-l3 详述）。

**模型级入口与 markov 关闭时的退化**：

- [deepspec/modeling/dspark/qwen3/modeling.py:309-333](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L309-L333)：`sample_draft_tokens` 是评估侧真正调用的入口。空提议短路后，若 `self.markov_head is None`（即 DFlash 的 `markov_rank=0`），**直接 `sample_tokens(base_logits, temperature)` 对整块独立采样并原样返回 base logits**——没有逐 token 依赖，块内各位置互不影响；否则委托给 `markov_head.sample_block_tokens`。
- [deepspec/eval/dspark/draft_ops.py:96-113](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L96-L113)：评估侧调用点。`build_dspark_proposal` 先 `compute_logits` 得到 `base_draft_logits`，然后 `first_prev_token_ids=draft_input_ids[:, 0]`——`draft_input_ids` 的第 0 个正是锚点 token（u6-l4 将展开整条块提议链路）。返回的 `draft_logits` 随后在 L140-143 经 `logits_to_probs` 变成 `draft_probs`。

**置信度头在推理侧的同类前驱拼接**：

- [deepspec/eval/dspark/draft_ops.py:57-68](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/draft_ops.py#L57-L68)：`_predict_confidence_logits` 用 `cat([draft_input_ids[:, :1], sampled_tokens[:, :-1]])` 构造前驱序列——与采样循环同一套「锚点 + 已采 token 右移」约定，保证置信度预测与采样看到的条件一致。

**开 / 关 markov 头的行为差异汇总**：

| 维度 | `markov_rank=256`（DSpark） | `markov_rank=0`（DFlash） |
| --- | --- | --- |
| `build_markov_head` 返回 | `VanillaMarkov` 实例 | `None` |
| 训练 forward | `apply_block_logits` 修正整块 logits | 跳过修正（[modeling.py:488](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L488) 的 `if` 不成立） |
| 推理采样 | `sample_block_tokens` 逐 token 迭代 | `sample_tokens` 整块独立采样 |
| 块内依赖 | 一阶（前一个 token） | 无（各槽位独立） |
| 额外参数 | 约 \(2|V|r \approx 78\mathrm{M}\)（Qwen3 词表） | 0 |
| 置信度头输入 | 可选拼接 markov 嵌入 | 不可拼接（头本身也被 DFlash 关闭） |

#### 4.3.4 代码实践

**实践目标**：完成规格任务后半部分——手工追踪（「手推」）一次 `sample_block_tokens` 的循环状态，并用代码核对。

**操作步骤**（示例代码）：

```python
# 示例代码：手推并核对 sample_block_tokens 的循环状态
import torch
from deepspec.modeling.dspark.markov_head import VanillaMarkov
from deepspec.utils.sampling import sample_tokens

torch.manual_seed(7)
head = VanillaMarkov(vocab_size=100, markov_rank=16)

B, L, V = 1, 4, 100
base = torch.randn(B, L, V)
first_prev = torch.tensor([37])                     # 模拟锚点 token

# 官方实现（temperature=0 贪心）
tokens, corrected = head.sample_block_tokens(
    base, first_prev_token_ids=first_prev, hidden_states=None, temperature=0.0)

# 手推循环：每步 prev 如何推进
prev = first_prev.clone()
trace = []
for t in range(L):
    step_logits = base[:, t, :] + head.compute_step_bias(prev, None)
    x_t = sample_tokens(step_logits.unsqueeze(1), temperature=0.0).squeeze(1)
    trace.append((t, int(prev), int(x_t)))
    prev = x_t
manual = torch.tensor([[x for _, _, x in trace]])

print(trace)                    # (槽位, 该步的 prev, 采出的 token)
print(torch.equal(tokens, manual))   # 预期 True
# 槽位 0 的 prev 是 37（锚点）；槽位 t>=1 的 prev 是上一行采出的 token
```

**需要观察的现象**：`trace` 中每行的第二个数等于上一行的第三个数（状态推进）；第一行的第二个数是 37（`first_prev_token_ids`）。`corrected` 的第 `t` 行与手推的 `step_logits` 相等。

**预期结果**：`torch.equal(tokens, manual)` 为 `True`。再换成 `temperature=1.0` 各跑一次，贪心结果确定，温度采样的结果每次运行可能不同，但 `corrected_logits` 不变（修正与采样解耦）。（待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`sample_block_tokens` 为什么必须返回 `corrected_logits` 而不让评估侧自己再加一遍偏置？

**答案**：返回的是**采样时实际条件于的** logits——逐槽位的偏置依赖前面已采出的 token，评估侧若想重建必须重放整个循环。且拒绝采样（u6-l3）需要草稿分布 \(q\) 与提议 token 严格来自同一组 logits；由实现一并返回，杜绝「采样用一套、算概率用另一套」的不一致。

**练习 2**：DFlash（`markov_rank=0`）推理时块内 7 个槽位独立采样，这会带来什么后果？

**答案**：草稿 token 之间没有任何依赖，块内容易出现不连贯的拼接（例如同块内重复或语义断裂），目标模型验证时大概率在前几个 token 就拒绝，平均接受长度下降。代价换来的是训练更简单（无偏置项）且推理侧无串行循环——这正是 DSpark/DFlash 的核心取舍，u5-l3 会用三份 config 做系统对比。

**练习 3**：`sample_block_tokens` 的循环里，`hidden_states[:, step_idx, ...]` 切片对 `VanillaMarkov` 有什么影响？

**答案**：没有影响——`VanillaMarkov.compute_step_bias` 第一行就 `del hidden_states`，切片只是被原样丢弃。该参数存在是为了让 `GatedMarkovHead` / `RNNHead` 等有状态变体能在同一循环框架里使用逐槽位隐状态，接口统一，行为由子类决定。

## 5. 综合实践

**任务：做一个「markov 头开关」对照小实验，量化偏置对采样结果的影响。**

1. **构造玩具场景**：`VanillaMarkov(vocab_size=50, markov_rank=8)`，随机 `base_logits [1, 7, 50]`（`block_size=7`），固定 `first_prev_token_ids`。
2. **三种模式各采样一次**：
   - 无 markov：`sample_tokens(base_logits, temperature=0.0)`（模拟 DFlash 路径，对照 [modeling.py:326-327](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L326-L327) 的退化分支）；
   - markov 贪心：`sample_block_tokens(..., temperature=0.0)`；
   - markov 温度采样：`temperature=1.0`。
3. **统计**：对比前两种模式的 7 个 token 有几个位置不同；验证第三种模式多次运行的 `corrected_logits` 恒定、仅采样结果变化。
4. **解释**：随机初始化的头偏置量级与 `base_logits` 相当，多数位置 argmax 被翻转；训练后偏置应学会「在 base 拿不准的地方才修正」。把 `markov_w2.weight` 乘 0.1 重跑第 2 步，观察差异位置数下降——偏置强度直接决定它对最终分布的话语权。

**预期结果**（待本地验证）：模式 1 与模式 2 的 token 序列在多数位置不同；缩小 `w2` 后差异收窄并趋向 0。这个实验把本讲三个模块串成一条线：低秩偏置（4.1）→ 训练整块修正（4.2）→ 推理逐 token 迭代（4.3）。

## 6. 本讲小结

- Markov 头用一个低秩双线性偏置 \( \mathrm{bias}(x_{\mathrm{prev}}) = W_2 W_1[x_{\mathrm{prev}}] \)（\(r=256\)，约 78M 参数）补偿 DSpark 块内并行构图丢失的 token 级一阶依赖。
- 训练走 `apply_block_logits`：teacher forcing 下每个槽位的「前一个 token」由锚点 token 加右移真值标签拼出，整块一次并行修正，修正后的 logits 才进入损失计算。
- 推理走 `sample_block_tokens`：以锚点 token 起步，逐槽位「修正 → 采样 → 推进 prev」，串行 \(L\) 次轻量算子，同时返回采样实际使用的 `corrected_logits` 供拒绝采样计算草稿分布。
- `apply_step_logits` 是两种场景共享的单步原子；对无记忆的 `VanillaMarkov`，整块修正与逐位置单步修正严格等价，对带状态的 `RNNHead` 不等价。
- `markov_rank=0` 时 `build_markov_head` 返回 `None`：训练跳过修正、推理退化为整块独立采样——这是 DFlash 复用 DSpark 代码的第一处配置开关。
- markov 嵌入还被置信度头复用（`confidence_head_with_markov=True`），为「该位置能否被目标接受」的预测提供前 token 特征。

## 7. 下一步学习建议

- **u4-l4（DSpark 训练损失）**：本讲修正后的 `draft_logits` 如何与 `target_ids`、`aligned_target_logits` 组合成 CE + L1 蒸馏 + 置信度的多目标损失，是 markov 头训练信号的来源。
- **u4-l5（Gemma4 变体）**：观察 gemma4 侧 `sample_draft_tokens` / `apply_block_logits` 的对称实现，体会 markov 头作为「模型族无关组件」被复用的方式。
- **提前翻看 u6-l3、u6-l4**：评估侧 `build_dspark_proposal` 如何把本讲的 `sample_block_tokens` 输出接入 `verify_draft_tokens` 的拒绝采样，把「修正后的 logits」变成「接受/拒绝」的统计量。
- 想深入变体，可对照阅读 `GatedMarkovHead`（[markov_head.py:93-122](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L93-L122)）与 `RNNHead`（[markov_head.py:125-284](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/markov_head.py#L125-L284)）的 `apply_block_logits`，理解「一阶无记忆 → 门控 → 循环状态」的依赖建模升级路线。
